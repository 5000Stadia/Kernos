"""SELF-IMPROVEMENT-CLOSURE-V1 Phase C — workflow integration tests.

Coverage:
* AC5 — workflow ``lookup_invariants`` + ``branch_on_invariants``
  routes correctly: linked → ``record_closure_attempt``,
  unlinked → ``terminal:legacy_fallback:legacy_ask_cc``.
* AC6 — workflow does NOT mark pattern resolved before probe
  passes. On probe pass the pattern transitions to resolved AND
  ClosureAttempt.outcome='passed'. On probe fail the pattern
  stays in current state, ClosureAttempt.outcome='failed', and
  closure.probe_failed event is emitted.
* AC10 — emit_outcome step's extra_payload carries
  ``closure_outcome``, ``closure_id``, ``invariant_id`` on the
  closure path; legacy emit carries ``closure_outcome:
  no_invariant_fallback`` only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kernos.kernel.closure_store import (
    ClosureStore,
    OUTCOME_FAILED,
    OUTCOME_PASSED,
    clear_probe_runners,
    record_closure_attempt,
    register_probe_runner,
    run_closure_probe,
)
from kernos.kernel.friction_patterns import FrictionPatternStore


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_probe_runners():
    clear_probe_runners()
    yield
    clear_probe_runners()


@pytest.fixture
async def fp_store(tmp_path: Path) -> FrictionPatternStore:
    store = FrictionPatternStore()
    await store.start(str(tmp_path))
    yield store
    await store.stop()


@pytest.fixture
async def closure_store(tmp_path: Path, fp_store) -> ClosureStore:
    store = ClosureStore()
    await store.start(str(tmp_path))
    yield store
    await store.close()


async def _seed_pattern_and_link(
    fp_store: FrictionPatternStore,
    closure_store: ClosureStore,
    *,
    instance_id: str = "i1",
    pattern_id: str = "p1",
    invariant_id: str = "tool-availability-honesty",
) -> None:
    """Insert a friction_pattern row + an invariant + the link
    row connecting them."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    await fp_store._db.execute(
        """
        INSERT INTO friction_pattern (
            instance_id, pattern_id, description, signal_type_keys,
            lifecycle_state, occurrence_count, first_observed_at,
            last_observed_at, created_at, active_epoch,
            reactivation_threshold
        ) VALUES (?, ?, ?, '[]', 'active', 0, ?, ?, ?, 0, 3)
        """,
        (instance_id, pattern_id, "seed for closure tests",
         now, now, now),
    )
    await closure_store.insert_invariant(
        instance_id=instance_id, invariant_id=invariant_id,
        statement="Catalog tools must be dispatchable.",
        owner="architect",
    )
    await closure_store.insert_link(
        instance_id=instance_id,
        pattern_id=pattern_id,
        invariant_id=invariant_id,
    )


# ---------------------------------------------------------------------
# AC5 — workflow YAML branch routing
# ---------------------------------------------------------------------


def _load_workflow():
    """Parse the self_improvement workflow YAML for shape checks."""
    from kernos.kernel.workflows.descriptor_parser import parse_descriptor
    return parse_descriptor(
        "specs/workflows/self_improvement.workflow.yaml",
    )


def test_ac5_workflow_has_lookup_invariants_step_first_after_record():
    wf = _load_workflow()
    ids = [a.id for a in wf.action_sequence]
    assert ids.index("lookup_invariants") == 1
    # record_recurrence runs first; lookup_invariants comes
    # immediately after (so the branch decision happens before
    # either path's CC dispatch).
    assert ids[0] == "record_recurrence"


def test_ac5_branch_on_invariants_routes_to_closure_path_on_true():
    wf = _load_workflow()
    branch = next(
        a for a in wf.action_sequence
        if a.id == "branch_on_invariants"
    )
    params = branch.parameters
    assert params["branch_on_true"] == "record_closure_attempt"
    assert params["branch_on_false"] == (
        "terminal:legacy_fallback:legacy_ask_cc"
    )


def test_ac5_branch_condition_references_has_invariants():
    wf = _load_workflow()
    branch = next(
        a for a in wf.action_sequence
        if a.id == "branch_on_invariants"
    )
    assert branch.parameters["condition"] == (
        "{step.lookup_invariants.value.has_invariants}"
    )


def test_ac5_closure_path_steps_present_after_branch():
    wf = _load_workflow()
    ids = [a.id for a in wf.action_sequence]
    # The closure path's steps follow the branch in main sequence.
    branch_idx = ids.index("branch_on_invariants")
    expected_after_branch = [
        "record_closure_attempt",
        "ask_cc_closure",
        "read_response_closure",
        "run_closure_probe",
        "emit_outcome_closure",
    ]
    assert ids[branch_idx + 1:] == expected_after_branch


def test_ac5_legacy_fallback_branch_has_renamed_steps():
    """Legacy path preserved under renamed step ids."""
    wf = _load_workflow()
    assert "legacy_fallback" in wf.terminal_branches
    legacy_ids = [a.id for a in wf.terminal_branches["legacy_fallback"]]
    assert legacy_ids == [
        "legacy_ask_cc",
        "legacy_read_response",
        "legacy_mark_resolved",
        "legacy_emit_outcome",
    ]


# ---------------------------------------------------------------------
# AC6 — pattern does not transition to resolved before probe passes
# ---------------------------------------------------------------------


async def test_ac6_probe_fail_leaves_pattern_in_current_state(
    fp_store, closure_store,
):
    """Pattern starts in 'active'; probe fails; pattern stays 'active'."""
    await _seed_pattern_and_link(fp_store, closure_store)

    async def _failing_probe(payload, ctx):
        return False, {
            "divergent_tools": ["broken_tool"],
            "checked_count": 27,
        }
    register_probe_runner("deterministic_introspection", _failing_probe)

    rec = await record_closure_attempt(
        store=closure_store,
        instance_id="i1",
        pattern_id="p1",
        invariant_id="tool-availability-honesty",
        active_epoch=0,
        route="code_change_via_cc",
        route_payload={},
        probe_kind="deterministic_introspection",
        probe_payload={},
        probe_payload_version=1,
    )

    emit_calls: list[dict] = []

    def _emit(*, instance_id, event_type, payload):
        emit_calls.append({
            "instance_id": instance_id,
            "event_type": event_type,
            "payload": payload,
        })

    transition_calls: list[dict] = []

    def _transition(**kwargs):
        transition_calls.append(kwargs)

    result = await run_closure_probe(
        store=closure_store,
        instance_id="i1",
        closure_id=rec["closure_id"],
        pattern_transition_fn=_transition,
        event_emit_fn=_emit,
    )

    assert result["outcome"] == OUTCOME_FAILED
    # Pattern transition NOT called on probe fail (AC6).
    assert transition_calls == []
    # Pattern still 'active' in store.
    async with fp_store._db.execute(
        "SELECT lifecycle_state FROM friction_pattern "
        "WHERE instance_id='i1' AND pattern_id='p1'"
    ) as cur:
        row = await cur.fetchone()
    assert row["lifecycle_state"] == "active"
    # closure.probe_failed event emitted with evidence.
    assert len(emit_calls) == 1
    assert emit_calls[0]["event_type"] == "closure.probe_failed"
    assert emit_calls[0]["payload"]["pattern_id"] == "p1"
    assert emit_calls[0]["payload"]["closure_id"] == rec["closure_id"]
    assert emit_calls[0]["payload"]["evidence"] == {
        "divergent_tools": ["broken_tool"],
        "checked_count": 27,
    }
    # ClosureAttempt outcome stored.
    stored = await closure_store.get_closure_attempt(
        instance_id="i1", closure_id=rec["closure_id"],
    )
    assert stored["outcome"] == "failed"
    assert stored["completed_at"] is not None


async def test_ac6_probe_pass_transitions_pattern_to_resolved(
    fp_store, closure_store,
):
    """Pattern starts 'active'; probe passes; transition_fn invoked
    with new_state='resolved'."""
    await _seed_pattern_and_link(fp_store, closure_store)

    async def _passing_probe(payload, ctx):
        return True, {"checked_count": 99}
    register_probe_runner("deterministic_introspection", _passing_probe)

    rec = await record_closure_attempt(
        store=closure_store,
        instance_id="i1",
        pattern_id="p1",
        invariant_id="tool-availability-honesty",
        active_epoch=0,
        route="code_change_via_cc",
        route_payload={},
        probe_kind="deterministic_introspection",
        probe_payload={},
        probe_payload_version=1,
    )

    emit_calls: list[dict] = []
    transition_calls: list[dict] = []

    def _emit(*, instance_id, event_type, payload):
        emit_calls.append({"event_type": event_type})

    def _transition(**kwargs):
        transition_calls.append(kwargs)

    result = await run_closure_probe(
        store=closure_store,
        instance_id="i1",
        closure_id=rec["closure_id"],
        pattern_transition_fn=_transition,
        event_emit_fn=_emit,
    )

    assert result["outcome"] == OUTCOME_PASSED
    # AC6: pattern transition called with new_state='resolved'.
    assert len(transition_calls) == 1
    assert transition_calls[0]["pattern_id"] == "p1"
    assert transition_calls[0]["new_state"] == "resolved"
    assert transition_calls[0]["resolved_by_spec"] == (
        "self_improvement_closure"
    )
    # closure.probe_failed NOT emitted on pass.
    assert emit_calls == []
    # ClosureAttempt outcome stored.
    stored = await closure_store.get_closure_attempt(
        instance_id="i1", closure_id=rec["closure_id"],
    )
    assert stored["outcome"] == "passed"


# ---------------------------------------------------------------------
# AC10 — outcome vocabulary additive, not replacing
# ---------------------------------------------------------------------


def test_ac10_closure_emit_carries_outcome_plus_extra_payload():
    """The closure path's emit_outcome step preserves the legacy
    `outcome` field (CC's investigation_outcome string) and ADDS
    closure_outcome, closure_id, invariant_id under extra_payload."""
    wf = _load_workflow()
    emit_step = next(
        a for a in wf.action_sequence
        if a.id == "emit_outcome_closure"
    )
    args = emit_step.parameters["args"]
    # Legacy field preserved unchanged.
    assert args["outcome"] == (
        "{step.read_response_closure.value.investigation_outcome}"
    )
    # New additive extra_payload.
    extra = args["extra_payload"]
    assert extra["closure_outcome"] == (
        "{step.run_closure_probe.value.outcome}"
    )
    assert extra["closure_id"] == (
        "{step.record_closure_attempt.value.closure_id}"
    )
    assert extra["invariant_id"] == (
        "{step.lookup_invariants.value.primary_invariant_id}"
    )


def test_ac10_legacy_emit_carries_no_invariant_fallback():
    """The legacy fallback emit step carries
    ``closure_outcome: no_invariant_fallback`` under extra_payload;
    no closure_id / invariant_id (none exist on this path)."""
    wf = _load_workflow()
    legacy = wf.terminal_branches["legacy_fallback"]
    legacy_emit = next(a for a in legacy if a.id == "legacy_emit_outcome")
    args = legacy_emit.parameters["args"]
    assert args["outcome"] == (
        "{step.legacy_read_response.value.investigation_outcome}"
    )
    extra = args["extra_payload"]
    assert extra["closure_outcome"] == "no_invariant_fallback"
    # No closure_id / invariant_id keys on the legacy path.
    assert "closure_id" not in extra
    assert "invariant_id" not in extra


# ---------------------------------------------------------------------
# AC15 sanity (re-exercise the parser-level checks added by Phase B
# in test_self_improvement_workflow but pin closure-specific shape).
# ---------------------------------------------------------------------


def test_ac15_yaml_parses_without_cycle_detector_error():
    """Cycle detector + branch validator both run during
    parse_descriptor. A successful parse confirms AC15."""
    wf = _load_workflow()
    # Asserts the parser didn't raise; if it did, we wouldn't
    # reach here.
    assert wf.workflow_id == "self_improvement"


# ---------------------------------------------------------------------
# AC16 — both approval_gates declared with distinct request_id refs
# ---------------------------------------------------------------------


def test_ac16_closure_gate_binds_to_ask_cc_closure_request_id():
    wf = _load_workflow()
    closure_gate = next(
        g for g in wf.approval_gates
        if g.gate_name == "await_cc_response_closure"
    )
    pred = closure_gate.approval_event_predicate
    assert pred["path"] == "payload.request_id"
    assert pred["value"] == (
        "{step.ask_cc_closure.value.request_id}"
    )


def test_ac16_legacy_gate_binds_to_legacy_ask_cc_request_id():
    wf = _load_workflow()
    legacy_gate = next(
        g for g in wf.approval_gates
        if g.gate_name == "await_cc_response_legacy"
    )
    pred = legacy_gate.approval_event_predicate
    assert pred["path"] == "payload.request_id"
    assert pred["value"] == (
        "{step.legacy_ask_cc.value.request_id}"
    )


def test_ac16_gates_have_distinct_request_id_bindings():
    """The two gates MUST bind to different request_id refs so the
    engine matches each pending workflow execution to its own
    coding_consult.response_received event."""
    wf = _load_workflow()
    bindings = {
        g.gate_name: g.approval_event_predicate["value"]
        for g in wf.approval_gates
    }
    assert bindings["await_cc_response_closure"] != (
        bindings["await_cc_response_legacy"]
    )
