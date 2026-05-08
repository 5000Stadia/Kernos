"""Persist phase — conversation log, RM surfacing, compaction, events.

HANDLER-PIPELINE-DECOMPOSE. Verbatim port of ``MessageHandler._phase_persist``.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from kernos.kernel.event_types import EventType
from kernos.kernel.events import emit_event
from kernos.messages.phase_context import PhaseContext
from kernos.utils import utc_now

logger = logging.getLogger(__name__)


def _format_tool_receipts(
    tool_calls_trace: list[dict] | None,
    action_state_records: list | None = None,
) -> str | None:
    """Format the per-turn tool trace into the conv-log receipt block.

    Returns the formatted block (string) or None when there's nothing
    worth persisting (no entries with usable name+preview).

    RESPONSE-FIDELITY-V1 Batch 1.4 (2026-05-08, G.7 shift): block
    label became "Action state this turn" with structured per-record
    fields drawn from ActionStateRecord when available. When no
    ActionStateRecord matches a given tool_calls_trace entry (existing
    surfaces still pre-migration), falls back to the Batch 0 format:
    name + status marker + preview.

    Per Batch 0: both successful and failed tool_calls_trace entries
    persist, distinguished by ``state=completed`` / ``state=failed``
    in the rendered line. Closes cross-surface pattern C.7 from the
    Phase 1 audit ("absence of evidence is not evidence of absence"
    failure mode the agent self-identified).

    Per Batch 1.4: ActionStateRecords (currently only populated by
    note_this; other surfaces migrate Batch 2+) get their structured
    fields rendered explicitly: state, evidence_class (when set),
    affected_objects, user_visible_summary. This gives the next-turn
    agent receipt-grounded context to reason about, rather than just
    a binary success flag and a truncated string preview.
    """
    lines: list[str] = []

    # Index records as a FIFO list per operation so multiple calls to
    # the same tool in one turn each surface their own structured
    # render (codex review fold 2026-05-08: previous shape kept only
    # the first record per operation, dropping later ones — two
    # note_this calls in one turn would collapse to one receipt).
    records_by_op: dict[str, list] = {}
    for rec in (action_state_records or []):
        op = getattr(rec, "operation", "")
        if op:
            records_by_op.setdefault(op, []).append(rec)

    for tc in (tool_calls_trace or []):
        name = tc.get("name", "")
        preview = (tc.get("result_preview") or "")[:150]
        if not name:
            continue
        rec_list = records_by_op.get(name)
        if rec_list:
            # Structured render from the next ActionStateRecord in
            # FIFO order — pairs structured records 1:1 with their
            # corresponding tool_calls_trace entries when both exist.
            rec = rec_list.pop(0)
            lines.append(_render_record_line(name, rec))
            continue
        # Fallback: legacy trace-only format.
        succeeded = bool(tc.get("success"))
        if succeeded and not preview:
            continue
        status = "completed" if succeeded else "failed"
        if preview:
            lines.append(f"[{name}] state={status} | {preview}")
        else:
            lines.append(f"[{name}] state={status}")

    # Render any ActionStateRecords that didn't have a matching
    # tool_calls_trace entry (rare — would mean a record was
    # populated outside the dispatcher path).
    for op, rec_list in records_by_op.items():
        for rec in rec_list:
            lines.append(_render_record_line(op, rec))

    if not lines:
        return None
    return "Action state this turn:\n" + "\n".join(lines)


def _render_record_line(name: str, rec: object) -> str:
    """Render one ActionStateRecord into the per-line conv-log
    format. Pulls the structured fields (state, affected_objects,
    user_visible_summary, evidence_class) into a compact line."""
    state = getattr(rec, "execution_state", "unknown")
    summary = getattr(rec, "user_visible_summary", "")
    affected = getattr(rec, "affected_objects", ())
    evidence = getattr(rec, "evidence_class", "")
    parts = [f"[{name}] state={state}"]
    if affected:
        parts.append(f"objects={','.join(affected)}")
    if evidence:
        parts.append(f"evidence={evidence}")
    if summary:
        parts.append(summary[:150])
    return " | ".join(parts)


async def run(ctx: PhaseContext) -> PhaseContext:
    """Phase 6: Store messages, write to conv log, compaction, events."""
    handler = ctx.handler
    instance_id = ctx.instance_id
    message = ctx.message

    # RELATIONAL-MESSAGING v5: commit user-visible delivery.
    # For every envelope collected this turn that is still in the
    # `delivered` state (i.e., the agent did NOT call
    # resolve_relational_message with auto_handled=true), transition
    # to `surfaced`. If the agent already resolved (either auto-handled
    # or via normal surfaced→resolved), the CAS returns False and we
    # move on.
    if ctx.relational_messages:
        dispatcher = handler._get_relational_dispatcher()
        if dispatcher is not None:
            for rm in ctx.relational_messages:
                try:
                    # Re-read to pick up any mid-turn resolution.
                    current = await handler.state.get_relational_message(
                        instance_id, rm.id,
                    )
                    if current is None:
                        continue
                    if current.state != "delivered":
                        continue  # already surfaced/resolved/expired
                    await dispatcher.mark_surfaced(instance_id, rm.id)
                except Exception as exc:
                    logger.warning("RM_MARK_SURFACED_FAILED: %s", exc)

    assistant_entry = {
        "role": "assistant", "content": ctx.response_text,
        "timestamp": utc_now(), "platform": message.platform,
        "instance_id": instance_id, "conversation_id": ctx.conversation_id,
        "space_tags": ctx.router_result.tags,
    }
    await handler.conversations.append(instance_id, ctx.conversation_id, assistant_entry)
    await handler.conv_logger.append(instance_id=instance_id, space_id=ctx.active_space_id,
        speaker="assistant", channel=message.platform, content=ctx.response_text,
        member_id=ctx.member_id)

    # Log per-turn action state — effects in the world plus structured
    # per-record metadata.
    # RESPONSE-FIDELITY-V1 Batch 0 (2026-05-08): failed-attempt entries
    # persist alongside successful ones (closes C.7 from the Phase 1
    # audit). Batch 1.4 (2026-05-08, G.7 shift): block label is now
    # "Action state this turn" with structured per-record fields drawn
    # from ActionStateRecord when available; legacy fallback for
    # surfaces not yet migrated to populate records (Batch 2 onward).
    receipt_text = _format_tool_receipts(
        ctx.tool_calls_trace,
        getattr(ctx, "action_state_records", None),
    )
    if receipt_text:
        await handler.conv_logger.append(
            instance_id=instance_id, space_id=ctx.active_space_id,
            speaker="system", channel="receipt",
            content=receipt_text, member_id=ctx.member_id,
        )

    # Compaction (with concurrency guard + backoff)
    if ctx.active_space_id in handler._compacting:
        logger.info("COMPACTION: already in progress for space=%s, skipping", ctx.active_space_id)
    else:
        try:
            comp_state = await handler.compaction.load_state(instance_id, ctx.active_space_id, member_id=ctx.member_id)
            # DISCLOSURE-GATE: when the member-scoped state doesn't exist
            # yet (e.g. member routes into a space they haven't compacted
            # in before), initialize a fresh one rather than skipping.
            # Skipping here is what broke Emma's harvest after the gate
            # changes removed the lazy-migration fallback on load_state.
            if comp_state is None and ctx.member_id:
                try:
                    from kernos.kernel.compaction import (
                        CompactionState as _CS, compute_document_budget as _cdb,
                        MODEL_MAX_TOKENS as _MMT, DEFAULT_DAILY_HEADROOM as _DDH,
                    )
                    comp_state = _CS(
                        space_id=ctx.active_space_id,
                        conversation_headroom=_DDH,
                        document_budget=_cdb(_MMT, 4000, 0, _DDH),
                    )
                    await handler.compaction.save_state(
                        instance_id, ctx.active_space_id, comp_state,
                        member_id=ctx.member_id,
                    )
                    logger.info(
                        "COMPACTION_STATE_INIT: space=%s member=%s",
                        ctx.active_space_id, ctx.member_id,
                    )
                except Exception as _exc:
                    logger.warning(
                        "COMPACTION_STATE_INIT_FAILED: %s", _exc,
                    )
                    comp_state = None
            if comp_state:
                _skip = False
                if comp_state.consecutive_failures > 0 and comp_state.last_compaction_failure_at:
                    _backoff_s = min(60 * (2 ** (comp_state.consecutive_failures - 1)), 900)
                    try:
                        _last_fail = datetime.fromisoformat(comp_state.last_compaction_failure_at)
                        if (datetime.now(timezone.utc) - _last_fail).total_seconds() < _backoff_s:
                            _skip = True
                    except (ValueError, TypeError):
                        pass
                if not _skip:
                    log_info = await handler.conv_logger.get_current_log_info(instance_id, ctx.active_space_id, member_id=ctx.member_id)
                    new_tokens = log_info["tokens_est"] - log_info.get("seeded_tokens_est", 0)
                    _real_ctx = handler.reasoning.get_last_real_input_tokens(instance_id)
                    logger.info(
                        "COMPACTION_INPUT: space=%s tokens_est=%d threshold=%d real_ctx=%d",
                        ctx.active_space_id, new_tokens, comp_state.compaction_threshold, _real_ctx,
                    )
                    if new_tokens >= comp_state.compaction_threshold:
                        log_text, log_num = await handler.conv_logger.read_current_log_text(instance_id, ctx.active_space_id, member_id=ctx.member_id)
                        if log_text.strip() and ctx.active_space:
                            handler._compacting.add(ctx.active_space_id)
                            # UX signal: notify user on Discord (not SMS)
                            if message.platform == "discord":
                                try:
                                    await handler.send_outbound(
                                        instance_id, ctx.member_id, "discord",
                                        "(Compacting...)",
                                    )
                                except Exception:
                                    pass
                            try:
                                # Fact harvest is now integrated into the compaction call
                                comp_state = await handler.compaction.compact_from_log(
                                    instance_id, ctx.active_space_id, ctx.active_space, log_text, log_num, comp_state, member_id=ctx.member_id)
                                old_num, new_num = await handler.conv_logger.roll_log(instance_id, ctx.active_space_id, member_id=ctx.member_id)
                                _seed = comp_state.last_seed_depth
                                _seed_source = "adaptive" if _seed != 10 else "default"
                                await handler.conv_logger.seed_from_previous(instance_id, ctx.active_space_id, old_num, tail_entries=_seed, member_id=ctx.member_id)
                                logger.info("COMPACTION_SEED: space=%s depth=%d (%s)",
                                    ctx.active_space_id, _seed, _seed_source)
                                handler.reasoning.clear_loaded_tools(ctx.active_space_id)
                                comp_state.consecutive_failures = 0
                                comp_state.last_compaction_failure_at = ""
                                logger.info("COMPACTION_COMPLETE: space=%s source=log_%03d new_log=log_%03d",
                                    ctx.active_space_id, old_num, new_num)

                                # Rich fact harvest — sensitivity-aware reconciliation.
                                # Replaces the old FACT_HARVEST-section path (process_harvest_results)
                                # which had no sensitivity classification. Primary call extracts
                                # facts+sensitivity; secondary surfaces stewardship/insight.
                                # Failures never hide: FACT_HARVEST_OUTCOME logs every run.
                                try:
                                    from kernos.kernel.fact_harvest import harvest_facts
                                    _outcome = await harvest_facts(
                                        handler.reasoning, handler.state, handler.events,
                                        instance_id, ctx.active_space_id, log_text,
                                        data_dir=os.getenv("KERNOS_DATA_DIR", "./data"),
                                        member_id=ctx.member_id,
                                    )
                                    if ctx.trace:
                                        ctx.trace.record(
                                            "info" if _outcome.get("primary_ok") else "warning",
                                            "compaction", "FACT_HARVEST_OUTCOME",
                                            (f"adds={_outcome.get('adds', 0)} "
                                             f"updates={_outcome.get('updates', 0)} "
                                             f"reinforces={_outcome.get('reinforces', 0)} "
                                             f"primary_ok={_outcome.get('primary_ok')} "
                                             f"secondary_ok={_outcome.get('secondary_ok')}"),
                                            phase="consequence",
                                        )
                                except Exception as _hx:
                                    logger.warning("COMPACTION_HARVEST: harvest_facts failed: %s", _hx)
                                    if ctx.trace:
                                        ctx.trace.record(
                                            "error", "compaction", "FACT_HARVEST_ERROR",
                                            str(_hx)[:200], phase="consequence",
                                        )

                                # Process recurring workflows from compaction output
                                _workflows = getattr(comp_state, '_recurring_workflows', [])
                                if _workflows:
                                    try:
                                        from kernos.kernel.awareness import Whisper, generate_whisper_id
                                        for wf in _workflows:
                                            if wf.get("count", 0) >= 3:
                                                _desc = wf.get("description", "")[:100]
                                                _trigger = wf.get("trigger", "")[:60]
                                                whisper = Whisper(
                                                    whisper_id=generate_whisper_id(),
                                                    insight_text=(
                                                        f"I notice you always do this: {_desc}. "
                                                        f"Want me to write that as a procedure so it happens automatically?"
                                                    ),
                                                    delivery_class="ambient",
                                                    source_space_id=ctx.active_space_id,
                                                    target_space_id=ctx.active_space_id,
                                                    supporting_evidence=[
                                                        f"Observed {wf.get('count', 0)} times during compaction",
                                                        f"Trigger: {_trigger}" if _trigger else "No specific trigger",
                                                    ],
                                                    reasoning_trace=f"Compaction detected recurring workflow: {_desc}",
                                                    knowledge_entry_id="",
                                                    foresight_signal=f"recurring_workflow:{_desc[:40]}",
                                                    created_at=utc_now(),
                                                )
                                                await handler.state.save_whisper(instance_id, whisper)
                                                logger.info("RECURRING_WORKFLOW: desc=%r count=%d space=%s proposed=true",
                                                    _desc, wf.get("count", 0), ctx.active_space_id)
                                    except Exception as _rwx:
                                        logger.warning("RECURRING_WORKFLOW: processing failed: %s", _rwx)

                                # Process commitments from compaction output → triggers
                                _commitments = getattr(comp_state, '_follow_ups', [])
                                if _commitments:
                                    try:
                                        await handler._process_compaction_follow_ups(
                                            instance_id, ctx.active_space_id, _commitments)
                                    except Exception as _cx:
                                        logger.warning("FOLLOW_UP: processing failed: %s", _cx)

                                # Domain assessment + child briefings — async, non-blocking
                                try:
                                    import asyncio as _aio
                                    _aio.create_task(handler._assess_domain_creation(
                                        instance_id, ctx.active_space_id, ctx.active_space, comp_state))
                                    _aio.create_task(handler._produce_child_briefings(
                                        instance_id, ctx.active_space_id, ctx.active_space))
                                except Exception as _dax:
                                    logger.warning("DOMAIN_ASSESS/BRIEFING: launch failed: %s", _dax)
                            finally:
                                handler._compacting.discard(ctx.active_space_id)
                    else:
                        await handler.compaction.save_state(instance_id, ctx.active_space_id, comp_state, member_id=ctx.member_id)
        except Exception as exc:
            logger.warning("COMPACTION: failed for space=%s: %s", ctx.active_space_id, exc)
            try:
                comp_state = await handler.compaction.load_state(instance_id, ctx.active_space_id, member_id=ctx.member_id)
                if comp_state:
                    comp_state.consecutive_failures += 1
                    comp_state.last_compaction_failure_at = utc_now()
                    await handler.compaction.save_state(instance_id, ctx.active_space_id, comp_state, member_id=ctx.member_id)
            except Exception:
                pass
            handler._compacting.discard(ctx.active_space_id)

    # Emit message.sent
    try:
        await emit_event(handler.events, EventType.MESSAGE_SENT, instance_id, "handler",
            payload={"content": ctx.response_text, "conversation_id": ctx.conversation_id, "platform": message.platform})
    except Exception as exc:
        logger.warning("Failed to emit message.sent: %s", exc)

    await handler._update_conversation_summary(instance_id, ctx.conversation_id, message.platform)
    return ctx
