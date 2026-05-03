"""Capability-readiness contract — semantic, not string-ban.

INTEGRATION-CAPABILITY-FIRST-V1 Batch 1 acceptance criterion (per the
design review's edits 5 + 6):

> Capability-readiness contract test, semantic-not-string-ban:
> weather/calendar-style request + read tool surfaced ⇒ model
> invocation receives prompt that allows/encourages tool call, not
> one that says "no tool available." Plain-text-no-tools default
> behavior must affirmatively go away, not just lose its forbid-tools
> string.

This file tests the SEMANTIC property: with the kind prompts rewritten
capability-first AND the tool-use loop in place, a model that decides
to call a tool actually reaches the dispatcher. Pre-spec the system
prompt told the model not to use tools, so even if integration
surfaced them and the loop dispatched them, the model wouldn't call.
Post-spec the prompt is permissive and a calling model lands at the
dispatcher.

The test is end-to-end against a mock model: we don't run a real LLM
call, but we DO run the full path through PresenceRenderer with a
realistic kind-aware system prompt and verify the dispatcher fires
for read-tool calls. This is the contract: capability is not gated
by the prompt.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from kernos.kernel.enactment.presence_renderer import (
    _SYSTEM_PROMPT_CONSTRAINED_RESPONSE,
    _SYSTEM_PROMPT_FULL_MACHINERY_TERMINAL,
    _SYSTEM_PROMPT_PROPOSE_TOOL,
    _SYSTEM_PROMPT_RESPOND_ONLY,
    PresenceRenderer,
)
from kernos.kernel.integration.briefing import (
    AuditTrace,
    Briefing,
    RespondOnly,
)
from kernos.providers.base import ContentBlock, ProviderResponse


def _briefing_respond_only(turn_id: str = "conv-cap") -> Briefing:
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=RespondOnly(),
        presence_directive="Be helpful and direct.",
        audit_trace=AuditTrace(),
        turn_id=turn_id,
        integration_run_id="run-cap",
    )


def _resp_tool_use(name: str, tu_id: str = "tu1") -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="tool_use", id=tu_id, name=name, input={})],
        stop_reason="tool_use",
        input_tokens=1,
        output_tokens=1,
    )


def _resp_text(text: str = "ok") -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="text", text=text)],
        stop_reason="end_turn",
        input_tokens=1,
        output_tokens=1,
    )


# ---------------------------------------------------------------------------
# Semantic contract — capability emerges end-to-end
# ---------------------------------------------------------------------------


async def test_respond_only_kind_with_real_surface_permits_tool_use_in_prompt():
    """Semantic contract end-to-end: weather/calendar-style request +
    read tool surfaced via ``cognitive_context`` ⇒ the prompt the chain
    caller receives:
      - includes the surfaced tool's definition in the tools array
      - carries kind-aware system content that does NOT forbid tool use
      - reaches the dispatcher when the model emits a tool_use

    Pre-spec the kind prompt forbade tool calls and the loop didn't
    exist; both layers blocked capability even when a tool was
    surfaced. Post-spec the prompt is permissive AND tools land in
    the chain caller's tools= argument AND the loop dispatches a
    model-initiated call.

    This is the load-bearing capability assertion the spec calls for
    in §"Batch 1, required acceptance criteria":
    'weather/calendar-style request + read tool surfaced ⇒ model
    invocation receives prompt that allows/encourages tool call,
    not one that says "no tool available."' """
    from kernos.kernel.cognitive_context.types import ToolSurface
    from types import SimpleNamespace

    # Real-shape calendar read tool (matches MCP-served list-events).
    list_events_tool = {
        "name": "list-events",
        "description": "List calendar events for the user",
        "input_schema": {
            "type": "object",
            "properties": {
                "calendar": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    }

    # Duck-type a minimal cognitive_context shim with just the
    # `tool_surface` attribute the renderer reads via getattr. Keeps
    # the test focused on the capability semantic without forcing
    # full CognitiveContext block construction.
    packet = SimpleNamespace(
        tool_surface=ToolSurface(
            always_pinned=(),
            active_zone=(list_events_tool,),
            request_tool=None,
        ),
    )

    briefing = Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=RespondOnly(),
        presence_directive=(
            "User asks about today's calendar. Help them."
        ),
        audit_trace=AuditTrace(),
        turn_id="conv-cap-real",
        integration_run_id="run-cap-real",
        cognitive_context=packet,
    )

    captured: dict = {}

    async def chain(system, messages, tools, max_tokens, **kwargs):
        captured.setdefault("calls", []).append({
            "system": system,
            "tools": list(tools),
        })
        # First call: model decides to use the surfaced calendar tool.
        if len(captured["calls"]) == 1:
            return _resp_tool_use("list-events", tu_id="tu_a")
        return _resp_text("You have a meeting at 2pm.")

    dispatched: list[str] = []

    async def dispatcher(tool_name, tool_input, tool_use_id, conversation_id):
        dispatched.append(tool_name)
        return "Meeting at 2pm"

    renderer = PresenceRenderer(chain_caller=chain, tool_dispatcher=dispatcher)
    result = await renderer.render(briefing)

    # === SEMANTIC ASSERTIONS ===

    # 1. The surfaced read tool actually reaches the chain caller's
    #    tools= argument (not just present in some upstream container).
    first_call = captured["calls"][0]
    tool_names_in_first_call = {t.get("name") for t in first_call["tools"]}
    assert "list-events" in tool_names_in_first_call, (
        f"Surfaced calendar tool must reach the chain caller's tools "
        f"argument. Got tool names: {tool_names_in_first_call}"
    )

    # 2. The system prompt sent to the model on the first call does
    #    NOT contain anti-capability phrasing. This is the
    #    "affirmatively go away" half — semantic, not string-ban: we
    #    assert the prompt isn't telling the model not to call tools.
    system_prompt = first_call["system"]
    if isinstance(system_prompt, list):
        system_prompt = "\n".join(
            b.get("text", "") for b in system_prompt if isinstance(b, dict)
        )
    lower = system_prompt.lower()
    forbid_phrases = [
        "no tool calls",
        "do not execute the tool",
        "do not call",
        "no tool available",
    ]
    for phrase in forbid_phrases:
        assert phrase not in lower, (
            f"System prompt sent to chain caller still contains "
            f"anti-capability phrase {phrase!r}. Capability-first "
            f"contract violated."
        )

    # 3. Model-initiated tool call lands at the dispatcher.
    assert dispatched == ["list-events"], (
        f"Capability-first contract: model-initiated tool call on a "
        f"RESPOND_ONLY turn with the tool in surface must reach the "
        f"dispatcher. Got: {dispatched}"
    )
    assert "2pm" in result.text


# ---------------------------------------------------------------------------
# Structural — pre-spec forbid strings affirmatively gone
# ---------------------------------------------------------------------------


def test_kind_prompts_dropped_pre_spec_forbid_strings():
    """Companion structural pin: the four affected kind prompts no
    longer carry the pre-spec literal forbid strings. This is
    necessary-but-not-sufficient — the semantic test above is the
    real contract — but a regression on this string-level check
    is a fast warning sign that someone re-introduced the
    anti-capability framing."""
    forbidden_phrases = [
        "No tool calls.",
        "Do NOT execute the tool",
    ]
    affected_prompts = {
        "RESPOND_ONLY": _SYSTEM_PROMPT_RESPOND_ONLY,
        "CONSTRAINED_RESPONSE": _SYSTEM_PROMPT_CONSTRAINED_RESPONSE,
        "PROPOSE_TOOL": _SYSTEM_PROMPT_PROPOSE_TOOL,
        "FULL_MACHINERY_TERMINAL": _SYSTEM_PROMPT_FULL_MACHINERY_TERMINAL,
    }
    for name, prompt in affected_prompts.items():
        for phrase in forbidden_phrases:
            assert phrase not in prompt, (
                f"Kind prompt {name} contains pre-spec forbid string "
                f"{phrase!r}. Capability-first posture violated. See "
                f"specs/INTEGRATION-CAPABILITY-FIRST-V1.md §'Batch 1, "
                f"piece B'."
            )


def test_kind_prompts_affirmatively_permit_tool_use():
    """Semantic-ish positive pin: each affected kind prompt contains
    language that AFFIRMATIVELY permits tool use, not just the
    absence of a forbid string. Per spec: 'plain-text-no-tools
    default behavior must affirmatively go away, not just lose its
    forbid-tools string.'

    Match is fuzzy (case-insensitive 'tool' + permission verb) so
    rewording is permitted without test churn — the contract is
    'tool use is permitted in some form,' not exact phrasing."""
    affected_prompts = {
        "RESPOND_ONLY": _SYSTEM_PROMPT_RESPOND_ONLY,
        "CONSTRAINED_RESPONSE": _SYSTEM_PROMPT_CONSTRAINED_RESPONSE,
        "PROPOSE_TOOL": _SYSTEM_PROMPT_PROPOSE_TOOL,
        "FULL_MACHINERY_TERMINAL": _SYSTEM_PROMPT_FULL_MACHINERY_TERMINAL,
    }
    for name, prompt in affected_prompts.items():
        lower = prompt.lower()
        # Tool word present
        assert "tool" in lower, (
            f"Kind prompt {name} doesn't mention tools at all — "
            f"capability-first posture means tool use should at least "
            f"be acknowledged in the prompt."
        )
        # Permission verb present (call/use/serve/help/permitted/appropriate)
        permits = any(verb in lower for verb in (
            "call it",
            "call them",
            "use tool",
            "use it",
            "permitted",
            "appropriate",
            "if a tool",
            "if the tool",
            "tool calls are",
            "tool call would",
        ))
        assert permits, (
            f"Kind prompt {name} mentions tools but doesn't "
            f"affirmatively permit their use. The plain-text-no-tools "
            f"default must affirmatively go away. Current prompt:\n"
            f"---\n{prompt}\n---"
        )


# ---------------------------------------------------------------------------
# Conservative classification fallback (Kit edit, load-bearing)
# ---------------------------------------------------------------------------


def test_unknown_tool_classification_does_not_silently_default_to_read():
    """Spec acceptance: 'Conservative classification fallback:
    missing/unknown classification defaults to propose/blocked, not
    silently read-safe.' Verifies the surfaced_tools builder NEVER
    emits classification 'read' for tools the gate cannot place."""
    from unittest.mock import MagicMock
    from kernos.kernel.integration.surfaced_tools import build_surfaced_tools

    gate = MagicMock()
    gate.classify_tool_effect.return_value = "unknown"

    out = build_surfaced_tools(
        [{"name": "mystery", "description": "", "input_schema": {}}],
        gate=gate,
    )
    assert out[0].gate_classification == "unknown"
    assert out[0].gate_classification != "read", (
        "Conservative classification fallback violated: unknown tool "
        "got classified as 'read'. Per spec, unknown defaults to "
        "propose/blocked, not silently read-safe."
    )


def test_falsy_classification_does_not_silently_default_to_read():
    """Companion conservative-fallback pin: a None / empty / False
    return from the gate also does NOT silently land as 'read'."""
    from unittest.mock import MagicMock
    from kernos.kernel.integration.surfaced_tools import build_surfaced_tools

    for falsy in (None, "", False, 0):
        gate = MagicMock()
        gate.classify_tool_effect.return_value = falsy
        out = build_surfaced_tools(
            [{"name": f"tool-{falsy!r}", "description": "", "input_schema": {}}],
            gate=gate,
        )
        assert out[0].gate_classification != "read", (
            f"Falsy classification {falsy!r} silently defaulted to "
            f"'read' — conservative fallback violated."
        )
