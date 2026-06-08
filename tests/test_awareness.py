"""Tests for SPEC-3C: Proactive Awareness.

Covers: Whisper/SuppressionEntry dataclasses, AwarenessEvaluator (time pass,
suppression, queue bounding, cleanup), StateStore extensions (foresight query,
whisper/suppression CRUD), handler injection, dismiss_whisper tool, event type,
and suppression clearing on knowledge update.
"""
import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernos.kernel.awareness import (
    DISMISS_WHISPER_TOOL,
    AwarenessEvaluator,
    SuppressionEntry,
    Whisper,
    _hours_until_foresight,
    generate_whisper_id,
)
from kernos.kernel.event_types import EventType
from kernos.kernel.events import JsonEventStream
from kernos.kernel.state import KnowledgeEntry, StateStore
from kernos.kernel.state_json import JsonStateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _make_knowledge_entry(
    instance_id: str = "test_tenant",
    entry_id: str = "know_test1",
    foresight_signal: str = "Dentist appointment",
    foresight_expires: str = "",
    context_space: str = "space_daily",
    active: bool = True,
) -> KnowledgeEntry:
    if not foresight_expires:
        foresight_expires = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    return KnowledgeEntry(
        id=entry_id,
        instance_id=instance_id,
        category="fact",
        subject="calendar",
        content="User has a dentist appointment at 3pm today",
        confidence="stated",
        source_event_id="evt_test",
        source_description="test",
        created_at=_now_iso(),
        last_referenced=_now_iso(),
        tags=["calendar"],
        active=active,
        foresight_signal=foresight_signal,
        foresight_expires=foresight_expires,
        context_space=context_space,
    )


def _make_whisper(
    whisper_id: str = "",
    knowledge_entry_id: str = "know_test1",
    delivery_class: str = "stage",
    source_space_id: str = "space_daily",
    target_space_id: str = "space_daily",
    foresight_signal: str = "Dentist appointment",
) -> Whisper:
    return Whisper(
        whisper_id=whisper_id or generate_whisper_id(),
        insight_text="Upcoming: Dentist appointment. This is relevant today.",
        delivery_class=delivery_class,
        source_space_id=source_space_id,
        target_space_id=target_space_id,
        supporting_evidence=["Knowledge entry: know_test1"],
        reasoning_trace="Time pass detected: 'Dentist appointment' expires in 6.0 hours.",
        knowledge_entry_id=knowledge_entry_id,
        foresight_signal=foresight_signal,
        created_at=_now_iso(),
    )


# ---------------------------------------------------------------------------
# Data Model Tests
# ---------------------------------------------------------------------------


class TestWhisperDataclass:
    def test_whisper_fields(self):
        w = _make_whisper()
        assert w.whisper_id.startswith("wsp_")
        assert w.delivery_class == "stage"
        assert w.surfaced_at == ""

    def test_whisper_id_generation(self):
        ids = {generate_whisper_id() for _ in range(100)}
        assert len(ids) == 100  # All unique
        assert all(wid.startswith("wsp_") for wid in ids)

    def test_suppression_entry_fields(self):
        s = SuppressionEntry(
            whisper_id="wsp_test",
            knowledge_entry_id="know_test1",
            foresight_signal="Dentist appointment",
            created_at=_now_iso(),
            resolution_state="surfaced",
        )
        assert s.resolved_by == ""
        assert s.resolved_at == ""


class TestEventType:
    def test_proactive_insight_event_type(self):
        assert EventType.PROACTIVE_INSIGHT == "proactive.insight"
        assert EventType.PROACTIVE_INSIGHT.value == "proactive.insight"


class TestDismissWhisperTool:
    def test_tool_definition_shape(self):
        assert DISMISS_WHISPER_TOOL["name"] == "dismiss_whisper"
        schema = DISMISS_WHISPER_TOOL["input_schema"]
        assert "whisper_id" in schema["properties"]
        assert "reason" in schema["properties"]
        assert schema["required"] == ["whisper_id"]


# ---------------------------------------------------------------------------
# StateStore Extension Tests (JsonStateStore)
# ---------------------------------------------------------------------------


class TestForesightQuery:
    async def test_query_empty_store(self, tmp_path):
        store = JsonStateStore(tmp_path)
        results = await store.query_knowledge_by_foresight(
            "test_tenant",
            expires_before=(datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(),
            expires_after=_now_iso(),
        )
        assert results == []

    async def test_query_finds_in_window(self, tmp_path):
        store = JsonStateStore(tmp_path)
        entry = _make_knowledge_entry(
            foresight_expires=(datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        )
        await store.add_knowledge(entry)

        results = await store.query_knowledge_by_foresight(
            "test_tenant",
            expires_before=(datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(),
            expires_after=_now_iso(),
        )
        assert len(results) == 1
        assert results[0].id == "know_test1"

    async def test_query_excludes_expired(self, tmp_path):
        store = JsonStateStore(tmp_path)
        entry = _make_knowledge_entry(
            foresight_expires=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        )
        await store.add_knowledge(entry)

        results = await store.query_knowledge_by_foresight(
            "test_tenant",
            expires_before=(datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(),
            expires_after=_now_iso(),
        )
        assert len(results) == 0

    async def test_query_excludes_beyond_window(self, tmp_path):
        store = JsonStateStore(tmp_path)
        entry = _make_knowledge_entry(
            foresight_expires=(datetime.now(timezone.utc) + timedelta(hours=72)).isoformat()
        )
        await store.add_knowledge(entry)

        results = await store.query_knowledge_by_foresight(
            "test_tenant",
            expires_before=(datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(),
            expires_after=_now_iso(),
        )
        assert len(results) == 0

    async def test_query_excludes_no_signal(self, tmp_path):
        store = JsonStateStore(tmp_path)
        entry = _make_knowledge_entry(foresight_signal="")
        await store.add_knowledge(entry)

        results = await store.query_knowledge_by_foresight(
            "test_tenant",
            expires_before=(datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(),
            expires_after=_now_iso(),
        )
        assert len(results) == 0

    async def test_query_excludes_inactive(self, tmp_path):
        store = JsonStateStore(tmp_path)
        entry = _make_knowledge_entry(active=False)
        await store.add_knowledge(entry)

        results = await store.query_knowledge_by_foresight(
            "test_tenant",
            expires_before=(datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(),
            expires_after=_now_iso(),
        )
        assert len(results) == 0

    async def test_query_space_filter(self, tmp_path):
        store = JsonStateStore(tmp_path)
        entry = _make_knowledge_entry(context_space="space_work")
        await store.add_knowledge(entry)

        results = await store.query_knowledge_by_foresight(
            "test_tenant",
            expires_before=(datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(),
            expires_after=_now_iso(),
            space_id="space_daily",
        )
        assert len(results) == 0

        results = await store.query_knowledge_by_foresight(
            "test_tenant",
            expires_before=(datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(),
            expires_after=_now_iso(),
            space_id="space_work",
        )
        assert len(results) == 1


class TestWhisperPersistence:
    async def test_save_and_get_pending(self, tmp_path):
        store = JsonStateStore(tmp_path)
        w = _make_whisper()
        await store.save_whisper("test_tenant", w)

        pending = await store.get_pending_whispers("test_tenant")
        assert len(pending) == 1
        assert pending[0].whisper_id == w.whisper_id

    async def test_get_pending_excludes_surfaced(self, tmp_path):
        store = JsonStateStore(tmp_path)
        w = _make_whisper()
        w.surfaced_at = _now_iso()
        await store.save_whisper("test_tenant", w)

        pending = await store.get_pending_whispers("test_tenant")
        assert len(pending) == 0

    async def test_mark_surfaced(self, tmp_path):
        store = JsonStateStore(tmp_path)
        w = _make_whisper()
        await store.save_whisper("test_tenant", w)
        await store.mark_whisper_surfaced("test_tenant", w.whisper_id)

        pending = await store.get_pending_whispers("test_tenant")
        assert len(pending) == 0

    async def test_delete_whisper(self, tmp_path):
        store = JsonStateStore(tmp_path)
        w = _make_whisper()
        await store.save_whisper("test_tenant", w)
        await store.delete_whisper("test_tenant", w.whisper_id)

        pending = await store.get_pending_whispers("test_tenant")
        assert len(pending) == 0

    async def test_upsert_whisper(self, tmp_path):
        store = JsonStateStore(tmp_path)
        w = _make_whisper(whisper_id="wsp_fixed")
        await store.save_whisper("test_tenant", w)
        w.insight_text = "Updated insight"
        await store.save_whisper("test_tenant", w)

        # Should still be 1, not 2
        path = store._awareness_dir("test_tenant") / "whispers.json"
        raw = store._read_json(path, [])
        assert len(raw) == 1
        assert raw[0]["insight_text"] == "Updated insight"

    async def test_empty_store_returns_empty(self, tmp_path):
        store = JsonStateStore(tmp_path)
        pending = await store.get_pending_whispers("test_tenant")
        assert pending == []


class TestSuppressionPersistence:
    async def test_save_and_get(self, tmp_path):
        store = JsonStateStore(tmp_path)
        s = SuppressionEntry(
            whisper_id="wsp_test",
            knowledge_entry_id="know_test1",
            foresight_signal="Dentist appointment",
            created_at=_now_iso(),
            resolution_state="surfaced",
        )
        await store.save_suppression("test_tenant", s)

        results = await store.get_suppressions("test_tenant")
        assert len(results) == 1
        assert results[0].whisper_id == "wsp_test"

    async def test_filter_by_knowledge_entry_id(self, tmp_path):
        store = JsonStateStore(tmp_path)
        s1 = SuppressionEntry(
            whisper_id="wsp_1", knowledge_entry_id="know_1",
            foresight_signal="sig1", created_at=_now_iso(), resolution_state="surfaced",
        )
        s2 = SuppressionEntry(
            whisper_id="wsp_2", knowledge_entry_id="know_2",
            foresight_signal="sig2", created_at=_now_iso(), resolution_state="surfaced",
        )
        await store.save_suppression("test_tenant", s1)
        await store.save_suppression("test_tenant", s2)

        results = await store.get_suppressions("test_tenant", knowledge_entry_id="know_1")
        assert len(results) == 1
        assert results[0].whisper_id == "wsp_1"

    async def test_filter_by_whisper_id(self, tmp_path):
        store = JsonStateStore(tmp_path)
        s = SuppressionEntry(
            whisper_id="wsp_find_me", knowledge_entry_id="know_1",
            foresight_signal="sig", created_at=_now_iso(), resolution_state="surfaced",
        )
        await store.save_suppression("test_tenant", s)

        results = await store.get_suppressions("test_tenant", whisper_id="wsp_find_me")
        assert len(results) == 1
        results = await store.get_suppressions("test_tenant", whisper_id="wsp_not_here")
        assert len(results) == 0

    async def test_delete_suppression(self, tmp_path):
        store = JsonStateStore(tmp_path)
        s = SuppressionEntry(
            whisper_id="wsp_del", knowledge_entry_id="know_1",
            foresight_signal="sig", created_at=_now_iso(), resolution_state="surfaced",
        )
        await store.save_suppression("test_tenant", s)
        await store.delete_suppression("test_tenant", "wsp_del")

        results = await store.get_suppressions("test_tenant")
        assert len(results) == 0

    async def test_upsert_suppression(self, tmp_path):
        store = JsonStateStore(tmp_path)
        s = SuppressionEntry(
            whisper_id="wsp_up", knowledge_entry_id="know_1",
            foresight_signal="sig", created_at=_now_iso(), resolution_state="surfaced",
        )
        await store.save_suppression("test_tenant", s)
        s.resolution_state = "dismissed"
        s.resolved_by = "user_dismissed"
        await store.save_suppression("test_tenant", s)

        results = await store.get_suppressions("test_tenant")
        assert len(results) == 1
        assert results[0].resolution_state == "dismissed"

    async def test_empty_returns_empty(self, tmp_path):
        store = JsonStateStore(tmp_path)
        results = await store.get_suppressions("test_tenant")
        assert results == []


# ---------------------------------------------------------------------------
# AwarenessEvaluator Tests
# ---------------------------------------------------------------------------


class TestTimePass:
    async def test_no_signals_produces_no_whispers(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        evaluator = AwarenessEvaluator(store, events)

        whispers = await evaluator.run_time_pass("test_tenant")
        assert whispers == []

    async def test_signal_in_window_produces_whisper(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        entry = _make_knowledge_entry()
        await store.add_knowledge(entry)

        evaluator = AwarenessEvaluator(store, events)
        whispers = await evaluator.run_time_pass("test_tenant")
        assert len(whispers) == 1
        assert "Dentist appointment" in whispers[0].insight_text
        assert whispers[0].delivery_class == "stage"  # < 12 hours
        assert whispers[0].knowledge_entry_id == "know_test1"

    async def test_signal_beyond_48h_not_picked_up(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        entry = _make_knowledge_entry(
            foresight_expires=(datetime.now(timezone.utc) + timedelta(hours=72)).isoformat()
        )
        await store.add_knowledge(entry)

        evaluator = AwarenessEvaluator(store, events)
        whispers = await evaluator.run_time_pass("test_tenant")
        assert len(whispers) == 0

    async def test_expired_signal_not_picked_up(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        entry = _make_knowledge_entry(
            foresight_expires=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        )
        await store.add_knowledge(entry)

        evaluator = AwarenessEvaluator(store, events)
        whispers = await evaluator.run_time_pass("test_tenant")
        assert len(whispers) == 0

    async def test_delivery_class_ambient_for_far_signals(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        entry = _make_knowledge_entry(
            foresight_expires=(datetime.now(timezone.utc) + timedelta(hours=36)).isoformat()
        )
        await store.add_knowledge(entry)

        evaluator = AwarenessEvaluator(store, events)
        whispers = await evaluator.run_time_pass("test_tenant")
        assert len(whispers) == 1
        assert whispers[0].delivery_class == "ambient"


class TestFormatTimeInsight:
    def _evaluator(self):
        return AwarenessEvaluator(MagicMock(), MagicMock())

    def test_very_soon(self):
        e = AwarenessEvaluator(MagicMock(), MagicMock())
        entry = _make_knowledge_entry()
        text = e._format_time_insight(entry, 1.5)
        assert "very soon" in text

    def test_few_hours(self):
        e = AwarenessEvaluator(MagicMock(), MagicMock())
        entry = _make_knowledge_entry()
        text = e._format_time_insight(entry, 4)
        assert "next few hours" in text

    def test_today(self):
        e = AwarenessEvaluator(MagicMock(), MagicMock())
        entry = _make_knowledge_entry()
        text = e._format_time_insight(entry, 16)
        assert "today" in text

    def test_tomorrow(self):
        e = AwarenessEvaluator(MagicMock(), MagicMock())
        entry = _make_knowledge_entry()
        text = e._format_time_insight(entry, 30)
        assert "tomorrow" in text


class TestSuppression:
    async def test_suppressed_whisper_not_queued(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)

        # Create signal and existing suppression
        entry = _make_knowledge_entry()
        await store.add_knowledge(entry)
        s = SuppressionEntry(
            whisper_id="wsp_old",
            knowledge_entry_id="know_test1",
            foresight_signal="Dentist appointment",
            created_at=_now_iso(),
            resolution_state="surfaced",
        )
        await store.save_suppression("test_tenant", s)

        evaluator = AwarenessEvaluator(store, events)
        await evaluator._evaluate("test_tenant")

        # No new whispers should be queued
        pending = await store.get_pending_whispers("test_tenant")
        assert len(pending) == 0

    async def test_dismissed_whisper_stays_suppressed(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)

        entry = _make_knowledge_entry()
        await store.add_knowledge(entry)
        s = SuppressionEntry(
            whisper_id="wsp_old",
            knowledge_entry_id="know_test1",
            foresight_signal="Dentist appointment",
            created_at=_now_iso(),
            resolution_state="dismissed",
            resolved_by="user_dismissed",
        )
        await store.save_suppression("test_tenant", s)

        evaluator = AwarenessEvaluator(store, events)
        await evaluator._evaluate("test_tenant")

        pending = await store.get_pending_whispers("test_tenant")
        assert len(pending) == 0

    async def test_unsuppressed_whisper_is_queued(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)

        entry = _make_knowledge_entry()
        await store.add_knowledge(entry)

        evaluator = AwarenessEvaluator(store, events)
        await evaluator._evaluate("test_tenant")

        pending = await store.get_pending_whispers("test_tenant")
        assert len(pending) == 1


class TestQueueBounding:
    async def test_queue_trimmed_to_10(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        evaluator = AwarenessEvaluator(store, events)

        # Create 15 whispers with unique signals (dedup-safe)
        for i in range(15):
            w = _make_whisper(
                whisper_id=f"wsp_{i:03d}",
                knowledge_entry_id=f"know_{i}",
                delivery_class="ambient",
                foresight_signal=f"signal_{i}",
            )
            await store.save_whisper("test_tenant", w)

        await evaluator._enforce_queue_bound("test_tenant", max_whispers=10)
        pending = await store.get_pending_whispers("test_tenant")
        assert len(pending) == 10

    async def test_stage_whispers_prioritized(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        evaluator = AwarenessEvaluator(store, events)

        # 8 ambient + 5 stage = 13 (unique signals for dedup)
        for i in range(8):
            w = _make_whisper(whisper_id=f"wsp_amb_{i}", delivery_class="ambient",
                              knowledge_entry_id=f"know_a{i}", foresight_signal=f"amb_signal_{i}")
            await store.save_whisper("test_tenant", w)
        for i in range(5):
            w = _make_whisper(whisper_id=f"wsp_stg_{i}", delivery_class="stage",
                              knowledge_entry_id=f"know_s{i}", foresight_signal=f"stg_signal_{i}")
            await store.save_whisper("test_tenant", w)

        await evaluator._enforce_queue_bound("test_tenant", max_whispers=10)
        pending = await store.get_pending_whispers("test_tenant")
        assert len(pending) == 10

        # All 5 stage whispers should survive
        stage_count = sum(1 for w in pending if w.delivery_class == "stage")
        assert stage_count == 5

    async def test_under_limit_not_trimmed(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        evaluator = AwarenessEvaluator(store, events)

        for i in range(5):
            w = _make_whisper(whisper_id=f"wsp_{i}", knowledge_entry_id=f"know_{i}",
                              foresight_signal=f"signal_{i}")
            await store.save_whisper("test_tenant", w)

        await evaluator._enforce_queue_bound("test_tenant", max_whispers=10)
        pending = await store.get_pending_whispers("test_tenant")
        assert len(pending) == 5


class TestCleanup:
    async def test_old_suppressions_removed(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        evaluator = AwarenessEvaluator(store, events)

        # Old suppression (8 days)
        old = SuppressionEntry(
            whisper_id="wsp_old",
            knowledge_entry_id="know_1",
            foresight_signal="old signal",
            created_at=(datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),
            resolution_state="surfaced",
        )
        # Recent suppression (1 day)
        recent = SuppressionEntry(
            whisper_id="wsp_recent",
            knowledge_entry_id="know_2",
            foresight_signal="recent signal",
            created_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            resolution_state="surfaced",
        )
        await store.save_suppression("test_tenant", old)
        await store.save_suppression("test_tenant", recent)

        await evaluator._cleanup_old_suppressions("test_tenant")

        results = await store.get_suppressions("test_tenant")
        assert len(results) == 1
        assert results[0].whisper_id == "wsp_recent"


class TestEvaluatorLifecycle:
    async def test_start_and_stop(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        evaluator = AwarenessEvaluator(store, events, interval_seconds=3600)

        await evaluator.start("test_tenant")
        assert evaluator._running
        assert evaluator._task is not None

        await evaluator.stop()
        assert not evaluator._running
        assert evaluator._task is None

    async def test_stop_idempotent(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        evaluator = AwarenessEvaluator(store, events)
        # Should not raise
        await evaluator.stop()
        await evaluator.stop()


class TestEvaluateEmitsEvent:
    async def test_proactive_insight_event(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)

        entry = _make_knowledge_entry()
        await store.add_knowledge(entry)

        evaluator = AwarenessEvaluator(store, events)
        await evaluator._evaluate("test_tenant")

        # Check event stream for PROACTIVE_INSIGHT
        all_events = await events.query("test_tenant", event_types=["proactive.insight"])
        assert len(all_events) >= 1
        evt = all_events[0]
        assert evt.payload["delivery_class"] == "stage"
        assert "Dentist appointment" in evt.payload["insight_text"]


# ---------------------------------------------------------------------------
# Handler Integration Tests
# ---------------------------------------------------------------------------


class TestHandlerWhisperInjection:
    """Test _get_pending_awareness in the handler."""

    async def _make_handler_and_state(self, tmp_path):
        """Create a minimal handler with state store for testing."""
        from unittest.mock import AsyncMock, MagicMock, patch

        state = JsonStateStore(tmp_path)

        handler = MagicMock()
        handler.state = state
        # Bind the real method
        from kernos.messages.handler import MessageHandler
        handler._get_pending_awareness = MessageHandler._get_pending_awareness.__get__(handler)

        return handler, state

    async def test_no_whispers_returns_empty(self, tmp_path):
        handler, state = await self._make_handler_and_state(tmp_path)
        result = await handler._get_pending_awareness("test_tenant", "space_daily")
        assert result == ""

    async def test_whispers_formatted_correctly(self, tmp_path):
        handler, state = await self._make_handler_and_state(tmp_path)

        w = _make_whisper()
        await state.save_whisper("test_tenant", w)

        result = await handler._get_pending_awareness("test_tenant", "space_daily")
        # Source-clear auxiliary framing: whispers are labeled as the agent's
        # OWN background observations, not the user's message, so the model
        # doesn't acknowledge them as instructions ("got it, I'll preserve").
        assert "AGENT AWARENESS" in result
        assert "NOT the user's message" in result
        assert "Response obligation: none" in result
        assert "[STAGE]" in result
        assert w.whisper_id in result
        assert "Dentist appointment" in result
        assert "dismiss_whisper" in result

    async def test_whispers_marked_surfaced_after_injection(self, tmp_path):
        handler, state = await self._make_handler_and_state(tmp_path)

        w = _make_whisper()
        await state.save_whisper("test_tenant", w)

        await handler._get_pending_awareness("test_tenant", "space_daily")

        # Whisper should now be surfaced
        pending = await state.get_pending_whispers("test_tenant")
        assert len(pending) == 0

    async def test_suppression_created_on_injection(self, tmp_path):
        handler, state = await self._make_handler_and_state(tmp_path)

        w = _make_whisper()
        await state.save_whisper("test_tenant", w)

        await handler._get_pending_awareness("test_tenant", "space_daily")

        suppressions = await state.get_suppressions("test_tenant")
        assert len(suppressions) == 1
        assert suppressions[0].resolution_state == "surfaced"
        assert suppressions[0].knowledge_entry_id == "know_test1"

    async def test_space_filtering(self, tmp_path):
        handler, state = await self._make_handler_and_state(tmp_path)

        # Whisper for different space
        w = _make_whisper(target_space_id="space_work", source_space_id="space_work")
        await state.save_whisper("test_tenant", w)

        result = await handler._get_pending_awareness("test_tenant", "space_daily")
        assert result == ""

    async def test_stage_before_ambient(self, tmp_path):
        handler, state = await self._make_handler_and_state(tmp_path)

        w_ambient = _make_whisper(
            whisper_id="wsp_amb", delivery_class="ambient",
            knowledge_entry_id="know_2", foresight_signal="signal_ambient",
        )
        w_stage = _make_whisper(
            whisper_id="wsp_stg", delivery_class="stage",
            knowledge_entry_id="know_3", foresight_signal="signal_stage",
        )
        await state.save_whisper("test_tenant", w_ambient)
        await state.save_whisper("test_tenant", w_stage)

        result = await handler._get_pending_awareness("test_tenant", "space_daily")
        # STAGE should appear before AMBIENT
        stage_pos = result.index("[STAGE]")
        ambient_pos = result.index("[AMBIENT]")
        assert stage_pos < ambient_pos


# ---------------------------------------------------------------------------
# Dismiss Whisper Tool Tests
# ---------------------------------------------------------------------------


class TestDismissWhisper:
    async def test_dismiss_updates_suppression(self, tmp_path):
        from kernos.kernel.reasoning import ReasoningService

        state = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)

        # Create suppression
        s = SuppressionEntry(
            whisper_id="wsp_dismiss_test",
            knowledge_entry_id="know_1",
            foresight_signal="Test signal",
            created_at=_now_iso(),
            resolution_state="surfaced",
        )
        await state.save_suppression("test_tenant", s)

        # Create a minimal reasoning service with state wired
        provider = MagicMock()
        mcp = MagicMock()
        audit = AsyncMock()
        reasoning = ReasoningService(provider, events, mcp, audit)
        reasoning.set_state(state)

        result = await reasoning._handle_dismiss_whisper(
            "test_tenant", "wsp_dismiss_test", "user_dismissed"
        )
        assert "Dismissed" in result

        # Verify suppression updated
        updated = await state.get_suppressions("test_tenant", whisper_id="wsp_dismiss_test")
        assert len(updated) == 1
        assert updated[0].resolution_state == "dismissed"
        assert updated[0].resolved_by == "user_dismissed"

    async def test_dismiss_not_found(self, tmp_path):
        from kernos.kernel.reasoning import ReasoningService

        state = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        provider = MagicMock()
        mcp = MagicMock()
        audit = AsyncMock()
        reasoning = ReasoningService(provider, events, mcp, audit)
        reasoning.set_state(state)

        result = await reasoning._handle_dismiss_whisper("test_tenant", "wsp_not_found")
        assert "not found" in result


# ---------------------------------------------------------------------------
# Kernel Tool Classification Tests
# ---------------------------------------------------------------------------


class TestToolClassification:
    def test_dismiss_whisper_is_read_effect(self):
        from kernos.kernel.reasoning import ReasoningService

        provider = MagicMock()
        events = MagicMock()
        mcp = MagicMock()
        audit = MagicMock()
        reasoning = ReasoningService(provider, events, mcp, audit)

        assert reasoning._classify_tool_effect("dismiss_whisper", None) == "read"

    def test_dismiss_whisper_in_kernel_tools(self):
        from kernos.kernel.reasoning import ReasoningService
        assert "dismiss_whisper" in ReasoningService._KERNEL_TOOLS


# ---------------------------------------------------------------------------
# Suppression Clearing on Knowledge Update Tests
# ---------------------------------------------------------------------------


class TestSuppressionClearing:
    async def test_knowledge_update_clears_surfaced_suppression(self, tmp_path):
        """Simulate what llm_extractor does when it updates a knowledge entry."""
        store = JsonStateStore(tmp_path)

        # Create suppression for a knowledge entry
        s = SuppressionEntry(
            whisper_id="wsp_clear_test",
            knowledge_entry_id="know_target",
            foresight_signal="Meeting with Bob",
            created_at=_now_iso(),
            resolution_state="surfaced",
        )
        await store.save_suppression("test_tenant", s)

        # Simulate the clearing logic from llm_extractor
        suppressions = await store.get_suppressions(
            "test_tenant", knowledge_entry_id="know_target"
        )
        for sup in suppressions:
            if sup.resolution_state == "surfaced":
                await store.delete_suppression("test_tenant", sup.whisper_id)

        # Suppression should be gone
        remaining = await store.get_suppressions("test_tenant")
        assert len(remaining) == 0

    async def test_dismissed_suppression_not_cleared(self, tmp_path):
        """Dismissed suppressions should NOT be cleared by knowledge updates."""
        store = JsonStateStore(tmp_path)

        s = SuppressionEntry(
            whisper_id="wsp_no_clear",
            knowledge_entry_id="know_target",
            foresight_signal="Meeting with Bob",
            created_at=_now_iso(),
            resolution_state="dismissed",
            resolved_by="user_dismissed",
        )
        await store.save_suppression("test_tenant", s)

        # Simulate the clearing logic — only clears "surfaced"
        suppressions = await store.get_suppressions(
            "test_tenant", knowledge_entry_id="know_target"
        )
        for sup in suppressions:
            if sup.resolution_state == "surfaced":
                await store.delete_suppression("test_tenant", sup.whisper_id)

        # Dismissed suppression should remain
        remaining = await store.get_suppressions("test_tenant")
        assert len(remaining) == 1
        assert remaining[0].resolution_state == "dismissed"


# ---------------------------------------------------------------------------
# Atomic Persistence Tests
# ---------------------------------------------------------------------------


class TestAtomicPersistence:
    async def test_whisper_file_created_atomically(self, tmp_path):
        store = JsonStateStore(tmp_path)
        w = _make_whisper()
        await store.save_whisper("test_tenant", w)

        path = store._awareness_dir("test_tenant") / "whispers.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1

    async def test_suppression_file_created_atomically(self, tmp_path):
        store = JsonStateStore(tmp_path)
        s = SuppressionEntry(
            whisper_id="wsp_test", knowledge_entry_id="know_1",
            foresight_signal="sig", created_at=_now_iso(),
            resolution_state="surfaced",
        )
        await store.save_suppression("test_tenant", s)

        path = store._awareness_dir("test_tenant") / "suppressions.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1


# ---------------------------------------------------------------------------
# Interrupt Delivery (Whisper Spectrum)
# ---------------------------------------------------------------------------


class TestInterruptClassification:
    """Whispers with foresight_expires < 2 hours get delivery_class='interrupt'."""

    async def test_under_2_hours_is_interrupt(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        entry = _make_knowledge_entry(
            foresight_expires=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        )
        await store.add_knowledge(entry)

        evaluator = AwarenessEvaluator(store, events)
        whispers = await evaluator.run_time_pass("test_tenant")
        assert len(whispers) == 1
        assert whispers[0].delivery_class == "interrupt"

    async def test_6_hours_is_stage(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        entry = _make_knowledge_entry(
            foresight_expires=(datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        )
        await store.add_knowledge(entry)

        evaluator = AwarenessEvaluator(store, events)
        whispers = await evaluator.run_time_pass("test_tenant")
        assert len(whispers) == 1
        assert whispers[0].delivery_class == "stage"

    async def test_36_hours_is_ambient(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        entry = _make_knowledge_entry(
            foresight_expires=(datetime.now(timezone.utc) + timedelta(hours=36)).isoformat()
        )
        await store.add_knowledge(entry)

        evaluator = AwarenessEvaluator(store, events)
        whispers = await evaluator.run_time_pass("test_tenant")
        assert len(whispers) == 1
        assert whispers[0].delivery_class == "ambient"


class TestInterruptPush:
    """Interrupt whispers are pushed via send_outbound."""

    async def test_interrupt_pushed_via_outbound(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()

        evaluator = AwarenessEvaluator(store, events, handler=handler)

        whisper = _make_whisper(delivery_class="interrupt")
        pushed = await evaluator._push_interrupt("test_tenant", whisper)

        assert pushed is True
        handler.send_outbound.assert_called_once()

    async def test_interrupt_push_fail_returns_false(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=False)

        evaluator = AwarenessEvaluator(store, events, handler=handler)

        whisper = _make_whisper(delivery_class="interrupt")
        pushed = await evaluator._push_interrupt("test_tenant", whisper)

        assert pushed is False

    async def test_interrupt_suppressed_when_user_active(self, tmp_path):
        """If user messaged < 5 min ago, interrupt is downgraded to stage."""
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)

        # Create a space with recent last_active_at
        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(
            id="space_daily",
            instance_id="test_tenant",
            name="General",
            last_active_at=datetime.now(timezone.utc).isoformat(),
            is_default=True,
        )
        await store.save_context_space(space)

        evaluator = AwarenessEvaluator(store, events, handler=handler)
        whisper = _make_whisper(delivery_class="interrupt")
        pushed = await evaluator._push_interrupt("test_tenant", whisper)

        # Should be suppressed — user is active
        assert pushed is False
        assert whisper.delivery_class == "stage"
        handler.send_outbound.assert_not_called()

    async def test_no_handler_returns_false(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)

        evaluator = AwarenessEvaluator(store, events, handler=None)
        whisper = _make_whisper(delivery_class="interrupt")
        pushed = await evaluator._push_interrupt("test_tenant", whisper)

        assert pushed is False


class TestHoursUntilForesight:
    """Helper to extract hours remaining from whisper evidence."""

    def test_parses_expires_from_evidence(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        w = _make_whisper()
        w.supporting_evidence = [f"Expires: {future}"]
        hours = _hours_until_foresight(w)
        assert hours is not None
        assert 2.5 < hours < 3.5

    def test_no_expires_returns_none(self):
        w = _make_whisper()
        w.supporting_evidence = ["No relevant data"]
        assert _hours_until_foresight(w) is None


class TestWhisperNotifyVia:
    """Whisper has notify_via field."""

    def test_default_empty(self):
        w = _make_whisper()
        assert w.notify_via == ""

    def test_set_channel(self):
        w = _make_whisper()
        w.notify_via = "discord"
        assert w.notify_via == "discord"


class TestEvaluateWithInterrupt:
    """_evaluate() pushes interrupt whispers immediately."""

    async def test_interrupt_pushed_during_evaluate(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()

        # Create entry expiring in 1 hour (interrupt threshold)
        entry = _make_knowledge_entry(
            foresight_expires=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        )
        await store.add_knowledge(entry)

        evaluator = AwarenessEvaluator(store, events, handler=handler)
        await evaluator._evaluate("test_tenant")

        # Should have been pushed, not queued
        handler.send_outbound.assert_called_once()
        # Should NOT be in pending queue (it was delivered)
        pending = await store.get_pending_whispers("test_tenant")
        assert len(pending) == 0

    async def test_stage_queued_not_pushed(self, tmp_path):
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)

        # 6 hours = stage, not interrupt
        entry = _make_knowledge_entry(
            foresight_expires=(datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        )
        await store.add_knowledge(entry)

        evaluator = AwarenessEvaluator(store, events, handler=handler)
        await evaluator._evaluate("test_tenant")

        # Stage should be queued, NOT pushed
        handler.send_outbound.assert_not_called()
        pending = await store.get_pending_whispers("test_tenant")
        assert len(pending) == 1
        assert pending[0].delivery_class == "stage"


# ---------------------------------------------------------------------------
# Proactive Budget (Closeout Fix 2)
# ---------------------------------------------------------------------------


class TestProactiveBudget:
    """Tests for the proactive outbound budget on AwarenessEvaluator."""

    def test_first_message_allowed(self):
        evaluator = AwarenessEvaluator(AsyncMock(), AsyncMock())
        assert evaluator._check_proactive_budget("test") is True

    def test_second_message_allowed(self):
        evaluator = AwarenessEvaluator(AsyncMock(), AsyncMock())
        evaluator._check_proactive_budget("test")
        assert evaluator._check_proactive_budget("test") is True

    def test_third_message_blocked(self):
        evaluator = AwarenessEvaluator(AsyncMock(), AsyncMock())
        evaluator._check_proactive_budget("test")
        evaluator._check_proactive_budget("test")
        assert evaluator._check_proactive_budget("test") is False

    def test_budget_resets_after_window(self):
        import time as _time
        evaluator = AwarenessEvaluator(AsyncMock(), AsyncMock())
        evaluator.PROACTIVE_BUDGET_WINDOW_S = 0.05  # Very short for test
        evaluator._check_proactive_budget("test")
        evaluator._check_proactive_budget("test")
        assert evaluator._check_proactive_budget("test") is False
        _time.sleep(0.06)  # Wait for window to expire
        assert evaluator._check_proactive_budget("test") is True

    def test_budget_configurable(self):
        evaluator = AwarenessEvaluator(AsyncMock(), AsyncMock())
        evaluator.PROACTIVE_BUDGET_MAX = 5
        for _ in range(5):
            assert evaluator._check_proactive_budget("test") is True
        assert evaluator._check_proactive_budget("test") is False
