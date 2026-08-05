# WHISPER-DELIVERY-REGISTER-V1 — say it in the reader's register

**Status:** Draft (for kreview spec review — NARROW contract, replaces the
review-in-flight on WHISPER-SURFACE-DISCIPLINE-V1)
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

`Whisper` gains `user_facing_text`, **required with no default**. Producers
whose text is already reader-addressed set it equal to `insight_text`. Both
delivery paths emit `user_facing_text`; agent-awareness context continues to
consume `insight_text`.

**Both paths, not one.** `_deliver_pending_whispers` appends it, and
`AwarenessService._push_interrupt` sends `message=` and writes conversation
history via `_store_whisper_message` — fixing only the first would leave the
second publishing the wrong register into durable logs.

### 2. Legacy rows are not delivered — a one-time cleanup, not a lifecycle

Exactly **two** stored whispers lack the field, and both are the agent-facing
noise this spec removes. They are **not delivered**, are logged once with their
ids, and expire through the existing path.

This is deliberately *not* a durable `WITHHELD` state with a recovery command.
The obligation to deliver those two specific whispers is one nobody wants met —
"never silently drop" protects *future* whispers, and every future whisper
carries the field because it is required at construction. Building a recovery
lifecycle for two rows we want gone would be scope inversion.

**Bounded by assertion:** a test pins that the number of legacy field-less rows
in the live schema is what we believe it is, so this cleanup cannot quietly
become a general policy applied to rows nobody audited.

### 3. The update event carries no changelog

`format_update_event_text` is reduced to: the fact, the timestamp, and what was
**observed** — not what was inferred from commit prose.

Impact may only be claimed from surfaces that can actually be diffed across the
update (tool catalog, covenants/default preferences, procedure files). A
positive delta supports a narrow positive claim. **Anything else — computation
failure, or all deltas successfully empty — is `unknown`**, because those three
surfaces are an *incomplete observer*: this very change alters what the user
sees while leaving all three empty.

So the payload reports what was measured and never concludes from it:

> "Kernos updated. I did not detect changes to my tools, standing rules, or
> procedures."

`internal_only` / *"nothing you'd notice"* is not offered. The full changelog
remains in `.auto_update_log.md` unchanged; this spec makes **no claim** that
the agent can retrieve it, because it currently cannot — that affordance belongs
to the deferred spec.

### 4. Slash commands defer

Recognized slash commands do not carry pending whispers. The **transient**
offered batch is cleared; **no durable whisper is marked** surfaced or
suppressed, so the next conversational assembly re-offers it from pending state.
Deferral, not loss. Unknown leading-slash input is conversational and delivers
normally, so a typo cannot swallow delivery.

## Acceptance criteria

1. Delivery emits `user_facing_text` on **both** paths — asserted separately for
   `_deliver_pending_whispers` and for `_push_interrupt` including the text
   `_store_whisper_message` writes to history. Agent context still gets
   `insight_text`.
2. Pinned exact renderings for the **two real captured cases**, so the contract
   is fixed against observed data rather than a generated example.
3. A whisper genuinely about a **third party** survives unaltered — proving this
   is a register contract, not a pronoun filter.
4. `user_facing_text` is required at construction; omission raises. Producer
   audit covers dict-splat construction.
5. Legacy field-less rows are not delivered, are logged with ids, and the count
   of such rows is asserted against the live schema.
6. The update payload contains **no commit SHA or subject**; positive deltas
   yield narrow claims; **both** computation-failure and successful-all-empty
   yield `unknown`. Two mutations — force failure, and force successful-empty on
   a change that alters user-visible behaviour — asserting neither claims
   "nothing changed".
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
