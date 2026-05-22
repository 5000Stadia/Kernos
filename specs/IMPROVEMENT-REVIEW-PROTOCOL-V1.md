# IMPROVEMENT-REVIEW-PROTOCOL-V1

**Date:** 2026-05-22
**Status:** Draft for review (sub-spec #5 of `KERNOS-AUTONOMOUS-IMPROVEMENT-LOOP-V1`)
**Scope:** Prompt templates for four roles (spec-author,
  spec-reviewer, impl-author, impl-reviewer) + GREEN /
  NEEDS_REVISION convergence-detection helpers + iteration
  counter state machine. Substrate-only — no agent surface,
  no slash command. Consumed by the future
  IMPROVEMENT-LOOP-WORKFLOW-V1 orchestrator.
**Estimated size:** ~200 LOC source + ~120 LOC tests.

## Why this spec exists

Per parent spec D3: spec/code review uses `consult` with
role-distinguished system prompts. After each iteration the
author marks `STATUS: GREEN` or `STATUS: NEEDS_REVISION
<reason>`; the reviewer returns the same shape. Both must
hit GREEN consecutively before the loop progresses.

This sub-spec ships the substrate that the orchestrator
workflow composes: prompt templates + the convergence regex
+ the iteration counter.

## Design principles (load-bearing)

Per [[agent-facing-natural-simplicity]]: this is
substrate-internal. The agent does NOT call these helpers —
the orchestrator workflow (future) calls `consult` with
prompts these helpers compose, then runs `detect_convergence`
on the returned text. Operator sees the iteration progress
via `/improvement_status`.

## Current state

- `consult` kernel tool exists; routes to external coding
  agents via ACPX.
- No prompt-template module for review roles.
- No GREEN/NEEDS_REVISION parser.

## Design

### Roles

Four roles, each with a system-prompt template:

1. **`spec_author`** — given a `spec_requirement`, drafts a
   `spec.md` with explicit `STATUS: GREEN | NEEDS_REVISION
   <findings>` marker at the bottom.
2. **`spec_reviewer`** — reads the spec, returns same shape
   marker. Must converge with author over up to
   `spec_iteration_max` iterations.
3. **`impl_author`** — given the GREEN'd spec, writes the
   implementation in the worktree + emits a same-shape
   STATUS marker in a `impl_notes.md`.
4. **`impl_reviewer`** — reviews the diff (`git_diff_for_review`)
   against the spec, returns marker. Must converge with
   author over up to `impl_iteration_max` iterations.

### Prompt templates

Each role has a system-prompt template substrate-composed from:
- The role's responsibilities ("you are reviewing a spec for
  a Kernos improvement attempt; output STATUS: GREEN or
  STATUS: NEEDS_REVISION <findings>")
- The shared substrate context (Kernos's architectural
  conventions reference, the layered-design principle, etc.)
- The iteration count + prior round's findings (when not the
  first iteration)

Templates live as module-level constants in
`kernos/kernel/improvement_review_protocol.py`. Composing
them is a pure function (`render_prompt(role, context)`).

### Convergence detection

```python
def detect_status(text: str) -> tuple[Literal["GREEN", "NEEDS_REVISION", "UNKNOWN"], str]:
    """Parse the STATUS marker from author/reviewer output.

    Looks for the LAST occurrence of:
      STATUS: GREEN
      STATUS: NEEDS_REVISION <findings>
      STATUS: NEEDS_REVISION (no body)

    Returns (status, findings). 'findings' is empty for GREEN
    or when NEEDS_REVISION has no body.

    UNKNOWN when no marker found — treated as NEEDS_REVISION
    by callers (defensive)."""
```

Regex: `r"STATUS:\s*(GREEN|NEEDS_REVISION)\b\s*(.*)$"` with
MULTILINE + DOTALL, picking the last match.

### Iteration counter state machine

```python
@dataclass
class ReviewIterationState:
    role_pair: str               # "spec" or "impl"
    iteration: int               # 1-indexed, increments per round
    max_iterations: int          # configured cap
    author_history: list[str]    # ["GREEN", "NEEDS_REVISION", ...]
    reviewer_history: list[str]  # parallel
    finished: bool               # True when converged or capped
    outcome: Literal["GREEN", "ABORTED_UNCONVERGED", "PENDING"]
```

```python
def step_iteration(
    state: ReviewIterationState,
    author_status: str, reviewer_status: str,
) -> ReviewIterationState:
    """Append the iteration results, recompute outcome."""
```

Converged when:
- Both `author_status` and `reviewer_status` are `GREEN` in
  the SAME iteration AND
- The previous iteration also ended GREEN OR this is iteration 1

`iteration >= max_iterations` without convergence → `outcome
= "ABORTED_UNCONVERGED"`.

### Defaults (configurable via env)

- `KERNOS_IMPROVEMENT_SPEC_ITERATION_MAX` (default 5)
- `KERNOS_IMPROVEMENT_IMPL_ITERATION_MAX` (default 3)

## Acceptance criteria

| AC | Description |
|---|---|
| AC1 | `detect_status` returns `("GREEN", "")` for text containing `STATUS: GREEN`. |
| AC2 | `detect_status` returns `("NEEDS_REVISION", "<findings>")` when STATUS includes findings text. |
| AC3 | `detect_status` returns `("NEEDS_REVISION", "")` when STATUS is `NEEDS_REVISION` with no body. |
| AC4 | `detect_status` returns `("UNKNOWN", "")` when no STATUS marker. |
| AC5 | `detect_status` picks the LAST occurrence when multiple STATUS markers appear (review-then-rewrite is common). |
| AC6 | `render_prompt("spec_author", context)` produces a non-empty prompt string with the author's responsibilities + STATUS marker instructions. |
| AC7 | `render_prompt("spec_reviewer", context)` includes review-specific framing. |
| AC8 | `render_prompt("impl_author", context)` includes implementation framing. |
| AC9 | `render_prompt("impl_reviewer", context)` includes code-review framing. |
| AC10 | `render_prompt` includes prior iteration's findings when `iteration > 1`. |
| AC11 | `step_iteration` increments the iteration counter + appends to histories. |
| AC12 | `step_iteration` marks `outcome="GREEN"` + `finished=True` when both author + reviewer report GREEN. |
| AC13 | `step_iteration` marks `outcome="ABORTED_UNCONVERGED"` + `finished=True` when iteration >= max_iterations without convergence. |
| AC14 | `step_iteration` leaves `outcome="PENDING"` when not converged + not capped. |
| AC15 | Env-configurable max iterations (`KERNOS_IMPROVEMENT_SPEC_ITERATION_MAX`, `KERNOS_IMPROVEMENT_IMPL_ITERATION_MAX`). |

## Out of scope

- The `consult` invocation itself — orchestrator workflow.
- Spec file writing / reading — orchestrator workflow (uses
  workspace).
- Cross-role context-passing — orchestrator workflow.
- Operator-visible iteration display — `/improvement_status`
  consumes the ledger's events appended by the orchestrator.

## Risks

- **Risk:** Author writes STATUS at the bottom AND mentions
  STATUS in the body of the spec ("STATUS: GREEN means
  ready"). `detect_status` picks the last occurrence, which
  is typically the actual marker — but a malformed spec
  could confuse the parser.
  - **Mitigation:** Tests pin the last-occurrence behavior.
    Documented in the prompt templates.

- **Risk:** Reviewer GREENS without actually reviewing
  (rubber-stamp).
  - **Mitigation:** Out of scope for this sub-spec.
    Future-spec consideration: require reviewer to list at
    least one observation before GREEN.

## Dependencies

- No new dependencies. Uses stdlib re + dataclasses.

## Migration

Additive module. No schema, no behavior change to existing
code.
