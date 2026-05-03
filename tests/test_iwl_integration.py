"""End-to-end integration smoke tests (IWL C5).

Wires all four concrete hooks (Planner / StepDispatcher /
DivergenceReasoner / PresenceRenderer) into the EnactmentService
and verifies turns flow through cleanly with stub chains that
produce canned model outputs.

This bridges the unit-level pins for each hook with full-stack
behavior expected from the production wiring:

  - Hooks compose correctly through EnactmentService.
  - Trace sink is shared with ReasoningService for drain compat.
  - PDI invariants continue to hold post-IWL:
      * Streaming-disabled-by-construction (return-type pin).
      * No same-turn integration re-entry (EnactmentService
        constructor pin).
      * Vocabulary locked at 7 SignalKinds.
      * References-not-dumps (audit categories).
  - Equivalence telemetry: turn_completed_via metadata visible.
  - Cost-tracking aggregates once per turn.
"""

from __future__ import annotations

import inspect

import pytest

from kernos.kernel.enactment import (
    DivergenceReasoner,
    EnactmentService,
    PresenceRenderer,
    Planner,
    SignalKind,
    StaticToolCatalog,
    StepDispatcher,
    ToolCatalogEntry,
    ToolExecutionResult,
)
from kernos.kernel.enactment.dispatcher import ToolExecutor, ToolExecutionInputs
from kernos.kernel.enactment.divergence_reasoner import (
    JUDGE_DIVERGENCE_TOOL_NAME,
    MODIFIED_STEP_TOOL_NAME,
    PIVOT_STEP_TOOL_NAME,
    CLARIFICATION_TOOL_NAME,
)
from kernos.kernel.enactment.planner import PLAN_FINALIZE_TOOL_NAME
from kernos.kernel.enactment.service import (
    PresenceRenderResult,
    TerminationSubtype,
)
from kernos.kernel.enactment.tiers import FailureKind
from kernos.kernel.integration.briefing import (
    ActionEnvelope,
    AuditTrace,
    Briefing,
    ExecuteTool,
    RespondOnly,
)
from kernos.kernel.reasoning import ReasoningRequest, ReasoningResult
from kernos.kernel.response_delivery import (
    AggregatedTelemetry,
    ProductionResponseDelivery,
    wrap_chain_caller_with_telemetry,
)
from kernos.kernel.tool_descriptor import (
    GateClassification,
    OperationClassification,
    ToolDescriptor,
)
from kernos.providers.base import ContentBlock, ProviderResponse


# ---------------------------------------------------------------------------
# Fixtures: chain caller stubs per hook
# ---------------------------------------------------------------------------


def _resp_text(text: str) -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="text", text=text)],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=20,
    )


def _resp_tool_use(name: str, payload: dict) -> ProviderResponse:
    return ProviderResponse(
        content=[
            ContentBlock(type="tool_use", id="tu_1", name=name, input=payload)
        ],
        stop_reason="tool_use",
        input_tokens=10,
        output_tokens=20,
    )


def _make_planner_chain(steps_payload: dict):
    async def chain(system, messages, tools, max_tokens, **_):
        return _resp_tool_use(PLAN_FINALIZE_TOOL_NAME, steps_payload)
    return chain


def _make_reasoner_chain(payloads: dict):
    """payloads maps tool_name → response payload."""
    async def chain(system, messages, tools, max_tokens, **_):
        tool_name = tools[0]["name"] if tools else ""
        return _resp_tool_use(tool_name, payloads.get(tool_name, {}))
    return chain


def _make_renderer_chain(text: str):
    async def chain(system, messages, tools, max_tokens, **_):
        return _resp_text(text)
    return chain


# ---------------------------------------------------------------------------
# Stub tool executor + descriptor
# ---------------------------------------------------------------------------


class _FakeExecutor:
    """Records calls and returns canned results."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    async def execute(self, inputs: ToolExecutionInputs) -> ToolExecutionResult:
        self.calls.append(inputs)
        return self._results.pop(0)


class _StubLookup:
    def __init__(self, descriptors):
        self._descriptors = descriptors

    def descriptor_for(self, tool_id: str):
        return self._descriptors.get(tool_id)


# ---------------------------------------------------------------------------
# Briefing builders
# ---------------------------------------------------------------------------


def _execute_briefing() -> Briefing:
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=ExecuteTool(tool_id="email_send", arguments={}),
        presence_directive="execute",
        audit_trace=AuditTrace(),
        turn_id="turn-iwl",
        integration_run_id="run-iwl",
        action_envelope=ActionEnvelope(
            intended_outcome="send the email",
            allowed_tool_classes=("email",),
            allowed_operations=("send",),
        ),
    )


def _respond_only_briefing() -> Briefing:
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=RespondOnly(),
        presence_directive="answer the question",
        audit_trace=AuditTrace(),
        turn_id="turn-iwl",
        integration_run_id="run-iwl",
    )


def _request() -> ReasoningRequest:
    return ReasoningRequest(
        instance_id="inst-iwl",
        conversation_id="conv-iwl",
        system_prompt="x",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        model="claude-sonnet-4-6",
        trigger="user_message",
        member_id="mem-iwl",
        active_space_id="space-iwl",
        input_text="hi",
    )


# ---------------------------------------------------------------------------
# End-to-end: thin path turn through wired EnactmentService
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thin_path_turn_through_full_wiring():
    """RespondOnly briefing → thin path → PresenceRenderer renders text.
    No StepDispatcher, no Planner, no DivergenceReasoner consulted."""
    presence = PresenceRenderer(
        chain_caller=_make_renderer_chain("green grass is healthy"),
    )
    service = EnactmentService(presence_renderer=presence)
    outcome = await service.run(_respond_only_briefing())
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH
    assert outcome.text == "green grass is healthy"


@pytest.mark.asyncio
async def test_full_machinery_turn_through_full_wiring():
    """ExecuteTool briefing → full machinery → all four hooks engage:
      Planner produces 1-step plan → StepDispatcher dispatches via
      executor → DivergenceReasoner short-circuits on clean success →
      PresenceRenderer renders terminal text → SUCCESS_FULL_MACHINERY."""
    plan_payload = {
        "steps": [
            {
                "tool_id": "email_send",
                "tool_class": "email",
                "operation_name": "send",
                "arguments": {"to": "x@example.com"},
                "expectation": {
                    "prose": "email sent",
                    "structured": [
                        {"kind": SignalKind.SUCCESS_STATUS.value, "args": {}},
                    ],
                },
            }
        ]
    }
    planner = Planner(
        chain_caller=_make_planner_chain(plan_payload),
        tool_catalog=StaticToolCatalog(entries=(
            ToolCatalogEntry(
                tool_id="email_send",
                tool_class="email",
                operation_name="send",
            ),
        )),
    )
    descriptor = ToolDescriptor(
        name="email_send",
        description="d",
        input_schema={"type": "object"},
        implementation="x.py",
        operations=(
            OperationClassification(
                operation="send",
                classification=GateClassification.HARD_WRITE,
            ),
        ),
    )
    dispatcher = StepDispatcher(
        executor=_FakeExecutor([
            ToolExecutionResult(output={"ok": True, "id": "msg-1"}),
        ]),
        descriptor_lookup=_StubLookup({"email_send": descriptor}),
    )
    # Reasoner: judge_divergence will short-circuit (structured pass +
    # completed); other methods unused on happy path.
    reasoner = DivergenceReasoner(
        chain_caller=_make_reasoner_chain({}),
    )
    presence = PresenceRenderer(
        chain_caller=_make_renderer_chain("email sent confirming the meeting"),
    )

    service = EnactmentService(
        presence_renderer=presence,
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
    )
    outcome = await service.run(_execute_briefing())
    assert outcome.subtype is TerminationSubtype.SUCCESS_FULL_MACHINERY
    assert outcome.text == "email sent confirming the meeting"


@pytest.mark.asyncio
async def test_full_machinery_turn_with_translation_to_reasoning_result():
    """End-to-end with response_delivery: outcome → ReasoningResult.

    Pin: aggregated tokens reflect all hook calls; tool_iterations
    matches dispatch count; ReasoningResult shape is the seven
    shipped fields exactly."""
    plan_payload = {
        "steps": [{
            "tool_id": "email_send",
            "tool_class": "email",
            "operation_name": "send",
            "arguments": {},
            "expectation": {"prose": "x"},
        }],
    }
    descriptor = ToolDescriptor(
        name="email_send",
        description="d",
        input_schema={"type": "object"},
        implementation="x.py",
        operations=(
            OperationClassification(
                operation="send",
                classification=GateClassification.HARD_WRITE,
            ),
        ),
    )
    telemetry = AggregatedTelemetry()

    planner_chain = wrap_chain_caller_with_telemetry(
        _make_planner_chain(plan_payload), telemetry,
    )
    reasoner_chain = wrap_chain_caller_with_telemetry(
        _make_reasoner_chain({}), telemetry,
    )
    presence_chain = wrap_chain_caller_with_telemetry(
        _make_renderer_chain("done"), telemetry,
    )

    planner = Planner(
        chain_caller=planner_chain,
        tool_catalog=StaticToolCatalog(),
    )
    dispatcher = StepDispatcher(
        executor=_FakeExecutor([
            ToolExecutionResult(output={"ok": True}),
        ]),
        descriptor_lookup=_StubLookup({"email_send": descriptor}),
    )
    reasoner = DivergenceReasoner(chain_caller=reasoner_chain)
    presence = PresenceRenderer(chain_caller=presence_chain)

    service = EnactmentService(
        presence_renderer=presence,
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
    )

    delivery = ProductionResponseDelivery(
        request=_request(),
        telemetry=telemetry,
    )
    delivery.increment_tool_iteration()  # one dispatch
    outcome = await service.run(_execute_briefing())
    result = await delivery(_execute_briefing(), outcome)

    # ReasoningResult shape exactly the seven shipped fields.
    from dataclasses import fields
    assert {f.name for f in fields(ReasoningResult)} == {
        "text", "model", "input_tokens", "output_tokens",
        "estimated_cost_usd", "duration_ms", "tool_iterations",
    }
    # text from outcome.
    assert result.text == "done"
    # model from request.
    assert result.model == "claude-sonnet-4-6"
    # tokens aggregated across the planner + presence chains
    # (reasoner short-circuited; no model call).
    # 2 model calls × 10 input tokens = 20.
    assert result.input_tokens == 20
    assert result.output_tokens == 40
    # tool_iterations from explicit increment.
    assert result.tool_iterations == 1


# ---------------------------------------------------------------------------
# PDI invariants — IWL preserves all 11
# ---------------------------------------------------------------------------


def test_pdi_invariant_streaming_disabled_protocol_returns():
    """PDI invariant: hook return types have no `streamed` field."""
    from dataclasses import fields
    from kernos.kernel.enactment.service import (
        DivergenceJudgment,
        PlanCreationResult,
        StepDispatchResult,
    )
    for cls in (DivergenceJudgment, PlanCreationResult, StepDispatchResult):
        names = {f.name for f in fields(cls)}
        assert "streamed" not in names, (
            f"{cls.__name__} must not expose streamed field"
        )


def test_pdi_invariant_no_same_turn_integration_re_entry():
    """PDI invariant: EnactmentService.__init__ has no integration
    parameter."""
    sig = inspect.signature(EnactmentService.__init__)
    for param in sig.parameters:
        assert "integration" not in param.lower()


def test_pdi_invariant_vocabulary_locked_at_seven_signal_kinds():
    """PDI invariant: exactly 7 SignalKinds. IWL hooks must list the
    same seven."""
    assert len(set(SignalKind)) == 7


def test_pdi_invariant_b1_b2_render_inputs_redacted():
    """PDI invariant carried via IWL C4: B1RenderInputs and
    B2RenderInputs structurally exclude discovered_information."""
    from dataclasses import fields
    from kernos.kernel.enactment import B1RenderInputs, B2RenderInputs
    for cls in (B1RenderInputs, B2RenderInputs):
        names = {f.name for f in fields(cls)}
        assert "discovered_information" not in names


def test_iwl_no_double_count_chain_wrapper_has_no_event_emitter():
    """IWL invariant: chain wrapper has no event-emission parameters.
    Synthetic reasoning.* events live ONLY in
    ProductionResponseDelivery."""
    sig = inspect.signature(wrap_chain_caller_with_telemetry)
    for param in sig.parameters:
        assert "event" not in param.lower()
        assert "emit" not in param.lower()


def test_iwl_drain_ordering_response_delivery_has_no_drain_method():
    """IWL invariant: response_delivery does not drain or clear the
    trace sink. Handler owns drain."""
    delivery = ProductionResponseDelivery(
        request=_request(),
        telemetry=AggregatedTelemetry(),
    )
    assert not hasattr(delivery, "_trace_sink")
    assert not hasattr(delivery, "drain")
    assert not hasattr(delivery, "drain_tool_trace")


def test_iwl_reasoning_result_shape_unchanged():
    """IWL acceptance criterion: public ReasoningResult shape NOT
    widened. Exactly seven shipped fields."""
    from dataclasses import fields
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


# ---------------------------------------------------------------------------
# Trace sink shared across paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_sink_shared_with_reasoning_service_via_iwl_wiring():
    """When ReasoningService is constructed with the trace_sink
    parameter, the StepDispatcher's writes flow through to
    drain_tool_trace()."""
    from unittest.mock import AsyncMock
    from kernos.kernel.reasoning import ReasoningService
    from kernos.providers.base import Provider

    sink: list[dict] = []
    service = ReasoningService(provider=AsyncMock(spec=Provider), trace_sink=sink)

    descriptor = ToolDescriptor(
        name="email_send",
        description="d",
        input_schema={"type": "object"},
        implementation="x.py",
        operations=(
            OperationClassification(
                operation="send",
                classification=GateClassification.HARD_WRITE,
            ),
        ),
    )
    dispatcher = StepDispatcher(
        executor=_FakeExecutor([
            ToolExecutionResult(output={"ok": True}),
        ]),
        descriptor_lookup=_StubLookup({"email_send": descriptor}),
        trace_sink=sink,
    )

    from kernos.kernel.enactment.plan import Step, StepExpectation
    from kernos.kernel.enactment.service import StepDispatchInputs

    step = Step(
        step_id="s1",
        tool_id="email_send",
        arguments={},
        tool_class="email",
        operation_name="send",
        expectation=StepExpectation(prose="x"),
    )
    await dispatcher.dispatch(
        StepDispatchInputs(step=step, briefing=_execute_briefing())
    )

    drained = service.drain_tool_trace()
    assert len(drained) == 1
    assert drained[0]["name"] == "email_send"
    # Drain cleared the shared list.
    assert sink == []


# ---------------------------------------------------------------------------
# Equivalence telemetry visible in synthetic event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_equivalence_telemetry_records_turn_completed_via_decoupled():
    events = []

    async def emit(payload):
        events.append(payload)

    delivery = ProductionResponseDelivery(
        request=_request(),
        telemetry=AggregatedTelemetry(),
        event_emitter=emit,
    )
    await delivery.emit_request_event()

    presence = PresenceRenderer(
        chain_caller=_make_renderer_chain("hi"),
    )
    service = EnactmentService(presence_renderer=presence)
    outcome = await service.run(_respond_only_briefing())

    await delivery(_respond_only_briefing(), outcome)

    response = next(e for e in events if e["type"] == "reasoning.response")
    assert response["turn_completed_via"] == "decoupled"
    assert response["trigger"] == "turn_runner"
    # exactly one reasoning.response per turn
    response_events = [e for e in events if e["type"] == "reasoning.response"]
    assert len(response_events) == 1
