"""STS amendment for CRB main v1 (CRB C0).

Adds ``find_workflow_by_approval_event_id`` to STS for CRB's crash-
recovery sweep. Composes against the existing partial UNIQUE index
``idx_workflows_approval_unique ON (instance_id, approval_event_id)
WHERE approval_event_id IS NOT NULL`` shipped in STS C2.

Pins:

* Found: a registered workflow with a given approval_event_id is
  returned in full (rehydrated via the existing _workflow_from_row).
* Not-found: returns None (no exception).
* Cross-instance isolation: instance B never sees instance A's row.
"""
from __future__ import annotations

import uuid

import pytest

from kernos.kernel import event_stream
from kernos.kernel.agents.providers import ProviderRegistry as DARProviderRegistry
from kernos.kernel.agents.registry import AgentRegistry
from kernos.kernel.drafts.registry import DraftRegistry
from kernos.kernel.substrate_tools import (
    ContextBriefRegistry,
    ProviderRegistry,
    SubstrateTools,
    compute_descriptor_hash,
)
from kernos.kernel.workflows.agent_inbox import InMemoryAgentInbox
from kernos.kernel.workflows.trigger_registry import (
    TriggerRegistry,
    _reset_for_tests as _reset_trigger_registry,
)
from kernos.kernel.workflows.workflow_registry import WorkflowRegistry


@pytest.fixture
async def stack(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    crb_emitter = event_stream.emitter_registry().register("crb")
    dar_pr = DARProviderRegistry()
    dar_pr.register("inmemory", lambda ref: InMemoryAgentInbox())
    agents = AgentRegistry(provider_registry=dar_pr)
    await agents.start(str(tmp_path))
    trig = TriggerRegistry()
    await trig.start(str(tmp_path))
    wfr = WorkflowRegistry()
    await wfr.start(str(tmp_path), trig)
    wfr.wire_agent_registry(agents)
    drafts = DraftRegistry()
    await drafts.start(str(tmp_path))
    sts = SubstrateTools(
        agent_registry=agents, workflow_registry=wfr, draft_registry=drafts,
        provider_registry=ProviderRegistry(),
        context_brief_registry=ContextBriefRegistry(),
    )
    yield {
        "agents": agents, "wfr": wfr, "drafts": drafts, "sts": sts,
        "crb": crb_emitter, "tmp_path": tmp_path,
    }
    await drafts.stop()
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await agents.stop()
    await event_stream._reset_for_tests()


def _basic_descriptor(*, instance_id="inst_a", workflow_id=None) -> dict:
    return {
        "workflow_id": workflow_id or f"wf-{uuid.uuid4().hex[:8]}",
        "instance_id": instance_id,
        "name": "test-workflow",
        "owner": "owner",
        "version": "1",
        "bounds": {"iteration_count": 1},
        "verifier": {"flavor": "deterministic", "check": "x == y"},
        "action_sequence": [
            {
                "action_type": "mark_state",
                "parameters": {"key": "k", "value": "v", "scope": "ledger"},
            },
        ],
    }


async def _propose_and_approve(crb_emitter, descriptor, *, instance_id="inst_a") -> str:
    """Helper: emit proposed + approved, return approval event_id."""
    desc_hash = compute_descriptor_hash(descriptor)
    correlation_id = f"corr-{uuid.uuid4().hex[:8]}"
    await crb_emitter.emit(
        instance_id, "routine.proposed",
        {
            "correlation_id": correlation_id,
            "descriptor_hash": desc_hash,
            "instance_id": instance_id,
            "proposed_by": "drafter",
            "member_id": "mem_owner",
            "source_thread_id": "thr_x",
        },
        correlation_id=correlation_id,
    )
    await crb_emitter.emit(
        instance_id, "routine.approved",
        {
            "correlation_id": correlation_id,
            "descriptor_hash": desc_hash,
            "instance_id": instance_id,
            "approved_by": "owner",
            "member_id": "mem_owner",
            "source_turn_id": "turn_x",
        },
        correlation_id=correlation_id,
    )
    await event_stream.flush_now()
    correlated = await event_stream.events_by_correlation(instance_id, correlation_id)
    approval = next(e for e in correlated if e.event_type == "routine.approved")
    return approval.event_id


# ===========================================================================
# Found
# ===========================================================================


class TestFound:
    async def test_returns_workflow_after_registration(self, stack):
        descriptor = _basic_descriptor()
        approval_id = await _propose_and_approve(stack["crb"], descriptor)
        registered = await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor,
            approval_event_id=approval_id,
        )
        # Now look up by approval_event_id.
        found = await stack["sts"].find_workflow_by_approval_event_id(
            instance_id="inst_a", approval_event_id=approval_id,
        )
        assert found is not None
        assert found.workflow_id == registered.workflow_id
        assert found.instance_id == "inst_a"


# ===========================================================================
# Not found
# ===========================================================================


class TestNotFound:
    async def test_unconsumed_approval_returns_none(self, stack):
        # Approval event exists but not yet registered.
        descriptor = _basic_descriptor()
        approval_id = await _propose_and_approve(stack["crb"], descriptor)
        # No register_workflow call; lookup must return None.
        result = await stack["sts"].find_workflow_by_approval_event_id(
            instance_id="inst_a", approval_event_id=approval_id,
        )
        assert result is None

    async def test_nonexistent_event_id_returns_none(self, stack):
        result = await stack["sts"].find_workflow_by_approval_event_id(
            instance_id="inst_a", approval_event_id="never-emitted",
        )
        assert result is None

    async def test_empty_args_rejected(self, stack):
        with pytest.raises(ValueError):
            await stack["wfr"].find_workflow_by_approval_event_id(
                instance_id="", approval_event_id="x",
            )
        with pytest.raises(ValueError):
            await stack["wfr"].find_workflow_by_approval_event_id(
                instance_id="inst_a", approval_event_id="",
            )


# ===========================================================================
# Cross-instance isolation
# ===========================================================================


class TestCrossInstanceIsolation:
    async def test_instance_a_lookup_returns_none_for_b_workflow(self, stack):
        # Register workflow in inst_a.
        descriptor = _basic_descriptor(instance_id="inst_a")
        approval_id = await _propose_and_approve(
            stack["crb"], descriptor, instance_id="inst_a",
        )
        await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor,
            approval_event_id=approval_id,
        )
        # Lookup by approval_event_id but with inst_b scope must miss.
        result = await stack["sts"].find_workflow_by_approval_event_id(
            instance_id="inst_b", approval_event_id=approval_id,
        )
        assert result is None
