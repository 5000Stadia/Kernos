"""Pin tests for RESPONSE-FIDELITY-V1 conv-log receipt formatter.

Covers Batch 0 (failed-attempts persist) AND Batch 1.4 (G.7 conv-log
shift to "Action state this turn" with structured per-record fields
from ActionStateRecord).

Substrate behavior verified:

  - Failed attempts persist (not filtered out) — closes C.7 from
    the Phase 1 audit.
  - Successful and failed entries are distinguishable via the
    ``state=completed`` / ``state=failed`` marker.
  - When ActionStateRecord matches a tool_calls_trace entry by
    operation name, the structured render takes precedence (full
    record fields visible to next-turn agent).
  - Trace-only entries (existing surfaces pre-migration) fall back
    to the legacy state+preview format.
  - Block label is "Action state this turn" (G.7 shift).
"""
from __future__ import annotations

from kernos.kernel.integration import ActionStateRecord
from kernos.messages.phases.persist import _format_tool_receipts


def test_empty_returns_none():
    """No tool calls AND no records → no receipt block."""
    assert _format_tool_receipts(None) is None
    assert _format_tool_receipts([]) is None
    assert _format_tool_receipts([], []) is None


def test_block_label_is_action_state_this_turn():
    """G.7 shift: block label changed from "Tool effects" to
    "Action state this turn" with structured per-record fields."""
    trace = [
        {"name": "list_files", "success": True, "result_preview": "1 file"},
    ]
    block = _format_tool_receipts(trace)
    assert block is not None
    assert block.startswith("Action state this turn:\n")


def test_successful_trace_only_entries_use_state_completed():
    """Trace entries without matching ActionStateRecord render with
    ``state=completed`` (G.7 shift from "succeeded:")."""
    trace = [
        {"name": "list_files", "success": True, "result_preview": "1 file"},
        {"name": "read_file", "success": True, "result_preview": "hello"},
    ]
    block = _format_tool_receipts(trace)
    assert block is not None
    assert "[list_files] state=completed | 1 file" in block
    assert "[read_file] state=completed | hello" in block


def test_failed_entries_persist_with_state_failed():
    """The Batch 0 fix: failed attempts persist with explicit
    ``state=failed`` marker."""
    trace = [
        {
            "name": "send_to_channel",
            "success": False,
            "result_preview": "Error: Twilio auth failed",
        },
    ]
    block = _format_tool_receipts(trace)
    assert block is not None
    assert "[send_to_channel] state=failed | Error: Twilio auth failed" in block


def test_mixed_success_and_failure_both_persist_distinguishably():
    """Mixed turn: success and failure both land in the block, each
    with its own state marker."""
    trace = [
        {"name": "list_files", "success": True, "result_preview": "2 files"},
        {
            "name": "write_file",
            "success": False,
            "result_preview": "Error: read-only",
        },
        {"name": "read_file", "success": True, "result_preview": "content"},
    ]
    block = _format_tool_receipts(trace)
    assert block is not None
    assert "[list_files] state=completed | 2 files" in block
    assert "[write_file] state=failed | Error: read-only" in block
    assert "[read_file] state=completed | content" in block


def test_action_state_record_takes_precedence_over_trace():
    """When a tool_calls_trace entry has a matching ActionStateRecord
    (by operation name), the structured render takes precedence —
    state, affected_objects, user_visible_summary, evidence_class
    all surface."""
    trace = [
        {
            "name": "note_this",
            "success": True,
            "result_preview": "Noted (fact). subject=pets id=know_xyz",
        },
    ]
    record = ActionStateRecord(
        action_id="act_1",
        surface="memory",
        operation="note_this",
        operation_class="mutate",
        authorization_state="not_required",
        execution_state="completed",
        affected_objects=("know_xyz",),
        user_visible_summary="Noted as fact: pets",
    )
    block = _format_tool_receipts(trace, [record])
    assert block is not None
    line = [l for l in block.split("\n") if l.startswith("[note_this]")][0]
    assert "state=completed" in line
    assert "objects=know_xyz" in line
    assert "Noted as fact" in line


def test_record_evidence_class_surfaces_when_set():
    """Read-only paths (Batch 2 onward will populate evidence_class)
    surface the evidence class explicitly so next-turn agent can
    reason about source-of-claim."""
    record = ActionStateRecord(
        action_id="act_2",
        surface="canvas",
        operation="page_read",
        operation_class="read",
        authorization_state="not_required",
        execution_state="completed",
        evidence_class="page_read",
        user_visible_summary="Read concepts/canvas.md",
    )
    block = _format_tool_receipts([], [record])
    assert block is not None
    assert "evidence=page_read" in block


def test_record_without_matching_trace_still_renders():
    """Records populated outside the dispatcher path (rare but
    possible) still render. Defensive shape — never silently drop
    a substrate-authoritative record."""
    record = ActionStateRecord(
        action_id="act_3",
        surface="memory",
        operation="background_harvest",
        operation_class="mutate",
        authorization_state="not_required",
        execution_state="completed",
    )
    block = _format_tool_receipts([], [record])
    assert block is not None
    assert "[background_harvest] state=completed" in block


def test_failed_entry_with_empty_preview_still_persists():
    """Failed entries persist even without a preview — the fact of
    the attempt is itself signal. Successful entries without preview
    are filtered (legacy contract: success+preview is the surfacing
    pair for trace-only entries)."""
    trace = [
        {"name": "manage_covenants", "success": False, "result_preview": ""},
        {"name": "remember", "success": True, "result_preview": ""},
    ]
    block = _format_tool_receipts(trace)
    assert block is not None
    assert "[manage_covenants] state=failed" in block
    # Successful-without-preview filtered (preserves legacy shape).
    assert "[remember]" not in block


def test_entry_with_no_name_is_skipped():
    """Trace entries lacking ``name`` are noise; skip them."""
    trace = [
        {"name": "", "success": True, "result_preview": "x"},
        {"name": "list_files", "success": True, "result_preview": "ok"},
    ]
    block = _format_tool_receipts(trace)
    assert block is not None
    assert "[list_files]" in block
    assert "[]" not in block


def test_long_preview_is_truncated():
    """Receipt previews are capped at 150 chars."""
    long_preview = "x" * 500
    trace = [
        {"name": "tool", "success": True, "result_preview": long_preview},
    ]
    block = _format_tool_receipts(trace)
    assert block is not None
    line = [l for l in block.split("\n") if l.startswith("[tool]")][0]
    assert "x" * 150 in line
    assert "x" * 151 not in line
