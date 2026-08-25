#!/usr/bin/env python3
"""
Tier 3b -- variance attribution and membership inference.

Two independent parts, sharing only their arithmetic and their refusal to guess.

    Part A  the batch is known, the amount is wrong -> what explains the gap
    Part B  the amount is known, the batch is not   -> which payments made it

Sign convention, stated once and asserted
------------------------------------------
    delta = expected_net - actual_credit

This is the NEGATION of `BatchOutcome.variance_paise` in tier3_settlement,
which stores `actual - expected`. Two modules on opposite conventions is how
the carry-forward sign bug happened, so the conversion is done in exactly one
place (`variance_to_delta`) and asserted in the probes rather than left to
whoever reads it next.

    delta > 0   the bank paid LESS than computed -- something was deducted
                that we did not attribute, or an item never arrived
    delta < 0   the bank paid MORE than computed -- an overpayment, which is
                every bit as much an exception as a shortfall

Because both signs are real, the search is on MAGNITUDE: an item of size
|delta| explains the gap either way, and the direction is recorded as a label
rather than baked into the comparison. Written that way, `delta < 0` cannot
silently fall through to "no candidates found" -- which is what a search
hard-coded to `item - delta` would do.

Why L0 stops the search
-----------------------
A gap of one or two paise has no item behind it. It is the gateway and the
bank rounding sub-paise amounts differently, and hunting for a payment worth
two paise finds nothing and then reports AMOUNT_VARIANCE_UNEXPLAINED on a
fully understood non-problem. That is noise in the exception queue, and an
operator who learns the queue contains understood non-problems stops reading
the queue. So L0 closes; it does not attribute.

Which is also why L0 is reported separately from L1-L4 everywhere below.
Closing a rounding drift is not the same achievement as identifying an
unposted refund, and adding them together would flatter the engine.

Run:
    python -m finrecon.tier3_attribution --data data/seed42
"""

from __future__ import annotations

import argparse
import bisect
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import yaml

from .bizcal import BusinessCalendar
from .evidence import Evidence, assess, strength as evidence_strength, REFUSE
# Re-exported so existing callers keep working; the fact itself lives in
# linking/ because Tier 3 needs it too. See that module for why.
from .linking import pair_by_utr  # noqa: F401
from .money import apply_bps
from .normalize import LedgerEntry, NormalizedLedgers, load
from . import tier1_exact, tier2_tolerant, tier3_settlement

# The band every acceptance test uses. Matched to Tier 3's: the per-payment
# withholding approximation in `net_contribution` is off by at most a paisa
# because withholding truly rounds on the batch total, and that paisa has to
# fit inside the band along with genuine sub-paise drift.
TOLERANCE_PAISE = 2

# Search levels, in the order they are tried. The order is not arbitrary --
# it runs from "no item at all" through single items to combinations, so the
# simplest sufficient explanation wins and a pair is never proposed for
# something one refund already accounts for.
L0_ROUNDING_DRIFT = "L0"
L1_MISSING_PAYMENT = "L1"
L2_REFUND_ELSEWHERE = "L2"
L3_UNPOSTED_CHARGEBACK = "L3"
L4_ITEM_PAIR = "L4"
L5_UNEXPLAINED = "L5"

LEVEL_NAMES = {
    L0_ROUNDING_DRIFT: "rounding drift (closed, not attributed)",
    L1_MISSING_PAYMENT: "payment missing from the batch",
    L2_REFUND_ELSEWHERE: "refund attributed to another cycle",
    L3_UNPOSTED_CHARGEBACK: "unposted chargeback",
    L4_ITEM_PAIR: "two items summing to the gap",
    L5_UNEXPLAINED: "unexplained",
}

OUTCOME_ROUNDING_DRIFT_ACCEPTED = "ROUNDING_DRIFT_ACCEPTED"
OUTCOME_ATTRIBUTED = "ATTRIBUTED"
OUTCOME_AMBIGUOUS = "AMBIGUOUS_MULTI_CANDIDATE"
OUTCOME_UNEXPLAINED = "AMOUNT_VARIANCE_UNEXPLAINED"

# Part B verdicts.
OUTCOME_MEMBERSHIP_SOLVED = "MEMBERSHIP_SOLVED"
OUTCOME_NO_SUBSET = "NO_SUBSET_FOUND"
OUTCOME_UNKNOWN_CAP = "UNKNOWN_ITERATION_CAP_EXCEEDED"
OUTCOME_UNKNOWN_POOL_TOO_LARGE = "UNKNOWN_POOL_ABOVE_MEASURED_CEILING"

# Direction labels. The comparison is on magnitude; these record which way the
# money went, which is what an operator actually needs to see.
DIR_ABSENT = "absent_from_payout"    # delta > 0: the payout is short by this
DIR_EXTRA = "extra_in_payout"        # delta < 0: the payout included this

# Confidence. These say which level fired, not how sure a model is.
CONF_ROUNDING_CLOSED = 0.99   # understood completely; nothing to attribute
CONF_SINGLE_ITEM = 0.85       # one item of exactly the right size
CONF_ITEM_PAIR = 0.75         # two items -- more ways to be coincidentally right
CONF_MEMBERSHIP_SOLVED = 0.80
CONF_NONE = 0.0

# Part B: subsets are enumerated by brute force over halves, so the cost is
# 2^(n/2) per side. The cap is a promise that this returns rather than hangs;
# exceeding it is a real answer (UNKNOWN), not a failure to be retried.
DEFAULT_ITERATION_CAP = 500_000

# Slack for the meet-in-the-middle key. W(a) + W(b) and W(a + b) differ by at
# most 1 paisa because each is a half-up rounding of a linear function, so a
# margin of 4 cannot lose a solution. Every hit inside the widened window is
# then verified EXACTLY with the two-accumulator formula, so a margin that is
# too generous costs time and never correctness.
_KEY_MARGIN_PAISE = 4

# Part B has no pool-size constant any more. It refuses on the SAME rule as
# every other level: expected accidental fits >= 1, from evidence.py.
#
# The old constant was 20, read off eval/subset_reliability.py's table. The
# formula puts the boundary at 23 -- pool 23 expects 0.93 accidental fits, pool
# 24 expects 1.86 -- so the effective ceiling moves 20 -> 23. That is reported
# rather than forced back: the measurement and the estimate agree on the shape
# (pool 20 reliable, pool 40 hopeless) and disagree by three slots at the
# boundary, which is what a crude uniform-spread model should be expected to do.
#
# The gain is not the number. It is that one rule now governs the subset search
# and L1-L4 alike, in the same units, instead of a table for one and nothing
# for the others.


def variance_to_delta(variance_paise: int) -> int:
    """Convert tier3_settlement's `actual - expected` to this module's delta.

    One function, one place, so the inversion is impossible to get wrong twice.
    """
    return -variance_paise


# --------------------------------------------------------------------------
# Shared arithmetic
# --------------------------------------------------------------------------
def net_contribution(
    amount_paise: int, fee_paise: int, tax_paise: int, withholding_bps: int
) -> int:
    """What one payment adds to a batch's net payout, to within a paisa.

    Approximate, and knowingly so: withholding rounds ONCE on the batch gross,
    not per payment, so pulling a single payment's share of it out can be off
    by a paisa. That paisa is inside the tolerance band. This is the only place
    in the codebase where a per-payment withholding figure is legitimate, and
    it is legitimate only because the result is compared with a band -- summing
    these to rebuild a batch total is the 5-paise drift bug from Session 4.
    """
    return amount_paise - fee_paise - tax_paise - apply_bps(amount_paise, withholding_bps)


def subset_net(
    payments: Sequence[tuple[str, int, int, int]], withholding_bps: int
) -> tuple[int, int]:
    """Exact net payout for a subset, with withholding applied once.

    Returns (net_after_withholding, gross). TWO accumulators, deliberately:
    the running net-before-withholding and the running gross are carried
    separately, and withholding is taken on the summed gross at the end. There
    is no per-payment scalar that can be summed to get this right.
    """
    gross = 0
    net_before = 0
    for _pid, amount, fee, tax in payments:
        gross += amount
        net_before += amount - fee - tax
    return net_before - apply_bps(gross, withholding_bps), gross


# --------------------------------------------------------------------------
# Part A -- explain a variance
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Candidate:
    item_id: str
    item_kind: str          # payment | refund | chargeback | pair
    magnitude_paise: int
    direction: str
    residual_paise: int     # what remains unexplained if this is accepted
    components: tuple[str, ...] = ()
    # What the item's OWN record says its cycle is. When that names the batch
    # being explained, the gateway itself asserts the link and the match is far
    # more than numeric agreement -- see evidence.Evidence.declared_link.
    declared_settlement_id: str | None = None


@dataclass(frozen=True)
class Attribution:
    settlement_id: str
    bank_row_id: str
    expected_net_paise: int
    actual_credit_paise: int
    delta_paise: int
    level: str
    outcome: str
    candidates: tuple[Candidate, ...] = ()
    resolved: Candidate | None = None
    residual_paise: int = 0
    confidence: float = CONF_NONE
    # How likely this fit was an accident. Describes the attribution; never
    # decides a match.
    evidence: Evidence | None = None

    @property
    def is_attribution(self) -> bool:
        """True only for L1-L4. L0 closes a non-problem and is NOT an
        attribution success -- counting it as one would flatter the engine."""
        return self.outcome == OUTCOME_ATTRIBUTED


@dataclass(frozen=True)
class ItemPools:
    """Everything that could account for a gap in this batch."""

    # (item_id, magnitude_paise, declared_settlement_id)
    payments: tuple[tuple[str, int, str | None], ...] = ()
    refunds: tuple[tuple[str, int, str | None], ...] = ()
    chargebacks: tuple[tuple[str, int, str | None], ...] = ()

    def all_items(self) -> list[tuple[str, str, int, str | None]]:
        return (
            [(i, "payment", m, d) for i, m, d in self.payments]
            + [(i, "refund", m, d) for i, m, d in self.refunds]
            + [(i, "chargeback", m, d) for i, m, d in self.chargebacks]
        )

    def amounts_for(self, kind: str) -> list[int]:
        return [m for _i, k, m, _d in self.all_items() if k == kind]

    def all_amounts(self) -> list[int]:
        return [m for _i, _k, m, _d in self.all_items()]


def _direction(delta: int) -> str:
    return DIR_ABSENT if delta > 0 else DIR_EXTRA


def _single_item_candidates(
    items: Iterable[tuple[str, int, str | None]], kind: str, delta: int,
    tolerance: int,
) -> list[Candidate]:
    """Items whose magnitude accounts for the gap, in either direction.

    The test is `| |item| - |delta| | <= tol`, not `|item - delta| <= tol`.
    Item magnitudes are positive; delta is not. Comparing signed values would
    make every negative delta -- every overpayment -- fall through with no
    candidates and be reported as unexplained, which is a silent skip of half
    the problem space rather than an answer.
    """
    target = abs(delta)
    direction = _direction(delta)
    out = []
    for item_id, magnitude, declared in items:
        residual = target - magnitude
        if abs(residual) <= tolerance:
            out.append(
                Candidate(
                    item_id=item_id,
                    item_kind=kind,
                    magnitude_paise=magnitude,
                    direction=direction,
                    residual_paise=residual,
                    declared_settlement_id=declared,
                )
            )
    return out


def _pair_candidates(
    pools: ItemPools, delta: int, tolerance: int
) -> list[Candidate]:
    """Any two items summing to the gap.

    Sorted magnitudes plus a binary search rather than a double loop: the
    unrestricted pool is ~1,100 items, and the O(n^2) form is 600k comparisons
    per variance -- enough to matter once a real ledger has a month of them.
    """
    items = pools.all_items()
    items.sort(key=lambda t: t[2])
    magnitudes = [m for _i, _k, m, _d in items]
    target = abs(delta)
    direction = _direction(delta)

    out: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for i, (item_id, kind, magnitude, declared) in enumerate(items):
        lo = bisect.bisect_left(magnitudes, target - magnitude - tolerance, i + 1)
        hi = bisect.bisect_right(magnitudes, target - magnitude + tolerance, i + 1)
        for j in range(lo, hi):
            other_id, other_kind, other_mag, other_declared = items[j]
            key = tuple(sorted((item_id, other_id)))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                Candidate(
                    item_id=f"{item_id}+{other_id}",
                    item_kind="pair",
                    magnitude_paise=magnitude + other_mag,
                    direction=direction,
                    residual_paise=target - magnitude - other_mag,
                    components=(item_id, other_id),
                    # A pair is only declared-linked if BOTH halves say so.
                    declared_settlement_id=(
                        declared if declared and declared == other_declared else None
                    ),
                )
            )
    return out


def attribute_variance(
    settlement_id: str,
    bank_row_id: str,
    expected_net_paise: int,
    actual_credit_paise: int,
    pools: ItemPools,
    tolerance: int = TOLERANCE_PAISE,
) -> Attribution:
    """Explain one batch's gap, or say honestly that it cannot be explained.

    Levels are tried in order and the search STOPS at the first level that
    produces any candidate at all. Two or more candidates at any level from L1
    on is ambiguity, and ambiguity ends the search -- it does not fall through
    to the next level. Falling through would mean answering a question the
    engine has already shown it cannot answer: if three refunds each match the
    gap exactly, a pair that also matches is not better evidence, it is more
    ways to be wrong. A confident wrong attribution enters the books silently
    and surfaces months later; three possibilities and a human costs two
    minutes.
    """
    delta = expected_net_paise - actual_credit_paise

    base = dict(
        settlement_id=settlement_id,
        bank_row_id=bank_row_id,
        expected_net_paise=expected_net_paise,
        actual_credit_paise=actual_credit_paise,
        delta_paise=delta,
    )

    # -- L0: a magnitude test, with no candidate list to be ambiguous about ---
    if abs(delta) <= tolerance:
        return Attribution(
            **base,
            level=L0_ROUNDING_DRIFT,
            outcome=OUTCOME_ROUNDING_DRIFT_ACCEPTED,
            residual_paise=delta,
            confidence=CONF_ROUNDING_CLOSED,
        )

    n_items = len(pools.all_items())
    searches = (
        (L1_MISSING_PAYMENT,
         lambda: _single_item_candidates(pools.payments, "payment", delta, tolerance),
         lambda: (pools.amounts_for("payment"), len(pools.payments))),
        (L2_REFUND_ELSEWHERE,
         lambda: _single_item_candidates(pools.refunds, "refund", delta, tolerance),
         lambda: (pools.amounts_for("refund"), len(pools.refunds))),
        (L3_UNPOSTED_CHARGEBACK,
         lambda: _single_item_candidates(pools.chargebacks, "chargeback", delta, tolerance),
         lambda: (pools.amounts_for("chargeback"), len(pools.chargebacks))),
        # Pairs search n(n-1)/2 combinations, not n. Charging this level the
        # pool size would understate the search by three orders of magnitude
        # and make a coincidence look like proof.
        (L4_ITEM_PAIR,
         lambda: _pair_candidates(pools, delta, tolerance),
         lambda: (pools.all_amounts(), n_items * (n_items - 1) // 2)),
    )

    for level, search, describe in searches:
        candidates = search()
        if not candidates:
            continue
        amounts, n_searched = describe()
        if len(candidates) > 1:
            return Attribution(
                **base,
                level=level,
                outcome=OUTCOME_AMBIGUOUS,
                candidates=tuple(candidates),
                residual_paise=delta,
                confidence=CONF_NONE,
                evidence=assess(amounts, n_searched, tolerance, declared_link=False,
                                target_paise=delta,
                                mode=('pair' if level == L4_ITEM_PAIR else 'single')),
            )
        winner = candidates[0]
        evidence = assess(
            amounts, n_searched, tolerance,
            target_paise=delta,
            mode="pair" if level == L4_ITEM_PAIR else "single",
            # The gateway's own record naming this batch is close to proof;
            # numeric agreement is only an argument. Kept as a separate field
            # rather than folded into the estimate, because they are different
            # kinds of claim and an auditor needs to see which one this is.
            declared_link=winner.declared_settlement_id == settlement_id,
        )
        if evidence.refused:
            # Chance explains the fit as well as any cause does. Naming one
            # anyway would be reading noise aloud.
            return Attribution(
                **base,
                level=level,
                outcome=OUTCOME_UNEXPLAINED,
                candidates=(winner,),
                residual_paise=delta,
                confidence=CONF_NONE,
                evidence=evidence,
            )
        return Attribution(
            **base,
            level=level,
            outcome=OUTCOME_ATTRIBUTED,
            candidates=(winner,),
            resolved=winner,
            residual_paise=winner.residual_paise,
            confidence=CONF_ITEM_PAIR if level == L4_ITEM_PAIR else CONF_SINGLE_ITEM,
            evidence=evidence,
        )

    return Attribution(
        **base,
        level=L5_UNEXPLAINED,
        outcome=OUTCOME_UNEXPLAINED,
        residual_paise=delta,
        confidence=CONF_NONE,
    )


# --------------------------------------------------------------------------
# Part B -- infer membership
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SubsetSolution:
    payment_ids: tuple[str, ...]
    net_paise: int
    gross_paise: int
    residual_paise: int


@dataclass(frozen=True)
class MembershipResult:
    bank_row_id: str
    credit_paise: int
    outcome: str
    pool_size: int
    iterations: int
    subsets_found: int = 0
    solutions: tuple[SubsetSolution, ...] = ()
    confidence: float = CONF_NONE
    evidence: Evidence | None = None


def _enumerate_half(
    half: Sequence[tuple[str, int, int, int]], withholding_bps: int
) -> list[tuple[int, int, int, int]]:
    """All subsets of one half as (key, net_before_wh, gross, mask).

    `key = net_before_wh - W(gross)` is an ADDITIVE approximation of the true
    combined net, used only to index the search. It is approximate because
    withholding on the union is not the sum of withholding on the parts --
    which is the whole reason the exact check happens separately.
    """
    out = []
    n = len(half)
    for mask in range(1 << n):
        gross = 0
        net_before = 0
        m = mask
        while m:
            i = (m & -m).bit_length() - 1
            _pid, amount, fee, tax = half[i]
            gross += amount
            net_before += amount - fee - tax
            m &= m - 1
        out.append((net_before - apply_bps(gross, withholding_bps), net_before, gross, mask))
    return out


def infer_membership(
    bank_row_id: str,
    credit_paise: int,
    pool: Sequence[tuple[str, int, int, int]],
    withholding_bps: int,
    tolerance: int = TOLERANCE_PAISE,
    iteration_cap: int = DEFAULT_ITERATION_CAP,
    max_solutions_kept: int = 8,
    ignore_evidence_band: bool = False,
) -> MembershipResult:
    """Find every subset of `pool` whose net payout equals `credit_paise`.

    Meet in the middle: enumerate both halves, sort one, binary-search it for
    each subset of the other. 2^(n/2) rather than 2^n, which is what makes a
    thirty-payment day tractable at all.

    Every candidate found through the approximate key is then re-checked with
    `subset_net`, which applies withholding once to the combined gross. The
    key exists to make the search fast; only the exact check decides.

    More than one valid subset is AMBIGUOUS, not solved. Two different sets of
    payments that produce the same payout to the paisa are genuinely
    indistinguishable from the bank's side, and picking the first one found
    would make the answer depend on iteration order.
    """
    n = len(pool)
    if n == 0:
        return MembershipResult(
            bank_row_id=bank_row_id, credit_paise=credit_paise,
            outcome=OUTCOME_NO_SUBSET, pool_size=0, iterations=0,
        )
    # 2^n subsets are searched, so that is what the estimate is charged for.
    pool_evidence = assess(
        # Net contributions, not gross: the subset sums the search compares
        # against the credit are net of fee and tax, so the density must be
        # measured on the same quantity.
        [amount - fee - tax for _pid, amount, fee, tax in pool],
        n_candidates=2 ** n,
        tolerance_paise=tolerance,
        target_paise=credit_paise,
        mode="subset",
    )
    if pool_evidence.refused and not ignore_evidence_band:
        # Refused before searching, on the same rule L1-L4 use: at this pool
        # size chance produces a fit whether or not a real one exists, so the
        # search cannot tell us anything we could act on.
        return MembershipResult(
            bank_row_id=bank_row_id, credit_paise=credit_paise,
            outcome=OUTCOME_UNKNOWN_POOL_TOO_LARGE, pool_size=n, iterations=0,
            evidence=pool_evidence,
        )

    half = n // 2
    left, right = list(pool[:half]), list(pool[half:])
    projected = (1 << len(left)) + (1 << len(right))
    if projected > iteration_cap:
        # Answered, not failed: the honest response to a search too large to
        # complete is UNKNOWN, and it must be reported rather than hidden by
        # silently truncating the pool. Reported as cap+1 rather than the raw
        # projection, which for a 140-payment pool is a 22-digit number that
        # tells a reader nothing except that it did not run -- pool_size is the
        # figure that actually explains why.
        return MembershipResult(
            bank_row_id=bank_row_id, credit_paise=credit_paise,
            outcome=OUTCOME_UNKNOWN_CAP, pool_size=n, iterations=iteration_cap + 1,
        )

    iterations = 0
    left_subsets = _enumerate_half(left, withholding_bps)
    right_subsets = _enumerate_half(right, withholding_bps)
    iterations += len(left_subsets) + len(right_subsets)

    right_subsets.sort(key=lambda t: t[0])
    right_keys = [t[0] for t in right_subsets]

    found = 0
    kept: list[SubsetSolution] = []
    window = tolerance + _KEY_MARGIN_PAISE

    for key_l, _net_l, _gross_l, mask_l in left_subsets:
        lo = bisect.bisect_left(right_keys, credit_paise - window - key_l)
        hi = bisect.bisect_right(right_keys, credit_paise + window - key_l)
        for idx in range(lo, hi):
            iterations += 1
            if iterations > iteration_cap:
                return MembershipResult(
                    bank_row_id=bank_row_id, credit_paise=credit_paise,
                    outcome=OUTCOME_UNKNOWN_CAP, pool_size=n, iterations=iterations,
                )
            _key_r, _net_r, _gross_r, mask_r = right_subsets[idx]
            if mask_l == 0 and mask_r == 0:
                continue  # the empty subset pays out nothing; never a solution

            members = [left[i] for i in range(len(left)) if mask_l >> i & 1]
            members += [right[i] for i in range(len(right)) if mask_r >> i & 1]
            net, gross = subset_net(members, withholding_bps)
            residual = credit_paise - net
            if abs(residual) > tolerance:
                continue

            found += 1
            if len(kept) < max_solutions_kept:
                kept.append(
                    SubsetSolution(
                        payment_ids=tuple(p[0] for p in members),
                        net_paise=net,
                        gross_paise=gross,
                        residual_paise=residual,
                    )
                )

    if found == 0:
        outcome, confidence = OUTCOME_NO_SUBSET, CONF_NONE
    elif found == 1:
        outcome, confidence = OUTCOME_MEMBERSHIP_SOLVED, CONF_MEMBERSHIP_SOLVED
    else:
        outcome, confidence = OUTCOME_AMBIGUOUS, CONF_NONE

    return MembershipResult(
        bank_row_id=bank_row_id,
        credit_paise=credit_paise,
        outcome=outcome,
        pool_size=n,
        iterations=iterations,
        subsets_found=found,
        solutions=tuple(kept),
        confidence=confidence,
        # Carried on every result, not only refusals: a solved membership the
        # reader cannot weigh is a number without a unit.
        evidence=pool_evidence,
    )


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------
@dataclass
class AttributionResult:
    attributions: list[Attribution] = field(default_factory=list)
    memberships: list[MembershipResult] = field(default_factory=list)
    audit: list[dict] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def by_level(self) -> dict[str, int]:
        counts: dict[str, int] = {lvl: 0 for lvl in LEVEL_NAMES}
        for a in self.attributions:
            counts[a.level] += 1
        return counts

    @property
    def closed_l0(self) -> int:
        return sum(
            1 for a in self.attributions
            if a.outcome == OUTCOME_ROUNDING_DRIFT_ACCEPTED
        )

    @property
    def attributed_l1_l4(self) -> int:
        return sum(1 for a in self.attributions if a.is_attribution)

    @property
    def ambiguous(self) -> int:
        return sum(
            1 for a in self.attributions if a.outcome == OUTCOME_AMBIGUOUS
        ) + sum(1 for m in self.memberships if m.outcome == OUTCOME_AMBIGUOUS)


def build_pools(
    ledgers: NormalizedLedgers, settlement_id: str, withholding_bps: int
) -> ItemPools:
    """Items that are NOT already attributed to this batch.

    Anything already inside the batch is part of the expectation, so it cannot
    also be the explanation for the expectation being wrong.
    """
    payments = tuple(
        (
            p.entry_id,
            net_contribution(
                p.amount_paise,
                p.amounts["fee_paise"],
                p.amounts["tax_on_fee_paise"],
                withholding_bps,
            ),
            p.references["settlement_id"],
        )
        for p in ledgers.payments
        if p.references["settlement_id"] != settlement_id
    )
    refunds = tuple(
        (r.entry_id, r.amount_paise, r.references["settlement_id"])
        for r in ledgers.refunds
        if r.references["settlement_id"] != settlement_id
    )
    chargebacks = tuple(
        (c.entry_id, c.amount_paise, c.references["settlement_id"])
        for c in ledgers.chargebacks
        if c.references["settlement_id"] != settlement_id
    )
    return ItemPools(payments=payments, refunds=refunds, chargebacks=chargebacks)


def plausible_payment_pool(
    credit: LedgerEntry,
    ledgers: NormalizedLedgers,
    cal: BusinessCalendar,
    lag_days: int,
    cutoff_hour: int,
    window_days: int,
    withholding_bps: int = 0,
    tolerance: int = TOLERANCE_PAISE,
) -> list[tuple[str, int, int, int]]:
    """Payments that could plausibly have settled into this credit.

    Two prunes, and both are needed. The search is exponential in pool size, so
    an unpruned pool of 985 payments is not a search, it is a hang.

    Date: timing is derived from the capture cutoff and settlement lag rather
    than from settlement_id -- Part B exists precisely for the case where the
    settlement report is incomplete, so it cannot lean on it.

    Amount: every payment's net contribution is POSITIVE, so no member of a
    subset can exceed the credit it sums to. Dropping payments larger than the
    target is exact, not heuristic -- it cannot remove a real solution -- and
    on this data it is what takes a 146-payment pool down to a searchable one.
    """
    ceiling = credit.amounts["credit_paise"] + tolerance
    pool = []
    for payment in ledgers.payments:
        captured = payment.timestamp
        capture_date = captured.date()
        # Captures after the cutoff belong to the next capture day's batch.
        if captured.hour >= cutoff_hour:
            capture_date += timedelta(days=1)
        settled_on = cal.add_business_days(capture_date, lag_days)
        if not (
            settled_on <= credit.event_date
            <= cal.add_business_days(settled_on, window_days)
        ):
            continue
        contribution = net_contribution(
            payment.amount_paise,
            payment.amounts["fee_paise"],
            payment.amounts["tax_on_fee_paise"],
            withholding_bps,
        )
        if contribution > ceiling:
            continue
        pool.append(
            (
                payment.entry_id,
                payment.amount_paise,
                payment.amounts["fee_paise"],
                payment.amounts["tax_on_fee_paise"],
            )
        )
    return pool


def is_payout_shaped(credit: LedgerEntry) -> bool:
    """Could this credit be a gateway payout at all?

    Part B answers "which payments made this credit", and asking it of a credit
    that is not a payout invites a confident wrong answer. Subset-sum will
    happily find a combination of payments totalling a customer's direct NEFT
    -- measured on seed 42, it does exactly that for one of them, at confidence
    0.80, for money no payment was ever behind.

    The default posting window happens to make that pool intractable, so the
    iteration cap hides the problem. That is protection by accident, not by
    judgement, and it evaporates the moment someone narrows a window. So the
    gate is explicit: the same payout-shape test Tier 2 uses, which separates
    payout descriptors (100) from customer transfers (44) and ambient noise
    (33-34) with a wide margin.
    """
    # A row with no UTR still has a shape; the placeholder only fills the slot
    # in the template, and for the clipped template the UTR sits past the clip.
    utr = credit.references["utr"] or "AAAA000000000000"
    return (
        tier2_tolerant.narration_similarity(credit.text, utr)
        >= tier2_tolerant.NARRATION_SIMILARITY_THRESHOLD
    )


def run(
    ledgers: NormalizedLedgers,
    settlement_to_bank: Mapping[str, str],
    unexplained_credits: Sequence[LedgerEntry] = (),
    rates_path: str | Path = "config/rates.yaml",
    tolerance: int = TOLERANCE_PAISE,
    iteration_cap: int = DEFAULT_ITERATION_CAP,
    run_part_b: bool = True,
    require_payout_shape: bool = True,
) -> AttributionResult:
    """Part A over every tied batch with a gap; Part B over unexplained credits."""
    result = AttributionResult()
    rates = yaml.safe_load(Path(rates_path).read_text())
    withholding = rates.get("withholding", {}) or {}
    wh_bps = int(withholding.get("rate_bps", 0)) if withholding.get("enabled") else 0
    holidays = [
        datetime.strptime(h, "%Y-%m-%d").date() if isinstance(h, str) else h
        for h in rates.get("holidays_2026", [])
    ]
    cal = BusinessCalendar(holidays)
    settlement_cfg = rates.get("settlement", {}) or {}
    lag_days = int(settlement_cfg.get("lag_business_days", 2))
    cutoff_hour = int(settlement_cfg.get("cutoff_hour", 23))

    batches = {b.settlement_id: b for b in tier3_settlement.build_batches(ledgers, rates)}
    bank_by_id = {b.entry_id: b for b in ledgers.bank}

    # -- Part A --------------------------------------------------------------
    for settlement_id, bank_row_id in sorted(settlement_to_bank.items()):
        batch = batches.get(settlement_id)
        credit = bank_by_id.get(bank_row_id)
        if batch is None or credit is None:
            continue
        expected = tier3_settlement.compute_expected_net(batch)
        actual = credit.amounts["credit_paise"]
        if expected == actual:
            continue  # nothing to explain

        pools = build_pools(ledgers, settlement_id, wh_bps)
        attribution = attribute_variance(
            settlement_id, bank_row_id, expected, actual, pools, tolerance
        )
        result.attributions.append(attribution)
        result.audit.append(
            {
                "part": "A",
                "subject": settlement_id,
                "bank_row_id": bank_row_id,
                "delta_paise": attribution.delta_paise,
                "level": attribution.level,
                "level_name": LEVEL_NAMES[attribution.level],
                "outcome": attribution.outcome,
                "residual_paise": attribution.residual_paise,
                "confidence": attribution.confidence,
                "candidates_considered": len(attribution.candidates),
                "candidates": [asdict(c) for c in attribution.candidates],
                "pool_sizes": {
                    "payments": len(pools.payments),
                    "refunds": len(pools.refunds),
                    "chargebacks": len(pools.chargebacks),
                },
            }
        )

    # -- Part B --------------------------------------------------------------
    skipped_not_payout = 0
    if run_part_b:
        for credit in unexplained_credits:
            if credit.entry_type != "bank_credit":
                continue
            if require_payout_shape and not is_payout_shaped(credit):
                # Not a payout, so "which payments made it" is the wrong
                # question. Recorded rather than silently dropped.
                skipped_not_payout += 1
                result.audit.append(
                    {
                        "part": "B",
                        "subject": credit.entry_id,
                        "credit_paise": credit.amounts["credit_paise"],
                        "method": "payout_shape_gate",
                        "outcome": "SKIPPED_NOT_PAYOUT_SHAPED",
                        "narration": credit.text,
                        "confidence": CONF_NONE,
                    }
                )
                continue
            pool = plausible_payment_pool(
                credit, ledgers, cal, lag_days, cutoff_hour,
                window_days=tier3_settlement.POSTING_WINDOW_DAYS,
                withholding_bps=wh_bps, tolerance=tolerance,
            )
            membership = infer_membership(
                credit.entry_id,
                credit.amounts["credit_paise"],
                pool,
                wh_bps,
                tolerance=tolerance,
                iteration_cap=iteration_cap,
            )
            result.memberships.append(membership)
            result.audit.append(
                {
                    "part": "B",
                    "subject": credit.entry_id,
                    "credit_paise": membership.credit_paise,
                    "method": "meet_in_the_middle",
                    "outcome": membership.outcome,
                    "pool_size": membership.pool_size,
                    "iterations": membership.iterations,
                    "subsets_found": membership.subsets_found,
                    "confidence": membership.confidence,
                    "solutions": [asdict(s) for s in membership.solutions],
                }
            )

    levels = result.by_level()
    result.stats.update(
        variances_seen=len(result.attributions),
        closed_l0=result.closed_l0,
        attributed_l1_l4=result.attributed_l1_l4,
        ambiguous_part_a=sum(
            1 for a in result.attributions if a.outcome == OUTCOME_AMBIGUOUS
        ),
        unexplained_l5=sum(
            1 for a in result.attributions if a.outcome == OUTCOME_UNEXPLAINED
        ),
        **{f"level_{lvl}": n for lvl, n in levels.items()},
        membership_attempted=len(result.memberships),
        membership_solved=sum(
            1 for m in result.memberships if m.outcome == OUTCOME_MEMBERSHIP_SOLVED
        ),
        membership_ambiguous=sum(
            1 for m in result.memberships if m.outcome == OUTCOME_AMBIGUOUS
        ),
        membership_no_subset=sum(
            1 for m in result.memberships if m.outcome == OUTCOME_NO_SUBSET
        ),
        membership_cap_hits=sum(
            1 for m in result.memberships if m.outcome == OUTCOME_UNKNOWN_CAP
        ),
        membership_above_ceiling=sum(
            1 for m in result.memberships
            if m.outcome == OUTCOME_UNKNOWN_POOL_TOO_LARGE
        ),
        membership_max_pool=max((m.pool_size for m in result.memberships), default=0),
        membership_max_iterations=max(
            (m.iterations for m in result.memberships), default=0
        ),
        membership_skipped_not_payout=skipped_not_payout,
        ambiguous_total=result.ambiguous,
    )
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def format_report(result: AttributionResult) -> str:
    s = result.stats
    out = ["", "tier 3b -- variance attribution & membership inference", "-" * 64]
    out.append("  PART A -- explain a variance (batch known)")
    out.append(f"    variances seen              {s['variances_seen']:>6}")
    out.append("")
    out.append(f"    L0 rounding drift CLOSED    {s['closed_l0']:>6}"
               "   <- closed, NOT attributed")
    out.append(f"    L1-L4 ATTRIBUTED            {s['attributed_l1_l4']:>6}"
               "   <- attribution successes")
    out.append("")
    for level in (L1_MISSING_PAYMENT, L2_REFUND_ELSEWHERE,
                  L3_UNPOSTED_CHARGEBACK, L4_ITEM_PAIR, L5_UNEXPLAINED):
        out.append(f"      {level}  {LEVEL_NAMES[level]:<42} {s[f'level_{level}']:>4}")
    out.append("")
    out.append(f"    AMBIGUOUS (part A)          {s['ambiguous_part_a']:>6}")
    out.append(f"    L5 unexplained              {s['unexplained_l5']:>6}")
    out.append("")
    for attribution in result.attributions:
        out.append(
            f"    {attribution.settlement_id}  delta {attribution.delta_paise:+d} paise"
            f"  -> {attribution.level} {attribution.outcome}"
            f"  residual {attribution.residual_paise:+d}"
            f"  conf {attribution.confidence:.2f}"
        )
    out.append("")
    out.append("  PART B -- infer membership (batch unknown)")
    out.append(f"    credits offered             "
               f"{s['membership_attempted'] + s['membership_skipped_not_payout']:>6}")
    out.append(f"    skipped: not payout-shaped  {s['membership_skipped_not_payout']:>6}"
               "   <- customer transfers, not payouts")
    out.append(f"    credits searched            {s['membership_attempted']:>6}")
    out.append(f"    subsets SOLVED (unique)     {s['membership_solved']:>6}")
    out.append(f"    AMBIGUOUS (multi-subset)    {s['membership_ambiguous']:>6}")
    out.append(f"    no subset found             {s['membership_no_subset']:>6}")
    out.append(f"    iteration-cap hits          {s['membership_cap_hits']:>6}")
    out.append(f"    refused on evidence band      {s['membership_above_ceiling']:>6}")
    out.append(f"    largest pool / iterations   {s['membership_max_pool']:>6}"
               f" / {s['membership_max_iterations']}")
    out.append("")
    out.append(f"  AMBIGUITY BRANCH FIRED        {s['ambiguous_total']:>6}"
               "   <- the engine knowing what it does not know")
    out.append("-" * 64)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tier 3b -- attribution.")
    ap.add_argument("--data", default="data/seed42", help="dataset directory")
    ap.add_argument("--rates", default="config/rates.yaml")
    ap.add_argument("--cap", type=int, default=DEFAULT_ITERATION_CAP)
    ap.add_argument("--audit", default=None, help="write the audit log as JSONL")
    ap.add_argument("--no-part-b", action="store_true")
    args = ap.parse_args(argv)

    ledgers = load(args.data)
    tier1 = tier1_exact.run(ledgers, rates_path=args.rates)
    tier2 = tier2_tolerant.run(ledgers, tier1, rates_path=args.rates)
    tier3 = tier3_settlement.run(
        ledgers, tier1, already_tied=tier2.settlement_to_bank, rates_path=args.rates
    )
    tied = {
        **tier1.settlement_to_bank,
        **tier2.settlement_to_bank,
        **tier3.settlement_to_bank,
    }
    claimed = set(tied.values())
    # Batches no tier would match, paired to their credit on the UTR alone so
    # the amount gap becomes explainable rather than invisible.
    still_open = [s.entry_id for s in ledgers.settlements if s.entry_id not in tied]
    explain_only = pair_by_utr(ledgers, still_open, claimed)
    tied = {**tied, **explain_only}
    claimed |= set(explain_only.values())
    unexplained = [
        b for b in ledgers.bank
        if b.entry_type == "bank_credit" and b.entry_id not in claimed
    ]

    result = run(
        ledgers, tied, unexplained, rates_path=args.rates,
        iteration_cap=args.cap, run_part_b=not args.no_part_b,
    )
    print(format_report(result))

    if args.audit:
        Path(args.audit).write_text(
            "\n".join(json.dumps(e) for e in result.audit) + "\n", encoding="utf-8"
        )
        print(f"\naudit log -> {args.audit} ({len(result.audit)} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
