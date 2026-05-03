"""Build ``SurfacedTool`` tuples for ``IntegrationInputs.surfaced_tools``.

The C7 thin path's integration phase classifies user requests into
``ActionKind`` based partly on what tools are surfaced — without a
populated ``surfaced_tools`` field, the integration LLM defaults to
render-only kinds and the agent cannot select tool execution. Pre-
INTEGRATION-CAPABILITY-FIRST-V1, this seam was empty (the field
defaulted ``()`` and reasoning never populated it). This module is
the canonical builder.

Source data: ``CognitiveContextPacket.tool_surface.all_tools()``
returns a tuple of provider-API tool dicts (``name``, ``description``,
``input_schema``).

Mapping target: ``IntegrationRunner.SurfacedTool`` (frozen dataclass)
with ``tool_id``, ``description``, ``input_schema``,
``gate_classification`` ("read" / "soft_write" / "hard_write" /
"unknown"), and ``surfacing_rationale`` (provenance string).

Classification source: ``DispatchGate.classify_tool_effect()`` is the
canonical effect classifier that knows kernel tools (read/write
sets), capability-registered MCP tools (per-tool ``tool_effects``),
and action-dependent tools like ``manage_covenants`` /
``respond_to_parcel`` / etc.

Conservative fallback (per spec):
   When the gate returns an empty / falsy / "unknown" classification,
   this builder emits ``"unknown"`` — NOT silently "read". The
   integration runner only forwards tools classified as ``"read"``
   to the integration LLM as callable read tools, so an unknown
   tool stays in the surface (so the agent knows it exists) but
   does not get auto-promoted to a callable read.
"""

from __future__ import annotations

import logging
from typing import Any

from kernos.kernel.integration.runner import SurfacedTool

logger = logging.getLogger(__name__)


# Conservative classification when the gate cannot place a tool. Per
# INTEGRATION-CAPABILITY-FIRST-V1 §"Conservative classification fallback":
# missing/unknown classification defaults to propose/blocked rather than
# silently read-safe.
_FALLBACK_CLASSIFICATION = "unknown"


# Tools whose ``classify_tool_effect`` result depends on the per-call
# ``tool_input["action"]`` argument. At surfacing time we do NOT have
# the args yet — DispatchGate falls back to the action="list" /
# action="status" default which classifies "read". That cached "read"
# would let the integration runner expose the tool as a callable read
# even though calling it with action="create" / "delete" / "update"
# is a write. Per Codex Batch 1 review: this is the action-dependent
# safety gap. We classify these tools as ``"unknown"`` at surfacing
# time and let dispatch-time enforcement (Batch 2 workshop binding
# wiring) be the source of truth using the actual arguments.
#
# Source of truth: ``DispatchGate.classify_tool_effect`` in
# ``kernos/kernel/gate.py`` — keep this list in sync if new
# action-dependent kernel tools are added there.
_ACTION_DEPENDENT_TOOLS: frozenset[str] = frozenset({
    "manage_covenants",
    "manage_capabilities",
    "manage_channels",
    "manage_members",
    "manage_plan",
    "manage_workspace",
    "respond_to_parcel",
})


def build_surfaced_tools(
    tool_dicts: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    *,
    gate: Any,
    active_space: Any = None,
    rationale: str = "cognitive_context.tool_surface",
) -> tuple[SurfacedTool, ...]:
    """Map provider-API tool dicts to ``SurfacedTool`` tuples.

    Args:
        tool_dicts: tools from ``CognitiveContextPacket.tool_surface
            .all_tools()`` — dicts with ``name``, ``description``,
            ``input_schema`` keys.
        gate: ``DispatchGate`` instance with ``classify_tool_effect()``.
            Required.
        active_space: passed through to the classifier; the current
            classifier doesn't consult it but the signature requires
            an arg.
        rationale: surfacing_rationale string. Caller can override
            (e.g., "always_pinned", "active_zone", "request_tool")
            but the default reflects the typical thin-path use site.

    Returns:
        Tuple of ``SurfacedTool``, one per non-empty named tool dict.
        Tools missing a ``name`` are skipped (defensive). Tools the
        gate cannot classify get ``gate_classification="unknown"`` —
        the integration runner will then NOT forward them as callable
        reads, but they remain in the surface for awareness.
    """
    out: list[SurfacedTool] = []
    for t in tool_dicts:
        if not isinstance(t, dict):
            continue
        name = t.get("name", "")
        if not name:
            continue
        # Action-dependent tools: classify "unknown" at surfacing time
        # rather than baking in the gate's list/status default. The
        # actual effect is determined per-call by the action argument,
        # which we don't have yet. Per Batch 1 Codex review.
        if name in _ACTION_DEPENDENT_TOOLS:
            classification = _FALLBACK_CLASSIFICATION
        else:
            try:
                classification = gate.classify_tool_effect(
                    name, active_space, None,
                )
            except Exception as exc:
                logger.warning(
                    "SURFACED_TOOLS_CLASSIFY_FAILED: tool=%s err=%s",
                    name, exc,
                )
                classification = _FALLBACK_CLASSIFICATION
            if not classification:
                classification = _FALLBACK_CLASSIFICATION
        out.append(SurfacedTool(
            tool_id=name,
            description=t.get("description", ""),
            input_schema=t.get(
                "input_schema",
                {"type": "object", "properties": {}},
            ),
            gate_classification=classification,
            surfacing_rationale=rationale,
        ))
    return tuple(out)
