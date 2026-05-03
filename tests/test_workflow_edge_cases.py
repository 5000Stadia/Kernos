"""Edge-case live test sweep.

WLP-GATE-SCOPING C3. Eleven representative scenarios exercising the
WORKFLOW-LOOP-PRIMITIVE under conditions closer to real-world
workflows than the unit-level invariant pins:

  1. Wait-until-X then Y (happy path)
  2. Periodic time.tick
  3. Conditional cascade with branching
  4. Multi-stage workflow with approval gate (happy path)
  5. Approval gate timeout — escalate_to_owner
  6. Approval gate bypass attempt — gate_nonce missing
  7. Variable target agent (multiple agent_ids)
  8. Variable tool composition (multiple tool_ids)
  9. Configuration gap — no AgentInbox provider
 10. Restart-resume mid-execution
 11. Idempotency suppression

Side-effect containment: every world-effect surface uses a local /
in-memory provider (in-mem deliver, test canvases, in-mem inbox,
stub tools, scoped state store). No external API calls; no public
side effects.

The runbook at ``data/diagnostics/live-tests/WLP-EDGE-CASES-live-test.md``
captures the narrative observations from running this suite.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from kernos.kernel import event_stream
from kernos.kernel.workflows.action_library import (
    ActionLibrary,
    AppendToLedgerAction,
    CallToolAction,
    MarkStateAction,
    NotifyUserAction,
    PostToServiceAction,
    RouteToAgentAction,
)
from kernos.kernel.workflows.agent_inbox import (
    AgentInboxUnavailable,
    APPROVAL_REQUEST_KEY,
    InMemoryAgentInbox,
)
from kernos.kernel.workflows.execution_engine import ExecutionEngine
from kernos.kernel.workflows.ledger import WorkflowLedger
from kernos.kernel.workflows.trigger_registry import (
    Trigger,
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
# Local/test providers
# ===========================================================================


class _LocalDeliveryStub:
    """In-memory replacement for the presence/adapter delivery
    surface. Captures every notify_user call for inspection."""

    def __init__(self) -> None:
        self.delivered: list[dict] = []

    async def deliver(self, **kw):
        self.delivered.append(kw)
        return {"persisted_id": f"local-{len(self.delivered)}"}


class _LocalCanvasStore:
    """In-memory canvas store. Stand-in for the production canvas
    write/read surface during sweep."""

    def __init__(self) -> None:
        self.canvases: dict[str, str] = {}

    async def write(self, *, canvas_id, content, mode, instance_id):
        if mode == "replace":
            self.canvases[canvas_id] = content
        else:
            self.canvases[canvas_id] = self.canvases.get(canvas_id, "") + content

    async def read(self, *, canvas_id, instance_id):
        return self.canvases.get(canvas_id, "")


class _LocalToolDispatcher:
    """In-memory tool dispatch. Each tool_id maps to a callable."""

    def __init__(self) -> None:
        self.tools: dict[str, callable] = {}
        self.call_log: list[tuple[str, dict]] = []

    def register(self, tool_id: str, fn) -> None:
        self.tools[tool_id] = fn

    async def dispatch(self, *, tool_id, args, instance_id, member_id):
        self.call_log.append((tool_id, args))
        if tool_id not in self.tools:
            raise RuntimeError(f"unknown tool: {tool_id}")
        return await self.tools[tool_id](args)


class _LocalServiceStub:
    """Local stub for workshop service registry. post() captures."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post(self, *, service_id, payload, instance_id):
        self.posts.append((service_id, payload))
        return {"service_persisted_id": f"svc-{len(self.posts)}"}


# ===========================================================================
# Stack fixture
# ===========================================================================


@pytest.fixture
async def stack(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    trig = TriggerRegistry()
    await trig.start(str(tmp_path))
    # DAR C4: pre-register the agent_ids the edge-case scenarios
    # use (Scenario 4 routes to "owner", Scenario 7 routes to
    # "spec-agent" + "code-agent", Scenario 9 routes to "x") so
    # registration-time validation succeeds. Tests still use the
    # legacy RouteToAgentAction(inbox=...) dispatch path.
    from kernos.kernel.agents.registry import AgentRecord, AgentRegistry
    agents = AgentRegistry()
    await agents.start(str(tmp_path))
    for aid in ("owner", "spec-agent", "code-agent", "x"):
        await agents._insert_record(AgentRecord(
            agent_id=aid, instance_id="inst_a",
            provider_key="legacy-noop",
        ))
    wfr = WorkflowRegistry()
    await wfr.start(str(tmp_path), trig)
    wfr.wire_agent_registry(agents)
    state: dict = {}

    async def state_set(*, key, value, scope, instance_id):
        state[(scope, instance_id, key)] = value

    async def state_get(*, key, scope, instance_id):
        return state.get((scope, instance_id, key))

    delivery = _LocalDeliveryStub()
    canvas = _LocalCanvasStore()
    tools = _LocalToolDispatcher()
    services = _LocalServiceStub()
    inbox = InMemoryAgentInbox()
    lib = ActionLibrary()
    lib.register(MarkStateAction(state_store_set=state_set, state_store_get=state_get))
    lib.register(NotifyUserAction(deliver_fn=delivery.deliver))
    lib.register(RouteToAgentAction(inbox=inbox))
    lib.register(CallToolAction(tool_dispatch_fn=tools.dispatch))
    lib.register(PostToServiceAction(service_post_fn=services.post))

    ledger_buf: dict[tuple, list] = {}

    async def ledger_append(*, workflow_id, entry, instance_id):
        ledger_buf.setdefault((instance_id, workflow_id), []).append(entry)

    async def ledger_read_last(*, workflow_id, instance_id):
        items = ledger_buf.get((instance_id, workflow_id))
        return items[-1] if items else None

    lib.register(AppendToLedgerAction(
        ledger_append_fn=ledger_append, ledger_read_last_fn=ledger_read_last,
    ))
    ledger = WorkflowLedger(str(tmp_path))
    engine = ExecutionEngine()
    await engine.start(str(tmp_path), trig, wfr, lib, ledger)
    yield {
        "tmp_path": tmp_path, "trig": trig, "wfr": wfr, "lib": lib,
        "ledger": ledger, "engine": engine, "state": state,
        "delivery": delivery, "canvas": canvas, "tools": tools,
        "services": services, "inbox": inbox, "agents": agents,
    }
    await engine.stop()
    await wfr.stop()
    await agents.stop()
    await _reset_trigger_registry(trig)
    await event_stream._reset_for_tests()


async def _wait_for(predicate, timeout=3.0, step=0.02):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


def _basic_wf(workflow_id, action_sequence, *, trigger=None, gates=None,
              instance_local=False) -> Workflow:
    return Workflow(
        workflow_id=workflow_id,
        instance_id="inst_a",
        name=workflow_id,
        description="",
        owner="owner",
        version="1.0",
        bounds=Bounds(iteration_count=1, wall_time_seconds=30),
        verifier=Verifier(flavor="deterministic", check="ok"),
        action_sequence=action_sequence,
        approval_gates=gates or [],
        trigger=trigger or TriggerDescriptor(
            event_type="cc.batch.report",
            predicate={"op": "exists", "path": "event_id"},
        ),
        instance_local=instance_local,
    )


def _action(action_type, *, gate_ref=None, on_failure="abort", **params):
    return ActionDescriptor(
        action_type=action_type,
        parameters=params,
        gate_ref=gate_ref,
        continuation_rules=ContinuationRules(on_failure=on_failure),
    )


# ===========================================================================
# 1. Wait-until-X then Y (happy path)
# ===========================================================================


class TestScenario1WaitThenAct:
    async def test_canvas_event_drives_notify(self, stack):
        wf = _basic_wf("ed-wait-then-act", action_sequence=[
            _action("mark_state", key="recorded", value=1, scope="instance"),
            _action("notify_user", channel="primary",
                    message="canvas updated", urgency="low"),
        ], trigger=TriggerDescriptor(
            event_type="canvas.written",
            predicate={"op": "eq", "path": "payload.canvas_id",
                       "value": "spec-canvas"},
        ))
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit(
            "inst_a", "canvas.written", {"canvas_id": "spec-canvas"},
        )
        await event_stream.flush_now()
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "recorded") in stack["state"]
            and len(stack["delivery"].delivered) == 1,
        )
        assert ok
        # Observed: workflow runs cleanly; both actions captured;
        # notification delivered through local stub (no external
        # side effects).
        assert stack["delivery"].delivered[0]["message"] == "canvas updated"


# ===========================================================================
# 2. Periodic time.tick
# ===========================================================================


class TestScenario2Periodic:
    async def test_daily_tick_fires_and_marks_state(self, stack):
        wf = _basic_wf("ed-daily", action_sequence=[
            _action("mark_state", key="briefing_at_8", value=True,
                    scope="instance"),
        ], trigger=TriggerDescriptor(
            event_type="time.tick",
            predicate={"op": "AND", "operands": [
                {"op": "eq", "path": "payload.cadence", "value": "daily"},
                {"op": "eq", "path": "payload.local_time", "value": "08:00"},
            ]},
        ))
        await stack["wfr"]._register_workflow_unbound(wf)
        # Two ticks at different times; only the 08:00 daily fires.
        await event_stream.emit(
            "inst_a", "time.tick",
            {"cadence": "hourly", "local_time": "07:00"},
        )
        await event_stream.emit(
            "inst_a", "time.tick",
            {"cadence": "daily", "local_time": "08:00"},
        )
        await event_stream.emit(
            "inst_a", "time.tick",
            {"cadence": "daily", "local_time": "12:00"},
        )
        await event_stream.flush_now()
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "briefing_at_8") in stack["state"],
        )
        assert ok
        # Only the 08:00 daily tick matched; the cadence-mismatch and
        # the 12:00 daily are correctly NOT firing because of the AND
        # predicate.
        execs = await stack["engine"].list_executions("inst_a")
        assert len([e for e in execs if e.workflow_id == "ed-daily"]) == 1


# ===========================================================================
# 3. Conditional cascade with branching (continuation=continue)
# ===========================================================================


class TestScenario3ConditionalCascade:
    async def test_failed_step_with_continue_cascades_to_next(self, stack):
        # Step 1: a tool that always fails.
        async def flaky(args):
            raise RuntimeError("tool failed")
        stack["tools"].register("flaky", flaky)
        # Step 2: a tool that always succeeds.
        async def ok(args):
            return {"ok": True}
        stack["tools"].register("ok", ok)
        wf = _basic_wf("ed-cascade", action_sequence=[
            _action("call_tool", on_failure="continue", tool_id="flaky"),
            _action("call_tool", tool_id="ok"),
            _action("mark_state", key="reached_end", value=True, scope="instance"),
        ])
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        ok_run = await _wait_for(
            lambda: ("instance", "inst_a", "reached_end") in stack["state"],
        )
        assert ok_run
        # Both tools were called (the second despite the first failing).
        called = [t for t, _ in stack["tools"].call_log]
        assert called == ["flaky", "ok"]


# ===========================================================================
# 4. Approval gate happy path
# ===========================================================================


class TestScenario4GateHappy:
    async def test_route_to_agent_then_pause_then_resume(self, stack):
        wf = _basic_wf("ed-gate-happy", instance_local=True, action_sequence=[
            ActionDescriptor(
                action_type="route_to_agent",
                gate_ref="g1",
                continuation_rules=ContinuationRules(on_failure="abort"),
                parameters={
                    "agent_id": "owner",
                    "payload": {
                        APPROVAL_REQUEST_KEY: {
                            "execution_id": "{workflow.execution_id}",
                            "gate_nonce": "{workflow.gate_nonce}",
                            "gate_name": "g1",
                            "pause_reason": "approve please",
                            "response_event_type": "user.approval",
                            "response_predicate": {
                                "op": "actor_eq", "value": "owner",
                            },
                        },
                    },
                },
            ),
            _action("mark_state", key="post_gate", value=1, scope="instance"),
        ], gates=[ApprovalGate(
            gate_name="g1", pause_reason="approve please",
            approval_event_type="user.approval",
            approval_event_predicate={"op": "actor_eq", "value": "owner"},
            timeout_seconds=5,
            bound_behavior_on_timeout="abort_workflow",
        )])
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        # Wait for inbox to receive the approval request.
        await _wait_for(
            lambda: bool(stack["inbox"]._items.get(("inst_a", "owner"), [])),
        )
        items = stack["inbox"]._items[("inst_a", "owner")]
        block = items[0].payload[APPROVAL_REQUEST_KEY]
        # Compose the approval response with the nonce echoed back.
        await event_stream.emit(
            "inst_a", "user.approval",
            {"execution_id": block["execution_id"],
             "gate_nonce": block["gate_nonce"]},
            member_id="owner",
        )
        await event_stream.flush_now()
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "post_gate") in stack["state"],
        )
        assert ok


# ===========================================================================
# 5. Approval gate timeout — escalate_to_owner
# ===========================================================================


class TestScenario5bGateAutoProceed:
    """The third timeout mode (Codex C3 review): a gate with
    bound_behavior_on_timeout=auto_proceed_with_default emits the
    auto-proceed event and continues with the next action when no
    approval arrives. Workflow-registry safe-deny ensures all
    downstream actions until the next gate are reversible."""

    async def test_timeout_continues_with_default_value(self, stack):
        wf = _basic_wf("ed-gate-auto", instance_local=True, action_sequence=[
            _action("mark_state", gate_ref="g1",
                    key="pre", value=1, scope="instance"),
            _action("mark_state", key="post", value=2, scope="instance"),
        ], gates=[ApprovalGate(
            gate_name="g1", pause_reason="confirm",
            approval_event_type="user.approval",
            approval_event_predicate={"op": "actor_eq", "value": "owner"},
            timeout_seconds=1,
            bound_behavior_on_timeout="auto_proceed_with_default",
            default_value="ok",
        )])
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        # 8-second wait gives plenty of headroom over the 1s gate
        # timeout for slow CI runners; the test was flaking at 4s
        # under full-suite load.
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "post") in stack["state"],
            timeout=8.0,
        )
        assert ok
        # gate_auto_proceeded event flushed with the default_value.
        events = await event_stream.events_in_window(
            "inst_a",
            datetime.fromisoformat("2020-01-01T00:00:00+00:00"),
            datetime.fromisoformat("2099-01-01T00:00:00+00:00"),
        )
        auto = [e for e in events
                if e.event_type == "workflow.gate_auto_proceeded"]
        assert len(auto) == 1
        assert auto[0].payload["default_value"] == "ok"
        # Execution completed (no abort).
        execs = await stack["engine"].list_executions(
            "inst_a", state="completed",
        )
        assert any(e.workflow_id == "ed-gate-auto" for e in execs)


class TestScenario5GateEscalate:
    async def test_no_approval_triggers_owner_escalation(self, stack):
        wf = _basic_wf("ed-gate-escalate", instance_local=True, action_sequence=[
            _action("mark_state", gate_ref="g1",
                    key="pre", value=1, scope="instance"),
            _action("mark_state", key="never", value=1, scope="instance"),
        ], gates=[ApprovalGate(
            gate_name="g1", pause_reason="confirm",
            approval_event_type="user.approval",
            approval_event_predicate={"op": "actor_eq", "value": "owner"},
            timeout_seconds=1,
            bound_behavior_on_timeout="escalate_to_owner",
        )])
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        await asyncio.sleep(2.0)
        # Owner escalation event flushed, execution aborted.
        events = await event_stream.events_in_window(
            "inst_a",
            datetime.fromisoformat("2020-01-01T00:00:00+00:00"),
            datetime.fromisoformat("2099-01-01T00:00:00+00:00"),
        )
        assert any(
            e.event_type == "workflow.owner_escalation" for e in events
        )
        execs = await stack["engine"].list_executions(
            "inst_a", state="aborted",
        )
        assert any(
            "gate_escalated" in e.aborted_reason for e in execs
        )
        # Post-gate action did NOT run.
        assert ("instance", "inst_a", "never") not in stack["state"]


# ===========================================================================
# 6. Approval gate bypass attempt — gate_nonce missing
# ===========================================================================


class TestScenario6BypassAttempt:
    """Three bypass attack shapes — every one must keep the
    execution paused."""

    async def _setup_paused(self, stack, workflow_id):
        wf = _basic_wf(workflow_id, instance_local=True, action_sequence=[
            _action("mark_state", gate_ref="g1",
                    key="pre", value=1, scope="instance"),
            _action("mark_state", key="post", value=2, scope="instance"),
        ], gates=[ApprovalGate(
            gate_name="g1", pause_reason="confirm",
            approval_event_type="user.approval",
            approval_event_predicate={"op": "actor_eq", "value": "owner"},
            timeout_seconds=10,
            bound_behavior_on_timeout="abort_workflow",
        )])
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "pre") in stack["state"],
        )
        assert ok
        execs = await stack["engine"].list_executions("inst_a")
        return next(e for e in execs if e.workflow_id == workflow_id and e.gate_nonce)

    async def test_missing_nonce_does_not_wake(self, stack):
        await self._setup_paused(stack, "ed-bypass-missing")
        await event_stream.emit(
            "inst_a", "user.approval", {}, member_id="owner",
        )
        await event_stream.flush_now()
        await asyncio.sleep(0.3)
        assert ("instance", "inst_a", "post") not in stack["state"]

    async def test_wrong_nonce_does_not_wake(self, stack):
        gated = await self._setup_paused(stack, "ed-bypass-wrong")
        await event_stream.emit(
            "inst_a", "user.approval",
            {"execution_id": gated.execution_id,
             "gate_nonce": "00000000-0000-0000-0000-deadbeefdead"},
            member_id="owner",
        )
        await event_stream.flush_now()
        await asyncio.sleep(0.3)
        assert ("instance", "inst_a", "post") not in stack["state"]

    async def test_wrong_execution_id_does_not_wake(self, stack):
        gated = await self._setup_paused(stack, "ed-bypass-execid")
        await event_stream.emit(
            "inst_a", "user.approval",
            {"execution_id": "different-execution-id",
             "gate_nonce": gated.gate_nonce},
            member_id="owner",
        )
        await event_stream.flush_now()
        await asyncio.sleep(0.3)
        assert ("instance", "inst_a", "post") not in stack["state"]

    async def test_replayed_approval_after_resume_no_op(self, stack):
        """Resume the execution with a real approval; replay the same
        approval event; verify no second resume / re-fire."""
        gated = await self._setup_paused(stack, "ed-bypass-replay")
        approval = {
            "execution_id": gated.execution_id,
            "gate_nonce": gated.gate_nonce,
        }
        await event_stream.emit(
            "inst_a", "user.approval", approval, member_id="owner",
        )
        await event_stream.flush_now()
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "post") in stack["state"],
        )
        assert ok
        # Replay identical approval. Should be a no-op.
        await event_stream.emit(
            "inst_a", "user.approval", approval, member_id="owner",
        )
        await event_stream.flush_now()
        await asyncio.sleep(0.2)
        # Same execution_id, no re-pause / re-resume.
        execs = await stack["engine"].list_executions("inst_a")
        target = next(e for e in execs if e.execution_id == gated.execution_id)
        assert target.state == "completed"
        assert target.gate_nonce == ""


# ===========================================================================
# 7. Variable target agent — multiple agent_ids in sequence
# ===========================================================================


class TestScenario7VariableAgents:
    async def test_route_to_two_agents_isolated(self, stack):
        wf = _basic_wf("ed-routes", instance_local=True, action_sequence=[
            _action("route_to_agent", agent_id="spec-agent",
                    payload={"task": "draft spec"}),
            _action("route_to_agent", agent_id="code-agent",
                    payload={"task": "implement spec"}),
        ])
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        await _wait_for(
            lambda: bool(stack["inbox"]._items.get(("inst_a", "spec-agent"), []))
            and bool(stack["inbox"]._items.get(("inst_a", "code-agent"), [])),
        )
        spec_items = stack["inbox"]._items[("inst_a", "spec-agent")]
        code_items = stack["inbox"]._items[("inst_a", "code-agent")]
        assert spec_items[0].payload == {"task": "draft spec"}
        assert code_items[0].payload == {"task": "implement spec"}
        # Cross-contamination check: each inbox carries only its own
        # payload.
        assert len(spec_items) == 1
        assert len(code_items) == 1


# ===========================================================================
# 8. Variable tool composition
# ===========================================================================


class TestScenario8VariableTools:
    async def test_three_different_tools_run_in_sequence(self, stack):
        async def read_doc(args):
            return {"text": f"contents of {args['path']}"}
        async def classify(args):
            return {"label": "important" if "alert" in args.get("text", "")
                    else "noise"}
        async def write_canvas(args):
            return {"written": args.get("canvas_id")}
        stack["tools"].register("read_doc", read_doc)
        stack["tools"].register("classify", classify)
        stack["tools"].register("write_canvas", write_canvas)
        wf = _basic_wf("ed-tools", action_sequence=[
            _action("call_tool", tool_id="read_doc",
                    args={"path": "alert.md"}),
            _action("call_tool", tool_id="classify",
                    args={"text": "alert: deploy"}),
            _action("call_tool", tool_id="write_canvas",
                    args={"canvas_id": "result"}),
        ])
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        await _wait_for(
            lambda: len(stack["tools"].call_log) >= 3,
        )
        called = [t for t, _ in stack["tools"].call_log]
        assert called == ["read_doc", "classify", "write_canvas"]


# ===========================================================================
# 9. Configuration gap — no AgentInbox provider
# ===========================================================================


class TestScenario9ConfigGap:
    async def test_route_to_agent_without_provider_aborts_loudly(
        self, stack, tmp_path,
    ):
        """Loud-failure assertions (Codex C3 review): not just
        "execution aborted" but also that the abort_reason names the
        raise step AND a workflow.execution_step_failed event flushed
        carrying the AgentInboxUnavailable error class."""
        from kernos.kernel.workflows.action_library import (
            ActionLibrary, RouteToAgentAction, MarkStateAction,
        )
        gap_lib = ActionLibrary()
        gap_lib.register(RouteToAgentAction(inbox=None))

        async def state_set(*, key, value, scope, instance_id):
            stack["state"][(scope, instance_id, key)] = value

        async def state_get(*, key, scope, instance_id):
            return stack["state"].get((scope, instance_id, key))
        gap_lib.register(MarkStateAction(
            state_store_set=state_set, state_store_get=state_get,
        ))
        stack["engine"]._action_library = gap_lib
        wf = _basic_wf("ed-config-gap", instance_local=True, action_sequence=[
            _action("route_to_agent",
                    agent_id="x", payload={"task": "y"}),
            _action("mark_state", key="never", value=1, scope="instance"),
        ])
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        # Poll for the execution_step_failed event rather than fixed sleep.
        async def step_failed_seen():
            events = await event_stream.events_in_window(
                "inst_a",
                datetime.fromisoformat("2020-01-01T00:00:00+00:00"),
                datetime.fromisoformat("2099-01-01T00:00:00+00:00"),
            )
            return any(
                e.event_type == "workflow.execution_step_failed"
                and "AgentInboxUnavailable" in str(e.payload.get("error", ""))
                for e in events
            )
        for _ in range(150):
            if await step_failed_seen():
                break
            await asyncio.sleep(0.02)
        # Loud-failure assertions:
        # 1. step_failed event payload carries the inner exception class.
        events = await event_stream.events_in_window(
            "inst_a",
            datetime.fromisoformat("2020-01-01T00:00:00+00:00"),
            datetime.fromisoformat("2099-01-01T00:00:00+00:00"),
        )
        step_failed_events = [
            e for e in events
            if e.event_type == "workflow.execution_step_failed"
        ]
        assert any(
            "AgentInboxUnavailable" in str(e.payload.get("error", ""))
            and e.payload.get("action_type") == "route_to_agent"
            for e in step_failed_events
        )
        # 2. Execution aborted with the matching step-raised reason.
        execs = await stack["engine"].list_executions(
            "inst_a", state="aborted",
        )
        target = next(e for e in execs if e.workflow_id == "ed-config-gap")
        assert "raised" in target.aborted_reason
        # 3. Post-gap action did NOT run.
        assert ("instance", "inst_a", "never") not in stack["state"]


# ===========================================================================
# 10. Restart-resume mid-execution (resume_safe path; abort path)
# ===========================================================================


class TestScenario10RestartResume:
    async def test_resume_safe_step_resumes_after_engine_restart(
        self, stack, tmp_path,
    ):
        # Action 0 ran already; action 1 is resume_safe; restart
        # re-enqueues at action 1.
        wf = _basic_wf("ed-resume-safe", action_sequence=[
            _action("mark_state", key="step0", value=1, scope="instance"),
            ActionDescriptor(
                action_type="mark_state",
                parameters={"key": "step1", "value": 2, "scope": "instance"},
                resume_safe=True,
                continuation_rules=ContinuationRules(on_failure="abort"),
            ),
        ])
        await stack["wfr"]._register_workflow_unbound(wf)
        # Seed a row mid-execution.
        await stack["engine"]._db.execute(
            "INSERT INTO workflow_executions ("
            " execution_id, workflow_id, instance_id, correlation_id,"
            " state, action_index_completed, intermediate_state,"
            " last_heartbeat, aborted_reason, started_at, terminated_at,"
            " trigger_event_payload, trigger_event_id, member_id,"
            " gate_nonce"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "exec-resume", "ed-resume-safe", "inst_a", "cor-1",
                "running", 0, "{}", "", "",
                datetime.now().isoformat(), "",
                "{}", "ev-x", "mem_a", "",
            ),
        )
        # Restart engine.
        await stack["engine"].stop()
        engine2 = ExecutionEngine()
        await engine2.start(
            str(tmp_path), stack["trig"], stack["wfr"],
            stack["lib"], stack["ledger"],
        )
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "step1") in stack["state"],
        )
        assert ok
        await engine2.stop()


# ===========================================================================
# 11. Idempotency suppression
# ===========================================================================


class TestScenario11Idempotency:
    async def test_duplicate_event_with_same_idempotency_key_suppressed(
        self, stack,
    ):
        wf = _basic_wf("ed-idempotent", action_sequence=[
            _action("mark_state", key="fired", value=1, scope="instance"),
        ], trigger=TriggerDescriptor(
            event_type="external.webhook",
            predicate={"op": "exists", "path": "event_id"},
            idempotency_key_template="{payload[task_id]}",
        ))
        await stack["wfr"]._register_workflow_unbound(wf)
        # Two events with the same task_id.
        await event_stream.emit(
            "inst_a", "external.webhook", {"task_id": "T-1"},
        )
        await event_stream.flush_now()
        await event_stream.emit(
            "inst_a", "external.webhook", {"task_id": "T-1"},
        )
        await event_stream.flush_now()
        await asyncio.sleep(0.3)
        # Only one execution row exists — second emit suppressed
        # at the trigger registry.
        execs = await stack["engine"].list_executions("inst_a")
        wf_execs = [e for e in execs if e.workflow_id == "ed-idempotent"]
        assert len(wf_execs) == 1
