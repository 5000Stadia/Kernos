"""Tests for SPEC-1B7: Memory Projectors (Tier 1, Tier 2, coordinator, name ask)."""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from kernos.kernel.projectors.rules import Tier1Result, tier1_extract
from kernos.kernel.projectors.llm_extractor import (
    run_tier2_extraction,
    _write_entry,
    _apply_correction,
)
from kernos.kernel.projectors.coordinator import run_projectors
from kernos.kernel.soul import Soul
from kernos.kernel.state import KnowledgeEntry, StateStore, _content_hash
from kernos.kernel.events import EventStream
from kernos.messages.handler import _maybe_append_name_ask


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _soul(**kwargs) -> Soul:
    return Soul(instance_id="t1", **kwargs)


def _mock_state() -> AsyncMock:
    state = AsyncMock(spec=StateStore)
    state.get_knowledge_hashes.return_value = set()
    state.query_knowledge.return_value = []
    state.save_soul.return_value = None
    state.save_knowledge_entry.return_value = None
    return state


def _mock_events() -> AsyncMock:
    events = AsyncMock(spec=EventStream)
    events.emit.return_value = None
    return events


def _knowledge_entry(instance_id: str, subject: str, content: str,
                     confidence: str = "stated", active: bool = True) -> KnowledgeEntry:
    h = _content_hash(instance_id, subject, content)
    return KnowledgeEntry(
        id=f"know_test_{h}",
        instance_id=instance_id,
        category="fact",
        subject=subject,
        content=content,
        confidence=confidence,
        source_event_id="",
        source_description="test",
        created_at="2026-01-01T00:00:00+00:00",
        last_referenced="2026-01-01T00:00:00+00:00",
        tags=[],
        active=active,
        durability="permanent",
        content_hash=h,
    )


# ---------------------------------------------------------------------------
# Tier 1 — name extraction
# ---------------------------------------------------------------------------


def test_tier1_extracts_my_name_is():
    result = tier1_extract("my name is Alice")
    assert result.user_name == "Alice"


def test_tier1_extracts_call_me():
    result = tier1_extract("You can call me Bob")
    assert result.user_name == "Bob"


def test_tier1_extracts_i_go_by():
    result = tier1_extract("I go by Charlie")
    assert result.user_name == "Charlie"


def test_tier1_extracts_they_call_me():
    result = tier1_extract("They call me Doc")
    assert result.user_name == "Doc"


def test_tier1_extracts_everyone_calls_me():
    result = tier1_extract("Everyone calls me Sam")
    assert result.user_name == "Sam"


def test_tier1_extracts_im_with_comma():
    result = tier1_extract("I'm Dana, how are you?")
    assert result.user_name == "Dana"


def test_tier1_extracts_its_at_start():
    result = tier1_extract("It's Jordan here")
    assert result.user_name == "Jordan"


def test_tier1_capitalizes_name():
    result = tier1_extract("my name is alice")
    assert result.user_name == "Alice"


def test_tier1_false_positive_fine():
    result = tier1_extract("I'm fine, thanks")
    assert result.user_name == ""


def test_tier1_false_positive_good():
    result = tier1_extract("I'm good")
    assert result.user_name == ""


def test_tier1_false_positive_here():
    result = tier1_extract("I'm here now")
    assert result.user_name == ""


def test_tier1_false_positive_back():
    result = tier1_extract("I'm back!")
    assert result.user_name == ""


def test_tier1_false_positive_ready():
    result = tier1_extract("I'm ready")
    assert result.user_name == ""


def test_tier1_false_positive_new():
    result = tier1_extract("I'm new here")
    assert result.user_name == ""


def test_tier1_extracts_name_even_if_already_set():
    # Name is always authoritative — "my name is Alice" overwrites previous value
    result = tier1_extract("my name is Alice", current_name="Bob")
    assert result.user_name == "Alice"


def test_tier1_rejects_single_char_name():
    result = tier1_extract("call me X")
    assert result.user_name == ""


def test_tier1_no_name_in_message():
    result = tier1_extract("Just checking in, how's everything?")
    assert result.user_name == ""


# ---------------------------------------------------------------------------
# Tier 1 — style extraction
# ---------------------------------------------------------------------------


def test_tier1_direct_style_be_direct():
    result = tier1_extract("Please be direct with me")
    assert result.communication_style == "direct"


def test_tier1_direct_style_be_blunt():
    result = tier1_extract("just be blunt with me")
    assert result.communication_style == "direct"


def test_tier1_direct_style_straight():
    result = tier1_extract("be straight with me about it")
    assert result.communication_style == "direct"


def test_tier1_casual_keep_it_casual():
    result = tier1_extract("keep it casual please")
    assert result.communication_style == "casual"


def test_tier1_casual_no_need_to_be_formal():
    result = tier1_extract("no need to be formal with me")
    assert result.communication_style == "casual"


def test_tier1_casual_dont_sugarcoat():
    result = tier1_extract("Don't sugarcoat things")
    assert result.communication_style == "casual"


def test_tier1_formal_keep_it_professional():
    result = tier1_extract("Keep it professional please")
    assert result.communication_style == "formal"


def test_tier1_formal_be_formal():
    result = tier1_extract("be formal with me")
    assert result.communication_style == "formal"


def test_tier1_skips_style_if_already_set():
    result = tier1_extract("be direct with me", current_style="formal")
    assert result.communication_style == ""


def test_tier1_no_style_in_message():
    result = tier1_extract("What's on my calendar today?")
    assert result.communication_style == ""


def test_tier1_result_defaults_empty():
    result = Tier1Result()
    assert result.user_name == ""
    assert result.communication_style == ""


# ---------------------------------------------------------------------------
# _content_hash
# ---------------------------------------------------------------------------


def test_content_hash_deterministic():
    h1 = _content_hash("t1", "user", "runs a bakery")
    h2 = _content_hash("t1", "user", "runs a bakery")
    assert h1 == h2


def test_content_hash_case_insensitive():
    h1 = _content_hash("t1", "user", "Runs A Bakery")
    h2 = _content_hash("t1", "user", "runs a bakery")
    assert h1 == h2


def test_content_hash_subject_case_insensitive():
    h1 = _content_hash("t1", "User", "runs a bakery")
    h2 = _content_hash("t1", "user", "runs a bakery")
    assert h1 == h2


def test_content_hash_different_content():
    h1 = _content_hash("t1", "user", "runs a bakery")
    h2 = _content_hash("t1", "user", "runs a restaurant")
    assert h1 != h2


def test_content_hashinstance_isolated():
    h1 = _content_hash("t1", "user", "runs a bakery")
    h2 = _content_hash("t2", "user", "runs a bakery")
    assert h1 != h2


def test_content_hash_length():
    h = _content_hash("t1", "user", "content")
    assert len(h) == 16


# ---------------------------------------------------------------------------
# _parse_extraction_response — REMOVED in SPEC-2.0
# Structured output (output_schema) guarantees valid JSON from the API.
# Code fence stripping is no longer needed. Tests moved to test_schema_foundation.py.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _write_entry — deduplication and confidence precedence
# ---------------------------------------------------------------------------


async def test_write_entry_writes_new():
    state = _mock_state()
    events = _mock_events()
    wrote = await _write_entry(
        state=state, events=events, instance_id="t1",
        category="fact", subject="user", content="runs a bakery",
        confidence="stated",
        source_description="test", existing_hashes=set(),
        now="2026-01-01T00:00:00+00:00", tags=["fact"],
    )
    assert wrote == 1
    state.save_knowledge_entry.assert_called_once()
    saved = state.save_knowledge_entry.call_args[0][0]
    assert saved.content == "runs a bakery"
    assert saved.active is True


async def test_write_entry_skips_duplicate_hash():
    state = _mock_state()
    events = _mock_events()
    h = _content_hash("t1", "user", "runs a bakery")
    wrote = await _write_entry(
        state=state, events=events, instance_id="t1",
        category="fact", subject="user", content="runs a bakery",
        confidence="stated",
        source_description="test", existing_hashes={h},
        now="2026-01-01T00:00:00+00:00", tags=["fact"],
    )
    assert wrote == 0
    state.save_knowledge_entry.assert_not_called()


async def test_write_entry_inferred_discarded_when_stated_exists():
    state = _mock_state()
    events = _mock_events()
    existing = _knowledge_entry("t1", "user", "runs a bakery", confidence="stated")
    state.query_knowledge.return_value = [existing]

    wrote = await _write_entry(
        state=state, events=events, instance_id="t1",
        category="fact", subject="user", content="different content",
        confidence="inferred",
        source_description="test", existing_hashes=set(),
        now="2026-01-01T00:00:00+00:00", tags=["fact"],
    )
    assert wrote == 0
    state.save_knowledge_entry.assert_not_called()


async def test_write_entry_stated_marks_inferred_inactive():
    state = _mock_state()
    events = _mock_events()
    inferred_entry = _knowledge_entry("t1", "user", "probably a developer", confidence="inferred")
    state.query_knowledge.return_value = [inferred_entry]

    await _write_entry(
        state=state, events=events, instance_id="t1",
        category="fact", subject="user", content="definitely a developer",
        confidence="stated",
        source_description="test", existing_hashes=set(),
        now="2026-01-01T00:00:00+00:00", tags=["fact"],
    )
    # First call marks old entry inactive, second writes new entry
    assert state.save_knowledge_entry.call_count == 2
    first_saved = state.save_knowledge_entry.call_args_list[0][0][0]
    assert first_saved.active is False
    assert first_saved.id == inferred_entry.id


async def test_write_entry_hash_added_to_existing_set():
    state = _mock_state()
    events = _mock_events()
    existing = set()
    await _write_entry(
        state=state, events=events, instance_id="t1",
        category="fact", subject="user", content="runs a bakery",
        confidence="stated",
        source_description="test", existing_hashes=existing,
        now="2026-01-01T00:00:00+00:00", tags=["fact"],
    )
    h = _content_hash("t1", "user", "runs a bakery")
    assert h in existing


# ---------------------------------------------------------------------------
# _apply_correction
# ---------------------------------------------------------------------------


async def test_apply_correction_marks_old_inactive():
    state = _mock_state()
    events = _mock_events()
    old_entry = _knowledge_entry("t1", "user", "call me Alice")
    state.query_knowledge.return_value = [old_entry]
    soul = _soul(user_name="Alice")

    await _apply_correction(
        state=state, events=events, soul=soul, instance_id="t1",
        field="user_name", old_value="Alice", new_value="JT",
        now="2026-01-01T00:00:00+00:00",
    )

    calls = state.save_knowledge_entry.call_args_list
    # First call: mark old inactive
    assert calls[0][0][0].active is False
    assert calls[0][0][0].id == old_entry.id
    # Second call: new entry
    new_entry = calls[1][0][0]
    assert new_entry.content == "JT"
    assert new_entry.supersedes == old_entry.id
    assert new_entry.active is True


async def test_apply_correction_updates_soul_name():
    state = _mock_state()
    events = _mock_events()
    state.query_knowledge.return_value = []
    soul = _soul(user_name="Alice")

    await _apply_correction(
        state=state, events=events, soul=soul, instance_id="t1",
        field="user_name", old_value="Alice", new_value="JT",
        now="2026-01-01T00:00:00+00:00",
    )

    assert soul.user_name == "JT"
    state.save_soul.assert_called_once()


async def test_apply_correction_updates_soul_name_dot_notation():
    state = _mock_state()
    events = _mock_events()
    state.query_knowledge.return_value = []
    soul = _soul(user_name="Alice")

    await _apply_correction(
        state=state, events=events, soul=soul, instance_id="t1",
        field="user.name", old_value="Alice", new_value="JT",
        now="2026-01-01T00:00:00+00:00",
    )

    assert soul.user_name == "JT"
    state.save_soul.assert_called_once()


async def test_apply_correction_no_old_entry_still_creates_new():
    state = _mock_state()
    events = _mock_events()
    state.query_knowledge.return_value = []
    soul = _soul()

    await _apply_correction(
        state=state, events=events, soul=soul, instance_id="t1",
        field="occupation", old_value="teacher", new_value="nurse",
        now="2026-01-01T00:00:00+00:00",
    )

    # Only one call — the new entry (no old to mark inactive)
    state.save_knowledge_entry.assert_called_once()
    new_entry = state.save_knowledge_entry.call_args[0][0]
    assert new_entry.supersedes == ""
    assert new_entry.content == "nurse"


async def test_apply_correction_emits_event():
    state = _mock_state()
    events = _mock_events()
    state.query_knowledge.return_value = []
    soul = _soul()

    await _apply_correction(
        state=state, events=events, soul=soul, instance_id="t1",
        field="name", old_value="old", new_value="new",
        now="2026-01-01T00:00:00+00:00",
    )

    events.emit.assert_called_once()
    event = events.emit.call_args[0][0]
    assert "correction" in event.payload.get("type", "")


# ---------------------------------------------------------------------------
# run_tier2_extraction
# ---------------------------------------------------------------------------


async def test_tier2_writes_facts():
    """SPEC-CHECKPOINTED-FACT-HARVEST: facts are NO LONGER extracted per-turn.
    They are harvested at compaction boundaries instead."""
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value=json.dumps({
        "entities": [],
        "facts": [{"subject": "user", "content": "runs a bakery",
                   "confidence": "stated", "durability": "permanent"}],
        "preferences": [],
        "corrections": [],
    }))

    soul = _soul(user_context="")
    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "I run a bakery"}],
        soul=soul, state=state, events=events,
        reasoning_service=reasoning, instance_id="t1",
    )

    # Facts are no longer written per-turn — harvested at boundaries
    state.save_knowledge_entry.assert_not_called()


async def test_tier2_does_not_append_to_soul_user_context():
    """soul.user_context is deprecated — Tier 2 should NOT append to it."""
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value=json.dumps({
        "entities": [],
        "facts": [{"subject": "user", "content": "runs a bakery",
                   "confidence": "stated", "durability": "permanent"}],
        "preferences": [],
        "corrections": [],
    }))

    soul = _soul(user_context="")
    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "I run a bakery"}],
        soul=soul, state=state, events=events,
        reasoning_service=reasoning, instance_id="t1",
    )

    # soul.user_context still not touched (deprecated)
    assert soul.user_context == ""
    # Facts no longer written per-turn (boundary harvest)
    state.save_knowledge_entry.assert_not_called()


async def test_tier2_skips_empty_turns():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock()

    await run_tier2_extraction(
        recent_turns=[],
        soul=_soul(), state=state, events=events,
        reasoning_service=reasoning, instance_id="t1",
    )

    reasoning.complete_simple.assert_not_called()


async def test_tier2_handles_malformed_json_gracefully():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value="not json at all")

    # Should not raise
    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "hi"}],
        soul=_soul(), state=state, events=events,
        reasoning_service=reasoning, instance_id="t1",
    )
    state.save_knowledge_entry.assert_not_called()


async def test_tier2_handles_reasoning_error_gracefully():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(side_effect=RuntimeError("API down"))

    # Should not raise
    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "hi"}],
        soul=_soul(), state=state, events=events,
        reasoning_service=reasoning, instance_id="t1",
    )


async def test_tier2_emits_knowledge_extracted_on_writes():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value=json.dumps({
        "entities": [],
        "facts": [{"subject": "user", "content": "is a nurse",
                   "confidence": "stated", "durability": "permanent"}],
        "preferences": [],
        "corrections": [],
    }))

    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "I'm a nurse"}],
        soul=_soul(), state=state, events=events,
        reasoning_service=reasoning, instance_id="t1",
    )

    # Facts no longer extracted per-turn → no knowledge.extracted event for facts
    # (entities and corrections can still emit events)
    if events.emit.called:
        event = events.emit.call_args[0][0]
        # Should NOT have written fact entries
        assert event.payload.get("entries_written", 0) == 0


async def test_tier2_no_emit_when_nothing_written():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value=json.dumps({
        "entities": [], "facts": [], "preferences": [], "corrections": [],
    }))

    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "hi there"}],
        soul=_soul(), state=state, events=events,
        reasoning_service=reasoning, instance_id="t1",
    )

    events.emit.assert_not_called()


async def test_tier2_deduplicates_via_hash():
    state = _mock_state()
    events = _mock_events()
    # Pre-populate hash so write is skipped
    h = _content_hash("t1", "user", "runs a bakery")
    state.get_knowledge_hashes.return_value = {h}
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value=json.dumps({
        "entities": [],
        "facts": [{"subject": "user", "content": "runs a bakery",
                   "confidence": "stated", "durability": "permanent"}],
        "preferences": [],
        "corrections": [],
    }))

    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "I run a bakery"}],
        soul=_soul(), state=state, events=events,
        reasoning_service=reasoning, instance_id="t1",
    )

    state.save_knowledge_entry.assert_not_called()


async def test_tier2_writes_teacher_fact():
    """SPEC-CHECKPOINTED-FACT-HARVEST: facts no longer written per-turn."""
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    data = {
        "reasoning": "User is a teacher",
        "entities": [],
        "facts": [{"subject": "user", "content": "is a teacher",
                   "confidence": "stated", "lifecycle_archetype": "structural",
                   "foresight_signal": "", "foresight_expires": "", "salience": "0.5"}],
        "preferences": [],
        "corrections": [],
    }
    reasoning.complete_simple = AsyncMock(return_value=json.dumps(data))

    await run_tier2_extraction(
        recent_turns=[{"role": "user", "content": "I'm a teacher"}],
        soul=_soul(), state=state, events=events,
        reasoning_service=reasoning, instance_id="t1",
    )

    # Facts harvested at boundaries, not per-turn
    state.save_knowledge_entry.assert_not_called()


# ---------------------------------------------------------------------------
# run_projectors (coordinator)
# ---------------------------------------------------------------------------


async def test_coordinator_tier1_skips_name_extraction():
    """Tier1 no longer extracts names (false positive risk). Names come from tier2/compaction."""
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value='{"entities":[],"facts":[],"preferences":[],"corrections":[]}')
    soul = _soul(user_name="")

    with patch("kernos.kernel.projectors.coordinator.asyncio.create_task"):
        await run_projectors(
            user_message="my name is Alice",
            recent_turns=[],
            soul=soul, state=state, events=events,
            reasoning_service=reasoning, instance_id="t1",
        )

    # Name NOT extracted by tier1 — handled by tier2 LLM extraction
    assert soul.user_name == ""


async def test_coordinator_tier1_emits_event_on_style_update():
    """Tier1 still extracts communication style and emits events for it."""
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    soul = _soul(user_name="", communication_style="")

    with patch("kernos.kernel.projectors.coordinator.asyncio.create_task"):
        await run_projectors(
            user_message="keep it casual please",
            recent_turns=[],
            soul=soul, state=state, events=events,
            reasoning_service=reasoning, instance_id="t1",
        )

    events.emit.assert_called()
    event = events.emit.call_args[0][0]
    assert event.type == "knowledge.extracted"
    assert "communication_style" in event.payload["fields_updated"]


async def test_coordinator_tier1_no_name_extraction():
    """Tier1 no longer overwrites names — names come from tier2/compaction."""
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    soul = _soul(user_name="Bob")

    with patch("kernos.kernel.projectors.coordinator.asyncio.create_task"):
        await run_projectors(
            user_message="my name is Alice",
            recent_turns=[],
            soul=soul, state=state, events=events,
            reasoning_service=reasoning, instance_id="t1",
        )

    # Name stays as-is — tier1 doesn't touch it
    assert soul.user_name == "Bob"


async def test_coordinator_tier1_updates_style():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    soul = _soul(communication_style="")

    with patch("kernos.kernel.projectors.coordinator.asyncio.create_task"):
        await run_projectors(
            user_message="Please be direct with me",
            recent_turns=[],
            soul=soul, state=state, events=events,
            reasoning_service=reasoning, instance_id="t1",
        )

    assert soul.communication_style == "direct"


async def test_coordinator_tier1_no_update_on_no_match():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    soul = _soul()

    with patch("kernos.kernel.projectors.coordinator.asyncio.create_task"):
        await run_projectors(
            user_message="What's the weather like?",
            recent_turns=[],
            soul=soul, state=state, events=events,
            reasoning_service=reasoning, instance_id="t1",
        )

    state.save_soul.assert_not_called()
    events.emit.assert_not_called()


async def test_coordinator_schedules_tier2():
    state = _mock_state()
    events = _mock_events()
    reasoning = MagicMock()
    soul = _soul()

    with patch("kernos.kernel.projectors.coordinator.asyncio.create_task") as mock_create_task:
        await run_projectors(
            user_message="Hello",
            recent_turns=[{"role": "user", "content": "Hello"}],
            soul=soul, state=state, events=events,
            reasoning_service=reasoning, instance_id="t1",
        )

    mock_create_task.assert_called_once()


# ---------------------------------------------------------------------------
# _maybe_append_name_ask
# ---------------------------------------------------------------------------


def test_name_ask_not_force_appended_on_first_interaction():
    """Bootstrap prompt handles the name question — no force-append."""
    soul = Soul(instance_id="t1", interaction_count=0, user_name="")
    result = _maybe_append_name_ask("Hello! Nice to meet you.", soul)
    # Should return unchanged — no force-appended name question
    assert result == "Hello! Nice to meet you."


def test_name_ask_not_appended_if_name_set():
    soul = Soul(instance_id="t1", interaction_count=0, user_name="Alice")
    result = _maybe_append_name_ask("Hello!", soul)
    assert result == "Hello!"


def test_name_ask_not_appended_if_not_first_interaction():
    soul = Soul(instance_id="t1", interaction_count=3, user_name="")
    result = _maybe_append_name_ask("Hello!", soul)
    assert result == "Hello!"


def test_name_ask_not_appended_if_response_already_asks_name():
    soul = Soul(instance_id="t1", interaction_count=0, user_name="")
    response = "Hey! What's your name?"
    result = _maybe_append_name_ask(response, soul)
    assert result == response


def test_name_ask_not_appended_if_response_says_call_you():
    soul = Soul(instance_id="t1", interaction_count=0, user_name="")
    response = "Hi there! What should I call you?"
    result = _maybe_append_name_ask(response, soul)
    assert result == response


def test_name_ask_not_appended_if_response_asks_who_am_i_talking():
    soul = Soul(instance_id="t1", interaction_count=0, user_name="")
    response = "Hey! Who am I talking to today?"
    result = _maybe_append_name_ask(response, soul)
    assert result == response


# ---------------------------------------------------------------------------
# Knowledge extraction filtering (SPEC-KNOWLEDGE-FILTERING)
# ---------------------------------------------------------------------------

from kernos.kernel.projectors.llm_extractor import _is_suspicious_candidate


class TestSuspiciousCandidate:
    def test_ephemeral_always_suspicious(self):
        assert _is_suspicious_candidate({"lifecycle_archetype": "ephemeral", "content": "anything"})

    def test_contextual_always_suspicious(self):
        assert _is_suspicious_candidate({"lifecycle_archetype": "contextual", "content": "anything"})

    def test_identity_not_suspicious(self):
        assert not _is_suspicious_candidate({
            "lifecycle_archetype": "identity", "content": "User's name is Bob"
        })

    def test_structural_not_suspicious_without_markers(self):
        assert not _is_suspicious_candidate({
            "lifecycle_archetype": "structural", "content": "Lives in San Jose"
        })

    def test_content_with_task_markers_suspicious(self):
        assert _is_suspicious_candidate({
            "lifecycle_archetype": "structural", "content": "User requested a calendar entry"
        })

    def test_content_with_discussion_markers_suspicious(self):
        assert _is_suspicious_candidate({
            "lifecycle_archetype": "habitual", "content": "We discussed setting up reminders"
        })

    def test_habitual_preference_with_test_marker(self):
        assert _is_suspicious_candidate({
            "lifecycle_archetype": "habitual", "category": "preference",
            "content": "User tested the calendar integration"
        })

    def test_clean_preference_not_suspicious(self):
        assert not _is_suspicious_candidate({
            "lifecycle_archetype": "habitual", "category": "preference",
            "content": "Prefers SMS reminders before calendar events"
        })
