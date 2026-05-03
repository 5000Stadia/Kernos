"""Tier classification + budgets — explicit pure-function routing (PDI C5).

Per design review's C5 guidance: tier conditions are distinct, not a
fuzzy ladder. The most common implementation error is conflating
them — retry on a non-transient failure, or modify when retry would
fix it. The routing logic lives here as a pure function so it is
explicitly testable and never delegated to model judgment.

Tier conditions (locked in C5):

  Tier 1 (Retry):
    - Failure plausibly transient (connection error, rate-limit,
      5xx-ish provider state).
    - Step is idempotent (action-tool authors mark this; default
      assume idempotent for retry budget pursuit; non-idempotent
      tools opt out by exhausting the retry budget at 0).
    - Per-step retry budget remains (default 3, configurable per
      tool descriptor).
    - Effect: runtime re-dispatches with same args.

  Tier 2 (Modify):
    - Failure surfaced a CORRECTIVE signal (the tool returned guidance
      to retry differently — e.g. "rate-limit, batch size too large"
      or a stub-reload schema-mismatch synthetic result).
    - Step's intent unchanged; mechanism changes.
    - Per-step modify budget remains (default 2).
    - Envelope-validated (the design review edit — "same intent" is model assertion,
      not runtime guarantee).
    - Effect: model emits modified Step; runtime dispatches the new
      step.

  Tier 3 (Pivot):
    - Step SUCCEEDED but the result re-shapes the plan
      (information-driven divergence).
    - Original step's intent unachievable but a different intermediate
      goal would still serve the decided_action; rest of plan still
      makes sense.
    - Per-step pivot budget remains (default 2).
    - Envelope-validated.
    - Effect: model produces a different intermediate goal; runtime
      dispatches.

  Tier 4 (Reassemble): plan invalidated, decided_action still valid.
    Lands in C6.

  Tier 5 (Surface):
    - B1 (action invalidated): budgets exhausted, envelope violation,
      covenant block, etc. → terminate with reintegration context.
    - B2 (user disambiguation needed): mid-action ambiguity; structured
      ClarificationNeeded constructed; reintegration stored for next
      turn (NO same-turn integration re-entry per the design review edit).
    Lands in C6.

The classify_routing pure function is the single source of truth for
which tier fires given the inputs. Tier handlers consume its output;
they never re-classify.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FailureKind(str, Enum):
    """Closed taxonomy of step-failure categories.

    The runtime classifies a dispatch result into exactly one of
    these. The classification is the input to classify_routing.
    """

    NONE = "none"
    """No failure — step completed and effect matched expectation."""

    TRANSIENT = "transient"
    """Connection error, rate-limit, 5xx-ish. Retry-eligible."""

    NON_TRANSIENT = "non_transient"
    """4xx auth errors, invalid args, etc. Retry will not help."""

    CORRECTIVE_SIGNAL = "corrective_signal"
    """Tool returned guidance to retry differently. Modify-eligible."""

    INFORMATION_DIVERGENCE = "information_divergence"
    """Step succeeded but result re-shapes the plan. Pivot-eligible."""

    AMBIGUITY_NEEDS_USER = "ambiguity_needs_user"
    """Mid-action info surfaces a question Kernos cannot answer.
    B2 case. C6 wires the surface."""

    ENVELOPE_VIOLATION = "envelope_violation"
    """Tier-2/3 model emitted a step that fails envelope validation.
    Cannot be modified or pivoted further; B1 surface only."""


class TierRouting(str, Enum):
    """The routing decision returned by classify_routing.

    Stable across C5-C6. C5 wires PROCEED, TIER_1_RETRY, TIER_2_MODIFY,
    TIER_3_PIVOT. TIER_4_REASSEMBLE / TIER_5_SURFACE_B1 / TIER_5_SURFACE_B2
    land in C6; the enum values exist now so the tier-handler
    dispatch can grow without adding new enum values later.
    """

    PROCEED = "proceed"
    """Three-question check passed — advance to next step."""

    TIER_1_RETRY = "retry"
    TIER_2_MODIFY = "modify"
    TIER_3_PIVOT = "pivot"
    TIER_4_REASSEMBLE = "reassemble"
    TIER_5_SURFACE_B1 = "surface_b1"
    TIER_5_SURFACE_B2 = "surface_b2"


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


# Default per-step budgets. Per-tool overrides land via descriptor
# fields in C5+ (the spec mentions "configurable per tool descriptor").
DEFAULT_RETRY_BUDGET = 3
DEFAULT_MODIFY_BUDGET = 2
DEFAULT_PIVOT_BUDGET = 2


@dataclass(frozen=True)
class TierBudgets:
    """Per-step remaining budgets. Pure data; immutable.

    The runtime decrements by constructing a new TierBudgets with
    decremented fields after each tier handler fires. Frozen so a
    handler cannot accidentally mutate the budget shared with the
    caller.
    """

    retry_remaining: int = DEFAULT_RETRY_BUDGET
    modify_remaining: int = DEFAULT_MODIFY_BUDGET
    pivot_remaining: int = DEFAULT_PIVOT_BUDGET

    def with_retry_consumed(self) -> "TierBudgets":
        return TierBudgets(
            retry_remaining=max(0, self.retry_remaining - 1),
            modify_remaining=self.modify_remaining,
            pivot_remaining=self.pivot_remaining,
        )

    def with_modify_consumed(self) -> "TierBudgets":
        return TierBudgets(
            retry_remaining=self.retry_remaining,
            modify_remaining=max(0, self.modify_remaining - 1),
            pivot_remaining=self.pivot_remaining,
        )

    def with_pivot_consumed(self) -> "TierBudgets":
        return TierBudgets(
            retry_remaining=self.retry_remaining,
            modify_remaining=self.modify_remaining,
            pivot_remaining=max(0, self.pivot_remaining - 1),
        )


# ---------------------------------------------------------------------------
# Routing — pure function
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThreeQuestionResult:
    """Output of the three-question check.

    `step_completed`: did the dispatch return without error?
    `effect_matches_expectation`: did structured signals pass
        AND/OR did the prose-judged comparison match? None means
        "could not be determined" (rare; treated as diverged).
    `plan_still_valid`: model judgment with new state in context.
        None means "not yet evaluated" (pre-divergence-routing).

    All three pass → proceed. Any fail or None → divergence routing.
    """

    step_completed: bool
    effect_matches_expectation: bool | None
    plan_still_valid: bool | None


def all_three_pass(check: ThreeQuestionResult) -> bool:
    """True only when every question is a definite True."""
    return (
        check.step_completed
        and check.effect_matches_expectation is True
        and check.plan_still_valid is True
    )


def classify_routing(
    *,
    check: ThreeQuestionResult,
    failure_kind: FailureKind,
    budgets: TierBudgets,
) -> TierRouting:
    """Pure function: route a step outcome to its tier.

    The single source of truth for tier classification. Tests pin
    every transition. Tier handlers consume the output; they never
    re-classify.

    Routing rules (design review-locked):

      1. PROCEED only when all three questions pass.
      2. TIER_1_RETRY only for transient failures with retry budget.
      3. TIER_2_MODIFY only for corrective signals with modify
         budget — OR — transient failure with retry exhausted but
         modify available (modify is the "same intent, different
         mechanism" fallback for retry-exhaustion). NEVER for
         non-transient failures (those skip directly to modify).
      4. TIER_3_PIVOT only for INFORMATION_DIVERGENCE (step
         SUCCEEDED but result re-shapes plan) with pivot budget.
      5. TIER_5_SURFACE_B2 for AMBIGUITY_NEEDS_USER.
      6. TIER_5_SURFACE_B1 for ENVELOPE_VIOLATION.
      7. Budget exhaustion within a non-pivot failure → TIER_4_REASSEMBLE
         when sensible, else TIER_5_SURFACE_B1.

    Note: TIER_4_REASSEMBLE and TIER_5_SURFACE_B1/B2 land in C6;
    classify_routing returns the enum values now so the routing
    table is complete from C5.
    """
    # Rule 1: all three pass → proceed.
    if all_three_pass(check):
        return TierRouting.PROCEED

    # Special-case rule 5: ambiguity that needs user input always
    # routes to B2, regardless of budgets.
    if failure_kind is FailureKind.AMBIGUITY_NEEDS_USER:
        return TierRouting.TIER_5_SURFACE_B2

    # Special-case rule 6: envelope violation cannot be remediated
    # by tier 2 / tier 3 because those tiers re-validate against the
    # same envelope. Only B1 surface is appropriate.
    if failure_kind is FailureKind.ENVELOPE_VIOLATION:
        return TierRouting.TIER_5_SURFACE_B1

    # Rule 4: information_divergence → pivot (step SUCCEEDED).
    if failure_kind is FailureKind.INFORMATION_DIVERGENCE:
        if budgets.pivot_remaining > 0:
            return TierRouting.TIER_3_PIVOT
        # Pivot budget exhausted on info divergence → reassemble
        # rather than B1: the plan is invalidated by new information,
        # but the decided_action may still be valid with a different
        # plan shape.
        return TierRouting.TIER_4_REASSEMBLE

    # Rule 2: transient failure → retry first, then modify, then B1.
    if failure_kind is FailureKind.TRANSIENT:
        if budgets.retry_remaining > 0:
            return TierRouting.TIER_1_RETRY
        # Retry exhausted; transient failure may still be tractable
        # via a modified mechanism (e.g. smaller batch, different
        # endpoint).
        if budgets.modify_remaining > 0:
            return TierRouting.TIER_2_MODIFY
        return TierRouting.TIER_5_SURFACE_B1

    # Rule 3: non_transient or corrective_signal → modify directly
    # (skip retry — the failure is not retry-tractable).
    if failure_kind in (
        FailureKind.NON_TRANSIENT,
        FailureKind.CORRECTIVE_SIGNAL,
    ):
        if budgets.modify_remaining > 0:
            return TierRouting.TIER_2_MODIFY
        return TierRouting.TIER_5_SURFACE_B1

    # FailureKind.NONE with not-all-three-pass means the structured
    # check found a divergence on a step that "completed" cleanly —
    # treated as information divergence (the most common cause).
    if failure_kind is FailureKind.NONE:
        if budgets.pivot_remaining > 0:
            return TierRouting.TIER_3_PIVOT
        return TierRouting.TIER_4_REASSEMBLE

    # Defensive default — never reach here under the closed enum.
    return TierRouting.TIER_5_SURFACE_B1


__all__ = [
    "DEFAULT_MODIFY_BUDGET",
    "DEFAULT_PIVOT_BUDGET",
    "DEFAULT_RETRY_BUDGET",
    "FailureKind",
    "ThreeQuestionResult",
    "TierBudgets",
    "TierRouting",
    "all_three_pass",
    "classify_routing",
]
