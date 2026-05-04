"""Shared test fixture for thin-path ReasoningService construction.

TEST-INFRA-PARITY-V1 (2026-05-03): construction-test fixture that lets
unit tests build a ``ReasoningService`` with ``turn_runner_provider``
wired without re-implementing the full thin-path bring-up. The
fixture returns a stub turn-runner-provider that runs a minimal
tool-use loop against the test's mock provider and (optional) MCP
client — enough to satisfy integration tests that exercise
``handler.process`` / ``engine.execute`` / ``reasoning.reason`` end-
to-end without instantiating the full production stack.

What this fixture does:

  1. Calls ``provider.complete`` with the request's system / messages
     / tools.
  2. If the response carries ``tool_use`` blocks, dispatches each
     through ``reasoning.execute_tool`` (kernel tools) or
     ``mcp.call_tool`` (MCP tools), appends results, calls
     ``provider.complete`` again.
  3. Returns ``ReasoningResult`` on the first text-only response.

What this fixture does NOT do:

  - Per-turn telemetry aggregation. Token counts come from the last
    provider response only. Tests that pin precise input/output
    token counts get the values the mock provided. Tests that pin
    cumulative aggregation across multiple model calls don't fit
    this fixture.
  - Synthetic ``reasoning.request`` / ``reasoning.response`` events.
    Tests that pin those go through the real production pipeline,
    not this fixture.
  - Audit/trace emission via the live integration dispatcher /
    StepDispatcher seams. Tests that pin those use the real Live*
    components, not this fixture.

The fixture is for tests that pin "did the provider get called with
the right shape, did tools fire, did the right text come back." For
tests that pin substrate fidelity through the real pipeline, the
soak harness is the contract surface.
"""

from __future__ import annotations

import json
from typing import Any, Callable
from unittest.mock import AsyncMock

from kernos.kernel.reasoning import ReasoningResult


def make_test_turn_runner_provider(
    *,
    provider: Any,
    mcp: Any = None,
    reasoning_ref: Callable[[], Any] | None = None,
    max_tool_iterations: int = 10,
) -> Callable[[Any, Any], tuple]:
    """Return a stub ``turn_runner_provider`` callable for unit tests.

    Used by test ``_make_*`` helpers so bare ``ReasoningService``
    construction has the post-CCV1-C7-strike construction contract
    honored. Each turn runs the minimal tool-use loop documented in
    the module docstring.

    Args:
      provider: the test's mock provider. Its ``complete`` is called
        per LLM turn.
      mcp: optional mock MCPClientManager. When set, MCP-tool
        dispatch routes through ``mcp.call_tool``. When None, MCP
        tool calls return a stub string.
      reasoning_ref: optional callable returning the constructed
        ``ReasoningService`` post-init. Lets the loop dispatch
        kernel tools through ``reasoning.execute_tool``. When None,
        kernel tools are not dispatched (tests that need kernel
        tools must set this).
      max_tool_iterations: bounded iteration count to prevent
        runaway loops in misbehaved tests.
    """

    async def _stub_provider_factory(request: Any, event_emitter: Any) -> tuple:
        class _StubDelivery:
            async def emit_request_event(self) -> None:
                return None

        class _StubTurnRunner:
            async def run_turn(self, inputs: Any) -> ReasoningResult:
                messages = list(getattr(request, "messages", []))
                tools = list(getattr(request, "tools", []))
                system = getattr(request, "system_prompt", "")
                model = getattr(request, "model", "test-model")
                iterations = 0
                total_input_tokens = 0
                total_output_tokens = 0

                while iterations < max_tool_iterations:
                    response = await provider.complete(
                        model=model,
                        system=system,
                        messages=messages,
                        tools=tools,
                        max_tokens=getattr(request, "max_tokens", 1024),
                    )
                    # Sum tokens conservatively — tests that pin
                    # cumulative aggregation read from this surface.
                    total_input_tokens += int(getattr(response, "input_tokens", 0) or 0)
                    total_output_tokens += int(getattr(response, "output_tokens", 0) or 0)

                    text_chunks: list[str] = []
                    tool_uses: list[Any] = []
                    for block in getattr(response, "content", []) or []:
                        btype = getattr(block, "type", "")
                        if btype == "text":
                            text_chunks.append(getattr(block, "text", "") or "")
                        elif btype == "tool_use":
                            tool_uses.append(block)

                    if not tool_uses:
                        return ReasoningResult(
                            text="".join(text_chunks),
                            model=model,
                            input_tokens=total_input_tokens,
                            output_tokens=total_output_tokens,
                            estimated_cost_usd=0.0,
                            duration_ms=0,
                            tool_iterations=iterations,
                        )

                    # Tools fired — append assistant turn + tool
                    # results, loop.
                    messages.append({"role": "assistant", "content": response.content})
                    tool_result_blocks: list[dict] = []
                    for tu in tool_uses:
                        tname = getattr(tu, "name", "")
                        targs = getattr(tu, "input", {}) or {}
                        tu_id = getattr(tu, "id", "")
                        result_text = await _dispatch_tool(
                            tname, targs, request, mcp, reasoning_ref,
                        )
                        tool_result_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tu_id,
                            "content": result_text,
                        })
                    messages.append({"role": "user", "content": tool_result_blocks})
                    iterations += 1

                # Bounded — return whatever we have.
                return ReasoningResult(
                    text="(test stub: max tool iterations reached)",
                    model=model,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    estimated_cost_usd=0.0,
                    duration_ms=0,
                    tool_iterations=iterations,
                )

        return _StubTurnRunner(), _StubDelivery()

    return _stub_provider_factory


async def _dispatch_tool(
    tool_name: str,
    tool_input: dict,
    request: Any,
    mcp: Any,
    reasoning_ref: Callable[[], Any] | None,
) -> str:
    """Dispatch a single tool call through the test stub's pipeline.

    Kernel tools (in ``ReasoningService._KERNEL_TOOLS``) route
    through ``reasoning.execute_tool`` when reasoning_ref is set.
    Otherwise MCP tools route through ``mcp.call_tool``. Defensive
    fallback returns a stub error string so misconfigured tests
    don't deadlock.
    """
    if reasoning_ref is not None:
        try:
            from kernos.kernel.reasoning import ReasoningService
            reasoning = reasoning_ref()
            if reasoning is not None and tool_name in ReasoningService._KERNEL_TOOLS:
                return await reasoning.execute_tool(
                    tool_name, tool_input, request,
                )
        except Exception as exc:
            return f"test stub kernel tool dispatch failed: {exc}"
    if mcp is not None:
        try:
            return await mcp.call_tool(tool_name, tool_input)
        except Exception as exc:
            return f"test stub MCP tool dispatch failed: {exc}"
    return f"test stub: tool {tool_name!r} dispatched without mcp or reasoning_ref"


def wire_test_thin_path(
    reasoning_service: Any,
    *,
    provider: Any,
    mcp: Any = None,
    max_tool_iterations: int = 10,
) -> None:
    """Mutate an already-constructed ``ReasoningService`` to wire
    a stub turn-runner-provider.

    For tests that build the service directly via
    ``ReasoningService(mock_provider, events, mcp, audit)`` and need
    the post-strike construction contract satisfied without
    rewriting every test.

    Reasoning_ref closure captures the ReasoningService so kernel-
    tool dispatch routes through its execute_tool elif chain.
    """
    reasoning_service._turn_runner_provider = make_test_turn_runner_provider(
        provider=provider,
        mcp=mcp,
        reasoning_ref=lambda: reasoning_service,
        max_tool_iterations=max_tool_iterations,
    )


__all__ = [
    "make_test_turn_runner_provider",
    "wire_test_thin_path",
]
