"""Tests for the workflow execution engine.

WORKFLOW-LOOP-PRIMITIVE C5. Pins:

  - background execution: turn-loop / emit fast-path latency
    unaffected by concurrent workflow runs
  - end-to-end: registered workflow's trigger fires → engine
    enqueues → runs action sequence → emits started/terminated
  - per-step audit: step_succeeded / step_failed events
  - approval gates: pause AFTER action, resume on matching event,
    timeout behaviour for abort_workflow / escalate_to_owner /
    auto_proceed_with_default
  - restart-resume: running execution where next action is
    resume_safe re-enqueued at start; otherwise aborted
  - synthetic CohortContext build + active-space resolver kickback
  - multi-tenancy: executions scoped per instance_id
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from kernos.kernel import event_stream
from kernos.kernel.workflows.action_library import (
    ActionLibrary,
    AppendToLedgerAction,
    MarkStateAction,
    NotifyUserAction,
)
from kernos.kernel.workflows.execution_engine import (
    ExecutionEngine,
    WorkflowExecution,
)
from kernos.kernel.workflows.ledger import WorkflowLedger
from kernos.kernel.workflows.trigger_registry import (
    TriggerRegistry,
    _reset_for_tests as _reset_trigger_registry,
)
from kernos.kernel.workflows.workflow_registry import (
    ActionDescriptor,
    ApprovalGate,
    Bounds,
    ContinuationRules,
    TriggerDescriptor,
    Verifier,
    Workflow,
    WorkflowRegistry,
)


def _make_action(action_type="mark_state", gate_ref=None, resume_safe=False, **params):
    return ActionDescriptor(
        action_type=action_type,
        parameters=params,
        gate_ref=gate_ref,
        resume_safe=resume_safe,
        continuation_rules=ContinuationRules(on_failure="abort"),
    )


def _make_workflow(**overrides) -> Workflow:
    base = dict(
        workflow_id="wf-eng",
        instance_id="inst_a",
        name="engine test",
        description="",
        owner="owner",
        version="1.0",
        bounds=Bounds(iteration_count=1, wall_time_seconds=30),
        verifier=Verifier(flavor="deterministic", check="ok"),
        action_sequence=[
            _make_action("mark_state", key="x", value=1, scope="instance"),
        ],
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
    """Full stack: event_stream + trigger_registry + workflow_registry +
    action_library + ledger + execution_engine."""
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
    engine = ExecutionEngine()
    await engine.start(
        str(tmp_path), trig, wfr, lib, ledger,
        space_resolver=None,
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
    }
    await engine.stop()
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await event_stream._reset_for_tests()


async def _wait_for(predicate, timeout=2.0, step=0.02):
    """Poll until ``predicate()`` is true or timeout. Used because the
    engine runs on a separate task and tests need to give it a moment
    to drain."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


# ===========================================================================
# End-to-end happy path
# ===========================================================================


class TestEndToEnd:
    async def test_trigger_match_runs_workflow(self, stack):
        await stack["wfr"]._register_workflow_unbound(_make_workflow())
        await event_stream.emit(
            "inst_a", "cc.batch.report", {"k": "v"}, member_id="mem_a",
        )
        await event_stream.flush_now()
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "x") in stack["store"],
        )
        assert ok, "engine did not execute the action sequence"
        assert stack["store"][("instance", "inst_a", "x")] == 1
        # Execution row reflects completed state.
        executions = await stack["engine"].list_executions(
            "inst_a", state="completed",
        )
        assert len(executions) == 1
        assert executions[0].workflow_id == "wf-eng"


class TestAuditEvents:
    async def test_started_and_terminated_emitted(self, stack):
        await stack["wfr"]._register_workflow_unbound(_make_workflow())
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        await _wait_for(
            lambda: bool(asyncio.run_coroutine_threadsafe is asyncio.run_coroutine_threadsafe),
            timeout=0.3,
        )
        # Wait until terminated event flushes.
        await _wait_for(
            lambda: any(
                e.event_type == "workflow.execution_terminated"
                for e in _all_events(stack["tmp_path"])
            ),
        )
        events = _all_events(stack["tmp_path"])
        types = [e.event_type for e in events]
        assert "workflow.execution_started" in types
        assert "workflow.execution_terminated" in types
        # All execution events share the same correlation_id.
        exec_events = [
            e for e in events if e.event_type.startswith("workflow.execution_")
        ]
        cids = {e.correlation_id for e in exec_events}
        assert len(cids) == 1


# ===========================================================================
# Approval gates
# ===========================================================================


class TestApprovalGates:
    def _gated_workflow(self, gate_behavior="abort_workflow", default_value=None):
        gate = ApprovalGate(
            gate_name="g1",
            pause_reason="approve please",
            approval_event_type="user.approval",
            approval_event_predicate={"op": "actor_eq", "value": "owner"},
            timeout_seconds=1,
            bound_behavior_on_timeout=gate_behavior,
            default_value=default_value,
        )
        return _make_workflow(
            workflow_id="wf-gated",
            approval_gates=[gate],
            action_sequence=[
                _make_action("mark_state", gate_ref="g1", key="x",
                             value=1, scope="instance"),
                _make_action("mark_state", key="y", value=2, scope="instance"),
            ],
        )

    async def test_gate_resumes_on_matching_event(self, stack):
        """WLP-GATE-SCOPING C1: approval events MUST carry the
        engine-minted gate_nonce + execution_id to wake the paused
        execution. The engine persists the nonce after the gated
        action completes; tests query it to compose a valid approval."""
        await stack["wfr"]._register_workflow_unbound(
            self._gated_workflow(gate_behavior="abort_workflow"),
        )
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        # Wait until x is set (pre-gate action) and the engine is paused.
        await _wait_for(
            lambda: ("instance", "inst_a", "x") in stack["store"],
        )
        # Read the engine-minted nonce off the paused execution row.
        execs = await stack["engine"].list_executions("inst_a")
        gated = next(e for e in execs if e.gate_nonce)
        # Send approval event with the matching nonce + execution_id.
        await event_stream.emit(
            "inst_a", "user.approval",
            {"execution_id": gated.execution_id,
             "gate_nonce": gated.gate_nonce},
            member_id="owner",
        )
        await event_stream.flush_now()
        await _wait_for(
            lambda: ("instance", "inst_a", "y") in stack["store"],
        )
        assert stack["store"][("instance", "inst_a", "y")] == 2

    async def test_gate_timeout_aborts(self, stack):
        await stack["wfr"]._register_workflow_unbound(
            self._gated_workflow(gate_behavior="abort_workflow"),
        )
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        await _wait_for(
            lambda: any(
                e.state == "aborted" for e in
                __import__("asyncio").get_event_loop().run_until_complete(
                    stack["engine"].list_executions("inst_a"),
                )
            ) if False else False,  # avoid event loop reentry; use direct await
            timeout=0.0,
        )
        # Simpler: wait for the timeout (1s) plus slack.
        await asyncio.sleep(1.5)
        executions = await stack["engine"].list_executions(
            "inst_a", state="aborted",
        )
        assert len(executions) == 1
        assert "gate_timeout" in executions[0].aborted_reason

    async def test_gate_auto_proceed_with_default(self, stack):
        await stack["wfr"]._register_workflow_unbound(
            self._gated_workflow(
                gate_behavior="auto_proceed_with_default",
                default_value="ok",
            ),
        )
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        # Wait for the gate to time out and auto-proceed; y should be set.
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "y") in stack["store"],
            timeout=3.0,
        )
        assert ok, "auto_proceed_with_default did not continue the workflow"


# ===========================================================================
# Multi-tenancy
# ===========================================================================


class TestMultiTenancy:
    async def test_engine_runs_per_instance_workflows_isolated(self, stack):
        wf_a = _make_workflow(workflow_id="wf-a", instance_id="inst_a")
        wf_b = _make_workflow(
            workflow_id="wf-b",
            instance_id="inst_b",
            action_sequence=[_make_action(
                "mark_state", key="b_only", value=99, scope="instance",
            )],
            trigger=TriggerDescriptor(
                event_type="cc.batch.report",
                predicate={"op": "exists", "path": "event_id"},
            ),
        )
        await stack["wfr"]._register_workflow_unbound(wf_a)
        await stack["wfr"]._register_workflow_unbound(wf_b)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        ok_a = await _wait_for(
            lambda: ("instance", "inst_a", "x") in stack["store"],
        )
        assert ok_a
        # b shouldn't have fired — different instance.
        assert ("instance", "inst_b", "b_only") not in stack["store"]


# ===========================================================================
# Restart-resume
# ===========================================================================


class TestRestartResume:
    async def test_running_with_resume_safe_next_resumes(self, tmp_path):
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        try:
            trig = TriggerRegistry()
            await trig.start(str(tmp_path))
            wfr = WorkflowRegistry()
            await wfr.start(str(tmp_path), trig)
            store, set_, get_ = _state_store()
            lib = ActionLibrary()
            lib.register(MarkStateAction(state_store_set=set_, state_store_get=get_))
            ledger = WorkflowLedger(str(tmp_path))
            # Action sequence: 2 actions, second is resume_safe.
            wf = _make_workflow(
                workflow_id="wf-rs",
                action_sequence=[
                    _make_action("mark_state", key="a", value=1, scope="instance"),
                    _make_action(
                        "mark_state", key="b", value=2, scope="instance",
                        resume_safe=True,
                    ),
                ],
            )
            await wfr._register_workflow_unbound(wf)
            # Manually seed a "running" execution with action_index_completed=0
            # (i.e. the engine had completed the first action and is about to
            # run the second, which is resume_safe).
            engine = ExecutionEngine()
            await engine.start(str(tmp_path), trig, wfr, lib, ledger)
            # Simulate prior crash: insert a running record directly.
            await engine._db.execute(
                "INSERT INTO workflow_executions ("
                " execution_id, workflow_id, instance_id, correlation_id,"
                " state, action_index_completed, intermediate_state,"
                " last_heartbeat, aborted_reason, started_at, terminated_at,"
                " trigger_event_payload, trigger_event_id, member_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "exec-restart", "wf-rs", "inst_a", "cor-1",
                    "running", 0, "{}", "", "",
                    datetime.now(timezone.utc).isoformat(), "",
                    "{}", "ev-x", "mem_a",
                ),
            )
            await engine.stop()
            # Restart engine — restart-resume pass should re-enqueue this
            # execution at the resume_safe step.
            engine2 = ExecutionEngine()
            await engine2.start(str(tmp_path), trig, wfr, lib, ledger)
            ok = await _wait_for(
                lambda: ("instance", "inst_a", "b") in store,
            )
            assert ok, "restart-resume did not run the resume_safe step"
            assert store[("instance", "inst_a", "b")] == 2
            await engine2.stop()
            await wfr.stop()
            await _reset_trigger_registry(trig)
        finally:
            await event_stream._reset_for_tests()

    async def test_running_with_non_resume_safe_aborts(self, tmp_path):
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        try:
            trig = TriggerRegistry()
            await trig.start(str(tmp_path))
            wfr = WorkflowRegistry()
            await wfr.start(str(tmp_path), trig)
            store, set_, get_ = _state_store()
            lib = ActionLibrary()
            lib.register(MarkStateAction(state_store_set=set_, state_store_get=get_))
            ledger = WorkflowLedger(str(tmp_path))
            wf = _make_workflow(
                workflow_id="wf-rs2",
                action_sequence=[
                    _make_action("mark_state", key="a", value=1, scope="instance"),
                    _make_action(
                        "mark_state", key="b", value=2, scope="instance",
                        resume_safe=False,  # default, but explicit
                    ),
                ],
            )
            await wfr._register_workflow_unbound(wf)
            engine = ExecutionEngine()
            await engine.start(str(tmp_path), trig, wfr, lib, ledger)
            await engine._db.execute(
                "INSERT INTO workflow_executions ("
                " execution_id, workflow_id, instance_id, correlation_id,"
                " state, action_index_completed, intermediate_state,"
                " last_heartbeat, aborted_reason, started_at, terminated_at,"
                " trigger_event_payload, trigger_event_id, member_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "exec-abort", "wf-rs2", "inst_a", "cor-2",
                    "running", 0, "{}", "", "",
                    datetime.now(timezone.utc).isoformat(), "",
                    "{}", "ev-x", "mem_a",
                ),
            )
            await engine.stop()
            engine2 = ExecutionEngine()
            await engine2.start(str(tmp_path), trig, wfr, lib, ledger)
            await asyncio.sleep(0.2)
            execs = await engine2.list_executions("inst_a", state="aborted")
            assert any(
                e.execution_id == "exec-abort"
                and e.aborted_reason == "aborted_by_restart"
                for e in execs
            )
            await engine2.stop()
            await wfr.stop()
            await _reset_trigger_registry(trig)
        finally:
            await event_stream._reset_for_tests()


# ===========================================================================
# Background execution: emit fast-path
# ===========================================================================


class TestBoundsEnforcement:
    """Codex review post-C7: registration requires `bounds`, but
    execution must actually honour them at runtime — otherwise the
    declared bound is dead metadata. v1 enforces wall_time_seconds;
    iteration_count and cost_usd are not yet enforceable for
    sequential action chains and ship as registration-only metadata
    until a future spec adds runtime tracking."""

    async def test_wall_time_exceeded_aborts_execution(self, stack):
        """A workflow whose action runs longer than its
        wall_time_seconds bound must abort with `wall_time_exceeded`."""
        # Replace the mark_state verb with one that sleeps longer
        # than the workflow's wall_time bound.
        from kernos.kernel.workflows.action_library import (
            ActionLibrary, MarkStateAction,
        )
        slow_lib = ActionLibrary()

        async def slow_set(*, key, value, scope, instance_id):
            await asyncio.sleep(2.0)

        async def slow_get(*, key, scope, instance_id):
            return None

        slow_lib.register(MarkStateAction(
            state_store_set=slow_set, state_store_get=slow_get,
        ))
        # Swap in the slow library on the running engine.
        stack["engine"]._action_library = slow_lib
        # Register a workflow with a 1-second wall_time bound.
        wf = _make_workflow(
            workflow_id="wf-bounds",
            bounds=Bounds(iteration_count=1, wall_time_seconds=1),
        )
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        # Wait long enough for the bound to bite.
        await asyncio.sleep(1.5)
        execs = await stack["engine"].list_executions(
            "inst_a", state="aborted",
        )
        assert any(
            e.workflow_id == "wf-bounds"
            and e.aborted_reason == "wall_time_exceeded"
            for e in execs
        )


class TestBackgroundExecution:
    async def test_emit_latency_unchanged_with_engine_running(self, stack):
        """Acceptance criterion 11: workflows run BACKGROUND. emit
        latency stays within noise even with executions firing
        concurrently."""
        await stack["wfr"]._register_workflow_unbound(_make_workflow())
        # Kick off a few executions.
        for _ in range(5):
            await event_stream.emit("inst_a", "cc.batch.report", {})
        t0 = time.monotonic()
        for i in range(50):
            await event_stream.emit("inst_a", "tool.called", {"i": i})
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert elapsed_ms < 100, (
            f"50 emits took {elapsed_ms:.1f}ms — engine leaked into fast path"
        )


# ===========================================================================
# Spec 6: ExecutionEngine.register_emitter() + stop() composition
# ===========================================================================
#
# Pins the V7.2 implementation note: production-assembly-owned emitters
# register with the engine so their stop() runs deterministically when
# the engine tears down. Engine does NOT start emitters — that
# responsibility lives in bring_up_substrate (B3 ordering).


class TestSpec6EmitterRegistration:
    """V7.2: register_emitter + stop() composition. Substrate-plumbing
    primitive that lets production assembly register emitters whose
    lifecycle is bounded by the engine's stop()."""

    async def test_register_emitter_stores_under_name(self, stack):
        """Unit pin: registered emitter is observable in _emitters
        dict under its name key."""
        engine = stack["engine"]

        class _StubEmitter:
            def __init__(self):
                self.stopped = False

            async def stop(self):
                self.stopped = True

        emitter = _StubEmitter()
        engine.register_emitter("test_stub", emitter)
        assert "test_stub" in engine._emitters
        assert engine._emitters["test_stub"] is emitter

    async def test_engine_stop_invokes_emitter_stop(self, tmp_path):
        """Functional pin (user-feedback request): mechanic exercised
        under expected use. Register an emitter; stop the engine;
        verify the emitter's stop() was invoked. This is the lifecycle
        contract the production assembly relies on — emitters are torn
        down deterministically as part of engine shutdown."""
        from kernos.kernel.workflows.action_library import (
            ActionLibrary, MarkStateAction,
        )
        from kernos.kernel.workflows.execution_engine import ExecutionEngine
        from kernos.kernel.workflows.ledger import WorkflowLedger
        from kernos.kernel.workflows.trigger_registry import (
            TriggerRegistry,
            _reset_for_tests as _reset_trig,
        )
        from kernos.kernel.workflows.workflow_registry import WorkflowRegistry

        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        trig = TriggerRegistry()
        await trig.start(str(tmp_path))
        wfr = WorkflowRegistry()
        await wfr.start(str(tmp_path), trig)
        lib = ActionLibrary()

        async def _set(*, key, value, scope, instance_id):
            pass

        async def _get(*, key, scope, instance_id):
            return None

        lib.register(MarkStateAction(state_store_set=_set, state_store_get=_get))
        ledger = WorkflowLedger(str(tmp_path))
        engine = ExecutionEngine()
        await engine.start(str(tmp_path), trig, wfr, lib, ledger)

        stop_log: list[str] = []

        class _RealisticEmitter:
            def __init__(self, name):
                self.name = name
                self.stopped = False

            async def stop(self):
                self.stopped = True
                stop_log.append(self.name)

        a = _RealisticEmitter("emitter_a")
        b = _RealisticEmitter("emitter_b")
        engine.register_emitter("a", a)
        engine.register_emitter("b", b)

        try:
            await engine.stop()
        finally:
            await wfr.stop()
            await _reset_trig(trig)
            await event_stream._reset_for_tests()

        # Functional pin: BOTH emitters were stopped (lifecycle
        # contract honored end-to-end).
        assert a.stopped is True
        assert b.stopped is True
        # Substrate state pin: insertion-order shutdown log captured.
        assert stop_log == ["emitter_a", "emitter_b"]
        # Post-stop, _emitters is cleared so a restart wouldn't try
        # to stop the same instances twice.
        assert engine._emitters == {}

    async def test_engine_stop_tolerates_emitter_stop_failure(
        self, tmp_path, caplog,
    ):
        """One bad emitter's stop() raising doesn't leak resources on
        the others. The engine logs and continues."""
        import logging

        from kernos.kernel.workflows.action_library import (
            ActionLibrary, MarkStateAction,
        )
        from kernos.kernel.workflows.execution_engine import ExecutionEngine
        from kernos.kernel.workflows.ledger import WorkflowLedger
        from kernos.kernel.workflows.trigger_registry import (
            TriggerRegistry,
            _reset_for_tests as _reset_trig,
        )
        from kernos.kernel.workflows.workflow_registry import WorkflowRegistry

        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        trig = TriggerRegistry()
        await trig.start(str(tmp_path))
        wfr = WorkflowRegistry()
        await wfr.start(str(tmp_path), trig)
        lib = ActionLibrary()

        async def _set(*, key, value, scope, instance_id):
            pass

        async def _get(*, key, scope, instance_id):
            return None

        lib.register(MarkStateAction(state_store_set=_set, state_store_get=_get))
        ledger = WorkflowLedger(str(tmp_path))
        engine = ExecutionEngine()
        await engine.start(str(tmp_path), trig, wfr, lib, ledger)

        class _BadEmitter:
            async def stop(self):
                raise RuntimeError("intentional emitter failure")

        class _GoodEmitter:
            def __init__(self):
                self.stopped = False

            async def stop(self):
                self.stopped = True

        bad = _BadEmitter()
        good = _GoodEmitter()
        engine.register_emitter("bad", bad)
        engine.register_emitter("good", good)

        try:
            with caplog.at_level(logging.WARNING):
                await engine.stop()
        finally:
            await wfr.stop()
            await _reset_trig(trig)
            await event_stream._reset_for_tests()

        # Functional pin: the GOOD emitter was still stopped even
        # though the bad one raised.
        assert good.stopped is True
        # Substrate-trace pin: failure was logged loudly (not silent).
        assert any(
            "WORKFLOW_EMITTER_STOP_FAILED" in rec.message
            and "bad" in rec.message
            for rec in caplog.records
        )


# ===========================================================================
# Helpers
# ===========================================================================


def _all_events(tmp_path) -> list:
    """Read all events durably persisted (synchronously — the writer
    has already flushed via the explicit flush_now in caller code)."""
    from kernos.kernel.event_stream import _WRITER, Event
    import sqlite3
    db_path = tmp_path / "instance.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("SELECT * FROM events ORDER BY timestamp"))
    conn.close()
    return [Event.from_row(r) for r in rows]
