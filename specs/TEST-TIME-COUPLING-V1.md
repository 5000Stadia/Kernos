# TEST-TIME-COUPLING-V1 — tests that decay with the wall clock

**Status:** Draft rev 2 (kreview round 1 = YELLOW; all named changes folded)
**Modules:** `tests/test_friction_response.py`, plus any sibling identified by
the audit below. Possibly `kernos/kernel/friction_response.py` if the audit
finds a production time-coupling rather than a test-only one.

## Why

`tests/test_friction_response.py::test_verify_archives_quiet_resolved` has been
failing for some time and was repeatedly reported as "pre-existing, unrelated"
during a long review cycle. It is unrelated to that work — but it is not inert,
and the reason it fails is worth fixing properly rather than re-pinning.

**Root cause: the test mixes real wall-clock file mtimes with hard-coded ISO
fixture dates.**

- It marks a friction signature `PENDING_VERIFICATION` at the literal timestamp
  `2026-06-04T00:00:00+00:00`.
- It sets the quiet report's mtime to `time.time() - 100*3600` — *real* clock.
- `verify_and_archive` computes `recurred_after` as "a report of this signature
  with mtime after the pending epoch".

Measured: the wall clock is now **62 days past** the fixture date, so the
"quiet" report's mtime is far *after* the pending marker. It is therefore
classified as a **recurrence**, marked `RECURRED_FAILED`, and never appears in
`resolved` — which is exactly the assertion that fails.

The test passed when written, in the window where real time was near the fixture
dates, and became a time bomb the moment the clock moved on. The production
logic is correct; the fixture's timeline is incoherent.

This matters beyond one test. A permanently red suite erodes the meaning of
"pytest green", which is the gate the whole spec-execution protocol leans on —
and this one has already been absorbed as background noise across nine review
rounds, which is precisely how a real regression would be missed.

## Design

### Part A — Fix the failing test

Make the fixture's timeline internally consistent: derive file mtimes **from the
same ISO instants the test asserts against**, not from `time.time()`.

- quiet report → mtime *before* the pending epoch;
- the different-signature "detector opportunity" report → mtime *after* the
  pending epoch but before `now_iso`.

The test then encodes the scenario it describes in its own comments ("mark
pending 25h ago, no new reports since") and is independent of when it runs.

### Part B — Audit by dependency, not by syntax

**Rev 1's audit was too syntactic and would have missed a live sibling.**
kreview identified `test_verify_marks_recurred_failed`: it contains **no
`time.time()` call at all**. It creates a report with `_seed_friction`, so
`write_text` assigns the *real* current mtime, and compares that against a
hard-coded pending ISO. Grepping for `time.time()` never finds it.

Worse, it currently **passes for the wrong reason**: the real mtime is far after
the fixture's pending epoch, so "recurred" is satisfied by clock drift rather
than by the behaviour under test. It would keep passing if recurrence detection
were broken.

So the audit criterion is **dependency, not syntax**: *every verification test
whose assertion depends on a report's mtime*, including **implicit** mtimes from
ordinary file creation.

Classification, with reasons recorded inline so the next reader does not
re-audit:

| Site | Verdict |
|---|---|
| `test_verify_archives_quiet_resolved` | **coupled** — the failure under repair |
| `test_verify_marks_recurred_failed` | **coupled** — implicit mtime; passes vacuously |
| other `verify_and_archive` tests | audited individually against the same rule |
| `test_open_items_have_no_ttl` | **inert** — governance items have no TTL for the mtime to interact with |
| `test_openai_codex.py` credential tests | **inert** — compares two filesystem mtimes within one live domain; the hard-coded `last_refresh` string does not participate |

**The audit must be proved by mutation**, per the standing rule: inject a
verification fixture with an unbound real mtime — both a `time.time()` form and
a plain `write_text` form — and demonstrate the audit fails. An audit that has
only ever seen a passing tree is the exact defect class this repo has already
been bitten by twice.

### Part C — Prevent recurrence

A guard that fails when a test asserts on the relationship between a wall-clock
mtime and a hard-coded instant. Deliberately **not** a blanket ban on
`time.time()` in tests — it is legitimate, and an over-broad rule gets disabled
by whoever it blocks.

**Ruled by kreview: C1 *and* C2, both binding — not either alone.** My rev-1
lean toward the helper alone was wrong for a reason worth keeping: **a helper is
opt-in**, so it does not make the coupling unrepresentable, it only makes the
correct thing convenient. The audit is what makes it enforced.

- **C2 — a required logical-timeline helper.** Derives ISO strings *and*
  epoch/mtime values from one base instant, so a fixture's timeline is
  internally consistent by construction.
- **C1 — a narrow executable meta-test.** Rejects verification fixtures with an
  unbound real mtime unless explicitly annotated inert with a reason.

Deliberately **not** a blanket ban on `time.time()` in tests — it is legitimate,
and an over-broad rule gets disabled by whoever it blocks.

## Acceptance criteria

1. `test_verify_archives_quiet_resolved` passes with **explicit logical mtimes
   for both reports** — the quiet same-signature report *before* pending, the
   different-signature opportunity report *after* pending and *before*
   `now_iso`.
2. `test_verify_marks_recurred_failed` sets an explicit logical mtime and
   therefore stops passing vacuously.
3. **A permanent parametrized logical-timeline regression** exercises a base
   instant far from the host date. Per kreview this is a cheap parametrized case
   rather than a global clock freezer — no process-wide time patching, no moving
   part in every unrelated run.
4. Both tests still **mutation-kill broken recurrence detection**: break the
   comparison in `verify_and_archive` and they must fail. Repaired, not neutered.
5. Every verification test whose assertion depends on a report mtime —
   **including implicit mtimes from file creation** — is fixed or annotated
   inert with a reason.
6. The Part B audit is **demonstrated to fail** on an injected coupled fixture,
   in both the `time.time()` and plain-`write_text` forms.
7. The C2 helper is used by the repaired tests, and C1 rejects a fixture that
   bypasses it without an inert annotation.
8. No production behaviour change. `verify_and_archive` intentionally compares
   report `st_mtime` against the pending ISO epoch, and the live caller passes
   `utc_now` on the same host clock — independently confirmed by kreview, so no
   production change is warranted by this evidence.
9. The full governance + friction suites stay green.

## Settled by kreview round 1

- Root diagnosis and the **test-only** classification are confirmed; kreview
  reproduced the exact failure independently. I asked for this to be checked
  rather than accepted, and it was.
- Part C is **C1 + C2 together**, both binding.
- The future-clock case is **permanent**, as a parametrized logical-timeline
  regression rather than a global freezer.

## Open review asks (rev 2)

1. **Scope of the C1 meta-test.** I am scoping it to *verification* fixtures
   (tests exercising `verify_and_archive` and its neighbours) rather than the
   whole suite, to keep it narrow enough not to be disabled. Confirm that scope
   is wide enough to be worth having.
2. **Annotation format for inert sites.** A comment is human-readable but not
   machine-checkable; a marker (e.g. a `pytest.mark`) is checkable but heavier.
   I lean a recognised comment token the audit parses. Rule if that is too
   fragile.
