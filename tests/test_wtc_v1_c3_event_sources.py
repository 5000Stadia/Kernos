"""WTC v1 C3 — first-class event sources.

Pins:

* :class:`InternalEventAdapter` bridges event_stream's post-flush
  hook to ``runtime.on_event_observed``: every flushed event
  becomes a candidate for predicate matching.
* :class:`CalendarSource` emits ``calendar.event_observed`` events
  with the documented payload shape.
* :class:`SchedulerHeartbeatSource` emits ``scheduler.tick_due``
  events.
* End-to-end: emit through a source → flush → adapter → runtime
  fires (claims + dispatches via WLP).
* Failure isolation: a per-event handler raise doesn't poison the
  rest of the flush batch.
* start/stop are idempotent.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kernos.kernel import event_stream
from kernos.kernel.triggers import (
    CalendarSource,
    DispatchPolicy,
    EVENT_TYPE_CALENDAR_OBSERVED,
    EVENT_TYPE_SCHEDULER_TICK_DUE,
    InternalEventAdapter,
    SchedulerHeartbeatSource,
    TemporalRelation,
    TriggerEvaluationRuntime,
    TriggerPredicate,
)


# ---------------------------------------------------------------------------
# Stub WLP — mirrors the pattern from test_wtc_v1_c2_evaluator.py.
# ---------------------------------------------------------------------------


class _StubWLP:
    def __init__(self) -> None:
        self.executions: dict[str, str] = {}
        self.dispatch_calls: list[dict] = []

    async def execute_workflow(
        self,
        *,
        fire_id: str,
        workflow_id: str,
        instance_id: str,
        trigger_event_payload: Any = None,
        member_id: str = "",
        **kwargs: Any,
    ) -> str:
        self.dispatch_calls.append({
            "fire_id": fire_id,
            "workflow_id": workflow_id,
            "instance_id": instance_id,
        })
        if fire_id in self.executions:
            return self.executions[fire_id]
        execution_id = f"exec_{uuid.uuid4().hex[:8]}"
        self.executions[fire_id] = execution_id
        return execution_id

    async def find_execution_by_fire_id(self, fire_id: str) -> str | None:
        return self.executions.get(fire_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def event_stream_started(tmp_path):
    """Start the durable event_stream writer for the test, then
    drain + close on teardown. Mirrors the harness used by
    test_wtc_v1_wlp_fire_id.py."""
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path), flush_interval_s=0.05)
    yield
    await event_stream.stop_writer()
    await event_stream._reset_for_tests()


@pytest.fixture
async def wlp() -> _StubWLP:
    return _StubWLP()


@pytest.fixture
async def runtime(tmp_path, wlp, event_stream_started):
    rt = TriggerEvaluationRuntime()
    await rt.start(
        data_dir=str(tmp_path),
        heartbeat_seconds=1,
        wlp_dispatch=wlp.execute_workflow,
        wlp_lookup_by_fire_id=wlp.find_execution_by_fire_id,
    )
    yield rt
    await rt.stop()


@pytest.fixture
async def adapter(runtime):
    a = InternalEventAdapter(runtime)
    await a.start()
    yield a
    await a.stop()


# ---------------------------------------------------------------------------
# InternalEventAdapter — post-flush bridge
# ---------------------------------------------------------------------------


async def test_adapter_start_is_idempotent(runtime):
    a = InternalEventAdapter(runtime)
    await a.start()
    await a.start()  # second call is a no-op
    assert a._hook_attached is True
    await a.stop()
    await a.stop()  # second stop is a no-op
    assert a._hook_attached is False


async def test_adapter_routes_flushed_event_to_runtime(
    runtime, wlp, adapter,
):
    """End-to-end: register an on(Y) predicate, emit a matching
    event, flush — adapter feeds it to runtime → claim + dispatch."""
    await runtime.register(
        trigger_id="t1",
        instance_id="inst1",
        workflow_id="wf-on-greeting",
        predicate=TriggerPredicate(
            event_selector={
                "op": "eq", "path": "event_type", "value": "user.message",
            },
            temporal_relation=TemporalRelation(kind="on"),
            dispatch_policy=DispatchPolicy(),
        ),
    )

    await event_stream.emit(
        instance_id="inst1",
        event_type="user.message",
        payload={"text": "hi"},
    )
    await event_stream.flush_now()
    # Hook fires synchronously inside flush_now via the awaitable
    # branch; one dispatch call expected.
    assert len(wlp.dispatch_calls) == 1
    assert wlp.dispatch_calls[0]["workflow_id"] == "wf-on-greeting"


async def test_adapter_no_match_no_dispatch(runtime, wlp, adapter):
    """Predicate selector that doesn't match — adapter still routes
    the event, but runtime returns 0 fires and WLP is not called."""
    await runtime.register(
        trigger_id="t1",
        instance_id="inst1",
        workflow_id="wf",
        predicate=TriggerPredicate(
            event_selector={
                "op": "eq", "path": "event_type", "value": "user.message",
            },
            temporal_relation=TemporalRelation(kind="on"),
            dispatch_policy=DispatchPolicy(),
        ),
    )
    await event_stream.emit(
        instance_id="inst1",
        event_type="page.edit",  # different type — no match
        payload={},
    )
    await event_stream.flush_now()
    assert wlp.dispatch_calls == []


async def test_adapter_isolates_per_event_failure(
    runtime, wlp, adapter, caplog,
):
    """If on_event_observed raises for one event, the rest of the
    batch still flows. We force a raise by making one of the
    runtime's predicate selectors malformed; the failure-isolation
    path catches it and the second event still dispatches."""
    # First predicate: malformed selector — predicates.evaluate
    # logs and returns False (covered by event_matches_selector's
    # try/except). To exercise the InternalEventAdapter's own
    # try/except we monkey-patch on_event_observed on a per-call
    # basis.
    real_on_event_observed = runtime.on_event_observed
    call_count = {"n": 0}

    async def flaky(event):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom on first event")
        return await real_on_event_observed(event)

    runtime.on_event_observed = flaky  # type: ignore[method-assign]

    await runtime.register(
        trigger_id="t1",
        instance_id="inst1",
        workflow_id="wf",
        predicate=TriggerPredicate(
            event_selector={
                "op": "eq", "path": "event_type", "value": "user.message",
            },
            temporal_relation=TemporalRelation(kind="on"),
            dispatch_policy=DispatchPolicy(),
        ),
    )

    # Emit two events in the same batch.
    await event_stream.emit(
        instance_id="inst1",
        event_type="user.message",
        payload={"n": 1},
    )
    await event_stream.emit(
        instance_id="inst1",
        event_type="user.message",
        payload={"n": 2},
    )
    await event_stream.flush_now()

    # First raised — second dispatched.
    assert call_count["n"] == 2
    assert len(wlp.dispatch_calls) == 1


async def test_adapter_after_stop_no_routing(runtime, wlp):
    """Once the adapter is stopped, flushed events stop reaching
    the runtime."""
    a = InternalEventAdapter(runtime)
    await a.start()

    await runtime.register(
        trigger_id="t1",
        instance_id="inst1",
        workflow_id="wf",
        predicate=TriggerPredicate(
            event_selector={
                "op": "eq", "path": "event_type", "value": "user.message",
            },
            temporal_relation=TemporalRelation(kind="on"),
            dispatch_policy=DispatchPolicy(),
        ),
    )

    await a.stop()

    await event_stream.emit(
        instance_id="inst1",
        event_type="user.message",
        payload={},
    )
    await event_stream.flush_now()
    assert wlp.dispatch_calls == []


# ---------------------------------------------------------------------------
# CalendarSource — emits calendar.event_observed
# ---------------------------------------------------------------------------


async def test_calendar_source_emits_observed_event(
    runtime, wlp, adapter,
):
    """Calendar source emits an event that flows through adapter
    to a predicate selecting calendar.event_observed."""
    await runtime.register(
        trigger_id="cal-t",
        instance_id="inst1",
        workflow_id="wf-on-cal",
        predicate=TriggerPredicate(
            event_selector={
                "op": "eq",
                "path": "event_type",
                "value": EVENT_TYPE_CALENDAR_OBSERVED,
            },
            temporal_relation=TemporalRelation(kind="on"),
            dispatch_policy=DispatchPolicy(),
        ),
    )

    cal = CalendarSource(instance_id="inst1")
    await cal.start()
    event_id = await cal.emit_observed(
        calendar_event_id="cal_evt_001",
        summary="Standup",
        start_iso="2026-04-30T15:00:00+00:00",
        end_iso="2026-04-30T15:30:00+00:00",
        calendar_id="primary",
    )
    assert event_id

    await event_stream.flush_now()

    assert len(wlp.dispatch_calls) == 1
    assert wlp.dispatch_calls[0]["workflow_id"] == "wf-on-cal"
    await cal.stop()


async def test_calendar_source_payload_shape(event_stream_started):
    """Without runtime — verify the emitted event row has the
    documented payload shape."""
    cal = CalendarSource(instance_id="inst-cal")
    await cal.start()
    await cal.emit_observed(
        calendar_event_id="cal_evt_002",
        summary="Lunch",
        start_iso="2026-05-01T12:00:00+00:00",
        end_iso="2026-05-01T13:00:00+00:00",
        calendar_id="primary",
        extra={"location": "kitchen"},
    )
    await event_stream.flush_now()

    rows = await event_stream.events_in_window(
        "inst-cal",
        datetime.now(timezone.utc) - timedelta(minutes=5),
        datetime.now(timezone.utc) + timedelta(minutes=5),
        event_types=[EVENT_TYPE_CALENDAR_OBSERVED],
    )
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["calendar_event_id"] == "cal_evt_002"
    assert payload["summary"] == "Lunch"
    assert payload["start_iso"] == "2026-05-01T12:00:00+00:00"
    assert payload["end_iso"] == "2026-05-01T13:00:00+00:00"
    assert payload["calendar_id"] == "primary"
    assert payload["location"] == "kitchen"
    await cal.stop()


# ---------------------------------------------------------------------------
# SchedulerHeartbeatSource — emits scheduler.tick_due
# ---------------------------------------------------------------------------


async def test_scheduler_source_emits_tick_due(event_stream_started):
    sched = SchedulerHeartbeatSource(instance_id="inst-sched")
    await sched.start()
    await sched.emit_tick(
        tick_timestamp_iso="2026-04-30T16:00:00+00:00",
        reason="heartbeat",
    )
    await event_stream.flush_now()

    rows = await event_stream.events_in_window(
        "inst-sched",
        datetime.now(timezone.utc) - timedelta(minutes=5),
        datetime.now(timezone.utc) + timedelta(minutes=5),
        event_types=[EVENT_TYPE_SCHEDULER_TICK_DUE],
    )
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["tick_timestamp"] == "2026-04-30T16:00:00+00:00"
    assert payload["reason"] == "heartbeat"
    await sched.stop()


async def test_scheduler_source_extra_payload_passes_through(
    event_stream_started,
):
    sched = SchedulerHeartbeatSource(instance_id="inst-sched2")
    await sched.start()
    await sched.emit_tick(
        tick_timestamp_iso="2026-04-30T17:00:00+00:00",
        reason="manual",
        extra={"operator": "kabe"},
    )
    await event_stream.flush_now()
    rows = await event_stream.events_in_window(
        "inst-sched2",
        datetime.now(timezone.utc) - timedelta(minutes=5),
        datetime.now(timezone.utc) + timedelta(minutes=5),
        event_types=[EVENT_TYPE_SCHEDULER_TICK_DUE],
    )
    assert rows[0].payload["operator"] == "kabe"
    await sched.stop()


async def test_source_start_stop_idempotent(event_stream_started):
    cal = CalendarSource(instance_id="inst")
    await cal.start()
    await cal.start()  # idempotent
    await cal.stop()
    await cal.stop()  # idempotent

    sched = SchedulerHeartbeatSource(instance_id="inst")
    await sched.start()
    await sched.start()
    await sched.stop()
    await sched.stop()
