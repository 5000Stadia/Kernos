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
    from kernos.kernel.reasoning import REQUEST_TOOL, READ_DOC_TOOL, REMEMBER_DETAILS_TOOL, MANAGE_CAPABILITIES_TOOL
    from kernos.kernel.awareness import DISMISS_WHISPER_TOOL
    from kernos.kernel.tool_catalog import ALWAYS_PINNED, COMMON_MCP_NAMES, TOOL_TOKEN_BUDGET, SURFACER_SCHEMA

    # Build the kernel tool schema map (needed for all tiers)
    _kernel_tool_map: dict[str, dict] = {}
    from kernos.kernel.files import FILE_TOOLS
    from kernos.kernel.reasoning import READ_SOURCE_TOOL, READ_SOUL_TOOL, UPDATE_SOUL_TOOL
    from kernos.kernel.covenant_manager import MANAGE_COVENANTS_TOOL
    from kernos.kernel.channels import MANAGE_CHANNELS_TOOL, SEND_TO_CHANNEL_TOOL
    from kernos.kernel.scheduler import MANAGE_SCHEDULE_TOOL
    from kernos.kernel.tools import INSPECT_STATE_TOOL
    from kernos.kernel.code_exec import EXECUTE_CODE_TOOL
    from kernos.kernel.workspace import MANAGE_WORKSPACE_TOOL, REGISTER_TOOL_TOOL
    from kernos.kernel.execution import MANAGE_PLAN_TOOL
    from kernos.kernel.external_agents.tool import CONSULT_TOOL
    _all_kernel = FILE_TOOLS + [REQUEST_TOOL, READ_DOC_TOOL, DISMISS_WHISPER_TOOL,
                            MANAGE_CAPABILITIES_TOOL, REMEMBER_DETAILS_TOOL,
                            READ_SOURCE_TOOL, READ_SOUL_TOOL, UPDATE_SOUL_TOOL,
                            MANAGE_COVENANTS_TOOL, MANAGE_CHANNELS_TOOL,
                            SEND_TO_CHANNEL_TOOL, MANAGE_SCHEDULE_TOOL,
                            INSPECT_STATE_TOOL, EXECUTE_CODE_TOOL,
                            MANAGE_WORKSPACE_TOOL, REGISTER_TOOL_TOOL,
                            MANAGE_PLAN_TOOL, CONSULT_TOOL]
    from kernos.kernel.runtime_trace import READ_RUNTIME_TRACE_TOOL
    from kernos.kernel.diagnostics import DIAGNOSE_ISSUE_TOOL, PROPOSE_FIX_TOOL, SUBMIT_SPEC_TOOL
    from kernos.kernel.members import MANAGE_MEMBERS_TOOL
    from kernos.kernel.relational_tools import (
        SEND_RELATIONAL_MESSAGE_TOOL, RESOLVE_RELATIONAL_MESSAGE_TOOL,
    )
    _all_kernel.extend([
        READ_RUNTIME_TRACE_TOOL, DIAGNOSE_ISSUE_TOOL, PROPOSE_FIX_TOOL,
        SUBMIT_SPEC_TOOL, MANAGE_MEMBERS_TOOL,
        SEND_RELATIONAL_MESSAGE_TOOL, RESOLVE_RELATIONAL_MESSAGE_TOOL,
    ])
    if handler._retrieval:
        from kernos.kernel.retrieval import REMEMBER_TOOL
        _all_kernel.append(REMEMBER_TOOL)
    for t in _all_kernel:
        _kernel_tool_map[t["name"]] = t

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
    try:
        _service_store = handler._workspace.service_state_store()
        _disabled_services = _service_store.disabled_service_ids()
        _added.update(
            handler._tool_catalog.disabled_tool_names(_disabled_services)
        )
    except Exception:  # defensive — surfacing must not fail on store errors
        logger.warning(
            "TOOL_SURFACING: failed to read service_state for disabled-filter; "
            "continuing without filter",
            exc_info=True,
        )

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
            try:
                import json as _json
                scan_result = await handler.reasoning.complete_simple(
                    system_prompt=(
                        "Given the user's message, select which additional tools from the catalog "
                        "are needed. Only select tools directly relevant. Return empty array if "
                        "the loaded tools are sufficient.\n\n"
                        f"Already loaded: {sorted(_added)}"
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
    return ctx
