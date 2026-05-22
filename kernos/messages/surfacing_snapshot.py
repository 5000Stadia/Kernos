"""Per-turn surfacing snapshot — frozen tier+reason map.

Built once at the end of the assemble-phase surfacer; queried
mid-turn by ``inspect_tool_availability``. Per-turn ephemeral:
the next assemble overwrites it, so memory cardinality is
bounded by catalog size (currently ~50 tools).

POSTURE-PREFLIGHT-V1 (2026-05-22).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


Tier = Literal["pinned", "active", "catalog", "absent"]
Source = Literal["kernel", "mcp_capability", "stock", "workspace", "unknown"]


@dataclass(frozen=True)
class ToolSurfacingEntry:
    """One catalog tool's surfacing fate for the current turn."""

    name: str
    tier: Tier
    source: Source
    reason_if_absent: str = ""
    # One of: "evicted_for_budget", "not_registered",
    # "disabled_service", "withheld_by_policy", or "" when surfaced.


@dataclass(frozen=True)
class SurfacingSnapshot:
    """Frozen per-turn map of tool_name → tier+reason.

    Frozen at end-of-assemble; lookup is O(1) via ``get``.
    Missing tools synthesize an ``absent / not_registered``
    entry rather than raising.
    """

    entries: dict[str, ToolSurfacingEntry] = field(default_factory=dict)
    turn_id: str = ""

    def get(self, tool_name: str) -> ToolSurfacingEntry:
        if tool_name in self.entries:
            return self.entries[tool_name]
        return ToolSurfacingEntry(
            name=tool_name,
            tier="absent",
            source="unknown",
            reason_if_absent="not_registered",
        )
