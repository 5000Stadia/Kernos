"""Translate manage_schedule cron / calendar Triggers into the
unified-runtime shape (WTC v1 C5c-2-prep).

This adapter is the bridge between the user-facing
``manage_schedule`` tool surface in :mod:`kernos.kernel.scheduler`
and the unified :class:`TriggerEvaluationRuntime`. Two
responsibilities:

1. ``schedule_to_descriptor`` — pure translation: take the
   manage_schedule create-time inputs (action type, params,
   condition shape, recurrence, etc.) and produce a Kernos
   workflow descriptor whose ``descriptor.triggers`` are
   compilable to :class:`TriggerPredicate` and whose
   ``action_sequence`` uses real action_library verbs
   (``notify_user`` / ``call_tool``). No side effects.

2. ``register_managed_schedule_workflow`` — apply the
   translation: pre-compile the descriptor's triggers (validates
   shape), persist the workflow row via
   :meth:`WorkflowRegistry._register_workflow_unbound`, then
   register each compiled predicate with the runtime. Atomicity
   matches STS C5b's posture: trigger shape errors fail the
   whole call before any persist; the in-memory runtime
   registration after persist is best-effort (rehydratable from
   the durable workflow row).

**Why this lives outside cohorts/crb (bypass-grep allowance):**
the bypass-grep at
``tests/test_no_direct_register_unbound.py`` enforces that
cohort and CRB code go through the approval-bound STS
``register_workflow``. ``manage_schedule`` is system-authorized
in-the-moment by the user invoking the tool — it's not a CRB
proposal/approval flow. The unbound path is the architecturally-
correct entry point for system-internal workflows. C5c-2 + C7
will extend the bypass-grep to include the manage_schedule
adapter in its allow-list of explicit-bypass callers.

**Scope of C5c-2-prep:** translation + registration helpers ONLY.
The actual rewire of ``handle_manage_schedule`` to call this
module — and the strike of scheduler.py's legacy fire path —
lands in C5c-2 + C7 (atomic flag-flip + Pattern 05 strike).
This commit ships the new path behind no flag; the legacy path
remains authoritative until the strike commit.
"""
from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from kernos.kernel.triggers.adapters.crb_compiler import (
    CompiledTrigger,
    compile_descriptor_triggers,
)
from kernos.kernel.triggers.errors import TriggerError

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.triggers.runtime import TriggerEvaluationRuntime
    from kernos.kernel.workflows.workflow_registry import (
        Workflow,
        WorkflowRegistry,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — manage_schedule action types and the substrate event types
# their predicates target.
# ---------------------------------------------------------------------------


MANAGE_SCHEDULE_ACTION_NOTIFY: str = "notify"
MANAGE_SCHEDULE_ACTION_TOOL_CALL: str = "tool_call"

# Calendar source's event_type — predicates for event-based
# manage_schedule triggers select on this.
EVENT_TYPE_CALENDAR_OBSERVED: str = "calendar.event_observed"

# Marker in workflow.metadata so list/pause/resume/remove can
# distinguish managed-schedule rows from CRB-registered or other
# system workflows. Read by the manage_schedule list path after
# C5c-2 + C7's rewire.
MANAGED_SCHEDULE_METADATA_KEY: str = "managed_schedule"


# ---------------------------------------------------------------------------
# Translation — manage_schedule inputs → workflow descriptor
# ---------------------------------------------------------------------------


def schedule_to_descriptor(
    *,
    workflow_id: str,
    instance_id: str,
    description: str,
    member_id: str = "",
    space_id: str = "",
    conversation_id: str = "",
    action_type: str,
    action_params: dict | None = None,
    delivery_class: str = "stage",
    notify_via: str = "",
    condition_type: str,
    recurrence: str = "",
    event_filter: str = "",
    event_lead_minutes: int = 30,
) -> dict:
    """Build a Kernos workflow descriptor from manage_schedule
    create-time inputs.

    Args:
        workflow_id: deterministic id minted by the caller; written
            into the descriptor (``_build_workflow`` would otherwise
            generate one) so list/find lookups are stable.
        instance_id: substrate scope.
        description: human-readable schedule description (the same
            text the user typed into manage_schedule).
        member_id, space_id, conversation_id: caller context;
            stored in metadata so the dispatched action knows where
            to deliver.
        action_type: ``"notify"`` or ``"tool_call"``. Maps to
            ``notify_user`` or ``call_tool`` action_library verbs.
        action_params: provider-specific parameters
            (``message`` for notify; ``tool_name``+``tool_args``
            for tool_call).
        delivery_class: ``ambient`` | ``stage`` | ``interrupt``.
            Used as the default urgency for notify_user.
        notify_via: channel name for delivery; empty ⇒ default.
        condition_type: ``"time"`` (cron) or ``"event"`` (calendar).
        recurrence: cron expression (REQUIRED when
            ``condition_type='time'``; one-shot time triggers are
            out of scope for v1 — the unified predicate model has
            no one-shot temporal kind, and they can be added in a
            follow-up if needed).
        event_filter: substring match on
            ``payload.summary`` for calendar events.
        event_lead_minutes: how many minutes before the calendar
            event the workflow should fire. Mapped to
            ``temporal_relation.kind="before", minutes=N``.

    Returns:
        A descriptor dict suitable for
        :func:`_build_workflow` consumption + the unified
        runtime's :func:`compile_descriptor_triggers`.

    Raises:
        ValueError: unsupported action_type, condition_type, or
            missing-required-field combination (e.g., time without
            recurrence).
    """
    action_params = action_params or {}

    action_sequence = _build_action_sequence(
        action_type=action_type,
        action_params=action_params,
        notify_via=notify_via,
        delivery_class=delivery_class,
    )
    triggers = _build_triggers(
        condition_type=condition_type,
        recurrence=recurrence,
        event_filter=event_filter,
        event_lead_minutes=event_lead_minutes,
    )

    return {
        "workflow_id": workflow_id,
        "instance_id": instance_id,
        "name": description[:80] if description else f"managed-schedule-{workflow_id[:8]}",
        "description": description,
        "owner": member_id,
        "version": "1",
        "bounds": {"iteration_count": 1},
        "verifier": {"flavor": "deterministic", "check": "managed_schedule"},
        "action_sequence": action_sequence,
        "triggers": triggers,
        "instance_local": True,
        "metadata": {
            MANAGED_SCHEDULE_METADATA_KEY: True,
            "manage_schedule_action_type": action_type,
            "manage_schedule_action_params": dict(action_params),
            "delivery_class": delivery_class,
            "notify_via": notify_via,
            "space_id": space_id,
            "conversation_id": conversation_id,
            "condition_type": condition_type,
            "recurrence": recurrence,
            "event_filter": event_filter,
            "event_lead_minutes": event_lead_minutes,
            "member_id": member_id,
        },
    }


def _build_action_sequence(
    *,
    action_type: str,
    action_params: dict,
    notify_via: str,
    delivery_class: str,
) -> list[dict]:
    if action_type == MANAGE_SCHEDULE_ACTION_NOTIFY:
        # NotifyUserAction params: channel, message, urgency.
        return [{
            "action_type": "notify_user",
            "parameters": {
                "channel": notify_via or "default",
                "message": action_params.get("message", ""),
                "urgency": action_params.get("urgency", delivery_class),
            },
        }]
    if action_type == MANAGE_SCHEDULE_ACTION_TOOL_CALL:
        # CallToolAction params: tool_name, tool_args.
        return [{
            "action_type": "call_tool",
            "parameters": {
                "tool_name": action_params.get("tool_name", ""),
                "tool_args": action_params.get("tool_args", {}),
            },
        }]
    raise ValueError(
        f"unsupported manage_schedule action_type: {action_type!r}"
    )


def _build_triggers(
    *,
    condition_type: str,
    recurrence: str,
    event_filter: str,
    event_lead_minutes: int,
) -> list[dict]:
    if condition_type == "time":
        if not recurrence:
            # One-shot time triggers (specific datetime, no
            # recurrence) are not yet representable in the
            # three-part predicate model — there's no one_shot
            # temporal kind. Surface this clearly so the caller
            # can decide whether to fall back to the legacy path
            # or skip the migration.
            raise ValueError(
                "manage_schedule condition_type='time' without recurrence "
                "is not supported by the unified-runtime path; one-shot "
                "time triggers require a future temporal-kind extension"
            )
        return [{
            "event_type": "scheduler.tick_due",
            "temporal_relation": {
                "kind": "every", "cron_expression": recurrence,
            },
            "dispatch_policy": {"missed_window": "skip"},
        }]
    if condition_type == "event":
        # Calendar event-based trigger. event_filter is a substring
        # match on payload.summary; when empty, match any
        # calendar.event_observed.
        if event_lead_minutes <= 0:
            raise ValueError(
                f"manage_schedule event_lead_minutes must be > 0; got "
                f"{event_lead_minutes}"
            )
        if event_filter:
            selector = {
                "op": "AND", "operands": [
                    {"op": "eq", "path": "event_type",
                     "value": EVENT_TYPE_CALENDAR_OBSERVED},
                    {"op": "contains", "path": "payload.summary",
                     "value": event_filter},
                ],
            }
        else:
            selector = {
                "op": "eq", "path": "event_type",
                "value": EVENT_TYPE_CALENDAR_OBSERVED,
            }
        return [{
            "event_type": EVENT_TYPE_CALENDAR_OBSERVED,
            "event_selector": selector,
            "temporal_relation": {
                "kind": "before", "minutes": event_lead_minutes,
            },
            "dispatch_policy": {"missed_window": "skip"},
        }]
    raise ValueError(
        f"unsupported manage_schedule condition_type: {condition_type!r}"
    )


# ---------------------------------------------------------------------------
# Registration — descriptor + runtime hydration via _register_workflow_unbound
# ---------------------------------------------------------------------------


async def register_managed_schedule_workflow(
    *,
    workflow_registry: "WorkflowRegistry",
    runtime: "TriggerEvaluationRuntime",
    descriptor: dict,
) -> "Workflow":
    """Register a manage_schedule workflow + its trigger predicates
    with the unified runtime.

    Atomicity: trigger compilation runs BEFORE persist so a
    malformed descriptor.triggers fails the whole call without
    leaving a dangling workflow row. Post-persist runtime.register
    is best-effort (in-memory; rehydratable from the workflow row).

    EXPLICIT BYPASS NOTE: this function calls
    :meth:`WorkflowRegistry._register_workflow_unbound` directly,
    which the cohort/CRB bypass-grep test forbids. manage_schedule
    is system-authorized via the user's tool invocation — it does
    NOT go through CRB approval flow. C5c-2 + C7 extends the
    bypass-grep to include this adapter in the allow-list.

    Returns:
        The persisted :class:`Workflow`.

    Raises:
        TriggerError: descriptor.triggers compilation failed.
            Workflow row is NOT persisted.
        Other errors from
        :meth:`WorkflowRegistry._register_workflow_unbound` (DB
        errors, descriptor parse failures) propagate; the runtime
        is never touched in those paths.
    """
    workflow_id = descriptor.get("workflow_id")
    if not workflow_id:
        raise ValueError(
            "register_managed_schedule_workflow requires "
            "descriptor.workflow_id (caller must mint a stable id)"
        )

    # Pre-compile triggers — fails before any persist.
    compiled = compile_descriptor_triggers(
        workflow_id=workflow_id, descriptor=descriptor,
    )

    # Build + persist via unbound path (system-authorized).
    from kernos.kernel.workflows.descriptor_parser import _build_workflow
    wf = _build_workflow(descriptor)
    registered = await workflow_registry._register_workflow_unbound(wf)

    # Hydrate runtime. Per-trigger failures are logged but don't
    # unwind the persist (mirrors STS C5b posture).
    instance_id = registered.instance_id
    member_id = descriptor.get("owner", "")
    for ct in compiled:
        try:
            await runtime.register(
                trigger_id=ct.trigger_id,
                instance_id=instance_id,
                workflow_id=registered.workflow_id,
                predicate=ct.predicate,
                member_id=member_id,
            )
        except Exception:
            logger.exception(
                "WTC v1 C5c-2-prep: runtime.register failed for "
                "managed-schedule trigger_id=%s workflow_id=%s; "
                "workflow row is persisted, runtime can rehydrate "
                "on next start",
                ct.trigger_id, registered.workflow_id,
            )

    return registered


# ---------------------------------------------------------------------------
# Workflow ID minting — deterministic-enough for caller use
# ---------------------------------------------------------------------------


def mint_managed_schedule_workflow_id() -> str:
    """Return a UUID-based workflow_id with a managed-schedule
    prefix so list/find lookups can identify the source at a glance.
    The caller passes this into ``schedule_to_descriptor``."""
    return f"ms_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# Metadata helpers — for list/pause/resume/remove read-back
# ---------------------------------------------------------------------------


def is_managed_schedule_workflow(wf_metadata: dict | None) -> bool:
    if not wf_metadata:
        return False
    return bool(wf_metadata.get(MANAGED_SCHEDULE_METADATA_KEY))


def read_managed_schedule_metadata(wf_metadata: dict | None) -> dict[str, Any]:
    """Extract the manage_schedule fields from a workflow's
    metadata dict, returning an empty dict for non-managed-schedule
    rows. Used by the future list/pause/resume/remove path."""
    if not is_managed_schedule_workflow(wf_metadata):
        return {}
    fields = (
        "manage_schedule_action_type",
        "manage_schedule_action_params",
        "delivery_class",
        "notify_via",
        "space_id",
        "conversation_id",
        "condition_type",
        "recurrence",
        "event_filter",
        "event_lead_minutes",
        "member_id",
    )
    return {k: wf_metadata.get(k) for k in fields if k in (wf_metadata or {})}


__all__ = [
    "EVENT_TYPE_CALENDAR_OBSERVED",
    "MANAGE_SCHEDULE_ACTION_NOTIFY",
    "MANAGE_SCHEDULE_ACTION_TOOL_CALL",
    "MANAGED_SCHEDULE_METADATA_KEY",
    "is_managed_schedule_workflow",
    "mint_managed_schedule_workflow_id",
    "read_managed_schedule_metadata",
    "register_managed_schedule_workflow",
    "schedule_to_descriptor",
]
