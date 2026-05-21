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
import os
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
        # Spec 6 commit 7 Codex round-1 B2 fold + round-2 LOW 1
        # documentation: per-pattern dedup by active_epoch.
        #
        # SAME-PROCESS semantics. The substrate's reactivation
        # increments active_epoch monotonically per instance
        # (Spec 6 commit 1); the emitter tracks the last epoch
        # translated per (instance_id, pattern_id) and only emits
        # when the observed epoch is strictly greater. This closes
        # the v1 closure invariant: one activation episode → one
        # autonomy-loop turn. Same-process duplicate
        # friction.pattern_reactivated events for the same episode
        # (reentrant emission, post-flush hook re-firing, in-process
        # replay) collapse to the canonical first emission.
        #
        # ACROSS-RESTART semantics — explicit no-replay assumption.
        # event_stream's post-flush hook does NOT replay
        # already-persisted batches at startup; the InternalEventAdapter
        # only sees freshly-flushed events. Without a replay source
        # that re-emits historical friction.pattern_reactivated
        # events post-restart, this in-memory dedup is sufficient
        # for v1 — the WTC fire_id idempotency at the runtime
        # protects against duplicate dispatch within a single
        # cluster of post-flush-hook invocations.
        # If a replay source is later introduced (e.g., an
        # event-stream catch-up mode), durable persistence of
        # ``_last_emitted_epoch`` becomes load-bearing. Per the V1
        # operational verification scope discipline, the lift to
        # durable state folds in alongside the replay source's spec
        # rather than upfront.
        self._last_emitted_epoch: dict[str, int] = {}
        # Codex round-3 MEDIUM 1 fold: serialize the check → emit →
        # claim sequence across concurrent post-flush hook
        # invocations. event_stream's post-flush hook can fire from
        # multiple _flush_once() entry points without a global hook-
        # serialization lock; two concurrent invocations for the
        # same (pattern_id, active_epoch) could both observe no
        # claim, both emit, then both claim. A per-emitter
        # asyncio.Lock around the critical section closes that race.
        # Held only for the small dict-read + emit + dict-write
        # window; the per-event work inside _on_flush is otherwise
        # unaffected.
        self._dedup_lock = asyncio.Lock()

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
        """Post-flush hook: scan the just-flushed batch for two
        canonical inputs scoped to our instance and translate each
        into ``friction.pattern_frequency_threshold_exceeded``:

        1. ``friction.pattern_reactivated`` — emitted when a resolved
           pattern crosses its recurrence threshold (existing path).
        2. ``friction.pattern_active_frequency_threshold_crossed`` —
           emitted by ``record_occurrence`` when an active pattern's
           count just crosses its ``reactivation_threshold``
           (SELF-CONTROLLED-LOOP-LIVENESS-V1 2026-05-21 — without
           this path, active patterns accumulate occurrences forever
           and the self-improvement workflow trigger never matches).

        Both inputs share the same ``active_epoch`` dedup discipline
        so the workflow trigger sees exactly one canonical event per
        epoch, regardless of which input fired first.
        """
        _TRANSLATABLE_EVENT_TYPES = {
            "friction.pattern_reactivated",
            "friction.pattern_active_frequency_threshold_crossed",
        }
        for event in batch:
            if event.event_type not in _TRANSLATABLE_EVENT_TYPES:
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
            # B2 dedup + round-3 concurrency: serialize the check →
            # emit → claim sequence per emitter so concurrent
            # post-flush hook invocations for the same (pattern_id,
            # active_epoch) cannot both observe no claim and both
            # emit. The lock is per-emitter (one instance per
            # bring-up); per-pattern locks would be a future
            # refinement if throughput becomes a concern.
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
            async with self._dedup_lock:
                # Re-check inside the lock (compare-and-set
                # semantics).
                last_epoch = self._last_emitted_epoch.get(pattern_id, 0)
                if pattern.active_epoch <= last_epoch:
                    logger.debug(
                        "FRICTION_PATTERN_FREQUENCY_EMITTER_DEDUPED "
                        "pattern_id=%s active_epoch=%d "
                        "last_emitted=%d",
                        pattern_id, pattern.active_epoch, last_epoch,
                    )
                    continue
                # Codex round-2 MEDIUM 1 fold: update dedup state
                # only AFTER the emit succeeds. Held inside the
                # lock for round-3 MEDIUM 1's compare-and-set
                # serialization. event_stream.emit is currently
                # fire-and-forget so it's quick; for v1 the
                # serialization overhead is negligible.
                try:
                    await event_stream.emit(
                        self._instance_id,
                        "friction.pattern_frequency_threshold_exceeded",
                        translated_payload,
                    )
                except Exception as exc:
                    logger.warning(
                        "FRICTION_PATTERN_FREQUENCY_EMITTER_EMIT_FAILED "
                        "pattern_id=%s error=%s",
                        pattern_id, exc,
                    )
                    # Don't update dedup state; next same-epoch
                    # event for this pattern can retry.
                    continue
                # Emit succeeded — claim the dedup slot.
                self._last_emitted_epoch[pattern_id] = pattern.active_epoch
                self._emit_count += 1


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
        wake_callback=None,
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
        # AUTO-WAKE-V1 (2026-05-19): optional callback invoked once
        # per newly-arrived response so the MessageHandler can inject
        # a wake-turn into the originating space's mailbox. Signature:
        # `async def wake_callback(payload: dict) -> None` where
        # payload carries instance_id, request_id, target,
        # investigation_outcome, summary, originating_member_id,
        # originating_space. None = legacy behavior (audit-only,
        # no wake).
        self._wake_callback = wake_callback

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
        from kernos.utils import _safe_name

        # Pre-existing bug fix (2026-05-19): instance_id was used
        # raw here while the bridge_watcher + coding_session_bridge
        # both use ``_safe_name(instance_id)``. With colon-bearing
        # instance ids like ``discord:364303223047323649``, the raw
        # form pointed at a non-existent dir and the emitter loop
        # has been silently no-oping in prod. Agent-initiated
        # ``read_coding_session_response`` calls were doing the
        # emission work instead. Fixing here so the poll path actually
        # finds responses — required for AUTO-WAKE-V1.
        responses_dir = (
            Path(self._data_dir) / _safe_name(self._instance_id)
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
            wake_sentinel = responses_dir / f"{request_id}.waked"

            # Audit emission path — gated on .emitted sentinel.
            if not sentinel.exists():
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
                    continue

            # AUTO-WAKE-V1 path — INDEPENDENT of audit dedup. Both
            # emission paths (this emitter loop + agent-initiated
            # read_coding_session_response tool calls) race on the
            # .emitted sentinel; whichever wins skips the other.
            # The wake callback must fire regardless of which path
            # won the audit race, so it gets its own .waked sentinel
            # via O_CREAT|O_EXCL. This guarantees the wake fires
            # exactly once per response, but isn't blocked by an
            # agent that polled the response first.
            if self._wake_callback is not None and not wake_sentinel.exists():
                # Atomic claim — only one polling pass actually fires
                # the wake even if drain is somehow concurrent.
                try:
                    fd = os.open(
                        str(wake_sentinel),
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o644,
                    )
                except FileExistsError:
                    # Another pass already claimed the wake; skip.
                    continue
                except OSError as exc:
                    logger.warning(
                        "CODING_SESSION_BRIDGE_WAKE_SENTINEL_FAILED "
                        "request_id=%s error=%s",
                        request_id, exc,
                    )
                    continue
                try:
                    os.write(fd, b"waked")
                finally:
                    os.close(fd)
                try:
                    payload = self._read_request_payload(
                        response_file=response_file,
                        request_id=request_id,
                    )
                    await self._wake_callback(payload)
                except Exception as wake_exc:
                    logger.warning(
                        "CODING_SESSION_BRIDGE_WAKE_CALLBACK_FAILED "
                        "request_id=%s error=%s",
                        request_id, wake_exc,
                    )

    def _read_request_payload(
        self, *, response_file, request_id: str,
    ) -> dict:
        """Construct the wake payload by joining the response file
        (carries summary + investigation_outcome) with the original
        request file (carries originating_space + member_id + target).
        Missing fields default to empty strings rather than raising —
        the wake is best-effort surfacing, not a load-bearing path.
        """
        import json as _json
        # Response side
        try:
            response_data = _json.loads(
                response_file.read_text(encoding="utf-8")
            )
        except Exception:
            response_data = {}
        # Request side (sibling dir, same id)
        request_file = (
            response_file.parent.parent / "requests"
            / f"{request_id}.json"
        )
        try:
            request_data = _json.loads(
                request_file.read_text(encoding="utf-8")
            )
        except Exception:
            request_data = {}
        return {
            "request_id": request_id,
            "instance_id": self._instance_id,
            "target": response_data.get("target", ""),
            "investigation_outcome": response_data.get(
                "investigation_outcome", "",
            ),
            "summary": response_data.get("summary", ""),
            "originating_member_id": request_data.get(
                "originating_member_id", "",
            ),
            "originating_space": request_data.get(
                "originating_space", "",
            ),
            "originating_conversation_id": request_data.get(
                "originating_conversation_id", "",
            ),
        }


__all__ = [
    "CodingSessionBridgeResponseEmitter",
    "FrictionPatternFrequencyEmitter",
]
