"""Restricted port facades for Drafter (the design review pin v1→v2: capability wrappers).

Drafter's ``__init__`` receives ports, NOT raw dependencies. Forbidden
capabilities are STRUCTURALLY ABSENT from the port surface — calling
``port.mark_committed(...)`` raises :class:`AttributeError`, never
reaches the substrate whitelist. The whitelist remains as
belt-and-suspenders for any path that escapes the port surface
(serialization reconstitution, reflection-based access).

Three ports:

* :class:`DrafterDraftPort` — restricted facade over WDP's
  :class:`DraftRegistry`. Exposes ``create_draft`` / ``update_draft`` /
  ``abandon_draft`` / ``get_draft`` / ``list_drafts`` only.
  ``mark_committed`` is structurally absent.
* :class:`DrafterSubstrateToolsPort` — restricted facade over STS's
  :class:`SubstrateTools`. Exposes read surfaces +
  ``register_workflow_dry_run`` (hard-wired ``dry_run=True``).
  Full ``register_workflow`` is structurally absent.
* :class:`DrafterEventPort` — restricted facade over the EventEmitter.
  Exposes ``emit_signal`` / ``emit_receipt`` only; substrate sets
  ``envelope.source_module="drafter"``. Caller cannot stamp arbitrary
  source identity.

All write methods are idempotent via the action_log substrate: every
call computes a deterministic ``target_id`` from the source event +
action key, and ``action_log.record_and_perform`` ensures the side
effect runs exactly once across replays.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from kernos.kernel.cohorts._substrate.action_log import ActionLog

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.drafts.registry import DraftRegistry, WorkflowDraft
    from kernos.kernel.event_stream import EventEmitter
    from kernos.kernel.substrate_tools import (
        ContextBrief,
        ContextRef,
        DryRunResult,
        SubstrateTools,
    )


# ---------------------------------------------------------------------------
# Whitelist — belt-and-suspenders below the port structural absence
# ---------------------------------------------------------------------------


DRAFTER_WHITELIST: frozenset[str] = frozenset({
    # WDP writes
    "DraftRegistry.create_draft",
    "DraftRegistry.update_draft",
    "DraftRegistry.abandon_draft",
    # WDP reads
    "DraftRegistry.get_draft",
    "DraftRegistry.list_drafts",
    # STS reads
    "SubstrateTools.list_workflows",
    "SubstrateTools.list_known_providers",
    "SubstrateTools.list_agents",
    "SubstrateTools.query_context_brief",
    "SubstrateTools.list_drafts",
    # STS dry-run only
    "SubstrateTools.register_workflow_dry_run",
    # event_stream
    "EventStream.read",
    "DurableEventCursor.read_next_batch",
    "DurableEventCursor.commit_position",
    # signal emission
    "EventEmitter.emit",
})
"""Tool dispatch whitelist for Drafter (declared in cohort registration
metadata). NOT the primary defense — port structural absence is. The
whitelist catches any escape paths that bypass the port surface."""


# ---------------------------------------------------------------------------
# DrafterDraftPort
# ---------------------------------------------------------------------------


class DrafterDraftPort:
    """Restricted facade over :class:`DraftRegistry`.

    Structurally absent: ``mark_committed`` (no method on this class).
    Test pin: ``not hasattr(DrafterDraftPort, 'mark_committed')``.

    All writes route through :class:`ActionLog.record_and_perform` so
    crash-after-side-effect-before-cursor-commit replays do NOT
    duplicate the side effect.
    """

    def __init__(
        self,
        *,
        registry: "DraftRegistry",
        action_log: ActionLog,
        instance_id: str,
        cohort_id: str = "drafter",
    ) -> None:
        if not instance_id:
            raise ValueError("instance_id is required")
        self._registry = registry
        self._action_log = action_log
        self._instance_id = instance_id
        self._cohort_id = cohort_id

    @property
    def instance_id(self) -> str:
        return self._instance_id

    async def create_draft(
        self,
        *,
        source_event_id: str,
        intent_summary: str,
        home_space_id: str | None = None,
        source_thread_id: str | None = None,
        target_draft_id: str,
    ) -> dict:
        """Idempotent ``create_draft`` via action_log.

        ``target_draft_id`` MUST be deterministic (computed by the
        caller from ``(source_event_id, instance_id, intent_summary_hash)``)
        so replay produces the same composite key and the action_log's
        UNIQUE constraint catches the dup.

        Returns the action_log result summary on success. On replay,
        returns the cached summary unchanged.
        """
        if not source_event_id:
            raise ValueError("source_event_id is required")
        if not target_draft_id:
            raise ValueError("target_draft_id is required (deterministic)")

        async def _do_create() -> dict:
            draft = await self._registry.create_draft(
                instance_id=self._instance_id,
                intent_summary=intent_summary,
                home_space_id=home_space_id,
                source_thread_id=source_thread_id,
            )
            return {
                "draft_id": draft.draft_id,
                "status": draft.status,
                "version": draft.version,
            }

        return await self._action_log.record_and_perform(
            instance_id=self._instance_id,
            source_event_id=source_event_id,
            action_type="create_draft",
            target_id=target_draft_id,
            perform=_do_create,
            result_to_summary=lambda result: result,
        )

    async def update_draft(
        self,
        *,
        source_event_id: str,
        draft_id: str,
        **update_kwargs,
    ) -> dict:
        """Idempotent ``update_draft`` via action_log."""
        if not source_event_id:
            raise ValueError("source_event_id is required")
        if not draft_id:
            raise ValueError("draft_id is required")

        async def _do_update() -> dict:
            draft = await self._registry.update_draft(
                instance_id=self._instance_id,
                draft_id=draft_id,
                **update_kwargs,
            )
            return {
                "draft_id": draft.draft_id,
                "status": draft.status,
                "version": draft.version,
            }

        return await self._action_log.record_and_perform(
            instance_id=self._instance_id,
            source_event_id=source_event_id,
            action_type="update_draft",
            target_id=draft_id,
            perform=_do_update,
            result_to_summary=lambda result: result,
        )

    async def abandon_draft(
        self,
        *,
        source_event_id: str,
        draft_id: str,
        **abandon_kwargs,
    ) -> dict:
        """Idempotent ``abandon_draft`` via action_log."""
        if not source_event_id:
            raise ValueError("source_event_id is required")
        if not draft_id:
            raise ValueError("draft_id is required")

        async def _do_abandon() -> dict:
            draft = await self._registry.abandon_draft(
                instance_id=self._instance_id,
                draft_id=draft_id,
                **abandon_kwargs,
            )
            return {
                "draft_id": draft.draft_id,
                "status": draft.status,
                "version": draft.version,
            }

        return await self._action_log.record_and_perform(
            instance_id=self._instance_id,
            source_event_id=source_event_id,
            action_type="abandon_draft",
            target_id=draft_id,
            perform=_do_abandon,
            result_to_summary=lambda result: result,
        )

    async def get_draft(self, *, draft_id: str) -> "WorkflowDraft | None":
        """Pure read — no action_log involvement."""
        return await self._registry.get_draft(
            instance_id=self._instance_id, draft_id=draft_id,
        )

    async def list_drafts(self, **kwargs) -> "list[WorkflowDraft]":
        """Pure read — no action_log involvement."""
        return await self._registry.list_drafts(
            instance_id=self._instance_id, **kwargs,
        )

    # mark_committed: STRUCTURALLY ABSENT.


# ---------------------------------------------------------------------------
# DrafterSubstrateToolsPort
# ---------------------------------------------------------------------------


class DrafterSubstrateToolsPort:
    """Restricted facade over :class:`SubstrateTools`.

    Structurally absent: full ``register_workflow`` (with ``dry_run=False``).
    Only ``register_workflow_dry_run`` is exposed, hard-wired to
    ``dry_run=True``.
    """

    def __init__(
        self,
        *,
        sts: "SubstrateTools",
        instance_id: str,
    ) -> None:
        if not instance_id:
            raise ValueError("instance_id is required")
        self._sts = sts
        self._instance_id = instance_id

    @property
    def instance_id(self) -> str:
        return self._instance_id

    async def list_workflows(self, **kwargs):
        return await self._sts.list_workflows(
            instance_id=self._instance_id, **kwargs,
        )

    async def list_known_providers(self):
        return await self._sts.list_known_providers(
            instance_id=self._instance_id,
        )

    async def list_agents(self, **kwargs):
        return await self._sts.list_agents(
            instance_id=self._instance_id, **kwargs,
        )

    async def list_drafts(self, **kwargs):
        return await self._sts.list_drafts(
            instance_id=self._instance_id, **kwargs,
        )

    async def query_context_brief(
        self, *, ref: "ContextRef",
    ) -> "ContextBrief | None":
        return await self._sts.query_context_brief(
            instance_id=self._instance_id, ref=ref,
        )

    async def register_workflow_dry_run(
        self, *, descriptor: dict,
    ) -> "DryRunResult":
        """Hard-wired to STS.register_workflow(descriptor, dry_run=True).
        No way to set dry_run=False from this port."""
        result = await self._sts.register_workflow(
            instance_id=self._instance_id,
            descriptor=descriptor,
            dry_run=True,
        )
        # SubstrateTools.register_workflow returns DryRunResult on
        # dry_run=True. Type narrow for clarity.
        return result  # type: ignore[return-value]

    # register_workflow (full): STRUCTURALLY ABSENT.


# ---------------------------------------------------------------------------
# DrafterEventPort
# ---------------------------------------------------------------------------


class DrafterEventPort:
    """Restricted facade over the registered ``"drafter"`` EventEmitter.

    Structurally absent: ``emit`` (the raw EventEmitter method that
    could carry any event type). Only :meth:`emit_signal` and
    :meth:`emit_receipt` are exposed, both hard-wired to substrate-set
    ``envelope.source_module="drafter"`` via the registered emitter.

    Idempotent via action_log: signal/receipt IDs are deterministic
    from ``(source_event_id, signal_or_receipt_type, target_id)`` so
    replay produces the same composite key.
    """

    def __init__(
        self,
        *,
        emitter: "EventEmitter",
        action_log: ActionLog,
        instance_id: str,
    ) -> None:
        if not instance_id:
            raise ValueError("instance_id is required")
        if emitter.source_module != "drafter":
            raise ValueError(
                f"DrafterEventPort requires an emitter registered with "
                f"source_module='drafter', got "
                f"{emitter.source_module!r}"
            )
        self._emitter = emitter
        self._action_log = action_log
        self._instance_id = instance_id

    @property
    def instance_id(self) -> str:
        return self._instance_id

    async def emit_signal(
        self,
        *,
        source_event_id: str,
        signal_type: str,
        payload: dict,
        target_id: str,
        correlation_id: str | None = None,
    ) -> dict:
        """Idempotent signal emission via action_log.

        ``target_id`` is the deterministic signal identifier (e.g. the
        ``draft_id`` for ``draft_ready``, or a hash for
        ``multi_intent_detected``). Replay re-uses the same key so
        action_log dedupes.
        """
        if not source_event_id:
            raise ValueError("source_event_id is required")
        if not signal_type:
            raise ValueError("signal_type is required")
        if not target_id:
            raise ValueError("target_id is required (deterministic)")

        async def _do_emit() -> dict:
            await self._emitter.emit(
                self._instance_id, signal_type, payload,
                correlation_id=correlation_id,
            )
            return {
                "signal_type": signal_type,
                "target_id": target_id,
            }

        return await self._action_log.record_and_perform(
            instance_id=self._instance_id,
            source_event_id=source_event_id,
            action_type="emit_signal",
            target_id=target_id,
            perform=_do_emit,
            result_to_summary=lambda result: result,
        )

    async def emit_receipt(
        self,
        *,
        source_event_id: str,
        receipt_type: str,
        payload: dict,
        target_id: str,
        correlation_id: str | None = None,
    ) -> dict:
        """Idempotent receipt emission via action_log."""
        if not source_event_id:
            raise ValueError("source_event_id is required")
        if not receipt_type:
            raise ValueError("receipt_type is required")
        if not target_id:
            raise ValueError("target_id is required (deterministic)")

        async def _do_emit() -> dict:
            await self._emitter.emit(
                self._instance_id, receipt_type, payload,
                correlation_id=correlation_id,
            )
            return {
                "receipt_type": receipt_type,
                "target_id": target_id,
            }

        return await self._action_log.record_and_perform(
            instance_id=self._instance_id,
            source_event_id=source_event_id,
            action_type="emit_receipt",
            target_id=target_id,
            perform=_do_emit,
            result_to_summary=lambda result: result,
        )

    # emit (raw): STRUCTURALLY ABSENT.


__all__ = [
    "DRAFTER_WHITELIST",
    "DrafterDraftPort",
    "DrafterEventPort",
    "DrafterSubstrateToolsPort",
]
