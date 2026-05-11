# ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1 — Implementation Spec

**Status:** DRAFT v2 — pre-implementation, ONE Codex pre-spec review
round folded 2026-05-11. Seven findings addressed:

- **Blocker** — resume idempotency hides re-execution because the
  record append and the cursor advance weren't atomic. Refactored
  Decisions 3 + 6 to land them in one SQLite transaction; event
  emit moves after the commit. Concurrent-restart promoted from
  defensive flag to load-bearing state-machine concern.
- **High** — `authorization_state="confirmed"` doesn't compose with
  workflow gate semantics (action-first, pause-after). Folded to
  `authorization_state="not_required"` for v1; gate state continues
  to ride on `workflow.execution_paused_at_gate` /
  `workflow.execution_resumed` events.
- **High** — friction composition must mirror
  `FrictionObserver._classify_and_record` exactly (active/reactivated
  → `record_occurrence`; resolved → `record_recurrence`; archived →
  unclassified). Plus classify-only-when-the-append-actually-inserted
  so INSERT-OR-IGNORE skips don't double-count.
- **Medium** — Decision 1 preserved but rationale tightened to the
  real invariant: workflow-facing APIs MUST return
  `WorkflowActionRecord` (not bare `ActionStateRecord`) OR include a
  stable `receipt_refs` entry of the form
  `workflow:<execution_id>:step:<idx>`.
- **Medium** — connection ownership contradiction (sink-owns vs
  shared engine connection) resolved to shared engine connection;
  required for the atomicity fix.
- **Medium** — `risk_level` derived from action_type for v1: `low`
  for direct internal verbs (`mark_state`, `append_to_ledger`),
  `medium` for irreversible/world-effect verbs, `high` for
  external/public-service posts. Unknown `call_tool` risk surfaces
  `missing_metadata=True`.
- **Low** — failure summary computed mechanically from the same
  `error` string passed to `_record_step_failed`, eliminating drift.

Architect's four listed open questions all answered per Codex's
direction in this fold:

- Decision 1: preserve schema with the tighter invariant (don't fold
  to `workflow_context`).
- FK target: composite UNIQUE on `(instance_id, execution_id)`,
  backward-compatible.
- `operation_class`: map both `mark_state` and `append_to_ledger` to
  `mutate`; don't extend the enum.
- Concurrent restart: closed via the atomicity fix in Decision 6.

Awaiting architect ratification of v2 before CC implementation begins.

**Author:** CC, 2026-05-11. Resolves architect's framing + folds
Codex's round-1 review.

**Source framing:** PHASE-3-AUTONOMY-LOOP design consideration (Notion
`35cffafef4db81da8107e562307bc738`). Spec 3 of the five-spec autonomy
loop arc, queued by architect to drop in *after* FRICTION-PATTERN-STABLE-IDS-V1
landed (Spec 1; merged to `main` at `452dbee`) and CODING-SESSION-BRIDGE-V1
landed (Spec 2; merged at `a16c1d9`).

**Architect's lean on Option A** is locked: engine-side wrap each
workflow action's `execute` call to emit BOTH the existing `workflow.*`
event_stream events AND a new ActionStateRecord per step. Options B
(translate events at audit-render time) and C (synthesize records
in parallel with events) rejected per design-consideration rationale.

**Substrate review that gates this spec:** CC's earlier substrate
review of the workflow primitive at Notion
`35cffafef4db81f4a344e05ca9a2c9a8` (specifically Finding 2 on Option
A vs B vs C). Workflow execution wrap point is well-bounded
(~50 LOC change in `execution_engine.py`); no new primitives needed.

**Composes with:**

- RESPONSE-FIDELITY-V1 — the substrate-fidelity discipline this spec
  extends from turn-scoped actions to workflow-scoped actions.
- FRICTION-PATTERN-STABLE-IDS-V1 (Spec 1; merged) — `record_occurrence`
  / `record_recurrence` calls have a clean attach point at the
  workflow-step boundary once this spec lands.
- Existing workflow primitive (`kernos/kernel/workflows/`) — the
  spec modifies the step-execute path in `execution_engine.py`.
- Existing ActionStateRecord schema at
  `kernos/kernel/integration/briefing.py:1121` — the spec REUSES the
  schema unchanged (see Decision 1).

## What this spec ships

Six deliverables:

1. **NEW `workflow_action_records` table in `instance.db`** —
   per-execution storage of ActionStateRecord payloads with
   `(workflow_execution_id, step_index)` as the natural filter.
   Schema-in-store via `WorkflowActionSink.ensure_schema()` (mirrors
   FRICTION-PATTERN-STABLE-IDS-V1's convention; no separate
   migrations dir).
2. **NEW `WorkflowActionSink` in
   `kernos/kernel/workflows/action_sink.py`** — per-execution sink
   that persists ActionStateRecord rows alongside the existing
   `workflow_executions` table. Owns its own `aiosqlite` connection
   per the per-module-isolation pattern.
3. **Engine-side wrap in `execution_engine.py`** — after each step's
   `verb.execute` (the success path) and at each `_record_step_failed`
   site (the two failure paths — execute-raised at line 608 and
   verifier-rejected at line 632), construct an ActionStateRecord
   reflecting the step's outcome and append via the sink.
4. **Failure-path discipline** — failed steps produce
   `execution_state="failed"` ActionStateRecords with a populated
   `failure_reason` (carried in `user_visible_summary` since
   ActionStateRecord doesn't have a dedicated `failure_reason` field;
   see Decision 5).
5. **Resume-safe idempotency** — the sink's write path is
   `INSERT OR IGNORE` on `(workflow_execution_id, step_index)` so that
   restart-resume of an in-flight workflow doesn't re-emit records
   for already-completed steps. Combined with the workflow's existing
   `action_index_completed` cursor, the receipt of a completed step
   doesn't double on resume.
6. **Embedded live tests** — three substrate-fidelity assertion
   probes (successful step receipt, failed step receipt, resume
   across restart) plus FRICTION-PATTERN composition probe.

## What this spec does NOT ship

Per architect's explicit framing + my four-decision resolutions:

- **NO new fields on the ActionStateRecord dataclass.** Architect's
  framing says "preserve existing schema" in the NOT-ships list AND
  leaned toward "ActionStateRecord with optional `workflow_context`
  field" in the open-questions list. The two conflict. I resolve in
  favor of "preserve existing schema" — see Decision 1.
- **NO migration of existing `workflow.*` event emissions.** Both
  fire alongside per architect's lean on open question 3.
- **NO render-side changes for surfacing workflow ActionStateRecords
  to the agent.** When the agent should see them is open architect
  question 4; deferred to a follow-up. v1 ships persistence; agent
  consumption lands when the self-improvement workflow definition
  (Spec 4) needs it.
- **NO self-improvement workflow definition itself.** That's Spec 4;
  this spec ships the substrate it consumes.

## Architectural decisions

### Decision 1 — Where workflow context lives (DEVIATES from architect's lean)

**Architect's lean:** single ActionStateRecord shape with an optional
`workflow_context` field populated when the action originated from a
workflow step. Preserves single-shape discipline.

**My resolution:** ActionStateRecord stays **completely unchanged**.
Workflow context (`workflow_execution_id`, `step_index`, `action_type`)
rides in the storage layer's row schema, not on the record itself.

**Why deviate (revised v2 rationale per Codex round 1):** the v1
spec body claimed adding an optional field would require updates to
every existing producer + renderer. Codex correctly flagged that as
overstated — existing constructors with a defaulted optional field
don't all need updates, and the renderer mostly uses tolerant
`getattr` patterns. So that's not the real load-bearing reason.

The actual reason to preserve the dataclass schema and push workflow
context to the storage row is **a tighter invariant on the
workflow-facing API surface:**

> Workflow-facing APIs MUST return `WorkflowActionRecord` (the
> storage-row dataclass that bundles the unmodified
> `ActionStateRecord` with workflow context), NOT a bare
> `ActionStateRecord`. The bare ActionStateRecord type is reserved
> for callers who can't or won't distinguish between turn-scoped and
> workflow-scoped records. Workflow consumers always get the wrapper
> so the workflow context is statically reachable.
>
> The single exception: a bare `ActionStateRecord` MAY surface from
> a workflow path IF its `receipt_refs` includes a stable entry of
> the form `workflow:<execution_id>:step:<idx>` — that string is the
> escape hatch for callers who already only need to look at
> `receipt_refs` for provenance.

This invariant matters because it preserves the existing
dataclass-shape contract for non-workflow producers (cheaper, no
substrate-wide audit) while making workflow-scoped consumption
type-distinct at the API boundary.

Architect's lean on single-shape-with-optional-fields was a
reasonable v1 intuition. Codex's surface (preserve the dataclass; gate
workflow surface behind the wrapper) is the precise version of that
intuition; folded.

### Decision 2 — Storage shape

**Resolution:** new table `workflow_action_records` in `instance.db`,
mirroring the FRICTION-PATTERN-STABLE-IDS-V1 catalog convention:

```sql
CREATE TABLE IF NOT EXISTS workflow_action_records (
    instance_id             TEXT NOT NULL,
    workflow_execution_id   TEXT NOT NULL,
    step_index              INTEGER NOT NULL,
    action_id               TEXT NOT NULL,
    workflow_id             TEXT NOT NULL DEFAULT '',
    action_type             TEXT NOT NULL,
    record_json             TEXT NOT NULL,   -- serialized ActionStateRecord
    correlation_id          TEXT NOT NULL DEFAULT '',
    recorded_at             TEXT NOT NULL,
    PRIMARY KEY (instance_id, workflow_execution_id, step_index),
    FOREIGN KEY (instance_id, workflow_execution_id)
        REFERENCES workflow_executions(instance_id, execution_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_workflow_action_records_action_id
    ON workflow_action_records (instance_id, action_id);
CREATE INDEX IF NOT EXISTS idx_workflow_action_records_workflow
    ON workflow_action_records (instance_id, workflow_id);
```

Composite PK `(instance_id, workflow_execution_id, step_index)` carries
the resume-safe idempotency contract directly: `INSERT OR IGNORE` on a
duplicate primary key is the cheapest way to skip re-emission on
restart-resume.

`ON DELETE RESTRICT` mirrors FRICTION-PATTERN's no-destructive-deletions
discipline. Workflow executions transition through state machine
(running → completed / aborted / paused / resumed); they don't get
DELETE'd. If a future GC spec wants to clean up old terminated
executions, it ships its own pre-removal pass over
`workflow_action_records` first.

**`workflow_executions` does NOT currently declare a composite PK on
`(instance_id, execution_id)`** — the existing schema at
`execution_engine.py:212` has `execution_id` as the singular PK. To
satisfy the composite FK here, the implementer either:

(a) **Add a composite UNIQUE index** to `workflow_executions` —
`(instance_id, execution_id)` — which SQLite accepts as an FK target.
Backward-compatible.

(b) **Drop the FK** and rely on tool-implementation discipline (the
engine only writes rows for executions it just created).

Recommend (a). Mirrors FRICTION-PATTERN's FK discipline at the
substrate level rather than at the tool-implementation level. Codex
will likely have an opinion on this; flagged in the open-questions
section.

**PRAGMA foreign_keys=ON** is mandatory on every connection (same
discipline as FRICTION-PATTERN-STABLE-IDS-V1; Codex caught this in
round 2 there). The sink runs the pragma in `ensure_schema()` and
on every reconnect.

### Decision 3 — `WorkflowActionSink` lifecycle + ownership (v2 fold)

**Resolution (revised per Codex round 1):** the sink uses the
**engine's shared `aiosqlite` connection** to the workflow DB — NOT
its own. v1 spec body had a contradiction (deliverables said
sink-owned; this Decision said shared); v2 resolves to shared.

The shared connection is **load-bearing for Decision 6's atomicity
fix:** the single-transaction boundary that wraps "append record +
advance `action_index_completed`" can only span both tables when
they share a connection. A sink-owned connection would force a
two-phase commit pattern that v1 doesn't earn.

Per-execution `WorkflowExecutionActionSink` wrappers (thin) carry
the bound `(instance_id, workflow_execution_id, workflow_id,
correlation_id)` context. They delegate writes to the underlying
shared store using a `BEGIN IMMEDIATE` transaction that includes
both the `INSERT OR IGNORE` into `workflow_action_records` and the
`UPDATE workflow_executions SET action_index_completed = ?`.

Schema setup order on engine start:

1. Ensure `workflow_executions` table.
2. ALTER-IF-MISSING the existing migration columns (`gate_nonce`,
   `fire_id`) per the gate_nonce migration pattern.
3. CREATE UNIQUE INDEX `(instance_id, execution_id)` for the
   composite FK target.
4. CREATE `workflow_action_records` (FK target now exists).
5. CREATE indexes on `workflow_action_records`.

The engine owns the connection lifecycle. The sink is constructed
once at engine start and passed a borrowing reference; it does not
open or close the connection itself.

The sink exposes:

```python
class WorkflowActionSink:
    """Per-execution ActionStateRecord persistence for workflow steps."""

    async def append(
        self,
        record: ActionStateRecord,
        *,
        step_index: int,
        workflow_id: str,
        action_type: str,
        correlation_id: str,
    ) -> bool:
        """Append a record. Returns True if persisted; False if
        idempotency-skipped (already exists for this
        (workflow_execution_id, step_index))."""

    async def list_for_execution(
        self,
        instance_id: str,
        workflow_execution_id: str,
    ) -> list[ActionStateRecord]:
        """All records persisted for this execution. Ordered by step_index."""

    async def get_by_action_id(
        self,
        instance_id: str,
        action_id: str,
    ) -> ActionStateRecord | None: ...
```

The engine constructs a sink at execution start via
`engine.action_sink_for(execution)` and discards the wrapper at
execution end. The shared underlying connection persists for the
engine's lifetime.

### Decision 4 — `workflow.*` events stay alongside ActionStateRecords

**Resolution:** both fire on every step. Architect's lean confirmed.

The two carry different audit purposes:

- `workflow.*` event_stream events — workflow-lifecycle audit
  (started, paused, resumed, terminated, step_succeeded, step_failed).
  Ordered timeline; queryable via existing event_stream API.
  `correlation_id` chains to other workflow events.
- ActionStateRecord per step — substrate-affecting-action receipt.
  Has `operation_class` (read/mutate/etc.), `risk_level`,
  `evidence_class`, `affected_objects`, `partial_state` — the full
  substrate-fidelity vocabulary that RESPONSE-FIDELITY-V1 made
  load-bearing.

Both fire from the same step-execute wrap point. `correlation_id` on
both carries the workflow execution's `correlation_id` so audit
queries can join the two streams.

### Decision 5 — Failure-path receipt shape + authorization_state + risk_level (v2 fold)

**Resolution (revised v2 per Codex round 1, three changes):**

#### Failure summary computed mechanically (Codex Low 7)

The exception/rejection error string is computed **once** and passed
to both the ActionStateRecord builder AND the existing
`_record_step_failed` call. Eliminates drift between the
`workflow.execution_step_failed` event's `error` field and the
ActionStateRecord's `user_visible_summary`:

```python
# Pseudocode for the failure path:
if exec_raised:
    error = f"execute_raised:{type(exc).__name__}:{exc}"
elif not action_succeeded:
    error = result.error or "verifier_rejected"
elif verify_raised:
    error = f"verify_raised:{type(verify_exc).__name__}:{verify_exc}"

record = _build_action_state_record(
    ...,
    execution_state="failed",
    user_visible_summary=error,  # same string
)
await sink.append(record, step_index=idx, action_type=action.action_type)
await self._record_step_failed(execution, idx, action, error=error)
```

Failed steps produce ActionStateRecords with `execution_state="failed"`
and `user_visible_summary` carrying the error string in the format
the existing event already uses.

ActionStateRecord doesn't have a dedicated `failure_reason` field;
the existing convention across producers (note_this,
coding_session_bridge) is to put failure reasons in
`user_visible_summary`. v2 follows that convention rather than add a
new field (Decision 1 logic).

`partial_state` is None for v1 workflow steps.

#### `authorization_state="not_required"` for v1 (Codex High 2)

**ALL workflow step ActionStateRecords use `authorization_state="not_required"` in v1.**
The earlier draft had gated steps carry `"confirmed"`, but the
workflow primitive is **action-first / pause-after**: the action
executes BEFORE the gate waits. The record is appended at the
success-emission site, which fires before any gate release. The
record cannot know whether or when a downstream gate will release.

`workflow.execution_paused_at_gate` and `workflow.execution_resumed`
events continue to carry gate state on the event_stream side, which
preserves the existing audit chain for approval workflows. A future
spec can extend the record with explicit post-gate authorization
context for downstream steps if soak shows the need; v1 doesn't
attempt it.

#### `risk_level` derived from action_type (Codex Medium 6)

The earlier draft hard-coded `risk_level="low"` uniformly. Codex
correctly flagged that as substrate-truth loss: a `route_to_agent`
posting to a public inbox is genuinely higher-risk than a
`mark_state`. v2 derives the field from action_type per the
following mapping (using the existing `ACTION_RISK_LEVELS` vocabulary
of `low / medium / high` — no enum extension):

| Action verb | `risk_level` | Reasoning |
|---|---|---|
| `mark_state` | `low` | Direct-effect; internal state mutation only |
| `append_to_ledger` | `low` | Direct-effect; append-only audit row |
| `notify_user` | `medium` | World-effect; reaches the user but reversible (correction can follow) |
| `write_canvas` | `medium` | World-effect; reversible (canvas tombstone discipline) |
| `route_to_agent` | `high` | World-effect; reaches another agent's inbox; not always reversible |
| `post_to_service` | `high` | World-effect; reaches an external service; commonly not reversible |
| `call_tool` | derived; default `medium` | Wraps any kernel tool; risk per the tool's own classification when available, `medium` fallback. If the tool's risk can't be derived at append time, set `missing_metadata=True` to surface the gap. |

Helper `_risk_level_for_action_type(action_type, tool_input)`
encapsulates the mapping. The helper lives in `action_sink.py`
alongside `_build_action_state_record` so engine code stays compact.

### Decision 6 — Resume-safe semantics (v2 fold — atomic boundary)

**Resolution (revised v2 per Codex round 1 Blocker):** the v1 draft
had a crash-window state-machine hole. Original ordering was:

1. Append record to `workflow_action_records` (INSERT OR IGNORE).
2. Emit `workflow.execution_step_succeeded` event.
3. Later: `_mark_step_complete` updates
   `workflow_executions.action_index_completed`.

A crash between step 1 and step 3 leaves a record for a step the
engine considers incomplete. On restart, the engine re-enters at
`start_idx = max(0, action_index_completed + 1)`, which still points
at the same step. The step re-executes; the sink's INSERT OR IGNORE
silently swallows the second append; the FIRST record stays
authoritative even though the SECOND execution may have a different
outcome (different timestamp, different `affected_objects`, etc.).
That's a real fidelity bug for any caller that trusts the record.

**v2 fix: atomic boundary.** Append record AND advance the cursor
in **one SQLite transaction**:

```python
# Pseudocode, executed inside _run_step success path:
async with engine.workflow_db.transaction("BEGIN IMMEDIATE"):
    await sink.append_within_txn(
        record,
        step_index=idx,
        action_type=action.action_type,
    )
    await engine._advance_cursor_within_txn(execution, idx)
# After commit: emit workflow.* event (best-effort; failure logged
# but does not unwind the persisted record + cursor advance).
await self._record_step_succeeded(execution, idx, action, result)
```

The transaction either commits both (record persisted; cursor
advanced; on next restart the engine moves past this step) or rolls
back both (no record; cursor not advanced; on next restart the
engine re-attempts this step from scratch). The crash window between
"step appears done" and "engine considers step done" is closed.

`workflow.execution_step_succeeded` and the ledger append remain
**after the transaction commits** because:

- They're audit emissions, not state mutations the cursor depends on.
- An event_stream emit failure shouldn't unwind the substrate record
  (substrate fidelity stays loud-fail-rather-than-silently-revert).

For the failure path (execute-raised, verifier-rejected,
verify-raised), the same single-transaction pattern is used:

```python
async with engine.workflow_db.transaction("BEGIN IMMEDIATE"):
    await sink.append_within_txn(record, ...)  # execution_state="failed"
    # On hard-abort: do NOT advance cursor (engine will route to _abort).
    # On continue-on-failure: advance cursor so the next step runs.
    if action.continuation_rules.on_failure != "abort":
        await engine._advance_cursor_within_txn(execution, idx)
```

#### Crash-window state detection (Codex Blocker 1 follow-on)

Even with the atomic transaction, a third edge case exists: an
engine startup sequence that observes "record exists for step N but
`action_index_completed < N`" indicates a crash-window state from a
pre-v2 deployment (before the atomic boundary shipped) OR a bug.
v2's engine startup includes a one-time scan:

```python
# Engine startup, after _ensure_schema completes:
async for execution in engine.list_running_executions():
    last_record_step = await sink.get_max_step_index(
        execution.instance_id, execution.execution_id,
    )
    if last_record_step > execution.action_index_completed:
        # Crash-window state. v2 atomic boundary should prevent this
        # going forward; log loud + reconcile.
        logger.warning(
            "WORKFLOW_CRASH_WINDOW: execution_id=%s record_step=%d cursor=%d",
            execution.execution_id,
            last_record_step,
            execution.action_index_completed,
        )
        # Reconcile: advance cursor to match the highest recorded step.
        # The record is authoritative; the cursor lag is the bug.
        await engine._advance_cursor_to(execution, last_record_step)
```

This is a self-healing startup pass. The warning surfaces the case
to the operator; the reconcile prevents the engine from re-executing
steps that already have authoritative records.

#### Idempotency-skip semantics

`INSERT OR IGNORE` is still used inside the transaction. Within the
transaction, a PK collision means "another caller (probably this
engine on a different code path) already wrote this step's record" —
the sink returns `False` and the transaction proceeds to the cursor
advance. If the cursor is already past this step, the
`UPDATE ... SET action_index_completed = ?` is also effectively a
no-op (the WHERE clause restricts to executions whose
`action_index_completed < step_index`).

This means: legitimate idempotency (same engine retrying the same
step within the resume-safe contract) is silent; the crash-window
state-detection above catches the only case where the silence would
hide a real bug.

#### Concurrent-restart edge case (Codex open question)

Codex correctly elevated concurrent-restart from "defensive" to "the
main state-machine hole to close." The v1 spec body framed it as a
defensive flag; v2 closes it via the atomic boundary above.

The remaining concern is two engine instances both observing an
in-flight execution at the same time. The existing workflow primitive
uses `last_heartbeat` for engine-liveness detection; v2 reuses that
discipline: an execution's heartbeat is updated on every cursor
advance, so a stale heartbeat lets a second engine reclaim the
execution by transitioning it through the existing recovery state
machine. The atomic boundary in this Decision composes with that
recovery — the second engine reads the existing record + cursor and
resumes from there.

### Decision 7 — FRICTION-PATTERN composition (v2 fold)

**Resolution (revised v2 per Codex round 1 High 3):** the v1 spec
body's context doc claimed the workflow-side friction hook calls
`record_occurrence` on a classifier match. That's wrong — the
merged `FrictionObserver._classify_and_record` at
`kernos/kernel/friction.py:526` dispatches by lifecycle:

| Pattern `lifecycle_state` | Method | Effect |
|---|---|---|
| `active` or `reactivated` | `record_occurrence` | Increments counter; tracks |
| `resolved` | `record_recurrence` | Emits recurrence event; may reactivate per threshold |
| `archived` | `_emit_pattern_unclassified` | Preserves audit trail; no catalog write |

Calling only `record_occurrence` would (a) fail on resolved patterns
because `record_occurrence` rejects on that state, and (b) skip the
reactivation loop entirely.

**v2 fix:** workflow-side friction composition mirrors
`FrictionObserver._classify_and_record` exactly. The shape:

```python
# Pseudocode, inside WorkflowActionSink.append() AFTER the
# transaction commits AND the append actually inserted (returned
# True, not False from INSERT OR IGNORE):
if record.execution_state == "failed" and self._pattern_store is not None:
    # Synthesize a FrictionSignal-shaped object from the workflow
    # step's failure shape so classify_signal can match against the
    # catalog the same way turn-scoped friction reports do.
    from kernos.kernel.friction_patterns import (
        LIFECYCLE_ACTIVE,
        LIFECYCLE_REACTIVATED,
        LIFECYCLE_RESOLVED,
        classified_by_for_match_path,
        classify_signal,
    )

    candidates = await self._pattern_store.list_patterns(instance_id)
    result = classify_signal(
        signal_type=f"workflow_step:{action_type}:failed",
        signal_description=record.user_visible_summary,
        candidates=candidates,
    )
    if result is None:
        # Optional: emit friction.pattern_unclassified on the workflow
        # side too. v1 doesn't; FrictionObserver does. v2 punts:
        # surface as a future hook if soak shows the gap matters.
        return

    pattern, score, match_path = result
    classified_by = classified_by_for_match_path(match_path)
    observed_at = utc_now()

    if pattern.lifecycle_state in (LIFECYCLE_ACTIVE, LIFECYCLE_REACTIVATED):
        await self._pattern_store.record_occurrence(
            instance_id=instance_id,
            pattern_id=pattern.pattern_id,
            observed_at=observed_at,
            report_path="",  # workflow steps don't have markdown reports
            classifier_score=score,
            classified_by=classified_by,
            space_id="",  # workflow context, not space-scoped
            member_id=member_id,
        )
    elif pattern.lifecycle_state == LIFECYCLE_RESOLVED:
        await self._pattern_store.record_recurrence(
            instance_id=instance_id,
            pattern_id=pattern.pattern_id,
            observed_at=observed_at,
            report_path="",
            classifier_score=score,
            classified_by=classified_by,
            space_id="",
            member_id=member_id,
            emit_event=self._emit_event,  # for friction.pattern_recurrence
        )
    # Archived patterns: skip silently. Mirrors FrictionObserver's
    # archived → unclassified path; v1 doesn't add a new
    # workflow-side event for it.
```

**Classify-only-when-the-append-actually-inserted** (Codex High 3
follow-on): the classifier call lives INSIDE the
`if append_returned_true:` branch, NOT before. INSERT OR IGNORE
skips (legitimate idempotency from the resume-safe contract) MUST
NOT count toward friction frequency. The append's return value is
the signal: True = newly inserted, classify + record; False = skip
silently.

**`report_path=""` for workflow-step occurrences:** workflow steps
don't write markdown friction reports (those are the post-turn
FrictionObserver's surface). The catalog's UNIQUE partial index on
`(instance_id, report_path) WHERE report_path != ''` is satisfied
because empty paths bypass the constraint. Workflow-step occurrences
correlate to the source step via `action_id` (a future
`report_path` schema extension could carry
`workflow:<execution_id>:step:<idx>` if soak shows the need for
human-readable traceback).

## Open architectural questions — v2 status

The v1 spec body surfaced three open architectural questions for
Codex round 1. All resolved 2026-05-11:

1. ✅ **Decision 1 deviation.** Codex agreed with preserve-schema
   for v1 but tightened the rationale: the load-bearing invariant
   is that workflow-facing APIs return `WorkflowActionRecord` (not
   bare `ActionStateRecord`), OR the bare record carries a stable
   `receipt_refs` entry of the form
   `workflow:<execution_id>:step:<idx>`. v2 Decision 1 reflects
   this. Don't fold to `workflow_context` for v1.
2. ✅ **FK target.** Codex chose composite UNIQUE on
   `(instance_id, execution_id)`. Backward-compatible (existing
   `execution_id` PK already unique). v2 Decision 2 keeps that
   direction; the migration block in Decision 3 schema-setup-order
   shows where the CREATE UNIQUE INDEX lands.
3. ✅ **`partial_state` for workflow steps.** Stays None for v1;
   future spec can extend if soak surfaces partial-completion
   semantics.

Codex round 1 also surfaced four new findings already folded above:

4. ✅ **`operation_class` mapping.** Codex confirmed `mutate` for
   both `mark_state` and `append_to_ledger` is fine for v1; don't
   extend the enum.
5. ✅ **`risk_level` derivation.** v2 Decision 5 derives from
   action_type; uses existing `low / medium / high` vocabulary;
   `missing_metadata=True` flag when `call_tool` risk can't be
   derived.
6. ✅ **Concurrent restart edge case.** Promoted from "defensive"
   to load-bearing; closed via the atomic boundary in v2 Decision 6.
7. ✅ **Failure summary mechanical copy.** v2 Decision 5 computes
   the error string once and passes to both the record builder and
   `_record_step_failed`.

## Code-level shape

### File map (v2)

- NEW: `kernos/kernel/workflows/action_sink.py` (~220 LOC after v2
  folds): `WorkflowActionSink` class (borrowing-reference connection;
  schema-in-store via `ensure_schema()`); `WorkflowExecutionActionSink`
  per-execution wrapper; `WorkflowActionRecord` dataclass for the
  storage-row shape; `_build_action_state_record` +
  `_risk_level_for_action_type` helpers; classifier-hook integration
  with `FrictionPatternStore` (Decision 7) mirroring
  `FrictionObserver._classify_and_record`.
- MODIFIED: `kernos/kernel/workflows/execution_engine.py`:
  - `_EXECUTIONS_SCHEMA` migration block gains a
    `CREATE UNIQUE INDEX IF NOT EXISTS` on
    `(instance_id, execution_id)` for the composite FK target.
    Mirrors the existing `gate_nonce` migration's
    race-tolerant-ALTER pattern.
  - `_ensure_schema` runs the new sink's schema migration AFTER the
    composite UNIQUE INDEX exists (schema setup order per
    Decision 3).
  - `WorkflowEngine.__init__` constructs the sink with the shared
    connection; sink is a borrowing reference.
  - The step-execute loop body wraps each emission site in a
    `BEGIN IMMEDIATE` transaction that includes the record append
    AND the cursor advance (Decision 6 atomicity fix). `workflow.*`
    event_stream emit moves AFTER the transaction commits.
  - Engine startup adds the crash-window-state self-healing pass
    (Decision 6 second block) so pre-v2 deployments + any bug
    that leaks crash-window state reconciles loudly on next start.
- MODIFIED: `kernos/kernel/workflows/__init__.py` — exports
  `WorkflowActionSink`, `WorkflowExecutionActionSink`,
  `WorkflowActionRecord`.

### `WorkflowActionSink` API

```python
@dataclass(frozen=True)
class WorkflowActionRecord:
    """Storage-side row carrying an ActionStateRecord plus workflow
    context. The record itself is unchanged; this dataclass is the
    on-disk row shape."""
    instance_id: str
    workflow_execution_id: str
    step_index: int
    action_id: str
    workflow_id: str
    action_type: str
    record: ActionStateRecord  # the actual record
    correlation_id: str
    recorded_at: str


class WorkflowActionSink:
    """Per-execution ActionStateRecord persistence backed by
    instance.db."""

    async def ensure_schema(self, data_dir: str) -> None: ...

    def for_execution(
        self,
        *,
        instance_id: str,
        workflow_execution_id: str,
        workflow_id: str,
        correlation_id: str,
    ) -> "WorkflowExecutionActionSink": ...

    async def list_for_execution(
        self,
        instance_id: str,
        workflow_execution_id: str,
    ) -> list[WorkflowActionRecord]: ...

    async def get_by_action_id(
        self,
        instance_id: str,
        action_id: str,
    ) -> WorkflowActionRecord | None: ...


class WorkflowExecutionActionSink:
    """Per-execution wrapper. Each step's append() goes through here
    with the execution context bound."""

    async def append(
        self,
        record: ActionStateRecord,
        *,
        step_index: int,
        action_type: str,
    ) -> bool: ...
```

### Step-execute wrap shape (illustrative pseudocode)

Wrap the existing success path at `execution_engine.py:647`:

```python
# Existing:
await self._record_step_succeeded(execution, idx, action, result)

# Becomes:
record = _build_action_state_record(
    step_index=idx,
    action=action,
    result=result,
    execution_state="completed",
)
await execution.action_sink.append(
    record,
    step_index=idx,
    action_type=action.action_type,
)
await self._record_step_succeeded(execution, idx, action, result)
```

The record is built BEFORE the event_stream emit so it persists even
if the emit fails. The reverse ordering (event first, record second)
would risk an event without a record on emit-then-crash.

Failure-path wrap at line 608 and line 632 follows the same shape
with `execution_state="failed"` and `failure_reason` in
`user_visible_summary`.

### `_build_action_state_record` helper

Constructs the record from the workflow step context:

- `action_id` — generated UUID prefix `act_` (mirrors note_this).
- `surface` — `"workflow_step"` (new surface value).
- `operation` — `action.action_type` (e.g., `notify_user`,
  `call_tool`, `write_canvas`).
- `operation_class` — derived from `action.action_type` per the
  `action_classification.py` discipline. World-effect verbs map to
  `mutate` (notify_user, write_canvas, route_to_agent, call_tool,
  post_to_service); direct-effect verbs map to `mark` (`mark_state`,
  `append_to_ledger`) — `mark` isn't a current value in
  `ACTION_OPERATION_CLASSES` so direct-effect verbs use `mutate` too
  for v1; if soak shows the distinction matters, a follow-up extends
  the enum.
- `authorization_state` — `"not_required"` for non-gated steps;
  `"confirmed"` for steps following an approval gate that released.
- `execution_state` — `completed` on success path; `failed` on
  failure path.
- `receipt_refs` — references to upstream tool results when
  applicable; empty for `mark_state` / `append_to_ledger`.
- `affected_objects` — substrate IDs the step touched (e.g., the
  ID from a `write_canvas` result's receipt). Empty for
  non-affecting verbs.
- `user_visible_summary` — `f"workflow step {idx} ({action_type}) completed"`
  on success or the failure_reason prefix on failure.
- `risk_level` — `low` for v1; future spec can carry per-action
  classification.

This helper lives in `action_sink.py` so the engine doesn't grow a
record-construction surface.

## Embedded live tests

Substrate-fidelity assertion pattern: assert against substrate
state (catalog rows + event_stream queries + sink reads), not against
prose summaries.

### Successful step receipt

`tests/test_workflow_action_sink.py::TestStepReceiptSuccess`

1. **`test_step_succeeded_produces_record_and_event`** — register a
   workflow with a single `mark_state` action; trigger it; verify
   the event_stream has a `workflow.execution_step_succeeded` event
   AND the sink has an ActionStateRecord row; both carry the same
   `correlation_id`; the record's `execution_state` is `completed`;
   `operation` matches `action_type`.
2. **`test_record_persisted_before_event_emit`** — inject an
   event_stream stub that raises on emit; verify the sink still has
   the record (record-before-emit ordering).
3. **`test_action_id_routable_via_get_by_action_id`** — record an
   action; query the sink by `action_id`; verify the same record
   comes back.

### Failed step receipt

`tests/test_workflow_action_sink.py::TestStepReceiptFailure`

1. **`test_execute_raised_failure_record`** — register a workflow with
   an action that raises during execute; verify the sink has a
   record with `execution_state="failed"` and
   `user_visible_summary` containing the exception class name (per
   the existing `workflow.execution_step_failed` event's
   `error` format).
2. **`test_verifier_rejected_failure_record`** — register a workflow
   with an action whose verify returns False; verify the sink has a
   record with `execution_state="failed"` and `verifier_rejected` in
   the summary.
3. **`test_continuation_continue_does_not_orphan_records`** —
   workflow with `continuation_rules.on_failure="continue"`; second
   step fails; third step succeeds; verify both step 2 (failed) and
   step 3 (succeeded) have records.

### Resume across restart (v2 — atomicity-aware)

`tests/test_workflow_action_sink.py::TestResumeIdempotency`

1. **`test_idempotent_on_workflow_execution_id_step_index`** — record
   a step; call append again with the same
   `(workflow_execution_id, step_index)`; verify the second call
   returns False and the sink still has one row.
2. **`test_engine_restart_does_not_double_emit`** — simulate a
   workflow that completed step 0, then engine restart; verify
   step 0's record persists across restart AND the engine's
   re-enter-at-start_idx logic doesn't fire a second append for
   step 0.
3. **`test_atomic_boundary_record_and_cursor_advance_together`** —
   v2 Decision 6: simulate a crash AFTER the transaction commits
   (record + cursor both advanced); restart; verify the engine moves
   past the step without re-executing.
4. **`test_atomic_boundary_rolls_back_on_failure`** — v2 Decision 6:
   simulate a failure DURING the transaction (e.g., the cursor
   advance raises after the record append within the same txn);
   verify neither the record nor the cursor advance lands. On
   restart, the engine re-executes the step cleanly.
5. **`test_crash_window_state_self_heals_on_startup`** — v2
   Decision 6: directly insert a row into `workflow_action_records`
   for step N WITHOUT advancing `action_index_completed`; start the
   engine; verify the startup pass logs the WORKFLOW_CRASH_WINDOW
   warning AND reconciles the cursor to step N's value. Verify
   subsequent execution moves past step N rather than re-executing.

### FRICTION-PATTERN composition (v2 — lifecycle-dispatch)

`tests/test_workflow_action_sink.py::TestFrictionPatternComposition`

1. **`test_workflow_step_failure_records_friction_occurrence_for_active`**
   — pre-seed a FrictionPattern in `active` state with
   `signal_type_keys` matching the workflow step's failure signature;
   trigger a failing workflow step; verify the FrictionPattern's
   `occurrence_count` increments via `record_occurrence`.
2. **`test_workflow_step_failure_calls_record_recurrence_for_resolved`**
   — v2 High 3: pre-seed a FrictionPattern in `resolved` state;
   trigger a failing workflow step that matches; verify
   `record_recurrence` is called (NOT `record_occurrence`), and a
   `friction.pattern_recurrence` event fires.
3. **`test_workflow_step_failure_skips_archived_pattern`** — v2 High
   3: pre-seed an `archived` pattern; trigger a failing step that
   would match; verify neither `record_occurrence` nor
   `record_recurrence` is called.
4. **`test_idempotency_skip_does_not_double_count_friction`** — v2
   High 3 follow-on: trigger a workflow step that fires the friction
   hook once; restart and trigger the same step again (which
   INSERT-OR-IGNOREs); verify the friction pattern's
   `occurrence_count` reflects ONE increment, not two. The sink's
   classify-only-on-actual-insert discipline pins this.
5. **`test_workflow_step_success_does_not_fire_friction_hook`** —
   only failed steps fire the friction hook; successful steps don't.

## Risks and design constraints

| Risk | Mitigation |
|---|---|
| Schema migration of `workflow_executions` to add composite UNIQUE | Pre-existing-table-aware migration in `_ensure_schema`; CREATE UNIQUE INDEX IF NOT EXISTS in race-tolerant pattern. Mirrors the existing gate_nonce migration. |
| Record-build cost on every step (extra DB write per workflow step) | Workflow steps are already DB-bound (event_stream write + ledger append). One transaction with two writes is amortized; v1 accepts the cost. |
| Resume-idempotency hiding re-execution (Codex Blocker 1) | v2 Decision 6 atomic boundary: record append + cursor advance in one transaction. The PK collision becomes a clean idempotency signal only when both lands or neither does. |
| Crash-window state leaks from pre-v2 deployments | Engine startup self-healing pass reconciles cursor to highest recorded step; logs WORKFLOW_CRASH_WINDOW loud (Decision 6 second block). |
| Action-record-construction failure (e.g., enum validation) | Failure in `_build_action_state_record` MUST not abort the workflow step. Wrap in try/except; on failure, log loud and skip the record. Preserves the workflow-runs-even-if-audit-fails invariant. |
| Friction-pattern double-counting on resume-retry (Codex High 3) | v2 Decision 7: friction hook only fires when the sink append actually inserted (returned True), not on INSERT-OR-IGNORE skip. |
| Friction-pattern wrong-method dispatch on resolved patterns (Codex High 3) | v2 Decision 7: workflow-side friction hook mirrors `FrictionObserver._classify_and_record` exactly; dispatches by `lifecycle_state` to `record_occurrence` / `record_recurrence` / unclassified. |
| `authorization_state="confirmed"` claim on gated steps that haven't released yet (Codex High 2) | v2 Decision 5: ALL workflow step records use `authorization_state="not_required"`. Gate state stays on `workflow.execution_paused_at_gate` / `.execution_resumed` events. |
| Risk-level uniformity loses substrate truth (Codex Medium 6) | v2 Decision 5: `risk_level` derived from action_type; `low / medium / high` mapping per the spec body's table; `missing_metadata=True` flag when `call_tool` risk can't be derived. |
| Failure-summary drift between record and event (Codex Low 7) | v2 Decision 5: error string computed once and passed to both `_build_action_state_record` and `_record_step_failed`. |
| Cross-instance leakage | Composite PK includes `instance_id`. Per-instance scoping by construction. |
| Conflict with future renderer changes | ActionStateRecord shape unchanged → renderer changes are independent of this spec. |

## Open questions (Codex pre-spec review)

Surfacing transparently:

1. **Schema deviation from architect's lean (Decision 1).** Codex's
   call on whether to preserve schema (my choice) or to extend
   ActionStateRecord with `workflow_context` (architect's lean). The
   schema extension would require updates to every existing producer
   (note_this, coding_session_bridge, integration runner). My
   preserve-schema approach pushes context to the storage row.
2. **FK target on `workflow_executions`.** Adding a composite UNIQUE
   index for FK enforcement, OR dropping the FK and relying on
   tool-implementation discipline. I lean former; Codex may have a
   substrate-side preference.
3. **`operation_class` mapping for `mark_state` / `append_to_ledger`.**
   These verbs don't fit the existing `ACTION_OPERATION_CLASSES`
   vocabulary cleanly (`read/propose/mutate/delete/send/schedule/register/manage`).
   v1 maps both to `mutate`; if `mark` or `record` is a better fit,
   adding to the enum is a small change but it touches every existing
   producer's validator.
4. **`risk_level` for workflow steps.** v1 uses `low` uniformly.
   Workflow actions can have wildly different risk profiles
   (a `route_to_agent` that posts to a public inbox is higher-risk
   than a `mark_state`). Per-action `risk_level` would require either
   declaration on the action descriptor or a derivation rule;
   deferred to a follow-up.
5. **Concurrent workflow restart + new run on same execution_id.**
   Theoretically the engine guards against this via state machine,
   but the sink's `INSERT OR IGNORE` would silently merge the two
   if the guard were ever broken. Defensive but flagging.

## Sequence (per architect directive)

1. ✅ Architect-framed (Notion `35cffafef4db81c38131ef967cde367c`).
2. ✅ CC drafts spec at `specs/ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1.md`
   on branch `actionstaterecord-workflow-composition-v1`
   (commit `8fc53dd` v1 / `c5af8a6` v1 + context doc).
3. ✅ **Codex pre-spec review round 1** — caught one blocker
   (resume idempotency hides re-execution), two high (gate
   authorization semantics; friction dispatch by lifecycle), three
   medium (Decision 1 rationale; connection ownership; risk_level
   uniformity), one low (failure summary drift). Plus all four
   architect-listed open questions answered.
4. ✅ **CC folds Codex round 1** into spec body — this v2 revision.
5. 🟡 **Architect ratification of v2 spec body** — pending. Architect
   reviews diff (the round-1 fold isolated by diffing v2 against
   `c5af8a6` v1+context).
6. CC implements per ratified v2 spec.
7. Codex post-implementation review.
8. CC any final changes.
9. Architect ratifies on close.

Per pipeline compression rules (Spec 1's closeout): multi-round Codex
pre-spec review expected given schema + state-machine complexity.
Round 1 caught one blocker; whether round 2 is needed is the
architect's call at v2 ratification.

## Linked artifacts

- Architect spec build directive: Notion
  `35cffafef4db81c38131ef967cde367c`
- PHASE-3-AUTONOMY-LOOP framing: Notion
  `35cffafef4db81da8107e562307bc738`
- CC's earlier workflow-primitive substrate review (Option A/B/C
  rationale): Notion `35cffafef4db81f4a344e05ca9a2c9a8`
- FRICTION-PATTERN-STABLE-IDS-V1 (Spec 1; merged at `452dbee`):
  `specs/FRICTION-PATTERN-STABLE-IDS-V1.md`
- CODING-SESSION-BRIDGE-V1 (Spec 2; merged at `a16c1d9`):
  `specs/CODING-SESSION-BRIDGE-V1.md`
- RESPONSE-FIDELITY-V1 (discipline this spec extends): Notion
  `35affafef4db8147a79adae3892df3e9`
- Workflow primitive code: `kernos/kernel/workflows/execution_engine.py`
- ActionStateRecord schema: `kernos/kernel/integration/briefing.py:1121`
