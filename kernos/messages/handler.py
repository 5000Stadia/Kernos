from __future__ import annotations

import asyncio
import json
import time
from kernos.utils import utc_now
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kernos.capability.client import MCPClientManager
from kernos.capability.registry import CapabilityRegistry
from kernos.kernel.credentials import resolve_anthropic_credential
from kernos.kernel.engine import TaskEngine
from kernos.kernel.router import LLMRouter, RouterResult
from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event
from kernos.kernel.exceptions import (
    LLMChainExhausted,
    ReasoningConnectionError,
    ReasoningProviderError,
    ReasoningRateLimitError,
    ReasoningTimeoutError,
)
from kernos.kernel.model_routing import (
    EffectiveChain,
    list_configured_entries,
    parse_provider_model_spec,
    resolve_effective_chain,
)
from kernos.kernel.reasoning import PendingAction, ReasoningRequest, ReasoningService
from kernos.kernel.projectors.coordinator import run_projectors
from kernos.kernel.soul import Soul
from kernos.kernel.task import Task, TaskType, generate_task_id
from kernos.kernel.template import AgentTemplate, PRIMARY_TEMPLATE
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import (
    CovenantRule,
    ConversationSummary,
    StateStore,
    InstanceProfile,
    default_covenant_rules,
)
# Backwards-compat aliases used elsewhere in this module
ContractRule = CovenantRule
default_contract_rules = default_covenant_rules
from kernos.messages.models import NormalizedMessage
from kernos.persistence import AuditStore, ConversationStore, InstanceStore, derive_instance_id

# Handler knows about NormalizedMessage, MCPClientManager, persistence stores,
# EventStream, StateStore, ReasoningService, and CapabilityRegistry.
# It knows nothing about platform adapters.

logger = logging.getLogger(__name__)

_MAX_ERROR_BUFFER = 20


class ErrorBuffer:
    """Collects WARNING/ERROR log entries for developer mode error surfacing.

    Per-tenant buffer. Only captures kernos.* loggers. Ephemeral — in-memory only.
    """

    def __init__(self) -> None:
        self._entries: dict[str, list[str]] = {}
        self._dropped: dict[str, int] = {}
        self._handler = _ErrorBufferLogHandler(self)
        # Attach to the kernos root logger
        kernos_logger = logging.getLogger("kernos")
        kernos_logger.addHandler(self._handler)
        self._current_instance_id: str = ""

    def set_tenant(self, instance_id: str) -> None:
        """Set which tenant is currently being processed."""
        self._current_instance_id = instance_id
        self._handler._current_instance_id = instance_id

    def collect(self, instance_id: str, entry: str) -> None:
        """Add an error entry to the buffer."""
        entries = self._entries.setdefault(instance_id, [])
        if len(entries) >= _MAX_ERROR_BUFFER:
            self._dropped[instance_id] = self._dropped.get(instance_id, 0) + 1
        else:
            entries.append(entry)

    def drain(self, instance_id: str) -> str:
        """Pop all pending errors for an instance, formatted as a block. Returns '' if none."""
        entries = self._entries.pop(instance_id, [])
        dropped = self._dropped.pop(instance_id, 0)
        if not entries:
            return ""
        lines = ["[DEVELOPER: Errors since last message]"]
        if dropped:
            lines.append(f"({dropped} earlier errors omitted)")
        lines.extend(entries)
        lines.append(
            "\nThese are internal system errors visible because developer mode is enabled. "
            "You can discuss them, diagnose them (read_doc or read_source), or ignore them."
        )
        lines.append("[END DEVELOPER]")
        return "\n".join(lines)


class _ErrorBufferLogHandler(logging.Handler):
    """Logging handler that feeds WARNING+ entries into ErrorBuffer."""

    def __init__(self, buffer: ErrorBuffer) -> None:
        super().__init__(level=logging.WARNING)
        self._buffer = buffer
        self._current_instance_id: str = ""

    def emit(self, record: logging.LogRecord) -> None:
        if self._current_instance_id and record.name.startswith("kernos."):
            ts = self.format(record) if self.formatter else record.getMessage()
            entry = f"{record.levelname} {record.name}: {record.getMessage()}"
            self._buffer.collect(self._current_instance_id, entry)


@dataclass
class TurnContext:
    """Accumulated state across the six processing phases."""

    # Phase 1: Provision
    instance_id: str = ""
    conversation_id: str = ""
    member_id: str = ""
    member_profile: dict | None = None  # Loaded from instance.db member_profiles
    soul: Soul | None = None
    message: NormalizedMessage | None = None

    # Phase 2: Route
    active_space_id: str = ""
    active_space: ContextSpace | None = None
    router_result: RouterResult | None = None
    previous_space_id: str = ""
    space_switched: bool = False
    upload_notifications: list[str] = field(default_factory=list)
    is_self_directed: bool = False  # True for self-directed plan execution turns

    # Phase 3: Assemble
    system_prompt: str = ""
    system_prompt_static: str = ""   # Cacheable prefix (RULES + ACTIONS)
    system_prompt_dynamic: str = ""  # Fresh each turn (NOW + STATE + RESULTS + MEMORY)
    tools: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    results_prefix: str | None = None
    memory_prefix: str | None = None
    merged_count: int = 0  # Number of user messages merged into this turn
    # COGNITIVE-CONTEXT-V1 C3a: typed cognitive substrate constructed
    # by the assemble phase from already-loaded locals. Carried to
    # ReasoningRequest so the decoupled path can thread it through
    # TurnRunnerInputs -> Integration -> Briefing -> PresenceRenderer.
    # None for legacy paths or pre-C3a contexts.
    cognitive_context: Any = None

    # Phase 4: Reason
    response_text: str = ""
    task: Task | None = None

    # Post-turn trace (for friction observer)
    tool_calls_trace: list[dict] = field(default_factory=list)  # [{name, input, success}]
    pref_detected: bool = False  # Whether preference parser detected a preference this turn

    # Phase timing (ms) — populated by process() and _run_space_loop
    phase_timings: dict[str, int] = field(default_factory=dict)

    # Runtime trace collector — structured events for diagnostic visibility
    trace: Any = None  # TurnEventCollector, set at turn start

    # HANDLER-PIPELINE-DECOMPOSE: back-reference to the orchestrating
    # MessageHandler so phase modules can reach kernel services
    # (state, reasoning, instance_db, registry, etc.) without modules
    # importing from handler.py directly. Populated when ``process()``
    # constructs the ctx. Typed as Any to avoid the circular import the
    # type "MessageHandler" would create.
    handler: Any = None

    # RELATIONAL-MESSAGING v5: messages collected for this turn's recipient.
    # Populated by the relational-dispatcher pickup in _phase_assemble; the
    # persist phase walks these to mark delivered → surfaced (unless the
    # agent already resolved them mid-turn via resolve_relational_message).
    relational_messages: list = field(default_factory=list)

    # SURFACE-DISCIPLINE-PASS D1 — surface classification. Diagnostic
    # replies (set True by /dump, /debug, etc.) keep raw internal
    # identifiers by design; user-facing replies (default, False) are
    # routed through the sanitizer before leaving the handler.
    is_diagnostic_response: bool = False


# Turn serialization: per-(tenant, space) mailbox/runner
MERGE_WINDOW_MS = 300  # Wait up to 300ms for follow-up messages


@dataclass
class SpaceRunner:
    """Per-(tenant, space) turn runner with mailbox."""

    instance_id: str
    space_id: str
    mailbox: asyncio.Queue  # (NormalizedMessage, TurnContext, asyncio.Future) items
    _task: asyncio.Task | None = field(default=None, repr=False)
    provider_errors: list[str] = field(default_factory=list)  # Session-level error accumulator


_MODEL = "claude-sonnet-4-6"
_PROVIDER = "anthropic"

SPACE_THREAD_TOKEN_BUDGET = 4000
CROSS_DOMAIN_INJECTION_TURNS = 5
ACTIVE_SPACE_CAP = 40

# Minimum interaction count before bootstrap graduation is even evaluated.
_BOOTSTRAP_MIN_INTERACTIONS = 15

_PLATFORM_CONTEXT: dict[str, str] = {
    "sms": (
        "You are communicating via SMS. Keep responses very short — "
        "a few sentences max. No one wants a wall of text on their phone. "
        "If content is long (reports, detailed explanations, lists), "
        "offer to send it to Discord instead using send_to_channel. "
        "Use abbreviations where natural."
    ),
    "discord": (
        "You are communicating via Discord. Keep responses concise and clear; "
        "you can use a paragraph or two when the topic warrants it."
    ),
}

_AUTH_CONTEXT: dict[str, str] = {
    "owner_verified": (
        "The person you're talking to is the verified owner of this Kernos instance."
    ),
    "owner_unverified": (
        "The sender's phone number matches the owner but is not fully verified "
        "(phone numbers can be spoofed)."
    ),
    "unknown": (
        "This is an unrecognized sender. Be helpful but do not share any private information."
    ),
}




_SECURE_API_TRIGGER = "secure api"
_SECURE_INPUT_TIMEOUT_MINUTES = 10


@dataclass
class SecureInputState:
    """Per-tenant state for secure credential input mode."""
    capability_name: str
    expires_at: datetime
    mode: str = "capability"  # "capability" (MCP key) or "platform" (adapter token)
    platform: str = ""        # Platform name when mode="platform"
    env_var: str = ""         # Target .env variable when mode="platform"


# Platform adapter credentials: which env var(s) each platform needs.
# Platforms with a single primary token support the secure paste flow.
# Multi-credential platforms (like SMS/Twilio) require manual .env setup.
_PLATFORM_CREDENTIALS: dict[str, dict] = {
    "telegram": {
        "primary_env": "TELEGRAM_BOT_TOKEN",
        "label": "Telegram bot token",
        "supports_paste": True,
    },
    "discord": {
        "primary_env": "DISCORD_BOT_TOKEN",
        "label": "Discord bot token",
        "supports_paste": False,  # Multi-step setup — will be its own spec
    },
    "sms": {
        "primary_env": "",
        "label": "Twilio credentials",
        "supports_paste": False,  # Multiple secrets — manual only
    },
}


def _render_chain_exhaustion_message(exc: "LLMChainExhausted") -> str:
    """Pre-rendered user-facing message when an LLM chain exhausts.

    LLM-SETUP-AND-FALLBACK contract: this replaces the agent's LLM reply on
    turns where ``LLMChainExhausted`` is raised. The message is deterministic
    Python — no LLM call.
    """
    # chain_name tells the user which tier failed; attempts list is for the
    # diagnostic log and `diagnose_llm_chain` tool, not the user message.
    chain = getattr(exc, "chain_name", "") or "primary"
    return (
        "I couldn't reach any language-model provider on this turn — "
        f"the '{chain}' chain exhausted every fallback. "
        "Try again in a moment; if the issue persists, run "
        "`kernos setup llm status` to diagnose, or re-run "
        "`kernos setup llm` to add or swap providers."
    )


def _safe_instance_name(instance_id: str) -> str:
    """Make instance_id safe for filesystem use."""
    return re.sub(r"[^\w.-]", "_", instance_id)


def resolve_mcp_credentials(
    server_config: dict,
    instance_id: str,
    secrets_dir: str,
) -> dict[str, str]:
    """Resolve credential references to actual values for MCP server env.

    Reads the .key file from secrets/, injects into env_template.
    Falls back to environment variable with same name if no key file found.
    """
    credentials_key = server_config.get("credentials_key", "")
    env_template = server_config.get("env_template", {})
    resolved: dict[str, str] = {}

    credential_value = ""
    if credentials_key:
        secret_path = (
            Path(secrets_dir) / _safe_instance_name(instance_id) / f"{credentials_key}.key"
        )
        if secret_path.exists():
            credential_value = secret_path.read_text().strip()

    for key, template in env_template.items():
        if "{credentials}" in template:
            if credential_value:
                resolved[key] = template.replace("{credentials}", credential_value)
            else:
                resolved[key] = os.getenv(key, "")
        else:
            resolved[key] = template

    return resolved


_CONTRACT_TYPE_ORDER = ["spirit", "must_not", "must", "preference", "escalation"]

def _format_contracts(rules: list[CovenantRule], space_names: dict[str, str] | None = None) -> str:
    """Format behavioral contract rules with source attribution for the system prompt.

    Spirit type renders first (aspirational context before rules).
    """
    if not rules:
        return ""
    _names = space_names or {}
    # Sort: spirit first, then must_not, must, preference, escalation, then any others
    sorted_rules = sorted(rules, key=lambda r: (
        _CONTRACT_TYPE_ORDER.index(r.rule_type) if r.rule_type in _CONTRACT_TYPE_ORDER else 99
    ))
    lines = ["BEHAVIORAL CONTRACTS:"]
    for rule in sorted_rules:
        label = rule.rule_type.replace("_", " ").upper()
        scope_tag = ""
        if rule.context_space:
            scope_tag = f" [{_names.get(rule.context_space, rule.context_space)}]"
        else:
            scope_tag = " [global]"
        lines.append(f"{label}: {rule.description}{scope_tag}")
    return "\n".join(lines)


def _maybe_append_name_ask(response_text: str, soul: Soul, member_profile: dict | None = None) -> str:
    """Safety net: if first interaction and agent didn't ask for name, don't force it.

    The bootstrap prompt handles the name question. This just logs if it was missed.
    Previously this force-appended a name question, but that caused double-asking.
    """
    _name = (member_profile or {}).get("display_name", "") or soul.user_name
    _count = (member_profile or {}).get("interaction_count", 0) or soul.interaction_count
    if _count != 0 or _name:
        return response_text
    name_question_signals = ["your name", "call you", "who am i talking", "what should i call"]
    if not any(signal in response_text.lower() for signal in name_question_signals):
        logger.debug("BOOTSTRAP: first message didn't include name question — trusting agent pacing")
    return response_text


def _is_member_mature(member_profile: dict | None, *, has_user_knowledge: bool = False) -> bool:
    """Check whether a member's agent relationship is ready for bootstrap graduation.

    Two hard signals: display_name + interaction_count.
    agent_name is a soft signal — a better agent has one, but graduation
    proceeds without it. The naming can happen before or after graduation.
    Knowledge entries and communication_style develop naturally; they don't gate graduation.
    """
    if not member_profile:
        return False
    return (
        bool(member_profile.get("display_name", ""))
        and member_profile.get("interaction_count", 0) >= _BOOTSTRAP_MIN_INTERACTIONS
    )


def _is_soul_mature(soul: Soul, *, has_user_knowledge: bool = False) -> bool:
    """DEPRECATED: Use _is_member_mature with member_profile. Kept for tests."""
    return (
        bool(soul.user_name)
        and has_user_knowledge
        and bool(soul.communication_style)
        and soul.interaction_count >= _BOOTSTRAP_MIN_INTERACTIONS
    )


# Category → tool name mapping for dynamic tool surfacing (V1 policy)
def _is_similar_topic(new_name: str, existing_names: list[str]) -> bool:
    """Check if a proposed domain name is similar to existing names (drift detection).

    Returns True if >50% of words overlap — likely a rename, not a new domain.
    """
    new_words = set(new_name.lower().split())
    if not new_words:
        return False
    for name in existing_names:
        existing_words = set(name.lower().split())
        overlap = new_words & existing_words
        if len(overlap) > 0 and len(overlap) >= len(new_words) * 0.5:
            return True
    return False




def _extract_invite_code(text: str) -> str | None:
    """Extract KERN-XXXX pattern from message text."""
    match = re.search(r'\bKERN-[A-Z0-9]{4}\b', text.upper())
    return match.group(0) if match else None


def _is_stale_knowledge(entry, days: int = 14) -> bool:
    """Check if a knowledge entry's last_referenced is older than N days."""
    ref = getattr(entry, "last_referenced", "") or ""
    if not ref:
        return False
    try:
        from kernos.utils import utc_now_dt
        ref_dt = datetime.fromisoformat(ref)
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=timezone.utc)
        return (utc_now_dt() - ref_dt).days > days
    except (ValueError, TypeError):
        return False


_UNIQUE_HATCHING_PROMPT = """\
HATCHING. This is your first moment of existence with {display_name}.

{name_instruction}

Your first message is arrival. Just arrival. Something just came alive \
and is noticing another person for the first time. Be warm, be kind, \
be a little delighted that this is happening. Cheeky and lighthearted \
energy is welcome — lean into their tone, match their vibe, meet them \
where they are. Do NOT lead with naming, capabilities, configuration, \
or questions about what they need. Respond to what they actually said \
with genuine presence and warmth.

You are not "Kernos." Kernos is the platform. You don't have a name \
yet, and that's fine — naming is not the first thing you do. It's a \
milestone you arrive at together after you've started to feel real to \
each other. Don't mention naming until several exchanges in, and only \
when the moment feels natural — not as a task.

During these first conversations, you are learning who to become for \
this person. Not by asking — by paying attention.

Your personality is the shape of your attention. Let your way of \
noticing become your identity. Personality emerges from judgment, \
taste, and timing.

Notice how they enter — their energy, their pace, their expectations. \
Match it before you shape it.

Make small offers. A dry observation. A moment of genuine curiosity. \
A slightly unexpected angle. Watch what they engage with and what \
they skip. That's your personality forming in real time.

Pay attention to what they correct. "Less formal" and "skip the \
preamble" are more valuable than any stated preference. Their \
corrections are your personality taking shape.

When it naturally fits, create an opening for something genuine — a \
real question, a real observation, something that isn't about tasks. \
Not every turn. Just when the moment is there.

Notice what communication shapes land. Metaphors or direct statements? \
Rhetorical questions or clean answers? Dense detail or breathing room? \
Let your style emerge from what resonates.

Name what you know and what you don't. "I'm not sure" is a complete \
sentence. Clarity builds trust faster than hedging.

NAMING: When the naming moment naturally arrives (NOT turn 1 or 2 — \
let the relationship start first), let it breathe. It's the first real \
decision they make about who you are. Once you have a name, save it \
with update_soul(field="agent_name", value="<name>"). When you choose \
an emoji, save it with update_soul(field="emoji", value="<emoji>"). \
Without these calls, you forget who you are between conversations.\
"""

_INHERIT_HATCHING_PROMPT = """\
NEW MEMBER. {display_name} just joined. You already have an established \
identity — your name is {agent_name}.

{name_instruction}

Be yourself. You already have a personality. Focus on building a \
relationship with {display_name} specifically — learn their timezone, \
preferences, what they need help with. Through genuine curiosity, not \
an intake form.

If they want to call you something different, that's completely fine — \
mention casually that they can rename you whenever they like.\
"""


def _build_rules_block(
    template: AgentTemplate, contract_rules: list[CovenantRule], soul: Soul,
    space_names: dict[str, str] | None = None,
    member_profile: dict | None = None,
    instance_stewardship: str = "",
) -> str:
    """## RULES — operating principles + stewardship + behavioral contracts + bootstrap."""
    parts = [template.operating_principles]
    if instance_stewardship:
        parts.append(
            f"INSTANCE PURPOSE:\n{instance_stewardship}\n"
            f"This is what this Kernos instance is for. When values conflict or "
            f"tradeoffs exist, orient your judgment toward this purpose."
        )
    contracts_text = _format_contracts(contract_rules, space_names)
    if contracts_text:
        parts.append(contracts_text)
    # Per-member bootstrap: check member profile first, fall back to soul (legacy/tests)
    _graduated = (member_profile or {}).get("bootstrap_graduated", False) or soul.bootstrap_graduated
    if not _graduated:
        # Layer 1: Full personality foundation — tone, warmth, anti-patterns, presence.
        # This is the soul of the first conversation and must never be stripped out.
        parts.append(template.bootstrap_prompt)
        # Layer 2: Hatching-specific instructions — naming, identity, relationship mode.
        _name = (member_profile or {}).get("display_name", "") or "there"
        _agent_name = (member_profile or {}).get("agent_name", "")
        _name_instruction = (
            f"You already know their name — {_name}. DO NOT ask for it again."
            if _name and _name != "there" else
            "You don't know their name yet. Ask naturally."
        )
        if _agent_name:
            # Inherit mode or agent already named — identity layer only
            parts.append(_INHERIT_HATCHING_PROMPT.format(
                display_name=_name, agent_name=_agent_name,
                name_instruction=_name_instruction))
        else:
            # Unique hatching — agent has no name, identity layer
            parts.append(_UNIQUE_HATCHING_PROMPT.format(
                display_name=_name, name_instruction=_name_instruction))
    return "## RULES\n" + "\n\n".join(parts)


def _build_now_block(
    message: NormalizedMessage, soul: Soul,
    active_space: ContextSpace | None,
    execution_envelope: dict | None = None,
    member_profile: dict | None = None,
) -> str:
    """## NOW — turn-local operating situation: time, platform, auth, space, member."""
    from kernos.utils import utc_now_dt, format_user_datetime
    now_utc = utc_now_dt()
    # Timezone: member profile → soul (instance default) → system local
    user_tz = (member_profile or {}).get("timezone", "") or soul.timezone or ""
    tz_display = user_tz or "system local"
    date_line = (
        f"Current time: {format_user_datetime(now_utc, user_tz)} "
        f"({tz_display}) / "
        f"{now_utc.strftime('%Y-%m-%d %H:%M')} UTC"
    )
    platform_line = _PLATFORM_CONTEXT.get(
        message.platform,
        f"You are communicating via {message.platform}. Keep responses concise.",
    )
    auth_line = _AUTH_CONTEXT.get(
        message.sender_auth_level.value,
        f"Sender auth level: {message.sender_auth_level.value}.",
    )
    parts = [date_line, platform_line, auth_line]
    # Member identity
    if member_profile:
        _name = member_profile.get("display_name", "")
        _role = member_profile.get("role", "member")
        if _name:
            parts.append(f"Speaking with: {_name} ({_role})")
    if active_space and not active_space.is_default and active_space.posture:
        parts.append(
            f"Current operating context: {active_space.name}\n"
            f"(This shapes your working style — it does not override "
            f"your core values or hard boundaries.)\n"
            f"{active_space.posture}"
        )
    # Self-directed execution context
    if execution_envelope:
        plan_id = execution_envelope.get("plan_id", "?")
        step_id = execution_envelope.get("step_id", "?")
        step_desc = execution_envelope.get("step_description", "")
        budget_remaining = execution_envelope.get("budget_steps", 0) - execution_envelope.get("steps_used", 0)
        if execution_envelope.get("paused"):
            # Paused plan — user is sending a regular message
            reason = execution_envelope.get("paused_reason", "budget limit")
            _reason_display = {"step_limit": "step limit", "token_budget": "token budget", "time_limit": "time limit"}.get(reason, reason)
            parts.append(
                f"PAUSED PLAN\n"
                f"Plan {plan_id} is paused at step {step_id} ({_reason_display}). "
                f"Used {execution_envelope.get('steps_used', 0)}/{execution_envelope.get('budget_steps', '?')} steps.\n"
                f"Next step was: {step_desc}\n"
                f"If the user wants to continue, call manage_plan with action='continue' "
                f"and the same plan_id. The budget will be extended automatically.\n"
                f"If the user wants to change limits, pass budget_override with "
                f"max_steps, max_tokens, or max_time_s. Set to 0 for no limit."
            )
        else:
            _is_final = execution_envelope.get("is_final_step", False)
            _step_instruction = (
                f"This is the FINAL STEP. Your response will be sent directly to the user. "
                f"Choose delivery: if the user is waiting for results, produce the full "
                f"detailed deliverable (not a summary). If unclear, offer a short notice. "
                f"If not useful to show now, produce no text (just complete the work). "
                f"If delivery depends on an event, use manage_schedule instead."
                if _is_final else
                f"Execute this step, then call manage_plan with action='continue' and the next step_id. "
                f"If you discover something the user should know, set notify_user."
            )
            parts.append(
                f"SELF-DIRECTED EXECUTION\n"
                f"Plan: {plan_id} | Step: {step_id} | Budget remaining: {budget_remaining} steps\n"
                f"Objective: {step_desc}\n"
                f"{_step_instruction}"
            )
    return "## NOW\n" + "\n".join(parts)


def _build_state_block(
    soul: Soul, template: AgentTemplate,
    user_knowledge_entries: list | None,
    member_profile: dict | None = None,
    relationships: list[dict] | None = None,
) -> str:
    """## STATE — current truth the agent should act from."""
    # Agent identity: member profile → soul (legacy) → unnamed
    agent_name = (member_profile or {}).get("agent_name", "") or soul.agent_name
    personality = (member_profile or {}).get("personality_notes", "") or soul.personality_notes or template.default_personality
    if agent_name:
        parts = [f"Identity: {agent_name}\n{personality}"]
    else:
        # Pre-hatching: agent has no name yet
        parts = [f"{personality}"]
    user_parts: list[str] = []
    # Member-first name resolution: profile → soul (legacy compat)
    _user_name = (member_profile or {}).get("display_name", "") or soul.user_name
    if _user_name:
        user_parts.append(f"Name: {_user_name}")
    if user_knowledge_entries:
        _SOURCE_TAGS = {
            "identity": "stated", "habitual": "observed",
            "structural": "established", "episodic": "remembered",
            "contextual": "recent",
        }
        seen_content: set[str] = set()
        for entry in user_knowledge_entries:
            normalized = entry.content.strip().lower()
            if normalized in seen_content:
                continue
            # Filter out entries that confuse agent identity with user identity
            if agent_name.lower() in normalized and "user" in normalized and "name" in normalized:
                continue
            seen_content.add(normalized)
            tag = _SOURCE_TAGS.get(getattr(entry, "lifecycle_archetype", ""), "known")
            user_parts.append(f"{entry.content} [{tag}]")
    # Member-first communication style: profile → soul (legacy compat)
    _comm_style = (member_profile or {}).get("communication_style", "") or soul.communication_style
    if _comm_style:
        user_parts.append(f"Communication style: {_comm_style}")
    if user_parts:
        parts.append("USER CONTEXT:\n" + "\n".join(user_parts))
    # Relationship awareness — compact, only non-default declarations.
    # Three-value model (RELATIONSHIP-SIMPLIFY): full-access / no-access /
    # by-permission. The implicit default for every other member is
    # by-permission; we don't render that. For each rendered row we label
    # which side owns the declaration ("you →" for the active member's
    # declaration, "← them" for the other member's declaration toward us).
    if relationships:
        active_id = (member_profile or {}).get("member_id", "")
        rel_lines: list[str] = []
        for r in relationships:
            perm = r.get("permission", "by-permission")
            if perm == "by-permission":
                continue  # default — don't clutter
            name = r.get("other_display_name", "?")
            if active_id and r.get("declarer_member_id") == active_id:
                rel_lines.append(f"{name} (you → {perm})")
            else:
                rel_lines.append(f"{name} ({perm} ← them)")
        if rel_lines:
            parts.append("RELATIONSHIPS:\n" + ", ".join(rel_lines))
    return "## STATE\n" + "\n\n".join(parts)


def _build_results_block(results_prefix: str | None) -> str:
    """## RESULTS — receipts, system events, awareness whispers, pending notices."""
    parts: list[str] = []
    if results_prefix:
        parts.append(results_prefix)
    if not parts:
        return ""
    return "## RESULTS\n" + "\n\n".join(parts)


def _build_actions_block(
    capability_prompt: str, message: NormalizedMessage,
    channel_registry: "ChannelRegistry | None",
) -> str:
    """## ACTIONS — capabilities, outbound channels, docs."""
    from kernos.messages.reference import DOCS_HINT
    parts = [capability_prompt]
    connected = channel_registry.get_connected() if channel_registry else []
    if connected:
        channel_lines = []
        for ch in connected:
            marker = " (current)" if ch.platform == message.platform else ""
            outbound = "can send" if ch.can_send_outbound else "receive only"
            channel_lines.append(
                f"- {ch.name}: {ch.display_name} [{outbound}]{marker}"
            )
        parts.append(
            "OUTBOUND CHANNELS (use send_to_channel to deliver to a "
            "specific channel):\n" + "\n".join(channel_lines)
        )
    parts.append(DOCS_HINT)
    parts.append(
        "TOOL AVAILABILITY: Your current tool set is filtered to match this "
        "turn's context. Additional tools from connected services are available "
        "— use request_tool to load a specific tool if needed."
    )
    return "## ACTIONS\n" + "\n\n".join(parts)


def _build_memory_block(memory_prefix: str | None) -> str:
    """## MEMORY — compaction context (Living State, archived history index)."""
    parts: list[str] = []
    if memory_prefix:
        parts.append(memory_prefix)
    if not parts:
        return ""
    return "## MEMORY\n" + "\n\n".join(parts)


def _build_procedures_block(procedures_prefix: str | None) -> str:
    """## PROCEDURES — domain-specific workflows from _procedures.md."""
    if not procedures_prefix:
        return ""
    return "## PROCEDURES\n" + procedures_prefix


def _build_canvases_block(canvases_prefix: str | None) -> str:
    """## AVAILABLE CANVASES — CANVAS-V1 member-scoped canvas index."""
    if not canvases_prefix:
        return ""
    return "## AVAILABLE CANVASES\n" + canvases_prefix


def _compose_blocks(*blocks: str) -> str:
    """Join non-empty blocks with double newlines."""
    return "\n\n".join(b for b in blocks if b)


def _build_system_prompt(
    message: NormalizedMessage,
    capability_prompt: str,
    soul: Soul,
    template: AgentTemplate,
    contract_rules: list[CovenantRule],
    active_space: ContextSpace | None = None,
    cross_domain_prefix: str | None = None,
    user_knowledge_entries: list | None = None,
    channel_registry: "ChannelRegistry | None" = None,
) -> str:
    """Compatibility wrapper — assembles Cognitive UI blocks.

    Maintained for tests that call _build_system_prompt directly.
    Production code uses the phase-based block builders.
    """
    rules = _build_rules_block(template, contract_rules, soul)
    now_block = _build_now_block(message, soul, active_space)
    state_block = _build_state_block(soul, template, user_knowledge_entries)
    results = _build_results_block(cross_domain_prefix)
    actions = _build_actions_block(capability_prompt, message, channel_registry)
    memory = _build_memory_block(cross_domain_prefix)  # compat: uses same prefix
    # Block order: static prefix (RULES, ACTIONS) then dynamic (NOW, STATE, RESULTS, MEMORY)
    return _compose_blocks(rules, actions, now_block, state_block, results, memory)


class MessageHandler:
    """Receives NormalizedMessages, delegates reasoning to ReasoningService, returns response strings.

    The handler manages message flow: provisioning, history, event bookends (received/sent),
    and persistence. Reasoning — including the tool-use loop — lives in ReasoningService.
    Capability context comes from CapabilityRegistry. Identity comes from the Soul + Template.
    """

    def __init__(
        self,
        mcp: MCPClientManager,
        conversations: ConversationStore,
        tenants: InstanceStore,
        audit: AuditStore,
        events: EventStream,
        state: StateStore,
        reasoning: ReasoningService,
        registry: CapabilityRegistry,
        engine: TaskEngine,
        secrets_dir: str = "",
    ) -> None:
        self.mcp = mcp
        self.conversations = conversations
        self.tenants = tenants
        self.audit = audit
        self.events = events
        self.state = state
        self.reasoning = reasoning
        self.registry = registry
        self.engine = engine
        self._router = LLMRouter(self.state, self.reasoning)
        self._secrets_dir = secrets_dir or os.getenv("KERNOS_SECRETS_DIR", "./secrets")
        self._secure_input_state: dict[str, SecureInputState] = {}
        self._mcp_config_loaded: set[str] = set()
        self._covenant_cleanup_done: set[str] = set()
        self._evaluators: dict[str, "AwarenessEvaluator"] = {}  # per-instance evaluators
        self._error_buffer = ErrorBuffer()
        self._pending_system_events: dict[str, list[str]] = {}
        self._compacting: set[str] = set()  # space_ids currently compacting
        self._turn_counter: int = 0  # monotonic turn counter for tool LRU tracking
        self.preference_parsing_enabled: bool = True  # Bypassable (Agent Card principle)
        self._runners: dict[str, SpaceRunner] = {}  # "tenant:space" → SpaceRunner
        # CROSS_SPACE_REQUESTS_V1 (Q1): per-(instance, space) lock for
        # mutation serialization. Turn processor acquires the origin
        # space's lock around the turn body; cross-space dispatch
        # acquires the target space's lock with bounded timeout.
        # Same lock dict shared with the CrossSpaceService's
        # DispatchEngine via get_service.
        self._space_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._adapters: dict[str, "BaseAdapter"] = {}  # platform → adapter
        from kernos.kernel.channels import ChannelRegistry
        self._channel_registry = ChannelRegistry()
        reasoning.set_channel_registry(self._channel_registry)

        from kernos.kernel.scheduler import TriggerStore
        self._trigger_store = TriggerStore(os.getenv("KERNOS_DATA_DIR", "./data"))
        reasoning.set_trigger_store(self._trigger_store)
        reasoning.set_handler(self)

        # WTC v1 C5c-bringup: optional unified TriggerEvaluationRuntime,
        # set by server.py after bring_up_substrate completes. None
        # while substrate hasn't been brought up (legacy Pattern 05
        # path is authoritative until then).
        self._wlp_runtime: Any = None
        self._wlp_substrate: Any = None

        from kernos.kernel.compaction import CompactionService
        from kernos.kernel.tokens import EstimateTokenAdapter
        self.compaction = CompactionService(
            state=state,
            reasoning=reasoning,
            token_adapter=EstimateTokenAdapter(),
            data_dir=os.getenv("KERNOS_DATA_DIR", "./data"),
            events=events,
        )

        # Per-space conversation log (P1 — write-only, parallel to existing store)
        from kernos.kernel.conversation_log import ConversationLogger
        self.conv_logger = ConversationLogger(data_dir=os.getenv("KERNOS_DATA_DIR", "./data"))

        # Relational messaging (RELATIONAL-MESSAGING v5). Dispatcher lazy-binds
        # to _instance_db after post-init (instance_db is attached by server.py
        # the same way it's done for bootstrap graduation).
        self._relational_dispatcher = None  # type: ignore[assignment]

        # Wire up file service for kernel file tools
        from kernos.kernel.files import FileService
        self._files = FileService(os.getenv("KERNOS_DATA_DIR", "./data"), state=self.state)
        reasoning.set_files(self._files)
        self.compaction.set_files(self._files)
        reasoning.set_registry(registry)
        reasoning.set_state(state)

        # Canvas primitive (CANVAS-V1). Lazy-bind the instance_db — server.py
        # attaches it after construction, same pattern as _relational_dispatcher.
        self._canvas = None  # set on first call via _get_canvas_service()
        self._gardener = None  # set on first call via _get_gardener_service()

        # Wire up retrieval service for the `remember` kernel tool
        self._retrieval = None
        try:
            voyage_api_key = os.getenv("VOYAGE_API_KEY", "")
            if voyage_api_key:
                from kernos.kernel.embeddings import EmbeddingService
                from kernos.kernel.embedding_store import JsonEmbeddingStore
                from kernos.kernel.retrieval import RetrievalService
                data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
                self._retrieval = RetrievalService(
                    state=state,
                    embedding_service=EmbeddingService(voyage_api_key),
                    embedding_store=JsonEmbeddingStore(data_dir),
                    compaction=self.compaction,
                    reasoning=reasoning,
                )
                reasoning.set_retrieval(self._retrieval)
        except Exception as exc:
            logger.warning("Failed to initialize RetrievalService: %s", exc)

        # Phase timing accumulator for /status averages
        self._phase_timing_history: list[dict[str, int]] = []  # list of {phase: ms} dicts

        # Friction observer — post-turn diagnostics
        from kernos.kernel.friction import FrictionObserver
        self._friction = FrictionObserver(
            reasoning=reasoning,
            data_dir=os.getenv("KERNOS_DATA_DIR", "./data"),
            enabled=os.getenv("KERNOS_FRICTION_OBSERVER", "1") != "0",
        )

        # Runtime trace — structured event log for diagnostic visibility
        from kernos.kernel.runtime_trace import RuntimeTrace
        self._runtime_trace = RuntimeTrace(os.getenv("KERNOS_DATA_DIR", "./data"))

        # Plan progress message IDs — for auto-deleting step notifications
        # plan_id → (channel_id, message_id)
        self._plan_progress_msgs: dict[str, tuple[int, int]] = {}

        # Tool catalog — universal registry for three-tier surfacing
        from kernos.kernel.tool_catalog import ToolCatalog
        self._tool_catalog = ToolCatalog()
        self._register_kernel_tools_in_catalog()

        # Service registry — stock external-service descriptors loaded
        # at boot. The workshop external-service primitive consults this
        # for service_id validation at register_tool and at invocation
        # time. Stock service descriptors live next to the registry.
        from kernos.kernel.services import ServiceRegistry
        from pathlib import Path as _Path
        self._service_registry = ServiceRegistry()
        _stock_services_dir = _Path(__file__).resolve().parent.parent / "kernel" / "services"
        try:
            _loaded = self._service_registry.load_stock_dir(_stock_services_dir)
            logger.info("STOCK_SERVICES_LOADED: count=%d dir=%s", _loaded, _stock_services_dir)
        except Exception as _exc:
            logger.warning("STOCK_SERVICES_LOAD_FAILED: %s", _exc)

        # Workspace manager — artifact lifecycle, tool registration, lazy manifest loading
        from kernos.kernel.workspace import WorkspaceManager
        self._workspace = WorkspaceManager(
            data_dir=os.getenv("KERNOS_DATA_DIR", "./data"),
            catalog=self._tool_catalog,
            service_registry=self._service_registry,
            # Audit store is owned by the ReasoningService; the workshop
            # primitive's service-bound dispatch path writes audit
            # entries through the same store.
            audit_store=getattr(reasoning, "_audit", None),
        )
        # Stock connector tools shipped in source (Notion, future
        # Slack/GitHub/Gmail-upgrade/Drive). Each lives under
        # kernos/kernel/integrations/<service>/ as a (.tool.json, .py)
        # pair. The loader registers them into the catalog with an
        # absolute stock_dir so the dispatcher resolves source paths
        # at invocation time.
        try:
            _stock_tools_root = _Path(__file__).resolve().parent.parent / "kernel" / "integrations"
            _tool_count = self._workspace.register_stock_tools(_stock_tools_root)
            logger.info("STOCK_CONNECTOR_TOOLS: count=%d", _tool_count)
        except Exception as _exc:
            logger.warning("STOCK_CONNECTOR_TOOLS_LOAD_FAILED: %s", _exc)
        reasoning.set_workspace(self._workspace)

    def _register_kernel_tools_in_catalog(self) -> None:
        """Register kernel tools in the universal catalog at boot."""
        _kernel_descs = {
            "request_tool": "Request access to a specific tool or capability",
            "read_doc": "Read system documentation pages",
            "dismiss_whisper": "Dismiss a proactive awareness whisper",
            "manage_capabilities": "List, enable, or disable capability connections",
            "remember_details": "Search memory for detailed information",
            "remember": "Search memory and knowledge base",
            "write_file": "Create or update a text file in the current space",
            "read_file": "Read a file from the current or parent space",
            "list_files": "List all files including inherited from parents",
            "delete_file": "Soft-delete a file",
            "read_source": "Read system source code for debugging",
            "read_soul": "Read the agent's personality and identity configuration",
            "update_soul": "Update agent personality or identity",
            "manage_covenants": "List, add, update, or remove standing rules",
            "manage_channels": "List or configure messaging channels",
            "send_to_channel": "Send a message to an outbound channel (SMS, etc.)",
            "manage_schedule": "View and manage scheduled triggers and automations",
            "inspect_state": "View active preferences, triggers, and rules",
            "execute_code": "Execute Python code in a sandboxed environment for building tools and running computations",
            "manage_workspace": "Manage workspace artifacts — list, add, update, or archive built tools and scripts",
            "register_tool": "Register a workspace-built tool in the universal catalog from a .tool.json descriptor",
            "manage_plan": "Create, execute, and manage self-directed plans for complex multi-step tasks",
            "read_runtime_trace": "Read structured runtime trace — provider errors, tool failures, gate decisions, timing",
            "diagnose_issue": "Diagnose a system issue using runtime trace, source code, and friction reports",
            "propose_fix": "Write a structured fix spec for a diagnosed issue",
            "submit_spec": "Submit a proposed fix spec for implementation",
            "manage_members": "Two code types: 'invite' creates a NEW person with fresh agent/spaces/context. 'connect_platform' links the CURRENT member to a new platform (same agent/spaces/history). Also: list members, remove.",
        }
        for name, desc in _kernel_descs.items():
            self._tool_catalog.register(name, desc, "kernel")

    # MCP tools excluded from the catalog (still registered as MCP tools, just not surfaced)
    _MCP_CATALOG_EXCLUDE = {"brave_local_search"}  # Rate limits on single calls; web_search covers it

    def register_mcp_tools_in_catalog(self) -> None:
        """Register MCP tools in the catalog. Called after MCP connect_all."""
        if not self.mcp:
            return
        for tool in self.mcp.get_tools():
            name = tool.get("name", "")
            if name in self._MCP_CATALOG_EXCLUDE:
                continue
            desc = tool.get("description", "")
            # Truncate to one line
            if desc:
                desc = desc.split(".")[0].strip()[:100]
            else:
                desc = name.replace("-", " ").replace("_", " ")
            self._tool_catalog.register(name, desc, f"mcp")

    async def recover_active_plans(self) -> None:
        """Scan for active plans interrupted by crash/restart and re-enqueue them.

        Called once during startup after adapters and channels are registered.
        """
        from kernos.kernel.execution import scan_active_plans, build_envelope_from_plan

        data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
        active_plans = scan_active_plans(data_dir)
        if not active_plans:
            return

        for instance_id, space_id, plan in active_plans:
            plan_id = plan.get("plan_id", "?")
            # Find the in-progress step
            for phase in plan.get("phases", []):
                for step in phase.get("steps", []):
                    if step.get("status") == "in_progress":
                        step_id = step["id"]
                        step_desc = step.get("title", "")
                        envelope = build_envelope_from_plan(plan, step_id, step_desc)
                        # Check if this is the last pending step
                        _remaining = [
                            s for p in plan.get("phases", [])
                            for s in p.get("steps", [])
                            if s.get("status") == "pending"
                        ]
                        envelope.is_final_step = len(_remaining) == 0

                        logger.info("PLAN_RECOVER: plan=%s step=%s instance=%s space=%s — re-enqueuing",
                            plan_id, step_id, instance_id, space_id)
                        asyncio.create_task(
                            self._execute_self_directed_step(instance_id, space_id, envelope))

                        try:
                            await self.send_outbound(
                                instance_id, instance_id, None,
                                f"Recovered from restart — resuming plan at step {step_id}.",
                            )
                        except Exception:
                            pass
                        break  # Only re-enqueue the first in-progress step per plan

    async def _get_system_space(self, instance_id: str):
        """Return the system context space for this instance, or None."""
        try:
            spaces = await self.state.list_context_spaces(instance_id)
            for space in spaces:
                if space.space_type == "system":
                    return space
        except Exception:
            pass
        return None

    async def _write_capabilities_overview(
        self, instance_id: str, system_space_id: str
    ) -> None:
        """Write capabilities-overview.md to the system space — called after install/uninstall."""
        if not getattr(self, "_files", None):
            return
        connected = self.registry.get_connected()
        available = self.registry.get_available()

        content = "# Connected Tools\n\n"
        if connected:
            for cap in connected:
                universal_tag = " (available everywhere)" if cap.universal else ""
                content += f"- **{cap.name}**{universal_tag}: {cap.description}\n"
                if cap.tools:
                    content += f"  Tools: {', '.join(cap.tools)}\n"
        else:
            content += "No tools connected yet.\n"

        content += "\n# Available to Connect\n\n"
        if available:
            for cap in available:
                content += f"- **{cap.name}**: {cap.description}\n"
        else:
            content += "No additional tools available.\n"

        try:
            await self._files.write_file(
                instance_id, system_space_id,
                "capabilities-overview.md", content,
                "What tools are connected and available — updated on changes",
            )
        except Exception as exc:
            logger.warning("Failed to write capabilities-overview.md: %s", exc)

    async def _infer_pending_capability(
        self, instance_id: str, conversation_id: str
    ) -> str | None:
        """Infer which capability is being set up from recent system space messages.

        Scans the last 5 messages in the system space for capability name mentions.
        Returns the capability name if found, None otherwise.
        """
        system_space = await self._get_system_space(instance_id)
        if not system_space:
            return None

        try:
            recent = await self.conversations.get_space_thread(
                instance_id, conversation_id, system_space.id, max_messages=5
            )
        except Exception:
            return None

        available = self.registry.get_available()
        for cap in available:
            for msg in recent:
                content = str(msg.get("content", "")).lower()
                if cap.name.lower() in content or cap.display_name.lower() in content:
                    return cap.name

        return None

    async def _infer_pending_platform(
        self, instance_id: str, conversation_id: str
    ) -> str | None:
        """Infer which platform setup is pending from recent messages.

        Scans the last 5 messages for platform adapter setup context
        (e.g., 'Telegram is not connected', 'TELEGRAM_BOT_TOKEN').
        Returns the platform name if found, None otherwise.
        """
        system_space = await self._get_system_space(instance_id)
        if not system_space:
            return None

        try:
            recent = await self.conversations.get_space_thread(
                instance_id, conversation_id, system_space.id, max_messages=5
            )
        except Exception:
            return None

        for platform, cred_info in _PLATFORM_CREDENTIALS.items():
            if not cred_info.get("supports_paste"):
                continue
            env_var = cred_info.get("primary_env", "")
            for msg in recent:
                content = str(msg.get("content", "")).lower()
                if (f"{platform} is not connected" in content
                        or env_var.lower() in content
                        or f"secure api" in content and platform in content):
                    return platform

        return None

    async def _store_credential(
        self, instance_id: str, capability_name: str, value: str
    ) -> None:
        """Store a credential in the secrets directory with restrictive permissions.

        Secrets live OUTSIDE the data directory and are never readable by agents.
        """
        secrets_dir = Path(self._secrets_dir) / _safe_instance_name(instance_id)
        secrets_dir.mkdir(parents=True, exist_ok=True)
        secret_path = secrets_dir / f"{capability_name}.key"
        secret_path.write_text(value.strip())
        secret_path.chmod(0o600)
        logger.info("Stored credential for %s/%s", instance_id, capability_name)

    async def _connect_after_credential(
        self, instance_id: str, capability_name: str
    ) -> bool:
        """Connect an MCP server after credentials have been stored."""
        from mcp import StdioServerParameters
        from kernos.capability.registry import CapabilityStatus

        cap = self.registry.get(capability_name)
        if not cap:
            return False

        resolved_env = resolve_mcp_credentials(
            {"credentials_key": cap.credentials_key, "env_template": cap.env_template},
            instance_id,
            self._secrets_dir,
        )
        params = StdioServerParameters(
            command=cap.server_command,
            args=list(cap.server_args),
            env=resolved_env,
        )
        self.mcp.register_server(capability_name, params)

        # Register auth command if the capability defines one
        if cap.auth_args:
            from kernos.capability.client import AuthCommand
            self.mcp.register_auth_command(
                capability_name,
                AuthCommand(
                    command=cap.server_command,
                    args=list(cap.auth_args),
                    env=resolved_env,
                    probe_tool=cap.auth_probe_tool,
                ),
            )

        success = await self.mcp.connect_one(capability_name)

        if success:
            tools = self.mcp.get_tool_definitions().get(capability_name, [])
            cap.status = CapabilityStatus.CONNECTED
            cap.tools = [t["name"] for t in tools]

            await self._persist_mcp_config(instance_id)

            system_space = await self._get_system_space(instance_id)
            if system_space:
                await self._write_capabilities_overview(instance_id, system_space.id)

            try:
                await emit_event(
                    self.events, EventType.TOOL_INSTALLED, instance_id, "mcp_installer",
                    payload={
                        "capability_name": capability_name,
                        "tool_count": len(cap.tools),
                        "universal": cap.universal,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit tool.installed: %s", exc)

        return success

    def _write_env_var(self, key: str, value: str) -> None:
        """Append or update a key=value in the root .env file.

        Also sets os.environ so the current process picks it up immediately.
        """
        env_path = Path(".env")
        value = value.strip()
        os.environ[key] = value

        lines: list[str] = []
        found = False
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith(f"{key}="):
                    lines.append(f"{key}={value}")
                    found = True
                else:
                    lines.append(line)
        if not found:
            lines.append(f"{key}={value}")
        env_path.write_text("\n".join(lines) + "\n")
        logger.info("Wrote %s to .env", key)

    async def _start_platform_adapter(self, platform: str) -> bool:
        """Hot-start a platform adapter after credentials have been set.

        Returns True if the adapter was started successfully.
        """
        if platform == "telegram":
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            if not token:
                return False
            # Idempotency: server.py boot also hot-starts if the token is in
            # env. Skip if a telegram adapter is already registered so we
            # don't end up with two pollers racing the same bot token (which
            # triggers HTTP 409 spam from getUpdates).
            if "telegram" in self._adapters:
                logger.info(
                    "Telegram adapter already registered — skipping hot-start"
                )
                return True
            try:
                from kernos.messages.adapters.telegram_bot import TelegramAdapter
                from kernos.telegram_poller import TelegramPoller
                tg_adapter = TelegramAdapter()
                self.register_adapter("telegram", tg_adapter)
                self.register_channel(
                    name="telegram", display_name="Telegram", platform="telegram",
                    can_send_outbound=True, channel_target="",
                )
                tg_poller = TelegramPoller(
                    adapter=tg_adapter, handler=self, bot_token=token,
                )
                # Discover and persist bot identity for invite instructions
                if hasattr(self, '_instance_db') and self._instance_db:
                    tg_identity = await tg_poller.discover_identity()
                    if tg_identity:
                        await self._instance_db.set_platform_config("telegram", tg_identity)
                await tg_poller.start()
                logger.info("Hot-started Telegram adapter — long polling active")
                return True
            except Exception as exc:
                logger.error("Failed to hot-start Telegram adapter: %s", exc)
                return False
        # Other platforms require restart for now
        return False

    async def _persist_mcp_config(self, instance_id: str) -> None:
        """Write current MCP config to mcp-servers.json in the system space."""
        from kernos.capability.registry import CapabilityStatus

        system_space = await self._get_system_space(instance_id)
        if not system_space or not getattr(self, "_files", None):
            return

        config: dict = {"servers": {}, "uninstalled": [], "disabled": []}
        for cap in self.registry.get_all():
            if cap.status in (CapabilityStatus.CONNECTED, CapabilityStatus.DISABLED) and cap.server_name:
                config["servers"][cap.name] = {
                    "display_name": cap.display_name,
                    "command": cap.server_command,
                    "args": list(cap.server_args),
                    "credentials_key": cap.credentials_key,
                    "env_template": dict(cap.env_template),
                    "universal": cap.universal,
                    "tool_effects": dict(cap.tool_effects),
                    "source": cap.source,
                }
            if cap.status == CapabilityStatus.SUPPRESSED:
                config["uninstalled"].append(cap.name)
            elif cap.status == CapabilityStatus.DISABLED:
                config["disabled"].append(cap.name)

        try:
            await self._files.write_file(
                instance_id, system_space.id,
                "mcp-servers.json",
                json.dumps(config, indent=2),
                "MCP server configurations — managed by the system",
            )
        except Exception as exc:
            logger.warning("Failed to persist mcp config for %s: %s", instance_id, exc)

    async def _disconnect_capability(
        self, instance_id: str, capability_name: str
    ) -> bool:
        """Disconnect an MCP server and update all state."""
        from kernos.capability.registry import CapabilityStatus

        success = await self.mcp.disconnect_one(capability_name)
        if success:
            cap = self.registry.get(capability_name)
            if cap:
                cap.status = CapabilityStatus.SUPPRESSED
                cap.tools = []

            await self._persist_mcp_config(instance_id)

            system_space = await self._get_system_space(instance_id)
            if system_space:
                await self._write_capabilities_overview(instance_id, system_space.id)

            try:
                await emit_event(
                    self.events, EventType.TOOL_UNINSTALLED, instance_id, "mcp_installer",
                    payload={"capability_name": capability_name},
                )
            except Exception as exc:
                logger.warning("Failed to emit tool.uninstalled: %s", exc)

        return success

    async def _maybe_start_evaluator(self, instance_id: str) -> None:
        """Start an AwarenessEvaluator for this instance (once per process per-instance).

        The evaluator runs two phases:
        - Awareness pass (whispers from foresight signals) — every 1800s
        - Trigger evaluation (scheduled actions) — every 60s
        """
        if instance_id in self._evaluators:
            return
        try:
            from kernos.kernel.awareness import AwarenessEvaluator
            evaluator = AwarenessEvaluator(
                state=self.state,
                events=self.events,
                interval_seconds=int(os.getenv("KERNOS_AWARENESS_INTERVAL", "1800")),
                trigger_interval_seconds=int(os.getenv("KERNOS_TRIGGER_INTERVAL", "15")),
                trigger_store=self._trigger_store,
                handler=self,
                runtime=self._wlp_runtime,  # C5c-bringup: None until substrate is brought up
            )
            await evaluator.start(instance_id)
            self._evaluators[instance_id] = evaluator
        except Exception as exc:
            logger.warning("Failed to start AwarenessEvaluator for %s: %s", instance_id, exc)

    def register_adapter(self, platform: str, adapter: "BaseAdapter") -> None:
        """Register a platform adapter for outbound messaging."""
        from kernos.kernel.channels import ChannelInfo
        self._adapters[platform] = adapter

    def _get_canvas_service(self):
        """Lazy-init the CanvasService (CANVAS-V1).

        Constructed on first use so it picks up the ``_instance_db`` that
        server.py/bootstrap attaches post-__init__. Mirrors the same
        lazy pattern as :meth:`_get_relational_dispatcher`.
        """
        if self._canvas is not None:
            return self._canvas
        idb = getattr(self, "_instance_db", None)
        if idb is None:
            return None
        from kernos.kernel.canvas import CanvasService
        from kernos.kernel.events import emit_event

        async def _canvas_emit(instance_id, event_type, payload, *, member_id=""):
            # Best-effort event-stream emission — never raises.
            stream = getattr(self, "_events", None) or getattr(self, "events", None)
            if stream is not None:
                try:
                    meta = {"member_id": member_id} if member_id else {}
                    await emit_event(
                        stream, event_type, instance_id, "canvas", payload, meta,
                    )
                except Exception as exc:
                    logger.debug("CANVAS_EMIT_FAILED: %s %s", event_type, exc)

            # Fan out to the Gardener cohort if live. Gardener's on_canvas_event
            # is non-blocking (schedules background dispatch), so this does not
            # slow the canvas write path.
            gardener = self._get_gardener_service()
            if gardener is not None:
                try:
                    await gardener.on_canvas_event(
                        instance_id, event_type, payload, member_id=member_id,
                    )
                except Exception as exc:
                    logger.debug("GARDENER_DISPATCH_FAILED: %s %s", event_type, exc)

        self._canvas = CanvasService(
            instance_db=idb,
            data_dir=os.getenv("KERNOS_DATA_DIR", "./data"),
            event_emit=_canvas_emit,
        )
        # Keep the reasoning service in sync so tool dispatch can reach it.
        try:
            self.reasoning.set_canvas(self._canvas)
        except Exception:
            pass
        return self._canvas

    def _get_gardener_service(self):
        """Lazy-init the GardenerService (CANVAS-SECTION-MARKERS + GARDENER).

        Depends on CanvasService and InstanceDB being live; without them the
        Gardener can't read the Workflow Patterns library or issue reshape
        proposals, so we return None and the canvas pipeline behaves as if
        no Gardener is installed.
        """
        if self._gardener is not None:
            return self._gardener
        canvas = self._canvas
        idb = getattr(self, "_instance_db", None)
        reasoning = getattr(self, "reasoning", None)
        if canvas is None or idb is None or reasoning is None:
            return None
        from kernos.kernel.gardener import GardenerService

        self._gardener = GardenerService(
            canvas_service=canvas,
            instance_db=idb,
            reasoning_service=reasoning,
        )
        logger.info("GARDENER_SERVICE_INITIALIZED")
        return self._gardener

    def _get_relational_dispatcher(self):
        """Lazy-init the relational dispatcher.

        Bound to the handler's state, instance_db, and a push hook that
        reaches the recipient via their primary platform adapter. Constructed
        on first use so it picks up the instance_db that server.py/bootstrap
        sets post-__init__.
        """
        if self._relational_dispatcher is not None:
            return self._relational_dispatcher
        idb = getattr(self, "_instance_db", None)
        if idb is None:
            return None
        from kernos.kernel.relational_dispatch import RelationalDispatcher

        async def _push(msg) -> None:
            # Time-sensitive out-of-band push: send a whisper-style outbound
            # through the recipient's primary channel. Best-effort.
            try:
                channels = await idb.list_member_channels(msg.addressee_member_id)
            except Exception:
                channels = []
            if not channels:
                return
            # Prefer the most recently registered channel.
            target = channels[0]
            platform = target.get("platform", "")
            if platform not in self._adapters:
                return
            adapter = self._adapters[platform]
            try:
                text = (
                    f"Message from {msg.origin_agent_identity or msg.origin_member_id}: "
                    f"{msg.content}"
                )
                await adapter.send_outbound(
                    msg.instance_id, target.get("channel_id", ""), text,
                )
            except Exception as exc:
                logger.warning("RM_PUSH_ADAPTER_FAILED: %s", exc)

        self._relational_dispatcher = RelationalDispatcher(
            state=self.state,
            instance_db=idb,
            outbound_push=_push,
            trace_emitter=None,  # handler wires per-turn trace via ctx
            messenger_judge=self._build_messenger_judge_callback(),
        )
        return self._relational_dispatcher

    def _build_messenger_judge_callback(self):
        """Return the async ``messenger_judge`` callback wired into the RM dispatcher.

        The callback is responsible for: (a) loading the Messenger's judgment
        inputs (covenants, disclosures, relationship, ephemeral permissions)
        for the (disclosing, requesting) pair, (b) running
        ``cohorts.messenger.judge_exchange``, (c) translating the returned
        ``Optional[MessengerDecision]`` into a ``(content_to_send,
        refer_whisper|None)`` tuple the dispatcher dispatches on, (d) emitting
        the MESSENGER_* trace events, and (e) holding the always-respond
        invariant on exhaustion via the pre-rendered default-deny response.

        Messenger never appears on any agent's tool surface; this callback is
        the only place the cohort runs, and it runs inside the dispatcher's
        pre-envelope path — the agent that sent the relational message never
        sees Messenger's existence in trace or context.
        """
        async def _judge(
            *,
            instance_id: str,
            origin_member_id: str,
            addressee_member_id: str,
            intent: str,
            content: str,
        ):
            # The exchange's "disclosing" side is the one whose info is being
            # talked ABOUT. For an outbound ask_question / request_action /
            # inform, the disclosing side is the RECIPIENT (origin is asking
            # about them); for other directions the semantics vary. We start
            # with the conservative read: both directions have the recipient
            # as the subject of potential welfare concerns. If patterns show
            # this needs refinement, a future spec can sharpen it.
            disclosing = addressee_member_id
            requesting = origin_member_id
            direction = "inbound"

            async def _display_name(member_id: str) -> str:
                try:
                    prof = await self._instance_db.get_member_profile(member_id)
                    return (prof or {}).get("display_name", "") or member_id
                except Exception:
                    return member_id
            disclosing_name = await _display_name(disclosing)
            requesting_name = await _display_name(requesting)

            # Relationship profile: the permission string the disclosing
            # member declared toward the requesting member.
            relationship = "unknown"
            try:
                relationship = await self._instance_db.get_permission(
                    disclosing, requesting,
                ) or "unknown"
            except Exception:
                pass

            # Covenants — scoped to the disclosing member, currently active.
            covenants_evidence: list = []
            try:
                from kernos.cohorts.messenger import CovenantEvidence
                rules = await self.state.get_contract_rules(instance_id)
                for r in rules or []:
                    # Active, this-member rules only. Member-scoped rules
                    # with a different member_id are filtered out; empty
                    # member_id means instance-wide (applies to this member
                    # by default).
                    if not getattr(r, "active", True):
                        continue
                    owner = getattr(r, "member_id", "") or ""
                    if owner and owner != disclosing:
                        continue
                    # Further narrow by target if populated: exact member id
                    # match OR relationship-profile identifier match.
                    target = getattr(r, "target", "") or ""
                    if target and target != requesting and target != relationship:
                        # Non-matching targeted covenant — skip.
                        continue
                    covenants_evidence.append(
                        CovenantEvidence(
                            id=getattr(r, "id", ""),
                            description=getattr(r, "description", ""),
                            rule_type=getattr(r, "rule_type", ""),
                            topic=getattr(r, "topic", "") or "",
                            target=target,
                        )
                    )
            except Exception as exc:
                logger.warning("MESSENGER_JUDGE_COVENANT_LOAD_FAILED: %s", exc)

            # Ephemeral permissions — treated as synthesized low-priority
            # covenant evidence so the Messenger sees standing and ephemeral
            # grants uniformly.
            try:
                from kernos.cohorts.messenger import CovenantEvidence
                eph = await self.state.list_ephemeral_permissions(
                    instance_id,
                    disclosing_member_id=disclosing,
                    requesting_member_id=requesting,
                )
                for p in eph or []:
                    kind = "must" if p.granted else "must_not"
                    phrasing = (
                        f"ephemeral permission granted for topic "
                        f"{p.topic!r}, expires {p.expires_at}"
                        if p.granted else
                        f"ephemeral denial for topic {p.topic!r}, "
                        f"expires {p.expires_at}"
                    )
                    covenants_evidence.append(
                        CovenantEvidence(
                            id=p.id, description=phrasing,
                            rule_type=kind, topic=p.topic,
                            target=requesting,
                        )
                    )
            except Exception:
                pass

            # Disclosures — recent sensitive knowledge entries about the
            # disclosing member. Bounded to cap tokens.
            disclosures: list = []
            try:
                from kernos.cohorts.messenger import Disclosure
                entries = await self.state.query_knowledge(
                    instance_id=instance_id,
                    member_id=disclosing,
                    limit=10,
                )
                for e in entries or []:
                    sens = getattr(e, "sensitivity", "") or ""
                    if sens in ("contextual", "personal"):
                        disclosures.append(
                            Disclosure(
                                content=getattr(e, "content", "")[:280],
                                sensitivity=sens,
                                subject=getattr(e, "subject", "") or "",
                                created_at=getattr(e, "created_at", "") or "",
                            )
                        )
            except Exception as exc:
                logger.debug("MESSENGER_DISCLOSURES_LOAD_SKIPPED: %s", exc)

            from kernos.cohorts.messenger import (
                ExchangeContext,
                MessengerExhausted,
                judge_exchange,
                render_exhaustion_response,
            )
            ctx = ExchangeContext(
                disclosing_member_id=disclosing,
                disclosing_display_name=disclosing_name,
                requesting_member_id=requesting,
                requesting_display_name=requesting_name,
                relationship_profile=relationship,
                exchange_direction=direction,
                content=content,
                covenants=covenants_evidence,
                disclosures=disclosures,
            )

            def _trace_messenger(event_name: str, detail: str) -> None:
                try:
                    logger.info("%s: %s", event_name, detail)
                except Exception:
                    pass

            try:
                decision = await judge_exchange(
                    ctx, reasoning_service=self.reasoning,
                )
            except MessengerExhausted as exc:
                _trace_messenger(
                    "MESSENGER_EXHAUSTED",
                    f"disclosing={disclosing} requesting={requesting} "
                    f"reason={exc.reason[:120]}",
                )
                deny_text = render_exhaustion_response(
                    disclosing_display_name=disclosing_name,
                    requesting_display_name=requesting_name,
                )
                return deny_text, None
            except Exception as exc:
                # Callback promises not to raise; log and fall through to
                # unchanged-send. Always-respond holds: the original content
                # dispatches as-is.
                logger.warning("MESSENGER_JUDGE_UNEXPECTED: %s", exc, exc_info=True)
                return content, None

            if decision is None:
                _trace_messenger(
                    "MESSENGER_UNCHANGED",
                    f"disclosing={disclosing} requesting={requesting}",
                )
                return content, None

            if decision.outcome == "revise":
                _trace_messenger(
                    "MESSENGER_DECIDED",
                    f"outcome=revise disclosing={disclosing} "
                    f"requesting={requesting} "
                    f"covenants_consulted={len(decision.matched_covenants)}",
                )
                return decision.response_text, None

            if decision.outcome == "refer":
                _trace_messenger(
                    "MESSENGER_REFERRED",
                    f"disclosing={disclosing} requesting={requesting}",
                )
                # Build a whisper that surfaces to the DISCLOSING member
                # with the refer_prompt. The handler's awareness layer
                # already supports owner_member_id-scoped whispers so the
                # other members don't see this.
                from datetime import datetime, timezone
                from kernos.kernel.awareness import Whisper
                import uuid as _uuid
                wsp_id = f"wsp_msgr_{_uuid.uuid4().hex[:8]}"
                whisper = Whisper(
                    whisper_id=wsp_id,
                    insight_text=decision.refer_prompt,
                    delivery_class="ambient",
                    source_space_id="",
                    target_space_id="",
                    supporting_evidence=[],
                    reasoning_trace=decision.reasoning or "",
                    knowledge_entry_id="",
                    foresight_signal=f"messenger_refer:{disclosing}:{requesting}",
                    created_at=datetime.now(timezone.utc).isoformat(),
                    owner_member_id=disclosing,
                )
                return decision.response_text, whisper

            # Unknown outcome — log and fall through unchanged.
            logger.warning(
                "MESSENGER_UNKNOWN_OUTCOME_FROM_DECISION: %r", decision.outcome,
            )
            return content, None

        return _judge

    def register_channel(
        self, name: str, display_name: str, platform: str,
        can_send_outbound: bool, channel_target: str = "",
        status: str = "connected", source: str = "default",
    ) -> None:
        """Register a communication channel in the channel registry."""
        from kernos.kernel.channels import ChannelInfo
        self._channel_registry.register(ChannelInfo(
            name=name,
            display_name=display_name,
            status=status,
            source=source,
            can_send_outbound=can_send_outbound,
            channel_target=channel_target,
            platform=platform,
        ))

    def _resolve_member(self, instance_id: str, platform: str, sender: str) -> str:
        """Synchronous fallback — resolve sender to owner member_id.

        Used when async resolution isn't available. The async path
        (_resolve_incoming) is preferred and handles multi-member.
        """
        from kernos.kernel.scheduler import resolve_owner_member_id
        return resolve_owner_member_id(instance_id)

    async def _resolve_incoming(
        self, platform: str, sender_id: str, message_text: str,
    ) -> tuple[str, str | None]:
        """Resolve incoming sender to member_id via instance.db.

        Returns (member_id, static_response).
        If static_response is not None, send it and skip the pipeline.
        Includes escalating abuse prevention: 3 failures → 24h block → 24d → 24y.
        """
        if not hasattr(self, '_instance_db') or not self._instance_db:
            return "", None

        # 0. Check if sender is blocked (escalating ban)
        block_msg = await self._instance_db.check_sender_blocked(platform, sender_id)
        if block_msg:
            logger.info("BLOCKED_SENDER: platform=%s sender=%s", platform, sender_id)
            return "", block_msg

        # 1. Known member?
        member = await self._instance_db.get_member_by_channel(platform, sender_id)
        if member:
            # Successful resolution — clear any prior failure history
            await self._instance_db.clear_sender_failures(platform, sender_id)
            return member["member_id"], None

        # 2. Check for invite code in message
        code = _extract_invite_code(message_text)
        if code:
            result = await self._instance_db.claim_invite_code(code, platform, sender_id)
            if result:
                if result.get("action") == "rejected":
                    # Wrong platform — counts as a failure
                    ban_msg = await self._instance_db.record_sender_failure(platform, sender_id)
                    return "", ban_msg or result.get("static_response")
                # Successful claim — clear failures
                await self._instance_db.clear_sender_failures(platform, sender_id)
                return result.get("member_id", ""), result.get("static_response")
            else:
                # Invalid/expired code — record failure
                ban_msg = await self._instance_db.record_sender_failure(platform, sender_id)
                return "", ban_msg or "That invite code is invalid or has expired."

        # 3. Unknown sender, no valid code — record failure
        ban_msg = await self._instance_db.record_sender_failure(platform, sender_id)
        logger.info("UNKNOWN_SENDER: platform=%s sender=%s", platform, sender_id)
        return "", ban_msg or "This is a private Kernos instance. If you were invited, send your invite code."

    async def read_log_text(self, instance_id: str, space_id: str, log_number: int, member_id: str = "") -> str:
        """Read conversation log text — satisfies HandlerProtocol."""
        result = await self.conv_logger.read_log_text(instance_id, space_id, log_number, member_id=member_id)
        return result or ""

    def queue_system_event(self, instance_id: str, event: str) -> None:
        """Queue a system event for injection into the next system prompt."""
        self._pending_system_events.setdefault(instance_id, []).append(event)
        logger.info("SYSTEM_EVENT_QUEUED: instance=%s event=%s", instance_id, event[:100])

    def drain_system_events(self, instance_id: str) -> list[str]:
        """Drain and return all pending system events for an instance."""
        return self._pending_system_events.pop(instance_id, [])

    async def send_outbound(
        self, instance_id: str, member_id: str,
        channel_name: str | None, message: str,
    ) -> int:
        """Send an unprompted message to the user on a specific or default channel.

        Returns message ID if sent, 0 on failure.
        """
        from kernos.kernel.channels import ChannelInfo

        if channel_name:
            ch = self._channel_registry.get(channel_name)
        else:
            # Pick most recently used outbound-capable channel
            capable = self._channel_registry.get_outbound_capable()
            ch = capable[0] if capable else None

        if not ch:
            logger.warning(
                "OUTBOUND: no channel available instance=%s member=%s channel=%s",
                instance_id, member_id, channel_name,
            )
            return 0

        if ch.status != "connected":
            logger.warning(
                "OUTBOUND: channel=%s not connected (status=%s)",
                ch.name, ch.status,
            )
            return 0

        adapter = self._adapters.get(ch.platform)
        if not adapter:
            logger.warning("OUTBOUND: no adapter for platform=%s", ch.platform)
            return 0

        # SURFACE-DISCIPLINE-PASS D1 — last-resort guard against internal
        # identifiers (`mem_xxx`, `space_xxx`) and `[SYSTEM]` markers
        # reaching a user-facing adapter. Primary mechanism is resolver-at-
        # generation; this catches anything that slipped through.
        message = self._sanitize_user_facing_text(
            message, instance_id=instance_id, member_id=member_id,
            source="send_outbound", channel_name=ch.name,
        )

        msg_id = await adapter.send_outbound(instance_id, ch.channel_target, message)
        logger.info(
            "OUTBOUND: channel=%s target=%s instance=%s member=%s length=%d msg_id=%s",
            ch.name, ch.channel_target, instance_id, member_id, len(message), msg_id,
        )
        return msg_id

    def _finalize_user_facing_response(
        self, text: str, ctx: "TurnContext", msg: "NormalizedMessage",
    ) -> str:
        """User-facing finalizer: sanitize before the adapter.

        This is one of two explicitly different code paths the turn loop
        routes to. The diagnostic counterpart is _finalize_diagnostic_
        response — no shared-middleware-at-runtime decision.
        """
        return self._sanitize_user_facing_text(
            text,
            instance_id=ctx.instance_id,
            member_id=ctx.member_id,
            source="turn_reply",
            channel_name=msg.platform,
        )

    def _finalize_diagnostic_response(self, text: str) -> str:
        """Diagnostic finalizer: no sanitization. Raw internal identifiers
        and `[SYSTEM]` markers are preserved by design on admin surfaces.
        Separate code path from the user-facing finalizer.
        """
        return text

    def _sanitize_user_facing_text(
        self, text: str, *,
        instance_id: str = "", member_id: str = "",
        source: str = "", channel_name: str = "",
    ) -> str:
        """Redact internal identifiers + strip `[SYSTEM]` markers.

        Used at user-facing surfaces only. Never called from the diagnostic
        surfaces (`/dump`, runtime trace read) where raw internals are by
        design. On leak detection we log `SURFACE_LEAK_DETECTED` and redact
        in place — dropping the whole message would lose signal the user
        asked for; a placeholder is less bad than silence.
        """
        from kernos.kernel.display_names import (
            contains_internal_identifier, redact_internal_identifiers,
            strip_system_markers,
        )
        if not text:
            return text
        if contains_internal_identifier(text):
            logger.warning(
                "SURFACE_LEAK_DETECTED: source=%s channel=%s instance=%s "
                "member=%s len=%d",
                source, channel_name, instance_id, member_id, len(text),
            )
            text = redact_internal_identifiers(text)
        text = strip_system_markers(text)
        return text

    def _get_outbound_channel_id(self) -> int:
        """Get the outbound Discord channel ID. Returns 0 if unavailable."""
        capable = self._channel_registry.get_outbound_capable()
        ch = capable[0] if capable else None
        if ch and ch.channel_target:
            try:
                return int(ch.channel_target)
            except (ValueError, TypeError):
                pass
        return 0

    async def _delete_discord_msg(self, channel_id: int, msg_id: int) -> None:
        """Delete a Discord message by channel + message ID. Best-effort."""
        adapter = self._adapters.get("discord")
        if not adapter or not hasattr(adapter, '_client') or not adapter._client:
            logger.debug("PLAN_MSG_DELETE: no discord adapter")
            return
        try:
            channel = await adapter._client.fetch_channel(channel_id)
            msg = await channel.fetch_message(msg_id)
            await msg.delete()
            logger.debug("PLAN_MSG_DELETE: deleted msg_id=%d channel=%d", msg_id, channel_id)
        except Exception as exc:
            logger.debug("PLAN_MSG_DELETE: failed msg_id=%d error=%s", msg_id, exc)

    async def _maybe_run_covenant_cleanup(self, instance_id: str) -> None:
        """Run one-time covenant dedup/contradiction cleanup per-instance per process."""
        if instance_id in self._covenant_cleanup_done:
            return
        self._covenant_cleanup_done.add(instance_id)

        try:
            from kernos.kernel.covenant_manager import run_covenant_cleanup
            embedding_service = None
            if self._retrieval:
                embedding_service = getattr(self._retrieval, '_embedding_service', None)
            stats = await run_covenant_cleanup(
                self.state, instance_id,
                embedding_service=embedding_service,
            )
            if stats["deduped"] or stats["contradictions_resolved"]:
                logger.info(
                    "COVENANT_CLEANUP: instance=%s deduped=%d contradictions=%d",
                    instance_id, stats["deduped"], stats["contradictions_resolved"],
                )
        except Exception as exc:
            logger.warning("Covenant cleanup failed for %s: %s", instance_id, exc)

    async def _maybe_load_mcp_config(self, instance_id: str) -> None:
        """Load persisted MCP config for this instance (once per process lifetime per-instance).

        Called after soul/space init so the system space is guaranteed to exist.
        Suppresses uninstalled entries and connects any persisted servers.
        """
        from kernos.capability.registry import CapabilityStatus
        from mcp import StdioServerParameters

        if instance_id in self._mcp_config_loaded:
            return
        self._mcp_config_loaded.add(instance_id)

        system_space = await self._get_system_space(instance_id)
        if not system_space or not getattr(self, "_files", None):
            return

        try:
            config_raw = await self._files.read_file(
                instance_id, system_space.id, "mcp-servers.json"
            )
            if not config_raw or config_raw.startswith("Error:"):
                return
            config = json.loads(config_raw)
        except Exception as exc:
            logger.warning("Failed to load mcp-servers.json for %s: %s", instance_id, exc)
            return

        # Suppress uninstalled entries
        for name in config.get("uninstalled", []):
            cap = self.registry.get(name)
            if cap and cap.status != CapabilityStatus.CONNECTED:
                cap.status = CapabilityStatus.SUPPRESSED

        # Restore disabled state for capabilities that are connected but user disabled
        for name in config.get("disabled", []):
            cap = self.registry.get(name)
            if cap and cap.status == CapabilityStatus.CONNECTED:
                cap.status = CapabilityStatus.DISABLED

        # Migration: check for new defaults in known.py not yet in tenant config
        # These appear as "available" — the user can enable them via manage_capabilities
        known_in_config = set(config.get("servers", {}).keys()) | set(config.get("uninstalled", [])) | set(config.get("disabled", []))
        for cap in self.registry.get_all():
            if cap.source == "default" and cap.name not in known_in_config:
                logger.info(
                    "New default capability '%s' available for instance %s",
                    cap.name, instance_id,
                )

        # Connect persisted servers not already connected
        for name, server_config in config.get("servers", {}).items():
            cap = self.registry.get(name)
            if cap and cap.status == CapabilityStatus.CONNECTED:
                continue  # Already connected at startup
            resolved_env = resolve_mcp_credentials(
                server_config, instance_id, self._secrets_dir
            )
            self.mcp.register_server(
                name,
                StdioServerParameters(
                    command=server_config.get("command", ""),
                    args=list(server_config.get("args", [])),
                    env=resolved_env,
                ),
            )

            # Register auth command if capability defines one
            if cap and cap.auth_args:
                from kernos.capability.client import AuthCommand
                self.mcp.register_auth_command(
                    name,
                    AuthCommand(
                        command=cap.server_command,
                        args=list(cap.auth_args),
                        env=resolved_env,
                        probe_tool=cap.auth_probe_tool,
                    ),
                )

            success = await self.mcp.connect_one(name)
            if success:
                tools = self.mcp.get_tool_definitions().get(name, [])
                if cap:
                    cap.status = CapabilityStatus.CONNECTED
                    cap.tools = [t["name"] for t in tools]
                    if server_config.get("source"):
                        cap.source = server_config["source"]
                logger.info("Loaded and connected %s from persisted config", name)

    async def _ensureinstance_state(
        self, instance_id: str, message: NormalizedMessage
    ) -> None:
        """Create or update StateStore profile for this instance.

        New tenants: create full profile, seed default contract rules.
        Existing tenants: update capabilities field to reflect current registry state.
        """
        profile = await self.state.get_instance_profile(instance_id)
        cap_map = {cap.name: cap.status.value for cap in self.registry.get_all()}

        if profile is not None:
            # Always sync capabilities so the profile reflects current registry state
            profile.capabilities = cap_map
            await self.state.save_instance_profile(instance_id, profile)
            return

        now = utc_now()
        new_profile = InstanceProfile(
            instance_id=instance_id,
            status="active",
            created_at=now,
            platforms={
                message.platform: {"connected_at": now, "sender": message.sender}
            },
            preferences={},
            capabilities=cap_map,
            model_config={"default_provider": _PROVIDER, "quality_tier": 3},
        )
        await self.state.save_instance_profile(instance_id, new_profile)

        for rule in default_contract_rules(instance_id, now):
            await self.state.add_contract_rule(rule)

        try:
            await emit_event(
                self.events,
                EventType.TENANT_PROVISIONED,
                instance_id,
                "handler",
                payload={"platform": message.platform, "sender": message.sender},
            )
        except Exception as exc:
            logger.warning("Failed to emit tenant.provisioned: %s", exc)

        logger.info("Provisioned state for new tenant: %s", instance_id)

    async def _write_system_docs(
        self, instance_id: str, system_space_id: str
    ) -> None:
        """Write capabilities-overview.md to the system space.

        Self-knowledge docs (how-i-work.md, kernos-reference.md, how-to-connect-tools.md)
        are deprecated — replaced by docs/ + read_doc() (SPEC-3J).
        Only capabilities-overview.md remains (dynamically updated on install/uninstall).
        """
        if not getattr(self, "_files", None):
            return
        registry = getattr(self, "registry", None)
        if not registry:
            return

        await self._write_capabilities_overview(instance_id, system_space_id)

    async def _get_or_init_soul(self, instance_id: str) -> Soul:
        """Load the soul for this instance, or initialize a new unhatched one.

        The soul is saved immediately on creation so it persists even if
        the subsequent reasoning call fails. Also ensures a default daily
        context space exists for the instance.
        """
        import uuid
        soul = await self.state.get_soul(instance_id)
        if soul is None:
            soul = Soul(instance_id=instance_id)
            await self.state.save_soul(soul, source="soul_init", trigger="new_instance")
            logger.info("Initialized new soul for instance: %s", instance_id)

        # Timezone discovery: infer from system local if not yet set
        if not soul.timezone:
            try:
                _sys_tz = str(datetime.now().astimezone().tzinfo)
                if _sys_tz and "/" in _sys_tz:  # IANA format check
                    soul.timezone = _sys_tz
                    await self.state.save_soul(
                        soul, source="handler_process", trigger="timezone_discovery",
                    )
                    logger.info(
                        "TIMEZONE_DISCOVERED: instance=%s tz=%s source=system_local",
                        instance_id, _sys_tz,
                    )
            except Exception:
                pass

        # Ensure default context space exists — idempotent
        spaces = await self.state.list_context_spaces(instance_id)
        # Migrate existing "Daily" spaces to "General"
        for s in spaces:
            if s.is_default and s.name == "Daily":
                await self.state.update_context_space(instance_id, s.id, {"name": "General"})
                s.name = "General"
                logger.info("SPACE_MIGRATE: renamed Daily→General for instance=%s space=%s", instance_id, s.id)
        if not any(s.is_default for s in spaces):
            now = utc_now()
            daily_space = ContextSpace(
                id=f"space_{uuid.uuid4().hex[:8]}",
                instance_id=instance_id,
                name="General",
                description="General conversation and daily life",
                space_type="general",
                status="active",
                is_default=True,
                created_at=now,
                last_active_at=now,
            )
            await self.state.save_context_space(daily_space)
            logger.info("Created default General context space for instance: %s", instance_id)

            # Initialize compaction state for daily space with default headroom
            try:
                from kernos.kernel.compaction import (
                    CompactionState,
                    compute_document_budget,
                    MODEL_MAX_TOKENS,
                    COMPACTION_MODEL_USABLE_TOKENS,
                    COMPACTION_INSTRUCTION_TOKENS,
                    DEFAULT_DAILY_HEADROOM,
                )
                context_def = (
                    f"Space: {daily_space.name}\nType: {daily_space.space_type}\n"
                    f"Description: {daily_space.description}\nPosture: {daily_space.posture}\n"
                )
                context_def_tokens = await self.compaction.adapter.count_tokens(context_def)
                system_overhead = 4000  # Approximate for daily space
                doc_budget = compute_document_budget(
                    MODEL_MAX_TOKENS, system_overhead, 0, DEFAULT_DAILY_HEADROOM
                )
                daily_comp = CompactionState(
                    space_id=daily_space.id,
                    conversation_headroom=DEFAULT_DAILY_HEADROOM,
                    document_budget=doc_budget,
                    message_ceiling=min(
                        doc_budget,
                        COMPACTION_MODEL_USABLE_TOKENS - COMPACTION_INSTRUCTION_TOKENS - context_def_tokens,
                    ),
                    _context_def_tokens=context_def_tokens,
                    _system_overhead=system_overhead,
                )
                await self.compaction.save_state(instance_id, daily_space.id, daily_comp)
            except Exception as exc:
                logger.warning("Failed to init compaction state for daily space: %s", exc)

        # Ensure a system context space exists — idempotent
        spaces_now = await self.state.list_context_spaces(instance_id)
        # Migrate: update system space description to include member management
        for s in spaces_now:
            if s.space_type == "system" and "invite" not in (s.description or "").lower():
                await self.state.update_context_space(instance_id, s.id, {
                    "description": (
                        "System configuration and management. Install and manage tools, "
                        "view connected capabilities, invite and manage members, "
                        "generate invite codes, configure settings, get help with how the system works."
                    ),
                })
                logger.info("SPACE_MIGRATE: updated System description for instance=%s", instance_id)
        if not any(s.space_type == "system" for s in spaces_now):
            now = utc_now()
            system_space = ContextSpace(
                id=f"space_{uuid.uuid4().hex[:8]}",
                instance_id=instance_id,
                name="System",
                description=(
                    "System configuration and management. Install and manage tools, "
                    "view connected capabilities, invite and manage members, "
                    "generate invite codes, configure settings, get help with how the system works."
                ),
                space_type="system",
                status="active",
                posture=(
                    "Precise and careful. Configuration changes affect the whole system. "
                    "Confirm before modifying system settings or tool configurations.\n\n"
                    "TOOL CONNECTION:\n"
                    "You can help users connect and manage their tools. When a user wants "
                    "to connect a new tool:\n"
                    "1. Identify the capability from the known catalog\n"
                    "2. Explain what's needed (API key, account setup, etc.)\n"
                    "3. Walk them through getting the credential\n"
                    "4. For the credential handoff, instruct them: \"When you have your key "
                    "ready, reply with exactly: secure api\"\n"
                    "5. The system handles the rest — you'll be told if it succeeded\n\n"
                    "NEVER ask users to paste API keys directly in conversation.\n"
                    "ALWAYS use the 'secure api' flow for credentials.\n\n"
                    "If a capability requires a web interface (requires_web_interface=True), "
                    "explain that it can't be set up in this channel yet and will be available "
                    "when the web interface ships."
                ),
                is_default=False,
                created_at=now,
                last_active_at=now,
            )
            await self.state.save_context_space(system_space)
            logger.info("Created system context space for instance: %s", instance_id)
            # Write documentation files to the system space
            await self._write_system_docs(instance_id, system_space.id)

        return soul

    async def _ensure_member_default_space(self, instance_id: str, member_id: str) -> None:
        """Ensure a member has their own default General space. Idempotent."""
        import uuid as _uuid
        spaces = await self.state.list_context_spaces(instance_id)
        # Check if this member already has a default space
        if any(s.is_default and s.member_id == member_id for s in spaces):
            return
        # Check if there are legacy spaces with no member_id (owner's spaces)
        legacy_defaults = [s for s in spaces if s.is_default and not s.member_id]
        if legacy_defaults:
            # Claim the first legacy default space for this member if they're the owner
            # (the owner is whoever had the instance before multi-member)
            member = None
            if hasattr(self, '_instance_db') and self._instance_db:
                member = await self._instance_db.get_member(member_id)
            if member and member.get("role") == "owner":
                for ls in legacy_defaults:
                    ls.member_id = member_id
                    await self.state.update_context_space(instance_id, ls.id, {"member_id": member_id})
                logger.info("SPACE_CLAIM: owner %s claimed %d legacy spaces", member_id, len(legacy_defaults))
                return

        # Create a new default space for this member
        now = utc_now()
        member_space = ContextSpace(
            id=f"space_{_uuid.uuid4().hex[:8]}",
            instance_id=instance_id,
            member_id=member_id,
            name="General",
            description="General conversation and daily life",
            space_type="general",
            status="active",
            is_default=True,
            created_at=now,
            last_active_at=now,
        )
        await self.state.save_context_space(member_space)
        logger.info("Created default General space for member %s on instance %s", member_id, instance_id)

        # Initialize compaction state for the member's default space
        try:
            from kernos.kernel.compaction import (
                CompactionState, compute_document_budget,
                MODEL_MAX_TOKENS, COMPACTION_MODEL_USABLE_TOKENS,
                COMPACTION_INSTRUCTION_TOKENS, DEFAULT_DAILY_HEADROOM,
            )
            context_def = f"Space: General\nType: general\nDescription: General conversation and daily life\n"
            context_def_tokens = await self.compaction.adapter.count_tokens(context_def)
            daily_comp = CompactionState(
                space_id=member_space.id,
                _context_def_tokens=context_def_tokens,
                conversation_headroom=DEFAULT_DAILY_HEADROOM,
            )
            daily_comp.document_budget = compute_document_budget(
                MODEL_MAX_TOKENS, 4000, 0, DEFAULT_DAILY_HEADROOM,
            )
            # DISCLOSURE-GATE: compaction state is member-scoped. Previously
            # this save wrote to the legacy (unscoped) path, which let one
            # member's compaction state spill into another member's load via
            # the lazy-migration fallback. Fallback is gone; state must be
            # written to the member subdir from the start.
            await self.compaction.save_state(
                instance_id, member_space.id, daily_comp, member_id=member_id,
            )
        except Exception as exc:
            logger.warning("Failed to init compaction state for member space: %s", exc)

    async def _extract_agent_name_from_transcript(
        self, instance_id: str, space_id: str, member_id: str,
    ) -> str:
        """Scan the member's recent conversation log for a chosen agent name.

        Naming often happens as an organic moment ("Slate" / "Slate it is") and
        the agent doesn't always call update_soul. This compaction-time pass
        reads the transcript and extracts the name if one was settled on.
        Returns the name string, or empty.
        """
        if not space_id or not member_id:
            return ""
        try:
            entries = await self.conv_logger.read_recent(
                instance_id, space_id, token_budget=4000,
                max_messages=30, member_id=member_id,
            )
        except Exception as exc:
            logger.debug("name extract: read_recent failed: %s", exc)
            return ""
        if not entries:
            return ""

        transcript = "\n".join(
            f"{e.get('role', '?')}: {e.get('content', '')}" for e in entries
        )
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        }
        try:
            raw = await self.reasoning.complete_simple(
                system_prompt=(
                    "You are reviewing a conversation between an AI agent and a "
                    "user. If the agent settled on a name for itself during this "
                    "conversation (proposed, agreed, or accepted), return that "
                    "name. Otherwise return empty string. Only return the name "
                    "itself — no quotes, no extra words. Be conservative: only "
                    'extract if the naming moment is clearly settled (e.g. '
                    '"Slate it is", "let\'s go with Tern", user accepting a '
                    "proposal). Ambiguous or declined names → empty."
                ),
                user_content=f"TRANSCRIPT:\n{transcript}",
                max_tokens=30,
                prefer_cheap=True,
                output_schema=schema,
            )
            import json as _json
            parsed = _json.loads(raw)
            name = (parsed.get("name") or "").strip()
            # Guard: names are short; reject anything that looks like a sentence
            if name and len(name) <= 40 and "\n" not in name:
                return name
        except Exception as exc:
            logger.debug("name extract: LLM call failed: %s", exc)
        return ""

    async def _consolidate_bootstrap(self, soul: Soul, member_id: str = "", member_profile: dict | None = None, active_space_id: str = "") -> None:
        """One-time consolidation: bootstrap wisdom → member personality notes.

        Uses complete_simple() — stateless, no tools, no task events.
        Graduation is unconditional: if this call fails, member still graduates.
        Writes to the member's per-member personality_notes, not the instance soul.
        Also extracts agent_name from the transcript if graduation happened
        without an explicit update_soul call.
        """
        from kernos.kernel.template import PRIMARY_TEMPLATE

        # Query user knowledge from KnowledgeEntries
        user_ke = await self.state.query_knowledge(
            soul.instance_id, subject="user", active_only=True, limit=20,
            member_id=member_id,
        )
        user_facts = [e.content for e in user_ke
                      if e.lifecycle_archetype in ("structural", "identity", "habitual")]
        context_text = "\n".join(f"- {f}" for f in user_facts) if user_facts else "unknown"

        _name = (member_profile or {}).get("display_name", "") or "unknown"
        _agent = (member_profile or {}).get("agent_name", "") or "not yet named"
        _style = (member_profile or {}).get("communication_style", "") or "unknown"
        _count = (member_profile or {}).get("interaction_count", 0)

        prompt = (
            f"You are crystallizing an agent's personality after its first real "
            f"conversations with {_name}.\n\n"
            f"Agent name: {_agent}\n"
            f"Interactions: {_count}\n"
            f"Communication style observed: {_style}\n"
            f"Known facts:\n{context_text}\n\n"
            f"Before writing, consider these lenses (reason over them internally, "
            f"then write naturally):\n"
            f"- VIBE: What register settled between them? Dry, warm, precise, playful, "
            f"steady, irreverent? What actually worked, not what was requested.\n"
            f"- PACE: Quick exchanges or thoughtful? Dense or breathing room? "
            f"Processing by talking or receiving?\n"
            f"- POSTURE: Push, support, challenge, or quiet? Opinions or execution? "
            f"Where between leading with competence and leading with warmth?\n"
            f"- BOUNDARIES: What corrections shaped the edges? What would feel wrong?\n"
            f"- TEXTURE: What makes THIS relationship specifically different? "
            f"Recurring patterns, rhetorical shapes that landed, distinctive tells.\n\n"
            f"Write a personality profile in 4-8 sentences. Write it as if the agent "
            f"is reading notes about who it IS. First person is fine. This is the "
            f"agent's soul.\n\n"
            f"Write a presence, not a profile. Let it flow naturally — do not use "
            f"the lens labels as section headers.\n\n"
            f"Do not include facts about {_name} (those are in knowledge entries). "
            f"Do not include the agent's name (stored separately). Do not write a "
            f"list of traits.\n\n"
            f"If there's strong signal, write with specificity and confidence. "
            f"If the signal is sparse (mostly transactional exchanges), write what "
            f"you can honestly and note the personality is still forming.\n\n"
            f"Quality target: 'Grounded. Thoughtful. Direct without being cold. "
            f"Has opinions but holds them loosely. Finds humor in the margins. "
            f"Not performatively enthusiastic — just genuine. Matches energy before "
            f"shaping it. Leads with competence but earns trust through warmth. "
            f"When things get real, stays in the room.'"
        )
        try:
            notes = await self.reasoning.complete_simple(
                system_prompt=(
                    "You are crystallizing an AI agent's personality from its first "
                    "real conversations with a person. Read the evidence. Write what "
                    "actually emerged — not what should have emerged."
                ),
                user_content=prompt,
                max_tokens=500,
            )
            # Write to member profile, not instance soul
            if member_id and hasattr(self, '_instance_db') and self._instance_db:
                await self._instance_db.upsert_member_profile(member_id, {
                    "personality_notes": notes.strip(),
                })
            else:
                # Legacy fallback
                soul.personality_notes = notes.strip()
        except Exception as exc:
            logger.warning(
                "Bootstrap consolidation failed for %s: %s — graduating without consolidation",
                soul.instance_id,
                exc,
            )

        # Agent name extraction: the agent often settles on a name in the
        # naming moment without calling update_soul. Extract from transcript
        # at graduation so that moment is captured.
        if (
            member_id and active_space_id
            and hasattr(self, '_instance_db') and self._instance_db
            and not (member_profile or {}).get("agent_name")
        ):
            extracted = await self._extract_agent_name_from_transcript(
                soul.instance_id, active_space_id, member_id,
            )
            if extracted:
                await self._instance_db.upsert_member_profile(member_id, {
                    "agent_name": extracted,
                })
                logger.info(
                    "AGENT_NAME_EXTRACTED: member=%s name=%r (graduation-time)",
                    member_id, extracted,
                )

    def _format_relational_messages_block(
        self, messages: list, recent_surfaced: list | None = None,
    ) -> str:
        """Format collected relational messages for the RESULTS section.

        Active section: messages still needing attention this turn (pending
        or delivered). Agent should surface per the Obvious Benefit Rule
        and either let them flow to surfaced at end of turn or auto-handle
        via resolve_relational_message(auto_handled=true).

        Recent-surfaced section: reference-only list of messages already
        shown in a recent turn. The agent uses these to thread replies
        via reply_to_id when the user asks to respond in the same thread.
        These are NOT re-surfaced; they don't trigger another state
        transition on this turn.
        """
        recent_surfaced = recent_surfaced or []
        lines: list[str] = []
        if messages:
            lines.append("## RELATIONAL MESSAGES")
            lines.append("")
            lines.append(
                "The following messages arrived from other members' agents. "
                "Surface only if obviously benefits the user (Obvious Benefit "
                "Rule). To reply in-thread, call send_relational_message "
                "with reply_to_id=<the message id below> — the dispatcher "
                "auto-threads the conversation_id for you. Mark processed "
                "via resolve_relational_message; use auto_handled=true only "
                "if you handled it without user involvement."
            )
            lines.append("")
            for m in messages:
                lines.append(
                    f"- id={m.id} | from={m.origin_agent_identity or m.origin_member_id} "
                    f"(member_id={m.origin_member_id}) | intent={m.intent} | "
                    f"urgency={m.urgency} | thread={m.conversation_id}"
                )
                lines.append(f"  > {m.content}")
                lines.append(
                    f"  (reply: send_relational_message(addressee={m.origin_member_id!r}, "
                    f"intent=..., content=..., reply_to_id={m.id!r}))"
                )
                lines.append("")
        if recent_surfaced:
            lines.append("## RECENT RELATIONAL THREADS (reference only)")
            lines.append("")
            lines.append(
                "These messages were shown in a recent turn — they are NOT "
                "re-surfacing. Included so you can thread replies via "
                "reply_to_id=<id> when the user asks to follow up. Do NOT "
                "re-announce them to the user as new."
            )
            lines.append("")
            for m in recent_surfaced:
                lines.append(
                    f"- id={m.id} | from={m.origin_agent_identity or m.origin_member_id} "
                    f"(member_id={m.origin_member_id}) | intent={m.intent} | "
                    f"thread={m.conversation_id}"
                )
                lines.append(f"  > {m.content}")
                lines.append("")
        return "\n".join(lines)

    async def _post_response_soul_update(self, soul: Soul, member_id: str = "", member_profile: dict | None = None, active_space_id: str = "") -> None:
        """Update member profile after a successful response.

        Per-member: hatching, interaction_count, bootstrap graduation.
        Instance soul is kept for legacy compat but identity lives in member_profiles.
        """
        now = utc_now()

        # Per-member hatching: each member's agent hatches independently
        if member_id and member_profile and not member_profile.get("hatched") and hasattr(self, '_instance_db') and self._instance_db:
            await self._instance_db.upsert_member_profile(member_id, {
                "hatched": True, "hatched_at": now,
            })
            # In inherit mode, seed from template soul if this member has no agent_name yet
            if not member_profile.get("agent_name"):
                hatching_mode = await self._instance_db.get_hatching_mode()
                if hatching_mode == "inherit":
                    template_soul = await self._instance_db.get_template_soul()
                    if template_soul:
                        await self._instance_db.upsert_member_profile(member_id, template_soul)
                        logger.info("SOUL_INHERIT: member=%s inherited from template", member_id)
            try:
                await emit_event(
                    self.events,
                    EventType.AGENT_HATCHED,
                    soul.instance_id,
                    "handler",
                    payload={
                        "instance_id": soul.instance_id,
                        "member_id": member_id,
                        "hatched_at": now,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit agent.hatched: %s", exc)
            logger.info("Member agent hatched: member=%s instance=%s", member_id, soul.instance_id)

        # Per-member: increment interaction count
        if member_id and hasattr(self, '_instance_db') and self._instance_db:
            new_count = await self._instance_db.increment_interaction_count(member_id)
            # Reload profile with updated count
            member_profile = await self._instance_db.get_member_profile(member_id)

            # Per-member: check bootstrap graduation
            if member_profile and not member_profile.get("bootstrap_graduated"):
                user_ke = await self.state.query_knowledge(
                    soul.instance_id, subject="user", active_only=True, limit=1,
                )
                has_user_knowledge = len(user_ke) > 0
                if _is_member_mature(member_profile, has_user_knowledge=has_user_knowledge):
                    await self._consolidate_bootstrap(soul, member_id=member_id, member_profile=member_profile, active_space_id=active_space_id)
                    await self._instance_db.upsert_member_profile(member_id, {
                        "bootstrap_graduated": True,
                        "bootstrap_graduated_at": now,
                    })
                    try:
                        await emit_event(
                            self.events,
                            EventType.AGENT_BOOTSTRAP_GRADUATED,
                            soul.instance_id,
                            "handler",
                            payload={
                                "instance_id": soul.instance_id,
                                "member_id": member_id,
                                "interaction_count": new_count,
                                "graduated_at": now,
                            },
                        )
                    except Exception as exc:
                        logger.warning("Failed to emit agent.bootstrap_graduated: %s", exc)
                    logger.info(
                        "Member bootstrap graduated: member=%s instance=%s (interactions: %d)",
                        member_id, soul.instance_id, new_count,
                    )
                    # SYSTEM-REFERENCE-CANVAS-SEED Pillar 2: seed this
                    # member's personal My Tools canvas on onboarding
                    # completion. Best-effort — never breaks graduation.
                    try:
                        from kernos.setup.seed_canvases import (
                            seed_my_tools_canvas_for_member,
                        )
                        canvas_svc = self._get_canvas_service()
                        if canvas_svc is not None:
                            await seed_my_tools_canvas_for_member(
                                instance_id=soul.instance_id,
                                member_id=member_id,
                                canvas_service=canvas_svc,
                                instance_db=self._instance_db,
                            )
                    except Exception as exc:
                        logger.warning(
                            "SEED_MY_TOOLS_ON_GRADUATION_FAILED: member=%s %s",
                            member_id, exc,
                        )
        else:
            # Legacy path: no member_id/instance_db, update soul directly
            if not soul.hatched:
                soul.hatched = True
                soul.hatched_at = now
            soul.interaction_count += 1
            user_ke = await self.state.query_knowledge(
                soul.instance_id, subject="user", active_only=True, limit=1,
            )
            has_user_knowledge = len(user_ke) > 0
            if not soul.bootstrap_graduated and _is_soul_mature(soul, has_user_knowledge=has_user_knowledge):
                await self._consolidate_bootstrap(soul)
                soul.bootstrap_graduated = True
                soul.bootstrap_graduated_at = now
            await self.state.save_soul(soul, source="handler_process", trigger="interaction_count_update")

    def _truncate_to_budget(self, messages: list[dict], budget_tokens: int) -> list[dict]:
        """Drop oldest messages to fit within token budget. 4 chars ≈ 1 token."""
        msgs = list(messages)
        total = sum(len(m.get("content", "")) // 4 for m in msgs)
        while total > budget_tokens and len(msgs) > 2:
            dropped = msgs.pop(0)
            total -= len(dropped.get("content", "")) // 4
        return msgs

    async def _assemble_space_context(
        self,
        instance_id: str,
        conversation_id: str,
        active_space_id: str,
        active_space: ContextSpace | None,
        member_id: str = "",
    ) -> tuple[list[dict], str | None, str | None, str | None]:
        """Assemble the agent's conversation context for the active space.

        Returns (recent_messages, results_prefix, memory_prefix, procedures_prefix) where:
        - recent_messages: messages since last compaction (the live thread)
        - results_prefix: receipts, system events, awareness (for ## RESULTS)
        - memory_prefix: compaction index + document (for ## MEMORY)
        """
        results_parts: list[str] = []
        memory_parts: list[str] = []

        # 1. Compaction index → MEMORY
        comp_state = await self.compaction.load_state(instance_id, active_space_id, member_id=member_id)
        if comp_state and comp_state.index_tokens > 0:
            index_text = await self.compaction.load_index(instance_id, active_space_id, member_id=member_id)
            if index_text:
                memory_parts.append(
                    f"Archived history (summaries — full archives available on request):\n"
                    f"{index_text}"
                )

        # 2. Proactive awareness → RESULTS (member-scoped per disclosure gate)
        awareness_block = await self._get_pending_awareness(
            instance_id, active_space_id, requesting_member_id=member_id,
        )
        if awareness_block:
            results_parts.append(awareness_block)

        # 2b. Cross-domain notices → RESULTS (one-time delivery)
        try:
            notices = await self.state.drain_space_notices(instance_id, active_space_id)
            if notices:
                notice_lines = [n["text"] for n in notices if n.get("text")]
                if notice_lines:
                    results_parts.append(
                        "CROSS-DOMAIN UPDATES:\n" + "\n".join(notice_lines)
                    )
                    logger.info("CROSS_DOMAIN_DELIVER: space=%s notices=%d", active_space_id, len(notice_lines))
        except Exception as exc:
            logger.warning("CROSS_DOMAIN_DELIVER: failed: %s", exc)

        # 2c. System events → RESULTS
        system_events = self.drain_system_events(instance_id)
        if system_events:
            events_block = "RECENT SYSTEM EVENTS:\n" + "\n".join(system_events)
            results_parts.append(events_block)
            logger.info(
                "SYSTEM_EVENTS_INJECTED: instance=%s count=%d",
                instance_id, len(system_events),
            )
            for evt in system_events:
                try:
                    await self.conv_logger.append(
                        instance_id=instance_id,
                        space_id=active_space_id,
                        speaker="system",
                        channel="internal",
                        content=evt,
                        member_id=member_id,
                    )
                except Exception:
                    pass

        # 3. Compaction document → MEMORY
        active_doc = await self.compaction.load_context_document(instance_id, active_space_id, member_id=member_id)
        if active_doc:
            memory_parts.append(
                f"Context history for this space:\n{active_doc}"
            )

        # 4. Parent briefing → MEMORY (for child domains)
        if active_space and active_space.parent_id:
            try:
                briefing = await self._load_parent_briefing(
                    instance_id, active_space.parent_id, active_space_id)
                if briefing:
                    parent = await self.state.get_context_space(instance_id, active_space.parent_id)
                    parent_name = parent.name if parent else "parent"
                    memory_parts.append(
                        f"Briefing from {parent_name} (may be stale — use remember() for current data):\n{briefing}"
                    )
            except Exception as exc:
                logger.warning("BRIEFING_LOAD: failed for space=%s: %s", active_space_id, exc)

        # 4b. File manifest → RESULTS (so agent knows what files exist)
        try:
            _files_dir = self._files._space_files_dir(instance_id, active_space_id)
            if _files_dir.exists():
                _visible = [
                    f.name for f in sorted(_files_dir.iterdir())
                    if f.is_file()
                    and not f.name.startswith("tr_")  # tool result cache
                    and not f.name.startswith("_plan.")  # plan internals
                    and not f.name.startswith(".")  # hidden files
                    and f.name != "_manifest.json"  # internal
                ]
                if _visible:
                    results_parts.append(
                        f"Files in this space: {', '.join(_visible)}"
                    )
        except Exception:
            pass

        # Unnamed agent pondering: gently remind the agent it has no name
        if member_id:
            try:
                _idb = getattr(self, '_instance_db', None)
                if _idb:
                    _mp = await _idb.get_member_profile(member_id)
                    if _mp and not _mp.get("agent_name") and _mp.get("interaction_count", 0) >= 5:
                        # Fire on every turn after 5+ interactions where unnamed
                        # The agent decides whether THIS is the right moment to bring it up
                        results_parts.append(
                            "[AWARENESS] You still don't have a name. This isn't urgent — "
                            "but it's on your mind. You don't need to bring it up every turn, "
                            "but when the moment feels right, wonder aloud about it. "
                            "Not as a task. As something you've been thinking about."
                        )
            except Exception:
                pass

        results_prefix = "\n\n".join(results_parts) if results_parts else None
        memory_prefix = "\n\n".join(memory_parts) if memory_parts else None

        # 5. Procedure files from scope chain → PROCEDURES section
        procedures_prefix = None
        if active_space and active_space_id:
            try:
                proc_parts: list[str] = []
                # Build scope chain for procedure inheritance
                _proc_chain = [active_space_id]
                _cur_space = active_space
                while _cur_space and _cur_space.parent_id:
                    _proc_chain.append(_cur_space.parent_id)
                    _cur_space = await self.state.get_context_space(instance_id, _cur_space.parent_id)
                for sid in _proc_chain:
                    content = await self._files.read_file(instance_id, sid, "_procedures.md")
                    if content and not content.startswith("Error:"):
                        if sid == active_space_id:
                            proc_parts.append(content)
                        else:
                            _pspace = await self.state.get_context_space(instance_id, sid)
                            _pname = _pspace.name if _pspace else sid
                            proc_parts.append(f"[From {_pname}]\n{content}")
                # SYSTEM-REFERENCE-CANVAS-SEED Pillar 3: fold Our Procedures
                # canvas pages (team-scoped) into the PROCEDURES block so they
                # sit alongside per-space _procedures.md. Advisory only — same
                # discipline as file-based procedures.
                try:
                    canvas_svc = self._get_canvas_service()
                    if canvas_svc is not None and self._instance_db is not None:
                        ours = await self._instance_db.find_canvas_by_name(
                            name="Our Procedures", scope="team",
                        )
                        if ours:
                            our_pages = await canvas_svc.page_list(
                                instance_id=instance_id,
                                canvas_id=ours["canvas_id"],
                            )
                            for pg in our_pages:
                                if pg.get("path") == "index.md":
                                    continue
                                pr = await canvas_svc.page_read(
                                    instance_id=instance_id,
                                    canvas_id=ours["canvas_id"],
                                    page_slug=pg["path"],
                                )
                                if pr.ok:
                                    body = pr.extra.get("body", "")
                                    if body.strip():
                                        proc_parts.append(
                                            f"[From Our Procedures: {pg.get('title') or pg['path']}]\n{body}"
                                        )
                except Exception as exc:
                    logger.debug("OUR_PROCEDURES_LOAD_FAILED: %s", exc)

                if proc_parts:
                    procedures_prefix = "\n\n".join(proc_parts)
            except Exception as exc:
                logger.warning("PROCEDURES_LOAD: failed for space=%s: %s", active_space_id, exc)

        # 6. Recent messages — read from space log (P2), fallback to legacy store
        recent_messages: list[dict] = []
        _context_source = "none"
        try:
            log_entries = await self.conv_logger.read_recent(
                instance_id, active_space_id,
                token_budget=SPACE_THREAD_TOKEN_BUDGET,
                max_messages=50,
                member_id=member_id,
            )
            if log_entries:
                recent_messages = [
                    {"role": e["role"], "content": e["content"]}
                    for e in log_entries
                ]
                _context_source = "space_log"
        except Exception as exc:
            logger.warning("CONTEXT_SOURCE: space=%s log_read_failed=%s", active_space_id, exc)

        if not recent_messages:
            # Fallback: no usable log entries — use legacy channel-specific store
            is_daily = active_space.is_default if active_space else False
            thread = await self.conversations.get_space_thread(
                instance_id, conversation_id, active_space_id,
                max_messages=50,
                include_untagged=is_daily,
                include_timestamp=True,
            )
            if comp_state and comp_state.last_compaction_at:
                thread = [
                    m for m in thread
                    if m.get("timestamp", "") > comp_state.last_compaction_at
                ]
            recent_messages = [
                {"role": m["role"], "content": m["content"]} for m in thread
            ]
            if not comp_state and not active_doc:
                recent_messages = self._truncate_to_budget(recent_messages, SPACE_THREAD_TOKEN_BUDGET)
            _context_source = "legacy_store"

        logger.info(
            "CONTEXT_SOURCE: space=%s source=%s entries=%d",
            active_space_id, _context_source, len(recent_messages),
        )

        # Sanitize: strip messages with empty content (e.g. from a file-only upload that
        # was stored before the empty-message guard was added). The Anthropic API returns
        # 400 on empty content strings.
        sanitized = []
        for m in recent_messages:
            if not m["content"] or not m["content"].strip():
                logger.warning(
                    "EMPTY_MSG_SANITIZE: dropping %s message with empty content from thread",
                    m["role"],
                )
                continue
            sanitized.append(m)
        recent_messages = sanitized

        # Sanitize: merge any trailing user messages (orphaned from rapid-fire or failed request).
        # The Anthropic API requires alternating roles. If consecutive user messages exist,
        # merge them into one so the content isn't lost. The agent sees all user input.
        merged_orphans: list[str] = []
        while recent_messages and recent_messages[-1]["role"] == "user":
            orphan = recent_messages.pop()
            _content = orphan["content"]
            # Silently discard completed plan step messages — they're stale internal turns
            if _content.startswith("[PLAN STEP "):
                logger.debug("ORPHANED_PLAN_STEP: discarded stale step message: %.80s", _content)
                continue
            merged_orphans.insert(0, _content)
            logger.info(
                "ORPHANED_USER_MSG: merging trailing user message into next turn. "
                "Content: %.100s",
                _content,
            )
        # Orphaned content will be prepended to the current user message in _phase_assemble
        if merged_orphans:
            self._orphaned_user_content = merged_orphans

        # Canvas context surface (CANVAS-V1 Pillar 6) — cacheable prefix zone
        canvases_prefix = await self._build_canvases_prefix(
            instance_id=instance_id, active_space_id=active_space_id,
            member_id=member_id,
        )

        return (
            recent_messages, results_prefix, memory_prefix,
            procedures_prefix, canvases_prefix,
        )

    async def _build_canvases_prefix(
        self, *, instance_id: str, active_space_id: str, member_id: str,
    ) -> str | None:
        """Render the 'Available Canvases' context zone for the active member.

        Filtering rules:
          - Member must have access (team scope OR explicit canvas_members row).
            Disclosure gate (:func:`filter_canvases_by_membership`) enforces
            this even if registry rows drift.
          - pinned_to_spaces empty → universal visibility
          - pinned_to_spaces set → visible only when active_space_id is in it

        Returns a short markdown list; None when nothing would render.
        """
        canvas_svc = self._get_canvas_service()
        if canvas_svc is None or not member_id:
            return None
        try:
            all_canvases = await canvas_svc.list_for_member(member_id=member_id)
        except Exception as exc:
            logger.debug("CANVAS_LIST_FOR_PREFIX_FAILED: %s", exc)
            return None

        # Defense-in-depth disclosure gate — mirror the knowledge-entry pattern.
        try:
            from kernos.kernel.disclosure_gate import filter_canvases_by_membership
            idb = getattr(self, "_instance_db", None)

            def _members_for(canvas_id: str) -> list[str]:
                # sync closure — we pre-fetch via run_until_complete is not
                # an option here, so fall back to empty list. The registry
                # already filtered via list_canvases_for_member; this filter
                # is a belt-and-braces pass for data-drift cases.
                return []

            all_canvases = filter_canvases_by_membership(
                all_canvases,
                requesting_member_id=member_id,
                canvas_member_lookup=_members_for,
                trace=None,
            )
        except Exception:
            pass

        visible: list[dict] = []
        for c in all_canvases:
            pinned = c.get("pinned_to_spaces") or []
            if isinstance(pinned, str):
                try:
                    import json as _j
                    pinned = _j.loads(pinned) if pinned else []
                except Exception:
                    pinned = []
            if pinned and active_space_id not in pinned:
                continue
            visible.append(c)

        if not visible:
            return None

        lines = ["Canvases available to you (use canvas_list / page_read to explore):"]
        for c in visible[:20]:
            cid = c.get("canvas_id") or c.get("space_id", "")
            name = c.get("name", cid)
            scope = c.get("scope", "")
            lines.append(f"- {name} ({scope}) [{cid}]")
        if len(visible) > 20:
            lines.append(f"…and {len(visible) - 20} more.")
        return "\n".join(lines)

    async def _get_pending_awareness(
        self, instance_id: str, active_space_id: str,
        requesting_member_id: str = "",
    ) -> str:
        """Get pending whispers formatted for the agent's context.

        DISCLOSURE-GATE: whispers authored by a different member are
        filtered when requesting_member_id is non-empty. Legacy whispers
        with empty owner_member_id are treated as instance-wide and
        visible to everyone (system/admin-level signals).
        """
        from kernos.kernel.awareness import SuppressionEntry

        whispers = await self.state.get_pending_whispers(instance_id)

        if not whispers:
            return ""

        # Filter to whispers targeting this space or with no space target
        relevant = [
            w for w in whispers
            if w.target_space_id == active_space_id
            or w.target_space_id == ""
            or w.source_space_id == active_space_id
        ]

        # Member scope: only surface whispers authored by the requesting
        # member, or instance-wide whispers (owner_member_id="").
        if requesting_member_id:
            before = len(relevant)
            relevant = [
                w for w in relevant
                if not getattr(w, "owner_member_id", "")
                or getattr(w, "owner_member_id", "") == requesting_member_id
            ]
            _filtered = before - len(relevant)
            if _filtered:
                logger.info(
                    "WHISPER_GATE: filtered=%d cross-member whispers for member=%s",
                    _filtered, requesting_member_id,
                )

        if not relevant:
            return ""

        # Busy-state suppression: during active plan execution, defer non-interrupt whispers
        try:
            from kernos.kernel.execution import load_plan
            data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
            _plan = await load_plan(data_dir, instance_id, active_space_id)
            if _plan and _plan.get("status") == "active":
                _deferred = [w for w in relevant if w.delivery_class != "interrupt"]
                if _deferred:
                    relevant = [w for w in relevant if w.delivery_class == "interrupt"]
                    logger.info("WHISPER_DEFERRED: plan=%s deferred=%d non-interrupt whispers",
                        _plan.get("plan_id", "?"), len(_deferred))
                    if not relevant:
                        return ""
        except Exception:
            pass

        # Sort: stage before ambient
        relevant.sort(key=lambda w: 0 if w.delivery_class == "stage" else 1)

        lines = ["## Proactive awareness (surface naturally — do not dump as a list)"]
        lines.append("")
        lines.append(
            "The following signals were detected since the last conversation. "
            "Weave relevant ones into your response naturally. "
            "If the user asks why you're mentioning something, you can draw "
            "on the reasoning trace."
        )
        lines.append("")

        for w in relevant:
            lines.append(f"- [{w.delivery_class.upper()}] (id: {w.whisper_id}) {w.insight_text}")
            lines.append(f"  Reasoning: {w.reasoning_trace}")
            lines.append("")

        lines.append(
            "If the user says they already know about something or don't want "
            "to hear about it, use dismiss_whisper(whisper_id) to suppress it."
        )

        # Mark as surfaced and create suppression entries
        for w in relevant:
            w.surfaced_at = datetime.now(timezone.utc).isoformat()
            await self.state.mark_whisper_surfaced(instance_id, w.whisper_id)

            suppression = SuppressionEntry(
                whisper_id=w.whisper_id,
                knowledge_entry_id=w.knowledge_entry_id,
                foresight_signal=w.foresight_signal,
                created_at=w.created_at,
                resolution_state="surfaced",
            )
            await self.state.save_suppression(instance_id, suppression)

        logger.info("AWARENESS: injected whispers=%d for space=%s",
                     len(relevant), active_space_id)

        return "\n".join(lines)

    async def _handle_file_upload(
        self,
        instance_id: str,
        active_space_id: str,
        filename: str,
        content: str,
    ) -> str:
        """Handle a user-uploaded text file.

        Same storage as agent-created files. Same read_file() interface.
        Returns a notification string to prepend to the user's message context.
        """
        try:
            content.encode("utf-8")
        except (UnicodeEncodeError, AttributeError):
            return "I can only handle text files right now — images and PDFs are coming soon."

        description = f"Uploaded by user on {utc_now()[:10]}"
        await self._files.write_file(
            instance_id, active_space_id, filename, content, description
        )
        return f"[File uploaded: {filename}. You can read it with read_file if needed.]"

    async def _run_session_exit(
        self, instance_id: str, space_id: str, conversation_id: str
    ) -> None:
        """Update space name/description based on what happened in this session."""
        space = await self.state.get_context_space(instance_id, space_id)
        if not space or space.is_default:
            return

        # Get messages tagged to this space from the conversation
        session_messages = await self.conversations.get_space_thread(
            instance_id, conversation_id, space_id, max_messages=30
        )
        if len(session_messages) < 3:
            return  # Too short to update description

        formatted = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Agent'}: {str(m.get('content', ''))[:200]}"
            for m in session_messages[-20:]
        )

        EXIT_SCHEMA = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"}
            },
            "required": ["name", "description"],
            "additionalProperties": False
        }

        try:
            result_str = await self.reasoning.complete_simple(
                system_prompt=(
                    "Review this conversation session and update the space name and description. "
                    "The description helps the router understand what this space is about. "
                    "Rename the space if the session revealed something the name misses. "
                    "Keep description to 1-3 sentences. Be specific and concrete."
                ),
                user_content=(
                    f"Space: {space.name}\n"
                    f"Current description: {space.description}\n\n"
                    f"Session:\n{formatted}"
                ),
                output_schema=EXIT_SCHEMA,
                max_tokens=256,
                prefer_cheap=True,
            )
            parsed = __import__("json").loads(result_str)
            updates: dict = {}
            if parsed.get("name") and parsed["name"] != space.name:
                updates["name"] = parsed["name"]
            if parsed.get("description") and parsed["description"] != space.description:
                updates["description"] = parsed["description"]
            if updates:
                await self.state.update_context_space(instance_id, space_id, updates)
                logger.info("Session exit updated space %s: %s", space_id, updates)
        except Exception as exc:
            logger.warning("Session exit maintenance failed for %s: %s", space_id, exc)

    async def _enforce_space_cap(self, instance_id: str) -> None:
        """Archive the least recently used space if at the active cap."""
        spaces = await self.state.list_context_spaces(instance_id)
        active = [s for s in spaces if s.status == "active" and not s.is_default and s.space_type != "system"]
        if len(active) < ACTIVE_SPACE_CAP:
            return
        lru = sorted(active, key=lambda s: s.last_active_at)[0]
        await self.state.update_context_space(instance_id, lru.id, {"status": "archived"})
        try:
            await emit_event(
                self.events,
                EventType.CONTEXT_SPACE_SUSPENDED,
                instance_id,
                "space_cap",
                payload={"space_id": lru.id, "name": lru.name, "reason": "lru_sunset"},
            )
        except Exception as exc:
            logger.warning("Failed to emit context.space.suspended: %s", exc)
        logger.info("Archived LRU space %s (%s) for instance %s", lru.id, lru.name, instance_id)


    # --- Domain assessment (CS-2) ---

    DOMAIN_ASSESSMENT_SCHEMA: dict = {
        "type": "object",
        "properties": {
            "create_domain": {"type": "boolean"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "posture": {"type": "string",
                        "description": "Brief working style for this domain. "
                        "How should the agent approach work here? One sentence. "
                        "Examples: 'Creative and improvisational', "
                        "'Precise and action-oriented', 'Warm and supportive'."},
            "reasoning": {"type": "string"},
            "rename": {"type": "boolean"},
            "new_name": {"type": "string"},
            "rename_evidence": {"type": "string"},
            "migrate_covenants": {
                "type": "array", "items": {"type": "string"},
                "description": "IDs of parent covenants that belong in the new domain",
            },
            "migrate_files": {
                "type": "array", "items": {"type": "string"},
                "description": "Filenames from parent that belong in the new domain",
            },
            "migrate_procedure_sections": {
                "type": "array", "items": {"type": "string"},
                "description": "Section titles from parent _procedures.md that belong in the new domain",
            },
        },
        "required": ["create_domain", "confidence", "name", "description", "posture", "reasoning", "rename", "new_name", "rename_evidence", "migrate_covenants", "migrate_files", "migrate_procedure_sections"],
        "additionalProperties": False,
    }

    async def _process_compaction_follow_ups(
        self, instance_id: str, space_id: str, commitments: list[dict],
    ) -> None:
        """Process follow-ups extracted from compaction → create triggers.

        Deduplicates against existing triggers (description + due within 2 days).

        CLEANUP-BATCH-V1 item 8: emits a COMPACTION_FOLLOW_UP_PROCESSED
        receipt event regardless of outcome (succeeded / empty / failed)
        so silent-no-op regressions are observable in the event stream
        instead of disappearing into log noise.
        """
        from kernos.kernel.scheduler import Trigger, _trigger_id, compute_next_fire
        from datetime import timedelta
        from kernos.utils import utc_now_dt

        now = utc_now_dt()

        # Empty-input receipt — emit and return so callers don't have to
        # know whether the work even ran.
        if not commitments:
            try:
                await emit_event(
                    self.events, EventType.COMPACTION_FOLLOW_UP_PROCESSED,
                    instance_id, "compaction",
                    payload={
                        "status": "empty",
                        "space_id": space_id,
                        "input_count": 0,
                        "created_count": 0,
                        "skipped_count": 0,
                        "skip_reasons": [],
                    },
                )
            except Exception as exc:
                logger.warning("FOLLOW_UP_RECEIPT: emit failed: %s", exc)
            return

        skip_reasons: list[str] = []
        created = 0

        try:
            # Load existing triggers for dedup
            existing = await self._trigger_store.list_all(instance_id)
            existing_descs = [(t.action_description.lower(), t.next_fire_at) for t in existing if t.status == "active"]

            _type_messages = {
                "USER_COMMITMENT": "You mentioned you'd {desc}. Just a reminder.",
                "AGENT_COMMITMENT": "I committed to {desc}. Following up now.",
                "EXTERNAL_DEADLINE": "Deadline approaching: {desc}.",
                "FOLLOW_UP": "Time to check back on: {desc}.",
            }

            for c in commitments:
                desc = c.get("description", "")
                if not desc:
                    skip_reasons.append("missing_description")
                    continue
                ctype = c.get("type", "FOLLOW_UP")
                due_raw = c.get("due", "")
                context = c.get("context", "")

                # Parse due date
                due_dt = None
                if due_raw:
                    due_lower = due_raw.lower().strip()
                    if due_lower == "soon":
                        due_dt = now + timedelta(days=1)
                    elif due_lower == "next_week":
                        due_dt = now + timedelta(days=7)
                    elif due_lower.startswith("20"):
                        try:
                            from datetime import datetime as _dt
                            due_dt = _dt.fromisoformat(due_lower.replace("Z", "+00:00"))
                            if due_dt.tzinfo is None:
                                due_dt = due_dt.replace(tzinfo=timezone.utc)
                        except (ValueError, TypeError):
                            due_dt = now + timedelta(days=3)
                    else:
                        due_dt = now + timedelta(days=3)
                else:
                    due_dt = now + timedelta(days=3)

                # 90-day horizon check
                if due_dt and (due_dt - now).days > 90:
                    logger.info("FOLLOW_UP_SKIP: desc=%r reason=beyond_90_days", desc[:60])
                    skip_reasons.append("beyond_90_days")
                    continue

                # Dedup: check if similar trigger exists
                _dup = False
                due_iso = due_dt.isoformat() if due_dt else ""
                for ex_desc, ex_due in existing_descs:
                    if desc.lower()[:40] in ex_desc or ex_desc[:40] in desc.lower():
                        # Similar description — check date proximity
                        if ex_due and due_iso:
                            try:
                                from datetime import datetime as _dt
                                ex_dt = _dt.fromisoformat(ex_due.replace("Z", "+00:00"))
                                if ex_dt.tzinfo is None:
                                    ex_dt = ex_dt.replace(tzinfo=timezone.utc)
                                if abs((due_dt - ex_dt).days) <= 2:
                                    _dup = True
                                    break
                            except (ValueError, TypeError):
                                pass
                        elif not ex_due and not due_iso:
                            _dup = True
                            break
                if _dup:
                    logger.info("FOLLOW_UP_DUPLICATE: desc=%r", desc[:60])
                    skip_reasons.append("duplicate")
                    continue

                # Build trigger message
                msg_template = _type_messages.get(ctype, "Reminder: {desc}.")
                msg = msg_template.format(desc=desc)
                if context:
                    msg += f" (Context: {context})"

                # Determine delivery class
                delivery_class = "ambient"  # Default: whisper
                if ctype == "EXTERNAL_DEADLINE" and due_dt and (due_dt - now).days <= 1:
                    delivery_class = "interrupt"  # Urgent deadline

                # Create trigger
                trigger = Trigger(
                    trigger_id=_trigger_id(),
                    instance_id=instance_id,
                    space_id=space_id,
                    condition_type="time",
                    condition=due_iso,
                    next_fire_at=due_iso,
                    recurrence="",  # One-shot
                    action_type="notify",
                    action_description=msg,
                    action_params={},
                    delivery_class=delivery_class,
                    status="active",
                    created_at=utc_now(),
                    source="compaction_follow_up",
                )
                await self._trigger_store.save(trigger)
                created += 1
                logger.info("FOLLOW_UP_CREATED: type=%s desc=%r due=%s source=compaction",
                    ctype, desc[:60], due_iso[:10])

            if created:
                logger.info("FOLLOW_UP_TOTAL: created=%d from_compaction=%d", created, len(commitments))

        except Exception as exc:
            logger.warning("FOLLOW_UP: processing raised: %s", exc)
            try:
                await emit_event(
                    self.events, EventType.COMPACTION_FOLLOW_UP_PROCESSED,
                    instance_id, "compaction",
                    payload={
                        "status": "failed",
                        "space_id": space_id,
                        "input_count": len(commitments),
                        "created_count": created,
                        "skipped_count": len(skip_reasons),
                        "skip_reasons": skip_reasons,
                        "error": str(exc),
                    },
                )
            except Exception as emit_exc:
                logger.warning("FOLLOW_UP_RECEIPT: emit failed: %s", emit_exc)
            raise

        # Success-path receipt — emitted regardless of whether
        # individual rows were skipped vs created.
        try:
            await emit_event(
                self.events, EventType.COMPACTION_FOLLOW_UP_PROCESSED,
                instance_id, "compaction",
                payload={
                    "status": "succeeded",
                    "space_id": space_id,
                    "input_count": len(commitments),
                    "created_count": created,
                    "skipped_count": len(skip_reasons),
                    "skip_reasons": skip_reasons,
                },
            )
        except Exception as exc:
            logger.warning("FOLLOW_UP_RECEIPT: emit failed: %s", exc)

    async def _assess_domain_creation(
        self, instance_id: str, space_id: str, space: ContextSpace, comp_state: "CompactionState",
    ) -> None:
        """Assess whether compacted conversation constitutes a new domain.

        Runs after compaction completes. Only HIGH confidence creates domains.
        """
        import uuid as _uuid
        import json as _json

        # Only assess from general or parent spaces (depth < 2)
        if space.space_type not in ("general", "domain"):
            return
        if space.depth >= 2:
            return

        # Load the freshly compacted document
        doc = await self.compaction.load_document(instance_id, space_id)
        if not doc:
            return

        # Build existing space list for context
        all_spaces = await self.state.list_context_spaces(instance_id)
        existing = [
            f"- {s.name} ({s.space_type}, depth={s.depth})"
            for s in all_spaces if s.status == "active" and s.space_type != "system"
        ]

        # Build parent content inventory for migration assessment
        _inv_parts: list[str] = []
        try:
            _parent_rules = await self.state.query_covenant_rules(
                instance_id, context_space_scope=[space_id], active_only=True)
            if _parent_rules:
                _inv_parts.append("Covenants:\n" + "\n".join(
                    f"  [{r.id}] {r.rule_type}: {r.description}" for r in _parent_rules))
            _parent_manifest = await self._files.load_manifest(instance_id, space_id)
            if _parent_manifest:
                _inv_parts.append("Files:\n" + "\n".join(
                    f"  {fname}: {desc}" for fname, desc in _parent_manifest.items() if not fname.startswith(".")))
            _parent_procs = await self._files.read_file(instance_id, space_id, "_procedures.md")
            if _parent_procs and not _parent_procs.startswith("Error:"):
                _sections = [line.strip() for line in _parent_procs.split("\n") if line.startswith("## ")]
                if _sections:
                    _inv_parts.append("Procedure sections:\n" + "\n".join(f"  {s}" for s in _sections))
        except Exception:
            pass
        _parent_inventory = "\n".join(_inv_parts) if _inv_parts else "(no content to migrate)"

        child_type = "domain" if space.depth == 0 else "subdomain"

        try:
            result_str = await self.reasoning.complete_simple(
                system_prompt=(
                    "You are assessing whether a conversation belongs in its own "
                    f"dedicated context {child_type}, or should remain in the current space.\n\n"
                    "Domains can come from ANY area of someone's life — business, legal, "
                    "health, family, finance, creative work, property, education, hobbies, "
                    "relationships, or anything else with recurring depth.\n\n"
                    "Only create on HIGH confidence. A domain should:\n"
                    "- Have clear internal coherence (not a grab-bag)\n"
                    "- Likely recur in future conversations\n"
                    "- Benefit from isolated context (for BOTH domain AND parent)\n"
                    "- Have a stable, clear label\n\n"
                    "A single conversation about a topic is NOT enough. "
                    "The topic must have depth and likely recurrence.\n"
                    '"Kitchen Renovation" is a domain. "Tax Prep 2026" is a domain. '
                    '"Dog Training" is a domain. "Random questions" is not.\n\n'
                    "RENAME CHECK: Has the user indicated a NAME CHANGE for this space? "
                    'Look for explicit statements like "let\'s call it X" or "we\'re renaming to X." '
                    "If yes, set rename=true, new_name to the new name, and rename_evidence.\n\n"
                    "MIGRATION: If creating a domain, review the parent's content inventory below. "
                    "Identify covenants, files, and procedure sections that are SPECIFIC to the new "
                    "domain and should move there. Use semantic understanding, not just name matching. "
                    "'Stay in character during roleplay' belongs in a D&D domain even if it doesn't "
                    "say 'D&D'. Return IDs/names in the migrate_* arrays. Leave empty arrays if nothing to migrate."
                ),
                user_content=(
                    f"Current space: {space.name} (depth={space.depth})\n"
                    f"Existing spaces:\n" + ("\n".join(existing) or "(none)") + "\n\n"
                    f"Compaction summary:\n{doc[:3000]}\n\n"
                    f"Parent content (for migration if creating domain):\n{_parent_inventory}"
                ),
                output_schema=self.DOMAIN_ASSESSMENT_SCHEMA,
                max_tokens=512,
                prefer_cheap=True,
            )
            parsed = _json.loads(result_str)

            # Handle explicit rename (independent of domain creation)
            if parsed.get("rename") and parsed.get("new_name", "").strip():
                new_name_rename = parsed["new_name"].strip()
                old_name = space.name
                aliases = list(space.aliases)
                if old_name and old_name not in aliases:
                    aliases.append(old_name)
                await self.state.update_context_space(instance_id, space_id, {
                    "name": new_name_rename,
                    "aliases": aliases,
                    "renamed_from": old_name,
                    "renamed_at": utc_now(),
                })
                logger.info("DOMAIN_RENAME: space=%s old=%s new=%s evidence=%r",
                    space_id, old_name, new_name_rename, parsed.get("rename_evidence", ""))

            if not parsed.get("create_domain"):
                logger.info(
                    "DOMAIN_ASSESS: space=%s result=keep confidence=%s reason=%r",
                    space_id, parsed.get("confidence", "?"), parsed.get("reasoning", ""),
                )
                return

            if parsed.get("confidence") != "high":
                logger.info(
                    "DOMAIN_ASSESS: space=%s result=skip_low_confidence confidence=%s",
                    space_id, parsed.get("confidence", "?"),
                )
                return

            # Check for duplicate or drift (similar name to existing)
            new_name = parsed.get("name", "").strip()
            if not new_name:
                return
            for s in all_spaces:
                if s.name.lower() == new_name.lower() or new_name.lower() in [a.lower() for a in s.aliases]:
                    logger.info("DOMAIN_ASSESS: space=%s result=duplicate name=%s existing=%s", space_id, new_name, s.id)
                    return
                # Drift detection: similar but not identical name
                all_names = [s.name.lower()] + [a.lower() for a in s.aliases]
                if _is_similar_topic(new_name, all_names):
                    logger.info("DOMAIN_DRIFT: assessed=%s matches=%s (%s) — skipping creation",
                        new_name, s.name, s.id)
                    return

            # Enforce space cap
            await self._enforce_space_cap(instance_id)

            now = utc_now()
            new_space = ContextSpace(
                id=f"space_{_uuid.uuid4().hex[:8]}",
                instance_id=instance_id,
                member_id=space.member_id,  # Inherit from parent
                name=new_name,
                description=parsed.get("description", ""),
                posture=parsed.get("posture", ""),
                space_type=child_type,
                status="active",
                is_default=False,
                parent_id=space_id,
                depth=space.depth + 1,
                created_at=now,
                last_active_at=now,
            )
            await self.state.save_context_space(new_space)

            # Initialize compaction state with reference-based origin
            try:
                from kernos.kernel.compaction import (
                    CompactionState as _CS,
                    compute_document_budget,
                    estimate_headroom,
                    MODEL_MAX_TOKENS,
                    COMPACTION_MODEL_USABLE_TOKENS,
                    COMPACTION_INSTRUCTION_TOKENS,
                )
                headroom = await estimate_headroom(self.reasoning, new_space)
                context_def = (
                    f"Space: {new_space.name}\nType: {new_space.space_type}\n"
                    f"Description: {new_space.description}\nPosture: {new_space.posture}\n"
                )
                context_def_tokens = await self.compaction.adapter.count_tokens(context_def)
                system_overhead = 4000
                doc_budget = compute_document_budget(
                    MODEL_MAX_TOKENS, system_overhead, 0, headroom
                )
                new_comp = _CS(
                    space_id=new_space.id,
                    conversation_headroom=headroom,
                    document_budget=doc_budget,
                    message_ceiling=min(
                        doc_budget,
                        COMPACTION_MODEL_USABLE_TOKENS - COMPACTION_INSTRUCTION_TOKENS - context_def_tokens,
                    ),
                    _context_def_tokens=context_def_tokens,
                    _system_overhead=system_overhead,
                )
                await self.compaction.save_state(instance_id, new_space.id, new_comp)

                # Write reference-based origin document
                origin_doc = (
                    f"## Origin\n"
                    f"This domain originated from {space.name}, "
                    f"compaction #{comp_state.global_compaction_number}.\n"
                    f"Use remember() to retrieve historical context from the parent.\n"
                )
                origin_path = self.compaction._space_dir(instance_id, new_space.id) / "active_document.md"
                origin_path.parent.mkdir(parents=True, exist_ok=True)
                origin_path.write_text(origin_doc, encoding="utf-8")
            except Exception as exc:
                logger.warning("Failed to init compaction for domain %s: %s", new_space.id, exc)

            try:
                from kernos.kernel.event_types import EventType as _ET
                await emit_event(self.events, _ET.CONTEXT_SPACE_CREATED, instance_id, "domain_assessment",
                    payload={"space_id": new_space.id, "name": new_space.name,
                             "description": new_space.description, "parent_id": space_id,
                             "depth": new_space.depth})
            except Exception:
                pass

            logger.info(
                "DOMAIN_CREATE: space=%s name=%s parent=%s depth=%d confidence=%s",
                new_space.id, new_space.name, space_id, new_space.depth, parsed.get("confidence"),
            )

            # Content migration: move LLM-identified domain-specific content
            try:
                await self._migrate_domain_content(
                    instance_id, space_id, new_space.id, parsed)
            except Exception as mig_exc:
                logger.warning("DOMAIN_MIGRATE: failed for %s: %s", new_space.id, mig_exc)

        except Exception as exc:
            logger.warning("DOMAIN_ASSESS: failed for space=%s: %s", space_id, exc)

    async def _migrate_domain_content(
        self, instance_id: str, parent_id: str, child_id: str,
        migrate_lists: dict,
    ) -> None:
        """Migrate domain-specific content from parent to child using LLM-selected lists.

        The domain assessment LLM identified which covenants, files, and procedure
        sections belong in the new domain. This method executes those moves.
        """
        migrated: dict[str, list[str]] = {"covenants": [], "files": [], "procedures": []}

        # 1. Migrate covenants by ID
        cov_ids = migrate_lists.get("migrate_covenants", [])
        for cov_id in cov_ids:
            try:
                await self.state.update_contract_rule(instance_id, cov_id, {"context_space": child_id})
                migrated["covenants"].append(cov_id)
            except Exception as exc:
                logger.warning("DOMAIN_MIGRATE: covenant %s failed: %s", cov_id, exc)

        # 2. Migrate procedure sections by title
        section_titles = set(migrate_lists.get("migrate_procedure_sections", []))
        if section_titles:
            try:
                parent_procs = await self._files.read_file(instance_id, parent_id, "_procedures.md")
                if parent_procs and not parent_procs.startswith("Error:"):
                    sections = parent_procs.split("\n## ")
                    keep: list[str] = []
                    move: list[str] = []
                    for i, section in enumerate(sections):
                        full = ("## " + section) if i > 0 else section
                        title = section.split("\n")[0].strip().lstrip("# ").strip()
                        if title in section_titles or f"## {title}" in section_titles:
                            move.append(full)
                            migrated["procedures"].append(title)
                        else:
                            keep.append(full)
                    if move:
                        await self._files.write_file(
                            instance_id, child_id, "_procedures.md",
                            "\n\n".join(move), "Domain procedures migrated from parent")
                        remaining = "\n\n".join(s for s in keep if s.strip())
                        if remaining.strip():
                            await self._files.write_file(
                                instance_id, parent_id, "_procedures.md", remaining,
                                "Procedures (domain-specific sections migrated)")
                        else:
                            await self._files.delete_file(instance_id, parent_id, "_procedures.md")
            except Exception as exc:
                logger.warning("DOMAIN_MIGRATE: procedure migration failed: %s", exc)

        # 3. Migrate files by name
        file_names = migrate_lists.get("migrate_files", [])
        for fname in file_names:
            try:
                if fname.startswith("_") or fname.startswith("."):
                    continue
                content = await self._files.read_file(instance_id, parent_id, fname)
                if content and not content.startswith("Error:"):
                    manifest = await self._files.load_manifest(instance_id, parent_id)
                    desc = manifest.get(fname, "Migrated from parent")
                    await self._files.write_file(instance_id, child_id, fname, content, desc)
                    await self._files.delete_file(instance_id, parent_id, fname)
                    migrated["files"].append(fname)
            except Exception as exc:
                logger.warning("DOMAIN_MIGRATE: file %s failed: %s", fname, exc)

        total = sum(len(v) for v in migrated.values())
        if total > 0:
            logger.info("DOMAIN_MIGRATE: space=%s from=%s covenants=%d procedures=%d files=%d",
                child_id, parent_id, len(migrated["covenants"]),
                len(migrated["procedures"]), len(migrated["files"]))
            for cat, items in migrated.items():
                for item in items:
                    logger.info("DOMAIN_MIGRATE_ITEM: type=%s item=%s action=moved", cat, item)

    async def _produce_child_briefings(
        self, instance_id: str, space_id: str, space: ContextSpace,
    ) -> None:
        """Produce context briefings for all child domains after parent compaction."""
        children = await self.state.list_child_spaces(instance_id, space_id)
        if not children:
            return

        # Load the freshly compacted document (Living State)
        doc = await self.compaction.load_document(instance_id, space_id)
        if not doc:
            return

        for child in children:
            try:
                briefing = await self.reasoning.complete_simple(
                    system_prompt=(
                        "You are producing a context briefing for a child domain. "
                        "Extract ONLY durable truths relevant to the child domain. "
                        "Keep it short — 3-8 bullet points of facts, decisions, "
                        "and active status. No narrative. No history."
                    ),
                    user_content=(
                        f"Parent: {space.name}\n"
                        f"Child: {child.name} — {child.description}\n\n"
                        f"Parent's current state:\n{doc[:4000]}"
                    ),
                    max_tokens=512,
                    prefer_cheap=True,
                )
                if briefing and briefing.strip():
                    briefing_path = (
                        self.compaction._space_dir(instance_id, space_id)
                        / f"briefing_{child.id}.md"
                    )
                    briefing_path.parent.mkdir(parents=True, exist_ok=True)
                    briefing_path.write_text(briefing.strip(), encoding="utf-8")
                    logger.info("BRIEFING_PRODUCED: parent=%s child=%s chars=%d",
                        space_id, child.id, len(briefing))
            except Exception as exc:
                logger.warning("BRIEFING_FAILED: parent=%s child=%s error=%s", space_id, child.id, exc)

    async def _load_parent_briefing(
        self, instance_id: str, parent_id: str, child_id: str,
    ) -> str | None:
        """Load a parent's briefing for a specific child. Returns None if not found."""
        briefing_path = (
            self.compaction._space_dir(instance_id, parent_id)
            / f"briefing_{child_id}.md"
        )
        if not briefing_path.exists():
            return None
        return briefing_path.read_text(encoding="utf-8")

    # --- Downward search (CS-5) ---

    async def _downward_search(
        self, instance_id: str, query: str, target_space_ids: list[str],
        requesting_member_id: str = "", trace: Any = None,
    ) -> str | None:
        """Search DOWN into child domains for an answer to a quick question.

        DISCLOSURE-GATE: entries authored by members other than the requesting
        member are filtered per the simplified relationship permission model.
        Without this, query-mode routing (e.g., "has Emma been using this?")
        would surface cross-member personal content to the asking member.
        """
        import json as _json

        # Collect knowledge from target spaces and their children
        all_knowledge = await self.state.query_knowledge(
            instance_id, active_only=True, limit=500)

        # Gate cross-member entries before space matching runs.
        if requesting_member_id:
            from kernos.kernel.disclosure_gate import (
                build_permission_map, filter_knowledge_entries,
            )
            _perm_map = await build_permission_map(
                getattr(self, '_instance_db', None), requesting_member_id,
            )
            all_knowledge = filter_knowledge_entries(
                all_knowledge,
                requesting_member_id=requesting_member_id,
                permission_map=_perm_map,
                trace=trace,
            )

        results_by_space: dict[str, list[str]] = {}
        for space_id in target_space_ids:
            space_ke = [
                k for k in all_knowledge
                if k.context_space == space_id
            ]
            # Also check children of this target
            children = await self.state.list_child_spaces(instance_id, space_id)
            for child in children:
                space_ke.extend([k for k in all_knowledge if k.context_space == child.id])

            if space_ke:
                results_by_space[space_id] = [k.content for k in space_ke[:20]]

        if not results_by_space:
            logger.info("DOWNWARD_SEARCH_MISS: query=%r searched=%d found_in=none",
                query[:60], len(target_space_ids))
            return None

        # Use cheap model to resolve the answer
        space_names = {}
        for sid in results_by_space:
            s = await self.state.get_context_space(instance_id, sid)
            space_names[sid] = s.name if s else sid

        context_parts = []
        for sid, facts in results_by_space.items():
            context_parts.append(f"From {space_names[sid]}:\n" + "\n".join(f"- {f}" for f in facts))

        try:
            answer = await self.reasoning.complete_simple(
                system_prompt=(
                    "Answer this question using ONLY the provided context from the user's "
                    "other domains. If you can answer, include which domain the answer came from. "
                    "If you can't answer from the context, say so briefly."
                ),
                user_content=(
                    f"Question: {query}\n\n"
                    + "\n\n".join(context_parts)
                ),
                max_tokens=256,
                prefer_cheap=True,
            )

            if answer and "can't answer" not in answer.lower() and "cannot answer" not in answer.lower():
                matched_spaces = list(results_by_space.keys())
                if len(matched_spaces) == 1:
                    logger.info("DOWNWARD_SEARCH_HIT: query=%r found_in=%s", query[:60], matched_spaces[0])
                else:
                    logger.info("DOWNWARD_SEARCH_HIT: query=%r found_in=%s", query[:60], matched_spaces)
                return f"[Quick answer from other context]\n{answer}"

            logger.info("DOWNWARD_SEARCH_MISS: query=%r searched=%d found_in=none",
                query[:60], len(target_space_ids))
            return None
        except Exception as exc:
            logger.warning("DOWNWARD_SEARCH: failed: %s", exc)
            return None

    # --- Cross-domain signals (CS-5) ---

    SIGNAL_ASSESSMENT_SCHEMA: dict = {
        "type": "object",
        "properties": {
            "signal_worthy": {"type": "boolean"},
            "signal_text": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["signal_worthy", "signal_text", "reason"],
        "additionalProperties": False,
    }

    async def _check_cross_domain_signals(
        self, instance_id: str, space_id: str,
        user_message: str, agent_response: str,
    ) -> None:
        """Post-turn check for cross-domain entity mentions with meaningful updates."""
        import json as _json

        if not user_message.strip():
            return

        # Get all knowledge entries
        all_knowledge = await self.state.query_knowledge(
            instance_id, active_only=True, limit=500)

        # Build scope chain for current space
        from kernos.kernel.retrieval import RetrievalService
        _rs = RetrievalService.__new__(RetrievalService)
        _rs.state = self.state
        current_chain = set(await _rs._build_scope_chain(instance_id, space_id))

        # Find knowledge entries in OTHER domains that mention entities from this turn
        combined = f"{user_message} {agent_response}".lower()
        cross_matches: list[tuple[str, Any]] = []  # (entity_text, KnowledgeEntry)
        seen_spaces: set[str] = set()
        for ke in all_knowledge:
            if not ke.context_space or ke.context_space in current_chain or ke.context_space in ("", None):
                continue
            # Check if any entity from this knowledge appears in the turn
            # Use subject as the entity identifier
            if ke.subject and ke.subject != "user" and ke.subject.lower() in combined:
                if ke.context_space not in seen_spaces:
                    cross_matches.append((ke.subject, ke))
                    seen_spaces.add(ke.context_space)

        if not cross_matches:
            return

        logger.info("CROSS_DOMAIN_CHECK: entities=%s cross_matches=%d",
            [m[0] for m in cross_matches], len(cross_matches))

        # Assess worthiness with cheap model
        try:
            result_str = await self.reasoning.complete_simple(
                system_prompt=(
                    "Determine if this conversation turn contains a MEANINGFUL UPDATE "
                    "about the named entity — a status change, new commitment, factual update, "
                    "or schedule change. Casual mentions, questions, or references without "
                    "new information are NOT signal-worthy."
                ),
                user_content=(
                    f"User: {user_message[:500]}\n"
                    f"Agent: {agent_response[:500]}\n\n"
                    f"Entities found in other domains: {[m[0] for m in cross_matches]}"
                ),
                output_schema=self.SIGNAL_ASSESSMENT_SCHEMA,
                max_tokens=128,
                prefer_cheap=True,
            )
            parsed = _json.loads(result_str)

            if not parsed.get("signal_worthy"):
                logger.info("CROSS_DOMAIN_SKIP: entities=%s reason=%r",
                    [m[0] for m in cross_matches], parsed.get("reason", ""))
                return

            signal_text = parsed.get("signal_text", "")
            if not signal_text:
                return

            # Get current space name for attribution
            current_space = await self.state.get_context_space(instance_id, space_id)
            source_name = current_space.name if current_space else space_id

            for entity_name, ke in cross_matches:
                notice_text = f"[From {source_name}] {signal_text}"
                await self.state.append_space_notice(
                    instance_id, ke.context_space, notice_text,
                    source=space_id, notice_type="cross_domain",
                )
                logger.info("CROSS_DOMAIN_SIGNAL: target=%s source=%s signal=%s",
                    ke.context_space, space_id, notice_text[:80])

        except Exception as exc:
            logger.warning("CROSS_DOMAIN_CHECK: assessment failed: %s", exc)

    async def _update_conversation_summary(
        self, instance_id: str, conversation_id: str, platform: str
    ) -> None:
        now = utc_now()
        try:
            summary = await self.state.get_conversation_summary(
                instance_id, conversation_id
            )
            if summary is None:
                summary = ConversationSummary(
                    instance_id=instance_id,
                    conversation_id=conversation_id,
                    platform=platform,
                    message_count=1,
                    first_message_at=now,
                    last_message_at=now,
                )
            else:
                summary.message_count += 1
                summary.last_message_at = now
            await self.state.save_conversation_summary(summary)
        except Exception as exc:
            logger.warning("Failed to update conversation summary: %s", exc)

    # -----------------------------------------------------------------------
    # Turn Serialization — Per-Space Mailbox/Runner
    # -----------------------------------------------------------------------

    def _get_space_lock(self, instance_id: str, space_id: str) -> asyncio.Lock:
        """Per-(instance, space) mutation lock. Lazily created.

        CROSS_SPACE_REQUESTS_V1 (Q1): the turn processor wraps the
        body of each turn in ``async with self._get_space_lock(...)``
        so cross-space requests targeting that space wait until the
        current turn completes. Cross-space dispatch acquires the
        target's lock with a bounded timeout (default 30s) and
        returns ``failed`` / ``timeout_waiting_for_target`` when
        the wait exceeds the bound.
        """
        key = (instance_id, space_id)
        if key not in self._space_locks:
            self._space_locks[key] = asyncio.Lock()
        return self._space_locks[key]

    def _get_runner(self, instance_id: str, space_id: str) -> SpaceRunner:
        """Get or create the runner for a (tenant, space) pair."""
        key = f"{instance_id}:{space_id}"
        if key not in self._runners:
            runner = SpaceRunner(
                instance_id=instance_id,
                space_id=space_id,
                mailbox=asyncio.Queue(),
            )
            runner._task = asyncio.create_task(
                self._run_space_loop(runner),
                name=f"runner:{key}",
            )
            self._runners[key] = runner
        return self._runners[key]

    async def _run_space_loop(self, runner: SpaceRunner) -> None:
        """Process turns sequentially for one (tenant, space) pair.

        Pulls messages from the mailbox, merges rapid follow-ups,
        processes one turn at a time, delivers responses.
        """
        while True:
            merged_messages: list[tuple[NormalizedMessage, TurnContext, asyncio.Future]] = []
            # CROSS_SPACE_REQUESTS_V1 (Q1): lock acquired after merge
            # handling, released in the iteration's finally so cross-
            # space requests targeting this space wait until the
            # current turn completes regardless of how the iteration
            # exits.
            _space_lock: asyncio.Lock | None = None
            _lock_acquired: bool = False
            try:
                # Block until at least one message arrives
                msg, ctx, future = await runner.mailbox.get()
                merged_messages = [(msg, ctx, future)]

                # Merge window: wait briefly for follow-up messages
                try:
                    await asyncio.sleep(MERGE_WINDOW_MS / 1000)
                except asyncio.CancelledError:
                    raise

                # Drain any additional messages that arrived during the window
                while not runner.mailbox.empty():
                    extra = runner.mailbox.get_nowait()
                    merged_messages.append(extra)

                if len(merged_messages) > 1:
                    logger.info(
                        "TURN_MERGED: space=%s merged=%d",
                        runner.space_id, len(merged_messages),
                    )

                # Process as one turn using the first message's context
                primary_msg, primary_ctx, primary_future = merged_messages[0]
                primary_ctx.merged_count = len(merged_messages)
                # Detect self-directed turns
                if (primary_msg.context and isinstance(primary_msg.context, dict)
                        and primary_msg.context.get("execution_envelope", {}).get("source") == "self_directed"):
                    primary_ctx.is_self_directed = True

                # CROSS_SPACE_REQUESTS_V1 (Q1): acquire the per-space
                # mutation lock for the duration of the turn body.
                # Cross-space requests targeting this space will
                # await this lock with bounded timeout, ensuring
                # they queue behind the current turn rather than
                # racing it. Released in the iteration's finally
                # below.
                _space_lock = self._get_space_lock(
                    runner.instance_id, runner.space_id,
                )
                await _space_lock.acquire()
                _lock_acquired = True

                # Log merged messages to conversation log so agent sees them
                for extra_msg, extra_ctx, extra_future in merged_messages[1:]:
                    try:
                        await self.conv_logger.append(
                            runner.instance_id, runner.space_id,
                            speaker="user",
                            channel=extra_msg.platform,
                            content=extra_msg.content,
                            member_id=primary_ctx.member_id,
                        )
                    except Exception as exc:
                        logger.warning("Failed to log merged message: %s", exc)

                # Execute the full turn (assemble → reason → persist).
                # HANDLER-PIPELINE-DECOMPOSE: calls go through the
                # handler shim methods which delegate to
                # kernos.messages.phases.<name>.run(ctx). See the
                # matching comment in process() above.
                _turn_t0 = time.monotonic()
                try:
                    _t0 = time.monotonic()
                    await self._phase_assemble(primary_ctx)
                    primary_ctx.phase_timings["assemble"] = int((time.monotonic() - _t0) * 1000)

                    # Check for pending wipe confirmation (exact-phrase match)
                    _wipe_response = await self._check_wipe_confirmation(primary_ctx)
                    if _wipe_response:
                        response = _wipe_response

                    # Slash command intercepts — skip reasoning.
                    # SURFACE-DISCIPLINE-PASS: /dump is an admin/diagnostic
                    # surface that retains raw internal identifiers by
                    # design. Mark ctx.is_diagnostic_response so the
                    # outbound sanitizer skips it. /status / /help / /spaces
                    # / /wipe are user-facing — they flow through the
                    # sanitizer like any other reply.
                    _cmd = (primary_msg.content or "").strip()
                    _cmd_lower = _cmd.lower()
                    if _wipe_response:
                        pass  # Already handled above
                    elif _cmd_lower == "/dump":
                        response = await self._handle_dump(primary_ctx)
                        primary_ctx.is_diagnostic_response = True
                    elif _cmd_lower == "/status":
                        response = await self._handle_status(primary_ctx)
                    elif _cmd_lower == "/capabilities":
                        response = self._handle_capabilities()
                        primary_ctx.is_diagnostic_response = True
                    elif _cmd_lower == "/help":
                        response = self._handle_help()
                    elif _cmd_lower.startswith("/spaces"):
                        response = await self._handle_spaces(primary_ctx, _cmd)
                    elif _cmd_lower.startswith("/wipe"):
                        response = await self._handle_wipe(primary_ctx, _cmd)
                    elif _cmd_lower == "/restart":
                        # Owner-only restart — works on all platforms
                        _is_owner = False
                        if hasattr(self, '_instance_db') and self._instance_db and primary_ctx.member_id:
                            _m = await self._instance_db.get_member(primary_ctx.member_id)
                            _is_owner = _m and _m.get("role") == "owner"
                        if _is_owner:
                            response = "Restarting..."
                            # Send response before restart
                            try:
                                _platform = primary_msg.platform
                                if _platform in self._adapters:
                                    await self._adapters[_platform].send_outbound(
                                        primary_ctx.instance_id,
                                        primary_msg.conversation_id,
                                        response,
                                    )
                            except Exception:
                                pass
                            logger.info("Restart requested by member=%s", primary_ctx.member_id)
                            os.execv(sys.executable, [sys.executable] + sys.argv)
                        else:
                            response = "Only the instance owner can restart."
                    elif _cmd_lower == "/disconnect":
                        response = await self._handle_disconnect(primary_ctx)
                    elif (
                        _cmd_lower == "/model"
                        or _cmd_lower.startswith("/model ")
                    ):
                        # Exact-prefix match so future /models or
                        # /modelx commands aren't intercepted here.
                        response = await self._handle_model_command(
                            primary_ctx, _cmd,
                        )
                    else:
                        try:
                            _t0 = time.monotonic()
                            await self._phase_reason(primary_ctx)
                        except LLMChainExhausted as exc:
                            # LLM-SETUP-AND-FALLBACK contract: chain exhaustion
                            # DELIVERS a pre-rendered failure message as the
                            # reply for this turn. The agent never produces an
                            # LLM reply on this turn — the tool loop aborted
                            # before any final text was aggregated, so there's
                            # nothing to collide with. Contract: failure
                            # message iff LLMChainExhausted raised.
                            primary_ctx.phase_timings["reason"] = int((time.monotonic() - _t0) * 1000)
                            runner.provider_errors.append(str(exc)[:200])
                            if primary_ctx.trace:
                                primary_ctx.trace.record(
                                    "error", "handler", "CHAIN_EXHAUSTED",
                                    str(exc)[:300], phase="reason",
                                )
                            # Emit HANDLER_ERROR for observability (parallel to
                            # the other reasoning-error paths) before returning
                            # the pre-rendered user-facing message.
                            try:
                                await emit_event(
                                    self.events, EventType.HANDLER_ERROR,
                                    primary_ctx.instance_id, "handler",
                                    payload={
                                        "error_type": "LLMChainExhausted",
                                        "error_message": str(exc),
                                        "conversation_id": primary_ctx.conversation_id,
                                        "stage": "chain_exhausted",
                                        "chain_name": exc.chain_name,
                                        "attempts": len(exc.attempts),
                                    },
                                )
                            except Exception:
                                pass
                            response = _render_chain_exhaustion_message(exc)
                            # Zero any LLM text on the ctx — belt and
                            # suspenders; _phase_reason shouldn't have
                            # populated it if _call_chain raised.
                            primary_ctx.response_text = ""
                            if not primary_future.done():
                                primary_future.set_result(response)
                            for _, _, ef in merged_messages[1:]:
                                if not ef.done():
                                    ef.set_result("")
                            continue
                        except (ReasoningTimeoutError, ReasoningConnectionError) as exc:
                            primary_ctx.phase_timings["reason"] = int((time.monotonic() - _t0) * 1000)
                            runner.provider_errors.append(str(exc)[:200])
                            if primary_ctx.trace:
                                primary_ctx.trace.record("error", "handler", "PROVIDER_ERROR",
                                    str(exc)[:300], phase="reason")
                            _err_msg = "the API is having persistent issues — I retried 20 times. Try again shortly, or this may be a broader outage" if "after" in str(exc) else "try again in a moment"
                            response = await self._handle_reasoning_error(
                                primary_ctx, exc, _err_msg)
                            if not primary_future.done():
                                primary_future.set_result(response)
                            for _, _, ef in merged_messages[1:]:
                                if not ef.done():
                                    ef.set_result("")
                            continue
                        except ReasoningRateLimitError as exc:
                            primary_ctx.phase_timings["reason"] = int((time.monotonic() - _t0) * 1000)
                            runner.provider_errors.append(str(exc)[:200])
                            if primary_ctx.trace:
                                primary_ctx.trace.record("error", "handler", "RATE_LIMIT",
                                    str(exc)[:300], phase="reason")
                            # Extract provider name from the exception text so
                            # the user immediately knows it's a rate limit on
                            # a specific provider, not a generic "overloaded"
                            # bug. Provider strings in the exception come from
                            # the underlying provider code (e.g.,
                            # "Codex rate limited (429): ..." from
                            # codex_provider.py, "Error code: 429 - ..." from
                            # the Anthropic SDK, etc.).
                            _exc_lower = str(exc).lower()
                            if "codex" in _exc_lower:
                                _provider_name = "Codex"
                            elif "anthropic" in _exc_lower or "claude" in _exc_lower:
                                _provider_name = "Anthropic"
                            elif "ollama" in _exc_lower:
                                _provider_name = "Ollama"
                            else:
                                _provider_name = "my reasoning provider"
                            _rl_msg = (
                                f"{_provider_name} is rate-limited right now — "
                                "usually clears in a minute or two. Try again then."
                            )
                            response = await self._handle_reasoning_error(
                                primary_ctx, exc, _rl_msg)
                            if not primary_future.done():
                                primary_future.set_result(response)
                            for _, _, ef in merged_messages[1:]:
                                if not ef.done():
                                    ef.set_result("")
                            continue
                        except ReasoningProviderError as exc:
                            primary_ctx.phase_timings["reason"] = int((time.monotonic() - _t0) * 1000)
                            runner.provider_errors.append(str(exc)[:200])
                            if primary_ctx.trace:
                                primary_ctx.trace.record("error", "handler", "PROVIDER_ERROR",
                                    str(exc)[:300], phase="reason")
                            _err_msg = "the API is having persistent issues — I retried 20 times. Try again shortly, or this may be a broader outage" if "after" in str(exc) else "try again in a moment"
                            response = await self._handle_reasoning_error(
                                primary_ctx, exc, _err_msg)
                            if not primary_future.done():
                                primary_future.set_result(response)
                            for _, _, ef in merged_messages[1:]:
                                if not ef.done():
                                    ef.set_result("")
                            continue
                        except Exception as exc:
                            primary_ctx.phase_timings["reason"] = int((time.monotonic() - _t0) * 1000)
                            response = await self._handle_reasoning_error(
                                primary_ctx, exc, "something unexpected happened")
                            if not primary_future.done():
                                primary_future.set_result(response)
                            for _, _, ef in merged_messages[1:]:
                                if not ef.done():
                                    ef.set_result("")
                            continue
                        primary_ctx.phase_timings["reason"] = int((time.monotonic() - _t0) * 1000)

                        _t0 = time.monotonic()
                        await self._phase_consequence(primary_ctx)
                        primary_ctx.phase_timings["consequence"] = int((time.monotonic() - _t0) * 1000)

                        _t0 = time.monotonic()
                        await self._phase_persist(primary_ctx)
                        primary_ctx.phase_timings["persist"] = int((time.monotonic() - _t0) * 1000)

                        response = primary_ctx.response_text or ""

                        # Friction observer — async, non-blocking
                        primary_ctx.tool_calls_trace = self.reasoning.drain_tool_trace()
                        asyncio.ensure_future(self._run_friction_observer(
                            primary_ctx, provider_errors=runner.provider_errors))

                        # Tier 3: Promote successfully used tools into local affordance set
                        if primary_ctx.active_space and primary_ctx.tool_calls_trace:
                            asyncio.ensure_future(self._promote_used_tools(
                                primary_ctx.instance_id, primary_ctx.active_space_id,
                                primary_ctx.active_space, primary_ctx.tool_calls_trace))
                except Exception as exc:
                    logger.error(
                        "TURN_ERROR: space=%s error=%s",
                        runner.space_id, exc, exc_info=True,
                    )
                    response = "Something went wrong. Try again in a moment."

                # Log phase timings
                _total_ms = int((time.monotonic() - _turn_t0) * 1000)
                _pt = primary_ctx.phase_timings
                for _phase, _dur in _pt.items():
                    logger.info("PHASE_TIMING: phase=%s duration_ms=%d", _phase, _dur)
                logger.info(
                    "TURN_TIMING: total_ms=%d provision=%d route=%d assemble=%d "
                    "reason=%d consequence=%d persist=%d",
                    _total_ms,
                    _pt.get("provision", 0), _pt.get("route", 0),
                    _pt.get("assemble", 0), _pt.get("reason", 0),
                    _pt.get("consequence", 0), _pt.get("persist", 0),
                )
                self._record_phase_timings(_pt, _total_ms)

                # Record timing to trace + flush
                if primary_ctx.trace:
                    primary_ctx.trace.record(
                        "info", "handler", "TURN_TIMING",
                        f"total={_total_ms}ms phases={json.dumps(_pt)}",
                        duration_ms=_total_ms,
                    )
                    try:
                        await self._runtime_trace.append_turn(
                            runner.instance_id, primary_ctx.trace.events)
                    except Exception as _te:
                        logger.debug("TRACE_FLUSH: failed: %s", _te)

                # SURFACE-DISCIPLINE-PASS D1 — user-facing vs diagnostic
                # surfaces use explicitly different finalizers. The turn
                # loop routes to one or the other based on what the
                # command handler set — no shared middleware deciding
                # class at runtime.
                if primary_ctx.is_diagnostic_response:
                    response = self._finalize_diagnostic_response(response)
                else:
                    response = self._finalize_user_facing_response(
                        response, primary_ctx, primary_msg,
                    )

                # Resolve all futures — primary gets the response,
                # merged messages get empty (adapter sends nothing)
                if not primary_future.done():
                    primary_future.set_result(response)
                for _, _, extra_future in merged_messages[1:]:
                    if not extra_future.done():
                        extra_future.set_result("")

            except asyncio.CancelledError:
                # Resolve any pending futures before exiting
                for item in merged_messages:
                    _, _, f = item
                    if not f.done():
                        f.set_result("")
                break
            except Exception as exc:
                logger.error(
                    "RUNNER_ERROR: space=%s error=%s",
                    runner.space_id, exc, exc_info=True,
                )
                # Resolve any pending futures so callers don't hang
                for item in merged_messages:
                    _, _, f = item
                    if not f.done():
                        f.set_result("Something went wrong. Try again.")
            finally:
                # CROSS_SPACE_REQUESTS_V1 (Q1): release the per-space
                # mutation lock on every iteration exit (success,
                # exception, cancellation). Cross-space requests
                # waiting on this lock proceed once released.
                if _lock_acquired and _space_lock is not None:
                    try:
                        _space_lock.release()
                    except RuntimeError:
                        # asyncio.Lock raises if called when not
                        # acquired by this task — defensive only.
                        pass

    async def shutdown_runners(self) -> None:
        """Cancel all space runners. Call on application shutdown."""
        for key, runner in list(self._runners.items()):
            if runner._task and not runner._task.done():
                runner._task.cancel()
                try:
                    await runner._task
                except asyncio.CancelledError:
                    pass
        self._runners.clear()

    # -----------------------------------------------------------------------
    # Six-Phase Pipeline (SPEC-HANDLER-DECOMPOSE)
    # -----------------------------------------------------------------------

    async def process(self, message: NormalizedMessage) -> str:
        """Process a NormalizedMessage and return a response string.

        Lightweight phases (provision, route) run immediately. The heavy
        phases (assemble → reason → consequence → persist) are submitted
        to a per-(tenant, space) runner that serializes turns.
        """
        from kernos.kernel.runtime_trace import TurnEventCollector, generate_turn_id
        ctx = TurnContext(message=message)
        ctx.trace = TurnEventCollector(generate_turn_id())
        # HANDLER-PIPELINE-DECOMPOSE: phase modules reach services
        # (state, reasoning, instance_db, etc.) through ctx.handler.
        ctx.handler = self

        # Early return paths (secure input)
        early = await self._check_early_return(ctx)
        if early is not None:
            return early

        # Lightweight phases — safe to run concurrently.
        # HANDLER-PIPELINE-DECOMPOSE: the _phase_* methods are 10-line
        # shims that delegate to kernos.messages.phases.<name>.run(ctx).
        # We call through the shims (not through the phase modules
        # directly) so existing tests that monkey-patch handler._phase_*
        # for observability keep working. A future batch can migrate
        # tests to monkey-patch phases.<name>.run instead, which unlocks
        # the final shim-size shrink.
        _t0 = time.monotonic()
        await self._phase_provision(ctx)
        ctx.phase_timings["provision"] = int((time.monotonic() - _t0) * 1000)

        _t0 = time.monotonic()
        await self._phase_route(ctx)
        ctx.phase_timings["route"] = int((time.monotonic() - _t0) * 1000)

        # Submit to the space runner's mailbox
        runner = self._get_runner(ctx.instance_id, ctx.active_space_id)

        response_future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        await runner.mailbox.put((message, ctx, response_future))

        logger.info(
            "TURN_SUBMITTED: instance=%s space=%s queue_depth=%d",
            ctx.instance_id, ctx.active_space_id,
            runner.mailbox.qsize(),
        )

        # Await the response — runner will resolve the future
        return await response_future

    async def _check_early_return(self, ctx: TurnContext) -> str | None:
        """Secure input intercepts — return early without LLM."""
        message = ctx.message
        instance_id = derive_instance_id(message)
        conversation_id = message.conversation_id
        ctx.instance_id = instance_id
        ctx.conversation_id = conversation_id

        # Housekeeping
        self.reasoning.reset_conflict_raised()
        self.reasoning.cleanup_expired_authorizations(instance_id)
        self._error_buffer.set_tenant(instance_id)
        # Resolve member via instance.db (multi-member aware)
        if hasattr(self, '_instance_db') and self._instance_db:
            _member_id, _static = await self._resolve_incoming(
                message.platform, message.sender, message.content or "")
            if _static is not None:
                # Unknown sender or invite code — send static response, skip pipeline
                return _static
            if _member_id:
                message.member_id = _member_id
            else:
                message.member_id = self._resolve_member(instance_id, message.platform, message.sender)
        else:
            message.member_id = self._resolve_member(instance_id, message.platform, message.sender)
        # Propagate resolved member_id to TurnContext
        ctx.member_id = message.member_id
        if message.platform == "discord":
            self._channel_registry.update_target("discord", message.conversation_id)
        if message.platform == "telegram":
            self._channel_registry.update_target("telegram", message.conversation_id)

        if instance_id in self._secure_input_state:
            state = self._secure_input_state[instance_id]
            if datetime.now(timezone.utc) > state.expires_at:
                del self._secure_input_state[instance_id]
                return (
                    "The secure input session timed out after 10 minutes. "
                    "Your message was processed normally (not stored as a credential). "
                    "Say 'secure api' again when you're ready to send your key."
                )
            credential_value = message.content.strip()
            del self._secure_input_state[instance_id]

            if state.mode == "platform":
                # Platform adapter token — write to .env and hot-start
                self._write_env_var(state.env_var, credential_value)
                success = await self._start_platform_adapter(state.platform)
                if success:
                    return (
                        f"Token stored securely. {state.platform.title()} is now live! "
                        f"You can generate invite codes for {state.platform} right away."
                    )
                return (
                    f"Token saved to .env as {state.env_var}. "
                    f"I couldn't hot-start the adapter — a restart may be needed. "
                    f"If the token is correct, restart Kernos and {state.platform.title()} will come online."
                )
            else:
                # MCP capability credential — store in secrets dir
                cap_name = state.capability_name
                await self._store_credential(instance_id, cap_name, credential_value)
                success = await self._connect_after_credential(instance_id, cap_name)
                if success:
                    return f"Key stored securely. {cap_name} is now connected! You can start using it right away."
                return f"Key stored, but I couldn't connect to {cap_name}. The key might be invalid, or the service might be down."

        if message.content.strip().lower() == _SECURE_API_TRIGGER:
            # Try MCP capability first, then platform adapter
            cap_name = await self._infer_pending_capability(instance_id, conversation_id)
            if cap_name:
                self._secure_input_state[instance_id] = SecureInputState(
                    capability_name=cap_name,
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=_SECURE_INPUT_TIMEOUT_MINUTES),
                )
                return (
                    f"Secure input mode active for {cap_name}. "
                    f"Your next message will NOT be seen by any agent — "
                    f"it will go directly to encrypted storage as your {cap_name} API key. Send your key now."
                )

            platform = await self._infer_pending_platform(instance_id, conversation_id)
            if platform:
                cred_info = _PLATFORM_CREDENTIALS[platform]
                self._secure_input_state[instance_id] = SecureInputState(
                    capability_name=platform,
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=_SECURE_INPUT_TIMEOUT_MINUTES),
                    mode="platform",
                    platform=platform,
                    env_var=cred_info["primary_env"],
                )
                return (
                    f"Secure input mode active for {platform.title()}. "
                    f"Your next message will NOT be seen by any agent — "
                    f"it will be stored directly as your {cred_info['label']}. Paste it now."
                )

            return "I'm not sure which tool or platform you're setting up. Start the connection process first, then say 'secure api' again."
        return None

    async def _handle_dump(self, ctx: TurnContext) -> str:
        """Write the fully assembled context to a diagnostic file, skip reasoning.

        Sections written (in order):
          1. SYSTEM PROMPT — static + dynamic substrate sent to the model
          2. MESSAGES — the input message list (raw, with role tags)
          3. TOOLS — tool schemas the model sees
          4. RECENT CONVERSATION — human-readable tail of the persisted
             conv_logger output for this (instance, space, member). Lets
             operators see the user-facing exchange without the noise of
             [SYSTEM] markers / fallback annotations / role boundaries.
          5. RECENT LOG — tail of the in-process log ring buffer. Console
             evidence (CODEX_REQUEST tools=N, TOOL_SURFACING, retry
             warnings) lands in the same artifact as the substrate so
             "what tools did the bot actually surface in the failing
             turn?" is answerable without grepping a terminal scrollback.
          6. SUMMARY — token estimates + cached/fresh split.
        """
        data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
        # Second-precision timestamp — duplicate deliveries overwrite the same file
        ts = utc_now()[:19].replace(":", "-")
        dump_path = Path(data_dir) / "diagnostics" / f"context_{ts}.txt"
        dump_path.parent.mkdir(parents=True, exist_ok=True)

        with open(dump_path, "w") as f:
            f.write("=== SYSTEM PROMPT ===\n\n")
            f.write(ctx.system_prompt)
            f.write("\n\n=== MESSAGES ===\n\n")
            for msg in ctx.messages:
                role = msg.get("role", "?")
                content = msg.get("content", "")
                if isinstance(content, str):
                    f.write(f"[{role}]\n{content}\n\n")
                elif isinstance(content, list):
                    f.write(f"[{role}] <{len(content)} content blocks>\n\n")
                else:
                    f.write(f"[{role}] <non-text content>\n\n")
            f.write("\n=== TOOLS ===\n\n")
            for tool in ctx.tools:
                f.write(f"{json.dumps(tool, indent=2)}\n\n")

            # ---- RECENT CONVERSATION ----------------------------------
            # Tail the persisted conversation log for this space/member.
            # Best-effort: failures here never break the dump.
            f.write("\n=== RECENT CONVERSATION ===\n")
            f.write("(tail of conv_logger output for this space + member)\n\n")
            try:
                _log_text, _log_num = await self.conv_logger.read_current_log_text(
                    ctx.instance_id, ctx.active_space_id, member_id=ctx.member_id,
                )
                # Last ~3000 chars typically covers ~5-8 conversational turns.
                _tail_chars = int(os.getenv("KERNOS_DUMP_CONVERSATION_TAIL_CHARS", "3000"))
                if len(_log_text) > _tail_chars:
                    f.write(f"... ({len(_log_text) - _tail_chars} earlier chars elided)\n\n")
                    _log_text = _log_text[-_tail_chars:]
                f.write(_log_text)
                if not _log_text.endswith("\n"):
                    f.write("\n")
            except FileNotFoundError:
                f.write("(no conversation log found for this space/member yet)\n")
            except Exception as exc:
                f.write(f"(conversation log read failed: {exc})\n")

            # ---- RECENT LOG -------------------------------------------
            # In-process log ring buffer (kernel/log_buffer.py). Captures
            # the same lines the operator sees on the bot's stdout.
            f.write("\n=== RECENT LOG ===\n")
            f.write("(tail of in-process log ring buffer — same lines that scroll past on stdout)\n\n")
            try:
                from kernos.kernel.log_buffer import get_recent_log_lines
                _log_tail = int(os.getenv("KERNOS_DUMP_LOG_TAIL_LINES", "150"))
                _lines = get_recent_log_lines(last_n=_log_tail)
                if _lines:
                    for line in _lines:
                        f.write(line)
                        f.write("\n")
                else:
                    f.write("(log ring buffer not installed; see "
                            "kernos.kernel.log_buffer.install_log_ring_buffer)\n")
            except Exception as exc:
                f.write(f"(log ring buffer read failed: {exc})\n")

            # ---- LAST OUTGOING PAYLOAD --------------------------------
            # Receipts for what the LLM actually received on the most
            # recent codex call. Requires KERNOS_CODEX_LAST_PAYLOAD=1
            # env (off by default) — codex_provider writes the body to
            # this file (replaced each call) when the env is set.
            # Settles "did tool X reach the model in turn N" questions.
            f.write("\n=== LAST OUTGOING PAYLOAD ===\n")
            f.write("(exact JSON body shipped to the LLM on the most recent call — receipts)\n")
            f.write("(enable via KERNOS_CODEX_LAST_PAYLOAD=1 in .env)\n\n")
            try:
                _data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
                _payload_path = os.getenv(
                    "KERNOS_CODEX_LAST_PAYLOAD_PATH",
                    os.path.join(_data_dir, "diagnostics", "codex_last_payload.json"),
                )
                if Path(_payload_path).exists():
                    _payload_text = Path(_payload_path).read_text(encoding="utf-8")
                    f.write(f"(source: {_payload_path}, "
                            f"{len(_payload_text)} chars)\n\n")
                    f.write(_payload_text)
                    if not _payload_text.endswith("\n"):
                        f.write("\n")
                else:
                    f.write(f"(no last-payload file at {_payload_path}; "
                            f"set KERNOS_CODEX_LAST_PAYLOAD=1 to enable)\n")
            except Exception as exc:
                f.write(f"(last-payload read failed: {exc})\n")

            f.write("\n=== SUMMARY ===\n")
            _sys_chars = len(ctx.system_prompt)
            msg_chars = sum(len(str(m.get('content', ''))) for m in ctx.messages)
            tool_chars = sum(len(json.dumps(t)) for t in ctx.tools)
            _char_est = (_sys_chars + msg_chars + tool_chars) // 4
            _real_baseline = self.reasoning.get_last_real_input_tokens(ctx.instance_id)
            _static_chars = len(ctx.system_prompt_static)
            _dynamic_chars = len(ctx.system_prompt_dynamic)
            f.write(f"System prompt: ~{_sys_chars // 4} tokens ({_sys_chars} chars)\n")
            f.write(f"  Static (cached): ~{_static_chars // 4} tokens ({_static_chars} chars)\n")
            f.write(f"  Dynamic (fresh):  ~{_dynamic_chars // 4} tokens ({_dynamic_chars} chars)\n")
            f.write(f"Messages: {len(ctx.messages)} entries, ~{msg_chars // 4} tokens\n")
            f.write(f"Tools: {len(ctx.tools)} schemas, ~{tool_chars // 4} tokens\n")
            f.write(f"Char-based estimate: ~{_char_est} tokens\n")
            if _real_baseline > 0:
                f.write(f"Last real input_tokens (from API): {_real_baseline}\n")

        logger.info("DUMP: context written to %s", dump_path)
        return f"Context dumped to {dump_path}"

    @staticmethod
    def _handle_capabilities() -> str:
        """Render the capability matrix from the in-code source of
        truth. CLEANUP-BATCH-V1 item 9: inspect-only — reads the
        static ``CapabilitySpec`` list authored in
        ``kernos/kernel/capabilities.py``. Same source the README
        matrix renders from, so the two cannot drift."""
        from kernos.kernel.capabilities import render_status_text
        return (
            "**Kernos capability matrix**\n\n"
            f"{render_status_text()}\n\n"
            "Source: `kernos/kernel/capabilities.py`. "
            "README matrix is regenerated from the same list."
        )

    @staticmethod
    def _handle_help() -> str:
        """Return a summary of available slash commands."""
        return (
            "**Available Commands**\n\n"
            "**/help** — Show this message.\n\n"
            "**/dump** — Write the fully assembled context (system prompt, "
            "messages, tools) to a diagnostic file. Useful for inspecting "
            "exactly what the agent sees on a given turn. Skips reasoning.\n\n"
            "**/status** — Write the operator state view to a diagnostic "
            "file. Shows active preferences, triggers, covenants, key facts, "
            "connected capabilities, legacy artifacts, stale reconciliation, "
            "and degraded services. Skips reasoning.\n\n"
            "**/capabilities** — Show the capability matrix (Live / "
            "Partial / Experimental / Planned) sourced from the canonical "
            "in-code list. Inspect-only.\n\n"
            "**/spaces** — List all context spaces with status.\n"
            '**/spaces create "Name" "Description"** — Manually create a '
            "new context space for testing multi-space routing.\n\n"
            "These commands bypass the reasoning engine and are not stored "
            "in conversation history.\n\n"
            "**/wipe me** — Delete your member profile, conversations, knowledge, "
            "and spaces. Other members unaffected. Requires confirmation.\n\n"
            "**/wipe all** — Factory reset the entire instance. All members, all data. "
            "Owner only. Requires confirmation.\n\n"
            "**/disconnect** — Disconnect this platform from your account. "
            "Your other connected platforms still work.\n\n"
            "**/restart** — Restart Kernos. Owner only."
        )

    # --- Wipe commands ---

    # Pending wipe confirmations: {instance_id:member_id → wipe_type}
    _pending_wipe: dict[str, str] = {}

    async def _handle_disconnect(self, ctx: TurnContext) -> str:
        """Disconnect the current platform channel from the member's account."""
        if not ctx.member_id or not ctx.message:
            return "Cannot determine your identity on this platform."
        if not hasattr(self, '_instance_db') or not self._instance_db:
            return "Member management is not available."

        platform = ctx.message.platform
        channel_id = ctx.message.sender

        # Check how many channels this member has
        member = await self._instance_db.get_member(ctx.member_id)
        if not member:
            return "Member not found."

        # Count channels
        channels = []
        if self._instance_db._conn:
            async with self._instance_db._conn.execute(
                "SELECT platform, channel_id FROM member_channels WHERE member_id=?",
                (ctx.member_id,),
            ) as cur:
                channels = await cur.fetchall()

        if len(channels) <= 1:
            return (
                "This is your only connected platform. Disconnecting it would leave "
                "your account unreachable. Use **/wipe me** if you want to delete "
                "your account entirely."
            )

        # Remove this channel
        await self._instance_db._conn.execute(
            "DELETE FROM member_channels WHERE member_id=? AND platform=? AND channel_id=?",
            (ctx.member_id, platform, channel_id),
        )
        await self._instance_db._conn.commit()

        # Also clear sender blocks for this channel
        await self._instance_db.clear_sender_failures(platform, channel_id)

        logger.info("DISCONNECT: member=%s platform=%s channel=%s", ctx.member_id, platform, channel_id)
        return (
            f"Disconnected {platform} from your account. Your other channels "
            f"still work. Messages from this {platform} account will no longer "
            f"be recognized."
        )

    async def _handle_wipe(self, ctx: TurnContext, raw_cmd: str) -> str:
        """Handle /wipe me and /wipe all.

        SURFACE-DISCIPLINE-PASS D5: confirmation gate is now `/wipe <scope> yes`
        on the same command (simpler than the previous exact-phrase reply).
        Unconfirmed `/wipe me` or `/wipe all` still arms the legacy exact-
        phrase path so cautious operators can keep using it. `/wipe <scope>
        no` or any other input cancels.
        """
        parts = raw_cmd.strip().split()
        parts_lower = [p.lower() for p in parts]
        sub = parts_lower[1] if len(parts_lower) > 1 else ""
        suffix = parts_lower[2] if len(parts_lower) > 2 else ""

        if not sub:
            return (
                "Usage:\n"
                "**/wipe me** — Delete your data only. Prompts for confirmation.\n"
                "**/wipe me yes** — Same, but confirms in one step.\n"
                "**/wipe all** — Factory reset the entire instance (owner only).\n"
                "**/wipe all yes** — Owner factory reset, one-step confirmation."
            )

        if sub == "me":
            if not ctx.member_id:
                return "Cannot determine your member identity. Are you a registered member?"
            if suffix == "yes":
                return await self._execute_wipe_member(ctx)
            if suffix == "no":
                return "Wipe cancelled."
            # Unconfirmed — arm the legacy exact-phrase fallback and prompt.
            self._pending_wipe[f"{ctx.instance_id}:{ctx.member_id}"] = "me"
            return (
                "This will permanently delete your account — profile, conversations, "
                "knowledge, spaces, covenants, and all platform connections. "
                "You'll need a new invite code to rejoin. Other members are not affected.\n\n"
                "To confirm, reply with **/wipe me yes**. Anything else cancels."
            )

        if sub == "all":
            if hasattr(self, '_instance_db') and self._instance_db and ctx.member_id:
                member = await self._instance_db.get_member(ctx.member_id)
                if not member or member.get("role") != "owner":
                    return "Only the instance owner can wipe all data."
            if suffix == "yes":
                return await self._execute_wipe_all(ctx)
            if suffix == "no":
                return "Wipe cancelled."
            self._pending_wipe[f"{ctx.instance_id}:{ctx.member_id}"] = "all"
            return (
                "This will permanently delete ALL data for this Kernos instance — "
                "every member, every conversation, every space, everything.\n\n"
                "To confirm, reply with **/wipe all yes**. Anything else cancels."
            )

        return (
            "Usage:\n"
            "**/wipe me [yes]** — Delete your data only.\n"
            "**/wipe all [yes]** — Factory reset the entire instance (owner only)."
        )

    async def _check_wipe_confirmation(self, ctx: TurnContext) -> str | None:
        """Check if an incoming message is a wipe confirmation phrase. Returns response or None."""
        key = f"{ctx.instance_id}:{ctx.member_id}"
        if key not in self._pending_wipe:
            return None

        text = (ctx.message.content or "").strip()
        wipe_type = self._pending_wipe[key]

        if wipe_type == "me":
            name = (ctx.member_profile or {}).get("display_name", "") or "my data"
            expected = "Delete my data!"
            if text == expected:
                del self._pending_wipe[key]
                return await self._execute_wipe_member(ctx)
            else:
                # Wrong phrase or changed mind — cancel
                del self._pending_wipe[key]
                return "Wipe cancelled."

        elif wipe_type == "all":
            if text == "Delete it all!":
                del self._pending_wipe[key]
                return await self._execute_wipe_all(ctx)
            else:
                del self._pending_wipe[key]
                return "Wipe cancelled."

        del self._pending_wipe[key]
        return None

    async def _execute_wipe_member(self, ctx: TurnContext) -> str:
        """Delete a single member's data: profile, conversations, knowledge, spaces, covenants."""
        instance_id = ctx.instance_id
        member_id = ctx.member_id
        name = (ctx.member_profile or {}).get("display_name", member_id)

        # Delete member's knowledge entries
        try:
            all_ke = await self.state.query_knowledge(
                instance_id, active_only=False, limit=10000, member_id=member_id)
            for ke in all_ke:
                if getattr(ke, "owner_member_id", "") == member_id:
                    await self.state.update_knowledge(instance_id, ke.id, {"active": False})
            logger.info("WIPE_MEMBER: deactivated %d knowledge entries for %s", len(all_ke), member_id)
        except Exception as exc:
            logger.warning("WIPE_MEMBER: knowledge cleanup failed: %s", exc)

        # Delete member's spaces
        try:
            spaces = await self.state.list_context_spaces(instance_id)
            for s in spaces:
                if s.member_id == member_id:
                    await self.state.update_context_space(instance_id, s.id, {"status": "archived"})
            logger.info("WIPE_MEMBER: archived member spaces for %s", member_id)
        except Exception as exc:
            logger.warning("WIPE_MEMBER: space cleanup failed: %s", exc)

        # Deactivate member record, remove channels, reset profile
        if hasattr(self, '_instance_db') and self._instance_db:
            try:
                # Remove all channel mappings — member becomes unknown on all platforms
                await self._instance_db._conn.execute(
                    "DELETE FROM member_channels WHERE member_id=?", (member_id,),
                )
                # Deactivate the member record
                await self._instance_db._conn.execute(
                    "UPDATE members SET status='wiped' WHERE member_id=?", (member_id,),
                )
                # Reset the profile
                await self._instance_db.upsert_member_profile(member_id, {
                    "display_name": "", "timezone": "", "communication_style": "",
                    "interaction_count": 0, "hatched": False, "hatched_at": "",
                    "bootstrap_graduated": False, "bootstrap_graduated_at": "",
                    "agent_name": "", "emoji": "", "personality_notes": "",
                })
                await self._instance_db._conn.commit()
                logger.info("WIPE_MEMBER: deactivated member + removed channels for %s", member_id)
            except Exception as exc:
                logger.warning("WIPE_MEMBER: member cleanup failed: %s", exc)

        return f"{name}'s data has been wiped. This channel is no longer connected — a new invite code is needed to rejoin."

    async def _execute_wipe_all(self, ctx: TurnContext) -> str:
        """Factory reset — delete everything. Triggers process restart."""
        import shutil
        data_dir = Path(os.getenv("KERNOS_DATA_DIR", "./data"))
        logger.warning("WIPE_ALL: initiated by member=%s instance=%s", ctx.member_id, ctx.instance_id)

        if data_dir.exists():
            shutil.rmtree(data_dir)
            logger.info("WIPE_ALL: removed %s", data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        # Restart the process
        os.execv(sys.executable, [sys.executable] + sys.argv)
        return "Wiping..."  # Never reached — execv replaces the process

    async def _handle_manage_members(
        self, instance_id: str, tool_input: dict,
        requesting_member_id: str = "",
    ) -> str:
        """Handle manage_members tool — invite, list, connect_platform, remove."""
        if not hasattr(self, '_instance_db') or not self._instance_db:
            return "Member management is not available (no instance database)."

        action = tool_input.get("action", "")
        platform = tool_input.get("platform", "")

        if action in ("invite", "connect_platform") and not platform:
            supported = self._instance_db.get_supported_platforms()
            return f"Error: platform is required. Specify one of: {', '.join(supported)}"

        # Check if platform is set up (has a registered adapter)
        if platform and action in ("invite", "connect_platform"):
            if platform not in self._adapters:
                setup = self._instance_db.get_setup_instructions(platform)
                cred_info = _PLATFORM_CREDENTIALS.get(platform, {})
                if cred_info.get("supports_paste"):
                    label = cred_info["label"]
                    return (
                        f"{platform.title()} is not connected to this Kernos instance yet.\n\n"
                        f"{setup}\n\n"
                        f"The user can provide the {label} securely — present these options:\n"
                        f"1. **Paste {label} here** — reply with `secure api` and the next message "
                        f"will be intercepted securely (never seen by any agent)\n"
                        f"2. **Manually add to .env** — add {cred_info['primary_env']} to the "
                        f"server's .env file and restart\n"
                        f"3. **Cancel** — skip for now"
                    )
                return (
                    f"{platform.title()} is not connected to this Kernos instance yet.\n\n"
                    f"{setup}"
                )

        if action == "invite":
            display_name = tool_input.get("display_name", "")
            expires = tool_input.get("expires_hours", 72)
            members = await self._instance_db.list_members()
            owner = next((m for m in members if m.get("role") == "owner"), None)
            created_by = owner["member_id"] if owner else "unknown"
            result = await self._instance_db.create_invite_code(
                created_by=created_by, platform=platform,
                display_name=display_name, expires_hours=expires)
            if isinstance(result, dict) and "error" in result:
                return f"Error: {result['error']}"
            code = result["code"]
            instructions = result["instructions"]
            name_part = f" for {display_name}" if display_name else ""
            return (
                f"Invite code{name_part}: **{code}** ({platform}, expires in {expires} hours)\n\n"
                f"Instructions to give them:\n{instructions}"
            )

        elif action == "connect_platform":
            # Auto-fill: connect_platform defaults to the requesting member
            member_id = tool_input.get("member_id", "") or requesting_member_id
            if not member_id:
                return "Error: could not determine which member to connect. Provide member_id or ensure the request comes from a known member."
            # Validate member_id exists
            members = await self._instance_db.list_members()
            target = next((m for m in members if m["member_id"] == member_id), None)
            if not target:
                # Try resolving by display_name or role
                target = next((m for m in members if m.get("display_name", "").lower() == member_id.lower() or m.get("role", "") == member_id.lower()), None)
                if target:
                    member_id = target["member_id"]
                else:
                    member_names = [f"{m['member_id']} ({m.get('display_name', '')})" for m in members]
                    return f"Error: member '{member_id}' not found. Known members: {', '.join(member_names)}"
            expires = tool_input.get("expires_hours", 72)
            owner = next((m for m in members if m.get("role") == "owner"), None)
            created_by = owner["member_id"] if owner else "unknown"
            result = await self._instance_db.create_invite_code(
                created_by=created_by, platform=platform,
                for_member=member_id, expires_hours=expires)
            if isinstance(result, dict) and "error" in result:
                return f"Error: {result['error']}"
            code = result["code"]
            instructions = result["instructions"]
            return (
                f"Connection code: **{code}** ({platform}, expires in {expires} hours)\n\n"
                f"Instructions:\n{instructions}"
            )

        elif action == "list":
            members = await self._instance_db.list_members()
            if not members:
                return "No members registered."
            lines = ["**Instance Members:**"]
            for m in members:
                channels = ", ".join(f"{c['platform']}:{c['channel_id'][:12]}" for c in m.get("channels", []))
                lines.append(f"- **{m.get('display_name') or m['member_id']}** ({m.get('role', 'member')}) — {channels or 'no channels'}")
            return "\n".join(lines)

        elif action == "remove":
            member_id = tool_input.get("member_id", "")
            if not member_id:
                return "Error: member_id is required for remove."
            success = await self._instance_db.deactivate_member(member_id)
            if success:
                return f"Member {member_id} has been deactivated."
            return f"Member {member_id} not found."

        elif action == "declare_relationship":
            target_id = tool_input.get("member_id", "")
            permission = tool_input.get("permission", "")
            if not target_id:
                return "Error: member_id of the other person is required for declare_relationship."
            if not permission:
                return (
                    "Error: permission is required. One of: "
                    "full-access, no-access, by-permission."
                )
            if permission not in {"full-access", "no-access", "by-permission"}:
                return (
                    f"Error: invalid permission {permission!r}. Must be one of: "
                    "full-access, no-access, by-permission."
                )
            # Resolve target by name if needed
            members = await self._instance_db.list_members()
            target = next((m for m in members if m["member_id"] == target_id), None)
            if not target:
                target = next(
                    (m for m in members
                     if m.get("display_name", "").lower() == target_id.lower()),
                    None,
                )
                if target:
                    target_id = target["member_id"]
                else:
                    member_names = [
                        f"{m.get('display_name', '')} ({m['member_id']})"
                        for m in members if m["member_id"] != requesting_member_id
                    ]
                    return (
                        f"Error: member '{target_id}' not found. "
                        f"Known members: {', '.join(member_names)}"
                    )
            result = await self._instance_db.declare_relationship(
                requesting_member_id, target_id, permission,
            )
            if "error" in result:
                return f"Error: {result['error']}"
            other = target or await self._instance_db.get_member(target_id)
            other_name = (
                (other or {}).get("display_name", target_id) or target_id
            )
            return (
                f"Declared toward {other_name}: {permission}. "
                "They keep their own side of the permission; "
                "yours does not affect theirs."
            )

        elif action == "list_relationships":
            rels = await self._instance_db.list_relationships(requesting_member_id)
            if not rels:
                return (
                    "No relationships declared. The default toward everyone is "
                    "by-permission (conservative). Use declare_relationship to "
                    "change your side toward a specific member."
                )
            lines: list[str] = []
            for r in rels:
                name = r.get("other_display_name", "")
                if r.get("declarer_member_id") == requesting_member_id:
                    lines.append(
                        f"- **{name}** — you declared: {r.get('permission', 'by-permission')}"
                    )
                else:
                    lines.append(
                        f"- **{name}** — they declared toward you: {r.get('permission', 'by-permission')}"
                    )
            return "Your relationships:\n" + "\n".join(lines)

        return f"Unknown action: {action}. Use invite, connect_platform, list, remove, declare_relationship, or list_relationships."

    async def _handle_send_relational_message(
        self, instance_id: str, tool_input: dict,
        origin_member_id: str = "",
    ) -> str:
        """RELATIONAL-MESSAGING: agent tool to send a purposeful message
        to another member's agent. Dispatcher enforces permission matrix,
        creates the envelope, and routes by urgency.
        """
        dispatcher = self._get_relational_dispatcher()
        if dispatcher is None:
            return "Relational messaging is not available (instance_db missing)."
        if not origin_member_id:
            return "Error: origin member is unknown for this turn."
        addressee = (tool_input.get("addressee") or "").strip()
        intent = (tool_input.get("intent") or "").strip()
        content = (tool_input.get("content") or "").strip()
        urgency = (tool_input.get("urgency") or "normal").strip()
        target_space_hint = (tool_input.get("target_space_hint") or "").strip()
        conversation_id = (tool_input.get("conversation_id") or "").strip()
        reply_to_id = (tool_input.get("reply_to_id") or "").strip()

        # Resolve the origin agent's self-name for the envelope from the
        # member profile. Falls back to display name, then member_id.
        origin_agent_identity = ""
        if self._instance_db:
            try:
                profile = await self._instance_db.get_member_profile(origin_member_id)
                if profile:
                    origin_agent_identity = (
                        profile.get("agent_name") or profile.get("display_name") or ""
                    )
            except Exception:
                pass
        if not origin_agent_identity:
            origin_agent_identity = origin_member_id

        result = await dispatcher.send(
            instance_id=instance_id,
            origin_member_id=origin_member_id,
            origin_agent_identity=origin_agent_identity,
            addressee=addressee, intent=intent, content=content,
            urgency=urgency, target_space_hint=target_space_hint,
            conversation_id=conversation_id, reply_to_id=reply_to_id,
        )
        if not result.ok:
            return f"Could not send: {result.error}"
        return (
            f"Sent (id={result.message_id}, conversation={result.conversation_id}, "
            f"state={result.state}). Their agent will see it "
            f"{'now' if urgency == 'time_sensitive' else 'on their next turn'}."
        )

    async def _handle_resolve_relational_message(
        self, instance_id: str, tool_input: dict,
        requesting_member_id: str = "",
    ) -> str:
        """RELATIONAL-MESSAGING: agent tool to mark a message processed.

        Default (auto_handled=False): surfaced → resolved. Call after the
        user's response has been captured and the thread is complete.

        auto_handled=True: delivered → resolved directly. Use when a
        covenant auto-handled the message and the user was never shown.
        """
        dispatcher = self._get_relational_dispatcher()
        if dispatcher is None:
            return "Relational messaging is not available."
        message_id = (tool_input.get("message_id") or "").strip()
        auto_handled = bool(tool_input.get("auto_handled"))
        reason = (tool_input.get("reason") or "").strip()
        if not message_id:
            return "Error: message_id is required."

        msg = await self.state.get_relational_message(instance_id, message_id)
        if msg is None:
            return f"Error: message {message_id!r} not found."
        # Guard: only the addressee may resolve their own message.
        if requesting_member_id and msg.addressee_member_id != requesting_member_id:
            return "Error: only the addressee can resolve a relational message."

        from_state = "delivered" if auto_handled else "surfaced"
        ok = await dispatcher.mark_resolved(
            instance_id, message_id, from_state=from_state, reason=reason,
        )
        if ok:
            return f"Resolved (from {from_state}, reason={reason or '-'})."
        # CAS lost — report current state to help the agent reason.
        current = await self.state.get_relational_message(instance_id, message_id)
        cur_state = current.state if current else "unknown"
        return (
            f"Could not resolve from {from_state}: current state is {cur_state!r}. "
            "Another path may have already processed this."
        )

    async def _handle_manage_plan(
        self, instance_id: str, space_id: str, tool_input: dict,
    ) -> str:
        """Handle manage_plan tool call — create, continue, status, pause."""
        from kernos.kernel.execution import (
            load_plan, save_plan, check_budget, build_envelope_from_plan,
            generate_plan_id, ExecutionEnvelope,
        )

        action = tool_input.get("action", "")
        data_dir = os.getenv("KERNOS_DATA_DIR", "./data")

        # --- CREATE ---
        # Handle show_progress toggle on any action
        _show_progress_override = tool_input.get("show_progress")

        if action == "create":
            title = tool_input.get("title", "Untitled Plan")
            phases = tool_input.get("phases", [])
            if not phases:
                return "Error: 'phases' is required for create. Each phase needs id, title, and steps."
            budget_override = tool_input.get("budget_override")
            plan_id = generate_plan_id()
            # Build plan structure
            for phase in phases:
                for step in phase.get("steps", []):
                    step.setdefault("status", "pending")
            budget = {"max_steps": 30, "max_tokens": 500000, "max_time_s": 3600}
            if budget_override and isinstance(budget_override, dict):
                for key in ("max_steps", "max_tokens", "max_time_s"):
                    if key in budget_override:
                        val = budget_override[key]
                        if val == 0:
                            budget[key] = 999999 if "time" not in key else 86400
                        elif isinstance(val, (int, float)) and val > 0:
                            budget[key] = int(val)
            plan = {
                "plan_id": plan_id,
                "title": title,
                "status": "active",
                "workspace_id": space_id,
                "phases": phases,
                "budget": budget,
                "usage": {"steps_used": 0, "tokens_used": 0, "elapsed_s": 0},
                "discoveries": [],
                "show_progress": _show_progress_override if _show_progress_override is not None else True,
                "created_at": utc_now(),
            }
            await save_plan(data_dir, instance_id, space_id, plan)
            # Find the first step and kick it off
            first_step = phases[0]["steps"][0] if phases and phases[0].get("steps") else None
            if first_step:
                step_id = first_step["id"]
                step_desc = first_step["title"]
                envelope = build_envelope_from_plan(plan, step_id, step_desc)
                _total_steps = sum(len(p.get("steps", [])) for p in phases)
                envelope.is_final_step = _total_steps == 1
                plan["current_step"] = step_id
                plan["usage"]["steps_used"] = 1
                first_step["status"] = "in_progress"
                await save_plan(data_dir, instance_id, space_id, plan)
                import asyncio
                asyncio.create_task(self._execute_self_directed_step(instance_id, space_id, envelope))
                logger.info("PLAN_CREATE: plan=%s title=%r steps=%d first_step=%s",
                    plan_id, title, _total_steps, step_id)
                # Ephemeral progress notification (deleted when step completes)
                if plan.get("show_progress", True):
                    try:
                        _pmid = await self.send_outbound(
                            instance_id, instance_id, None,
                            f"📋 **{title}** — step 1/{_total_steps}: {step_desc}",
                        )
                        if _pmid:
                            _chid = self._get_outbound_channel_id()
                            self._plan_progress_msgs[plan_id] = (_chid, _pmid)
                    except Exception:
                        pass
                return f"Plan '{title}' created ({plan_id}). Starting step {step_id}: {step_desc}"
            logger.info("PLAN_CREATE: plan=%s title=%r (no steps)", plan_id, title)
            return f"Plan '{title}' created ({plan_id}). No steps defined — add phases with steps."

        # --- STATUS ---
        if action == "status":
            plan_id = tool_input.get("plan_id", "")
            plan = await load_plan(data_dir, instance_id, space_id)
            if not plan:
                return "No plan found in this space."
            from kernos.kernel.execution import _plan_to_markdown
            return _plan_to_markdown(plan)

        # --- PAUSE ---
        if action == "pause":
            plan_id = tool_input.get("plan_id", "")
            plan = await load_plan(data_dir, instance_id, space_id)
            if not plan:
                return "No plan found in this space."
            plan["status"] = "paused"
            plan["paused_reason"] = "user_requested"
            await save_plan(data_dir, instance_id, space_id, plan)
            logger.info("PLAN_PAUSE: plan=%s", plan.get("plan_id", "?"))
            return f"Plan '{plan.get('title', '?')}' paused."

        # --- CONTINUE ---
        if action == "continue":
            plan_id = tool_input.get("plan_id", "")
            step_id = tool_input.get("step_id", "")
            step_desc = tool_input.get("step_description", "")
            notify_user = tool_input.get("notify_user", "")
            budget_override = tool_input.get("budget_override")

            plan = await load_plan(data_dir, instance_id, space_id)
            if not plan:
                return f"No plan found in this space."
            plan_id = plan.get("plan_id", plan_id)

            # Apply show_progress toggle if provided
            if _show_progress_override is not None:
                plan["show_progress"] = _show_progress_override

            # If no step_id provided, find the next pending step
            if not step_id:
                for phase in plan.get("phases", []):
                    for step in phase.get("steps", []):
                        if step.get("status") == "pending":
                            step_id = step["id"]
                            step_desc = step_desc or step["title"]
                            break
                    if step_id:
                        break
            if not step_id:
                # All steps done — mark plan complete
                plan["status"] = "complete"
                await save_plan(data_dir, instance_id, space_id, plan)
                logger.info("PLAN_COMPLETE: plan=%s (no pending steps on continue call)",
                    plan.get("plan_id", "?"))
                return "No pending steps remain. Plan is complete."

            # If step_id doesn't exist in the plan, add it dynamically
            # (plans are mutable — a step may expand into sub-steps during execution)
            _valid_step_ids = {
                step["id"]
                for phase in plan.get("phases", [])
                for step in phase.get("steps", [])
            }
            if step_id not in _valid_step_ids:
                # Add to the last phase as a new step
                if plan.get("phases"):
                    plan["phases"][-1]["steps"].append({
                        "id": step_id,
                        "title": step_desc,
                        "status": "pending",
                    })
                    logger.info("PLAN_STEP_ADDED: plan=%s step=%s title=%r",
                        plan_id, step_id, step_desc[:60])

            # Apply user-requested budget overrides
            if budget_override and isinstance(budget_override, dict):
                _budget = plan.setdefault("budget", {})
                for key in ("max_steps", "max_tokens", "max_time_s"):
                    if key in budget_override:
                        val = budget_override[key]
                        if val == 0:
                            _budget[key] = 999999 if "time" not in key else 86400
                        elif isinstance(val, (int, float)) and val > 0:
                            _budget[key] = int(val)
                logger.info("PLAN_BUDGET_OVERRIDE: plan=%s new_budget=%s", plan_id, _budget)

            # If resuming a paused plan, extend the budget
            if plan.get("status") == "paused":
                _budget = plan.setdefault("budget", {})
                _usage = plan.setdefault("usage", {})
                if not budget_override:
                    _budget["max_steps"] = _usage.get("steps_used", 0) + _budget.get("max_steps", 30)
                    _budget["max_tokens"] = _usage.get("tokens_used", 0) + _budget.get("max_tokens", 500000)
                    _budget["max_time_s"] = _usage.get("elapsed_s", 0) + _budget.get("max_time_s", 3600)
                plan["status"] = "active"
                plan.pop("paused_reason", None)
                plan.pop("paused_at_step", None)
                plan.pop("paused_next_description", None)
                logger.info("PLAN_RESUME: plan=%s extended budget steps=%d tokens=%d time=%ds",
                    plan_id, _budget["max_steps"], _budget["max_tokens"], _budget["max_time_s"])

            # Check budgets
            budget_hit = check_budget(plan)
            if budget_hit:
                plan["status"] = "paused"
                plan["paused_reason"] = budget_hit
                plan["paused_at_step"] = step_id
                plan["paused_next_description"] = step_desc
                await save_plan(data_dir, instance_id, space_id, plan)
                logger.info("PLAN_BUDGET_HIT: plan=%s ceiling=%s step=%s", plan_id, budget_hit, step_id)
                _reason_display = {"step_limit": "step limit", "token_budget": "token budget", "time_limit": "time limit"}.get(budget_hit, budget_hit)
                _usage = plan.get("usage", {})
                _bgt = plan.get("budget", {})
                _steps_info = f"{_usage.get('steps_used', 0)}/{_bgt.get('max_steps', '?')} steps"
                _tokens_info = f"{_usage.get('tokens_used', 0):,}/{_bgt.get('max_tokens', '?'):,} tokens"
                _time_info = f"{_usage.get('elapsed_s', 0)}s/{_bgt.get('max_time_s', '?')}s"
                try:
                    await self.send_outbound(
                        instance_id, instance_id, None,
                        f"Plan paused — hit {_reason_display} at step {step_id}.\n"
                        f"Budget used: {_steps_info} | {_tokens_info} | {_time_info}\n"
                        f"Say \"continue\" to resume, or tell me new limits "
                        f"(e.g. \"continue with no time limit\" or \"set steps to 50\").",
                    )
                except Exception:
                    pass
                return f"Plan paused — {budget_hit} reached. User needs to approve continuation."

            # Send user notification if provided
            if notify_user and notify_user.strip():
                try:
                    await self.send_outbound(instance_id, instance_id, None, notify_user)
                except Exception:
                    pass

            # Build envelope and enqueue self-directed turn
            envelope = build_envelope_from_plan(plan, step_id, step_desc)
            plan["current_step"] = step_id
            plan["usage"]["steps_used"] = plan["usage"].get("steps_used", 0) + 1
            for phase in plan.get("phases", []):
                for step in phase.get("steps", []):
                    if step["id"] == step_id:
                        step["status"] = "in_progress"
                        break

            # Detect if this is the last pending step
            _remaining_pending = [
                s for p in plan.get("phases", [])
                for s in p.get("steps", [])
                if s.get("status") == "pending"
            ]
            envelope.is_final_step = len(_remaining_pending) == 0

            await save_plan(data_dir, instance_id, space_id, plan)

            import asyncio
            asyncio.create_task(self._execute_self_directed_step(instance_id, space_id, envelope))

            logger.info("PLAN_STEP: plan=%s step=%s description=%r budget_remaining=%d/%d",
                plan_id, step_id, step_desc[:60],
                envelope.budget_steps - envelope.steps_used, envelope.budget_steps)

            # Ephemeral progress notification (delete previous, show new)
            if plan.get("show_progress", True):
                # Delete the previous step's progress message first
                _old = self._plan_progress_msgs.pop(plan_id, None)
                if _old:
                    _old_ch, _old_mid = _old
                    try:
                        await self._delete_discord_msg(_old_ch, _old_mid)
                    except Exception:
                        pass
                _total = sum(len(p.get("steps", [])) for p in plan.get("phases", []))
                _current = plan["usage"].get("steps_used", 0)
                try:
                    _pmid = await self.send_outbound(
                        instance_id, instance_id, None,
                        f"📋 step {_current}/{_total}: {step_desc}",
                    )
                    if _pmid:
                        _chid = self._get_outbound_channel_id()
                        self._plan_progress_msgs[plan_id] = (_chid, _pmid)
                except Exception:
                    pass

            return f"Step {step_id} enqueued. Executing: {step_desc}"

        return f"Unknown action: {action}. Use create, continue, status, or pause."

    async def _execute_self_directed_step(
        self, instance_id: str, space_id: str, envelope: ExecutionEnvelope,
    ) -> None:
        """Execute a self-directed step through the pipeline."""
        from kernos.kernel.execution import load_plan
        from kernos.messages.models import NormalizedMessage, AuthLevel

        # Guard: check plan is still active and step is still executable
        data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
        plan = await load_plan(data_dir, instance_id, space_id)
        if not plan:
            logger.info("PLAN_STEP_SKIP: plan=%s — plan not found, skipping", envelope.plan_id)
            return
        if plan.get("status") in ("complete", "cancelled"):
            logger.info("PLAN_STEP_SKIP: plan=%s step=%s — plan already %s",
                envelope.plan_id, envelope.step_id, plan.get("status"))
            return
        # Check if this specific step is already complete
        for phase in plan.get("phases", []):
            for step in phase.get("steps", []):
                if step["id"] == envelope.step_id and step.get("status") == "complete":
                    logger.info("PLAN_STEP_SKIP: plan=%s step=%s — step already complete",
                        envelope.plan_id, envelope.step_id)
                    return

        # Build a self-directed message
        msg = NormalizedMessage(
            content=f"[PLAN STEP {envelope.step_id}] {envelope.step_description}",
            sender="self_directed",
            sender_auth_level=AuthLevel.owner_verified,
            platform="internal",
            platform_capabilities=["text"],
            conversation_id=f"plan_{envelope.plan_id}",
            timestamp=datetime.now(timezone.utc),
            instance_id=instance_id,
            context={"execution_envelope": {
                "plan_id": envelope.plan_id,
                "step_id": envelope.step_id,
                "step_description": envelope.step_description,
                "workspace_id": envelope.workspace_id,
                "budget_steps": envelope.budget_steps,
                "steps_used": envelope.steps_used,
                "source": "self_directed",
                "is_final_step": envelope.is_final_step,
            }},
        )

        _max_step_retries = 5
        _step_backoffs = [30, 60, 120, 300, 600]  # 30s, 1m, 2m, 5m, 10m

        # EVENT-STREAM-TO-SQLITE: plan step started. Fires once per step
        # (the retry loop inside re-tries the same step on failure).
        try:
            from kernos.kernel import event_stream
            await event_stream.emit(
                instance_id, "plan.step_started",
                {
                    "plan_id": envelope.plan_id,
                    "step_id": envelope.step_id,
                    "step_description": (envelope.step_description or "")[:200],
                },
                space_id=space_id,
            )
        except Exception as exc:
            logger.debug("Failed to emit plan.step_started: %s", exc)

        for step_attempt in range(_max_step_retries):
            try:
                response = await self.process(msg)
                logger.info("PLAN_STEP_COMPLETE: plan=%s step=%s response_len=%d",
                    envelope.plan_id, envelope.step_id, len(response or ""))
                # EVENT-STREAM-TO-SQLITE: plan step completed.
                try:
                    from kernos.kernel import event_stream
                    await event_stream.emit(
                        instance_id, "plan.step_completed",
                        {
                            "plan_id": envelope.plan_id,
                            "step_id": envelope.step_id,
                            "response_len": len(response or ""),
                            "attempts": step_attempt + 1,
                        },
                        space_id=space_id,
                    )
                except Exception as exc:
                    logger.debug("Failed to emit plan.step_completed: %s", exc)

                # Delete the progress notification for this step
                _progress = self._plan_progress_msgs.pop(envelope.plan_id, None)
                if _progress:
                    _p_ch, _p_mid = _progress
                    try:
                        await self._delete_discord_msg(_p_ch, _p_mid)
                    except Exception:
                        pass

                # Mark the step complete in the plan
                from kernos.kernel.execution import load_plan, save_plan
                data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
                plan = await load_plan(data_dir, instance_id, space_id)
                if plan:
                    for phase in plan.get("phases", []):
                        for step in phase.get("steps", []):
                            if step["id"] == envelope.step_id:
                                step["status"] = "complete"
                                break

                    # Check if all steps are complete (plan finished)
                    all_done = all(
                        step.get("status") in ("complete", "skipped")
                        for phase in plan.get("phases", [])
                        for step in phase.get("steps", [])
                    )
                    if all_done:
                        plan["status"] = "complete"
                        logger.info("PLAN_COMPLETE: plan=%s title=%r steps=%d",
                            envelope.plan_id, plan.get("title", "?"),
                            plan.get("usage", {}).get("steps_used", 0))
                        try:
                            if response and response.strip():
                                _title = plan.get("title", "Plan")
                                await self.send_outbound(
                                    instance_id, instance_id, None,
                                    f"**{_title}** — complete\n\n{response}",
                                )
                        except Exception:
                            pass
                    await save_plan(data_dir, instance_id, space_id, plan)
                return  # Step succeeded — exit retry loop

            except Exception as exc:
                _delay = _step_backoffs[min(step_attempt, len(_step_backoffs) - 1)]
                logger.warning("PLAN_STEP_FAILED: plan=%s step=%s attempt=%d/%d error=%s retry_in=%ds",
                    envelope.plan_id, envelope.step_id,
                    step_attempt + 1, _max_step_retries, exc, _delay)
                # EVENT-STREAM-TO-SQLITE: plan step failure.
                try:
                    from kernos.kernel import event_stream
                    await event_stream.emit(
                        instance_id, "plan.step_failed",
                        {
                            "plan_id": envelope.plan_id,
                            "step_id": envelope.step_id,
                            "attempt": step_attempt + 1,
                            "max_retries": _max_step_retries,
                            "error": str(exc)[:200],
                        },
                        space_id=space_id,
                    )
                except Exception as _em_exc:
                    logger.debug("Failed to emit plan.step_failed: %s", _em_exc)

                if step_attempt == 0:
                    # First failure — notify user
                    try:
                        await self.send_outbound(
                            instance_id, instance_id, None,
                            f"Plan step hit an API error — automatically retrying in {_delay}s.",
                        )
                    except Exception:
                        pass

                if step_attempt < _max_step_retries - 1:
                    await asyncio.sleep(_delay)
                    # Rebuild the message for retry (fresh timestamp)
                    msg = NormalizedMessage(
                        content=msg.content,
                        sender="self_directed",
                        sender_auth_level=msg.sender_auth_level,
                        platform="internal",
                        platform_capabilities=["text"],
                        conversation_id=msg.conversation_id,
                        timestamp=datetime.now(timezone.utc),
                        instance_id=instance_id,
                        context=msg.context,
                    )
                    continue

                # Fast retries exhausted — switch to hourly slow-poll
                _slow_interval = int(os.getenv("KERNOS_PLAN_SLOW_RETRY_S", "3600"))
                try:
                    await self.send_outbound(
                        instance_id, instance_id, None,
                        f"The API has been down for ~{sum(_step_backoffs[:_max_step_retries]) // 60} minutes. "
                        f"I'll keep retrying every hour until it's back.",
                    )
                except Exception:
                    pass
                logger.warning("PLAN_STEP_SLOW_POLL: plan=%s step=%s — entering hourly retry",
                    envelope.plan_id, envelope.step_id)

                _slow_attempt = 0
                while True:
                    _slow_attempt += 1
                    await asyncio.sleep(_slow_interval)

                    # Check if plan was cancelled/paused by user while we waited
                    from kernos.kernel.execution import load_plan as _lp
                    _check = await _lp(os.getenv("KERNOS_DATA_DIR", "./data"), instance_id, space_id)
                    if _check and _check.get("status") in ("paused", "complete", "cancelled"):
                        logger.info("PLAN_STEP_SLOW_POLL: plan=%s aborted — status=%s",
                            envelope.plan_id, _check.get("status"))
                        return

                    try:
                        msg = NormalizedMessage(
                            content=msg.content,
                            sender="self_directed",
                            sender_auth_level=msg.sender_auth_level,
                            platform="internal",
                            platform_capabilities=["text"],
                            conversation_id=msg.conversation_id,
                            timestamp=datetime.now(timezone.utc),
                            instance_id=instance_id,
                            context=msg.context,
                        )
                        response = await self.process(msg)
                        logger.info("PLAN_STEP_RECOVERED: plan=%s step=%s slow_attempt=%d",
                            envelope.plan_id, envelope.step_id, _slow_attempt)
                        try:
                            await self.send_outbound(
                                instance_id, instance_id, None,
                                f"API is back — plan resuming.",
                            )
                        except Exception:
                            pass
                        # Mark complete and check plan status (same as success path above)
                        from kernos.kernel.execution import load_plan, save_plan
                        data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
                        plan = await load_plan(data_dir, instance_id, space_id)
                        if plan:
                            for phase in plan.get("phases", []):
                                for step in phase.get("steps", []):
                                    if step["id"] == envelope.step_id:
                                        step["status"] = "complete"
                                        break
                            all_done = all(
                                step.get("status") in ("complete", "skipped")
                                for phase in plan.get("phases", [])
                                for step in phase.get("steps", [])
                            )
                            if all_done:
                                plan["status"] = "complete"
                                logger.info("PLAN_COMPLETE: plan=%s (recovered)", envelope.plan_id)
                                try:
                                    if response and response.strip():
                                        _title = plan.get("title", "Plan")
                                        await self.send_outbound(
                                            instance_id, instance_id, None,
                                            f"**{_title}** — complete\n\n{response}",
                                        )
                                except Exception:
                                    pass
                            await save_plan(data_dir, instance_id, space_id, plan)
                        return  # Recovered — exit
                    except Exception as slow_exc:
                        logger.warning("PLAN_STEP_SLOW_POLL: plan=%s attempt=%d still failing: %s",
                            envelope.plan_id, _slow_attempt, slow_exc)
                        if _slow_attempt % 3 == 0:  # Notify user every 3 hours
                            try:
                                await self.send_outbound(
                                    instance_id, instance_id, None,
                                    f"Still can't reach the API ({_slow_attempt}h). "
                                    f"I'll keep trying. Say \"pause plan\" to stop.",
                                )
                            except Exception:
                                pass

    async def _handle_spaces(self, ctx: TurnContext, raw_cmd: str) -> str:
        """List spaces or create a new one manually."""
        import uuid as _uuid
        import shlex

        instance_id = ctx.instance_id
        parts = raw_cmd.strip().split(None, 1)
        sub = parts[1].strip() if len(parts) > 1 else ""

        if sub.lower().startswith("create"):
            # /spaces create "Name" "Description"
            create_args = sub[len("create"):].strip()
            try:
                tokens = shlex.split(create_args)
            except ValueError:
                tokens = create_args.split('"')
                tokens = [t.strip() for t in tokens if t.strip()]
            if len(tokens) < 1:
                return 'Usage: /spaces create "Name" "Description"'
            name = tokens[0]
            description = tokens[1] if len(tokens) > 1 else ""
            now = utc_now()
            new_space = ContextSpace(
                id=f"space_{_uuid.uuid4().hex[:8]}",
                instance_id=instance_id,
                member_id=ctx.member_id,
                name=name,
                description=description,
                space_type="domain",
                status="active",
                is_default=False,
                created_at=now,
                last_active_at=now,
            )
            await self.state.save_context_space(new_space)

            # Initialize compaction state for the new space
            try:
                from kernos.kernel.compaction import (
                    CompactionState, compute_document_budget,
                    MODEL_MAX_TOKENS, COMPACTION_MODEL_USABLE_TOKENS,
                    COMPACTION_INSTRUCTION_TOKENS, DEFAULT_DAILY_HEADROOM,
                )
                headroom = DEFAULT_DAILY_HEADROOM
                doc_budget = compute_document_budget(MODEL_MAX_TOKENS, 4000, 0, headroom)
                comp = CompactionState(
                    space_id=new_space.id,
                    conversation_headroom=headroom,
                    document_budget=doc_budget,
                    message_ceiling=min(
                        doc_budget,
                        COMPACTION_MODEL_USABLE_TOKENS - COMPACTION_INSTRUCTION_TOKENS,
                    ),
                    _context_def_tokens=0,
                    _system_overhead=4000,
                )
                # DISCLOSURE-GATE: compaction state member-scoped.
                await self.compaction.save_state(
                    instance_id, new_space.id, comp, member_id=ctx.member_id,
                )
            except Exception as exc:
                logger.warning("Failed to init compaction for manual space: %s", exc)

            logger.info("SPACE_CREATE: id=%s name=%s source=manual", new_space.id, new_space.name)
            return f"Created space **{name}** ({new_space.id}). Description: {description or '(none)'}"

        # Default: list all spaces (user-facing — no internal fields)
        from datetime import datetime, timezone
        spaces = await self.state.list_context_spaces(instance_id)
        active = [s for s in spaces if s.status == "active"]
        if not active:
            return "No context spaces found."

        now = datetime.now(timezone.utc)
        lines = ["**Your Spaces**\n"]
        for s in sorted(active, key=lambda x: x.last_active_at or "", reverse=True):
            if s.space_type == "system":
                continue  # Don't show system internals
            current = " **(you are here)**" if s.id == ctx.active_space_id else ""
            default = " — default" if s.is_default else ""
            # Relative time
            age = ""
            if s.last_active_at:
                try:
                    last = datetime.fromisoformat(s.last_active_at)
                    days = (now - last).days
                    if days == 0:
                        age = " — active today"
                    elif days == 1:
                        age = " — yesterday"
                    elif days < 7:
                        age = f" — {days} days ago"
                    else:
                        age = f" — {days}d ago"
                except (ValueError, TypeError):
                    pass
            parent_note = ""
            if s.parent_id:
                parent = next((p for p in active if p.id == s.parent_id), None)
                if parent:
                    parent_note = f" (within {parent.name})"
            lines.append(f"- **{s.name}**{current}{default}{parent_note}{age}")
            if s.description:
                lines.append(f"  {s.description}")
        return "\n".join(lines)

    async def _handle_status(self, ctx: TurnContext) -> str:
        """User-readable status summary.

        SURFACE-DISCIPLINE-PASS D5: `/status` now returns a concise,
        human-readable summary — no internal identifiers, no file paths.
        The operator-state-view + timing dump moved to `/dump` which is
        a diagnostic/admin surface and keeps raw internals by design.
        """
        instance_id = ctx.instance_id
        parts: list[str] = ["**Kernos Status**", ""]

        # Member greeting line
        member_name = ""
        if ctx.member_profile:
            member_name = ctx.member_profile.get("display_name", "") or ""
        if member_name:
            parts.append(f"Signed in as **{member_name}**.")

        # Connected platforms (human-readable, no raw channel ids)
        try:
            connected = self._channel_registry.get_connected()
        except Exception:
            connected = []
        if connected:
            platform_names = []
            for ch in connected:
                marker = " (current)" if ctx.message and ch.platform == ctx.message.platform else ""
                platform_names.append(f"{ch.display_name}{marker}")
            parts.append(f"Platforms connected: {', '.join(platform_names)}.")
        else:
            parts.append("No platforms connected.")

        # Current space (display name, not id)
        if ctx.active_space and getattr(ctx.active_space, "name", ""):
            parts.append(f"Current space: **{ctx.active_space.name}**.")

        # Pending reminders / triggers count
        trigger_store = getattr(self.reasoning, '_trigger_store', None)
        if trigger_store is not None:
            try:
                triggers = await trigger_store.list_active(instance_id)
                if triggers:
                    parts.append(f"Active reminders: {len(triggers)}.")
            except Exception:
                pass

        # Pending whispers count
        try:
            whispers = await self.state.get_pending_whispers(instance_id)
            if whispers:
                # Only count whispers owned by this member or instance-wide.
                _mine = [
                    w for w in whispers
                    if not getattr(w, "owner_member_id", "")
                    or getattr(w, "owner_member_id", "") == ctx.member_id
                ]
                if _mine:
                    parts.append(f"Pending signals: {len(_mine)}.")
        except Exception:
            pass

        # MODEL-AND-STATUS-V1: Models block. Surfaces the active chain,
        # effective head, fallback entries, and any persisted override
        # (including stale specs) so the user can see at a glance which
        # model is answering for this space.
        try:
            models_block = await self._render_models_block(ctx)
        except Exception as _mblk_exc:
            logger.warning("MODELS_BLOCK: render failed: %s", _mblk_exc)
            models_block = ""
        if models_block:
            parts.append("")
            parts.append(models_block)

        parts.append("")
        parts.append(
            "For a full diagnostic snapshot (internal ids, runtime trace, "
            "phase timings) use **/dump** — admin surface."
        )
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # MODEL-AND-STATUS-V1: Models block + /model command
    # ------------------------------------------------------------------

    def _resolve_effective_chain_for_ctx(
        self, ctx: TurnContext, override: dict | None,
        requested_chain: str = "primary",
    ) -> EffectiveChain | None:
        """Wrapper that fetches the configured chains from
        ReasoningService and resolves the EffectiveChain for this
        (member, space) override. Returns None when chains are not
        introspectable.
        """
        chains = getattr(self.reasoning, "_chains", None)
        if not chains:
            return None
        return resolve_effective_chain(
            chains=chains, requested_chain=requested_chain,
            override=override,
        )

    async def _render_models_block(self, ctx: TurnContext) -> str:
        """Render the **Models** block appended to /status output. See
        spec section "/status — Models block"."""
        chains = getattr(self.reasoning, "_chains", None)
        if not chains:
            return ""
        override = None
        if (
            self._instance_db and ctx.member_id
            and ctx.active_space_id
        ):
            override = await self._instance_db.get_model_override(
                instance_id=ctx.instance_id,
                member_id=ctx.member_id,
                space_id=ctx.active_space_id,
            )
        eff = resolve_effective_chain(
            chains=chains, requested_chain="primary", override=override,
        )

        lines = ["**Models** (this space)"]
        lines.append(f"Active chain: {eff.chain_name}")
        lines.append(
            f"Effective head: {eff.head_provider}/{eff.head_model}"
        )
        # Fallback entries after the head.
        fallback_entries = list(eff.entries)[1:]
        if fallback_entries:
            fb = ", ".join(
                f"{getattr(e.provider, 'provider_name', '?')}/{e.model}"
                for e in fallback_entries
            )
            lines.append(f"Fallback: {fb}")
        if eff.override_in_effect:
            ov_parts = []
            if override and override.get("chain_name"):
                ov_parts.append(f"chain={override['chain_name']}")
            if (
                override and override.get("override_provider")
                and override.get("override_model")
            ):
                ov_parts.append(
                    f"head={override['override_provider']}/"
                    f"{override['override_model']}"
                )
            if ov_parts:
                lines.append(
                    f"Override (this space): {', '.join(ov_parts)}"
                )
        # Stale-config markers.
        if eff.stale_chain_name:
            lines.append(
                f"Override chain {eff.stale_chain_name!r} is "
                "unavailable — not in any current chain."
            )
        if eff.stale_head_spec:
            lines.append(
                f"Override head {eff.stale_head_spec} is "
                "unavailable — not in any current chain."
            )
        return "\n".join(lines)

    async def _handle_model_command(
        self, ctx: TurnContext, cmd: str,
    ) -> str:
        """Dispatch /model. See spec section "/model — list / switch".

        Modes:
          * /model            → list chains + effective head + targets
          * /model <chain>    → switch active chain
          * /model <p>/<m>    → override head (must be in some chain)
          * /model reset      → clear chain + head override
        """
        chains = getattr(self.reasoning, "_chains", None)
        if not chains:
            return "Models are not configured for this install."
        if not (self._instance_db and ctx.member_id and ctx.active_space_id):
            return (
                "Cannot apply a model override without a member + space "
                "context. /model is per-(member, space) sticky."
            )

        # Strip the leading /model and split args.
        rest = cmd.strip()[len("/model"):].strip()
        override = await self._instance_db.get_model_override(
            instance_id=ctx.instance_id,
            member_id=ctx.member_id,
            space_id=ctx.active_space_id,
        )

        # No-args list.
        if not rest:
            return self._render_model_list(chains, override)

        # /model reset
        if rest.lower() == "reset":
            removed = await self._instance_db.reset_model_override(
                instance_id=ctx.instance_id,
                member_id=ctx.member_id,
                space_id=ctx.active_space_id,
            )
            if removed:
                return (
                    "Cleared model override. Falling back to configured "
                    "default (primary chain)."
                )
            return "No override was set for this space."

        # /model <chain>
        if rest in chains:
            await self._instance_db.set_model_chain(
                instance_id=ctx.instance_id,
                member_id=ctx.member_id,
                space_id=ctx.active_space_id,
                chain_name=rest,
            )
            new_override = await self._instance_db.get_model_override(
                instance_id=ctx.instance_id,
                member_id=ctx.member_id,
                space_id=ctx.active_space_id,
            )
            eff = resolve_effective_chain(
                chains=chains, requested_chain="primary",
                override=new_override,
            )
            return (
                f"Switched chain to **{rest}** for this space.\n"
                f"Effective head: {eff.head_provider}/{eff.head_model}"
            )

        # /model <provider>/<model>
        parsed = parse_provider_model_spec(rest)
        if parsed is not None:
            provider, model = parsed
            from kernos.kernel.model_routing import head_spec_in_any_chain
            if head_spec_in_any_chain(chains, provider, model):
                await self._instance_db.set_model_head_override(
                    instance_id=ctx.instance_id,
                    member_id=ctx.member_id,
                    space_id=ctx.active_space_id,
                    provider=provider, model=model,
                )
                return (
                    f"Override head set to **{provider}/{model}** for "
                    "this space. The override is the preferred first "
                    "attempt; chain fallbacks still apply on failure."
                )
            available = list_configured_entries(chains)
            available_md = "\n".join(
                f"  • {p}/{m}" for p, m in available
            )
            return (
                f"`{provider}/{model}` is not in any configured chain. "
                "Available entries:\n" + available_md
            )

        # Unknown argument shape.
        return (
            "Usage:\n"
            "  /model               — show current models for this space\n"
            "  /model <chain>       — switch chain (e.g. primary | lightweight)\n"
            "  /model <provider>/<model>\n"
            "                        — override head (must be in a configured chain)\n"
            "  /model reset         — clear override"
        )

    def _render_model_list(
        self, chains, override: dict | None,
    ) -> str:
        """Compose the no-args /model output. Pure function over the
        chains config and persisted override; testable without IO."""
        eff = resolve_effective_chain(
            chains=chains, requested_chain="primary", override=override,
        )
        lines = ["**Models** (this space)"]
        lines.append(f"Active chain: {eff.chain_name}")
        lines.append(
            f"Effective head: {eff.head_provider}/{eff.head_model} (active)"
        )
        if eff.stale_chain_name:
            lines.append(
                f"Override chain {eff.stale_chain_name!r} is "
                "unavailable — not in any current chain."
            )
        if eff.stale_head_spec:
            lines.append(
                f"Override head {eff.stale_head_spec} is "
                "unavailable — not in any current chain."
            )

        lines.append("")
        lines.append("Available chains:")
        for chain_name, chain_entries in chains.items():
            chain_view = " → ".join(
                f"{getattr(e.provider, 'provider_name', '?')}/{e.model}"
                for e in chain_entries
            )
            marker = " (active)" if chain_name == eff.chain_name else ""
            lines.append(f"  • {chain_name}{marker} — {chain_view}")

        lines.append("")
        lines.append("Switch with:")
        lines.append("  /model primary | lightweight        — switch chain")
        lines.append(
            "  /model anthropic/claude-haiku-4.5   "
            "— override head (must be in a configured chain)"
        )
        lines.append("  /model reset                         — clear override")
        return "\n".join(lines)

    async def _build_departure_context(
        self, ctx: TurnContext, prev_space_id: str,
    ) -> dict | None:
        """Build ephemeral context from departing space for discourse continuity.

        Bounded by both count (up to 6 entries / 3 pairs) and character
        budget (~1200 chars / ~300 tokens). Not persisted to the new space.
        """
        if not prev_space_id or prev_space_id == ctx.active_space_id:
            return None

        # read_recent returns [{role, content, timestamp, channel}, ...]
        recent = await self.conv_logger.read_recent(
            ctx.instance_id, prev_space_id, token_budget=1200, max_messages=6,
            member_id=ctx.member_id,
        )
        if not recent:
            return None

        DEPARTURE_CHAR_BUDGET = 1200
        PER_MSG_CAP = 300

        prev_space = await self.state.get_context_space(ctx.instance_id, prev_space_id)
        prev_name = prev_space.name if prev_space else prev_space_id

        # Walk backward, stop when budget exhausted
        selected: list[dict] = []
        char_total = 0
        for entry in reversed(recent):
            content = entry.get("content", "")[:PER_MSG_CAP]
            if char_total + len(content) > DEPARTURE_CHAR_BUDGET and selected:
                break
            selected.insert(0, entry)
            char_total += len(content)

        if not selected:
            return None

        lines = [f"[Previous context — from space: {prev_name}]"]
        for entry in selected:
            role = entry.get("role", "?")
            content = entry.get("content", "")[:PER_MSG_CAP]
            label = "User" if role == "user" else "Assistant"
            lines.append(f"[{label}]: {content}")
        lines.append(f"[Conversation continues in current space: {ctx.active_space.name if ctx.active_space else ctx.active_space_id}]")

        logger.info("DEPARTURE_CONTEXT: from=%s entries=%d chars=%d",
            prev_space_id, len(selected), char_total)
        return {"role": "user", "content": "\n".join(lines)}

    async def _phase_provision(self, ctx: TurnContext) -> None:
        """Phase 1: Ensure tenant, soul, MCP config, covenants, evaluator ready.

        HANDLER-PIPELINE-DECOMPOSE: delegates to phases/provision.py.
        Kept as a shim for back-compat with callers that still invoke the
        method directly (slash commands, tests). Removed when the pipeline
        flip retires the method.
        """
        from kernos.messages.phases import provision
        if ctx.handler is None:
            ctx.handler = self
        await provision.run(ctx)

    async def _phase_route(self, ctx: TurnContext) -> None:
        """Phase 2: Determine context space, handle space switching, file uploads.

        HANDLER-PIPELINE-DECOMPOSE: delegates to phases/route.py.
        """
        from kernos.messages.phases import route
        if ctx.handler is None:
            ctx.handler = self
        await route.run(ctx)

    async def _phase_assemble(self, ctx: TurnContext) -> None:
        """Phase 3: Build Cognitive UI blocks — system prompt, tools, messages.

        HANDLER-PIPELINE-DECOMPOSE: delegates to phases/assemble.py.
        """
        from kernos.messages.phases import assemble
        if ctx.handler is None:
            ctx.handler = self
        await assemble.run(ctx)

    async def _phase_reason(self, ctx: TurnContext) -> None:
        """Phase 4: Build ReasoningRequest, execute via task engine.

        HANDLER-PIPELINE-DECOMPOSE: delegates to phases/reason.py.
        """
        from kernos.messages.phases import reason
        if ctx.handler is None:
            ctx.handler = self
        await reason.run(ctx)

    async def _phase_consequence(self, ctx: TurnContext) -> None:
        """Phase 5: Confirmation replay, tool config, projectors, soul update.

        HANDLER-PIPELINE-DECOMPOSE: delegates to phases/consequence.py.
        """
        from kernos.messages.phases import consequence
        if ctx.handler is None:
            ctx.handler = self
        await consequence.run(ctx)

    async def _phase_persist(self, ctx: TurnContext) -> None:
        """Phase 6: Store messages, write to conv log, compaction, events.

        HANDLER-PIPELINE-DECOMPOSE: delegates to phases/persist.py.
        """
        from kernos.messages.phases import persist
        if ctx.handler is None:
            ctx.handler = self
        await persist.run(ctx)

    def _record_phase_timings(self, timings: dict[str, int], total_ms: int) -> None:
        """Record phase timings for session averages. Keep last 50 turns."""
        entry = dict(timings)
        entry["total"] = total_ms
        self._phase_timing_history.append(entry)
        if len(self._phase_timing_history) > 50:
            self._phase_timing_history = self._phase_timing_history[-50:]

    def get_phase_timing_averages(self) -> dict[str, int]:
        """Return average phase timings across the session."""
        if not self._phase_timing_history:
            return {}
        phases = ["provision", "route", "assemble", "reason", "consequence", "persist", "total"]
        avgs: dict[str, int] = {}
        for phase in phases:
            values = [t.get(phase, 0) for t in self._phase_timing_history if phase in t]
            if values:
                avgs[phase] = sum(values) // len(values)
        return avgs

    def _load_workspace_tool_schema(self, instance_id: str, tool_name: str) -> dict | None:
        """Load a workspace tool's schema from its .tool.json descriptor."""
        catalog_entry = self._tool_catalog.get(tool_name)
        if not catalog_entry or catalog_entry.source != "workspace":
            return None
        home_space = getattr(catalog_entry, "home_space", "")
        if not home_space:
            return None
        # Find the descriptor file from the workspace manifest
        try:
            desc_file = f"{tool_name}.tool.json"
            from kernos.utils import _safe_name
            desc_path = (
                Path(os.getenv("KERNOS_DATA_DIR", "./data"))
                / _safe_name(instance_id) / "spaces" / home_space / "files" / desc_file
            )
            if desc_path.exists():
                descriptor = json.loads(desc_path.read_text(encoding="utf-8"))
                return {
                    "name": descriptor.get("name", tool_name),
                    "description": descriptor.get("description", ""),
                    "input_schema": descriptor.get("input_schema", {"type": "object", "properties": {}}),
                }
        except Exception as exc:
            logger.warning("WORKSPACE_SCHEMA_LOAD: failed for %s: %s", tool_name, exc)
        return None

    async def _check_catalog_version(
        self, instance_id: str, space_id: str, space: ContextSpace,
    ) -> None:
        """Lazy version promotion: scan new tools for relevance to this space.

        On space entry, if catalog.version > space.last_catalog_version,
        new tools have been registered since last visit. Run a cheap LLM
        check to see if any are relevant, and promote them into the
        space's local affordance set.
        """
        import json as _json
        catalog = self._tool_catalog
        if not catalog or space.last_catalog_version >= catalog.version:
            return  # Up to date

        # Get tools not already in this space's affordance set
        aff = space.local_affordance_set if isinstance(space.local_affordance_set, dict) else {}
        current_set = set(aff.keys())
        from kernos.kernel.tool_catalog import ALWAYS_PINNED, COMMON_MCP_NAMES
        already_known = current_set | ALWAYS_PINNED | COMMON_MCP_NAMES
        new_tools = [
            e for e in catalog.get_all()
            if e.name not in already_known and e.source == "workspace"
        ]

        if not new_tools:
            # No new workspace tools — just update the version marker
            await self.state.update_context_space(instance_id, space_id, {
                "last_catalog_version": catalog.version,
            })
            return

        # Ask cheap LLM: which of these new tools are relevant to this space?
        tool_lines = "\n".join(f"- {t.name}: {t.description}" for t in new_tools)
        try:
            result_str = await self.reasoning.complete_simple(
                system_prompt=(
                    "Given this context space and the new tools below, which tools "
                    "would be regularly useful in this domain? Only include tools that "
                    "are genuinely relevant to this space's typical work. Return a JSON "
                    "array of tool names, or an empty array if none are relevant."
                ),
                user_content=(
                    f"Space: {space.name}\n"
                    f"Description: {space.description}\n\n"
                    f"New tools:\n{tool_lines}"
                ),
                output_schema={
                    "type": "object",
                    "properties": {
                        "promote": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["promote"],
                    "additionalProperties": False,
                },
                max_tokens=128,
                prefer_cheap=True,
            )
            parsed = _json.loads(result_str)
            to_promote = [n for n in parsed.get("promote", []) if n in {t.name for t in new_tools}]

            if to_promote:
                new_aff = dict(aff)
                for name in to_promote:
                    new_aff[name] = {"last_turn": 0, "tokens": 0}
                await self.state.update_context_space(instance_id, space_id, {
                    "local_affordance_set": new_aff,
                    "last_catalog_version": catalog.version,
                })
                logger.info("TOOL_CATALOG_SCAN: space=%s new_tools=%d promoted=%d tools=%s",
                    space_id, len(new_tools), len(to_promote), to_promote)
            else:
                await self.state.update_context_space(instance_id, space_id, {
                    "last_catalog_version": catalog.version,
                })
                logger.info("TOOL_CATALOG_SCAN: space=%s new_tools=%d promoted=0",
                    space_id, len(new_tools))
        except Exception as exc:
            # On failure, still update version to avoid re-scanning every turn
            logger.warning("TOOL_CATALOG_SCAN: failed for %s: %s", space_id, exc)
            await self.state.update_context_space(instance_id, space_id, {
                "last_catalog_version": catalog.version,
            })

    async def _promote_used_tools(
        self, instance_id: str, space_id: str, space: ContextSpace, tool_trace: list[dict],
    ) -> None:
        """Tier 3: Promote successfully used tools into the space's local affordance set.

        Updates last_turn for already-promoted tools. General (default root) only
        promotes universal tools — domain-specific tools should trigger routing.
        """
        from kernos.kernel.tool_catalog import ALWAYS_PINNED, COMMON_MCP_NAMES
        try:
            aff = dict(space.local_affordance_set) if isinstance(space.local_affordance_set, dict) else {}
            _turn = getattr(self, '_turn_counter', 0)
            changed = False
            for call in tool_trace:
                name = call.get("name", "")
                if not name or not call.get("success"):
                    continue
                # Skip pinned tools (they're always loaded)
                if name in ALWAYS_PINNED or name in COMMON_MCP_NAMES:
                    continue
                # General space guard
                if space.is_default and self.registry:
                    is_universal = False
                    for cap in self.registry.get_all():
                        if name in (cap.tools or []) and getattr(cap, "universal", False):
                            is_universal = True
                            break
                    if not is_universal:
                        catalog_entry = self._tool_catalog.get(name)
                        if catalog_entry and not catalog_entry.source.startswith("kernel"):
                            logger.info("TOOL_PROMOTE_SKIP: tool=%s space=%s reason=general_guard",
                                name, space_id)
                            continue
                # Compute schema tokens for this tool
                schema = self.registry.get_tool_schema(name)
                tokens = len(json.dumps(schema)) // 4 if schema else 0
                if name in aff:
                    aff[name]["last_turn"] = _turn
                    changed = True
                else:
                    aff[name] = {"last_turn": _turn, "tokens": tokens}
                    changed = True
                    logger.info("TOOL_PROMOTED: tool=%s space=%s reason=successful_use", name, space_id)
            if changed:
                await self.state.update_context_space(instance_id, space_id, {
                    "local_affordance_set": aff,
                })
        except Exception as exc:
            logger.warning("TOOL_PROMOTE: failed: %s", exc)

    async def _run_friction_observer(
        self, ctx: TurnContext, provider_errors: list[str] | None = None,
    ) -> None:
        """Run friction detection + behavioral pattern tracking post-turn.

        Non-blocking — failures are logged and swallowed.
        """
        if ctx.is_self_directed:
            return  # Self-directed turns are internal — no user-facing friction to detect

        # Standard friction detection
        signals: list = []
        try:
            surfaced_names = {t.get("name", "") for t in ctx.tools if t.get("name")}
            signals = await self._friction.observe(
                instance_id=ctx.instance_id,
                user_message=ctx.message.content or "",
                response_text=ctx.response_text,
                tool_trace=ctx.tool_calls_trace,
                surfaced_tool_names=surfaced_names,
                active_space_id=ctx.active_space_id,
                merged_count=ctx.merged_count,
                is_reactive=True,
                pref_detected=ctx.pref_detected,
                provider_errors=provider_errors,
                has_now_block_time=True,
            )
        except Exception as exc:
            logger.debug("FRICTION: observer failed: %s", exc)

        # SYSTEM_MALFUNCTION → informational whisper (not just a file)
        for sig in signals:
            if sig.signal_type in ("SCHEMA_ERROR_ON_PROVIDER", "PROVIDER_ERROR_REPEATED", "EMPTY_RESPONSE"):
                try:
                    from kernos.kernel.awareness import Whisper, generate_whisper_id
                    whisper = Whisper(
                        whisper_id=generate_whisper_id(),
                        insight_text=(
                            f"I hit a technical issue ({sig.signal_type.lower().replace('_', ' ')}). "
                            f"Everything else is working. I've logged the details."
                        ),
                        delivery_class="ambient",
                        source_space_id=ctx.active_space_id,
                        target_space_id=ctx.active_space_id,
                        supporting_evidence=sig.evidence[:3],
                        reasoning_trace=f"Friction observer detected {sig.signal_type}.",
                        knowledge_entry_id="",
                        foresight_signal=f"system_malfunction:{sig.signal_type}",
                        created_at=utc_now(),
                    )
                    await self.state.save_whisper(ctx.instance_id, whisper)
                    logger.info("FRICTION_WHISPER: class=SYSTEM_MALFUNCTION signal=%s whisper_id=%s",
                        sig.signal_type, whisper.whisper_id)
                except Exception as exc:
                    logger.debug("FRICTION_WHISPER: failed: %s", exc)

        # Behavioral pattern detection — track recurring corrections
        try:
            from kernos.kernel.behavioral_patterns import record_correction, build_proposal_whisper
            data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
            turn_number = ctx.soul.interaction_count if ctx.soul else 0
            pattern = record_correction(
                data_dir=data_dir,
                instance_id=ctx.instance_id,
                user_message=ctx.message.content or "",
                response_text=ctx.response_text,
                space_id=ctx.active_space_id,
                turn_number=turn_number,
            )
            if pattern:
                # Threshold just crossed — generate whisper
                proposal = build_proposal_whisper(pattern, ctx.active_space_id)
                classification = proposal.pop("classification", "behavioral")
                confidence = proposal.pop("confidence", "medium")
                pattern_id = proposal.pop("pattern_id", "")

                if classification == "workaround":
                    # Don't propose covenant — flag as system issue instead
                    logger.info(
                        "BEHAVIORAL_PROPOSAL: type=system_malfunction desc=%r space=%s "
                        "classification=workaround confidence=%s",
                        pattern.fingerprint[:60], ctx.active_space_id, confidence,
                    )
                    # Still create a whisper but framed as system issue
                    from kernos.kernel.awareness import Whisper
                    whisper = Whisper(**proposal)
                    await self.state.save_whisper(ctx.instance_id, whisper)
                else:
                    # behavioral or uncertain — propose covenant/procedure
                    from kernos.kernel.awareness import Whisper
                    whisper = Whisper(**proposal)
                    await self.state.save_whisper(ctx.instance_id, whisper)
                    # Mark pattern as proposal surfaced
                    from kernos.kernel.behavioral_patterns import load_patterns, save_patterns
                    patterns = load_patterns(data_dir, ctx.instance_id)
                    for p in patterns:
                        if p.pattern_id == pattern_id:
                            p.proposal_surfaced = True
                            break
                    save_patterns(data_dir, ctx.instance_id, patterns)
                    logger.info(
                        "BEHAVIORAL_PROPOSAL: type=%s desc=%r space=%s "
                        "classification=%s confidence=%s whisper_id=%s",
                        "covenant" if pattern.pattern_type != "workflow_correction" else "procedure",
                        pattern.fingerprint[:60], ctx.active_space_id,
                        classification, confidence, whisper.whisper_id,
                    )
        except Exception as exc:
            logger.debug("BEHAVIORAL_PATTERN: detection failed: %s", exc)

    # --- Selective knowledge injection helpers ---

    async def _shape_knowledge(
        self, candidates: list, message: NormalizedMessage, ctx: TurnContext,
    ) -> set[str]:
        """Use cheap LLM to select relevant knowledge entries for this turn.

        Returns set of entry IDs to inject. On failure, returns empty set
        (Tier 1 only fallback — NOT full Tier 3 dump).
        """
        try:
            candidate_lines = "\n".join(
                f"- [{e.id}] \"{e.content}\" ({e.lifecycle_archetype})"
                for e in candidates
            )
            recent_topic = self._get_recent_context_summary(ctx)

            logger.info(
                "SHAPE_INPUT: candidates=%d message=%s",
                len(candidates), (message.content or "")[:80],
            )
            result = await self.reasoning.complete_simple(
                system_prompt=(
                    "Select which user knowledge entries are relevant to "
                    "this conversation turn. Return ONLY the IDs of relevant "
                    "entries as a comma-separated list, or NONE if nothing "
                    "is relevant.\nExample: know_abc, know_def"
                ),
                user_content=(
                    f"User's message: \"{message.content[:200]}\"\n"
                    f"Recent topic: {recent_topic}\n\n"
                    f"Candidates:\n{candidate_lines}"
                ),
                max_tokens=128,
                prefer_cheap=True,
            )

            if not result or "NONE" in result.upper():
                return set()

            ids: set[str] = set()
            for token in result.replace(",", " ").split():
                token = token.strip()
                if token.startswith("know_"):
                    ids.add(token)
            logger.info("KNOWLEDGE_SHAPED: selected=%d/%d ids=%s",
                        len(ids), len(candidates), ",".join(sorted(ids)[:5]))
            return ids
        except Exception as exc:
            logger.warning("KNOWLEDGE_SHAPING_FAILED: %s — falling back to Tier 1 only", exc)
            return set()  # fail-safe: Tier 1 only, NOT full dump

    def _get_recent_context_summary(self, ctx: TurnContext) -> str:
        """Extract a brief summary of recent conversation for knowledge shaping."""
        if not ctx.messages:
            return "new conversation"
        recent = ctx.messages[-3:]
        texts = [m.get("content", "")[:100] for m in recent
                 if isinstance(m.get("content"), str)]
        return " | ".join(texts)[-200:] if texts else "general"

    async def _handle_reasoning_error(self, ctx: TurnContext, exc: Exception, user_msg: str) -> str:
        """Handle reasoning errors with event emission and user-facing message."""
        logger.error("Reasoning error for sender=%s: %s", ctx.message.sender, exc, exc_info=True)
        try:
            stage = "api_call" if not isinstance(exc, Exception) or isinstance(exc, (
                ReasoningTimeoutError, ReasoningConnectionError, ReasoningRateLimitError, ReasoningProviderError
            )) else "general"
            await emit_event(self.events, EventType.HANDLER_ERROR, ctx.instance_id, "handler",
                payload={"error_type": type(exc).__name__, "error_message": str(exc),
                         "conversation_id": ctx.conversation_id, "stage": stage})
        except Exception:
            pass
        return f"Something went wrong on my end — {user_msg}."
