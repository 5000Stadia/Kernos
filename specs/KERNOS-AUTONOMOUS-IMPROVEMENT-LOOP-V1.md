# KERNOS-AUTONOMOUS-IMPROVEMENT-LOOP-V1 — Design spec

**Date:** 2026-05-21 (revised post-Codex round 1: YELLOW → 9 findings folded)
**Status:** Draft for review (design spec, not implementation)
**Scope:** The end-to-end arc where Kernos autonomously improves
  itself: pulls latest source into an isolated workspace, dispatches
  a spec requirement to a coding agent, reviews back-and-forth until
  GREEN, has the coding agent implement, reviews the implementation
  until GREEN, requests operator approval to commit, self-restarts
  to pick up the new version, and self-tests until first-pass GREEN.
**Estimated size:** 0 LOC. This is a design.

## Why this spec exists

The operator's verbatim ask (2026-05-21):

> I want a tool to improve architecture that
> 1. Downloads latest github of kernos to a container,
> 2. passes spec requirement to determined primary coding agent for
>    spec creation against the source structure,
> 3. reviews with kernos itself as spec reviewer, back and forth
>    until green.
> 4. When green primary coding agent implements,
> 5. kernos does code review, until green,
> 6. on green request to commit to repo,
> 7. then kernos self restart to get new version,
> 8. then self test until green,
> 9. if not green repeat from step code review
>
> until this loop shows green out of the gate on first test after
> commit.

This is the **autonomous-improvement loop** — Kernos doing for its
own substrate what we (CC + Codex + operator) have been doing
manually across the last several batches: spec, review, implement,
review, commit, restart, verify. The first-pass GREEN target is the
operator's quality bar: the loop is "done" only when the implementation
is so well-converged that the very first test after commit succeeds.

**Prerequisite met:** `SELF-CONTROLLED-LOOP-LIVENESS-V1` (commit
`2758538`) makes the substrate this loop sits on actually work
— `manage_plan` admits even when the model hallucinates the name,
and workflows now fire on lifecycle-accurate signals. Without that,
the autonomous loop couldn't have run on any substrate.

**~60% of primitives exist.** The audit (2026-05-21) confirms:
`consult` + `ask_coding_session` (coding-agent dispatch), workflow
engine (durable scheduled work with restart-resume), `restart_self`,
`self_update` (auto-pull on boot), event stream, DispatchGate. The
gaps are in composition + a few new primitives — not in foundational
substrate.

## Current state (truth, not memory)

(From 2026-05-21 audit. Pin: recheck before implementing each
sub-spec.)

### What exists and is wired

- **External coding-agent dispatch** (`kernos/kernel/external_agents/`):
  - `consult` — synchronous in-turn dispatch. Harnesses:
    `claude_code | codex | gemini`. Session reuse via `session_id`.
    Read+write access to the repo by default.
  - `ask_coding_session` — async equivalent; returns `request_id`;
    poll via `read_coding_session_response`.
  - ACPX adapter handles process management, NDJSON parsing, stale-
    session detection, descendant cleanup. Production-ready.
- **Workflow engine** (`kernos/kernel/workflows/`): durable scheduled
  work with **restart-resume support**. State in `workflow_executions`
  SQLite table. Audit events: `workflow.execution_started`, `..._step_succeeded`,
  `..._step_failed`, `..._paused_at_gate`, `..._terminated`. Action
  library has `notify_user`, `call_tool`, `mark_state`, `append_to_ledger`,
  etc. **This is the right substrate for the loop** — manage_plan
  doesn't survive restart; workflows do.
- **`restart_self`** (`kernos/kernel/self_admin_tools.py:97`): two-call
  pattern with `confirm=true`. `os.execv(sys.executable, sys.argv)`.
  In-flight async dies including the current turn.
- **`self_update`** (`kernos/setup/self_update.py`): pulls + reinstalls
  + restarts on every boot when `KERNOS_AUTO_UPDATE=on` (default).
  Graceful fallback on every failure mode. So after a commit + push,
  the next boot of Kernos auto-pulls the new code.
- **`execute_code`** (`kernos/kernel/code_exec.py`): subprocess
  sandbox with monkey-patched filesystem allow-list. Scope:
  `KERNOS_WORKSPACE_SCOPE=isolated|unleashed`. **Not a real
  container** — subprocess + preamble, no cgroup/VM. Sufficient
  for accidental-malice protection; insufficient for determined
  adversary. Critical for this design: `execute_code` can run
  `git` shell commands inside the workspace.
- **DispatchGate + approval tokens** (`kernos/kernel/gate.py`):
  classification model + UUID-keyed tokens. Tokens are
  **process-scoped** — lose on restart. This is a real problem for
  long autonomous runs that span restart boundaries.
- **Event stream** (`kernos/kernel/event_stream.py`): SQLite-backed
  append-only timeline. `register_post_flush_hook` for downstream
  observers. Per-instance queryable.
- **`SELF-CONTROLLED-LOOP-LIVENESS-V1`** (`2758538`): the boot-smoke
  sentinel that proves the substrate event-trigger-workflow loop
  runs end-to-end after every restart. This is the load-bearing
  proof that the autonomous-improvement loop has a substrate to
  sit on.

### What's partial

- **`manage_plan`** (`kernos/kernel/execution.py`): create/continue/
  status/pause actions. JSON file per space. **Does NOT survive
  restart** — no workflow-level resume hook. So the loop cannot live
  in manage_plan; it must live in a workflow.
- **Approval gates**: tokens lose on restart. Approval predicates
  defined for workflow gates, but no operator-visible "request to
  commit" surface beyond `notify_user`.
- **Per-call audit**: `ToolInvocationAuditEntry` is rich for
  service-bound tools; live-integration dispatcher emits its own
  separate entries (gap covered in `TOOL-MAKING-ARC-V1` design).
- **Live-integration policy gate**: only `classify_tool_effect` runs;
  full `DispatchGate.evaluate` is acknowledged-future
  (`TOOL-MAKING-ARC-V1` Section D1).

### What does NOT exist yet

These are the new primitives this design adds:

- **Isolated workspace primitive for source operations.** The operator's
  ask says "container," but the practical near-term target is a
  **git-worktree-based workspace** rooted in `data/<instance>/improvement_workspace/`,
  reusing the existing `execute_code` sandbox for command execution.
  A real container (Docker/podman) is a future-spec upgrade path.
- **Agent-callable git surface.** No `git_commit` / `git_push` /
  `git_diff_for_review` tools today. The loop's commit step needs
  these as a kernel-tool primitive, not as shell-out via execute_code
  — gate classification + audit fidelity demand explicit primitives.
- **Loop-orchestration workflow.** A new workflow descriptor
  (`improvement_loop.workflow.yaml`) that owns the 9-step arc end-to-
  end. Survives restart, has explicit approval gates, has timeout +
  retry semantics per step.
- **First-pass GREEN test gate.** A unified "self-test" command Kernos
  can call on itself post-restart. Scope-question: full pytest suite
  (slow, has known stall at ~74%) vs. a curated smoke gate. This
  spec picks **curated smoke** as v1 with full-suite as v2.
- **Improvement-attempt ledger.** A new structured ledger
  (`improvement_attempts`) recording: start, spec-review iterations,
  impl iterations, commit decision, restart event, test outcome, final
  state. Single-row-per-attempt with referenced sub-rows; survives
  restart; the operator's "follow along" surface.
- **Operator approval surface for commit gates.** Tokens that survive
  restart, delivered via a durable notification (Discord DM that
  references the attempt_id; operator replies with `/approve <id>`).
- **Coding-agent role assignment.** The operator's ask says
  "determined primary coding agent" — implies one of
  `claude_code | codex` is picked per-attempt based on policy
  (configurable; default = whichever is available + cheaper). Spec
  this explicitly so the loop's behavior is predictable.

## End-to-end arc — the contract

```
                  ┌─────────────────────────────────────────────┐
                  │  Operator (or workflow trigger) initiates:  │
                  │  improve_kernos(spec_requirement=<text>,    │
                  │                 attempt_id=<auto-generated>)│
                  └─────────────────────┬───────────────────────┘
                                        │
                ┌───────────────────────▼──────────────────────┐
                │ Step 1: Isolate                              │
                │ - Create git worktree at                     │
                │   data/<instance>/improvement_workspace/     │
                │   <attempt_id>/                              │
                │ - Pulls origin/main into worktree            │
                │ - Verifies clean state, captures HEAD sha    │
                │ → Emits improvement.attempt_started event    │
                └───────────────────────┬──────────────────────┘
                                        │
                ┌───────────────────────▼──────────────────────┐
                │ Step 2: Spec-creation loop                   │
                │ - consult(harness=primary_coding_agent,      │
                │           question=<spec_requirement>,       │
                │           workspace_dir=worktree)            │
                │ - Coding agent writes spec.md in worktree    │
                │ - Kernos reads spec.md, runs internal review │
                │   via consult(harness=secondary, role=review)│
                │ - Back-and-forth: agent revises → Kernos     │
                │   re-reviews → until both report GREEN OR    │
                │   spec_iteration_max (default 5) hit         │
                │ → Emits improvement.spec_reviewed (each      │
                │   iteration) + improvement.spec_green events │
                └───────────────────────┬──────────────────────┘
                                        │ spec green
                ┌───────────────────────▼──────────────────────┐
                │ Step 3: Implementation                       │
                │ - consult(harness=primary_coding_agent,      │
                │           question="implement the spec",     │
                │           context=spec.md path,              │
                │           workspace_dir=worktree)            │
                │ - Coding agent edits source in worktree      │
                │ → Emits improvement.impl_committed event     │
                └───────────────────────┬──────────────────────┘
                                        │
                ┌───────────────────────▼──────────────────────┐
                │ Step 4: Code review loop                     │
                │ - kernos_internal_code_review(worktree, spec)│
                │   – uses consult(harness=reviewer) for       │
                │     architecture-level critique              │
                │   – uses pytest on touched modules           │
                │ - GREEN: tests pass + reviewer says ship     │
                │ - Back to Step 3 if reviewer says revise OR  │
                │   tests fail, up to impl_iteration_max       │
                │ → Emits improvement.code_reviewed (each      │
                │   iteration) + improvement.code_green events │
                └───────────────────────┬──────────────────────┘
                                        │ code green
                ┌───────────────────────▼──────────────────────┐
                │ Step 5: Commit request (operator-gated)      │
                │ - Workflow pauses at approval gate           │
                │ - notify_user surfaces commit request to     │
                │   operator with attempt_id + spec diff +     │
                │   code diff summary                          │
                │ - Operator replies /approve <attempt_id> OR  │
                │   /reject <attempt_id> <reason>              │
                │ - Approval receipt persisted to              │
                │   improvement_commit_approvals table         │
                │   (durable, survives restart)                │
                │ → Emits improvement.commit_approved /        │
                │   improvement.commit_rejected event          │
                └───────────────────────┬──────────────────────┘
                                        │ approved
                ┌───────────────────────▼──────────────────────┐
                │ Step 6: Commit + push                        │
                │ - git_commit(worktree, message, approval_id) │
                │   → verifies pre-commit diff, writes         │
                │     commit_sha back into receipt             │
                │ - git_push(worktree, target_branch="main",   │
                │            approval_id)                      │
                │   → verifies receipt's commit_sha matches HEAD│
                │ → Emits improvement.committed event          │
                └───────────────────────┬──────────────────────┘
                                        │
                ┌───────────────────────▼──────────────────────┐
                │ Step 7: Self-restart                         │
                │ - Workflow records pending_self_test         │
                │   marker in improvement_attempts table       │
                │ - call_tool(restart_self, confirm=true,      │
                │             reason="autonomous improvement   │
                │             commit <attempt_id>")            │
                │ - Process restarts; self_update auto-pulls   │
                │   the new commit (it's on origin/main)       │
                │ → Restart loses workflow's current execution,│
                │   but boot-resume sees pending_self_test +   │
                │   resumes the workflow at Step 8             │
                └───────────────────────┬──────────────────────┘
                                        │ (post-restart)
                ┌───────────────────────▼──────────────────────┐
                │ Step 8: Self-test gate                       │
                │ - call_tool(run_self_test_suite, scope=smoke)│
                │ - Smoke gate (v1): pytest on a curated       │
                │   acceptance set + loop_health sentinel +    │
                │   the spec's own acceptance tests            │
                │ - Pass: improvement.test_green → DONE        │
                │ - Fail: improvement.test_failed              │
                └───────────────────────┬──────────────────────┘
                                        │ if fail
                ┌───────────────────────▼──────────────────────┐
                │ Step 9: Recovery branch                      │
                │ - If iteration_after_commit < max_recovery   │
                │   (default 2): jump back to Step 4 with the  │
                │   test output as context.                    │
                │ - If max exceeded: improvement.attempt_failed│
                │   → operator notified; manual revert decision│
                │ - Loop SUCCESS criterion = test green on     │
                │   FIRST attempt post-commit (no recovery     │
                │   iterations needed)                         │
                └───────────────────────┬──────────────────────┘
                                        │
                ┌───────────────────────▼──────────────────────┐
                │ Terminal: improvement.attempt_completed      │
                │ - Final ledger row: spec_iters, impl_iters,  │
                │   final_commit_sha, test_outcome,            │
                │   first_pass_green                           │
                │ - Improvement workspace cleanup (deleted     │
                │   after configurable retention window)       │
                └──────────────────────────────────────────────┘
```

## Design decisions

### D1 — Sandbox shape: git worktree as TRUSTED-AGENT v1 (not a security sandbox)

The operator's ask says "container." This spec ships a **git
worktree** rooted at `data/<instance>/improvement_workspace/<attempt_id>/`
in v1, with a **container-required upgrade path for v2**.

**Trust boundary, stated explicitly:**

The worktree is **NOT a security sandbox.** It protects the live
process's source tree from accidental edits during a successful
attempt; it does NOT protect against a misbehaving or
compromised coding agent. The current coding-agent dispatch
(`consult` via ACPX with `--approve-all`) gives the spawned agent:

- Full filesystem read/write within the worktree
- The ability to shell out arbitrary commands via its own tools
- Access to credentials in the parent process environment that
  the subprocess inherits (KERNOS_* env vars, git creds, MCP
  tokens, etc.)
- The ability to push to the same git remote the live Kernos uses

Worst-case if a coding agent goes rogue: full operator-machine
compromise — credential exfiltration, files outside the worktree
modified, destructive git push to origin, Kernos process killed.

**v1 ships this loop only against TRUSTED CODING AGENTS** —
`claude_code` and `codex` as operator-vetted commercial CLIs. The
loop must be initiated by the operator (via `improve_kernos`) and
the commit gate (Step 5) is operator-approved. Untrusted-agent
support is explicitly v2 and requires the container upgrade.

**v2 container upgrade path:** Replace `IMPROVEMENT-WORKSPACE-V1`'s
worktree implementation with a Docker/podman-backed primitive
WITHOUT changing the orchestration workflow. The workflow only
knows about "the workspace path" and "command execution against
that path"; the substitution is contained behind that interface.
v2 needs: image build, env-scrubbing, network egress policy, git
credential injection (not inheritance), and resource limits.

**Rationale for shipping v1 anyway:** Container infra is a
significant new dependency. The trusted-agent model is sufficient
to deliver the operator's immediate value (autonomous improvement
of Kernos's own substrate, with operator approval at commit). The
trust boundary is documented + visible in every sub-spec; v2 closes
it before opening the loop to untrusted agents.

**Implementation sub-spec:** `IMPROVEMENT-WORKSPACE-V1` — worktree
create/use/delete + branch contract (D1.1 below) + path scoping +
retention. Small.

### D1.1 — Worktree branch contract

`git worktree` cannot check out a branch already checked out
elsewhere — the live Kernos is on `main`, so the worktree can't
also be on `main`. The contract:

- Worktree branch name: `improvement/<attempt_id>`, created from
  `origin/main` at the attempt's start.
- All commits in the worktree go to `improvement/<attempt_id>`.
- Step 6 push: `git push origin HEAD:main` ONLY IF the operator-
  approved commit's `expected_parent_sha` (captured at THIS cycle's
  start; cycle 1 = original attempt base, cycle 2+ = previous cycle's
  pushed commit_sha) matches the current `origin/main` SHA at
  push time. If `origin/main` has drifted, emit
  `improvement.head_drifted_pre_push` and refuse the push.
  Operator decides whether to abort or re-attempt with a rebase.
- **No force push, ever.** `git push --force` is explicitly
  disallowed by the `git_push` primitive (gate refuses).
- Worktree cleanup: `git worktree remove` after retention window
  (or immediately on `attempt_aborted_*`).

### D2 — Loop lives in a workflow, NOT in manage_plan

The audit confirmed `manage_plan` does not survive restart; the
workflow engine does. Step 7 (restart) makes this an absolute
requirement: the loop MUST be re-enterable after restart at Step 8.

**Implementation sub-spec:** `IMPROVEMENT-LOOP-WORKFLOW-V1` — new
`improvement_loop.workflow.yaml` descriptor + helper + action wiring.
9 steps, multiple approval gates (Step 5 for commit; Step 7 implicit
via restart marker; Step 9 for max-iteration escalation).

### D3 — Spec/code review uses `consult`, with role-distinguished prompts

"Kernos as spec reviewer" and "Kernos does code review" both mean:
Kernos issues a `consult` call to a coding-agent harness with a
**reviewer-role system prompt**. There is no special "Kernos
internal reviewer" primitive — the existing `consult` IS the path.

Convergence protocol:
- After each spec iteration: spec author marks `STATUS: GREEN` or
  `STATUS: NEEDS_REVISION <reason>` at the bottom of `spec.md`.
- Reviewer reads the spec, returns same shape: `GREEN` or
  `NEEDS_REVISION <findings>`.
- Both must hit GREEN consecutively before Step 3 proceeds.
- Hard cap: `spec_iteration_max=5` (default; configurable per attempt).
- Same protocol for code review at Step 4: `impl_iteration_max=3`.

If either max is hit without convergence: emit
`improvement.attempt_aborted_unconverged` and escalate to operator.

**Implementation sub-spec:** `IMPROVEMENT-REVIEW-PROTOCOL-V1` —
prompt templates + the convergence-detection regex + iteration
counters in the workflow context.

### D4 — Coding-agent role assignment

The operator's ask: "determined primary coding agent." Implies a
policy:

- `primary_coding_agent` — does spec authoring + implementation.
  Default: `claude_code` (training-side strength in implementation).
- `reviewer_coding_agent` — does spec review + code review. Default:
  `codex` (independent perspective + lower per-call cost).
- Both configurable per-attempt via the `improve_kernos` tool kwargs.
- If primary == reviewer, the design still works (same model, different
  prompts), but the operator should know.

**Implementation:** baked into `improve_kernos` tool input schema.
No separate sub-spec.

### D5 — Git operations as kernel tools, not shell-outs

Full tool set (designed for audit fidelity, gate classification,
and explicit safety invariants per tool):

**Read tools:**
- `git_fetch(workspace_dir, remote="origin")` — read; updates
  remote-tracking refs.
- `git_rev_parse(workspace_dir, ref)` — read; returns SHA of a ref.
  Used to capture base + verify expected-HEAD.
- `git_status(workspace_dir)` — read; verifies clean state at attempt
  start.
- `git_diff_for_review(workspace_dir, base="origin/main", head="HEAD")`
  — read; returns the diff for review.

**Worktree management:** owned exclusively by
`IMPROVEMENT-WORKSPACE-V1`. The substrate creates + removes
worktrees as part of attempt lifecycle; agents never directly
invoke worktree-create/remove (they consume the already-created
worktree via the agent-callable tools below).

**Mutation tools:**
- `git_commit(workspace_dir, message, approval_id, files=[])` —
  `hard_write`. Reads the approval receipt, verifies the pre-commit
  worktree-index diff hash matches `expected_diff_hash`, verifies
  the current parent matches `expected_parent_sha`. Refuses on any
  drift. Stages only the listed files (NEVER `git add -A`); refuses
  if files outside the worktree are specified. On success, writes
  the new `commit_sha` back into the receipt row (single atomic
  update).
- `git_push(workspace_dir, target_branch="main", approval_id=...)`
  — `hard_write` + `external_agent_read` (network egress). Reads the
  approval receipt, verifies its `expected_parent_sha` still matches
  `origin/main`, verifies the worktree's HEAD == receipt's
  `commit_sha` (populated by the prior `git_commit`). Refuses
  `--force` always. Refuses if receipt is not in state=`approved`
  with `commit_sha` populated.

All tools registered as kernel tools with explicit gate
classifications. Each tool validates its `workspace_dir` is under
the `data/<instance>/improvement_workspace/` root — refuses if
operator (or hallucinating agent) passes a path outside the
managed workspace area. Workspace-guard primitive ships as part
of `IMPROVEMENT-WORKSPACE-V1` and is used by every git tool.

Rationale: gate classification + audit fidelity demand explicit
primitives. Shell-out via `execute_code` would put the git work
behind the `execute_code` audit surface, blurring the audit
trail for what's actually a code-modifying action. The workspace
guard catches the case where a coding agent in a worktree tries
to commit/push from a different path (e.g., the live Kernos
source tree).

**Implementation sub-spec:** `GIT-OPERATIONS-PRIMITIVES-V1` — full
tool set above + gate entries + audit category + workspace-guard
integration. Larger than originally scoped but still self-contained.

### D6 — Self-test gate is curated smoke (v1), full suite (v2)

The full pytest suite has a known stall at ~74% (per memory:
`project_pytest_full_suite_stall.md`). Running it as the
first-pass-GREEN gate would block every autonomous attempt for
hours and surface unrelated flakes.

V1 smoke gate (curated set):
- `tests/test_self_controlled_loop_liveness.py` (LIVENESS-V1 contract)
- `tests/test_substrate_bringup_providers.py` (PROVIDER-INJECTION-V1)
- `tests/test_gateway_health_observer.py` (HEARTBEAT-CROSSCHECK-V1)
- The spec's own acceptance tests (the new feature's test file)
- `tests/test_self_improvement_workflow.py` (the autonomy-loop e2e)
- After-boot live-bot probe: assertion that `LOOP_HEALTH_EXECUTION_COMPLETED`
  fires within 30 seconds of boot

V2 (parked): unified test command that runs the full suite with
the 74% stall diagnosed.

**Implementation sub-spec:** `SELF-TEST-GATE-V1` — new
`run_self_test_suite` kernel tool + the curated set definition +
the after-boot probe + the result-to-ledger writer.

### D7 — Operator approval at commit gate (Step 5) — durable receipt model

The operator's ask: "request to commit to repo" — confirms approval
required at commit time. Fully autonomous commit is not the v1 shape.

**Approvals are durable receipts, not tokens.** The existing
DispatchGate's process-scoped `ApprovalToken` model (5-minute TTL,
lost on restart) is the wrong shape for a 24-hour autonomous-loop
commit gate. Receipts are bound to a specific approved act with
full re-verification at commit + push time.

**Non-blocking approval gate.** Workflow approval gates that
execute-then-wait inline would block unrelated workflows for the
24h gate window. The commit gate ships as an **external durable
state resumed by event**: the workflow PAUSES (writes state),
exits its worker turn (frees the worker), and resumes when the
operator's slash-command emits the approval event.

**Receipt is per-cycle, not per-attempt.**
A single attempt can have multiple commit cycles when post-commit
test failures trigger recovery iterations (Step 9). Each cycle
gets its own approval receipt because the operator is approving a
specific diff against a specific base — both of which change cycle-
to-cycle.

Approval receipt shape (new sqlite table `improvement_commit_approvals`):

| Field | Notes |
|---|---|
| `approval_id` | PK, UUID |
| `attempt_id` | FK to improvement_attempts |
| `commit_sequence` | 1, 2, 3 … for cycles within the attempt (cycle 1 is the first commit; cycle 2+ are recovery iterations) |
| `execution_id` | FK to workflow_executions row that's gated for this cycle |
| `gate_nonce` | matches the workflow gate's nonce (anti-replay) |
| `expected_parent_sha` | the current `origin/main` SHA AT THE START OF THIS CYCLE (for cycle 1: the attempt's original base; for cycle 2+: the previous cycle's pushed commit_sha) |
| `expected_diff_hash` | SHA-256 of the **pre-commit** worktree-index diff at approval-request time. `git_commit` verifies this BEFORE committing. |
| `commit_sha` | populated by `git_commit` AFTER it commits — the receipt initially has this NULL; commit-step writes the new SHA in. `git_push` later verifies the actual remote push targets this exact SHA. |
| `operator_actor_id` | who approved; matches `KERNOS_OPERATOR_ACTOR_ID` |
| `state` | `pending | approved | rejected | expired | consumed` |
| `state_reason` | for rejected, the operator's reason text |
| `requested_at`, `decided_at`, `expires_at` |
| `single_use` | bool, default true — consumed on first git_commit + git_push for this cycle |

**Diff-hash + SHA flow** (approve pre-commit diff → `git_commit`
creates + records SHA → `git_push` verifies recorded SHA):

1. **Approval request**: hash the PRE-COMMIT worktree-index diff
   (what the operator sees + approves). Receipt writes
   `expected_diff_hash` and `expected_parent_sha`; `commit_sha`
   is NULL at this point.
2. **`git_commit`**: re-compute the pre-commit diff hash; refuse
   if drifted from `expected_diff_hash`. Verify the worktree's
   current parent matches `expected_parent_sha`; refuse if drifted.
   Then commit. **Writes the resulting `commit_sha` back into the
   receipt** (single atomic update on the receipt row).
3. **`git_push`**: read `commit_sha` from the receipt; verify the
   worktree's HEAD now points at that SHA (refuses if drifted —
   detects e.g. an amended-commit scenario). Push that SHA to
   `origin/main` as a fast-forward only.

This ordering makes the receipt's lifecycle explicit:
`pending → approved (commit_sha=NULL) → commit_sha populated by
git_commit → consumed by successful git_push`. The operator
approves a specific diff + parent; the substrate creates the
commit and records its identity; the push respects that recorded
identity.

Lifecycle:
1. Workflow at Step 5 INSERTS row with `state=pending`,
   `gate_nonce=workflow.gate_nonce`, `expires_at=now()+24h`.
2. Workflow PAUSES (writes its state to `workflow_executions`)
   and exits its worker turn. The worker is free to handle other
   workflows.
3. `notify_user` action surfaces the request (attempt_id, diff
   summary, expires_at) to the operator's channel.
4. Operator replies `/approve <attempt_id>` or `/reject <attempt_id>
   <reason>`. Slash-command handler:
   - Verifies `actor_id == KERNOS_OPERATOR_ACTOR_ID`
   - Updates `improvement_commit_approvals` row state
   - Emits `improvement.commit_approved` (or `_rejected`) event
     into the event stream
5. The workflow's gate-resume predicate matches that event; the
   trigger runtime re-enters the workflow at Step 6.
6. Step 6's `git_commit` + `git_push` re-verifies the approval
   receipt: still `approved`, not expired, `expected_diff_hash`
   matches current diff, `expected_parent_sha` matches `origin/main`.
   Any mismatch → emit `improvement.commit_approval_drift` and
   abort with worktree preserved for operator inspection.
7. On successful push: receipt marked `consumed`.

Timeout: row hits `expires_at` → emit
`improvement.commit_timeout` + auto-mark `expired`; workflow's
expiry trigger matches and aborts the attempt.

**Implementation sub-spec:** `DURABLE-APPROVAL-RECEIPTS-V1`.
"Receipts" (not "tokens") because they carry per-act binding
distinct from DispatchGate's process-scoped tokens. The receipt
table is
generic-substrate plumbing useful beyond this loop; future
hard_write surfaces that need durable operator approval can
reuse the same shape.

### D8 — Improvement-attempt ledger as the single observer-visible truth

A new sqlite table `improvement_attempts` keyed by `attempt_id`:
- `attempt_id` (PK)
- `started_at`, `ended_at`
- `spec_requirement` (operator-provided text)
- `primary_coding_agent`, `reviewer_coding_agent`
- `worktree_path`
- `spec_iterations`, `spec_iterations_outcome`
- `impl_iterations`, `impl_iterations_outcome`
- `final_commit_sha` (the LAST committed SHA across all recovery
  cycles; null until at least one commit completes; updated on each
  cycle's git_push). Per-cycle commit truth lives in
  `improvement_attempt_commits` — the top-level field is just a
  convenience pointer to the latest.
- `test_outcome` (pass/fail/skipped)
- `first_pass_green` (boolean — the operator's SUCCESS criterion)
- `final_state` (`completed | aborted_unconverged | rejected_at_commit | restart_failed | test_failed_unrecovered`)

Plus two satellite tables (per-cycle truth + per-iteration narrative):

- **`improvement_attempt_commits`** — one row per commit cycle:
  `attempt_id` (FK), `commit_sequence` (1-based), `commit_sha`,
  `parent_sha`, `pushed_at`, `approval_id` (FK to
  `improvement_commit_approvals`), `test_outcome_after_this_commit`,
  `recovery_trigger` (the test failure that led to this cycle, null
  for cycle 1). Lets the operator reconstruct exactly which commits
  on `main` came from the autonomous loop and which were follow-up
  recoveries.
- **`improvement_attempt_events`** — append-only per-iteration
  detail (spec iteration reviewed, code iteration reviewed,
  restart fired, test passed/failed). Ordered narrative for
  `/improvement_status` to render.

The operator can `/improvement_status <attempt_id>` or
`/improvement_status` (latest) to follow along at any point.

**Implementation sub-spec:** `IMPROVEMENT-ATTEMPT-LEDGER-V1` —
schema + helpers + slash-command + maybe a Notion-bridge writer
for long-form attempt narratives.

### D9 — `improve_kernos` is the agent-callable entry point

The operator's ask: "I want a tool." The tool:

```
improve_kernos(
  spec_requirement: str,                # required, the change to make
  primary_coding_agent: str = "claude_code",
  reviewer_coding_agent: str = "codex",
  spec_iteration_max: int = 5,
  impl_iteration_max: int = 3,
  max_recovery_after_commit: int = 2,
  attempt_id: str = "",                 # auto-generated if empty
)
```

Returns: `attempt_id` immediately (the loop runs asynchronously via
the workflow engine). Operator follows via `/improvement_status`.

Classification: `hard_write` — initiates a workflow that will
modify and commit source. The initiating call itself can be auto-
approved (nothing destructive until Step 6's gate, which has its
own durable approval receipt). For v1, the
initiation itself can be auto-approved (it just starts a workflow;
nothing destructive until Step 6, which has its own gate).

**Implementation:** part of `IMPROVEMENT-LOOP-WORKFLOW-V1` sub-spec.

### D10 — Failure handling explicit

| Failure | Behavior |
|---|---|
| Step 1: worktree creation fails (dirty state, disk full) | `attempt_aborted_pre_spec` + operator notify |
| Step 2: spec_iteration_max hit without GREEN | `aborted_unconverged_spec` + ledger captures last spec.md |
| Step 3: implementation runtime error | retry up to 2x, then `aborted_impl_failed` |
| Step 4: impl_iteration_max hit without GREEN | `aborted_unconverged_impl` |
| Step 5: operator rejects | `rejected_at_commit` + reason captured |
| Step 5: gate timeout (24h default) | `commit_timeout` |
| Step 6: commit/push fails | retry once; if still fails `commit_failed` + worktree preserved |
| Step 7: restart succeeds but auto-update fails | next boot detects pending_self_test, attempts test anyway; if test passes against stale code, mark `test_passed_stale_code` (operator visible warning) |
| Step 8: test fails | route to Step 9; iteration counter increments |
| Step 9: bounded follow-up commits | Recovery iteration returns to Step 4 (code review with test output as context) → if review converges to GREEN → produces a NEW commit (NEW gate-nonce, NEW approval receipt with `commit_sequence` incremented, NEW `expected_parent_sha` = previous cycle's pushed SHA) → push → restart → re-test. Each recovery iteration is a full new mini-cycle through Steps 4-8. Hard cap: `max_recovery_after_commit` (default 2) follow-up commit cycles, NOT just 2 review iterations. The first-pass-GREEN success metric counts only the FIRST test attempt; if recovery cycles fire, `first_pass_green=false` even if eventually completing. |
| Step 9: max_recovery exhausted | `test_failed_unrecovered`; revert option surfaced to operator (manual git revert decision); the multiple commits made during recovery iterations are all preserved in main's history for operator audit |

Every terminal state writes to the ledger AND emits a structured
event. Operator can audit attempts retroactively.

## Sub-spec sequence

Lock this design first. Then ship in this order — each sub-spec
small and independently shippable with its own Codex review loop.
Sub-spec ordering reflects dependency chain: durable approval +
workspace guard ship before git tools that consume them; the review
protocol + ledger + self-test gate ship in parallel; the
orchestrator workflow is last because it composes everything.

1. **`DURABLE-APPROVAL-RECEIPTS-V1`** (D7). SQLite-backed approval
   receipts (per-act-binding shape, NOT process-scoped tokens) +
   `/approve` `/reject` slash-command handlers + workflow gate
   integration via event-resume (non-blocking). **Generic
   substrate; useful beyond this loop.** No dependencies. Ships
   first.
2. **`IMPROVEMENT-WORKSPACE-V1`** (D1 + D1.1). Owns workspace
   lifecycle end-to-end:
   - Worktree create / use / remove (the internal git operations
     are subprocess-level, NOT agent-callable — workspace
     management is substrate-owned)
   - Branch contract: `improvement/<attempt_id>` from `origin/main`
   - The **workspace-guard primitive** (validates any
     `workspace_dir` argument is under
     `data/<instance>/improvement_workspace/`); exposed for #3 to
     consume
   - Retention policy (default 7 days; configurable)
   - Trust-boundary documentation explicit (see "Trust model" section)

   Required BEFORE git ops so git tools can call the guard.
3. **`GIT-OPERATIONS-PRIMITIVES-V1`** (D5). Six agent-callable
   kernel tools that operate on an already-created worktree
   (worktree create/remove are owned by #2, not exposed as
   agent tools):
   `git_fetch`, `git_rev_parse`, `git_status`,
   `git_diff_for_review`, `git_commit`, `git_push`. Gate entries
   + audit category. Each tool calls the #2 workspace guard.
   `git_push` enforces expected-parent-SHA match + no-force, and
   requires a #1 approval receipt for the specific push cycle.
4. **`IMPROVEMENT-ATTEMPT-LEDGER-V1`** (D8). Schema for
   `improvement_attempts` + `improvement_attempt_events` tables +
   helpers + `/improvement_status` slash command. Independent;
   ships in parallel with #2/#3.
5. **`IMPROVEMENT-REVIEW-PROTOCOL-V1`** (D3). Prompt templates
   for spec-author / spec-reviewer / impl-author / impl-reviewer
   roles + the GREEN/NEEDS_REVISION convergence-detection regex
   + iteration counter state machine + the `consult`-call wiring
   for each role. Depends on nothing; ships in parallel.
6. **`SELF-TEST-GATE-V1`** (D6). `run_self_test_suite` kernel
   tool + curated smoke set (must include
   `tests/test_self_controlled_loop_liveness.py` — recursive
   testing of the loop's own substrate) + after-boot
   probe + result-to-ledger writer. Depends on #4.
7. **`IMPROVEMENT-LOOP-WORKFLOW-V1`** (D2 + D4 + D9 + D10).
   The orchestrator. Pulls in #1-#6 as dependencies. The new
   `improvement_loop.workflow.yaml` + `improve_kernos` tool +
   failure-mode handling. The 9-step state machine that uses
   every prior sub-spec.
8. **End-to-end integration test.** Synthetic full-loop run
   against a trivial spec ("add a one-line comment to README.md"),
   asserts all 9 steps fire + ledger row + commit sha + first-pass
   GREEN. Also tests the recovery branch with a forced test
   failure. Recursive coverage: tests the autonomy-loop's OWN
   substrate primitives — durable approval, workspace guard, git
   push expected-SHA refusal, ledger resume marker, self-test gate.
   Lands alongside #7.

Each sub-spec lands with: (a) Codex spec review, (b) implementation,
(c) Codex code review, (d) commit + push.

## D6 validation — when is the arc shipped?

The arc is "alive" when:

1. **Trivial-spec test passes end-to-end.** A `/improve_kernos
   spec_requirement="add a one-line comment '# improved by Kernos'
   to the top of README.md"` invocation:
   - Pulls latest, creates worktree
   - Spec converges in 1-2 iterations
   - Code converges in 1 iteration
   - Operator approves commit
   - Restart succeeds, new code loaded
   - Self-test gate (smoke) passes first-pass GREEN
   - Ledger row final state = `completed`, `first_pass_green=true`

2. **Failure modes proven.** Operator triggers attempt that should
   fail (e.g., spec_requirement that's ambiguous / contradictory);
   loop reaches `aborted_unconverged_spec` cleanly within iteration
   max; ledger reflects.

3. **Restart survival proven.** Operator manually `/restart` mid-loop
   (e.g., during spec iteration); on boot, workflow resumes from
   the last persisted step without losing the worktree.

4. **Operator visibility.** `/improvement_status` returns coherent
   real-time state at every step.

5. **Concurrent safety (or explicit refusal).** Two simultaneous
   `improve_kernos` calls either run in parallel (different
   worktrees) OR the second one is refused with a clear error.
   Spec picks REFUSED for v1 — one autonomous attempt at a time
   keeps the audit trail simple.

These five checkpoints are the live-bot validation gate.

## What this spec explicitly does NOT define

- **Real container isolation** (Docker/podman). Worktree is v1; container
  is a v2 follow-up.
- **Self-rollback on failed restart-and-test.** If Step 7+8 fail,
  the operator decides whether to `git revert` manually. Autonomous
  rollback is a future spec (needs careful "what counts as failure"
  semantics).
- **Multi-spec autonomous arcs.** v1 is **one-spec-per-attempt**.
  Multi-spec coordination (e.g., one attempt that creates a design
  spec + multiple implementation sub-specs in sequence) is parked.
  Within a single attempt, multiple commits ARE allowed for
  recovery iterations — see D10 / Step 9 below.
- **Coding-agent failure-recovery beyond retry.** If `consult` itself
  errors (network, billing, etc.), the loop retries once then aborts.
  Smarter fallback (try a different harness) is parked.
- **The CC-IMPLEMENTATION-CONTRACT workflow** (the human-Claude-Code
  shape we use today). That's a separate practice; this autonomous
  loop is for Kernos doing similar work without a human Claude-Code
  in the loop.
- **Per-spec configuration of the smoke gate.** v1 uses a single
  curated set; per-spec acceptance-test injection is a follow-up.

## Trust model

The autonomous-improvement loop runs trusted commercial coding
agents (claude_code, codex) on the operator's host machine with
filesystem + git credential access. This section makes the trust
boundary impossible to miss — for the operator initiating the loop,
for future maintainers extending it, and for future reviewers
evaluating whether to open the loop to a new agent class.

**What the v1 loop trusts:**
- The operator (initiates `improve_kernos`, approves at commit gate).
- The primary + reviewer coding agents (`claude_code`, `codex`)
  as commercial CLIs operated under operator-vetted credentials.
- The `consult` / `ask_coding_session` ACPX dispatch as a
  trusted-agent channel (NOT a sandbox).
- The host machine's git credentials (inherited by the spawned
  coding-agent subprocess).

**What the v1 loop does NOT trust but cannot fully verify:**
- That a coding agent's reasoning is correct (mitigated: reviewer
  agent in spec + code review loops; operator approval at commit).
- That a coding agent obeys its prompt and stays within the
  worktree (mitigated: workspace-guard primitive refuses
  out-of-worktree git operations; no further filesystem mitigation).

**What the v1 loop does NOT trust AND mitigates substrate-side:**
- The proposed diff at commit time vs. the diff at approval time
  (mitigated: `expected_diff_hash` on the approval receipt
  refuses if drifted).
- That `origin/main` hasn't moved since attempt start
  (mitigated: `expected_parent_sha` on git_push, re-verified at push time).
- That the coding agent doesn't `git push --force` (mitigated:
  the `git_push` primitive refuses `--force` at the gate).

**What's explicitly NOT mitigated in v1 (closed in v2 container):**
- A malicious or compromised coding agent attempting credential
  exfiltration, out-of-worktree filesystem modification, or
  unintended network egress.

**Composition note:**
- `SELF-CONTROLLED-LOOP-LIVENESS-V1` (shipped, commit `2758538`)
  is a clean substrate dependency. Required.
- `TOOL-MAKING-ARC-V1` (design locked at commit `2b290f8`, no
  sub-specs implemented yet) is a clean architectural pattern
  reference, but this loop does NOT depend on its sub-specs
  shipping. Specifically: `improve_kernos` and the six git
  tools are first-class kernel tools in this spec, NOT
  workspace-registered tools that would need TOOL-MAKING-ARC's
  D1 (catalog classification fix) or D3 (registration-time
  approval) to function.

## Risk

- **Scope.** Eight sub-specs is a lot. Mitigation: #1 + #4 are
  generic-substrate wins useful even if the autonomous loop slips
  (durable approval + ledger are good plumbing). #5 (self-test
  gate) is similarly reusable. So even partial completion delivers
  value.
- **Worktree disk usage.** Each attempt creates a full clone in
  `data/<instance>/improvement_workspace/<attempt_id>/`. ~50-100MB
  per attempt × N concurrent or recent = nontrivial. Mitigation:
  retention (default 7 days; configurable; old workspaces deleted
  by background cleanup).
- **Coding-agent misuse of git or shell.** A coding agent in the
  worktree CAN shell out arbitrary commands through its own native
  tools (claude_code's bash, codex's exec, etc.) — Kernos's
  `execute_code` sandbox does NOT scope what the spawned coding
  agent's process can do. The coding agent inherits the operator's
  git credentials. Worst case: agent issues `git push --force` from
  inside its own shell (bypassing Kernos's `git_push` primitive's
  no-force enforcement), or modifies files outside the worktree.
  **v1 mitigation: trusted-agent model only — operator-vetted
  commercial CLIs (claude_code, codex), operator-initiated
  attempts, operator-approved commits.** The Kernos-side gates
  on `git_commit` / `git_push` defend against accidental misuse
  WHEN the agent routes through Kernos's tools; they do not
  defend against the agent bypassing Kernos and shelling out
  directly. Container isolation (v2) is the substrate fix.
- **Auto-update race.** Step 7 restart triggers `self_update` on
  boot, which auto-pulls. If between Step 6 (push) and Step 7's
  boot, another commit lands on `origin/main`, the auto-pull
  picks up BOTH commits. The test gate would then verify against
  the combined HEAD, not the loop's commit. Mitigation: Step 7
  captures the expected commit_sha; Step 8 verifies HEAD matches
  before running smoke. If HEAD has drifted, emit
  `improvement.head_drifted` and proceed anyway (the operator's
  intent presumably tolerates this — concurrent commits to main
  are an organizational concern, not a loop concern).
- **First-pass GREEN is hard.** Real autonomous coding agents
  produce code that needs revision. v1's success metric (first-pass
  GREEN with up to 2 recovery iterations after commit) is forgiving
  but still demanding. If most attempts fall into recovery,
  iteration counts in the ledger will show the operator how
  realistic the current shape is.
- **Operator review burden.** Step 5 surfaces a commit request
  per attempt. If autonomous attempts happen frequently (e.g.,
  scheduled improvement runs), the operator could get pinged
  often. Mitigation: rate-limit `improve_kernos` initiations
  (default max 1 per hour, configurable); attempt aggregation is
  a follow-up.
- **Test gate too narrow.** The v1 curated smoke set might miss
  a regression that the full suite would catch. Mitigation: v2
  unifies to full suite once the 74% stall is diagnosed; until
  then, attempts can ship code that breaks unrelated tests. The
  ledger captures `test_outcome` so operators can detect this
  pattern and decide whether to expand the smoke set.

## Out of scope

- Anything Notion-related as a primary surface (the ledger is the
  primary; Notion is at most a long-form mirror).
- Multi-instance autonomous coordination (each instance owns its
  own attempts).
- MCP-side tool changes for the loop's needs (the loop uses what
  exists today).

## Acceptance for this design spec

This spec is "GREEN" when Codex agrees:

1. The current-state audit matches what's actually in the codebase.
2. The end-to-end contract (9 steps) closes all the operator's
   intent verbatim AND handles the failure modes plausibly.
3. The sub-spec sequence is correct — specifically that #1 + #4
   are independent / parallel, and that #6 has the dependencies
   right.
4. No sub-spec is missing.
5. The 5-checkpoint live validation is sufficient to call the
   arc "alive."
6. The "first-pass GREEN" success criterion is measurable and
   achievable enough to be meaningful.

Once GREEN, the design freezes. Implementation sub-specs are
written against this design as their north star. Bug fixes route
through the design, not around it.
