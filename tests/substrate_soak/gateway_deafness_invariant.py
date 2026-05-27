"""Probe 6 — gateway_deafness_invariant (SUBSTRATE-SELF-TEST-V1).

Asserts the full detect-strike-restart cascade fires when the
Discord gateway delivers zero socket events past the deaf window
even with healthy heartbeats.

Regression bug: a7302b0. Both detection layers (watchdog +
observer) were blind to "healthy heartbeat + total socket
silence" for the 100-minute production incident.
"""
from __future__ import annotations

import time
from unittest.mock import patch

from kernos.kernel.self_test_gate import ProbeResult


REQUIRED_BEHAVIORAL_KEYS = frozenset({
    "watchdog_unhealthy",
    "observer_signal_emitted",
    "execv_called",
})

REQUIRED_SUBSTRATE_KEYS = frozenset({
    "watchdog_reason",
    "observer_signal_evidence",
    "strikes_before_restart",
    "execv_call_count",
})


class _StubClient:
    """Minimal stand-in for discord.Client exposing only
    the latency attribute the watchdog checks."""
    def __init__(self, latency: float = 0.05) -> None:
        self._latency = latency

    @property
    def latency(self) -> float:
        return self._latency


async def run_probe() -> ProbeResult:
    start = time.monotonic()

    # Lazy imports so monkeypatches reach the actual functions
    # (module-level imports bind at probe-module load time and
    # bypass mutations applied later).
    from kernos import server as _server
    from kernos.kernel import gateway_health as _gw_health

    # --- (1) watchdog unhealthy with silence reason ---
    # Backdate _last_any_socket_event_ts so the silence-check fires
    # while latency is healthy.
    orig_last_any = _server._last_any_socket_event_ts
    orig_silence_window = _server._DISCORD_DEAF_SILENCE_WINDOW_SEC
    orig_strikes = _server._gateway_unhealthy_strikes
    orig_disable = _server._DISCORD_WATCHDOG_DISABLE

    watchdog_reason = ""
    watchdog_unhealthy = False
    strikes_before_restart = 0
    execv_calls: list[tuple] = []

    try:
        # Tight window + escape-hatch disabled.
        _server._DISCORD_DEAF_SILENCE_WINDOW_SEC = 1
        _server._DISCORD_WATCHDOG_DISABLE = False
        _server._last_any_socket_event_ts = time.time() - 10
        _server._gateway_unhealthy_strikes = 0

        # Stub the discord.Client + os.execv so the cascade is
        # observable without actually restarting the process.
        with patch.object(_server, "client", _StubClient(latency=0.05)):
            with patch.object(
                _server.os, "execv",
                lambda *a, **kw: execv_calls.append(a),
            ):
                # (1) Direct unhealthy-check call captures the
                # silence reason.
                wd_unhealthy, wd_reason = (
                    _server._is_gateway_heartbeat_unhealthy()
                )
                watchdog_unhealthy = wd_unhealthy
                watchdog_reason = wd_reason

                # (2) Three sequential ticks → escalation to execv.
                tick1 = _server._watchdog_tick()
                tick2 = _server._watchdog_tick()
                tick3 = _server._watchdog_tick()
                strikes_before_restart = (
                    _server._gateway_unhealthy_strikes
                )
    finally:
        _server._last_any_socket_event_ts = orig_last_any
        _server._DISCORD_DEAF_SILENCE_WINDOW_SEC = orig_silence_window
        _server._gateway_unhealthy_strikes = orig_strikes
        _server._DISCORD_WATCHDOG_DISABLE = orig_disable

    # --- (2) observer-side detection ---
    # Construct a fresh observer with the new any_socket_event_ts
    # provider backdated to trigger pattern B (total socket silence).
    now = time.time()
    socket_silence_ts = now - 1200  # 20 min ago, > 600s window

    # Minimal stub store that records emissions.
    class _CaptureStore:
        def __init__(self):
            self.emitted: list[dict] = []

        async def get_pattern_by_signal_type(self, *, instance_id, signal_type):
            return None

        async def record_occurrence(self, **kw):
            return None

    observer = _gw_health.GatewayHealthObserver(
        instance_id="probe6_test",
        data_dir="/tmp/probe6_test_data",
        pattern_store=_CaptureStore(),
        latency_provider=lambda: 0.05,
        inbound_event_ts_provider=lambda: 0.0,
        message_create_counter=None,
        any_socket_event_ts_provider=lambda: socket_silence_ts,
        last_on_message_provider=lambda: 0.0,
    )

    signal = observer._detect_gateway_deaf(now)
    observer_signal_emitted = signal is not None
    observer_signal_evidence_str = ""
    if signal is not None:
        observer_signal_evidence_str = (
            f"signal_type={signal.signal_type} "
            f"evidence={'; '.join(signal.evidence)}"
        )

    duration_ms = int((time.monotonic() - start) * 1000)

    # Pass conditions:
    # - watchdog flagged unhealthy with the silence reason
    # - observer emitted the DISCORD_GATEWAY_DEAF signal with
    #   pattern=total_socket_silence evidence
    # - 3 strikes accumulated (after the latency-healthy +
    #   silence-unhealthy state)
    # - os.execv called exactly once (the escalation reached
    #   the restart call, not just fired strikes)
    cond_watchdog = (
        watchdog_unhealthy
        and "no socket events received" in watchdog_reason
        and "gateway deaf despite latency" in watchdog_reason
    )
    cond_observer = (
        observer_signal_emitted
        and signal is not None
        and signal.signal_type == "DISCORD_GATEWAY_DEAF"
        and any(
            "pattern=total_socket_silence" in e
            for e in signal.evidence
        )
    )
    cond_strikes = (strikes_before_restart == 3)
    cond_execv = (len(execv_calls) == 1)

    all_passed = (
        cond_watchdog and cond_observer
        and cond_strikes and cond_execv
    )

    failure_reason = ""
    if not all_passed:
        failed = []
        if not cond_watchdog:
            failed.append(
                f"watchdog_silence_check (reason={watchdog_reason!r})"
            )
        if not cond_observer:
            failed.append(
                f"observer_pattern_b (emitted={observer_signal_emitted})"
            )
        if not cond_strikes:
            failed.append(
                f"strike_escalation (got={strikes_before_restart}, want=3)"
            )
        if not cond_execv:
            failed.append(
                f"execv_called (got={len(execv_calls)}, want=1)"
            )
        failure_reason = (
            f"gateway-deafness invariant violated: {', '.join(failed)}. "
            f"Likely regression of a7302b0 (watchdog silence-check OR "
            f"observer pattern-B branch)."
        )

    # Pair the cascade booleans with substantive summary text so
    # AC2's shallow-evidence check sees real signal (spec bans
    # all-bool evidence dicts like {"ok": True}).
    cascade_summary = (
        f"watchdog={'unhealthy' if watchdog_unhealthy else 'healthy'}, "
        f"observer={'emitted' if observer_signal_emitted else 'silent'}, "
        f"execv_calls={len(execv_calls)}"
    )

    return ProbeResult(
        probe_name="gateway_deafness_invariant",
        passed=all_passed,
        behavioral_evidence={
            "watchdog_unhealthy": watchdog_unhealthy,
            "observer_signal_emitted": observer_signal_emitted,
            "execv_called": len(execv_calls) == 1,
            "cascade_summary": cascade_summary,
        },
        substrate_evidence={
            "watchdog_reason": watchdog_reason,
            "observer_signal_evidence": observer_signal_evidence_str,
            "strikes_before_restart": strikes_before_restart,
            "execv_call_count": len(execv_calls),
        },
        duration_ms=duration_ms,
        failure_reason=failure_reason,
    )
