"""Workflow execution engine — runs workflows in the background.

WORKFLOW-LOOP-PRIMITIVE C5.

Triggers fire via ``TriggerRegistry``'s match listener; the engine
attaches one and translates each (Trigger, Event) into a
``WorkflowExecution`` record persisted to SQLite and enqueued on an
in-process asyncio queue. A background task drains the queue,
executing one workflow at a time. The workflow runs as an
ACTION-LOOP-PRIMITIVE shape: intent (trigger event payload), gather
(active spaces + synthetic context), action (run the action sequence
via the action library), verify (per-step verifier + workflow-level
verifier), decide (complete / abort / retry).

Synthetic CohortContext-equivalent (the design review edit, narrow review): the
context constructed at execution start matches the shipped
``CohortContext`` shape — ``instance_id``, ``member_id``,
``user_message`` (synthetic placeholder describing the trigger
event), ``conversation_thread`` (empty tuple), ``active_spaces``
(resolved by the engine's space resolver), ``turn_id`` (synthetic
``"workflow:"`` + execution_id), ``produced_at``. Kick-back
trigger fires if active-space resolution fails for an instance —
the engine emits a kickback event and aborts the execution rather
than running covenant-blind.

Approval gates: when an action descriptor references a gate, the
engine first **executes the action**, then **pauses AFTER** waiting
for an approval event matching the gate's predicate. Per the spec's
"action first → pause AFTER → wait → resume" semantics. Timeout
behaviour is set per gate descriptor:

  - ``abort_workflow``: emit terminated(reason=gate_timeout); end.
  - ``escalate_to_owner``: emit owner_escalation event; abort.
  - ``auto_proceed_with_default``: continue with the gate's
    default_value; safe-deny enforcement at workflow registration
    prevents any irreversible downstream action.

Restart-resume: ``workflow_executions`` SQLite table records the
state of every execution. On engine start, executions in
``running`` state are inspected; if the next-to-run action is
``resume_safe``, the execution is re-enqueued at that step;
otherwise it's aborted with ``aborted_by_restart``. Default
``resume_safe = False`` — conservative.

Audit events emitted to event_stream:

  - ``workflow.execution_started``
  - ``workflow.execution_step_succeeded``
  - ``workflow.execution_step_failed``
  - ``workflow.execution_paused_at_gate`` (entered approval gate;
    payload carries gate_nonce for engine-bound match logic per
    WLP-GATE-SCOPING C1)
  - ``workflow.execution_resumed`` (gate satisfied)
  - ``workflow.gate_auto_proceeded`` (timeout with default value)
  - ``workflow.owner_escalation`` (timeout with escalate behavior)
  - ``workflow.execution_terminated``

All carry the execution's ``correlation_id`` so audit chains compose
with the rest of Kernos's event taxonomy.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiosqlite

from kernos.kernel import event_stream
from kernos.kernel.cohorts.descriptor import (
    CohortContext,
    ContextSpaceRef,
)
from kernos.kernel.event_stream import Event
from kernos.kernel.workflows.action_library import (
    ActionLibrary,
    ActionResult,
)
from kernos.kernel.integration.briefing import ActionStateRecord
from kernos.kernel.workflows.action_sink import (
    EventStreamEmitter,
    ToolOperationClassLookup,
    WorkflowActionSink,
    WorkflowExecutionActionSink,
    _append_and_abort,
    _append_and_advance,
    _append_and_persist_gate_nonce,
    _build_action_state_record,
    ensure_workflow_action_records_schema,
)
from kernos.kernel.workflows.ledger import WorkflowLedger
from kernos.kernel.workflows.predicates import evaluate as evaluate_predicate
from kernos.kernel.workflows.trigger_registry import Trigger, TriggerRegistry
from kernos.kernel.workflows.workflow_registry import (
    ActionDescriptor,
    ApprovalGate,
    Workflow,
    WorkflowRegistry,
)

if False:  # TYPE_CHECKING — avoid circular import at module load
    from kernos.kernel.friction_patterns import FrictionPatternStore

logger = logging.getLogger(__name__)


# Active-space resolver. The engine calls this to populate the
# synthetic CohortContext.active_spaces tuple. Real implementations
# read ContextSpace by instance_id; tests inject a stub.
ActiveSpaceResolver = Callable[[str], Awaitable[tuple[ContextSpaceRef, ...]]]


# ---------------------------------------------------------------------------
# WorkflowExecution record
# ---------------------------------------------------------------------------


@dataclass
class WorkflowExecution:
    execution_id: str
    workflow_id: str
    instance_id: str
    correlation_id: str
    state: str  # queued | running | completed | aborted
    action_index_completed: int = -1
    intermediate_state: dict = field(default_factory=dict)
    last_heartbeat: str = ""
    aborted_reason: str = ""
    started_at: str = ""
    terminated_at: str = ""
    trigger_event_payload: dict = field(default_factory=dict)
    trigger_event_id: str = ""
    member_id: str = ""
    # WLP-GATE-SCOPING C1: gate_nonce is set after a gate_ref action
    # completes successfully and persisted while the execution waits
    # for approval. Cleared by ``_clear_gate_nonce`` on successful
    # resume (including auto_proceed_with_default). Aborted/timed-out
    # executions are terminated rather than cleared, so the column
    # may remain populated on terminated rows — match logic only
    # consults the in-memory waiter table, never SQL nonce values,
    # so this is hygienic debt rather than a wake vector.
    gate_nonce: str = ""
    # WTC v1 C1 (the design review must-fix): fire_id is the application-layer
    # idempotency key supplied by the unified trigger runtime when
    # dispatching through the outbox. Empty for the legacy
    # _on_trigger_match path. The partial unique index on the
    # workflow_executions table catches duplicate execute_workflow
    # calls with the same fire_id and lets us return the original
    # execution_id without creating a second row.
    fire_id: str = ""

    def to_row(self) -> tuple:
        return (
            self.execution_id, self.workflow_id, self.instance_id,
            self.correlation_id, self.state, self.action_index_completed,
            json.dumps(self.intermediate_state), self.last_heartbeat,
            self.aborted_reason, self.started_at, self.terminated_at,
            json.dumps(self.trigger_event_payload), self.trigger_event_id,
            self.member_id, self.gate_nonce, self.fire_id,
        )

    @classmethod
    def from_row(cls, row) -> "WorkflowExecution":
        try:
            intermediate = json.loads(row["intermediate_state"]) or {}
        except Exception:
            intermediate = {}
        try:
            payload = json.loads(row["trigger_event_payload"]) or {}
        except Exception:
            payload = {}
        # gate_nonce was added by WLP-GATE-SCOPING C1; rows from the
        # original WLP schema may not have the column populated. Fall
        # back to "" so older rows present consistently.
        try:
            gate_nonce = row["gate_nonce"] or ""
        except (KeyError, IndexError):
            gate_nonce = ""
        # WTC v1 C1: fire_id added for outbox idempotency. Rows from
        # before the migration get "" (legacy in-process Trigger-
        # matched executions don't carry an outbox fire_id).
        try:
            fire_id = row["fire_id"] or ""
        except (KeyError, IndexError):
            fire_id = ""
        return cls(
            execution_id=row["execution_id"],
            workflow_id=row["workflow_id"],
            instance_id=row["instance_id"],
            correlation_id=row["correlation_id"],
            state=row["state"],
            action_index_completed=row["action_index_completed"],
            intermediate_state=intermediate,
            last_heartbeat=row["last_heartbeat"] or "",
            aborted_reason=row["aborted_reason"] or "",
            started_at=row["started_at"] or "",
            terminated_at=row["terminated_at"] or "",
            trigger_event_payload=payload,
            trigger_event_id=row["trigger_event_id"] or "",
            member_id=row["member_id"] or "",
            gate_nonce=gate_nonce,
            fire_id=fire_id,
        )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


# WTC v1 C1 (the design review must-fix): the ``fire_id`` column is the
# application-layer idempotency key for cross-process WLP dispatch
# dedup. Empty for legacy in-process Trigger-matched executions
# (those don't carry an outbox fire_id). Non-empty values are
# unique by partial index below — the legacy path with empty
# fire_id is exempt from the constraint by design. The schema
# string deliberately holds no SQL line comments (``--``) because
# this module's split-by-``;`` evaluator would mis-handle a
# semicolon embedded in comment prose. Keep the documentation in
# Python comments; keep the SQL minimal.
_EXECUTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_executions (
    execution_id            TEXT PRIMARY KEY,
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
"""
# WTC v1 C1 (the design review must-fix): the partial unique index on fire_id is
# DELIBERATELY NOT in the schema string — legacy DBs without the
# fire_id column would fail at this CREATE INDEX before the
# migration block below has a chance to ALTER the column in. The
# migration block creates the index after the ALTER, so both
# fresh installs and migrating-from-legacy take the same path.


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    for stmt in _EXECUTIONS_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            await db.execute(stmt)
    # WLP-GATE-SCOPING C1: explicit ALTER migration for the gate_nonce
    # column. CREATE TABLE IF NOT EXISTS does not add the column to a
    # pre-existing workflow_executions table from the WLP batch, so we
    # check the column list and add it if absent. Pre-existing rows
    # have an empty gate_nonce until their next pause event.
    #
    # Race tolerance (Codex C1 review iteration): two engine
    # initializers running concurrently can both observe the column
    # absent, then both attempt ALTER. SQLite raises OperationalError
    # ("duplicate column name") on the loser. Catch that specific
    # case and treat as benign — by the time we caught it, the
    # column exists, which is exactly what we wanted.
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

    # WTC v1 C1 (the design review must-fix): same idempotent ALTER pattern for
    # fire_id. Pre-existing executions get an empty fire_id and are
    # therefore exempt from the partial unique index — only outbox-
    # driven dispatch (execute_workflow) populates the column.
    if "fire_id" not in existing_columns:
        try:
            await db.execute(
                "ALTER TABLE workflow_executions "
                "ADD COLUMN fire_id TEXT DEFAULT ''"
            )
        except aiosqlite.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    # The partial unique index gets created by the CREATE TABLE
    # block above on fresh installs. For previously-installed DBs
    # that didn't have the index (because the column didn't exist),
    # CREATE UNIQUE INDEX IF NOT EXISTS is the idempotent path.
    #
    # Fail-closed posture (Codex mid-batch fold #1): the WTC v1
    # fire_id idempotency invariant depends on this partial unique
    # index catching SELECT-then-INSERT races. Without it, two
    # concurrent execute_workflow callers with the same fire_id can
    # both pre-read empty and both insert — producing duplicate
    # executions. App-layer SELECT alone is not race-safe. If the
    # CREATE statement raises (e.g. unsupported SQLite version),
    # abort engine startup rather than silently degrading.
    try:
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_executions_fire_id "
            "ON workflow_executions(fire_id) WHERE fire_id != ''"
        )
    except aiosqlite.OperationalError as exc:
        raise RuntimeError(
            "WTC v1 fire_id idempotency requires the partial unique "
            "index idx_executions_fire_id, which failed to create. "
            "This typically indicates an unsupported SQLite version "
            "(partial indexes require SQLite 3.8.0+). Engine startup "
            f"aborted to prevent silent dedup degradation. "
            f"Underlying error: {exc}"
        ) from exc

    # Defensive verification: confirm the index is actually present
    # in the schema. CREATE UNIQUE INDEX IF NOT EXISTS is normally
    # idempotent, but a malformed pre-existing index of the same
    # name could survive without enforcing the partial constraint.
    # Belt-and-suspenders for an invariant the substrate is
    # required to uphold.
    async with db.execute(
        "SELECT name FROM pragma_index_list('workflow_executions')"
    ) as cur:
        existing_indexes = {row[0] for row in await cur.fetchall()}
    if "idx_executions_fire_id" not in existing_indexes:
        raise RuntimeError(
            "WTC v1 fire_id idempotency invariant cannot be confirmed: "
            "idx_executions_fire_id is not present in workflow_executions "
            "after schema setup. Aborting engine startup."
        )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# WLP-GATE-SCOPING C1: action-payload template interpolation. The
# engine substitutes a small set of named placeholders inside string
# values within an action's parameters dict so descriptors can refer
# to engine-minted runtime values (notably the gate_nonce that wasn't
# yet known when the descriptor was authored).
#
# Recognised placeholders:
#   {workflow.execution_id}   → execution.execution_id
#   {workflow.gate_nonce}     → engine-minted nonce (gate_ref actions only)
#   {workflow.correlation_id} → execution.correlation_id
#   {workflow.workflow_id}    → execution.workflow_id
#   {workflow.instance_id}    → execution.instance_id
#
# Substitution is plain string replacement (no Python format-spec
# semantics) so descriptors cannot reach into Python attributes the
# substitution table doesn't expose. Recursive over nested dicts and
# lists; non-string scalars pass through unchanged.


_INTERPOLATION_KEYS = (
    "execution_id", "gate_nonce", "correlation_id",
    "workflow_id", "instance_id",
)


def _safe_build_record(
    *,
    execution: "WorkflowExecution",
    step_index: int,
    action: ActionDescriptor,
    execution_state: str,
    result: ActionResult | None = None,
    error: str = "",
    tool_lookup: ToolOperationClassLookup | None = None,
) -> ActionStateRecord | None:
    """Wrapper around ``_build_action_state_record`` that catches
    enum-validation failures.

    Risks-table invariant: action-record-construction failure MUST
    NOT abort the workflow step. If the helper raises (e.g. an
    unknown action verb whose operation_class fallback also fails
    validation), log loud and skip the record so the workflow runs
    even if audit is degraded.
    """
    try:
        return _build_action_state_record(
            execution=execution,
            step_index=step_index,
            action=action,
            execution_state=execution_state,
            result=result,
            error=error,
            tool_lookup=tool_lookup,
        )
    except Exception as exc:
        logger.warning(
            "WORKFLOW_ACTION_RECORD_BUILD_FAILED execution_id=%s "
            "step=%d action_type=%s error=%s",
            execution.execution_id, step_index, action.action_type, exc,
        )
        return None


def _interpolate_params(value: Any, ctx: dict[str, str]) -> Any:
    if isinstance(value, str):
        out = value
        for key in _INTERPOLATION_KEYS:
            out = out.replace("{workflow." + key + "}", ctx.get(key, ""))
        return out
    if isinstance(value, dict):
        return {k: _interpolate_params(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_params(item, ctx) for item in value]
    if isinstance(value, tuple):
        return tuple(_interpolate_params(item, ctx) for item in value)
    return value


class ExecutionEngine:
    """Background workflow execution. One engine per Kernos
    installation; one queue; one worker task; sequential dispatch."""

    def __init__(self) -> None:
        self._trigger_registry: TriggerRegistry | None = None
        self._workflow_registry: WorkflowRegistry | None = None
        self._action_library: ActionLibrary | None = None
        self._ledger: WorkflowLedger | None = None
        self._space_resolver: ActiveSpaceResolver | None = None
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._queue: asyncio.Queue[WorkflowExecution] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._listener_callable: Callable | None = None
        self._gate_waiters: dict[str, asyncio.Event] = {}
        # gate_waiters: execution_id → Event signalled when an
        # approval event matching the current gate predicate AND
        # carrying the engine-minted nonce flushes.
        self._gate_predicates: dict[str, dict] = {}
        self._gate_event_types: dict[str, str] = {}
        # WLP-GATE-SCOPING C1: per-active-wait nonce. Match logic in
        # ``_on_post_flush_for_gates`` requires the incoming event's
        # ``payload.gate_nonce`` to match this AND ``payload.execution_id``
        # to equal the paused execution's id, in addition to the
        # descriptor predicate.
        self._gate_nonces: dict[str, str] = {}
        self._gate_hook_registered: bool = False
        # ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1: substrate sink for
        # per-step ActionStateRecords. Constructed in ``start()`` once
        # the workflow DB connection is open. Per-execution wrappers
        # (``WorkflowExecutionActionSink``) bind the execution context.
        self._action_sink: WorkflowActionSink | None = None
        # Codex round-2 High 3: serialize BEGIN IMMEDIATE / COMMIT
        # boundaries on the shared workflow DB connection. Concurrent
        # asyncio tasks would otherwise interleave their transaction
        # markers and SQLite would error with "cannot start a
        # transaction within a transaction". The lock is acquired
        # inside ``_run_workflow_txn``.
        self._workflow_db_write_lock = asyncio.Lock()

    # -- lifecycle ------------------------------------------------------

    async def start(
        self,
        data_dir: str,
        trigger_registry: TriggerRegistry,
        workflow_registry: WorkflowRegistry,
        action_library: ActionLibrary,
        ledger: WorkflowLedger,
        *,
        space_resolver: ActiveSpaceResolver | None = None,
        pattern_store: "FrictionPatternStore | None" = None,
        action_sink_emit_event: EventStreamEmitter | None = None,
        tool_operation_class_lookup: ToolOperationClassLookup | None = None,
    ) -> None:
        if self._db is not None:
            return
        self._trigger_registry = trigger_registry
        self._workflow_registry = workflow_registry
        self._action_library = action_library
        self._ledger = ledger
        self._space_resolver = space_resolver
        self._db_path = Path(data_dir) / "instance.db"
        self._db = await aiosqlite.connect(
            str(self._db_path), isolation_level=None,
        )
        self._db.row_factory = aiosqlite.Row
        await _ensure_schema(self._db)
        # ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1: workflow_action_records
        # schema runs AFTER the existing workflow_executions ensure +
        # ALTER migrations so the FK target column already exists.
        await ensure_workflow_action_records_schema(self._db)
        self._action_sink = WorkflowActionSink(
            self._db,
            pattern_store=pattern_store,
            emit_event=action_sink_emit_event,
            tool_lookup=tool_operation_class_lookup,
        )
        self._stop_event = asyncio.Event()
        # Register the trigger match listener.
        self._listener_callable = self._on_trigger_match
        trigger_registry.add_match_listener(self._listener_callable)
        # Register the approval-gate post-flush hook.
        if not self._gate_hook_registered:
            event_stream.register_post_flush_hook(self._on_post_flush_for_gates)
            self._gate_hook_registered = True
        # ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1 Decision 6 / Codex
        # round-2 High 2: state-aware crash-window self-heal. Runs
        # BEFORE the restart-resume pass so reconciled executions
        # restart with the correct cursor.
        await self._self_heal_action_records()
        # Restart-resume: re-enqueue running executions where the next
        # action is resume-safe; abort the rest with aborted_by_restart.
        await self._restart_resume_pass()
        self._worker_task = asyncio.create_task(
            self._worker(), name="workflow_execution_engine",
        )

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._worker_task is not None:
            try:
                # Drain by enqueueing a sentinel so the worker can wake.
                self._queue.put_nowait(None)  # type: ignore[arg-type]
            except asyncio.QueueFull:
                pass
            try:
                await asyncio.wait_for(self._worker_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._worker_task = None
        if self._listener_callable is not None and self._trigger_registry is not None:
            self._trigger_registry.remove_match_listener(self._listener_callable)
            self._listener_callable = None
        if self._gate_hook_registered:
            event_stream.unregister_post_flush_hook(self._on_post_flush_for_gates)
            self._gate_hook_registered = False
        if self._db is not None:
            await self._db.close()
            self._db = None

    # -- trigger match → enqueue ---------------------------------------

    async def _on_trigger_match(self, trigger: Trigger, event: Event) -> None:
        """TriggerRegistry calls this when a trigger matches a durable
        event. Persist a queued WorkflowExecution and push it on the
        engine queue."""
        if self._db is None:
            return
        execution = WorkflowExecution(
            execution_id=str(uuid.uuid4()),
            workflow_id=trigger.workflow_id,
            instance_id=event.instance_id,
            correlation_id=str(uuid.uuid4()),
            state="queued",
            started_at=_now(),
            trigger_event_payload=event.payload,
            trigger_event_id=event.event_id,
            member_id=event.member_id or "",
        )
        await self._db.execute(
            "INSERT INTO workflow_executions ("
            " execution_id, workflow_id, instance_id, correlation_id,"
            " state, action_index_completed, intermediate_state,"
            " last_heartbeat, aborted_reason, started_at, terminated_at,"
            " trigger_event_payload, trigger_event_id, member_id,"
            " gate_nonce, fire_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            execution.to_row(),
        )
        self._queue.put_nowait(execution)

    # -- worker ---------------------------------------------------------

    async def _worker(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                execution = await asyncio.wait_for(
                    self._queue.get(), timeout=0.5,
                )
            except asyncio.TimeoutError:
                continue
            if execution is None:
                # Sentinel — stop drain.
                break
            try:
                await self._run_execution(execution)
            except Exception as exc:
                logger.warning(
                    "WORKFLOW_EXECUTION_FAILED execution_id=%s error=%s",
                    execution.execution_id, exc, exc_info=True,
                )

    # -- execution ------------------------------------------------------

    async def _run_execution(self, execution: WorkflowExecution) -> None:
        assert self._workflow_registry is not None
        assert self._action_library is not None
        # Mark running.
        await self._update_state(execution, "running")
        await event_stream.emit(
            execution.instance_id, "workflow.execution_started",
            {"workflow_id": execution.workflow_id,
             "execution_id": execution.execution_id,
             "trigger_event_id": execution.trigger_event_id},
            correlation_id=execution.correlation_id,
            member_id=execution.member_id or None,
        )
        wf = await self._workflow_registry.get_workflow(execution.workflow_id)
        if wf is None:
            await self._abort(execution, "workflow_not_found")
            return
        # Bounds enforcement (Codex review post-C7): wrap the rest of
        # the run in asyncio.wait_for so wall_time_seconds bounds are
        # actually enforced at runtime — registration requires the
        # field, so the runtime should honour it. iteration_count and
        # cost_usd bounds are not yet enforceable for sequential
        # action chains; future work.
        wall_time = wf.bounds.wall_time_seconds
        if wall_time is not None and wall_time > 0:
            try:
                await asyncio.wait_for(
                    self._run_action_sequence(execution, wf),
                    timeout=wall_time,
                )
            except asyncio.TimeoutError:
                await self._abort(execution, "wall_time_exceeded")
            return
        await self._run_action_sequence(execution, wf)

    async def _run_action_sequence(
        self, execution: WorkflowExecution, wf: Workflow,
    ) -> None:
        # Build synthetic CohortContext.
        try:
            context = await self._build_context(execution, wf)
        except _ContextBuildError as exc:
            await self._abort(execution, f"context_build_failed:{exc}")
            return
        # ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1: per-execution
        # action sink wrapper. Execution identity bound at
        # construction; appends do NOT take instance_id /
        # workflow_execution_id arguments.
        action_sink = self._execution_action_sink(execution)
        gate_by_name = {g.gate_name: g for g in wf.approval_gates}
        start_idx = max(0, execution.action_index_completed + 1)
        for idx in range(start_idx, len(wf.action_sequence)):
            action = wf.action_sequence[idx]
            verb = self._action_library.get(action.action_type)
            # WLP-GATE-SCOPING C1: nonce minted BEFORE the gated
            # action executes so the action's payload can carry it
            # (e.g. route_to_agent's approval_request block). The
            # nonce is held in a local until the action completes
            # successfully; if the action fails or aborts, the unused
            # nonce is discarded and no pause is entered.
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
            interpolated_params = _interpolate_params(
                action.parameters, interp_ctx,
            )
            try:
                result = await verb.execute(context, interpolated_params)
            except Exception as exc:
                # Codex C3 review: include the exception class name so
                # founders / debug tools can diagnose the failure
                # mode (e.g. AgentInboxUnavailable) without having to
                # parse the message string.
                error = f"execute_raised:{type(exc).__name__}:{exc}"
                # ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1 Decision 6
                # matrix row 4: append failed record + transition to
                # aborted state in one transaction.
                await self._append_failed_and_abort(
                    execution, action_sink, idx, action,
                    error=error,
                    aborted_reason=f"step_{idx}_raised:{type(exc).__name__}",
                )
                await self._record_step_failed(
                    execution, idx, action, error=error,
                )
                await self._emit_terminated_aborted(
                    execution, f"step_{idx}_raised:{type(exc).__name__}",
                )
                return
            verified = False
            try:
                verified = await verb.verify(
                    context, interpolated_params, result,
                )
            except Exception as exc:
                logger.warning(
                    "VERIFY_RAISED execution_id=%s step=%s error=%s",
                    execution.execution_id, idx, exc,
                )
            action_succeeded = result.success and verified
            if not action_succeeded:
                # Gated-action failure path: discard the unused
                # nonce; do NOT enter gate wait. AC #8 pin.
                # `pending_gate_nonce` is a local; going out of scope
                # here is the discard.
                error = result.error or "verifier_rejected"
                if action.continuation_rules.on_failure == "abort":
                    # Decision 6 matrix row 4: aborting failure.
                    await self._append_failed_and_abort(
                        execution, action_sink, idx, action,
                        error=error,
                        aborted_reason=f"step_{idx}_failed",
                    )
                    await self._record_step_failed(
                        execution, idx, action, error=error,
                    )
                    await self._emit_terminated_aborted(
                        execution, f"step_{idx}_failed",
                    )
                    return
                # Decision 6 matrix row 3: continue-on-failure.
                # Append failed record + advance cursor atomically.
                await self._append_failed_and_advance(
                    execution, action_sink, idx, action, error=error,
                )
                await self._record_step_failed(
                    execution, idx, action, error=error,
                )
                continue
            # Success path. Decision 6 matrix rows 1 + 2 distinguish
            # non-gated success (append + advance cursor) from gated
            # success (append + persist gate_nonce; cursor advances
            # later after gate release).
            if action.gate_ref is not None:
                await self._append_success_and_persist_gate_nonce(
                    execution, action_sink, idx, action, result,
                    gate_nonce=pending_gate_nonce,
                )
                await self._record_step_succeeded(
                    execution, idx, action, result,
                )
                gate = gate_by_name[action.gate_ref]
                # ``_persist_gate_nonce`` already happened inside the
                # transaction above; updating execution.gate_nonce in
                # memory so ``_await_gate`` and the post-flush match
                # logic see the engine-minted nonce.
                execution.gate_nonce = pending_gate_nonce
                cont = await self._await_gate(execution, gate)
                if not cont:
                    return  # _await_gate aborted
                await self._clear_gate_nonce(execution)
                await self._mark_step_complete(execution, idx)
            else:
                await self._append_success_and_advance(
                    execution, action_sink, idx, action, result,
                )
                await self._record_step_succeeded(
                    execution, idx, action, result,
                )
        # All steps done — mark completed.
        await self._complete(execution)

    # -- Decision 6 per-outcome wrappers -------------------------------

    async def _append_success_and_advance(
        self,
        execution: WorkflowExecution,
        sink: WorkflowExecutionActionSink,
        idx: int,
        action: ActionDescriptor,
        result: ActionResult,
    ) -> None:
        record = _safe_build_record(
            execution=execution, step_index=idx, action=action,
            execution_state="completed", result=result,
            tool_lookup=self._action_sink._tool_lookup
            if self._action_sink is not None else None,
        )
        if record is None:
            return
        await self._run_workflow_txn(
            lambda db: _append_and_advance(
                db, sink, record,
                step_index=idx, action_type=action.action_type,
            ),
        )
        execution.action_index_completed = idx

    async def _append_success_and_persist_gate_nonce(
        self,
        execution: WorkflowExecution,
        sink: WorkflowExecutionActionSink,
        idx: int,
        action: ActionDescriptor,
        result: ActionResult,
        *,
        gate_nonce: str,
    ) -> None:
        record = _safe_build_record(
            execution=execution, step_index=idx, action=action,
            execution_state="completed", result=result,
            tool_lookup=self._action_sink._tool_lookup
            if self._action_sink is not None else None,
        )
        if record is None:
            return
        await self._run_workflow_txn(
            lambda db: _append_and_persist_gate_nonce(
                db, sink, record,
                step_index=idx, action_type=action.action_type,
                gate_nonce=gate_nonce,
            ),
        )

    async def _append_failed_and_advance(
        self,
        execution: WorkflowExecution,
        sink: WorkflowExecutionActionSink,
        idx: int,
        action: ActionDescriptor,
        *,
        error: str,
    ) -> None:
        record = _safe_build_record(
            execution=execution, step_index=idx, action=action,
            execution_state="failed", error=error,
            tool_lookup=self._action_sink._tool_lookup
            if self._action_sink is not None else None,
        )
        if record is None:
            return
        inserted = await self._run_workflow_txn(
            lambda db: _append_and_advance(
                db, sink, record,
                step_index=idx, action_type=action.action_type,
            ),
        )
        execution.action_index_completed = idx
        if inserted:
            await self._post_commit_friction(sink, record, idx, action)

    async def _append_failed_and_abort(
        self,
        execution: WorkflowExecution,
        sink: WorkflowExecutionActionSink,
        idx: int,
        action: ActionDescriptor,
        *,
        error: str,
        aborted_reason: str,
    ) -> None:
        record = _safe_build_record(
            execution=execution, step_index=idx, action=action,
            execution_state="failed", error=error,
            tool_lookup=self._action_sink._tool_lookup
            if self._action_sink is not None else None,
        )
        if record is None:
            # Fall back to legacy abort path so the execution still
            # terminates correctly when record construction fails.
            await self._abort(execution, aborted_reason)
            return
        inserted = await self._run_workflow_txn(
            lambda db: _append_and_abort(
                db, sink, record,
                step_index=idx, action_type=action.action_type,
                aborted_reason=aborted_reason,
            ),
        )
        execution.state = "aborted"
        execution.aborted_reason = aborted_reason
        execution.terminated_at = _now()
        if inserted:
            await self._post_commit_friction(sink, record, idx, action)

    async def _post_commit_friction(
        self,
        sink: WorkflowExecutionActionSink,
        record: ActionStateRecord,
        idx: int,
        action: ActionDescriptor,
    ) -> None:
        try:
            await sink._classify_friction(
                record=record,
                step_index=idx,
                action_type=action.action_type,
            )
        except Exception as exc:
            logger.debug(
                "WORKFLOW_FRICTION_HOOK_FAILED execution_id=%s error=%s",
                sink.workflow_execution_id, exc,
            )

    async def _emit_terminated_aborted(
        self, execution: WorkflowExecution, reason: str,
    ) -> None:
        await event_stream.emit(
            execution.instance_id, "workflow.execution_terminated",
            {"execution_id": execution.execution_id,
             "workflow_id": execution.workflow_id,
             "outcome": "aborted",
             "reason": reason},
            correlation_id=execution.correlation_id,
        )

    async def _await_gate(
        self, execution: WorkflowExecution, gate: ApprovalGate,
    ) -> bool:
        """Pause until an approval event matches the gate predicate AND
        carries the engine-minted gate_nonce + execution_id, or timeout.
        Returns True if the engine should continue with the next
        action, False if it aborted the execution.

        WLP-GATE-SCOPING C1: emits ``workflow.execution_paused_at_gate``
        with the full gate descriptor + the engine-minted gate_nonce
        so downstream agents (owner UI, AgentInbox listeners) know
        what fields to compose into a valid approval response.
        """
        await event_stream.emit(
            execution.instance_id, "workflow.execution_paused_at_gate",
            {"execution_id": execution.execution_id,
             "gate_name": gate.gate_name,
             "gate_nonce": execution.gate_nonce,
             "pause_reason": gate.pause_reason,
             "approval_event_type": gate.approval_event_type,
             "approval_event_predicate": gate.approval_event_predicate,
             "timeout_seconds": gate.timeout_seconds,
             "bound_behavior_on_timeout": gate.bound_behavior_on_timeout},
            correlation_id=execution.correlation_id,
        )
        ev = asyncio.Event()
        self._gate_waiters[execution.execution_id] = ev
        self._gate_predicates[execution.execution_id] = gate.approval_event_predicate
        self._gate_event_types[execution.execution_id] = gate.approval_event_type
        # Stash the nonce + execution_id on the waiter context so the
        # post-flush match logic can verify both, not just the
        # descriptor predicate.
        self._gate_nonces[execution.execution_id] = execution.gate_nonce
        try:
            await asyncio.wait_for(ev.wait(), timeout=gate.timeout_seconds)
        except asyncio.TimeoutError:
            self._gate_waiters.pop(execution.execution_id, None)
            self._gate_predicates.pop(execution.execution_id, None)
            self._gate_event_types.pop(execution.execution_id, None)
            self._gate_nonces.pop(execution.execution_id, None)
            return await self._handle_gate_timeout(execution, gate)
        finally:
            self._gate_waiters.pop(execution.execution_id, None)
            self._gate_predicates.pop(execution.execution_id, None)
            self._gate_event_types.pop(execution.execution_id, None)
            self._gate_nonces.pop(execution.execution_id, None)
        await event_stream.emit(
            execution.instance_id, "workflow.execution_resumed",
            {"execution_id": execution.execution_id,
             "gate_name": gate.gate_name},
            correlation_id=execution.correlation_id,
        )
        return True

    async def _handle_gate_timeout(
        self, execution: WorkflowExecution, gate: ApprovalGate,
    ) -> bool:
        if gate.bound_behavior_on_timeout == "abort_workflow":
            await self._abort(execution, f"gate_timeout:{gate.gate_name}")
            return False
        if gate.bound_behavior_on_timeout == "escalate_to_owner":
            await event_stream.emit(
                execution.instance_id, "workflow.owner_escalation",
                {"execution_id": execution.execution_id,
                 "gate_name": gate.gate_name},
                correlation_id=execution.correlation_id,
            )
            await self._abort(execution, f"gate_escalated:{gate.gate_name}")
            return False
        # auto_proceed_with_default
        await event_stream.emit(
            execution.instance_id, "workflow.gate_auto_proceeded",
            {"execution_id": execution.execution_id,
             "gate_name": gate.gate_name,
             "default_value": gate.default_value},
            correlation_id=execution.correlation_id,
        )
        return True

    async def _on_post_flush_for_gates(self, batch: list[Event]) -> None:
        """Post-flush hook that resolves approval-gate waits.

        WLP-GATE-SCOPING C1: requires BOTH the descriptor predicate
        match AND the engine-minted nonce + execution_id binding.
        Either failing means the event does not wake this paused
        execution. This closes the bypass risk where a broad
        descriptor predicate (e.g. ``actor_eq owner``) would
        otherwise let any approval from that actor wake any paused
        execution waiting on the same event_type.
        """
        if not self._gate_waiters:
            return
        for execution_id, waiter in list(self._gate_waiters.items()):
            event_type = self._gate_event_types.get(execution_id)
            predicate = self._gate_predicates.get(execution_id)
            expected_nonce = self._gate_nonces.get(execution_id)
            if event_type is None or predicate is None or not expected_nonce:
                continue
            for event in batch:
                if event.event_type != event_type:
                    continue
                # Nonce + execution_id binding (engine-enforced).
                payload = event.payload or {}
                if payload.get("execution_id") != execution_id:
                    continue
                if payload.get("gate_nonce") != expected_nonce:
                    continue
                # Descriptor predicate (author-controlled).
                try:
                    if evaluate_predicate(predicate, event):
                        waiter.set()
                        break
                except Exception:
                    pass

    # -- workflow_db transactional helper -------------------------------

    async def _run_workflow_txn(
        self,
        body: Callable[[aiosqlite.Connection], Awaitable[Any]],
        *,
        retries: int = 3,
        retry_backoff_ms: int = 50,
    ) -> Any:
        """Run ``body`` inside a BEGIN IMMEDIATE transaction on the
        engine's shared workflow DB connection.

        ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1 Decision 3 / Codex
        round-2 High 3: aiosqlite serializes SQL through a single
        background thread per connection but does NOT prevent two
        asyncio tasks from interleaving BEGIN IMMEDIATE / COMMIT
        boundaries on the shared connection. SQLite errors with
        "cannot start a transaction within a transaction" on the
        second BEGIN. This helper acquires an engine-level
        ``asyncio.Lock`` for the duration so the boundary discipline
        is impossible to bypass accidentally.

        ROLLBACK on body exception; bounded busy-retry on
        ``database is locked``. The body MUST NOT call BEGIN /
        COMMIT itself.
        """
        assert self._db is not None
        attempt = 0
        async with self._workflow_db_write_lock:
            while True:
                try:
                    await self._db.execute("BEGIN IMMEDIATE")
                    try:
                        value = await body(self._db)
                    except Exception:
                        try:
                            await self._db.execute("ROLLBACK")
                        except aiosqlite.OperationalError:
                            pass
                        raise
                    await self._db.execute("COMMIT")
                    return value
                except aiosqlite.OperationalError as exc:
                    if (
                        "database is locked" in str(exc).lower()
                        and attempt < retries
                    ):
                        attempt += 1
                        await asyncio.sleep(
                            (retry_backoff_ms / 1000.0)
                            * (2 ** (attempt - 1))
                        )
                        continue
                    raise

    def _execution_action_sink(
        self, execution: WorkflowExecution,
    ) -> WorkflowExecutionActionSink:
        """Construct the per-execution action sink wrapper.

        Bound at this layer so the wrapper picks up the execution's
        ``instance_id``, ``workflow_execution_id``, ``workflow_id``,
        ``correlation_id``, and ``member_id`` directly from the
        ``WorkflowExecution`` (Codex round-2 Medium 6 writer
        invariant). Callers downstream cannot override these.
        """
        assert self._action_sink is not None, "action_sink not initialised"
        return self._action_sink.for_execution(
            execution, member_id=execution.member_id or "",
        )

    # -- crash-window self-heal -----------------------------------------

    async def _self_heal_action_records(self) -> None:
        """State-aware reconcile pass on engine startup.

        ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1 Decision 6 / Codex
        round-2 High 2: if a running execution has an action record
        for step N but ``action_index_completed < N``, decide whether
        to advance the cursor or leave the restart path to handle it.

        Advance ONLY when:
            * record.execution_state == "completed" AND
              action.gate_ref is None AND
              execution.gate_nonce == ""
          OR
            * record.execution_state == "failed" AND
              action.continuation_rules.on_failure != "abort"

        Otherwise log SKIP and rely on the existing restart logic
        (which already inspects gate_nonce, gate_ref, and
        continuation_rules) to do the right thing.
        """
        assert self._db is not None
        if self._action_sink is None or self._workflow_registry is None:
            return
        async with self._db.execute(
            "SELECT * FROM workflow_executions WHERE state = 'running'"
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            execution = WorkflowExecution.from_row(row)
            wf = await self._workflow_registry.get_workflow(execution.workflow_id)
            if wf is None:
                continue
            max_step = await self._action_sink.get_max_step_index(
                execution.instance_id, execution.execution_id,
            )
            if max_step is None or max_step <= execution.action_index_completed:
                continue
            if max_step >= len(wf.action_sequence):
                # Records past the end of the action sequence are
                # symptomatic of a corrupted workflow or a stale
                # record from a renamed workflow. Don't advance;
                # log loud.
                logger.warning(
                    "WORKFLOW_CRASH_WINDOW_SKIP execution_id=%s "
                    "record_step=%d cursor=%d reason=record_past_end",
                    execution.execution_id, max_step,
                    execution.action_index_completed,
                )
                continue
            record_row = await self._action_sink.get_by_step(
                execution.instance_id, execution.execution_id,
                step_index=max_step,
            )
            action = wf.action_sequence[max_step]
            record_state = (
                record_row.record.execution_state
                if record_row is not None else "?"
            )
            can_advance_completed = (
                record_row is not None
                and record_row.record.execution_state == "completed"
                and action.gate_ref is None
                and not execution.gate_nonce
            )
            can_advance_continue_failed = (
                record_row is not None
                and record_row.record.execution_state == "failed"
                and action.continuation_rules.on_failure != "abort"
            )
            if can_advance_completed or can_advance_continue_failed:
                logger.warning(
                    "WORKFLOW_CRASH_WINDOW_RECONCILE execution_id=%s "
                    "record_step=%d cursor=%d state=%s",
                    execution.execution_id, max_step,
                    execution.action_index_completed, record_state,
                )
                await self._db.execute(
                    "UPDATE workflow_executions "
                    "SET action_index_completed = ?, last_heartbeat = ? "
                    "WHERE execution_id = ? AND action_index_completed < ?",
                    (max_step, _now(), execution.execution_id, max_step),
                )
            else:
                logger.warning(
                    "WORKFLOW_CRASH_WINDOW_SKIP execution_id=%s "
                    "record_step=%d cursor=%d state=%s gate_ref=%s "
                    "on_failure=%s gate_nonce=%s",
                    execution.execution_id, max_step,
                    execution.action_index_completed, record_state,
                    action.gate_ref,
                    action.continuation_rules.on_failure,
                    execution.gate_nonce or "",
                )

    # -- restart-resume -------------------------------------------------

    async def _restart_resume_pass(self) -> None:
        assert self._db is not None
        assert self._workflow_registry is not None
        async with self._db.execute(
            "SELECT * FROM workflow_executions WHERE state = 'running'"
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            execution = WorkflowExecution.from_row(row)
            wf = await self._workflow_registry.get_workflow(execution.workflow_id)
            next_idx = execution.action_index_completed + 1
            if (
                wf is not None
                and 0 <= next_idx < len(wf.action_sequence)
                and wf.action_sequence[next_idx].resume_safe
            ):
                await event_stream.emit(
                    execution.instance_id, "workflow.execution_resumed",
                    {"execution_id": execution.execution_id,
                     "reason": "restart_resume",
                     "from_step": next_idx},
                    correlation_id=execution.correlation_id,
                )
                self._queue.put_nowait(execution)
            else:
                # Conservative default: not resume-safe → abort.
                await self._abort(execution, "aborted_by_restart")

    # -- audit + persistence helpers -----------------------------------

    async def _update_state(
        self, execution: WorkflowExecution, state: str,
    ) -> None:
        assert self._db is not None
        execution.state = state
        execution.last_heartbeat = _now()
        await self._db.execute(
            "UPDATE workflow_executions SET state = ?, last_heartbeat = ? "
            "WHERE execution_id = ?",
            (state, execution.last_heartbeat, execution.execution_id),
        )

    async def _persist_gate_nonce(
        self, execution: WorkflowExecution, nonce: str,
    ) -> None:
        """Record the gate_nonce against the running execution row so
        post-flush match logic can require it on incoming approvals.
        Called after the gate_ref action completes successfully —
        unsuccessful actions discard the unused nonce instead."""
        assert self._db is not None
        execution.gate_nonce = nonce
        await self._db.execute(
            "UPDATE workflow_executions SET gate_nonce = ?, last_heartbeat = ? "
            "WHERE execution_id = ?",
            (nonce, _now(), execution.execution_id),
        )

    async def _clear_gate_nonce(
        self, execution: WorkflowExecution,
    ) -> None:
        """Clear the gate_nonce after the execution resumes from a
        gate. Stale-nonce-rejection (AC #13) relies on this — once
        cleared, a replayed approval event carrying the old nonce
        finds no waiter to wake."""
        assert self._db is not None
        execution.gate_nonce = ""
        await self._db.execute(
            "UPDATE workflow_executions SET gate_nonce = '', "
            "last_heartbeat = ? WHERE execution_id = ?",
            (_now(), execution.execution_id),
        )

    async def _mark_step_complete(
        self, execution: WorkflowExecution, idx: int,
    ) -> None:
        assert self._db is not None
        execution.action_index_completed = idx
        await self._db.execute(
            "UPDATE workflow_executions SET action_index_completed = ?, "
            "last_heartbeat = ? WHERE execution_id = ?",
            (idx, _now(), execution.execution_id),
        )

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
                await self._ledger.append(
                    execution.instance_id, execution.workflow_id,
                    {"execution_id": execution.execution_id,
                     "step_index": idx,
                     "agent_or_action": action.action_type,
                     "synopsis": f"{action.action_type} failed",
                     "result_summary": "failed",
                     "kickback_if_any": error},
                )
            except Exception as exc:
                logger.warning("LEDGER_APPEND_FAILED %s", exc)

    async def _abort(
        self, execution: WorkflowExecution, reason: str,
    ) -> None:
        assert self._db is not None
        execution.state = "aborted"
        execution.aborted_reason = reason
        execution.terminated_at = _now()
        await self._db.execute(
            "UPDATE workflow_executions SET state = ?, aborted_reason = ?, "
            "terminated_at = ? WHERE execution_id = ?",
            ("aborted", reason, execution.terminated_at, execution.execution_id),
        )
        await event_stream.emit(
            execution.instance_id, "workflow.execution_terminated",
            {"execution_id": execution.execution_id,
             "workflow_id": execution.workflow_id,
             "outcome": "aborted",
             "reason": reason},
            correlation_id=execution.correlation_id,
        )

    async def _complete(self, execution: WorkflowExecution) -> None:
        assert self._db is not None
        execution.state = "completed"
        execution.terminated_at = _now()
        await self._db.execute(
            "UPDATE workflow_executions SET state = ?, terminated_at = ? "
            "WHERE execution_id = ?",
            ("completed", execution.terminated_at, execution.execution_id),
        )
        await event_stream.emit(
            execution.instance_id, "workflow.execution_terminated",
            {"execution_id": execution.execution_id,
             "workflow_id": execution.workflow_id,
             "outcome": "completed"},
            correlation_id=execution.correlation_id,
        )

    # -- context construction ------------------------------------------

    async def _build_context(
        self, execution: WorkflowExecution, wf: Workflow,
    ) -> CohortContext:
        if self._space_resolver is not None:
            try:
                spaces = await self._space_resolver(execution.instance_id)
            except Exception as exc:
                raise _ContextBuildError(
                    f"active_space_resolution_failed: {exc}"
                ) from exc
        else:
            spaces = ()
        return CohortContext(
            member_id=execution.member_id or "workflow",
            user_message=(
                f"workflow:{wf.workflow_id} fired by trigger event "
                f"{execution.trigger_event_id}"
            ),
            conversation_thread=(),
            active_spaces=spaces,
            turn_id=f"workflow:{execution.execution_id}",
            instance_id=execution.instance_id,
            produced_at=execution.started_at,
        )

    # -- WTC v1: outbox-driven dispatch entry point ---------------------

    async def execute_workflow(
        self,
        *,
        fire_id: str,
        workflow_id: str,
        instance_id: str,
        trigger_event_payload: dict | None = None,
        trigger_event_id: str = "",
        member_id: str = "",
    ) -> str:
        """Public entry point used by the unified trigger runtime
        (WTC v1) for outbox-driven cross-process dispatch.

        Idempotent on ``fire_id`` (the design review must-fix, post-fold). Behaviour:

        1. SELECT the row with this ``fire_id`` first. If present,
           return its ``execution_id`` — the original execution
           created by the prior call. No second row, no second
           workflow run.
        2. Otherwise INSERT a fresh execution_id with the supplied
           ``fire_id`` and queue it for the worker.

        ``fire_id`` MUST be non-empty. Empty ``fire_id`` is the
        legacy in-process Trigger-matched path's signal — that path
        uses ``_on_trigger_match`` and is exempt from the partial
        unique index by design. Callers must supply a stable
        ``fire_id`` derived from ``(trigger_id, fire_window_key)``.

        Race tolerance: if two callers race the SELECT-then-INSERT
        with the same ``fire_id``, the partial unique index catches
        the loser at INSERT time. The loser then re-runs the SELECT
        and returns the winner's ``execution_id``. Net effect:
        exactly one execution per ``fire_id``.
        """
        if self._db is None:
            raise RuntimeError("ExecutionEngine not started")
        if not fire_id:
            raise ValueError(
                "execute_workflow requires a non-empty fire_id; "
                "the legacy in-process path uses _on_trigger_match"
            )

        # Idempotency check — return original execution_id if the
        # fire_id has already been registered.
        existing = await self._find_execution_by_fire_id_unlocked(fire_id)
        if existing is not None:
            return existing

        execution = WorkflowExecution(
            execution_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            instance_id=instance_id,
            correlation_id=str(uuid.uuid4()),
            state="queued",
            started_at=_now(),
            trigger_event_payload=trigger_event_payload or {},
            trigger_event_id=trigger_event_id,
            member_id=member_id,
            fire_id=fire_id,
        )
        try:
            await self._db.execute(
                "INSERT INTO workflow_executions ("
                " execution_id, workflow_id, instance_id, correlation_id,"
                " state, action_index_completed, intermediate_state,"
                " last_heartbeat, aborted_reason, started_at, terminated_at,"
                " trigger_event_payload, trigger_event_id, member_id,"
                " gate_nonce, fire_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                execution.to_row(),
            )
        except aiosqlite.IntegrityError as exc:
            # Partial-unique-index race: another caller won the
            # INSERT. Re-fetch and return their execution_id.
            if "fire_id" not in str(exc).lower() and "unique" not in str(exc).lower():
                raise
            existing = await self._find_execution_by_fire_id_unlocked(fire_id)
            if existing is None:
                # Pathological: index hit but row not visible. Fail
                # loudly rather than silently double-dispatch.
                raise RuntimeError(
                    f"execute_workflow race: fire_id={fire_id!r} hit "
                    "the unique index but no row visible on re-read"
                ) from exc
            return existing

        self._queue.put_nowait(execution)
        return execution.execution_id

    async def find_execution_by_fire_id(
        self, fire_id: str,
    ) -> str | None:
        """Public lookup used by the recovery sweep before
        re-dispatching a still-pending outbox row past its
        claim_lease. Returns the workflow_execution_id for the
        supplied fire_id, or None when no execution exists.

        WTC v1 (the design review must-fix). Closes the seam between WLP accept
        and trigger-runtime mark_dispatched: the recovery sweep
        consults this method first, reconciles to dispatched/
        completed without re-invoking execute_workflow when the
        WLP execution already exists.
        """
        if self._db is None:
            return None
        if not fire_id:
            return None
        return await self._find_execution_by_fire_id_unlocked(fire_id)

    async def _find_execution_by_fire_id_unlocked(
        self, fire_id: str,
    ) -> str | None:
        async with self._db.execute(
            "SELECT execution_id FROM workflow_executions "
            "WHERE fire_id = ? LIMIT 1",
            (fire_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return row["execution_id"] if hasattr(row, "keys") else row[0]

    # -- queries --------------------------------------------------------

    async def get_execution(
        self, execution_id: str,
    ) -> WorkflowExecution | None:
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT * FROM workflow_executions WHERE execution_id = ?",
            (execution_id,),
        ) as cur:
            row = await cur.fetchone()
        return WorkflowExecution.from_row(row) if row else None

    async def list_executions(
        self, instance_id: str, *, state: str | None = None,
    ) -> list[WorkflowExecution]:
        if self._db is None:
            return []
        if state is None:
            query = (
                "SELECT * FROM workflow_executions WHERE instance_id = ? "
                "ORDER BY started_at"
            )
            args: tuple = (instance_id,)
        else:
            query = (
                "SELECT * FROM workflow_executions WHERE instance_id = ? "
                "AND state = ? ORDER BY started_at"
            )
            args = (instance_id, state)
        async with self._db.execute(query, args) as cur:
            rows = await cur.fetchall()
        return [WorkflowExecution.from_row(r) for r in rows]


class _ContextBuildError(RuntimeError):
    """Internal: signal that synthetic CohortContext construction
    failed and the execution should be aborted."""


__all__ = [
    "ActiveSpaceResolver",
    "ExecutionEngine",
    "WorkflowExecution",
]
