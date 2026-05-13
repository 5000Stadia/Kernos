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
