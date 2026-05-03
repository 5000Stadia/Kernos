"""Pin tests for CONFIRMED-PATH-DISPATCH-PARITY-V1.

Architect verdict 2026-05-03 (Bucket C surfaced during strike re-stage):
five tools were dispatched only inside the legacy reason() loop's
elif chain — remember_details, inspect_state, set_chain_model,
diagnose_llm_chain, diagnose_messenger. The legacy strike removes
the loop; their dispatch parity must move to execute_tool() before
the strike can ship cleanly.

This file pins:

  * Direct dispatch for all five at the new seam (execute_tool).
  * Admin-space gating for the three admin-gated tools
    (set_chain_model, diagnose_llm_chain, diagnose_messenger).
  * The shared _assert_admin_space helper produces the canonical
    rejection text (no duplicate policy).
  * Static parity coverage: every entry in _KERNEL_TOOLS has a
    dispatch home (execute_tool elif, canvas helper, or named
    exclusion).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernos.kernel.reasoning import ReasoningRequest, ReasoningService


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


@dataclass
class _FakeContextSpace:
    space_type: str = "general"


def _make_request(
    *, instance_id: str = "test:inst",
    space_type: str = "general",
    member_id: str = "mem_test",
    tool_input_extras: dict | None = None,
) -> ReasoningRequest:
    return ReasoningRequest(
        instance_id=instance_id,
        conversation_id="conv-test",
        system_prompt="",
        messages=[],
        tools=[],
        model="test-model",
        trigger="test",
        active_space_id="space_test",
        member_id=member_id,
        active_space=_FakeContextSpace(space_type=space_type),
    )


def _make_service() -> ReasoningService:
    """Build a bare ReasoningService for unit-testing execute_tool dispatch.

    Internal stores wired to mocks; we exercise the dispatch elif
    chain without requiring the full handler/MCP stack.
    """
    from kernos.providers.base import Provider, ProviderResponse, ChainEntry

    class _StubProvider(Provider):
        provider_name = "stub"

        async def complete(self, **kwargs):  # type: ignore[override]
            return ProviderResponse(
                content=[], stop_reason="end_turn",
                input_tokens=0, output_tokens=0,
            )

    chains = {"primary": [ChainEntry(provider=_StubProvider(), model="m")]}
    svc = ReasoningService(chains=chains)
    return svc


# ---------------------------------------------------------------------------
# Behavior 1 — five elif branches dispatch correctly at execute_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember_details_dispatches_through_helper():
    """remember_details routes to _handle_remember_details."""
    svc = _make_service()
    svc._handle_remember_details = AsyncMock(return_value="recall result")
    request = _make_request()
    result = await svc.execute_tool(
        "remember_details",
        {"query": "what did I say about marigold?"},
        request,
    )
    assert result == "recall result"
    svc._handle_remember_details.assert_awaited_once()


@pytest.mark.asyncio
async def test_inspect_state_dispatches_through_introspection():
    """inspect_state routes to introspection.build_user_truth_view."""
    svc = _make_service()
    svc._state = MagicMock()
    svc._trigger_store = MagicMock()
    svc._registry = MagicMock()
    request = _make_request()
    with patch(
        "kernos.kernel.introspection.build_user_truth_view",
        new=AsyncMock(return_value="state snapshot"),
    ) as mock_view:
        result = await svc.execute_tool("inspect_state", {}, request)
    assert result == "state snapshot"
    mock_view.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_chain_model_dispatches_in_system_space():
    """set_chain_model in system space routes to admin_tools."""
    svc = _make_service()
    request = _make_request(space_type="system")
    with patch(
        "kernos.setup.admin_tools.set_chain_model",
        return_value={"message": "model swapped to claude-opus-4-7"},
    ) as mock_set:
        result = await svc.execute_tool(
            "set_chain_model",
            {"chain": "primary", "provider_id": "anthropic", "model_id": "opus"},
            request,
        )
    assert "model swapped" in result
    mock_set.assert_called_once()


@pytest.mark.asyncio
async def test_diagnose_llm_chain_dispatches_in_system_space():
    """diagnose_llm_chain in system space routes to admin_tools."""
    svc = _make_service()
    request = _make_request(space_type="system")
    with patch(
        "kernos.setup.admin_tools.diagnose_llm_chain",
        return_value={"primary": {"provider": "anthropic"}},
    ) as mock_diag:
        result = await svc.execute_tool("diagnose_llm_chain", {}, request)
    # Result is JSON-serialized; substring assertion suffices.
    assert "anthropic" in result
    mock_diag.assert_called_once()


@pytest.mark.asyncio
async def test_diagnose_messenger_dispatches_in_system_space():
    """diagnose_messenger in system space routes to cohorts.admin."""
    svc = _make_service()
    svc._state = MagicMock()
    request = _make_request(space_type="system")
    with patch(
        "kernos.cohorts.admin.diagnose_messenger",
        new=AsyncMock(return_value={"member_a": "ok", "member_b": "ok"}),
    ) as mock_diag:
        result = await svc.execute_tool(
            "diagnose_messenger",
            {"member_a_id": "mem_a", "member_b_id": "mem_b"},
            request,
        )
    assert "ok" in result
    mock_diag.assert_awaited_once()


# ---------------------------------------------------------------------------
# Behavior 2 — admin-space gating (the three gated tools)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_chain_model_rejected_outside_system_space():
    """set_chain_model in general space returns admin-only message."""
    svc = _make_service()
    request = _make_request(space_type="general")
    result = await svc.execute_tool(
        "set_chain_model",
        {"chain": "primary"},
        request,
    )
    assert "admin-only" in result
    assert "System space" in result


@pytest.mark.asyncio
async def test_diagnose_llm_chain_rejected_outside_system_space():
    """diagnose_llm_chain in general space returns admin-only message."""
    svc = _make_service()
    request = _make_request(space_type="general")
    result = await svc.execute_tool("diagnose_llm_chain", {}, request)
    assert "admin-only" in result


@pytest.mark.asyncio
async def test_diagnose_messenger_rejected_outside_system_space():
    """diagnose_messenger in general space returns admin-only message."""
    svc = _make_service()
    request = _make_request(space_type="general")
    result = await svc.execute_tool(
        "diagnose_messenger",
        {"member_a_id": "a", "member_b_id": "b"},
        request,
    )
    assert "admin-only" in result


# ---------------------------------------------------------------------------
# Behavior 3 — _assert_admin_space helper carries the canonical policy
# ---------------------------------------------------------------------------


def test_assert_admin_space_returns_rejection_for_non_system_space():
    """Helper returns the canonical rejection text for non-system spaces."""
    svc = _make_service()
    request = _make_request(space_type="general")
    msg = svc._assert_admin_space(request, "set_chain_model")
    assert msg is not None
    assert "set_chain_model" in msg
    assert "admin-only" in msg
    assert "System space" in msg


def test_assert_admin_space_returns_none_for_system_space():
    """Helper returns None (allow) when the active space is 'system'."""
    svc = _make_service()
    request = _make_request(space_type="system")
    assert svc._assert_admin_space(request, "any_admin_tool") is None


def test_assert_admin_space_handles_missing_active_space():
    """Helper treats missing active_space as non-system (defensive)."""
    svc = _make_service()
    request = _make_request(space_type="general")
    request.active_space = None
    msg = svc._assert_admin_space(request, "set_chain_model")
    assert msg is not None
    assert "admin-only" in msg


def test_assert_admin_space_canonical_policy_text_no_duplication_post_strike():
    """The rejection text comes from one source post-strike. Pre-
    strike the legacy reason() loop still carries duplicate inline
    policy strings (set_chain_model / diagnose_llm_chain /
    diagnose_messenger). The strike removes that loop; post-strike
    the helper is the single source.

    During the parity-V1-to-strike interval, the helper's policy
    fragment appears once (in the helper) and the legacy loop has
    three more inline copies. After the strike the count drops to
    one. Pin: helper carries the canonical fragment now.
    """
    import inspect
    from kernos.kernel import reasoning
    src = inspect.getsource(reasoning)
    fragment = "is admin-only and only available"
    helper_src = inspect.getsource(reasoning.ReasoningService._assert_admin_space)
    assert fragment in helper_src, (
        "_assert_admin_space helper must carry the canonical "
        "admin-space rejection text"
    )
    # Helper is single source going forward; pre-strike we also
    # have the legacy copies. The strike commit removes those.


# ---------------------------------------------------------------------------
# Static parity pin: every _KERNEL_TOOLS entry has a dispatch home
# ---------------------------------------------------------------------------


# Tools intentionally NOT dispatched directly through execute_tool.
# Each entry must carry a documented rationale.
_DISPATCH_EXCLUSIONS: dict[str, str] = {
    # No exclusions today — every kernel tool dispatches through
    # either execute_tool's elif chain or _handle_canvas_tool.
    # If a future tool legitimately bypasses both (e.g., a
    # cohort-only tool), add it here with a one-line reason.
}


def _execute_tool_dispatched_names() -> set[str]:
    """Parse `tool_name == "<name>"` and tuple-membership branches
    from execute_tool's elif chain."""
    import re
    from pathlib import Path
    src = (
        Path(__file__).resolve().parent.parent
        / "kernos" / "kernel" / "reasoning.py"
    ).read_text()
    # Find execute_tool body — between its def and the next async def.
    start_match = re.search(r"^    async def execute_tool\(", src, re.MULTILINE)
    assert start_match, "execute_tool not found in reasoning.py"
    body_start = start_match.start()
    after = src[body_start:]
    end_match = re.search(
        r"^    (async )?def [^e]", after[1:], re.MULTILINE,
    )
    body_end = (
        body_start + 1 + end_match.start() if end_match else len(src)
    )
    body = src[body_start:body_end]

    names: set[str] = set()
    # Direct equality: tool_name == "<name>"
    for m in re.finditer(r'tool_name == "([a-z_]+)"', body):
        names.add(m.group(1))
    # Tuple membership for canvas tools.
    for m in re.finditer(r'tool_name in \(([^)]+)\)', body):
        for sub in re.finditer(r'"([a-z_]+)"', m.group(1)):
            names.add(sub.group(1))
    return names


def test_every_kernel_tool_has_dispatch_home_or_named_exclusion():
    """Static parity pin: every entry in _KERNEL_TOOLS is either
    handled by execute_tool's elif chain (or its canvas helper
    branch), or appears in _DISPATCH_EXCLUSIONS with a rationale.

    Future tool registration that lands in _KERNEL_TOOLS without a
    dispatch home fails CI. The parity-V1 spec calls this out as
    the load-bearing pin against the kind of bucket-C drift that
    surfaced during strike re-stage on 2026-05-03.
    """
    dispatched = _execute_tool_dispatched_names()
    missing: list[str] = []
    for name in ReasoningService._KERNEL_TOOLS:
        if name in dispatched:
            continue
        if name in _DISPATCH_EXCLUSIONS:
            continue
        missing.append(name)
    assert not missing, (
        f"kernel tools without dispatch home: {sorted(missing)}. "
        f"Each tool must have an `elif tool_name == \"<name>\"` "
        f"branch in execute_tool, or a named entry in "
        f"_DISPATCH_EXCLUSIONS with a one-line rationale. "
        f"Parity pin per CONFIRMED-PATH-DISPATCH-PARITY-V1."
    )
