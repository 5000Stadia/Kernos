# ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1 — Implementation Spec

**Status:** DRAFT v-final — pre-implementation. TWO Codex pre-spec
review rounds folded (7 + 8 findings) + FIVE architect calls folded
2026-05-11. Codex round 2 returned 4 blocker/high implementation-
surface findings and 4 medium/low — all 8 folded here. The
architectural decisions remain locked from v3; v-final only changes
the implementation surface (atomicity matrix per outcome class, self-
heal scope narrowed, async write lock + helper, targeted ON CONFLICT,
stale-text sweep, instance_id binding from parent execution, friction
unclassified emission, call_tool operation_class source).

**Codex round 1 (7 findings, folded into v2):**

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

**Architect's five calls (folded into v3):**

- **Q1 — Preserve ActionStateRecord schema** (deviation confirmed
  correct). Workflow context is metadata about *where* an action
  came from, not *what* the action is; pushing context to the
  storage row preserves single-shape discipline on the dataclass
  and avoids cross-arc coupling with RESPONSE-FIDELITY Batch 2.
  v2 was already correct; v3 promotes the deviation to canonical
  decision.
- **Q2 — FK on `workflow_executions(execution_id)` SINGULAR, not
  composite.** Workflow `execution_id` is UUID-generated and
  globally unique; FK on the singular column is sufficient.
  `instance_id` in the new table's composite PK is for query
  locality and partitioning, NOT FK targeting. No composite UNIQUE
  migration needed on `workflow_executions`. Changes v2's
  composite-FK design back to singular.
- **Q3 — `operation_class` `mutate` for `mark_state` and
  `append_to_ledger`** (v2 already correct). Vocabulary expansion
  deferred to a future spec.
- **Q4 — `risk_level` derived from `operation_class`, not
  `action_type`.** Mapping: `read` → low; `mutate` → medium;
  `delete` → high; other classes (including `send`,
  `notify_user`-style verbs, `call_tool`) → medium. Uses
  vocabulary that already exists in the schema. Changes v2's
  action_type-based three-tier mapping to a simpler
  operation-class-derived rule.
- **Q5 — Concurrent restart edge case NOT a v1 concern.**
  UUID-generated `execution_id` provides probabilistic uniqueness;
  the engine's state machine prevents the paused-then-terminated-
  then-new path. Pulled back from "load-bearing" framing to
  defensive flag in Decision 6. NOTE: the atomic-boundary fix from
  Codex Blocker 1 stays in place — it addresses a DIFFERENT
  concern (single-engine crash between record append and cursor
  advance), not inter-engine concurrent restart. Q5 specifically
  resolves the inter-engine collision question.

**Codex round 2 (8 findings, folded into v-final):**

- **Blocker** — Decision 6's "append + advance cursor" atomic
  boundary was correct for non-gated success and continue-on-failure
  outcomes only. For gated success it would bypass the approval
  wait on restart (cursor advances before gate release); for
  aborting failure the abort transition must commit in the same
  transaction as the failed record. Refactored to a **per-outcome
  transaction matrix** with four shapes (Decision 6 below).
- **High** — engine-startup self-heal was too broad ("reconcile
  cursor to highest recorded step"). Could skip pending gates or
  promote aborting failures into continuation. Narrowed to a
  state-aware reconcile that inspects `record.execution_state`,
  `action.continuation_rules.on_failure`, `action.gate_ref`, and
  `execution.gate_nonce` before advancing.
- **High** — shared `aiosqlite` connection + `BEGIN IMMEDIATE`
  isn't enough on its own under concurrent asyncio tasks. Added an
  engine-level `asyncio.Lock` plus `_run_workflow_txn()` helper
  that owns rollback + bounded busy-retry semantics. The
  step-execute path acquires the lock before BEGIN IMMEDIATE.
- **High** — bare `INSERT OR IGNORE` can suppress non-PK
  constraint failures. Replaced with targeted
  `ON CONFLICT(instance_id, workflow_execution_id, step_index) DO NOTHING`.
  Append returns False only on PK conflict; other constraint
  failures raise.
- **Medium** — stale text in earlier sections still said sink-owned
  connection, `authorization_state="confirmed"` for gated steps,
  uniform/old risk_level mapping, the v1 open-questions list, and
  non-transactional step-execute wrap pseudocode. Swept the
  deliverables, code-shape, helper, risks, and open-question
  sections to match v-final.
- **Medium** — singular FK target can't enforce
  `workflow_action_records.instance_id == workflow_executions.instance_id`
  at the schema layer. Added a writer invariant: the per-execution
  sink wrapper binds `instance_id` directly from the parent
  `WorkflowExecution`; caller-supplied `instance_id` arguments are
  ignored (or asserted to match in debug builds). Plus a test that
  pins it.
- **Medium** — Decision 7 said "mirror `FrictionObserver._classify_and_record`
  exactly" but the pseudocode silently dropped the unclassified /
  archived path. Folded by emitting `workflow.friction_pattern_unclassified`
  in the v1 implementation (the workflow-side analog of the
  observer's `_emit_pattern_unclassified` path). Also added
  `member_id` to the execution-sink API so the workflow-side
  attribution mirrors the observer's exactly.
- **Low** — `call_tool` `operation_class` derivation didn't name
  its source. Defined as: lookup against the tool registry's
  declared `operation_class` for the wrapped tool when present;
  otherwise default to `operation_class="mutate"`,
  `risk_level="medium"`, `missing_metadata=True`. Renderer / audit
  consumers can distinguish derived-from-registry vs default-with-
  missing-metadata via the flag.

**Author:** CC, 2026-05-11. Resolves architect's framing + folds
Codex round 1 + architect's five v3 calls + Codex round 2.

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
   `workflow_executions` table. Uses the engine's **shared
   `aiosqlite` connection** (Decision 3 / Codex round 1 Medium 5)
   because the atomicity matrix in Decision 6 requires the record
   write + cursor advance / gate persist / abort transition to land
   inside one transaction. Per-execution wrappers
   (`WorkflowExecutionActionSink`) bind execution context AT
   CONSTRUCTION TIME (Codex round 2 Medium 6): instance_id and
   workflow_execution_id are NEVER passed by callers on append().
3. **Engine-side wrap in `execution_engine.py`** — at each of the
   four outcome sites (non-gated success, gated success,
   continue-on-failure, aborting failure), construct an
   ActionStateRecord reflecting the outcome and append via the sink
   inside `_run_workflow_txn(...)` with the matching write payload
   (Decision 6 per-outcome matrix). The success path at the
   existing line ~647 splits into gated vs non-gated; the
   execute-raised path at ~608 and verifier-rejected path at ~632
   feed into the abort-or-continue branch.
4. **Failure-path discipline** — failed steps produce
   `execution_state="failed"` ActionStateRecords with a populated
   `failure_reason` (carried in `user_visible_summary` since
   ActionStateRecord doesn't have a dedicated `failure_reason` field;
   see Decision 5). Aborting failure transitions the execution row
   to `state='aborted'` in the same transaction as the record append
   (Decision 6).
5. **Resume-safe idempotency** — the sink's write uses targeted
   `ON CONFLICT(instance_id, workflow_execution_id, step_index) DO NOTHING`
   (Codex round 2 High 4) so PK conflict skips the insert but other
   constraint failures raise. Combined with the per-outcome
   atomicity matrix and the engine-startup state-aware self-heal
   pass (Decision 6), restart-resume of an in-flight workflow
   doesn't re-emit records, doesn't bypass gates, and doesn't
   promote aborts into continuations.
6. **Async write-lock + helper** (Codex round 2 High 3) —
   engine-level `asyncio.Lock` plus `_run_workflow_txn()` helper
   with rollback + bounded busy-retry. Atomic operations route
   through the helper; the lock guarantees BEGIN IMMEDIATE / COMMIT
   boundaries don't interleave across concurrent asyncio tasks
   sharing the underlying connection.
7. **Embedded live tests** — substrate-fidelity assertion probes
   covering all four outcomes from the matrix, the state-aware
   self-heal, ON CONFLICT semantics, instance_id binding, write-
   lock serialization, plus FRICTION-PATTERN composition (active /
   resolved / archived / unclassified) and the `call_tool`
   operation_class source rule.

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

**Resolution (revised v3 per architect Q2):** new table
`workflow_action_records` in `instance.db`. **FK is on the singular
`execution_id` column**, not composite. `instance_id` in the new
table's PK is for query locality and partitioning, NOT FK targeting.

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
    FOREIGN KEY (workflow_execution_id)
        REFERENCES workflow_executions(execution_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_workflow_action_records_action_id
    ON workflow_action_records (instance_id, action_id);
CREATE INDEX IF NOT EXISTS idx_workflow_action_records_workflow
    ON workflow_action_records (instance_id, workflow_id);
```

**Architect's Q2 reasoning:** workflow `execution_id` is UUID-generated
and globally unique. FK on the singular column is sufficient for
referential integrity. The FRICTION-PATTERN composite-PK-with-composite-FK
convention applied THERE because `pattern_id` is a per-instance
namespace (same slug can exist independently across instances).
Workflow execution_id has no equivalent namespacing constraint;
singular FK is the right fit.

**Consequence:** no schema migration on `workflow_executions` is
required. The existing singular `execution_id` PK at
`execution_engine.py:213` already serves as the FK target.

`(instance_id, workflow_execution_id, step_index)` composite PK on
the new table is still load-bearing for two things:

1. **Resume-safe idempotency contract** — targeted `ON CONFLICT
   (instance_id, workflow_execution_id, step_index) DO NOTHING` on
   the primary key is the cheapest way to skip re-emission under
   the per-outcome atomicity matrix (Decision 6).
2. **Query locality / partitioning** — most queries filter by
   `instance_id` first; having it as the PK leading column lets
   SQLite use the PK index for these lookups.

`ON DELETE RESTRICT` mirrors FRICTION-PATTERN's no-destructive-deletions
discipline. Workflow executions transition through state machine
(running → completed / aborted / paused / resumed); they don't get
DELETE'd. If a future GC spec wants to clean up old terminated
executions, it ships its own pre-removal pass over
`workflow_action_records` first.

**PRAGMA foreign_keys=ON** is mandatory on every connection (same
discipline as FRICTION-PATTERN-STABLE-IDS-V1; Codex caught this in
round 2 there). The sink runs the pragma in `ensure_schema()` and
on every reconnect.

### Decision 3 — `WorkflowActionSink` lifecycle + ownership + write-lock (v-final fold)

**Resolution (revised per Codex round 1 + round 2):** the sink uses
the **engine's shared `aiosqlite` connection** to the workflow DB —
NOT its own. Per-execution `WorkflowExecutionActionSink` wrappers
(thin) carry the bound execution context. Plus, per Codex round 2
High 3, the engine owns an **asyncio write-lock** and a
`_run_workflow_txn()` helper; the sink delegates atomic operations
through that helper so concurrent asyncio tasks don't collide on the
shared connection.

The shared connection is **load-bearing for Decision 6's atomicity
matrix:** the single-transaction boundary that wraps "append record +
advance `action_index_completed`" (or "append record + transition to
aborted state") can only span both tables when they share a
connection. A sink-owned connection would force a two-phase commit
pattern that v1 doesn't earn.

**Write-lock + helper (Codex round 2 High 3):** `aiosqlite` serializes
SQL statements through a single background thread per connection, but
nothing prevents two asyncio tasks from interleaving
`BEGIN IMMEDIATE` / `COMMIT` boundaries when they share that
connection. A second task entering `BEGIN IMMEDIATE` while a first
task's transaction is still open causes SQLite to error with "cannot
start a transaction within a transaction." The engine guards the
boundary explicitly:

```python
class WorkflowEngine:
    def __init__(self, ...):
        ...
        self._workflow_db_write_lock = asyncio.Lock()

    async def _run_workflow_txn(
        self,
        body: Callable[[aiosqlite.Connection], Awaitable[T]],
        *,
        retries: int = 3,
        retry_backoff_ms: int = 50,
    ) -> T:
        """Run `body` inside a BEGIN IMMEDIATE transaction with
        rollback-on-error and bounded busy-retry. Holds the engine's
        write-lock for the duration so concurrent step-execute tasks
        serialize. Caller's `body` must not call BEGIN/COMMIT itself."""
        attempt = 0
        async with self._workflow_db_write_lock:
            while True:
                try:
                    await self._workflow_db.execute("BEGIN IMMEDIATE")
                    try:
                        value = await body(self._workflow_db)
                    except Exception:
                        await self._workflow_db.execute("ROLLBACK")
                        raise
                    await self._workflow_db.commit()
                    return value
                except aiosqlite.OperationalError as exc:
                    if "database is locked" in str(exc) and attempt < retries:
                        attempt += 1
                        await asyncio.sleep(retry_backoff_ms / 1000 * (2 ** (attempt - 1)))
                        continue
                    raise
```

The step-execute path calls `_run_workflow_txn(...)` instead of
manually issuing `BEGIN IMMEDIATE` / `COMMIT`. The helper centralizes
rollback + retry so individual call sites stay short and the
write-lock discipline is impossible to bypass accidentally.

**`instance_id` binding from parent (Codex round 2 Medium 6):** the
singular-FK target on `workflow_executions(execution_id)` enforces
referential integrity for the execution row but cannot enforce that
`workflow_action_records.instance_id` matches its parent execution's
`instance_id` (FKs in SQLite don't span non-PK columns of the parent
without a composite UNIQUE). The writer-side invariant pins this:

> `WorkflowExecutionActionSink` is constructed with a reference to
> the parent `WorkflowExecution` instance. Its `append()` reads
> `instance_id`, `workflow_execution_id`, `workflow_id`, and
> `correlation_id` DIRECTLY from `execution`, NOT from caller
> arguments. Caller arguments for these fields are removed from the
> API surface. A `WorkflowExecution` is the only legitimate source
> of these values; if mismatched values were ever supplied via some
> future code path, the writer-side binding would silently ignore
> them.

A test (`TestSinkContextBinding::test_caller_cannot_override_instance_id`)
pins the invariant by asserting that no public API on the per-
execution sink accepts `instance_id` as an argument.

Schema setup order on engine start (revised v3 per Q2 — no composite
UNIQUE migration):

1. Ensure `workflow_executions` table.
2. ALTER-IF-MISSING the existing migration columns (`gate_nonce`,
   `fire_id`) per the gate_nonce migration pattern.
3. CREATE `workflow_action_records` (FK targets the existing
   singular `execution_id` PK; no migration on `workflow_executions`
   required).
4. CREATE indexes on `workflow_action_records`.

The engine owns the connection lifecycle. The sink is constructed
once at engine start and passed a borrowing reference; it does not
open or close the connection itself.

The sink exposes:

```python
class WorkflowActionSink:
    """Per-execution ActionStateRecord persistence for workflow steps."""

    async def ensure_schema(self, data_dir: str) -> None: ...

    def for_execution(
        self,
        execution: WorkflowExecution,
        *,
        member_id: str = "",
    ) -> "WorkflowExecutionActionSink":
        """Per-execution wrapper. `instance_id`, `workflow_execution_id`,
        `workflow_id`, `correlation_id` are bound from `execution`.
        `member_id` is bound here (workflow-step occurrences attribute
        to the member that triggered the workflow execution)."""

    async def list_for_execution(
        self,
        instance_id: str,
        workflow_execution_id: str,
    ) -> list[WorkflowActionRecord]:
        """All records persisted for this execution, ordered by step_index."""

    async def get_by_action_id(
        self,
        instance_id: str,
        action_id: str,
    ) -> WorkflowActionRecord | None: ...


class WorkflowExecutionActionSink:
    """Per-execution wrapper. The execution's identity is BOUND at
    construction time; callers do NOT pass instance_id /
    workflow_execution_id / workflow_id / correlation_id on append()."""

    async def append(
        self,
        record: ActionStateRecord,
        *,
        step_index: int,
        action_type: str,
    ) -> bool:
        """Append a record. Returns True if persisted; False if a
        PK conflict (instance_id, workflow_execution_id, step_index)
        skipped the insert. Other constraint failures raise."""
```

The engine constructs the per-execution wrapper via
`engine.action_sink_for(execution)` and discards it at execution end.
The shared underlying connection persists for the engine's lifetime.

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

#### `risk_level` derived from `operation_class` (revised v3 per architect Q4)

v2 had a per-action-type three-tier mapping (low / medium / high
per individual verb). v3 follows the architect's Q4 ruling: derive
`risk_level` from `operation_class` instead. Simpler rule; uses
vocabulary that already exists in the schema; no new mapping table
to maintain as new action verbs land.

**The rule:**

| `operation_class` | `risk_level` | Why |
|---|---|---|
| `read` | `low` | No state mutation |
| `propose` | `low` | Pre-commit; no substrate write |
| `mutate` | `medium` | Standard state write; reversible discipline |
| `delete` | `high` | Destructive; high reversal cost |
| `send` | `medium` | World-effect but typically reversible at the message layer |
| `schedule` | `medium` | World-effect but at a future-deferred level |
| `register` | `medium` | Adds substrate registration; reversible via tombstone |
| `manage` | `medium` | Per-tool variance; medium as conservative default |

**Workflow verb → operation_class assignments** (combine with the
above table to derive `risk_level`):

| Action verb | `operation_class` | `risk_level` |
|---|---|---|
| `mark_state` | `mutate` (per architect Q3) | `medium` |
| `append_to_ledger` | `mutate` (per architect Q3) | `medium` |
| `notify_user` | `send` | `medium` |
| `write_canvas` | `mutate` | `medium` |
| `route_to_agent` | `register` | `medium` |
| `post_to_service` | `send` | `medium` |
| `call_tool` | derived from wrapped tool; default `mutate` | derived; default `medium` |

Most workflow verbs land on `medium` under this rule. That's
intentional: workflow actions are inherently world-affecting (the
workflow primitive exists to coordinate world-effect work);
`medium` captures the baseline. `delete`-class operations (if a
workflow ever issues one through `call_tool`) surface as `high`
appropriately; `read`-class operations (a `call_tool` reading
state) surface as `low`.

**`missing_metadata=True` fallback:** when `call_tool` invokes a
tool whose own `operation_class` can't be determined at append
time, the record sets `missing_metadata=True`. Renderer / audit
consumers know the `risk_level` is the derivation default rather
than a derived value.

Helper `_risk_level_for_operation_class(operation_class)` lives in
`action_sink.py` alongside `_build_action_state_record` and the
operation_class assignment helper. Engine code stays compact.

**Architect's Q4 intent:** uniform `low` understates real risk;
per-action-type custom rules are over-engineering for v1. The
operation-class-derived approach gives appropriate gradation with
no new vocabulary, and the mapping is short enough to fit in a
single helper function.

### Decision 6 — Resume-safe semantics (v-final — per-outcome transaction matrix)

**Resolution (revised v-final per Codex round 2 Blocker 1 + High 2):**
the v2 draft folded the atomic-boundary fix as a single shape
("append record + advance cursor in one transaction"). Codex round 2
caught that the single shape is correct only for the non-gated
success and continue-on-failure outcomes. For **gated success** it
would advance the cursor BEFORE the gate-await resolves — restart
during the gate wait would then bypass approval entirely. For
**aborting failure** the abort transition must commit atomically
with the failed record so a restart-during-abort can't promote an
aborted execution back into a continuation state.

v-final replaces the single-shape boundary with a **per-outcome
transaction matrix**.

#### Per-outcome transaction matrix

Four outcomes; four transaction shapes. All four use
`_run_workflow_txn(...)` from Decision 3 so the write-lock + rollback
+ busy-retry discipline is uniform.

| Outcome | Inside one BEGIN IMMEDIATE txn | After commit |
|---|---|---|
| **Non-gated success** | INSERT record (`completed`); UPDATE cursor to `idx` | Emit `workflow.execution_step_succeeded` |
| **Gated success** | INSERT record (`completed`); UPDATE `gate_nonce = pending_gate_nonce` on the execution row (replaces the standalone `_persist_gate_nonce` write). Cursor is NOT advanced here. | Emit `workflow.execution_step_succeeded`; emit `workflow.execution_paused_at_gate`; enter `_await_gate`. Cursor advances later, inside `_clear_gate_nonce` on resume (already a write-lock-protected single statement; left as-is) |
| **Continue-on-failure** | INSERT record (`failed`); UPDATE cursor to `idx` | Emit `workflow.execution_step_failed`; loop continues |
| **Aborting failure** | INSERT record (`failed`); UPDATE `state='aborted'`, `aborted_reason=...`, `terminated_at=now` on the execution row. Cursor is NOT advanced. | Emit `workflow.execution_step_failed`; emit `workflow.execution_terminated`; return out of `_run_step` |

**Pseudocode (illustrative, lives in `execution_engine.py`):**

```python
# Non-gated success path:
record = _build_action_state_record(..., execution_state="completed")
await self._run_workflow_txn(
    lambda db: _append_and_advance(
        db, sink_ctx, record, step_index=idx,
        action_type=action.action_type,
    ),
)
await self._record_step_succeeded(execution, idx, action, result)

# Gated success path:
record = _build_action_state_record(..., execution_state="completed")
await self._run_workflow_txn(
    lambda db: _append_and_persist_gate_nonce(
        db, sink_ctx, record, step_index=idx,
        action_type=action.action_type,
        gate_nonce=pending_gate_nonce,
    ),
)
await self._record_step_succeeded(execution, idx, action, result)
# _await_gate -> on resume, _clear_gate_nonce -> _mark_step_complete:
#   the cursor advance lives inside _clear_gate_nonce's existing write.

# Continue-on-failure path:
record = _build_action_state_record(..., execution_state="failed")
await self._run_workflow_txn(
    lambda db: _append_and_advance(
        db, sink_ctx, record, step_index=idx,
        action_type=action.action_type,
    ),
)
await self._record_step_failed(execution, idx, action, error=error)
# Loop continues.

# Aborting failure path:
record = _build_action_state_record(..., execution_state="failed")
await self._run_workflow_txn(
    lambda db: _append_and_abort(
        db, sink_ctx, record, step_index=idx,
        action_type=action.action_type,
        aborted_reason=f"step_{idx}_{abort_reason_suffix}",
    ),
)
await self._record_step_failed(execution, idx, action, error=error)
# Emit terminated event (now part of the abort transaction's
# post-commit responsibility; existing _abort emits it).
```

Helper functions (`_append_and_advance`, `_append_and_persist_gate_nonce`,
`_append_and_abort`) live in `action_sink.py`. They take the bound
sink context plus the per-outcome write target and issue two SQL
statements per call. They are designed to be invoked inside
`_run_workflow_txn(body)` — they MUST NOT call BEGIN/COMMIT
themselves.

**Why four shapes instead of one:** restart semantics are
outcome-specific. After a crash, the engine reads
`(state, action_index_completed, gate_nonce)` for each execution and
decides what to do next:

- `state=running, no gate_nonce, cursor=K-1`: re-run step K
- `state=running, gate_nonce set, cursor=K-1`: gate was pending —
  re-enter `_await_gate` for step K (do NOT re-execute step K)
- `state=aborted`: skip resume; execution is terminal

The matrix preserves these invariants atomically across crashes.

#### Engine-startup self-healing pass (revised v-final per Codex round 2 High 2)

The v2 self-heal blindly reconciled `action_index_completed` to the
highest recorded step index. Codex round 2 caught that this could:

- Skip a pending gate (record exists for the gated step, gate_nonce
  is set, but the heal would advance past the step as if it had
  cleared the gate)
- Promote an aborting failure into continuation (record exists with
  `execution_state="failed"` for a step whose `on_failure="abort"`,
  but the heal would advance past it)

v-final's self-heal is **state-aware**. For each running execution at
engine startup:

```python
# Engine startup, after _ensure_schema completes:
async for execution in self._list_executions_in_state("running"):
    workflow = await self._load_workflow_def(execution)
    if workflow is None:
        continue
    last_record_step = await sink.get_max_step_index(
        execution.instance_id, execution.execution_id,
    )
    if last_record_step <= execution.action_index_completed:
        continue  # nothing to heal
    # Crash-window state. Investigate before advancing.
    record = await sink.get_by_step(
        execution.instance_id, execution.execution_id,
        step_index=last_record_step,
    )
    action = workflow.actions[last_record_step]
    can_advance = (
        record is not None
        and record.execution_state == "completed"
        and action.gate_ref is None
        and not execution.gate_nonce
    )
    can_advance_failed = (
        record is not None
        and record.execution_state == "failed"
        and action.continuation_rules.on_failure != "abort"
    )
    if can_advance or can_advance_failed:
        logger.warning(
            "WORKFLOW_CRASH_WINDOW_RECONCILE: execution_id=%s "
            "record_step=%d cursor=%d state=%s",
            execution.execution_id, last_record_step,
            execution.action_index_completed, record.execution_state,
        )
        await self._advance_cursor_to(execution, last_record_step)
    else:
        logger.warning(
            "WORKFLOW_CRASH_WINDOW_SKIP: execution_id=%s "
            "record_step=%d cursor=%d state=%s gate_ref=%s "
            "on_failure=%s gate_nonce=%s — restart will replay step "
            "or honor gate / abort path.",
            execution.execution_id, last_record_step,
            execution.action_index_completed,
            getattr(record, "execution_state", "?"),
            getattr(action, "gate_ref", None),
            getattr(getattr(action, "continuation_rules", None), "on_failure", "?"),
            execution.gate_nonce or "",
        )
```

The two log lines distinguish "heal applied" from "heal declined,
restart path will do the right thing on its own." The skip-and-log
path is the safety net: if any of the four predicates is wrong (a
record marked completed for a step that actually needs to wait at a
gate; a failed record on an `on_failure="abort"` step), the existing
restart logic — which already inspects `gate_nonce`,
`action.gate_ref`, and `action.continuation_rules` — handles the
outcome correctly without the heal.

#### Idempotency-skip semantics (revised v-final per Codex round 2 High 4)

The v2 spec body said the sink uses `INSERT OR IGNORE`. Codex round
2 caught that this is too broad — it suppresses non-PK constraint
failures (e.g., NOT NULL violations on `record_json`,
`recorded_at`, etc.) which would silently drop records that should
have raised.

v-final replaces the bare `INSERT OR IGNORE` with a **targeted ON
CONFLICT**:

```sql
INSERT INTO workflow_action_records (
    instance_id, workflow_execution_id, step_index, action_id,
    workflow_id, action_type, record_json, correlation_id, recorded_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(instance_id, workflow_execution_id, step_index) DO NOTHING
```

The sink reports a True/False return by inspecting `cursor.rowcount`
after the statement. `rowcount == 1` → True (newly inserted);
`rowcount == 0` → False (PK conflict; legitimate idempotency on the
resume-safe contract). Other constraint failures raise normally
because they have no `ON CONFLICT` branch.

The legitimate False-return paths inside the matrix:

- A second call with the same `(instance_id, workflow_execution_id,
  step_index)` on a restart-replay path → False, the wrapping
  transaction's cursor advance / state transition / gate persist is
  still applied (the row matched by PK already records this step's
  outcome).
- Concurrent task collision on the same execution_id (shouldn't
  happen under the engine's per-execution dispatch but the lock
  protects either way) → False on the loser; True on the winner.

#### Concurrent-restart edge case (revised v3 per architect Q5)

**Architect's Q5 ruling: not a v1 concern.** Workflow `execution_id`
is UUID-generated and globally unique; probabilistic uniqueness
makes inter-engine collision negligible. The engine's existing state
machine prevents the paused-then-terminated-then-new-collides path.
v-final retains the defensive flag here for record-keeping; no spec
change or implementation work is required.

The Q5 ruling is on the inter-engine collision case, not the
intra-engine crash-window case. The per-outcome transaction matrix
above addresses the latter; Q5 confirms the former is out of scope.

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
# True, not False from the targeted ON CONFLICT DO NOTHING):
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
        # v-final (Codex round 2 Medium 7): emit
        # workflow.friction_pattern_unclassified to mirror the
        # observer's _emit_pattern_unclassified path exactly.
        # Preserves audit trail; signals that the workflow step
        # failure was observed but did NOT match any active /
        # resolved / reactivated catalog pattern.
        await event_stream.emit(
            instance_id, "workflow.friction_pattern_unclassified",
            {
                "workflow_execution_id": workflow_execution_id,
                "step_index": step_index,
                "action_type": action_type,
                "signal_type": f"workflow_step:{action_type}:failed",
                "signal_description": record.user_visible_summary,
                "correlation_id": correlation_id,
                "member_id": self._member_id,
            },
            member_id=self._member_id,
        )
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
            member_id=self._member_id,
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
            member_id=self._member_id,
            emit_event=self._emit_event,  # for friction.pattern_recurrence
        )
    elif pattern.lifecycle_state == LIFECYCLE_ARCHIVED:
        # v-final (Codex round 2 Medium 7): emit the unclassified
        # event for archived-pattern matches too. The observer's
        # _classify_and_record only routes to record_occurrence /
        # record_recurrence for active / reactivated / resolved
        # states; archived patterns fall through its match path
        # because list_patterns excludes them. The workflow side
        # already filters at list_patterns, so reaching this branch
        # would indicate a future code path that surfaces archived
        # patterns explicitly. Emit unclassified for consistency.
        await event_stream.emit(
            instance_id, "workflow.friction_pattern_unclassified",
            {
                "workflow_execution_id": workflow_execution_id,
                "step_index": step_index,
                "action_type": action_type,
                "matched_pattern_id": pattern.pattern_id,
                "matched_pattern_state": pattern.lifecycle_state,
                "correlation_id": correlation_id,
                "member_id": self._member_id,
            },
            member_id=self._member_id,
        )
```

**`member_id` binding (Codex round 2 Medium 7 follow-on):** the
per-execution sink wrapper accepts `member_id` at construction time
(see Decision 3's `for_execution(execution, *, member_id="")` API).
The bound value is read on every call to `record_occurrence` /
`record_recurrence` / unclassified-emit so workflow-step occurrences
carry the same member-attribution discipline as the observer's
turn-scoped occurrences. Member identity is engine-bound — workflow
callers don't pass it on each append; the engine attributes the
execution to the member that triggered it.

**Classify-only-when-the-append-actually-inserted** (Codex High 3
follow-on): the classifier call lives INSIDE the
`if append_returned_true:` branch, NOT before. ON CONFLICT DO NOTHING
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

## Open architectural questions — v-final status (all locked)

The v1 spec body surfaced three open architectural questions. Codex
round 1 folded into v2. Architect made five calls 2026-05-11 that
folded into v3. Codex round 2 raised 8 implementation-surface
findings (no architectural challenges) folded into v-final. All
architectural decisions remain locked.

1. ✅ **Decision 1 deviation.** Codex agreed with preserve-schema;
   architect Q1 confirmed deviation is correct and promoted it to
   the canonical decision. Don't fold to `workflow_context` for v1.
   The load-bearing invariant: workflow-facing APIs return
   `WorkflowActionRecord` (not bare `ActionStateRecord`) OR the
   bare record carries a stable `receipt_refs` entry of the form
   `workflow:<execution_id>:step:<idx>`.
2. ✅ **FK target.** Codex initially chose composite UNIQUE.
   Architect Q2 ruled singular `execution_id` (UUID-generated;
   globally unique; FK on singular column sufficient). v3 Decision 2
   reflects the singular FK; no migration on `workflow_executions`
   required.
3. ✅ **`partial_state` for workflow steps.** Stays None for v1;
   future spec can extend if soak surfaces partial-completion
   semantics.
4. ✅ **`operation_class` mapping.** Codex + architect Q3 both
   confirmed `mutate` for `mark_state` and `append_to_ledger`.
   Vocabulary expansion deferred.
5. ✅ **`risk_level` derivation.** Codex flagged uniform-low as
   substrate-truth loss; architect Q4 ruled derive from
   `operation_class` (NOT `action_type` as v2 had). v3 Decision 5
   reflects the operation-class-based rule.
6. ✅ **Concurrent restart edge case.** Codex elevated to
   "load-bearing." Architect Q5 ruled NOT a v1 concern; UUID
   uniqueness + state-machine handles it. v3 Decision 6 pulled
   back the framing but left the atomic-boundary fix in place
   (different concern: single-engine crash-window, not inter-engine
   collision).
7. ✅ **Failure summary mechanical copy.** v2 Decision 5 already
   correct; v3 unchanged.

## Code-level shape

### File map (v-final)

- NEW: `kernos/kernel/workflows/action_sink.py` (~300 LOC after
  v-final folds): `WorkflowActionSink` class (borrowing-reference
  connection; schema-in-store via `ensure_schema()`);
  `WorkflowExecutionActionSink` per-execution wrapper with
  construction-time identity binding; `WorkflowActionRecord`
  dataclass for the storage-row shape; `_build_action_state_record`
  + `_risk_level_for_operation_class` + `_operation_class_for_call_tool`
  helpers; per-outcome SQL helpers (`_append_and_advance`,
  `_append_and_persist_gate_nonce`, `_append_and_abort`) for use
  inside `_run_workflow_txn`; classifier-hook integration with
  `FrictionPatternStore` (Decision 7) mirroring
  `FrictionObserver._classify_and_record` exactly including the
  `_emit_pattern_unclassified` analog.
- MODIFIED: `kernos/kernel/workflows/execution_engine.py`:
  - `_EXECUTIONS_SCHEMA` unchanged (no migration on
    `workflow_executions` required per architect Q2 — singular FK
    target sufficient).
  - `_ensure_schema` runs the new sink's schema migration after
    the existing `workflow_executions` ensure + ALTER migrations
    (schema setup order per Decision 3).
  - `WorkflowEngine.__init__` constructs the sink with the shared
    connection AND initializes `self._workflow_db_write_lock =
    asyncio.Lock()`. The sink is a borrowing reference.
  - NEW `_run_workflow_txn(body, *, retries=3, retry_backoff_ms=50)`
    method (Decision 3 / Codex round 2 High 3) that acquires the
    write-lock, issues BEGIN IMMEDIATE, runs the body, COMMITs or
    ROLLBACKs on exception, and retries on `database is locked`.
  - The step-execute loop body wraps each of the four outcomes
    (Decision 6 matrix) through `_run_workflow_txn(...)` with the
    matching per-outcome SQL helper. `workflow.*` event_stream
    emits move AFTER the transaction commits.
  - Engine startup adds the state-aware self-healing pass
    (Decision 6, Codex round 2 High 2) that inspects
    `record.execution_state`, `action.gate_ref`,
    `action.continuation_rules.on_failure`, and
    `execution.gate_nonce` before advancing.
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

### Step-execute wrap shape (illustrative pseudocode, v-final)

The step-execute loop body in `execution_engine.py` wraps each
outcome in the matching `_run_workflow_txn(...)` shape from
Decision 6:

```python
# Success path (after action_succeeded check passes):
record = _build_action_state_record(
    step_index=idx,
    action=action,
    result=result,
    execution_state="completed",
)
sink_ctx = execution.action_sink  # bound to (execution, member_id)
if action.gate_ref is not None:
    # Gated success: append record + persist gate_nonce; cursor
    # advances later, after gate release.
    await self._run_workflow_txn(
        lambda db: _append_and_persist_gate_nonce(
            db, sink_ctx, record,
            step_index=idx, action_type=action.action_type,
            gate_nonce=pending_gate_nonce,
        ),
    )
else:
    # Non-gated success: append record + advance cursor atomically.
    await self._run_workflow_txn(
        lambda db: _append_and_advance(
            db, sink_ctx, record,
            step_index=idx, action_type=action.action_type,
        ),
    )
await self._record_step_succeeded(execution, idx, action, result)
if action.gate_ref is not None:
    gate = gate_by_name[action.gate_ref]
    # gate_nonce already persisted inside the transaction above;
    # _persist_gate_nonce call removed from this path.
    cont = await self._await_gate(execution, gate)
    if not cont:
        return
    await self._clear_gate_nonce(execution)  # advances cursor inside
else:
    pass  # cursor already advanced inside the transaction

# Failure path (execute_raised, verifier_rejected, verify_raised):
error = _compute_failure_error(...)  # the single string Decision 5 names
record = _build_action_state_record(
    step_index=idx,
    action=action,
    error=error,
    execution_state="failed",
)
if action.continuation_rules.on_failure == "abort":
    # Aborting failure: append record + transition execution to
    # aborted state atomically. Cursor stays put.
    await self._run_workflow_txn(
        lambda db: _append_and_abort(
            db, sink_ctx, record,
            step_index=idx, action_type=action.action_type,
            aborted_reason=f"step_{idx}_{abort_reason_suffix}",
        ),
    )
    await self._record_step_failed(execution, idx, action, error=error)
    # _abort's post-commit responsibility: emit terminated event
    # (existing _abort already does this).
    return
# Continue-on-failure: append record + advance cursor atomically.
await self._run_workflow_txn(
    lambda db: _append_and_advance(
        db, sink_ctx, record,
        step_index=idx, action_type=action.action_type,
    ),
)
await self._record_step_failed(execution, idx, action, error=error)
# Loop continues.
```

The record is built BEFORE any event_stream emit so substrate truth
persists even if the emit fails. The reverse ordering (event first,
record second) would risk an event without a record on
emit-then-crash. The transaction commits before the emit happens;
the emit is best-effort after.

### `_build_action_state_record` helper

Constructs the record from the workflow step context:

- `action_id` — generated UUID prefix `act_` (mirrors note_this).
- `surface` — `"workflow_step"` (new surface value).
- `operation` — `action.action_type` (e.g., `notify_user`,
  `call_tool`, `write_canvas`).
- `operation_class` — derived from `action.action_type` per the
  verb-to-class table in Decision 5. Direct-effect verbs
  (`mark_state`, `append_to_ledger`) map to `mutate` (per architect
  Q3). `call_tool` is the special case: see the source-of-truth rule
  below.
- `authorization_state` — `"not_required"` UNIFORMLY for v1 (per
  Decision 5 / Codex round 1 High 2). Gate state stays on
  event_stream events, NOT on the record.
- `execution_state` — `completed` on success path; `failed` on
  failure path.
- `receipt_refs` — references to upstream tool results when
  applicable; empty for `mark_state` / `append_to_ledger`. ALSO
  includes a stable provenance entry of the form
  `workflow:<execution_id>:step:<idx>` so callers receiving a bare
  `ActionStateRecord` can route it back to the workflow context
  (Decision 1's escape-hatch invariant).
- `affected_objects` — substrate IDs the step touched (e.g., the
  ID from a `write_canvas` result's receipt). Empty for
  non-affecting verbs.
- `user_visible_summary` — on success:
  `f"workflow step {idx} ({action_type}) completed"`. On failure:
  the error string computed by `_compute_failure_error()` (the same
  string passed to `_record_step_failed`, per Decision 5).
- `risk_level` — derived from `operation_class` per Decision 5's
  table.
- `missing_metadata` — True when `call_tool` falls back to the
  default operation_class (see source rule below); False otherwise.

#### `call_tool` operation_class source (Codex round 2 Low 8)

`call_tool` wraps a tool invocation whose effect varies per tool.
v-final's source-of-truth rule:

1. **Tool registry lookup.** If the wrapped tool has a declared
   `operation_class` in the tool registry's metadata (the
   KERNEL-TOOL-REGISTRY-V1 catalog being prepared for C7), use that.
   Set `missing_metadata=False`.
2. **Default fallback.** If the registry has no declaration OR the
   wrapped tool isn't in the registry (e.g., a direct MCP tool
   without registry metadata), default to
   `operation_class="mutate"`, `risk_level="medium"`,
   `missing_metadata=True`.

The default biases conservative (`mutate` + `medium`) so audit
consumers never see an under-classified `read`-class default that
hides a substrate mutation. Renderer / audit consumers distinguish
"derived from registry" vs "default-because-unknown" via the
`missing_metadata` flag.

When KERNEL-TOOL-REGISTRY-V1 ships, the default-fallback rate should
drop to zero or near-zero for production tools; the
`missing_metadata=True` rate is a real signal that the registry has
gaps. v1 of this spec doesn't depend on the registry being complete;
the default keeps the substrate-fidelity contract intact.

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

### Resume across restart (v-final — per-outcome atomicity)

`tests/test_workflow_action_sink.py::TestResumeIdempotency`

1. **`test_targeted_on_conflict_returns_false_on_pk_collision`** —
   v-final Codex round 2 High 4: record a step; call append again
   with the same `(instance_id, workflow_execution_id, step_index)`;
   verify the second call returns False and the sink still has one
   row.
2. **`test_targeted_on_conflict_raises_on_non_pk_constraint`** —
   v-final Codex round 2 High 4: trigger a non-PK constraint
   failure (e.g., NULL `recorded_at`); verify it raises rather than
   being silently swallowed.
3. **`test_engine_restart_does_not_double_emit`** — simulate a
   workflow that completed step 0, then engine restart; verify
   step 0's record persists across restart AND the engine's
   re-enter-at-start_idx logic doesn't fire a second append for
   step 0.
4. **`test_atomic_boundary_non_gated_success`** — v-final Decision 6
   matrix row 1: simulate a crash after the success transaction
   commits (record + cursor both advanced); restart; verify the
   engine moves past the step without re-executing.
5. **`test_atomic_boundary_gated_success_persists_gate_nonce`** —
   v-final Decision 6 matrix row 2: simulate a crash AFTER the
   gated-success transaction commits (record persisted + gate_nonce
   set, cursor NOT advanced); restart; verify the engine re-enters
   `_await_gate` for the same step rather than advancing past it.
6. **`test_atomic_boundary_continue_failure`** — v-final Decision 6
   matrix row 3: simulate a crash after the continue-failure
   transaction commits; restart; verify the engine moves past the
   failed step to the next one.
7. **`test_atomic_boundary_aborting_failure_atomic`** — v-final
   Decision 6 matrix row 4: simulate a crash after the
   aborting-failure transaction commits; restart; verify the
   execution is in `state='aborted'` with the failed-step record
   present; the engine does not resume.
8. **`test_atomic_boundary_rolls_back_on_failure`** — v-final
   Decision 6: simulate a failure DURING the transaction (e.g., the
   second statement raises after the first); verify neither the
   record nor the cursor advance / gate persist / abort transition
   lands. On restart, the engine re-executes the step cleanly.
9. **`test_write_lock_serializes_concurrent_tasks`** — v-final
   Codex round 2 High 3: spawn two asyncio tasks racing to enter
   `_run_workflow_txn`; verify they serialize (no "cannot start a
   transaction within a transaction" error) and both transactions
   commit in order.
10. **`test_busy_retry_under_external_lock`** — v-final Codex round
    2 High 3: simulate a brief external locker on the workflow DB;
    verify `_run_workflow_txn` retries with exponential backoff and
    eventually succeeds within the retry budget.
11. **`test_self_heal_advances_for_completed_record`** — v-final
    Codex round 2 High 2: directly insert a `completed` record for
    step N WITHOUT advancing the cursor; start the engine; verify
    the WORKFLOW_CRASH_WINDOW_RECONCILE warning logs AND the cursor
    advances.
12. **`test_self_heal_skips_for_pending_gate`** — v-final Codex
    round 2 High 2: insert a `completed` record for step N on a
    gated step WITH `execution.gate_nonce` still set; start the
    engine; verify the WORKFLOW_CRASH_WINDOW_SKIP warning logs AND
    the cursor does NOT advance (restart logic will re-enter the
    gate wait).
13. **`test_self_heal_skips_for_aborting_failed`** — v-final Codex
    round 2 High 2: insert a `failed` record for step N on a step
    whose `continuation_rules.on_failure == "abort"`; start the
    engine; verify the WORKFLOW_CRASH_WINDOW_SKIP warning logs AND
    the cursor does NOT advance (restart logic will route to abort
    via the existing engine path).

### FRICTION-PATTERN composition (v-final — lifecycle-dispatch + unclassified)

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
3. **`test_workflow_step_failure_emits_unclassified_for_no_match`** —
   v-final Codex round 2 Medium 7: trigger a failing workflow step
   that matches NO catalog pattern; verify
   `workflow.friction_pattern_unclassified` event fires with the
   `signal_type`, `signal_description`, `member_id`, and
   correlation chain populated.
4. **`test_workflow_step_failure_skips_archived_pattern`** — v2 High
   3 / v-final Codex round 2 Medium 7: pre-seed an `archived`
   pattern; trigger a failing step that would match; verify the
   pattern's counters are NOT incremented (archived patterns aren't
   listed for matching). If a future code path forces an archived
   match, the unclassified event fires per Decision 7's archived
   branch.
5. **`test_idempotency_skip_does_not_double_count_friction`** — v2
   High 3 follow-on: trigger a workflow step that fires the friction
   hook once; restart and trigger the same step again (which
   ON-CONFLICT-DO-NOTHINGs); verify the friction pattern's
   `occurrence_count` reflects ONE increment, not two. The sink's
   classify-only-on-actual-insert discipline pins this.
6. **`test_workflow_step_success_does_not_fire_friction_hook`** —
   only failed steps fire the friction hook; successful steps don't.
7. **`test_friction_member_id_attribution`** — v-final Codex round 2
   Medium 7 follow-on: pre-seed an active pattern; trigger a failing
   workflow step where `execution.member_id = "mem_abc"`; verify the
   `record_occurrence` call carries `member_id="mem_abc"` (NOT the
   empty string, NOT the caller's argument).

### Sink context binding (v-final — Codex round 2 Medium 6)

`tests/test_workflow_action_sink.py::TestSinkContextBinding`

1. **`test_caller_cannot_override_instance_id`** — confirm that
   `WorkflowExecutionActionSink.append()` does NOT accept
   `instance_id` as an argument; only `record`, `step_index`,
   `action_type` are public parameters.
2. **`test_sink_binds_instance_id_from_execution`** — construct two
   sinks for two executions with different `instance_id`s; verify
   their `append()` calls write to the correct partition based on
   the execution's binding, not on any caller-supplied value.
3. **`test_member_id_bound_at_construction`** —
   `engine.action_sink_for(execution, member_id="mem_xyz")` binds
   the member; subsequent friction-hook calls carry `member_id="mem_xyz"`.

### `call_tool` operation_class derivation (v-final — Codex round 2 Low 8)

`tests/test_workflow_action_sink.py::TestCallToolOperationClassSource`

1. **`test_call_tool_with_registry_metadata_uses_declared_class`** —
   pre-register a tool with `operation_class="read"`; workflow step
   invokes `call_tool` for that tool; verify the record's
   `operation_class="read"`, `risk_level="low"`, `missing_metadata=False`.
2. **`test_call_tool_without_registry_metadata_defaults_to_mutate`** —
   workflow step invokes `call_tool` for a tool with no registry
   metadata; verify the record's `operation_class="mutate"`,
   `risk_level="medium"`, `missing_metadata=True`.
3. **`test_call_tool_default_preserves_audit_trail`** —
   `missing_metadata=True` doesn't suppress any other field;
   `affected_objects` and `receipt_refs` still populate from the
   step's result.

## Risks and design constraints

| Risk | Mitigation |
|---|---|
| Schema migration on `workflow_executions` | NONE required (architect Q2 ruled singular FK target is sufficient; the new table references the existing `execution_id` PK directly). |
| Record-build cost on every step (extra DB write per workflow step) | Workflow steps are already DB-bound (event_stream write + ledger append). One transaction with two writes is amortized; v1 accepts the cost. |
| Resume-idempotency hiding re-execution (Codex round 1 Blocker / round 2 Blocker 1) | v-final Decision 6 per-outcome transaction matrix: record append plus the matching state mutation (cursor advance OR gate_nonce persist OR abort transition) commit atomically. Restart inspects state on a per-outcome basis. |
| Gated success cursor-advance bypassing approval (Codex round 2 Blocker 1) | v-final Decision 6 matrix row 2: gated success does NOT advance cursor inside the success transaction. Cursor advance is deferred to `_clear_gate_nonce` on gate release. |
| Aborting failure promoted to continuation on restart (Codex round 2 Blocker 1) | v-final Decision 6 matrix row 4: aborting failure commits the `state='aborted'` transition in the same transaction as the record append. Restart never sees a "running" execution with a failed-abort record. |
| Concurrent asyncio tasks racing BEGIN IMMEDIATE on the shared connection (Codex round 2 High 3) | v-final engine-level `asyncio.Lock` + `_run_workflow_txn()` helper. The lock is acquired before BEGIN IMMEDIATE; rollback + bounded busy-retry handled inside the helper. |
| Bare `INSERT OR IGNORE` swallowing non-PK constraint failures (Codex round 2 High 4) | v-final targeted `ON CONFLICT(instance_id, workflow_execution_id, step_index) DO NOTHING`. Other constraint failures raise normally. |
| Self-heal too broad — skipping gates or promoting aborts (Codex round 2 High 2) | v-final state-aware self-heal: only advances cursor when the latest record is `completed` (no gate_ref, no gate_nonce) OR `failed` with `on_failure != "abort"`. All other cases log SKIP and rely on the restart logic. |
| `instance_id` cross-partition leakage via caller-supplied argument (Codex round 2 Medium 6) | v-final writer invariant: `WorkflowExecutionActionSink` binds `instance_id` from the parent `WorkflowExecution`; no caller can override. |
| Friction unclassified path missing on workflow side (Codex round 2 Medium 7) | v-final Decision 7: emits `workflow.friction_pattern_unclassified` for no-match and explicit-archived cases. `member_id` bound from the execution. |
| `call_tool` operation_class fabricated or under-classified (Codex round 2 Low 8) | v-final Decision 5 source rule: tool-registry-declared class when available; otherwise default `mutate` / `medium` / `missing_metadata=True`. |
| Action-record-construction failure (e.g., enum validation) | Failure in `_build_action_state_record` MUST not abort the workflow step. Wrap in try/except; on failure, log loud and skip the record. Preserves the workflow-runs-even-if-audit-fails invariant. |
| Friction-pattern double-counting on resume-retry (Codex round 1 High 3) | Decision 7: friction hook only fires when the sink append actually inserted (returned True), not on ON-CONFLICT-DO-NOTHING skip. |
| Friction-pattern wrong-method dispatch on resolved patterns (Codex round 1 High 3) | Decision 7: workflow-side friction hook mirrors `FrictionObserver._classify_and_record`; dispatches by `lifecycle_state` to `record_occurrence` / `record_recurrence` / unclassified-emit. |
| `authorization_state="confirmed"` claim on gated steps that haven't released yet (Codex round 1 High 2) | Decision 5: ALL workflow step records use `authorization_state="not_required"`. Gate state stays on `workflow.execution_paused_at_gate` / `.execution_resumed` events. |
| Risk-level uniformity loses substrate truth (Codex round 1 Medium 6) | Decision 5: `risk_level` derived from `operation_class` (per architect Q4) with `missing_metadata=True` flag when `call_tool` operation_class falls back to the default. |
| Failure-summary drift between record and event (Codex round 1 Low 7) | Decision 5: error string computed once and passed to both `_build_action_state_record` and `_record_step_failed`. |
| Cross-instance leakage | Composite PK includes `instance_id`. Per-instance scoping by construction. |
| Conflict with future renderer changes | ActionStateRecord shape unchanged → renderer changes are independent of this spec. |

## Sequence (per architect directive)

1. ✅ Architect-framed (Notion `35cffafef4db81c38131ef967cde367c`).
2. ✅ CC drafts spec at `specs/ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1.md`
   on branch `actionstaterecord-workflow-composition-v1`
   (commit `8fc53dd` v1 / `c5af8a6` v1 + context doc).
3. ✅ **Codex pre-spec review round 1** — caught one blocker
   (resume idempotency hides re-execution), two high (gate
   authorization semantics; friction dispatch by lifecycle), three
   medium (Decision 1 rationale; connection ownership; risk_level
   uniformity), one low (failure summary drift). Plus surfaced four
   open architectural questions.
4. ✅ **CC folds Codex round 1** into spec body (v2 at `062cf19`).
5. ✅ **Architect makes five calls on v2's open questions
   2026-05-11**: preserve schema (Q1); singular FK target (Q2);
   `operation_class` mutate for direct-effect verbs (Q3);
   `risk_level` derived from `operation_class` (Q4); concurrent
   restart NOT a v1 concern (Q5).
6. ✅ **CC folds architect's five calls** into spec body — this v3
   revision.
7. ✅ **Codex pre-spec review round 2** (8 findings: 1 blocker, 3
   high, 3 medium, 1 low — all implementation-surface, no
   architectural decisions challenged).
8. ✅ **CC folds Codex round 2** into v-final — this revision.
9. 🟡 Architect ratification of v-final (same-turn after CC pings
   per pipeline compression).
10. CC implements per ratified spec.
11. Codex post-implementation review.
12. CC any final changes.
13. Architect ratifies on close.

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
