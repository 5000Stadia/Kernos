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
    os.getenv("KERNOS_GATEWAY_DEAF_WINDOW_SEC", "600"),  # 10 min
)
_HEARTBEAT_THRESHOLD_SEC = float(
    os.getenv("KERNOS_DISCORD_WATCHDOG_LATENCY_THRESHOLD_SEC", "60"),
)
_POOL_LEAK_THRESHOLD = int(
    os.getenv("KERNOS_HTTP_POOL_CLOSE_WAIT_THRESHOLD", "30"),
)


@dataclass
class _MessageCreateCounter:
    """Bounded window of MESSAGE_CREATE socket-event timestamps.

    Wired from ``server.py``'s ``on_socket_event_type`` handler.
    Window-bounded so memory is constant regardless of message rate.
    """
    _events: deque[float]

    def __init__(self, window_sec: int) -> None:
        # maxlen large enough for high-throughput sessions; window
        # filtering happens at query time.
        self._events = deque(maxlen=10_000)
        self._window_sec = window_sec

    def record(self, ts: float) -> None:
        self._events.append(ts)

    def count_in_window(self, now: float) -> int:
        cutoff = now - self._window_sec
        # deque is unordered for membership but ordered for
        # insertion; events come in monotonic time, so a left-pop
        # would work, but we keep the deque immutable here and just
        # count to avoid mutating during read.
        return sum(1 for ts in self._events if ts >= cutoff)


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
        message_create_counter: _MessageCreateCounter,
        runner_inspector: Callable[[], list[tuple[str, float]]] | None = None,
        poll_interval_sec: int = _POLL_INTERVAL_SEC,
    ) -> None:
        self._instance_id = instance_id
        self._data_dir = data_dir
        self._pattern_store = pattern_store
        self._latency_provider = latency_provider
        self._inbound_event_ts_provider = inbound_event_ts_provider
        self._message_create_counter = message_create_counter
        self._runner_inspector = runner_inspector
        self._poll_interval_sec = max(1, poll_interval_sec)
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        # Test-introspection counters
        self._poll_count = 0
        self._signals_emitted = 0

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "GATEWAY_HEALTH_OBSERVER_STARTED instance=%s "
            "poll_interval=%ds deaf_window=%ds heartbeat_threshold=%.1fs "
            "pool_leak_threshold=%d",
            self._instance_id, self._poll_interval_sec,
            _GATEWAY_DEAF_WINDOW_SEC, _HEARTBEAT_THRESHOLD_SEC,
            _POOL_LEAK_THRESHOLD,
        )

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
        detector — one detector raising doesn't skip the others."""
        import time as _time
        now = _time.time()
        detectors = [
            ("discord-heartbeat-blocked", self._detect_heartbeat_blocked),
            ("discord-gateway-deaf", lambda: self._detect_gateway_deaf(now)),
            ("space-runner-stuck", lambda: self._detect_runner_stuck(now)),
            ("discord-connection-pool-leak", self._detect_pool_leak),
        ]
        for name, fn in detectors:
            try:
                signal = fn()
                if signal is not None:
                    await self._record_signal(signal)
            except Exception as exc:
                logger.warning(
                    "GATEWAY_HEALTH_DETECTOR_FAILED: detector=%s exc=%s",
                    name, exc,
                )

    # ----- detectors -------------------------------------------------

    def _detect_heartbeat_blocked(self) -> FrictionSignal | None:
        """Mirror the existing watchdog's heartbeat check, but emit
        as a FrictionSignal instead of incrementing a strike
        counter. V1: detection only. V3 will fold the watchdog's
        restart action into this pattern's remediation policy."""
        latency = self._latency_provider()
        if latency is None:
            reason = "client.latency is None"
        else:
            try:
                finite = math.isfinite(latency)
            except (TypeError, ValueError):
                reason = f"client.latency non-numeric: {latency!r}"
            else:
                if not finite:
                    reason = f"client.latency non-finite: {latency}"
                elif latency <= 0:
                    reason = f"client.latency non-positive: {latency}"
                elif latency > _HEARTBEAT_THRESHOLD_SEC:
                    reason = (
                        f"client.latency={latency:.1f}s exceeds "
                        f"threshold {_HEARTBEAT_THRESHOLD_SEC}s"
                    )
                else:
                    return None  # healthy
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

    def _detect_gateway_deaf(self, now: float) -> FrictionSignal | None:
        """MESSAGE_CREATE socket events observed but on_message
        hasn't fired in the configured window → discord.py parser
        layer is broken (or our handler is broken). Per Codex's
        2026-05-19 analysis this is rare in stock discord.py but
        possible; if it happens we want loud signal."""
        if self._message_create_counter is None:
            return None  # not wired (test fixtures, etc.)
        mc_count = self._message_create_counter.count_in_window(now)
        if mc_count == 0:
            return None  # no recent message-creates; can't tell deaf from idle
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
            ],
            context={
                "space": "",
                "member_id": "",
                "window_sec": _GATEWAY_DEAF_WINDOW_SEC,
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
    "_MessageCreateCounter",
]
