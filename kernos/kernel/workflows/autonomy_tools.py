"""Spec 6 autonomy-loop substrate-tier tools.

Three substrate-tier tools that workflows call to drive the
SELF-IMPROVEMENT-WORKFLOW-V1 autonomy loop:

  * ``transition_friction_pattern_lifecycle`` — operator-only
    transition of a friction pattern's lifecycle_state via the
    FrictionPatternStore.transition_lifecycle path. Used by the
    self_improvement workflow to mark a pattern resolved after a
    coding-session fix lands.

  * ``record_friction_pattern_recurrence`` — operator-only recording
    of a recurrence on a resolved pattern. Drives the
    resolved → reactivated transition when the recurrence threshold
    is crossed (the substrate primitive owns the threshold logic).
    The autonomy loop calls this when it observes a pattern
    re-occurring after a prior resolution.

  * ``emit_autonomy_loop_event`` — operator-only append to the
    ``autonomy_loop_outcomes`` ledger via WorkflowLedger. Records
    the canonical outcome event for each autonomy-loop turn
    (workflow execution end + addresses_friction_patterns linkage).

All three are classified substrate_tier by the Spec 5 governance
classifier (their tool_ids are in SUBSTRATE_TOOL_IDS); Kernos cannot
author workflows that call them. Only architect (via direct authoring)
or operator (via the production autonomy-loop helper) can execute.

The tools share a common shape:

  * Take an ``AuthoringContext`` for identity + operator-actor check
    (fail-closed when KERNOS_OPERATOR_ACTOR_ID is unset).
  * Return an ``AutonomyToolResult`` with success flag, value, and
    structured error category.
  * Emit a ``workflow_autonomy.action_recorded`` audit event via
    event_stream so the audit trail is durable.

The ``handle_*_tool`` wrappers translate the tool-dispatch shape
(tool_id + args + instance_id + member_id keyword args) into the
direct function signature. Production wiring (commit 6) registers
these handlers in the reasoning.py kernel-tool dispatch path.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kernos.kernel.workflows.authoring import (
    ACTOR_OPERATOR,
    AuthoringContext,
    _is_operator,
    derive_actor_kind,
)

if TYPE_CHECKING:
    from kernos.kernel.friction_patterns import FrictionPatternStore
    from kernos.kernel.workflows.ledger import WorkflowLedger

logger = logging.getLogger(__name__)


# Error categories surfaced by the autonomy tools. Keep these distinct
# from Spec 5's ValidationError categories so callers can pattern-match
# on the autonomy-tool surface without conflict.
CAT_AUTONOMY_NOT_AUTHORIZED = "autonomy_not_authorized"
CAT_AUTONOMY_INVALID_ARGS = "autonomy_invalid_args"
CAT_AUTONOMY_SUBSTRATE_ERROR = "autonomy_substrate_error"

VALID_AUTONOMY_TOOL_NAMES: frozenset[str] = frozenset({
    "transition_friction_pattern_lifecycle",
    "record_friction_pattern_recurrence",
    "emit_autonomy_loop_event",
})


@dataclass(frozen=True)
class AutonomyToolError:
    """Structured error from an autonomy-loop tool."""

    category: str
    message: str


@dataclass
class AutonomyToolResult:
    """Return shape for the autonomy-loop tools. ``errors`` is non-empty
    iff ``success=False``. ``value`` carries the tool-specific result
    payload (e.g., reactivation flag for record_recurrence; updated
    pattern dataclass for transition_lifecycle)."""

    success: bool
    tool: str = ""
    value: Any = None
    errors: list[AutonomyToolError] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def transition_friction_pattern_lifecycle(
    *,
    ctx: AuthoringContext,
    pattern_store: "FrictionPatternStore",
    instance_id: str,
    pattern_id: str,
    new_state: str,
    resolved_by_spec: str = "",
) -> AutonomyToolResult:
    """Operator-only friction-pattern lifecycle transition.

    Wraps FrictionPatternStore.transition_lifecycle with the
    operator-actor gate and the autonomy-tool result shape.

    On success, ``result.value`` is the updated FrictionPattern
    dataclass (carrying the new ``lifecycle_state`` and any incremented
    ``active_epoch`` per Spec 6 commit 1 substrate plumbing).
    """
    if not _is_operator(ctx):
        return AutonomyToolResult(
            success=False,
            tool="transition_friction_pattern_lifecycle",
            errors=[AutonomyToolError(
                category=CAT_AUTONOMY_NOT_AUTHORIZED,
                message=(
                    f"transition_friction_pattern_lifecycle requires "
                    f"operator actor; got actor_kind={ctx.actor_kind}"
                ),
            )],
        )
    if not instance_id or not pattern_id or not new_state:
        return AutonomyToolResult(
            success=False,
            tool="transition_friction_pattern_lifecycle",
            errors=[AutonomyToolError(
                category=CAT_AUTONOMY_INVALID_ARGS,
                message=(
                    f"instance_id, pattern_id, and new_state are all "
                    f"required (got "
                    f"instance_id={instance_id!r}, "
                    f"pattern_id={pattern_id!r}, "
                    f"new_state={new_state!r})"
                ),
            )],
        )
    try:
        updated = await pattern_store.transition_lifecycle(
            instance_id, pattern_id, new_state,
            resolved_by_spec=resolved_by_spec,
        )
    except Exception as exc:
        return AutonomyToolResult(
            success=False,
            tool="transition_friction_pattern_lifecycle",
            errors=[AutonomyToolError(
                category=CAT_AUTONOMY_SUBSTRATE_ERROR,
                message=str(exc),
            )],
        )
    await _emit_autonomy_action_record(
        operation="transition_friction_pattern_lifecycle",
        actor=ctx,
        instance_id=instance_id,
        payload={
            "pattern_id": pattern_id,
            "new_state": new_state,
            "resolved_by_spec": resolved_by_spec,
            "active_epoch": updated.active_epoch,
        },
    )
    return AutonomyToolResult(
        success=True,
        tool="transition_friction_pattern_lifecycle",
        value=updated,
        extra={
            "lifecycle_state": updated.lifecycle_state,
            "active_epoch": updated.active_epoch,
        },
    )


async def record_friction_pattern_recurrence(
    *,
    ctx: AuthoringContext,
    pattern_store: "FrictionPatternStore",
    instance_id: str,
    pattern_id: str,
    observed_at: str,
    report_path: str = "",
    classifier_score: float = 0.0,
    classified_by: str = "auto-signal-type",
    space_id: str = "",
    member_id: str = "",
) -> AutonomyToolResult:
    """Operator-only recurrence recording on a resolved pattern.

    Wraps FrictionPatternStore.record_recurrence with the
    operator-actor gate and the autonomy-tool result shape.

    On success, ``result.value`` is the bool flag indicating whether
    the recurrence drove the pattern through the threshold to
    REACTIVATED state. ``extra['triggered_reactivation']`` mirrors
    the bool for inspection.
    """
    if not _is_operator(ctx):
        return AutonomyToolResult(
            success=False,
            tool="record_friction_pattern_recurrence",
            errors=[AutonomyToolError(
                category=CAT_AUTONOMY_NOT_AUTHORIZED,
                message=(
                    f"record_friction_pattern_recurrence requires "
                    f"operator actor; got actor_kind={ctx.actor_kind}"
                ),
            )],
        )
    if not instance_id or not pattern_id:
        return AutonomyToolResult(
            success=False,
            tool="record_friction_pattern_recurrence",
            errors=[AutonomyToolError(
                category=CAT_AUTONOMY_INVALID_ARGS,
                message=(
                    f"instance_id and pattern_id are required (got "
                    f"instance_id={instance_id!r}, "
                    f"pattern_id={pattern_id!r})"
                ),
            )],
        )
    # Spec 6 commit 7: when caller omits observed_at (the autonomy
    # loop's workflow YAML doesn't carry a timestamp ref under the
    # canonical Spec 4 ref grammar), default to current UTC. Caller-
    # supplied observed_at still wins for explicit-timestamp paths.
    if not observed_at:
        from datetime import datetime, timezone
        observed_at = datetime.now(timezone.utc).isoformat()
    # Spec 6 commit 7: state-aware routing. The substrate primitives
    # are state-specific (record_recurrence works on RESOLVED;
    # record_occurrence works on ACTIVE / REACTIVATED). The autonomy-
    # loop workflow fires AFTER the substrate has already reactivated
    # the pattern (the trigger event ``friction.pattern_frequency_
    # threshold_exceeded`` is downstream of the substrate's
    # threshold-crossing reactivation). At workflow-execution time
    # the pattern is in ACTIVE / REACTIVATED state, not RESOLVED —
    # the autonomy tool routes to record_occurrence accordingly so
    # the workflow's "log the recurrence observation that triggered
    # us" semantic intent works regardless of substrate state.
    from kernos.kernel.friction_patterns import (
        LIFECYCLE_ACTIVE,
        LIFECYCLE_REACTIVATED,
        LIFECYCLE_RESOLVED,
    )
    try:
        current = await pattern_store.get_pattern(instance_id, pattern_id)
        if current is None:
            return AutonomyToolResult(
                success=False,
                tool="record_friction_pattern_recurrence",
                errors=[AutonomyToolError(
                    category=CAT_AUTONOMY_SUBSTRATE_ERROR,
                    message=(
                        f"pattern {pattern_id!r} not found in instance "
                        f"{instance_id!r}"
                    ),
                )],
            )
        if current.lifecycle_state == LIFECYCLE_RESOLVED:
            triggered = await pattern_store.record_recurrence(
                instance_id=instance_id,
                pattern_id=pattern_id,
                observed_at=observed_at,
                report_path=report_path,
                classifier_score=classifier_score,
                classified_by=classified_by,
                space_id=space_id,
                member_id=member_id,
            )
        elif current.lifecycle_state in (LIFECYCLE_ACTIVE, LIFECYCLE_REACTIVATED):
            await pattern_store.record_occurrence(
                instance_id=instance_id,
                pattern_id=pattern_id,
                observed_at=observed_at,
                report_path=report_path,
                classifier_score=classifier_score,
                classified_by=classified_by,
                space_id=space_id,
                member_id=member_id,
            )
            triggered = False  # already-reactivated patterns can't
            # re-trigger reactivation via this path; the autonomy loop
            # records the observation but the substrate state is
            # unchanged.
        else:
            return AutonomyToolResult(
                success=False,
                tool="record_friction_pattern_recurrence",
                errors=[AutonomyToolError(
                    category=CAT_AUTONOMY_SUBSTRATE_ERROR,
                    message=(
                        f"pattern {pattern_id!r} is "
                        f"{current.lifecycle_state}; cannot record "
                        f"recurrence/occurrence"
                    ),
                )],
            )
    except Exception as exc:
        return AutonomyToolResult(
            success=False,
            tool="record_friction_pattern_recurrence",
            errors=[AutonomyToolError(
                category=CAT_AUTONOMY_SUBSTRATE_ERROR,
                message=str(exc),
            )],
        )
    await _emit_autonomy_action_record(
        operation="record_friction_pattern_recurrence",
        actor=ctx,
        instance_id=instance_id,
        payload={
            "pattern_id": pattern_id,
            "observed_at": observed_at,
            "triggered_reactivation": triggered,
            "classified_by": classified_by,
            "report_path": report_path,
        },
    )
    return AutonomyToolResult(
        success=True,
        tool="record_friction_pattern_recurrence",
        value=triggered,
        extra={"triggered_reactivation": triggered},
    )


async def emit_autonomy_loop_event(
    *,
    ctx: AuthoringContext,
    ledger: "WorkflowLedger",
    instance_id: str,
    workflow_id: str,
    outcome: str,
    addresses_friction_patterns: list[str] | tuple[str, ...] = (),
    extra_payload: dict | None = None,
) -> AutonomyToolResult:
    """Operator-only autonomy-loop outcome event.

    Appends to the ``autonomy_loop_outcomes`` ledger via WorkflowLedger.
    Records the canonical outcome of each autonomy-loop turn so the
    catalog can correlate fix events with the originating friction
    patterns (the before/after measurement substrate from the
    five-spec roadmap).

    ``outcome`` is a free-form string ("completed", "failed",
    "aborted", "skipped:authoring_inactive"). ``addresses_friction_patterns``
    is a list of pattern_ids the workflow turn addressed. The autonomy
    loop reads this list later to update lifecycle states / measure
    fix effectiveness.
    """
    if not _is_operator(ctx):
        return AutonomyToolResult(
            success=False,
            tool="emit_autonomy_loop_event",
            errors=[AutonomyToolError(
                category=CAT_AUTONOMY_NOT_AUTHORIZED,
                message=(
                    f"emit_autonomy_loop_event requires operator actor; "
                    f"got actor_kind={ctx.actor_kind}"
                ),
            )],
        )
    if not instance_id or not workflow_id or not outcome:
        return AutonomyToolResult(
            success=False,
            tool="emit_autonomy_loop_event",
            errors=[AutonomyToolError(
                category=CAT_AUTONOMY_INVALID_ARGS,
                message=(
                    f"instance_id, workflow_id, and outcome are all "
                    f"required (got "
                    f"instance_id={instance_id!r}, "
                    f"workflow_id={workflow_id!r}, "
                    f"outcome={outcome!r})"
                ),
            )],
        )
    entry_payload: dict[str, Any] = {
        "agent_or_action": "emit_autonomy_loop_event",
        "workflow_id": workflow_id,
        "outcome": outcome,
        "addresses_friction_patterns": list(addresses_friction_patterns),
    }
    if extra_payload:
        entry_payload.update(extra_payload)
    try:
        # WorkflowLedger.append(instance_id, workflow_id, entry):
        # entries live under data/<instance>/workflows/autonomy_loop_outcomes/ledger.md
        # so the canonical autonomy-loop outcome stream has a single
        # known path per instance.
        await ledger.append(
            instance_id,
            "autonomy_loop_outcomes",
            entry_payload,
        )
    except Exception as exc:
        return AutonomyToolResult(
            success=False,
            tool="emit_autonomy_loop_event",
            errors=[AutonomyToolError(
                category=CAT_AUTONOMY_SUBSTRATE_ERROR,
                message=str(exc),
            )],
        )
    await _emit_autonomy_action_record(
        operation="emit_autonomy_loop_event",
        actor=ctx,
        instance_id=instance_id,
        payload=entry_payload,
    )
    return AutonomyToolResult(
        success=True,
        tool="emit_autonomy_loop_event",
        value=entry_payload,
        extra={"workflow_id": workflow_id, "outcome": outcome},
    )


# ---------------------------------------------------------------------------
# Audit event emission
# ---------------------------------------------------------------------------


async def _emit_autonomy_action_record(
    *,
    operation: str,
    actor: AuthoringContext,
    instance_id: str,
    payload: dict,
) -> None:
    """Mirrors the Spec 5 ``workflow_authoring.action_recorded`` pattern
    for the autonomy-loop tools. The audit event captures the operator
    identity + operation + payload so the autonomy loop's substrate
    changes are traceable in the event stream alongside the workflow's
    own ``workflow.execution_*`` events.

    Best-effort: if event emission fails the tool result is NOT
    affected (the substrate transition already committed). The
    operator can still inspect substrate state directly.
    """
    from kernos.kernel import event_stream

    audit_payload = {
        "operation": operation,
        "actor_id": actor.actor_id,
        "actor_kind": actor.actor_kind,
        **payload,
    }
    try:
        await event_stream.emit(
            instance_id or "system",
            "workflow_autonomy.action_recorded",
            audit_payload,
            member_id=actor.actor_id or None,
        )
    except Exception as exc:
        logger.debug(
            "WORKFLOW_AUTONOMY_RECORD_EMIT_FAILED operation=%s error=%s",
            operation, exc,
        )


# ---------------------------------------------------------------------------
# Tool-dispatch handlers (call_tool shape)
# ---------------------------------------------------------------------------
#
# These handlers translate the kernel-tool dispatch shape
# (tool_id, args, instance_id, member_id) into the direct function
# signatures above. Production wiring (commit 6) registers these in
# the reasoning.py kernel-tool dispatch path so workflows calling
# ``call_tool`` with one of the three tool_ids route through here.


def _result_to_dict(result: AutonomyToolResult) -> dict:
    """Convert an ``AutonomyToolResult`` to a JSON-serializable dict
    for return through CallToolAction. The workflow's step-output
    envelope serializes the return value via ``json.dumps``; a
    dataclass would fail serialization and break downstream
    ``{step.X.value.field}`` refs.

    ``value`` is stringified when it's a FrictionPattern dataclass
    (transition_lifecycle return); for primitive ``value`` types
    (bool from record_recurrence; dict from emit_autonomy_loop_event)
    the raw value is preserved."""
    raw_value = result.value
    if hasattr(raw_value, "__dict__") and not isinstance(raw_value, dict):
        # Dataclass / object — convert to dict via __dict__ for JSON
        # compatibility. Tuple fields convert to lists for the
        # canonical JSON shape.
        value_dict = {
            k: (list(v) if isinstance(v, tuple) else v)
            for k, v in raw_value.__dict__.items()
        }
    else:
        value_dict = raw_value
    return {
        "success": result.success,
        "tool": result.tool,
        "value": value_dict,
        "errors": [
            {"category": e.category, "message": e.message}
            for e in result.errors
        ],
        "extra": dict(result.extra) if result.extra else {},
    }


async def handle_transition_friction_pattern_lifecycle_tool(
    *,
    pattern_store: "FrictionPatternStore",
    instance_id: str,
    member_id: str,
    args: dict,
) -> dict:
    """Dispatch wrapper for ``transition_friction_pattern_lifecycle``.

    Returns a JSON-serializable dict (not ``AutonomyToolResult``)
    so the workflow step output envelope serializes cleanly.
    """
    ctx = AuthoringContext(
        actor_id=member_id, actor_kind=derive_actor_kind(member_id),
    )
    result = await transition_friction_pattern_lifecycle(
        ctx=ctx,
        pattern_store=pattern_store,
        instance_id=instance_id,
        pattern_id=args.get("pattern_id", ""),
        new_state=args.get("new_state", ""),
        resolved_by_spec=args.get("resolved_by_spec", ""),
    )
    return _result_to_dict(result)


async def handle_record_friction_pattern_recurrence_tool(
    *,
    pattern_store: "FrictionPatternStore",
    instance_id: str,
    member_id: str,
    args: dict,
) -> dict:
    """Dispatch wrapper for ``record_friction_pattern_recurrence``."""
    ctx = AuthoringContext(
        actor_id=member_id, actor_kind=derive_actor_kind(member_id),
    )
    result = await record_friction_pattern_recurrence(
        ctx=ctx,
        pattern_store=pattern_store,
        instance_id=instance_id,
        pattern_id=args.get("pattern_id", ""),
        observed_at=args.get("observed_at", ""),
        report_path=args.get("report_path", ""),
        classifier_score=float(args.get("classifier_score", 0.0)),
        classified_by=args.get("classified_by", "auto-signal-type"),
        space_id=args.get("space_id", ""),
        member_id=args.get("member_id", ""),
    )
    return _result_to_dict(result)


async def handle_emit_autonomy_loop_event_tool(
    *,
    ledger: "WorkflowLedger",
    instance_id: str,
    member_id: str,
    args: dict,
) -> dict:
    """Dispatch wrapper for ``emit_autonomy_loop_event``."""
    ctx = AuthoringContext(
        actor_id=member_id, actor_kind=derive_actor_kind(member_id),
    )
    result = await emit_autonomy_loop_event(
        ctx=ctx,
        ledger=ledger,
        instance_id=instance_id,
        workflow_id=args.get("workflow_id", ""),
        outcome=args.get("outcome", ""),
        addresses_friction_patterns=tuple(
            args.get("addresses_friction_patterns") or ()
        ),
        extra_payload=args.get("extra_payload") or None,
    )
    return _result_to_dict(result)


# ---------------------------------------------------------------------------
# Spec 6 parallel workflow handlers: bridge ask/read for workflow callers
# ---------------------------------------------------------------------------
#
# The self_improvement workflow's action_sequence calls into the
# coding-session bridge to consult a coding session (typically CC) and
# wait for the response. The bridge's existing handlers
# (``handle_ask_coding_session`` / ``handle_read_coding_session_response``)
# are member-facing — they take ``member_id`` + ``active_space_id``
# arguments shaped for the conversational caller. Workflows have a
# synthetic execution context with no real "member" or "active space";
# the for_workflow wrappers normalize that context so the workflow's
# ``call_tool`` action can route through without re-deriving the
# member-facing arg shape.


async def handle_ask_coding_session_for_workflow(
    *,
    instance_id: str,
    member_id: str,
    args: dict,
    data_dir: str,
) -> dict:
    """Workflow-facing wrapper for ``handle_ask_coding_session``.

    Args dict shape (workflow's call_tool ``args`` parameter):
      * ``target``: which coding session to consult ("cc", etc.)
      * ``question``: the question to ask
      * ``context``: optional dict of extra context fields
      * ``active_space_id``: optional; defaults to empty

    Returns a workflow-friendly dict shape: ``{"success": bool,
    "request_id": str, "summary": str, "execution_state": str}``.
    The ``request_id`` is the load-bearing field the workflow's next
    step (read_coding_session_response_for_workflow) consumes.
    """
    from kernos.kernel.coding_session_bridge import handle_ask_coding_session

    target = args.get("target", "")
    question = args.get("question", "")
    context = args.get("context") or {}
    active_space_id = args.get("active_space_id", "")
    summary, record = await handle_ask_coding_session(
        instance_id=instance_id,
        member_id=member_id,
        active_space_id=active_space_id,
        data_dir=data_dir,
        target=target,
        question=question,
        context=context,
    )
    # Extract request_id from the receipt_refs (the bridge embeds it
    # there per its existing contract). Fall back to empty string if
    # the call failed before request_id assignment.
    request_id = ""
    if record.receipt_refs:
        request_id = record.receipt_refs[0]
    return {
        "success": record.execution_state in ("attempted", "completed"),
        "request_id": request_id,
        "summary": summary,
        "execution_state": record.execution_state,
    }


async def handle_read_coding_session_response_for_workflow(
    *,
    instance_id: str,
    member_id: str,
    args: dict,
    data_dir: str,
) -> dict:
    """Workflow-facing wrapper for ``handle_read_coding_session_response``.

    Args dict shape:
      * ``request_id``: the request_id returned by ask_coding_session_for_workflow.

    Returns workflow-friendly dict: ``{"success": bool,
    "request_id": str, "summary": str, "execution_state": str,
    "investigation_outcome": str}``. The autonomy-loop workflow's
    emit_outcome step refs ``{step.read_response.value.investigation_outcome}``
    so the canonical outcome from the coding session's response file
    flows into the autonomy_loop_outcomes ledger (architect's v7.3
    dynamic-outcome modification — capture CC's actual outcome in the
    audit trail, not a hardcoded "completed").
    """
    import json as _json
    from pathlib import Path as _Path

    from kernos.kernel.coding_session_bridge import (
        handle_read_coding_session_response,
    )

    request_id = args.get("request_id", "")
    summary, record = await handle_read_coding_session_response(
        instance_id=instance_id,
        data_dir=data_dir,
        request_id=request_id,
    )
    # If the bridge accepted the response, read the response file
    # again to surface investigation_outcome to the workflow.
    # Codex round-1 M1 fold + round-2 MEDIUM 2 fold: both surfaces
    # (bridge event_stream emission + workflow ledger entry) route
    # through the SAME normalize_investigation_outcome helper so
    # the autonomy_loop_outcomes ledger entry and the
    # coding_consult.response_received event payload always agree
    # on the canonical outcome for every input case (missing,
    # empty, valid, invalid).
    investigation_outcome = ""
    if record.execution_state == "completed":
        from kernos.kernel.coding_session_bridge import (
            normalize_investigation_outcome,
        )
        response_path = (
            _Path(data_dir) / instance_id / "coding_session_bridge"
            / "responses" / f"{request_id}.json"
        )
        try:
            with response_path.open("r", encoding="utf-8") as fp:
                response_data = _json.load(fp)
            investigation_outcome = normalize_investigation_outcome(
                response_data.get("investigation_outcome"),
            )
        except (FileNotFoundError, _json.JSONDecodeError, OSError):
            # Best-effort: if the file is unreadable here (rare —
            # the bridge just read it), pass the missing-value
            # sentinel through normalization for the canonical
            # fallback ("completed" per the helper's contract).
            investigation_outcome = normalize_investigation_outcome(None)
    return {
        "success": record.execution_state in ("attempted", "completed"),
        "request_id": request_id,
        "summary": summary,
        "execution_state": record.execution_state,
        "investigation_outcome": investigation_outcome,
    }


__all__ = [
    "AutonomyToolError",
    "AutonomyToolResult",
    "CAT_AUTONOMY_INVALID_ARGS",
    "CAT_AUTONOMY_NOT_AUTHORIZED",
    "CAT_AUTONOMY_SUBSTRATE_ERROR",
    "VALID_AUTONOMY_TOOL_NAMES",
    "emit_autonomy_loop_event",
    "handle_ask_coding_session_for_workflow",
    "handle_emit_autonomy_loop_event_tool",
    "handle_read_coding_session_response_for_workflow",
    "handle_record_friction_pattern_recurrence_tool",
    "handle_transition_friction_pattern_lifecycle_tool",
    "record_friction_pattern_recurrence",
    "transition_friction_pattern_lifecycle",
]
