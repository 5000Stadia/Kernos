"""Crash-idempotent action log tests (DRAFTER C1, AC #27 + #30).

Pins:

* :data:`ALLOWED_ACTION_TYPES` exact set (any addition is a deliberate
  substrate change).
* ``target_id`` NOT NULL invariant — empty/None rejected at the API
  boundary.
* ``record_and_perform`` atomicity: side effect + log row in same
  transaction. UNIQUE-constraint violation translates to replay-skip
  (returns prior summary).
* Cross-instance isolation via composite PK including ``instance_id``.
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from kernos.kernel.cohorts._substrate.action_log import (
    ALLOWED_ACTION_TYPES,
    ActionLog,
    ActionLogConflict,
    ActionLogInvalidActionType,
    ActionLogInvalidTarget,
)


@pytest.fixture
async def action_log(tmp_path):
    log = ActionLog(cohort_id="drafter")
    await log.start(str(tmp_path))
    yield log
    await log.stop()


# ===========================================================================
# Pinned action-type set
# ===========================================================================


class TestActionTypeSurface:
    def test_allowed_action_types_pinned(self):
        """Pin the exact set so adding a type is deliberate."""
        assert ALLOWED_ACTION_TYPES == frozenset({
            "create_draft",
            "update_draft",
            "abandon_draft",
            "emit_signal",
            "emit_receipt",
        })

    @pytest.mark.parametrize("bogus", [
        "delete_draft",
        "mark_committed",
        "register_workflow",
        "send_message",
        "",
    ])
    async def test_unknown_action_type_rejected(self, action_log, bogus):
        with pytest.raises(ActionLogInvalidActionType):
            await action_log.is_already_done(
                instance_id="inst_a",
                source_event_id="evt_x",
                action_type=bogus,
                target_id="t",
            )


# ===========================================================================
# NOT NULL target_id (the design review pin v1→v2)
# ===========================================================================


class TestTargetIdNotNull:
    @pytest.mark.parametrize("bad_target", ["", None])
    async def test_empty_target_id_rejected_at_is_already_done(
        self, action_log, bad_target,
    ):
        with pytest.raises(ActionLogInvalidTarget):
            await action_log.is_already_done(
                instance_id="inst_a",
                source_event_id="evt_x",
                action_type="create_draft",
                target_id=bad_target,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize("bad_target", ["", None])
    async def test_empty_target_id_rejected_at_record_and_perform(
        self, action_log, bad_target,
    ):
        async def _noop():
            return {}

        with pytest.raises(ActionLogInvalidTarget):
            await action_log.record_and_perform(
                instance_id="inst_a",
                source_event_id="evt_x",
                action_type="create_draft",
                target_id=bad_target,  # type: ignore[arg-type]
                perform=_noop,
            )

    async def test_schema_target_id_not_null(self, tmp_path, action_log):
        """The on-disk schema must declare target_id NOT NULL so a
        future bug bypassing the API guard still hits the SQL constraint."""
        async with action_log._db.execute(
            "PRAGMA table_info(cohort_action_log)"
        ) as cur:
            rows = await cur.fetchall()
        cols = {r[1]: r for r in rows}
        assert "target_id" in cols
        # PRAGMA returns notnull as an int column; should be 1.
        assert cols["target_id"][3] == 1, (
            "cohort_action_log.target_id MUST be declared NOT NULL "
            "(the design review pin v1→v2). SQLite NULL in composite keys defeats "
            "UNIQUE semantics."
        )


# ===========================================================================
# Atomic record_and_perform
# ===========================================================================


class TestRecordAndPerform:
    async def test_first_call_runs_perform_and_records(self, action_log):
        called = 0

        async def perform():
            nonlocal called
            called += 1
            return {"draft_id": "d-1"}

        result = await action_log.record_and_perform(
            instance_id="inst_a",
            source_event_id="evt_1",
            action_type="create_draft",
            target_id="d-1",
            perform=perform,
        )
        assert called == 1
        assert result == {"draft_id": "d-1"}

    async def test_replay_returns_prior_summary_without_re_running(
        self, action_log,
    ):
        called = 0

        async def perform():
            nonlocal called
            called += 1
            return {"draft_id": "d-1", "version": called}

        await action_log.record_and_perform(
            instance_id="inst_a",
            source_event_id="evt_1",
            action_type="create_draft",
            target_id="d-1",
            perform=perform,
            result_to_summary=lambda r: r,
        )
        # Second call (replay) must NOT invoke perform.
        result = await action_log.record_and_perform(
            instance_id="inst_a",
            source_event_id="evt_1",
            action_type="create_draft",
            target_id="d-1",
            perform=perform,
        )
        assert called == 1, "perform must run exactly once across replays"
        assert result["draft_id"] == "d-1"

    async def test_perform_exception_marks_failed_and_allows_retry(self, action_log):
        """Codex mid-batch fix: claim-first protocol leaves a row with
        status='failed' so the next call can detect and retry. The
        original "no record" semantic is replaced; the new contract is
        "retry-safe via the status field"."""
        attempts = []

        async def perform():
            attempts.append("called")
            raise RuntimeError("simulated crash mid-perform")

        with pytest.raises(RuntimeError, match="simulated crash"):
            await action_log.record_and_perform(
                instance_id="inst_a",
                source_event_id="evt_1",
                action_type="create_draft",
                target_id="d-1",
                perform=perform,
            )
        # Record exists with status='failed'.
        prior = await action_log.is_already_done(
            instance_id="inst_a",
            source_event_id="evt_1",
            action_type="create_draft",
            target_id="d-1",
        )
        assert prior is not None
        assert prior.status == "failed"

        # Retry runs perform again (the row is removed before re-claim).
        async def perform_success():
            attempts.append("retry")
            return {"draft_id": "d-1"}

        result = await action_log.record_and_perform(
            instance_id="inst_a",
            source_event_id="evt_1",
            action_type="create_draft",
            target_id="d-1",
            perform=perform_success,
        )
        assert attempts == ["called", "retry"]
        assert result == {"draft_id": "d-1"}
        # And now status is 'performed'.
        latest = await action_log.is_already_done(
            instance_id="inst_a",
            source_event_id="evt_1",
            action_type="create_draft",
            target_id="d-1",
        )
        assert latest is not None
        assert latest.status == "performed"

    async def test_distinct_keys_run_independently(self, action_log):
        runs = []

        async def make_perform(key):
            async def perform():
                runs.append(key)
                return {"key": key}
            return perform

        await action_log.record_and_perform(
            instance_id="inst_a", source_event_id="evt_1",
            action_type="create_draft", target_id="d-1",
            perform=await make_perform("a"),
        )
        await action_log.record_and_perform(
            instance_id="inst_a", source_event_id="evt_1",
            action_type="create_draft", target_id="d-2",
            perform=await make_perform("b"),
        )
        await action_log.record_and_perform(
            instance_id="inst_a", source_event_id="evt_2",
            action_type="create_draft", target_id="d-1",
            perform=await make_perform("c"),
        )
        assert runs == ["a", "b", "c"]


# ===========================================================================
# Cross-instance isolation
# ===========================================================================


class TestClaimFirstProtocol:
    """Codex mid-batch fix: claim-first protocol — INSERT pending,
    perform, UPDATE performed/failed. Verifies the three phases."""

    async def test_pending_row_visible_during_perform(self, action_log):
        from kernos.kernel.cohorts._substrate.action_log import STATUS_PENDING
        observed_during = []

        async def perform():
            # While we're in here, the row should already be 'pending'.
            mid = await action_log.is_already_done(
                instance_id="inst_a", source_event_id="evt_1",
                action_type="create_draft", target_id="d-1",
            )
            observed_during.append(mid)
            return {"draft_id": "d-1"}

        await action_log.record_and_perform(
            instance_id="inst_a", source_event_id="evt_1",
            action_type="create_draft", target_id="d-1",
            perform=perform,
        )
        assert len(observed_during) == 1
        assert observed_during[0] is not None
        assert observed_during[0].status == STATUS_PENDING

    async def test_performed_status_after_success(self, action_log):
        from kernos.kernel.cohorts._substrate.action_log import STATUS_PERFORMED

        async def perform():
            return {"draft_id": "d-1"}

        await action_log.record_and_perform(
            instance_id="inst_a", source_event_id="evt_1",
            action_type="create_draft", target_id="d-1",
            perform=perform,
            result_to_summary=lambda r: r,
        )
        record = await action_log.is_already_done(
            instance_id="inst_a", source_event_id="evt_1",
            action_type="create_draft", target_id="d-1",
        )
        assert record.status == STATUS_PERFORMED
        assert record.result_summary == {"draft_id": "d-1"}


class TestSchemaConstraints:
    """Codex mid-batch hardening: DDL-level CHECK constraint on status."""

    async def test_status_check_constraint_in_schema(self, action_log):
        # Direct INSERT of an invalid status value should fail at the
        # SQL layer.
        with pytest.raises(aiosqlite.IntegrityError):
            await action_log._db.execute(
                "INSERT INTO cohort_action_log "
                "(cohort_id, instance_id, source_event_id, action_type, "
                " target_id, status, result_summary, performed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "drafter", "inst_a", "evt_x", "create_draft",
                    "d-x", "bogus_status", "{}", "2026-04-30T00:00:00+00:00",
                ),
            )


class TestCrossInstanceIsolation:
    async def test_same_target_distinct_instance_runs_twice(self, action_log):
        """Same source_event_id + action_type + target_id but different
        instance_id MUST run the side effect once per instance —
        composite-key includes instance_id."""
        runs = []

        async def make_perform(label):
            async def perform():
                runs.append(label)
                return {"label": label}
            return perform

        await action_log.record_and_perform(
            instance_id="inst_a", source_event_id="evt_1",
            action_type="create_draft", target_id="d-1",
            perform=await make_perform("A"),
        )
        await action_log.record_and_perform(
            instance_id="inst_b", source_event_id="evt_1",
            action_type="create_draft", target_id="d-1",
            perform=await make_perform("B"),
        )
        assert runs == ["A", "B"]

    async def test_is_already_done_scoped_to_instance(self, action_log):
        async def perform():
            return {"draft_id": "d-1"}

        await action_log.record_and_perform(
            instance_id="inst_a", source_event_id="evt_1",
            action_type="create_draft", target_id="d-1",
            perform=perform,
        )
        # Inst B should not see inst A's record.
        assert await action_log.is_already_done(
            instance_id="inst_b", source_event_id="evt_1",
            action_type="create_draft", target_id="d-1",
        ) is None
