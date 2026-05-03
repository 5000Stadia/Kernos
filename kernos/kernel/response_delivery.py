"""ProductionResponseDelivery — the response_delivery hook (IWL C5).

Conforms to PDI's response_delivery Protocol from C2 with concrete
translation logic. Owns:

  (a) accepting EnactmentOutcome
  (b) translating to ReasoningResult (live shape, not widened)
  (c) emitting synthetic `reasoning.request` / `reasoning.response`
      events at the TurnRunner boundary
  (d) bridging tool-trace records into the shared trace sink
      (handler owns the drain)

Public ReasoningResult shape (live, NOT widened by this spec):

    text, model, input_tokens, output_tokens, estimated_cost_usd,
    duration_ms, tool_iterations

Translation aggregates tokens/cost across all model calls in the
turn (integration + planner + per-step divergence + presence).
Aggregation is the single source of truth for legacy
`reasoning.response`-shape consumers.

NO-DOUBLE-COUNT INVARIANT (the design review edit, locked):

Inner hook model calls do NOT emit `reasoning.*` events. Synthetic
outer `reasoning.request` / `reasoning.response` events are emitted
ONCE per turn at the TurnRunner boundary. Test pin verifies count
of `reasoning.response` events per new-path turn equals exactly 1.

DRAIN-ORDERING INVARIANT (the design review final-signoff, locked):

`response_delivery` writes/bridges into the shared trace sink and
MUST NOT drain or clear the trace store. The handler is the single
owner of the drain via `ReasoningService.drain_tool_trace()` after
`reason()` returns.

EQUIVALENCE TELEMETRY HOOK:

Emits `turn.completed_via="decoupled"` metadata on the turn-
completion event. Legacy path emits `"legacy"` (additive change in
ReasoningService's existing emission, not part of this module).
The metadata lets equivalence-test infrastructure compute latency
overhead of new path vs legacy on representative scenarios.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from kernos.kernel.enactment.service import (
    EnactmentOutcome,
    TerminationSubtype,
)
from kernos.kernel.integration.briefing import (
    Briefing,
    ExecuteTool,
)
from kernos.kernel.reasoning import ReasoningRequest, ReasoningResult


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AggregatedTelemetry — accumulator across hook calls
# ---------------------------------------------------------------------------


@dataclass
class AggregatedTelemetry:
    """Mutable accumulator for per-hook telemetry across one turn.

    Each hook's chain wrapper increments these counters; at turn
    boundary, response_delivery reads the totals into the
    ReasoningResult.

    Construction-time invariant: shared across hooks within a single
    turn; reset at turn start by the wiring layer.

    StepDispatcher writes `tool_iterations` here after each
    successful dispatch so ProductionResponseDelivery reads the
    accurate count when constructing the ReasoningResult.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    hook_call_count: int = 0
    """Total number of model calls across hooks (for diagnostics)."""
    tool_iterations: int = 0
    """Count of step dispatches in the turn — written by StepDispatcher."""

    def add(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.estimated_cost_usd += cost_usd
        self.hook_call_count += 1

    def add_tool_iteration(self) -> None:
        """Called by StepDispatcher (or its wiring) after each
        successful dispatch. Idempotent across paths because the
        legacy reasoning loop and the new path use distinct telemetry
        instances per turn."""
        self.tool_iterations += 1


# ---------------------------------------------------------------------------
# Event emission — synthetic reasoning.* aggregator
# ---------------------------------------------------------------------------


EventEmitter = Callable[[dict[str, Any]], Awaitable[None]]
"""(payload) → None. Used for synthetic reasoning.request /
reasoning.response events. Best-effort; failures logged + swallowed."""


# ---------------------------------------------------------------------------
# Translation — live ReasoningResult shape only
# ---------------------------------------------------------------------------


def enactment_outcome_to_reasoning_result(
    outcome: EnactmentOutcome,
    *,
    request: ReasoningRequest,
    telemetry: AggregatedTelemetry,
    duration_ms: int,
    tool_iterations: int = 0,
) -> ReasoningResult:
    """Translate EnactmentOutcome into a ReasoningResult.

    Per the design review edit: targets ONLY the actual shipped fields. No
    `tool_calls`, `assistant_content`, `stop_reason`, `provider`, or
    `event_id` fields exist on ReasoningResult; richer detail flows
    through audit/trace.

    Mapping:
      - `text`: outcome.text (PresenceRenderResult.text for thin-path
        turns; full-machinery completion text for execute_tool turns).
      - `model`: request.model (the integration model — most user-
        facing decision).
      - `input_tokens` / `output_tokens` / `estimated_cost_usd`:
        aggregated sums across ALL hook model calls in the turn.
      - `duration_ms`: total wall-clock turn duration.
      - `tool_iterations`: count of step dispatches in full-machinery
        turns; 0 for thin-path turns.
    """
    return ReasoningResult(
        text=outcome.text,
        model=request.model,
        input_tokens=telemetry.input_tokens,
        output_tokens=telemetry.output_tokens,
        estimated_cost_usd=telemetry.estimated_cost_usd,
        duration_ms=duration_ms,
        tool_iterations=tool_iterations,
    )


# ---------------------------------------------------------------------------
# Synthetic event helpers
# ---------------------------------------------------------------------------


def _new_event_id() -> str:
    """Synthetic event_id for the reasoning.* emission. Same allocation
    style as legacy events so downstream tools chaining on event_id
    work identically across paths."""
    return uuid.uuid4().hex


def _step_count_from_outcome(outcome: EnactmentOutcome) -> int:
    """Approximate tool_iterations from the outcome's subtype.

    For SUCCESS_FULL_MACHINERY: the outcome itself doesn't carry the
    step count; the wiring layer counts via the dispatcher. The
    response_delivery accepts an explicit count parameter for
    accuracy. This helper is a defensive default when the wiring
    layer doesn't provide the count.
    """
    if outcome.is_thin_path:
        return 0
    return 1  # at least one dispatch fired on full-machinery completion


# ---------------------------------------------------------------------------
# ProductionResponseDelivery
# ---------------------------------------------------------------------------


class ProductionResponseDelivery:
    """Concrete `response_delivery` hook for TurnRunner.

    Lifecycle per turn:
      1. TurnRunner emits cohort fan-out → integration → enactment.
      2. EnactmentService produces an EnactmentOutcome.
      3. TurnRunner.deliver(briefing, outcome) calls this hook.
      4. This hook:
         a. Computes total turn duration.
         b. Translates outcome → ReasoningResult (live shape).
         c. Emits synthetic `reasoning.request` / `reasoning.response`
            events with `trigger="turn_runner"` for legacy
            event-stream consumers.
         d. Records `turn.completed_via="decoupled"` metadata for
            equivalence telemetry.
      5. Returns the ReasoningResult.

    Telemetry aggregation: hooks (Planner, DivergenceReasoner,
    PresenceRenderer) increment `telemetry` via their wrapper
    chain_caller. The wiring layer constructs a fresh
    AggregatedTelemetry per turn and binds it to the chain wrappers
    + this hook.

    Drain-ordering: this hook does NOT drain the tool-trace sink.
    The handler calls `ReasoningService.drain_tool_trace()` after
    `reason()` returns; the trace sink IS shared, so the handler's
    drain returns entries from BOTH legacy and new paths.

    Cost-tracking no-double-count: this hook is the SOLE emitter of
    `reasoning.request` / `reasoning.response` events on the new
    path. Inner hooks (planner, divergence_reasoner, presence_renderer)
    emit per-hook telemetry into the new audit family ONLY.
    """

    def __init__(
        self,
        *,
        request: ReasoningRequest,
        telemetry: AggregatedTelemetry,
        event_emitter: EventEmitter | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._request = request
        self._telemetry = telemetry
        self._event = event_emitter
        self._clock = clock
        self._turn_started_at: float = self._clock()
        self._tool_iterations: int = 0

    def increment_tool_iteration(self) -> None:
        """Called by the StepDispatcher (or its wrapper) after each
        tool dispatch to populate ReasoningResult.tool_iterations
        accurately. Idempotent: caller controls when to increment.

        Production wiring routes the increment through
        AggregatedTelemetry.add_tool_iteration() so a single
        per-turn telemetry instance is the source of truth. Tests
        may call this method directly on the delivery instance for
        unit-level pinning.
        """
        self._tool_iterations += 1

    @property
    def turn_started_at(self) -> float:
        return self._turn_started_at

    async def emit_request_event(self) -> None:
        """Emit the synthetic `reasoning.request` event at turn start.

        Called by the wiring layer immediately after the request is
        accepted, before any inner hooks fire. Matches the legacy
        emission shape so consumers see one request per turn on
        either path.
        """
        if self._event is None:
            return
        try:
            await self._event(
                {
                    "type": "reasoning.request",
                    "event_id": _new_event_id(),
                    "instance_id": self._request.instance_id,
                    "conversation_id": self._request.conversation_id,
                    "model": self._request.model,
                    "trigger": "turn_runner",
                    "message_count": len(self._request.messages),
                    "tool_count": len(self._request.tools),
                }
            )
        except Exception:
            logger.exception("REASONING_REQUEST_EMIT_FAILED")

    async def __call__(
        self, briefing: Briefing, outcome: EnactmentOutcome
    ) -> ReasoningResult:
        """The response_delivery Protocol entry point.

        TurnRunner calls this with the briefing + outcome at the end
        of the turn. Returns a ReasoningResult that ReasoningService
        forwards to the caller.
        """
        duration_ms = self._ms_since(self._turn_started_at)
        # Source of truth for tool_iterations:
        #   1. Explicit increments via increment_tool_iteration()
        #      (tests / direct callers).
        #   2. Telemetry's tool_iterations (production wiring — the
        #      StepDispatcher writes here per dispatch).
        #   3. Outcome-based default (defensive, when neither is
        #      populated).
        if self._tool_iterations:
            tool_iterations = self._tool_iterations
        elif self._telemetry.tool_iterations:
            tool_iterations = self._telemetry.tool_iterations
        else:
            tool_iterations = _step_count_from_outcome(outcome)

        result = enactment_outcome_to_reasoning_result(
            outcome,
            request=self._request,
            telemetry=self._telemetry,
            duration_ms=duration_ms,
            tool_iterations=tool_iterations,
        )

        # Synthetic reasoning.response — the SINGLE aggregated
        # cost/token emission for this turn.
        await self._emit_response_event(
            briefing=briefing,
            outcome=outcome,
            duration_ms=duration_ms,
            tool_iterations=tool_iterations,
        )

        return result

    async def _emit_response_event(
        self,
        *,
        briefing: Briefing,
        outcome: EnactmentOutcome,
        duration_ms: int,
        tool_iterations: int,
    ) -> None:
        if self._event is None:
            return
        try:
            await self._event(
                {
                    "type": "reasoning.response",
                    "event_id": _new_event_id(),
                    "instance_id": self._request.instance_id,
                    "conversation_id": self._request.conversation_id,
                    "model": self._request.model,
                    "input_tokens": self._telemetry.input_tokens,
                    "output_tokens": self._telemetry.output_tokens,
                    "estimated_cost_usd": (
                        self._telemetry.estimated_cost_usd
                    ),
                    "duration_ms": duration_ms,
                    "tool_iterations": tool_iterations,
                    "trigger": "turn_runner",
                    "turn_completed_via": "decoupled",
                    # Equivalence telemetry hook (acceptance criterion
                    # #26): records which path produced this turn's
                    # outcome.
                    "termination_subtype": outcome.subtype.value,
                    "decided_action_kind": outcome.decided_action_kind.value,
                }
            )
        except Exception:
            logger.exception("REASONING_RESPONSE_EMIT_FAILED")

    def _ms_since(self, start: float) -> int:
        return max(0, int((self._clock() - start) * 1000))


# ---------------------------------------------------------------------------
# Telemetry-aggregating chain caller wrapper
# ---------------------------------------------------------------------------


def wrap_chain_caller_with_telemetry(
    chain_caller, telemetry: AggregatedTelemetry
):
    """Decorate a ChainCaller to accumulate per-hook tokens + cost
    into the shared AggregatedTelemetry.

    The wiring layer wraps EACH hook's chain_caller with this
    decorator binding the SAME AggregatedTelemetry instance per
    turn. After all hooks fire, the response_delivery hook reads
    the aggregated totals into the synthetic `reasoning.response`
    event + the ReasoningResult.

    Cost-tracking note: this wrapper does NOT emit `reasoning.*`
    events (that's the no-double-count invariant). It only
    accumulates into the shared telemetry.
    """

    async def _wrapped(system, messages, tools, max_tokens, *, conversation_id=""):
        # ============================================================
        # WIRE-SHAPE PLUMBING SEAM — forward conversation_id through.
        # ============================================================
        # This wrapper sits BETWEEN PresenceRenderer and the shared chain
        # caller — exactly where a future refactor might drop a kwarg
        # "for cleanup." Don't. conversation_id reaches the Codex provider
        # via this seam to populate prompt_cache_key + session correlation
        # headers. See kernos/providers/codex_provider.py class docstring
        # "WIRE SHAPE INVARIANTS" for the full contract and failure mode.
        #
        # Empty-conversation_id carve-out: only forward when non-empty so
        # legacy/test chain_caller stubs that don't accept the kwarg
        # still work. Pin tests at
        # tests/test_thin_path_codex_wire_shape_plumbing.py enforce both
        # the forward-when-set and omit-when-empty halves.
        chain_kwargs = {}
        if conversation_id:
            chain_kwargs["conversation_id"] = conversation_id
        response = await chain_caller(
            system, messages, tools, max_tokens,
            **chain_kwargs,
        )
        # Best-effort token accumulation. Provider responses carry
        # token counts; cost is left to the wiring layer's
        # provider-specific cost calculator (out of scope for v1
        # bare wrapper).
        telemetry.add(
            input_tokens=getattr(response, "input_tokens", 0) or 0,
            output_tokens=getattr(response, "output_tokens", 0) or 0,
            cost_usd=0.0,  # cost calc lives in wiring layer
        )
        return response

    return _wrapped


__all__ = [
    "AggregatedTelemetry",
    "EventEmitter",
    "ProductionResponseDelivery",
    "enactment_outcome_to_reasoning_result",
    "wrap_chain_caller_with_telemetry",
]
