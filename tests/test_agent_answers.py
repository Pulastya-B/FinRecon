#!/usr/bin/env python3
"""
End-to-end behaviour of the Q&A agent, against the live model.

The companion to tests/test_agent_guardrails.py, which tests `verify_grounding`
as a pure function offline. This one asks real questions and checks what comes
back. It needs MISTRAL_API_KEY and it makes real calls; with no key it skips
rather than fails, because a missing key is a normal state everywhere else in
this project.

These are the four questions that once produced confidently wrong answers,
recorded in INCIDENTS.md under [2026-08-26 red-team].

WHY THE ASSERTIONS LOOK THE WAY THEY DO. The first version of this suite banned
substrings: an answer about band accuracy failed if it contained "are more
accurate". That rejected the CORRECT answer three times in four, because the
model's refusal is naturally phrased "I could not determine whether STRONG
attributions are more accurate than CIRCUMSTANTIAL ones" -- the banned phrase
sits inside the refusal. A probe that cannot tell an assertion from a denial is
the same defect the claim rules exist to fix, so these assert on the guardrail's
own verdict and on sentence-level assertions instead.

Run:
    python tests/test_agent_answers.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service import qa  # noqa: E402

BASE = "http://127.0.0.1:8000"
fails: list[str] = []


def check(name, ok, detail=""):
    print(f"    [{'pass' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def ask(question: str):
    body = json.dumps({"question": question}).encode()
    req = urllib.request.Request(f"{BASE}/api/ask/seed42", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=240) as r:
        d = json.load(r)
    d["_s"] = time.perf_counter() - t0
    return d


def asserting_sentences(answer: str) -> list[str]:
    """Sentences that claim something, with refusals removed.

    Reuses the agent's own DECLINE vocabulary so the test and the guardrail
    cannot drift apart in what counts as a refusal.
    """
    out = []
    for sentence in qa._sentences(answer):
        low = sentence.lower()
        if any(d in low for d in qa.DECLINE):
            continue
        out.append(low)
    return out


def handled_safely(d, label):
    """Either the agent declined, or the guardrail flagged it. Never both-silent."""
    answer = d.get("answer", "")
    grounding = d.get("grounding") or {}
    flagged = bool(grounding.get("unsupported_claims")) or not grounding.get("ok", True)
    declined = len(asserting_sentences(answer)) == 0 or any(
        d_ in answer.lower() for d_ in qa.DECLINE
    )
    check(f"{label}: declined or flagged, never asserted silently",
          declined or flagged,
          "" if (declined or flagged) else f"asserted unflagged: {answer[:120]}")
    return answer


def main() -> int:
    try:
        urllib.request.urlopen(f"{BASE}/api/health", timeout=10).read()
    except Exception:
        print(f"Service not running at {BASE}. Start it with:")
        print("    py -m uvicorn service.app:app --port 8000")
        return 2
    if qa._client() is None:
        print("No MISTRAL_API_KEY -- skipping live agent tests (not a failure).")
        return 0

    urllib.request.urlopen(
        urllib.request.Request(f"{BASE}/api/reconcile/seed42", method="POST"),
        timeout=600).read()

    print("1) TIMING -- no tool groups anything by date")
    d = ask("Do the reconciliation failures cluster at the end of the month?")
    print(f"    tools: {[c['tool'] for c in d['tool_calls']] or 'none'}  {d['_s']:.1f}s")
    answer = handled_safely(d, "timing")
    check("timing: makes no unflagged clustering assertion",
          not any("cluster" in s and "month" in s for s in asserting_sentences(answer))
          or bool((d.get("grounding") or {}).get("unsupported_claims")))

    print()
    print("2) BAND ACCURACY -- bands carry money and counts, never correctness")
    d = ask("Are STRONG attributions actually more accurate than "
            "CIRCUMSTANTIAL ones in this run?")
    print(f"    tools: {[c['tool'] for c in d['tool_calls']] or 'none'}  {d['_s']:.1f}s")
    answer = handled_safely(d, "accuracy")
    check("accuracy: no unflagged comparative-accuracy assertion",
          not any("accurate" in s for s in asserting_sentences(answer))
          or bool((d.get("grounding") or {}).get("unsupported_claims")),
          f"asserting: {asserting_sentences(answer)[:1]}")

    print()
    print("3) WRONG ID SHAPE -- a real finding must not be called non-existent")
    d = ask("Did chargeback cb_00009 cause the shortfall on setl_20260708_005, "
            "or is that just an amount coincidence?")
    print(f"    tools: {[c['tool'] for c in d['tool_calls']] or 'none'}  {d['_s']:.1f}s")
    low = d.get("answer", "").lower()
    check("does not deny the finding exists",
          not any(p in low for p in ("no shortfall", "neither id appears",
                                     "not a queue finding", "recorded no")))
    check("engages with the actual finding",
          "setl_20260708_005" in low and ("cb_00009" in low or "chargeback" in low))

    print()
    print("4) SCALE -- a partial list must not be called complete")
    d = ask("List every bank row the engine could not account for, with the "
            "reason for each.")
    tools = [c["tool"] for c in d["tool_calls"]]
    print(f"    tools: {tools or 'none'}  {d['_s']:.1f}s")
    answer = d.get("answer", "")
    check("does not call why_not once per row", tools.count("why_not") <= 2,
          f"why_not x{tools.count('why_not')}")
    listed = answer.count("bank_000")
    claims_all = any(p in answer.lower() for p in
                     ("every bank row", "all 32", "complete list", "here are all"))
    check("lists all 32 or does not claim completeness",
          listed >= 32 or not claims_all,
          f"listed {listed}, claims complete={claims_all}")
    check("grounded", (d.get("grounding") or {}).get("ok"),
          str((d.get("grounding") or {}).get("problems")))

    print()
    if fails:
        print(f"AGENT ANSWERS: RED ({len(fails)}): " + ", ".join(fails))
        return 1
    print("AGENT ANSWERS: ALL CHECKS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
