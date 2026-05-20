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


class TestStaleSessionDetection:
    """2026-05-19 live-bug pin: ACPX named sessions can go stale
    when the bound agent process dies. `sessions ensure` reports
    the session "exists" so subsequent dispatch hits stderr
    'agent needs reconnect' with rc=1. There's no `sessions
    reset` — close + re-ensure is the only path. dispatch() now
    auto-retries once on this exact failure shape."""

    def test_marker_detected_in_real_stderr(self):
        from kernos.kernel.external_agents.acpx_adapter import (
            _stderr_indicates_stale_agent,
        )
        # Exact string captured from server.log during the 2026-05-19
        # bug report.
        live_stderr = (
            "[acpx] session 5ffe4c7047de1deb "
            "(6e22e50b-072a-4cb3-9edf-5cc7551197a0) · "
            "/home/k/Kernos-main · agent needs reconnect\n"
        )
        assert _stderr_indicates_stale_agent(live_stderr) is True

    def test_marker_case_insensitive(self):
        from kernos.kernel.external_agents.acpx_adapter import (
            _stderr_indicates_stale_agent,
        )
        assert _stderr_indicates_stale_agent(
            "AGENT NEEDS RECONNECT"
        ) is True

    def test_alternative_marker(self):
        from kernos.kernel.external_agents.acpx_adapter import (
            _stderr_indicates_stale_agent,
        )
        assert _stderr_indicates_stale_agent(
            "[acpx] agent disconnected from session foo"
        ) is True

    def test_empty_stderr_not_detected(self):
        from kernos.kernel.external_agents.acpx_adapter import (
            _stderr_indicates_stale_agent,
        )
        assert _stderr_indicates_stale_agent("") is False

    def test_unrelated_error_not_detected(self):
        from kernos.kernel.external_agents.acpx_adapter import (
            _stderr_indicates_stale_agent,
        )
        # Must NOT false-positive on generic errors
        assert _stderr_indicates_stale_agent(
            "connection refused"
        ) is False
        assert _stderr_indicates_stale_agent(
            "Error: missing required argument"
        ) is False
        assert _stderr_indicates_stale_agent(
            "permission denied"
        ) is False


class TestConsultSchemaValidation:
    """2026-05-19 live-bug pin: agent called consult with
    harness='' (empty string). JSON schema accepted because string
    type allows empty. Now harness + question are minLength: 1 so
    schema-validation layer rejects empty before dispatch reaches
    the registry."""

    def test_harness_field_has_min_length_1(self):
        from kernos.kernel.external_agents.tool import CONSULT_TOOL
        props = CONSULT_TOOL["input_schema"]["properties"]
        assert props["harness"].get("minLength") == 1

    def test_question_field_has_min_length_1(self):
        from kernos.kernel.external_agents.tool import CONSULT_TOOL
        props = CONSULT_TOOL["input_schema"]["properties"]
        assert props["question"].get("minLength") == 1

    def test_harness_description_advertises_examples(self):
        """Description should name actual harnesses so the agent
        knows what to pass instead of guessing or leaving empty."""
        from kernos.kernel.external_agents.tool import CONSULT_TOOL
        props = CONSULT_TOOL["input_schema"]["properties"]
        desc = props["harness"]["description"]
        for name in ("claude_code", "codex", "gemini"):
            assert name in desc

    def test_harness_enum_locks_valid_values(self):
        """2026-05-20 founder push: 'How do we get kernos to freaking
        talk to codex and claude code'. The live agent kept fumbling
        with empty harness, wrong tool names (external_agent_consult.cc,
        codex_async_advisory). enum on the harness field hard-rejects
        bad values at schema-validation time, before the call dispatches.
        """
        from kernos.kernel.external_agents.tool import CONSULT_TOOL
        props = CONSULT_TOOL["input_schema"]["properties"]
        enum = props["harness"].get("enum")
        assert enum == ["claude_code", "codex", "gemini"], (
            f"harness enum should lock to the three supported "
            f"agent-callable harnesses; got {enum!r}"
        )

    def test_consult_description_names_specific_call_shape(self):
        """Tool-name hallucination guard: description must explicitly
        show the right call shape so the model has examples to
        anchor on instead of inventing names like
        'external_agent_consult.cc' or 'codex_async_advisory'."""
        from kernos.kernel.external_agents.tool import CONSULT_TOOL
        desc = CONSULT_TOOL["description"]
        # Concrete call shape
        assert 'consult(harness="codex"' in desc
        assert 'consult(harness="claude_code"' in desc
        # Negative examples (what NOT to do)
        assert "external_agent_consult.cc" in desc
        assert "codex_async_advisory" in desc


class TestSupportedTargets:
    def test_includes_three_canonical_names(self):
        assert "claude_code" in SUPPORTED_TARGETS
        assert "codex" in SUPPORTED_TARGETS
        assert "gemini" in SUPPORTED_TARGETS

    def test_supported_targets_are_strings(self):
        assert all(isinstance(t, str) for t in SUPPORTED_TARGETS)


# ===========================================================================
# Descendant reaping — closes the codex-acp orphan leak
# ===========================================================================


class TestCollectDescendants:
    """``_collect_descendants`` walks ``/proc/<pid>/task/<tid>/children``
    to enumerate the full process tree under a root PID. Required
    because ``npm exec`` calls ``setsid`` and the leaves escape our
    process group — killpg can't reach them.
    """

    def test_returns_self_descendants_via_subprocess(self):
        import subprocess
        from kernos.kernel.external_agents.acpx_adapter import (
            _collect_descendants,
        )

        # Spawn `sh -c "sleep 30 & sleep 30 & wait"` so we have a
        # deterministic descendant tree we can observe.
        proc = subprocess.Popen(
            ["sh", "-c", "sleep 30 & sleep 30 & wait"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Give the shell a moment to fork its children
            import time
            time.sleep(0.3)
            descendants = _collect_descendants(proc.pid)
            # Should see at least the two `sleep` children
            assert len(descendants) >= 2, (
                f"expected >=2 descendants, got {descendants}"
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            # Clean up any straggler sleeps
            import os, signal
            for pid in descendants if 'descendants' in dir() else []:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def test_returns_empty_for_nonexistent_pid(self):
        from kernos.kernel.external_agents.acpx_adapter import (
            _collect_descendants,
        )
        # A PID very unlikely to exist
        assert _collect_descendants(9_999_999) == []

    def test_returns_empty_for_leaf_process(self):
        import subprocess
        from kernos.kernel.external_agents.acpx_adapter import (
            _collect_descendants,
        )
        # `sleep` with no children → empty descendant set
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            import time
            time.sleep(0.1)
            assert _collect_descendants(proc.pid) == []
        finally:
            proc.terminate()
            proc.wait(timeout=5)


class TestKillTree:
    """``_kill_tree`` SIGKILLs every descendant of a root PID and
    returns the count signaled. Used in the dispatch teardown to
    reap codex-acp / claude-acp grandchildren that escaped the
    process group."""

    def test_kills_descendants_and_returns_count(self):
        import subprocess, time, os
        from kernos.kernel.external_agents.acpx_adapter import (
            _kill_tree, _collect_descendants,
        )
        # sh with two sleep grandchildren we want killed
        proc = subprocess.Popen(
            ["sh", "-c", "sleep 30 & sleep 30 & wait"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.3)
            descendants_before = _collect_descendants(proc.pid)
            assert len(descendants_before) >= 2
            killed = _kill_tree(proc.pid)
            assert killed >= 2
            # Give the kernel a moment to reap
            time.sleep(0.2)
            # Now confirm the descendants are gone
            for pid in descendants_before:
                # Either truly gone or zombie waiting for parent
                try:
                    # SIGCONT to test existence; ESRCH means gone
                    os.kill(pid, 0)
                    # Still there — must be a zombie (status Z)
                    with open(f"/proc/{pid}/status") as f:
                        state_line = next(
                            l for l in f if l.startswith("State:")
                        )
                    assert "Z" in state_line, (
                        f"PID {pid} survived kill: {state_line}"
                    )
                except (ProcessLookupError, OSError, FileNotFoundError):
                    pass  # gone, expected
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_returns_zero_for_leaf_pid(self):
        import subprocess, time
        from kernos.kernel.external_agents.acpx_adapter import _kill_tree
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.1)
            assert _kill_tree(proc.pid) == 0
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_handles_nonexistent_pid_gracefully(self):
        from kernos.kernel.external_agents.acpx_adapter import _kill_tree
        assert _kill_tree(9_999_999) == 0
