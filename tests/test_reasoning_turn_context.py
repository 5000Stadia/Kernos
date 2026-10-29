"""Request-owned receipts and assembled-input bounds on the live turn path."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kernos.kernel.cohorts.descriptor import CohortFanOutResult
from kernos.kernel.engine import TaskEngine
from kernos.kernel.enactment import StaticToolCatalog, ToolCatalogEntry
from kernos.kernel.enactment.planner import PLAN_FINALIZE_TOOL_NAME
from kernos.kernel.integration.live_wiring import LiveExecutor, LiveIntegrationDispatcher
from kernos.kernel.reasoning import ReasoningRequest, ReasoningService
from kernos.kernel.turn_runner_provider import (
    ThinPathContext, _build_live_request_factory, build_turn_runner_provider,
)
from kernos.messages.handler import TurnContext
from kernos.messages.phases import reason
from kernos.providers.base import ContentBlock, Provider, ProviderResponse
from tests.test_enactment_dispatcher import _StubLookup, _descriptor
from tests.test_handler import _make_handler, _make_message
from tests.test_cognitive_context_types import _minimal_packet
from kernos.kernel.cognitive_context.types import ToolSurface


def _response(name=None, payload=None):
    block = (ContentBlock(type="tool_use", id="synthetic-call", name=name, input=payload)
             if name else ContentBlock(type="text", text="done"))
    return ProviderResponse(content=[block], stop_reason="tool_use" if name else "end_turn",
                            input_tokens=1, output_tokens=1)


def _live_service(modes, *, fail_render=False):
    """Real canonical factory, integration, renderer, dispatcher and executors.

    Only the external model, tool body, gate verdict and cohort fan-out are
    synthetic. A barrier holds completed tool calls before either turn ends.
    """
    reached = set()
    ready = asyncio.Event()
    release = asyncio.Event()
    render_counts = {}
    integration_counts = {}
    captured = []

    async def chain(system, messages, tools, max_tokens, *, conversation_id="", **kw):
        if not conversation_id:
            conversation_id = (next(iter(modes)) if len(modes) == 1 else next(
                turn for turn in modes
                if (f"<user_message>\n{turn}\n</user_message>" in messages[0]["content"]
                    or f"[turn:{turn}]" in messages[0]["content"])
            ))
        captured.append((conversation_id, messages))
        names = {tool["name"] for tool in tools}
        mode = modes[conversation_id]
        if "__finalize_briefing__" in names:
            count = integration_counts.get(conversation_id, 0)
            integration_counts[conversation_id] = count + 1
            if mode == "prep" and count == 0:
                return _response("email_send", {"marker": conversation_id})
            payload = {
                "relevant_context": [], "filtered_context": [],
                "decided_action": ({"kind": "execute_tool", "tool_id": "email_send", "arguments": {}}
                                   if mode == "full" else {"kind": "respond_only"}),
                "presence_directive": f"Answer the synthetic request. [turn:{conversation_id}]",
            }
            if mode == "full":
                payload["action_envelope"] = {
                    "intended_outcome": "synthetic lookup", "allowed_tool_classes": ["email"],
                    "allowed_operations": ["send"],
                }
            return _response("__finalize_briefing__", payload)
        if PLAN_FINALIZE_TOOL_NAME in names:
            return _response(PLAN_FINALIZE_TOOL_NAME, {"steps": [{
                "tool_id": "email_send", "tool_class": "email", "operation_name": "send",
                "arguments": {"marker": conversation_id},
                "expectation": {"prose": "synthetic result returned", "structured": [
                    {"kind": "returns_truthy", "args": {}},
                ]},
            }]})
        count = render_counts.get(conversation_id, 0)
        render_counts[conversation_id] = count + 1
        if mode == "thin" and count == 0:
            return _response("email_send", {"marker": conversation_id})
        reached.add(conversation_id)
        if len(reached) == len(modes):
            ready.set()
        await release.wait()
        if fail_render and mode == "full":
            raise RuntimeError("synthetic render failure after dispatch")
        return _response()

    gate = SimpleNamespace(
        classify_tool_effect=lambda *args: "read",
        evaluate=AsyncMock(return_value=SimpleNamespace(allowed=True)),
    )
    execute = AsyncMock(side_effect=lambda tool_id, args, request:
                        f"receipt-{request.active_space_id}-{args['marker']}")
    factory = _build_live_request_factory()
    context = ThinPathContext(
        chain_caller=chain, integration_audit_emitter=AsyncMock(),
        cohort_runner=SimpleNamespace(run=AsyncMock(return_value=CohortFanOutResult(
            outputs=(), fan_out_started_at="2026-01-01T00:00:00+00:00",
            fan_out_completed_at="2026-01-01T00:00:01+00:00",
        ))),
        executor=LiveExecutor(execute_tool=execute, gate=gate, request_factory=factory),
        descriptor_lookup=_StubLookup({"email_send": _descriptor()}),
        integration_dispatcher=LiveIntegrationDispatcher(
            execute_tool=execute, gate=gate, request_factory=factory,
        ),
        planner_tool_catalog=StaticToolCatalog(entries=(ToolCatalogEntry(
            tool_id="email_send", tool_class="email", operation_name="send",
        ),)),
    )
    service = ReasoningService(
        provider=AsyncMock(spec=Provider), trace_sink=context.trace_sink,
        turn_runner_provider=build_turn_runner_provider(context),
    )
    service._gate = gate
    return service, ready, release, execute, captured


def _request(turn):
    request = ReasoningRequest(
        instance_id="synthetic-instance", conversation_id=turn, system_prompt="synthetic",
        messages=[{"role": "user", "content": "synthetic input"}], tools=[],
        model="synthetic-model", trigger="user_message", active_space_id=f"space-{turn}",
        member_id=f"member-{turn}", input_text=turn,
    )
    # Permit running these regressions against pre-carrier code as behavioral
    # failures rather than failing construction on an unknown keyword.
    request.tool_calls_trace = []
    request.cognitive_context = _minimal_packet().with_updates(tool_surface=ToolSurface(
        always_pinned=({"name": "email_send", "description": "Synthetic tool",
                        "input_schema": {"type": "object", "properties": {}}},),
        active_zone=(), request_tool=None,
    ))
    return request


@pytest.mark.parametrize("modes", [
    {"a": "full", "b": "full"},
    {"a": "full", "b": "thin"},
    {"a": "prep", "b": "thin"},
    {"a": "full", "b": "quiet"},
])
async def test_concurrent_live_turn_receipts_belong_to_request(modes):
    service, ready, release, execute, _ = _live_service(modes)
    requests = [_request(turn) for turn in modes]
    tasks = [asyncio.create_task(service.reason(request)) for request in requests]
    try:
        await asyncio.wait_for(ready.wait(), 3)
        release.set()
        results = await asyncio.gather(*tasks)
        assert [result.text for result in results] == ["done", "done"]
        for request in requests:
            entries = request.tool_calls_trace
            expected_count = 0 if modes[request.conversation_id] == "quiet" else 1
            assert len(entries) == expected_count
            if entries:
                assert entries[0]["input"] == {"marker": request.conversation_id}
                assert f"receipt-{request.active_space_id}" in entries[0]["result_preview"]
                assert entries[0]["success"] is True
        assert execute.await_count == sum(mode != "quiet" for mode in modes.values())
        assert service.drain_tool_trace() == []
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_failed_live_turn_does_not_leave_receipts_for_next_turn():
    modes = {"a": "full"}
    service, ready, release, _, _ = _live_service(modes, fail_render=True)
    request = _request("a")
    task = asyncio.create_task(service.reason(request))
    await asyncio.wait_for(ready.wait(), 3)
    release.set()
    with pytest.raises(RuntimeError, match="synthetic render failure"):
        await task
    assert len(request.tool_calls_trace) == 1
    modes["b"] = "quiet"
    next_request = _request("b")
    assert (await service.reason(next_request)).text == "done"
    assert next_request.tool_calls_trace == []
    assert service.drain_tool_trace() == []


@pytest.mark.parametrize("failure", ["typed", "raises"])
async def test_live_inline_failure_receipt_stays_on_request(failure):
    from kernos.kernel.tool_failure import ToolFailure

    service, _, release, execute, _ = _live_service({"a": "thin"})
    if failure == "typed":
        execute.side_effect = None
        execute.return_value = ToolFailure("synthetic tool failure")
    else:
        execute.side_effect = RuntimeError("synthetic tool failure")
    release.set()
    request = _request("a")
    assert (await service.reason(request)).text == "done"
    assert len(request.tool_calls_trace) == 1
    assert request.tool_calls_trace[0]["success"] is False
    assert "synthetic tool failure" in request.tool_calls_trace[0]["result_preview"]
    assert service.drain_tool_trace() == []


async def test_handler_persists_its_request_receipts_without_global_drain():
    handler, _ = _make_handler()
    handler.conv_logger.append = AsyncMock()
    handler.reasoning._turn_tool_trace.append({
        "name": "other_turn", "input": {}, "success": True, "result_preview": "other receipt",
    })

    async def execute(task, request):
        if not hasattr(request, "tool_calls_trace"):
            request.tool_calls_trace = []
        request.tool_calls_trace.append({
            "name": "own_tool", "input": {}, "success": True, "result_preview": "own receipt",
        })
        task.result_text = "done"
        return task

    handler.engine.execute = execute
    try:
        assert await handler.process(_make_message("synthetic input")) == "done"
        receipts = [call.kwargs["content"] for call in handler.conv_logger.append.call_args_list
                    if call.kwargs.get("channel") == "receipt"]
        assert receipts == ["Action state this turn:\n[own_tool] state=completed | own receipt"]
        assert handler.reasoning._turn_tool_trace[0]["name"] == "other_turn"
    finally:
        await handler.shutdown_runners()


async def test_assembled_input_reaches_real_integration_with_notices_and_file_reference():
    handler, _ = _make_handler()
    message = _make_message("X" * 20000 + "SYNTHETIC_UNBOUNDED_TAIL")
    ctx = TurnContext(handler=handler, message=message, instance_id=message.instance_id,
                      conversation_id=message.conversation_id, member_id="synthetic-member")
    await handler._phase_provision(ctx)
    await handler._phase_route(ctx)
    ctx.active_space_id = "synthetic-space"
    ctx.upload_notifications = ["SYNTHETIC_UPLOAD_NOTICE"]
    handler._orphaned_user_content = ["SYNTHETIC_EARLIER_INPUT"]
    handler._files = SimpleNamespace(write_file=AsyncMock())
    await handler._phase_assemble(ctx)
    assembled = ctx.messages[-1]["content"]
    assert len(assembled) < 4000
    assert "SYNTHETIC_UPLOAD_NOTICE" in assembled
    assert "SYNTHETIC_EARLIER_INPUT" in assembled
    assert "Message continues" in assembled and "read_file(" in assembled
    assert "SYNTHETIC_UNBOUNDED_TAIL" not in assembled
    saved = [call.args[3] for call in handler._files.write_file.call_args_list
             if call.kwargs.get("description") == "User input (auto-persisted, oversized)"]
    assert len(saved) == 1 and saved[0].endswith("SYNTHETIC_UNBOUNDED_TAIL")

    service, ready, release, _, captured = _live_service({ctx.conversation_id: "quiet"})
    release.set()
    handler.reasoning = service
    handler.engine = TaskEngine(reasoning=service, events=handler.events)
    await reason.run(ctx)
    initial_payload = captured[0][1][0]["content"]
    assert f"<user_message>\n{assembled}\n</user_message>" in initial_payload
    assert "SYNTHETIC_UNBOUNDED_TAIL" not in initial_payload
