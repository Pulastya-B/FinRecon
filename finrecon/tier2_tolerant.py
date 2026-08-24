#!/usr/bin/env python3
"""
Tier 2 -- tolerant matching, for what exact reference matching could not claim.

Tier 1 crosses the bank boundary only when a UTR survived in the narration and
the net matched to the paisa. This tier takes its residue and relaxes exactly
three things, one at a time and never all at once without saying so:

    amount     exact  -> within a fee-adjusted band
    date       T+3    -> T+6 clearing days
    reference  UTR    -> narration shape, where the UTR was clipped away

What fuzzy narration matching actually buys
-------------------------------------------
Less than it looks like, and saying so is the point.

The narration templates put the UTR in different positions. `NEFT CR-
RATN0000088-RAZORPAY SOFTWARE PVT LTD-{utr}` carries it past character 45, so a
35-character clip destroys it -- and every settlement clipped from that template
produces the *identical* string. Measured on seed 42: three different
settlements score 100.0 against the same clipped row. Similarity cannot tell
which settlement a row belongs to, because the surviving text contains nothing
that varies between settlements.

So narration similarity is a PAYOUT-SHAPE GATE, not an identity signal. It
answers "is this row a gateway payout at all", where a customer's direct NEFT
scores 44 and ambient noise scores 33-34 against the same templates -- a wide,
comfortable margin. Identity comes from amount and date. Selling this as fuzzy
reference matching would be selling a coincidence.

Why a bank row that carries someone else's UTR is never a candidate
-------------------------------------------------------------------
The single most valuable rule here. Eleven of the sixteen unclaimed credits on
seed 42 are direct customer NEFTs -- real money, plausible amounts, a
well-formed UTR that belongs to no settlement. They are the obvious way for a
tolerant tier to manufacture false positives. A row whose narration carries a
legible reference to something *other* than this settlement is not this
settlement's payout, whatever the amount says, so it is excluded before any
band is applied.

The ambiguity rule
------------------
A candidate with more than one plausible match is NOT matched. It passes
through. Uniqueness is checked in BOTH directions -- one settlement with two
candidate rows is ambiguous, and so is one row claimed by two settlements --
because a greedy first-come assignment would make the result depend on
iteration order, which is not a property anyone should have to reason about
when auditing a reconciliation months later.

Run:
    python -m finrecon.tier2_tolerant --data data/seed42
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Mapping

import yaml
from rapidfuzz import fuzz

from .bizcal import BusinessCalendar
from .normalize import LedgerEntry, NormalizedLedgers, load
from .tier1_exact import (
    AMBIGUOUS_MULTI_CANDIDATE,
    AMOUNT_MISMATCH,
    Decline,
    Tier1Result,
    order_side_gates,
    OUTSIDE_POSTING_WINDOW,
)
from . import tier1_exact

# The gateway's own payout descriptors. This is contract knowledge -- a merchant
# knows what their settlement narration looks like because they receive one
# every day -- not oracle knowledge. Nothing here is read from defects.yaml.
PAYOUT_NARRATION_TEMPLATES: tuple[str, ...] = (
    "NEFT-RAZORPAY SOFTWARE PRIVATE LIMITED-{utr}-SETTLEMENT",
    "IMPS/{utr}/RAZORPAY SOFTWARE/PAYOUT",
    "NEFT CR-RATN0000088-RAZORPAY SOFTWARE PVT LTD-{utr}",
    "RTGS-{utr}-RAZORPAYSOFTWARE-MERCHANT SETTLEMENT",
)

# Set from the measured separation, not from a boundary: real payout clips score
# 100, customer transfers 44, ambient noise 33-34. Anything from 60 to 95 splits
# those cleanly, so 85 sits in open space rather than on an edge -- a threshold
# tuned to within a point of the nearest negative is a threshold that will move
# on the next seed.
NARRATION_SIMILARITY_THRESHOLD = 85.0

# Fee-adjusted tolerance band.
#
# A cycle's net is the sum of independently-rounded terms -- one fee and one tax
# per payment, plus withholding on the gross -- so two implementations that
# round differently can disagree by a paisa on each. The band therefore widens
# with batch size rather than being a flat constant, because a 26-payment batch
# genuinely has more places to drift than a 3-payment one.
#
# It is NOT sized at the theoretical worst case of one paisa per rounded term.
# Rounding errors cancel about as often as they accumulate, and a worst-case
# band on a 26-payment batch would be ~53 paise wide -- which buys nothing here
# and would start admitting coincidences on a busier ledger.
BASE_TOLERANCE_PAISE = 2
PER_PAYMENT_TOLERANCE_PAISE = 1

# Double Tier 1's SLA window. An item this late is operationally late, not
# missing, and being able to tell those apart is the entire reason the window
# is a parameter instead of a constant.
TIER2_POSTING_WINDOW_DAYS = 6

RULE_E_UTR_TOLERANT_AMOUNT = "E_UTR_TOLERANT_AMOUNT"
RULE_F_NARRATION_AMOUNT_DATE = "F_NARRATION_AMOUNT_DATE"

# Confidence is a statement about WHICH SIGNALS AGREED, not a tuned score.
# One soft signal is worth more than two, and that is the whole ordering.
CONF_UTR_EXACT_AMOUNT_TOLERANT = 0.9   # reference exact, amount within band
CONF_NARRATION_AMOUNT_EXACT = 0.8      # reference soft, amount to the paisa
CONF_NARRATION_AMOUNT_TOLERANT = 0.7   # reference soft, amount within band

BANK_ROW_CARRIES_OTHER_UTR = "BANK_ROW_CARRIES_OTHER_UTR"
NARRATION_NOT_PAYOUT_SHAPED = "NARRATION_NOT_PAYOUT_SHAPED"
NO_TOLERANT_CANDIDATE = "NO_TOLERANT_CANDIDATE"
BANK_ROW_CONTESTED = "BANK_ROW_CONTESTED"


# --------------------------------------------------------------------------
# Output shapes
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TolerantMatch:
    """One chain resolved on relaxed evidence, with the relaxation recorded.

    Unlike Tier 1's, this confidence is below 1.0 and means something: an
    auditor re-checking the books later needs to know which of these rested on
    a paisa of slack or a clipped descriptor, and `evidence` carries the exact
    slack that was used rather than the fact that some was.
    """

    order_id: str
    payment_id: str
    settlement_id: str
    bank_row_id: str
    rules_fired: tuple[str, ...]
    evidence: Mapping[str, str]
    confidence: float


@dataclass
class Candidate:
    """One (settlement, bank row) pair that survived every filter.

    Held rather than acted on, because uniqueness cannot be judged until every
    pair exists. Matching the first plausible pair and moving on is how a
    tolerant tier silently becomes order-dependent.
    """

    settlement_id: str
    bank_row_id: str
    rule: str
    confidence: float
    evidence: Mapping[str, str]


@dataclass
class Tier2Result:
    matches: list[TolerantMatch] = field(default_factory=list)

    # settlement_id -> bank_row_id, for ties THIS tier made (not Tier 1's).
    settlement_to_bank: dict[str, str] = field(default_factory=dict)

    unmatched_orders: list[LedgerEntry] = field(default_factory=list)
    unmatched_settlements: list[LedgerEntry] = field(default_factory=list)
    unmatched_bank_credits: list[LedgerEntry] = field(default_factory=list)

    declines: dict[str, Decline] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)

    # Every pair considered, including rejected ones -- the audit trail for
    # "why did you not match this", which is the question a tolerant tier
    # actually gets asked.
    candidates: list[Candidate] = field(default_factory=list)

    def confidence_histogram(self) -> dict[float, int]:
        return dict(Counter(m.confidence for m in self.matches))

    def decline_breakdown(self) -> dict[str, dict[str, int]]:
        out: dict[str, Counter[str]] = defaultdict(Counter)
        for decline in self.declines.values():
            out[decline.kind][decline.reason] += 1
        return {kind: dict(counts) for kind, counts in out.items()}


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------
def fee_adjusted_tolerance(n_payments: int) -> int:
    """Paise of slack allowed on a batch of this size. See the constants above."""
    return BASE_TOLERANCE_PAISE + PER_PAYMENT_TOLERANCE_PAISE * n_payments


def narration_similarity(narration: str, utr: str) -> float:
    """How well a narration matches this settlement's expected payout descriptor.

    Each template is rendered with the settlement's own UTR and truncated to the
    observed narration's length before comparison, because comparing a clipped
    string against a full one penalises the clip itself rather than the content.

    Returns the best score across templates. Note this is a shape score: for the
    template whose UTR sits past the clip point, every settlement scores
    identically. See the module docstring.
    """
    if not narration:
        return 0.0
    return max(
        fuzz.partial_ratio(narration, template.format(utr=utr)[: len(narration)])
        for template in PAYOUT_NARRATION_TEMPLATES
    )


def _classify_pair(
    settlement: LedgerEntry,
    credit: LedgerEntry,
    tolerance: int,
    cal: BusinessCalendar,
    window_days: int,
) -> tuple[Candidate | None, str | None]:
    """Decide whether one settlement/credit pair is a candidate, and how strong.

    Returns (candidate, None) or (None, reason-it-failed). The reason is kept
    for the audit trail even though only settlement-level declines are recorded,
    because "no candidate" and "a candidate that failed on date" are different
    findings for whoever works the queue.
    """
    settlement_utr = settlement.references["utr"] or ""
    credit_utr = credit.references["utr"]

    # -- reference -----------------------------------------------------------
    if credit_utr is not None:
        # The row carries a legible reference. If it is not ours, this is
        # someone else's money -- a customer's direct NEFT, most often -- and no
        # amount coincidence should be allowed to override that.
        if credit_utr != settlement_utr:
            return None, BANK_ROW_CARRIES_OTHER_UTR
        rule, reference_exact = RULE_E_UTR_TOLERANT_AMOUNT, True
        similarity = 100.0
    else:
        similarity = narration_similarity(credit.text, settlement_utr)
        if similarity < NARRATION_SIMILARITY_THRESHOLD:
            return None, NARRATION_NOT_PAYOUT_SHAPED
        rule, reference_exact = RULE_F_NARRATION_AMOUNT_DATE, False

    # -- amount --------------------------------------------------------------
    delta = credit.amounts["credit_paise"] - settlement.amounts["net_paise"]
    if abs(delta) > tolerance:
        return None, AMOUNT_MISMATCH
    amount_exact = delta == 0

    # -- date ----------------------------------------------------------------
    # Lower bound is the payout date itself: money does not arrive before it
    # was sent, and a credit that predates its own settlement is evidence
    # against the pairing, not slack to be absorbed.
    lo = settlement.event_date
    hi = cal.add_business_days(lo, window_days)
    if not (lo <= credit.event_date <= hi):
        return None, OUTSIDE_POSTING_WINDOW

    if reference_exact:
        confidence = CONF_UTR_EXACT_AMOUNT_TOLERANT
    elif amount_exact:
        confidence = CONF_NARRATION_AMOUNT_EXACT
    else:
        confidence = CONF_NARRATION_AMOUNT_TOLERANT

    return (
        Candidate(
            settlement_id=settlement.entry_id,
            bank_row_id=credit.entry_id,
            rule=rule,
            confidence=confidence,
            evidence={
                "utr": settlement_utr,
                "bank_utr": credit_utr or "",
                "amount_delta_paise": str(delta),
                "tolerance_paise": str(tolerance),
                "narration_similarity": f"{similarity:.1f}",
                "settled_on": lo.isoformat(),
                "posted_on": credit.event_date.isoformat(),
                "days_late": str(cal.business_days_between(lo, credit.event_date)),
            },
        ),
        None,
    )


# --------------------------------------------------------------------------
# Tier 2
# --------------------------------------------------------------------------
def run(
    ledgers: NormalizedLedgers,
    tier1: Tier1Result,
    rates_path: str | Path = "config/rates.yaml",
    posting_window_days: int = TIER2_POSTING_WINDOW_DAYS,
) -> Tier2Result:
    """Match Tier 1's residue on relaxed evidence, refusing anything ambiguous."""
    result = Tier2Result()

    rates = yaml.safe_load(Path(rates_path).read_text())
    holidays = [
        datetime.strptime(h, "%Y-%m-%d").date() if isinstance(h, str) else h
        for h in rates.get("holidays_2026", [])
    ]
    cal = BusinessCalendar(holidays)

    payments_by_settlement: dict[str, list[LedgerEntry]] = defaultdict(list)
    for payment in ledgers.payments:
        sid = payment.references["settlement_id"]
        if sid:
            payments_by_settlement[sid].append(payment)

    settlements = {s.entry_id: s for s in tier1.unmatched_settlements}
    credits = [
        b for b in tier1.unmatched_bank_credits if b.entry_type == "bank_credit"
    ]

    result.stats["settlements_in"] = len(settlements)
    result.stats["bank_credits_in"] = len(credits)

    # -- phase 1: every pair that survives every filter ----------------------
    for settlement in settlements.values():
        tolerance = fee_adjusted_tolerance(
            len(payments_by_settlement.get(settlement.entry_id, ()))
        )
        for credit in credits:
            candidate, _reason = _classify_pair(
                settlement, credit, tolerance, cal, posting_window_days
            )
            if candidate is not None:
                result.candidates.append(candidate)

    # -- phase 2: uniqueness, in both directions -----------------------------
    by_settlement: dict[str, list[Candidate]] = defaultdict(list)
    by_bank_row: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in result.candidates:
        by_settlement[candidate.settlement_id].append(candidate)
        by_bank_row[candidate.bank_row_id].append(candidate)

    contested_rows = {rid for rid, cs in by_bank_row.items() if len(cs) > 1}

    for sid, settlement in settlements.items():
        candidates = by_settlement.get(sid, [])
        if not candidates:
            result.declines[sid] = Decline("settlements", NO_TOLERANT_CANDIDATE)
            continue
        if len(candidates) > 1:
            # Two rows fit this settlement. Either could be right; picking is a
            # coin flip that lands in the books.
            result.declines[sid] = Decline("settlements", AMBIGUOUS_MULTI_CANDIDATE)
            continue
        candidate = candidates[0]
        if candidate.bank_row_id in contested_rows:
            # This settlement has one candidate, but that row is also the best
            # fit for another settlement. Resolving it needs batch arithmetic
            # this tier does not do.
            result.declines[sid] = Decline("settlements", BANK_ROW_CONTESTED)
            continue
        result.settlement_to_bank[sid] = candidate.bank_row_id

    # -- phase 3: chains unlocked by the new ties ----------------------------
    # The order-side gates are re-run rather than inherited from Tier 1's
    # decline record. Tier 2 emits its own matches and must stand on its own
    # evidence -- and the shared helper means both tiers admit exactly the same
    # chains, so a tolerant tier cannot quietly accept a disputed payment that
    # the exact tier rejected.
    payments_by_order: dict[str, list[LedgerEntry]] = defaultdict(list)
    for payment in ledgers.payments:
        order_id = payment.references["order_id"]
        if order_id:
            payments_by_order[order_id].append(payment)

    chargebacks_by_payment: dict[str, list[LedgerEntry]] = defaultdict(list)
    for chargeback in ledgers.chargebacks:
        payment_id = chargeback.references["payment_id"]
        if payment_id:
            chargebacks_by_payment[payment_id].append(chargeback)

    confidence_by_settlement = {
        c.settlement_id: c
        for c in result.candidates
        if result.settlement_to_bank.get(c.settlement_id) == c.bank_row_id
    }

    for order in tier1.unmatched_orders:
        payment, reason = order_side_gates(
            order, payments_by_order, chargebacks_by_payment
        )
        if payment is None:
            result.declines[order.entry_id] = Decline("orders", reason or "UNKNOWN")
            result.unmatched_orders.append(order)
            continue

        sid = payment.references["settlement_id"]
        bank_row_id = result.settlement_to_bank.get(sid or "")
        if bank_row_id is None:
            upstream = result.declines.get(sid or "")
            result.declines[order.entry_id] = Decline(
                "orders",
                upstream.reason if upstream else NO_TOLERANT_CANDIDATE,
                caused_by=sid,
            )
            result.unmatched_orders.append(order)
            continue

        candidate = confidence_by_settlement[sid]
        result.matches.append(
            TolerantMatch(
                order_id=order.entry_id,
                payment_id=payment.entry_id,
                settlement_id=sid,
                bank_row_id=bank_row_id,
                rules_fired=(
                    tier1_exact.RULE_A_ORDER_ID,
                    tier1_exact.RULE_B_SETTLEMENT_ID,
                    candidate.rule,
                    tier1_exact.RULE_D_CHARGEBACK_CLEAR,
                ),
                evidence=candidate.evidence,
                confidence=candidate.confidence,
            )
        )

    # -- residue -------------------------------------------------------------
    result.unmatched_settlements = [
        s for sid, s in settlements.items() if sid not in result.settlement_to_bank
    ]
    claimed = set(result.settlement_to_bank.values())
    result.unmatched_bank_credits = [b for b in credits if b.entry_id not in claimed]

    result.stats.update(
        candidates=len(result.candidates),
        settlements_tied=len(result.settlement_to_bank),
        settlements_still_unmatched=len(result.unmatched_settlements),
        bank_credits_claimed=len(claimed),
        bank_credits_still_unmatched=len(result.unmatched_bank_credits),
        matches=len(result.matches),
        orders_still_unmatched=len(result.unmatched_orders),
        contested_rows=len(contested_rows),
    )
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def format_funnel(tier1: Tier1Result, result: Tier2Result) -> str:
    s = result.stats
    out = ["", "tier 2 -- tolerant matching (on tier 1's residue)", "-" * 62]
    out.append(f"  settlements in                {s['settlements_in']:>6}")
    out.append(f"  bank credits in               {s['bank_credits_in']:>6}")
    out.append(f"  candidate pairs surviving     {s['candidates']:>6}")
    out.append(f"  contested rows (declined)     {s['contested_rows']:>6}")
    out.append("")
    out.append(f"  settlements newly tied        {s['settlements_tied']:>6}"
               f" / {s['settlements_in']}")
    out.append(f"  settlements still unmatched   {s['settlements_still_unmatched']:>6}")
    out.append(f"  chains newly matched          {s['matches']:>6}")
    out.append("")
    out.append("  confidence of new matches")
    for confidence, n in sorted(result.confidence_histogram().items(), reverse=True):
        out.append(f"    {confidence:.1f}                          {n:>6}")
    out.append("")
    out.append("  settlements passed through, by reason")
    for reason, n in sorted(
        result.decline_breakdown().get("settlements", {}).items(), key=lambda kv: -kv[1]
    ):
        out.append(f"    {reason:<32} {n:>5}")
    out.append("-" * 62)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tier 2 -- tolerant matching.")
    ap.add_argument("--data", default="data/seed42", help="dataset directory")
    ap.add_argument("--rates", default="config/rates.yaml")
    ap.add_argument("--window", type=int, default=TIER2_POSTING_WINDOW_DAYS)
    args = ap.parse_args(argv)

    ledgers = load(args.data)
    tier1 = tier1_exact.run(ledgers, rates_path=args.rates)
    tier2 = run(ledgers, tier1, rates_path=args.rates, posting_window_days=args.window)
    print(format_funnel(tier1, tier2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
