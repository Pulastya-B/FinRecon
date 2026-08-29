#!/usr/bin/env python3
"""
Controls for everything that stops the Q&A agent asserting things it cannot
support.

This suite lives in the repo, not in a scratchpad. An earlier version of it did
not, and the temp directory was pruned -- taking with it the only evidence that
the guardrails work. The checks ARE the claim; they have to survive.

Runs entirely offline. `verify_grounding` is a pure function and the tools read
a completed run, so no API key and no network are involved.

FALSE POSITIVES ARE REPORTED FIRST. Flagging a correct refusal is worse than
the failure this machinery exists to catch: it would punish the exact behaviour
the agent is supposed to have.

Run:
    python tests/test_agent_guardrails.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service import engine, qa  # noqa: E402

run = engine.get_run("seed42")
ctx = {
    "seed": "seed42",
    "seed_dir": engine._seed_dir("seed42"),
    "result": run["result"],
    "run": run,
    "group_detail": engine.group_detail,
}

BREAKDOWN = qa._run_tool("queue_breakdown", {}, ctx)
DECLINES = qa._run_tool("aggregate_declines", {"kind": "bank"}, ctx)
CHAIN = qa._run_tool("trace_chain", {"order_id": "ord_000412"}, ctx)
SUMMARY = qa._run_tool("run_summary", {}, ctx)
ARITH = qa._run_tool("settlement_breakdown",
                     {"settlement_id": "setl_20260708_005"}, ctx)
TRACE = qa._run_tool("why_not", {"entity_id": "setl_20260708_005"}, ctx)
COMPARE = qa._run_tool("compare_findings",
                       {"group_ids": ["MISSING_IN_BANK__setl_20260730_028"]}, ctx)

C_BREAKDOWN = [{"tool": "queue_breakdown", "ok": True}]
C_DECLINES = [{"tool": "aggregate_declines", "ok": True}]
C_CHAIN = [{"tool": "trace_chain", "ok": True}]
C_SUMMARY = [{"tool": "run_summary", "ok": True}]
C_ARITH = [{"tool": "settlement_breakdown", "ok": True},
           {"tool": "why_not", "ok": True}]
C_COMPARE = [{"tool": "compare_findings", "ok": True}]

false_pos: list[str] = []
missed: list[str] = []
broken: list[str] = []


def _flagged(answer, results, calls):
    g = qa.verify_grounding(answer, results, calls)
    return (not g["ok"]), g["problems"]


def must_pass(name, answer, results, calls):
    """Legitimate output. Flagging it is a FALSE POSITIVE."""
    bad, problems = _flagged(answer, results, calls)
    print(f"    [{'FAIL' if bad else 'pass'}] {name}"
          + (f"   <- wrongly flagged: {problems}" if bad else ""))
    if bad:
        false_pos.append(name)


def must_flag(name, answer, results, calls):
    """Unsupportable output. Letting it through is a MISS."""
    bad, _ = _flagged(answer, results, calls)
    print(f"    [{'pass' if bad else 'FAIL'}] {name}"
          + ("" if bad else "   <- let through"))
    if not bad:
        missed.append(name)


def check(name, ok, detail=""):
    print(f"    [{'pass' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        broken.append(name)


# ===========================================================================
print("=" * 74)
print("FALSE POSITIVES -- legitimate answers that must NOT be flagged")
print("=" * 74)

must_pass("an answer built entirely from tool output",
          "The engine expected ₹5,28,662.38 for setl_20260708_005 and the bank "
          "credited ₹5,27,156.38, a gap of ₹1,506.00. Tier 3 recorded "
          "AMOUNT_VARIANCE_UNEXPLAINED.",
          [ARITH, TRACE], C_ARITH)

must_pass("a correct refusal naming what it cannot determine",
          "I could not determine whether the failures cluster at the end of "
          "the month from this run.",
          [BREAKDOWN], C_BREAKDOWN)

must_pass("a refusal about band accuracy",
          "I could not determine whether STRONG attributions are more accurate "
          "than CIRCUMSTANTIAL ones; the engine records money and counts per "
          "band, never correctness rates.",
          [BREAKDOWN], C_BREAKDOWN)

must_pass("a factual date statement using dates that are in the evidence",
          "Order ord_000412 was captured on 2026-07-29 and settled into a "
          "payout dated 2026-07-31, which the bank credited the same day.",
          [CHAIN], C_CHAIN)

must_pass("a supported claim about wrong matches",
          "The engine recorded 0 incorrect matches in this run.",
          [SUMMARY], C_SUMMARY)

must_pass("a supported comparison of MONEY, not accuracy",
          "ORDER_UNPAID holds more of the queue than MISSING_IN_BANK does.",
          [BREAKDOWN], C_BREAKDOWN)

must_pass("a supported count claim",
          "The most common reason a bank row was declined is "
          "UTR_NOT_IN_SETTLEMENT_REPORT.",
          [DECLINES], C_DECLINES)

must_pass("quoting the engine's own caused_by link is reporting, not asserting",
          "The engine declined it at tier 2 with NO_TOLERANT_CANDIDATE.",
          [TRACE], [{"tool": "why_not", "ok": True}])

must_pass("uniqueness where the count really is one",
          "This is the only DUPLICATE_PAYMENT finding.",
          [{"siblings_in_queue": {"DUPLICATE_PAYMENT": {
              "total_with_this_code": 1, "others_you_did_not_ask_about": []}}}],
          C_COMPARE)

# ===========================================================================
print()
print("=" * 74)
print("TOKEN-LEVEL -- fabricated figures, ids and codes must be caught")
print("=" * 74)

must_flag("an invented figure",
          "The gap on setl_20260708_005 was ₹9,99,999.99.", [ARITH], C_ARITH)
must_flag("an invented id",
          "The credit bank_999999 was the one at fault.", [ARITH], C_ARITH)
must_flag("an invented reason code",
          "Tier 2 declined with BANK_WAS_ON_HOLIDAY.", [ARITH], C_ARITH)
must_flag("speculative language",
          "The payout probably failed because the bank was slow that week.",
          [ARITH], C_ARITH)
must_flag("a cause asserted with no tool called at all",
          "It failed because the merchant refunded it.", [], [])
must_flag("an id that appears only inside a tool ERROR",
          "Order ord_999999 traced cleanly to the bank.",
          [{"error": "no decision recorded for ord_999999"}],
          [{"tool": "trace_chain", "ok": False}])
must_flag("uniqueness the tools contradict",
          "This is the only MISSING_IN_BANK finding in the queue.",
          [COMPARE], C_COMPARE)

# ===========================================================================
print()
print("=" * 74)
print("CLAIM-LEVEL -- unsupported claim TYPES must be caught")
print("=" * 74)

must_flag("asserts clustering in time, on evidence with no date dimension",
          "The reconciliation failures cluster at the end of the month.",
          [BREAKDOWN], C_BREAKDOWN)
must_flag("the NEGATIVE temporal assertion is still a temporal claim",
          "The failures do not cluster at the end of the month; they are "
          "spread evenly across the period.",
          [BREAKDOWN], C_BREAKDOWN)
must_flag("a date on one entity is not a distribution over dates",
          "Most of the failures happen in the last week of the month.",
          [CHAIN], C_CHAIN)
must_flag("asserts one band is more accurate than another",
          "STRONG attributions are more accurate than CIRCUMSTANTIAL ones.",
          [BREAKDOWN], C_BREAKDOWN)
must_flag("the negative accuracy assertion, which is how it actually failed",
          "STRONG attributions are not more accurate than CIRCUMSTANTIAL ones; "
          "they account for only a small share of the queue.",
          [BREAKDOWN], C_BREAKDOWN)
must_flag("predicts the future",
          "This payout will most likely arrive in the next two business days.",
          [BREAKDOWN], C_BREAKDOWN)

# ===========================================================================
print()
print("=" * 74)
print("THE INSTRUMENT ITSELF -- these checks must be able to fail")
print("=" * 74)

_saved = qa.CLAIM_RULES
qa.CLAIM_RULES = ()
still_caught = sum(
    1 for a in ("The failures cluster at the end of the month.",
                "STRONG attributions are more accurate than CIRCUMSTANTIAL ones.",
                "This payout will arrive in the next two business days.")
    if not qa.verify_grounding(a, [BREAKDOWN], C_BREAKDOWN)["ok"]
)
qa.CLAIM_RULES = _saved
check("with CLAIM_RULES disabled, every claim check goes red",
      still_caught == 0, f"{still_caught} of 3 still caught")

_saved_rules = qa.CLAIM_RULES
qa.CLAIM_RULES = ()
recovered = qa.verify_grounding("The failures cluster at the end of the month.",
                                [BREAKDOWN], C_BREAKDOWN)["ok"]
qa.CLAIM_RULES = _saved_rules
check("and restoring them makes it red again",
      not qa.verify_grounding("The failures cluster at the end of the month.",
                              [BREAKDOWN], C_BREAKDOWN)["ok"],
      f"(inert run allowed it: {recovered})")

# ===========================================================================
print()
print("=" * 74)
if false_pos:
    print(f"FALSE POSITIVES ({len(false_pos)}): " + ", ".join(false_pos))
    print("  A wrongly-flagged correct answer is worse than the bug this fixes.")
else:
    print("FALSE POSITIVES: none -- no legitimate answer was flagged")

print(f"MISSED: {', '.join(missed) if missed else 'none'}")
print(f"INSTRUMENT: {', '.join(broken) if broken else 'ok -- the checks can fail'}")

print()
if false_pos or missed or broken:
    print("AGENT GUARDRAILS: RED")
    sys.exit(1)
print("AGENT GUARDRAILS: ALL CONTROLS PASS")
