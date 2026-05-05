"""Tests for the Friction Observer — post-turn diagnostic detection."""
import os
import pytest
from unittest.mock import AsyncMock, MagicMock

from kernos.kernel.friction import FrictionObserver, FrictionSignal


# ===========================
# Signal 1: TOOL_REQUEST_FOR_SURFACED_TOOL
# ===========================

class TestRequestForSurfaced:
    async def test_detects_request_for_surfaced_calendar(self):
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="Create a calendar event for 10pm",
            response_text="I'll create that for you",
            tool_trace=[{
                "name": "request_tool",
                "input": {"capability_name": "unknown", "description": "calendar event creation"},
                "success": True,
            }],
            surfaced_tool_names={"create-event", "list-events", "remember", "request_reference"},
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
        )
        types = [s.signal_type for s in signals]
        assert "TOOL_REQUEST_FOR_SURFACED_TOOL" in types

    async def test_no_signal_when_tool_not_surfaced(self):
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="Create a calendar event",
            response_text="I'll need to request that tool",
            tool_trace=[{
                "name": "request_tool",
                "input": {"capability_name": "unknown", "description": "I need a weather tool"},
                "success": True,
            }],
            surfaced_tool_names={"remember", "request_reference"},
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
        )
        types = [s.signal_type for s in signals]
        assert "TOOL_REQUEST_FOR_SURFACED_TOOL" not in types

    async def test_no_signal_when_no_request_tool_call(self):
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="Create a calendar event",
            response_text="Done!",
            tool_trace=[{"name": "create-event", "input": {"summary": "Test"}, "success": True}],
            surfaced_tool_names={"create-event", "list-events"},
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
        )
        types = [s.signal_type for s in signals]
        assert "TOOL_REQUEST_FOR_SURFACED_TOOL" not in types


# ===========================
# Signal 2: STALE_DATA_IN_RESPONSE (heuristic)
# ===========================

class TestStaleData:
    async def test_detects_time_query_without_tool(self):
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="What time is it?",
            response_text="It's 3:45 PM",
            tool_trace=[],
            surfaced_tool_names={"get-current-time"},
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
        )
        stale = [s for s in signals if s.signal_type == "STALE_DATA_IN_RESPONSE"]
        assert len(stale) == 1
        assert stale[0].heuristic is True

    async def test_no_signal_when_tool_called(self):
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="What time is it?",
            response_text="It's 3:45 PM",
            tool_trace=[{"name": "get-current-time", "input": {}, "success": True}],
            surfaced_tool_names={"get-current-time"},
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
        )
        stale = [s for s in signals if s.signal_type == "STALE_DATA_IN_RESPONSE"]
        assert len(stale) == 0


# ===========================
# Signal 4: TOOL_AVAILABLE_BUT_NOT_USED (heuristic)
# ===========================

class TestToolNotUsed:
    async def test_detects_state_query_without_inspect(self):
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="What preferences do I have set up?",
            response_text="You like to wake up at 6:30.",
            tool_trace=[],
            surfaced_tool_names={"inspect_state", "remember"},
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
        )
        types = [s.signal_type for s in signals]
        assert "TOOL_AVAILABLE_BUT_NOT_USED" in types

    async def test_no_signal_when_inspect_called(self):
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="What preferences do I have set up?",
            response_text="Here are your preferences...",
            tool_trace=[{"name": "inspect_state", "input": {}, "success": True}],
            surfaced_tool_names={"inspect_state", "remember"},
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
        )
        types = [s.signal_type for s in signals]
        assert "TOOL_AVAILABLE_BUT_NOT_USED" not in types


# ===========================
# Signal 6: MERGED_MESSAGES_DROPPED
# ===========================

class TestMergedDropped:
    async def test_detects_short_response_for_merged(self):
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="Three things at once",
            response_text="OK",  # Way too short for 3 messages
            tool_trace=[],
            surfaced_tool_names=set(),
            active_space_id="space_1",
            merged_count=3,
            is_reactive=True,
            pref_detected=False,
        )
        types = [s.signal_type for s in signals]
        assert "MERGED_MESSAGES_DROPPED" in types

    async def test_no_signal_for_single_message(self):
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="Just one thing",
            response_text="OK",
            tool_trace=[],
            surfaced_tool_names=set(),
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
        )
        types = [s.signal_type for s in signals]
        assert "MERGED_MESSAGES_DROPPED" not in types


# ===========================
# Signal 7: PREFERENCE_STATED_BUT_NOT_CAPTURED
# ===========================

class TestPrefMissed:
    async def test_detects_missed_preference(self):
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="From now on, always remind me before meetings",
            response_text="Got it!",
            tool_trace=[],
            surfaced_tool_names=set(),
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
        )
        types = [s.signal_type for s in signals]
        assert "PREFERENCE_STATED_BUT_NOT_CAPTURED" in types

    async def test_no_signal_when_detected(self):
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="From now on, always remind me before meetings",
            response_text="Got it!",
            tool_trace=[],
            surfaced_tool_names=set(),
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=True,  # Parser caught it
        )
        types = [s.signal_type for s in signals]
        assert "PREFERENCE_STATED_BUT_NOT_CAPTURED" not in types

    async def test_no_signal_for_non_preference(self):
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="What's the weather like today?",
            response_text="It's sunny.",
            tool_trace=[],
            surfaced_tool_names=set(),
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
        )
        types = [s.signal_type for s in signals]
        assert "PREFERENCE_STATED_BUT_NOT_CAPTURED" not in types


# ===========================
# Signal 8: PROVIDER_ERROR_REPEATED
# ===========================

class TestProviderErrorRepeated:
    async def test_detects_repeated_errors(self):
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="hello",
            response_text="hi",
            tool_trace=[],
            surfaced_tool_names=set(),
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
            provider_errors=[
                "Codex stream error: unknown",
                "Codex stream error: unknown",
                "Codex stream error: unknown",
            ],
        )
        types = [s.signal_type for s in signals]
        assert "PROVIDER_ERROR_REPEATED" in types
        sig = [s for s in signals if s.signal_type == "PROVIDER_ERROR_REPEATED"][0]
        assert "3x" in sig.description

    async def test_no_signal_for_single_error(self):
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="hello",
            response_text="hi",
            tool_trace=[],
            surfaced_tool_names=set(),
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
            provider_errors=["Some error"],
        )
        types = [s.signal_type for s in signals]
        assert "PROVIDER_ERROR_REPEATED" not in types

    async def test_no_signal_for_no_errors(self):
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="hello",
            response_text="hi",
            tool_trace=[],
            surfaced_tool_names=set(),
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
        )
        types = [s.signal_type for s in signals]
        assert "PROVIDER_ERROR_REPEATED" not in types


# ===========================
# Schedule query false positive fix
# ===========================

class TestScheduleQueryFalsePositive:
    async def test_no_signal_when_list_events_called(self):
        """'What's on my schedule?' + list-events called → no friction."""
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="What reminders do I have?",
            response_text="You have a meeting at 3pm",
            tool_trace=[{"name": "list-events", "input": {}, "success": True}],
            surfaced_tool_names={"list-events", "manage_schedule"},
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
        )
        types = [s.signal_type for s in signals]
        assert "TOOL_AVAILABLE_BUT_NOT_USED" not in types

    async def test_signal_when_trigger_query_unanswered(self):
        """'What reminders do I have?' + no manage_schedule → friction."""
        obs = FrictionObserver(enabled=True, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="What reminders do I have?",
            response_text="You don't have any reminders",
            tool_trace=[],
            surfaced_tool_names={"manage_schedule"},
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
        )
        types = [s.signal_type for s in signals]
        assert "TOOL_AVAILABLE_BUT_NOT_USED" in types


# ===========================
# Report writing
# ===========================

class TestReportWriting:
    async def test_report_written_to_disk(self, tmp_path):
        obs = FrictionObserver(enabled=True, data_dir=str(tmp_path))
        signals = await obs.observe(
            instance_id="t1",
            user_message="What time is it?",
            response_text="It's noon",
            tool_trace=[],
            surfaced_tool_names={"get-current-time"},
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
        )
        assert len(signals) > 0

        friction_dir = tmp_path / "diagnostics" / "friction"
        assert friction_dir.exists()
        files = list(friction_dir.glob("FRICTION_*.md"))
        assert len(files) >= 1

        content = files[0].read_text()
        assert "# Friction Report:" in content
        assert "## Description" in content
        assert "## Recommendation:" in content
        assert "## Evidence" in content
        assert "## Context" in content

    async def test_report_contains_recommendation(self, tmp_path):
        obs = FrictionObserver(enabled=True, data_dir=str(tmp_path))
        await obs.observe(
            instance_id="t1",
            user_message="What time is it?",
            response_text="It's noon",
            tool_trace=[],
            surfaced_tool_names={"get-current-time"},
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
        )
        friction_dir = tmp_path / "diagnostics" / "friction"
        content = list(friction_dir.glob("FRICTION_*.md"))[0].read_text()
        # Must contain one of the valid recommendations
        assert any(r in content for r in ["REMOVE", "STRUCTURAL_ENFORCE", "SIMPLIFY", "ADD"])


# ===========================
# Disabled observer
# ===========================

class TestDisabled:
    async def test_disabled_produces_no_signals(self):
        obs = FrictionObserver(enabled=False, data_dir="/tmp/friction_test")
        signals = await obs.observe(
            instance_id="t1",
            user_message="What time is it?",
            response_text="It's noon",
            tool_trace=[],
            surfaced_tool_names={"get-current-time"},
            active_space_id="space_1",
            merged_count=1,
            is_reactive=True,
            pref_detected=False,
        )
        assert signals == []


# ===========================
# Default recommendations
# ===========================

class TestDefaultRecommendations:
    def test_default_recommendations(self):
        obs = FrictionObserver()
        assert obs._default_recommendation("TOOL_REQUEST_FOR_SURFACED_TOOL") == "SIMPLIFY"
        assert obs._default_recommendation("GATE_CONFIRM_ON_REACTIVE") == "STRUCTURAL_ENFORCE"
        assert obs._default_recommendation("SCHEMA_ERROR_ON_PROVIDER") == "STRUCTURAL_ENFORCE"
        assert obs._default_recommendation("UNKNOWN_TYPE") == "SIMPLIFY"


# ===========================
# Reasoning service trace
# ===========================

class TestToolTrace:
    def test_drain_clears_trace(self):
        from kernos.kernel.reasoning import ReasoningService
        from unittest.mock import MagicMock
        provider = MagicMock()
        provider.main_model = "test"
        events = MagicMock()
        mcp = MagicMock()
        audit = MagicMock()
        svc = ReasoningService(provider, events, mcp, audit)
        svc._turn_tool_trace = [{"name": "foo", "input": {}, "success": True}]
        trace = svc.drain_tool_trace()
        assert len(trace) == 1
        assert trace[0]["name"] == "foo"
        assert svc._turn_tool_trace == []

    def test_drain_empty(self):
        from kernos.kernel.reasoning import ReasoningService
        from unittest.mock import MagicMock
        provider = MagicMock()
        provider.main_model = "test"
        svc = ReasoningService(provider, MagicMock(), MagicMock(), MagicMock())
        assert svc.drain_tool_trace() == []
