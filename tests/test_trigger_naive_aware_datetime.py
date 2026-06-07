"""v1 self-test bug #8: a scheduled trigger whose next_fire_at is tz-naive
(agent-supplied `when` often omits tz) could not be compared with the tz-aware
`now`, raising "can't compare offset-naive and offset-aware datetimes" every
trigger-eval tick — killing the whole pass so no reminder ever fired. get_due
now tz-normalizes (naive→UTC) before comparing.
"""
import pytest

from kernos.kernel.scheduler import TriggerStore


def _store(monkeypatch, rows):
    s = TriggerStore("/tmp/sched-test")
    monkeypatch.setattr(s, "_read", lambda instance_id: rows)
    return s


def _row(nfa: str):
    return {"status": "active", "fire_count": 0, "recurrence": "",
            "next_fire_at": nfa, "trigger_id": "t1", "instance_id": "i1",
            "condition_type": "time", "action_type": "reminder"}


async def test_naive_nfa_vs_aware_now_does_not_raise_and_is_due(monkeypatch):
    # naive past fire time, aware now → due, no TypeError
    s = _store(monkeypatch, [_row("2026-06-07T00:00:00")])  # naive, in the past
    due = await s.get_due("i1", "2026-06-07T01:00:00+00:00")  # aware now
    assert len(due) == 1


async def test_naive_future_not_due(monkeypatch):
    s = _store(monkeypatch, [_row("2026-06-07T05:00:00")])   # naive, future
    due = await s.get_due("i1", "2026-06-07T01:00:00+00:00")
    assert due == []


async def test_aware_both_still_works(monkeypatch):
    s = _store(monkeypatch, [_row("2026-06-07T00:00:00+00:00")])
    due = await s.get_due("i1", "2026-06-07T01:00:00+00:00")
    assert len(due) == 1
