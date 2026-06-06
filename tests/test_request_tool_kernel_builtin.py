"""v1 self-test bug #5: request_tool only searched the capability registry
(MCP/connector tools), so it falsely reported built-in kernel tools like
manage_schedule / read_source as "not installed → go to System space."
A dispatchable kernel tool should be reported as an always-available built-in.
"""
from unittest.mock import AsyncMock, MagicMock

from kernos.kernel.reasoning import ReasoningService
from kernos.kernel.tool_catalog import ALWAYS_PINNED


def test_scheduling_is_pinned():
    # bedrock capability — must always be surfaced (the primary bug-5 fix)
    assert "manage_schedule" in ALWAYS_PINNED


def _svc():
    svc = MagicMock(spec=ReasoningService)
    svc._KERNEL_TOOLS = ReasoningService._KERNEL_TOOLS
    svc._handle_request_tool = ReasoningService._handle_request_tool.__get__(svc)
    # registry with no matching connector capability
    reg = MagicMock()
    reg.get.return_value = None
    reg.get_all.return_value = []
    svc._registry = reg
    return svc


async def test_request_tool_reports_kernel_tool_as_builtin():
    svc = _svc()
    out = await svc._handle_request_tool("t1", "space1", "manage_schedule", "set a reminder")
    assert "built-in" in out.lower()
    assert "System space" not in out          # not the misleading "not installed" path


async def test_request_tool_unknown_still_points_to_system_space():
    svc = _svc()
    out = await svc._handle_request_tool("t1", "space1", "spotify_player", "play music")
    assert "System space" in out              # genuine non-kernel, non-connector tool
