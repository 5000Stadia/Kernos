"""WTC v1 C2 — predicate evaluator + four temporal relations +
recovery sweep.

Pins:

* Per-temporal-relation evaluation correctness (every / on /
  before / after).
* Race-safe claim under concurrent matches (only one fire per
  window).
* All four AC6 crash-recovery scenarios end-to-end through the
  outbox + runtime, including the new scenario #2 (Kit must-fix
  seam — crash after WLP accept before mark_dispatched).
* Idempotency invariant: each scenario produces exactly one
  workflow execution.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kernos.kernel.event_stream import Event
from kernos.kernel.triggers import (
    DispatchPolicy,
    TemporalRelation,
    TriggerEvaluationRuntime,
    TriggerPredicate,
    derive_fire_id,
)


# ---------------------------------------------------------------------------
# Stub WLP — captures dispatch calls; idempotent on fire_id
# ---------------------------------------------------------------------------


class _StubWLP:
    """In-memory stand-in for WLP.execute_workflow + lookup. Mirrors
    the real WLP's idempotency contract: same fire_id returns the
    original execution_id."""

    def __init__(self) -> None:
        self.executions: dict[str, str] = {}  # fire_id → execution_id
        self.dispatch_calls: list[dict] = []
        # Test hooks for crash simulation.
        self.fail_next_dispatches: int = 0
        self.fail_next_lookups: int = 0

    async def execute_workflow(
        self,
        *,
        fire_id: str,
        workflow_id: str,
        instance_id: str,
        trigger_event_payload=None,
        member_id: str = "",
        **kwargs,
    ) -> str:
        self.dispatch_calls.append({
            "fire_id": fire_id,
            "workflow_id": workflow_id,
            "instance_id": instance_id,
        })
        if self.fail_next_dispatches > 0:
            self.fail_next_dispatches -= 1
            raise RuntimeError("simulated dispatch failure")
        if fire_id in self.executions:
            return self.executions[fire_id]
        execution_id = f"exec_{uuid.uuid4().hex[:8]}"
        self.executions[fire_id] = execution_id
        return execution_id

    async def find_execution_by_fire_id(self, fire_id: str) -> str | None:
        if self.fail_next_lookups > 0:
            self.fail_next_lookups -= 1
            raise RuntimeError("simulated WLP lookup failure")
        return self.executions.get(fire_id)


@pytest.fixture
async def wlp():
    return _StubWLP()


@pytest.fixture
async def runtime(tmp_path, wlp):
    rt = TriggerEvaluationRuntime()
    await rt.start(
        data_dir=str(tmp_path),
        heartbeat_seconds=1,
        wlp_dispatch=wlp.execute_workflow,
        wlp_lookup_by_fire_id=wlp.find_execution_by_fire_id,
    )
    yield rt
    await rt.stop()


def _make_event(
    *,
    event_type: str = "user.message",
    instance_id: str = "inst1",
    payload: dict | None = None,
    timestamp: str | None = None,
    event_id: str | None = None,
) -> Event:
    return Event(
        event_id=event_id or f"evt_{uuid.uuid4().hex[:8]}",
        instance_id=instance_id,
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
        event_type=event_type,
        payload=payload or {},
    )


# ---------------------------------------------------------------------------
# every(cron) — heartbeat walk
# ---------------------------------------------------------------------------


def _last_evaluated_for_one_fire(cron_minutes: int) -> datetime:
    """Compute a last_evaluated that makes (last_evaluated, now]
    contain EXACTLY one cron boundary for ``*/cron_minutes * * * *``.
    Anchors the rollback to the most recent boundary so it's
    independent of where wall-clock seconds land in the minute.
    """
    now = datetime.now(timezone.utc)
    # Most recent boundary at or before now.
    minute_floor = (now.minute // cron_minutes) * cron_minutes
    boundary = now.replace(minute=minute_floor, second=0, microsecond=0)
    # If now is exactly on a boundary, boundary == now → window
    # would be empty. That's vanishingly unlikely in tests but
    # safe to nudge by stepping back one cron interval.
    if boundary >= now:
        boundary = boundary - timedelta(minutes=cron_minutes)
    # Window (boundary - 1s, now] contains exactly one boundary
    # (boundary itself) as long as now < boundary + cron_minutes.
    return boundary - timedelta(seconds=1)


class TestEveryCron:
    async def test_every_on_time_fires_single_window(self, runtime, wlp):
        """The on-time case: a cron walk over a window containing
        exactly one fire dispatches normally regardless of
        missed_window policy (no downtime, nothing to skip)."""
        pred = TriggerPredicate(
            event_selector={"op": "exists", "path": "event_id"},
            temporal_relation=TemporalRelation(
                kind="every", cron_expression="*/5 * * * *",
            ),
        )
        await runtime.register(
            trigger_id="trig_cron",
            instance_id="inst1",
            workflow_id="wf_cron",
            predicate=pred,
        )
        record = runtime._predicates["trig_cron"]
        record.last_evaluated = _last_evaluated_for_one_fire(5)
        fired = await runtime.evaluate_now()
        assert fired == 1
        assert len(wlp.dispatch_calls) == 1

    async def test_every_idempotent_within_same_window(self, runtime, wlp):
        # Two calls to evaluate_now within the same cron interval
        # should produce no second fire (deterministic
        # fire_window_key dedup at the outbox PK).
        pred = TriggerPredicate(
            event_selector={"op": "exists", "path": "event_id"},
            temporal_relation=TemporalRelation(
                kind="every", cron_expression="*/5 * * * *",
            ),
        )
        await runtime.register(
            trigger_id="trig_dup",
            instance_id="inst1",
            workflow_id="wf_dup",
            predicate=pred,
        )
        record = runtime._predicates["trig_dup"]
        record.last_evaluated = _last_evaluated_for_one_fire(5)
        fired_first = await runtime.evaluate_now()
        record.last_evaluated = _last_evaluated_for_one_fire(5)
        fired_second = await runtime.evaluate_now()
        assert fired_first == 1
        assert fired_second == 0


# ---------------------------------------------------------------------------
# on(Y) — event-driven
# ---------------------------------------------------------------------------


class TestOnEvent:
    async def test_on_fires_on_match(self, runtime, wlp):
        pred = TriggerPredicate(
            event_selector={"op": "eq", "path": "event_type",
                             "value": "user.message"},
            temporal_relation=TemporalRelation(kind="on"),
        )
        await runtime.register(
            trigger_id="trig_on",
            instance_id="inst1",
            workflow_id="wf_on",
            predicate=pred,
        )
        evt = _make_event(event_type="user.message")
        fired = await runtime.on_event_observed(evt)
        assert fired == 1
        assert wlp.dispatch_calls[0]["workflow_id"] == "wf_on"

    async def test_on_does_not_fire_on_mismatch(self, runtime, wlp):
        pred = TriggerPredicate(
            event_selector={"op": "eq", "path": "event_type",
                             "value": "user.message"},
            temporal_relation=TemporalRelation(kind="on"),
        )
        await runtime.register(
            trigger_id="trig_on",
            instance_id="inst1",
            workflow_id="wf_on",
            predicate=pred,
        )
        evt = _make_event(event_type="something.else")
        fired = await runtime.on_event_observed(evt)
        assert fired == 0
        assert wlp.dispatch_calls == []

    async def test_on_idempotent_on_duplicate_event(self, runtime, wlp):
        pred = TriggerPredicate(
            event_selector={"op": "exists", "path": "event_id"},
            temporal_relation=TemporalRelation(kind="on"),
        )
        await runtime.register(
            trigger_id="trig_on",
            instance_id="inst1",
            workflow_id="wf_on",
            predicate=pred,
        )
        # Same event_id twice — outbox dedup catches the second.
        evt1 = _make_event(event_id="evt_dup_test")
        evt2 = _make_event(event_id="evt_dup_test")
        first = await runtime.on_event_observed(evt1)
        second = await runtime.on_event_observed(evt2)
        assert first == 1
        assert second == 0


# ---------------------------------------------------------------------------
# before / after — due-time math
# ---------------------------------------------------------------------------


class TestBeforeAfter:
    async def test_after_with_zero_minutes_fires_immediately(
        self, runtime, wlp,
    ):
        pred = TriggerPredicate(
            event_selector={"op": "exists", "path": "event_id"},
            temporal_relation=TemporalRelation(kind="after", minutes=1),
        )
        await runtime.register(
            trigger_id="trig_after",
            instance_id="inst1",
            workflow_id="wf_after",
            predicate=pred,
        )
        # Event timestamp is 5 minutes ago — due_at = ts + 1min is
        # already in the past, fires immediately.
        old_ts = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()
        evt = _make_event(timestamp=old_ts)
        fired = await runtime.on_event_observed(evt)
        assert fired == 1

    async def test_after_with_future_due_enqueues(self, runtime, wlp):
        # Y observed now; predicate is after(Y, 30) → due_at is 30
        # minutes in the future; should enqueue, not fire.
        pred = TriggerPredicate(
            event_selector={"op": "exists", "path": "event_id"},
            temporal_relation=TemporalRelation(kind="after", minutes=30),
        )
        await runtime.register(
            trigger_id="trig_after_fut",
            instance_id="inst1",
            workflow_id="wf_after_fut",
            predicate=pred,
        )
        evt = _make_event()
        fired = await runtime.on_event_observed(evt)
        assert fired == 0
        assert len(runtime._pending_due_fires) == 1

    async def test_pending_drained_when_due(self, runtime, wlp):
        pred = TriggerPredicate(
            event_selector={"op": "exists", "path": "event_id"},
            temporal_relation=TemporalRelation(kind="after", minutes=1),
        )
        await runtime.register(
            trigger_id="trig_drain",
            instance_id="inst1",
            workflow_id="wf_drain",
            predicate=pred,
        )
        evt = _make_event()
        await runtime.on_event_observed(evt)
        # Force the pending fire's due_at to the past.
        runtime._pending_due_fires[0].due_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        fired = await runtime.evaluate_now()
        assert fired == 1
        assert runtime._pending_due_fires == []

    async def test_before_uses_negative_offset(self, runtime, wlp):
        # before(Y, 30): due_at = Y.timestamp - 30min. If Y is in
        # the future by 60min, before(Y, 30) is in the future by
        # 30min — enqueue.
        pred = TriggerPredicate(
            event_selector={"op": "exists", "path": "event_id"},
            temporal_relation=TemporalRelation(kind="before", minutes=30),
        )
        await runtime.register(
            trigger_id="trig_before",
            instance_id="inst1",
            workflow_id="wf_before",
            predicate=pred,
        )
        future_ts = (
            datetime.now(timezone.utc) + timedelta(minutes=60)
        ).isoformat()
        evt = _make_event(timestamp=future_ts)
        await runtime.on_event_observed(evt)
        assert len(runtime._pending_due_fires) == 1
        assert runtime._pending_due_fires[0].due_at > datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Race-safe claim under concurrent matches
# ---------------------------------------------------------------------------


class TestConcurrentMatches:
    async def test_concurrent_on_event_one_fires(self, runtime, wlp):
        pred = TriggerPredicate(
            event_selector={"op": "exists", "path": "event_id"},
            temporal_relation=TemporalRelation(kind="on"),
        )
        await runtime.register(
            trigger_id="trig_race",
            instance_id="inst1",
            workflow_id="wf_race",
            predicate=pred,
        )
        # Same event_id observed twice concurrently. Only one
        # claim should win.
        evt = _make_event(event_id="evt_race")
        first, second = await asyncio.gather(
            runtime.on_event_observed(evt),
            runtime.on_event_observed(evt),
        )
        assert (first + second) == 1
        # Exactly one execution at WLP.
        assert len(wlp.executions) == 1


# ---------------------------------------------------------------------------
# AC6 — four crash-recovery scenarios
# ---------------------------------------------------------------------------


async def _setup_runtime_for_recovery(tmp_path):
    """Build a runtime + stub WLP for recovery tests. Recovery
    uses the per-process claim_owner; we simulate "another
    process" by stopping/restarting the runtime."""
    wlp = _StubWLP()
    rt = TriggerEvaluationRuntime()
    await rt.start(
        data_dir=str(tmp_path),
        heartbeat_seconds=1,
        wlp_dispatch=wlp.execute_workflow,
        wlp_lookup_by_fire_id=wlp.find_execution_by_fire_id,
    )
    pred = TriggerPredicate(
        event_selector={"op": "exists", "path": "event_id"},
        temporal_relation=TemporalRelation(kind="on"),
    )
    await rt.register(
        trigger_id="trig_recover",
        instance_id="inst1",
        workflow_id="wf_recover",
        predicate=pred,
    )
    return rt, wlp


class TestAC6CrashRecovery:
    """All four scenarios must produce exactly one workflow
    execution. Test pins per the post-fold spec."""

    async def test_scenario_1_crash_before_dispatch(self, tmp_path):
        """Runtime claims the fire (status='pending'); crash
        before WLP is invoked. Restart sweep finds pending past
        lease; queries WLP (no record); reclaims + re-dispatches.
        Exactly one execution.
        """
        rt, wlp = await _setup_runtime_for_recovery(tmp_path)
        # Manually claim a pending row to simulate crashed-before-dispatch.
        from kernos.kernel.triggers.predicate import (
            fire_window_key_for_on,
        )
        fwk = fire_window_key_for_on("evt_s1")
        claim = await rt._outbox.claim_fire(
            instance_id="inst1",
            trigger_id="trig_recover",
            fire_window_key=fwk,
            payload={"event_id": "evt_s1"},
            claim_owner="dead_runtime",
        )
        assert claim is not None
        assert wlp.dispatch_calls == []  # never dispatched

        # Force the row past its lease.
        await rt._outbox._db.execute(
            "UPDATE trigger_fires SET claimed_at = ? "
            "WHERE fire_id = ?",
            ("2020-01-01T00:00:00+00:00", claim.fire_id),
        )

        recovered = await rt.recover()
        assert recovered == 1
        assert len(wlp.executions) == 1
        # The execution is linked to this fire_id.
        assert claim.fire_id in wlp.executions
        await rt.stop()

    async def test_scenario_2_crash_after_wlp_accept(self, tmp_path):
        """Kit must-fix scenario. Runtime claims fire → calls
        WLP.execute_workflow → WLP creates execution row and
        returns → runtime crashes BEFORE persisting
        workflow_execution_id / status='dispatched' on
        trigger_fires. Restart sweep queries WLP by fire_id;
        WLP returns the existing execution_id; outbox row
        reconciles to dispatched WITHOUT a second
        execute_workflow call. Exactly one execution.
        """
        rt, wlp = await _setup_runtime_for_recovery(tmp_path)
        from kernos.kernel.triggers.predicate import (
            fire_window_key_for_on,
        )
        fwk = fire_window_key_for_on("evt_s2")
        claim = await rt._outbox.claim_fire(
            instance_id="inst1",
            trigger_id="trig_recover",
            fire_window_key=fwk,
            payload={"event_id": "evt_s2"},
            claim_owner="dead_runtime",
        )
        # Simulate: WLP accepted execution before the crash.
        existing_exec = await wlp.execute_workflow(
            fire_id=claim.fire_id,
            workflow_id="wf_recover",
            instance_id="inst1",
        )
        # Crash: outbox row is still pending; runtime never got
        # to mark_dispatched. Force past lease.
        await rt._outbox._db.execute(
            "UPDATE trigger_fires SET claimed_at = ? "
            "WHERE fire_id = ?",
            ("2020-01-01T00:00:00+00:00", claim.fire_id),
        )

        # Recovery sweep — reconciles without re-dispatching.
        before_calls = len(wlp.dispatch_calls)
        recovered = await rt.recover()
        after_calls = len(wlp.dispatch_calls)
        assert recovered == 1
        # No second WLP invocation.
        assert after_calls == before_calls, (
            "Kit must-fix invariant: recovery must NOT re-invoke "
            "WLP when fire_id is already known to WLP"
        )
        # Outbox row is now dispatched, linked to the original
        # execution.
        loaded = await rt._outbox.get_by_fire_id(claim.fire_id)
        assert loaded.status == "dispatched"
        assert loaded.workflow_execution_id == existing_exec
        # WLP still has exactly one execution for that fire_id.
        assert len(wlp.executions) == 1
        await rt.stop()

    async def test_scenario_3_crash_after_dispatch(self, tmp_path):
        """Runtime claimed + marked dispatched → WLP ran the
        workflow → crash before mark_completed. Restart sweep
        finds dispatched past lease and logs for operator
        triage. C2 confirms the row stays dispatched (not
        re-fired); full WLP-completion reconciliation lands in
        C5.
        """
        rt, wlp = await _setup_runtime_for_recovery(tmp_path)
        from kernos.kernel.triggers.predicate import (
            fire_window_key_for_on,
        )
        fwk = fire_window_key_for_on("evt_s3")
        claim = await rt._outbox.claim_fire(
            instance_id="inst1",
            trigger_id="trig_recover",
            fire_window_key=fwk,
            payload={"event_id": "evt_s3"},
            claim_owner=rt.claim_owner,
        )
        execution_id = await wlp.execute_workflow(
            fire_id=claim.fire_id,
            workflow_id="wf_recover",
            instance_id="inst1",
        )
        await rt._outbox.mark_dispatched(
            fire_id=claim.fire_id,
            claim_owner=rt.claim_owner,
            workflow_execution_id=execution_id,
        )
        # Force past dispatch_lease.
        await rt._outbox._db.execute(
            "UPDATE trigger_fires SET dispatched_at = ? "
            "WHERE fire_id = ?",
            ("2020-01-01T00:00:00+00:00", claim.fire_id),
        )

        before_calls = len(wlp.dispatch_calls)
        await rt.recover()
        # Recovery must NOT re-dispatch — the row is dispatched,
        # not pending.
        assert len(wlp.dispatch_calls) == before_calls
        # WLP still has exactly one execution.
        assert len(wlp.executions) == 1
        await rt.stop()

    async def test_scenario_4_duplicate_event_observation(
        self, tmp_path,
    ):
        """Same event_id observed twice in overlapping batches.
        Idempotency key catches the second; only one fire intent
        created; only one dispatch.
        """
        rt, wlp = await _setup_runtime_for_recovery(tmp_path)
        evt1 = _make_event(event_id="evt_s4_dup")
        evt2 = _make_event(event_id="evt_s4_dup")  # same id!
        first = await rt.on_event_observed(evt1)
        second = await rt.on_event_observed(evt2)
        assert first + second == 1
        assert len(wlp.executions) == 1
        await rt.stop()
