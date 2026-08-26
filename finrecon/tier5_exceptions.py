#!/usr/bin/env python3
"""
Tier 5 -- typing the exceptions, and grouping them into work.

Every tier below this one either matches a chain or DECLINES it, with a reason.
Declining is the right call and the reason is always recorded -- but until now
nothing turned "I declined this because the order has no payment" into
"ORDER_UNPAID". The engine knew exactly what was wrong with 94 chains on seed
42 and said nothing about any of them, which is why exception accuracy averaged
76.90% across thirty seeds and swung 56 points.

So this tier adds no analysis at all. It reads the reason the gate already
recorded and names it:

    no payment for this order        -> ORDER_UNPAID
    multiple payments for one order  -> DUPLICATE_PAYMENT
    order/payment amounts disagree   -> AMOUNT_VARIANCE_UNEXPLAINED
    payment has an unposted dispute  -> CHARGEBACK_UNPOSTED
    anything else                    -> UNKNOWN

Reading the reason rather than recomputing it
---------------------------------------------
The gates own the reason. This tier does not re-run `order_side_gates` to
work out why a chain failed, because that is precisely how the three "each tier
correct, pipeline wrong" bugs happened -- two places deciding the same thing
and drifting apart. If a reason ever fails to reach here, the fix is to
propagate it further, never to derive it again.

UNKNOWN is load-bearing
-----------------------
A decline whose reason has no mapping gets UNKNOWN, not the nearest plausible
code. If UNKNOWN never fires the mapping is complete; if it fires often the
mapping has a hole, and either way the number is worth having. Guessing here
would hide exactly the thing this tier exists to surface.

Grouping is by root cause, and sorted by money
----------------------------------------------
229 exception rows on seed 42 are not 229 things to investigate. One payout
that never arrived strands every order in its batch, and an operator works the
payout, not the forty order lines behind it. Groups are sorted by rupees at
stake descending, because that is the order a finance team actually works in --
not by identifier, which is an order nobody chose.

Run:
    python -m finrecon.tier5_exceptions --data data/seed42
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .money import format_inr
from .normalize import NormalizedLedgers, load
from .tier1_exact import (
    Decline,
    MULTIPLE_PAYMENTS_FOR_ORDER,
    NO_PAYMENT_FOR_ORDER,
    ORDER_PAYMENT_AMOUNT_MISMATCH,
    PAYMENT_HAS_CHARGEBACK,
)

CODE_UNKNOWN = "UNKNOWN"

# The gate's reason IS the diagnosis. This table only renames it; nothing here
# inspects a ledger or recomputes a condition.
REASON_TO_CODE: dict[str, str] = {
    NO_PAYMENT_FOR_ORDER: "ORDER_UNPAID",
    MULTIPLE_PAYMENTS_FOR_ORDER: "DUPLICATE_PAYMENT",
    ORDER_PAYMENT_AMOUNT_MISMATCH: "AMOUNT_VARIANCE_UNEXPLAINED",
    PAYMENT_HAS_CHARGEBACK: "CHARGEBACK_UNPOSTED",
}

# What the group is, in words an operator would use rather than a rule name.
ROOT_CAUSE: dict[str, str] = {
    "ORDER_UNPAID": "orders with no payment anywhere",
    "DUPLICATE_PAYMENT": "orders captured more than once",
    "AMOUNT_VARIANCE_UNEXPLAINED": "orders whose value disagrees with the capture",
    "CHARGEBACK_UNPOSTED": "disputes debited but not recorded in the books",
    "MISSING_IN_BANK": "payouts that never reached the bank",
    "TIMING_PENDING": "payouts still inside their clearing window",
    "AMBIGUOUS_MULTI_CANDIDATE": "payouts that cannot be told apart",
    CODE_UNKNOWN: "declines the engine cannot classify",
}

# Phrased as something a person does next, not as a restatement of the problem.
SUGGESTED_ACTION: dict[str, str] = {
    "ORDER_UNPAID": (
        "Check the checkout log for these orders. If payment was never "
        "attempted, cancel them; a cluster here usually means checkout is "
        "dropping customers at one step."
    ),
    "DUPLICATE_PAYMENT": (
        "Decide which capture is the real sale, refund the other, and confirm "
        "the settlement batch still balances afterwards."
    ),
    "AMOUNT_VARIANCE_UNEXPLAINED": (
        "Compare each order's value against the amount actually captured and "
        "correct whichever side is wrong. These are usually manual keying "
        "errors in the order system, not gateway faults."
    ),
    "CHARGEBACK_UNPOSTED": (
        "Record these disputes in the books and confirm each gateway debit "
        "against the payout it was taken from."
    ),
    "MISSING_IN_BANK": (
        "Confirm with the gateway whether the payout was released, and check "
        "the statement beyond the expected clearing window."
    ),
    "TIMING_PENDING": (
        "No action yet -- these are inside their clearing window. Re-check "
        "after the window closes."
    ),
    "AMBIGUOUS_MULTI_CANDIDATE": (
        "Ask the gateway which payout each credit belongs to; the amounts "
        "alone cannot separate them."
    ),
    CODE_UNKNOWN: (
        "Route to a human. The engine declined these for a reason it has no "
        "classification for, which is a gap in the engine as much as in the "
        "data."
    ),
}


@dataclass(frozen=True)
class TypedException:
    """One chain, named. No judgement beyond reading the gate's reason."""

    order_id: str
    code: str
    reason: str
    entities: tuple[str, ...]
    amount_paise: int
    caused_by: str | None = None

    @property
    def group_key(self) -> str:
        """What to file this under.

        The upstream cause when there is one -- forty orders stranded by one
        payout are one investigation -- and otherwise the code, so forty
        unrelated unpaid orders still read as a single pattern rather than
        forty separate tickets.
        """
        return self.caused_by or self.code


@dataclass
class ExceptionGroup:
    group_key: str
    code: str
    root_cause: str
    reasons: tuple[str, ...]
    chain_count: int
    total_paise: int
    suggested_action: str
    order_ids: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()

    @property
    def rupees(self) -> str:
        return format_inr(self.total_paise)


@dataclass
class Tier5Result:
    exceptions: list[TypedException] = field(default_factory=list)
    groups: list[ExceptionGroup] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def counts_by_code(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for item in self.exceptions:
            counts[item.code] += 1
        return dict(counts)


def merge_declines(*decline_maps: Mapping[str, Decline]) -> dict[str, Decline]:
    """One decline per entity, later tiers winning.

    All three tiers call the same `order_side_gates`, so they agree on the
    order-side reasons; where a later tier saw more -- a settlement that failed
    to tie, with its `caused_by` link -- its record is the better one.
    """
    merged: dict[str, Decline] = {}
    for decline_map in decline_maps:
        merged.update(decline_map)
    return merged


def run(
    ledgers: NormalizedLedgers,
    undecided_order_ids: Sequence[str],
    declines: Mapping[str, Decline],
) -> Tier5Result:
    """Type every chain nothing else decided, and group the result into work."""
    result = Tier5Result()

    orders = {o.entry_id: o for o in ledgers.orders}
    payments_by_order: dict[str, list[str]] = defaultdict(list)
    settlement_of: dict[str, str] = {}
    for payment in ledgers.payments:
        order_id = payment.references["order_id"]
        if order_id:
            payments_by_order[order_id].append(payment.entry_id)
            if payment.references["settlement_id"]:
                settlement_of[payment.entry_id] = payment.references["settlement_id"]

    unmapped: dict[str, int] = defaultdict(int)

    for order_id in undecided_order_ids:
        order = orders.get(order_id)
        decline = declines.get(order_id)
        reason = decline.reason if decline else "NOT_DECLINED_BY_ANY_TIER"
        code = REASON_TO_CODE.get(reason, CODE_UNKNOWN)
        if code == CODE_UNKNOWN:
            # Counted so an incomplete mapping shows up as a number rather
            # than as a quietly plausible label.
            unmapped[reason] += 1

        entities = [order_id]
        for payment_id in payments_by_order.get(order_id, []):
            entities.append(payment_id)
            if payment_id in settlement_of:
                entities.append(settlement_of[payment_id])

        result.exceptions.append(
            TypedException(
                order_id=order_id,
                code=code,
                reason=reason,
                entities=tuple(dict.fromkeys(entities)),
                amount_paise=order.amount_paise if order else 0,
                caused_by=decline.caused_by if decline else None,
            )
        )

    # -- group by root cause, then sort by money ----------------------------
    buckets: dict[str, list[TypedException]] = defaultdict(list)
    for item in result.exceptions:
        buckets[item.group_key].append(item)

    for key, items in buckets.items():
        code = items[0].code
        result.groups.append(
            ExceptionGroup(
                group_key=key,
                code=code,
                root_cause=ROOT_CAUSE.get(code, code),
                reasons=tuple(sorted({i.reason for i in items})),
                chain_count=len(items),
                total_paise=sum(i.amount_paise for i in items),
                suggested_action=SUGGESTED_ACTION.get(code, SUGGESTED_ACTION[CODE_UNKNOWN]),
                order_ids=tuple(sorted(i.order_id for i in items)),
                entities=tuple(sorted({e for i in items for e in i.entities})),
            )
        )

    # Money descending. A finance team works the largest exposure first; an
    # id-ordered queue is an order nobody chose and nobody wants.
    result.groups.sort(key=lambda g: (-g.total_paise, g.group_key))

    result.stats.update(
        typed=len(result.exceptions),
        groups=len(result.groups),
        unknown=sum(1 for i in result.exceptions if i.code == CODE_UNKNOWN),
        total_paise=sum(i.amount_paise for i in result.exceptions),
        **{f"unmapped_{reason}": n for reason, n in unmapped.items()},
    )
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def format_report(result: Tier5Result, top: int = 5) -> str:
    out = ["", "tier 5 -- typed exceptions, grouped by root cause", "-" * 70]
    out.append(f"  chains typed                  {result.stats['typed']:>6}")
    out.append(f"  groups                        {result.stats['groups']:>6}")
    out.append(f"  UNKNOWN (unmapped reason)     {result.stats['unknown']:>6}")
    out.append(f"  total at stake                {format_inr(result.stats['total_paise'])}")
    out.append("")
    out.append("  by code")
    for code, n in sorted(result.counts_by_code().items(), key=lambda kv: -kv[1]):
        out.append(f"    {code:<32} {n:>5}")

    unmapped = {k: v for k, v in result.stats.items() if k.startswith("unmapped_")}
    if unmapped:
        out.append("")
        out.append("  reasons with no mapping (why UNKNOWN fired)")
        for key, n in sorted(unmapped.items(), key=lambda kv: -kv[1]):
            out.append(f"    {key[len('unmapped_'):]:<32} {n:>5}")

    out.append("")
    out.append(f"  top {top} groups by rupees at stake")
    out.append("-" * 70)
    for group in result.groups[:top]:
        out.append(f"  {group.rupees:>16}   {group.chain_count:>4} chains   "
                   f"{group.code}")
        out.append(f"                     {group.root_cause}")
        out.append(f"                     -> {group.suggested_action}")
        out.append("")
    out.append("-" * 70)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tier 5 -- type and group exceptions.")
    ap.add_argument("--data", default="data/seed42")
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    from .pipeline import reconcile

    # Rebuilt here rather than carried in the pipeline's return value: that
    # dict must stay JSON-serialisable for --out and for the scorer, and a
    # dataclass in it broke both the moment it was added.
    from . import tier1_exact, tier2_tolerant, tier3_settlement

    ledgers = load(args.data)
    engine = reconcile(args.data, tiers=(1, 2, 3))
    tier1 = tier1_exact.run(ledgers)
    tier2 = tier2_tolerant.run(ledgers, tier1)
    tier3 = tier3_settlement.run(
        ledgers, tier1, already_tied=tier2.settlement_to_bank
    )
    decided = {d["order_id"] for d in engine["decisions"]}
    undecided = [o.entry_id for o in ledgers.orders if o.entry_id not in decided]
    result = run(
        ledgers, undecided,
        merge_declines(tier1.declines, tier2.declines, tier3.declines),
    )
    print(format_report(result, top=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
