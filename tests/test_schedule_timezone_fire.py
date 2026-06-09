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

    # `list` must render the SAME local wall-clock as the create receipt — not
    # the raw UTC it's stored as (Codex P2).
    listing = await handle_manage_schedule(
        store, "inst", "mem", "space", "list",
        reasoning_service=_FakeReasoning(""), user_timezone="America/Los_Angeles",
    )
    assert "11:58" in listing
    assert "18:58" not in listing


async def test_invalid_or_missing_tz_falls_back_to_server_local(tmp_path, monkeypatch):
    # The profile timezone can be empty OR a non-IANA abbreviation like "PDT"
    # (ZoneInfo rejects it). Either must fall back to the SERVER's local zone so
    # the naive wall-clock is converted to UTC — never left naive (which get_due
    # would misread as UTC → instant fire). Pin server local to a fixed +00:00
    # so the assertion is deterministic regardless of CI's TZ.
    import os, time as _time
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(_time, "tzset"):
        _time.tzset()
    store = TriggerStore(str(tmp_path))
    for bad_tz in ("", "PDT"):
        st = TriggerStore(str(tmp_path / bad_tz.replace("", "empty") or "x"))
        await handle_manage_schedule(
            st, "inst", "mem", "space", "create",
            description="remind me",
            reasoning_service=_FakeReasoning("2026-06-08T18:58:00"),
            user_timezone=bad_tz,
        )
        nfa = (await st.list_all("inst"))[0].next_fire_at
        dt = datetime.fromisoformat(nfa)
        assert dt.tzinfo is not None                    # always tz-aware, never naive
        # Server local pinned to UTC → 18:58 local == 18:58 UTC.
        assert await st.get_due("inst", "2026-06-08T18:00:00+00:00") == []
        assert len(await st.get_due("inst", "2026-06-08T19:00:00+00:00")) == 1


def test_recurring_cron_honors_local_timezone():
    # "every morning at 8am" = 0 8 * * * is LOCAL intent. Anchored in
    # America/Los_Angeles (PDT, UTC-7), 8am local → 15:00 UTC — NOT 8am UTC.
    from kernos.kernel.scheduler import compute_next_fire
    after = "2026-06-09T00:00:00+00:00"  # = 2026-06-08 17:00 PDT
    local = compute_next_fire("0 8 * * *", after, tz_name="America/Los_Angeles")
    dt = datetime.fromisoformat(local)
    assert dt.astimezone(__import__("datetime").timezone.utc).hour == 15  # 8am PDT
    # No tz → evaluated in UTC (unchanged legacy behavior) → 8am UTC.
    utc = compute_next_fire("0 8 * * *", after)
    assert datetime.fromisoformat(utc).hour == 8


async def test_recurring_trigger_stores_tz_and_reschedules_local(tmp_path):
    # A recurring create captures the tz on the Trigger and the stored next_fire
    # is the LOCAL 8am, in UTC. (Uses the real handle_manage_schedule create.)
    import json as _json
    from kernos.kernel.scheduler import TriggerStore, handle_manage_schedule

    class _Recur:
        async def complete_simple(self, **kw):
            return _json.dumps({
                "action_type": "notify", "when": "", "message": "standup",
                "recurrence": "0 8 * * *", "delivery_class": "stage",
                "notify_via": "", "tool_name": "", "tool_args": "",
                "condition_type": "time", "event_source": "", "event_filter": "",
                "event_lead_minutes": 0,
            })

    store = TriggerStore(str(tmp_path))
    await handle_manage_schedule(
        store, "inst", "mem", "space", "create",
        description="every morning at 8am tell me standup",
        reasoning_service=_Recur(), user_timezone="America/Los_Angeles",
    )
    t = (await store.list_all("inst"))[0]
    assert t.timezone == "America/Los_Angeles"
    assert t.recurrence == "0 8 * * *"
    # Assert the LOCAL hour (8am), not a fixed UTC hour — this uses the real
    # `utc_now()`, so the UTC offset is 15 in PDT / 16 in PST. Converting back to
    # the user's zone is DST-independent (Codex P2).
    from zoneinfo import ZoneInfo
    nfa = datetime.fromisoformat(t.next_fire_at)
    assert nfa.astimezone(ZoneInfo("America/Los_Angeles")).hour == 8
