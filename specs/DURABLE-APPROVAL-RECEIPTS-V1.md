# DURABLE-APPROVAL-RECEIPTS-V1

**Date:** 2026-05-21 (revised post-Codex round 1: YELLOW → 4 blockers folded)
**Status:** Draft for review
**Scope:** A generic substrate primitive: SQLite-backed approval
  receipts that survive process restart, carry per-act binding,
  bind to a specific workflow execution + gate nonce, and resolve
  via operator slash commands (`/approve <approval_id>` /
  `/reject <approval_id> <reason>`). The first sub-spec of the
  `KERNOS-AUTONOMOUS-IMPROVEMENT-LOOP-V1` arc, but generic enough
  that any future hard_write capability needing durable operator
  approval reuses it.
**Estimated size:** ~250 LOC source (schema + helpers + slash
  handlers + workflow action verb) + ~200 LOC tests.

## Why this spec exists

`DispatchGate.ApprovalToken` (`gate.py:57`) is process-scoped
(`self._approval_tokens: dict[str, ApprovalToken] = {}`) with a
short TTL. It is the right shape for an in-conversation
"did you mean to delete this file" confirmation — issue token,
ask user, user confirms, second tool call carries token, gate
validates. The token's job is done within a single conversation
turn.

It is the **wrong shape** for any approval that needs to:
- Survive a process restart (the dict is in memory; restart loses
  all pending approvals).
- Bind to a SPECIFIC act (not just `tool_name + tool_input_hash`
  — sometimes the approval is for a particular workflow execution,
  a particular commit cycle, a particular target SHA, etc.).
- Carry a meaningful operator decision over hours or days (the
  autonomous-improvement loop's commit gate is 24h by design).
- Be retrievable by the substrate for re-verification at execution
  time (the operator approved THIS diff against THAT base — verify
  both haven't drifted).

`KERNOS-AUTONOMOUS-IMPROVEMENT-LOOP-V1`'s D7 needs all four. So do
several near-term primitives the audit surfaced as gaps:
hard_write live-dispatch policy gating, cross-member data writes
needing recipient confirmation, external-network destructive ops
the operator wants to approve once per occurrence rather than
once-per-class.

Building a bespoke approval flow for each is the wrong move. One
substrate primitive — receipts — composes for all of them.

## Current state (truth, not memory)

### What exists and is wired

- **`DispatchGate.ApprovalToken`** (`kernos/kernel/gate.py:57`):
  in-memory dict, single-use, no persistence. Validates against
  `(tool_name, tool_input_hash)`. Issued by
  `issue_approval_token`; consumed by `validate_approval_token`.
  Lost on restart. Fine for its use case.
- **Workflow approval gates** (`kernos/kernel/workflows/execution_engine.py:1677`):
  `_on_post_flush_for_gates` is a post-flush hook that wakes
  paused workflow executions when a matching event arrives. The
  wake requires: `execution_id` match + `gate_nonce` match (engine-
  minted, persisted on the `workflow_executions` row) + descriptor
  predicate match (author-controlled). `gate_nonce` column +
  pending-gate restart-resume already shipped (`execution_engine.py:314`,
  `_restart_resume_pass`).
- **Slash-command handler pattern** (`kernos/messages/handler.py:4280+`):
  `elif _cmd_lower == "/dump"` style dispatch in the handler's
  message-routing block. Owner-role check available via
  `self._instance_db.get_member(primary_ctx.member_id)`.

### What does NOT exist

- A **durable receipt table** keyed by `approval_id`, with state
  machine (`pending | approved | rejected | expired | consumed`),
  binding fields, and per-receipt expiry.
- An **agent / workflow-callable helper** to "request an approval
  receipt" (creates a row, returns the `approval_id`, emits a
  notification to the operator).
- **`/approve` and `/reject` slash commands** that look up a
  receipt by `approval_id`, verify operator identity, mutate state,
  emit the `approval.decision_recorded` event the workflow gate
  consumes.
- The event-stream emission shape that the existing workflow
  approval-gate mechanism can match against (the gate consumes
  events; receipts must EMIT one on approve/reject).

## Design

### D1 — Receipt is the generic substrate; per-use-case fields live in `binding_payload`

A single core `approval_receipts` table that every use case
consumes. Use-case-specific fields go in a JSON `binding_payload`
column the caller writes at request time and re-verifies at consume
time. This keeps the substrate generic and pushes use-case
semantics where they belong (in the caller).

Schema:

```sql
CREATE TABLE approval_receipts (
    approval_id            TEXT PRIMARY KEY,         -- UUID
    instance_id            TEXT NOT NULL,
    kind                   TEXT NOT NULL,            -- 'autonomous_commit', 'hard_write', etc.
    requested_for_actor    TEXT NOT NULL,            -- whose action is being approved
    operator_actor_id      TEXT NOT NULL,            -- env-derived (KERNOS_OPERATOR_ACTOR_ID) — authoritative for slash-command identity check
    operator_member_id     TEXT NOT NULL DEFAULT '', -- Kernos member_id of the operator; populated at request time when distinct from operator_actor_id (Codex r1#6)
    workflow_execution_id  TEXT,                     -- nullable; null when not workflow-gated
    gate_nonce             TEXT,                     -- mirrors the workflow gate's nonce
    request_summary        TEXT NOT NULL,            -- human-readable, surfaced to operator
    binding_payload_json   TEXT NOT NULL DEFAULT '{}', -- caller-defined per-receipt context; IMMUTABLE after request (Codex r1 answer 1)
    binding_schema_version INTEGER NOT NULL DEFAULT 1, -- caller-versioned; consume-time check refuses unknown versions
    outcome_payload_json   TEXT NOT NULL DEFAULT '{}', -- written by consume_approval for caller write-backs (e.g., commit_sha); separate from binding so binding stays immutable
    state                  TEXT NOT NULL DEFAULT 'pending'
                           CHECK (state IN ('pending','approved','rejected','expired','consumed')),
    state_reason           TEXT NOT NULL DEFAULT '',
    requested_at           TEXT NOT NULL,            -- ISO 8601 UTC
    decided_at             TEXT,                     -- ISO 8601 UTC, NULL until decided
    expires_at             TEXT NOT NULL,            -- ISO 8601 UTC
    consumed_at            TEXT,                     -- ISO 8601 UTC, NULL until consumed
    single_use             INTEGER NOT NULL DEFAULT 1 CHECK (single_use IN (0,1)),
    decision_emitted_at    TEXT                      -- ISO 8601 UTC, NULL until decision event emit attempted; boot-reconcile re-emits any terminal receipts where this is NULL (Codex r1#4 durability)
);

CREATE INDEX idx_approval_receipts_state
    ON approval_receipts (state);
CREATE INDEX idx_approval_receipts_pending_per_instance
    ON approval_receipts (instance_id, state)
    WHERE state = 'pending';
CREATE INDEX idx_approval_receipts_expiry
    ON approval_receipts (instance_id, state, expires_at);
CREATE INDEX idx_approval_receipts_workflow
    ON approval_receipts (workflow_execution_id, gate_nonce)
    WHERE workflow_execution_id IS NOT NULL;
CREATE INDEX idx_approval_receipts_reconcile_pending_emit
    ON approval_receipts (decision_emitted_at)
    WHERE decision_emitted_at IS NULL AND state IN ('approved','rejected','expired');
```

Workflow-field consistency: when `workflow_execution_id` is set,
`gate_nonce` MUST also be set (and vice versa). Enforced by the
request helper, not by a SQL CHECK (cross-column check on
nullables is fiddly across sqlite versions).

State machine:

```
        request_approval()
              │
              ▼
           pending
           │  │  │
   approve │  │  │ reject
           │  │  │
           ▼  │  ▼
       approved │ rejected
           │   │
   consume │   │ (terminal)
           │   ▼
           │ expired (terminal; on now() > expires_at)
           ▼
       consumed (terminal; single-use=1)
```

Mutations (all use atomic CAS with full predicate per Codex
round 1 finding 4):

- `pending → approved` on `/approve`. CAS predicate:
  `state='pending' AND expires_at > now() AND instance_id=?`.
  If rowcount != 1, refuse with "no longer pending or expired."
- `pending → rejected` on `/reject`. Same CAS predicate as approve.
- `pending → expired` by background pass. CAS predicate:
  `state='pending' AND expires_at <= now()`.
- `approved → consumed` by consumer at execution time. CAS
  predicate: `state='approved' AND expires_at > now() AND
  single_use=1 AND instance_id=?` (Codex round 1 finding 3 —
  expiry guards consume; approved receipts DO have a TTL beyond
  the decision window; if the caller waits past `expires_at` to
  consume, the approval is no longer authoritative). If rowcount
  != 1, refuse with "not approved, expired, or already consumed."
- All terminal states (`consumed | rejected | expired`) are
  sticky — no further transitions. The "approved" → "expired"
  transition specifically lives only as a CONSUME-TIME refusal,
  NOT as a state mutation; the row stays in `approved` for audit
  (the operator did approve, the consumer just didn't act in
  time).

### D2 — Request side: agent + workflow-callable helper

```python
# kernos/kernel/approval_receipts.py

async def request_approval(
    *,
    db_path: Path,
    instance_id: str,
    kind: str,
    requested_for_actor: str,
    operator_actor_id: str,
    request_summary: str,
    binding_payload: dict,
    workflow_execution_id: str | None = None,
    gate_nonce: str | None = None,
    ttl_seconds: int = 86400,  # 24h default
    single_use: bool = True,
    event_stream=None,
) -> str:
    """Create a pending receipt; emit approval.requested event;
    return the approval_id. The caller is responsible for surfacing
    the request to the operator (via notify_user, a workflow
    notify_user step, etc.) using the returned approval_id.
    """
```

Returns the new `approval_id`. Side-effects: inserts the row,
emits an `approval.requested` event into the event stream with
`{approval_id, kind, request_summary, expires_at}` payload.

For workflow gates: a new action verb `request_approval` ships
that wraps this call + populates `workflow_execution_id`,
`gate_nonce` from the workflow context. The workflow step
emits the request, then immediately PAUSES at an approval gate
whose `approval_event_type = 'approval.decision_recorded'` and
predicate matches `(approval_id, decision='approved')` or
similar. The existing post-flush hook resumes the workflow.

### D3 — Decision side: slash commands (two-step CONFIRM)

The approve flow uses a two-step pattern (Codex round 1 finding 7
+ round 2 finding 5 — sticky terminal states require defending
against typos at the request side, not after the fact):

**`/approve <approval_id>`** (single-arg form, no `CONFIRM`):
owner-only. Looks up the receipt WITHOUT mutating state. Refuses
if not `pending`, if expired, or if wrong instance. Returns a
confirmation prompt to the operator:

> About to approve: `<request_summary>` (kind=<kind>, expires in
> <hh:mm>). Reply `/approve <approval_id> CONFIRM` to proceed.

**`/approve <approval_id> CONFIRM`** (two-arg form): owner-only.
Atomic CAS (`state='pending' AND expires_at > now() AND instance_id=?`)
mutates state to `approved`, sets `decided_at`. Emit + flush +
mark `decision_emitted_at` per D6. Returns a friendly
confirmation message + the summary.

**`/reject <approval_id> <reason>`** — single-step (rejection is
less typo-dangerous than approval; reject is the default safe
outcome). Owner-only. Atomic CAS, captures `reason` in
`state_reason`, emits decision event with `decision: "rejected"`.

The operator's identity check — v1 simplified model (Codex round
4: `InstanceDB` has no `actor_id` field or `get_member_by_actor_id`
method today; the round-2 split between actor_id and member_id
was based on a member model that doesn't exist yet):

**v1 contract: `KERNOS_OPERATOR_ACTOR_ID` IS a Kernos `member_id`.**
The two fields exist for forward-compatibility (a future spec can
introduce a separate actor identity if needed), but v1
populates both from the same source:

- At REQUEST time: `operator_actor_id = operator_member_id =
  os.environ["KERNOS_OPERATOR_ACTOR_ID"]`. Both columns same value.
- At SLASH-COMMAND time: the handler verifies
  `primary_ctx.member_id == receipt.operator_member_id`. Belt-
  and-suspenders: also check `instance_db.get_member(member_id)`
  returns a row with `role == 'owner'` (in case operator env was
  changed mid-attempt).
- Mismatch: refuse with "approval restricted to designated
  operator." No bypass.

The forward-compat split (separate columns even though v1
populates them identically) means: if a future spec adds a real
actor-identity layer to InstanceDB, the receipt schema doesn't
need migration — just populate the columns with their distinct
sources. v1 callers and v1 slash handlers can ignore the
distinction.

**Single canonical decision event type for ALL gate-resolution
outcomes** (Codex round 1 findings 1 + 2 — workflow gates match
ONE event type; the gate must receive a single matching event
regardless of approved/rejected/expired outcome, AND the event
payload MUST include `execution_id` + `gate_nonce` to satisfy the
existing `_on_post_flush_for_gates` binding check at
`execution_engine.py:1688`):

Event type: `approval.decision_recorded` for ALL decisions.
Payload:

```json
{
    "approval_id": "uuid-hex",
    "decision": "approved",     // or "rejected" or "expired"
    "execution_id": "uuid-hex",  // REQUIRED if workflow-gated; from receipt
    "gate_nonce": "uuid-hex",    // REQUIRED if workflow-gated; from receipt
    "kind": "autonomous_commit", // for predicate filtering
    "operator_actor_id": "owner-uuid",
    "decided_at": "2026-05-21T...",
    "reason": ""                 // empty for approved/expired; operator text for rejected
}
```

The workflow approval-gate descriptor's predicate references
`payload.approval_id` (which the request action returns as
`{step.request_approval.output.approval_id}` per Codex r1
answer 5) plus optionally `payload.decision` to branch on the
outcome.

A separate diagnostic event `approval.expired` continues to
emit on expiry for NON-workflow consumers (operator dashboards,
audit) — that side-channel does NOT wake gates and exists only
for observability.

### D4 — Consume side: re-verify binding before acting

Before the caller acts on an approved receipt, it MUST:

1. Re-load the receipt from the DB.
2. Verify state is `approved` (not `consumed` already, not
   `expired`).
3. Re-verify whatever binding semantics the caller defined in
   `binding_payload` (e.g., for autonomous commit:
   `expected_diff_hash` still matches; `expected_parent_sha` still
   matches `origin/main`).
4. If binding still holds: atomically update state to `consumed`,
   set `consumed_at`. The atomicity uses
   `UPDATE ... WHERE approval_id = ? AND instance_id = ? AND
   state = 'approved' AND expires_at > now() AND single_use = 1`
   and checks the row count was 1 (compare-and-set; full
   predicate per the Mutations table — defends against
   double-consume, expired-consume, wrong-instance-consume,
   non-single-use semantics).
5. If binding drifted: leave the receipt in `approved` state for
   audit, but refuse the action. Caller emits a domain-specific
   drift event.

A helper `consume_approval(db_path, approval_id, instance_id) -> bool`
does steps 1, 2, 4. Step 3 is caller-specific.

### D5 — Background expiry pass

A periodic task (default 60s) sweeps pending receipts where
`expires_at < now()` and transitions them to `expired`. Emits
TWO events per expired receipt (Codex round 2 finding 4):

1. `approval.decision_recorded` with `decision="expired"` —
   identical payload shape to approved/rejected (same
   execution_id + gate_nonce + approval_id + kind fields). This
   is the **workflow-gate-resumable** event; the existing
   `_on_post_flush_for_gates` matches it via the standard
   approval-event predicate.
2. `approval.expired` — the **diagnostic side-channel** event
   for non-workflow consumers (operator dashboards, audit).
   Workflow gates do NOT match this event type.

Both events undergo the same flush + decision_emitted_at marker
discipline as approved/rejected (D6).

The pass runs as part of substrate bring-up alongside other
background tasks. Its task is started after the receipts DB is
initialized.

### D6 — Restart fidelity + decision-event reconcile (Codex r1#4 durability)

On substrate boot:
- Receipts table is ensured (idempotent schema create).
- No in-memory state to rehydrate — receipts are queried on demand
  by the slash handlers and the consume helper.
- The existing workflow restart-resume pass picks up paused
  executions; the post-flush hook will see future
  `approval.decision_recorded` events and resume the right
  execution. The receipt rows are DB-durable.
- Background expiry pass restarts; sweeps over surviving rows
  picks up any whose expiry passed during downtime.

**Decision-event reconcile** addresses the durability gap Codex
round 1 finding 4 surfaced: `event_stream.emit()` is queued with
a ~2s flush window. A crash between row-update and flush would
leave a terminal-state receipt whose decision event never
reached gate consumers, stranding any paused workflow.

The fix uses the `decision_emitted_at` column **with explicit
flush** (Codex round 2 finding 1 — marking after enqueue is not
safe because emit() only queues; need to mark after the SQLite
durability write actually lands):

- `/approve`, `/reject`, the expiry pass, and any other state-
  transition path follow this pattern (using the real
  event-stream API per `event_stream.py:493,503` — `emit(instance_id,
  event_type, payload, ...)` + `flush_now()`):
  1. Atomic CAS to mutate state + set `decided_at`.
  2. Call `event_stream.emit(instance_id, "approval.decision_recorded", {...})`.
     For expiry: ALSO call `event_stream.emit(instance_id,
     "approval.expired", {...})` (both events queued before flush).
  3. **Await `event_stream.flush_now()`** to force the queued
     event(s) to durable storage before proceeding.
  4. UPDATE the row's `decision_emitted_at = now()` ONLY after
     the flush returns successfully. If the flush raises or the
     process crashes between steps 3 and 4, `decision_emitted_at`
     stays NULL and the boot reconcile pass re-emits.

`decision_emitted_at` tracks emission of the GATE-RESUMABLE event
(`approval.decision_recorded`). The diagnostic `approval.expired`
event is best-effort coupled to the same flush — if reconcile
re-emits the decision event, it also re-emits the expired event
for the same row. One `decision_emitted_at` column suffices
because both events are flushed together.
- **Boot reconcile pass**: scans `approval_receipts` for rows
  where `state IN ('approved','rejected','expired') AND
  decision_emitted_at IS NULL`. For each, applies the same
  emit+flush+mark discipline as the live mutation path:
  1. `event_stream.emit(instance_id, "approval.decision_recorded",
     payload)` reconstructing the payload from the row.
  2. For state=`expired`: ALSO emit `approval.expired` diagnostic.
  3. `event_stream.flush_now()`.
  4. UPDATE `decision_emitted_at = now()` only after flush returns.

Workflow gate consumers MUST be idempotent (Codex round 1
finding 4): the same decision event may be emitted twice if a
crash lands between emit + decision_emitted_at update. The
existing `_on_post_flush_for_gates` is naturally idempotent
because `asyncio.Event.set()` is idempotent and a re-wake of an
already-resumed execution is a no-op. The cached release payload
gets overwritten with identical content. So duplicate emits are
safe.

The reconcile also handles the "bot was down longer than TTL"
case: receipts that should have expired during downtime get the
same emit + flush + mark discipline as a live transition:
1. CAS `pending → expired` (where `expires_at < now`).
2. Emit `approval.decision_recorded` with `decision="expired"`
   AND `approval.expired` (both queued).
3. `flush_now()`.
4. Mark `decision_emitted_at = now()` only after flush returns.

This means downtime-discovered expiries flow through the same
durability path as live ones — no shortcut that would re-open
the marker-before-durability hole.

### D7 — Generic vs improvement-loop specifics

This sub-spec ships GENERIC receipts. The
`KERNOS-AUTONOMOUS-IMPROVEMENT-LOOP-V1`'s use case populates
`binding_payload_json` with improvement-specific fields:

```json
// binding_payload_json (immutable after request):
{
    "attempt_id": "...",
    "commit_sequence": 1,
    "expected_parent_sha": "...",
    "expected_diff_hash": "...",
    "diff_summary_for_operator": "..."
}

// outcome_payload_json (initially {}; consume_approval writes
// caller-defined outcomes here; for the improvement loop,
// git_commit writes the new commit_sha and git_push reads it):
{
    "commit_sha": "deadbeef..."
}
```

When the improvement loop's `git_commit` runs, it loads the receipt,
verifies `expected_diff_hash` against the current pre-commit diff
hash + `expected_parent_sha` against the current worktree parent,
then on success it writes the new `commit_sha` into
`outcome_payload_json` (NOT `binding_payload_json` — binding is
immutable; outcome is for caller write-backs). The improvement
loop's `git_push` later reads the receipt's
`outcome_payload_json.commit_sha` to verify the worktree's HEAD.
This per-use-case state lives in the JSON columns; the core
schema doesn't need to know about it.

For other use cases (hard_write live-dispatch gating, etc.),
binding_payload carries whatever that use case needs to verify.
The core substrate only needs to know about state transitions and
event emission.

## What does NOT change

- `DispatchGate.ApprovalToken` is untouched. It continues to serve
  the in-conversation confirmation flow it was built for.
- Workflow approval-gate mechanism (`_on_post_flush_for_gates`,
  `_gate_predicates`, `_gate_nonces`) is untouched. Receipts emit
  events that the existing mechanism consumes; no new gate
  infrastructure.
- Existing slash commands untouched.
- Event stream schema untouched (we emit two new event types but
  the storage is the same).

## Acceptance criteria

1. **Schema migration**: starting from a DB without
   `approval_receipts`, `ensure_schema` creates the table +
   indexes. Idempotent re-call against existing DB is a no-op.
2. **request_approval creates a pending row + emits event**:
   call returns an `approval_id`; the row exists in the DB with
   state=`pending`, correct binding_payload JSON, expires_at
   set; an `approval.requested` event is in the stream.
3. **Two-step /approve transitions state + emits decision event**:
   - First call `/approve <approval_id>` (no CONFIRM): receipt
     row UNCHANGED, response contains confirmation prompt with
     request_summary. No decision event emitted.
   - Second call `/approve <approval_id> CONFIRM` from owner:
     row state → `approved`, `decided_at` populated,
     `approval.decision_recorded` event in stream with
     decision=`approved` (after flush + decision_emitted_at marker).
   - Non-owner caller on either form: refused with "approval
     restricted to designated operator." No state change.
4. **/reject transitions state + captures reason**: same as
   AC3 but state=`rejected`, `state_reason` captured.
5. **Expiry pass on pending**: a receipt with `expires_at` in the
   past + state=`pending` gets moved to `expired` by one pass;
   `approval.decision_recorded` event with decision=`expired` is
   emitted (workflow-gate-resumable); a separate diagnostic
   `approval.expired` event also emits for non-workflow consumers.
   Multiple receipts in one pass: all transition.
6. **Expiry guards consume**: a receipt that is `approved` but
   whose `expires_at` is in the past must be REFUSED by
   `consume_approval` (returns False). Row stays in `approved`
   state for audit; no transition to `consumed`. Pins Codex round 1
   finding 3.
7. **Consume helper atomic CAS**: two concurrent `consume_approval`
   calls on the same `approved` receipt — exactly one returns True,
   one returns False. State ends as `consumed`. CAS predicate
   includes `instance_id`, `state='approved'`, `expires_at > now`,
   `single_use=1`.
8. **Restart fidelity**: insert a pending receipt; "restart"
   (close + reopen the DB); the receipt is still there in
   `pending` state; `/approve` still works.
9. **Decision-event reconcile on boot**: create an approved
   receipt with `decision_emitted_at=NULL` (simulating a crash
   between flush + marker update). Call the boot reconcile pass;
   assert exactly one `approval.decision_recorded` event is
   emitted and `decision_emitted_at` is now populated. A second
   reconcile pass does NOT re-emit.
9.5. **Crash-between-enqueue-and-flush is recoverable** (Codex r2#1):
    simulate the crash window by emitting the decision event but
    NOT calling flush; assert `decision_emitted_at` is still NULL
    (the spec requires flush-before-marker). Restart + reconcile;
    assert the event is re-emitted and the marker is now populated.
    Pins that the marker-after-flush ordering closes the gap.
10. **Workflow integration smoke**: emit a synthetic
    `approval.decision_recorded` event with `execution_id`,
    `gate_nonce`, `approval_id`, `decision="approved"` payload
    fields; verify the payload shape satisfies the existing
    `_on_post_flush_for_gates` binding check (execution_id +
    gate_nonce nonempty, predicate-evaluable). Full end-to-end
    workflow-resume test deferred to
    `IMPROVEMENT-LOOP-WORKFLOW-V1`.

## Deferred to follow-up (post-ship change of scope)

Codex code review caught two real blockers in the original
attempt to ship the workflow-callable `RequestApprovalAction`
verb that D2 names:

1. The engine's `CohortContext` doesn't carry `execution_id` +
   `gate_nonce` today, so a receipt created from a workflow step
   wouldn't bind to the gate — `/approve` would fire decision
   events that the gate's `_on_post_flush_for_gates` binding
   check (`execution_engine.py:1688`) would refuse.
2. The workflow ref-resolver maps `{step.X.output.K}` to
   `result.value`, not `result.receipt`, so the documented
   predicate path wouldn't resolve to the new `approval_id`.

Both fixes need deeper engine surface changes than fit this
sub-spec. The receipts substrate (`approval_receipts` module +
slash commands) is generic-callable from any non-workflow code
path (`/improve_kernos` invocations, future hard_write tools,
operator-triggered approvals on stand-alone capabilities). The
workflow-callable verb + its engine-surface dependencies land
in a follow-up sub-spec — `APPROVAL-RECEIPT-WORKFLOW-ACTION-V1`
or similar — that ships alongside the engine changes.

This deferral does NOT block the autonomous-improvement loop
arc as a whole — that arc's commit gate just calls the
receipts substrate directly rather than through a workflow
action verb. The follow-up sub-spec is only needed when another
workflow descriptor needs to request approval mid-execution.

## Out of scope

- Multi-operator approval (today's gate model: only the
  `KERNOS_OPERATOR_ACTOR_ID` owner can approve). Multi-quorum,
  delegation, M-of-N — future spec.
- Cross-instance approval (each instance owns its own receipts).
- Approval-receipt revocation by the requester (the requester can
  let it expire by not consuming; explicit revoke is a future
  spec if needed).
- A UI / canvas / Notion view of pending receipts. v1 surfaces are
  the slash commands + the `approval.requested` event the caller
  pairs with their own `notify_user` for operator visibility.
- A new agent-callable kernel tool to enumerate / introspect
  receipts. Future addition.

## Risk

- **`KERNOS_OPERATOR_ACTOR_ID` mismatch.** If the receipt is
  created with `operator_actor_id` set to the current owner, and
  later the owner identity changes (env var rebind), pending
  receipts orphaned to the old identity. Mitigation: capture the
  owner at request time; if owner changes, pending receipts go
  to expired (operator-visible warning in `notify_user` text:
  "expires unless owner approves").
- **Race between approve + expire.** Operator approves at
  T = expires_at - 1ms; background pass runs at T+. Risk: pass
  marks expired despite the approval landing first. Mitigation:
  approve uses CAS on state=`pending`; expire uses CAS on
  state=`pending` AND expires_at <= now. If approve lands first,
  the row's state is `approved`, so expire's CAS fails and the
  approval stands.
- **Binding_payload schema drift.** v1 stores arbitrary JSON; no
  schema validation. A use-case-side schema change could break
  the consume re-verify. Mitigation: each consumer owns its
  binding_payload shape; if it evolves, the consumer's
  re-verification step rejects mismatched rows gracefully.
- **Operator approves the wrong attempt by typo.** If the operator
  types `/approve <wrong-id>`, they could approve a different
  pending receipt. Terminal states are sticky (Codex round 1
  finding 7) so a follow-up `/reject` cannot undo the
  approval. Mitigation: a confirmation-before-action pattern at
  REQUEST time, not after. When the operator types `/approve <id>`,
  the handler:
  1. Looks up the receipt by approval_id WITHOUT mutating state.
  2. Returns a confirmation prompt: "About to approve: 'commit
     autonomous improvement [attempt_12abc]: add a comment to
     README.md' (expires in 23h). Reply `/approve <id> CONFIRM`
     to proceed."
  3. The mutating CAS only fires on the second call with the
     `CONFIRM` token. Reduces typo blast-radius to a two-step
     mistake.
  This adds one round-trip but cleanly defends against typos
  without breaking the sticky-terminal-state invariant.
- **Background expiry skew across restart.** If the bot is down
  longer than the receipt's TTL, receipts that should have expired
  hours ago don't get the expiry event emitted until the first
  post-boot pass. Mitigation: emit a single batched
  `approval.expired` event per receipt on the first post-boot
  pass; downstream consumers (workflow gates) wake correctly even
  if the expire event is "late."

## Roll-out

Single batch. Manual verification post-merge:

1. After restart, INSERT a pending receipt via Python REPL:
   ```python
   from kernos.kernel.approval_receipts import request_approval
   approval_id = await request_approval(
       db_path=Path("data/instance.db"),
       instance_id="default",
       kind="test",
       requested_for_actor="test_actor",
       operator_actor_id="<owner_id>",
       request_summary="test request",
       binding_payload={"foo": "bar"},
       ttl_seconds=300,
   )
   ```
2. Send `/approve <approval_id>` from Discord/Telegram as the
   owner. Expect the confirmation-prompt response (row UNCHANGED).
3. Send `/approve <approval_id> CONFIRM`. Expect the friendly
   "approved" confirmation; row should be `approved` in the DB.
4. Send `/approve <fake_id>` — expect "not found" error.
5. Send `/approve <approval_id> CONFIRM` again (already decided)
   — expect "no longer pending" error.
5. Insert another receipt with a 5s TTL; wait ~70s; check the row
   is `expired`.

The slash commands' confirmation messages + the
`approval.decision_recorded` event stream entries are the
operator-visible proof the substrate works.
