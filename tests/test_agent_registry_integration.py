"""DAR C4 integration tests.

Pins:
  - RouteToAgentAction registry path (AC #10 typed errors,
    AC #11 verifier snapshot insulated from registry mutation,
    AC #13 approval flow round-trip)
  - register_workflow agent_id validation (AC #8, AC #9)
  - Conversational routing thin consumer (AC #14)
"""
from __future__ import annotations

import asyncio

import pytest

from kernos.kernel import event_stream
from kernos.kernel.agents.conversational_routing import (
    AskClarification,
    DispatchTo,
    Unknown,
    route_phrase_to_agent,
)
from kernos.kernel.agents.providers import ProviderRegistry
from kernos.kernel.agents.registry import (
    AgentInboxProviderUnavailable,
    AgentNotRegistered,
    AgentPaused,
    AgentRecord,
    AgentRegistry,
    AgentRetired,
)
from kernos.kernel.workflows.action_library import (
    ActionLibrary,
    MarkStateAction,
    RouteToAgentAction,
)
from kernos.kernel.workflows.agent_inbox import (
    APPROVAL_REQUEST_KEY,
    InMemoryAgentInbox,
)
from kernos.kernel.workflows.execution_engine import ExecutionEngine
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
    WorkflowError,
    WorkflowRegistry,
)


# ===========================================================================
# Stack helpers
# ===========================================================================


def _make_provider_registry_with_shared_inbox():
    """Build a ProviderRegistry whose factories all return the
    SAME InMemoryAgentInbox so tests can read posts back. Each
    distinct provider_config_ref maps to a separate inbox so the
    snapshot-vs-current-config tests work."""
    inboxes_by_config: dict[str, InMemoryAgentInbox] = {}

    def factory_for(config_ref: str):
        if config_ref not in inboxes_by_config:
            inboxes_by_config[config_ref] = InMemoryAgentInbox()
        return inboxes_by_config[config_ref]

    pr = ProviderRegistry()
    pr.register("inmemory", factory_for)
    return pr, inboxes_by_config


@pytest.fixture
async def stack(tmp_path):
    """Full DAR-aware stack."""
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    pr, inboxes = _make_provider_registry_with_shared_inbox()
    agents = AgentRegistry(provider_registry=pr)
    await agents.start(str(tmp_path))
    trig = TriggerRegistry()
    await trig.start(str(tmp_path))
    wfr = WorkflowRegistry()
    await wfr.start(str(tmp_path), trig)
    wfr.wire_agent_registry(agents)
    state: dict = {}

    async def state_set(*, key, value, scope, instance_id):
        state[(scope, instance_id, key)] = value

    async def state_get(*, key, scope, instance_id):
        return state.get((scope, instance_id, key))

    lib = ActionLibrary()
    lib.register(MarkStateAction(state_store_set=state_set, state_store_get=state_get))
    lib.register(RouteToAgentAction(registry=agents))
    ledger = WorkflowLedger(str(tmp_path))
    engine = ExecutionEngine()
    await engine.start(str(tmp_path), trig, wfr, lib, ledger)
    yield {
        "tmp_path": tmp_path, "agents": agents, "providers": pr,
        "inboxes": inboxes, "trig": trig, "wfr": wfr, "lib": lib,
        "ledger": ledger, "engine": engine, "state": state,
    }
    await engine.stop()
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await agents.stop()
    await event_stream._reset_for_tests()


def _agent_record(**overrides) -> AgentRecord:
    base = dict(
        agent_id="spec-agent",
        instance_id="inst_a",
        display_name="Spec drafter",
        aliases=[],
        provider_key="inmemory",
        provider_config_ref="default",
        domain_summary="",
        capabilities_summary="",
        status="active",
    )
    base.update(overrides)
    return AgentRecord(**base)


async def _wait_for(predicate, timeout=2.0, step=0.02):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


# ===========================================================================
# RouteToAgentAction registry path (AC #10)
# ===========================================================================


class TestRouteToAgentRegistryPath:
    async def test_constructor_rejects_both_inbox_and_registry(self, stack):
        with pytest.raises(ValueError, match="not both"):
            RouteToAgentAction(
                inbox=InMemoryAgentInbox(),
                registry=stack["agents"],
            )

    async def test_neither_inbox_nor_registry_raises_unavailable(self):
        from kernos.kernel.workflows.agent_inbox import AgentInboxUnavailable
        verb = RouteToAgentAction()  # neither
        with pytest.raises(AgentInboxUnavailable):
            await verb.execute(_Ctx(), {"agent_id": "x", "payload": {}})

    async def test_unregistered_agent_id_raises_AgentNotRegistered(
        self, stack,
    ):
        verb = RouteToAgentAction(registry=stack["agents"])
        with pytest.raises(AgentNotRegistered):
            await verb.execute(
                _Ctx(),
                {"agent_id": "nonexistent", "payload": {"x": 1}},
            )

    async def test_paused_agent_raises_AgentPaused(self, stack):
        await stack["agents"].register_agent(_agent_record())
        await stack["agents"].update_status(
            "spec-agent", "inst_a", "paused",
        )
        verb = RouteToAgentAction(registry=stack["agents"])
        with pytest.raises(AgentPaused):
            await verb.execute(
                _Ctx(),
                {"agent_id": "spec-agent", "payload": {"x": 1}},
            )

    async def test_retired_agent_raises_AgentRetired(self, stack):
        await stack["agents"].register_agent(_agent_record())
        await stack["agents"].update_status(
            "spec-agent", "inst_a", "retired",
        )
        verb = RouteToAgentAction(registry=stack["agents"])
        with pytest.raises(AgentRetired):
            await verb.execute(
                _Ctx(),
                {"agent_id": "spec-agent", "payload": {"x": 1}},
            )

    async def test_unbound_provider_key_raises_provider_unavailable(
        self, stack,
    ):
        # Register an agent with a provider_key that has no factory.
        await stack["agents"]._insert_record(_agent_record(
            provider_key="unbound-key",
        ))
        verb = RouteToAgentAction(registry=stack["agents"])
        with pytest.raises(AgentInboxProviderUnavailable):
            await verb.execute(
                _Ctx(),
                {"agent_id": "spec-agent", "payload": {"x": 1}},
            )

    async def test_happy_path_post_lands_via_registry(self, stack):
        await stack["agents"].register_agent(_agent_record())
        verb = RouteToAgentAction(registry=stack["agents"])
        result = await verb.execute(
            _Ctx(),
            {"agent_id": "spec-agent", "payload": {"task": "draft"}},
        )
        assert result.success
        # Receipt carries the snapshot.
        assert result.receipt["agent_id"] == "spec-agent"
        assert result.receipt["provider_key"] == "inmemory"
        assert result.receipt["provider_config_ref"] == "default"
        assert result.receipt["persisted_id"]
        # And the inbox has the post.
        inbox = stack["inboxes"]["default"]
        items = await inbox.read(agent_id="spec-agent", instance_id="inst_a")
        assert len(items) == 1
        assert items[0].payload == {"task": "draft"}


# ===========================================================================
# Verifier snapshot insulated from registry mutation (AC #11)
# ===========================================================================


class TestVerifierSnapshot:
    async def test_verify_uses_snapshot_after_provider_config_changes(
        self, stack,
    ):
        """Edit the registry to point at a different
        provider_config_ref between execute and verify; verify
        must still hit the original provider via the snapshot."""
        await stack["agents"].register_agent(_agent_record(
            provider_config_ref="config-A",
        ))
        verb = RouteToAgentAction(registry=stack["agents"])
        ctx = _Ctx()
        result = await verb.execute(
            ctx, {"agent_id": "spec-agent", "payload": {"v": 1}},
        )
        assert result.success
        # Forcibly mutate the registry row to point at config-B.
        # (No update_record API yet; do it via raw SQL to simulate
        # the architectural concern the design review flagged.)
        await stack["agents"]._db.execute(
            "UPDATE agent_records SET provider_config_ref = ? "
            "WHERE instance_id = ? AND agent_id = ?",
            ("config-B", "inst_a", "spec-agent"),
        )
        # Verify reads the SNAPSHOT (config-A), not the current row
        # (config-B). The original inbox has the post; the new
        # inbox doesn't.
        verified = await verb.verify(
            ctx, {"agent_id": "spec-agent"}, result,
        )
        assert verified is True
        # Sanity: the new inbox is empty (verifier didn't
        # accidentally re-resolve through the registry).
        new_inbox = stack["inboxes"].get("config-B")
        if new_inbox is not None:
            new_items = await new_inbox.read(
                agent_id="spec-agent", instance_id="inst_a",
            )
            # No post should have landed in config-B's inbox.
            assert all(
                i.persisted_id != result.receipt["persisted_id"]
                for i in new_items
            )

    async def test_verify_after_agent_paused(self, stack):
        """Pause the agent between execute and verify; verify
        still completes successfully against the snapshotted
        provider — agent lifecycle doesn't disturb verification."""
        await stack["agents"].register_agent(_agent_record())
        verb = RouteToAgentAction(registry=stack["agents"])
        ctx = _Ctx()
        result = await verb.execute(
            ctx, {"agent_id": "spec-agent", "payload": {"v": 1}},
        )
        # Pause AFTER execute.
        await stack["agents"].update_status(
            "spec-agent", "inst_a", "paused",
        )
        verified = await verb.verify(
            ctx, {"agent_id": "spec-agent"}, result,
        )
        assert verified is True


# ===========================================================================
# register_workflow agent_id validation (AC #8, #9)
# ===========================================================================


class TestWorkflowAgentValidation:
    async def test_unregistered_agent_id_fails_workflow_registration(
        self, stack,
    ):
        wf = _build_workflow(
            workflow_id="wf-bad",
            action_sequence=[ActionDescriptor(
                action_type="route_to_agent",
                parameters={"agent_id": "nonexistent",
                            "payload": {"x": 1}},
            )],
        )
        with pytest.raises(WorkflowError, match="not registered"):
            await stack["wfr"]._register_workflow_unbound(wf)
        # No partial state — workflow row not persisted.
        wfs = await stack["wfr"].list_workflows("inst_a")
        assert all(w.workflow_id != "wf-bad" for w in wfs)

    async def test_paused_agent_id_fails_workflow_registration(self, stack):
        await stack["agents"].register_agent(_agent_record())
        await stack["agents"].update_status(
            "spec-agent", "inst_a", "paused",
        )
        wf = _build_workflow(
            workflow_id="wf-paused",
            action_sequence=[ActionDescriptor(
                action_type="route_to_agent",
                parameters={"agent_id": "spec-agent",
                            "payload": {"x": 1}},
            )],
        )
        with pytest.raises(WorkflowError, match="paused"):
            await stack["wfr"]._register_workflow_unbound(wf)

    async def test_retired_agent_id_fails_workflow_registration(self, stack):
        await stack["agents"].register_agent(_agent_record())
        await stack["agents"].update_status(
            "spec-agent", "inst_a", "retired",
        )
        wf = _build_workflow(
            workflow_id="wf-retired",
            action_sequence=[ActionDescriptor(
                action_type="route_to_agent",
                parameters={"agent_id": "spec-agent",
                            "payload": {"x": 1}},
            )],
        )
        with pytest.raises(WorkflowError, match="retired"):
            await stack["wfr"]._register_workflow_unbound(wf)

    async def test_default_reference_syntax_rejected(self, stack):
        await stack["agents"].register_agent(_agent_record())
        wf = _build_workflow(
            workflow_id="wf-default-ref",
            action_sequence=[ActionDescriptor(
                action_type="route_to_agent",
                parameters={"agent_id": "@default:code-review",
                            "payload": {"x": 1}},
            )],
        )
        with pytest.raises(WorkflowError, match="@default:"):
            await stack["wfr"]._register_workflow_unbound(wf)

    async def test_active_agent_id_succeeds(self, stack):
        await stack["agents"].register_agent(_agent_record())
        wf = _build_workflow(
            workflow_id="wf-good",
            action_sequence=[ActionDescriptor(
                action_type="route_to_agent",
                parameters={"agent_id": "spec-agent",
                            "payload": {"x": 1}},
            )],
        )
        registered = await stack["wfr"]._register_workflow_unbound(wf)
        assert registered.workflow_id == "wf-good"

    async def test_route_to_agent_workflow_without_registry_wired_fails(
        self, tmp_path,
    ):
        """Codex consolidated DAR review: when a workflow contains
        route_to_agent and no AgentRegistry is wired into
        WorkflowRegistry, registration MUST fail closed. AC #8 / #9
        are not bypassable by forgetting to wire the registry."""
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        try:
            trig = TriggerRegistry()
            await trig.start(str(tmp_path))
            wfr = WorkflowRegistry()
            await wfr.start(str(tmp_path), trig)
            # NOTE: deliberately not calling wire_agent_registry.
            wf = _build_workflow(
                workflow_id="wf-no-registry",
                action_sequence=[ActionDescriptor(
                    action_type="route_to_agent",
                    parameters={"agent_id": "anything",
                                "payload": {}},
                )],
            )
            with pytest.raises(WorkflowError, match="agent registry"):
                await wfr._register_workflow_unbound(wf)
            # No partial state.
            wfs = await wfr.list_workflows("inst_a")
            assert all(w.workflow_id != "wf-no-registry" for w in wfs)
            await wfr.stop()
            await _reset_trigger_registry(trig)
        finally:
            await event_stream._reset_for_tests()

    async def test_workflow_without_route_to_agent_does_not_require_registry(
        self, tmp_path,
    ):
        """Mark_state-only workflows (and other non-route_to_agent
        workflows) don't need an agent registry — backward compat
        with WLP-era workflows holds."""
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        try:
            trig = TriggerRegistry()
            await trig.start(str(tmp_path))
            wfr = WorkflowRegistry()
            await wfr.start(str(tmp_path), trig)
            # No agent registry wired.
            wf = _build_workflow(
                workflow_id="wf-state-only",
                action_sequence=[ActionDescriptor(
                    action_type="mark_state",
                    parameters={"key": "x", "value": 1, "scope": "instance"},
                )],
            )
            # Succeeds without any registry binding.
            registered = await wfr._register_workflow_unbound(wf)
            assert registered.workflow_id == "wf-state-only"
            await wfr.stop()
            await _reset_trigger_registry(trig)
        finally:
            await event_stream._reset_for_tests()


# ===========================================================================
# Conversational routing thin consumer (AC #14)
# ===========================================================================


class TestConversationalRouting:
    async def test_dispatch_to_on_match(self, stack):
        await stack["agents"].register_agent(_agent_record(
            agent_id="reviewer", aliases=["code review"],
        ))
        decision = await route_phrase_to_agent(
            stack["agents"], "code review", "inst_a",
        )
        assert isinstance(decision, DispatchTo)
        assert decision.record.agent_id == "reviewer"

    async def test_ask_clarification_on_ambiguity(self, stack):
        # Two agents claim the same alias — set up via the
        # paused-then-active dance.
        await stack["agents"].register_agent(_agent_record(
            agent_id="agent-a", aliases=["reviewer"],
        ))
        await stack["agents"].update_status("agent-a", "inst_a", "paused")
        await stack["agents"].register_agent(_agent_record(
            agent_id="agent-b", aliases=["reviewer"],
        ))
        await stack["agents"].update_status("agent-a", "inst_a", "active")
        decision = await route_phrase_to_agent(
            stack["agents"], "reviewer", "inst_a",
        )
        assert isinstance(decision, AskClarification)
        assert {r.agent_id for r in decision.candidates} == {
            "agent-a", "agent-b",
        }

    async def test_unknown_lists_known_agents(self, stack):
        await stack["agents"].register_agent(_agent_record(
            agent_id="known-agent",
        ))
        decision = await route_phrase_to_agent(
            stack["agents"], "completely unrecognised phrase", "inst_a",
        )
        assert isinstance(decision, Unknown)
        assert any(
            r.agent_id == "known-agent" for r in decision.known_agents
        )


# ===========================================================================
# End-to-end: registered workflow → triggers → routes via registry
# ===========================================================================


class TestEndToEndApprovalFlow:
    """AC #13: a workflow with gate_ref through a registered agent
    still posts the approval_request block correctly; gate_nonce
    binding still works end-to-end."""

    async def test_gated_route_to_agent_via_registry(self, stack):
        await stack["agents"].register_agent(_agent_record(
            agent_id="owner", provider_config_ref="owner-inbox",
        ))
        wf = _build_workflow(
            workflow_id="wf-gated-routed",
            instance_local=True,
            approval_gates=[ApprovalGate(
                gate_name="g1",
                pause_reason="approve",
                approval_event_type="user.approval",
                approval_event_predicate={
                    "op": "actor_eq", "value": "owner",
                },
                timeout_seconds=5,
                bound_behavior_on_timeout="abort_workflow",
            )],
            action_sequence=[
                ActionDescriptor(
                    action_type="route_to_agent",
                    gate_ref="g1",
                    parameters={
                        "agent_id": "owner",
                        "payload": {
                            APPROVAL_REQUEST_KEY: {
                                "execution_id": "{workflow.execution_id}",
                                "gate_nonce": "{workflow.gate_nonce}",
                                "gate_name": "g1",
                                "pause_reason": "approve",
                                "response_event_type": "user.approval",
                                "response_predicate": {
                                    "op": "actor_eq", "value": "owner",
                                },
                            },
                        },
                    },
                ),
                ActionDescriptor(
                    action_type="mark_state",
                    parameters={"key": "post", "value": 1, "scope": "instance"},
                ),
            ],
        )
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        # Inbox receives the approval_request.
        await _wait_for(
            lambda: bool(stack["inboxes"].get("owner-inbox")
                         and stack["inboxes"]["owner-inbox"]._items.get(
                             ("inst_a", "owner"), [])),
        )
        items = stack["inboxes"]["owner-inbox"]._items[("inst_a", "owner")]
        block = items[0].payload[APPROVAL_REQUEST_KEY]
        # Echo the approval back.
        await event_stream.emit(
            "inst_a", "user.approval",
            {"execution_id": block["execution_id"],
             "gate_nonce": block["gate_nonce"]},
            member_id="owner",
        )
        await event_stream.flush_now()
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "post") in stack["state"],
        )
        assert ok


# ===========================================================================
# Helpers
# ===========================================================================


from dataclasses import dataclass


@dataclass
class _Ctx:
    instance_id: str = "inst_a"
    member_id: str = "mem_a"


def _build_workflow(*, workflow_id="wf", instance_local=False,
                   action_sequence=None, approval_gates=None,
                   trigger=None) -> Workflow:
    return Workflow(
        workflow_id=workflow_id,
        instance_id="inst_a",
        name=workflow_id,
        description="",
        owner="owner",
        version="1.0",
        bounds=Bounds(iteration_count=1, wall_time_seconds=30),
        verifier=Verifier(flavor="deterministic", check="ok"),
        action_sequence=action_sequence or [],
        approval_gates=approval_gates or [],
        trigger=trigger or TriggerDescriptor(
            event_type="cc.batch.report",
            predicate={"op": "exists", "path": "event_id"},
        ),
        instance_local=instance_local,
    )
