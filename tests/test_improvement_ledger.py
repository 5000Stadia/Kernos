"""IMPROVEMENT-ATTEMPT-LEDGER-V1 (2026-05-22) acceptance tests.

Pins the three-table schema + helpers + renderer shape. Operator-
facing only (no agent layer — per [[agent-facing-natural-simplicity]]
this is the "no agent layer needed" case for an observer
surface).
"""
from __future__ import annotations

import pytest

from kernos.kernel import improvement_ledger as ledger
from kernos.kernel.instance_db import InstanceDB


# ============================================================
# AC1 — schema created
# ============================================================


@pytest.fixture
async def db(tmp_path):
    d = InstanceDB(str(tmp_path))
    await d.connect()
    yield d
    await d.close()


class TestSchema:
    async def test_ac1_attempts_table_exists(self, db):
        async with db._conn.execute(
            "PRAGMA table_info(improvement_attempts)"
        ) as cur:
            cols = {r[1] for r in await cur.fetchall()}
        expected = {
            "attempt_id", "instance_id", "started_at", "ended_at",
            "spec_requirement", "primary_coding_agent",
            "reviewer_coding_agent", "worktree_path",
            "spec_iterations", "spec_iterations_outcome",
            "impl_iterations", "impl_iterations_outcome",
            "final_commit_sha", "test_outcome", "first_pass_green",
            "final_state",
        }
        assert cols == expected

    async def test_ac1_commits_table_exists(self, db):
        async with db._conn.execute(
            "PRAGMA table_info(improvement_attempt_commits)"
        ) as cur:
            cols = {r[1] for r in await cur.fetchall()}
        expected = {
            "attempt_id", "commit_sequence", "commit_sha",
            "parent_sha", "pushed_at", "approval_id",
            "test_outcome_after_this_commit", "recovery_trigger",
        }
        assert cols == expected

    async def test_ac1_events_table_exists(self, db):
        async with db._conn.execute(
            "PRAGMA table_info(improvement_attempt_events)"
        ) as cur:
            cols = {r[1] for r in await cur.fetchall()}
        expected = {
            "event_id", "attempt_id", "sequence", "timestamp",
            "kind", "detail",
        }
        assert cols == expected


# ============================================================
# AC2-6 — attempts CRUD
# ============================================================


class TestAttemptsCRUD:
    async def test_ac2_create_attempt(self, db):
        await ledger.create_attempt(
            db._conn,
            instance_id="t1",
            attempt_id="att_a",
            spec_requirement="add a comment to README",
        )
        row = await ledger.get_attempt(db._conn, "att_a")
        assert row is not None
        assert row["attempt_id"] == "att_a"
        assert row["spec_requirement"] == "add a comment to README"
        assert row["final_state"] is None
        assert row["spec_iterations"] == 0

    async def test_ac3_update_attempt_only_passed_fields(self, db):
        await ledger.create_attempt(
            db._conn, instance_id="t1", attempt_id="att_b",
            spec_requirement="req",
        )
        await ledger.update_attempt(
            db._conn, attempt_id="att_b",
            spec_iterations=2,
            spec_iterations_outcome="GREEN",
        )
        row = await ledger.get_attempt(db._conn, "att_b")
        assert row["spec_iterations"] == 2
        assert row["spec_iterations_outcome"] == "GREEN"
        # Other fields unchanged
        assert row["impl_iterations"] == 0
        assert row["final_state"] is None

    async def test_update_attempt_rejects_unknown_field(self, db):
        await ledger.create_attempt(
            db._conn, instance_id="t1", attempt_id="att_c",
            spec_requirement="req",
        )
        with pytest.raises(ValueError):
            await ledger.update_attempt(
                db._conn, attempt_id="att_c",
                bogus_field="x",
            )

    async def test_ac6_get_attempt_missing_returns_none(self, db):
        row = await ledger.get_attempt(db._conn, "never_seen")
        assert row is None


# ============================================================
# AC7 — list_recent_attempts
# ============================================================


class TestListRecent:
    async def test_ac7_orders_by_started_desc(self, db):
        await ledger.create_attempt(
            db._conn, instance_id="t1", attempt_id="old",
            spec_requirement="x", started_at="2026-01-01T00:00:00",
        )
        await ledger.create_attempt(
            db._conn, instance_id="t1", attempt_id="new",
            spec_requirement="y", started_at="2026-05-22T00:00:00",
        )
        recent = await ledger.list_recent_attempts(
            db._conn, instance_id="t1", limit=10,
        )
        assert recent[0]["attempt_id"] == "new"
        assert recent[1]["attempt_id"] == "old"

    async def test_list_recent_isolates_by_instance(self, db):
        await ledger.create_attempt(
            db._conn, instance_id="t1", attempt_id="a",
            spec_requirement="x",
        )
        await ledger.create_attempt(
            db._conn, instance_id="t2", attempt_id="b",
            spec_requirement="y",
        )
        t1 = await ledger.list_recent_attempts(db._conn, "t1")
        ids = {a["attempt_id"] for a in t1}
        assert "a" in ids
        assert "b" not in ids


# ============================================================
# AC4 — append_event sequence
# ============================================================


class TestAppendEvent:
    async def test_ac4_sequence_increments_per_attempt(self, db):
        await ledger.create_attempt(
            db._conn, instance_id="t1", attempt_id="att_e",
            spec_requirement="x",
        )
        s1 = await ledger.append_event(
            db._conn, attempt_id="att_e", kind="spec_iteration",
            detail="round 1",
        )
        s2 = await ledger.append_event(
            db._conn, attempt_id="att_e", kind="spec_iteration",
            detail="round 2",
        )
        assert s1 == 1
        assert s2 == 2

    async def test_get_attempt_events_ordered_by_sequence(self, db):
        await ledger.create_attempt(
            db._conn, instance_id="t1", attempt_id="att_f",
            spec_requirement="x",
        )
        await ledger.append_event(
            db._conn, attempt_id="att_f", kind="a",
        )
        await ledger.append_event(
            db._conn, attempt_id="att_f", kind="b",
        )
        events = await ledger.get_attempt_events(db._conn, "att_f")
        assert [e["sequence"] for e in events] == [1, 2]
        assert [e["kind"] for e in events] == ["a", "b"]


# ============================================================
# AC5 — record_commit
# ============================================================


class TestRecordCommit:
    async def test_ac5_increments_commit_sequence(self, db):
        await ledger.create_attempt(
            db._conn, instance_id="t1", attempt_id="att_g",
            spec_requirement="x",
        )
        s1 = await ledger.record_commit(
            db._conn, attempt_id="att_g",
            commit_sha="aaa111", parent_sha="000000",
        )
        s2 = await ledger.record_commit(
            db._conn, attempt_id="att_g",
            commit_sha="bbb222", parent_sha="aaa111",
            recovery_trigger="test_x failed",
        )
        assert s1 == 1
        assert s2 == 2

    async def test_ac5_bumps_final_commit_sha(self, db):
        await ledger.create_attempt(
            db._conn, instance_id="t1", attempt_id="att_h",
            spec_requirement="x",
        )
        await ledger.record_commit(
            db._conn, attempt_id="att_h",
            commit_sha="first_sha", parent_sha="root",
        )
        row = await ledger.get_attempt(db._conn, "att_h")
        assert row["final_commit_sha"] == "first_sha"
        # Second commit bumps the pointer
        await ledger.record_commit(
            db._conn, attempt_id="att_h",
            commit_sha="second_sha", parent_sha="first_sha",
        )
        row = await ledger.get_attempt(db._conn, "att_h")
        assert row["final_commit_sha"] == "second_sha"


# ============================================================
# AC8 — get_attempt_commits ordered
# ============================================================


class TestGetCommits:
    async def test_ac8_ordered_by_sequence(self, db):
        await ledger.create_attempt(
            db._conn, instance_id="t1", attempt_id="att_i",
            spec_requirement="x",
        )
        await ledger.record_commit(
            db._conn, attempt_id="att_i",
            commit_sha="sha_a", parent_sha="root",
        )
        await ledger.record_commit(
            db._conn, attempt_id="att_i",
            commit_sha="sha_b", parent_sha="sha_a",
        )
        commits = await ledger.get_attempt_commits(db._conn, "att_i")
        assert [c["commit_sequence"] for c in commits] == [1, 2]
        assert [c["commit_sha"] for c in commits] == ["sha_a", "sha_b"]


# ============================================================
# AC10-11 — renderer output shape
# ============================================================


class TestRenderers:
    async def test_ac10_render_recent_attempts_empty(self):
        text = ledger.render_recent_attempts([])
        assert "no improvement attempts" in text.lower()

    async def test_ac10_render_recent_attempts_lists(self, db):
        await ledger.create_attempt(
            db._conn, instance_id="t1", attempt_id="rendered",
            spec_requirement="add a feature",
        )
        attempts = await ledger.list_recent_attempts(db._conn, "t1")
        text = ledger.render_recent_attempts(attempts)
        assert "rendered" in text
        assert "add a feature" in text
        assert "running" in text  # final_state null → "running"

    async def test_ac11_render_detail_includes_all_sections(self, db):
        await ledger.create_attempt(
            db._conn, instance_id="t1", attempt_id="detail",
            spec_requirement="comprehensive test",
            primary_coding_agent="codex",
        )
        await ledger.append_event(
            db._conn, attempt_id="detail", kind="spec_iteration",
            detail="round 1 GREEN",
        )
        await ledger.record_commit(
            db._conn, attempt_id="detail",
            commit_sha="abc123", parent_sha="parent_sha",
        )
        attempt = await ledger.get_attempt(db._conn, "detail")
        commits = await ledger.get_attempt_commits(db._conn, "detail")
        events = await ledger.get_attempt_events(db._conn, "detail")
        text = ledger.render_attempt_detail(attempt, commits, events)
        assert "detail" in text
        assert "comprehensive test" in text
        assert "codex" in text
        assert "abc123" in text
        assert "spec_iteration" in text

    def test_render_detail_handles_none_attempt(self):
        text = ledger.render_attempt_detail(None, [], [])
        assert "no such attempt" in text.lower()
