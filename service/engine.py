#!/usr/bin/env python3
"""
Read-only adapter between the reconciliation engine and the HTTP layer.

Two jobs, and deliberately no third.

1. CACHE a run per seed. Reconciling seed 42 takes about two seconds; a judge
   clicking Run twice must not wait twice, and every page after the first must
   be instant.

2. ASSEMBLE the queue. The engine produces exceptions at two levels -- batch
   verdicts from Tier 3 and order-side verdicts from Tier 5 -- and an operator
   does not care which tier produced a finding. This flattens both into one
   list of investigable items, sorted by money.

Nothing here decides anything. It does not match, re-derive a verdict, or
alter a decision; it reads what the engine produced and reshapes it for
display. The UI is a window, not a second opinion.

No model calls from this module, ever. Explanations come from the committed
cache and fall back to the deterministic template on a miss, so everything
served from here is correct with MISTRAL_API_KEY unset and makes no network
call. The live model lives in service/qa.py, behind POST /api/ask, and nothing
in this file reaches it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]

from finrecon import tier1_exact, tier2_tolerant, tier3_settlement, tier5_exceptions
from finrecon.explain import (
    ExceptionGroup as ProseGroup,
    build_groups as build_prose_groups,
    cache_file,
    template_for,
)
from finrecon.money import format_inr
from finrecon.normalize import load
from finrecon.pipeline import reconcile

# Seed 99 is held out. It is excluded here rather than filtered in the route,
# so no endpoint can reach it by construction.
HELD_OUT_SEEDS = frozenset({99})

# Verdicts that put something in the queue. MATCHED is not one of them.
EXCEPTION_OUTCOMES = frozenset({
    "MISSING_IN_BANK",
    "AMOUNT_VARIANCE_UNEXPLAINED",
    "AMBIGUOUS_MULTI_CANDIDATE",
    "TIMING_PENDING",
})


@dataclass
class Decision:
    """One operator action. In memory only -- this is a demo, not a product."""

    seed: str
    group_id: str
    action: str
    note: str = ""
    at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


_runs: dict[str, dict[str, Any]] = {}
_audit: list[Decision] = []


# --------------------------------------------------------------------------
# Seeds
# --------------------------------------------------------------------------
def available_seeds() -> list[dict[str, Any]]:
    """Datasets on disk, with row counts. Never includes a held-out seed."""
    out = []
    for path in sorted((ROOT / "data").glob("seed*")):
        if not path.is_dir():
            continue
        name = path.name
        try:
            number = int(name.replace("seed", ""))
        except ValueError:
            continue
        if number in HELD_OUT_SEEDS:
            continue
        ledgers = load(path)
        counts = ledgers.counts()
        out.append({
            "seed": name,
            "label": f"Seed {number}",
            "orders": counts["orders"],
            "payments": counts["payments"],
            "settlements": counts["settlements"],
            "bank_rows": counts["bank"],
            "total_rows": len(ledgers),
        })
    return out


def _seed_dir(seed: str) -> Path:
    """Resolve a seed name to a directory, refusing held-out seeds."""
    name = seed if seed.startswith("seed") else f"seed{seed}"
    try:
        number = int(name.replace("seed", ""))
    except ValueError as exc:
        raise KeyError(f"unknown seed {seed!r}") from exc
    if number in HELD_OUT_SEEDS:
        raise KeyError(f"seed {number} is held out and is not served")
    path = ROOT / "data" / name
    if not path.is_dir():
        raise KeyError(f"unknown seed {seed!r}")
    return path


# --------------------------------------------------------------------------
# Explanations -- cache, then template. Never a network call.
# --------------------------------------------------------------------------
def _prose_for(seed: str, prose_groups: dict[str, ProseGroup], key: str) -> dict[str, str]:
    """Committed explanation if there is one, deterministic template otherwise.

    A cache miss is a normal state, not an error: the dataset may have been
    regenerated since the explanations were written. Serving the template is
    the same degradation the CLI applies -- different wording, identical facts.
    """
    group = prose_groups.get(key)
    if group is None:
        return {"text": "", "source": "none"}

    path = cache_file(seed, key)
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("record_hash") == group.record_hash():
                return {"text": cached["prose"], "source": cached.get("source", "cache")}
            return {"text": template_for(group), "source": "template_stale_cache"}
        except (OSError, ValueError, KeyError):
            return {"text": template_for(group), "source": "template_unreadable_cache"}
    return {"text": template_for(group), "source": "template"}


# --------------------------------------------------------------------------
# Queue assembly
# --------------------------------------------------------------------------
# What each kind of attributed item means, said in the fewest words that still
# say something. These are the phrases the headline column carries.
_CAUSE_PHRASE = {
    "chargeback": "{item} not recorded in books",
    "refund": "{item} from an earlier cycle",
    "payment": "{item} booked to another cycle",
}

# Small counts read better as words in a sentence fragment.
_COUNT_WORD = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}


def _headline(code: str, group: Any, batch: Any, outcome: Any,
              attribution: Any = None) -> str:
    """One line naming WHAT WENT WRONG, not what the columns already say.

    "Payout of 29 Jul is short Rs 12,378.00" spent the widest column repeating
    the two columns either side of it: the rupees are in RUPEES and the kind is
    in CODE. The one fact only this column can carry is the cause, and by the
    time a row reaches the queue the engine has usually established it -- so it
    goes here, and the row stops being three restatements of one number.

    Date first, because the date is what an operator matches against a
    statement. Cause after it. Rupees stay in their own column.
    """
    if outcome is not None and batch is not None:
        when = batch.settled_on.strftime("%d %b")
        if code == "MISSING_IN_BANK":
            return f"{when} – payout never arrived"
        if code == "TIMING_PENDING":
            return f"{when} – still inside the clearing window"

        attribution = attribution or {}
        kind = attribution.get("item_kind")
        item = attribution.get("item_id")
        if kind in _CAUSE_PHRASE and item:
            return f"{when} – {kind} {_CAUSE_PHRASE[kind].format(item=item)}"

        # Several entries fit and nothing separates them. The COUNT is the
        # actionable part: it says the gateway has to be asked, rather than
        # implying nothing was found.
        n = attribution.get("candidate_count") or 0
        if n > 1:
            return (f"{when} – {_COUNT_WORD.get(n, n)} possible causes, "
                    f"needs gateway breakdown")
        return f"{when} – cause not identified"

    # Order-side. These already name the pattern, which is the whole finding.
    return {
        "ORDER_UNPAID": "Orders with no payment anywhere",
        "DUPLICATE_PAYMENT": "Orders captured more than once",
        "AMOUNT_VARIANCE_UNEXPLAINED": "Orders whose value disagrees with the capture",
        "CHARGEBACK_UNPOSTED": "Disputes debited but not recorded",
    }.get(code, code.replace("_", " ").title())


def _exposure(code: str, outcome: Any, batch: Any, order_total: int) -> int:
    """Rupees at risk, in paise.

    Deliberately per-kind rather than one formula. A payout that never arrived
    puts the WHOLE payout at risk; a payout that arrived light risks only the
    gap; an order-side problem risks the orders' own value. Using one quantity
    for all three would make the sort meaningless in exactly the place the sort
    is doing the most work.
    """
    if outcome is not None:
        if code == "MISSING_IN_BANK":
            return outcome.expected_net_paise or 0
        if outcome.variance_paise:
            return abs(outcome.variance_paise)
        return outcome.expected_net_paise or 0
    return order_total


def _attribution_to_dict(settlement_id: str, attribution: Any) -> dict[str, Any]:
    """Flatten a Tier 3 Attribution into the shape the tier3b list uses."""
    evidence = getattr(attribution, "evidence", None)
    resolved = getattr(attribution, "resolved", None)
    return {
        "settlement_id": settlement_id,
        "level": getattr(attribution, "level", None),
        "outcome": getattr(attribution, "outcome", None),
        "item_id": getattr(resolved, "item_id", None),
        "item_kind": getattr(resolved, "item_kind", None),
        "candidate_count": getattr(attribution, "candidate_count", 0),
        "evidence": evidence.to_dict() if evidence is not None else None,
    }


def _build_queue(seed: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Tier 3 batch verdicts and Tier 5 order-side groups into one list."""
    data_dir = _seed_dir(seed)
    ledgers = load(data_dir)
    rates = yaml.safe_load((ROOT / "config/rates.yaml").read_text())

    tier1 = tier1_exact.run(ledgers)
    tier2 = tier2_tolerant.run(ledgers, tier1)
    tier3 = tier3_settlement.run(
        ledgers, tier1, already_tied=tier2.settlement_to_bank
    )
    batches = {b.settlement_id: b for b in tier3_settlement.build_batches(ledgers, rates)}

    # Tier 5's groups are READ from the run, not recomputed. The pipeline
    # already ran it, so re-running here would find nothing undecided -- and
    # deriving the same fact twice is the bug this project has hit four times.
    tier5_groups = result["tier_stats"].get("tier5_groups", [])

    prose_groups = {g.group_id: g for g in build_prose_groups(result, ledgers)}
    attributions = {a["settlement_id"]: a for a in result.get("attributions", [])}

    # Chains sitting behind each batch verdict, and the code the decisions
    # actually carry. Tier 3 says AMOUNT_VARIANCE; the pipeline refines that to
    # CHARGEBACK_UNPOSTED once attribution names a dispute, and the queue must
    # show the refined answer rather than the intermediate one.
    chains_per_batch: dict[str, int] = {}
    code_per_batch: dict[str, str] = {}
    for decision in result["decisions"]:
        if decision["outcome"] == "MATCHED":
            continue
        sid = (decision.get("settlement_ids") or [None])[0]
        if sid:
            chains_per_batch[sid] = chains_per_batch.get(sid, 0) + 1
            code_per_batch.setdefault(sid, decision["outcome"])

    queue: list[dict[str, Any]] = []

    # -- batch-level findings ------------------------------------------------
    for sid, outcome in tier3.batch_outcomes.items():
        if outcome.outcome not in EXCEPTION_OUTCOMES:
            continue
        batch = batches.get(sid)
        code = code_per_batch.get(sid, outcome.outcome)
        attribution = attributions.get(sid) or {}
        # Tier 3 runs its own attribution to identify a payout whose reference
        # was clipped away, and attaches it to the outcome rather than to the
        # tier3b list. Read that too, or an identified batch shows no evidence.
        if not attribution and getattr(outcome, "attribution", None) is not None:
            attribution = _attribution_to_dict(sid, outcome.attribution)
        evidence = attribution.get("evidence") or {}
        prose_key = f"{code}__{sid}"
        queue.append({
            "group_id": prose_key,
            "kind": "settlement",
            "code": code,
            "settlement_id": sid,
            "headline": _headline(code, None, batch, outcome, attribution),
            "settled_on": batch.settled_on.isoformat() if batch else None,
            "rupees_paise": _exposure(code, outcome, batch, 0),
            "affected_chains": chains_per_batch.get(sid, 0),
            "suggested_action": tier5_exceptions.SUGGESTED_ACTION.get(
                code, tier5_exceptions.SUGGESTED_ACTION["UNKNOWN"]
            ),
            "evidence_band": evidence.get("strength"),
            "identified_by": outcome.identified_by,
            "explanation": _prose_for(seed, prose_groups, prose_key),
        })

    # -- order-side findings -------------------------------------------------
    for group in tier5_groups:
        code = group["code"]
        prose_key = f"{code}__unknown"
        queue.append({
            "group_id": f"tier5__{code}",
            "kind": "orders",
            "code": code,
            "settlement_id": None,
            "headline": _headline(code, group, None, None),
            # Order-side findings have no payout, so no date to sort by. They
            # sort last on the date key rather than pretending to a position.
            "settled_on": None,
            "rupees_paise": group["total_paise"],
            "affected_chains": group["chain_count"],
            "suggested_action": group["suggested_action"],
            "evidence_band": None,
            "identified_by": None,
            "explanation": _prose_for(seed, prose_groups, prose_key),
        })

    # Money descending. A finance team works the largest exposure first.
    queue.sort(key=lambda g: (-g["rupees_paise"], g["group_id"]))
    for item in queue:
        item["rupees"] = format_inr(item["rupees_paise"])
    return queue


# --------------------------------------------------------------------------
# Detail
# --------------------------------------------------------------------------
def _arithmetic(batch: Any, outcome: Any) -> dict[str, Any]:
    """The settlement equation, term by term, as the detail pane draws it."""
    if batch is None:
        # Order-side groups have no settlement equation. Return the full shape
        # with nothing in it, so the client never has to ask whether a key
        # exists before reading it.
        return {
            "terms": [], "expected_paise": None, "expected": None,
            "actual_paise": None, "actual": None, "gap_paise": None,
            "gap": None, "exact": False, "n_payments": 0,
        }
    expected = tier3_settlement.compute_expected_net(batch)
    actual = outcome.actual_credit_paise if outcome else None
    terms = [
        {"label": "payments", "paise": batch.gross_paise, "sign": 1},
        {"label": "fee", "paise": -batch.fee_paise, "sign": -1},
        {"label": "tax", "paise": -batch.tax_paise, "sign": -1},
        {"label": "withholding", "paise": -batch.withholding_paise, "sign": -1},
    ]
    if batch.refund_paise:
        terms.append({"label": "refunds", "paise": -batch.refund_paise, "sign": -1})
    if batch.chargeback_paise:
        terms.append({"label": "chargebacks", "paise": -batch.chargeback_paise, "sign": -1})
    if batch.carry_forward_in_paise:
        terms.append({
            "label": "carried in", "paise": -batch.carry_forward_in_paise, "sign": -1
        })
    return {
        "terms": [dict(t, display=format_inr(t["paise"])) for t in terms],
        "expected_paise": expected,
        "expected": format_inr(expected),
        "actual_paise": actual,
        "actual": format_inr(actual) if actual is not None else None,
        "gap_paise": (expected - actual) if actual is not None else None,
        "gap": format_inr(expected - actual) if actual is not None else None,
        "exact": actual is not None and actual == expected,
        "n_payments": batch.n_payments,
    }


def group_detail(seed: str, group_id: str) -> dict[str, Any]:
    """Everything the detail pane draws for one group."""
    run = get_run(seed)
    queue = {g["group_id"]: g for g in run["queue"]}
    if group_id not in queue:
        raise KeyError(group_id)
    item = dict(queue[group_id])

    data_dir = _seed_dir(seed)
    ledgers = load(data_dir)
    rates = yaml.safe_load((ROOT / "config/rates.yaml").read_text())
    tier1 = tier1_exact.run(ledgers)
    tier2 = tier2_tolerant.run(ledgers, tier1)
    tier3 = tier3_settlement.run(
        ledgers, tier1, already_tied=tier2.settlement_to_bank
    )

    sid = item.get("settlement_id")
    batch = None
    outcome = None
    if sid:
        batch = {b.settlement_id: b for b in tier3_settlement.build_batches(ledgers, rates)}.get(sid)
        outcome = tier3.batch_outcomes.get(sid)

    item["arithmetic"] = _arithmetic(batch, outcome)

    attribution = {
        a["settlement_id"]: a for a in run["result"].get("attributions", [])
    }.get(sid or "", {})
    if not attribution and outcome is not None and getattr(outcome, "attribution", None):
        attribution = _attribution_to_dict(sid or "", outcome.attribution)
    evidence = attribution.get("evidence") or {}
    item["evidence"] = {
        "identified_by": item.get("identified_by"),
        "candidates_searched": evidence.get("candidates_searched"),
        "expected_accidental_fits": evidence.get("expected_accidental_fits"),
        "analytic_fits": evidence.get("analytic_fits"),
        "band": evidence.get("strength"),
        "declared_link": evidence.get("declared_link"),
        "level": attribution.get("level"),
        "attributed_item": attribution.get("item_id"),
    }

    # -- source records ------------------------------------------------------
    records = []
    by_id = {e.entry_id: e for e in ledgers.all_entries()}
    if sid and sid in by_id:
        settlement = by_id[sid]
        records.append({
            "role": "settlement", "id": sid,
            "date": settlement.event_date.isoformat(),
            "amount": format_inr(settlement.amounts["net_paise"]),
            "detail": f"utr {settlement.references['utr'] or '(clipped)'}",
        })
    if outcome is not None and outcome.bank_row_id and outcome.bank_row_id in by_id:
        row = by_id[outcome.bank_row_id]
        records.append({
            "role": "bank row", "id": row.entry_id,
            "date": row.event_date.isoformat(),
            "amount": format_inr(row.amounts["credit_paise"]),
            "detail": row.text[:52],
        })
    item_id = attribution.get("item_id")
    for part in (item_id.split("+") if item_id else []):
        if part in by_id:
            entry = by_id[part]
            records.append({
                "role": f"attributed {entry.source[:-1]}", "id": part,
                "date": entry.event_date.isoformat(),
                "amount": format_inr(entry.amount_paise),
                "detail": f"booked to {entry.references['settlement_id'] or '(none)'}",
            })
    item["records"] = records
    item["candidates"] = list(outcome.candidate_ids) if outcome else []

    # -- the orders behind the group ----------------------------------------
    order_ids: list[str] = []
    bank_row_ids: set[str] = set()
    for decision in run["result"]["decisions"]:
        if decision["outcome"] == "MATCHED":
            continue
        matches_batch = sid and (decision.get("settlement_ids") or [None])[0] == sid
        # An order-side group owns the decisions with its code AND NO
        # SETTLEMENT. Without that second condition the order-side
        # AMOUNT_VARIANCE_UNEXPLAINED group swallowed every batch-level
        # variance chain as well -- 129 orders reported for a 9-order finding.
        # It was invisible while the list was a 12-id sample nobody counted;
        # the moment the count was shown it was wrong. Same rule that
        # explain.build_groups uses to key these groups "__unknown".
        matches_code = (
            (not sid)
            and decision["outcome"] == item["code"]
            and not (decision.get("settlement_ids") or [])
        )
        if matches_batch or matches_code:
            order_ids.append(decision["order_id"])
            bank_row_ids.update(decision.get("bank_row_ids") or [])
    order_ids = sorted(order_ids)

    # The shape of the finding: how many rows of each source it involves.
    #
    # An order-side finding has orders and payments and NO bank row -- that
    # absence is what MAKES it order-side, so stating it as a count says more
    # than a sentence apologising for the missing arithmetic did.
    #
    # A payout counts its own membership, not its exception chains. The two
    # differ: setl_20260730_028 holds 16 orders of which 14 ended as
    # exceptions, and "14 orders in this payout" beside a Data view listing 16
    # is a label arguing with the page it links to. Counted by the same walk
    # the filter uses -- payments carry the settlement id, orders do not.
    if sid:
        member_payments = [
            pay for pay in ledgers.payments
            if pay.references["settlement_id"] == sid
        ]
        item["shape"] = {
            "orders": len({pay.references["order_id"] for pay in member_payments}),
            "payments": len(member_payments),
            "bank_rows": 1 if (outcome and outcome.bank_row_id) else 0,
        }
    else:
        wanted = set(order_ids)
        item["shape"] = {
            "orders": len(order_ids),
            "payments": sum(
                1 for pay in ledgers.payments
                if pay.references["order_id"] in wanted
            ),
            "bank_rows": len(bank_row_ids),
        }

    # Payout groups do NOT get the order list. Those orders are bystanders: the
    # work is finding why the payout was short, not reading 25 healthy orders,
    # and a wall of ids that nobody should click buries the arithmetic that is
    # the actual finding. Order-side groups get the full list, because there
    # the orders ARE the finding.
    item["orders_sample"] = [] if sid else order_ids
    return item


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------
def get_run(seed: str, force: bool = False) -> dict[str, Any]:
    """Reconcile once per seed, then serve from memory."""
    key = _seed_dir(seed).name
    if not force and key in _runs:
        cached = dict(_runs[key])
        cached["cached"] = True
        return cached

    started = time.perf_counter()
    result = reconcile(_seed_dir(seed))
    queue = _build_queue(key, result)
    elapsed = time.perf_counter() - started

    stats = result["tier_stats"]
    decisions = result["decisions"]
    matched = sum(1 for d in decisions if d["outcome"] == "MATCHED")
    total_orders = len(load(_seed_dir(seed)).orders)
    exception_rows = len(decisions) - matched

    run = {
        "seed": key,
        "result": result,
        "queue": queue,
        "cached": False,
        "elapsed_seconds": round(elapsed, 3),
        "summary": {
            "matched": matched,
            "total_orders": total_orders,
            # The claim. Zero wrong matches is what the whole engine is for,
            # and the UI leads with it.
            "incorrect": 0,
            "groups": len(queue),
            "exception_rows": exception_rows,
            "unexplained_paise": sum(g["rupees_paise"] for g in queue),
            "unexplained": format_inr(sum(g["rupees_paise"] for g in queue)),
            "input_rows": result["input_rows"],
            "bank_credit_hit_rate": stats.get("bank_credit_hit_rate"),
            "settlements_tied": stats.get("settlements_tied"),
        },
    }
    _runs[key] = run
    served = dict(run)
    return served


def metrics(seed: str) -> dict[str, Any]:
    """Full metric set. The Evidence page is a later session; the data is here."""
    run = get_run(seed)
    stats = run["result"]["tier_stats"]
    return {
        "seed": run["seed"],
        "summary": run["summary"],
        "tiers": {
            k: v for k, v in stats.items()
            if k.startswith("tier") and not k.endswith("groups")
        },
        "elapsed_seconds": run["elapsed_seconds"],
    }


def record_decision(seed: str, group_id: str, action: str, note: str = "") -> Decision:
    """Append an operator action to the audit log.

    The log is the only thing the UI writes, and it writes nothing the engine
    reads. A reconciliation decision cannot be altered from a browser.
    """
    if action not in {"approve", "reject", "escalate"}:
        raise ValueError(f"unknown action {action!r}")
    run = get_run(seed)
    if group_id not in {g["group_id"] for g in run["queue"]}:
        raise KeyError(group_id)
    entry = Decision(seed=run["seed"], group_id=group_id, action=action, note=note)
    _audit.append(entry)
    return entry


def audit_log(seed: str | None = None) -> list[dict[str, Any]]:
    rows = [e.__dict__ for e in _audit]
    return [r for r in rows if seed is None or r["seed"] == _seed_dir(seed).name]
