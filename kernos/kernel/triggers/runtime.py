"""TriggerEvaluationRuntime — unified time + event trigger runtime.

C1 ships the **interface shell only**: start/stop, register/deactivate
(with predicate validation + atomic persistence), evaluate_now() +
recover() entry points. The actual evaluators (cron walk,
event-driven match, before/after due-time math) land in C2.

Composition with shipped substrate:

* The existing :class:`kernos.kernel.workflows.trigger_registry.TriggerRegistry`
  remains the persistence + cache layer for trigger metadata. The
  runtime sits on top of it.
* :class:`FireOutbox` owns the dispatch-state table
  (``trigger_fires`` extended) and is the durable boundary the
  runtime claims fires through.
* The dispatch boundary (Codex D7) — claim → dispatch → mark — is
  the same regardless of whether the fire was decided by the
  cron walk or by an event match.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any

from kernos.kernel.triggers.errors import TriggerError
from kernos.kernel.triggers.outbox import FireOutbox
from kernos.kernel.triggers.predicate import (
    TriggerPredicate,
    validate_predicate,
)

logger = logging.getLogger(__name__)


def _generate_claim_owner() -> str:
    """Build a stable per-process claim_owner string. Combines
    hostname + pid + process-start-time hash so a recovered claim
    can be distinguished from a duplicate-claim race within the
    same hostname."""
    return (
        f"runtime:{socket.gethostname()}:"
        f"{os.getpid()}:{int.from_bytes(os.urandom(4), 'big'):08x}"
    )


class TriggerEvaluationRuntime:
    """Unified time + event trigger runtime. Time-driven via
    heartbeat; event-driven via event_stream post-flush hook.
    Dispatch through durable :class:`FireOutbox`.

    C1 shell: holds the substrate plumbing and exposes
    register/deactivate/evaluate_now/recover. Actual evaluation
    logic lands in C2.
    """

    def __init__(self) -> None:
        self._outbox: FireOutbox | None = None
        self._heartbeat_seconds: int = 30
        self._heartbeat_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._claim_owner: str = ""
        # Predicates are held in-memory for evaluation; the existing
        # TriggerRegistry persists trigger metadata. C2 wires the
        # round-trip.
        self._predicates: dict[str, dict[str, Any]] = {}
        # Predicate dispatch hooks — the unified entry point WLP
        # will set so the runtime can call WLP.execute_workflow
        # under the dispatch boundary. Wired in C2 alongside the
        # actual evaluator path. Held as Optional in C1 so the
        # interface compiles cleanly.
        self._wlp_dispatch: Any | None = None

    # -- lifecycle ------------------------------------------------------

    async def start(
        self,
        *,
        data_dir: str,
        heartbeat_seconds: int = 30,
        wlp_dispatch: Any | None = None,
    ) -> None:
        """Boot: open the FireOutbox connection, attach event-stream
        hook (C2), start heartbeat loop (C2), run recovery sweep
        (C2). C1 ships the lifecycle skeleton; the heartbeat /
        post-flush hook attachments arrive in C2."""
        if self._outbox is not None:
            return
        self._heartbeat_seconds = max(1, int(heartbeat_seconds))
        self._claim_owner = _generate_claim_owner()
        self._outbox = FireOutbox()
        await self._outbox.start(data_dir)
        self._wlp_dispatch = wlp_dispatch
        self._stop_event = asyncio.Event()
        logger.info(
            "WTC v1 runtime started: claim_owner=%s heartbeat=%ds",
            self._claim_owner, self._heartbeat_seconds,
        )

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeat_task = None
        if self._outbox is not None:
            await self._outbox.stop()
            self._outbox = None
        self._predicates.clear()

    @property
    def outbox(self) -> FireOutbox:
        """Public access to the outbox so the runtime's owner (or
        adapter modules) can inspect / share the same instance.
        Raises if start() hasn't run."""
        if self._outbox is None:
            raise RuntimeError("TriggerEvaluationRuntime not started")
        return self._outbox

    @property
    def claim_owner(self) -> str:
        """The per-process claim owner string. Stable for the
        runtime's lifetime; rebuilt on every fresh process."""
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
        """Persist + activate a trigger. Atomic — fails if predicate
        validation fails; never partially registered. C1 stores
        metadata in-memory; C5 wires through the existing
        TriggerRegistry persistence so registrations survive
        restart.
        """
        if self._outbox is None:
            raise RuntimeError("TriggerEvaluationRuntime not started")
        if not trigger_id:
            raise TriggerError("trigger_id is required")
        if not instance_id:
            raise TriggerError("instance_id is required")
        if not workflow_id:
            raise TriggerError("workflow_id is required")

        # Validation FIRST so a partial state is impossible.
        validate_predicate(predicate)

        self._predicates[trigger_id] = {
            "trigger_id": trigger_id,
            "instance_id": instance_id,
            "workflow_id": workflow_id,
            "predicate": predicate,
            "member_id": member_id,
            "active": True,
        }

    async def deactivate(self, trigger_id: str) -> None:
        """Mark a trigger inactive. Subsequent evaluate_now() calls
        skip it. Idempotent."""
        record = self._predicates.get(trigger_id)
        if record is not None:
            record["active"] = False

    async def list_active(self) -> list[dict[str, Any]]:
        """Return registered active predicates. C1 in-memory only;
        C5 reads through to the persistence layer."""
        return [r for r in self._predicates.values() if r.get("active")]

    # -- evaluation entry points (shells; C2 fills in) ------------------

    async def evaluate_now(self) -> int:
        """Heartbeat tick. Walks time-shape predicates; due fires
        call into _claim_fire → _dispatch. Idempotent: safe to
        call twice in the same window. Returns count of fires
        claimed during this tick.

        C1 returns 0 (no evaluator). C2 implements the cron walk
        + before/after due-time math. C3 wires the event-driven
        path.
        """
        if self._outbox is None:
            raise RuntimeError("TriggerEvaluationRuntime not started")
        return 0

    async def recover(self) -> int:
        """Engine-startup recovery sweep. Walks
        ``status='pending'`` AND ``status='dispatched'`` rows in
        trigger_fires; resumes or completes. Returns count
        recovered. Idempotent.

        C1 returns 0 (no sweep logic). C2 implements:

        1. Pending past claim_lease: query WLP by fire_id; if WLP
           has the execution, reconcile the outbox row to
           ``dispatched`` (closes the Kit must-fix seam — AC6
           scenario #2). Otherwise reclaim and re-dispatch.
        2. Dispatched past dispatch_lease: query WLP for execution
           outcome; transition completed when WLP done, else
           re-dispatch.
        """
        if self._outbox is None:
            raise RuntimeError("TriggerEvaluationRuntime not started")
        return 0


__all__ = ["TriggerEvaluationRuntime"]
