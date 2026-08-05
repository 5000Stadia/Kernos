# WHISPER-NOTIFICATION-DURABILITY-V1 — guaranteed eventual delivery (DEFERRED)

**Status:** DEFERRED — not scheduled. Review paused at rev 15 by founder decision
2026-08-05, on my recommendation and with kreview's agreement.

**Why this is parked.** This began as the fix for a user-visible defect: a raw
changelog published to chat. Across fifteen revisions it became a specification
for *guaranteed eventual delivery* — owner-bound routing, an ownerless
aggregate with reconciliation triggers, retention ceilings, event-scoped
records, `inspect_update`, and a `WITHHELD` state machine with an authorized
recovery surface.

Every kreview finding here was legitimate. The problem was not the findings; it
was that each one was a correct consequence of a guarantee **nobody had decided
to add**. The reviewer reached the same conclusion independently: the
accumulated requirements follow from bolting mutable aggregation onto the
existing two-stage delivery architecture, not from the reported defect.

**The reported defect is fixed separately and non-regressively** by
`WHISPER-DELIVERY-REGISTER-V1`, which changes the register of the text and the
content of one payload while leaving delivery reliability exactly as it is.

**What this spec still owns, if it is ever entered deliberately:** eventual
delivery guarantees, owner-bound routing (`owner_member_id=""` is a real
pre-existing defect), multi-event aggregation through the READY phase,
retention with an enforceable ceiling, on-demand event retrieval, and durable
withheld state with a reachable exit.

**Read it for the failure enumeration, not as a plan.** Its value now is the
record of twelve review rounds of reachable states — see also
`docs/reference/governance-lifecycle-failure-state-enumeration.md`.

---

*Original content follows, unchanged from rev 15.*


**Status:** Draft rev 15 (kreview rounds 1–12; stale text VERIFIED removed; ceiling precedence chosen)
**Modules:** `kernos/kernel/awareness.py` (the `Whisper` dataclass **and**
`AwarenessService._push_interrupt`), `kernos/messages/handler.py`
(`_deliver_pending_whispers`, slash dispatch, `_handle_dump`),
`kernos/kernel/fact_harvest.py`, `kernos/kernel/state_json.py`,
`kernos/kernel/state_sqlite.py`, every direct `Whisper(...)` producer
(`covenant_manager.py`, `improvement_loop_workflow.py`, `diagnostics.py`,
`setup/self_update.py`, `server.py`, `messages/phases/persist.py`),
`tests/test_handler.py`, `tests/test_fact_harvest_whisper_emit.py`,
`tests/test_awareness*.py`, state round-trip tests.

*Rev 2's module list was wrong, not merely incomplete — it named one delivery
path when there are two, and omitted the dataclass, both persistence backends,
and every producer.*

## Why

Observed live. The owner typed `/model` twice and the reply carried:

> _Background notes (from my own awareness):_
> - They keep running into confusion about whether model/config changes are
>   possible from chat, so I'd add a surfaced session-capability inspector plus
>   a clear model-switch request path…

Traced end to end: `fact_harvest`'s secondary pass generates an
`operational_insight` **for the agent's own awareness**, stores it as an
`ambient` whisper, and `_deliver_pending_whispers` appends every offered whisper
**verbatim** to the user's reply under a fixed header.

Two distinct defects.

### A. The text is written for the agent and published to the user

The insight is authored in the third person *about* the person reading it —
"They keep running into confusion…". That is the correct register for a note the
agent writes to itself. It is the wrong register for text shown to the person it
describes, and it will read strangely **every time**, by construction. This is
not a bad generation; it is a missing translation step at the delivery boundary.

It also contradicts the layered-surface principle already held elsewhere in this
repo (operator gets receipts, user gets the sentence) and the "ambient, not
demanding" posture.

Confirmed as systemic, not a one-off — the only other stored whisper has the
identical shape: *"They're trying to answer 'what docs exist?' repeatedly, so I
could build a single docs-index helper…"*

### A2. Substrate events designed to stay internal are published raw

Found live by the owner in a `/dump`, and it is the sharpest instance of this
defect class. The reply carried:

> _Background notes (from my own awareness):_
> `[SUBSTRATE_EVENT: kernos_self_updated]` … `11 commits since previous head`
> … `c1ff983 Add the governance-lifecycle failure-state enumeration (kreview)` …
> `Full log persisted at .auto_update_log.md`

**Two specs are in direct conflict, and the newer one silently broke the older.**

`AUTO-UPDATE-INFORMING-V1` states the intent in `self_update.py:979`:

> "the whisper carries the substrate event **for the agent's situation
> context**. The agent reads this alongside its covenants and produces
> user-facing phrasing **in its own voice. Substrate does not pre-phrase.**"

The raw event was never meant to reach the user. But DELIVER-ON-DELIVERY
appends **every** offered whisper verbatim so the model cannot silently drop
one — a reasonable guarantee that overrode the older design without noticing.
The user receives machine output (commit SHAs, internal log paths) under a
header claiming it is the agent's own awareness, *in addition to* whatever the
agent renders in its own voice per the system-prompt preference.

This is worse than the `fact_harvest` case: that at least produces prose about
the reader. This publishes a changelog.

**Consequence for the design:** "every whisper carries `user_facing_text`" is
insufficient. Some whispers are **agent-context-only by construction**, and the
correct value is not text at all — it is *"none, by design; the agent renders
this."* That is semantically distinct from *"legacy row, we do not know"*, and
collapsing them would either publish machine output or quarantine a working
feature.

**Audience is a TAGGED UNION — required, with no default:**

| `audience` | `user_facing_text` | Delivery |
|---|---|---|
| `READER` | validated **non-empty** string, required | delivered |
| *(absent)* | — | legacy only → routed to `WITHHELD` **before construction** |

**V1 ships `READER` only.** Rev 10 introduced an `AGENT_CONTEXT_ONLY` class with
a `CONSUMED` lifecycle — and then reclassified its *only* producer
(`kernos_self_updated`) to `READER`, leaving a lifecycle with no user. kreview:
do not ship an unused lifecycle justified by a producer that no longer uses it.
Correct, and it independently matches the scope concern I had already raised:
this spec had grown well past the defect it was written for.

`AGENT_CONTEXT_ONLY` is **deferred** until a concrete signal genuinely needs it.
The union is designed so adding it later is additive, not a migration.

*Rev 7 added the third class while leaving the earlier "every producer must set
`user_facing_text`, no default" text normative — the same
safe-rule-beside-contradictory-normative-text failure kreview caught in the
governance spec. A tagged union removes the contradiction instead of layering
another sentence on it.* The static audit rejects any producer that omits
`audience` **or** pairs an audience with the wrong payload shape.

### `kernos_self_updated` is READER, not context-only

**This is the correction that matters most, and I had it backwards.**

I diagnosed DELIVER-ON-DELIVERY as having silently broken
AUTO-UPDATE-INFORMING — then proposed a fix that silently breaks it *again from
the other side*. DELIVER-ON-DELIVERY exists **because** a mere model offer often
produced no user surface at all. Classifying the update as `AGENT_CONTEXT_ONLY`
marks it consumed the moment it is offered, **even if the model says nothing** —
and my criterion 9j would have passed under total user silence. That is not a
fix; it is the same class of regression wearing my own diagnosis as cover.

So the update event is **`READER`**, carrying a compact, safe fallback:

> "Kernos applied an update at *&lt;time&gt;*. I can look at what changed if you want."

**Event records and `inspect_update` are MANDATORY v1 scope** — confirmed by the
founder. The fallback therefore offers inspection unconditionally, because the
affordance is guaranteed to exist. (Rev 11 left the primitive optional and the
offer conditional, which authorized a smaller implementation than the confirmed
scope — a spec should not quietly permit less than what was agreed.)

The compact fact stays in `insight_text` for the agent's own context, and the
system-prompt preference still lets it say something better in its own voice.
What is removed is the **changelog**, not the **obligation**. The eventual
user-notification contract is preserved rather than quietly dropped.

#### `AWAITING_OWNER` needs a trigger, not just a resting place

Rev 11 said an unresolvable owner "persists as unresolved". That fails closed
for confidentiality and **fails to stay live for the notification obligation** —
once an owner is later established, nothing in the contract binds the event to
them or admits the whisper. It is the third safe-state-with-no-exit in this
spec, after `WITHHELD` and the undiscoverable `event_id`. I keep building rooms
with no doors.

**Two collections, because one cannot be both bounded and never-expiring**
(kreview round 10, and the sentence that settles it):

| | Mutability | Retention |
|---|---|---|
| **update event record** — `event_id`, timestamp, measured deltas, changelog | **immutable** | newest 50 **and** everything within 90 days |
| **owner-notification aggregate** — state, owner binding, member `event_id`s, applied count, latest timestamp | **mutable, single** | lives until terminal delivery |

Rev 12 marked *every* ownerless event `AWAITING_OWNER` and exempted each from
retention "until it binds". With no owner that is **forever**, so a daily update
stream grows without bound — my fail-closed exemption became an unbounded
collection. And at binding, ordinary same-signal dedup would either drop
obligations or flood the new owner with N notifications; criterion 9u
constructed only one awaiting event and could not tell those apart.

So: event records obey the retention rule **with no exemption**. A **single
mutable aggregate per notification scope/signal** carries the obligation.

**The aggregation boundary is DELIVERY, not owner discovery.** Rev 13 stopped
aggregating the moment an owner was found — so the identical loss/flood
ambiguity returned one transition later: owner binds, the occurrence sits
`READY` because the owner has not taken a turn, and E2…E52 arrive with nothing
specifying what they do. Same-signal dedup could drop them, replace the
occurrence, or spawn multiple `READY` rows and flood later. Criterion 9u stopped
at owner establishment and could not tell those apart.

The aggregate may be **unbound (`AWAITING_OWNER`)** or **owner-bound
(`READY`)**, and **every update atomically joins it until that occurrence
reaches a terminal delivery decision** (surfaced, dismissed, expired). Binding
changes *authorization and state*; it does not change *whether aggregation
continues*. On owner establishment the aggregate becomes **exactly one**
owner-bound `READY` occurrence and keeps accumulating. The fallback then says *multiple* updates were applied, and
`inspect_update()` lists the **retained** events.

The count and the retained set can legitimately diverge — "14 updates applied, 8
still retained" is honest and is what the aggregate reports. **No dangling
reference:** a member `event_id` evicted by the 50/90 bound is dropped from the
aggregate's list at reconciliation, never surfaced as a broken link.

Reconciled **under the state lock** on owner establishment, on startup, and at
the next enqueue.

#### The pull path must exist before it can be promised

Rev 9 promised the agent could read `.auto_update_log.md` on demand. **It
cannot**, and kreview checked what I asserted:

- the log lives under the data root, while `read_file` is current-space-only
  (plus repo source under `kernos/`, `specs/`, `docs/`) — it is unreachable;
- `/dump` never deliberately included it; the owner saw the content only because
  the raw event was already sitting in assembled context;
- it is a **single file overwritten by the next update**, so it is not even
  stable for the event being discussed.

I specified a retrieval affordance that does not exist — the fourth time this
spec has claimed a property its mechanism could not deliver. Binding it:

- update records become **immutable and event-scoped**, keyed by an `event_id`
  carried on the whisper, rather than one file overwritten each time;
- an **owner-scoped `inspect_update(event_id=None)`** tool makes exactly that
  event readable;
- a real "what changed?" **tool turn is tested against that exact event**.

**`event_id` must be discoverable on the turn that needs it.** Rev 11 carried it
only in the one-time whisper's `insight_text`; the user-facing fallback
deliberately does not expose it, and after delivery the whisper is no longer
pending. So on a later "what changed?" turn the model has **neither the id nor
any way to obtain it** — and criterion 9q would still have passed by injecting
the id synthetically while the real conversational path failed. A third
vacuous-test escape in this spec.

So `event_id` is **optional**, resolved by an exact predicate rather than two
sentences that can disagree:

| Call | Result |
|---|---|
| explicit `event_id` | that exact event, owner-scoped |
| omitted, most recent surfaced receipt references **exactly one** retained event | that event |
| omitted, that receipt references **multiple** retained events | a **list** of them |
| omitted, receipt's referenced events are **all evicted** | scoped `NOT_FOUND` — "that update is no longer retained". **Never walks back to an older receipt** |
| omitted, **no** surfaced update receipt exists | scoped `NOT_FOUND` (no recent update) |

Tested as **two real turns** — the
fallback is delivered, then the owner asks "what changed?" **without supplying
an id** — plus a two-update case so "that update" cannot silently resolve to the
wrong record.

A pointer to something unreadable is worse than no pointer, because it reads as
an available affordance — which is why this is **mandatory scope**, not a
conditional. *(Rev 12 kept an "if it is not built, remove the claim" branch
standing beside the mandatory declaration. That is the fifth stale-contradiction
in this spec, and the first on a point the founder had explicitly decided.)*

#### Governing principle: context must earn its place

Kernos claims its context is **curated to what is immediately helpful to the
agent's awareness**. That is a product claim, and nothing currently enforces it
at the whisper/context-offer boundary — any producer can push arbitrary text
into the model's situation context and it will be carried.

The rule this spec adopts, and which the substrate event violates on both
counts: **an injection must be actionable for the turn it appears in, and the
agent must be able to act on it without evidence it does not have.** Extract the
part that meets that bar; leave the rest retrievable.

Applied here: "an update happened, and here is what changed for you" is
actionable. "`f38b31f Eighth corrective: confine archive refs`" is not — the
agent cannot inspect it, verify it, or do anything with it, and its only
available action is to repeat it.

#### The payload itself is also wrong — push the fact, pull the detail

Owner judgement, and I agree on the stronger reading: not publishing the
changelog is necessary but not sufficient. **The agent should not be handed the
commit list either.**

The raw form gives the agent **false specificity**. It "knows" about
*"the governance-lifecycle failure-state enumeration"* with no ability to
inspect, verify or reason about it, and cannot act on `f38b31f` in any way. What
it *can* do is speak confidently about changes it does not understand — which is
a hallucination surface, and is precisely what produced the observed parroting.
The commit subjects are also written by engineers in an engineering register,
which invites the agent to adopt that voice.

What is genuinely useful to the agent is small, and it is about **user impact**,
not about code:

- **that** an update applied, and **when** — so it does not claim stale
  capabilities and can answer honestly if asked whether it restarted;
- a **user-impact classification** (below);
- an **`event_id`**, resolvable via `inspect_update(event_id)` (below).

#### User impact must be OBSERVED, never inferred from commit prose

The owner's framing is the right target: the agent should be able to say
*"updated some internal nuance — nothing you'd notice"* rather than reciting
SHAs. But that claim has to be **earned**. Deriving "does this affect the user"
by reading commit subjects is the same false specificity as the changelog,
pointed the other way — and more dangerous, because a wrong "nothing changed" is
*reassuring*.

Impact is therefore computed from surfaces the substrate can actually **diff
across the update**, never from prose:

| Observation | Claim it supports |
|---|---|
| tool catalog delta (names before/after) | "I can now do X" / "X is gone" |
| covenant / default-preference delta | "a standing rule changed" |
| procedure surface delta | "how I do X changed" |
| delta computation **failed** | `unknown` |
| all three computed successfully, **all empty** | **`unknown`** — see below |

**There is no `internal_only`.** Rev 9 let a successful zero across those three
surfaces support *"nothing you'd notice"*. kreview refuted it with a
counterexample that is this very change: **removing raw changelogs from replies
is something the user notices, while tool, covenant and procedure deltas are all
empty.** So are response semantics, disclosure gates, notification routing,
model/config behaviour, and command output.

I had applied "absence of a computed delta is not evidence of absence of change"
to the *failure* branch and then violated it one branch over. The three tracked
surfaces are an **incomplete observer**; a successful zero proves only that
*those three* did not change — never that nothing did.

**Report what was measured, not a conclusion drawn from it:**

> "Kernos updated. I did not detect changes to my tools, standing rules, or
> procedures."

That sentence is true and bounded. *"Nothing you'd notice"* is neither.
`internal_only` would become supportable only behind a **complete authoritative
change classifier** proving every user-observable surface unchanged — no such
completeness invariant exists, so the class is not offered.

The full changelog is engineering telemetry already persisted in two appropriate
places. If the user asks *what changed*, the agent calls
`inspect_update(event_id)` for **that exact event** and answers with the record
in front of it, rather than reciting from ambient context. (Rev 9 said it reads
`.auto_update_log.md`; that file is unreachable by `read_file` and is
overwritten by the next update — see below.)

This matches how Kernos handles everything else: small ambient context, detail
retrieved when needed. The current design pushes maximum detail at the moment of
least relevance. `format_update_event_text` is therefore reduced to the fact, the timestamp,
the impact observation and the `event_id`; it does not enumerate commits.

Note the producer **currently** sets `owner_member_id=""` — that is the
**pre-fix behaviour**, not the final contract. It is simultaneously the concrete
instance of the empty-owner case in the authorization section, and it is
replaced by owner resolution at enqueue (below).

### B. Ambient whispers ride on slash-command output

`/model` is an admin surface, not a conversation. Attaching ambient
self-reflection to command output is a category error: the user asked the system
to do a thing, not to muse about them while doing it.

**Non-goals.** Suppressing whispers generally, changing what `fact_harvest`
detects, changing whisper dedup/expiry/suppression, or weakening the
DELIVER-ON-DELIVERY guarantee that an offered whisper is never silently dropped.

## Design

### Part A — Translate at the delivery boundary

`_deliver_pending_whispers` renders second-person, reader-addressed text rather
than emitting the stored insight raw. The stored `insight_text` is unchanged —
it remains the agent-facing artifact, and the audit/receipt trail keeps the
original wording.

**Resolved by kreview: DUAL-REGISTER PAYLOAD.** Rev 1 proposed translating at
delivery (A2). That was refused for two good reasons:

- **Re-voicing arbitrary prose is not a deterministic contract.** Pronoun
  substitution is semantically unsafe — "they" may legitimately refer to
  *someone other than the reader*, and a template cannot tell the difference.
- **Withholding contradicted my own non-goal.** Rev 1 said a whisper that could
  not be re-voiced should be dropped, while declaring "never silently dropped" a
  non-goal. Both cannot hold.

The mechanism is therefore to **produce both registers in the existing
`fact_harvest` call** — no second LLM call, no re-voicing:

- `insight_text` — unchanged, agent-facing, what the audit and receipts keep.
- `user_facing_text` — explicitly addressed to the reader.

Delivery selects the **audience-tagged reader-facing field** at the boundary.
Structured proposal data from which both forms derive deterministically is an
acceptable alternative shape.

**`audience` is required with no default, and `READER` requires a validated
non-empty `user_facing_text`.** (Rev 6 stated this as an unconditional
"`user_facing_text` is required of every producer"; that is now a READER rule,
so the tagged union and this section can both be satisfied.) kreview
ruled on my open ask and chose the harder option deliberately: breaking every
producer *is* the enforcement pressure. A default would let old rows deserialize
silently and turn the invariant back into a convention.

At persistence boundaries, legacy dictionaries are **inspected and migrated
before** a `Whisper` is constructed — the model is never weakened to accommodate
them.

Every producer sets it explicitly, including those whose text is already
reader-facing (`user_facing_text=insight_text`).

*Rev 2 proposed inferring "already reader-facing" from an audited-producer
allowlist when the field was absent. kreview refused it and is right: delivery
cannot safely infer producer intent from absence, and an allowlist silently
grants reader-facing status to anything added later.* **A missing field means
legacy/unrenderable, everywhere, with no exceptions.** Enforced by a central
construction path plus a static audit that also covers `Whisper(**proposal)`
dict construction, which would otherwise bypass a keyword-level check.

**Both delivery paths must select the reader-facing field.** Rev 2 named only
`_deliver_pending_whispers`. Verified second path:
`AwarenessService._push_interrupt` sends `message=whisper.insight_text` **and**
writes the same agent-facing text into conversation history via
`_store_whisper_message`. An interrupt-path assertion is required so a future
agent-facing record cannot bypass the audience boundary. Agent-awareness context
continues to consume `insight_text` — that is the point of keeping both.

### The withheld lifecycle vs. 48-hour expiry (kreview round-2 P0)

Rev 2 said unrenderable records "remain pending, never marked surfaced, with
expiry unchanged". **Those three cannot all hold**, and I verified why:
`state_json.get_pending_whispers` expires a pending row by setting
`surfaced_at` — recording a whisper as *surfaced* that was never shown. SQLite
expires pending rows too. So a withheld record would silently age out at 48h and
be recorded as delivered: the same dishonesty this spec exists to remove, one
layer down.

Bound before implementation:

- **Every legacy row missing the field goes to `WITHHELD`.** No producer
  inference, no adjudication by me. kreview's reasoning: explicitly resolving a
  known reader-facing row later is safe, whereas silently backfilling a
  *misclassified* agent-facing row recreates the defect. New code has no
  ambiguity because the field is required.
- **Normal ready-whisper 48h expiry is unchanged.** Only `WITHHELD` is exempt,
  because expiry-as-surfaced is a lossy terminal transition for a record that
  was never shown. `WITHHELD` must not inherit that debt.

### The WITHHELD state machine (kreview round-3 P0)

Rev 3 named a durable state and gave it **no way out**. "Operator-visible until
explicitly resolved" is not a recovery path — it does not say who may resolve
it, through what API, or what transition occurs. The two known agent-facing rows
would have been preserved as bytes and stuck forever, weakening
deliver-on-delivery indefinitely rather than restoring it.

| From | To | Via |
|---|---|---|
| `READY` | `SURFACED` / `DISMISSED` / `EXPIRED` | existing paths, unchanged |
| legacy missing field | `WITHHELD(reason=missing_user_facing_text)` | migration, atomically — **never** through `surfaced_at` |
| `WITHHELD` | `READY` | `release_withheld_whisper(id, user_facing_text)` — preserves the original `insight_text`, records resolver and time |
| `WITHHELD` | `DISMISSED` | explicit dismissal with a reason |

**A Python method is not a recovery surface** (kreview round-4 P0). Rev 4 named
`release_withheld_whisper(...)` and stopped — `/dump` gave the operator an id
and nothing to do with it. A state that only an in-process function can leave is
still operationally stuck.

**Reachable surface, in scope and tested end to end:** an owner-authorized
`/whispers` command with `release` and `dismiss` subcommands, mirrored as
System-space tools per this repo's existing admin pattern. The tested path runs
from the id shown in `/dump` all the way to the backend mutation — not from the
function inwards.

**Authorization** validates the actor against the record's `owner_member_id`. An
instance-owner path for empty-owner records is **mandatory, explicit and
audited** — not optional, and not implied by role.

### Contended transitions need a compare-and-set contract

Release and dismissal are **atomic, occurrence-qualified** transitions in both
backends, with a result enum and defined precedence:

`RELEASED` · `ALREADY_RELEASED` · `DISMISSED` · `ALREADY_DISMISSED` ·
`READY_CONFLICT` · `INVALID_INPUT` · `STATE_CONFLICT` · `NOT_FOUND` ·
`UNAUTHORIZED` · `STATE_ERROR`

*Rev 5 used `READY_CONFLICT` in prose but omitted it from the enum, and claimed
"defined precedence" while defining none. Without an order, two competing truths
can legitimately return different results on JSON and SQLite.*

**Ordered decision table, evaluated inside the atomic transition:**

*Rev 6 ordered "authorization" before "not found" and claimed that prevented id
probing. kreview showed the claim is incoherent: **determining whether a record
belongs to you requires finding it first.** An existing foreign id returning
`UNAUTHORIZED` while a random id returns `NOT_FOUND` is precisely the
enumeration oracle the ordering was supposed to close. I asserted a property the
mechanism could not deliver.*

The fix is to split **surface eligibility** from **record lookup**:

| # | Stage | Condition | Result |
|---|---|---|---|
| 1 | — | document unreadable / malformed / unknown version | `STATE_ERROR` |
| 2 | **surface** | actor may not use this command/tool at all | `UNAUTHORIZED` |
| 3 | **lookup** | no record for `(instance_id, id, authorized-owner-scope)` | `NOT_FOUND` |
| 4 | | invalid `user_facing_text` supplied (release) | `INVALID_INPUT` *(non-mutating)* |
| 5 | | already in the **same** terminal state, same digest | `ALREADY_RELEASED` / `ALREADY_DISMISSED` |
| 6 | | already in the **opposite** terminal state, or same state with a *different* digest | `STATE_CONFLICT` |
| 7 | | release, and a newer `READY` occurrence exists on this signal | `READY_CONFLICT` (stays `WITHHELD`) |
| 8 | | exact `WITHHELD` match | `RELEASED` / `DISMISSED` |

Stage 2 rejects actors who cannot use the surface **at all** — no record is
consulted, so nothing leaks. Stage 3 then queries **scoped**: a record outside
the caller's authorized scope and a record that does not exist collapse to the
**same observable `NOT_FOUND`**. The instance owner's scope explicitly includes
`owner_member_id == ""`.

If an internal API needs a distinguishable foreign-record `UNAUTHORIZED`, it
stays **off the user-observable command/tool surface**.

Rule 6 catching the differing-digest case is what stops a retry with *different*
text overwriting a `READY`, `SURFACED`, or already-released occurrence.
Concurrent release-versus-dismiss therefore has exactly one winner; the loser
lands on rule 6.

### A required field is not a valid value

*Rev 6 said `user_facing_text` is "required with no default" and treated that as
the invariant. kreview: a plain dataclass field rejects **omission**, not
`""`, whitespace-only, or a non-string.* The consequence is concrete — release
could move a record `WITHHELD → READY` with a blank reader payload, delivery
would then emit nothing useful and still mark the occurrence surfaced, breaking
DELIVER-ON-DELIVERY through the door I had just finished bolting.

Binding, at **central construction and at the persistence boundary**:

- `user_facing_text` must be a **non-empty string**. Whitespace-only **fails**.
  (Surrounding whitespace on an otherwise valid string is preserved verbatim, so
  the digest is stable.)
- The same rule applies to new-producer construction **and** to persisted rows
  where the key exists but the value is invalid.
- Legacy rows with an invalid payload migrate to `WITHHELD` — same destination
  as a missing field, since "present but unusable" is not better evidence than
  "absent".
- Operator release with an invalid payload returns **`INVALID_INPUT` and mutates
  nothing**; it is a caller error, not a state transition.
- The release **digest is computed over the exact validated persisted string**,
  so idempotency and the differing-digest conflict rule agree on one value.

### Every migrated row must have a reachable owner

`Whisper.owner_member_id` defaults to `""`, and several legacy producers omitted
it — the schema calls that legacy/instance-wide. **Equality against an empty
owner authorizes nobody**, so rev 5 would have moved those rows to `WITHHELD`
and then left them permanently unreleasable: exactly the stuck state this whole
section exists to remove, reached through the authorization door instead.

**Policy, binding:** a record with an empty `owner_member_id` is instance-wide,
and **only the audited instance owner** may release or dismiss it. This path is
**mandatory, not optional** — without it those records are unreleasable. It is
explicit and audited rather than implied by role, and the instance owner's
authorized scope explicitly includes `owner_member_id == ""`.

### The same-signal collision

Because `WITHHELD` deliberately does not block automatic dedup, a **newer
`READY` occurrence may already exist** on that signal by the time an operator
releases the old one. Rev 4 did not say what happens then, and both naive
outcomes are bad: an ordinary upsert silently dropping the released record, or
two `READY` rows existing while the spec claims dedup is unchanged.

**Adopted: `READY_CONFLICT`** — release returns it and leaves the record
`WITHHELD` until the newer `READY` occurrence resolves. kreview's reasoning, and
I agree: it preserves the automatic queue invariant and stays recoverable,
whereas an occurrence-scoped coexistence override would weaken the dedup claim
made two sections up.

**Dismissal is occurrence-scoped — and the real broad key is
`knowledge_entry_id`, not `foresight_signal`.** Verified:
`AwarenessService._is_suppressed` looks receipts up **by
`knowledge_entry_id`** and treats any receipt in
`{surfaced, dismissed, acted_on}` as suppressing later whispers from that entry.

So rev 5's test would have guarded the wrong key entirely: it could pass while a
later occurrence sharing the same `knowledge_entry_id` was still starved.
Dismissing one legacy `OPERATIONAL_INSIGHT` would silently suppress every
corrected insight derived from that entry — the starvation bug arriving through
the door I wasn't watching.

The dismissal receipt must therefore be **distinguishable from ordinary
topic/entry suppression**: it is scoped to the occurrence (`whisper_id`), and
`_is_suppressed` must not treat an occurrence-scoped dismissal as entry-level
suppression.

**Migration is atomic and idempotent under concurrent first reads**, so neither
backend can produce both a `READY` and a `WITHHELD` copy of one legacy row.

**No automatic agent action may manufacture the reader-facing text** for a
withheld record. The missing register is a human judgement — that is the whole
point of the state existing.

### Queue interactions

`WITHHELD` is excluded from `get_pending_whispers`, from the 48-hour ready
expiry scan, and from queue-bound deletion.

**The dedup hazard kreview caught:** a withheld row must not block a later valid
`READY` whisper sharing its `foresight_signal`. Otherwise either of the two
stuck legacy operational-insight rows would suppress **every corrected
post-upgrade insight on that signal, forever** — the fix silently causing a
worse version of the original problem. Dedup is therefore defined **separately
for `READY` and `WITHHELD`**: the withheld audit record is retained, and a newly
renderable occurrence is admitted to `READY`.

Vocabulary: this spec uses **withheld**, never *suppressed* — the latter already
carries durable lifecycle meaning in the whisper substrate.

### Part B — No ambient whispers on command surfaces

Slash-command replies do not carry pending whispers. **All recognized slash
commands defer uniformly, including `/selfreview`** — a reflective *topic* does
not make command output a conversational surface. Unknown leading-slash input is
**not** treated as a command: it falls through to the ordinary conversational
path and carries whispers normally, so a typo cannot silently swallow delivery.

**Rev 1 had the state handling backwards.** It said the offered batch must *not*
be popped so it "survives" — kreview showed that leaves stale transient state
with three real consequences: the batch persists under one space key, the same
whisper can later be delivered and marked from a *different* space, and the
tolerant instance-wide fallback can then pop the stale batch and deliver it a
second time.

Correct behaviour: on a slash turn, **clear the transient offered batch** and
**do not mark or suppress any durable whisper**. Durability does not depend on
the in-memory batch — the durable pending whisper is the source of truth, and
the next conversational assembly re-offers it from pending state. Deferral comes
from the durable record, not from holding transient state open.

`/dump` and other operator surfaces are unaffected — they never carried
whispers.

## Acceptance criteria

1. **Delivery emits the audience-tagged reader-facing field**, never
   `insight_text`. kreview is right that criterion 1 in rev 1 was semantic and
   unprovable by banning words like "they", so it is replaced by two checks:
   - the delivered string is exactly `user_facing_text` (field-level assertion);
   - **pinned exact renderings** for the two captured operational-insight cases,
     so the contract is fixed against real data rather than a generated example.
2. Tests **retain legitimate third-party references** — a whisper genuinely
   about someone other than the reader must survive unaltered, proving the fix
   is a register contract and not a pronoun filter.
3. The stored `insight_text` is unchanged by delivery; receipts and audit keep
   the original agent-facing wording.
4. A whisper pending when a slash command is handled is **not** delivered that
   turn and **not** marked surfaced or suppressed, and **is** delivered on the
   next conversational turn — including when that next turn is in a
   **different space**. A **third** turn then proves **no duplicate delivery**.
5. `/selfreview` defers like every other recognized command. Unknown
   leading-slash input is conversational and delivers normally.
6. **`audience` is required with no default; `READER` requires a validated
   non-empty `user_facing_text`.** Omitting `audience`, or pairing an audience
   with the wrong payload shape, raises at construction. The static audit is now SECONDARY defence, not the
   invariant. Producer scope includes `behavioral_patterns.py` and every dict
   proposal builder; mutation-proved that `Whisper(**proposal)` without the
   field both raises and fails the audit.
7. **Both delivery paths select the reader-facing field.** Asserted separately
   for `_deliver_pending_whispers` and for `_push_interrupt`, including the text
   `_store_whisper_message` writes into conversation history. Agent-awareness
   context still receives `insight_text`.
8. **Persistence round-trips preserve the field** in both JSON and SQLite
   backends, with a compatibility test for rows written before this change.
9. **A `WITHHELD` record does not expire into `surfaced_at`.** Directly tested
   against the verified expiry path — a withheld record aged past 48h remains
   withheld and operator-visible, never recorded as surfaced. Normal
   ready-whisper 48h expiry is unchanged, also tested.
9b. **`WITHHELD` has a REACHABLE exit.** Tested end to end from the id `/dump`
   shows, through the `/whispers release|dismiss` surface, to the backend
   mutation — on **both** JSON and SQLite. Authorization is asserted against
   `owner_member_id`, with any instance-owner override explicit and audited.
   No automatic path may manufacture the reader-facing text.
9d. **Contention has exactly one winner.** Concurrent release-versus-dismiss
   yields one terminal decision and `STATE_CONFLICT` for the loser. Retry is
   idempotent only for the same decision, and for release only for the same
   `user_facing_text` digest — a retry with different text must not overwrite a
   `READY`, `SURFACED`, or already-released occurrence.
9e. **Releasing into an existing newer `READY` returns `READY_CONFLICT`** and
   leaves the record `WITHHELD`. Asserted that no upsert silently drops it and
   that two `READY` rows are never created.
9f. **Dismissal is occurrence-scoped against the REAL broad key.** Test: W and N
   share **both** `foresight_signal` **and** `knowledge_entry_id`, with distinct
   `whisper_id`. Dismiss W, then N must still admit and deliver. A test using
   only `foresight_signal` passes vacuously, because `_is_suppressed` keys on
   `knowledge_entry_id`.
9h. **Empty-owner rows are releasable.** A migrated row with
   `owner_member_id == ""` is instance-wide and released or dismissed by the
   audited instance owner only — asserted end to end on both backends, plus a
   negative case proving a non-owner member gets `UNAUTHORIZED`.
9i. **The decision table is asserted in order**, and a same-state retry with a
   *different* digest returns `STATE_CONFLICT` rather than overwriting.
9m. **No enumeration oracle.** An **existing record outside the caller's scope**
   and a **random nonexistent id** produce byte-identical observable results
   (`NOT_FOUND`) on **both** backends. Asserted directly — this is the property
   rev 6 claimed and did not have.
9n. **Invalid reader payloads cannot enter `READY`.** Missing, `""`,
   whitespace-only, and non-string are each rejected at construction and at the
   persistence boundary; legacy rows carrying them migrate to `WITHHELD`;
   operator release returns `INVALID_INPUT` and mutates nothing; and a release
   retry after correction succeeds. Tested on both backends. Mutation: accept a
   blank payload and the DELIVER-ON-DELIVERY test must fail.

9r. **The update notification reaches the OWNER, not whoever is next.** The
   producer currently sets `owner_member_id=""`, which the substrate treats as
   instance-wide — so the first non-owner member to take a turn could receive
   and surface the owner's update notification, while `inspect_update` is
   owner-scoped and unavailable to them, and the owner might never be told. The
   instance owner is resolved **at enqueue** and the whisper bound to that
   member; if no owner resolves it enters **`AWAITING_OWNER`** (below) rather
   than broadening to everyone. Tested: a non-owner takes the first
   post-update turn and sees nothing and cannot inspect; the owner then receives
   it once and can inspect that exact event.
9s. **Retention is time-bounded AND hard-capped — two different claims.** The
   50/90 union is a *time* window, not a storage ceiling: at a high enough
   update rate it grows without limit, so rev 13's "bounded storage" assertion
   was stronger than its own rule. v1 therefore states both: retain the
   **newest 50** records **and everything within 90 days**, subject to a **hard
   ceiling of 500 records**, and **64 KiB per record measured over the canonical
   serialized form** — not just the changelog. Variable fields are reduced in a
   deterministic order (changelog, then measured-delta strings, then delta
   collections), truncated on UTF-8 boundaries with an explicit marker, so an
   oversized *non-changelog* delta is bounded too. **When the ceiling binds it wins over the 90-day window** —
   oldest-first eviction, discoverability sacrificed to the ceiling, and
   `applied_count` on the aggregate still reports the true total. The
   acceptance test drives the **ceiling**, not a low-rate example. **The ceiling wins, unconditionally.** Rev 14 also promised a
   surfaced-but-uninspected event is kept to the age bound *regardless of
   count* — 501 such events inside 90 days makes those two rules contradict.
   Precedence is explicit: surfaced-uninspected events are preferred for
   retention **within** the ceiling, and evicted by it when it binds. So
   `inspect_update` **can honestly return `NOT_FOUND` before 90 days**, and
   that failure is tested rather than pretended away. The **single owner-notification aggregate** is retained until terminal
   delivery; **member event records have no exemption**. After eviction `inspect_update` returns **scoped
   `NOT_FOUND`** — no tombstone, so a foreign owner's event id and a random
   nonexistent id remain **observationally identical**, per 9m. Eviction
   **never mutates an already-surfaced notification receipt**.
9t. **`event_id` is discoverable on a real later turn.** Two actual turns: the
   update fallback is delivered, then the owner asks "what changed?" **without
   supplying an id**, and `inspect_update()` resolves the exact surfaced event.
   A two-update case asserts "that update" cannot resolve to the wrong record.
   The test may **not** inject the id synthetically — that would pass while the
   conversational path fails.
9x. **Aggregation continues through the READY phase.** Two constructed
   schedules, because owner-discovery and delivery are different boundaries:
   **(a)** owner present from E1, 51+ updates before the owner takes any turn;
   **(b)** owner established mid-stream, then further updates before the first
   owner turn. Both assert **one** notification, the **full** applied count,
   correct retained-id membership, **no dedup loss, no replacement, no flood**,
   and restart idempotency **across the READY phase** — not merely up to owner
   establishment.
9u. **`AWAITING_OWNER` binds and delivers exactly once — at scale.** The
   single-event case, plus the constructed schedule kreview specified: **at
   least 51 updates spanning more than 90 days with no owner**, repeated
   startup/enqueue reconciliation throughout, then owner establishment. Assert
   **the storage ceiling holds** (event records obey 50/90 *and* the 500-record
   / 64 KiB caps; the aggregate stays one record), **exactly one** notification (no flood, no dedup loss), **no
   dangling `event_id`** in the aggregate after eviction, and **restart
   idempotency**. One awaiting event cannot distinguish flood from loss, which
   is why the multi-update case is the criterion.
9v. **The no-id predicate is pinned branch by branch — all five.** Explicit id →
   that exact event. Receipt referencing exactly one retained event → that
   event. Referencing several → a list. **Referencing only evicted events →
   scoped `NOT_FOUND`, never walking back to an older receipt** (reachable:
   surface E1, then enqueue past the ceiling with no further surfaced turn).
   No surfaced receipt at all → scoped `NOT_FOUND`. Each branch asserted
   separately, not summarised as "cannot choose wrong".
9w. **Immutable event data and mutable delivery state are separate.**
   `event_id`, timestamp, measured deltas and changelog are never rewritten;
   owner binding, state, aggregate membership and receipts live in delivery
   metadata. Asserted by mutating delivery state and re-reading the event record
   byte-identical.
9q. **The pull path exists — mandatory, not conditional.**
   `inspect_update(event_id)` returns the record for that exact event, is
   owner-scoped, and survives a subsequent update (asserted by applying a second
   update and re-reading the first). A "what changed?" tool turn is tested end
   to end. There is no omission branch.

9o. **Impact claims never outrun the observer.** With a tool added, the payload
   supports "I can now do X". When computation **fails**, `unknown`. When all
   three deltas compute successfully as **empty**, also `unknown` — the payload
   reports what was measured and asserts nothing about overall impact.
   **Two mutations, because the second is the quiet one:** force the computation
   to fail and assert no "nothing changed" claim; *and* compute all three
   successfully empty for a change to whisper reply rendering — this spec's own
   change — and assert the payload does NOT claim `internal_only` or "nothing
   changed". Testing only the failure branch misses the false-reassurance branch.
9l. **The substrate event payload carries no commit list.** Asserted that it
   carries the fact, the timestamp, the impact observation and an `event_id`,
   and that no commit SHA or subject appears in it. A "what changed?" turn is
   served by `inspect_update(event_id)`, never from ambient context.


9g. **Legacy migration is atomic and idempotent under concurrent first reads** —
   neither backend may produce both a `READY` and a `WITHHELD` copy of one row.
9c. **A withheld row never starves its signal.** A `WITHHELD` record sharing a
   `foresight_signal` with a later valid `READY` whisper must NOT suppress it —
   otherwise the two stuck legacy rows would silently block every corrected
   insight on that signal forever. Dedup is asserted separately for `READY` and
   `WITHHELD`, and `WITHHELD` is excluded from `get_pending_whispers`, the ready
   expiry scan, and queue trimming.
10. `/dump` shows a distinct `WITHHELD WHISPERS` section, preserves member and
    disclosure scoping, and reading it does not mutate the record.
11. Whisper dedup, busy-state suppression and disclosure-gate scoping are
    otherwise unchanged, verified by the existing suites.
12. Mutation-proved: delete the field selection and the register test must fail;
    delete the command-path guard and the deferral test must fail; leave the
    transient batch uncleared and the different-space duplicate test must fail;
    remove the withheld exemption and the expiry test must fail.

## Operator surface

A **separately labelled `WITHHELD WHISPERS` section** in `/dump` — *not* mixed
into `QUARANTINED REPORTS`, which is about misclassified friction and would
conflate two unrelated conditions.

Per record: stable identity, producer/signal, age, and reason. **Member and
disclosure scoping are preserved**, and it shows no more private text than the
diagnostic contract already permits — a withheld whisper may contain
member-scoped content, so the recovery surface must not become a disclosure
bypass. Reading `/dump` **must not mutate** the withheld record; asserted.

## Settled by kreview round 3

Both of my open asks were ruled toward the stricter option, and both rulings are
adopted: the reader payload is **required and validated for `READER`** (rev 11
makes that conditional on `audience` rather than universal), and **every**
legacy row missing it goes to `WITHHELD` with no producer adjudication.

Recorded separately, not fixed here: ready-whisper expiry recording itself
through `surfaced_at` is semantically dishonest — a lossy terminal transition
written as a delivery that never happened. kreview confirmed changing normal
ready expiry is outside this spec's scope; `WITHHELD` simply must not inherit
the debt.
