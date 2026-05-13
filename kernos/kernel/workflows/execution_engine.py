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
    _abort_state_only,
    _advance_cursor_only,
    _append_and_abort,
    _append_and_advance,
    _append_and_advance_with_branch,
    _append_and_persist_gate_nonce,
    _build_action_state_record,
    _clear_gate_nonce_and_advance,
    _persist_gate_nonce_only,
    ensure_workflow_action_records_schema,
)
from kernos.kernel.workflows.refs import (
    RefResolutionError,
    ResolutionContext,
    resolve_references_in_value,
)
from kernos.kernel.workflows.step_outputs import (
    build_output_envelope,
    ensure_workflow_step_outputs_schema,
    load_workflow_outputs,
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
    # WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 7: when the
    # workflow enters a terminal branch via the `branch` verb, the
    # branch_name is captured here for audit. Engine terminal_state
    # stays completed / aborted; this is metadata only.
    terminal_branch: str = ""
    # WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 9 (Codex round-1
    # Blocker 1): the global step_index that the engine should
    # execute next, OVERRIDING the natural action_index_completed + 1
    # advance. Set by the `branch` verb's atomic transaction; cleared
    # to -1 by the target step's commit. Restart-resume reads this
    # before defaulting.
    next_step_index: int = -1

    def to_row(self) -> tuple:
        # NOTE: ``to_row`` is only used by the INSERT path
        # (_on_trigger_match + execute_workflow). The columns
        # terminal_branch + next_step_index land via ALTER and have
        # their DB-side defaults (empty string / -1); the INSERT
        # statements explicitly list the 16 baseline columns, so
        # to_row stays 16-wide. UPDATE paths handle the new columns
        # directly.
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
        # WORKFLOW-ORCHESTRATION-PRIMITIVES-V1: terminal_branch and
        # next_step_index added by Spec 4a's ALTER migration. Rows
        # from before the migration get the column defaults.
        try:
            terminal_branch = row["terminal_branch"] or ""
        except (KeyError, IndexError):
            terminal_branch = ""
        try:
            next_step_index = row["next_step_index"]
            if next_step_index is None:
                next_step_index = -1
        except (KeyError, IndexError):
            next_step_index = -1
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
            terminal_branch=terminal_branch,
            next_step_index=next_step_index,
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
    # WORKFLOW-ORCHESTRATION-PRIMITIVES-V1: terminal_branch +
    # next_step_index columns. Co-located with the base ensure_schema
    # so any code path that opens a workflow_executions table picks
    # them up (Spec 3 helpers reference next_step_index in UPDATE
    # clauses; the column MUST exist).
    if "terminal_branch" not in existing_columns:
        try:
            await db.execute(
                "ALTER TABLE workflow_executions "
                "ADD COLUMN terminal_branch TEXT DEFAULT ''"
            )
        except aiosqlite.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    if "next_step_index" not in existing_columns:
        try:
            await db.execute(
                "ALTER TABLE workflow_executions "
                "ADD COLUMN next_step_index INTEGER DEFAULT -1"
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


def _resolve_predicate_ast(predicate: Any, ctx: "ResolutionContext") -> dict | None:
    """Decision 8: recursively walk a predicate AST, substituting
    references in ``value:`` and ``values:`` leaves via the resolver.

    Composite operators (AND, OR, NOT) walk into operands/operand.
    Leaves with ``value`` get substitution. Returns the resolved AST
    (a new dict) OR None if any reference can't resolve (caller
    skips this evaluation pass).
    """
    from kernos.kernel.workflows.refs import _NOT_FOUND  # type: ignore
    if not isinstance(predicate, dict):
        return None
    op = predicate.get("op")
    if not op:
        return None
    if op in ("AND", "OR"):
        operands = predicate.get("operands") or []
        resolved_operands = []
        for sub in operands:
            resolved_sub = _resolve_predicate_ast(sub, ctx)
            if resolved_sub is None:
                return None
            resolved_operands.append(resolved_sub)
        return {"op": op, "operands": resolved_operands}
    if op == "NOT":
        sub = predicate.get("operand")
        resolved_sub = _resolve_predicate_ast(sub, ctx)
        if resolved_sub is None:
            return None
        return {"op": "NOT", "operand": resolved_sub}
    # Leaf operator. Walk each field and resolve string templates.
    resolved_leaf: dict[str, Any] = {}
    for key, val in predicate.items():
        resolved_val = resolve_references_in_value(val, ctx)
        if resolved_val is _NOT_FOUND:
            return None
        resolved_leaf[key] = resolved_val
    return resolved_leaf


def _safe_build_record(
    *,
    execution: "WorkflowExecution",
    step_index: int,
    action: ActionDescriptor,
    execution_state: str,
    result: ActionResult | None = None,
    error: str = "",
    tool_lookup: ToolOperationClassLookup | None = None,
    resolved_params: dict | None = None,
) -> ActionStateRecord | None:
    """Wrapper around ``_build_action_state_record`` that catches
    enum-validation failures.

    Spec 4 post-impl Medium 7: ``resolved_params`` carries the
    post-reference-resolution parameter dict so the audit record
    captures the values actually passed to the verb (not the raw
    descriptor templates).

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
            resolved_params=resolved_params,
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
        # WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 6 / Codex
        # round-1 High 5: per-execution gate-release payload buffer.
        # The post-flush match logic writes the satisfying event's
        # payload here before signalling the waiter; _await_gate reads
        # and pops it so the payload can be threaded into
        # _clear_gate_and_advance for atomic capture into
        # workflow_step_outputs (output_kind='gate').
        self._gate_release_payloads: dict[str, dict] = {}
        # WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 8 / Codex
        # round-1 Medium 8: cache resolved predicate AST per
        # (execution_id, gate_nonce). Predicate evaluation happens
        # for every event matching event_type that flushes; resolving
        # references each time would be expensive. Keyed on gate_nonce
        # (per-attempt UUID) so reuse of a gate_name within an
        # execution can't read a stale cache. Cleared on gate
        # release / timeout / abort alongside the other waiter dicts.
        self._predicate_resolution_cache: dict[tuple[str, str], dict] = {}
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
        # WORKFLOW-ORCHESTRATION-PRIMITIVES-V1: workflow_step_outputs
        # table + ALTERs for terminal_branch + next_step_index columns
        # on workflow_executions. Idempotent migration; runs every
        # engine start.
        await ensure_workflow_step_outputs_schema(self._db)
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
        engine queue.

        Codex round-2-impl High 4: this listener fires on the event-
        stream post-flush task, NOT on the engine's own worker. Route
        the INSERT through ``_run_workflow_write`` so it can't
        interleave inside a concurrent ``_run_workflow_txn`` body and
        end up implicitly committed alongside (or rolled back with)
        an unrelated transaction.
        """
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
        await self._run_workflow_write(
            lambda db: db.execute(
                "INSERT INTO workflow_executions ("
                " execution_id, workflow_id, instance_id, correlation_id,"
                " state, action_index_completed, intermediate_state,"
                " last_heartbeat, aborted_reason, started_at, terminated_at,"
                " trigger_event_payload, trigger_event_id, member_id,"
                " gate_nonce, fire_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                execution.to_row(),
            ),
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
        # WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 0 + 9: build
        # the global step_index → action map across main +
        # terminal_branches. step_index is the ordinal Spec 3's
        # workflow_action_records PK uses; lookup by global ordinal
        # makes branch / terminal targeting uniform.
        action_by_index = self._build_action_by_index(wf)
        terminal_ranges = self._build_terminal_branch_ranges(wf)
        main_range = self._build_main_range(wf)
        # Codex round-2-impl Blocker 1 (Spec 3): pending-gate restart.
        if await self._is_pending_gate_resume(execution, wf):
            gate_step_index = execution.action_index_completed + 1
            gate_action = action_by_index[gate_step_index]
            gate = gate_by_name[gate_action.gate_ref]
            cont, gate_payload = await self._await_gate(execution, gate)
            if not cont:
                return  # _await_gate aborted
            updated = await self._clear_gate_and_advance(
                execution, gate_step_index,
                gate_name=gate_action.gate_ref,
                gate_output_payload=gate_payload,
            )
            if not updated:
                # Spec 4 post-impl Medium 8: stale-nonce divergence
                # at gate release means the row's state has diverged
                # from our in-memory execution. Bail out; the
                # restart-resume pass picks up the actual DB state
                # on next engine start.
                return
            current_step_index = self._natural_next_step_index(
                gate_step_index, terminal_ranges, main_range=main_range,
            )
        else:
            current_step_index = self._resolve_next_step_index(execution, wf)
        # WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 9: explicit
        # while-loop with next_step_index pointer; branch verb mutates
        # the pointer via the atomic _append_and_advance_with_branch
        # helper.
        total_step_count = len(action_by_index)
        while current_step_index is not None and current_step_index < total_step_count:
            action = action_by_index[current_step_index]
            verb = self._action_library.get(action.action_type)
            # Track terminal branch entry for audit (Decision 7).
            entered_terminal = self._terminal_branch_for_step(
                current_step_index, terminal_ranges,
            )
            if entered_terminal and entered_terminal != execution.terminal_branch:
                execution.terminal_branch = entered_terminal
                # Persist the branch name on the execution row so
                # post-completion queries can attribute the workflow's
                # terminal path.
                await self._run_workflow_write(
                    lambda db: db.execute(
                        "UPDATE workflow_executions "
                        "SET terminal_branch = ?, last_heartbeat = ? "
                        "WHERE execution_id = ?",
                        (entered_terminal, _now(), execution.execution_id),
                    ),
                )
            # WLP-GATE-SCOPING C1: gate nonce minted BEFORE the action
            # executes so the action's payload can carry it.
            pending_gate_nonce = (
                str(uuid.uuid4()) if action.gate_ref is not None else ""
            )
            # WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 3:
            # resolve template references in action parameters via
            # the ResolutionContext + reference resolver.
            try:
                resolve_ctx = await self._build_resolution_context(
                    execution, pending_gate_nonce, mode="parameter",
                )
                resolved_params = resolve_references_in_value(
                    action.parameters, resolve_ctx,
                )
            except RefResolutionError as exc:
                # Decision 3 v2: dynamic resolution failure in
                # parameter context aborts the workflow via the
                # per-outcome aborting-failure matrix (Spec 3).
                error = f"ref_resolution_failed:{exc}"
                await self._append_failed_and_abort(
                    execution, action_sink, current_step_index, action,
                    error=error,
                    aborted_reason=(
                        f"step_{action.id or current_step_index}_ref_failed"
                    ),
                )
                await self._record_step_failed(
                    execution, current_step_index, action, error=error,
                )
                await self._emit_terminated_aborted(
                    execution,
                    f"step_{action.id or current_step_index}_ref_failed",
                )
                return
            try:
                result = await verb.execute(context, resolved_params)
            except Exception as exc:
                error = f"execute_raised:{type(exc).__name__}:{exc}"
                await self._append_failed_and_abort(
                    execution, action_sink, current_step_index, action,
                    error=error,
                    aborted_reason=(
                        f"step_{action.id or current_step_index}"
                        f"_raised:{type(exc).__name__}"
                    ),
                    resolved_params=resolved_params,
                )
                await self._record_step_failed(
                    execution, current_step_index, action, error=error,
                )
                await self._emit_terminated_aborted(
                    execution,
                    f"step_{action.id or current_step_index}"
                    f"_raised:{type(exc).__name__}",
                )
                return
            verified = False
            try:
                verified = await verb.verify(
                    context, resolved_params, result,
                )
            except Exception as exc:
                logger.warning(
                    "VERIFY_RAISED execution_id=%s step=%s error=%s",
                    execution.execution_id, current_step_index, exc,
                )
            action_succeeded = result.success and verified
            if not action_succeeded:
                error = result.error or "verifier_rejected"
                if action.continuation_rules.on_failure == "abort":
                    await self._append_failed_and_abort(
                        execution, action_sink, current_step_index, action,
                        error=error,
                        aborted_reason=(
                            f"step_{action.id or current_step_index}_failed"
                        ),
                        resolved_params=resolved_params,
                    )
                    await self._record_step_failed(
                        execution, current_step_index, action, error=error,
                    )
                    await self._emit_terminated_aborted(
                        execution,
                        f"step_{action.id or current_step_index}_failed",
                    )
                    return
                # Continue-on-failure.
                await self._append_failed_and_advance(
                    execution, action_sink, current_step_index, action,
                    error=error,
                    resolved_params=resolved_params,
                )
                await self._record_step_failed(
                    execution, current_step_index, action, error=error,
                )
                current_step_index = self._natural_next_step_index(
                    current_step_index, terminal_ranges,
                    main_range=main_range,
                )
                continue
            # Success path.
            if action.action_type == "branch":
                # WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 5 +
                # Codex round-1 Blocker 1: branch verb. Resolve the
                # target step_id to a global ordinal; persist
                # next_step_index atomically with the record.
                target_id = result.receipt.get("branched_to", "")
                target_global = self._resolve_branch_target_to_global(
                    target_id, action_by_index, wf, terminal_ranges,
                )
                if target_global is None:
                    # Should not happen if validate_workflow passed;
                    # defensive abort.
                    error = (
                        f"branch_target_unresolved:{target_id!r}"
                    )
                    await self._append_failed_and_abort(
                        execution, action_sink, current_step_index, action,
                        error=error,
                        aborted_reason=(
                            f"step_{action.id or current_step_index}_branch_failed"
                        ),
                        resolved_params=resolved_params,
                    )
                    await self._record_step_failed(
                        execution, current_step_index, action, error=error,
                    )
                    await self._emit_terminated_aborted(
                        execution,
                        f"step_{action.id or current_step_index}_branch_failed",
                    )
                    return
                await self._append_branch_and_advance(
                    execution, action_sink, current_step_index, action, result,
                    next_step_index=target_global,
                    resolved_params=resolved_params,
                )
                await self._record_step_succeeded(
                    execution, current_step_index, action, result,
                )
                current_step_index = target_global
                continue
            if action.gate_ref is not None:
                await self._append_success_and_persist_gate_nonce(
                    execution, action_sink, current_step_index, action, result,
                    gate_nonce=pending_gate_nonce,
                    resolved_params=resolved_params,
                )
                await self._record_step_succeeded(
                    execution, current_step_index, action, result,
                )
                gate = gate_by_name[action.gate_ref]
                execution.gate_nonce = pending_gate_nonce
                cont, gate_payload = await self._await_gate(execution, gate)
                if not cont:
                    return  # _await_gate aborted
                updated = await self._clear_gate_and_advance(
                    execution, current_step_index,
                    gate_name=action.gate_ref,
                    gate_output_payload=gate_payload,
                )
                if not updated:
                    # Spec 4 post-impl Medium 8: stale-nonce divergence.
                    # Bail out; restart-resume picks up actual state.
                    return
            else:
                await self._append_success_and_advance(
                    execution, action_sink, current_step_index, action, result,
                    resolved_params=resolved_params,
                )
                await self._record_step_succeeded(
                    execution, current_step_index, action, result,
                )
            current_step_index = self._natural_next_step_index(
                current_step_index, terminal_ranges,
                main_range=main_range,
            )
        # All steps done — mark completed (with optional terminal
        # branch metadata for audit).
        await self._complete(execution)

    # -- Spec 4 cursor / step-index helpers ----------------------------

    def _build_action_by_index(
        self, wf: Workflow,
    ) -> dict[int, ActionDescriptor]:
        """Map global step_index → ActionDescriptor for main +
        terminal_branches. step_index is assigned by validate_workflow.
        """
        result: dict[int, ActionDescriptor] = {}
        for action in wf.action_sequence:
            if action.step_index >= 0:
                result[action.step_index] = action
        for branch_actions in wf.terminal_branches.values():
            for action in branch_actions:
                if action.step_index >= 0:
                    result[action.step_index] = action
        return result

    def _build_terminal_branch_ranges(
        self, wf: Workflow,
    ) -> dict[str, tuple[int, int]]:
        """Map terminal branch name → (first_step_index, last_step_index)
        inclusive. Used for cursor-in-branch detection.
        """
        ranges: dict[str, tuple[int, int]] = {}
        for branch_name, branch_actions in wf.terminal_branches.items():
            if not branch_actions:
                continue
            indices = [a.step_index for a in branch_actions if a.step_index >= 0]
            if indices:
                ranges[branch_name] = (min(indices), max(indices))
        return ranges

    def _build_main_range(
        self, wf: Workflow,
    ) -> tuple[int, int] | None:
        """Return (first_step_index, last_step_index) for the main
        action_sequence. Used to detect end-of-main so the engine
        doesn't fall through into terminal branches.
        """
        indices = [a.step_index for a in wf.action_sequence if a.step_index >= 0]
        if not indices:
            return None
        return (min(indices), max(indices))

    def _terminal_branch_for_step(
        self,
        step_index: int,
        ranges: dict[str, tuple[int, int]],
    ) -> str:
        """Return branch_name if step_index falls within a terminal
        branch's range; empty string otherwise.
        """
        for branch_name, (lo, hi) in ranges.items():
            if lo <= step_index <= hi:
                return branch_name
        return ""

    def _natural_next_step_index(
        self,
        current: int,
        ranges: dict[str, tuple[int, int]],
        *,
        main_range: tuple[int, int] | None = None,
    ) -> int | None:
        """Compute the natural next global step_index after ``current``.

        Three cases:
        - ``current`` in a terminal branch range: advance within the
          branch; end-of-branch → None (terminal branches don't fall
          back to main sequence).
        - ``current`` in main range: advance within main;
          end-of-main → None (main sequence doesn't fall through into
          terminal branches — those are only reachable via branch
          verb routing).
        - Otherwise: defensive None (unknown position).
        """
        for lo, hi in ranges.values():
            if lo <= current <= hi:
                if current == hi:
                    return None
                return current + 1
        if main_range is not None:
            lo, hi = main_range
            if lo <= current <= hi:
                if current == hi:
                    return None
                return current + 1
        return None

    def _resolve_next_step_index(
        self, execution: WorkflowExecution, wf: Workflow,
    ) -> int:
        """At workflow start AND after restart-resume, decide the first
        step to run.

        Codex round-1 Blocker 1 (durable branch): if next_step_index
        is set on the execution row (>= 0), use it — a branch
        decision was persisted. Otherwise default to
        action_index_completed + 1.
        """
        if execution.next_step_index >= 0:
            return execution.next_step_index
        return max(0, execution.action_index_completed + 1)

    def _resolve_branch_target_to_global(
        self,
        target_id: str,
        action_by_index: dict[int, ActionDescriptor],
        wf: Workflow,
        terminal_ranges: dict[str, tuple[int, int]],
    ) -> int | None:
        """Map a branch verb's target_step_id to its global step_index.

        Bare ``<step_id>``: look up in any registered action.
        ``terminal:<branch_name>:<step_id>``: walk the named branch.
        """
        if not isinstance(target_id, str) or not target_id:
            return None
        if target_id.startswith("terminal:"):
            parts = target_id.split(":", 2)
            if len(parts) != 3:
                return None
            _, branch_name, step_id = parts
            for action in wf.terminal_branches.get(branch_name, []):
                if action.id == step_id and action.step_index >= 0:
                    return action.step_index
            return None
        # Bare step ID — search all registered actions.
        for action in action_by_index.values():
            if action.id == target_id:
                return action.step_index
        return None

    async def _build_resolution_context(
        self,
        execution: WorkflowExecution,
        pending_gate_nonce: str,
        *,
        mode: str = "parameter",
    ) -> ResolutionContext:
        """Construct the resolver context for the current step.

        Loads step + gate outputs from workflow_step_outputs so
        references against earlier-completed steps resolve.
        ``pending_gate_nonce`` shadows ``execution.gate_nonce`` for
        ``{workflow.gate_nonce}`` resolution (matches the original
        ``_interpolate_params`` semantics where the minted nonce is
        the value the gated action's payload carries).
        """
        assert self._db is not None
        step_outputs, gate_outputs = await load_workflow_outputs(
            self._db, execution.instance_id, execution.execution_id,
        )
        return ResolutionContext(
            execution=execution,
            trigger_payload=execution.trigger_event_payload or {},
            step_outputs=step_outputs,
            gate_outputs=gate_outputs,
            pending_gate_nonce=pending_gate_nonce,
            mode=mode,
        )

    # -- Decision 6 per-outcome wrappers -------------------------------

    async def _append_success_and_advance(
        self,
        execution: WorkflowExecution,
        sink: WorkflowExecutionActionSink,
        idx: int,
        action: ActionDescriptor,
        result: ActionResult,
        *,
        resolved_params: dict | None = None,
    ) -> None:
        record = _safe_build_record(
            execution=execution, step_index=idx, action=action,
            execution_state="completed", result=result,
            tool_lookup=self._action_sink._tool_lookup
            if self._action_sink is not None else None,
            resolved_params=resolved_params,
        )
        # WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 2: per-outcome
        # capture matrix — non-gated success envelope.
        envelope = build_output_envelope(
            success=True, value=result.value,
            error=None, receipt=result.receipt,
        )
        if record is None:
            # Codex round-2-impl High 3 (Spec 3): audit-build failure
            # still advances cursor; skips the record AND skips the
            # step output capture (Decision 11 consistency invariant).
            await self._run_workflow_txn(
                lambda db: _advance_cursor_only(
                    db, execution.execution_id, step_index=idx,
                ),
            )
            execution.action_index_completed = idx
            execution.next_step_index = -1
            return
        await self._run_workflow_txn(
            lambda db: _append_and_advance(
                db, sink, record,
                step_index=idx, action_type=action.action_type,
                step_output_envelope=envelope, step_id=action.id,
            ),
        )
        execution.action_index_completed = idx
        execution.next_step_index = -1

    async def _append_branch_and_advance(
        self,
        execution: WorkflowExecution,
        sink: WorkflowExecutionActionSink,
        idx: int,
        action: ActionDescriptor,
        result: ActionResult,
        *,
        next_step_index: int,
        resolved_params: dict | None = None,
    ) -> None:
        """WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 5 + 9 /
        Codex round-1 Blocker 1: branch verb's atomic boundary.
        Persists next_step_index in the same transaction as the
        record append so restart-resume honors the branch decision
        durably.
        """
        record = _safe_build_record(
            execution=execution, step_index=idx, action=action,
            execution_state="completed", result=result,
            tool_lookup=self._action_sink._tool_lookup
            if self._action_sink is not None else None,
            resolved_params=resolved_params,
        )
        envelope = build_output_envelope(
            success=True, value=result.value,
            error=None, receipt=result.receipt,
        )
        if record is None:
            # Audit-build failed — still advance cursor + persist
            # next_step_index so the branch is honored on restart.
            async def _body(db):
                await db.execute(
                    "UPDATE workflow_executions "
                    "SET action_index_completed = ?, next_step_index = ?, "
                    "last_heartbeat = ? "
                    "WHERE execution_id = ? AND action_index_completed < ?",
                    (
                        idx, next_step_index, _now(),
                        execution.execution_id, idx,
                    ),
                )
            await self._run_workflow_txn(_body)
            execution.action_index_completed = idx
            execution.next_step_index = next_step_index
            return
        await self._run_workflow_txn(
            lambda db: _append_and_advance_with_branch(
                db, sink, record,
                step_index=idx, action_type=action.action_type,
                next_step_index=next_step_index,
                step_output_envelope=envelope, step_id=action.id,
            ),
        )
        execution.action_index_completed = idx
        execution.next_step_index = next_step_index

    async def _append_success_and_persist_gate_nonce(
        self,
        execution: WorkflowExecution,
        sink: WorkflowExecutionActionSink,
        idx: int,
        action: ActionDescriptor,
        result: ActionResult,
        *,
        gate_nonce: str,
        resolved_params: dict | None = None,
    ) -> None:
        record = _safe_build_record(
            execution=execution, step_index=idx, action=action,
            execution_state="completed", result=result,
            tool_lookup=self._action_sink._tool_lookup
            if self._action_sink is not None else None,
            resolved_params=resolved_params,
        )
        envelope = build_output_envelope(
            success=True, value=result.value,
            error=None, receipt=result.receipt,
        )
        if record is None:
            await self._run_workflow_txn(
                lambda db: _persist_gate_nonce_only(
                    db, execution.execution_id, gate_nonce=gate_nonce,
                ),
            )
            return
        await self._run_workflow_txn(
            lambda db: _append_and_persist_gate_nonce(
                db, sink, record,
                step_index=idx, action_type=action.action_type,
                gate_nonce=gate_nonce,
                step_output_envelope=envelope, step_id=action.id,
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
        resolved_params: dict | None = None,
    ) -> None:
        record = _safe_build_record(
            execution=execution, step_index=idx, action=action,
            execution_state="failed", error=error,
            tool_lookup=self._action_sink._tool_lookup
            if self._action_sink is not None else None,
            resolved_params=resolved_params,
        )
        # Per-outcome envelope for continue-on-failure (Decision 2 v2).
        envelope = build_output_envelope(
            success=False, value=None, error=error,
            receipt={},
        )
        if record is None:
            await self._run_workflow_txn(
                lambda db: _advance_cursor_only(
                    db, execution.execution_id, step_index=idx,
                ),
            )
            execution.action_index_completed = idx
            execution.next_step_index = -1
            return
        inserted = await self._run_workflow_txn(
            lambda db: _append_and_advance(
                db, sink, record,
                step_index=idx, action_type=action.action_type,
                step_output_envelope=envelope, step_id=action.id,
            ),
        )
        execution.action_index_completed = idx
        execution.next_step_index = -1
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
        resolved_params: dict | None = None,
    ) -> None:
        record = _safe_build_record(
            execution=execution, step_index=idx, action=action,
            execution_state="failed", error=error,
            tool_lookup=self._action_sink._tool_lookup
            if self._action_sink is not None else None,
            resolved_params=resolved_params,
        )
        # Per-outcome envelope for aborting failure (Decision 2 v2).
        envelope = build_output_envelope(
            success=False, value=None, error=error,
            receipt={},
        )
        if record is None:
            await self._run_workflow_txn(
                lambda db: _abort_state_only(
                    db, execution.execution_id,
                    aborted_reason=aborted_reason,
                ),
            )
            execution.state = "aborted"
            execution.aborted_reason = aborted_reason
            execution.terminated_at = _now()
            return
        inserted = await self._run_workflow_txn(
            lambda db: _append_and_abort(
                db, sink, record,
                step_index=idx, action_type=action.action_type,
                aborted_reason=aborted_reason,
                step_output_envelope=envelope, step_id=action.id,
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
    ) -> tuple[bool, dict | None]:
        """Pause until an approval event matches the gate predicate AND
        carries the engine-minted gate_nonce + execution_id, or timeout.

        WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 6 (Codex round-1
        High 5): returns ``(continue, matched_event_payload)`` so
        the caller can thread the payload into
        ``_clear_gate_and_advance`` for atomic capture. Returns
        ``(False, None)`` if the gate timed out and the timeout
        handler chose abort; ``(True, synthesized_payload)`` if the
        timeout chose ``auto_proceed_with_default``;
        ``(True, matched_payload)`` on real approval.

        WLP-GATE-SCOPING C1: emits ``workflow.execution_paused_at_gate``
        with the full gate descriptor + the engine-minted gate_nonce.
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
        nonce_for_cache = execution.gate_nonce
        try:
            await asyncio.wait_for(ev.wait(), timeout=gate.timeout_seconds)
        except asyncio.TimeoutError:
            self._gate_waiters.pop(execution.execution_id, None)
            self._gate_predicates.pop(execution.execution_id, None)
            self._gate_event_types.pop(execution.execution_id, None)
            self._gate_nonces.pop(execution.execution_id, None)
            self._gate_release_payloads.pop(execution.execution_id, None)
            self._predicate_resolution_cache.pop(
                (execution.execution_id, nonce_for_cache), None,
            )
            cont = await self._handle_gate_timeout(execution, gate)
            if not cont:
                return False, None
            # auto_proceed_with_default: synthesize a payload so
            # subsequent steps' references to {gate.<name>.output.*}
            # still resolve. The synthesized payload carries the
            # gate's default_value plus a timed_out flag.
            return True, {
                "timed_out": True,
                "default_value": gate.default_value,
                "gate_name": gate.gate_name,
            }
        finally:
            self._gate_waiters.pop(execution.execution_id, None)
            self._gate_predicates.pop(execution.execution_id, None)
            self._gate_event_types.pop(execution.execution_id, None)
            self._gate_nonces.pop(execution.execution_id, None)
            self._predicate_resolution_cache.pop(
                (execution.execution_id, nonce_for_cache), None,
            )
        # Read + pop the matched event payload that the post-flush
        # match logic wrote before signalling the waiter (Decision 6 /
        # Codex round-1 High 5).
        matched_payload = self._gate_release_payloads.pop(
            execution.execution_id, None,
        )
        await event_stream.emit(
            execution.instance_id, "workflow.execution_resumed",
            {"execution_id": execution.execution_id,
             "gate_name": gate.gate_name},
            correlation_id=execution.correlation_id,
        )
        return True, matched_payload

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
            # WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 8: resolve
            # template references in the predicate's value: fields
            # via the resolver. Cache per (execution_id, gate_nonce).
            resolved_predicate = await self._resolve_predicate_for_gate(
                execution_id, expected_nonce, predicate,
            )
            if resolved_predicate is None:
                # Reference resolution failed (e.g., referenced step
                # output not yet present). Predicate evaluation returns
                # False; gate stays paused; this evaluation pass skips
                # the gate. Subsequent passes will re-try.
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
                    if evaluate_predicate(resolved_predicate, event):
                        # WORKFLOW-ORCHESTRATION-PRIMITIVES-V1
                        # Decision 6 / Codex round-1 High 5: write
                        # the matched event payload to the buffer
                        # BEFORE signalling the waiter so
                        # _await_gate can read it on wake-up.
                        self._gate_release_payloads[execution_id] = dict(payload)
                        waiter.set()
                        break
                except Exception:
                    pass

    async def _resolve_predicate_for_gate(
        self,
        execution_id: str,
        gate_nonce: str,
        predicate: dict,
    ) -> dict | None:
        """Decision 8: resolve template references inside a predicate
        AST's ``value:`` fields. Cached per (execution_id,
        gate_nonce).

        Returns the resolved AST (suitable for evaluate_predicate),
        OR None if any reference couldn't resolve — caller treats
        None as "no match this evaluation pass".
        """
        cache_key = (execution_id, gate_nonce)
        if cache_key in self._predicate_resolution_cache:
            return self._predicate_resolution_cache[cache_key]
        # Build a ResolutionContext for predicate evaluation. Mode
        # 'predicate' makes the resolver return _NOT_FOUND instead of
        # raising on unresolved references.
        execution = await self._fetch_execution_row(execution_id)
        if execution is None:
            return None
        try:
            assert self._db is not None
            step_outputs, gate_outputs = await load_workflow_outputs(
                self._db, execution.instance_id, execution.execution_id,
            )
        except Exception:
            return None
        ctx = ResolutionContext(
            execution=execution,
            trigger_payload=execution.trigger_event_payload or {},
            step_outputs=step_outputs,
            gate_outputs=gate_outputs,
            mode="predicate",
        )
        resolved = _resolve_predicate_ast(predicate, ctx)
        if resolved is None:
            return None
        self._predicate_resolution_cache[cache_key] = resolved
        return resolved

    async def _fetch_execution_row(
        self, execution_id: str,
    ) -> WorkflowExecution | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM workflow_executions WHERE execution_id = ? LIMIT 1",
            (execution_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return WorkflowExecution.from_row(row)

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
                # Codex round-2-impl Low 8: track whether BEGIN
                # IMMEDIATE actually succeeded so we know whether to
                # rollback before a retry. The previous shape only
                # rolled back on a body-raised exception, leaving a
                # COMMIT-time "database is locked" with the
                # transaction potentially still open on the next
                # retry's BEGIN.
                in_txn = False
                try:
                    await self._db.execute("BEGIN IMMEDIATE")
                    in_txn = True
                    try:
                        value = await body(self._db)
                    except Exception:
                        try:
                            await self._db.execute("ROLLBACK")
                        except aiosqlite.OperationalError:
                            pass
                        in_txn = False
                        raise
                    await self._db.execute("COMMIT")
                    in_txn = False
                    return value
                except aiosqlite.OperationalError as exc:
                    if in_txn:
                        # Any post-BEGIN OperationalError (most
                        # commonly COMMIT-time lock contention) leaves
                        # the transaction open. Rollback before
                        # retrying so the next BEGIN IMMEDIATE doesn't
                        # raise "cannot start a transaction within a
                        # transaction".
                        try:
                            await self._db.execute("ROLLBACK")
                        except aiosqlite.OperationalError:
                            pass
                        in_txn = False
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

    async def _run_workflow_write(
        self,
        body: Callable[[aiosqlite.Connection], Awaitable[Any]],
    ) -> Any:
        """Codex round-2-impl High 4: every workflow_executions write
        — single-statement or multi-statement — must serialize through
        the engine's write-lock so it can't interleave inside another
        coroutine's open BEGIN IMMEDIATE. Direct ``self._db.execute``
        calls would otherwise commit alongside (or roll back with) an
        unrelated transaction.

        For single-statement writes that don't need explicit-
        transaction atomicity, this thin helper holds the lock for the
        duration of the body without issuing BEGIN / COMMIT (auto-
        commit mode handles the durability boundary).

        For multi-statement atomic writes, callers route through
        ``_run_workflow_txn`` instead, which adds the BEGIN IMMEDIATE
        boundary plus rollback + busy-retry discipline.
        """
        assert self._db is not None
        async with self._workflow_db_write_lock:
            return await body(self._db)

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
                await self._run_workflow_write(
                    lambda db: db.execute(
                        "UPDATE workflow_executions "
                        "SET action_index_completed = ?, last_heartbeat = ? "
                        "WHERE execution_id = ? AND action_index_completed < ?",
                        (max_step, _now(), execution.execution_id, max_step),
                    ),
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

    async def _is_pending_gate_resume(
        self,
        execution: WorkflowExecution,
        wf: Workflow,
    ) -> bool:
        """Codex round-2-impl Blocker 1 + Spec 4 post-impl Blocker 2:
        detect pending-gate restart state via global step ordinal.

        The execution is mid-execution at a step whose ``gate_ref`` is
        set; the action already ran and its record is persisted;
        ``gate_nonce`` is set on the execution row (gated-success
        atomic boundary). The step to resume at is either the
        ``next_step_index`` override (if a branch verb routed to this
        gated step) OR the natural ``action_index_completed + 1``.
        Restart must re-enter ``_await_gate`` for that step, NOT
        re-execute it.
        """
        if not execution.gate_nonce:
            return False
        if self._action_sink is None:
            return False
        # Spec 4 post-impl Blocker 2: use global action_by_index +
        # next_step_index when set so branch-to-gated targets are
        # resume-correct.
        action_by_index = self._build_action_by_index(wf)
        if execution.next_step_index >= 0:
            gate_step_index = execution.next_step_index
        else:
            gate_step_index = execution.action_index_completed + 1
        if gate_step_index not in action_by_index:
            return False
        gate_action = action_by_index[gate_step_index]
        if gate_action.gate_ref is None:
            return False
        existing = await self._action_sink.get_by_step(
            execution.instance_id, execution.execution_id,
            step_index=gate_step_index,
        )
        if existing is None:
            return False
        return existing.record.execution_state == "completed"

    async def _restart_resume_pass(self) -> None:
        """Spec 4 post-impl Blocker 1: honor durable branch decisions
        via execution.next_step_index. Resolve via the global
        action_by_index map so terminal-branch targets resume
        correctly across crash boundaries.
        """
        assert self._db is not None
        assert self._workflow_registry is not None
        async with self._db.execute(
            "SELECT * FROM workflow_executions WHERE state = 'running'"
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            execution = WorkflowExecution.from_row(row)
            wf = await self._workflow_registry.get_workflow(execution.workflow_id)
            if wf is None:
                await self._abort(execution, "aborted_by_restart")
                continue
            # Pending-gate restart branch (Codex round-2-impl Blocker 1
            # + Spec 4 post-impl Blocker 2): always resume regardless
            # of action's resume_safe flag — the step already ran; we
            # just need to wait on approval again.
            if await self._is_pending_gate_resume(execution, wf):
                next_idx = (
                    execution.next_step_index
                    if execution.next_step_index >= 0
                    else execution.action_index_completed + 1
                )
                await event_stream.emit(
                    execution.instance_id, "workflow.execution_resumed",
                    {"execution_id": execution.execution_id,
                     "reason": "restart_resume_pending_gate",
                     "from_step": next_idx},
                    correlation_id=execution.correlation_id,
                )
                self._queue.put_nowait(execution)
                continue
            # Spec 4 post-impl Blocker 1: durable-branch restart.
            # next_step_index, if set, was persisted atomically with a
            # branch step's record commit. Use it as the candidate for
            # the next step instead of the natural cursor advance.
            action_by_index = self._build_action_by_index(wf)
            terminal_ranges = self._build_terminal_branch_ranges(wf)
            if execution.next_step_index >= 0:
                next_step_index = execution.next_step_index
            else:
                next_step_index = execution.action_index_completed + 1
            # Terminal-branch resume policy (Decision 9 Option b): if
            # the resume target is inside a terminal branch AND
            # next_step_index is NOT set (i.e., we were already
            # executing terminal-branch steps before the crash), abort
            # cleanly. Honor next_step_index for the branch-entry case
            # (target step hasn't run yet).
            in_terminal = self._terminal_branch_for_step(
                next_step_index, terminal_ranges,
            )
            if in_terminal and execution.next_step_index < 0:
                await self._abort(
                    execution,
                    f"aborted_by_restart_mid_terminal_branch:{in_terminal}",
                )
                continue
            if next_step_index in action_by_index:
                action_at_target = action_by_index[next_step_index]
                # Allow durable-branch resume regardless of resume_safe
                # flag — the branch decision was committed and the
                # target hasn't executed yet, so resuming at it is
                # equivalent to the engine never having crashed.
                durable_branch = execution.next_step_index >= 0
                if durable_branch or action_at_target.resume_safe:
                    reason = (
                        "restart_resume_durable_branch"
                        if durable_branch else "restart_resume"
                    )
                    await event_stream.emit(
                        execution.instance_id, "workflow.execution_resumed",
                        {"execution_id": execution.execution_id,
                         "reason": reason,
                         "from_step": next_step_index},
                        correlation_id=execution.correlation_id,
                    )
                    self._queue.put_nowait(execution)
                    continue
            # Conservative default: not resume-safe → abort.
            await self._abort(execution, "aborted_by_restart")

    # -- audit + persistence helpers -----------------------------------

    async def _update_state(
        self, execution: WorkflowExecution, state: str,
    ) -> None:
        execution.state = state
        execution.last_heartbeat = _now()
        await self._run_workflow_write(
            lambda db: db.execute(
                "UPDATE workflow_executions SET state = ?, last_heartbeat = ? "
                "WHERE execution_id = ?",
                (state, execution.last_heartbeat, execution.execution_id),
            ),
        )

    async def _persist_gate_nonce(
        self, execution: WorkflowExecution, nonce: str,
    ) -> None:
        """Record the gate_nonce against the running execution row so
        post-flush match logic can require it on incoming approvals.
        Called after the gate_ref action completes successfully —
        unsuccessful actions discard the unused nonce instead.

        v-final post-impl: this method is preserved for callers that
        need standalone nonce persistence outside the per-outcome
        matrix (e.g., legacy code paths). The matrix-aware success
        path uses ``_append_and_persist_gate_nonce`` which lands the
        record + nonce in one transaction.
        """
        execution.gate_nonce = nonce
        await self._run_workflow_write(
            lambda db: db.execute(
                "UPDATE workflow_executions SET gate_nonce = ?, last_heartbeat = ? "
                "WHERE execution_id = ?",
                (nonce, _now(), execution.execution_id),
            ),
        )

    async def _clear_gate_and_advance(
        self, execution: WorkflowExecution, idx: int,
        *,
        gate_name: str = "",
        gate_output_payload: dict | None = None,
    ) -> bool:
        """Codex round-2-impl Blocker 2: atomic gate release. Clear
        gate_nonce AND advance action_index_completed in one BEGIN
        IMMEDIATE transaction so a crash between the two writes can't
        leave the execution in a state where restart logic would
        re-execute step ``idx``.

        Defensive nonce-guard: the WHERE clause includes the
        expected gate_nonce so two concurrent gate resolutions for
        the same execution can't both advance.

        Spec 4 post-impl Medium 8: returns the bool result from
        ``_clear_gate_nonce_and_advance``. False indicates the
        expected nonce no longer matched (the row's gate_nonce
        diverged from the in-memory execution); in that case the
        in-memory advance is NOT applied and the caller is expected
        to reload state. True means the atomic transition
        succeeded; in-memory state is advanced.

        WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 6 (Codex round
        1 High 5): when ``gate_name`` + ``gate_output_payload`` are
        supplied, atomically capture the satisfying event's payload
        into workflow_step_outputs (output_kind='gate'). The capture
        + the gate release land in the same transaction.
        """
        expected_nonce = execution.gate_nonce
        updated = await self._run_workflow_txn(
            lambda db: _clear_gate_nonce_and_advance(
                db, execution.execution_id,
                step_index=idx, expected_nonce=expected_nonce,
                gate_name=gate_name,
                gate_output_payload=gate_output_payload,
                instance_id=execution.instance_id,
            ),
        )
        if updated:
            execution.gate_nonce = ""
            execution.action_index_completed = idx
            execution.next_step_index = -1
        else:
            # Stale-nonce divergence (Spec 4 post-impl Medium 8):
            # the row's gate_nonce no longer matches what we expected;
            # another path resolved the gate or aborted the execution.
            # Do NOT advance the in-memory execution.
            logger.warning(
                "WORKFLOW_GATE_RELEASE_STALE_NONCE execution_id=%s "
                "step=%d expected_nonce=%s — reloading state",
                execution.execution_id, idx, expected_nonce,
            )
        return bool(updated)

    async def _clear_gate_nonce(
        self, execution: WorkflowExecution,
    ) -> None:
        """Clear the gate_nonce after the execution resumes from a
        gate. Preserved for any caller that explicitly wants nonce
        clearing without advancing the cursor; v-final's gate-release
        flow uses ``_clear_gate_and_advance`` to land both writes
        atomically. Stale-nonce-rejection (AC #13) relies on this —
        once cleared, a replayed approval event carrying the old
        nonce finds no waiter to wake."""
        execution.gate_nonce = ""
        await self._run_workflow_write(
            lambda db: db.execute(
                "UPDATE workflow_executions SET gate_nonce = '', "
                "last_heartbeat = ? WHERE execution_id = ?",
                (_now(), execution.execution_id),
            ),
        )

    async def _mark_step_complete(
        self, execution: WorkflowExecution, idx: int,
    ) -> None:
        execution.action_index_completed = idx
        await self._run_workflow_write(
            lambda db: db.execute(
                "UPDATE workflow_executions SET action_index_completed = ?, "
                "last_heartbeat = ? WHERE execution_id = ?",
                (idx, _now(), execution.execution_id),
            ),
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
        execution.state = "aborted"
        execution.aborted_reason = reason
        execution.terminated_at = _now()
        await self._run_workflow_write(
            lambda db: db.execute(
                "UPDATE workflow_executions SET state = ?, aborted_reason = ?, "
                "terminated_at = ? WHERE execution_id = ?",
                ("aborted", reason, execution.terminated_at,
                 execution.execution_id),
            ),
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
        execution.state = "completed"
        execution.terminated_at = _now()
        await self._run_workflow_write(
            lambda db: db.execute(
                "UPDATE workflow_executions SET state = ?, terminated_at = ? "
                "WHERE execution_id = ?",
                ("completed", execution.terminated_at,
                 execution.execution_id),
            ),
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
            # Codex round-2-impl High 4: route through the engine
            # write-lock so a concurrent _run_workflow_txn body
            # can't commit this INSERT along with unrelated step
            # work.
            await self._run_workflow_write(
                lambda db: db.execute(
                    "INSERT INTO workflow_executions ("
                    " execution_id, workflow_id, instance_id, correlation_id,"
                    " state, action_index_completed, intermediate_state,"
                    " last_heartbeat, aborted_reason, started_at, terminated_at,"
                    " trigger_event_payload, trigger_event_id, member_id,"
                    " gate_nonce, fire_id"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    execution.to_row(),
                ),
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
