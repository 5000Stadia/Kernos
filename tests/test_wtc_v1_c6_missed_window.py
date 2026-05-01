"""WTC v1 C6 — missed-window semantics + catch_up.

Pins:

* 0 fires in window → 0 dispatches.
* 1 fire in window (on-time) → 1 dispatch regardless of
  missed_window policy.
* 2+ fires in window (downtime detected):
  - missed_window="skip" (default) → 0 dispatches; one
    workflow.missed_fire emitted per missed fire.
  - missed_window="catch_up" → 1 dispatch (the latest);
    workflow.missed_fire emitted for each EXCEPT the latest.
* No fan-out for long downtime: a 24-hour outage does NOT
  produce 1440 dispatches; it produces 0 (skip) or 1 (catch_up).
* The single catch-up fire carries catch_up=True on the outbox row.
* missed_fire emit failures do not prevent dispatch.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kernos.kernel import event_stream
from kernos.kernel.triggers import (
    DispatchPolicy,
    EVENT_TYPE_WORKFLOW_MISSED_FIRE,
    TemporalRelation,
    TriggerEvaluationRuntime,
    TriggerPredicate,
)


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
            "fire_id": fire_id,
            "workflow_id": workflow_id,
            "payload": trigger_event_payload,
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


def _every_minute(missed_window: str = "skip") -> TriggerPredicate:
    return TriggerPredicate(
        event_selector={"op": "exists", "path": "event_id"},
        temporal_relation=TemporalRelation(
            kind="every", cron_expression="* * * * *",
        ),
        dispatch_policy=DispatchPolicy(missed_window=missed_window),
    )


def _every_5min(missed_window: str = "skip") -> TriggerPredicate:
    return TriggerPredicate(
        event_selector={"op": "exists", "path": "event_id"},
        temporal_relation=TemporalRelation(
            kind="every", cron_expression="*/5 * * * *",
        ),
        dispatch_policy=DispatchPolicy(missed_window=missed_window),
    )


def _last_evaluated_for_one_fire(cron_minutes: int) -> datetime:
    """Anchor the rollback to a known cron boundary so the window
    contains exactly one fire — independent of where wall-clock
    seconds land in the cron interval."""
    now = datetime.now(timezone.utc)
    minute_floor = (now.minute // cron_minutes) * cron_minutes
    boundary = now.replace(minute=minute_floor, second=0, microsecond=0)
    if boundary >= now:
        boundary = boundary - timedelta(minutes=cron_minutes)
    return boundary - timedelta(seconds=1)


async def _missed_fire_events_for(instance_id: str) -> list:
    await event_stream.flush_now()
    return await event_stream.events_in_window(
        instance_id,
        datetime.now(timezone.utc) - timedelta(hours=2),
        datetime.now(timezone.utc) + timedelta(minutes=5),
        event_types=[EVENT_TYPE_WORKFLOW_MISSED_FIRE],
    )


# ---------------------------------------------------------------------------
# 0/1 fires — no missed-window logic applies
# ---------------------------------------------------------------------------


async def test_no_fires_means_no_dispatch_no_missed_event(runtime):
    """An idle tick with no fires in the window dispatches nothing
    and emits no missed_fire events."""
    await runtime.register(
        trigger_id="t", instance_id="inst1", workflow_id="wf",
        predicate=_every_minute(),
    )
    # last_evaluated == now (no window).
    fired = await runtime.evaluate_now()
    assert fired == 0
    assert runtime._test_wlp.dispatch_calls == []  # type: ignore[attr-defined]
    events = await _missed_fire_events_for("inst1")
    assert events == []


async def test_one_fire_dispatches_normally_skip(runtime):
    """A single fire in the window dispatches regardless of
    missed_window policy — the on-time case."""
    await runtime.register(
        trigger_id="t", instance_id="inst1", workflow_id="wf",
        predicate=_every_5min(missed_window="skip"),
    )
    rec = runtime._predicates["t"]
    rec.last_evaluated = _last_evaluated_for_one_fire(5)
    fired = await runtime.evaluate_now()
    assert fired == 1
    events = await _missed_fire_events_for("inst1")
    assert events == []


async def test_one_fire_dispatches_normally_catch_up(runtime):
    await runtime.register(
        trigger_id="t", instance_id="inst1", workflow_id="wf",
        predicate=_every_5min(missed_window="catch_up"),
    )
    rec = runtime._predicates["t"]
    rec.last_evaluated = _last_evaluated_for_one_fire(5)
    fired = await runtime.evaluate_now()
    assert fired == 1
    events = await _missed_fire_events_for("inst1")
    assert events == []


# ---------------------------------------------------------------------------
# 2+ fires (downtime) — missed_window honored
# ---------------------------------------------------------------------------


async def test_skip_dispatches_none_emits_event_per_missed(runtime):
    """5-minute downtime + skip policy: 0 dispatches, 5 missed_fire
    events."""
    await runtime.register(
        trigger_id="t", instance_id="inst1", workflow_id="wf",
        predicate=_every_minute(missed_window="skip"),
    )
    rec = runtime._predicates["t"]
    rec.last_evaluated = datetime.now(timezone.utc) - timedelta(minutes=5)
    fired = await runtime.evaluate_now()
    assert fired == 0
    assert runtime._test_wlp.dispatch_calls == []  # type: ignore[attr-defined]
    events = await _missed_fire_events_for("inst1")
    assert len(events) >= 4  # At least 4 missed minute boundaries.
    for ev in events:
        assert ev.payload["reason"] == "skip"
        assert ev.payload["trigger_id"] == "t"
        assert ev.payload["workflow_id"] == "wf"
        assert ev.payload["cron_expression"] == "* * * * *"


async def test_catch_up_dispatches_one_emits_event_for_rest(runtime):
    """5-minute downtime + catch_up policy: 1 dispatch (the latest);
    missed_fire events for the older windows."""
    await runtime.register(
        trigger_id="t", instance_id="inst1", workflow_id="wf",
        predicate=_every_minute(missed_window="catch_up"),
    )
    rec = runtime._predicates["t"]
    rec.last_evaluated = datetime.now(timezone.utc) - timedelta(minutes=5)
    fired = await runtime.evaluate_now()
    assert fired == 1
    events = await _missed_fire_events_for("inst1")
    # One fewer event than total windows; total is at least 4
    # (5-minute window minus the latest collapsed into the catch-up).
    assert len(events) >= 3
    for ev in events:
        assert ev.payload["reason"] == "catch_up_collapsed"
    # The single dispatch carries catch_up=True.
    payload = runtime._test_wlp.dispatch_calls[0]["payload"]  # type: ignore[attr-defined]
    assert payload.get("catch_up") is True


# ---------------------------------------------------------------------------
# Long downtime — no fan-out
# ---------------------------------------------------------------------------


async def test_long_downtime_skip_no_fanout(runtime):
    """24-hour downtime with skip policy MUST NOT produce 1440
    dispatches. The bound is 0 dispatches (skip)."""
    await runtime.register(
        trigger_id="t", instance_id="inst1", workflow_id="wf",
        predicate=_every_minute(missed_window="skip"),
    )
    rec = runtime._predicates["t"]
    rec.last_evaluated = datetime.now(timezone.utc) - timedelta(hours=24)
    fired = await runtime.evaluate_now()
    assert fired == 0
    assert runtime._test_wlp.dispatch_calls == []  # type: ignore[attr-defined]


async def test_long_downtime_catch_up_no_fanout(runtime):
    """24-hour downtime with catch_up policy MUST produce exactly
    one catch-up dispatch — not 1440."""
    await runtime.register(
        trigger_id="t", instance_id="inst1", workflow_id="wf",
        predicate=_every_minute(missed_window="catch_up"),
    )
    rec = runtime._predicates["t"]
    rec.last_evaluated = datetime.now(timezone.utc) - timedelta(hours=24)
    fired = await runtime.evaluate_now()
    assert fired == 1
    assert len(runtime._test_wlp.dispatch_calls) == 1  # type: ignore[attr-defined]
    payload = runtime._test_wlp.dispatch_calls[0]["payload"]  # type: ignore[attr-defined]
    assert payload["catch_up"] is True


# ---------------------------------------------------------------------------
# Per-predicate isolation — multiple predicates with different policies
# coexist without interference.
# ---------------------------------------------------------------------------


async def test_skip_and_catch_up_predicates_coexist(runtime):
    await runtime.register(
        trigger_id="t_skip", instance_id="inst1",
        workflow_id="wf_skip",
        predicate=_every_minute(missed_window="skip"),
    )
    await runtime.register(
        trigger_id="t_catch", instance_id="inst1",
        workflow_id="wf_catch",
        predicate=_every_minute(missed_window="catch_up"),
    )
    for tid in ("t_skip", "t_catch"):
        runtime._predicates[tid].last_evaluated = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        )
    fired = await runtime.evaluate_now()
    # 0 from skip + 1 from catch_up.
    assert fired == 1
    assert len(runtime._test_wlp.dispatch_calls) == 1  # type: ignore[attr-defined]
    assert runtime._test_wlp.dispatch_calls[0]["workflow_id"] == "wf_catch"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Default policy is skip
# ---------------------------------------------------------------------------


async def test_default_policy_is_skip(runtime):
    """When no explicit missed_window is set, the default
    DispatchPolicy is skip — no fan-out on downtime."""
    pred_default = TriggerPredicate(
        event_selector={"op": "exists", "path": "event_id"},
        temporal_relation=TemporalRelation(
            kind="every", cron_expression="* * * * *",
        ),
        # dispatch_policy left at default
    )
    await runtime.register(
        trigger_id="t", instance_id="inst1", workflow_id="wf",
        predicate=pred_default,
    )
    rec = runtime._predicates["t"]
    rec.last_evaluated = datetime.now(timezone.utc) - timedelta(minutes=10)
    fired = await runtime.evaluate_now()
    assert fired == 0


# ---------------------------------------------------------------------------
# Idempotency — re-running evaluate_now with the same downtime
# does not double-dispatch under catch_up (outbox PK gate).
# ---------------------------------------------------------------------------


async def test_catch_up_idempotent_across_reruns(runtime):
    await runtime.register(
        trigger_id="t", instance_id="inst1", workflow_id="wf",
        predicate=_every_minute(missed_window="catch_up"),
    )
    rec = runtime._predicates["t"]
    # First pass: 5 missed → 1 catch-up dispatch.
    rec.last_evaluated = datetime.now(timezone.utc) - timedelta(minutes=5)
    fired_first = await runtime.evaluate_now()
    assert fired_first == 1
    # Second pass: rewind last_evaluated so the same windows are
    # in the walk. The deterministic fire_window_key dedup at the
    # outbox PK catches it — no second dispatch.
    rec.last_evaluated = datetime.now(timezone.utc) - timedelta(minutes=5)
    fired_second = await runtime.evaluate_now()
    assert fired_second == 0
