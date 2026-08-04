# Governance Lifecycle Failure-State Enumeration

**Status:** Review handoff and rewrite acceptance criteria  
**Date:** 2026-08-04  
**Related spec:** [`SELF-REVIEW-SURFACING-INTEGRITY-V1`](../../specs/SELF-REVIEW-SURFACING-INTEGRITY-V1.md)  
**Review chain:** `b1db4b6` → `704dc56` → `d5a4538` → `9695ef2` →
`7db8043` → `0ec65aa` → `bae8ae9` → `51c8c04` → `f38b31f`

## Purpose

The governance queue exists so a human-gated finding cannot disappear after a
one-shot notification. Its core promise is stronger than ordinary best-effort
diagnostics:

> A finding remains durably open until its condition clears, and every closed
> occurrence has exactly one retained history entry.

Nine review rounds found reachable states that broke that promise. This
reference preserves those states independently of the patches that exposed or
attempted to fix them. It is intended to serve as:

- acceptance criteria for a rewrite;
- a regression-state catalog if the current design is retained; and
- a warning against reviewing this subsystem only as a sequence of local file
  operations.

Every state below was reproduced against the immutable commit named for its
round. These are observed failure modes, not speculative concerns.

At handoff, the implementation owner reported no live exposure: the clean tree
had no unassigned modules, and neither deployment had governance items,
archives, or manifests. That makes this a latent design defect rather than an
active incident. It does not weaken the acceptance criteria.

## System model

The reviewed implementation distributed one logical occurrence across three
artifacts:

- **S — source:** the open governance item, which is authoritative while open;
- **A — archive:** the retained payload after closure; and
- **M — audit:** the closure row linking identity, payload, condition, and
  archive.

The intended transition was:

```text
OPEN:   S present, A absent,  M absent
CLOSED: S absent,  A present, M present and resolvable
```

The required invariants are:

1. At least one authoritative copy exists at every point.
2. One occurrence produces exactly one closure history entry.
3. An audit references the archive containing that occurrence's final payload.
4. A recurrence is a new occurrence, including during recovery of the prior
   occurrence.
5. Any reported failure remains safely and automatically retryable.
6. Unknown or corrupt state fails closed without mutating S, A, or M.
7. Concurrent operations preserve the same invariants as sequential ones.

## The nine failure-state families

Each numbered family corresponds to one RED review round. Some rounds exposed
multiple interleavings with the same underlying structural error.

### 1. Move-first closure loses the recovery surface

**Commit:** `b1db4b6`

**Starting state:** S exists; A and M do not.

**Interleaving:**

1. Move S to a timestamp-derived A.
2. Append M on a best-effort basis.
3. The append fails, or two closures resolve to the same truncated timestamp
   and the second move overwrites the first A.

**Observable end condition:** S is absent. Either A exists without M and is no
longer enumerated by the open queue, or an earlier A has been overwritten while
its history is still implied. The finding is stranded or destroyed.

**Violated assumption:** A successful filesystem move was treated as closure,
while audit persistence and collision-free identity were treated as secondary.
Moving the only authoritative copy before the logical transition commits is a
destructive partial transaction.

### 2. Audit-first closure records an effect that never happened

**Commit:** `704dc56`

**Starting state:** S exists; A and M do not.

**Interleaving:**

1. Append M first so an audit cannot go missing.
2. Moving S to A fails.

**Observable end condition:** S remains open, M says it closed, and A is
absent. A retry may append another closure row for the same opening.

**Violated assumption:** Recording intent was treated as equivalent to
committing the referenced effect. Reversing the order moves the inconsistency
window; it does not remove it.

### 3. A compensating move is not a transaction

**Commit:** `d5a4538`

**Starting state:** S exists; A and M do not.

**Interleaving:**

1. Move S to A.
2. Appending M fails.
3. The compensating move from A back to S also fails.

**Observable end condition:** S is absent, A is unaudited, M is absent, and
the open-item reader cannot find the occurrence. The rollback path creates a
second failure point capable of stranding the item.

**Violated assumption:** A best-effort inverse operation was treated as
rollback. Compensation is safe only when the compensation itself is guaranteed
or when the original authoritative copy was never removed.

### 4. Loss-free copy is not idempotent recovery

**Commit:** `9695ef2`

**Starting state:** S exists; closure proceeds as copy A, append M, then unlink
S.

**Interleaving:**

1. A and M commit.
2. Retiring S fails, or M fails and cleanup of the unaudited A fails.
3. The next attempt derives a new timestamped target and restarts closure.

**Observable end condition:** one opening creates multiple archives and/or
multiple audit rows. Cleanup failure is loss-free, but retry is not a resume;
it is a new transaction with a new identity.

**Violated assumption:** Keeping S preserved data but did not make the three
phases recognizable. Retry safety requires a stable occurrence/transaction
identity and phase detection, not only a safe ordering.

### 5. Filename existence and boolean audit lookup are not phase evidence

**Commit:** `7db8043`

**Starting state:** a stable transaction ID is derived from signature and
opened time; retry uses the same archive filename.

**Interleavings:**

- M is unreadable after A and M committed but S retirement failed. The read
  error is interpreted as “not recorded,” so retry appends a duplicate M.
- Copy writes a truncated A and raises. Retry trusts `A.exists()` and commits M
  for the partial file.
- An unaudited orphan A contains payload A; S is upserted to payload B. Retry
  trusts the old filename and records final payload B while retaining archive
  payload A.
- A and M for occurrence A commit; S retirement fails; payload B recurs into
  the still-present source. Retry recognizes A's old transaction and deletes S,
  absorbing B into the prior closure.

**Observable end condition:** duplicate audit rows, a truncated archive, audit
and archive payload mismatch, or total loss of a recurrence.

**Violated assumption:** Existence was treated as validity, read failure as
absence, and opened-time identity as sufficient to distinguish recurrence.
Phase evidence must be tri-state and content/occurrence aware.

### 6. Persisting occurrence identity does not remove ambiguous completion

**Commit:** `0ec65aa`

**Starting state:** S now carries `Occurrence:`; unaudited archives are
atomically refreshed from S; close uses a tri-state audit lookup.

**Interleavings:**

- Upsert still collapses audit `UNKNOWN` to not-recorded. If A and M committed,
  S retirement failed, and M is temporarily unreadable when B recurs, upsert
  overwrites S under A's occurrence. A later close sees A recorded and deletes
  B.
- Appending M fully succeeds and flushes, then reports an error. Error cleanup
  removes A. Retry sees M recorded and retires S, leaving an audit with no
  archive.
- A parent-format in-flight item has no persisted occurrence and uses
  `sha256(signature|opened)`. The new fallback uses
  `sha256(signature|opened|)`, so retry creates a second transaction for the
  same opening.

**Observable end condition:** lost recurrence, M present with both S and A
absent, or duplicate history across an upgrade.

**Violated assumption:** Tri-state semantics were applied to close but not to
every reader; an exception from a write was treated as proof that nothing
landed; and a durable identity derivation was changed without an on-disk
compatibility rule.

### 7. Fail-closed can become permanently stuck, and references must resolve

**Commit:** `bae8ae9`

**Starting state:** upsert handles audit `UNKNOWN`; ambiguous append preserves
both S and A; recorded-but-missing A can be rebuilt; the initial occurrence ID
matches the legacy derivation.

**Interleavings:**

- A legacy in-flight source has no `Occurrence:` but already has committed A
  and M. B recurs before the first post-upgrade close. Upsert skips lookup when
  the field is absent, assigns the legacy ID to B, and the next close absorbs B.
- An append writes only a prefix of its JSON row and raises. The malformed tail
  is `UNKNOWN` on every later scan, so closure can never complete without
  manual manifest surgery.
- A recorded row declares archive `gone.md`. Recovery creates a different
  canonical filename, retires S, and leaves M pointing to the still-missing
  `gone.md`.

**Observable end condition:** lost recurrence, an open item that is permanently
unclosable, or a closed item whose audit link never resolves.

**Violated assumption:** Missing persisted identity was treated as no prior
identity; “fail closed” was considered sufficient without a recovery
transition; and creating some archive was treated as restoring the archive the
audit actually names.

### 8. Atomic replacement is not a shared transaction or a trust boundary

**Commit:** `51c8c04`

**Starting state:** M is rewritten through temp-plus-replace; torn legacy tails
are repaired; the row's archive field is authoritative.

**Interleavings:**

- M declares `../friction/<source-name>` as its archive. The path resolves to
  S, `exists()` passes, archive creation is skipped, and close unlinks the only
  copy.
- Governance reads the shared manifest. The existing friction-resolution
  writer appends a row. Governance replaces from its stale snapshot, erasing
  the committed friction row.
- The first read sees the transaction recorded. A second read for its archive
  name fails or changes. Recovery silently falls back to a canonical name,
  retires S, and leaves the original audit reference broken.

**Observable end condition:** deletion of the only copy through metadata,
loss of an unrelated audit row, or M referencing no archive.

**Violated assumption:** Atomic visibility was mistaken for atomic
read-modify-write; on-disk metadata was allowed to steer filesystem operations
without confinement; and state plus metadata were read from different
snapshots.

### 9. Locking the audit does not lock the lifecycle

**Commit:** `f38b31f`

**Starting state:** governance has a separate manifest; its read-modify-write
is locked; state and row come from one snapshot; archive paths are confined to
plain basenames.

**Interleavings:**

- Close reads and archives occurrence A, then pauses before M commits. A
  concurrent upsert sees M absent and rewrites S with recurrence B under the
  same occurrence. Close writes A's audit and unlinks S, which is now B.
- M declares `_governance_manifest.jsonl` itself as the archive. It is a safe
  basename and regular file, so existence passes and close deletes S without
  retaining its payload.
- Two closes of the same occurrence both check `ABSENT` outside the lock. They
  enter the locked write one after another, and each unconditionally appends
  the same transaction row.
- If `flock` fails, the lock helper deliberately continues unlocked, restoring
  the lost-update race correctness now depends on excluding.

**Observable end condition:** lost recurrence, no actual archive despite a
successful close, duplicate rows for one occurrence, or nondeterministic lost
updates when locking is unavailable.

**Violated assumption:** The invariant spans source, closed history, and audit,
but only manifest replacement was serialized. Path confinement proves location,
not artifact identity. A check outside a lock is not protected by the later
locked action, and a correctness lock cannot be best-effort.

## Structural conclusions

The sequence supports conclusions broader than any individual patch:

1. **Three artifacts require a real transaction protocol.** Ordering,
   compensation, stable filenames, atomic replacement, and a manifest-only lock
   each protect one edge while leaving another interleaving reachable.
2. **Existence is not identity.** A file can exist yet be partial, stale,
   unrelated, a control file, a symlink, or the source itself.
3. **Occurrence identity is domain state.** It must be persisted and migrated,
   not reconstructed opportunistically from time or filename.
4. **Fail-closed requires a recovery transition.** A state that preserves data
   but can never progress is safe against loss and broken for operation.
5. **Atomic replace is not serializable read-modify-write.** Every writer must
   share a mandatory transaction boundary, and the decision must be rechecked
   inside it.
6. **The lock boundary must match the invariant boundary.** Serializing M does
   not serialize changes to S or prove A's identity.
7. **Upgrade states are first-class states.** A format change is incomplete
   until live, partially completed transactions from the direct parent have an
   explicit outcome.

## Rewrite acceptance criteria

The proposed simplification is one atomically replaced state document containing
both open items and retained closed history. The exact schema may vary, but a
rewrite is not complete unless the following are true.

### Representation

1. One authoritative document represents both open and closed occurrences.
   Closure moves an occurrence from `open` to `closed` in one new document; it
   does not coordinate source, archive, and manifest files.
2. Closed entries retain the full final payload, signature, occurrence ID,
   opened/closed timestamps, resolving condition, and human-gated marker.
   “Shadow archive, never delete” is preserved as retained closed state.
3. Occurrence IDs are explicit persisted fields. A recurrence always receives a
   new ID, even if it arrives while the prior close is being retried or within
   the same clock tick.
4. The document has a versioned schema and an explicit, idempotent migration
   path. Unknown versions or corrupt interior state fail closed without writes.

### Transaction boundary

5. Upsert, close, and migration use the same mandatory cross-process lock over
   the complete read-decide-write operation. Failure to acquire the lock fails
   closed; there is no unlocked fallback.
6. A close and a concurrent upsert of the same signature are serializable: one
   happens first, and the later operation observes its committed result.
7. Two closes of the same occurrence are idempotent. They yield one closed
   entry and no error-dependent duplicate.
8. Concurrent operations on different signatures retain both results.
9. The write uses a unique temp file, validates the complete candidate document,
   atomically replaces the authoritative file, and provides the durability
   guarantees claimed by the API. If crash durability is claimed, fsync the
   temp file and containing directory as required by the target platform.

### Failure semantics

10. Failure before replacement leaves the previous document authoritative and
    unchanged. Failure reported after replacement is completion-ambiguous but
    safe: retry recognizes the occurrence already committed and does not
    duplicate it.
11. A malformed legacy torn tail has one deterministic, tested migration
    outcome. Corrupt committed history is never silently discarded.
12. Reads never mutate state unless they are executing an explicit migration
    under the transaction lock.
13. No field from persisted state is interpreted as an unconstrained filesystem
    path. In the single-document design, closed payloads require no path
    reference at all.

### Required state-construction tests

14. Inject failure before temp write, during temp write, before replace, after
    replace but before acknowledgement, and during retry.
15. Interleave upsert with close at every transition point, especially after
    the old payload is read and before the new document replaces the old one.
16. Run two closes for the same occurrence and closes for many distinct
    occurrences across threads and processes.
17. Exercise unreadable state, corrupt interior state, a legacy torn tail,
    unknown schema version, and every supported direct-parent format.
18. Assert the end condition, not just the return value: exact open count,
    exact closed count, unique occurrence IDs, exact final payloads, and no
    unreferenced or duplicate history.
19. Keep the operator recovery surface intact: open human-gated findings remain
    visible in `/dump`, enumeration has no surfacing side effect, and no closed
    or open entry can trigger automated constitutional action.

## Precursor findings outside the lifecycle state machine

The first review also found integration defects that are not represented by the
nine transaction families above: stale functional-map coverage, quarantined
unknown classes missing from `/dump`, mirrored rather than end-to-end `/dump`
tests, runtime-default documentation audit false negatives, and stale impacted
tests. Those remain part of the original review record and should not be
mistaken for transaction acceptance criteria.

## Review rule carried forward

For this subsystem, review must construct states. Reading the intended order of
operations is insufficient. For every mutation, ask:

- What if it failed before changing state?
- What if it changed state and then reported failure?
- What if a retry starts from that exact partial state?
- What if an upsert or second close happens between any two steps?
- What proves the identity and content of the artifact being trusted?

If the design still requires separate answers for S, A, and M, it has not yet
collapsed the failure space this review exposed.
