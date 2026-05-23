"""Tests for SELF-ADMIN-TOOLS-V1 — dump_context + restart_self.

Per memory feedback_system_space_design.md: slash commands should
also be accessible as tools in System space. /dump and /restart
are the first two to land. These tests pin:

* Schemas registered in kernel_tool_registry + reasoning._KERNEL_TOOLS
* dump_context writes a substrate-honest file with the right sections
* restart_self uses a two-call confirmation pattern (does NOT execv
  on confirm=false; DOES on confirm=true — execv is mocked here so
  tests don't replace themselves)
* Both tools are System-space-gated by the dispatch policy (defense
  in depth on top of the surfacing-layer gate)
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest


# ===========================================================================
# Schema + registry pins
# ===========================================================================


class TestSchemasRegistered:
    def test_both_tools_in_kernel_tool_names(self):
        from kernos.kernel.kernel_tool_registry import kernel_tool_names
        names = kernel_tool_names()
        assert "dump_context" in names
        assert "restart_self" in names

    def test_both_tools_in_reasoning_kernel_tools_set(self):
        from kernos.kernel.reasoning import ReasoningService
        assert "dump_context" in ReasoningService._KERNEL_TOOLS
        assert "restart_self" in ReasoningService._KERNEL_TOOLS

    def test_dump_context_schema_shape(self):
        from kernos.kernel.self_admin_tools import DUMP_CONTEXT_TOOL
        assert DUMP_CONTEXT_TOOL["name"] == "dump_context"
        assert "description" in DUMP_CONTEXT_TOOL
        assert DUMP_CONTEXT_TOOL["input_schema"]["type"] == "object"

    def test_restart_self_schema_requires_reason(self):
        from kernos.kernel.self_admin_tools import RESTART_SELF_TOOL
        assert RESTART_SELF_TOOL["name"] == "restart_self"
        # reason is required; confirm is optional with default false
        assert "reason" in RESTART_SELF_TOOL["input_schema"]["required"]
        props = RESTART_SELF_TOOL["input_schema"]["properties"]
        assert "reason" in props
        assert "confirm" in props


# ===========================================================================
# write_context_dump — the file-writing helper
# ===========================================================================


class TestWriteContextDump:
    def test_writes_file_with_expected_sections(self, tmp_path):
        from kernos.kernel.self_admin_tools import write_context_dump
        path = write_context_dump(
            system_prompt="SYS",
            messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
            tools=[
                {"name": "fake_tool", "description": "x"},
            ],
            instance_id="test_inst",
            data_dir=str(tmp_path),
        )
        assert path.exists()
        content = path.read_text()
        # All major sections present
        assert "=== SYSTEM PROMPT ===" in content
        assert "=== MESSAGES ===" in content
        assert "=== TOOLS ===" in content
        assert "=== RECENT LOG ===" in content
        assert "=== SUMMARY ===" in content
        # Substrate content lands
        assert "SYS" in content
        assert "hello" in content
        assert "hi" in content
        assert "fake_tool" in content
        # Instance noted
        assert "test_inst" in content

    def test_writes_under_diagnostics_dir(self, tmp_path):
        from kernos.kernel.self_admin_tools import write_context_dump
        path = write_context_dump(
            system_prompt="",
            messages=[],
            tools=[],
            instance_id="i",
            data_dir=str(tmp_path),
        )
        assert path.parent == tmp_path / "diagnostics"
        assert path.name.startswith("context_")
        assert path.name.endswith(".txt")

    def test_omits_conversation_with_explanatory_note(self, tmp_path):
        """Tool-dispatched dump can't include RECENT CONVERSATION
        (no conv_logger access); the file must still tell the
        reader why so they're not surprised."""
        from kernos.kernel.self_admin_tools import write_context_dump
        path = write_context_dump(
            system_prompt="",
            messages=[],
            tools=[],
            instance_id="i",
            data_dir=str(tmp_path),
            omit_conversation_note=True,
        )
        content = path.read_text()
        assert "=== RECENT CONVERSATION ===" in content
        assert "omitted" in content.lower()
        assert "/dump" in content  # points operators at the full version

    def test_summary_includes_token_estimates(self, tmp_path):
        from kernos.kernel.self_admin_tools import write_context_dump
        path = write_context_dump(
            system_prompt="x" * 4000,  # ~1000 tokens at 4 chars/token
            messages=[{"role": "u", "content": "y" * 400}],  # ~100 tokens
            tools=[{"name": "t"}],
            instance_id="i",
            data_dir=str(tmp_path),
            system_prompt_static="x" * 2000,
            system_prompt_dynamic="x" * 2000,
        )
        content = path.read_text()
        assert "Static (cached): ~500 tokens" in content
        assert "Dynamic (fresh):  ~500 tokens" in content
        assert "System prompt: ~1000 tokens" in content


# ===========================================================================
# handle_dump_context_tool — dispatch wrapper
# ===========================================================================


class TestHandleDumpContextTool:
    """Pins the agent-facing contract: by default the tool returns
    a useful summary (path + sizes + section list) instead of just
    a pointer. include_content=True inlines the full file so the
    agent can actually read what was dumped (since the dump file
    lives outside any space and read_file can't reach it)."""

    def test_default_returns_summary_not_just_path(self, tmp_path, monkeypatch):
        from kernos.kernel.self_admin_tools import handle_dump_context_tool

        class _FakeRequest:
            instance_id = "test_inst"
            system_prompt = "SYS"
            messages = [{"role": "user", "content": "x"}]
            tools = [{"name": "y"}]
            system_prompt_static = ""
            system_prompt_dynamic = ""

        monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
        result = handle_dump_context_tool(request=_FakeRequest(), reason="test")
        # File path present
        assert "Context dumped to" in result
        assert "diagnostics" in result
        # Useful summary present — not just the path
        assert "file size:" in result
        assert "system prompt:" in result
        assert "messages:" in result
        assert "tools:" in result
        # Includes the offer to inline content
        assert "include_content=true" in result

    def test_include_content_inlines_full_dump(self, tmp_path, monkeypatch):
        """The bug this guards against (live-observed 2026-05-19):
        agent dumps context, gets the path back, tries read_file —
        fails because the dump lives outside spaces. With
        include_content=true the full file is in the tool result
        so the agent doesn't need a separate read."""
        from kernos.kernel.self_admin_tools import handle_dump_context_tool

        class _FakeRequest:
            instance_id = "i"
            system_prompt = "MARKER_SYSTEM_PROMPT_TEXT"
            messages = [{"role": "user", "content": "MARKER_MSG_CONTENT"}]
            tools = [{"name": "MARKER_TOOL_NAME"}]
            system_prompt_static = ""
            system_prompt_dynamic = ""

        monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
        result = handle_dump_context_tool(
            request=_FakeRequest(), include_content=True,
        )
        # The full dump file content is inline in the response
        assert "MARKER_SYSTEM_PROMPT_TEXT" in result
        assert "MARKER_MSG_CONTENT" in result
        assert "MARKER_TOOL_NAME" in result
        # Header is clear about what this is
        assert "=== dump_context (full content" in result
        assert "Source:" in result

    def test_include_content_false_does_not_inline(self, tmp_path, monkeypatch):
        from kernos.kernel.self_admin_tools import handle_dump_context_tool

        class _FakeRequest:
            instance_id = "i"
            system_prompt = "MARKER_SYSTEM_PROMPT_TEXT"
            messages = [{"role": "user", "content": "MARKER_MSG_CONTENT"}]
            tools = [{"name": "MARKER_TOOL_NAME"}]
            system_prompt_static = ""
            system_prompt_dynamic = ""

        monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
        # Default: include_content is False
        result = handle_dump_context_tool(request=_FakeRequest())
        # Markers are NOT in the result (only in the file)
        assert "MARKER_SYSTEM_PROMPT_TEXT" not in result
        assert "MARKER_MSG_CONTENT" not in result

    def test_schema_advertises_include_content_param(self):
        from kernos.kernel.self_admin_tools import DUMP_CONTEXT_TOOL
        props = DUMP_CONTEXT_TOOL["input_schema"]["properties"]
        assert "include_content" in props
        assert props["include_content"]["type"] == "boolean"

    def test_falls_back_to_reasoning_cache_when_request_empty(
        self, tmp_path, monkeypatch,
    ):
        """2026-05-23 fix: when the tool is dispatched via the live-
        dispatch path, the ReasoningRequest carries empty
        system_prompt/messages/tools (minimal-request from the
        live-dispatch factory). Without the fallback, the dump's
        summary line reports ~0 tokens despite a real prior turn
        having loaded substrate state. Fix: ReasoningService caches
        the last reasoning payload per instance; the tool falls
        back to that when the dispatch-time request is empty."""
        from kernos.kernel.self_admin_tools import handle_dump_context_tool
        from kernos.kernel.reasoning import (
            _set_active_reasoning_service,
            get_active_reasoning_service,
        )

        # Construct a stub reasoning service with a cached payload
        class _StubReasoning:
            def get_last_reasoning_payload(self, instance_id):
                if instance_id == "real_inst":
                    return {
                        "system_prompt": "CACHED_SYSTEM_PROMPT" * 30,
                        "messages": [
                            {"role": "user", "content": "cached msg"}
                        ],
                        "tools": [
                            {"name": "cached_tool",
                             "description": "x" * 50}
                        ],
                        "system_prompt_static": "STATIC",
                        "system_prompt_dynamic": "DYNAMIC",
                    }
                return {}

        prior = get_active_reasoning_service()
        try:
            _set_active_reasoning_service(_StubReasoning())

            class _EmptyRequest:
                """Mimics the live-dispatch factory's minimal request."""
                instance_id = "real_inst"
                system_prompt = ""
                messages = []
                tools = []
                system_prompt_static = ""
                system_prompt_dynamic = ""

            monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
            result = handle_dump_context_tool(
                request=_EmptyRequest(), include_content=True,
            )
            # The fallback hydrated the dump from the cache —
            # non-zero token counts + cached content visible.
            assert "cached msg" in result
            assert "cached_tool" in result
            # Summary line shows real token estimates, not 0
            assert "tokens (" in result
            # System prompt section non-empty
            assert "CACHED_SYSTEM_PROMPT" in result
        finally:
            _set_active_reasoning_service(prior)

    def test_cache_fallback_skipped_when_request_has_payload(
        self, tmp_path, monkeypatch,
    ):
        """Cache is ONLY consulted when the dispatch-time request is
        empty — the slash-command path (which DOES populate the
        request fully) must not get its real payload overwritten by
        a stale cache entry."""
        from kernos.kernel.self_admin_tools import handle_dump_context_tool
        from kernos.kernel.reasoning import (
            _set_active_reasoning_service,
            get_active_reasoning_service,
        )

        class _StubReasoning:
            def get_last_reasoning_payload(self, instance_id):
                return {
                    "system_prompt": "STALE_CACHE_VALUE",
                    "messages": [],
                    "tools": [],
                }

        prior = get_active_reasoning_service()
        try:
            _set_active_reasoning_service(_StubReasoning())

            class _PopulatedRequest:
                instance_id = "i"
                system_prompt = "FRESH_REQUEST_VALUE"
                messages = [{"role": "user", "content": "fresh"}]
                tools = [{"name": "fresh_tool"}]
                system_prompt_static = ""
                system_prompt_dynamic = ""

            monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
            result = handle_dump_context_tool(
                request=_PopulatedRequest(), include_content=True,
            )
            assert "FRESH_REQUEST_VALUE" in result
            assert "STALE_CACHE_VALUE" not in result
        finally:
            _set_active_reasoning_service(prior)


# ===========================================================================
# handle_restart_self_tool — two-call confirmation
# ===========================================================================


class TestHandleRestartSelfTool:
    def test_confirm_false_does_not_execv(self):
        """Critical safety pin: confirm=false MUST NOT execv. The
        first call returns the proposed action for the agent to
        surface to the user; only the explicit confirm=true call
        actually restarts."""
        from kernos.kernel.self_admin_tools import handle_restart_self_tool

        with patch("os.execv") as mock_execv:
            result = handle_restart_self_tool(
                reason="testing", confirm=False, instance_id="i",
            )
        assert mock_execv.call_count == 0
        assert "Proposed restart" in result
        assert "testing" in result
        assert "confirm=true" in result

    def test_confirm_missing_does_not_execv(self):
        from kernos.kernel.self_admin_tools import handle_restart_self_tool
        with patch("os.execv") as mock_execv:
            result = handle_restart_self_tool(reason="testing", instance_id="i")
        assert mock_execv.call_count == 0
        assert "Proposed restart" in result

    def test_empty_reason_rejected(self):
        from kernos.kernel.self_admin_tools import handle_restart_self_tool
        with patch("os.execv") as mock_execv:
            result = handle_restart_self_tool(
                reason="", confirm=True, instance_id="i",
            )
        assert mock_execv.call_count == 0
        assert "requires a reason" in result

    def test_confirm_true_with_reason_calls_execv(self):
        from kernos.kernel.self_admin_tools import handle_restart_self_tool
        with patch("os.execv") as mock_execv:
            handle_restart_self_tool(
                reason="stuck gateway", confirm=True, instance_id="i",
            )
        assert mock_execv.call_count == 1


# ===========================================================================
# Dispatch policy + admin-space gate
# ===========================================================================


class TestDispatchPolicy:
    def test_both_tools_in_dispatch_policy_map(self):
        """_KERNEL_TOOL_PATHS gates which dispatch chain runs for a
        tool; missing entries silently route through the wrong path."""
        from kernos.kernel.reasoning import ReasoningService
        assert "dump_context" in ReasoningService._KERNEL_TOOL_PATHS
        assert "restart_self" in ReasoningService._KERNEL_TOOL_PATHS

    def test_both_tools_always_pinned(self):
        """Founder decision 2026-05-19: both tools must be in
        ALWAYS_PINNED so the agent always has self-introspection
        + self-recovery reachable, regardless of active space or
        the surfacer's per-turn choices."""
        from kernos.kernel.tool_catalog import ALWAYS_PINNED
        assert "dump_context" in ALWAYS_PINNED
        assert "restart_self" in ALWAYS_PINNED

    def test_dispatch_branches_do_not_require_system_space(self):
        """Regression pin (founder decision 2026-05-19): the
        dispatch branches for dump_context and restart_self must
        NOT call _assert_admin_space — they're available in every
        space. Safety stays at the handler level (restart_self's
        confirm=true) + gate's hard_write classification."""
        import inspect
        from kernos.kernel.reasoning import ReasoningService
        src = inspect.getsource(ReasoningService.execute_tool)
        # Find the dump_context / restart_self dispatch blocks and
        # verify _assert_admin_space isn't called in them. We grep
        # the source around each branch.
        for tool in ("dump_context", "restart_self"):
            marker = f'tool_name == "{tool}"'
            idx = src.find(marker)
            assert idx >= 0, f"dispatch branch for {tool} missing"
            # Take the next 400 chars after the marker — that's
            # the dispatch block.
            block = src[idx:idx + 400]
            assert "_assert_admin_space" not in block, (
                f"{tool} dispatch must not call _assert_admin_space "
                f"(founder decision 2026-05-19: available everywhere)"
            )


class TestGateClassification:
    """The gate classifies tool effects (read / soft_write /
    hard_write). dump_context is pure introspection → read.
    restart_self kills in-flight tasks → hard_write."""

    def test_dump_context_classified_as_read(self):
        from kernos.kernel.gate import DispatchGate
        gate = DispatchGate(
            reasoning_service=None, registry=None, state=None, events=None,
        )
        effect = gate.classify_tool_effect("dump_context", None, {})
        assert effect == "read"

    def test_restart_self_classified_as_hard_write(self):
        from kernos.kernel.gate import DispatchGate
        gate = DispatchGate(
            reasoning_service=None, registry=None, state=None, events=None,
        )
        effect = gate.classify_tool_effect("restart_self", None, {})
        assert effect == "hard_write"
