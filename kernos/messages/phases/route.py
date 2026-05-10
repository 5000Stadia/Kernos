"""Route phase — invoke the router cohort, assign active_space_id.

HANDLER-PIPELINE-DECOMPOSE. Verbatim port of ``MessageHandler._phase_route``.
Responsibilities (unchanged from the monolith):
  - Consult the router cohort for space selection + query/work mode
  - Honor query-mode downward search → keep current focus
  - Handle space switching (session exit, fact harvest, event emission)
  - Resolve active_space and kick off lazy workspace-tool registration
  - Handle attachment notifications for file uploads
"""
from __future__ import annotations

import asyncio
import logging
import os

from kernos.kernel.event_types import EventType
from kernos.kernel.events import emit_event
from kernos.kernel.router import RouterResult
from kernos.kernel.space_candidates import list_route_candidate_spaces
from kernos.kernel.space_evidence import build_space_evidence
from kernos.messages.phase_context import PhaseContext
from kernos.utils import utc_now

logger = logging.getLogger(__name__)


async def run(ctx: PhaseContext) -> PhaseContext:
    """Phase 2: Determine context space, handle space switching, file uploads."""
    handler = ctx.handler
    instance_id = ctx.instance_id
    message = ctx.message
    conversation_id = ctx.conversation_id

    recent_full = await handler.conversations.get_recent_full(instance_id, conversation_id, limit=20)
    instance_profile = await handler.state.get_instance_profile(instance_id)
    current_focus_id = instance_profile.last_active_space_id if instance_profile else ""

    logger.info(
        "ROUTE_INPUT: message=%s recent=%d current_focus=%s",
        (message.content or "")[:80], len(recent_full), current_focus_id or "none",
    )

    # SUBSTRATE-DIAGNOSTIC SLASH-COMMAND BYPASS:
    # Diagnostic slash commands (``/dump``, ``/status``) MUST inspect
    # the same space the operator is in — otherwise the captured
    # diagnostic is for a different substrate than the conversation
    # that produced it. The router cohort treats ``/dump`` as
    # "diagnostic intent" and routes to the System space, which means
    # ``/dump`` after several conversational turns in General captures
    # the System-space substrate, NOT General's. That defeats
    # substrate inspection.
    #
    # Codex C7 deliberation 2026-05-02 surfaced this during the
    # CCV1 soak harness's probe_d_compaction run: compaction wrote
    # General-space carry context, but ``/dump`` routed to System
    # before assembly, so the dump showed an empty MEMORY zone for
    # the wrong space. This bypass keeps the operator in their
    # current space so the diagnostic captures what the conversation
    # was actually building.
    _content = (message.content or "").strip()
    _first_word = _content.split()[0].lower() if _content else ""
    _DIAGNOSTIC_BYPASS_COMMANDS = {"/dump", "/status"}
    if _first_word in _DIAGNOSTIC_BYPASS_COMMANDS and current_focus_id:
        # ROUTER-EVIDENCE-V1: the diagnostic bypass MUST short-circuit
        # before evidence build — diagnostic intent is the same regardless
        # of substrate, and loading evidence for every candidate space here
        # is wasted work that can also slow `/dump` noticeably.
        logger.info(
            "ROUTE_DIAGNOSTIC_BYPASS: cmd=%s staying in current_focus=%s "
            "(skipped router cohort + evidence build)",
            _first_word, current_focus_id,
        )
        ctx.router_result = RouterResult(
            tags=[current_focus_id],
            focus=current_focus_id,
            continuation=True,
            query_mode=False,
        )
    else:
        # ROUTER-EVIDENCE-V1: build per-space evidence bundles before the
        # router call so the cohort sees substrate-derived orientation
        # signals (recent activity tail, Living State, Ledger entries),
        # not just static descriptions. Both the candidate list and the
        # evidence build are best-effort — any failure here falls back
        # to descriptions-only routing (legacy behavior) so the router
        # cohort always gets a chance to decide.
        candidates: list = []
        evidence: dict = {}
        try:
            candidates = await list_route_candidate_spaces(
                handler.state, instance_id, member_id=ctx.member_id,
            )
            evidence = await build_space_evidence(
                conv_logger=handler.conv_logger,
                compaction=handler.compaction,
                instance_id=instance_id,
                member_id=ctx.member_id,
                candidates=candidates,
                message_content=message.content or "",
                current_focus_id=current_focus_id,
            )
        except Exception as exc:
            logger.warning(
                "ROUTER_EVIDENCE: candidate/evidence build failed, "
                "routing without evidence: %s", exc,
            )
            candidates = []
            evidence = {}
        # When candidates is empty, pass None so the router falls back to
        # its internal candidate computation (legacy behavior).
        ctx.router_result = await handler._router.route(
            instance_id, message.content, recent_full, current_focus_id,
            member_id=ctx.member_id,
            candidate_spaces=candidates if candidates else None,
            space_evidence=evidence if evidence else None,
        )

    # Query mode: quick question about another domain — stay in current space
    if ctx.router_result.query_mode and current_focus_id and ctx.router_result.focus != current_focus_id:
        target_space_ids = [
            t for t in ctx.router_result.tags
            if t != current_focus_id and not t.startswith("_")
        ]
        if target_space_ids:
            logger.info("DOWNWARD_SEARCH: query=%r target_domains=%s",
                (message.content or "")[:60], target_space_ids)
            answer = await handler._downward_search(
                instance_id, message.content or "", target_space_ids,
                requesting_member_id=ctx.member_id, trace=ctx.trace,
            )
            if answer:
                if ctx.results_prefix:
                    ctx.results_prefix += f"\n\n{answer}"
                else:
                    ctx.results_prefix = answer
        # Stay in current space regardless
        ctx.router_result = RouterResult(
            tags=ctx.router_result.tags,
            focus=current_focus_id,
            continuation=False,
            query_mode=True,
        )

    # Work mode: intentional domain-specific work — route there confidently
    if ctx.router_result.work_mode and current_focus_id and ctx.router_result.focus != current_focus_id:
        logger.info("WORK_MODE: routing to %s for domain-specific work",
            ctx.router_result.focus)

    ctx.active_space_id = ctx.router_result.focus
    ctx.previous_space_id = current_focus_id
    ctx.space_switched = (
        ctx.active_space_id != ctx.previous_space_id
        and ctx.previous_space_id != ""
        and ctx.active_space_id != ""
    )

    logger.info("USER_MSG: sender=%s full_text=%r", message.sender, message.content)
    _route_space_name = ""
    if ctx.active_space_id:
        _route_space = await handler.state.get_context_space(instance_id, ctx.active_space_id)
        _route_space_name = _route_space.name if _route_space else ""
    logger.info(
        "ROUTE: space=%s (%s) tags=%s continuation=%s prev=%s switched=%s router=llm",
        ctx.active_space_id, _route_space_name or "unknown",
        ctx.router_result.tags, ctx.router_result.continuation,
        ctx.previous_space_id, ctx.space_switched,
    )

    if ctx.space_switched:
        _prev_space = await handler.state.get_context_space(instance_id, ctx.previous_space_id)
        _prev_name = _prev_space.name if _prev_space else "unknown"
        logger.info(
            "SPACE_SWITCH: from=%s (%s) to=%s (%s)",
            ctx.previous_space_id, _prev_name,
            ctx.active_space_id, _route_space_name or "unknown",
        )
        asyncio.create_task(handler._run_session_exit(instance_id, ctx.previous_space_id, conversation_id))
        # Harvest facts from departing space
        try:
            from kernos.kernel.fact_harvest import harvest_facts
            log_text = await handler.conv_logger.read_current_log_text(instance_id, ctx.previous_space_id, member_id=ctx.member_id)
            if isinstance(log_text, tuple):
                log_text = log_text[0]
            asyncio.create_task(harvest_facts(
                handler.reasoning, handler.state, handler.events,
                instance_id, ctx.previous_space_id, log_text or "",
                data_dir=os.getenv("KERNOS_DATA_DIR", "./data"),
                member_id=ctx.member_id,
            ))
        except Exception:
            pass

    if instance_profile and ctx.active_space_id and ctx.active_space_id != ctx.previous_space_id:
        instance_profile.last_active_space_id = ctx.active_space_id
        await handler.state.save_instance_profile(instance_id, instance_profile)

    if ctx.space_switched:
        try:
            await emit_event(handler.events, EventType.CONTEXT_SPACE_SWITCHED, instance_id, "router",
                payload={"from_space": ctx.previous_space_id, "to_space": ctx.active_space_id,
                         "router_tags": ctx.router_result.tags, "continuation": ctx.router_result.continuation})
        except Exception as exc:
            logger.warning("Failed to emit context.space.switched: %s", exc)

    ctx.active_space = (
        await handler.state.get_context_space(instance_id, ctx.active_space_id)
        if ctx.active_space_id else None
    )
    if ctx.active_space and ctx.active_space_id:
        await handler.state.update_context_space(instance_id, ctx.active_space_id,
            {"last_active_at": utc_now(), "status": "active"})
        # Lazy workspace registration — ensure built tools are in the catalog
        try:
            await handler._workspace.ensure_registered(instance_id, ctx.active_space_id)
        except Exception as exc:
            logger.warning("WORKSPACE: lazy registration failed for %s: %s", ctx.active_space_id, exc)

        # Lazy catalog version promotion — scan for new tools relevant to this space
        try:
            await handler._check_catalog_version(instance_id, ctx.active_space_id, ctx.active_space)
        except Exception as exc:
            logger.warning("CATALOG_VERSION: check failed for %s: %s", ctx.active_space_id, exc)

    if message.context and ctx.active_space_id:
        for att in message.context.get("attachments", []):
            note = await handler._handle_file_upload(instance_id, ctx.active_space_id,
                att.get("filename", "upload.txt"), att.get("content", ""))
            ctx.upload_notifications.append(note)
        # Inject hard-stop directive for files that couldn't be processed
        rejected = message.context.get("rejected_files", [])
        if rejected:
            names = ", ".join(rejected)
            ctx.upload_notifications.append(
                f"[SYSTEM] Document processing failure:\n"
                f"- Files: {names}\n"
                f"- Status: unreadable (binary, unsupported format, or decode error)\n"
                f"- Extracted text: none\n\n"
                f"You do NOT have access to the contents of these files. "
                f"Do not summarize, quote, analyze, or infer their contents. "
                f"Tell the user you couldn't read the file and suggest they paste "
                f"the text directly or provide a plain-text version."
            )
    return ctx
