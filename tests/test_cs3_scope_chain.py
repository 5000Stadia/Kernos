"""Tests for SPEC-CS-3: Scope Chain Retrieval + Parent Briefing.

Covers: scope chain building, scope chain search with parent hits,
briefing production, briefing injection, list_child_spaces.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import KnowledgeEntry
from kernos.kernel.state_json import JsonStateStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_knowledge(kid: str, content: str, space: str = "") -> KnowledgeEntry:
    return KnowledgeEntry(
        id=kid, instance_id="t1", category="fact", subject="user",
        content=content, confidence="stated",
        source_event_id="", source_description="test",
        created_at=_now(), last_referenced=_now(), tags=[],
        context_space=space,
    )


# ---------------------------------------------------------------------------
# list_child_spaces
# ---------------------------------------------------------------------------


class TestListChildSpaces:
    async def test_returns_children(self, tmp_path):
        store = JsonStateStore(tmp_path)
        parent = ContextSpace(id="sp", instance_id="t1", name="General", created_at=_now())
        child1 = ContextSpace(id="c1", instance_id="t1", name="D&D", parent_id="sp", depth=1, created_at=_now())
        child2 = ContextSpace(id="c2", instance_id="t1", name="Work", parent_id="sp", depth=1, created_at=_now())
        other = ContextSpace(id="o1", instance_id="t1", name="Unrelated", created_at=_now())
        for s in [parent, child1, child2, other]:
            await store.save_context_space(s)

        children = await store.list_child_spaces("t1", "sp")
        ids = {c.id for c in children}
        assert ids == {"c1", "c2"}

    async def test_returns_empty_for_no_children(self, tmp_path):
        store = JsonStateStore(tmp_path)
        parent = ContextSpace(id="sp", instance_id="t1", name="General", created_at=_now())
        await store.save_context_space(parent)

        children = await store.list_child_spaces("t1", "sp")
        assert children == []

    async def test_excludes_archived(self, tmp_path):
        store = JsonStateStore(tmp_path)
        parent = ContextSpace(id="sp", instance_id="t1", name="General", created_at=_now())
        child = ContextSpace(id="c1", instance_id="t1", name="Old", parent_id="sp", status="archived", created_at=_now())
        for s in [parent, child]:
            await store.save_context_space(s)

        children = await store.list_child_spaces("t1", "sp")
        assert children == []


# ---------------------------------------------------------------------------
# Scope Chain Building
# ---------------------------------------------------------------------------


class TestScopeChain:
    async def test_flat_space_returns_self(self, tmp_path):
        from kernos.kernel.retrieval import RetrievalService
        store = JsonStateStore(tmp_path)
        general = ContextSpace(id="gen", instance_id="t1", name="General", created_at=_now())
        await store.save_context_space(general)

        svc = RetrievalService.__new__(RetrievalService)
        svc.state = store
        chain = await svc._build_scope_chain("t1", "gen")
        assert chain == ["gen"]

    async def test_child_walks_to_root(self, tmp_path):
        from kernos.kernel.retrieval import RetrievalService
        store = JsonStateStore(tmp_path)
        root = ContextSpace(id="root", instance_id="t1", name="General", created_at=_now())
        mid = ContextSpace(id="mid", instance_id="t1", name="Wedding", parent_id="root", depth=1, created_at=_now())
        leaf = ContextSpace(id="leaf", instance_id="t1", name="Henderson", parent_id="mid", depth=2, created_at=_now())
        for s in [root, mid, leaf]:
            await store.save_context_space(s)

        svc = RetrievalService.__new__(RetrievalService)
        svc.state = store
        chain = await svc._build_scope_chain("t1", "leaf")
        assert chain == ["leaf", "mid", "root"]

    async def test_handles_missing_parent(self, tmp_path):
        from kernos.kernel.retrieval import RetrievalService
        store = JsonStateStore(tmp_path)
        orphan = ContextSpace(id="orph", instance_id="t1", name="Orphan", parent_id="gone", created_at=_now())
        await store.save_context_space(orphan)

        svc = RetrievalService.__new__(RetrievalService)
        svc.state = store
        chain = await svc._build_scope_chain("t1", "orph")
        assert chain == ["orph"]  # Stops at missing parent


# ---------------------------------------------------------------------------
# Scope Chain in Knowledge Search
# ---------------------------------------------------------------------------


class TestScopeChainKnowledgeSearch:
    async def test_parent_knowledge_visible_in_child(self, tmp_path):
        from kernos.kernel.retrieval import RetrievalService
        store = JsonStateStore(tmp_path)

        # Create knowledge in parent space
        parent_ke = _make_knowledge("k1", "Budget is $45k", space="parent_space")
        child_ke = _make_knowledge("k2", "Henderson meeting notes", space="child_space")
        global_ke = _make_knowledge("k3", "User is named the design review", space="")
        await store.add_knowledge(parent_ke)
        await store.add_knowledge(child_ke)
        await store.add_knowledge(global_ke)

        # Build scope chain: child → parent
        scope_chain = ["child_space", "parent_space"]
        _scope = set(scope_chain)

        all_entries = await store.query_knowledge("t1", active_only=True)
        visible = [
            e for e in all_entries
            if e.context_space in _scope or e.context_space in ("", None)
        ]

        ids = {e.id for e in visible}
        assert "k1" in ids  # Parent knowledge visible
        assert "k2" in ids  # Child knowledge visible
        assert "k3" in ids  # Global knowledge visible


# ---------------------------------------------------------------------------
# Parent Briefing Production
# ---------------------------------------------------------------------------


class TestParentBriefing:
    def _make_handler(self, tmp_path):
        from kernos.messages.handler import MessageHandler
        handler = MessageHandler.__new__(MessageHandler)
        handler.state = JsonStateStore(tmp_path)
        handler.reasoning = MagicMock()
        handler.reasoning.complete_simple = AsyncMock(return_value="- Budget: $45k\n- Venue: Riverside Lodge")
        handler.compaction = MagicMock()
        handler.compaction.load_document = AsyncMock(return_value="## Living State\nBudget confirmed at $45k...")
        handler.compaction._space_dir = MagicMock(return_value=tmp_path / "comp" / "parent_space")
        return handler

    async def test_produces_briefing_for_child(self, tmp_path):
        handler = self._make_handler(tmp_path)
        tid = "t1"

        parent = ContextSpace(
            id="parent_space", instance_id=tid, name="Wedding Planning",
            space_type="domain", created_at=_now(),
        )
        child = ContextSpace(
            id="child_space", instance_id=tid, name="Henderson",
            space_type="subdomain", parent_id="parent_space", depth=2,
            description="Henderson wedding vendor coordination",
            created_at=_now(),
        )
        await handler.state.save_context_space(parent)
        await handler.state.save_context_space(child)

        await handler._produce_child_briefings(tid, "parent_space", parent)

        briefing_path = tmp_path / "comp" / "parent_space" / "briefing_child_space.md"
        assert briefing_path.exists()
        text = briefing_path.read_text()
        assert "Budget" in text or "$45k" in text

    async def test_no_briefings_without_children(self, tmp_path):
        handler = self._make_handler(tmp_path)
        tid = "t1"

        parent = ContextSpace(
            id="parent_space", instance_id=tid, name="General",
            space_type="general", created_at=_now(),
        )
        await handler.state.save_context_space(parent)

        await handler._produce_child_briefings(tid, "parent_space", parent)

        # LLM should not have been called
        handler.reasoning.complete_simple.assert_not_called()


class TestParentBriefingInjection:
    async def test_load_parent_briefing(self, tmp_path):
        from kernos.messages.handler import MessageHandler
        handler = MessageHandler.__new__(MessageHandler)
        handler.compaction = MagicMock()
        handler.compaction._space_dir = MagicMock(return_value=tmp_path / "comp" / "parent")

        # Write a briefing file
        briefing_dir = tmp_path / "comp" / "parent"
        briefing_dir.mkdir(parents=True)
        (briefing_dir / "briefing_child_1.md").write_text("- Budget: $45k\n- Venue: Riverside Lodge")

        result = await handler._load_parent_briefing("t1", "parent", "child_1")
        assert result is not None
        assert "Budget" in result
        assert "Riverside Lodge" in result

    async def test_load_missing_briefing_returns_none(self, tmp_path):
        from kernos.messages.handler import MessageHandler
        handler = MessageHandler.__new__(MessageHandler)
        handler.compaction = MagicMock()
        handler.compaction._space_dir = MagicMock(return_value=tmp_path / "comp" / "parent")

        result = await handler._load_parent_briefing("t1", "parent", "child_no_exist")
        assert result is None
