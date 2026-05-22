"""Tests for SPEC-2.0: Schema Foundation Sprint.

Covers:
- KnowledgeEntry evolution and new fields
- Durability → lifecycle_archetype migration
- compute_retrieval_strength() utility
- CovenantRule rename + new fields (backwards compat alias)
- EntityNode, IdentityEdge, CausalEdge models
- PendingAction model
- StateStore CRUD for new types (JsonStateStore)
- CapabilityInfo tool_effects
- EventType Phase 2 additions
- complete_simple() output_schema parameter
"""
import json
import math
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernos.capability.registry import CapabilityInfo, CapabilityStatus
from kernos.kernel.entities import CausalEdge, EntityNode, IdentityEdge
from kernos.kernel.event_types import EventType
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import (
    ARCHETYPE_STABILITY,
    ContractRule,  # alias
    CovenantRule,
    KnowledgeEntry,
    PendingAction,
    W20,
    compute_retrieval_strength,
    default_contract_rules,  # alias
    default_covenant_rules,
)
from kernos.kernel.state_json import JsonStateStore, _durability_to_archetype, _load_knowledge_entry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_ago(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# KnowledgeEntry — new fields and defaults
# ---------------------------------------------------------------------------


def test_knowledge_entry_new_fields_have_defaults():
    entry = KnowledgeEntry(
        id="know_abc123",
        instance_id="t1",
        category="fact",
        subject="user",
        content="Works as a carpenter",
        confidence="stated",
        source_event_id="",
        source_description="test",
        created_at=_now(),
        last_referenced=_now(),
        tags=[],
    )
    assert entry.lifecycle_archetype == "structural"
    assert entry.storage_strength == 1.0
    assert entry.reinforcement_count == 1
    assert entry.salience == 0.5
    assert entry.foresight_signal == ""
    assert entry.foresight_expires == ""
    assert entry.entity_node_id == ""
    assert entry.context_space == ""
    assert entry.expired_at == ""
    assert entry.valid_at == ""
    assert entry.invalid_at == ""
    assert entry.last_reinforced_at == ""
    assert entry.durability == ""  # new entries have empty durability


def test_knowledge_entry_all_archetypes_accepted():
    for archetype in ("identity", "structural", "habitual", "contextual", "ephemeral"):
        entry = KnowledgeEntry(
            id="know_x", instance_id="t", category="fact", subject="user",
            content="test", confidence="stated", source_event_id="",
            source_description="", created_at=_now(), last_referenced=_now(),
            tags=[], lifecycle_archetype=archetype,
        )
        assert entry.lifecycle_archetype == archetype


# ---------------------------------------------------------------------------
# Durability migration
# ---------------------------------------------------------------------------


def test_durability_to_archetype_permanent():
    assert _durability_to_archetype("permanent") == "structural"


def test_durability_to_archetype_empty():
    assert _durability_to_archetype("") == "structural"


def test_durability_to_archetype_session():
    assert _durability_to_archetype("session") == "ephemeral"


def test_durability_to_archetype_expires_at():
    assert _durability_to_archetype("expires_at:2026-04-01T00:00:00") == "contextual"


def test_load_knowledge_entry_migrates_permanent():
    d = {
        "id": "know_x", "instance_id": "t", "category": "fact", "subject": "user",
        "content": "test", "confidence": "stated", "source_event_id": "",
        "source_description": "", "created_at": _now(), "last_referenced": _now(),
        "tags": [], "active": True, "durability": "permanent",
        # No lifecycle_archetype — old format
    }
    entry = _load_knowledge_entry(d)
    assert entry.lifecycle_archetype == "structural"


def test_load_knowledge_entry_migrates_session():
    d = {
        "id": "know_x", "instance_id": "t", "category": "fact", "subject": "user",
        "content": "test", "confidence": "stated", "source_event_id": "",
        "source_description": "", "created_at": _now(), "last_referenced": _now(),
        "tags": [], "active": True, "durability": "session",
    }
    entry = _load_knowledge_entry(d)
    assert entry.lifecycle_archetype == "ephemeral"


def test_load_knowledge_entry_migrates_expires_at():
    d = {
        "id": "know_x", "instance_id": "t", "category": "fact", "subject": "user",
        "content": "test", "confidence": "stated", "source_event_id": "",
        "source_description": "", "created_at": _now(), "last_referenced": _now(),
        "tags": [], "active": True, "durability": "expires_at:2026-04-01T00:00:00+00:00",
    }
    entry = _load_knowledge_entry(d)
    assert entry.lifecycle_archetype == "contextual"
    assert entry.foresight_expires == "2026-04-01T00:00:00+00:00"


def test_load_knowledge_entry_respects_existing_lifecycle_archetype():
    d = {
        "id": "know_x", "instance_id": "t", "category": "fact", "subject": "user",
        "content": "test", "confidence": "stated", "source_event_id": "",
        "source_description": "", "created_at": _now(), "last_referenced": _now(),
        "tags": [], "active": True, "durability": "permanent",
        "lifecycle_archetype": "identity",  # Already set — should NOT be overridden
    }
    entry = _load_knowledge_entry(d)
    assert entry.lifecycle_archetype == "identity"


async def test_json_state_store_knowledge_migration_on_load(tmp_path):
    """Old-format entries in JSON load with correct lifecycle_archetype."""
    import json as _json
    state_dir = tmp_path / "t1" / "state"
    state_dir.mkdir(parents=True)
    old_entry = {
        "id": "know_old1", "instance_id": "t1", "category": "fact", "subject": "user",
        "content": "Likes tea", "confidence": "stated", "source_event_id": "",
        "source_description": "old", "created_at": _now(), "last_referenced": _now(),
        "tags": [], "active": True, "durability": "permanent", "content_hash": "abc123",
    }
    (state_dir / "knowledge.json").write_text(_json.dumps([old_entry]))

    store = JsonStateStore(tmp_path)
    entries = await store.query_knowledge("t1")
    assert len(entries) == 1
    assert entries[0].lifecycle_archetype == "structural"


# ---------------------------------------------------------------------------
# compute_retrieval_strength()
# ---------------------------------------------------------------------------


def test_retrieval_strength_fresh_entry_no_reinforcement():
    """Entry with no last_reinforced_at returns 1.0."""
    entry = KnowledgeEntry(
        id="know_x", instance_id="t", category="fact", subject="user",
        content="test", confidence="stated", source_event_id="",
        source_description="", created_at=_now(), last_referenced=_now(),
        tags=[], lifecycle_archetype="structural",
    )
    assert compute_retrieval_strength(entry, _now()) == 1.0


def test_retrieval_strength_just_reinforced():
    """Entry reinforced just now returns 1.0."""
    now = _now()
    entry = KnowledgeEntry(
        id="know_x", instance_id="t", category="fact", subject="user",
        content="test", confidence="stated", source_event_id="",
        source_description="", created_at=now, last_referenced=now,
        tags=[], lifecycle_archetype="structural", last_reinforced_at=now,
    )
    assert compute_retrieval_strength(entry, now) == 1.0


def test_retrieval_strength_decays_over_time():
    """Strength < 1.0 when some time has passed."""
    reinforced = _days_ago(30)
    entry = KnowledgeEntry(
        id="know_x", instance_id="t", category="fact", subject="user",
        content="test", confidence="stated", source_event_id="",
        source_description="", created_at=reinforced, last_referenced=reinforced,
        tags=[], lifecycle_archetype="structural", last_reinforced_at=reinforced,
    )
    r = compute_retrieval_strength(entry, _now())
    assert 0.0 < r < 1.0


def test_retrieval_strength_ephemeral_decays_faster_than_identity():
    """Ephemeral archetype decays much faster than identity."""
    reinforced = _days_ago(3)
    base_fields = dict(
        id="know_x", instance_id="t", category="fact", subject="user",
        content="test", confidence="stated", source_event_id="",
        source_description="", created_at=reinforced, last_referenced=reinforced,
        tags=[], last_reinforced_at=reinforced,
    )
    ephemeral = KnowledgeEntry(**base_fields, lifecycle_archetype="ephemeral")
    identity = KnowledgeEntry(**base_fields, lifecycle_archetype="identity")
    now = _now()
    r_eph = compute_retrieval_strength(ephemeral, now)
    r_id = compute_retrieval_strength(identity, now)
    assert r_eph < r_id


def test_retrieval_strength_archetype_stability_ordering():
    """Stability ordering: identity > structural > habitual > contextual > ephemeral."""
    assert ARCHETYPE_STABILITY["identity"] > ARCHETYPE_STABILITY["structural"]
    assert ARCHETYPE_STABILITY["structural"] > ARCHETYPE_STABILITY["habitual"]
    assert ARCHETYPE_STABILITY["habitual"] > ARCHETYPE_STABILITY["contextual"]
    assert ARCHETYPE_STABILITY["contextual"] > ARCHETYPE_STABILITY["ephemeral"]


def test_retrieval_strength_higher_storage_strength_slows_decay():
    """Higher storage_strength means slower decay."""
    reinforced = _days_ago(60)
    base_fields = dict(
        id="know_x", instance_id="t", category="fact", subject="user",
        content="test", confidence="stated", source_event_id="",
        source_description="", created_at=reinforced, last_referenced=reinforced,
        tags=[], lifecycle_archetype="structural", last_reinforced_at=reinforced,
    )
    low = KnowledgeEntry(**base_fields, storage_strength=1.0)
    high = KnowledgeEntry(**base_fields, storage_strength=10.0)
    now = _now()
    assert compute_retrieval_strength(high, now) > compute_retrieval_strength(low, now)


# ---------------------------------------------------------------------------
# CovenantRule rename + backwards compat
# ---------------------------------------------------------------------------


def test_covenant_rule_has_new_fields():
    rule = CovenantRule(
        id="rule_abc", instance_id="t", capability="general", rule_type="must_not",
        description="Never delete data", active=True, source="default",
    )
    assert rule.layer == "principle"
    assert rule.enforcement_tier == "confirm"
    assert rule.fallback_action == "ask_user"
    assert rule.graduation_positive_signals == 0
    assert rule.graduation_threshold == 25
    assert rule.graduation_eligible is False
    assert rule.version == 1
    assert rule.supersedes == ""


def test_contract_rule_alias_is_covenant_rule():
    """ContractRule alias points to CovenantRule."""
    assert ContractRule is CovenantRule


def test_contract_rule_can_be_constructed():
    """Code using the old name still works."""
    rule = ContractRule(
        id="rule_x", instance_id="t", capability="general", rule_type="preference",
        description="Be concise", active=True, source="default",
    )
    assert isinstance(rule, CovenantRule)


def test_default_contract_rules_alias():
    """default_contract_rules() alias produces CovenantRule instances."""
    assert default_contract_rules is default_covenant_rules
    rules = default_contract_rules("t1", _now())
    assert all(isinstance(r, CovenantRule) for r in rules)


def test_default_covenant_rules_sets_enforcement_tier(monkeypatch):
    """New default rules have enforcement_tier set appropriately.

    POSTURE-SEEDED-COVENANTS-V1 (2026-05-22): the default seed is
    now profile-aware (minimal=5, standard=7, strict=9). This test
    pins enforcement-tier wiring against the strict profile so the
    full type set (must/must_not/preference/escalation) is exercised.
    """
    monkeypatch.setenv("KERNOS_POSTURE_PROFILE", "strict")
    rules = default_covenant_rules("t1", _now())
    by_type = {r.rule_type: r for r in rules}
    assert by_type["preference"].enforcement_tier == "silent"
    assert by_type["must"].enforcement_tier == "confirm"
    assert by_type["must_not"].enforcement_tier == "confirm"
    assert by_type["escalation"].enforcement_tier == "confirm"


def test_covenant_rule_loads_from_old_json(tmp_path):
    """Old contracts.json (ContractRule format) loads with enforcement_tier migrated from rule_type."""
    import json as _json
    state_dir = tmp_path / "t1" / "state"
    state_dir.mkdir(parents=True)
    old_rules = [
        {
            "id": "rule_a", "instance_id": "t1", "capability": "general",
            "rule_type": "must_not", "description": "Never delete data",
            "active": True, "source": "default", "source_event_id": None,
            "created_at": _now(), "updated_at": _now(), "context_space": None,
        },
        {
            "id": "rule_b", "instance_id": "t1", "capability": "general",
            "rule_type": "preference", "description": "Be concise",
            "active": True, "source": "default", "source_event_id": None,
            "created_at": _now(), "updated_at": _now(), "context_space": None,
        },
    ]
    (state_dir / "contracts.json").write_text(_json.dumps(old_rules))

    import asyncio
    store = JsonStateStore(tmp_path)

    async def _check():
        rules = await store.get_contract_rules("t1")
        assert len(rules) == 2
        by_type = {r.rule_type: r for r in rules}
        assert by_type["must_not"].enforcement_tier == "confirm"   # migrated
        assert by_type["preference"].enforcement_tier == "silent"  # migrated from rule_type
        assert by_type["must_not"].layer == "principle"            # default
        assert by_type["must_not"].version == 1

    asyncio.get_event_loop().run_until_complete(_check())


def test_default_covenant_rules_updated_wording(monkeypatch):
    """New tenants get the current covenant wording.

    Note: RELATIONAL-MESSAGING clarified the third-party rule to exclude
    intra-system send_relational_message calls. Updated phrasing uses
    "third-party CONTACTS" (uppercase emphasis) with the carve-out text.

    SELF-MODEL-CLARIFICATION (Clarification 3) paired every one-sided
    negative rule with its positive complement. Each negative in this
    set now includes an explicit "when X, do it" counterpart in the
    same rule string.

    POSTURE-SEEDED-COVENANTS-V1 (2026-05-22): pinned to strict so the
    full historical wording set (including spending/drafts/depth) is
    exercised. The minimal default no longer carries those rules.
    """
    monkeypatch.setenv("KERNOS_POSTURE_PROFILE", "strict")
    rules = default_covenant_rules("t1", _now())
    descs = [r.description for r in rules]
    assert any("third-party CONTACTS" in d and "owner initiated" in d for d in descs)
    assert any("owner-directed channel delivery" in d.lower() for d in descs)
    assert any("Information shared with you belongs to whoever shared it" in d for d in descs)
    assert any("Match the depth" in d for d in descs)
    assert any("genuinely ambiguous AND involves irreversible" in d for d in descs)
    # Paired positive complements landed on the negatives (Clarification 3)
    assert any(
        "third-party CONTACTS" in d and "hesitation past that point is friction" in d
        for d in descs
    )
    assert any(
        "Confirm before spending money" in d
        and "confirmation is the authorization event" in d
        for d in descs
    )
    assert any(
        "Show drafts" in d and "approval is the authorization event" in d
        for d in descs
    )
    # Old wording should NOT be present
    assert not any("external contacts without owner approval" in d for d in descs)
    assert not any("Keep responses concise unless detail" in d for d in descs)


def test_covenant_migration_updates_old_wording(tmp_path):
    """Existing tenants with old default wording get migrated on load."""
    import json as _json
    state_dir = tmp_path / "t1" / "state"
    state_dir.mkdir(parents=True)
    old_rules = [
        {
            "id": "rule_a", "instance_id": "t1", "capability": "general",
            "rule_type": "must_not",
            "description": "Never send messages to external contacts without owner approval",
            "active": True, "source": "default", "source_event_id": None,
            "created_at": _now(), "updated_at": _now(), "context_space": None,
        },
        {
            "id": "rule_b", "instance_id": "t1", "capability": "general",
            "rule_type": "must",
            "description": "Always confirm before sending communications to THIRD PARTIES on the owner's behalf. Reminders and notifications TO the owner are always authorized.",
            "active": True, "source": "default", "source_event_id": None,
            "created_at": _now(), "updated_at": _now(), "context_space": None,
        },
    ]
    (state_dir / "contracts.json").write_text(_json.dumps(old_rules))

    import asyncio
    store = JsonStateStore(tmp_path)

    async def _check():
        rules = await store.get_contract_rules("t1")
        assert len(rules) == 2
        descs = [r.description for r in rules]
        # Migration lands on the paired wording (SELF-MODEL-CLARIFICATION):
        # both the negative ("unless owner initiated") and the positive
        # complement ("when initiated ... send it") present in the same rule.
        assert any(
            "third-party CONTACTS" in d
            and "owner initiated" in d
            and "send it" in d
            for d in descs
        )
        assert any("owner-directed channel delivery" in d.lower() for d in descs)
        assert any("approval is the authorization event" in d for d in descs)
        # Old wording gone
        assert not any("external contacts without owner approval" in d for d in descs)
        assert not any("THIRD PARTIES" in d for d in descs)
        # Verify migration persisted to disk
        raw = _json.loads((state_dir / "contracts.json").read_text())
        assert "third-party CONTACTS" in raw[0]["description"]

    asyncio.get_event_loop().run_until_complete(_check())


def test_covenant_migration_skips_user_modified_rules(tmp_path):
    """Rules with source != 'default' are NOT migrated even if wording matches."""
    import json as _json
    state_dir = tmp_path / "t1" / "state"
    state_dir.mkdir(parents=True)
    user_rule = [{
        "id": "rule_u", "instance_id": "t1", "capability": "general",
        "rule_type": "must_not",
        "description": "Never send messages to external contacts without owner approval",
        "active": True, "source": "user", "source_event_id": None,
        "created_at": _now(), "updated_at": _now(), "context_space": None,
    }]
    (state_dir / "contracts.json").write_text(_json.dumps(user_rule))

    import asyncio
    store = JsonStateStore(tmp_path)

    async def _check():
        rules = await store.get_contract_rules("t1")
        # User-sourced rule should NOT be migrated
        assert rules[0].description == "Never send messages to external contacts without owner approval"

    asyncio.get_event_loop().run_until_complete(_check())


# ---------------------------------------------------------------------------
# EntityNode, IdentityEdge, CausalEdge models
# ---------------------------------------------------------------------------


def test_entity_node_fields():
    node = EntityNode(
        id="ent_abc123",
        instance_id="t1",
        canonical_name="Alice Smith",
        entity_type="person",
    )
    assert node.aliases == []
    assert node.embedding == []
    assert node.is_canonical is True
    assert node.active is True
    assert node.conversation_ids == []


def test_identity_edge_fields():
    edge = IdentityEdge(
        source_id="ent_a", target_id="ent_b", edge_type="MAYBE_SAME_AS", confidence=0.7
    )
    assert edge.evidence_signals == []
    assert edge.superseded_at == ""


def test_causal_edge_fields():
    edge = CausalEdge(source_id="know_a", target_id="know_b", relationship="enables")
    assert edge.confidence == 0.0
    assert edge.superseded_at == ""


async def test_state_store_entity_node_crud(tmp_path):
    store = JsonStateStore(tmp_path)
    node = EntityNode(
        id="ent_abc123", instance_id="t1", canonical_name="Alice Smith",
        entity_type="person", first_seen=_now(),
    )
    await store.save_entity_node(node)

    fetched = await store.get_entity_node("t1", "ent_abc123")
    assert fetched is not None
    assert fetched.canonical_name == "Alice Smith"
    assert fetched.entity_type == "person"


async def test_state_store_query_entity_nodes_by_name(tmp_path):
    store = JsonStateStore(tmp_path)
    alice = EntityNode(id="ent_a", instance_id="t1", canonical_name="Alice Smith", entity_type="person")
    bob = EntityNode(id="ent_b", instance_id="t1", canonical_name="Bob Jones", entity_type="person")
    await store.save_entity_node(alice)
    await store.save_entity_node(bob)

    results = await store.query_entity_nodes("t1", name="alice")
    assert len(results) == 1
    assert results[0].canonical_name == "Alice Smith"


async def test_state_store_query_entity_nodes_by_type(tmp_path):
    store = JsonStateStore(tmp_path)
    person = EntityNode(id="ent_a", instance_id="t1", canonical_name="Alice", entity_type="person")
    org = EntityNode(id="ent_b", instance_id="t1", canonical_name="Acme Corp", entity_type="organization")
    await store.save_entity_node(person)
    await store.save_entity_node(org)

    results = await store.query_entity_nodes("t1", entity_type="organization")
    assert len(results) == 1
    assert results[0].canonical_name == "Acme Corp"


async def test_state_store_entity_upsert(tmp_path):
    """Saving entity with same ID updates, doesn't duplicate."""
    store = JsonStateStore(tmp_path)
    node = EntityNode(id="ent_a", instance_id="t1", canonical_name="Alice")
    await store.save_entity_node(node)

    updated = EntityNode(id="ent_a", instance_id="t1", canonical_name="Alice Smith", entity_type="person")
    await store.save_entity_node(updated)

    all_nodes = await store.query_entity_nodes("t1")
    assert len(all_nodes) == 1
    assert all_nodes[0].canonical_name == "Alice Smith"


# ---------------------------------------------------------------------------
# PendingAction model
# ---------------------------------------------------------------------------


def test_pending_action_fields():
    action = PendingAction(
        id="pending_abc", instance_id="t1", rule_id="rule_x", tool_name="send_email",
        created_at=_now(),
    )
    assert action.status == "pending"
    assert action.tool_arguments == {}
    assert action.context == {}
    assert action.batch_id == ""


async def test_state_store_pending_action_crud(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    action = PendingAction(
        id="pending_abc", instance_id="t1", rule_id="rule_x", tool_name="send_email",
        tool_arguments={"to": "bob@example.com", "subject": "Hello"},
        created_at=now, status="pending",
    )
    await store.save_pending_action(action)

    results = await store.get_pending_actions("t1", status="pending")
    assert len(results) == 1
    assert results[0].tool_name == "send_email"
    assert results[0].tool_arguments["to"] == "bob@example.com"


async def test_state_store_update_pending_action(tmp_path):
    store = JsonStateStore(tmp_path)
    action = PendingAction(
        id="pending_abc", instance_id="t1", rule_id="rule_x", tool_name="delete_event",
        created_at=_now(), status="pending",
    )
    await store.save_pending_action(action)

    await store.update_pending_action("t1", "pending_abc", {"status": "approved"})
    results = await store.get_pending_actions("t1", status="approved")
    assert len(results) == 1
    assert results[0].status == "approved"


async def test_state_store_pending_actions_filtered_by_status(tmp_path):
    store = JsonStateStore(tmp_path)
    for i, status in enumerate(["pending", "approved", "rejected"]):
        action = PendingAction(
            id=f"pending_{i}", instance_id="t1", rule_id="rule_x",
            tool_name="tool", created_at=_now(), status=status,
        )
        await store.save_pending_action(action)

    pending = await store.get_pending_actions("t1", status="pending")
    assert len(pending) == 1
    assert pending[0].id == "pending_0"


# ---------------------------------------------------------------------------
# CapabilityInfo tool_effects
# ---------------------------------------------------------------------------


def test_capability_info_tool_effects_field():
    cap = CapabilityInfo(
        name="test-cap", display_name="Test", description="desc",
        category="test", status=CapabilityStatus.AVAILABLE,
        tool_effects={"list": "read", "create": "soft_write", "delete": "hard_write"},
    )
    assert cap.tool_effects["list"] == "read"
    assert cap.tool_effects["create"] == "soft_write"
    assert cap.tool_effects["delete"] == "hard_write"


def test_capability_info_tool_effects_default_empty():
    cap = CapabilityInfo(
        name="test-cap", display_name="Test", description="desc",
        category="test", status=CapabilityStatus.AVAILABLE,
    )
    assert cap.tool_effects == {}


def test_known_capabilities_google_calendar_tool_effects():
    from kernos.capability.known import KNOWN_CAPABILITIES
    cal = next(c for c in KNOWN_CAPABILITIES if c.name == "google-calendar")
    assert cal.tool_effects.get("list-events") == "read"
    assert cal.tool_effects.get("create-event") == "soft_write"
    assert cal.tool_effects.get("delete-event") == "hard_write"


def test_known_capabilities_gmail_tool_effects():
    from kernos.capability.known import KNOWN_CAPABILITIES
    gmail = next(c for c in KNOWN_CAPABILITIES if c.name == "gmail")
    assert gmail.tool_effects.get("list-messages") == "read"
    assert gmail.tool_effects.get("send-email") == "hard_write"
    assert gmail.tool_effects.get("create-draft") == "soft_write"


def test_unknown_tool_not_in_map_returns_none():
    """Tools absent from the map return None (caller defaults to 'unknown')."""
    from kernos.capability.known import KNOWN_CAPABILITIES
    cal = next(c for c in KNOWN_CAPABILITIES if c.name == "google-calendar")
    assert cal.tool_effects.get("nonexistent_tool") is None


# ---------------------------------------------------------------------------
# EventType Phase 2 additions
# ---------------------------------------------------------------------------


def test_phase2_event_types_exist():
    # Covenant lifecycle
    assert EventType.COVENANT_EVALUATED == "covenant.evaluated"
    assert EventType.COVENANT_ACTION_STAGED == "covenant.action.staged"
    assert EventType.COVENANT_ACTION_APPROVED == "covenant.action.approved"
    assert EventType.COVENANT_ACTION_REJECTED == "covenant.action.rejected"
    assert EventType.COVENANT_ACTION_EXPIRED == "covenant.action.expired"
    assert EventType.COVENANT_RULE_GRADUATED == "covenant.rule.graduated"
    assert EventType.COVENANT_RULE_REGRESSED == "covenant.rule.regressed"
    assert EventType.COVENANT_RULE_CREATED == "covenant.rule.created"
    assert EventType.COVENANT_RULE_UPDATED == "covenant.rule.updated"
    # Entity resolution
    assert EventType.ENTITY_CREATED == "entity.created"
    assert EventType.ENTITY_MERGED == "entity.merged"
    assert EventType.ENTITY_LINKED == "entity.linked"
    # Knowledge lifecycle
    assert EventType.KNOWLEDGE_REINFORCED == "knowledge.reinforced"
    assert EventType.KNOWLEDGE_INVALIDATED == "knowledge.invalidated"
    assert EventType.KNOWLEDGE_DECAYED == "knowledge.decayed"


def test_existing_event_types_unchanged():
    """Phase 1 event types are still present and correct."""
    assert EventType.MESSAGE_RECEIVED == "message.received"
    assert EventType.REASONING_REQUEST == "reasoning.request"
    assert EventType.KNOWLEDGE_EXTRACTED == "knowledge.extracted"
    assert EventType.AGENT_HATCHED == "agent.hatched"


# ---------------------------------------------------------------------------
# complete_simple() output_schema parameter
# ---------------------------------------------------------------------------


async def test_complete_simple_with_output_schema():
    """output_schema is passed to provider and response is returned."""
    from kernos.kernel.reasoning import AnthropicProvider, ReasoningService

    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": False}

    mock_provider = AsyncMock()
    mock_response = MagicMock()
    mock_response.stop_reason = "end_turn"
    mock_response.content = [MagicMock(type="text", text='{"name": "Alice"}')]
    mock_provider.complete = AsyncMock(return_value=mock_response)

    service = ReasoningService(
        provider=mock_provider,
        events=MagicMock(),
        mcp=MagicMock(),
        audit=MagicMock(),
    )

    result = await service.complete_simple(
        system_prompt="Extract name",
        user_content="My name is Alice",
        output_schema=schema,
    )
    assert result == '{"name": "Alice"}'
    # Verify output_schema was passed to provider
    call_kwargs = mock_provider.complete.call_args.kwargs
    assert call_kwargs.get("output_schema") == schema


async def test_complete_simple_truncation_returns_empty_json():
    """Truncated response (max_tokens) returns '{}'."""
    mock_provider = AsyncMock()
    mock_response = MagicMock()
    mock_response.stop_reason = "max_tokens"
    mock_response.content = []
    mock_provider.complete = AsyncMock(return_value=mock_response)

    from kernos.kernel.reasoning import ReasoningService
    service = ReasoningService(
        provider=mock_provider, events=MagicMock(), mcp=MagicMock(), audit=MagicMock()
    )

    result = await service.complete_simple(
        system_prompt="sys", user_content="user", output_schema={"type": "object"}
    )
    assert result == "{}"


async def test_complete_simple_refusal_returns_empty_json():
    """Refused response returns '{}'."""
    mock_provider = AsyncMock()
    mock_response = MagicMock()
    mock_response.stop_reason = "refusal"
    mock_response.content = []
    mock_provider.complete = AsyncMock(return_value=mock_response)

    from kernos.kernel.reasoning import ReasoningService
    service = ReasoningService(
        provider=mock_provider, events=MagicMock(), mcp=MagicMock(), audit=MagicMock()
    )

    result = await service.complete_simple(
        system_prompt="sys", user_content="user", output_schema={"type": "object"}
    )
    assert result == "{}"


async def test_complete_simple_without_schema_unchanged():
    """Without output_schema, behavior is unchanged (no output_config kwarg)."""
    mock_provider = AsyncMock()
    mock_response = MagicMock()
    mock_response.stop_reason = "end_turn"
    mock_response.content = [MagicMock(type="text", text="Hello")]
    mock_provider.complete = AsyncMock(return_value=mock_response)

    from kernos.kernel.reasoning import ReasoningService
    service = ReasoningService(
        provider=mock_provider, events=MagicMock(), mcp=MagicMock(), audit=MagicMock()
    )

    result = await service.complete_simple(
        system_prompt="sys", user_content="hi"
    )
    assert result == "Hello"
    call_kwargs = mock_provider.complete.call_args.kwargs
    assert call_kwargs.get("output_schema") is None


# ---------------------------------------------------------------------------
# Extraction schema structure
# ---------------------------------------------------------------------------


def test_extraction_schema_is_valid_structure():
    from kernos.kernel.projectors.llm_extractor import EXTRACTION_SCHEMA
    assert EXTRACTION_SCHEMA["type"] == "object"
    assert "reasoning" in EXTRACTION_SCHEMA["required"]
    assert "entities" in EXTRACTION_SCHEMA["required"]
    assert "facts" in EXTRACTION_SCHEMA["required"]
    assert "preferences" in EXTRACTION_SCHEMA["required"]
    assert "corrections" in EXTRACTION_SCHEMA["required"]
    assert EXTRACTION_SCHEMA.get("additionalProperties") is False


def test_extraction_schema_facts_have_lifecycle_archetype():
    from kernos.kernel.projectors.llm_extractor import EXTRACTION_SCHEMA
    fact_props = EXTRACTION_SCHEMA["properties"]["facts"]["items"]["properties"]
    assert "lifecycle_archetype" in fact_props
    assert "foresight_signal" in fact_props
    assert "foresight_expires" in fact_props
    assert "salience" in fact_props


def test_extraction_schema_preferences_have_lifecycle_archetype():
    from kernos.kernel.projectors.llm_extractor import EXTRACTION_SCHEMA
    pref_props = EXTRACTION_SCHEMA["properties"]["preferences"]["items"]["properties"]
    assert "lifecycle_archetype" in pref_props


# ---------------------------------------------------------------------------
# Tier 2 extraction uses new fields
# ---------------------------------------------------------------------------


async def test_tier2_extraction_populates_lifecycle_archetype(tmp_path):
    """Extracted facts carry lifecycle_archetype from the LLM response."""
    from kernos.kernel.events import JsonEventStream
    from kernos.kernel.projectors.llm_extractor import run_tier2_extraction
    from kernos.kernel.soul import Soul

    extracted_json = json.dumps({
        "reasoning": "User is a carpenter",
        "entities": [],
        "facts": [
            {"subject": "user", "content": "Works as a carpenter",
             "confidence": "stated", "lifecycle_archetype": "structural",
             "foresight_signal": "", "foresight_expires": "", "salience": "0.7"}
        ],
        "preferences": [],
        "corrections": [],
    })

    mock_rs = MagicMock()
    mock_rs.complete_simple = AsyncMock(return_value=extracted_json)

    store = JsonStateStore(tmp_path)
    soul = Soul(instance_id="t1")
    events = JsonEventStream(tmp_path)

    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "I'm a carpenter"}],
        soul=soul, state=store, events=events,
        reasoning_service=mock_rs, instance_id="t1",
    )

    # SPEC-CHECKPOINTED-FACT-HARVEST: facts no longer extracted per-turn
    entries = await store.query_knowledge("t1")
    assert len(entries) == 0  # harvested at boundaries, not per-turn


# ---------------------------------------------------------------------------
# ContextSpace model
# ---------------------------------------------------------------------------


def test_context_space_defaults():
    space = ContextSpace(id="space_abc123", instance_id="t1", name="General")
    assert space.space_type == "general"
    assert space.status == "active"
    assert space.is_default is False
    assert space.description == ""
    assert space.posture == ""
    assert space.parent_id == ""
    assert space.aliases == []
    assert space.depth == 0


def test_context_space_general_default():
    space = ContextSpace(
        id="space_abc123", instance_id="t1", name="General",
        space_type="general", is_default=True,
    )
    assert space.is_default is True
    assert space.space_type == "general"


def test_context_space_all_types_accepted():
    for space_type in ("general", "domain", "subdomain", "system"):
        s = ContextSpace(id="space_x", instance_id="t", name="Test", space_type=space_type)
        assert s.space_type == space_type


async def test_state_store_context_space_crud(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    space = ContextSpace(
        id="space_abc123", instance_id="t1", name="TTRPG — Aethoria Campaign",
        description="Fantasy RPG campaign",
        space_type="project", status="active",
        created_at=now, last_active_at=now,
    )
    await store.save_context_space(space)

    fetched = await store.get_context_space("t1", "space_abc123")
    assert fetched is not None
    assert fetched.name == "TTRPG — Aethoria Campaign"
    assert fetched.space_type == "project"
    assert fetched.description == "Fantasy RPG campaign"


async def test_state_store_list_context_spaces(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    daily = ContextSpace(id="space_d", instance_id="t1", name="General", is_default=True, created_at=now)
    project = ContextSpace(id="space_p", instance_id="t1", name="Side Project", space_type="project", created_at=now)
    await store.save_context_space(daily)
    await store.save_context_space(project)

    spaces = await store.list_context_spaces("t1")
    assert len(spaces) == 2
    names = {s.name for s in spaces}
    assert "General" in names
    assert "Side Project" in names


async def test_state_store_context_space_upsert(tmp_path):
    """Saving space with same ID updates, doesn't duplicate."""
    store = JsonStateStore(tmp_path)
    space = ContextSpace(id="space_x", instance_id="t1", name="Old Name")
    await store.save_context_space(space)

    updated = ContextSpace(id="space_x", instance_id="t1", name="New Name", status="dormant")
    await store.save_context_space(updated)

    spaces = await store.list_context_spaces("t1")
    assert len(spaces) == 1
    assert spaces[0].name == "New Name"
    assert spaces[0].status == "dormant"


async def test_state_store_update_context_space(tmp_path):
    store = JsonStateStore(tmp_path)
    space = ContextSpace(id="space_x", instance_id="t1", name="General", is_default=True)
    await store.save_context_space(space)

    await store.update_context_space("t1", "space_x", {"status": "dormant", "last_active_at": _now()})

    fetched = await store.get_context_space("t1", "space_x")
    assert fetched is not None
    assert fetched.status == "dormant"


async def test_state_store_context_spaceinstance_isolation(tmp_path):
    """Spaces are stored per_instance."""
    store = JsonStateStore(tmp_path)
    s1 = ContextSpace(id="space_a", instance_id="t1", name="T1 Space")
    s2 = ContextSpace(id="space_b", instance_id="t2", name="T2 Space")
    await store.save_context_space(s1)
    await store.save_context_space(s2)

    t1_spaces = await store.list_context_spaces("t1")
    t2_spaces = await store.list_context_spaces("t2")
    assert len(t1_spaces) == 1
    assert t1_spaces[0].name == "T1 Space"
    assert len(t2_spaces) == 1
    assert t2_spaces[0].name == "T2 Space"


async def test_state_store_get_nonexistent_space_returns_none(tmp_path):
    store = JsonStateStore(tmp_path)
    result = await store.get_context_space("t1", "space_nonexistent")
    assert result is None


async def test_state_store_list_spaces_empty_for_new_instance(tmp_path):
    store = JsonStateStore(tmp_path)
    spaces = await store.list_context_spaces("brand_new_instance")
    assert spaces == []


# ---------------------------------------------------------------------------
# Daily space auto-creation in handler
# ---------------------------------------------------------------------------


async def test_handler_creates_daily_space_for_new_instance(tmp_path):
    """_get_or_init_soul() auto-creates a daily space for new tenants."""
    from kernos.kernel.state_json import JsonStateStore
    from kernos.messages.handler import MessageHandler

    store = JsonStateStore(tmp_path)
    handler = MessageHandler.__new__(MessageHandler)
    handler.state = store

    await handler._get_or_init_soul("t1")

    spaces = await store.list_context_spaces("t1")
    # Now creates daily + system spaces
    assert len(spaces) == 2
    daily_spaces = [s for s in spaces if s.is_default]
    assert len(daily_spaces) == 1
    daily = daily_spaces[0]
    assert daily.is_default is True
    assert daily.space_type == "general"
    assert daily.name == "General"
    assert daily.instance_id == "t1"
    system_spaces = [s for s in spaces if s.space_type == "system"]
    assert len(system_spaces) == 1


async def test_handler_does_not_duplicate_daily_space(tmp_path):
    """Calling _get_or_init_soul() twice doesn't create two daily spaces."""
    from kernos.kernel.state_json import JsonStateStore
    from kernos.messages.handler import MessageHandler

    store = JsonStateStore(tmp_path)
    handler = MessageHandler.__new__(MessageHandler)
    handler.state = store

    await handler._get_or_init_soul("t1")
    await handler._get_or_init_soul("t1")  # second call

    spaces = await store.list_context_spaces("t1")
    assert len(spaces) == 2  # daily + system (no duplicates)


# ---------------------------------------------------------------------------
# Context Space event types
# ---------------------------------------------------------------------------


def test_context_space_event_types_exist():
    assert EventType.CONTEXT_SPACE_CREATED == "context.space.created"
    assert EventType.CONTEXT_SPACE_SWITCHED == "context.space.switched"
    assert EventType.CONTEXT_SPACE_SUSPENDED == "context.space.suspended"


async def test_tier2_extraction_handles_empty_json_gracefully(tmp_path):
    """complete_simple returning '{}' (truncation) doesn't crash extractor."""
    from kernos.kernel.events import JsonEventStream
    from kernos.kernel.projectors.llm_extractor import run_tier2_extraction
    from kernos.kernel.soul import Soul

    mock_rs = MagicMock()
    mock_rs.complete_simple = AsyncMock(return_value="{}")

    store = JsonStateStore(tmp_path)
    soul = Soul(instance_id="t1")
    events = JsonEventStream(tmp_path)

    # Should not raise
    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "hi"}],
        soul=soul, state=store, events=events,
        reasoning_service=mock_rs, instance_id="t1",
    )
    entries = await store.query_knowledge("t1")
    assert entries == []
