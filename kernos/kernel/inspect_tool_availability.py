"""inspect_tool_availability kernel tool — agent-callable
preflight check on tool surfacing.

POSTURE-PREFLIGHT-V1 (2026-05-22). Read-only. Pinned in
ALWAYS_PINNED so the contract holds (the preflight tool
would be useless if itself evictable).

Agent flow:
  1. Agent considers calling tool X.
  2. Before calling X, agent calls
     ``inspect_tool_availability(tool_name="X")``.
  3. Response carries available / tier / source /
     reason_if_absent / request_tool_suggestion.
  4. Agent chooses: call X, call request_tool (if MCP-source
     suggestion), restate intent (if stock/kernel-source),
     or report the limitation.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


INSPECT_TOOL_AVAILABILITY_TOOL: dict = {
    "name": "inspect_tool_availability",
    "description": (
        "Check whether a tool is currently surfaced for this "
        "turn. Returns the tool's tier (pinned | active | "
        "catalog | absent), its source (kernel | mcp_capability "
        "| stock | workspace | unknown), the reason if absent "
        "(evicted_for_budget | not_registered | disabled_service "
        "| empty), and a source-aware suggestion for recovery. "
        "Use BEFORE calling an unfamiliar tool to avoid wasted "
        "attempts; use BEFORE invoking request_tool since "
        "request_tool is MCP-only and would be a no-op for "
        "kernel/stock/workspace tools."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "Exact tool name to inspect.",
            },
        },
        "required": ["tool_name"],
    },
}


def _suggestion_for(entry: Any) -> str:
    """Source-aware recovery hint per POSTURE-V1 D5 contract."""
    if entry.tier in ("pinned", "active"):
        return ""
    if entry.source == "mcp_capability":
        return (
            f"Tool is part of an MCP capability. Try "
            f"request_tool(capability_name=<the cap>, "
            f"description='need {entry.name}') to activate it."
        )
    if entry.source == "kernel":
        if entry.reason_if_absent == "evicted_for_budget":
            return (
                "Tool is registered but evicted from this turn's "
                "surface. It will re-rank on a turn whose intent "
                "matches its effect class; alternatively raise "
                "KERNOS_TOOL_TOKEN_BUDGET via /posture (when "
                "POSTURE-CONFIGURATION-V1 ships)."
            )
        return (
            "Tool is registered as a kernel tool but not "
            "surfaced this turn. Re-state your intent clearly "
            "and retry — the surfacer's intent-aware ranker "
            "should promote it when intent matches."
        )
    if entry.source == "stock":
        return (
            "Tool is registered but not surfaced. The "
            "intent-aware ranker should promote it when "
            "intent matches; if your intent is clear, retry "
            "after re-stating it explicitly."
        )
    if entry.source == "workspace":
        return (
            "Tool is workspace-registered. Same surfacing "
            "rules as stock; retry with a clearer intent "
            "statement."
        )
    if entry.reason_if_absent == "disabled_service":
        return (
            "Tool is registered but the underlying service is "
            "disabled. Enable the service to surface this tool."
        )
    return (
        "Tool is not registered. Check the catalog or "
        "request_tool for an MCP capability that provides it."
    )


def handle_inspect_tool_availability_tool(
    *, handler: Any, tool_input: dict,
) -> dict:
    """Look up the named tool in the current per-turn snapshot."""
    tool_name = (tool_input or {}).get("tool_name", "").strip()
    if not tool_name:
        return {
            "ok": False,
            "error": "tool_name is required",
        }
    snapshot = getattr(handler, "_surfacing_snapshot", None)
    if snapshot is None:
        return {
            "ok": False,
            "error": (
                "No surfacing snapshot available. This usually "
                "means the assemble phase hasn't run yet this "
                "turn — try again on the next turn."
            ),
        }
    entry = snapshot.get(tool_name)
    return {
        "ok": True,
        "tool_name": entry.name,
        "available": entry.tier in ("pinned", "active"),
        "tier": entry.tier,
        "source": entry.source,
        "reason_if_absent": entry.reason_if_absent,
        "request_tool_suggestion": _suggestion_for(entry),
    }
