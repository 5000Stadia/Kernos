"""Workflow authoring layer.

WORKFLOW-AUTHORING-PRIMITIVES-V1. Substrate that lets Kernos
(composition_tier) and the architect (any tier) author workflows
against Spec 4's execution primitives. All Kernos-authored
workflows require architect ratification at activation; this is the
safety boundary.

Eight architectural intents (per architect build directive):

  1. ``register_workflow(ctx, descriptor, governance_tier)`` —
     authoring entry point.
  2. ``register_trigger(ctx, workflow_id, event_type, predicate)`` —
     bind triggers; only allowed in registered_not_activated state
     (Codex round-1 Blocker 1).
  3. ``activate_workflow(ctx, workflow_id)`` — architect-only;
     transitions to active; re-runs validation.
  4. ``deactivate_workflow(ctx, workflow_id, reason)`` —
     architect-only; reversible.
  5. Validation feedback channel — structured ValidationError shape.
  6. Governance-tier classification — narrow substrate-tool-id list
     (architect Q1: conservative-by-default).
  7. Disposition guidance — orientation prompt + per-tool description.
  8. Composition with friction patterns — soft-prompted reflection
     on workflow_resolvable recurrence.

Architect's three calls on Spec 5 v2 open questions:

  * Q1: substrate-tool-id list is narrow for v1; expansion is a
    deliberate architect amendment.
  * Q2: validation re-run only at activation; no periodic check.
  * Q3: CAS-style SQL transitions are sufficient
    (registered_workflows.py); first-writer-wins.
"""
from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiosqlite

from kernos.kernel.integration.briefing import ActionStateRecord
from kernos.kernel.workflows.registered_workflows import (
    STATE_ACTIVE,
    STATE_DEACTIVATED,
    STATE_REGISTERED,
    TIER_COMPOSITION,
    TIER_SUBSTRATE,
    VALID_ACTIVATION_STATES,
    VALID_GOVERNANCE_TIERS,
    RegisteredWorkflow,
    get_activation_state,
    get_registered_workflow,
    insert_registered_workflow_within_txn,
    transition_to_active,
    transition_to_deactivated,
)

if TYPE_CHECKING:
    from kernos.kernel.workflows.execution_engine import ExecutionEngine
    from kernos.kernel.workflows.workflow_registry import Workflow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Identity context (Decision 9)
# ---------------------------------------------------------------------------


# Actor-kind discriminator.
ACTOR_KERNOS = "kernos"
ACTOR_ARCHITECT = "architect"
ACTOR_SYSTEM = "system"

VALID_ACTOR_KINDS = frozenset({ACTOR_KERNOS, ACTOR_ARCHITECT, ACTOR_SYSTEM})


@dataclass(frozen=True)
class AuthoringContext:
    """Identity context for authoring tools.

    Constructed by the caller / tool-dispatcher; cannot be mutated
    mid-call. The discriminator decides whether architect-only tools
    accept the request.

    actor_id: the concrete actor identifier (member_id for Kernos,
        operator_id for architect-via-operator, "system" for
        engine-internal calls).
    actor_kind: discriminator. "kernos" | "architect" | "system".
        "system" is used for engine-internal calls during workflow
        execution; system actors still require architect
        ratification at activation.
    """
    actor_id: str
    actor_kind: str

    def is_architect(self) -> bool:
        return self.actor_kind == ACTOR_ARCHITECT


def _is_architect(ctx: AuthoringContext) -> bool:
    """Architect-only tools call this. Fail-closed semantics: if
    KERNOS_ARCHITECT_ACTOR_ID is unset, NO actor passes the check.
    """
    expected = os.environ.get("KERNOS_ARCHITECT_ACTOR_ID", "")
    if not expected:
        return False
    return ctx.actor_kind == ACTOR_ARCHITECT and ctx.actor_id == expected


def derive_actor_kind(actor_id: str) -> str:
    """Helper for tool dispatchers: derive actor_kind from actor_id
    based on the env-var-set architect identity. Empty actor_id
    falls back to "system".
    """
    if not actor_id:
        return ACTOR_SYSTEM
    architect_id = os.environ.get("KERNOS_ARCHITECT_ACTOR_ID", "")
    if architect_id and actor_id == architect_id:
        return ACTOR_ARCHITECT
    return ACTOR_KERNOS


# ---------------------------------------------------------------------------
# Validation feedback (Decision 5 v2; 14 categories)
# ---------------------------------------------------------------------------


# Categories surfaced by the authoring validators.
CAT_MISSING_REQUIRED = "missing_required_field"
CAT_INVALID_VALUE = "invalid_value"
CAT_UNKNOWN_ACTION_TYPE = "unknown_action_type"
CAT_UNKNOWN_STEP_ID = "unknown_step_id"
CAT_UNKNOWN_GATE_NAME = "unknown_gate_name"
CAT_DUPLICATE_STEP_ID = "duplicate_step_id"
CAT_INVALID_IDENTIFIER = "invalid_identifier"
CAT_DANGLING_BRANCH_TARGET = "dangling_branch_target"
CAT_CIRCULAR_BRANCH = "circular_branch"
CAT_GOVERNANCE_TIER_VIOLATION = "governance_tier_violation"
CAT_GOVERNANCE_CLAIM_VIOLATION = "governance_claim_violation"
CAT_DESCRIPTOR_SHAPE_INVALID = "descriptor_shape_invalid"
CAT_PREDICATE_INVALID = "predicate_invalid"
CAT_NOT_AUTHORIZED = "not_authorized"
CAT_INVALID_ACTIVATION_STATE = "invalid_activation_state"

VALID_VALIDATION_CATEGORIES = frozenset({
    CAT_MISSING_REQUIRED, CAT_INVALID_VALUE, CAT_UNKNOWN_ACTION_TYPE,
    CAT_UNKNOWN_STEP_ID, CAT_UNKNOWN_GATE_NAME, CAT_DUPLICATE_STEP_ID,
    CAT_INVALID_IDENTIFIER, CAT_DANGLING_BRANCH_TARGET, CAT_CIRCULAR_BRANCH,
    CAT_GOVERNANCE_TIER_VIOLATION, CAT_GOVERNANCE_CLAIM_VIOLATION,
    CAT_DESCRIPTOR_SHAPE_INVALID, CAT_PREDICATE_INVALID, CAT_NOT_AUTHORIZED,
    CAT_INVALID_ACTIVATION_STATE,
})


@dataclass(frozen=True)
class ValidationError:
    """Structured validation feedback. Routed to Kernos's awareness
    layer so the next attempt can fix specifically what's wrong.
    """
    field_path: str
    category: str
    message: str
    severity: str = "error"


# ---------------------------------------------------------------------------
# Governance-tier classification (Decision 6 v2)
# ---------------------------------------------------------------------------


# Architect Q1 ruling: narrow substrate-tool-id list for v1. The
# hardcoded set stays small and auditable. Extending the list is a
# deliberate architect amendment via spec. Conservative-by-default-
# expansive-by-permission.
SUBSTRATE_TOOL_IDS: frozenset[str] = frozenset({
    # Authoring layer (this spec's own tools)
    "register_workflow", "register_trigger",
    "activate_workflow", "deactivate_workflow",
    # NOTE: workflow primitive (Spec 4) has no production tools that
    # modify substrate; descriptor changes happen via deactivate +
    # register new.
    # NOTE: friction-pattern catalog (Spec 1) has no production tools
    # currently exposed for substrate modification.
    # NOTE: bridge primitive (Spec 2) has no production tools that
    # modify substrate.
})

# State-key patterns whose mutation classifies as substrate change.
# Matched via fnmatch.fnmatchcase against the mark_state ``key``.
SUBSTRATE_STATE_KEY_PATTERNS: tuple[str, ...] = (
    "workflow.*", "registered_workflow.*",
    "friction_pattern.*",
    "coding_session_bridge.*",
)

# Ledger names whose append classifies as substrate change.
SUBSTRATE_LEDGER_NAMES: frozenset[str] = frozenset({
    "autonomy_loop_outcomes",
})

# Service IDs whose post classifies as substrate change.
# (Empty for v1; all current services are composition-tier.)
SUBSTRATE_SERVICE_IDS: frozenset[str] = frozenset()

# Canvas IDs whose write classifies as substrate change.
# (Empty for v1; canvases are content tier.)
SUBSTRATE_CANVAS_IDS: frozenset[str] = frozenset()


def _matches_substrate_state_key(key: str) -> bool:
    if not key:
        return False
    return any(
        fnmatch.fnmatchcase(key, pattern)
        for pattern in SUBSTRATE_STATE_KEY_PATTERNS
    )


def classify_governance_tier(wf: "Workflow") -> str:
    """Walk action_sequence + terminal_branches; return computed
    governance tier. substrate_tier if ANY action targets a
    substrate-modification surface; composition_tier otherwise.
    """
    all_actions = list(wf.action_sequence)
    for branch_actions in wf.terminal_branches.values():
        all_actions.extend(branch_actions)
    for action in all_actions:
        params = action.parameters or {}
        if action.action_type == "call_tool":
            tool_id = params.get("tool_id") or params.get("tool_name") or ""
            if tool_id in SUBSTRATE_TOOL_IDS:
                return TIER_SUBSTRATE
        elif action.action_type == "mark_state":
            key = params.get("key", "")
            if _matches_substrate_state_key(key):
                return TIER_SUBSTRATE
        elif action.action_type == "append_to_ledger":
            ledger = params.get("ledger", "")
            if ledger in SUBSTRATE_LEDGER_NAMES:
                return TIER_SUBSTRATE
        elif action.action_type == "post_to_service":
            service_id = params.get("service_id", "")
            if service_id in SUBSTRATE_SERVICE_IDS:
                return TIER_SUBSTRATE
        elif action.action_type == "write_canvas":
            canvas_id = params.get("canvas_id", "")
            if canvas_id in SUBSTRATE_CANVAS_IDS:
                return TIER_SUBSTRATE
        # notify_user, route_to_agent, branch: never substrate.
    return TIER_COMPOSITION


# ---------------------------------------------------------------------------
# Spec 5 ActionStateRecord builder (Decision 10)
# ---------------------------------------------------------------------------


# Authoring risk-level matrix. Activation is the safety boundary;
# explicit override to "high" surfaces the moment architect ratifies.
_AUTHORING_RISK_MATRIX: dict[str, str] = {
    "register_workflow": "medium",
    "register_trigger": "medium",
    "activate_workflow": "high",   # the safety boundary; loud audit
    "deactivate_workflow": "medium",
}


def _build_authoring_action_state_record(
    *,
    operation: str,
    actor: AuthoringContext,
    workflow_id: str = "",
    trigger_id: str = "",
    execution_state: str = "completed",
    error: str = "",
    errors: list[ValidationError] | None = None,
    extra_affected_objects: tuple[str, ...] = (),
) -> ActionStateRecord:
    """Builder for authoring-operation ActionStateRecords.

    Spec 5 Decision 10: overrides Spec 3's default risk derivation
    so activate_workflow surfaces as risk_level="high" (the safety
    boundary) while other authoring operations stay at medium.
    """
    import uuid
    risk_level = _AUTHORING_RISK_MATRIX.get(operation, "medium")
    op_class = "manage"
    affected: list[str] = []
    if workflow_id:
        affected.append(workflow_id)
    if trigger_id:
        affected.append(trigger_id)
    affected.extend(extra_affected_objects)
    receipt_refs: list[str] = []
    if workflow_id:
        receipt_refs.append(f"workflow_id:{workflow_id}")
    if trigger_id:
        receipt_refs.append(f"trigger_id:{trigger_id}")
    receipt_refs.append(f"actor_kind:{actor.actor_kind}")
    receipt_refs.append(f"actor_id:{actor.actor_id}")
    if errors:
        for err in errors:
            receipt_refs.append(
                f"error:{err.category}:{err.field_path}"
            )
    if execution_state == "completed":
        summary = f"authoring operation {operation} succeeded"
        if workflow_id:
            summary += f" (workflow_id={workflow_id})"
    else:
        summary = error or f"authoring operation {operation} failed"
    return ActionStateRecord(
        action_id=f"act_{uuid.uuid4().hex}",
        surface="workflow_authoring",
        operation=operation,
        operation_class=op_class,
        authorization_state="not_required",
        execution_state=execution_state,
        receipt_refs=tuple(receipt_refs),
        affected_objects=tuple(affected),
        partial_state=None,
        user_visible_summary=summary,
        risk_level=risk_level,
        evidence_class="",
        missing_metadata=False,
    )


# ---------------------------------------------------------------------------
# Authoring tool implementations
# ---------------------------------------------------------------------------


@dataclass
class AuthoringResult:
    """Return shape for authoring tools. ``errors`` is non-empty iff
    ``success=False``; Kernos's awareness layer reads it for
    self-correction.
    """
    success: bool
    workflow_id: str = ""
    trigger_id: str = ""
    errors: list[ValidationError] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


async def register_workflow(
    engine: "ExecutionEngine",
    ctx: AuthoringContext,
    descriptor: dict,
    governance_tier: str,
) -> AuthoringResult:
    """Register a workflow descriptor.

    Decision 1 v2 (atomic): both the Spec 4 ``workflows`` insert and
    the Spec 5 ``registered_workflows`` insert land in one
    transaction via ``_run_authoring_txn``.

    Architect Q1: Kernos cannot claim substrate_tier. The
    classifier walks the descriptor; mismatched claims are
    rejected with structured ValidationErrors.
    """
    # Lazy imports to avoid circular dependencies.
    from kernos.kernel.workflows.descriptor_parser import (
        DescriptorError,
        _build_workflow,
    )
    from kernos.kernel.workflows.workflow_registry import (
        WorkflowError,
        validate_workflow,
    )

    if governance_tier not in VALID_GOVERNANCE_TIERS:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="governance_tier",
                category=CAT_INVALID_VALUE,
                message=(
                    f"governance_tier value {governance_tier!r} "
                    f"violates constraint: must be one of "
                    f"{sorted(VALID_GOVERNANCE_TIERS)}"
                ),
            )],
        )
    if not isinstance(descriptor, dict):
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="descriptor",
                category=CAT_DESCRIPTOR_SHAPE_INVALID,
                message=(
                    f"descriptor must be a dict; got {type(descriptor).__name__}"
                ),
            )],
        )
    # Build Workflow dataclass via Spec 4 parser.
    try:
        wf = _build_workflow(descriptor)
    except (DescriptorError, KeyError) as exc:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="descriptor",
                category=CAT_DESCRIPTOR_SHAPE_INVALID,
                message=str(exc),
            )],
        )
    # Spec 4 validation (assigns global step ordinals as side
    # effect; catches grammar / branch-target / reference issues).
    try:
        validate_workflow(wf)
    except WorkflowError as exc:
        return AuthoringResult(
            success=False,
            errors=[_workflow_error_to_validation_error(exc)],
        )
    # Spec 5 governance-tier classification.
    computed_tier = classify_governance_tier(wf)
    is_architect = _is_architect(ctx)
    # Kernos cannot claim substrate_tier (Codex High 4).
    if not is_architect and governance_tier == TIER_SUBSTRATE:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="governance_tier",
                category=CAT_GOVERNANCE_CLAIM_VIOLATION,
                message=(
                    f"actor_kind={ctx.actor_kind} cannot claim "
                    f"governance_tier=substrate_tier; only architect may"
                ),
            )],
        )
    # Kernos cannot register substrate_tier workflows (governance
    # boundary; substrate-level enforcement).
    if not is_architect and computed_tier == TIER_SUBSTRATE:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="action_sequence",
                category=CAT_GOVERNANCE_TIER_VIOLATION,
                message=(
                    "workflow's computed governance tier is "
                    "substrate_tier (substrate-modifying action "
                    "detected); Kernos cannot author substrate_tier "
                    "workflows"
                ),
            )],
        )
    # Effective tier: architect can over-classify (composition →
    # substrate) but Kernos cannot. Computed tier overrides
    # composition claims when computed is substrate.
    effective_tier = governance_tier
    if computed_tier == TIER_SUBSTRATE:
        effective_tier = TIER_SUBSTRATE  # always substrate when computed
    # Persist both rows in one transaction.
    async def _body(db: aiosqlite.Connection) -> None:
        await _register_workflow_uncommitted_in_txn(db, wf)
        await insert_registered_workflow_within_txn(
            db,
            workflow_id=wf.workflow_id,
            instance_id=wf.instance_id,
            governance_tier=effective_tier,
            computed_tier=computed_tier,
            authored_by=ctx.actor_id,
            architect_authored=is_architect,
        )
    try:
        await engine._run_workflow_txn(_body)
    except Exception as exc:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="descriptor",
                category=CAT_DESCRIPTOR_SHAPE_INVALID,
                message=f"persistence failure: {exc}",
            )],
        )
    return AuthoringResult(
        success=True,
        workflow_id=wf.workflow_id,
        extra={
            "governance_tier": effective_tier,
            "computed_tier": computed_tier,
        },
    )


async def _register_workflow_uncommitted_in_txn(
    db: aiosqlite.Connection,
    wf: "Workflow",
) -> None:
    """Spec 5 v2 Decision 1: uncommitted INSERT into the Spec 4
    ``workflows`` table inside an external transaction. The
    WorkflowRegistry's own register path is atomic on its own; this
    path exists specifically for the authoring layer to bundle the
    insert with the registered_workflows row in one transaction.

    Mirrors WorkflowRegistry._register_workflow_unbound's INSERT,
    minus the validate / atomic-commit boundary (validation already
    ran in the caller; commit happens at the txn body's COMMIT).
    """
    import json
    from dataclasses import asdict
    from kernos.kernel.workflows.workflow_registry import (
        _workflow_descriptor_blob,
    )

    # Validation + step-ordinal-assignment + grammar checks all ran
    # in the caller; we land the row exactly as the caller's wf
    # instance has it.
    descriptor_blob = _workflow_descriptor_blob(wf)
    await db.execute(
        "INSERT INTO workflows ("
        " workflow_id, instance_id, name, description, owner,"
        " version, status, descriptor_json, created_at,"
        " approval_event_id"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            wf.workflow_id, wf.instance_id, wf.name, wf.description,
            wf.owner, wf.version, wf.status, descriptor_blob,
            wf.created_at or _utc_now(), None,
        ),
    )


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def register_trigger(
    engine: "ExecutionEngine",
    ctx: AuthoringContext,
    workflow_id: str,
    event_type: str,
    predicate: dict,
) -> AuthoringResult:
    """Bind a trigger to a registered workflow.

    Decision 2 v2: REQUIRES workflow to be in
    ``registered_not_activated`` state (Codex Blocker 1). Active or
    deactivated workflows reject trigger registration; pattern is
    deactivate → register triggers → re-activate.
    """
    import uuid

    assert engine._db is not None
    assert engine._trigger_registry is not None
    # Look up workflow state.
    registered = await get_registered_workflow(
        engine._db, workflow_id=workflow_id,
    )
    if registered is None:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="workflow_id",
                category=CAT_UNKNOWN_STEP_ID,
                message=f"workflow_id {workflow_id!r} is not registered",
            )],
        )
    if registered.activation_state != STATE_REGISTERED:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="workflow_id",
                category=CAT_INVALID_ACTIVATION_STATE,
                message=(
                    f"workflow {workflow_id} is in state "
                    f"{registered.activation_state}; operation "
                    f"register_trigger requires state(s) "
                    f"{[STATE_REGISTERED]}"
                ),
            )],
        )
    # Validate predicate via Spec 4's evaluator.
    from kernos.kernel.workflows.predicates import (
        PredicateError, validate as validate_predicate,
    )
    try:
        validate_predicate(predicate)
    except PredicateError as exc:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="predicate",
                category=CAT_PREDICATE_INVALID,
                message=f"predicate at predicate failed validation: {exc}",
            )],
        )
    # Mint trigger_id; register with the existing TriggerRegistry.
    trigger_id = f"trig_{uuid.uuid4().hex}"
    try:
        from kernos.kernel.workflows.trigger_registry import Trigger
        trigger = Trigger(
            trigger_id=trigger_id,
            workflow_id=workflow_id,
            instance_id=registered.instance_id,
            event_type=event_type,
            predicate=predicate,
        )
        await engine._trigger_registry.register_trigger(trigger)
    except Exception as exc:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="trigger",
                category=CAT_PREDICATE_INVALID,
                message=f"trigger registration failed: {exc}",
            )],
        )
    return AuthoringResult(
        success=True,
        workflow_id=workflow_id,
        trigger_id=trigger_id,
    )


async def activate_workflow(
    engine: "ExecutionEngine",
    ctx: AuthoringContext,
    workflow_id: str,
) -> AuthoringResult:
    """Architect-only activation gate.

    Decision 3 v2: re-runs validation + governance-tier
    classification (architect Q2: re-validation at activation
    only). State transition is CAS-style; idempotent on already-
    active.
    """
    from kernos.kernel.workflows.workflow_registry import (
        WorkflowError,
        validate_workflow,
    )

    assert engine._db is not None
    assert engine._workflow_registry is not None
    if not _is_architect(ctx):
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="ctx",
                category=CAT_NOT_AUTHORIZED,
                message=(
                    f"activate_workflow requires architect actor; "
                    f"got actor_kind={ctx.actor_kind}"
                ),
            )],
        )
    registered = await get_registered_workflow(
        engine._db, workflow_id=workflow_id,
    )
    if registered is None:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="workflow_id",
                category=CAT_UNKNOWN_STEP_ID,
                message=f"workflow_id {workflow_id!r} is not registered",
            )],
        )
    # Idempotent on already-active.
    if registered.activation_state == STATE_ACTIVE:
        return AuthoringResult(
            success=True,
            workflow_id=workflow_id,
            extra={"already_active": True},
        )
    # Re-run validation (Q2: substrate may have changed since
    # registration; architect ratification IS the safety boundary).
    wf = await engine._workflow_registry.get_workflow(workflow_id)
    if wf is None:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="workflow_id",
                category=CAT_UNKNOWN_STEP_ID,
                message=(
                    f"workflow {workflow_id!r} not found in workflows "
                    f"registry (registered_workflows row exists but "
                    f"workflow descriptor missing)"
                ),
            )],
        )
    try:
        validate_workflow(wf)
    except WorkflowError as exc:
        return AuthoringResult(
            success=False,
            errors=[_workflow_error_to_validation_error(exc)],
        )
    # CAS transition (Q3).
    async def _body(db: aiosqlite.Connection):
        return await transition_to_active(db, workflow_id=workflow_id)
    updated, current_state = await engine._run_workflow_txn(_body)
    if not updated:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="workflow_id",
                category=CAT_INVALID_ACTIVATION_STATE,
                message=(
                    f"workflow {workflow_id} is in state {current_state}; "
                    f"operation activate_workflow requires state(s) "
                    f"{[STATE_REGISTERED, STATE_DEACTIVATED]}"
                ),
            )],
        )
    return AuthoringResult(success=True, workflow_id=workflow_id)


async def deactivate_workflow(
    engine: "ExecutionEngine",
    ctx: AuthoringContext,
    workflow_id: str,
    *,
    reason: str = "",
) -> AuthoringResult:
    """Architect-only deactivation. Decision 4 v2: in-flight
    executions complete naturally; deactivation only stops NEW
    triggers from firing.
    """
    if not _is_architect(ctx):
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="ctx",
                category=CAT_NOT_AUTHORIZED,
                message=(
                    f"deactivate_workflow requires architect actor; "
                    f"got actor_kind={ctx.actor_kind}"
                ),
            )],
        )
    assert engine._db is not None
    registered = await get_registered_workflow(
        engine._db, workflow_id=workflow_id,
    )
    if registered is None:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="workflow_id",
                category=CAT_UNKNOWN_STEP_ID,
                message=f"workflow_id {workflow_id!r} is not registered",
            )],
        )
    if registered.activation_state == STATE_DEACTIVATED:
        return AuthoringResult(
            success=True,
            workflow_id=workflow_id,
            extra={"already_deactivated": True},
        )
    async def _body(db: aiosqlite.Connection):
        return await transition_to_deactivated(
            db, workflow_id=workflow_id, reason=reason,
        )
    updated, current_state = await engine._run_workflow_txn(_body)
    if not updated:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="workflow_id",
                category=CAT_INVALID_ACTIVATION_STATE,
                message=(
                    f"workflow {workflow_id} is in state {current_state}; "
                    f"operation deactivate_workflow requires state(s) "
                    f"{[STATE_ACTIVE]}"
                ),
            )],
        )
    return AuthoringResult(success=True, workflow_id=workflow_id)


def _workflow_error_to_validation_error(exc: Exception) -> ValidationError:
    """Map a Spec 4 WorkflowError to a Spec 5 ValidationError. The
    Spec 4 validator raises with descriptive messages naming the
    field; we route to the closest matching category.
    """
    msg = str(exc)
    msg_lower = msg.lower()
    # Pattern match on known message prefixes (Spec 4 validators are
    # consistent in their phrasing).
    if "duplicate step id" in msg_lower:
        category = CAT_DUPLICATE_STEP_ID
    elif "is not a known verb" in msg_lower:
        category = CAT_UNKNOWN_ACTION_TYPE
    elif "unknown step" in msg_lower or "references unknown step" in msg_lower:
        category = CAT_UNKNOWN_STEP_ID
    elif "unknown gate" in msg_lower:
        category = CAT_UNKNOWN_GATE_NAME
    elif "must match" in msg_lower:
        category = CAT_INVALID_IDENTIFIER
    elif "does not resolve to a declared step" in msg_lower:
        category = CAT_DANGLING_BRANCH_TARGET
    elif "cycle" in msg_lower:
        category = CAT_CIRCULAR_BRANCH
    else:
        category = CAT_INVALID_VALUE
    return ValidationError(
        field_path="descriptor",
        category=category,
        message=msg,
    )


# ---------------------------------------------------------------------------
# Disposition guidance (Decision 7 + path-1 manage_plan contrast)
# ---------------------------------------------------------------------------


# Per-tool description for the register_workflow surface. Teaches
# Kernos when a workflow is the right shape AND contrasts with
# manage_plan (the existing self-directed-plan primitive) so the
# decision rule is clean.
REGISTER_WORKFLOW_TOOL_DESCRIPTION = """\
Compose a workflow when the work is: (a) multi-step coordinated,
(b) dependent on async signals from external sources (CC, Codex,
operator response), (c) needs to resume after restart, OR (d)
requires retry / abort / branching semantics that the workflow
primitive handles deterministically.

For ad-hoc coordination that fits none of those, prefer simpler
primitives:

  - manage_plan (kernos.kernel.execution): use when multi-step
    work emerges from YOUR reasoning step-by-step in conversation.
    Each step is a reasoning turn; the user prompted you; each
    step depends on what you discover. Budget bounds (max_steps /
    max_tokens / max_time_s) are the discipline, not approval
    gates. Plans live in your workspace.

  - schedule (slash command): use for time-based recurrence
    without orchestration (a recurring reminder or check).

  - friction-pattern record only: use when the right move is to
    notice the pattern, not to act on it.

A workflow is the right shape when the STRUCTURE is known in
advance and you can describe the verbs + parameters as a YAML
descriptor. If you're not sure, start with manage_plan; if the
pattern recurs and stabilizes into a known shape, consider
templating it into a workflow.

All workflows authored at the composition_tier require architect
ratification at activation. The register call validates structure
and persists, but the workflow is DORMANT until the architect
activates it via activate_workflow. You can propose; architect
ratifies.
"""


# Orientation prompt addition. Loaded by Kernos's prompt-assembly
# layer when this spec is wired into production. Names both
# primitives so the disposition is symmetric — neither feels
# preferred, but each has a natural shape.
ORIENTATION_PROMPT_ADDITION = """\
You have two primitives for multi-step coordination:

PLANS (manage_plan): for open-ended exploration where you figure
out the shape as you go. Each step is a reasoning turn. The user
prompted you in conversation. Budget bounds keep you on track.

WORKFLOWS (register_workflow): for declarative orchestration with
known structure. Each step is a substrate verb (call_tool,
notify_user, branch, etc.). Triggered by events. Restart-safe.
Architect ratifies at activation.

Decision rule: if you can describe the steps in advance as a
YAML descriptor with specific tool calls and parameters, it's a
workflow. If the next step depends on what the prior step
discovers in your reasoning, it's a plan.

When uncertain, start with a plan. Plans become workflows
naturally once the pattern stabilizes.
"""


__all__ = [
    "ACTOR_ARCHITECT",
    "ACTOR_KERNOS",
    "ACTOR_SYSTEM",
    "AuthoringContext",
    "AuthoringResult",
    "CAT_CIRCULAR_BRANCH",
    "CAT_DANGLING_BRANCH_TARGET",
    "CAT_DESCRIPTOR_SHAPE_INVALID",
    "CAT_DUPLICATE_STEP_ID",
    "CAT_GOVERNANCE_CLAIM_VIOLATION",
    "CAT_GOVERNANCE_TIER_VIOLATION",
    "CAT_INVALID_ACTIVATION_STATE",
    "CAT_INVALID_IDENTIFIER",
    "CAT_INVALID_VALUE",
    "CAT_MISSING_REQUIRED",
    "CAT_NOT_AUTHORIZED",
    "CAT_PREDICATE_INVALID",
    "CAT_UNKNOWN_ACTION_TYPE",
    "CAT_UNKNOWN_GATE_NAME",
    "CAT_UNKNOWN_STEP_ID",
    "ORIENTATION_PROMPT_ADDITION",
    "REGISTER_WORKFLOW_TOOL_DESCRIPTION",
    "SUBSTRATE_CANVAS_IDS",
    "SUBSTRATE_LEDGER_NAMES",
    "SUBSTRATE_SERVICE_IDS",
    "SUBSTRATE_STATE_KEY_PATTERNS",
    "SUBSTRATE_TOOL_IDS",
    "VALID_ACTOR_KINDS",
    "VALID_VALIDATION_CATEGORIES",
    "ValidationError",
    "activate_workflow",
    "classify_governance_tier",
    "deactivate_workflow",
    "derive_actor_kind",
    "register_trigger",
    "register_workflow",
]
