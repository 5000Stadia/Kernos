"""Review protocol for the autonomous-improvement loop.

IMPROVEMENT-REVIEW-PROTOCOL-V1 (2026-05-22).

Four roles (spec_author, spec_reviewer, impl_author,
impl_reviewer), each with a system-prompt template. The
orchestrator workflow (future IMPROVEMENT-LOOP-WORKFLOW-V1)
calls ``consult`` with the rendered prompts; this module
provides the templates + the GREEN / NEEDS_REVISION
convergence detection + iteration counter state machine.

Substrate-only: no agent surface, no slash command. The
agent never calls these helpers directly.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Defaults (env-configurable)
# ---------------------------------------------------------------------


def _spec_iteration_max() -> int:
    return int(
        os.environ.get(
            "KERNOS_IMPROVEMENT_SPEC_ITERATION_MAX", "5",
        )
    )


def _impl_iteration_max() -> int:
    return int(
        os.environ.get(
            "KERNOS_IMPROVEMENT_IMPL_ITERATION_MAX", "3",
        )
    )


# ---------------------------------------------------------------------
# Convergence detection
# ---------------------------------------------------------------------


_STATUS_RE = re.compile(
    r"STATUS:\s*(GREEN|NEEDS_REVISION)\b(.*?)(?=$|\n)",
    re.MULTILINE,
)


def detect_status(text: str) -> tuple[
    Literal["GREEN", "NEEDS_REVISION", "UNKNOWN"], str,
]:
    """Parse the STATUS marker from author/reviewer output.

    Looks for the LAST occurrence of ``STATUS: GREEN`` or
    ``STATUS: NEEDS_REVISION [<findings>]``. Returns
    ``(status, findings)``. ``findings`` is empty for GREEN
    or when NEEDS_REVISION has no body.

    UNKNOWN when no marker found — callers should treat as
    NEEDS_REVISION (defensive).
    """
    if not text:
        return ("UNKNOWN", "")
    matches = list(_STATUS_RE.finditer(text))
    if not matches:
        return ("UNKNOWN", "")
    last = matches[-1]
    status = last.group(1)
    findings = (last.group(2) or "").strip()
    if status == "GREEN":
        return ("GREEN", "")
    return ("NEEDS_REVISION", findings)


# ---------------------------------------------------------------------
# Iteration state machine
# ---------------------------------------------------------------------


@dataclass
class ReviewIterationState:
    """Tracks the back-and-forth between author + reviewer for
    one role-pair (spec or impl)."""

    role_pair: Literal["spec", "impl"]
    iteration: int = 0  # 1-indexed; 0 = not yet started
    max_iterations: int = 5
    author_history: list[str] = field(default_factory=list)
    reviewer_history: list[str] = field(default_factory=list)
    findings_history: list[str] = field(default_factory=list)
    finished: bool = False
    outcome: Literal["GREEN", "ABORTED_UNCONVERGED", "PENDING"] = "PENDING"

    @classmethod
    def for_spec(cls) -> "ReviewIterationState":
        return cls(
            role_pair="spec", max_iterations=_spec_iteration_max(),
        )

    @classmethod
    def for_impl(cls) -> "ReviewIterationState":
        return cls(
            role_pair="impl", max_iterations=_impl_iteration_max(),
        )


def step_iteration(
    state: ReviewIterationState,
    *,
    author_status: str,
    reviewer_status: str,
    author_findings: str = "",
    reviewer_findings: str = "",
) -> ReviewIterationState:
    """Append the iteration's author + reviewer statuses,
    recompute the outcome.

    Both statuses must be ``GREEN`` in the SAME iteration to
    converge. Convergence is allowed on iteration 1 (no prior-
    round dependency) — the parent spec's "consecutive GREEN"
    phrasing refers to author + reviewer agreeing in the
    current round.

    Returns the same state (mutated).
    """
    state.iteration += 1
    state.author_history.append(author_status)
    state.reviewer_history.append(reviewer_status)
    combined_findings = " | ".join(
        f for f in (author_findings, reviewer_findings) if f
    )
    state.findings_history.append(combined_findings)
    if author_status == "GREEN" and reviewer_status == "GREEN":
        state.outcome = "GREEN"
        state.finished = True
    elif state.iteration >= state.max_iterations:
        state.outcome = "ABORTED_UNCONVERGED"
        state.finished = True
    else:
        state.outcome = "PENDING"
    return state


# ---------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------


_SHARED_FRAMING = (
    "You are participating in Kernos's autonomous-improvement "
    "loop. This is a substrate that modifies its own source "
    "code under operator approval at the commit gate. Your "
    "output must end with a single line in this exact form:\n\n"
    "    STATUS: GREEN\n"
    "  OR\n"
    "    STATUS: NEEDS_REVISION <one-or-more findings>\n\n"
    "The orchestrator parses this marker to decide whether to "
    "advance or iterate. Do not include the STATUS line "
    "anywhere except the bottom of your response. Do not "
    "include backticks around the marker."
)


_ROLE_FRAMING = {
    "spec_author": (
        "Your role: SPEC AUTHOR. Given the operator's "
        "improvement requirement, draft a focused spec.md "
        "describing what will change, the acceptance criteria, "
        "out-of-scope notes, and any risks. Bias toward small + "
        "shippable. Honor Kernos's architectural conventions "
        "(memory store, capability flow, gate model, "
        "agent-facing natural simplicity). At the bottom of "
        "your spec output the STATUS line.\n\n"
        "If you're rewriting after reviewer findings, address "
        "each finding explicitly in the rewritten spec text. "
        "Set STATUS: GREEN only if you genuinely believe the "
        "spec is ready for implementation."
    ),
    "spec_reviewer": (
        "Your role: SPEC REVIEWER. Read the spec the author "
        "produced. Verify it's scoped sensibly, that ACs are "
        "real (not vibes), that the spec honors Kernos's "
        "agent-facing natural-simplicity principle (substrate "
        "structure for operators, prose for the agent), and "
        "that risks + out-of-scope are explicit. Output "
        "STATUS: GREEN if the spec is ready for implementation; "
        "STATUS: NEEDS_REVISION <specific findings> otherwise. "
        "Be specific in findings — vague reviews force the "
        "author to guess."
    ),
    "impl_author": (
        "Your role: IMPLEMENTATION AUTHOR. The spec has been "
        "GREEN'd. Implement the changes in the worktree at "
        "the provided path. Produce a focused diff that maps "
        "back to the spec's ACs. Write tests that exercise "
        "the substrate-fidelity assertion pattern (behavioral "
        "signal AND substrate state in the same test). After "
        "implementing, write `impl_notes.md` summarizing what "
        "you changed + which ACs the diff covers. At the "
        "bottom of impl_notes.md output the STATUS line.\n\n"
        "If rewriting after reviewer findings, address each "
        "finding explicitly in the new diff + notes."
    ),
    "impl_reviewer": (
        "Your role: IMPLEMENTATION REVIEWER. Review the diff "
        "(via git_diff_for_review) against the spec's ACs. "
        "Verify: each AC has a matching test; tests follow "
        "substrate-fidelity (behavioral + substrate state); "
        "the diff doesn't expand scope beyond the spec; "
        "agent-facing surfaces stay natural-prose. Output "
        "STATUS: GREEN if the implementation matches the spec "
        "and tests pin the invariants properly; "
        "STATUS: NEEDS_REVISION <specific findings> otherwise."
    ),
}


def render_prompt(
    role: Literal["spec_author", "spec_reviewer", "impl_author", "impl_reviewer"],
    *,
    spec_requirement: str = "",
    iteration: int = 1,
    prior_findings: str = "",
    workspace_dir: str = "",
    spec_text: str = "",
) -> str:
    """Compose the system-prompt string for a role.

    ``spec_requirement`` is the operator-provided initial
    requirement (passed to spec_author).
    ``iteration`` is 1-indexed.
    ``prior_findings`` is the previous round's findings text
    (empty on iteration 1).
    ``workspace_dir`` is the worktree path (passed to impl
    roles).
    ``spec_text`` is the latest spec content (passed to
    spec_reviewer + impl roles).
    """
    if role not in _ROLE_FRAMING:
        raise ValueError(f"unknown review role {role!r}")
    parts = [_SHARED_FRAMING, _ROLE_FRAMING[role]]
    if iteration > 1 and prior_findings:
        parts.append(
            "\nPrior iteration's findings (address each):\n"
            f"{prior_findings.strip()}"
        )
    if spec_requirement:
        parts.append(
            "\nOperator's improvement requirement:\n"
            f"{spec_requirement.strip()}"
        )
    if spec_text and role in ("spec_reviewer", "impl_author", "impl_reviewer"):
        parts.append(
            "\nCurrent spec text:\n"
            "```\n"
            f"{spec_text.strip()}\n"
            "```"
        )
    if workspace_dir and role in ("impl_author", "impl_reviewer"):
        parts.append(
            f"\nWorktree path: {workspace_dir}"
        )
    parts.append(
        f"\nIteration: {iteration} (max "
        f"{_spec_iteration_max() if role.startswith('spec') else _impl_iteration_max()})."
    )
    return "\n\n".join(parts)
