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

import asyncio
import logging
import os
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
    from kernos.kernel.reference.catalog import CatalogStore
    from kernos.kernel.reference.cohort import CatalogingCohort
    from kernos.kernel.reference.events import ReferenceEventEmitter
    from kernos.kernel.reference.ingest import IngestionScanner
    from kernos.kernel.reference.tools import ReferenceService

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
    # REFERENCE-PRIMITIVE-V1 C-bringup
    reference_catalog: "CatalogStore"
    reference_event_emitter: "ReferenceEventEmitter"
    reference_cohort: "CatalogingCohort"
    reference_ingestion_scanner: "IngestionScanner"
    reference_service: "ReferenceService"
    # DURABLE-APPROVAL-RECEIPTS-V1 background expiry task. Cancelled
    # in tear_down_substrate so the substrate has clean teardown
    # discipline (Codex round-1-code finding 4 — was untracked + leaked).
    approval_expiry_task: "Any" = None


async def bring_up_substrate(
    *,
    data_dir: str,
    handler: Any,
    agent_registry: "AgentRegistry",
    gateway_health_providers: "Any | None" = None,
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
        gateway_health_providers: optional
            :class:`kernos.kernel.gateway_health.GatewayHealthProviders`
            bundle of live state sources for the gateway-health
            observer. When ``None`` (default), the observer is
            skipped entirely — appropriate for tests, headless
            invocations, or any caller that does not own the live
            Discord client. SUBSTRATE-PROVIDER-INJECTION-V1 (2026-05-21):
            this replaces the prior ``import kernos.server`` reach-back
            that silently broke under ``python kernos/server.py``
            (dual-module bug — RCA in spec).

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

    # --- WorkflowLedger -----------------------------------------------
    # Constructed BEFORE action library so AppendToLedgerAction can be
    # wired with the real ledger surface (Spec 6 commit 5 production
    # wiring; replaces the prior _unwired_stub).
    from kernos.kernel.workflows.ledger import WorkflowLedger
    workflow_ledger = WorkflowLedger(data_dir)

    # --- ActionLibrary + register all 7 verbs -------------------------
    from kernos.kernel.workflows.action_library import ActionLibrary
    action_library = ActionLibrary()
    _register_all_actions(
        action_library, handler, agent_registry, workflow_ledger, data_dir,
    )

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

    # --- Reference primitive substrate -------------------------------
    # REFERENCE-PRIMITIVE-V1 C-bringup: catalog store + cataloging
    # cohort + ingestion scanner + agent-facing tool service. The
    # ``"reference"`` source_module registers idempotently with the
    # EmitterRegistry. Source roots (docs/ + per-domain references/)
    # are NOT pre-registered here — registration happens lazily as
    # the per-turn ingestion check identifies new domains.
    from kernos.kernel.reference.bringup_adapters import (
        ReferenceCheapLLMAdapter,
    )
    from kernos.kernel.reference.catalog import CatalogStore as _CatalogStore
    from kernos.kernel.reference.cohort import CatalogingCohort as _CatCohort
    from kernos.kernel.reference.events import (
        REFERENCE_SOURCE_MODULE,
        ReferenceEventEmitter as _RefEventEmitter,
    )
    from kernos.kernel.reference.ingest import (
        IngestionScanner as _IngScanner,
        docs_source_root as _docs_source_root,
    )
    from kernos.kernel.reference.tools import ReferenceService as _RefService
    from pathlib import Path as _Path

    reference_catalog = _CatalogStore()
    await reference_catalog.start(data_dir)

    ref_emitter_raw = registry.get(REFERENCE_SOURCE_MODULE) or registry.register(
        REFERENCE_SOURCE_MODULE
    )
    reference_event_emitter = _RefEventEmitter(emitter=ref_emitter_raw)

    reference_llm = ReferenceCheapLLMAdapter(reasoning=handler.reasoning)
    # The instance_id here is the SUBSTRATE-level instance id used
    # for catalog row keying. Per-turn dispatch will pass the
    # caller's instance_id in the ReferenceServiceContext. v1 ships
    # single-tenant, so the substrate-level id is the canonical
    # value for catalog rows; multi-tenant support adds a per-call
    # override at the service boundary.
    _substrate_instance_id = "default"
    reference_cohort = _CatCohort(
        catalog=reference_catalog,
        emitter=reference_event_emitter,
        llm=reference_llm,
        instance_id=_substrate_instance_id,
    )
    await reference_cohort.start()
    reference_ingestion_scanner = _IngScanner(
        catalog=reference_catalog,
        cohort=reference_cohort,
        emitter=reference_event_emitter,
        instance_id=_substrate_instance_id,
    )
    references_root = _Path(data_dir) / "references"
    references_root.mkdir(parents=True, exist_ok=True)
    reference_service = _RefService(
        catalog=reference_catalog,
        cohort=reference_cohort,
        emitter=reference_event_emitter,
        navigator_llm=reference_llm,
        references_root=references_root,
        instance_id=_substrate_instance_id,
    )

    # First-boot catalog hydration. The instance-scoped ``docs/`` source
    # root is registered now (it ships with every install). Per-domain
    # ``references/`` roots are registered lazily as domains accrue
    # references; the cohort tool path also fires async cataloging
    # directly when an agent stores material, so the per-domain scan
    # is not load-bearing for v1.
    #
    # The scan itself is gated behind ``KERNOS_REFERENCE_FIRST_BOOT_SCAN``
    # (default off). With ~100 docs files split into ~850 H2 sections,
    # an unconditional scan fires that many ``complete_simple`` calls
    # against the cataloging cohort — a ~40-minute LLM-call deluge that
    # restarts on every ``/wipe`` (which clears the catalog along with
    # the rest of ``data/``). Founder direction is that scan should be
    # trigger-driven (on git pull / on store_reference write), not
    # per-boot. Until the trigger wiring lands, opt-in via env var:
    #
    #   KERNOS_REFERENCE_FIRST_BOOT_SCAN=1   # eager hydration
    #
    # Without the flag, the source root is still registered (so the
    # scanner can be invoked on demand later) and the catalog hydrates
    # lazily via hash-mismatch-on-retrieval for files that are already
    # cataloged. Files that were never cataloged stay uncataloged until
    # an explicit scan, but request_reference will simply miss them
    # rather than triggering a runaway.
    try:
        _docs_root_path = _Path(__file__).resolve().parent.parent.parent / "docs"
        if _docs_root_path.exists() and _docs_root_path.is_dir():
            reference_ingestion_scanner.add_source(
                _docs_source_root(_docs_root_path)
            )

            # REFERENCE-CATALOG-BAKED-V1: prefer the baked artifact path
            # over the live scan. Hydration from baked is the architect-
            # locked default (Path B+, 2026-05-06): the canonical docs
            # tree's catalog is derived content that ships with the repo,
            # built by ``scripts/regenerate_reference_catalog.py`` at
            # contribution time. Hash validation per file is the trust
            # mechanism — runtime always re-validates source-hash before
            # injection regardless of hydration source. Stale or missing
            # baked entries surface loud per-file diagnostics; the
            # live-scan path picks them up via hash-mismatch-on-retrieval.
            import os as _os
            from kernos.kernel.reference.baked import (
                load_baked_catalog as _load_baked,
            )
            from kernos.kernel.reference.catalog import (
                SCOPE_INSTANCE as _SCOPE_INSTANCE,
                TRUST_CANONICAL as _TRUST_CANONICAL,
            )
            _baked_catalog_root = _docs_root_path / "_catalog"
            _baked_summary = await _load_baked(
                docs_root=_docs_root_path,
                catalog_root=_baked_catalog_root,
                instance_id=_substrate_instance_id,
                catalog_store=reference_catalog,
                scope=_SCOPE_INSTANCE,
                trust_tier=_TRUST_CANONICAL,
                owner_domain_id="",
            )
            if not _baked_summary.manifest_present:
                logger.info(
                    "REFERENCE_BAKED_HYDRATION: no manifest at %s — catalog "
                    "starts empty. Run "
                    "`python scripts/regenerate_reference_catalog.py` to "
                    "seed the baked artifacts, or set "
                    "KERNOS_REFERENCE_FIRST_BOOT_SCAN=1 to live-scan once.",
                    _baked_catalog_root,
                )
            else:
                _level = (
                    logger.warning
                    if (
                        _baked_summary.files_stale
                        or _baked_summary.files_missing_artifact
                        or _baked_summary.files_artifact_invalid
                        or _baked_summary.files_uncatalogued
                    )
                    else logger.info
                )
                _level(
                    "REFERENCE_BAKED_HYDRATION: loaded=%d sections=%d "
                    "stale=%d missing_artifact=%d artifact_invalid=%d "
                    "uncatalogued=%d",
                    _baked_summary.files_loaded,
                    _baked_summary.sections_imported,
                    _baked_summary.files_stale,
                    _baked_summary.files_missing_artifact,
                    _baked_summary.files_artifact_invalid,
                    _baked_summary.files_uncatalogued,
                )

            # The legacy first-boot scan stays available as the explicit
            # opt-in / dev-loop hatch. With the baked artifact in place,
            # this is rarely needed: the catalog is already hydrated
            # before the agent's first turn, and live cataloging only
            # fires on hash mismatch or on store_reference writes. With
            # the artifact missing or stale and no baked override, this
            # is the way to populate the catalog without running the
            # contributor regen script.
            if _os.environ.get("KERNOS_REFERENCE_FIRST_BOOT_SCAN", "0") == "1":
                import asyncio as _asyncio
                _asyncio.create_task(
                    reference_ingestion_scanner.scan(),
                    name="reference_first_boot_scan",
                )
                logger.info(
                    "REFERENCE_FIRST_BOOT_SCAN: launched (env opt-in) — "
                    "expect ~%d LLM calls as %d docs hydrate",
                    sum(1 for _ in _docs_root_path.rglob("*.md")) * 8,
                    sum(1 for _ in _docs_root_path.rglob("*.md")),
                )
            else:
                logger.info(
                    "REFERENCE_FIRST_BOOT_SCAN: skipped (default). Baked "
                    "hydration handled the canonical docs tree; live-scan "
                    "remains opt-in via KERNOS_REFERENCE_FIRST_BOOT_SCAN=1."
                )
    except Exception:  # pragma: no cover
        logger.exception(
            "REFERENCE_BRINGUP_FIRST_BOOT_SCAN_FAILED — catalog will hydrate "
            "lazily via hash-mismatch-on-retrieval; non-blocking"
        )

    # FRICTION-PATTERN-SEED-V1 (2026-05-16): seed the starter friction
    # pattern catalog UNCONDITIONALLY. The FrictionObserver itself
    # isn't gated on the autonomy-loop env vars; it observes every
    # turn regardless. Without seeded patterns the observer detects
    # signals but the classifier has nothing to match against, so
    # signals become "unclassified" reports and never enter the
    # catalog. Seed must mirror the FrictionObserver's unconditional
    # posture — patterns are observation infrastructure, useful even
    # when the autonomy loop's architect+operator env vars aren't set.
    # Fail-open: catalog-seed failures log a warning and bring-up
    # continues; the substrate operates without the catalog the way
    # it did pre-spec.
    # Codex round-1 Fold #1: track ownership so we don't leak a SQLite
    # connection when seed creates a fresh store. Production usually
    # has handler._friction_pattern_store (long-lived for the handler's
    # FrictionObserver lifecycle) so seed shares it; in stub / opt-out
    # paths where the handler has no store, the seed-only instance
    # opens its own connection via ensure_schema and must be stopped
    # here or the connection leaks for the bot's lifetime.
    _seed_pattern_store = getattr(handler, "_friction_pattern_store", None)
    _seed_store_owned_here = False
    if _seed_pattern_store is None:
        from kernos.kernel.friction_patterns import (
            FrictionPatternStore as _FrictionPatternStore_seed,
        )
        _seed_pattern_store = _FrictionPatternStore_seed()
        _seed_store_owned_here = True
    try:
        import os as _os_seed_fp
        _seed_instance_id_fp = _os_seed_fp.environ.get(
            "KERNOS_INSTANCE_ID", "",
        ) or _substrate_instance_id
        from kernos.setup.seed_friction_patterns import (
            seed_friction_patterns_on_first_boot,
        )
        await seed_friction_patterns_on_first_boot(
            _seed_instance_id_fp,
            _seed_pattern_store,
            data_dir=data_dir,
        )
    except Exception as _exc_seed_fp:
        logger.warning(
            "FRICTION_PATTERN_SEED_BRINGUP_FAILED error=%s — "
            "substrate continues without the starter catalog; "
            "autonomy loop will have nothing to react to until "
            "the catalog is populated some other way",
            _exc_seed_fp,
        )
    finally:
        if _seed_store_owned_here:
            try:
                await _seed_pattern_store.stop()
            except Exception as _exc_close:  # pragma: no cover
                logger.debug(
                    "FRICTION_PATTERN_SEED_STORE_CLOSE_FAILED error=%s "
                    "— non-blocking, store was seed-only and bring-up "
                    "is complete",
                    _exc_close,
                )

    # Spec 6 commit 7 / B3 fold: register the self_improvement
    # workflow + launch the autonomy-loop emitters in B3 order
    # (helper success BEFORE emitters launch so trigger predicates
    # exist before friction.pattern_frequency_threshold_exceeded
    # events fire).
    #
    # Conditional on KERNOS_ARCHITECT_ACTOR_ID being set (required
    # for the architect-only authoring path). When unset, the
    # autonomy loop bring-up is skipped with a clear log line; the
    # rest of the substrate operates normally so the bring-up isn't
    # blocked on autonomy-loop optionality.
    import os as _os_si
    _architect_actor_id_si = _os_si.environ.get(
        "KERNOS_ARCHITECT_ACTOR_ID", "",
    )
    _operator_actor_id_si = _os_si.environ.get(
        "KERNOS_OPERATOR_ACTOR_ID", "",
    )
    if _architect_actor_id_si and _operator_actor_id_si:
        # Spec 6 commit 7 Codex round-1 H1 fold: BOTH architect and
        # operator identities required before announcing the autonomy
        # loop live. The operator actor authorizes the workflow's
        # autonomy-tool calls at execution time (autonomy_tools'
        # _is_operator gate); without operator, the workflow would
        # register + activate cleanly but every record_recurrence /
        # mark_resolved / emit_outcome call would fail at execute
        # time with CAT_AUTONOMY_NOT_AUTHORIZED. Skipping bring-up
        # entirely when operator is unset preserves the "no
        # half-initialised autonomy loop" invariant (v7 H3 fail-loud
        # pattern composes with v1 operational scope discipline:
        # the loop is either fully wireable or skipped with a clear
        # log).
        from kernos.kernel.workflows.authoring import (
            ACTOR_ARCHITECT as _ACTOR_ARCHITECT_SI,
            AuthoringContext as _AuthoringContext_si,
        )
        from kernos.kernel.workflows.self_improvement_helper import (
            register_self_improvement_workflow,
        )
        _architect_ctx_si = _AuthoringContext_si(
            actor_id=_architect_actor_id_si,
            actor_kind=_ACTOR_ARCHITECT_SI,
        )
        # Self-improvement workflow and its emitters MUST share an
        # instance namespace with the running handler's FrictionObserver,
        # otherwise friction.pattern_reactivated events emitted under the
        # live KERNOS_INSTANCE_ID will be skipped by the frequency
        # emitter's instance filter (autonomy_emitters.py:167) and the
        # loop never fires. The substrate-level "default" id is correct
        # for the reference catalog (single-tenant rows) but wrong for
        # cross-event subscription where the upstream event carries the
        # bot's real instance_id. Fall back to "default" only when env
        # is unset (tests / dev REPLs without an instance configured).
        _si_instance_id = _os_si.environ.get(
            "KERNOS_INSTANCE_ID", "",
        ) or _substrate_instance_id
        try:
            _si_workflow_id = await register_self_improvement_workflow(
                engine=execution_engine,
                architect_ctx=_architect_ctx_si,
                instance_id=_si_instance_id,
                trigger_runtime=runtime,
                operator_actor_id=_operator_actor_id_si,
            )
            # B3 fold: emitters launch AFTER helper success.
            # FrictionPatternFrequencyEmitter needs a started pattern
            # store; CodingSessionBridgeResponseEmitter needs only
            # the data_dir + instance_id.
            from kernos.kernel.friction_patterns import (
                FrictionPatternStore as _FrictionPatternStore_si,
            )
            from kernos.kernel.workflows.autonomy_emitters import (
                CodingSessionBridgeResponseEmitter,
                FrictionPatternFrequencyEmitter,
            )
            # Reuse the handler's store when available so the autonomy
            # loop observes the same friction patterns the handler's
            # FrictionObserver records. Otherwise construct + start a
            # fresh store. Either way ensure_schema is idempotent.
            _si_pattern_store = getattr(handler, "_friction_pattern_store", None)
            if _si_pattern_store is None:
                _si_pattern_store = _FrictionPatternStore_si()
            await _si_pattern_store.ensure_schema(data_dir)

            # FRICTION-REMEDIATION-V2 (2026-05-20): register the
            # declarative remediation handlers. Currently only
            # restart_kernos — fired when discord-heartbeat-blocked
            # crosses its threshold (5 occurrences in 10 min by
            # default). Sentinel-file cool-off prevents loop-restart.
            async def _restart_kernos_handler(
                *, instance_id: str, pattern_id: str,
                occurrence_count: int,
            ) -> None:
                import sys as _sys
                import os as _os
                import logging as _logging
                _logging.getLogger(__name__).error(
                    "FRICTION_REMEDIATION_RESTART_KERNOS: "
                    "pattern=%s instance=%s occurrence_count=%d — "
                    "executing os.execv now",
                    pattern_id, instance_id, occurrence_count,
                )
                for h in _logging.getLogger().handlers:
                    try:
                        h.flush()
                    except Exception:
                        pass
                _os.execv(
                    _sys.executable,
                    [_sys.executable] + _sys.argv,
                )
            _si_pattern_store.register_remediation_handler(
                "restart_kernos", _restart_kernos_handler,
            )
            _freq_emitter_si = FrictionPatternFrequencyEmitter(
                instance_id=_si_instance_id,
                pattern_store=_si_pattern_store,
            )
            await _freq_emitter_si.start()
            execution_engine.register_emitter(
                "friction_pattern_frequency", _freq_emitter_si,
            )
            # AUTO-WAKE-V1 (2026-05-19): wire the emitter's wake
            # callback to the handler's injection method so consult
            # completions wake a turn in the originating space
            # instead of requiring the agent to poll.
            _response_emitter_si = CodingSessionBridgeResponseEmitter(
                instance_id=_si_instance_id,
                data_dir=data_dir,
                wake_callback=getattr(
                    handler, "inject_consult_completion_wake", None,
                ),
            )
            await _response_emitter_si.start()
            execution_engine.register_emitter(
                "coding_session_response", _response_emitter_si,
            )

            logger.info(
                "SELF_IMPROVEMENT_AUTONOMY_LOOP_LIVE workflow_id=%s "
                "instance_id=%s",
                _si_workflow_id, _si_instance_id,
            )
        except Exception as _exc_si:
            logger.warning(
                "SELF_IMPROVEMENT_AUTONOMY_LOOP_BRINGUP_FAILED error=%s "
                "— continuing without autonomy loop",
                _exc_si,
            )
    elif _architect_actor_id_si and not _operator_actor_id_si:
        logger.info(
            "SELF_IMPROVEMENT_AUTONOMY_LOOP_SKIPPED: "
            "KERNOS_OPERATOR_ACTOR_ID not set; the autonomy loop's "
            "tool calls require an operator identity at execution "
            "time. Set both KERNOS_ARCHITECT_ACTOR_ID and "
            "KERNOS_OPERATOR_ACTOR_ID to enable the loop."
        )
    else:
        logger.info(
            "SELF_IMPROVEMENT_AUTONOMY_LOOP_SKIPPED: "
            "KERNOS_ARCHITECT_ACTOR_ID not set; helper requires "
            "architect identity at bring-up"
        )

    # SELF-CONTROLLED-LOOP-LIVENESS-V1 (2026-05-21): boot-smoke
    # sentinel workflow that proves the substrate event-trigger-
    # workflow loop is alive on every restart. Unconditional —
    # uses a synthetic substrate-owned architect so it does not
    # depend on KERNOS_ARCHITECT_ACTOR_ID being set. Failure logs
    # WARNING and continues; the sentinel is diagnostic infra and
    # cannot cascade into a substrate boot abort.
    try:
        from kernos.kernel.workflows.loop_health_helper import (
            register_loop_health_workflow,
            emit_boot_probe,
            register_completion_logger,
            _generate_boot_id,
        )
        from kernos.kernel import event_stream as _event_stream_loop_health
        _loop_health_instance_id = (
            os.environ.get("KERNOS_INSTANCE_ID", "")
            or _substrate_instance_id
            or "default"
        )
        await register_loop_health_workflow(
            engine=execution_engine,
            instance_id=_loop_health_instance_id,
            trigger_runtime=runtime,
        )
        # Codex round 3: generate boot_id + subscribe completion-log
        # hook BEFORE emitting the boot probe so the hook is in place
        # before any workflow.execution_terminated event could flush
        # (avoids flush-order race where the workflow completes before
        # the subscriber registers).
        _loop_health_boot_id = _generate_boot_id()
        register_completion_logger(
            event_stream=_event_stream_loop_health,
            instance_id=_loop_health_instance_id,
            boot_id=_loop_health_boot_id,
        )
        # Emit AFTER both registration AND completion-logger subscription.
        await emit_boot_probe(
            instance_id=_loop_health_instance_id,
            event_stream=_event_stream_loop_health,
            boot_id=_loop_health_boot_id,
        )
    except Exception as _exc_lh:
        logger.warning(
            "LOOP_HEALTH_SENTINEL_BRINGUP_FAILED error=%s — "
            "substrate continues without boot-smoke liveness proof",
            _exc_lh,
        )

    # SUBSTRATE-SELF-TEST-V1 (2026-05-26) post-bring-up hook.
    # Run the 8-probe soak suite against the current process
    # state once at bring-up. Loud signal on any failure; does
    # NOT abort bring-up (per spec AC5 + Open Question 1 — emit
    # signal + activate autonomous-mutation gate, not halt).
    #
    # v1.2 (2026-05-26): step-by-step breadcrumb logs + 60s
    # timeout + BaseException catch. The original hook returned
    # silently on the first live deployment (no PASSED, no
    # FAILED, no BRINGUP_FAILED) because (a) `except Exception`
    # misses CancelledError when bring-up is racing the event
    # loop, and (b) without per-step breadcrumbs there's no way
    # to localize which await stalled. Hardened so the next
    # silent-soak symptom is impossible.
    logger.info("SUBSTRATE_SELF_TEST_HOOK_ENTERED")
    try:
        from kernos.kernel.self_test_gate import (
            SubstrateSoakRunner,
            _format_soak_result_prose,
            mark_substrate_health,
        )
        from kernos.kernel import event_stream as _event_stream_soak
        logger.info("SUBSTRATE_SELF_TEST_HOOK_IMPORTS_OK")

        _soak_runner = SubstrateSoakRunner()
        logger.info("SUBSTRATE_SELF_TEST_HOOK_RUNNER_BUILT")
        _soak_result = await asyncio.wait_for(
            _soak_runner.run_all(), timeout=60.0,
        )
        logger.info(
            "SUBSTRATE_SELF_TEST_HOOK_RUN_RETURNED probes=%d",
            len(_soak_result.per_probe),
        )
        _soak_prose = _format_soak_result_prose(_soak_result)

        # v1.2 durable artifact: write the result to a JSON sentinel
        # file so operators (and the substrate-soak verification
        # tooling) can inspect ground truth even when log emissions
        # are dropped due to handler-detach races (discord.py
        # setup_logging mutates root.handlers during bring-up; see
        # GATEWAY_HEALTH_LOG_FILE_REATTACHED). The file is
        # overwritten each boot — current state, not history.
        try:
            import json as _json_soak
            from pathlib import Path as _Path_soak
            _artifact_dir = _Path_soak("data/diagnostics")
            _artifact_dir.mkdir(parents=True, exist_ok=True)
            _artifact_path = _artifact_dir / "substrate_soak_last.json"
            _artifact_path.write_text(_json_soak.dumps({
                "all_passed": _soak_result.all_passed,
                "total_duration_ms": _soak_result.total_duration_ms,
                "failing_probes": list(
                    _soak_result.failing_probe_names(),
                ),
                "per_probe": [
                    {
                        "probe_name": p.probe_name,
                        "passed": p.passed,
                        "duration_ms": p.duration_ms,
                        "failure_reason": p.failure_reason,
                    }
                    for p in _soak_result.per_probe
                ],
            }, indent=2))
            logger.info(
                "SUBSTRATE_SELF_TEST_ARTIFACT_WRITTEN path=%s",
                str(_artifact_path),
            )
        except Exception as _exc_artifact:
            logger.warning(
                "SUBSTRATE_SELF_TEST_ARTIFACT_WRITE_FAILED error=%s",
                _exc_artifact,
            )

        # Update the AC9 autonomous-mutation gate's health flag
        # so git_commit/push gate appropriately on this result.
        mark_substrate_health(
            passed=_soak_result.all_passed,
            failing_probes=_soak_result.failing_probe_names(),
        )

        if _soak_result.all_passed:
            logger.info(
                "SUBSTRATE_SELF_TEST_PASSED probes=%d "
                "total_duration_ms=%d",
                len(_soak_result.per_probe),
                _soak_result.total_duration_ms,
            )
            try:
                await _event_stream_soak.emit(
                    _loop_health_instance_id,
                    "substrate.self_test_passed",
                    {
                        "probe_count": len(_soak_result.per_probe),
                        "total_duration_ms": (
                            _soak_result.total_duration_ms
                        ),
                        "per_probe_durations": {
                            p.probe_name: p.duration_ms
                            for p in _soak_result.per_probe
                        },
                    },
                    space_id="",
                )
            except Exception:
                pass
        else:
            logger.warning(
                "SUBSTRATE_SELF_TEST_FAILED failing=%s "
                "total_duration_ms=%d — substrate continues but "
                "autonomous-mutation gate active until next pass",
                _soak_result.failing_probe_names(),
                _soak_result.total_duration_ms,
            )
            # Loud per-probe details for operator triage.
            for _probe in _soak_result.per_probe:
                if not _probe.passed:
                    logger.warning(
                        "SUBSTRATE_SELF_TEST_PROBE_FAIL probe=%s "
                        "reason=%s",
                        _probe.probe_name,
                        _probe.failure_reason,
                    )
            try:
                await _event_stream_soak.emit(
                    _loop_health_instance_id,
                    "substrate.self_test_failed",
                    {
                        "severity": "unhealthy",
                        "failing_probes": list(
                            _soak_result.failing_probe_names(),
                        ),
                        "total_duration_ms": (
                            _soak_result.total_duration_ms
                        ),
                        # Per spec AC5 + Codex round-1 fold:
                        # failed events MUST include
                        # behavioral_evidence + substrate_evidence
                        # so operators can triage without a
                        # second tool call.
                        "per_probe_outcomes": {
                            p.probe_name: {
                                "passed": p.passed,
                                "duration_ms": p.duration_ms,
                                "failure_reason": p.failure_reason,
                                "behavioral_evidence": (
                                    p.behavioral_evidence
                                ),
                                "substrate_evidence": (
                                    p.substrate_evidence
                                ),
                            }
                            for p in _soak_result.per_probe
                        },
                    },
                    space_id="",
                )
            except Exception:
                pass
    except asyncio.TimeoutError:
        logger.warning(
            "SUBSTRATE_SELF_TEST_BRINGUP_TIMEOUT timeout_s=60 — "
            "soak ran past 60s in live process (CLI runs in <2s). "
            "Substrate continues; some probe is interacting badly "
            "with live event_stream/workflow engine. Investigate."
        )
    except BaseException as _exc_soak:
        # v1.2 catches BaseException (not just Exception) so
        # CancelledError, KeyboardInterrupt, and SystemExit all
        # surface. The original silent-soak symptom was a
        # CancelledError swallowed by `except Exception`.
        logger.warning(
            "SUBSTRATE_SELF_TEST_BRINGUP_FAILED error=%s:%s — "
            "substrate continues without soak self-test result",
            type(_exc_soak).__name__, _exc_soak,
        )
        # CancelledError must re-raise so the surrounding task
        # group is not held alive by a cancelled child.
        if isinstance(_exc_soak, BaseException) and not isinstance(
            _exc_soak, Exception,
        ):
            # Re-raise only true BaseException subclasses
            # (CancelledError, KeyboardInterrupt, SystemExit).
            # Plain Exception is logged and suppressed per AC5.
            raise

    # DURABLE-APPROVAL-RECEIPTS-V1 (2026-05-21): generic operator-
    # approval primitive. Schema ensure + boot reconcile + background
    # expiry pass. Generic substrate; useful beyond the autonomous
    # improvement loop (any future hard_write capability needing
    # durable operator approval reuses these primitives).
    try:
        from kernos.kernel import approval_receipts as _approvals
        from kernos.kernel import event_stream as _event_stream_approvals
        await _approvals.ensure_schema(data_dir)
        # Boot reconcile: catches downtime expiries + re-emits any
        # terminal-state receipts whose decision_emitted_at is NULL
        # (decision event was queued but not flushed before the
        # prior crash). Idempotent on clean shutdowns.
        await _approvals.boot_reconcile(
            data_dir=data_dir, event_stream=_event_stream_approvals,
        )

        # Background expiry pass — default 60s cadence. Failure-
        # isolated per pass so a transient DB hiccup doesn't kill
        # the background task.
        import asyncio as _asyncio_approvals

        async def _approval_expiry_loop():
            while True:
                try:
                    await _approvals.expire_pass(
                        data_dir=data_dir,
                        event_stream=_event_stream_approvals,
                    )
                except Exception as _exc_pass:
                    logger.warning(
                        "APPROVAL_EXPIRY_PASS_FAILED exc=%s — "
                        "continuing", _exc_pass,
                    )
                await _asyncio_approvals.sleep(60)

        _approval_expiry_task = _asyncio_approvals.create_task(
            _approval_expiry_loop(),
        )
        logger.info(
            "APPROVAL_RECEIPTS_BRINGUP_OK schema=ensured "
            "boot_reconcile=ran expiry_loop=started",
        )
    except Exception as _exc_ar:
        logger.warning(
            "APPROVAL_RECEIPTS_BRINGUP_FAILED error=%s — "
            "substrate continues without durable approval primitive",
            _exc_ar,
        )
        _approval_expiry_task = None

    # POSTURE-CONFIGURATION-V1 (2026-05-22): apply persisted
    # gate_mode from instance_posture if present. The gate's
    # __init__ resolves env-only; this hook lets persisted
    # operator config take precedence, surviving restart /
    # execv / self-update.
    try:
        _kernos_instance_id = os.environ.get("KERNOS_INSTANCE_ID", "").strip()
        if _kernos_instance_id and handler._instance_db is not None:
            _posture_row = await handler._instance_db.get_instance_posture(
                _kernos_instance_id,
            )
            _persisted_mode = (_posture_row.get("gate_mode") or "").strip()
            if _persisted_mode:
                from kernos.kernel.gate import get_mode_policy_by_name
                _policy = get_mode_policy_by_name(_persisted_mode)
                if _policy is not None:
                    handler.reasoning._get_gate().set_mode_policy(_policy)
                    logger.info(
                        "POSTURE_BRINGUP applied persisted gate_mode=%s",
                        _persisted_mode,
                    )
                else:
                    logger.warning(
                        "POSTURE_BRINGUP persisted gate_mode=%r unknown; "
                        "leaving env-derived default",
                        _persisted_mode,
                    )
    except Exception as _exc_pc:
        logger.warning(
            "POSTURE_BRINGUP_FAILED error=%s — substrate continues "
            "with env-derived gate mode", _exc_pc,
        )

    # GATEWAY-HEALTH-OBSERVER-V1 (2026-05-19) + SUBSTRATE-PROVIDER-
    # INJECTION-V1 (2026-05-21): gateway-health is a SAFETY MONITOR;
    # it must run independently of self-improvement gating, and it
    # must read live state directly from the caller (not via
    # ``import kernos.server``, which silently produces a parallel
    # module copy under ``python kernos/server.py`` — RCA in spec).
    #
    # When ``gateway_health_providers`` is None, skip the observer
    # entirely with a loud log (test/headless mode). When provided,
    # construct via injected callables/counter. The observer reads
    # live state through the provider lambdas; no substrate-side
    # import of the caller.
    await _bring_up_gateway_health_observer(
        data_dir=data_dir,
        handler=handler,
        execution_engine=execution_engine,
        gateway_health_providers=gateway_health_providers,
    )

    logger.info(
        "WTC v1 C5c-bringup: substrate live — runtime=%s engine=%s "
        "verbs=%d crb=ready reference=ready",
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
        reference_catalog=reference_catalog,
        reference_event_emitter=reference_event_emitter,
        reference_cohort=reference_cohort,
        reference_ingestion_scanner=reference_ingestion_scanner,
        reference_service=reference_service,
        approval_expiry_task=_approval_expiry_task,
    )


async def _bring_up_gateway_health_observer(
    *,
    data_dir: str,
    handler: Any,
    execution_engine: Any,
    gateway_health_providers: "Any | None",
) -> None:
    """Construct + start the GatewayHealthObserver using injected
    providers. Extracted as a helper so it can be unit-tested in
    isolation with mocked dependencies — without standing up the
    entire substrate.

    Behavior:
      * ``gateway_health_providers is None`` → log SKIPPED, return.
      * Otherwise: construct the observer with the providers, start
        it, register on the execution_engine. Any exception during
        bring-up is caught and logged as
        ``GATEWAY_HEALTH_OBSERVER_BRINGUP_FAILED`` — the rest of
        the substrate continues.

    The handler's pre-existing ``_friction_pattern_store`` is reused
    if present (default in MessageHandler.__init__). If absent
    (e.g., handler init skipped its pattern store, or a stub handler
    in tests), a fresh ``FrictionPatternStore`` is created and
    schema-ensured against ``data_dir``. The fallback store's
    teardown is not tracked here — that's the handler's job in
    production; tests should pass a handler with a populated store
    if they care about shutdown semantics.
    """
    if gateway_health_providers is None:
        logger.info(
            "GATEWAY_HEALTH_OBSERVER_SKIPPED: no providers injected "
            "(test or headless mode)",
        )
        return
    try:
        from kernos.kernel.gateway_health import (
            GatewayHealthObserver as _GatewayHealthObserver,
        )
        _gw_instance_id = (
            os.getenv("KERNOS_INSTANCE_ID", "")
            or getattr(handler, "_instance_id", "")
            or "default"
        )
        _gw_pattern_store = getattr(
            handler, "_friction_pattern_store", None,
        )
        if _gw_pattern_store is None:
            from kernos.kernel.friction_patterns import (
                FrictionPatternStore,
            )
            _gw_pattern_store = FrictionPatternStore()
            await _gw_pattern_store.ensure_schema(data_dir)
            logger.info(
                "GATEWAY_HEALTH_OBSERVER_FALLBACK_STORE: handler had "
                "no _friction_pattern_store; created fresh "
                "FrictionPatternStore for the observer (teardown "
                "left to caller).",
            )
        _gw_observer = _GatewayHealthObserver(
            instance_id=_gw_instance_id,
            data_dir=data_dir,
            pattern_store=_gw_pattern_store,
            latency_provider=gateway_health_providers.latency_provider,
            inbound_event_ts_provider=(
                gateway_health_providers.inbound_event_ts_provider
            ),
            message_create_counter=(
                gateway_health_providers.message_create_counter
            ),
            last_on_message_provider=(
                gateway_health_providers.last_on_message_provider
            ),
            # DISCORD-GATEWAY-DEAFNESS-DETECT-V1 (2026-05-25):
            # forward the new provider if callers wired it. None
            # for back-compat with callers that haven't updated
            # their GatewayHealthProviders instance.
            any_socket_event_ts_provider=getattr(
                gateway_health_providers,
                "any_socket_event_ts_provider",
                None,
            ),
            runner_inspector=None,  # V1.5 wires this
        )
        await _gw_observer.start()
        execution_engine.register_emitter(
            "gateway_health", _gw_observer,
        )
    except Exception as _exc_gw:
        logger.warning(
            "GATEWAY_HEALTH_OBSERVER_BRINGUP_FAILED error=%s — "
            "continuing without gateway-health observer",
            _exc_gw,
        )


async def tear_down_substrate(substrate: Substrate) -> None:
    """Stop the substrate's components in reverse construction order.
    Best-effort: failures are logged but don't propagate."""
    # DURABLE-APPROVAL-RECEIPTS-V1: cancel the background expiry task
    # first so it doesn't race against the DB closing.
    if substrate.approval_expiry_task is not None:
        try:
            substrate.approval_expiry_task.cancel()
            import asyncio as _asyncio_td
            try:
                await _asyncio_td.wait_for(
                    substrate.approval_expiry_task, timeout=2.0,
                )
            except (_asyncio_td.CancelledError, _asyncio_td.TimeoutError, Exception):
                pass
        except Exception as exc:
            logger.warning(
                "WTC v1 C5c-bringup teardown: approval_expiry_task "
                "cancel raised: %s", exc,
            )

    for label, coro_factory in (
        ("reference_cohort", substrate.reference_cohort.stop),
        ("reference_catalog", substrate.reference_catalog.stop),
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
    workflow_ledger: "WorkflowLedger",
    data_dir: str,
) -> None:
    """Register every Action class shipped in the action library.

    Where production callables are obvious (handler.send_outbound,
    handler.reasoning.execute_tool, etc.), wire them. Where infra
    isn't yet available in production, register with a clear-error
    stub that surfaces the gap when invoked rather than at startup.

    Spec 6 commit 5: AppendToLedgerAction wired with WorkflowLedger
    (replaces prior _unwired_stub). The action library's verb-shape
    contract for ledger_append_fn / ledger_read_last_fn uses kwargs
    (workflow_id=, entry=, instance_id=); the adapters bridge to
    WorkflowLedger's positional shape (instance_id, workflow_id, ...).
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
        tool_dispatch_fn=_call_tool_adapter(
            handler, workflow_ledger, data_dir,
        ),
    ))
    library.register(PostToServiceAction(
        service_post_fn=_unwired_stub("post_to_service"),
    ))
    library.register(MarkStateAction(
        state_store_set=_state_set_adapter(handler),
        state_store_get=_state_get_adapter(handler),
    ))
    library.register(AppendToLedgerAction(
        ledger_append_fn=_workflow_ledger_append_adapter(workflow_ledger),
        ledger_read_last_fn=_workflow_ledger_read_last_adapter(workflow_ledger),
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


def _call_tool_adapter(
    handler: Any,
    workflow_ledger: "WorkflowLedger",
    data_dir: str,
):
    """Production tool-dispatch adapter for CallToolAction.

    Spec 6 commit 7 (Codex round-1 B1 fold): routes the five
    autonomy-loop tool_ids to autonomy_tools handlers directly,
    threading the production substrate (pattern_store from the
    handler, workflow_ledger from bring-up, data_dir from bring-up).
    Other tool_ids fall through to ``handler.reasoning.execute_tool``
    via the kwargs-adaptation path.

    The autonomy-loop workflow's action_sequence calls
    ``call_tool`` with one of the five tool_ids; without the
    direct routing here, the workflow would crash on the first
    record_recurrence step because reasoning.execute_tool's
    signature is ``(tool_name, tool_input, request)`` — a
    different shape from the kwargs CallToolAction passes
    (``tool_id``, ``args``, ``instance_id``, ``member_id``).
    """
    autonomy_tool_ids = frozenset({
        "transition_friction_pattern_lifecycle",
        "record_friction_pattern_recurrence",
        "emit_autonomy_loop_event",
        "ask_coding_session_for_workflow",
        "read_coding_session_response_for_workflow",
    })

    async def _dispatch(*, tool_id: str, args: dict,
                        instance_id: str, member_id: str):
        # Spec 6 autonomy tools route directly to the autonomy_tools
        # handlers with real substrate (pattern_store from the
        # handler; ledger + data_dir from bring-up closure).
        if tool_id in autonomy_tool_ids:
            from kernos.kernel.workflows.autonomy_tools import (
                handle_ask_coding_session_for_workflow,
                handle_emit_autonomy_loop_event_tool,
                handle_read_coding_session_response_for_workflow,
                handle_record_friction_pattern_recurrence_tool,
                handle_transition_friction_pattern_lifecycle_tool,
            )
            pattern_store = getattr(handler, "_friction_pattern_store", None)
            if tool_id == "transition_friction_pattern_lifecycle":
                if pattern_store is None:
                    raise RuntimeError(
                        "transition_friction_pattern_lifecycle requires "
                        "handler._friction_pattern_store"
                    )
                return await handle_transition_friction_pattern_lifecycle_tool(
                    pattern_store=pattern_store,
                    instance_id=instance_id, member_id=member_id, args=args,
                )
            if tool_id == "record_friction_pattern_recurrence":
                if pattern_store is None:
                    raise RuntimeError(
                        "record_friction_pattern_recurrence requires "
                        "handler._friction_pattern_store"
                    )
                return await handle_record_friction_pattern_recurrence_tool(
                    pattern_store=pattern_store,
                    instance_id=instance_id, member_id=member_id, args=args,
                )
            if tool_id == "emit_autonomy_loop_event":
                return await handle_emit_autonomy_loop_event_tool(
                    ledger=workflow_ledger,
                    instance_id=instance_id, member_id=member_id, args=args,
                )
            if tool_id == "ask_coding_session_for_workflow":
                return await handle_ask_coding_session_for_workflow(
                    instance_id=instance_id, member_id=member_id,
                    args=args, data_dir=data_dir,
                )
            if tool_id == "read_coding_session_response_for_workflow":
                return await handle_read_coding_session_response_for_workflow(
                    instance_id=instance_id, member_id=member_id,
                    args=args, data_dir=data_dir,
                )
        # Non-autonomy tools: adapt kwargs to reasoning.execute_tool's
        # signature (tool_name, tool_input, request). The CallToolAction
        # kwargs (tool_id, args, instance_id, member_id) don't map
        # cleanly to ReasoningRequest, so for now we surface a clear
        # error — bring this surface up alongside the agent-side
        # workflow authoring follow-up that registers
        # KERNEL_AUTHORING_TOOL_NAMES in reasoning.py.
        reasoning = getattr(handler, "reasoning", None)
        if reasoning is None or not hasattr(reasoning, "execute_tool"):
            raise RuntimeError(
                "CallToolAction invoked but handler.reasoning.execute_tool "
                "is unavailable — bringup-stub gap"
            )
        raise RuntimeError(
            f"CallToolAction tool_id={tool_id!r} is not in the autonomy-loop "
            f"set and the legacy reasoning.execute_tool kwarg-adaptation "
            f"path is not yet wired. See Spec 5 deferral / follow-up: "
            f"agent-side workflow tool dispatch (KERNEL_AUTHORING_TOOL_NAMES "
            f"registration)."
        )
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
    yet (e.g., PostToServiceAction's workshop service registry).
    Registration with this stub keeps the verb in the library so
    descriptors that reference it parse cleanly; invocation surfaces
    the gap.

    Spec 6 commit 5 wired AppendToLedgerAction with WorkflowLedger;
    only PostToServiceAction (workshop registry) remains stubbed.
    """
    async def _stub(*args, **kwargs):
        raise NotImplementedError(
            f"Action verb {verb!r} is registered but its production "
            f"callable hasn't been wired in C5c-bringup yet. Surface "
            f"a follow-up if you need this verb."
        )
    return _stub


def _workflow_ledger_append_adapter(workflow_ledger: "WorkflowLedger"):
    """Spec 6 commit 5: adapt WorkflowLedger.append's positional
    (instance_id, workflow_id, entry) signature to the
    AppendToLedgerAction's kwargs shape
    (workflow_id=, entry=, instance_id=)."""
    async def _append(*, workflow_id: str, entry: dict, instance_id: str = ""):
        await workflow_ledger.append(instance_id, workflow_id, entry)
    return _append


def _workflow_ledger_read_last_adapter(workflow_ledger: "WorkflowLedger"):
    """Companion adapter for AppendToLedgerAction's verifier path."""
    async def _read_last(*, workflow_id: str, instance_id: str = ""):
        return await workflow_ledger.read_last(instance_id, workflow_id)
    return _read_last


__all__ = [
    "Substrate",
    "bring_up_substrate",
    "tear_down_substrate",
]
