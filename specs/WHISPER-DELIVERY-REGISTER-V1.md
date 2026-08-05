# WHISPER-DELIVERY-REGISTER-V1 — say it in the reader's register

**Status:** Draft rev 11 (kreview round 10 — F6 REOPENED; root identity is the
canonical path, not a new primitive)

## Open blocker ledger

Maintained because three blockers survived unanswered while I folded newer
findings — F1 for **seven** revisions, F2 and F3 for **three** each. Reading the
latest letter and answering it is not the same as closing what is open. Every
future revision states each blocker's status explicitly.

**A design fix is not a closure.** My rev-8 ledger marked all six closed;
kreview's audit found only F4 was. The pattern is specific and worth naming: I
corrected the *design prose* and left the *acceptance assertions* unwritten, so
nothing would have failed if an implementation ignored the prose. Status is now
tracked in two columns, because they are two different claims.

| ID | Blocker | Design | Acceptance | Audited |
|---|---|---|---|---|
| F1 | Slash deferral loses a whisper in the expiry window | rev 8 | rev 9 | **CLOSED r9** |
| F2 | Approval not bound to the reviewed manifest | rev 8 | rev 9 | **CLOSED r9** (original finding) |
| F3 | Completion is not atomic across instance shards | **rev 10** | **rev 10** | open |
| F4 | Phase-N read compatibility vs criterion 4 | rev 8 | rev 8 | **CLOSED r8** |
| F5 | Lock exclusivity, and a truthful writer inventory | **rev 10** | **rev 10** | open |
| F6 | Rollout evidence can be stale | rev 8 | rev 9 | **REOPENED r10** — bound to a primitive that did not exist |
| F7 | Approval not bound to the target install/root | **rev 10** | **rev 10** | new |

The fourth column is kreview's independent audit, not my claim. Rev 8 taught me
my statuses are not evidence; rev 9 taught me a design fix is not a closure.

**Modules:** `kernos/kernel/awareness.py` (the `Whisper` dataclass and
`AwarenessService._push_interrupt`), `kernos/messages/handler.py`
(`_deliver_pending_whispers`, slash dispatch), `kernos/setup/self_update.py`,
**`kernos/kernel/state_json.py`**, **`kernos/kernel/state_sqlite.py`**,
**`kernos/setup/whisper_register_cleanup.py`** (new — the pre-deployment
command), **`kernos/server.py`**, **`kernos/repl.py`**, **`kernos/chat.py`**,
**`kernos/evals/bootstrap.py`** (runtime hosts holding the shared root lock;
server and repl additionally run the read-only startup *verification*),
**`kernos/cli.py`** (eleven store constructors — guarded or narrowed), and the
new **central lock/store-factory module**, every direct `Whisper(...)` producer, `tests/test_handler.py`,
`tests/test_awareness*.py`, `tests/test_fact_harvest_whisper_emit.py`,
state round-trip and migration tests on **both** backends.

## Why this spec exists separately

The owner reported one thing: Kernos pasted a raw git changelog into chat under
a header reading *"Background notes (from my own awareness)"*.

`WHISPER-SURFACE-DISCIPLINE-V1` grew across fifteen revisions answering that,
and every kreview finding in it was legitimate. But the accumulated scope — an
owner-bound notification aggregate, `AWAITING_OWNER` reconciliation, retention
ceilings, `inspect_update`, a `WITHHELD` state machine with an authorized
recovery command — all exists to provide a **delivery guarantee that does not
exist today**. That is a real feature and it should be entered deliberately.

**This spec is non-regressive except in one bounded, documented case**, and rev
1–7's flat "strictly non-regressive / same reliability" claim was **false**.
kreview raised it seven times; I asserted it away each time instead of checking
it.

**The exception:** today a slash turn *delivers* a pending whisper. Under this
spec it *defers* it. A whisper already at 47h59m of its 48h expiry is therefore
delivered today and **expires undelivered** under the new behaviour, because the
next conversational turn arrives after expiry.

That is a real, if narrow, loss. It is **accepted rather than denied**, and the
alternatives were each worse for a register fix: extending expiry on deferral
changes a shared mechanism, and delivering on slash turns is the defect being
removed. The window is bounded by the existing expiry, no whisper is dropped
outside it, and criterion 7 tests the 47h59m schedule explicitly rather than
asserting it cannot happen.

**Explicit non-goals**, deferred to `WHISPER-NOTIFICATION-DURABILITY-V1`:
guaranteed eventual delivery, owner-bound routing, multi-event aggregation,
event retention, on-demand detail retrieval, and any durable withheld state.

## The three defects

### A. Agent-facing text is published verbatim

`fact_harvest`'s secondary pass writes an `operational_insight` **for the
agent's own awareness**, in the third person about the reader — *"They keep
running into confusion about whether model/config changes are possible from
chat…"* — and `_deliver_pending_whispers` appends every offered whisper
verbatim under a fixed header.

It is not a bad generation. It is a missing translation at the audience
boundary, and it reads wrongly **every time**, by construction. Confirmed
systemic: the only other stored whisper has the identical shape.

### B. A changelog is pushed into context and published

`self_update.py` states its own intent: *"the whisper carries the substrate
event for the agent's situation context … Substrate does not pre-phrase."* The
raw event was never meant for the user. `DELIVER-ON-DELIVERY`, added later to
stop the model silently dropping whispers, overrode that without either side
noticing.

Two harms, and the second is the one that matters more: the user gets machine
output, **and the agent is handed commit SHAs and subjects it cannot inspect,
verify, or act on**. Its only available action is to repeat them — false
specificity, in an engineering register it then adopts.

### C. Ambient whispers ride on slash-command output

`/model` is an admin action, not a conversation.

## Design

### 1. Dual register, required at construction

`Whisper` gains `user_facing_text`, **required and validated as a non-empty
string** at central construction. Required-with-no-default rejects *omission*
only — it accepts `""`, whitespace, `None`, and non-strings, any of which would
be appended or sent and then marked surfaced, breaking deliver-on-delivery with
no durability feature involved. A persisted row whose value is **present but
invalid** follows the same migration-discard policy as a missing one. Producers
whose text is already reader-addressed set it equal to `insight_text`. Both
delivery paths emit `user_facing_text`; agent-awareness context continues to
consume `insight_text`.

**Three sinks, not two.** `_deliver_pending_whispers` appends it; and
`_push_interrupt` uses `insight_text` in **three** places — the outbound
`message=`, `_store_whisper_message`, and a separate per-space
`conv_logger.append(content=…)`. Rev 1 named the first two. Leaving the third
would give the user the correct register while the durable per-space
conversation log still recorded the agent-facing text — the wrong voice
preserved in exactly the place it outlives the conversation.

### 2. Legacy rows get an explicit migration discard

kreview accepted that these rows do not warrant a durable recovery lifecycle,
and then showed that rev 1's "not delivered, logged once, expire naturally" has
no implementable read path:

- **both backends construct the dataclass directly** — `Whisper(**d)` in
  `state_json.py` and `_build_dataclass(Whisper, …)` in `state_sqlite.py` — so a
  required field makes a legacy row **raise before it can be skipped**;
- **"logged once" needs a durable discriminator.** Without mutating the row,
  every read and every restart logs it again;
- **existing expiry writes `surfaced_at`**, manufacturing a false delivery
  receipt for a row deliberately never surfaced.

kreview's decisive framing: **an audited one-time deletion, not a durable
pseudo-pending state** — and, in round 4, the narrowest shape that actually
works: **a pre-deployment cleanup command, so ordinary startup never becomes a
migration coordinator.**

Rev 4 made startup run the migration. That forced answers to questions the
feature should not have to raise at all: what a first start with no manifest
does, what an auto-update restarting straight into a permanent abort looks like,
and what document shape lets a JSON audit object live in a bare list without
hitting dataclass construction itself.

**The sequence is operator-driven and ordered:**

1. `whisper_register_cleanup --report --data-root <path>` enumerates candidates
   by **raw row reads** against the **authoritative backend only**
   (`KERNOS_STORE_BACKEND`), writing the manifest into that data root at
   `diagnostics/whisper_legacy_manifest.json`. Remnants in the inactive store
   are reported and left untouched.
2. **The operator reviews and approves** that manifest, taking its canonical
   SHA-256.
3. **`--apply --approve <canonical-manifest-sha256>`.** A bare `--apply` is
   rejected. The digest binds the approval to the *reviewed bytes*: rev 5–7 had
   review followed by an unqualified apply, so manifest M1 could be reviewed and
   replaced by M2 before apply ran, and nothing would notice. Apply
   re-enumerates, requires an exact match against the manifest **and** that the
   manifest matches the approved digest, and on any mismatch mutates nothing and
   exits non-zero. The approval identity is retained in the completion record.
4. **Only then** is the required-field code deployed.

**The approved manifest IS the audit record — and completion is recorded INSIDE
the same atomic operation.** Rev 5–7 said the manifest removed the two-effect
problem and then wrote a *sidecar completion marker*, reintroducing exactly the
second effect it claimed to have eliminated. A crash after the backend deletion
but before the marker leaves an empty candidate set that no longer exact-matches
the approved manifest, so the "idempotent" claim was false: the retry would
abort on mismatch with no record of why.

Completion is therefore part of the backend-local commit:

- **JSON — this requires a document shape the store does not currently have.**
  `whispers.json` is a **bare list**, and `get_pending_whispers` sends every
  unsurfaced element through `Whisper(**d)`. Appending a completion record to
  that list recreates exactly the sentinel-crash problem I removed two
  revisions ago. So Phase N introduces a **versioned envelope**:

  ```json
  {"schema_version": 1, "whispers": [...], "cleanup_completed": {...} }
  ```

  **Every** production path — pending reads, save, delete, mark-surfaced, dedup
  — goes through one load/store helper that accepts **either** a legacy bare
  list **or** an envelope, and always writes an envelope. Only `whispers` is
  ever iterated as rows, so no reader can meet the completion record. The
  temp+rename then carries both effects in one write.
- **SQLite** — the same transaction inserts the completion row and deletes the
  whisper rows.

**The commit unit is ONE INSTANCE, because that is what storage actually
shards.** Rev 8–9 described a single atomic commit for an install-wide manifest
— but JSON keeps one `whispers.json` per instance and SQLite one
`data/{instance_id}/kernos.db` per instance, and **no temp+rename or
transaction spans those shards**. A crash after instance A commits and before B
leaves an install-wide partial apply, which criterion 5's "interrupted apply
leaves no partial state" flatly denied. A singular completion record also cannot
distinguish *fully* complete from *partially* complete.

So:

- the manifest is **partitioned by instance**, each partition carrying its own
  canonical digest;
- **each instance commits independently** — its own atomic rewrite or
  transaction, its own completion record holding the approval digest;
- an **aggregate coordinator** drives them in **two passes while holding the
  exclusive lock**. Pass one **preflights every incomplete partition**; only if
  all verify does pass two commit any of them. Without that, "any mismatch
  aborts with zero mutations" is unimplementable — the coordinator could commit
  A and only then discover a mismatch in B;
- **resume has an explicit exception to the exact-match rule**, because rows in
  a completed partition are *intentionally* missing and would otherwise read as
  a mismatch. On re-run: verify the **unchanged approved top-level manifest**
  first; a partition whose completion record matches is **skipped without
  re-enumeration**; every incomplete partition is re-enumerated and
  exact-matched. Each completion record stores **both** the operator-approved
  top-level digest **and** its partition digest, plus the bound root identity —
  and **either** mismatch refuses rather than skipping;
- overall success requires **every** partition complete; a partial run exits
  non-zero naming the outstanding instances.

**Idempotence is then well-defined per shard**: a re-run finding a matching
completion record for an instance is a success no-op for that instance and does
not re-enumerate it.

**`--apply` requires a STOPPED install, and verifies it against a lock this spec
must first CREATE.** JSON apply snapshots a bare list and replaces it by
temp+rename, so a valid whisper written by the live server between the read and
the rename is silently overwritten though it was never a candidate. A cleanup
that can destroy unrelated data is worse than the two rows it removes.

Rev 7 said apply "positively checks the service lease / process lock" — kreview
searched the runtime and **no such primitive exists**. There is no
server-lifetime lease, PID lock, or global process lock in `server.py` or
`start.sh`; only unrelated component locks. I specified a check against
something imaginary, and a barrier test would have passed against a mock
production never holds. That is the synthetic-affordance failure I have now made
four times.

**So the lock is part of this spec, and it is a real one:**

- **Path** — `<data_root>/.kernos_instance.lock`, under the *selected* data
  root, so two installs never contend.
- **Held SHARED by every runtime host** — `LOCK_SH`, acquired **before any
  store access** and held for process lifetime. Rev 8–9 said `LOCK_EX |
  LOCK_NB`, which would have made server, repl, chat and eval hosts **mutually
  exclusive with each other** for their whole lifetimes — breaking the
  documented "same tenant, different door" chat path whenever the server is
  live. That is a **second regression**, outside the one bounded loss this spec
  accepts, introduced by a lock intended only to exclude cleanup.
- **Cleanup takes `LOCK_EX | LOCK_NB`** — non-blocking, so it fails immediately
  while any runtime host holds shared access, and it **holds the exclusive lock
  across re-enumeration and apply**, not merely at the start.
- **Staleness** — file *existence is never proof*; `flock` ownership is released
  by the kernel when the holder dies, so a leftover file is acquirable and
  correctly treated as stopped.
- **Platform** — no `fcntl` means apply refuses rather than proceeding
  unlocked.
- **Other writers — enumerated truthfully this time, and structurally
  enforced.** Rev 9 claimed direct writer call sites live in four modules. That
  was **false**, and the cause is worth naming: I enumerated from a `head`-
  truncated grep and reported the visible subset as an audit. The real set is
  **eleven** modules — `kernel/awareness.py`, `messages/handler.py`,
  `kernel/improvement_loop_workflow.py`, `server.py`,
  `kernel/relational_dispatch.py`, `setup/self_update.py`,
  `messages/phases/persist.py`, `kernel/reasoning.py`, `kernel/fact_harvest.py`,
  `kernel/diagnostics.py`, `kernel/covenant_manager.py`.

  All eleven are **transitively covered today** — reachable only from a
  lock-held host — but a fixed list is not a guarantee.

  **And the host list was still incomplete.** `kernos/cli.py` constructs
  `JsonStateStore` in **eleven** places and was absent from it. A store exposes
  whisper mutation whether or not today's CLI commands call it, so capability —
  not current usage — is what must be covered.

  **Enforcement is structural and ROOT-BOUND:** every production store is
  constructed through one guarded factory that **requires proof the runtime lock
  is held for that canonical root**. The proof is not a boolean or a bare token:
  it carries the `realpath` it was acquired for, so a guard obtained for root A
  **cannot** construct a store for root B. Alternatively a CLI path may take a
  store narrowed so that whisper writes are literally unavailable. A
  source-inventory test classifies **every** construction site — lock-held,
  transitively covered, read-only-narrowed, test-only, or prohibited — and fails
  on any new one, so the inventory cannot rot back into a stale list.

Tested with a **real subprocess** holding the lock, not an in-process barrier —
an in-process test cannot prove a cross-process contract. A barrier-controlled
concurrent-writer case additionally proves no unrelated row is lost.

**Each manifest entry is `(backend, instance, whisper_id, digest, reason)`**,
with both variable parts pinned so report and apply compute identical sets from
unchanged data:

- **`digest`** — SHA-256 over a canonical JSON serialization of the **complete
  logical row**, UTF-8, sorted keys, no whitespace. Rev 4 hashed seven fields
  and **excluded `user_facing_text` — the very value whose absence authorizes
  the deletion.** kreview's consequences are all reachable: one non-string
  reader value replaced by a different non-string value approves unchanged; a
  row that becomes *surfaced* between report and apply keeps its digest and is
  deleted despite the lifecycle change; private `supporting_evidence` can change
  under an identical approval. **An approval that does not bind the thing being
  approved is not an approval.**
  The reader field is included with a **tagged representation** — `missing` is
  distinct from JSON `null`, from a string, and from any other type — so each
  `reason` value is digest-distinguishable.
  **Only enumerated backend transport columns are excluded**, each justified:
  SQLite `rowid` and JSON list position are storage location rather than
  content, and differ between stores for the same logical row.
- **`reason`** — a closed enum: `missing`, `blank`, `whitespace_only`,
  `non_string`. Not free text.

**The manifest's canonical top-level bytes bind the TARGET ROOT — and "root
identity" is a real thing, not a primitive I am inventing.**

Rev 10 bound approval and rollout evidence to "stable install identity and
data-root identity". kreview checked: **no such primitive exists**.
`KERNOS_INSTANCE_ID` is optional tenant/adapter identity, not install identity.
I bound two safety properties to a mechanism that lived only in my prose — the
fifth affordance in this feature that production does not have.

I am **declining to invent an install-ID primitive.** kreview's own questions
show why it is wrong for a two-row cleanup: it would need a creation event, a
lifecycle, alias canonicalization, copy/move semantics and migration — a
deployment-identity subsystem arriving through a different door than the one
already refused.

**Root identity is `os.path.realpath(data_root)`** — the canonical absolute
path. No creation, no lifecycle, no migration:

- **aliases** — relative paths, `..` segments and symlinks canonicalize to the
  same value by construction;
- **distinct roots** differ trivially, so identical rows cannot share an
  approval;
- **a moved or copied root** resolves to a *different* path, the manifest no
  longer matches, and apply **refuses** — correct, since a copied root must not
  inherit an approval reviewed against the original;
- **missing or malformed** fails closed;
- **unforgeable by apply** — derived from the `--data-root` argument at
  invocation, never read from stored state, so apply cannot generate or replace
  it to make a mismatch pass.

The **same value** binds F6's rollout evidence and the lock guard's root
binding, so all three rest on one real mechanism instead of three prose ones. Without that, two installs holding identical candidate rows produce
an **identical approval digest**, so an approval reviewed against root A would
authorize deletion in root B. `--apply` verifies the manifest's bound identity
against `--data-root` and refuses on mismatch. Tested directly: two roots with
byte-identical rows **cannot share an approval**.

### The release is TWO-PHASE, because one phase cannot be ordered

Rev 5 claimed a `report → approve → apply → then deploy` sequence and that this
removed the auto-update restart question. **That claim was false**, and kreview
caught it against the real updater: the cleanup command ships in the *same
artifact* that makes the field required. Under the default unattended updater
the new code is pulled and exec'd before an operator could run a tool that did
not exist in the previous version — so the "pre-deployment" step is
unschedulable, and the service goes offline until someone intervenes.

**Phase N has two distinct schemas, and rev 6 conflated them.** "Optional on
read" cannot mean "optional on new write" — if new writes may omit the field,
cleanup can never converge, because fresh candidates keep appearing behind it.
So phase N is ordered internally:

1. **Write-side cutover.** Every producer sets `user_facing_text`; new writes
   without it are rejected. Reads remain **compatible** with legacy rows so the
   service keeps running.
2. **Only then** does `--apply` run — against a set that can no longer grow.
3. `whisper_register_cleanup` is available throughout for `--report`.

**Phase N+1 — read compatibility is removed and the dataclass field becomes
required**, and it ships **only when every supported install reports zero
candidates** under phase N's shared predicate.

Rev 6 said "after phase N has been available long enough". **Elapsed time is not
evidence of cleanup** — the absence-of-evidence error this spec has already been
caught making twice, applied to a release decision.

The gate is a **durable, predicate-versioned evidence record** per install, not
a remembered zero: `{install identity, authoritative backend, data-root
identity, cleanup predicate version, schema version, report digest, candidate
count, observed_at}`. Without those fields a stale zero — taken before a
producer regression reintroduced field-less writes — could authorize N+1.

**Freshness is part of the gate:** each record must be generated *after* the
final Phase-N build and *after* the write-side rejection tests pass on that
install. Two installs exist, so this is obtainable rather than aspirational.

If the backstop is ever *deliberately* allowed to catch stragglers instead, that
is a rollout policy with an accepted outage and must be stated as such — not
implied by a waiting period.

**Acceptance (F6):** an N+1 gate test asserts the evidence record carries every
required field; that it **binds install identity, authoritative backend and
data-root identity**; that predicate and schema versions match the shipping
build; and that the gate **rejects** evidence which is stale (generated before
the final Phase-N build), mismatched (different predicate version), or drawn
from an install whose write-side rejection tests had not yet passed.

The two phase schemas are specified and tested **independently**: phase N
asserts legacy rows still read while new writes are rejected; phase N+1 asserts
legacy rows no longer construct at all.

**The startup guard is the BACKSTOP, not the path.** An install that reaches
N+1 without cleaning refuses to start, naming the exact recovery command. Two
things must be documented rather than implied, because both are true here:

- the recovery command is run **externally**, against the stopped install;
- **`start.sh` is not a supervisor** — `python kernos/server.py` is its last
  line with no restart loop — so a refusing start leaves the service **down
  until manual intervention**. That is the honest cost of the backstop, and it
  is why phase N exists to make the backstop rare rather than routine.

Zero candidates passes silently in both phases.

### 3. The update event carries no changelog — and claims no impact

`format_update_event_text` is reduced to **the fact and the timestamp**. No
commit SHAs, no subjects.

**Impact classification is dropped from this spec.** Rev 1 said positive deltas
on tool catalog / covenants / procedures could support narrow claims. kreview
checked and there is **no observation mechanism**: nothing snapshots the
pre-update side before pull and restart, and `format_update_event_text` only
parses commit prose. A test could therefore pass by stubbing a delta production
can never obtain — the same synthetic-affordance failure as the parked design,
which I had just finished objecting to.

Building it properly means a versioned pre-update surface manifest persisted
before restart, a post-update manifest after readiness, like-version comparison
only, and everything else resolving to `unknown`. That is real new machinery and
belongs with the deferred guarantee, not in a non-regressive register fix.

So the agent is told **only what is true and observable**: an update applied, and
when. The changelog remains in `.auto_update_log.md`, and this spec makes no
claim the agent can retrieve it.

### 4. Slash commands defer

Recognized slash commands do not carry pending whispers. The **transient**
offered batch is cleared; **no durable whisper is marked** surfaced or
suppressed, so the next conversational assembly re-offers it from pending state.
Deferred — except inside the expiry window documented above. Unknown leading-slash input is conversational and delivers
normally, so a typo cannot swallow delivery.

## Acceptance criteria

1. Delivery emits `user_facing_text` at **all four delivery/logging sinks**
   — stated as a list, not a count, so a test plan cannot collapse one by
   interpreting the number. Asserted separately: `_deliver_pending_whispers`; `_push_interrupt`'s outbound
   `message=`; `_store_whisper_message`; and the per-space
   `conv_logger.append(content=…)`. Agent-awareness assembly alone keeps
   `insight_text`.
2. Pinned exact renderings for the **two real captured cases**, so the contract
   is fixed against observed data rather than a generated example.
3. A whisper genuinely about a **third party** survives unaltered — proving this
   is a register contract, not a pronoun filter.
4. **Read and write compatibility are separate mechanisms, and the criterion is
   split four ways.** Rev 7's design allowed Phase-N legacy *reads* while
   criterion 4 still demanded that invalidity raise on "both persistence read
   paths" — the two cannot both be the acceptance rule. A single optional
   dataclass constructor also cannot accept a legacy read and reject an
   identical new construction, because it has no provenance.
   So compatibility lives in a **dedicated Phase-N persistence loader**, not in
   the dataclass:
   - **N-write** — `save_whisper` / the new-write factory rejects missing, `""`,
     whitespace-only, `None`, non-string. A producer calling the ordinary
     constructor cannot exploit read compatibility.
   - **N-read** — the compatibility loader constructs legacy rows successfully.
   - **N+1-write** — unchanged: rejected.
   - **N+1-read** — the compatibility path is gone; legacy rows do not
     construct, and the startup verifier prevents that being reached at all.
   Producer audit covers dict-splat construction in every phase.
5. **Pre-deployment cleanup, per install, authoritative backend only.**
   `--report` enumerates by raw row reads and writes the manifest into the named
   data root; `--apply` re-enumerates and requires an exact match.
   Asserted: `--report` twice on unchanged data yields an **identical** manifest
   (proving canonicalization); **any** mismatch — extra row, missing row,
   changed reader value, changed `surfaced_at`, changed supporting evidence —
   aborts with **zero mutations** and a non-zero exit; an interrupted `--apply` leaves
   **no torn per-instance shard** (JSON: temp discarded, document
   byte-unchanged; SQLite: transaction rolled back), while **between-shard
   partial progress is durable and resumable** — the blanket "no partial state"
   of rev 8–10 contradicted the multi-instance assertion in the same criterion; `--apply` is idempotent; **no `surfaced_at`
   is ever written** by this path; remnants in the **inactive** backend are
   reported but untouched.
   **Approval binds the reviewed bytes (F2):** a bare `--apply` is **rejected**;
   reviewing manifest M1 and replacing it with M2 before apply **cannot apply**;
   a wrong `--approve` digest yields **zero mutation and a non-zero exit**; and
   the approval digest is **retained in the completion record**.
   **Completion is inside the commit (F3):** asserted that the JSON completion
   record and the row removal land in the **same** temp+rename and the SQLite
   completion row and deletes in the **same** transaction; that a crash at that
   boundary leaves the document byte-unchanged / the transaction rolled back;
   that a re-run with a **matching** digest is a success no-op that does not
   re-enumerate; and that **every** production path (pending read, save,
   delete, mark-surfaced, dedup) reads both a legacy bare list and an envelope,
   never iterating the completion record as a row.
   **The lock is proved across processes, and is SHARED among runtime hosts
   (F5):** **two** real subprocess runtime hosts hold `LOCK_SH` **concurrently**
   — proving the chat path is not broken by the server being live — while
   cleanup's `LOCK_EX | LOCK_NB` **fails until both release**. A
   barrier-controlled concurrent writer proves no unrelated row is lost. Each of
   `server.py`, `repl.py`, `chat.py`, `evals/bootstrap.py` acquires before any
   store access, and the **structural guard** is asserted: a store constructed
   outside the lock-holding path **raises**, and the source-inventory test fails
   on any new host or writer edge.
   **Multi-instance commit (F3):** a two-instance install with a crash **between
   shard commits** leaves instance A complete and B untouched — never a torn
   shard — and the resumed run **skips A** and completes B, ending with one
   completion record per instance and a non-zero exit on the interrupted run.
   **Root identity is real and unforgeable (F6/F7):** two unchanged `--report`
   runs of one root produce the **same** identity; path aliases (relative,
   `..`, symlink) resolve to the **same** root; two roots with byte-identical
   rows produce **different** approval digests and an approval for A **refuses**
   against B with zero mutation; a **moved/copied** root refuses; and apply
   **never generates or replaces** identity to make a mismatch pass — asserted
   by mutating the stored value and confirming apply still derives it from
   `--data-root`. A lock guard acquired for root A **cannot** construct a store
   for root B.
   **Two-pass preflight (F3):** a mismatch in the **last** partition produces
   **zero mutations in earlier partitions**; and on resume a partition whose
   completion record matches is skipped, while a completion whose top-level
   **or** partition digest disagrees **refuses** rather than skipping.
   **The startup guard shares the cleanup's validity predicate exactly.** Rev 5
   said it refuses on "field-less rows" while the cleanup classified missing,
   blank, whitespace-only, `null` **and** non-string as candidates — so a row
   with `user_facing_text=""` passed the guard and then raised on the first
   pending read, which is precisely the crash the guard exists to prevent. One
   closed predicate and one `reason` enum, shared by report, apply and verify.
   Mutation-tested: **each** invalid type independently trips both `server.py`
   and `repl.py` startup, and no whisper producer or consumer runs when it
   does.

6. **`event_payload` is a CLOSED union in v1**, not a mapping with one
   constrained case. Rev 6 constrained the shape only "when
   `event == kernos_self_updated`", which accepts `{}`, a mapping with no
   `event` key, and arbitrary `{event: other, …}` — three shapes with no defined
   meaning. V1 is exactly `None | KernosSelfUpdatedPayload`; **every other
   non-`None` shape is rejected at construction and at persistence read**.
   The field is **optional** (most whispers are not events) and **never a
   mutable shared default** — `None`, not `{}`.
   `applied_iso` is a **timezone-qualified ISO-8601 string**; the marker text is
   **normalized to UTC on ingest** and the normalized form is what is persisted
   and rendered, so report and render cannot disagree about the same instant.
   The payload is **immutable after construction**.
   Round-trip and invalid-shape tests cover: unknown event, missing key, extra
   key, non-string timestamp, **naive** timestamp, `{}`, and caller mutation
   after construction. The round-trip exercises the **production serializer** —
   not a hand-built dict — because an immutable representation (frozen mapping,
   mapping proxy, dataclass) is not automatically serializable by either
   backend, and a hand-built dict would prove nothing about what production
   actually persists.
   `format_update_event_text(payload)` renders one sentence from those two keys
   and nothing else — rev 4 specified keys with no field to hold them and no
   function to render them, so the requirement had no owner.
   **`applied_iso` comes from the pending-update marker** `self_update` already
   persists across exec, *not* the log heading, so "no value derived from the
   update log" is true rather than nominal.
   **Malformed or missing timestamp: the whisper is not emitted, and the marker
   is CONSUMED**, with the offending value recorded in the log for diagnosis.
   Rev 5 said "do not emit and log" and left the next restart unspecified —
   retaining the marker would re-log and re-fail on every boot, which is the
   same "logged once needs a durable discriminator" defect kreview raised
   against my legacy handling three rounds ago. Losing one notification beats an
   endless warning, and beats inventing a time. Asserted: the structured payload
   has **no other keys**, and **no value derived from the commit log or the
   update log** appears anywhere in it. Rev 3 asserted "no delta-classification
   vocabulary", which is a lexical proxy — it would miss a novel impact
   inference phrased differently and could reject harmless wording. Pinning the
   fields makes the property checkable rather than guessed at.
7. **Slash deferral, including its bounded loss.** A pending whisper is not
   delivered on a slash turn, **not marked**, and is delivered on the next
   conversational turn including in a **different space**; a third turn proves
   no duplicate.
   **And the 47h59m schedule is tested explicitly**: a whisper at 47h59m
   deferred by a slash turn **expires undelivered**, asserting the documented
   loss rather than a claim that it cannot occur. Delivery today, expiry under
   this spec — that difference is the accepted regression, and the test is what
   keeps it accepted rather than forgotten.
8. Mutation-proved: remove the field selection and the register test fails;
   remove the command guard and the deferral test fails.
9. No change to whisper dedup, expiry, suppression, disclosure-gate scoping, or
   recipient routing. This spec **adds no guarantee**, and removes none **except
   the bounded slash-deferral expiry case in criterion 7**, which is accepted
   and tested rather than denied.

## Known, unchanged, deferred

`self_update` sets `owner_member_id=""`, so the update whisper is instance-wide
and may be surfaced by whoever takes the next turn. That is **pre-existing** and
this spec does not alter it. It is a real defect and it belongs to
`WHISPER-NOTIFICATION-DURABILITY-V1`, along with the eventual-delivery
guarantee. Recording it here so the narrowing is a documented decision rather
than an oversight.
