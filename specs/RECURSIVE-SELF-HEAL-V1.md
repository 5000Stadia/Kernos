# RECURSIVE-SELF-HEAL-V1

Status: DRAFT v2 (synthesis — author CC, anchored on KERNOS's interview 2026-06-04; Codex adversarial safety review YELLOW → folded, session 019e9073) 2026-06-04

> **§9 is binding.** The Codex safety pass found three things "not safe enough as written": classifier precision (no negative guards), the runaway bound (no *durable* depth), and the constitutional boundary (too narrow). §9 supersedes any looser statement above.

## 1. Principle: make it boring

Recursive self-improvement is the natural endpoint of `improve_kernos` — but the design *is* the guardrails. KERNOS's own framing (and this spec's spine):

> Not magical. Not free-form. Not "the agent can fix itself recursively." A **bounded recovery lane.**

```
attempt fails → evidence-backed MACHINERY failure → diagnosis artifact →
KNOWN failure signature → one child repair → DETERMINISTIC verification gate →
resume or stop
```

The goal: **resilience when KERNOS's own loop breaks** — without granting a vague mandate to recursively rewrite itself whenever it feels stuck.

## 2. The line that matters most

Distinguish, with evidence, two failure classes:
- **`classified_machinery_failure`** — a bug in KERNOS's *own* loop infrastructure (a known signature). → eligible to recurse.
- **`classified_task_failure`** — the task is hard, the agent's output is weak, convergence didn't happen. → **never recurse**; report + stop.

Misclassifying is how it runs away. So a machinery failure must match a **curated signature with a deterministic symptom AND a deterministic verification** — not an LLM judgment of "seems like our bug."

## 3. State machine (supervisor-owned, not the child agent)

```
original_running → failure_detected → diagnosing →
  ├─ classified_task_failure → parent_aborted (report; no recursion)
  └─ classified_machinery_failure →
       child_repair_requested → child_repair_running → child_repair_verifying →
         ├─ child_repair_passed → parent_resuming
         └─ child_repair_failed  → parent_aborted | human_review_required
```

The **supervisor** owns all transitions. The child repair attempt is a normal (bounded) `improve_kernos` run scoped to the diagnosed signature.

## 4. Initial allowlist (v1) — 5 signatures only

Each requires a **deterministic symptom** + a **deterministic verification** (a fixture/probe that passes or fails, no model judgment). The system may *propose* new classes but **may not auto-run** them.

| # | Signature | Deterministic symptom | Verification |
|---|---|---|---|
| 1 | Import/path failure in substrate self-tests | module import error | canonical self-test command passes |
| 2 | Tool receipt capture mismatch | controlled stdout/stderr probe mismatch | known probe output appears in receipt |
| 3 | Consult async drain/readback failure | request completes but readback/wake path fails | known consult reaches completed/failed/attempted correctly |
| 4 | Alias repair miss for known alias | registered alias fails to resolve | exact alias dispatch succeeds or fails with correct classified error |
| 5 | Worktree dirty-state invariant failure | changed/untracked files not reflected in change detection | controlled tracked/untracked diff fixture *(this session's `2d46d05`/`4056489` bug)* |

## 5. v1 constraints (non-negotiable)

- **max depth: 1** — a child repair cannot itself recurse.
- **max child attempts per parent: 1.**
- **known failure signatures only** (the allowlist above).
- **deterministic invariant required** for every signature.
- **no recursion for weak task performance** (§2 line).
- **no auto-commit for orchestration / approval / tool-dispatch ("constitutional") changes** — those stay human-gated even inside a repair. (Aligns with CLAUDE.md: `start.sh` is human-only.)
- **explicit budgets** (time + cost) on the child repair.
- **idempotency checks** + **rollback plan before any mutation** (boot-guard remains the floor).
- **attempt graph** (`AttemptGraphEdge { from, relation, to }`) for full traceability — parent↔child links queryable.
- **no repeated fix for the same failed signature** (de-dup).
- **human circuit-breaker** before constitutional machinery changes.

## 6. Surfacing (concise but honest)

When recursion fires, the user sees one clear line — recursion is visible, not a process dump:

> "The parent improvement hit a machinery failure: the self-test gate could not import its probe modules. I'm spawning one bounded repair attempt for that infrastructure issue, then I'll rerun the gate and resume or stop."

## 7. Build sequence (dependency-ordered, hard done-gates)

1. **Signature classifier** — maps a legible failure (we have these post-Stage-1) to one of the 5 signatures or to `task_failure`. Deterministic match on the symptom, not an LLM vibe. Ships with the 5 fixtures.
2. **Attempt graph + state machine** — supervisor-owned transitions, parent/child linkage in the ledger.
3. **Child-repair dispatch** — a depth-capped, budgeted `improve_kernos` scoped to the signature's known cause→fix, with the deterministic verification gate.
4. **Resume/stop wiring** — on `child_repair_passed`, re-run the parent's failed step; else `human_review_required`.
5. **Surfacing + circuit-breaker** — the honest one-liner + a halt control.
6. **Live gate** — induce signature #5 (worktree invariant) on a throwaway, confirm: detect → classify → bounded child repair → verify → resume, all visible and bounded.

## 8. Highest-risk unknowns (validate before committing code)

1. **Classifier precision** — a `task_failure` misread as `machinery_failure` is the runaway path. The deterministic-symptom requirement is the guard; validate the classifier never fires on a weak-output round.
2. **Verification determinism** — each signature's fixture must be a true pass/fail, reproducible, no flakiness (else the loop "verifies" a non-fix).
3. **Constitutional boundary** — confirm the "no auto-commit for orchestration/approval/dispatch" carve-out actually covers every file whose change could disable the guardrails themselves.

*KERNOS's verdict, verbatim: resilience when its own loop breaks, without granting it a vague mandate to recursively rewrite itself whenever it feels stuck.*

## 9. Codex safety hardening (folded — BINDING, supersedes looser text above)

Adversarial review verdict: **YELLOW — build only after these.** A recursive self-modifying loop is unsafe without them.

### 9.1 Classifier: positive match AND a negative guard (per signature)
A deterministic symptom is NOT sufficient — a *task* failure can mimic a machinery signature. Every signature requires `match(symptom) AND NOT explained_by_task` (task difficulty, agent non-output, malformed/missing `STATUS`, hard-task timeout). Per-signature negative guards:
- **#1 import/path** — recurse only if the failing import is in **unchanged canonical self-test/probe plumbing**, not in task-touched code (a bad impl that adds/renames modules also throws `ImportError`).
- **#2 receipt mismatch** — recurse only on an **isolated fixed probe** through the receipt path, never the attempt's own noisy stdout/stderr (a task can emit junk / exceed caps / inspect the wrong receipt).
- **#3 consult drain/readback** — **weakest signature**; require a **synthetic echo / fake-agent probe proving transport failure**. A long response, missing STATUS, malformed bridge response, or agent stall all mimic "completed but readback failed."
- **#4 alias miss** — the exact alias must be in the **canonical alias table** and fail *before* task-specific dispatch (agent using an unknown alias is task failure).
- **#5 dirty-state** — require **objective** dirty state from `git status --porcelain`; "the reviewer saw no diff" *alone* is task failure, not machinery.

### 9.2 Verification: hermetic + exact (no real LLM/gateway/network)
Every verification is a fixture, run in isolation, asserting exact state — never a model judgment, never the live path:
- **#1** — isolate the *target import path*; "canonical self-test passes" is too broad (pytest/sys.path/soak-state/timeouts can green a non-fix).
- **#2** — assert exact **structured receipt fields / hash**, not presence in prose/log/truncated preview.
- **#3** — **fake/local echo adapter** + exact state transitions; NO real coding-agent/ACPX.
- **#4** — require **canonical alias resolution succeeds**; "succeeds or fails with correct error" still allows the alias to stay unresolved.
- **#5** — best class: fresh **temp git fixture** (tracked + untracked). Note sharp edge: `git_diff_for_review` now mutates the index via `git add -N .` (git_operations.py:399) — the fixture must reset and explicitly assert untracked handling.

### 9.3 Durable runaway bound (the current ledger CANNOT express this)
The ledger has attempts/commits/events but **no parent/child/depth/signature/dedup** (instance_db.py:208). Add a durable table:
```
improvement_attempt_edges(
  parent_attempt_id, child_attempt_id, relation, signature_id,
  failure_fingerprint, root_attempt_id, child_depth)
```
Constraints (DB-enforced, survive restart):
- `UNIQUE(parent_attempt_id, relation='recursive_repair')` — one child per parent.
- `UNIQUE(root_attempt_id, signature_id, failure_fingerprint)` — never repeat a fix for the same root+signature.
- `child_depth <= 1`; depth is checked **globally against `root_attempt_id`**, not per-signature (a child hitting a *different* signature must not recurse).
- Child attempts carry `recursion_disabled = true`.
- **Transactionally reserve** the child edge *before* spawn (else parent-resume → re-fail → spawn-again launders depth). In-memory or event-prose counters are insufficient — a restart launders depth.

### 9.4 Constitutional boundary (current carve-out is far too narrow)
Auto-proceed today only blocks `start.sh` + file-count + deletion-volume (improvement_loop_workflow.py:621). The **human-only set** (no auto-commit, even inside a repair) must cover every file whose change could disable the guardrails:
`start.sh`, `kernos/setup/{boot_guard,self_update,bring_up_substrate}.py`; `kernos/kernel/{improvement_loop_workflow,improvement_ledger,instance_db,git_operations,self_test_gate,improvement_review_protocol,approval_receipts,gate,reasoning,kernel_tool_registry,tool_aliases,fix_authorization}.py`, `tool_runtime*.py`; `kernos/kernel/external_agents/**`; `kernos/kernel/workflows/**`, `specs/workflows/*.workflow.yaml`; `specs/RECURSIVE-SELF-HEAL-V1.md`; guardrail tests under `tests/**` + `tests/substrate_soak/**`.
**Implement by reusing/extending the stricter substrate path lattice in `fix_authorization.py`** — not the narrow auto-proceed check.

### 9.5 Other must-haves before build
- **Default-off kill switch**: `KERNOS_RECURSIVE_SELF_HEAL=0` for v1.
- **Child repair is NOT a full `improve_kernos` surface**: the supervisor strips recursive tools and enforces `recursion_disabled`.
- **Boot-guard rollback branch**: if the child's commit is rolled back, mark `child_repair_rolled_back` → abort parent / human-review; do **not** reclassify as a fresh machinery failure.
- **Parent resume reruns ONLY the failed deterministic step**; if the signature *changed* after the child passed → human review, not another child.

### 9.6 Convergence target
Re-run this Codex pass after folding → **GREEN** before any code. Then build §7 behind the default-off flag, signature #5 (worktree dirty-state) first since its fixture is the most deterministic — and it's the bug we already fixed, so the lane can be proven against a known-good repair.
