"""Envelope validation — the structural enforcer of "EnactmentService
never changes decided_action" (PDI C5).

Per the design review edit on PDI: ActionEnvelope is structural, not prose-derived.
Validation runs at every plan-changing tier:

  - Initial plan creation (this commit, C5).
  - Tier-2 modify (C5; per the design review edit, not just pivot/reassemble).
  - Tier-3 pivot (C5).
  - Tier-4 reassemble (C6).

Validation checks each Step against the Briefing's ActionEnvelope:

  1. Step's `tool_class` ∈ envelope.allowed_tool_classes.
  2. Step's `operation_name` ∈ envelope.allowed_operations.
  3. Step does not match any pattern in envelope.forbidden_moves.
  4. Step does not bypass envelope.confirmation_requirements
     (a step whose operation_name is in confirmation_requirements
     must be the LAST step of its sub-chain in this turn — the
     downstream step that actually performs the require-confirmation
     operation cannot fire without explicit user confirmation, which
     crosses a turn boundary).

Returns a ValidationOutcome carrying `valid: bool` plus a redacted
`reason` for audit. Pure function — no I/O, no model calls — so the
tier handlers can call it cheaply at every plan-changing decision.

Universal across tools because the envelope itself is constructed by
integration per-action from briefing reasoning. The validation logic
is generic.
"""

from __future__ import annotations

from dataclasses import dataclass

from kernos.kernel.enactment.plan import Plan, Step
from kernos.kernel.integration.briefing import ActionEnvelope


# ---------------------------------------------------------------------------
# Outcome shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationOutcome:
    """Result of an envelope-validation call.

    `valid`: True when the step / plan satisfies every envelope
             constraint; False otherwise.
    `reason`: operator-readable label naming the failed check
              (`tool_class_not_allowed`, `operation_not_allowed`,
              `forbidden_move_matched`, `confirmation_bypass`,
              `step_invalid` for a generic catch-all). Empty when
              `valid is True`.
    `detail`: human-readable detail string for the audit trail.
              Redacted: never includes step argument values that
              might carry user content; only structural references.
    `step_id`: id of the failing step when applicable. Empty for
               whole-plan failures.
    """

    valid: bool
    reason: str = ""
    detail: str = ""
    step_id: str = ""


# Reasons (closed taxonomy for audit consumers).
REASON_TOOL_CLASS_NOT_ALLOWED = "tool_class_not_allowed"
REASON_OPERATION_NOT_ALLOWED = "operation_not_allowed"
REASON_FORBIDDEN_MOVE_MATCHED = "forbidden_move_matched"
REASON_CONFIRMATION_BYPASS = "confirmation_bypass"
REASON_PLAN_EMPTY = "plan_empty"


# ---------------------------------------------------------------------------
# Step-level validation
# ---------------------------------------------------------------------------


def validate_step_against_envelope(
    step: Step, envelope: ActionEnvelope
) -> ValidationOutcome:
    """Validate one Step against the action envelope.

    Pure function. Returns a ValidationOutcome with `valid` and
    `reason` set. Does NOT raise — the caller routes to B1 termination
    when valid is False.

    Order of checks: tool_class → operation_name → forbidden_moves.
    confirmation_requirements is enforced at the plan level (see
    validate_plan_against_envelope) because confirmation routing
    needs to know whether the require-confirmation operation is the
    last step of its sub-chain.
    """
    # 1. Tool class.
    if envelope.allowed_tool_classes:
        if step.tool_class not in envelope.allowed_tool_classes:
            return ValidationOutcome(
                valid=False,
                reason=REASON_TOOL_CLASS_NOT_ALLOWED,
                detail=(
                    f"step {step.step_id} tool_class "
                    f"{step.tool_class!r} not in allowed_tool_classes"
                ),
                step_id=step.step_id,
            )
    elif step.tool_class:
        # Empty allowed_tool_classes on the envelope means "no tool
        # classes permitted." If the step declares one anyway, that
        # is a violation.
        return ValidationOutcome(
            valid=False,
            reason=REASON_TOOL_CLASS_NOT_ALLOWED,
            detail=(
                f"step {step.step_id} declared tool_class "
                f"{step.tool_class!r} but envelope permits none"
            ),
            step_id=step.step_id,
        )

    # 2. Operation name.
    if envelope.allowed_operations:
        if step.operation_name not in envelope.allowed_operations:
            return ValidationOutcome(
                valid=False,
                reason=REASON_OPERATION_NOT_ALLOWED,
                detail=(
                    f"step {step.step_id} operation_name "
                    f"{step.operation_name!r} not in allowed_operations"
                ),
                step_id=step.step_id,
            )

    # 3. Forbidden moves. The envelope's forbidden_moves entries are
    # free-form pattern labels (e.g. "channel_switch",
    # "operation_escalation"). The step encodes its own move semantics
    # via tool_class + operation_name; matching against the envelope's
    # forbidden_move labels is structural, not prose-fuzzy. v1
    # interprets a forbidden_move match as "the step's
    # operation_name OR tool_class equals the forbidden label" — this
    # gives integration a way to forbid a specific operation without
    # having to enumerate every other allowed one.
    for forbidden in envelope.forbidden_moves:
        if forbidden == step.operation_name or forbidden == step.tool_class:
            return ValidationOutcome(
                valid=False,
                reason=REASON_FORBIDDEN_MOVE_MATCHED,
                detail=(
                    f"step {step.step_id} matches forbidden_move "
                    f"{forbidden!r}"
                ),
                step_id=step.step_id,
            )

    return ValidationOutcome(valid=True)


# ---------------------------------------------------------------------------
# Plan-level validation
# ---------------------------------------------------------------------------


def validate_plan_against_envelope(
    plan: Plan, envelope: ActionEnvelope
) -> ValidationOutcome:
    """Validate every step in the plan + the confirmation-boundary rule.

    Pure function. Walks each step through validate_step_against_envelope
    first; on first failure returns immediately. Then enforces:

      Confirmation-boundary rule: any step whose operation_name is in
      envelope.confirmation_requirements is allowed only when the user
      has explicitly confirmed in this turn. Since enactment cannot
      cross a turn boundary mid-plan, a require-confirmation step in
      the plan is a structural violation UNLESS it is the explicit
      authorisation envelope (i.e. integration on this turn already
      saw the user's confirmation alongside the request, and produced
      an envelope that lists the operation in allowed_operations
      without listing it in confirmation_requirements). Concretely:
      if a step's operation_name is in envelope.confirmation_requirements,
      validation rejects the plan with REASON_CONFIRMATION_BYPASS.

      Rationale: confirmation_requirements means "this operation needs
      explicit user confirmation," and the canonical Kernos pattern
      is to cross a turn boundary for that confirmation. A plan
      attempting to chain through a confirmation step in the same
      turn is a structural violation; the previous turn's draft must
      surface to the user, and the next turn's integration produces
      a fresh envelope with the send permitted (confirmation_requirements
      empty for the send step).
    """
    if not plan.steps:
        return ValidationOutcome(
            valid=False,
            reason=REASON_PLAN_EMPTY,
            detail="plan has no steps",
        )

    for step in plan.steps:
        step_outcome = validate_step_against_envelope(step, envelope)
        if not step_outcome.valid:
            return step_outcome

    # Confirmation boundary.
    for step in plan.steps:
        if step.operation_name in envelope.confirmation_requirements:
            return ValidationOutcome(
                valid=False,
                reason=REASON_CONFIRMATION_BYPASS,
                detail=(
                    f"step {step.step_id} operation "
                    f"{step.operation_name!r} requires user confirmation; "
                    f"plan attempts to dispatch within the same turn. "
                    f"Confirmation crosses a turn boundary."
                ),
                step_id=step.step_id,
            )

    return ValidationOutcome(valid=True)


__all__ = [
    "REASON_CONFIRMATION_BYPASS",
    "REASON_FORBIDDEN_MOVE_MATCHED",
    "REASON_OPERATION_NOT_ALLOWED",
    "REASON_PLAN_EMPTY",
    "REASON_TOOL_CLASS_NOT_ALLOWED",
    "ValidationOutcome",
    "validate_plan_against_envelope",
    "validate_step_against_envelope",
]
