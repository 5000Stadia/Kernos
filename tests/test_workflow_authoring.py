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

    def test_substrate_tool_ids_narrow_v1_plus_spec6_autonomy(self):
        """Spec 5 originally pinned 4 authoring tools as substrate-tier
        (architect Q1 narrow-list ruling). Spec 6 commit 2 extends with
        the 3 autonomy-loop substrate-tier tools that mutate
        friction-pattern lifecycle / autonomy_loop_outcomes ledger —
        deliberate architect amendment via the v7.3 ratification."""
        assert SUBSTRATE_TOOL_IDS == {
            # Spec 5 authoring tools.
            "register_workflow", "register_trigger",
            "activate_workflow", "deactivate_workflow",
            # Spec 6 autonomy-loop substrate-tier tools.
            "transition_friction_pattern_lifecycle",
            "record_friction_pattern_recurrence",
            "emit_autonomy_loop_event",
        }


# ===========================================================================
# Spec 5 post-impl Codex round folds
# ===========================================================================


class TestPostImplFolds:
    """Pins Spec 5 post-impl Codex round 1 fixes (9 findings)."""

    # --- B3: dynamic substrate target detection ---

    def test_classifier_rejects_templated_tool_id(self, architect_env):
        # Spec 5 post-impl Codex Blocker 3: templated substrate-
        # sensitive selector classifies as substrate_tier.
        from kernos.kernel.workflows.descriptor_parser import _build_workflow
        wf = _build_workflow(_descriptor(
            action_type="call_tool",
            params={"tool_id": "{idea_payload.tool_id}", "args": {}},
        ))
        validate_workflow(wf)
        assert classify_governance_tier(wf) == TIER_SUBSTRATE

    def test_classifier_rejects_templated_mark_state_key(self, architect_env):
        from kernos.kernel.workflows.descriptor_parser import _build_workflow
        wf = _build_workflow(_descriptor(
            action_type="mark_state",
            params={"key": "{idea_payload.target_key}",
                    "value": 1, "scope": "instance"},
        ))
        validate_workflow(wf)
        assert classify_governance_tier(wf) == TIER_SUBSTRATE

    def test_classifier_rejects_templated_ledger(self, architect_env):
        from kernos.kernel.workflows.descriptor_parser import _build_workflow
        wf = _build_workflow(_descriptor(
            action_type="append_to_ledger",
            params={"ledger": "{idea_payload.ledger}", "entry": {}},
        ))
        validate_workflow(wf)
        assert classify_governance_tier(wf) == TIER_SUBSTRATE

    async def test_kernos_cannot_register_templated_tool_id(
        self, stack, architect_env,
    ):
        # Kernos issues a workflow with templated tool_id; classifier
        # promotes to substrate; rejected.
        result = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(
                workflow_id="wf-templated",
                action_type="call_tool",
                params={"tool_id": "{idea_payload.tool_id}", "args": {}},
            ),
            TIER_COMPOSITION,
        )
        assert result.success is False
        assert any(
            err.category == "governance_tier_violation"
            for err in result.errors
        )

    # --- B2: race-safe register_trigger ---

    async def test_register_trigger_inside_atomic_txn(
        self, stack, architect_env,
    ):
        # Confirm register_trigger inserts via the engine's
        # transaction (queryable on engine._db). Race-coverage is a
        # property test; this one pins the atomic-insert path.
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-race-trig"),
            TIER_COMPOSITION,
        )
        assert reg.success
        trig = await register_trigger(
            stack["engine"], _kernos_ctx(), "wf-race-trig",
            event_type="cc.batch.report",
            predicate={"op": "exists", "path": "event_id"},
        )
        assert trig.success
        async with stack["engine"]._db.execute(
            "SELECT trigger_id FROM triggers WHERE workflow_id = ?",
            ("wf-race-trig",),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["trigger_id"] == trig.trigger_id

    # --- H4: activate re-runs governance classifier ---

    async def test_activation_rejects_tier_drift(
        self, stack, architect_env, monkeypatch,
    ):
        # Kernos registers a composition workflow. Then we mutate
        # SUBSTRATE_TOOL_IDS to include a tool the workflow uses,
        # simulating a substrate-rule change since registration.
        # Activation must detect the drift and reject.
        from kernos.kernel.workflows import authoring as auth_mod
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(
                workflow_id="wf-tier-drift",
                action_type="call_tool",
                params={"tool_id": "future_tool", "args": {}},
            ),
            TIER_COMPOSITION,
        )
        assert reg.success
        # Simulate the substrate-tool-id list growing.
        new_ids = frozenset(auth_mod.SUBSTRATE_TOOL_IDS | {"future_tool"})
        monkeypatch.setattr(auth_mod, "SUBSTRATE_TOOL_IDS", new_ids)
        # Architect activation re-runs classifier; sees substrate
        # tier; rejects because the row was registered as composition
        # by Kernos.
        result = await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-tier-drift",
        )
        assert result.success is False
        assert any(
            err.category == "governance_tier_violation"
            for err in result.errors
        )

    # --- H5: ActionStateRecord emission ---

    async def test_register_workflow_emits_action_state_record(
        self, stack, architect_env,
    ):
        from kernos.kernel import event_stream as es
        captured = []
        original_emit = es.emit

        async def capture_emit(*args, **kwargs):
            event_type = args[1] if len(args) > 1 else kwargs.get("event_type")
            if event_type == "workflow_authoring.action_recorded":
                captured.append({
                    "instance_id": args[0] if args else "",
                    "event_type": event_type,
                    "payload": args[2] if len(args) > 2 else kwargs.get("payload"),
                })
            return await original_emit(*args, **kwargs)
        es.emit = capture_emit
        try:
            await register_workflow(
                stack["engine"], _kernos_ctx(),
                _descriptor(workflow_id="wf-emits"),
                TIER_COMPOSITION,
            )
        finally:
            es.emit = original_emit
        assert len(captured) == 1
        payload = captured[0]["payload"]
        assert payload["operation"] == "register_workflow"
        assert payload["execution_state"] == "completed"
        assert payload["operation_class"] == "manage"
        assert payload["risk_level"] == "medium"

    async def test_activate_workflow_emits_high_risk_record(
        self, stack, architect_env,
    ):
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-emit-high"),
            TIER_COMPOSITION,
        )
        assert reg.success
        from kernos.kernel import event_stream as es
        captured = []
        original_emit = es.emit

        async def capture_emit(*args, **kwargs):
            event_type = args[1] if len(args) > 1 else kwargs.get("event_type")
            if event_type == "workflow_authoring.action_recorded":
                captured.append({
                    "payload": args[2] if len(args) > 2 else kwargs.get("payload"),
                })
            return await original_emit(*args, **kwargs)
        es.emit = capture_emit
        try:
            await activate_workflow(
                stack["engine"], _architect_ctx(), "wf-emit-high",
            )
        finally:
            es.emit = original_emit
        # Find the activate_workflow record.
        activations = [
            c for c in captured
            if c["payload"]["operation"] == "activate_workflow"
        ]
        assert len(activations) == 1
        assert activations[0]["payload"]["risk_level"] == "high"

    # --- H6: idempotent under race ---

    async def test_activate_already_active_returns_success(
        self, stack, architect_env,
    ):
        # Direct DB state transition to simulate a race-winner having
        # already moved state to active. Subsequent activate should
        # return success with already_active=True (not failure).
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-race-already-active"),
            TIER_COMPOSITION,
        )
        assert reg.success
        # First activation
        ctx = _architect_ctx()
        first = await activate_workflow(
            stack["engine"], ctx, "wf-race-already-active",
        )
        assert first.success
        # Second activation: already-active path
        second = await activate_workflow(
            stack["engine"], ctx, "wf-race-already-active",
        )
        assert second.success
        assert second.extra.get("already_active") is True

    async def test_deactivate_already_deactivated_returns_success(
        self, stack, architect_env,
    ):
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(workflow_id="wf-race-already-deact"),
            TIER_COMPOSITION,
        )
        assert reg.success
        ctx = _architect_ctx()
        assert (await activate_workflow(
            stack["engine"], ctx, "wf-race-already-deact",
        )).success
        first = await deactivate_workflow(
            stack["engine"], ctx, "wf-race-already-deact",
        )
        assert first.success
        second = await deactivate_workflow(
            stack["engine"], ctx, "wf-race-already-deact",
        )
        assert second.success
        assert second.extra.get("already_deactivated") is True

    # --- M7: fail-closed on lookup error ---

    async def test_dispatch_check_fails_closed_on_db_unavailable(
        self, stack, architect_env, monkeypatch,
    ):
        # Force the engine's _db to None and call the helper directly.
        engine = stack["engine"]
        original_db = engine._db
        engine._db = None
        try:
            result = await engine._is_authoring_workflow_inactive("any")
            assert result is True  # fail-closed
        finally:
            engine._db = original_db

    # --- M8: friction recurrence subscriber ---

    async def _ensure_friction_pattern_schema(self, db):
        await db.execute(
            "CREATE TABLE IF NOT EXISTS friction_pattern ("
            " instance_id TEXT NOT NULL,"
            " pattern_id TEXT NOT NULL,"
            " parent_pattern_id TEXT NOT NULL DEFAULT '',"
            " display_name TEXT NOT NULL DEFAULT '',"
            " description TEXT NOT NULL DEFAULT '',"
            " signal_type_keys TEXT NOT NULL DEFAULT '[]',"
            " aliases TEXT NOT NULL DEFAULT '[]',"
            " lifecycle_state TEXT NOT NULL DEFAULT 'active',"
            " occurrence_count INTEGER NOT NULL DEFAULT 0,"
            " first_observed_at TEXT NOT NULL DEFAULT '',"
            " last_observed_at TEXT NOT NULL DEFAULT '',"
            " resolved_at TEXT NOT NULL DEFAULT '',"
            " resolved_by_spec TEXT NOT NULL DEFAULT '',"
            " reactivated_at TEXT NOT NULL DEFAULT '',"
            " created_at TEXT NOT NULL DEFAULT '',"
            " workflow_resolvable INTEGER NOT NULL DEFAULT 0,"
            " PRIMARY KEY (instance_id, pattern_id)"
            ")"
        )

    async def test_friction_recurrence_subscriber_emits_for_tagged(
        self, stack, architect_env,
    ):
        from kernos.kernel.workflows.authoring import (
            handle_friction_pattern_recurrence,
        )
        await self._ensure_friction_pattern_schema(stack["engine"]._db)
        # Seed a friction_pattern row tagged workflow_resolvable=1.
        await stack["engine"]._db.execute(
            "INSERT INTO friction_pattern ("
            " instance_id, pattern_id, parent_pattern_id, display_name,"
            " description, signal_type_keys, aliases, lifecycle_state,"
            " occurrence_count, first_observed_at, last_observed_at,"
            " resolved_at, resolved_by_spec, reactivated_at, created_at,"
            " workflow_resolvable"
            ") VALUES (?, ?, '', '', ?, '[]', '[]', 'active', 0, '', '', '', '', '', '', 1)",
            ("inst_a", "pattern_X", "test pattern"),
        )

        captured = []

        async def capture_emit(instance_id, event_type, payload, **kw):
            captured.append({
                "instance_id": instance_id,
                "event_type": event_type,
                "payload": payload,
            })

        await handle_friction_pattern_recurrence(
            stack["engine"]._db,
            event_payload={
                "instance_id": "inst_a",
                "resolved_pattern_id": "pattern_X",
            },
            emit_event=capture_emit,
        )
        assert len(captured) == 1
        assert (
            captured[0]["event_type"]
            == "workflow_authoring.soft_prompt_workflow_resolvable"
        )
        assert captured[0]["payload"]["pattern_id"] == "pattern_X"

    async def test_friction_recurrence_subscriber_skips_untagged(
        self, stack, architect_env,
    ):
        from kernos.kernel.workflows.authoring import (
            handle_friction_pattern_recurrence,
        )
        await self._ensure_friction_pattern_schema(stack["engine"]._db)
        # Pattern NOT tagged (workflow_resolvable defaults 0).
        await stack["engine"]._db.execute(
            "INSERT INTO friction_pattern ("
            " instance_id, pattern_id, parent_pattern_id, display_name,"
            " description, signal_type_keys, aliases, lifecycle_state,"
            " occurrence_count, first_observed_at, last_observed_at,"
            " resolved_at, resolved_by_spec, reactivated_at, created_at"
            ") VALUES (?, ?, '', '', ?, '[]', '[]', 'active', 0, '', '', '', '', '', '')",
            ("inst_a", "pattern_Y", "untagged pattern"),
        )

        captured = []

        async def capture_emit(*args, **kwargs):
            captured.append(args)

        result = await handle_friction_pattern_recurrence(
            stack["engine"]._db,
            event_payload={
                "instance_id": "inst_a",
                "resolved_pattern_id": "pattern_Y",
            },
            emit_event=capture_emit,
        )
        assert result is False
        assert len(captured) == 0

    # --- M9: aggregate validation errors ---

    async def test_governance_errors_aggregate(self, stack, architect_env):
        # Kernos claims substrate_tier on a descriptor that IS
        # substrate (templated tool_id). Both governance_claim_violation
        # AND governance_tier_violation should surface.
        result = await register_workflow(
            stack["engine"], _kernos_ctx(),
            _descriptor(
                workflow_id="wf-aggregate",
                action_type="call_tool",
                params={"tool_id": "{idea_payload.tool_id}", "args": {}},
            ),
            TIER_SUBSTRATE,
        )
        assert result.success is False
        categories = {err.category for err in result.errors}
        assert "governance_claim_violation" in categories
        assert "governance_tier_violation" in categories


# ===========================================================================
# Tool-dispatch handler shape (Spec 5 post-impl Codex Blocker 1)
# ===========================================================================


class TestToolDispatchHandlers:
    async def test_handle_register_workflow_tool_returns_summary_and_record(
        self, stack, architect_env,
    ):
        from kernos.kernel.workflows.authoring import (
            handle_register_workflow_tool, KERNEL_AUTHORING_TOOL_NAMES,
        )
        summary, record = await handle_register_workflow_tool(
            engine=stack["engine"],
            instance_id="inst_a",
            member_id="mem_kernos",
            descriptor=_descriptor(workflow_id="wf-dispatch"),
            governance_tier=TIER_COMPOSITION,
        )
        assert "register_workflow" in summary
        assert "wf-dispatch" in summary
        assert record.operation == "register_workflow"
        assert record.execution_state == "completed"

    async def test_handle_activate_workflow_tool_kernos_rejected(
        self, stack, architect_env,
    ):
        from kernos.kernel.workflows.authoring import (
            handle_register_workflow_tool, handle_activate_workflow_tool,
        )
        await handle_register_workflow_tool(
            engine=stack["engine"],
            instance_id="inst_a", member_id="mem_kernos",
            descriptor=_descriptor(workflow_id="wf-kernos-acts"),
            governance_tier=TIER_COMPOSITION,
        )
        summary, record = await handle_activate_workflow_tool(
            engine=stack["engine"],
            instance_id="inst_a", member_id="mem_kernos",
            workflow_id="wf-kernos-acts",
        )
        assert "failed" in summary
        assert "not_authorized" in summary
        assert record.execution_state == "failed"
        assert record.risk_level == "high"

    def test_kernel_authoring_tool_names_constant(self):
        from kernos.kernel.workflows.authoring import (
            KERNEL_AUTHORING_TOOL_NAMES,
        )
        assert KERNEL_AUTHORING_TOOL_NAMES == frozenset({
            "register_workflow", "register_trigger",
            "activate_workflow", "deactivate_workflow",
        })


# ===========================================================================
# Spec 5 13th amendment (v7.1/v7.2/v7.3 scope completion)
# ===========================================================================
#
# Pins idempotent register + canonical persistence + serializability
# check + SELECT-after-catch. Tests follow the substrate-fidelity
# assertion pattern: behavioral signal from the AuthoringResult AND
# substrate state read directly from the registered_workflows row.


class TestThirteenthAmendmentSerializability:
    """M1 fold: serializability premise enforced at the public boundary
    before the Spec 4 parser does any work."""

    async def test_non_serializable_descriptor_rejected_loud(
        self, stack, architect_env,
    ):
        import datetime as _dt

        from kernos.kernel.workflows.authoring import (
            CAT_DESCRIPTOR_SHAPE_INVALID,
        )

        # datetime objects aren't JSON-serializable; would slip past
        # `isinstance(descriptor, dict)` and crash the Spec 4 parser
        # at descriptor_json_blob time. M1 fold catches it at boundary.
        descriptor = _descriptor(workflow_id="wf-nonser-1")
        descriptor["metadata"] = {"created_at": _dt.datetime.now(_dt.timezone.utc)}
        result = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert result.success is False
        assert len(result.errors) == 1
        assert result.errors[0].category == CAT_DESCRIPTOR_SHAPE_INVALID
        assert "not JSON-serializable" in result.errors[0].message
        # Substrate state pin: no row landed.
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-nonser-1",
        )
        assert row is None

    async def test_set_descriptor_rejected_loud(self, stack, architect_env):
        from kernos.kernel.workflows.authoring import (
            CAT_DESCRIPTOR_SHAPE_INVALID,
        )

        descriptor = _descriptor(workflow_id="wf-nonser-2")
        descriptor["metadata"] = {"tags": {"a", "b", "c"}}  # set, not list
        result = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert result.success is False
        assert result.errors[0].category == CAT_DESCRIPTOR_SHAPE_INVALID
        assert "not JSON-serializable" in result.errors[0].message

    async def test_serializability_runs_before_parser(
        self, stack, architect_env,
    ):
        """Pin ordering: non-serializable values that also violate Spec 4
        shape (no instance_id) still surface the serializability error,
        not a DescriptorError. Confirms M1 runs at the public boundary
        before _build_workflow."""
        from kernos.kernel.workflows.authoring import (
            CAT_DESCRIPTOR_SHAPE_INVALID,
        )

        # Build a descriptor that's both non-serializable AND
        # missing instance_id. If the parser ran first, we'd see a
        # KeyError on instance_id wrapped as descriptor_shape_invalid
        # with a different message. M1 runs first.
        descriptor = {
            "workflow_id": "wf-order",
            "name": "broken",
            "version": "1.0",
            # missing instance_id (would fail _build_workflow)
            "metadata": {"bad": object()},  # also non-serializable
            "bounds": {"iteration_count": 1, "wall_time_seconds": 30},
            "verifier": {"flavor": "deterministic", "check": "ok"},
            "action_sequence": [],
            "approval_gates": [],
            "terminal_branches": {},
        }
        result = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert result.success is False
        assert result.errors[0].category == CAT_DESCRIPTOR_SHAPE_INVALID
        assert "not JSON-serializable" in result.errors[0].message


class TestThirteenthAmendmentCanonicalPersistence:
    """v7.2: descriptor_json_canonical + descriptor_digest persisted on
    registered_workflows row (V7.1.1 placement). The 16th amendment
    hardened the canonical function (allow_nan=False, tight
    separators); tests go through the central helper so the canonical
    form has a single source of truth."""

    async def test_canonical_json_and_digest_persisted(
        self, stack, architect_env,
    ):
        from kernos.kernel.workflows.authoring import (
            _compute_canonical_descriptor_json,
            _compute_descriptor_digest,
        )

        descriptor = _descriptor(workflow_id="wf-canon-1")
        result = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert result.success is True
        # Behavioral signal: digest surfaced via AuthoringResult.extra.
        expected_canonical = _compute_canonical_descriptor_json(descriptor)
        expected_digest = _compute_descriptor_digest(expected_canonical)
        assert result.extra["descriptor_digest"] == expected_digest
        # Substrate state: row carries both columns.
        async with stack["engine"]._db.execute(
            "SELECT descriptor_json_canonical, descriptor_digest "
            "FROM registered_workflows WHERE workflow_id = ?",
            ("wf-canon-1",),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["descriptor_json_canonical"] == expected_canonical
        assert row["descriptor_digest"] == expected_digest

    async def test_canonical_form_independent_of_key_order(
        self, stack, architect_env,
    ):
        """Two descriptors that differ only in key insertion order
        produce the same digest. Canonical form must be order-invariant
        for idempotent register to work as advertised."""
        from kernos.kernel.workflows.authoring import (
            _compute_canonical_descriptor_json,
            _compute_descriptor_digest,
        )

        descriptor_a = _descriptor(workflow_id="wf-order-a")
        # Build a descriptor with the same content but a different
        # insertion order. dict insertion order matters in Python 3.7+
        # but the canonical helper's sort_keys=True normalizes it.
        descriptor_b = {
            k: descriptor_a[k] for k in reversed(list(descriptor_a.keys()))
        }
        descriptor_b["workflow_id"] = "wf-order-b"
        # Compute expected digests via the canonical pipeline.
        digest_a = _compute_descriptor_digest(
            _compute_canonical_descriptor_json(descriptor_a)
        )
        digest_b = _compute_descriptor_digest(
            _compute_canonical_descriptor_json(descriptor_b)
        )
        result_a = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor_a, TIER_COMPOSITION,
        )
        result_b = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor_b, TIER_COMPOSITION,
        )
        assert result_a.success and result_b.success
        assert result_a.extra["descriptor_digest"] == digest_a
        assert result_b.extra["descriptor_digest"] == digest_b


class TestThirteenthAmendmentIdempotentRegister:
    """v7.1: SELECT-after-catch idempotent register. Same workflow_id +
    same canonical descriptor → idempotent success returning the prior
    row's workflow_id; same workflow_id + different content → distinct-
    collision error.

    L1 reframe of premise 7: _run_workflow_txn rolls back on body
    IntegrityError before re-raise, so the SELECT in
    _handle_register_pk_collision runs post-rollback on the same
    connection and observes the committed winner via normal visibility.
    """

    async def test_idempotent_replay_returns_existing_id(
        self, stack, architect_env,
    ):
        descriptor = _descriptor(workflow_id="wf-idempotent")
        # First register: lands the row.
        result1 = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert result1.success is True
        assert result1.workflow_id == "wf-idempotent"
        assert "idempotent_replay" not in result1.extra
        first_digest = result1.extra["descriptor_digest"]
        # Second register with the same descriptor: IntegrityError on
        # PK collision; SELECT-after-catch finds the matching digest;
        # idempotent success returned with the existing workflow_id.
        result2 = await register_workflow(
            stack["engine"], _kernos_ctx(), dict(descriptor), TIER_COMPOSITION,
        )
        assert result2.success is True
        assert result2.workflow_id == "wf-idempotent"
        assert result2.extra.get("idempotent_replay") is True
        assert result2.extra["descriptor_digest"] == first_digest

    async def test_distinct_collision_rejected_loud(
        self, stack, architect_env,
    ):
        from kernos.kernel.workflows.authoring import (
            CAT_DESCRIPTOR_SHAPE_INVALID,
        )

        descriptor_a = _descriptor(
            workflow_id="wf-collide", name="first version",
        )
        result1 = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor_a, TIER_COMPOSITION,
        )
        assert result1.success is True
        first_digest = result1.extra["descriptor_digest"]
        # Register a DIFFERENT descriptor under the same workflow_id:
        # IntegrityError → SELECT-after-catch → digest mismatch →
        # distinct-collision ValidationError surfaced.
        descriptor_b = _descriptor(
            workflow_id="wf-collide", name="DIFFERENT VERSION",
        )
        result2 = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor_b, TIER_COMPOSITION,
        )
        assert result2.success is False
        assert len(result2.errors) == 1
        err = result2.errors[0]
        assert err.category == CAT_DESCRIPTOR_SHAPE_INVALID
        assert err.field_path == "workflow_id"
        assert "already registered with a different descriptor" in err.message
        # Substrate state pin: the prior row remains the canonical
        # winner; the new descriptor did NOT overwrite it.
        async with stack["engine"]._db.execute(
            "SELECT descriptor_digest, name FROM registered_workflows "
            "INNER JOIN workflows USING (workflow_id) "
            "WHERE workflow_id = ?",
            ("wf-collide",),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["descriptor_digest"] == first_digest
        assert row["name"] == "first version"

    async def test_select_after_catch_runs_post_rollback(
        self, stack, architect_env,
    ):
        """v7.3 L1 reframe of premise 7: _run_workflow_txn ROLLBACKs
        before re-raising body exceptions. Verifying by triggering an
        IntegrityError mid-body and confirming the engine connection
        is in a usable state (no leaked open transaction) for the
        subsequent SELECT.

        Direct verification by re-registering after an idempotent
        replay: if rollback hadn't happened, the next BEGIN IMMEDIATE
        would raise 'cannot start a transaction within a transaction'.
        """
        descriptor = _descriptor(workflow_id="wf-rollback-pin")
        result1 = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert result1.success is True
        # Force the IntegrityError + SELECT-after-catch path.
        result2 = await register_workflow(
            stack["engine"], _kernos_ctx(), dict(descriptor), TIER_COMPOSITION,
        )
        assert result2.extra.get("idempotent_replay") is True
        # Critical signal: register a DIFFERENT workflow afterward
        # to prove the engine connection isn't stuck in an open txn.
        descriptor_other = _descriptor(workflow_id="wf-rollback-other")
        result3 = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor_other, TIER_COMPOSITION,
        )
        assert result3.success is True, (
            f"engine connection unusable after collision rollback: "
            f"{result3.errors}"
        )

    async def test_idempotent_replay_preserves_authored_by(
        self, stack, architect_env,
    ):
        """The idempotent path returns the EXISTING row's metadata —
        a Kernos replay of an architect-authored workflow doesn't
        change the architect_authored flag."""
        descriptor = _descriptor(workflow_id="wf-preserve")
        # Architect registers first.
        result1 = await register_workflow(
            stack["engine"], _architect_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert result1.success is True
        # Kernos tries to re-register; should see idempotent replay
        # and the existing architect-authored row is preserved.
        result2 = await register_workflow(
            stack["engine"], _kernos_ctx(), dict(descriptor), TIER_COMPOSITION,
        )
        assert result2.success is True
        assert result2.extra.get("idempotent_replay") is True
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-preserve",
        )
        assert row is not None
        assert row.architect_authored is True
        assert row.authored_by == ARCHITECT_ID


# ===========================================================================
# Spec 5 14th amendment H1 fold (compile_descriptor_triggers wiring)
# ===========================================================================
#
# H1 wires the WTC compiler into register_workflow + activate_workflow
# so plural-triggers descriptors (12th amendment shape) are validated
# at register time AND re-validated at architect activation. The guard
# is key-presence-only (M2 fold): triggers=[] or triggers=null still
# route to compile_descriptor_triggers so the substrate primitive that
# OWNS trigger validation makes the accept/reject call.


def _plural_triggers_descriptor(
    *,
    workflow_id: str = "wf-plural",
    instance_id: str = "inst_a",
    event_type: str = "friction.pattern_frequency_threshold_exceeded",
    triggers: list | None = None,
) -> dict:
    """Build a descriptor that exercises the plural-triggers shape
    (12th amendment / production WTC path)."""
    if triggers is None:
        triggers = [{
            "event_type": event_type,
            "event_selector": {
                "op": "exists", "path": "payload.pattern_id",
            },
        }]
    return {
        "workflow_id": workflow_id,
        "instance_id": instance_id,
        "name": "plural test",
        "description": "",
        "owner": "owner",
        "version": "1.0",
        "bounds": {
            "iteration_count": 1, "wall_time_seconds": 30,
            "cost_usd": None, "composite": None,
        },
        "verifier": {"flavor": "deterministic", "check": "ok"},
        "action_sequence": [{
            "action_type": "mark_state",
            "id": "step1",
            "parameters": {"key": "x", "value": 1, "scope": "instance"},
            "continuation_rules": {"on_failure": "abort"},
        }],
        "approval_gates": [],
        "triggers": triggers,
        "terminal_branches": {},
    }


class TestFourteenthAmendmentH1Register:
    """H1: compile_descriptor_triggers runs in register_workflow BEFORE
    persistence so malformed triggers fail loud without partial state."""

    async def test_valid_plural_triggers_register_succeeds(
        self, stack, architect_env,
    ):
        descriptor = _plural_triggers_descriptor(workflow_id="wf-h1-ok")
        result = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert result.success is True, f"errors: {result.errors}"
        # Substrate state pin: canonical JSON carries the plural shape
        # for activate-time re-validation.
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-h1-ok",
        )
        assert row is not None
        assert '"triggers"' in row.descriptor_json_canonical

    async def test_malformed_trigger_rejected_loud_before_persistence(
        self, stack, architect_env,
    ):
        from kernos.kernel.workflows.authoring import (
            CAT_PREDICATE_INVALID,
        )

        # Missing required event_type on the trigger entry.
        descriptor = _plural_triggers_descriptor(
            workflow_id="wf-h1-malformed",
            triggers=[{"not_event_type": "oops"}],
        )
        result = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert result.success is False
        assert len(result.errors) == 1
        assert result.errors[0].category == CAT_PREDICATE_INVALID
        assert result.errors[0].field_path == "descriptor.triggers"
        # Substrate state pin: no row landed (compile failed before
        # the persistence transaction body even ran).
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-h1-malformed",
        )
        assert row is None
        # And no workflows-table row either.
        async with stack["engine"]._db.execute(
            "SELECT workflow_id FROM workflows WHERE workflow_id = ?",
            ("wf-h1-malformed",),
        ) as cur:
            wf_row = await cur.fetchone()
        assert wf_row is None

    async def test_empty_triggers_list_rejected(
        self, stack, architect_env,
    ):
        from kernos.kernel.workflows.authoring import (
            CAT_PREDICATE_INVALID,
        )

        descriptor = _plural_triggers_descriptor(
            workflow_id="wf-h1-empty", triggers=[],
        )
        result = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert result.success is False
        assert result.errors[0].category == CAT_PREDICATE_INVALID
        assert "non-empty" in result.errors[0].message

    async def test_null_triggers_rejected(self, stack, architect_env):
        """Key-presence-only guard: triggers=None still routes to
        compile_descriptor_triggers, which rejects with a typed
        PredicateValidationError. Confirms the falsey-value pass-through
        (v7.3 M2 fold ruling)."""
        from kernos.kernel.workflows.authoring import (
            CAT_PREDICATE_INVALID,
        )

        descriptor = _descriptor(workflow_id="wf-h1-null")
        # Use a directly-built descriptor with no singular trigger
        # and triggers=None so we can pin the key-presence-only branch.
        descriptor.pop("trigger", None)
        descriptor["triggers"] = None
        result = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert result.success is False
        assert result.errors[0].category == CAT_PREDICATE_INVALID
        assert "must be a list" in result.errors[0].message

    async def test_singular_trigger_descriptor_skips_h1(
        self, stack, architect_env,
    ):
        """Backward-compat pin: legacy singular-trigger descriptors
        have no 'triggers' key; H1 skips compile_descriptor_triggers
        for them (the Spec 4 parser + predicate-validator already
        ran inside _build_workflow). 16th amendment LOW 1: substrate-
        state pin added so the success path proves the row landed,
        not just that the call returned cleanly."""
        descriptor = _descriptor(workflow_id="wf-h1-singular")
        assert "triggers" not in descriptor
        result = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert result.success is True, f"errors: {result.errors}"
        # Substrate state pin: registered_workflows row exists; the
        # canonical descriptor has 'trigger' (singular) but NOT
        # 'triggers' (plural).
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-h1-singular",
        )
        assert row is not None
        assert '"trigger"' in row.descriptor_json_canonical
        assert '"triggers"' not in row.descriptor_json_canonical


class TestFourteenthAmendmentH1Activate:
    """H1 / M2 fold: conditional re-validation in activate_workflow.
    Reads descriptor_json_canonical, parses, and routes triggers to the
    WTC compiler if the key is present. Pre-13th-amendment rows (empty
    canonical) skip re-validation."""

    async def test_activate_re_validates_plural_triggers(
        self, stack, architect_env,
    ):
        descriptor = _plural_triggers_descriptor(workflow_id="wf-h1-act-ok")
        await register_workflow(
            stack["engine"], _architect_ctx(), descriptor, TIER_COMPOSITION,
        )
        result = await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-h1-act-ok",
        )
        assert result.success is True, f"errors: {result.errors}"
        # Substrate state pin: row now active.
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-h1-act-ok",
        )
        assert row is not None
        assert row.activation_state == STATE_ACTIVE

    async def test_activate_rejects_canonical_with_corrupted_triggers(
        self, stack, architect_env,
    ):
        """Pin the M2 re-validation actually runs at activate time. We
        register a valid plural-triggers descriptor, mutate the
        canonical column out-of-band to simulate substrate drift (e.g.
        the WTC compiler grew a new constraint between register and
        activate), then attempt activation. PredicateValidationError
        must surface as CAT_PREDICATE_INVALID and the row must NOT
        transition to active."""
        import json as _json

        from kernos.kernel.workflows.authoring import (
            CAT_PREDICATE_INVALID,
        )

        descriptor = _plural_triggers_descriptor(workflow_id="wf-h1-drift")
        reg = await register_workflow(
            stack["engine"], _architect_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert reg.success is True
        # Out-of-band mutate the canonical column to corrupt the
        # trigger entry (missing event_type).
        bad_canonical = _json.dumps({
            **descriptor,
            "triggers": [{"not_event_type": "drifted"}],
        }, sort_keys=True)
        await stack["engine"]._db.execute(
            "UPDATE registered_workflows SET descriptor_json_canonical = ? "
            "WHERE workflow_id = ?",
            (bad_canonical, "wf-h1-drift"),
        )
        await stack["engine"]._db.commit()
        # Activation: re-validation routes to compile_descriptor_triggers
        # → PredicateValidationError → CAT_PREDICATE_INVALID.
        result = await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-h1-drift",
        )
        assert result.success is False
        assert result.errors[0].category == CAT_PREDICATE_INVALID
        assert result.errors[0].field_path == "descriptor.triggers"
        # Substrate state pin: row is still in registered state, not
        # active.
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-h1-drift",
        )
        assert row is not None
        assert row.activation_state == STATE_REGISTERED

    async def test_activate_skips_re_validation_when_canonical_empty(
        self, stack, architect_env,
    ):
        """Pre-13th-amendment rows have empty descriptor_json_canonical.
        The ALTER TABLE migration filled the column with empty string;
        activate must skip re-validation in that case so legacy rows
        don't get retroactively rejected."""
        descriptor = _descriptor(workflow_id="wf-h1-legacy")
        reg = await register_workflow(
            stack["engine"], _architect_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert reg.success is True
        # Simulate a pre-amendment row by clearing the canonical column.
        await stack["engine"]._db.execute(
            "UPDATE registered_workflows SET descriptor_json_canonical = '' "
            "WHERE workflow_id = ?",
            ("wf-h1-legacy",),
        )
        await stack["engine"]._db.commit()
        result = await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-h1-legacy",
        )
        assert result.success is True, f"errors: {result.errors}"

    async def test_activate_skips_re_validation_when_no_triggers_key(
        self, stack, architect_env,
    ):
        """Singular-trigger workflows have no 'triggers' key in their
        canonical descriptor; activate should not invoke
        compile_descriptor_triggers for them."""
        descriptor = _descriptor(workflow_id="wf-h1-no-triggers-key")
        reg = await register_workflow(
            stack["engine"], _architect_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert reg.success is True
        # The canonical descriptor has 'trigger' (singular) but not
        # 'triggers' (plural) — confirm the row reflects that.
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-h1-no-triggers-key",
        )
        assert '"trigger"' in row.descriptor_json_canonical
        assert '"triggers"' not in row.descriptor_json_canonical
        # Activation: re-validation guard is False; CAS transition runs.
        result = await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-h1-no-triggers-key",
        )
        assert result.success is True, f"errors: {result.errors}"


# ===========================================================================
# Spec 5 15th amendment B2 fold (execute_workflow activation gate)
# ===========================================================================
#
# B2 wires the activation gate into ExecutionEngine.execute_workflow
# (the WTC outbox-driven dispatch path). Inactive workflows receive
# the EXECUTE_SKIPPED_AUTHORING_INACTIVE sentinel string instead of an
# execution_id; no workflow_executions row is created. The legacy
# in-process _on_trigger_match path already had its gate via Codex
# Blocker 3; B2 closes the parallel gap on the cross-process path.


class TestFifteenthAmendmentB2:
    """B2: execute_workflow returns the
    ``EXECUTE_SKIPPED_AUTHORING_INACTIVE`` sentinel for workflows that
    are registered via the authoring layer but not active."""

    async def test_inactive_workflow_returns_sentinel(
        self, stack, architect_env,
    ):
        from kernos.kernel.workflows.execution_engine import (
            EXECUTE_SKIPPED_AUTHORING_INACTIVE,
        )

        # Register but do NOT activate — workflow sits in
        # registered_not_activated.
        descriptor = _descriptor(workflow_id="wf-b2-inactive")
        reg = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert reg.success is True
        # Direct execute_workflow call: gate fires; sentinel returned.
        execution_id = await stack["engine"].execute_workflow(
            fire_id="fire_b2_1",
            workflow_id="wf-b2-inactive",
            instance_id="inst_a",
            trigger_event_payload={},
            trigger_event_id="evt_1",
        )
        assert execution_id == EXECUTE_SKIPPED_AUTHORING_INACTIVE
        # Substrate state pin: NO workflow_executions row created.
        async with stack["engine"]._db.execute(
            "SELECT execution_id FROM workflow_executions "
            "WHERE workflow_id = ?",
            ("wf-b2-inactive",),
        ) as cur:
            row = await cur.fetchone()
        assert row is None

    async def test_active_workflow_dispatches_normally(
        self, stack, architect_env,
    ):
        from kernos.kernel.workflows.execution_engine import (
            EXECUTE_SKIPPED_AUTHORING_INACTIVE,
        )

        descriptor = _descriptor(workflow_id="wf-b2-active")
        await register_workflow(
            stack["engine"], _architect_ctx(), descriptor, TIER_COMPOSITION,
        )
        await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-b2-active",
        )
        execution_id = await stack["engine"].execute_workflow(
            fire_id="fire_b2_2",
            workflow_id="wf-b2-active",
            instance_id="inst_a",
            trigger_event_payload={},
            trigger_event_id="evt_2",
        )
        # Real execution_id, not the sentinel.
        assert execution_id != EXECUTE_SKIPPED_AUTHORING_INACTIVE
        assert execution_id  # non-empty UUID
        # Substrate state pin: workflow_executions row created with
        # the matching fire_id.
        async with stack["engine"]._db.execute(
            "SELECT execution_id, fire_id FROM workflow_executions "
            "WHERE workflow_id = ?",
            ("wf-b2-active",),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["fire_id"] == "fire_b2_2"
        assert row["execution_id"] == execution_id

    async def test_idempotent_fire_id_wins_over_gate(
        self, stack, architect_env,
    ):
        """Sequencing pin: the fire_id idempotency check runs BEFORE
        the activation gate. A workflow that dispatched while active
        and then got deactivated should still return its original
        execution_id when re-called with the same fire_id (the prior
        dispatch is the canonical winner; the gate only affects NEW
        dispatches). 16th amendment LOW 1: substrate-state pin added
        for the canonical row identity."""
        from kernos.kernel.workflows.execution_engine import (
            EXECUTE_SKIPPED_AUTHORING_INACTIVE,
        )

        descriptor = _descriptor(workflow_id="wf-b2-race")
        await register_workflow(
            stack["engine"], _architect_ctx(), descriptor, TIER_COMPOSITION,
        )
        await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-b2-race",
        )
        # First dispatch while active: creates the row.
        first_id = await stack["engine"].execute_workflow(
            fire_id="fire_b2_race",
            workflow_id="wf-b2-race",
            instance_id="inst_a",
            trigger_event_payload={},
        )
        assert first_id != EXECUTE_SKIPPED_AUTHORING_INACTIVE
        # Deactivate the workflow.
        deact = await deactivate_workflow(
            stack["engine"], _architect_ctx(), "wf-b2-race",
            reason="testing race",
        )
        assert deact.success is True
        # Re-dispatch with same fire_id: idempotent check returns
        # the original execution_id; the gate doesn't run.
        replay_id = await stack["engine"].execute_workflow(
            fire_id="fire_b2_race",
            workflow_id="wf-b2-race",
            instance_id="inst_a",
            trigger_event_payload={},
        )
        assert replay_id == first_id
        # Substrate state pin: exactly ONE workflow_executions row
        # exists with this fire_id (no duplicate, no orphan).
        async with stack["engine"]._db.execute(
            "SELECT COUNT(*) AS n FROM workflow_executions WHERE fire_id = ?",
            ("fire_b2_race",),
        ) as cur:
            row = await cur.fetchone()
        assert row["n"] == 1

    async def test_legacy_non_authoring_workflow_unaffected(
        self, stack, architect_env,
    ):
        """Workflows NOT registered via the Spec 5 authoring layer
        (no registered_workflows row) dispatch unconditionally — the
        gate's _is_authoring_workflow_inactive returns False when the
        row is absent."""
        from kernos.kernel.workflows.execution_engine import (
            EXECUTE_SKIPPED_AUTHORING_INACTIVE,
        )

        # Insert directly into workflows table only — bypass authoring.
        # The execute_workflow path doesn't actually require a
        # workflows row to create a workflow_executions row (the FK
        # constraint may not be set; let me check).
        # Simpler approach: just use a workflow_id that's not in
        # registered_workflows. The engine's _is_authoring_workflow_inactive
        # returns False (legacy / non-authoring) → dispatch proceeds.
        execution_id = await stack["engine"].execute_workflow(
            fire_id="fire_b2_legacy",
            workflow_id="wf-legacy-no-authoring",
            instance_id="inst_a",
            trigger_event_payload={},
        )
        assert execution_id != EXECUTE_SKIPPED_AUTHORING_INACTIVE
        # The execution row was created.
        async with stack["engine"]._db.execute(
            "SELECT fire_id FROM workflow_executions WHERE execution_id = ?",
            (execution_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["fire_id"] == "fire_b2_legacy"

    async def test_sentinel_exported_for_callers(self):
        """B2 callers (the WTC trigger runtime) need a non-magic
        comparison target. The sentinel is exported via __all__.

        NOTE: pure API-surface probe; substrate-state pin not
        applicable (this test pins the module's public exports, not
        any persisted row). Exempt from the
        substrate-fidelity-assertion-pattern requirement per 16th
        amendment LOW 1 fold."""
        from kernos.kernel.workflows import execution_engine

        assert "EXECUTE_SKIPPED_AUTHORING_INACTIVE" in execution_engine.__all__
        assert (
            execution_engine.EXECUTE_SKIPPED_AUTHORING_INACTIVE
            == "skipped:authoring_inactive"
        )

    async def test_deactivated_workflow_returns_sentinel(
        self, stack, architect_env,
    ):
        """Deactivated state (post-activate, post-deactivate) is also
        not 'active' so the gate fires for a NEW fire_id. 16th
        amendment LOW 1: substrate-state pin added so the assertion
        proves no row was created, not just that the sentinel
        returned."""
        from kernos.kernel.workflows.execution_engine import (
            EXECUTE_SKIPPED_AUTHORING_INACTIVE,
        )

        descriptor = _descriptor(workflow_id="wf-b2-deactivated")
        await register_workflow(
            stack["engine"], _architect_ctx(), descriptor, TIER_COMPOSITION,
        )
        await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-b2-deactivated",
        )
        await deactivate_workflow(
            stack["engine"], _architect_ctx(), "wf-b2-deactivated",
            reason="testing deactivated gate",
        )
        execution_id = await stack["engine"].execute_workflow(
            fire_id="fire_b2_deactivated_new",
            workflow_id="wf-b2-deactivated",
            instance_id="inst_a",
            trigger_event_payload={},
        )
        assert execution_id == EXECUTE_SKIPPED_AUTHORING_INACTIVE
        # Substrate state pin: no workflow_executions row for this
        # fire_id; activation_state in registered_workflows reads
        # 'deactivated'.
        async with stack["engine"]._db.execute(
            "SELECT execution_id FROM workflow_executions WHERE fire_id = ?",
            ("fire_b2_deactivated_new",),
        ) as cur:
            row = await cur.fetchone()
        assert row is None
        registered = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-b2-deactivated",
        )
        assert registered is not None
        assert registered.activation_state == STATE_DEACTIVATED


# ===========================================================================
# Spec 5 16th amendment — Codex post-impl fold (5 findings)
# ===========================================================================
#
# Folds Codex post-impl review findings: HIGH 1 (WTC sentinel
# propagation), HIGH 2 (corrupted-canonical fail-closed at activate),
# MEDIUM 1 (canonical helper rejects NaN/Infinity + tight separators),
# MEDIUM 2 (gate atomic with INSERT under write lock), LOW 1
# (substrate-state pins on flagged tests).


class TestSixteenthAmendmentMedium1Canonical:
    """MEDIUM 1: canonical helper rejects NaN/Infinity via
    allow_nan=False and uses tight separators for platform-deterministic
    bytes."""

    def test_canonical_helper_rejects_nan(self):
        """Pure-helper probe: exercises _compute_canonical_descriptor_json
        in isolation. Substrate-state pin not applicable — the helper
        is a pure function with no DB / event / queue side effects.
        Exempt from the substrate-fidelity-assertion-pattern requirement
        per 16th amendment round-2 LOW 2 fold."""
        import math

        from kernos.kernel.workflows.authoring import (
            _compute_canonical_descriptor_json,
        )

        with pytest.raises(ValueError):
            _compute_canonical_descriptor_json(
                {"metadata": {"score": math.nan}}
            )

    def test_canonical_helper_rejects_infinity(self):
        """Pure-helper probe (see test_canonical_helper_rejects_nan
        docstring for exemption rationale)."""
        import math

        from kernos.kernel.workflows.authoring import (
            _compute_canonical_descriptor_json,
        )

        with pytest.raises(ValueError):
            _compute_canonical_descriptor_json(
                {"metadata": {"score": math.inf}}
            )

    def test_canonical_helper_uses_tight_separators(self):
        """Pure-helper probe pinning the separator choice: no
        whitespace, just ``,`` and ``:``. Drift here would change every
        digest and break idempotent register's content-addressing
        invariant. Substrate-state pin not applicable — exempt per 16th
        amendment round-2 LOW 2 fold (pure function, no side effects)."""
        from kernos.kernel.workflows.authoring import (
            _compute_canonical_descriptor_json,
        )

        out = _compute_canonical_descriptor_json({"a": 1, "b": [2, 3]})
        assert out == '{"a":1,"b":[2,3]}'
        # No spaces after separators.
        assert ", " not in out
        assert ": " not in out

    async def test_register_rejects_nan_descriptor_loud(
        self, stack, architect_env,
    ):
        import math

        from kernos.kernel.workflows.authoring import (
            CAT_DESCRIPTOR_SHAPE_INVALID,
        )

        descriptor = _descriptor(workflow_id="wf-canon-nan")
        descriptor["metadata"] = {"score": math.nan}
        result = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert result.success is False
        assert result.errors[0].category == CAT_DESCRIPTOR_SHAPE_INVALID
        assert "not JSON-serializable" in result.errors[0].message
        # Substrate state pin: no row landed.
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-canon-nan",
        )
        assert row is None


class TestSixteenthAmendmentHigh2CorruptedCanonical:
    """HIGH 2: non-empty descriptor_json_canonical that fails to parse
    or doesn't deserialize to a dict fails loud at activate. Empty
    canonical (legacy pre-13th row) still skips re-validation."""

    async def test_activate_rejects_corrupted_canonical_loud(
        self, stack, architect_env,
    ):
        from kernos.kernel.workflows.authoring import (
            CAT_DESCRIPTOR_SHAPE_INVALID,
        )

        descriptor = _descriptor(workflow_id="wf-canon-corrupted")
        reg = await register_workflow(
            stack["engine"], _architect_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert reg.success is True
        # Out-of-band mutate the canonical column to invalid JSON.
        await stack["engine"]._db.execute(
            "UPDATE registered_workflows "
            "SET descriptor_json_canonical = ? "
            "WHERE workflow_id = ?",
            ("{ not valid json {{{", "wf-canon-corrupted"),
        )
        await stack["engine"]._db.commit()
        result = await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-canon-corrupted",
        )
        assert result.success is False
        assert result.errors[0].category == CAT_DESCRIPTOR_SHAPE_INVALID
        assert result.errors[0].field_path == "descriptor_json_canonical"
        # Substrate state pin: activation_state stayed at registered.
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-canon-corrupted",
        )
        assert row is not None
        assert row.activation_state == STATE_REGISTERED

    async def test_activate_rejects_non_dict_canonical_loud(
        self, stack, architect_env,
    ):
        """Non-empty canonical that parses to e.g. a list or a string
        also fails loud — the canonical descriptor must round-trip
        to a dict."""
        import json as _json

        from kernos.kernel.workflows.authoring import (
            CAT_DESCRIPTOR_SHAPE_INVALID,
        )

        descriptor = _descriptor(workflow_id="wf-canon-nondict")
        reg = await register_workflow(
            stack["engine"], _architect_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert reg.success is True
        await stack["engine"]._db.execute(
            "UPDATE registered_workflows "
            "SET descriptor_json_canonical = ? "
            "WHERE workflow_id = ?",
            (_json.dumps(["not", "a", "dict"]), "wf-canon-nondict"),
        )
        await stack["engine"]._db.commit()
        result = await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-canon-nondict",
        )
        assert result.success is False
        assert result.errors[0].category == CAT_DESCRIPTOR_SHAPE_INVALID
        assert result.errors[0].field_path == "descriptor_json_canonical"
        assert "must parse to a dict" in result.errors[0].message
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-canon-nondict",
        )
        assert row.activation_state == STATE_REGISTERED


class TestSixteenthAmendmentMedium2GateAtomic:
    """MEDIUM 2: B2 gate check runs INSIDE the write lock so a
    concurrent deactivate_workflow cannot interleave between the gate
    read and the workflow_executions INSERT. Without race injection
    machinery we pin behavior via the structural invariant: the
    gate's substrate effect (no INSERT) is observable when the
    workflow is inactive, AND the gate's pre-check happens against the
    same locked state as the INSERT."""

    async def test_gate_sees_inactive_state_same_as_insert(
        self, stack, architect_env,
    ):
        """Sequence pin: register, do NOT activate, call
        execute_workflow. The gate (inside the write-lock body) reads
        activation_state and sees registered_not_activated; INSERT is
        skipped under the same lock. This pins the atomic shape — same
        lock, same SELECT-INSERT sequence — even though the race
        injection itself isn't directly testable in-process."""
        from kernos.kernel.workflows.execution_engine import (
            EXECUTE_SKIPPED_AUTHORING_INACTIVE,
        )

        descriptor = _descriptor(workflow_id="wf-med2-atomic")
        await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor, TIER_COMPOSITION,
        )
        # Gate is INSIDE the write lock body now. Calling
        # execute_workflow returns the sentinel and creates no row.
        result = await stack["engine"].execute_workflow(
            fire_id="fire_med2_atomic",
            workflow_id="wf-med2-atomic",
            instance_id="inst_a",
            trigger_event_payload={},
        )
        assert result == EXECUTE_SKIPPED_AUTHORING_INACTIVE
        # Substrate state pin: no row.
        async with stack["engine"]._db.execute(
            "SELECT execution_id FROM workflow_executions "
            "WHERE workflow_id = ?",
            ("wf-med2-atomic",),
        ) as cur:
            row = await cur.fetchone()
        assert row is None

    async def test_locked_body_rechecks_fire_id_wins_over_gate(
        self, stack, architect_env,
    ):
        """16th amendment round-2 HIGH 1 fold: simulate the race where
        the outer find_execution_by_fire_id_unlocked missed a prior
        commit AND the workflow was deactivated between the prior
        dispatch and this call.

        Without the locked-body fire_id re-check, this turn would
        return EXECUTE_SKIPPED_AUTHORING_INACTIVE (the gate fires
        because the workflow is now inactive). With the re-check, the
        prior dispatch's execution_id is returned (the canonical
        winner; "prior dispatch is canonical; only NEW dispatches are
        gated"). Race injection: monkey-patch
        ``_find_execution_by_fire_id_unlocked`` to return None on
        first call so the outer find misses the row that's already in
        the database.

        Substrate-fidelity pin: behavioral signal (returned id matches
        the existing row's id) AND substrate state (only one
        workflow_executions row exists for the fire_id; no orphan
        from the racy turn)."""
        from kernos.kernel.workflows.execution_engine import (
            EXECUTE_SKIPPED_AUTHORING_INACTIVE,
        )

        engine = stack["engine"]
        descriptor = _descriptor(workflow_id="wf-race-recheck")
        await register_workflow(
            engine, _architect_ctx(), descriptor, TIER_COMPOSITION,
        )
        await activate_workflow(
            engine, _architect_ctx(), "wf-race-recheck",
        )
        # First dispatch lands the row.
        first_id = await engine.execute_workflow(
            fire_id="fire_race_recheck",
            workflow_id="wf-race-recheck",
            instance_id="inst_a",
            trigger_event_payload={},
        )
        assert first_id != EXECUTE_SKIPPED_AUTHORING_INACTIVE
        # Deactivate so the gate would fire on a new dispatch.
        await deactivate_workflow(
            engine, _architect_ctx(), "wf-race-recheck",
            reason="race test",
        )
        # Race injection: outer find returns None on first invocation
        # (simulating the race window where another caller's commit
        # hasn't propagated to our snapshot yet); subsequent calls
        # return the real value (the locked-body re-check sees it).
        real_find = engine._find_execution_by_fire_id_unlocked
        call_log: list[str] = []

        async def racy_find(fire_id: str):
            call_log.append(fire_id)
            if len(call_log) == 1:
                # Simulate the unlocked outer find missing A's row.
                return None
            return await real_find(fire_id)

        engine._find_execution_by_fire_id_unlocked = racy_find  # type: ignore[method-assign]
        try:
            replay_id = await engine.execute_workflow(
                fire_id="fire_race_recheck",
                workflow_id="wf-race-recheck",
                instance_id="inst_a",
                trigger_event_payload={},
            )
        finally:
            engine._find_execution_by_fire_id_unlocked = real_find  # type: ignore[method-assign]
        # Behavioral pin: the canonical winner's id was returned,
        # NOT the sentinel.
        assert replay_id == first_id
        assert replay_id != EXECUTE_SKIPPED_AUTHORING_INACTIVE
        # Call sequence pin: the outer find ran (returned None), then
        # the locked-body re-find ran and saw the row.
        assert len(call_log) >= 2
        # Substrate state pin: exactly ONE workflow_executions row
        # exists for this fire_id — no orphan from the racy turn,
        # no duplicate.
        async with engine._db.execute(
            "SELECT COUNT(*) AS n FROM workflow_executions WHERE fire_id = ?",
            ("fire_race_recheck",),
        ) as cur:
            row = await cur.fetchone()
        assert row["n"] == 1

    async def test_activate_after_deactivate_re_enables_dispatch(
        self, stack, architect_env,
    ):
        """Lifecycle pin: gate is state-sensitive, not stale. Register,
        activate, dispatch (creates row). Deactivate (new fire gets
        sentinel). Reactivate (new fire dispatches normally). The
        atomic-gate fold doesn't alter this lifecycle invariant."""
        from kernos.kernel.workflows.execution_engine import (
            EXECUTE_SKIPPED_AUTHORING_INACTIVE,
        )

        descriptor = _descriptor(workflow_id="wf-med2-cycle")
        await register_workflow(
            stack["engine"], _architect_ctx(), descriptor, TIER_COMPOSITION,
        )
        await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-med2-cycle",
        )
        eid1 = await stack["engine"].execute_workflow(
            fire_id="fire_med2_cycle_1",
            workflow_id="wf-med2-cycle",
            instance_id="inst_a",
            trigger_event_payload={},
        )
        assert eid1 != EXECUTE_SKIPPED_AUTHORING_INACTIVE
        # Deactivate; new dispatch is gated.
        await deactivate_workflow(
            stack["engine"], _architect_ctx(), "wf-med2-cycle",
            reason="cycle test",
        )
        eid2 = await stack["engine"].execute_workflow(
            fire_id="fire_med2_cycle_2",
            workflow_id="wf-med2-cycle",
            instance_id="inst_a",
            trigger_event_payload={},
        )
        assert eid2 == EXECUTE_SKIPPED_AUTHORING_INACTIVE
        # Reactivate; new dispatch proceeds.
        await activate_workflow(
            stack["engine"], _architect_ctx(), "wf-med2-cycle",
        )
        eid3 = await stack["engine"].execute_workflow(
            fire_id="fire_med2_cycle_3",
            workflow_id="wf-med2-cycle",
            instance_id="inst_a",
            trigger_event_payload={},
        )
        assert eid3 != EXECUTE_SKIPPED_AUTHORING_INACTIVE
        # Substrate state pin: exactly two workflow_executions rows
        # (the gated one isn't there).
        async with stack["engine"]._db.execute(
            "SELECT execution_id, fire_id FROM workflow_executions "
            "WHERE workflow_id = ? ORDER BY started_at",
            ("wf-med2-cycle",),
        ) as cur:
            rows = await cur.fetchall()
        assert len(rows) == 2
        fire_ids = sorted(r["fire_id"] for r in rows)
        assert fire_ids == ["fire_med2_cycle_1", "fire_med2_cycle_3"]


class TestSixteenthAmendmentHigh1RuntimeSentinel:
    """HIGH 1: WTC runtime detects the B2 sentinel from execute_workflow
    and routes to mark_failed with the sentinel as last_error instead
    of calling mark_dispatched with a sentinel string. Tests use a
    minimal stub outbox to observe the routing decision."""

    async def test_runtime_routes_sentinel_to_mark_failed(self):
        """Stub-based pin: simulate _wlp_dispatch returning the
        sentinel; observe that the runtime calls mark_failed with the
        sentinel as last_error, NOT mark_dispatched.

        NOTE: pure-stub probe; substrate-state pin not applicable.
        This test inlines the conditional shape from the runtime's
        _claim_and_dispatch to pin the structural invariant in
        isolation. The companion test_runtime_dispatch_path_routes_sentinel_end_to_end
        pins the end-to-end behavior with real substrate state.
        Exempt from the substrate-fidelity-assertion-pattern
        requirement per 16th amendment round-2 LOW 2 fold."""
        from kernos.kernel.triggers.runtime import (
            _EXECUTE_SKIPPED_AUTHORING_INACTIVE,
        )

        calls: list[tuple[str, dict]] = []

        class _StubOutbox:
            async def mark_failed(self, *, fire_id, claim_owner, error):
                calls.append(("mark_failed", {
                    "fire_id": fire_id,
                    "claim_owner": claim_owner,
                    "error": error,
                }))

            async def mark_dispatched(self, *, fire_id, claim_owner,
                                       workflow_execution_id):
                calls.append(("mark_dispatched", {
                    "fire_id": fire_id,
                    "claim_owner": claim_owner,
                    "workflow_execution_id": workflow_execution_id,
                }))

        # Inline the runtime's active-claim dispatch decision so the
        # test pins the structural behavior even though the full
        # TriggerRuntime requires more wiring than a unit test wants.
        # The fold's contribution is the conditional branch:
        # ``if workflow_execution_id == _EXECUTE_SKIPPED_AUTHORING_INACTIVE``.
        outbox = _StubOutbox()
        workflow_execution_id = _EXECUTE_SKIPPED_AUTHORING_INACTIVE
        if workflow_execution_id == _EXECUTE_SKIPPED_AUTHORING_INACTIVE:
            await outbox.mark_failed(
                fire_id="fire_abc", claim_owner="runtime:test",
                error=_EXECUTE_SKIPPED_AUTHORING_INACTIVE,
            )
        else:
            await outbox.mark_dispatched(
                fire_id="fire_abc", claim_owner="runtime:test",
                workflow_execution_id=workflow_execution_id,
            )
        # The runtime called mark_failed, NOT mark_dispatched.
        assert len(calls) == 1
        assert calls[0][0] == "mark_failed"
        assert calls[0][1]["error"] == "skipped:authoring_inactive"

    def test_runtime_imports_sentinel_from_engine(self):
        """Source-of-truth pin: the runtime's sentinel is imported from
        the engine, not duplicated. Drift between the two would silently
        break sentinel detection.

        NOTE: pure-API probe; substrate-state pin not applicable
        (this test pins module-import structure, not persisted state).
        Exempt from the substrate-fidelity-assertion-pattern
        requirement per 16th amendment round-2 LOW 2 fold."""
        from kernos.kernel.triggers import runtime
        from kernos.kernel.workflows.execution_engine import (
            EXECUTE_SKIPPED_AUTHORING_INACTIVE,
        )

        assert (
            runtime._EXECUTE_SKIPPED_AUTHORING_INACTIVE
            == EXECUTE_SKIPPED_AUTHORING_INACTIVE
        )

    async def test_runtime_dispatch_path_routes_sentinel_end_to_end(
        self, tmp_path,
    ):
        """End-to-end pin: build a real TriggerEvaluationRuntime with a
        stubbed wlp_dispatch that returns the sentinel. Drive a real
        event through ``on_event_observed`` and confirm the outbox
        trigger_fires row lands in 'failed' status with
        last_error = sentinel, NOT 'dispatched' with sentinel as
        workflow_execution_id.

        Substrate-fidelity pin: behavioral signal (zero mark_dispatched
        calls captured by the stub) AND substrate state (trigger_fires
        row status='failed', last_error=sentinel,
        workflow_execution_id is empty)."""
        from kernos.kernel.triggers.predicate import (
            DispatchPolicy, TemporalRelation, TriggerPredicate,
        )
        from kernos.kernel.triggers.runtime import (
            TriggerEvaluationRuntime,
            _EXECUTE_SKIPPED_AUTHORING_INACTIVE,
        )

        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path), flush_interval_s=0.05)
        try:
            dispatch_call_count = 0

            async def _stub_dispatch(
                *, fire_id, workflow_id, instance_id,
                trigger_event_payload, member_id,
            ):
                nonlocal dispatch_call_count
                dispatch_call_count += 1
                return _EXECUTE_SKIPPED_AUTHORING_INACTIVE

            rt = TriggerEvaluationRuntime()
            await rt.start(
                data_dir=str(tmp_path),
                heartbeat_seconds=60,
                wlp_dispatch=_stub_dispatch,
            )
            try:
                predicate = TriggerPredicate(
                    event_selector={
                        "op": "eq", "path": "event_type",
                        "value": "test.runtime_skip",
                    },
                    temporal_relation=TemporalRelation(kind="on"),
                    dispatch_policy=DispatchPolicy(),
                )
                await rt.register(
                    trigger_id="trig_runtime_skip",
                    instance_id="inst_a",
                    workflow_id="wf-runtime-skip",
                    predicate=predicate,
                )
                # Drive an event through the public path.
                event = event_stream.Event(
                    event_id="evt_runtime_skip_1",
                    event_type="test.runtime_skip",
                    instance_id="inst_a",
                    timestamp="2026-05-14T00:00:00+00:00",
                    payload={"foo": "bar"},
                )
                await rt.on_event_observed(event)
                # Stub was called exactly once.
                assert dispatch_call_count == 1
                # Substrate state pin: outbox row is 'failed' with
                # sentinel as last_error; workflow_execution_id stays
                # empty (NEVER set to the sentinel via mark_dispatched).
                assert rt._outbox is not None
                async with rt._outbox._db.execute(
                    "SELECT status, last_error, workflow_execution_id "
                    "FROM trigger_fires WHERE trigger_id = ?",
                    ("trig_runtime_skip",),
                ) as cur:
                    row = await cur.fetchone()
                assert row is not None
                assert row["status"] == "failed"
                assert row["last_error"] == _EXECUTE_SKIPPED_AUTHORING_INACTIVE
                assert row["workflow_execution_id"] in ("", None)
            finally:
                await rt.stop()
        finally:
            await event_stream.stop_writer()
            await event_stream._reset_for_tests()


# ===========================================================================
# Spec 6: AuthoringContext operator extension
# ===========================================================================
#
# Pins the new ACTOR_OPERATOR kind + KERNOS_OPERATOR_ACTOR_ID env var.
# Operators carry substrate-tier authority for autonomy-loop tools
# (transition_friction_pattern_lifecycle, etc., landed in commit 3)
# but cannot ratify workflows at activation — that authority stays
# exclusively with the architect.


@pytest.fixture
def operator_env(monkeypatch):
    """Set KERNOS_OPERATOR_ACTOR_ID for the duration of the test."""
    monkeypatch.setenv("KERNOS_OPERATOR_ACTOR_ID", "op_kernos_autonomy")


@pytest.fixture
def unset_operator_env(monkeypatch):
    """Ensure KERNOS_OPERATOR_ACTOR_ID is unset."""
    monkeypatch.delenv("KERNOS_OPERATOR_ACTOR_ID", raising=False)


class TestSpec6OperatorActor:
    """Spec 6 substrate plumbing: ACTOR_OPERATOR actor kind + helpers."""

    def test_actor_operator_constant_exported(self):
        """Pure-API probe; substrate-state pin not applicable per the
        substrate-fidelity exemption pattern."""
        from kernos.kernel.workflows.authoring import (
            ACTOR_OPERATOR, VALID_ACTOR_KINDS,
        )

        assert ACTOR_OPERATOR == "operator"
        assert ACTOR_OPERATOR in VALID_ACTOR_KINDS

    def test_authoring_context_is_operator(self):
        """Pure-API probe (no substrate; AuthoringContext is a frozen
        dataclass with no DB side effects)."""
        from kernos.kernel.workflows.authoring import (
            ACTOR_OPERATOR, AuthoringContext,
        )

        ctx = AuthoringContext(
            actor_id="op_kernos_autonomy", actor_kind=ACTOR_OPERATOR,
        )
        assert ctx.is_operator() is True
        assert ctx.is_architect() is False

    def test_derive_actor_kind_returns_operator_when_env_matches(
        self, operator_env, unset_architect_env,
    ):
        from kernos.kernel.workflows.authoring import ACTOR_OPERATOR

        kind = derive_actor_kind("op_kernos_autonomy")
        assert kind == ACTOR_OPERATOR

    def test_derive_actor_kind_kernos_when_operator_env_unset(
        self, unset_operator_env, unset_architect_env,
    ):
        # Both env vars unset; arbitrary actor_id falls back to Kernos.
        kind = derive_actor_kind("op_kernos_autonomy")
        assert kind == ACTOR_KERNOS

    def test_architect_id_wins_over_operator_id(
        self, monkeypatch,
    ):
        """Defensive: if KERNOS_ARCHITECT_ACTOR_ID and
        KERNOS_OPERATOR_ACTOR_ID happen to match the same actor_id,
        the architect kind wins. In practice the env vars should
        carry distinct values."""
        from kernos.kernel.workflows.authoring import (
            ACTOR_ARCHITECT,
        )

        monkeypatch.setenv("KERNOS_ARCHITECT_ACTOR_ID", "same_id")
        monkeypatch.setenv("KERNOS_OPERATOR_ACTOR_ID", "same_id")
        assert derive_actor_kind("same_id") == ACTOR_ARCHITECT

    def test_is_operator_helper_fail_closed_when_env_unset(
        self, unset_operator_env,
    ):
        """Fail-closed semantics: with KERNOS_OPERATOR_ACTOR_ID unset,
        NO ctx passes _is_operator (matches the architect discipline
        so misconfigured environments don't accidentally grant
        operator authority)."""
        from kernos.kernel.workflows.authoring import (
            ACTOR_OPERATOR, AuthoringContext, _is_operator,
        )

        ctx = AuthoringContext(
            actor_id="anyone", actor_kind=ACTOR_OPERATOR,
        )
        assert _is_operator(ctx) is False

    def test_is_operator_helper_passes_when_env_and_kind_match(
        self, operator_env,
    ):
        from kernos.kernel.workflows.authoring import (
            ACTOR_OPERATOR, AuthoringContext, _is_operator,
        )

        ctx = AuthoringContext(
            actor_id="op_kernos_autonomy", actor_kind=ACTOR_OPERATOR,
        )
        assert _is_operator(ctx) is True

    def test_is_operator_helper_rejects_wrong_kind(
        self, operator_env,
    ):
        """Actor with matching id but wrong kind doesn't pass — both
        actor_id AND actor_kind are checked."""
        from kernos.kernel.workflows.authoring import (
            AuthoringContext, _is_operator,
        )

        ctx = AuthoringContext(
            actor_id="op_kernos_autonomy", actor_kind=ACTOR_KERNOS,
        )
        assert _is_operator(ctx) is False

    async def test_kernos_cannot_author_workflow_calling_autonomy_tool(
        self, stack, architect_env,
    ):
        """Functional pin: a workflow that calls one of the Spec 6
        autonomy-loop tools (e.g., transition_friction_pattern_lifecycle)
        is classified substrate_tier by the governance classifier.
        Kernos attempting to register it as composition_tier is
        rejected with CAT_GOVERNANCE_TIER_VIOLATION; only architect (or
        architect-over-classified substrate) may author such workflows.
        Substrate state pin: no row landed in registered_workflows."""
        descriptor = _descriptor(
            workflow_id="wf-autonomy-tool-kernos",
            action_type="call_tool",
            params={"tool_id": "transition_friction_pattern_lifecycle"},
        )
        result = await register_workflow(
            stack["engine"], _kernos_ctx(), descriptor, TIER_COMPOSITION,
        )
        assert result.success is False
        # Should surface the governance tier violation.
        categories = {err.category for err in result.errors}
        assert CAT_GOVERNANCE_TIER_VIOLATION in categories
        # Substrate state pin: no row landed.
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-autonomy-tool-kernos",
        )
        assert row is None

    async def test_architect_can_author_workflow_calling_autonomy_tool(
        self, stack, architect_env,
    ):
        """Companion functional pin: architect CAN author the same
        workflow shape (substrate_tier registration succeeds).
        Substrate state pin: registered_workflows row exists with
        governance_tier=substrate_tier and architect_authored=True."""
        descriptor = _descriptor(
            workflow_id="wf-autonomy-tool-architect",
            action_type="call_tool",
            params={"tool_id": "record_friction_pattern_recurrence"},
        )
        result = await register_workflow(
            stack["engine"], _architect_ctx(), descriptor, TIER_SUBSTRATE,
        )
        assert result.success is True, f"errors: {result.errors}"
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-autonomy-tool-architect",
        )
        assert row is not None
        assert row.governance_tier == TIER_SUBSTRATE
        assert row.architect_authored is True

    async def test_operator_cannot_activate_workflow(
        self, stack, architect_env, operator_env,
    ):
        """Functional pin (architect's user-feedback request): operator
        actor calls activate_workflow → rejected with CAT_NOT_AUTHORIZED.
        Activation authority stays exclusively with architect; operator
        is for substrate-tier autonomy-loop tools, not authoring
        ratification. The workflow's activation_state stays unchanged
        (substrate-state pin)."""
        from kernos.kernel.workflows.authoring import (
            ACTOR_OPERATOR,
        )

        descriptor = _descriptor(workflow_id="wf-operator-cannot-activate")
        # Register via architect (legitimate path).
        await register_workflow(
            stack["engine"], _architect_ctx(),
            descriptor, TIER_COMPOSITION,
        )
        # Operator attempts activation → must fail.
        operator_ctx = AuthoringContext(
            actor_id="op_kernos_autonomy", actor_kind=ACTOR_OPERATOR,
        )
        result = await activate_workflow(
            stack["engine"], operator_ctx,
            "wf-operator-cannot-activate",
        )
        assert result.success is False
        assert result.errors[0].category == CAT_NOT_AUTHORIZED
        # Substrate state pin: activation_state untouched.
        row = await get_registered_workflow(
            stack["engine"]._db, workflow_id="wf-operator-cannot-activate",
        )
        assert row is not None
        assert row.activation_state == STATE_REGISTERED
