from __future__ import annotations

from kernos.kernel.kernel_tool_registry import kernel_tool_schema_map
from kernos.kernel.reasoning import ReasoningService


def test_project_tools_are_registered_and_dispatchable():
    schemas = kernel_tool_schema_map()
    for name in (
        "start_project",
        "record_project_decision",
        "surface_project_status",
    ):
        assert name in schemas
        assert name in ReasoningService._KERNEL_TOOLS
        assert name in ReasoningService._DISPATCHABLE_KERNEL_TOOLS
        assert ReasoningService._KERNEL_TOOL_PATHS[name] == frozenset({"confirmed"})

