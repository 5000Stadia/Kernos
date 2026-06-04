"""IMPROVEMENT-REVIEW-PROTOCOL-V1 (2026-05-22) acceptance tests.

Pins the GREEN / NEEDS_REVISION parser + prompt-template
composer + iteration state machine.
"""
from __future__ import annotations

import pytest

from kernos.kernel.improvement_review_protocol import (
    ReviewIterationState,
    detect_status,
    render_prompt,
    step_iteration,
)


# ============================================================
# ACs 1-5: detect_status
# ============================================================


class TestDetectStatus:
    def test_ac1_green_returns_green_empty(self):
        text = "spec content here\n\nSTATUS: GREEN"
        status, findings = detect_status(text)
        assert status == "GREEN"
        assert findings == ""

    def test_ac2_needs_revision_with_findings(self):
        text = (
            "spec content\n\nSTATUS: NEEDS_REVISION ACs are too vague"
        )
        status, findings = detect_status(text)
        assert status == "NEEDS_REVISION"
        assert "ACs are too vague" in findings

    def test_ac3_needs_revision_no_body(self):
        text = "spec\n\nSTATUS: NEEDS_REVISION"
        status, findings = detect_status(text)
        assert status == "NEEDS_REVISION"
        assert findings == ""

    def test_ac4_unknown_when_no_marker(self):
        text = "spec content without a marker"
        status, findings = detect_status(text)
        assert status == "UNKNOWN"
        assert findings == ""

    def test_ac5_picks_last_occurrence(self):
        # Body mentions STATUS in prose; actual marker at bottom
        text = (
            "When you finish, write STATUS: GREEN or "
            "STATUS: NEEDS_REVISION at the bottom.\n\n"
            "spec body\n\n"
            "STATUS: GREEN"
        )
        status, _ = detect_status(text)
        assert status == "GREEN"

    def test_empty_text_returns_unknown(self):
        assert detect_status("") == ("UNKNOWN", "")
        assert detect_status(None) == ("UNKNOWN", "")  # type: ignore[arg-type]


# ============================================================
# ACs 6-10: render_prompt
# ============================================================


class TestRenderPrompt:
    def test_ac6_spec_author_includes_author_framing(self):
        text = render_prompt(
            "spec_author",
            spec_requirement="add a comment to README",
        )
        assert text
        assert "SPEC AUTHOR" in text
        assert "STATUS: GREEN" in text
        assert "STATUS: NEEDS_REVISION" in text
        assert "add a comment to README" in text

    def test_ac7_spec_reviewer_has_review_framing(self):
        text = render_prompt("spec_reviewer", spec_text="some spec")
        assert "SPEC REVIEWER" in text
        assert "some spec" in text

    def test_ac8_impl_author_has_impl_framing(self):
        text = render_prompt(
            "impl_author",
            workspace_dir="/tmp/wt",
            spec_text="spec",
        )
        assert "IMPLEMENTATION AUTHOR" in text
        assert "/tmp/wt" in text
        assert "impl_notes.md" in text

    def test_ac9_impl_reviewer_has_code_review_framing(self):
        text = render_prompt(
            "impl_reviewer",
            workspace_dir="/tmp/wt",
            spec_text="spec",
        )
        assert "IMPLEMENTATION REVIEWER" in text
        assert "git_diff_for_review" in text

    def test_ac10_prior_findings_included_on_iteration_gt_1(self):
        text = render_prompt(
            "spec_author",
            spec_requirement="x",
            iteration=2,
            prior_findings="ACs not measurable",
        )
        assert "ACs not measurable" in text
        assert "Iteration: 2" in text

    def test_no_prior_findings_on_iteration_1(self):
        text = render_prompt(
            "spec_author",
            spec_requirement="x",
            iteration=1,
            prior_findings="should_not_appear",
        )
        # On iteration 1, prior findings are skipped
        assert "should_not_appear" not in text

    def test_unknown_role_raises(self):
        with pytest.raises(ValueError):
            render_prompt("bogus_role")  # type: ignore[arg-type]


# ============================================================
# ACs 11-14: step_iteration state machine
# ============================================================


class TestStepIteration:
    def test_ac11_increments_iteration_appends_histories(self):
        state = ReviewIterationState(
            role_pair="spec", max_iterations=5,
        )
        step_iteration(
            state,
            author_status="NEEDS_REVISION",
            reviewer_status="NEEDS_REVISION",
            author_findings="too vague",
        )
        assert state.iteration == 1
        assert state.author_history == ["NEEDS_REVISION"]
        assert state.reviewer_history == ["NEEDS_REVISION"]
        assert state.findings_history == ["too vague"]

    def test_ac12_converges_on_both_green(self):
        state = ReviewIterationState(
            role_pair="spec", max_iterations=5,
        )
        step_iteration(
            state,
            author_status="GREEN", reviewer_status="GREEN",
        )
        assert state.outcome == "GREEN"
        assert state.finished is True

    def test_ac13_aborts_at_max_iterations(self):
        state = ReviewIterationState(
            role_pair="impl", max_iterations=3,
        )
        for _ in range(3):
            step_iteration(
                state,
                author_status="NEEDS_REVISION",
                reviewer_status="NEEDS_REVISION",
            )
        assert state.iteration == 3
        assert state.outcome == "ABORTED_UNCONVERGED"
        assert state.finished is True

    def test_ac14_pending_when_not_converged_not_capped(self):
        state = ReviewIterationState(
            role_pair="spec", max_iterations=5,
        )
        step_iteration(
            state,
            author_status="GREEN", reviewer_status="NEEDS_REVISION",
        )
        assert state.outcome == "PENDING"
        assert state.finished is False


# ============================================================
# AC15: env-configurable max iterations
# ============================================================


class TestEnvConfig:
    def test_ac15_spec_max_from_env(self, monkeypatch):
        monkeypatch.setenv("KERNOS_IMPROVEMENT_SPEC_ITERATION_MAX", "7")
        state = ReviewIterationState.for_spec()
        assert state.max_iterations == 7

    def test_ac15_impl_max_from_env(self, monkeypatch):
        monkeypatch.setenv("KERNOS_IMPROVEMENT_IMPL_ITERATION_MAX", "2")
        state = ReviewIterationState.for_impl()
        assert state.max_iterations == 2

    def test_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv(
            "KERNOS_IMPROVEMENT_SPEC_ITERATION_MAX", raising=False,
        )
        monkeypatch.delenv(
            "KERNOS_IMPROVEMENT_IMPL_ITERATION_MAX", raising=False,
        )
        assert ReviewIterationState.for_spec().max_iterations == 8
        assert ReviewIterationState.for_impl().max_iterations == 6


# ============================================================
# Integration: full converged spec cycle
# ============================================================


def test_full_converged_spec_cycle():
    state = ReviewIterationState.for_spec()
    # Round 1: author says NEEDS_REVISION (still drafting)
    step_iteration(
        state,
        author_status="NEEDS_REVISION", reviewer_status="NEEDS_REVISION",
        author_findings="ACs incomplete",
    )
    assert state.outcome == "PENDING"
    # Round 2: both GREEN
    step_iteration(
        state,
        author_status="GREEN", reviewer_status="GREEN",
    )
    assert state.outcome == "GREEN"
    assert state.iteration == 2
    assert state.finished is True


def test_full_capped_spec_cycle():
    state = ReviewIterationState(role_pair="spec", max_iterations=2)
    step_iteration(
        state,
        author_status="NEEDS_REVISION", reviewer_status="NEEDS_REVISION",
    )
    step_iteration(
        state,
        author_status="GREEN", reviewer_status="NEEDS_REVISION",
    )
    assert state.outcome == "ABORTED_UNCONVERGED"
    assert state.iteration == 2


def test_proportionality_clause_in_all_roles():
    """Every role must carry the proportionality guidance so a trivial change
    doesn't get gold-plated through many rounds (regression for att_02243d:
    6 spec rounds + ~hundreds of impl ops for a 3-sentence doc)."""
    for role in ("spec_author", "spec_reviewer", "impl_author", "impl_reviewer"):
        text = render_prompt(role, spec_requirement="x", spec_text="y",
                             workspace_dir="/tmp/wt").lower()
        assert "proportion" in text or "right-size" in text, role


def test_reviewers_told_to_green_trivial_first_pass():
    spec_rev = render_prompt("spec_reviewer", spec_text="y").lower()
    impl_rev = render_prompt("impl_reviewer", spec_text="y",
                             workspace_dir="/tmp/wt").lower()
    assert "green" in spec_rev and "trivial" in spec_rev
    assert "trivial" in impl_rev
