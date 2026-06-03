"""AGENT-CONSULT-CHANNEL-V1 Stage 1b: structured failure diagnostics."""
from kernos.kernel.external_agents.errors import (
    ConsultationFailed,
    ConsultationStalled,
    consultation_diagnostics,
)


def test_diagnostics_from_stalled_carries_structured_context():
    exc = ConsultationStalled(
        "stalled", last_event_kind="tool_call",
        silence_seconds=661.0, event_count=202, attempts=2,
        last_reason="idle_stall",
    )
    d = consultation_diagnostics(exc)
    assert d["exc_type"] == "ConsultationStalled"
    assert d["last_event_kind"] == "tool_call"
    assert d["silence_seconds"] == 661.0
    assert d["event_count"] == 202


def test_diagnostics_from_failed_carries_exit_status():
    d = consultation_diagnostics(ConsultationFailed("boom", exit_status=128))
    assert d["exc_type"] == "ConsultationFailed"
    assert d["exit_status"] == 128
    assert "boom" in d["message"]


def test_diagnostics_safe_on_plain_exception():
    d = consultation_diagnostics(ValueError("plain"))
    assert d["exc_type"] == "ValueError" and "plain" in d["message"]
