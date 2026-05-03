"""Receipt-grade pin tests for the thin-path tool-use loop.

INTEGRATION-CAPABILITY-FIRST-V1 Batch 1 piece C: PresenceRenderer's
``_render`` previously called ``chain_caller`` once and silently
dropped tool_use blocks. The bounded loop dispatches tool_use, appends
tool_result, and re-invokes chain_caller until either text-only
response or ``max_tool_iterations`` (whichever first).

Pin coverage per spec acceptance criteria:
  (a) multiple ``tool_use`` blocks handled
  (b) ``tool_use_id`` preserved through to corresponding ``tool_result``
  (c) ``conversation_id`` forwarded every iteration
  (d) MESSAGE-THREAD parity with legacy: assistant block carries
      tool_use, user block carries tool_result, alternation matches
      Anthropic-style API. Trace/audit/event parity (legacy
      ``TOOL_CALLED`` event emission) is OWNED BY THE DISPATCHER
      LAYER, not the renderer loop — Batch 2 wires the dispatcher to
      the executor that emits those events. This pin verifies the
      shape the dispatcher receives, not the dispatcher's own
      side-effects.
  (e) max-iteration friendly failure (not silent drop)
  (f) dispatcher called once per actual tool_use, not per attempt

Plus a backward-compat baseline: no dispatcher → single call (legacy
behavior unchanged).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from kernos.kernel.enactment.presence_renderer import PresenceRenderer
from kernos.kernel.integration.briefing import (
    AuditTrace,
    Briefing,
    RespondOnly,
)
from kernos.providers.base import ContentBlock, ProviderResponse


def _briefing(turn_id: str = "conv-loop") -> Briefing:
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=RespondOnly(),
        presence_directive="x",
        audit_trace=AuditTrace(),
        turn_id=turn_id,
        integration_run_id="run-loop",
    )


def _resp_text(text: str = "ok") -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="text", text=text)],
        stop_reason="end_turn",
        input_tokens=1,
        output_tokens=1,
    )


def _resp_tool_use(tools: list[tuple[str, str, dict]]) -> ProviderResponse:
    """Build a response with one or more tool_use blocks.

    ``tools`` is a list of (id, name, input) tuples.
    """
    return ProviderResponse(
        content=[
            ContentBlock(type="tool_use", id=tid, name=name, input=inp)
            for tid, name, inp in tools
        ],
        stop_reason="tool_use",
        input_tokens=1,
        output_tokens=1,
    )


# ---------------------------------------------------------------------------
# Baseline — no dispatcher = legacy single-call behavior
# ---------------------------------------------------------------------------


async def test_render_without_dispatcher_calls_chain_once_text_extracted():
    """Baseline pin: when tool_dispatcher is None, the renderer
    behaves exactly as the pre-spec version — one chain call, text
    extracted, no loop. Legacy callers keep working unchanged."""
    chain = AsyncMock(return_value=_resp_text("hello"))
    renderer = PresenceRenderer(chain_caller=chain)
    result = await renderer.render(_briefing())
    assert chain.await_count == 1
    assert result.text == "hello"


# ---------------------------------------------------------------------------
# (a) Multiple tool_use blocks handled in single response
# ---------------------------------------------------------------------------


async def test_render_handles_multiple_tool_use_blocks_in_single_response():
    """Pin (a): a response with multiple tool_use blocks dispatches
    all of them and emits all tool_result blocks in one user message
    before next iteration."""
    dispatched: list[str] = []

    async def dispatcher(tool_name, tool_input, tool_use_id, conversation_id):
        dispatched.append(tool_name)
        return f"result-of-{tool_name}"

    chain = AsyncMock(side_effect=[
        _resp_tool_use([
            ("tu1", "list-events", {"q": "today"}),
            ("tu2", "brave_web_search", {"q": "weather"}),
            ("tu3", "get-current-time", {}),
        ]),
        _resp_text("done"),
    ])
    renderer = PresenceRenderer(chain_caller=chain, tool_dispatcher=dispatcher)
    result = await renderer.render(_briefing())

    assert dispatched == ["list-events", "brave_web_search", "get-current-time"]
    assert result.text == "done"


# ---------------------------------------------------------------------------
# (b) tool_use_id preserved through to tool_result
# ---------------------------------------------------------------------------


async def test_render_preserves_tool_use_id_through_to_tool_result():
    """Pin (b): tool_result blocks reference the same tool_use_id
    as the tool_use that triggered them. Provider correlation depends
    on this — without ID preservation, the model can't match results
    to its prior calls."""
    captured_messages: list[list[dict]] = []

    async def chain(system, messages, tools, max_tokens, **_):
        captured_messages.append([dict(m) for m in messages])
        if len(captured_messages) == 1:
            return _resp_tool_use([
                ("tu_alpha", "list-events", {}),
                ("tu_beta", "get-current-time", {}),
            ])
        return _resp_text("done")

    async def dispatcher(tool_name, tool_input, tool_use_id, conversation_id):
        return f"result for {tool_use_id}"

    renderer = PresenceRenderer(chain_caller=chain, tool_dispatcher=dispatcher)
    await renderer.render(_briefing())

    # Second iteration's messages: the tool_result blocks must carry
    # the original tool_use_ids verbatim.
    second_iter_messages = captured_messages[1]
    tool_result_msg = second_iter_messages[-1]
    assert tool_result_msg["role"] == "user"
    result_blocks = tool_result_msg["content"]
    ids_in_results = {b["tool_use_id"] for b in result_blocks if b.get("type") == "tool_result"}
    assert ids_in_results == {"tu_alpha", "tu_beta"}


# ---------------------------------------------------------------------------
# (c) conversation_id forwarded every iteration
# ---------------------------------------------------------------------------


async def test_render_forwards_conversation_id_every_iteration():
    """Pin (c): briefing.turn_id reaches chain_caller as
    conversation_id on every loop iteration, not just the first.
    Without this, mid-loop chain calls miss prompt_cache_key + session
    correlation headers and the Codex provider's wire-shape repair
    silently disables on iterations 2+."""
    captured_kwargs: list[dict] = []

    async def chain(system, messages, tools, max_tokens, **kwargs):
        captured_kwargs.append(kwargs)
        if len(captured_kwargs) < 3:
            return _resp_tool_use([(f"tu{len(captured_kwargs)}", "tool", {})])
        return _resp_text("done")

    async def dispatcher(tool_name, tool_input, tool_use_id, conversation_id):
        return "ok"

    renderer = PresenceRenderer(chain_caller=chain, tool_dispatcher=dispatcher)
    await renderer.render(_briefing(turn_id="conv-multi-iter"))

    assert len(captured_kwargs) == 3
    for i, kw in enumerate(captured_kwargs):
        assert kw.get("conversation_id") == "conv-multi-iter", (
            f"iteration {i} dropped conversation_id; got {kw!r}"
        )


# ---------------------------------------------------------------------------
# (d) Message thread shape matches API expectation
# ---------------------------------------------------------------------------


async def test_render_appends_assistant_then_user_tool_result_alternation():
    """Pin (d): MESSAGE-THREAD parity with legacy path. After a
    tool_use response, the next chain call sees
    [original user, assistant(tool_use), user(tool_result), ...].

    NOTE on scope: trace/audit/event parity (legacy ``TOOL_CALLED``
    event emission etc.) is owned by the dispatcher layer, not the
    renderer loop. Batch 2's workshop-binding wiring lands the
    real dispatcher with audit/event semantics; this pin verifies
    the shape the dispatcher receives, not the dispatcher's own
    side-effects. The capability-first contract for piece C is the
    structural correctness of the loop's I/O contract."""
    captured_messages: list[list[dict]] = []

    async def chain(system, messages, tools, max_tokens, **_):
        captured_messages.append([dict(m) for m in messages])
        if len(captured_messages) == 1:
            return _resp_tool_use([("tu1", "tool-x", {"a": 1})])
        return _resp_text("done")

    async def dispatcher(tool_name, tool_input, tool_use_id, conversation_id):
        return "x-result"

    renderer = PresenceRenderer(chain_caller=chain, tool_dispatcher=dispatcher)
    await renderer.render(_briefing())

    # First iteration: just the original user message.
    assert len(captured_messages[0]) == 1
    assert captured_messages[0][0]["role"] == "user"

    # Second iteration: user, assistant (with tool_use), user (with tool_result).
    second = captured_messages[1]
    assert len(second) == 3
    assert second[0]["role"] == "user"
    assert second[1]["role"] == "assistant"
    assert second[2]["role"] == "user"
    # Assistant block carries tool_use; user block carries tool_result.
    assert any(b.get("type") == "tool_use" for b in second[1]["content"])
    assert any(b.get("type") == "tool_result" for b in second[2]["content"])


# ---------------------------------------------------------------------------
# (e) Max-iteration friendly failure (not silent drop)
# ---------------------------------------------------------------------------


async def test_render_max_iterations_returns_friendly_text_not_silent():
    """Pin (e): when the loop hits max_tool_iterations without
    text-only termination, surface a friendly text rather than
    dropping silently. Spec: 'max-iteration friendly failure (not
    silent drop)'."""
    chain = AsyncMock(return_value=_resp_tool_use([("tu", "looper", {})]))

    async def dispatcher(tool_name, tool_input, tool_use_id, conversation_id):
        return "loop-result"

    renderer = PresenceRenderer(
        chain_caller=chain, tool_dispatcher=dispatcher, max_tool_iterations=2,
    )
    result = await renderer.render(_briefing())

    # Loop ran the max iterations (2) — chain called 2 times.
    assert chain.await_count == 2
    # Result is non-empty text describing the cap, not an empty drop.
    assert result.text
    assert "iteration" in result.text.lower() or "limit" in result.text.lower()


# ---------------------------------------------------------------------------
# (f) Dispatcher called once per actual tool_use, not per attempt
# ---------------------------------------------------------------------------


async def test_render_dispatcher_called_once_per_tool_use_block():
    """Pin (f): dispatcher counter equals total tool_use blocks
    encountered. Two iterations with one tool_use each → dispatcher
    called twice. One iteration with three tool_use blocks → called
    three times. Telemetry parity downstream depends on this."""
    dispatch_count = 0

    async def dispatcher(tool_name, tool_input, tool_use_id, conversation_id):
        nonlocal dispatch_count
        dispatch_count += 1
        return "ok"

    # Scenario: 2 iterations, 3 tool_use blocks total.
    chain = AsyncMock(side_effect=[
        _resp_tool_use([("tu1", "a", {}), ("tu2", "b", {})]),
        _resp_tool_use([("tu3", "c", {})]),
        _resp_text("final"),
    ])
    renderer = PresenceRenderer(chain_caller=chain, tool_dispatcher=dispatcher)
    await renderer.render(_briefing())
    assert dispatch_count == 3
    assert chain.await_count == 3


# ---------------------------------------------------------------------------
# Tool-failure resilience (loop continues; not silent drop on dispatcher exception)
# ---------------------------------------------------------------------------


async def test_render_loop_continues_when_dispatcher_raises():
    """Companion pin: if the dispatcher raises on a tool, the loop
    surfaces a friendly tool-error result back to the model rather
    than tearing down the render. Capability-first posture: a single
    tool failure shouldn't kill the whole turn."""
    captured_messages: list[list[dict]] = []

    async def chain(system, messages, tools, max_tokens, **_):
        captured_messages.append([dict(m) for m in messages])
        if len(captured_messages) == 1:
            return _resp_tool_use([("tu1", "broken-tool", {})])
        return _resp_text("recovered")

    async def dispatcher(tool_name, tool_input, tool_use_id, conversation_id):
        raise RuntimeError("backend exploded")

    renderer = PresenceRenderer(chain_caller=chain, tool_dispatcher=dispatcher)
    result = await renderer.render(_briefing())

    assert result.text == "recovered"
    second_iter = captured_messages[1]
    tool_result_block = second_iter[-1]["content"][0]
    assert tool_result_block["type"] == "tool_result"
    assert "tool error" in tool_result_block["content"].lower()
