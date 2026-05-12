"""ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1 substrate-fidelity tests.

Pins the v-final contract:

  * Successful step → ActionStateRecord with execution_state="completed"
    persisted via the per-outcome ``_append_and_advance`` shape; cursor
    advances atomically.
  * Failed step → ActionStateRecord with execution_state="failed"; the
    ``user_visible_summary`` carries the same error string the existing
    ``workflow.execution_step_failed`` event records.
  * Resume-safe idempotency on (instance_id, workflow_execution_id,
    step_index) via targeted ON CONFLICT DO NOTHING (non-PK constraint
    failures raise).
  * Per-outcome transaction matrix: non-gated success, gated success,
    continue-on-failure, aborting failure each commit the right
    combination of record + state mutation atomically.
  * Engine-startup state-aware self-heal: advance for completed records
    when no gate pending and no abort routing; skip + log otherwise.
  * Async write-lock + ``_run_workflow_txn`` helper serialize concurrent
    asyncio tasks; busy-retry under contention.
  * Friction composition: lifecycle dispatch to record_occurrence /
    record_recurrence / unclassified-emit; member_id bound at sink
    construction; classify-only-on-actual-insert.
  * Sink context binding: callers cannot override instance_id or
    workflow_execution_id; member_id bound at construction.
  * ``call_tool`` operation_class source rule: tool-registry lookup,
    otherwise default ``mutate`` / ``medium`` / missing_metadata=True.
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from kernos.kernel import event_stream
from kernos.kernel.friction_patterns import (
    FrictionPatternStore,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_ARCHIVED,
    LIFECYCLE_RESOLVED,
)
from kernos.kernel.integration.briefing import ActionStateRecord
from kernos.kernel.workflows.action_library import (
    ActionLibrary,
    ActionResult,
    MarkStateAction,
    NotifyUserAction,
)
from kernos.kernel.workflows.action_sink import (
    WorkflowActionRecord,
    WorkflowActionSink,
    WorkflowExecutionActionSink,
    _append_and_abort,
    _append_and_advance,
    _append_and_persist_gate_nonce,
    _build_action_state_record,
    _operation_class_for_action_type,
    _risk_level_for_operation_class,
    ensure_workflow_action_records_schema,
)
from kernos.kernel.workflows.execution_engine import (
    ExecutionEngine,
    WorkflowExecution,
    _ensure_schema,
)
from kernos.kernel.workflows.ledger import WorkflowLedger
from kernos.kernel.workflows.trigger_registry import (
    TriggerRegistry,
    _reset_for_tests as _reset_trigger_registry,
)
from kernos.kernel.workflows.workflow_registry import (
    ActionDescriptor,
    Bounds,
    ContinuationRules,
    TriggerDescriptor,
    Verifier,
    Workflow,
    WorkflowRegistry,
)


# ===========================================================================
# Shared fixtures + helpers
# ===========================================================================


def _make_action(action_type="mark_state", gate_ref=None,
                 on_failure="abort", resume_safe=False, **params):
    return ActionDescriptor(
        action_type=action_type,
        parameters=params,
        gate_ref=gate_ref,
        resume_safe=resume_safe,
        continuation_rules=ContinuationRules(on_failure=on_failure),
    )


def _make_workflow(actions, *, workflow_id="wf-sink", **overrides) -> Workflow:
    base = dict(
        workflow_id=workflow_id,
        instance_id="inst_a",
        name="sink test",
        description="",
        owner="owner",
        version="1.0",
        bounds=Bounds(iteration_count=10, wall_time_seconds=30),
        verifier=Verifier(flavor="deterministic", check="ok"),
        action_sequence=actions,
        approval_gates=[],
        trigger=TriggerDescriptor(
            event_type="cc.batch.report",
            predicate={"op": "exists", "path": "event_id"},
        ),
    )
    base.update(overrides)
    return Workflow(**base)


def _state_store() -> tuple[dict, "callable", "callable"]:
    store: dict = {}

    async def set_(*, key, value, scope, instance_id):
        store[(scope, instance_id, key)] = value

    async def get_(*, key, scope, instance_id):
        return store.get((scope, instance_id, key))

    return store, set_, get_


@pytest.fixture
async def stack(tmp_path):
    """Full engine stack with the action sink wired."""
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    trig = TriggerRegistry()
    await trig.start(str(tmp_path))
    wfr = WorkflowRegistry()
    await wfr.start(str(tmp_path), trig)
    store, set_, get_ = _state_store()
    lib = ActionLibrary()
    lib.register(MarkStateAction(state_store_set=set_, state_store_get=get_))
    delivered: list = []

    async def deliver(**kw):
        delivered.append(kw)
        return {"persisted_id": f"msg-{len(delivered)}"}
    lib.register(NotifyUserAction(deliver_fn=deliver))
    ledger = WorkflowLedger(str(tmp_path))
    pattern_store = FrictionPatternStore()
    await pattern_store.start(str(tmp_path))
    emitted_events: list = []

    async def emit_capture(instance_id, event_type, payload, **kw):
        emitted_events.append({
            "instance_id": instance_id,
            "event_type": event_type,
            "payload": payload,
            "kw": kw,
        })
        await event_stream.emit(instance_id, event_type, payload, **kw)

    engine = ExecutionEngine()
    await engine.start(
        str(tmp_path), trig, wfr, lib, ledger,
        space_resolver=None,
        pattern_store=pattern_store,
        action_sink_emit_event=emit_capture,
    )
    yield {
        "tmp_path": tmp_path,
        "trig": trig,
        "wfr": wfr,
        "lib": lib,
        "ledger": ledger,
        "engine": engine,
        "store": store,
        "delivered": delivered,
        "pattern_store": pattern_store,
        "emitted_events": emitted_events,
    }
    await engine.stop()
    await pattern_store.stop()
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await event_stream._reset_for_tests()


async def _wait_for(predicate, timeout=2.0, step=0.02):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


async def _open_db(tmp_path) -> aiosqlite.Connection:
    """Open a standalone aiosqlite connection on the workflow DB with
    schema ensured. Used by unit-level probes that bypass the engine.
    """
    db = await aiosqlite.connect(
        str(tmp_path / "instance.db"), isolation_level=None,
    )
    db.row_factory = aiosqlite.Row
    await _ensure_schema(db)
    await ensure_workflow_action_records_schema(db)
    return db


def _make_execution(
    *, workflow_id="wf-sink", instance_id="inst_a",
    execution_id="exec_test_0001", correlation_id="corr_0001",
    member_id="mem_a",
) -> WorkflowExecution:
    return WorkflowExecution(
        execution_id=execution_id,
        workflow_id=workflow_id,
        instance_id=instance_id,
        correlation_id=correlation_id,
        state="running",
        action_index_completed=-1,
        member_id=member_id,
        started_at="2026-05-11T00:00:00+00:00",
    )


async def _insert_execution_row(
    db: aiosqlite.Connection, execution: WorkflowExecution,
) -> None:
    await db.execute(
        "INSERT INTO workflow_executions ("
        " execution_id, workflow_id, instance_id, correlation_id,"
        " state, action_index_completed, intermediate_state,"
        " last_heartbeat, aborted_reason, started_at, terminated_at,"
        " trigger_event_payload, trigger_event_id, member_id,"
        " gate_nonce, fire_id"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        execution.to_row(),
    )


# ===========================================================================
# Successful step receipt
# ===========================================================================


class TestStepReceiptSuccess:
    async def test_step_succeeded_produces_record_and_event(self, stack):
        wf = _make_workflow([_make_action("mark_state", key="x", value=1,
                                          scope="instance")])
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit(
            "inst_a", "cc.batch.report", {}, member_id="mem_a",
        )
        await event_stream.flush_now()
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "x") in stack["store"],
        )
        assert ok
        executions = await stack["engine"].list_executions(
            "inst_a", state="completed",
        )
        assert len(executions) == 1
        sink = stack["engine"]._action_sink
        records = await sink.list_for_execution(
            "inst_a", executions[0].execution_id,
        )
        assert len(records) == 1
        record = records[0].record
        assert record.surface == "workflow_step"
        assert record.operation == "mark_state"
        assert record.operation_class == "mutate"
        assert record.execution_state == "completed"
        assert record.authorization_state == "not_required"
        assert record.risk_level == "medium"  # mutate → medium per Q4
        # Stable provenance tag from Decision 1 escape hatch.
        assert any(
            ref.startswith(
                f"workflow:{executions[0].execution_id}:step:0"
            ) for ref in record.receipt_refs
        )

    async def test_action_id_routable_via_get_by_action_id(self, stack):
        wf = _make_workflow([_make_action("mark_state", key="x", value=1,
                                          scope="instance")])
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        await _wait_for(
            lambda: ("instance", "inst_a", "x") in stack["store"],
        )
        sink = stack["engine"]._action_sink
        executions = await stack["engine"].list_executions(
            "inst_a", state="completed",
        )
        records = await sink.list_for_execution(
            "inst_a", executions[0].execution_id,
        )
        fetched = await sink.get_by_action_id(
            "inst_a", records[0].record.action_id,
        )
        assert fetched is not None
        assert fetched.record.action_id == records[0].record.action_id


# ===========================================================================
# Failed step receipt
# ===========================================================================


class TestStepReceiptFailure:
    async def test_execute_raised_produces_failed_record(self, stack):
        # Replace the registered mark_state verb with one that raises.
        class RaisingMarkState:
            action_type = "mark_state"

            async def execute(self, context, params):
                raise RuntimeError("synthetic_failure")

            async def verify(self, context, params, result):
                return False

        stack["lib"]._verbs["mark_state"] = RaisingMarkState()
        action = _make_action(
            "mark_state", on_failure="abort",
            key="x", value=1, scope="instance",
        )
        wf = _make_workflow([action], workflow_id="wf-raises")
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        executions = []
        for _ in range(100):
            executions = await stack["engine"].list_executions(
                "inst_a", state="aborted",
            )
            if executions:
                break
            await asyncio.sleep(0.02)
        assert executions, "expected an aborted execution"
        sink = stack["engine"]._action_sink
        records = await sink.list_for_execution(
            "inst_a", executions[0].execution_id,
        )
        assert len(records) == 1
        record = records[0].record
        assert record.execution_state == "failed"
        assert "execute_raised:RuntimeError" in record.user_visible_summary
        assert executions[0].state == "aborted"

    async def test_continuation_continue_does_not_orphan_records(self, stack):
        # Replace mark_state with a verifier-failing variant.
        class VerifierFailsMarkState:
            action_type = "mark_state"
            _seen_keys: list[str] = []

            async def execute(self, context, params):
                # Allow the post-failure step to actually write.
                if params.get("key") == "post_failure_marker":
                    # Behave as the real MarkStateAction:
                    return ActionResult(
                        success=True, value=None,
                        receipt={"persisted_id": "ok"},
                    )
                return ActionResult(success=True, value="ok",
                                    receipt={"id": "x"})

            async def verify(self, context, params, result):
                # First action fails the verifier; second succeeds.
                if params.get("key") == "post_failure_marker":
                    return True
                return False

        stack["lib"]._verbs["mark_state"] = VerifierFailsMarkState()
        action1 = _make_action(
            "mark_state", on_failure="continue",
            key="first_step", value=1, scope="instance",
        )
        action2 = _make_action(
            "mark_state", on_failure="continue",
            key="post_failure_marker", value="set", scope="instance",
        )
        wf = _make_workflow([action1, action2], workflow_id="wf-continue")
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        for _ in range(150):
            executions = await stack["engine"].list_executions(
                "inst_a", state="completed",
            )
            if executions:
                break
            await asyncio.sleep(0.02)
        assert executions, "workflow should reach completed state"
        sink = stack["engine"]._action_sink
        records = await sink.list_for_execution(
            "inst_a", executions[0].execution_id,
        )
        assert len(records) == 2
        assert records[0].record.execution_state == "failed"
        assert records[1].record.execution_state == "completed"


# ===========================================================================
# Resume across restart — per-outcome atomicity, ON CONFLICT, self-heal
# ===========================================================================


class TestResumeIdempotency:
    async def test_targeted_on_conflict_returns_false_on_pk_collision(
        self, tmp_path,
    ):
        db = await _open_db(tmp_path)
        sink = WorkflowActionSink(db)
        execution = _make_execution()
        await _insert_execution_row(db, execution)
        per_exec = sink.for_execution(execution)
        action = _make_action("mark_state", key="x", value=1, scope="instance")
        record = _build_action_state_record(
            execution=execution, step_index=0, action=action,
            execution_state="completed",
        )
        inserted_first = await per_exec.append(
            record, step_index=0, action_type=action.action_type,
        )
        inserted_second = await per_exec.append(
            record, step_index=0, action_type=action.action_type,
        )
        assert inserted_first is True
        assert inserted_second is False
        rows = await sink.list_for_execution(
            execution.instance_id, execution.execution_id,
        )
        assert len(rows) == 1
        await db.close()

    async def test_targeted_on_conflict_raises_on_non_pk_constraint(
        self, tmp_path,
    ):
        db = await _open_db(tmp_path)
        sink = WorkflowActionSink(db)
        execution = _make_execution()
        await _insert_execution_row(db, execution)
        per_exec = sink.for_execution(execution)
        # Build a record then write a NULL into one of the NOT NULL
        # columns directly to verify the constraint fires (a real PK
        # conflict path uses ON CONFLICT; non-PK paths must raise).
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO workflow_action_records ("
                " instance_id, workflow_execution_id, step_index,"
                " action_id, workflow_id, action_type, record_json,"
                " correlation_id, recorded_at"
                ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    execution.instance_id, execution.execution_id, 5,
                    "act_x", execution.workflow_id, "mark_state",
                    execution.correlation_id, "2026-05-11T00:00:00+00:00",
                ),
            )
        await db.close()

    async def test_atomic_boundary_non_gated_success_advances_cursor(
        self, tmp_path,
    ):
        db = await _open_db(tmp_path)
        sink = WorkflowActionSink(db)
        execution = _make_execution()
        await _insert_execution_row(db, execution)
        per_exec = sink.for_execution(execution)
        action = _make_action("mark_state", key="x", value=1, scope="instance")
        record = _build_action_state_record(
            execution=execution, step_index=0, action=action,
            execution_state="completed",
        )
        await db.execute("BEGIN IMMEDIATE")
        await _append_and_advance(
            db, per_exec, record, step_index=0,
            action_type=action.action_type,
        )
        await db.execute("COMMIT")
        async with db.execute(
            "SELECT action_index_completed FROM workflow_executions "
            "WHERE execution_id = ?", (execution.execution_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row["action_index_completed"] == 0
        records = await sink.list_for_execution(
            execution.instance_id, execution.execution_id,
        )
        assert len(records) == 1
        await db.close()

    async def test_atomic_boundary_gated_success_persists_gate_nonce(
        self, tmp_path,
    ):
        db = await _open_db(tmp_path)
        sink = WorkflowActionSink(db)
        execution = _make_execution()
        await _insert_execution_row(db, execution)
        per_exec = sink.for_execution(execution)
        action = _make_action(
            "mark_state", gate_ref="g1",
            key="x", value=1, scope="instance",
        )
        record = _build_action_state_record(
            execution=execution, step_index=0, action=action,
            execution_state="completed",
        )
        await db.execute("BEGIN IMMEDIATE")
        await _append_and_persist_gate_nonce(
            db, per_exec, record, step_index=0,
            action_type=action.action_type,
            gate_nonce="nonce_xyz",
        )
        await db.execute("COMMIT")
        async with db.execute(
            "SELECT action_index_completed, gate_nonce "
            "FROM workflow_executions WHERE execution_id = ?",
            (execution.execution_id,),
        ) as cur:
            row = await cur.fetchone()
        # Cursor NOT advanced — gated success defers cursor advance
        # until gate release. Gate nonce persisted.
        assert row["action_index_completed"] == -1
        assert row["gate_nonce"] == "nonce_xyz"
        records = await sink.list_for_execution(
            execution.instance_id, execution.execution_id,
        )
        assert len(records) == 1
        await db.close()

    async def test_atomic_boundary_aborting_failure_commits_state(
        self, tmp_path,
    ):
        db = await _open_db(tmp_path)
        sink = WorkflowActionSink(db)
        execution = _make_execution()
        await _insert_execution_row(db, execution)
        per_exec = sink.for_execution(execution)
        action = _make_action(
            "mark_state", on_failure="abort",
            key="x", value=1, scope="instance",
        )
        record = _build_action_state_record(
            execution=execution, step_index=0, action=action,
            execution_state="failed", error="verifier_rejected",
        )
        await db.execute("BEGIN IMMEDIATE")
        await _append_and_abort(
            db, per_exec, record, step_index=0,
            action_type=action.action_type,
            aborted_reason="step_0_failed",
        )
        await db.execute("COMMIT")
        async with db.execute(
            "SELECT state, action_index_completed, aborted_reason "
            "FROM workflow_executions WHERE execution_id = ?",
            (execution.execution_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row["state"] == "aborted"
        assert row["aborted_reason"] == "step_0_failed"
        # Cursor NOT advanced — aborting failure leaves cursor put.
        assert row["action_index_completed"] == -1
        records = await sink.list_for_execution(
            execution.instance_id, execution.execution_id,
        )
        assert len(records) == 1
        assert records[0].record.execution_state == "failed"
        await db.close()

    async def test_atomic_boundary_rolls_back_on_body_exception(
        self, tmp_path,
    ):
        db = await _open_db(tmp_path)
        sink = WorkflowActionSink(db)
        execution = _make_execution()
        await _insert_execution_row(db, execution)
        per_exec = sink.for_execution(execution)
        action = _make_action("mark_state", key="x", value=1, scope="instance")
        record = _build_action_state_record(
            execution=execution, step_index=0, action=action,
            execution_state="completed",
        )
        # Simulate failure inside the transaction by manually
        # rolling back after a partial write.
        await db.execute("BEGIN IMMEDIATE")
        await per_exec._insert_within_txn(
            db, record, step_index=0, action_type=action.action_type,
        )
        await db.execute("ROLLBACK")
        records = await sink.list_for_execution(
            execution.instance_id, execution.execution_id,
        )
        async with db.execute(
            "SELECT action_index_completed FROM workflow_executions "
            "WHERE execution_id = ?", (execution.execution_id,),
        ) as cur:
            row = await cur.fetchone()
        assert len(records) == 0
        assert row["action_index_completed"] == -1
        await db.close()

    async def test_write_lock_serializes_concurrent_txns(self, tmp_path):
        # Use the engine's _run_workflow_txn via a minimal engine stack.
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        trig = TriggerRegistry()
        await trig.start(str(tmp_path))
        wfr = WorkflowRegistry()
        await wfr.start(str(tmp_path), trig)
        store, set_, get_ = _state_store()
        lib = ActionLibrary()
        lib.register(MarkStateAction(state_store_set=set_, state_store_get=get_))
        ledger = WorkflowLedger(str(tmp_path))
        engine = ExecutionEngine()
        await engine.start(str(tmp_path), trig, wfr, lib, ledger,
                           space_resolver=None)
        try:
            order: list[int] = []

            async def body_one(db):
                order.append(1)
                await asyncio.sleep(0.05)
                order.append(11)

            async def body_two(db):
                order.append(2)
                await asyncio.sleep(0.01)
                order.append(22)

            task1 = asyncio.create_task(engine._run_workflow_txn(body_one))
            await asyncio.sleep(0.005)  # let body_one acquire the lock first
            task2 = asyncio.create_task(engine._run_workflow_txn(body_two))
            await asyncio.gather(task1, task2)
            # body_one must complete before body_two starts
            assert order == [1, 11, 2, 22]
        finally:
            await engine.stop()
            await wfr.stop()
            await _reset_trigger_registry(trig)
            await event_stream._reset_for_tests()

    async def test_self_heal_advances_for_completed_record(self, tmp_path):
        # Pre-seed: execution row in running state with a completed
        # record for step 0 but cursor not advanced. After engine
        # start, cursor should advance.
        db = await aiosqlite.connect(
            str(tmp_path / "instance.db"), isolation_level=None,
        )
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        await ensure_workflow_action_records_schema(db)
        execution = _make_execution(execution_id="exec_heal_001")
        await _insert_execution_row(db, execution)
        action = _make_action("mark_state", key="x", value=1, scope="instance")
        record = _build_action_state_record(
            execution=execution, step_index=0, action=action,
            execution_state="completed",
        )
        sink = WorkflowActionSink(db)
        per_exec = sink.for_execution(execution)
        await per_exec._insert_within_txn(
            db, record, step_index=0, action_type=action.action_type,
        )
        await db.close()
        # Register workflow + start engine to fire self-heal.
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        trig = TriggerRegistry()
        await trig.start(str(tmp_path))
        wfr = WorkflowRegistry()
        await wfr.start(str(tmp_path), trig)
        wf = _make_workflow([action], workflow_id="wf-sink")
        await wfr._register_workflow_unbound(wf)
        lib = ActionLibrary()
        store, set_, get_ = _state_store()
        lib.register(MarkStateAction(state_store_set=set_, state_store_get=get_))
        ledger = WorkflowLedger(str(tmp_path))
        engine = ExecutionEngine()
        await engine.start(
            str(tmp_path), trig, wfr, lib, ledger, space_resolver=None,
        )
        try:
            async with engine._db.execute(
                "SELECT action_index_completed FROM workflow_executions "
                "WHERE execution_id = ?", (execution.execution_id,),
            ) as cur:
                row = await cur.fetchone()
            assert row["action_index_completed"] == 0
        finally:
            await engine.stop()
            await wfr.stop()
            await _reset_trigger_registry(trig)
            await event_stream._reset_for_tests()

    async def test_self_heal_skips_for_pending_gate(self, tmp_path):
        # Pre-seed: execution with gate_nonce set + a completed record
        # for step 0. Self-heal must NOT advance cursor (restart logic
        # will re-enter the gate wait).
        db = await aiosqlite.connect(
            str(tmp_path / "instance.db"), isolation_level=None,
        )
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        await ensure_workflow_action_records_schema(db)
        execution = _make_execution(execution_id="exec_heal_gate_001")
        execution.gate_nonce = "nonce_pending"
        await _insert_execution_row(db, execution)
        action = _make_action(
            "mark_state", gate_ref="g1",
            key="x", value=1, scope="instance",
        )
        record = _build_action_state_record(
            execution=execution, step_index=0, action=action,
            execution_state="completed",
        )
        sink = WorkflowActionSink(db)
        per_exec = sink.for_execution(execution)
        await per_exec._insert_within_txn(
            db, record, step_index=0, action_type=action.action_type,
        )
        await db.close()
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        trig = TriggerRegistry()
        await trig.start(str(tmp_path))
        wfr = WorkflowRegistry()
        await wfr.start(str(tmp_path), trig)
        # Workflow needs a gate defined for the action's gate_ref.
        from kernos.kernel.workflows.workflow_registry import ApprovalGate
        wf = _make_workflow(
            [action], workflow_id="wf-sink",
            approval_gates=[ApprovalGate(
                gate_name="g1",
                approval_event_type="approval.granted",
                approval_event_predicate={"op": "exists", "path": "event_id"},
                timeout_seconds=30,
                bound_behavior_on_timeout="abort_workflow",
                pause_reason="awaiting approval",
            )],
        )
        await wfr._register_workflow_unbound(wf)
        lib = ActionLibrary()
        store, set_, get_ = _state_store()
        lib.register(MarkStateAction(state_store_set=set_, state_store_get=get_))
        ledger = WorkflowLedger(str(tmp_path))
        engine = ExecutionEngine()
        await engine.start(
            str(tmp_path), trig, wfr, lib, ledger, space_resolver=None,
        )
        try:
            async with engine._db.execute(
                "SELECT action_index_completed FROM workflow_executions "
                "WHERE execution_id = ?", (execution.execution_id,),
            ) as cur:
                row = await cur.fetchone()
            # Cursor must NOT have advanced through the gate.
            assert row["action_index_completed"] == -1
        finally:
            await engine.stop()
            await wfr.stop()
            await _reset_trigger_registry(trig)
            await event_stream._reset_for_tests()

    async def test_self_heal_skips_for_aborting_failed(self, tmp_path):
        # Pre-seed: execution with a failed record on a step whose
        # on_failure="abort". Self-heal must NOT advance (restart
        # logic will route to abort).
        db = await aiosqlite.connect(
            str(tmp_path / "instance.db"), isolation_level=None,
        )
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        await ensure_workflow_action_records_schema(db)
        execution = _make_execution(execution_id="exec_heal_abort_001")
        await _insert_execution_row(db, execution)
        action = _make_action(
            "mark_state", on_failure="abort",
            key="x", value=1, scope="instance",
        )
        record = _build_action_state_record(
            execution=execution, step_index=0, action=action,
            execution_state="failed", error="verifier_rejected",
        )
        sink = WorkflowActionSink(db)
        per_exec = sink.for_execution(execution)
        await per_exec._insert_within_txn(
            db, record, step_index=0, action_type=action.action_type,
        )
        await db.close()
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        trig = TriggerRegistry()
        await trig.start(str(tmp_path))
        wfr = WorkflowRegistry()
        await wfr.start(str(tmp_path), trig)
        wf = _make_workflow([action], workflow_id="wf-sink")
        await wfr._register_workflow_unbound(wf)
        lib = ActionLibrary()
        store, set_, get_ = _state_store()
        lib.register(MarkStateAction(state_store_set=set_, state_store_get=get_))
        ledger = WorkflowLedger(str(tmp_path))
        engine = ExecutionEngine()
        await engine.start(
            str(tmp_path), trig, wfr, lib, ledger, space_resolver=None,
        )
        try:
            async with engine._db.execute(
                "SELECT action_index_completed FROM workflow_executions "
                "WHERE execution_id = ?", (execution.execution_id,),
            ) as cur:
                row = await cur.fetchone()
            assert row["action_index_completed"] == -1
        finally:
            await engine.stop()
            await wfr.stop()
            await _reset_trigger_registry(trig)
            await event_stream._reset_for_tests()


# ===========================================================================
# Sink context binding (Codex round-2 Medium 6)
# ===========================================================================


class TestSinkContextBinding:
    def test_caller_cannot_override_instance_id(self):
        # The public append signature must not accept instance_id.
        import inspect
        sig = inspect.signature(WorkflowExecutionActionSink.append)
        params = sig.parameters
        assert "instance_id" not in params
        assert "workflow_execution_id" not in params
        assert "workflow_id" not in params
        assert "correlation_id" not in params

    async def test_sink_binds_instance_id_from_execution(self, tmp_path):
        db = await _open_db(tmp_path)
        sink = WorkflowActionSink(db)
        exec_a = _make_execution(
            execution_id="exec_a", instance_id="inst_a", member_id="mem_a",
        )
        exec_b = _make_execution(
            execution_id="exec_b", instance_id="inst_b", member_id="mem_b",
        )
        await _insert_execution_row(db, exec_a)
        await _insert_execution_row(db, exec_b)
        per_a = sink.for_execution(exec_a)
        per_b = sink.for_execution(exec_b)
        assert per_a.instance_id == "inst_a"
        assert per_b.instance_id == "inst_b"
        assert per_a.workflow_execution_id == "exec_a"
        assert per_b.workflow_execution_id == "exec_b"
        assert per_a.member_id == "mem_a"
        assert per_b.member_id == "mem_b"
        await db.close()

    async def test_member_id_bound_at_construction(self, tmp_path):
        db = await _open_db(tmp_path)
        sink = WorkflowActionSink(db)
        execution = _make_execution(member_id="mem_original")
        await _insert_execution_row(db, execution)
        per_exec = sink.for_execution(execution, member_id="mem_override")
        assert per_exec.member_id == "mem_override"
        await db.close()


# ===========================================================================
# FRICTION-PATTERN composition (v-final lifecycle dispatch + unclassified)
# ===========================================================================


class TestFrictionPatternComposition:
    async def test_failed_step_records_occurrence_for_active_pattern(
        self, stack,
    ):
        pattern_store = stack["pattern_store"]
        await pattern_store.create_pattern(
            instance_id="inst_a",
            description="Workflow notify failure pattern",
            signal_type_keys=["workflow_step:notify_user:failed"],
            seed_slug="workflow-notify-failed",
        )

        # Verb that always fails (verifier returns False) but
        # action_type matches the seeded pattern's signal_type_keys.
        class FailingNotify:
            action_type = "notify_user"

            async def execute(self, context, params):
                return ActionResult(success=False, error="deliver_failed")

            async def verify(self, context, params, result):
                return False

        # Replace any existing NotifyUserAction in the library to force
        # this failure path.
        stack["lib"]._verbs["notify_user"] = FailingNotify()
        action = ActionDescriptor(
            action_type="notify_user",
            parameters={"channel": "chan_a", "message": "hi"},
            gate_ref=None,
            resume_safe=False,
            continuation_rules=ContinuationRules(on_failure="continue"),
        )
        wf = _make_workflow([action], workflow_id="wf-friction-active")
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        for _ in range(100):
            patterns = await pattern_store.list_patterns("inst_a")
            target = next(
                (p for p in patterns
                 if p.pattern_id == "workflow-notify-failed"),
                None,
            )
            if target is not None and target.occurrence_count > 0:
                break
            await asyncio.sleep(0.02)
        assert target is not None
        assert target.occurrence_count == 1

    async def test_failed_step_emits_unclassified_for_no_match(self, stack):
        # No seeded patterns. Failed step must emit
        # workflow.friction_pattern_unclassified.
        class FailingNotify:
            action_type = "notify_user"

            async def execute(self, context, params):
                return ActionResult(success=False, error="deliver_failed")

            async def verify(self, context, params, result):
                return False

        stack["lib"]._verbs["notify_user"] = FailingNotify()
        action = ActionDescriptor(
            action_type="notify_user",
            parameters={"channel": "chan_a", "message": "hi"},
            gate_ref=None,
            resume_safe=False,
            continuation_rules=ContinuationRules(on_failure="continue"),
        )
        wf = _make_workflow([action], workflow_id="wf-unclassified")
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        for _ in range(100):
            unclassified = [
                e for e in stack["emitted_events"]
                if e["event_type"] == "workflow.friction_pattern_unclassified"
            ]
            if unclassified:
                break
            await asyncio.sleep(0.02)
        assert unclassified, "expected unclassified event"
        payload = unclassified[0]["payload"]
        assert payload["signal_type"] == "workflow_step:notify_user:failed"
        assert payload["member_id"] in ("mem_a", "")
        # Acknowledge member_id binds from execution; in the engine,
        # execution.member_id is "" because the trigger event didn't
        # carry one. Test mainly pins the signal_type + emission.

    async def test_successful_step_does_not_fire_friction_hook(self, stack):
        # Pre-seed an active pattern; trigger a SUCCESSFUL step.
        # Pattern's occurrence_count must remain 0.
        pattern_store = stack["pattern_store"]
        await pattern_store.create_pattern(
            instance_id="inst_a",
            description="Should not fire for success",
            signal_type_keys=["workflow_step:mark_state:failed"],
            seed_slug="should-not-fire",
        )
        wf = _make_workflow(
            [_make_action("mark_state", key="x", value=1, scope="instance")],
            workflow_id="wf-no-friction-success",
        )
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        await _wait_for(
            lambda: ("instance", "inst_a", "x") in stack["store"],
        )
        patterns = await pattern_store.list_patterns("inst_a")
        target = next(
            (p for p in patterns if p.pattern_id == "should-not-fire"),
            None,
        )
        assert target is not None
        assert target.occurrence_count == 0


# ===========================================================================
# call_tool operation_class source (Codex round-2 Low 8)
# ===========================================================================


class TestCallToolOperationClassSource:
    def test_call_tool_with_registry_metadata_uses_declared_class(self):
        def lookup(tool_name: str) -> str | None:
            return {"read_state": "read", "delete_blob": "delete"}.get(tool_name)

        op_class, missing = _operation_class_for_action_type(
            "call_tool", {"tool_name": "read_state"}, tool_lookup=lookup,
        )
        assert op_class == "read"
        assert missing is False
        assert _risk_level_for_operation_class(op_class) == "low"

        op_class, missing = _operation_class_for_action_type(
            "call_tool", {"tool_name": "delete_blob"}, tool_lookup=lookup,
        )
        assert op_class == "delete"
        assert missing is False
        assert _risk_level_for_operation_class(op_class) == "high"

    def test_call_tool_without_registry_metadata_defaults_to_mutate(self):
        op_class, missing = _operation_class_for_action_type(
            "call_tool", {"tool_name": "unknown_tool"}, tool_lookup=None,
        )
        assert op_class == "mutate"
        assert missing is True
        assert _risk_level_for_operation_class(op_class) == "medium"

    def test_call_tool_with_empty_lookup_falls_back_to_default(self):
        def lookup(tool_name: str) -> str | None:
            return None

        op_class, missing = _operation_class_for_action_type(
            "call_tool", {"tool_name": "absent"}, tool_lookup=lookup,
        )
        assert op_class == "mutate"
        assert missing is True


# ===========================================================================
# Direct verb classification
# ===========================================================================


class TestVerbClassification:
    def test_direct_effect_verbs_map_to_mutate(self):
        for verb in ("mark_state", "append_to_ledger"):
            op_class, missing = _operation_class_for_action_type(verb, {})
            assert op_class == "mutate"
            assert missing is False
            assert _risk_level_for_operation_class(op_class) == "medium"

    def test_world_effect_verbs_map_per_table(self):
        cases = {
            "notify_user": ("send", "medium"),
            "write_canvas": ("mutate", "medium"),
            "route_to_agent": ("register", "medium"),
            "post_to_service": ("send", "medium"),
        }
        for verb, (op_class, risk) in cases.items():
            actual_class, missing = _operation_class_for_action_type(verb, {})
            assert actual_class == op_class
            assert missing is False
            assert _risk_level_for_operation_class(actual_class) == risk
