"""WTC v1 C1 — WLP fire_id idempotency (Kit must-fix).

Pins the public ``ExecutionEngine.execute_workflow(fire_id, ...)``
and ``find_execution_by_fire_id`` surface that closes the crash
window between WLP accept and trigger-runtime mark_dispatched.

These tests don't exercise the action loop — they pin the
execution-creation boundary's idempotency contract. Full action-loop
behaviour has separate coverage in test_workflows_execution_engine.
"""
from __future__ import annotations

import pytest

from kernos.kernel import event_stream
from kernos.kernel.workflows.action_library import ActionLibrary
from kernos.kernel.workflows.execution_engine import ExecutionEngine
from kernos.kernel.workflows.ledger import WorkflowLedger
from kernos.kernel.workflows.trigger_registry import (
    TriggerRegistry,
    _reset_for_tests as _reset_trigger_registry,
)
from kernos.kernel.workflows.workflow_registry import WorkflowRegistry


@pytest.fixture
async def engine(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    trig = TriggerRegistry()
    await trig.start(str(tmp_path))
    wfr = WorkflowRegistry()
    await wfr.start(str(tmp_path), trig)
    lib = ActionLibrary()
    ledger = WorkflowLedger(str(tmp_path))
    eng = ExecutionEngine()
    await eng.start(
        str(tmp_path), trig, wfr, lib, ledger,
        space_resolver=None,
    )
    yield eng
    await eng.stop()
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await event_stream._reset_for_tests()


# ---------------------------------------------------------------------------
# Schema migration + column shape
# ---------------------------------------------------------------------------


class TestSchema:
    async def test_fire_id_column_exists(self, engine):
        # Confirm the migration / fresh-install DDL added the column.
        async with engine._db.execute(
            "SELECT name FROM pragma_table_info('workflow_executions')"
        ) as cur:
            cols = {row[0] for row in await cur.fetchall()}
        assert "fire_id" in cols

    async def test_partial_unique_index_exists(self, engine):
        # The partial unique index is what catches concurrent INSERTs
        # racing on the same fire_id.
        async with engine._db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='workflow_executions'"
        ) as cur:
            indexes = {row[0] for row in await cur.fetchall()}
        assert "idx_executions_fire_id" in indexes


# ---------------------------------------------------------------------------
# execute_workflow happy path + idempotency
# ---------------------------------------------------------------------------


class TestExecuteWorkflowIdempotency:
    async def test_first_call_creates_execution(self, engine):
        execution_id = await engine.execute_workflow(
            fire_id="fire_test_1",
            workflow_id="wf_x",
            instance_id="inst_a",
            trigger_event_payload={"hello": "world"},
        )
        assert execution_id

        loaded = await engine.get_execution(execution_id)
        assert loaded is not None
        assert loaded.fire_id == "fire_test_1"
        assert loaded.workflow_id == "wf_x"
        assert loaded.state == "queued"

    async def test_duplicate_fire_id_returns_original(self, engine):
        first = await engine.execute_workflow(
            fire_id="fire_dup_test",
            workflow_id="wf_x",
            instance_id="inst_a",
            trigger_event_payload={"v": 1},
        )
        second = await engine.execute_workflow(
            fire_id="fire_dup_test",
            workflow_id="wf_x",
            instance_id="inst_a",
            trigger_event_payload={"v": 2},  # ignored — first call wins
        )
        assert first == second, (
            "duplicate fire_id must return the original execution_id; "
            "Kit must-fix invariant"
        )

    async def test_duplicate_fire_id_does_not_create_second_row(self, engine):
        await engine.execute_workflow(
            fire_id="fire_no_double",
            workflow_id="wf_x",
            instance_id="inst_a",
        )
        await engine.execute_workflow(
            fire_id="fire_no_double",
            workflow_id="wf_x",
            instance_id="inst_a",
        )
        async with engine._db.execute(
            "SELECT COUNT(*) FROM workflow_executions WHERE fire_id = ?",
            ("fire_no_double",),
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == 1

    async def test_different_fire_ids_create_distinct_executions(self, engine):
        a = await engine.execute_workflow(
            fire_id="fire_a", workflow_id="wf", instance_id="inst",
        )
        b = await engine.execute_workflow(
            fire_id="fire_b", workflow_id="wf", instance_id="inst",
        )
        assert a != b

    async def test_empty_fire_id_rejected(self, engine):
        # Empty fire_id is the legacy in-process Trigger-matched
        # path's signal; the public execute_workflow contract
        # requires a non-empty key so the partial unique index
        # actually applies.
        with pytest.raises(ValueError, match="non-empty fire_id"):
            await engine.execute_workflow(
                fire_id="",
                workflow_id="wf", instance_id="inst",
            )


# ---------------------------------------------------------------------------
# find_execution_by_fire_id (recovery sweep dependency)
# ---------------------------------------------------------------------------


class TestFindExecutionByFireId:
    async def test_returns_none_when_not_found(self, engine):
        result = await engine.find_execution_by_fire_id("fire_missing")
        assert result is None

    async def test_returns_execution_id_after_dispatch(self, engine):
        execution_id = await engine.execute_workflow(
            fire_id="fire_lookup_test",
            workflow_id="wf",
            instance_id="inst",
        )
        result = await engine.find_execution_by_fire_id("fire_lookup_test")
        assert result == execution_id

    async def test_empty_fire_id_returns_none(self, engine):
        # Defensive: legacy in-process executions have empty fire_id;
        # querying for "" must not match them. Kit must-fix invariant.
        result = await engine.find_execution_by_fire_id("")
        assert result is None


# ---------------------------------------------------------------------------
# Crash-recovery seam closure (the new AC6 scenario #2)
# ---------------------------------------------------------------------------


class TestCrashRecoverySeamClosure:
    """The Kit must-fix scenario: trigger-runtime calls
    execute_workflow(fire_id), WLP creates the row + returns,
    runtime crashes before persisting workflow_execution_id.
    Recovery sweep queries by fire_id and gets the existing
    execution_id back without re-dispatching.
    """

    async def test_recovery_finds_existing_execution_by_fire_id(
        self, engine,
    ):
        # Step 1: trigger runtime calls execute_workflow.
        first_execution_id = await engine.execute_workflow(
            fire_id="fire_crash_seam",
            workflow_id="wf_crash_test",
            instance_id="inst_a",
        )

        # Step 2: simulated crash — runtime never persisted the
        # workflow_execution_id back to its trigger_fires row. The
        # outbox row is still pending past its claim_lease.

        # Step 3: recovery sweep queries WLP by fire_id BEFORE
        # re-dispatching.
        recovered = await engine.find_execution_by_fire_id(
            "fire_crash_seam",
        )
        assert recovered == first_execution_id

        # Step 4: even if recovery defensively re-dispatches with
        # the same fire_id (which it shouldn't, given step 3, but
        # the contract holds either way), the same execution_id is
        # returned and no second row is created.
        re_dispatched = await engine.execute_workflow(
            fire_id="fire_crash_seam",
            workflow_id="wf_crash_test",
            instance_id="inst_a",
        )
        assert re_dispatched == first_execution_id

        async with engine._db.execute(
            "SELECT COUNT(*) FROM workflow_executions WHERE fire_id = ?",
            ("fire_crash_seam",),
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == 1, (
            "must produce exactly one workflow execution per "
            "Kit must-fix AC6 scenario #2"
        )


# ---------------------------------------------------------------------------
# Coexistence with legacy _on_trigger_match path (empty fire_id)
# ---------------------------------------------------------------------------


class TestLegacyPathCoexistence:
    async def test_empty_fire_id_rows_not_subject_to_unique_index(
        self, engine,
    ):
        # The legacy _on_trigger_match path creates rows with
        # empty fire_id. Multiple such rows must coexist (no
        # collision), since the unique index is partial on
        # non-empty fire_id only.
        from kernos.kernel.workflows.execution_engine import WorkflowExecution
        import json
        from datetime import datetime, timezone

        for i in range(3):
            row = WorkflowExecution(
                execution_id=f"legacy_{i}",
                workflow_id="wf",
                instance_id="inst",
                correlation_id=f"corr_{i}",
                state="queued",
                started_at=datetime.now(timezone.utc).isoformat(),
                trigger_event_payload={},
                trigger_event_id=f"evt_{i}",
                member_id="",
                fire_id="",  # legacy
            )
            await engine._db.execute(
                "INSERT INTO workflow_executions ("
                " execution_id, workflow_id, instance_id, correlation_id,"
                " state, action_index_completed, intermediate_state,"
                " last_heartbeat, aborted_reason, started_at, terminated_at,"
                " trigger_event_payload, trigger_event_id, member_id,"
                " gate_nonce, fire_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row.to_row(),
            )
        async with engine._db.execute(
            "SELECT COUNT(*) FROM workflow_executions WHERE fire_id = ''"
        ) as cur:
            count_row = await cur.fetchone()
        assert count_row[0] == 3, (
            "three empty-fire_id rows must coexist; the partial "
            "unique index excludes empty values"
        )
