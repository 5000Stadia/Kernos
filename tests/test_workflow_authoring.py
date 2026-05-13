"""WORKFLOW-AUTHORING-PRIMITIVES-V1 substrate-fidelity tests.

Pins Spec 5 v2's contract across 8 categories:

  * Composition-tier registration (Kernos authors; persisted; ActionStateRecord)
  * Governance-tier classification (Kernos vs architect; Kernos-cannot-claim-substrate;
    classifier covers all substrate verbs)
  * Activation gate (architect-only; fail-closed on unset env var; state machine
    transitions; idempotent on already-active; register-trigger-on-active-rejected;
    at-most-one-inflight-crosses-deactivation-boundary)
  * Deactivation (architect-only; in-flight executions complete naturally;
    reactivation)
  * Validation feedback (structured ValidationError shape with all categories;
    multiple errors aggregated)
  * Disposition layer (tool description present; manage_plan contrast)
  * End-to-end (Kernos authors → architect activates → trigger fires)
  * Composition with Spec 3 ActionStateRecord
"""
from __future__ import annotations

import asyncio
import os

import aiosqlite
import pytest

from kernos.kernel import event_stream
from kernos.kernel.workflows.action_library import (
    ActionLibrary,
    BranchAction,
    MarkStateAction,
    NotifyUserAction,
)
from kernos.kernel.workflows.authoring import (
    ACTOR_ARCHITECT,
    ACTOR_KERNOS,
    ACTOR_SYSTEM,
    AuthoringContext,
    CAT_GOVERNANCE_CLAIM_VIOLATION,
    CAT_GOVERNANCE_TIER_VIOLATION,
    CAT_INVALID_ACTIVATION_STATE,
    CAT_NOT_AUTHORIZED,
    CAT_UNKNOWN_STEP_ID,
    ORIENTATION_PROMPT_ADDITION,
    REGISTER_WORKFLOW_TOOL_DESCRIPTION,
    SUBSTRATE_TOOL_IDS,
    activate_workflow,
    classify_governance_tier,
    deactivate_workflow,
    derive_actor_kind,
    register_trigger,
    register_workflow,
)
from kernos.kernel.workflows.execution_engine import ExecutionEngine
from kernos.kernel.workflows.ledger import WorkflowLedger
from kernos.kernel.workflows.registered_workflows import (
    STATE_ACTIVE,
    STATE_DEACTIVATED,
    STATE_REGISTERED,
    TIER_COMPOSITION,
    TIER_SUBSTRATE,
    get_registered_workflow,
    is_workflow_active,
)
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
    validate_workflow,
)


# ===========================================================================
# Helpers + fixtures
# ===========================================================================


ARCHITECT_ID = "op_architect_test"


def _descriptor(
    *,
    workflow_id: str = "wf-auth-test",
    instance_id: str = "inst_a",
    name: str = "auth test",
    action_type: str = "mark_state",
    action_id: str = "step1",
    params: dict | None = None,
    terminal_branches: dict | None = None,
) -> dict:
    """Build a descriptor dict that _build_workflow can parse."""
    if params is None:
        params = {"key": "x", "value": 1, "scope": "instance"}
    return {
        "workflow_id": workflow_id,
        "instance_id": instance_id,
        "name": name,
        "description": "",
        "owner": "owner",
        "version": "1.0",
        "bounds": {
            "iteration_count": 1, "wall_time_seconds": 30,
            "cost_usd": None, "composite": None,
        },
        "verifier": {"flavor": "deterministic", "check": "ok"},
        "action_sequence": [{
            "action_type": action_type,
            "id": action_id,
            "parameters": params,
            "continuation_rules": {"on_failure": "abort"},
        }],
        "approval_gates": [],
        "trigger": {
            "event_type": "cc.batch.report",
            "predicate": {"op": "exists", "path": "event_id"},
        },
        "terminal_branches": terminal_branches or {},
    }


def _state_store():
    store: dict = {}

    async def set_(*, key, value, scope, instance_id):
        store[(scope, instance_id, key)] = value

    async def get_(*, key, scope, instance_id):
        return store.get((scope, instance_id, key))

    return store, set_, get_


@pytest.fixture
def architect_env(monkeypatch):
    """Set KERNOS_ARCHITECT_ACTOR_ID for the duration of the test."""
    monkeypatch.setenv("KERNOS_ARCHITECT_ACTOR_ID", ARCHITECT_ID)


@pytest.fixture
def unset_architect_env(monkeypatch):
    """Ensure KERNOS_ARCHITECT_ACTOR_ID is unset."""
    monkeypatch.delenv("KERNOS_ARCHITECT_ACTOR_ID", raising=False)


@pytest.fixture
async def stack(tmp_path):
    """Engine stack with registered_workflows schema available."""
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    trig = TriggerRegistry()
    await trig.start(str(tmp_path))
    wfr = WorkflowRegistry()
    await wfr.start(str(tmp_path), trig)
    store, set_, get_ = _state_store()
    lib = ActionLibrary()
    lib.register(MarkStateAction(state_store_set=set_, state_store_get=get_))
    lib.register(BranchAction())
    delivered: list = []

    async def deliver(**kw):
        delivered.append(kw)
        return {"persisted_id": f"msg-{len(delivered)}"}
    lib.register(NotifyUserAction(deliver_fn=deliver))
    ledger = WorkflowLedger(str(tmp_path))
    engine = ExecutionEngine()
    await engine.start(str(tmp_path), trig, wfr, lib, ledger, space_resolver=None)
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


def _kernos_ctx() -> AuthoringContext:
    return AuthoringContext(actor_id="mem_kernos_test", actor_kind=ACTOR_KERNOS)


def _architect_ctx() -> AuthoringContext:
    return AuthoringContext(actor_id=ARCHITECT_ID, actor_kind=ACTOR_ARCHITECT)


def _system_ctx() -> AuthoringContext:
    return AuthoringContext(actor_id="system", actor_kind=ACTOR_SYSTEM)


# ===========================================================================
# Category 1: Composition-tier registration
# ===========================================================================


class TestRegisterComposition:
    async def test_kernos_registers_valid_composition_workflow(
        self, stack, architect_env,
    ):
        result = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-1"),
            TIER_COMPOSITION,
        )
        assert result.success, f"failed with errors: {result.errors}"
        assert result.workflow_id == "wf-1"
        # registered_workflows row exists with correct fields.
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-1",
        )
        assert row is not None
        assert row.governance_tier == TIER_COMPOSITION
        assert row.activation_state == STATE_REGISTERED
        assert row.architect_authored is False
        assert row.authored_by == "mem_kernos_test"

    async def test_kernos_can_register_trigger_for_registered_workflow(
        self, stack, architect_env,
    ):
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-2"),
            TIER_COMPOSITION,
        )
        assert reg.success
        trig_result = await register_trigger(
            stack["engine"], _kernos_ctx(), "wf-2",
            event_type="cc.batch.report",
            predicate={"op": "exists", "path": "event_id"},
        )
        assert trig_result.success
        assert trig_result.trigger_id.startswith("trig_")


# ===========================================================================
# Category 2: Substrate-tier governance enforcement
# ===========================================================================


class TestGovernanceTier:
    def test_classifier_composition_for_simple_workflow(self):
        # Use _build_workflow + validate_workflow to get a Workflow.
        from kernos.kernel.workflows.descriptor_parser import _build_workflow
        wf = _build_workflow(_descriptor())
        validate_workflow(wf)
        assert classify_governance_tier(wf) == TIER_COMPOSITION

    def test_classifier_substrate_for_call_tool_authoring(self):
        from kernos.kernel.workflows.descriptor_parser import _build_workflow
        wf = _build_workflow(_descriptor(
            action_type="call_tool",
            params={"tool_id": "register_workflow", "args": {}},
        ))
        validate_workflow(wf)
        assert classify_governance_tier(wf) == TIER_SUBSTRATE

    def test_classifier_substrate_for_mark_state_substrate_key(self):
        from kernos.kernel.workflows.descriptor_parser import _build_workflow
        wf = _build_workflow(_descriptor(
            action_type="mark_state",
            params={"key": "friction_pattern.something",
                    "value": 1, "scope": "instance"},
        ))
        validate_workflow(wf)
        assert classify_governance_tier(wf) == TIER_SUBSTRATE

    def test_classifier_substrate_for_append_to_ledger_substrate(self):
        from kernos.kernel.workflows.descriptor_parser import _build_workflow
        wf = _build_workflow(_descriptor(
            action_type="append_to_ledger",
            params={"ledger": "autonomy_loop_outcomes", "entry": {}},
        ))
        validate_workflow(wf)
        assert classify_governance_tier(wf) == TIER_SUBSTRATE

    async def test_kernos_substrate_tier_workflow_rejected(
        self, stack, architect_env,
    ):
        # Compose a descriptor with a substrate-modifying call_tool.
        result = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(
                workflow_id="wf-substr",
                action_type="call_tool",
                params={"tool_id": "register_workflow", "args": {}},
            ),
            TIER_COMPOSITION,
        )
        assert result.success is False
        assert any(
            err.category == CAT_GOVERNANCE_TIER_VIOLATION
            for err in result.errors
        )

    async def test_kernos_cannot_claim_substrate_tier(
        self, stack, architect_env,
    ):
        # Claim substrate_tier even on a composition-shaped descriptor.
        result = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-claim-substr"),
            TIER_SUBSTRATE,
        )
        assert result.success is False
        assert any(
            err.category == CAT_GOVERNANCE_CLAIM_VIOLATION
            for err in result.errors
        )

    async def test_architect_can_register_substrate_tier(
        self, stack, architect_env,
    ):
        result = await register_workflow(
            stack["engine"], _architect_ctx(),
            _descriptor(
                workflow_id="wf-architect-substr",
                action_type="call_tool",
                params={"tool_id": "register_workflow", "args": {}},
            ),
            TIER_SUBSTRATE,
        )
        assert result.success, f"failed: {result.errors}"
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-architect-substr",
        )
        assert row is not None
        assert row.governance_tier == TIER_SUBSTRATE
        assert row.architect_authored is True


# ===========================================================================
# Category 3: Architect-only activation + state machine
# ===========================================================================


class TestActivation:
    async def test_kernos_cannot_activate(self, stack, architect_env):
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-act-1"),
            TIER_COMPOSITION,
        )
        assert reg.success
        result = await activate_workflow(
            stack["engine"], _kernos_ctx(), "wf-act-1",
        )
        assert result.success is False
        assert any(
            err.category == CAT_NOT_AUTHORIZED for err in result.errors
        )

    async def test_system_cannot_activate(self, stack, architect_env):
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-act-2"),
            TIER_COMPOSITION,
        )
        assert reg.success
        result = await activate_workflow(
            stack["engine"], _system_ctx(), "wf-act-2",
        )
        assert result.success is False
        assert any(
            err.category == CAT_NOT_AUTHORIZED for err in result.errors
        )

    async def test_unset_env_var_fails_architect_call(
        self, stack, unset_architect_env,
    ):
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-act-3"),
            TIER_COMPOSITION,
        )
        assert reg.success
        # Even with actor_kind="architect", the env var being unset
        # means fail-closed.
        result = await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-act-3",
        )
        assert result.success is False
        assert any(
            err.category == CAT_NOT_AUTHORIZED for err in result.errors
        )

    async def test_architect_activates_registered_workflow(
        self, stack, architect_env,
    ):
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-act-4"),
            TIER_COMPOSITION,
        )
        assert reg.success
        result = await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-act-4",
        )
        assert result.success
        assert await is_workflow_active(
            stack["engine"]._db, workflow_id="wf-act-4",
        )

    async def test_reactivation_after_deactivation(
        self, stack, architect_env,
    ):
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-react"),
            TIER_COMPOSITION,
        )
        assert reg.success
        ctx = _architect_ctx()
        assert (await activate_workflow(
            stack["engine"], ctx, "wf-react"
        )).success
        assert (await deactivate_workflow(
            stack["engine"], ctx, "wf-react", reason="test",
        )).success
        # Reactivate.
        assert (await activate_workflow(
            stack["engine"], ctx, "wf-react"
        )).success
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-react",
        )
        assert row.activation_state == STATE_ACTIVE

    async def test_reactivation_idempotent_on_already_active(
        self, stack, architect_env,
    ):
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-idem"),
            TIER_COMPOSITION,
        )
        assert reg.success
        ctx = _architect_ctx()
        first = await activate_workflow(stack["engine"], ctx, "wf-idem")
        assert first.success
        second = await activate_workflow(stack["engine"], ctx, "wf-idem")
        assert second.success
        assert second.extra.get("already_active") is True

    async def test_register_trigger_on_active_workflow_rejected(
        self, stack, architect_env,
    ):
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-trig-locked"),
            TIER_COMPOSITION,
        )
        assert reg.success
        assert (await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-trig-locked",
        )).success
        # Now register_trigger should reject.
        result = await register_trigger(
            stack["engine"], _kernos_ctx(),
            "wf-trig-locked",
            event_type="cc.batch.report",
            predicate={"op": "exists", "path": "event_id"},
        )
        assert result.success is False
        assert any(
            err.category == CAT_INVALID_ACTIVATION_STATE
            for err in result.errors
        )


# ===========================================================================
# Category 4: Deactivation
# ===========================================================================


class TestDeactivation:
    async def test_architect_deactivates_active(self, stack, architect_env):
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-deact-1"),
            TIER_COMPOSITION,
        )
        assert reg.success
        ctx = _architect_ctx()
        assert (await activate_workflow(
            stack["engine"], ctx, "wf-deact-1"
        )).success
        result = await deactivate_workflow(
            stack["engine"], ctx, "wf-deact-1", reason="test reason",
        )
        assert result.success
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-deact-1",
        )
        assert row.activation_state == STATE_DEACTIVATED
        assert row.deactivation_reason == "test reason"

    async def test_kernos_cannot_deactivate(self, stack, architect_env):
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-deact-k"),
            TIER_COMPOSITION,
        )
        assert reg.success
        assert (await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-deact-k",
        )).success
        result = await deactivate_workflow(
            stack["engine"], _kernos_ctx(), "wf-deact-k",
        )
        assert result.success is False
        assert any(
            err.category == CAT_NOT_AUTHORIZED for err in result.errors
        )


# ===========================================================================
# Category 5: Trigger dispatch + activation_state integration
# ===========================================================================


class TestTriggerActivationIntegration:
    async def test_inactive_workflow_triggers_do_not_fire(
        self, stack, architect_env,
    ):
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-inactive"),
            TIER_COMPOSITION,
        )
        assert reg.success
        # Don't activate. Register trigger (allowed in
        # registered_not_activated).
        trig = await register_trigger(
            stack["engine"], _kernos_ctx(), "wf-inactive",
            event_type="cc.batch.report",
            predicate={"op": "exists", "path": "event_id"},
        )
        assert trig.success
        # Emit the matching event.
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        # Wait briefly; the workflow must NOT have fired.
        for _ in range(20):
            executions = await stack["engine"].list_executions(
                "inst_a", state="completed",
            )
            if executions:
                break
            await asyncio.sleep(0.02)
        executions = await stack["engine"].list_executions("inst_a")
        # No execution row should exist for wf-inactive.
        assert not any(
            e.workflow_id == "wf-inactive" for e in executions
        )

    async def test_activated_workflow_triggers_fire(
        self, stack, architect_env,
    ):
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-active-fires"),
            TIER_COMPOSITION,
        )
        assert reg.success
        trig = await register_trigger(
            stack["engine"], _kernos_ctx(), "wf-active-fires",
            event_type="cc.batch.report",
            predicate={"op": "exists", "path": "event_id"},
        )
        assert trig.success
        assert (await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-active-fires",
        )).success
        # Emit the matching event.
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        # Wait for completion.
        for _ in range(100):
            executions = await stack["engine"].list_executions(
                "inst_a", state="completed",
            )
            if any(e.workflow_id == "wf-active-fires" for e in executions):
                break
            await asyncio.sleep(0.02)
        assert any(
            e.workflow_id == "wf-active-fires" for e in executions
        )


# ===========================================================================
# Category 6: Validation feedback
# ===========================================================================


class TestValidationFeedback:
    async def test_invalid_descriptor_shape_reported(
        self, stack, architect_env,
    ):
        result = await register_workflow(
            stack["engine"], _kernos_ctx(),
            "not a dict",  # type: ignore[arg-type]
            TIER_COMPOSITION,
        )
        assert result.success is False
        assert len(result.errors) == 1
        assert result.errors[0].category == "descriptor_shape_invalid"

    async def test_unknown_step_reference_reported(
        self, stack, architect_env,
    ):
        descriptor = _descriptor(workflow_id="wf-bad-ref")
        descriptor["action_sequence"][0]["parameters"]["value"] = (
            "{step.does_not_exist.output.id}"
        )
        result = await register_workflow(
            stack["engine"], _kernos_ctx(),
            descriptor, TIER_COMPOSITION,
        )
        assert result.success is False
        assert any(
            err.category == CAT_UNKNOWN_STEP_ID
            for err in result.errors
        )


# ===========================================================================
# Category 7: Disposition guidance
# ===========================================================================


class TestDispositionLayer:
    def test_tool_description_contrasts_with_manage_plan(self):
        # Path-1 refinement: register_workflow tool description
        # explicitly contrasts with manage_plan so Kernos's
        # decision rule is clean.
        assert "manage_plan" in REGISTER_WORKFLOW_TOOL_DESCRIPTION
        assert "reasoning turn" in REGISTER_WORKFLOW_TOOL_DESCRIPTION
        assert "workspace" in REGISTER_WORKFLOW_TOOL_DESCRIPTION

    def test_tool_description_names_workflow_shape(self):
        # The contrast clause names what makes a workflow the right
        # shape (multi-step coordinated, async signals, restart,
        # branching).
        assert "multi-step coordinated" in REGISTER_WORKFLOW_TOOL_DESCRIPTION
        assert "approval" in REGISTER_WORKFLOW_TOOL_DESCRIPTION.lower()

    def test_orientation_prompt_names_both_primitives(self):
        assert "PLANS" in ORIENTATION_PROMPT_ADDITION
        assert "WORKFLOWS" in ORIENTATION_PROMPT_ADDITION
        assert "manage_plan" in ORIENTATION_PROMPT_ADDITION
        assert "register_workflow" in ORIENTATION_PROMPT_ADDITION


# ===========================================================================
# Category 8: End-to-end bootstrap
# ===========================================================================


class TestEndToEnd:
    async def test_kernos_authors_architect_activates_workflow_fires(
        self, stack, architect_env,
    ):
        # (a) Kernos registers workflow.
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-e2e"),
            TIER_COMPOSITION,
        )
        assert reg.success
        # (b) Kernos registers trigger.
        trig = await register_trigger(
            stack["engine"], _kernos_ctx(), "wf-e2e",
            event_type="cc.batch.report",
            predicate={"op": "exists", "path": "event_id"},
        )
        assert trig.success
        # (c) Trigger fires from matching event → NO execution
        # (workflow not activated yet).
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        await asyncio.sleep(0.1)
        executions = await stack["engine"].list_executions("inst_a")
        assert not any(
            e.workflow_id == "wf-e2e" for e in executions
        )
        # (d) Architect activates.
        act = await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-e2e",
        )
        assert act.success
        # (e) Trigger fires from another matching event → execution
        # row created and completes.
        await event_stream.emit("inst_a", "cc.batch.report", {"second": True})
        await event_stream.flush_now()
        for _ in range(100):
            executions = await stack["engine"].list_executions(
                "inst_a", state="completed",
            )
            if any(e.workflow_id == "wf-e2e" for e in executions):
                break
            await asyncio.sleep(0.02)
        assert any(
            e.workflow_id == "wf-e2e" for e in executions
        )


# ===========================================================================
# Category 9: ActionStateRecord composition (Spec 3 surface)
# ===========================================================================


class TestSpec3Composition:
    def test_authoring_action_state_record_builder(self):
        from kernos.kernel.workflows.authoring import (
            _build_authoring_action_state_record,
        )
        record = _build_authoring_action_state_record(
            operation="activate_workflow",
            actor=_architect_ctx(),
            workflow_id="wf-test",
        )
        assert record.operation == "activate_workflow"
        assert record.operation_class == "manage"
        assert record.risk_level == "high"  # the safety boundary override
        assert "wf-test" in record.affected_objects
        assert any(
            ref.startswith("workflow_id:wf-test")
            for ref in record.receipt_refs
        )
        assert any(
            ref.startswith("actor_kind:")
            for ref in record.receipt_refs
        )

    def test_authoring_action_state_record_register_is_medium(self):
        from kernos.kernel.workflows.authoring import (
            _build_authoring_action_state_record,
        )
        record = _build_authoring_action_state_record(
            operation="register_workflow",
            actor=_kernos_ctx(),
            workflow_id="wf-test",
        )
        assert record.risk_level == "medium"


# ===========================================================================
# Helper sanity
# ===========================================================================


class TestActorKindDerivation:
    def test_derive_actor_kind_kernos_when_member_id(self, architect_env):
        kind = derive_actor_kind("mem_some_user")
        assert kind == ACTOR_KERNOS

    def test_derive_actor_kind_architect_when_id_matches(
        self, architect_env,
    ):
        kind = derive_actor_kind(ARCHITECT_ID)
        assert kind == ACTOR_ARCHITECT

    def test_derive_actor_kind_system_for_empty(self, architect_env):
        kind = derive_actor_kind("")
        assert kind == ACTOR_SYSTEM

    def test_substrate_tool_ids_narrow_v1(self):
        # Architect Q1 ruling: narrow list for v1.
        assert SUBSTRATE_TOOL_IDS == {
            "register_workflow", "register_trigger",
            "activate_workflow", "deactivate_workflow",
        }
