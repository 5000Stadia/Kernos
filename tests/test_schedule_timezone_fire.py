"""v1 self-test Test 6 (live): a one-hour reminder fired instantly and vanished.

Root cause: schedule extraction asks the model for `when` in LOCAL wall-clock
with no offset (a naive string like "2026-06-08T11:58:00"). The trigger evaluator
(`get_due`) treats a naive `next_fire_at` as UTC. So "11:58 AM Pacific" (~1h out)
was stored naive, re-read as 11:58 UTC (~7h in the PAST), fired + marked completed
immediately, and disappeared from `manage_schedule list` the moment it was created.

The fix wires the long-existing `interpret_local_iso_as_utc` helper into the
extraction seam: the local wall-clock is converted to UTC before storage, so the
whole pipeline stores and compares one consistent zone.
"""
import json
from datetime import datetime

from kernos.kernel.scheduler import handle_manage_schedule, TriggerStore


class _FakeReasoning:
    """Stands in for the extraction model — returns a LOCAL naive `when`."""

    def __init__(self, when: str):
        self._when = when

    async def complete_simple(self, **kwargs):
        return json.dumps({
            "action_type": "notify", "when": self._when, "message": "stretch",
            "recurrence": "", "delivery_class": "stage", "notify_via": "",
            "tool_name": "", "tool_args": "", "condition_type": "time",
            "event_source": "", "event_filter": "", "event_lead_minutes": 0,
        })


async def test_local_when_is_stored_utc_and_not_instantly_due(tmp_path):
    store = TriggerStore(str(tmp_path))
    # 2026-06-08 is PDT (UTC-7): 11:58 local → 18:58 UTC.
    res = await handle_manage_schedule(
        store, "inst", "mem", "space", "create",
        description="remind me to stretch in an hour",
        reasoning_service=_FakeReasoning("2026-06-08T11:58:00"),
        user_timezone="America/Los_Angeles",
    )

    triggers = await store.list_all("inst")
    assert len(triggers) == 1
    nfa = triggers[0].next_fire_at
    dt = datetime.fromisoformat(nfa)
    assert dt.tzinfo is not None                       # stored tz-aware
    assert dt.utcoffset().total_seconds() == 0         # ...in UTC
    assert (dt.hour, dt.minute) == (18, 58)            # 11:58 PDT → 18:58 UTC

    # THE BUG: with a `now` before the real fire instant, the trigger must NOT
    # be due. Pre-fix it was (naive 11:58 read as UTC, already past).
    assert await store.get_due("inst", "2026-06-08T18:00:00+00:00") == []
    # ...and it IS due once the real UTC instant passes.
    assert len(await store.get_due("inst", "2026-06-08T19:00:00+00:00")) == 1

    # Status stays active until it fires → visible in list right after create.
    assert triggers[0].status == "active"
    # Receipt echoes the LOCAL wall-clock the user asked for, not raw UTC.
    assert "11:58" in res


async def test_no_timezone_falls_back_to_utc_naive(tmp_path):
    # No user_timezone → can't localize; store the wall-clock as-is (UTC).
    store = TriggerStore(str(tmp_path))
    await handle_manage_schedule(
        store, "inst", "mem", "space", "create",
        description="remind me",
        reasoning_service=_FakeReasoning("2026-06-08T18:58:00"),
        user_timezone="",
    )
    nfa = (await store.list_all("inst"))[0].next_fire_at
    dt = datetime.fromisoformat(nfa.replace(" ", "T"))
    # 18:58 treated as UTC either way → due after 19:00, not before.
    assert await store.get_due("inst", "2026-06-08T18:00:00+00:00") == []
    assert len(await store.get_due("inst", "2026-06-08T19:00:00+00:00")) == 1
