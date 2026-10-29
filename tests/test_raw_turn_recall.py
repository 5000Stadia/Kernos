"""Remember recalls closed source conversations without losing their provenance.

All source material is synthetic and lives under pytest's temporary directory.
Assertions use the public final text, not raw-search implementation helpers.
"""
import json
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.compaction import CompactionService
from kernos.kernel.conversation_log import ConversationLogger
from kernos.kernel.retrieval import RetrievalService
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import KnowledgeEntry
from kernos.kernel.tokens import EstimateTokenAdapter


INSTANCE = "test_instance"
MEMBER = "member_a"
SPACE = "workshop"


@pytest.fixture(autouse=True)
def raw_search_defaults_on(monkeypatch):
    monkeypatch.delenv("KERNOS_MEMORY_RAW_SEARCH", raising=False)


def _services(tmp_path, *, parents=None, facts=()):
    parents = parents or {SPACE: ""}
    spaces = {
        sid: ContextSpace(id=sid, instance_id=INSTANCE, name=sid, parent_id=parent)
        for sid, parent in parents.items()
    }
    state = MagicMock()
    state.get_context_space = AsyncMock(side_effect=lambda iid, sid: spaces.get(sid))
    state.query_knowledge = AsyncMock(return_value=list(facts))
    state.query_entity_nodes = AsyncMock(return_value=[])
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value="none")
    compaction = CompactionService(
        state=state, reasoning=reasoning, token_adapter=EstimateTokenAdapter(),
        data_dir=str(tmp_path),
    )
    service = RetrievalService(
        state=state, embedding_service=None, embedding_store=MagicMock(),
        compaction=compaction, reasoning=reasoning,
    )
    # Keep the behavioral tests executable on the pre-R8 constructor too.
    service.conversation_logger = ConversationLogger(data_dir=str(tmp_path))
    return service, compaction


def _turn(speaker, content, minute=0, *, day=1):
    return f"[2026-05-{day:02d}T10:{minute:02d}:00+00:00] [{speaker}] [test] {content}\n"


def _write_log(service, number, *turns, space=SPACE, member=MEMBER, current=99):
    directory = service.conversation_logger._logs_dir(INSTANCE, space, member)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"log_{number:03d}.txt").write_text("".join(turns), encoding="utf-8")
    if current is not None:
        (directory / "meta.json").write_text(
            json.dumps({"current_log": current, "current_log_tokens_est": 0}),
            encoding="utf-8",
        )


def _write_ledger(compaction, *entries, space=SPACE, member=MEMBER):
    directory = compaction._space_dir(INSTANCE, space, member)
    directory.mkdir(parents=True, exist_ok=True)
    text = "# Ledger\n\n" + "\n\n".join(entries) + "\n\n# Living State\n\nSynthetic state.\n"
    (directory / "active_document.md").write_text(text, encoding="utf-8")


def _fact(number, content):
    return KnowledgeEntry(
        id=f"fact_{number}", instance_id=INSTANCE, category="fact", subject="user",
        content=content, confidence="stated", source_event_id="",
        source_description="synthetic test", created_at="2026-05-01T10:00:00+00:00",
        last_referenced="2026-05-01T10:00:00+00:00", tags=[],
        context_space=SPACE, owner_member_id=MEMBER,
    )


async def _search(service, query, *, space=SPACE, member=MEMBER):
    return await service.search(INSTANCE, query, space, requesting_member_id=member)


def _assert_turn_reference(text, number):
    # Allow either individual labels or an inclusive source turn range.
    individual = re.search(rf"\bturn\s*(?:#|:)?\s*{number}\b", text, re.I)
    ranges = re.findall(r"\bturns?\s*(?:#|:)?\s*(\d+)\s*[-–—]\s*(\d+)", text, re.I)
    assert individual or any(int(start) <= number <= int(end) for start, end in ranges), text


async def test_default_raw_recall_keeps_question_answer_and_detail_after_600_chars(tmp_path):
    service, _ = _services(tmp_path)
    question = "How does the amberwheel latch open?"
    answer = "First inspect the hinge. " + "The outer panel stays level. " * 28
    answer += "The amberwheel release code is COPPER-47; lift the lower pin."
    _write_log(service, 7, _turn("user", question, 3), _turn("assistant", answer, 4))

    text = await _search(service, "amberwheel latch")

    assert question in text
    assert answer in text  # A small complete window fits, so preserve all of it.
    assert "COPPER-47" in text
    assert "user" in text and "assistant" in text
    assert "2026-05-01T10:03:00+00:00" in text
    assert "2026-05-01T10:04:00+00:00" in text
    assert SPACE in text and "log_007" in text
    _assert_turn_reference(text, 1)
    _assert_turn_reference(text, 2)


async def test_kill_switch_removes_raw_but_keeps_facts_and_ledger(tmp_path, monkeypatch):
    service, compaction = _services(tmp_path, facts=[_fact(1, "amberwheel FACT-KEPT")])
    _write_ledger(compaction, "## Compaction #1 (source: log_001)\n\n- amberwheel LEDGER-KEPT")
    _write_log(service, 1, _turn("user", "amberwheel RAW-ONLY-CLOSED"))
    enabled = await _search(service, "amberwheel")
    assert "RAW-ONLY-CLOSED" in enabled

    monkeypatch.setenv("KERNOS_MEMORY_RAW_SEARCH", "0")
    disabled = await _search(service, "amberwheel")

    assert "RAW-ONLY-CLOSED" not in disabled
    assert "FACT-KEPT" in disabled and "LEDGER-KEPT" in disabled


async def test_metadata_current_log_is_excluded_even_when_not_the_newest_file(tmp_path):
    service, _ = _services(tmp_path)
    _write_log(service, 1, _turn("user", "amberwheel CLOSED-ONE"), current=2)
    _write_log(service, 2, _turn("user", "amberwheel CURRENT-SECRET"), current=2)
    _write_log(service, 3, _turn("user", "amberwheel CLOSED-THREE"), current=2)

    text = await _search(service, "amberwheel")

    assert "CLOSED-ONE" in text and "CLOSED-THREE" in text
    assert "CURRENT-SECRET" not in text


async def test_missing_metadata_conservatively_excludes_newest_log(tmp_path):
    service, _ = _services(tmp_path)
    _write_log(service, 1, _turn("user", "amberwheel OLD-CLOSED"), current=None)
    _write_log(service, 8, _turn("user", "amberwheel NEWEST-CURRENT"), current=None)

    text = await _search(service, "amberwheel")

    assert "OLD-CLOSED" in text
    assert "NEWEST-CURRENT" not in text


async def test_member_raw_search_never_falls_back_to_legacy_or_another_member(tmp_path):
    service, _ = _services(tmp_path)
    _write_log(service, 1, _turn("user", "amberwheel OWN-CLOSED"))
    _write_log(service, 1, _turn("user", "amberwheel OTHER-MEMBER-SECRET"), member="member_b")
    _write_log(service, 1, _turn("user", "amberwheel LEGACY-SECRET"), member="")

    own = await _search(service, "amberwheel")
    missing = await _search(service, "amberwheel", member="member_without_logs")

    assert "OWN-CLOSED" in own
    for text in (own, missing):
        assert "OTHER-MEMBER-SECRET" not in text and "LEGACY-SECRET" not in text
    assert "OWN-CLOSED" not in missing


async def test_anonymous_search_never_reads_raw_logs(tmp_path):
    service, _ = _services(tmp_path)
    _write_log(service, 1, _turn("user", "amberwheel MEMBER-CLOSED"))
    _write_log(service, 1, _turn("user", "amberwheel LEGACY-CLOSED"), member="")
    assert "MEMBER-CLOSED" in await _search(service, "amberwheel")

    text = await _search(service, "amberwheel", member="")

    assert "MEMBER-CLOSED" not in text and "LEGACY-CLOSED" not in text


async def test_raw_scope_is_active_and_ancestors_and_excludes_each_current_log(tmp_path):
    service, _ = _services(tmp_path, parents={SPACE: "root", "root": "", "unrelated": ""})
    for sid in (SPACE, "root", "unrelated"):
        _write_log(service, 1, _turn("user", f"amberwheel CLOSED-{sid}"), space=sid, current=2)
        _write_log(service, 2, _turn("user", f"amberwheel CURRENT-{sid}"), space=sid, current=2)

    text = await _search(service, "amberwheel")

    assert f"CLOSED-{SPACE}" in text and "CLOSED-root" in text
    assert "root" in text and "log_001" in text
    assert "CLOSED-unrelated" not in text
    assert "CURRENT-" not in text


async def test_overlapping_hits_merge_before_top_three_distinct_conversations(tmp_path):
    service, _ = _services(tmp_path)
    # Every turn in the first window matches. Selecting three turns before
    # merging their neighbour windows would discard the other conversations.
    _write_log(service, 1,
               _turn("user", "amberwheel latch DENSE-QUESTION", 0),
               _turn("assistant", "amberwheel latch DENSE-ANSWER", 1),
               _turn("user", "amberwheel latch DENSE-FOLLOWUP", 2),
               _turn("assistant", "amberwheel latch DENSE-RESOLUTION", 3))
    for number, name in ((2, "SECOND"), (3, "THIRD")):
        _write_log(service, number,
                   _turn("user", f"amberwheel latch {name}-QUESTION", day=number),
                   _turn("assistant", f"{name}-ANSWER", 1, day=number))

    text = await _search(service, "amberwheel latch")

    for marker in ("DENSE-QUESTION", "DENSE-RESOLUTION", "SECOND-QUESTION",
                   "SECOND-ANSWER", "THIRD-QUESTION", "THIRD-ANSWER"):
        assert marker in text
    assert text.count("DENSE-ANSWER") == 1
    assert all(f"log_{number:03d}" in text for number in (1, 2, 3))


async def test_ranks_separate_neighbour_windows_in_one_log_and_limits_to_three(tmp_path):
    service, _ = _services(tmp_path)
    turns = []
    for group in range(4):
        if group:
            # Separate the conversations beyond the neighbour radius. Noise
            # fits the final budget too, so returning the whole log is visible.
            turns.extend(_turn("assistant", "Unrelated inventory check completed.", minute)
                         for minute in range(10, 20))
        if group < 3:
            turns.append(_turn("user", f"amberwheel latch code STRONG-{group}-QUESTION", 20))
            turns.append(_turn("assistant", f"STRONG-{group}-ANSWER is a bronze pin.", 21))
        else:
            turns.append(_turn("user", "amberwheel WEAK-FOURTH-QUESTION", 22))
            turns.append(_turn("assistant", "WEAK-FOURTH-ANSWER is irrelevant.", 23))
    _write_log(service, 6, *turns)

    text = await _search(service, "amberwheel latch code")

    for group in range(3):
        assert f"STRONG-{group}-QUESTION" in text
        assert f"STRONG-{group}-ANSWER" in text
    assert "WEAK-FOURTH" not in text
    assert "log_006" in text


async def test_exact_match_outranks_weak_hits_in_much_longer_logs(tmp_path):
    service, _ = _services(tmp_path)
    _write_log(service, 1,
               _turn("user", "amberwheel latch release code EXACT-ANSWER"),
               _turn("assistant", "EXACT-ANSWER is the bronze release pin.", 1))
    for number in (2, 3, 4):
        noise = [_turn("assistant", "Unrelated inventory count checked.", minute % 60,
                       day=number) for minute in range(80)]
        _write_log(service, number, *noise,
                   _turn("user", f"amberwheel WEAK-{number}", 25, day=number))

    text = await _search(service, "amberwheel latch release code")

    assert "EXACT-ANSWER is the bronze release pin." in text
    assert "log_001" in text


async def test_same_service_observes_changed_closed_log_content(tmp_path):
    service, _ = _services(tmp_path)
    original = _turn("user", "amberwheel ORIGINAL-CLOSED-DETAIL")
    _write_log(service, 1, original, current=2)
    assert "ORIGINAL-CLOSED-DETAIL" in await _search(service, "amberwheel")

    # Rewrite the same path with an added pair. The size and modification time
    # change, while the service and log identity remain the same.
    _write_log(service, 1, original,
               _turn("user", "How does the heliotrope detent open?", 2),
               _turn("assistant", "Use NEW-CLOSED-DETAIL-COPPER-94 for heliotrope.", 3),
               current=2)

    text = await _search(service, "heliotrope detent")

    assert "How does the heliotrope detent open?" in text
    assert "NEW-CLOSED-DETAIL-COPPER-94" in text
    assert "log_001" in text


async def test_same_service_observes_rollover_and_searches_newly_closed_log(tmp_path):
    service, _ = _services(tmp_path)
    _write_log(service, 1, _turn("user", "amberwheel ALREADY-CLOSED"), current=2)
    _write_log(service, 2, _turn("user", "amberwheel NEWLY-CLOSED"), current=2)
    before = await _search(service, "amberwheel")
    assert "ALREADY-CLOSED" in before and "NEWLY-CLOSED" not in before

    _write_log(service, 3, _turn("user", "amberwheel STILL-CURRENT-SECRET"), current=3)

    after = await _search(service, "amberwheel")

    assert "ALREADY-CLOSED" in after and "NEWLY-CLOSED" in after
    assert "STILL-CURRENT-SECRET" not in after
    assert "log_002" in after


async def test_closed_log_seeded_from_excluded_current_log_does_not_leak_current_turn(tmp_path):
    service, _ = _services(tmp_path)
    current_seed = _turn("user", "amberwheel CURRENT-ONLY-SEEDED-SECRET", 1)
    _write_log(service, 1, _turn("user", "amberwheel OLDER-CLOSED-DETAIL"), current=2)
    _write_log(service, 2, current_seed, current=2)
    _write_log(service, 3, current_seed,
               _turn("assistant", "amberwheel GENUINE-HISTORICAL-THREE", 2), current=2)

    text = await _search(service, "amberwheel")

    assert "GENUINE-HISTORICAL-THREE" in text
    assert "CURRENT-ONLY-SEEDED-SECRET" not in text
    assert "log_003" in text
    _assert_turn_reference(text, 2)


async def test_seeded_duplicate_turns_do_not_consume_slots_or_renumber_original_turns(tmp_path):
    service, _ = _services(tmp_path)
    seed_q = _turn("user", "amberwheel DUPLICATED-QUESTION", 0)
    seed_a = _turn("assistant", "amberwheel DUPLICATED-ANSWER", 1)
    _write_log(service, 1, seed_q, seed_a)
    _write_log(service, 2, seed_q, seed_a,
               _turn("user", "amberwheel SECOND-ORIGINAL-QUESTION", 2),
               _turn("assistant", "SECOND-ORIGINAL-ANSWER", 3))
    _write_log(service, 3,
               _turn("user", "amberwheel THIRD-ORIGINAL-QUESTION", 0, day=3),
               _turn("assistant", "THIRD-ORIGINAL-ANSWER", 1, day=3))

    text = await _search(service, "amberwheel")

    assert text.count("DUPLICATED-QUESTION") == 1
    assert text.count("DUPLICATED-ANSWER") == 1
    assert "SECOND-ORIGINAL-QUESTION" in text and "SECOND-ORIGINAL-ANSWER" in text
    assert "THIRD-ORIGINAL-QUESTION" in text and "THIRD-ORIGINAL-ANSWER" in text
    # The copied head was two physical turns, so the new pair remains 3 and 4.
    _assert_turn_reference(text, 3)
    _assert_turn_reference(text, 4)
    assert "log_002" in text


async def test_oversized_window_excerpts_near_match_with_omission_and_source(tmp_path):
    service, _ = _services(tmp_path)
    huge = "Ordinary padding before the useful detail. " * 220
    huge += "Why does the amberwheel latch need the VIOLET-KEY-91?"
    huge += " Ordinary padding after the useful detail." * 220
    _write_log(service, 12, _turn("user", huge, 5),
               _turn("assistant", "The VIOLET-KEY-91 releases its inner spring.", 6))

    text = await _search(service, "amberwheel latch")

    assert "amberwheel latch need the VIOLET-KEY-91" in text
    assert re.search(r"omitted|truncated|excerpt", text, re.I), text
    assert "log_012" in text and SPACE in text
    _assert_turn_reference(text, 1)
    assert len(text) <= 6000


async def test_final_budget_includes_wrappers_and_reserves_raw_evidence_under_pressure(tmp_path):
    facts = [_fact(n, "amberwheel " + f"FACT-{n} " * 95) for n in range(12)]
    service, compaction = _services(tmp_path, facts=facts)
    _write_ledger(compaction, *[
        f"## Compaction #{n} (source: log_{n:03d})\n\n- amberwheel " + "Ledger detail. " * 80
        for n in range(1, 5)
    ])
    answer = "amberwheel RAW-EVIDENCE-BEGIN " + "Exact original detail. " * 35
    answer += " RAW-EVIDENCE-END-47"
    _write_log(service, 5, _turn("user", "Explain the amberwheel evidence?"),
               _turn("assistant", answer, 1))

    text = await _search(service, "amberwheel")

    assert "Explain the amberwheel evidence?" in text
    assert "RAW-EVIDENCE-BEGIN" in text and "RAW-EVIDENCE-END-47" in text
    assert "log_005" in text and SPACE in text
    assert len(text) <= 6000  # Count the actual final response, separators included.
