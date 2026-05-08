"""Pin tests for RESPONSE-FIDELITY-V1 Batch 0 (failed-attempts in conv log).

Tests the receipt-formatter helper in kernos.messages.phases.persist —
the substrate behavior that the spec's embedded live test calls for:

  - Failed attempts persist (not filtered out).
  - Successful and failed entries are distinguishable in the formatted
    block so the next-turn agent can reason about both classes.
  - Existing behavior for successful entries is preserved.

Closes cross-surface pattern C.7 from the Phase 1 audit. The G.7
conv-log-shift ("Tool effects this turn" → "Action state this turn"
with structured per-record ActionStateRecord fields) is Batch 1 work
and explicitly NOT in scope here — this batch's tests verify only
that failed-attempt data stops getting filtered on the floor.
"""
from __future__ import annotations

from kernos.messages.phases.persist import _format_tool_receipts


def test_empty_trace_returns_none():
    """No tool calls → no receipt block. Persist phase skips the
    conv-log append entirely."""
    assert _format_tool_receipts(None) is None
    assert _format_tool_receipts([]) is None


def test_successful_entries_format_with_succeeded_marker():
    """Successful entries include the ``succeeded`` status marker.
    This IS a behavior change from pre-Batch-0 (which used no marker
    at all because there was only one class to display). The marker
    is the distinguishing-feature the spec asks for."""
    trace = [
        {"name": "list_files", "success": True, "result_preview": "1 file"},
        {"name": "read_file", "success": True, "result_preview": "hello"},
    ]
    block = _format_tool_receipts(trace)
    assert block is not None
    assert block.startswith("Tool effects this turn:\n")
    assert "[list_files] succeeded: 1 file" in block
    assert "[read_file] succeeded: hello" in block


def test_failed_entries_persist_with_failed_marker():
    """The Batch 0 fix: failed attempts persist (not filtered out)
    and carry an explicit ``failed`` status marker so the next-turn
    agent can reason about attempted-and-failed records."""
    trace = [
        {
            "name": "send_to_channel",
            "success": False,
            "result_preview": "Error: Twilio auth failed",
        },
    ]
    block = _format_tool_receipts(trace)
    assert block is not None
    assert "[send_to_channel] failed: Error: Twilio auth failed" in block


def test_mixed_success_and_failure_both_persist_distinguishably():
    """Mixed turn: one tool succeeded, another failed. Both land in
    the receipt block. Status markers make them distinguishable for
    downstream consumers (next-turn agent reading conv log)."""
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
    assert "[list_files] succeeded: 2 files" in block
    assert "[write_file] failed: Error: read-only" in block
    assert "[read_file] succeeded: content" in block


def test_failed_entry_with_empty_preview_still_persists():
    """Failed entries persist even without a preview — the fact of
    the attempt is itself signal. ('Tried X and got nothing back' is
    meaningfully different from 'never tried.') Successful entries
    without preview are filtered (legacy contract: success+preview is
    the surfacing pair for the trace)."""
    trace = [
        {"name": "manage_covenants", "success": False, "result_preview": ""},
        {"name": "remember", "success": True, "result_preview": ""},
    ]
    block = _format_tool_receipts(trace)
    assert block is not None
    assert "[manage_covenants] failed" in block
    # Successful-without-preview filtered (preserves legacy shape).
    assert "[remember]" not in block


def test_entry_with_no_name_is_skipped():
    """Trace entries lacking ``name`` are noise; skip them. Defensive
    against malformed dispatcher output."""
    trace = [
        {"name": "", "success": True, "result_preview": "x"},
        {"name": "list_files", "success": True, "result_preview": "ok"},
    ]
    block = _format_tool_receipts(trace)
    assert block is not None
    assert "[list_files]" in block
    # Empty-name entry got skipped.
    assert "[]" not in block


def test_long_preview_is_truncated():
    """Receipt previews are capped at 150 chars (legacy contract)
    to keep conv-log entries from ballooning."""
    long_preview = "x" * 500
    trace = [
        {"name": "tool", "success": True, "result_preview": long_preview},
    ]
    block = _format_tool_receipts(trace)
    assert block is not None
    # 150 chars of the preview at most, plus formatting overhead.
    line = [l for l in block.split("\n") if l.startswith("[tool]")][0]
    assert "x" * 150 in line
    assert "x" * 151 not in line
