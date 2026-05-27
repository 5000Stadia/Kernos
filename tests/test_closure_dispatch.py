"""SELF-IMPROVEMENT-CLOSURE-V1 Phase B — kernel-tool dispatch +
gate classification tests.

Coverage:
* AC9 — DispatchGate.classify_tool_effect returns the right
  values for the three closure tools.
* AC17 — _DISPATCHABLE_KERNEL_TOOLS contract: every name in
  get_dispatchable_kernel_tools() has a concrete handler branch
  in execute_tool that does NOT return the sentinel
  "Kernel tool '<name>' not handled." string.
* Dispatch surface — closure-tool dispatch branches reach
  closure_store helpers; friendly-string fallback when handler
  isn't wired.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kernos.kernel.closure_store import ClosureStore
from kernos.kernel.friction_patterns import FrictionPatternStore
from kernos.kernel.gate import DispatchGate
from kernos.kernel.reasoning import ReasoningRequest, ReasoningService


# ---------------------------------------------------------------------
# AC9 — gate classification
# ---------------------------------------------------------------------


@pytest.fixture
def gate() -> DispatchGate:
    return DispatchGate(
        reasoning_service=None, registry=None, state=None, events=None,
    )


def test_ac9_lookup_pattern_invariants_classifies_as_read(gate):
    assert gate.classify_tool_effect(
        "lookup_pattern_invariants", None, None,
    ) == "read"


def test_ac9_record_closure_attempt_classifies_as_soft_write(gate):
    assert gate.classify_tool_effect(
        "record_closure_attempt", None, None,
    ) == "soft_write"


def test_ac9_run_closure_probe_classifies_as_soft_write(gate):
    """Wrapper soft_write — probe handler is read-only but the
    wrapper updates closure_attempt outcome + may transition the
    friction pattern lifecycle + emits closure.probe_failed."""
    assert gate.classify_tool_effect(
        "run_closure_probe", None, None,
    ) == "soft_write"


# ---------------------------------------------------------------------
# AC17 — dispatchability registry contract
# ---------------------------------------------------------------------


@pytest.fixture
def reasoning_service():
    """Minimal ReasoningService with no provider/handler bindings.
    Sufficient for the catch-all sentinel check — execute_tool's
    closure-branch returns the friendly fallback string when
    handler is unwired, which is NOT the sentinel string."""
    stub_provider = MagicMock()
    stub_provider.complete = MagicMock()
    rs = ReasoningService(provider=stub_provider, events=None, mcp=None)
    return rs


async def test_ac17_dispatchability_registry_subset_of_kernel_tools(
    reasoning_service,
):
    """Sanity check: every dispatchable name is also in _KERNEL_TOOLS."""
    dispatchable = reasoning_service.get_dispatchable_kernel_tools()
    kernel_tools = reasoning_service._KERNEL_TOOLS
    extras = dispatchable - kernel_tools
    assert not extras, (
        f"_DISPATCHABLE_KERNEL_TOOLS contains names NOT in "
        f"_KERNEL_TOOLS: {sorted(extras)}"
    )


async def test_ac17_dispatchability_registry_no_sentinel_returns(
    reasoning_service,
):
    """The contract: for every name in
    get_dispatchable_kernel_tools(), execute_tool must NOT return
    the 'Kernel tool <name> not handled.' sentinel string.

    Names that route through dispatch helpers but fail at deeper
    substrate (because handler is unwired) MAY return a friendly
    fallback like 'X is not available.' — that's fine, the
    sentinel is what we're checking. Adding a name to
    _DISPATCHABLE_KERNEL_TOOLS without a handler branch breaks
    this test.
    """
    request = ReasoningRequest(
        instance_id="ac17_test",
        conversation_id="ac17_conv",
        system_prompt="",
        messages=[],
        tools=[],
        model="",
        trigger="test",
    )
    dispatchable = reasoning_service.get_dispatchable_kernel_tools()
    sentinel_returners = []
    for name in sorted(dispatchable):
        try:
            result = await reasoning_service.execute_tool(
                tool_name=name,
                tool_input={},
                request=request,
            )
        except Exception:
            # Handler may raise (e.g., on missing required arg).
            # AC17 is about the dispatcher reaching SOME branch,
            # which an exception proves it did. Sentinel returns
            # are the failure mode — exceptions are not.
            continue
        if isinstance(result, str) and (
            f"Kernel tool '{name}' not handled." in result
        ):
            sentinel_returners.append(name)
    assert not sentinel_returners, (
        f"_DISPATCHABLE_KERNEL_TOOLS contract violated: these "
        f"names returned the 'not handled' sentinel from "
        f"execute_tool: {sentinel_returners}. Either add their "
        f"handler branch or remove them from "
        f"_DISPATCHABLE_KERNEL_TOOLS."
    )


async def test_ac17_closure_tools_in_dispatchability_registry(
    reasoning_service,
):
    dispatchable = reasoning_service.get_dispatchable_kernel_tools()
    assert "record_closure_attempt" in dispatchable
    assert "run_closure_probe" in dispatchable
    assert "lookup_pattern_invariants" in dispatchable


# ---------------------------------------------------------------------
# Dispatch surface — closure tools reach closure_store
# ---------------------------------------------------------------------


@pytest.fixture
async def closure_handler(tmp_path: Path):
    """Build a minimal handler-shim with both friction_pattern_store
    and closure_store wired; sufficient for the dispatch branch to
    reach closure_store helpers."""
    fp_store = FrictionPatternStore()
    await fp_store.start(str(tmp_path))
    cl_store = ClosureStore()
    await cl_store.start(str(tmp_path))

    class _Handler:
        _friction_pattern_store = fp_store
        _closure_store = cl_store
        _events = None

    yield _Handler()
    await cl_store.close()
    await fp_store.stop()


async def test_dispatch_lookup_returns_no_invariants_when_unlinked(
    closure_handler,
):
    """End-to-end through reasoning.execute_tool — closure tool
    reaches closure_store and returns a JSON-encoded response."""
    stub_provider = MagicMock()
    stub_provider.complete = MagicMock()
    rs = ReasoningService(provider=stub_provider, events=None, mcp=None)
    rs._handler = closure_handler  # type: ignore[attr-defined]

    request = ReasoningRequest(
        instance_id="dispatch_test",
        conversation_id="dispatch_conv",
        system_prompt="",
        messages=[],
        tools=[],
        model="",
        trigger="test",
    )
    result_str = await rs.execute_tool(
        tool_name="lookup_pattern_invariants",
        tool_input={"pattern_id": "nonexistent"},
        request=request,
    )
    result = json.loads(result_str)
    assert result == {
        "has_invariants": False,
        "primary_invariant_id": "",
        "all_invariant_ids": [],
    }


async def test_dispatch_friendly_string_when_handler_unwired():
    """Handler with no _closure_store → friendly string, NOT the
    sentinel."""
    stub_provider = MagicMock()
    rs = ReasoningService(provider=stub_provider, events=None, mcp=None)
    # No handler attached.
    request = ReasoningRequest(
        instance_id="unwired_test",
        conversation_id="unwired_conv",
        system_prompt="",
        messages=[],
        tools=[],
        model="",
        trigger="test",
    )
    result = await rs.execute_tool(
        tool_name="lookup_pattern_invariants",
        tool_input={"pattern_id": "p1"},
        request=request,
    )
    assert isinstance(result, str)
    assert "Closure substrate is not available" in result
    assert "Kernel tool 'lookup_pattern_invariants' not handled." not in result
