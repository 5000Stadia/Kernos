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
import dataclasses
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


def _briefing(
    *,
    user_message: str = "",
    recent_messages: tuple[dict, ...] = (),
    narration_context: str = "",
) -> Briefing:
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=ExecuteTool(
            tool_id="email_send",
            arguments={},
            narration_context=narration_context,
        ),
        presence_directive="execute",
        audit_trace=AuditTrace(),
        turn_id="turn-disp",
        integration_run_id="run-disp",
        action_envelope=ActionEnvelope(
            intended_outcome="send the email",
            allowed_tool_classes=("email",),
            allowed_operations=("send",),
        ),
        user_message=user_message,
        recent_messages=recent_messages,
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


@pytest.mark.asyncio
async def test_executor_receives_gate_authorization_context_from_briefing():
    """Hard-write gate evaluation needs the original user request and
    agent reasoning to see that the action is user-authorized."""
    lookup = _StubLookup({"email_send": _descriptor()})
    executor = _StubExecutor(ToolExecutionResult(output={}))
    dispatcher = StepDispatcher(
        executor=executor, descriptor_lookup=lookup
    )
    briefing = _briefing(
        user_message="Send the email now.",
        recent_messages=({"role": "user", "content": "draft it first"},),
        narration_context="The user explicitly authorized the send.",
    )

    await dispatcher.dispatch(
        StepDispatchInputs(step=_step(), briefing=briefing)
    )

    sent = executor.calls[0]
    assert sent.user_message == "Send the email now."
    assert sent.recent_messages == (
        {"role": "user", "content": "draft it first"},
    )
    assert "explicitly authorized" in sent.agent_reasoning
    assert "Presence directive: execute" in sent.agent_reasoning


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
# CORRECTIVE-SIGNAL-CLOSEST-MATCH-V1 pins
# ---------------------------------------------------------------------------


@dataclass
class _StubLookupWithKnown:
    """Extends _StubLookup with the optional known_tool_ids() method
    that the dispatcher uses to surface closest-match suggestions
    on tool_not_registered."""
    descriptors: dict[str, ToolDescriptor] = field(default_factory=dict)
    known: set[str] = field(default_factory=set)

    def descriptor_for(self, tool_id: str) -> ToolDescriptor | None:
        return self.descriptors.get(tool_id)

    def known_tool_ids(self) -> set[str]:
        return self.known


@pytest.mark.asyncio
async def test_unknown_tool_id_surfaces_closest_match_in_corrective_signal():
    """Pin: when the model emits a hallucinated namespaced tool name
    like `code_execution.execute_python` and the real registered tool
    is `execute_code`, the dispatcher's failure result should populate
    corrective_signal with the closest match so the model self-
    corrects on retry instead of inventing more namespace variants."""
    lookup = _StubLookupWithKnown(
        descriptors={},
        known={
            "execute_code", "consult", "ask_coding_session",
            "read_coding_session_response", "diagnose_issue",
            "remember", "remember_details",
        },
    )
    dispatcher = StepDispatcher(
        executor=_StubExecutor(ToolExecutionResult(output={})),
        descriptor_lookup=lookup,
    )
    result = await dispatcher.dispatch(
        StepDispatchInputs(
            step=_step(tool_id="code_execution.execute_python"),
            briefing=_briefing(),
        )
    )
    assert result.completed is False
    assert "execute_code" in result.corrective_signal, (
        f"expected corrective_signal to suggest 'execute_code'; "
        f"got: {result.corrective_signal!r}"
    )
    assert result.corrective_signal.startswith("tool ")


@pytest.mark.asyncio
async def test_consult_namespace_hallucination_suggests_consult():
    """Pin: the other observed hallucination —
    `external_coding_agent_consult.consult` should surface `consult`
    in the corrective_signal."""
    lookup = _StubLookupWithKnown(
        descriptors={},
        known={
            "consult", "ask_coding_session",
            "read_coding_session_response", "execute_code",
        },
    )
    dispatcher = StepDispatcher(
        executor=_StubExecutor(ToolExecutionResult(output={})),
        descriptor_lookup=lookup,
    )
    result = await dispatcher.dispatch(
        StepDispatchInputs(
            step=_step(tool_id="external_coding_agent_consult.consult"),
            briefing=_briefing(),
        )
    )
    assert "consult" in result.corrective_signal


@pytest.mark.asyncio
async def test_lookup_without_known_tool_ids_falls_back_silently():
    """Pin: lookups (like _StubLookup) that don't implement
    known_tool_ids() should produce an empty corrective_signal
    without raising — best-effort suggestion never blocks failure
    return."""
    lookup = _StubLookup({})  # no known_tool_ids() method
    dispatcher = StepDispatcher(
        executor=_StubExecutor(ToolExecutionResult(output={})),
        descriptor_lookup=lookup,
    )
    result = await dispatcher.dispatch(
        StepDispatchInputs(
            step=_step(tool_id="whatever"),
            briefing=_briefing(),
        )
    )
    assert result.completed is False
    assert result.corrective_signal == ""


@pytest.mark.asyncio
async def test_no_close_match_leaves_corrective_signal_empty():
    """Pin: when no registered tool name is close to the requested
    one (cutoff=0.4), corrective_signal stays empty — don't surface
    misleading suggestions."""
    lookup = _StubLookupWithKnown(
        descriptors={},
        known={"alpha", "beta", "gamma"},
    )
    dispatcher = StepDispatcher(
        executor=_StubExecutor(ToolExecutionResult(output={})),
        descriptor_lookup=lookup,
    )
    result = await dispatcher.dispatch(
        StepDispatchInputs(
            step=_step(tool_id="totally_unrelated_xyz"),
            briefing=_briefing(),
        )
    )
    assert result.completed is False
    assert result.corrective_signal == ""


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


# ---------------------------------------------------------------------------
# Alias-repair at enactment dispatcher (2026-05-25)
#
# Verification B of the SELF-IMPROVEMENT-CLOSURE alignment soak observed
# advisory_spec_retrieval_consult failing as "not registered with the
# workshop" — the prior alias-repair wirings at reasoning.execute_tool
# and gate.classify_tool_effect don't cover this surface. The enactment
# dispatcher is a third ingress that must consult the alias dict before
# descriptor lookup.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_known_alias_canonicalized_before_descriptor_lookup(
    monkeypatch,
):
    """When the requested tool_id matches a known alias, the dispatcher
    canonicalizes it BEFORE descriptor_for() is called. The lookup
    therefore receives the canonical name and finds the descriptor."""
    # Use an existing real alias from the shipped dict to avoid
    # coupling this test to internal alias-dict mutations.
    from kernos.kernel.tool_aliases import canonicalize_tool_name
    alias, canonical = "advisory_spec_retrieval_consult", "consult"
    # Sanity: the alias entry is present in the production dict.
    repaired_name, was_repaired = canonicalize_tool_name(alias)
    assert was_repaired is True
    assert repaired_name == canonical

    lookup = _StubLookup({canonical: _descriptor(
        name=canonical,
        operations=(
            OperationClassification(
                operation="consult",
                classification=GateClassification.SOFT_WRITE,
            ),
        ),
    )})
    executor = _StubExecutor(ToolExecutionResult(output={"ok": True}))
    dispatcher = StepDispatcher(
        executor=executor, descriptor_lookup=lookup,
    )
    briefing = _briefing()
    # The action envelope only allows "email"; widen it for this test.
    briefing = dataclasses.replace(
        briefing,
        action_envelope=ActionEnvelope(
            intended_outcome="advisory consult",
            allowed_tool_classes=("consult",),
            allowed_operations=("consult",),
        ),
    )

    result = await dispatcher.dispatch(
        StepDispatchInputs(
            step=_step(
                tool_id=alias, tool_class="consult",
                operation_name="consult", arguments={},
            ),
            briefing=briefing,
        )
    )
    # Repair landed: the dispatcher reached the executor through the
    # canonical name (no "tool not registered" failure).
    assert result.completed is True
    assert result.error_summary == ""
    # Executor saw the canonical name, not the alias.
    assert len(executor.calls) == 1
    assert executor.calls[0].tool_id == canonical


@pytest.mark.asyncio
async def test_alias_repair_emits_receipt_event():
    """Per TOOL-ALIAS-RECEIPT-V1: every alias canonicalization emits a
    tool.alias_repaired event with requested/canonical/context fields.
    The enactment ingress uses context='enactment'."""
    captured: list[dict] = []

    async def _capture_event(payload: dict) -> None:
        captured.append(payload)

    lookup = _StubLookup({"consult": _descriptor(
        name="consult",
        operations=(
            OperationClassification(
                operation="consult",
                classification=GateClassification.SOFT_WRITE,
            ),
        ),
    )})
    executor = _StubExecutor(ToolExecutionResult(output={}))
    dispatcher = StepDispatcher(
        executor=executor, descriptor_lookup=lookup,
        event_emitter=_capture_event,
    )
    briefing = dataclasses.replace(
        _briefing(),
        action_envelope=ActionEnvelope(
            intended_outcome="x",
            allowed_tool_classes=("consult",),
            allowed_operations=("consult",),
        ),
    )

    await dispatcher.dispatch(
        StepDispatchInputs(
            step=_step(
                tool_id="advisory_spec_retrieval_consult",
                tool_class="consult",
                operation_name="consult",
                arguments={},
            ),
            briefing=briefing,
        )
    )

    repair_events = [
        e for e in captured if e.get("type") == "tool.alias_repaired"
    ]
    assert len(repair_events) == 1
    evt = repair_events[0]
    assert evt["requested"] == "advisory_spec_retrieval_consult"
    assert evt["canonical"] == "consult"
    assert evt["context"] == "enactment"


@pytest.mark.asyncio
async def test_canonical_name_passes_through_unchanged():
    """When the tool_id is already canonical (not in the alias dict),
    the dispatcher does NOT emit a repair event and the executor sees
    the same name the step carried in."""
    captured: list[dict] = []

    async def _capture_event(payload: dict) -> None:
        captured.append(payload)

    lookup = _StubLookup({"email_send": _descriptor()})
    executor = _StubExecutor(ToolExecutionResult(output={}))
    dispatcher = StepDispatcher(
        executor=executor, descriptor_lookup=lookup,
        event_emitter=_capture_event,
    )
    await dispatcher.dispatch(
        StepDispatchInputs(step=_step(), briefing=_briefing())
    )
    # No alias_repaired events for canonical names.
    repair_events = [
        e for e in captured if e.get("type") == "tool.alias_repaired"
    ]
    assert repair_events == []
    assert executor.calls[0].tool_id == "email_send"


@pytest.mark.asyncio
async def test_unknown_non_alias_tool_still_returns_not_registered():
    """An unknown tool_id that is NOT in the alias dict still fails
    as 'not registered with the workshop' — alias-repair doesn't
    fabricate descriptors for unknown names."""
    lookup = _StubLookup({})  # no descriptors at all
    executor = _StubExecutor(ToolExecutionResult(output={}))
    dispatcher = StepDispatcher(
        executor=executor, descriptor_lookup=lookup,
    )
    result = await dispatcher.dispatch(
        StepDispatchInputs(
            step=_step(tool_id="some_truly_unknown_tool_42"),
            briefing=_briefing(),
        )
    )
    assert result.completed is False
    assert result.failure_kind is FailureKind.NON_TRANSIENT
    assert "not registered" in result.error_summary
