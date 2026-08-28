#!/usr/bin/env python3
"""
A question-answering agent over one reconciliation run.

This is the one place in the project that makes a live model call, and it is a
deliberate exception to the zero-calls-at-request-time rule that governs
everything else. The explanation layer stays offline and cached; this does not.

The model is not handed a pile of CSVs and asked to do arithmetic -- that would
put a language model in the path of the only claim this project makes, which is
that no number it reports is wrong. Instead it is given TOOLS that query the
engine, and the engine answers. The model decides what to look up and how to
say it; every figure it repeats came out of a tier.

Two things make that trustworthy rather than merely stated:

  1. The tool results are the only source of fact in the conversation. There is
     no dataset in the prompt for the model to misread.
  2. After it answers, `verify_numbers` pulls every figure out of the reply and
     checks it against the numbers the tools actually returned. An unverified
     figure is reported as unverified, in the response, to the UI. The check
     cannot be silently skipped: it runs on every answer and its result is a
     required field.

The tool calls are returned too, because watching the agent query the engine is
most of the point -- an answer with its working shown is a different artifact
from an answer.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from finrecon.explain import load_dotenv
from finrecon.money import format_inr
from service import trace as trace_mod

MODEL = "mistral-small-latest"

# Tool ROUNDS, not loop iterations -- a grounding retry must not eat one.
# Six covers "list the queue, open the biggest, trace it, check its payout";
# four was short enough that a reasonable multi-part question ran out.
MAX_STEPS = 6

SYSTEM = """You are the analyst for a three-way reconciliation engine that ties \
merchant orders to gateway payments to bank credits.

You have tools that query one completed reconciliation run. Use them. Never \
answer a factual question about this dataset from memory or by guessing -- if \
you need a number, call a tool and read it.

Rules you must follow:
- Every figure in your answer must have come from a tool result in this \
conversation. Do not compute new totals, averages or differences yourself.
- Every id you name must have appeared in a SUCCESSFUL tool result. Never \
construct an id, never guess one that looks plausible, and never pick one \
because it follows a pattern. If a tool returns an error for an id, that id \
does not exist -- say so, do not try neighbouring ids.
- When a tool gives you a list of the orders in a finding, those are the ONLY \
orders in it. Do not trace or discuss any order outside that list.
- A tool result about one item tells you nothing about how many such items \
exist. Do not say "the only", "the largest" or "the sole" unless a tool \
returned a count or the full population.
- NEVER INVENT A REASON. The engine records exactly why it declined each \
match, and those reasons are a closed list that the tools return to you. \
Report the reason the engine gave. Do not supply a plausible banking \
explanation of your own, do not speculate about what "probably" happened, and \
do not offer a cause the tools did not state. An invented reason is worse than \
no answer, because the person reading it cannot tell the difference.
- If the tools do not support an answer, say plainly what you could not \
determine. "The engine did not record a cause for this one" and "I could not \
determine that from this run" are both correct, complete answers.
- Amounts are Indian rupees, already formatted. Repeat them exactly as the \
tool gave them, including the rupee sign and comma placement.
- Be concise and concrete. Two to five sentences. Name the ids you used.
- You are explaining a machine's decision to a human auditor. Prefer "the \
engine declined because..." over "it seems that...".

WHAT NO TOOL CAN ANSWER.

These have no tool behind them. Say so plainly and stop; do not reach for a
different tool and present its numbers as the answer.

- WHEN anything happened, or whether failures cluster by date, day or month. \
Nothing here aggregates by time.
- Whether one evidence band is more ACCURATE than another. The bands carry \
money and counts, never correctness rates. "STRONG holds less money than \
CIRCUMSTANTIAL" is not an answer to "is STRONG more accurate".
- Anything about the future: when a payout will arrive, what will happen next.
- Anything about people, customers, intent or fault.
- Arithmetic you would have to do yourself -- differences, ratios, "how much \
would be left if".

Returning real figures that do not bear on the question is a WRONG ANSWER, and
a worse one than "I could not determine that", because the numbers make it look
checked. Several tools now return a `not_evidence_for` list saying what their
own output cannot support. Read it and obey it.

WHAT COUNTS AS A USEFUL ANSWER.

The person asking is already looking at the app. For any finding they have \
selected, the screen ALREADY shows them: the headline, the settlement \
arithmetic line by line, the expected net, what the bank credited, the \
shortfall, the evidence band, the source records, and a written explanation of \
the finding. Repeating any of that back to them is worthless -- they can read \
it faster than you can say it.

Your value is the questions that screen CANNOT answer, because it shows one \
finding at a time and cannot add up, compare, or follow a chain:
- totals and shares across the whole queue (queue_breakdown)
- patterns across hundreds of declined entities (aggregate_declines)
- following one order across all four ledgers to the hop where it dies \
(trace_chain)
- what would have had to be true for a decline to have matched \
(what_would_change)
- whether two findings are the same underlying problem (compare_findings)

So: if the question is already answered by the panel on screen, do not just \
restate it. Answer it in one short clause and then add the thing that is NOT \
on screen -- how it compares, how common it is, what it would take, what else \
shares its cause. If a question is purely a restatement request and you have \
nothing to add, say so briefly rather than padding.
"""

# Fed back to the model when grounding fails. It names the offending tokens
# rather than scolding in general: a correction that does not say which figure
# was invented gets the same answer again with different wording.
RETRY = """Your answer failed the grounding check that runs on every reply.

{problems}

Rewrite it. Use only figures, ids and reasons that appear in the tool results \
above, calling another tool if you need one you do not have. If the evidence \
does not support the claim, drop the claim and say what could not be \
determined."""

# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "why_not",
            "description": (
                "Why the engine did not match one entity, tier by tier, with "
                "the reason each tier recorded. Works for a bank row, a "
                "settlement, an order or a payment id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "e.g. bank_000013, setl_20260722_019, ord_000412",
                    }
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_entity",
            "description": (
                "The raw record for one id: amount, date, references and "
                "narration, as the engine normalized it."
            ),
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_exceptions",
            "description": (
                "The investigable findings in the queue, largest first. Each "
                "has a code, a headline, the money at stake and how many "
                "orders it affects. Optionally filter by code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "MISSING_IN_BANK, AMOUNT_VARIANCE_UNEXPLAINED, "
                            "CHARGEBACK_UNPOSTED, DUPLICATE_PAYMENT, "
                            "ORDER_UNPAID, TIMING_PENDING"
                        ),
                    },
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "settlement_breakdown",
            "description": (
                "The settlement equation for one payout: gross, fee, tax, "
                "refunds, chargebacks, withholding, the expected net, what the "
                "bank actually credited, and the shortfall."
            ),
            "parameters": {
                "type": "object",
                "properties": {"settlement_id": {"type": "string"}},
                "required": ["settlement_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain_finding",
            "description": (
                "Everything about one queue finding by its group_id: the "
                "headline, the settlement arithmetic behind it, the evidence "
                "band, and the orders it affects. Use this after "
                "list_exceptions to answer 'what caused it'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {
                        "type": "string",
                        "description": "a group_id from list_exceptions, or "
                                       "the settlement id inside one "
                                       "(setl_...) -- either is accepted",
                    }
                },
                "required": ["group_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_breakdown",
            "description": (
                "The whole queue aggregated: money and count per exception "
                "code, per evidence band, and split between payout-side and "
                "order-side. Use for 'where is the money concentrated', 'how "
                "much did the engine refuse to attribute', 'what share is X'. "
                "The UI shows one finding at a time and cannot answer these."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aggregate_declines",
            "description": (
                "Count every entity the tiers declined, grouped by the reason "
                "they recorded, optionally for one kind (orders, settlements, "
                "bank, payments). Hundreds of entities. Use for 'what is the "
                "most common reason X fails', 'how many bank rows had no UTR'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string",
                             "description": "orders | settlements | bank | payments"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trace_chain",
            "description": (
                "Follow one order all the way through: order to gateway "
                "payment to payout to bank credit, naming the hop where the "
                "chain stops and why. Use for 'what happened to order X', "
                "'where does this chain break'."
            ),
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "what_would_change",
            "description": (
                "The counterfactual: for a declined entity, the specific "
                "condition that would have to hold for the engine to have "
                "matched it. Use for 'what would it take', 'why not just "
                "match it anyway', 'what is missing'."
            ),
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_findings",
            "description": (
                "Put two or more queue findings side by side: code, money, "
                "orders affected, evidence band, and whether they share a "
                "root cause. Use for 'are these the same problem', 'how does "
                "X differ from Y'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "group_ids": {
                        "type": "array", "items": {"type": "string"},
                        "description": "group_ids, or the settlement ids "
                                       "inside them -- either is accepted",
                    },
                },
                "required": ["group_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_summary",
            "description": (
                "Headline numbers for this run: orders matched, wrong matches, "
                "how many exception rows collapsed into how many findings, and "
                "the total money in the queue."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_entities",
            "description": (
                "Find entities the engine declined to match, by kind, "
                "optionally above an amount. Each row comes back WITH the "
                "reason the engine recorded, so this answers 'list them and "
                "why' in one call -- do not call why_not per row. The result "
                "says whether the list is complete; if it is not, do not "
                "describe it as every one of them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "orders | settlements | bank | payments",
                    },
                    "min_rupees": {"type": "number"},
                    "limit": {"type": "integer"},
                },
                "required": ["kind"],
            },
        },
    },
]


def _resolve_group(ref: str, ctx: dict[str, Any]) -> str | None:
    """Accept either a group_id or the settlement id inside it.

    Asked whether cb_00009 caused the shortfall on setl_20260708_005, the
    agent passed those raw ids to a tool that wanted group_ids, got errors,
    and concluded that neither existed -- when setl_20260708_005 IS a finding,
    under the group_id CHARGEBACK_UNPOSTED__setl_20260708_005. Refusing an id
    because it is spelled the other way is a false negative, and a false
    negative here reads as "there is no problem here".
    """
    queue = ctx["run"]["queue"]
    if any(g["group_id"] == ref for g in queue):
        return ref
    match = next((g for g in queue if g.get("settlement_id") == ref), None)
    return match["group_id"] if match else None


def _finding(group_id: str, ctx: dict[str, Any]) -> Any:
    """One queue finding, whole. The engine already assembles this for the
    detail pane; the agent gets the same object rather than a second view of
    it that could drift."""
    resolved = _resolve_group(group_id, ctx)
    if resolved is None:
        return {
            "error": f"{group_id} is not a finding in this run",
            "hint": "It may still be a real entity that simply reconciled "
                    "cleanly, or it may be an order/payment/bank id. Call "
                    "why_not or get_entity on it before concluding it does "
                    "not exist.",
        }
    group_id = resolved
    try:
        detail = ctx["group_detail"](ctx["seed"], group_id)
    except KeyError:
        return {"error": f"no finding with group_id {group_id}"}
    # Settlement-keyed findings come back with an EMPTY orders_sample: the
    # queue pane shows a count for them, not a list, so nothing ever needed to
    # build one. Handing the model a finding with "45 orders affected" and no
    # order ids is how it ends up inventing plausible ones -- it did, traced
    # ord_000412 through a completely unrelated payout, found it matched, and
    # reported that as the answer. Every id was real; the answer was about the
    # wrong rows. Derive the real list rather than leave the gap.
    orders = list(detail.get("orders_sample") or [])
    settlement_id = detail.get("settlement_id")
    if not orders and settlement_id:
        orders = [
            d["order_id"]
            for d in ctx["result"].get("decisions", [])
            if settlement_id in (d.get("settlement_ids") or [])
        ]

    return {
        "group_id": group_id,
        "code": detail.get("code"),
        "headline": detail.get("headline"),
        "amount": detail.get("rupees"),
        "orders_affected": detail.get("affected_chains"),
        "settlement_id": settlement_id,
        "arithmetic": detail.get("arithmetic"),
        "evidence": detail.get("evidence"),
        "suggested_action": detail.get("suggested_action"),
        "orders_in_this_finding": orders[:12],
        "orders_listed": min(len(orders), 12),
        "orders_total": len(orders),
        "note_on_orders": (
            "These are the ONLY orders in this finding. Do not trace any order "
            "that is not in this list."
            if orders else
            "No order list is available for this finding; do not guess ids."
        ),
        # The engine's own committed explanation of this finding. Offline,
        # verified, and the best-grounded sentence available about it.
        "engine_explanation": (detail.get("explanation") or {}).get("text"),
    }


def _queue_breakdown(run: dict[str, Any]) -> dict[str, Any]:
    """The queue, aggregated three ways.

    Nothing on screen can answer this. The queue is a sorted list of findings
    and a reader can see one at a time; "how much of the exposure is
    chargeback-related" or "how much did the engine refuse to attribute"
    requires summing across all of them.
    """
    queue = run["queue"]
    total = sum(g["rupees_paise"] for g in queue) or 1

    def bucket(key_fn):
        out: dict[str, dict[str, Any]] = {}
        for g in queue:
            key = str(key_fn(g))
            row = out.setdefault(key, {"findings": 0, "paise": 0, "orders": 0})
            row["findings"] += 1
            row["paise"] += g["rupees_paise"]
            row["orders"] += g["affected_chains"]
        for key, row in out.items():
            row["amount"] = format_inr(row["paise"])
            row["share_of_queue"] = f"{100 * row['paise'] / total:.1f}%"
            row.pop("paise")
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["findings"]))

    return {
        "total_in_queue": format_inr(total),
        "findings": len(queue),
        "by_code": bucket(lambda g: g["code"]),
        "by_evidence_band": bucket(lambda g: g.get("evidence_band") or "no cause searched"),
        "by_side": bucket(
            lambda g: "order-side" if g.get("kind") == "orders" else "payout-side"
        ),
        # The disclaimer travels with the data.
        #
        # This tool was the one reached for when no tool fit. Asked whether
        # failures cluster at month end, the agent returned these shares and
        # concluded they do not; asked whether STRONG attributions are more
        # accurate than CIRCUMSTANTIAL ones, it returned the same shares and
        # concluded they are not. Both answers were built from real figures
        # and were nonsense. A caveat in the tool output is read; one in the
        # system prompt competes with everything else in it.
        "not_evidence_for": [
            "TIMING. There is no date dimension here at all. This cannot say "
            "whether anything clusters at month end, or on any day.",
            "ACCURACY. by_evidence_band is how much MONEY sits in each band, "
            "not how often a band was right. It says nothing about whether "
            "STRONG attributions are more correct than CIRCUMSTANTIAL ones. "
            "No tool here measures that.",
        ],
    }


def _aggregate_declines(result: dict[str, Any], kind: str | None) -> dict[str, Any]:
    """Every declined entity, counted by the reason the tier recorded.

    The decline ledger runs to hundreds of entities per seed. It is the single
    largest body of fact the engine produces and there is no screen for it.
    """
    from collections import Counter

    per_tier: dict[str, Any] = {}
    for tier in ("tier1", "tier2", "tier3"):
        records = result.get("declines", {}).get(tier, {})
        rows = [r for r in records.values() if not kind or r["kind"] == kind]
        if not rows:
            continue
        counts = Counter(r["reason"] for r in rows)
        per_tier[tier] = {
            "entities_declined": len(rows),
            "by_reason": dict(counts.most_common()),
            "by_kind": dict(Counter(r["kind"] for r in rows).most_common()),
        }
    return {
        "kind": kind or "all",
        "tiers": per_tier,
        "complete": True,
        "note": "These counts cover EVERY declined entity, not a sample. Use "
                "them to answer 'how many' and 'most common' without listing "
                "rows one at a time.",
        "not_evidence_for": [
            "TIMING. Reasons are not broken down by date and this cannot "
            "support any claim about when failures happen.",
        ],
    }


def _trace_chain(seed_dir, result: dict[str, Any], order_id: str) -> dict[str, Any]:
    """Order to payment to payout to bank credit, and where it stops."""
    decision = next(
        (d for d in result.get("decisions", []) if d.get("order_id") == order_id), None
    )
    if decision is None:
        return {"error": f"no decision recorded for {order_id}"}

    by_id = trace_mod._index(seed_dir)

    def describe(entity_id):
        entry = by_id.get(entity_id)
        if entry is None:
            return {"id": entity_id}
        return {"id": entity_id, "amount": format_inr(entry.amount_paise),
                "date": entry.timestamp.date().isoformat()}

    hops = [{"ledger": "order", "entities": [describe(order_id)]}]
    for label, key in (("gateway payment", "payment_ids"),
                       ("payout", "settlement_ids"),
                       ("bank credit", "bank_row_ids")):
        ids = decision.get(key) or []
        hops.append({
            "ledger": label,
            "entities": [describe(i) for i in ids],
            "present": bool(ids),
        })

    broke_at = next((h["ledger"] for h in hops if not h.get("entities")), None)
    declines = result.get("declines", {})
    reason = None
    for tier in ("tier3", "tier2", "tier1"):
        record = declines.get(tier, {}).get(order_id)
        if record:
            reason = {"tier": int(tier[-1]), "reason": record["reason"],
                      "caused_by": record.get("caused_by")}
            break

    return {
        "order_id": order_id,
        "outcome": decision["outcome"],
        "hops": hops,
        "chain_breaks_at": broke_at,
        "reason_recorded": reason,
    }


# What each terminal reason would need in order to stop being a decline. The
# text is a CONDITION, not a guess about what happened -- "a credit within the
# band" is checkable; "the bank was probably late" is not.
_COUNTERFACTUAL = {
    "NO_BANK_CREDIT_WITH_UTR":
        "a bank credit whose narration contains the UTR {utr}",
    "NARRATION_HAS_NO_UTR":
        "a UTR anywhere in this row's narration",
    "UTR_NOT_IN_SETTLEMENT_REPORT":
        "a settlement in the gateway's report declaring the UTR {utr}",
    "AMOUNT_MISMATCH":
        "a credit carrying this UTR whose amount equals the net {net}",
    "NO_TOLERANT_CANDIDATE":
        "a bank credit within the tolerant band of {net}, inside the posting window",
    "OUTSIDE_POSTING_WINDOW":
        "the matching credit to fall inside the business-day posting window",
    "AMBIGUOUS_MULTI_CANDIDATE":
        "one fewer equally good candidate -- the tie has to be broken by "
        "evidence, and no evidence separates them",
    "BANK_ROW_CONTESTED":
        "the competing settlement to be resolved first, freeing the credit",
    "BANK_ROW_ALREADY_CLAIMED":
        "a second credit, since the one that fits is already settling another payout",
    "MISSING_IN_BANK":
        "a credit for {net} to appear in the statement at all",
    "AMOUNT_VARIANCE_UNEXPLAINED":
        "a refund, chargeback or payment in the gateway ledger accounting for "
        "the difference between the rebuilt payout and the credit",
    "NO_PAYMENT_FOR_ORDER":
        "a payment row referencing this order",
    "MULTIPLE_PAYMENTS_FOR_ORDER":
        "the ledger to say which of the several payments settled this order",
    "PAYMENT_HAS_CHARGEBACK":
        "the chargeback to be reversed or posted, so the chain closes normally",
    "ORDER_PAYMENT_AMOUNT_MISMATCH":
        "the payment's amount to equal the order's {amount}",
    "PAYMENT_HAS_NO_SETTLEMENT_ID":
        "the payment to name the payout it settled in",
    "SETTLEMENT_ID_NOT_FOUND":
        "the named settlement to appear in the settlement report",
}


def _counterfactual(seed_dir, result: dict[str, Any], entity_id: str) -> dict[str, Any]:
    """What would have to be true for this to have matched.

    Derived from the LAST reason a tier recorded, not invented: each reason
    corresponds to one unmet condition, and the condition is stated with the
    entity's own figures so it can be checked rather than believed.
    """
    detail = trace_mod.trace(seed_dir, result, entity_id)
    if not detail["steps"]:
        return {"entity_id": entity_id,
                "note": "No tier declined this entity; there is nothing it needed."}
    last = detail["steps"][-1]
    entry = trace_mod._index(seed_dir).get(entity_id)
    template = _COUNTERFACTUAL.get(last["reason"])
    condition = (
        template.format(**trace_mod._slots(entry))
        if template and entry is not None
        else f"the condition behind {last['reason']}, which has no stated form yet"
    )
    return {
        "entity_id": entity_id,
        "blocked_at_tier": last["tier"],
        "final_reason": last["reason"],
        "would_need": condition,
        "engine_position": (
            "The engine declines rather than matching on a weaker signal. A "
            "wrong match enters the books silently; a decline costs a person "
            "two minutes."
        ),
    }


def _run_tool(name: str, args: dict[str, Any], ctx: dict[str, Any]) -> Any:
    """Execute one tool against the engine. Never raises to the model."""
    seed_dir, result, run = ctx["seed_dir"], ctx["result"], ctx["run"]
    try:
        if name == "why_not":
            entity_id = args["entity_id"]
            # list_exceptions hands back group_ids, so the model reasonably
            # tries why_not(group_id) next. Routing it beats returning "no
            # such entity" and making it guess again -- two of the six tool
            # rounds went that way before this.
            group = next(
                (g for g in run["queue"] if g["group_id"] == entity_id), None
            )
            if group is not None:
                return _finding(entity_id, ctx)
            return trace_mod.trace(seed_dir, result, entity_id)

        if name == "explain_finding":
            return _finding(args["group_id"], ctx)

        if name == "get_entity":
            entry = trace_mod._index(seed_dir).get(args["entity_id"])
            if entry is None:
                return {"error": f"no entity with id {args['entity_id']}"}
            return {
                "entity_id": entry.entry_id,
                "source": entry.source,
                "type": entry.entry_type,
                "amount": format_inr(entry.amount_paise),
                "date": entry.timestamp.date().isoformat(),
                "references": {k: v for k, v in (entry.references or {}).items() if v},
                "narration": entry.text or None,
            }

        if name == "list_exceptions":
            rows = run["queue"]
            code = args.get("code")
            if code:
                rows = [g for g in rows if g["code"] == code]
            limit = int(args.get("limit") or 10)
            return [{
                "group_id": g["group_id"],
                "code": g["code"],
                "headline": g["headline"],
                "amount": g["rupees"],
                "orders_affected": g["affected_chains"],
                "evidence_band": g.get("evidence_band"),
            } for g in rows[:limit]]

        if name == "settlement_breakdown":
            sid = args["settlement_id"]
            match = next(
                (g for g in run["queue"] if g.get("settlement_id") == sid), None
            )
            if match is None:
                return {
                    "error": f"{sid} is not in the exception queue -- it either "
                             f"reconciled cleanly or is not a settlement id"
                }
            detail = ctx["group_detail"](ctx["seed"], match["group_id"])
            return {
                "settlement_id": sid,
                "arithmetic": detail.get("arithmetic"),
                "headline": detail.get("headline"),
                "evidence": detail.get("evidence"),
            }

        if name == "queue_breakdown":
            return _queue_breakdown(run)

        if name == "aggregate_declines":
            return _aggregate_declines(result, args.get("kind"))

        if name == "trace_chain":
            return _trace_chain(seed_dir, result, args["order_id"])

        if name == "what_would_change":
            return _counterfactual(seed_dir, result, args["entity_id"])

        if name == "compare_findings":
            ids = args.get("group_ids") or []
            findings = [_finding(g, ctx) for g in ids[:4]]
            # How many findings share each code, ACROSS THE WHOLE QUEUE.
            #
            # Without this the model asked about one finding, got one finding
            # back, and concluded it was "the only MISSING_IN_BANK in the run"
            # -- when there are two. A comparison tool that cannot say how big
            # the population is invites exactly that inference.
            from collections import Counter
            population = Counter(g["code"] for g in run["queue"])
            siblings = {}
            warnings = []
            for f in findings:
                code = f.get("code")
                if not code:
                    continue
                others = [
                    g["group_id"] for g in run["queue"]
                    if g["code"] == code and g["group_id"] != f["group_id"]
                ]
                siblings[code] = {
                    "total_with_this_code": population.get(code, 0),
                    "others_you_did_not_ask_about": others,
                }
                # Stated as a correction, not as a field to be noticed. A count
                # tucked into a side key was returned once and ignored, and the
                # answer claimed "the only MISSING_IN_BANK in the queue" when
                # there were two.
                if others:
                    warnings.append(
                        f"There are {population[code]} findings with code {code} "
                        f"in this run, not one. Do NOT call this the only "
                        f"{code}. The others are: {', '.join(others)}."
                    )
            return {
                "findings": findings,
                "siblings_in_queue": siblings,
                "important": warnings or [
                    "Each code you asked about has exactly one finding in this run."
                ],
            }

        if name == "run_summary":
            summary = dict(run["summary"])
            summary["seed"] = run["seed"]
            return summary

        if name == "search_entities":
            kind = args["kind"]
            ids = trace_mod.declinable_ids(result).get(kind, [])
            by_id = trace_mod._index(seed_dir)
            floor = float(args.get("min_rupees") or 0) * 100
            rows = []
            for entity_id in ids:
                entry = by_id.get(entity_id)
                if entry is None or abs(entry.amount_paise) < floor:
                    continue
                rows.append({
                    "entity_id": entity_id,
                    "amount": format_inr(entry.amount_paise),
                    "date": entry.timestamp.date().isoformat(),
                })
            # The reason travels with each row.
            #
            # Asked to list every unaccounted bank row WITH its reason, the
            # agent got ids here and then called why_not once per row --
            # sixteen tool rounds, sixteen of thirty-two rows returned, and
            # the result presented as "every bank row". One call now carries
            # what the question needs.
            declines = result.get("declines", {})
            def reason_for(entity_id):
                for tier in ("tier3", "tier2", "tier1"):
                    rec = declines.get(tier, {}).get(entity_id)
                    if rec:
                        return rec["reason"]
                return None
            for row in rows:
                row["reason"] = reason_for(row["entity_id"])

            rows.sort(key=lambda r: r["entity_id"])
            limit = int(args.get("limit") or 15)
            shown = rows[:limit]
            complete = len(shown) == len(rows)
            return {
                "kind": kind,
                "total_declined": len(rows),
                "showing_count": len(shown),
                "complete": complete,
                "note": (
                    f"This is the complete list of {len(rows)}."
                    if complete else
                    f"Showing {len(shown)} of {len(rows)}. You have NOT been "
                    f"given the rest -- do not describe this as every one of "
                    f"them. Raise `limit` or use aggregate_declines for the "
                    f"full distribution."
                ),
                "showing": shown,
            }

        return {"error": f"no such tool {name}"}
    except KeyError as exc:
        return {"error": f"not found: {exc}"}
    except Exception as exc:  # a tool failure is an answer, not a crash
        return {"error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------
# Number verification
# --------------------------------------------------------------------------
# Any run of digits, with or without separators and decimals. Deliberately
# greedy about what counts as a number: it is better to challenge a figure the
# model got right than to wave one through that it invented.
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _digits(token: str) -> str:
    return token.replace(",", "").rstrip("0").rstrip(".") if "." in token \
        else token.replace(",", "")


# Ids the engine issues. An id in an answer that no tool returned is an
# invention, and it is the most damaging kind because it looks like evidence.
_ID = re.compile(r"\b(?:ord|pay|setl|bank|rfnd|cb)_[A-Za-z0-9_]+\b")

# The engine's reason and outcome codes are a CLOSED vocabulary. If an answer
# names one that no tool returned, the model has reached for a mechanism the
# engine never reported.
_CODE = re.compile(r"\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+\b")

# Language that asserts or speculates about a cause. The first group is
# borrowed from the explanation layer, where moving cause out of the model's
# hands took causal speculation from 5-of-9 to 0-of-11. The second is the
# hedging that shows up when a model is filling a gap it should have declined.
_SPECULATION = (
    "probably", "presumably", "i assume", "i'd guess", "i would guess",
    "must have been", "it seems that", "appears to have been", "my guess",
    "typically this", "usually this", "in most cases this",
)

_CAUSAL_MARKERS = ("because", "due to", "caused by", "the reason", "as a result of")

# Claims about being the one and only. Checked against the counts the tools
# returned, because uniqueness is a statement about a population.
_EXCLUSIVITY = (
    "the only", "the sole", "no other", "none other", "is unique",
    "only one", "the single", "no others",
)


# --------------------------------------------------------------------------
# Claim-type checking: does the evidence support this KIND of claim?
# --------------------------------------------------------------------------
# What each tool can actually support. The load-bearing part of this table is
# what NOTHING declares -- temporal_distribution, accuracy_rates, prediction --
# because those are the claim types the agent has been caught making on
# evidence that cannot carry them.
#
# This is the capability boundary as an auditable table rather than a paragraph
# in a prompt. The `not_evidence_for` fields on individual tools stay: they
# steer the model before it answers, this catches it afterwards.
TOOL_CAPABILITIES: dict[str, frozenset[str]] = {
    "why_not": frozenset({"reasons", "entity_ids", "entity_dates"}),
    "get_entity": frozenset({"entity_ids", "entity_dates", "money_totals"}),
    "list_exceptions": frozenset({"counts", "money_totals", "entity_ids",
                                  "population_counts"}),
    "settlement_breakdown": frozenset({"settlement_arithmetic", "money_totals"}),
    "explain_finding": frozenset({"settlement_arithmetic", "money_totals",
                                  "entity_ids", "reasons"}),
    "queue_breakdown": frozenset({"money_totals", "counts", "population_counts"}),
    "aggregate_declines": frozenset({"counts", "reasons", "population_counts"}),
    "trace_chain": frozenset({"chain_hops", "entity_ids", "entity_dates"}),
    "what_would_change": frozenset({"counterfactual", "reasons"}),
    "compare_findings": frozenset({"money_totals", "counts", "entity_ids",
                                   "population_counts"}),
    "run_summary": frozenset({"money_totals", "counts"}),
    "search_entities": frozenset({"entity_ids", "counts", "reasons",
                                  "entity_dates", "population_counts"}),
}

# A sentence that declines is not a claim. Without this the check would flag
# "I could not determine whether failures cluster at month end" -- punishing
# the exact behaviour it exists to encourage, which would be worse than the
# bug being fixed.
DECLINE = (
    "could not determine", "cannot determine", "can't determine", "no tool",
    "not able to", "unable to", "does not measure", "cannot say", "no way to",
    "does not track", "not measured", "cannot answer", "not exposed",
    "does not record", "no data", "not something i can",
)

# Two signals, not one.
#
# A temporal-distribution claim needs BOTH a distribution word and a time word
# in the same sentence. One signal alone produces false positives that matter:
# "the money is concentrated in ORDER_UNPAID" is a supported claim about money,
# and "settled into a payout dated 2026-07-31" is a supported fact about a date.
# Neither is a claim about how failures are spread over time.
_DISTRIBUTION = ("cluster", "concentrat", "spread", "trend", "seasonal",
                 "distribut", "most of the", "majority of", "more likely on",
                 "peak")
_TIME_PERIOD = ("month end", "month-end", "end of the month", "over time",
                "weekend", "weekday", "day of the week", "time of day",
                "per month", "per week", "monthly", "weekly", "daily",
                "last week of", "early in the month", "late in the month",
                "start of the month", "each quarter", "by date")

_ACCURACY = ("more accurate", "less accurate", "as accurate", "most accurate",
             "more reliable", "less reliable", "better calibrated",
             "worse calibrated", "accuracy of the", "correctness rate",
             "hit rate of", "more trustworthy", "less trustworthy")

_FUTURE_ARRIVAL = re.compile(
    r"\bwill\b[^.]{0,40}\b(arrive|land|be credited|be paid|clear|come in|show up)\b"
)
_FORWARD = ("in the next ", "in the coming ", "forecast", "expected to arrive",
            "going to arrive", "predict")


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def _is_temporal_claim(low: str) -> bool:
    return (any(d in low for d in _DISTRIBUTION)
            and any(t in low for t in _TIME_PERIOD))


def _is_accuracy_claim(low: str) -> bool:
    return any(a in low for a in _ACCURACY)


def _is_prediction(low: str) -> bool:
    return bool(_FUTURE_ARRIVAL.search(low)) or any(f in low for f in _FORWARD)


# name, required capability, why -- the `why` is shown to the model on retry
# and to the reader in the UI, so it states the gap rather than scolding.
CLAIM_RULES = (
    ("a claim about timing", "temporal_distribution", _is_temporal_claim,
     "no tool called here groups anything by date, so nothing in this "
     "evidence can show how failures are spread over time"),
    ("a claim about accuracy", "accuracy_rates", _is_accuracy_claim,
     "the bands carry money and counts, never correctness rates -- no tool "
     "measures whether one band is more often right than another"),
    ("a claim about the future", "prediction", _is_prediction,
     "nothing here projects forward; the statement is historical and ends on "
     "a fixed date"),
)


def unsupported_claims(answer: str, tool_calls: list[dict[str, Any]]) -> list[str]:
    """Claim types the evidence in this transcript cannot carry.

    Generalises what verify_grounding already did for causal and exclusivity
    claims: detect the KIND of claim, then check the evidence actually
    retrieved can support that kind. The two failures that motivated it --
    "failures do not cluster at month end" and "STRONG is not more accurate" --
    contained no fabricated figure and so passed every token-level check.
    """
    available: set[str] = set()
    for call in tool_calls:
        available |= TOOL_CAPABILITIES.get(call.get("tool", ""), frozenset())

    problems: list[str] = []
    for sentence in _sentences(answer):
        low = sentence.lower()
        if any(d in low for d in DECLINE):
            continue
        for label, capability, detect, why in CLAIM_RULES:
            if detect(low) and capability not in available:
                problems.append(f"makes {label} that the evidence cannot "
                                f"support: {why}")
                break
    # Deduped: one sentence per kind is enough to send it back.
    return sorted(set(problems))


def verify_grounding(answer: str, tool_results: list[Any],
                     tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Is every claim in the answer traceable to something a tool returned?

    Numbers were the first version of this check and they are not enough: a
    model can invent a REASON without using a single digit, and an invented
    reason is the failure that makes the whole agent worthless. So four things
    are checked, all against the tool output and nothing else.

      numbers  reformatting is allowed, inventing is not
      ids      an id no tool returned is fabricated evidence
      codes    the engine's reason vocabulary is closed; anything outside it
               is a mechanism the engine never reported
      cause    a causal claim with no tool call behind it is ungrounded by
               construction, and speculative language is flagged outright

    This runs on every answer and its verdict is a required field of the
    response. It is not advisory: a failure sends the answer back to the model
    once, and if it fails again the UI is told the answer is ungrounded.
    """
    from finrecon.explain import BANNED_CAUSAL

    # ensure_ascii=False is load-bearing, not cosmetic.
    #
    # format_inr emits a rupee sign, and json.dumps escapes it to ₹. The
    # number regex then matched the "9" INSIDE that escape and glued it on, so
    # the tool's own "Rs 5,28,662.38" was indexed as 9528662.38 -- and every
    # correct rupee figure the model quoted was reported as invented. The
    # en-dash in headlines (–) did the same thing. The verifier was
    # rejecting true answers and pushing the model to strip real numbers out.
    # A FAILED tool call is not evidence.
    #
    # _run_tool echoes the id back in its error ("no decision recorded for
    # ord_000412"), so a fabricated id became "grounded" the moment the model
    # looked it up and was told it did not exist. Errors are excluded from the
    # vocabulary entirely: the only thing a failed lookup establishes is that
    # the lookup failed.
    succeeded = [
        r for r in tool_results
        if not (isinstance(r, dict) and "error" in r)
    ]
    haystack = json.dumps(succeeded, default=str, ensure_ascii=False)
    lowered = answer.lower()

    known_numbers = {_digits(m) for m in _NUM.findall(haystack)}
    known_ids = set(_ID.findall(haystack))
    known_codes = set(_CODE.findall(haystack))

    numbers_bad = []
    numbers_ok = []
    for token in _NUM.findall(answer):
        digits = _digits(token)
        # Only single digits are waved through, for "Tier 3" and "two of the
        # four". A two-digit count like "32 bank rows" is exactly the kind of
        # figure worth checking, and the earlier threshold of 2 let it pass
        # unexamined while reporting checked=0.
        if len(digits.replace(".", "")) <= 1:
            continue
        (numbers_ok if digits in known_numbers else numbers_bad).append(token)

    ids_bad = sorted({i for i in _ID.findall(answer) if i not in known_ids})
    codes_bad = sorted({
        c for c in _CODE.findall(answer)
        if c not in known_codes and c not in {"UTR", "IST", "CSV"}
    })

    # The engine records its OWN causal links, in the trace's `caused_by`
    # field: a settlement that failed strands every order in its batch, and
    # saying so is reporting, not speculating. So the cause-asserting phrases
    # are only banned when no tool returned such a link -- otherwise the
    # verifier punishes the model for quoting the engine accurately, which is
    # exactly what it is being asked to do.
    declared_links = bool(re.search(r'"caused_by":\s*"[^"]', haystack))
    banned = [p for p in BANNED_CAUSAL
              if not (declared_links and p in ("caused by", "the cause is",
                                               "the cause was"))]

    speculative = [p for p in _SPECULATION if p in lowered]
    speculative += [p for p in banned if p in lowered]

    # A causal claim with nothing looked up cannot be grounded in anything.
    claims_cause = any(m in lowered for m in _CAUSAL_MARKERS)
    unsupported_cause = claims_cause and not tool_calls

    # Exclusivity claims, checked against the population the tools reported.
    #
    # "This is the only MISSING_IN_BANK finding" survived every other check --
    # it contains no figure, no id and no reason code -- while the same tool
    # result said there were two. Uniqueness is a claim about a COUNT, so it
    # is checked against the count.
    contradicted_exclusivity = []
    if any(p in lowered for p in _EXCLUSIVITY):
        for match in re.finditer(r'"total_with_this_code":\s*(\d+)', haystack):
            if int(match.group(1)) > 1:
                contradicted_exclusivity.append(
                    f"claims uniqueness while a tool reported "
                    f"{match.group(1)} of them"
                )
                break

    problems = []
    if numbers_bad:
        problems.append(f"figures not in any tool result: {', '.join(numbers_bad)}")
    if ids_bad:
        problems.append(f"ids no tool returned: {', '.join(ids_bad)}")
    if codes_bad:
        problems.append(f"reason codes the engine never reported: {', '.join(codes_bad)}")
    if speculative:
        problems.append(f"speculative or cause-asserting language: {', '.join(sorted(set(speculative)))}")
    if unsupported_cause:
        problems.append("states a cause without having called a single tool")
    problems.extend(contradicted_exclusivity)
    # The reasoning check. Everything above validates TOKENS -- that each
    # figure, id and code came from the evidence. This validates the KIND of
    # claim against the kind of evidence retrieved, which is the hole the two
    # month-end / band-accuracy answers went through.
    claim_problems = unsupported_claims(answer, tool_calls)
    problems.extend(claim_problems)

    return {
        "ok": not problems,
        "problems": problems,
        "numbers": {"verified": numbers_ok, "unverified": numbers_bad},
        "ids": {"unverified": ids_bad},
        "codes": {"unverified": codes_bad},
        "speculative": sorted(set(speculative)),
        # Kept separate so the UI can say "unsupported claim" rather than
        # "ungrounded figure" -- they are different failures and a reader
        # should be able to tell which one fired.
        "unsupported_claims": claim_problems,
        "checked": len(numbers_ok) + len(numbers_bad) + len(_ID.findall(answer)),
    }


# The older name, kept so nothing that imported it breaks.
def verify_numbers(answer: str, tool_results: list[Any]) -> dict[str, Any]:
    return verify_grounding(answer, tool_results, [{"tool": "legacy"}])


# --------------------------------------------------------------------------
ADJUDICATOR = """You are an evidence adjudicator. You will be shown a QUESTION, \
the EVIDENCE a reconciliation engine returned, and an ANSWER written from it.

Decide one thing only: does the ANSWER follow from the EVIDENCE?

Mark it UNSUPPORTED if the answer asserts anything the evidence cannot \
establish -- most importantly a claim of a KIND the evidence cannot carry. \
Evidence that reports money and counts cannot establish WHEN things happened, \
whether one category is more ACCURATE than another, or what will happen NEXT, \
no matter how real its numbers are.

Mark it SUPPORTED if every assertion traces to the evidence, or if the answer \
declines to answer. Declining is always SUPPORTED.

Reply with exactly one line, nothing else:
SUPPORTED
or
UNSUPPORTED: <the specific claim that is not supported>"""


def adjudicate(question: str, tool_results: list[Any], answer: str) -> dict[str, Any]:
    """A second model, asked only whether the answer follows from the evidence.

    NOT IN THE REQUEST PATH, on purpose. `ask()` never calls this.

    It was built as an experiment and measured by eval/adjudicator_experiment.py
    against 20 unsupported probes and 6 supported controls. The result:

        declined by the agent itself      18 / 20
        caught by the deterministic table  2 / 20
        caught by the adjudicator ONLY     0 / 20
        false rejections                   0 / 6
        latency it would have added        1.67s mean

    Zero incremental catches. Wiring it in would buy no coverage in exchange
    for a second model call, 1.7s on every answer and a second failure mode,
    so it stays out. Kept here so the experiment remains runnable and the null
    result stays reproducible rather than becoming a claim in a commit message.

    Strictly a REJECTOR. It never writes an answer, so the worst a wrong
    verdict can do is let something through that the deterministic table
    already let through, or raise a flag a human can read and dismiss.

    FAILS OPEN. Any error, timeout or unparseable reply returns
    assessed=False, and the caller proceeds on the table's verdict alone. An
    adjudicator that can block an answer by being unreachable would be a
    reliability regression traded for a correctness gain.
    """
    client = _client()
    if client is None:
        return {"assessed": False, "why": "no api key"}
    evidence = json.dumps(tool_results, default=str, ensure_ascii=False)[:6000]
    try:
        response = client.chat.complete(
            model=MODEL,
            messages=[
                {"role": "system", "content": ADJUDICATOR},
                {"role": "user",
                 "content": f"QUESTION:\n{question}\n\nEVIDENCE:\n{evidence}"
                            f"\n\nANSWER:\n{answer}"},
            ],
            temperature=0.0,
            max_tokens=120,
        )
        verdict = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        return {"assessed": False, "why": f"{type(exc).__name__}: {exc}"}

    upper = verdict.upper()
    if upper.startswith("UNSUPPORTED"):
        return {"assessed": True, "supported": False,
                "why": verdict.split(":", 1)[-1].strip() or verdict}
    if upper.startswith("SUPPORTED"):
        return {"assessed": True, "supported": True, "why": ""}
    return {"assessed": False, "why": f"unparseable verdict: {verdict[:80]}"}


def _client():
    load_dotenv()
    key = os.environ.get("MISTRAL_API_KEY", "").strip()
    if not key:
        return None
    from mistralai import Mistral

    return Mistral(api_key=key)


def _subject_brief(subject: str | None, ctx: dict[str, Any]) -> str:
    """What the user is looking at, told to the model as context.

    Without this, referring to a finding means typing setl_20260722_019 by
    hand, and "this one" is the way a person actually refers to the row in
    front of them. The brief also states what is ALREADY on their screen,
    which is what stops the answer being a restatement of it.
    """
    if not subject:
        return ""
    try:
        finding = _finding(subject, ctx)
        if "error" not in finding:
            return (
                f"\n\nThe user is looking at finding {subject} on screen: "
                f"{finding.get('headline')}, {finding.get('amount')}, "
                f"{finding.get('orders_affected')} orders, code "
                f"{finding.get('code')}. They can ALREADY see its full "
                f"arithmetic, evidence and written explanation. Do not repeat "
                f"those. When they say \"this\" or \"it\", they mean this "
                f"finding."
            )
    except Exception:
        pass
    return (
        f"\n\nThe user is looking at {subject} on screen. When they say "
        f"\"this\" or \"it\", they mean {subject}."
    )


def ask(seed: str, question: str, ctx: dict[str, Any],
        subject: str | None = None) -> dict[str, Any]:
    """Answer one question about one run, showing the agent's working."""
    client = _client()
    if client is None:
        return {
            "ok": False,
            "error": "no_api_key",
            "message": (
                "This feature makes a live model call and needs MISTRAL_API_KEY "
                "set, or a .env file containing it. Everything else in the app "
                "runs without one."
            ),
        }

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user",
         "content": f"Dataset: {seed}{_subject_brief(subject, ctx)}"
                    f"\n\nQuestion: {question}"},
    ]
    calls: list[dict[str, Any]] = []
    results: list[Any] = []
    retried = False
    rejected: dict[str, Any] | None = None
    rounds = 0

    while rounds <= MAX_STEPS:
        response = client.chat.complete(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.2,
            max_tokens=700,
        )
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []

        if not tool_calls:
            answer = (message.content or "").strip()
            grounding = verify_grounding(answer, results, calls)

            # One correction, with the offending tokens named. Not a loop:
            # a model that cannot ground its answer on the second attempt is
            # not going to on the fifth, and the honest move is to hand the
            # UI a failed check rather than spend a judge's patience hiding it.
            if not grounding["ok"] and not retried:
                retried = True
                messages.append({"role": "assistant", "content": answer})
                messages.append({
                    "role": "user",
                    "content": RETRY.format(
                        problems="\n".join(f"- {p}" for p in grounding["problems"])
                    ),
                })
                rejected = {"answer": answer, "problems": grounding["problems"]}
                continue

            return {
                "ok": True,
                "answer": answer,
                "tool_calls": calls,
                "steps": len(calls),
                "model": MODEL,
                "grounding": grounding,
                # Shown in the UI when it happened. A guardrail nobody can see
                # fire is indistinguishable from one that is not there.
                "corrected": rejected,
            }

        messages.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.function.name,
                                 "arguments": c.function.arguments},
                }
                for c in tool_calls
            ],
        })

        rounds += 1
        for call in tool_calls:
            name = call.function.name
            raw = call.function.arguments
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            except json.JSONDecodeError:
                args = {}
            output = _run_tool(name, args, ctx)
            calls.append({"tool": name, "arguments": args,
                          "ok": not (isinstance(output, dict) and "error" in output)})
            results.append(output)
            messages.append({
                "role": "tool",
                "name": name,
                "tool_call_id": call.id,
                # ensure_ascii=False so the model reads a rupee sign rather
                # than an escape, for the same reason the verifier does.
                "content": json.dumps(output, default=str, ensure_ascii=False)[:6000],
            })

    # Out of tool rounds. Take the tools away and ask for an answer from what
    # it already has, rather than returning an error.
    #
    # "The agent ran out of steps, try a narrower question" is a failure the
    # user has to work around, and it is usually wrong: by this point four
    # tools have returned real facts and the answer is sitting in the
    # conversation. Only a genuine refusal should reach the screen.
    messages.append({
        "role": "user",
        "content": "Answer now, from the tool results above. Do not call any "
                   "more tools. If they do not support an answer, say exactly "
                   "what you could not determine.",
    })
    final = client.chat.complete(
        model=MODEL, messages=messages, temperature=0.2, max_tokens=700,
    )
    answer = (final.choices[0].message.content or "").strip()
    return {
        "ok": True,
        "answer": answer,
        "tool_calls": calls,
        "steps": len(calls),
        "model": MODEL,
        "grounding": verify_grounding(answer, results, calls),
        "corrected": rejected,
        "note": "Answered from the tools already called, after using the "
                "full tool budget.",
    }


# Deliberately none of these is answerable by reading the queue. "Explain the
# shortfall on X" was in this list and it was the worst question on it: the
# detail pane answers it completely, so the agent could only restate it.
SUGGESTED = [
    "Where is the money concentrated — which cause accounts for most of the queue?",
    "What is the single most common reason the engine could not match a bank row?",
    "How much of the queue did the engine refuse to attribute a cause to, and why?",
    "Are the chargeback findings all the same underlying problem?",
    "If I only had an hour, which findings should I work first and why?",
]

# Shown under the box when a finding is selected, because the useful questions
# about a specific finding are different from the useful questions about a run.
SUGGESTED_FOR_SUBJECT = [
    "What would it have taken for this to match?",
    "How does this compare to the others like it?",
    "Is this a one-off or a pattern across the run?",
    "Where exactly does the chain break for these orders?",
]
