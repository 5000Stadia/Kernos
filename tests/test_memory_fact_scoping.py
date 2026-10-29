"""Fact store member scoping, ordering, and harvest versioning.

memory.md promises facts that are per-member, reconciled with stable IDs,
and archived (not overwritten) when superseded. These tests plant two
members' facts and check that each member's queries and harvests see and
change only their own.
"""
import json

import aiosqlite
import pytest

from kernos.kernel.fact_harvest import harvest_facts
from kernos.kernel.state import KnowledgeEntry
from kernos.kernel.state_json import JsonStateStore
from kernos.kernel.state_sqlite import SqliteStateStore

INST = "inst1"


def _fact(fid, owner, content, *, archetype="structural", updated="2026-01-01T00:00:00",
          space="", active=True):
    return KnowledgeEntry(
        id=fid, instance_id=INST, category="fact", subject="user", content=content,
        confidence="stated", source_event_id="", source_description="test",
        created_at=updated, last_referenced=updated, tags=[],
        lifecycle_archetype=archetype, owner_member_id=owner, context_space=space,
        active=active,
    )


@pytest.fixture(params=["sqlite", "json"])
async def store(request, tmp_path):
    if request.param == "sqlite":
        s = SqliteStateStore(str(tmp_path))
        yield s
        await s.close_all()
    else:
        yield JsonStateStore(str(tmp_path))


class TestMemberScoping:
    async def test_member_query_excludes_other_members(self, store):
        await store.add_knowledge(_fact("a1", "mem_a", "A's fact"))
        await store.add_knowledge(_fact("b1", "mem_b", "B's fact"))
        got = await store.query_knowledge(INST, member_id="mem_a", limit=50)
        assert [e.id for e in got] == ["a1"]

    async def test_ownerless_facts_are_not_shared(self, store):
        await store.add_knowledge(_fact("x1", "", "legacy fact"))
        assert await store.query_knowledge(INST, member_id="mem_b", limit=50) == []

    async def test_attribute_unowned_gives_legacy_facts_to_owner(self, store):
        await store.add_knowledge(_fact("x1", "", "legacy fact"))
        await store.add_knowledge(_fact("b1", "mem_b", "B's fact"))
        assert await store.attribute_unowned_knowledge(INST, "mem_owner") == 1
        owner = await store.query_knowledge(INST, member_id="mem_owner", limit=50)
        assert [e.id for e in owner] == ["x1"]
        assert [e.id for e in await store.query_knowledge(
            INST, member_id="mem_b", limit=50)] == ["b1"]

    async def test_inactive_entry_is_stored_inactive(self, store):
        await store.add_knowledge(_fact("a1", "mem_a", "old", active=False))
        assert await store.query_knowledge(INST, member_id="mem_a", limit=50) == []
        got = await store.query_knowledge(INST, member_id="mem_a", active_only=False, limit=50)
        assert [e.id for e in got] == ["a1"]


class TestSqliteColumns:
    async def test_columns_mirror_owner_and_space(self, tmp_path):
        store = SqliteStateStore(str(tmp_path))
        try:
            await store.add_knowledge(_fact("a1", "mem_a", "x", space="space_1"))
            db = await store._db(INST)
            async with db.execute(
                "SELECT member_id, space_id FROM knowledge WHERE id='a1'"
            ) as cur:
                row = await cur.fetchone()
            assert (row[0], row[1]) == ("mem_a", "space_1")
        finally:
            await store.close_all()

    async def test_backfill_fills_columns_of_old_rows(self, tmp_path):
        store = SqliteStateStore(str(tmp_path))
        await store.add_knowledge(_fact("a1", "mem_a", "x", space="space_1"))
        db = await store._db(INST)
        # Simulate a row written before the columns were filled.
        await db.execute("UPDATE knowledge SET member_id='', space_id=''")
        await db.commit()
        await store.close_all()

        reopened = SqliteStateStore(str(tmp_path))
        try:
            got = await reopened.query_knowledge(INST, member_id="mem_a", limit=50)
            assert [e.id for e in got] == ["a1"]
            assert got[0].context_space == "space_1"
        finally:
            await reopened.close_all()

    async def test_window_is_identity_first_then_most_recent(self, tmp_path):
        store = SqliteStateStore(str(tmp_path))
        try:
            await store.add_knowledge(_fact("old", "mem_a", "o", updated="2026-01-01"))
            await store.add_knowledge(_fact("new", "mem_a", "n", updated="2026-03-01"))
            await store.add_knowledge(_fact("idn", "mem_a", "i", archetype="identity",
                                            updated="2025-01-01"))
            got = await store.query_knowledge(INST, member_id="mem_a", limit=2)
            assert [e.id for e in got] == ["idn", "new"]
        finally:
            await store.close_all()


class _FakeReasoning:
    """complete_simple returns the queued JSON for the primary call, then {} ."""

    def __init__(self, primary: dict):
        self._primary = primary
        self.calls: list[str] = []

    async def complete_simple(self, *, system_prompt, user_content, **kw):
        self.calls.append(user_content)
        if len(self.calls) == 1:
            return json.dumps(self._primary)
        return json.dumps({"stewardship": "", "operational_insight": ""})


class _FakeEmbedder:
    def __init__(self):
        self.saved: dict[str, list[float]] = {}

    async def embed(self, text):
        return [float(len(text)), 1.0]

    async def save(self, instance_id, entry_id, vec):
        self.saved[entry_id] = vec


async def _harvest(store, primary, member="mem_a", space="space_1", emb=None):
    reasoning = _FakeReasoning(primary)
    out = await harvest_facts(
        reasoning, store, None, INST, space, "user: some conversation",
        member_id=member, embedding_service=emb, embedding_store=emb,
    )
    return out, reasoning


class TestHarvestWriters:
    async def test_reconciliation_sees_only_own_facts(self, store):
        await store.add_knowledge(_fact("a1", "mem_a", "A lives in Lisbon"))
        await store.add_knowledge(_fact("b1", "mem_b", "B lives in Oslo"))
        _, reasoning = await _harvest(store, {"add": [], "update": [], "reinforce": []})
        assert "A lives in Lisbon" in reasoning.calls[0]
        assert "B lives in Oslo" not in reasoning.calls[0]

    async def test_add_sets_scope_and_embeds(self, store):
        emb = _FakeEmbedder()
        out, _ = await _harvest(store, {"add": [
            {"content": "Name is Ada", "archetype": "identity", "confidence": "stated",
             "subject": "user", "sensitivity": "open", "scope": "member"},
            {"content": "Kitchen budget is 12k", "archetype": "contextual",
             "confidence": "stated", "subject": "user", "sensitivity": "contextual",
             "scope": "space"},
        ], "update": [], "reinforce": []}, emb=emb)
        assert out["adds"] == 2
        got = {e.content: e for e in await store.query_knowledge(
            INST, member_id="mem_a", limit=50)}
        assert got["Name is Ada"].context_space == ""
        assert got["Kitchen budget is 12k"].context_space == "space_1"
        assert all(e.owner_member_id == "mem_a" for e in got.values())
        assert all(e.content_hash for e in got.values())
        assert set(emb.saved) == {e.id for e in got.values()}

    async def test_update_keeps_id_and_archives_old_version(self, store):
        await store.add_knowledge(_fact("a1", "mem_a", "Works at Acme"))
        emb = _FakeEmbedder()
        out, _ = await _harvest(store, {"add": [], "update": [
            {"id": "a1", "new_content": "Works at Globex", "reason": "changed jobs"},
        ], "reinforce": []}, emb=emb)
        assert out["updates"] == 1
        current = await store.get_knowledge_entry(INST, "a1")
        assert current.content == "Works at Globex" and current.active
        assert current.supersedes
        old = await store.get_knowledge_entry(INST, current.supersedes)
        assert old.content == "Works at Acme"
        assert not old.active and old.invalid_at
        assert old.owner_member_id == "mem_a"
        active = await store.query_knowledge(INST, member_id="mem_a", limit=50)
        assert [e.id for e in active] == ["a1"]
        assert "a1" in emb.saved

    async def test_second_update_extends_the_chain(self, store):
        await store.add_knowledge(_fact("a1", "mem_a", "v1"))
        await _harvest(store, {"add": [], "update": [
            {"id": "a1", "new_content": "v2", "reason": ""}], "reinforce": []})
        await _harvest(store, {"add": [], "update": [
            {"id": "a1", "new_content": "v3", "reason": ""}], "reinforce": []})
        v3 = await store.get_knowledge_entry(INST, "a1")
        v2 = await store.get_knowledge_entry(INST, v3.supersedes)
        v1 = await store.get_knowledge_entry(INST, v2.supersedes)
        assert (v3.content, v2.content, v1.content) == ("v3", "v2", "v1")

    async def test_cannot_update_or_reinforce_another_members_fact(self, store):
        await store.add_knowledge(_fact("b1", "mem_b", "B lives in Oslo"))
        out, _ = await _harvest(store, {"add": [], "update": [
            {"id": "b1", "new_content": "B lives in Rome", "reason": ""},
        ], "reinforce": [{"id": "b1"}]})
        assert out["updates"] == 0 and out["reinforces"] == 0
        b1 = await store.get_knowledge_entry(INST, "b1")
        assert b1.content == "B lives in Oslo"
        assert b1.storage_strength == 1.0 and b1.reinforcement_count == 1

    async def test_reinforce_strengthens(self, store):
        await store.add_knowledge(_fact("a1", "mem_a", "Runs on Sundays"))
        out, _ = await _harvest(store, {"add": [], "update": [],
                                        "reinforce": [{"id": "a1"}]})
        assert out["reinforces"] == 1
        a1 = await store.get_knowledge_entry(INST, "a1")
        assert a1.storage_strength == 2.0
        assert a1.reinforcement_count == 2
        assert a1.last_reinforced_at
