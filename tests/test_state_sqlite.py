"""Tests for SqliteStateStore — ensures parity with JsonStateStore."""
import pytest
from datetime import datetime, timezone

from kernos.kernel.state import (
    CovenantRule, ConversationSummary, KnowledgeEntry, Soul, InstanceProfile,
    default_covenant_rules,
)
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state_sqlite import SqliteStateStore


def _now():
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
async def store(tmp_path):
    s = SqliteStateStore(str(tmp_path))
    yield s
    await s.close_all()


class TestSoul:
    async def test_save_and_get(self, store):
        soul = Soul(instance_id="t1", agent_name="Kernos", interaction_count=5)
        await store.save_soul(soul)
        loaded = await store.get_soul("t1")
        assert loaded is not None
        assert loaded.agent_name == "Kernos"
        assert loaded.interaction_count == 5

    async def test_get_nonexistent(self, store):
        assert await store.get_soul("nonexistent") is None

    async def test_upsert(self, store):
        soul = Soul(instance_id="t1", agent_name="V1")
        await store.save_soul(soul)
        soul.agent_name = "V2"
        await store.save_soul(soul)
        loaded = await store.get_soul("t1")
        assert loaded.agent_name == "V2"


class TestInstanceProfile:
    async def test_save_and_get(self, store):
        profile = InstanceProfile(instance_id="t1", status="active", created_at=_now())
        await store.save_instance_profile("t1", profile)
        loaded = await store.get_instance_profile("t1")
        assert loaded is not None
        assert loaded.status == "active"


class TestKnowledge:
    async def test_add_and_query(self, store):
        entry = KnowledgeEntry(
            id="k1", instance_id="t1", subject="user", content="Likes sushi",
            category="preference", confidence="high", source_event_id="",
            source_description="test", last_referenced=_now(), tags=[],
            created_at=_now(),
        )
        await store.add_knowledge(entry)
        results = await store.query_knowledge("t1", subject="user", limit=10)
        assert len(results) == 1
        assert results[0].content == "Likes sushi"

    async def test_update_knowledge(self, store):
        entry = KnowledgeEntry(
            id="k2", instance_id="t1", subject="user", content="Original",
            category="fact", confidence="high", source_event_id="",
            source_description="test", last_referenced=_now(), tags=[],
            created_at=_now(),
        )
        await store.add_knowledge(entry)
        await store.update_knowledge("t1", "k2", {"content": "Updated"})
        loaded = await store.get_knowledge_entry("t1", "k2")
        assert loaded.content == "Updated"

    async def test_hash_lookup(self, store):
        entry = KnowledgeEntry(
            id="k3", instance_id="t1", subject="user", content="Test",
            category="fact", confidence="high", source_event_id="",
            source_description="test", last_referenced=_now(), tags=[],
            content_hash="abc123", created_at=_now(),
        )
        await store.add_knowledge(entry)
        hashes = await store.get_knowledge_hashes("t1")
        assert "abc123" in hashes
        found = await store.get_knowledge_by_hash("t1", "abc123")
        assert found is not None
        assert found.id == "k3"

    async def test_foresight_query(self, store):
        entry = KnowledgeEntry(
            id="k4", instance_id="t1", subject="user", content="Dentist",
            category="event", confidence="high", source_event_id="",
            source_description="test", last_referenced=_now(), tags=[],
            foresight_signal="dentist", foresight_expires="2026-04-15T00:00:00+00:00",
            created_at=_now(),
        )
        await store.add_knowledge(entry)
        results = await store.query_knowledge_by_foresight(
            "t1", expires_before="2026-04-16T00:00:00+00:00",
            expires_after="2026-04-14T00:00:00+00:00",
        )
        assert len(results) == 1


class TestCovenants:
    async def test_add_and_get(self, store):
        rules = default_covenant_rules("t1", _now())
        for r in rules:
            await store.add_contract_rule(r)
        loaded = await store.get_contract_rules("t1")
        assert len(loaded) == 9

    async def test_scope_filtering(self, store):
        r = CovenantRule(
            id="r1", instance_id="t1", capability="general",
            rule_type="preference", description="Space-scoped rule",
            active=True, source="user_stated", context_space="space_abc",
            created_at=_now(), updated_at=_now(),
        )
        await store.add_contract_rule(r)
        # Query with scope including space_abc
        results = await store.query_covenant_rules("t1", context_space_scope=["space_abc", None])
        assert any(x.id == "r1" for x in results)
        # Query with scope NOT including space_abc
        results = await store.query_covenant_rules("t1", context_space_scope=["space_xyz", None])
        assert not any(x.id == "r1" for x in results)


class TestContextSpaces:
    async def test_save_and_list(self, store):
        space = ContextSpace(id="sp1", instance_id="t1", name="General", is_default=True)
        await store.save_context_space(space)
        spaces = await store.list_context_spaces("t1")
        assert len(spaces) == 1
        assert spaces[0].name == "General"

    async def test_update(self, store):
        space = ContextSpace(id="sp2", instance_id="t1", name="Old Name")
        await store.save_context_space(space)
        await store.update_context_space("t1", "sp2", {"name": "New Name"})
        loaded = await store.get_context_space("t1", "sp2")
        assert loaded.name == "New Name"


class TestWhispers:
    async def test_save_and_get_pending(self, store):
        from kernos.kernel.awareness import Whisper, generate_whisper_id
        w = Whisper(
            whisper_id=generate_whisper_id(),
            insight_text="Test whisper",
            delivery_class="ambient",
            source_space_id="sp1",
            target_space_id="sp1",
            supporting_evidence=["test"],
            reasoning_trace="test",
            knowledge_entry_id="",
            foresight_signal="test_signal",
            created_at=_now(),
        )
        await store.save_whisper("t1", w)
        pending = await store.get_pending_whispers("t1")
        assert len(pending) == 1
        assert pending[0].insight_text == "Test whisper"

    async def test_dedup(self, store):
        from kernos.kernel.awareness import Whisper, generate_whisper_id
        for i in range(3):
            w = Whisper(
                whisper_id=generate_whisper_id(),
                insight_text=f"Whisper {i}",
                delivery_class="ambient",
                source_space_id="sp1",
                target_space_id="sp1",
                supporting_evidence=[],
                reasoning_trace="",
                knowledge_entry_id="",
                foresight_signal="same_signal",
                created_at=_now(),
            )
            await store.save_whisper("t1", w)
        pending = await store.get_pending_whispers("t1")
        assert len(pending) == 1  # Dedup kept only the first


class TestSpaceNotices:
    async def test_append_and_drain(self, store):
        await store.append_space_notice("t1", "sp1", "Test notice")
        await store.append_space_notice("t1", "sp1", "Second notice")
        notices = await store.drain_space_notices("t1", "sp1")
        assert len(notices) == 2
        # Drain should clear them
        notices2 = await store.drain_space_notices("t1", "sp1")
        assert len(notices2) == 0


class TestConversationSummaries:
    async def test_save_and_get(self, store):
        summary = ConversationSummary(
            instance_id="t1", conversation_id="c1", platform="discord",
            message_count=10, first_message_at=_now(), last_message_at=_now(),
        )
        await store.save_conversation_summary(summary)
        loaded = await store.get_conversation_summary("t1", "c1")
        assert loaded is not None
        assert loaded.message_count == 10


class TestTenantIsolation:
    async def test_knowledge_isolated(self, store):
        e1 = KnowledgeEntry(
            id="k_a", instance_id="tenant_a", subject="user", content="A's fact",
            category="fact", confidence="high", source_event_id="",
            source_description="test", last_referenced=_now(), tags=[],
            created_at=_now(),
        )
        e2 = KnowledgeEntry(
            id="k_b", instance_id="tenant_b", subject="user", content="B's fact",
            category="fact", confidence="high", source_event_id="",
            source_description="test", last_referenced=_now(), tags=[],
            created_at=_now(),
        )
        await store.add_knowledge(e1)
        await store.add_knowledge(e2)
        a_results = await store.query_knowledge("tenant_a", limit=10)
        b_results = await store.query_knowledge("tenant_b", limit=10)
        assert len(a_results) == 1
        assert a_results[0].content == "A's fact"
        assert len(b_results) == 1
        assert b_results[0].content == "B's fact"
