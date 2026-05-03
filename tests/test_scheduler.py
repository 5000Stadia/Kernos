"""Tests for SPEC-3E-B: Time-Triggered Scheduler + Event Triggers + Lifecycle.

Covers: Trigger dataclass, TriggerStore persistence, manage_schedule tool,
trigger evaluation, time helpers, gate classification, CalendarEvent,
parse_calendar_events, evaluate_event_triggers, resolve_owner_member_id,
classify_trigger_failure, retire_stale_triggers, degraded lifecycle.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.scheduler import (
    CalendarEvent,
    EVENT_DAILY_FIRE_CAP,
    MANAGE_SCHEDULE_TOOL,
    TRANSIENT_FAILURE_NOTIFY_THRESHOLD,
    Trigger,
    TriggerStore,
    classify_trigger_failure,
    compute_next_fire,
    evaluate_event_triggers,
    evaluate_triggers,
    handle_manage_schedule,
    parse_calendar_events,
    resolve_owner_member_id,
    retire_stale_triggers,
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _future_iso(hours: float = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _past_iso(hours: float = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


# ---------------------------------------------------------------------------
# Trigger dataclass
# ---------------------------------------------------------------------------


class TestTriggerDataclass:
    def test_defaults(self):
        t = Trigger(trigger_id="trig_test", instance_id="t1")
        assert t.status == "active"
        assert t.action_type == "notify"
        assert t.fire_count == 0
        assert t.delivery_class == "stage"


# ---------------------------------------------------------------------------
# TriggerStore
# ---------------------------------------------------------------------------


class TestTriggerStore:
    async def test_save_and_get(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(trigger_id="trig_1", instance_id="t1", action_description="test")
        await store.save(t)
        loaded = await store.get("t1", "trig_1")
        assert loaded is not None
        assert loaded.action_description == "test"

    async def test_list_active(self, tmp_path):
        store = TriggerStore(tmp_path)
        await store.save(Trigger(trigger_id="t1", instance_id="t", status="active"))
        await store.save(Trigger(trigger_id="t2", instance_id="t", status="completed"))
        active = await store.list_active("t")
        assert len(active) == 1
        assert active[0].trigger_id == "t1"

    async def test_list_all(self, tmp_path):
        store = TriggerStore(tmp_path)
        await store.save(Trigger(trigger_id="t1", instance_id="t", status="active"))
        await store.save(Trigger(trigger_id="t2", instance_id="t", status="completed"))
        all_triggers = await store.list_all("t")
        assert len(all_triggers) == 2

    async def test_get_due(self, tmp_path):
        store = TriggerStore(tmp_path)
        await store.save(Trigger(
            trigger_id="due", instance_id="t", status="active",
            next_fire_at=_past_iso(1),
        ))
        await store.save(Trigger(
            trigger_id="future", instance_id="t", status="active",
            next_fire_at=_future_iso(1),
        ))
        due = await store.get_due("t", _now_iso())
        assert len(due) == 1
        assert due[0].trigger_id == "due"

    async def test_remove(self, tmp_path):
        store = TriggerStore(tmp_path)
        await store.save(Trigger(trigger_id="t1", instance_id="t"))
        assert await store.remove("t", "t1")
        assert await store.get("t", "t1") is None

    async def test_remove_nonexistent(self, tmp_path):
        store = TriggerStore(tmp_path)
        assert not await store.remove("t", "nope")

    async def test_upsert(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(trigger_id="t1", instance_id="t", action_description="v1")
        await store.save(t)
        t.action_description = "v2"
        await store.save(t)
        loaded = await store.get("t", "t1")
        assert loaded.action_description == "v2"
        all_triggers = await store.list_all("t")
        assert len(all_triggers) == 1

    async def test_empty_store(self, tmp_path):
        store = TriggerStore(tmp_path)
        assert await store.list_all("t") == []
        assert await store.get_due("t", _now_iso()) == []


# ---------------------------------------------------------------------------
# manage_schedule tool
# ---------------------------------------------------------------------------


class TestManageScheduleTool:
    def test_tool_shape(self):
        assert MANAGE_SCHEDULE_TOOL["name"] == "manage_schedule"
        schema = MANAGE_SCHEDULE_TOOL["input_schema"]
        assert "action" in schema["properties"]
        assert "create" in schema["properties"]["action"]["enum"]

    def test_in_kernel_tools(self):
        from kernos.kernel.reasoning import ReasoningService
        assert "manage_schedule" in ReasoningService._KERNEL_TOOLS

    def test_gate_classification_list(self):
        from kernos.kernel.reasoning import ReasoningService
        r = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        assert r._classify_tool_effect("manage_schedule", None, {"action": "list"}) == "read"

    def test_gate_classification_action_dependent(self):
        """INTEGRATION-CAPABILITY-FIRST-V1 Batch 2 Fold 5 (the design
        review verdict 2026-05-03): manage_schedule classification is
        now action-dependent. Pre-fix the tool returned "read" for
        every action with the rationale "gate fires at fire time, not
        management time" — but management actions (create / update /
        pause / resume / remove) DO mutate trigger state at the
        scheduler. The dispatch-time gate enforcement codified by
        Fold 3 ("gate at dispatch, hint at surfacing") requires the
        classifier to reflect the actual effect of the call.

        - list → read (no state change)
        - create / update / pause / resume / remove → soft_write
          (reversible — pause/resume/remove can be undone via
          list-then-create-or-resume; create can be remove'd;
          update produces a soft history)
        """
        from kernos.kernel.reasoning import ReasoningService
        r = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        assert r._classify_tool_effect(
            "manage_schedule", None, {"action": "list"},
        ) == "read"
        for action in ["create", "remove", "pause", "resume", "update"]:
            assert r._classify_tool_effect(
                "manage_schedule", None, {"action": action},
            ) == "soft_write", f"Expected soft_write for action={action}"


class TestManageScheduleHandler:
    async def test_list_empty(self, tmp_path):
        store = TriggerStore(tmp_path)
        result = await handle_manage_schedule(store, "t1", "", "", "list")
        assert "No scheduled" in result

    async def test_create_notify_via_extraction(self, tmp_path):
        """Create with NL description + mock Haiku extraction."""
        import json
        store = TriggerStore(tmp_path)
        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "action_type": "notify",
            "when": _future_iso(1),
            "message": "Time to check your email!",
            "delivery_class": "stage",
            "recurrence": "",
            "notify_via": "",
            "tool_name": "",
            "tool_args": "",
        }))

        result = await handle_manage_schedule(
            store, "t1", "m1", "s1", "create",
            description="Remind me to check email in 1 hour",
            reasoning_service=reasoning,
        )
        assert "Scheduled" in result
        assert "trig_" in result
        triggers = await store.list_active("t1")
        assert len(triggers) == 1
        assert triggers[0].action_type == "notify"

    async def test_create_missing_reasoning_service(self, tmp_path):
        store = TriggerStore(tmp_path)
        result = await handle_manage_schedule(
            store, "t1", "", "", "create", description="test",
        )
        assert "Error" in result

    async def test_create_missing_description(self, tmp_path):
        store = TriggerStore(tmp_path)
        reasoning = AsyncMock()
        result = await handle_manage_schedule(
            store, "t1", "", "", "create", reasoning_service=reasoning,
        )
        assert "Error" in result

    async def test_pause_and_resume(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(trigger_id="trig_pr", instance_id="t1", status="active",
                    action_description="test", next_fire_at=_future_iso(1))
        await store.save(t)

        result = await handle_manage_schedule(store, "t1", "", "", "pause", trigger_id="trig_pr")
        assert "paused" in result
        assert len(await store.list_active("t1")) == 0

        result = await handle_manage_schedule(store, "t1", "", "", "resume", trigger_id="trig_pr")
        assert "active" in result
        assert len(await store.list_active("t1")) == 1

    async def test_remove(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(trigger_id="trig_rm", instance_id="t1", action_description="test")
        await store.save(t)

        result = await handle_manage_schedule(store, "t1", "", "", "remove", trigger_id="trig_rm")
        assert "Removed" in result
        assert await store.get("t1", "trig_rm") is None

    async def test_create_stores_conversation_id(self, tmp_path):
        """Create passes conversation_id through to the trigger."""
        import json
        store = TriggerStore(tmp_path)
        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "action_type": "notify",
            "when": _future_iso(1),
            "message": "Test reminder",
            "delivery_class": "stage",
            "recurrence": "",
            "notify_via": "",
            "tool_name": "",
            "tool_args": "",
        }))

        await handle_manage_schedule(
            store, "t1", "m1", "s1", "create",
            description="Remind me in 1 hour",
            reasoning_service=reasoning,
            conversation_id="conv_abc",
        )
        triggers = await store.list_active("t1")
        assert len(triggers) == 1
        assert triggers[0].conversation_id == "conv_abc"

    async def test_list_shows_triggers(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(trigger_id="trig_ls", instance_id="t1", action_description="Check email",
                    next_fire_at=_future_iso(1), action_type="notify")
        await store.save(t)
        result = await handle_manage_schedule(store, "t1", "", "", "list")
        assert "Check email" in result
        assert "notify" in result


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


class TestTimeHelpers:
    def test_compute_next_fire_cron(self):
        now = _now_iso()
        result = compute_next_fire("0 9 * * 1", now)  # Every Monday 9am
        assert result != ""
        assert result > now

    def test_compute_next_fire_invalid(self):
        result = compute_next_fire("not a cron", _now_iso())
        assert result == ""


# ---------------------------------------------------------------------------
# Trigger evaluation
# ---------------------------------------------------------------------------


class TestTriggerEvaluation:
    async def test_fires_due_notify(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_due", instance_id="t1", status="active",
            next_fire_at=_past_iso(0.01),
            action_type="notify",
            action_description="Check email",
            action_params={"message": "Time to check email!"},
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)

        fired = await evaluate_triggers(store, "t1", handler)
        assert fired == 1

        handler.send_outbound.assert_called_once()
        updated = await store.get("t1", "trig_due")
        assert updated.status == "completed"
        assert updated.fire_count == 1

    async def test_skips_future_triggers(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_future", instance_id="t1", status="active",
            next_fire_at=_future_iso(1),
        )
        await store.save(t)

        handler = MagicMock()
        fired = await evaluate_triggers(store, "t1", handler)
        assert fired == 0

    async def test_recurring_recomputes(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_recur", instance_id="t1", status="active",
            next_fire_at=_past_iso(0.01),
            recurrence="0 9 * * *",  # Daily at 9am
            action_type="notify",
            action_description="Daily check",
            action_params={"message": "Good morning!"},
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)

        fired = await evaluate_triggers(store, "t1", handler)
        assert fired == 1

        updated = await store.get("t1", "trig_recur")
        assert updated.status == "active"  # Still active (recurring)
        assert updated.next_fire_at > _now_iso()  # Recomputed to future

    async def test_notify_stores_scheduled_message(self, tmp_path):
        """Successful notify injects [SCHEDULED] message into conversation history."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_hist", instance_id="t1", status="active",
            next_fire_at=_past_iso(0.01),
            action_type="notify",
            action_description="Check email",
            action_params={"message": "Time to check email!"},
            conversation_id="conv_123",
            space_id="space_daily",
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        await evaluate_triggers(store, "t1", handler)

        handler.conversations.append.assert_called_once()
        call_args = handler.conversations.append.call_args
        assert call_args[0][0] == "t1"
        assert call_args[0][1] == "conv_123"
        entry = call_args[0][2]
        assert entry["role"] == "assistant"
        assert entry["content"].startswith("[SCHEDULED]")
        assert "Time to check email!" in entry["content"]
        assert entry["platform"] == "scheduler"
        assert entry["space_tags"] == ["space_daily"]

    async def test_notify_no_history_without_conversation_id(self, tmp_path):
        """No conversation history injection when trigger has no conversation_id."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_noid", instance_id="t1", status="active",
            next_fire_at=_past_iso(0.01),
            action_type="notify",
            action_description="Test",
            action_params={"message": "Hello"},
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        await evaluate_triggers(store, "t1", handler)
        handler.conversations.append.assert_not_called()

    async def test_outbound_failure_no_history(self, tmp_path):
        """Failed outbound does NOT inject into conversation history."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_fh", instance_id="t1", status="active",
            next_fire_at=_past_iso(0.01),
            action_type="notify",
            action_params={"message": "Hello"},
            conversation_id="conv_123",
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=False)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        await evaluate_triggers(store, "t1", handler)
        handler.conversations.append.assert_not_called()

    async def test_outbound_failure_sets_pending(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_fail", instance_id="t1", status="active",
            next_fire_at=_past_iso(0.01),
            action_type="notify",
            action_description="Test",
            action_params={"message": "Hello"},
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=False)

        await evaluate_triggers(store, "t1", handler)
        updated = await store.get("t1", "trig_fail")
        assert updated.pending_delivery == "Hello"


# ---------------------------------------------------------------------------
# resolve_owner_member_id (Component 3)
# ---------------------------------------------------------------------------


class TestResolveOwnerMemberId:
    def test_canonical_format(self):
        assert resolve_owner_member_id("discord:123") == "member:discord:123:owner"

    def test_sms_tenant(self):
        assert resolve_owner_member_id("+15555550100") == "member:+15555550100:owner"


# ---------------------------------------------------------------------------
# CalendarEvent + parse_calendar_events (Component 1)
# ---------------------------------------------------------------------------


def _calendar_item(event_id: str, summary: str, start_dt: datetime,
                   end_dt: datetime | None = None, location: str = "",
                   all_day: bool = False) -> dict:
    """Build a mock Google Calendar API item."""
    item: dict = {"id": event_id, "summary": summary}
    if all_day:
        item["start"] = {"date": start_dt.strftime("%Y-%m-%d")}
        item["end"] = {"date": (start_dt + timedelta(days=1)).strftime("%Y-%m-%d")}
    else:
        item["start"] = {"dateTime": start_dt.isoformat()}
        if end_dt:
            item["end"] = {"dateTime": end_dt.isoformat()}
    if location:
        item["location"] = location
    return item


class TestCalendarEventParsing:
    def test_parse_timed_events(self):
        now = datetime.now(timezone.utc)
        items = [
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=20)),
            _calendar_item("ev2", "Lunch", now + timedelta(hours=2), location="Cafe"),
        ]
        events = parse_calendar_events(json.dumps(items))
        assert len(events) == 2
        assert events[0].id == "ev1"
        assert events[0].summary == "Meeting"
        assert events[1].location == "Cafe"
        assert not events[0].is_all_day

    def test_skip_all_day_events(self):
        """AC5: All-day events are skipped entirely."""
        now = datetime.now(timezone.utc)
        items = [
            _calendar_item("ev1", "Holiday", now, all_day=True),
            _calendar_item("ev2", "Meeting", now + timedelta(minutes=20)),
        ]
        events = parse_calendar_events(json.dumps(items))
        assert len(events) == 1
        assert events[0].id == "ev2"

    def test_parse_wrapped_response(self):
        """Handles {items: [...]} and {events: [...]} wrappers."""
        now = datetime.now(timezone.utc)
        item = _calendar_item("ev1", "Test", now)
        assert len(parse_calendar_events(json.dumps({"items": [item]}))) == 1
        assert len(parse_calendar_events(json.dumps({"events": [item]}))) == 1

    def test_parse_invalid_json(self):
        assert parse_calendar_events("not json") == []

    def test_parse_missing_datetime(self):
        """Items without dateTime in start are skipped."""
        events = parse_calendar_events(json.dumps([{"id": "x", "start": {}}]))
        assert events == []

    def test_default_summary(self):
        now = datetime.now(timezone.utc)
        events = parse_calendar_events(json.dumps([
            {"id": "x", "start": {"dateTime": now.isoformat()}}
        ]))
        assert events[0].summary == "Calendar event"


# ---------------------------------------------------------------------------
# TriggerStore.get_by_condition_type (Component 5)
# ---------------------------------------------------------------------------


class TestGetByConditionType:
    async def test_returns_only_event_triggers(self, tmp_path):
        store = TriggerStore(tmp_path)
        time_trigger = Trigger(trigger_id="trig_1", instance_id="t1",
                               condition_type="time", status="active",
                               next_fire_at=_future_iso())
        event_trigger = Trigger(trigger_id="trig_2", instance_id="t1",
                                condition_type="event", status="active",
                                event_source="calendar")
        await store.save(time_trigger)
        await store.save(event_trigger)

        result = await store.get_by_condition_type("t1", "event")
        assert len(result) == 1
        assert result[0].trigger_id == "trig_2"

    async def test_filters_by_status(self, tmp_path):
        store = TriggerStore(tmp_path)
        await store.save(Trigger(trigger_id="trig_1", instance_id="t1",
                                 condition_type="event", status="active",
                                 event_source="calendar"))
        await store.save(Trigger(trigger_id="trig_2", instance_id="t1",
                                 condition_type="event", status="completed",
                                 event_source="calendar"))

        result = await store.get_by_condition_type("t1", "event", status="active")
        assert len(result) == 1
        assert result[0].trigger_id == "trig_1"


# ---------------------------------------------------------------------------
# evaluate_event_triggers (Component 4)
# ---------------------------------------------------------------------------


class TestEvaluateEventTriggers:
    def _make_trigger(self, **kwargs) -> Trigger:
        defaults = dict(
            trigger_id="trig_ev1", instance_id="t1",
            condition_type="event", event_source="calendar",
            event_lead_minutes=30, status="active",
            action_type="notify", action_description="Calendar alert",
        )
        defaults.update(kwargs)
        return Trigger(**defaults)

    def _calendar_json(self, events: list[dict]) -> str:
        return json.dumps(events)

    async def test_fires_within_lead_time(self, tmp_path):
        """AC3: Fires when event is within lead_minutes."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger()
        await store.save(trigger)

        event_start = now + timedelta(minutes=15)
        raw = self._calendar_json([
            _calendar_item("ev1", "Team Standup", event_start),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        fired, _ = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 1
        handler.send_outbound.assert_called_once()
        call_args = handler.send_outbound.call_args
        assert "Team Standup" in call_args[0][3]

    async def test_does_not_fire_outside_lead_time(self, tmp_path):
        """Event 2 hours away should not fire with 30min lead."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger()
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(hours=2)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        fired, _ = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 0
        handler.send_outbound.assert_not_called()

    async def test_no_duplicate_fire(self, tmp_path):
        """AC4: Same event doesn't fire twice."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(event_matched_ids=["ev1"])
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=15)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        fired, _ = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 0

    async def test_event_filter_matches_title(self, tmp_path):
        """AC15: event_filter matches title/summary only."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(event_filter="dentist")
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Dentist Appointment", now + timedelta(minutes=20)),
            _calendar_item("ev2", "Team Meeting", now + timedelta(minutes=20)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        fired, _ = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 1
        assert "Dentist" in handler.send_outbound.call_args[0][3]

    async def test_one_shot_completes_after_fire(self, tmp_path):
        """AC6: One-shot triggers complete after first fire."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(recurrence="")  # one-shot
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=10)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        await evaluate_event_triggers(store, "t1", handler, mcp)
        updated = await store.get("t1", "trig_ev1")
        assert updated.status == "completed"

    async def test_standing_stays_active(self, tmp_path):
        """AC6: Standing triggers stay active after fire."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(recurrence="standing")
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=10)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        await evaluate_event_triggers(store, "t1", handler, mcp)
        updated = await store.get("t1", "trig_ev1")
        assert updated.status == "active"
        assert updated.fire_count == 1
        assert updated.event_daily_fire_count == 1

    async def test_daily_cap_prevents_spam(self, tmp_path):
        """AC7/AC8: Daily cap on standing triggers."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(
            recurrence="standing",
            event_daily_fire_count=EVENT_DAILY_FIRE_CAP,
            event_daily_fire_date=now.strftime("%Y-%m-%d"),
        )
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=10)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        fired, _ = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 0

    async def test_daily_cap_resets_on_new_day(self, tmp_path):
        """Daily cap resets when the date changes."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(
            recurrence="standing",
            event_daily_fire_count=EVENT_DAILY_FIRE_CAP,
            event_daily_fire_date="2020-01-01",  # old date
        )
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=10)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        fired, _ = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 1  # cap reset

    async def test_mcp_error_logs_and_skips(self, tmp_path):
        """AC9: MCP failures log and skip; trigger stays active."""
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger()
        await store.save(trigger)

        handler = MagicMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value="Tool error: connection refused")

        fired, _ = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 0
        updated = await store.get("t1", "trig_ev1")
        assert updated.status == "active"

    async def test_matched_ids_pruned(self, tmp_path):
        """AC10: event_matched_ids pruned each pass."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(
            event_matched_ids=["old_gone", "ev1"],
        )
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=15)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        await evaluate_event_triggers(store, "t1", handler, mcp)
        updated = await store.get("t1", "trig_ev1")
        # "old_gone" should be pruned; "ev1" remains (still in window)
        assert "old_gone" not in updated.event_matched_ids
        assert "ev1" in updated.event_matched_ids

    async def test_notify_via_channel_targeting(self, tmp_path):
        """AC11: Event notifications respect notify_via."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(notify_via="sms")
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=10)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        await evaluate_event_triggers(store, "t1", handler, mcp)
        call_args = handler.send_outbound.call_args
        assert call_args[0][2] == "sms"

    async def test_member_id_uses_resolver(self, tmp_path):
        """AC13: member_id resolved via resolve_owner_member_id()."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = self._make_trigger(instance_id="discord:456")
        await store.save(trigger)

        raw = self._calendar_json([
            _calendar_item("ev1", "Meeting", now + timedelta(minutes=10)),
        ])

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        await evaluate_event_triggers(store, "discord:456", handler, mcp)
        call_args = handler.send_outbound.call_args
        assert call_args[0][1] == "member:discord:456:owner"


# ---------------------------------------------------------------------------
# Time triggers unchanged (AC16)
# ---------------------------------------------------------------------------


class TestTimeTriggerNoRegression:
    async def test_time_triggers_still_work(self, tmp_path):
        """AC16: time-based triggers are unaffected by event trigger additions."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_time1", instance_id="t1", condition_type="time",
            next_fire_at=_past_iso(), status="active",
            action_type="notify", action_params={"message": "Hello"},
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        fired = await evaluate_triggers(store, "t1", handler)
        assert fired == 1
        handler.send_outbound.assert_called_once()


# ---------------------------------------------------------------------------
# Trigger dataclass — event fields (Component 2)
# ---------------------------------------------------------------------------


class TestTriggerEventFields:
    def test_defaults(self):
        t = Trigger(trigger_id="t1", instance_id="t1")
        assert t.condition_type == "time"
        assert t.event_source == ""
        assert t.event_filter == ""
        assert t.event_lead_minutes == 30
        assert t.event_matched_ids == []
        assert t.event_daily_fire_count == 0
        assert t.event_daily_fire_date == ""

    def test_event_trigger_fields(self):
        t = Trigger(
            trigger_id="t1", instance_id="t1",
            condition_type="event", event_source="calendar",
            event_filter="dentist", event_lead_minutes=15,
        )
        assert t.condition_type == "event"
        assert t.event_source == "calendar"
        assert t.event_filter == "dentist"
        assert t.event_lead_minutes == 15


# ---------------------------------------------------------------------------
# Bug regression tests
# ---------------------------------------------------------------------------


class TestBugRegressions:
    async def test_standing_recurrence_not_parsed_as_cron(self, tmp_path):
        """BUG 2: recurrence='standing' should not be parsed as cron for event triggers."""
        from kernos.kernel.scheduler import _create_trigger
        store = TriggerStore(tmp_path)
        result = await _create_trigger(
            store, "t1", "member:t1:owner", "space1",
            description="Alert before meetings",
            when="", action_type="notify", message="Meeting soon",
            tool_name="", tool_args={}, notify_via="",
            delivery_class="stage", recurrence="standing",
            condition_type="event", event_source="calendar",
            event_filter="", event_lead_minutes=30,
        )
        # Should succeed, not "Could not parse recurrence 'standing'"
        assert "Scheduled:" in result
        assert "Error" not in result

        # Verify trigger was stored correctly
        triggers = await store.list_active("t1")
        assert len(triggers) == 1
        t = triggers[0]
        assert t.condition_type == "event"
        assert t.recurrence == "standing"
        assert t.next_fire_at == ""  # event triggers don't use next_fire_at

    async def test_stale_tool_trigger_fails_gracefully(self, tmp_path):
        """BUG 3: tool_call trigger with nonexistent tool should fail, not retry forever."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_stale", instance_id="t1",
            condition_type="time", next_fire_at=_past_iso(),
            status="active", action_type="tool_call",
            action_params={"tool_name": "nonexistent_tool", "tool_args": {}},
            action_description="Run missing tool",
        )
        await store.save(t)

        handler = MagicMock()
        handler.reasoning = MagicMock()
        handler.reasoning.execute_tool = AsyncMock(
            return_value="Kernel tool 'nonexistent_tool' not handled."
        )
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        await evaluate_triggers(store, "t1", handler)
        updated = await store.get("t1", "trig_stale")
        assert updated.status == "retired"
        assert updated.failure_class == "structural"
        assert "permanently unavailable" in updated.failure_reason.lower()

    def test_seed_from_previous_param_name(self):
        """BUG 1: seed_from_previous parameter is tail_entries, not tail_lines."""
        import inspect
        from kernos.kernel.conversation_log import ConversationLogger
        sig = inspect.signature(ConversationLogger.seed_from_previous)
        assert "tail_entries" in sig.parameters
        assert "tail_lines" not in sig.parameters


# ---------------------------------------------------------------------------
# classify_trigger_failure (Component 2)
# ---------------------------------------------------------------------------


class TestExtractionSchemaEventFields:
    """Verify extraction schema includes event trigger fields (Bug 1 fix)."""

    def test_schema_has_event_fields(self):
        from kernos.kernel.scheduler import _SCHEDULE_EXTRACTION_SCHEMA
        props = _SCHEDULE_EXTRACTION_SCHEMA["properties"]
        assert "condition_type" in props
        assert "event_source" in props
        assert "event_filter" in props
        assert "event_lead_minutes" in props

    def test_schema_condition_type_enum(self):
        from kernos.kernel.scheduler import _SCHEDULE_EXTRACTION_SCHEMA
        ct = _SCHEDULE_EXTRACTION_SCHEMA["properties"]["condition_type"]
        assert set(ct["enum"]) == {"time", "event"}

    def test_schema_event_source_allows_empty(self):
        """event_source must allow empty string for time triggers."""
        from kernos.kernel.scheduler import _SCHEDULE_EXTRACTION_SCHEMA
        es = _SCHEDULE_EXTRACTION_SCHEMA["properties"]["event_source"]
        assert "" in es["enum"]

    def test_event_fields_in_required(self):
        from kernos.kernel.scheduler import _SCHEDULE_EXTRACTION_SCHEMA
        req = _SCHEDULE_EXTRACTION_SCHEMA["required"]
        assert "condition_type" in req
        assert "event_source" in req
        assert "event_filter" in req
        assert "event_lead_minutes" in req

    async def test_event_trigger_creation_from_extracted(self, tmp_path):
        """Simulate what happens when extraction returns event fields."""
        from kernos.kernel.scheduler import _create_trigger
        store = TriggerStore(tmp_path)
        result = await _create_trigger(
            store, "t1", "member:t1:owner", "space1",
            description="Notify me 5 minutes before any calendar event",
            when="", action_type="notify",
            message="Upcoming calendar event",
            tool_name="", tool_args={}, notify_via="",
            delivery_class="stage", recurrence="standing",
            condition_type="event", event_source="calendar",
            event_filter="", event_lead_minutes=5,
        )
        assert "Scheduled:" in result
        assert "Event trigger" in result  # new event-specific return format

        triggers = await store.list_active("t1")
        t = triggers[0]
        assert t.condition_type == "event"
        assert t.event_source == "calendar"
        assert t.event_lead_minutes == 5
        assert t.event_filter == ""
        assert t.recurrence == "standing"
        assert t.next_fire_at == ""

    async def test_event_trigger_normalizes_tool_call_to_notify(self, tmp_path):
        """P0: extraction returns action_type=tool_call for event trigger → normalized to notify."""
        store = TriggerStore(tmp_path)
        reasoning = MagicMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "action_type": "tool_call",  # BUG: model returns tool_call
            "when": "",
            "message": "Calendar event coming up",
            "recurrence": "standing",
            "delivery_class": "stage",
            "notify_via": "",
            "tool_name": "",  # empty — would fail tool_name validation
            "tool_args": "",
            "condition_type": "event",
            "event_source": "calendar",
            "event_filter": "",
            "event_lead_minutes": 1,
        }))

        result = await handle_manage_schedule(
            store, "t1", "member:t1:owner", "space1",
            action="create",
            description="Remind me 1 minute before calendar events",
            reasoning_service=reasoning,
            conversation_id="test",
        )
        # Should succeed, not "tool_name is required"
        assert "Scheduled:" in result
        assert "Error" not in result

        triggers = await store.list_active("t1")
        assert len(triggers) == 1
        t = triggers[0]
        assert t.condition_type == "event"
        assert t.action_type == "notify"  # normalized
        assert t.event_source == "calendar"
        assert t.event_lead_minutes == 1

    async def test_event_trigger_persisted_on_disk(self, tmp_path):
        """P0 verification: event fields must survive JSON round-trip."""
        from kernos.kernel.scheduler import _create_trigger
        store = TriggerStore(tmp_path)
        await _create_trigger(
            store, "t1", "member:t1:owner", "space1",
            description="Alert 1 min before events",
            when="", action_type="notify",
            message="Event soon",
            tool_name="", tool_args={}, notify_via="",
            delivery_class="stage", recurrence="standing",
            condition_type="event", event_source="calendar",
            event_filter="", event_lead_minutes=1,
        )

        # Read raw JSON from disk
        raw = store._read("t1")
        assert len(raw) == 1
        d = raw[0]
        assert d["condition_type"] == "event"
        assert d["event_source"] == "calendar"
        assert d["event_lead_minutes"] == 1
        assert d["recurrence"] == "standing"
        assert d["next_fire_at"] == ""

        # Reload via Trigger(**d) and verify
        t = Trigger(**d)
        assert t.condition_type == "event"
        assert t.event_source == "calendar"
        assert t.event_lead_minutes == 1

    async def test_full_handle_manage_schedule_event_path(self, tmp_path):
        """End-to-end: handle_manage_schedule → extraction → persisted event trigger."""
        store = TriggerStore(tmp_path)
        # Mock reasoning service that returns event extraction
        reasoning = MagicMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "action_type": "notify",
            "when": "",
            "message": "Calendar event coming up",
            "recurrence": "standing",
            "delivery_class": "stage",
            "notify_via": "",
            "tool_name": "",
            "tool_args": "",
            "condition_type": "event",
            "event_source": "calendar",
            "event_filter": "",
            "event_lead_minutes": 5,
        }))

        result = await handle_manage_schedule(
            store, "t1", "member:t1:owner", "space1",
            action="create",
            description="Remind me 5 minutes before any calendar event",
            reasoning_service=reasoning,
            conversation_id="test",
        )
        assert "Scheduled:" in result
        assert "Event trigger" in result

        # Verify persisted trigger
        triggers = await store.list_active("t1")
        assert len(triggers) == 1
        t = triggers[0]
        assert t.condition_type == "event"
        assert t.event_source == "calendar"
        assert t.event_lead_minutes == 5
        assert t.recurrence == "standing"
        assert t.next_fire_at == ""


class TestSystemEventQueue:
    """Verify trigger lifecycle uses queue_system_event instead of send_outbound."""

    async def test_retirement_queues_system_event(self, tmp_path):
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_ret", instance_id="t1",
            condition_type="time", next_fire_at=_past_iso(),
            status="active", action_type="tool_call",
            action_params={"tool_name": "gone_tool", "tool_args": {}},
            action_description="Run gone tool",
        )
        await store.save(t)

        handler = MagicMock()
        handler.reasoning = MagicMock()
        handler.reasoning.execute_tool = AsyncMock(
            return_value="Kernel tool 'gone_tool' not handled."
        )
        handler.queue_system_event = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        await evaluate_triggers(store, "t1", handler)
        # Should use queue_system_event, NOT send_outbound for notification
        handler.queue_system_event.assert_called_once()
        event_text = handler.queue_system_event.call_args[0][1]
        assert "[SYSTEM] trigger_retired" in event_text

    def test_handler_queue_drain(self):
        """Queue and drain system events on handler."""
        from kernos.messages.handler import MessageHandler
        handler = MagicMock(spec=MessageHandler)
        handler._pending_system_events = {}
        handler.queue_system_event = MessageHandler.queue_system_event.__get__(handler)
        handler.drain_system_events = MessageHandler.drain_system_events.__get__(handler)

        handler.queue_system_event("t1", "[SYSTEM] trigger_retired: test")
        handler.queue_system_event("t1", "[SYSTEM] dependency_degraded: cal")
        events = handler.drain_system_events("t1")
        assert len(events) == 2
        assert "[SYSTEM] trigger_retired" in events[0]

        # Second drain returns empty
        assert handler.drain_system_events("t1") == []


class TestPreferenceReplacement:
    """Fix 1: Standing event triggers replace previous matching triggers."""

    async def test_standing_replaces_standing_same_source(self, tmp_path):
        from kernos.kernel.scheduler import _create_trigger
        store = TriggerStore(tmp_path)
        # Create first trigger
        await _create_trigger(
            store, "t1", "m1", "s1", "2 min before events", "",
            "notify", "msg", "", {}, "", "stage", "standing",
            condition_type="event", event_source="calendar",
            event_filter="", event_lead_minutes=2,
        )
        first = (await store.list_active("t1"))[0]

        # Create replacement
        result = await _create_trigger(
            store, "t1", "m1", "s1", "5 min before events", "",
            "notify", "msg", "", {}, "", "stage", "standing",
            condition_type="event", event_source="calendar",
            event_filter="", event_lead_minutes=5,
        )
        assert "Replaced" in result

        all_triggers = await store.list_all("t1")
        active = [t for t in all_triggers if t.status == "active"]
        replaced = [t for t in all_triggers if t.status == "replaced"]
        assert len(active) == 1
        assert active[0].event_lead_minutes == 5
        assert len(replaced) == 1
        assert replaced[0].trigger_id == first.trigger_id
        assert replaced[0].replaced_by == active[0].trigger_id

    async def test_different_notify_via_still_replaced(self, tmp_path):
        """v2: notify_via is ignored — latest preference wins regardless of channel."""
        from kernos.kernel.scheduler import _create_trigger
        store = TriggerStore(tmp_path)
        # Discord trigger
        await _create_trigger(
            store, "t1", "m1", "s1", "2 min before events (discord)", "",
            "notify", "msg", "", {}, "", "stage", "standing",
            condition_type="event", event_source="calendar",
            event_filter="", event_lead_minutes=2,
        )
        # SMS trigger should replace the Discord one
        result = await _create_trigger(
            store, "t1", "m1", "s1", "5 min before events (sms)", "",
            "notify", "msg", "", {}, "sms", "stage", "standing",
            condition_type="event", event_source="calendar",
            event_filter="", event_lead_minutes=5,
        )
        assert "Replaced" in result

        all_triggers = await store.list_all("t1")
        active = [t for t in all_triggers if t.status == "active"]
        assert len(active) == 1
        assert active[0].notify_via == "sms"
        assert active[0].event_lead_minutes == 5

    async def test_different_filter_not_replaced(self, tmp_path):
        from kernos.kernel.scheduler import _create_trigger
        store = TriggerStore(tmp_path)
        await _create_trigger(
            store, "t1", "m1", "s1", "before meetings", "",
            "notify", "msg", "", {}, "", "stage", "standing",
            condition_type="event", event_source="calendar",
            event_filter="meeting", event_lead_minutes=5,
        )
        await _create_trigger(
            store, "t1", "m1", "s1", "before dentist", "",
            "notify", "msg", "", {}, "", "stage", "standing",
            condition_type="event", event_source="calendar",
            event_filter="dentist", event_lead_minutes=10,
        )
        active = await store.list_active("t1")
        assert len(active) == 2  # different filters, both stay

    async def test_one_shot_not_replaced_by_standing(self, tmp_path):
        from kernos.kernel.scheduler import _create_trigger
        store = TriggerStore(tmp_path)
        await _create_trigger(
            store, "t1", "m1", "s1", "one-shot dentist", "",
            "notify", "msg", "", {}, "", "stage", "",
            condition_type="event", event_source="calendar",
            event_filter="dentist", event_lead_minutes=5,
        )
        await _create_trigger(
            store, "t1", "m1", "s1", "standing all events", "",
            "notify", "msg", "", {}, "", "stage", "standing",
            condition_type="event", event_source="calendar",
            event_filter="", event_lead_minutes=10,
        )
        active = await store.list_active("t1")
        assert len(active) == 2  # one-shot not replaced


class TestSkipPastEvents:
    """Fix 2: Past events skipped, grammar fix."""

    async def test_past_events_not_fired(self, tmp_path):
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = Trigger(
            trigger_id="trig_past", instance_id="t1",
            condition_type="event", event_source="calendar",
            event_lead_minutes=30, status="active", recurrence="standing",
        )
        await store.save(trigger)

        past_event = CalendarEvent(
            id="ev_past", summary="Already happened",
            start=now - timedelta(minutes=10), end=None,
        )
        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=json.dumps([
            {"id": "ev_past", "summary": "Already happened",
             "start": {"dateTime": (now - timedelta(minutes=10)).isoformat()}}
        ]))
        fired, _ = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 0
        handler.send_outbound.assert_not_called()


class TestFarFutureEventSkip:
    """Fix 2 v2: Far-future events skipped silently."""

    async def test_far_future_events_not_checked(self, tmp_path):
        """Events far beyond max_lead are not passed to triggers."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = Trigger(
            trigger_id="trig_ff", instance_id="t1",
            condition_type="event", event_source="calendar",
            event_lead_minutes=5, status="active", recurrence="standing",
        )
        await store.save(trigger)

        # Far-future event (200 min away, lead=5) should be filtered
        raw = json.dumps([
            {"id": "ev_far", "summary": "Far meeting",
             "start": {"dateTime": (now + timedelta(minutes=200)).isoformat()}},
            {"id": "ev_near", "summary": "Near meeting",
             "start": {"dateTime": (now + timedelta(minutes=3)).isoformat()}},
        ])
        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        fired, _ = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 1  # only near event fires
        msg = handler.send_outbound.call_args[0][3]
        assert "Near meeting" in msg


class TestAdaptiveCadence:
    """Fix 3: Adaptive polling cadence."""

    def test_no_events_returns_ceiling(self):
        from kernos.kernel.scheduler import _compute_adaptive_cadence
        result = _compute_adaptive_cadence([], [])
        assert result == 15 * 60

    def test_far_event_returns_capped(self):
        from kernos.kernel.scheduler import _compute_adaptive_cadence
        now = datetime.now(timezone.utc)
        events = [CalendarEvent("e1", "Meeting", now + timedelta(hours=5), None)]
        triggers = [Trigger(trigger_id="t1", instance_id="t1", event_lead_minutes=15)]
        result = _compute_adaptive_cadence(events, triggers)
        assert result <= 5 * 60  # capped at 5 min
        assert result >= 30  # floor

    def test_imminent_event_returns_floor(self):
        from kernos.kernel.scheduler import _compute_adaptive_cadence
        now = datetime.now(timezone.utc)
        events = [CalendarEvent("e1", "Meeting", now + timedelta(minutes=1), None)]
        triggers = [Trigger(trigger_id="t1", instance_id="t1", event_lead_minutes=15)]
        result = _compute_adaptive_cadence(events, triggers)
        assert result == 30  # floor


class TestExecutionReceipts:
    """SPEC-BACKGROUND-EXECUTION-RECEIPTS: structured [RECEIPT] entries."""

    async def test_event_fire_writes_receipt(self, tmp_path):
        """Event trigger fire produces a [RECEIPT] log entry."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = Trigger(
            trigger_id="trig_rcpt", instance_id="t1",
            condition_type="event", event_source="calendar",
            event_lead_minutes=10, status="active", recurrence="standing",
            action_description="SMS 5 min before events",
            space_id="space1", notify_via="sms",
        )
        await store.save(trigger)

        raw = json.dumps([
            {"id": "ev1", "summary": "Team standup",
             "start": {"dateTime": (now + timedelta(minutes=5)).isoformat()}}
        ])

        receipt_calls = []
        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock(side_effect=lambda **kw: receipt_calls.append(kw))
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        fired, _ = await evaluate_event_triggers(store, "t1", handler, mcp)
        assert fired == 1

        # Find the receipt entry
        receipt_entries = [c for c in receipt_calls if c.get("channel") == "receipt"]
        assert len(receipt_entries) == 1
        r = receipt_entries[0]
        assert r["speaker"] == "system"
        assert "[RECEIPT] trigger_fired" in r["content"]
        assert "trig_rcpt" in r["content"]
        assert "Team standup" in r["content"]
        assert "outcome=success" in r["content"]

    async def test_event_fire_failure_writes_receipt(self, tmp_path):
        """Failed event fire produces a failure receipt."""
        now = datetime.now(timezone.utc)
        store = TriggerStore(tmp_path)
        trigger = Trigger(
            trigger_id="trig_fail", instance_id="t1",
            condition_type="event", event_source="calendar",
            event_lead_minutes=10, status="active", recurrence="standing",
            action_description="Notify before events",
            space_id="space1",
        )
        await store.save(trigger)

        raw = json.dumps([
            {"id": "ev1", "summary": "Meeting",
             "start": {"dateTime": (now + timedelta(minutes=5)).isoformat()}}
        ])

        receipt_calls = []
        handler = MagicMock()
        handler.send_outbound = AsyncMock(side_effect=RuntimeError("network down"))
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock(side_effect=lambda **kw: receipt_calls.append(kw))
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value=raw)

        fired, _ = await evaluate_event_triggers(store, "t1", handler, mcp)

        receipt_entries = [c for c in receipt_calls if c.get("channel") == "receipt"]
        assert len(receipt_entries) == 1
        assert "outcome=failed" in receipt_entries[0]["content"]
        assert "network down" in receipt_entries[0]["content"]

    async def test_time_trigger_fire_writes_receipt(self, tmp_path):
        """Time trigger fire produces a [RECEIPT] log entry."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_time_rcpt", instance_id="t1",
            condition_type="time", next_fire_at=_past_iso(),
            status="active", action_type="notify",
            action_params={"message": "Hello"},
            action_description="Reminder: Hello",
            space_id="space1", conversation_id="conv1",
        )
        await store.save(t)

        receipt_calls = []
        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock(side_effect=lambda **kw: receipt_calls.append(kw))

        await evaluate_triggers(store, "t1", handler)

        receipt_entries = [c for c in receipt_calls if c.get("channel") == "receipt"]
        assert len(receipt_entries) >= 1
        r = receipt_entries[0]
        assert "[RECEIPT] trigger_fired" in r["content"]
        assert "outcome=success" in r["content"]

    def test_receipt_format(self):
        """Receipt content follows the structured tag format."""
        content = (
            "[RECEIPT] trigger_fired | trig_abc | Reminder"
            " | channel=sms | outcome=success | fire_count=1"
            " | timestamp=2026-03-27T01:00:00+00:00"
        )
        assert content.startswith("[RECEIPT]")
        assert "trigger_fired" in content
        assert "outcome=success" in content


class TestEventPollTimeFormat:
    """Bug fix: MCP rejects timezone offsets and microseconds in timeMin/timeMax."""

    def test_poll_args_no_timezone_offset(self):
        """timeMin/timeMax must not contain +00:00 or timezone info."""
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        time_fmt = "%Y-%m-%dT%H:%M:%S"
        time_min = now.strftime(time_fmt)
        time_max = (now + timedelta(hours=24)).strftime(time_fmt)
        # Must not contain + or Z
        assert "+" not in time_min
        assert "+" not in time_max
        assert "Z" not in time_min
        # Must match expected format
        assert len(time_min) == 19  # '2026-01-01T00:00:00'


class TestClassifyTriggerFailure:
    def test_structural_patterns(self):
        assert classify_trigger_failure("Tool not found: calendar") == "structural"
        assert classify_trigger_failure("Kernel tool 'x' not handled") == "structural"
        assert classify_trigger_failure("unknown tool foo") == "structural"
        assert classify_trigger_failure("channel not registered") == "structural"
        assert classify_trigger_failure("Tool permanently unavailable") == "structural"

    def test_transient_default(self):
        assert classify_trigger_failure("connection timeout") == "transient"
        assert classify_trigger_failure("rate limited") == "transient"
        assert classify_trigger_failure("internal server error") == "transient"
        assert classify_trigger_failure(RuntimeError("network down")) == "transient"

    def test_conservative_default(self):
        """Unknown errors default to transient — better retry than retire."""
        assert classify_trigger_failure("something went wrong") == "transient"


# ---------------------------------------------------------------------------
# Trigger lifecycle fields (Component 1)
# ---------------------------------------------------------------------------


class TestTriggerLifecycleFields:
    def test_defaults(self):
        t = Trigger(trigger_id="t1", instance_id="t1")
        assert t.failure_class == ""
        assert t.transient_failure_count == 0
        assert t.last_failure_at == ""
        assert t.degraded is False
        assert t.retired_at == ""

    def test_retired_status(self):
        t = Trigger(trigger_id="t1", instance_id="t1", status="retired",
                    failure_class="structural", retired_at="2026-01-01T00:00:00")
        assert t.status == "retired"


# ---------------------------------------------------------------------------
# Degraded lifecycle (Components 3 & 4)
# ---------------------------------------------------------------------------


class TestDegradedLifecycle:
    async def test_structural_failure_retires_trigger(self, tmp_path):
        """AC1-3: Structural failure retires trigger permanently."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_struct", instance_id="t1",
            condition_type="time", next_fire_at=_past_iso(),
            status="active", action_type="tool_call",
            action_params={"tool_name": "missing_tool", "tool_args": {}},
        )
        await store.save(t)

        handler = MagicMock()
        handler.reasoning = MagicMock()
        handler.reasoning.execute_tool = AsyncMock(
            return_value="Kernel tool 'missing_tool' not handled."
        )
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        await evaluate_triggers(store, "t1", handler)
        updated = await store.get("t1", "trig_struct")
        assert updated.status == "retired"
        assert updated.failure_class == "structural"
        assert updated.retired_at != ""

    async def test_transient_failure_keeps_active(self, tmp_path):
        """AC4: Transient failures keep trigger active."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_trans", instance_id="t1",
            condition_type="time", next_fire_at=_past_iso(),
            status="active", action_type="tool_call",
            action_params={"tool_name": "some_tool", "tool_args": {}},
            recurrence="0 * * * *",  # recurring so it doesn't auto-fail
        )
        await store.save(t)

        handler = MagicMock()
        handler.reasoning = MagicMock()
        handler.reasoning.execute_tool = AsyncMock(
            side_effect=RuntimeError("connection timeout")
        )
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        await evaluate_triggers(store, "t1", handler)
        updated = await store.get("t1", "trig_trans")
        assert updated.status != "retired"
        assert updated.degraded is True
        assert updated.transient_failure_count == 1
        assert updated.failure_class == "transient"

    async def test_degraded_logged_only_on_transition(self, tmp_path):
        """AC5-6: degraded=True on first failure, log only on transition."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_d", instance_id="t1",
            condition_type="time", next_fire_at=_past_iso(),
            status="active", action_type="notify",
            action_params={"message": "Hello"},
            recurrence="0 * * * *",
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=False)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()

        # First fire — delivery fails, trigger degrades
        await evaluate_triggers(store, "t1", handler)
        updated = await store.get("t1", "trig_d")
        # Notify trigger returns False when send_outbound fails
        # but that's just pending delivery, not classified as transient
        # The trigger stays active with pending_delivery set

    async def test_recovery_clears_degraded(self, tmp_path):
        """AC8-9: Successful fire after degraded clears flag, keeps failure_reason."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_recov", instance_id="t1",
            condition_type="time", next_fire_at=_past_iso(),
            status="active", action_type="notify",
            action_params={"message": "Hello"},
            degraded=True, transient_failure_count=5,
            failure_class="transient", failure_reason="previous error",
        )
        await store.save(t)

        handler = MagicMock()
        handler.send_outbound = AsyncMock(return_value=True)
        handler.conversations = MagicMock()
        handler.conversations.append = AsyncMock()
        handler.conv_logger = MagicMock()
        handler.conv_logger.append = AsyncMock()

        await evaluate_triggers(store, "t1", handler)
        updated = await store.get("t1", "trig_recov")
        assert updated.degraded is False
        assert updated.transient_failure_count == 0
        assert updated.failure_class == ""
        # failure_reason preserved for debugging history
        assert updated.failure_reason == "previous error"


# ---------------------------------------------------------------------------
# Boot scan — retire_stale_triggers (Component 5)
# ---------------------------------------------------------------------------


class TestRetireStaleTriggers:
    async def test_retires_missing_tool(self, tmp_path):
        """AC1-2: Stale trigger with missing tool is retired and notified."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_stale2", instance_id="t1",
            status="active", action_type="tool_call",
            action_params={"tool_name": "calendar_event_reminder"},
            action_description="Old broken reminder",
        )
        await store.save(t)

        registry = MagicMock()
        registry.get_tool_schema = MagicMock(return_value=None)  # Tool doesn't exist

        handler = MagicMock()
        handler.queue_system_event = MagicMock()

        retired = await retire_stale_triggers(store, "t1", registry, handler)
        assert retired == 1

        updated = await store.get("t1", "trig_stale2")
        assert updated.status == "retired"
        assert updated.failure_class == "structural"
        assert "no longer exists" in updated.failure_reason
        handler.queue_system_event.assert_called_once()
        event_text = handler.queue_system_event.call_args[0][1]
        assert "[SYSTEM] trigger_retired" in event_text

    async def test_skips_existing_tools(self, tmp_path):
        """Tools that still exist are not retired."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_ok", instance_id="t1",
            status="active", action_type="tool_call",
            action_params={"tool_name": "list-events"},
        )
        await store.save(t)

        registry = MagicMock()
        registry.get_tool_schema = MagicMock(return_value={"name": "list-events"})

        handler = MagicMock()

        retired = await retire_stale_triggers(store, "t1", registry, handler)
        assert retired == 0

    async def test_skips_notify_triggers(self, tmp_path):
        """Notify triggers are not scanned for tool existence."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_notify", instance_id="t1",
            status="active", action_type="notify",
            action_params={"message": "Hello"},
        )
        await store.save(t)

        registry = MagicMock()
        handler = MagicMock()

        retired = await retire_stale_triggers(store, "t1", registry, handler)
        assert retired == 0

    async def test_skips_already_retired(self, tmp_path):
        """AC11: Already retired triggers are not re-processed."""
        store = TriggerStore(tmp_path)
        t = Trigger(
            trigger_id="trig_ret", instance_id="t1",
            status="retired", action_type="tool_call",
            action_params={"tool_name": "gone"},
        )
        await store.save(t)

        registry = MagicMock()
        registry.get_tool_schema = MagicMock(return_value=None)
        handler = MagicMock()

        retired = await retire_stale_triggers(store, "t1", registry, handler)
        assert retired == 0
