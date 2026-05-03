"""Tests for tier classification — pure-function routing (PDI C5).

Per design review's C5 guidance: tier conditions are distinct, not a
fuzzy ladder. The most common implementation error is conflating
them. classify_routing is the explicit testable pure function; tests
pin every transition.
"""

from __future__ import annotations

import pytest

from kernos.kernel.enactment.tiers import (
    DEFAULT_MODIFY_BUDGET,
    DEFAULT_PIVOT_BUDGET,
    DEFAULT_RETRY_BUDGET,
    FailureKind,
    ThreeQuestionResult,
    TierBudgets,
    TierRouting,
    all_three_pass,
    classify_routing,
)


def _check(
    *,
    completed: bool = True,
    effect_matches: bool | None = True,
    plan_still_valid: bool | None = True,
) -> ThreeQuestionResult:
    return ThreeQuestionResult(
        step_completed=completed,
        effect_matches_expectation=effect_matches,
        plan_still_valid=plan_still_valid,
    )


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_default_budgets_match_documented_values():
    b = TierBudgets()
    assert b.retry_remaining == DEFAULT_RETRY_BUDGET == 3
    assert b.modify_remaining == DEFAULT_MODIFY_BUDGET == 2
    assert b.pivot_remaining == DEFAULT_PIVOT_BUDGET == 2


# ---------------------------------------------------------------------------
# all_three_pass
# ---------------------------------------------------------------------------


def test_all_three_pass_true_only_for_definite_true():
    assert all_three_pass(_check()) is True


def test_all_three_pass_false_when_any_is_false():
    assert all_three_pass(_check(completed=False)) is False
    assert all_three_pass(_check(effect_matches=False)) is False
    assert all_three_pass(_check(plan_still_valid=False)) is False


def test_all_three_pass_false_when_any_is_none():
    assert all_three_pass(_check(effect_matches=None)) is False
    assert all_three_pass(_check(plan_still_valid=None)) is False


# ---------------------------------------------------------------------------
# Routing — happy path (PROCEED)
# ---------------------------------------------------------------------------


def test_proceed_when_all_three_pass_and_no_failure():
    routing = classify_routing(
        check=_check(),
        failure_kind=FailureKind.NONE,
        budgets=TierBudgets(),
    )
    assert routing is TierRouting.PROCEED


# ---------------------------------------------------------------------------
# Tier 1 retry
# ---------------------------------------------------------------------------


def test_tier_1_retry_for_transient_failure_with_retry_budget():
    routing = classify_routing(
        check=_check(completed=False, effect_matches=False),
        failure_kind=FailureKind.TRANSIENT,
        budgets=TierBudgets(),
    )
    assert routing is TierRouting.TIER_1_RETRY


def test_transient_with_exhausted_retry_falls_to_modify():
    routing = classify_routing(
        check=_check(completed=False, effect_matches=False),
        failure_kind=FailureKind.TRANSIENT,
        budgets=TierBudgets(retry_remaining=0),
    )
    assert routing is TierRouting.TIER_2_MODIFY


def test_transient_with_all_budgets_exhausted_surfaces_b1():
    routing = classify_routing(
        check=_check(completed=False, effect_matches=False),
        failure_kind=FailureKind.TRANSIENT,
        budgets=TierBudgets(retry_remaining=0, modify_remaining=0),
    )
    assert routing is TierRouting.TIER_5_SURFACE_B1


# ---------------------------------------------------------------------------
# Tier 2 modify
# ---------------------------------------------------------------------------


def test_tier_2_modify_for_corrective_signal_with_modify_budget():
    routing = classify_routing(
        check=_check(completed=False, effect_matches=False),
        failure_kind=FailureKind.CORRECTIVE_SIGNAL,
        budgets=TierBudgets(),
    )
    assert routing is TierRouting.TIER_2_MODIFY


def test_corrective_signal_with_no_modify_budget_surfaces_b1():
    routing = classify_routing(
        check=_check(completed=False, effect_matches=False),
        failure_kind=FailureKind.CORRECTIVE_SIGNAL,
        budgets=TierBudgets(modify_remaining=0),
    )
    assert routing is TierRouting.TIER_5_SURFACE_B1


def test_non_transient_failure_routes_to_modify_directly():
    """Non-transient failures skip tier 1 retry — retry will not fix
    them."""
    routing = classify_routing(
        check=_check(completed=False, effect_matches=False),
        failure_kind=FailureKind.NON_TRANSIENT,
        budgets=TierBudgets(retry_remaining=99, modify_remaining=2),
    )
    assert routing is TierRouting.TIER_2_MODIFY


def test_non_transient_with_no_modify_budget_surfaces_b1():
    routing = classify_routing(
        check=_check(completed=False, effect_matches=False),
        failure_kind=FailureKind.NON_TRANSIENT,
        budgets=TierBudgets(modify_remaining=0),
    )
    assert routing is TierRouting.TIER_5_SURFACE_B1


# ---------------------------------------------------------------------------
# Tier 3 pivot
# ---------------------------------------------------------------------------


def test_tier_3_pivot_for_information_divergence():
    """Step succeeded but result re-shapes plan."""
    routing = classify_routing(
        check=_check(plan_still_valid=False),  # plan invalidated by new info
        failure_kind=FailureKind.INFORMATION_DIVERGENCE,
        budgets=TierBudgets(),
    )
    assert routing is TierRouting.TIER_3_PIVOT


def test_information_divergence_with_no_pivot_budget_routes_to_reassemble():
    """Pivot exhausted on info divergence → reassemble (the plan is
    invalidated by new information; decided_action may still be valid
    with a different plan shape)."""
    routing = classify_routing(
        check=_check(plan_still_valid=False),
        failure_kind=FailureKind.INFORMATION_DIVERGENCE,
        budgets=TierBudgets(pivot_remaining=0),
    )
    assert routing is TierRouting.TIER_4_REASSEMBLE


def test_clean_completion_with_failed_structured_signals_routes_to_pivot():
    """FailureKind.NONE plus not-all-three-pass means structured
    check found a divergence on a step that completed cleanly. Treat
    as info divergence — pivot first, then reassemble."""
    routing = classify_routing(
        check=_check(effect_matches=False),
        failure_kind=FailureKind.NONE,
        budgets=TierBudgets(),
    )
    assert routing is TierRouting.TIER_3_PIVOT


def test_clean_completion_no_pivot_budget_falls_to_reassemble():
    routing = classify_routing(
        check=_check(effect_matches=False),
        failure_kind=FailureKind.NONE,
        budgets=TierBudgets(pivot_remaining=0),
    )
    assert routing is TierRouting.TIER_4_REASSEMBLE


# ---------------------------------------------------------------------------
# Special-case routing: ambiguity + envelope violation
# ---------------------------------------------------------------------------


def test_ambiguity_needs_user_routes_to_b2_regardless_of_budgets():
    routing = classify_routing(
        check=_check(completed=False),
        failure_kind=FailureKind.AMBIGUITY_NEEDS_USER,
        budgets=TierBudgets(retry_remaining=99, modify_remaining=99, pivot_remaining=99),
    )
    assert routing is TierRouting.TIER_5_SURFACE_B2


def test_envelope_violation_routes_to_b1_directly():
    """Envelope violation cannot be remediated by tier 2/3 (they
    re-validate against the same envelope). Direct B1."""
    routing = classify_routing(
        check=_check(completed=False),
        failure_kind=FailureKind.ENVELOPE_VIOLATION,
        budgets=TierBudgets(),  # full budgets, doesn't matter
    )
    assert routing is TierRouting.TIER_5_SURFACE_B1


# ---------------------------------------------------------------------------
# Budget consumption helpers
# ---------------------------------------------------------------------------


def test_with_retry_consumed_decrements_only_retry():
    b = TierBudgets(retry_remaining=3, modify_remaining=2, pivot_remaining=2)
    consumed = b.with_retry_consumed()
    assert consumed.retry_remaining == 2
    assert consumed.modify_remaining == 2
    assert consumed.pivot_remaining == 2


def test_with_modify_consumed_decrements_only_modify():
    b = TierBudgets()
    consumed = b.with_modify_consumed()
    assert consumed.modify_remaining == DEFAULT_MODIFY_BUDGET - 1
    assert consumed.retry_remaining == DEFAULT_RETRY_BUDGET
    assert consumed.pivot_remaining == DEFAULT_PIVOT_BUDGET


def test_with_pivot_consumed_decrements_only_pivot():
    b = TierBudgets()
    consumed = b.with_pivot_consumed()
    assert consumed.pivot_remaining == DEFAULT_PIVOT_BUDGET - 1


def test_consume_floors_at_zero():
    """Defensive: consuming a budget already at 0 stays at 0 rather
    than going negative."""
    b = TierBudgets(retry_remaining=0)
    assert b.with_retry_consumed().retry_remaining == 0
