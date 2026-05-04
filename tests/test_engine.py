"""Tests for the TaskEngine."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.engine import TaskEngine
from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream
from kernos.kernel.exceptions import ReasoningTimeoutError
from kernos.kernel.reasoning import (
    ContentBlock,
    Provider,
    ProviderResponse,
    ReasoningRequest,
    ReasoningService,
)
from kernos.kernel.task import Task, TaskStatus, TaskType
from kernos.capability.client import MCPClientManager
from kernos.persistence import AuditStore


def _make_request() -> ReasoningRequest:
    return ReasoningRequest(
        instance_id="sms:+15555550100",
        conversation_id="+15555550100",
        system_prompt="You are Kernos.",
        messages=[{"role": "user", "content": "Hello"}],
        tools=[],
        model="claude-sonnet-4-6",
        trigger="user_message",
    )


def _make_task() -> Task:
    return Task(
        id="task_123456789_abcd",
        type=TaskType.REACTIVE_SIMPLE,
        instance_id="sms:+15555550100",
        conversation_id="+15555550100",
        source="user_message",
        input_text="Hello",
        created_at="2026-03-03T00:00:00+00:00",
    )


def _mock_provider_response(text: str) -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="text", text=text)],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=20,
    )


def _make_engine() -> tuple[TaskEngine, AsyncMock, AsyncMock]:
    """Return (engine, mock_provider, mock_events)."""
    mock_provider = AsyncMock(spec=Provider)
    mcp = MagicMock(spec=MCPClientManager)
    mcp.call_tool = AsyncMock(return_value="")
    audit = AsyncMock(spec=AuditStore)
    audit.log.return_value = None
    events = AsyncMock(spec=EventStream)
    events.emit.return_value = None

    reasoning = ReasoningService(mock_provider, events, mcp, audit)
    # TEST-INFRA-PARITY-V1 (2026-05-03): wire stub turn_runner_provider
    # so post-strike construction contract is honored.
    from tests._thin_path_test_fixture import wire_test_thin_path
    wire_test_thin_path(reasoning, provider=mock_provider, mcp=mcp)
    engine = TaskEngine(reasoning=reasoning, events=events)
    return engine, mock_provider, events


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


async def test_execute_returns_completed_task():
    engine, mock_provider, _ = _make_engine()
    mock_provider.complete.return_value = _mock_provider_response("Hello back!")

    task = await engine.execute(_make_task(), _make_request())

    assert task.status == TaskStatus.COMPLETED
    assert task.result_text == "Hello back!"


async def test_execute_populates_metrics():
    engine, mock_provider, _ = _make_engine()
    mock_provider.complete.return_value = _mock_provider_response("Hello!")

    task = await engine.execute(_make_task(), _make_request())

    assert task.input_tokens == 10
    assert task.output_tokens == 20
    assert task.estimated_cost_usd >= 0
    assert task.duration_ms >= 0
    assert task.tool_iterations == 0  # no tools used, no tool iterations


async def test_execute_sets_started_at():
    engine, mock_provider, _ = _make_engine()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    task = _make_task()
    assert task.started_at == ""

    await engine.execute(task, _make_request())

    assert task.started_at != ""


async def test_execute_sets_completed_at():
    engine, mock_provider, _ = _make_engine()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    task = _make_task()
    await engine.execute(task, _make_request())

    assert task.completed_at != ""


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


async def test_execute_reraises_on_failure():
    engine, mock_provider, _ = _make_engine()
    mock_provider.complete.side_effect = ReasoningTimeoutError("timeout")

    with pytest.raises(ReasoningTimeoutError):
        await engine.execute(_make_task(), _make_request())


async def test_execute_sets_failed_status_on_error():
    engine, mock_provider, _ = _make_engine()
    mock_provider.complete.side_effect = ReasoningTimeoutError("timeout")

    task = _make_task()
    with pytest.raises(ReasoningTimeoutError):
        await engine.execute(task, _make_request())

    assert task.status == TaskStatus.FAILED
    assert "timeout" in task.error_message


async def test_execute_sets_completed_at_on_failure():
    engine, mock_provider, _ = _make_engine()
    mock_provider.complete.side_effect = ReasoningTimeoutError("timeout")

    task = _make_task()
    with pytest.raises(ReasoningTimeoutError):
        await engine.execute(task, _make_request())

    assert task.completed_at != ""


# ---------------------------------------------------------------------------
# Event emission — success
# ---------------------------------------------------------------------------


async def test_execute_emits_task_created():
    engine, mock_provider, events = _make_engine()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    await engine.execute(_make_task(), _make_request())

    emitted_types = [c.args[0].type for c in events.emit.call_args_list]
    assert EventType.TASK_CREATED in emitted_types


async def test_execute_emits_task_completed():
    engine, mock_provider, events = _make_engine()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    await engine.execute(_make_task(), _make_request())

    emitted_types = [c.args[0].type for c in events.emit.call_args_list]
    assert EventType.TASK_COMPLETED in emitted_types


async def test_task_created_event_payload():
    engine, mock_provider, events = _make_engine()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    task = _make_task()
    await engine.execute(task, _make_request())

    emitted = [c.args[0] for c in events.emit.call_args_list]
    created_event = next(e for e in emitted if e.type == EventType.TASK_CREATED)
    assert created_event.payload["task_id"] == task.id
    assert created_event.payload["task_type"] == "reactive_simple"
    assert created_event.payload["source"] == "user_message"
    assert "priority" in created_event.payload


async def test_task_completed_event_payload():
    engine, mock_provider, events = _make_engine()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    task = _make_task()
    await engine.execute(task, _make_request())

    emitted = [c.args[0] for c in events.emit.call_args_list]
    completed_event = next(e for e in emitted if e.type == EventType.TASK_COMPLETED)
    assert completed_event.payload["task_id"] == task.id
    assert "duration_ms" in completed_event.payload
    assert "input_tokens" in completed_event.payload
    assert "output_tokens" in completed_event.payload
    assert "estimated_cost_usd" in completed_event.payload


# ---------------------------------------------------------------------------
# Event emission — failure
# ---------------------------------------------------------------------------


async def test_execute_emits_task_failed():
    engine, mock_provider, events = _make_engine()
    mock_provider.complete.side_effect = ReasoningTimeoutError("timeout")

    with pytest.raises(ReasoningTimeoutError):
        await engine.execute(_make_task(), _make_request())

    emitted_types = [c.args[0].type for c in events.emit.call_args_list]
    assert EventType.TASK_FAILED in emitted_types


async def test_task_failed_event_payload():
    engine, mock_provider, events = _make_engine()
    mock_provider.complete.side_effect = ReasoningTimeoutError("timeout")

    task = _make_task()
    with pytest.raises(ReasoningTimeoutError):
        await engine.execute(task, _make_request())

    emitted = [c.args[0] for c in events.emit.call_args_list]
    failed_event = next(e for e in emitted if e.type == EventType.TASK_FAILED)
    assert failed_event.payload["task_id"] == task.id
    assert failed_event.payload["error_type"] == "ReasoningTimeoutError"
    assert "error_message" in failed_event.payload


# ---------------------------------------------------------------------------
# Best-effort event emission — failures don't break execution
# ---------------------------------------------------------------------------


async def test_event_emission_failure_does_not_break_success_path():
    """If task.created emit fails, execution still proceeds."""
    engine, mock_provider, events = _make_engine()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")
    events.emit.side_effect = RuntimeError("disk full")

    # Should not raise — event failure is best-effort
    task = await engine.execute(_make_task(), _make_request())
    assert task.status == TaskStatus.COMPLETED


async def test_event_emission_failure_does_not_suppress_reasoning_error():
    """If both event emit and reasoning fail, reasoning error is re-raised."""
    engine, mock_provider, events = _make_engine()
    mock_provider.complete.side_effect = ReasoningTimeoutError("timeout")
    events.emit.side_effect = RuntimeError("disk full")

    with pytest.raises(ReasoningTimeoutError):
        await engine.execute(_make_task(), _make_request())
