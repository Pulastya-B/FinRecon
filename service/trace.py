#!/usr/bin/env python3
"""
Why the engine did not match one particular thing.

Every tier already records a reason for every entity it declines -- that is how
Tier 5 types an exception at all -- and until now those reasons were merged
into one map, handed to Tier 5, and dropped. This module hands them back.

The merged form is right for Tier 5, which needs one code per chain, and wrong
for a person. "Tier 1 found no UTR in the narration, then Tier 2 found the
amount twelve rupees outside its band, then Tier 3 reproduced the payout from
the settlement equation and found no credit against it" is a different and far
more useful statement than the last code alone. On seed 42, 251 entities carry
a different reason in a later tier than they did in Tier 1.

Nothing here computes a verdict. It reads what the tiers decided and renders
it; if this module and the engine ever disagree, the engine is right and this
is broken.
"""

from __future__ import annotations

from typing import Any

from finrecon.money import format_inr
from finrecon.normalize import load

# What each tier was trying to do, so a decline reads as "this was attempted
# and here is why it failed" rather than as a bare error code.
TIER_TITLE = {
    "tier1": "Exact reference match",
    "tier2": "Tolerant match",
    "tier3": "Settlement arithmetic",
}

TIER_GOAL = {
    "tier1": "join on a reference the two sides both state",
    "tier2": "join on relaxed evidence, refusing anything ambiguous",
    "tier3": "rebuild the payout from the settlement equation and find its credit",
}

# One sentence per reason code, in plain language, written to be read by
# someone who has never seen this codebase.
#
# `{}` slots are filled from the entity's own record. A reason with no entry
# here renders its code and says so rather than rendering a blank -- see
# `_sentence`, and the probe that asserts every reason the engine can emit is
# covered.
REASON_TEXT: dict[str, str] = {
    # -- bank rows ---------------------------------------------------------
    "NARRATION_HAS_NO_UTR":
        "The narration on this credit carries no UTR: {narration}. There is "
        "nothing in the text to join on.",
    "UTR_NOT_IN_SETTLEMENT_REPORT":
        "A UTR was read out of the narration ({utr}), but no settlement in the "
        "report declares it. The payout this credit belongs to is not in the "
        "gateway's file.",
    "SETTLEMENT_NOT_MATCHED_TO_BANK":
        "This credit names a settlement the engine could not tie in the other "
        "direction, so claiming it here would assert a link the settlement "
        "side does not support.",
    "BANK_ROW_ALREADY_CLAIMED":
        "Another settlement had already claimed this bank row. One credit "
        "cannot settle two payouts, and taking it twice would double-count.",
    # -- settlements -------------------------------------------------------
    "NO_BANK_CREDIT_WITH_UTR":
        "No bank credit anywhere in the statement carries this payout's UTR "
        "({utr}). Either the money has not arrived or the narration lost the "
        "reference.",
    "AMOUNT_MISMATCH":
        "A credit carrying this UTR exists, but the amount does not agree with "
        "the payout's net of {net}. Tier 1 will not match across a difference "
        "it cannot account for.",
    "OUTSIDE_POSTING_WINDOW":
        "A credit matching this payout exists, but it falls outside the "
        "business-day posting window. Matching across it would assert a "
        "settlement timeline the calendar does not support.",
    "AMBIGUOUS_MULTI_CANDIDATE":
        "More than one candidate fits this payout equally well. Either could "
        "be right, so picking one is a coin flip that lands in the books.",
    "NO_TOLERANT_CANDIDATE":
        "Even with the band relaxed and the window widened, no bank credit "
        "fits this payout's net of {net}.",
    "BANK_ROW_CONTESTED":
        "The credit that fits this payout also fits another one. Two "
        "settlements want the same row, so neither gets it.",
    "AMOUNT_VARIANCE_UNEXPLAINED":
        "The payout was rebuilt from the settlement equation and a credit was "
        "found, but the two differ and no refund, chargeback or payment in the "
        "gateway ledger accounts for the difference.",
    "MISSING_IN_BANK":
        "The payout reproduces exactly from the settlement equation, and no "
        "credit for it appears in the statement at all.",
    "TIMING_PENDING":
        "The payout is correct and its credit has not arrived yet. The posting "
        "window is still open, so this is not yet a discrepancy.",
    # -- orders ------------------------------------------------------------
    "NO_PAYMENT_FOR_ORDER":
        "This order has no payment against it anywhere in the gateway ledger. "
        "There is no chain to reconcile.",
    "MULTIPLE_PAYMENTS_FOR_ORDER":
        "More than one payment is recorded against this order. Which one "
        "settled it is a question the ledger does not answer.",
    "PAYMENT_HAS_CHARGEBACK":
        "The payment for this order carries a chargeback, so the money did not "
        "stay with the merchant and the chain does not close normally.",
    "PAYMENT_HAS_NO_SETTLEMENT_ID":
        "The payment for this order names no settlement, so there is no payout "
        "to follow it into.",
    "SETTLEMENT_ID_NOT_FOUND":
        "The payment names a settlement that does not appear in the settlement "
        "report.",
    "ORDER_PAYMENT_AMOUNT_MISMATCH":
        "The payment's amount does not agree with the order's {amount}.",
    "ORDER_CANCELLED":
        "The order was cancelled, so no payment against it is expected.",
    "UNKNOWN":
        "The tier declined without recording a more specific reason.",
}

KIND_LABEL = {
    "orders": "order",
    "payments": "payment",
    "settlements": "settlement",
    "bank": "bank row",
}


def _index(seed_dir) -> dict[str, Any]:
    """Every entity by id. `load` is cached, so this is a dict build."""
    ledgers = load(seed_dir)
    by_id: dict[str, Any] = {}
    for stream in ("orders", "payments", "refunds", "chargebacks",
                   "settlements", "bank"):
        for entry in getattr(ledgers, stream, ()) or ():
            by_id[entry.entry_id] = entry
    return by_id


def _facts(entry) -> list[dict[str, str]]:
    """The entity's own record, as label/value pairs.

    Read off the normalized entry rather than re-read from the CSV: the tiers
    decided against THIS view of the row, so a trace that quotes a different
    one can disagree with the verdict it is explaining.
    """
    out = [
        {"label": "id", "value": entry.entry_id},
        {"label": "amount", "value": format_inr(entry.amount_paise)},
        {"label": "date", "value": entry.timestamp.date().isoformat()},
    ]
    for key in ("net_paise", "gross_paise", "fee_paise", "tax_paise"):
        if key in entry.amounts and key != "amount_paise":
            out.append({
                "label": key.replace("_paise", ""),
                "value": format_inr(entry.amounts[key]),
            })
    for key, value in (entry.references or {}).items():
        if value:
            out.append({"label": key, "value": str(value)})
    if entry.text:
        out.append({"label": "narration", "value": entry.text})
    return out


def _slots(entry) -> dict[str, str]:
    """Values the reason sentences can interpolate.

    Every slot has a non-empty fallback. A sentence that renders "the narration
    carries no UTR: " with nothing after the colon is worse than no sentence,
    and this is the same failure the explanation templates were fixed for.
    """
    refs = entry.references or {}
    narration = (entry.text or "").strip()
    return {
        "narration": f'"{narration}"' if narration else "the row carries no narration",
        "utr": str(refs.get("utr") or "none recorded"),
        "net": format_inr(entry.amounts.get("net_paise", entry.amount_paise)),
        "amount": format_inr(entry.amount_paise),
    }


def _sentence(reason: str, entry) -> str:
    template = REASON_TEXT.get(reason)
    if template is None:
        # Loud, not blank. An uncovered reason is a gap in this file, and
        # saying so is better than rendering an empty explanation.
        return (
            f"The tier declined with reason {reason}, which has no written "
            f"explanation yet."
        )
    return template.format(**_slots(entry))


def trace(seed_dir, result: dict[str, Any], entity_id: str) -> dict[str, Any]:
    """The tier-by-tier story of one entity."""
    by_id = _index(seed_dir)
    entry = by_id.get(entity_id)
    if entry is None:
        raise KeyError(entity_id)

    declines = result.get("declines", {})
    steps: list[dict[str, Any]] = []
    for tier in ("tier1", "tier2", "tier3"):
        record = declines.get(tier, {}).get(entity_id)
        if record is None:
            continue
        steps.append({
            "tier": int(tier[-1]),
            "title": TIER_TITLE[tier],
            "goal": TIER_GOAL[tier],
            "verdict": "declined",
            "reason": record["reason"],
            "sentence": _sentence(record["reason"], entry),
            "caused_by": record.get("caused_by"),
        })

    # Did anything decide it in the end?
    outcome: dict[str, Any] = {"state": "undecided"}
    for decision in result.get("decisions", []):
        if decision.get("order_id") != entity_id:
            continue
        notes = decision.get("notes") or {}
        outcome = {
            "state": "matched" if decision["outcome"] == "MATCHED" else "exception",
            "code": decision["outcome"],
            "tier": notes.get("tier"),
        }
        break
    if outcome["state"] == "undecided":
        disposition = (result.get("bank_dispositions") or {}).get(entity_id)
        if disposition == "MATCHED":
            outcome = {"state": "matched", "code": "MATCHED", "tier": None}
        elif disposition == "IGNORED":
            outcome = {
                "state": "ignored",
                "code": "IGNORED",
                "tier": None,
                "note": "A debit is money leaving the account. It cannot be an "
                        "inbound payout, so no tier tries to match it.",
            }
    # Settlements and bank rows are not keyed in `decisions`, which is one
    # record per ORDER. Without this a payout that is sitting in the queue
    # under a named exception code reported "undecided", which is both wrong
    # and the least useful thing the trace could say about it.
    if outcome["state"] == "undecided" and entry.source == "settlements":
        for decision in result.get("decisions", []):
            if entity_id in (decision.get("settlement_ids") or []):
                if decision["outcome"] != "MATCHED":
                    outcome = {
                        "state": "exception",
                        "code": decision["outcome"],
                        "tier": (decision.get("notes") or {}).get("tier"),
                        "note": "This payout is in the queue under this code.",
                    }
                    break
                outcome = {"state": "matched", "code": "MATCHED",
                           "tier": (decision.get("notes") or {}).get("tier")}
                break

    kind = KIND_LABEL.get(entry.source, entry.source)
    return {
        "entity_id": entity_id,
        "kind": kind,
        "source": entry.source,
        "headline": f"{kind.title()} {entity_id} · {format_inr(entry.amount_paise)}"
                    f" · {entry.timestamp.date().isoformat()}",
        "facts": _facts(entry),
        "outcome": outcome,
        "steps": steps,
        "tiers_run": result.get("tier_stats", {}).get("tiers_run", []),
    }


def declinable_ids(result: dict[str, Any]) -> dict[str, list[str]]:
    """Every entity that any tier declined, grouped by kind.

    This is what makes the feature discoverable: without a list, asking "why
    not" requires already knowing an id to ask about.
    """
    out: dict[str, list[str]] = {}
    seen: set[str] = set()
    for tier in ("tier1", "tier2", "tier3"):
        for entity_id, record in (result.get("declines", {}).get(tier, {})).items():
            if entity_id in seen:
                continue
            seen.add(entity_id)
            out.setdefault(record["kind"], []).append(entity_id)
    for ids in out.values():
        ids.sort()
    return out
