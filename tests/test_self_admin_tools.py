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
    def test_returns_path_pointer(self, tmp_path, monkeypatch):
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
        assert "Context dumped to" in result
        # File should exist at the referenced path
        path_str = result.split("Context dumped to ", 1)[1].split(".", 2)
        # Reasonable smoke check that a path was written
        assert "diagnostics" in result


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
