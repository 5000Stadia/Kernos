"""ACPX_TIMEOUT_DIAGNOSTICS (2026-05-22) acceptance tests.

The investigation of the prior 600s ConsultationTimeout incident
landed on "we have no evidence — instrument the next one." This
test pins the diagnostic writer's contract so the next timeout
produces a useful friction report.
"""
from __future__ import annotations

import time
from pathlib import Path

from kernos.kernel.external_agents.acpx_adapter import (
    _write_acpx_timeout_friction_report,
)


def test_writes_friction_report_with_no_events(tmp_path, monkeypatch):
    """When dispatch timed out without receiving any events,
    the report should call that out as the most-likely cause."""
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))

    path = _write_acpx_timeout_friction_report(
        target="codex",
        acpx_agent_name="codex",
        session_id="",
        timeout_seconds=600,
        dispatch_started_at=time.monotonic() - 600,
        last_event_at=None,
        last_event_kind="",
        event_count=0,
        parse_errors=0,
        stdout_errors=[],
        stderr_chunks=[b"some stderr line\n", b"another\n"],
        last_stop_reason="",
        workspace_dir="/tmp/ws",
        prompt_preview="test prompt",
    )
    assert path
    text = Path(path).read_text(encoding="utf-8")
    assert "ACPX_TIMEOUT_CODEX" in text
    assert "events_received: `0`" in text
    assert "No events ever received" in text
    assert "some stderr line" in text


def test_writes_friction_report_with_stalled_mid_stream(
    tmp_path, monkeypatch,
):
    """Mid-stream stall: events flowed then silence > 50% of timeout.
    Report should call out the stall."""
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))

    now = time.monotonic()
    path = _write_acpx_timeout_friction_report(
        target="claude_code",
        acpx_agent_name="claude-code",
        session_id="sess_abc",
        timeout_seconds=600,
        dispatch_started_at=now - 600,
        last_event_at=now - 400,  # 400s ago, > 300s (50% of timeout)
        last_event_kind="tool_use",
        event_count=23,
        parse_errors=1,
        stdout_errors=[],
        stderr_chunks=[],
        last_stop_reason="",
        workspace_dir="/tmp/ws",
        prompt_preview="long prompt",
    )
    text = Path(path).read_text(encoding="utf-8")
    assert "Stalled mid-stream" in text
    assert "events_received: `23`" in text
    assert "last_event_kind: `tool_use`" in text


def test_writes_friction_report_with_stdout_errors(tmp_path, monkeypatch):
    """JSON-RPC error channel populated: report mentions it
    AND lists the errors."""
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))

    path = _write_acpx_timeout_friction_report(
        target="codex",
        acpx_agent_name="codex",
        session_id="",
        timeout_seconds=600,
        dispatch_started_at=time.monotonic() - 600,
        last_event_at=None,
        last_event_kind="",
        event_count=0,
        parse_errors=0,
        stdout_errors=["Credit balance too low", "auth refused"],
        stderr_chunks=[],
        last_stop_reason="",
        workspace_dir="/tmp/ws",
        prompt_preview="",
    )
    text = Path(path).read_text(encoding="utf-8")
    assert "stdout error channel populated" in text
    assert "Credit balance too low" in text


def test_returns_empty_string_when_no_data_dir_writable(
    tmp_path, monkeypatch,
):
    """If the friction directory can't be created (e.g. wrong env),
    writer returns empty string instead of raising."""
    # Point to a path that can't be created (subdir of a file)
    bad_path = tmp_path / "not_a_dir.txt"
    bad_path.write_text("file in the way")
    monkeypatch.setenv("KERNOS_DATA_DIR", str(bad_path))

    path = _write_acpx_timeout_friction_report(
        target="codex",
        acpx_agent_name="codex",
        session_id="",
        timeout_seconds=600,
        dispatch_started_at=time.monotonic(),
        last_event_at=None,
        last_event_kind="",
        event_count=0,
        parse_errors=0,
        stdout_errors=[],
        stderr_chunks=[],
        last_stop_reason="",
        workspace_dir="/tmp/ws",
        prompt_preview="",
    )
    assert path == ""
