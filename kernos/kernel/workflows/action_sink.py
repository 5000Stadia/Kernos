"""Workflow step ActionStateRecord persistence.

ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1 ships per-execution sinks
that persist an ``ActionStateRecord`` per workflow step alongside
the existing ``workflow.*`` event_stream emissions and ledger
appends. Substrate-fidelity discipline that RESPONSE-FIDELITY-V1
made load-bearing for turn-scoped actions extends to workflow-
scoped actions here.

Design (v-final after Codex round 2 fold):

* Per-execution wrappers (``WorkflowExecutionActionSink``) bind
  ``instance_id``, ``workflow_execution_id``, ``workflow_id``,
  ``correlation_id``, and ``member_id`` at construction time from
  the parent ``WorkflowExecution``. Callers do NOT pass these on
  ``append()`` — the writer invariant prevents cross-partition
  leakage.

* The sink shares the engine's ``aiosqlite`` connection so the
  per-outcome transaction matrix in Decision 6 (record append +
  matching state mutation) can land in one transaction. Engine-side
  ``_run_workflow_txn`` serializes BEGIN IMMEDIATE / COMMIT
  boundaries via ``asyncio.Lock``.

* Per-outcome SQL helpers (``_append_and_advance``,
  ``_append_and_persist_gate_nonce``, ``_append_and_abort``) issue
  the record INSERT + the matching ``workflow_executions`` UPDATE
  inside the caller's transaction. The caller is
  ``ExecutionEngine._run_workflow_txn(body)``; these helpers MUST
  NOT call BEGIN / COMMIT themselves.

* ``INSERT ... ON CONFLICT(instance_id, workflow_execution_id,
  step_index) DO NOTHING`` is the resume-safe idempotency primitive.
  PK conflict → ``cursor.rowcount == 0`` → False; non-PK constraint
  failures raise normally.

* FRICTION-PATTERN composition runs AFTER the transaction commits
  and ONLY when the insert actually happened (idempotency-skip
  paths do not double-count). The lifecycle dispatch mirrors
  ``FrictionObserver._classify_and_record``:

      active / reactivated   → ``record_occurrence``
      resolved               → ``record_recurrence``
      no match / archived    → ``workflow.friction_pattern_unclassified``
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import aiosqlite

from kernos.kernel.integration.briefing import ActionStateRecord

if TYPE_CHECKING:
    from kernos.kernel.friction_patterns import FrictionPatternStore
    from kernos.kernel.workflows.action_library import ActionResult
    from kernos.kernel.workflows.execution_engine import WorkflowExecution
    from kernos.kernel.workflows.workflow_registry import ActionDescriptor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Operation-class + risk-level derivation
# ---------------------------------------------------------------------------


# Workflow verb → ``operation_class`` (member of ACTION_OPERATION_CLASSES).
# Direct-effect verbs map to ``mutate`` per architect Q3. World-effect
# verbs map per their substrate flavour. ``call_tool`` is resolved
# separately because it wraps an arbitrary tool whose effect varies.
ACTION_OPERATION_CLASS_BY_VERB: dict[str, str] = {
    "mark_state": "mutate",
    "append_to_ledger": "mutate",
    "notify_user": "send",
    "write_canvas": "mutate",
    "route_to_agent": "register",
    "post_to_service": "send",
    "request_approval": "register",
    # Spec 4 post-impl Medium 6: branch verb explicitly classified
    # as mutate (control-flow mutation of next_step_index).
    "branch": "mutate",
    # call_tool resolved by _operation_class_for_call_tool
}


# operation_class → risk_level (members of ACTION_RISK_LEVELS).
# Per architect Q4: derive risk from operation_class, not from the
# raw action verb.
RISK_LEVEL_BY_OPERATION_CLASS: dict[str, str] = {
    "read": "low",
    "propose": "low",
    "mutate": "medium",
    "delete": "high",
    "send": "medium",
    "schedule": "medium",
    "register": "medium",
    "manage": "medium",
}


# Default operation_class + risk_level when ``call_tool`` wraps a
# tool with no registry metadata. Biases conservative so audit
# consumers never see an under-classified default that hides a
# substrate mutation. ``missing_metadata=True`` signals the
# fallback.
_CALL_TOOL_DEFAULT_OPERATION_CLASS = "mutate"
_CALL_TOOL_DEFAULT_RISK_LEVEL = "medium"


# Tool registry lookup. The engine wires a callable that returns
# ``operation_class`` for a given tool name, or None when the tool
# has no declared metadata. Kept as an injection point because the
# tool registry (KERNEL-TOOL-REGISTRY-V1) is being prepared in
# parallel; v1 of this spec ships without depending on a concrete
# registry shape.
ToolOperationClassLookup = Callable[[str], str | None]


class GateReleaseMissingStepOutput(RuntimeError):
    """Raised when a gate release cannot find its requesting step row."""

    def __init__(self, step_id: str) -> None:
        super().__init__(f"gate_release_missing_step_output:{step_id}")
        self.step_id = step_id


def _operation_class_for_action_type(
    action_type: str,
    params: dict | None = None,
    *,
    tool_lookup: ToolOperationClassLookup | None = None,
) -> tuple[str, bool]:
    """Return ``(operation_class, missing_metadata)`` for an action.

    ``missing_metadata`` is True only when ``call_tool`` falls back
    to the default because the wrapped tool has no declared class.
    """
    if action_type == "call_tool":
        params = params or {}
        # Codex round-2-impl Medium 5: the workflow CallToolAction
        # descriptor uses ``tool_id`` for the tool key (see
        # action_library.CallToolAction). Check that first; keep
        # ``tool_name`` / ``name`` as fallbacks for shapes a future
        # descriptor flavour might use.
        tool_key = (
            params.get("tool_id")
            or params.get("tool_name")
            or params.get("name")
            or ""
        )
        if tool_lookup is not None and tool_key:
            declared = tool_lookup(tool_key)
            if declared:
                return declared, False
        return _CALL_TOOL_DEFAULT_OPERATION_CLASS, True
    mapped = ACTION_OPERATION_CLASS_BY_VERB.get(action_type)
    if mapped is not None:
        return mapped, False
    # Unknown action verb. Defensive: bias mutate / missing_metadata.
    return _CALL_TOOL_DEFAULT_OPERATION_CLASS, True


def _risk_level_for_operation_class(operation_class: str) -> str:
    return RISK_LEVEL_BY_OPERATION_CLASS.get(
        operation_class, _CALL_TOOL_DEFAULT_RISK_LEVEL,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Storage row dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowActionRecord:
    """Storage-side row carrying an ``ActionStateRecord`` plus workflow
    context. The wrapped record is unchanged; this dataclass is the
    on-disk row shape.
    """

    instance_id: str
    workflow_execution_id: str
    step_index: int
    action_id: str
    workflow_id: str
    action_type: str
    record: ActionStateRecord
    correlation_id: str
    recorded_at: str


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_WORKFLOW_ACTION_RECORDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_action_records (
    instance_id             TEXT NOT NULL,
    workflow_execution_id   TEXT NOT NULL,
    step_index              INTEGER NOT NULL,
    action_id               TEXT NOT NULL,
    workflow_id             TEXT NOT NULL DEFAULT '',
    action_type             TEXT NOT NULL,
    record_json             TEXT NOT NULL,
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
"""


async def ensure_workflow_action_records_schema(
    db: aiosqlite.Connection,
) -> None:
    """Create the ``workflow_action_records`` table and indexes.

    Called by ``ExecutionEngine._ensure_schema`` after the existing
    ``workflow_executions`` ensure + ALTER migrations so the FK
    target column already exists.
    """
    await db.execute("PRAGMA foreign_keys=ON")
    for stmt in _WORKFLOW_ACTION_RECORDS_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            await db.execute(stmt)


# ---------------------------------------------------------------------------
# Record construction
# ---------------------------------------------------------------------------


_WORKFLOW_STEP_SURFACE = "workflow_step"


def _build_action_state_record(
    *,
    execution: "WorkflowExecution",
    step_index: int,
    action: "ActionDescriptor",
    execution_state: str,
    result: "ActionResult | None" = None,
    error: str = "",
    tool_lookup: ToolOperationClassLookup | None = None,
    resolved_params: dict | None = None,
) -> ActionStateRecord:
    """Construct an ``ActionStateRecord`` for a workflow step.

    Spec 4 post-impl Medium 7: ``resolved_params`` carries the
    post-reference-resolution parameter dict so the record captures
    the values actually passed to the verb (NOT the templated
    descriptor strings). Used for both audit (param_resolved refs)
    AND tool-id-aware operation_class lookup for call_tool whose
    tool_id might be a template reference.

    Direct mapping:
        action_id           → uuid-prefixed ``act_<hex>``
        surface             → ``"workflow_step"``
        operation           → ``action.action_type``
        operation_class     → derived (see _operation_class_for_action_type;
                              uses resolved_params for call_tool)
        authorization_state → ``"not_required"`` UNIFORMLY (Decision 5;
                              gate state stays on event_stream events)
        execution_state     → caller-provided (``completed`` | ``failed``)
        receipt_refs        → upstream tool refs + stable provenance tag +
                              ``param_resolved:<path>:<summary>`` refs +
                              for branch: ``branch_target`` + ``condition_value``
        affected_objects    → IDs from the step's result receipt when present
        user_visible_summary→ success summary OR the failure error string;
                              branch verb gets a goto-specific summary
        risk_level          → derived from operation_class (Decision 5)
        missing_metadata    → True only when call_tool falls back to default
    """
    # Use resolved_params for the lookup when available (Spec 4
    # post-impl Medium 7); falls back to descriptor params for
    # legacy callers.
    lookup_params = resolved_params if resolved_params is not None else action.parameters
    op_class, missing_metadata = _operation_class_for_action_type(
        action.action_type,
        lookup_params,
        tool_lookup=tool_lookup,
    )
    risk_level = _risk_level_for_operation_class(op_class)

    receipt_refs: list[str] = []
    affected_objects: list[str] = []
    if result is not None and result.receipt:
        receipt = result.receipt
        # Codex round-2-impl Medium 6: the action library emits a
        # variety of receipt keys depending on verb. Iterate over the
        # known keys and capture EACH that's populated so the record
        # carries the tool-side provenance, not just a single ref.
        #   write_canvas      → canvas_id, mode, wrote_at
        #   route_to_agent    → persisted_id
        #   call_tool         → tool_id, called_at
        #   post_to_service   → service_id, posted_at
        #   notify_user       → delivered_at (value=persisted_id)
        #   branch            → branched_to, condition_value
        for key in (
            "tool_id", "persisted_id", "canvas_id", "service_id",
            "receipt_id", "id", "delivered_at", "called_at",
            "wrote_at", "posted_at",
            "branched_to", "condition_value",
        ):
            value = receipt.get(key)
            if value is not None and value != "":
                receipt_refs.append(f"{action.action_type}:{key}:{value}")
        # ActionResult.value carries the tool-side primary handle for
        # notify_user (the receipt). Capture if it's hashable/scalar.
        if result.value and isinstance(result.value, (str, int)):
            receipt_refs.append(f"{action.action_type}:value:{result.value}")
        for key in (
            "affected_object_id", "object_id", "subject_id",
            "tool_id", "canvas_id", "service_id", "persisted_id",
        ):
            value = receipt.get(key)
            if value:
                affected_objects.append(str(value))
    # Spec 4 post-impl Medium 7: resolved parameter audit. For each
    # parameter that contained a template reference (i.e., the
    # resolved value differs from the descriptor value), capture
    # the resolved value as a receipt ref so audit consumers see
    # what was actually passed to the verb.
    if resolved_params is not None:
        for key, descriptor_value in (action.parameters or {}).items():
            resolved_value = resolved_params.get(key)
            if resolved_value != descriptor_value:
                # Truncate value summary for readability.
                summary_value = (
                    str(resolved_value)[:64] if resolved_value is not None
                    else "null"
                )
                receipt_refs.append(
                    f"param_resolved:{key}:{summary_value}"
                )
    # Decision 1 escape hatch: stable provenance tag so a bare
    # ActionStateRecord surfaced from a workflow path is still
    # routable back to its workflow context.
    receipt_refs.append(
        f"workflow:{execution.execution_id}:step:{step_index}"
    )

    if execution_state == "completed":
        # Spec 4 post-impl Medium 6: branch verb gets a
        # goto-specific summary so audit shows the routing choice.
        if action.action_type == "branch" and result is not None:
            target = result.receipt.get("branched_to", "?") if result.receipt else "?"
            cond = (
                result.receipt.get("condition_value", "?")
                if result.receipt else "?"
            )
            summary = (
                f"branch evaluated condition={cond} → {target} "
                f"(step {step_index})"
            )
        else:
            summary = (
                f"workflow step {step_index} ({action.action_type}) completed"
            )
    elif execution_state == "failed":
        summary = error or "workflow_step_failed"
    else:
        summary = f"workflow step {step_index} ({action.action_type})"

    return ActionStateRecord(
        action_id=f"act_{uuid.uuid4().hex}",
        surface=_WORKFLOW_STEP_SURFACE,
        operation=action.action_type,
        operation_class=op_class,
        authorization_state="not_required",
        execution_state=execution_state,
        receipt_refs=tuple(receipt_refs),
        affected_objects=tuple(affected_objects),
        partial_state=None,
        user_visible_summary=summary,
        risk_level=risk_level,
        evidence_class="",
        missing_metadata=missing_metadata,
    )


def _serialize_record(record: ActionStateRecord) -> str:
    payload = {
        "action_id": record.action_id,
        "surface": record.surface,
        "operation": record.operation,
        "operation_class": record.operation_class,
        "authorization_state": record.authorization_state,
        "execution_state": record.execution_state,
        "receipt_refs": list(record.receipt_refs),
        "affected_objects": list(record.affected_objects),
        "partial_state": record.partial_state,
        "user_visible_summary": record.user_visible_summary,
        "risk_level": record.risk_level,
        "evidence_class": record.evidence_class,
        "missing_metadata": record.missing_metadata,
    }
    return json.dumps(payload)


def _deserialize_record(record_json: str) -> ActionStateRecord:
    raw = json.loads(record_json)
    return ActionStateRecord(
        action_id=raw["action_id"],
        surface=raw["surface"],
        operation=raw["operation"],
        operation_class=raw["operation_class"],
        authorization_state=raw["authorization_state"],
        execution_state=raw["execution_state"],
        receipt_refs=tuple(raw.get("receipt_refs", ())),
        affected_objects=tuple(raw.get("affected_objects", ())),
        partial_state=raw.get("partial_state"),
        user_visible_summary=raw.get("user_visible_summary", ""),
        risk_level=raw.get("risk_level", "low"),
        evidence_class=raw.get("evidence_class", ""),
        missing_metadata=bool(raw.get("missing_metadata", False)),
    )


def _row_to_workflow_record(row: aiosqlite.Row) -> WorkflowActionRecord:
    return WorkflowActionRecord(
        instance_id=row["instance_id"],
        workflow_execution_id=row["workflow_execution_id"],
        step_index=row["step_index"],
        action_id=row["action_id"],
        workflow_id=row["workflow_id"],
        action_type=row["action_type"],
        record=_deserialize_record(row["record_json"]),
        correlation_id=row["correlation_id"],
        recorded_at=row["recorded_at"],
    )


# ---------------------------------------------------------------------------
# Sink (engine-shared connection)
# ---------------------------------------------------------------------------


# Type alias for the post-flush event_stream emitter the sink uses
# for ``workflow.friction_pattern_unclassified``. Matches the
# kernos.kernel.event_stream.emit shape:
#   emit(instance_id, event_type, payload, *, correlation_id=...,
#        member_id=...)
EventStreamEmitter = Callable[..., Awaitable[None]]


class WorkflowActionSink:
    """Persistence + classifier-hook surface for workflow step
    ActionStateRecords.

    One sink per engine; shares the engine's ``aiosqlite`` connection
    so atomic transactions can span both ``workflow_action_records``
    and ``workflow_executions``. Per-execution wrappers
    (``WorkflowExecutionActionSink``) carry the bound execution
    identity.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        *,
        pattern_store: "FrictionPatternStore | None" = None,
        emit_event: EventStreamEmitter | None = None,
        tool_lookup: ToolOperationClassLookup | None = None,
    ) -> None:
        self._db = db
        self._pattern_store = pattern_store
        self._emit_event = emit_event
        self._tool_lookup = tool_lookup

    @property
    def db(self) -> aiosqlite.Connection:
        return self._db

    async def ensure_schema(self) -> None:
        await ensure_workflow_action_records_schema(self._db)

    def for_execution(
        self,
        execution: "WorkflowExecution",
        *,
        member_id: str = "",
    ) -> "WorkflowExecutionActionSink":
        """Construct a per-execution wrapper. Execution identity is
        BOUND from ``execution``; callers MUST NOT supply
        ``instance_id`` / ``workflow_execution_id`` /
        ``workflow_id`` / ``correlation_id`` on append.
        """
        return WorkflowExecutionActionSink(
            parent=self,
            execution=execution,
            member_id=member_id or execution.member_id or "",
        )

    async def list_for_execution(
        self,
        instance_id: str,
        workflow_execution_id: str,
    ) -> list[WorkflowActionRecord]:
        async with self._db.execute(
            "SELECT * FROM workflow_action_records "
            "WHERE instance_id = ? AND workflow_execution_id = ? "
            "ORDER BY step_index ASC",
            (instance_id, workflow_execution_id),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_workflow_record(row) for row in rows]

    async def get_by_action_id(
        self,
        instance_id: str,
        action_id: str,
    ) -> WorkflowActionRecord | None:
        async with self._db.execute(
            "SELECT * FROM workflow_action_records "
            "WHERE instance_id = ? AND action_id = ? "
            "LIMIT 1",
            (instance_id, action_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_workflow_record(row) if row is not None else None

    async def get_by_step(
        self,
        instance_id: str,
        workflow_execution_id: str,
        step_index: int,
    ) -> WorkflowActionRecord | None:
        async with self._db.execute(
            "SELECT * FROM workflow_action_records "
            "WHERE instance_id = ? AND workflow_execution_id = ? "
            "AND step_index = ?",
            (instance_id, workflow_execution_id, step_index),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_workflow_record(row) if row is not None else None

    async def get_max_step_index(
        self,
        instance_id: str,
        workflow_execution_id: str,
    ) -> int | None:
        async with self._db.execute(
            "SELECT MAX(step_index) AS max_step FROM workflow_action_records "
            "WHERE instance_id = ? AND workflow_execution_id = ?",
            (instance_id, workflow_execution_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        value = row["max_step"]
        return int(value) if value is not None else None


# ---------------------------------------------------------------------------
# Per-execution wrapper
# ---------------------------------------------------------------------------


class WorkflowExecutionActionSink:
    """Per-execution append surface. Execution identity is bound at
    construction time; callers pass only the per-step payload
    (record + step_index + action_type).
    """

    def __init__(
        self,
        *,
        parent: WorkflowActionSink,
        execution: "WorkflowExecution",
        member_id: str = "",
    ) -> None:
        self._parent = parent
        # Bind from the parent execution. Callers cannot override.
        self._instance_id = execution.instance_id
        self._workflow_execution_id = execution.execution_id
        self._workflow_id = execution.workflow_id
        self._correlation_id = execution.correlation_id
        self._member_id = member_id or execution.member_id or ""

    @property
    def parent(self) -> WorkflowActionSink:
        return self._parent

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def workflow_execution_id(self) -> str:
        return self._workflow_execution_id

    @property
    def workflow_id(self) -> str:
        return self._workflow_id

    @property
    def correlation_id(self) -> str:
        return self._correlation_id

    @property
    def member_id(self) -> str:
        return self._member_id

    async def append(
        self,
        record: ActionStateRecord,
        *,
        step_index: int,
        action_type: str,
    ) -> bool:
        """Standalone append (outside the per-outcome matrix). Uses
        the parent sink's connection directly. Returns True if
        inserted, False on PK conflict. Friction composition fires
        when inserted and ``execution_state == "failed"``.

        Production paths inside the engine route through the
        per-outcome SQL helpers below (``_append_and_advance`` etc.)
        which call ``_insert_within_txn`` directly so the append +
        state mutation share one transaction.
        """
        db = self._parent.db
        inserted = await self._insert_within_txn(
            db, record, step_index=step_index, action_type=action_type,
        )
        if inserted and record.execution_state == "failed":
            await self._classify_friction(
                record=record, step_index=step_index, action_type=action_type,
            )
        return inserted

    async def _insert_within_txn(
        self,
        db: aiosqlite.Connection,
        record: ActionStateRecord,
        *,
        step_index: int,
        action_type: str,
    ) -> bool:
        """Issue the ON CONFLICT DO NOTHING insert inside the
        caller's transaction. Returns True if a row was inserted
        (rowcount == 1), False on PK conflict (rowcount == 0).
        Non-PK constraint failures raise normally because there is
        no ON CONFLICT branch for them.
        """
        cursor = await db.execute(
            "INSERT INTO workflow_action_records ("
            " instance_id, workflow_execution_id, step_index, action_id,"
            " workflow_id, action_type, record_json, correlation_id,"
            " recorded_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(instance_id, workflow_execution_id, step_index) "
            "DO NOTHING",
            (
                self._instance_id,
                self._workflow_execution_id,
                step_index,
                record.action_id,
                self._workflow_id,
                action_type,
                _serialize_record(record),
                self._correlation_id,
                _now(),
            ),
        )
        return cursor.rowcount == 1

    async def _classify_friction(
        self,
        *,
        record: ActionStateRecord,
        step_index: int,
        action_type: str,
    ) -> None:
        """Friction composition for failed workflow steps.

        Mirrors ``FrictionObserver._classify_and_record``:
            active / reactivated  → record_occurrence
            resolved              → record_recurrence
            no match / archived   → workflow.friction_pattern_unclassified

        Only fires when the sink's insert actually inserted (return
        False signals a PK-conflict idempotency skip; the friction
        hook does NOT fire for skips).
        """
        if self._parent._pattern_store is None:
            return
        # Lazy import to avoid circular import at module load.
        from kernos.kernel.friction_patterns import (
            LIFECYCLE_ACTIVE,
            LIFECYCLE_ARCHIVED,
            LIFECYCLE_REACTIVATED,
            LIFECYCLE_RESOLVED,
            classified_by_for_match_path,
            classify_signal,
        )

        store = self._parent._pattern_store
        signal_type = f"workflow_step:{action_type}:failed"
        signal_description = record.user_visible_summary

        try:
            candidates = await store.list_patterns(self._instance_id)
        except Exception as exc:
            logger.debug(
                "WORKFLOW_FRICTION_LIST_FAILED execution_id=%s error=%s",
                self._workflow_execution_id, exc,
            )
            return

        result = classify_signal(
            signal_type=signal_type,
            signal_description=signal_description,
            candidates=candidates,
        )

        if result is None:
            await self._emit_pattern_unclassified(
                step_index=step_index,
                action_type=action_type,
                signal_type=signal_type,
                signal_description=signal_description,
            )
            return

        pattern, score, match_path = result
        classified_by = classified_by_for_match_path(match_path)
        observed_at = _now()

        try:
            if pattern.lifecycle_state in (LIFECYCLE_ACTIVE, LIFECYCLE_REACTIVATED):
                await store.record_occurrence(
                    instance_id=self._instance_id,
                    pattern_id=pattern.pattern_id,
                    observed_at=observed_at,
                    report_path="",
                    classifier_score=score,
                    classified_by=classified_by,
                    space_id="",
                    member_id=self._member_id,
                )
            elif pattern.lifecycle_state == LIFECYCLE_RESOLVED:
                # Codex round-2-impl Medium 7: FrictionPatternStore
                # invokes the injected emitter with (event_type,
                # payload) — a 2-arg shape that does NOT match our
                # EventStreamEmitter type (event_stream.emit-style).
                # Bridge with an adapter so the workflow emitter
                # actually receives recurrence events with the right
                # instance_id / correlation / member context. Without
                # this shim, the store catches the signature TypeError
                # and silently falls back to module event_stream.emit,
                # which loses workflow attribution.
                parent_emit = self._parent._emit_event

                async def _recurrence_emit_adapter(event_type, payload):
                    if parent_emit is None:
                        return
                    await parent_emit(
                        self._instance_id,
                        event_type,
                        payload,
                        correlation_id=self._correlation_id,
                        member_id=self._member_id or None,
                    )

                await store.record_recurrence(
                    instance_id=self._instance_id,
                    pattern_id=pattern.pattern_id,
                    observed_at=observed_at,
                    report_path="",
                    classifier_score=score,
                    classified_by=classified_by,
                    space_id="",
                    member_id=self._member_id,
                    emit_event=(
                        _recurrence_emit_adapter
                        if parent_emit is not None else None
                    ),
                )
            elif pattern.lifecycle_state == LIFECYCLE_ARCHIVED:
                # classify_signal already filters archived; if a future
                # code path surfaces an archived match, emit the
                # unclassified event for symmetry with the observer.
                await self._emit_pattern_unclassified(
                    step_index=step_index,
                    action_type=action_type,
                    signal_type=signal_type,
                    signal_description=signal_description,
                    matched_pattern_id=pattern.pattern_id,
                    matched_pattern_state=pattern.lifecycle_state,
                )
        except Exception as exc:
            logger.debug(
                "WORKFLOW_FRICTION_RECORD_FAILED execution_id=%s "
                "pattern_id=%s error=%s",
                self._workflow_execution_id, pattern.pattern_id, exc,
            )

    async def _emit_pattern_unclassified(
        self,
        *,
        step_index: int,
        action_type: str,
        signal_type: str,
        signal_description: str,
        matched_pattern_id: str = "",
        matched_pattern_state: str = "",
    ) -> None:
        payload: dict[str, Any] = {
            "workflow_execution_id": self._workflow_execution_id,
            "step_index": step_index,
            "action_type": action_type,
            "signal_type": signal_type,
            "signal_description": signal_description[:200],
            "correlation_id": self._correlation_id,
            "member_id": self._member_id,
        }
        if matched_pattern_id:
            payload["matched_pattern_id"] = matched_pattern_id
        if matched_pattern_state:
            payload["matched_pattern_state"] = matched_pattern_state
        try:
            emit = self._parent._emit_event
            if emit is not None:
                await emit(
                    self._instance_id,
                    "workflow.friction_pattern_unclassified",
                    payload,
                    correlation_id=self._correlation_id,
                    member_id=self._member_id or None,
                )
                return
            # Fallback to event_stream module directly so the event
            # still lands even when callers didn't wire an explicit
            # emitter. Mirrors FrictionObserver's fallback path.
            from kernos.kernel import event_stream
            await event_stream.emit(
                self._instance_id,
                "workflow.friction_pattern_unclassified",
                payload,
                correlation_id=self._correlation_id,
                member_id=self._member_id or None,
            )
        except Exception as exc:
            logger.debug(
                "WORKFLOW_FRICTION_UNCLASSIFIED_EMIT_FAILED "
                "execution_id=%s error=%s",
                self._workflow_execution_id, exc,
            )


# ---------------------------------------------------------------------------
# Per-outcome SQL helpers
# ---------------------------------------------------------------------------


# The four shapes from Decision 6's transaction matrix. Each helper
# issues two SQL statements inside the caller's BEGIN IMMEDIATE
# transaction. The caller is ``ExecutionEngine._run_workflow_txn``;
# these helpers MUST NOT call BEGIN / COMMIT themselves.
#
# Return value: True if the record was newly inserted, False on PK
# conflict (legitimate idempotency on the resume-safe contract).
# The state mutation always runs — for the False path it's a no-op
# (cursor already past this step; gate_nonce already set; execution
# already aborted) under existing WHERE-clause guards or simply a
# redundant write that matches the prior state.


async def _append_and_advance(
    db: aiosqlite.Connection,
    sink: WorkflowExecutionActionSink,
    record: ActionStateRecord,
    *,
    step_index: int,
    action_type: str,
    step_output_envelope: dict | None = None,
    step_id: str = "",
) -> bool:
    """Non-gated success OR continue-on-failure: append record + advance
    cursor in one transaction.

    WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 11: step output
    capture co-locates with the action record append. Gated on the
    record's ``inserted=True`` return so step_outputs and
    action_records stay consistent under retry / replay.

    Also clears ``next_step_index = -1`` so any pending branch
    override expires when this step's commit lands.
    """
    inserted = await sink._insert_within_txn(
        db, record, step_index=step_index, action_type=action_type,
    )
    await db.execute(
        "UPDATE workflow_executions "
        "SET action_index_completed = ?, next_step_index = -1, "
        "last_heartbeat = ? "
        "WHERE execution_id = ? AND action_index_completed < ?",
        (step_index, _now(), sink.workflow_execution_id, step_index),
    )
    if inserted and step_output_envelope is not None and step_id:
        from kernos.kernel.workflows.step_outputs import capture_step_output
        await capture_step_output(
            db,
            instance_id=sink.instance_id,
            workflow_execution_id=sink.workflow_execution_id,
            step_id=step_id,
            envelope=step_output_envelope,
        )
    return inserted


async def _append_and_advance_with_branch(
    db: aiosqlite.Connection,
    sink: WorkflowExecutionActionSink,
    record: ActionStateRecord,
    *,
    step_index: int,
    action_type: str,
    next_step_index: int,
    step_output_envelope: dict | None = None,
    step_id: str = "",
) -> bool:
    """Branch verb's atomic boundary (Codex round-1 Blocker 1):
    append record, advance cursor to the branch step itself, AND set
    next_step_index on the execution row in one transaction. Restart
    will read next_step_index before defaulting to
    action_index_completed + 1; the chosen target step is durable
    across crashes.
    """
    inserted = await sink._insert_within_txn(
        db, record, step_index=step_index, action_type=action_type,
    )
    await db.execute(
        "UPDATE workflow_executions "
        "SET action_index_completed = ?, next_step_index = ?, "
        "last_heartbeat = ? "
        "WHERE execution_id = ? AND action_index_completed < ?",
        (
            step_index, next_step_index, _now(),
            sink.workflow_execution_id, step_index,
        ),
    )
    if inserted and step_output_envelope is not None and step_id:
        from kernos.kernel.workflows.step_outputs import capture_step_output
        await capture_step_output(
            db,
            instance_id=sink.instance_id,
            workflow_execution_id=sink.workflow_execution_id,
            step_id=step_id,
            envelope=step_output_envelope,
        )
    return inserted


async def _append_and_persist_gate_nonce(
    db: aiosqlite.Connection,
    sink: WorkflowExecutionActionSink,
    record: ActionStateRecord,
    *,
    step_index: int,
    action_type: str,
    gate_nonce: str,
    step_output_envelope: dict | None = None,
    step_id: str = "",
) -> bool:
    """Gated success: append record + persist gate_nonce on the
    execution row in one transaction. Cursor is NOT advanced here;
    advancing waits for gate release (``_clear_gate_and_advance``).
    """
    inserted = await sink._insert_within_txn(
        db, record, step_index=step_index, action_type=action_type,
    )
    await db.execute(
        "UPDATE workflow_executions "
        "SET gate_nonce = ?, last_heartbeat = ? "
        "WHERE execution_id = ?",
        (gate_nonce, _now(), sink.workflow_execution_id),
    )
    if inserted and step_output_envelope is not None and step_id:
        from kernos.kernel.workflows.step_outputs import capture_step_output
        await capture_step_output(
            db,
            instance_id=sink.instance_id,
            workflow_execution_id=sink.workflow_execution_id,
            step_id=step_id,
            envelope=step_output_envelope,
        )
    return inserted


async def _clear_gate_nonce_and_advance(
    db: aiosqlite.Connection,
    execution_id: str,
    *,
    step_index: int,
    expected_nonce: str = "",
    gate_name: str = "",
    gate_output_payload: dict | None = None,
    instance_id: str = "",
    step_id: str = "",
    approval_outcome: dict | None = None,
) -> bool:
    """Codex round-2-impl Blocker 2: clear gate_nonce AND advance the
    cursor in one transaction. After gate approval the engine needs
    both writes to land atomically; a crash between them would leave
    cursor=K-1 with cleared nonce, which restart logic would then
    treat as "re-execute step K".

    Optional ``expected_nonce`` adds a defensive WHERE clause so two
    concurrent gate resolutions for the same execution can't both
    advance. Returns True if the row was updated; False if the
    expected nonce no longer matched (caller treats as a no-op).

    WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 6 (Codex round-1
    High 5): when ``gate_name`` + ``gate_output_payload`` +
    ``instance_id`` are supplied, atomically capture the satisfying
    event's payload into workflow_step_outputs under
    output_kind='gate'. Reference syntax
    ``{gate.<gate_name>.output.payload.<path>}`` resolves against
    the wrapping envelope.

    Also clears ``next_step_index = -1`` so any pending branch
    override expires when this step's commit lands.
    """
    if expected_nonce:
        cursor = await db.execute(
            "UPDATE workflow_executions "
            "SET gate_nonce = '', action_index_completed = ?, "
            "next_step_index = -1, last_heartbeat = ? "
            "WHERE execution_id = ? AND gate_nonce = ?",
            (step_index, _now(), execution_id, expected_nonce),
        )
    else:
        cursor = await db.execute(
            "UPDATE workflow_executions "
            "SET gate_nonce = '', action_index_completed = ?, "
            "next_step_index = -1, last_heartbeat = ? "
            "WHERE execution_id = ?",
            (step_index, _now(), execution_id),
        )
    updated = cursor.rowcount == 1
    if updated and gate_name and gate_output_payload is not None and instance_id:
        # Decision 6 v2: atomic gate output capture inside the same
        # transaction as the gate release.
        from kernos.kernel.workflows.step_outputs import capture_gate_output
        await capture_gate_output(
            db,
            instance_id=instance_id,
            workflow_execution_id=execution_id,
            gate_name=gate_name,
            event_payload=gate_output_payload,
        )
    if updated and approval_outcome is not None and instance_id and step_id:
        await _merge_approval_outcome_into_step_output(
            db,
            instance_id=instance_id,
            execution_id=execution_id,
            step_id=step_id,
            approval_outcome=approval_outcome,
        )
    return updated


async def _merge_approval_outcome_into_step_output(
    db: aiosqlite.Connection,
    *,
    instance_id: str,
    execution_id: str,
    step_id: str,
    approval_outcome: dict,
) -> None:
    """Merge approval_outcome into the existing step envelope."""
    async with db.execute(
        "SELECT output_json FROM workflow_step_outputs "
        "WHERE instance_id = ? AND workflow_execution_id = ? "
        "AND output_kind = 'step' AND output_name = ? LIMIT 1",
        (instance_id, execution_id, step_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise GateReleaseMissingStepOutput(step_id)
    try:
        raw_output = row["output_json"]
    except (KeyError, TypeError, IndexError):
        raw_output = row[0]
    try:
        envelope = json.loads(raw_output)
    except (json.JSONDecodeError, TypeError):
        envelope = {}
    envelope["approval_outcome"] = approval_outcome
    from kernos.kernel.workflows.step_outputs import serialize_envelope
    payload, truncated, _ = serialize_envelope(envelope)
    await db.execute(
        "UPDATE workflow_step_outputs "
        "SET output_json = ?, truncated = ?, recorded_at = ? "
        "WHERE instance_id = ? AND workflow_execution_id = ? "
        "AND output_kind = 'step' AND output_name = ?",
        (
            payload, 1 if truncated else 0, _now(),
            instance_id, execution_id, step_id,
        ),
    )


async def _advance_cursor_only(
    db: aiosqlite.Connection,
    execution_id: str,
    *,
    step_index: int,
) -> None:
    """Codex round-2-impl High 3 fallback: advance cursor when the
    audit-record build failed but the workflow step did succeed.
    Preserves the "workflow runs even if audit fails" invariant.
    """
    await db.execute(
        "UPDATE workflow_executions "
        "SET action_index_completed = ?, last_heartbeat = ? "
        "WHERE execution_id = ? AND action_index_completed < ?",
        (step_index, _now(), execution_id, step_index),
    )


async def _persist_gate_nonce_only(
    db: aiosqlite.Connection,
    execution_id: str,
    *,
    gate_nonce: str,
) -> None:
    """Codex round-2-impl High 3 fallback: persist gate_nonce when
    the audit-record build failed on a gated success.
    """
    await db.execute(
        "UPDATE workflow_executions "
        "SET gate_nonce = ?, last_heartbeat = ? "
        "WHERE execution_id = ?",
        (gate_nonce, _now(), execution_id),
    )


async def _abort_state_only(
    db: aiosqlite.Connection,
    execution_id: str,
    *,
    aborted_reason: str,
) -> None:
    """Codex round-2-impl High 3 fallback: transition execution to
    aborted state WITHOUT inserting a record. The caller is
    responsible for emitting workflow.execution_terminated. Used when
    audit-record build fails on the abort path so the workflow still
    terminates cleanly.
    """
    now = _now()
    await db.execute(
        "UPDATE workflow_executions "
        "SET state = 'aborted', aborted_reason = ?, terminated_at = ?, "
        "last_heartbeat = ? "
        "WHERE execution_id = ?",
        (aborted_reason, now, now, execution_id),
    )


async def _append_and_abort(
    db: aiosqlite.Connection,
    sink: WorkflowExecutionActionSink,
    record: ActionStateRecord,
    *,
    step_index: int,
    action_type: str,
    aborted_reason: str,
    step_output_envelope: dict | None = None,
    step_id: str = "",
) -> bool:
    """Aborting failure: append record + transition execution to
    aborted state in one transaction. Cursor is NOT advanced.
    """
    inserted = await sink._insert_within_txn(
        db, record, step_index=step_index, action_type=action_type,
    )
    now = _now()
    await db.execute(
        "UPDATE workflow_executions "
        "SET state = 'aborted', aborted_reason = ?, terminated_at = ?, "
        "last_heartbeat = ? "
        "WHERE execution_id = ?",
        (aborted_reason, now, now, sink.workflow_execution_id),
    )
    if inserted and step_output_envelope is not None and step_id:
        from kernos.kernel.workflows.step_outputs import capture_step_output
        await capture_step_output(
            db,
            instance_id=sink.instance_id,
            workflow_execution_id=sink.workflow_execution_id,
            step_id=step_id,
            envelope=step_output_envelope,
        )
    return inserted


__all__ = [
    "ACTION_OPERATION_CLASS_BY_VERB",
    "GateReleaseMissingStepOutput",
    "RISK_LEVEL_BY_OPERATION_CLASS",
    "ToolOperationClassLookup",
    "EventStreamEmitter",
    "WorkflowActionRecord",
    "WorkflowActionSink",
    "WorkflowExecutionActionSink",
    "ensure_workflow_action_records_schema",
    "_abort_state_only",
    "_advance_cursor_only",
    "_append_and_abort",
    "_append_and_advance",
    "_append_and_advance_with_branch",
    "_append_and_persist_gate_nonce",
    "_build_action_state_record",
    "_clear_gate_nonce_and_advance",
    "_operation_class_for_action_type",
    "_persist_gate_nonce_only",
    "_risk_level_for_operation_class",
]
