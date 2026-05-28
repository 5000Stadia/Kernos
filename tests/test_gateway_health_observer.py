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
    last_on_message=0.0,
    past_warmup=True,
    any_socket_event_ts=None,
):
    obs = GatewayHealthObserver(
        instance_id="test_inst",
        data_dir=str(tmp_path) if tmp_path else "./data",
        pattern_store=store or _CaptureStore(),
        latency_provider=lambda: latency,
        inbound_event_ts_provider=lambda: last_inbound,
        message_create_counter=mc_counter,
        runner_inspector=runner_inspector,
        last_on_message_provider=lambda: last_on_message,
        any_socket_event_ts_provider=(
            (lambda: any_socket_event_ts)
            if any_socket_event_ts is not None else None
        ),
    )
    if past_warmup:
        # Backdate the start marker so _uptime_sec returns a value
        # past the warm-up grace window. monotonic() is a counter
        # not a wall clock; subtracting an hour from "now" pushes
        # uptime far past any reasonable warmup default.
        import time as _time
        obs._observer_started_at = _time.monotonic() - 3600.0
    return obs


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


class TestHeartbeatLivenessCrossCheck:
    """Behavior tests for the HEARTBEAT-DETECTOR-LIVENESS-CROSSCHECK-V1
    cross-check + warm-up suppression. Covers the spec's acceptance
    criteria (scenarios 1-18). Pure-function focus where possible —
    we exercise the detector synchronously, not the running observer
    task."""

    # --- scenarios 1, 2: live traffic suppresses tracker-unreliable

    def test_scenario1_nan_with_message_create_suppressed(self, tmp_path):
        counter = _MessageCreateCounter(window_sec=600)
        counter.record(time.time())
        obs = _make_observer(
            latency=float("nan"), mc_counter=counter, tmp_path=tmp_path,
        )
        assert obs._detect_heartbeat_blocked() is None
        assert obs._heartbeat_suppression_count == 1
        assert obs._last_suppression_kind == "live_traffic"

    def test_scenario2_none_latency_with_on_message_suppressed(self, tmp_path):
        obs = _make_observer(
            latency=None, last_on_message=time.time(), tmp_path=tmp_path,
        )
        assert obs._detect_heartbeat_blocked() is None
        assert obs._last_suppression_kind == "live_traffic"

    # --- scenario 3: no liveness evidence → emit

    def test_scenario3_nan_no_traffic_no_on_message_emits(self, tmp_path):
        obs = _make_observer(latency=float("nan"), tmp_path=tmp_path)
        signal = obs._detect_heartbeat_blocked()
        assert signal is not None
        assert obs._heartbeat_suppression_count == 0

    # --- scenario 4: warm-up suppresses

    def test_scenario4_nan_within_warmup_suppressed(self, tmp_path):
        obs = _make_observer(
            latency=float("nan"), tmp_path=tmp_path, past_warmup=False,
        )
        assert obs._detect_heartbeat_blocked() is None
        assert obs._last_suppression_kind == "warmup"

    # --- scenario 5: real high latency emits even with traffic

    def test_scenario5_high_latency_with_traffic_emits(self, tmp_path):
        counter = _MessageCreateCounter(window_sec=600)
        counter.record(time.time())
        obs = _make_observer(
            latency=120.0, mc_counter=counter, tmp_path=tmp_path,
        )
        signal = obs._detect_heartbeat_blocked()
        assert signal is not None
        assert "exceeds threshold" in signal.description
        assert obs._heartbeat_suppression_count == 0

    # --- scenario 6: healthy returns None always

    def test_scenario6_healthy_returns_none(self, tmp_path):
        obs = _make_observer(latency=0.05, tmp_path=tmp_path)
        assert obs._detect_heartbeat_blocked() is None

    # --- scenarios 7-8: counter=None + on_message-only behavior

    def test_scenario7_counter_none_recent_on_message_suppressed(self, tmp_path):
        obs = _make_observer(
            latency=float("nan"),
            mc_counter=None,
            last_on_message=time.time(),
            tmp_path=tmp_path,
        )
        assert obs._detect_heartbeat_blocked() is None

    def test_scenario8_counter_none_no_on_message_emits(self, tmp_path):
        obs = _make_observer(
            latency=float("nan"),
            mc_counter=None,
            last_on_message=0.0,
            tmp_path=tmp_path,
        )
        assert obs._detect_heartbeat_blocked() is not None

    # --- scenario 9: warm-up does NOT suppress real high-latency

    def test_scenario9_high_latency_during_warmup_still_emits(self, tmp_path):
        obs = _make_observer(
            latency=120.0, tmp_path=tmp_path, past_warmup=False,
        )
        signal = obs._detect_heartbeat_blocked()
        assert signal is not None
        assert obs._heartbeat_suppression_count == 0

    # --- scenario 10: +inf is pathological, NOT suppressible

    def test_scenario10_positive_inf_emits_even_with_traffic(self, tmp_path):
        counter = _MessageCreateCounter(window_sec=600)
        counter.record(time.time())
        obs = _make_observer(
            latency=float("inf"), mc_counter=counter, tmp_path=tmp_path,
        )
        signal = obs._detect_heartbeat_blocked()
        assert signal is not None
        assert "infinite" in signal.description

    # --- scenario 11: -inf IS suppressible

    def test_scenario11_negative_inf_suppressible_with_traffic(self, tmp_path):
        counter = _MessageCreateCounter(window_sec=600)
        counter.record(time.time())
        obs = _make_observer(
            latency=float("-inf"), mc_counter=counter, tmp_path=tmp_path,
        )
        assert obs._detect_heartbeat_blocked() is None
        # Without traffic, still emits
        obs2 = _make_observer(latency=float("-inf"), tmp_path=tmp_path)
        assert obs2._detect_heartbeat_blocked() is not None

    # --- scenarios 12-13: non-numeric + negative

    def test_scenario12_non_numeric_suppressible(self, tmp_path):
        counter = _MessageCreateCounter(window_sec=600)
        counter.record(time.time())
        obs = _make_observer(
            latency="foo",  # type: ignore[arg-type]
            mc_counter=counter, tmp_path=tmp_path,
        )
        assert obs._detect_heartbeat_blocked() is None
        obs2 = _make_observer(
            latency="foo", tmp_path=tmp_path,  # type: ignore[arg-type]
        )
        signal = obs2._detect_heartbeat_blocked()
        assert signal is not None
        assert "non-numeric" in signal.description

    def test_scenario13_negative_finite_suppressible(self, tmp_path):
        counter = _MessageCreateCounter(window_sec=600)
        counter.record(time.time())
        obs = _make_observer(
            latency=-1.0, mc_counter=counter, tmp_path=tmp_path,
        )
        assert obs._detect_heartbeat_blocked() is None

    # --- scenario 14: window boundary
    #
    # Codex round 3 finding: the two signal sources have different
    # boundary semantics by construction:
    #
    #   * on_message path uses strict ``<`` (matches the
    #     existing _detect_gateway_deaf idle_sec check at line 444)
    #   * MESSAGE_CREATE counter uses inclusive ``>=`` cutoff (the
    #     existing count_in_window implementation at line ~107)
    #
    # In production these differ by a single sample at the exact
    # boundary — irrelevant given clock resolution — but pin the
    # behavior anyway so a future change doesn't drift either side
    # silently.

    def test_scenario14_on_message_just_inside_window_suppresses(self, tmp_path):
        now = 1_000_000.0  # deterministic; pass to detector via `now` arg
        obs = _make_observer(
            latency=float("nan"),
            last_on_message=now - 299.0,  # idle = 299, < 300 → suppress
            tmp_path=tmp_path,
        )
        assert obs._detect_heartbeat_blocked(now=now) is None

    def test_scenario14_on_message_at_exact_boundary_emits(self, tmp_path):
        """Strict-less-than: idle == 300.0 is NOT < 300 → emit.
        Pins on_message-path boundary semantics."""
        now = 1_000_000.0
        obs = _make_observer(
            latency=float("nan"),
            last_on_message=now - 300.0,
            tmp_path=tmp_path,
        )
        assert obs._detect_heartbeat_blocked(now=now) is not None

    def test_scenario14_on_message_just_outside_window_emits(self, tmp_path):
        now = 1_000_000.0
        obs = _make_observer(
            latency=float("nan"),
            last_on_message=now - 301.0,
            tmp_path=tmp_path,
        )
        assert obs._detect_heartbeat_blocked(now=now) is not None

    def test_scenario14_counter_at_exact_boundary_suppresses(self, tmp_path):
        """Inclusive ``>=`` cutoff: event at exactly now-300 counts.
        Pins counter-path boundary semantics."""
        now = 1_000_000.0
        counter = _MessageCreateCounter(window_sec=600)
        counter.record(now - 300.0)
        obs = _make_observer(
            latency=float("nan"), mc_counter=counter, tmp_path=tmp_path,
        )
        assert obs._detect_heartbeat_blocked(now=now) is None

    def test_scenario14_counter_just_past_boundary_emits(self, tmp_path):
        now = 1_000_000.0
        counter = _MessageCreateCounter(window_sec=600)
        counter.record(now - 300.5)
        obs = _make_observer(
            latency=float("nan"), mc_counter=counter, tmp_path=tmp_path,
        )
        assert obs._detect_heartbeat_blocked(now=now) is not None

    # --- scenario 15: lifecycle leak negative test

    def test_scenario15_lifecycle_fresh_ts_does_not_leak(self, tmp_path):
        """Codex round 2 tightening: this is the load-bearing
        negative test that proves lifecycle bumps (on_ready,
        on_resumed) do NOT leak into the heartbeat cross-check.

        Setup forces the wrong cross-check signal (lifecycle ts is
        fresh = NOW) to be available, while the right signal
        (on_message-only ts = 0.0, counter = None) is empty. If
        the detector accidentally consults inbound_event_ts_provider,
        this test fails."""
        now = time.time()
        obs = _make_observer(
            latency=float("nan"),
            mc_counter=None,  # no counter wired
            last_inbound=now,  # lifecycle bumped to NOW (mocking on_resumed)
            last_on_message=0.0,  # no real message ever received
            tmp_path=tmp_path,
        )
        signal = obs._detect_heartbeat_blocked()
        assert signal is not None, (
            "Lifecycle event leaked into heartbeat cross-check — "
            "detector should ONLY consult on_message-only ts + "
            "MESSAGE_CREATE counter, never inbound_event_ts."
        )

    def test_scenario15b_no_lifecycle_bumps_in_server_module(self):
        """Wiring assertion: ``_bump_on_message_only_ts()`` must
        appear EXACTLY ONCE in server.py, and that call's enclosing
        function MUST be ``on_message``. Additionally, writes to
        the ``_last_on_message_only_ts`` global may only occur
        inside ``_bump_on_message_only_ts`` itself (and at module
        init / annotation).

        Codex round 3 tightening: a permissive forbidden-set guard
        misses leaks via newly-added lifecycle helpers, renamed
        bootstrap paths, indirect calls, or direct assignment to
        the global. Exact-count + enclosing-function + assignment
        scope cover all four."""
        import ast
        import kernos.server as srv_mod
        src = open(srv_mod.__file__).read()
        tree = ast.parse(src)

        bump_call_sites: list[tuple[str, int]] = []
        global_writes: list[tuple[str, int]] = []
        # Track call-stack of enclosing function/method when walking
        # so nested call lookup is exact.

        def walk_func(node, enclosing: str):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Call) and (
                    isinstance(child.func, ast.Name)
                    and child.func.id == "_bump_on_message_only_ts"
                ):
                    bump_call_sites.append((enclosing, child.lineno))
                elif isinstance(child, ast.Call) and (
                    isinstance(child.func, ast.Attribute)
                    and child.func.attr == "_bump_on_message_only_ts"
                ):
                    # qualified call (mod._bump_on_message_only_ts())
                    bump_call_sites.append((enclosing, child.lineno))
                if isinstance(child, ast.Assign):
                    for tgt in child.targets:
                        if (
                            isinstance(tgt, ast.Name)
                            and tgt.id == "_last_on_message_only_ts"
                        ):
                            global_writes.append((enclosing, child.lineno))
                # Recurse into nested funcs / blocks
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    walk_func(child, child.name)
                else:
                    walk_func(child, enclosing)

        # Module-level walk (annotations + module-level assignments
        # don't count as "writes" we care about restricting)
        for top in ast.iter_child_nodes(tree):
            if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk_func(top, top.name)

        # Exactly one direct call, inside on_message
        assert len(bump_call_sites) == 1, (
            f"_bump_on_message_only_ts() must appear exactly once "
            f"in server.py; found {len(bump_call_sites)} site(s): "
            f"{bump_call_sites}"
        )
        enclosing, _lineno = bump_call_sites[0]
        assert enclosing == "on_message", (
            f"_bump_on_message_only_ts() must be called from "
            f"on_message; found in {enclosing!r}. Adding it to "
            f"any lifecycle handler breaks the heartbeat "
            f"cross-check (spec scenario 15)."
        )

        # Writes to _last_on_message_only_ts: only allowed inside
        # _bump_on_message_only_ts itself
        for enclosing, lineno in global_writes:
            assert enclosing == "_bump_on_message_only_ts", (
                f"_last_on_message_only_ts written from {enclosing!r} "
                f"at line {lineno}. This global may only be mutated "
                f"by _bump_on_message_only_ts (or module init), or "
                f"the lifecycle-leak guarantee is broken."
            )

    # --- scenario 16: V1.5 strike interaction
    # The strike counter increment happens in _tick (not in
    # _detect_heartbeat_blocked), so we exercise it through _tick.

    @pytest.mark.asyncio
    async def test_scenario16_suppressed_tick_does_not_increment_strikes(
        self, tmp_path,
    ):
        counter = _MessageCreateCounter(window_sec=600)
        counter.record(time.time())
        obs = _make_observer(
            latency=float("nan"), mc_counter=counter, tmp_path=tmp_path,
        )
        await obs._tick()
        assert obs._consecutive_heartbeat_strikes == 0

    @pytest.mark.asyncio
    async def test_scenario16b_suppressed_after_strikes_clears_counter(
        self, tmp_path,
    ):
        # First a few real strikes (no traffic, nan, past warmup)
        obs = _make_observer(latency=float("nan"), tmp_path=tmp_path)
        await obs._tick()
        await obs._tick()
        assert obs._consecutive_heartbeat_strikes == 2
        # Now traffic arrives — suppressed; strike counter clears
        # (because the else-branch in _tick resets to 0 when
        # heartbeat_unhealthy_this_tick is False)
        counter = _MessageCreateCounter(window_sec=600)
        counter.record(time.time())
        obs._message_create_counter = counter
        await obs._tick()
        assert obs._consecutive_heartbeat_strikes == 0

    # --- scenario 17: suppression log fires every Nth time

    def test_scenario17_suppression_log_emits_every_10th(
        self, tmp_path, caplog,
    ):
        import logging as _logging
        obs = _make_observer(
            latency=float("nan"),
            last_on_message=time.time(),
            tmp_path=tmp_path,
        )
        with caplog.at_level(_logging.INFO, logger="kernos.kernel.gateway_health"):
            for _ in range(10):
                obs._detect_heartbeat_blocked()
        assert obs._heartbeat_suppression_count == 10
        nominal = [
            r for r in caplog.records
            if "GATEWAY_HEALTH_HEARTBEAT_SUPPRESSED_NOMINAL" in r.getMessage()
        ]
        assert len(nominal) == 1, (
            f"Expected exactly one nominal log per 10 suppressions, "
            f"got {len(nominal)}"
        )

    # --- scenario 18: finite high latency reaches FrictionSignal

    def test_scenario18_real_signal_reaches_friction_signal(self, tmp_path):
        obs = _make_observer(latency=120.0, tmp_path=tmp_path)
        signal = obs._detect_heartbeat_blocked()
        assert signal is not None
        assert signal.signal_type == "DISCORD_HEARTBEAT_BLOCKED"
        assert signal.heuristic is False
        # Real signals do NOT count as suppressions
        assert obs._heartbeat_suppression_count == 0


class TestMessageCreateCounterWindowOverride:
    """Codex round 2 finding 1: count_in_window must accept an
    optional window_sec override so different detectors can query
    their own windows."""

    def test_default_window_used_when_no_override(self):
        c = _MessageCreateCounter(window_sec=600)
        now = time.time()
        c.record(now - 500)  # inside 600 default
        assert c.count_in_window(now) == 1

    def test_override_window_can_be_tighter(self):
        c = _MessageCreateCounter(window_sec=600)
        now = time.time()
        c.record(now - 500)  # outside 300 override
        assert c.count_in_window(now, window_sec=300) == 0

    def test_override_window_can_be_wider(self):
        c = _MessageCreateCounter(window_sec=60)
        now = time.time()
        c.record(now - 100)  # outside 60 default, inside 300 override
        assert c.count_in_window(now, window_sec=300) == 1


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
        """PATTERN A — MESSAGE_CREATE seen but on_message stale →
        deaf gateway (parser broken)."""
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
        assert "pattern=parser_broken" in signal.evidence

    # ─── DISCORD-GATEWAY-DEAFNESS-DETECT-V1 (2026-05-25) ─────────
    # PATTERN B — total socket silence. Surfaced when the bot ran
    # 100+ minutes with zero socket events of any type after
    # restart. Pre-spec the detector early-returned on mc_count==0
    # and missed this entirely. Now it checks any_socket_event_ts
    # first and emits the signal with pattern=total_socket_silence
    # evidence.

    def test_pattern_b_total_socket_silence_emits_signal(self, tmp_path):
        """Bug repro: ZERO socket events in deaf window AND elevated
        heartbeat latency. Pre-spec this returned None silently; now
        it surfaces DEAF.

        Latency=10.0 here exercises the GATEWAY-OBSERVER-FALSE-
        POSITIVE-GUARD (2026-05-28): silence alone with healthy
        heartbeat is treated as quiet-server, not deafness.
        """
        now = time.time()
        obs = _make_observer(
            mc_counter=None,
            any_socket_event_ts=now - 2400,  # 40 min silence vs 1800s window
            latency=10.0,                    # > 5.0s corroborating threshold
            tmp_path=tmp_path,
        )
        signal = obs._detect_gateway_deaf(now)
        assert signal is not None
        assert signal.signal_type == "DISCORD_GATEWAY_DEAF"
        assert "total silence" in signal.description.lower() or \
               "not delivered any socket event" in signal.description
        assert "pattern=total_socket_silence" in signal.evidence

    def test_pattern_b_recent_any_socket_event_returns_none(self, tmp_path):
        """Any socket event in window → healthy; no pattern-B alarm."""
        now = time.time()
        obs = _make_observer(
            mc_counter=None,
            any_socket_event_ts=now - 30,  # 30s ago, well inside window
            tmp_path=tmp_path,
        )
        assert obs._detect_gateway_deaf(now) is None

    def test_pattern_b_silence_with_healthy_latency_suppressed(
        self, tmp_path,
    ):
        """GATEWAY-OBSERVER-FALSE-POSITIVE-GUARD (2026-05-28): long
        silence + healthy heartbeat latency = quiet server, NOT
        deafness. The corroborating-latency guard suppresses the
        signal. Direct fix for the observed 483 friction signals in
        2 days on a low-traffic personal-bot guild."""
        now = time.time()
        obs = _make_observer(
            mc_counter=None,
            any_socket_event_ts=now - 2400,  # 40 min silence
            latency=0.05,                    # healthy heartbeat
            tmp_path=tmp_path,
        )
        assert obs._detect_gateway_deaf(now) is None

    def test_pattern_b_silence_with_none_latency_suppressed(
        self, tmp_path,
    ):
        """If latency_provider can't tell us anything (None), don't
        false-positive. Mirrors the watchdog's same defensive
        choice — unknown corroboration cannot promote silence to
        deafness."""
        now = time.time()
        obs = _make_observer(
            mc_counter=None,
            any_socket_event_ts=now - 2400,
            latency=None,
            tmp_path=tmp_path,
        )
        assert obs._detect_gateway_deaf(now) is None

    def test_pattern_b_corroborating_threshold_zero_restores_legacy(
        self, tmp_path, monkeypatch,
    ):
        """Setting KERNOS_GATEWAY_DEAF_CORROBORATING_LATENCY_SEC=0
        restores the legacy silence-only emission behavior — for
        operators who want the old aggressive detection back."""
        from kernos.kernel import gateway_health

        monkeypatch.setattr(
            gateway_health, "_GATEWAY_DEAF_CORROBORATING_LATENCY_SEC", 0.0,
        )
        now = time.time()
        obs = _make_observer(
            mc_counter=None,
            any_socket_event_ts=now - 2400,  # long silence
            latency=0.05,                    # healthy heartbeat
            tmp_path=tmp_path,
        )
        signal = obs._detect_gateway_deaf(now)
        assert signal is not None
        assert signal.signal_type == "DISCORD_GATEWAY_DEAF"
        assert "pattern=total_socket_silence" in signal.evidence

    def test_pattern_b_zero_timestamp_skipped(self, tmp_path):
        """If any_socket_event_ts == 0 (bot just started, no events
        yet), don't false-positive — give bot warm-up time."""
        now = time.time()
        obs = _make_observer(
            mc_counter=None,
            any_socket_event_ts=0.0,
            tmp_path=tmp_path,
        )
        assert obs._detect_gateway_deaf(now) is None

    def test_pattern_b_no_provider_falls_back_to_pattern_a_only(
        self, tmp_path,
    ):
        """When any_socket_event_ts_provider is None (test fixtures
        / legacy callers), the new pattern-B check is skipped and
        the old pattern-A logic alone runs."""
        now = time.time()
        # No mc_counter, no any-socket provider → both paths skip,
        # detector returns None (back-compat preserved).
        obs = _make_observer(
            mc_counter=None,
            any_socket_event_ts=None,
            tmp_path=tmp_path,
        )
        assert obs._detect_gateway_deaf(now) is None

    def test_pattern_b_wins_over_pattern_a_when_both_could_fire(
        self, tmp_path,
    ):
        """Sequencing: pattern B (total silence) is the more severe
        diagnostic so it surfaces first. If somehow both conditions
        held (total silence AND stale on_message AND mc_count > 0
        from earlier), pattern B's evidence wins.

        Latency=10.0 satisfies the corroborating-latency guard
        added 2026-05-28.
        """
        now = time.time()
        counter = _MessageCreateCounter(window_sec=600)
        # Old MESSAGE_CREATE outside window won't count anyway.
        # But add a recent one to make pattern A theoretically fireable
        # too — pattern B should still take precedence because the
        # any-socket-silence check runs first.
        counter.record(now - 50)
        obs = _make_observer(
            mc_counter=counter,
            last_inbound=now - 3600,  # pattern A would fire on this
            any_socket_event_ts=now - 2400,  # pattern B fires on this
            latency=10.0,  # corroborating-latency guard satisfied
            tmp_path=tmp_path,
        )
        signal = obs._detect_gateway_deaf(now)
        assert signal is not None
        # Pattern B's evidence in the signal
        assert "pattern=total_socket_silence" in signal.evidence


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
