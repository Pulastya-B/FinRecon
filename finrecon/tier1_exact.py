#!/usr/bin/env python3
"""
Tier 1 -- exact reference matching.

Four joins, and only one of them is interesting.

    (a) order      -> payment    on order_id, and the amounts must agree
    (b) payment    -> settlement on settlement_id
    (c) settlement -> bank credit on UTR + amount + posting window
    (d) payment    -> chargeback on payment_id, which must return NOTHING

(a) and (b) are lookups. The gateway authored both sides of them, its foreign
keys are real, and a dictionary resolves them. They are here because the chain
needs them, not because they are hard.

(d) is a lookup too, and it is the odd one: it disqualifies rather than links.
A disputed payment still reconciles perfectly on the money -- the chargeback
was deducted inside the settlement net -- but the merchant has not booked the
debit, so the ledgers agree on the total while disagreeing about what happened.
That is an exception for a human, not a match.

(c) is the whole problem. The bank did not author a foreign key -- it authored
prose. The UTR is embedded in a narration that may have been clipped at 35
characters, the amount is a NET figure that a rounding difference can shift by
a paisa, and the credit may post days after the payout date. So (c) is a
conjunction of three independent conditions, not one lookup, and it is expected
to fail on a meaningful fraction of rows. That is the point: the rows it cannot
claim are what Tiers 2 and 3 exist to recover.

What this tier will not do
--------------------------
It never guesses. Every join is exact equality on a value that is present in
the data, and anything that does not resolve is DECLINED with a recorded reason
and passed through untouched. Declining is always safe -- a miss costs a human
two minutes, a wrong match is silent, plausible, lands in the books, and
surfaces months later as an unexplained variance. So where two candidates fit,
this tier takes neither.

It also never opens ground_truth.json. It reads the normalised ledgers and
config/rates.yaml -- the holiday calendar a merchant genuinely holds -- and
nothing else. A tier that has seen the oracle cannot be scored against it.

Run:
    python -m finrecon.tier1_exact --data data/seed42
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Mapping

import yaml

from .bizcal import BusinessCalendar
from .normalize import LedgerEntry, NormalizedLedgers, load

# Rule identifiers, recorded on every match so a decision can be explained
# after the fact. A match nobody can account for is not auditable, and an
# unauditable reconciliation is worth very little.
RULE_A_ORDER_ID = "A_ORDER_ID"
RULE_B_SETTLEMENT_ID = "B_SETTLEMENT_ID"
RULE_C_UTR_AMOUNT_DATE = "C_UTR_AMOUNT_DATE"
# (d) is a join whose EMPTY result is what the match depends on: the dispute
# lookup ran and returned nothing. Recorded positively so a match says "checked
# for chargebacks, found none" rather than staying silent about a check that
# never happened.
RULE_D_CHARGEBACK_CLEAR = "D_CHARGEBACK_CLEAR"

# Why each item was passed through rather than matched. These are diagnostic
# labels, NOT exception codes -- Tier 1 declines to decide, it does not
# classify. Turning "I could not match this" into "this is a MISSING_IN_BANK"
# is a judgement that needs the batch arithmetic of a later tier.
NO_PAYMENT_FOR_ORDER = "NO_PAYMENT_FOR_ORDER"
MULTIPLE_PAYMENTS_FOR_ORDER = "MULTIPLE_PAYMENTS_FOR_ORDER"
ORDER_PAYMENT_AMOUNT_MISMATCH = "ORDER_PAYMENT_AMOUNT_MISMATCH"
PAYMENT_HAS_CHARGEBACK = "PAYMENT_HAS_CHARGEBACK"
PAYMENT_HAS_NO_SETTLEMENT_ID = "PAYMENT_HAS_NO_SETTLEMENT_ID"
SETTLEMENT_ID_NOT_FOUND = "SETTLEMENT_ID_NOT_FOUND"
SETTLEMENT_NOT_MATCHED_TO_BANK = "SETTLEMENT_NOT_MATCHED_TO_BANK"
NO_BANK_CREDIT_WITH_UTR = "NO_BANK_CREDIT_WITH_UTR"
AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
OUTSIDE_POSTING_WINDOW = "OUTSIDE_POSTING_WINDOW"
AMBIGUOUS_MULTI_CANDIDATE = "AMBIGUOUS_MULTI_CANDIDATE"
BANK_ROW_ALREADY_CLAIMED = "BANK_ROW_ALREADY_CLAIMED"
NARRATION_HAS_NO_UTR = "NARRATION_HAS_NO_UTR"
UTR_NOT_IN_SETTLEMENT_REPORT = "UTR_NOT_IN_SETTLEMENT_REPORT"

# How many clearing days after the payout date a credit may still post and
# still be the same payout.
#
# Deliberately a stated operational assumption rather than a number read out of
# config/defects.yaml. defects.yaml describes how the synthetic data was
# damaged; a production reconciler does not get to read that, it gets an SLA
# from its gateway agreement. Sizing the window from the injector would be
# tuning to the answer key.
#
# Business days, not calendar days: a payout on a Friday posting on the
# following Tuesday is on time, and an engine that calls that late generates a
# queue of false exceptions every Monday -- which trains the operator to ignore
# the queue, the worst failure this class of tool has.
DEFAULT_POSTING_WINDOW_DAYS = 3


# --------------------------------------------------------------------------
# Output shapes
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Decline:
    """Why one entity was passed through instead of matched.

    Carries the entity kind alongside the reason because the two are read
    together and a bare reason count lies. One settlement failing on amount
    strands every order in its batch, so a flat tally shows "24 AMOUNT_MISMATCH"
    when the actual event was a single settlement and 23 consequences of it --
    which is a different bug report and a different fix.
    """

    kind: str        # orders | payments | settlements | bank
    reason: str
    # The entity whose failure caused this one, where it was inherited.
    caused_by: str | None = None


@dataclass(frozen=True)
class ExactMatch:
    """One fully-resolved order -> payment -> settlement -> bank chain.

    confidence is 1.0 by construction and is not a knob. Every join underneath
    it was exact equality on a value both sides actually carried; there is no
    inference to discount. A tier that scores its own guesses belongs above
    this one.
    """

    order_id: str
    payment_id: str
    settlement_id: str
    bank_row_id: str
    rules_fired: tuple[str, ...]
    evidence: Mapping[str, str]
    confidence: float = 1.0


@dataclass
class Tier1Result:
    """Matches, plus everything Tier 1 refused to touch.

    The residual pools are the deliverable as much as the matches are. Tier 2
    needs the unmatched settlements and the unmatched bank credits in their
    original form -- not filtered, not annotated with a guess -- or it inherits
    this tier's blind spots instead of correcting them.
    """

    matches: list[ExactMatch] = field(default_factory=list)

    unmatched_orders: list[LedgerEntry] = field(default_factory=list)
    unmatched_payments: list[LedgerEntry] = field(default_factory=list)
    unmatched_settlements: list[LedgerEntry] = field(default_factory=list)
    unmatched_bank_credits: list[LedgerEntry] = field(default_factory=list)

    # entity_id -> why it was passed through. Audit trail, not a verdict.
    declines: dict[str, Decline] = field(default_factory=dict)

    # settlement_id -> bank_row_id, for the join that actually mattered.
    settlement_to_bank: dict[str, str] = field(default_factory=dict)

    # Counts of what happened at each join, for the funnel report.
    stats: dict[str, int] = field(default_factory=dict)

    def decline_reasons(self) -> dict[str, int]:
        return dict(Counter(d.reason for d in self.declines.values()))

    def decline_breakdown(self) -> dict[str, dict[str, int]]:
        """Decline reasons grouped by entity kind -- see `Decline` for why."""
        out: dict[str, Counter[str]] = defaultdict(Counter)
        for decline in self.declines.values():
            out[decline.kind][decline.reason] += 1
        return {kind: dict(counts) for kind, counts in out.items()}

    def root_causes(self) -> dict[str, int]:
        """Declines that were not inherited from another entity's failure.

        The list to actually work: 6 settlements missing a bank credit is the
        finding, and the 118 orders stranded behind them are its blast radius.
        """
        return dict(
            Counter(
                d.reason for d in self.declines.values() if d.caused_by is None
            )
        )

    @property
    def bank_credits_total(self) -> int:
        return self.stats.get("bank_credits_total", 0)

    @property
    def bank_credits_matched(self) -> int:
        return len(self.settlement_to_bank)

    @property
    def bank_credit_hit_rate(self) -> float:
        """Share of bank credits Tier 1 tied to a settlement.

        The headline difficulty number: this is the fraction of the bank
        boundary that a pure reference join can cross unaided.
        """
        total = self.bank_credits_total
        return self.bank_credits_matched / total if total else 0.0


# --------------------------------------------------------------------------
# Order-side admission -- shared with Tier 2
# --------------------------------------------------------------------------
def order_side_gates(
    order: LedgerEntry,
    payments_by_order: Mapping[str, list[LedgerEntry]],
    chargebacks_by_payment: Mapping[str, list[LedgerEntry]],
) -> tuple[LedgerEntry | None, str | None]:
    """Joins (a) and (d) plus the amount gate -- everything decidable from an
    order and its payment alone, before the bank boundary is involved.

    Returns (payment, None) when the chain is admissible, or (None, reason).

    Shared with Tier 2 deliberately. Both tiers must admit exactly the same
    chains and differ only in how they cross the bank boundary; if the tolerant
    tier reimplemented these gates it would eventually accept a disputed or
    typo'd chain that the exact tier rejected, and that is a precision
    regression nobody would look for -- the tiers would each be individually
    correct and the pipeline still wrong.
    """
    candidates = payments_by_order.get(order.entry_id, [])

    if not candidates:
        return None, NO_PAYMENT_FOR_ORDER

    if len(candidates) > 1:
        # A duplicate capture. GROUP BY finds it instantly; deciding which
        # capture is the real sale, and whether the batch still balances, is
        # real work and does not belong in a reference join.
        return None, MULTIPLE_PAYMENTS_FOR_ORDER

    payment = candidates[0]

    # Sharing an order_id and agreeing about the money are different claims,
    # and only the second one reconciles anything. A manually-keyed order
    # carrying a typo joins cleanly by id while the merchant's books and the
    # gateway state different numbers -- somebody has to look at that, and
    # calling it MATCHED is precisely the silent error this engine avoids.
    if payment.amount_paise != order.amount_paise:
        return None, ORDER_PAYMENT_AMOUNT_MISMATCH

    # Join (d): a hit here disqualifies the chain. The money still reconciles
    # exactly -- the dispute was deducted inside the settlement net -- but the
    # merchant has not booked the debit, so their books and the gateway's
    # disagree about what happened even though they agree about the total.
    #
    # Chargebacks and refunds look symmetric here and are not, which is why
    # this names chargebacks specifically rather than reversals generally. A
    # refund is a deduction the merchant initiated and already knows about; a
    # chargeback is one imposed by an issuer, typically not yet booked. On seed
    # 42 all 81 matched chains touching a refund are correctly MATCHED, so
    # declining reversals as a class would cost 81 good matches to catch 10 bad.
    if payment.entry_id in chargebacks_by_payment:
        return None, PAYMENT_HAS_CHARGEBACK

    return payment, None


# --------------------------------------------------------------------------
# Join (c) -- the one that matters
# --------------------------------------------------------------------------
def _posting_window(
    settled_on: date, cal: BusinessCalendar, window_days: int
) -> tuple[date, date]:
    """Dates on which a payout settled on `settled_on` may legitimately post.

    Lower bound is the payout date itself -- money does not arrive before it
    was sent. Upper bound is `window_days` clearing days later.
    """
    return settled_on, cal.add_business_days(settled_on, window_days)


def _match_settlements_to_bank(
    settlements: list[LedgerEntry],
    bank: list[LedgerEntry],
    cal: BusinessCalendar,
    window_days: int,
    result: Tier1Result,
) -> None:
    """Join (c): settlement -> bank credit on UTR AND amount AND date window.

    All three conditions, not any one of them. The UTR alone is very nearly
    sufficient on this data, and that is exactly why it is not trusted alone: a
    reconciler that matches on a single recovered token has no way to notice
    when the token was recovered wrongly. The amount and the window are cheap,
    independent confirmations, and requiring the conjunction is what keeps this
    tier's precision at 1.0 rather than merely high.
    """
    credits = [e for e in bank if e.entry_type == "bank_credit"]
    result.stats["bank_credits_total"] = len(credits)
    result.stats["bank_debits_total"] = len(bank) - len(credits)

    # Index the bank side by the UTR Tier 0 recovered. Rows whose narration was
    # clipped before the UTR carry None and are simply not in this index --
    # they are unreachable from here by construction, and recovering them from
    # batch arithmetic is Tier 2/3's job, not something to approximate here.
    by_utr: dict[str, list[LedgerEntry]] = defaultdict(list)
    no_utr: list[LedgerEntry] = []
    for entry in credits:
        utr = entry.references["utr"]
        if utr is None:
            no_utr.append(entry)
        else:
            by_utr[utr].append(entry)

    result.stats["bank_credits_with_utr"] = len(credits) - len(no_utr)
    result.stats["bank_credits_without_utr"] = len(no_utr)

    claimed: dict[str, str] = {}  # bank_row_id -> settlement_id

    for settlement in settlements:
        utr = settlement.references["utr"]
        sid = settlement.entry_id

        candidates = list(by_utr.get(utr, ())) if utr else []
        if not candidates:
            result.declines[sid] = Decline("settlements", NO_BANK_CREDIT_WITH_UTR)
            continue

        # Narrow UTR -> amount -> date, in that order, so the recorded reason
        # names the first condition that actually failed.
        on_amount = [
            c for c in candidates
            if c.amounts["credit_paise"] == settlement.amounts["net_paise"]
        ]
        if not on_amount:
            # Exact equality, deliberately: rounding_drift shifts a batch by a
            # paisa or two, and absorbing that here would mean this tier owns a
            # tolerance band. Tolerance is Tier 3's, where the balancing
            # arithmetic can say what the gap consists of.
            result.declines[sid] = Decline("settlements", AMOUNT_MISMATCH)
            continue

        lo, hi = _posting_window(settlement.event_date, cal, window_days)
        on_date = [c for c in on_amount if lo <= c.event_date <= hi]
        if not on_date:
            result.declines[sid] = Decline("settlements", OUTSIDE_POSTING_WINDOW)
            continue

        if len(on_date) > 1:
            # Two credits fit equally well. Picking one would be a coin flip
            # dressed as a match, so this tier takes neither.
            result.declines[sid] = Decline("settlements", AMBIGUOUS_MULTI_CANDIDATE)
            continue

        winner = on_date[0]
        if winner.entry_id in claimed:
            # One bank credit cannot be two payouts. Cannot arise while UTRs
            # are unique, which is precisely why it is worth catching: if it
            # ever fires, an assumption broke silently.
            result.declines[sid] = Decline("settlements", BANK_ROW_ALREADY_CLAIMED)
            continue

        claimed[winner.entry_id] = sid
        result.settlement_to_bank[sid] = winner.entry_id

    # Bank credits Tier 1 could not account for, with the reason. These pass
    # through as-is -- a credit whose UTR is not in the settlement report might
    # be a direct customer NEFT or a payout whose report row is missing, and
    # Tier 1 has no basis to say which.
    settlement_utrs = {s.references["utr"] for s in settlements}
    for entry in credits:
        if entry.entry_id in claimed:
            continue
        result.unmatched_bank_credits.append(entry)
        utr = entry.references["utr"]
        if utr is None:
            result.declines[entry.entry_id] = Decline("bank", NARRATION_HAS_NO_UTR)
        elif utr not in settlement_utrs:
            result.declines[entry.entry_id] = Decline("bank", UTR_NOT_IN_SETTLEMENT_REPORT)
        else:
            result.declines[entry.entry_id] = Decline("bank", SETTLEMENT_NOT_MATCHED_TO_BANK)


# --------------------------------------------------------------------------
# Tier 1
# --------------------------------------------------------------------------
def run(
    ledgers: NormalizedLedgers,
    rates_path: str | Path = "config/rates.yaml",
    posting_window_days: int = DEFAULT_POSTING_WINDOW_DAYS,
) -> Tier1Result:
    """Run all four joins and return matches plus untouched residue."""
    result = Tier1Result()

    rates = yaml.safe_load(Path(rates_path).read_text())
    holidays = [
        datetime.strptime(h, "%Y-%m-%d").date() if isinstance(h, str) else h
        for h in rates.get("holidays_2026", [])
    ]
    cal = BusinessCalendar(holidays)

    # -- join (c) first: it is the binding constraint, so resolving it up front
    # -- means (a) and (b) are walked once rather than speculatively.
    _match_settlements_to_bank(
        ledgers.settlements, ledgers.bank, cal, posting_window_days, result
    )
    settlements_by_id = {s.entry_id: s for s in ledgers.settlements}

    # Disputes indexed by the payment they reverse, for join (d) below.
    chargebacks_by_payment: dict[str, list[LedgerEntry]] = defaultdict(list)
    for chargeback in ledgers.chargebacks:
        payment_id = chargeback.references["payment_id"]
        if payment_id:
            chargebacks_by_payment[payment_id].append(chargeback)

    # -- join (a): order -> payment on order_id -------------------------------
    payments_by_order: dict[str, list[LedgerEntry]] = defaultdict(list)
    for payment in ledgers.payments:
        order_id = payment.references["order_id"]
        if order_id:
            payments_by_order[order_id].append(payment)

    matched_payment_ids: set[str] = set()
    matched_settlement_ids: set[str] = set()

    for order in ledgers.orders:
        oid = order.entry_id

        # -- joins (a) and (d), plus the amount gate --------------------------
        payment, reason = order_side_gates(
            order, payments_by_order, chargebacks_by_payment
        )
        if payment is None:
            result.declines[oid] = Decline("orders", reason or "UNKNOWN")
            result.unmatched_orders.append(order)
            continue


        # -- join (b): payment -> settlement on settlement_id -----------------
        sid = payment.references["settlement_id"]
        if not sid:
            result.declines[oid] = Decline("orders", PAYMENT_HAS_NO_SETTLEMENT_ID)
            result.unmatched_orders.append(order)
            continue
        if sid not in settlements_by_id:
            result.declines[oid] = Decline("orders", SETTLEMENT_ID_NOT_FOUND)
            result.unmatched_orders.append(order)
            continue

        # -- join (c), already resolved above ---------------------------------
        bank_row_id = result.settlement_to_bank.get(sid)
        if bank_row_id is None:
            # The chain is intact up to the bank boundary and dies there. That
            # is the interesting failure, and the reason is already recorded
            # against the settlement rather than being restated per order.
            upstream = result.declines.get(sid)
            result.declines[oid] = Decline(
                "orders",
                upstream.reason if upstream else SETTLEMENT_NOT_MATCHED_TO_BANK,
                caused_by=sid,
            )
            result.unmatched_orders.append(order)
            continue

        settlement = settlements_by_id[sid]
        result.matches.append(
            ExactMatch(
                order_id=oid,
                payment_id=payment.entry_id,
                settlement_id=sid,
                bank_row_id=bank_row_id,
                rules_fired=(
                    RULE_A_ORDER_ID,
                    RULE_B_SETTLEMENT_ID,
                    RULE_C_UTR_AMOUNT_DATE,
                    RULE_D_CHARGEBACK_CLEAR,
                ),
                evidence={
                    "order_id": oid,
                    "settlement_id": sid,
                    "utr": settlement.references["utr"] or "",
                    "net_paise": str(settlement.amounts["net_paise"]),
                    "settled_on": settlement.event_date.isoformat(),
                },
            )
        )
        matched_payment_ids.add(payment.entry_id)
        matched_settlement_ids.add(sid)

    # -- residue --------------------------------------------------------------
    result.unmatched_payments = [
        p for p in ledgers.payments if p.entry_id not in matched_payment_ids
    ]
    result.unmatched_settlements = [
        s for s in ledgers.settlements if s.entry_id not in result.settlement_to_bank
    ]

    result.stats.update(
        orders_total=len(ledgers.orders),
        payments_total=len(ledgers.payments),
        settlements_total=len(ledgers.settlements),
        settlements_matched=len(result.settlement_to_bank),
        matches=len(result.matches),
        orders_unmatched=len(result.unmatched_orders),
        payments_unmatched=len(result.unmatched_payments),
        settlements_unmatched=len(result.unmatched_settlements),
        bank_credits_matched=len(result.settlement_to_bank),
        bank_credits_unmatched=len(result.unmatched_bank_credits),
    )
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def format_funnel(result: Tier1Result) -> str:
    s = result.stats
    out = ["", "tier 1 -- exact reference matching", "-" * 60]
    out.append(f"  orders                    {s['orders_total']:>6}")
    out.append(f"  payments                  {s['payments_total']:>6}")
    out.append(f"  settlements               {s['settlements_total']:>6}")
    out.append("")
    out.append(f"  bank credits              {s['bank_credits_total']:>6}")
    out.append(f"    with a UTR recovered    {s['bank_credits_with_utr']:>6}")
    out.append(f"    narration clipped       {s['bank_credits_without_utr']:>6}")
    out.append("")
    out.append(f"  join (c) settlements tied {s['settlements_matched']:>6}"
               f" / {s['settlements_total']}")
    out.append(f"  BANK CREDIT HIT RATE      {result.bank_credit_hit_rate * 100:>5.1f}%"
               f"   {result.bank_credits_matched}/{result.bank_credits_total}")
    out.append("")
    out.append(f"  chains matched            {s['matches']:>6}"
               f" / {s['orders_total']} orders")
    out.append("")
    out.append("  passed through untouched, by entity kind")
    breakdown = result.decline_breakdown()
    for kind in ("settlements", "bank", "orders", "payments"):
        reasons = breakdown.get(kind)
        if not reasons:
            continue
        out.append(f"    {kind}")
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            out.append(f"      {reason:<32} {n:>5}")
    out.append("")
    out.append("  root causes only (inherited failures excluded)")
    for reason, n in sorted(result.root_causes().items(), key=lambda kv: -kv[1]):
        out.append(f"    {reason:<34} {n:>5}")
    out.append("-" * 60)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tier 1 -- exact reference matching.")
    ap.add_argument("--data", default="data/seed42", help="dataset directory")
    ap.add_argument("--rates", default="config/rates.yaml")
    ap.add_argument("--window", type=int, default=DEFAULT_POSTING_WINDOW_DAYS,
                    help="posting window, in clearing days")
    args = ap.parse_args(argv)

    result = run(load(args.data), rates_path=args.rates, posting_window_days=args.window)
    print(format_funnel(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
