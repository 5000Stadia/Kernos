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
import hashlib
import json
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
# Spec 6: operator actor kind. Carries substrate-tier authority for
# specific autonomy-loop tools (transition_friction_pattern_lifecycle,
# record_friction_pattern_recurrence, emit_autonomy_loop_event) while
# remaining distinct from architect — operators run the production
# assembly (bring-up, autonomy loop) but cannot ratify new workflows
# at activation time. Identity is set via ``KERNOS_OPERATOR_ACTOR_ID``
# env var (parallel to KERNOS_ARCHITECT_ACTOR_ID for fail-closed
# semantics when unset).
ACTOR_OPERATOR = "operator"

VALID_ACTOR_KINDS = frozenset({
    ACTOR_KERNOS, ACTOR_ARCHITECT, ACTOR_SYSTEM, ACTOR_OPERATOR,
})


@dataclass(frozen=True)
class AuthoringContext:
    """Identity context for authoring tools.

    Constructed by the caller / tool-dispatcher; cannot be mutated
    mid-call. The discriminator decides whether architect-only tools
    accept the request.

    actor_id: the concrete actor identifier (member_id for Kernos,
        operator_id for architect-via-operator, "system" for
        engine-internal calls).
    actor_kind: discriminator. "kernos" | "architect" | "system" |
        "operator". "system" is used for engine-internal calls
        during workflow execution; system actors still require
        architect ratification at activation. "operator" (Spec 6) is
        the production-assembly actor kind — substrate-tier authority
        for autonomy-loop tools (transition_friction_pattern_lifecycle,
        record_friction_pattern_recurrence, emit_autonomy_loop_event)
        but cannot ratify workflows at activation.
    """
    actor_id: str
    actor_kind: str

    def is_architect(self) -> bool:
        return self.actor_kind == ACTOR_ARCHITECT

    def is_operator(self) -> bool:
        return self.actor_kind == ACTOR_OPERATOR


# ---------------------------------------------------------------------------
# Canonical-descriptor helpers (Spec 5 13th + 16th amendment hardening)
# ---------------------------------------------------------------------------


def _compute_canonical_descriptor_json(descriptor: dict) -> str:
    """Single source of truth for the canonical-form computation.

    Spec 5 13th amendment introduced canonical persistence; the 16th
    amendment (Codex MEDIUM 1) tightens the canonical function so the
    digest is deterministic across Python versions and platforms and
    rejects non-JSON values that Python's default ``json.dumps`` would
    emit as ``NaN`` / ``Infinity`` tokens.

      * ``sort_keys=True`` — key-order invariance.
      * ``allow_nan=False`` — NaN/Infinity raise ``ValueError`` so the
        serializability check at the public register boundary catches
        them. NaN/Infinity are not valid JSON per RFC 7159; emitting
        them produces non-portable, non-deterministic strings.
      * ``separators=(",", ":")`` — the tightest separators (no
        whitespace) so the canonical bytes are minimal and identical
        whether the source dict was built by hand, parsed from YAML,
        or round-tripped through json.loads. Whitespace from Python's
        default separators (``", "`` / ``": "``) would otherwise drift
        if the json module's defaults ever changed.

    Callers MUST go through this helper for both write-time
    canonicalization (register_workflow) and read-side digest
    comparison (idempotent-replay detection, tests). Inlining
    ``json.dumps(..., sort_keys=True)`` elsewhere is a correctness
    regression.
    """
    return json.dumps(
        descriptor,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    )


def _compute_descriptor_digest(canonical_json: str) -> str:
    """SHA-256 hex digest of the canonical bytes. Pairs with
    :func:`_compute_canonical_descriptor_json` — the two together are
    the canonical-fingerprint pipeline."""
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _is_architect(ctx: AuthoringContext) -> bool:
    """Architect-only tools call this. Fail-closed semantics: if
    KERNOS_ARCHITECT_ACTOR_ID is unset, NO actor passes the check.
    """
    expected = os.environ.get("KERNOS_ARCHITECT_ACTOR_ID", "")
    if not expected:
        return False
    return ctx.actor_kind == ACTOR_ARCHITECT and ctx.actor_id == expected


def _is_operator(ctx: AuthoringContext) -> bool:
    """Operator-only tools (Spec 6 substrate-tier autonomy-loop tools)
    call this. Fail-closed semantics: if KERNOS_OPERATOR_ACTOR_ID is
    unset, NO actor passes the check — matches the architect
    discipline so misconfigured environments don't accidentally grant
    operator authority."""
    expected = os.environ.get("KERNOS_OPERATOR_ACTOR_ID", "")
    if not expected:
        return False
    return ctx.actor_kind == ACTOR_OPERATOR and ctx.actor_id == expected


def derive_actor_kind(actor_id: str) -> str:
    """Helper for tool dispatchers: derive actor_kind from actor_id
    based on the env-var-set architect / operator identities. Empty
    actor_id falls back to "system".

    Resolution order: architect identity wins over operator identity
    when both env vars happen to match the same actor_id (defensive;
    in practice they should be distinct). Otherwise: architect →
    operator → default Kernos.
    """
    if not actor_id:
        return ACTOR_SYSTEM
    architect_id = os.environ.get("KERNOS_ARCHITECT_ACTOR_ID", "")
    if architect_id and actor_id == architect_id:
        return ACTOR_ARCHITECT
    operator_id = os.environ.get("KERNOS_OPERATOR_ACTOR_ID", "")
    if operator_id and actor_id == operator_id:
        return ACTOR_OPERATOR
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


def _is_templated(value: object) -> bool:
    """Spec 5 post-impl Codex Blocker 3: detect template syntax in
    substrate-sensitive fields. Conservative-by-default: any value
    containing reference braces classifies as substrate-tier even
    though we can't statically resolve the final substrate target.
    """
    return isinstance(value, str) and "{" in value and "}" in value


def classify_governance_tier(wf: "Workflow") -> str:
    """Walk action_sequence + terminal_branches; return computed
    governance tier. substrate_tier if ANY action targets a
    substrate-modification surface OR if a substrate-sensitive
    selector field is templated (Codex Blocker 3 fold).

    The templated-selector rule closes a real bypass: a Kernos-
    authored workflow with ``tool_id: '{idea_payload.tool_id}'``
    would have classified as composition because the literal string
    isn't in SUBSTRATE_TOOL_IDS, but at runtime the resolver could
    substitute ``activate_workflow``. Conservative-by-default forces
    any templated substrate-sensitive field to substrate_tier so
    architect ratification is required.
    """
    all_actions = list(wf.action_sequence)
    for branch_actions in wf.terminal_branches.values():
        all_actions.extend(branch_actions)
    for action in all_actions:
        params = action.parameters or {}
        if action.action_type == "call_tool":
            tool_id = params.get("tool_id") or params.get("tool_name") or ""
            # Templated selector → conservative substrate (Codex B3).
            if _is_templated(tool_id):
                return TIER_SUBSTRATE
            if tool_id in SUBSTRATE_TOOL_IDS:
                return TIER_SUBSTRATE
        elif action.action_type == "mark_state":
            key = params.get("key", "")
            if _is_templated(key):
                return TIER_SUBSTRATE
            if _matches_substrate_state_key(key):
                return TIER_SUBSTRATE
        elif action.action_type == "append_to_ledger":
            ledger = params.get("ledger", "")
            if _is_templated(ledger):
                return TIER_SUBSTRATE
            if ledger in SUBSTRATE_LEDGER_NAMES:
                return TIER_SUBSTRATE
        elif action.action_type == "post_to_service":
            service_id = params.get("service_id", "")
            if _is_templated(service_id):
                return TIER_SUBSTRATE
            if service_id in SUBSTRATE_SERVICE_IDS:
                return TIER_SUBSTRATE
        elif action.action_type == "write_canvas":
            canvas_id = params.get("canvas_id", "")
            if _is_templated(canvas_id):
                return TIER_SUBSTRATE
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


def _emit_record_after(operation: str):
    """Decorator: after the wrapped authoring function returns,
    emit an ActionStateRecord via event_stream per Spec 5 post-impl
    Codex High 5. Captures both success and failure outcomes.

    The emitter (_emit_authoring_record) is defined below; the
    decorator looks it up at call time (not decoration time) so
    forward reference works.
    """
    import functools

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            ctx: AuthoringContext | None = (
                kwargs.get("ctx") if "ctx" in kwargs
                else (args[1] if len(args) > 1 else None)
            )
            instance_id = ""
            descriptor = kwargs.get("descriptor")
            if descriptor is None and len(args) > 2:
                descriptor = args[2]
            if isinstance(descriptor, dict):
                instance_id = descriptor.get("instance_id", "") or ""
            result = await fn(*args, **kwargs)
            if ctx is not None:
                await _emit_authoring_record(
                    operation=operation,
                    actor=ctx,
                    workflow_id=result.workflow_id,
                    trigger_id=result.trigger_id,
                    result=result,
                    instance_id=instance_id,
                )
            return result
        return wrapper
    return decorator


@_emit_record_after("register_workflow")
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

    Spec 5 13th amendment (v7.1/v7.2/v7.3 scope completion):

      * **Serializability check (M1 fold).** ``json.dumps`` runs
        against the descriptor at the public boundary BEFORE the
        Spec 4 parser touches it. Non-JSON-serializable values
        (datetime, set, custom objects from a yaml.safe_load tree
        that bypassed coercion, etc.) surface as a structured
        ValidationError with category ``CAT_DESCRIPTOR_SHAPE_INVALID``
        (V7.3.1: ValidationError, not AuthoringError — the latter
        doesn't exist in this module).
      * **Canonical persistence (v7.2).** The sorted-keys JSON form
        + its SHA-256 digest are persisted on the
        ``registered_workflows`` row alongside governance metadata
        (V7.1.1 placement).
      * **Idempotent register / SELECT-after-catch (v7.1).** On a
        body exception, ``_run_workflow_txn`` has already rolled
        back (v7.3 L1 premise 7 reframe: rollback-before-select,
        not post-IntegrityError visibility). The caller then
        SELECTs the existing ``registered_workflows`` row by
        ``workflow_id`` and compares digests. Match → idempotent
        success returning the prior row's ``workflow_id``;
        mismatch → distinct collision error (same workflow_id,
        different content); absent → original failure path.
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
    # Spec 5 13th amendment M1 + 16th amendment MEDIUM 1: canonical
    # form computed via the central helper so allow_nan=False rejects
    # NaN/Infinity (which Python's default json.dumps emits as
    # non-standard tokens) and tight separators keep the bytes
    # platform-deterministic. Non-JSON-serializable values raise
    # TypeError (datetime, set, custom classes); NaN/Infinity raise
    # ValueError under allow_nan=False; both route to the same
    # CAT_DESCRIPTOR_SHAPE_INVALID surface.
    try:
        canonical_json = _compute_canonical_descriptor_json(descriptor)
    except (TypeError, ValueError) as exc:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="descriptor",
                category=CAT_DESCRIPTOR_SHAPE_INVALID,
                message=f"descriptor not JSON-serializable: {exc}",
            )],
        )
    descriptor_digest = _compute_descriptor_digest(canonical_json)
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
    # Spec 5 14th amendment H1 fold: compile plural-triggers shape
    # at register time so a malformed descriptor.triggers list fails
    # loud BEFORE any persistence work. Key-presence-only guard
    # (v7.3 M2-fold semantics): pre-12th-amendment legacy descriptors
    # that use singular ``trigger:`` (Spec 4 shape) skip this path;
    # plural-triggers descriptors (production WTC path) get validated
    # here. PredicateValidationError per V7.3.2 (not TriggerError).
    if "triggers" in descriptor:
        from kernos.kernel.triggers import (
            PredicateValidationError,
            compile_descriptor_triggers,
        )
        try:
            compile_descriptor_triggers(
                workflow_id=wf.workflow_id, descriptor=descriptor,
            )
        except PredicateValidationError as exc:
            return AuthoringResult(
                success=False,
                errors=[ValidationError(
                    field_path="descriptor.triggers",
                    category=CAT_PREDICATE_INVALID,
                    message=str(exc),
                )],
            )
    # Spec 5 governance-tier classification.
    computed_tier = classify_governance_tier(wf)
    is_architect = _is_architect(ctx)
    # Spec 5 post-impl Codex Medium 9: aggregate governance errors
    # so a Kernos-issued request with BOTH a substrate-tier claim
    # AND a substrate-tier descriptor surfaces both findings in one
    # response (instead of one error per attempt).
    governance_errors: list[ValidationError] = []
    if not is_architect and governance_tier == TIER_SUBSTRATE:
        governance_errors.append(ValidationError(
            field_path="governance_tier",
            category=CAT_GOVERNANCE_CLAIM_VIOLATION,
            message=(
                f"actor_kind={ctx.actor_kind} cannot claim "
                f"governance_tier=substrate_tier; only architect may"
            ),
        ))
    if not is_architect and computed_tier == TIER_SUBSTRATE:
        governance_errors.append(ValidationError(
            field_path="action_sequence",
            category=CAT_GOVERNANCE_TIER_VIOLATION,
            message=(
                "workflow's computed governance tier is "
                "substrate_tier (substrate-modifying action or "
                "templated substrate-sensitive selector detected); "
                "Kernos cannot author substrate_tier workflows"
            ),
        ))
    if governance_errors:
        return AuthoringResult(
            success=False,
            errors=governance_errors,
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
            descriptor_json_canonical=canonical_json,
            descriptor_digest=descriptor_digest,
        )
    try:
        await engine._run_workflow_txn(_body)
    except aiosqlite.IntegrityError as exc:
        # 13th amendment SELECT-after-catch: post-rollback collision
        # disambiguation. The body's ROLLBACK already ran inside
        # _run_workflow_txn before re-raise (premise 7 reframe), so
        # this SELECT sees the prior committed winner without
        # transaction-visibility ambiguity.
        return await _handle_register_pk_collision(
            engine=engine,
            workflow_id=wf.workflow_id,
            new_digest=descriptor_digest,
            effective_tier=effective_tier,
            computed_tier=computed_tier,
            integrity_error=exc,
        )
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
            "descriptor_digest": descriptor_digest,
        },
    )


async def _handle_register_pk_collision(
    *,
    engine: "ExecutionEngine",
    workflow_id: str,
    new_digest: str,
    effective_tier: str,
    computed_tier: str,
    integrity_error: BaseException,
) -> AuthoringResult:
    """Disambiguate a post-rollback IntegrityError on register.

    v7.3 L1 reframe of premise 7: ``_run_workflow_txn`` rolls back
    on body exception before re-raising. Reads on the same
    connection AFTER rollback observe the committed winner via
    SQLite's normal visibility rules — no special "post-IntegrityError
    transaction visibility" semantics required.

    Outcomes:

      * **Match.** Prior digest equals ``new_digest`` → register call
        is idempotent. Return success with the existing row's
        ``workflow_id``; ``extra['idempotent_replay']`` flags the
        replay so callers/audits can see the dedupe happened.
      * **Mismatch.** Same ``workflow_id``, different canonical
        content → distinct collision; return
        ``CAT_DESCRIPTOR_SHAPE_INVALID`` so the caller treats this
        as a workflow-id reuse bug (deterministic IDs must derive
        from content).
      * **Absent.** ``workflow_id`` present in ``workflows`` but
        not in ``registered_workflows`` (or a non-PK IntegrityError
        surfaced via the same path) → preserve the original
        persistence-failure error so the surprise propagates.
    """
    assert engine._db is not None
    try:
        existing = await get_registered_workflow(
            engine._db, workflow_id=workflow_id,
        )
    except Exception as lookup_exc:
        # Lookup itself failed; treat as the original persistence
        # error rather than masking it.
        logger.warning(
            "REGISTER_COLLISION_LOOKUP_FAILED workflow_id=%s error=%s",
            workflow_id, lookup_exc,
        )
        existing = None
    if existing is None:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="descriptor",
                category=CAT_DESCRIPTOR_SHAPE_INVALID,
                message=f"persistence failure: {integrity_error}",
            )],
        )
    if existing.descriptor_digest and existing.descriptor_digest == new_digest:
        # Idempotent replay: same content, same workflow_id.
        return AuthoringResult(
            success=True,
            workflow_id=existing.workflow_id,
            extra={
                "governance_tier": existing.governance_tier,
                "computed_tier": existing.computed_tier or computed_tier,
                "descriptor_digest": existing.descriptor_digest,
                "idempotent_replay": True,
            },
        )
    # Either the digest differs or it's empty (pre-amendment row
    # the caller is trying to re-register with new content). Either
    # way, this is a distinct-collision: same workflow_id, different
    # content. Caller should derive a content-addressed workflow_id.
    return AuthoringResult(
        success=False,
        errors=[ValidationError(
            field_path="workflow_id",
            category=CAT_DESCRIPTOR_SHAPE_INVALID,
            message=(
                f"workflow_id {workflow_id!r} is already registered with a "
                f"different descriptor (existing digest={existing.descriptor_digest!r}, "
                f"new digest={new_digest!r}); choose a content-addressed "
                f"workflow_id or deactivate + re-author with a new id"
            ),
        )],
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


@_emit_record_after("register_trigger")
async def register_trigger(
    engine: "ExecutionEngine",
    ctx: AuthoringContext,
    workflow_id: str,
    event_type: str,
    predicate: dict,
) -> AuthoringResult:
    """Bind a trigger to a registered workflow.

    Spec 5 v2 Decision 2 / Codex round-1 Blocker 1: REQUIRES workflow
    to be in ``registered_not_activated`` state. Active or
    deactivated workflows reject trigger registration; pattern is
    deactivate → register triggers → re-activate.

    Spec 5 post-impl Codex Blocker 2: the activation-state read +
    trigger row INSERT happen in ONE transaction via
    _run_workflow_txn so an architect activation that commits
    between the two steps can't slip a trigger past ratification.
    The TriggerRegistry cache is updated after commit via a
    cache-only method on the registry; cache lag is recovered by
    the next match cycle's read.
    """
    import uuid

    assert engine._db is not None
    assert engine._trigger_registry is not None
    # Pre-flight: workflow registered? (cheap read; final state
    # check happens inside the atomic txn).
    pre_registered = await get_registered_workflow(
        engine._db, workflow_id=workflow_id,
    )
    if pre_registered is None:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="workflow_id",
                category=CAT_UNKNOWN_STEP_ID,
                message=f"workflow_id {workflow_id!r} is not registered",
            )],
        )
    # Validate predicate before any I/O.
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
    # Atomic boundary: re-read state inside the txn; INSERT trigger
    # only if state is STATE_REGISTERED. Loser of the race vs
    # concurrent activation sees the post-activation state and bails
    # out without inserting.
    trigger_id = f"trig_{uuid.uuid4().hex}"
    from kernos.kernel.workflows.trigger_registry import Trigger
    trigger = Trigger(
        trigger_id=trigger_id,
        workflow_id=workflow_id,
        instance_id=pre_registered.instance_id,
        event_type=event_type,
        predicate=predicate,
    )
    if not trigger.created_at:
        from datetime import datetime, timezone
        trigger.created_at = datetime.now(timezone.utc).isoformat()

    # Result container so the txn body can communicate back.
    _result: dict = {"inserted": False, "final_state": ""}

    async def _body(db: aiosqlite.Connection) -> None:
        # Re-read activation state inside the serialized transaction.
        async with db.execute(
            "SELECT activation_state FROM registered_workflows "
            "WHERE workflow_id = ? LIMIT 1",
            (workflow_id,),
        ) as cur:
            row = await cur.fetchone()
        current_state = row["activation_state"] if row is not None else ""
        _result["final_state"] = current_state
        if current_state != STATE_REGISTERED:
            return  # bail; not inserted
        # Insert trigger row directly via the engine's connection
        # (shared instance.db). The TriggerRegistry's own connection
        # cache is updated post-commit via _cache_insert below.
        await db.execute(
            "INSERT INTO triggers ("
            " trigger_id, workflow_id, instance_id, event_type, predicate,"
            " predicate_source, description, actor_filter, correlation_filter,"
            " idempotency_key_template, owner, version, status, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            trigger.to_row(),
        )
        _result["inserted"] = True

    try:
        await engine._run_workflow_txn(_body)
    except Exception as exc:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="trigger",
                category=CAT_PREDICATE_INVALID,
                message=f"trigger registration failed: {exc}",
            )],
        )
    if not _result["inserted"]:
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="workflow_id",
                category=CAT_INVALID_ACTIVATION_STATE,
                message=(
                    f"workflow {workflow_id} is in state "
                    f"{_result['final_state']}; operation "
                    f"register_trigger requires state(s) "
                    f"{[STATE_REGISTERED]}"
                ),
            )],
        )
    # Update TriggerRegistry's in-memory cache after commit. Lag is
    # acceptable: cache miss falls through to the next match cycle's
    # SQL read (the registry's match logic reads from the cache OR
    # falls back to DB).
    try:
        async with engine._trigger_registry._cache_lock:
            engine._trigger_registry._cache_insert(trigger)
    except Exception as exc:
        # Cache update failure is non-fatal; the trigger is durable.
        logger.warning(
            "TRIGGER_CACHE_UPDATE_FAILED trigger_id=%s error=%s",
            trigger_id, exc,
        )
    return AuthoringResult(
        success=True,
        workflow_id=workflow_id,
        trigger_id=trigger_id,
    )


@_emit_record_after("activate_workflow")
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
    # Idempotent on already-active (pre-CAS check; race-safe variant
    # below catches the case where another caller's CAS commits
    # between this read and our own attempt).
    if registered.activation_state == STATE_ACTIVE:
        return AuthoringResult(
            success=True,
            workflow_id=workflow_id,
            extra={"already_active": True},
        )
    # Re-run validation + governance classification (Q2: substrate
    # may have changed since registration; architect ratification IS
    # the safety boundary). Spec 5 post-impl Codex High 4: also
    # re-run classify_governance_tier and ensure the tier didn't
    # change to something Kernos isn't allowed to author.
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
    # Spec 5 14th amendment H1/M2 fold + 16th amendment HIGH 2 fold:
    # conditional re-validation of plural triggers from the canonical
    # descriptor blob.
    #
    # Semantics:
    #   * Empty canonical → legacy pre-13th-amendment row; skip
    #     re-validation (those rows predate the plural-triggers shape
    #     entirely; failing them would retroactively block activation).
    #   * Non-empty canonical that fails json.loads OR parses to a
    #     non-dict → fail-closed at the architect safety boundary.
    #     A corrupted canonical blob is not skippable; it's a substrate
    #     integrity violation that warrants architect attention.
    #     Activation_state stays unchanged.
    #   * Parsed dict with "triggers" key → route to
    #     compile_descriptor_triggers per v7.3 M2 (key-presence-only;
    #     falsey values like triggers=[] / triggers=null still route
    #     so the substrate primitive owns accept/reject).
    if registered.descriptor_json_canonical:
        try:
            stored_descriptor = json.loads(registered.descriptor_json_canonical)
        except (ValueError, TypeError) as exc:
            return AuthoringResult(
                success=False,
                errors=[ValidationError(
                    field_path="descriptor_json_canonical",
                    category=CAT_DESCRIPTOR_SHAPE_INVALID,
                    message=(
                        f"persisted canonical descriptor failed to parse "
                        f"as JSON: {exc}"
                    ),
                )],
            )
        if not isinstance(stored_descriptor, dict):
            return AuthoringResult(
                success=False,
                errors=[ValidationError(
                    field_path="descriptor_json_canonical",
                    category=CAT_DESCRIPTOR_SHAPE_INVALID,
                    message=(
                        f"persisted canonical descriptor must parse to a "
                        f"dict; got {type(stored_descriptor).__name__}"
                    ),
                )],
            )
        if "triggers" in stored_descriptor:
            from kernos.kernel.triggers import (
                PredicateValidationError,
                compile_descriptor_triggers,
            )
            try:
                compile_descriptor_triggers(
                    workflow_id=workflow_id, descriptor=stored_descriptor,
                )
            except PredicateValidationError as exc:
                return AuthoringResult(
                    success=False,
                    errors=[ValidationError(
                        field_path="descriptor.triggers",
                        category=CAT_PREDICATE_INVALID,
                        message=str(exc),
                    )],
                )
    # Spec 5 post-impl Codex High 4: re-run governance classifier.
    # If the classifier now sees substrate-modification surfaces that
    # weren't present at registration (substrate-tool-id list grew;
    # workflow's referenced tools became substrate), AND the original
    # registration was Kernos-issued composition, reject activation.
    # Architect can re-author with substrate_tier intent if needed.
    recomputed_tier = classify_governance_tier(wf)
    if (
        recomputed_tier == TIER_SUBSTRATE
        and registered.governance_tier == TIER_COMPOSITION
        and not registered.architect_authored
    ):
        return AuthoringResult(
            success=False,
            errors=[ValidationError(
                field_path="action_sequence",
                category=CAT_GOVERNANCE_TIER_VIOLATION,
                message=(
                    f"workflow {workflow_id}'s computed governance tier "
                    f"has drifted to substrate_tier since Kernos "
                    f"registered it as composition_tier (substrate-tool "
                    f"rules may have changed). Re-authoring by architect "
                    f"required."
                ),
            )],
        )
    # CAS transition (Q3). Spec 5 post-impl Codex High 6: if CAS
    # fails because another caller already moved the row to active,
    # treat as idempotent success (already_active=True).
    async def _body(db: aiosqlite.Connection):
        return await transition_to_active(db, workflow_id=workflow_id)
    updated, current_state = await engine._run_workflow_txn(_body)
    if not updated:
        if current_state == STATE_ACTIVE:
            return AuthoringResult(
                success=True,
                workflow_id=workflow_id,
                extra={"already_active": True},
            )
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


@_emit_record_after("deactivate_workflow")
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
        # Spec 5 post-impl Codex High 6: idempotent on race.
        if current_state == STATE_DEACTIVATED:
            return AuthoringResult(
                success=True,
                workflow_id=workflow_id,
                extra={"already_deactivated": True},
            )
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


# ---------------------------------------------------------------------------
# Tool-dispatch handlers (Spec 5 post-impl Codex Blocker 1)
# ---------------------------------------------------------------------------
#
# These handlers are the shape reasoning.py's tool-dispatch elif chain
# expects: take per-turn context (instance_id, member_id) + tool_input
# dict, return (summary: str, record: ActionStateRecord). They wrap the
# four authoring functions with AuthoringContext construction + the
# summary/record translation.
#
# NOTE on production wiring: the actual reasoning.py registration —
# adding "register_workflow" etc. to _KERNEL_TOOLS, the elif dispatch
# block, and the tool schemas — is Spec 6's production-wiring step 3
# per the architect's Spec 6 draft (which explicitly says "CC's
# production wiring for Spec 2 (bridge tools) and Spec 5 (authoring
# tools) was deferred to consuming spec. Spec 6 is the consumer.").
#
# These dispatch handlers exist NOW so Spec 6's wiring batch only
# needs to add the elif-block call sites + tool-name set + tool
# schemas; the authoring-side implementation is complete and tested.


KERNEL_AUTHORING_TOOL_NAMES: frozenset[str] = frozenset({
    "register_workflow", "register_trigger",
    "activate_workflow", "deactivate_workflow",
})


async def handle_register_workflow_tool(
    *,
    engine: "ExecutionEngine",
    instance_id: str,
    member_id: str,
    descriptor: dict,
    governance_tier: str,
) -> tuple[str, "ActionStateRecord"]:
    """Tool-dispatch shape for register_workflow.

    Constructs AuthoringContext from the per-turn member_id;
    derives actor_kind via the env-var-based discriminator;
    calls the authoring function; returns (summary, record).
    """
    ctx = AuthoringContext(
        actor_id=member_id, actor_kind=derive_actor_kind(member_id),
    )
    result = await register_workflow(engine, ctx, descriptor, governance_tier)
    summary = _summary_for_authoring_result(
        operation="register_workflow", result=result,
    )
    record = _build_authoring_action_state_record(
        operation="register_workflow", actor=ctx,
        workflow_id=result.workflow_id,
        execution_state="completed" if result.success else "failed",
        error=(
            "; ".join(e.message for e in result.errors[:3])
            if result.errors else ""
        ),
        errors=result.errors,
    )
    return summary, record


async def handle_register_trigger_tool(
    *,
    engine: "ExecutionEngine",
    instance_id: str,
    member_id: str,
    workflow_id: str,
    event_type: str,
    predicate: dict,
) -> tuple[str, "ActionStateRecord"]:
    """Tool-dispatch shape for register_trigger."""
    ctx = AuthoringContext(
        actor_id=member_id, actor_kind=derive_actor_kind(member_id),
    )
    result = await register_trigger(
        engine, ctx, workflow_id, event_type, predicate,
    )
    summary = _summary_for_authoring_result(
        operation="register_trigger", result=result,
    )
    record = _build_authoring_action_state_record(
        operation="register_trigger", actor=ctx,
        workflow_id=result.workflow_id,
        trigger_id=result.trigger_id,
        execution_state="completed" if result.success else "failed",
        error=(
            "; ".join(e.message for e in result.errors[:3])
            if result.errors else ""
        ),
        errors=result.errors,
    )
    return summary, record


async def handle_activate_workflow_tool(
    *,
    engine: "ExecutionEngine",
    instance_id: str,
    member_id: str,
    workflow_id: str,
) -> tuple[str, "ActionStateRecord"]:
    """Tool-dispatch shape for activate_workflow."""
    ctx = AuthoringContext(
        actor_id=member_id, actor_kind=derive_actor_kind(member_id),
    )
    result = await activate_workflow(engine, ctx, workflow_id)
    summary = _summary_for_authoring_result(
        operation="activate_workflow", result=result,
    )
    record = _build_authoring_action_state_record(
        operation="activate_workflow", actor=ctx,
        workflow_id=result.workflow_id,
        execution_state="completed" if result.success else "failed",
        error=(
            "; ".join(e.message for e in result.errors[:3])
            if result.errors else ""
        ),
        errors=result.errors,
    )
    return summary, record


async def handle_deactivate_workflow_tool(
    *,
    engine: "ExecutionEngine",
    instance_id: str,
    member_id: str,
    workflow_id: str,
    reason: str = "",
) -> tuple[str, "ActionStateRecord"]:
    """Tool-dispatch shape for deactivate_workflow."""
    ctx = AuthoringContext(
        actor_id=member_id, actor_kind=derive_actor_kind(member_id),
    )
    result = await deactivate_workflow(engine, ctx, workflow_id, reason=reason)
    summary = _summary_for_authoring_result(
        operation="deactivate_workflow", result=result,
    )
    record = _build_authoring_action_state_record(
        operation="deactivate_workflow", actor=ctx,
        workflow_id=result.workflow_id,
        execution_state="completed" if result.success else "failed",
        error=(
            "; ".join(e.message for e in result.errors[:3])
            if result.errors else ""
        ),
        errors=result.errors,
    )
    return summary, record


def _summary_for_authoring_result(
    *, operation: str, result: AuthoringResult,
) -> str:
    """Human-readable summary string for the tool surface."""
    if result.success:
        if result.extra.get("already_active"):
            return (
                f"{operation} succeeded: workflow {result.workflow_id} "
                f"already active"
            )
        if result.extra.get("already_deactivated"):
            return (
                f"{operation} succeeded: workflow {result.workflow_id} "
                f"already deactivated"
            )
        parts = [f"{operation} succeeded"]
        if result.workflow_id:
            parts.append(f"workflow_id={result.workflow_id}")
        if result.trigger_id:
            parts.append(f"trigger_id={result.trigger_id}")
        return ", ".join(parts)
    # Failure: surface the first few categories.
    err_descriptions = [
        f"{err.category}: {err.message}" for err in result.errors[:3]
    ]
    return f"{operation} failed: " + "; ".join(err_descriptions)


async def handle_friction_pattern_recurrence(
    db: aiosqlite.Connection,
    event_payload: dict,
    *,
    emit_event=None,
) -> bool:
    """Spec 5 Decision 8 / post-impl Codex Medium 8: subscriber for
    friction.pattern_recurrence events.

    Reads the pattern's ``workflow_resolvable`` flag from
    friction_pattern table; if 1, emits a soft-prompted reflection
    via event_stream so Kernos's awareness layer can surface it.

    The recurrence event payload key is ``resolved_pattern_id``
    (Spec 1's actual emit shape; the spec-body's reference to
    ``pattern_id`` was the spec-side name and is normalized here).

    Returns True iff a soft-prompt event was emitted (the pattern
    is tagged workflow_resolvable and the lookup succeeded).
    """
    from kernos.kernel import event_stream

    if emit_event is None:
        emit_event = event_stream.emit
    pattern_id = (
        event_payload.get("resolved_pattern_id")
        or event_payload.get("pattern_id")
        or ""
    )
    instance_id = event_payload.get("instance_id", "") or ""
    if not pattern_id or not instance_id:
        return False
    try:
        async with db.execute(
            "SELECT workflow_resolvable, description, signal_type_keys "
            "FROM friction_pattern WHERE instance_id = ? AND pattern_id = ? "
            "LIMIT 1",
            (instance_id, pattern_id),
        ) as cur:
            row = await cur.fetchone()
    except Exception as exc:
        logger.debug(
            "FRICTION_RECURRENCE_SUBSCRIBER_LOOKUP_FAILED "
            "pattern_id=%s error=%s", pattern_id, exc,
        )
        return False
    if row is None:
        return False
    try:
        is_workflow_resolvable = bool(row["workflow_resolvable"])
    except (KeyError, IndexError):
        # Pre-Spec-5 friction_pattern rows lack the column; default
        # to False (no soft prompt) until architect curates the tag.
        is_workflow_resolvable = False
    if not is_workflow_resolvable:
        return False
    # Emit the soft-prompted reflection. Kernos's awareness layer
    # consumes this event_type (full rendering wiring is Spec 6's
    # production batch).
    try:
        await emit_event(
            instance_id,
            "workflow_authoring.soft_prompt_workflow_resolvable",
            {
                "pattern_id": pattern_id,
                "description": row["description"] if row else "",
                "reflection": (
                    f"Pattern {pattern_id} recurred. Consider authoring a "
                    f"workflow to handle this autonomously. Reference the "
                    f"pattern_id in your descriptor's metadata."
                ),
            },
        )
        return True
    except Exception as exc:
        logger.debug(
            "FRICTION_RECURRENCE_SUBSCRIBER_EMIT_FAILED "
            "pattern_id=%s error=%s", pattern_id, exc,
        )
        return False


async def _emit_authoring_record(
    *,
    operation: str,
    actor: AuthoringContext,
    workflow_id: str,
    trigger_id: str = "",
    result: AuthoringResult,
    instance_id: str = "",
) -> None:
    """Spec 5 post-impl Codex High 5: emit an ActionStateRecord for
    each authoring operation via event_stream so the audit trail is
    durable.

    Event shape:
      event_type = 'workflow_authoring.action_recorded'
      payload   = the full ActionStateRecord serialized as dict
    """
    from kernos.kernel import event_stream
    record = _build_authoring_action_state_record(
        operation=operation,
        actor=actor,
        workflow_id=workflow_id,
        trigger_id=trigger_id,
        execution_state="completed" if result.success else "failed",
        error=(
            "; ".join(e.message for e in result.errors[:3])
            if result.errors else ""
        ),
        errors=result.errors,
    )
    payload = {
        "action_id": record.action_id,
        "surface": record.surface,
        "operation": record.operation,
        "operation_class": record.operation_class,
        "authorization_state": record.authorization_state,
        "execution_state": record.execution_state,
        "receipt_refs": list(record.receipt_refs),
        "affected_objects": list(record.affected_objects),
        "user_visible_summary": record.user_visible_summary,
        "risk_level": record.risk_level,
        "missing_metadata": record.missing_metadata,
        "actor_id": actor.actor_id,
        "actor_kind": actor.actor_kind,
    }
    try:
        await event_stream.emit(
            instance_id or "system",
            "workflow_authoring.action_recorded",
            payload,
            member_id=actor.actor_id or None,
        )
    except Exception as exc:
        logger.debug(
            "WORKFLOW_AUTHORING_RECORD_EMIT_FAILED operation=%s "
            "workflow_id=%s error=%s",
            operation, workflow_id, exc,
        )


__all__ = [
    "ACTOR_ARCHITECT",
    "ACTOR_KERNOS",
    "ACTOR_OPERATOR",
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
    "handle_activate_workflow_tool",
    "handle_deactivate_workflow_tool",
    "handle_friction_pattern_recurrence",
    "handle_register_trigger_tool",
    "handle_register_workflow_tool",
    "KERNEL_AUTHORING_TOOL_NAMES",
    "register_trigger",
    "register_workflow",
]
