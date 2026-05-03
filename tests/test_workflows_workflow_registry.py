"""Tests for the workflow registry.

WORKFLOW-LOOP-PRIMITIVE C3. Pins the dataclass shape, the
validation-time invariants (bounds required, verifier required,
gate_ref resolution, safe-deny on auto_proceed_with_default),
atomic cross-table registration, and multi-tenancy.
"""
from __future__ import annotations

import pytest

from kernos.kernel import event_stream
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
    validate_workflow,
)


@pytest.fixture
async def registries(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    trig = TriggerRegistry()
    await trig.start(str(tmp_path))
    wf = WorkflowRegistry()
    await wf.start(str(tmp_path), trig)
    yield trig, wf
    await wf.stop()
    await _reset_trigger_registry(trig)
    await event_stream._reset_for_tests()


def _basic_action(action_type="mark_state", gate_ref=None, **params) -> ActionDescriptor:
    return ActionDescriptor(
        action_type=action_type,
        parameters=params,
        gate_ref=gate_ref,
        continuation_rules=ContinuationRules(on_failure="abort"),
    )


def _basic_workflow(**overrides) -> Workflow:
    base = dict(
        workflow_id="wf-test",
        instance_id="inst_a",
        name="test workflow",
        description="",
        owner="owner",
        version="1.0",
        bounds=Bounds(iteration_count=1, wall_time_seconds=30),
        verifier=Verifier(flavor="deterministic", check="state_set"),
        action_sequence=[_basic_action("mark_state", key="x", value=1, scope="instance")],
        approval_gates=[],
        trigger=TriggerDescriptor(
            event_type="cc.batch.report",
            predicate={"op": "eq", "path": "payload.kind", "value": "report"},
        ),
        metadata={},
    )
    base.update(overrides)
    return Workflow(**base)


# ===========================================================================
# Validation
# ===========================================================================


class TestBoundsRequired:
    def test_workflow_without_bounds_rejected(self):
        wf = _basic_workflow(bounds=Bounds())
        with pytest.raises(WorkflowError, match="bounds is required"):
            validate_workflow(wf)

    def test_only_iteration_count_is_enough(self):
        wf = _basic_workflow(bounds=Bounds(iteration_count=5))
        validate_workflow(wf)  # no raise

    def test_only_wall_time_is_enough(self):
        wf = _basic_workflow(bounds=Bounds(wall_time_seconds=60))
        validate_workflow(wf)


class TestVerifierRequired:
    def test_workflow_without_verifier_rejected(self):
        wf = _basic_workflow()
        wf.verifier = None  # type: ignore[assignment]
        with pytest.raises(WorkflowError, match="verifier is required"):
            validate_workflow(wf)

    def test_unknown_flavor_rejected(self):
        wf = _basic_workflow(verifier=Verifier(flavor="vibes", check="x"))
        with pytest.raises(WorkflowError, match="verifier.flavor"):
            validate_workflow(wf)


class TestGateReferenceResolution:
    def test_gate_ref_to_undeclared_gate_rejected(self):
        wf = _basic_workflow(
            action_sequence=[_basic_action(gate_ref="missing_gate")],
            approval_gates=[],
        )
        with pytest.raises(WorkflowError, match="gate_ref"):
            validate_workflow(wf)

    def test_gate_ref_to_declared_gate_accepted(self):
        gate = ApprovalGate(
            gate_name="g1",
            pause_reason="approve please",
            approval_event_type="user.approval",
            approval_event_predicate={"op": "actor_eq", "value": "owner"},
            timeout_seconds=300,
            bound_behavior_on_timeout="abort_workflow",
        )
        wf = _basic_workflow(
            action_sequence=[_basic_action(gate_ref="g1")],
            approval_gates=[gate],
        )
        validate_workflow(wf)


class TestApprovalGateValidation:
    def test_auto_proceed_without_default_rejected(self):
        gate = ApprovalGate(
            gate_name="g1",
            pause_reason="x",
            approval_event_type="user.approval",
            approval_event_predicate={"op": "actor_eq", "value": "owner"},
            timeout_seconds=300,
            bound_behavior_on_timeout="auto_proceed_with_default",
            default_value=None,
        )
        wf = _basic_workflow(
            action_sequence=[_basic_action(gate_ref="g1")],
            approval_gates=[gate],
        )
        with pytest.raises(WorkflowError, match="default_value"):
            validate_workflow(wf)

    def test_unknown_timeout_behavior_rejected(self):
        gate = ApprovalGate(
            gate_name="g1",
            pause_reason="x",
            approval_event_type="user.approval",
            approval_event_predicate={"op": "actor_eq", "value": "owner"},
            timeout_seconds=300,
            bound_behavior_on_timeout="ride_it_out",
        )
        wf = _basic_workflow(approval_gates=[gate])
        with pytest.raises(WorkflowError, match="bound_behavior_on_timeout"):
            validate_workflow(wf)

    def test_duplicate_gate_names_rejected(self):
        g = ApprovalGate(
            gate_name="g1", pause_reason="", approval_event_type="x",
            approval_event_predicate={"op": "exists", "path": "event_id"},
            timeout_seconds=10, bound_behavior_on_timeout="abort_workflow",
        )
        wf = _basic_workflow(approval_gates=[g, g])
        with pytest.raises(WorkflowError, match="duplicate"):
            validate_workflow(wf)


class TestSafeDenyOnAutoProceed:
    """A gate with auto_proceed_with_default cannot be followed by an
    irreversible action before the next gate or end."""

    def _gate(self, name: str, *, auto_proceed=False) -> ApprovalGate:
        return ApprovalGate(
            gate_name=name,
            pause_reason="x",
            approval_event_type="user.approval",
            approval_event_predicate={"op": "actor_eq", "value": "owner"},
            timeout_seconds=300,
            bound_behavior_on_timeout=(
                "auto_proceed_with_default" if auto_proceed else "abort_workflow"
            ),
            default_value="ok" if auto_proceed else None,
        )

    def test_auto_proceed_followed_by_notify_rejected(self):
        gate = self._gate("g1", auto_proceed=True)
        wf = _basic_workflow(
            approval_gates=[gate],
            action_sequence=[
                _basic_action("mark_state", gate_ref="g1", key="x", value=1, scope="instance"),
                _basic_action("notify_user", channel="primary", message="hi", urgency="low"),
            ],
        )
        with pytest.raises(WorkflowError, match="auto_proceed_with_default"):
            validate_workflow(wf)

    def test_auto_proceed_followed_by_replace_canvas_rejected(self):
        gate = self._gate("g1", auto_proceed=True)
        wf = _basic_workflow(
            approval_gates=[gate],
            action_sequence=[
                _basic_action("mark_state", gate_ref="g1", key="x", value=1, scope="instance"),
                _basic_action("write_canvas", canvas_id="c1", content="...",
                              append_or_replace="replace"),
            ],
        )
        with pytest.raises(WorkflowError, match="auto_proceed_with_default"):
            validate_workflow(wf)

    def test_auto_proceed_followed_by_append_canvas_accepted(self):
        gate = self._gate("g1", auto_proceed=True)
        wf = _basic_workflow(
            approval_gates=[gate],
            action_sequence=[
                _basic_action("mark_state", gate_ref="g1", key="x", value=1, scope="instance"),
                _basic_action("write_canvas", canvas_id="c1", content="...",
                              append_or_replace="append"),
            ],
        )
        validate_workflow(wf)  # no raise — append is reversible

    def test_auto_proceed_followed_by_direct_effect_accepted(self):
        gate = self._gate("g1", auto_proceed=True)
        wf = _basic_workflow(
            approval_gates=[gate],
            action_sequence=[
                _basic_action("mark_state", gate_ref="g1", key="x", value=1, scope="instance"),
                _basic_action("append_to_ledger"),
            ],
        )
        validate_workflow(wf)

    def test_auto_proceed_safe_when_next_gate_intervenes(self):
        g1 = self._gate("g1", auto_proceed=True)
        g2 = self._gate("g2", auto_proceed=False)
        wf = _basic_workflow(
            approval_gates=[g1, g2],
            action_sequence=[
                _basic_action("mark_state", gate_ref="g1", key="x", value=1, scope="instance"),
                _basic_action("mark_state", key="y", value=2, scope="instance"),
                # Next gate boundary — irreversible action AFTER it is fine.
                _basic_action("notify_user", gate_ref="g2", channel="primary",
                              message="hi", urgency="low"),
            ],
        )
        validate_workflow(wf)

    def test_abort_workflow_gate_does_not_trigger_safe_deny(self):
        gate = self._gate("g1", auto_proceed=False)  # abort_workflow
        wf = _basic_workflow(
            approval_gates=[gate],
            action_sequence=[
                _basic_action("mark_state", gate_ref="g1", key="x", value=1, scope="instance"),
                _basic_action("notify_user", channel="primary", message="hi", urgency="low"),
            ],
        )
        validate_workflow(wf)


class TestActionTypeValidation:
    def test_unknown_action_type_rejected(self):
        wf = _basic_workflow(
            action_sequence=[ActionDescriptor(action_type="summon_demon")],
        )
        with pytest.raises(WorkflowError, match="not a known verb"):
            validate_workflow(wf)

    def test_empty_action_sequence_rejected(self):
        wf = _basic_workflow(action_sequence=[])
        with pytest.raises(WorkflowError, match="at least one action"):
            validate_workflow(wf)


# ===========================================================================
# Persistence + atomic registration
# ===========================================================================


class TestRegistration:
    async def test_register_round_trip(self, registries):
        _, wf_registry = registries
        wf = await wf_registry._register_workflow_unbound(_basic_workflow())
        assert wf.workflow_id == "wf-test"
        loaded = await wf_registry.get_workflow("wf-test")
        assert loaded is not None
        assert loaded.name == "test workflow"
        assert loaded.bounds.iteration_count == 1
        assert loaded.verifier.check == "state_set"
        assert loaded.action_sequence[0].action_type == "mark_state"
        assert loaded.trigger is not None
        assert loaded.trigger.event_type == "cc.batch.report"

    async def test_register_persists_trigger_atomically(self, registries):
        trig_registry, wf_registry = registries
        await wf_registry._register_workflow_unbound(_basic_workflow())
        triggers = await trig_registry.list_triggers("inst_a", status="active")
        assert len(triggers) == 1
        assert triggers[0].workflow_id == "wf-test"
        assert triggers[0].event_type == "cc.batch.report"

    async def test_register_invalid_predicate_no_partial_state(self, registries):
        trig_registry, wf_registry = registries
        bad_trigger = TriggerDescriptor(
            event_type="cc.batch.report",
            predicate={"op": "summon_demon"},  # invalid op
        )
        wf = _basic_workflow(workflow_id="wf-bad", trigger=bad_trigger)
        with pytest.raises(Exception):
            await wf_registry._register_workflow_unbound(wf)
        # No workflow row, no trigger row.
        wfs = await wf_registry.list_workflows("inst_a")
        trigs = await trig_registry.list_triggers("inst_a")
        assert all(w.workflow_id != "wf-bad" for w in wfs)
        assert all(t.workflow_id != "wf-bad" for t in trigs)

    async def test_register_invalid_workflow_no_partial_state(self, registries):
        trig_registry, wf_registry = registries
        wf = _basic_workflow(workflow_id="wf-bad", verifier=None)  # type: ignore[arg-type]
        with pytest.raises(Exception):
            await wf_registry._register_workflow_unbound(wf)
        wfs = await wf_registry.list_workflows("inst_a")
        trigs = await trig_registry.list_triggers("inst_a")
        assert all(w.workflow_id != "wf-bad" for w in wfs)
        assert all(t.workflow_id != "wf-bad" for t in trigs)

    async def test_register_rolls_back_workflow_when_trigger_insert_fails(
        self, registries, monkeypatch,
    ):
        """Force the trigger INSERT to fail AFTER the workflow INSERT
        succeeded. The cross-table rollback must undo the workflow
        row so neither table holds partial state."""
        from kernos.kernel.workflows.trigger_registry import Trigger
        from kernos.kernel.workflows import workflow_registry as wr_mod

        trig_registry, wf_registry = registries
        # Seed a Trigger with a known id.
        await trig_registry.register_trigger(Trigger(
            trigger_id="collide-me",
            workflow_id="some-other-wf",
            instance_id="inst_a",
            event_type="cc.batch.report",
            predicate={"op": "exists", "path": "event_id"},
            owner="owner",
        ))
        # Force register_workflow to mint the same trigger_id for
        # this registration; the second INSERT will hit the PRIMARY KEY
        # constraint and raise IntegrityError.
        monkeypatch.setattr(wr_mod.uuid, "uuid4", lambda: "collide-me")
        with pytest.raises(Exception):
            await wf_registry._register_workflow_unbound(
                _basic_workflow(workflow_id="wf-rollback"),
            )
        # Workflow row rolled back.
        wfs = await wf_registry.list_workflows("inst_a")
        assert all(w.workflow_id != "wf-rollback" for w in wfs)
        # Only the seeded trigger remains.
        trigs = await trig_registry.list_triggers("inst_a")
        assert [t.trigger_id for t in trigs] == ["collide-me"]

    async def test_register_duplicate_workflow_id_fails_atomically(self, registries):
        """Second register with the same workflow_id raises on
        IntegrityError; the existing row stays intact and no orphan
        trigger is left from the failed second attempt."""
        trig_registry, wf_registry = registries
        await wf_registry._register_workflow_unbound(_basic_workflow())
        # Second register with the same workflow_id
        with pytest.raises(Exception):
            await wf_registry._register_workflow_unbound(_basic_workflow())
        # Original workflow + trigger still there; no second trigger row.
        wfs = await wf_registry.list_workflows("inst_a")
        trigs = await trig_registry.list_triggers("inst_a")
        assert len(wfs) == 1
        assert len(trigs) == 1


class TestQueryAndStatus:
    async def test_list_filters_by_status(self, registries):
        _, wf_registry = registries
        wf_a = _basic_workflow(workflow_id="wf-a")
        wf_b = _basic_workflow(
            workflow_id="wf-b",
            trigger=TriggerDescriptor(
                event_type="cc.batch.report",
                predicate={"op": "exists", "path": "event_id"},
            ),
        )
        await wf_registry._register_workflow_unbound(wf_a)
        await wf_registry._register_workflow_unbound(wf_b)
        await wf_registry.update_status("wf-a", "paused")
        active = await wf_registry.list_workflows("inst_a", status="active")
        paused = await wf_registry.list_workflows("inst_a", status="paused")
        assert {w.workflow_id for w in active} == {"wf-b"}
        assert {w.workflow_id for w in paused} == {"wf-a"}


class TestMultiTenancy:
    async def test_workflows_in_different_instances_isolated(self, registries):
        _, wf_registry = registries
        a = _basic_workflow(workflow_id="wf-a", instance_id="inst_a")
        b = _basic_workflow(
            workflow_id="wf-b",
            instance_id="inst_b",
            trigger=TriggerDescriptor(
                event_type="cc.batch.report",
                predicate={"op": "exists", "path": "event_id"},
            ),
        )
        await wf_registry._register_workflow_unbound(a)
        await wf_registry._register_workflow_unbound(b)
        only_a = await wf_registry.list_workflows("inst_a")
        only_b = await wf_registry.list_workflows("inst_b")
        assert {w.workflow_id for w in only_a} == {"wf-a"}
        assert {w.workflow_id for w in only_b} == {"wf-b"}


class TestEndToEndTriggerFire:
    """Atomicity criterion ends in the matching behaviour: a registered
    workflow's trigger fires when a matching event flushes."""

    async def test_workflow_trigger_fires_listener(self, registries):
        trig_registry, wf_registry = registries
        captured: list = []
        trig_registry.add_match_listener(
            lambda t, e: captured.append((t.workflow_id, e.event_type)),
        )
        await wf_registry._register_workflow_unbound(_basic_workflow())
        await event_stream.emit("inst_a", "cc.batch.report", {"kind": "report"})
        await event_stream.flush_now()
        assert captured == [("wf-test", "cc.batch.report")]


class TestRegisterFromFile:
    async def test_register_from_yaml(self, registries, tmp_path):
        trig_registry, wf_registry = registries
        path = tmp_path / "morning.workflow.yaml"
        path.write_text(
            "workflow_id: wf-from-file\n"
            "instance_id: inst_a\n"
            "name: From file\n"
            "version: \"1.0\"\n"
            "owner: owner\n"
            "bounds:\n"
            "  iteration_count: 1\n"
            "  wall_time_seconds: 30\n"
            "verifier:\n"
            "  flavor: deterministic\n"
            "  check: ok\n"
            "action_sequence:\n"
            "  - action_type: mark_state\n"
            "    parameters:\n"
            "      key: x\n"
            "      value: 1\n"
            "      scope: instance\n"
            "trigger:\n"
            "  event_type: cc.batch.report\n"
            "  predicate:\n"
            "    op: exists\n"
            "    path: event_id\n"
        )
        wf = await wf_registry.register_workflow_from_file(str(path))
        assert wf.workflow_id == "wf-from-file"
        loaded = await wf_registry.get_workflow("wf-from-file")
        assert loaded is not None
        triggers = await trig_registry.list_triggers("inst_a", status="active")
        assert any(t.workflow_id == "wf-from-file" for t in triggers)
