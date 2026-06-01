# RESUME-FROM-LEDGER-V1 (draft — design to a reviewable point)

**Status:** DRAFT for design review. NOT implemented. Authored autonomously
2026-06-01 as the reviewable artifact for the highest-value pattern adopted
from Claude Code's Workflow tool (resume-from-journal). Ship only after
architect review — it changes the improvement loop's control flow and has
real correctness risks (see §5).

## 1. Problem

The `improve_kernos` loop is all-or-nothing per attempt. When an attempt
aborts (consult hang/timeout, unconverged, a failed deploy) and is re-fired,
a **brand-new attempt starts from scratch** — it re-derives the spec even
when the previous attempt had already converged it.

Observed live (`att_c8159f092bf7`, 2026-06-01): the spec converged in 2
review rounds (author=GREEN, reviewer=GREEN), then the *implementation*
consult hung and the attempt aborted at the 30-min cap. A retry throws away
the converged spec and re-runs the whole spec phase — wasted consults, wasted
wall-clock, and a fresh chance to *not* converge.

## 2. Insight

**KERNOS already keeps the journal — it just never replays it.** Every step
is durably recorded in `improvement_attempt_events` (spec_iteration with
author/reviewer verdicts, impl_iteration, etc.) and the converged spec lives
in the attempt workspace (`improvement_workspace/att_X/spec.md`). The pattern
from CC's Workflow tool (`resumeFromRunId`: unchanged steps return cached,
only new/edited steps re-run) maps directly onto infrastructure KERNOS has.
Resume = use the ledger + workspace we already produce.

## 3. Goal

A re-fired/retried attempt **resumes from the last durable checkpoint**
instead of restarting: if the spec already reached mutual GREEN, skip the
spec phase and re-enter at implementation with the converged spec.

## 4. Design (phased, conservative)

**Phase 1 — explicit, spec-only resume (safe slice):**
- New optional arg `resume_from: str` on `improve_kernos` (an attempt_id).
  Agent-set only when the user asks to retry/resume a specific prior attempt
  (per-call, not a global mode — same discipline as `debug_trace`).
- On resume: load the prior attempt's ledger. If it shows a converged spec
  (a `spec_iteration` with author=GREEN AND reviewer=GREEN) AND the prior
  workspace's `spec.md` exists + hashes to what the ledger recorded, **copy
  the converged spec into the new attempt and skip directly to the impl
  phase.** Emit a `resumed_from` ledger event recording the source attempt +
  what was reused. Otherwise fall back to a normal fresh attempt (resume is
  best-effort, never worse than starting over).
- The new attempt is still a distinct attempt_id with its own gate/approval —
  resume reuses *artifacts*, not *authority*.

**Phase 2 (later):** resume mid-impl (reuse partial worktree diff), and
auto-link a retry to its predecessor without an explicit id.

## 5. Why this needs review (correctness risks)

1. **Stale spec.** The reused spec was converged against the repo at the
   prior attempt's HEAD. If `origin/main` advanced (it does — auto-update),
   the spec may reference code that changed. Mitigation: re-validate the spec
   against current HEAD with one reviewer round before skipping; if it no
   longer holds, fall back to fresh. (This re-validation is the crux — getting
   it wrong ships a fix built on a stale premise.)
2. **Workspace/worktree reuse.** Each attempt uses its own git worktree.
   Resume must decide: fresh worktree + copied spec (cleaner, recommended) vs
   reuse the old worktree (faster but carries stale state, untracked debug
   files, etc.). Phase 1 = fresh worktree, copy only the validated spec.
3. **Convergence-budget interaction.** Skipping spec must not let an attempt
   loop indefinitely. Resume consumes a fresh impl budget; it must not also
   re-grant spec budget on fallback. (Founder's standing concern: "3 4 4 goes
   back to 3 — we don't start 3 4 4 5 6.")
4. **Authority boundary.** Resume reuses artifacts but each attempt keeps its
   own human approval gate — a resumed attempt must NOT inherit a prior
   approval. (Verify against `consume_approval`/`find_terminal_by_binding`.)

## 6. Acceptance criteria (when implemented)

- Re-firing with `resume_from=<converged-spec attempt>` skips the spec phase,
  emits `resumed_from`, and enters impl with the validated converged spec.
- A spec that no longer validates against current HEAD → clean fallback to a
  fresh attempt (no stale-premise implementation).
- Resume never grants more total budget than a fresh attempt + the reused
  phase; no infinite loop.
- A resumed attempt still pauses at its own approval gate; no inherited
  approval.
- Tests: converged-resume happy path, stale-spec fallback, no-prior-attempt
  fallback, budget non-escalation.

## 7. Out of scope (v1)

Mid-impl partial resume, auto-linking retries, cross-attempt worktree reuse.
