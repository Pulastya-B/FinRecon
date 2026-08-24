#!/usr/bin/env python3
"""
Tier 3 -- settlement arithmetic, forward.

Tiers 1 and 2 cross the bank boundary by finding a reference: a UTR that
survived, or a narration that still looks like a payout. This tier does not
look for a reference at all. It rebuilds what the payout *should* have been
from the payment ledger, and then looks for a bank credit of that size.

That is a different kind of evidence, and on this data a stronger one. A UTR is
sixteen characters that either survived or did not. Reproducing a 26-payment
settlement equation to the paisa requires every fee, every tax, the batch
withholding, and every attributed reversal to land exactly right -- a
coincidence at that precision is not a coincidence.

Forward only
------------
This tier computes what the net should be and checks whether a credit of that
size exists. When there is a gap it records the gap and stops. Explaining a
variance -- deciding that 4,312 paise short is one unposted refund rather than
two rounding drifts and a fee change -- is attribution, and attribution is the
next tier's problem. Recording "short by 4,312" is a fact; naming its cause
here would be a guess wearing a number.

The rounding, which is the whole tier
--------------------------------------
    fee(p) = bps(amount_p, mdr_bps[method_p])   per payment, then summed
    tax(p) = bps(fee_p, tax_on_fee_bps)         per payment, then summed
    W(B)   = bps(sum_of_gross, withholding_bps) ONCE, on the batch total

Withholding is computed once on the batch gross and NOT per payment. This is
not a stylistic preference. Rounding 35 times instead of once drifts by up to a
few paise, and measured on seed 42 the per-payment form reproduces 1 of 46
batches while the batch-level form reproduces 46 of 46. A tier that got this
wrong would need a tolerance band wider than the 1-2 paise discrepancies it
exists to detect -- it would be unable to see the very thing it is for.

Fees and taxes go the other way: those genuinely are per-payment quantities,
because the MDR rate depends on the payment method, and a blended rate is not
recoverable from the batch gross alone.

Timing is not an exception
--------------------------
A payout with no matching credit is only missing if the window it was due in
has closed. Inside the window it is in transit, which is TIMING_PENDING and
belongs in nobody's queue. Conflating the two floods the exception queue every
Monday with Friday's in-flight payouts, and an operator who learns the queue is
mostly noise stops reading the queue.

Run:
    python -m finrecon.tier3_settlement --data data/seed42
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Mapping

import yaml

from .bizcal import BusinessCalendar
from .linking import pair_by_utr, pair_zero_payouts
from .money import apply_bps
from .normalize import LedgerEntry, NormalizedLedgers, load
from .tier1_exact import Decline, Tier1Result, order_side_gates
from . import tier1_exact

# Batch-level verdicts. These ARE the schema's exception codes -- unlike Tiers
# 1 and 2, which only ever decline, this tier can say what is wrong, because
# having reproduced the expected payout it knows the difference between "I
# could not find it" and "it is not there".
OUTCOME_MATCHED = "MATCHED"
OUTCOME_AMBIGUOUS = "AMBIGUOUS_MULTI_CANDIDATE"
OUTCOME_TIMING_PENDING = "TIMING_PENDING"
OUTCOME_MISSING_IN_BANK = "MISSING_IN_BANK"
# The payout ARRIVED, and arrived light. Distinct from MISSING_IN_BANK
# because the operator action is completely different: explain a
# shortfall against a credit you can see, versus chase a payout that
# never landed. Conflating them sends someone hunting for money that is
# already in the statement.
OUTCOME_AMOUNT_VARIANCE = "AMOUNT_VARIANCE_UNEXPLAINED"

# The band the tier is built to see through. Deliberately narrow: rounding
# drift is 1-2 paise, and the whole point of computing withholding once on the
# batch is that the band can stay this tight. Widening it would start absorbing
# the small real variances -- an unposted fee change, a partial refund -- that
# Tier 4 needs to attribute.
AMOUNT_TOLERANCE_PAISE = 2

# Same clearing-day window as Tier 1. This tier relaxes the *reference*, not
# the timing, and stacking two relaxations at once is how a tolerant tier stops
# being able to say which relaxation earned the match.
POSTING_WINDOW_DAYS = 3

# Reproducing the full equation exactly is stronger evidence than reproducing
# it within a band, so the two are not worth the same. Neither is a tuned
# number: they say which of the two possible outcomes of the arithmetic check
# actually happened.
CONF_EXACT_EQUATION = 0.95
CONF_WITHIN_DRIFT_BAND = 0.90

NO_BANK_CREDIT_MATCHING_COMPUTED_NET = "NO_BANK_CREDIT_MATCHING_COMPUTED_NET"


class SelfCheckFailure(AssertionError):
    """The recomputed settlement equation disagreed with the gateway's own net.

    Raised loudly and never downgraded to a warning. eval/validate.py has
    already proved the CSVs balance against themselves, so a disagreement here
    is this module's arithmetic, not the data -- and every match this tier
    makes rests on that arithmetic being right. Matching on a broken equation
    would produce confident, wrong, and entirely plausible results.
    """


# --------------------------------------------------------------------------
# Batch
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Batch:
    """One settlement cycle, with every derived quantity RECOMPUTED.

    `reported_*` are what the gateway said. The un-prefixed fields are what the
    payment ledger implies. Keeping both is the point: the self-check compares
    them, and a variance is only meaningful if the expectation was derived
    independently of the thing it is being compared against.
    """

    settlement_id: str
    capture_date: date
    settled_on: date
    n_payments: int

    gross_paise: int
    fee_paise: int
    tax_paise: int
    withholding_paise: int
    refund_paise: int
    chargeback_paise: int
    carry_forward_in_paise: int

    reported_net_paise: int
    reported: Mapping[str, int]

    @property
    def raw_paise(self) -> int:
        """The payout before it is floored at zero.

        carry_forward_in is DEDUCTED: it is a shortfall inherited from the
        previous cycle, a debt the merchant owes. An earlier version of this
        equation had the sign inverted and paid the merchant their own debt --
        a cycle inheriting a shortfall settled for more than its gross. See
        FAILURES.md, 2026-08-21.
        """
        return (
            self.gross_paise
            - self.fee_paise
            - self.tax_paise
            - self.withholding_paise
            - self.refund_paise
            - self.chargeback_paise
            - self.carry_forward_in_paise
        )


def compute_expected_net(batch: Batch) -> int:
    """What this cycle should have paid out, in paise. Floored at zero."""
    return max(batch.raw_paise, 0)


def compute_carry_forward_out(batch: Batch) -> int:
    """The shortfall this cycle could not cover and pushes to the next.

    ADDED BACK when reconstructing the reported row, because the payout was
    floored at zero rather than going negative -- the identity
    net == raw + carry_out is what makes the gateway's row balance.
    """
    return max(-batch.raw_paise, 0)


def build_batches(
    ledgers: NormalizedLedgers, rates: Mapping[str, object]
) -> list[Batch]:
    """Rebuild every settlement cycle from the payment ledger.

    Nothing derived is read from gateway_settlements.csv except carry_forward_in
    and the reported figures used for the self-check. carry_forward_in is a
    stated input to the cycle rather than a derived quantity -- it is the
    previous cycle's shortfall, and the settlement report is where a merchant
    learns of it.
    """
    mdr_bps: Mapping[str, int] = rates["mdr_bps"]          # type: ignore[assignment]
    tax_bps: int = int(rates["tax_on_fee_bps"])            # type: ignore[arg-type]
    withholding = rates.get("withholding", {}) or {}
    wh_enabled = bool(withholding.get("enabled", False))   # type: ignore[union-attr]
    wh_bps = int(withholding.get("rate_bps", 0))           # type: ignore[union-attr]

    payments_by_settlement: dict[str, list[LedgerEntry]] = defaultdict(list)
    for payment in ledgers.payments:
        sid = payment.references["settlement_id"]
        if sid:
            payments_by_settlement[sid].append(payment)

    # Reversals land in the cycle that PROCESSED them, which may be weeks after
    # the capture they reverse. Attribution is taken from the reversal's own
    # settlement_id and never inferred from the payment it points at -- that
    # cross-period gap is the problem, not a nuisance to normalise away.
    refunds_by_settlement: dict[str, int] = defaultdict(int)
    for refund in ledgers.refunds:
        sid = refund.references["settlement_id"]
        if sid:
            refunds_by_settlement[sid] += refund.amount_paise

    chargebacks_by_settlement: dict[str, int] = defaultdict(int)
    for chargeback in ledgers.chargebacks:
        sid = chargeback.references["settlement_id"]
        if sid:
            chargebacks_by_settlement[sid] += chargeback.amount_paise

    batches = []
    for settlement in ledgers.settlements:
        sid = settlement.entry_id
        payments = payments_by_settlement.get(sid, [])

        gross = sum(p.amount_paise for p in payments)

        # Per payment, because the MDR rate depends on the method -- a blended
        # rate cannot be recovered from the batch gross.
        fee = sum(
            apply_bps(p.amount_paise, mdr_bps[p.raw_row["method"]]) for p in payments
        )
        tax = sum(
            apply_bps(apply_bps(p.amount_paise, mdr_bps[p.raw_row["method"]]), tax_bps)
            for p in payments
        )

        # ONCE, on the batch total. See the module docstring: per-payment
        # withholding reproduces 1 of 46 batches, batch-level reproduces 46.
        wh = apply_bps(gross, wh_bps) if wh_enabled else 0

        amounts = settlement.amounts
        batches.append(
            Batch(
                settlement_id=sid,
                capture_date=datetime.strptime(
                    settlement.raw_row["capture_date"], "%Y-%m-%d"
                ).date(),
                settled_on=settlement.event_date,
                n_payments=len(payments),
                gross_paise=gross,
                fee_paise=fee,
                tax_paise=tax,
                withholding_paise=wh,
                refund_paise=refunds_by_settlement.get(sid, 0),
                chargeback_paise=chargebacks_by_settlement.get(sid, 0),
                carry_forward_in_paise=amounts["carry_forward_in_paise"],
                reported_net_paise=amounts["net_paise"],
                reported={
                    "gross_paise": amounts["gross_paise"],
                    "fee_paise": amounts["fee_paise"],
                    "tax_paise": amounts["tax_paise"],
                    "withholding_paise": amounts["withholding_paise"],
                    "refund_paise": amounts["refund_paise"],
                    "chargeback_paise": amounts["chargeback_paise"],
                    "carry_forward_out_paise": amounts["carry_forward_out_paise"],
                },
            )
        )
    return batches


# --------------------------------------------------------------------------
# Self-check
# --------------------------------------------------------------------------
@dataclass
class SelfCheckResult:
    total: int = 0
    exact: int = 0
    mismatches: list[tuple[str, int, int]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.total > 0 and self.exact == self.total


def self_check(batches: list[Batch], strict: bool = True) -> SelfCheckResult:
    """Recomputed net must equal the gateway's reported net, for every cycle.

    Runs before any matching. Every match this tier makes is an assertion about
    an amount this arithmetic produced, so if the arithmetic is wrong the
    matches are confidently wrong -- the worst possible failure here, and one
    no downstream metric would flag as arithmetic rather than as matching.
    """
    result = SelfCheckResult(total=len(batches))
    for batch in batches:
        expected = compute_expected_net(batch)
        if expected == batch.reported_net_paise:
            result.exact += 1
        else:
            result.mismatches.append(
                (batch.settlement_id, expected, batch.reported_net_paise)
            )

    if strict and not result.passed:
        detail = ", ".join(
            f"{sid}: computed {c} != reported {r}"
            for sid, c, r in result.mismatches[:5]
        )
        raise SelfCheckFailure(
            f"settlement equation self-check failed on "
            f"{len(result.mismatches)}/{result.total} batches -- {detail}"
        )
    return result


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class BatchOutcome:
    settlement_id: str
    outcome: str
    expected_net_paise: int
    bank_row_id: str | None = None
    actual_credit_paise: int | None = None
    variance_paise: int = 0
    confidence: float = 0.0
    candidate_ids: tuple[str, ...] = ()
    had_utr: bool = False
    # Which route proved the payout arrived: "utr" (a reference identified it),
    # "amount" (the computed net matched a credit), or "attribution" (the gap
    # from expected is exactly explained by one ledger item). Recorded so the
    # audit trail says HOW arrival was established, not merely that it was.
    identified_by: str | None = None
    # The attribution that identified it, where that was the route.
    attribution: object | None = None

    @property
    def matched(self) -> bool:
        return self.outcome == OUTCOME_MATCHED

    @property
    def claims_row(self) -> bool:
        """Does this outcome take a bank row out of circulation?

        A MATCHED batch does. So does an AMOUNT_VARIANCE batch that positively
        identified its credit -- it has proved that row is its payout, and two
        batches must not both prove that about one row.
        """
        return bool(self.bank_row_id) and self.outcome in (
            OUTCOME_MATCHED, OUTCOME_AMOUNT_VARIANCE
        )


# The band an attribution must reach before it is allowed to prove a payout
# arrived. STRONG only, never CIRCUMSTANTIAL: this is a NEW way for a credit to
# be claimed, so it is exactly where a wrong match would enter, and "these two
# numbers differ by an amount some ledger item happens to equal" is not enough
# to tell an operator the money is in the bank.
IDENTIFY_MIN_BAND = "STRONG"


def _identify_by_attribution(
    batch: Batch,
    expected: int,
    credits: list[LedgerEntry],
    ledgers: NormalizedLedgers,
    withholding_bps: int,
    tolerance_paise: int,
) -> list[tuple[LedgerEntry, object]]:
    """Credits whose gap from `expected` is exactly explained by one item.

    A UTR is one way to identify a payout, not the only one. A credit that sits
    1,237,800 paise below the computed net, where a single chargeback of
    1,237,800 paise accounts for the difference, is identified by that
    explanation -- arguably better than a string match, because it accounts for
    the discrepancy rather than merely coinciding with an id.

    That route matters because a batch can lose BOTH: a shortfall defect puts
    the credit outside every amount band, and a clipped narration destroys the
    UTR. With both closed the payout was reported as never having arrived while
    sitting in the statement.

    The attribution search is imported here rather than at module scope because
    tier3_attribution imports this module; a deferred import keeps the one
    search in one place instead of growing a second copy.
    """
    from .evidence import STRONG
    from .tier3_attribution import attribute_variance, build_pools, OUTCOME_ATTRIBUTED

    if not credits:
        return []

    pools = build_pools(ledgers, batch.settlement_id, withholding_bps)
    accepted: list[tuple[LedgerEntry, object]] = []
    for credit in credits:
        actual = credit.amounts["credit_paise"]
        attribution = attribute_variance(
            batch.settlement_id, credit.entry_id, expected, actual,
            pools, tolerance_paise,
        )
        if attribution.outcome != OUTCOME_ATTRIBUTED:
            continue
        evidence = attribution.evidence
        if evidence is None or evidence.strength != STRONG:
            continue
        accepted.append((credit, attribution))
    return accepted


def match_batch_to_bank(
    batch: Batch,
    bank_credits: list[LedgerEntry],
    cal: BusinessCalendar,
    as_of: date,
    tolerance_paise: int = AMOUNT_TOLERANCE_PAISE,
    window_days: int = POSTING_WINDOW_DAYS,
    utr_pair: LedgerEntry | None = None,
    zero_pair: LedgerEntry | None = None,
    ledgers: NormalizedLedgers | None = None,
    withholding_bps: int = 0,
) -> BatchOutcome:
    """Find the one bank credit this batch's computed net explains, if there is one."""
    expected = compute_expected_net(batch)

    lo = batch.settled_on
    hi = cal.add_business_days(lo, window_days)

    candidates = [
        credit
        for credit in bank_credits
        # Lower bound is the payout date: money does not arrive before it was
        # sent, and a credit that predates its own settlement is evidence
        # against the pairing rather than slack to absorb.
        if lo <= credit.event_date <= hi
        and abs(credit.amounts["credit_paise"] - expected) <= tolerance_paise
    ]

    if len(candidates) > 1:
        # Two credits of the same size in the same window. Either could be this
        # payout; choosing is a coin flip that lands in the books.
        return BatchOutcome(
            settlement_id=batch.settlement_id,
            outcome=OUTCOME_AMBIGUOUS,
            expected_net_paise=expected,
            candidate_ids=tuple(c.entry_id for c in candidates),
        )

    # A cycle that carries forward owes nothing, and a payout of nothing is
    # still a payout: the statement carries a row with credit 0 and debit 0.
    # It never reaches `candidates` because Tier 0 types a zero row as a debit,
    # so without this the correct answer -- paid in full, which was nothing --
    # was reported as money that never arrived.
    if expected == 0 and zero_pair is not None:
        return BatchOutcome(
            settlement_id=batch.settlement_id,
            outcome=OUTCOME_MATCHED,
            expected_net_paise=0,
            bank_row_id=zero_pair.entry_id,
            actual_credit_paise=0,
            variance_paise=0,
            confidence=CONF_EXACT_EQUATION,
            candidate_ids=(zero_pair.entry_id,),
            had_utr=zero_pair.references["utr"] is not None,
            identified_by="utr",
        )

    if not candidates:
        # Before declaring money absent, check whether it is merely SHORT. A
        # credit carrying this batch's exact UTR is the payout, whatever the
        # amount says, and "arrived light" is a different finding from "never
        # arrived" -- different verdict, different queue, different action.
        if utr_pair is not None:
            actual = utr_pair.amounts["credit_paise"]
            return BatchOutcome(
                settlement_id=batch.settlement_id,
                outcome=OUTCOME_AMOUNT_VARIANCE,
                expected_net_paise=expected,
                bank_row_id=utr_pair.entry_id,
                actual_credit_paise=actual,
                variance_paise=actual - expected,
                # No confidence: this is not a match and must never be counted
                # as one. The reference identifies the payout; the gap is still
                # unexplained, and explaining it is attribution's job.
                confidence=0.0,
                candidate_ids=(utr_pair.entry_id,),
                had_utr=True,
                identified_by="utr",
            )

        # Neither the amount nor a reference could identify this payout. Before
        # declaring the money never came, try to identify it by EXPLAINING the
        # gap: a credit whose distance from the computed net is exactly one
        # ledger item is that payout, short by that item.
        if ledgers is not None and expected > 0:
            in_window = [
                c for c in bank_credits if lo <= c.event_date <= hi
            ]
            identified = _identify_by_attribution(
                batch, expected, in_window, ledgers, withholding_bps,
                tolerance_paise,
            )
            if len(identified) > 1:
                # Two credits each fully explained. Either could be the payout,
                # and claiming arrival for the wrong one is worse than the bug
                # this route exists to fix.
                return BatchOutcome(
                    settlement_id=batch.settlement_id,
                    outcome=OUTCOME_AMBIGUOUS,
                    expected_net_paise=expected,
                    candidate_ids=tuple(c.entry_id for c, _a in identified),
                )
            if len(identified) == 1:
                credit, attribution = identified[0]
                actual = credit.amounts["credit_paise"]
                return BatchOutcome(
                    settlement_id=batch.settlement_id,
                    outcome=OUTCOME_AMOUNT_VARIANCE,
                    expected_net_paise=expected,
                    bank_row_id=credit.entry_id,
                    actual_credit_paise=actual,
                    variance_paise=actual - expected,
                    # Still not a match: the payout arrived light, and the
                    # shortfall is a finding for a human even though its cause
                    # is now named.
                    confidence=0.0,
                    candidate_ids=(credit.entry_id,),
                    had_utr=credit.references["utr"] is not None,
                    identified_by="attribution",
                    attribution=attribution,
                )

        if expected == 0:
            # Owed nothing, and no zero row could be told apart from another
            # cycle's. Chasing the gateway for money it never owed is the
            # wrong instruction, so this is ambiguity, not absence.
            return BatchOutcome(
                settlement_id=batch.settlement_id,
                outcome=OUTCOME_AMBIGUOUS,
                expected_net_paise=0,
            )

        # The distinction that keeps the queue readable. Inside the window the
        # payout is in transit; past it, it is genuinely absent.
        pending = as_of <= hi
        return BatchOutcome(
            settlement_id=batch.settlement_id,
            outcome=OUTCOME_TIMING_PENDING if pending else OUTCOME_MISSING_IN_BANK,
            expected_net_paise=expected,
        )

    winner = candidates[0]
    actual = winner.amounts["credit_paise"]
    variance = actual - expected
    return BatchOutcome(
        settlement_id=batch.settlement_id,
        outcome=OUTCOME_MATCHED,
        expected_net_paise=expected,
        bank_row_id=winner.entry_id,
        actual_credit_paise=actual,
        variance_paise=variance,
        confidence=CONF_EXACT_EQUATION if variance == 0 else CONF_WITHIN_DRIFT_BAND,
        candidate_ids=(winner.entry_id,),
        # Recorded, never used to match. It is the measurement of how much of
        # the bank boundary this tier crossed with no reference at all.
        had_utr=winner.references["utr"] is not None,
        identified_by="amount",
    )


# --------------------------------------------------------------------------
# Tier 3
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SettlementMatch:
    order_id: str
    payment_id: str
    settlement_id: str
    bank_row_id: str
    rules_fired: tuple[str, ...]
    evidence: Mapping[str, str]
    confidence: float


@dataclass
class Tier3Result:
    matches: list[SettlementMatch] = field(default_factory=list)
    settlement_to_bank: dict[str, str] = field(default_factory=dict)

    # settlement_id -> the batch-level verdict, including the ones that are
    # exceptions rather than matches.
    batch_outcomes: dict[str, BatchOutcome] = field(default_factory=dict)

    # order_id -> exception code, for chains whose only problem is the batch.
    chain_exceptions: dict[str, str] = field(default_factory=dict)

    self_check_result: SelfCheckResult = field(default_factory=SelfCheckResult)
    declines: dict[str, Decline] = field(default_factory=dict)
    unmatched_orders: list[LedgerEntry] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def variances(self) -> list[BatchOutcome]:
        """Every batch whose credit did not equal the computed net.

        Includes AMOUNT_VARIANCE batches, not just matched ones: a batch short
        by a whole refund is the variance most worth explaining, and it is
        precisely the one no tier will ever MATCH.
        """
        return [
            o for o in self.batch_outcomes.values()
            if o.variance_paise and o.bank_row_id
        ]

    def outcome_counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for outcome in self.batch_outcomes.values():
            counts[outcome.outcome] += 1
        return dict(counts)


def run(
    ledgers: NormalizedLedgers,
    tier1: Tier1Result,
    already_tied: Mapping[str, str] | None = None,
    rates_path: str | Path = "config/rates.yaml",
    as_of: date | None = None,
    tolerance_paise: int = AMOUNT_TOLERANCE_PAISE,
    window_days: int = POSTING_WINDOW_DAYS,
) -> Tier3Result:
    """Rebuild every cycle, self-check, then match Tier 1's unresolved batches.

    `already_tied` lets the pipeline hand over settlements another tier has
    already claimed, so two tiers never match the same batch. Left empty, this
    tier processes everything Tier 1 declined -- which is what makes a clean
    Tier-1-versus-Tier-3 measurement possible.
    """
    result = Tier3Result()
    already_tied = dict(already_tied or {})

    rates = yaml.safe_load(Path(rates_path).read_text())
    holidays = [
        datetime.strptime(h, "%Y-%m-%d").date() if isinstance(h, str) else h
        for h in rates.get("holidays_2026", [])
    ]
    cal = BusinessCalendar(holidays)

    # -- step 1 & 2: rebuild every cycle, then prove the arithmetic ----------
    # The self-check covers ALL batches, not just the ones being matched. An
    # equation that is wrong on a batch Tier 1 already resolved is still wrong.
    batches = build_batches(ledgers, rates)
    result.self_check_result = self_check(batches, strict=True)

    # Needed by the attribution identification route, which computes a
    # payment's net contribution the same way build_batches does.
    withholding_cfg = rates.get("withholding", {}) or {}
    wh_bps = (
        int(withholding_cfg.get("rate_bps", 0))
        if withholding_cfg.get("enabled") else 0
    )

    # As-of defaults to the last line of the bank statement rather than the
    # wall clock: you reconcile a statement as of its own closing date, and a
    # report whose TIMING_PENDING count changes because it was run on a
    # different afternoon is not a report anyone can check.
    if as_of is None:
        as_of = max((b.event_date for b in ledgers.bank), default=date.today())

    # -- step 3: match only what is still open -------------------------------
    resolved = set(tier1.settlement_to_bank) | set(already_tied)
    open_batches = [b for b in batches if b.settlement_id not in resolved]

    claimed_rows = set(tier1.settlement_to_bank.values()) | set(already_tied.values())
    available_credits = [
        c
        for c in ledgers.bank
        if c.entry_type == "bank_credit" and c.entry_id not in claimed_rows
    ]

    # Reference pairings for the batches still open, computed ONCE and shared.
    # Tier 3 needs them to tell "arrived light" from "never arrived";
    # attribution needs the same pairs to have a variance to explain. They live
    # in linking/ so neither tier owns the fact -- see that module's docstring
    # for the three bugs that came from a tier holding one privately.
    by_id = {b.entry_id: b for b in ledgers.bank}
    utr_pairs = pair_by_utr(
        ledgers, [b.settlement_id for b in open_batches], claimed_rows
    )
    # Cycles that owed nothing, paired to the rows that paid nothing.
    zero_pairs = pair_zero_payouts(
        ledgers,
        [b.settlement_id for b in open_batches if compute_expected_net(b) == 0],
        claimed_rows,
    )

    proposed = {
        batch.settlement_id: match_batch_to_bank(
            batch, available_credits, cal, as_of, tolerance_paise, window_days,
            utr_pair=by_id.get(utr_pairs.get(batch.settlement_id, "")),
            zero_pair=by_id.get(zero_pairs.get(batch.settlement_id, "")),
            ledgers=ledgers,
            withholding_bps=wh_bps,
        )
        for batch in open_batches
    }

    # Uniqueness has to be checked in BOTH directions, and one batch seeing a
    # single candidate is only the first direction. Batches have different
    # posting windows, so two cycles can each see exactly one candidate and it
    # can be the same credit -- each locally unambiguous, jointly contradictory.
    # A credit is one payout; whichever batch happened to be iterated first
    # would otherwise take it, making the result depend on iteration order.
    # Every outcome that POSITIVELY IDENTIFIED a row claims it -- matched, or
    # AMOUNT_VARIANCE where a reference or an explanation proved which payout
    # the credit is. Two batches must not both prove that about one row.
    claims = Counter(
        o.bank_row_id for o in proposed.values() if o.claims_row
    )
    contested = {row_id for row_id, n in claims.items() if n > 1}

    for sid, outcome in proposed.items():
        if outcome.claims_row and outcome.bank_row_id in contested:
            # Demoted to ambiguity rather than resolved. Deciding which cycle
            # a shared credit belongs to needs the batch arithmetic of
            # attribution, which this tier deliberately does not do.
            outcome = replace(
                outcome,
                outcome=OUTCOME_AMBIGUOUS,
                bank_row_id=None,
                actual_credit_paise=None,
                variance_paise=0,
                confidence=0.0,
            )
        result.batch_outcomes[sid] = outcome
        if outcome.matched and outcome.bank_row_id:
            result.settlement_to_bank[sid] = outcome.bank_row_id
        else:
            result.declines[sid] = Decline("settlements", outcome.outcome)

    # Now a genuine invariant rather than a hope: the contention pass above
    # removes every duplicate claim, so this can only fire if that pass is
    # broken. Kept loud for exactly that reason.
    claimed = list(result.settlement_to_bank.values())
    if len(claimed) != len(set(claimed)):
        raise SelfCheckFailure(f"a bank row was claimed by two batches: {claimed}")

    # -- step 5: chains unlocked, and chains now explained -------------------
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

    for order in tier1.unmatched_orders:
        payment, reason = order_side_gates(
            order, payments_by_order, chargebacks_by_payment
        )
        if payment is None:
            # The chain has a problem this tier cannot see past. Its
            # classification belongs to whichever tier owns that problem, so it
            # is left undecided rather than labelled with the batch's verdict.
            result.declines[order.entry_id] = Decline("orders", reason or "UNKNOWN")
            result.unmatched_orders.append(order)
            continue

        sid = payment.references["settlement_id"] or ""
        outcome = result.batch_outcomes.get(sid)
        if outcome is None:
            result.unmatched_orders.append(order)
            continue

        if outcome.matched and outcome.bank_row_id:
            result.matches.append(
                SettlementMatch(
                    order_id=order.entry_id,
                    payment_id=payment.entry_id,
                    settlement_id=sid,
                    bank_row_id=outcome.bank_row_id,
                    rules_fired=(
                        tier1_exact.RULE_A_ORDER_ID,
                        tier1_exact.RULE_B_SETTLEMENT_ID,
                        "G_COMPUTED_NET_AMOUNT_DATE",
                        tier1_exact.RULE_D_CHARGEBACK_CLEAR,
                    ),
                    evidence={
                        "expected_net_paise": str(outcome.expected_net_paise),
                        "actual_credit_paise": str(outcome.actual_credit_paise),
                        "variance_paise": str(outcome.variance_paise),
                        "batch_payments": str(
                            next(
                                b.n_payments
                                for b in batches
                                if b.settlement_id == sid
                            )
                        ),
                        "matched_without_utr": str(not outcome.had_utr),
                    },
                    confidence=outcome.confidence,
                )
            )
        else:
            # The chain is intact up to the bank boundary, and the batch-level
            # verdict is therefore the chain's verdict too. This is the first
            # tier that can say so: it knows the payout is absent rather than
            # merely unfound, because it reproduced what was owed.
            result.chain_exceptions[order.entry_id] = outcome.outcome
            result.unmatched_orders.append(order)

    matched_no_utr = sum(
        1 for o in result.batch_outcomes.values() if o.matched and not o.had_utr
    )
    counts = result.outcome_counts()
    result.stats.update(
        amount_variance=counts.get(OUTCOME_AMOUNT_VARIANCE, 0),
        identified_by_utr=sum(
            1 for o in result.batch_outcomes.values() if o.identified_by == "utr"
        ),
        identified_by_amount=sum(
            1 for o in result.batch_outcomes.values() if o.identified_by == "amount"
        ),
        identified_by_attribution=sum(
            1 for o in result.batch_outcomes.values()
            if o.identified_by == "attribution"
        ),
        batches_total=len(batches),
        batches_open=len(open_batches),
        batches_matched=len(result.settlement_to_bank),
        matched_without_utr=matched_no_utr,
        ambiguous=counts.get(OUTCOME_AMBIGUOUS, 0),
        timing_pending=counts.get(OUTCOME_TIMING_PENDING, 0),
        missing_in_bank=counts.get(OUTCOME_MISSING_IN_BANK, 0),
        variances=len(result.variances()),
        matches=len(result.matches),
        chain_exceptions=len(result.chain_exceptions),
        self_check_exact=result.self_check_result.exact,
        self_check_total=result.self_check_result.total,
    )
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def format_report(result: Tier3Result, as_of: date) -> str:
    s = result.stats
    out = ["", "tier 3 -- settlement arithmetic (forward)", "-" * 62]
    check = result.self_check_result
    mark = "PASS" if check.passed else "FAIL"
    out.append(f"  [{mark}] self-check: computed net == reported net   "
               f"{check.exact}/{check.total}")
    out.append(f"  as-of date (last bank line)   {as_of}")
    out.append("")
    out.append(f"  batches total                 {s['batches_total']:>6}")
    out.append(f"  batches still open            {s['batches_open']:>6}")
    out.append(f"  batches matched               {s['batches_matched']:>6}")
    out.append(f"    ...with NO usable UTR       {s['matched_without_utr']:>6}"
               "   (crossed on arithmetic alone)")
    out.append("")
    out.append(f"  AMBIGUOUS_MULTI_CANDIDATE     {s['ambiguous']:>6}")
    out.append(f"  TIMING_PENDING                {s['timing_pending']:>6}"
               "   (in transit, not an exception)")
    out.append(f"  MISSING_IN_BANK               {s['missing_in_bank']:>6}"
               "   (never arrived)")
    out.append(f"  AMOUNT_VARIANCE               {s['amount_variance']:>6}"
               "   (arrived light -> attribution)")
    out.append("")
    out.append(f"  chains newly matched          {s['matches']:>6}")
    out.append(f"  chains given an exception     {s['chain_exceptions']:>6}")
    out.append("")
    variances = result.variances()
    out.append(f"  batches with non-zero variance ({len(variances)})")
    if not variances:
        out.append("    none -- every matched batch reproduced to the paisa")
    for outcome in sorted(variances, key=lambda o: o.settlement_id):
        sign = "short" if outcome.variance_paise < 0 else "over"
        out.append(
            f"    {outcome.settlement_id}  expected {outcome.expected_net_paise:>12}"
            f"  actual {outcome.actual_credit_paise:>12}"
            f"  variance {outcome.variance_paise:+d} paise ({sign})"
        )
    out.append("-" * 62)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tier 3 -- settlement arithmetic.")
    ap.add_argument("--data", default="data/seed42", help="dataset directory")
    ap.add_argument("--rates", default="config/rates.yaml")
    ap.add_argument("--as-of", default=None,
                    help="reconcile as of this date (default: last bank line)")
    args = ap.parse_args(argv)

    ledgers = load(args.data)
    tier1 = tier1_exact.run(ledgers, rates_path=args.rates)
    as_of = (
        datetime.strptime(args.as_of, "%Y-%m-%d").date()
        if args.as_of
        else max((b.event_date for b in ledgers.bank), default=date.today())
    )
    result = run(ledgers, tier1, rates_path=args.rates, as_of=as_of)
    print(format_report(result, as_of))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
