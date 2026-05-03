"""Tests for ProductionResponseDelivery + translation seam (IWL C5).

Coverage:
  - Translation seam: enactment_outcome_to_reasoning_result targets
    EXACTLY the live ReasoningResult fields. No invented fields.
  - Public ReasoningResult shape NOT widened.
  - Aggregation: tokens / cost summed across hook calls.
  - tool_iterations: 0 for thin-path turns; equals dispatch count
    for full-machinery turns.
  - Synthetic reasoning.request emitted at turn start.
  - Synthetic reasoning.response emitted at turn end (single emission).
  - No-double-count invariant: per-turn reasoning.response count = 1.
  - Drain-ordering invariant: response_delivery does not drain or
    clear the trace sink.
  - Equivalence telemetry: turn_completed_via="decoupled" recorded.
  - wrap_chain_caller_with_telemetry decorator accumulates tokens.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from kernos.kernel.enactment.service import (
    EnactmentOutcome,
    TerminationSubtype,
)
from kernos.kernel.integration.briefing import (
    ActionEnvelope,
    ActionKind,
    AuditTrace,
    Briefing,
    ExecuteTool,
    RespondOnly,
)
from kernos.kernel.reasoning import ReasoningRequest, ReasoningResult
from kernos.kernel.response_delivery import (
    AggregatedTelemetry,
    ProductionResponseDelivery,
    enactment_outcome_to_reasoning_result,
    wrap_chain_caller_with_telemetry,
)
from kernos.providers.base import ContentBlock, ProviderResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request() -> ReasoningRequest:
    return ReasoningRequest(
        instance_id="inst-1",
        conversation_id="conv-1",
        system_prompt="x",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        model="claude-sonnet-4-6",
        trigger="user_message",
        member_id="mem-1",
        active_space_id="space-1",
        input_text="hi",
    )


def _briefing(decided_action=None) -> Briefing:
    if decided_action is None:
        decided_action = RespondOnly()
    extra: dict = {}
    if isinstance(decided_action, ExecuteTool):
        extra["action_envelope"] = ActionEnvelope(
            intended_outcome="x",
            allowed_tool_classes=("email",),
            allowed_operations=("send",),
        )
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=decided_action,
        presence_directive="x",
        audit_trace=AuditTrace(),
        turn_id="turn-rd",
        integration_run_id="run-rd",
        **extra,
    )


def _outcome(
    *,
    subtype: TerminationSubtype = TerminationSubtype.SUCCESS_THIN_PATH,
    decided_action_kind: ActionKind = ActionKind.RESPOND_ONLY,
    text: str = "rendered text",
) -> EnactmentOutcome:
    return EnactmentOutcome(
        text=text,
        subtype=subtype,
        decided_action_kind=decided_action_kind,
    )


# ---------------------------------------------------------------------------
# Translation seam — live ReasoningResult shape preservation
# ---------------------------------------------------------------------------


def test_reasoning_result_shape_not_widened_by_iwl():
    """Acceptance criterion #19: public ReasoningResult shape is NOT
    widened. Exactly the seven shipped fields."""
    names = {f.name for f in fields(ReasoningResult)}
    assert names == {
        "text",
        "model",
        "input_tokens",
        "output_tokens",
        "estimated_cost_usd",
        "duration_ms",
        "tool_iterations",
    }


def test_translation_targets_only_live_fields():
    """No invented fields (tool_calls, assistant_content, stop_reason,
    provider, event_id) on translation output."""
    request = _request()
    telemetry = AggregatedTelemetry(
        input_tokens=100, output_tokens=50, estimated_cost_usd=0.01,
    )
    outcome = _outcome(text="hello")
    result = enactment_outcome_to_reasoning_result(
        outcome,
        request=request,
        telemetry=telemetry,
        duration_ms=123,
        tool_iterations=0,
    )
    assert isinstance(result, ReasoningResult)
    # No invented fields on the dataclass.
    assert not hasattr(result, "tool_calls")
    assert not hasattr(result, "assistant_content")
    assert not hasattr(result, "stop_reason")
    assert not hasattr(result, "provider")
    assert not hasattr(result, "event_id")


def test_translation_text_from_outcome():
    request = _request()
    outcome = _outcome(text="rendered output")
    result = enactment_outcome_to_reasoning_result(
        outcome,
        request=request,
        telemetry=AggregatedTelemetry(),
        duration_ms=0,
    )
    assert result.text == "rendered output"


def test_translation_model_from_request():
    request = _request()
    outcome = _outcome()
    result = enactment_outcome_to_reasoning_result(
        outcome,
        request=request,
        telemetry=AggregatedTelemetry(),
        duration_ms=0,
    )
    assert result.model == request.model


def test_translation_aggregates_tokens_across_hook_calls():
    """Acceptance criterion #20: aggregation across all hook model
    calls in the turn."""
    request = _request()
    telemetry = AggregatedTelemetry()
    telemetry.add(input_tokens=100, output_tokens=50, cost_usd=0.005)  # planner
    telemetry.add(input_tokens=80, output_tokens=40, cost_usd=0.004)   # divergence
    telemetry.add(input_tokens=120, output_tokens=60, cost_usd=0.006)  # presence
    outcome = _outcome()
    result = enactment_outcome_to_reasoning_result(
        outcome,
        request=request,
        telemetry=telemetry,
        duration_ms=500,
    )
    assert result.input_tokens == 300
    assert result.output_tokens == 150
    assert result.estimated_cost_usd == pytest.approx(0.015)


def test_translation_thin_path_tool_iterations_zero():
    """tool_iterations is 0 for thin-path turns."""
    request = _request()
    outcome = _outcome(
        subtype=TerminationSubtype.SUCCESS_THIN_PATH,
        decided_action_kind=ActionKind.RESPOND_ONLY,
    )
    result = enactment_outcome_to_reasoning_result(
        outcome,
        request=request,
        telemetry=AggregatedTelemetry(),
        duration_ms=0,
        tool_iterations=0,
    )
    assert result.tool_iterations == 0


def test_translation_full_machinery_tool_iterations_explicit():
    """tool_iterations equals the explicit count from the dispatcher.
    Wiring layer increments per dispatch."""
    request = _request()
    outcome = _outcome(
        subtype=TerminationSubtype.SUCCESS_FULL_MACHINERY,
        decided_action_kind=ActionKind.EXECUTE_TOOL,
    )
    result = enactment_outcome_to_reasoning_result(
        outcome,
        request=request,
        telemetry=AggregatedTelemetry(),
        duration_ms=0,
        tool_iterations=3,
    )
    assert result.tool_iterations == 3


# ---------------------------------------------------------------------------
# Synthetic reasoning.* events — single emission per turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_delivery_emits_reasoning_request_at_turn_start():
    events = []

    async def emit(payload):
        events.append(payload)

    delivery = ProductionResponseDelivery(
        request=_request(),
        telemetry=AggregatedTelemetry(),
        event_emitter=emit,
    )
    await delivery.emit_request_event()
    assert len(events) == 1
    assert events[0]["type"] == "reasoning.request"
    assert events[0]["trigger"] == "turn_runner"
    assert events[0]["model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_response_delivery_emits_one_reasoning_response_per_turn():
    """No-double-count invariant: exactly ONE `reasoning.response`
    event per new-path turn (acceptance criterion #22)."""
    events = []

    async def emit(payload):
        events.append(payload)

    delivery = ProductionResponseDelivery(
        request=_request(),
        telemetry=AggregatedTelemetry(input_tokens=100, output_tokens=50),
        event_emitter=emit,
    )
    result = await delivery(_briefing(), _outcome())
    response_events = [e for e in events if e["type"] == "reasoning.response"]
    assert len(response_events) == 1
    # Carries aggregated tokens.
    assert response_events[0]["input_tokens"] == 100
    assert response_events[0]["output_tokens"] == 50


@pytest.mark.asyncio
async def test_synthetic_response_carries_trigger_turn_runner():
    events = []

    async def emit(payload):
        events.append(payload)

    delivery = ProductionResponseDelivery(
        request=_request(),
        telemetry=AggregatedTelemetry(),
        event_emitter=emit,
    )
    await delivery(_briefing(), _outcome())
    response = next(e for e in events if e["type"] == "reasoning.response")
    assert response["trigger"] == "turn_runner"


@pytest.mark.asyncio
async def test_synthetic_event_failures_do_not_break_delivery():
    """Best-effort event emission. A broken event store doesn't
    break the turn."""

    async def broken_emit(payload):
        raise RuntimeError("event store unavailable")

    delivery = ProductionResponseDelivery(
        request=_request(),
        telemetry=AggregatedTelemetry(),
        event_emitter=broken_emit,
    )
    # Both emit_request_event and __call__ swallow errors gracefully.
    await delivery.emit_request_event()
    result = await delivery(_briefing(), _outcome())
    assert isinstance(result, ReasoningResult)


@pytest.mark.asyncio
async def test_no_event_emitter_is_silent():
    delivery = ProductionResponseDelivery(
        request=_request(),
        telemetry=AggregatedTelemetry(),
        event_emitter=None,
    )
    await delivery.emit_request_event()
    result = await delivery(_briefing(), _outcome())
    assert isinstance(result, ReasoningResult)


# ---------------------------------------------------------------------------
# Equivalence telemetry hook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_event_records_turn_completed_via_decoupled():
    """Acceptance criterion #26: TurnRunner emits
    `turn.completed_via="decoupled"` metadata on turn-completion.
    Recorded on the synthetic reasoning.response."""
    events = []

    async def emit(payload):
        events.append(payload)

    delivery = ProductionResponseDelivery(
        request=_request(),
        telemetry=AggregatedTelemetry(),
        event_emitter=emit,
    )
    await delivery(_briefing(), _outcome())
    response = next(e for e in events if e["type"] == "reasoning.response")
    assert response["turn_completed_via"] == "decoupled"


@pytest.mark.asyncio
async def test_response_event_carries_termination_subtype_and_kind():
    """Per-path diagnostics: the synthetic event surfaces the
    termination subtype + decided_action_kind so audit consumers
    can filter on either."""
    events = []

    async def emit(payload):
        events.append(payload)

    delivery = ProductionResponseDelivery(
        request=_request(),
        telemetry=AggregatedTelemetry(),
        event_emitter=emit,
    )
    await delivery(
        _briefing(ExecuteTool(tool_id="x", arguments={})),
        _outcome(
            subtype=TerminationSubtype.SUCCESS_FULL_MACHINERY,
            decided_action_kind=ActionKind.EXECUTE_TOOL,
        ),
    )
    response = next(e for e in events if e["type"] == "reasoning.response")
    assert response["termination_subtype"] == "success_full_machinery"
    assert response["decided_action_kind"] == "execute_tool"


# ---------------------------------------------------------------------------
# Tool iteration counter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_iteration_counter_increments_explicitly():
    """The wiring layer (StepDispatcher wrapper) calls
    increment_tool_iteration() after each dispatch. The result's
    tool_iterations reflects the explicit count."""
    delivery = ProductionResponseDelivery(
        request=_request(),
        telemetry=AggregatedTelemetry(),
    )
    delivery.increment_tool_iteration()
    delivery.increment_tool_iteration()
    delivery.increment_tool_iteration()
    result = await delivery(
        _briefing(ExecuteTool(tool_id="x", arguments={})),
        _outcome(
            subtype=TerminationSubtype.SUCCESS_FULL_MACHINERY,
            decided_action_kind=ActionKind.EXECUTE_TOOL,
        ),
    )
    assert result.tool_iterations == 3


# ---------------------------------------------------------------------------
# Drain-ordering invariant — response_delivery does NOT touch trace sink
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_delivery_does_not_drain_trace_sink():
    """Drain-ordering invariant (Kit final-signoff): response_delivery
    writes/bridges into the trace sink but MUST NOT drain or clear
    it. The handler owns the drain.

    Pin: ProductionResponseDelivery has no `_trace_sink` attribute.
    The trace sink is shared via the StepDispatcher's wiring; this
    hook is purely for translation + synthetic events."""
    delivery = ProductionResponseDelivery(
        request=_request(),
        telemetry=AggregatedTelemetry(),
    )
    # Defensive: response_delivery has no trace-sink attribute. The
    # only path to the trace sink is via StepDispatcher.
    assert not hasattr(delivery, "_trace_sink")
    assert not hasattr(delivery, "trace_sink")
    # And no drain/clear methods on the delivery hook.
    assert not hasattr(delivery, "drain")
    assert not hasattr(delivery, "drain_tool_trace")


# ---------------------------------------------------------------------------
# wrap_chain_caller_with_telemetry — token aggregation decorator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_caller_wrapper_accumulates_tokens_into_shared_telemetry():
    telemetry = AggregatedTelemetry()

    async def fake_chain(system, messages, tools, max_tokens, **_):
        return ProviderResponse(
            content=[ContentBlock(type="text", text="x")],
            stop_reason="end_turn",
            input_tokens=100,
            output_tokens=50,
        )

    wrapped = wrap_chain_caller_with_telemetry(fake_chain, telemetry)

    await wrapped("sys", [{"role": "user", "content": "x"}], [], 100)
    assert telemetry.input_tokens == 100
    assert telemetry.output_tokens == 50
    assert telemetry.hook_call_count == 1

    await wrapped("sys", [{"role": "user", "content": "x"}], [], 100)
    assert telemetry.input_tokens == 200
    assert telemetry.output_tokens == 100
    assert telemetry.hook_call_count == 2


def test_wrapped_chain_signature_has_no_event_emitter():
    """No-double-count invariant: the wrapper signature does NOT
    accept an event_emitter parameter. By construction, the wrapper
    cannot emit reasoning.* events because it has no event-emission
    surface. Synthetic outer events live ONLY in
    ProductionResponseDelivery.

    This is a structural pin, not a behavioral check."""
    import inspect
    sig = inspect.signature(wrap_chain_caller_with_telemetry)
    param_names = set(sig.parameters)
    forbidden = {"event_emitter", "events", "audit_emitter", "emit"}
    overlap = param_names & forbidden
    assert overlap == set(), (
        f"wrap_chain_caller_with_telemetry must not accept event-"
        f"emission parameters; found {overlap}. The synthetic "
        f"reasoning.* emission lives in ProductionResponseDelivery only."
    )
