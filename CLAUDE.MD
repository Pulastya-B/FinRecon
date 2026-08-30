# Project constraints

## Frozen — do not modify
- finrecon/generator.py, schema.py, money.py, bizcal.py
- config/rates.yaml, config/defects.yaml
- data/seed42/, data/seed99/
Changing generator RNG call order breaks byte-reproducibility and
invalidates the held-out seed comparison.

## Money
- Integer paise everywhere. No float touches a money value.
- Read CSVs with dtype=str on money columns, convert explicitly.
  pandas float64 inference is the exact bug this project avoids.

## Seeds
- data/seed42 = development. Debug freely.
- data/seed99 = was held out. SPENT on 30 Aug 2026: run once against a frozen
  engine, scored, and published (coverage 52.40%, precision 100.00% on 524
  claims, recall 100%, attribution 100%). It is now a normal servable dataset
  and appears in /api/seeds labelled "HELD OUT, scored 30 Aug".
  Its value as a held-out set is gone and cannot be restored, so:
  - Do NOT tune anything against it. Never change a rule, threshold or
    published number because of what seed 99 shows.
  - Do NOT re-seal it. Hiding a dataset whose result is already on the
    Evidence page reads as concealment, not discipline.
  - A NEW held-out seed must be freshly generated and never run.

## ground_truth.json
- Readable ONLY by eval/. The matcher must never open it.
- If it leaks into the reconciliation path, precision is meaningless.

## Verification
- python tests/test_invariants.py  and
  python eval/validate.py --data data/seed42
  must both pass after every change.

## Style
- One tier per module. No monolithic main.py.
- Every design decision gets a one-line comment saying WHY.

## Incident capture

Append to INCIDENTS.md (not FAILURES.md) whenever any of these happen:
- A test or validator check fails
- An assertion or exception fires during a run
- A metric moves unexpectedly (precision drops, coverage jumps)
- You change an approach after the first one didn't work
- You discover a wrong assumption in existing code

Format — facts only, no narrative:

  ## [timestamp] one-line symptom
  Observed: <what the output actually was>
  Expected: <what it should have been>
  Investigated: <what was checked, in order>
  Root cause: <if found; "unknown" if not>
  Change made: <files touched, what changed>
  Verification: <which checks were re-run and their result>

Do NOT write to FAILURES.md. Do NOT editorialise. Do NOT skip an
incident because it was resolved quickly.