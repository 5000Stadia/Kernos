"""WTC v1 C1b — triggers module skeleton tests.

Pins the predicate model, fire_window_key determinism, FireOutbox
CAS-based transitions, schema migration on trigger_fires, and the
runtime interface shell.

The actual evaluator (cron walk, event-driven match, before/after
due-time math) lands in C2 with its own tests.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from kernos.kernel.triggers import (
    DispatchPolicy,
    FireOutbox,
    TemporalRelation,
    TriggerEvaluationRuntime,
    TriggerPredicate,
    derive_fire_id,
    ensure_outbox_schema,
    fire_window_key_for_after,
    fire_window_key_for_before,
    fire_window_key_for_every,
    fire_window_key_for_on,
)
from kernos.kernel.triggers.errors import (
    DispatchPolicyError,
    PredicateValidationError,
    StaleClaimError,
    TemporalRelationError,
)
from kernos.kernel.triggers.outbox import (
    _PERMITTED_TRANSITIONS,
    _validate_transition,
)


# ---------------------------------------------------------------------------
# Predicate model — round-trip + validation
# ---------------------------------------------------------------------------


class TestTemporalRelation:
    def test_every_requires_cron_expression(self):
        with pytest.raises(TemporalRelationError, match="cron_expression"):
            from kernos.kernel.triggers.predicate import validate_temporal
            validate_temporal(TemporalRelation(kind="every"))

    def test_every_rejects_minutes(self):
        with pytest.raises(TemporalRelationError, match="minutes=0"):
            from kernos.kernel.triggers.predicate import validate_temporal
            validate_temporal(TemporalRelation(
                kind="every", cron_expression="*/5 * * * *", minutes=10,
            ))

    def test_before_requires_positive_minutes(self):
        with pytest.raises(TemporalRelationError, match="minutes > 0"):
            from kernos.kernel.triggers.predicate import validate_temporal
            validate_temporal(TemporalRelation(kind="before"))

    def test_before_rejects_cron_expression(self):
        with pytest.raises(TemporalRelationError, match="must not"):
            from kernos.kernel.triggers.predicate import validate_temporal
            validate_temporal(TemporalRelation(
                kind="before", minutes=5, cron_expression="*/5 * * * *",
            ))

    def test_on_rejects_minutes_or_cron(self):
        with pytest.raises(TemporalRelationError):
            from kernos.kernel.triggers.predicate import validate_temporal
            validate_temporal(TemporalRelation(kind="on", minutes=5))

    def test_unknown_kind_rejected(self):
        with pytest.raises(TemporalRelationError, match="must be one of"):
            from kernos.kernel.triggers.predicate import validate_temporal
            validate_temporal(TemporalRelation(kind="weekly"))  # type: ignore[arg-type]


class TestDispatchPolicy:
    def test_negative_dedup_window_rejected(self):
        with pytest.raises(DispatchPolicyError):
            from kernos.kernel.triggers.predicate import validate_dispatch_policy
            validate_dispatch_policy(DispatchPolicy(dedup_window_seconds=-1))

    def test_unknown_missed_window_rejected(self):
        with pytest.raises(DispatchPolicyError, match="missed_window"):
            from kernos.kernel.triggers.predicate import validate_dispatch_policy
            validate_dispatch_policy(DispatchPolicy(
                missed_window="ignore",  # type: ignore[arg-type]
            ))

    def test_negative_retry_rejected(self):
        with pytest.raises(DispatchPolicyError):
            from kernos.kernel.triggers.predicate import validate_dispatch_policy
            validate_dispatch_policy(DispatchPolicy(retry_on_dispatch_failure=-1))


class TestTriggerPredicate:
    def test_valid_every_predicate_round_trips(self):
        from kernos.kernel.triggers.predicate import validate_predicate
        pred = TriggerPredicate(
            event_selector={"op": "exists", "path": "event_id"},
            temporal_relation=TemporalRelation(
                kind="every", cron_expression="*/15 * * * *",
            ),
        )
        validate_predicate(pred)
        # Frozen dataclass invariant.
        with pytest.raises((AttributeError, Exception)):
            pred.temporal_relation = TemporalRelation(kind="on")  # type: ignore[misc]

    def test_invalid_event_selector_rejected(self):
        from kernos.kernel.triggers.predicate import validate_predicate
        with pytest.raises(PredicateValidationError, match="event_selector"):
            # Construct with a non-dict — dataclass accepts but
            # validate_predicate rejects.
            pred = TriggerPredicate.__new__(TriggerPredicate)
            object.__setattr__(pred, "event_selector", "not a dict")
            object.__setattr__(
                pred, "temporal_relation",
                TemporalRelation(kind="on"),
            )
            object.__setattr__(pred, "dispatch_policy", DispatchPolicy())
            validate_predicate(pred)


# ---------------------------------------------------------------------------
# fire_window_key + fire_id determinism
# ---------------------------------------------------------------------------


class TestFireWindowKeys:
    def test_every_window_key_deterministic(self):
        a = fire_window_key_for_every("*/5 * * * *", "2026-05-01T12:00:00")
        b = fire_window_key_for_every("*/5 * * * *", "2026-05-01T12:00:00")
        assert a == b

    def test_every_window_key_differs_per_time(self):
        a = fire_window_key_for_every("*/5 * * * *", "2026-05-01T12:00:00")
        b = fire_window_key_for_every("*/5 * * * *", "2026-05-01T12:05:00")
        assert a != b

    def test_on_window_key_uses_event_id(self):
        a = fire_window_key_for_on("evt_xyz")
        b = fire_window_key_for_on("evt_xyz")
        assert a == b
        assert "evt_xyz" in a

    def test_before_after_distinct(self):
        before = fire_window_key_for_before("evt_xyz", 30)
        after = fire_window_key_for_after("evt_xyz", 30)
        assert before != after


class TestFireIdDerivation:
    def test_same_inputs_produce_same_id(self):
        a = derive_fire_id("trig_a", "every::*/5::2026-05-01T12:00:00")
        b = derive_fire_id("trig_a", "every::*/5::2026-05-01T12:00:00")
        assert a == b

    def test_different_inputs_produce_different_ids(self):
        a = derive_fire_id("trig_a", "every::*/5::2026-05-01T12:00:00")
        b = derive_fire_id("trig_b", "every::*/5::2026-05-01T12:00:00")
        c = derive_fire_id("trig_a", "every::*/5::2026-05-01T12:05:00")
        assert a != b != c

    def test_empty_inputs_rejected(self):
        with pytest.raises(ValueError):
            derive_fire_id("", "key")
        with pytest.raises(ValueError):
            derive_fire_id("trig", "")


# ---------------------------------------------------------------------------
# Status state machine
# ---------------------------------------------------------------------------


class TestStatusStateMachine:
    def test_pending_to_dispatched_allowed(self):
        _validate_transition("pending", "dispatched")

    def test_dispatched_to_completed_allowed(self):
        _validate_transition("dispatched", "completed")

    def test_pending_to_failed_allowed(self):
        _validate_transition("pending", "failed")

    def test_dispatched_to_failed_allowed(self):
        _validate_transition("dispatched", "failed")

    def test_completed_terminal(self):
        with pytest.raises(StaleClaimError):
            _validate_transition("completed", "dispatched")

    def test_failed_terminal(self):
        with pytest.raises(StaleClaimError):
            _validate_transition("failed", "completed")

    def test_pending_to_completed_forbidden(self):
        # Skipping dispatched is illegal; must transit
        # pending → dispatched → completed.
        with pytest.raises(StaleClaimError):
            _validate_transition("pending", "completed")


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_with_legacy_table(tmp_path):
    """A fresh instance.db with the legacy trigger_fires shape only."""
    db_path = tmp_path / "instance.db"
    db = await aiosqlite.connect(str(db_path), isolation_level=None)
    db.row_factory = aiosqlite.Row
    await db.execute(
        "CREATE TABLE IF NOT EXISTS trigger_fires ("
        " trigger_id TEXT NOT NULL,"
        " idempotency_key TEXT NOT NULL,"
        " event_id TEXT NOT NULL,"
        " fired_at TEXT NOT NULL,"
        " PRIMARY KEY (trigger_id, idempotency_key)"
        ")"
    )
    yield db
    await db.close()


class TestSchemaMigration:
    async def test_migration_adds_all_columns(self, db_with_legacy_table):
        await ensure_outbox_schema(db_with_legacy_table)
        async with db_with_legacy_table.execute(
            "SELECT name FROM pragma_table_info('trigger_fires')"
        ) as cur:
            cols = {row[0] for row in await cur.fetchall()}
        for required in (
            "trigger_id", "idempotency_key", "event_id", "fired_at",
            "instance_id", "status", "claimed_at", "claim_owner",
            "dispatched_at", "completed_at", "workflow_execution_id",
            "last_error", "catch_up", "payload_json", "fire_id",
        ):
            assert required in cols, f"missing {required!r}"

    async def test_migration_idempotent(self, db_with_legacy_table):
        # Running twice must not raise.
        await ensure_outbox_schema(db_with_legacy_table)
        await ensure_outbox_schema(db_with_legacy_table)

    async def test_recovery_indexes_created(self, db_with_legacy_table):
        await ensure_outbox_schema(db_with_legacy_table)
        async with db_with_legacy_table.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='trigger_fires'"
        ) as cur:
            indexes = {row[0] for row in await cur.fetchall()}
        assert "idx_trigger_fires_status_pending" in indexes
        assert "idx_trigger_fires_instance_status" in indexes
        assert "idx_trigger_fires_fire_id" in indexes


# ---------------------------------------------------------------------------
# FireOutbox CAS / claim race-safety
# ---------------------------------------------------------------------------


@pytest.fixture
async def outbox(tmp_path):
    ob = FireOutbox()
    await ob.start(str(tmp_path))
    yield ob
    await ob.stop()


class TestFireOutboxClaim:
    async def test_claim_creates_pending_row(self, outbox):
        record = await outbox.claim_fire(
            instance_id="inst",
            trigger_id="trig_a",
            fire_window_key="every::*/5::2026-05-01T12:00",
            payload={"event_id": "evt_1"},
            claim_owner="runner_1",
        )
        assert record is not None
        assert record.status == "pending"
        assert record.fire_id
        assert record.fire_window_key == "every::*/5::2026-05-01T12:00"

    async def test_concurrent_claims_one_winner(self, outbox):
        # Two concurrent claim attempts on the same window — only
        # one returns a record; the other returns None.
        async def attempt():
            return await outbox.claim_fire(
                instance_id="inst",
                trigger_id="trig_a",
                fire_window_key="every::*/5::2026-05-01T12:00",
                payload={},
                claim_owner="runner",
            )

        first, second = await asyncio.gather(attempt(), attempt())
        winners = [r for r in (first, second) if r is not None]
        losers = [r for r in (first, second) if r is None]
        assert len(winners) == 1
        assert len(losers) == 1


class TestFireOutboxTransitions:
    async def test_mark_dispatched_advances_state(self, outbox):
        record = await outbox.claim_fire(
            instance_id="inst", trigger_id="trig_a",
            fire_window_key="k1", payload={}, claim_owner="r1",
        )
        await outbox.mark_dispatched(
            fire_id=record.fire_id, claim_owner="r1",
            workflow_execution_id="exec_1",
        )
        loaded = await outbox.get_by_fire_id(record.fire_id)
        assert loaded.status == "dispatched"
        assert loaded.workflow_execution_id == "exec_1"

    async def test_mark_dispatched_rejects_wrong_owner(self, outbox):
        record = await outbox.claim_fire(
            instance_id="inst", trigger_id="trig_a",
            fire_window_key="k2", payload={}, claim_owner="r1",
        )
        with pytest.raises(StaleClaimError):
            await outbox.mark_dispatched(
                fire_id=record.fire_id, claim_owner="r_other",
                workflow_execution_id="exec_1",
            )

    async def test_mark_completed_only_from_dispatched(self, outbox):
        record = await outbox.claim_fire(
            instance_id="inst", trigger_id="trig_a",
            fire_window_key="k3", payload={}, claim_owner="r1",
        )
        # pending → completed not permitted (must transit
        # dispatched first).
        with pytest.raises(StaleClaimError):
            await outbox.mark_completed(
                fire_id=record.fire_id, claim_owner="r1",
            )

    async def test_mark_completed_idempotent_for_owner(self, outbox):
        record = await outbox.claim_fire(
            instance_id="inst", trigger_id="trig_a",
            fire_window_key="k4", payload={}, claim_owner="r1",
        )
        await outbox.mark_dispatched(
            fire_id=record.fire_id, claim_owner="r1",
            workflow_execution_id="exec_1",
        )
        await outbox.mark_completed(
            fire_id=record.fire_id, claim_owner="r1",
        )
        # Second call must not raise.
        await outbox.mark_completed(
            fire_id=record.fire_id, claim_owner="r1",
        )

    async def test_reconcile_to_dispatched_closes_seam(self, outbox):
        # the design review must-fix scenario: WLP returned an execution_id; the
        # runtime crashed before mark_dispatched. Recovery sweep
        # invokes reconcile_to_dispatched without claim_owner.
        record = await outbox.claim_fire(
            instance_id="inst", trigger_id="trig_a",
            fire_window_key="k_seam", payload={}, claim_owner="r_orig",
        )
        ok = await outbox.reconcile_to_dispatched(
            fire_id=record.fire_id,
            workflow_execution_id="exec_recovered",
        )
        assert ok is True
        loaded = await outbox.get_by_fire_id(record.fire_id)
        assert loaded.status == "dispatched"
        assert loaded.workflow_execution_id == "exec_recovered"
        # Re-running reconcile is a no-op (status no longer pending).
        ok2 = await outbox.reconcile_to_dispatched(
            fire_id=record.fire_id,
            workflow_execution_id="exec_recovered",
        )
        assert ok2 is False


# ---------------------------------------------------------------------------
# Recovery sweep helpers
# ---------------------------------------------------------------------------


class TestRecoveryQueries:
    async def test_find_pending_past_lease_filters_recent(self, outbox):
        # Just-claimed row shouldn't appear when we query for rows
        # past a generous lease.
        record = await outbox.claim_fire(
            instance_id="inst", trigger_id="trig_a",
            fire_window_key="k_recent", payload={}, claim_owner="r1",
        )
        results = await outbox.find_pending_past_lease(
            claim_lease_seconds=3600,
        )
        assert all(r.fire_id != record.fire_id for r in results)

    async def test_reclaim_transitions_orphan(self, outbox):
        record = await outbox.claim_fire(
            instance_id="inst", trigger_id="trig_a",
            fire_window_key="k_orphan", payload={}, claim_owner="r_orig",
        )
        new = await outbox.reclaim(
            fire_id=record.fire_id, new_claim_owner="r_recovery",
        )
        assert new is not None
        assert new.claim_owner == "r_recovery"


# ---------------------------------------------------------------------------
# Runtime interface shell
# ---------------------------------------------------------------------------


class TestRuntimeShell:
    async def test_register_persists_in_memory(self, tmp_path):
        rt = TriggerEvaluationRuntime()
        await rt.start(data_dir=str(tmp_path))
        try:
            pred = TriggerPredicate(
                event_selector={"op": "exists", "path": "event_id"},
                temporal_relation=TemporalRelation(
                    kind="every", cron_expression="*/5 * * * *",
                ),
            )
            await rt.register(
                trigger_id="trig_x",
                instance_id="inst_a",
                workflow_id="wf_x",
                predicate=pred,
            )
            actives = await rt.list_active()
            assert len(actives) == 1
            assert actives[0]["trigger_id"] == "trig_x"
        finally:
            await rt.stop()

    async def test_register_validation_atomic(self, tmp_path):
        # Invalid predicate must raise BEFORE any state is mutated.
        rt = TriggerEvaluationRuntime()
        await rt.start(data_dir=str(tmp_path))
        try:
            invalid_pred = TriggerPredicate(
                event_selector={"op": "exists", "path": "event_id"},
                temporal_relation=TemporalRelation(kind="every"),  # missing cron
            )
            with pytest.raises(TemporalRelationError):
                await rt.register(
                    trigger_id="trig_y",
                    instance_id="inst_a",
                    workflow_id="wf_y",
                    predicate=invalid_pred,
                )
            assert await rt.list_active() == []
        finally:
            await rt.stop()

    async def test_evaluate_now_no_op_in_c1(self, tmp_path):
        rt = TriggerEvaluationRuntime()
        await rt.start(data_dir=str(tmp_path))
        try:
            assert await rt.evaluate_now() == 0
        finally:
            await rt.stop()

    async def test_recover_no_op_in_c1(self, tmp_path):
        rt = TriggerEvaluationRuntime()
        await rt.start(data_dir=str(tmp_path))
        try:
            assert await rt.recover() == 0
        finally:
            await rt.stop()

    async def test_claim_owner_is_per_process_stable(self, tmp_path):
        rt = TriggerEvaluationRuntime()
        await rt.start(data_dir=str(tmp_path))
        try:
            owner = rt.claim_owner
            assert owner.startswith("runtime:")
            # Stable for the runtime's lifetime.
            assert rt.claim_owner == owner
        finally:
            await rt.stop()
