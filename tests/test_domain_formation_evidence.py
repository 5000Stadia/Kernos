"""DOMAIN-FORMATION-EVIDENCE: slice fix + hint ledger + escalation ladder.

Live finding 2026-06-10: the domain assessor read ``doc[:3000]`` of a
compaction document ordered oldest-first — the stalest slice — and the
router's emerging-topic hints were recorded per-message but never
aggregated, so "likely recurrence" had no receipts. Medium-confidence
verdicts left only a log line, so near-misses never escalated.
"""
import json

import pytest

from kernos.kernel.compaction import domain_assessment_evidence
from kernos.kernel.topic_hints import (
    NEAR_MISS_SUGGESTION_THRESHOLD,
    TopicHintLedger,
    normalize_hint,
)

T = "discord:1234567890"
SPACE = "space_abc12345"


@pytest.fixture
def ledger(tmp_path):
    return TopicHintLedger(str(tmp_path))


# ---------------------------------------------------------------------------
# domain_assessment_evidence — the slice fix
# ---------------------------------------------------------------------------


def _doc(n_entries: int = 4, living_state: str = "## Current Situation\nD&D campaign prep is the dominant thread.") -> str:
    parts = ["# Ledger"]
    for i in range(1, n_entries + 1):
        parts.append(f"## Compaction #{i}\n" + (f"- old ledger entry {i} " * 40))
    parts.append(living_state)
    return "\n\n".join(parts)


def test_evidence_starts_at_most_recent_ledger_entry():
    doc = _doc(n_entries=4)
    ev = domain_assessment_evidence(doc, max_chars=3000)
    assert ev.startswith("## Compaction #4")
    assert "Current Situation" in ev
    assert "## Compaction #1" not in ev


def test_evidence_keeps_tail_when_over_budget():
    """When even the latest entry overflows, the Living State tail wins."""
    doc = _doc(n_entries=1, living_state="## Current Situation\n" + "fresh truth " * 50)
    long_doc = doc.replace("- old ledger entry 1 ", "- bulky ledger line ", 1)
    ev = domain_assessment_evidence(long_doc, max_chars=400)
    assert len(ev) == 400
    assert ev.endswith(long_doc[-50:])


def test_evidence_short_doc_passes_through():
    doc = "## Current Situation\nshort"
    assert domain_assessment_evidence(doc, max_chars=3000) == doc


def test_evidence_no_ledger_marker_falls_back_to_tail():
    doc = "x" * 5000
    ev = domain_assessment_evidence(doc, max_chars=3000)
    assert len(ev) == 3000


def test_old_head_slice_would_have_missed_living_state():
    """Pin the bug: the pre-fix head slice excluded the Living State."""
    doc = _doc(n_entries=4)
    assert "Current Situation" not in doc[:3000]
    assert "Current Situation" in domain_assessment_evidence(doc)


# ---------------------------------------------------------------------------
# TopicHintLedger — hints
# ---------------------------------------------------------------------------


def test_normalize_hint_canonicalizes_variants():
    assert normalize_hint("DnD Campaign") == "dnd_campaign"
    assert normalize_hint("dnd-campaign") == "dnd_campaign"
    assert normalize_hint("  dnd_campaign  ") == "dnd_campaign"
    assert normalize_hint("!!!") == ""


def test_record_and_rank_hints(ledger):
    ledger.record_hints(T, SPACE, ["dnd_campaign"])
    ledger.record_hints(T, SPACE, ["dnd_campaign", "kitchen_reno"])
    ledger.record_hints(T, SPACE, ["DnD Campaign"])
    top = ledger.top_hints(T, SPACE, limit=5)
    assert top[0]["hint"] == "dnd_campaign"
    assert top[0]["count"] == 3
    assert top[1]["hint"] == "kitchen_reno"
    assert top[1]["count"] == 1


def test_hints_are_space_scoped(ledger):
    ledger.record_hints(T, SPACE, ["dnd_campaign"])
    assert ledger.top_hints(T, "space_other000") == []


def test_hint_cap_keeps_most_recent(ledger):
    for i in range(60):
        ledger.record_hints(T, SPACE, [f"topic_{i:03d}"])
    data = ledger._read(T)
    assert len(data[SPACE]["hints"]) == 50
    assert "topic_059" in data[SPACE]["hints"]


def test_empty_and_invalid_hints_are_noops(ledger, tmp_path):
    ledger.record_hints(T, SPACE, ["", "!!!"])
    ledger.record_hints(T, "", ["dnd_campaign"])
    assert not ledger._path(T).exists()


def test_corrupt_ledger_file_degrades_to_empty(ledger):
    path = ledger._path(T)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert ledger.top_hints(T, SPACE) == []
    ledger.record_hints(T, SPACE, ["dnd_campaign"])  # recovers by rewriting
    assert ledger.top_hints(T, SPACE)[0]["count"] == 1


# ---------------------------------------------------------------------------
# TopicHintLedger — near-miss escalation ladder
# ---------------------------------------------------------------------------


def test_near_miss_counts_and_suggests_at_threshold(ledger):
    assert ledger.record_near_miss(T, SPACE, "DnD Campaign") == 1
    assert not ledger.should_suggest(T, SPACE, "DnD Campaign")
    count = ledger.record_near_miss(T, SPACE, "dnd-campaign")  # name variant merges
    assert count == NEAR_MISS_SUGGESTION_THRESHOLD
    assert ledger.should_suggest(T, SPACE, "DnD Campaign")


def test_suggestion_fires_once(ledger):
    ledger.record_near_miss(T, SPACE, "dnd")
    ledger.record_near_miss(T, SPACE, "dnd")
    assert ledger.should_suggest(T, SPACE, "dnd")
    ledger.mark_suggested(T, SPACE, "dnd")
    assert not ledger.should_suggest(T, SPACE, "dnd")
    ledger.record_near_miss(T, SPACE, "dnd")
    assert not ledger.should_suggest(T, SPACE, "dnd")  # anti-nag holds


def test_clear_near_miss_on_creation(ledger):
    ledger.record_near_miss(T, SPACE, "dnd")
    ledger.clear_near_miss(T, SPACE, "dnd")
    data = ledger._read(T)
    assert data[SPACE]["near_misses"] == {}


def test_atomic_write_format(ledger):
    ledger.record_hints(T, SPACE, ["dnd_campaign"])
    raw = json.loads(ledger._path(T).read_text(encoding="utf-8"))
    entry = raw[SPACE]["hints"]["dnd_campaign"]
    assert set(entry) == {"count", "first_seen", "last_seen"}


# ---------------------------------------------------------------------------
# Assessor integration — evidence + hints reach the model; medium escalates
# ---------------------------------------------------------------------------


class _FakeSpace:
    def __init__(self):
        self.id = SPACE
        self.name = "General"
        self.member_id = "mem_x"
        self.space_type = "general"
        self.depth = 0
        self.status = "active"
        self.aliases = []
        self.parent_id = ""


def _make_handler(tmp_path, monkeypatch, model_response: dict):
    """Minimal MessageHandler shell exercising _assess_domain_creation."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from kernos.messages.handler import MessageHandler

    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    h = MessageHandler.__new__(MessageHandler)
    captured: dict = {}

    async def _complete_simple(**kwargs):
        captured.update(kwargs)
        return json.dumps(model_response)

    h.reasoning = MagicMock()
    h.reasoning.complete_simple = AsyncMock(side_effect=_complete_simple)
    doc = _doc(n_entries=4)
    h.compaction = MagicMock()
    h.compaction.load_document = AsyncMock(return_value=doc)
    h.state = MagicMock()
    h.state.list_context_spaces = AsyncMock(return_value=[_FakeSpace()])
    h.state.query_covenant_rules = AsyncMock(return_value=[])
    h.state.save_whisper = AsyncMock()
    h._files = MagicMock()
    h._files.load_manifest = AsyncMock(return_value={})
    h._files.read_file = AsyncMock(return_value="")
    return h, captured


async def test_assessor_reads_fresh_slice_and_hints(tmp_path, monkeypatch):
    ledger = TopicHintLedger(str(tmp_path))
    for _ in range(3):
        ledger.record_hints(T, SPACE, ["dnd_campaign"])
    h, captured = _make_handler(
        tmp_path, monkeypatch,
        {"create_domain": False, "confidence": "low", "reasoning": "no"},
    )
    space = _FakeSpace()
    await h._assess_domain_creation(T, SPACE, space, None)
    content = captured["user_content"]
    assert "Current Situation" in content          # fresh tail present
    assert "## Compaction #1" not in content       # stale head absent
    assert "dnd_campaign: tagged on 3 message(s)" in content


async def test_assessor_medium_confidence_escalates_to_whisper(tmp_path, monkeypatch):
    h, _ = _make_handler(
        tmp_path, monkeypatch,
        {"create_domain": True, "confidence": "medium", "name": "D&D Campaign"},
    )
    space = _FakeSpace()
    await h._assess_domain_creation(T, SPACE, space, None)   # near-miss 1
    h.state.save_whisper.assert_not_awaited()
    await h._assess_domain_creation(T, SPACE, space, None)   # near-miss 2
    h.state.save_whisper.assert_awaited_once()
    whisper = h.state.save_whisper.await_args.args[1]
    assert "D&D Campaign" in whisper.insight_text or "dnd" in whisper.insight_text.lower()
    assert whisper.foresight_signal.startswith("domain_near_miss:")
    # Third medium verdict: already suggested — no second whisper (anti-nag).
    await h._assess_domain_creation(T, SPACE, space, None)
    h.state.save_whisper.assert_awaited_once()


# ---------------------------------------------------------------------------
# Codex r1 folds
# ---------------------------------------------------------------------------


def test_extract_hint_tags_rejects_internal_ids():
    from kernos.kernel.topic_hints import extract_hint_tags
    tags = ["space_abc12345", "mem_e310018f", "sp_dnd", "_internal",
            "dnd_campaign", "Kitchen Reno", "kitchen_reno", "dnd_campaign"]
    out = extract_hint_tags(tags, known_space_ids={"space_abc12345"})
    assert out == ["dnd_campaign", "kitchen_reno"]  # ids/reserved/non-snake dropped, deduped


def test_extract_hint_tags_subtracts_known_space_ids():
    from kernos.kernel.topic_hints import extract_hint_tags
    # A legacy space id with no reserved prefix is still subtracted by set.
    out = extract_hint_tags(["legacy_space_name", "dnd_campaign"],
                            known_space_ids={"legacy_space_name"})
    assert out == ["dnd_campaign"]


def test_evidence_slicer_ignores_inline_heading_mention():
    """rfind false-match guard: heading text inside a sentence doesn't anchor."""
    doc = _doc(n_entries=2, living_state=(
        "## Current Situation\nWe discussed the '## Compaction #99' marker format today."
    ))
    ev = domain_assessment_evidence(doc, max_chars=5000)
    assert ev.startswith("## Compaction #2")
    assert "Current Situation" in ev


async def test_assessor_loads_member_scoped_document(tmp_path, monkeypatch):
    """Codex r1 REAL-BUG: member-scoped compaction docs must be loadable."""
    h, captured = _make_handler(
        tmp_path, monkeypatch,
        {"create_domain": False, "confidence": "low", "reasoning": "no"},
    )
    space = _FakeSpace()
    await h._assess_domain_creation(T, SPACE, space, None, member_id="mem_x")
    h.compaction.load_document.assert_awaited_once_with(T, SPACE, "mem_x")
    assert "user_content" in captured  # assessment actually ran


async def test_assessor_falls_back_to_legacy_document(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    h, captured = _make_handler(
        tmp_path, monkeypatch,
        {"create_domain": False, "confidence": "low", "reasoning": "no"},
    )
    doc = _doc()
    h.compaction.load_document = AsyncMock(side_effect=[None, doc])  # member miss -> legacy hit
    space = _FakeSpace()
    await h._assess_domain_creation(T, SPACE, space, None, member_id="mem_x")
    assert h.compaction.load_document.await_count == 2
    assert "user_content" in captured


async def test_medium_verdict_for_existing_space_records_no_near_miss(tmp_path, monkeypatch):
    """Codex r1 REAL-BUG: duplicate names must not feed the suggestion ladder."""
    h, _ = _make_handler(
        tmp_path, monkeypatch,
        {"create_domain": True, "confidence": "medium", "name": "General"},
    )
    space = _FakeSpace()
    await h._assess_domain_creation(T, SPACE, space, None)
    await h._assess_domain_creation(T, SPACE, space, None)
    h.state.save_whisper.assert_not_awaited()
    ledger = TopicHintLedger(str(tmp_path))
    assert ledger._read(T).get(SPACE, {}).get("near_misses", {}) == {}
