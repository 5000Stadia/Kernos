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
      2. Launcher sets the early-bound fields (``chains``,
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

    CHAIN-CALLER-PARITY-V1: the canonical source for chain selection
    is ``chains`` (the ChainConfig). Per-turn the factory builds a
    resilient chain caller via ``build_resilient_chain_caller(chains=
    ctx.chains, request=request, ...)``, preserving chain fallback,
    context-window pre-flight skip, and model-override resolution.
    The legacy ``chain_caller`` field is retained for tests / edge
    callers that want to inject a specific callable (no resilience).
    """

    # Set in step 2 (early-bound):
    # Canonical chain selection surface — preferred for production:
    chains: Any = None
    # Legacy / test-injection callable — used only when chains is None:
    chain_caller: Any = None
    cohort_runner: Any = None
    dispatcher_event_emitter: Any = None
    dispatcher_audit_emitter: Any = None
    integration_audit_emitter: Any = None
    trace_sink: list = dataclasses.field(default_factory=list)
    # RESPONSE-FIDELITY-V1 Batch 1.3 hardening (2026-05-08): shared
    # list mirroring the trace_sink pattern. ReasoningService appends
    # ActionStateRecords here (via note_this and Batch 2+ migrated
    # surfaces); the integration runner peeks (copy without clearing)
    # at finalize time so records appear on Briefing.audit_trace
    # without preventing the handler-level drain that feeds the
    # "Action state this turn" conv-log block.
    action_record_sink: list = dataclasses.field(default_factory=list)

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


def _default_safety_margin() -> float:
    """Per-call safety margin applied to each chain entry's effective
    ceiling. Mirrors ``ReasoningService._context_safety_margin``."""
    import os
    raw = os.environ.get("KERNOS_CONTEXT_SAFETY_MARGIN", "")
    try:
        v = float(raw)
        if 0.0 <= v < 1.0:
            return v
    except ValueError:
        pass
    return 0.10


def _default_catalog_provider() -> dict[str, Any]:
    """Best-effort load of the model registry. Empty dict on failure
    so the chain caller falls through to tolerant routing."""
    try:
        from kernos.models import load_catalog
        result = load_catalog()
        return result.cards_by_name if result is not None else {}
    except Exception:
        return {}


def build_resilient_chain_caller(
    *,
    chains: Any,
    request: Any = None,
    chain_name: str = "primary",
    catalog_provider: Callable[[], dict[str, Any]] | None = None,
    safety_margin: float | None = None,
    request_model: str | None = None,
    trace_callback: Callable[..., None] | None = None,
) -> Callable[..., Awaitable[Any]]:
    """Build a per-turn chain caller with the three resilience behaviors
    that the legacy ``_call_chain`` provided.

    CHAIN-CALLER-PARITY-V1 (2026-05-03): preserves these behaviors at
    the thin-path seam so the strike commit can remove the legacy
    helper without regressing production functionality:

      1. **Chain fallback.** Iterate the resolved primary-chain
         entries; on a provider error from one entry, try the next.
         Surface ``LLMChainExhausted`` when all entries are exhausted.
      2. **Context-window pre-flight skip.** Before each provider
         call, consult the merged catalog and skip entries whose
         effective ceiling cannot fit the estimated payload. Raise
         ``ChainPayloadTooLarge`` when no entry fits.
      3. **Model-override resolution.** Read ``request.model_override``
         and route through ``model_routing.resolve_effective_chain``
         to compute the effective chain plus head model. The override
         is "preferred first attempt" — natural fallback applies on
         override-head failure.

    The override is resolved at build time (per-turn). The returned
    closure has the standard chain-caller signature
    ``(system, messages, tools, max_tokens, *, conversation_id="", tool_choice="auto")``
    and iterates the resolved entries with fallback + pre-flight skip.

    Construction parameters mirror legacy semantics:
      - ``chains``: ChainConfig (the configured chain dict).
      - ``request``: per-turn ReasoningRequest; provides
        ``model_override``, ``trace``, ``conversation_id``,
        ``model``. None acceptable for callers without per-turn
        context.
      - ``chain_name``: which chain to dispatch (default "primary").
      - ``catalog_provider``: callable returning the model-card
        dict; defaults to lazy load of ``kernos.models`` registry.
      - ``safety_margin``: per-call margin against ceiling; defaults
        to ``_default_safety_margin()`` reading
        ``KERNOS_CONTEXT_SAFETY_MARGIN``.
      - ``request_model``: substitute model on entry 0 unless the
        head was overridden. Defaults to ``request.model``.
      - ``trace_callback``: ``(level, source, event, detail) -> None``
        for trace recording; defaults to no-op.
    """
    from kernos.kernel.exceptions import (
        ChainPayloadTooLarge,
        LLMChainExhausted,
        ReasoningConnectionError,
        ReasoningProviderError,
    )
    from kernos.kernel.model_routing import resolve_effective_chain
    from kernos.kernel.token_estimator import estimate_tokens

    if catalog_provider is None:
        catalog_provider = _default_catalog_provider
    if safety_margin is None:
        safety_margin = _default_safety_margin()
    if trace_callback is None:
        trace_callback = lambda *args, **kwargs: None  # noqa: E731
    if request_model is None and request is not None:
        request_model = getattr(request, "model", "") or None

    # Resolve the effective chain at build time using the per-turn
    # request's model_override. The resolver handles stale chain
    # names + stale head specs gracefully.
    override = getattr(request, "model_override", None) if request is not None else None
    eff = resolve_effective_chain(
        chains=chains,
        requested_chain=chain_name,
        override=override,
    )
    resolved_chain_name = eff.chain_name
    resolved_entries = list(eff.entries)
    if not resolved_entries:
        resolved_entries = list(
            chains.get(resolved_chain_name)
            or chains.get("primary", [])
            or []
        )

    # Codex post-impl fold from the legacy implementation: when the
    # override carries an explicit (provider, model) head spec, entry
    # 0's model must NOT be replaced by request_model.
    head_was_overridden = bool(
        override is not None
        and override.get("override_provider")
        and override.get("override_model")
        and not eff.stale_head_spec
    )

    async def _resilient_chain_caller(
        system: Any,
        messages: list,
        tools: list,
        max_tokens: int,
        *,
        conversation_id: str = "",
        tool_choice: str = "auto",
    ) -> Any:
        # Pre-flight payload estimate; same estimator the legacy
        # helper uses.
        est_tokens = estimate_tokens(
            system=system, messages=messages, tools=tools,
        )
        catalog = catalog_provider() or {}
        called_count = 0
        skipped_count = 0
        largest_ceiling: int | None = None
        attempts: list[tuple[str, str, str]] = []
        last_exc: Exception | None = None

        for i, entry in enumerate(resolved_entries):
            # Determine the model to use for this entry. Entry 0
            # gets request_model unless the head was explicitly
            # overridden; subsequent entries always use their
            # configured model.
            if i == 0 and head_was_overridden:
                model = entry.model
            else:
                model = request_model if (i == 0 and request_model) else entry.model
            pname = getattr(entry.provider, "provider_name", "unknown")

            # Pre-flight context-window skip. Tolerant: missing
            # cards fall through (preserves behavior for unknown
            # models). The legacy helper warns once per process for
            # unknown models; the new seam is intentionally quieter
            # — diagnostics live in the trace_callback.
            card = catalog.get(model) if catalog else None
            if card is not None and getattr(card, "effective_max_input_tokens", 0):
                ceiling = card.effective_max_input_tokens
                if largest_ceiling is None or ceiling > largest_ceiling:
                    largest_ceiling = ceiling
                threshold = int(ceiling * (1.0 - safety_margin))
                if est_tokens > threshold:
                    skipped_count += 1
                    skip_reason = (
                        f"skipped: payload {est_tokens} tokens exceeds "
                        f"threshold {threshold} (ceiling {ceiling}, "
                        f"margin {safety_margin:.0%})"
                    )
                    trace_callback(
                        "info", "reasoning", "CHAIN_SKIP",
                        f"chain={resolved_chain_name} entry={pname} "
                        f"model={model} estimated_tokens={est_tokens} "
                        f"threshold={threshold}",
                    )
                    logger.info(
                        "CHAIN[%s]: skip %s/%s — %s",
                        resolved_chain_name, pname, model, skip_reason,
                    )
                    attempts.append((pname, model, skip_reason))
                    continue

            # Forward conversation_id if set so the wire-shape seam
            # reaches Codex's prompt_cache_key (see codex_provider
            # docstring).
            try:
                called_count += 1
                response = await entry.provider.complete(
                    model=model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    conversation_id=conversation_id,
                    tool_choice=tool_choice,
                )
                if i > 0:
                    # Partial fallback succeeded — silent per the
                    # LLM-SETUP-AND-FALLBACK contract.
                    trace_callback(
                        "info", "reasoning", "FALLBACK_USED",
                        f"chain={resolved_chain_name} via {pname}/{model} "
                        f"(skipped {i} entries)",
                    )
                    logger.info(
                        "CHAIN[%s]: success via %s/%s",
                        resolved_chain_name, pname, model,
                    )
                return response
            except (ReasoningProviderError, ReasoningConnectionError) as exc:
                trace_callback(
                    "warning", "reasoning", "CHAIN_FALLBACK",
                    f"chain={resolved_chain_name} {pname}/{model} "
                    f"failed: {str(exc)[:150]}",
                )
                logger.warning(
                    "CHAIN[%s]: %s/%s failed: %s",
                    resolved_chain_name, pname, model, exc,
                )
                last_exc = exc
                attempts.append((pname, model, str(exc)))
                continue

        # Distinct exhaustion paths: payload-too-large vs all-failed.
        if called_count == 0 and skipped_count > 0:
            trace_callback(
                "error", "reasoning", "CHAIN_PAYLOAD_TOO_LARGE",
                f"chain={resolved_chain_name} estimated_tokens={est_tokens} "
                f"largest_ceiling={largest_ceiling} skipped={skipped_count}",
            )
            logger.error(
                "CHAIN[%s]: payload too large for any entry "
                "(estimated=%d, largest_ceiling=%s)",
                resolved_chain_name, est_tokens, largest_ceiling,
            )
            raise ChainPayloadTooLarge(
                chain_name=resolved_chain_name,
                estimated_tokens=est_tokens,
                largest_ceiling=largest_ceiling,
                attempts=attempts,
            )

        trace_callback(
            "error", "reasoning", "CHAIN_EXHAUSTED",
            f"chain={resolved_chain_name} all "
            f"{len(resolved_entries)} entries exhausted",
        )
        logger.error(
            "CHAIN[%s]: all %d providers failed",
            resolved_chain_name, len(resolved_entries),
        )
        raise LLMChainExhausted(
            chain_name=resolved_chain_name, attempts=attempts,
        )

    return _resilient_chain_caller


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
        # CHAIN-CALLER-PARITY-V1: when ctx.chains is set (production
        # path), build a per-turn resilient chain caller that closes
        # over the request for model-override resolution and carries
        # chain fallback + context-window pre-flight skip. Falls back
        # to ctx.chain_caller for tests / edge callers that injected
        # a specific callable.
        if ctx.chains is not None:
            def _trace_cb(level, source, event, detail, **kw):
                if request is not None and getattr(request, "trace", None):
                    request.trace.record(level, source, event, detail, **kw)
            base_chain_caller = build_resilient_chain_caller(
                chains=ctx.chains,
                request=request,
                trace_callback=_trace_cb,
            )
        else:
            base_chain_caller = ctx.chain_caller
        wrapped_chain = wrap_chain_caller_with_telemetry(
            base_chain_caller, telemetry,
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
        # Production-wired config: pulls in env-var overrides
        # (KERNOS_INTEGRATION_TIMEOUT_SECONDS, _MAX_RETRIES,
        # _MAX_ITERATIONS, KERNOS_DATA_DIR for friction report
        # surfacing). Overriding callers can still build a config
        # by hand and wire IntegrationService(config=...) directly.
        from kernos.kernel.integration.runner import IntegrationConfig
        # RESPONSE-FIDELITY-V1 Batch 1.3 (2026-05-08, hardened
        # 2026-05-08): integration runner peeks the shared
        # action_record_sink at finalize time. Peek-without-clear so
        # the handler can still drain the same list afterwards to
        # populate ctx.action_state_records for the conv-log block.
        # The provider closure has no direct ReasoningService
        # reference (chicken-and-egg construction), so we wire via
        # the shared list — same pattern as trace_sink.
        _action_record_sink = ctx.action_record_sink
        integration = IntegrationService(
            chain_caller=wrapped_chain,
            read_only_dispatcher=ctx.integration_dispatcher,
            audit_emitter=ctx.integration_audit_emitter,
            config=IntegrationConfig.from_env(),
            action_record_drainer=lambda: list(_action_record_sink),
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
                # Carry the user's message so execute_tool handlers can
                # compose required NL args (e.g. improve_kernos's
                # spec_requirement) when an upstream stage dropped them.
                input_text=getattr(inputs, "user_message", "") or "",
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
            input_text=getattr(dispatch_inputs, "user_message", "") or "",
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
        tool_choice="auto",
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
            tool_choice=tool_choice,
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
        # CHAIN-CALLER-PARITY-V1: chains is the canonical source. The
        # per-turn factory builds the resilient chain caller from this.
        chains=chains,
        # Legacy callable retained for compat; per-turn factory prefers
        # the resilient caller built from `chains`.
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
    "build_resilient_chain_caller",
    "build_turn_runner_provider",
    "setup_default_thin_path_context",
    "wire_live_thin_path",
]
