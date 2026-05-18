"""Concrete StepDispatcher implementing PDI's StepDispatcherLike (IWL C2).

Wraps the existing tool-dispatch primitive minimally — single source
of truth for dispatch behavior across legacy and new path. Operation
resolution at dispatch time uses PDI C1's `operation_resolver`
callable; per-operation timeout uses PDI C1's
`OperationClassification.timeout_ms` enforced via `asyncio.wait_for`.

Implements PDI's shipped surface:

    dispatch(inputs: StepDispatchInputs) -> StepDispatchResult

Where StepDispatchResult fields are exactly the shipped six:
`completed`, `output`, `failure_kind`, `error_summary`,
`corrective_signal`, `duration_ms`. Richer detail (per-step
audit, friction observer signals, tool-call records) lives in audit
side effects, NOT in invented return-shape fields.

Legacy tool trace/persistence preserved (acceptance criterion #10):
StepDispatcher emits `tool.called` and `tool.result` events,
logs to the audit store, and populates the same trace store
ReasoningService's `drain_tool_trace()` reads from. Cost tracking,
telemetry, audit replay continue identically across paths.

Architecture: tool executor is dependency-injected via the
`ToolExecutor` Protocol. The production wiring (IWL C5) provides
an executor that bridges to the workshop dispatch primitive in
ReasoningService. Tests inject stubs for unit-level coverage.

Trace sink: an injectable `list[dict]` shared with ReasoningService.
StepDispatcher appends per-call entries in the legacy shape
(`{name, input, success, result_preview}`); the handler drains via
`ReasoningService.drain_tool_trace()` after `reason()` returns.
**Drain ordering invariant:** StepDispatcher appends only — never
drains, never clears. The handler owns the drain.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol, runtime_checkable

from kernos.kernel.enactment.service import (
    StepDispatchInputs,
    StepDispatchResult,
)
from kernos.kernel.enactment.tiers import FailureKind
from kernos.kernel.tool_descriptor import OperationClassification, ToolDescriptor
from kernos.kernel.tools.operation_resolver import (
    OperationResolution,
    resolve_operation,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool executor + descriptor lookup Protocols
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolExecutionInputs:
    """What the production tool executor receives."""

    tool_id: str
    arguments: dict[str, Any]
    operation_name: str
    instance_id: str
    member_id: str
    space_id: str
    turn_id: str


@dataclass(frozen=True)
class ToolExecutionResult:
    """What the production tool executor returns.

    Mirrors the shape of the legacy tool-dispatch primitive's output
    so the dispatcher can translate uniformly. `is_error` distinguishes
    successful tool returns from error returns; `corrective_signal`
    is populated when the tool returned guidance (e.g. rate-limit
    advice the model can act on for tier-2 modify).
    """

    output: dict[str, Any]
    is_error: bool = False
    corrective_signal: str = ""
    error_summary: str = ""
    classify_as: FailureKind | None = None
    """When set, overrides the dispatcher's default failure
    classification. Used by the production executor to surface
    workshop-level signals like covenant rejection or
    sensitive-action fallback."""


@runtime_checkable
class ToolExecutor(Protocol):
    """Executes a single tool invocation. Production wiring binds to
    the workshop dispatch primitive in ReasoningService; tests pass
    stubs that return canned ToolExecutionResults.

    The executor is responsible for:
      - actual tool invocation (workshop dispatch),
      - gate cache + per-operation safety checks,
      - covenant enforcement,
      - any per-tool authentication / credential resolution.

    StepDispatcher composes the executor with operation resolution,
    per-op timeout, trace-sink writes, and event emission.
    """

    async def execute(
        self, inputs: ToolExecutionInputs
    ) -> ToolExecutionResult: ...


@runtime_checkable
class ToolDescriptorLookup(Protocol):
    """Resolves a tool_id to its ToolDescriptor for operation-resolution
    + per-operation timeout lookup. Production wiring binds to the
    workshop registry; tests pass dict-backed stubs.

    Optional method ``known_tool_ids()`` returns the set of currently
    registered tool names. Used by the dispatcher's tool_not_registered
    path to populate ``corrective_signal`` with closest-match
    suggestions — preventing the namespace-hallucination retry loops
    where the model tries ``code_execution.execute_python`` instead
    of ``execute_code``. Stubs that don't implement it skip the
    suggestion silently.
    """

    def descriptor_for(self, tool_id: str) -> ToolDescriptor | None: ...


# ---------------------------------------------------------------------------
# Trace sink
# ---------------------------------------------------------------------------


# The trace sink is a list of trace entries shared with ReasoningService
# so the handler's `drain_tool_trace()` consumption works identically
# across paths. Per-entry shape mirrors the legacy
# `_turn_tool_trace.append({...})` site in reasoning.py.
TraceSink = list[dict[str, Any]]


def _build_trace_entry(
    *,
    tool_id: str,
    arguments: dict[str, Any],
    is_error: bool,
    output: Any,
) -> dict[str, Any]:
    """Build a trace entry matching legacy shape. References-not-dumps:
    output is truncated to the same 200-char preview legacy uses."""
    if isinstance(output, str):
        preview = output[:200]
    else:
        preview = str(output)[:200]
    return {
        "name": tool_id,
        "input": dict(arguments),
        "success": not is_error,
        "result_preview": preview,
    }


# ---------------------------------------------------------------------------
# Audit + event emission Protocols
# ---------------------------------------------------------------------------


AuditEmitter = Callable[[dict[str, Any]], Awaitable[None]]
"""(audit_entry) → None. Dispatcher emits tool.called / tool.result here."""

EventEmitter = Callable[[dict[str, Any]], Awaitable[None]]
"""(event_payload) → None. Dispatcher emits tool.called / tool.result events
into the event stream — same shape legacy reasoning loop produces."""


# ---------------------------------------------------------------------------
# StepDispatcher
# ---------------------------------------------------------------------------


# Default per-tool timeout when the descriptor doesn't specify one.
# Generous default — tools that need tighter control declare per-op
# timeout via OperationClassification.timeout_ms.
DEFAULT_TOOL_TIMEOUT_MS = 30_000


class StepDispatcher:
    """Concrete StepDispatcher conforming to PDI's StepDispatcherLike.

    Per-step lifecycle:
      1. Resolve operation via PDI C1's resolver against descriptor.
      2. Look up per-op timeout from OperationClassification.timeout_ms;
         fall back to default if 0/unset.
      3. Emit `tool.called` event matching legacy shape.
      4. Invoke tool via injected ToolExecutor under
         asyncio.wait_for(timeout).
      5. On clean return: emit `tool.result` event; append trace entry
         to the shared trace sink; return StepDispatchResult.
      6. On timeout: classify failure_kind=NON_TRANSIENT (timeout
         is a definite failure, not retry-eligible by default); emit
         tool.result with timeout marker; append trace entry; return.
      7. On unexpected exception: classify failure_kind=NON_TRANSIENT;
         emit tool.result with redacted error_summary.

    Operation ambiguity (PDI C1 contract): when resolution returns
    `ambiguous=True`, the dispatcher refuses to invoke the tool and
    returns failure_kind=NON_TRANSIENT with a clear error_summary.
    Ambiguous operations are NEVER dispatched — the conservative
    fallback fires structurally.
    """

    def __init__(
        self,
        *,
        executor: ToolExecutor,
        descriptor_lookup: ToolDescriptorLookup,
        trace_sink: TraceSink | None = None,
        event_emitter: EventEmitter | None = None,
        audit_emitter: AuditEmitter | None = None,
        default_timeout_ms: int = DEFAULT_TOOL_TIMEOUT_MS,
        clock: Callable[[], float] = time.monotonic,
        on_dispatch_complete: Callable[[], None] | None = None,
    ) -> None:
        self._executor = executor
        self._lookup = descriptor_lookup
        # Shared trace sink (acceptance criterion #10). Default is a
        # private list when not injected so unit tests don't need to
        # supply one.
        self._trace_sink: TraceSink = (
            trace_sink if trace_sink is not None else []
        )
        self._event = event_emitter
        self._audit = audit_emitter
        self._default_timeout_ms = default_timeout_ms
        self._clock = clock
        # Per-turn callback fired after each dispatch attempt (success
        # or failure). Production wiring binds this to
        # AggregatedTelemetry.add_tool_iteration so the ProductionResponseDelivery
        # reads accurate tool_iterations when constructing the
        # ReasoningResult. Tests may pass None.
        self._on_dispatch_complete = on_dispatch_complete

    @property
    def trace_sink(self) -> TraceSink:
        """Expose the trace sink reference for inspection.

        Drain ordering invariant: callers must NOT clear or pop from
        this list. The handler owns the drain via
        ReasoningService.drain_tool_trace().
        """
        return self._trace_sink

    async def dispatch(
        self, inputs: StepDispatchInputs
    ) -> StepDispatchResult:
        step = inputs.step
        briefing = inputs.briefing
        start = self._clock()

        descriptor = self._lookup.descriptor_for(step.tool_id)

        # Operation resolution + ambiguity check.
        if descriptor is None:
            # Tool unknown — conservative failure; never dispatch.
            duration = self._ms_since(start)
            await self._emit_tool_called(briefing, step)
            await self._emit_tool_result_failure(
                briefing, step, "tool_not_registered", duration_ms=duration,
            )
            await self._emit_audit_entry(
                briefing=briefing, step=step, completed=False,
                duration_ms=duration, failure_label="tool_not_registered",
            )
            self._trace_sink.append(
                _build_trace_entry(
                    tool_id=step.tool_id,
                    arguments=step.arguments,
                    is_error=True,
                    output=f"tool {step.tool_id!r} not registered",
                )
            )
            self._fire_on_dispatch_complete()
            # CORRECTIVE-SIGNAL-CLOSEST-MATCH (2026-05-17): when the
            # lookup exposes known_tool_ids(), surface 1–3 closest
            # matches so the model can self-correct on retry rather
            # than burning more turns inventing namespace variants.
            # Pattern observed twice: model emitted
            # `code_execution.execute_python` and
            # `external_coding_agent_consult.consult` instead of the
            # flat registered names `execute_code` and `consult`.
            # Stubs that don't implement known_tool_ids() fall back
            # to no-suggestion (empty corrective_signal).
            _corrective = ""
            try:
                _known_fn = getattr(self._lookup, "known_tool_ids", None)
                if callable(_known_fn):
                    import difflib
                    _known = list(_known_fn() or [])
                    _matches = difflib.get_close_matches(
                        step.tool_id, _known, n=3, cutoff=0.4,
                    )
                    if _matches:
                        _corrective = (
                            f"tool {step.tool_id!r} is not registered. "
                            f"Did you mean: {', '.join(_matches)}?"
                        )
            except Exception:
                # Suggestion is best-effort — never let it block
                # the failure return.
                pass
            return StepDispatchResult(
                completed=False,
                output={},
                failure_kind=FailureKind.NON_TRANSIENT,
                error_summary=(
                    f"tool {step.tool_id!r} is not registered with "
                    f"the workshop"
                ),
                corrective_signal=_corrective,
                duration_ms=duration,
            )

        resolution = resolve_operation(
            descriptor,
            explicit_operation=step.operation_name or None,
            arguments=step.arguments,
        )
        if resolution.ambiguous:
            # Ambiguous operations are NEVER dispatched (PDI C1
            # contract). Conservative refusal with clear error.
            duration = self._ms_since(start)
            await self._emit_tool_called(briefing, step)
            await self._emit_tool_result_failure(
                briefing, step, "operation_ambiguous", duration_ms=duration,
            )
            await self._emit_audit_entry(
                briefing=briefing, step=step, completed=False,
                duration_ms=duration, failure_label="operation_ambiguous",
            )
            self._trace_sink.append(
                _build_trace_entry(
                    tool_id=step.tool_id,
                    arguments=step.arguments,
                    is_error=True,
                    output=(
                        f"tool {step.tool_id!r} operation ambiguous; "
                        f"refusing dispatch"
                    ),
                )
            )
            self._fire_on_dispatch_complete()
            return StepDispatchResult(
                completed=False,
                output={},
                failure_kind=FailureKind.NON_TRANSIENT,
                error_summary=(
                    f"operation resolution ambiguous for tool "
                    f"{step.tool_id!r}; refusing dispatch (PDI C1 "
                    f"conservative fallback)"
                ),
                duration_ms=duration,
            )

        # Per-operation timeout.
        timeout_ms = self._timeout_ms_for(descriptor, resolution)

        await self._emit_tool_called(briefing, step)

        executor_inputs = ToolExecutionInputs(
            tool_id=step.tool_id,
            arguments=dict(step.arguments),
            operation_name=resolution.operation_name or step.operation_name,
            instance_id=getattr(briefing, "instance_id", "")
            or getattr(briefing.audit_trace, "instance_id", "")
            or "",
            member_id=getattr(briefing, "member_id", ""),
            space_id=getattr(briefing, "space_id", ""),
            turn_id=briefing.turn_id,
        )

        try:
            result = await asyncio.wait_for(
                self._executor.execute(executor_inputs),
                timeout=timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            duration = self._ms_since(start)
            await self._emit_tool_result_failure(
                briefing, step, "timeout", duration_ms=duration
            )
            await self._emit_audit_entry(
                briefing=briefing, step=step, completed=False,
                duration_ms=duration, failure_label="timeout",
            )
            self._trace_sink.append(
                _build_trace_entry(
                    tool_id=step.tool_id,
                    arguments=step.arguments,
                    is_error=True,
                    output=f"tool {step.tool_id!r} timed out after {timeout_ms}ms",
                )
            )
            self._fire_on_dispatch_complete()
            return StepDispatchResult(
                completed=False,
                output={},
                failure_kind=FailureKind.NON_TRANSIENT,
                error_summary=(
                    f"tool {step.tool_id!r} exceeded {timeout_ms}ms timeout"
                ),
                duration_ms=duration,
            )
        except Exception as exc:
            duration = self._ms_since(start)
            redacted = type(exc).__name__
            await self._emit_tool_result_failure(
                briefing, step, redacted, duration_ms=duration
            )
            await self._emit_audit_entry(
                briefing=briefing, step=step, completed=False,
                duration_ms=duration, failure_label=redacted,
            )
            self._trace_sink.append(
                _build_trace_entry(
                    tool_id=step.tool_id,
                    arguments=step.arguments,
                    is_error=True,
                    output=f"tool {step.tool_id!r} raised {redacted}",
                )
            )
            self._fire_on_dispatch_complete()
            logger.exception("STEP_DISPATCH_UNEXPECTED_FAILURE tool=%s", step.tool_id)
            return StepDispatchResult(
                completed=False,
                output={},
                failure_kind=FailureKind.NON_TRANSIENT,
                error_summary=f"tool raised {redacted}",
                duration_ms=duration,
            )

        duration_ms = self._ms_since(start)

        # Classify failure kind.
        failure_kind = self._classify_failure(result)

        await self._emit_tool_result_success(
            briefing, step, result=result, duration_ms=duration_ms
        )
        await self._emit_audit_entry(
            briefing=briefing, step=step,
            completed=not result.is_error,
            duration_ms=duration_ms,
            failure_label=result.error_summary if result.is_error else "",
        )
        self._trace_sink.append(
            _build_trace_entry(
                tool_id=step.tool_id,
                arguments=step.arguments,
                is_error=result.is_error,
                output=result.output,
            )
        )
        self._fire_on_dispatch_complete()

        return StepDispatchResult(
            completed=not result.is_error,
            output=dict(result.output),
            failure_kind=failure_kind,
            error_summary=result.error_summary,
            corrective_signal=result.corrective_signal,
            duration_ms=duration_ms,
        )

    # ----- helpers -----

    def _timeout_ms_for(
        self,
        descriptor: ToolDescriptor,
        resolution: OperationResolution,
    ) -> int:
        """Look up per-operation timeout via PDI C1's
        OperationClassification.timeout_ms. 0 / unset → default."""
        op_name = resolution.operation_name
        if op_name:
            op = descriptor.operation_for(op_name)
            if op is not None and op.timeout_ms > 0:
                return op.timeout_ms
        return self._default_timeout_ms

    def _classify_failure(
        self, result: ToolExecutionResult
    ) -> FailureKind:
        """Translate a ToolExecutionResult into a FailureKind for the
        StepDispatchResult. Honors the executor's `classify_as`
        override when it set one (e.g., for covenant rejection or
        ambiguous-fallback paths the executor knows about)."""
        if result.classify_as is not None:
            return result.classify_as
        if not result.is_error:
            return FailureKind.NONE
        if result.corrective_signal:
            return FailureKind.CORRECTIVE_SIGNAL
        return FailureKind.NON_TRANSIENT

    def _ms_since(self, start: float) -> int:
        return max(0, int((self._clock() - start) * 1000))

    # ----- event + audit emission -----

    async def _emit_tool_called(
        self, briefing, step
    ) -> None:
        """Emit `tool.called` event matching legacy shape so cost
        tracking + telemetry consumers work unchanged."""
        if self._event is None:
            return
        try:
            await self._event(
                {
                    "type": "tool.called",
                    "instance_id": getattr(briefing, "instance_id", ""),
                    "tool_name": step.tool_id,
                    "tool_input_shape": list(step.arguments.keys()),
                    "turn_id": briefing.turn_id,
                }
            )
        except Exception:
            logger.exception("TOOL_CALLED_EMIT_FAILED tool=%s", step.tool_id)

    async def _emit_tool_result_success(
        self, briefing, step, *, result: ToolExecutionResult, duration_ms: int
    ) -> None:
        if self._event is None:
            return
        try:
            await self._event(
                {
                    "type": "tool.result",
                    "instance_id": getattr(briefing, "instance_id", ""),
                    "tool_name": step.tool_id,
                    "is_error": result.is_error,
                    "duration_ms": duration_ms,
                    "turn_id": briefing.turn_id,
                }
            )
        except Exception:
            logger.exception("TOOL_RESULT_EMIT_FAILED tool=%s", step.tool_id)

    async def _emit_tool_result_failure(
        self, briefing, step, error_label: str, *, duration_ms: int
    ) -> None:
        if self._event is None:
            return
        try:
            await self._event(
                {
                    "type": "tool.result",
                    "instance_id": getattr(briefing, "instance_id", ""),
                    "tool_name": step.tool_id,
                    "is_error": True,
                    "error_label": error_label,
                    "duration_ms": duration_ms,
                    "turn_id": briefing.turn_id,
                }
            )
        except Exception:
            logger.exception(
                "TOOL_RESULT_EMIT_FAILED tool=%s", step.tool_id
            )

    async def _emit_audit_entry(
        self, *, briefing, step, completed: bool, duration_ms: int,
        failure_label: str = "",
    ) -> None:
        """Emit a per-dispatch audit entry. The audit shape mirrors
        the legacy reasoning loop's audit-store behavior so audit
        replay / cost tracking work identically across paths.

        References-not-dumps invariant: tool_id + operation_name +
        turn_id are operator-readable references; argument values and
        output payloads are NOT embedded. `instance_id` is included
        so the audit-store adapter (which routes by instance) can
        partition correctly without re-deriving from the briefing.
        """
        if self._audit is None:
            return
        try:
            await self._audit(
                {
                    "category": "tool.dispatch",
                    "instance_id": _instance_id_from_briefing(briefing),
                    "turn_id": briefing.turn_id,
                    "tool_id": step.tool_id,
                    "operation_name": step.operation_name,
                    "tool_class": step.tool_class,
                    "completed": completed,
                    "duration_ms": duration_ms,
                    "failure_label": failure_label,
                }
            )
        except Exception:
            logger.exception(
                "TOOL_DISPATCH_AUDIT_EMIT_FAILED tool=%s", step.tool_id
            )

    def _fire_on_dispatch_complete(self) -> None:
        """Invoke the per-turn dispatch-complete callback (production
        wiring binds this to AggregatedTelemetry.add_tool_iteration).
        Best-effort — a misbehaving callback doesn't break dispatch."""
        if self._on_dispatch_complete is None:
            return
        try:
            self._on_dispatch_complete()
        except Exception:
            logger.exception("ON_DISPATCH_COMPLETE_FAILED")


def build_step_dispatcher(
    *,
    executor: ToolExecutor,
    descriptor_lookup: ToolDescriptorLookup,
    trace_sink: TraceSink | None = None,
    event_emitter: EventEmitter | None = None,
    audit_emitter: AuditEmitter | None = None,
    default_timeout_ms: int = DEFAULT_TOOL_TIMEOUT_MS,
    on_dispatch_complete: Callable[[], None] | None = None,
) -> StepDispatcher:
    """Convenience factory mirroring the constructor."""
    return StepDispatcher(
        executor=executor,
        descriptor_lookup=descriptor_lookup,
        trace_sink=trace_sink,
        event_emitter=event_emitter,
        audit_emitter=audit_emitter,
        default_timeout_ms=default_timeout_ms,
        on_dispatch_complete=on_dispatch_complete,
    )


def _instance_id_from_briefing(briefing) -> str:
    """Best-effort lookup of instance_id from a Briefing.

    The Briefing dataclass doesn't carry instance_id directly; it's
    threaded via the audit_trace's references in V1. We attempt the
    common attribute paths and fall back to "" so the audit emitter
    can partition by empty bucket if upstream wiring didn't set it.
    """
    return (
        getattr(briefing, "instance_id", "")
        or getattr(briefing.audit_trace, "instance_id", "")
        or ""
    )


__all__ = [
    "AuditEmitter",
    "DEFAULT_TOOL_TIMEOUT_MS",
    "EventEmitter",
    "StepDispatcher",
    "ToolDescriptorLookup",
    "ToolExecutionInputs",
    "ToolExecutionResult",
    "ToolExecutor",
    "TraceSink",
    "build_step_dispatcher",
]
