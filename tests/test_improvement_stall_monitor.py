"""Tests for the improvement-loop stall detector (self-monitoring)."""
import datetime as dt
import sqlite3

import pytest

from kernos.kernel.improvement_loop_workflow import (
    find_stalled_improvement_attempts,
)


def _iso(minutes_ago: float) -> str:
    return (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    ).isoformat()


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "instance.db"))
    conn.executescript(
        """
        CREATE TABLE improvement_attempts (
            attempt_id TEXT PRIMARY KEY, final_state TEXT);
        CREATE TABLE improvement_attempt_events (
            attempt_id TEXT, kind TEXT, sequence INTEGER,
            timestamp TEXT, detail TEXT);
        """
    )
    # a) in-flight + stalled (last event 20 min ago)
    conn.execute("INSERT INTO improvement_attempts VALUES ('att_stall', NULL)")
    conn.execute(
        "INSERT INTO improvement_attempt_events VALUES "
        "('att_stall','attempt_origin',0,?,?)",
        (_iso(25), '{"origin_space_id":"sp1","origin_member_id":"mem1"}'),
    )
    conn.execute(
        "INSERT INTO improvement_attempt_events VALUES "
        "('att_stall','impl_iteration',1,?,'round 1')",
        (_iso(20),),
    )
    # b) in-flight + fresh (last event 1 min ago) -> not stalled
    conn.execute("INSERT INTO improvement_attempts VALUES ('att_fresh', NULL)")
    conn.execute(
        "INSERT INTO improvement_attempt_events VALUES "
        "('att_fresh','spec_iteration',0,?,'round 1')",
        (_iso(1),),
    )
    # c) terminal (old) -> ignored even though stale
    conn.execute(
        "INSERT INTO improvement_attempts VALUES "
        "('att_done','aborted_consult_failure')"
    )
    conn.execute(
        "INSERT INTO improvement_attempt_events VALUES "
        "('att_done','attempt_failed',0,?,'x')",
        (_iso(99),),
    )
    conn.commit()
    conn.close()
    return tmp_path


async def test_detects_only_stalled_inflight(db):
    stalled = await find_stalled_improvement_attempts(
        str(db), stall_threshold_sec=720,  # 12 min
    )
    assert [s["attempt_id"] for s in stalled] == ["att_stall"]
    s = stalled[0]
    assert s["last_kind"] == "impl_iteration"
    assert s["origin_space_id"] == "sp1"
    assert s["origin_member_id"] == "mem1"
    assert s["stalled_sec"] >= 720


async def test_threshold_respected(db):
    # 30-min threshold -> the 20-min stall isn't surfaced yet.
    stalled = await find_stalled_improvement_attempts(
        str(db), stall_threshold_sec=1800,
    )
    assert stalled == []


async def test_missing_db_is_safe(tmp_path):
    assert await find_stalled_improvement_attempts(str(tmp_path)) == []
