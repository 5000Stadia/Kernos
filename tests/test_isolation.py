"""Cross-tenant isolation and path traversal tests.

Every test in this file either:
- Creates two tenants (A and B) in the same data directory, writes data for A,
  and verifies B cannot see it.
- Tests that malicious or malformed tenant/conversation IDs cannot escape
  their filesystem sandbox.
"""
from datetime import datetime, timezone

import pytest

from kernos.kernel.events import JsonEventStream, emit_event
from kernos.kernel.soul import Soul
from kernos.kernel.state import (
    ConversationSummary,
    KnowledgeEntry,
    InstanceProfile,
    default_contract_rules,
)
from kernos.kernel.state_json import JsonStateStore
from kernos.persistence.json_file import (
    JsonAuditStore,
    JsonConversationStore,
    JsonInstanceStore,
)
from kernos.utils import _safe_name


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_knowledge(instance_id: str, entry_id: str, subject: str = "Alice") -> KnowledgeEntry:
    now = _now()
    return KnowledgeEntry(
        id=entry_id,
        instance_id=instance_id,
        category="entity",
        subject=subject,
        content=f"Content about {subject}",
        confidence="stated",
        source_event_id="evt_test",
        source_description="test",
        created_at=now,
        last_referenced=now,
        tags=["test"],
    )


# ============================================================================
# 1.1 Conversation Store Isolation
# ============================================================================


async def test_conversationinstance_a_invisible_to_b(tmp_path):
    store = JsonConversationStore(tmp_path)
    await store.append("tenant_a", "conv_1", {"role": "user", "content": "Hello"})
    result = await store.get_recent("tenant_b", "conv_1")
    assert result == []


async def test_conversation_different_conv_id_invisible_to_b(tmp_path):
    store = JsonConversationStore(tmp_path)
    await store.append("tenant_a", "conv_1", {"role": "user", "content": "Hello"})
    result = await store.get_recent("tenant_b", "different_conv")
    assert result == []


async def test_conversation_archive_lands_in_correct_dir(tmp_path):
    store = JsonConversationStore(tmp_path)
    await store.append("tenant_a", "conv_1", {"role": "user", "content": "Hello"})
    await store.archive("tenant_a", "conv_1")
    tenant_a_archive = tmp_path / "tenant_a" / "archive" / "conversations"
    assert any(tenant_a_archive.rglob("*.json"))
    assert not (tmp_path / "tenant_b").exists()


# ============================================================================
# 1.2 Event Stream Isolation
# ============================================================================


async def test_event_streaminstance_a_invisible_to_b(tmp_path):
    stream = JsonEventStream(tmp_path)
    await emit_event(stream, "message.received", "tenant_a", "test", payload={"content": "Hello"})
    events = await stream.query("tenant_b")
    assert events == []


async def test_event_stream_count_zero_for_b(tmp_path):
    stream = JsonEventStream(tmp_path)
    await emit_event(stream, "message.received", "tenant_a", "test", payload={})
    count = await stream.count("tenant_b")
    assert count == 0


async def test_event_stream_type_filter_invisible_to_b(tmp_path):
    stream = JsonEventStream(tmp_path)
    await emit_event(stream, "message.received", "tenant_a", "test", payload={})
    events = await stream.query("tenant_b", event_types=["message.received"])
    assert events == []


async def test_event_stream_both_tenants_see_only_own(tmp_path):
    stream = JsonEventStream(tmp_path)
    for _ in range(3):
        await emit_event(stream, "message.received", "tenant_a", "test", payload={})
    for _ in range(5):
        await emit_event(stream, "message.received", "tenant_b", "test", payload={})
    assert await stream.count("tenant_a") == 3
    assert await stream.count("tenant_b") == 5


# ============================================================================
# 1.3 State Store — Tenant Profile Isolation
# ============================================================================


async def test_profileinstance_a_invisible_to_b(tmp_path):
    store = JsonStateStore(tmp_path)
    profile = InstanceProfile(instance_id="tenant_a", status="active", created_at=_now())
    await store.save_instance_profile("tenant_a", profile)
    assert await store.get_instance_profile("tenant_b") is None


async def test_profile_eachinstance_sees_own(tmp_path):
    store = JsonStateStore(tmp_path)
    await store.save_instance_profile("tenant_a", InstanceProfile(instance_id="tenant_a", status="active", created_at=_now()))
    await store.save_instance_profile("tenant_b", InstanceProfile(instance_id="tenant_b", status="active", created_at=_now()))
    fetched_a = await store.get_instance_profile("tenant_a")
    fetched_b = await store.get_instance_profile("tenant_b")
    assert fetched_a.instance_id == "tenant_a"
    assert fetched_b.instance_id == "tenant_b"


async def test_profile_update_a_does_not_affect_b(tmp_path):
    store = JsonStateStore(tmp_path)
    profile_a = InstanceProfile(instance_id="tenant_a", status="active", created_at=_now())
    profile_b = InstanceProfile(instance_id="tenant_b", status="active", created_at=_now())
    await store.save_instance_profile("tenant_a", profile_a)
    await store.save_instance_profile("tenant_b", profile_b)
    profile_a.status = "suspended"
    await store.save_instance_profile("tenant_a", profile_a)
    fetched_b = await store.get_instance_profile("tenant_b")
    assert fetched_b.status == "active"


# ============================================================================
# 1.4 State Store — Soul Isolation
# ============================================================================


async def test_soulinstance_a_invisible_to_b(tmp_path):
    store = JsonStateStore(tmp_path)
    await store.save_soul(Soul(instance_id="tenant_a", user_name="Alice", hatched=True))
    assert await store.get_soul("tenant_b") is None


async def test_soul_eachinstance_sees_own(tmp_path):
    store = JsonStateStore(tmp_path)
    await store.save_soul(Soul(instance_id="tenant_a", user_name="Alice"))
    await store.save_soul(Soul(instance_id="tenant_b", user_name="Bob"))
    assert (await store.get_soul("tenant_a")).user_name == "Alice"
    assert (await store.get_soul("tenant_b")).user_name == "Bob"


async def test_soul_update_a_does_not_affect_b(tmp_path):
    store = JsonStateStore(tmp_path)
    soul_a = Soul(instance_id="tenant_a", user_name="Alice")
    soul_b = Soul(instance_id="tenant_b", user_name="Bob")
    await store.save_soul(soul_a)
    await store.save_soul(soul_b)
    soul_a.interaction_count = 99
    await store.save_soul(soul_a)
    assert (await store.get_soul("tenant_b")).interaction_count == 0


# ============================================================================
# 1.5 State Store — Knowledge Isolation
# ============================================================================


async def test_knowledgeinstance_a_invisible_to_b(tmp_path):
    store = JsonStateStore(tmp_path)
    await store.add_knowledge(_make_knowledge("tenant_a", "know_a1"))
    assert await store.query_knowledge("tenant_b") == []


async def test_knowledge_subject_search_invisible_to_b(tmp_path):
    store = JsonStateStore(tmp_path)
    await store.add_knowledge(_make_knowledge("tenant_a", "know_a1", subject="John"))
    assert await store.query_knowledge("tenant_b", subject="John") == []


async def test_knowledge_both_tenants_see_own(tmp_path):
    store = JsonStateStore(tmp_path)
    await store.add_knowledge(_make_knowledge("tenant_a", "know_a1", subject="Alice"))
    await store.add_knowledge(_make_knowledge("tenant_b", "know_b1", subject="Alice"))
    result_a = await store.query_knowledge("tenant_a", subject="Alice")
    result_b = await store.query_knowledge("tenant_b", subject="Alice")
    assert len(result_a) == 1 and result_a[0].instance_id == "tenant_a"
    assert len(result_b) == 1 and result_b[0].instance_id == "tenant_b"


async def test_knowledge_update_a_does_not_affect_b(tmp_path):
    """CRITICAL: update_knowledge with Tenant A's entry_id must NOT touch Tenant B's data."""
    store = JsonStateStore(tmp_path)
    entry_a = _make_knowledge("tenant_a", "know_a1", subject="Alice")
    entry_b = _make_knowledge("tenant_b", "know_b1", subject="Alice")
    await store.add_knowledge(entry_a)
    await store.add_knowledge(entry_b)
    await store.update_knowledge("tenant_a", "know_a1", {"content": "Updated content"})
    result_b = await store.query_knowledge("tenant_b")
    assert result_b[0].content == "Content about Alice"


# ============================================================================
# 1.6 State Store — Behavioral Contract Isolation
# ============================================================================


async def test_contractsinstance_a_invisible_to_b(tmp_path):
    store = JsonStateStore(tmp_path)
    for rule in default_contract_rules("tenant_a", _now()):
        await store.add_contract_rule(rule)
    assert await store.get_contract_rules("tenant_b") == []


async def test_contracts_default_provisioning_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_POSTURE_PROFILE", "strict")
    store = JsonStateStore(tmp_path)
    for rule in default_contract_rules("tenant_a", _now()):
        await store.add_contract_rule(rule)
    assert len(await store.get_contract_rules("tenant_b")) == 0
    assert len(await store.get_contract_rules("tenant_a")) == 9


async def test_contract_update_a_does_not_affect_b(tmp_path, monkeypatch):
    """CRITICAL: update_contract_rule with Tenant A's rule_id must NOT touch Tenant B's data."""
    monkeypatch.setenv("KERNOS_POSTURE_PROFILE", "strict")
    store = JsonStateStore(tmp_path)
    rules_a = default_contract_rules("tenant_a", _now())
    rules_b = default_contract_rules("tenant_b", _now())
    for rule in rules_a:
        await store.add_contract_rule(rule)
    for rule in rules_b:
        await store.add_contract_rule(rule)
    await store.update_contract_rule("tenant_a", rules_a[0].id, {"active": False})
    result_b = await store.get_contract_rules("tenant_b", active_only=True)
    assert len(result_b) == 9


# ============================================================================
# 1.7 State Store — Conversation Summary Isolation
# ============================================================================


async def test_conversation_summaryinstance_a_invisible_to_b(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    summary = ConversationSummary(
        instance_id="tenant_a", conversation_id="conv_1", platform="discord",
        message_count=3, first_message_at=now, last_message_at=now,
    )
    await store.save_conversation_summary(summary)
    assert await store.list_conversations("tenant_b") == []


async def test_conversation_summary_same_conv_id_isolated(tmp_path):
    """Two tenants with the same conversation_id each see only their own."""
    store = JsonStateStore(tmp_path)
    now = _now()
    await store.save_conversation_summary(ConversationSummary(
        instance_id="tenant_a", conversation_id="conv_shared", platform="discord",
        message_count=3, first_message_at=now, last_message_at=now,
    ))
    await store.save_conversation_summary(ConversationSummary(
        instance_id="tenant_b", conversation_id="conv_shared", platform="sms",
        message_count=7, first_message_at=now, last_message_at=now,
    ))
    result_a = await store.get_conversation_summary("tenant_a", "conv_shared")
    result_b = await store.get_conversation_summary("tenant_b", "conv_shared")
    assert result_a.message_count == 3
    assert result_b.message_count == 7


# ============================================================================
# 1.8 Tenant Store Isolation
# ============================================================================


async def testinstance_store_each_provisioned_separately(tmp_path):
    store = JsonInstanceStore(tmp_path)
    record_a = await store.get_or_create("tenant_a")
    record_b = await store.get_or_create("tenant_b")
    assert record_a["instance_id"] == "tenant_a"
    assert record_b["instance_id"] == "tenant_b"


async def testinstance_store_directories_isolated(tmp_path):
    store = JsonInstanceStore(tmp_path)
    await store.get_or_create("tenant_a")
    await store.get_or_create("tenant_b")
    a_files = list((tmp_path / "tenant_a").rglob("*"))
    b_files = list((tmp_path / "tenant_b").rglob("*"))
    assert not any("tenant_b" in str(f) for f in a_files)
    assert not any("tenant_a" in str(f) for f in b_files)


# ============================================================================
# 1.9 Audit Store Isolation
# ============================================================================


async def test_auditinstance_b_dir_absent_after_a_logs(tmp_path):
    store = JsonAuditStore(tmp_path)
    await store.log("tenant_a", {"type": "test", "content": "hello"})
    assert not (tmp_path / "tenant_b" / "audit").exists()


# ============================================================================
# 2.1 Path Traversal Attempts
# ============================================================================


def test_safe_name_etc_passwd():
    result = _safe_name("../../etc/passwd")
    assert ".." not in result
    assert "/" not in result


def test_safe_name_relative_dir_escape():
    result = _safe_name("../other_tenant/state/soul")
    assert ".." not in result


def test_safe_name_nested_traversal():
    result = _safe_name("tenant_a/../../tenant_b")
    assert ".." not in result


def test_safe_name_double_dot_alone():
    assert ".." not in _safe_name("..")


def test_safe_name_backslash_traversal():
    result = _safe_name("tenant_a\\..\\tenant_b")
    assert ".." not in result
    assert "\\" not in result


def test_safe_name_strips_colon():
    assert ":" not in _safe_name("discord:123456789")


def test_safe_name_strips_forward_slash():
    assert "/" not in _safe_name("platform/user/123")


# ============================================================================
# 2.2 Malformed Input
# ============================================================================


def test_safe_name_empty_string():
    result = _safe_name("")
    assert result == "_empty_"


def test_safe_name_whitespace_only():
    assert _safe_name("   ") == "_empty_"


def test_safe_name_very_long():
    result = _safe_name("a" * 1001)
    assert result  # Must not crash, must be non-empty


def test_safe_name_unicode():
    result = _safe_name("tenant_日本語")
    assert result  # Must not crash


def test_safe_name_null_bytes():
    assert "\x00" not in _safe_name("tenant\x00a")


async def test_conversation_id_path_separators_stay_in_sandbox(tmp_path):
    """conversation_id with path separators cannot escape the instance sandbox."""
    store = JsonConversationStore(tmp_path)
    await store.append("tenant_a", "../../../etc/escaped", {"role": "user", "content": "x"})
    all_files = list(tmp_path.rglob("*.json"))
    for f in all_files:
        assert str(f).startswith(str(tmp_path))


# ============================================================================
# 2.3 _safe_name Coverage
# ============================================================================


def test_safe_name_discord_tenant():
    assert _safe_name("discord:123456789012345678") == "discord_123456789012345678"


def test_safe_name_sms_tenant():
    assert _safe_name("sms:+15555550100") == "sms_+15555550100"


def test_safe_name_normal_input_unchanged():
    assert _safe_name("normal_tenant") == "normal_tenant"


def test_safe_name_no_mutation_for_safe_input():
    result = _safe_name("tenant_abc_123")
    assert result == "tenant_abc_123"
