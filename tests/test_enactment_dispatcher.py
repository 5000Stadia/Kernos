"""Tests for the concrete StepDispatcher (IWL C2).

Coverage:
  - Conforms to PDI's shipped StepDispatcherLike Protocol.
  - Operation resolution via PDI C1 resolver: explicit → resolver →
    single-entry → ambiguous-fallback (refuses dispatch).
  - Per-operation timeout via asyncio.wait_for using
    OperationClassification.timeout_ms; default fallback when 0/unset.
  - tool.called / tool.result events emitted in legacy shape.
  - Trace sink populated with legacy-shape entries (single source of
    truth for drain_tool_trace).
  - Drain-ordering invariant: dispatcher only appends; never drains.
  - Failure classifications: NONE on success; CORRECTIVE_SIGNAL when
    tool returned guidance; NON_TRANSIENT on timeout / unexpected
    exception / ambiguous resolution.
  - Executor's classify_as override honored (e.g., covenant rejection).
  - StepDispatchResult fields exactly match shipped PDI shape.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from kernos.kernel.enactment.dispatcher import (
    DEFAULT_TOOL_TIMEOUT_MS,
    StepDispatcher,
    ToolDescriptorLookup,
    ToolExecutionInputs,
    ToolExecutionResult,
    ToolExecutor,
    build_step_dispatcher,
)
from kernos.kernel.enactment.plan import Step, StepExpectation
from kernos.kernel.enactment.service import (
    StepDispatchInputs,
    StepDispatchResult,
    StepDispatcherLike,
)
from kernos.kernel.enactment.tiers import FailureKind
from kernos.kernel.integration.briefing import (
    ActionEnvelope,
    AuditTrace,
    Briefing,
    ExecuteTool,
)
from kernos.kernel.tool_descriptor import (
    GateClassification,
    OperationClassification,
    OperationSafety,
    ToolDescriptor,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _step(
    *,
    step_id: str = "s1",
    tool_id: str = "email_send",
    tool_class: str = "email",
    operation_name: str = "send",
    arguments: dict | None = None,
) -> Step:
    return Step(
        step_id=step_id,
        tool_id=tool_id,
        arguments=arguments or {"to": "x@example.com"},
        tool_class=tool_class,
        operation_name=operation_name,
        expectation=StepExpectation(prose="x"),
    )


def _briefing() -> Briefing:
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=ExecuteTool(tool_id="email_send", arguments={}),
        presence_directive="execute",
        audit_trace=AuditTrace(),
        turn_id="turn-disp",
        integration_run_id="run-disp",
        action_envelope=ActionEnvelope(
            intended_outcome="send the email",
            allowed_tool_classes=("email",),
            allowed_operations=("send",),
        ),
    )


def _descriptor(
    *,
    name: str = "email_send",
    operations: tuple[OperationClassification, ...] = (
        OperationClassification(
            operation="send",
            classification=GateClassification.HARD_WRITE,
        ),
    ),
    operation_resolver=None,
) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        description="d",
        input_schema={"type": "object"},
        implementation="x.py",
        operations=operations,
        operation_resolver=operation_resolver,
    )


@dataclass
class _StubLookup:
    descriptors: dict[str, ToolDescriptor] = field(default_factory=dict)

    def descriptor_for(self, tool_id: str) -> ToolDescriptor | None:
        return self.descriptors.get(tool_id)


@dataclass
class _StubExecutor:
    result: ToolExecutionResult
    calls: list[ToolExecutionInputs] = field(default_factory=list)

    async def execute(self, inputs: ToolExecutionInputs) -> ToolExecutionResult:
        self.calls.append(inputs)
        return self.result


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_step_dispatcher_conforms_to_step_dispatcher_like_protocol():
    dispatcher = StepDispatcher(
        executor=_StubExecutor(ToolExecutionResult(output={"ok": True})),
        descriptor_lookup=_StubLookup(),
    )
    assert isinstance(dispatcher, StepDispatcherLike)


def test_factory_returns_dispatcher():
    dispatcher = build_step_dispatcher(
        executor=_StubExecutor(ToolExecutionResult(output={})),
        descriptor_lookup=_StubLookup(),
    )
    assert isinstance(dispatcher, StepDispatcher)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_completed_step_dispatch_result():
    lookup = _StubLookup({"email_send": _descriptor()})
    executor = _StubExecutor(
        ToolExecutionResult(output={"ok": True, "id": "msg-1"})
    )
    dispatcher = StepDispatcher(
        executor=executor, descriptor_lookup=lookup
    )
    result = await dispatcher.dispatch(
        StepDispatchInputs(step=_step(), briefing=_briefing())
    )
    assert isinstance(result, StepDispatchResult)
    assert result.completed is True
    assert result.failure_kind is FailureKind.NONE
    assert result.output == {"ok": True, "id": "msg-1"}
    assert result.error_summary == ""
    # Executor was invoked exactly once.
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_executor_receives_resolved_operation_name():
    """The dispatcher passes the resolved operation_name (post-PDI-C1
    resolver) into the executor inputs."""
    lookup = _StubLookup({"email_send": _descriptor()})
    executor = _StubExecutor(ToolExecutionResult(output={}))
    dispatcher = StepDispatcher(
        executor=executor, descriptor_lookup=lookup
    )
    await dispatcher.dispatch(
        StepDispatchInputs(step=_step(), briefing=_briefing())
    )
    assert executor.calls[0].operation_name == "send"


# ---------------------------------------------------------------------------
# StepDispatchResult shape — exactly the shipped six fields
# ---------------------------------------------------------------------------


def test_step_dispatch_result_shape_is_pdi_shipped_six_fields_only():
    """Acceptance criterion #4: StepDispatchResult fields are exactly
    `completed`, `output`, `failure_kind`, `error_summary`,
    `corrective_signal`, `duration_ms`. No invented fields."""
    from dataclasses import fields
    names = {f.name for f in fields(StepDispatchResult)}
    assert names == {
        "completed",
        "output",
        "failure_kind",
        "error_summary",
        "corrective_signal",
        "duration_ms",
    }


# ---------------------------------------------------------------------------
# Operation resolution: explicit / resolver / single-entry / ambiguous
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_operation_name_used_directly():
    lookup = _StubLookup({"email_send": _descriptor()})
    executor = _StubExecutor(ToolExecutionResult(output={}))
    dispatcher = StepDispatcher(
        executor=executor, descriptor_lookup=lookup
    )
    await dispatcher.dispatch(
        StepDispatchInputs(
            step=_step(operation_name="send"), briefing=_briefing()
        )
    )
    assert executor.calls[0].operation_name == "send"


@pytest.mark.asyncio
async def test_operation_resolver_derives_operation_from_args():
    def _resolve(args):
        return "send" if args.get("mode") == "send" else "draft"

    descriptor = _descriptor(
        operations=(
            OperationClassification(
                operation="send",
                classification=GateClassification.HARD_WRITE,
            ),
            OperationClassification(
                operation="draft",
                classification=GateClassification.SOFT_WRITE,
            ),
        ),
        operation_resolver=_resolve,
    )
    lookup = _StubLookup({"email_send": descriptor})
    executor = _StubExecutor(ToolExecutionResult(output={}))
    dispatcher = StepDispatcher(
        executor=executor, descriptor_lookup=lookup
    )
    # No explicit operation_name → resolver picks based on args.
    await dispatcher.dispatch(
        StepDispatchInputs(
            step=_step(operation_name="", arguments={"mode": "send"}),
            briefing=_briefing(),
        )
    )
    assert executor.calls[0].operation_name == "send"


@pytest.mark.asyncio
async def test_ambiguous_operation_refuses_dispatch():
    """Per PDI C1: ambiguous operations are NEVER dispatched.
    Conservative refusal with NON_TRANSIENT failure_kind."""
    descriptor = _descriptor(
        operations=(
            OperationClassification(
                operation="send",
                classification=GateClassification.HARD_WRITE,
            ),
            OperationClassification(
                operation="draft",
                classification=GateClassification.SOFT_WRITE,
            ),
        ),
        # No resolver, multiple operations, no explicit operation_name
        # → ambiguous.
    )
    lookup = _StubLookup({"email_send": descriptor})
    executor = _StubExecutor(ToolExecutionResult(output={}))
    dispatcher = StepDispatcher(
        executor=executor, descriptor_lookup=lookup
    )
    result = await dispatcher.dispatch(
        StepDispatchInputs(
            step=_step(operation_name=""),
            briefing=_briefing(),
        )
    )
    assert result.completed is False
    assert result.failure_kind is FailureKind.NON_TRANSIENT
    assert "ambiguous" in result.error_summary
    # Executor was NOT invoked.
    assert len(executor.calls) == 0


@pytest.mark.asyncio
async def test_unknown_tool_id_refuses_dispatch():
    lookup = _StubLookup({})  # no descriptors registered
    executor = _StubExecutor(ToolExecutionResult(output={}))
    dispatcher = StepDispatcher(
        executor=executor, descriptor_lookup=lookup
    )
    result = await dispatcher.dispatch(
        StepDispatchInputs(step=_step(tool_id="unknown"), briefing=_briefing())
    )
    assert result.completed is False
    assert result.failure_kind is FailureKind.NON_TRANSIENT
    assert "not registered" in result.error_summary
    assert len(executor.calls) == 0


# ---------------------------------------------------------------------------
# Per-operation timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_operation_timeout_enforced_via_asyncio_wait_for():
    """Acceptance criterion #9: ToolOperation.timeout_ms enforced via
    asyncio.wait_for. Engineered: timeout_ms=50, executor sleeps 200ms;
    expect timeout."""
    descriptor = _descriptor(
        operations=(
            OperationClassification(
                operation="send",
                classification=GateClassification.HARD_WRITE,
                timeout_ms=50,
            ),
        ),
    )

    class _SlowExecutor:
        async def execute(self, inputs):
            await asyncio.sleep(0.2)
            return ToolExecutionResult(output={"ok": True})

    lookup = _StubLookup({"email_send": descriptor})
    dispatcher = StepDispatcher(
        executor=_SlowExecutor(), descriptor_lookup=lookup
    )
    result = await dispatcher.dispatch(
        StepDispatchInputs(step=_step(), briefing=_briefing())
    )
    assert result.completed is False
    assert result.failure_kind is FailureKind.NON_TRANSIENT
    assert "timeout" in result.error_summary.lower()


@pytest.mark.asyncio
async def test_timeout_falls_back_to_default_when_op_timeout_zero():
    """When OperationClassification.timeout_ms is 0/unset, the
    dispatcher uses its constructor default."""
    descriptor = _descriptor(
        operations=(
            OperationClassification(
                operation="send",
                classification=GateClassification.HARD_WRITE,
                timeout_ms=0,
            ),
        ),
    )

    class _SlowExecutor:
        async def execute(self, inputs):
            await asyncio.sleep(0.05)
            return ToolExecutionResult(output={"ok": True})

    lookup = _StubLookup({"email_send": descriptor})
    # Default 30s + actual sleep 50ms → succeeds.
    dispatcher = StepDispatcher(
        executor=_SlowExecutor(), descriptor_lookup=lookup
    )
    result = await dispatcher.dispatch(
        StepDispatchInputs(step=_step(), briefing=_briefing())
    )
    assert result.completed is True


def test_default_timeout_constant_matches_documented_value():
    assert DEFAULT_TOOL_TIMEOUT_MS == 30_000


# ---------------------------------------------------------------------------
# Trace sink — single source of truth, drain-ordering invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_sink_populated_with_legacy_shape_entry():
    """Acceptance criterion #10: trace sink entry shape matches what
    legacy reasoning loop produces — name, input, success, result_preview."""
    sink: list[dict] = []
    lookup = _StubLookup({"email_send": _descriptor()})
    executor = _StubExecutor(
        ToolExecutionResult(output={"ok": True, "id": "msg-1"})
    )
    dispatcher = StepDispatcher(
        executor=executor,
        descriptor_lookup=lookup,
        trace_sink=sink,
    )
    await dispatcher.dispatch(
        StepDispatchInputs(step=_step(), briefing=_briefing())
    )
    assert len(sink) == 1
    entry = sink[0]
    assert set(entry.keys()) == {"name", "input", "success", "result_preview"}
    assert entry["name"] == "email_send"
    assert entry["success"] is True


@pytest.mark.asyncio
async def test_trace_sink_shared_with_reasoning_service_drain():
    """End-to-end pin: when ReasoningService is constructed with the
    same trace_sink list, drain_tool_trace() returns the entry the
    StepDispatcher wrote."""
    from unittest.mock import AsyncMock
    from kernos.kernel.reasoning import ReasoningService
    from kernos.providers.base import Provider

    sink: list[dict] = []
    service = ReasoningService(provider=AsyncMock(spec=Provider), trace_sink=sink)

    # Dispatcher populates the same list.
    lookup = _StubLookup({"email_send": _descriptor()})
    executor = _StubExecutor(ToolExecutionResult(output={"ok": True}))
    dispatcher = StepDispatcher(
        executor=executor,
        descriptor_lookup=lookup,
        trace_sink=sink,
    )
    await dispatcher.dispatch(
        StepDispatchInputs(step=_step(), briefing=_briefing())
    )

    # ReasoningService.drain_tool_trace returns the dispatcher's entry.
    drained = service.drain_tool_trace()
    assert len(drained) == 1
    assert drained[0]["name"] == "email_send"

    # The drain cleared the shared list.
    assert sink == []


@pytest.mark.asyncio
async def test_dispatcher_does_not_drain_or_clear_trace_sink():
    """Drain-ordering invariant (the design review final-signoff): dispatcher only
    appends. The handler owns the drain via
    ReasoningService.drain_tool_trace()."""
    sink: list[dict] = []
    lookup = _StubLookup({"email_send": _descriptor()})
    executor = _StubExecutor(ToolExecutionResult(output={"ok": True}))
    dispatcher = StepDispatcher(
        executor=executor, descriptor_lookup=lookup, trace_sink=sink
    )
    # Multiple dispatches — sink accumulates without clearing.
    for i in range(3):
        await dispatcher.dispatch(
            StepDispatchInputs(step=_step(step_id=f"s{i}"), briefing=_briefing())
        )
    # Three entries; nothing was drained mid-execution.
    assert len(sink) == 3


@pytest.mark.asyncio
async def test_trace_sink_records_failure_entry_on_timeout():
    descriptor = _descriptor(
        operations=(
            OperationClassification(
                operation="send",
                classification=GateClassification.HARD_WRITE,
                timeout_ms=50,
            ),
        ),
    )

    class _SlowExecutor:
        async def execute(self, inputs):
            await asyncio.sleep(0.2)
            return ToolExecutionResult(output={})

    sink: list[dict] = []
    dispatcher = StepDispatcher(
        executor=_SlowExecutor(),
        descriptor_lookup=_StubLookup({"email_send": descriptor}),
        trace_sink=sink,
    )
    await dispatcher.dispatch(
        StepDispatchInputs(step=_step(), briefing=_briefing())
    )
    assert len(sink) == 1
    assert sink[0]["success"] is False


# ---------------------------------------------------------------------------
# Event emission — tool.called / tool.result legacy shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_called_and_tool_result_events_emitted_in_order():
    events: list[dict] = []

    async def emit(payload):
        events.append(payload)

    lookup = _StubLookup({"email_send": _descriptor()})
    executor = _StubExecutor(ToolExecutionResult(output={"ok": True}))
    dispatcher = StepDispatcher(
        executor=executor,
        descriptor_lookup=lookup,
        event_emitter=emit,
    )
    await dispatcher.dispatch(
        StepDispatchInputs(step=_step(), briefing=_briefing())
    )
    types = [e["type"] for e in events]
    assert types == ["tool.called", "tool.result"]
    assert events[0]["tool_name"] == "email_send"
    assert events[1]["is_error"] is False


@pytest.mark.asyncio
async def test_event_emit_failure_does_not_break_dispatch():
    """Best-effort emission: if the event-stream backing call raises,
    the dispatch still completes."""

    async def broken_emit(payload):
        raise RuntimeError("event store unavailable")

    lookup = _StubLookup({"email_send": _descriptor()})
    executor = _StubExecutor(ToolExecutionResult(output={"ok": True}))
    dispatcher = StepDispatcher(
        executor=executor,
        descriptor_lookup=lookup,
        event_emitter=broken_emit,
    )
    result = await dispatcher.dispatch(
        StepDispatchInputs(step=_step(), briefing=_briefing())
    )
    assert result.completed is True


# ---------------------------------------------------------------------------
# Failure classifications
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corrective_signal_classifies_as_corrective_signal():
    lookup = _StubLookup({"email_send": _descriptor()})
    executor = _StubExecutor(
        ToolExecutionResult(
            output={},
            is_error=True,
            corrective_signal="rate-limit, batch too large",
        )
    )
    dispatcher = StepDispatcher(
        executor=executor, descriptor_lookup=lookup
    )
    result = await dispatcher.dispatch(
        StepDispatchInputs(step=_step(), briefing=_briefing())
    )
    assert result.failure_kind is FailureKind.CORRECTIVE_SIGNAL
    assert result.corrective_signal == "rate-limit, batch too large"


@pytest.mark.asyncio
async def test_classify_as_override_honored():
    """The executor may surface a richer classification than the
    dispatcher's default heuristic — e.g., covenant rejection
    classified as NON_TRANSIENT explicitly."""
    lookup = _StubLookup({"email_send": _descriptor()})
    executor = _StubExecutor(
        ToolExecutionResult(
            output={},
            is_error=True,
            error_summary="covenant blocked",
            classify_as=FailureKind.NON_TRANSIENT,
        )
    )
    dispatcher = StepDispatcher(
        executor=executor, descriptor_lookup=lookup
    )
    result = await dispatcher.dispatch(
        StepDispatchInputs(step=_step(), briefing=_briefing())
    )
    assert result.failure_kind is FailureKind.NON_TRANSIENT
    assert result.error_summary == "covenant blocked"


@pytest.mark.asyncio
async def test_unexpected_exception_classifies_as_non_transient():
    class _RaisingExecutor:
        async def execute(self, inputs):
            raise RuntimeError("network oops")

    lookup = _StubLookup({"email_send": _descriptor()})
    dispatcher = StepDispatcher(
        executor=_RaisingExecutor(), descriptor_lookup=lookup
    )
    result = await dispatcher.dispatch(
        StepDispatchInputs(step=_step(), briefing=_briefing())
    )
    assert result.completed is False
    assert result.failure_kind is FailureKind.NON_TRANSIENT
    # Error summary is redacted — only the exception type, not message.
    assert "RuntimeError" in result.error_summary
    assert "network oops" not in result.error_summary


# ---------------------------------------------------------------------------
# Duration measurement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duration_ms_populated_on_success():
    lookup = _StubLookup({"email_send": _descriptor()})
    executor = _StubExecutor(ToolExecutionResult(output={"ok": True}))
    dispatcher = StepDispatcher(
        executor=executor, descriptor_lookup=lookup
    )
    result = await dispatcher.dispatch(
        StepDispatchInputs(step=_step(), briefing=_briefing())
    )
    assert result.duration_ms >= 0
