"""Shared turn-runner-provider construction.

Canonical construction contract for ``ReasoningService``'s
``turn_runner_provider``. Every entry point that constructs
``ReasoningService`` (server.py, repl.py, app.py, chat.py,
evals/bootstrap.py) wires the per-turn factory through this module
so the construction shape stays consistent across all entry points.

Per Kit's framing (2026-05-03): this is the fourth independent
instance of "canonical source + derived consumers + parity pins"
in the recent architecture work — in this case, ReasoningService
construction shape is the canonical contract; every callsite
derives via the shared helper; the pin test at
``tests/test_reasoning_service_construction_parity.py`` asserts
no callsite re-creates the closure pattern by hand. Copy-paste IS
the failure mode the principle exists to prevent.

Why per-turn binding (not a static TurnRunner):
``AggregatedTelemetry`` must be fresh per turn so token aggregation
+ tool_iterations accumulate correctly; ``ProductionResponseDelivery``
captures the request so synthetic events carry the right
identifiers; chain wrappers bind the same telemetry across all
hooks so cost-tracking aggregates ONCE.

Why a mutable context (not a frozen dataclass): the live-thin-path
wiring happens AFTER the closure is already passed to
``ReasoningService`` (LiveExecutor/LiveIntegrationDispatcher need
``reasoning.execute_tool``, which doesn't exist until reasoning is
constructed). The closure reads context fields at call time via
late binding — mutating the context with live components after
``reasoning`` is built propagates to subsequent per-turn calls.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class ThinPathContext:
    """Mutable container for thin-path turn-runner-provider components.

    Lifecycle:

      1. Launcher constructs an empty ``ThinPathContext``.
      2. Launcher sets the early-bound fields (``chain_caller``,
         ``cohort_runner``, the dispatcher / audit / integration
         emitters) before constructing ``ReasoningService``.
      3. Launcher calls ``build_turn_runner_provider(ctx)`` and passes
         the returned closure to ``ReasoningService(turn_runner_provider=...)``.
      4. After ``MessageHandler`` is constructed, launcher calls
         ``wire_live_thin_path(ctx, reasoning=..., handler=...)`` to
         swap the unwired stubs for the production ``LiveExecutor`` /
         ``LiveDescriptorLookup`` / ``LiveIntegrationDispatcher`` /
         ``LivePlannerCatalog``.

    The closure reads fields at call time (per turn), so mutations
    after step 3 propagate transparently. This preserves the late-
    binding semantics of the original in-launcher closures without
    requiring each launcher to re-create them.
    """

    # Set in step 2 (early-bound):
    chain_caller: Any = None
    cohort_runner: Any = None
    dispatcher_event_emitter: Any = None
    dispatcher_audit_emitter: Any = None
    integration_audit_emitter: Any = None
    trace_sink: list = dataclasses.field(default_factory=list)

    # Set in step 4 (late-bound to live components):
    executor: Any = None
    descriptor_lookup: Any = None
    integration_dispatcher: Any = None
    planner_tool_catalog: Any = None


@dataclasses.dataclass(frozen=True)
class _RendererTurnInputs:
    """Per-turn renderer inputs carrying the briefing identifiers.

    Fold 6 (Batch 2 Codex review): identifiers must thread through
    the renderer's tool-dispatcher adapter so the integration
    dispatcher's request_factory has real instance_id, member_id,
    space_id when populating downstream ReasoningRequest. Pre-Fold-6
    the adapter passed inputs=None, breaking inline tool calls.
    """

    instance_id: str
    member_id: str
    space_id: str
    turn_id: str


def build_turn_runner_provider(ctx: ThinPathContext) -> Callable[[Any, Any], tuple]:
    """Return the per-turn factory closure for ``ReasoningService``.

    The returned callable matches ``ReasoningService``'s
    ``turn_runner_provider`` contract: takes ``(request, event_emitter)``
    and returns ``(TurnRunner, ProductionResponseDelivery)``.

    Reads ``ctx`` fields at call time (per turn). Live wiring set
    via ``wire_live_thin_path()`` AFTER this function returns is
    automatically picked up by subsequent invocations.
    """

    def _build_per_turn_runner(request: Any, event_emitter: Any) -> tuple:
        # Local imports mirror server.py / repl.py canonical paths.
        # Deferred from module top so importing turn_runner_provider
        # itself doesn't pull the full enactment graph.
        from kernos.kernel.enactment import (
            DivergenceReasoner,
            EnactmentService,
            Planner,
            PresenceRenderer,
            StepDispatcher,
        )
        from kernos.kernel.integration.live_wiring import (
            build_renderer_to_integration_adapter,
        )
        from kernos.kernel.integration.service import IntegrationService
        from kernos.kernel.response_delivery import (
            AggregatedTelemetry,
            ProductionResponseDelivery,
            wrap_chain_caller_with_telemetry,
        )
        from kernos.kernel.turn_runner import TurnRunner

        telemetry = AggregatedTelemetry()
        wrapped_chain = wrap_chain_caller_with_telemetry(
            ctx.chain_caller, telemetry,
        )

        planner = Planner(
            chain_caller=wrapped_chain,
            tool_catalog=ctx.planner_tool_catalog,
        )
        dispatcher = StepDispatcher(
            executor=ctx.executor,
            descriptor_lookup=ctx.descriptor_lookup,
            trace_sink=ctx.trace_sink,
            event_emitter=ctx.dispatcher_event_emitter,
            audit_emitter=ctx.dispatcher_audit_emitter,
            on_dispatch_complete=telemetry.add_tool_iteration,
        )
        reasoner = DivergenceReasoner(chain_caller=wrapped_chain)

        def _renderer_inputs_factory(conversation_id: str) -> Any:
            return _RendererTurnInputs(
                instance_id=getattr(request, "instance_id", "") or "",
                member_id=getattr(request, "member_id", "") or "",
                space_id=getattr(request, "active_space_id", "") or "",
                turn_id=conversation_id
                or getattr(request, "conversation_id", "")
                or "",
            )

        presence = PresenceRenderer(
            chain_caller=wrapped_chain,
            tool_dispatcher=build_renderer_to_integration_adapter(
                integration_dispatcher=ctx.integration_dispatcher,
                inputs_factory=_renderer_inputs_factory,
            ),
        )
        integration = IntegrationService(
            chain_caller=wrapped_chain,
            read_only_dispatcher=ctx.integration_dispatcher,
            audit_emitter=ctx.integration_audit_emitter,
        )
        enactment = EnactmentService(
            presence_renderer=presence,
            planner=planner,
            step_dispatcher=dispatcher,
            divergence_reasoner=reasoner,
        )
        delivery = ProductionResponseDelivery(
            request=request,
            telemetry=telemetry,
            event_emitter=event_emitter,
        )
        runner = TurnRunner(
            cohort_runner=ctx.cohort_runner,
            integration_service=integration,
            enactment_service=enactment,
            response_delivery=delivery,
        )
        return runner, delivery

    return _build_per_turn_runner


def _build_live_request_factory() -> Callable[..., Any]:
    """Construct the shared request-factory that translates either
    ``ToolExecutionInputs`` (executor path) or ``(tool_id, args,
    inputs)`` positional (integration-dispatcher path) into a minimal
    ``ReasoningRequest``. Both paths reach the same execute_tool seam.

    The shape mirrors the ``_live_request_factory`` previously
    inlined in server.py / repl.py: trigger labels distinguish the
    two callers in trace logs and audit; system_prompt/model/messages
    are unused by execute_tool dispatch (it routes to kernel tools,
    not the LLM) so they're empty.
    """

    def _live_request_factory(*args: Any) -> Any:
        from kernos.kernel.reasoning import ReasoningRequest

        if len(args) == 1:
            # Executor path: receives ToolExecutionInputs.
            inputs = args[0]
            return ReasoningRequest(
                instance_id=getattr(inputs, "instance_id", "") or "",
                conversation_id=getattr(inputs, "turn_id", "") or "",
                system_prompt="",
                messages=[],
                tools=[],
                model="",
                trigger="thin-path-executor",
                active_space_id=getattr(inputs, "space_id", "") or "",
                member_id=getattr(inputs, "member_id", "") or "",
            )
        # Integration-dispatcher path: (tool_id, args, inputs).
        _, _, dispatch_inputs = args
        return ReasoningRequest(
            instance_id=getattr(dispatch_inputs, "instance_id", "") or "",
            conversation_id=getattr(dispatch_inputs, "turn_id", "") or "",
            system_prompt="",
            messages=[],
            tools=[],
            model="",
            trigger="thin-path-integration-dispatcher",
            active_space_id=getattr(dispatch_inputs, "space_id", "") or "",
            member_id=getattr(dispatch_inputs, "member_id", "") or "",
        )

    return _live_request_factory


def wire_live_thin_path(
    ctx: ThinPathContext,
    *,
    reasoning: Any,
    handler: Any,
) -> None:
    """Mutate ``ctx`` with the live thin-path components.

    Call AFTER ``MessageHandler`` and ``ReasoningService`` are
    constructed. Replaces the unwired stubs (or ``None``) on
    ``ctx`` with production-wired ``LiveExecutor`` /
    ``LiveDescriptorLookup`` / ``LiveIntegrationDispatcher`` /
    ``LivePlannerCatalog`` reading from ``handler._tool_catalog``
    and routing through ``reasoning.execute_tool``.

    Per Fold 3 ("Gate at dispatch, hint at surfacing"): every live
    dispatch path classifies with the actual call arguments before
    executing — surfacing-time hints are not authoritative. The
    LiveExecutor and LiveIntegrationDispatcher both consult the
    gate at dispatch time.

    Per Fold 8: tool.called / tool.result events + audit fire on
    every dispatch so equivalence soak compares audit/event trails
    cleanly. The emitters reused here come from ``ctx`` so launcher-
    specific behavior (e.g., /dump-visibility logging) is preserved.
    """
    from kernos.kernel.integration.live_wiring import (
        LiveDescriptorLookup,
        LiveExecutor,
        LiveIntegrationDispatcher,
        LivePlannerCatalog,
    )

    request_factory = _build_live_request_factory()

    ctx.descriptor_lookup = LiveDescriptorLookup(
        tool_catalog=handler._tool_catalog,
    )
    ctx.executor = LiveExecutor(
        execute_tool=reasoning.execute_tool,
        gate=reasoning._get_gate(),
        request_factory=lambda inputs: request_factory(inputs),
    )
    ctx.integration_dispatcher = LiveIntegrationDispatcher(
        execute_tool=reasoning.execute_tool,
        gate=reasoning._get_gate(),
        request_factory=lambda tid, args, inp: request_factory(tid, args, inp),
        event_emitter=ctx.dispatcher_event_emitter,
        audit_emitter=ctx.dispatcher_audit_emitter,
    )
    ctx.planner_tool_catalog = LivePlannerCatalog(
        tool_catalog=handler._tool_catalog,
    )
    logger.info(
        "INTEGRATION_CAPABILITY_FIRST_V1_BATCH2: live workshop binding "
        "wired via shared turn_runner_provider helper "
        "(REASONING-SERVICE-CONSTRUCTION-PARITY-V1)",
    )


def setup_default_thin_path_context(
    *,
    chains: Any,
    state: Any,
    events: Any,
    audit: Any,
) -> ThinPathContext:
    """Build a populated ``ThinPathContext`` with default components.

    Convenience for callsites that don't need launcher-specific
    emitters (e.g., ``app.py``, ``chat.py``, ``evals/bootstrap.py``).
    Constructs:

      - ``chain_caller``: standard primary-chain caller
        (``KERNOS_LLM_PROVIDER`` provider via ``chains["primary"][0]``)
      - ``cohort_runner``: ``CohortFanOutRunner`` with covenant cohort
        registered against ``state``
      - ``dispatcher_event_emitter`` / ``dispatcher_audit_emitter`` /
        ``integration_audit_emitter``: bridge to ``events`` / ``audit``
        with no extra logging (server.py / repl.py wire their own with
        /dump-visibility logging — those callsites construct
        ``ThinPathContext`` directly).
      - Unwired-stub ``executor`` / ``descriptor_lookup`` /
        ``integration_dispatcher`` / ``planner_tool_catalog``: raise
        loudly if reached before ``wire_live_thin_path()`` runs.

    server.py and repl.py do NOT use this helper because their
    emitters carry custom /dump-visibility logging behavior. They
    construct ``ThinPathContext`` directly with their own emitters.
    """
    from kernos.kernel.cohorts import (
        CohortFanOutConfig,
        CohortFanOutRunner,
        CohortRegistry,
        register_covenant_cohort,
    )
    from kernos.kernel.enactment import StaticToolCatalog
    from kernos.kernel.events import emit_event
    from kernos.kernel.event_types import EventType

    # Cohort registry with the covenant cohort registered.
    cohort_registry = CohortRegistry()
    try:
        register_covenant_cohort(cohort_registry, state)
    except Exception:
        logger.exception("CONSTRUCTION_PARITY_V1: covenant cohort registration failed")

    async def _cohort_audit_emitter(entry: dict) -> None:
        try:
            if audit is None or not hasattr(audit, "log"):
                return
            instance_id = entry.get("instance_id", "") or ""
            await audit.log(instance_id, entry)
        except Exception:
            logger.exception("CONSTRUCTION_PARITY_V1: cohort audit emit failed")

    cohort_runner = CohortFanOutRunner(
        registry=cohort_registry,
        audit_emitter=_cohort_audit_emitter,
        config=CohortFanOutConfig(),
    )

    # Standard chain caller — same shape as server.py's _shared_chain_caller.
    primary_chain = chains.get("primary", []) if chains else []

    async def _chain_caller(
        system, messages, tools, max_tokens, *, conversation_id="",
    ):
        if not primary_chain:
            raise RuntimeError(
                "primary chain not configured for thin-path operation"
            )
        entry = primary_chain[0]
        return await entry.provider.complete(
            model=entry.model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            conversation_id=conversation_id,
        )

    # Default emitters — bridge to events + audit, no extra logging.
    async def _dispatcher_event_emitter(payload: dict) -> None:
        if events is None:
            return
        try:
            event_type = (
                EventType.TOOL_CALLED
                if payload.get("type") == "tool.called"
                else EventType.TOOL_RESULT
            )
            await emit_event(
                events,
                event_type,
                payload.get("instance_id", ""),
                "step_dispatcher",
                payload=payload,
            )
        except Exception:
            logger.warning(
                "CONSTRUCTION_PARITY_V1: dispatcher event emit failed",
            )

    async def _dispatcher_audit_emitter(entry: dict) -> None:
        try:
            if audit is None or not hasattr(audit, "log"):
                return
            instance_id = entry.get("instance_id", "") or ""
            await audit.log(instance_id, entry)
        except Exception:
            logger.exception("CONSTRUCTION_PARITY_V1: dispatcher audit emit failed")

    async def _integration_audit_emitter(entry: dict) -> None:
        try:
            if audit is None or not hasattr(audit, "log"):
                return
            instance_id = entry.get("instance_id", "") or ""
            await audit.log(instance_id, entry)
        except Exception:
            logger.exception("CONSTRUCTION_PARITY_V1: integration audit emit failed")

    # Unwired stubs — raise if reached before wire_live_thin_path().
    class _UnwiredExecutor:
        async def execute(self, inputs: Any) -> Any:
            raise RuntimeError(
                f"thin-path executor not yet wired; "
                f"call wire_live_thin_path() after handler construction. "
                f"tool={getattr(inputs, 'tool_id', '?')!r}",
            )

    class _UnwiredDescriptorLookup:
        def descriptor_for(self, tool_id: str) -> Any:
            raise NotImplementedError(
                f"thin-path descriptor lookup not yet wired; "
                f"call wire_live_thin_path() after handler construction. "
                f"tool={tool_id!r}",
            )

    async def _unwired_integration_dispatcher(tool_id, args, inputs):
        return {"error": f"integration dispatcher not yet wired; tool={tool_id!r}"}

    return ThinPathContext(
        chain_caller=_chain_caller,
        cohort_runner=cohort_runner,
        dispatcher_event_emitter=_dispatcher_event_emitter,
        dispatcher_audit_emitter=_dispatcher_audit_emitter,
        integration_audit_emitter=_integration_audit_emitter,
        trace_sink=[],
        executor=_UnwiredExecutor(),
        descriptor_lookup=_UnwiredDescriptorLookup(),
        integration_dispatcher=_unwired_integration_dispatcher,
        planner_tool_catalog=StaticToolCatalog(),
    )


__all__ = [
    "ThinPathContext",
    "build_turn_runner_provider",
    "setup_default_thin_path_context",
    "wire_live_thin_path",
]
