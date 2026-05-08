"""Pin tests for DISCORD-429-SMART-BACKOFF (2026-05-08).

Verifies:
  * The exponential backoff schedule is the documented values.
  * The duration formatter produces operator-readable strings.
  * The wrapper catches HTTPException(429) and sleeps before retrying.
  * Non-429 exceptions re-raise unchanged.
  * After the schedule exhausts, raises with operator-actionable logging.

The wrapper itself isn't async (client.run is blocking), so tests use
plain sync patterns.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_schedule_uses_exponential_minutes_to_hours():
    """Schedule reflects observed Cloudflare-flag recovery times:
    1m → 5m → 30m → 1h → 4h. Short retries compound the abuse;
    this schedule respects how long the flag actually persists."""
    from kernos.server import _DISCORD_429_BACKOFF_SCHEDULE
    assert _DISCORD_429_BACKOFF_SCHEDULE == [60, 300, 1800, 3600, 14400]


def test_format_duration_seconds():
    from kernos.server import _format_429_wait_duration
    assert _format_429_wait_duration(1) == "1 second"
    assert _format_429_wait_duration(30) == "30 seconds"


def test_format_duration_minutes():
    from kernos.server import _format_429_wait_duration
    assert _format_429_wait_duration(60) == "1 minute"
    assert _format_429_wait_duration(300) == "5 minutes"
    assert _format_429_wait_duration(1800) == "30 minutes"


def test_format_duration_hours():
    from kernos.server import _format_429_wait_duration
    assert _format_429_wait_duration(3600) == "1 hour"
    assert _format_429_wait_duration(7200) == "2 hours"
    assert _format_429_wait_duration(14400) == "4 hours"


def test_format_duration_fractional_hours():
    """1.5 hours surfaces as ``1.5 hours``, not ``1 hour 30 minutes``.
    The formatter is chosen for compact operator-readable output, not
    grammatical correctness."""
    from kernos.server import _format_429_wait_duration
    assert _format_429_wait_duration(5400) == "1.5 hours"


def test_wrapper_returns_on_clean_run(monkeypatch):
    """When client.run completes without exception (graceful shutdown
    path), the wrapper returns without retrying."""
    from kernos.server import _run_with_429_smart_backoff
    client = MagicMock()
    client.run = MagicMock(return_value=None)
    _run_with_429_smart_backoff(client, "fake-token")
    assert client.run.call_count == 1


def test_wrapper_reraises_non_429_exceptions(monkeypatch):
    """Non-429 errors (PrivilegedIntentsRequired, LoginFailure, 5xx)
    re-raise unchanged so the existing friendly remediation handlers
    in __main__ can produce their messages."""
    import discord
    from kernos.server import _run_with_429_smart_backoff

    err = discord.HTTPException.__new__(discord.HTTPException)
    err.status = 500
    err.code = 0
    err.text = "internal server error"

    client = MagicMock()
    client.run = MagicMock(side_effect=err)

    with pytest.raises(discord.HTTPException) as exc_info:
        _run_with_429_smart_backoff(client, "fake-token")
    assert exc_info.value.status == 500
    # No retry on non-429.
    assert client.run.call_count == 1


def test_wrapper_retries_on_429_then_succeeds(monkeypatch):
    """First call 429s → wrapper sleeps per schedule → second call
    succeeds → wrapper returns. The sleep call confirms the schedule
    is being honored (we patch sleep to capture the duration)."""
    import discord
    from kernos.server import _run_with_429_smart_backoff

    err = discord.HTTPException.__new__(discord.HTTPException)
    err.status = 429
    err.code = 40062
    err.text = "rate limited"

    call_count = {"n": 0}

    def fake_run(_token):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise err
        return None  # second call succeeds

    # The wrapper does ``import time as _time`` inside the function.
    # That binding resolves to the shared time module; patching the
    # module attribute reaches the wrapper's local alias.
    import time
    sleeps: list[int] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    client = MagicMock()
    client.run = fake_run

    _run_with_429_smart_backoff(client, "fake-token")
    assert call_count["n"] == 2
    assert sleeps == [60]  # first schedule entry


def test_wrapper_exhausts_schedule_then_raises(monkeypatch):
    """All schedule entries fail → wrapper raises the final 429 with
    operator-actionable logging. The bot exits; start.sh's exit
    handler surfaces the message."""
    import discord
    from kernos.server import (
        _DISCORD_429_BACKOFF_SCHEDULE,
        _run_with_429_smart_backoff,
    )

    err = discord.HTTPException.__new__(discord.HTTPException)
    err.status = 429
    err.code = 40062
    err.text = "rate limited"

    sleeps: list[int] = []
    import time
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    client = MagicMock()
    client.run = MagicMock(side_effect=err)

    with pytest.raises(discord.HTTPException):
        _run_with_429_smart_backoff(client, "fake-token")

    # Each schedule entry consumed = one sleep + one retry.
    # Final attempt exhausts the schedule and raises without sleeping.
    assert sleeps == _DISCORD_429_BACKOFF_SCHEDULE
    # Initial run + one per schedule entry = N+1 total calls.
    assert client.run.call_count == len(_DISCORD_429_BACKOFF_SCHEDULE) + 1
