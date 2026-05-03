"""Pin tests for the C7 thin-path plumbing that restores Codex
wire-shape repair (commit ``e50fb32``).

The Codex provider's wire-shape repair (prompt_cache_key + session
correlation headers) is gated on ``conversation_id`` being non-empty.
The provider-level wire shape is already pinned by
``test_openai_codex.py``. What is NOT pinned at that level: whether
the C7 decoupled-cognition production path actually FORWARDS
``conversation_id`` to the provider, or whether it silently drops it
on the floor — which would re-create the recurrent mid-stream
``server_error`` failures on payloads above ~50KB that e50fb32 closed.

This file pins the plumbing structurally:

1. PresenceRenderer.render() forwards ``briefing.turn_id`` as the
   ``conversation_id`` kwarg to its chain_caller. ``turn_id`` IS the
   upstream conversation_id, renamed at the TurnRunnerInputs boundary
   (``reasoning.py`` builds TurnRunnerInputs with
   ``turn_id=request.conversation_id``).
2. ``response_delivery._wrapped`` forwards ``conversation_id`` through
   to the inner chain_caller without dropping it.
3. Empty turn_id (legacy stub paths, test scaffolding) → kwarg is
   omitted, keeping compatibility with chain_caller stubs that don't
   accept it.

If a future refactor drops conversation_id at any of these seams, the
soak harness's full Discord turns will revert to the pre-e50fb32
shape and start failing mid-stream on large payloads. These pins
catch the regression at unit-test speed.
"""

from __future__ import annotations

from kernos.kernel.enactment.presence_renderer import (
    B1RenderInputs,
    B2RenderInputs,
    PresenceRenderer,
)
from kernos.kernel.integration.briefing import (
    AuditTrace,
    Briefing,
    RespondOnly,
)
from kernos.kernel.response_delivery import (
    AggregatedTelemetry,
    wrap_chain_caller_with_telemetry,
)
from kernos.providers.base import ContentBlock, ProviderResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _briefing(turn_id: str = "conv-pin-xyz") -> Briefing:
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=RespondOnly(),
        presence_directive="x",
        audit_trace=AuditTrace(),
        turn_id=turn_id,
        integration_run_id="run-pin",
    )


def _resp(text: str = "ok") -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="text", text=text)],
        stop_reason="end_turn",
        input_tokens=1,
        output_tokens=1,
    )


def _capture():
    captured = {}

    async def chain(system, messages, tools, max_tokens, **kwargs):
        captured["kwargs"] = kwargs
        return _resp()

    return chain, captured


# ---------------------------------------------------------------------------
# PresenceRenderer.render() — main thin-path entry
# ---------------------------------------------------------------------------


async def test_render_forwards_briefing_turn_id_as_conversation_id():
    """C7 wire-shape plumbing pin: PresenceRenderer.render() must
    pass briefing.turn_id as conversation_id to the chain_caller, so
    the Codex provider's wire-shape repair (prompt_cache_key + session
    correlation) can engage. This is the load-bearing fix that
    restores e50fb32's behavior on the C7 thin path."""
    chain, captured = _capture()
    renderer = PresenceRenderer(chain_caller=chain)

    await renderer.render(_briefing(turn_id="conv-test-123"))

    assert captured["kwargs"].get("conversation_id") == "conv-test-123", (
        f"PresenceRenderer.render() must forward briefing.turn_id as "
        f"conversation_id; got kwargs={captured['kwargs']!r}. Without "
        f"this, the Codex provider's wire-shape repair fields are "
        f"silently disabled and large-payload calls revert to the "
        f"pre-e50fb32 shape that fails mid-stream with server_error."
    )


async def test_render_b1_forwards_briefing_turn_id():
    """Same pin for the B1 termination render path."""
    chain, captured = _capture()
    renderer = PresenceRenderer(chain_caller=chain)

    safe = B1RenderInputs(intended_outcome_summary="x")
    await renderer.render_b1(_briefing(turn_id="conv-b1"), safe)

    assert captured["kwargs"].get("conversation_id") == "conv-b1"


async def test_render_b2_forwards_briefing_turn_id():
    """Same pin for the B2 clarification render path."""
    chain, captured = _capture()
    renderer = PresenceRenderer(chain_caller=chain)

    safe = B2RenderInputs(question="what did you mean?")
    await renderer.render_b2(_briefing(turn_id="conv-b2"), safe)

    assert captured["kwargs"].get("conversation_id") == "conv-b2"


async def test_render_with_empty_turn_id_omits_conversation_id_kwarg():
    """Carve-out pin: when briefing.turn_id is empty (legacy /
    pre-C7 callers, test scaffolding), the renderer must NOT pass
    conversation_id=" " — it must omit the kwarg entirely, keeping
    chain_caller stubs that don't accept conversation_id compatible.
    Without this carve-out, ~17 existing test stubs would break."""
    chain, captured = _capture()
    renderer = PresenceRenderer(chain_caller=chain)

    await renderer.render(_briefing(turn_id=""))

    assert "conversation_id" not in captured["kwargs"], (
        f"Empty turn_id must NOT pass conversation_id kwarg; "
        f"got kwargs={captured['kwargs']!r}"
    )


# ---------------------------------------------------------------------------
# response_delivery wrapper — forwards conversation_id through
# ---------------------------------------------------------------------------


async def test_response_delivery_wrapper_forwards_conversation_id():
    """The per-turn telemetry wrapper around the chain_caller must
    forward conversation_id without dropping it. Pin closes the gap
    that response_delivery sits BETWEEN PresenceRenderer and the
    shared chain_caller — the right place to forget conversation_id
    if a future refactor isn't careful."""
    captured = {}

    async def inner_chain(system, messages, tools, max_tokens, **kwargs):
        captured["kwargs"] = kwargs
        return _resp()

    telemetry = AggregatedTelemetry()
    wrapped = wrap_chain_caller_with_telemetry(inner_chain, telemetry)

    await wrapped("sys", [], [], 100, conversation_id="conv-wrap")

    assert captured["kwargs"].get("conversation_id") == "conv-wrap", (
        f"response_delivery._wrapped must forward conversation_id; "
        f"got {captured['kwargs']!r}"
    )


async def test_response_delivery_wrapper_omits_conversation_id_when_empty():
    """Carve-out pin: empty conversation_id at the wrapper layer
    also omits the kwarg, mirroring the renderer's carve-out so
    inner chain stubs that don't take **kwargs still work."""
    captured = {}

    async def inner_chain(system, messages, tools, max_tokens, **kwargs):
        captured["kwargs"] = kwargs
        return _resp()

    telemetry = AggregatedTelemetry()
    wrapped = wrap_chain_caller_with_telemetry(inner_chain, telemetry)

    await wrapped("sys", [], [], 100, conversation_id="")

    assert "conversation_id" not in captured["kwargs"]


async def test_response_delivery_wrapper_default_omits_conversation_id():
    """Default (no kwarg supplied) also omits — i.e. the wrapper
    treats missing conversation_id and empty conversation_id
    identically. Pre-C7 callers that don't supply it stay working."""
    captured = {}

    async def inner_chain(system, messages, tools, max_tokens, **kwargs):
        captured["kwargs"] = kwargs
        return _resp()

    telemetry = AggregatedTelemetry()
    wrapped = wrap_chain_caller_with_telemetry(inner_chain, telemetry)

    await wrapped("sys", [], [], 100)

    assert "conversation_id" not in captured["kwargs"]
