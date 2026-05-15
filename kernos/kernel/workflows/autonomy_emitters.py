"""Spec 6 autonomy-loop event emitters.

Two production-assembly-owned emitters that translate substrate-side
signals into the canonical autonomy-loop event types the
self_improvement workflow's triggers match on:

  * ``FrictionPatternFrequencyEmitter`` — subscribes to
    ``friction.pattern_reactivated`` (already emitted by
    FrictionPatternStore.record_recurrence when the recurrence
    threshold crosses) and emits
    ``friction.pattern_frequency_threshold_exceeded`` with
    ``active_epoch`` in the payload for downstream emitter dedup.
    Post-flush-hook driven (event-responsive, not polling).

  * ``CodingSessionBridgeResponseEmitter`` — polls the coding-session
    bridge's responses directory for newly-arrived response files
    and triggers ``handle_read_coding_session_response`` for each
    unprocessed one, which fires
    ``coding_consult.response_received`` exactly once per response
    (O_EXCL sentinel dedup is owned by coding_session_bridge).
    Polling-based because the bridge writes files; no source event
    exists to subscribe to.

Both emitters share the lifecycle contract enforced by
ExecutionEngine.register_emitter (Spec 6 commit 1):

  * ``async def start()`` — register hooks / kick off polling task.
  * ``async def stop()`` — unregister hooks / cancel polling task.
  * Engine.stop() invokes each emitter's stop() during teardown.

Production assembly (bring_up_substrate, Spec 6 commit 6) constructs
each emitter AFTER the helper has registered the self_improvement
workflow's trigger predicates so the events route to a known
destination (Spec 6 B3 bring-up ordering).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from kernos.kernel import event_stream
from kernos.kernel.event_stream import Event

if TYPE_CHECKING:
    from kernos.kernel.friction_patterns import FrictionPatternStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FrictionPatternFrequencyEmitter
# ---------------------------------------------------------------------------


class FrictionPatternFrequencyEmitter:
    """Translates ``friction.pattern_reactivated`` events into
    ``friction.pattern_frequency_threshold_exceeded`` events with the
    pattern's current ``active_epoch`` in the payload.

    The translation is the canonical autonomy-loop trigger event —
    ``friction.pattern_reactivated`` is the substrate-internal signal
    that record_recurrence emits when the threshold crosses; the
    threshold-exceeded event is the workflow-facing semantic event
    (its name describes the autonomy-loop's interest, not the
    substrate machinery).

    Active-epoch enrichment matters because the workflow's downstream
    consumers (the autonomy-loop helper, dedup tracking) need to know
    which activation episode this event refers to — a pattern can
    reactivate multiple times across its lifetime, and each episode
    is a distinct autonomy-loop turn.
    """

    def __init__(
        self,
        *,
        instance_id: str,
        pattern_store: "FrictionPatternStore",
    ) -> None:
        self._instance_id = instance_id
        self._pattern_store = pattern_store
        self._started = False
        # Track whether we've registered the post-flush hook so stop()
        # can cleanly unregister even if start was called multiple
        # times accidentally.
        self._hook_registered = False
        # Test introspection: number of translated emissions.
        self._emit_count = 0
        # Spec 6 commit 7 Codex round-1 B2 fold: per-pattern dedup by
        # active_epoch. The substrate's reactivation increments
        # active_epoch monotonically per instance (Spec 6 commit 1);
        # the emitter tracks the last epoch translated per
        # (instance_id, pattern_id) and only emits when the observed
        # epoch is strictly greater. This closes the v1 closure
        # invariant: one activation episode → one autonomy-loop turn.
        # Multiple friction.pattern_reactivated events for the same
        # episode (replay, reentrant emission, restart-rehydration)
        # collapse to the canonical first emission. In-memory state
        # for v1; restart clears it and the first post-restart event
        # per pattern is treated as the canonical fire — operational
        # evidence will inform whether durable persistence is needed
        # for v2 per the V1 operational verification scope discipline.
        self._last_emitted_epoch: dict[str, int] = {}

    async def start(self) -> None:
        """Register the post-flush hook. Idempotent."""
        if self._started:
            return
        event_stream.register_post_flush_hook(self._on_flush)
        self._hook_registered = True
        self._started = True
        logger.info(
            "FRICTION_PATTERN_FREQUENCY_EMITTER_STARTED instance_id=%s",
            self._instance_id,
        )

    async def stop(self) -> None:
        """Unregister the post-flush hook. Idempotent."""
        if not self._started:
            return
        if self._hook_registered:
            event_stream.unregister_post_flush_hook(self._on_flush)
            self._hook_registered = False
        self._started = False
        logger.info(
            "FRICTION_PATTERN_FREQUENCY_EMITTER_STOPPED instance_id=%s",
            self._instance_id,
        )

    async def _on_flush(self, batch: list[Event]) -> None:
        """Post-flush hook: scan the just-flushed batch for
        friction.pattern_reactivated events scoped to our instance,
        look up the pattern's active_epoch, and emit the
        threshold-exceeded translation."""
        for event in batch:
            if event.event_type != "friction.pattern_reactivated":
                continue
            if event.instance_id != self._instance_id:
                continue
            pattern_id = (event.payload or {}).get("pattern_id", "")
            if not pattern_id:
                continue
            try:
                pattern = await self._pattern_store.get_pattern(
                    self._instance_id, pattern_id,
                )
            except Exception as exc:
                logger.warning(
                    "FRICTION_PATTERN_FREQUENCY_EMITTER_LOOKUP_FAILED "
                    "pattern_id=%s error=%s",
                    pattern_id, exc,
                )
                continue
            if pattern is None:
                # Pattern row vanished between event emit and lookup
                # (extremely unusual; log and skip).
                logger.warning(
                    "FRICTION_PATTERN_FREQUENCY_EMITTER_PATTERN_MISSING "
                    "instance_id=%s pattern_id=%s",
                    self._instance_id, pattern_id,
                )
                continue
            # B2 dedup: only emit when active_epoch strictly increased
            # for this pattern. Same-epoch re-fires (replay, reentrant
            # emission) collapse to the canonical first emission so
            # downstream workflow executions are 1:1 with activation
            # episodes.
            last_epoch = self._last_emitted_epoch.get(pattern_id, 0)
            if pattern.active_epoch <= last_epoch:
                logger.debug(
                    "FRICTION_PATTERN_FREQUENCY_EMITTER_DEDUPED "
                    "pattern_id=%s active_epoch=%d last_emitted=%d",
                    pattern_id, pattern.active_epoch, last_epoch,
                )
                continue
            self._last_emitted_epoch[pattern_id] = pattern.active_epoch
            translated_payload = {
                "pattern_id": pattern_id,
                "active_epoch": pattern.active_epoch,
                "lifecycle_state": pattern.lifecycle_state,
                "recurrence_count": (
                    event.payload.get("recurrence_count", 0)
                    if event.payload else 0
                ),
                "reactivated_at": (
                    event.payload.get("reactivated_at", "")
                    if event.payload else ""
                ),
                "source_event_id": event.event_id,
            }
            try:
                await event_stream.emit(
                    self._instance_id,
                    "friction.pattern_frequency_threshold_exceeded",
                    translated_payload,
                )
                self._emit_count += 1
            except Exception as exc:
                logger.warning(
                    "FRICTION_PATTERN_FREQUENCY_EMITTER_EMIT_FAILED "
                    "pattern_id=%s error=%s",
                    pattern_id, exc,
                )


# ---------------------------------------------------------------------------
# CodingSessionBridgeResponseEmitter
# ---------------------------------------------------------------------------


class CodingSessionBridgeResponseEmitter:
    """Polls the coding-session bridge's responses directory for
    newly-arrived response files and triggers
    ``handle_read_coding_session_response`` for each unprocessed one.

    The bridge's existing
    ``coding_session_bridge._emit_response_received_once`` owns the
    O_EXCL sentinel dedup, so this emitter is safe to call repeatedly
    against the same request_id — only the first call per request_id
    actually fires the event.

    Polling rather than filesystem-event-driven because external
    tooling (CC session, operator) writes the response files
    asynchronously and we don't control when. A short polling cadence
    keeps autonomy-loop latency bounded without requiring platform-
    specific filesystem watchers.
    """

    def __init__(
        self,
        *,
        instance_id: str,
        data_dir: str,
        poll_interval_s: float = 2.0,
    ) -> None:
        self._instance_id = instance_id
        self._data_dir = data_dir
        self._poll_interval_s = max(0.1, poll_interval_s)
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        # Test introspection: number of polling iterations + emits
        # triggered.
        self._poll_count = 0
        self._emit_count = 0

    async def start(self) -> None:
        """Spawn the polling task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "CODING_SESSION_BRIDGE_RESPONSE_EMITTER_STARTED "
            "instance_id=%s poll_interval_s=%.2f",
            self._instance_id, self._poll_interval_s,
        )

    async def stop(self) -> None:
        """Signal the polling task to exit and await its completion.
        Idempotent."""
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
            "CODING_SESSION_BRIDGE_RESPONSE_EMITTER_STOPPED instance_id=%s",
            self._instance_id,
        )

    async def _poll_loop(self) -> None:
        """Drain unprocessed responses, then wait poll_interval_s
        before the next pass. Exits cleanly on stop_event.set()."""
        while True:
            try:
                await self._drain_responses()
            except Exception as exc:
                logger.warning(
                    "CODING_SESSION_BRIDGE_RESPONSE_EMITTER_POLL_FAILED "
                    "error=%s",
                    exc,
                )
            self._poll_count += 1
            assert self._stop_event is not None
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval_s,
                )
                return  # stop signalled
            except asyncio.TimeoutError:
                continue  # next poll pass

    async def _drain_responses(self) -> None:
        """One polling pass. For each *.json response file without a
        matching *.emitted sentinel, trigger
        handle_read_coding_session_response (which fires the
        coding_consult.response_received event via the existing
        O_EXCL-claim emit-once primitive)."""
        from kernos.kernel.coding_session_bridge import (
            handle_read_coding_session_response,
        )

        responses_dir = (
            Path(self._data_dir) / self._instance_id
            / "coding_session_bridge" / "responses"
        )
        if not responses_dir.exists():
            return
        for response_file in responses_dir.iterdir():
            if not response_file.is_file():
                continue
            if response_file.suffix != ".json":
                continue
            request_id = response_file.stem
            sentinel = responses_dir / f"{request_id}.emitted"
            if sentinel.exists():
                # Already emitted; skip.
                continue
            try:
                await handle_read_coding_session_response(
                    instance_id=self._instance_id,
                    data_dir=self._data_dir,
                    request_id=request_id,
                )
                self._emit_count += 1
            except Exception as exc:
                logger.warning(
                    "CODING_SESSION_BRIDGE_RESPONSE_EMITTER_READ_FAILED "
                    "request_id=%s error=%s",
                    request_id, exc,
                )


__all__ = [
    "CodingSessionBridgeResponseEmitter",
    "FrictionPatternFrequencyEmitter",
]
