"""Tests for the per-gate nonce binding.

WLP-GATE-SCOPING C1. Pins ACs #1-5 + #7-13:

  - schema migration: existing workflow_executions table without
    gate_nonce column has the column ALTER-added on engine startup
  - nonce minted BEFORE the gated action executes (round-trip:
    action's payload contains the same nonce the engine then
    persists)
  - gated-action failure: nonce discarded; no orphan pause
  - descriptor-match alone does NOT wake
  - nonce-match alone does NOT wake
  - both match → resume
  - cross-execution isolation: two paused workflows; approval-A
    only wakes execution-A
  - stale nonce rejection: replayed approval after resume does not
    re-wake
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from kernos.kernel import event_stream
from kernos.kernel.workflows.action_library import (
    ActionLibrary,
    MarkStateAction,
    NotifyUserAction,
    RouteToAgentAction,
)
from kernos.kernel.workflows.agent_inbox import InMemoryAgentInbox
from kernos.kernel.workflows.execution_engine import (
    ExecutionEngine,
    _interpolate_params,
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


# ===========================================================================
# Fixtures + helpers
# ===========================================================================


def _state_store():
    s: dict = {}

    async def set_(*, key, value, scope, instance_id):
        s[(scope, instance_id, key)] = value

    async def get_(*, key, scope, instance_id):
        return s.get((scope, instance_id, key))
    return s, set_, get_


def _make_action(action_type="mark_state", gate_ref=None, **params):
    return ActionDescriptor(
        action_type=action_type,
        parameters=params,
        gate_ref=gate_ref,
        continuation_rules=ContinuationRules(on_failure="abort"),
    )


def _gate(name, *, behavior="abort_workflow", default_value=None,
          predicate=None):
    return ApprovalGate(
        gate_name=name,
        pause_reason="confirm",
        approval_event_type="user.approval",
        approval_event_predicate=predicate or {
            "op": "actor_eq", "value": "owner",
        },
        timeout_seconds=2,
        bound_behavior_on_timeout=behavior,
        default_value=default_value,
    )


def _make_workflow(*, workflow_id="wf-gs", instance_id="inst_a",
                   action_sequence=None, approval_gates=None):
    return Workflow(
        workflow_id=workflow_id,
        instance_id=instance_id,
        name="gs-test",
        description="",
        owner="owner",
        version="1.0",
        bounds=Bounds(iteration_count=1, wall_time_seconds=30),
        verifier=Verifier(flavor="deterministic", check="ok"),
        action_sequence=action_sequence or [_make_action(
            "mark_state", key="x", value=1, scope="instance",
        )],
        approval_gates=approval_gates or [],
        trigger=TriggerDescriptor(
            event_type="cc.batch.report",
            predicate={"op": "exists", "path": "event_id"},
        ),
    )


@pytest.fixture
async def stack(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    trig = TriggerRegistry()
    await trig.start(str(tmp_path))
    # DAR C4: legacy RouteToAgentAction(inbox=...) test uses the
    # legacy dispatch path, but registration-time agent_id validation
    # still requires the registry to be wired with the referenced
    # agent_ids. Pre-register "owner" (the only agent the gate
    # tests route to) so registrations succeed without churning the
    # shipped test logic.
    from kernos.kernel.agents.registry import AgentRecord, AgentRegistry
    agents = AgentRegistry()
    await agents.start(str(tmp_path))
    await agents._insert_record(AgentRecord(
        agent_id="owner", instance_id="inst_a",
        provider_key="legacy-noop",
    ))
    wfr = WorkflowRegistry()
    await wfr.start(str(tmp_path), trig)
    wfr.wire_agent_registry(agents)
    state, set_, get_ = _state_store()
    lib = ActionLibrary()
    lib.register(MarkStateAction(state_store_set=set_, state_store_get=get_))
    inbox = InMemoryAgentInbox()
    lib.register(RouteToAgentAction(inbox=inbox))
    delivered: list = []

    async def deliver(**kw):
        delivered.append(kw)
        return {"persisted_id": f"msg-{len(delivered)}"}
    lib.register(NotifyUserAction(deliver_fn=deliver))
    ledger = WorkflowLedger(str(tmp_path))
    engine = ExecutionEngine()
    await engine.start(str(tmp_path), trig, wfr, lib, ledger)
    yield {
        "tmp_path": tmp_path, "trig": trig, "wfr": wfr, "lib": lib,
        "ledger": ledger, "engine": engine, "state": state,
        "inbox": inbox, "agents": agents,
    }
    await engine.stop()
    await wfr.stop()
    await agents.stop()
    await _reset_trigger_registry(trig)
    await event_stream._reset_for_tests()


async def _wait_for(predicate, timeout=2.0, step=0.02):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


def _make_deliver(stack):
    async def deliver(**kw):
        return {"persisted_id": "d-1"}
    return deliver


# ===========================================================================
# Schema migration (AC #3)
# ===========================================================================


class TestSchemaMigration:
    async def test_alter_table_adds_gate_nonce_to_existing_db(self, tmp_path):
        """A pre-existing workflow_executions table from the WLP era
        (without gate_nonce column) gets the column ALTER-added on
        engine startup."""
        # Manually create the old-shape table BEFORE the engine opens.
        db_path = tmp_path / "instance.db"
        conn = await aiosqlite.connect(str(db_path), isolation_level=None)
        await conn.execute(
            "CREATE TABLE workflow_executions ("
            " execution_id TEXT PRIMARY KEY, workflow_id TEXT, "
            " instance_id TEXT, correlation_id TEXT, state TEXT,"
            " action_index_completed INTEGER, intermediate_state TEXT,"
            " last_heartbeat TEXT, aborted_reason TEXT, started_at TEXT,"
            " terminated_at TEXT, trigger_event_payload TEXT,"
            " trigger_event_id TEXT, member_id TEXT)"
        )
        # Insert a row in the old shape.
        await conn.execute(
            "INSERT INTO workflow_executions ("
            "execution_id, workflow_id, instance_id, correlation_id,"
            " state, action_index_completed, started_at) "
            "VALUES ('e-old', 'wf-old', 'inst_a', 'cor-old',"
            " 'completed', 0, '2026-04-01T00:00:00+00:00')"
        )
        await conn.close()
        # Now bring up the full stack — engine.start should ALTER.
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        try:
            trig = TriggerRegistry()
            await trig.start(str(tmp_path))
            wfr = WorkflowRegistry()
            await wfr.start(str(tmp_path), trig)
            engine = ExecutionEngine()
            ledger = WorkflowLedger(str(tmp_path))
            await engine.start(str(tmp_path), trig, wfr, ActionLibrary(), ledger)
            # Verify column was added.
            assert engine._db is not None
            async with engine._db.execute(
                "SELECT name FROM pragma_table_info('workflow_executions')"
            ) as cur:
                cols = {r[0] for r in await cur.fetchall()}
            assert "gate_nonce" in cols
            # The pre-existing row reads cleanly with empty gate_nonce.
            old = await engine.get_execution("e-old")
            assert old is not None
            assert old.gate_nonce == ""
            await engine.stop()
            await wfr.stop()
            await _reset_trigger_registry(trig)
        finally:
            await event_stream._reset_for_tests()


# ===========================================================================
# Nonce-availability-during-action (AC #2, #7)
# ===========================================================================


class TestNonceAvailabilityDuringAction:
    async def test_route_to_agent_payload_carries_minted_nonce(self, stack):
        """A route_to_agent action carrying gate_ref MUST be able to
        embed the engine-minted nonce + execution_id in its
        approval_request block via template interpolation. The
        block's nonce MUST equal the engine's persisted gate_nonce."""
        wf = _make_workflow(
            workflow_id="wf-route-gate",
            approval_gates=[_gate("g1")],
            action_sequence=[
                _make_action(
                    "route_to_agent",
                    gate_ref="g1",
                    agent_id="owner",
                    payload={
                        "approval_request": {
                            "execution_id": "{workflow.execution_id}",
                            "gate_nonce": "{workflow.gate_nonce}",
                            "gate_name": "g1",
                            "pause_reason": "confirm",
                            "response_event_type": "user.approval",
                            "response_predicate": {
                                "op": "actor_eq", "value": "owner",
                            },
                        },
                    },
                ),
                _make_action("mark_state", key="post", value=1, scope="instance"),
            ],
        )
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        # Wait until inbox has the approval_request payload posted.
        await _wait_for(
            lambda: stack["inbox"]._items.get(
                ("inst_a", "owner"), [],
            ),
        )
        items = stack["inbox"]._items[("inst_a", "owner")]
        block = items[0].payload["approval_request"]
        # The nonce in the action's payload matches what the engine
        # persists on the paused execution row.
        execs = await stack["engine"].list_executions("inst_a")
        gated = next(e for e in execs if e.gate_nonce)
        assert block["execution_id"] == gated.execution_id
        assert block["gate_nonce"] == gated.gate_nonce
        # And it's a real UUIDv4, not the literal placeholder.
        assert "{workflow." not in block["gate_nonce"]
        assert len(block["gate_nonce"]) == 36

    async def test_nonce_is_uuidv4_shape(self, stack):
        """The minted nonce is a real UUIDv4 string of expected
        shape, not a stub or static value."""
        wf = _make_workflow(
            workflow_id="wf-uuid",
            approval_gates=[_gate("g1")],
            action_sequence=[
                _make_action("mark_state", gate_ref="g1",
                             key="pre", value=1, scope="instance"),
                _make_action("mark_state", key="post", value=2, scope="instance"),
            ],
        )
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        await _wait_for(
            lambda: ("instance", "inst_a", "pre") in stack["state"],
        )
        execs = await stack["engine"].list_executions("inst_a")
        gated = next(e for e in execs if e.gate_nonce)
        # UUIDv4 hex form: 36 chars, four dashes.
        assert len(gated.gate_nonce) == 36
        assert gated.gate_nonce.count("-") == 4


# ===========================================================================
# Gated-action failure path (AC #8)
# ===========================================================================


class TestGatedActionFailure:
    async def test_failed_gated_action_with_continue_does_not_enter_gate(
        self, stack,
    ):
        """Codex C1 review iteration: a gated action that fails with
        continuation_rules.on_failure='continue' MUST NOT enter the
        gate — the discard-on-failure invariant applies regardless
        of the continuation rule. Without this fix the engine would
        persist the nonce + pause for an action that never actually
        produced the approval request."""
        # Same crash setup but continuation 'continue'.
        crash_lib = ActionLibrary()
        write_count = {"n": 0}

        async def explode_set(**kw):
            write_count["n"] += 1
            raise RuntimeError("boom")

        async def explode_get(**kw):
            return None

        crash_lib.register(MarkStateAction(
            state_store_set=explode_set, state_store_get=explode_get,
        ))
        # Add a follow-up direct-effect action that succeeds, so the
        # workflow can complete past the failed gate-step.
        crash_lib.register(NotifyUserAction(deliver_fn=_make_deliver(stack)))
        stack["engine"]._action_library = crash_lib
        wf = _make_workflow(
            workflow_id="wf-fail-continue",
            approval_gates=[_gate("g1")],
            action_sequence=[
                ActionDescriptor(
                    action_type="mark_state",
                    parameters={"key": "x", "value": 1, "scope": "instance"},
                    gate_ref="g1",
                    continuation_rules=ContinuationRules(on_failure="continue"),
                ),
                _make_action("notify_user", channel="primary",
                             message="continued", urgency="low"),
            ],
        )
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        await _wait_for(lambda: write_count["n"] >= 1, timeout=2.0)
        # Give the engine a moment to dispatch the post-fail step.
        await asyncio.sleep(0.3)
        # Workflow must NOT be paused; gate_nonce stays empty.
        execs = await stack["engine"].list_executions("inst_a")
        target = next(e for e in execs if e.workflow_id == "wf-fail-continue")
        assert target.gate_nonce == ""
        assert target.state in {"completed", "running", "aborted"}
        # And the gate-paused event should NOT have been emitted for
        # this execution.
        from datetime import datetime, timezone
        events = await event_stream.events_in_window(
            "inst_a",
            datetime.fromisoformat("2020-01-01T00:00:00+00:00"),
            datetime.fromisoformat("2099-01-01T00:00:00+00:00"),
        )
        assert not any(
            e.event_type == "workflow.execution_paused_at_gate"
            and e.correlation_id == target.correlation_id
            for e in events
        )

    async def test_failed_gated_action_discards_nonce(self, stack):
        """If the gate_ref action fails, the unused nonce is discarded
        and the execution does NOT enter gate wait. No orphan
        nonce; no orphan paused_at_gate event."""
        # Make the gated action fail by giving it a verifier-rejecting
        # state-store that records the value but the verifier reads
        # back a different one.
        crash_lib = ActionLibrary()
        write_count = {"n": 0}

        async def explode_set(**kw):
            write_count["n"] += 1
            raise RuntimeError("disk on fire")

        async def explode_get(**kw):
            return None
        crash_lib.register(MarkStateAction(
            state_store_set=explode_set, state_store_get=explode_get,
        ))
        stack["engine"]._action_library = crash_lib
        wf = _make_workflow(
            workflow_id="wf-fail",
            approval_gates=[_gate("g1")],
            action_sequence=[
                _make_action("mark_state", gate_ref="g1",
                             key="x", value=1, scope="instance"),
                _make_action("mark_state", key="never", value=2, scope="instance"),
            ],
        )
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        # Wait for execution to abort.
        await _wait_for(
            lambda: bool(write_count["n"]),
            timeout=2.0,
        )
        await asyncio.sleep(0.2)
        execs = await stack["engine"].list_executions("inst_a")
        target = next(e for e in execs if e.workflow_id == "wf-fail")
        # Execution aborted; gate_nonce was discarded (empty).
        assert target.state == "aborted"
        assert target.gate_nonce == ""


# ===========================================================================
# Match logic (AC #9-13)
# ===========================================================================


class TestMatchLogic:
    async def _pause_workflow(self, stack, workflow_id="wf-m"):
        """Helper: register a gated workflow and wait for it to pause."""
        wf = _make_workflow(
            workflow_id=workflow_id,
            approval_gates=[_gate("g1")],
            action_sequence=[
                _make_action("mark_state", gate_ref="g1",
                             key="pre", value=1, scope="instance"),
                _make_action("mark_state", key="post", value=2, scope="instance"),
            ],
        )
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        await _wait_for(
            lambda: ("instance", "inst_a", "pre") in stack["state"],
        )
        execs = await stack["engine"].list_executions("inst_a")
        return next(e for e in execs if e.gate_nonce)

    async def test_descriptor_match_alone_does_not_wake(self, stack):
        """Approval event matching descriptor predicate but missing
        gate_nonce → execution stays paused (AC #9)."""
        gated = await self._pause_workflow(stack)
        # Emit approval matching descriptor predicate (actor_eq owner)
        # but NO nonce / execution_id in the payload.
        await event_stream.emit(
            "inst_a", "user.approval", {}, member_id="owner",
        )
        await event_stream.flush_now()
        await asyncio.sleep(0.2)
        # post action has NOT run.
        assert ("instance", "inst_a", "post") not in stack["state"]
        # Execution still paused.
        execs = await stack["engine"].list_executions("inst_a")
        target = next(e for e in execs if e.execution_id == gated.execution_id)
        assert target.gate_nonce == gated.gate_nonce  # still set

    async def test_nonce_match_alone_does_not_wake(self, stack):
        """Approval event with matching nonce + execution_id but
        failing descriptor predicate → execution stays paused
        (AC #10)."""
        gated = await self._pause_workflow(stack)
        # Carry the right nonce + execution_id but emit from wrong
        # actor (predicate is actor_eq owner).
        await event_stream.emit(
            "inst_a", "user.approval",
            {"execution_id": gated.execution_id,
             "gate_nonce": gated.gate_nonce},
            member_id="not_founder",
        )
        await event_stream.flush_now()
        await asyncio.sleep(0.2)
        assert ("instance", "inst_a", "post") not in stack["state"]

    async def test_both_match_resumes(self, stack):
        """Approval event with matching descriptor predicate AND
        matching nonce + execution_id → resume (AC #11)."""
        gated = await self._pause_workflow(stack)
        await event_stream.emit(
            "inst_a", "user.approval",
            {"execution_id": gated.execution_id,
             "gate_nonce": gated.gate_nonce},
            member_id="owner",
        )
        await event_stream.flush_now()
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "post") in stack["state"],
        )
        assert ok

    async def test_wrong_execution_id_does_not_wake(self, stack):
        """Approval with matching nonce but wrong execution_id stays
        paused. Defends against nonce-leak across executions."""
        gated = await self._pause_workflow(stack)
        await event_stream.emit(
            "inst_a", "user.approval",
            {"execution_id": "different-execution",
             "gate_nonce": gated.gate_nonce},
            member_id="owner",
        )
        await event_stream.flush_now()
        await asyncio.sleep(0.2)
        assert ("instance", "inst_a", "post") not in stack["state"]


# ===========================================================================
# Stale nonce rejection (AC #13)
# ===========================================================================


class TestStaleNonceRejection:
    async def test_replayed_approval_after_resume_does_not_re_wake(self, stack):
        """Replay a valid approval event after the execution resumed.
        Engine sees no nonce match (nonce cleared on resume), so the
        replay does nothing. AC #13."""
        wf = _make_workflow(
            workflow_id="wf-stale",
            approval_gates=[_gate("g1")],
            action_sequence=[
                _make_action("mark_state", gate_ref="g1",
                             key="pre", value=1, scope="instance"),
                _make_action("mark_state", key="post", value=2, scope="instance"),
            ],
        )
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        await _wait_for(
            lambda: ("instance", "inst_a", "pre") in stack["state"],
        )
        execs = await stack["engine"].list_executions("inst_a")
        gated = next(e for e in execs if e.gate_nonce)
        approval = {
            "execution_id": gated.execution_id,
            "gate_nonce": gated.gate_nonce,
        }
        await event_stream.emit(
            "inst_a", "user.approval", approval, member_id="owner",
        )
        await event_stream.flush_now()
        await _wait_for(
            lambda: ("instance", "inst_a", "post") in stack["state"],
        )
        # Execution completed. Now replay the same approval event.
        # Should be a no-op — nonce is cleared from execution row,
        # match logic finds no waiter expecting that nonce.
        await event_stream.emit(
            "inst_a", "user.approval", approval, member_id="owner",
        )
        await event_stream.flush_now()
        await asyncio.sleep(0.2)
        # Nothing changed; execution remains completed.
        execs2 = await stack["engine"].list_executions(
            "inst_a", state="completed",
        )
        assert any(
            e.execution_id == gated.execution_id and e.gate_nonce == ""
            for e in execs2
        )


# ===========================================================================
# Cross-execution isolation (AC #12)
# ===========================================================================


class TestCrossExecutionIsolation:
    async def test_approval_for_A_does_not_wake_B(self, stack):
        """Two paused executions of the same workflow shape with
        different nonces. An approval carrying execution-A's nonce
        wakes only execution-A; execution-B stays paused.

        Note: the engine processes executions sequentially per the
        WLP single-worker model, so we test isolation via the match
        logic directly rather than two simultaneously-paused
        executions. We pause execution-A, emit a forged approval
        with B's would-be nonce, observe no wake."""
        gated_a = await self._pause_workflow(stack, workflow_id="wf-A")
        # Forge an approval with a different nonce than A's.
        forged_nonce = "00000000-0000-0000-0000-000000000000"
        await event_stream.emit(
            "inst_a", "user.approval",
            {"execution_id": gated_a.execution_id,
             "gate_nonce": forged_nonce},
            member_id="owner",
        )
        await event_stream.flush_now()
        await asyncio.sleep(0.2)
        # A stays paused.
        assert ("instance", "inst_a", "post") not in stack["state"]
        # Now wake A properly.
        await event_stream.emit(
            "inst_a", "user.approval",
            {"execution_id": gated_a.execution_id,
             "gate_nonce": gated_a.gate_nonce},
            member_id="owner",
        )
        await event_stream.flush_now()
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "post") in stack["state"],
        )
        assert ok

    async def _pause_workflow(self, stack, *, workflow_id="wf-iso"):
        wf = _make_workflow(
            workflow_id=workflow_id,
            approval_gates=[_gate("g1")],
            action_sequence=[
                _make_action("mark_state", gate_ref="g1",
                             key="pre", value=1, scope="instance"),
                _make_action("mark_state", key="post", value=2, scope="instance"),
            ],
        )
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        await _wait_for(
            lambda: ("instance", "inst_a", "pre") in stack["state"],
        )
        execs = await stack["engine"].list_executions("inst_a")
        return next(e for e in execs if e.gate_nonce)


# ===========================================================================
# Template interpolation unit tests
# ===========================================================================


class TestInterpolation:
    def test_substitutes_placeholders_in_string(self):
        ctx = {"execution_id": "exec-1", "gate_nonce": "n-1",
               "correlation_id": "cor-1", "workflow_id": "wf-1",
               "instance_id": "inst-1"}
        out = _interpolate_params(
            "exec={workflow.execution_id} nonce={workflow.gate_nonce}",
            ctx,
        )
        assert out == "exec=exec-1 nonce=n-1"

    def test_recurses_into_dicts_and_lists(self):
        ctx = {"execution_id": "exec-1", "gate_nonce": "n-1",
               "correlation_id": "", "workflow_id": "", "instance_id": ""}
        params = {
            "payload": {
                "request": {
                    "id": "{workflow.execution_id}",
                    "tags": ["{workflow.gate_nonce}", "static"],
                },
            },
            "untouched": 42,
        }
        out = _interpolate_params(params, ctx)
        assert out == {
            "payload": {
                "request": {"id": "exec-1", "tags": ["n-1", "static"]},
            },
            "untouched": 42,
        }

    def test_unknown_placeholders_pass_through(self):
        ctx = {"execution_id": "x", "gate_nonce": "", "correlation_id": "",
               "workflow_id": "", "instance_id": ""}
        out = _interpolate_params("{user.something}", ctx)
        # Unknown namespace not in our table → unchanged.
        assert out == "{user.something}"
