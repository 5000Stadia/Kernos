"""Tests for runtime trace — Improvement Loop Tier 2 Pass 1."""
import asyncio
import json
import pytest
from pathlib import Path

from kernos.kernel import runtime_trace as runtime_trace_module
from kernos.kernel.runtime_trace import (
    TraceEvent,
    TurnEventCollector,
    RuntimeTrace,
    generate_turn_id,
    MAX_TURNS,
)


class TestTurnEventCollector:
    def test_record_event(self):
        c = TurnEventCollector("turn_abc")
        c.record("warning", "codex_provider", "CODEX_STREAM_ERROR",
                 "server_error: An error occurred", phase="reason")
        assert len(c.events) == 1
        assert c.events[0].turn_id == "turn_abc"
        assert c.events[0].level == "warning"
        assert c.events[0].event == "CODEX_STREAM_ERROR"
        assert c.events[0].phase == "reason"

    def test_multiple_events(self):
        c = TurnEventCollector("turn_xyz")
        c.record("info", "handler", "COVENANT_INJECT", "pinned=7 relevant=2")
        c.record("info", "handler", "TURN_TIMING", "total=5000ms", duration_ms=5000)
        assert len(c.events) == 2
        assert c.events[1].duration_ms == 5000

    def test_detail_truncated(self):
        c = TurnEventCollector("turn_t")
        c.record("error", "provider", "BIG_ERROR", "x" * 1000)
        assert len(c.events[0].detail) == 500


class TestGenerateTurnId:
    def test_format(self):
        tid = generate_turn_id()
        assert tid.startswith("turn_")
        assert len(tid) > 10

    def test_unique(self):
        ids = {generate_turn_id() for _ in range(100)}
        assert len(ids) == 100


class TestRuntimeTrace:
    @pytest.fixture
    def trace(self, tmp_path):
        return RuntimeTrace(str(tmp_path))

    async def test_append_and_read(self, trace, tmp_path):
        events = [
            TraceEvent("turn_1", "2026-04-11T00:00:00Z", "warning",
                       "provider", "CODEX_ERROR", "server error", "reason"),
            TraceEvent("turn_1", "2026-04-11T00:00:01Z", "info",
                       "handler", "FALLBACK", "success via ollama", "reason"),
        ]
        await trace.append_turn("tenant1", events)

        result = await trace.read("tenant1")
        assert len(result) == 2
        assert result[0]["event"] == "CODEX_ERROR"

    async def test_read_empty(self, trace):
        result = await trace.read("tenant_nonexistent")
        assert result == []

    async def test_filter_by_level(self, trace):
        events = [
            TraceEvent("turn_1", "t1", "warning", "p", "E1", "d1"),
            TraceEvent("turn_1", "t2", "info", "p", "E2", "d2"),
            TraceEvent("turn_1", "t3", "error", "p", "E3", "d3"),
        ]
        await trace.append_turn("t1", events)

        errors = await trace.read("t1", filter_level="error")
        assert len(errors) == 1
        assert errors[0]["event"] == "E3"

        warnings = await trace.read("t1", filter_level="warning")
        assert len(warnings) == 1

    async def test_filter_by_turn_id(self, trace):
        events1 = [TraceEvent("turn_A", "t1", "info", "p", "E1", "d1")]
        events2 = [TraceEvent("turn_B", "t2", "info", "p", "E2", "d2")]
        await trace.append_turn("t1", events1)
        await trace.append_turn("t1", events2)

        result = await trace.read("t1", turn_id="turn_A")
        assert len(result) == 1
        assert result[0]["turn_id"] == "turn_A"

    async def test_turns_limit(self, trace):
        for i in range(20):
            events = [TraceEvent(f"turn_{i}", f"ts_{i}", "info", "p", "E", "d")]
            await trace.append_turn("t1", events)

        result = await trace.read("t1", turns=5)
        turn_ids = {e["turn_id"] for e in result}
        assert len(turn_ids) == 5
        # Should be the 5 most recent
        assert "turn_19" in turn_ids
        assert "turn_15" in turn_ids

    async def test_rotation(self, trace):
        # Append more than MAX_TURNS
        for i in range(MAX_TURNS + 20):
            events = [TraceEvent(f"turn_{i}", f"ts_{i:06d}", "info", "p", "E", "d")]
            await trace.append_turn("t1", events)

        # Should have been rotated to MAX_TURNS
        all_events = await trace.read("t1", turns=MAX_TURNS)
        turn_ids = {e["turn_id"] for e in all_events}
        assert len(turn_ids) <= MAX_TURNS
        # Oldest should be gone
        assert "turn_0" not in turn_ids
        # Newest should be present
        assert f"turn_{MAX_TURNS + 19}" in turn_ids

    async def test_concurrent_rotation_keeps_both_appended_turns(self, trace, monkeypatch):
        instance_id = "t1"
        for i in range(MAX_TURNS):
            events = [TraceEvent(f"seed_{i}", f"ts_{i:06d}", "info", "p", "E", "seed")]
            await trace.append_turn(instance_id, events)

        path = trace._trace_path(instance_id)
        original_run_sync_io = runtime_trace_module._run_sync_io
        a_read = asyncio.Event()
        b_appended = asyncio.Event()
        a_rotated = asyncio.Event()

        def trace_lock_held() -> bool:
            return any(lock.locked() for lock in getattr(trace, "_trace_locks", {}).values())

        async def controlled_run_sync_io(fn, *args, **kwargs):
            task = asyncio.current_task()
            task_name = task.get_name() if task else ""
            fn_name = getattr(fn, "__name__", "")
            is_append = fn is runtime_trace_module._append_events_sync
            is_read = fn_name == "read_text"
            is_rotate = fn_name == "_rotate"

            if task_name == "trace-writer-b" and is_append:
                await a_read.wait()
            if task_name == "trace-writer-b" and is_read:
                await a_rotated.wait()
            if task_name == "trace-writer-a" and is_rotate and not trace_lock_held():
                await asyncio.wait_for(b_appended.wait(), timeout=5)

            result = await original_run_sync_io(fn, *args, **kwargs)

            if task_name == "trace-writer-a" and is_read:
                a_read.set()
            if task_name == "trace-writer-b" and is_append:
                b_appended.set()
            if task_name == "trace-writer-a" and is_rotate:
                a_rotated.set()

            return result

        monkeypatch.setattr(runtime_trace_module, "_run_sync_io", controlled_run_sync_io)

        async def append_marker(turn_id: str, marker: str) -> None:
            await trace.append_turn(
                instance_id,
                [TraceEvent(turn_id, f"ts_{turn_id}", "info", "p", "E", marker)],
            )

        task_a = asyncio.create_task(
            append_marker("turn_concurrent_a", "marker-A"),
            name="trace-writer-a",
        )
        await a_read.wait()
        task_b = asyncio.create_task(
            append_marker("turn_concurrent_b", "marker-B"),
            name="trace-writer-b",
        )
        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)

        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        markers_by_turn = {record["turn_id"]: record["detail"] for record in records}
        turn_ids = set(markers_by_turn)

        assert len(records) == MAX_TURNS
        assert len(turn_ids) == MAX_TURNS
        assert markers_by_turn["turn_concurrent_a"] == "marker-A"
        assert markers_by_turn["turn_concurrent_b"] == "marker-B"

    async def test_filter_provider(self, trace):
        events = [
            TraceEvent("t1", "ts", "warning", "p", "CODEX_STREAM_ERROR", "d"),
            TraceEvent("t1", "ts", "info", "p", "COVENANT_INJECT", "d"),
            TraceEvent("t1", "ts", "info", "p", "FALLBACK_TOOLLOOP", "d"),
        ]
        await trace.append_turn("t1", events)

        result = await trace.read("t1", filter_level="provider")
        assert len(result) == 2  # CODEX + FALLBACK

    async def test_filter_gate(self, trace):
        events = [
            TraceEvent("t1", "ts", "info", "p", "GATE_DENIED", "d"),
            TraceEvent("t1", "ts", "info", "p", "GATE_APPROVED", "d"),
            TraceEvent("t1", "ts", "info", "p", "SOMETHING_ELSE", "d"),
        ]
        await trace.append_turn("t1", events)

        result = await trace.read("t1", filter_level="gate")
        assert len(result) == 2
