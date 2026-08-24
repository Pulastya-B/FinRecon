# Failure log

Started day one, on purpose. The application asks "what broke, and how you got
out" and says it is the answer they read first. That cannot be reconstructed
convincingly on the last day — write entries as they happen.

Log real engineering failures with what you actually tried. `npm install`
failing is not a failure worth logging.

---

## 2026-08-21 — pandas silently reintroduced floats into the money path

**What broke.** The invariant test asserting every amount is exact to two
decimal places failed on all three money columns, while the generator's own
arithmetic was provably integer-only end to end.

**What I tried.** First assumed the generator was wrong and traced
`apply_bps` by hand across several settlements. The integer arithmetic was
correct. Then checked the CSV on disk — also correct, `3464.00` as written.

**Root cause.** `pd.read_csv` inferred `float64` on the money columns at read
time. `"3464.00"` became `3464.0`, and the regex asserting two decimal places
failed against the repr. The generator was never at fault; the *test* had
reintroduced exactly the class of bug the project is built to avoid.

**Fix.** Money columns are now read with `dtype=str` and converted to integer
paise explicitly. Added a `MONEY_COLS` set and a `_read()` helper so no reader
can forget.

**What I'd do differently.** The float ban was enforced in the generator but
not at the ingest boundary. Boundaries are where the invariant actually needs
enforcing — internal discipline doesn't help if the data re-enters through a
door with type inference on it. The reconciliation engine's normalizer needs
the same treatment.

---

## 2026-08-21 — ground truth silently disagreed with the CSVs for 1.5% of orders

**What broke.** Wrote an independent validator that recomputes every derived
quantity from the emitted CSVs without importing the generator. It immediately
failed one check on both seeds: `chain.settlement_id` did not match the
settlement recorded on that chain's own payments. 16 chains on seed 42, 11 on
seed 99.

**What I tried.** Dumped every chain with more than one payment and compared
its ground-truth settlement against the settlements its payments actually
landed in. The pattern was immediate: every failing chain was a duplicate
capture whose two payments sat in *different* cycles.

**Root cause.** A duplicate capture can straddle the capture-date cutoff — the
customer's retry crosses midnight, so the two payments bucket into different
capture days and therefore different settlement cycles, paid by different bank
credits. `Chain` modelled `settlement_id` and `bank_row_id` as scalars. The
generator wrote both settlements onto the chain in sequence and the second
silently clobbered the first. The domain is one-to-many; the schema said
one-to-one.

**Why it mattered more than the count suggests.** This is not a cosmetic
ground-truth defect. A matcher that *correctly* linked the first payment to
its real settlement would have been scored against the surviving second one
and counted as a **false positive**. The measured precision would have been
depressed by roughly 1.5 points by a bug in the oracle, and I would have spent
days hunting a matcher bug that did not exist.

**Fix.** `settlement_ids` and `bank_row_ids` are lists. Outcome precedence is
now an explicit ordering (`OUTCOME_PRECEDENCE`) rather than an artefact of
which generator stage happened to run first. Added a validator check asserting
`chain.settlement_ids` equals exactly the set of cycles its payments landed in,
plus a regression guard asserting multi-cycle duplicates are actually present
in the data.

**What I'd do differently.** The scalar felt obviously right when the schema
was written, because the mental model was "an order settles." Orders don't
settle — payments do, and an order can have more than one. I should have
derived the cardinality from the domain rather than from the common case.

---

## 2026-08-21 — carry-forward signs inverted; a cycle paid out more than it collected

**What broke.** After fixing the above, seeds 42 and 99 passed 119/119. Ran the
validator across six additional unseen seeds; seed 7 and seed 21 failed
`net == full settlement equation`.

**What I tried.** Printed every cycle with non-zero carry-forward. Seeds 42 and
99 had **none** — no cycle on either had refunds exceeding gross, so the entire
carry-forward path was dead code on both datasets I had been testing against.
Seed 7 had two such cycles, and the numbers were visibly absurd: a cycle with
gross ₹2,99,554 settled for ₹3,37,908.

**Root cause.** Both carry-forward terms carried the wrong sign.
`carry_forward_in` is a shortfall inherited from the previous cycle — a debt
the merchant owes — and must be **deducted**. It was being added. The generator
paid the merchant their own debt, then the validator, which had reimplemented
the same equation from the same wrong mental model, agreed.

**Fix.** Corrected to `net = gross − fee − tax − refunds − chargebacks −
withholding − carry_in + carry_out`, where `carry_out` is added back because
the payout is floored at zero rather than going negative. Added two structural
assertions that would have caught it regardless of sign convention: a cycle
that carries forward must pay out exactly zero, and **no cycle may ever settle
above its own gross**.

**What I'd do differently.** Two things. First, testing against two seeds is
not testing — a rare branch that neither seed exercises is untested code
wearing a passing badge. The sweep across 28 seeds now runs before any number
is reported. Second, the validator reimplemented the equation from the same
assumption as the generator, so on this one check its independence was
illusory. The assertions that actually caught it are the ones phrased as
domain invariants ("never pay more than you collected") rather than as
restatements of the formula.

---

## 2026-08-21 — two validator bugs that would have condemned valid data

**What broke.** With the generator correct, seeds 3, 13, 101 and 99 still
failed. All three failures were in the validator, not the data.

**Root causes.**

*Asserting a stochastic event must occur.* `settlement_missing_in_bank` fires
at 2% across ~46 cycles, so the expected count is ~0.9 and drawing zero is
entirely normal. The check asserted `len(missing) > 0`. Seed 7 drew zero and
was marked invalid.

*Deriving settlement-level facts from chain-level records.* "Which settlements
are missing from the bank" was computed by unioning the `settlement_ids` of
every chain carrying the flag — but a chain straddling two cycles where only
one is missing contributes **both**, inflating the count. This also produced a
phantom 5-sigma rate anomaly on seed 3 (observed 6 against an expected 0.9);
measuring across 60 seeds showed the true rate is 0.0178 against 0.0200
configured, well within tolerance. The anomaly was entirely the over-count.

*Ordering by an ambiguous key.* The carry-forward chain check sorted cycles by
`settled_on`. Weekend captures collapse onto a shared settlement date — 12
collisions on seed 101 — so date order does not determine cycle order.

**Fix.** Ground truth now records `bank_row_id` and a dense `sequence` at
settlement level, so settlement-level questions are answered by settlement-level
records instead of being reconstructed from chains. The validator sorts by that
sequence. Stochastic assertions are now conditional on the event having fired.

**What I'd do differently.** The first two share a root: I was inferring facts
about one entity from records about a different entity. If the scorer needs a
settlement-to-bank link, ground truth should state it, not leave it to be
reassembled from order-level rows — reassembly is where the ambiguity creeps
in. Worth carrying into the matcher: emit the fact at the level it is true.

---

## 2026-08-22 — my reproducibility guarantee was platform-dependent, and I explained the failure away twice

**What broke.** `tests/test_invariants.py` check 8, "seed 42 reproduces
byte-identically", failed on Windows. The other seven checks passed. I called it
an environment artefact and moved on. It failed again on the next change, and I
explained it again.

**What I tried.** Byte-compared a fresh generation against the checked-in
`data/seed42`: all seven files differed. Stripped `\r` from both sides and
compared again: all seven were **content-identical**. So the generator was
deterministic in everything except the bytes the test actually measures.

**Root cause.** Three separate platform dependencies in the write path, none of
which touch the RNG:

- `DataFrame.to_csv` defaults `lineterminator` to `os.linesep` — LF on Linux,
  CRLF on Windows.
- `Path.write_text` opens with `newline=None`, which also translates to
  `os.linesep`, and picks up the *locale* codec rather than a fixed one. That
  covered `ground_truth.json`, which check 8 also compares.
- Separately, `scripts/generate.py` prints `format_inr()`, which contains
  U+20B9. Under `subprocess.run(capture_output=True)` Windows stdout is cp1252,
  so it died with `UnicodeEncodeError` *after* writing the data correctly — the
  test saw a non-zero exit and a traceback rather than a clean comparison.

**Why it mattered more than a newline should.** The README claims byte-identical
output on any machine, and that claim was simply false on half of them. The
held-out-seed comparison is this project's answer to "one number proves
nothing"; if a reviewer clones the repo on Windows and the determinism test goes
red, that argument collapses at exactly the moment it is load-bearing. And a
check that fails every run has stopped being a signal — the next real regression
would have hidden behind it, wearing the same explanation.

**Fix.** `lineterminator="\n"` on all six `to_csv` calls;
`newline="\n", encoding="utf-8"` on the `ground_truth.json` write;
`sys.stdout.reconfigure(encoding="utf-8")` in `scripts/generate.py`. Regenerated
both seeds and compared SHA-256 against the pre-change files: all 14 identical,
confirming the content was never wrong and only the framing bytes were. Check 8
now passes in a default shell with no `PYTHONIOENCODING` workaround.

**What I'd do differently.** Two things, and the second is the real one. First,
a guarantee stated in prose and enforced by a test that only ever ran on one OS
is not enforced. Second: I diagnosed this correctly the first time — I knew it
was CRLF, I proved the content matched — and then wrote it up as a known
environment quirk instead of spending five minutes on it. That is how a defect
gets laundered into a feature of the repo. The rule I'm keeping: if a test fails
and the answer is "that's just the environment", either remove the environment
dependency or delete the test. Leaving it red is the one option that costs
something later.

---

## 2026-08-22 — a decline tally reported one cause and its 23 consequences as 24 peers

**What broke.** Tier 1's first funnel report listed `AMOUNT_MISMATCH 24` near
the top of its decline reasons — on a dataset containing 46 settlements. Read
literally, over half the batches were failing an amount comparison, which would
have meant the exact-equality join was the wrong design.

**What I tried.** Grouped the decline dictionary by entity kind before believing
it. The 24 resolved into **1 settlement and 23 orders**.

**Root cause.** `declines` was a single flat `dict[entity_id, reason]` spanning
orders, settlements and bank rows. One settlement failed the amount check —
a rounding-drift batch off by a paisa — and the 23 orders inside that batch each
recorded the same reason string as their own decline. The tally then counted the
cause and its blast radius as peers. `NO_BANK_CREDIT_WITH_UTR 124` was the same
illusion: 6 settlements, plus 118 orders stranded behind them.

**Why it mattered.** It points the reader at the wrong work. "24 amount
mismatches" argues for widening the tolerance band — which would have traded
precision away for nothing. "One batch off by a paisa" is a single known defect
that Tier 3 is already designed to absorb. Same numbers, opposite conclusions.

**Fix.** `Decline` now carries `kind` and `caused_by`. `decline_breakdown()`
groups by entity kind, `root_causes()` excludes anything inherited, and the
funnel prints both — blast radius and root cause side by side, never summed.

**What I'd do differently.** This is the same mistake as the 2026-08-21 entry on
deriving settlement-level facts from chain-level records, which ended with
"emit the fact at the level it is true." I wrote that line and then, one tier
later, aggregated four entity types into one counter. Noting a lesson in a log
is not the same as building the thing that makes it unrepeatable — the counter
should have been per-entity from the start, because the hierarchy was already
known to be the sharp edge here.

---

## 2026-08-24 — four times the broken thing was the instrument, not the engine

**What broke.** Four separate investigations, each opened because a number
looked wrong, each initially read as a defect in the reconciliation engine.
None of them was.

*The reliability script measuring through its own ceiling.*
`eval/subset_reliability.py` exists to derive Part B's pool limit. After the
limit it produced was written back into `infer_membership` as
`MAX_POOL_SIZE = 20`, the script began reporting zero valid subsets at pools 40
and 80 — the search refusing before it started. The curve had no information
above 20 and looked like the search had collapsed. The script was confirming
its own answer.

*The banned-phrase verifier matching backspace bytes.* `verify_wording` was
written to reject prose asserting cause. It rejected nothing. The patterns had
been authored through a shell heredoc into a Python string, and `\b` collapsed
one layer too far: what reached disk was `r"^Hcaused by^H"`, with literal 0x08
bytes where the word boundaries should have been. Every pattern searched for a
control character that appears in no prose ever written. The verifier reported
clean because it could not fail.

*The scorer comparing a pair against a scalar.* `compound_shortfall` records
two causing items in `item_ids`; the scorer compared proposals against
`item_id`, which is null for a compound. CIRCUMSTANTIAL came back 0/7 — every
correct pair judged wrong against a null. The band looked broken. The
comparison was.

*The units bug in this session's replacement estimator.* The new empirical
counter took `acceptance_window_paise(2)`, which returns 5 — the COUNT of
accepting values, correct for `E = N·W/R` where W is the measure of the
accepting set. The counter treats its argument as a HALF-WIDTH, so it searched
±5 instead of ±2 and returned exactly double the real pair count. The first
measurement read 5.17 where the pool holds 2.40. It was caught only because
brute force disagreed with it.

**Root cause.** Nothing in common at the code level: a stale constant, an
escaping collapse, a schema change with one consumer updated, a units mismatch.
What they share is position. Each lived in the thing doing the measuring, and
in every case the measurement was the only evidence that anything was wrong —
so the instrument was simultaneously the accused and the only witness.

**Why it kept happening.** The engine is defended. Every tier has probes,
conservation checks, determinism assertions, and the ground-truth firewall.
The instruments had none of that, because their output *is* the check. There
was nothing behind them to disagree. Three of the four were caught only because
a second, independent computation happened to exist — brute force for the pair
count, the ambiguity results for the L4 band, ground truth for the scorer. The
verifier with no second opinion was the one that stayed broken longest, and it
stayed broken silently while reporting success.

**Fix.** Beyond the four repairs: every instrument now has something that
disagrees with it. `subset_reliability` passes `ignore_evidence_band=True` and
says why in the call. `verify_wording` uses plain substrings and a probe
asserts no pattern contains a control byte. The scorers compare item SETS and a
probe drives a compound case. The empirical estimator is asserted against
brute-force enumeration and against `_pair_candidates` on real pools.

**What I'd do differently.** When a number looks wrong, check the instrument
before the engine. Not because the engine is trustworthy — it has had real bugs
— but because the instrument is where a defect is invisible. A wrong engine
produces a wrong number that a measurement can catch. A wrong measurement
produces a wrong number that nothing catches, and it will be believed, because
believing it is what it is for.

The operational form: no instrument ships without a second, independent way to
produce the same number, even a slow and stupid one. Brute force over 578,350
pairs is not a technique anyone would ship, and it is the only reason today's
units bug did not become a published finding.

---

## Template

## YYYY-MM-DD — one-line summary

**What broke.**

**What I tried.**

**Root cause.**

**Fix.**

**What I'd do differently.**
