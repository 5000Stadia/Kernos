# WHISPER-DELIVERY-REGISTER-V1 — say it in the reader's register

**Status:** Draft rev 2 (kreview round 1; migration discard bound, impact
classification dropped as out-of-scope machinery)
**Modules:** `kernos/kernel/awareness.py` (the `Whisper` dataclass and
`AwarenessService._push_interrupt`), `kernos/messages/handler.py`
(`_deliver_pending_whispers`, slash dispatch), `kernos/setup/self_update.py`,
every direct `Whisper(...)` producer, `tests/test_handler.py`,
`tests/test_awareness*.py`, `tests/test_fact_harvest_whisper_emit.py`.

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

So it is a **bounded migration disposition**, not a filter:

- **intercept before construction in BOTH backends**;
- durably record a `migration_discarded` reason — a state that is **never**
  interpreted as surfaced — or delete the audited row outright;
- the disposition is idempotent: restart and repeated reads produce no further
  audit entries.

**The count cannot be asserted by a unit test.** Rev 1's criterion claimed a test
would pin "exactly two in the live schema"; no unit test can know a deployed
data root. Instead the **deployment migration enumerates the actual target** and
**fails closed on mismatch**: if it finds ids or a count outside the audited set,
it reports and stops rather than discarding rows nobody examined.

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

1. Delivery emits `user_facing_text` at **all three sinks**, asserted
   separately: `_deliver_pending_whispers`; `_push_interrupt`'s outbound
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
5. **Migration discard, both backends.** Legacy rows missing or carrying an
   invalid reader payload are intercepted **before dataclass construction** in
   `state_json` and `state_sqlite`, receive a durable `migration_discarded`
   disposition that is **never** read as surfaced, and are idempotent across
   restart and repeated reads — no re-logging, no `surfaced_at` written.
   Compatibility tests on **both** backends, including restart, repeated reads,
   and **an unexpected third field-less row**. The deployment migration
   enumerates the real data root and **fails closed** when the discovered set
   differs from the audited one; no unit test claims to know the live count.
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
