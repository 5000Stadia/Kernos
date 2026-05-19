"""Harness-shim contract tests (ACPX-INTEGRATION-V1, 2026-05-18).

The C2 harness classes (ClaudeCodeHarness, CodexHarness, GeminiHarness)
are now thin compatibility shims that delegate to
``acpx_adapter.dispatch``. The substrate-of-record for actual CLI
behavior is the ACPX adapter, exercised in tests/test_acpx_adapter.py.

These tests pin the shim contract:
  * ``consult()`` builds a prompt and calls ``dispatch`` with the
    expected target alias, prompt body, session_id, workspace_dir,
    and timeout.
  * ``health_check()`` still surfaces binary presence (used by the
    bring-up code in server.py to log AGENT_PROTOCOL_AVAILABLE).
  * The legacy ``_hex_to_uuid`` and ``_parse_codex_jsonl`` helpers
    remain for back-compat consumers and still behave correctly.

Live-CLI tests still gate on ``KERNOS_LIVE_AGENT_TESTS`` + the
real ``acpx`` binary; they exercise the full dispatch path
end-to-end.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from kernos.kernel.external_agents import sanitize_session_id
from kernos.kernel.external_agents.harness import ConsultResult
from kernos.kernel.external_agents.harnesses import (
    ClaudeCodeHarness,
    CodexHarness,
    GeminiHarness,
)
from kernos.kernel.external_agents.harnesses.claude_code import _hex_to_uuid
from kernos.kernel.external_agents.harnesses.codex import _parse_codex_jsonl


# ===========================================================================
# Dispatch capture — substrate-fidelity test pattern: assert the call
# shape the shim hands to acpx_adapter.dispatch, since dispatch IS
# the substrate boundary the shim is wrapping.
# ===========================================================================


def _patch_dispatch(monkeypatch, harness_name: str) -> dict[str, Any]:
    """Replace acpx_adapter.dispatch with a recorder that returns a
    canned ConsultResult and stores the kwargs. Returns the capture
    dict, populated on each invocation."""
    capture: dict[str, Any] = {}

    async def fake_dispatch(**kwargs):
        capture.update(kwargs)
        return ConsultResult(
            response=f"echo: {kwargs.get('prompt', '')[:60]}",
            harness=kwargs.get("target", ""),
            session_id=kwargs.get("session_id", ""),
            native_session_ref="fake-native-ref",
            metadata={"duration_seconds": 0.01, "exit_status": 0},
            truncated=False,
        )

    # The shim does a local `from ... import dispatch` inside consult();
    # monkeypatch the module attribute so the import resolves to our fake.
    import kernos.kernel.external_agents.acpx_adapter as acpx_mod
    monkeypatch.setattr(acpx_mod, "dispatch", fake_dispatch)
    return capture


# ===========================================================================
# Claude Code shim
# ===========================================================================


class TestClaudeCodeHarness:
    def test_health_check_when_installed(self):
        if not shutil.which("claude"):
            pytest.skip("claude binary not on PATH")
        h = ClaudeCodeHarness()
        out = h.health_check()
        assert out.name == "claude_code"
        assert out.installed is True

    def test_health_check_when_missing(self):
        h = ClaudeCodeHarness(binary="/nonexistent/claude")
        out = h.health_check()
        assert out.installed is False
        assert "PATH" in out.detail

    async def test_consult_delegates_to_acpx_dispatch(
        self, tmp_path, monkeypatch,
    ):
        cap = _patch_dispatch(monkeypatch, "claude_code")
        h = ClaudeCodeHarness()
        sess = sanitize_session_id("test-session")
        out = await h.consult(
            question="hello",
            context="",
            session_id=sess,
            workspace_dir=tmp_path,
            timeout_seconds=30,
            harness_options={},
        )
        # Substrate state at the dispatch boundary:
        assert cap["target"] == "claude_code"
        assert cap["prompt"] == "hello"
        assert cap["session_id"] == sess
        assert cap["workspace_dir"] == str(tmp_path)
        assert cap["timeout_seconds"] == 30
        # Returned ConsultResult flows through unchanged
        assert out.harness == "claude_code"
        assert "echo: hello" in out.response

    async def test_consult_inlines_dict_context_into_prompt(
        self, tmp_path, monkeypatch,
    ):
        cap = _patch_dispatch(monkeypatch, "claude_code")
        h = ClaudeCodeHarness()
        await h.consult(
            question="explain the diff",
            context={"file": "foo.py", "branch": "main"},
            session_id="",
            workspace_dir=tmp_path,
            timeout_seconds=10,
            harness_options={},
        )
        # The shim's _compose_prompt() must serialize the dict into the
        # prompt body since ACPX takes a single string prompt.
        assert "explain the diff" in cap["prompt"]
        assert "foo.py" in cap["prompt"]
        assert "[Context]" in cap["prompt"]


class TestHexToUuid:
    """Legacy helper kept for any back-compat consumer reading
    sanitized session_ids. ACPX uses its own 16-char prefix
    (derive_session_id) for named sessions, but _hex_to_uuid still
    produces a stable UUID-shaped string from a sanitized hex id."""

    def test_produces_valid_uuid_format(self):
        hex_id = sanitize_session_id("kernos-session")
        out = _hex_to_uuid(hex_id)
        parts = out.split("-")
        assert len(parts) == 5
        assert [len(p) for p in parts] == [8, 4, 4, 4, 12]

    def test_deterministic(self):
        hex_id = sanitize_session_id("foo")
        assert _hex_to_uuid(hex_id) == _hex_to_uuid(hex_id)

    def test_handles_short_input_gracefully(self):
        out = _hex_to_uuid("abc")
        assert out


# ===========================================================================
# Codex shim
# ===========================================================================


class TestCodexHarness:
    def test_health_check_when_missing(self):
        h = CodexHarness(binary="/nonexistent/codex")
        out = h.health_check()
        assert out.installed is False

    async def test_consult_delegates_to_acpx_dispatch(
        self, tmp_path, monkeypatch,
    ):
        cap = _patch_dispatch(monkeypatch, "codex")
        h = CodexHarness()
        out = await h.consult(
            question="hello",
            context="",
            session_id="kernos-test",
            workspace_dir=tmp_path,
            timeout_seconds=30,
            harness_options={},
        )
        assert cap["target"] == "codex"
        assert cap["prompt"] == "hello"
        assert cap["session_id"] == "kernos-test"
        assert out.harness == "codex"

    async def test_consult_inlines_dict_context(
        self, tmp_path, monkeypatch,
    ):
        cap = _patch_dispatch(monkeypatch, "codex")
        h = CodexHarness()
        await h.consult(
            question="review this",
            context={"file": "x.py"},
            session_id="",
            workspace_dir=tmp_path,
            timeout_seconds=10,
            harness_options={},
        )
        assert "review this" in cap["prompt"]
        assert "x.py" in cap["prompt"]


class TestParseCodexJsonl:
    """Legacy ``codex exec --json`` parser kept for any caller that
    still uses it directly. New code routes through ACPX, but the
    parser remains importable and correct."""

    def test_parses_thread_message_and_usage(self):
        stdout = "\n".join([
            '{"type": "thread.started", "thread_id": "abc-123"}',
            '{"type": "turn.started"}',
            '{"type": "item.completed",'
            ' "item": {"id": "item_0", "type": "agent_message",'
            ' "text": "Hello there."}}',
            '{"type": "turn.completed",'
            ' "usage": {"input_tokens": 50, "output_tokens": 10}}',
            '',
        ])
        thread_id, response, usage = _parse_codex_jsonl(stdout)
        assert thread_id == "abc-123"
        assert response == "Hello there."
        assert usage == {"input_tokens": 50, "output_tokens": 10}

    def test_handles_malformed_lines_gracefully(self):
        stdout = (
            '{"type": "thread.started", "thread_id": "x"}\n'
            'not-json\n'
            '{"type": "item.completed",'
            ' "item": {"type": "agent_message", "text": "ok"}}\n'
        )
        thread_id, response, _ = _parse_codex_jsonl(stdout)
        assert thread_id == "x"
        assert response == "ok"

    def test_non_dict_event_skipped(self):
        stdout = "\n".join([
            "[1, 2, 3]",
            '"just a string"',
            'true',
            '{"type": "thread.started", "thread_id": "ok"}',
        ])
        thread_id, _, _ = _parse_codex_jsonl(stdout)
        assert thread_id == "ok"

    def test_non_dict_item_skipped(self):
        stdout = "\n".join([
            '{"type": "item.completed", "item": "not a dict"}',
            '{"type": "item.completed", "item": null}',
            '{"type": "item.completed",'
            ' "item": {"type": "agent_message", "text": "valid"}}',
        ])
        _, response, _ = _parse_codex_jsonl(stdout)
        assert response == "valid"

    def test_non_dict_usage_ignored(self):
        stdout = '{"type": "turn.completed", "usage": "not-a-dict"}'
        _, _, usage = _parse_codex_jsonl(stdout)
        assert usage == {}


# ===========================================================================
# Gemini shim
# ===========================================================================


class TestGeminiHarness:
    def test_health_check_when_missing(self):
        h = GeminiHarness(binary="/nonexistent/gemini")
        out = h.health_check()
        assert out.installed is False

    async def test_consult_delegates_to_acpx_dispatch(
        self, tmp_path, monkeypatch,
    ):
        cap = _patch_dispatch(monkeypatch, "gemini")
        h = GeminiHarness()
        sess = sanitize_session_id("gem-session")
        out = await h.consult(
            question="ping",
            context="",
            session_id=sess,
            workspace_dir=tmp_path,
            timeout_seconds=30,
            harness_options={},
        )
        assert cap["target"] == "gemini"
        assert cap["prompt"] == "ping"
        assert cap["session_id"] == sess
        assert out.harness == "gemini"


# ===========================================================================
# Live-CLI integration tests (skip unless KERNOS_LIVE_AGENT_TESTS=1 and
# the acpx binary is on PATH — the actual dispatch substrate now).
# ===========================================================================


def _live_tests_enabled() -> bool:
    return bool(os.environ.get("KERNOS_LIVE_AGENT_TESTS"))


def _acpx_available() -> bool:
    return shutil.which("acpx") is not None


@pytest.mark.skipif(
    not _live_tests_enabled() or not _acpx_available() or not shutil.which("claude"),
    reason="live agent tests require KERNOS_LIVE_AGENT_TESTS=1, acpx + claude on PATH",
)
class TestClaudeLive:
    async def test_live_consult_returns_response(self, tmp_path):
        h = ClaudeCodeHarness()
        out = await h.consult(
            question="Reply with the single word: pong",
            context="",
            session_id="",
            workspace_dir=tmp_path,
            timeout_seconds=120,
            harness_options={},
        )
        assert out.harness == "claude_code"
        assert out.response.strip()


@pytest.mark.skipif(
    not _live_tests_enabled() or not _acpx_available() or not shutil.which("codex"),
    reason="live agent tests require KERNOS_LIVE_AGENT_TESTS=1, acpx + codex on PATH",
)
class TestCodexLive:
    async def test_live_consult_captures_session(self, tmp_path):
        h = CodexHarness()
        out = await h.consult(
            question="Reply in one short sentence.",
            context="",
            session_id="kernos-live-test",
            workspace_dir=tmp_path,
            timeout_seconds=120,
            harness_options={},
        )
        assert out.harness == "codex"
        assert out.response.strip()
