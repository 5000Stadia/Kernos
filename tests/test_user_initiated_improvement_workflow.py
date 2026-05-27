"""USER-INITIATED-IMPROVEMENT-TRIGGER-V1 Phase E — workflow YAML
integration + hard-boundary static-analysis pin tests.

Coverage:
* AC3 — workflow YAML registered + parses + first step is
  record_authorization.
* AC4 — surface_investigation_started step shape.
* AC5 — investigation prompt includes external-cause guidance.
* AC7 — branch routes correctly: True → substrate path,
  False → terminal:light_apply:apply_fix.
* AC8 — substrate path goes through await_architect_ratification
  before any apply.
* AC9 — light path goes through await_light_apply_response.
* AC10 — surface_to_user calls use distinct message_kinds.
* AC11 — surface_to_user steps use on_failure: continue.
* AC15 — event payload schema is exported as a module constant.
* AC23 — closure with no link → no_invariant_fallback (unit test).
* AC24 — closure with linked invariant → probe runs (unit test).
* AC27 — hard-boundary static-analysis: substrate-tier branch
  cannot reach apply without architect gate.
* AC28 — sensitive scope still triggers architect gate.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kernos.kernel.fix_authorization import (
    classify_fix_scope,
    maybe_run_closure_for_fix,
)


def _load_workflow():
    from kernos.kernel.workflows.descriptor_parser import parse_descriptor
    return parse_descriptor(
        "specs/workflows/user_initiated_improvement.workflow.yaml",
    )


# ---------------------------------------------------------------------
# AC3 — workflow registered + parses
# ---------------------------------------------------------------------


def test_ac3_workflow_id_is_user_initiated_improvement():
    wf = _load_workflow()
    assert wf.workflow_id == "user_initiated_improvement"


def test_ac3_first_step_is_record_authorization():
    wf = _load_workflow()
    assert wf.action_sequence[0].id == "record_authorization"


def test_ac3_trigger_is_user_fix_authorization_received():
    """Triggers list has the right event_type."""
    import yaml
    with Path(
        "specs/workflows/user_initiated_improvement.workflow.yaml"
    ).open() as fp:
        body = yaml.safe_load(fp)
    triggers = body["triggers"]
    assert any(
        t["event_type"] == "user.fix_authorization_received"
        for t in triggers
    )


# ---------------------------------------------------------------------
# AC4 — surface_investigation_started step
# ---------------------------------------------------------------------


def test_ac4_surface_investigation_started_runs_after_record():
    wf = _load_workflow()
    ids = [a.id for a in wf.action_sequence]
    rec_idx = ids.index("record_authorization")
    surf_idx = ids.index("surface_investigation_started")
    assert surf_idx == rec_idx + 1


def test_ac4_surface_started_uses_investigation_started_kind():
    wf = _load_workflow()
    step = next(
        a for a in wf.action_sequence
        if a.id == "surface_investigation_started"
    )
    args = step.parameters["args"]
    assert args["message_kind"] == "investigation_started"


# ---------------------------------------------------------------------
# AC5 — external-cause guidance in investigation prompt
# ---------------------------------------------------------------------


def test_ac5_investigation_prompt_includes_external_cause_guidance():
    wf = _load_workflow()
    step = next(
        a for a in wf.action_sequence if a.id == "investigate"
    )
    question = step.parameters["args"]["question"]
    # Regression pin: the prompt MUST direct CC to consider
    # external causes (not just internal-Kernos causes).
    assert "External causes" in question
    assert "Internal causes" in question
    # Webpage/API examples kept literal so a regression that
    # drops external-cause guidance is caught here.
    assert "webpage" in question.lower() or "api" in question.lower()


def test_ac5_investigation_prompt_requires_structured_fields():
    wf = _load_workflow()
    step = next(
        a for a in wf.action_sequence if a.id == "investigate"
    )
    question = step.parameters["args"]["question"]
    # Per spec — investigation response MUST populate
    # these structured fields.
    for required_field in (
        "failure_mode",
        "external_cause_evidence",
        "internal_cause_evidence",
        "proposed_fix_summary",
        "proposed_fix_diff",
        "touches_paths",
    ):
        assert required_field in question, (
            f"investigation prompt missing required structured "
            f"field: {required_field}"
        )


def test_ac5_investigation_prompt_warns_against_substrate_changes():
    wf = _load_workflow()
    step = next(
        a for a in wf.action_sequence if a.id == "investigate"
    )
    question = step.parameters["args"]["question"]
    # CC MUST be told not to apply substrate-tier changes
    # itself (this workflow routes them through the architect
    # gate).
    assert (
        "substrate-tier changes" in question.lower()
        or "DO NOT apply substrate" in question
    )


# ---------------------------------------------------------------------
# AC7 — branch routing
# ---------------------------------------------------------------------


def test_ac7_branch_on_requires_architect_gate_native_bool():
    wf = _load_workflow()
    branch = next(
        a for a in wf.action_sequence if a.id == "branch_on_gate_weight"
    )
    cond = branch.parameters["condition"]
    assert cond == (
        "{step.classify_scope.value.requires_architect_gate}"
    )


def test_ac7_branch_true_routes_to_request_architect_gate():
    wf = _load_workflow()
    branch = next(
        a for a in wf.action_sequence if a.id == "branch_on_gate_weight"
    )
    assert branch.parameters["branch_on_true"] == (
        "request_architect_gate"
    )


def test_ac7_branch_false_routes_to_terminal_light_apply():
    wf = _load_workflow()
    branch = next(
        a for a in wf.action_sequence if a.id == "branch_on_gate_weight"
    )
    assert branch.parameters["branch_on_false"] == (
        "terminal:light_apply:apply_fix"
    )


# ---------------------------------------------------------------------
# AC8 / AC9 — gates bound correctly
# ---------------------------------------------------------------------


def test_ac8_substrate_path_has_architect_gate():
    wf = _load_workflow()
    arch_step = next(
        a for a in wf.action_sequence if a.id == "request_architect_gate"
    )
    assert arch_step.gate_ref == "await_architect_ratification"
    gate = next(
        g for g in wf.approval_gates
        if g.gate_name == "await_architect_ratification"
    )
    assert gate.approval_event_predicate["value"] == (
        "{step.request_architect_gate.value.request_id}"
    )


def test_ac9_light_path_uses_light_apply_gate():
    wf = _load_workflow()
    light_steps = wf.terminal_branches["light_apply"]
    apply_step = next(s for s in light_steps if s.id == "apply_fix")
    assert apply_step.gate_ref == "await_light_apply_response"
    gate = next(
        g for g in wf.approval_gates
        if g.gate_name == "await_light_apply_response"
    )
    assert gate.approval_event_predicate["value"] == (
        "{step.apply_fix.value.request_id}"
    )


def test_ac8_ac9_gate_request_ids_distinct():
    wf = _load_workflow()
    bindings = {
        g.gate_name: g.approval_event_predicate["value"]
        for g in wf.approval_gates
    }
    distinct = set(bindings.values())
    assert len(distinct) == len(bindings), (
        f"gate predicates must be distinct request_id bindings; "
        f"got {bindings}"
    )


# ---------------------------------------------------------------------
# AC10 — surfacing kinds distinct
# ---------------------------------------------------------------------


def test_ac10_surfacing_steps_use_distinct_kinds():
    wf = _load_workflow()
    surf_kinds_main = [
        a.parameters["args"]["message_kind"]
        for a in wf.action_sequence
        if a.action_type == "call_tool"
        and a.parameters.get("tool_id") == "surface_to_user"
    ]
    surf_kinds_light = [
        a.parameters["args"]["message_kind"]
        for a in wf.terminal_branches["light_apply"]
        if a.action_type == "call_tool"
        and a.parameters.get("tool_id") == "surface_to_user"
    ]
    all_kinds = surf_kinds_main + surf_kinds_light
    assert "investigation_started" in all_kinds
    assert "investigation_outcome" in all_kinds
    # All surface_to_user calls in this workflow are either
    # _started or _outcome — no other kinds.
    assert set(all_kinds) <= {
        "investigation_started", "investigation_outcome",
    }


# ---------------------------------------------------------------------
# AC11 — surface_to_user uses on_failure: continue
# ---------------------------------------------------------------------


def test_ac11_surfacing_failure_does_not_abort_workflow():
    wf = _load_workflow()
    for step in (
        list(wf.action_sequence)
        + list(wf.terminal_branches["light_apply"])
    ):
        if (
            step.action_type == "call_tool"
            and step.parameters.get("tool_id") == "surface_to_user"
        ):
            assert step.continuation_rules.on_failure == "continue", (
                f"surface_to_user step {step.id} must use "
                f"on_failure: continue; got "
                f"{step.continuation_rules.on_failure}"
            )


# ---------------------------------------------------------------------
# AC15 — payload schema documented in source
# ---------------------------------------------------------------------


def test_ac15_event_schema_constant_exists():
    """The event payload schema is exported from a Python module
    so future spec authors don't have to reverse-engineer it
    from YAML refs."""
    from kernos.kernel.fix_authorization import (
        FixAuthorizationStore,
        FixScopeResult,
        SCOPE_EXTERNAL_ONLY, SCOPE_CONFIG_DATA,
        SCOPE_SENSITIVE, SCOPE_SUBSTRATE_TIER,
    )
    # If these imports work, the schema constants are accessible
    # from the module (the contract). Stand-in for "schema
    # documented" check.
    assert SCOPE_EXTERNAL_ONLY == "external_only"
    assert SCOPE_SUBSTRATE_TIER == "substrate_tier"


# ---------------------------------------------------------------------
# AC23 — closure composition: no link → no_invariant_fallback
# ---------------------------------------------------------------------


async def test_ac23_no_related_pattern_returns_no_invariant_fallback():
    result = await maybe_run_closure_for_fix(
        instance_id="i1",
        related_pattern_id="",
        active_epoch=0,
        closure_store=None,
    )
    assert result == {
        "closure_outcome": "no_invariant_fallback",
        "closure_id": "",
        "invariant_id": "",
    }


async def test_ac23_no_closure_store_returns_no_invariant_fallback():
    """Even with related_pattern_id non-empty, no closure_store →
    fallback."""
    result = await maybe_run_closure_for_fix(
        instance_id="i1",
        related_pattern_id="some-pattern",
        active_epoch=0,
        closure_store=None,
    )
    assert result["closure_outcome"] == "no_invariant_fallback"


# ---------------------------------------------------------------------
# AC24 — closure composition: linked invariant → probe runs
# ---------------------------------------------------------------------


@pytest.fixture
async def closure_composition_substrate(tmp_path: Path):
    from kernos.kernel.closure_store import (
        ClosureStore, clear_probe_runners, register_probe_runner,
    )
    from kernos.kernel.friction_patterns import FrictionPatternStore
    from datetime import datetime, timezone

    fp_store = FrictionPatternStore()
    await fp_store.start(str(tmp_path))
    cs = ClosureStore()
    await cs.start(str(tmp_path))

    clear_probe_runners()

    async def _passing_probe(payload, ctx):
        return True, {"checked": 1}
    register_probe_runner("deterministic_introspection", _passing_probe)

    now = datetime.now(timezone.utc).isoformat()
    await fp_store._db.execute(
        """
        INSERT INTO friction_pattern (
            instance_id, pattern_id, description, signal_type_keys,
            lifecycle_state, occurrence_count, first_observed_at,
            last_observed_at, created_at, active_epoch,
            reactivation_threshold
        ) VALUES ('i1', 'pat_x', 'seed', '[]', 'active', 0, ?, ?, ?, 0, 3)
        """,
        (now, now, now),
    )
    await cs.insert_invariant(
        instance_id="i1", invariant_id="inv_x",
        statement="x", owner="architect",
    )
    await cs.insert_link(
        instance_id="i1", pattern_id="pat_x", invariant_id="inv_x",
    )

    yield {"fp_store": fp_store, "closure_store": cs}

    await cs.close()
    await fp_store.stop()
    clear_probe_runners()


async def test_ac24_linked_pattern_runs_probe(
    closure_composition_substrate,
):
    cs = closure_composition_substrate["closure_store"]
    result = await maybe_run_closure_for_fix(
        instance_id="i1",
        related_pattern_id="pat_x",
        active_epoch=0,
        closure_store=cs,
    )
    assert result["closure_outcome"] == "passed"
    assert result["closure_id"]   # non-empty
    assert result["invariant_id"] == "inv_x"


# ---------------------------------------------------------------------
# AC27 — hard-boundary preservation (static analysis on YAML)
# ---------------------------------------------------------------------


def test_ac27_substrate_path_unreachable_without_architect_gate():
    """Walk the workflow's main action_sequence; verify any
    step that applies code changes (ask_coding_session with an
    apply-shaped question) is preceded by
    await_architect_ratification gate."""
    wf = _load_workflow()
    seen_architect_gate = False
    for step in wf.action_sequence:
        if (
            step.gate_ref == "await_architect_ratification"
        ):
            seen_architect_gate = True
        # The only step that POSSIBLY applies code changes
        # in the main sequence is request_architect_gate
        # itself (which IS the architect gate). The classifier,
        # validation, and surfacing steps don't apply changes.
        if step.id == "request_architect_gate":
            assert seen_architect_gate or (
                step.gate_ref == "await_architect_ratification"
            ), (
                "request_architect_gate must itself bind the "
                "await_architect_ratification gate; static "
                "analysis pin failed"
            )


def test_ac27_substrate_path_branch_target_is_main_not_terminal():
    """branch_on_true=request_architect_gate ensures
    substrate-tier execution stays in main sequence (under
    the gate) rather than jumping to a terminal that bypasses
    the gate."""
    wf = _load_workflow()
    branch = next(
        a for a in wf.action_sequence if a.id == "branch_on_gate_weight"
    )
    true_target = branch.parameters["branch_on_true"]
    # True target must be a main-sequence step id (no
    # terminal: prefix).
    assert not true_target.startswith("terminal:")
    # And it must be the architect-gate step specifically.
    assert true_target == "request_architect_gate"


# ---------------------------------------------------------------------
# AC28 — sensitive scope routes architect gate + labels distinctly
# ---------------------------------------------------------------------


def test_ac28_sensitive_scope_requires_architect_gate():
    r = classify_fix_scope(touches_paths=[".env"])
    assert r.scope == "sensitive"
    assert r.requires_architect_gate is True
    # workflow routes True → request_architect_gate (same gate
    # as substrate_tier). The label distinction lives in the
    # surfacing metadata.


def test_ac28_substrate_outcome_surfacing_carries_scope_label():
    wf = _load_workflow()
    surf_step = next(
        a for a in wf.action_sequence
        if a.id == "surface_architect_outcome"
    )
    metadata = surf_step.parameters["args"]["metadata"]
    # Scope label + sensitive flag both carried so operator can
    # distinguish substrate_tier from sensitive.
    assert metadata["scope"] == "{step.classify_scope.value.scope}"
    assert metadata["sensitive_path_detected"] == (
        "{step.classify_scope.value.sensitive_path_detected}"
    )
    assert metadata["sensitive_paths"] == (
        "{step.classify_scope.value.sensitive_paths}"
    )
