# POSTURE-PREFLIGHT-V1 — REJECTED (2026-05-22)

**Date:** 2026-05-22
**Status:** REJECTED post-implementation. Reverted in the
  same-day batch that introduced the end-of-assemble
  predictive scan. Spec retained as a record of the failed
  shape + the architectural lesson.

**Why rejected:** The premise of tool surfacing is to
minimize agent context bloat by putting only relevant tools
in front of the agent. This spec proposed an agent-callable
tool the agent would invoke to check whether other tools
were surfaced — adding a full turn (LLM call + tokens) for
the agent to query state that the surfacing system was
already trying to get right. Self-defeating: if surfacing
works, preflight is redundant; if surfacing fails, preflight
adds the exact friction the surfacing system was designed to
prevent.

**Replacement:** Move the existing catalog-scan LLM call to
the END of assemble (after the rest of the agent's context is
built) so it ranks the catalog against the full assembled
context. Agent never needs to ask about surfacing because the
surfacer has already seen the full trajectory and pre-staged
the right set. Predictive surfacing via existing LLM call, no
agent-visible surface.

**Process lesson:** A parent design spec's named sub-spec
isn't sufficient justification for the sub-spec on its own.
Each sub-spec must earn its weight against the problem
statement, including re-checking the load-bearing question
that may have been deferred at the parent level. Codex review
on the parent ratified the architecture; that did not
substitute for first-principles review at sub-spec time.

---

## Original spec (preserved for reference)

**Original scope:** New agent-callable kernel tool
  `inspect_tool_availability` backed by a per-turn
  surfacing snapshot. Agent can ask the substrate
  "is tool X currently surfaced?" before either calling
  it (wasted attempt → unknown classification) or
  invoking `request_tool` (no-op if the tool is
  already available).
**Estimated size:** ~110 LOC source + ~120 LOC tests.

## Why this spec exists

Per `KERNOS-DEFAULT-POSTURE-V1` D5 + the
`POSTURE-SURFACING-CALIBRATION-V1` deferral note: the agent
currently has no introspective surface for "is tool X
currently in my active zone." Symptoms observed during the
canvas test (2026-05-22):

- Agent tries `page_write` → returns "tool not found" because
  it was evicted for budget. Agent's only recovery is to
  call `request_tool`, which only activates MCP capabilities
  (`reasoning.py:1561`) and is a no-op for kernel tools.
- Agent doesn't know whether the tool is missing because:
  (a) it was evicted from this turn's active zone (budget
  pressure), (b) it was never registered, (c) it was filtered
  by service-disable, or (d) it's behind a posture withhold.
- Without that signal, the agent either retries the same
  call until denial-limit fires, or apologetically gives up.

The preflight tool gives the agent a structured introspection:
a single read-only call returns `available`, `tier`, and a
source-aware suggestion for recovery.

## Current state

- `assemble.py`'s surfacer builds `pinned_tools` + `active_tools`
  per turn but does NOT persist a queryable snapshot.
- `kernos/kernel/tool_catalog.py` exposes
  `ALWAYS_PINNED` (static set) and `get_names()` (catalog
  membership) but not per-turn tier.
- `request_tool` (`reasoning.py:1561`) activates MCP
  capabilities only. Kernel tools, stock canvas tools, and
  workspace-registered tools have no `request_tool` path.
- The `tool.withheld_from_surface` event from
  `POSTURE-SURFACING-CALIBRATION-V1` records eviction
  attributably AFTER the fact, but the agent has no synchronous
  way to query the current surface mid-turn.

## Design

### Per-turn surfacing snapshot

New module: `kernos/messages/surfacing_snapshot.py`.

```python
"""Per-turn surfacing snapshot — frozen tier+reason map for
inspect_tool_availability. Built at the end of the
assemble-phase surfacer; queried mid-turn by the agent.

POSTURE-PREFLIGHT-V1 (2026-05-22).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

Tier = Literal["pinned", "active", "catalog", "absent"]
Source = Literal["kernel", "mcp_capability", "stock", "workspace", "unknown"]

@dataclass(frozen=True)
class ToolSurfacingEntry:
    name: str
    tier: Tier
    source: Source
    reason_if_absent: str = ""  # one of: "evicted_for_budget",
                                # "not_registered",
                                # "disabled_service",
                                # "withheld_by_policy", ""

@dataclass(frozen=True)
class SurfacingSnapshot:
    """Built once at end-of-assemble. Frozen for the turn.

    Lookup is O(1) on entries dict. Cardinality bounded by
    total catalog size (currently ~50 tools)."""
    entries: dict[str, ToolSurfacingEntry] = field(default_factory=dict)
    turn_id: str = ""

    def get(self, tool_name: str) -> ToolSurfacingEntry:
        """Returns the entry for tool_name, or a synthetic
        'absent / not_registered' entry if not seen."""
        if tool_name in self.entries:
            return self.entries[tool_name]
        return ToolSurfacingEntry(
            name=tool_name,
            tier="absent",
            source="unknown",
            reason_if_absent="not_registered",
        )
```

### Snapshot capture in assemble.py

At the end of the surfacer block (right before `ctx.tools = tools`):

```python
from kernos.messages.surfacing_snapshot import (
    SurfacingSnapshot, ToolSurfacingEntry,
)

# Build the snapshot. Pinned + active = surfaced; everything
# else in the catalog is "absent" with a reason.
_entries: dict[str, ToolSurfacingEntry] = {}
_pinned_names = {t.get("name", "") for t in pinned_tools}
_active_names = {t.get("name", "") for t in active_tools}
_evicted_names = set(_evicted)
_all_catalog = handler._tool_catalog.get_names()

def _source_for(name: str) -> str:
    if name in _kernel_tool_map:
        return "kernel"
    # MCP capabilities expose tool_effects on their registry
    # entries; stock and workspace tools live in the
    # registrar's catalog without a capability owner.
    for cap in handler.registry.get_all():
        if name in (cap.tools or []):
            return "mcp_capability"
    if name in _all_catalog:
        return "stock"
    return "unknown"

for name in _pinned_names:
    _entries[name] = ToolSurfacingEntry(
        name=name, tier="pinned", source=_source_for(name),
    )
for name in _active_names:
    _entries[name] = ToolSurfacingEntry(
        name=name, tier="active", source=_source_for(name),
    )
for name in _evicted_names:
    _entries[name] = ToolSurfacingEntry(
        name=name, tier="absent", source=_source_for(name),
        reason_if_absent="evicted_for_budget",
    )
for name in _disabled_tool_names:
    _entries[name] = ToolSurfacingEntry(
        name=name, tier="absent", source=_source_for(name),
        reason_if_absent="disabled_service",
    )
# Anything in the catalog but not yet entered → "catalog"
# tier (registered, not surfaced this turn, not evicted —
# i.e., never ranked into candidates).
for name in _all_catalog:
    if name not in _entries:
        _entries[name] = ToolSurfacingEntry(
            name=name, tier="catalog", source=_source_for(name),
        )

handler._surfacing_snapshot = SurfacingSnapshot(
    entries=_entries,
    turn_id=getattr(ctx, "turn_id", "") or "",
)
```

The snapshot lives on the handler instance, replaced per turn
(no accumulation, no leak). Lookup happens through
`handler._surfacing_snapshot.get(tool_name)`.

### `inspect_tool_availability` kernel tool

New module: `kernos/kernel/inspect_tool_availability.py`.

```python
"""inspect_tool_availability kernel tool — agent-callable
preflight check on tool surfacing.

POSTURE-PREFLIGHT-V1 (2026-05-22). Read-only; pinned in
ALWAYS_PINNED so the contract holds (would be useless if
itself evictable).
"""
from __future__ import annotations
from typing import Any


INSPECT_TOOL_AVAILABILITY_TOOL: dict = {
    "name": "inspect_tool_availability",
    "description": (
        "Check whether a tool is currently surfaced for this "
        "turn. Returns the tool's tier (pinned | active | "
        "catalog | absent), its source (kernel | mcp_capability "
        "| stock | workspace | unknown), the reason if absent "
        "(evicted_for_budget | not_registered | disabled_service "
        "| empty), and a source-aware suggestion for recovery. "
        "Use BEFORE calling an unfamiliar tool to avoid "
        "wasted attempts; use BEFORE invoking request_tool "
        "since request_tool is MCP-only and would be a no-op "
        "for kernel/stock/workspace tools."
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
        return ""  # Already surfaced; no action needed.
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
            "intent matches; if your intent is clear, "
            "retry after re-stating it explicitly."
        )
    if entry.source == "workspace":
        return (
            "Tool is workspace-registered. Same surfacing "
            "rules as stock; retry with a clearer intent "
            "statement."
        )
    if entry.reason_if_absent == "disabled_service":
        return (
            "Tool is registered but the underlying service "
            "is disabled. Enable the service to surface this "
            "tool."
        )
    return (
        "Tool is not registered. Check the catalog or "
        "request_tool for an MCP capability that provides it."
    )


def handle_inspect_tool_availability_tool(
    *, handler: Any, tool_input: dict,
) -> dict:
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
```

### Wiring

1. **Schema registration**: import + append in
   `kernos/kernel/kernel_tool_registry.py` (alongside the
   other diagnostic tools).
2. **Catalog name**: add `"inspect_tool_availability"` to
   `_KERNEL_TOOLS` in `reasoning.py:660`.
3. **Pin**: add `"inspect_tool_availability"` to
   `ALWAYS_PINNED` in `kernos/kernel/tool_catalog.py:51+`.
4. **Effect**: add `"inspect_tool_availability"` to
   `_KERNEL_READS` in `gate.py:90+`.
5. **Dispatch**: add an `elif tool_name == "inspect_tool_availability"`
   branch in `reasoning.execute_tool` that calls
   `handle_inspect_tool_availability_tool(handler=self._handler, tool_input=tool_input)`.
6. **Policy registry**: add the tool's policy stanza to the
   per-tool policy map in
   `reasoning.py:_KERNEL_TOOLS_POLICY` (`frozenset({"confirmed"})` to
   mirror dump_context's read-only contract).

### Pin rationale

The preflight tool's contract is "the agent can always ask
about surfacing." If the preflight tool itself were evictable,
the contract would fail exactly when most needed (high-budget-
pressure turn). Pinning costs one extra schema in the
permanent set (~150 tokens at most) and resolves the recursion.

## Acceptance criteria

| AC | Description |
|---|---|
| AC1 | `inspect_tool_availability(tool_name="write_file")` on a fresh turn returns `available=True`, `tier="pinned"`, `source="kernel"`. |
| AC2 | `inspect_tool_availability(tool_name="page_write")` after an active-zone fill returns `tier="active"` (if surfaced) or `tier="absent"`+`reason_if_absent="evicted_for_budget"` (if evicted). |
| AC3 | `inspect_tool_availability(tool_name="never_registered_tool")` returns `tier="absent"`, `source="unknown"`, `reason_if_absent="not_registered"`. |
| AC4 | `inspect_tool_availability(tool_name="")` returns `ok=False` with `error` mentioning tool_name requirement. |
| AC5 | Calling `inspect_tool_availability` before any surfacing snapshot is built returns `ok=False` with the "no snapshot" error. |
| AC6 | Source-aware suggestion for `mcp_capability` source includes `request_tool(capability_name=...)`. |
| AC7 | Source-aware suggestion for `kernel` source with `evicted_for_budget` mentions `KERNOS_TOOL_TOKEN_BUDGET`. |
| AC8 | Source-aware suggestion for `stock` source mentions intent-aware ranker promotion. |
| AC9 | `inspect_tool_availability` is in `ALWAYS_PINNED` — verify the constant directly. |
| AC10 | `inspect_tool_availability` classifies as `read` at the dispatch gate. |
| AC11 | Snapshot reflects pinned tools at `tier="pinned"`. |
| AC12 | Snapshot reflects active-zone tools at `tier="active"`. |
| AC13 | Snapshot reflects evicted tools at `tier="absent"`+`reason="evicted_for_budget"`. |
| AC14 | Snapshot reflects disabled-service tools at `tier="absent"`+`reason="disabled_service"`. |
| AC15 | Snapshot reflects catalog-but-unranked tools at `tier="catalog"`. |
| AC16 | No regressions on existing surfacing tests. |

## Soak gate

1. **Automated**: ACs pin tool wiring + snapshot construction
   + suggestion correctness.
2. **Operator soak**: in System space, send
   "Inspect tool availability for `page_write`." Verify the
   agent calls `inspect_tool_availability(tool_name="page_write")`
   and reports the response correctly.
3. **Eviction soak**: lower `KERNOS_TOOL_TOKEN_BUDGET` to
   force eviction. Verify `inspect_tool_availability` on an
   evicted tool returns `reason="evicted_for_budget"` + the
   budget suggestion.

## Out of scope

- `request_tool` extension to support kernel/stock/workspace
  promotion → reserved (a future spec MAY add a "force-pin
  for next turn" verb; v1 keeps `request_tool` MCP-only and
  exposes the distinction through the preflight tool).
- Snapshot persistence across turns → unnecessary; per-turn
  is the contract.
- Snapshot diffing / history → out of scope; the friction
  observer can derive patterns from the
  `tool.withheld_from_surface` event stream.

## Risks

- **Risk:** Snapshot lookup race — agent calls
  `inspect_tool_availability` before assemble has finished.
  - **Mitigation:** Assemble runs synchronously before the
    reasoning turn. The first tool call this turn always
    sees a fresh snapshot. The defensive `if snapshot is None`
    branch returns `ok=False` with a clear error rather than
    crashing.

- **Risk:** Snapshot cardinality grows with catalog growth.
  - **Mitigation:** Per-turn replacement bounds memory to
    one snapshot. ~50 tools × ~100 bytes each = ~5KB. Fine.

- **Risk:** Source attribution `_source_for` could
  mis-attribute a tool that appears in multiple registries
  (e.g. a kernel tool also exported by an MCP capability).
  - **Mitigation:** The check-order (kernel first, then
    MCP, then stock/catalog) gives kernel precedence. This
    is correct: a kernel-source tool's recovery suggestion
    is more useful than an MCP one even if both technically
    apply.

## Dependencies

- `POSTURE-SURFACING-CALIBRATION-V1` (commit `b31991b`)
  — landed. Establishes the disabled-service tracking the
  snapshot reads via `_disabled_tool_names`.
- Blocks: nothing immediate.

## Migration

No state migration. Snapshot is per-turn ephemeral on the
handler instance. Tool registration is additive.
