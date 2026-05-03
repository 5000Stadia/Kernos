"""STS v2 — automated live sweep (mirrors STS-live-test.md runbook).

End-to-end exercises across all 23 spec scenarios. Many are already
pinned in the focused unit suites
(``test_substrate_tools_{query,hash,registration}.py``); this sweep
runs them in spec order against a single live stack so a single
``pytest`` invocation produces the runbook-equivalent verdict.

Local providers throughout: in-memory ``AgentInbox``, in-memory event
stream backed by the test ``instance.db``, CRB emitter registered via
``EmitterRegistry.register("crb")``. No external network or MCP
calls.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest

from kernos.kernel import event_stream
from kernos.kernel.agents.providers import ProviderRegistry as DARProviderRegistry
from kernos.kernel.agents.registry import AgentRecord, AgentRegistry
from kernos.kernel.drafts.registry import DraftRegistry
from kernos.kernel.substrate_tools import (
    ApprovalAlreadyConsumed,
    ApprovalAuthorityIncomplete,
    ApprovalAuthoritySpoofed,
    ApprovalBindingMissing,
    ApprovalDescriptorMismatch,
    ApprovalInstanceMismatch,
    ApprovalModificationTargetMismatch,
    ApprovalModificationTargetMissing,
    ApprovalProposalMismatch,
    ApprovalProvenanceUnverifiable,
    ContextBrief,
    ContextBriefRegistry,
    ContextRef,
    DryRunResult,
    InvalidCapabilityTagFormat,
    ProviderRecord,
    ProviderRegistry,
    RegistrationValidationFailed,
    SubstrateTools,
    compute_descriptor_hash,
    validate_capability_tag,
)
from kernos.kernel.event_stream import (
    EmitterAlreadyRegistered,
    UNREGISTERED_SOURCE_MODULE,
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
    crb = event_stream.emitter_registry().register("crb")
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
    sts_pr = ProviderRegistry()
    cbr = ContextBriefRegistry()
    sts = SubstrateTools(
        agent_registry=agents,
        workflow_registry=wfr,
        draft_registry=drafts,
        provider_registry=sts_pr,
        context_brief_registry=cbr,
    )
    yield {
        "agents": agents, "wfr": wfr, "drafts": drafts, "sts": sts,
        "crb": crb, "sts_pr": sts_pr, "cbr": cbr,
    }
    await drafts.stop()
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await agents.stop()
    await event_stream._reset_for_tests()


def _descriptor(*, instance_id="inst_a", workflow_id=None, **overrides) -> dict:
    desc = {
        "workflow_id": workflow_id or f"wf-{uuid.uuid4().hex[:8]}",
        "instance_id": instance_id,
        "name": "live-sweep",
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
    desc.update(overrides)
    return desc


async def _full_approval_chain(crb_emitter, descriptor, *, instance_id="inst_a") -> str:
    """Emit proposed + approved for ``descriptor`` and return the
    approval event_id."""
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
# Scenario 1: query happy path
# ===========================================================================


async def test_scenario_01_query_happy_path(stack):
    sts = stack["sts"]
    # Bare minimum: each surface is callable and returns the right type.
    providers = await sts.list_known_providers(instance_id="inst_a")
    assert isinstance(providers, list)
    agents = await sts.list_agents(instance_id="inst_a")
    assert isinstance(agents, list)
    workflows = await sts.list_workflows(instance_id="inst_a")
    assert isinstance(workflows, list)
    drafts = await sts.list_drafts(instance_id="inst_a")
    assert isinstance(drafts, list)
    brief = await sts.query_context_brief(
        instance_id="inst_a", ref=ContextRef(type="space", id="spc_x"),
    )
    assert brief is None  # No resolver registered → None


# ===========================================================================
# Scenario 2: cross-instance isolation
# ===========================================================================


async def test_scenario_02_cross_instance_isolation(stack):
    sts = stack["sts"]
    await stack["agents"].register_agent(AgentRecord(
        agent_id="a-A", instance_id="inst_a",
        provider_key="inmemory", provider_config_ref="default",
    ))
    await stack["agents"].register_agent(AgentRecord(
        agent_id="a-B", instance_id="inst_b",
        provider_key="inmemory", provider_config_ref="default",
    ))
    a_only = await sts.list_agents(instance_id="inst_a")
    b_only = await sts.list_agents(instance_id="inst_b")
    assert {r.agent_id for r in a_only} == {"a-A"}
    assert {r.agent_id for r in b_only} == {"a-B"}


# ===========================================================================
# Scenario 3: provider type registration
# ===========================================================================


async def test_scenario_03_provider_type_registration(stack):
    pr = stack["sts_pr"]

    def list_canvases(instance_id: str) -> list[ProviderRecord]:
        return [
            ProviderRecord(
                provider_id="canvas-1", provider_type="canvas",
                capability_tags=["canvas.read"],
            ),
        ]

    pr.register_provider_type("canvas", list_canvases)
    records = await pr.list_all(instance_id="inst_a")
    assert any(r.provider_type == "canvas" for r in records)


# ===========================================================================
# Scenario 4: capability tag format enforcement
# ===========================================================================


async def test_scenario_04_capability_tag_format_enforcement():
    validate_capability_tag("email.send")  # ok
    for bad in ("Email.send", "email", "email.send\n", "email-send"):
        with pytest.raises(InvalidCapabilityTagFormat):
            validate_capability_tag(bad)


# ===========================================================================
# Scenario 5: context brief dispatch
# ===========================================================================


async def test_scenario_05_context_brief_dispatch(stack):
    cbr = stack["cbr"]

    def space_resolver(instance_id: str, ref_id: str) -> ContextBrief | None:
        return ContextBrief(
            ref=ContextRef(type="space", id=ref_id),
            summary=f"space {ref_id}",
        )

    cbr.register_resolver("space", space_resolver)
    brief = await stack["sts"].query_context_brief(
        instance_id="inst_a", ref=ContextRef(type="space", id="spc_x"),
    )
    assert brief is not None and brief.summary == "space spc_x"
    miss = await stack["sts"].query_context_brief(
        instance_id="inst_a", ref=ContextRef(type="canvas", id="cvs-1"),
    )
    assert miss is None


# ===========================================================================
# Scenarios 6-10: hash determinism and field handling
# ===========================================================================


async def test_scenario_06_hash_determinism():
    a = compute_descriptor_hash(_descriptor())
    b = compute_descriptor_hash(_descriptor())
    assert a == b


async def test_scenario_07_hash_under_key_reorder():
    desc_a = {"foo": 1, "bar": {"a": 1, "b": 2}}
    desc_b = {"bar": {"b": 2, "a": 1}, "foo": 1}
    assert compute_descriptor_hash(desc_a) == compute_descriptor_hash(desc_b)


async def test_scenario_08_hash_includes_display_name_and_aliases():
    base = _descriptor(display_name="A", aliases=["one"])
    different_name = _descriptor(display_name="B", aliases=["one"])
    different_aliases = _descriptor(display_name="A", aliases=["two"])
    assert compute_descriptor_hash(base) != compute_descriptor_hash(different_name)
    assert compute_descriptor_hash(base) != compute_descriptor_hash(different_aliases)


async def test_scenario_09_hash_includes_prev_version_id():
    a = compute_descriptor_hash(_descriptor(prev_version_id="wf-A"))
    b = compute_descriptor_hash(_descriptor(prev_version_id="wf-B"))
    assert a != b


async def test_scenario_10_hash_excludes_volatile_fields():
    base = _descriptor()
    for volatile in (
        "id", "workflow_id", "created_at", "updated_at",
        "registered_at", "version",
    ):
        mutated = dict(base)
        mutated[volatile] = "irrelevant"
        # Note: workflow_id and version are present in base; mutating
        # them MUST NOT change the hash.
        assert compute_descriptor_hash(mutated) == compute_descriptor_hash(base)


# ===========================================================================
# Scenarios 11-12: dry-run produces results without persistence
# ===========================================================================


async def test_scenario_11_dry_run_valid_clean_descriptor(stack):
    sts = stack["sts"]
    before = await stack["wfr"].list_workflows("inst_a")
    result = await sts.register_workflow(
        instance_id="inst_a", descriptor=_descriptor(), dry_run=True,
    )
    assert isinstance(result, DryRunResult)
    assert result.valid is True
    after = await stack["wfr"].list_workflows("inst_a")
    assert before == after  # No persistence.


async def test_scenario_12_dry_run_invalid_surfaces_issues(stack):
    sts = stack["sts"]
    bad = _descriptor()
    del bad["verifier"]
    result = await sts.register_workflow(
        instance_id="inst_a", descriptor=bad, dry_run=True,
    )
    assert result.valid is False
    assert any(i.severity == "error" for i in result.issues)


# ===========================================================================
# Scenario 13: real registration without approval raises
# ===========================================================================


async def test_scenario_13_registration_requires_approval(stack):
    with pytest.raises(ApprovalBindingMissing):
        await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=_descriptor(), dry_run=False,
        )


# ===========================================================================
# Scenario 14: spoofed approval (envelope-source-checked) rejected
# ===========================================================================


async def test_scenario_14_envelope_spoof_rejected(stack):
    """The classic attack: payload claims source_module='crb' but the
    envelope was set by a non-CRB emitter."""
    spoofer = event_stream.emitter_registry().register("evil")
    descriptor = _descriptor()
    desc_hash = compute_descriptor_hash(descriptor)
    correlation_id = "corr-spoof"
    await spoofer.emit(
        "inst_a", "routine.approved",
        {
            "source_module": "crb",  # spoof
            "correlation_id": correlation_id,
            "descriptor_hash": desc_hash,
            "instance_id": "inst_a",
            "approved_by": "attacker",
            "member_id": "mem_owner",
            "source_turn_id": "turn_x",
        },
        correlation_id=correlation_id,
    )
    await event_stream.flush_now()
    correlated = await event_stream.events_by_correlation("inst_a", correlation_id)
    approval = next(e for e in correlated if e.event_type == "routine.approved")
    assert approval.envelope.source_module == "evil"  # NOT crb
    with pytest.raises(ApprovalAuthoritySpoofed):
        await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor,
            approval_event_id=approval.event_id,
        )


# ===========================================================================
# Scenario 15: incomplete approval rejected
# ===========================================================================


async def test_scenario_15_incomplete_approval_rejected(stack):
    descriptor = _descriptor()
    desc_hash = compute_descriptor_hash(descriptor)
    correlation_id = "corr-incomplete"
    await stack["crb"].emit(
        "inst_a", "routine.proposed",
        {
            "correlation_id": correlation_id,
            "descriptor_hash": desc_hash,
            "instance_id": "inst_a",
            "proposed_by": "drafter",
            "member_id": "mem_owner",
            "source_thread_id": "thr_x",
        },
        correlation_id=correlation_id,
    )
    await stack["crb"].emit(
        "inst_a", "routine.approved",
        {
            # Missing approved_by.
            "correlation_id": correlation_id,
            "descriptor_hash": desc_hash,
            "instance_id": "inst_a",
            "member_id": "mem_owner",
            "source_turn_id": "turn_x",
        },
        correlation_id=correlation_id,
    )
    await event_stream.flush_now()
    correlated = await event_stream.events_by_correlation("inst_a", correlation_id)
    approval = next(e for e in correlated if e.event_type == "routine.approved")
    with pytest.raises(ApprovalAuthorityIncomplete):
        await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor,
            approval_event_id=approval.event_id,
        )


# ===========================================================================
# Scenario 16: proposal-anchor failure modes
# ===========================================================================


async def test_scenario_16_no_proposal_raises(stack):
    descriptor = _descriptor()
    desc_hash = compute_descriptor_hash(descriptor)
    await stack["crb"].emit(
        "inst_a", "routine.approved",
        {
            "correlation_id": "no-proposal",
            "descriptor_hash": desc_hash,
            "instance_id": "inst_a",
            "approved_by": "owner",
            "member_id": "mem_owner",
            "source_turn_id": "turn_x",
        },
        correlation_id="no-proposal",
    )
    await event_stream.flush_now()
    events = await event_stream.events_by_correlation("inst_a", "no-proposal")
    approval_id = events[0].event_id
    with pytest.raises(ApprovalProvenanceUnverifiable):
        await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor,
            approval_event_id=approval_id,
        )


# ===========================================================================
# Scenario 17: registration-time revalidation (P7) — NOT cached
# ===========================================================================


async def test_scenario_17_registration_revalidates(stack):
    await stack["agents"].register_agent(AgentRecord(
        agent_id="spec-agent", instance_id="inst_a",
        provider_key="inmemory", provider_config_ref="default",
        status="active",
    ))
    descriptor = _descriptor(
        instance_local=True,
        action_sequence=[
            {
                "action_type": "route_to_agent",
                "parameters": {"agent_id": "spec-agent"},
                "per_action_expectation": "agent picks it up",
            },
        ],
    )
    dr = await stack["sts"].register_workflow(
        instance_id="inst_a", descriptor=descriptor, dry_run=True,
    )
    assert dr.valid is True
    approval_id = await _full_approval_chain(stack["crb"], descriptor)
    await stack["agents"].update_status(
        instance_id="inst_a", agent_id="spec-agent", new_status="paused",
    )
    with pytest.raises(RegistrationValidationFailed):
        await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor,
            approval_event_id=approval_id,
        )


# ===========================================================================
# Scenario 18: hash mismatch at registration
# ===========================================================================


async def test_scenario_18_hash_mismatch(stack):
    descriptor_a = _descriptor(workflow_id="wf-shared", display_name="Original")
    approval_id = await _full_approval_chain(stack["crb"], descriptor_a)
    descriptor_b = _descriptor(workflow_id="wf-shared", display_name="Mutated")
    with pytest.raises(ApprovalDescriptorMismatch):
        await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor_b,
            approval_event_id=approval_id,
        )


# ===========================================================================
# Scenario 19: single-use consumption AND terminal failure
# ===========================================================================


async def test_scenario_19_terminal_already_consumed(stack):
    descriptor = _descriptor()
    approval_id = await _full_approval_chain(stack["crb"], descriptor)
    await stack["sts"].register_workflow(
        instance_id="inst_a", descriptor=descriptor,
        approval_event_id=approval_id,
    )
    with pytest.raises(ApprovalAlreadyConsumed):
        await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor,
            approval_event_id=approval_id,
        )
    # Class docstring states TERMINAL.
    assert "terminal" in (ApprovalAlreadyConsumed.__doc__ or "").lower()
    assert "must not retry" in (ApprovalAlreadyConsumed.__doc__ or "").lower()


# ===========================================================================
# Scenario 20: retry-after-failure preserves approval
# ===========================================================================


async def test_scenario_20_retry_preserves_approval(stack):
    await stack["agents"].register_agent(AgentRecord(
        agent_id="spec-agent", instance_id="inst_a",
        provider_key="inmemory", provider_config_ref="default",
        status="active",
    ))
    descriptor = _descriptor(
        instance_local=True,
        action_sequence=[
            {
                "action_type": "route_to_agent",
                "parameters": {"agent_id": "spec-agent"},
                "per_action_expectation": "agent picks it up",
            },
        ],
    )
    approval_id = await _full_approval_chain(stack["crb"], descriptor)
    await stack["agents"].update_status(
        instance_id="inst_a", agent_id="spec-agent", new_status="paused",
    )
    with pytest.raises(RegistrationValidationFailed):
        await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor,
            approval_event_id=approval_id,
        )
    await stack["agents"].update_status(
        instance_id="inst_a", agent_id="spec-agent", new_status="active",
    )
    wf = await stack["sts"].register_workflow(
        instance_id="inst_a", descriptor=descriptor,
        approval_event_id=approval_id,
    )
    assert wf.workflow_id == descriptor["workflow_id"]


# ===========================================================================
# Scenario 21: modification target binding
# ===========================================================================


async def test_scenario_21_modification_target_binding(stack):
    descriptor_a = _descriptor(workflow_id="wf-a")
    approval_a = await _full_approval_chain(stack["crb"], descriptor_a)
    await stack["sts"].register_workflow(
        instance_id="inst_a", descriptor=descriptor_a,
        approval_event_id=approval_a,
    )
    # Forge a modification approval whose prev_workflow_id mismatches
    # descriptor.prev_version_id.
    desc_mod = _descriptor(workflow_id="wf-c", prev_version_id="wf-a")
    desc_hash = compute_descriptor_hash(desc_mod)
    correlation_id = "corr-mod-attack"
    await stack["crb"].emit(
        "inst_a", "routine.proposed",
        {
            "correlation_id": correlation_id,
            "descriptor_hash": desc_hash,
            "instance_id": "inst_a",
            "proposed_by": "drafter",
            "member_id": "mem_owner",
            "source_thread_id": "thr_x",
        },
        correlation_id=correlation_id,
    )
    await stack["crb"].emit(
        "inst_a", "routine.modification.approved",
        {
            "correlation_id": correlation_id,
            "descriptor_hash": desc_hash,
            "instance_id": "inst_a",
            "approved_by": "owner",
            "member_id": "mem_owner",
            "source_turn_id": "turn_x",
            "prev_workflow_id": "wf-different",  # mismatch
            "change_summary": "modify",
        },
        correlation_id=correlation_id,
    )
    await event_stream.flush_now()
    correlated = await event_stream.events_by_correlation("inst_a", correlation_id)
    approval = next(e for e in correlated if e.event_type == "routine.modification.approved")
    with pytest.raises(ApprovalModificationTargetMismatch):
        await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=desc_mod,
            approval_event_id=approval.event_id,
        )


# ===========================================================================
# Scenario 22: event envelope substrate-set
# ===========================================================================


async def test_scenario_22_envelope_substrate_set(stack):
    # Two modules cannot both register as source_module="crb".
    with pytest.raises(EmitterAlreadyRegistered):
        event_stream.emitter_registry().register("crb")
    # Module registered as "foo" cannot stamp envelope as "crb" via payload.
    foo = event_stream.emitter_registry().register("foo")
    await foo.emit(
        "inst_a", "tool.called", {"source_module": "crb"},
    )
    await event_stream.flush_now()
    events = await event_stream.events_in_window(
        "inst_a",
        since=dt.datetime.fromtimestamp(0, tz=dt.timezone.utc),
        until=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1),
    )
    foo_events = [e for e in events if e.event_type == "tool.called"]
    assert len(foo_events) == 1
    assert foo_events[0].envelope.source_module == "foo"


# ===========================================================================
# Scenario 23: bypass grep test
# ===========================================================================


async def test_scenario_23_bypass_grep_test_present():
    """Sanity check: the bypass-grep test exists and is callable.
    The test itself runs in test_no_direct_register_unbound.py."""
    from tests import test_no_direct_register_unbound

    assert hasattr(
        test_no_direct_register_unbound, "TestNoDirectUnboundRegistration"
    )
    assert hasattr(
        test_no_direct_register_unbound, "TestNoDirectEnvelopeBypass"
    )


# ===========================================================================
# Scenario 24: happy path round trip
# ===========================================================================


async def test_scenario_24_propose_approve_register_happy_path(stack):
    descriptor = _descriptor(display_name="Live sweep happy path")
    approval_id = await _full_approval_chain(stack["crb"], descriptor)
    wf = await stack["sts"].register_workflow(
        instance_id="inst_a", descriptor=descriptor,
        approval_event_id=approval_id,
    )
    assert wf.workflow_id == descriptor["workflow_id"]
    # Persist confirmed.
    listed = await stack["wfr"].list_workflows("inst_a")
    assert any(w.workflow_id == wf.workflow_id for w in listed)
    # approval_event_id written to row.
    async with stack["wfr"]._db.execute(
        "SELECT approval_event_id FROM workflows WHERE workflow_id = ?",
        (wf.workflow_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == approval_id
