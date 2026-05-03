"""Tests for the AgentInbox approval_request payload contract +
``workflow.execution_paused_at_gate`` event payload shape.

WLP-GATE-SCOPING C2. Pins:

  - the approval_request block carries all six documented fields
  - is_approval_request_payload classifier accepts complete blocks
    and rejects incomplete ones
  - the engine emits ``workflow.execution_paused_at_gate`` with the
    full set of fields from AC #4
  - end-to-end: a route_to_agent action authored with
    {workflow.execution_id} / {workflow.gate_nonce} placeholders
    produces an inbox payload whose approval_request block matches
    both the engine-persisted nonce AND the emitted pause-event
    payload
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from kernos.kernel import event_stream
from kernos.kernel.workflows.action_library import (
    ActionLibrary,
    MarkStateAction,
    NotifyUserAction,
    RouteToAgentAction,
)
from kernos.kernel.workflows.agent_inbox import (
    APPROVAL_REQUEST_FIELDS,
    APPROVAL_REQUEST_KEY,
    InMemoryAgentInbox,
    is_approval_request_payload,
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
    WorkflowRegistry,
)


# ===========================================================================
# Classifier unit tests
# ===========================================================================


class TestApprovalRequestClassifier:
    def test_complete_block_accepted(self):
        block = {
            "execution_id": "e-1", "gate_nonce": "n-1",
            "gate_name": "g1", "pause_reason": "confirm",
            "response_event_type": "user.approval",
            "response_predicate": {"op": "actor_eq", "value": "owner"},
        }
        assert is_approval_request_payload({APPROVAL_REQUEST_KEY: block})

    def test_missing_field_rejected(self):
        block = {
            "execution_id": "e-1", "gate_nonce": "n-1",
            "gate_name": "g1", "pause_reason": "confirm",
            "response_event_type": "user.approval",
            # response_predicate missing
        }
        assert not is_approval_request_payload({APPROVAL_REQUEST_KEY: block})

    def test_no_block_rejected(self):
        assert not is_approval_request_payload({"other": 1})

    def test_non_dict_rejected(self):
        assert not is_approval_request_payload([])
        assert not is_approval_request_payload("string")

    def test_block_must_be_dict(self):
        # A scalar at the approval_request key is invalid.
        assert not is_approval_request_payload(
            {APPROVAL_REQUEST_KEY: "not-a-block"},
        )


# ===========================================================================
# End-to-end: route_to_agent + descriptor placeholders + engine binding
# ===========================================================================


@pytest.fixture
async def stack(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    trig = TriggerRegistry()
    await trig.start(str(tmp_path))
    # DAR C4: workflows that use route_to_agent require the agent
    # registry to be wired into WorkflowRegistry. This test uses
    # the legacy RouteToAgentAction(inbox=...) dispatch path, so
    # we register a minimal agent record just to satisfy the
    # registration-time validation; the action library bypasses
    # the registry at dispatch via the legacy inbox= constructor
    # parameter.
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
    state: dict = {}

    async def state_set(*, key, value, scope, instance_id):
        state[(scope, instance_id, key)] = value

    async def state_get(*, key, scope, instance_id):
        return state.get((scope, instance_id, key))

    async def deliver(**kw):
        return {"persisted_id": "msg-1"}

    inbox = InMemoryAgentInbox()
    lib = ActionLibrary()
    lib.register(MarkStateAction(state_store_set=state_set, state_store_get=state_get))
    lib.register(NotifyUserAction(deliver_fn=deliver))
    lib.register(RouteToAgentAction(inbox=inbox))
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


def _approval_workflow():
    """A workflow whose gated action posts an approval_request to the
    AgentInbox via template interpolation."""
    return Workflow(
        workflow_id="wf-approval",
        instance_id="inst_a",
        name="approval e2e",
        description="",
        owner="owner",
        version="1.0",
        bounds=Bounds(iteration_count=1, wall_time_seconds=30),
        verifier=Verifier(flavor="deterministic", check="ok"),
        action_sequence=[
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
                                "op": "actor_eq",
                                "value": "owner",
                            },
                        },
                    },
                },
            ),
            ActionDescriptor(
                action_type="mark_state",
                parameters={"key": "post", "value": 1, "scope": "instance"},
                continuation_rules=ContinuationRules(on_failure="abort"),
            ),
        ],
        approval_gates=[ApprovalGate(
            gate_name="g1",
            pause_reason="approve please",
            approval_event_type="user.approval",
            approval_event_predicate={"op": "actor_eq", "value": "owner"},
            timeout_seconds=5,
            bound_behavior_on_timeout="abort_workflow",
        )],
        trigger=TriggerDescriptor(
            event_type="cc.batch.report",
            predicate={"op": "exists", "path": "event_id"},
        ),
    )


class TestApprovalRequestEndToEnd:
    async def test_inbox_payload_carries_complete_block(self, stack):
        await stack["wfr"]._register_workflow_unbound(_approval_workflow())
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        await _wait_for(
            lambda: bool(stack["inbox"]._items.get(("inst_a", "owner"), [])),
        )
        items = stack["inbox"]._items[("inst_a", "owner")]
        assert len(items) == 1
        payload = items[0].payload
        # Block is present and complete (all six fields).
        assert is_approval_request_payload(payload)
        block = payload[APPROVAL_REQUEST_KEY]
        for field in APPROVAL_REQUEST_FIELDS:
            assert field in block, f"missing field {field}"
        # Nonce + execution_id resolved (no remaining placeholders).
        assert "{workflow." not in block["execution_id"]
        assert "{workflow." not in block["gate_nonce"]
        # Match the engine's persisted state.
        execs = await stack["engine"].list_executions("inst_a")
        gated = next(e for e in execs if e.gate_nonce)
        assert block["execution_id"] == gated.execution_id
        assert block["gate_nonce"] == gated.gate_nonce


# ===========================================================================
# workflow.execution_paused_at_gate event payload shape (AC #4)
# ===========================================================================


async def _read_all_events(instance_id: str):
    return await event_stream.events_in_window(
        instance_id,
        datetime.fromisoformat("2020-01-01T00:00:00+00:00"),
        datetime.fromisoformat("2099-01-01T00:00:00+00:00"),
    )


class TestPausedAtGateEvent:
    async def test_event_payload_has_full_field_set(self, stack):
        await stack["wfr"]._register_workflow_unbound(_approval_workflow())
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()

        async def _has_gate_event():
            events = await _read_all_events("inst_a")
            return any(
                e.event_type == "workflow.execution_paused_at_gate"
                for e in events
            )
        # Poll the read API directly.
        for _ in range(100):
            if await _has_gate_event():
                break
            await asyncio.sleep(0.02)
        events = await _read_all_events("inst_a")
        gate_events = [
            e for e in events
            if e.event_type == "workflow.execution_paused_at_gate"
        ]
        assert len(gate_events) == 1
        payload = gate_events[0].payload
        # AC #4: full field set.
        assert payload["execution_id"]
        assert payload["gate_name"] == "g1"
        assert payload["gate_nonce"]
        assert payload["pause_reason"] == "approve please"
        assert payload["approval_event_type"] == "user.approval"
        assert payload["approval_event_predicate"] == {
            "op": "actor_eq", "value": "owner",
        }
        assert payload["timeout_seconds"] == 5
        assert payload["bound_behavior_on_timeout"] == "abort_workflow"
        execs = await stack["engine"].list_executions("inst_a")
        gated = next(e for e in execs if e.gate_nonce)
        assert payload["gate_nonce"] == gated.gate_nonce

    async def test_old_paused_event_not_emitted(self, stack):
        """C2 confirmation: the engine emits
        ``workflow.execution_paused_at_gate``, NOT the old
        ``workflow.execution_paused``. There are no other pause causes
        in shipped WLP code — clean rename, not coexistence."""
        await stack["wfr"]._register_workflow_unbound(_approval_workflow())
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        for _ in range(100):
            events = await _read_all_events("inst_a")
            if any(
                e.event_type == "workflow.execution_paused_at_gate"
                for e in events
            ):
                break
            await asyncio.sleep(0.02)
        events = await _read_all_events("inst_a")
        assert not any(
            e.event_type == "workflow.execution_paused" for e in events
        )
