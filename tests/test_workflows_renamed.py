"""WLP rename + approval-event-id schema migration tests (STS C2).

Spec reference: SPEC-STS-v2 AC #13 (rename) + schema migration safety.
Pins:

* Public ``register_workflow`` is gone; ``_register_workflow_unbound``
  is the underscore-prefixed direct entry point.
* ``register_workflow_from_file`` still works and routes through the
  underscore method.
* ``approval_event_id`` column exists on the ``workflows`` table; the
  partial UNIQUE index ``idx_workflows_approval_unique`` rejects
  duplicate ``(instance_id, approval_event_id)`` pairs only when
  ``approval_event_id IS NOT NULL`` (so legacy NULL rows coexist).
* Pre-STS databases auto-migrate via ALTER TABLE; existing rows get
  NULL ``approval_event_id``.
"""
from __future__ import annotations

import aiosqlite
import pytest

from kernos.kernel import event_stream
from kernos.kernel.workflows.trigger_registry import (
    TriggerRegistry,
    _reset_for_tests as _reset_trigger_registry,
)
from kernos.kernel.workflows.workflow_registry import (
    ActionDescriptor,
    Bounds,
    ContinuationRules,
    Verifier,
    Workflow,
    WorkflowRegistry,
)


@pytest.fixture
async def stack(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    trig = TriggerRegistry()
    await trig.start(str(tmp_path))
    wfr = WorkflowRegistry()
    await wfr.start(str(tmp_path), trig)
    yield {"tmp_path": tmp_path, "wfr": wfr, "trig": trig}
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await event_stream._reset_for_tests()


def _wf(workflow_id="wf-1", instance_id="inst_a") -> Workflow:
    return Workflow(
        workflow_id=workflow_id,
        instance_id=instance_id,
        name="renamed-test",
        description="",
        owner="owner",
        version="1",
        bounds=Bounds(iteration_count=1),
        verifier=Verifier(flavor="deterministic", check="x == y"),
        action_sequence=[
            ActionDescriptor(
                action_type="mark_state",
                parameters={"key": "k", "value": "v", "scope": "ledger"},
                continuation_rules=ContinuationRules(),
            ),
        ],
    )


class TestRename:
    """AC #13: register_workflow renamed to _register_workflow_unbound."""

    def test_public_register_workflow_no_longer_exists(self, stack):
        wfr = stack["wfr"]
        assert not hasattr(wfr, "register_workflow"), (
            "AC #13: public register_workflow must be renamed; production "
            "callers go through SubstrateTools.register_workflow"
        )

    def test_underscore_register_workflow_unbound_exists(self, stack):
        wfr = stack["wfr"]
        assert callable(getattr(wfr, "_register_workflow_unbound", None))

    async def test_underscore_method_persists_workflow(self, stack):
        wfr = stack["wfr"]
        wf = await wfr._register_workflow_unbound(_wf())
        assert wf.workflow_id == "wf-1"
        listed = await wfr.list_workflows("inst_a")
        assert any(w.workflow_id == "wf-1" for w in listed)

    async def test_register_workflow_from_file_routes_through_underscore(
        self, stack, tmp_path,
    ):
        """register_workflow_from_file is the only public-facing entry on
        WLP today; it MUST continue to work and route through the
        underscore method internally."""
        wfr = stack["wfr"]
        descriptor = tmp_path / "test.workflow.yaml"
        descriptor.write_text(
            "workflow_id: wf-from-file\n"
            "instance_id: inst_a\n"
            "name: from file\n"
            "description: \"\"\n"
            "owner: owner\n"
            "version: \"1\"\n"
            "bounds:\n"
            "  iteration_count: 1\n"
            "verifier:\n"
            "  flavor: deterministic\n"
            "  check: x == y\n"
            "action_sequence:\n"
            "  - action_type: mark_state\n"
            "    parameters:\n"
            "      key: k\n"
            "      value: v\n"
            "      scope: ledger\n",
        )
        wf = await wfr.register_workflow_from_file(str(descriptor))
        assert wf.workflow_id == "wf-from-file"


class TestApprovalEventIdSchema:
    """AC #11: schema gains approval_event_id column + partial UNIQUE."""

    async def test_workflows_table_has_approval_event_id_column(self, stack):
        wfr = stack["wfr"]
        async with wfr._db.execute("PRAGMA table_info(workflows)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        assert "approval_event_id" in cols

    async def test_partial_unique_index_exists(self, stack):
        wfr = stack["wfr"]
        async with wfr._db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND "
            "name = 'idx_workflows_approval_unique'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None, (
            "idx_workflows_approval_unique should exist after schema setup"
        )

    async def test_register_with_approval_event_id_writes_column(self, stack):
        wfr = stack["wfr"]
        await wfr._register_workflow_unbound(_wf(), approval_event_id="evt-1")
        async with wfr._db.execute(
            "SELECT approval_event_id FROM workflows WHERE workflow_id = ?",
            ("wf-1",),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "evt-1"

    async def test_register_without_approval_event_id_writes_null(self, stack):
        wfr = stack["wfr"]
        await wfr._register_workflow_unbound(_wf())
        async with wfr._db.execute(
            "SELECT approval_event_id FROM workflows WHERE workflow_id = ?",
            ("wf-1",),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] is None

    async def test_duplicate_approval_event_id_raises_integrity_error(self, stack):
        wfr = stack["wfr"]
        await wfr._register_workflow_unbound(_wf(workflow_id="wf-1"), approval_event_id="evt-shared")
        with pytest.raises(aiosqlite.IntegrityError):
            await wfr._register_workflow_unbound(
                _wf(workflow_id="wf-2"), approval_event_id="evt-shared",
            )

    async def test_two_null_approval_event_ids_coexist(self, stack):
        """Partial UNIQUE excludes NULL — pre-STS workflows and tests
        that don't supply an approval can coexist freely."""
        wfr = stack["wfr"]
        await wfr._register_workflow_unbound(_wf(workflow_id="wf-1"))
        await wfr._register_workflow_unbound(_wf(workflow_id="wf-2"))
        listed = await wfr.list_workflows("inst_a")
        assert {w.workflow_id for w in listed} == {"wf-1", "wf-2"}


class TestLazyMigration:
    """A pre-STS database has no approval_event_id column; first start
    auto-migrates."""

    async def test_alter_table_in_place(self, tmp_path):
        await event_stream._reset_for_tests()
        db_path = tmp_path / "instance.db"
        # Build a legacy-shaped workflows table without approval_event_id.
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                """
                CREATE TABLE workflows (
                    workflow_id     TEXT PRIMARY KEY,
                    instance_id     TEXT NOT NULL,
                    name            TEXT NOT NULL,
                    description     TEXT DEFAULT '',
                    owner           TEXT DEFAULT '',
                    version         TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'active',
                    descriptor_json TEXT NOT NULL,
                    created_at      TEXT NOT NULL
                )
                """
            )
            await db.execute(
                "INSERT INTO workflows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy_wf", "inst_a", "legacy", "", "owner", "1",
                    "active", "{}", "2026-04-29T00:00:00+00:00",
                ),
            )
            await db.commit()

        # Bring up the registry — schema migration should add the column
        # and create the index.
        trig = TriggerRegistry()
        await trig.start(str(tmp_path))
        wfr = WorkflowRegistry()
        await wfr.start(str(tmp_path), trig)
        try:
            async with wfr._db.execute("PRAGMA table_info(workflows)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            assert "approval_event_id" in cols, (
                "lazy migration should add approval_event_id"
            )
            async with wfr._db.execute(
                "SELECT approval_event_id FROM workflows WHERE workflow_id = ?",
                ("legacy_wf",),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] is None, (
                "legacy rows should retain NULL approval_event_id"
            )
        finally:
            await wfr.stop()
            await _reset_trigger_registry(trig)
            await event_stream._reset_for_tests()
