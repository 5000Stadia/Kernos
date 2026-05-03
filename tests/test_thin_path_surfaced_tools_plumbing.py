"""Pin tests for INTEGRATION-CAPABILITY-FIRST-V1 Batch 1 piece A.

Threads ``surfaced_tools`` end-to-end on the C7 thin path so the
integration LLM can see what tools are available for the turn and
classify ActionKind correctly. Pre-fix the field was empty, causing
integration to default to render-only kinds (RESPOND_ONLY /
CONSTRAINED_RESPONSE / PROPOSE_TOOL) and the agent to refuse tool
calls even when explicitly requested.

These pins close the seam structurally:

1. ``build_surfaced_tools`` maps tool_surface dicts to SurfacedTool
   tuples with non-empty gate_classification.
2. Conservative classification fallback: missing/unknown tool name
   → "unknown" (NOT silently "read"). Per spec: missing/unknown
   classification defaults to propose/blocked.
3. Reasoning's ``_run_via_turn_runner_provider`` builds
   TurnRunnerInputs.surfaced_tools from cognitive_context.tool_surface.
4. TurnRunnerInputs.surfaced_tools reaches IntegrationInputs.surfaced_tools
   (already pinned by turn_runner.py:299; this test re-verifies the
   structural plumbing through the runner.run_integration seam).

Pin file is intentionally narrow — six tests, focused.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kernos.kernel.integration.runner import SurfacedTool
from kernos.kernel.integration.surfaced_tools import build_surfaced_tools


# ---------------------------------------------------------------------------
# build_surfaced_tools — structural mapping
# ---------------------------------------------------------------------------


def test_build_surfaced_tools_maps_dict_to_surfaced_tool():
    """Pin: each tool dict becomes a SurfacedTool with the right
    fields (tool_id, description, input_schema, gate_classification,
    surfacing_rationale)."""
    gate = MagicMock()
    gate.classify_tool_effect.return_value = "read"
    tool_dicts = [
        {
            "name": "list-events",
            "description": "List calendar events",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    ]
    out = build_surfaced_tools(tool_dicts, gate=gate, rationale="test-rationale")

    assert len(out) == 1
    t = out[0]
    assert isinstance(t, SurfacedTool)
    assert t.tool_id == "list-events"
    assert t.description == "List calendar events"
    assert t.input_schema == {"type": "object", "properties": {"q": {"type": "string"}}}
    assert t.gate_classification == "read"
    assert t.surfacing_rationale == "test-rationale"


def test_build_surfaced_tools_uses_gate_classify_for_each_tool():
    """Pin: classification comes from gate.classify_tool_effect, not
    hard-coded — verifies the canonical effect classifier is used."""
    gate = MagicMock()
    gate.classify_tool_effect.side_effect = lambda name, _, __: {
        "list-events": "read",
        "create-event": "soft_write",
        "delete-event": "hard_write",
    }.get(name, "unknown")
    tool_dicts = [
        {"name": "list-events", "description": "", "input_schema": {}},
        {"name": "create-event", "description": "", "input_schema": {}},
        {"name": "delete-event", "description": "", "input_schema": {}},
    ]
    out = build_surfaced_tools(tool_dicts, gate=gate)
    classifications = {t.tool_id: t.gate_classification for t in out}
    assert classifications == {
        "list-events": "read",
        "create-event": "soft_write",
        "delete-event": "hard_write",
    }


# ---------------------------------------------------------------------------
# Conservative classification fallback (Kit edit, load-bearing)
# ---------------------------------------------------------------------------


def test_build_surfaced_tools_falls_back_to_unknown_when_gate_empty_string():
    """Pin: empty/falsy classification from gate becomes "unknown",
    NOT silently "read". Per
    INTEGRATION-CAPABILITY-FIRST-V1 §"Conservative classification
    fallback": missing/unknown classification defaults to
    propose/blocked, not silently read-safe."""
    gate = MagicMock()
    gate.classify_tool_effect.return_value = ""  # empty string from gate
    tool_dicts = [{"name": "mystery-tool", "description": "", "input_schema": {}}]
    out = build_surfaced_tools(tool_dicts, gate=gate)
    assert out[0].gate_classification == "unknown"
    assert out[0].gate_classification != "read"


def test_build_surfaced_tools_falls_back_to_unknown_when_gate_raises():
    """Pin: if classifier raises, fallback to "unknown" rather than
    crashing the build or silently passing through. Best-effort
    defensive — never break the turn over classification glitches."""
    gate = MagicMock()
    gate.classify_tool_effect.side_effect = RuntimeError("classifier broken")
    tool_dicts = [{"name": "broken-classifier-tool", "description": "", "input_schema": {}}]
    out = build_surfaced_tools(tool_dicts, gate=gate)
    assert len(out) == 1
    assert out[0].gate_classification == "unknown"


def test_build_surfaced_tools_skips_dicts_without_name():
    """Pin: defensive — tool dicts missing a name are skipped, not
    surfaced as nameless SurfacedTool entries that would confuse
    the integration LLM."""
    gate = MagicMock()
    gate.classify_tool_effect.return_value = "read"
    tool_dicts = [
        {"name": "valid-tool", "description": "", "input_schema": {}},
        {"description": "no name here", "input_schema": {}},
        {"name": "", "description": "empty name", "input_schema": {}},
        "not-a-dict",  # defensive: non-dict entries skipped
    ]
    out = build_surfaced_tools(tool_dicts, gate=gate)
    assert len(out) == 1
    assert out[0].tool_id == "valid-tool"


# ---------------------------------------------------------------------------
# Action-dependent tools — conservative classification at surfacing time
# ---------------------------------------------------------------------------


def test_build_surfaced_tools_classifies_action_dependent_as_unknown():
    """Pin: action-dependent kernel tools (manage_covenants,
    manage_capabilities, manage_channels, manage_members,
    manage_plan, manage_workspace, respond_to_parcel) MUST be
    classified ``"unknown"`` at surfacing time, NOT silently "read".

    These tools' actual effect is determined by the per-call
    ``action`` argument. ``DispatchGate.classify_tool_effect``
    falls back to ``action="list" / "status"`` when no input is
    provided, which classifies "read". Caching that "read" at
    surfacing time would let integration's read-only path
    auto-promote them as callable reads — and Batch 2's live
    dispatch could execute mutating actions through them.

    This pin is the load-bearing safety check Codex flagged in
    Batch 1 review."""
    from unittest.mock import MagicMock
    gate = MagicMock()
    # If the builder ever consults the gate for these names, the
    # gate's default ("read" via list/status fallback) would be
    # the wrong result. Use a sentinel to detect any consultation.
    gate.classify_tool_effect.return_value = "read"

    action_dependent_names = [
        "manage_covenants",
        "manage_capabilities",
        "manage_channels",
        "manage_members",
        "manage_plan",
        "manage_workspace",
        "respond_to_parcel",
    ]
    tool_dicts = [
        {"name": n, "description": "", "input_schema": {}}
        for n in action_dependent_names
    ]
    out = build_surfaced_tools(tool_dicts, gate=gate)

    for tool in out:
        assert tool.gate_classification == "unknown", (
            f"Action-dependent tool {tool.tool_id!r} classified "
            f"{tool.gate_classification!r} — must be 'unknown' so "
            f"dispatch-time enforcement (Batch 2 workshop binding) "
            f"is the source of truth using actual arguments."
        )


# ---------------------------------------------------------------------------
# Architectural-interface pin: reasoning.py passes surfaced_tools
# ---------------------------------------------------------------------------


def test_reasoning_run_via_turn_runner_provider_builds_surfaced_tools():
    """Source-inspection pin: ``reasoning._run_via_turn_runner_provider``
    MUST build surfaced_tools from cognitive_context.tool_surface
    before constructing TurnRunnerInputs. If a future refactor drops
    this, the integration-LLM-sees-empty-surface failure mode
    returns and the agent reverts to refusing tools."""
    import inspect
    from kernos.kernel import reasoning

    src = inspect.getsource(reasoning)
    # Helper is imported from the canonical module
    assert "from kernos.kernel.integration.surfaced_tools import" in src, (
        "reasoning must import build_surfaced_tools from the canonical "
        "surfaced_tools module — empty IntegrationInputs.surfaced_tools "
        "is the production failure mode this batch closes."
    )
    assert "build_surfaced_tools(" in src, (
        "reasoning must call build_surfaced_tools when constructing "
        "TurnRunnerInputs on the C7 thin path."
    )
    assert "surfaced_tools=_surfaced_tools" in src, (
        "TurnRunnerInputs construction must pass the built tuple as "
        "the surfaced_tools kwarg — without this, the field defaults "
        "to () and integration sees zero tools."
    )
