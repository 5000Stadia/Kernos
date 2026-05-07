"""Tests for the concrete PresenceRenderer (IWL C4).

Coverage:
  - Conforms to PDI's shipped PresenceRendererLike Protocol.
  - render() returns awaited PresenceRenderResult (NOT iterator).
  - Kind-aware branching: each ActionKind picks the right system prompt.
  - B1RenderInputs / B2RenderInputs structurally exclude
    discovered_information — sentinel test pins this in BOTH the
    prompt input AND the rendered output.
  - render_b1 / render_b2 take dedicated safe input types.
  - PresenceRenderResult shape preserved (text + streamed).
"""

from __future__ import annotations

import pytest

from kernos.kernel.enactment.presence_renderer import (
    B1RenderInputs,
    B2RenderInputs,
    DEFAULT_PRESENCE_MAX_TOKENS,
    PresenceRenderer,
    build_presence_renderer,
)
from kernos.kernel.enactment.service import (
    PresenceRenderResult,
    PresenceRendererLike,
)
from kernos.kernel.integration.briefing import (
    ActionEnvelope,
    AuditTrace,
    Briefing,
    ClarificationNeeded,
    ClarificationPartialState,
    ConstrainedResponse,
    Defer,
    ExecuteTool,
    Pivot,
    ProposeTool,
    RespondOnly,
)
from kernos.providers.base import ContentBlock, ProviderResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _briefing(decided_action, **extra) -> Briefing:
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=decided_action,
        presence_directive="x",
        audit_trace=AuditTrace(),
        turn_id="turn-pr",
        integration_run_id="run-pr",
        **extra,
    )


def _execute_briefing() -> Briefing:
    return _briefing(
        ExecuteTool(tool_id="email_send", arguments={}),
        action_envelope=ActionEnvelope(
            intended_outcome="send the email",
            allowed_tool_classes=("email",),
            allowed_operations=("send",),
        ),
    )


def _resp(text: str) -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="text", text=text)],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=20,
    )


def _capture_chain(text: str = "rendered text", captured: dict | None = None):
    async def chain(system, messages, tools, max_tokens, **kwargs):
        if captured is not None:
            captured["system"] = system
            captured["messages"] = messages
            captured["tools"] = tools
            captured["max_tokens"] = max_tokens
            # Capture forward-compat kwargs (e.g. conversation_id added
            # in CCV1-C7 wire-shape plumbing fix) so pin tests can
            # assert on them.
            captured["kwargs"] = kwargs
        return _resp(text)

    return chain


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_presence_renderer_conforms_to_protocol():
    renderer = PresenceRenderer(chain_caller=_capture_chain())
    assert isinstance(renderer, PresenceRendererLike)


def test_factory_returns_renderer():
    renderer = build_presence_renderer(chain_caller=_capture_chain())
    assert isinstance(renderer, PresenceRenderer)


def test_default_max_tokens_documented():
    assert DEFAULT_PRESENCE_MAX_TOKENS == 2048


# ---------------------------------------------------------------------------
# Awaited render (NOT AsyncIterator)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_returns_awaited_presence_render_result():
    """Per the design review edit: render returns awaited PresenceRenderResult,
    NOT AsyncIterator. Pin verifies the return shape."""
    renderer = PresenceRenderer(chain_caller=_capture_chain("hello"))
    result = await renderer.render(_briefing(RespondOnly()))
    assert isinstance(result, PresenceRenderResult)
    assert result.text == "hello"
    # `streamed` field exists with the documented shape.
    assert hasattr(result, "streamed")


# ---------------------------------------------------------------------------
# Kind-aware branching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_prepends_forwarded_tool_results():
    """INTEGRATION-RENDERER-RESULT-FORWARD-V1 (2026-05-07): when the
    integration runner has captured tool results during synthesis,
    the renderer prepends them to the user message so the model
    already has the content and doesn't re-dispatch the same read.
    """
    captured: dict = {}
    renderer = PresenceRenderer(
        chain_caller=_capture_chain(captured=captured),
    )
    audit = AuditTrace(
        tool_results_during_prep=(
            {
                "tool_name": "read_file",
                "result": "audit content body here",
            },
        ),
    )
    briefing = Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=RespondOnly(),
        presence_directive="render the audit",
        audit_trace=audit,
        turn_id="turn-x",
        integration_run_id="run-x",
    )
    await renderer.render(briefing)

    user_msg = captured["messages"][0]["content"]
    assert "Prior tool results (already fetched this turn)" in user_msg
    assert "read_file" in user_msg
    assert "audit content body here" in user_msg


@pytest.mark.asyncio
async def test_render_omits_forward_block_when_no_tool_results():
    """When the integration runner didn't dispatch any tools, no
    forward block is added — the user message is unchanged from
    the pre-spec shape."""
    captured: dict = {}
    renderer = PresenceRenderer(
        chain_caller=_capture_chain(captured=captured),
    )
    await renderer.render(_briefing(RespondOnly()))
    user_msg = captured["messages"][0]["content"]
    assert "Prior tool results" not in user_msg


@pytest.mark.asyncio
async def test_respond_only_uses_respond_only_prompt():
    captured = {}
    renderer = PresenceRenderer(chain_caller=_capture_chain(captured=captured))
    await renderer.render(_briefing(RespondOnly()))
    assert "conversational reply" in captured["system"].lower()


@pytest.mark.asyncio
async def test_defer_uses_defer_prompt():
    captured = {}
    renderer = PresenceRenderer(chain_caller=_capture_chain(captured=captured))
    await renderer.render(
        _briefing(Defer(reason="x", follow_up_signal="y"))
    )
    assert "defer" in captured["system"].lower()


@pytest.mark.asyncio
async def test_constrained_response_uses_constrained_prompt():
    captured = {}
    renderer = PresenceRenderer(chain_caller=_capture_chain(captured=captured))
    await renderer.render(
        _briefing(ConstrainedResponse(constraint="t", satisfaction_partial="b"))
    )
    assert "constrained" in captured["system"].lower()


@pytest.mark.asyncio
async def test_pivot_uses_pivot_prompt():
    captured = {}
    renderer = PresenceRenderer(chain_caller=_capture_chain(captured=captured))
    await renderer.render(
        _briefing(Pivot(reason="x", suggested_shape="redirect"))
    )
    assert "pivot" in captured["system"].lower()


@pytest.mark.asyncio
async def test_propose_tool_uses_propose_prompt():
    captured = {}
    renderer = PresenceRenderer(chain_caller=_capture_chain(captured=captured))
    await renderer.render(
        _briefing(ProposeTool(tool_id="x", arguments={}, reason="r"))
    )
    assert "proposal" in captured["system"].lower()
    # MUST NOT execute — render returns proposal text only.
    # (Render-only is structural; renderer has no dispatcher.)


@pytest.mark.asyncio
async def test_clarification_first_pass_uses_clarification_prompt():
    captured = {}
    renderer = PresenceRenderer(chain_caller=_capture_chain(captured=captured))
    await renderer.render(
        _briefing(
            ClarificationNeeded(
                question="What did you mean?", ambiguity_type="target",
            )
        )
    )
    assert "clarification" in captured["system"].lower()


@pytest.mark.asyncio
async def test_execute_tool_terminal_uses_full_machinery_prompt():
    """For full-machinery completions, render(briefing) is called
    AFTER all steps complete. The prompt indicates streaming is now
    permitted (loop has terminated)."""
    captured = {}
    renderer = PresenceRenderer(chain_caller=_capture_chain(captured=captured))
    await renderer.render(_execute_briefing())
    assert "completed" in captured["system"].lower()


# ---------------------------------------------------------------------------
# B1 / B2 STRUCTURAL SAFETY — sentinel test (design review-mandated)
# ---------------------------------------------------------------------------


SENTINEL = "RESTRICTED_SENTINEL_XYZ"


def test_b2_render_inputs_does_not_have_discovered_information_field():
    """Construction-time invariant: B2RenderInputs has no
    `discovered_information` field. The renderer literally cannot
    access it because the type doesn't carry it."""
    from dataclasses import fields
    names = {f.name for f in fields(B2RenderInputs)}
    assert "discovered_information" not in names, (
        "B2RenderInputs must structurally exclude discovered_information"
    )


def test_b1_render_inputs_does_not_have_discovered_information_field():
    """Same structural invariant for B1RenderInputs."""
    from dataclasses import fields
    names = {f.name for f in fields(B1RenderInputs)}
    assert "discovered_information" not in names, (
        "B1RenderInputs must structurally exclude discovered_information"
    )


def test_b2_render_inputs_from_partial_state_drops_discovered_information():
    """The factory `from_partial_state()` constructor takes a
    ClarificationPartialState (which DOES carry discovered_information)
    and drops the field on the floor by design."""
    partial = ClarificationPartialState(
        attempted_action_summary="started drafting",
        discovered_information=SENTINEL,  # the sentinel restricted field
        blocking_ambiguity="which recipient",
        safe_question_context="confirming target choice",
        audit_refs=("audit-1",),
    )
    safe = B2RenderInputs.from_partial_state(
        question="Did you mean Henry?", partial_state=partial,
    )
    # The sentinel does NOT appear anywhere on the safe input.
    safe_repr = (
        safe.question
        + safe.blocking_ambiguity
        + safe.safe_question_context
        + " ".join(safe.audit_refs)
    )
    assert SENTINEL not in safe_repr


@pytest.mark.asyncio
async def test_b2_sentinel_absent_from_renderer_prompt_input_and_output():
    """Design Review-mandated sentinel pin (acceptance criterion #18):
    seed discovered_information with a restricted sentinel; verify
    the sentinel is absent from BOTH the renderer's prompt input AND
    the rendered output text.

    Structural enforcement: B2RenderInputs has no
    discovered_information field, so the prompt builder cannot reach
    the sentinel even if a future bug tried to pass it."""
    captured: dict = {}
    renderer = PresenceRenderer(
        chain_caller=_capture_chain(text="here is the question", captured=captured)
    )
    partial = ClarificationPartialState(
        attempted_action_summary="started drafting",
        discovered_information=SENTINEL,
        blocking_ambiguity="which recipient",
        safe_question_context="confirming target choice",
        audit_refs=("audit-1",),
    )
    briefing = _briefing(
        ClarificationNeeded(
            question="Did you mean Henry?",
            ambiguity_type="target",
            partial_state=partial,
        )
    )
    safe = B2RenderInputs.from_partial_state(
        question=briefing.decided_action.question,
        partial_state=partial,
    )
    result = await renderer.render_b2(briefing, safe)

    # Sentinel absent from prompt input (system + user message).
    prompt_text = (
        captured["system"]
        + "\n"
        + "\n".join(
            m.get("content", "") if isinstance(m.get("content"), str)
            else str(m.get("content", ""))
            for m in captured["messages"]
        )
    )
    assert SENTINEL not in prompt_text, (
        f"sentinel {SENTINEL!r} reached the renderer's prompt input"
    )

    # Sentinel absent from rendered output text.
    assert SENTINEL not in result.text


@pytest.mark.asyncio
async def test_b1_sentinel_absent_from_renderer_prompt_input():
    """B1 counterpart: B1RenderInputs has no discovered_information
    field; the sentinel cannot be reached even by a malformed
    upstream caller."""
    captured: dict = {}
    renderer = PresenceRenderer(
        chain_caller=_capture_chain(captured=captured)
    )
    safe = B1RenderInputs(
        intended_outcome_summary="send the email",
        attempted_action_summary="started drafting; covenant blocked",
        audit_refs=("audit-b1-1",),
    )
    briefing = _execute_briefing()
    await renderer.render_b1(briefing, safe)

    prompt_text = captured["system"] + "\n" + "\n".join(
        m.get("content", "") if isinstance(m.get("content"), str)
        else str(m.get("content", ""))
        for m in captured["messages"]
    )
    # The sentinel was never on B1RenderInputs in the first place.
    assert SENTINEL not in prompt_text


@pytest.mark.asyncio
async def test_b2_render_does_not_include_partial_state_directly():
    """Defensive pin: render_b2 takes B2RenderInputs (safe). It does
    NOT accept ClarificationPartialState directly. A test that tries
    to pass a partial_state where B2RenderInputs is expected fails
    at the type level — verified via inspection of render_b2's
    signature."""
    import inspect
    sig = inspect.signature(PresenceRenderer.render_b2)
    safe_param = sig.parameters.get("safe")
    assert safe_param is not None
    # The annotation is B2RenderInputs (or its forward ref).
    annotation = safe_param.annotation
    # Either the literal class or a string reference to it.
    annotation_repr = (
        annotation.__name__ if hasattr(annotation, "__name__")
        else str(annotation)
    )
    assert "B2RenderInputs" in annotation_repr


# ---------------------------------------------------------------------------
# Streaming flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_returns_streamed_false_by_default():
    """The renderer's internal _render returns streamed=False by
    default. Production wiring (IWL C5) wraps in an adapter that
    streams and sets the flag accordingly."""
    renderer = PresenceRenderer(chain_caller=_capture_chain("text"))
    result = await renderer.render(_briefing(RespondOnly()))
    assert result.streamed is False


# ---------------------------------------------------------------------------
# PresenceRenderResult shape preserved
# ---------------------------------------------------------------------------


def test_presence_render_result_has_text_and_streamed_only():
    """PDI-shipped shape: PresenceRenderResult is text + streamed.
    No invented fields."""
    from dataclasses import fields
    names = {f.name for f in fields(PresenceRenderResult)}
    assert names == {"text", "streamed"}


# ---------------------------------------------------------------------------
# Same-model default (chain_caller invoked uniformly)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_chain_caller_invoked_for_all_kinds():
    """v1 same-model default. Chain caller is invoked for each kind;
    differentiation via prompt, not via chain selection."""
    invocations = []

    async def chain(system, messages, tools, max_tokens, **_):
        invocations.append(system)
        return _resp("x")

    renderer = PresenceRenderer(chain_caller=chain)
    await renderer.render(_briefing(RespondOnly()))
    await renderer.render(_briefing(Defer(reason="x", follow_up_signal="y")))
    await renderer.render(
        _briefing(ConstrainedResponse(constraint="t", satisfaction_partial="b"))
    )
    await renderer.render(
        _briefing(Pivot(reason="x", suggested_shape="redirect"))
    )
    await renderer.render(
        _briefing(ProposeTool(tool_id="x", arguments={}, reason="r"))
    )
    await renderer.render(
        _briefing(
            ClarificationNeeded(question="?", ambiguity_type="target")
        )
    )
    await renderer.render(_execute_briefing())

    assert len(invocations) == 7
