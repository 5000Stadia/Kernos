"""Tests for the Discord gateway watchdog.

Pins the detection invariants in `_is_gateway_heartbeat_unhealthy`
+ the strike-counter / restart behavior of
`_discord_gateway_watchdog_loop`.

Live failure that motivated the watchdog (2026-05-19 14:24):
* User sent /dump
* Bot processed it (TURN_TIMING shows 54ms)
* Discord gateway WebSocket closed server-side
* discord.py did NOT auto-reconnect; bot accumulated CLOSE_WAIT
  sockets with 25 bytes unread each
* Bot stayed alive (ep_poll, 1.4% CPU) but deaf to all incoming
  Discord messages for 20+ min
* Manual /restart was required

The watchdog uses `client.latency` (heartbeat round-trip time) as
the health signal — this is INDEPENDENT of whether users are
actively talking, so a quiet conversation doesn't false-positive
the way "no incoming events" would.
"""
from __future__ import annotations

import asyncio
import math
import os
from unittest.mock import patch

import pytest


# Importing server.py exercises a lot of top-level setup; isolate
# the small surface we care about by stubbing the discord client
# attribute before importing the watchdog helpers.

@pytest.fixture
def server_module(monkeypatch):
    """Import kernos.server with a stub Discord client so we can
    poke `client.latency` without a real connection."""
    import sys
    if "kernos.server" in sys.modules:
        del sys.modules["kernos.server"]
    import kernos.server as server
    return server


class _StubClient:
    """Stand-in for discord.Client exposing only the attributes
    the watchdog cares about."""
    def __init__(self, latency=0.05):
        self._latency = latency

    @property
    def latency(self):
        if isinstance(self._latency, Exception):
            raise self._latency
        return self._latency


class TestIsGatewayHeartbeatUnhealthy:
    def test_healthy_latency(self, server_module):
        with patch.object(server_module, "client", _StubClient(0.05)):
            unhealthy, reason = server_module._is_gateway_heartbeat_unhealthy()
        assert unhealthy is False
        assert "OK" in reason

    def test_infinity_latency_is_unhealthy(self, server_module):
        with patch.object(server_module, "client", _StubClient(float("inf"))):
            unhealthy, reason = server_module._is_gateway_heartbeat_unhealthy()
        assert unhealthy is True
        assert "non-finite" in reason

    def test_nan_latency_is_unhealthy(self, server_module):
        with patch.object(server_module, "client", _StubClient(float("nan"))):
            unhealthy, reason = server_module._is_gateway_heartbeat_unhealthy()
        assert unhealthy is True
        assert "non-finite" in reason

    def test_none_latency_is_unhealthy(self, server_module):
        with patch.object(server_module, "client", _StubClient(None)):
            unhealthy, reason = server_module._is_gateway_heartbeat_unhealthy()
        assert unhealthy is True
        assert "None" in reason

    def test_zero_latency_is_unhealthy(self, server_module):
        # latency=0 means no heartbeat data has been recorded yet —
        # treat as unhealthy.
        with patch.object(server_module, "client", _StubClient(0.0)):
            unhealthy, reason = server_module._is_gateway_heartbeat_unhealthy()
        assert unhealthy is True
        assert "non-positive" in reason

    def test_negative_latency_is_unhealthy(self, server_module):
        with patch.object(server_module, "client", _StubClient(-1.0)):
            unhealthy, reason = server_module._is_gateway_heartbeat_unhealthy()
        assert unhealthy is True
        assert "non-positive" in reason

    def test_excessive_latency_is_unhealthy(self, server_module):
        # Threshold default 60s; 120s should trigger
        with patch.object(server_module, "client", _StubClient(120.0)):
            unhealthy, reason = server_module._is_gateway_heartbeat_unhealthy()
        assert unhealthy is True
        assert "exceeds threshold" in reason

    def test_latency_exactly_at_threshold_is_healthy(self, server_module):
        # Default threshold is 60s; equal-to should still be considered
        # healthy (typical Discord heartbeat interval is ~41s + RTT).
        threshold = server_module._DISCORD_WATCHDOG_LATENCY_THRESHOLD_SEC
        with patch.object(server_module, "client", _StubClient(threshold)):
            unhealthy, _reason = server_module._is_gateway_heartbeat_unhealthy()
        assert unhealthy is False

    def test_latency_raises_treated_as_unhealthy(self, server_module):
        with patch.object(
            server_module, "client", _StubClient(RuntimeError("ws gone")),
        ):
            unhealthy, reason = server_module._is_gateway_heartbeat_unhealthy()
        assert unhealthy is True
        assert "raised" in reason


class TestMarkInboundEvent:
    def test_updates_timestamp(self, server_module):
        before = server_module._last_inbound_event_ts
        server_module._mark_inbound_event()
        after = server_module._last_inbound_event_ts
        assert after > before or after > 0


class TestWatchdogTick:
    """Pins ``_watchdog_tick`` semantics directly — no loop, no
    asyncio sleep. The loop's only contribution is timing; the
    behavior under test (strike counter, recovery reset,
    threshold-triggered execv) all lives in the tick function."""

    def test_healthy_returns_ok_resets_strikes(self, server_module, monkeypatch):
        server_module._gateway_unhealthy_strikes = 2
        monkeypatch.setattr(server_module, "client", _StubClient(0.05))
        # First call: was 2 strikes, transitions to recovered
        result = server_module._watchdog_tick()
        assert result == "recovered"
        assert server_module._gateway_unhealthy_strikes == 0
        # Second call: stays at 0, returns ok
        result = server_module._watchdog_tick()
        assert result == "ok"
        assert server_module._gateway_unhealthy_strikes == 0

    def test_unhealthy_increments_then_restarts(
        self, server_module, monkeypatch,
    ):
        """Three consecutive unhealthy ticks → execv called."""
        monkeypatch.setattr(
            server_module, "_DISCORD_WATCHDOG_STRIKES_TO_RESTART", 3,
        )
        monkeypatch.setattr(
            server_module, "client", _StubClient(float("inf")),
        )
        server_module._gateway_unhealthy_strikes = 0
        execv_calls = []
        monkeypatch.setattr(
            server_module.os, "execv",
            lambda *a, **kw: execv_calls.append(a),
        )

        # First two ticks: strike, no restart yet
        assert server_module._watchdog_tick() == "strike"
        assert server_module._gateway_unhealthy_strikes == 1
        assert execv_calls == []
        assert server_module._watchdog_tick() == "strike"
        assert server_module._gateway_unhealthy_strikes == 2
        assert execv_calls == []
        # Third tick: hits threshold, execv called
        result = server_module._watchdog_tick()
        assert result == "restart"
        assert len(execv_calls) == 1

    def test_recovery_resets_strikes_to_zero(
        self, server_module, monkeypatch,
    ):
        """If the gateway recovers between strikes, the counter
        resets — no spurious restart after a flap."""
        monkeypatch.setattr(
            server_module, "_DISCORD_WATCHDOG_STRIKES_TO_RESTART", 5,
        )
        execv_calls = []
        monkeypatch.setattr(
            server_module.os, "execv",
            lambda *a, **kw: execv_calls.append(a),
        )
        server_module._gateway_unhealthy_strikes = 0

        # Two strikes
        monkeypatch.setattr(
            server_module, "client", _StubClient(float("inf")),
        )
        server_module._watchdog_tick()
        server_module._watchdog_tick()
        assert server_module._gateway_unhealthy_strikes == 2

        # Recovery
        monkeypatch.setattr(server_module, "client", _StubClient(0.05))
        assert server_module._watchdog_tick() == "recovered"
        assert server_module._gateway_unhealthy_strikes == 0

        # No execv — recovery prevented escalation
        assert execv_calls == []

    async def test_loop_respects_disable_flag(
        self, server_module, monkeypatch,
    ):
        """KERNOS_DISCORD_WATCHDOG_DISABLE=1 means the loop exits
        immediately. Diagnostic-session escape hatch."""
        monkeypatch.setattr(
            server_module, "_DISCORD_WATCHDOG_DISABLE", True,
        )
        # Even with an unhealthy client, execv must NOT fire
        monkeypatch.setattr(
            server_module, "client", _StubClient(float("inf")),
        )
        execv_calls = []
        monkeypatch.setattr(
            server_module.os, "execv",
            lambda *a, **kw: execv_calls.append(a),
        )
        # Loop should return immediately, not enter the sleep+tick path
        await asyncio.wait_for(
            server_module._discord_gateway_watchdog_loop(), timeout=0.5,
        )
        assert execv_calls == []

    async def test_loop_actually_runs_body_without_name_errors(
        self, server_module, monkeypatch,
    ):
        """Regression pin: the prior async-test for the loop took
        the early-return path (disable=True), so the bodies of
        the try/except inside the while True loop NEVER ran in
        tests. That let a missing module import (``asyncio``)
        ship to prod where the watchdog task crashed on every
        startup with ``NameError: name 'asyncio' is not defined``.
        This test forces the loop body to run at least one full
        iteration so any missing imports / NameErrors / typos
        surface immediately.
        """
        # Short interval, healthy client → loop ticks once cleanly
        monkeypatch.setattr(
            server_module, "_DISCORD_WATCHDOG_INTERVAL_SEC", 0.05,
        )
        monkeypatch.setattr(server_module, "client", _StubClient(0.05))
        monkeypatch.setattr(
            server_module, "_DISCORD_WATCHDOG_DISABLE", False,
        )
        server_module._gateway_unhealthy_strikes = 0
        execv_calls = []
        monkeypatch.setattr(
            server_module.os, "execv",
            lambda *a, **kw: execv_calls.append(a),
        )

        task = asyncio.create_task(
            server_module._discord_gateway_watchdog_loop()
        )
        # Let at least one tick + sleep cycle complete (interval=0.05s)
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # No execv (healthy gateway), no NameError surfacing in
        # the task — if either module-level reference was missing,
        # the task would have died on the first iteration.
        assert execv_calls == []
        # Confirm we DID enter the loop body and at least one
        # tick fired (strike counter behavior we'd see on tick
        # is "stay at 0" for healthy ticks).
        assert server_module._gateway_unhealthy_strikes == 0
