# GOVERNANCE-STATE-DOCUMENT-V1 — collapse the governance lifecycle to one document

**Status:** Draft rev 6 (kreview rounds 1–5; case 6 aborts in v1 — one outcome, no 'either')
**Supersedes:** the three-artifact governance lifecycle shipped across
`b1db4b6..f38b31f`.
**Authority:** `docs/reference/governance-lifecycle-failure-state-enumeration.md`
— its 19 acceptance criteria are this spec's acceptance criteria, not a
restatement of my own assumptions.
**Modules:** `kernos/kernel/friction_response.py`,
`kernos/kernel/self_maintenance_review.py`, `kernos/messages/handler.py`
(`_handle_dump`), `tests/test_governance_items.py`, `tests/test_handler.py`.

## Why

Nine review rounds found nine reachable failure families in the governance
queue. Every fix was correct for the state last constructed and silent about its
neighbours; the last three P0s were each introduced by the fix for the previous
one. The reviewer's diagnosis is the reason for this spec rather than a tenth
patch:

> "The ninth state is the source lifecycle racing the audit lifecycle; the
> current lock protects only the latter."

One logical occurrence is currently spread across three artifacts — the open
item (S), the archive (A), and the closure audit (M) — which must agree at every
interleaving. Ordering, compensation, stable filenames, atomic replacement and a
manifest-only lock each protected one edge and left another reachable.

**This spec does not add a tenth guard. It removes the artifacts.** One
atomically replaced document holds both open items and retained closed history.
Most of the nine families become *unrepresentable* rather than *guarded*: there
is no archive to validate, no path to confine, no manifest to race, and no
source lifecycle separate from the audit lifecycle.

**Non-goals.** Changing what a governance item *means*, weakening the
human-gate, changing the deterministic-only v1 scope (coverage gap remains the
sole producer), or altering the `/dump` recovery surface's contract.

## Design

Single file: `diagnostics/governance/state.json`, replaced atomically.

```jsonc
{
  "schema_version": 1,
  "migration_notes": [ { from_format, decision, occurrence, note } ],   // bounded, structured, validated
  "open":   { "<signature>": { occurrence, signature, title, condition,
                               payload[], opened_iso, last_seen_iso,
                               human_gated } },
  "closed": [ { occurrence, signature, title, payload[], opened_iso,
                closed_iso, resolving_condition, human_gated } ]
}
```

- **Closure is a single write**: move the entry from `open` to `closed` and
  replace the document. There is no second artifact to keep in step, so criteria
  1 and 2 hold by construction and "shadow archive, never delete" becomes
  *retained closed state* rather than a moved file. The lock file is
  coordination, not a second lifecycle artifact.

### Compare-and-close (kreview P0-1)

**`close()` is occurrence-qualified**, not signature-qualified:

```python
close_governance_item(data_dir, *, signature, expected_occurrence, ...) -> CloseResult
```

**Returns an enum, not a bool** (kreview round 2). A boolean cannot distinguish
"already done" from "refused because something newer is open", and the caller
must not treat those alike:

| Result | Meaning |
|---|---|
| `CLOSED` | this occurrence was closed by this call |
| `ALREADY_CLOSED` | this exact occurrence was already in `closed` — idempotent success |
| `OCCURRENCE_MISMATCH` | a **different** occurrence is open; nothing was touched |
| `NOT_OPEN` | no open entry for this signature |
| `STATE_ERROR` | unreadable/corrupt/unknown-version; nothing was touched |

**Precedence, evaluated under the lock** — `ALREADY_CLOSED` and
`OCCURRENCE_MISMATCH` can both be true after the acceptance-23 schedule, so the
order is part of the contract, not an implementation detail:

1. invalid state or invariant conflict → `STATE_ERROR`
2. expected occurrence found in `closed` → `ALREADY_CLOSED` **even if a newer
   occurrence is open**
3. nothing open and expected absent → `NOT_OPEN`
4. a different occurrence is open, expected absent → `OCCURRENCE_MISMATCH`
5. exact open match → close → `CLOSED`

Rule 2 ahead of rule 4 is the point: an ambiguous retry **acknowledges A** while
leaving B untouched, and the next scan evaluates B independently.

On `OCCURRENCE_MISMATCH` **the caller does nothing clever in that pass.** The
next scan re-evaluates the real condition, re-reads the current occurrence, and
may issue a new qualified close only if the condition is still clear.

Rev 1 keyed the transition by signature alone and described a retry as
"observing the occurrence already in `closed`". That is reachable and wrong:

> occurrence A is atomically closed but the acknowledgement fails → upsert opens
> recurrence B → the retry `close(signature)` closes **B**.

An ambiguous retry would absorb a brand-new recurrence — the central
lost-recurrence family, recreated in the design meant to eliminate it. Binding
the request to an expected occurrence removes it:

- proceed only if `open[signature].occurrence == expected_occurrence`;
- return **idempotent success** only when *that exact occurrence* is already in
  `closed`;
- if a **newer** open occurrence is present, leave it untouched and report the
  mismatch — never close it.

The caller (`self_maintenance_review`) reads the occurrence it observed and
passes it, so the close it requests is the close it gets.
- **No path is ever derived from persisted state** (criterion 13). The escape
  that could delete the only copy is not merely blocked — it has no
  representation.
- **Occurrence identity** stays an explicit persisted field, and a recurrence
  always mints a new one, including within one clock tick (criterion 3) by
  mixing in the predecessor id.

### Transaction boundary

Every mutation — upsert, close, migration — runs the **entire** read-decide-write
under one mandatory cross-process lock scoped to the document (criterion 5).
**Failure to acquire the lock fails closed; there is no unlocked fallback.** The
current best-effort `flock` that proceeds unlocked is removed: correctness now
depends on serialization, so silently degrading it is the exact
loud-fail-over-silent-degradation violation this repo already rejects elsewhere.

This closes families 9.1 and 9.3 directly — the lock boundary becomes the
invariant boundary, and the check-then-act window between "observed ABSENT" and
"appended row" no longer exists because the decision is re-made inside the lock.

### Write protocol

Unique temp file → validate the complete candidate document parses and preserves
**every prior closed entry AND every unrelated open entry** → `fsync` temp →
atomic `replace` → `fsync` the containing directory (criterion 9). Validating
only closed history would let a write silently drop another signature's open
item. Durability is claimed only to the extent it is actually implemented; the
docstring states exactly what is guaranteed.

### Failure semantics

- Failure *before* replacement leaves the previous document authoritative and
  byte-unchanged (criterion 10).
- Failure reported *after* replacement is completion-ambiguous but safe: the
  retry observes the occurrence already in `closed` and returns success without
  duplicating it (criteria 7 and 10).
- Unreadable, corrupt-interior, or unknown-`schema_version` documents **fail
  closed with no write** (criteria 4 and 11).
- **Reads never mutate** except an explicit migration under the lock
  (criterion 12) — the current in-passing torn-tail repair inside a read is
  removed.

### Migration (kreview P0-2)

Idempotent, versioned, under the lock. Rev 1 said only "no prior state → fresh
empty document", which would have **discarded a normal legacy open item** —
losing exactly the findings this feature exists to keep. Every direct-parent
S/A/M combination gets a deterministic, individually tested outcome:

All **eight** S/A/M combinations, explicitly. Rev 2 said "every combination" and
listed five; the gaps were not cosmetic — two of them can lose the only
surviving payload once `state.json` becomes authoritative and legacy is ignored.

| # | S | A | M | Outcome |
|---|---|---|---|---|
| 1 | – | – | – | fresh empty document |
| 2 | ✓ | – | – | → `open`, identity and payload preserved |
| 3 | – | ✓ | – | closure never committed. If A is a **complete, validated** governance source copy → recover as **`open`**. If partial, unreadable, or identity unprovable → **abort, no write** |
| 4 | – | – | ✓ | **abort** — a row alone has no recoverable payload; never synthesise an incomplete closed entry |
| 5 | ✓ | ✓ | – | closure never committed → `open` from **S** (A is an uncommitted attempt) |
| 6 | ✓ | – | ✓ | **always aborts in v1** — see below. M cannot furnish a complete closed record without an archive, and equality cannot prove S is not a raced recurrence |
| 7 | – | ✓ | ✓ | → `closed`, full final payload and identity recovered from A and **validated** against M |
| 8 | ✓ | ✓ | ✓ | divergent on any lifecycle field → `closed` (A/M) **+** S as a new `open`. Identical → **abort**. See below |

**Cases 6 and 8 are where I kept being wrong, in progressively subtler ways.**
Rev 2 unconditionally dropped S. Rev 3 dropped S when its occurrence and payload
matched A. kreview refuted rev 3 with a schedule that defeats content equality:

> copy A from S at t1 → a concurrent upsert re-detects the same condition and
> writes the **same payload** at t2 (M is still absent, so it preserves the old
> occurrence and may change only `Last-seen`) → close commits M for the t1
> snapshot → retirement fails → migration sees equal occurrence and payload and
> drops S as a duplicate.

A live recurrence is lost *while payload equality passes*. And within one clock
tick an identical redetection may leave **no distinguishable field at all**.

The real problem is epistemic, and it is worth stating plainly because I had it
backwards twice: **equality is consistent with both hypotheses.** "S matches A"
is equally explained by "S is merely retirement-pending" and by "the condition
was re-observed identically during close". Direct-parent state does not contain
the evidence to prove the negative claim that no recurrence occurred, so
equality is **not proof** and must never be treated as such.

Migration is therefore bound to the evidence it actually has:

- **Semantic divergence spans every lifecycle field** — `occurrence`, `payload`,
  `Last-seen`, `condition`, `title`, and the human-gate marker — not merely
  occurrence and payload. *Any* divergence → preserve the committed A/M closure
  **and** carry S as a new **open** occurrence — **but only in case 8, where A
  supplies the historical snapshot.** Case 6 has no archive and therefore cannot
  satisfy this rule; it aborts (below). This qualification is load-bearing: the
  unqualified version of this sentence, left standing in rev 5, still authorized
  the unsafe outcome the case-6 section was written to forbid.
- **Identical → ABORT**, no state write, legacy left authoritative — unless a
  trusted direct-parent phase marker proves S was not rewritten after the
  archived snapshot. No such marker exists in `f38b31f`, so in practice this
  aborts.
- **Case 6 (S+M, no A) takes the same rule.** M's `final_payload` plus an equal S
  can prove the payload is *available*; it cannot prove S is not a raced
  recurrence. Without trusted phase evidence: abort.

#### Case 6 divergence must also be RECONSTRUCTABLE (kreview round 4)

Rev 4 said divergence in case 6 yields "`closed` (from M) + S as a new `open`".
That over-promised, because **the direct-parent M row is not a complete closed
record.** The live writer stores `governance_txn`, `governance_signature`,
`opened_iso`, `closed_iso`, `resolving_condition`, `final_payload` and
`archive` — but the new `closed` schema also requires `title` and
`human_gated`, and case 6 has **no archive** to recover them from.

The only other on-disk source is S — which, on the divergent branch, *is
recurrence B*. Borrowing B's values to describe A's historical closure would
fabricate history, and it is worst exactly where it matters: when `title` or the
human-gate marker **is** the field that diverged.

**V1 DECISION: case 6 aborts unconditionally.** kreview round 5 was right that
rev 5 left the predicate to the implementation ("*if* title and human_gated are
declared immutable derivations…") while the acceptance criterion accepted
"closure+open **or** abort". That is not a test oracle — it is a spec that
cannot be failed.

So this spec picks kreview's option (b) rather than (a):

- **Case 6 → abort, no state write, legacy left authoritative.** Always. One
  outcome, testable exactly.

Why (b) over enumerating an authoritative registry: it is true *today* that
`human_gated` is constant and the sole producer's `title` is fixed — but those
are **incidental facts about the current code**, not versioned invariants.
Elevating them into migration-load-bearing guarantees would mean a future second
producer silently breaks historical reconstruction. Given the verified
no-live-artifact precondition, aborting costs nothing real and keeps v1 honest.
A later version may add registry-derived reconstruction as a deliberate,
versioned invariant.

**Never a synthesized closed row** remains the governing rule; v1 simply reaches
it by refusing the case rather than by qualifying it.

Case 8 is unaffected: it has A, and therefore the full snapshot.

A conservative ambiguous-recovery state could replace abort, but it would have
to be explicit and human/re-scan resolvable — silently choosing `closed` is the
unsafe option. Given the verified no-live-artifact precondition, **aborting on
ambiguous constructed parent states is the smaller v1 design** and is what this
spec adopts.

| Manifest condition | Outcome |
|---|---|
| M torn tail | dropped (an incomplete append was never durable), recorded in migration notes, never silently |
| M corrupt interior | **abort, no write, loud error** — committed history is never discarded |
| unknown `schema_version` | abort, no write |

**Governing rule for every abort:** leave legacy state authoritative and write
nothing. **An empty or partial new document must never become authoritative over
the sole surviving copy of a payload.**

**Precedence once `state.json` exists** (criterion 12): the valid document is
authoritative and legacy files become read-only and are **ignored** — never
re-imported. Migration runs once, records that it ran, and a crash mid-migration
retries safely because the import is idempotent and the replace is atomic.

**Format scope.** Formats at or after `f38b31f` (the `_governance_manifest.jsonl`
era) are supported. The older *shared* `_manifest.jsonl` era is also imported,
with **strict row filtering** — a row must carry `governance_txn` **and** a
`governance_signature` to be considered, so friction-resolution rows in that
shared file can never be imported as governance closures.

Verified precondition: no governance artifacts exist in any deployment today, so
migration correctness is exercised against constructed parent-format states in
*tests*. That is a fact about deployments, **not** evidence the design is safe.

## Acceptance criteria

The 19 criteria in
`docs/reference/governance-lifecycle-failure-state-enumeration.md` §"Rewrite
acceptance criteria" are adopted verbatim and in full. Additionally:

20. Every one of the nine documented failure families is either **unrepresentable**
    in the new design, or has a test constructing it and asserting the safe end
    state. The spec's own claim of "unrepresentable" must be justified per family
    in the implementation notes, not asserted globally.
21. `unassigned_modules()` remains empty on a clean tree, and the coverage-gap
    producer still opens, updates and closes an item end-to-end.
22. No behavioural change to the human-gate, the fail-closed class parser, or the
    quarantine surface.
23. **Compare-and-close is constructed, not assumed.** The exact schedule —
    close A → replace succeeds → acknowledgement fails → upsert opens recurrence
    B → retry close(signature, expected=A) — returns `ALREADY_CLOSED`, ends with
    B still open, one closed entry for A, and no second closure. The full
    `CloseResult` precedence order is asserted, including the case where
    `ALREADY_CLOSED` and `OCCURRENCE_MISMATCH` are simultaneously true.
23a. **Case 6 aborts — exactly one expected outcome per test.** Constructed
    cases where S diverges *only* in `title`, *only* in the human-gate marker,
    where M lacks `final_payload`, and where S and M are identical. **All four
    assert abort with no state write and legacy left authoritative.** No test
    accepts "either outcome".
23b. **Migration ambiguity fails closed.** kreview's schedule — copy at t1,
    same-payload re-detection at t2, M committed for the t1 snapshot,
    retirement failed — must **abort with no state write**, not merge. Asserted
    for both case 6 and case 8, including the same-clock-tick variant where no
    field differs at all.
24. **Every migration row in the table above has its own test** built from that
    on-disk state, asserting the exact resulting document. Re-running migration
    over an existing valid `state.json` is a no-op that does not re-import
    legacy files.
25. **Candidate validation rejects a write that drops an unrelated open entry**,
    proved by mutation.
26. **Document size and write/lock latency are observable**, and the over-limit
    path fails closed rather than truncating.

## Settled by kreview round 1

- **Single document is the right primitive.** Closure is genuinely one atomic
  replacement; the lock file is coordination, not a second lifecycle artifact.
- **Document-wide lock** for every read-decide-write. Per-signature locks are
  *incorrect* for writers replacing one shared document.
- **Lock acquisition failure fails closed.** My rev-1 justification was
  self-contradictory: I wrote that "refusing to record is worse than the race"
  and then chose fail-closed anyway. kreview corrected the direction — **the
  race is worse than refusing to record.** An unlocked fallback is never
  acceptable; an unsupported platform needs a real platform-specific lock or a
  loud refusal.
- **No compaction in v1**, and deletion is not invented here. Retained history
  is acceptable provided **size and lock/write latency are observable**, with
  **fail-closed over-limit behaviour** rather than silent truncation.

## Open review asks (rev 2)

1. **Compare-and-close mismatch reporting.** When `expected_occurrence` does not
   match a newer open occurrence, I return a distinguishable failure rather than
   a bare `False`, so the caller can tell "already closed" from "superseded".
   Confirm the caller should simply skip and re-evaluate next scan rather than
   attempt anything cleverer.
2. **Orphan archive with no source and no row.** I record it as an observable
   anomaly and refuse to synthesise a closed entry, because a closure that never
   committed did not happen. Confirm that is preferable to importing it as
   closed on the grounds that the payload exists.
3. *(settled — kreview round 3)* The case 3 / case 5 asymmetry is **intended**:
   it is **source precedence**. With no M, closure never committed. If S exists
   it remains the authoritative current occurrence and the orphan A is only an
   uncommitted snapshot. If S is absent, a complete validated A is the sole
   surviving payload, so conservative recovery as open is correct. Partial or
   unprovable A still aborts.
