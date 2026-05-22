# IMPROVEMENT-LOOP-WORKFLOW-V1

**Date:** 2026-05-22
**Status:** Draft for review (sub-spec #7 — orchestrator — of `KERNOS-AUTONOMOUS-IMPROVEMENT-LOOP-V1`)
**Scope:** `improve_kernos` agent-callable tool + a Python
  orchestrator class that composes the 5 prior sub-specs into
  the happy-path autonomous improvement attempt. Ledger is the
  durability surface. v1 ships the happy path; recovery
  cycles + mid-attempt restart-resume + complex failure
  routing are explicitly deferred to a follow-up spec
  (`IMPROVEMENT-LOOP-RECOVERY-V1`) after operational evidence
  informs the right shape.
**Estimated size:** ~350 LOC source + ~200 LOC tests.

## Why this spec exists

Per parent spec D2 + D9 + D10: the autonomous improvement loop
needs a single entry point (`improve_kernos`) that an agent
can call. The orchestrator composes:
  - workspace lifecycle (IMPROVEMENT-WORKSPACE-V1)
  - ledger writes (IMPROVEMENT-ATTEMPT-LEDGER-V1)
  - git operations (GIT-OPERATIONS-PRIMITIVES-V1)
  - review-protocol cycles (IMPROVEMENT-REVIEW-PROTOCOL-V1)
  - self-test gate (SELF-TEST-GATE-V1)
  - receipts for commit approval (DURABLE-APPROVAL-RECEIPTS-V1)
  - `consult` for coding-agent dispatch (existing)
  - `restart_self` for post-commit reload (existing)

Parent spec called for a workflow-engine YAML implementation
for full durability. v1 implements as a Python orchestrator
class because:
1. The ledger already provides observer-durability — every
   step appends an event; mid-attempt crash + restart leaves
   the ledger as the recovery surface for the operator.
2. The workflow engine YAML route adds substantial spec
   complexity for v1 with unclear value beyond ledger
   durability.
3. Promoting to a workflow-engine YAML is a clean future move
   if operational evidence shows mid-attempt durability is
   load-bearing.

## Design principles (load-bearing)

Per [[agent-facing-natural-simplicity]]: `improve_kernos`
returns natural-prose status updates ("attempt started:
{id} — I'll work through the spec, get your approval, and
ping you when ready"). The operator gets full structured
state via `/improvement_status`.

Per parent spec's trust-boundary note: the worktree is NOT a
security sandbox. v1 ships against TRUSTED CODING AGENTS only
(`claude_code`, `codex`). `improve_kernos` only accepts those
two targets in its kwargs.

## v1 happy path (the 7 steps that actually ship)

1. **`improve_kernos(spec_requirement, primary_agent, reviewer_agent)`** —
   agent or operator calls. Orchestrator creates attempt_id,
   inserts ledger row, returns the attempt_id immediately for
   tracking. Continues asynchronously.
2. **Workspace create** — `ImprovementWorkspace.create(attempt_id)`.
   Ledger event: `workspace_created`.
3. **Spec drafting cycle** — runs review-protocol against
   primary_agent (author) + reviewer_agent (reviewer) via
   `consult`. Iterates until convergence or cap. Spec is
   written to `<worktree>/spec.md`. Ledger events:
   `spec_iteration` per round; `spec_converged` or
   `spec_aborted_unconverged` at the end.
3. **Implementation cycle** — primary_agent writes the impl
   in the worktree; reviewer_agent reviews via
   `git_diff_for_review`. Iterates until convergence or cap.
   Ledger events: `impl_iteration`, `impl_converged` or
   `impl_aborted_unconverged`.
4. **Operator approval** — substrate captures pre-commit state
   (`expected_parent_sha`, `expected_diff_hash`), issues a
   `git_commit_authorization` receipt via
   `request_approval()`. Returns approval_id to operator via
   natural-prose message ("Commit ready for approval:
   /approve {id} CONFIRM"). Ledger event: `approval_requested`.
   Orchestrator returns; awaits approval async.
5. **Operator approves (or rejects)** — `/approve` triggers
   the receipt approval. The orchestrator's continuation hook
   (post-approval callback in the `/approve` handler) fires
   the next step.
6. **Commit + push** — `git_commit` + `git_push`. Ledger events:
   `commit_recorded` (via record_commit), `push_succeeded`.
7. **Restart + post-restart test gate** — `restart_self` fires.
   On reboot, a bring-up hook checks the ledger for any
   `in_flight` attempts whose final step is "awaiting
   post-restart test." If found: run `run_self_test_suite`
   against the live (now-updated) Kernos's test directory.
   Ledger event: `self_test_result`; final_state set to
   `completed` (pass) or `test_failed` (fail; recovery is
   a future-spec follow-up).

## Explicit scope cuts (deferred to follow-up specs)

- **Recovery cycles after test failure** — when the post-
  restart test fails, v1 marks `final_state=test_failed_unrecovered`
  and surfaces the failure to the operator. Auto-recovery
  (spawn coding agent to fix the failed test, re-commit,
  re-restart) lands in `IMPROVEMENT-LOOP-RECOVERY-V1`.
- **Mid-attempt restart-resume** — if Kernos crashes mid-
  attempt (e.g., between spec convergence and impl), v1
  marks attempt as `in_flight_at_crash` on next boot via
  the ledger inspection but does NOT auto-resume. Operator
  decides whether to abandon or re-trigger.
- **`improvement.head_drifted_pre_push`** event handling —
  v1 surfaces the drift to the operator via `git_push`'s
  natural-prose error; doesn't auto-rebase. Future spec.
- **Per-step retry budgets** — v1 has the iteration caps
  from REVIEW-PROTOCOL but no per-step retry on transient
  `consult` failures. Future spec adds.

## `improve_kernos` tool surface

```python
IMPROVE_KERNOS_TOOL: dict = {
    "name": "improve_kernos",
    "description": (
        "Start an autonomous improvement attempt against "
        "Kernos's own source. The substrate spawns trusted "
        "coding agents to draft + implement a spec, then "
        "asks for operator approval before committing + "
        "restarting. Returns an attempt_id you can track "
        "via /improvement_status. The attempt continues "
        "asynchronously after this call returns."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "spec_requirement": {
                "type": "string",
                "description": (
                    "Operator's improvement requirement in "
                    "natural language. Will be passed to the "
                    "spec-author coding agent."
                ),
            },
            "primary_coding_agent": {
                "type": "string",
                "enum": ["claude_code", "codex"],
                "description": (
                    "Coding agent for spec authoring + "
                    "implementation. Default: claude_code."
                ),
            },
            "reviewer_coding_agent": {
                "type": "string",
                "enum": ["claude_code", "codex"],
                "description": (
                    "Coding agent for spec review + code "
                    "review. Default: codex. Same value as "
                    "primary is allowed but loses independent-"
                    "perspective benefit."
                ),
            },
        },
        "required": ["spec_requirement"],
    },
}
```

Gate classification: **`hard_write`**. The loop ultimately
modifies Kernos's source + restarts the process. Receipt-
requirement comes at the commit gate (Step 5), not at
attempt-start — starting an attempt is operator-visible
(via ledger + the attempt_id returned synchronously) but
doesn't itself mutate live code.

Pinned in `ALWAYS_PINNED` so the agent always has the
capability surfaced (matches the operator's mental model:
"any turn, you can ask me to improve myself").

## Architecture

```
kernos/kernel/improvement_loop_workflow.py
  - class ImprovementLoopOrchestrator:
      async def start_attempt(spec_requirement, primary, reviewer, instance_id) -> str
        # Creates attempt_id, ledger row, worktree
        # Returns attempt_id immediately
        # Spawns background task for the rest

      async def _run_attempt(attempt_id) -> None:
        # Background task: spec cycle → impl cycle → request approval
        # Ledger writes at every step
        # Returns when approval requested (continuation handed off)

      async def continue_after_approval(approval_id) -> None:
        # Called by /approve's post-approve callback
        # commit + push + restart
        # restart_self ends the process; post-restart hook
        # picks up the rest

      async def continue_after_restart(instance_id) -> None:
        # Called by bring-up hook on next boot
        # Checks ledger for in_flight attempts with
        # state=awaiting_post_restart_test; runs the test
        # gate against the live tree; marks final state.
```

The orchestrator's state is the LEDGER. No in-memory state
survives restart; everything load-bearing is appended via
`append_event` / `update_attempt`.

## Acceptance criteria

### Happy path

| AC | Description |
|---|---|
| AC1 | `improve_kernos(spec_requirement, ...)` returns prose containing the attempt_id within 5 seconds. |
| AC2 | Ledger row created with `started_at`, `spec_requirement`, agent assignments. |
| AC3 | Workspace worktree created at the expected path with branch `improvement/<attempt_id>`. |
| AC4 | Spec cycle: ledger event `spec_iteration` appended per round; `spec_converged` on success. |
| AC5 | Impl cycle: ledger event `impl_iteration` per round; `impl_converged` on success. |
| AC6 | At Step 5, `git_commit_authorization` receipt issued with `expected_parent_sha` + `expected_diff_hash`. Ledger event `approval_requested`. Operator-facing message includes the approval_id. |
| AC7 | `/approve <id> CONFIRM` triggers `continue_after_approval`. |
| AC8 | Commit + push: ledger events `commit_recorded` + `push_succeeded`; final_commit_sha bumped. |
| AC9 | `restart_self` fires after push. |
| AC10 | Post-restart bring-up hook detects in-flight attempt, runs self-test gate, writes ledger event `self_test_result`. |
| AC11 | On test pass: `final_state="completed"`, `first_pass_green=1`. |
| AC12 | On test fail: `final_state="test_failed_unrecovered"`. |

### Tool surface

| AC | Description |
|---|---|
| AC13 | `improve_kernos` in `_KERNEL_TOOLS` + `ALWAYS_PINNED` + classified `hard_write`. |
| AC14 | Schema rejects unknown `primary_coding_agent` values via the enum. |
| AC15 | Defaults: `primary=claude_code`, `reviewer=codex`. |

### Failure paths (v1 simple shape)

| AC | Description |
|---|---|
| AC16 | Spec cycle hits max iterations → `spec_aborted_unconverged`; attempt stops with `final_state="aborted_unconverged"`. |
| AC17 | Impl cycle hits max iterations → `impl_aborted_unconverged`; same final_state. |
| AC18 | `/reject` on the approval receipt → `final_state="rejected_at_commit"`. |
| AC19 | `consult` raises (e.g., coding agent unavailable) → attempt logged + `final_state="aborted_consult_failure"`; operator sees prose in next `/improvement_status`. |
| AC20 | `restart_self` fails → currently rare; attempt logged + `final_state="restart_failed"`. |

## Soak gate

1. **Automated**: ACs above via test fixtures that stub
   `consult` (returns spec text + STATUS markers
   deterministically) + the workspace + the receipts. No
   actual restart fired in tests.
2. **Operator soak**: invoke `improve_kernos` with a trivial
   spec ("add a one-line comment to README.md"); follow via
   `/improvement_status`; approve at commit gate; observe
   the loop complete + test pass.
3. **Failure soak**: trigger each documented failure mode
   (cap convergence, reject receipt, kill consult mid-call)
   and verify the final_state lands correctly.

## Out of scope (deferred to IMPROVEMENT-LOOP-RECOVERY-V1)

- Recovery cycles after test failure
- Mid-attempt restart-resume
- Auto-rebase on origin/main drift
- Per-step retry budgets

## Risks

- **Risk:** Background asyncio task spawned by
  `_run_attempt` outlives the calling request. If the task
  crashes silently, the ledger goes stale + the operator
  thinks the attempt is still running.
  - **Mitigation:** Wrap `_run_attempt` body in
    try/except/finally that ALWAYS appends a final event
    (success or failure path). Ledger inspection at boot
    detects orphaned attempts with no terminal event +
    surfaces them.

- **Risk:** Coding agents go off-rail and produce specs/code
  that diverge from the operator's intent.
  - **Mitigation:** Convergence requires BOTH author +
    reviewer GREEN (cross-validation). Operator gates the
    commit before any source modification reaches live.

- **Risk:** Receipt-bound commit creates a tight coupling
  between approval expiry + attempt liveness. If the
  operator takes >24h to approve, the receipt expires;
  the attempt is stuck.
  - **Mitigation:** Receipt TTL is documented; expiry
    surfaces in `/improvement_status` so the operator
    can re-trigger. Future spec adds re-issue.

## Dependencies

All shipped:
- IMPROVEMENT-WORKSPACE-V1 (`05c508d`)
- IMPROVEMENT-ATTEMPT-LEDGER-V1 (`ac04e74`)
- GIT-OPERATIONS-PRIMITIVES-V1 (`960ce0b`)
- IMPROVEMENT-REVIEW-PROTOCOL-V1 (`e9e1dc7`)
- SELF-TEST-GATE-V1 (`5c72c3b`)
- DURABLE-APPROVAL-RECEIPTS-V1 (`96f4582`)
- TOOL-REGISTRATION-AUTHORIZATION-V1 (`27f0352`) — receipt
  callback dispatch pattern that this spec extends.

## Migration

Additive. No schema change. New kernel tool + new module.
The bring-up hook for post-restart continuation is wired
into `bring_up_substrate.py` as a small additional pass.
