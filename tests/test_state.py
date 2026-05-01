"""Tests for kernos.kernel.state and kernos.kernel.state_json."""
from datetime import datetime, timezone

from kernos.kernel.state import (
    ContractRule,
    ConversationSummary,
    KnowledgeEntry,
    InstanceProfile,
    default_contract_rules,
)
from kernos.kernel.state_json import JsonStateStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- InstanceProfile CRUD ---


async def test_get_instance_profile_none_for_new(tmp_path):
    store = JsonStateStore(tmp_path)
    profile = await store.get_instance_profile("new_instance")
    assert profile is None


async def test_save_and_get_instance_profile(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    profile = InstanceProfile(
        instance_id="sms:+15555550100",
        status="active",
        created_at=now,
    )
    await store.save_instance_profile("sms:+15555550100", profile)
    fetched = await store.get_instance_profile("sms:+15555550100")
    assert fetched is not None
    assert fetched.instance_id == "sms:+15555550100"
    assert fetched.status == "active"


async def test_save_instance_profile_overwrites(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    profile = InstanceProfile(instance_id="t1", status="active", created_at=now)
    await store.save_instance_profile("t1", profile)

    profile2 = InstanceProfile(instance_id="t1", status="suspended", created_at=now)
    await store.save_instance_profile("t1", profile2)

    fetched = await store.get_instance_profile("t1")
    assert fetched.status == "suspended"


async def test_instance_profile_default_dicts(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    profile = InstanceProfile(instance_id="t1", status="active", created_at=now)
    await store.save_instance_profile("t1", profile)
    fetched = await store.get_instance_profile("t1")
    assert fetched.platforms == {}
    assert fetched.preferences == {}
    assert fetched.capabilities == {}
    assert fetched.model_config == {}


# --- KnowledgeEntry CRUD ---


def _make_knowledge(
    instance_id: str = "t1",
    subject: str = "Alice",
    category: str = "entity",
    kid: str | None = None,
    tags: list[str] | None = None,
) -> KnowledgeEntry:
    now = _now()
    return KnowledgeEntry(
        id=kid or f"know_test_{subject}",
        instance_id=instance_id,
        category=category,
        subject=subject,
        content=f"{subject} is a person",
        confidence="stated",
        source_event_id="evt_test",
        source_description="test",
        created_at=now,
        last_referenced=now,
        tags=tags or ["person"],
    )


async def test_add_and_query_knowledge(tmp_path):
    store = JsonStateStore(tmp_path)
    entry = _make_knowledge()
    await store.add_knowledge(entry)
    results = await store.query_knowledge("t1")
    assert len(results) == 1
    assert results[0].subject == "Alice"


async def test_query_knowledge_empty(tmp_path):
    store = JsonStateStore(tmp_path)
    results = await store.query_knowledge("t1")
    assert results == []


async def test_query_knowledge_by_subject(tmp_path):
    store = JsonStateStore(tmp_path)
    await store.add_knowledge(_make_knowledge(subject="Alice", kid="k_alice"))
    await store.add_knowledge(_make_knowledge(subject="Bob", kid="k_bob"))

    results = await store.query_knowledge("t1", subject="alice")
    assert len(results) == 1
    assert results[0].subject == "Alice"


async def test_query_knowledge_by_category(tmp_path):
    store = JsonStateStore(tmp_path)
    await store.add_knowledge(_make_knowledge(category="entity", kid="k1"))
    await store.add_knowledge(
        _make_knowledge(subject="gym pref", category="preference", kid="k2")
    )

    results = await store.query_knowledge("t1", category="preference")
    assert len(results) == 1
    assert results[0].category == "preference"


async def test_query_knowledge_active_only(tmp_path):
    store = JsonStateStore(tmp_path)
    entry = _make_knowledge()
    await store.add_knowledge(entry)
    await store.update_knowledge("t1", entry.id, {"active": False})

    active = await store.query_knowledge("t1", active_only=True)
    all_entries = await store.query_knowledge("t1", active_only=False)
    assert len(active) == 0
    assert len(all_entries) == 1


async def test_query_knowledge_by_tags(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    e1 = KnowledgeEntry(
        id="k1",
        instance_id="t1",
        category="entity",
        subject="Alice",
        content="Alice info",
        confidence="stated",
        source_event_id="e1",
        source_description="test",
        created_at=now,
        last_referenced=now,
        tags=["person", "vip"],
    )
    e2 = KnowledgeEntry(
        id="k2",
        instance_id="t1",
        category="entity",
        subject="Bob",
        content="Bob info",
        confidence="stated",
        source_event_id="e2",
        source_description="test",
        created_at=now,
        last_referenced=now,
        tags=["person"],
    )
    await store.add_knowledge(e1)
    await store.add_knowledge(e2)

    vip = await store.query_knowledge("t1", tags=["vip"])
    assert len(vip) == 1
    assert vip[0].subject == "Alice"


async def test_update_knowledge(tmp_path):
    store = JsonStateStore(tmp_path)
    entry = _make_knowledge()
    await store.add_knowledge(entry)
    await store.update_knowledge(
        "t1", entry.id, {"content": "Alice is a developer", "confidence": "observed"}
    )
    results = await store.query_knowledge("t1")
    assert results[0].content == "Alice is a developer"
    assert results[0].confidence == "observed"


async def test_update_knowledge_shadow_archive(tmp_path):
    """Setting active=False archives rather than deletes."""
    store = JsonStateStore(tmp_path)
    entry = _make_knowledge()
    await store.add_knowledge(entry)
    await store.update_knowledge("t1", entry.id, {"active": False})

    active = await store.query_knowledge("t1", active_only=True)
    archived = await store.query_knowledge("t1", active_only=False)
    assert len(active) == 0
    assert len(archived) == 1
    assert archived[0].active is False


# --- Behavioral Contracts ---


def test_default_contract_rules_count():
    now = _now()
    rules = default_contract_rules("t1", now)
    assert len(rules) == 9  # 8 base + 1 update-notification (INFORMING-V1)


def test_default_contract_rules_all_active():
    now = _now()
    rules = default_contract_rules("t1", now)
    assert all(r.active for r in rules)


def test_default_contract_rules_source():
    now = _now()
    rules = default_contract_rules("t1", now)
    assert all(r.source == "default" for r in rules)


def test_default_contract_rules_has_must_nots():
    now = _now()
    rules = default_contract_rules("t1", now)
    must_nots = [r for r in rules if r.rule_type == "must_not"]
    assert len(must_nots) == 3


def test_default_contract_rules_correct_tenant():
    now = _now()
    rules = default_contract_rules("sms:+15555550100", now)
    assert all(r.instance_id == "sms:+15555550100" for r in rules)


async def test_add_and_get_contract_rules(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    for rule in default_contract_rules("t1", now):
        await store.add_contract_rule(rule)

    all_rules = await store.get_contract_rules("t1")
    assert len(all_rules) == 9


async def test_get_contract_rules_by_type(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    for rule in default_contract_rules("t1", now):
        await store.add_contract_rule(rule)

    must_nots = await store.get_contract_rules("t1", rule_type="must_not")
    assert len(must_nots) == 3
    assert all(r.rule_type == "must_not" for r in must_nots)


async def test_get_contract_rules_active_only(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    rules = default_contract_rules("t1", now)
    for rule in rules:
        await store.add_contract_rule(rule)

    await store.update_contract_rule("t1", rules[0].id, {"active": False})

    active = await store.get_contract_rules("t1", active_only=True)
    all_r = await store.get_contract_rules("t1", active_only=False)
    assert len(active) == 8
    assert len(all_r) == 9


async def test_update_contract_rule(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    rule = ContractRule(
        id="rule_test",
        instance_id="t1",
        capability="general",
        rule_type="must_not",
        description="Original description",
        active=True,
        source="default",
        source_event_id=None,
        created_at=now,
        updated_at=now,
    )
    await store.add_contract_rule(rule)
    await store.update_contract_rule("t1", "rule_test", {"description": "Updated description"})

    rules = await store.get_contract_rules("t1", active_only=False)
    assert rules[0].description == "Updated description"


async def test_get_contract_rules_empty(tmp_path):
    store = JsonStateStore(tmp_path)
    rules = await store.get_contract_rules("t1")
    assert rules == []


# --- ConversationSummary ---


async def test_get_conversation_summary_none_for_new(tmp_path):
    store = JsonStateStore(tmp_path)
    summary = await store.get_conversation_summary("t1", "conv1")
    assert summary is None


async def test_save_and_get_conversation_summary(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    summary = ConversationSummary(
        instance_id="t1",
        conversation_id="conv1",
        platform="sms",
        message_count=1,
        first_message_at=now,
        last_message_at=now,
    )
    await store.save_conversation_summary(summary)
    fetched = await store.get_conversation_summary("t1", "conv1")
    assert fetched is not None
    assert fetched.conversation_id == "conv1"
    assert fetched.message_count == 1


async def test_conversation_summary_upsert(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    summary = ConversationSummary(
        instance_id="t1",
        conversation_id="conv1",
        platform="sms",
        message_count=1,
        first_message_at=now,
        last_message_at=now,
    )
    await store.save_conversation_summary(summary)

    # Mutate and re-save (upsert)
    summary.message_count = 5
    summary.last_message_at = _now()
    await store.save_conversation_summary(summary)

    fetched = await store.get_conversation_summary("t1", "conv1")
    assert fetched.message_count == 5

    # Should still be only one entry
    all_convs = await store.list_conversations("t1", active_only=False)
    assert len(all_convs) == 1


async def test_list_conversations_sorted_by_last_message(tmp_path):
    store = JsonStateStore(tmp_path)
    s1 = ConversationSummary(
        instance_id="t1",
        conversation_id="conv_old",
        platform="sms",
        message_count=1,
        first_message_at="2026-03-01T00:00:00+00:00",
        last_message_at="2026-03-01T00:00:00+00:00",
    )
    s2 = ConversationSummary(
        instance_id="t1",
        conversation_id="conv_new",
        platform="sms",
        message_count=3,
        first_message_at="2026-03-03T00:00:00+00:00",
        last_message_at="2026-03-03T00:00:00+00:00",
    )
    await store.save_conversation_summary(s1)
    await store.save_conversation_summary(s2)

    convs = await store.list_conversations("t1")
    assert convs[0].conversation_id == "conv_new"
    assert convs[1].conversation_id == "conv_old"


async def test_list_conversations_active_only(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    s1 = ConversationSummary(
        instance_id="t1",
        conversation_id="conv1",
        platform="sms",
        message_count=1,
        first_message_at=now,
        last_message_at=now,
        active=True,
    )
    s2 = ConversationSummary(
        instance_id="t1",
        conversation_id="conv2",
        platform="sms",
        message_count=1,
        first_message_at=now,
        last_message_at=now,
        active=False,
    )
    await store.save_conversation_summary(s1)
    await store.save_conversation_summary(s2)

    active = await store.list_conversations("t1", active_only=True)
    all_c = await store.list_conversations("t1", active_only=False)
    assert len(active) == 1
    assert len(all_c) == 2


async def test_list_conversations_empty(tmp_path):
    store = JsonStateStore(tmp_path)
    convs = await store.list_conversations("t1")
    assert convs == []
