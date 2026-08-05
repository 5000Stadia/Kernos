# WHISPER-DELIVERY-REGISTER-V1 — say it in the reader's register

**Status:** Draft rev 3 (kreview round 2; migration is one audited transaction)
**Modules:** `kernos/kernel/awareness.py` (the `Whisper` dataclass and
`AwarenessService._push_interrupt`), `kernos/messages/handler.py`
(`_deliver_pending_whispers`, slash dispatch), `kernos/setup/self_update.py`,
**`kernos/kernel/state_json.py`**, **`kernos/kernel/state_sqlite.py`**,
**`kernos/setup/whisper_register_migration.py`** (new — the audited
transaction), every direct `Whisper(...)` producer, `tests/test_handler.py`,
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

**This spec is strictly non-regressive.** Whispers are still delivered on the
same path, with the same reliability, to the same recipient. Only the *register
of the text* and the *content of one payload* change. Nothing here weakens an
existing contract, which is why it does not need the machinery that protects
one.

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
pseudo-pending state.** Rev 2 said "record `migration_discarded` **or** delete"
and separately "fail closed on mismatch" — which contradict. An unexpected
invalid row is simultaneously something to discard *and* a mismatch that must
stop. So the disposition is **one transaction**, in this order:

1. **Identify candidates without constructing `Whisper`** — raw row reads in
   both backends, since construction is what raises.
2. **Compare the complete set** of `(backend, instance, whisper_id, digest,
   reason)` against a concrete audited manifest.
3. **On any mismatch — extra row, missing row, changed digest — mutate NOTHING**
   and fail closed.
4. **On exact match, atomically audit and delete all of them.** One transaction;
   a partial disposition is a failure, not a partial success.
5. **Only after success** are normal pending reads permitted under the new
   required schema.

**The audited manifest is a real, locatable artifact**, not a notion: the
migration's `--report` mode enumerates the target data root and writes
`specs/manifests/whisper-legacy-rows.json` — `(backend, instance, whisper_id,
digest, reason)` per row. A human reviews and commits it. The migration then
requires an exact match against that committed file. Rev 2 said "differs from
the audited set" while never saying where that set lives, which made the rule
unexecutable.

**Target scope** is the `KERNOS_DATA_DIR` of the install being migrated, named
explicitly at invocation — not "wherever it runs".

**Fail-closed aborts service startup**, not merely the migration. This is the
important half: if the migration cannot complete, the required field cannot be
safely enforced, and continuing would crash on the first whisper read with a
constructor error instead of a legible operator message. Aborting with that
message is the honest failure.

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
Deferral, not loss. Unknown leading-slash input is conversational and delivers
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
4. `user_facing_text` is **validated non-empty** at central construction.
   Omission, `""`, whitespace-only, `None`, and non-string each raise — tested
   on the **new-construction path and both persistence read paths**. Producer
   audit covers dict-splat construction.
5. **The migration is one audited transaction.** Candidates are identified by
   **raw row reads** in both backends (never by constructing `Whisper`). The
   complete `(backend, instance, whisper_id, digest, reason)` set is compared
   against the committed `specs/manifests/whisper-legacy-rows.json`. **Any**
   mismatch — an extra row, a missing row, a changed digest — mutates nothing
   and fails closed. On exact match all rows are audited and deleted
   atomically.
   Asserted: an **unexpected third row** aborts with zero mutations; a
   **missing** expected row aborts with zero mutations; a **write failure on
   one backend** leaves **no partial disposition** on either; success is
   idempotent across restart and repeated reads; and **no `surfaced_at` is ever
   written** by this path. Fail-closed **aborts service startup**, verified by
   asserting the startup path refuses rather than proceeding to a read that
   would raise on the required field.
6. The update payload contains **the fact and timestamp only** — no commit SHA,
   no subject, and **no impact claim of any kind**. Asserted that no
   delta-classification vocabulary appears in it, so a future contributor cannot
   reintroduce an unobservable claim without failing this test.
7. A whisper pending when a slash command runs is not delivered, **not marked**,
   and is delivered on the next conversational turn including in a different
   space; a third turn proves no duplicate.
8. Mutation-proved: remove the field selection and the register test fails;
   remove the command guard and the deferral test fails.
9. No change to whisper dedup, expiry, suppression, disclosure-gate scoping, or
   recipient routing. This spec adds no guarantee and removes none.

## Known, unchanged, deferred

`self_update` sets `owner_member_id=""`, so the update whisper is instance-wide
and may be surfaced by whoever takes the next turn. That is **pre-existing** and
this spec does not alter it. It is a real defect and it belongs to
`WHISPER-NOTIFICATION-DURABILITY-V1`, along with the eventual-delivery
guarantee. Recording it here so the narrowing is a documented decision rather
than an oversight.
