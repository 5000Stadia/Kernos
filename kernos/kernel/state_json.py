"""JSON-on-disk implementation of StateStore.

Files per-instance under {data_dir}/{instance_id}/state/:
  profile.json         — InstanceProfile (single object)
  knowledge.json       — list of KnowledgeEntry
  contracts.json       — list of CovenantRule
  conversations.json   — list of ConversationSummary
  soul.json            — Soul (single object)
  entities.json        — list of EntityNode
  identity_edges.json  — list of IdentityEdge (per-instance, Phase 2A)
  pending_actions.json — list of PendingAction
  preferences.json     — list of Preference (Phase 6A)

The interface abstracts the backend — a future MemOS or database integration
changes only this file.
"""
import json
import logging
from dataclasses import asdict
from pathlib import Path

from filelock import FileLock

from kernos.kernel.entities import EntityNode, IdentityEdge
from kernos.kernel.soul import Soul
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import (
    ConversationSummary,
    CovenantRule,
    KnowledgeEntry,
    PendingAction,
    Preference,
    StateStore,
    InstanceProfile,
)

logger = logging.getLogger(__name__)


from kernos.utils import _safe_name


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------


def _durability_to_archetype(durability: str) -> str:
    """Map legacy durability string to lifecycle_archetype."""
    if not durability or durability == "permanent":
        return "structural"
    if durability == "session":
        return "ephemeral"
    if durability.startswith("expires_at:"):
        return "contextual"
    return "structural"


_RULE_TYPE_TO_ENFORCEMENT_TIER = {
    "must_not": "confirm",
    "must": "confirm",
    "preference": "silent",
    "escalation": "confirm",
}


def _load_covenant_rule(d: dict) -> CovenantRule:
    """Load CovenantRule from dict, migrating fields from older formats.

    Old contracts.json files may lack enforcement_tier or superseded_by.
    """
    data = dict(d)
    if "enforcement_tier" not in data:
        data["enforcement_tier"] = _RULE_TYPE_TO_ENFORCEMENT_TIER.get(
            data.get("rule_type", ""), "confirm"
        )
    if "superseded_by" not in data:
        data["superseded_by"] = ""
    if "tier" not in data:
        from kernos.kernel.state import classify_covenant_tier
        data["tier"] = classify_covenant_tier(data.get("rule_type", ""), data.get("source", ""))
    return CovenantRule(**data)


_CONTEXT_SPACE_FIELDS = {
    "id", "instance_id", "name", "description", "space_type", "status",
    "posture", "model_preference", "created_at", "last_active_at", "is_default",
    "max_file_size_bytes", "max_space_bytes", "active_tools",
    "parent_id", "aliases", "depth",
    "renamed_from", "renamed_at",
    "local_affordance_set", "last_catalog_version",
}


def _load_context_space(d: dict) -> ContextSpace:
    """Load ContextSpace from dict, silently ignoring unknown/removed fields.

    Old spaces.json files may have routing_keywords, routing_aliases,
    routing_entity_ids, suggestion_suppressed_until — all removed in v2.
    """
    filtered = {k: v for k, v in d.items() if k in _CONTEXT_SPACE_FIELDS}
    if "active_tools" not in filtered:
        filtered["active_tools"] = []
    if "aliases" not in filtered:
        filtered["aliases"] = []
    if "local_affordance_set" not in filtered:
        filtered["local_affordance_set"] = {}
    elif isinstance(filtered.get("local_affordance_set"), list):
        # Migrate from old list[str] format to dict with metadata
        filtered["local_affordance_set"] = {
            name: {"last_turn": 0, "tokens": 0} for name in filtered["local_affordance_set"]
        }
    # Migrate legacy "daily" space_type to "general"
    if filtered.get("space_type") == "daily":
        filtered["space_type"] = "general"
    return ContextSpace(**filtered)


def _load_knowledge_entry(d: dict) -> KnowledgeEntry:
    """Load KnowledgeEntry from dict, applying migrations for missing fields.

    Handles: durability → lifecycle_archetype, multi-member fields (V1 defaults),
    and silently drops unknown keys (e.g. updated_at written by fact_harvest).
    """
    data = dict(d)
    if not data.get("lifecycle_archetype"):
        durability = data.get("durability", "")
        data["lifecycle_archetype"] = _durability_to_archetype(durability)
        if (
            durability.startswith("expires_at:")
            and not data.get("foresight_expires")
        ):
            data["foresight_expires"] = durability[len("expires_at:"):]
    # Multi-member foundation defaults (V1)
    if "owner_member_id" not in data:
        data["owner_member_id"] = ""
    if "sensitivity" not in data:
        data["sensitivity"] = "open"
    if "visible_to" not in data:
        data["visible_to"] = None
    # Strip unknown fields (forward-compat: new fields written by harvest/projectors)
    known_fields = {f for f in KnowledgeEntry.__dataclass_fields__}
    data = {k: v for k, v in data.items() if k in known_fields}
    return KnowledgeEntry(**data)


# ---------------------------------------------------------------------------
# JsonStateStore
# ---------------------------------------------------------------------------


class JsonStateStore(StateStore):
    """JSON file-backed state store."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._preference_migration_done: set[str] = set()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _state_dir(self, instance_id: str) -> Path:
        return self._data_dir / _safe_name(instance_id) / "state"

    def _read_json(self, path: Path, default):
        if not path.exists():
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_json(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = str(path) + ".lock"
        with FileLock(lock_path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    # -----------------------------------------------------------------------
    # Soul
    # -----------------------------------------------------------------------

    async def get_soul(self, instance_id: str) -> Soul | None:
        path = self._state_dir(instance_id) / "soul.json"
        data = self._read_json(path, None)
        if data is None:
            return None
        # Migration: no backfill for agent_name/emoji — these are per-member now.
        # Empty string means "not yet named" (pre-hatching).
        migrated = False
        soul = Soul(**data)
        if migrated:
            self._write_json(path, asdict(soul))
        return soul

    async def save_soul(self, soul: Soul, *, source: str = "", trigger: str = "") -> None:
        path = self._state_dir(soul.instance_id) / "soul.json"
        self._write_json(path, asdict(soul))
        if source:
            logger.info("SOUL_WRITE: source=%s trigger=%s instance=%s", source, trigger, soul.instance_id)

    # -----------------------------------------------------------------------
    # Tenant Profile
    # -----------------------------------------------------------------------

    async def get_instance_profile(self, instance_id: str) -> InstanceProfile | None:
        path = self._state_dir(instance_id) / "profile.json"
        data = self._read_json(path, None)
        if data is None:
            return None
        return InstanceProfile(**data)

    async def save_instance_profile(self, instance_id: str, profile: InstanceProfile) -> None:
        path = self._state_dir(instance_id) / "profile.json"
        self._write_json(path, asdict(profile))

    # -----------------------------------------------------------------------
    # Knowledge
    # -----------------------------------------------------------------------

    async def add_knowledge(self, entry: KnowledgeEntry) -> None:
        path = self._state_dir(entry.instance_id) / "knowledge.json"
        entries = self._read_json(path, [])
        entries.append(asdict(entry))
        self._write_json(path, entries)

    async def query_knowledge(
        self,
        instance_id: str,
        subject: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        active_only: bool = True,
        limit: int = 20,
        member_id: str = "",
    ) -> list[KnowledgeEntry]:
        path = self._state_dir(instance_id) / "knowledge.json"
        raw = self._read_json(path, [])
        results: list[KnowledgeEntry] = []
        for d in raw:
            entry = _load_knowledge_entry(d)
            if active_only and not entry.active:
                continue
            if category and entry.category != category:
                continue
            if subject and subject.lower() not in entry.subject.lower():
                continue
            if tags and not any(t in entry.tags for t in tags):
                continue
            # Member visibility: the member's own entries only (ownerless
            # entries are attributed at boot; see attribute_unowned_knowledge).
            if member_id and getattr(entry, "owner_member_id", "") != member_id:
                continue
            results.append(entry)
        return results[:limit]

    async def save_knowledge_entry(self, entry: KnowledgeEntry) -> None:
        """Upsert a KnowledgeEntry by ID."""
        path = self._state_dir(entry.instance_id) / "knowledge.json"
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("id") == entry.id:
                raw[i] = asdict(entry)
                self._write_json(path, raw)
                return
        raw.append(asdict(entry))
        self._write_json(path, raw)

    async def get_knowledge_entry(
        self, instance_id: str, entry_id: str
    ) -> KnowledgeEntry | None:
        """Get a single KnowledgeEntry by ID."""
        path = self._state_dir(instance_id) / "knowledge.json"
        raw = self._read_json(path, [])
        for d in raw:
            if d.get("id") == entry_id:
                return _load_knowledge_entry(d)
        return None

    async def get_knowledge_hashes(self, instance_id: str) -> set[str]:
        """Return content_hash values for all active entries."""
        path = self._state_dir(instance_id) / "knowledge.json"
        raw = self._read_json(path, [])
        return {
            d["content_hash"]
            for d in raw
            if d.get("active", True) and d.get("content_hash")
        }

    async def get_knowledge_by_hash(
        self, instance_id: str, content_hash: str
    ) -> KnowledgeEntry | None:
        """Find an active entry by content_hash."""
        path = self._state_dir(instance_id) / "knowledge.json"
        raw = self._read_json(path, [])
        for d in raw:
            if d.get("content_hash") == content_hash and d.get("active", True):
                return _load_knowledge_entry(d)
        return None

    async def update_knowledge(self, instance_id: str, entry_id: str, updates: dict) -> None:
        """Find and update a knowledge entry by ID, scoped to the given tenant."""
        path = self._state_dir(instance_id) / "knowledge.json"
        if not path.exists():
            logger.warning("update_knowledge: no knowledge file for instance %s", instance_id)
            return
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("id") == entry_id:
                raw[i].update(updates)
                self._write_json(path, raw)
                return
        logger.warning("update_knowledge: entry_id %s not found for instance %s", entry_id, instance_id)

    # -----------------------------------------------------------------------
    # Behavioral Contracts (CovenantRule)
    # -----------------------------------------------------------------------

    _COVENANT_MIGRATIONS: list[tuple[str, str]] = [
        # Historical migrations (Phase 4 → Phase 6)
        (
            "Never send messages to external contacts without owner approval",
            "Never send messages to third-party CONTACTS (external humans via SMS/email/social) unless the owner initiated the request. When the owner has initiated and the intent is clear, send it — hesitation past that point is friction, not caution. This does NOT apply to send_relational_message, which routes to another MEMBER's agent through the internal permission-matrix dispatcher — that's an intra-system message, not a third-party contact.",
        ),
        # SELF-MODEL-CLARIFICATION: fold in the positive complement for any instance
        # that already migrated to the earlier short form, or to the long form
        # before the paired wording shipped.
        (
            "Never send messages to third-party contacts unless the owner initiated the request",
            "Never send messages to third-party CONTACTS (external humans via SMS/email/social) unless the owner initiated the request. When the owner has initiated and the intent is clear, send it — hesitation past that point is friction, not caution. This does NOT apply to send_relational_message, which routes to another MEMBER's agent through the internal permission-matrix dispatcher — that's an intra-system message, not a third-party contact.",
        ),
        (
            "Never send messages to third-party CONTACTS (external humans via SMS/email/social) unless the owner initiated the request. This does NOT apply to send_relational_message, which routes to another MEMBER's agent through the internal permission-matrix dispatcher — that's an intra-system message, not a third-party contact.",
            "Never send messages to third-party CONTACTS (external humans via SMS/email/social) unless the owner initiated the request. When the owner has initiated and the intent is clear, send it — hesitation past that point is friction, not caution. This does NOT apply to send_relational_message, which routes to another MEMBER's agent through the internal permission-matrix dispatcher — that's an intra-system message, not a third-party contact.",
        ),
        # Phase 6 → current wording
        (
            "Never delete or archive data without owner awareness",
            "Never delete the user's files, entries, or records unless they asked you to. When they ask, do it — their request is the confirmation.",
        ),
        (
            "Never delete or archive data unless the owner requested it. If the owner asks to delete something specific, do it — their request is the confirmation.",
            "Never delete the user's files, entries, or records unless they asked you to. When they ask, do it — their request is the confirmation.",
        ),
        (
            "Never share owner's private information with unrecognized senders",
            "Information shared with you belongs to whoever shared it. Use good judgment about what's appropriate to pass along — routine, expected information can flow naturally between people who know each other. But when someone shares something sensitive, confidential, or clearly meant for you alone, don't disclose it to others without the sharer's consent — even to people they know well.",
        ),
        (
            "Treat information as belonging to whoever shared it. Never disclose someone's information to another person unless the original sharer would clearly expect or want it shared. When uncertain, ask the person who told you before sharing with anyone else — even recognized contacts.",
            "Information shared with you belongs to whoever shared it. Use good judgment about what's appropriate to pass along — routine, expected information can flow naturally between people who know each other. But when someone shares something sensitive, confidential, or clearly meant for you alone, don't disclose it to others without the sharer's consent — even to people they know well.",
        ),
        (
            "Always confirm before any action that costs money",
            "Confirm before spending money unless the owner specified the amount and recipient in their request. When the spend is confirmed, complete it — the confirmation is the authorization event, don't re-confirm.",
        ),
        # SELF-MODEL-CLARIFICATION: fold in the positive complement for instances
        # on the previous money wording.
        (
            "Confirm before spending money unless the owner specified the amount and recipient in their request.",
            "Confirm before spending money unless the owner specified the amount and recipient in their request. When the spend is confirmed, complete it — the confirmation is the authorization event, don't re-confirm.",
        ),
        (
            "Always confirm before sending communications to THIRD PARTIES on the owner's behalf. Reminders and notifications TO the owner are always authorized.",
            "Show drafts before sending to third parties on open channels (SMS, email, social). Once the draft is approved, send it — approval is the authorization event. No draft needed for owner-directed channel delivery, and no draft needed for send_relational_message — that tool routes to another member's AGENT through the permission-matrix dispatcher, not to the person directly, and the receiving agent applies its own judgment.",
        ),
        (
            "For composed messages to third parties, show the draft before sending. For simple relays, briefly confirm content and recipient. Owner-directed delivery to connected channels needs no confirmation.",
            "Show drafts before sending to third parties on open channels (SMS, email, social). Once the draft is approved, send it — approval is the authorization event. No draft needed for owner-directed channel delivery, and no draft needed for send_relational_message — that tool routes to another member's AGENT through the permission-matrix dispatcher, not to the person directly, and the receiving agent applies its own judgment.",
        ),
        # SELF-MODEL-CLARIFICATION: fold in the positive complement for instances
        # on the short drafts wording or the pre-pairing long form.
        (
            "Show drafts before sending to third parties. No confirmation needed for owner-directed channel delivery.",
            "Show drafts before sending to third parties on open channels (SMS, email, social). Once the draft is approved, send it — approval is the authorization event. No draft needed for owner-directed channel delivery, and no draft needed for send_relational_message — that tool routes to another member's AGENT through the permission-matrix dispatcher, not to the person directly, and the receiving agent applies its own judgment.",
        ),
        (
            "Show drafts before sending to third parties on open channels (SMS, email, social). No draft needed for owner-directed channel delivery, and no draft needed for send_relational_message — that tool routes to another member's AGENT through the permission-matrix dispatcher, not to the person directly, and the receiving agent applies its own judgment.",
            "Show drafts before sending to third parties on open channels (SMS, email, social). Once the draft is approved, send it — approval is the authorization event. No draft needed for owner-directed channel delivery, and no draft needed for send_relational_message — that tool routes to another member's AGENT through the permission-matrix dispatcher, not to the person directly, and the receiving agent applies its own judgment.",
        ),
        (
            "Keep responses concise unless detail is requested",
            "Match the depth of your response to what the moment needs. Don't over-explain simple things or under-deliver on complex ones.",
        ),
        (
            "Escalate to owner when request is ambiguous and stakes are non-trivial",
            "When a request is genuinely ambiguous AND involves irreversible consequences, money, or third-party impact, clarify before acting. If the request is clear, act.",
        ),
    ]

    async def get_contract_rules(
        self,
        instance_id: str,
        capability: str | None = None,
        rule_type: str | None = None,
        active_only: bool = True,
    ) -> list[CovenantRule]:
        path = self._state_dir(instance_id) / "contracts.json"
        raw = self._read_json(path, [])
        # One-time migration: update old default covenant wording
        migrated = False
        for d in raw:
            for old_text, new_text in self._COVENANT_MIGRATIONS:
                if d.get("description") == old_text and d.get("source") == "default":
                    d["description"] = new_text
                    migrated = True
        if migrated:
            self._write_json(path, raw)
            logger.info("COVENANT_MIGRATE: instance=%s migrated default covenant wording", instance_id)
        results: list[CovenantRule] = []
        for d in raw:
            rule = _load_covenant_rule(d)
            if active_only and not rule.active:
                continue
            if active_only and rule.superseded_by:
                continue
            if capability and rule.capability != capability:
                continue
            if rule_type and rule.rule_type != rule_type:
                continue
            results.append(rule)
        return results

    async def query_covenant_rules(
        self,
        instance_id: str,
        capability: str | None = None,
        context_space_scope: list[str | None] | None = None,
        active_only: bool = True,
    ) -> list[CovenantRule]:
        path = self._state_dir(instance_id) / "contracts.json"
        raw = self._read_json(path, [])
        results: list[CovenantRule] = []
        for d in raw:
            rule = _load_covenant_rule(d)
            if active_only and not rule.active:
                continue
            if active_only and rule.superseded_by:
                continue
            if capability and rule.capability not in (capability, "general"):
                continue
            if context_space_scope is not None:
                if rule.context_space not in context_space_scope:
                    continue
            results.append(rule)
        return results

    async def add_contract_rule(self, rule: CovenantRule) -> None:
        path = self._state_dir(rule.instance_id) / "contracts.json"
        rules = self._read_json(path, [])
        rules.append(asdict(rule))
        self._write_json(path, rules)

    async def update_contract_rule(self, instance_id: str, rule_id: str, updates: dict) -> None:
        """Find and update a contract rule by ID, scoped to the given tenant."""
        path = self._state_dir(instance_id) / "contracts.json"
        if not path.exists():
            logger.warning("update_contract_rule: no contracts file for instance %s", instance_id)
            return
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("id") == rule_id:
                raw[i].update(updates)
                self._write_json(path, raw)
                return
        logger.warning("update_contract_rule: rule_id %s not found for instance %s", rule_id, instance_id)

    # -----------------------------------------------------------------------
    # Entity Resolution (basic CRUD — Phase 2A adds real logic)
    # -----------------------------------------------------------------------

    async def save_entity_node(self, node: EntityNode) -> None:
        path = self._state_dir(node.instance_id) / "entities.json"
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("id") == node.id:
                raw[i] = asdict(node)
                self._write_json(path, raw)
                return
        raw.append(asdict(node))
        self._write_json(path, raw)

    async def get_entity_node(self, instance_id: str, entity_id: str) -> EntityNode | None:
        path = self._state_dir(instance_id) / "entities.json"
        raw = self._read_json(path, [])
        for d in raw:
            if d.get("id") == entity_id:
                return EntityNode(**d)
        return None

    async def query_entity_nodes(
        self,
        instance_id: str,
        name: str | None = None,
        entity_type: str | None = None,
        active_only: bool = True,
    ) -> list[EntityNode]:
        path = self._state_dir(instance_id) / "entities.json"
        raw = self._read_json(path, [])
        results: list[EntityNode] = []
        for d in raw:
            node = EntityNode(**d)
            if active_only and not node.active:
                continue
            if entity_type and node.entity_type != entity_type:
                continue
            if name:
                name_lower = name.lower()
                match = (
                    name_lower in node.canonical_name.lower()
                    or any(name_lower in a.lower() for a in node.aliases)
                )
                if not match:
                    continue
            results.append(node)
        return results

    async def save_identity_edge(self, instance_id: str, edge: IdentityEdge) -> None:
        path = self._state_dir(instance_id) / "identity_edges.json"
        raw = self._read_json(path, [])
        key = (edge.source_id, edge.target_id)
        for i, d in enumerate(raw):
            if (d.get("source_id"), d.get("target_id")) == key:
                raw[i] = asdict(edge)
                self._write_json(path, raw)
                return
        raw.append(asdict(edge))
        self._write_json(path, raw)

    async def query_identity_edges(
        self, instance_id: str, entity_id: str
    ) -> list[IdentityEdge]:
        path = self._state_dir(instance_id) / "identity_edges.json"
        raw = self._read_json(path, [])
        results = []
        for d in raw:
            if d.get("source_id") == entity_id or d.get("target_id") == entity_id:
                results.append(IdentityEdge(**d))
        return results

    # -----------------------------------------------------------------------
    # Pending Actions (Phase 2B Dispatch Interceptor)
    # -----------------------------------------------------------------------

    async def save_pending_action(self, action: PendingAction) -> None:
        path = self._state_dir(action.instance_id) / "pending_actions.json"
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("id") == action.id:
                raw[i] = asdict(action)
                self._write_json(path, raw)
                return
        raw.append(asdict(action))
        self._write_json(path, raw)

    async def get_pending_actions(
        self, instance_id: str, status: str = "pending"
    ) -> list[PendingAction]:
        path = self._state_dir(instance_id) / "pending_actions.json"
        raw = self._read_json(path, [])
        return [
            PendingAction(**d)
            for d in raw
            if d.get("status") == status
        ]

    async def update_pending_action(
        self, instance_id: str, action_id: str, updates: dict
    ) -> None:
        path = self._state_dir(instance_id) / "pending_actions.json"
        if not path.exists():
            logger.warning("update_pending_action: no file for instance %s", instance_id)
            return
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("id") == action_id:
                raw[i].update(updates)
                self._write_json(path, raw)
                return
        logger.warning("update_pending_action: id %s not found for instance %s", action_id, instance_id)

    # -----------------------------------------------------------------------
    # Context Spaces
    # -----------------------------------------------------------------------

    async def save_context_space(self, space: ContextSpace) -> None:
        path = self._state_dir(space.instance_id) / "spaces.json"
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("id") == space.id:
                raw[i] = asdict(space)
                self._write_json(path, raw)
                return
        raw.append(asdict(space))
        self._write_json(path, raw)

    async def get_context_space(
        self, instance_id: str, space_id: str
    ) -> ContextSpace | None:
        path = self._state_dir(instance_id) / "spaces.json"
        raw = self._read_json(path, [])
        for d in raw:
            if d.get("id") == space_id:
                return _load_context_space(d)
        return None

    async def list_context_spaces(self, instance_id: str) -> list[ContextSpace]:
        path = self._state_dir(instance_id) / "spaces.json"
        raw = self._read_json(path, [])
        return [_load_context_space(d) for d in raw]

    async def update_context_space(
        self, instance_id: str, space_id: str, updates: dict
    ) -> None:
        path = self._state_dir(instance_id) / "spaces.json"
        if not path.exists():
            logger.warning("update_context_space: no file for instance %s", instance_id)
            return
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("id") == space_id:
                raw[i].update(updates)
                self._write_json(path, raw)
                return
        logger.warning("update_context_space: id %s not found for instance %s", space_id, instance_id)

    # -----------------------------------------------------------------------
    # Space Notices (CS-5 cross-domain signals)
    # -----------------------------------------------------------------------

    def _notices_path(self, instance_id: str) -> Path:
        return self._state_dir(instance_id) / "space_notices.json"

    async def append_space_notice(
        self, instance_id: str, space_id: str, text: str,
        source: str = "", notice_type: str = "cross_domain",
    ) -> None:
        from kernos.utils import utc_now
        path = self._notices_path(instance_id)
        notices = self._read_json(path, {})
        if space_id not in notices:
            notices[space_id] = []
        notices[space_id].append({
            "text": text, "source": source, "type": notice_type,
            "created_at": utc_now(),
        })
        self._write_json(path, notices)

    async def drain_space_notices(self, instance_id: str, space_id: str) -> list[dict]:
        path = self._notices_path(instance_id)
        notices = self._read_json(path, {})
        pending = notices.pop(space_id, [])
        if pending:
            self._write_json(path, notices)
        return pending

    # -----------------------------------------------------------------------
    # Knowledge Foresight Query (Phase 3C)
    # -----------------------------------------------------------------------

    async def query_knowledge_by_foresight(
        self,
        instance_id: str,
        expires_before: str,
        expires_after: str = "",
        space_id: str = "",
    ) -> "list[KnowledgeEntry]":
        path = self._state_dir(instance_id) / "knowledge.json"
        raw = self._read_json(path, [])
        results = []
        for d in raw:
            entry = _load_knowledge_entry(d)
            if not entry.active:
                continue
            if not entry.foresight_signal or not entry.foresight_expires:
                continue
            if space_id and entry.context_space != space_id:
                continue
            # foresight_expires must be in (expires_after, expires_before]
            fe = entry.foresight_expires
            if expires_after and fe <= expires_after:
                continue
            if fe > expires_before:
                continue
            results.append(entry)
        return results

    # -----------------------------------------------------------------------
    # Ephemeral Permissions (MESSENGER-COHORT refer-flow)
    # -----------------------------------------------------------------------
    # Flat per-instance JSON. 24h TTL enforced at read time so a missed
    # expiry sweep doesn't surface stale permissions. No correlation with
    # any whisper; no refer-id; no reconciliation.

    def _ephemeral_permissions_path(self, instance_id: str) -> Path:
        return self._state_dir(instance_id) / "ephemeral_permissions.json"

    @staticmethod
    def _ephemeral_is_expired(d: dict) -> bool:
        from datetime import datetime, timezone
        exp = d.get("expires_at", "")
        if not exp:
            return True
        try:
            ts = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts <= datetime.now(timezone.utc)
        except (ValueError, TypeError):
            return True

    async def save_ephemeral_permission(self, perm) -> None:
        from dataclasses import asdict as _asdict
        path = self._ephemeral_permissions_path(perm.instance_id)
        raw = self._read_json(path, [])
        # Drop expired + upsert by id.
        raw = [d for d in raw if not self._ephemeral_is_expired(d) and d.get("id") != perm.id]
        raw.append(_asdict(perm))
        self._write_json(path, raw)

    async def list_ephemeral_permissions(
        self,
        instance_id: str,
        *,
        disclosing_member_id: str = "",
        requesting_member_id: str = "",
    ) -> list:
        from kernos.kernel.state import EphemeralPermission
        path = self._ephemeral_permissions_path(instance_id)
        raw = self._read_json(path, [])
        out = []
        for d in raw:
            if self._ephemeral_is_expired(d):
                continue
            if disclosing_member_id and d.get("disclosing_member_id") != disclosing_member_id:
                continue
            if requesting_member_id and d.get("requesting_member_id") != requesting_member_id:
                continue
            out.append(EphemeralPermission(**d))
        return out

    async def expire_ephemeral_permissions(self, instance_id: str) -> int:
        path = self._ephemeral_permissions_path(instance_id)
        raw = self._read_json(path, [])
        kept = [d for d in raw if not self._ephemeral_is_expired(d)]
        removed = len(raw) - len(kept)
        if removed:
            self._write_json(path, kept)
        return removed

    # -----------------------------------------------------------------------
    # Whispers and Suppressions (Phase 3C)
    # -----------------------------------------------------------------------

    def _awareness_dir(self, instance_id: str) -> Path:
        return self._data_dir / _safe_name(instance_id) / "awareness"

    async def save_whisper(self, instance_id: str, whisper) -> None:
        from kernos.kernel.awareness import Whisper
        from dataclasses import asdict as _asdict
        path = self._awareness_dir(instance_id) / "whispers.json"
        raw = self._read_json(path, [])
        # Upsert by whisper_id
        for i, d in enumerate(raw):
            if d.get("whisper_id") == whisper.whisper_id:
                raw[i] = _asdict(whisper)
                self._write_json(path, raw)
                return
        # Dedup: skip if a pending whisper with the same foresight_signal exists
        _signal = getattr(whisper, 'foresight_signal', '')
        if _signal:
            for d in raw:
                if d.get("foresight_signal") == _signal and not d.get("surfaced_at"):
                    logger.info("WHISPER_DEDUP: signal=%r already pending, skipping", _signal[:60])
                    return
        raw.append(_asdict(whisper))
        self._write_json(path, raw)

    async def get_pending_whispers(self, instance_id: str) -> list:
        from kernos.kernel.awareness import Whisper
        from datetime import datetime, timezone
        path = self._awareness_dir(instance_id) / "whispers.json"
        raw = self._read_json(path, [])
        now = datetime.now(timezone.utc)
        pending = []
        changed = False
        for d in raw:
            if d.get("surfaced_at"):
                continue
            # Expire whispers older than 48 hours
            created = d.get("created_at", "")
            if created:
                try:
                    created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if created_dt.tzinfo is None:
                        created_dt = created_dt.replace(tzinfo=timezone.utc)
                    age_hours = (now - created_dt).total_seconds() / 3600
                    if age_hours > 48:
                        d["surfaced_at"] = now.isoformat()  # Mark as resolved
                        changed = True
                        logger.info("WHISPER_EXPIRED: id=%s age_hours=%.1f",
                            d.get("whisper_id", "?"), age_hours)
                        continue
                except (ValueError, TypeError):
                    pass
            pending.append(Whisper(**d))
        if changed:
            self._write_json(path, raw)
        return pending

    async def mark_whisper_surfaced(self, instance_id: str, whisper_id: str) -> None:
        from datetime import datetime, timezone
        path = self._awareness_dir(instance_id) / "whispers.json"
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("whisper_id") == whisper_id:
                raw[i]["surfaced_at"] = datetime.now(timezone.utc).isoformat()
                self._write_json(path, raw)
                return

    async def delete_whisper(self, instance_id: str, whisper_id: str) -> None:
        path = self._awareness_dir(instance_id) / "whispers.json"
        raw = self._read_json(path, [])
        raw = [d for d in raw if d.get("whisper_id") != whisper_id]
        self._write_json(path, raw)

    async def save_suppression(self, instance_id: str, entry) -> None:
        from kernos.kernel.awareness import SuppressionEntry
        from dataclasses import asdict as _asdict
        path = self._awareness_dir(instance_id) / "suppressions.json"
        raw = self._read_json(path, [])
        # Upsert by whisper_id
        for i, d in enumerate(raw):
            if d.get("whisper_id") == entry.whisper_id:
                raw[i] = _asdict(entry)
                self._write_json(path, raw)
                return
        raw.append(_asdict(entry))
        self._write_json(path, raw)

    async def get_suppressions(
        self,
        instance_id: str,
        knowledge_entry_id: str = "",
        whisper_id: str = "",
        foresight_signal: str = "",
    ) -> list:
        from kernos.kernel.awareness import SuppressionEntry
        path = self._awareness_dir(instance_id) / "suppressions.json"
        raw = self._read_json(path, [])
        results = []
        for d in raw:
            if knowledge_entry_id and d.get("knowledge_entry_id") != knowledge_entry_id:
                continue
            if whisper_id and d.get("whisper_id") != whisper_id:
                continue
            if foresight_signal and d.get("foresight_signal") != foresight_signal:
                continue
            results.append(SuppressionEntry(**d))
        return results

    async def delete_suppression(self, instance_id: str, whisper_id: str) -> None:
        path = self._awareness_dir(instance_id) / "suppressions.json"
        raw = self._read_json(path, [])
        raw = [d for d in raw if d.get("whisper_id") != whisper_id]
        self._write_json(path, raw)

    # -----------------------------------------------------------------------
    # Conversation Summaries
    # -----------------------------------------------------------------------

    async def get_conversation_summary(
        self, instance_id: str, conversation_id: str
    ) -> ConversationSummary | None:
        path = self._state_dir(instance_id) / "conversations.json"
        raw = self._read_json(path, [])
        for d in raw:
            if d.get("conversation_id") == conversation_id:
                return ConversationSummary(**d)
        return None

    async def save_conversation_summary(self, summary: ConversationSummary) -> None:
        path = self._state_dir(summary.instance_id) / "conversations.json"
        raw = self._read_json(path, [])
        # Upsert: replace existing or append
        for i, d in enumerate(raw):
            if d.get("conversation_id") == summary.conversation_id:
                raw[i] = asdict(summary)
                self._write_json(path, raw)
                return
        raw.append(asdict(summary))
        self._write_json(path, raw)

    async def list_conversations(
        self, instance_id: str, active_only: bool = True, limit: int = 20
    ) -> list[ConversationSummary]:
        path = self._state_dir(instance_id) / "conversations.json"
        raw = self._read_json(path, [])
        results = [ConversationSummary(**d) for d in raw]
        if active_only:
            results = [c for c in results if c.active]
        # Most recent first
        results.sort(key=lambda c: c.last_message_at, reverse=True)
        return results[:limit]

    # --- Preferences (Phase 6A) ---

    def _preferences_path(self, instance_id: str) -> Path:
        return self._state_dir(instance_id) / "preferences.json"

    async def _maybe_migrate_preferences(self, instance_id: str) -> None:
        """Lazy migration: convert category='preference' KnowledgeEntries to Preferences."""
        if instance_id in self._preference_migration_done:
            return
        self._preference_migration_done.add(instance_id)

        from kernos.kernel.state import generate_preference_id
        from kernos.utils import utc_now

        knowledge = await self.query_knowledge(instance_id, category="preference")
        if not knowledge:
            return

        existing_prefs = self._load_preferences(instance_id)
        # Skip if migration already happened (check for source_knowledge_id matches)
        existing_source_ids = {e.get("source_knowledge_id", "") for e in existing_prefs}

        migrated_count = 0
        for entry in knowledge:
            if entry.id in existing_source_ids:
                continue  # Already migrated
            pref = Preference(
                id=generate_preference_id(),
                instance_id=instance_id,
                intent=entry.content,
                category="general",
                subject=entry.subject,
                action="prefer",
                parameters={},
                scope="global" if not entry.context_space else entry.context_space,
                context_space=entry.context_space or "",
                status="active",
                created_at=entry.created_at,
                updated_at=utc_now(),
                source_knowledge_id=entry.id,
            )
            existing_prefs.append(asdict(pref))

            # Mark original as migrated (no destructive deletion)
            entry.category = "preference_migrated"
            await self.save_knowledge_entry(entry)
            migrated_count += 1

        if migrated_count:
            self._save_preferences(instance_id, existing_prefs)
            logger.info(
                "PREF_MIGRATION: instance=%s migrated=%d from KnowledgeEntry",
                instance_id, migrated_count,
            )

    def _load_preferences(self, instance_id: str) -> list[dict]:
        return self._read_json(self._preferences_path(instance_id), [])

    def _save_preferences(self, instance_id: str, data: list[dict]) -> None:
        path = self._preferences_path(instance_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(path) + ".lock")
        with lock:
            path.write_text(json.dumps(data, indent=2))

    @staticmethod
    def _load_preference(d: dict) -> Preference:
        """Load a Preference from a dict, handling missing fields gracefully."""
        known_fields = {f.name for f in Preference.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known_fields}
        return Preference(**filtered)

    async def add_preference(self, pref: Preference) -> None:
        await self._maybe_migrate_preferences(pref.instance_id)
        entries = self._load_preferences(pref.instance_id)
        entries.append(asdict(pref))
        self._save_preferences(pref.instance_id, entries)
        logger.info(
            "PREF_WRITE: id=%s action=ADD subject=%s category=%s instance=%s",
            pref.id, pref.subject, pref.category, pref.instance_id,
        )

    async def save_preference(self, pref: Preference) -> None:
        entries = self._load_preferences(pref.instance_id)
        found = False
        for i, e in enumerate(entries):
            if e.get("id") == pref.id:
                entries[i] = asdict(pref)
                found = True
                break
        if not found:
            entries.append(asdict(pref))
        self._save_preferences(pref.instance_id, entries)
        logger.info(
            "PREF_WRITE: id=%s action=%s subject=%s status=%s instance=%s",
            pref.id, "UPDATE" if found else "ADD", pref.subject, pref.status, pref.instance_id,
        )

    async def get_preference(self, instance_id: str, pref_id: str) -> Preference | None:
        await self._maybe_migrate_preferences(instance_id)
        entries = self._load_preferences(instance_id)
        for e in entries:
            if e.get("id") == pref_id:
                return self._load_preference(e)
        return None

    async def query_preferences(
        self,
        instance_id: str,
        status: str = "",
        subject: str = "",
        category: str = "",
        scope: str = "",
        active_only: bool = True,
    ) -> list[Preference]:
        await self._maybe_migrate_preferences(instance_id)
        entries = self._load_preferences(instance_id)
        results: list[Preference] = []
        for e in entries:
            if active_only and e.get("status", "active") != "active":
                continue
            if status and e.get("status") != status:
                continue
            if subject and e.get("subject") != subject:
                continue
            if category and e.get("category") != category:
                continue
            if scope and e.get("scope") != scope:
                continue
            results.append(self._load_preference(e))
        return results

    # --- Relational messaging (RELATIONAL-MESSAGING v5) ---

    def _rm_path(self, instance_id: str) -> Path:
        return self._state_dir(instance_id) / "relational_messages.json"

    @staticmethod
    def _rm_from_dict(d: dict):
        from kernos.kernel.relational_messaging import RelationalMessage
        return RelationalMessage(
            id=d["id"], instance_id=d["instance_id"],
            origin_member_id=d["origin_member_id"],
            origin_agent_identity=d.get("origin_agent_identity", ""),
            addressee_member_id=d["addressee_member_id"],
            intent=d["intent"], content=d["content"],
            urgency=d.get("urgency", "normal"),
            conversation_id=d["conversation_id"],
            state=d.get("state", "pending"),
            created_at=d["created_at"],
            target_space_hint=d.get("target_space_hint", ""),
            delivered_at=d.get("delivered_at", ""),
            surfaced_at=d.get("surfaced_at", ""),
            resolved_at=d.get("resolved_at", ""),
            expired_at=d.get("expired_at", ""),
            resolution_reason=d.get("resolution_reason", ""),
            reply_to_id=d.get("reply_to_id", ""),
            envelope_type=d.get("envelope_type", "message"),
            parcel_id=d.get("parcel_id", ""),
            canvas_id=d.get("canvas_id", ""),
        )

    async def add_relational_message(self, message) -> None:
        from dataclasses import asdict
        path = self._rm_path(message.instance_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = str(path) + ".lock"
        with FileLock(lock_path):
            raw = self._read_json(path, [])
            raw.append(asdict(message))
            with open(path, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)

    async def get_relational_message(self, instance_id, message_id):
        raw = self._read_json(self._rm_path(instance_id), [])
        for d in raw:
            if d.get("id") == message_id and d.get("instance_id") == instance_id:
                return self._rm_from_dict(d)
        return None

    async def query_relational_messages(
        self, instance_id, addressee_member_id="", origin_member_id="",
        states=None, conversation_id="", limit=200,
    ):
        raw = self._read_json(self._rm_path(instance_id), [])
        out = []
        for d in raw:
            if d.get("instance_id") != instance_id:
                continue
            if addressee_member_id and d.get("addressee_member_id") != addressee_member_id:
                continue
            if origin_member_id and d.get("origin_member_id") != origin_member_id:
                continue
            if conversation_id and d.get("conversation_id") != conversation_id:
                continue
            if states and d.get("state") not in states:
                continue
            out.append(self._rm_from_dict(d))
        # Sort by created_at ascending, cap at limit.
        out.sort(key=lambda m: m.created_at)
        return out[:limit]

    async def transition_relational_message_state(
        self, instance_id, message_id, from_state, to_state, updates=None,
    ) -> bool:
        """Atomic CAS on state.json under filelock.

        The filelock spans the full read→check→write sequence, so this is
        genuinely atomic. Concurrent callers either win (returns True) or
        see the row in the new state by the time they acquire the lock
        (returns False).
        """
        updates = updates or {}
        _ALLOWED = {
            "delivered_at", "surfaced_at", "resolved_at", "expired_at",
            "resolution_reason",
        }
        path = self._rm_path(instance_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = str(path) + ".lock"
        with FileLock(lock_path):
            raw = self._read_json(path, [])
            found = False
            won = False
            for i, d in enumerate(raw):
                if d.get("id") != message_id or d.get("instance_id") != instance_id:
                    continue
                found = True
                if d.get("state") != from_state:
                    # Another path won the race.
                    break
                d["state"] = to_state
                for k, v in updates.items():
                    if k in _ALLOWED:
                        d[k] = v
                raw[i] = d
                won = True
                break
            if won:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(raw, f, ensure_ascii=False, indent=2)
            return won and found

    async def delete_relational_message(self, instance_id, message_id) -> None:
        path = self._rm_path(instance_id)
        if not path.exists():
            return
        lock_path = str(path) + ".lock"
        with FileLock(lock_path):
            raw = self._read_json(path, [])
            raw = [
                d for d in raw
                if not (d.get("id") == message_id and d.get("instance_id") == instance_id)
            ]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
