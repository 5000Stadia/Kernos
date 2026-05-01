"""WTC v1 C5b — STS register_workflow atomic trigger registration.

Pins:

* Step 1b: descriptor.triggers compiles BEFORE persist. Malformed
  triggers raise RegistrationValidationFailed; the workflow row
  never enters the DB and the approval is never consumed.
* Step 10: after step 9's persist, runtime.register() runs for
  each compiled trigger.
* End-to-end: a workflow registered with descriptor.triggers fires
  via the unified runtime when a matching event flushes.
* No-runtime path: register_workflow without a runtime kwarg works
  exactly as before — triggers are validated but not registered.
* Step 1b also fires during dry_run so the Drafter / Compiler
  surfaces shape errors before approval emission.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from kernos.kernel import event_stream
from kernos.kernel.agents.providers import (
    ProviderRegistry as DARProviderRegistry,
)
from kernos.kernel.agents.registry import AgentRegistry
from kernos.kernel.drafts.registry import DraftRegistry
from kernos.kernel.substrate_tools import (
    ContextBriefRegistry,
    ProviderRegistry,
    SubstrateTools,
    compute_descriptor_hash,
)
from kernos.kernel.substrate_tools.errors import (
    RegistrationValidationFailed,
)
from kernos.kernel.substrate_tools.registration.register import (
    _precompile_triggers,
    register_workflow as _register_workflow,
)
from kernos.kernel.triggers import (
    InternalEventAdapter,
    TriggerEvaluationRuntime,
)
from kernos.kernel.workflows.agent_inbox import InMemoryAgentInbox
from kernos.kernel.workflows.trigger_registry import (
    TriggerRegistry,
    _reset_for_tests as _reset_trigger_registry,
)
from kernos.kernel.workflows.workflow_registry import WorkflowRegistry


# ---------------------------------------------------------------------------
# _precompile_triggers — direct unit
# ---------------------------------------------------------------------------


def test_precompile_returns_empty_when_no_triggers():
    assert _precompile_triggers({"name": "x"}) == []


def test_precompile_returns_empty_when_workflow_id_missing():
    """Without workflow_id (or name), we have no anchor for the
    deterministic trigger_id derivation; skip rather than fabricate."""
    assert _precompile_triggers({"triggers": [{"event_type": "x"}]}) == []


def test_precompile_translates_minimal_descriptor():
    compiled = _precompile_triggers({
        "name": "wf_demo",
        "triggers": [{"event_type": "user.message"}],
    })
    assert len(compiled) == 1
    assert compiled[0].workflow_id == "wf_demo"
    assert compiled[0].predicate.temporal_relation.kind == "on"


def test_precompile_uses_workflow_id_when_present():
    compiled = _precompile_triggers({
        "workflow_id": "wf_explicit",
        "name": "ignored",
        "triggers": [{"event_type": "user.message"}],
    })
    assert compiled[0].workflow_id == "wf_explicit"


def test_precompile_raises_on_malformed_trigger():
    with pytest.raises(RegistrationValidationFailed):
        _precompile_triggers({
            "name": "wf",
            "triggers": [{
                "event_type": "x",
                "temporal_relation": {"kind": "bogus"},
            }],
        })


def test_precompile_raises_on_missing_event_type():
    with pytest.raises(RegistrationValidationFailed):
        _precompile_triggers({
            "name": "wf",
            "triggers": [{}],
        })


# ---------------------------------------------------------------------------
# Integration fixture — STS + WLP + runtime
# ---------------------------------------------------------------------------


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
    runtime = TriggerEvaluationRuntime()
    await runtime.start(
        data_dir=str(tmp_path),
        heartbeat_seconds=1,
    )
    sts = SubstrateTools(
        agent_registry=agents, workflow_registry=wfr,
        draft_registry=drafts,
        provider_registry=ProviderRegistry(),
        context_brief_registry=ContextBriefRegistry(),
        runtime=runtime,
    )
    yield {
        "agents": agents, "wfr": wfr, "drafts": drafts, "sts": sts,
        "crb": crb_emitter, "tmp_path": tmp_path, "runtime": runtime,
    }
    await runtime.stop()
    await drafts.stop()
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await agents.stop()
    await event_stream._reset_for_tests()


def _basic_descriptor(
    *, instance_id="inst_a", workflow_id=None,
    triggers=None,
) -> dict:
    return {
        "workflow_id": workflow_id or f"wf-{uuid.uuid4().hex[:8]}",
        "instance_id": instance_id,
        "name": "test-workflow",
        "owner": "founder",
        "version": "1",
        "bounds": {"iteration_count": 1},
        "verifier": {"flavor": "deterministic", "check": "x == y"},
        "triggers": triggers or [{"event_type": "user.message"}],
        "action_sequence": [
            {
                "action_type": "mark_state",
                "parameters": {"key": "k", "value": "v", "scope": "ledger"},
            },
        ],
    }


async def _propose_and_approve(
    crb_emitter, descriptor, *, instance_id="inst_a",
) -> str:
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
            "approved_by": "founder",
            "member_id": "mem_owner",
            "source_turn_id": "turn_x",
        },
        correlation_id=correlation_id,
    )
    await event_stream.flush_now()
    correlated = await event_stream.events_by_correlation(
        instance_id, correlation_id,
    )
    approval = next(
        e for e in correlated if e.event_type == "routine.approved"
    )
    return approval.event_id


# ---------------------------------------------------------------------------
# Step 10 — runtime.register() runs after persist
# ---------------------------------------------------------------------------


async def test_register_workflow_hydrates_runtime(stack):
    descriptor = _basic_descriptor(
        triggers=[{"event_type": "user.message"}],
    )
    approval_id = await _propose_and_approve(stack["crb"], descriptor)
    registered = await stack["sts"].register_workflow(
        instance_id="inst_a",
        descriptor=descriptor,
        approval_event_id=approval_id,
    )
    # The workflow row is persisted.
    assert registered.workflow_id == descriptor["workflow_id"]
    # The runtime now holds the trigger.
    active = await stack["runtime"].list_active()
    workflow_ids = {r["workflow_id"] for r in active}
    assert descriptor["workflow_id"] in workflow_ids


async def test_register_workflow_multiple_triggers_all_registered(stack):
    descriptor = _basic_descriptor(
        triggers=[
            {"event_type": "user.message"},
            {"event_type": "page.edit"},
        ],
    )
    approval_id = await _propose_and_approve(stack["crb"], descriptor)
    await stack["sts"].register_workflow(
        instance_id="inst_a",
        descriptor=descriptor,
        approval_event_id=approval_id,
    )
    active = await stack["runtime"].list_active()
    rows = [r for r in active
            if r["workflow_id"] == descriptor["workflow_id"]]
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Step 1b — pre-compile fires BEFORE persist; malformed triggers
# fail registration without consuming approval.
# ---------------------------------------------------------------------------


async def test_malformed_trigger_aborts_before_persist(stack):
    descriptor = _basic_descriptor(
        triggers=[{
            "event_type": "x",
            "temporal_relation": {"kind": "bogus"},
        }],
    )
    approval_id = await _propose_and_approve(stack["crb"], descriptor)
    with pytest.raises(RegistrationValidationFailed):
        await stack["sts"].register_workflow(
            instance_id="inst_a",
            descriptor=descriptor,
            approval_event_id=approval_id,
        )
    # Approval was NOT consumed — caller can re-attempt with a
    # fixed descriptor under the same approval_event_id (won't
    # error with ApprovalAlreadyConsumed).
    found = await stack["sts"].find_workflow_by_approval_event_id(
        instance_id="inst_a", approval_event_id=approval_id,
    )
    assert found is None
    # No trigger snuck into the runtime either.
    active = await stack["runtime"].list_active()
    workflow_ids = {r["workflow_id"] for r in active}
    assert descriptor["workflow_id"] not in workflow_ids


async def test_dry_run_surfaces_trigger_shape_error(stack):
    """Step 1b runs in the dry_run path too — the Drafter +
    Compiler see trigger shape errors before approval emission."""
    descriptor = _basic_descriptor(
        triggers=[{
            "event_type": "x",
            "temporal_relation": {"kind": "every"},  # missing cron
        }],
    )
    with pytest.raises(RegistrationValidationFailed):
        await stack["sts"].register_workflow(
            instance_id="inst_a",
            descriptor=descriptor,
            dry_run=True,
        )


# ---------------------------------------------------------------------------
# No-runtime path — backward compat
# ---------------------------------------------------------------------------


async def test_no_runtime_skips_step_10(stack, tmp_path):
    """When SubstrateTools is constructed without a runtime,
    register_workflow still validates triggers (step 1b) but
    skips step 10 — the workflow row persists, no runtime
    registration occurs."""
    sts_no_runtime = SubstrateTools(
        agent_registry=stack["agents"],
        workflow_registry=stack["wfr"],
        draft_registry=stack["drafts"],
        provider_registry=ProviderRegistry(),
        context_brief_registry=ContextBriefRegistry(),
        # runtime intentionally omitted.
    )
    descriptor = _basic_descriptor(
        triggers=[{"event_type": "user.message"}],
    )
    approval_id = await _propose_and_approve(stack["crb"], descriptor)
    registered = await sts_no_runtime.register_workflow(
        instance_id="inst_a",
        descriptor=descriptor,
        approval_event_id=approval_id,
    )
    assert registered.workflow_id == descriptor["workflow_id"]
    # The shared runtime fixture in `stack` was NOT used by the
    # alternate STS — the trigger isn't there.
    active = await stack["runtime"].list_active()
    workflow_ids = {r["workflow_id"] for r in active}
    assert descriptor["workflow_id"] not in workflow_ids


# ---------------------------------------------------------------------------
# End-to-end — registered workflow fires via the runtime
# ---------------------------------------------------------------------------


class _StubWLP:
    def __init__(self) -> None:
        self.executions: dict[str, str] = {}
        self.dispatch_calls: list[dict] = []

    async def execute_workflow(
        self, *, fire_id: str, workflow_id: str, instance_id: str,
        trigger_event_payload: Any = None, member_id: str = "",
        **kwargs: Any,
    ) -> str:
        self.dispatch_calls.append({
            "fire_id": fire_id, "workflow_id": workflow_id,
        })
        if fire_id in self.executions:
            return self.executions[fire_id]
        eid = f"exec_{uuid.uuid4().hex[:8]}"
        self.executions[fire_id] = eid
        return eid

    async def find_execution_by_fire_id(self, fire_id: str) -> str | None:
        return self.executions.get(fire_id)


async def test_registered_workflow_fires_on_matching_event(tmp_path):
    """Full path: STS register_workflow with descriptor.triggers
    AND a runtime wired to a stub WLP. Emit a matching event;
    the runtime claims + dispatches via the stub."""
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    try:
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
        wlp = _StubWLP()
        runtime = TriggerEvaluationRuntime()
        await runtime.start(
            data_dir=str(tmp_path),
            heartbeat_seconds=1,
            wlp_dispatch=wlp.execute_workflow,
            wlp_lookup_by_fire_id=wlp.find_execution_by_fire_id,
        )
        adapter = InternalEventAdapter(runtime)
        await adapter.start()
        sts = SubstrateTools(
            agent_registry=agents, workflow_registry=wfr,
            draft_registry=drafts,
            provider_registry=ProviderRegistry(),
            context_brief_registry=ContextBriefRegistry(),
            runtime=runtime,
        )

        descriptor = _basic_descriptor(
            triggers=[{"event_type": "user.message"}],
        )
        approval_id = await _propose_and_approve(crb_emitter, descriptor)
        registered = await sts.register_workflow(
            instance_id="inst_a",
            descriptor=descriptor,
            approval_event_id=approval_id,
        )

        # Emit a matching event; runtime should dispatch.
        await event_stream.emit(
            instance_id="inst_a",
            event_type="user.message",
            payload={"text": "hello"},
        )
        await event_stream.flush_now()
        assert len(wlp.dispatch_calls) == 1
        assert (
            wlp.dispatch_calls[0]["workflow_id"] == registered.workflow_id
        )
    finally:
        try:
            await adapter.stop()
        except Exception:
            pass
        try:
            await runtime.stop()
        except Exception:
            pass
        try:
            await drafts.stop()
            await wfr.stop()
            await _reset_trigger_registry(trig)
            await agents.stop()
        except Exception:
            pass
        await event_stream._reset_for_tests()
