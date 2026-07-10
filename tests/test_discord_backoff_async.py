"""CLI-FIRST-CORE-V1 A3 — async 429 smart-backoff lifecycle pins.

The sync wrapper's tests are explicitly NOT evidence for this lifecycle
(Cx round-1 #3); these pin the new one: retry schedule, close-between-
attempts ordering, non-429 re-raise, exhaustion, and cancellation.
"""

import asyncio

import discord
import pytest

from kernos.discord_runtime import (
    format_429_wait_duration,
    run_discord_with_429_smart_backoff,
)


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "test"


def _http_exc(status: int) -> discord.HTTPException:
    return discord.HTTPException(_Resp(status), "test failure")


class FakeClient:
    """Duck-typed client recording the start/close call sequence."""

    def __init__(self, outcomes: list) -> None:
        # Each outcome: None (graceful return) or an exception to raise.
        self._outcomes = list(outcomes)
        self.calls: list[str] = []

    async def start(self, token: str) -> None:
        self.calls.append("start")
        outcome = self._outcomes.pop(0)
        if outcome is not None:
            raise outcome

    async def close(self) -> None:
        self.calls.append("close")


def _sleep_recorder(record: list):
    async def _sleep(seconds):
        record.append(seconds)
    return _sleep


async def test_graceful_return_no_retry():
    client = FakeClient([None])
    slept: list = []
    await run_discord_with_429_smart_backoff(
        client, "tok", schedule=[1, 2], sleep=_sleep_recorder(slept)
    )
    assert client.calls == ["start"]
    assert slept == []


async def test_429_closes_sleeps_schedule_then_succeeds():
    client = FakeClient([_http_exc(429), _http_exc(429), None])
    slept: list = []
    await run_discord_with_429_smart_backoff(
        client, "tok", schedule=[7, 13], sleep=_sleep_recorder(slept)
    )
    # Ordering pin: close precedes each retry's start.
    assert client.calls == ["start", "close", "start", "close", "start"]
    assert slept == [7, 13]


async def test_non_429_closes_and_reraises():
    client = FakeClient([_http_exc(401)])
    slept: list = []
    with pytest.raises(discord.HTTPException) as excinfo:
        await run_discord_with_429_smart_backoff(
            client, "tok", schedule=[7], sleep=_sleep_recorder(slept)
        )
    assert excinfo.value.status == 401
    assert client.calls == ["start", "close"]
    assert slept == []  # no backoff consumed for non-429


async def test_schedule_exhaustion_reraises_last_429(capsys):
    client = FakeClient([_http_exc(429)] * 3)
    slept: list = []
    with pytest.raises(discord.HTTPException) as excinfo:
        await run_discord_with_429_smart_backoff(
            client, "tok", schedule=[1, 2], sleep=_sleep_recorder(slept)
        )
    assert excinfo.value.status == 429
    assert slept == [1, 2]  # full schedule consumed
    # Give-up operator text reaches stderr (same remediation steps).
    err = capsys.readouterr().err
    assert "backoff schedule exhausted" in err
    assert "Reset Token" in err


async def test_cancellation_during_start_closes_and_propagates():
    client = FakeClient([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await run_discord_with_429_smart_backoff(
            client, "tok", schedule=[1], sleep=_sleep_recorder([])
        )
    assert client.calls == ["start", "close"]


async def test_cancellation_during_backoff_sleep_closes_and_propagates():
    client = FakeClient([_http_exc(429)])

    async def _cancelled_sleep(seconds):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run_discord_with_429_smart_backoff(
            client, "tok", schedule=[5], sleep=_cancelled_sleep
        )
    # close after the 429, and close again on cancel — both in shutdown lane.
    assert client.calls == ["start", "close", "close"]


def test_wait_duration_formatting_matches_sync_wrapper():
    assert format_429_wait_duration(59) == "59 seconds"
    assert format_429_wait_duration(60) == "1 minute"
    assert format_429_wait_duration(300) == "5 minutes"
    assert format_429_wait_duration(3600) == "1 hour"
    assert format_429_wait_duration(5400) == "1.5 hours"
    assert format_429_wait_duration(14400) == "4 hours"
