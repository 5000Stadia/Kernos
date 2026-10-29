"""Long-arc recall: remember finds Ledger entries, remember_details opens their log.

The design's recall chain (compaction prompt, remember_details schema): the
Ledger is a table of contents for the member's source logs; remember() finds
the entry, remember_details(log_NNN) reads exactly what was said. These tests
walk that chain end to end on disk, and check it never crosses members.
"""
from unittest.mock import AsyncMock, MagicMock

from kernos.kernel.compaction import CompactionService
from kernos.kernel.conversation_log import ConversationLogger
from kernos.kernel.reasoning import ReasoningService
from kernos.kernel.retrieval import RetrievalService, bm25_rank
from kernos.kernel.tokens import EstimateTokenAdapter


def _doc(*entries: str, state: str = "State.") -> str:
    return "# Ledger\n\n" + "\n\n".join(entries) + f"\n\n# Living State\n\n{state}\n"


E1 = "## Compaction #1 (source: log_001) — 2026-05-01T10:00 → 2026-05-01T11:00\n\n- Alex birthday: April 12, sushi, $50/pp."
E2 = "## Compaction #2 (source: log_002) — 2026-05-03T10:00 → 2026-05-03T11:00\n\n- Kitchen renovation: budget 12k, contractor Henderson."
E3 = "## Compaction #3 (source: log_003) — 2026-05-05T10:00 → 2026-05-05T11:00\n\n- Guitar: knows G/C/D/Em, barre chords hurt."


def _services(tmp_path):
    compaction = CompactionService(
        state=MagicMock(), reasoning=None,
        token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
    )
    state = MagicMock()
    state.query_knowledge = AsyncMock(return_value=[])
    state.get_context_space = AsyncMock(return_value=None)
    retrieval = RetrievalService(
        state=state, embedding_service=None, embedding_store=MagicMock(),
        compaction=compaction, reasoning=MagicMock(),
    )
    return compaction, retrieval


def _write(compaction, space, member, text, archive=None):
    d = compaction._space_dir("t1", space, member)
    d.mkdir(parents=True, exist_ok=True)
    if archive:
        (d / "archives").mkdir(exist_ok=True)
        (d / "archives" / archive).write_text(text, encoding="utf-8")
    else:
        (d / "active_document.md").write_text(text, encoding="utf-8")


class TestBm25:
    def test_ranks_the_matching_entry_first(self):
        ranked = bm25_rank("what was the kitchen budget", [E1, E2, E3])
        assert ranked[0][0] == 1
        assert {i for i, _ in ranked} == {1}

    def test_no_shared_terms_returns_nothing(self):
        assert bm25_rank("volcano", [E1, E2]) == []
        assert bm25_rank("the and of", [E1]) == []


class TestLedgerSearch:
    async def test_finds_old_entries_including_sealed_archives(self, tmp_path):
        compaction, retrieval = _services(tmp_path)
        # Sealed archive holds #1 and #2; rotation carried #2 forward.
        _write(compaction, "sp1", "mem_a", _doc(E1, E2), archive="compaction_archive_001.md")
        _write(compaction, "sp1", "mem_a", _doc(E2, E3))
        entries = await compaction.load_all_ledger_entries("t1", "sp1", "mem_a")
        assert entries == [E1, E2, E3]
        hits = await retrieval._search_ledger("t1", "Alex birthday dinner", "sp1", ["sp1"], "mem_a")
        assert hits and hits[0][2] == E1

    async def test_never_returns_another_members_ledger(self, tmp_path):
        compaction, retrieval = _services(tmp_path)
        _write(compaction, "sp1", "mem_b", _doc(E2))
        _write(compaction, "sp1", "", _doc(E2))  # legacy shared document
        hits = await retrieval._search_ledger("t1", "kitchen budget", "sp1", ["sp1"], "mem_a")
        assert hits == []

    async def test_remember_returns_entry_and_points_to_source(self, tmp_path):
        compaction, retrieval = _services(tmp_path)
        _write(compaction, "sp1", "mem_a", _doc(E1, E2, E3))
        text = await retrieval.search("t1", "kitchen contractor", "sp1",
                                      requesting_member_id="mem_a")
        assert "From the Ledger:" in text
        assert "contractor Henderson" in text and "source: log_002" in text
        assert "remember_details" in text

    async def test_parent_space_hit_names_its_space(self, tmp_path):
        compaction, retrieval = _services(tmp_path)
        _write(compaction, "root", "mem_a", _doc(E2))
        retrieval._build_scope_chain = AsyncMock(return_value=["child", "root"])
        text = await retrieval.search("t1", "kitchen budget", "child",
                                      requesting_member_id="mem_a")
        assert "From the Ledger [space: root]:" in text


class TestRememberDetailsFollowsTheEntry:
    def _reasoning(self, conv):
        svc = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        handler = MagicMock()

        async def _read(tid, sid, num, member_id=""):
            return await conv.read_log_text(tid, sid, num, member_id=member_id)
        handler.read_log_text = _read
        svc.set_handler(handler)
        return svc

    async def test_opens_the_log_in_the_named_space(self, tmp_path):
        conv = ConversationLogger(data_dir=str(tmp_path))
        await conv.append("t1", "root", "user", "discord",
                          "The contractor is Henderson, budget 12k", member_id="mem_a")
        svc = self._reasoning(conv)
        text = await svc._handle_remember_details(
            "t1", "child", {"source_ref": "log_001", "space": "root", "query": "Henderson"},
            member_id="mem_a",
        )
        assert "Henderson, budget 12k" in text

    async def test_rejects_a_space_that_is_not_an_id(self, tmp_path):
        conv = ConversationLogger(data_dir=str(tmp_path))
        svc = self._reasoning(conv)
        text = await svc._handle_remember_details(
            "t1", "child", {"source_ref": "log_001", "space": "../../etc"},
            member_id="mem_a",
        )
        assert "not a space id" in text
