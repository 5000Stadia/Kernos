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

Recovery sweep (``recover()``) closes the the design review must-fix seam
(post-fold AC6 scenario #2): for any pending row past
``claim_lease``, query WLP by fire_id BEFORE re-dispatching. If
WLP has the execution, ``reconcile_to_dispatched`` advances the
outbox row without re-invoking WLP.

C1 evaluator stubs (``evaluate_now``, ``recover``) are replaced.
The interface signatures stay the same so callers don't change.

Concurrency model (Codex mid-batch fold #6): the runtime is
designed for a single-process, single-asyncio-loop deployment.
``_predicates`` and ``_pending_due_fires`` mutation paths are
cooperative — register / deactivate / on_event_observed /
evaluate_now never preempt each other across an ``await`` to the
same in-memory dict / list. Cross-process safety is provided by
SQLite at the outbox layer, not by in-memory locks.
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

# WTC v1 C6: substrate-emitted event when a cron predicate's
# evaluate_now walk detects more than one missed fire in
# (last_evaluated, now]. The event records the missed window so
# audit/diagnostics can show what the runtime chose not to fire
# (skip policy) or collapsed into the single catch-up fire
# (catch_up policy).
EVENT_TYPE_WORKFLOW_MISSED_FIRE: str = "workflow.missed_fire"
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
# Spec 5 15th amendment B2 sentinel — engine returns this from
# execute_workflow when the workflow is registered_not_activated /
# deactivated. The runtime detects it in both the active-claim and
# recovery-sweep dispatch paths and routes to terminal-skip via
# mark_failed (with sentinel as last_error) so an authoring-inactive
# workflow doesn't leave a dispatched row pointing at a non-existent
# execution. No structural circular: execution_engine does not
# import from kernos.kernel.triggers at module-load time.
from kernos.kernel.workflows.execution_engine import (
    EXECUTE_SKIPPED_AUTHORING_INACTIVE as _EXECUTE_SKIPPED_AUTHORING_INACTIVE,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WLP dispatch hook protocol
# ---------------------------------------------------------------------------

# Async callable matching ExecutionEngine.execute_workflow's
# keyword-only kwargs subset the runtime cares about. The C1a WLP
# substrate already exposes this signature; the dispatch HOOK itself
# is wired via runtime.start(wlp_dispatch=...) at bring-up time, so
# the triggers module doesn't depend structurally on WLP internals
# through this hook. The Spec 5 15th amendment added a small
# module-load-time import above for the ``EXECUTE_SKIPPED_AUTHORING_INACTIVE``
# sentinel — that's a single value constant, not a structural dependency,
# and execution_engine does not import from kernos.kernel.triggers at
# module-load time so no circular hazard.
WLPDispatchHook = Callable[..., Awaitable[str]]


# WLP execution-status lookup — used by the recovery sweep to
# check whether a dispatched row's workflow actually ran. C2
# wires only the fire_id-based lookup (closes the the design review seam);
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


def _extract_event_type_filter(
    event_selector: dict[str, Any] | None,
) -> str | None:
    """Return the event_type value when the selector is a simple
    ``{"op": "eq", "path": "event_type", "value": X}`` (the default
    shape produced by the CRB Compiler for minimal trigger
    descriptors). Returns None for richer selectors — those land
    in the unfiltered bucket and walk on every event.

    Codex mid-batch fold #5: this is the prefilter key for the
    runtime's event_type index. Common case (CRB descriptors with
    ``{"event_type": "X"}``) translates to a simple eq selector
    and benefits; complex AND/OR/payload-field selectors stay
    in the all-walk path.
    """
    if not isinstance(event_selector, dict):
        return None
    if event_selector.get("op") != "eq":
        return None
    if event_selector.get("path") != "event_type":
        return None
    value = event_selector.get("value")
    if not isinstance(value, str) or not value:
        return None
    return value


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
        # Event-type prefilter index (Codex mid-batch fold #5).
        # Maps event_type → set of trigger_ids whose event_selector
        # is a simple ``eq event_type==X``. Predicates with richer
        # selectors (composite AST, payload-field filters, etc.)
        # land in ``_predicates_unfiltered`` and walk on every event.
        # on_event_observed walks only the union of the matching
        # event_type bucket and the unfiltered set, converting the
        # common case from O(events × predicates) to O(events ×
        # matching_predicates).
        self._predicates_by_event_type: dict[str, set[str]] = {}
        self._predicates_unfiltered: set[str] = set()
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
        self._predicates_by_event_type.clear()
        self._predicates_unfiltered.clear()
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

        # Drop any prior index entry for this trigger_id — register
        # is idempotent; re-registration with a different selector
        # must move the trigger between buckets cleanly.
        self._unindex_event_type(trigger_id)

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

        # Index by simple event_type when the selector permits;
        # otherwise add to the unfiltered bucket. ``every``
        # predicates never fire on the event-driven path, so they
        # stay out of the index entirely (the cron walk in
        # evaluate_now() iterates _predicates directly).
        if predicate.temporal_relation.kind != "every":
            event_type = _extract_event_type_filter(
                predicate.event_selector,
            )
            if event_type is not None:
                self._predicates_by_event_type.setdefault(
                    event_type, set(),
                ).add(trigger_id)
            else:
                self._predicates_unfiltered.add(trigger_id)

    def _unindex_event_type(self, trigger_id: str) -> None:
        """Remove a trigger_id from the event_type prefilter index.
        Called on register (idempotent re-registration) and
        deactivate."""
        # Walk both buckets; one or zero will contain the id. Cost
        # is bounded by the small number of distinct event_type keys,
        # not by the total predicate count.
        for event_type, ids in list(
            self._predicates_by_event_type.items()
        ):
            if trigger_id in ids:
                ids.discard(trigger_id)
                if not ids:
                    self._predicates_by_event_type.pop(event_type, None)
        self._predicates_unfiltered.discard(trigger_id)

    async def deactivate(self, trigger_id: str) -> None:
        record = self._predicates.get(trigger_id)
        if record is not None:
            record.active = False
            # Drop from the prefilter index so on_event_observed
            # doesn't walk a deactivated predicate's bucket. Active
            # check inside the walk is belt-and-suspenders.
            self._unindex_event_type(trigger_id)

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

        Codex mid-batch fold #5: the candidate set is the union of
        the event_type prefilter bucket and the unfiltered bucket
        (predicates with rich selectors). Predicates whose simple
        eq selector targets a different event_type are skipped
        without invoking ``event_matches_selector`` at all.
        """
        if self._outbox is None:
            return 0
        fired = 0
        now = datetime.now(timezone.utc)
        # Build the candidate trigger_id set for this event:
        # event_type bucket ∪ unfiltered bucket. Snapshot via copy()
        # so concurrent register/deactivate during await points
        # below doesn't mutate the iteration target.
        candidate_ids: set[str] = (
            self._predicates_by_event_type
            .get(getattr(event, "event_type", ""), set())
            .copy()
        )
        candidate_ids.update(self._predicates_unfiltered)
        for trigger_id in candidate_ids:
            record = self._predicates.get(trigger_id)
            if record is None or not record.active:
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
            # WTC v1 C6: missed-window semantics. For 0 fires the
            # walk is silent. For exactly 1 fire (the on-time
            # case) we dispatch normally. For 2+ fires (downtime
            # detected) we honor DispatchPolicy.missed_window:
            #
            #   skip      — emit workflow.missed_fire for every
            #               missed fire; dispatch nothing. The
            #               next on-time tick fires normally.
            #   catch_up  — emit workflow.missed_fire for every
            #               missed fire EXCEPT the latest;
            #               dispatch the latest as a single
            #               catch-up fire (catch_up=True flag on
            #               the outbox row). Bounds the catch-up
            #               cost at exactly one dispatch per
            #               predicate per recovery, regardless of
            #               downtime length.
            policy = record.predicate.dispatch_policy
            fired += await self._dispatch_cron_fires(
                record=record,
                cron_expr=cron_expr,
                fires=fires,
                missed_window=policy.missed_window,
            )
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

    # -- missed-window helpers (C6) ------------------------------------

    async def _dispatch_cron_fires(
        self,
        *,
        record: _RegisteredPredicate,
        cron_expr: str,
        fires: list[datetime],
        missed_window: str,
    ) -> int:
        """Apply missed-window semantics to a cron walk's fire list.

        * 0 fires → 0 dispatches.
        * 1 fire  → dispatch normally (no missed-window logic).
        * 2+ fires (downtime detected):
          - ``skip``: emit workflow.missed_fire for each; dispatch
            none. Next on-time tick fires normally.
          - ``catch_up``: emit workflow.missed_fire for each
            EXCEPT the latest; dispatch the latest with
            catch_up=True.

        Returns count of fires actually dispatched.
        """
        if not fires:
            return 0
        if len(fires) == 1:
            # On-time: a single fire in (last_evaluated, now].
            return await self._claim_one_cron_fire(
                record=record, cron_expr=cron_expr,
                fire_time=fires[0], catch_up=False,
            )
        # Downtime detected — len(fires) >= 2.
        if missed_window == "skip":
            for f in fires:
                await self._emit_missed_fire(
                    record=record, cron_expr=cron_expr,
                    fire_time=f, reason="skip",
                )
            return 0
        # catch_up — emit missed_fire for all but the latest;
        # dispatch the latest as a single catch-up.
        for f in fires[:-1]:
            await self._emit_missed_fire(
                record=record, cron_expr=cron_expr,
                fire_time=f, reason="catch_up_collapsed",
            )
        return await self._claim_one_cron_fire(
            record=record, cron_expr=cron_expr,
            fire_time=fires[-1], catch_up=True,
        )

    async def _claim_one_cron_fire(
        self,
        *,
        record: _RegisteredPredicate,
        cron_expr: str,
        fire_time: datetime,
        catch_up: bool,
    ) -> int:
        """Build the fire_window_key + payload for a cron fire and
        route through _claim_and_dispatch. Returns 1 if claimed
        and dispatched, else 0."""
        normalized = normalize_cron_fire_time(cron_expr, fire_time)
        fwk = fire_window_key_for_every(cron_expr, normalized)
        payload = {
            "fire_time": normalized,
            "cron_expression": cron_expr,
        }
        if catch_up:
            payload["catch_up"] = True
        if await self._claim_and_dispatch(
            record=record,
            fire_window_key=fwk,
            payload=payload,
            catch_up=catch_up,
        ):
            return 1
        return 0

    async def _emit_missed_fire(
        self,
        *,
        record: _RegisteredPredicate,
        cron_expr: str,
        fire_time: datetime,
        reason: str,
    ) -> None:
        """Emit ``workflow.missed_fire`` to the durable event_stream.

        ``reason`` is ``"skip"`` (missed_window=skip dropped this
        fire) or ``"catch_up_collapsed"`` (catch_up policy
        collapsed this fire into the single catch-up). Failure to
        emit is logged but does not prevent dispatch — the audit
        record is best-effort, the durable outbox claim is the
        authoritative substrate state.
        """
        from kernos.kernel import event_stream
        normalized = normalize_cron_fire_time(cron_expr, fire_time)
        try:
            await event_stream.emit(
                instance_id=record.instance_id,
                event_type=EVENT_TYPE_WORKFLOW_MISSED_FIRE,
                payload={
                    "trigger_id": record.trigger_id,
                    "workflow_id": record.workflow_id,
                    "cron_expression": cron_expr,
                    "missed_fire_time": normalized,
                    "reason": reason,
                },
            )
        except Exception as exc:
            logger.warning(
                "WTC v1 C6: workflow.missed_fire emit failed "
                "trigger=%s reason=%s: %s",
                record.trigger_id, reason, exc,
            )

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

            # Spec 5 15th amendment B2 sentinel: WLP returned without
            # raising but the workflow is registered_not_activated /
            # deactivated. Route to terminal-skip via mark_failed with
            # the sentinel as the last_error so operators can filter
            # ``last_error LIKE 'skipped:%'`` to distinguish from
            # genuine dispatch failure. Non-retryable — keep returning
            # True to leave the retry loop.
            if workflow_execution_id == _EXECUTE_SKIPPED_AUTHORING_INACTIVE:
                logger.info(
                    "WTC v1 dispatch skipped (authoring inactive): "
                    "trigger=%s fire_id=%s",
                    record.trigger_id, claim.fire_id,
                )
                try:
                    await self._outbox.mark_failed(
                        fire_id=claim.fire_id,
                        claim_owner=self._claim_owner,
                        error=_EXECUTE_SKIPPED_AUTHORING_INACTIVE,
                    )
                except StaleClaimError:
                    pass
                return True

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
           re-invoking WLP — this closes the the design review must-fix seam
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

        # Pending past lease — close the the design review seam.
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
                        "execution=%s (the design review seam closure)",
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

            # B2 sentinel handling in the recovery sweep, mirroring
            # the active-claim path: skip mark_dispatched; route to
            # mark_failed with the sentinel as last_error.
            if workflow_execution_id == _EXECUTE_SKIPPED_AUTHORING_INACTIVE:
                logger.info(
                    "WTC v1 recover: redispatch skipped (authoring "
                    "inactive) fire_id=%s",
                    record.fire_id,
                )
                try:
                    await self._outbox.mark_failed(
                        fire_id=record.fire_id,
                        claim_owner=self._claim_owner,
                        error=_EXECUTE_SKIPPED_AUTHORING_INACTIVE,
                    )
                    recovered += 1
                except StaleClaimError:
                    pass
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
