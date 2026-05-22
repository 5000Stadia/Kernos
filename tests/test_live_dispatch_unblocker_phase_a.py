"""LIVE-DISPATCH-UNBLOCKER-V1 Phase A acceptance tests.

Pins ACs 1-5: gate.evaluate() fires on every live-path tool
call (both seams), gate-refusal returns natural-prose error,
ToolExecutionInputs carries the new fields.

Phases B (amortization), C (binding diagnostics), D (catalog
metadata reads) have their own test modules when shipped.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.enactment.dispatcher import (
    ToolExecutionInputs,
    ToolExecutionResult,
)
from kernos.kernel.gate import GateResult
from kernos.kernel.integration.live_wiring import (
    LiveExecutor,
    LiveIntegrationDispatcher,
    _gate_refusal_prose,
)


def _inputs(**overrides) -> ToolExecutionInputs:
    defaults = dict(
        tool_id="list-events", arguments={}, operation_name="list-events",
        instance_id="inst-x", member_id="mem-x", space_id="space-x",
        turn_id="turn-x",
    )
    defaults.update(overrides)
    return ToolExecutionInputs(**defaults)


def _gate(
    classification: str = "read", *, allowed: bool = True,
    reason: str = "approved", proposed_action: str = "",
    conflicting_rule: str = "",
) -> MagicMock:
    g = MagicMock()
    g.classify_tool_effect.return_value = classification
    g.evaluate = AsyncMock(return_value=GateResult(
        allowed=allowed, reason=reason, method="model_check",
        proposed_action=proposed_action,
        conflicting_rule=conflicting_rule,
    ))
    return g


# ============================================================
# AC1 — LiveExecutor calls gate.evaluate for every classified call
# ============================================================


@pytest.mark.asyncio
async def test_ac1_executor_calls_evaluate():
    gate = _gate("read")
    execute_tool = AsyncMock(return_value="ok")
    executor = LiveExecutor(
        execute_tool=execute_tool, gate=gate,
        request_factory=lambda inputs: MagicMock(),
    )
    await executor.execute(_inputs())
    gate.evaluate.assert_called_once()


# ============================================================
# AC2 — LiveIntegrationDispatcher calls gate.evaluate
# ============================================================


@pytest.mark.asyncio
async def test_ac2_dispatcher_calls_evaluate():
    gate = _gate("read")
    execute_tool = AsyncMock(return_value="ok")
    dispatcher = LiveIntegrationDispatcher(
        execute_tool=execute_tool, gate=gate,
        request_factory=lambda *args, **kw: MagicMock(),
    )
    await dispatcher("list-events", {}, _inputs())
    gate.evaluate.assert_called_once()


# ============================================================
# AC3 — gate refusal blocks dispatch + surfaces prose
# ============================================================


@pytest.mark.asyncio
async def test_ac3_executor_refuses_on_gate_blocked():
    gate = _gate(
        "soft_write", allowed=False, reason="confirm",
        proposed_action="Send email to mom",
    )
    execute_tool = AsyncMock()
    executor = LiveExecutor(
        execute_tool=execute_tool, gate=gate,
        request_factory=lambda inputs: MagicMock(),
    )
    result = await executor.execute(_inputs(tool_id="send-email"))
    assert result.is_error is True
    assert "{" not in result.output["error"]  # no JSON
    assert "confirm" in result.output["error"].lower()
    execute_tool.assert_not_called()


@pytest.mark.asyncio
async def test_ac3_dispatcher_refuses_on_gate_blocked():
    gate = _gate(
        "soft_write", allowed=False, reason="confirm",
        proposed_action="x",
    )
    execute_tool = AsyncMock()
    dispatcher = LiveIntegrationDispatcher(
        execute_tool=execute_tool, gate=gate,
        request_factory=lambda *args, **kw: MagicMock(),
    )
    result = await dispatcher("send-email", {}, _inputs())
    assert result["is_error"] is True
    assert "{" not in result["error"]
    execute_tool.assert_not_called()


# ============================================================
# AC4 — when evaluate allowed, dispatch proceeds
# ============================================================


@pytest.mark.asyncio
async def test_ac4_executor_proceeds_when_allowed():
    gate = _gate("read", allowed=True)
    execute_tool = AsyncMock(return_value="data")
    executor = LiveExecutor(
        execute_tool=execute_tool, gate=gate,
        request_factory=lambda inputs: MagicMock(),
    )
    result = await executor.execute(_inputs())
    assert result.is_error is False
    assert result.output == {"text": "data"}
    execute_tool.assert_called_once()


# ============================================================
# AC5 — ToolExecutionInputs carries new fields
# ============================================================


def test_ac5_inputs_carries_gate_context_fields():
    """All 5 new fields have backwards-compatible defaults."""
    inputs = _inputs()  # default construction
    assert inputs.agent_reasoning == ""
    assert inputs.is_reactive is True
    assert inputs.approval_token_id == ""
    assert inputs.user_message == ""
    assert inputs.recent_messages == ()


def test_inputs_accepts_explicit_gate_context():
    inputs = _inputs(
        agent_reasoning="reasoning text",
        is_reactive=False,
        approval_token_id="tok_123",
        user_message="send a thank-you note",
        recent_messages=({"role": "user", "content": "hi"},),
    )
    assert inputs.agent_reasoning == "reasoning text"
    assert inputs.is_reactive is False
    assert inputs.approval_token_id == "tok_123"
    assert inputs.user_message == "send a thank-you note"
    assert len(inputs.recent_messages) == 1


@pytest.mark.asyncio
async def test_executor_threads_inputs_into_evaluate():
    """Verify the gate.evaluate call sees the inputs' context fields."""
    gate = _gate("read")
    execute_tool = AsyncMock(return_value="ok")
    executor = LiveExecutor(
        execute_tool=execute_tool, gate=gate,
        request_factory=lambda inputs: MagicMock(),
    )
    await executor.execute(_inputs(
        agent_reasoning="I want to check the calendar",
        user_message="what's on my schedule?",
        is_reactive=True,
    ))
    call_kwargs = gate.evaluate.call_args.kwargs
    assert call_kwargs["agent_reasoning"] == "I want to check the calendar"
    assert call_kwargs["user_message"] == "what's on my schedule?"
    assert call_kwargs["is_reactive"] is True


# ============================================================
# Natural-prose refusal composer
# ============================================================


class TestGateRefusalProse:
    def test_covenant_conflict_inlines_rule(self):
        result = GateResult(
            allowed=False, reason="covenant_conflict",
            method="model_check",
            conflicting_rule="Never send to third parties without approval",
        )
        prose = _gate_refusal_prose(result)
        assert "Never send to third parties" in prose
        assert "{" not in prose  # no JSON
        assert "covenant_conflict" not in prose  # no enum leak

    def test_clarify_mentions_ambiguity(self):
        result = GateResult(
            allowed=False, reason="clarify", method="model_check",
            proposed_action="schedule a meeting",
        )
        prose = _gate_refusal_prose(result)
        assert "ambiguous" in prose.lower()
        assert "clarify" not in prose.lower() or "clarify" in prose.lower()
        # Either way the agent gets a natural sentence
        assert "schedule a meeting" in prose

    def test_confirm_asks_for_user_confirmation(self):
        result = GateResult(
            allowed=False, reason="confirm", method="model_check",
            proposed_action="delete the file",
        )
        prose = _gate_refusal_prose(result)
        assert "confirm" in prose.lower()
        assert "delete the file" in prose

    def test_refused_by_mode_explains_posture(self):
        result = GateResult(
            allowed=False, reason="refused_by_mode",
            method="mode_strict",
        )
        prose = _gate_refusal_prose(result)
        assert "strict" in prose.lower() or "posture" in prose.lower()
        assert "refused_by_mode" not in prose

    def test_unknown_reason_falls_back_to_generic(self):
        result = GateResult(
            allowed=False, reason="something_new",
            method="something",
        )
        prose = _gate_refusal_prose(result)
        # Generic fallback prose, still natural
        assert "{" not in prose
        assert "blocked" in prose.lower()
