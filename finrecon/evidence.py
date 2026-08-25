#!/usr/bin/env python3
"""
How likely was this attribution an accident?

Attribution says a gap of ₹3,847 matches a refund of ₹3,847. What that
establishes is that two numbers are equal -- not that the refund caused the
gap. The refund may be correctly booked where it says it is, and the real cause
something else that happens to also be ₹3,847.

Whether that coincidence is worth worrying about is arithmetic, not intuition.
Search N candidates, accept anything landing within a window of W paise, and if
those candidates are spread over a range of R paise, the number you should
expect to fit by chance alone is

    E = N * W / R

That is the whole model. It is deliberately crude -- it assumes candidate
amounts are spread evenly over their range, which they are not -- but it is
crude in a knowable direction and it turns "this feels solid" into a number
that can be checked against outcomes. eval/evidence_calibration.py does exactly
that check.

Reading E:

    E = 0.0001   searching 985 payments, one fit expected per 10,000 gaps.
                 A fit is essentially certain to be the real thing.
    E = 0.058    searching 484,620 pairs. Still unlikely, but no longer
                 negligible -- one gap in seventeen would find a false pair.
    E = 130698   searching 2^40 subsets. Chance produces a hundred thousand
                 fits. The one you found means nothing.

The bands follow from that directly, and the last one is the important one:
above E = 1 the search is expected to find a fit whether or not a real cause
exists, so accepting the fit is reading noise.

Why the range is measured, not assumed
--------------------------------------
R comes from the candidates ACTUALLY searched. A pool of small refunds spreads
over a different range from a pool of large payments, and using a constant
would make the same fit look strong in one pool and weak in another for no
reason connected to the evidence. Measuring it is also what makes the number
survive a change of dataset.
"""

from __future__ import annotations

import random
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Iterable, Sequence

# An accidental fit expected less than once per hundred gaps. At this level the
# coincidence explanation is not competitive with the real one.
STRONG_MAX = 0.01

# At or above one expected accidental fit, the search finds something whether
# or not a cause exists. Chance explains the result as well as anything does,
# so there is nothing to report but the refusal.
REFUSE_MIN = 1.0

STRONG = "STRONG"
CIRCUMSTANTIAL = "CIRCUMSTANTIAL"
REFUSE = "REFUSE"

BAND_MEANING = {
    STRONG: "accidental fit essentially ruled out",
    CIRCUMSTANTIAL: "plausible, verify before acting",
    REFUSE: "chance explains it equally well",
}


def acceptance_window_paise(tolerance_paise: int) -> int:
    """Width of the accepting band, in whole paise.

    ±2 paise accepts five distinct values (-2, -1, 0, +1, +2), not four. The
    off-by-one matters at the STRONG boundary, where the whole point is that
    the estimate is not being flattered.
    """
    return 2 * abs(tolerance_paise) + 1


def pool_range_paise(amounts: Iterable[int]) -> int:
    """Spread of the candidates searched, max - min.

    Zero when every candidate has the same amount, or there is only one. That
    is not a degenerate case to paper over: a pool with no spread means every
    member fits whatever the first one fits, and the caller is told the
    estimate is unbounded rather than handed a flattering number.
    """
    values = [abs(a) for a in amounts]
    if len(values) < 2:
        return 0
    return max(values) - min(values)


def analytic_accidental_fits(
    n_candidates: int, window_paise: int, pool_range_paise: int
) -> float:
    """E = N * W / R -- the closed form, kept for comparison only.

    Superseded as the band input by `empirical_accidental_fits`, and retained
    because the GAP between the two is the finding. It assumes candidate
    amounts are spread evenly over their range. They are not: 55% of payments
    on this data are under Rs 2,499, so candidates cluster far more densely
    near small targets than uniform-over-Rs-4.17-lakh implies. Measured at L4
    the closed form said 0.069 while the pool actually delivered ~1.5 -- 22x
    optimistic, in the direction that makes a coincidence look like proof.

    Returns infinity when the pool has no spread, because in that case a fit
    carries no information at all and any finite number would overstate it.
    """
    if n_candidates <= 0:
        return 0.0
    if pool_range_paise <= 0:
        return float("inf")
    return n_candidates * window_paise / pool_range_paise


# Trials for the empirical estimate. 500 puts the standard error of a mean
# near 1.5 at about 0.055, which separates 0.069 from 1.5 decisively and still
# resolves the 1.0 band boundary. More trials buy precision nobody acts on.
DEFAULT_TRIALS = 500

# Fixed so two runs of the same measurement agree. An estimate that moves when
# you re-run it is not a measurement, and this one decides a published band.
DEFAULT_RNG_SEED = 20260824

# Subset sums are sampled rather than counted -- 2^n is not enumerable -- and
# the local density around the target is read off the sample. Wide enough that
# a few hundred samples land in it, narrow enough to be local.
_DENSITY_BINS = 60

_cache: dict[tuple, float] = {}


def _draw_targets(rng: random.Random, target_paise: int, n: int) -> list[int]:
    """Random targets of similar magnitude to the real one.

    Similar, not identical: measuring at the exact target would count the real
    answer among the accidents. Half to one-and-a-half times keeps the draw in
    the same part of the amount distribution, which is the whole point -- the
    density of candidates near Rs 4,000 is nothing like the density near
    Rs 4,00,000.
    """
    lo = max(1, abs(target_paise) // 2)
    hi = max(lo + 1, abs(target_paise) * 3 // 2)
    return [rng.randrange(lo, hi) for _ in range(n)]


def _count_singles(sorted_amounts: list[int], target: int, half_width: int) -> int:
    """Items within +-half_width of the target. Half-width, not set count."""
    return (bisect_right(sorted_amounts, target + half_width)
            - bisect_left(sorted_amounts, target - half_width))


def _count_pairs(sorted_amounts: list[int], target: int, half_width: int) -> int:
    """Pairs i<j whose amounts sum to within +-half_width of the target.

    Takes a HALF-WIDTH, not the accepting-set count. See the note in
    `empirical_accidental_fits`.

    Sampling is the wrong tool here: at ~1.5 hits among 578,350 pairs the hit
    rate is 2.6e-6, so a sample large enough to see hits reliably would be
    slower than counting them. Sorted array plus two binary searches per item
    counts them exactly in O(n log n).
    """
    total = 0
    n = len(sorted_amounts)
    for i, a in enumerate(sorted_amounts):
        lo = bisect_left(sorted_amounts, target - a - half_width, i + 1)
        hi = bisect_right(sorted_amounts, target - a + half_width, i + 1)
        total += hi - lo
    return total


def _subset_density_fits(
    amounts: Sequence[int], target: int, window: int, rng: random.Random,
    samples: int = 20_000,
) -> float:
    """Expected subset sums landing in the window, from a sampled density.

    2^n subsets cannot be enumerated and cannot be sampled directly either --
    the hit rate is far too low. So the local DENSITY of subset sums near the
    target is estimated from a sample, then multiplied by the window width and
    the true subset count. Density estimation is the right tool precisely
    because subset sums are not uniform: they pile up near half the pool total,
    which is exactly the structure the closed form ignores.
    """
    n = len(amounts)
    if n == 0:
        return 0.0
    total = sum(amounts)
    if total <= 0:
        return float("inf")

    sums = []
    for _ in range(samples):
        # A uniformly random subset: each item in with probability 1/2.
        sums.append(sum(a for a in amounts if rng.random() < 0.5))

    bin_width = max(1, total // _DENSITY_BINS)
    lo, hi = target - bin_width // 2, target + bin_width // 2
    in_bin = sum(1 for value in sums if lo <= value <= hi)
    if in_bin == 0:
        # Nothing sampled near the target. Report the smallest density the
        # sample can resolve rather than zero, which would claim certainty the
        # sample does not support.
        in_bin = 1
    density_per_paise = (in_bin / samples) / bin_width
    return (2 ** n) * density_per_paise * window


def empirical_accidental_fits(
    pool: Sequence[int],
    target_paise: int,
    window_paise: int,
    mode: str = "single",
    n_trials: int = DEFAULT_TRIALS,
    rng_seed: int = DEFAULT_RNG_SEED,
) -> float:
    """How many candidates ACTUALLY fit a target of this size, by measurement.

    Draw random targets of similar magnitude to the real one, count how many
    pool items (or pairs, or subsets) land within the window, and average. No
    assumption about the shape of the amount distribution survives -- the pool
    is measured as it is.
    """
    amounts = sorted(abs(a) for a in pool)
    if not amounts:
        return 0.0

    # UNITS. `window_paise` is the COUNT of accepting values -- 5 for a +-2
    # tolerance -- which is the right multiplier for the analytic form, where W
    # is the measure of the accepting set. The counters below need the
    # HALF-WIDTH instead, because they test `target +- h`. Passing the count
    # straight through searched +-5 and returned exactly double the real number
    # of pairs, which is how the first measurement read 5.17 where the pool
    # actually holds 3. The conversion is explicit and in one place.
    half_width = max(0, (window_paise - 1) // 2)

    key = (mode, len(amounts), amounts[0], amounts[-1], sum(amounts),
           abs(target_paise) // 1000, window_paise, n_trials, rng_seed)
    if key in _cache:
        return _cache[key]

    rng = random.Random(rng_seed)

    if mode == "subset":
        # One density estimate answers the question; repeating it re-samples
        # the same distribution at greater cost.
        result = _subset_density_fits(amounts, abs(target_paise), window_paise, rng)
    else:
        counter = _count_pairs if mode == "pair" else _count_singles
        targets = _draw_targets(rng, target_paise, n_trials)
        result = sum(counter(amounts, t, half_width) for t in targets) / n_trials

    _cache[key] = result
    return result


# Kept so older callers and probes keep working; the analytic form is now a
# comparison point, not the band input.
expected_accidental_fits = analytic_accidental_fits


def strength(expected: float) -> str:
    """Band for an expected-accidental-fit count."""
    if expected < STRONG_MAX:
        return STRONG
    if expected < REFUSE_MIN:
        return CIRCUMSTANTIAL
    return REFUSE


@dataclass(frozen=True)
class Evidence:
    """What was searched, and how much a fit is worth given that.

    `declared_link` is the one field here that is not statistical, and it
    outranks everything else. It records whether the winning candidate's OWN
    record already points at this batch -- its settlement_id naming this cycle
    -- rather than merely agreeing on an amount. A declared link is close to
    proof: the gateway itself says these belong together. Numeric agreement is
    an argument. Collapsing the two into one confidence number would lose
    exactly the distinction an auditor needs.
    """

    candidates_searched: int
    window_paise: int
    pool_range_paise: int
    # The band input: measured against the actual pool, no distribution
    # assumption. See `empirical_accidental_fits`.
    expected_accidental_fits: float
    strength: str
    declared_link: bool = False
    # The closed form, carried alongside for comparison only. The GAP between
    # the two is the finding, and hiding it would hide the reason the estimate
    # changed.
    analytic_fits: float = 0.0
    mode: str = "single"

    @property
    def refused(self) -> bool:
        return self.strength == REFUSE

    def to_dict(self) -> dict:
        return {
            "candidates_searched": self.candidates_searched,
            "declared_link": self.declared_link,
            # Rounded for display only. The band was decided on the full value,
            # so a figure that rounds to 0.0 never silently becomes STRONG.
            "expected_accidental_fits": round(self.expected_accidental_fits, 6),
            "analytic_fits": round(self.analytic_fits, 6),
            "mode": self.mode,
            "strength": self.strength,
            "meaning": BAND_MEANING[self.strength],
        }


def assess(
    candidate_amounts: Sequence[int],
    n_candidates: int | None = None,
    tolerance_paise: int = 2,
    declared_link: bool = False,
    target_paise: int = 0,
    mode: str = "single",
) -> Evidence:
    """Build an Evidence record from the pool that was actually searched.

    The band comes from the EMPIRICAL estimate -- random targets of the same
    magnitude, counted against the real pool. The analytic form is computed too
    and carried alongside, because the two disagreeing by a factor of twenty is
    a fact about this data that a reader should be able to see rather than
    take on trust.

    `n_candidates` defaults to the pool size but is passed explicitly where the
    search space is larger than the pool -- pairs are O(n^2) and subsets are
    2^n, and charging them the pool size would understate the search enormously.
    """
    window = acceptance_window_paise(tolerance_paise)
    spread = pool_range_paise(candidate_amounts)
    n = len(candidate_amounts) if n_candidates is None else n_candidates
    analytic = analytic_accidental_fits(n, window, spread)

    if target_paise:
        measured = empirical_accidental_fits(
            candidate_amounts, target_paise, window, mode=mode
        )
    else:
        # Without a target there is nothing to measure against, so the closed
        # form is all there is. Recorded as such rather than silently blended.
        measured = analytic

    return Evidence(
        candidates_searched=n,
        window_paise=window,
        pool_range_paise=spread,
        expected_accidental_fits=measured,
        strength=strength(measured),
        declared_link=declared_link,
        analytic_fits=analytic,
        mode=mode,
    )
