"""Tests for GATEWAY-HEALTH-OBSERVER-V1.

Pins the four detectors + the catalog-integration contract. The
observer emits gateway/dispatch-layer FrictionSignals into the
SAME FrictionPatternStore the per-turn FrictionObserver uses (no
parallel catalog) — that's the spec's load-bearing invariant.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any

import pytest

from kernos.kernel.friction import FrictionSignal
from kernos.kernel.gateway_health import (
    GatewayHealthObserver,
    _MessageCreateCounter,
)


# ===========================================================================
# _MessageCreateCounter — bounded windowed counter
# ===========================================================================


class TestMessageCreateCounter:
    def test_records_and_counts(self):
        c = _MessageCreateCounter(window_sec=600)
        now = time.time()
        c.record(now - 100)
        c.record(now - 50)
        c.record(now)
        assert c.count_in_window(now) == 3

    def test_window_filters_old_events(self):
        c = _MessageCreateCounter(window_sec=60)
        now = time.time()
        c.record(now - 120)  # outside window
        c.record(now - 30)
        c.record(now)
        assert c.count_in_window(now) == 2

    def test_maxlen_bounded(self):
        c = _MessageCreateCounter(window_sec=60)
        for _ in range(20_000):
            c.record(time.time())
        # deque maxlen=10000 caps memory
        assert len(c._events) <= 10_000


# ===========================================================================
# Detector pins
# ===========================================================================


class _CaptureStore:
    """Stand-in FrictionPatternStore that records calls without
    needing the real sqlite-backed schema."""
    def __init__(self):
        self.occurrences: list[tuple] = []
        self.recurrences: list[tuple] = []
        self.schema_ensured = False

    async def ensure_schema(self, data_dir):
        self.schema_ensured = True

    async def list_patterns(self, instance_id):
        # Return fake patterns that match our gateway signal_types.
        from kernos.kernel.friction_patterns import (
            FrictionPattern,
        )
        return [
            FrictionPattern(
                instance_id=instance_id,
                pattern_id="discord-heartbeat-blocked",
                description="x",
                signal_type_keys=("DISCORD_HEARTBEAT_BLOCKED",),
                display_name="x",
                lifecycle_state="active",
                reactivation_threshold=3,
            ),
            FrictionPattern(
                instance_id=instance_id,
                pattern_id="discord-gateway-deaf",
                description="x",
                signal_type_keys=("DISCORD_GATEWAY_DEAF",),
                display_name="x",
                lifecycle_state="active",
                reactivation_threshold=2,
            ),
            FrictionPattern(
                instance_id=instance_id,
                pattern_id="discord-connection-pool-leak",
                description="x",
                signal_type_keys=("CONNECTION_POOL_LEAK",),
                display_name="x",
                lifecycle_state="active",
                reactivation_threshold=5,
            ),
            FrictionPattern(
                instance_id=instance_id,
                pattern_id="space-runner-stuck",
                description="x",
                signal_type_keys=("SPACE_RUNNER_STUCK",),
                display_name="x",
                lifecycle_state="active",
                reactivation_threshold=2,
            ),
        ]

    async def record_occurrence(self, **kwargs):
        self.occurrences.append(kwargs)

    async def record_recurrence(self, **kwargs):
        self.recurrences.append(kwargs)


def _make_observer(
    *, latency=0.05, last_inbound=0.0, mc_counter=None,
    runner_inspector=None, store=None, tmp_path=None,
):
    return GatewayHealthObserver(
        instance_id="test_inst",
        data_dir=str(tmp_path) if tmp_path else "./data",
        pattern_store=store or _CaptureStore(),
        latency_provider=lambda: latency,
        inbound_event_ts_provider=lambda: last_inbound,
        message_create_counter=mc_counter,
        runner_inspector=runner_inspector,
    )


class TestDetectHeartbeatBlocked:
    def test_healthy_latency_returns_none(self, tmp_path):
        obs = _make_observer(latency=0.05, tmp_path=tmp_path)
        assert obs._detect_heartbeat_blocked() is None

    def test_inf_latency_emits_signal(self, tmp_path):
        obs = _make_observer(latency=float("inf"), tmp_path=tmp_path)
        signal = obs._detect_heartbeat_blocked()
        assert signal is not None
        assert signal.signal_type == "DISCORD_HEARTBEAT_BLOCKED"
        assert "non-finite" in signal.description

    def test_nan_latency_emits_signal(self, tmp_path):
        obs = _make_observer(latency=float("nan"), tmp_path=tmp_path)
        signal = obs._detect_heartbeat_blocked()
        assert signal is not None
        assert signal.signal_type == "DISCORD_HEARTBEAT_BLOCKED"

    def test_zero_latency_emits_signal(self, tmp_path):
        obs = _make_observer(latency=0.0, tmp_path=tmp_path)
        signal = obs._detect_heartbeat_blocked()
        assert signal is not None

    def test_excessive_latency_emits_signal(self, tmp_path):
        obs = _make_observer(latency=120.0, tmp_path=tmp_path)
        signal = obs._detect_heartbeat_blocked()
        assert signal is not None
        assert "exceeds threshold" in signal.description

    def test_none_latency_emits_signal(self, tmp_path):
        obs = _make_observer(latency=None, tmp_path=tmp_path)
        signal = obs._detect_heartbeat_blocked()
        assert signal is not None


class TestDetectGatewayDeaf:
    def test_no_mc_counter_returns_none(self, tmp_path):
        obs = _make_observer(mc_counter=None, tmp_path=tmp_path)
        assert obs._detect_gateway_deaf(time.time()) is None

    def test_zero_message_creates_returns_none(self, tmp_path):
        """No MESSAGE_CREATE in window = can't tell deaf from idle."""
        counter = _MessageCreateCounter(window_sec=600)
        obs = _make_observer(mc_counter=counter, tmp_path=tmp_path)
        assert obs._detect_gateway_deaf(time.time()) is None

    def test_bot_just_started_no_false_positive(self, tmp_path):
        """If on_message has never fired (timestamp=0), don't emit
        — give the bot time to receive its first event."""
        counter = _MessageCreateCounter(window_sec=600)
        counter.record(time.time())
        obs = _make_observer(
            mc_counter=counter, last_inbound=0.0, tmp_path=tmp_path,
        )
        assert obs._detect_gateway_deaf(time.time()) is None

    def test_recent_on_message_returns_none(self, tmp_path):
        """MESSAGE_CREATE seen AND on_message fired recently → healthy."""
        now = time.time()
        counter = _MessageCreateCounter(window_sec=600)
        counter.record(now)
        obs = _make_observer(
            mc_counter=counter, last_inbound=now - 60, tmp_path=tmp_path,
        )
        assert obs._detect_gateway_deaf(now) is None

    def test_deaf_pattern_emits_signal(self, tmp_path):
        """MESSAGE_CREATE seen but on_message stale → deaf gateway."""
        now = time.time()
        counter = _MessageCreateCounter(window_sec=600)
        counter.record(now - 100)
        counter.record(now - 50)
        # Last on_message was 1 hour ago, way beyond the 600s window
        obs = _make_observer(
            mc_counter=counter, last_inbound=now - 3600, tmp_path=tmp_path,
        )
        signal = obs._detect_gateway_deaf(now)
        assert signal is not None
        assert signal.signal_type == "DISCORD_GATEWAY_DEAF"
        assert "MESSAGE_CREATE" in signal.description


class TestDetectRunnerStuck:
    def test_no_inspector_returns_none(self, tmp_path):
        obs = _make_observer(runner_inspector=None, tmp_path=tmp_path)
        assert obs._detect_runner_stuck(time.time()) is None

    def test_empty_runners_returns_none(self, tmp_path):
        obs = _make_observer(
            runner_inspector=lambda: [], tmp_path=tmp_path,
        )
        assert obs._detect_runner_stuck(time.time()) is None

    def test_fresh_mailbox_returns_none(self, tmp_path):
        now = time.time()
        obs = _make_observer(
            runner_inspector=lambda: [("space_a", now - 10)],
            tmp_path=tmp_path,
        )
        assert obs._detect_runner_stuck(now) is None

    def test_stuck_mailbox_emits_signal(self, tmp_path):
        """Mailbox item older than threshold → stuck runner."""
        now = time.time()
        obs = _make_observer(
            runner_inspector=lambda: [("space_stuck", now - 1000)],
            tmp_path=tmp_path,
        )
        signal = obs._detect_runner_stuck(now)
        assert signal is not None
        assert signal.signal_type == "SPACE_RUNNER_STUCK"
        assert "space_stuck" in signal.description


class TestDetectPoolLeak:
    def test_low_close_wait_returns_none(self, monkeypatch, tmp_path):
        """When CLOSE_WAIT count is below threshold, no signal."""
        obs = _make_observer(tmp_path=tmp_path)

        class _FakeConn:
            status = "ESTABLISHED"

        class _FakeProc:
            def net_connections(self, kind):
                return [_FakeConn() for _ in range(10)]

        import psutil
        monkeypatch.setattr(psutil, "Process", lambda pid: _FakeProc())
        monkeypatch.setattr(psutil, "CONN_CLOSE_WAIT", "CLOSE_WAIT")
        assert obs._detect_pool_leak() is None

    def test_high_close_wait_emits_signal(self, monkeypatch, tmp_path):
        obs = _make_observer(tmp_path=tmp_path)
        import psutil

        class _FakeConn:
            status = "CLOSE_WAIT"

        class _FakeProc:
            def net_connections(self, kind):
                return [_FakeConn() for _ in range(50)]

        monkeypatch.setattr(psutil, "Process", lambda pid: _FakeProc())
        monkeypatch.setattr(psutil, "CONN_CLOSE_WAIT", "CLOSE_WAIT")
        signal = obs._detect_pool_leak()
        assert signal is not None
        assert signal.signal_type == "CONNECTION_POOL_LEAK"
        assert "50" in signal.description


# ===========================================================================
# Catalog integration — load-bearing invariant per spec
# ===========================================================================


class TestCatalogIntegration:
    async def test_emitted_signal_records_occurrence(self, tmp_path):
        """Spec invariant: GatewayHealthObserver feeds the SAME
        FrictionPatternStore the per-turn FrictionObserver uses,
        via the same _classify_and_record-style path. Verify by
        emitting a signal and confirming record_occurrence fires."""
        store = _CaptureStore()
        obs = _make_observer(store=store, tmp_path=tmp_path)
        signal = FrictionSignal(
            signal_type="DISCORD_HEARTBEAT_BLOCKED",
            description="test heartbeat unhealthy",
            evidence=["evidence line"],
            context={"space": "", "member_id": ""},
        )
        await obs._record_signal(signal)
        assert store.schema_ensured
        assert len(store.occurrences) == 1
        rec = store.occurrences[0]
        assert rec["pattern_id"] == "discord-heartbeat-blocked"
        assert rec["instance_id"] == "test_inst"

    async def test_unclassified_signal_logs_warning(self, tmp_path, caplog):
        """If the signal_type doesn't match a seeded pattern,
        warn loudly (catches the case where someone adds a new
        detector but forgets to seed the pattern)."""
        store = _CaptureStore()
        obs = _make_observer(store=store, tmp_path=tmp_path)
        signal = FrictionSignal(
            signal_type="UNSEEDED_GATEWAY_SIGNAL",
            description="will not match catalog",
            evidence=[],
            context={"space": "", "member_id": ""},
        )
        import logging
        with caplog.at_level(logging.WARNING):
            await obs._record_signal(signal)
        assert any(
            "UNCLASSIFIED" in r.message
            for r in caplog.records
        )
        assert store.occurrences == []


# ===========================================================================
# Background loop lifecycle
# ===========================================================================


class TestObserverLifecycle:
    async def test_start_stop_idempotent(self, tmp_path):
        obs = _make_observer(tmp_path=tmp_path)
        await obs.start()
        await obs.start()  # second start is no-op
        await obs.stop()
        await obs.stop()  # second stop is no-op

    async def test_tick_runs_all_detectors_isolated(self, tmp_path):
        """Each detector runs in its own try/except so one
        failure doesn't skip the others (pin: failure isolation)."""
        obs = _make_observer(tmp_path=tmp_path)

        # Force one detector to raise
        def broken_detector():
            raise RuntimeError("synthetic detector failure")
        obs._detect_heartbeat_blocked = broken_detector

        # The other detectors should still run; _tick shouldn't raise
        await obs._tick()


class TestInlineRemediation:
    """V1.5 inline remediation (founder request 2026-05-20): when
    discord-heartbeat-blocked fires for N consecutive ticks, the
    observer force-restarts via os.execv. Safety net for the case
    where the standalone watchdog has stopped firing.
    """

    async def test_strikes_increment_on_consecutive_unhealthy_ticks(
        self, tmp_path, monkeypatch,
    ):
        from kernos.kernel import gateway_health
        monkeypatch.setattr(
            gateway_health,
            "_RESTART_AFTER_CONSECUTIVE_HEARTBEAT_STRIKES", 10,
        )
        execv_calls = []
        monkeypatch.setattr(
            gateway_health.os, "execv",
            lambda *a, **kw: execv_calls.append(a),
        )
        obs = _make_observer(latency=float("nan"), tmp_path=tmp_path)
        for _ in range(3):
            await obs._tick()
        assert obs._consecutive_heartbeat_strikes == 3
        assert execv_calls == []

    async def test_strikes_reset_on_healthy_tick(
        self, tmp_path, monkeypatch,
    ):
        from kernos.kernel import gateway_health
        monkeypatch.setattr(
            gateway_health,
            "_RESTART_AFTER_CONSECUTIVE_HEARTBEAT_STRIKES", 10,
        )
        execv_calls = []
        monkeypatch.setattr(
            gateway_health.os, "execv",
            lambda *a, **kw: execv_calls.append(a),
        )
        # Start unhealthy
        obs = _make_observer(latency=float("nan"), tmp_path=tmp_path)
        await obs._tick()
        await obs._tick()
        assert obs._consecutive_heartbeat_strikes == 2

        # Switch to healthy — strikes must reset to 0
        obs._latency_provider = lambda: 0.05
        await obs._tick()
        assert obs._consecutive_heartbeat_strikes == 0
        assert execv_calls == []

    async def test_threshold_triggers_execv(self, tmp_path, monkeypatch):
        from kernos.kernel import gateway_health
        monkeypatch.setattr(
            gateway_health,
            "_RESTART_AFTER_CONSECUTIVE_HEARTBEAT_STRIKES", 3,
        )
        execv_calls = []
        monkeypatch.setattr(
            gateway_health.os, "execv",
            lambda *a, **kw: execv_calls.append(a),
        )
        obs = _make_observer(latency=float("nan"), tmp_path=tmp_path)
        for _ in range(3):
            await obs._tick()
        assert len(execv_calls) == 1, (
            "observer should have called execv after 3 consecutive "
            "heartbeat-unhealthy ticks"
        )

    async def test_skips_restart_when_v2_sentinel_within_window(
        self, tmp_path, monkeypatch,
    ):
        """The critical loop-prevention pin: V1.5 must respect the
        SAME sentinel V2 writes. Live-observed 2026-05-20: bot
        restart-looped every ~5 min because V1.5 had no cool-off
        while V2 did. Both now share the sentinel; restart loops
        are mathematically impossible regardless of which path
        triggered the first restart."""
        from kernos.kernel import gateway_health
        from datetime import datetime, timezone
        monkeypatch.setattr(
            gateway_health,
            "_RESTART_AFTER_CONSECUTIVE_HEARTBEAT_STRIKES", 3,
        )
        execv_calls = []
        monkeypatch.setattr(
            gateway_health.os, "execv",
            lambda *a, **kw: execv_calls.append(a),
        )

        # Seed the sentinel as if V2 just fired
        instance = "test_inst"
        sentinel_path = (
            tmp_path / "diagnostics" / "friction" / "remediation"
            / f"{instance}__discord-heartbeat-blocked.last_fired"
        )
        sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        sentinel_path.write_text(
            datetime.now(timezone.utc).isoformat(),
            encoding="utf-8",
        )

        obs = _make_observer(
            latency=float("nan"), tmp_path=tmp_path,
        )
        # Override instance_id to match the sentinel
        obs._instance_id = instance

        # 3 consecutive unhealthy ticks would normally trigger
        # restart, but sentinel says we just restarted
        for _ in range(3):
            await obs._tick()
        assert execv_calls == [], (
            "V1.5 must respect V2's sentinel cool-off; otherwise "
            "the two restart paths race each other and we restart-loop "
            "every 5 min (the 2026-05-20 live failure shape)"
        )
        # Strikes get reset on skip so we don't spam the log
        assert obs._consecutive_heartbeat_strikes == 0

    async def test_restarts_when_sentinel_aged_out(self, tmp_path, monkeypatch):
        """After the cool-off window expires, V1.5 IS allowed to
        restart — the sentinel is a window-gate, not a kill switch."""
        from kernos.kernel import gateway_health
        from datetime import datetime, timedelta, timezone
        monkeypatch.setattr(
            gateway_health,
            "_RESTART_AFTER_CONSECUTIVE_HEARTBEAT_STRIKES", 3,
        )
        monkeypatch.setenv(
            "KERNOS_DISCORD_HEARTBEAT_REMEDIATION_WINDOW_SEC", "60",
        )
        execv_calls = []
        monkeypatch.setattr(
            gateway_health.os, "execv",
            lambda *a, **kw: execv_calls.append(a),
        )

        instance = "test_inst"
        sentinel_path = (
            tmp_path / "diagnostics" / "friction" / "remediation"
            / f"{instance}__discord-heartbeat-blocked.last_fired"
        )
        sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        # Backdate the sentinel beyond the 60s window
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=90)
        sentinel_path.write_text(old_ts.isoformat(), encoding="utf-8")

        obs = _make_observer(latency=float("nan"), tmp_path=tmp_path)
        obs._instance_id = instance
        for _ in range(3):
            await obs._tick()
        assert len(execv_calls) == 1, (
            "V1.5 should fire restart once the V2 sentinel has aged "
            "past the window — sentinel is a cool-off, not a permanent block"
        )

    async def test_other_signals_do_not_count_against_heartbeat_strikes(
        self, tmp_path, monkeypatch,
    ):
        """The strike counter is heartbeat-specific. A pool-leak or
        gateway-deaf signal should NOT trigger restart, only persistent
        heartbeat-blocked does."""
        from kernos.kernel import gateway_health
        monkeypatch.setattr(
            gateway_health,
            "_RESTART_AFTER_CONSECUTIVE_HEARTBEAT_STRIKES", 3,
        )
        execv_calls = []
        monkeypatch.setattr(
            gateway_health.os, "execv",
            lambda *a, **kw: execv_calls.append(a),
        )
        # Healthy latency but high pool-leak — emits CONNECTION_POOL_LEAK
        # signal, NOT heartbeat-blocked. Strike counter stays 0.
        import psutil

        class _FakeConn:
            status = "CLOSE_WAIT"

        class _FakeProc:
            def net_connections(self, kind):
                return [_FakeConn() for _ in range(50)]

        monkeypatch.setattr(psutil, "Process", lambda pid: _FakeProc())
        monkeypatch.setattr(psutil, "CONN_CLOSE_WAIT", "CLOSE_WAIT")
        obs = _make_observer(latency=0.05, tmp_path=tmp_path)
        for _ in range(5):
            await obs._tick()
        assert obs._consecutive_heartbeat_strikes == 0
        assert execv_calls == []


# ===========================================================================
# Seed-pattern presence pins
# ===========================================================================


class TestSeedPatternsPresent:
    def test_all_four_gateway_patterns_seeded(self):
        from kernos.setup.seed_friction_patterns import _STARTER_PATTERNS
        ids = {p.pattern_id for p in _STARTER_PATTERNS}
        for required in (
            "discord-gateway-deaf",
            "space-runner-stuck",
            "discord-heartbeat-blocked",
            "discord-connection-pool-leak",
        ):
            assert required in ids, (
                f"Pattern {required!r} missing from seed catalog — "
                f"GatewayHealthObserver signals won't be classified"
            )

    def test_signal_type_to_pattern_id_mapping(self):
        """The classifier matches signal_type → pattern via the
        seed's signal_type_keys. Verify each gateway signal_type
        is reachable from a seed."""
        from kernos.setup.seed_friction_patterns import _STARTER_PATTERNS
        mapping = {}
        for p in _STARTER_PATTERNS:
            for key in p.signal_type_keys:
                mapping[key] = p.pattern_id
        for signal_type, expected_id in (
            ("DISCORD_GATEWAY_DEAF", "discord-gateway-deaf"),
            ("SPACE_RUNNER_STUCK", "space-runner-stuck"),
            ("DISCORD_HEARTBEAT_BLOCKED", "discord-heartbeat-blocked"),
            ("CONNECTION_POOL_LEAK", "discord-connection-pool-leak"),
        ):
            assert mapping.get(signal_type) == expected_id
