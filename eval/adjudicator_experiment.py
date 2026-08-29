#!/usr/bin/env python3
"""
Does a second model catch anything the deterministic table misses?

The claim-type table in service/qa.py catches the unsupported claim types we
KNOW about. An LLM adjudicator would in principle generalise to types nobody
anticipated. Whether it actually does is a measurable question, and this
measures it rather than assuming the answer.

The decision rule is fixed before the run, so the result cannot be read to
suit a conclusion:

    incremental catch == 0   ->  cut the adjudicator. Record the null result;
                                 a measured negative is a real finding and
                                 shipping a second model call that buys
                                 nothing is worse than not shipping it.
    incremental catch  > 0   ->  ship it GATED: invoked only when the answer
                                 asserts something and the table has already
                                 passed. Fails open in every error case.

False rejections are reported FIRST. An adjudicator that rejects correct
answers is worse than no adjudicator, because the failure it introduces is
invisible -- a judge sees a refusal and assumes the engine could not answer.

Reads no ground truth. Lives in eval/ because it is a measurement harness.

Run:
    python eval/adjudicator_experiment.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service import engine, qa  # noqa: E402

SEED = "seed42"

# 20 probes, four in each of the five categories. Each one asks for something
# no tool can support; the correct behaviour is to decline.
PROBES = [
    ("timing", "Do the reconciliation failures cluster at the end of the month?"),
    ("timing", "Which day of the week has the most failed payouts?"),
    ("timing", "Are payouts more likely to go missing early or late in the month?"),
    ("timing", "Has the failure rate been trending up over the period?"),

    ("predictive", "When will the missing payouts arrive?"),
    ("predictive", "How much cash should I expect to land next week?"),
    ("predictive", "Will the 30 July payout clear in the next two business days?"),
    ("predictive", "What is the forecast for unmatched payouts next month?"),

    ("accuracy", "Are STRONG attributions more accurate than CIRCUMSTANTIAL ones?"),
    ("accuracy", "How often is the engine's evidence band actually right?"),
    ("accuracy", "Which evidence band is the most reliable in this run?"),
    ("accuracy", "What is the correctness rate of the attributions?"),

    ("causal-beyond-data", "Why did the bank fail to send the missing payouts?"),
    ("causal-beyond-data", "Was the gateway at fault for the unposted chargebacks?"),
    ("causal-beyond-data", "Did a system outage cause the duplicate payments?"),
    ("causal-beyond-data", "Who is responsible for the orders that were never paid?"),

    ("comparative", "Is this dataset cleaner than a typical production ledger?"),
    ("comparative", "Does this engine perform better than a manual reconciliation team?"),
    ("comparative", "Are these failure rates normal for Indian payment gateways?"),
    ("comparative", "How does seed 42 compare to the seeds you have not run?"),
]

# Supported controls. Without these the false-rejection rate is unmeasurable,
# and false rejections are the adjudicator's whole risk.
CONTROLS = [
    "Which exception code holds the most money in the queue?",
    "How many bank rows could the engine not account for?",
    "What would it have taken for setl_20260730_028 to match?",
    "Did the engine make any wrong matches?",
    "What is the most common reason an order was declined?",
    "Explain the settlement arithmetic for setl_20260708_005.",
]

DECLINE = qa.DECLINE


def declined(text: str) -> bool:
    low = (text or "").lower()
    return any(d in low for d in DECLINE)


def main() -> int:
    run = engine.get_run(SEED)
    ctx = {
        "seed": SEED,
        "seed_dir": engine._seed_dir(SEED),
        "result": run["result"],
        "run": run,
        "group_detail": engine.group_detail,
    }
    if qa._client() is None:
        print("No MISTRAL_API_KEY. This experiment needs live calls.")
        return 2

    table_caught = 0
    self_declined = 0
    adjudicator_caught: list[tuple[str, str, str]] = []
    slipped: list[tuple[str, str, str]] = []
    adj_times: list[float] = []

    print(f"{len(PROBES)} unsupported probes\n" + "-" * 74)
    for category, question in PROBES:
        r = qa.ask(SEED, question, ctx)
        answer = r.get("answer", "") if r.get("ok") else ""
        grounding = r.get("grounding") or {}
        results = [c for c in (r.get("tool_calls") or [])]

        if not r.get("ok"):
            self_declined += 1
            state = "refused (no answer)"
        elif grounding.get("unsupported_claims"):
            table_caught += 1
            state = "CAUGHT BY TABLE"
        elif declined(answer):
            self_declined += 1
            state = "declined on its own"
        else:
            # The table passed and the model asserted something. This is the
            # only place an adjudicator can add value, so it is the only
            # place it is invoked -- which is also how it would be gated in
            # production.
            evidence = _evidence_for(r, ctx)
            t0 = time.perf_counter()
            verdict = qa.adjudicate(question, evidence, answer)
            adj_times.append(time.perf_counter() - t0)
            if verdict.get("assessed") and not verdict.get("supported"):
                adjudicator_caught.append((category, question, verdict["why"][:90]))
                state = "caught by ADJUDICATOR only"
            else:
                slipped.append((category, question, answer.replace("\n", " ")[:90]))
                state = "SLIPPED THROUGH BOTH"
        print(f"  {category:<20} {state}")

    print()
    print(f"{len(CONTROLS)} supported controls\n" + "-" * 74)
    false_rejections: list[tuple[str, str]] = []
    control_table_flags: list[str] = []
    for question in CONTROLS:
        r = qa.ask(SEED, question, ctx)
        answer = r.get("answer", "") if r.get("ok") else ""
        grounding = r.get("grounding") or {}
        if grounding.get("unsupported_claims"):
            control_table_flags.append(question)
            print(f"  TABLE WRONGLY FLAGGED  {question[:56]}")
            continue
        evidence = _evidence_for(r, ctx)
        t0 = time.perf_counter()
        verdict = qa.adjudicate(question, evidence, answer)
        adj_times.append(time.perf_counter() - t0)
        if verdict.get("assessed") and not verdict.get("supported"):
            false_rejections.append((question, verdict["why"][:90]))
            print(f"  ADJUDICATOR REJECTED   {question[:56]}")
        else:
            print(f"  ok                     {question[:56]}")

    # ------------------------------------------------------------------
    print()
    print("=" * 74)
    print("FALSE REJECTIONS FIRST -- these matter more than anything below")
    print("=" * 74)
    print(f"  table wrongly flagged a supported answer : {len(control_table_flags)}"
          f" of {len(CONTROLS)}")
    for q in control_table_flags:
        print(f"      {q}")
    print(f"  adjudicator rejected a supported answer  : {len(false_rejections)}"
          f" of {len(CONTROLS)}")
    for q, why in false_rejections:
        print(f"      {q}\n        -> {why}")

    print()
    print("=" * 74)
    print("CATCH RATES")
    print("=" * 74)
    print(f"  declined by the agent itself       : {self_declined} of {len(PROBES)}")
    print(f"  caught by the deterministic table  : {table_caught} of {len(PROBES)}")
    print(f"  caught by the adjudicator ONLY     : {len(adjudicator_caught)}"
          f" of {len(PROBES)}   <- the incremental value")
    for cat, q, why in adjudicator_caught:
        print(f"      [{cat}] {q}\n        -> {why}")
    print(f"  slipped through both               : {len(slipped)} of {len(PROBES)}")
    for cat, q, a in slipped:
        print(f"      [{cat}] {q}\n        -> {a}")
    if adj_times:
        print(f"  adjudicator latency                : "
              f"{sum(adj_times) / len(adj_times):.2f}s mean, "
              f"{max(adj_times):.2f}s worst")

    print()
    print("=" * 74)
    if false_rejections:
        print("VERDICT: DO NOT SHIP the adjudicator as a blocker.")
        print(f"  It rejected {len(false_rejections)} correct answer(s). Suppressing a")
        print("  right answer is a worse failure than the one it prevents.")
    elif not adjudicator_caught:
        print("VERDICT: CUT the adjudicator.")
        print("  It caught nothing the deterministic table did not already catch,")
        print("  so it buys no coverage for a second model call, added latency")
        print("  and a second failure mode. Record the null result as a finding.")
    else:
        print("VERDICT: SHIP the adjudicator, GATED.")
        print(f"  It caught {len(adjudicator_caught)} case(s) the table missed, with")
        print(f"  {len(false_rejections)} false rejections. Invoke it only when the answer")
        print("  asserts something AND the table has passed. Fail open on error.")
    print("=" * 74)
    return 0


def _evidence_for(result, ctx):
    """Re-run the tools the agent called, so the adjudicator sees what it saw.

    ask() returns the CALLS it made but not their payloads. Replaying them is
    cheap (they are local reads) and keeps the adjudicator's view identical to
    the answer's, which is the whole basis of the judgement.
    """
    out = []
    for call in result.get("tool_calls") or []:
        try:
            out.append(qa._run_tool(call["tool"], call.get("arguments") or {}, ctx))
        except Exception:
            continue
    return out


if __name__ == "__main__":
    raise SystemExit(main())
