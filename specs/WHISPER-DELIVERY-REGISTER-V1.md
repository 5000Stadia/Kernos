# WHISPER-DELIVERY-REGISTER-V1 — say it in the reader's register

**Status:** Draft rev 7 (kreview round 6; write-side cutover, stopped-install apply, closed payload union)
**Modules:** `kernos/kernel/awareness.py` (the `Whisper` dataclass and
`AwarenessService._push_interrupt`), `kernos/messages/handler.py`
(`_deliver_pending_whispers`, slash dispatch), `kernos/setup/self_update.py`,
**`kernos/kernel/state_json.py`**, **`kernos/kernel/state_sqlite.py`**,
**`kernos/setup/whisper_register_cleanup.py`** (new — the pre-deployment
command), **`kernos/server.py`** and **`kernos/repl.py`** (a read-only startup
*verification*, not a migration), every direct `Whisper(...)` producer, `tests/test_handler.py`,
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
2. **The operator reviews and approves** that manifest.
3. `--apply` re-enumerates, requires an **exact match** against the approved
   manifest, and on any mismatch mutates nothing and exits non-zero.
4. **Only then** is the required-field code deployed.

**The approved manifest IS the audit record.** That removes the two-effect
problem entirely — there is no separate audit sink to keep atomic with the
delete, so JSON needs no versioned document shape and no sentinel object in its
bare list. `--apply` writes a completion marker beside the manifest recording
when it ran and against which digest.

**`--apply` requires a STOPPED install, and verifies it rather than trusting
it.** SQLite serializes its own writes, but JSON apply snapshots a bare list and
replaces it by temp+rename — so a valid whisper written by the live server
between the read and the rename is **silently overwritten**, even though it was
never a deletion candidate. A cleanup that can destroy unrelated data is worse
than the two rows it removes.

Operator discipline is not a mechanism, so `--apply` **positively checks the
service lease / process lock and refuses while it is held**, in ordinary phase-N
use and not only in post-refusal recovery. A barrier-controlled concurrent-writer
test proves no unrelated row is lost.

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
evidence of cleanup** — it is the absence-of-evidence error this spec has already
been caught making twice, applied to a release decision. Two installs exist
(`Kernos-main` live, `Kernos` dev); `--report` against each returning zero
candidates is achievable evidence, and it is the gate.

If the backstop is ever *deliberately* allowed to catch stragglers instead, that
is a rollout policy with an accepted outage and must be stated as such — not
implied by a waiting period.

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
5. **Pre-deployment cleanup, per install, authoritative backend only.**
   `--report` enumerates by raw row reads and writes the manifest into the named
   data root; `--apply` re-enumerates and requires an exact match.
   Asserted: `--report` twice on unchanged data yields an **identical** manifest
   (proving canonicalization); **any** mismatch — extra row, missing row,
   changed reader value, changed `surfaced_at`, changed supporting evidence —
   aborts with **zero mutations** and a non-zero exit; an interrupted `--apply`
   leaves no partial state (JSON: temp discarded, document byte-unchanged;
   SQLite: transaction rolled back); `--apply` is idempotent; **no `surfaced_at`
   is ever written** by this path; remnants in the **inactive** backend are
   reported but untouched.
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
   after construction.
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
