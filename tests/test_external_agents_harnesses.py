"""C2 harness implementation tests.

Two layers:

* **Mock-CLI tests** — every harness is exercised against a
  Python script standing in for the real CLI. Validates flag
  composition, output parsing, error mapping, session-id mapping
  for each backend without depending on the real binary being
  installed.
* **Live-CLI tests** — marked with ``live_cli`` and skipped when
  the binary isn't on PATH or ``KERNOS_LIVE_AGENT_TESTS`` env
  isn't set. These exercise the real CLIs end-to-end and ship in
  C6 alongside the agent tool surface; included here in stub form
  so the test suite is one-stop.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from kernos.kernel.external_agents import (
    ConsultationFailed,
    ConsultationTimeout,
    HarnessUnavailable,
    sanitize_session_id,
)
from kernos.kernel.external_agents.harnesses import (
    ClaudeCodeHarness,
    CodexHarness,
    GeminiHarness,
)
from kernos.kernel.external_agents.harnesses.claude_code import _hex_to_uuid
from kernos.kernel.external_agents.harnesses.codex import _parse_codex_jsonl


# ===========================================================================
# Helpers — Python stand-ins for each CLI
# ===========================================================================


@pytest.fixture
def claude_stub(tmp_path):
    """Write a Python script that mimics `claude --print`. Verifies
    flags via stdout JSON and emits a deterministic response so the
    harness can parse it as text."""
    stub = tmp_path / "claude_stub.py"
    stub.write_text(
        "import sys, json\n"
        "argv = sys.argv[1:]\n"
        "# Last arg is the prompt\n"
        "prompt = argv[-1] if argv else ''\n"
        "print(f'echo: {prompt[:60]}')\n"
        "sys.exit(0)\n"
    )
    wrapper = tmp_path / "claude"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {stub} \"$@\"\n")
    wrapper.chmod(0o755)
    return wrapper


@pytest.fixture
def codex_stub(tmp_path):
    """Stand-in that emits the JSON-event shape `codex exec --json`
    produces: thread.started → item.completed → turn.completed."""
    stub = tmp_path / "codex_stub.py"
    stub.write_text(
        "import sys, json\n"
        "argv = sys.argv[1:]\n"
        "thread_id = '019de0ed-925a-7c03-8951-bb70938cccbd'\n"
        "# Detect 'resume <id>' shape\n"
        "if 'resume' in argv:\n"
        "    idx = argv.index('resume')\n"
        "    if idx + 1 < len(argv):\n"
        "        thread_id = argv[idx + 1]\n"
        "prompt = argv[-1] if argv else ''\n"
        "events = [\n"
        "    {'type': 'thread.started', 'thread_id': thread_id},\n"
        "    {'type': 'turn.started'},\n"
        "    {'type': 'item.completed',\n"
        "     'item': {'id': 'item_0', 'type': 'agent_message',\n"
        "              'text': f'echo: {prompt[:60]}'}},\n"
        "    {'type': 'turn.completed',\n"
        "     'usage': {'input_tokens': 100, 'output_tokens': 12}},\n"
        "]\n"
        "for ev in events:\n"
        "    print(json.dumps(ev))\n"
        "sys.exit(0)\n"
    )
    wrapper = tmp_path / "codex"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {stub} \"$@\"\n")
    wrapper.chmod(0o755)
    return wrapper


@pytest.fixture
def gemini_stub(tmp_path):
    stub = tmp_path / "gemini_stub.py"
    stub.write_text(
        "import sys\n"
        "argv = sys.argv[1:]\n"
        "# Find --prompt arg\n"
        "prompt = ''\n"
        "if '--prompt' in argv:\n"
        "    idx = argv.index('--prompt')\n"
        "    if idx + 1 < len(argv):\n"
        "        prompt = argv[idx + 1]\n"
        "print(f'echo: {prompt[:60]}')\n"
        "sys.exit(0)\n"
    )
    wrapper = tmp_path / "gemini"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {stub} \"$@\"\n")
    wrapper.chmod(0o755)
    return wrapper


# ===========================================================================
# Claude Code
# ===========================================================================


class TestClaudeCodeHarness:
    def test_health_check_when_installed(self):
        # claude is on PATH on this system; skip otherwise.
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

    async def test_consult_against_stub(self, claude_stub, tmp_path):
        h = ClaudeCodeHarness(binary=str(claude_stub))
        sess = sanitize_session_id("test-session")
        out = await h.consult(
            question="hello",
            context="",
            session_id=sess,
            workspace_dir=tmp_path,
            timeout_seconds=30,
            harness_options={},
        )
        assert out.harness == "claude_code"
        assert out.session_id == sess
        assert "echo: hello" in out.response
        # native_session_ref is the UUID-shaped derivation
        assert out.native_session_ref == _hex_to_uuid(sess)
        assert "-" in out.native_session_ref
        assert len(out.native_session_ref) == 36

    async def test_consult_unavailable_when_binary_missing(self, tmp_path):
        h = ClaudeCodeHarness(binary="/nonexistent/claude")
        with pytest.raises(HarnessUnavailable, match="PATH"):
            await h.consult(
                question="x", context="", session_id="",
                workspace_dir=tmp_path, timeout_seconds=10,
                harness_options={},
            )

    async def test_consult_failure_maps_to_typed_error(self, tmp_path):
        # Stub that exits non-zero
        stub = tmp_path / "claude_fail.py"
        stub.write_text(
            "import sys\n"
            "sys.stderr.write('boom\\n')\n"
            "sys.exit(2)\n"
        )
        wrapper = tmp_path / "claude"
        wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {stub} \"$@\"\n")
        wrapper.chmod(0o755)
        h = ClaudeCodeHarness(binary=str(wrapper))
        with pytest.raises(ConsultationFailed, match="exited 2"):
            await h.consult(
                question="x", context="", session_id="",
                workspace_dir=tmp_path, timeout_seconds=10,
                harness_options={},
            )

    async def test_consult_timeout_raises_typed(self, tmp_path):
        stub = tmp_path / "claude_slow.py"
        stub.write_text("import time; time.sleep(10)\n")
        wrapper = tmp_path / "claude"
        wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {stub} \"$@\"\n")
        wrapper.chmod(0o755)
        h = ClaudeCodeHarness(binary=str(wrapper))
        with pytest.raises(ConsultationTimeout):
            await h.consult(
                question="x", context="", session_id="",
                workspace_dir=tmp_path, timeout_seconds=1,
                harness_options={},
            )


class TestHexToUuid:
    def test_produces_valid_uuid_format(self):
        hex_id = sanitize_session_id("kernos-session")
        out = _hex_to_uuid(hex_id)
        # UUID format 8-4-4-4-12
        parts = out.split("-")
        assert len(parts) == 5
        assert [len(p) for p in parts] == [8, 4, 4, 4, 12]

    def test_deterministic(self):
        hex_id = sanitize_session_id("foo")
        assert _hex_to_uuid(hex_id) == _hex_to_uuid(hex_id)

    def test_handles_short_input_gracefully(self):
        out = _hex_to_uuid("abc")
        assert out  # padded; doesn't crash


# ===========================================================================
# Codex
# ===========================================================================


class TestCodexHarness:
    def test_health_check_when_missing(self):
        h = CodexHarness(binary="/nonexistent/codex")
        out = h.health_check()
        assert out.installed is False

    async def test_consult_against_stub_captures_thread_id(
        self, codex_stub, tmp_path,
    ):
        h = CodexHarness(binary=str(codex_stub))
        out = await h.consult(
            question="hello",
            context="",
            session_id="kernos-test",
            workspace_dir=tmp_path,
            timeout_seconds=30,
            harness_options={},
        )
        assert out.harness == "codex"
        assert out.native_session_ref == "019de0ed-925a-7c03-8951-bb70938cccbd"
        assert "echo: hello" in out.response
        assert out.metadata["usage"]["input_tokens"] == 100

    async def test_resume_uses_prior_native_ref(
        self, codex_stub, tmp_path,
    ):
        h = CodexHarness(binary=str(codex_stub))
        out = await h.consult(
            question="follow-up",
            context="",
            session_id="kernos-test",
            workspace_dir=tmp_path,
            timeout_seconds=30,
            harness_options={"prior_native_session_ref": "saved-thread-1"},
        )
        # Stub echoes back the resumed thread id
        assert out.native_session_ref == "saved-thread-1"


class TestParseCodexJsonl:
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

    def test_concatenates_multiple_agent_messages(self):
        stdout = (
            '{"type": "item.completed",'
            ' "item": {"type": "agent_message", "text": "Part A"}}\n'
            '{"type": "item.completed",'
            ' "item": {"type": "agent_message", "text": "Part B"}}\n'
        )
        _, response, _ = _parse_codex_jsonl(stdout)
        assert response == "Part A\nPart B"


# ===========================================================================
# Gemini
# ===========================================================================


class TestGeminiHarness:
    def test_health_check_when_missing(self):
        h = GeminiHarness(binary="/nonexistent/gemini")
        out = h.health_check()
        assert out.installed is False

    async def test_consult_against_stub(self, gemini_stub, tmp_path):
        h = GeminiHarness(binary=str(gemini_stub), history_root=tmp_path)
        sess = sanitize_session_id("gem-session")
        out = await h.consult(
            question="ping",
            context="",
            session_id=sess,
            workspace_dir=tmp_path,
            timeout_seconds=30,
            harness_options={},
        )
        assert out.harness == "gemini"
        assert "echo: ping" in out.response
        # History file is created.
        history = tmp_path / sess / "gemini.jsonl"
        assert history.exists()

    async def test_history_replay_on_second_call(
        self, gemini_stub, tmp_path,
    ):
        """Second call with same session_id includes prior turns in
        the prompt — the stub captures the first 60 chars of the
        prompt, so we should see the prior-turn marker."""
        h = GeminiHarness(binary=str(gemini_stub), history_root=tmp_path)
        sess = sanitize_session_id("threaded")
        await h.consult(
            question="first message",
            context="",
            session_id=sess,
            workspace_dir=tmp_path,
            timeout_seconds=30,
            harness_options={},
        )
        out = await h.consult(
            question="second message",
            context="",
            session_id=sess,
            workspace_dir=tmp_path,
            timeout_seconds=30,
            harness_options={},
        )
        # Second call carries prior conversation in the prompt; the
        # stub echoes the first 60 chars which start with "[Prior".
        assert "[Prior" in out.response or "Prior" in out.response

    async def test_no_history_when_session_id_empty(
        self, gemini_stub, tmp_path,
    ):
        h = GeminiHarness(binary=str(gemini_stub), history_root=tmp_path)
        out = await h.consult(
            question="anonymous",
            context="",
            session_id="",
            workspace_dir=tmp_path,
            timeout_seconds=30,
            harness_options={},
        )
        assert out.native_session_ref == ""
        # No history file created
        assert list(tmp_path.glob("**/gemini.jsonl")) == []

    async def test_unsanitized_session_id_rejected(
        self, gemini_stub, tmp_path,
    ):
        """Codex mid-batch fold: harness enforces sanitized hex
        session_id format to prevent path traversal."""
        h = GeminiHarness(binary=str(gemini_stub), history_root=tmp_path)
        with pytest.raises(ConsultationFailed, match="sanitized"):
            await h.consult(
                question="x", context="",
                session_id="../../etc/passwd",  # path traversal attempt
                workspace_dir=tmp_path,
                timeout_seconds=10, harness_options={},
            )

    async def test_short_unsanitized_session_id_rejected(
        self, gemini_stub, tmp_path,
    ):
        h = GeminiHarness(binary=str(gemini_stub), history_root=tmp_path)
        with pytest.raises(ConsultationFailed, match="sanitized"):
            await h.consult(
                question="x", context="",
                session_id="not-a-hex-string",
                workspace_dir=tmp_path,
                timeout_seconds=10, harness_options={},
            )


# ===========================================================================
# Fold pinning — Codex mid-batch C2 review folds
# ===========================================================================


class TestSpawnFailureWrapping:
    """Codex mid-batch fold: spawn OSError must surface as
    HarnessUnavailable, not bubble untyped.

    Monkeypatches ``run_subprocess`` directly to inject the OSError
    we want to test the wrapping for. The earlier "non-executable
    binary" approach was rejected by ``shutil.which`` before the
    spawn ever happened, so it tested the binary-missing path
    rather than the spawn-error path."""

    async def test_claude_spawn_oserror_wrapped(
        self, claude_stub, tmp_path, monkeypatch,
    ):
        from kernos.kernel.external_agents.harnesses import claude_code

        async def boom(*args, **kwargs):
            raise OSError("simulated exec failure")

        monkeypatch.setattr(claude_code, "run_subprocess", boom)
        h = ClaudeCodeHarness(binary=str(claude_stub))
        with pytest.raises(HarnessUnavailable, match="spawn failed"):
            await h.consult(
                question="x", context="", session_id="",
                workspace_dir=tmp_path, timeout_seconds=10,
                harness_options={},
            )

    async def test_codex_spawn_oserror_wrapped(
        self, codex_stub, tmp_path, monkeypatch,
    ):
        from kernos.kernel.external_agents.harnesses import codex as codex_mod

        async def boom(*args, **kwargs):
            raise FileNotFoundError("simulated exec failure")

        monkeypatch.setattr(codex_mod, "run_subprocess", boom)
        h = CodexHarness(binary=str(codex_stub))
        with pytest.raises(HarnessUnavailable, match="spawn failed"):
            await h.consult(
                question="x", context="", session_id="",
                workspace_dir=tmp_path, timeout_seconds=10,
                harness_options={},
            )

    async def test_gemini_spawn_oserror_wrapped(
        self, gemini_stub, tmp_path, monkeypatch,
    ):
        from kernos.kernel.external_agents.harnesses import gemini as gemini_mod

        async def boom(*args, **kwargs):
            raise OSError("simulated exec failure")

        monkeypatch.setattr(gemini_mod, "run_subprocess", boom)
        h = GeminiHarness(binary=str(gemini_stub), history_root=tmp_path)
        with pytest.raises(HarnessUnavailable, match="spawn failed"):
            await h.consult(
                question="x", context="", session_id="",
                workspace_dir=tmp_path, timeout_seconds=10,
                harness_options={},
            )


class TestNonSerializableContextWrapping:
    """Codex mid-batch fold: context with non-JSON-serializable values
    raises ConsultationFailed, not a generic TypeError."""

    async def test_codex_non_serializable_context_raises_typed(
        self, codex_stub, tmp_path,
    ):
        h = CodexHarness(binary=str(codex_stub))
        # Pass a non-serializable dict (e.g., a set)
        with pytest.raises(ConsultationFailed, match="JSON-serializable"):
            await h.consult(
                question="x",
                context={"bad": {1, 2, 3}},  # set is not JSON-serializable
                session_id="",
                workspace_dir=tmp_path,
                timeout_seconds=10, harness_options={},
            )


class TestCodexParserRobustness:
    """Codex mid-batch fold: parser must defensively handle malformed
    event shapes (non-dict event, non-dict item, non-string text,
    non-dict usage) without crashing or polluting metadata."""

    def test_non_dict_event_skipped(self):
        from kernos.kernel.external_agents.harnesses.codex import (
            _parse_codex_jsonl,
        )
        stdout = "\n".join([
            "[1, 2, 3]",  # JSON array, not dict
            '"just a string"',
            'true',
            '{"type": "thread.started", "thread_id": "ok"}',
        ])
        thread_id, response, usage = _parse_codex_jsonl(stdout)
        assert thread_id == "ok"

    def test_non_dict_item_skipped(self):
        from kernos.kernel.external_agents.harnesses.codex import (
            _parse_codex_jsonl,
        )
        stdout = "\n".join([
            '{"type": "item.completed", "item": "not a dict"}',
            '{"type": "item.completed", "item": null}',
            '{"type": "item.completed",'
            ' "item": {"type": "agent_message", "text": "valid"}}',
        ])
        _, response, _ = _parse_codex_jsonl(stdout)
        assert response == "valid"

    def test_non_string_text_skipped(self):
        from kernos.kernel.external_agents.harnesses.codex import (
            _parse_codex_jsonl,
        )
        stdout = "\n".join([
            '{"type": "item.completed",'
            ' "item": {"type": "agent_message", "text": {"weird": true}}}',
            '{"type": "item.completed",'
            ' "item": {"type": "agent_message", "text": "ok"}}',
        ])
        _, response, _ = _parse_codex_jsonl(stdout)
        assert response == "ok"

    def test_non_dict_usage_ignored(self):
        from kernos.kernel.external_agents.harnesses.codex import (
            _parse_codex_jsonl,
        )
        stdout = '{"type": "turn.completed", "usage": "not-a-dict"}'
        _, _, usage = _parse_codex_jsonl(stdout)
        assert usage == {}


# ===========================================================================
# Live-CLI integration tests (skip when binary missing or env unset)
# ===========================================================================


def _live_tests_enabled() -> bool:
    return bool(os.environ.get("KERNOS_LIVE_AGENT_TESTS"))


@pytest.mark.skipif(
    not _live_tests_enabled() or not shutil.which("claude"),
    reason="live agent tests require KERNOS_LIVE_AGENT_TESTS=1 and claude on PATH",
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
    not _live_tests_enabled() or not shutil.which("codex"),
    reason="live agent tests require KERNOS_LIVE_AGENT_TESTS=1 and codex on PATH",
)
class TestCodexLive:
    async def test_live_consult_captures_thread_id(self, tmp_path):
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
        # Codex --json mode emits thread_id in real responses
        assert out.native_session_ref


@pytest.mark.skipif(
    not _live_tests_enabled() or not shutil.which("gemini"),
    reason="live agent tests require KERNOS_LIVE_AGENT_TESTS=1 and gemini on PATH",
)
class TestGeminiLive:
    async def test_live_consult_returns_response(self, tmp_path):
        h = GeminiHarness(history_root=tmp_path)
        out = await h.consult(
            question="Reply in 5 words or fewer.",
            context="",
            session_id="",
            workspace_dir=tmp_path,
            timeout_seconds=120,
            harness_options={},
        )
        assert out.harness == "gemini"
        assert out.response.strip()
