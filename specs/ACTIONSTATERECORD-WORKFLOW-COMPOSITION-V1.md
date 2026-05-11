# ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1 — Implementation Spec

**Status:** DRAFT v1 — pre-Codex-pre-spec-review. Architect-framed
2026-05-10 (Notion `35cffafef4db81c38131ef967cde367c`). Open for Codex
pre-spec review.

**Author:** CC, 2026-05-11. Resolves the architect's four open
architectural questions explicitly; deviates from one architect lean
with rationale (Decision 1 — see below); surfaces three new open
questions for Codex.

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

**Why deviate:** ActionStateRecord is a frozen dataclass with strict
`__post_init__` validation against five enum constants
(`ACTION_OPERATION_CLASSES`, `ACTION_AUTHORIZATION_STATES`,
`ACTION_EXECUTION_STATES`, `ACTION_RISK_LEVELS`,
`ACTION_EVIDENCE_CLASSES`). It already has 13 fields. Adding a 14th
optional `workflow_context` field would:

- Require validator updates across every existing record producer.
- Require renderer updates (Batch 2 of RESPONSE-FIDELITY-V1 is
  in flight — adding a field mid-arc would force a coupling pin on
  that batch).
- Violate the architect's own "preserve existing schema" rule in the
  NOT-ships list.

Pushing workflow context to the storage row (not the record itself)
preserves the single-shape discipline at the dataclass level while
still letting workflow-scoped consumers filter / query by execution
context. The record is byte-identical to a turn-scoped record; the
table adds adjacent columns.

This is the safer move and matches the explicit NOT-ships constraint.
Architect's lean on single-shape-with-optional-fields was the right
intuition for v1 simplicity; my resolution preserves the intuition
without requiring a substrate-wide schema edit.

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

### Decision 3 — `WorkflowActionSink` lifecycle + ownership

**Resolution:** per-execution `WorkflowActionSink` instance constructed
by the engine at execution start; persists alongside the existing
`workflow_executions` row.

The sink has a SHARED `aiosqlite` connection — one connection per
engine instance, not per execution. Per-execution sink instances are
thin wrappers carrying the `(instance_id, workflow_execution_id)`
context; they share the underlying writer. Per-execution rather than
shared because:

- Each sink carries the workflow_execution_id for every record it
  writes, removing one argument from every call.
- Failure isolation: if one workflow's sink writes fail repeatedly,
  it doesn't poison the writer connection for the engine; the engine
  recreates the wrapper for the next execution.
- Mirrors the existing `_turn_action_records` per-turn ephemeral
  shape (each turn gets its own list); workflows just have a
  longer-lived equivalent backed by SQLite.

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

### Decision 5 — Failure-path receipt shape

**Resolution:** failed steps produce ActionStateRecords with:

- `execution_state="failed"`
- `user_visible_summary` carries a structured `failure_reason` prefix
  (e.g., `"verifier_rejected: ..."` or
  `"execute_raised:AgentInboxUnavailable: ..."`) — the same format
  the existing `workflow.execution_step_failed` event uses in its
  `error` field.

ActionStateRecord doesn't have a dedicated `failure_reason` field; the
existing convention across producers (note_this, coding_session_bridge,
etc.) is to put failure reasons in `user_visible_summary`. We follow
that convention rather than add a new field (Decision 1 logic).

`partial_state` is None for v1 workflow steps. If a workflow step's
verifier reports partial completion (`result.success=False` with a
partial-completion semantic) a future spec can extend the failure
shape. v1 treats verifier-rejected as failed-clean.

### Decision 6 — Resume-safe semantics

**Resolution:** sink writes use `INSERT OR IGNORE` on the composite PK
`(instance_id, workflow_execution_id, step_index)`. Two consequences:

1. **Restart of an in-flight workflow:** when the engine restarts and
   re-enters the loop at `start_idx = max(0, action_index_completed + 1)`
   (existing logic at `execution_engine.py:578`), it doesn't replay
   prior steps. The sink would not be called for those steps anyway.
2. **Resume-safe step retry within the same workflow run:** if a
   `resume_safe` step is re-executed (rare; current implementation
   doesn't retry, but a future continuation-rules retry path could),
   the second emission silently skips. The first record is
   authoritative.

The architect's lean ("prior steps' records persist; no re-emission
on resume") is honored both ways. Implementation correctness check:
the sink's `append` returns `False` on idempotency skip so the engine
can log the skip at DEBUG without firing a second `workflow.*` event.

## Open architectural questions (for Codex pre-spec review)

1. **Decision 1 deviation.** I went against architect's
   "ActionStateRecord-with-optional-field" lean in favor of
   "preserve schema; carry workflow context at the storage row level."
   Rationale in Decision 1. Codex's call on whether the deviation is
   load-bearing or whether I should fold to architect's preference
   (which would mean adding a `workflow_context: dict | None = None`
   field to ActionStateRecord and updating every existing producer).

2. **FK target on `workflow_executions`.** The current schema has
   `execution_id TEXT PRIMARY KEY` — singular. Composite FK from
   `workflow_action_records` requires either adding a composite
   UNIQUE index to `workflow_executions` OR dropping the FK and
   trusting tool-implementation discipline. I lean composite UNIQUE
   (Decision 2 option a) because it mirrors FRICTION-PATTERN's
   substrate-level FK enforcement; flagging for Codex.

3. **`partial_state` for workflow steps.** Currently None for all
   workflow ActionStateRecords. Future use case: a workflow step that
   wraps `call_tool` could surface the tool's `partial` state through
   the step's record. v1 doesn't ship this; documenting deferral.

## Code-level shape

### File map

- NEW: `kernos/kernel/workflows/action_sink.py` (~180 LOC):
  `WorkflowActionSink` class, schema-in-store via `ensure_schema()`,
  per-execution wrapper construction via `for_execution()`.
- MODIFIED: `kernos/kernel/workflows/execution_engine.py`:
  - `_EXECUTIONS_SCHEMA` gains a `CREATE UNIQUE INDEX` on
    `(instance_id, execution_id)` for the FK target.
  - `_ensure_schema` runs the new sink's schema migration alongside
    the executions schema.
  - `WorkflowEngine.__init__` constructs the sink with the shared
    connection.
  - `WorkflowEngine._run_step` (the existing step-execute loop body,
    around line 580) builds an ActionStateRecord at each
    `_record_step_succeeded` / `_record_step_failed` call site and
    appends through the sink BEFORE the existing
    `workflow.execution_step_*` event_stream emit (so the
    ActionStateRecord row exists if the event_stream emit fails).
- MODIFIED: `kernos/kernel/workflows/__init__.py` — exports
  `WorkflowActionSink`.

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

### Resume across restart

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

### FRICTION-PATTERN composition

`tests/test_workflow_action_sink.py::TestFrictionPatternComposition`

1. **`test_workflow_step_failure_records_friction_occurrence`** —
   pre-seed a FrictionPattern with `signal_type_keys` matching the
   workflow step's failure signature; trigger a failing workflow
   step; verify the FrictionPattern's `occurrence_count` increments.
   This tests the architect-named load-bearing benefit of Option A:
   workflow steps as friction observation surfaces.

## Risks and design constraints

| Risk | Mitigation |
|---|---|
| Schema migration of `workflow_executions` to add composite UNIQUE | Pre-existing-table-aware migration in `_ensure_schema`; ALTER then CREATE INDEX. Mirrors the existing gate_nonce migration pattern. |
| Record-build cost on every step (extra DB write per workflow step) | Workflow steps are already DB-bound (event_stream write + ledger append). One more `INSERT OR IGNORE` is amortized; v1 accepts the cost. |
| Idempotency races on resume-retry | `INSERT OR IGNORE` is atomic in SQLite; resume cannot race a fresh execute. The PK collision IS the resume-skip signal. |
| Action-record-construction failure (e.g., enum validation) | Failure in `_build_action_state_record` MUST not abort the workflow step. Wrap in try/except; on failure, log loud and skip the record. Preserves the workflow-runs-even-if-audit-fails invariant. |
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
   on branch `actionstaterecord-workflow-composition-v1` (this commit).
3. 🟡 **Codex pre-spec review** — pasteable blip via founder-relay;
   Codex reviews from repo state on this branch.
4. CC folds Codex review.
5. Architect ratification of spec body.
6. CC implements per spec.
7. Codex post-implementation review.
8. CC any final changes.
9. Architect ratifies on close.

Per pipeline compression rules (Spec 1's ratification page): multi-round
Codex pre-spec review expected given schema + state-machine complexity.

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
