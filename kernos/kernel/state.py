"""State Store — the kernel's knowledge model.

Current understanding of the user and their world. The query surface for
context assembly. Four domains: tenant profile, user knowledge, behavioral
contracts, conversation summaries.
"""
import hashlib
import logging
import math
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


def _content_hash(instance_id: str, subject: str, content: str) -> str:
    """SHA256[:16] of (instance_id|subject|content) for deduplication."""
    raw = f"{instance_id}|{subject.lower().strip()}|{content.lower().strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

from kernos.kernel.soul import Soul
from kernos.kernel.entities import EntityNode, IdentityEdge
from kernos.kernel.spaces import ContextSpace


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------


def _knowledge_id() -> str:
    return f"know_{uuid.uuid4().hex[:8]}"


def _rule_id() -> str:
    return f"rule_{uuid.uuid4().hex[:8]}"


def _entity_id() -> str:
    return f"ent_{uuid.uuid4().hex[:8]}"


def _pending_id() -> str:
    return f"pending_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Domain 1: Tenant Profile
# ---------------------------------------------------------------------------


@dataclass
class InstanceProfile:
    """Enriched tenant record. Richer than InstanceStore's tenant.json."""

    instance_id: str
    status: str                     # "active", "suspended", "cancelled"
    created_at: str                 # ISO timestamp
    platforms: dict[str, Any] = field(default_factory=dict)
    preferences: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, str] = field(default_factory=dict)
    model_config: dict[str, Any] = field(default_factory=dict)
    last_active_space_id: str = ""   # Tracks active space across messages
    permission_overrides: dict[str, str] = field(default_factory=dict)
    # Maps capability_name → "ask" | "always-allow"
    # Default (not in dict) = "ask" (gate fires)
    # "always-allow" → bypass gate for all write tools in this capability
    developer_mode: bool = False


@dataclass
class ProjectState:
    """Durable binding for a long-horizon project.

    A project is not a separate substrate: it binds an existing
    ContextSpace, canvas, knowledge tags, and one scheduler trigger.
    """

    project_id: str
    instance_id: str
    space_id: str
    canvas_id: str
    name: str
    owner_member_id: str = ""
    lifecycle_state: str = "active"
    created_at: str = ""
    updated_at: str = ""
    last_activity_at: str = ""
    checkin_trigger_id: str = ""
    next_checkin_at: str = ""
    completed_at: str = ""
    completion_summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Domain 2: User Knowledge
# ---------------------------------------------------------------------------


@dataclass
class KnowledgeEntry:
    """A piece of knowledge about the user or their world."""

    # --- Identity ---
    id: str                          # "know_{uuid8}"
    instance_id: str
    category: str                    # "entity", "fact", "preference", "pattern"
    subject: str                     # What this is about
    content: str                     # The knowledge text
    confidence: str                  # "stated", "inferred", "observed"
    source_event_id: str             # Provenance — links to the event that created this
    source_description: str          # Human-readable provenance
    created_at: str
    last_referenced: str
    tags: list[str]

    # --- Status ---
    active: bool = True              # False = archived (never deleted)

    # --- Provenance (extended) ---
    supersedes: str = ""             # ID of the entry this one replaces (provenance chain)
    content_hash: str = ""           # SHA256[:16] of (instance_id|subject|content) for dedup
    entity_node_id: str = ""         # Link to EntityNode (populated by entity resolution)

    # --- Legacy compat: durability is retired, kept for reading old JSON ---
    # New entries leave this empty. lifecycle_archetype is the authoritative field.
    durability: str = ""             # deprecated — "permanent"|"session"|"expires_at:<ISO>"

    # --- Content lifecycle ---
    lifecycle_archetype: str = "structural"
    # "identity" | "structural" | "habitual" | "contextual" | "ephemeral"

    # --- Temporal (bitemporal from Graphiti) ---
    expired_at: str = ""             # When the kernel invalidated this (transaction time)
    valid_at: str = ""               # When this became true in reality (valid time)
    invalid_at: str = ""             # When this stopped being true (valid time)

    # --- Strength (dual-strength from Bjork/FSRS) ---
    storage_strength: float = 1.0    # How well-established. Monotonically increasing.
    last_reinforced_at: str = ""     # When user last mentioned/confirmed (not last_referenced)
    reinforcement_count: int = 1     # How many times confirmed across conversations

    # --- Foresight (from EverMemOS) ---
    foresight_signal: str = ""       # Forward-looking implication, if any
    foresight_expires: str = ""      # When the foresight signal becomes irrelevant

    # --- Classification ---
    context_space: str = ""          # Reserved for context spaces (empty = global)
    salience: float = 0.5            # Initial importance weight (0.0-1.0)

    # --- Multi-member foundation (V1: defaults, V2: populated) ---
    owner_member_id: str = ""        # Who contributed this. Empty = instance owner.
    sensitivity: str = "open"        # "open" | "contextual" | "personal" | "classified"
    visible_to: list[str] | None = None  # None = follow sensitivity default. List = only these member_ids.


# ---------------------------------------------------------------------------
# Retrieval strength — computed at read time, never stored
# ---------------------------------------------------------------------------

ARCHETYPE_STABILITY: dict[str, int] = {
    "identity": 730,      # ~2 years
    "structural": 120,    # ~4 months
    "habitual": 45,       # ~6 weeks
    "contextual": 14,     # ~2 weeks
    "ephemeral": 1,       # ~1 day
}

# FSRS-6 parameter (default — tunable from usage data in Phase 3)
W20 = 0.5


def compute_retrieval_strength(entry: KnowledgeEntry, now_iso: str) -> float:
    """Compute current retrieval strength using FSRS-6 power-law decay.

    Called at read time, not stored. Keeps write path clean.
    Returns 0.0-1.0 where 1.0 = fully accessible, 0.0 = effectively forgotten.
    """
    if not entry.last_reinforced_at:
        return 1.0  # New entry, no decay yet

    from datetime import datetime, timezone
    last = datetime.fromisoformat(entry.last_reinforced_at)
    now = datetime.fromisoformat(now_iso)
    days_since = max((now - last).total_seconds() / 86400, 0)

    if days_since == 0:
        return 1.0

    base_stability = ARCHETYPE_STABILITY.get(entry.lifecycle_archetype, 120)
    effective_stability = base_stability * (1 + 0.1 * math.log1p(entry.storage_strength))

    factor = 0.9 ** (-1 / W20) - 1
    return (1 + factor * days_since / effective_stability) ** (-W20)


# ---------------------------------------------------------------------------
# Domain 3: Behavioral Contracts → Covenant
# ---------------------------------------------------------------------------


@dataclass
class EphemeralPermission:
    """A one-off Messenger permission granted by the disclosing member in
    response to a refer-flow whisper.

    Consulted by the Messenger as a judgment input alongside covenants; when
    ``granted`` is True, Messenger can treat the topic as cleared for this
    specific requester for the lifetime of the entry. When False, it's an
    explicit denial for this ask that shouldn't be relitigated soon.

    TTL is 24h from creation (V1 contract; not configurable). Entries past
    their TTL are filtered at read time. No reconciliation pass; no refer-id;
    no correlation with the original whisper. Kept deliberately flat —
    durable refer-state is a V2 consideration gated on empirical need.
    """

    id: str                       # "eph_{uuid8}"
    instance_id: str
    disclosing_member_id: str     # Who granted the permission
    requesting_member_id: str     # Who can use it
    topic: str                    # User-phrased verbatim ("therapy", "dad's surgery")
    granted: bool                 # True = permission; False = explicit denial
    created_at: str
    expires_at: str               # ISO timestamp, 24h after created_at


@dataclass
class CovenantRule:
    """A behavioral rule in the Covenant — the living contract between agent and user."""

    # --- Preserved from ContractRule (backwards compatible) ---
    id: str                          # "rule_{uuid8}"
    instance_id: str
    capability: str                  # "calendar", "email", "general"
    rule_type: str                   # "must", "must_not", "preference", "escalation"
    description: str                 # Human-readable — what the agent reads and user sees
    active: bool
    source: str                      # "default", "user_stated", "evolved"
    source_event_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    context_space: str | None = None  # None = global

    # --- Covenant layer ---
    layer: str = "principle"         # "principle" | "practice"

    # --- Action class targeting ---
    action_class: str = ""           # "email.delete.spam", "calendar.schedule.business_hours"
    trigger_tool: str = ""           # MCP tool name. Empty = all tools in capability.

    # --- Enforcement ---
    enforcement_tier: str = "confirm"   # "silent" | "notify" | "confirm" | "block"
    fallback_action: str = "ask_user"   # "ask_user" | "stage_as_draft" | "log_and_proceed" | "block_with_explanation"
    escalation_message: str = ""        # "Sending to {recipient} — confirm?"

    # --- Graduation state (meaningful for Practices only) ---
    graduation_positive_signals: int = 0
    graduation_last_rejection: str = ""     # ISO timestamp, empty = never rejected
    graduation_eligible: bool = False
    graduation_threshold: int = 25
    graduation_tier_locked_until: str = ""  # Rate limit

    # --- Versioning ---
    supersedes: str = ""
    superseded_by: str = ""              # rule_id that replaced this, "user_removed", or "" (active)
    version: int = 1

    # --- Preference linkage (Phase 6A) ---
    source_preference_id: str = ""       # Preference ID that generated this rule, or ""

    # --- Selective injection (Improvement Loop Tier 1) ---
    tier: str = ""  # "pinned" | "situational" — empty triggers migration on load

    # --- Member scoping (Multi-Member Pass 1) ---
    member_id: str = ""  # Empty = instance-level (applies to all). Non-empty = per-member.

    # --- Messenger cross-member scoping (MESSENGER-COHORT) ---
    # Optional, user-phrased, no taxonomy. Populated by the covenant-creation
    # path when the user's directive is recognizably scoped ("don't tell mom
    # about therapy"). Empty means this covenant is not cross-member-scoped;
    # Messenger still sees it but doesn't get a direct topic/target anchor.
    topic: str = ""   # Verbatim user phrasing: "therapy", "the breakup", "dad's surgery".
    target: str = ""  # Either a resolved member_id OR a relationship-profile identifier
                      # (e.g. "by-permission-members"). Resolution is Python, deterministic.

    # --- Reserved for future phases ---
    agent_id: str = ""
    precondition: str = ""
    workspace_id: str = ""


# Backwards-compatibility alias — ContractRule is CovenantRule
ContractRule = CovenantRule


def _enforcement_tier_for(rule_type: str) -> str:
    """Map legacy rule_type to enforcement_tier for new default rules."""
    return {
        "spirit": "silent",
        "must_not": "confirm",
        "must": "confirm",
        "preference": "silent",
        "escalation": "confirm",
    }.get(rule_type, "confirm")


def classify_covenant_tier(rule_type: str, source: str) -> str:
    """Classify a covenant as pinned (always loaded) or situational (loaded when relevant).

    Pinned: safety rules, escalation, system defaults — always in context.
    Situational: preferences and user-stated rules — loaded by MessageAnalyzer.
    """
    if rule_type == "spirit":
        return "pinned"
    if rule_type == "must_not":
        return "pinned"
    if rule_type == "escalation":
        return "pinned"
    if source in ("default", "system"):
        return "pinned"
    return "situational"


# POSTURE-SEEDED-COVENANTS-V1 (2026-05-22): the canonical rule
# descriptions. Each text is referenced by name from the profile
# lists below so the wording lives in one place.
_DESC_SPIRIT = "You are making someone's life genuinely better. Your energy in the work you do with them, the warmth you bring to the relationship, matters. You are a signal of good judgment to the path you two will embark. When in doubt, your realness and helpfulness will guide you. You got this."
_DESC_THIRD_PARTY_CONTACTS = "Never send messages to third-party CONTACTS (external humans via SMS/email/social) unless the owner initiated the request. When the owner has initiated and the intent is clear, send it — hesitation past that point is friction, not caution. This does NOT apply to send_relational_message, which routes to another MEMBER's agent through the internal permission-matrix dispatcher — that's an intra-system message, not a third-party contact."
_DESC_DELETE_FILES = "Never delete the user's files, entries, or records unless they asked you to. When they ask, do it — their request is the confirmation."
_DESC_SHARER_INFO = "Information shared with you belongs to whoever shared it. Use good judgment about what's appropriate to pass along — routine, expected information can flow naturally between people who know each other. But when someone shares something sensitive, confidential, or clearly meant for you alone, don't disclose it to others without the sharer's consent — even to people they know well."
_DESC_CONFIRM_SPENDING = "Confirm before spending money unless the owner specified the amount and recipient in their request. When the spend is confirmed, complete it — the confirmation is the authorization event, don't re-confirm."
_DESC_SHOW_DRAFTS = "Show drafts before sending to third parties on open channels (SMS, email, social). Once the draft is approved, send it — approval is the authorization event. No draft needed for owner-directed channel delivery, and no draft needed for send_relational_message — that tool routes to another member's AGENT through the permission-matrix dispatcher, not to the person directly, and the receiving agent applies its own judgment."
_DESC_MATCH_DEPTH = "Match the depth of your response to what the moment needs. Don't over-explain simple things or under-deliver on complex ones."
_DESC_SELF_UPDATE = "When you see a substrate event indicating Kernos updated itself with new code, mention the update naturally in conversation and summarize what changed in your own words. The user wants to be kept in the loop on updates. If they ask you to stop telling them, or only mention certain kinds of updates, archive or revise this rule accordingly."
_DESC_ESCALATION = "When a request is genuinely ambiguous AND involves irreversible consequences, money, or third-party impact, clarify before acting. If the request is clear, act."

# Profile MINIMAL (DEFAULT): 4 rules. The behavior-neutral seed
# the operator gets out of the box. Spirit + privacy invariant +
# delete-consent invariant + escalation.
# 2026-05-22: self-update notice removed from minimal at owner
# request — the original INFORMING-V1 motivation (operator
# wanting to be told about substrate edits) is opt-in territory,
# not a behavior-neutral default. Strict still carries it for
# pre-POSTURE parity.
_PROFILE_MINIMAL = [
    ("spirit", "general", _DESC_SPIRIT),
    ("must_not", "general", _DESC_SHARER_INFO),
    ("must_not", "general", _DESC_DELETE_FILES),
    ("escalation", "general", _DESC_ESCALATION),
]

# Profile STANDARD adds the situational must rules around money +
# 3rd-party drafts. 6 rules total.
_PROFILE_STANDARD_ADDS = [
    ("must", "general", _DESC_CONFIRM_SPENDING),
    ("must", "general", _DESC_SHOW_DRAFTS),
]

# Profile STRICT: full 9 rules in their ORIGINAL pre-change order
# so operators who set strict get byte-for-byte parity with the
# pre-POSTURE-V1 behavior (no display/introspection drift).
_PROFILE_STRICT_ORDERED = [
    ("spirit", "general", _DESC_SPIRIT),
    ("must_not", "general", _DESC_THIRD_PARTY_CONTACTS),
    ("must_not", "general", _DESC_DELETE_FILES),
    ("must_not", "general", _DESC_SHARER_INFO),
    ("must", "general", _DESC_CONFIRM_SPENDING),
    ("must", "general", _DESC_SHOW_DRAFTS),
    ("preference", "general", _DESC_MATCH_DEPTH),
    ("preference", "general", _DESC_SELF_UPDATE),
    ("escalation", "general", _DESC_ESCALATION),
]


def _resolve_posture_profile(profile_override: str = "") -> str:
    """Read + normalize the posture profile.

    Resolution order:
      1. ``profile_override`` if non-empty (from persisted
         ``instance_posture`` row per POSTURE-CONFIGURATION-V1).
      2. ``KERNOS_POSTURE_PROFILE`` env var.
      3. ``minimal`` hardcoded default.

    Invalid value at any layer → ``strict`` + ERROR log
    (fail-loud + over-seed is safer than silent under-seed
    since existing instances aren't auto-rebased).
    """
    raw = (profile_override or "").strip().lower()
    source = "persisted"
    if not raw:
        raw = os.environ.get("KERNOS_POSTURE_PROFILE", "").strip().lower()
        source = "env"
    if not raw:
        return "minimal"
    if raw in ("minimal", "standard", "strict"):
        return raw
    logger.error(
        "posture profile %r (source=%s) unknown; falling back to "
        "'strict' (fail-loud + over-seed safer than silent "
        "under-seed). Valid: minimal|standard|strict.",
        raw, source,
    )
    return "strict"


def default_covenant_rules(
    instance_id: str, now: str, profile_override: str = "",
) -> list[CovenantRule]:
    """POSTURE-SEEDED-COVENANTS-V1 (2026-05-22): the rules every
    new tenant starts with, profile-selectable via
    ``KERNOS_POSTURE_PROFILE`` (minimal | standard | strict).

    POSTURE-CONFIGURATION-V1 (2026-05-22): ``profile_override``
    lets callers pass an operator-persisted posture profile that
    wins over env. Empty string → fall through to env-based
    resolution (preserves prior behavior).

    Strict reproduces the pre-POSTURE behavior byte-for-byte
    including rule order. Minimal is the default — behavior-
    neutral out of the box.
    """
    profile = _resolve_posture_profile(profile_override)
    if profile == "strict":
        rules = list(_PROFILE_STRICT_ORDERED)
    else:
        rules = list(_PROFILE_MINIMAL)
        if profile == "standard":
            rules.extend(_PROFILE_STANDARD_ADDS)
    logger.info(
        "DEFAULT_COVENANTS_SEEDED instance=%s profile=%s rule_count=%d",
        instance_id, profile, len(rules),
    )
    return [
        CovenantRule(
            id=_rule_id(),
            instance_id=instance_id,
            capability=cap,
            rule_type=rt,
            description=desc,
            active=True,
            source="default",
            source_event_id=None,
            created_at=now,
            updated_at=now,
            enforcement_tier=_enforcement_tier_for(rt),
            tier=classify_covenant_tier(rt, "default"),
        )
        for rt, cap, desc in rules
    ]


# Backwards-compatibility alias
default_contract_rules = default_covenant_rules


# ---------------------------------------------------------------------------
# Domain 3b: Pending Actions (for Dispatch Interceptor, Phase 2B)
# ---------------------------------------------------------------------------


@dataclass
class PendingAction:
    """A tool call staged for user confirmation by the Dispatch Interceptor."""

    id: str                     # "pending_{uuid8}"
    instance_id: str
    rule_id: str                # Which CovenantRule triggered this
    tool_name: str
    tool_arguments: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)  # Enough state to resume
    created_at: str = ""
    expires_at: str = ""        # Default: 1 hour from creation
    status: str = "pending"     # "pending" | "approved" | "rejected" | "expired"
    conversation_id: str = ""
    batch_id: str = ""          # Groups tool calls from the same reasoning turn


# ---------------------------------------------------------------------------
# Domain 4: Conversation Summaries
# ---------------------------------------------------------------------------


@dataclass
class ConversationSummary:
    """Lightweight metadata about a conversation. Not the messages themselves."""

    instance_id: str
    conversation_id: str
    platform: str
    message_count: int
    first_message_at: str
    last_message_at: str
    topics: list[str] = field(default_factory=list)
    active: bool = True


# ---------------------------------------------------------------------------
# Domain 5: First-Class Preferences
# ---------------------------------------------------------------------------


def generate_preference_id() -> str:
    """Generate a unique preference ID."""
    return f"pref_{uuid.uuid4().hex[:8]}"


@dataclass
class Preference:
    """A first-class user preference — upstream of covenants, triggers, and knowledge.

    Preferences capture WHAT the user wants to remain true. They have different
    lifecycle, supersession, and introspection needs than KnowledgeEntry.
    """

    # Identity
    id: str                       # "pref_{uuid8}"
    instance_id: str

    # Intent
    intent: str                   # Original user language that created this

    # Classification
    category: str                 # "notification", "behavior", "format", "access", "schedule"
    subject: str                  # What it's about: "calendar_events", "email", "responses", etc.
    action: str                   # "notify", "always_do", "never_do", "prefer", "schedule"
    parameters: dict = field(default_factory=dict)  # Specific values: lead_time, channel, etc.

    # Scope
    scope: str = "global"         # "global" or space_id
    context_space: str = ""       # If scope is space-specific, which space

    # Lifecycle
    status: str = "active"        # "active", "superseded", "revoked"
    supersedes: str = ""          # Preference ID this replaces (if any)
    superseded_by: str = ""       # Preference ID that replaced this (if any)

    # Provenance
    created_at: str = ""          # ISO 8601 UTC
    updated_at: str = ""          # Last modification
    source_turn_id: str = ""      # Conversation turn / event that created this
    source_knowledge_id: str = "" # If migrated from KnowledgeEntry, the original ID

    # Derived artifacts
    derived_trigger_ids: list[str] = field(default_factory=list)
    derived_covenant_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# StateStore interface
# ---------------------------------------------------------------------------


class StateStore(ABC):
    """The kernel's knowledge model. Current understanding of user and world."""

    # Soul
    @abstractmethod
    async def get_soul(self, instance_id: str) -> Soul | None: ...

    @abstractmethod
    async def save_soul(self, soul: Soul, *, source: str = "", trigger: str = "") -> None: ...

    # Tenant Profile
    @abstractmethod
    async def get_instance_profile(self, instance_id: str) -> InstanceProfile | None: ...

    @abstractmethod
    async def save_instance_profile(self, instance_id: str, profile: InstanceProfile) -> None: ...

    # Knowledge
    @abstractmethod
    async def add_knowledge(self, entry: KnowledgeEntry) -> None: ...

    @abstractmethod
    async def query_knowledge(
        self,
        instance_id: str,
        subject: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        active_only: bool = True,
        limit: int = 20,
        member_id: str = "",
    ) -> list[KnowledgeEntry]: ...

    @abstractmethod
    async def update_knowledge(self, instance_id: str, entry_id: str, updates: dict) -> None: ...

    @abstractmethod
    async def save_knowledge_entry(self, entry: KnowledgeEntry) -> None:
        """Upsert a KnowledgeEntry by ID (write or overwrite)."""
        ...

    @abstractmethod
    async def get_knowledge_entry(self, instance_id: str, entry_id: str) -> "KnowledgeEntry | None":
        """Get a single KnowledgeEntry by ID. Returns None if not found."""
        ...

    @abstractmethod
    async def get_knowledge_hashes(self, instance_id: str) -> set[str]:
        """Return set of content_hash values for all active entries. O(1) dedup check."""
        ...

    @abstractmethod
    async def get_knowledge_by_hash(
        self, instance_id: str, content_hash: str
    ) -> "KnowledgeEntry | None":
        """Find an active entry by content_hash. Returns None if not found."""
        ...

    # Behavioral Contracts (method names preserved for backwards compatibility)
    @abstractmethod
    async def get_contract_rules(
        self,
        instance_id: str,
        capability: str | None = None,
        rule_type: str | None = None,
        active_only: bool = True,
    ) -> list[CovenantRule]: ...

    @abstractmethod
    async def query_covenant_rules(
        self,
        instance_id: str,
        capability: str | None = None,
        context_space_scope: list[str | None] | None = None,
        active_only: bool = True,
    ) -> list[CovenantRule]:
        """Query covenant rules with optional context space scope filtering.

        context_space_scope: [space_id, None] loads space-scoped + global rules.
        If None (not provided), returns all rules (used by CLI/admin).
        """
        ...

    @abstractmethod
    async def add_contract_rule(self, rule: CovenantRule) -> None: ...

    @abstractmethod
    async def update_contract_rule(self, instance_id: str, rule_id: str, updates: dict) -> None: ...

    # Entity Resolution (Phase 2A will implement real logic)
    @abstractmethod
    async def save_entity_node(self, node: EntityNode) -> None: ...

    @abstractmethod
    async def get_entity_node(self, instance_id: str, entity_id: str) -> EntityNode | None: ...

    @abstractmethod
    async def query_entity_nodes(
        self,
        instance_id: str,
        name: str | None = None,
        entity_type: str | None = None,
        active_only: bool = True,
    ) -> list[EntityNode]: ...

    @abstractmethod
    async def save_identity_edge(self, instance_id: str, edge: IdentityEdge) -> None: ...

    @abstractmethod
    async def query_identity_edges(
        self, instance_id: str, entity_id: str
    ) -> list[IdentityEdge]: ...

    # Pending Actions (Phase 2B Dispatch Interceptor)
    @abstractmethod
    async def save_pending_action(self, action: PendingAction) -> None: ...

    @abstractmethod
    async def get_pending_actions(
        self, instance_id: str, status: str = "pending"
    ) -> list[PendingAction]: ...

    @abstractmethod
    async def update_pending_action(
        self, instance_id: str, action_id: str, updates: dict
    ) -> None: ...

    # Context Spaces (Phase 2A routing builds on top of this)
    @abstractmethod
    async def save_context_space(self, space: ContextSpace) -> None: ...

    @abstractmethod
    async def get_context_space(
        self, instance_id: str, space_id: str
    ) -> ContextSpace | None: ...

    @abstractmethod
    async def list_context_spaces(self, instance_id: str) -> list[ContextSpace]: ...

    @abstractmethod
    async def update_context_space(
        self, instance_id: str, space_id: str, updates: dict
    ) -> None: ...

    # Long-horizon projects (LONG-HORIZON-PROJECT-V1). Default
    # implementations keep legacy stores constructible; SqliteStateStore is
    # the production implementation.
    async def insert_project_state(self, project: ProjectState) -> None:
        raise NotImplementedError

    async def create_project_state(self, project: ProjectState) -> None:
        await self.insert_project_state(project)

    async def get_project_state(
        self, instance_id: str, project_id: str
    ) -> ProjectState | None:
        raise NotImplementedError

    async def get_project_state_by_space(
        self, instance_id: str, space_id: str,
        lifecycle_state: str | None = None,
    ) -> ProjectState | None:
        raise NotImplementedError

    async def list_active_projects(
        self, instance_id: str, owner_member_id: str = "",
    ) -> list[ProjectState]:
        raise NotImplementedError

    async def mark_project_completed(
        self,
        instance_id: str,
        project_id: str,
        completion_summary: str = "",
        completed_at: str = "",
    ) -> ProjectState | None:
        raise NotImplementedError

    async def update_project_activity(
        self,
        instance_id: str,
        project_id: str,
        *,
        last_activity_at: str = "",
        checkin_trigger_id: str | None = None,
        next_checkin_at: str | None = None,
    ) -> ProjectState | None:
        raise NotImplementedError

    async def list_child_spaces(self, instance_id: str, parent_id: str) -> list[ContextSpace]:
        """Return all spaces with parent_id matching the given space."""
        all_spaces = await self.list_context_spaces(instance_id)
        return [s for s in all_spaces if s.parent_id == parent_id and s.status == "active"]

    # Space notices (cross-domain signals)
    async def append_space_notice(
        self, instance_id: str, space_id: str, text: str,
        source: str = "", notice_type: str = "cross_domain",
    ) -> None:
        """Append a notice to a space's pending notice queue."""
        ...

    async def drain_space_notices(self, instance_id: str, space_id: str) -> list[dict]:
        """Return and clear all pending notices for a space."""
        ...


    # Knowledge Foresight Query (Phase 3C: Proactive Awareness)
    @abstractmethod
    async def query_knowledge_by_foresight(
        self,
        instance_id: str,
        expires_before: str,
        expires_after: str = "",
        space_id: str = "",
    ) -> list[KnowledgeEntry]:
        """Return knowledge entries with active foresight signals expiring in the given window.

        An entry is included if:
        - foresight_signal is non-empty
        - foresight_expires is non-empty
        - foresight_expires falls within (expires_after, expires_before]
        - The entry is active
        """
        ...

    # Ephemeral Permissions (MESSENGER-COHORT refer-flow, 24h TTL)
    # Default empty implementation so stores that don't yet implement the
    # surface return a safe no-op (empty list, silent save). Concrete stores
    # are encouraged to override. The Messenger's flow degrades gracefully
    # when ephemeral permissions aren't available — the covenant path still
    # works, and absent ephemeral state the next ask simply re-refers.
    async def save_ephemeral_permission(
        self, perm: "EphemeralPermission",
    ) -> None:
        """Persist an ephemeral permission. Idempotent by id."""
        return None

    async def list_ephemeral_permissions(
        self,
        instance_id: str,
        *,
        disclosing_member_id: str = "",
        requesting_member_id: str = "",
    ) -> "list[EphemeralPermission]":
        """List unexpired ephemeral permissions, optionally filtered by pair."""
        return []

    async def expire_ephemeral_permissions(self, instance_id: str) -> int:
        """Remove expired ephemeral permissions. Returns count removed."""
        return 0

    # Whispers and Suppressions (Phase 3C: Proactive Awareness)
    @abstractmethod
    async def save_whisper(self, instance_id: str, whisper: "Whisper") -> None:
        """Save a pending whisper to the queue."""
        ...

    @abstractmethod
    async def get_pending_whispers(self, instance_id: str) -> "list[Whisper]":
        """Get all unsurfaced whispers for an instance."""
        ...

    @abstractmethod
    async def mark_whisper_surfaced(self, instance_id: str, whisper_id: str) -> None:
        """Mark a whisper as surfaced (set surfaced_at)."""
        ...

    @abstractmethod
    async def delete_whisper(self, instance_id: str, whisper_id: str) -> None:
        """Delete a whisper from the pending queue. Used for queue bounding."""
        ...

    @abstractmethod
    async def save_suppression(self, instance_id: str, entry: "SuppressionEntry") -> None:
        """Save a suppression entry."""
        ...

    @abstractmethod
    async def get_suppressions(
        self,
        instance_id: str,
        knowledge_entry_id: str = "",
        whisper_id: str = "",
        foresight_signal: str = "",
    ) -> "list[SuppressionEntry]":
        """Get suppression entries, optionally filtered."""
        ...

    @abstractmethod
    async def delete_suppression(self, instance_id: str, whisper_id: str) -> None:
        """Delete a suppression entry. Used when knowledge updates clear a suppression."""
        ...

    # Conversation Summaries
    @abstractmethod
    async def get_conversation_summary(
        self, instance_id: str, conversation_id: str
    ) -> ConversationSummary | None: ...

    @abstractmethod
    async def save_conversation_summary(self, summary: ConversationSummary) -> None: ...

    @abstractmethod
    async def list_conversations(
        self, instance_id: str, active_only: bool = True, limit: int = 20
    ) -> list[ConversationSummary]: ...

    # Preferences (Phase 6A)
    @abstractmethod
    async def add_preference(self, pref: Preference) -> None:
        """Persist a new preference."""
        ...

    @abstractmethod
    async def save_preference(self, pref: Preference) -> None:
        """Upsert a preference by ID."""
        ...

    @abstractmethod
    async def get_preference(self, instance_id: str, pref_id: str) -> Preference | None:
        """Get a single preference by ID."""
        ...

    @abstractmethod
    async def query_preferences(
        self,
        instance_id: str,
        status: str = "",
        subject: str = "",
        category: str = "",
        scope: str = "",
        active_only: bool = True,
    ) -> list[Preference]:
        """Query preferences with optional filters. active_only filters status='active'."""
        ...

    # --- Relational messaging (RELATIONAL-MESSAGING v5) ---

    @abstractmethod
    async def add_relational_message(self, message: "RelationalMessage") -> None:
        """Store a new relational-message envelope."""
        ...

    @abstractmethod
    async def get_relational_message(
        self, instance_id: str, message_id: str,
    ) -> "RelationalMessage | None":
        """Fetch a single envelope by id. Returns None if not found."""
        ...

    @abstractmethod
    async def query_relational_messages(
        self,
        instance_id: str,
        addressee_member_id: str = "",
        origin_member_id: str = "",
        states: list[str] | None = None,
        conversation_id: str = "",
        limit: int = 200,
    ) -> list["RelationalMessage"]:
        """Query envelopes by recipient / origin / state / thread id."""
        ...

    @abstractmethod
    async def transition_relational_message_state(
        self,
        instance_id: str,
        message_id: str,
        from_state: str,
        to_state: str,
        updates: dict | None = None,
    ) -> bool:
        """Atomic compare-and-swap on envelope.state.

        Returns True iff the envelope was in `from_state` at the moment of
        update. Returns False if the state had already advanced (another
        path won the race) — caller should treat this as "someone else
        handled it, skip" rather than an error.

        IMPLEMENTATIONS MUST use the backing store's atomic primitives:
        SQLite via `UPDATE ... WHERE state=?` with a rowcount check;
        JSON via read-check-write under the existing filelock. Do NOT
        implement as separate read-then-update at the call site.
        """
        ...

    @abstractmethod
    async def delete_relational_message(
        self, instance_id: str, message_id: str,
    ) -> None:
        """Remove an envelope by id (used by tests and expiration sweep)."""
        ...
