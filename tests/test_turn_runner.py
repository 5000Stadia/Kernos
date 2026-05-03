"""Tests for TurnRunner skeleton + feature-flag routing (PDI C2).

Acceptance criterion focus:
  - #2: ReasoningService routes to TurnRunner when feature flag is True;
        runs existing reasoning loop unchanged when False.
  - #3: Public API of ReasoningService.process_turn() is unchanged.
  - #7 (the design review edit): TurnRunner threads required_safety_cohort_failures
        to IntegrationService at the boundary, not just runner-
        representable. Acceptance test asserts the metadata flows
        across the seam.
  - #34: Feature flag features.use_decoupled_turn_runner defaults to False.

C2 covers the plumbing only; concrete IntegrationService and
EnactmentService land in C3+. These tests use Protocol-conforming
stubs so the seam is testable now.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kernos.kernel.cohorts.descriptor import (
    CohortFanOutResult,
    ContextSpaceRef,
    Turn,
)
from kernos.kernel.integration.briefing import (
    AuditTrace,
    Briefing,
    RespondOnly,
)
from kernos.kernel.integration.runner import IntegrationInputs
from kernos.kernel.turn_runner import (
    EnactmentServiceLike,
    FEATURE_FLAG_ENV,
    IntegrationServiceLike,
    TurnRunner,
    TurnRunnerInputs,
    TurnRunnerNotWired,
    use_decoupled_turn_runner,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _briefing() -> Briefing:
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=RespondOnly(),
        presence_directive="answer briefly",
        audit_trace=AuditTrace(),
    )


def _fan_out_result(
    *,
    required_safety_cohort_failures: tuple[str, ...] = (),
) -> CohortFanOutResult:
    return CohortFanOutResult(
        outputs=(),
        fan_out_started_at="2026-04-26T00:00:00+00:00",
        fan_out_completed_at="2026-04-26T00:00:01+00:00",
        required_cohort_failures=(),
        required_safety_cohort_failures=required_safety_cohort_failures,
    )


class _StubIntegrationService:
    """Captures the IntegrationInputs it's invoked with, for the seam
    assertion. Returns a minimal RespondOnly briefing."""

    def __init__(self) -> None:
        self.received_inputs: IntegrationInputs | None = None

    async def run(self, inputs: IntegrationInputs) -> Briefing:
        self.received_inputs = inputs
        return _briefing()


class _StubEnactmentService:
    def __init__(self, return_value: Any = "ok") -> None:
        self._return_value = return_value
        self.received_briefing: Briefing | None = None

    async def run(self, briefing: Briefing) -> Any:
        self.received_briefing = briefing
        return self._return_value


def _inputs(turn_id: str = "turn-x") -> TurnRunnerInputs:
    return TurnRunnerInputs(
        instance_id="inst-1",
        member_id="mem-1",
        space_id="space-1",
        turn_id=turn_id,
        user_message="hello",
        cohort_thread=(),
        cohort_active_spaces=(ContextSpaceRef(space_id="space-1"),),
        integration_thread=({"role": "user", "content": "hello"},),
        integration_active_spaces=({"space_id": "space-1"},),
    )


# ---------------------------------------------------------------------------
# Feature flag (acceptance criterion #34)
# ---------------------------------------------------------------------------


def test_feature_flag_default_on(monkeypatch):
    """With the env unset, thin path is the default (CCV1 C7 default flip 2026-05-03)."""
    monkeypatch.delenv(FEATURE_FLAG_ENV, raising=False)
    assert use_decoupled_turn_runner() is True


@pytest.mark.parametrize("value", ["", "1", "true", "TRUE", "yes", "on", "maybe", "anything"])
def test_feature_flag_on_for_non_optout_values(monkeypatch, value):
    """Any value outside the legacy opt-out set keeps thin-path default."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, value)
    assert use_decoupled_turn_runner() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "off", "no"])
def test_feature_flag_off_only_for_explicit_optout(monkeypatch, value):
    """Only explicit legacy opt-out values flip the flag off."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, value)
    assert use_decoupled_turn_runner() is False


# ---------------------------------------------------------------------------
# Skeleton wiring errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_runner_without_cohort_runner_raises_clear_error():
    runner = TurnRunner()
    with pytest.raises(TurnRunnerNotWired, match="cohort_runner"):
        await runner.run_cohort_fan_out(_inputs())


@pytest.mark.asyncio
async def test_turn_runner_without_integration_service_raises_clear_error():
    runner = TurnRunner()
    with pytest.raises(TurnRunnerNotWired, match="integration_service"):
        await runner.run_integration(_fan_out_result(), _inputs())


@pytest.mark.asyncio
async def test_turn_runner_without_enactment_service_raises_clear_error():
    runner = TurnRunner()
    with pytest.raises(TurnRunnerNotWired, match="enactment_service"):
        await runner.run_enactment(_briefing())


# ---------------------------------------------------------------------------
# Safety-degraded plumbing seam (the design review edit — acceptance criterion #7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safety_failures_thread_from_fan_out_to_integration():
    """The load-bearing seam: required_safety_cohort_failures flows
    TurnRunner → IntegrationService boundary, not just representable
    on the runner. Verifies the captured IntegrationInputs carries
    the failures exactly as fan-out reported them."""
    integration = _StubIntegrationService()
    runner = TurnRunner(integration_service=integration)

    fan_out = _fan_out_result(
        required_safety_cohort_failures=("covenant", "consent")
    )
    await runner.run_integration(fan_out, _inputs())

    assert integration.received_inputs is not None
    assert integration.received_inputs.required_safety_cohort_failures == (
        "covenant",
        "consent",
    )


@pytest.mark.asyncio
async def test_no_safety_failures_threads_empty_tuple():
    integration = _StubIntegrationService()
    runner = TurnRunner(integration_service=integration)

    fan_out = _fan_out_result(required_safety_cohort_failures=())
    await runner.run_integration(fan_out, _inputs())

    assert integration.received_inputs is not None
    assert integration.received_inputs.required_safety_cohort_failures == ()


@pytest.mark.asyncio
async def test_integration_inputs_carry_thread_and_user_message():
    integration = _StubIntegrationService()
    runner = TurnRunner(integration_service=integration)

    inputs = _inputs(turn_id="turn-thread-test")
    await runner.run_integration(_fan_out_result(), inputs)

    received = integration.received_inputs
    assert received is not None
    assert received.user_message == "hello"
    assert received.turn_id == "turn-thread-test"
    assert received.member_id == "mem-1"
    assert received.instance_id == "inst-1"
    assert received.space_id == "space-1"


# ---------------------------------------------------------------------------
# Composability — internal seams are independently testable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_enactment_passes_briefing_through():
    enactment = _StubEnactmentService(return_value="enacted")
    runner = TurnRunner(enactment_service=enactment)

    briefing = _briefing()
    result = await runner.run_enactment(briefing)

    assert result == "enacted"
    assert enactment.received_briefing is briefing


@pytest.mark.asyncio
async def test_deliver_returns_outcome_when_no_hook():
    runner = TurnRunner()
    result = await runner.deliver(_briefing(), "outcome-payload")
    assert result == "outcome-payload"


@pytest.mark.asyncio
async def test_deliver_uses_hook_when_provided():
    captured: dict[str, Any] = {}

    async def _hook(briefing: Briefing, outcome: Any) -> str:
        captured["briefing"] = briefing
        captured["outcome"] = outcome
        return "delivered:" + str(outcome)

    runner = TurnRunner(response_delivery=_hook)
    briefing = _briefing()
    result = await runner.deliver(briefing, "raw")

    assert result == "delivered:raw"
    assert captured["briefing"] is briefing
    assert captured["outcome"] == "raw"


# ---------------------------------------------------------------------------
# End-to-end skeleton — fan-out + integration + enactment + delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_run_turn_orchestrates_all_seams():
    integration = _StubIntegrationService()
    enactment = _StubEnactmentService(return_value="enacted")

    fake_fan_out = _fan_out_result(required_safety_cohort_failures=("covenant",))

    class _StubCohortRunner:
        def __init__(self) -> None:
            self.received_ctx = None

        async def run(self, ctx):
            self.received_ctx = ctx
            return fake_fan_out

    cohort_runner = _StubCohortRunner()

    async def _hook(briefing: Briefing, outcome: Any) -> dict:
        return {"briefing": briefing, "outcome": outcome}

    runner = TurnRunner(
        cohort_runner=cohort_runner,
        integration_service=integration,
        enactment_service=enactment,
        response_delivery=_hook,
    )

    result = await runner.run_turn(_inputs())

    # Cohort fan-out received the right context.
    assert cohort_runner.received_ctx is not None
    assert cohort_runner.received_ctx.user_message == "hello"
    # Integration received the safety-failure metadata at the boundary.
    assert integration.received_inputs is not None
    assert integration.received_inputs.required_safety_cohort_failures == (
        "covenant",
    )
    # Enactment received the briefing integration produced.
    assert enactment.received_briefing is not None
    # Delivery hook saw both briefing and outcome.
    assert result["outcome"] == "enacted"


# ---------------------------------------------------------------------------
# TurnRunnerInputs.from_api_messages convenience
# ---------------------------------------------------------------------------


def test_from_api_messages_builds_cohort_turns_from_string_content():
    inputs = TurnRunnerInputs.from_api_messages(
        instance_id="i",
        member_id="m",
        space_id="s",
        turn_id="t",
        user_message="hi",
        api_messages=(
            {"role": "user", "content": "earlier message"},
            {"role": "assistant", "content": "earlier reply"},
            {"role": "user", "content": [{"type": "text", "text": "block"}]},
        ),
        active_space_ids=("s",),
    )
    # String-content messages become Turn entries; multi-block content
    # is skipped (cohorts read thread for context, not raw block playback).
    assert len(inputs.cohort_thread) == 2
    assert all(isinstance(t, Turn) for t in inputs.cohort_thread)
    # Active spaces produced both shapes.
    assert inputs.cohort_active_spaces == (ContextSpaceRef(space_id="s"),)
    assert inputs.integration_active_spaces == ({"space_id": "s"},)
    # Integration thread keeps the original API-message dicts including
    # the multi-block one.
    assert len(inputs.integration_thread) == 3


# ---------------------------------------------------------------------------
# Protocol-shape sanity
# ---------------------------------------------------------------------------


def test_stub_integration_service_conforms_to_protocol():
    assert isinstance(_StubIntegrationService(), IntegrationServiceLike)


def test_stub_enactment_service_conforms_to_protocol():
    assert isinstance(_StubEnactmentService(), EnactmentServiceLike)


# ---------------------------------------------------------------------------
# ReasoningService façade routing (acceptance criteria #2 / #3)
# ---------------------------------------------------------------------------


def _reasoning_request():
    from kernos.kernel.reasoning import ReasoningRequest
    return ReasoningRequest(
        instance_id="inst-1",
        conversation_id="conv-1",
        system_prompt="You are Kernos.",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        model="claude-sonnet-4-6",
        trigger="user_message",
        member_id="mem-1",
        active_space_id="space-1",
        input_text="hi",
    )


@pytest.mark.asyncio
async def test_reasoning_service_routes_to_turn_runner_when_flag_on(monkeypatch):
    from kernos.kernel.reasoning import ReasoningResult, ReasoningService
    from kernos.providers.base import Provider

    monkeypatch.setenv(FEATURE_FLAG_ENV, "1")

    class _RecordingTurnRunner:
        def __init__(self) -> None:
            self.received_inputs: TurnRunnerInputs | None = None

        async def run_turn(self, inputs: TurnRunnerInputs):
            self.received_inputs = inputs
            return ReasoningResult(
                text="from-turn-runner",
                model="claude-sonnet-4-6",
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0.0,
                duration_ms=0,
                tool_iterations=0,
            )

    turn_runner = _RecordingTurnRunner()
    service = ReasoningService(
        provider=AsyncMock(spec=Provider),
        turn_runner=turn_runner,
    )
    result = await service.reason(_reasoning_request())

    assert isinstance(result, ReasoningResult)
    assert result.text == "from-turn-runner"
    # The reasoning request was translated into TurnRunnerInputs and
    # handed to the wired runner.
    assert turn_runner.received_inputs is not None
    assert turn_runner.received_inputs.instance_id == "inst-1"
    assert turn_runner.received_inputs.member_id == "mem-1"
    assert turn_runner.received_inputs.user_message == "hi"


@pytest.mark.asyncio
async def test_reasoning_service_skips_turn_runner_when_flag_off(monkeypatch):
    """Flag off → legacy reasoning path runs. We assert routing by
    confirming the wired TurnRunner is NOT consulted; we don't run
    the full legacy loop here (that's covered in test_reasoning.py).

    Post-CCV1-C7-flip (2026-05-03): unset = thin path. Explicit
    opt-out to legacy is now `setenv("0")` rather than `delenv`."""
    from kernos.kernel.reasoning import ReasoningService
    from kernos.providers.base import Provider

    monkeypatch.setenv(FEATURE_FLAG_ENV, "0")

    class _UnreachableTurnRunner:
        async def run_turn(self, inputs):
            raise AssertionError(
                "TurnRunner should not be invoked when flag is OFF"
            )

    # The TurnRunner is wired but the flag is OFF — service must not
    # route to it. We don't run the legacy loop end-to-end (no live
    # provider); instead assert the routing decision via a dummy
    # check: calling .reason() with a stub provider that errors lets
    # us confirm the legacy path was entered, not the TurnRunner.
    service = ReasoningService(
        provider=AsyncMock(spec=Provider),
        turn_runner=_UnreachableTurnRunner(),
    )

    # We don't care about a successful end-to-end legacy run here —
    # just that the TurnRunner is not consulted. The legacy path
    # will run with a mock provider; if it errors, that's fine; the
    # test only fails if _UnreachableTurnRunner.run_turn was called.
    try:
        await service.reason(_reasoning_request())
    except AssertionError:
        raise  # the unreachable runner was invoked
    except Exception:
        pass  # legacy-path provider errors are expected here


@pytest.mark.asyncio
async def test_reasoning_service_raises_when_flag_on_without_turn_runner(
    monkeypatch,
):
    """Flag on + no TurnRunner wired → clear error rather than a
    half-formed turn."""
    from kernos.kernel.reasoning import ReasoningService
    from kernos.providers.base import Provider

    monkeypatch.setenv(FEATURE_FLAG_ENV, "1")
    service = ReasoningService(provider=AsyncMock(spec=Provider))

    with pytest.raises(TurnRunnerNotWired, match="no TurnRunner was provided"):
        await service.reason(_reasoning_request())
