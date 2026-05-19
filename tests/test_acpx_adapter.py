"""Substrate tests for kernos.kernel.external_agents.acpx_adapter.

This module is the actual substrate boundary the harness shims wrap.
ACPX (openclaw/acpx) speaks the Agent Client Protocol — these tests
pin the pieces that don't require the ``acpx`` binary itself:

  * ``derive_session_id`` — deterministic substrate-coordinate hash
  * NDJSON event parsing — ``_parse_ndjson_event`` shape discipline
  * ``_extract_agent_message_chunk`` — ACP envelope variants
  * ``_extract_stop_reason`` — JSON-RPC response detection
  * ``is_acpx_available`` — bring-up probe behavior
  * Alias map — ``claude_code/codex/gemini`` → ACPX agent names

Live ``dispatch`` end-to-end is exercised by the harness shim
TestClaudeLive / TestCodexLive when ``KERNOS_LIVE_AGENT_TESTS=1``.
"""
from __future__ import annotations

import shutil

import pytest

from kernos.kernel.external_agents import acpx_adapter
from kernos.kernel.external_agents.acpx_adapter import (
    SUPPORTED_TARGETS,
    _ParseFailure,
    _extract_agent_message_chunk,
    _extract_stop_reason,
    _parse_ndjson_event,
    derive_session_id,
    is_acpx_available,
)


# ===========================================================================
# derive_session_id — substrate-coordinate hash discipline
# ===========================================================================


class TestDeriveSessionId:
    def test_deterministic_for_same_coordinates(self):
        a = derive_session_id(
            instance_id="kernos-prod",
            target="claude_code",
            member_id="m1",
            conversation_id="conv-42",
        )
        b = derive_session_id(
            instance_id="kernos-prod",
            target="claude_code",
            member_id="m1",
            conversation_id="conv-42",
        )
        assert a == b
        assert a  # not empty

    def test_distinct_for_different_targets(self):
        a = derive_session_id(
            instance_id="i", target="claude_code", member_id="m",
            conversation_id="c",
        )
        b = derive_session_id(
            instance_id="i", target="codex", member_id="m",
            conversation_id="c",
        )
        assert a != b

    def test_distinct_for_different_conversations(self):
        a = derive_session_id(
            instance_id="i", target="claude_code", member_id="m",
            conversation_id="c1",
        )
        b = derive_session_id(
            instance_id="i", target="claude_code", member_id="m",
            conversation_id="c2",
        )
        assert a != b

    def test_distinct_for_different_members(self):
        a = derive_session_id(
            instance_id="i", target="claude_code", member_id="m1",
            conversation_id="c",
        )
        b = derive_session_id(
            instance_id="i", target="claude_code", member_id="m2",
            conversation_id="c",
        )
        assert a != b

    def test_returns_16_char_prefix(self):
        # Architect call: 16-char prefix fits ACPX session storage
        # comfortably and stays operator-readable in logs.
        s = derive_session_id(
            instance_id="kernos-prod",
            target="claude_code",
            member_id="member-1",
            conversation_id="conv-abc",
        )
        assert len(s) == 16
        # Sanitized hex chars only (lowercase 0-9a-f)
        assert all(c in "0123456789abcdef" for c in s)

    def test_blank_member_and_conv_still_produces_id(self):
        # Some dispatches are out-of-conversation (system bring-up,
        # health check). Empty member_id/conversation_id should still
        # produce a valid id, not blow up.
        s = derive_session_id(
            instance_id="kernos-prod", target="claude_code",
        )
        assert len(s) == 16

    def test_empty_instance_and_target_produces_empty_or_constant(self):
        # Defensive: if caller passes nothing meaningful, we still
        # return a string (possibly empty) rather than raise.
        s = derive_session_id(instance_id="", target="")
        assert isinstance(s, str)


# ===========================================================================
# NDJSON parse discipline (Codex review folds #4 + #5)
# ===========================================================================


class TestParseNdjsonEvent:
    def test_parses_valid_json_object(self):
        out = _parse_ndjson_event('{"method": "session/update"}')
        assert out == {"method": "session/update"}

    def test_blank_line_returns_none(self):
        assert _parse_ndjson_event("") is None
        assert _parse_ndjson_event("   ") is None
        assert _parse_ndjson_event("\t\n") is None

    def test_malformed_json_raises_parse_failure(self):
        with pytest.raises(_ParseFailure):
            _parse_ndjson_event("not-json{")

    def test_valid_json_non_dict_raises_parse_failure(self):
        # Fold #4: bare strings/arrays/numbers are valid JSON but
        # wrong shape; downstream .get() would crash.
        with pytest.raises(_ParseFailure):
            _parse_ndjson_event('[1, 2, 3]')
        with pytest.raises(_ParseFailure):
            _parse_ndjson_event('"just a string"')
        with pytest.raises(_ParseFailure):
            _parse_ndjson_event('42')
        with pytest.raises(_ParseFailure):
            _parse_ndjson_event('null')


class TestExtractAgentMessageChunk:
    def test_canonical_acp_session_update_text(self):
        event = {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "hello"},
                },
            },
        }
        assert _extract_agent_message_chunk(event) == "hello"

    def test_acp_session_update_value_variant(self):
        # Some adapters wrap as {value: ...} rather than {text: ...}
        event = {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text_delta", "value": "world"},
                },
            },
        }
        assert _extract_agent_message_chunk(event) == "world"

    def test_forward_compat_top_level_text_delta(self):
        event = {"type": "text_delta", "delta": "frag"}
        assert _extract_agent_message_chunk(event) == "frag"

    def test_unrelated_event_returns_none(self):
        assert _extract_agent_message_chunk(
            {"method": "tool/call", "params": {"x": 1}}
        ) is None
        assert _extract_agent_message_chunk({}) is None

    def test_malformed_nested_shapes_dont_crash(self):
        # Fold #4: each nesting level dict-checked explicitly so
        # malformed envelopes degrade silently instead of crashing
        # the streaming drain task.
        evil_shapes = [
            {"method": "session/update", "params": "not-a-dict"},
            {"method": "session/update", "params": {"update": "not-a-dict"}},
            {"method": "session/update", "params": {"update": {
                "sessionUpdate": "agent_message_chunk",
                "content": "not-a-dict",
            }}},
            {"method": "session/update", "params": {"update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"text": 42},  # text not a string
            }}},
            {"type": "text_delta", "delta": {"nested": "wrong"}},
        ]
        for ev in evil_shapes:
            assert _extract_agent_message_chunk(ev) is None


class TestExtractStopReason:
    def test_recognizes_jsonrpc_result_stop_reason(self):
        event = {"id": 1, "result": {"stopReason": "end_turn"}}
        assert _extract_stop_reason(event) == "end_turn"

    def test_other_stop_reason_values(self):
        for reason in ["max_tokens", "stop_sequence", "tool_use", "refusal"]:
            event = {"result": {"stopReason": reason}}
            assert _extract_stop_reason(event) == reason

    def test_missing_result_returns_none(self):
        assert _extract_stop_reason({"method": "session/update"}) is None

    def test_result_without_stop_reason_returns_none(self):
        assert _extract_stop_reason({"result": {"other": "thing"}}) is None

    def test_non_string_stop_reason_ignored(self):
        # Defensive: a numeric or null stopReason shouldn't be
        # surfaced as if completion happened.
        assert _extract_stop_reason({"result": {"stopReason": 42}}) is None
        assert _extract_stop_reason({"result": {"stopReason": None}}) is None

    def test_empty_string_stop_reason_treated_as_not_complete(self):
        assert _extract_stop_reason({"result": {"stopReason": ""}}) is None


# ===========================================================================
# Bring-up probe — is_acpx_available
# ===========================================================================


class TestIsAcpxAvailable:
    def test_returns_tuple_of_bool_and_string(self):
        ok, detail = is_acpx_available()
        assert isinstance(ok, bool)
        assert isinstance(detail, str)

    def test_returns_false_when_binary_missing(self, monkeypatch):
        # Force the binary lookup to miss
        monkeypatch.setattr(acpx_adapter, "_acpx_binary", lambda: "")
        ok, detail = is_acpx_available()
        assert ok is False
        assert detail  # has a reason string


# ===========================================================================
# Alias map / supported targets
# ===========================================================================


class TestSupportedTargets:
    def test_includes_three_canonical_names(self):
        assert "claude_code" in SUPPORTED_TARGETS
        assert "codex" in SUPPORTED_TARGETS
        assert "gemini" in SUPPORTED_TARGETS

    def test_supported_targets_are_strings(self):
        assert all(isinstance(t, str) for t in SUPPORTED_TARGETS)
