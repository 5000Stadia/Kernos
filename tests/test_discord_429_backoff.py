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


@pytest.fixture(autouse=True)
def _reset_discord_pause_state():
    """The cool-off state lives at module level on kernos.server.
    Reset between tests so one test's 429 doesn't activate the
    pause for the next test's helper call."""
    import kernos.server as srv
    srv._discord_pause_until = 0.0
    srv._discord_429_streak = 0
    yield
    srv._discord_pause_until = 0.0
    srv._discord_429_streak = 0


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


@pytest.mark.asyncio
async def test_begin_typing_safely_returns_ctx_on_success():
    """When typing.__aenter__ succeeds, the helper returns the
    context manager so the caller can __aexit__ later."""
    from unittest.mock import AsyncMock
    from kernos.server import _begin_typing_safely

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=None)
    channel = MagicMock()
    channel.typing = MagicMock(return_value=ctx)

    result = await _begin_typing_safely(channel)
    assert result is ctx
    ctx.__aenter__.assert_awaited_once()


@pytest.mark.asyncio
async def test_begin_typing_safely_returns_none_on_429():
    """Typing 429 → returns None (caller proceeds without indicator).
    The previous shape let typing's __aenter__ exception kill the
    whole turn before handler.process ran."""
    import discord
    from unittest.mock import AsyncMock
    from kernos.server import _begin_typing_safely

    err = discord.HTTPException.__new__(discord.HTTPException)
    err.status = 429
    err.code = 40062
    err.text = "rate limited"

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(side_effect=err)
    channel = MagicMock()
    channel.typing = MagicMock(return_value=ctx)

    result = await _begin_typing_safely(channel)
    assert result is None


@pytest.mark.asyncio
async def test_begin_typing_safely_reraises_non_429():
    """Non-429 errors (e.g., 5xx, network) re-raise so the existing
    error path catches them."""
    import discord
    from unittest.mock import AsyncMock
    from kernos.server import _begin_typing_safely

    err = discord.HTTPException.__new__(discord.HTTPException)
    err.status = 500
    err.code = 0
    err.text = "internal server error"

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(side_effect=err)
    channel = MagicMock()
    channel.typing = MagicMock(return_value=ctx)

    with pytest.raises(discord.HTTPException):
        await _begin_typing_safely(channel)


@pytest.mark.asyncio
async def test_send_safely_returns_true_on_success():
    from unittest.mock import AsyncMock
    from kernos.server import _send_safely

    channel = MagicMock()
    channel.send = AsyncMock(return_value=None)
    assert await _send_safely(channel, "hello") is True


@pytest.mark.asyncio
async def test_send_safely_returns_false_on_429():
    """Send 429 → returns False so the chunking loop in on_message
    can stop early. Response is already in conv-log on disk; further
    sends to the same channel would also fail."""
    import discord
    from unittest.mock import AsyncMock
    from kernos.server import _send_safely

    err = discord.HTTPException.__new__(discord.HTTPException)
    err.status = 429
    err.code = 40062
    err.text = "rate limited"

    channel = MagicMock()
    channel.send = AsyncMock(side_effect=err)
    assert await _send_safely(channel, "hello") is False


@pytest.mark.asyncio
async def test_send_safely_reraises_non_429():
    import discord
    from unittest.mock import AsyncMock
    from kernos.server import _send_safely

    err = discord.HTTPException.__new__(discord.HTTPException)
    err.status = 500
    err.code = 0
    err.text = "internal server error"

    channel = MagicMock()
    channel.send = AsyncMock(side_effect=err)
    with pytest.raises(discord.HTTPException):
        await _send_safely(channel, "hello")


def test_pause_schedule_escalates():
    """Cool-off schedule escalates: 5m → 30m → 2h → 6h. Subsequent
    429 observations extend the pause."""
    from kernos.server import _DISCORD_PAUSE_SCHEDULE_SEC
    assert _DISCORD_PAUSE_SCHEDULE_SEC == [300, 1800, 7200, 21600]


def test_register_429_activates_pause(monkeypatch):
    """First _register_discord_429 sets _is_discord_paused True for
    the schedule's first duration (5 min)."""
    import kernos.server as srv

    # Reset module state for a clean test.
    monkeypatch.setattr(srv, "_discord_pause_until", 0.0, raising=False)
    monkeypatch.setattr(srv, "_discord_429_streak", 0, raising=False)

    assert srv._is_discord_paused() is False
    srv._register_discord_429("test")
    assert srv._is_discord_paused() is True
    # Streak incremented from 0 → 1.
    assert srv._discord_429_streak == 1


def test_register_429_streak_escalates(monkeypatch):
    """Consecutive 429s escalate through the schedule. After the last
    entry the duration stays at the maximum (6h)."""
    import kernos.server as srv

    monkeypatch.setattr(srv, "_discord_pause_until", 0.0, raising=False)
    monkeypatch.setattr(srv, "_discord_429_streak", 0, raising=False)

    durations: list[int] = []
    for _ in range(6):
        srv._register_discord_429("test")
        durations.append(srv._seconds_until_resume())

    # Each call should pick a duration from the schedule, capped at
    # the last entry once the streak exceeds schedule length.
    expected = srv._DISCORD_PAUSE_SCHEDULE_SEC + [
        srv._DISCORD_PAUSE_SCHEDULE_SEC[-1]
    ] * 2
    # Allow ±1s slop from time.time() between observations.
    for got, exp in zip(durations, expected):
        assert abs(got - exp) <= 2, f"got {got}, expected ~{exp}"


def test_register_call_succeeded_resets_streak(monkeypatch):
    """A successful Discord call resets the consecutive-429 streak
    so the next 429 starts at the schedule's first duration again."""
    import kernos.server as srv

    monkeypatch.setattr(srv, "_discord_pause_until", 0.0, raising=False)
    monkeypatch.setattr(srv, "_discord_429_streak", 3, raising=False)

    srv._register_discord_call_succeeded()
    assert srv._discord_429_streak == 0


@pytest.mark.asyncio
async def test_begin_typing_safely_skips_when_paused(monkeypatch):
    """When the global cool-off is active, _begin_typing_safely
    returns None WITHOUT calling channel.typing() — preventing
    discord.py's internal 5x retry from firing."""
    from unittest.mock import AsyncMock
    import kernos.server as srv

    # Activate pause state.
    monkeypatch.setattr(
        srv, "_discord_pause_until", _time_module.time() + 300.0,
        raising=False,
    )

    channel = MagicMock()
    channel.typing = MagicMock()
    result = await srv._begin_typing_safely(channel)
    assert result is None
    channel.typing.assert_not_called()


@pytest.mark.asyncio
async def test_send_safely_skips_when_paused(monkeypatch):
    """When the global cool-off is active, _send_safely returns False
    WITHOUT calling channel.send() and logs the response content for
    operator recovery."""
    from unittest.mock import AsyncMock
    import kernos.server as srv

    monkeypatch.setattr(
        srv, "_discord_pause_until", _time_module.time() + 300.0,
        raising=False,
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    result = await srv._send_safely(channel, "hello")
    assert result is False
    channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_typing_429_activates_pause(monkeypatch):
    """A typing 429 activates the global cool-off so subsequent
    on_message calls skip the API entirely."""
    import discord
    from unittest.mock import AsyncMock
    import kernos.server as srv

    monkeypatch.setattr(srv, "_discord_pause_until", 0.0, raising=False)
    monkeypatch.setattr(srv, "_discord_429_streak", 0, raising=False)

    err = discord.HTTPException.__new__(discord.HTTPException)
    err.status = 429
    err.code = 40062
    err.text = "rate limited"

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(side_effect=err)
    channel = MagicMock()
    channel.typing = MagicMock(return_value=ctx)

    result = await srv._begin_typing_safely(channel)
    assert result is None
    assert srv._is_discord_paused() is True
    assert srv._discord_429_streak == 1


import time as _time_module


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
