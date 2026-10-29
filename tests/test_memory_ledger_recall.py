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


class TestLedgerRebuild:
    """Entries lost to the old whole-document rewrite are rebuilt from their logs."""

    async def _setup(self, tmp_path):
        conv = ConversationLogger(data_dir=str(tmp_path))
        for n in (1, 2, 3):
            if n > 1:
                old, _ = await conv.roll_log("t1", "sp1", member_id="mem_a")
                await conv.seed_from_previous("t1", "sp1", old, tail_entries=1, member_id="mem_a")
            await conv.append("t1", "sp1", "user", "discord", f"topic {n} discussed", member_id="mem_a")
        await conv.roll_log("t1", "sp1", member_id="mem_a")  # log_004 is current
        reasoning = MagicMock()
        reasoning.complete_simple = AsyncMock(return_value=(
            "# New Ledger Entry\n\n- Rebuilt topic.\n\n# Living State\n\nIGNORED STATE\n\nSEED_DEPTH: 3\n"))
        compaction = CompactionService(state=MagicMock(), reasoning=reasoning,
                                       token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path))
        e2 = "## Compaction #2 (source: log_002) — 2026-05-03T10:00 → 2026-05-03T11:00\n\n- Topic two."
        _write(compaction, "sp1", "mem_a", _doc(e2, state="Live state."))
        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(id="sp1", instance_id="t1", name="General")
        return conv, compaction, reasoning, space, e2

    async def test_rebuilds_uncited_closed_logs_in_order(self, tmp_path):
        conv, compaction, reasoning, space, e2 = await self._setup(tmp_path)
        rebuilt = await compaction.rebuild_missing_entries("t1", "sp1", space, conv, member_id="mem_a")
        assert rebuilt == [1, 3]
        doc = await compaction.load_document("t1", "sp1", "mem_a")
        entries = compaction._parse_ledger_entries(doc)
        assert [e.split(" (source")[0] for e in entries] == [
            "## Compaction #1", "## Compaction #2", "## Compaction #3"]
        assert entries[1] == e2
        assert entries[0].split("\n")[0].endswith("(rebuilt)")
        assert compaction._extract_living_state(doc) == "Live state."
        # log_003's seeded head (topic 2) is not recompacted; only topic 3 is.
        span3 = reasoning.complete_simple.call_args_list[1].kwargs["user_content"]
        assert "topic 3 discussed" in span3 and "topic 2 discussed" not in span3
        # Running it again finds nothing missing.
        assert await compaction.rebuild_missing_entries("t1", "sp1", space, conv, member_id="mem_a") == []

    async def test_never_reads_another_members_logs(self, tmp_path):
        conv, compaction, reasoning, space, _ = await self._setup(tmp_path)
        _write(compaction, "sp1", "mem_b", _doc("## Compaction #9 (source: log_009)\n\n- B."))
        assert await compaction.rebuild_missing_entries("t1", "sp1", space, conv, member_id="mem_b") == []
        reasoning.complete_simple.assert_not_called()


class TestStripSeededHead:
    def test_strips_the_run_equal_to_the_previous_tail(self):
        from kernos.kernel.conversation_log import strip_seeded_head
        prev = ("[2026-05-01T10:00:00] [user] [discord] a\n"
                "[2026-05-01T10:01:00] [system] [receipt] b\n")
        # Older seeds relabelled system lines as assistant; content and time still match.
        cur = ("[2026-05-01T10:01:00] [assistant] [receipt] b\n"
               "[2026-05-02T09:00:00] [user] [discord] c\n")
        assert strip_seeded_head(cur, prev) == "[2026-05-02T09:00:00] [user] [discord] c\n"
        assert strip_seeded_head(cur, None) == cur
        assert strip_seeded_head("[2026-05-03T00:00:00] [user] [discord] z\n", prev).endswith("z\n")


async def test_old_archive_living_state_remains_searchable_without_index(tmp_path):
    compaction, retrieval = _services(tmp_path)
    _write(compaction, "sp1", "mem_a", _doc(E1, state="The obsidian beacon uses access code SILVER-29."),
           archive="compaction_archive_001.md")
    text = await retrieval.search("t1", "obsidian beacon", "sp1", requesting_member_id="mem_a")
    assert "SILVER-29" in text and "Historical archived state" in text
    assert "compaction_archive_001" in text and "sp1" in text
    assert len(text) <= 6000
    other = await retrieval.search("t1", "obsidian beacon", "sp1", requesting_member_id="mem_b")
    assert "SILVER-29" not in other


async def test_structured_state_intercept_shares_final_output_budget(tmp_path):
    from unittest.mock import patch
    _, retrieval = _services(tmp_path)
    with patch("kernos.kernel.introspection.build_user_truth_view",
               new=AsyncMock(return_value="Synthetic state. " * 1000)):
        text = await retrieval.search("t1", "notification settings", "sp1", requesting_member_id="mem_a")
    assert len(text) <= 6000 and "omitted" in text
