"""Spec 6 commit 4: autonomy-loop emitter tests.

Pins the two production-assembly-owned emitters that translate
substrate-side signals into the canonical autonomy-loop event types
the self_improvement workflow's triggers match on.

Test shape per architect user-feedback: every mechanic has BOTH a
unit pin AND a functional pin where the mechanic is exercised under
its expected workflow-side use and the expected outcome is asserted.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kernos.kernel import event_stream
from kernos.kernel.friction_patterns import (
    CLASSIFIED_AUTO_SIGNAL_TYPE,
    FrictionPatternStore,
    LIFECYCLE_REACTIVATED,
    LIFECYCLE_RESOLVED,
)
from kernos.kernel.workflows.autonomy_emitters import (
    CodingSessionBridgeResponseEmitter,
    FrictionPatternFrequencyEmitter,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
async def event_stream_writer(tmp_path):
    """Start + tear down the event_stream writer."""
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path), flush_interval_s=0.05)
    yield tmp_path
    await event_stream.stop_writer()
    await event_stream._reset_for_tests()


@pytest.fixture
async def pattern_store(tmp_path):
    s = FrictionPatternStore()
    await s.start(str(tmp_path))
    yield s
    await s.stop()


async def _fetch_all_events(instance_id: str) -> list:
    """Helper: fetch all events for an instance via the existing
    events_in_window API. Window spans 1 day back through 1 day
    forward — wide enough for any test event."""
    now = datetime.now(timezone.utc)
    return await event_stream.events_in_window(
        instance_id,
        now.replace(year=now.year - 1),
        now.replace(year=now.year + 1),
        limit=1000,
    )


async def _wait_until(predicate, timeout_s: float = 2.0, step_s: float = 0.02):
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step_s)
    return False


# ===========================================================================
# FrictionPatternFrequencyEmitter
# ===========================================================================


class TestFrictionPatternFrequencyEmitter:
    """Translates friction.pattern_reactivated → friction.pattern_frequency_threshold_exceeded
    with active_epoch in the payload."""

    async def test_start_stop_lifecycle(
        self, event_stream_writer, pattern_store,
    ):
        """Unit pin: start registers the post-flush hook; stop
        unregisters. Idempotent on both calls. Hook equality uses
        ``==`` (bound methods compare by underlying function + instance)
        because each ``emitter._on_flush`` attribute access creates a
        fresh BoundMethod object — ``is`` identity would always fail."""
        emitter = FrictionPatternFrequencyEmitter(
            instance_id="inst_a", pattern_store=pattern_store,
        )
        await emitter.start()
        hooks = event_stream._registered_post_flush_hooks()
        assert emitter._on_flush in hooks
        # Idempotent re-start: still exactly one entry equal to the hook.
        await emitter.start()
        hooks = event_stream._registered_post_flush_hooks()
        assert sum(1 for h in hooks if h == emitter._on_flush) == 1
        # Stop unregisters.
        await emitter.stop()
        hooks = event_stream._registered_post_flush_hooks()
        assert emitter._on_flush not in hooks
        # Idempotent stop.
        await emitter.stop()

    async def test_translates_reactivation_event_with_active_epoch(
        self, event_stream_writer, pattern_store, monkeypatch,
    ):
        """FUNCTIONAL pin (architect's user-feedback request): exercise
        the mechanic end-to-end. Create + resolve a pattern, then
        record a recurrence that crosses the threshold (substrate emits
        friction.pattern_reactivated). The emitter observes the event
        on flush, looks up active_epoch, and emits
        friction.pattern_frequency_threshold_exceeded with the epoch
        in payload — the canonical autonomy-loop trigger shape."""
        # Threshold=1 so single recurrence triggers reactivation.
        monkeypatch.setenv("KERNOS_FRICTION_REACTIVATION_THRESHOLD", "1")
        monkeypatch.setenv(
            "KERNOS_FRICTION_REACTIVATION_WINDOW_DAYS", "365",
        )
        emitter = FrictionPatternFrequencyEmitter(
            instance_id="inst_a", pattern_store=pattern_store,
        )
        await emitter.start()
        try:
            # Create + resolve a pattern. The active_epoch=1 at create
            # time; once reactivated, epoch=2.
            p = await pattern_store.create_pattern(
                instance_id="inst_a",
                description="freq test pattern",
                signal_type_keys=["kfreq"],
            )
            await pattern_store.transition_lifecycle(
                "inst_a", p.pattern_id, LIFECYCLE_RESOLVED,
            )

            # Drive a recurrence through the bridge's
            # record_recurrence which emits friction.pattern_reactivated
            # via its emit_event callback. Wire the callback to
            # event_stream.emit so the emitter's post-flush hook sees it.
            async def _emit_to_stream(event_type, payload):
                await event_stream.emit("inst_a", event_type, payload)

            triggered = await pattern_store.record_recurrence(
                instance_id="inst_a",
                pattern_id=p.pattern_id,
                observed_at=_now(),
                report_path="freq-test.md",
                classified_by=CLASSIFIED_AUTO_SIGNAL_TYPE,
                emit_event=_emit_to_stream,
            )
            assert triggered is True
            # Flush so the post-flush hook fires; the translated emit
            # then queues for the NEXT flush, so we flush twice.
            await event_stream.flush_now()
            await event_stream.flush_now()

            # Substrate state pin: friction.pattern_frequency_threshold_exceeded
            # landed in the event stream with active_epoch=2.
            all_events = await _fetch_all_events("inst_a")
            translated = [
                e for e in all_events
                if e.event_type == "friction.pattern_frequency_threshold_exceeded"
            ]
            assert len(translated) == 1, (
                f"expected one translated event; got events: "
                f"{[(e.event_type, e.payload) for e in all_events]}"
            )
            evt = translated[0]
            assert evt.payload["pattern_id"] == p.pattern_id
            assert evt.payload["active_epoch"] == 2  # reactivation episode
            assert evt.payload["lifecycle_state"] == LIFECYCLE_REACTIVATED
            assert evt.instance_id == "inst_a"
            # Behavioral signal: emit_count incremented.
            assert emitter._emit_count == 1
        finally:
            await emitter.stop()

    async def test_ignores_other_event_types(
        self, event_stream_writer, pattern_store,
    ):
        """Unit pin: events that aren't friction.pattern_reactivated
        flow through untouched."""
        emitter = FrictionPatternFrequencyEmitter(
            instance_id="inst_a", pattern_store=pattern_store,
        )
        await emitter.start()
        try:
            await event_stream.emit(
                "inst_a", "unrelated.event", {"data": "x"},
            )
            await event_stream.flush_now()
            await event_stream.flush_now()
            all_events = await _fetch_all_events("inst_a")
            translated = [
                e for e in all_events
                if e.event_type == "friction.pattern_frequency_threshold_exceeded"
            ]
            assert translated == []
            assert emitter._emit_count == 0
        finally:
            await emitter.stop()

    async def test_ignores_other_instance_events(
        self, event_stream_writer, pattern_store,
    ):
        """Multi-instance isolation: emitter scoped to inst_a ignores
        events for inst_b."""
        emitter = FrictionPatternFrequencyEmitter(
            instance_id="inst_a", pattern_store=pattern_store,
        )
        await emitter.start()
        try:
            await event_stream.emit(
                "inst_b", "friction.pattern_reactivated",
                {"pattern_id": "p1", "reactivated_at": _now()},
            )
            await event_stream.flush_now()
            await event_stream.flush_now()
            all_events = await _fetch_all_events("inst_a")
            translated = [
                e for e in all_events
                if e.event_type == "friction.pattern_frequency_threshold_exceeded"
            ]
            assert translated == []
            assert emitter._emit_count == 0
        finally:
            await emitter.stop()

    async def test_missing_pattern_id_skipped(
        self, event_stream_writer, pattern_store,
    ):
        """Defensive: an event with no pattern_id is silently skipped
        (don't crash the post-flush hook)."""
        emitter = FrictionPatternFrequencyEmitter(
            instance_id="inst_a", pattern_store=pattern_store,
        )
        await emitter.start()
        try:
            await event_stream.emit(
                "inst_a", "friction.pattern_reactivated", {},
            )
            await event_stream.flush_now()
            await event_stream.flush_now()
            assert emitter._emit_count == 0
        finally:
            await emitter.stop()


# ===========================================================================
# CodingSessionBridgeResponseEmitter
# ===========================================================================


class TestCodingSessionBridgeResponseEmitter:
    """Polls responses dir; triggers emit-once on each new response."""

    async def test_start_stop_lifecycle(self, event_stream_writer):
        """Unit pin: start spawns polling task; stop signals + cleans."""
        emitter = CodingSessionBridgeResponseEmitter(
            instance_id="inst_a",
            data_dir=str(event_stream_writer),
            poll_interval_s=0.1,
        )
        await emitter.start()
        assert emitter._task is not None
        assert not emitter._task.done()
        await emitter.stop()
        assert emitter._task is None

    async def test_idempotent_start(self, event_stream_writer):
        """Re-calling start() doesn't spawn a second task."""
        emitter = CodingSessionBridgeResponseEmitter(
            instance_id="inst_a",
            data_dir=str(event_stream_writer),
            poll_interval_s=0.1,
        )
        await emitter.start()
        first_task = emitter._task
        await emitter.start()
        assert emitter._task is first_task
        await emitter.stop()

    async def test_no_responses_directory_safe(self, event_stream_writer):
        """Polling a non-existent responses dir is a no-op (operator
        hasn't sent any requests yet)."""
        emitter = CodingSessionBridgeResponseEmitter(
            instance_id="inst_a",
            data_dir=str(event_stream_writer),
            poll_interval_s=0.05,
        )
        await emitter.start()
        try:
            await _wait_until(lambda: emitter._poll_count >= 1, timeout_s=1.0)
            assert emitter._emit_count == 0
        finally:
            await emitter.stop()

    async def test_functional_response_polling_emits_event(
        self, event_stream_writer,
    ):
        """FUNCTIONAL pin (architect's user-feedback request): full
        polling-emit flow.

        Setup:
          1. Write a request file under requests/ (bridge convention).
          2. Write a response file under responses/.
          3. Start the emitter.
          4. Wait for the polling pass to trigger emit-once.
        Substrate-state pin:
          * coding_consult.response_received event lands in the stream.
          * Sentinel file <request_id>.emitted exists in the responses dir.
        """
        tmp_path = event_stream_writer
        instance_id = "inst_a"
        bridge_root = (
            Path(tmp_path) / instance_id / "coding_session_bridge"
        )
        requests_dir = bridge_root / "requests"
        responses_dir = bridge_root / "responses"
        requests_dir.mkdir(parents=True, exist_ok=True)
        responses_dir.mkdir(parents=True, exist_ok=True)

        request_id = "req_func_test"
        # Bridge convention: request file contains JSON metadata.
        request_data = {
            "request_id": request_id,
            "originating_kernos_instance": instance_id,
            "originating_member_id": "mem_test",
            "target": "cc-claude",
            "ask": "fix the bug",
        }
        with (requests_dir / f"{request_id}.json").open("w") as fp:
            json.dump(request_data, fp)
        # Response file content. Bridge reads investigation_outcome.
        response_data = {
            "request_id": request_id,
            "target": "cc-claude",
            "investigation_outcome": "fixed via commit abc123",
        }
        with (responses_dir / f"{request_id}.json").open("w") as fp:
            # ``investigation_outcome`` must be one of the bridge's
            # VALID_INVESTIGATION_OUTCOMES; unknown values get
            # normalized to ``unable_to_investigate`` which would mask
            # our event-payload assertion below.
            response_data["investigation_outcome"] = "completed"
            json.dump(response_data, fp)

        emitter = CodingSessionBridgeResponseEmitter(
            instance_id=instance_id,
            data_dir=str(tmp_path),
            poll_interval_s=0.05,
        )
        await emitter.start()
        try:
            # Wait for the polling pass to trigger emit-once.
            await _wait_until(
                lambda: emitter._emit_count >= 1, timeout_s=2.0,
            )
            await event_stream.flush_now()
            await event_stream.flush_now()
        finally:
            await emitter.stop()

        # Substrate state pin: response_received event landed.
        all_events = await _fetch_all_events("inst_a")
        response_events = [
            e for e in all_events
            if e.event_type == "coding_consult.response_received"
        ]
        assert len(response_events) == 1
        evt = response_events[0]
        assert evt.payload["request_id"] == request_id
        assert evt.payload["target"] == "cc-claude"
        assert evt.payload["investigation_outcome"] == "completed"
        # Substrate state pin: O_EXCL sentinel exists (dedup armed).
        sentinel = responses_dir / f"{request_id}.emitted"
        assert sentinel.exists()

    async def test_emit_once_across_multiple_polling_passes(
        self, event_stream_writer,
    ):
        """Sentinel-dedup pin: even if the polling pass runs multiple
        times against the same response, the bridge's O_EXCL sentinel
        ensures the event fires exactly once. This is the load-bearing
        guarantee for the workflow's gate semantics — the workflow
        unpauses on the first response and would re-pause-or-error if
        the event refired."""
        tmp_path = event_stream_writer
        instance_id = "inst_a"
        bridge_root = Path(tmp_path) / instance_id / "coding_session_bridge"
        requests_dir = bridge_root / "requests"
        responses_dir = bridge_root / "responses"
        requests_dir.mkdir(parents=True, exist_ok=True)
        responses_dir.mkdir(parents=True, exist_ok=True)

        request_id = "req_dedup_test"
        with (requests_dir / f"{request_id}.json").open("w") as fp:
            json.dump({
                "request_id": request_id,
                "originating_kernos_instance": instance_id,
                "originating_member_id": "mem_test",
                "target": "cc",
            }, fp)
        with (responses_dir / f"{request_id}.json").open("w") as fp:
            json.dump({
                "request_id": request_id,
                "target": "cc",
                "investigation_outcome": "completed",
            }, fp)

        emitter = CodingSessionBridgeResponseEmitter(
            instance_id=instance_id,
            data_dir=str(tmp_path),
            poll_interval_s=0.05,
        )
        await emitter.start()
        try:
            # Wait for multiple polling passes (should see at least 3
            # within 1.0s with poll_interval_s=0.05).
            await _wait_until(
                lambda: emitter._poll_count >= 3, timeout_s=1.5,
            )
            await event_stream.flush_now()
            await event_stream.flush_now()
        finally:
            await emitter.stop()

        # Even though the emitter polled multiple times, the bridge's
        # O_EXCL sentinel ensures the event fired EXACTLY ONCE.
        all_events = await _fetch_all_events("inst_a")
        response_events = [
            e for e in all_events
            if e.event_type == "coding_consult.response_received"
        ]
        assert len(response_events) == 1
