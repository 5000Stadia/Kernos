"""Production bring-up of the WLP / runtime / STS substrate.

WTC v1 C5c-bringup (Codex-audit fold, design review-confirmed scope).

Prior to this module: the WLP / CRB / STS / TriggerEvaluationRuntime
substrate was shipped substrate that **was never instantiated in
production**. Tests wired everything via per-fixture stacks; the
live ``server.py`` brought up the cohort registry + reasoning +
adapters but skipped the entire workflow execution pipeline. The
joint stale-elements audit (CC + Codex, May 2026) confirmed this
across all six load-bearing classes:

* ``WorkflowRegistry``  — never instantiated in production
* ``ExecutionEngine``   — never instantiated in production
* ``ActionLibrary``     — never instantiated in production (all 7
                          Action classes were defined but never
                          registered)
* ``SubstrateTools``    — never instantiated in production
* ``TriggerRegistry``   — never instantiated in production
* ``TriggerEvaluationRuntime`` — never instantiated in production

This module fixes all of that in one place. ``server.py`` calls
:func:`bring_up_substrate` after the handler is constructed; the
returned :class:`Substrate` bundle is wired back into the handler so
``AwarenessEvaluator`` can pick up ``runtime`` for the unified
heartbeat (Phase 2b shipped in C5c-1 but dormant until now).

Per the design review direction:

* All 7 Action verbs MUST be registered. Where the handler-supplied
  callable is obvious (notify_user → send_outbound, etc.), the
  adapter wraps it. Where production infrastructure is not yet
  available (e.g., PostToServiceAction's workshop service
  registry), the verb is registered with a clear-error stub so the
  gap is surfaced when someone actually tries to invoke it rather
  than at substrate bring-up.
* The legacy ``TriggerRegistry`` post-flush hook is **retired in
  production** (the new InternalEventAdapter handles event flow to
  the runtime). ``TriggerRegistry.start`` gains an
  ``attach_post_flush_hook`` parameter; production passes ``False``,
  legacy tests default to ``True``.
* Expanded scope: ``ProviderRegistry``, ``ContextBriefRegistry``,
  ``DraftRegistry`` (STS dependencies) all brought up.

CRB scope (folded in from C5c-bringup-crb): ``InstallProposalStore``
gets its sqlite started, ``CRBProposalAuthor`` is constructed against
a :class:`ReasoningLLMAdapter` over ``handler.reasoning``, and
``CRBApprovalFlow`` is wired with restricted ports
(:class:`DraftRegistryReadAdapter`, :class:`SubstrateToolsSTSAdapter`,
:class:`CRBEventEmitter` over the registered ``"crb"`` source_module).
The 5-tuple — store + author + flow + draft port + sts port — moves
the substrate one step closer to surfacing routine.proposed →
routine.approved end-to-end.

Failure posture: the bring-up is intentionally fail-loud. If a
component fails to construct, server.py logs and continues with
the legacy Pattern 05 path active. The new substrate not coming up
should not block bot startup.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.agents.registry import AgentRegistry
    from kernos.kernel.crb.approval.flow import CRBApprovalFlow
    from kernos.kernel.crb.events import CRBEventEmitter
    from kernos.kernel.crb.proposal.author import CRBProposalAuthor
    from kernos.kernel.crb.proposal.install_proposal_store import (
        InstallProposalStore,
    )
    from kernos.kernel.drafts.registry import DraftRegistry
    from kernos.kernel.substrate_tools.facade import SubstrateTools
    from kernos.kernel.substrate_tools.query.context_brief import (
        ContextBriefRegistry,
    )
    from kernos.kernel.substrate_tools.query.list_providers import (
        ProviderRegistry,
    )
    from kernos.kernel.triggers.runtime import TriggerEvaluationRuntime
    from kernos.kernel.triggers.sources import InternalEventAdapter
    from kernos.kernel.workflows.action_library import ActionLibrary
    from kernos.kernel.workflows.execution_engine import ExecutionEngine
    from kernos.kernel.workflows.ledger import WorkflowLedger
    from kernos.kernel.workflows.trigger_registry import TriggerRegistry
    from kernos.kernel.workflows.workflow_registry import WorkflowRegistry

logger = logging.getLogger(__name__)


@dataclass
class Substrate:
    """The full bring-up bundle. Returned by :func:`bring_up_substrate`
    so server.py can wire references into the handler + retain handles
    for shutdown.
    """
    provider_registry: "ProviderRegistry"
    context_brief_registry: "ContextBriefRegistry"
    draft_registry: "DraftRegistry"
    trigger_registry: "TriggerRegistry"
    workflow_registry: "WorkflowRegistry"
    action_library: "ActionLibrary"
    workflow_ledger: "WorkflowLedger"
    execution_engine: "ExecutionEngine"
    runtime: "TriggerEvaluationRuntime"
    internal_event_adapter: "InternalEventAdapter"
    substrate_tools: "SubstrateTools"
    install_proposal_store: "InstallProposalStore"
    crb_event_emitter: "CRBEventEmitter"
    crb_proposal_author: "CRBProposalAuthor"
    crb_approval_flow: "CRBApprovalFlow"


async def bring_up_substrate(
    *,
    data_dir: str,
    handler: Any,
    agent_registry: "AgentRegistry",
) -> Substrate:
    """Construct and start the full WLP / runtime / STS substrate.

    Args:
        data_dir: production data directory (typically ``./data``).
        handler: production :class:`MessageHandler`. Used to wire the
            Action library verbs (deliver_fn = handler.send_outbound,
            tool_dispatch_fn = handler.reasoning.execute_tool, etc.)
            and to source state-store callables.
        agent_registry: production DAR :class:`AgentRegistry`,
            already constructed and started by the legacy bring-up
            path. Reused here rather than re-instantiated.

    Returns:
        :class:`Substrate` bundle. Callers should hold this for the
        lifetime of the process; on shutdown call
        :func:`tear_down_substrate` to close DBs cleanly.
    """
    # --- STS query-surface registries ----------------------------------
    from kernos.kernel.substrate_tools.query.list_providers import (
        ProviderRegistry,
    )
    from kernos.kernel.substrate_tools.query.context_brief import (
        ContextBriefRegistry,
    )
    provider_registry = ProviderRegistry()
    context_brief_registry = ContextBriefRegistry()

    # --- WDP DraftRegistry --------------------------------------------
    from kernos.kernel.drafts.registry import DraftRegistry
    draft_registry = DraftRegistry()
    await draft_registry.start(data_dir)

    # --- WLP TriggerRegistry (NO legacy post-flush hook in production) -
    from kernos.kernel.workflows.trigger_registry import TriggerRegistry
    trigger_registry = TriggerRegistry()
    await trigger_registry.start(data_dir, attach_post_flush_hook=False)

    # --- WLP WorkflowRegistry -----------------------------------------
    from kernos.kernel.workflows.workflow_registry import WorkflowRegistry
    workflow_registry = WorkflowRegistry()
    await workflow_registry.start(data_dir, trigger_registry)
    workflow_registry.wire_agent_registry(agent_registry)

    # --- ActionLibrary + register all 7 verbs -------------------------
    from kernos.kernel.workflows.action_library import ActionLibrary
    action_library = ActionLibrary()
    _register_all_actions(action_library, handler, agent_registry)

    # --- WorkflowLedger -----------------------------------------------
    from kernos.kernel.workflows.ledger import WorkflowLedger
    workflow_ledger = WorkflowLedger(data_dir)

    # --- ExecutionEngine ----------------------------------------------
    from kernos.kernel.workflows.execution_engine import ExecutionEngine
    execution_engine = ExecutionEngine()
    await execution_engine.start(
        data_dir,
        trigger_registry,
        workflow_registry,
        action_library,
        workflow_ledger,
    )

    # --- TriggerEvaluationRuntime + InternalEventAdapter --------------
    # The runtime dispatches into ExecutionEngine.execute_workflow; the
    # adapter bridges event_stream's post-flush hook into the runtime's
    # on_event_observed. With both wired, the unified path replaces the
    # legacy TriggerRegistry post-flush layer for event-driven trigger
    # evaluation in production.
    from kernos.kernel.triggers.runtime import TriggerEvaluationRuntime
    from kernos.kernel.triggers.sources import InternalEventAdapter
    runtime = TriggerEvaluationRuntime()
    await runtime.start(
        data_dir=data_dir,
        wlp_dispatch=execution_engine.execute_workflow,
        wlp_lookup_by_fire_id=execution_engine.find_execution_by_fire_id,
    )
    internal_event_adapter = InternalEventAdapter(runtime)
    await internal_event_adapter.start()

    # --- SubstrateTools facade (with runtime) -------------------------
    from kernos.kernel.substrate_tools.facade import SubstrateTools
    substrate_tools = SubstrateTools(
        agent_registry=agent_registry,
        workflow_registry=workflow_registry,
        draft_registry=draft_registry,
        provider_registry=provider_registry,
        context_brief_registry=context_brief_registry,
        runtime=runtime,
    )

    # --- CRB approval flow + dependencies ----------------------------
    # InstallProposalStore (sqlite-backed proposal state machine),
    # CRBProposalAuthor (LLM-driven user-facing wording), and the
    # CRBApprovalFlow that orchestrates them. Restricted ports keep CRB
    # from accessing capabilities outside its remit.
    from kernos.kernel import event_stream as _event_stream
    from kernos.kernel.crb.approval.flow import CRBApprovalFlow
    from kernos.kernel.crb.bringup_adapters import (
        DraftRegistryReadAdapter,
        ReasoningLLMAdapter,
        SubstrateToolsSTSAdapter,
    )
    from kernos.kernel.crb.events import CRB_SOURCE_MODULE, CRBEventEmitter
    from kernos.kernel.crb.proposal.author import CRBProposalAuthor
    from kernos.kernel.crb.proposal.install_proposal_store import (
        InstallProposalStore,
    )

    install_proposal_store = InstallProposalStore()
    await install_proposal_store.start(data_dir)

    # EmitterRegistry is a process singleton. Idempotent fetch keeps
    # bring-up safe across repeated calls within one process (rare in
    # production, common in tests).
    registry = _event_stream.emitter_registry()
    crb_emitter_raw = registry.get(CRB_SOURCE_MODULE) or registry.register(
        CRB_SOURCE_MODULE
    )
    crb_event_emitter = CRBEventEmitter(emitter=crb_emitter_raw)

    crb_proposal_author = CRBProposalAuthor(
        llm_client=ReasoningLLMAdapter(reasoning=handler.reasoning),
    )
    crb_approval_flow = CRBApprovalFlow(
        install_proposal_store=install_proposal_store,
        draft_port=DraftRegistryReadAdapter(draft_registry=draft_registry),
        sts_port=SubstrateToolsSTSAdapter(substrate_tools=substrate_tools),
        event_emitter=crb_event_emitter,
        author=crb_proposal_author,
    )

    logger.info(
        "WTC v1 C5c-bringup: substrate live — runtime=%s engine=%s "
        "verbs=%d crb=ready",
        runtime.claim_owner, "started", len(action_library._verbs),
    )

    return Substrate(
        provider_registry=provider_registry,
        context_brief_registry=context_brief_registry,
        draft_registry=draft_registry,
        trigger_registry=trigger_registry,
        workflow_registry=workflow_registry,
        action_library=action_library,
        workflow_ledger=workflow_ledger,
        execution_engine=execution_engine,
        runtime=runtime,
        internal_event_adapter=internal_event_adapter,
        substrate_tools=substrate_tools,
        install_proposal_store=install_proposal_store,
        crb_event_emitter=crb_event_emitter,
        crb_proposal_author=crb_proposal_author,
        crb_approval_flow=crb_approval_flow,
    )


async def tear_down_substrate(substrate: Substrate) -> None:
    """Stop the substrate's components in reverse construction order.
    Best-effort: failures are logged but don't propagate."""
    for label, coro_factory in (
        ("install_proposal_store", substrate.install_proposal_store.stop),
        ("internal_event_adapter", substrate.internal_event_adapter.stop),
        ("runtime", substrate.runtime.stop),
        ("execution_engine", substrate.execution_engine.stop),
        ("workflow_registry", substrate.workflow_registry.stop),
        ("trigger_registry", substrate.trigger_registry.stop),
        ("draft_registry", substrate.draft_registry.stop),
    ):
        try:
            await coro_factory()
        except Exception as exc:
            logger.warning(
                "WTC v1 C5c-bringup teardown: %s.stop raised: %s",
                label, exc,
            )


# ---------------------------------------------------------------------------
# Action library verb registration — all 7
# ---------------------------------------------------------------------------


def _register_all_actions(
    library: "ActionLibrary",
    handler: Any,
    agent_registry: "AgentRegistry",
) -> None:
    """Register every Action class shipped in the action library.

    Where production callables are obvious (handler.send_outbound,
    handler.reasoning.execute_tool, etc.), wire them. Where infra
    isn't yet available in production, register with a clear-error
    stub that surfaces the gap when invoked rather than at startup.
    """
    from kernos.kernel.workflows.action_library import (
        AppendToLedgerAction,
        CallToolAction,
        MarkStateAction,
        NotifyUserAction,
        PostToServiceAction,
        RouteToAgentAction,
        WriteCanvasAction,
    )

    library.register(NotifyUserAction(
        deliver_fn=_notify_deliver_adapter(handler),
    ))
    library.register(WriteCanvasAction(
        canvas_write_fn=_canvas_write_adapter(handler),
        canvas_read_fn=_canvas_read_adapter(handler),
    ))
    library.register(RouteToAgentAction(
        inbox=None,  # Provider-configuration-containment: operator binds
                     # at install time. Without binding, route_to_agent
                     # raises AgentInboxUnavailable per the spec.
        registry=agent_registry,
    ))
    library.register(CallToolAction(
        tool_dispatch_fn=_call_tool_adapter(handler),
    ))
    library.register(PostToServiceAction(
        service_post_fn=_unwired_stub("post_to_service"),
    ))
    library.register(MarkStateAction(
        state_store_set=_state_set_adapter(handler),
        state_store_get=_state_get_adapter(handler),
    ))
    library.register(AppendToLedgerAction(
        ledger_append_fn=_unwired_stub("append_to_ledger"),
        ledger_read_last_fn=_unwired_stub("append_to_ledger.read_last"),
    ))


# ---------------------------------------------------------------------------
# Adapter functions — bridge handler-shaped callables to verb-shaped ones.
# ---------------------------------------------------------------------------


def _notify_deliver_adapter(handler: Any):
    """Wrap handler.send_outbound to NotifyUserAction's expected signature.
    NotifyUserAction calls deliver_fn(channel=, message=, urgency=,
    instance_id=, member_id=) and expects a dict-like receipt."""
    async def _deliver(
        *, channel: str, message: str, urgency: str = "normal",
        instance_id: str = "", member_id: str = "",
    ) -> dict:
        # urgency reserved for future use; send_outbound doesn't currently
        # honor it but the kwarg-shape stays stable for forward compat.
        msg_id = await handler.send_outbound(
            instance_id, member_id, channel or None, message,
        )
        return {
            "persisted_id": str(msg_id) if msg_id else "",
            "channel": channel,
        }
    return _deliver


def _canvas_write_adapter(handler: Any):
    """Bridge to handler._get_canvas_service().write or equivalent.
    Returns a stub if the service isn't available so verb registration
    succeeds and failures surface at invocation time."""
    async def _write(**kwargs):
        svc = handler._get_canvas_service()
        if svc is None:
            raise RuntimeError(
                "WriteCanvasAction invoked but canvas_service is "
                "unavailable on this handler — bringup-stub gap"
            )
        # Best-effort signature match; unify when canvas_service's write
        # API is more locked down.
        return await svc.write(**kwargs)
    return _write


def _canvas_read_adapter(handler: Any):
    async def _read(**kwargs) -> str:
        svc = handler._get_canvas_service()
        if svc is None:
            raise RuntimeError(
                "WriteCanvasAction.read invoked but canvas_service is "
                "unavailable on this handler — bringup-stub gap"
            )
        return await svc.read(**kwargs)
    return _read


def _call_tool_adapter(handler: Any):
    async def _dispatch(**kwargs):
        reasoning = getattr(handler, "reasoning", None)
        if reasoning is None or not hasattr(reasoning, "execute_tool"):
            raise RuntimeError(
                "CallToolAction invoked but handler.reasoning.execute_tool "
                "is unavailable — bringup-stub gap"
            )
        return await reasoning.execute_tool(**kwargs)
    return _dispatch


def _state_set_adapter(handler: Any):
    async def _set(**kwargs):
        state = getattr(handler, "state", None)
        if state is None or not hasattr(state, "set_preference"):
            raise RuntimeError(
                "MarkStateAction.set invoked but handler.state is "
                "unavailable — bringup-stub gap"
            )
        # MarkStateAction's signature (state_store_set) is open-shape;
        # production callers will refine when manage_schedule rewires.
        return await state.set_preference(**kwargs)
    return _set


def _state_get_adapter(handler: Any):
    async def _get(**kwargs):
        state = getattr(handler, "state", None)
        if state is None or not hasattr(state, "get_preference"):
            raise RuntimeError(
                "MarkStateAction.get invoked but handler.state is "
                "unavailable — bringup-stub gap"
            )
        return await state.get_preference(**kwargs)
    return _get


def _unwired_stub(verb: str):
    """Return a callable that raises a clear NotImplementedError when
    invoked. Used for verbs whose production callables aren't available
    yet (e.g., PostToServiceAction's workshop service registry, the
    AppendToLedgerAction's ledger-write surface). Registration with
    this stub keeps the verb in the library so descriptors that
    reference it parse cleanly; invocation surfaces the gap."""
    async def _stub(*args, **kwargs):
        raise NotImplementedError(
            f"Action verb {verb!r} is registered but its production "
            f"callable hasn't been wired in C5c-bringup yet. Surface "
            f"a follow-up if you need this verb."
        )
    return _stub


__all__ = [
    "Substrate",
    "bring_up_substrate",
    "tear_down_substrate",
]
