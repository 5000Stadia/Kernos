"""Tests for the integration-layer briefing schema (C1 of INTEGRATION-LAYER).

Covers:
  - Visibility tagged union (Public, Restricted) round-trip + dispatch
  - CohortOutput validation, restriction-marker, JSON round-trip
  - ContextItem / FilteredItem validation and round-trip (free-form
    source_type per revised spec)
  - DecidedAction tagged-union dispatch (all six variants, including
    Pivot.suggested_shape and Defer.follow_up_signal)
  - BudgetState shape + any_hit
  - AuditTrace validation + telemetry fields, references-not-dumps shape
  - Briefing top-level validation + JSON round-trip
  - Minimal fail-soft fallback construction (Section 4c)
  - Frozen-dataclass immutability
  - Redaction-invariant smoke checks at the schema surface
"""

from __future__ import annotations

import json

import pytest

from kernos.kernel.integration.briefing import (
    ActionEnvelope,
    ActionKind,
    AuditTrace,
    Briefing,
    BriefingValidationError,
    BudgetState,
    ClarificationNeeded,
    ClarificationPartialState,
    CohortOutput,
    ConstrainedResponse,
    ContextItem,
    Defer,
    ExecuteTool,
    FilteredItem,
    Outcome,
    Pivot,
    ProposeTool,
    Public,
    RespondOnly,
    Restricted,
    VisibilityKind,
    action_kind_requires_envelope,
    decided_action_from_dict,
    minimal_fail_soft_briefing,
    now_iso,
    visibility_from_dict,
)


# ---------------------------------------------------------------------------
# Visibility tagged union
# ---------------------------------------------------------------------------


def test_public_round_trip():
    v = Public()
    assert v.kind is VisibilityKind.PUBLIC
    payload = v.to_dict()
    assert payload == {"kind": "public"}
    assert Public.from_dict(payload) == v
    assert visibility_from_dict(payload) == v


def test_restricted_round_trip():
    v = Restricted(reason="covenant")
    assert v.kind is VisibilityKind.RESTRICTED
    payload = v.to_dict()
    assert payload == {"kind": "restricted", "reason": "covenant"}
    assert Restricted.from_dict(payload) == v
    assert visibility_from_dict(payload) == v


def test_restricted_requires_reason():
    with pytest.raises(BriefingValidationError, match="reason"):
        Restricted(reason="")


def test_visibility_from_dict_rejects_unknown_kind():
    with pytest.raises(BriefingValidationError, match="kind"):
        visibility_from_dict({"kind": "unknown_visibility"})


# ---------------------------------------------------------------------------
# CohortOutput
# ---------------------------------------------------------------------------


def test_cohort_output_default_visibility_is_public():
    co = CohortOutput(
        cohort_id="memory",
        cohort_run_id="memcohort:r1",
        output={"hits": ["x"]},
        produced_at=now_iso(),
    )
    assert isinstance(co.visibility, Public)
    assert co.is_restricted is False


def test_cohort_output_round_trip_public():
    co = CohortOutput(
        cohort_id="weather",
        cohort_run_id="wc:r1",
        output={"forecast": "clear"},
        visibility=Public(),
        produced_at="2026-04-26T00:00:00+00:00",
    )
    payload = co.to_dict()
    assert payload["visibility"] == {"kind": "public"}
    assert CohortOutput.from_dict(payload) == co


def test_cohort_output_round_trip_restricted():
    co = CohortOutput(
        cohort_id="covenant",
        cohort_run_id="cv:r1",
        output={"covenant_text": "secret"},
        visibility=Restricted(reason="covenant"),
        produced_at="2026-04-26T00:00:00+00:00",
    )
    payload = co.to_dict()
    assert payload["visibility"] == {"kind": "restricted", "reason": "covenant"}
    parsed = CohortOutput.from_dict(payload)
    assert parsed == co
    assert parsed.is_restricted is True


def test_cohort_output_default_outcome_is_success():
    co = CohortOutput(
        cohort_id="memory", cohort_run_id="memcohort:r1", output={},
    )
    assert co.outcome is Outcome.SUCCESS
    assert co.error_summary == ""
    assert co.is_synthetic is False


def test_cohort_output_synthetic_with_outcome_and_error_summary():
    """COHORT-FAN-OUT-RUNNER the design review edit #4: synthetic outputs carry
    outcome + error_summary as runner-owned metadata; output dict
    stays empty."""
    co = CohortOutput(
        cohort_id="memory",
        cohort_run_id="turn-7:memory:0",
        output={},
        outcome=Outcome.TIMEOUT_PER_COHORT,
        error_summary="exceeded 500ms",
    )
    assert co.is_synthetic is True
    assert co.outcome is Outcome.TIMEOUT_PER_COHORT
    payload = co.to_dict()
    assert payload["outcome"] == "timeout_per_cohort"
    assert payload["error_summary"] == "exceeded 500ms"
    assert payload["output"] == {}
    assert CohortOutput.from_dict(payload) == co


def test_cohort_output_round_trips_each_outcome_variant():
    for variant in Outcome:
        co = CohortOutput(
            cohort_id="x",
            cohort_run_id="t:x:0",
            output={},
            outcome=variant,
            error_summary="" if variant is Outcome.SUCCESS else "redacted",
        )
        assert CohortOutput.from_dict(co.to_dict()) == co


def test_cohort_output_rejects_invalid_outcome_at_parse():
    with pytest.raises(BriefingValidationError, match="outcome"):
        CohortOutput.from_dict(
            {
                "cohort_id": "x",
                "cohort_run_id": "r1",
                "output": {},
                "visibility": {"kind": "public"},
                "produced_at": "",
                "outcome": "exploded",
                "error_summary": "",
            }
        )


def test_cohort_output_rejects_non_outcome_value():
    with pytest.raises(BriefingValidationError, match="outcome"):
        CohortOutput(
            cohort_id="x",
            cohort_run_id="r1",
            output={},
            outcome="success",  # type: ignore[arg-type]
        )


def test_outcome_enum_has_five_variants():
    """Per the design review edit #8: timeout split into per-cohort vs global."""
    assert {o.value for o in Outcome} == {
        "success",
        "timeout_per_cohort",
        "timeout_global",
        "error",
        "cancelled",
    }


def test_cohort_output_rejects_empty_cohort_id():
    with pytest.raises(BriefingValidationError, match="cohort_id"):
        CohortOutput(cohort_id="", cohort_run_id="r1", output={})


def test_cohort_output_rejects_empty_run_id():
    with pytest.raises(BriefingValidationError, match="cohort_run_id"):
        CohortOutput(cohort_id="x", cohort_run_id="", output={})


def test_cohort_output_rejects_non_dict_output():
    with pytest.raises(BriefingValidationError, match="output"):
        CohortOutput(
            cohort_id="x",
            cohort_run_id="r1",
            output="not a dict",  # type: ignore[arg-type]
        )


def test_cohort_output_rejects_non_visibility():
    with pytest.raises(BriefingValidationError, match="visibility"):
        CohortOutput(
            cohort_id="x",
            cohort_run_id="r1",
            output={},
            visibility="public",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# ContextItem
# ---------------------------------------------------------------------------


def test_context_item_happy_path_and_round_trip():
    item = ContextItem(
        source_type="cohort.memory",
        source_id="mem-42",
        summary="user owns a cat named Calliope",
        confidence=0.85,
    )
    payload = item.to_dict()
    assert payload == {
        "source_type": "cohort.memory",
        "source_id": "mem-42",
        "summary": "user owns a cat named Calliope",
        "confidence": 0.85,
    }
    assert ContextItem.from_dict(payload) == item


def test_context_item_accepts_dotted_source_type():
    item = ContextItem(
        source_type="tool.read.drive_read_doc",
        source_id="inv-1",
        summary="doc summarized",
        confidence=0.7,
    )
    assert item.source_type == "tool.read.drive_read_doc"


def test_context_item_rejects_empty_source_type():
    with pytest.raises(BriefingValidationError, match="source_type"):
        ContextItem(
            source_type="   ",
            source_id="x",
            summary="y",
            confidence=0.1,
        )


def test_context_item_rejects_confidence_out_of_range():
    with pytest.raises(BriefingValidationError, match=r"\[0\.0, 1\.0\]"):
        ContextItem(
            source_type="cohort.memory",
            source_id="c",
            summary="s",
            confidence=1.5,
        )


def test_context_item_rejects_empty_summary():
    with pytest.raises(BriefingValidationError, match="summary"):
        ContextItem(
            source_type="cohort.memory",
            source_id="c",
            summary="   ",
            confidence=0.5,
        )


def test_context_item_is_frozen():
    item = ContextItem(
        source_type="cohort.memory",
        source_id="c",
        summary="s",
        confidence=0.5,
    )
    with pytest.raises((AttributeError, Exception)):
        item.summary = "tampered"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FilteredItem (no summary field per revised spec)
# ---------------------------------------------------------------------------


def test_filtered_item_happy_path_and_round_trip():
    item = FilteredItem(
        source_type="cohort.weather",
        source_id="weather-cohort:r1",
        reason_filtered="user did not ask about weather",
    )
    payload = item.to_dict()
    assert set(payload.keys()) == {"source_type", "source_id", "reason_filtered"}
    assert FilteredItem.from_dict(payload) == item


def test_filtered_item_requires_reason_filtered():
    with pytest.raises(BriefingValidationError, match="reason_filtered"):
        FilteredItem(
            source_type="cohort.x",
            source_id="c",
            reason_filtered="",
        )


# ---------------------------------------------------------------------------
# DecidedAction tagged union
# ---------------------------------------------------------------------------


def test_respond_only_round_trip():
    action = RespondOnly()
    assert action.kind is ActionKind.RESPOND_ONLY
    payload = action.to_dict()
    assert payload == {"kind": "respond_only"}
    assert decided_action_from_dict(payload) == action


def test_execute_tool_round_trip():
    action = ExecuteTool(
        tool_id="drive_read_doc",
        arguments={"file_id": "abc-123"},
        narration_context="user asked about the quarterly plan",
    )
    payload = action.to_dict()
    assert payload["kind"] == "execute_tool"
    assert payload["tool_id"] == "drive_read_doc"
    assert payload["arguments"] == {"file_id": "abc-123"}
    parsed = decided_action_from_dict(payload)
    assert isinstance(parsed, ExecuteTool)
    assert parsed == action


def test_execute_tool_requires_tool_id():
    with pytest.raises(BriefingValidationError, match="tool_id"):
        ExecuteTool(tool_id="", arguments={}, narration_context="ctx")


def test_propose_tool_round_trip():
    action = ProposeTool(
        tool_id="discord_send_message",
        arguments={"channel_id": "1234", "content": "hi"},
        reason="this requires confirmation per gate class hard_write",
    )
    payload = action.to_dict()
    assert payload["kind"] == "propose_tool"
    assert decided_action_from_dict(payload) == action


def test_propose_tool_requires_reason():
    with pytest.raises(BriefingValidationError, match="reason"):
        ProposeTool(tool_id="t", arguments={}, reason="")


def test_constrained_response_round_trip_with_satisfaction_partial():
    """Spec uses field name `satisfaction_partial`."""
    action = ConstrainedResponse(
        constraint="full document not accessible",
        satisfaction_partial="summary of available sections",
    )
    payload = action.to_dict()
    assert payload["kind"] == "constrained_response"
    assert payload["satisfaction_partial"] == "summary of available sections"
    assert decided_action_from_dict(payload) == action


def test_constrained_response_requires_both_fields():
    with pytest.raises(BriefingValidationError, match="constraint"):
        ConstrainedResponse(constraint="", satisfaction_partial="x")
    with pytest.raises(BriefingValidationError, match="satisfaction_partial"):
        ConstrainedResponse(constraint="x", satisfaction_partial="")


def test_pivot_round_trip_with_suggested_shape():
    """Spec adds `suggested_shape` field to Pivot."""
    action = Pivot(
        reason="redirect toward agreed alternative",
        suggested_shape="general planning conversation",
    )
    payload = action.to_dict()
    assert payload == {
        "kind": "pivot",
        "reason": "redirect toward agreed alternative",
        "suggested_shape": "general planning conversation",
    }
    assert decided_action_from_dict(payload) == action


def test_pivot_requires_suggested_shape():
    with pytest.raises(BriefingValidationError, match="suggested_shape"):
        Pivot(reason="x", suggested_shape="")


def test_defer_round_trip_with_follow_up_signal():
    """Spec adds `follow_up_signal` field to Defer."""
    action = Defer(
        reason="external dependency unavailable",
        follow_up_signal="will retry in 10 min",
    )
    payload = action.to_dict()
    assert payload == {
        "kind": "defer",
        "reason": "external dependency unavailable",
        "follow_up_signal": "will retry in 10 min",
    }
    assert decided_action_from_dict(payload) == action


def test_defer_requires_follow_up_signal():
    with pytest.raises(BriefingValidationError, match="follow_up_signal"):
        Defer(reason="x", follow_up_signal="")


def test_decided_action_rejects_unknown_kind():
    with pytest.raises(BriefingValidationError, match="kind"):
        decided_action_from_dict({"kind": "nuke_everything"})


def test_decided_action_rejects_kind_mismatch_on_specific_variant():
    with pytest.raises(BriefingValidationError, match="kind mismatch"):
        Pivot.from_dict({"kind": "defer", "reason": "x", "suggested_shape": "y"})


def test_decided_action_enum_includes_pdi_clarification_extension():
    """Spec Section 1 fixed the enum at six variants; PDI extends to
    seven by adding clarification_needed (V1 schema extension)."""
    assert {k.value for k in ActionKind} == {
        "respond_only",
        "execute_tool",
        "propose_tool",
        "constrained_response",
        "pivot",
        "defer",
        "clarification_needed",
    }


# ---------------------------------------------------------------------------
# BudgetState
# ---------------------------------------------------------------------------


def test_budget_state_default_is_no_limits_hit():
    bs = BudgetState()
    assert bs.any_hit is False
    assert bs.iterations_hit_limit is False


def test_budget_state_any_hit_when_iterations_hit():
    bs = BudgetState(iterations_hit_limit=True)
    assert bs.any_hit is True


def test_budget_state_round_trip():
    bs = BudgetState(
        iterations_hit_limit=True,
        timeout_hit_limit=False,
        cohort_entries_hit_limit=False,
        filtered_entries_hit_limit=True,
        tokens_hit_limit=False,
    )
    payload = bs.to_dict()
    assert payload == {
        "iterations_hit_limit": True,
        "timeout_hit_limit": False,
        "cohort_entries_hit_limit": False,
        "filtered_entries_hit_limit": True,
        "tokens_hit_limit": False,
        "required_cohort_failed": False,
        "required_safety_cohort_failed": False,
        "cohort_fan_out_global_timeout": False,
    }
    assert BudgetState.from_dict(payload) == bs


def test_budget_state_cohort_fan_out_flags_round_trip():
    """COHORT-FAN-OUT-RUNNER Section 8 extends BudgetState with three
    flags integration's filter phase reads to apply policy."""
    bs = BudgetState(
        required_cohort_failed=True,
        required_safety_cohort_failed=True,
        cohort_fan_out_global_timeout=True,
    )
    assert bs.any_hit is True
    payload = bs.to_dict()
    assert payload["required_cohort_failed"] is True
    assert payload["required_safety_cohort_failed"] is True
    assert payload["cohort_fan_out_global_timeout"] is True
    assert BudgetState.from_dict(payload) == bs


# ---------------------------------------------------------------------------
# AuditTrace (renamed fields per revised spec)
# ---------------------------------------------------------------------------


def test_audit_trace_defaults_round_trip():
    trace = AuditTrace()
    payload = trace.to_dict()
    assert payload["iterations_used"] == 0
    assert payload["fail_soft_engaged"] is False
    assert payload["cohort_outputs"] == []
    assert payload["tools_called_during_prep"] == []
    assert payload["budget_state"]["iterations_hit_limit"] is False
    assert AuditTrace.from_dict(payload) == trace


def test_audit_trace_with_telemetry_round_trip():
    trace = AuditTrace(
        cohort_outputs=("memory-cohort:r1", "weather-cohort:r1"),
        tools_called_during_prep=("inv:42",),
        iterations_used=3,
        budget_state=BudgetState(iterations_hit_limit=True),
        fail_soft_engaged=True,
        phase_durations_ms={"collect": 5, "filter": 12, "integrate": 220},
        notes="surfaced 2 cohort outputs; iterations hit limit",
    )
    payload = trace.to_dict()
    assert AuditTrace.from_dict(payload) == trace


def test_audit_trace_rejects_negative_iterations_used():
    with pytest.raises(BriefingValidationError, match="iterations_used"):
        AuditTrace(iterations_used=-1)


def test_audit_trace_rejects_negative_phase_duration():
    with pytest.raises(BriefingValidationError, match="phase_durations_ms"):
        AuditTrace(phase_durations_ms={"collect": -1})


def test_audit_trace_rejects_empty_cohort_output_reference():
    with pytest.raises(BriefingValidationError, match="cohort_outputs"):
        AuditTrace(cohort_outputs=("",))


def test_audit_trace_rejects_non_budget_state_value():
    with pytest.raises(BriefingValidationError, match="budget_state"):
        AuditTrace(budget_state="all_clear")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Briefing
# ---------------------------------------------------------------------------


def _build_briefing() -> Briefing:
    return Briefing(
        relevant_context=(
            ContextItem(
                source_type="cohort.memory",
                source_id="mem-1",
                summary="user prefers terse responses",
                confidence=0.9,
            ),
            ContextItem(
                source_type="cohort.weather",
                source_id="weather-cohort:r1",
                summary="local weather is clear and warm",
                confidence=0.7,
            ),
        ),
        filtered_context=(
            FilteredItem(
                source_type="cohort.patterns",
                source_id="patterns-cohort:r1",
                reason_filtered="not relevant to current short message",
            ),
        ),
        decided_action=RespondOnly(),
        presence_directive="answer concisely; user is in a quick-exchange mode",
        audit_trace=AuditTrace(
            cohort_outputs=("weather-cohort:r1", "patterns-cohort:r1"),
            iterations_used=1,
            phase_durations_ms={"collect": 3, "filter": 7, "integrate": 41},
        ),
        turn_id="turn-007",
        integration_run_id="run-aabb",
    )


def test_briefing_happy_path_round_trip_via_json():
    briefing = _build_briefing()
    payload = briefing.to_dict()
    raw = json.dumps(payload)
    parsed = Briefing.from_dict(json.loads(raw))
    assert parsed == briefing


def test_briefing_serialised_action_carries_kind_discriminator():
    briefing = _build_briefing()
    payload = briefing.to_dict()
    assert payload["decided_action"]["kind"] == "respond_only"


def test_briefing_with_execute_tool_action_round_trip():
    briefing = Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=ExecuteTool(
            tool_id="drive_read_doc",
            arguments={"file_id": "abc"},
            narration_context="reading the requested doc",
        ),
        presence_directive="execute and narrate",
        audit_trace=AuditTrace(iterations_used=2),
        action_envelope=ActionEnvelope(
            intended_outcome="read the requested document on the user's behalf",
            allowed_tool_classes=("drive",),
            allowed_operations=("read",),
        ),
    )
    parsed = Briefing.from_dict(briefing.to_dict())
    assert isinstance(parsed.decided_action, ExecuteTool)
    assert parsed.decided_action.tool_id == "drive_read_doc"
    assert parsed.action_envelope is not None
    assert parsed.action_envelope.intended_outcome.startswith("read")


def test_briefing_with_pivot_action_round_trip():
    briefing = Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=Pivot(
            reason="user-flagged sensitive topic",
            suggested_shape="acknowledge and redirect",
        ),
        presence_directive="redirect gently to an adjacent topic",
        audit_trace=AuditTrace(iterations_used=1),
    )
    parsed = Briefing.from_dict(briefing.to_dict())
    assert isinstance(parsed.decided_action, Pivot)
    assert parsed.decided_action.suggested_shape == "acknowledge and redirect"


def test_briefing_requires_non_empty_presence_directive():
    with pytest.raises(BriefingValidationError, match="presence_directive"):
        Briefing(
            relevant_context=(),
            filtered_context=(),
            decided_action=RespondOnly(),
            presence_directive="   ",
            audit_trace=AuditTrace(),
        )


def test_briefing_rejects_non_context_item_in_relevant_context():
    with pytest.raises(BriefingValidationError, match="relevant_context"):
        Briefing(
            relevant_context=("not-a-context-item",),  # type: ignore[arg-type]
            filtered_context=(),
            decided_action=RespondOnly(),
            presence_directive="x",
            audit_trace=AuditTrace(),
        )


def test_briefing_rejects_invalid_decided_action_type():
    with pytest.raises(BriefingValidationError, match="decided_action"):
        Briefing(
            relevant_context=(),
            filtered_context=(),
            decided_action="respond",  # type: ignore[arg-type]
            presence_directive="x",
            audit_trace=AuditTrace(),
        )


# ---------------------------------------------------------------------------
# Minimal fail-soft fallback
# ---------------------------------------------------------------------------


def test_minimal_fail_soft_briefing_shape():
    briefing = minimal_fail_soft_briefing(
        turn_id="t-1",
        integration_run_id="r-1",
        notes="iteration budget exhausted",
        budget_state=BudgetState(iterations_hit_limit=True),
    )
    assert briefing.relevant_context == ()
    assert briefing.filtered_context == ()
    assert isinstance(briefing.decided_action, RespondOnly)
    assert briefing.audit_trace.fail_soft_engaged is True
    assert briefing.audit_trace.budget_state.iterations_hit_limit is True
    assert briefing.audit_trace.notes == "iteration budget exhausted"
    assert "incomplete" in briefing.presence_directive.lower()


def test_minimal_fail_soft_briefing_round_trips():
    briefing = minimal_fail_soft_briefing(notes="timeout")
    parsed = Briefing.from_dict(briefing.to_dict())
    assert parsed == briefing


# ---------------------------------------------------------------------------
# Redaction-invariant smoke check
# ---------------------------------------------------------------------------


def test_briefing_to_dict_top_level_keys_are_closed_set():
    """Top-level Briefing fields are a closed set per spec Section 1.
    Pin to prevent a future schema drift sneaking a raw-content blob
    field in by accident."""
    briefing = _build_briefing()
    payload = briefing.to_dict()
    assert set(payload.keys()) == {
        "relevant_context",
        "filtered_context",
        "decided_action",
        "presence_directive",
        "audit_trace",
        "turn_id",
        "integration_run_id",
        "action_envelope",
    }


def test_audit_trace_serialisation_uses_references_not_raw_content():
    trace = AuditTrace(
        cohort_outputs=("cohort-a:r1",),
        tools_called_during_prep=("inv:1",),
    )
    payload = trace.to_dict()
    assert all(isinstance(r, str) for r in payload["cohort_outputs"])
    assert all(isinstance(r, str) for r in payload["tools_called_during_prep"])


def test_filtered_item_has_no_summary_field_per_revised_spec():
    """Negative pin: revised FilteredItem shape is exactly
    source_type / source_id / reason_filtered. Constructing with a
    summary kwarg must fail."""
    with pytest.raises(TypeError):
        FilteredItem(
            source_type="cohort.x",
            source_id="x",
            summary="should not exist",  # type: ignore[call-arg]
            reason_filtered="r",
        )


# ---------------------------------------------------------------------------
# PDI extensions: ClarificationPartialState
# ---------------------------------------------------------------------------


def test_clarification_partial_state_default_round_trip():
    state = ClarificationPartialState()
    payload = state.to_dict()
    assert payload == {
        "attempted_action_summary": "",
        "discovered_information": "",
        "blocking_ambiguity": "",
        "safe_question_context": "",
        "audit_refs": [],
    }
    assert ClarificationPartialState.from_dict(payload) == state


def test_clarification_partial_state_full_round_trip():
    state = ClarificationPartialState(
        attempted_action_summary="started drafting an email to Henry",
        discovered_information="user has two contacts named Henry",
        blocking_ambiguity="which Henry should receive the message",
        safe_question_context="confirming the recipient before sending",
        audit_refs=("evt-123", "evt-124"),
    )
    parsed = ClarificationPartialState.from_dict(state.to_dict())
    assert parsed == state


def test_clarification_partial_state_caps_attempted_action_summary():
    with pytest.raises(BriefingValidationError, match="attempted_action_summary"):
        ClarificationPartialState(attempted_action_summary="x" * 1001)


def test_clarification_partial_state_caps_discovered_information():
    with pytest.raises(BriefingValidationError, match="discovered_information"):
        ClarificationPartialState(discovered_information="x" * 1001)


def test_clarification_partial_state_caps_blocking_ambiguity():
    with pytest.raises(BriefingValidationError, match="blocking_ambiguity"):
        ClarificationPartialState(blocking_ambiguity="x" * 501)


def test_clarification_partial_state_caps_safe_question_context():
    with pytest.raises(BriefingValidationError, match="safe_question_context"):
        ClarificationPartialState(safe_question_context="x" * 501)


def test_clarification_partial_state_rejects_empty_audit_ref():
    with pytest.raises(BriefingValidationError, match="audit_refs"):
        ClarificationPartialState(audit_refs=("",))


# ---------------------------------------------------------------------------
# PDI extensions: ClarificationNeeded variant
# ---------------------------------------------------------------------------


def test_clarification_needed_first_pass_round_trip():
    action = ClarificationNeeded(
        question="Which calendar should I check?",
        ambiguity_type="target",
    )
    assert action.kind is ActionKind.CLARIFICATION_NEEDED
    payload = action.to_dict()
    assert payload["kind"] == "clarification_needed"
    assert payload["partial_state"] is None
    parsed = decided_action_from_dict(payload)
    assert isinstance(parsed, ClarificationNeeded)
    assert parsed.partial_state is None


def test_clarification_needed_b2_round_trip():
    state = ClarificationPartialState(
        attempted_action_summary="started drafting",
        discovered_information="two recipients match",
        blocking_ambiguity="which recipient",
        safe_question_context="confirming the recipient",
        audit_refs=("evt-1",),
    )
    action = ClarificationNeeded(
        question="Did you mean Henry from work or Henry from book club?",
        ambiguity_type="target",
        partial_state=state,
    )
    payload = action.to_dict()
    parsed = decided_action_from_dict(payload)
    assert isinstance(parsed, ClarificationNeeded)
    assert parsed.partial_state == state


def test_clarification_needed_requires_non_empty_question():
    with pytest.raises(BriefingValidationError, match="question"):
        ClarificationNeeded(question="   ", ambiguity_type="target")


def test_clarification_needed_caps_question_length():
    with pytest.raises(BriefingValidationError, match="question"):
        ClarificationNeeded(question="?" * 201, ambiguity_type="target")


def test_clarification_needed_rejects_unknown_ambiguity_type():
    with pytest.raises(BriefingValidationError, match="ambiguity_type"):
        ClarificationNeeded(question="?", ambiguity_type="not_a_kind")


def test_clarification_needed_accepts_all_ambiguity_types():
    for kind in ("target", "parameter", "approach", "intent", "other"):
        action = ClarificationNeeded(question="?", ambiguity_type=kind)
        assert action.ambiguity_type == kind


# ---------------------------------------------------------------------------
# PDI extensions: ActionEnvelope
# ---------------------------------------------------------------------------


def test_action_envelope_minimal_round_trip():
    envelope = ActionEnvelope(intended_outcome="send a quick confirmation email")
    payload = envelope.to_dict()
    parsed = ActionEnvelope.from_dict(payload)
    assert parsed == envelope


def test_action_envelope_full_round_trip():
    envelope = ActionEnvelope(
        intended_outcome="send a quick confirmation email",
        allowed_tool_classes=("email",),
        allowed_operations=("draft", "send"),
        constraints=("must include the meeting time",),
        confirmation_requirements=("send",),
        forbidden_moves=("channel_switch", "operation_escalation"),
    )
    parsed = ActionEnvelope.from_dict(envelope.to_dict())
    assert parsed == envelope


def test_action_envelope_requires_non_empty_intended_outcome():
    with pytest.raises(BriefingValidationError, match="intended_outcome"):
        ActionEnvelope(intended_outcome="   ")


def test_action_envelope_rejects_empty_string_in_collection():
    with pytest.raises(BriefingValidationError, match="allowed_operations"):
        ActionEnvelope(
            intended_outcome="x",
            allowed_operations=("",),
        )


def test_action_kind_requires_envelope_only_for_execute_tool():
    assert action_kind_requires_envelope(ActionKind.EXECUTE_TOOL) is True
    for other in (
        ActionKind.RESPOND_ONLY,
        ActionKind.PROPOSE_TOOL,
        ActionKind.CONSTRAINED_RESPONSE,
        ActionKind.PIVOT,
        ActionKind.DEFER,
        ActionKind.CLARIFICATION_NEEDED,
    ):
        assert action_kind_requires_envelope(other) is False


# ---------------------------------------------------------------------------
# PDI extensions: Briefing.action_envelope structural rule
# ---------------------------------------------------------------------------


def test_briefing_execute_tool_requires_action_envelope():
    with pytest.raises(BriefingValidationError, match="action_envelope"):
        Briefing(
            relevant_context=(),
            filtered_context=(),
            decided_action=ExecuteTool(
                tool_id="calendar_create_event",
                arguments={"title": "x"},
            ),
            presence_directive="execute and narrate",
            audit_trace=AuditTrace(),
        )


def test_briefing_respond_only_rejects_action_envelope():
    with pytest.raises(BriefingValidationError, match="action_envelope"):
        Briefing(
            relevant_context=(),
            filtered_context=(),
            decided_action=RespondOnly(),
            presence_directive="answer briefly",
            audit_trace=AuditTrace(),
            action_envelope=ActionEnvelope(intended_outcome="should not be here"),
        )


def test_briefing_propose_tool_rejects_action_envelope():
    """Per the design review edit: propose_tool is render-only on the thin path. The
    actual execute_tool that fires on the next turn carries its own
    envelope. propose_tool itself MUST NOT carry one."""
    with pytest.raises(BriefingValidationError, match="action_envelope"):
        Briefing(
            relevant_context=(),
            filtered_context=(),
            decided_action=ProposeTool(
                tool_id="email_send",
                arguments={"to": "x@example.com"},
                reason="user asked to email",
            ),
            presence_directive="render the proposal",
            audit_trace=AuditTrace(),
            action_envelope=ActionEnvelope(intended_outcome="should not be here"),
        )


def test_briefing_clarification_needed_round_trip():
    briefing = Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=ClarificationNeeded(
            question="Which calendar should I check?",
            ambiguity_type="target",
        ),
        presence_directive="ask the question naturally",
        audit_trace=AuditTrace(iterations_used=1),
    )
    parsed = Briefing.from_dict(briefing.to_dict())
    assert isinstance(parsed.decided_action, ClarificationNeeded)
    assert parsed.action_envelope is None
