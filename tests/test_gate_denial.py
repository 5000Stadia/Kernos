"""Tests for gate denial tracking (SPEC-IQ-4)."""
import pytest
from unittest.mock import MagicMock, AsyncMock
from kernos.kernel.gate import DispatchGate, GateResult
from kernos.kernel.state import InstanceProfile


def _make_gate():
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value="CONFIRM")
    registry = MagicMock()
    registry.get_all.return_value = []
    state = AsyncMock()
    state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
        instance_id="t1", status="active", created_at="2026-01-01"))
    state.query_covenant_rules = AsyncMock(return_value=[])
    events = MagicMock()
    gate = DispatchGate(reasoning, registry, state, events)
    return gate


class TestDenialCounting:
    async def test_denial_after_threshold(self):
        gate = _make_gate()
        # Simulate 3 consecutive blocks
        for _ in range(3):
            result = await gate.evaluate(
                "create-event", {}, "hard_write", "", "t1", "s1",
                is_reactive=False,
            )
            assert not result.allowed

        # 4th attempt should hit denial limit
        result = await gate.evaluate(
            "create-event", {}, "hard_write", "", "t1", "s1",
            is_reactive=False,
        )
        assert not result.allowed
        assert result.method == "denial_tracking"
        assert result.reason == "denial_limit"

    async def test_counter_resets_on_approval(self):
        gate = _make_gate()
        # Block once
        gate._denial_counts["create-event"] = 2
        # Then approve via token
        token = gate.issue_approval_token("create-event", {"summary": "X"})
        result = await gate.evaluate(
            "create-event", {"summary": "X"}, "hard_write", "", "t1", "s1",
            approval_token_id=token.token_id,
            is_reactive=False,
        )
        assert result.allowed
        assert "create-event" not in gate._denial_counts

    async def test_counter_resets_on_new_turn(self):
        gate = _make_gate()
        gate._denial_counts["create-event"] = 2
        gate.reset_denial_counts()
        assert gate._denial_counts == {}

    async def test_different_tools_tracked_independently(self):
        gate = _make_gate()
        gate._denial_counts["create-event"] = 2
        gate._denial_counts["send-email"] = 1
        # create-event is at 2, send-email at 1 — neither at limit (3)
        # Evaluating send-email should not hit denial limit
        result = await gate.evaluate(
            "send-email", {}, "hard_write", "", "t1", "s1",
            is_reactive=False,
        )
        assert result.method != "denial_tracking"

    def test_default_limit_is_3(self):
        gate = _make_gate()
        assert gate._denial_limit == 3

    async def test_denial_limit_surfaces_agent_facing_message(self):
        """DOCS-AUDIT-RECOVERY #6: when the gate short-circuits on the
        per-tool denial limit, the result's ``proposed_action`` carries
        an agent-readable message explaining WHY the call failed so the
        agent can pivot rather than loop."""
        gate = _make_gate()
        # Pre-load the counter so the next evaluate hits the limit.
        gate._denial_counts["create-event"] = 3
        result = await gate.evaluate(
            "create-event", {"summary": "X"}, "hard_write", "", "t1", "s1",
            is_reactive=False,
        )
        assert result.reason == "denial_limit"
        assert "denial limit" in result.proposed_action.lower()
        assert "create-event" in result.proposed_action
        # The message tells the agent what to do next.
        assert (
            "different approach" in result.proposed_action
            or "surface the friction" in result.proposed_action
            or "next turn" in result.proposed_action
        )
