#!/usr/bin/env python3
"""
Score a reconciliation result against ground truth.

This module lives in eval/ because it opens `ground_truth.json`, and that file
is readable ONLY from here. If the oracle leaks into the reconciliation path,
precision stops being a measurement and becomes a tautology.

Why precision is the headline number
------------------------------------
A production reconciler can report coverage -- how much it matched. It cannot
report precision, because it has no oracle. Here one exists, so the dangerous
error mode is measurable, and the two error modes are wildly asymmetric:

    a MISS       goes to the exception queue and costs a human two minutes
    a WRONG MATCH is silent, plausible, lands in the books, and surfaces
                 months later as an unexplained variance -- or never

So `precision = correct_matches / matches_made` is the number that matters, and
an engine that routes a hard case to an exception is doing the right thing.
Coverage falling while precision holds is correct behaviour. Precision falling
alongside coverage is a bug, not a property.

Undefined, not flattering
-------------------------
With zero matches made, precision has no denominator. It is reported as None
and rendered "n/a", never as 1.0. Scoring a matcher that has not been built yet
must not produce a perfect score -- which is exactly why this runs clean on an
empty result, before any matching logic exists.

Run:
    python eval/score.py --data data/seed42
    python eval/score.py --data data/seed42 --result out/result.json
    python eval/score.py --data data/seed42 --json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MATCHED = "MATCHED"

# Chain-level codes the scorer expects to see from an engine. UNKNOWN is the
# honest answer for "something is wrong here and I cannot say what", and is
# scored like any other code rather than being waved through.
CHAIN_OUTCOMES: tuple[str, ...] = (
    "MATCHED",
    "ORDER_UNPAID",
    "DUPLICATE_PAYMENT",
    "MISSING_IN_BANK",
    "CHARGEBACK_UNPOSTED",
    "AMOUNT_VARIANCE_UNEXPLAINED",
    "TIMING_PENDING",
    "AMBIGUOUS_MULTI_CANDIDATE",
    "UNKNOWN",
)

# Bank-row verdicts. A bank row is not a chain -- it has its own denominator --
# so it is scored in a separate section rather than folded into chain accuracy.
BANK_IGNORED = "IGNORED"


# --------------------------------------------------------------------------
# Result contract
# --------------------------------------------------------------------------
@dataclass
class MatchDecision:
    """One engine decision about one order.

    The id lists are what the engine ASSERTS. An empty list asserts nothing and
    is not checked against truth -- but see `matches_without_linkage` in the
    report, because an engine that claims MATCHED while naming no settlement
    and no bank row has not actually reconciled anything, and that must not
    read as perfect precision.
    """

    order_id: str | None
    outcome: str = MATCHED
    payment_ids: list[str] = field(default_factory=list)
    settlement_ids: list[str] = field(default_factory=list)
    bank_row_ids: list[str] = field(default_factory=list)
    # Free-form engine metadata (tier that fired, confidence, evidence). Never
    # scored -- it exists so a decision stays auditable after the fact.
    notes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_match(self) -> bool:
        return self.outcome == MATCHED


@dataclass
class ReconciliationResult:
    """What an engine hands the scorer.

    `elapsed_seconds` and `input_rows` are supplied by the caller that ran the
    engine, not measured here: timing the scorer would report the scorer's
    throughput, which is not a fact anyone needs.
    """

    decisions: list[MatchDecision] = field(default_factory=list)
    # bank_row_id -> verdict. Optional: an engine that does not yet dispose of
    # bank rows simply leaves this empty and the bank section is skipped.
    bank_dispositions: dict[str, str] = field(default_factory=dict)
    elapsed_seconds: float | None = None
    input_rows: int | None = None


def empty_result() -> ReconciliationResult:
    """A result that matched nothing.

    The zero point. Running it should report coverage 0, recall 0, precision
    n/a -- never a perfect score, and never a crash.
    """
    return ReconciliationResult()


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
@dataclass
class ScoreReport:
    # -- population -------------------------------------------------------
    total_chains: int = 0
    matchable_chains: int = 0          # ground truth says MATCHED
    true_exception_chains: int = 0

    # -- what the engine did ----------------------------------------------
    decisions_made: int = 0
    matches_made: int = 0
    exceptions_raised: int = 0
    undecided_chains: int = 0
    duplicate_decisions: int = 0
    unknown_orders: int = 0

    # -- match quality ----------------------------------------------------
    correct_matches: int = 0
    wrong_matches: int = 0
    matches_without_linkage: int = 0
    wrong_match_reasons: dict[str, int] = field(default_factory=dict)

    # -- exception quality ------------------------------------------------
    correct_exceptions: int = 0
    wrong_code_exceptions: int = 0
    false_exceptions: int = 0          # raised on a chain that was matchable
    missed_exceptions: int = 0         # true exception the engine matched or skipped

    # -- headline metrics -------------------------------------------------
    coverage: float = 0.0
    precision: float | None = None
    recall: float = 0.0
    false_positive_rate: float | None = None
    false_positive_share_of_book: float = 0.0
    exception_accuracy: float | None = None
    exception_recall: float = 0.0

    # -- throughput -------------------------------------------------------
    elapsed_seconds: float | None = None
    input_rows: int | None = None
    rows_per_second: float | None = None
    chains_per_second: float | None = None
    throughput_note: str = ""

    # -- attribution (only when the engine proposed causes) ----------------
    # Coverage and precision cannot see attribution at all: explaining a gap is
    # not a match, so an engine that correctly identifies every unposted refund
    # scores exactly the same on both. These two metrics are the only place
    # that work becomes visible.
    attribution_attempted: int = 0
    causes_proposed: int = 0
    causes_correct: int = 0
    causes_wrong: int = 0
    attribution_rate: float | None = None
    attribution_accuracy: float | None = None
    unattributable_by_design: int = 0
    # Split by evidence band. One accuracy figure over mixed evidence hides the
    # question that matters: is the engine's own confidence honest?
    by_strength: dict[str, dict[str, int]] = field(default_factory=dict)
    refused_on_evidence: int = 0

    # -- bank rows (only when dispositions were supplied) ------------------
    bank_scored: bool = False
    bank_rows_total: int = 0
    orphans_total: int = 0
    orphans_found: int = 0
    missing_in_gateway_recall: float | None = None
    noise_rows_total: int = 0
    noise_rows_flagged: int = 0
    noise_false_flag_rate: float | None = None

    # -- diagnostics ------------------------------------------------------
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Flat dict, for sensitivity.py's `row.update(...)` and for --json."""
        return {
            "total_chains": self.total_chains,
            "matchable_chains": self.matchable_chains,
            "matches_made": self.matches_made,
            "correct_matches": self.correct_matches,
            "wrong_matches": self.wrong_matches,
            "exceptions_raised": self.exceptions_raised,
            "undecided_chains": self.undecided_chains,
            "coverage": self.coverage,
            "precision": self.precision,
            "recall": self.recall,
            "false_positive_rate": self.false_positive_rate,
            "false_positive_share_of_book": self.false_positive_share_of_book,
            "exception_accuracy": self.exception_accuracy,
            "exception_recall": self.exception_recall,
            "rows_per_second": self.rows_per_second,
            "chains_per_second": self.chains_per_second,
            "missing_in_gateway_recall": self.missing_in_gateway_recall,
            "noise_false_flag_rate": self.noise_false_flag_rate,
            "attribution_rate": self.attribution_rate,
            "attribution_accuracy": self.attribution_accuracy,
            "causes_proposed": self.causes_proposed,
            "causes_correct": self.causes_correct,
            "by_strength": self.by_strength,
            "refused_on_evidence": self.refused_on_evidence,
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _ratio(numerator: int, denominator: int) -> float | None:
    """Divide, or return None when the denominator does not exist.

    None rather than 0.0 or 1.0: a metric with no denominator is undefined, and
    substituting either constant would be a claim the data does not support.
    """
    return numerator / denominator if denominator else None


def _pct(value: float | None) -> str:
    return "   n/a" if value is None else f"{value * 100:6.2f}%"


def _as_decision(obj: Any) -> MatchDecision:
    """Accept a MatchDecision or a plain dict, so an engine can hand back JSON."""
    if isinstance(obj, MatchDecision):
        return obj
    if not isinstance(obj, Mapping):
        raise TypeError(f"cannot read a decision from {type(obj).__name__}")
    return MatchDecision(
        order_id=obj.get("order_id"),
        outcome=obj.get("outcome", MATCHED),
        payment_ids=list(obj.get("payment_ids") or []),
        settlement_ids=list(obj.get("settlement_ids") or []),
        bank_row_ids=list(obj.get("bank_row_ids") or []),
        notes=obj.get("notes") or {},
    )


def _coerce_result(result: Any) -> ReconciliationResult:
    """Normalise whatever the caller passed into a ReconciliationResult.

    Deliberately permissive about the container and strict about the contents:
    the scorer should not be the reason a matcher cannot be plugged in, but it
    must not silently accept a decision it cannot interpret.
    """
    if result is None:
        return empty_result()
    if isinstance(result, ReconciliationResult):
        return result
    if isinstance(result, Mapping):
        return ReconciliationResult(
            decisions=[_as_decision(d) for d in result.get("decisions", [])],
            bank_dispositions=dict(result.get("bank_dispositions") or {}),
            elapsed_seconds=result.get("elapsed_seconds"),
            input_rows=result.get("input_rows"),
        )
    if isinstance(result, Iterable):
        return ReconciliationResult(decisions=[_as_decision(d) for d in result])
    raise TypeError(f"cannot score a result of type {type(result).__name__}")


def _linkage_errors(decision: MatchDecision, chain: Mapping[str, Any]) -> list[str]:
    """Which asserted id lists disagree with truth.

    Set equality, not subset: asserting three of a settlement's four payments is
    a different claim from asserting all four, and a partial linkage that scores
    as correct would hide exactly the batch-membership errors this project
    exists to catch. An empty assertion is not checked -- the engine made no
    claim there, and `matches_without_linkage` records how often that happened.
    """
    errors = []
    for field_name, reason in (
        ("payment_ids", "wrong_payments"),
        ("settlement_ids", "wrong_settlements"),
        ("bank_row_ids", "wrong_bank_rows"),
    ):
        asserted = set(getattr(decision, field_name))
        if asserted and asserted != set(chain.get(field_name) or []):
            errors.append(reason)
    return errors


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def score_against_ground_truth(
    result: Any,
    ground_truth_path: str | Path,
) -> ScoreReport:
    """Score a reconciliation result. Safe on an empty result."""
    gt = json.loads(Path(ground_truth_path).read_text())
    res = _coerce_result(result)
    rep = ScoreReport()

    chains = {c["order_id"]: c for c in gt.get("chains", []) if c.get("order_id")}
    rep.total_chains = len(chains)
    rep.matchable_chains = sum(
        1 for c in chains.values() if c.get("expected_outcome") == MATCHED
    )
    rep.true_exception_chains = rep.total_chains - rep.matchable_chains

    # -- one decision per order. Later duplicates are counted and dropped so
    # -- the score cannot depend on dict ordering.
    seen: dict[str, MatchDecision] = {}
    for raw in res.decisions:
        decision = _as_decision(raw)
        key = decision.order_id
        if key is None:
            rep.unknown_orders += 1
            continue
        if key in seen:
            rep.duplicate_decisions += 1
            continue
        seen[key] = decision

    reasons: Counter[str] = Counter()
    confusion: dict[str, Counter[str]] = {}

    for order_id, decision in seen.items():
        chain = chains.get(order_id)
        if chain is None:
            # A decision about an order that does not exist. If it claims a
            # match, it invented a linkage, which is a false positive.
            rep.unknown_orders += 1
            rep.decisions_made += 1
            if decision.is_match:
                rep.matches_made += 1
                rep.wrong_matches += 1
                reasons["unknown_order"] += 1
            else:
                rep.exceptions_raised += 1
                rep.wrong_code_exceptions += 1
            continue

        expected = chain.get("expected_outcome", "UNKNOWN")
        rep.decisions_made += 1
        confusion.setdefault(expected, Counter())[decision.outcome] += 1

        if decision.is_match:
            rep.matches_made += 1
            if expected != MATCHED:
                rep.wrong_matches += 1
                reasons["should_be_exception"] += 1
                rep.missed_exceptions += 1
                continue
            errors = _linkage_errors(decision, chain)
            if errors:
                rep.wrong_matches += 1
                for reason in errors:
                    reasons[reason] += 1
                continue
            rep.correct_matches += 1
            if not (decision.settlement_ids or decision.bank_row_ids):
                # Counted, not penalised: a MATCHED claim naming neither a
                # settlement nor a bank row asserts nothing checkable, and a
                # precision built out of those is not a precision.
                rep.matches_without_linkage += 1
        else:
            rep.exceptions_raised += 1
            if expected == MATCHED:
                rep.false_exceptions += 1
            elif decision.outcome == expected:
                rep.correct_exceptions += 1
            else:
                rep.wrong_code_exceptions += 1

    # A true exception the engine never decided on is still missed.
    for order_id, chain in chains.items():
        if order_id in seen:
            continue
        rep.undecided_chains += 1
        if chain.get("expected_outcome") != MATCHED:
            rep.missed_exceptions += 1

    rep.wrong_match_reasons = dict(reasons)
    rep.confusion = {k: dict(v) for k, v in confusion.items()}

    # -- headline metrics --------------------------------------------------
    # coverage: share of the whole book the engine claims to have auto-matched.
    # Denominator is every chain, not just the matchable ones, because a
    # production engine does not know which items were resolvable -- that is
    # the entire reason it needs a human queue.
    rep.coverage = (rep.matches_made / rep.total_chains) if rep.total_chains else 0.0
    rep.precision = _ratio(rep.correct_matches, rep.matches_made)
    rep.recall = (
        rep.correct_matches / rep.matchable_chains if rep.matchable_chains else 0.0
    )
    rep.false_positive_rate = _ratio(rep.wrong_matches, rep.matches_made)
    # The same errors over the whole book: how much of the ledger is silently
    # poisoned. This is the number an accountant is actually exposed to.
    rep.false_positive_share_of_book = (
        rep.wrong_matches / rep.total_chains if rep.total_chains else 0.0
    )
    rep.exception_accuracy = _ratio(rep.correct_exceptions, rep.exceptions_raised)
    rep.exception_recall = (
        rep.correct_exceptions / rep.true_exception_chains
        if rep.true_exception_chains
        else 0.0
    )

    # -- throughput --------------------------------------------------------
    rep.elapsed_seconds = res.elapsed_seconds
    rep.input_rows = res.input_rows
    if res.elapsed_seconds and res.elapsed_seconds > 0:
        if res.input_rows:
            rep.rows_per_second = res.input_rows / res.elapsed_seconds
        rep.chains_per_second = rep.total_chains / res.elapsed_seconds
    else:
        rep.throughput_note = "no timing supplied by the caller"

    _score_bank_rows(gt, res, rep)
    _score_attribution(gt, result, rep)
    return rep


def _proposed_items(proposal: Mapping[str, Any]) -> list[str]:
    """Every item the engine named -- one for a single cause, two for a pair."""
    item = proposal.get("item_id")
    if not item:
        return []
    # A pair is recorded as "a+b" by the pipeline; split it back out.
    return item.split("+") if "+" in item else [item]


def _score_attribution(gt: Mapping[str, Any], result: Any, rep: ScoreReport) -> None:
    """Did the engine name a cause, and was it the RIGHT one?

    Two metrics, and the second is the claim. Naming a cause is easy -- a
    subset-sum will name one for almost any gap you hand it. Naming the cause
    that actually produced the shortfall is the thing worth measuring, so
    correctness is judged on the ITEM the engine identified, not on the
    category it guessed. An engine that says "a refund did this" and points at
    the wrong refund has not attributed anything.
    """
    shortfalls = {
        x["settlement_id"]: x for x in gt.get("settlement_shortfalls", [])
    }
    if not shortfalls:
        return

    proposals = []
    if isinstance(result, Mapping):
        proposals = result.get("attributions") or []
    if not proposals:
        return

    # A shortfall with no ledger counterpart cannot be attributed by anyone.
    # Counted separately so it neither flatters nor penalises the engine.
    rep.unattributable_by_design = sum(
        1 for x in shortfalls.values() if not x.get("attributable", True)
    )

    for proposal in proposals:
        rep.attribution_attempted += 1
        if (proposal.get("evidence") or {}).get("strength") == "REFUSE":
            rep.refused_on_evidence += 1
        if not proposal.get("item_id"):
            continue  # a level fired but named nothing; not a proposal
        rep.causes_proposed += 1
        band = (proposal.get("evidence") or {}).get("strength", "UNKNOWN")
        bucket = rep.by_strength.setdefault(band, {"n": 0, "correct": 0})
        bucket["n"] += 1
        truth = shortfalls.get(proposal.get("settlement_id"))
        # Compare SETS. A compound shortfall has two causing items and ground
        # truth records both; judging a pair against a single item_id field
        # marks every correct pair wrong, which is how this metric read 0.0%
        # for one run before the comparison was fixed.
        proposed_items = set(_proposed_items(proposal))
        true_items = set(truth.get("item_ids") or
                         ([truth["item_id"]] if truth and truth.get("item_id") else [])
                         ) if truth else set()
        if true_items and proposed_items == true_items:
            rep.causes_correct += 1
            bucket["correct"] += 1
        else:
            rep.causes_wrong += 1

    rep.attribution_rate = _ratio(rep.causes_proposed, rep.attribution_attempted)
    rep.attribution_accuracy = _ratio(rep.causes_correct, rep.causes_proposed)


def _score_bank_rows(
    gt: Mapping[str, Any],
    res: ReconciliationResult,
    rep: ScoreReport,
) -> None:
    """Score bank-row verdicts, when the engine supplied any.

    Separate from chain scoring because the denominator is different: 79 bank
    rows against 1000 chains. Folding them together would let a good bank-row
    score dilute a bad chain score.

    Two things are measured here that chain scoring cannot see at all:
    MISSING_IN_GATEWAY (direct NEFT -- money with no payment object, so it
    belongs to no chain) and ambient noise, which must be ignored WITHOUT being
    flagged. A queue full of salary rows trains the operator to ignore the
    queue, which is this tool's worst failure mode.
    """
    if not res.bank_dispositions:
        return

    orphan_ids = {o["bank_row_id"] for o in gt.get("orphan_bank_rows", [])}
    settlement_rows = {
        s["bank_row_id"] for s in gt.get("settlements", []) if s.get("bank_row_id")
    }
    all_dispositioned = set(res.bank_dispositions)
    # Noise = every bank row the engine ruled on that is neither a settlement
    # credit nor a known orphan. Derived from ground truth, so it needs no
    # narration heuristics.
    noise_ids = all_dispositioned - orphan_ids - settlement_rows

    rep.bank_scored = True
    rep.bank_rows_total = len(all_dispositioned)
    rep.orphans_total = len(orphan_ids)
    rep.orphans_found = sum(
        1
        for bid in orphan_ids
        if res.bank_dispositions.get(bid) == "MISSING_IN_GATEWAY"
    )
    rep.missing_in_gateway_recall = _ratio(rep.orphans_found, rep.orphans_total)

    rep.noise_rows_total = len(noise_ids)
    rep.noise_rows_flagged = sum(
        1
        for bid in noise_ids
        if res.bank_dispositions.get(bid) not in (BANK_IGNORED, None)
    )
    rep.noise_false_flag_rate = _ratio(rep.noise_rows_flagged, rep.noise_rows_total)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def format_report(rep: ScoreReport, label: str = "") -> str:
    out: list[str] = []
    bar = "=" * 68
    out.append(bar)
    out.append(f"RECONCILIATION SCORE{f'  --  {label}' if label else ''}")
    out.append(bar)

    out.append("\n  population")
    out.append(f"    chains in ground truth        {rep.total_chains:>7}")
    out.append(f"    truly matchable               {rep.matchable_chains:>7}")
    out.append(f"    true exceptions               {rep.true_exception_chains:>7}")

    out.append("\n  engine output")
    out.append(f"    decisions made                {rep.decisions_made:>7}")
    out.append(f"    matches claimed               {rep.matches_made:>7}")
    out.append(f"    exceptions raised             {rep.exceptions_raised:>7}")
    out.append(f"    chains never decided          {rep.undecided_chains:>7}")

    out.append("\n  headline metrics")
    out.append(f"    coverage                      {_pct(rep.coverage)}"
               f"   {rep.matches_made}/{rep.total_chains} chains claimed")
    out.append(f"    PRECISION                     {_pct(rep.precision)}"
               f"   {rep.correct_matches}/{rep.matches_made} claims correct")
    out.append(f"    recall                        {_pct(rep.recall)}"
               f"   {rep.correct_matches}/{rep.matchable_chains} matchable found")
    out.append(f"    false-positive rate           {_pct(rep.false_positive_rate)}"
               f"   {rep.wrong_matches}/{rep.matches_made} claims wrong")
    out.append(f"    ... as share of the book      {_pct(rep.false_positive_share_of_book)}"
               f"   {rep.wrong_matches}/{rep.total_chains} chains poisoned")
    out.append(f"    exception accuracy            {_pct(rep.exception_accuracy)}"
               f"   {rep.correct_exceptions}/{rep.exceptions_raised} codes right")
    out.append(f"    exception recall              {_pct(rep.exception_recall)}"
               f"   {rep.correct_exceptions}/{rep.true_exception_chains} found")

    out.append("\n  throughput")
    if rep.rows_per_second or rep.chains_per_second:
        out.append(f"    elapsed                       {rep.elapsed_seconds:>7.3f} s")
        if rep.rows_per_second:
            out.append(f"    input rows/sec                {rep.rows_per_second:>7.0f}"
                       f"   over {rep.input_rows} normalised entries")
        out.append(f"    chains/sec                    {rep.chains_per_second:>7.0f}")
        if rep.throughput_note:
            out.append(f"    note: {rep.throughput_note}")
    else:
        out.append(f"    n/a -- {rep.throughput_note or 'no timing supplied'}")

    if rep.wrong_matches:
        out.append("\n  why matches were wrong")
        for reason, n in sorted(
            rep.wrong_match_reasons.items(), key=lambda kv: -kv[1]
        ):
            out.append(f"    {reason:<28} {n:>7}")

    if rep.matches_without_linkage:
        out.append(
            f"\n  WARNING: {rep.matches_without_linkage} matches claimed MATCHED "
            "without naming a settlement or bank row."
        )
        out.append("           Those assert nothing checkable; precision above is soft.")

    if rep.duplicate_decisions or rep.unknown_orders:
        out.append("\n  contract violations")
        if rep.duplicate_decisions:
            out.append(f"    duplicate decisions           {rep.duplicate_decisions:>7}"
                       "   (first kept)")
        if rep.unknown_orders:
            out.append(f"    decisions on unknown orders   {rep.unknown_orders:>7}")

    if rep.confusion:
        out.append("\n  confusion (expected -> predicted)")
        for expected in sorted(rep.confusion):
            row = rep.confusion[expected]
            cells = ", ".join(f"{k} {v}" for k, v in sorted(row.items()))
            out.append(f"    {expected:<28} {cells}")

    if rep.attribution_rate is not None or rep.causes_proposed:
        out.append("\n  attribution (invisible to coverage and precision)")
        out.append(f"    variances offered             {rep.attribution_attempted:>7}")
        out.append(f"    causes proposed               {rep.causes_proposed:>7}")
        out.append(f"    attribution rate              {_pct(rep.attribution_rate)}")
        out.append(f"    ATTRIBUTION ACCURACY          {_pct(rep.attribution_accuracy)}"
                   f"   {rep.causes_correct}/{rep.causes_proposed} named the right item")
        if rep.unattributable_by_design:
            out.append(f"    unattributable by design      "
                       f"{rep.unattributable_by_design:>7}   (no ledger counterpart)")
        if rep.refused_on_evidence:
            out.append(f"    refused on evidence band      "
                       f"{rep.refused_on_evidence:>7}   (chance explains it)")
        for band in ("STRONG", "CIRCUMSTANTIAL", "REFUSE", "UNKNOWN"):
            bucket = rep.by_strength.get(band)
            if not bucket:
                continue
            acc = bucket["correct"] / bucket["n"] if bucket["n"] else None
            out.append(f"    {band:<28}  {bucket['n']:>4} attributions, "
                       f"{_pct(acc).strip()} correct")

    if rep.bank_scored:
        out.append("\n  bank rows (separate denominator)")
        out.append(f"    rows dispositioned            {rep.bank_rows_total:>7}")
        out.append(f"    MISSING_IN_GATEWAY recall     {_pct(rep.missing_in_gateway_recall)}"
                   f"   {rep.orphans_found}/{rep.orphans_total} direct credits found")
        out.append(f"    ambient noise falsely flagged {_pct(rep.noise_false_flag_rate)}"
                   f"   {rep.noise_rows_flagged}/{rep.noise_rows_total}")
    else:
        out.append("\n  bank rows: not scored -- engine supplied no dispositions")

    out.append("")
    if rep.matches_made == 0:
        out.append("  No matches claimed. Precision is undefined, not perfect --")
        out.append("  reported as n/a so an unbuilt matcher cannot score 100%.")
        out.append("")
    out.append(bar)
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _load_result(path: str | None, data_dir: Path) -> ReconciliationResult:
    """Load an engine result, or synthesise the empty one.

    With no result file this still produces a real throughput number by timing
    Tier 0, which is the only stage that exists yet -- and confirms the scoring
    plumbing works before there is anything to plug into it.
    """
    if path:
        return _coerce_result(json.loads(Path(path).read_text()))

    from finrecon.normalize import load as normalize_load

    started = time.perf_counter()
    ledgers = normalize_load(data_dir)
    elapsed = time.perf_counter() - started

    res = empty_result()
    res.elapsed_seconds = elapsed
    res.input_rows = len(ledgers)
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score a reconciliation result.")
    ap.add_argument("--data", default="data/seed42", help="dataset directory")
    ap.add_argument("--result", default=None, help="engine result JSON (omit for empty)")
    ap.add_argument("--json", action="store_true", help="emit metrics as JSON")
    args = ap.parse_args(argv)

    data_dir = Path(args.data)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir

    result = _load_result(args.result, data_dir)
    if args.result is None and not args.json:
        print(
            f"\nno --result given: scoring the empty result "
            f"(throughput measured over Tier-0 normalisation of {result.input_rows} rows)"
        )

    rep = score_against_ground_truth(result, data_dir / "ground_truth.json")

    if args.json:
        print(json.dumps(rep.to_dict(), indent=2))
    else:
        print(format_report(rep, label=args.data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
