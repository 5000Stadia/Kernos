"""First-class event sources for the unified trigger runtime.

WTC v1 C3 substrate. The runtime evaluates predicates against
events flowing through ``kernos.kernel.event_stream``. Different
sources of events plug in via:

* :class:`EventSource` — protocol for anything that emits events
  into the durable stream (calendar polling, scheduler ticks,
  webhook receivers, future external integrations).
* :class:`InternalEventAdapter` — the load-bearing piece. Bridges
  the event_stream's post-flush hook to the runtime's
  ``on_event_observed`` so every flushed event becomes a
  candidate for ``on(Y)`` / ``before(Y, N)`` / ``after(Y, N)``
  predicate matching.
* :class:`CalendarSource` — thin wrapper that emits
  ``calendar.event_observed`` events. The actual polling logic
  remains in the shipped scheduler.py through C4; C5 wires the
  refactored adapter in.
* :class:`SchedulerHeartbeatSource` — emits ``scheduler.tick_due``
  events on heartbeat. Same migration posture: thin emitter in C3,
  C5 strips scheduler.py's direct firing and routes everything
  through this source.

Source authority (Codex mid-batch fold #7): CalendarSource and
SchedulerHeartbeatSource go through the :class:`EmitterRegistry`
with typed source authority (``"calendar"`` / ``"scheduler"``).
This mirrors the CRB pattern and unblocks future source-authority
gates from special-casing trust on UNREGISTERED events from these
substrates.

Out of scope for C3:

* Polling logic itself (calendar API calls, cron parsing) — those
  stay where they are until C5's migration.
* External source contracts (email/Notion observers) — C4.
* End-to-end CRB workflow registration through unified runtime —
  C5.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from kernos.kernel.event_stream import (
    Event,
    EmitterAlreadyRegistered,
    register_post_flush_hook,
    unregister_post_flush_hook,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source authority — registered source_module identities for the v1
# substrate-controlled sources.
# ---------------------------------------------------------------------------


CALENDAR_SOURCE_MODULE: str = "calendar"
SCHEDULER_SOURCE_MODULE: str = "scheduler"


def _get_or_register_emitter(source_module: str):
    """Fetch the EmitterRegistry-bound emitter for ``source_module``,
    registering it if absent. Idempotent across multiple Source
    instances (test fixtures construct fresh sources per test; the
    registry is process-global and persists)."""
    from kernos.kernel.event_stream import emitter_registry
    registry = emitter_registry()
    existing = registry.get(source_module)
    if existing is not None:
        return existing
    try:
        return registry.register(source_module)
    except EmitterAlreadyRegistered:
        # Race against a concurrent first-call. Read back what won.
        winner = registry.get(source_module)
        if winner is None:
            # Should never happen — register raised
            # EmitterAlreadyRegistered, so the registry must hold it.
            raise
        return winner


# ---------------------------------------------------------------------------
# Event type constants — sources emit these; predicates match on them.
# ---------------------------------------------------------------------------


# Calendar source. Carries the observed calendar event in the
# payload so before(Y, N) / after(Y, N) predicates can derive
# Y.timestamp (for due-time math) from the event.
EVENT_TYPE_CALENDAR_OBSERVED: str = "calendar.event_observed"


# Scheduler heartbeat tick. v1 cron predicates evaluate via the
# runtime's heartbeat (every-walk in evaluate_now); the
# scheduler adapter exists so future every-cron-relative events
# can match against tick_due events through the unified path.
EVENT_TYPE_SCHEDULER_TICK_DUE: str = "scheduler.tick_due"


# ---------------------------------------------------------------------------
# EventSource protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EventSource(Protocol):
    """Anything that feeds events into the durable event stream so
    the unified trigger runtime can match predicates against them.

    The protocol is intentionally narrow: a name (for diagnostics
    + adapter registry) and async start/stop. Implementations
    handle their own polling / emission inside start/stop's
    lifecycle.
    """

    name: str

    async def start(self) -> None:
        """Begin emitting events. Idempotent — calling twice is
        safe and a no-op."""

    async def stop(self) -> None:
        """Stop emission and clean up. Idempotent."""


# ---------------------------------------------------------------------------
# InternalEventAdapter — bridges post-flush hook to runtime
# ---------------------------------------------------------------------------


class InternalEventAdapter:
    """Bridges the durable event_stream post-flush hook to the
    trigger runtime's :meth:`TriggerEvaluationRuntime.on_event_observed`.

    Every event flushed through the durable stream — whether
    emitted by Kernos's normal handlers, scheduler ticks, calendar
    observations, or future external sources — becomes a candidate
    for predicate matching via this adapter.

    Failure isolation: exceptions raised by ``on_event_observed``
    are caught here so a single bad predicate can't poison the
    entire flush batch. event_stream's outer hook firing also
    catches, but the inner catch surfaces the trigger_id-level
    detail in logs.
    """

    name = "internal_event_adapter"

    def __init__(self, runtime: Any) -> None:
        # Type the runtime as Any to avoid circular import of
        # TriggerEvaluationRuntime; duck-typing on
        # on_event_observed is sufficient.
        self._runtime = runtime
        self._hook_attached: bool = False

    async def start(self) -> None:
        if self._hook_attached:
            return
        register_post_flush_hook(self._on_post_flush)
        self._hook_attached = True
        logger.info(
            "WTC v1 InternalEventAdapter started — runtime now "
            "consumes flushed events via post-flush hook"
        )

    async def stop(self) -> None:
        if not self._hook_attached:
            return
        unregister_post_flush_hook(self._on_post_flush)
        self._hook_attached = False

    async def _on_post_flush(self, batch: list[Event]) -> None:
        """Hook entry — invoked by event_stream after each
        successful SQLite flush. Walks the batch and feeds each
        event to the runtime's evaluator."""
        for event in batch:
            try:
                await self._runtime.on_event_observed(event)
            except Exception as exc:
                # Per-event isolation: one bad event must not
                # block the rest of the batch.
                logger.warning(
                    "WTC v1 InternalEventAdapter: on_event_observed "
                    "raised for event_id=%s type=%s — %s",
                    getattr(event, "event_id", "?"),
                    getattr(event, "event_type", "?"),
                    exc,
                )


# ---------------------------------------------------------------------------
# CalendarSource — thin emitter for calendar.event_observed
# ---------------------------------------------------------------------------


class CalendarSource:
    """Emits ``calendar.event_observed`` events into the durable
    stream when the calendar polling layer reports an observed
    calendar event.

    v1 (C3) ships the emitter shape; the actual polling stays in
    the shipped ``scheduler.py`` calendar code. C5's migration
    rewires that polling to call :meth:`emit_observed` instead of
    firing triggers directly. Until then the emitter is exposed
    for tests + early adopters.

    Payload shape (used by ``before/after(Y, N)`` predicates'
    due-time math):

    .. code-block:: python

        {
            "calendar_event_id": str,
            "summary": str,
            "start_iso": str,           # ISO datetime — used as Y.timestamp
            "end_iso": str,
            "calendar_id": str,
            ...                          # additional fields pass through
        }
    """

    name = "calendar_source"

    def __init__(self, *, instance_id: str) -> None:
        self._instance_id = instance_id
        self._started: bool = False
        # Bound to the EmitterRegistry-issued emitter on first
        # access; lazy so test fixtures that reset the registry
        # between tests don't latch onto a stale handle.
        self._emitter = None

    async def start(self) -> None:
        # No-op in C3; scheduler.py owns the polling loop today.
        # C5 starts a real polling task here.
        self._started = True

    async def stop(self) -> None:
        self._started = False

    def _get_emitter(self):
        if self._emitter is None:
            self._emitter = _get_or_register_emitter(
                CALENDAR_SOURCE_MODULE,
            )
        return self._emitter

    async def emit_observed(
        self,
        *,
        calendar_event_id: str,
        summary: str = "",
        start_iso: str = "",
        end_iso: str = "",
        calendar_id: str = "",
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Emit a ``calendar.event_observed`` event into the
        durable stream with ``envelope.source_module="calendar"``
        (Codex mid-batch fold #7). Returns the substrate-generated
        event_id so callers can correlate the emission with the
        eventual Event row read back via the stream.
        """
        payload: dict[str, Any] = {
            "calendar_event_id": calendar_event_id,
            "summary": summary,
            "start_iso": start_iso,
            "end_iso": end_iso,
            "calendar_id": calendar_id,
        }
        if extra:
            payload.update(extra)
        return await self._get_emitter().emit(
            instance_id=self._instance_id,
            event_type=EVENT_TYPE_CALENDAR_OBSERVED,
            payload=payload,
        )


# ---------------------------------------------------------------------------
# SchedulerHeartbeatSource — emits scheduler.tick_due events
# ---------------------------------------------------------------------------


class SchedulerHeartbeatSource:
    """Emits ``scheduler.tick_due`` events on heartbeat ticks.

    v1 (C3) ships the emitter; C5's migration consolidates the
    shipped scheduler.py heartbeat into a single tick that calls
    both the runtime's ``evaluate_now()`` and this source's
    ``emit_tick`` (so future predicates can match on tick events).

    The tick payload carries the absolute tick timestamp +
    optional metadata (operator-supplied "tick reason" for
    diagnostic clarity). Predicates rarely match on raw tick
    events directly — most cron-driven workflows use
    ``every(cron)`` and run via the heartbeat walk in
    ``evaluate_now()``. The tick-due event_type exists for the
    cases where a predicate genuinely wants the tick boundary
    (e.g., "fire when a tick lands AND condition X" via AND
    composition in the event_selector).
    """

    name = "scheduler_heartbeat_source"

    def __init__(self, *, instance_id: str) -> None:
        self._instance_id = instance_id
        self._started: bool = False
        self._emitter = None

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    def _get_emitter(self):
        if self._emitter is None:
            self._emitter = _get_or_register_emitter(
                SCHEDULER_SOURCE_MODULE,
            )
        return self._emitter

    async def emit_tick(
        self,
        *,
        tick_timestamp_iso: str,
        reason: str = "heartbeat",
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Emit a ``scheduler.tick_due`` event with
        ``envelope.source_module="scheduler"`` (Codex mid-batch
        fold #7). Returns the substrate-generated event_id."""
        payload: dict[str, Any] = {
            "tick_timestamp": tick_timestamp_iso,
            "reason": reason,
        }
        if extra:
            payload.update(extra)
        return await self._get_emitter().emit(
            instance_id=self._instance_id,
            event_type=EVENT_TYPE_SCHEDULER_TICK_DUE,
            payload=payload,
        )


__all__ = [
    "CALENDAR_SOURCE_MODULE",
    "CalendarSource",
    "EVENT_TYPE_CALENDAR_OBSERVED",
    "EVENT_TYPE_SCHEDULER_TICK_DUE",
    "EventSource",
    "InternalEventAdapter",
    "SCHEDULER_SOURCE_MODULE",
    "SchedulerHeartbeatSource",
]
