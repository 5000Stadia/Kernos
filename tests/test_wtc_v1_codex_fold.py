"""WTC v1 Codex mid-batch fold — fail-closed fire_id index +
predicate prefilter + source authority.

Pins:

* execute_workflow's partial unique index ``idx_executions_fire_id``
  is verified after schema setup; missing index aborts startup.
* The runtime indexes predicates by simple eq event_type; events
  whose type doesn't appear in any predicate selector skip the
  predicate walk entirely.
* CalendarSource and SchedulerHeartbeatSource emit with
  ``envelope.source_module`` of ``"calendar"`` / ``"scheduler"``
  (substrate-set, not from payload).
* Re-registration with a different selector cleanly moves the
  trigger between event_type buckets (no leak).
* Deactivation drops the trigger from the prefilter index.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kernos.kernel import event_stream
from kernos.kernel.event_stream import emitter_registry
from kernos.kernel.triggers import (
    CALENDAR_SOURCE_MODULE,
    CalendarSource,
    DispatchPolicy,
    EVENT_TYPE_CALENDAR_OBSERVED,
    EVENT_TYPE_SCHEDULER_TICK_DUE,
    InternalEventAdapter,
    SCHEDULER_SOURCE_MODULE,
    SchedulerHeartbeatSource,
    TemporalRelation,
    TriggerEvaluationRuntime,
    TriggerPredicate,
)


# ---------------------------------------------------------------------------
# Codex fold #1: fail-closed fire_id partial unique index
# ---------------------------------------------------------------------------


async def test_fire_id_partial_unique_index_present_after_schema():
    """Engine startup must produce idx_executions_fire_id in the
    workflow_executions index list. Without it, the WTC v1
    fire_id idempotency invariant degrades silently."""
    import tempfile
    import aiosqlite
    from kernos.kernel.workflows.execution_engine import _ensure_schema

    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        async with aiosqlite.connect(tmp.name) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            async with db.execute(
                "SELECT name FROM pragma_index_list('workflow_executions')"
            ) as cur:
                indexes = {row[0] for row in await cur.fetchall()}
    assert "idx_executions_fire_id" in indexes


async def test_ensure_schema_recreates_dropped_fire_id_index():
    """Defensive verification covers the case where the partial
    unique index was dropped externally between engine starts.
    Re-running _ensure_schema must recreate it."""
    import tempfile
    import aiosqlite
    from kernos.kernel.workflows.execution_engine import _ensure_schema

    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        async with aiosqlite.connect(tmp.name) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            # Drop the index to simulate external schema drift.
            await db.execute("DROP INDEX idx_executions_fire_id")
            await db.commit()
            # Re-run schema setup — must restore the index, not
            # silently allow startup with the index missing.
            await _ensure_schema(db)
            async with db.execute(
                "SELECT name FROM pragma_index_list('workflow_executions')"
            ) as cur:
                indexes = {row[0] for row in await cur.fetchall()}
    assert "idx_executions_fire_id" in indexes


# ---------------------------------------------------------------------------
# Codex fold #5: event_type prefilter index
# ---------------------------------------------------------------------------


class _StubWLP:
    def __init__(self) -> None:
        self.executions: dict[str, str] = {}
        self.dispatch_calls: list[dict] = []

    async def execute_workflow(
        self, *, fire_id: str, workflow_id: str, instance_id: str,
        trigger_event_payload: Any = None, member_id: str = "",
        **kwargs: Any,
    ) -> str:
        self.dispatch_calls.append({
            "fire_id": fire_id, "workflow_id": workflow_id,
        })
        if fire_id in self.executions:
            return self.executions[fire_id]
        eid = f"exec_{uuid.uuid4().hex[:8]}"
        self.executions[fire_id] = eid
        return eid

    async def find_execution_by_fire_id(self, fire_id: str) -> str | None:
        return self.executions.get(fire_id)


@pytest.fixture
async def event_stream_started(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path), flush_interval_s=0.05)
    yield
    await event_stream.stop_writer()
    await event_stream._reset_for_tests()


@pytest.fixture
async def runtime(tmp_path, event_stream_started):
    wlp = _StubWLP()
    rt = TriggerEvaluationRuntime()
    await rt.start(
        data_dir=str(tmp_path),
        heartbeat_seconds=1,
        wlp_dispatch=wlp.execute_workflow,
        wlp_lookup_by_fire_id=wlp.find_execution_by_fire_id,
    )
    rt._test_wlp = wlp  # type: ignore[attr-defined]
    yield rt
    await rt.stop()


def _on_eq_event_type(value: str) -> TriggerPredicate:
    return TriggerPredicate(
        event_selector={"op": "eq", "path": "event_type", "value": value},
        temporal_relation=TemporalRelation(kind="on"),
        dispatch_policy=DispatchPolicy(),
    )


async def test_simple_eq_predicate_lands_in_event_type_bucket(runtime):
    await runtime.register(
        trigger_id="t1", instance_id="inst1", workflow_id="wf",
        predicate=_on_eq_event_type("user.message"),
    )
    assert "user.message" in runtime._predicates_by_event_type
    assert "t1" in runtime._predicates_by_event_type["user.message"]
    assert "t1" not in runtime._predicates_unfiltered


async def test_complex_selector_lands_in_unfiltered_bucket(runtime):
    """Predicates with composite selectors can't be event_type-
    indexed; they fall back to the unfiltered bucket."""
    composite = TriggerPredicate(
        event_selector={
            "op": "AND", "operands": [
                {"op": "eq", "path": "event_type", "value": "user.message"},
                {"op": "eq", "path": "payload.from", "value": "kit"},
            ],
        },
        temporal_relation=TemporalRelation(kind="on"),
        dispatch_policy=DispatchPolicy(),
    )
    await runtime.register(
        trigger_id="t1", instance_id="inst1", workflow_id="wf",
        predicate=composite,
    )
    assert "t1" in runtime._predicates_unfiltered
    assert all(
        "t1" not in s for s in runtime._predicates_by_event_type.values()
    )


async def test_every_predicate_not_in_either_bucket(runtime):
    """``every(cron)`` predicates fire via the cron walk in
    evaluate_now(), not via on_event_observed — they must not
    appear in either prefilter bucket."""
    await runtime.register(
        trigger_id="cron-t",
        instance_id="inst1",
        workflow_id="wf",
        predicate=TriggerPredicate(
            event_selector={"op": "eq", "path": "event_type",
                            "value": "schedule.tick"},
            temporal_relation=TemporalRelation(
                kind="every", cron_expression="*/5 * * * *",
            ),
            dispatch_policy=DispatchPolicy(),
        ),
    )
    assert "cron-t" not in runtime._predicates_unfiltered
    assert all(
        "cron-t" not in s
        for s in runtime._predicates_by_event_type.values()
    )


async def test_reregister_with_different_event_type_moves_bucket(runtime):
    """Idempotent re-registration with a different selector must
    drop the old bucket entry and add the new one — no leak."""
    await runtime.register(
        trigger_id="t1", instance_id="inst1", workflow_id="wf",
        predicate=_on_eq_event_type("user.message"),
    )
    await runtime.register(
        trigger_id="t1", instance_id="inst1", workflow_id="wf",
        predicate=_on_eq_event_type("page.edit"),
    )
    # Old bucket no longer holds t1.
    assert "t1" not in runtime._predicates_by_event_type.get(
        "user.message", set(),
    )
    # New bucket does.
    assert "t1" in runtime._predicates_by_event_type["page.edit"]
    # Old bucket may have been removed entirely if it became empty.


async def test_deactivate_removes_from_prefilter_index(runtime):
    await runtime.register(
        trigger_id="t1", instance_id="inst1", workflow_id="wf",
        predicate=_on_eq_event_type("user.message"),
    )
    await runtime.deactivate("t1")
    assert "t1" not in runtime._predicates_by_event_type.get(
        "user.message", set(),
    )


async def test_event_type_with_no_matching_predicate_skips_walk(
    runtime,
):
    """An event whose type appears in no predicate selector and
    no unfiltered predicate matches — on_event_observed returns
    0 fires without invoking event_matches_selector. Verified by
    the fast path: candidate set is empty."""
    await runtime.register(
        trigger_id="t1", instance_id="inst1", workflow_id="wf",
        predicate=_on_eq_event_type("user.message"),
    )
    # An event whose type isn't user.message and has no unfiltered
    # listener.
    from kernos.kernel.event_stream import Event
    other_event = Event(
        event_id="evt_other",
        instance_id="inst1",
        timestamp=datetime.now(timezone.utc).isoformat(),
        event_type="some.other.type",
        payload={},
    )
    fired = await runtime.on_event_observed(other_event)
    assert fired == 0
    assert runtime._test_wlp.dispatch_calls == []  # type: ignore[attr-defined]


async def test_unfiltered_predicate_walks_on_every_event(runtime):
    """Unfiltered predicates (rich selectors) must still be evaluated
    against every flushed event — regression guard against the
    prefilter accidentally dropping them."""
    composite = TriggerPredicate(
        event_selector={
            "op": "AND", "operands": [
                {"op": "eq", "path": "event_type", "value": "user.message"},
                {"op": "eq", "path": "payload.priority", "value": "high"},
            ],
        },
        temporal_relation=TemporalRelation(kind="on"),
        dispatch_policy=DispatchPolicy(),
    )
    await runtime.register(
        trigger_id="t1", instance_id="inst1", workflow_id="wf",
        predicate=composite,
    )
    from kernos.kernel.event_stream import Event
    matching = Event(
        event_id="evt_m",
        instance_id="inst1",
        timestamp=datetime.now(timezone.utc).isoformat(),
        event_type="user.message",
        payload={"priority": "high"},
    )
    fired = await runtime.on_event_observed(matching)
    assert fired == 1


# ---------------------------------------------------------------------------
# Codex fold #7: source authority on calendar / scheduler
# ---------------------------------------------------------------------------


async def test_calendar_source_emits_with_registered_authority(
    event_stream_started,
):
    """CalendarSource events MUST carry envelope.source_module
    set to "calendar" (registry-bound), not the unregistered
    sentinel."""
    src = CalendarSource(instance_id="inst-cal-auth")
    await src.start()
    await src.emit_observed(
        calendar_event_id="cal_001",
        summary="standup",
        start_iso="2026-04-30T15:00:00+00:00",
    )
    await event_stream.flush_now()
    rows = await event_stream.events_in_window(
        "inst-cal-auth",
        datetime.now(timezone.utc) - timedelta(minutes=5),
        datetime.now(timezone.utc) + timedelta(minutes=5),
        event_types=[EVENT_TYPE_CALENDAR_OBSERVED],
    )
    assert len(rows) == 1
    assert rows[0].source_module == CALENDAR_SOURCE_MODULE
    await src.stop()


async def test_scheduler_source_emits_with_registered_authority(
    event_stream_started,
):
    src = SchedulerHeartbeatSource(instance_id="inst-sched-auth")
    await src.start()
    await src.emit_tick(tick_timestamp_iso="2026-04-30T16:00:00+00:00")
    await event_stream.flush_now()
    rows = await event_stream.events_in_window(
        "inst-sched-auth",
        datetime.now(timezone.utc) - timedelta(minutes=5),
        datetime.now(timezone.utc) + timedelta(minutes=5),
        event_types=[EVENT_TYPE_SCHEDULER_TICK_DUE],
    )
    assert len(rows) == 1
    assert rows[0].source_module == SCHEDULER_SOURCE_MODULE
    await src.stop()


async def test_calendar_emitter_is_idempotent_across_instances(
    event_stream_started,
):
    """Multiple CalendarSource instances share a single
    EmitterRegistry-bound emitter — second-instance instantiation
    must not raise EmitterAlreadyRegistered."""
    a = CalendarSource(instance_id="inst-a")
    b = CalendarSource(instance_id="inst-b")
    await a.start()
    await b.start()
    # Both can emit without conflict.
    await a.emit_observed(calendar_event_id="cal_a")
    await b.emit_observed(calendar_event_id="cal_b")
    await event_stream.flush_now()
    # Both events recorded with the same source_module.
    for instance in ("inst-a", "inst-b"):
        rows = await event_stream.events_in_window(
            instance,
            datetime.now(timezone.utc) - timedelta(minutes=5),
            datetime.now(timezone.utc) + timedelta(minutes=5),
            event_types=[EVENT_TYPE_CALENDAR_OBSERVED],
        )
        assert len(rows) == 1
        assert rows[0].source_module == CALENDAR_SOURCE_MODULE
    await a.stop()
    await b.stop()
