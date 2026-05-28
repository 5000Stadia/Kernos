"""GATEWAY-HEALTH-OBSERVER-V1 (2026-05-19) — gateway/dispatch-layer
friction signal source.

Companion to ``FrictionObserver`` (which observes per-turn). This
observer runs continuously in the background and emits
``FrictionSignal`` records into the SAME ``FrictionPatternStore``
catalog the per-turn observer feeds. No parallel system — same
data model, same lifecycle, same auto-classifier hook.

See ``specs/GATEWAY-HEALTH-OBSERVER-V1.md`` for the full design
including V2 (declarative auto-remediation policy on patterns) and
V3 (delete standalone watchdog once V2 lands).

V1 scope: detect + emit. No auto-remediation; the existing
``_discord_gateway_watchdog_loop`` in server.py stays as the
safety net for restart-on-broken-heartbeat. V1 just adds the
gateway/dispatch-layer patterns to the catalog so operators
(and the next investigation) have evidence.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from kernos.kernel.friction import FrictionSignal

logger = logging.getLogger(__name__)


# Detection thresholds. All env-tunable so an operator can tighten
# or loosen without redeploy. Defaults conservative per spec V1.
_POLL_INTERVAL_SEC = int(
    os.getenv("KERNOS_GATEWAY_HEALTH_POLL_SEC", "60"),
)
_GATEWAY_DEAF_WINDOW_SEC = int(
    # Bumped 600s → 1800s (2026-05-28, GATEWAY-OBSERVER-FALSE-
    # POSITIVE-GUARD): low-traffic personal bot saw 483
    # DISCORD_GATEWAY_DEAF friction reports in 2 days at the
    # 600s threshold — quiet hours regularly cross that without
    # the gateway being actually deaf. Same rationale as the
    # WATCHDOG-FALSE-POSITIVE-GUARD-V1 (commit b610abd) bumping
    # the parallel watchdog threshold to 1800s.
    os.getenv("KERNOS_GATEWAY_DEAF_WINDOW_SEC", "1800"),  # 30 min
)
# Corroborating-latency guard for the observer's pattern B
# detection. Mirrors the watchdog's _DISCORD_DEAF_CORROBORATING_
# LATENCY_SEC pattern: silence alone with healthy heartbeat is a
# quiet-server-likely signal, not deafness. Setting to 0 restores
# the legacy silence-only behavior.
_GATEWAY_DEAF_CORROBORATING_LATENCY_SEC = float(
    os.getenv("KERNOS_GATEWAY_DEAF_CORROBORATING_LATENCY_SEC", "5.0"),
)
_HEARTBEAT_THRESHOLD_SEC = float(
    os.getenv("KERNOS_DISCORD_WATCHDOG_LATENCY_THRESHOLD_SEC", "60"),
)
_POOL_LEAK_THRESHOLD = int(
    os.getenv("KERNOS_HTTP_POOL_CLOSE_WAIT_THRESHOLD", "30"),
)
# V1.5 inline remediation (founder's all-three request, 2026-05-20):
# when the observer has detected discord-heartbeat-blocked for N
# consecutive ticks, force-restart via os.execv directly. This is
# the safety net for the failure mode we observed where the
# existing watchdog stopped firing (its task may have died silently)
# — the observer's tick is the one we know is reliable.
#
# Spec'd more cleanly as V2 (declarative remediation_action on
# FrictionPattern records via FrictionPatternStore.start callback).
# This inline shape is the minimum-viable safety net while V2 is
# scoped — addresses the exact failure we just observed without
# blocking on the larger framework change.
_RESTART_AFTER_CONSECUTIVE_HEARTBEAT_STRIKES = int(
    os.getenv("KERNOS_GATEWAY_HEALTH_RESTART_AFTER_STRIKES", "5"),
)
# HEARTBEAT-DETECTOR-LIVENESS-CROSSCHECK-V1 (2026-05-20):
# tunables for the cross-check that suppresses noise from a
# tracker-unreliable client.latency when the gateway is alive
# via other evidence.
_WARMUP_GRACE_SEC = int(
    os.getenv("KERNOS_GATEWAY_HEALTH_WARMUP_GRACE_SEC", "60"),
)
_INBOUND_TRAFFIC_LIVENESS_WINDOW_SEC = int(
    os.getenv("KERNOS_HEARTBEAT_LIVENESS_TRAFFIC_WINDOW_SEC", "300"),
)
_SUPPRESSION_LOG_EVERY_N = int(
    os.getenv("KERNOS_HEARTBEAT_SUPPRESSION_LOG_EVERY_N", "10"),
)


@dataclass
class _MessageCreateCounter:
    """Bounded window of MESSAGE_CREATE socket-event timestamps.

    Wired from ``server.py``'s ``on_socket_event_type`` handler.
    Window-bounded so memory is constant regardless of message rate.

    ``count_in_window`` accepts an optional ``window_sec`` override
    so different detectors can query their own windows without
    each owning a private counter. The heartbeat cross-check uses
    its 300s window; the gateway-deaf detector uses its 600s.
    """
    _events: deque[float]

    def __init__(self, window_sec: int) -> None:
        # maxlen large enough for high-throughput sessions; window
        # filtering happens at query time.
        self._events = deque(maxlen=10_000)
        self._window_sec = window_sec

    def record(self, ts: float) -> None:
        self._events.append(ts)

    def count_in_window(
        self, now: float, *, window_sec: int | None = None,
    ) -> int:
        window = window_sec if window_sec is not None else self._window_sec
        cutoff = now - window
        # deque is unordered for membership but ordered for
        # insertion; events come in monotonic time, so a left-pop
        # would work, but we keep the deque immutable here and just
        # count to avoid mutating during read.
        return sum(1 for ts in self._events if ts >= cutoff)


@dataclass(frozen=True)
class GatewayHealthProviders:
    """Live state sources the ``GatewayHealthObserver`` reads on
    every tick. The caller (typically ``server.py``) constructs
    this from its own module globals and passes it to
    ``bring_up_substrate`` as the ``gateway_health_providers``
    kwarg.

    Why this exists (SUBSTRATE-PROVIDER-INJECTION-V1, 2026-05-21):
    before this, ``bring_up_substrate.py`` did
    ``import kernos.server as _srv`` to read live state. But
    ``server.py`` runs as ``__main__`` (via
    ``python kernos/server.py`` from ``start.sh``), so
    ``import kernos.server`` produced a separate module object —
    the observer's lambdas read inert globals while the live
    @client.event handlers mutated the ``__main__`` copy. The
    heartbeat cross-check never suppressed a single signal in
    production. This dataclass moves the dependency to a single,
    visible, testable boundary: substrate no longer imports its
    caller.

    All fields except ``message_create_counter`` are callables so
    the observer reads fresh values on every tick. The counter is
    a stable mutable object that records in place; passing the
    reference is sufficient.
    """
    latency_provider: Callable[[], float | None]
    inbound_event_ts_provider: Callable[[], float]
    last_on_message_provider: Callable[[], float]
    message_create_counter: "_MessageCreateCounter | None"
    # DISCORD-GATEWAY-DEAFNESS-DETECT-V1 (2026-05-25): timestamp of
    # the last gateway socket event of ANY type. Lets the observer
    # distinguish "gateway deaf — no events at all" from "gateway
    # quiet — no message events in particular." Default None for
    # back-compat with callers that don't pass it; observer
    # skips the new-pattern detection in that case.
    any_socket_event_ts_provider: Callable[[], float] | None = None


class GatewayHealthObserver:
    """Background-task observer for gateway/dispatch-layer friction.

    Construction signature mirrors ``FrictionObserver`` for parity:
    accept the ``pattern_store`` for catalog interactions and a
    ``data_dir`` for the diagnostic-report path. The observer does
    NOT depend on the discord ``Client`` directly — callers inject
    ``latency_provider`` + ``inbound_event_ts_provider`` so this
    file stays import-safe in tests.

    ``message_create_counter`` is the bridge to discord.py's
    ``on_socket_event_type`` handler (wired in server.py) — the
    handler calls ``counter.record(time.time())`` when
    MESSAGE_CREATE arrives.

    ``runner_inspector`` returns a list of
    ``(space_id, oldest_mailbox_ts)`` tuples for stuck-runner
    detection; ``None`` skips that detector (V1 ships with a stub
    that returns []; full implementation lands in V1.5 with the
    runner-inspector seam).
    """

    def __init__(
        self,
        *,
        instance_id: str,
        data_dir: str,
        pattern_store: Any,
        latency_provider: Callable[[], float | None],
        inbound_event_ts_provider: Callable[[], float],
        message_create_counter: _MessageCreateCounter | None,
        runner_inspector: Callable[[], list[tuple[str, float]]] | None = None,
        poll_interval_sec: int = _POLL_INTERVAL_SEC,
        last_on_message_provider: Callable[[], float] | None = None,
        any_socket_event_ts_provider: Callable[[], float] | None = None,
    ) -> None:
        self._instance_id = instance_id
        self._data_dir = data_dir
        self._pattern_store = pattern_store
        self._latency_provider = latency_provider
        self._inbound_event_ts_provider = inbound_event_ts_provider
        self._message_create_counter = message_create_counter
        # DISCORD-GATEWAY-DEAFNESS-DETECT-V1 (2026-05-25): None when
        # callers haven't wired the provider (test fixtures, legacy
        # bring-up paths). When set, _detect_gateway_deaf adds a
        # second branch that catches "gateway received zero socket
        # events of any kind" — the failure mode the original
        # MESSAGE_CREATE-vs-on_message comparison cannot see.
        self._any_socket_event_ts_provider = any_socket_event_ts_provider
        self._runner_inspector = runner_inspector
        self._poll_interval_sec = max(1, poll_interval_sec)
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        # Test-introspection counters
        self._poll_count = 0
        self._signals_emitted = 0
        # V1.5 inline remediation state. Counts consecutive ticks
        # that emit DISCORD_HEARTBEAT_BLOCKED. When the count
        # reaches _RESTART_AFTER_CONSECUTIVE_HEARTBEAT_STRIKES we
        # log + execv. Reset to 0 on any healthy heartbeat tick.
        self._consecutive_heartbeat_strikes = 0
        # HEARTBEAT-DETECTOR-LIVENESS-CROSSCHECK-V1. Pure on_message
        # timestamp (not bumped by on_ready/on_resumed). Codex round 1
        # finding: inbound_event_ts_provider includes lifecycle events
        # so a post-resume gateway death would have a fresh ts even
        # with zero real traffic; this provider is bumped only inside
        # on_message. None during construction; set in start() so
        # _uptime_sec is honest about "since the observer task began".
        self._last_on_message_provider = last_on_message_provider
        self._observer_started_at: float = 0.0
        self._heartbeat_suppression_count: int = 0
        self._last_suppression_kind: str = ""
        self._last_suppression_reason: str = ""

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        import time as _time
        # monotonic for clock-skew immunity. Set BEFORE create_task
        # so the first tick already sees a real uptime.
        self._observer_started_at = _time.monotonic()
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "GATEWAY_HEALTH_OBSERVER_STARTED instance=%s "
            "poll_interval=%ds deaf_window=%ds heartbeat_threshold=%.1fs "
            "pool_leak_threshold=%d warmup_grace=%ds "
            "liveness_traffic_window=%ds",
            self._instance_id, self._poll_interval_sec,
            _GATEWAY_DEAF_WINDOW_SEC, _HEARTBEAT_THRESHOLD_SEC,
            _POOL_LEAK_THRESHOLD,
            _WARMUP_GRACE_SEC, _INBOUND_TRAFFIC_LIVENESS_WINDOW_SEC,
        )

    def _uptime_sec(self) -> float:
        """Seconds since ``start()`` was called. Returns 0.0 if the
        observer hasn't been started — callers should treat that
        as 'still warming up' so warm-up grace remains in effect
        for tests that exercise the detector without starting the
        background task."""
        if self._observer_started_at == 0.0:
            return 0.0
        import time as _time
        return _time.monotonic() - self._observer_started_at

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None
        self._stop_event = None
        logger.info(
            "GATEWAY_HEALTH_OBSERVER_STOPPED instance=%s",
            self._instance_id,
        )

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception as exc:
                logger.warning(
                    "GATEWAY_HEALTH_OBSERVER_TICK_FAILED: %s",
                    exc, exc_info=True,
                )
            self._poll_count += 1
            assert self._stop_event is not None
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval_sec,
                )
                return  # stop signalled
            except asyncio.TimeoutError:
                continue  # next tick

    async def _tick(self) -> None:
        """One observation cycle. Run all detectors; emit each
        non-None signal into the catalog. Fail-isolated per
        detector — one detector raising doesn't skip the others.

        V1.5 inline remediation: track consecutive heartbeat-
        blocked ticks; when ``_RESTART_AFTER_CONSECUTIVE_HEARTBEAT_STRIKES``
        is reached, log + os.execv. Catches the case where the
        standalone watchdog stopped firing (silent task death)
        and the gateway has been broken long enough that recovery
        won't come from anywhere else.
        """
        import time as _time
        now = _time.time()

        # Defensive: ensure the log file handler is still attached to
        # logging.root. Live observation 2026-05-20: the handler was
        # silently detached at some point ~30min after boot, so
        # post-failure RCA had no file evidence even though the
        # ring buffer kept capturing. This re-attach is best-effort.
        try:
            from kernos.kernel.log_buffer import (
                ensure_log_file_handler_attached,
            )
            if ensure_log_file_handler_attached():
                logger.warning(
                    "GATEWAY_HEALTH_LOG_FILE_REATTACHED: "
                    "RotatingFileHandler had been detached from "
                    "logging.root.handlers; re-attached. Cause "
                    "unconfirmed (likely discord.py setup_logging "
                    "or other handler-list mutation)."
                )
        except Exception as exc:
            logger.warning(
                "GATEWAY_HEALTH_LOG_FILE_CHECK_FAILED: %s", exc,
            )

        detectors = [
            ("discord-heartbeat-blocked", lambda: self._detect_heartbeat_blocked(now)),
            ("discord-gateway-deaf", lambda: self._detect_gateway_deaf(now)),
            ("space-runner-stuck", lambda: self._detect_runner_stuck(now)),
            ("discord-connection-pool-leak", self._detect_pool_leak),
        ]
        heartbeat_unhealthy_this_tick = False
        for name, fn in detectors:
            try:
                signal = fn()
            except Exception as exc:
                logger.warning(
                    "GATEWAY_HEALTH_DETECTOR_FAILED: detector=%s exc=%s",
                    name, exc,
                )
                continue
            # Set strike flag from the DETECTOR output, not from
            # whether _record_signal succeeded. Codex audit
            # 2026-05-20: tying the strike counter to the catalog
            # write means a transient store failure silently
            # zeroes our restart guard. The detector is the source
            # of truth for "is the heartbeat unhealthy right now."
            if signal is not None and name == "discord-heartbeat-blocked":
                heartbeat_unhealthy_this_tick = True
            if signal is not None:
                try:
                    await self._record_signal(signal)
                except Exception as exc:
                    logger.warning(
                        "GATEWAY_HEALTH_RECORD_SIGNAL_FAILED: "
                        "detector=%s exc=%s", name, exc,
                    )

        # V1.5 inline remediation for the failure mode observed
        # 2026-05-20: heartbeat NaN persists, standalone watchdog
        # not firing (its task died silently somewhere). The observer
        # is the survivor; restart via the observer's path.
        #
        # FRICTION-REMEDIATION-V2.1 (2026-05-20): both V1.5 and V2
        # claim through ``try_claim_remediation_fire`` — single source
        # of truth for the cool-off sentinel AND the rolling escalation
        # guard. The prior implementation only had V1.5 READ V2's
        # sentinel but never WRITE one, so a V1.5-first fire would
        # still loop. With the shared helper, V1.5 writes the same
        # sentinel + history that V2 reads (and vice versa).
        if heartbeat_unhealthy_this_tick:
            self._consecutive_heartbeat_strikes += 1
            if (
                self._consecutive_heartbeat_strikes
                >= _RESTART_AFTER_CONSECUTIVE_HEARTBEAT_STRIKES
            ):
                from kernos.kernel.friction_patterns import (
                    try_claim_remediation_fire,
                )
                window_sec = int(
                    os.getenv(
                        "KERNOS_DISCORD_HEARTBEAT_REMEDIATION_WINDOW_SEC",
                        "600",
                    ),
                )
                escalation_window_sec = int(
                    os.environ.get(
                        "KERNOS_FRICTION_REMEDIATION_ESCALATION_WINDOW_SEC",
                        "3600",
                    )
                )
                escalation_max_fires = int(
                    os.environ.get(
                        "KERNOS_FRICTION_REMEDIATION_ESCALATION_MAX_FIRES",
                        "3",
                    )
                )
                claimed, reason = try_claim_remediation_fire(
                    data_dir=self._data_dir,
                    instance_id=self._instance_id,
                    pattern_id="discord-heartbeat-blocked",
                    window_sec=window_sec,
                    max_fires_per_window=escalation_max_fires,
                    escalation_window_sec=escalation_window_sec,
                )
                if not claimed:
                    if reason == "cool_off":
                        logger.warning(
                            "GATEWAY_HEALTH_FORCE_RESTART_SKIPPED_COOL_OFF: "
                            "heartbeat unhealthy for %d ticks but shared "
                            "remediation sentinel says we restarted "
                            "recently — gateway issue persists across "
                            "restart, not a recoverable failure. Skipping "
                            "V1.5 restart to prevent loop.",
                            self._consecutive_heartbeat_strikes,
                        )
                    elif reason == "escalation_max_fires_reached":
                        logger.error(
                            "GATEWAY_HEALTH_FORCE_RESTART_ESCALATION_GUARD: "
                            "heartbeat unhealthy for %d ticks but the "
                            "shared escalation guard tripped — restart "
                            "isn't fixing this. Refusing further fires "
                            "until operator clears history.",
                            self._consecutive_heartbeat_strikes,
                        )
                    else:
                        logger.warning(
                            "GATEWAY_HEALTH_FORCE_RESTART_CLAIM_REFUSED: "
                            "reason=%s strikes=%d",
                            reason, self._consecutive_heartbeat_strikes,
                        )
                    # Reset strikes so we don't log STRIKE every
                    # subsequent tick during cool-off / escalation
                    self._consecutive_heartbeat_strikes = 0
                else:
                    logger.error(
                        "GATEWAY_HEALTH_FORCE_RESTART: heartbeat unhealthy "
                        "for %d consecutive ticks (interval=%ds, threshold=%d). "
                        "Standalone watchdog hasn't recovered — observer "
                        "is force-restarting via os.execv (claim acquired "
                        "via shared try_claim_remediation_fire).",
                        self._consecutive_heartbeat_strikes,
                        self._poll_interval_sec,
                        _RESTART_AFTER_CONSECUTIVE_HEARTBEAT_STRIKES,
                    )
                    # Flush logs before exec replaces the process
                    for h in logging.getLogger().handlers:
                        try:
                            h.flush()
                        except Exception:
                            pass
                    os.execv(__import__("sys").executable, [
                        __import__("sys").executable
                    ] + __import__("sys").argv)
        else:
            if self._consecutive_heartbeat_strikes > 0:
                logger.info(
                    "GATEWAY_HEALTH_HEARTBEAT_RECOVERED: strikes_cleared=%d",
                    self._consecutive_heartbeat_strikes,
                )
            self._consecutive_heartbeat_strikes = 0

    # ----- detectors -------------------------------------------------

    def _detect_heartbeat_blocked(
        self, now: float | None = None,
    ) -> FrictionSignal | None:
        """Emit ``DISCORD_HEARTBEAT_BLOCKED`` when ``client.latency``
        indicates the heartbeat tracker is unhealthy AND the
        substrate has no independent evidence that the gateway is
        alive (cross-check, V1 LIVENESS-CROSSCHECK 2026-05-20).

        Optional ``now`` argument mirrors ``_detect_gateway_deaf`` —
        the poll-loop passes ``time.time()`` once per tick and
        threads it through both detectors so they see the same
        instant. Tests pass an explicit value to pin window
        boundary behavior without racing the internal clock.

        Failure-mode classification + suppression rules:

        | latency value                          | tracker_unreliable | emits when no traffic |
        | -------------------------------------- | ------------------ | --------------------- |
        | finite > 0, <= 60s                     | n/a (healthy)      | no                    |
        | finite > 60s threshold                 | False              | yes (real signal)     |
        | +inf                                   | False              | yes (pathological)    |
        | None / nan / -inf / non-numeric / <= 0 | True               | yes (after window)    |

        ``tracker_unreliable`` cases are suppressed when (a) the
        observer is within the warm-up grace window, or (b)
        ``_is_inbound_traffic_alive`` returns True (recent
        on_message-only evidence). Non-suppressible cases (finite
        high latency, +inf) emit regardless.
        """
        if now is None:
            import time as _time
            now = _time.time()
        latency = self._latency_provider()

        reason, tracker_unreliable = self._classify_latency(latency)
        if reason is None:
            return None  # healthy

        if tracker_unreliable:
            if self._uptime_sec() < _WARMUP_GRACE_SEC:
                self._note_heartbeat_suppression("warmup", reason)
                return None
            if self._is_inbound_traffic_alive(now):
                self._note_heartbeat_suppression("live_traffic", reason)
                return None

        return FrictionSignal(
            signal_type="DISCORD_HEARTBEAT_BLOCKED",
            description=f"Discord heartbeat unhealthy: {reason}",
            evidence=[reason],
            context={
                "space": "",  # gateway-layer; no per-space scoping
                "member_id": "",
                "latency_threshold_sec": _HEARTBEAT_THRESHOLD_SEC,
            },
            heuristic=False,
        )

    def _classify_latency(
        self, latency: Any,
    ) -> tuple[str | None, bool]:
        """Return ``(reason, tracker_unreliable)``. ``reason=None``
        signals 'healthy, no emission'. ``tracker_unreliable=True``
        flags this as a candidate for cross-check suppression.

        Order of checks matters: ``None`` first (avoids isfinite
        TypeError); ``isfinite`` in a try/except to handle weird
        types (string, list, etc.); then the finite branches.

        +inf is treated as a REAL signal (latency is pathologically
        high) and is NOT suppressible — the tracker IS reporting,
        the value just happens to be infinity. -inf has no
        meaningful interpretation; suppressible.
        """
        if latency is None:
            return ("client.latency is None", True)
        try:
            finite = math.isfinite(latency)
        except (TypeError, ValueError):
            return (
                f"client.latency non-numeric: {latency!r}", True,
            )
        if not finite:
            try:
                if math.isnan(latency):
                    return (
                        f"client.latency non-finite: {latency}",
                        True,
                    )
                # +inf vs -inf — both isfinite()=False, isnan()=False
                if latency > 0:
                    return (
                        f"client.latency non-finite: {latency} "
                        f"(infinite)",
                        False,  # real signal: tracker says infinity
                    )
                # -inf
                return (
                    f"client.latency non-finite: {latency}", True,
                )
            except (TypeError, ValueError):
                # Belt-and-suspenders if isnan trips on a weird type
                return (
                    f"client.latency non-numeric: {latency!r}", True,
                )
        if latency <= 0:
            return (f"client.latency non-positive: {latency}", True)
        if latency > _HEARTBEAT_THRESHOLD_SEC:
            return (
                f"client.latency={latency:.1f}s exceeds threshold "
                f"{_HEARTBEAT_THRESHOLD_SEC}s",
                False,  # real signal: tracker is computing, value is bad
            )
        return (None, False)  # healthy

    def _is_inbound_traffic_alive(self, now: float) -> bool:
        """True iff PURE on_message evidence shows the gateway is
        alive. Two independent signals — either is sufficient:

          * MESSAGE_CREATE socket-event counter > 0 inside
            ``_INBOUND_TRAFFIC_LIVENESS_WINDOW_SEC`` (proves the
            websocket is delivering dispatch).
          * ``last_on_message_provider()`` within the same window
            (proves the parser + handler fired on real input).

        Both signals are bumped ONLY by real message traffic.
        ``inbound_event_ts_provider`` (used by ``_detect_gateway_deaf``)
        is intentionally NOT consulted here because it includes
        ``on_ready`` / ``on_resumed`` / bootstrap bumps, which would
        mask a post-resume gateway death.

        Counter absence (None) does NOT short-circuit — the
        on_message path is checked independently so test fixtures
        and partial wirings still get an honest answer.
        """
        # MESSAGE_CREATE check — explicit window override so we
        # don't inherit the deaf-detector counter's 600s window.
        if self._message_create_counter is not None:
            if self._message_create_counter.count_in_window(
                now,
                window_sec=_INBOUND_TRAFFIC_LIVENESS_WINDOW_SEC,
            ) > 0:
                return True
        # on_message-only check — independent of counter wiring
        if self._last_on_message_provider is not None:
            last = self._last_on_message_provider()
            if last and last > 0:
                idle = now - last
                if idle < _INBOUND_TRAFFIC_LIVENESS_WINDOW_SEC:
                    return True
        return False

    def _note_heartbeat_suppression(
        self, kind: str, reason: str,
    ) -> None:
        """Track suppression so operators can distinguish 'detector
        is suppressing correctly' from 'detector stopped running'.
        Per Codex round 1 medium finding: debug-only logs may be
        invisible during rollout; an INFO log every Nth suppression
        plus a counter exposed for tests makes suppression
        observable without spamming."""
        self._heartbeat_suppression_count += 1
        self._last_suppression_kind = kind
        self._last_suppression_reason = reason
        if (
            self._heartbeat_suppression_count
            % _SUPPRESSION_LOG_EVERY_N == 0
        ):
            logger.info(
                "GATEWAY_HEALTH_HEARTBEAT_SUPPRESSED_NOMINAL: "
                "kind=%s reason=%s suppression_count=%d — detector "
                "is alive and correctly ignoring tracker noise "
                "(gateway has real traffic evidence or is in "
                "warm-up grace).",
                kind, reason, self._heartbeat_suppression_count,
            )

    def _detect_gateway_deaf(self, now: float) -> FrictionSignal | None:
        """Two failure modes both surface as DISCORD_GATEWAY_DEAF:

        PATTERN A — parser-broken (Codex 2026-05-19 RCA):
          MESSAGE_CREATE socket events observed but on_message
          hasn't fired in the deaf window. The gateway is
          delivering events; discord.py's parser layer or our
          handler is broken.

        PATTERN B — gateway-totally-silent
        (DISCORD-GATEWAY-DEAFNESS-DETECT-V1, 2026-05-25):
          ZERO socket events of any type in the deaf window. The
          gateway connection is up at the heartbeat level but
          dispatch is completely dead. Surfaced 2026-05-25 when
          the bot ran 100+ minutes after restart with no on_message
          events AND no MESSAGE_CREATE counter activity — the
          original detector early-returned on mc_count==0 and
          silently missed the failure for the entire duration.

        Both patterns emit the same signal_type so the existing
        friction pattern lifecycle handles them uniformly; evidence
        names which pattern fired.
        """
        # ─── PATTERN B: total socket silence ──────────────────────
        # Check this FIRST because it dominates when the gateway is
        # fully dead — pattern A's mc_count check would early-return
        # otherwise and hide the failure.
        if self._any_socket_event_ts_provider is not None:
            last_any = self._any_socket_event_ts_provider()
            if last_any > 0:
                silence_sec = now - last_any
                if silence_sec > _GATEWAY_DEAF_WINDOW_SEC:
                    # GATEWAY-OBSERVER-FALSE-POSITIVE-GUARD
                    # (2026-05-28): a quiet personal-bot guild can
                    # cross the 30-min silence threshold during low-
                    # traffic hours without the gateway actually
                    # being deaf. Require a corroborating signal —
                    # elevated heartbeat latency — before emitting.
                    # Mirrors the watchdog's
                    # _DISCORD_DEAF_CORROBORATING_LATENCY_SEC guard.
                    # Setting the env var to 0 restores legacy
                    # silence-only behavior.
                    if _GATEWAY_DEAF_CORROBORATING_LATENCY_SEC > 0:
                        latency_now = self._latency_provider()
                        if (
                            latency_now is None
                            or latency_now <= _GATEWAY_DEAF_CORROBORATING_LATENCY_SEC
                        ):
                            return None
                    return FrictionSignal(
                        signal_type="DISCORD_GATEWAY_DEAF",
                        description=(
                            f"Discord gateway has not delivered any "
                            f"socket event in {silence_sec:.0f}s "
                            f"(threshold {_GATEWAY_DEAF_WINDOW_SEC}s). "
                            f"Connected guild bots receive presence/"
                            f"typing events constantly; total silence "
                            f"this long combined with elevated heartbeat "
                            f"latency indicates the gateway is dispatch-"
                            f"dead despite a healthy heartbeat."
                        ),
                        evidence=[
                            f"any_socket_silence_sec={silence_sec:.0f}",
                            f"window_sec={_GATEWAY_DEAF_WINDOW_SEC}",
                            f"corroborating_latency_threshold_sec={_GATEWAY_DEAF_CORROBORATING_LATENCY_SEC}",
                            "pattern=total_socket_silence",
                        ],
                        context={
                            "space": "",
                            "member_id": "",
                            "window_sec": _GATEWAY_DEAF_WINDOW_SEC,
                            "pattern": "total_socket_silence",
                        },
                        heuristic=False,
                    )

        # ─── PATTERN A: parser broken ─────────────────────────────
        if self._message_create_counter is None:
            return None  # not wired (test fixtures, etc.)
        mc_count = self._message_create_counter.count_in_window(now)
        if mc_count == 0:
            return None  # no recent message-creates; pattern A doesn't apply
        last_on_message = self._inbound_event_ts_provider()
        if last_on_message <= 0:
            # Bot just started; no on_message events yet. Don't
            # false-positive — give it the window to receive one.
            return None
        idle_sec = now - last_on_message
        if idle_sec < _GATEWAY_DEAF_WINDOW_SEC:
            return None  # healthy: we got on_message within window
        return FrictionSignal(
            signal_type="DISCORD_GATEWAY_DEAF",
            description=(
                f"Discord gateway dispatched {mc_count} MESSAGE_CREATE "
                f"events in the last {_GATEWAY_DEAF_WINDOW_SEC}s but "
                f"on_message hasn't fired in {idle_sec:.0f}s"
            ),
            evidence=[
                f"message_create_count_in_window={mc_count}",
                f"on_message_idle_sec={idle_sec:.0f}",
                f"window_sec={_GATEWAY_DEAF_WINDOW_SEC}",
                "pattern=parser_broken",
            ],
            context={
                "space": "",
                "member_id": "",
                "window_sec": _GATEWAY_DEAF_WINDOW_SEC,
                "pattern": "parser_broken",
            },
            heuristic=False,
        )

    def _detect_runner_stuck(self, now: float) -> FrictionSignal | None:
        """V1 stub: returns None unless a runner_inspector is wired
        and reports at least one space with a mailbox item older
        than the configured threshold. Default V1 deployment ships
        without the inspector (full implementation in V1.5 requires
        a small instrumentation hook in _run_space_loop)."""
        if self._runner_inspector is None:
            return None
        stuck_window_sec = int(
            os.getenv("KERNOS_RUNNER_STUCK_WINDOW_SEC", "300")  # 5 min
        )
        try:
            entries = self._runner_inspector()
        except Exception as exc:
            logger.warning(
                "GATEWAY_HEALTH_RUNNER_INSPECT_FAILED: %s", exc,
            )
            return None
        worst_age = 0.0
        worst_space = ""
        for space_id, oldest_ts in entries:
            age = now - oldest_ts
            if age > worst_age:
                worst_age = age
                worst_space = space_id
        if worst_age < stuck_window_sec:
            return None
        return FrictionSignal(
            signal_type="SPACE_RUNNER_STUCK",
            description=(
                f"Space runner for {worst_space} has had a mailbox "
                f"item pending for {worst_age:.0f}s (threshold "
                f"{stuck_window_sec}s)"
            ),
            evidence=[
                f"space={worst_space}",
                f"oldest_mailbox_age_sec={worst_age:.0f}",
                f"stuck_window_sec={stuck_window_sec}",
            ],
            context={
                "space": worst_space,
                "member_id": "",
            },
            heuristic=False,
        )

    def _detect_pool_leak(self) -> FrictionSignal | None:
        """Count CLOSE_WAIT sockets on the bot's process. Pool size
        for httpx default is 20; counts above the threshold imply
        a real connection-lifecycle leak (the Codex 2026-05-19
        analysis fingered ``codex_provider.py`` as one site that
        never aclose()s its httpx.AsyncClient).

        psutil-based; skips silently if psutil isn't importable."""
        try:
            import psutil
            proc = psutil.Process(os.getpid())
            close_wait = sum(
                1 for c in proc.net_connections(kind="tcp")
                if c.status == psutil.CONN_CLOSE_WAIT
            )
        except Exception as exc:
            logger.warning(
                "GATEWAY_HEALTH_POOL_INSPECT_FAILED: %s", exc,
            )
            return None
        if close_wait < _POOL_LEAK_THRESHOLD:
            return None
        return FrictionSignal(
            signal_type="CONNECTION_POOL_LEAK",
            description=(
                f"Process has {close_wait} CLOSE_WAIT sockets "
                f"(threshold {_POOL_LEAK_THRESHOLD}). Likely "
                f"unclosed httpx.AsyncClient / aiohttp.ClientSession "
                f"somewhere; remote-closed keepalives accumulating."
            ),
            evidence=[
                f"close_wait_count={close_wait}",
                f"threshold={_POOL_LEAK_THRESHOLD}",
            ],
            context={
                "space": "",
                "member_id": "",
            },
            heuristic=False,
        )

    # ----- catalog interaction --------------------------------------

    async def _record_signal(self, signal: FrictionSignal) -> None:
        """Write a markdown report + classify into the catalog —
        mirrors the path FrictionObserver uses, intentionally so
        the catalog records are uniform regardless of source."""
        from datetime import datetime, timezone
        import uuid as _uuid
        from pathlib import Path

        # Markdown report under data/diagnostics/friction/ matching
        # FrictionObserver's convention. The report is the durable
        # human-readable artifact; the catalog record is the
        # programmatic one.
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        report_id = _uuid.uuid4().hex[:8]
        report_path = (
            Path(self._data_dir) / "diagnostics" / "friction"
            / f"{ts}_{signal.signal_type}_{report_id}.md"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_lines = [
            f"# {signal.signal_type}",
            "",
            f"**Source:** GatewayHealthObserver (gateway/dispatch layer)",
            f"**When:** {ts}",
            "",
            "## Description",
            signal.description,
            "",
            "## Evidence",
        ]
        for ev in signal.evidence:
            report_lines.append(f"- {ev}")
        report_lines += ["", "## Context", str(signal.context), ""]
        report_path.write_text("\n".join(report_lines), encoding="utf-8")

        # Classify + record against the catalog. Lazy import to
        # mirror FrictionObserver's pattern.
        from kernos.kernel.friction_patterns import (
            LIFECYCLE_ACTIVE,
            LIFECYCLE_REACTIVATED,
            LIFECYCLE_RESOLVED,
            classified_by_for_match_path,
            classify_signal,
        )
        from kernos.utils import utc_now

        await self._pattern_store.ensure_schema(self._data_dir)
        candidates = await self._pattern_store.list_patterns(self._instance_id)
        result = classify_signal(
            signal_type=signal.signal_type,
            signal_description=signal.description,
            candidates=candidates,
        )
        if result is None:
            logger.warning(
                "GATEWAY_HEALTH_SIGNAL_UNCLASSIFIED: signal_type=%s "
                "(pattern not seeded?)",
                signal.signal_type,
            )
            return
        pattern, score, match_path = result
        classified_by = classified_by_for_match_path(match_path)
        observed_at = utc_now()
        if pattern.lifecycle_state in (LIFECYCLE_ACTIVE, LIFECYCLE_REACTIVATED):
            await self._pattern_store.record_occurrence(
                instance_id=self._instance_id,
                pattern_id=pattern.pattern_id,
                observed_at=observed_at,
                report_path=str(report_path),
                classifier_score=score,
                classified_by=classified_by,
                space_id=signal.context.get("space", "") or "",
                member_id=signal.context.get("member_id", "") or "",
            )
        elif pattern.lifecycle_state == LIFECYCLE_RESOLVED:
            await self._pattern_store.record_recurrence(
                instance_id=self._instance_id,
                pattern_id=pattern.pattern_id,
                observed_at=observed_at,
                report_path=str(report_path),
                classifier_score=score,
                classified_by=classified_by,
                space_id=signal.context.get("space", "") or "",
                member_id=signal.context.get("member_id", "") or "",
            )
        self._signals_emitted += 1
        logger.info(
            "GATEWAY_HEALTH_SIGNAL_EMITTED: pattern_id=%s "
            "lifecycle=%s match_path=%s report=%s",
            pattern.pattern_id, pattern.lifecycle_state,
            match_path, report_path,
        )


__all__ = [
    "GatewayHealthObserver",
    "GatewayHealthProviders",
    "_MessageCreateCounter",
]
