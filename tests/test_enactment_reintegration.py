"""Tests for ReintegrationContext + PlanRef + ExecutionTrace (PDI C6).

Per design review's C6 guidance: caps enforced at construction, not
aspirational. truncated flag set when ANY field exceeds its cap.
"""

from __future__ import annotations

import pytest

from kernos.kernel.enactment.plan import Plan, Step, StepExpectation
from kernos.kernel.enactment.reintegration import (
    DISCOVERED_INFORMATION_CAP,
    ExecutionTrace,
    PLANS_ATTEMPTED_CAP,
    PlanRef,
    ReintegrationContext,
    TOOL_OUTCOMES_SUMMARY_CAP,
)


# ---------------------------------------------------------------------------
# Caps locked
# ---------------------------------------------------------------------------


def test_caps_match_spec_locked_values():
    """Design Review-locked caps. Changes here are coordinated migrations,
    not silent tweaks."""
    assert TOOL_OUTCOMES_SUMMARY_CAP == 1000
    assert DISCOVERED_INFORMATION_CAP == 500
    assert PLANS_ATTEMPTED_CAP == 5


# ---------------------------------------------------------------------------
# PlanRef
# ---------------------------------------------------------------------------


def _step(step_id: str = "s1") -> Step:
    return Step(
        step_id=step_id,
        tool_id="t",
        arguments={},
        tool_class="email",
        operation_name="send",
        expectation=StepExpectation(prose="x"),
    )


def test_plan_ref_round_trip():
    ref = PlanRef(plan_id="p1", created_via="initial", step_count=3)
    parsed = PlanRef.from_dict(ref.to_dict())
    assert parsed == ref


def test_plan_ref_from_plan():
    plan = Plan(plan_id="p1", turn_id="t1", steps=(_step(), _step("s2")))
    ref = PlanRef.from_plan(plan)
    assert ref.plan_id == "p1"
    assert ref.created_via == "initial"
    assert ref.step_count == 2


# ---------------------------------------------------------------------------
# ReintegrationContext — caps enforced at construction
# ---------------------------------------------------------------------------


def test_reintegration_under_caps_no_truncation():
    ctx = ReintegrationContext(
        original_decided_action_kind="execute_tool",
        plans_attempted=(
            PlanRef(plan_id="p1", created_via="initial", step_count=1),
        ),
        tool_outcomes_summary="short summary",
        discovered_information="found something",
        audit_refs=("ref-1",),
    )
    assert ctx.truncated is False


def test_reintegration_truncates_tool_outcomes_summary():
    huge = "x" * (TOOL_OUTCOMES_SUMMARY_CAP + 500)
    ctx = ReintegrationContext(
        original_decided_action_kind="execute_tool",
        tool_outcomes_summary=huge,
    )
    assert len(ctx.tool_outcomes_summary) == TOOL_OUTCOMES_SUMMARY_CAP
    assert ctx.truncated is True


def test_reintegration_truncates_discovered_information():
    huge = "y" * (DISCOVERED_INFORMATION_CAP + 200)
    ctx = ReintegrationContext(
        original_decided_action_kind="execute_tool",
        discovered_information=huge,
    )
    assert len(ctx.discovered_information) == DISCOVERED_INFORMATION_CAP
    assert ctx.truncated is True


def test_reintegration_truncates_plans_attempted():
    many = tuple(
        PlanRef(plan_id=f"p{i}", created_via="initial", step_count=1)
        for i in range(PLANS_ATTEMPTED_CAP + 3)
    )
    ctx = ReintegrationContext(
        original_decided_action_kind="execute_tool",
        plans_attempted=many,
    )
    assert len(ctx.plans_attempted) == PLANS_ATTEMPTED_CAP
    assert ctx.truncated is True


def test_reintegration_audit_refs_unbounded():
    """audit_refs is intentionally unbounded — references-not-dumps;
    full traces live in audit, not in the payload."""
    refs = tuple(f"ref-{i}" for i in range(50))
    ctx = ReintegrationContext(
        original_decided_action_kind="execute_tool",
        audit_refs=refs,
    )
    assert len(ctx.audit_refs) == 50
    # Truncation flag is only set for the capped fields.
    assert ctx.truncated is False


def test_reintegration_rejects_empty_audit_ref():
    with pytest.raises(ValueError, match="audit_refs"):
        ReintegrationContext(
            original_decided_action_kind="execute_tool",
            audit_refs=("",),
        )


def test_reintegration_truncated_flag_explicitly_set_by_caller():
    """A caller may set truncated=True explicitly even when no field
    is over cap (e.g., synthesised from prior-turn payload). The
    flag remains True."""
    ctx = ReintegrationContext(
        original_decided_action_kind="execute_tool",
        tool_outcomes_summary="ok",
        truncated=True,
    )
    assert ctx.truncated is True


def test_reintegration_round_trip():
    ctx = ReintegrationContext(
        original_decided_action_kind="execute_tool",
        plans_attempted=(
            PlanRef(plan_id="p1", created_via="initial", step_count=1),
        ),
        tool_outcomes_summary="x",
        discovered_information="y",
        audit_refs=("r1",),
    )
    parsed = ReintegrationContext.from_dict(ctx.to_dict())
    assert parsed == ctx


# ---------------------------------------------------------------------------
# ExecutionTrace
# ---------------------------------------------------------------------------


def test_execution_trace_records_plans_outcomes_information_refs():
    trace = ExecutionTrace()
    plan = Plan(plan_id="p1", turn_id="t1", steps=(_step(),))
    trace.record_plan(plan)
    trace.record_step_outcome("step s1: completed")
    trace.record_discovered_information("found data")
    trace.record_audit_ref("audit-1")

    assert len(trace.plans_attempted) == 1
    assert trace.tool_outcomes == ["step s1: completed"]
    assert trace.discovered_information_chunks == ["found data"]
    assert trace.audit_refs == ["audit-1"]


def test_execution_trace_skips_blank_strings():
    trace = ExecutionTrace()
    trace.record_step_outcome("   ")
    trace.record_discovered_information("")
    trace.record_audit_ref(" ")
    assert trace.tool_outcomes == []
    assert trace.discovered_information_chunks == []
    assert trace.audit_refs == []


def test_execution_trace_to_reintegration_context_applies_caps():
    """The trace accumulates unbounded; caps applied at
    ReintegrationContext construction."""
    trace = ExecutionTrace()
    for i in range(7):
        plan = Plan(
            plan_id=f"p{i}", turn_id="t1", steps=(_step(f"s{i}"),)
        )
        trace.record_plan(plan)
    huge_chunk = "x" * 600
    trace.record_discovered_information(huge_chunk)

    ctx = trace.to_reintegration_context(
        original_decided_action_kind="execute_tool"
    )
    # Plans capped at 5.
    assert len(ctx.plans_attempted) == PLANS_ATTEMPTED_CAP
    # Discovered information capped at 500.
    assert len(ctx.discovered_information) == DISCOVERED_INFORMATION_CAP
    assert ctx.truncated is True
