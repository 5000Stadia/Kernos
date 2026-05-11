# ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1 — Substrate Context

**Purpose:** Self-contained code excerpts that the main spec
(`ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1.md`) references via
`kernos/...` paths. Codex's review needs these visible without
reaching outside `specs/`.

Files quoted here are copies-with-context for review only. The
authoritative source remains in the repo at the cited paths; if a
quote drifts from the source, the source wins.

---

## ActionStateRecord schema

**Source:** `kernos/kernel/integration/briefing.py:1100-1240`.

### Vocabulary constants

```python
ACTION_OPERATION_CLASSES: frozenset[str] = frozenset({
    "read", "propose", "mutate", "delete",
    "send", "schedule", "register", "manage",
})
ACTION_AUTHORIZATION_STATES: frozenset[str] = frozenset({
    "requested", "confirmed", "denied", "not_required",
})
ACTION_EXECUTION_STATES: frozenset[str] = frozenset({
    "not_attempted", "attempted", "completed",
    "partial", "blocked", "failed", "unknown",
})
ACTION_RISK_LEVELS: frozenset[str] = frozenset({"low", "medium", "high"})
ACTION_EVIDENCE_CLASSES: frozenset[str] = frozenset({
    "", "search_hit", "page_read", "memory_entry",
    "inferred", "missing", "unverified",
})
```

### Dataclass

```python
@dataclass(frozen=True)
class ActionStateRecord:
    """Substrate-authoritative record of an action surface event.

    RESPONSE-FIDELITY-V1 Batch 1 (2026-05-08): the structured envelope
    that bridges substrate truth and renderer language.
    """

    action_id: str
    surface: str
    operation: str
    operation_class: str
    authorization_state: str
    execution_state: str
    receipt_refs: tuple[str, ...] = ()
    affected_objects: tuple[str, ...] = ()
    partial_state: dict | None = None
    user_visible_summary: str = ""
    risk_level: str = "low"
    evidence_class: str = ""
    missing_metadata: bool = False

    def __post_init__(self) -> None:
        # Strict validation against the vocabulary constants above.
        # Every existing producer must already pass these checks; adding
        # a new field requires updating every producer.
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise BriefingValidationError(
                "ActionStateRecord.action_id must be a non-empty string"
            )
        if not isinstance(self.surface, str) or not self.surface.strip():
            raise BriefingValidationError(
                "ActionStateRecord.surface must be a non-empty string"
            )
        if not isinstance(self.operation, str) or not self.operation.strip():
            raise BriefingValidationError(
                "ActionStateRecord.operation must be a non-empty string"
            )
        if self.operation_class not in ACTION_OPERATION_CLASSES:
            raise BriefingValidationError(...)
        if self.authorization_state not in ACTION_AUTHORIZATION_STATES:
            raise BriefingValidationError(...)
        if self.execution_state not in ACTION_EXECUTION_STATES:
            raise BriefingValidationError(...)
        if self.risk_level not in ACTION_RISK_LEVELS:
            raise BriefingValidationError(...)
        if self.evidence_class not in ACTION_EVIDENCE_CLASSES:
            raise BriefingValidationError(...)
        if self.partial_state is not None and not isinstance(self.partial_state, dict):
            raise BriefingValidationError(...)
        # Substrate-fidelity: partial state requires structured partial_state
        if self.execution_state == "partial" and self.partial_state is None:
            raise BriefingValidationError(
                "ActionStateRecord.partial_state is required when "
                "execution_state=='partial'"
            )
        if not isinstance(self.missing_metadata, bool):
            raise BriefingValidationError(...)
```

**Decision-1-relevant facts:**

- Frozen dataclass with strict `__post_init__` validation against five enum constants.
- 13 fields total.
- Adding a 14th `workflow_context: dict | None = None` field is a substrate-wide schema edit: every existing record producer (note_this, coding_session_bridge, integration runner, etc.) would need to either populate it or pass through. The validator changes are simpler; the cross-producer audit is the load-bearing cost.

---

## workflow_executions schema (existing)

**Source:** `kernos/kernel/workflows/execution_engine.py:210-232`.

```sql
CREATE TABLE IF NOT EXISTS workflow_executions (
    execution_id            TEXT PRIMARY KEY,         -- singular PK
    workflow_id             TEXT NOT NULL,
    instance_id             TEXT NOT NULL,
    correlation_id          TEXT NOT NULL,
    state                   TEXT NOT NULL,
    action_index_completed  INTEGER DEFAULT -1,
    intermediate_state      TEXT DEFAULT '{}',
    last_heartbeat          TEXT DEFAULT '',
    aborted_reason          TEXT DEFAULT '',
    started_at              TEXT NOT NULL,
    terminated_at           TEXT DEFAULT '',
    trigger_event_payload   TEXT DEFAULT '{}',
    trigger_event_id        TEXT DEFAULT '',
    member_id               TEXT DEFAULT '',
    gate_nonce              TEXT DEFAULT '',
    fire_id                 TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_executions_state
    ON workflow_executions(instance_id, state);
```

**Open-question #2 background:** the PK is singular `execution_id`. The proposed composite FK from `workflow_action_records` on `(instance_id, execution_id)` requires either:

(a) Adding a `CREATE UNIQUE INDEX ... ON workflow_executions(instance_id, execution_id)` so SQLite accepts the composite FK target. Backward-compatible (existing rows satisfy the composite uniqueness because `execution_id` is already PK).

(b) Dropping the FK and relying on tool-implementation discipline (the engine only writes rows for executions it just created).

(c) Targeting only `execution_id` in the FK (drop `instance_id` from the FK columns). Then `workflow_action_records` still carries `instance_id` for scoping but the FK target is the existing singular PK.

### Existing migration pattern (gate_nonce, fire_id)

```python
async def _ensure_schema(db: aiosqlite.Connection) -> None:
    for stmt in _EXECUTIONS_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            await db.execute(stmt)
    # Race-tolerant ALTER migration: two engine initializers running
    # concurrently can both observe the column absent and both attempt
    # ALTER. Catch "duplicate column name" specifically.
    async with db.execute(
        "SELECT name FROM pragma_table_info('workflow_executions')"
    ) as cur:
        existing_columns = {row[0] for row in await cur.fetchall()}
    if "gate_nonce" not in existing_columns:
        try:
            await db.execute(
                "ALTER TABLE workflow_executions "
                "ADD COLUMN gate_nonce TEXT DEFAULT ''"
            )
        except aiosqlite.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    # (Same pattern for fire_id...)
```

This is the precedent for any schema migration on `workflow_executions`. Adding a composite UNIQUE index follows the same idempotent-on-retry pattern.

---

## Workflow step-execute loop body

**Source:** `kernos/kernel/workflows/execution_engine.py:577-660`.

The body of `WorkflowEngine._run_steps()` (renamed in spec body as the step-execute loop) iterates `action_sequence` and dispatches each verb. **This is the wrap site for Option A.**

```python
gate_by_name = {g.gate_name: g for g in wf.approval_gates}
start_idx = max(0, execution.action_index_completed + 1)
for idx in range(start_idx, len(wf.action_sequence)):
    action = wf.action_sequence[idx]
    verb = self._action_library.get(action.action_type)
    # Nonce minted BEFORE the gated action executes so the action's
    # payload can carry it. Held in a local until the action completes
    # successfully; if the action fails or aborts, the unused nonce is
    # discarded and no pause is entered.
    pending_gate_nonce = (
        str(uuid.uuid4()) if action.gate_ref is not None else ""
    )
    interp_ctx = {
        "execution_id": execution.execution_id,
        "gate_nonce": pending_gate_nonce,
        "correlation_id": execution.correlation_id,
        "workflow_id": execution.workflow_id,
        "instance_id": execution.instance_id,
    }
    interpolated_params = _interpolate_params(action.parameters, interp_ctx)
    try:
        result = await verb.execute(context, interpolated_params)
    except Exception as exc:
        # Failure path A: execute raised.
        await self._record_step_failed(
            execution, idx, action,
            error=f"execute_raised:{type(exc).__name__}:{exc}",
        )
        await self._abort(
            execution, f"step_{idx}_raised:{type(exc).__name__}",
        )
        return
    verified = False
    try:
        verified = await verb.verify(context, interpolated_params, result)
    except Exception as exc:
        logger.warning(
            "VERIFY_RAISED execution_id=%s step=%s error=%s",
            execution.execution_id, idx, exc,
        )
    action_succeeded = result.success and verified
    if not action_succeeded:
        # Failure path B: verifier rejected or result.success=False.
        await self._record_step_failed(
            execution, idx, action,
            error=result.error or "verifier_rejected",
        )
        if action.continuation_rules.on_failure == "abort":
            await self._abort(execution, f"step_{idx}_failed")
            return
        # continue/retry path
        await self._mark_step_complete(execution, idx)
        continue
    # Success path.
    await self._record_step_succeeded(execution, idx, action, result)
    # Approval-gate handling: action FIRST (already executed above and
    # succeeded), pause AFTER.
    if action.gate_ref is not None:
        gate = gate_by_name[action.gate_ref]
        # ... gate wait + resume logic ...
    await self._mark_step_complete(execution, idx)
# All steps done — mark completed.
await self._complete(execution)
```

**Spec body Decision 5 + Decision 6 wrap sites:**

- After `result = await verb.execute(...)` in the try block (success path lands at the `_record_step_succeeded` call).
- Before `_record_step_failed` in the `except Exception` block (failure path A: `execute` raised).
- Before `_record_step_failed` in the `if not action_succeeded:` block (failure path B: verifier rejected).

All three sites build an ActionStateRecord and append via the sink **before** the existing `_record_step_succeeded` / `_record_step_failed` call (so the record persists even if the event_stream emit fails).

---

## Existing event_stream emit methods (workflow.* events)

**Source:** `kernos/kernel/workflows/execution_engine.py:864-915`.

These are the existing emit sites that the spec leaves untouched and runs alongside.

```python
async def _record_step_succeeded(
    self,
    execution: WorkflowExecution,
    idx: int,
    action: ActionDescriptor,
    result: ActionResult,
) -> None:
    await event_stream.emit(
        execution.instance_id, "workflow.execution_step_succeeded",
        {"execution_id": execution.execution_id,
         "step_index": idx,
         "action_type": action.action_type},
        correlation_id=execution.correlation_id,
    )
    if self._ledger is not None:
        try:
            await self._ledger.append(
                execution.instance_id, execution.workflow_id,
                {"execution_id": execution.execution_id,
                 "step_index": idx,
                 "agent_or_action": action.action_type,
                 "synopsis": f"{action.action_type} succeeded",
                 "result_summary": "success",
                 "kickback_if_any": ""},
            )
        except Exception as exc:
            logger.warning("LEDGER_APPEND_FAILED %s", exc)


async def _record_step_failed(
    self,
    execution: WorkflowExecution,
    idx: int,
    action: ActionDescriptor,
    *,
    error: str,
) -> None:
    await event_stream.emit(
        execution.instance_id, "workflow.execution_step_failed",
        {"execution_id": execution.execution_id,
         "step_index": idx,
         "action_type": action.action_type,
         "error": error},
        correlation_id=execution.correlation_id,
    )
    if self._ledger is not None:
        try:
            await self._ledger.append(...)  # similar
        except Exception as exc:
            logger.warning("LEDGER_APPEND_FAILED %s", exc)
```

The `error` field uses the format `execute_raised:ExceptionClass:msg` for execute-raised failures and `verifier_rejected` (or `result.error` if populated) for verifier-rejected failures. Decision 5 in the spec body reuses this string format for the ActionStateRecord's `user_visible_summary` so audit chains compose without extra translation.

---

## Action verb classification

**Source:** `kernos/kernel/workflows/action_classification.py:20-33`.

```python
WORLD_EFFECT_VERBS = frozenset({
    "notify_user",
    "write_canvas",
    "route_to_agent",
    "call_tool",
    "post_to_service",
})

DIRECT_EFFECT_VERBS = frozenset({
    "mark_state",
    "append_to_ledger",
})

KNOWN_ACTION_TYPES = WORLD_EFFECT_VERBS | DIRECT_EFFECT_VERBS
```

**Open-question #3 background:** Decision 5's `operation_class` mapping needs a value from `ACTION_OPERATION_CLASSES` (`read / propose / mutate / delete / send / schedule / register / manage`). World-effect verbs map cleanly to `mutate` (or `send` for `notify_user`, `register` for `route_to_agent` — defensible alternatives). Direct-effect verbs (`mark_state`, `append_to_ledger`) don't fit any current value cleanly; the spec maps them to `mutate` for v1 with a flag that a future enum extension could refine.

### ActionResult shape

**Source:** `kernos/kernel/workflows/action_library.py:75-86`.

```python
@dataclass
class ActionResult:
    """Uniform return shape for verb execution. Verifier reads
    ``success`` and (for world-effect verbs) cross-checks the
    receipt against the wrapped surface to confirm intent-
    satisfaction."""

    success: bool
    value: Any = None
    error: str | None = None
    receipt: dict = field(default_factory=dict)
```

The `receipt` dict carries the substrate IDs the spec maps to `affected_objects` on the ActionStateRecord. The `error` field is the source of the `user_visible_summary` failure_reason when verifier rejects.

---

## Existing turn-scoped action-record sink pattern

**Source:** `kernos/kernel/reasoning.py:255-302, 525-532, 994-1057`.

This is the precedent the spec extends to workflow scope. Turn-scoped actions append to `self._turn_action_records` (a list); the integration runner drains it at turn-end.

### Construction

```python
def __init__(
    self,
    ...
    action_record_sink: list | None = None,  # RESPONSE-FIDELITY-V1 Batch 1.3
):
    # When ``action_record_sink`` is injected at construction
    # (production wiring; mirrors trace_sink pattern), the runner's
    # peek-callable reads from the same backing list so records
    # land on Briefing.audit_trace.action_state_records.
    self._turn_action_records: list = (
        action_record_sink if action_record_sink is not None else []
    )
```

### Drain (per turn)

```python
def drain_turn_action_records(self) -> list:
    """DRAINS (this method, clear-on-read) at turn end to populate
    TurnContext.action_state_records for the conv-log 'Action state
    this turn' block. Two readers, one shared list — same pattern as
    trace_sink."""
    records = list(self._turn_action_records)
    self._turn_action_records.clear()
    return records
```

### Append in dispatch (note_this example)

```python
elif tool_name == "note_this":
    from kernos.kernel.note_this import handle_note_this
    summary, record = await handle_note_this(
        state=self._state,
        instance_id=request.instance_id,
        member_id=getattr(request, "member_id", "") or "",
        ...
    )
    self._turn_action_records.append(record)
    return summary
```

**Workflow-scoped equivalent (proposed in spec Decision 3):**

The workflow execution has no `ctx` and no turn boundary; the list-based ephemeral pattern doesn't fit. The `WorkflowActionSink` plays the same role but persists to SQLite so records survive engine restarts and outlive any single execution. The per-execution wrapper carries the bound `(instance_id, workflow_execution_id)` so the engine doesn't pass them on every call.

---

## FRICTION-PATTERN-STABLE-IDS-V1 composition hook

**Source:** `kernos/kernel/friction_patterns.py:FrictionPatternStore.record_occurrence` (merged on `main` at `452dbee`).

```python
async def record_occurrence(
    self,
    *,
    instance_id: str,
    pattern_id: str,
    observed_at: str,
    report_path: str = "",
    classifier_score: float = 0.0,
    classified_by: str = "auto-signal-type",
    space_id: str = "",
    member_id: str = "",
) -> None:
    """Record an occurrence on an active or reactivated pattern.

    Rejects on resolved (caller must use record_recurrence) or
    archived (raises PatternArchived). Idempotent on (instance_id,
    report_path) via the partial UNIQUE index.
    """
```

**Composition story:** workflow step failures with a matching `signal_type` become observable to the friction pattern catalog when this spec lands. The integration probe in Decision 3's test category covers it: pre-seed a `FrictionPattern` whose `signal_type_keys` matches the workflow step's failure signature; trigger a failing step; verify `occurrence_count` increments.

The hook fires from `WorkflowActionSink.append()` when the appended record has `execution_state == "failed"` — extract the failure_reason prefix from `user_visible_summary`, classify against the catalog, call `record_occurrence` if a pattern matches. Mirrors the existing FrictionObserver `_classify_and_record` shape in `kernos/kernel/friction.py`.

---

## Reference spec — FRICTION-PATTERN-STABLE-IDS-V1 (Spec 1)

The spec at `specs/FRICTION-PATTERN-STABLE-IDS-V1.md` is the worked example for the catalog convention this spec mirrors:

- Schema-in-store via `ensure_schema()` (no separate migrations dir).
- `PRAGMA foreign_keys=ON` mandatory on every connection.
- `BEGIN IMMEDIATE` + bounded retry on `SQLITE_BUSY` for concurrent writers.
- `ON DELETE RESTRICT` on the composite FK (no destructive deletions).
- `INSERT OR IGNORE` for resume-safe idempotency on PK collision.

The spec body's Decisions 2 (storage), 5 (failure shape), and 6 (resume-safe) all reuse this convention. Codex should verify these are consistent across the two specs.

---

## Reference spec — CODING-SESSION-BRIDGE-V1 (Spec 2)

The spec at `specs/CODING-SESSION-BRIDGE-V1.md` is the worked example for the ActionStateRecord + tool-handler pattern this spec extends to workflow scope:

- Handler returns `tuple[str, ActionStateRecord]`.
- Caller (`execute_tool` elif chain) appends the record to `self._turn_action_records`.
- Validation failure produces `execution_state="failed"` with `user_visible_summary` carrying the reason.

The spec body's Decision 5 (failure shape) mirrors Spec 2's handler convention for the workflow-step case.

---

## Open questions worth Codex's eye

The main spec body surfaces five open architectural questions. The four most-likely-load-bearing are:

1. **Decision 1 deviation** (preserve schema vs extend with `workflow_context`). Tradeoff: cross-producer audit cost (extend) vs storage-row indirection (preserve). Spec body chose preserve; Codex's call.
2. **FK target on `workflow_executions`** (composite UNIQUE index vs drop FK vs target singular execution_id only). Spec body chose composite UNIQUE; alternatives (b) and (c) above are reasonable.
3. **`operation_class` for `mark_state` / `append_to_ledger`** (no clean fit in current vocabulary; v1 maps to `mutate`).
4. **`risk_level` per workflow step** (v1 uses `low` uniformly; a `route_to_agent` posting publicly is higher-risk than a `mark_state`).

The fifth (#5 concurrent restart edge case) is a defensive flag rather than a real blocker.
