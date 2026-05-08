"""Pin tests for RESPONSE-FIDELITY-V1 Batch 1: ActionStateRecord schema.

Verifies the substrate primitive that bridges substrate truth and
renderer language. Schema validation, round-trip via to_dict/from_dict,
AuditTrace integration. Per Batch 1 spec: this is the schema test;
renderer rules and per-surface migration are Batch 2 onward.
"""
from __future__ import annotations

import pytest

from kernos.kernel.integration import (
    ACTION_AUTHORIZATION_STATES,
    ACTION_EVIDENCE_CLASSES,
    ACTION_EXECUTION_STATES,
    ACTION_OPERATION_CLASSES,
    ACTION_RISK_LEVELS,
    ActionStateRecord,
    AuditTrace,
    BriefingValidationError,
)


# ---------------------------------------------------------------------------
# Construction + validation
# ---------------------------------------------------------------------------


def test_minimal_construction():
    """Required fields only — defaults fill the rest."""
    rec = ActionStateRecord(
        action_id="act_1",
        surface="memory",
        operation="note_this",
        operation_class="mutate",
        authorization_state="not_required",
        execution_state="completed",
    )
    assert rec.action_id == "act_1"
    assert rec.surface == "memory"
    assert rec.receipt_refs == ()
    assert rec.affected_objects == ()
    assert rec.partial_state is None
    assert rec.user_visible_summary == ""
    assert rec.risk_level == "low"
    assert rec.evidence_class == ""
    assert rec.missing_metadata is False


def test_full_construction():
    """All fields populated."""
    rec = ActionStateRecord(
        action_id="act_xyz",
        surface="calendar",
        operation="schedule_event",
        operation_class="schedule",
        authorization_state="confirmed",
        execution_state="completed",
        receipt_refs=("schedule_event:iter1",),
        affected_objects=("event_abc",),
        partial_state=None,
        user_visible_summary="Created lunch event tomorrow at 12pm",
        risk_level="medium",
        evidence_class="",
        missing_metadata=False,
    )
    assert rec.receipt_refs == ("schedule_event:iter1",)
    assert rec.affected_objects == ("event_abc",)
    assert rec.user_visible_summary.startswith("Created")


def test_partial_state_dict():
    """partial_state carries a dict for partial executions."""
    rec = ActionStateRecord(
        action_id="act_p",
        surface="calendar",
        operation="schedule_batch",
        operation_class="schedule",
        authorization_state="confirmed",
        execution_state="partial",
        partial_state={
            "completed": ["event_a", "event_b"],
            "failed": [{"event_id": "event_c", "reason": "conflict"}],
        },
    )
    assert rec.partial_state is not None
    assert "event_a" in rec.partial_state["completed"]


# ---------------------------------------------------------------------------
# Validation rejections
# ---------------------------------------------------------------------------


def test_empty_action_id_rejected():
    with pytest.raises(BriefingValidationError, match="action_id"):
        ActionStateRecord(
            action_id="",
            surface="memory",
            operation="note_this",
            operation_class="mutate",
            authorization_state="not_required",
            execution_state="completed",
        )


def test_invalid_operation_class_rejected():
    with pytest.raises(BriefingValidationError, match="operation_class"):
        ActionStateRecord(
            action_id="act_1",
            surface="memory",
            operation="note_this",
            operation_class="invalid_class",
            authorization_state="not_required",
            execution_state="completed",
        )


def test_invalid_authorization_state_rejected():
    with pytest.raises(BriefingValidationError, match="authorization_state"):
        ActionStateRecord(
            action_id="act_1",
            surface="memory",
            operation="note_this",
            operation_class="mutate",
            authorization_state="weird",
            execution_state="completed",
        )


def test_invalid_execution_state_rejected():
    with pytest.raises(BriefingValidationError, match="execution_state"):
        ActionStateRecord(
            action_id="act_1",
            surface="memory",
            operation="note_this",
            operation_class="mutate",
            authorization_state="not_required",
            execution_state="indeterminate",
        )


def test_invalid_risk_level_rejected():
    with pytest.raises(BriefingValidationError, match="risk_level"):
        ActionStateRecord(
            action_id="act_1",
            surface="memory",
            operation="note_this",
            operation_class="mutate",
            authorization_state="not_required",
            execution_state="completed",
            risk_level="extreme",
        )


def test_invalid_evidence_class_rejected():
    with pytest.raises(BriefingValidationError, match="evidence_class"):
        ActionStateRecord(
            action_id="act_1",
            surface="memory",
            operation="note_this",
            operation_class="mutate",
            authorization_state="not_required",
            execution_state="completed",
            evidence_class="garbage",
        )


def test_partial_state_must_be_dict_or_none():
    with pytest.raises(BriefingValidationError, match="partial_state"):
        ActionStateRecord(
            action_id="act_1",
            surface="memory",
            operation="note_this",
            operation_class="mutate",
            authorization_state="not_required",
            execution_state="partial",
            partial_state="should be dict",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Vocabulary constants
# ---------------------------------------------------------------------------


def test_vocabulary_constants_present():
    """The categorical-field vocabulary is exported. Downstream
    consumers (renderer rules in Batch 2 onward) refer to these
    constants instead of stringly-typed literals."""
    assert "read" in ACTION_OPERATION_CLASSES
    assert "mutate" in ACTION_OPERATION_CLASSES
    assert "send" in ACTION_OPERATION_CLASSES
    assert "schedule" in ACTION_OPERATION_CLASSES
    assert "completed" in ACTION_EXECUTION_STATES
    assert "partial" in ACTION_EXECUTION_STATES
    assert "blocked" in ACTION_EXECUTION_STATES
    assert "failed" in ACTION_EXECUTION_STATES
    assert "confirmed" in ACTION_AUTHORIZATION_STATES
    assert "high" in ACTION_RISK_LEVELS
    assert "search_hit" in ACTION_EVIDENCE_CLASSES


# ---------------------------------------------------------------------------
# Round-trip via to_dict / from_dict
# ---------------------------------------------------------------------------


def test_round_trip_minimal():
    rec = ActionStateRecord(
        action_id="act_1",
        surface="memory",
        operation="note_this",
        operation_class="mutate",
        authorization_state="not_required",
        execution_state="completed",
    )
    revived = ActionStateRecord.from_dict(rec.to_dict())
    assert revived == rec


def test_round_trip_full():
    rec = ActionStateRecord(
        action_id="act_xyz",
        surface="calendar",
        operation="schedule_batch",
        operation_class="schedule",
        authorization_state="confirmed",
        execution_state="partial",
        receipt_refs=("schedule_event:iter1", "schedule_event:iter2"),
        affected_objects=("event_a", "event_b"),
        partial_state={"completed": ["event_a"], "failed": ["event_b"]},
        user_visible_summary="One scheduled, one blocked by conflict",
        risk_level="medium",
        evidence_class="",
        missing_metadata=False,
    )
    revived = ActionStateRecord.from_dict(rec.to_dict())
    assert revived == rec


def test_round_trip_via_audit_trace():
    """ActionStateRecords land on AuditTrace; the trace's own
    to_dict/from_dict round-trips them along with the rest of the
    audit shape."""
    rec_a = ActionStateRecord(
        action_id="act_a",
        surface="memory",
        operation="note_this",
        operation_class="mutate",
        authorization_state="not_required",
        execution_state="completed",
        affected_objects=("know_abc",),
    )
    rec_b = ActionStateRecord(
        action_id="act_b",
        surface="canvas",
        operation="page_read",
        operation_class="read",
        authorization_state="not_required",
        execution_state="completed",
        evidence_class="page_read",
    )
    trace = AuditTrace(action_state_records=(rec_a, rec_b))
    revived = AuditTrace.from_dict(trace.to_dict())
    assert revived.action_state_records == (rec_a, rec_b)


# ---------------------------------------------------------------------------
# AuditTrace integration
# ---------------------------------------------------------------------------


def test_audit_trace_default_action_state_records_is_empty_tuple():
    """Default empty so existing AuditTrace constructions stay
    valid; new field is opt-in for callers that populate it."""
    trace = AuditTrace()
    assert trace.action_state_records == ()


def test_audit_trace_rejects_non_record_entries():
    with pytest.raises(BriefingValidationError, match="ActionStateRecord"):
        AuditTrace(action_state_records=({"not": "a record"},))  # type: ignore[arg-type]


def test_audit_trace_to_dict_includes_action_state_records():
    rec = ActionStateRecord(
        action_id="act_1",
        surface="memory",
        operation="note_this",
        operation_class="mutate",
        authorization_state="not_required",
        execution_state="completed",
    )
    trace = AuditTrace(action_state_records=(rec,))
    serialized = trace.to_dict()
    assert "action_state_records" in serialized
    assert len(serialized["action_state_records"]) == 1
    assert serialized["action_state_records"][0]["action_id"] == "act_1"
