"""Assemble phase — build the seven Cognitive UI zones + tool catalog.

HANDLER-PIPELINE-DECOMPOSE. Verbatim port of ``MessageHandler._phase_assemble``.
The body is identical; only ``self.X`` references became ``handler.X``
(where ``handler = ctx.handler``).

The largest phase in the pipeline. Responsibilities (unchanged from the
monolith):
  - Space context assembly (compaction, cross-domain, system events)
  - Relational-messaging pickup (RELATIONAL-MESSAGING v5)
  - Message analyzer cohort (classification + preference detection + covenant relevance)
  - Disclosure-gate filtering of knowledge entries before STATE
  - Three-tier tool surfacing (pinned + active with eviction + catalog scan)
  - System-prompt composition (static + dynamic zones; RULES + ACTIONS cached)
  - Messages array construction (with orphan prefix, upload notifications, departure bridge)
  - Oversized user message budgeting
"""
from __future__ import annotations

import json
import logging
import os

from kernos.kernel.event_types import EventType
from kernos.kernel.events import emit_event
from kernos.messages.phase_context import PhaseContext
from kernos.utils import utc_now

logger = logging.getLogger(__name__)


_CROSS_SPACE_AWARENESS_LOOKBACK_HOURS = 24
_CROSS_SPACE_AWARENESS_MAX_ENTRIES = 5


async def _cross_space_awareness_block(
    handler, instance_id: str, active_space_id: str,
) -> str:
    """CROSS_SPACE_REQUESTS_V1 (target re-entry awareness).

    Query recent cross_space.action events that targeted the
    currently-active space and render a short block describing
    them. The agent reads this in its situation context and can
    answer "why is this here?" with target-local provenance +
    audit alone — no origin conversation pollutes target.

    Bounded: at most the 5 most recent events within the last 24h.
    Returns "" when no relevant events.
    """
    if not active_space_id:
        return ""
    if not getattr(handler, "events", None):
        return ""

    try:
        from datetime import datetime, timedelta, timezone

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(hours=_CROSS_SPACE_AWARENESS_LOOKBACK_HOURS)
        ).isoformat()
        events = await handler.events.query(
            instance_id,
            event_types=["cross_space.action"],
            after=cutoff,
            limit=50,
        )
    except Exception as exc:
        logger.debug("CROSS_SPACE_AWARENESS_QUERY_FAILED: %s", exc)
        return ""

    relevant: list = []
    for evt in events:
        payload = getattr(evt, "payload", None) or {}
        if payload.get("target_space_id") != active_space_id:
            continue
        relevant.append((evt, payload))

    if not relevant:
        return ""

    # Most-recent first, capped.
    relevant = relevant[-_CROSS_SPACE_AWARENESS_MAX_ENTRIES:]

    lines: list[str] = [
        "[CROSS_SPACE_INBOUND] This space recently received "
        "kernel-dispatched cross-space requests from origin spaces. "
        "When asked 'why is this here?' about any of the entries "
        "below, answer using only target-local provenance + audit; "
        "you do not have origin's conversation."
    ]
    for evt, payload in relevant:
        ts = getattr(evt, "timestamp", "") or ""
        action_kind = payload.get("action_kind", "")
        origin = payload.get("origin_space_id", "")
        member = payload.get("initiating_member_id", "")
        request_id = payload.get("request_id", "")
        receipt = payload.get("receipt", {}) or {}
        status = receipt.get("status", "")
        refs = receipt.get("created_refs", []) or []
        ref_summary = ", ".join(
            f"{r.get('type')}={r.get('id')}"
            for r in refs if isinstance(r, dict)
        ) or "(no refs)"
        lines.append(
            f"  - {ts[:19]} action={action_kind} status={status} "
            f"from={origin} member={member} request_id={request_id} "
            f"refs=[{ref_summary}]"
        )
    return "\n".join(lines)


async def run(ctx: PhaseContext) -> PhaseContext:
    """Phase 3: Build Cognitive UI blocks — system prompt, tools, messages."""
    handler = ctx.handler
    # Pull in block builders + templates used below. They live in handler.py
    # at module scope; import lazily to avoid the circular import.
    from kernos.messages.handler import (
        PRIMARY_TEMPLATE,
        _build_actions_block,
        _build_canvases_block,
        _build_memory_block,
        _build_now_block,
        _build_procedures_block,
        _build_results_block,
        _build_rules_block,
        _build_state_block,
        _compose_blocks,
    )

    instance_id = ctx.instance_id
    message = ctx.message
    soul = ctx.soul
    active_space = ctx.active_space
    active_space_id = ctx.active_space_id

    # Space context (compaction, cross-domain, system events, receipts)
    (
        space_messages, ctx.results_prefix, ctx.memory_prefix,
        _procedures_prefix, _canvases_prefix,
    ) = await handler._assemble_space_context(
        instance_id, ctx.conversation_id, active_space_id, active_space,
        member_id=ctx.member_id,
    )

    # RELATIONAL-MESSAGING v5: pick up any queued messages addressed to
    # the active member. This promotes pending → delivered atomically
    # and re-includes delivered-but-not-surfaced envelopes (crash
    # recovery). Messages that violate the space-hint rule are deferred
    # (handled in the dispatcher, not here).
    rm_block_text = ""
    dispatcher = handler._get_relational_dispatcher()
    if dispatcher is not None and ctx.member_id:
        try:
            # Build the recipient's current space id list for the
            # space-hint matching rule.
            _all_spaces = await handler.state.list_context_spaces(instance_id)
            _recipient_space_ids = [
                s.id for s in _all_spaces
                if s.member_id == ctx.member_id
                or s.space_type == "system"
                or not s.member_id
            ]
            ctx.relational_messages = await dispatcher.collect_pending_for_member(
                instance_id=instance_id, member_id=ctx.member_id,
                active_space_id=active_space_id,
                recipient_space_ids=_recipient_space_ids,
            )
            # Thread continuity: show recently-surfaced envelopes as
            # reference-only so the agent can reply in-thread without
            # losing the message id after the first surface.
            _recent_surfaced = await dispatcher.collect_recent_surfaced_for_member(
                instance_id=instance_id, member_id=ctx.member_id,
            )
            if ctx.relational_messages or _recent_surfaced:
                rm_block_text = handler._format_relational_messages_block(
                    ctx.relational_messages,
                    recent_surfaced=_recent_surfaced,
                )
                if ctx.trace:
                    ctx.trace.record(
                        "info", "relational_dispatch", "RM_PICKUP",
                        f"count={len(ctx.relational_messages)} "
                        f"member={ctx.member_id} space={active_space_id}",
                        phase="assemble",
                    )
        except Exception as exc:
            logger.warning("RM_PICKUP_FAILED: %s", exc)

    if rm_block_text:
        if ctx.results_prefix:
            ctx.results_prefix = rm_block_text + "\n\n" + ctx.results_prefix
        else:
            ctx.results_prefix = rm_block_text

    # CROSS_SPACE_REQUESTS_V1: target re-entry awareness. When the
    # active space has received cross-space mutations, surface the
    # most recent ones to the agent's situation context so the
    # agent can answer "why is this here?" from target-local
    # provenance + audit alone (no origin conversation context
    # needed). Bounded — 5 most recent within 24h.
    try:
        cs_block = await _cross_space_awareness_block(
            handler, instance_id, active_space_id,
        )
        if cs_block:
            if ctx.results_prefix:
                ctx.results_prefix = cs_block + "\n\n" + ctx.results_prefix
            else:
                ctx.results_prefix = cs_block
    except Exception as exc:
        logger.warning("CROSS_SPACE_AWARENESS_BLOCK_FAILED: %s", exc)

    # Emit message.received
    try:
        await emit_event(handler.events, EventType.MESSAGE_RECEIVED, instance_id, "handler",
            payload={"content": message.content, "sender": message.sender,
                     "sender_auth_level": message.sender_auth_level.value,
                     "platform": message.platform, "conversation_id": ctx.conversation_id})
    except Exception as exc:
        logger.warning("Failed to emit message.received: %s", exc)

    # Store user message
    user_content = message.content
    if not user_content or not user_content.strip():
        if ctx.upload_notifications:
            filenames = [att.get("filename", "file") for att in (message.context or {}).get("attachments", [])]
            user_content = "User uploaded: " + ", ".join(filenames) if filenames else "User uploaded a file."
        else:
            user_content = "(empty message)"
        logger.info("EMPTY_MSG_GUARD: injected content=%r for empty user message", user_content)

    # Skip persisting diagnostic commands — they shouldn't appear in conversation history
    _is_diagnostic = user_content.strip().lower().split()[0] in ("/dump", "/status", "/help", "/spaces") if user_content.strip() else False
    if not _is_diagnostic:
        user_entry = {
            "role": "user", "content": user_content,
            "timestamp": message.timestamp.isoformat(), "platform": message.platform,
            "instance_id": instance_id, "conversation_id": ctx.conversation_id,
            "space_tags": ctx.router_result.tags,
        }
        await handler.conversations.append(instance_id, ctx.conversation_id, user_entry)
        await handler.conv_logger.append(instance_id=instance_id, space_id=active_space_id,
            speaker="user", channel=message.platform, content=user_content,
            timestamp=message.timestamp.isoformat(), member_id=ctx.member_id)

    # --- Cohort agents: Message Analyzer + Covenant Query -------------------
    # Single LLM call replaces separate Preference Parser + Knowledge Shaper.
    # Four-way classification: preference | procedure | action | conversation.

    MESSAGE_ANALYSIS_SCHEMA = {
        "type": "object",
        "properties": {
            "classification": {
                "type": "string",
                "enum": ["preference", "procedure", "action", "conversation"],
                "description": (
                    "What kind of message is this? "
                    "'preference' = short behavioral rule (auto-capture as covenant). "
                    "'procedure' = multi-step workflow instructions (write to _procedures.md). "
                    "'action' = user wants something done. "
                    "'conversation' = chat, question, or continuation."
                ),
            },
            "preference": {
                "type": "object",
                "properties": {
                    "detected": {"type": "boolean"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "category": {"type": "string"},
                    "subject": {"type": "string"},
                    "action": {"type": "string"},
                    "parameters": {"type": "string", "description": "JSON-encoded parameters if any, or empty string"},
                    "scope_hint": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["detected", "confidence", "category", "subject", "action", "parameters", "scope_hint", "reasoning"],
                "additionalProperties": False,
            },
            "relevant_knowledge_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "IDs of knowledge entries relevant to this turn.",
            },
            "relevant_covenant_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "IDs of situational covenants relevant to this turn.",
            },
        },
        "required": ["classification", "preference", "relevant_knowledge_ids", "relevant_covenant_ids"],
        "additionalProperties": False,
    }

    async def _run_message_analysis(situational_covenants: list | None = None) -> dict:
        """Combined message classification + knowledge selection + preference detection + covenant relevance."""
        _empty = {"classification": "conversation", "preference": {"detected": False, "confidence": "low", "category": "", "subject": "", "action": "", "parameters": {}, "scope_hint": "", "reasoning": ""}, "relevant_knowledge_ids": [], "relevant_covenant_ids": []}
        if _is_diagnostic or not user_content.strip():
            return _empty

        # Build knowledge candidates with Bjork dual-strength ranking
        from kernos.kernel.state import compute_retrieval_strength
        all_ke = await handler.state.query_knowledge(instance_id, subject="user", active_only=True, limit=200, member_id=ctx.member_id)
        always_inject = [e for e in all_ke if e.lifecycle_archetype == "identity"]
        _never_archetypes = {"ephemeral"}
        _now_iso = utc_now()
        candidates = []
        for e in all_ke:
            if e in always_inject:
                continue
            if e.lifecycle_archetype in _never_archetypes:
                continue
            if getattr(e, "expired_at", ""):
                continue
            # Compute retrieval strength — replaces the crude _is_stale_knowledge check
            _rs = compute_retrieval_strength(e, _now_iso)
            if _rs < 0.10:
                continue  # Effectively forgotten — skip entirely
            e._retrieval_strength = _rs  # type: ignore[attr-defined]
            candidates.append(e)

        # Sort by retrieval strength (strongest first)
        candidates.sort(key=lambda e: e._retrieval_strength, reverse=True)

        # Budget cap: if over 50 candidates, drop bottom 20% by strength
        if len(candidates) > 50:
            _cutoff = int(len(candidates) * 0.8)
            _dropped = len(candidates) - _cutoff
            candidates = candidates[:_cutoff]
            logger.info("KNOWLEDGE_BUDGET: dropped=%d weakest candidates", _dropped)

        if candidates:
            logger.info("KNOWLEDGE_RANKED: candidates=%d top=%.2f bottom=%.2f",
                len(candidates), candidates[0]._retrieval_strength,
                candidates[-1]._retrieval_strength if candidates else 0)

        candidate_lines = "\n".join(
            f"- [{e.id}] \"{e.content}\" ({e.lifecycle_archetype}, strength={e._retrieval_strength:.2f})"
            for e in candidates
        ) if candidates else "(no candidates)"

        recent_context = handler._get_recent_context_summary(ctx)

        # Build situational covenant candidates for relevance selection
        covenant_lines = ""
        if situational_covenants:
            _cov_entries = []
            for c in situational_covenants[:20]:
                _desc = c.description[:80]
                _cov_entries.append(f"- [{c.id}] {c.rule_type}: \"{_desc}\"")
            covenant_lines = "\n".join(_cov_entries)

        try:
            import json as _json
            result_str = await handler.reasoning.complete_simple(
                system_prompt=(
                    "Analyze this message. Classify it, detect preferences, select relevant knowledge, "
                    "and select relevant situational covenants.\n\n"
                    "Classification:\n"
                    "- 'preference': short behavioral rule like 'always do X' or 'never ask about Y'\n"
                    "- 'procedure': multi-step workflow like 'when I eat, log it, estimate, show budget'\n"
                    "- 'action': user wants something done\n"
                    "- 'conversation': chat, question, continuation\n\n"
                    "If preference detected: fill in the preference object with category, subject, action.\n"
                    "Select knowledge entry IDs relevant to answering this message.\n"
                    "Select situational covenant IDs that apply to this turn's context. Return empty arrays for non-relevant."
                ),
                user_content=(
                    f"User message: \"{user_content[:300]}\"\n"
                    f"Recent context: {recent_context}\n\n"
                    f"Knowledge candidates:\n{candidate_lines}"
                    + (f"\n\nSituational covenants:\n{covenant_lines}" if covenant_lines else "")
                ),
                output_schema=MESSAGE_ANALYSIS_SCHEMA,
                max_tokens=256,
                prefer_cheap=True,
            )
            parsed = _json.loads(result_str)
            logger.info("MESSAGE_ANALYSIS: classification=%s pref_detected=%s knowledge=%d covenants=%d",
                parsed.get("classification", "?"),
                parsed.get("preference", {}).get("detected", False),
                len(parsed.get("relevant_knowledge_ids", [])),
                len(parsed.get("relevant_covenant_ids", [])))
            # Attach always_inject + shaped for downstream
            parsed["_always_inject"] = always_inject
            parsed["_candidates"] = candidates
            return parsed
        except Exception as exc:
            logger.warning("MESSAGE_ANALYSIS: failed: %s", exc)
            return {"classification": "conversation", "preference": {"detected": False, "confidence": "low", "category": "", "subject": "", "action": "", "parameters": {}, "scope_hint": "", "reasoning": ""}, "relevant_knowledge_ids": [], "relevant_covenant_ids": [], "_always_inject": always_inject, "_candidates": candidates}

    # Build scope chain for covenant inheritance (current + ancestors + global)
    _scope_chain = [active_space_id] if active_space_id else []
    if active_space and active_space.parent_id:
        _cur = active_space.parent_id
        _seen = {active_space_id}
        while _cur and _cur not in _seen:
            _scope_chain.append(_cur)
            _seen.add(_cur)
            _p = await handler.state.get_context_space(instance_id, _cur)
            _cur = _p.parent_id if _p and _p.parent_id else None
    space_scope = _scope_chain + [None] if _scope_chain else None

    # Query covenants first (fast JSON read), partition by tier
    all_covenants = await handler.state.query_covenant_rules(
        instance_id, context_space_scope=space_scope, active_only=True)
    _pinned_covenants = [r for r in all_covenants if r.tier != "situational"]
    _situational_covenants = [r for r in all_covenants if r.tier == "situational"]

    # Fire Message Analyzer with situational covenants as input
    analysis_result = await _run_message_analysis(
        situational_covenants=_situational_covenants)

    # Selective injection: pinned (always) + MessageAnalyzer-selected situational
    _relevant_cov_ids = set(analysis_result.get("relevant_covenant_ids", []))
    _selected_situational = [r for r in _situational_covenants if r.id in _relevant_cov_ids]
    contract_rules = _pinned_covenants + _selected_situational
    _skipped = len(_situational_covenants) - len(_selected_situational)
    logger.info("COVENANT_TIER: total=%d pinned=%d situational=%d",
        len(all_covenants), len(_pinned_covenants), len(_situational_covenants))
    logger.info("COVENANT_INJECT: pinned=%d relevant=%d skipped=%d",
        len(_pinned_covenants), len(_selected_situational), _skipped)
    if ctx.trace:
        ctx.trace.record("info", "handler", "COVENANT_INJECT",
            f"pinned={len(_pinned_covenants)} relevant={len(_selected_situational)} skipped={_skipped}",
            phase="assemble")

    # Extract preference note (commit if detected) — skip for self-directed turns
    _pref = analysis_result.get("preference", {})
    if _pref.get("detected") and _pref.get("confidence") in ("high", "medium") and not ctx.is_self_directed:
        ctx.pref_detected = True
        try:
            from kernos.kernel.preference_parser import commit_from_analysis
            pref_note = await commit_from_analysis(
                _pref, user_content, instance_id, active_space_id,
                handler.state, handler.reasoning,
                getattr(handler.reasoning, '_trigger_store', None),
            )
            if pref_note:
                if ctx.results_prefix:
                    ctx.results_prefix += "\n\n" + pref_note
                else:
                    ctx.results_prefix = pref_note
        except Exception as exc:
            logger.warning("PREF_COMMIT: failed: %s", exc)

    # Extract knowledge entries
    _relevant_ids = set(analysis_result.get("relevant_knowledge_ids", []))
    _always = analysis_result.get("_always_inject", [])
    _cands = analysis_result.get("_candidates", [])
    shaped = [e for e in _cands if e.id in _relevant_ids]
    user_knowledge_entries = _always + shaped

    # DISCLOSURE-GATE: final read-time filter before knowledge reaches STATE.
    # Catches any entry that slipped through member-scoped queries — legacy
    # entries with empty owner_member_id, cross-space injections, anything
    # another read path might have surfaced. Fail-closed, trace-logged.
    from kernos.kernel.disclosure_gate import (
        build_permission_map, filter_knowledge_entries,
    )
    _perm_map = await build_permission_map(
        getattr(handler, '_instance_db', None), ctx.member_id,
    )
    # Cache on ctx for downstream reads in the same turn (downward search etc.)
    ctx._disclosure_perm_map = _perm_map
    user_knowledge_entries = filter_knowledge_entries(
        user_knowledge_entries,
        requesting_member_id=ctx.member_id,
        permission_map=_perm_map,
        trace=ctx.trace,
    )

    # Touch injected entries — updates last_reinforced_at + reinforcement_count
    # This feeds the Bjork decay model: used entries stay accessible longer
    for _ke in shaped:
        try:
            await handler.state.update_knowledge(instance_id, _ke.id, {
                "last_reinforced_at": utc_now(),
                "reinforcement_count": getattr(_ke, 'reinforcement_count', 1) + 1,
            })
        except Exception:
            pass

    # --- Three-tier tool surfacing (TOOL-SURFACING-REDESIGN) ----------------
    #
    # KERNEL-TOOL-REGISTRY-V1 (2026-05-04): the kernel-tool schema map
    # is now derived from the canonical registrar at
    # ``kernos.kernel.kernel_tool_registry``. The hand-maintained
    # ``_all_kernel`` list (which drifted from dispatch authority by
    # ~15 tools — canvas, model diagnostics, parts of cross-space + the
    # canvas-preference flow — left dispatched-but-invisible to the
    # surfacer) is gone. Adding a new kernel tool to the registrar
    # surfaces it here automatically; the parity-pin tests at
    # ``tests/test_kernel_tool_registry_parity.py`` fail CI on any
    # drift between dispatch authority + registrar + this map.
    #
    # The retrieval-conditional include of REMEMBER_TOOL is preserved
    # for back-compat: when ``handler._retrieval`` is None the surfacer
    # still surfaces remember (it's wired into execute_tool's elif
    # chain regardless), but historically this aggregation only
    # included it when retrieval was wired. Keep the conditional to
    # match the legacy assemble shape exactly.
    from kernos.kernel.kernel_tool_registry import kernel_tool_schema_map
    from kernos.kernel.tool_catalog import (
        ALWAYS_PINNED, COMMON_MCP_NAMES, TOOL_TOKEN_BUDGET, SURFACER_SCHEMA,
        CO_SURFACING_PAIRS,
    )
    from kernos.messages.intent_classifier import classify_intent

    _kernel_tool_map: dict[str, dict] = dict(kernel_tool_schema_map())
    if not handler._retrieval:
        # Match legacy aggregation shape: when retrieval isn't wired,
        # remember was historically excluded from this map (the kernel
        # dispatch path still works but the surfacer skipped it).
        _kernel_tool_map.pop("remember", None)
    _all_kernel = list(_kernel_tool_map.values())

    # === BUDGETED TOOL WINDOW (SPEC-TOOL-WINDOW) ===
    # Two zones: PINNED (always loaded) + ACTIVE (token-budgeted, LRU eviction)

    def _schema_tokens(schema: dict) -> int:
        return len(json.dumps(schema)) // 4

    # --- Zone 1: PINNED (always loaded, never evicted) ---
    pinned_tools: list[dict] = []
    _added: set[str] = set()

    # INSTALL-FOR-STOCK-CONNECTORS Section 2 (surfacing layer):
    # disabled-service tools are filtered out of the agent's tool
    # catalog as a hard override, regardless of any other relevance
    # signal. Pre-populating _added with disabled tool names makes
    # every subsequent `if name in _added: continue` skip them, and
    # build_catalog_text(exclude=_added) excludes them from the
    # surfacer scan as well. Catalog entries themselves stay
    # registered so `kernos services list` can show them as
    # available-but-disabled.
    _disabled_tool_names: set[str] = set()
    try:
        _service_store = handler._workspace.service_state_store()
        _disabled_services = _service_store.disabled_service_ids()
        _disabled_tool_names = set(
            handler._tool_catalog.disabled_tool_names(_disabled_services)
        )
        _added.update(_disabled_tool_names)
    except Exception:  # defensive — surfacing must not fail on store errors
        logger.warning(
            "TOOL_SURFACING: failed to read service_state for disabled-filter; "
            "continuing without filter",
            exc_info=True,
        )
    # POSTURE-SURFACING-CALIBRATION-V1 (2026-05-22): emit a
    # withhold receipt per disabled-service tool. Operators can
    # grep tool.withheld_from_surface events to detect missing
    # tools blocked by configuration.
    for _disabled_name in _disabled_tool_names:
        try:
            await emit_event(
                handler.events,
                EventType.TOOL_WITHHELD_FROM_SURFACE,
                instance_id, active_space_id,
                payload={
                    "tool_name": _disabled_name,
                    "reason": "disabled_service",
                    "tier_attempted": "pinned",
                    "turn_id": getattr(ctx, "turn_id", "") or "",
                },
            )
        except Exception:
            pass  # best-effort per kernel emit convention

    def _add_tool(schema: dict) -> bool:
        name = schema.get("name", "")
        if name and name not in _added:
            _added.add(name)
            return True
        return False

    for name in ALWAYS_PINNED:
        if name in _kernel_tool_map:
            if _add_tool(_kernel_tool_map[name]):
                pinned_tools.append(_kernel_tool_map[name])
    # remember is pinned if available
    if handler._retrieval and "remember" in _kernel_tool_map:
        if _add_tool(_kernel_tool_map["remember"]):
            pinned_tools.append(_kernel_tool_map["remember"])

    _pinned_tokens = sum(_schema_tokens(t) for t in pinned_tools)

    # --- Zone 2: ACTIVE (token-budgeted, schema-weighted LRU) ---
    active_budget = TOOL_TOKEN_BUDGET - _pinned_tokens
    _tier = "common"

    # Collect candidate tools with priority scores
    # Priority: lower = keep longer. Schema-weighted LRU.
    _affordance = {}
    if active_space and isinstance(active_space.local_affordance_set, dict):
        _affordance = active_space.local_affordance_set
    _turn = getattr(handler, '_turn_counter', 0)
    handler._turn_counter = _turn + 1

    candidates: list[tuple[dict, int]] = []  # (schema, eviction_priority)

    # Session-loaded tools get priority (recently used this session)
    loaded_names = handler.reasoning.get_loaded_tools(active_space_id)

    # Common MCP tools get low priority score (preferred to keep)
    for name in COMMON_MCP_NAMES:
        if name in _added:
            continue
        schema = handler.registry.get_tool_schema(name)
        if schema and _add_tool(schema):
            tokens = _schema_tokens(schema)
            candidates.append((schema, tokens))  # low priority = keep

    # Local affordance set tools
    for name, meta in _affordance.items():
        if name in _added:
            continue
        schema = (_kernel_tool_map.get(name)
                  or handler.registry.get_tool_schema(name)
                  or handler._load_workspace_tool_schema(instance_id, name))
        if schema and _add_tool(schema):
            tokens = _schema_tokens(schema)
            turns_unused = max(1, _turn - meta.get("last_turn", 0))
            candidates.append((schema, turns_unused * tokens))

    # Session-loaded tools
    for name in loaded_names:
        if name in _added:
            continue
        schema = handler.registry.get_tool_schema(name)
        if schema and _add_tool(schema):
            tokens = _schema_tokens(schema)
            candidates.append((schema, tokens))  # recently loaded = low priority

    # Space-activated capabilities (via request_tool)
    if active_space and active_space.active_tools:
        for cap_name in active_space.active_tools:
            cap = handler.registry.get(cap_name)
            if cap and cap.tools:
                for tname in cap.tools:
                    if tname in _added:
                        continue
                    schema = handler.registry.get_tool_schema(tname)
                    if schema and _add_tool(schema):
                        candidates.append((schema, _schema_tokens(schema)))

    # System space: ensure admin tools are always in the candidate pool
    if active_space and active_space.space_type == "system":
        _SYSTEM_SPACE_TOOLS = {"manage_members", "manage_capabilities", "manage_channels", "manage_covenants", "manage_schedule"}
        for name in _SYSTEM_SPACE_TOOLS:
            if name in _added:
                continue
            schema = _kernel_tool_map.get(name)
            if schema and _add_tool(schema):
                candidates.append((schema, 0))  # highest priority in system space

    # Tier 2: Catalog scan for this turn's intent
    _msg_text = (message.content or "").strip()
    _unsurfaced = handler._tool_catalog.get_names() - _added
    if _msg_text and len(_msg_text) > 5 and _unsurfaced:
        catalog_text = handler._tool_catalog.build_catalog_text(exclude=_added)
        if catalog_text:
            # POSTURE-SURFACING-CALIBRATION-V1 (2026-05-22): local
            # intent classifier biases the ranker without an extra
            # LLM call. Empty set → no hint, preserving prior behavior.
            _intents = classify_intent(_msg_text)
            _intent_hint = ""
            if _intents:
                _intent_hint = (
                    f"\n\nThe user's intent appears to be: "
                    f"{', '.join(sorted(_intents))}. Prefer tools whose "
                    f"declared effect class matches one of these intents."
                )
            try:
                import json as _json
                scan_result = await handler.reasoning.complete_simple(
                    system_prompt=(
                        "Given the user's message, select which additional tools from the catalog "
                        "are needed. Only select tools directly relevant. Return empty array if "
                        "the loaded tools are sufficient.\n\n"
                        f"Already loaded: {sorted(_added)}"
                        + _intent_hint
                    ),
                    user_content=f"User message: \"{_msg_text[:300]}\"\n\nTool catalog:\n{catalog_text}",
                    output_schema=SURFACER_SCHEMA,
                    max_tokens=128,
                    prefer_cheap=True,
                )
                parsed_scan = _json.loads(scan_result)
                scan_tools = parsed_scan.get("tools", [])
                if scan_tools:
                    _tier = "catalog_scan"
                    for tool_name in scan_tools:
                        if tool_name in _added:
                            continue
                        # Try kernel → MCP → workspace descriptor
                        schema = _kernel_tool_map.get(tool_name) or handler.registry.get_tool_schema(tool_name)
                        if not schema:
                            schema = handler._load_workspace_tool_schema(instance_id, tool_name)
                        if schema and _add_tool(schema):
                            tokens = _schema_tokens(schema)
                            candidates.append((schema, 0))  # scan-selected = highest priority
                            handler.reasoning.load_tool(active_space_id, tool_name)
                    logger.info("TOOL_SURFACING: tier=catalog_scan selected=%s", scan_tools)
            except Exception as exc:
                logger.warning("TOOL_SURFACING: catalog scan failed: %s", exc)

    # POSTURE-SURFACING-CALIBRATION-V1 (2026-05-22): co-surfacing.
    # When one tool in a registered pair appears in the candidate
    # list, auto-promote the other before the active-zone fill loop.
    # Closes the "I can read canvas but can't write" surface gap.
    _candidate_names = {schema.get("name", "") for schema, _ in candidates}
    for _pair_a, _pair_b in CO_SURFACING_PAIRS:
        if _pair_a in _candidate_names and _pair_b not in _added:
            _pair_schema = _kernel_tool_map.get(_pair_b) or handler.registry.get_tool_schema(_pair_b)
            if _pair_schema and _add_tool(_pair_schema):
                candidates.append((_pair_schema, 0))
                logger.info("TOOL_CO_SURFACING: paired %s → %s", _pair_a, _pair_b)
        if _pair_b in _candidate_names and _pair_a not in _added:
            _pair_schema = _kernel_tool_map.get(_pair_a) or handler.registry.get_tool_schema(_pair_a)
            if _pair_schema and _add_tool(_pair_schema):
                candidates.append((_pair_schema, 0))
                logger.info("TOOL_CO_SURFACING: paired %s → %s", _pair_b, _pair_a)

    # Sort candidates by eviction priority (ascending = keep first)
    candidates.sort(key=lambda x: x[1])

    # Fill active zone within budget
    active_tools: list[dict] = []
    _active_tokens = 0
    _evicted: list[str] = []
    for schema, priority in candidates:
        tokens = _schema_tokens(schema)
        if _active_tokens + tokens <= active_budget:
            active_tools.append(schema)
            _active_tokens += tokens
        else:
            _evicted.append(schema.get("name", "?"))
            # POSTURE-SURFACING-CALIBRATION-V1: emit a withhold
            # receipt per evicted tool. Best-effort emit — surfacing
            # never fails on event-stream errors per kernel convention.
            try:
                await emit_event(
                    handler.events,
                    EventType.TOOL_WITHHELD_FROM_SURFACE,
                    instance_id, active_space_id,
                    payload={
                        "tool_name": schema.get("name", ""),
                        "reason": "evicted_for_budget",
                        "tier_attempted": "active",
                        "turn_id": getattr(ctx, "turn_id", "") or "",
                    },
                )
            except Exception:
                pass

    # Assemble final tool list: pinned first (sorted), then active (sorted)
    pinned_tools.sort(key=lambda t: t.get("name", ""))
    active_tools.sort(key=lambda t: t.get("name", ""))
    tools = pinned_tools + active_tools

    _total_tokens = _pinned_tokens + _active_tokens
    _total = len(handler._tool_catalog.get_names())
    if _evicted:
        logger.info("TOOL_EVICT: evicted=%s", _evicted)
    logger.info("TOOL_BUDGET: total=%d pinned=%d active=%d tokens=%d/%d evicted=%d",
        len(tools), len(pinned_tools), len(active_tools),
        _total_tokens, TOOL_TOKEN_BUDGET, len(_evicted))
    logger.info("TOOL_SURFACING: tier=%s surfaced=%d total_available=%d",
        _tier, len(tools), _total)
    ctx.tools = tools

    # Build system prompt blocks (Cognitive UI grammar)
    capability_prompt = handler.registry.build_tool_directory(space=active_space)

    # Inject merge note so agent knows multiple messages need addressing
    if ctx.merged_count > 1:
        merge_note = (
            f"IMPORTANT: This turn contains {ctx.merged_count} user messages "
            f"(merged from rapid input). You MUST address ALL of them in your "
            f"response. Do not skip any. Read through all the user messages in "
            f"the conversation before responding."
        )
        if ctx.results_prefix:
            ctx.results_prefix += "\n\n" + merge_note
        else:
            ctx.results_prefix = merge_note

    # Build space name map for covenant attribution
    _space_names: dict[str, str] = {}
    if active_space:
        _space_names[active_space_id] = active_space.name
    for sid in _scope_chain:
        if sid not in _space_names:
            _s = await handler.state.get_context_space(instance_id, sid)
            if _s:
                _space_names[sid] = _s.name

    # Load instance stewardship — the purpose that orients this Kernos
    _stewardship = ""
    if hasattr(handler, '_instance_db') and handler._instance_db:
        try:
            _stewardship = await handler._instance_db.get_instance_stewardship()
        except Exception:
            pass
    rules = _build_rules_block(PRIMARY_TEMPLATE, contract_rules, soul, space_names=_space_names, member_profile=ctx.member_profile, instance_stewardship=_stewardship)
    # Extract execution envelope for self-directed turns, or check for paused plan
    _exec_envelope = None
    if ctx.is_self_directed and message.context and isinstance(message.context, dict):
        _exec_envelope = message.context.get("execution_envelope")
    elif not ctx.is_self_directed:
        # Check for a paused plan the user might want to resume
        try:
            from kernos.kernel.execution import load_plan
            _paused_plan = await load_plan(
                os.getenv("KERNOS_DATA_DIR", "./data"), instance_id, active_space_id)
            if _paused_plan and _paused_plan.get("status") == "paused":
                _exec_envelope = {
                    "plan_id": _paused_plan.get("plan_id", "?"),
                    "step_id": _paused_plan.get("paused_at_step", "?"),
                    "step_description": _paused_plan.get("paused_next_description", ""),
                    "paused": True,
                    "paused_reason": _paused_plan.get("paused_reason", "unknown"),
                    "budget_steps": _paused_plan.get("budget", {}).get("max_steps", 0),
                    "steps_used": _paused_plan.get("usage", {}).get("steps_used", 0),
                }
        except Exception:
            pass
    now_block = _build_now_block(message, soul, active_space, execution_envelope=_exec_envelope, member_profile=ctx.member_profile)
    # Load relationships for STATE block injection
    _rels = []
    if ctx.member_id and hasattr(handler, '_instance_db') and handler._instance_db:
        try:
            _rels = await handler._instance_db.list_relationships(ctx.member_id)
        except Exception:
            pass
    state_block = _build_state_block(soul, PRIMARY_TEMPLATE, user_knowledge_entries, member_profile=ctx.member_profile, relationships=_rels)
    results = _build_results_block(ctx.results_prefix)
    actions = _build_actions_block(capability_prompt, message, handler._channel_registry)
    memory = _build_memory_block(ctx.memory_prefix)
    procedures = _build_procedures_block(_procedures_prefix)
    canvases = _build_canvases_block(_canvases_prefix)

    # Cache boundary: static prefix (RULES + ACTIONS) is stable across turns,
    # dynamic suffix (NOW + STATE + RESULTS + PROCEDURES + CANVASES + MEMORY)
    # changes every turn. CANVAS-V1: the canvases block sits alongside
    # procedures — cacheable-prefix-eligible, changes only when a canvas is
    # created / archived / repinned.
    ctx.system_prompt_static = _compose_blocks(rules, actions)
    ctx.system_prompt_dynamic = _compose_blocks(now_block, state_block, results, procedures, canvases, memory)
    ctx.system_prompt = _compose_blocks(ctx.system_prompt_static, ctx.system_prompt_dynamic)

    # Developer mode: inject pending errors
    instance_profile = await handler.state.get_instance_profile(instance_id)
    if instance_profile and getattr(instance_profile, 'developer_mode', False):
        error_block = handler._error_buffer.drain(instance_id)
        if error_block:
            ctx.system_prompt += "\n\n" + error_block

    # Pending trigger deliveries
    try:
        pending_triggers = await handler._trigger_store.list_all(instance_id)
        for trig in pending_triggers:
            if trig.pending_delivery:
                ctx.upload_notifications.append(
                    f"[Scheduled action result — {trig.action_description}]: {trig.pending_delivery}")
                trig.pending_delivery = ""
                await handler._trigger_store.save(trig)
    except Exception:
        pass

    # Build messages array (CONVERSATION block — carried by messages, not system prompt)
    final_user_content = message.content
    # Prepend orphaned user messages from rapid-fire input
    orphans = getattr(handler, '_orphaned_user_content', None)
    if orphans:
        prefix = "\n".join(f"(Earlier message: {o})" for o in orphans)
        final_user_content = prefix + "\n\n" + (message.content or "")
        handler._orphaned_user_content = None
    if ctx.upload_notifications:
        final_user_content = "\n".join(ctx.upload_notifications) + (
            "\n\n" + final_user_content if final_user_content else "")
    # Departure context: ephemeral bridge from departing space on switch
    departure_msg = None
    if ctx.space_switched and ctx.previous_space_id:
        departure_msg = await handler._build_departure_context(ctx, ctx.previous_space_id)

    # Budget oversized user messages — persist to file, send preview + reference
    # Same pattern as tool result budgeting. Prevents Codex payload limit failures.
    _USER_MSG_CHAR_BUDGET = 4000
    if len(final_user_content) > _USER_MSG_CHAR_BUDGET and active_space_id:
        try:
            _preview = final_user_content[:_USER_MSG_CHAR_BUDGET - 200]
            _fname = f"user_input_{utc_now().replace(':', '').replace('+', '_')[:19]}.txt"
            await handler._files.write_file(instance_id, active_space_id, _fname, final_user_content,
                description="User input (auto-persisted, oversized)")
            final_user_content = (
                f"{_preview}\n\n"
                f"[Message continues — full text saved to {_fname}. "
                f"Use read_file('{_fname}') to see the complete content.]"
            )
            logger.info("USER_MSG_BUDGETED: original=%d preview=%d file=%s",
                len(final_user_content), _USER_MSG_CHAR_BUDGET, _fname)
        except Exception as exc:
            logger.warning("USER_MSG_BUDGET: failed to persist: %s", exc)

    if departure_msg:
        ctx.messages = [departure_msg] + space_messages + [{"role": "user", "content": final_user_content}]
    else:
        ctx.messages = space_messages + [{"role": "user", "content": final_user_content}]

    # COGNITIVE-CONTEXT-V1: construct the typed cognitive substrate
    # alongside the legacy string assembly. Codex C3a-design Q2:
    # assembly owns the selected/filtered substrate; populate the
    # packet from already-loaded locals rather than re-querying.
    # Q4 (dual carry): legacy strings remain canonical for the legacy
    # path; the packet is canonical for the decoupled path.
    #
    # Codex C3b-review BLOCKER (request 003 fold): packet construction
    # was originally placed right after system_prompt build, BEFORE
    # ctx.messages was assigned. ``conversation.messages`` was wired
    # to read from ``ctx.messages or ()`` and silently came back
    # empty — false provenance (the field-provenance map said
    # "wired", reality said "always empty"). Construction now runs
    # at the END of the phase so ``ctx.messages`` is finalized.
    try:
        from kernos.kernel.cognitive_context.field_provenance import (
            PopulationContext,
            populate_packet,
        )
        from kernos.utils import utc_now_dt
        _channel_records: tuple = ()
        if handler._channel_registry is not None:
            try:
                _channel_records = tuple(
                    handler._channel_registry.get_outbound_capable()
                )
            except Exception:
                _channel_records = ()
        # C5: partition ctx.tools into the always-pinned subset
        # (matching ALWAYS_PINNED + request_tool) and the
        # surfacer-selected active zone. PresenceRenderer reads
        # tool_surface.all_tools() and passes to chain_caller's
        # tools= argument (replaces the empty list pre-C5).
        from kernos.kernel.tool_catalog import (
            ALWAYS_PINNED as _ALWAYS_PINNED,
        )
        _tool_surface_pinned: tuple = tuple(
            t for t in (ctx.tools or [])
            if isinstance(t, dict) and t.get("name") in _ALWAYS_PINNED
        )
        _tool_surface_active: tuple = tuple(
            t for t in (ctx.tools or [])
            if not (
                isinstance(t, dict) and t.get("name") in _ALWAYS_PINNED
            )
        )
        # C3b: derive sensitivity gates from the surfaced knowledge
        # entries (entries the disclosure gate already passed; this
        # field carries residual policy data the model must reason
        # about). Codex C3b-review CONCERN: filter out "open"
        # sensitivity (default classification, not actually a gate);
        # only entries with non-default classification count as
        # sensitivity gates the model must honor.
        _sensitivity_gates: tuple = ()
        if user_knowledge_entries:
            _sensitivity_gates = tuple(
                {
                    "entry_id": getattr(e, "id", ""),
                    "author_member_id": getattr(e, "author_member_id", "")
                    or getattr(e, "owner_member_id", ""),
                    "sensitivity": getattr(e, "sensitivity", "") or "",
                    "subject": getattr(e, "subject", ""),
                }
                for e in user_knowledge_entries
                if (getattr(e, "sensitivity", "") or "")
                not in ("", "open")
            )
        # C3b: cross-member rules — covenants tagged with a
        # "relationship:" context_space scope. Filtered from the
        # already-loaded contract_rules. Codex C3b-review CONCERN:
        # this only captures rules pre-selected into contract_rules;
        # if the message-analyzer's selection misses a relationship-
        # scope rule, the field misses it too. Acceptable per the
        # selection contract; explicit broader fetch is a follow-up
        # if needed.
        _cross_member_rules: tuple = tuple(
            {
                "id": r.id,
                "scope": r.context_space or "",
                "rule_type": r.rule_type,
                "description": r.description,
            }
            for r in (contract_rules or ())
            if (r.context_space or "").startswith("relationship:")
        )
        # C3b: disclosure_layer reuses the permission map already
        # built earlier in this phase (ctx._disclosure_perm_map set
        # by build_permission_map). Defensive isinstance(dict) guard:
        # some test fixtures wire a MagicMock for _instance_db whose
        # list_permissions_for returns a default MagicMock (not a
        # real dict). Without the guard, dict() on the MagicMock
        # crashes the packet build. build_permission_map itself was
        # also hardened to always return a real dict (Codex C3b-
        # review CONCERN fold).
        _raw_perm_map = getattr(ctx, "_disclosure_perm_map", None)
        _disclosure_layer = (
            _raw_perm_map if isinstance(_raw_perm_map, dict) else {}
        )
        # C3b-review BLOCKER fold: populate awareness_whispers from
        # state.get_pending_whispers (filtered by member-scope) so
        # the packet's memory.awareness_whispers field carries real
        # records — was always () with a "wired" provenance mark
        # before this fold (false provenance, exactly the drift
        # CCV1 prevents).
        _whisper_records: tuple = ()
        try:
            _pending_whispers = await handler.state.get_pending_whispers(instance_id)
            if _pending_whispers:
                _member = ctx.member_id or ""
                _whisper_records = tuple(
                    {
                        "whisper_id": getattr(w, "whisper_id", ""),
                        "insight_text": getattr(w, "insight_text", ""),
                        "delivery_class": getattr(w, "delivery_class", ""),
                        "owner_member_id": getattr(w, "owner_member_id", ""),
                        "created_at": getattr(w, "created_at", ""),
                    }
                    for w in _pending_whispers
                    if (
                        not getattr(w, "owner_member_id", "")
                        or getattr(w, "owner_member_id", "") == _member
                    )
                )
        except Exception:
            # Best-effort: legacy already filters whispers via
            # _get_pending_awareness for results_prefix; failure here
            # leaves memory.awareness_whispers empty without breaking
            # the legacy substrate path.
            _whisper_records = ()
        _pop_ctx = PopulationContext(
            instance_id=instance_id,
            member_id=ctx.member_id,
            space_id=active_space_id or "",
            state_store=handler.state,
            instance_db=getattr(handler, "_instance_db", None),
            handler=handler,
            user_timezone=(
                (ctx.member_profile or {}).get("timezone", "")
                or soul.timezone
            ),
            platform=message.platform,
            auth_level=str(message.sender_auth_level.value),
            timestamp_utc=utc_now_dt(),
            active_space_name=(
                active_space.name if active_space else ""
            ),
            member_display_name=(
                (ctx.member_profile or {}).get("display_name", "")
                or soul.user_name
            ),
            agent_name=(
                (ctx.member_profile or {}).get("agent_name", "")
                or soul.agent_name
            ),
            execution_envelope=_exec_envelope,
            member_profile=dict(ctx.member_profile or {}),
            soul=soul,
            covenants=tuple(contract_rules),
            space_names=dict(_space_names),
            instance_stewardship=_stewardship,
            relationships=tuple(_rels),
            knowledge_entries=tuple(user_knowledge_entries or ()),
            results_prefix=ctx.results_prefix or "",
            capability_prompt=capability_prompt,
            channel_registry=_channel_records,
            compaction_carry=ctx.memory_prefix or "",
            awareness_whispers=_whisper_records,
            conversation_messages=tuple(ctx.messages or ()),
            # C3b additions — procedures + canvases + safety substrate.
            procedures_prefix=_procedures_prefix or "",
            canvases_prefix=_canvases_prefix or "",
            sensitivity_gates=_sensitivity_gates,
            disclosure_layer=dict(_disclosure_layer),
            cross_member_rules=_cross_member_rules,
            # C5 additions — tool surface partitions.
            tool_surface_pinned=_tool_surface_pinned,
            tool_surface_active=_tool_surface_active,
        )
        ctx.cognitive_context = await populate_packet(_pop_ctx)
    except Exception as exc:
        # Construction of the packet is best-effort during the
        # rollout — if anything goes wrong, the legacy strings still
        # carry the substrate so the legacy path is unaffected. Log
        # so the gap surfaces in operator telemetry.
        logger.warning(
            "COGNITIVE_CONTEXT_CONSTRUCT_FAILED: %s", exc, exc_info=True,
        )
        ctx.cognitive_context = None

    return ctx
