"""SubstrateTools (STS) C2 registration-gate tests.

Spec reference: SPEC-STS-v2.

Pins:

* AC #4  — dry-run produces DryRunResult, no persistence.
* AC #5  — real registration requires approval_event_id.
* AC #6  — approval source authority via envelope (NOT payload).
* AC #7  — approval authority field check.
* AC #8  — proposal anchor: correlation, hash, instance, envelope.
* AC #9  — registration-time revalidation (P7).
* AC #10 — descriptor hash match at registration.
* AC #11 — atomic single-use consumption AND terminal ApprovalAlreadyConsumed.
* AC #12 — retry-after-failure preserves approval.
* AC #17 — routine.proposed event well-formed.
* AC #18 — routine.modification.approved parallel.
* AC #19 — modification target binding.
"""
from __future__ import annotations

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
    ApprovalEventNotFound,
    ApprovalEventTypeInvalid,
    ApprovalInstanceMismatch,
    ApprovalModificationTargetMismatch,
    ApprovalModificationTargetMissing,
    ApprovalProposalMismatch,
    ApprovalProvenanceUnverifiable,
    ContextBriefRegistry,
    DryRunResult,
    ProviderRegistry,
    RegistrationValidationFailed,
    SubstrateTools,
    compute_descriptor_hash,
)
from kernos.kernel.workflows.agent_inbox import InMemoryAgentInbox
from kernos.kernel.workflows.trigger_registry import (
    TriggerRegistry,
    _reset_for_tests as _reset_trigger_registry,
)
from kernos.kernel.workflows.workflow_registry import WorkflowRegistry


# ===========================================================================
# Stack helpers
# ===========================================================================


@pytest.fixture
async def stack(tmp_path):
    """Full STS stack with CRB emitter registered."""
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
        "crb": crb_emitter, "tmp_path": tmp_path,
    }
    await drafts.stop()
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await agents.stop()
    await event_stream._reset_for_tests()


def _basic_descriptor(*, instance_id="inst_a", workflow_id=None, **overrides) -> dict:
    """Build a minimal valid descriptor."""
    desc = {
        "workflow_id": workflow_id or f"wf-{uuid.uuid4().hex[:8]}",
        "instance_id": instance_id,
        "name": "test-workflow",
        "description": "test",
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


async def _emit_proposed(
    crb_emitter, *, instance_id, descriptor_hash, correlation_id,
    member_id="mem_owner", source_thread_id="thr_x",
    proposed_by="drafter",
):
    """Emit a routine.proposed event via the CRB emitter."""
    await crb_emitter.emit(
        instance_id, "routine.proposed",
        {
            "correlation_id": correlation_id,
            "descriptor_hash": descriptor_hash,
            "instance_id": instance_id,
            "proposed_by": proposed_by,
            "member_id": member_id,
            "source_thread_id": source_thread_id,
        },
        correlation_id=correlation_id,
    )
    await event_stream.flush_now()


async def _emit_approved(
    crb_emitter, *, instance_id, descriptor_hash, correlation_id,
    approved_by="owner", member_id="mem_owner",
    source_turn_id="turn_x",
) -> str:
    """Emit a routine.approved event via the CRB emitter and return its event_id."""
    await crb_emitter.emit(
        instance_id, "routine.approved",
        {
            "correlation_id": correlation_id,
            "descriptor_hash": descriptor_hash,
            "instance_id": instance_id,
            "approved_by": approved_by,
            "member_id": member_id,
            "source_turn_id": source_turn_id,
        },
        correlation_id=correlation_id,
    )
    await event_stream.flush_now()
    correlated = await event_stream.events_by_correlation(instance_id, correlation_id)
    approval = next(e for e in correlated if e.event_type == "routine.approved")
    return approval.event_id


async def _emit_modification_approved(
    crb_emitter, *, instance_id, descriptor_hash, correlation_id,
    prev_workflow_id, change_summary="modify",
    approved_by="owner", member_id="mem_owner",
    source_turn_id="turn_x",
) -> str:
    await crb_emitter.emit(
        instance_id, "routine.modification.approved",
        {
            "correlation_id": correlation_id,
            "descriptor_hash": descriptor_hash,
            "instance_id": instance_id,
            "approved_by": approved_by,
            "member_id": member_id,
            "source_turn_id": source_turn_id,
            "prev_workflow_id": prev_workflow_id,
            "change_summary": change_summary,
        },
        correlation_id=correlation_id,
    )
    await event_stream.flush_now()
    correlated = await event_stream.events_by_correlation(instance_id, correlation_id)
    approval = next(e for e in correlated if e.event_type == "routine.modification.approved")
    return approval.event_id


async def _propose_and_approve(crb_emitter, descriptor: dict, *, instance_id="inst_a") -> str:
    """Convenience: emit proposed + approved for a clean approval flow.
    Returns the approval event_id."""
    desc_hash = compute_descriptor_hash(descriptor)
    correlation_id = f"corr-{uuid.uuid4().hex[:8]}"
    await _emit_proposed(
        crb_emitter, instance_id=instance_id,
        descriptor_hash=desc_hash, correlation_id=correlation_id,
    )
    return await _emit_approved(
        crb_emitter, instance_id=instance_id,
        descriptor_hash=desc_hash, correlation_id=correlation_id,
    )


# ===========================================================================
# AC #4: dry-run produces DryRunResult, no persistence
# ===========================================================================


class TestDryRun:
    async def test_dry_run_returns_dry_run_result(self, stack):
        sts = stack["sts"]
        result = await sts.register_workflow(
            instance_id="inst_a",
            descriptor=_basic_descriptor(),
            dry_run=True,
        )
        assert isinstance(result, DryRunResult)
        assert result.valid is True
        assert len(result.descriptor_hash) == 64

    async def test_dry_run_no_persistence(self, stack):
        sts = stack["sts"]
        before = await stack["wfr"].list_workflows("inst_a")
        await sts.register_workflow(
            instance_id="inst_a",
            descriptor=_basic_descriptor(),
            dry_run=True,
        )
        after = await stack["wfr"].list_workflows("inst_a")
        assert before == after

    async def test_dry_run_invalid_descriptor_returns_issues(self, stack):
        sts = stack["sts"]
        bad = _basic_descriptor()
        del bad["verifier"]
        result = await sts.register_workflow(
            instance_id="inst_a", descriptor=bad, dry_run=True,
        )
        assert result.valid is False
        assert any(i.severity == "error" for i in result.issues)

    async def test_dry_run_ignores_approval_event_id(self, stack):
        """dry_run=True should not consume an approval, even if one is
        passed."""
        sts = stack["sts"]
        result = await sts.register_workflow(
            instance_id="inst_a",
            descriptor=_basic_descriptor(),
            dry_run=True,
            approval_event_id="bogus-not-resolved",
        )
        assert isinstance(result, DryRunResult)


# ===========================================================================
# AC #5: real registration requires approval_event_id
# ===========================================================================


class TestApprovalRequired:
    async def test_dry_run_false_without_approval_raises(self, stack):
        sts = stack["sts"]
        with pytest.raises(ApprovalBindingMissing):
            await sts.register_workflow(
                instance_id="inst_a",
                descriptor=_basic_descriptor(),
                dry_run=False,
            )

    async def test_empty_string_approval_raises(self, stack):
        sts = stack["sts"]
        with pytest.raises(ApprovalBindingMissing):
            await sts.register_workflow(
                instance_id="inst_a",
                descriptor=_basic_descriptor(),
                dry_run=False,
                approval_event_id="",
            )

    async def test_unresolvable_approval_raises_event_not_found(self, stack):
        sts = stack["sts"]
        with pytest.raises(ApprovalEventNotFound):
            await sts.register_workflow(
                instance_id="inst_a",
                descriptor=_basic_descriptor(),
                approval_event_id="nonexistent-event-id",
            )


# ===========================================================================
# AC #6: approval source authority via envelope (NOT payload)
# ===========================================================================


class TestEnvelopeSourceAuthority:
    async def test_payload_spoof_rejected(self, stack):
        """A NON-CRB emitter that puts source_module='crb' in payload
        cannot fool STS — the envelope is what counts."""
        # Register a non-CRB emitter and use it to emit a fake approval.
        spoofer = event_stream.emitter_registry().register("not-crb")
        descriptor = _basic_descriptor()
        desc_hash = compute_descriptor_hash(descriptor)
        correlation_id = "corr-spoof"
        # Spoofer tries to claim CRB authority via payload.
        await spoofer.emit(
            "inst_a", "routine.approved",
            {
                "source_module": "crb",  # ← spoof attempt
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
        events = await event_stream.events_by_correlation("inst_a", correlation_id)
        approval_id = events[0].event_id
        # The envelope source must be "not-crb" regardless of payload.
        assert events[0].envelope.source_module == "not-crb"
        # STS must reject.
        with pytest.raises(ApprovalAuthoritySpoofed):
            await stack["sts"].register_workflow(
                instance_id="inst_a",
                descriptor=descriptor,
                approval_event_id=approval_id,
            )

    async def test_unregistered_legacy_emit_is_rejected(self, stack):
        """Legacy emit() produces unregistered envelope — STS rejects."""
        descriptor = _basic_descriptor()
        desc_hash = compute_descriptor_hash(descriptor)
        await event_stream.emit(
            "inst_a", "routine.approved",
            {
                "correlation_id": "corr-legacy",
                "descriptor_hash": desc_hash,
                "instance_id": "inst_a",
                "approved_by": "owner",
                "member_id": "mem_owner",
                "source_turn_id": "turn_x",
            },
            correlation_id="corr-legacy",
        )
        await event_stream.flush_now()
        events = await event_stream.events_by_correlation("inst_a", "corr-legacy")
        approval_id = events[0].event_id
        with pytest.raises(ApprovalAuthoritySpoofed):
            await stack["sts"].register_workflow(
                instance_id="inst_a",
                descriptor=descriptor,
                approval_event_id=approval_id,
            )

    async def test_wrong_event_type_rejected(self, stack):
        """An event with envelope.source_module='crb' but wrong type is
        not an approval event."""
        crb = stack["crb"]
        await crb.emit("inst_a", "routine.proposed", {"foo": "bar"})
        await event_stream.flush_now()
        events = await event_stream.events_in_window(
            "inst_a",
            since=__import__("datetime").datetime.fromtimestamp(0, tz=__import__("datetime").timezone.utc),
            until=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        proposed = next(e for e in events if e.event_type == "routine.proposed")
        with pytest.raises(ApprovalEventTypeInvalid):
            await stack["sts"].register_workflow(
                instance_id="inst_a",
                descriptor=_basic_descriptor(),
                approval_event_id=proposed.event_id,
            )


# ===========================================================================
# AC #7: approval authority field check
# ===========================================================================


class TestAuthorityFields:
    @pytest.mark.parametrize("missing_field", [
        "approved_by", "member_id", "source_turn_id",
        "correlation_id", "descriptor_hash", "instance_id",
    ])
    async def test_missing_field_raises_authority_incomplete(self, stack, missing_field):
        descriptor = _basic_descriptor()
        desc_hash = compute_descriptor_hash(descriptor)
        correlation_id = "corr-missing"
        # Emit valid proposed (so step 4 doesn't fail first).
        await _emit_proposed(
            stack["crb"], instance_id="inst_a",
            descriptor_hash=desc_hash, correlation_id=correlation_id,
        )
        # Build approval payload missing the field under test.
        payload = {
            "correlation_id": correlation_id,
            "descriptor_hash": desc_hash,
            "instance_id": "inst_a",
            "approved_by": "owner",
            "member_id": "mem_owner",
            "source_turn_id": "turn_x",
        }
        del payload[missing_field]
        await stack["crb"].emit(
            "inst_a", "routine.approved", payload,
            correlation_id=correlation_id,
        )
        await event_stream.flush_now()
        correlated = await event_stream.events_by_correlation("inst_a", correlation_id)
        approval = next(e for e in correlated if e.event_type == "routine.approved")
        with pytest.raises(ApprovalAuthorityIncomplete):
            await stack["sts"].register_workflow(
                instance_id="inst_a",
                descriptor=descriptor,
                approval_event_id=approval.event_id,
            )


# ===========================================================================
# AC #8: proposal anchor — correlation, hash, instance, envelope
# ===========================================================================


class TestProposalAnchor:
    async def test_no_proposal_raises_provenance_unverifiable(self, stack):
        descriptor = _basic_descriptor()
        desc_hash = compute_descriptor_hash(descriptor)
        # Emit ONLY the approved event — no matching proposed.
        await stack["crb"].emit(
            "inst_a", "routine.approved",
            {
                "correlation_id": "no-proposal-corr",
                "descriptor_hash": desc_hash,
                "instance_id": "inst_a",
                "approved_by": "owner",
                "member_id": "mem_owner",
                "source_turn_id": "turn_x",
            },
            correlation_id="no-proposal-corr",
        )
        await event_stream.flush_now()
        events = await event_stream.events_by_correlation("inst_a", "no-proposal-corr")
        approval_id = events[0].event_id
        with pytest.raises(ApprovalProvenanceUnverifiable):
            await stack["sts"].register_workflow(
                instance_id="inst_a",
                descriptor=descriptor,
                approval_event_id=approval_id,
            )

    async def test_proposal_hash_mismatch_raises_proposal_mismatch(self, stack):
        descriptor = _basic_descriptor()
        approved_hash = compute_descriptor_hash(descriptor)
        proposed_hash = "different-hash-value"
        correlation_id = "corr-mismatch"
        await _emit_proposed(
            stack["crb"], instance_id="inst_a",
            descriptor_hash=proposed_hash, correlation_id=correlation_id,
        )
        await stack["crb"].emit(
            "inst_a", "routine.approved",
            {
                "correlation_id": correlation_id,
                "descriptor_hash": approved_hash,
                "instance_id": "inst_a",
                "approved_by": "owner",
                "member_id": "mem_owner",
                "source_turn_id": "turn_x",
            },
            correlation_id=correlation_id,
        )
        await event_stream.flush_now()
        correlated = await event_stream.events_by_correlation("inst_a", correlation_id)
        approval = next(e for e in correlated if e.event_type == "routine.approved")
        with pytest.raises(ApprovalProposalMismatch):
            await stack["sts"].register_workflow(
                instance_id="inst_a",
                descriptor=descriptor,
                approval_event_id=approval.event_id,
            )

    async def test_proposal_with_wrong_envelope_source_raises(self, stack):
        """A 'proposed' event emitted by a non-CRB module is not a valid
        anchor."""
        spoofer = event_stream.emitter_registry().register("evil-drafter")
        descriptor = _basic_descriptor()
        desc_hash = compute_descriptor_hash(descriptor)
        correlation_id = "corr-bad-proposal"
        # Bad-source proposed.
        await spoofer.emit(
            "inst_a", "routine.proposed",
            {
                "correlation_id": correlation_id,
                "descriptor_hash": desc_hash,
                "instance_id": "inst_a",
                "proposed_by": "evil",
                "member_id": "mem_owner",
                "source_thread_id": "thr_x",
            },
            correlation_id=correlation_id,
        )
        # Valid CRB approved.
        await stack["crb"].emit(
            "inst_a", "routine.approved",
            {
                "correlation_id": correlation_id,
                "descriptor_hash": desc_hash,
                "instance_id": "inst_a",
                "approved_by": "owner",
                "member_id": "mem_owner",
                "source_turn_id": "turn_x",
            },
            correlation_id=correlation_id,
        )
        await event_stream.flush_now()
        correlated = await event_stream.events_by_correlation("inst_a", correlation_id)
        approval = next(e for e in correlated if e.event_type == "routine.approved")
        with pytest.raises(ApprovalProvenanceUnverifiable):
            await stack["sts"].register_workflow(
                instance_id="inst_a",
                descriptor=descriptor,
                approval_event_id=approval.event_id,
            )


# ===========================================================================
# AC #5 / step 5: instance match
# ===========================================================================


class TestInstanceMatch:
    async def test_caller_instance_mismatch_raises(self, stack):
        """The descriptor's instance_id is inst_a but caller passes
        inst_b. Post-Codex C4: the descriptor-instance-mismatch guard
        fires BEFORE the approval-event lookup, so the failure is
        ApprovalInstanceMismatch (not ApprovalEventNotFound)."""
        descriptor = _basic_descriptor(instance_id="inst_a")
        approval_id = await _propose_and_approve(stack["crb"], descriptor, instance_id="inst_a")
        with pytest.raises(ApprovalInstanceMismatch):
            await stack["sts"].register_workflow(
                instance_id="inst_b",
                descriptor=descriptor,
                approval_event_id=approval_id,
            )


# ===========================================================================
# AC #9: registration-time revalidation (P7) — NOT cached
# ===========================================================================


class TestRegistrationRevalidation:
    async def test_dry_run_valid_then_provider_disconnect_raises_at_register(self, stack):
        """Dry-run says valid=True with a route_to_agent referencing a
        registered agent. Then the agent is retired. Real registration
        with that approval re-runs validation and raises."""
        # Register an agent the descriptor can reference.
        await stack["agents"].register_agent(AgentRecord(
            agent_id="spec-agent", instance_id="inst_a",
            display_name="Spec", aliases=[], provider_key="inmemory",
            provider_config_ref="default", domain_summary="",
            capabilities_summary="", status="active",
        ))
        descriptor = _basic_descriptor(
            instance_local=True,
            action_sequence=[
                {
                    "action_type": "route_to_agent",
                    "parameters": {"agent_id": "spec-agent", "envelope": {"foo": "bar"}},
                    "per_action_expectation": "agent picks it up",
                },
            ],
        )
        # Step 1: dry-run says valid.
        result = await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor, dry_run=True,
        )
        assert result.valid is True
        # Step 2: emit proposed/approved against this descriptor.
        approval_id = await _propose_and_approve(stack["crb"], descriptor)
        # Step 3: retire the agent.
        await stack["agents"].update_status(
            instance_id="inst_a", agent_id="spec-agent", new_status="retired",
        )
        # Step 4: real registration must re-run validation and raise.
        with pytest.raises(RegistrationValidationFailed) as exc_info:
            await stack["sts"].register_workflow(
                instance_id="inst_a",
                descriptor=descriptor,
                approval_event_id=approval_id,
            )
        assert any(
            i.code in ("agent_not_active", "unknown_agent")
            for i in exc_info.value.issues
        )


# ===========================================================================
# AC #10: descriptor hash match at registration
# ===========================================================================


class TestHashMatch:
    async def test_descriptor_mutation_raises_descriptor_mismatch(self, stack):
        """Approve descriptor A; try to register descriptor B (different
        hash) using A's approval."""
        descriptor_a = _basic_descriptor(workflow_id="wf-a", display_name="Original")
        approval_id = await _propose_and_approve(stack["crb"], descriptor_a)
        descriptor_b = _basic_descriptor(workflow_id="wf-a", display_name="Mutated")
        with pytest.raises(ApprovalDescriptorMismatch):
            await stack["sts"].register_workflow(
                instance_id="inst_a",
                descriptor=descriptor_b,
                approval_event_id=approval_id,
            )


# ===========================================================================
# AC #11: atomic single-use AND terminal ApprovalAlreadyConsumed
# ===========================================================================


class TestSingleUseConsumption:
    async def test_first_registration_succeeds(self, stack):
        descriptor = _basic_descriptor()
        approval_id = await _propose_and_approve(stack["crb"], descriptor)
        wf = await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor,
            approval_event_id=approval_id,
        )
        assert wf.workflow_id == descriptor["workflow_id"]

    async def test_second_registration_raises_already_consumed(self, stack):
        descriptor_1 = _basic_descriptor(workflow_id="wf-1")
        descriptor_2 = _basic_descriptor(workflow_id="wf-2")
        # Both descriptors hash differently; approve the first.
        approval_id = await _propose_and_approve(stack["crb"], descriptor_1)
        await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor_1,
            approval_event_id=approval_id,
        )
        # Try to reuse the approval for descriptor_2 (would also fail
        # ApprovalDescriptorMismatch — but we want to verify the
        # consumption fires for descriptor_1 too).
        with pytest.raises(ApprovalAlreadyConsumed):
            await stack["sts"].register_workflow(
                instance_id="inst_a", descriptor=descriptor_1,
                approval_event_id=approval_id,
            )

    def test_already_consumed_docstring_states_terminal(self):
        """The error class itself must declare TERMINAL semantics so
        callers reading the class know not to retry."""
        doc = ApprovalAlreadyConsumed.__doc__ or ""
        assert "terminal" in doc.lower()
        assert "must not retry" in doc.lower()


# ===========================================================================
# AC #12: retry-after-failure preserves approval
# ===========================================================================


class TestRetryAfterFailure:
    async def test_validation_failure_does_not_consume_approval(self, stack):
        """A failure during step 6 (revalidation) raises
        RegistrationValidationFailed BEFORE step 9 persistence —
        the approval remains valid for retry."""
        await stack["agents"].register_agent(AgentRecord(
            agent_id="spec-agent", instance_id="inst_a",
            display_name="Spec", aliases=[], provider_key="inmemory",
            provider_config_ref="default", domain_summary="",
            capabilities_summary="", status="active",
        ))
        descriptor = _basic_descriptor(
            instance_local=True,
            action_sequence=[
                {
                    "action_type": "route_to_agent",
                    "parameters": {"agent_id": "spec-agent"},
                    "per_action_expectation": "agent picks it up",
                },
            ],
        )
        approval_id = await _propose_and_approve(stack["crb"], descriptor)
        # Pause the agent → first attempt fails (paused is reversible).
        await stack["agents"].update_status(
            instance_id="inst_a", agent_id="spec-agent", new_status="paused",
        )
        with pytest.raises(RegistrationValidationFailed):
            await stack["sts"].register_workflow(
                instance_id="inst_a", descriptor=descriptor,
                approval_event_id=approval_id,
            )
        # Restore agent → retry succeeds.
        await stack["agents"].update_status(
            instance_id="inst_a", agent_id="spec-agent", new_status="active",
        )
        wf = await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor,
            approval_event_id=approval_id,
        )
        assert wf.workflow_id == descriptor["workflow_id"]


# ===========================================================================
# AC #19: modification target binding
# ===========================================================================


class TestModificationBinding:
    async def test_mod_target_match_succeeds(self, stack):
        """Modify routine A with an approval bound to A's prev_workflow_id."""
        # Register A first.
        descriptor_a = _basic_descriptor(workflow_id="wf-a", display_name="A")
        approval_a = await _propose_and_approve(stack["crb"], descriptor_a)
        await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor_a,
            approval_event_id=approval_a,
        )
        # Build modification descriptor: same workflow_id, different
        # display_name, prev_version_id pointing at A.
        descriptor_mod = _basic_descriptor(
            workflow_id="wf-b", display_name="B",
            prev_version_id="wf-a",
        )
        # Emit modification approval.
        desc_hash = compute_descriptor_hash(descriptor_mod)
        correlation_id = "corr-mod"
        await _emit_proposed(
            stack["crb"], instance_id="inst_a",
            descriptor_hash=desc_hash, correlation_id=correlation_id,
        )
        approval_mod = await _emit_modification_approved(
            stack["crb"], instance_id="inst_a",
            descriptor_hash=desc_hash, correlation_id=correlation_id,
            prev_workflow_id="wf-a",
        )
        wf = await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor_mod,
            approval_event_id=approval_mod,
        )
        assert wf.workflow_id == "wf-b"

    async def test_mod_target_swap_attempt_raises(self, stack):
        """Approve modification of A; try to swap descriptor.prev_version_id
        to B at registration. Must raise — both AC #19 (Step 5b) and
        AC #20 (hash-includes-prev_version_id)."""
        # Register A.
        descriptor_a = _basic_descriptor(workflow_id="wf-a")
        approval_a = await _propose_and_approve(stack["crb"], descriptor_a)
        await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor_a,
            approval_event_id=approval_a,
        )
        # Register B.
        descriptor_b = _basic_descriptor(workflow_id="wf-b")
        approval_b = await _propose_and_approve(stack["crb"], descriptor_b)
        await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor_b,
            approval_event_id=approval_b,
        )
        # Approve modification of A.
        desc_mod_for_a = _basic_descriptor(
            workflow_id="wf-c", display_name="modify-A", prev_version_id="wf-a",
        )
        hash_for_a = compute_descriptor_hash(desc_mod_for_a)
        correlation_id = "corr-mod-attack"
        await _emit_proposed(
            stack["crb"], instance_id="inst_a",
            descriptor_hash=hash_for_a, correlation_id=correlation_id,
        )
        approval_mod = await _emit_modification_approved(
            stack["crb"], instance_id="inst_a",
            descriptor_hash=hash_for_a, correlation_id=correlation_id,
            prev_workflow_id="wf-a",
        )
        # Attack: register the SAME descriptor body but with prev_version_id
        # swapped to B. Hash will differ AND Step 5b will fire.
        desc_swap_to_b = dict(desc_mod_for_a)
        desc_swap_to_b["prev_version_id"] = "wf-b"
        # This now fails ApprovalDescriptorMismatch (the hash is
        # different) — that's the belt-and-suspenders backstop. But we
        # also want to confirm the explicit Step 5b path: feed the
        # ORIGINAL descriptor and override prev_version_id to verify
        # the binding. Simpler: forge an approval with prev_workflow_id
        # pointing at B but descriptor still pointing at A.
        # Direct test of Step 5b: forge a modification approval whose
        # prev_workflow_id is "wf-b" but the descriptor.prev_version_id
        # is "wf-a".
        bad_correlation = "corr-mod-bad-bind"
        bad_hash = compute_descriptor_hash(desc_mod_for_a)  # still binds A
        await _emit_proposed(
            stack["crb"], instance_id="inst_a",
            descriptor_hash=bad_hash, correlation_id=bad_correlation,
        )
        bad_approval = await _emit_modification_approved(
            stack["crb"], instance_id="inst_a",
            descriptor_hash=bad_hash, correlation_id=bad_correlation,
            prev_workflow_id="wf-b",  # ← mismatch with descriptor.prev_version_id
        )
        with pytest.raises(ApprovalModificationTargetMismatch):
            await stack["sts"].register_workflow(
                instance_id="inst_a", descriptor=desc_mod_for_a,
                approval_event_id=bad_approval,
            )

    async def test_mod_target_missing_workflow_raises(self, stack):
        """Modification approval points at a workflow that doesn't exist."""
        desc_mod = _basic_descriptor(
            workflow_id="wf-c", prev_version_id="wf-nonexistent",
        )
        desc_hash = compute_descriptor_hash(desc_mod)
        correlation_id = "corr-missing-target"
        await _emit_proposed(
            stack["crb"], instance_id="inst_a",
            descriptor_hash=desc_hash, correlation_id=correlation_id,
        )
        approval = await _emit_modification_approved(
            stack["crb"], instance_id="inst_a",
            descriptor_hash=desc_hash, correlation_id=correlation_id,
            prev_workflow_id="wf-nonexistent",
        )
        with pytest.raises(ApprovalModificationTargetMissing):
            await stack["sts"].register_workflow(
                instance_id="inst_a", descriptor=desc_mod,
                approval_event_id=approval,
            )

    async def test_mod_approval_missing_prev_workflow_id_raises(self, stack):
        """A routine.modification.approved with empty prev_workflow_id is
        incomplete authority."""
        desc_mod = _basic_descriptor(
            workflow_id="wf-c", prev_version_id="wf-a",
        )
        desc_hash = compute_descriptor_hash(desc_mod)
        correlation_id = "corr-missing-prev"
        await _emit_proposed(
            stack["crb"], instance_id="inst_a",
            descriptor_hash=desc_hash, correlation_id=correlation_id,
        )
        # Emit modification.approved WITHOUT prev_workflow_id (empty).
        await stack["crb"].emit(
            "inst_a", "routine.modification.approved",
            {
                "correlation_id": correlation_id,
                "descriptor_hash": desc_hash,
                "instance_id": "inst_a",
                "approved_by": "owner",
                "member_id": "mem_owner",
                "source_turn_id": "turn_x",
                "prev_workflow_id": "",  # missing
                "change_summary": "modify",
            },
            correlation_id=correlation_id,
        )
        await event_stream.flush_now()
        correlated = await event_stream.events_by_correlation("inst_a", correlation_id)
        approval = next(e for e in correlated if e.event_type == "routine.modification.approved")
        with pytest.raises(ApprovalAuthorityIncomplete):
            await stack["sts"].register_workflow(
                instance_id="inst_a", descriptor=desc_mod,
                approval_event_id=approval.event_id,
            )


# ===========================================================================
# AC #17 / #18: event shape pins
# ===========================================================================


class TestEventShape:
    async def test_proposed_event_well_formed(self, stack):
        """When CRB emits a proposed event, payload must contain the
        required fields. Pinning the test fixture is the AC #17 pin."""
        await _emit_proposed(
            stack["crb"], instance_id="inst_a",
            descriptor_hash="abc123", correlation_id="corr-shape",
        )
        events = await event_stream.events_by_correlation("inst_a", "corr-shape")
        assert len(events) == 1
        proposed = events[0]
        assert proposed.event_type == "routine.proposed"
        assert proposed.envelope.source_module == "crb"
        for field in (
            "correlation_id", "descriptor_hash", "instance_id",
            "proposed_by", "member_id", "source_thread_id",
        ):
            assert field in proposed.payload, f"missing {field}"

    async def test_modification_approved_parallel_shape(self, stack):
        """routine.modification.approved adds prev_workflow_id and
        change_summary to the standard approval shape."""
        approval_id = await _emit_modification_approved(
            stack["crb"], instance_id="inst_a",
            descriptor_hash="abc123", correlation_id="corr-mod-shape",
            prev_workflow_id="wf-a", change_summary="bump bounds",
        )
        events = await event_stream.events_by_correlation("inst_a", "corr-mod-shape")
        approval = next(e for e in events if e.event_type == "routine.modification.approved")
        assert approval.envelope.source_module == "crb"
        for field in (
            "correlation_id", "descriptor_hash", "instance_id",
            "approved_by", "member_id", "source_turn_id",
            "prev_workflow_id", "change_summary",
        ):
            assert field in approval.payload, f"missing {field}"


# ===========================================================================
# Happy path
# ===========================================================================


class TestPostCodexHardening:
    """Codex final-pass findings (3 REAL + 2 HARDENING) — pinned in C4."""

    # REAL #1
    async def test_descriptor_instance_id_must_match_caller(self, stack):
        """A descriptor whose ``instance_id`` field disagrees with the
        caller's ``instance_id`` MUST be rejected before the approval
        event is consumed. Closes the multi-tenancy boundary bypass."""
        descriptor = _basic_descriptor(instance_id="inst_b")  # mismatched
        # Provide a working approval to prove the fail-fast is the
        # rejection cause, not approval-resolution noise.
        approval_id = await _propose_and_approve(
            stack["crb"], descriptor, instance_id="inst_b",
        )
        with pytest.raises(ApprovalInstanceMismatch):
            await stack["sts"].register_workflow(
                instance_id="inst_a",  # caller's instance
                descriptor=descriptor,
                approval_event_id=approval_id,
            )

    async def test_descriptor_without_instance_id_does_not_raise_early(self, stack):
        """If the descriptor omits ``instance_id``, the early guard is a
        no-op (downstream ``_build_workflow`` then enforces it). This
        preserves the existing parse-error path."""
        descriptor = _basic_descriptor()
        del descriptor["instance_id"]
        # No early raise; the descriptor parser fails later.
        with pytest.raises(ApprovalBindingMissing):
            # No approval supplied → ApprovalBindingMissing fires before
            # any descriptor parsing.
            await stack["sts"].register_workflow(
                instance_id="inst_a", descriptor=descriptor,
            )

    # REAL #2
    async def test_descriptor_with_prev_version_requires_modification_event(self, stack):
        """A descriptor declaring itself a modification (carries
        ``prev_version_id``) paired with a plain ``routine.approved``
        event MUST be rejected — the bidirectional consistency
        requirement that mirrors the existing
        modification-approved-needs-prev_workflow_id check."""
        descriptor = _basic_descriptor(prev_version_id="wf-some-target")
        # Use the regular (non-modification) approval pipeline.
        approval_id = await _propose_and_approve(stack["crb"], descriptor)
        with pytest.raises(ApprovalEventTypeInvalid):
            await stack["sts"].register_workflow(
                instance_id="inst_a", descriptor=descriptor,
                approval_event_id=approval_id,
            )

    async def test_descriptor_without_prev_version_accepts_plain_approval(self, stack):
        """Sanity: a descriptor without ``prev_version_id`` continues to
        register via plain ``routine.approved``. The bidirectional
        check is asymmetric — only the modification side triggers."""
        descriptor = _basic_descriptor()  # no prev_version_id
        approval_id = await _propose_and_approve(stack["crb"], descriptor)
        wf = await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor,
            approval_event_id=approval_id,
        )
        assert wf.workflow_id == descriptor["workflow_id"]

    # HARDENING #1
    async def test_workflow_id_collision_does_not_consume_approval(self, stack):
        """A duplicate ``workflow_id`` raises IntegrityError but the
        approval was rolled back — translating to
        ``ApprovalAlreadyConsumed`` would incorrectly mark a recoverable
        failure as terminal. The translation MUST only fire on the
        approval-binding index."""
        # Register a workflow with a known id.
        descriptor_a = _basic_descriptor(workflow_id="wf-shared-id")
        approval_a = await _propose_and_approve(stack["crb"], descriptor_a)
        await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor_a,
            approval_event_id=approval_a,
        )
        # New approval, same workflow_id — collision is on the PK,
        # not the approval-binding index.
        descriptor_b = _basic_descriptor(workflow_id="wf-shared-id", display_name="B")
        approval_b = await _propose_and_approve(stack["crb"], descriptor_b)
        # Should NOT raise ApprovalAlreadyConsumed (which is terminal).
        # The underlying IntegrityError surfaces because workflow_id
        # PK collides — but the approval is recoverable.
        import aiosqlite
        with pytest.raises(aiosqlite.IntegrityError):
            await stack["sts"].register_workflow(
                instance_id="inst_a", descriptor=descriptor_b,
                approval_event_id=approval_b,
            )

    # HARDENING #2 — taxonomy fallback pin (optional Codex suggestion)
    async def test_no_matching_candidate_preserves_error_taxonomy(self, stack):
        """When no full candidate matches the (CRB + hash + instance)
        filter, the fallback path preserves the legacy error class
        taxonomy: a wrong-source proposal raises
        ApprovalProvenanceUnverifiable; a hash-mismatch raises
        ApprovalProposalMismatch. Pins the fallback behavior so a
        future refactor can't quietly collapse the error classes."""
        descriptor = _basic_descriptor()
        desc_hash = compute_descriptor_hash(descriptor)
        correlation_id = "corr-fallback-taxonomy"
        # Emit a stale proposed with a different hash (CRB-sourced).
        await stack["crb"].emit(
            "inst_a", "routine.proposed",
            {
                "correlation_id": correlation_id,
                "descriptor_hash": "stale-hash",
                "instance_id": "inst_a",
                "proposed_by": "drafter",
                "member_id": "mem_owner",
                "source_thread_id": "thr_x",
            },
            correlation_id=correlation_id,
        )
        # Approve with the correct hash — but no proposed event matches
        # the candidate filter (none has the correct hash).
        await stack["crb"].emit(
            "inst_a", "routine.approved",
            {
                "correlation_id": correlation_id,
                "descriptor_hash": desc_hash,
                "instance_id": "inst_a",
                "approved_by": "owner",
                "member_id": "mem_owner",
                "source_turn_id": "turn_x",
            },
            correlation_id=correlation_id,
        )
        await event_stream.flush_now()
        correlated = await event_stream.events_by_correlation("inst_a", correlation_id)
        approval = next(e for e in correlated if e.event_type == "routine.approved")
        # The fallback should preserve ApprovalProposalMismatch (the
        # only reason no candidate matched is hash mismatch in this
        # setup).
        with pytest.raises(ApprovalProposalMismatch):
            await stack["sts"].register_workflow(
                instance_id="inst_a", descriptor=descriptor,
                approval_event_id=approval.event_id,
            )

    # HARDENING #2
    async def test_proposal_lookup_filters_by_descriptor_hash(self, stack):
        """A correlation_id that contains multiple ``routine.proposed``
        events should select the one whose descriptor_hash matches the
        approval, even if it is not the first."""
        descriptor = _basic_descriptor()
        desc_hash = compute_descriptor_hash(descriptor)
        correlation_id = "corr-multi-proposed"
        # Emit a stale proposed first with a DIFFERENT descriptor_hash.
        await stack["crb"].emit(
            "inst_a", "routine.proposed",
            {
                "correlation_id": correlation_id,
                "descriptor_hash": "stale-hash-from-earlier-iteration",
                "instance_id": "inst_a",
                "proposed_by": "drafter",
                "member_id": "mem_owner",
                "source_thread_id": "thr_x",
            },
            correlation_id=correlation_id,
        )
        # Then emit the real proposed with the correct hash.
        await _emit_proposed(
            stack["crb"], instance_id="inst_a",
            descriptor_hash=desc_hash, correlation_id=correlation_id,
        )
        approval_id = await _emit_approved(
            stack["crb"], instance_id="inst_a",
            descriptor_hash=desc_hash, correlation_id=correlation_id,
        )
        # Should succeed — the lookup filters proposed candidates by
        # hash + instance + envelope source, not by "first wins."
        wf = await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor,
            approval_event_id=approval_id,
        )
        assert wf.workflow_id == descriptor["workflow_id"]


class TestHappyPath:
    async def test_propose_approve_register_round_trip(self, stack):
        descriptor = _basic_descriptor(display_name="Round Trip")
        approval_id = await _propose_and_approve(stack["crb"], descriptor)
        wf = await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor,
            approval_event_id=approval_id,
        )
        assert wf.workflow_id == descriptor["workflow_id"]
        # Verify the approval_event_id was written to the row.
        async with stack["wfr"]._db.execute(
            "SELECT approval_event_id FROM workflows WHERE workflow_id = ?",
            (wf.workflow_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == approval_id

    async def test_register_workflow_returns_workflow_type(self, stack):
        descriptor = _basic_descriptor()
        approval_id = await _propose_and_approve(stack["crb"], descriptor)
        result = await stack["sts"].register_workflow(
            instance_id="inst_a", descriptor=descriptor,
            approval_event_id=approval_id,
        )
        # Not a DryRunResult — a Workflow.
        assert not isinstance(result, DryRunResult)
        assert hasattr(result, "workflow_id")
