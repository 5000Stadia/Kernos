"""Tests for IntegrationService (PDI C3) — production façade over the
V1 IntegrationRunner.

Coverage:
  - Service delegates run() to the underlying runner; happy path
    produces a Briefing.
  - First-pass clarification scenario: model emits clarification_needed
    with partial_state=None; runner builds the variant; service returns
    the briefing.
  - Action envelope construction path: model emits execute_tool +
    well-formed action_envelope; runner produces Briefing with the
    envelope attached.
  - Envelope-required rule: execute_tool without envelope routes
    through fail-soft (already covered by runner tests; sanity check
    here at the service surface).
  - Conforms to TurnRunner's IntegrationServiceLike Protocol.
"""

from __future__ import annotations

import pytest

from kernos.kernel.integration.briefing import (
    ActionEnvelope,
    AuditTrace,
    Briefing,
    ClarificationNeeded,
    ExecuteTool,
    RespondOnly,
)
from kernos.kernel.integration.runner import (
    IntegrationConfig,
    IntegrationInputs,
)
from kernos.kernel.integration.service import (
    IntegrationService,
    build_integration_service,
)
from kernos.kernel.turn_runner import IntegrationServiceLike
from kernos.providers.base import ContentBlock, ProviderResponse


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _inputs(**overrides) -> IntegrationInputs:
    base = dict(
        user_message="hi",
        conversation_thread=({"role": "user", "content": "hi"},),
        cohort_outputs=(),
        surfaced_tools=(),
        active_context_spaces=(),
        member_id="mem-1",
        instance_id="inst-1",
        space_id="space-1",
        turn_id="turn-1",
        integration_run_id="run-1",
    )
    base.update(overrides)
    return IntegrationInputs(**base)


def _finalize_block(payload: dict) -> ContentBlock:
    return ContentBlock(
        type="tool_use",
        id="tu_finalize_1",
        name="__finalize_briefing__",
        input=payload,
    )


def _resp(*blocks: ContentBlock) -> ProviderResponse:
    return ProviderResponse(
        content=list(blocks),
        stop_reason="tool_use",
        input_tokens=10,
        output_tokens=20,
    )


def _build_service(model_payload: dict) -> tuple[IntegrationService, list]:
    """Build a service whose chain immediately finalizes with the
    given payload. Audit sink is returned so tests can assert audit
    emission."""
    audit_sink: list[dict] = []

    async def chain(system, messages, tools, max_tokens, **_):
        return _resp(_finalize_block(model_payload))

    async def dispatcher(tool_id, args, inputs):
        return {"ok": True}

    async def emit(entry: dict) -> None:
        audit_sink.append(entry)

    service = IntegrationService(
        chain_caller=chain,
        read_only_dispatcher=dispatcher,
        audit_emitter=emit,
        config=IntegrationConfig(),
    )
    return service, audit_sink


# ---------------------------------------------------------------------------
# Happy path: service delegates to runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_returns_briefing_from_simple_respond_only():
    service, sink = _build_service({
        "relevant_context": [],
        "filtered_context": [],
        "decided_action": {"kind": "respond_only"},
        "presence_directive": "answer concisely",
    })
    briefing = await service.run(_inputs())
    assert isinstance(briefing, Briefing)
    assert isinstance(briefing.decided_action, RespondOnly)
    assert briefing.action_envelope is None
    assert sink, "service must emit audit through underlying runner"


# ---------------------------------------------------------------------------
# First-pass clarification scenario (acceptance criterion #6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_supports_clarification_needed_first_pass():
    """Per spec Section 1: integration first-pass produces
    clarification_needed when critical info is missing. partial_state
    is None for first-pass (nothing has been attempted yet)."""
    service, _ = _build_service({
        "relevant_context": [],
        "filtered_context": [],
        "decided_action": {
            "kind": "clarification_needed",
            "question": "Which calendar should I check?",
            "ambiguity_type": "target",
            "partial_state": None,
        },
        "presence_directive": "ask the question naturally",
    })
    briefing = await service.run(_inputs())
    assert isinstance(briefing.decided_action, ClarificationNeeded)
    assert briefing.decided_action.question == "Which calendar should I check?"
    assert briefing.decided_action.ambiguity_type == "target"
    assert briefing.decided_action.partial_state is None
    # No envelope because clarification_needed is not an action-shape kind.
    assert briefing.action_envelope is None


@pytest.mark.asyncio
async def test_service_clarification_with_omitted_partial_state_is_first_pass():
    service, _ = _build_service({
        "relevant_context": [],
        "filtered_context": [],
        "decided_action": {
            "kind": "clarification_needed",
            "question": "What time zone?",
            "ambiguity_type": "parameter",
            # partial_state omitted entirely — equivalent to None
        },
        "presence_directive": "ask",
    })
    briefing = await service.run(_inputs())
    assert isinstance(briefing.decided_action, ClarificationNeeded)
    assert briefing.decided_action.partial_state is None


# ---------------------------------------------------------------------------
# Action envelope construction (acceptance criteria #5, #14, #26)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_attaches_action_envelope_to_execute_tool_briefing():
    """Per Kit edit: action-shape decided_actions carry an explicit
    ActionEnvelope. The integration runner constructs the briefing
    from model output that includes the envelope; the service
    surfaces it untouched."""
    service, _ = _build_service({
        "relevant_context": [],
        "filtered_context": [],
        "decided_action": {
            "kind": "execute_tool",
            "tool_id": "calendar_create_event",
            "arguments": {"title": "Friday standup", "duration": 30},
            "narration_context": "creating the requested event",
        },
        "presence_directive": "execute and narrate",
        "action_envelope": {
            "intended_outcome": "create the Friday standup event the user asked for",
            "allowed_tool_classes": ["calendar"],
            "allowed_operations": ["create"],
            "constraints": ["use the user's stated duration"],
            "forbidden_moves": ["channel_switch"],
        },
    })
    briefing = await service.run(_inputs())
    assert isinstance(briefing.decided_action, ExecuteTool)
    assert briefing.decided_action.tool_id == "calendar_create_event"
    assert briefing.action_envelope is not None
    assert briefing.action_envelope.intended_outcome.startswith("create")
    assert "calendar" in briefing.action_envelope.allowed_tool_classes
    assert "create" in briefing.action_envelope.allowed_operations
    assert "channel_switch" in briefing.action_envelope.forbidden_moves


@pytest.mark.asyncio
async def test_service_attaches_envelope_with_confirmation_requirements():
    """Confirmation-boundary case: a draft+send envelope marks send
    as requiring user confirmation. EnactmentService C5+ enforces;
    the service surfaces the envelope intact."""
    service, _ = _build_service({
        "relevant_context": [],
        "filtered_context": [],
        "decided_action": {
            "kind": "execute_tool",
            "tool_id": "email_send",
            "arguments": {"to": "x@example.com", "subject": "x", "body": "x"},
            "narration_context": "drafting and sending",
        },
        "presence_directive": "draft then send",
        "action_envelope": {
            "intended_outcome": "send confirmation email to recipient",
            "allowed_tool_classes": ["email"],
            "allowed_operations": ["draft", "send"],
            "confirmation_requirements": ["send"],
        },
    })
    briefing = await service.run(_inputs())
    assert briefing.action_envelope is not None
    assert "send" in briefing.action_envelope.confirmation_requirements


@pytest.mark.asyncio
async def test_service_execute_tool_without_envelope_routes_to_fail_soft():
    """Spec contract: execute_tool MUST carry an action_envelope. The
    runner enforces structurally; an envelope-less execute_tool from
    the model routes through fail-soft to a minimal RespondOnly
    briefing rather than a malformed ExecuteTool."""
    service, _ = _build_service({
        "relevant_context": [],
        "filtered_context": [],
        "decided_action": {
            "kind": "execute_tool",
            "tool_id": "calendar_create_event",
            "arguments": {"title": "x"},
        },
        "presence_directive": "execute",
        # action_envelope deliberately omitted.
    })
    briefing = await service.run(_inputs())
    # Fail-soft engaged: the runner returned a minimal briefing,
    # not the malformed ExecuteTool.
    assert briefing.audit_trace.fail_soft_engaged
    assert briefing.action_envelope is None


# ---------------------------------------------------------------------------
# Factory + Protocol conformance
# ---------------------------------------------------------------------------


def test_build_integration_service_factory_returns_service():
    async def chain(*_a, **_kw):
        return _resp(_finalize_block({
            "relevant_context": [],
            "filtered_context": [],
            "decided_action": {"kind": "respond_only"},
            "presence_directive": "x",
        }))

    async def dispatcher(*_a, **_kw):
        return {"ok": True}

    async def emit(entry):
        return None

    service = build_integration_service(
        chain_caller=chain,
        read_only_dispatcher=dispatcher,
        audit_emitter=emit,
    )
    assert isinstance(service, IntegrationService)


def test_service_conforms_to_integration_service_like_protocol():
    """The TurnRunner depends on the IntegrationServiceLike Protocol;
    IntegrationService must conform structurally so wiring works."""
    async def chain(*_a, **_kw):
        return _resp(_finalize_block({
            "relevant_context": [],
            "filtered_context": [],
            "decided_action": {"kind": "respond_only"},
            "presence_directive": "x",
        }))

    async def dispatcher(*_a, **_kw):
        return {"ok": True}

    async def emit(entry):
        return None

    service = IntegrationService(
        chain_caller=chain,
        read_only_dispatcher=dispatcher,
        audit_emitter=emit,
    )
    assert isinstance(service, IntegrationServiceLike)


# ---------------------------------------------------------------------------
# Service + TurnRunner integration smoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_runner_can_consume_integration_service():
    """End-to-end: TurnRunner.run_integration → IntegrationService →
    IntegrationRunner → Briefing. Verifies the C2 Protocol seam binds
    cleanly to the C3 concrete class."""
    from kernos.kernel.cohorts.descriptor import CohortFanOutResult
    from kernos.kernel.turn_runner import TurnRunner, TurnRunnerInputs

    service, _ = _build_service({
        "relevant_context": [],
        "filtered_context": [],
        "decided_action": {"kind": "respond_only"},
        "presence_directive": "answer briefly",
    })
    runner = TurnRunner(integration_service=service)

    fan_out = CohortFanOutResult(
        outputs=(),
        fan_out_started_at="2026-04-26T00:00:00+00:00",
        fan_out_completed_at="2026-04-26T00:00:01+00:00",
    )
    inputs = TurnRunnerInputs(
        instance_id="inst-1",
        member_id="mem-1",
        space_id="space-1",
        turn_id="turn-1",
        user_message="hi",
        integration_thread=({"role": "user", "content": "hi"},),
    )
    briefing = await runner.run_integration(fan_out, inputs)
    assert isinstance(briefing, Briefing)
    assert isinstance(briefing.decided_action, RespondOnly)
