"""TriggerEvaluationRuntime — unified time + event trigger runtime.

C2 fills in the predicate evaluator + four temporal relations on
top of the C1b skeleton:

* ``every(cron)`` — heartbeat walks all registered ``every``
  predicates each tick; for each fire window in
  ``(last_evaluated, now]``, claim → dispatch.
* ``on(Y)`` — event-driven path. Post-flush hook walks ``on``
  predicates against incoming events; matching events claim →
  dispatch immediately.
* ``before(Y, N)`` / ``after(Y, N)`` — same event match path;
  computed fire time is ``Y.timestamp ± N``. Future-dated fires
  enter an in-memory pending queue; ``evaluate_now()`` drains the
  queue on each tick.

Both paths converge at ``_claim_and_dispatch(predicate, payload,
fire_window_key)`` which:

1. Calls ``FireOutbox.claim_fire`` (atomic at SQLite level via the
   composite PK).
2. On claim win, calls the wired ``wlp_dispatch(fire_id, ...)``
   hook to run the workflow.
3. On dispatch return, persists the ``workflow_execution_id`` via
   ``mark_dispatched``.

Recovery sweep (``recover()``) closes the Kit must-fix seam
(post-fold AC6 scenario #2): for any pending row past
``claim_lease``, query WLP by fire_id BEFORE re-dispatching. If
WLP has the execution, ``reconcile_to_dispatched`` advances the
outbox row without re-invoking WLP.

C1 evaluator stubs (``evaluate_now``, ``recover``) are replaced.
The interface signatures stay the same so callers don't change.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from kernos.kernel.event_stream import Event
from kernos.kernel.triggers.errors import (
    StaleClaimError,
    TriggerError,
)
from kernos.kernel.triggers.evaluator import (
    PendingDueFire,
    compute_due_at_for_temporal,
    cron_fires_in_window,
    event_matches_selector,
    fire_window_key_for_temporal_match,
    normalize_cron_fire_time,
)
from kernos.kernel.triggers.outbox import FireOutbox
from kernos.kernel.triggers.predicate import (
    TriggerPredicate,
    fire_window_key_for_every,
    validate_predicate,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WLP dispatch hook protocol
# ---------------------------------------------------------------------------

# Async callable matching ExecutionEngine.execute_workflow's
# keyword-only kwargs subset the runtime cares about. The C1a WLP
# substrate already exposes this signature; the runtime imports
# the engine at wiring time, not at module-import time, so the
# triggers module doesn't depend structurally on WLP internals.
WLPDispatchHook = Callable[..., Awaitable[str]]


# WLP execution-status lookup — used by the recovery sweep to
# check whether a dispatched row's workflow actually ran. C2
# wires only the fire_id-based lookup (closes the Kit seam);
# the dispatched-past-lease completion check uses the existing
# ExecutionEngine.get_execution surface.
WLPLookupHook = Callable[[str], Awaitable[str | None]]


def _generate_claim_owner() -> str:
    """Build a stable per-process claim_owner string. Combines
    hostname + pid + a per-process random suffix so a recovered
    claim can be distinguished from a duplicate-claim race within
    the same hostname."""
    return (
        f"runtime:{socket.gethostname()}:"
        f"{os.getpid()}:{int.from_bytes(os.urandom(4), 'big'):08x}"
    )


@dataclass
class _RegisteredPredicate:
    """In-memory record of a registered predicate. C5 will wire
    persistence through the existing TriggerRegistry; for C2 the
    in-memory record is the source of truth during a process
    lifetime."""

    trigger_id: str
    instance_id: str
    workflow_id: str
    predicate: TriggerPredicate
    member_id: str
    active: bool
    last_evaluated: datetime  # for every-cron walk windowing


class TriggerEvaluationRuntime:
    """Unified time + event trigger runtime. Time-driven via
    ``evaluate_now()`` ticks; event-driven via
    ``on_event_observed()`` (called by the post-flush hook in
    production wiring; tests call directly). Dispatch through
    durable :class:`FireOutbox`."""

    def __init__(self) -> None:
        self._outbox: FireOutbox | None = None
        self._heartbeat_seconds: int = 30
        self._stop_event: asyncio.Event | None = None
        self._claim_owner: str = ""
        self._predicates: dict[str, _RegisteredPredicate] = {}
        # Pending future-dated fires from before/after temporal
        # matches. evaluate_now() drains these on each tick.
        self._pending_due_fires: list[PendingDueFire] = []
        # WLP wiring. None when running in test contexts that
        # don't wire WLP — _claim_and_dispatch logs and skips
        # dispatch (the claim still succeeds).
        self._wlp_dispatch: WLPDispatchHook | None = None
        self._wlp_lookup_by_fire_id: WLPLookupHook | None = None

    # -- lifecycle ------------------------------------------------------

    async def start(
        self,
        *,
        data_dir: str,
        heartbeat_seconds: int = 30,
        wlp_dispatch: WLPDispatchHook | None = None,
        wlp_lookup_by_fire_id: WLPLookupHook | None = None,
    ) -> None:
        if self._outbox is not None:
            return
        self._heartbeat_seconds = max(1, int(heartbeat_seconds))
        self._claim_owner = _generate_claim_owner()
        self._outbox = FireOutbox()
        await self._outbox.start(data_dir)
        self._wlp_dispatch = wlp_dispatch
        self._wlp_lookup_by_fire_id = wlp_lookup_by_fire_id
        self._stop_event = asyncio.Event()
        logger.info(
            "WTC v1 runtime started: claim_owner=%s heartbeat=%ds "
            "wlp_wired=%s",
            self._claim_owner, self._heartbeat_seconds,
            "yes" if wlp_dispatch is not None else "no",
        )

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._outbox is not None:
            await self._outbox.stop()
            self._outbox = None
        self._predicates.clear()
        self._pending_due_fires.clear()

    @property
    def outbox(self) -> FireOutbox:
        if self._outbox is None:
            raise RuntimeError("TriggerEvaluationRuntime not started")
        return self._outbox

    @property
    def claim_owner(self) -> str:
        return self._claim_owner

    # -- registration ---------------------------------------------------

    async def register(
        self,
        *,
        trigger_id: str,
        instance_id: str,
        workflow_id: str,
        predicate: TriggerPredicate,
        member_id: str = "",
    ) -> None:
        if self._outbox is None:
            raise RuntimeError("TriggerEvaluationRuntime not started")
        if not trigger_id:
            raise TriggerError("trigger_id is required")
        if not instance_id:
            raise TriggerError("instance_id is required")
        if not workflow_id:
            raise TriggerError("workflow_id is required")

        validate_predicate(predicate)

        self._predicates[trigger_id] = _RegisteredPredicate(
            trigger_id=trigger_id,
            instance_id=instance_id,
            workflow_id=workflow_id,
            predicate=predicate,
            member_id=member_id,
            active=True,
            # First evaluate_now() tick walks the cron from now —
            # not from epoch — so existing `every` predicates
            # don't fire a flood of historical windows on
            # registration.
            last_evaluated=datetime.now(timezone.utc),
        )

    async def deactivate(self, trigger_id: str) -> None:
        record = self._predicates.get(trigger_id)
        if record is not None:
            record.active = False

    async def list_active(self) -> list[dict[str, Any]]:
        return [
            {
                "trigger_id": r.trigger_id,
                "instance_id": r.instance_id,
                "workflow_id": r.workflow_id,
                "predicate": r.predicate,
                "member_id": r.member_id,
                "active": r.active,
            }
            for r in self._predicates.values() if r.active
        ]

    # -- event-driven path ---------------------------------------------

    async def on_event_observed(self, event: Event) -> int:
        """Post-flush hook entry. Walks active predicates whose
        ``temporal_relation.kind`` is ``on``, ``before``, or
        ``after``; for matches:

        * ``on(Y)``: claim + dispatch immediately.
        * ``before/after(Y, N)``: compute due_at = Y.timestamp ±
          N. If due_at <= now, claim + dispatch. Otherwise enqueue
          to ``_pending_due_fires`` for the next ``evaluate_now()``
          tick to drain.

        Returns count of fires claimed during this call (immediate
        + already-due enqueued).
        """
        if self._outbox is None:
            return 0
        fired = 0
        now = datetime.now(timezone.utc)
        for record in list(self._predicates.values()):
            if not record.active:
                continue
            kind = record.predicate.temporal_relation.kind
            if kind == "every":
                continue
            if not event_matches_selector(
                record.predicate.event_selector, event,
            ):
                continue

            y_event_id = getattr(event, "event_id", "")
            if not y_event_id:
                continue

            if kind == "on":
                fwk = fire_window_key_for_temporal_match(
                    kind="on", y_event_id=y_event_id, minutes=0,
                )
                if await self._claim_and_dispatch(
                    record=record,
                    fire_window_key=fwk,
                    payload=_payload_from_event(event),
                ):
                    fired += 1
                continue

            # before / after — compute due_at.
            minutes = record.predicate.temporal_relation.minutes
            due_at = compute_due_at_for_temporal(
                kind=kind,
                y_timestamp=event.timestamp,
                minutes=minutes,
            )
            if due_at is None:
                continue
            fwk = fire_window_key_for_temporal_match(
                kind=kind, y_event_id=y_event_id, minutes=minutes,
            )
            payload = _payload_from_event(event)
            if due_at <= now:
                if await self._claim_and_dispatch(
                    record=record,
                    fire_window_key=fwk,
                    payload=payload,
                ):
                    fired += 1
            else:
                self._pending_due_fires.append(PendingDueFire(
                    trigger_id=record.trigger_id,
                    instance_id=record.instance_id,
                    workflow_id=record.workflow_id,
                    fire_window_key=fwk,
                    payload=payload,
                    due_at=due_at,
                ))
        return fired

    # -- time-driven path -----------------------------------------------

    async def evaluate_now(self) -> int:
        """Heartbeat tick. Walks every-cron predicates for due fires
        in ``(last_evaluated, now]``; drains the pending due-fire
        queue (before/after matches whose due_at has passed).
        Returns count of fires claimed. Idempotent — same window
        won't double-claim because of the deterministic
        fire_window_key + outbox PK.
        """
        if self._outbox is None:
            raise RuntimeError("TriggerEvaluationRuntime not started")
        now = datetime.now(timezone.utc)
        fired = 0

        # 1. every-cron walk.
        for record in list(self._predicates.values()):
            if not record.active:
                continue
            if record.predicate.temporal_relation.kind != "every":
                continue
            cron_expr = record.predicate.temporal_relation.cron_expression
            try:
                fires = cron_fires_in_window(
                    cron_expr,
                    after=record.last_evaluated,
                    until=now,
                )
            except Exception as exc:
                logger.warning(
                    "WTC v1 cron walk failed trigger=%s: %s",
                    record.trigger_id, exc,
                )
                continue
            for fire_time in fires:
                normalized = normalize_cron_fire_time(cron_expr, fire_time)
                fwk = fire_window_key_for_every(cron_expr, normalized)
                payload = {
                    "fire_time": normalized,
                    "cron_expression": cron_expr,
                }
                if await self._claim_and_dispatch(
                    record=record,
                    fire_window_key=fwk,
                    payload=payload,
                ):
                    fired += 1
            record.last_evaluated = now

        # 2. Drain pending due-fires whose due_at has passed.
        still_pending: list[PendingDueFire] = []
        for due in self._pending_due_fires:
            if due.due_at > now:
                still_pending.append(due)
                continue
            record = self._predicates.get(due.trigger_id)
            if record is None or not record.active:
                continue
            if await self._claim_and_dispatch(
                record=record,
                fire_window_key=due.fire_window_key,
                payload=due.payload,
                catch_up=due.catch_up,
            ):
                fired += 1
        self._pending_due_fires = still_pending

        return fired

    # -- dispatch boundary ----------------------------------------------

    async def _claim_and_dispatch(
        self,
        *,
        record: _RegisteredPredicate,
        fire_window_key: str,
        payload: dict[str, Any],
        catch_up: bool = False,
    ) -> bool:
        """Atomic claim → WLP dispatch → mark_dispatched. Returns
        True iff a fresh fire was claimed (and dispatched if WLP
        is wired). False on claim conflict (another process /
        an earlier tick already won) or on dispatch failure
        beyond retry budget.

        Race-safety: the outbox claim is atomic at SQLite level
        via the existing PK. WLP's execute_workflow is itself
        idempotent on fire_id, so a retry on transient dispatch
        failure won't create a second execution.
        """
        if self._outbox is None:
            return False
        record_payload = dict(payload)
        record_payload.setdefault("trigger_id", record.trigger_id)

        try:
            claim = await self._outbox.claim_fire(
                instance_id=record.instance_id,
                trigger_id=record.trigger_id,
                fire_window_key=fire_window_key,
                payload=record_payload,
                claim_owner=self._claim_owner,
                catch_up=catch_up,
            )
        except Exception as exc:
            logger.warning(
                "WTC v1 claim_fire raised trigger=%s: %s",
                record.trigger_id, exc,
            )
            return False
        if claim is None:
            # Already claimed by another path (or earlier tick).
            return False

        if self._wlp_dispatch is None:
            logger.info(
                "WTC v1 claim_only (no WLP wired): fire_id=%s trigger=%s",
                claim.fire_id, record.trigger_id,
            )
            return True

        retries = max(0, record.predicate.dispatch_policy.retry_on_dispatch_failure)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                workflow_execution_id = await self._wlp_dispatch(
                    fire_id=claim.fire_id,
                    workflow_id=record.workflow_id,
                    instance_id=record.instance_id,
                    trigger_event_payload=record_payload,
                    member_id=record.member_id,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "WTC v1 dispatch attempt %d/%d failed "
                    "trigger=%s fire_id=%s: %s",
                    attempt + 1, retries + 1,
                    record.trigger_id, claim.fire_id, exc,
                )
                continue

            try:
                await self._outbox.mark_dispatched(
                    fire_id=claim.fire_id,
                    claim_owner=self._claim_owner,
                    workflow_execution_id=workflow_execution_id,
                )
            except StaleClaimError:
                # Recovery sweep took the row from us — that's fine,
                # WLP is idempotent on fire_id so the second dispatch
                # would have returned the same execution_id.
                logger.info(
                    "WTC v1 mark_dispatched superseded by recovery: "
                    "fire_id=%s",
                    claim.fire_id,
                )
            return True

        # Exhausted retries — mark failed so the row leaves the
        # pending bucket and isn't re-dispatched indefinitely.
        try:
            await self._outbox.mark_failed(
                fire_id=claim.fire_id,
                claim_owner=self._claim_owner,
                error=f"dispatch_failed_after_retries: {last_error}",
            )
        except StaleClaimError:
            pass
        return False

    # -- recovery sweep -------------------------------------------------

    async def recover(self) -> int:
        """Engine-startup recovery sweep. Returns count recovered.
        Idempotent.

        Walks the outbox in two passes:

        1. ``status='pending'`` AND ``claimed_at`` past
           ``claim_lease`` (default 60s). For each row: query WLP
           by fire_id (the C1a lookup). If WLP has an execution,
           reconcile the outbox row to ``dispatched`` without
           re-invoking WLP — this closes the Kit must-fix seam
           (post-fold AC6 scenario #2). If WLP doesn't have it,
           reclaim the row to this runtime's claim_owner and
           re-dispatch with the same fire_id (which is itself
           idempotent at WLP).

        2. ``status='dispatched'`` AND ``dispatched_at`` past
           ``dispatch_lease`` (default 600s). C2 logs these for
           operator visibility; full WLP-completion reconciliation
           lands in C5 alongside the migration adapters.
        """
        if self._outbox is None:
            raise RuntimeError("TriggerEvaluationRuntime not started")
        recovered = 0

        # Pending past lease — close the Kit seam.
        pendings = await self._outbox.find_pending_past_lease(
            claim_lease_seconds=60,
        )
        for record in pendings:
            existing = None
            if self._wlp_lookup_by_fire_id is not None:
                try:
                    existing = await self._wlp_lookup_by_fire_id(
                        record.fire_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "WTC v1 recover: WLP lookup raised "
                        "fire_id=%s: %s", record.fire_id, exc,
                    )
                    existing = None

            if existing:
                # WLP already has the execution. Reconcile outbox row
                # to dispatched without re-invoking WLP.
                ok = await self._outbox.reconcile_to_dispatched(
                    fire_id=record.fire_id,
                    workflow_execution_id=existing,
                )
                if ok:
                    recovered += 1
                    logger.info(
                        "WTC v1 recover: reconciled fire_id=%s to "
                        "execution=%s (Kit seam closure)",
                        record.fire_id, existing,
                    )
                continue

            # WLP doesn't have the execution. Reclaim + redispatch.
            new_record = await self._outbox.reclaim(
                fire_id=record.fire_id,
                new_claim_owner=self._claim_owner,
            )
            if new_record is None:
                # Another sweep took it.
                continue
            predicate_record = self._predicates.get(record.trigger_id)
            if predicate_record is None:
                logger.info(
                    "WTC v1 recover: orphan trigger_id=%s — predicate "
                    "no longer registered, skipping",
                    record.trigger_id,
                )
                continue

            if self._wlp_dispatch is None:
                # No WLP wired (test context); reclaim is enough.
                recovered += 1
                continue

            try:
                workflow_execution_id = await self._wlp_dispatch(
                    fire_id=record.fire_id,
                    workflow_id=predicate_record.workflow_id,
                    instance_id=predicate_record.instance_id,
                    trigger_event_payload=record.payload,
                    member_id=predicate_record.member_id,
                )
            except Exception as exc:
                logger.warning(
                    "WTC v1 recover: redispatch failed fire_id=%s: %s",
                    record.fire_id, exc,
                )
                continue

            try:
                await self._outbox.mark_dispatched(
                    fire_id=record.fire_id,
                    claim_owner=self._claim_owner,
                    workflow_execution_id=workflow_execution_id,
                )
                recovered += 1
            except StaleClaimError:
                # Another runtime got there first; WLP is idempotent
                # so no harm done.
                logger.info(
                    "WTC v1 recover: mark_dispatched superseded "
                    "fire_id=%s", record.fire_id,
                )

        # Dispatched past lease — log for operator visibility. C5
        # adds the WLP-completion query to fully resolve.
        dispatcheds = await self._outbox.find_dispatched_past_lease(
            dispatch_lease_seconds=600,
        )
        if dispatcheds:
            logger.info(
                "WTC v1 recover: %d dispatched rows past lease "
                "(operator triage)", len(dispatcheds),
            )

        return recovered


def _payload_from_event(event: Event) -> dict[str, Any]:
    """Extract the canonical payload shape carried into the
    workflow's trigger_event_payload from an Event."""
    payload = dict(event.payload or {})
    payload.setdefault("event_id", event.event_id)
    payload.setdefault("event_type", event.event_type)
    payload.setdefault("event_timestamp", event.timestamp)
    return payload


__all__ = ["TriggerEvaluationRuntime"]
