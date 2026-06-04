# SELF-MAINTENANCE-REVIEW-V1

Status: DRAFT (founder direction 2026-06-04 — "a task passed out ~once a day to review Kernos code and systems intention vs healthiest implementation, then report surfaced to the main Kernos agent itself to consider")

## 1. Principle: reflect daily, bring concerns AND ideas to yourself

Once a day, within the ark of the governing principles + covenants, KERNOS holds a slice of its own code and systems up to the light through **two lenses**:

- **Corrective** — is this still the healthiest implementation of its intention, or has it drifted / decayed / grown an unguarded edge?
- **Generative** — *even when it is functional and healthy*, is there a more efficient or effective way to do this? And does this function's **validity and role still hold up against the overarching intention of the whole KERNOS harness**? This is creative, holistic pondering, not bug-hunting.

It produces a short, honest report and surfaces it **to the main KERNOS agent to consider** — not to act on autonomously.

> Reflection, not autonomy. **Thoughtful evolution, not out-of-hand mutation — one minor improvement at a time.** The review only ever produces a thought for KERNOS to consider; every actual change still flows through the existing approval-gated `improve_kernos`. This is the self-stewardship vision made routine — KERNOS not only monitoring and surfacing issues, but slowly, deliberately evolving toward a better version of itself.

## 2. The line that matters

- **Audit is read-only.** The review NEVER edits code, never commits, never triggers a change on its own.
- **It surfaces for consideration.** The output is a whisper to the agent. The agent decides what (if anything) to do: nothing, raise to the founder, or draft an `improve_kernos` spec — through the normal gate.
- **Honest when healthy.** If the slice is fine, it says so ("reviewed X, healthy, no action") — it does NOT manufacture concerns to seem useful (capability-first / honest-text principle).

## 3. Shape

1. **Cadence — daily, idle-aware.** Fires at most once per 24h, off-peak, and DEFERS if a turn is in flight or an `improve_kernos` attempt is running. Reuses the existing interval/workflow scheduling; no new scheduler.
2. **Scope — one rotating slice per day, nothing exempt.** Review a single subsystem per run, advancing a cursor pinned in state. Cheap, focused, sustainable; covers the whole system over ~a week. NOT a whole-codebase sweep. **The slice list includes the maintenance methodology itself** — the daily review, the self-healing lane, and the governing intention (operating principles). KERNOS turns the same two lenses on *how it reviews and evolves itself*. Those meta-slices are marked **constitutional**: reviewable + ponderable like anything else, but any evolution there is **human-gated** — surfaced to the founder to weigh, never self-applied (aligns with the recursive-self-heal constitutional set + "start.sh is human-only").
3. **The review — two lenses.** For the day's slice, read the *intent* (TECHNICAL-ARCHITECTURE, kernel outline, the relevant spec) and the *as-built* code, then assess in ONE bounded reasoning consult (not a multi-agent gauntlet):
   - **Corrective lens:** Does the implementation still serve the documented intention, or has it drifted? Is there a healthier / simpler / more elegant shape? Dead code, redundancy, an unguarded failure mode, a violated principle/covenant?
   - **Generative lens (runs even when the slice is healthy):** Is there a more *efficient or effective* way to handle this function? Does its **validity and role still hold up against the overarching intention of the whole KERNOS system** — is it still pulling its weight, still in the right place, still worth its complexity? If a thoughtful evolution suggests itself, propose **exactly one minor, well-reasoned step** — never a sweeping rewrite.
   Output a short structured report: `{slice, intention_summary, corrective_findings[], evolution_idea (≤1, optional), serves_the_whole (bool + why), overall_health, suggested_direction}`.

   **Evolution discipline (binding):** at most ONE evolution idea per review, and it must be *minor, reversible, and justified by how it serves the whole* — not novelty for its own sake. If nothing is genuinely worth evolving, the generative lens returns nothing. Thoughtful evolution earns its place; churn does not.
4. **Surface — a whisper to the agent.** Emit the report as an agent-facing whisper: "Here's today's self-review of `<slice>` — health, and one idea for thoughtful evolution if there is one. Consider whether any of it warrants raising to the founder or proposing a single minor improvement." The agent considers it on its next turn and decides whether to act through the gate.
5. **De-dup.** Track surfaced findings (fingerprint per slice+concern); suppress a repeat for N days so the same observation doesn't nag every rotation.
6. **Receipts.** Log each review (slice, findings, surfaced?) to an audit trail so the founder can see the cadence + what KERNOS has been noticing over time.

## 4. Guardrails (the ark)

- Read-only; no autonomous mutation. Action only via the existing approval gate.
- Bounded cost: one reasoning consult per day, one slice.
- Idle-aware: never competes with a live turn or an in-flight improvement.
- Covenant-bound: the review respects the governing principles + covenants; it may FLAG a principle violation but never override one.
- **Evolution discipline:** ≤1 minor, reversible, well-justified evolution idea per review; serves-the-whole or it isn't raised. No sweeping rewrites, no novelty-for-its-own-sake, no compounding churn. Thoughtful evolution, one minor step at a time.
- Env kill switch (`KERNOS_SELF_MAINTENANCE_REVIEW`); v1 ships **default-off** and is enabled deliberately after the first watched cycle.
- Honest-when-healthy (no manufactured concern) AND honest-when-nothing-to-evolve (the generative lens returns nothing rather than inventing an idea).

## 5. Why it's safe + elegant

It reuses what already exists — whispers (the surface), reasoning (the review), workflow scheduling (the cadence), `improve_kernos` (the gated action) — and adds only an observer that thinks out loud once a day. No new dangerous capability; the dangerous part (changing code) stays behind the gate it's already behind. It is the recurring, formalized version of the self-audit prompts (intention, health, gaps, next-step) and the natural daily heartbeat of the self-stewardship + recursive-self-heal arc.

## 6. Build sequence

1. Review descriptor + rotating-slice cursor in state.
2. The review consult (intent + as-built → structured report), honest-when-healthy.
3. Whisper surface to the agent (consideration framing) + de-dup + audit receipt.
4. Daily idle-aware trigger (defer on busy / in-flight attempt) behind the kill switch.
5. Live gate: run one cycle on a slice, confirm the whisper reaches the agent and the agent can act (or not) through the normal gate.
