#!/usr/bin/env python3
"""
The reconciliation pipeline.

    Tier 0  normalize        six CSVs   -> canonical LedgerEntry objects
    Tier 1  tier1_exact      exact       -> matches at confidence 1.0
    Tier 2  tier2_tolerant   relaxed     -> matches at confidence 0.7-0.9
    Tier 3  tier3_settlement arithmetic  -> matches at 0.90-0.95, plus the
                                            first real exception verdicts

`reconcile()` is the whole engine's entry point, and it is deliberately a thin
composition rather than a place where logic accumulates. Each tier owns its own
module; this file owns only the order they run in and the shape they hand back.

Tiers are strictly additive: each sees only what the previous ones declined, so
it can add decisions but never overturn one. That is what makes `tiers=` a real
before/after measurement rather than two unrelated runs -- and it is why Tier 3
can be measured against Tier 1 alone, with `tiers=(1, 3)`, to show what the
settlement arithmetic recovers on its own rather than what is left after Tier 2
has already taken the easy half.

The ground-truth firewall
-------------------------
`reconcile()` returns a plain JSON-serialisable dict, and this module does NOT
import eval/ at the top level. That is not stylistic. eval/score.py opens
ground_truth.json, so a module-level import here would put the oracle one
import away from the matcher, and the firewall in CLAUDE.md would hold only by
everyone's continued good intentions. Scoring is pulled in inside `main()`,
where it is a reporting step and visibly not part of the reconciliation path.

Run:
    python -m finrecon.pipeline --data data/seed42
    python -m finrecon.pipeline --data data/seed42 --out result.json
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

from . import (tier1_exact, tier2_tolerant, tier3_attribution,
               tier3_settlement, tier5_exceptions)
from .normalize import load

# What the engine asserts about a bank row it tied to a settlement, and about a
# row that cannot be a payout at all. Anything else is left undispositioned --
# absence of a verdict, which is the honest state for a credit no tier could
# account for.
BANK_MATCHED = "MATCHED"
BANK_IGNORED = "IGNORED"

ALL_TIERS = (1, 2, 3, 5)


def _decision(match: Any, tier: int) -> dict[str, Any]:
    """One engine decision, with the evidence that produced it attached.

    `notes` is never scored. It exists so a decision stays explainable months
    later, when someone asks why this order was called reconciled and what
    slack, if any, the match rested on.
    """
    return {
        "order_id": match.order_id,
        "outcome": "MATCHED",
        "payment_ids": [match.payment_id],
        "settlement_ids": [match.settlement_id],
        "bank_row_ids": [match.bank_row_id],
        "notes": {
            "tier": tier,
            "confidence": match.confidence,
            "rules_fired": list(match.rules_fired),
            "evidence": dict(match.evidence),
        },
    }


def reconcile(
    data_dir: str | Path,
    rates_path: str | Path = "config/rates.yaml",
    posting_window_days: int = tier1_exact.DEFAULT_POSTING_WINDOW_DAYS,
    tier2_window_days: int = tier2_tolerant.TIER2_POSTING_WINDOW_DAYS,
    as_of: date | None = None,
    tiers: Sequence[int] = ALL_TIERS,
) -> dict[str, Any]:
    """Normalise, run the requested tiers, and report what was decided.

    A chain a tier fully resolved gets a MATCHED decision. A chain Tier 3 could
    reproduce the payout for but found no credit against gets an exception
    code. Everything else is returned undecided -- declining to match is not
    the same claim as "this is an exception", and collapsing the two would
    invent a queue out of work later tiers are supposed to finish.
    """
    started = time.perf_counter()
    tiers = tuple(tiers)

    ledgers = load(data_dir)
    tier1 = tier1_exact.run(
        ledgers, rates_path=rates_path, posting_window_days=posting_window_days
    )

    decisions = [_decision(m, tier=1) for m in tier1.matches]
    settlement_to_bank = dict(tier1.settlement_to_bank)
    matched_orders = {m.order_id for m in tier1.matches}

    attributions: list[dict[str, Any]] = []
    tier_stats: dict[str, Any] = {
        "tier1": dict(tier1.stats),
        "tier1_bank_credit_hit_rate": tier1.bank_credit_hit_rate,
        "tier1_root_causes": tier1.root_causes(),
    }

    tier2 = None
    if 2 in tiers:
        tier2 = tier2_tolerant.run(
            ledgers, tier1, rates_path=rates_path, posting_window_days=tier2_window_days
        )
        decisions.extend(_decision(m, tier=2) for m in tier2.matches)
        settlement_to_bank.update(tier2.settlement_to_bank)
        matched_orders |= {m.order_id for m in tier2.matches}
        tier_stats["tier2"] = dict(tier2.stats)
        tier_stats["tier2_confidence"] = {
            str(k): v for k, v in tier2.confidence_histogram().items()
        }

    tier3 = None
    if 3 in tiers:
        # Hand over what Tier 2 already claimed, so no batch is matched twice.
        already = tier2.settlement_to_bank if tier2 is not None else {}
        tier3 = tier3_settlement.run(
            ledgers, tier1, already_tied=already, rates_path=rates_path, as_of=as_of
        )
        decisions.extend(_decision(m, tier=3) for m in tier3.matches)
        settlement_to_bank.update(tier3.settlement_to_bank)
        matched_orders |= {m.order_id for m in tier3.matches}

        # The first real exception verdicts in the pipeline. Tier 3 can make
        # them because it reproduced what the payout should have been, so it
        # knows the money is absent rather than merely unfound.
        order_cycle = {}
        for p in ledgers.payments:
            oid = p.references["order_id"]
            if oid:
                order_cycle.setdefault(oid, p.references["settlement_id"])
        for order_id, code in tier3.chain_exceptions.items():
            if order_id in matched_orders:
                continue
            sid = order_cycle.get(order_id)
            outcome = tier3.batch_outcomes.get(sid or "")
            decisions.append(
                {
                    "order_id": order_id,
                    "outcome": code,
                    "payment_ids": [],
                    # The batch this exception came FROM. Carried so the
                    # explanation layer can group 130 rows into the handful of
                    # payouts that actually caused them -- an operator works
                    # payouts, not order lines.
                    "settlement_ids": [sid] if sid else [],
                    "bank_row_ids": (
                        [outcome.bank_row_id]
                        if outcome and outcome.bank_row_id else []
                    ),
                    "notes": {"tier": 3, "source": "batch_outcome"},
                }
            )
        tier_stats["tier3"] = dict(tier3.stats)
        tier_stats["tier3_outcomes"] = tier3.outcome_counts()
        tier_stats["tier3_identified_by"] = {
            route: tier3.stats.get(f"identified_by_{route}", 0)
            for route in ("utr", "amount", "attribution")
        }
        tier_stats["tier3_variances"] = [
            {
                "settlement_id": o.settlement_id,
                "expected_net_paise": o.expected_net_paise,
                "actual_credit_paise": o.actual_credit_paise,
                "variance_paise": o.variance_paise,
            }
            for o in tier3.variances()
        ]
        tier_stats["tier3_self_check"] = {
            "exact": tier3.self_check_result.exact,
            "total": tier3.self_check_result.total,
            "passed": tier3.self_check_result.passed,
        }

    # -- Tier 3b: attribution. Diagnostic, not a matcher.
    # It explains variances and infers membership; it emits NO decisions, so
    # coverage and precision are unchanged by construction. That is the point --
    # an explanation is not a match, and letting attribution create matches is
    # exactly how a subset-sum finds a plausible set of payments behind a
    # customer's direct transfer.
    if 3 in tiers:
        claimed_rows = set(settlement_to_bank.values())
        unexplained = [
            b for b in ledgers.bank
            if b.entry_type == "bank_credit" and b.entry_id not in claimed_rows
        ]
        # Batches no tier matched, paired to their credit on the UTR alone so a
        # gap the size of a whole refund is explainable rather than invisible.
        # Explanation only -- never promoted to a match.
        still_open = [
            s.entry_id for s in ledgers.settlements
            if s.entry_id not in settlement_to_bank
        ]
        explain_only = tier3_attribution.pair_by_utr(
            ledgers, still_open, claimed_rows
        )
        attribution = tier3_attribution.run(
            ledgers, {**settlement_to_bank, **explain_only}, unexplained,
            rates_path=rates_path,
        )
        # Attribution refines the exception CODE, which settlement could not
        # know when it ruled. A shortfall settlement calls
        # AMOUNT_VARIANCE_UNEXPLAINED is a dispute if -- and only if -- L3
        # found the chargeback behind it. Where nothing was found the code
        # stays "unexplained", because a shortfall with no ledger counterpart
        # is not evidence of a dispute, it is evidence of not knowing.
        cause_by_settlement = {
            a.settlement_id: a for a in attribution.attributions
            if a.level == tier3_attribution.L3_UNPOSTED_CHARGEBACK
            and a.outcome == tier3_attribution.OUTCOME_ATTRIBUTED
        }
        # Tier 3 now runs the same search itself, to identify a payout whose
        # reference was clipped away. Those attributions must refine the code
        # too -- otherwise a dispute Tier 3 has already named is reported as an
        # unexplained variance purely because a different object holds it.
        if tier3 is not None:
            for outcome in tier3.batch_outcomes.values():
                found = getattr(outcome, "attribution", None)
                if (found is not None
                        and getattr(found, "level", None)
                        == tier3_attribution.L3_UNPOSTED_CHARGEBACK
                        and getattr(found, "outcome", None)
                        == tier3_attribution.OUTCOME_ATTRIBUTED):
                    cause_by_settlement[outcome.settlement_id] = found
        if cause_by_settlement and tier3 is not None:
            payment_cycle = {
                p.entry_id: p.references["settlement_id"] for p in ledgers.payments
            }
            order_payment = {}
            for p in ledgers.payments:
                order_payment.setdefault(p.references["order_id"], []).append(p.entry_id)
            for decision in decisions:
                if decision["outcome"] != "AMOUNT_VARIANCE_UNEXPLAINED":
                    continue
                pids = order_payment.get(decision["order_id"], [])
                if len(pids) == 1 and payment_cycle.get(pids[0]) in cause_by_settlement:
                    decision["outcome"] = "CHARGEBACK_UNPOSTED"
                    decision["notes"]["refined_by"] = "attribution_L3"

        tier_stats["tier3b"] = dict(attribution.stats)
        tier_stats["tier3b_audit_entries"] = len(attribution.audit)
        tier_stats["tier3b_by_level"] = attribution.by_level()
        # Surfaced at the top level so the scorer can ask whether the engine
        # named the RIGHT cause. Coverage and precision cannot see any of this.
        attributions = [
            {
                "settlement_id": a.settlement_id,
                "level": a.level,
                "outcome": a.outcome,
                "delta_paise": a.delta_paise,
                "item_id": a.resolved.item_id if a.resolved else None,
                "item_kind": a.resolved.item_kind if a.resolved else None,
                "confidence": a.confidence,
                # Describes the attribution; never gates a match.
                "evidence": a.evidence.to_dict() if a.evidence else None,
                # How many candidates the level found. "Nothing fits" and
                # "several fit and I cannot choose" are different findings and
                # an operator acts on them differently.
                "candidate_count": len(a.candidates),
                "candidate_items": [
                    "+".join(c.components) if c.components else c.item_id
                    for c in a.candidates[:6]
                ],
            }
            for a in attribution.attributions
        ]

    # -- Tier 5: name what nothing else decided ---------------------------
    # Types chains that were ALREADY declined. It reads the reason each gate
    # recorded and never re-derives it, and it cannot match anything -- every
    # decision it emits carries a non-MATCHED code, for an order no earlier
    # tier decided. Coverage, precision and recall are unreachable from here
    # by construction.
    tier5 = None
    if 5 in tiers:
        decided = {d["order_id"] for d in decisions}
        undecided = [
            o.entry_id for o in ledgers.orders if o.entry_id not in decided
        ]
        declines = tier5_exceptions.merge_declines(
            tier1.declines,
            tier2.declines if tier2 is not None else {},
            tier3.declines if tier3 is not None else {},
        )
        tier5 = tier5_exceptions.run(ledgers, undecided, declines)
        for item in tier5.exceptions:
            decisions.append(
                {
                    "order_id": item.order_id,
                    "outcome": item.code,
                    "payment_ids": [],
                    "settlement_ids": [],
                    "bank_row_ids": [],
                    "notes": {
                        "tier": 5,
                        "reason": item.reason,
                        "entities": list(item.entities),
                        "group": item.group_key,
                    },
                }
            )
        tier_stats["tier5"] = dict(tier5.stats)
        tier_stats["tier5_by_code"] = tier5.counts_by_code()
        tier_stats["tier5_groups"] = [
            {
                "group_key": g.group_key,
                "code": g.code,
                "root_cause": g.root_cause,
                "chain_count": g.chain_count,
                "total_paise": g.total_paise,
                "suggested_action": g.suggested_action,
            }
            for g in tier5.groups
        ]

    # Bank rows the engine can positively speak to, and only those.
    bank_dispositions: dict[str, str] = {}
    for entry in ledgers.bank:
        if entry.entry_type == "bank_debit":
            # A debit is money leaving the account; it cannot be an inbound
            # payout. Ignoring it is a fact about direction, not a judgement
            # about what the row is, so this can be said without guessing.
            bank_dispositions[entry.entry_id] = BANK_IGNORED
    for bank_row_id in settlement_to_bank.values():
        bank_dispositions[bank_row_id] = BANK_MATCHED

    elapsed = time.perf_counter() - started
    n_credits = tier1.stats.get("bank_credits_total", 0)
    tier_stats["bank_credit_hit_rate"] = (
        len(settlement_to_bank) / n_credits if n_credits else 0.0
    )
    tier_stats["settlements_tied"] = len(settlement_to_bank)
    tier_stats["tiers_run"] = list(tiers)

    # Why each entity was NOT matched, per tier, in the order the tiers ran.
    #
    # The tiers have always recorded this -- every gate names the reason it
    # declined -- and until now it was merged into one map for Tier 5 and then
    # dropped on the floor. Merging is right for Tier 5, which needs one code
    # per chain, and wrong for a reader: "Tier 1 found no UTR in the narration,
    # then Tier 2 found the amount 12 rupees outside its band" is a different
    # and far more useful statement than the last reason alone.
    #
    # Kept per tier and unmerged for that reason. Additive: the scorer ignores
    # keys it does not know, so no metric can move because of this.
    declines_by_tier: dict[str, dict[str, Any]] = {}
    for name, source in (("tier1", tier1), ("tier2", tier2), ("tier3", tier3)):
        if source is None:
            continue
        declines_by_tier[name] = {
            entity_id: {
                "kind": decline.kind,
                "reason": decline.reason,
                "caused_by": decline.caused_by,
            }
            for entity_id, decline in source.declines.items()
        }

    return {
        "decisions": decisions,
        "attributions": attributions,
        "bank_dispositions": bank_dispositions,
        "elapsed_seconds": elapsed,
        "input_rows": len(ledgers),
        # Engine-side diagnostics. The scorer ignores unknown keys; these are
        # here so a bad score can be explained without re-running the tiers.
        "tier_stats": tier_stats,
        "declines": declines_by_tier,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _fmt(value: float | None) -> str:
    return "    n/a" if value is None else f"{value * 100:7.2f}%"


def _delta(before: float | None, after: float | None) -> str:
    if before is None or after is None:
        return "      -"
    return f"{(after - before) * 100:+7.2f}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the reconciliation pipeline.")
    ap.add_argument("--data", default="data/seed42", help="dataset directory")
    ap.add_argument("--rates", default="config/rates.yaml")
    ap.add_argument("--window", type=int,
                    default=tier1_exact.DEFAULT_POSTING_WINDOW_DAYS,
                    help="tier 1 posting window, in clearing days")
    ap.add_argument("--tier2-window", type=int,
                    default=tier2_tolerant.TIER2_POSTING_WINDOW_DAYS,
                    help="tier 2 posting window, in clearing days")
    ap.add_argument("--as-of", default=None,
                    help="reconcile as of this date (default: last bank line)")
    ap.add_argument("--tiers", default="1,2,3,5",
                    help="comma-separated tiers to run, e.g. 1,3")
    ap.add_argument("--out", default=None, help="write the result as JSON")
    ap.add_argument("--no-score", action="store_true",
                    help="reconcile only; do not open ground truth")
    args = ap.parse_args(argv)

    as_of = (
        datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else None
    )
    tiers = tuple(int(t) for t in args.tiers.split(",") if t.strip())
    kwargs = dict(
        rates_path=args.rates,
        posting_window_days=args.window,
        tier2_window_days=args.tier2_window,
        as_of=as_of,
    )
    result = reconcile(args.data, tiers=tiers, **kwargs)

    ledgers = load(args.data)
    tier1 = tier1_exact.run(
        ledgers, rates_path=args.rates, posting_window_days=args.window
    )
    print(tier1_exact.format_funnel(tier1))
    tier2 = None
    if 2 in tiers:
        tier2 = tier2_tolerant.run(
            ledgers, tier1, rates_path=args.rates,
            posting_window_days=args.tier2_window,
        )
        print(tier2_tolerant.format_funnel(tier1, tier2))
    if 3 in tiers:
        effective_as_of = as_of or max(
            (b.event_date for b in ledgers.bank), default=date.today()
        )
        tier3 = tier3_settlement.run(
            ledgers, tier1,
            already_tied=tier2.settlement_to_bank if tier2 else {},
            rates_path=args.rates, as_of=as_of,
        )
        print(tier3_settlement.format_report(tier3, effective_as_of))

    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nresult -> {args.out}")

    if args.no_score:
        return 0

    # Imported HERE, not at module scope: score.py reads ground_truth.json, and
    # the reconciliation path above must stay provably unable to reach it.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from eval.score import format_report, score_against_ground_truth

    gt = Path(args.data) / "ground_truth.json"
    report = score_against_ground_truth(result, gt)
    label = "+".join(f"t{t}" for t in tiers)
    print(format_report(report, label=f"{args.data} -- {label}"))

    def rung(these: tuple[int, ...]):
        return score_against_ground_truth(
            reconcile(args.data, tiers=these, **kwargs), gt
        )

    if tiers == ALL_TIERS:
        r1, r12, r123 = rung((1,)), rung((1, 2)), report
        print("\n  TIER LADDER")
        print("  " + "-" * 62)
        print(f"  {'metric':<24}{'tier 1':>9}{'+tier 2':>10}{'+tier 3':>10}{'delta t3':>9}")
        print("  " + "-" * 62)
        for name, a, b, c in (
            ("coverage", r1.coverage, r12.coverage, r123.coverage),
            ("precision", r1.precision, r12.precision, r123.precision),
            ("recall", r1.recall, r12.recall, r123.recall),
            ("exception accuracy", r1.exception_accuracy,
             r12.exception_accuracy, r123.exception_accuracy),
            ("exception recall", r1.exception_recall,
             r12.exception_recall, r123.exception_recall),
        ):
            print(f"  {name:<24}{_fmt(a):>9}{_fmt(b):>10}{_fmt(c):>10}{_delta(b, c):>9}")
        print("  " + "-" * 62)

        # What was actually asked for: Tier 3 measured against Tier 1 alone,
        # with Tier 2 out of the way, so the settlement arithmetic is credited
        # with what it recovers rather than with what Tier 2 left behind.
        r13 = rung((1, 3))
        print("\n  TIER 3 AGAINST TIER 1 ALONE (tier 2 disabled)")
        print("  " + "-" * 62)
        print(f"  {'metric':<24}{'tier 1':>9}{'+tier 3':>10}{'delta':>10}")
        print("  " + "-" * 62)
        for name, a, b in (
            ("coverage", r1.coverage, r13.coverage),
            ("precision", r1.precision, r13.precision),
            ("recall", r1.recall, r13.recall),
            ("exception accuracy", r1.exception_accuracy, r13.exception_accuracy),
        ):
            print(f"  {name:<24}{_fmt(a):>9}{_fmt(b):>10}{_delta(a, b):>10}")
        print("  " + "-" * 62)
        if r1.precision is not None and r13.precision is not None:
            drop = (r1.precision - r13.precision) * 100
            verdict = "TOO LOOSE" if drop > 0.5 else "within budget"
            print(f"  precision drop {drop:+.2f} points ({verdict})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
