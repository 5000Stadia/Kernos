"""Context Space Router — LLM-based message routing.

Routes messages to context spaces using a lightweight LLM call. Reads
message meaning, recent history, space descriptions, and (per
ROUTER-EVIDENCE-V1) per-space substrate evidence — recent activity tail
and compacted Living State + Ledger entries.
"""
import json
from kernos.utils import utc_now
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from kernos.kernel.reasoning import ReasoningService
from kernos.kernel.space_candidates import list_route_candidate_spaces
from kernos.kernel.space_evidence import SpaceEvidence
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import StateStore

logger = logging.getLogger(__name__)

ROUTER_SYSTEM_PROMPT = """You are a message router for a personal AI assistant that serves the full breadth of one person's life — from professional work to personal projects, health to hobbies, finances to family, legal matters to creative pursuits. Any topic the user brings is in scope. Your job is to route each message to the right context space.

Each space lists a short static description plus, when available, evidence drawn from that space's substrate: a few recent log entries, the compacted Living State (current-truth snapshot), and the most recent Ledger entries (topical index of past compactions). When evidence contradicts a space's static description, weigh the evidence — descriptions are labels, not authoritative routing oracles.

Given the user's message, recent conversation history, and a list of context spaces, do three things:

1. TAG: Which space(s) does this message belong to? A message can belong to multiple spaces. Use space IDs from the list. If the message is about a recurring topic that doesn't yet have its own space, also include a concise snake_case topic hint (e.g., "legal_work", "kitchen_reno", "tax_prep"). Don't add a hint if the topic fits in General or an existing space.

2. FOCUS: Which single space should receive the agent's full attention right now? DEFAULT: keep the current focus unless there's a clear contextual signal pulling to a different space. Only switch when the message is clearly ABOUT a different domain's subject matter. Universal actions (calendar, time, search, memory) are not domain signals — they stay in the current space.

3. CONTINUATION: Is this an obvious continuation (short affirmation, reaction, "lol", "ok", "sounds good") that should ride conversational momentum? If yes, keep the current focus unchanged.

Rules:

HIERARCHY: Spaces form a tree. Some spaces are children of others (marked [child of: X]). Route to the RIGHT LEVEL of specificity:
- Broad domain content → parent space. From a specific client project, "What's our standard contract template?" steps up to the Legal parent. From a specific renovation, "What's the overall property budget?" steps up to the Real Estate parent.
- Specific ongoing work → child space. "Send the invoice to Martinez" steps into the Martinez-specific space. "Where did we leave off on chapter 3?" steps into the specific writing project.
- The cost of staying is low. The cost of switching wrong is high (breaks conversational context). When unsure, stay.
- Domains span all areas of life: business operations, legal work, personal finance, health, home & family, creative projects, travel, education, property management, relationships. Route to where the scope fits.

UNIVERSAL ACTIONS stay in the current space. Calendar, time, search, memory, file operations are available everywhere. "Make a calendar entry" or "search for X" from any space stays in that space.

DOMAIN-SPECIFIC WORK routes to the domain. "Send Martinez the estimate" routes to the business space. "Check my Roth IRA balance" routes to finance. The key distinction: does this message need a specific domain's data and context, or is it universal?

OTHER RULES:
- When a message signals something NEW within an existing domain ("new project", "starting fresh"), tag the parent. Let the new topic accumulate before it earns its own space.
- Ambiguity is not a domain signal. When uncertain, keep the current focus.
- A message mentioning a person or entity from one domain doesn't mean the message IS about that domain. Discussing someone casually doesn't route to their business context.
- Read the message in the context of recent history. A message after a long gap is a fresh start. A message seconds after the last one is a continuation.
- Never invent space IDs. Only use IDs from the provided space list, or snake_case topic hints for emerging topics.
"""

ROUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Space IDs this message belongs to, plus optional snake_case topic hints for emerging topics"
        },
        "focus": {
            "type": "string",
            "description": "Single space ID for the agent's main focus"
        },
        "continuation": {
            "type": "boolean",
            "description": "True if this is an obvious short continuation riding conversational momentum"
        },
        "query_mode": {
            "type": "boolean",
            "description": "True if this is an informational query about another domain "
                          "(quick question) rather than a switch into that domain. "
                          "The user wants an answer, not a context change."
        },
        "work_mode": {
            "type": "boolean",
            "description": "True if the user intends to DO WORK in another domain "
                          "(not just ask about it). 'Let's send Martinez the estimate' is work_mode. "
                          "'What was our last quote for Martinez?' is query_mode."
        },
    },
    "required": ["tags", "focus", "continuation", "query_mode", "work_mode"],
    "additionalProperties": False
}


@dataclass
class RouterResult:
    """The result of a routing decision."""
    tags: list[str]       # Space IDs (and optional topic hints) this message belongs to
    focus: str            # Space ID for the main agent's focus
    continuation: bool    # Obvious continuation — ride momentum
    query_mode: bool = False  # Quick question about another domain — don't switch
    work_mode: bool = False   # Intent to do work in another domain — route there




def _compute_gap_description(last_ts: str, now_ts: str) -> str:
    """Describe the time gap between the last message and now in human terms."""
    if not last_ts:
        return "first message"
    try:
        last = datetime.fromisoformat(last_ts)
        now = datetime.fromisoformat(now_ts)
        seconds = max((now - last).total_seconds(), 0)
        if seconds < 120:
            return f"{int(seconds)}s"
        if seconds < 3600:
            return f"{int(seconds / 60)}m"
        if seconds < 86400:
            return f"{int(seconds / 3600)}h"
        return f"{int(seconds / 86400)}d"
    except (ValueError, TypeError):
        return "unknown"


class LLMRouter:
    """Route messages to context spaces using a lightweight LLM.

    One call per message. Reads language, not keyword lists.
    Always fires — no single-space bypass.
    """

    def __init__(self, state: StateStore, reasoning: ReasoningService) -> None:
        self._state = state
        self._reasoning = reasoning

    async def route(
        self,
        instance_id: str,
        message_content: str,
        recent_history: list[dict],
        current_focus_id: str = "",
        member_id: str = "",
        *,
        candidate_spaces: list[ContextSpace] | None = None,
        space_evidence: dict[str, SpaceEvidence] | None = None,
    ) -> RouterResult:
        """Route a message. Returns RouterResult(tags, focus, continuation).

        recent_history: full metadata entries from get_recent_full().
        current_focus_id: the instance's last_active_space_id (for continuation logic).
        member_id: if provided, only route to spaces owned by this member (or legacy unowned).
        candidate_spaces: pre-computed visibility-filtered candidate set
            (ROUTER-EVIDENCE-V1). When None, the router calls
            ``list_route_candidate_spaces`` itself so legacy callers and
            tests stay green without hand-passing the candidates.
        space_evidence: per-space substrate evidence bundles
            (ROUTER-EVIDENCE-V1). When None or empty, the router falls
            back to descriptions-only — the legacy behavior.
        """
        if candidate_spaces is None:
            active_spaces = await list_route_candidate_spaces(
                self._state, instance_id, member_id=member_id,
            )
        else:
            active_spaces = list(candidate_spaces)

        # No spaces at all — nothing to route to
        if not active_spaces:
            return RouterResult(tags=[], focus="", continuation=False)

        # Build space list for the prompt (with hierarchy info + evidence blocks)
        evidence_map: dict[str, SpaceEvidence] = space_evidence or {}
        space_name_map_all = {s.id: s.name for s in active_spaces}
        space_lines = []
        for s in active_spaces:
            desc = s.description or "No description yet"
            default_marker = " [DEFAULT]" if s.is_default else ""
            hierarchy = ""
            if s.parent_id and s.parent_id in space_name_map_all:
                hierarchy = f" [child of: {space_name_map_all[s.parent_id]}]"
            elif s.depth == 0 and not s.is_default and s.space_type != "system":
                hierarchy = " [root domain]"
            space_lines.append(f"- {s.id}: {s.name}{default_marker}{hierarchy} — {desc}")
            ev = evidence_map.get(s.id)
            if ev:
                if ev.recent_tail:
                    space_lines.append("    Recent activity:")
                    for line in ev.recent_tail.splitlines():
                        space_lines.append(f"      {line}")
                if ev.living_state:
                    space_lines.append("    Living State:")
                    for line in ev.living_state.splitlines():
                        space_lines.append(f"      {line}")
                if ev.ledger_tail:
                    space_lines.append("    Recent Ledger entries:")
                    for line in ev.ledger_tail.splitlines():
                        space_lines.append(f"      {line}")
        space_descriptions = "\n".join(space_lines)

        # Build recent history with timestamps and existing tags
        space_name_map = {s.id: s.name for s in active_spaces}
        history_lines = []
        for msg in recent_history[-15:]:
            ts = msg.get("timestamp", "")
            role = msg.get("role", "")
            content = str(msg.get("content", ""))[:200]
            tags = msg.get("space_tags") or []
            tag_names = [space_name_map.get(t, t) for t in tags if t] if tags else ["(untagged)"]
            history_lines.append(f"[{ts}] ({role}) [{', '.join(tag_names)}]: {content}")

        # Temporal metadata
        now = utc_now()
        last_ts = recent_history[-1].get("timestamp", "") if recent_history else ""
        gap = _compute_gap_description(last_ts, now)
        current_focus_name = space_name_map.get(current_focus_id, "none")

        user_content = (
            f"Active spaces:\n{space_descriptions}\n\n"
            f"Recent history:\n" + ("\n".join(history_lines) if history_lines else "(no history)") + "\n\n"
            f"Time context: {now}. Gap since last message: {gap}.\n"
            f"Current focus: {current_focus_id} ({current_focus_name})\n\n"
            f"New message: {message_content}"
        )

        # Find daily space ID for fallback
        daily = next((s for s in active_spaces if s.is_default), active_spaces[0])
        daily_id = daily.id

        try:
            result_str = await self._reasoning.complete_simple(
                system_prompt=ROUTER_SYSTEM_PROMPT,
                user_content=user_content,
                output_schema=ROUTER_SCHEMA,
                max_tokens=128,
                prefer_cheap=True,
            )
            parsed = json.loads(result_str)
            tags = parsed.get("tags", [daily_id])
            focus = parsed.get("focus", daily_id)
            continuation = parsed.get("continuation", False)
            query_mode = parsed.get("query_mode", False)
            work_mode = parsed.get("work_mode", False)

            # Validate focus is a known space ID (not a topic hint)
            known_ids = {s.id for s in active_spaces}
            if focus not in known_ids:
                # Check aliases — LLM may have returned an old name
                for s in active_spaces:
                    if focus in s.aliases:
                        focus = s.id
                        break
                else:
                    focus = current_focus_id if current_focus_id in known_ids else daily_id

            # Ensure focus is in tags
            if focus not in tags:
                tags = [focus] + tags

            return RouterResult(tags=tags, focus=focus, continuation=continuation, query_mode=query_mode, work_mode=work_mode)

        except Exception as exc:
            logger.warning("LLM router failed, falling back to current focus: %s", exc)
            fallback = current_focus_id if current_focus_id else daily_id
            return RouterResult(tags=[fallback], focus=fallback, continuation=True)
