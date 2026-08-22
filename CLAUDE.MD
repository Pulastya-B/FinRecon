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
- data/seed99 = HELD OUT. Do not run against it. Do not read it.

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