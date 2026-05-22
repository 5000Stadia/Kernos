"""LIVE-DISPATCH-UNBLOCKER-V1 Phase B acceptance tests.

Pins ACs 6-12: scoped amortization layer per
[[kernos-dispatch-gate-design-input]]. Gate.evaluate() always
runs evaluation; cache collapses user-visible CONFIRM cost on
stable bindings (actor + tool_hash + effect + scope).

hard_write NEVER amortizes (per-call evaluation is its
semantics). read/soft_write/external_agent_read amortize.
Cache wipes on mode swap.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.gate import (
    DispatchGate,
    GateResult,
    GateModePolicy,
    _POLICY_PERMISSIVE,
    _POLICY_STRICT,
)
from kernos.kernel.state import InstanceProfile


# POSTURE-EVALUATION-MODES-V1 (2026-05-22) — pin to pre-V1
# 'balanced' so test assertions about CONFIRM behavior hold.
@pytest.fixture(autouse=True)
def _pin_gate_mode_balanced(monkeypatch):
    monkeypatch.setenv("KERNOS_GATE_MODE", "balanced")


def _make_gate(*, classification: str = "soft_write") -> DispatchGate:
    """Build a gate whose complete_simple always returns APPROVE,
    so model-eval-path approves let amortization cache them."""
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value="APPROVE")
    registry = MagicMock()
    registry.get_all.return_value = []
    registry.get_metadata = MagicMock(return_value=None)  # no metadata yet
    state = AsyncMock()
    state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
        instance_id="t1", status="active", created_at="2026-01-01",
    ))
    state.query_covenant_rules = AsyncMock(return_value=[])
    events = MagicMock()
    return DispatchGate(reasoning, registry, state, events)


# ============================================================
# AC6 — first call hits model; second call within TTL is amortized
# ============================================================


@pytest.mark.asyncio
async def test_ac6_repeat_call_amortizes():
    gate = _make_gate()
    # First call: hits the model (returns APPROVE)
    result1 = await gate.evaluate(
        tool_name="page_write", tool_input={"name": "x"},
        effect="soft_write", user_message="write a page",
        instance_id="t1", active_space_id="s1",
        member_id="owner_a", is_reactive=False,
    )
    # Use is_reactive=False so the reactive-soft_write bypass doesn't fire
    assert result1.allowed is True
    # Second call with same binding: amortized
    result2 = await gate.evaluate(
        tool_name="page_write", tool_input={"name": "y"},
        effect="soft_write", user_message="write another page",
        instance_id="t1", active_space_id="s1",
        member_id="owner_a", is_reactive=False,
    )
    assert result2.allowed is True
    assert result2.reason == "amortized"
    assert result2.method == "amortization"
    # Model only called once
    assert gate._reasoning.complete_simple.call_count == 1


# ============================================================
# AC7 — TTL expiry forces re-evaluation
# ============================================================


@pytest.mark.asyncio
async def test_ac7_ttl_expiry_evicts_entry(monkeypatch):
    # Set a tiny TTL to keep the test fast
    monkeypatch.setenv("KERNOS_GATE_AMORTIZATION_TTL_SEC", "0.05")
    gate = _make_gate()
    await gate.evaluate(
        tool_name="page_write", tool_input={"name": "x"},
        effect="soft_write", user_message="x",
        instance_id="t1", active_space_id="s1",
        member_id="owner_a", is_reactive=False,
    )
    # Cache should have one entry
    assert len(gate._amortization_cache) == 1
    # Wait past TTL
    time.sleep(0.1)
    await gate.evaluate(
        tool_name="page_write", tool_input={"name": "y"},
        effect="soft_write", user_message="y",
        instance_id="t1", active_space_id="s1",
        member_id="owner_a", is_reactive=False,
    )
    # Cache miss → model fired twice total
    assert gate._reasoning.complete_simple.call_count == 2


# ============================================================
# AC8 — hard_write NEVER amortizes
# ============================================================


@pytest.mark.asyncio
async def test_ac8_hard_write_never_amortizes():
    gate = _make_gate()
    for _ in range(3):
        result = await gate.evaluate(
            tool_name="restart_self", tool_input={"confirm": True},
            effect="hard_write", user_message="restart",
            instance_id="t1", active_space_id="s1",
            member_id="owner_a", is_reactive=False,
        )
        assert result.allowed is True
        # reason is "approved" via model, never "amortized"
        assert result.reason != "amortized"
    # Model fired all three times
    assert gate._reasoning.complete_simple.call_count == 3
    # No hard_write entries in cache
    assert len(gate._amortization_cache) == 0


# ============================================================
# AC9 — external_agent_read amortizes
# ============================================================


@pytest.mark.asyncio
async def test_ac9_external_agent_read_amortizes():
    gate = _make_gate()
    await gate.evaluate(
        tool_name="consult", tool_input={"target": "codex", "prompt": "x"},
        effect="external_agent_read",
        user_message="consult codex",
        instance_id="t1", active_space_id="s1",
        member_id="owner_a", is_reactive=False,
    )
    result2 = await gate.evaluate(
        tool_name="consult", tool_input={"target": "codex", "prompt": "y"},
        effect="external_agent_read",
        user_message="consult codex again",
        instance_id="t1", active_space_id="s1",
        member_id="owner_a", is_reactive=False,
    )
    assert result2.reason == "amortized"


# ============================================================
# AC11 — set_mode_policy wipes the cache
# ============================================================


@pytest.mark.asyncio
async def test_ac11_mode_swap_wipes_cache():
    gate = _make_gate()
    await gate.evaluate(
        tool_name="page_write", tool_input={"name": "x"},
        effect="soft_write", user_message="x",
        instance_id="t1", active_space_id="s1",
        member_id="owner_a", is_reactive=False,
    )
    assert len(gate._amortization_cache) == 1
    gate.set_mode_policy(_POLICY_STRICT)
    assert len(gate._amortization_cache) == 0


# ============================================================
# AC12 — LRU bounded eviction
# ============================================================


@pytest.mark.asyncio
async def test_ac12_lru_eviction_under_pressure(monkeypatch):
    monkeypatch.setenv("KERNOS_GATE_AMORTIZATION_MAX_ENTRIES", "3")
    gate = _make_gate()
    # Fill the cache past capacity
    for i in range(5):
        await gate.evaluate(
            tool_name=f"page_write_{i}", tool_input={},
            effect="soft_write", user_message=f"call {i}",
            instance_id="t1", active_space_id="s1",
            member_id="owner_a", is_reactive=False,
        )
    # Only the most-recent 3 should remain
    assert len(gate._amortization_cache) == 3


# ============================================================
# Different bindings don't cross-contaminate
# ============================================================


@pytest.mark.asyncio
async def test_different_members_have_separate_cache_entries():
    gate = _make_gate()
    await gate.evaluate(
        tool_name="page_write", tool_input={"name": "x"},
        effect="soft_write", user_message="x",
        instance_id="t1", active_space_id="s1",
        member_id="alice", is_reactive=False,
    )
    # Bob's first call should still hit the model
    await gate.evaluate(
        tool_name="page_write", tool_input={"name": "x"},
        effect="soft_write", user_message="x",
        instance_id="t1", active_space_id="s1",
        member_id="bob", is_reactive=False,
    )
    assert gate._reasoning.complete_simple.call_count == 2


@pytest.mark.asyncio
async def test_different_spaces_have_separate_cache_entries():
    gate = _make_gate()
    await gate.evaluate(
        tool_name="page_write", tool_input={"name": "x"},
        effect="soft_write", user_message="x",
        instance_id="t1", active_space_id="space_a",
        member_id="alice", is_reactive=False,
    )
    await gate.evaluate(
        tool_name="page_write", tool_input={"name": "x"},
        effect="soft_write", user_message="x",
        instance_id="t1", active_space_id="space_b",
        member_id="alice", is_reactive=False,
    )
    assert gate._reasoning.complete_simple.call_count == 2


# ============================================================
# Cache is NOT populated on denial
# ============================================================


@pytest.mark.asyncio
async def test_denials_do_not_cache():
    gate = _make_gate()
    # Make the model return CONFIRM → ambiguous → balanced fallback "confirm" (block)
    gate._reasoning.complete_simple = AsyncMock(return_value="CONFIRM")
    result = await gate.evaluate(
        tool_name="page_write", tool_input={},
        effect="soft_write", user_message="x",
        instance_id="t1", active_space_id="s1",
        member_id="owner_a", is_reactive=False,
    )
    assert result.allowed is False
    assert len(gate._amortization_cache) == 0


# ============================================================
# clear_amortization_cache helper
# ============================================================


@pytest.mark.asyncio
async def test_clear_cache_method():
    gate = _make_gate()
    await gate.evaluate(
        tool_name="page_write", tool_input={},
        effect="soft_write", user_message="x",
        instance_id="t1", active_space_id="s1",
        member_id="owner_a", is_reactive=False,
    )
    assert len(gate._amortization_cache) == 1
    gate.clear_amortization_cache()
    assert len(gate._amortization_cache) == 0
