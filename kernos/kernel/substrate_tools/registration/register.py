"""STS register_workflow gate.

The production registration entry point. Implements the full 9-step
validation flow before persistence. ``dry_run=True`` runs the same
validation but skips approval-related steps and never persists.

Production flow:

1. Validate caller arguments.
1b. (WTC v1 C5b) Pre-compile descriptor.triggers if present —
    catches predicate-shape errors BEFORE persist so atomicity
    holds: if a trigger is malformed, the workflow never enters
    the DB and no approval is consumed.
2. Resolve approval event (approval.py).
3. Validate envelope source authority (approval.py).
4. Validate proposal anchor (approval.py).
5. Validate approval-call instance match (approval.py).
5b. Modification target binding (approval.py).
6. Re-run full descriptor validation NOW (P7) — NOT cached.
7. Compare descriptor_hash to approval event's hash.
8. Verify validation produced no error-severity issues.
9. Atomic persist + UNIQUE-constraint consumption.
10. (WTC v1 C5b) If runtime is wired, register the pre-compiled
    triggers with the unified TriggerEvaluationRuntime. Step 9's
    persist is the durable record; step 10 hydrates the in-memory
    runtime so trigger evaluation begins immediately. Failures
    here are logged but do not unwind step 9 — the runtime can
    rehydrate from the durable workflow row on next start.

Step 9 raises :class:`ApprovalAlreadyConsumed` (TERMINAL) when the
partial UNIQUE index ``idx_workflows_approval_unique`` rejects a
duplicate ``(instance_id, approval_event_id)``. Caller MUST NOT retry.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Union

import aiosqlite

from kernos.kernel.substrate_tools.errors import (
    ApprovalAlreadyConsumed,
    ApprovalBindingMissing,
    ApprovalDescriptorMismatch,
    ApprovalInstanceMismatch,
    RegistrationValidationFailed,
)
from kernos.kernel.substrate_tools.registration.approval import (
    resolve_and_validate_approval,
)
from kernos.kernel.substrate_tools.registration.validation import (
    DryRunResult,
    run_full_validation,
)

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.agents.registry import AgentRegistry
    from kernos.kernel.triggers.runtime import TriggerEvaluationRuntime
    from kernos.kernel.workflows.workflow_registry import (
        Workflow,
        WorkflowRegistry,
    )

logger = logging.getLogger(__name__)


def _precompile_triggers(descriptor: dict) -> "list":
    """Pre-compile descriptor.triggers into CompiledTrigger records,
    or return an empty list when the descriptor has no triggers.

    Lifted out of register_workflow so step 1b can run before the
    approval flow without pulling the trigger-runtime imports into
    the hot path of every dry_run.

    Raises :class:`RegistrationValidationFailed` when a trigger
    descriptor is malformed — callers see the usual
    validation-failure shape rather than a raw TriggerError. Step 9
    (persist) and step 10 (runtime register) never run when this
    raises.
    """
    triggers = descriptor.get("triggers")
    workflow_id = descriptor.get("workflow_id") or descriptor.get("name")
    if not triggers or not workflow_id:
        return []
    from kernos.kernel.triggers.adapters import (
        compile_descriptor_triggers,
    )
    from kernos.kernel.triggers.errors import TriggerError
    try:
        return compile_descriptor_triggers(
            workflow_id=workflow_id,
            descriptor=descriptor,
        )
    except TriggerError as exc:
        raise RegistrationValidationFailed(
            f"descriptor.triggers compilation failed: {exc}",
            issues=(),
        ) from exc


async def register_workflow(
    *,
    instance_id: str,
    descriptor: dict,
    workflow_registry: "WorkflowRegistry",
    agent_registry: "AgentRegistry | None" = None,
    runtime: "TriggerEvaluationRuntime | None" = None,
    dry_run: bool = False,
    approval_event_id: str | None = None,
) -> "Union[Workflow, DryRunResult]":
    """Production-path registration gate. See module docstring for the
    9-step (now 10-step with WTC v1 C5b) flow.

    Args:
        instance_id: caller's instance scope.
        descriptor: workflow descriptor dict.
        workflow_registry: WLP instance for the underlying registration
            and modification-target lookup.
        agent_registry: DAR instance for route_to_agent reference
            validation. May be ``None`` when no DAR-aware actions are
            present (validation skips agent checks).
        runtime: WTC v1 :class:`TriggerEvaluationRuntime`. When
            provided, descriptor.triggers are compiled (step 1b)
            and registered with the runtime (step 10) atomically
            with the workflow. When ``None``, trigger registration
            is skipped — legacy callers continue to work; the
            workflow row alone is persisted.
        dry_run: True returns a :class:`DryRunResult` without
            persistence; ``approval_event_id`` is ignored. Trigger
            shape errors still surface during dry_run via step 1b.
        approval_event_id: REQUIRED when ``dry_run=False``. Bound to the
            new workflow row via the ``approval_event_id`` column;
            partial UNIQUE constraint enforces single-use.

    Returns:
        :class:`DryRunResult` when ``dry_run=True``; the persisted
        :class:`Workflow` otherwise.

    Raises:
        ApprovalBindingMissing: ``dry_run=False`` without approval.
        ApprovalEventNotFound, ApprovalEventTypeInvalid,
        ApprovalAuthoritySpoofed, ApprovalAuthorityIncomplete,
        ApprovalProvenanceUnverifiable, ApprovalProposalMismatch,
        ApprovalInstanceMismatch, ApprovalModificationTargetMismatch,
        ApprovalModificationTargetMissing: see approval.py.
        ApprovalDescriptorMismatch: recomputed hash != approval hash.
        RegistrationValidationFailed: revalidation produced errors,
            or descriptor.triggers compilation failed (step 1b).
        ApprovalAlreadyConsumed: TERMINAL — caller MUST NOT retry.
    """
    # Step 1: validate caller arguments.
    if not instance_id:
        raise ValueError("instance_id is required")
    if not isinstance(descriptor, dict):
        raise TypeError(
            f"descriptor must be a dict, got {type(descriptor).__name__}"
        )
    # Cross-check the descriptor's own instance_id against the caller.
    # Without this guard, a caller in instance A could approve a
    # descriptor whose instance_id field is B and consume the approval
    # under B's (instance_id, approval_event_id) unique key. The
    # approval-event lookup is already instance-scoped (Step 2); this
    # closes the symmetric gap on the descriptor side.
    descriptor_instance = descriptor.get("instance_id", "")
    if descriptor_instance and descriptor_instance != instance_id:
        raise ApprovalInstanceMismatch(
            f"descriptor.instance_id={descriptor_instance!r} does not "
            f"match caller instance_id={instance_id!r}"
        )

    # Step 1b (WTC v1 C5b): pre-compile descriptor.triggers. Catches
    # trigger-shape errors before persist so atomicity holds.
    # Compilation runs even when no runtime is wired so dry_run
    # surfaces the same errors that real registration would.
    compiled_triggers = _precompile_triggers(descriptor)

    if dry_run:
        return await run_full_validation(
            descriptor, agent_registry=agent_registry,
        )

    # Real registration: must be approval-bound.
    if not approval_event_id:
        raise ApprovalBindingMissing(
            "register_workflow(dry_run=False) requires approval_event_id"
        )

    # Steps 2-5b: resolve and validate the approval event.
    approval_event = await resolve_and_validate_approval(
        instance_id=instance_id,
        approval_event_id=approval_event_id,
        descriptor=descriptor,
        workflow_registry=workflow_registry,
    )

    # Step 6: re-run full descriptor validation NOW (P7).
    # The dry-run output is NEVER cached — provider state may have
    # drifted between proposal and registration (provider disconnected,
    # agent retired, etc.). Full validation re-runs immediately.
    validation = await run_full_validation(
        descriptor, agent_registry=agent_registry,
    )

    # Step 7: hash match.
    if validation.descriptor_hash != approval_event.payload.get("descriptor_hash"):
        raise ApprovalDescriptorMismatch(
            f"recomputed descriptor_hash={validation.descriptor_hash!r} != "
            f"approval.descriptor_hash="
            f"{approval_event.payload.get('descriptor_hash')!r}"
        )

    # Step 8: verify validation passed.
    if not validation.valid:
        raise RegistrationValidationFailed(
            f"registration-time revalidation failed with "
            f"{len(validation.issues)} issue(s)",
            issues=list(validation.issues),
        )

    # Step 9: atomic persist + UNIQUE-constraint consumption.
    # Build the Workflow from the descriptor (we know it parses — Step 6
    # passed).
    from kernos.kernel.workflows.descriptor_parser import _build_workflow

    wf = _build_workflow(descriptor)
    try:
        registered = await workflow_registry._register_workflow_unbound(
            wf, approval_event_id=approval_event_id,
        )
    except aiosqlite.IntegrityError as exc:
        # The partial UNIQUE on (instance_id, approval_event_id) fired —
        # this approval has already been consumed.
        #
        # Hardening (Codex final-pass): match ONLY the approval-binding
        # index name. A duplicate workflow_id would also surface as
        # IntegrityError but is recoverable (the approval was rolled
        # back); translating that to ApprovalAlreadyConsumed would
        # incorrectly mark a recoverable failure as terminal. Re-raise
        # any other IntegrityError so the caller sees the underlying
        # constraint violation.
        # SQLite's IntegrityError message names the conflicting columns
        # rather than the index name, e.g. "UNIQUE constraint failed:
        # workflows.instance_id, workflows.approval_event_id". Match on
        # the column-pair signature so a duplicate workflow_id PK
        # (different shape) re-raises as the underlying IntegrityError
        # rather than mis-translating to terminal ApprovalAlreadyConsumed.
        msg = str(exc)
        is_approval_index_violation = (
            "idx_workflows_approval_unique" in msg
            or (
                "approval_event_id" in msg
                and "instance_id" in msg
                and "UNIQUE" in msg.upper()
            )
        )
        if is_approval_index_violation:
            raise ApprovalAlreadyConsumed(
                f"approval_event_id={approval_event_id!r} has already been "
                f"consumed in instance={instance_id!r}; this is a TERMINAL "
                f"failure mode — do NOT retry"
            ) from exc
        raise

    # Step 10 (WTC v1 C5b): hydrate the unified TriggerEvaluationRuntime
    # with the pre-compiled triggers. The workflow row from step 9 is
    # the durable record; the runtime registration is in-memory state
    # that begins evaluation immediately. Failures here are logged
    # but do not unwind step 9 — the runtime can rehydrate from the
    # durable row on next startup, so a transient registration error
    # is recoverable. Step 1b already validated trigger shape, so
    # failures at this point indicate a programming bug or runtime-
    # state error, not a malformed descriptor.
    if runtime is not None and compiled_triggers:
        for ct in compiled_triggers:
            try:
                await runtime.register(
                    trigger_id=ct.trigger_id,
                    instance_id=instance_id,
                    workflow_id=registered.workflow_id,
                    predicate=ct.predicate,
                )
            except Exception:
                logger.exception(
                    "WTC v1 C5b: runtime.register failed for "
                    "trigger_id=%s workflow_id=%s; workflow row is "
                    "persisted, runtime can rehydrate on next start",
                    ct.trigger_id, registered.workflow_id,
                )

    return registered


__all__ = ["register_workflow"]
