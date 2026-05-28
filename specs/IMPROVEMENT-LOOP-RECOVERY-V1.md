# IMPROVEMENT-LOOP-RECOVERY-V1

**Date:** 2026-05-28
**Status:** Buildable POC v1
**Scope:** Shape 1 only: test-failure recovery with
`surface_to_agent`.
**POC goal:** A clean zero-user portfolio demo where an autonomous
improvement lands, the post-restart self-test fails, Kernos wakes the
active agent with the failure, the agent chooses recovery or abandon,
and at most two approval-gated fix-up commits are attempted.
**Estimated size:** ~250 LOC source + ~180 LOC tests.

## Buildability Check

Verified against shipped code:

- `ImprovementLoopOrchestrator.start_attempt` creates the attempt,
  worktree, and background task at `kernos/kernel/improvement_loop_workflow.py:124`.
  `_run_attempt` runs spec, impl, then calls `_request_commit_approval`
  at `kernos/kernel/improvement_loop_workflow.py:215` and
  `kernos/kernel/improvement_loop_workflow.py:273`. It does not yet
  continue after approval.
- `_restart_fn` is accepted and stored, but not used:
  `kernos/kernel/improvement_loop_workflow.py:109` and
  `kernos/kernel/improvement_loop_workflow.py:116`.
- The real ledger has three tables and no typed current-state column:
  `improvement_attempts.final_state`, commit rows, and append-only
  event `kind` strings live at `kernos/kernel/instance_db.py:208`,
  `kernos/kernel/instance_db.py:232`, and
  `kernos/kernel/instance_db.py:245`.
- Ledger mutation is through `update_attempt`,
  `append_event`, and `record_commit`:
  `kernos/kernel/improvement_ledger.py:79`,
  `kernos/kernel/improvement_ledger.py:130`, and
  `kernos/kernel/improvement_ledger.py:174`.
- Existing shipped event kinds in the orchestrator are
  `workspace_created`, `spec_iteration`, `impl_iteration`,
  `approval_requested`, and `attempt_failed`:
  `kernos/kernel/improvement_loop_workflow.py:186`,
  `kernos/kernel/improvement_loop_workflow.py:360`,
  `kernos/kernel/improvement_loop_workflow.py:432`,
  `kernos/kernel/improvement_loop_workflow.py:505`, and
  `kernos/kernel/improvement_loop_workflow.py:297`.
- The self-test gate exists as `run_self_test_suite`; it validates an
  improvement worktree, appends `self_test_result`, updates
  `test_outcome`, and sets `first_pass_green=1` only if it is still
  NULL on a pass:
  `kernos/kernel/self_test_gate.py:565`,
  `kernos/kernel/self_test_gate.py:625`,
  `kernos/kernel/self_test_gate.py:754`, and
  `kernos/kernel/self_test_gate.py:760`.
- Durable approval receipts already support
  `git_commit_authorization`: requests are created at
  `kernos/kernel/approval_receipts.py:105`; `/approve ... CONFIRM`
  has a post-approval callback seam by receipt kind at
  `kernos/messages/handler.py:5374`.
- `git_commit` and `git_push` enforce the existing approval gate and
  receipt binding at `kernos/kernel/git_operations.py:410` and
  `kernos/kernel/git_operations.py:583`. They do not write the
  improvement ledger today.
- Agent-callable kernel tools are registered canonically by schema
  import in `kernos/kernel/kernel_tool_registry.py:95`, dispatcher
  inventory and paths in `kernos/kernel/reasoning.py:723`,
  `kernos/kernel/reasoning.py:738`, and
  `kernos/kernel/reasoning.py:832`, `execute_tool` at
  `kernos/kernel/reasoning.py:986`, concrete handler branches near
  `kernos/kernel/reasoning.py:1565`, and gate classification at
  `kernos/kernel/gate.py:323`.
- Tool-surfacing starts from `kernel_tool_schema_map` at
  `kernos/messages/phases/assemble.py:503`, always-pins tools at
  `kernos/messages/phases/assemble.py:555`, and catalog-scans the
  remaining tools at `kernos/messages/phases/assemble.py:648`.
- Synthetic agent wake turns already exist for consult completion via
  `NormalizedMessage` injection at `kernos/messages/handler.py:4711`;
  the recovery surface should use that pattern.

Buildability conclusion: the post-restart self-test failure path and
`awaiting_recovery_decision` state do not exist yet. V1 must add the
minimal approval/restart/test continuation plus Shape 1 recovery.

## In Scope

- Default and only recovery mode: `surface_to_agent`.
- Trigger: a post-restart `run_self_test_suite` result with
  `test_outcome != "pass"` for an improvement attempt.
- Two agent-callable tools:
  `proceed_with_recovery(attempt_id)` and
  `abandon_attempt(attempt_id, reason)`.
- Conditional tool surfacing: these two tools are visible only when
  the active space has an attempt in `awaiting_recovery_decision`.
- Recovery iteration cap: 2 accepted `proceed_with_recovery` calls per
  attempt.
- Recovery commits reuse the existing `git_commit_authorization`
  receipt gate. No source mutation bypasses approval.
- Operator override uses the same substrate service functions as the
  agent tools.

## Out Of Scope

- Security hardening, sandboxing, and defense-in-depth changes.
- Mode flags (`auto`, `surface_to_operator`, `off`).
- New workflow engine primitives.
- Auto-rebase, transient retry budgets, mid-attempt restart-resume,
  bridge sentinel work, and architect-bound escalation.

## V1 Flow

### 0. Minimal continuation needed for the POC

The shipped loop currently stops after `approval_requested`. V1 adds
the missing continuation so recovery has a real trigger:

1. Extend the existing `/approve` receipt-kind callback seam for
   `kind="git_commit_authorization"`.
2. Parse the receipt binding payload for `attempt_id`,
   `workspace_dir`, `expected_parent_sha`, and `expected_diff_hash`.
3. Run the existing `git_commit` and `git_push` handlers with the
   approved receipt.
4. Write a commit row with `record_commit`; for recovery commits set
   `recovery_trigger="post_restart_self_test_failed"`.
5. Append `commit_recorded` and `push_succeeded` events.
6. Set `final_state="awaiting_post_restart_test"` and invoke the
   existing restart function.
7. On bring-up, scan attempts with
   `final_state="awaiting_post_restart_test"` and run
   `run_self_test_suite` against the attempt worktree with
   `include_soak=True`.

The POC uses the existing worktree path because
`run_self_test_suite` requires a workspace under
`data/<instance>/improvement_workspace/`.

### 1. Failed post-restart self-test

When the bring-up continuation sees `test_outcome != "pass"`:

1. If this is the first failed post-restart test for the attempt, set
   `first_pass_green=0` before any recovery pass can later set it to
   1.
2. If fewer than 2 recovery iterations have started, set
   `final_state="awaiting_recovery_decision"`.
3. Append `recovery_decision_requested` with JSON detail containing
   `attempt_id`, `failure_summary`, `failed_test_ids` if available,
   `worktree_path`, and `recovery_iterations_used`.
4. Inject a synthetic system turn into the origin space using the
   existing wake-turn pattern. The message tells the agent the test
   failed and that exactly two tools are available:
   `proceed_with_recovery` or `abandon_attempt`.

Origin space/member are recorded at attempt start as an
`attempt_origin` ledger event. This avoids a schema migration while
keeping the wake target durable across restart.

If two recovery iterations have already started, set
`final_state="test_failed_unrecovered"`, set `ended_at`, and append
`recovery_cap_hit`.

### 2. `proceed_with_recovery(attempt_id)`

The tool accepts only attempts whose `final_state` is
`awaiting_recovery_decision`.

On success:

1. Count prior `recovery_started` events. If the count is already 2,
   close the attempt as `test_failed_unrecovered`.
2. Set `final_state="recovery_in_progress"`.
3. Append `recovery_started` with JSON detail
   `{ "iteration": N, "trigger": "post_restart_self_test_failed" }`.
4. Spawn a bounded fix-up cycle against the existing worktree:
   call the original `primary_coding_agent` with the failure summary,
   failed tests, spec requirement, worktree path, and instruction to
   edit the worktree and end with `STATUS: GREEN` or
   `STATUS: NEEDS_REVISION <reason>`.
5. Append `recovery_iteration` with `iteration`, `agent`, and
   `outcome`.
6. If the recovery consult is not GREEN, set
   `final_state="test_failed_unrecovered"`, set `ended_at`, and append
   `recovery_aborted_unconverged`.
7. If GREEN, issue the same `git_commit_authorization` approval as
   the original attempt, with binding payload fields
   `attempt_id`, `workspace_dir`, `expected_parent_sha`,
   `expected_diff_hash`, and `recovery_iteration=N`.
8. Append `approval_requested` with `recovery_iteration=N`. The
   attempt waits for operator approval exactly like the first commit.

After approval, the continuation path commits, pushes, restarts, and
runs the post-restart self-test again. A pass closes the attempt as
`completed`; a failure returns to `awaiting_recovery_decision` unless
the cap is exhausted.

### 3. `abandon_attempt(attempt_id, reason)`

The tool accepts only attempts whose `final_state` is
`awaiting_recovery_decision`.

On success:

- Set `final_state="test_failed_abandoned_by_agent"`.
- Set `ended_at`.
- Append `test_failed_abandoned_by_agent` with the provided reason.
- Do not spawn a coding agent and do not request approval.

## Agent Tools

Define the schemas next to `IMPROVE_KERNOS_TOOL` in
`kernos/kernel/improvement_loop_workflow.py`.

`proceed_with_recovery` schema:

```json
{
  "name": "proceed_with_recovery",
  "input_schema": {
    "type": "object",
    "properties": {
      "attempt_id": { "type": "string" }
    },
    "required": ["attempt_id"],
    "additionalProperties": false
  }
}
```

`abandon_attempt` schema:

```json
{
  "name": "abandon_attempt",
  "input_schema": {
    "type": "object",
    "properties": {
      "attempt_id": { "type": "string" },
      "reason": { "type": "string" }
    },
    "required": ["attempt_id", "reason"],
    "additionalProperties": false
  }
}
```

Registration requirements:

- Import both schemas in `kernel_tool_registry._import_kernel_schemas`
  and include them in the schema list.
- Add both names to `ReasoningService._KERNEL_TOOLS`,
  `_DISPATCHABLE_KERNEL_TOOLS`, and `_KERNEL_TOOL_PATHS` with
  `frozenset({"confirmed"})`.
- Add concrete `execute_tool` branches that call handlers in
  `improvement_loop_workflow.py`.
- Classify both as `soft_write` in `DispatchGate.classify_tool_effect`.
  They mutate the ledger and may spawn a consult, but source mutation
  still waits for the existing commit approval gate.
- Do not add either tool to `ALWAYS_PINNED`.

## Conditional Surfacing

The tools are registered globally for dispatch parity but surfaced
only during recovery decision turns.

Implement a small surfacing helper used by assemble:

1. Before pinned/active selection, check whether the current
   `instance_id` and active space have an attempt whose
   `final_state="awaiting_recovery_decision"` and whose
   `attempt_origin` event targets that space.
2. If no such attempt exists, remove `proceed_with_recovery` and
   `abandon_attempt` from the local `_kernel_tool_map` so neither
   pinned nor catalog scan can surface them.
3. If such an attempt exists, force-add both schemas to the active
   tool list for this turn.

This keeps dispatch registered while ensuring the agent only sees
the decision tools when there is an actual recovery decision to make.

## Ledger Contract

No new tables. No new columns.

Use the existing APIs:

- `update_attempt(... final_state=...)`
- `append_event(... kind=..., detail=...)`
- `record_commit(... recovery_trigger=...)`

Existing states/events remain unchanged.

New `final_state` values:

- `awaiting_post_restart_test` - nonterminal; commit pushed and
  restart requested.
- `awaiting_recovery_decision` - nonterminal; tools may surface.
- `recovery_in_progress` - nonterminal; fix-up consult is running.
- `completed` - terminal; final post-restart self-test passed.
- `test_failed_unrecovered` - terminal; failure remained after cap or
  fix-up could not converge.
- `test_failed_abandoned_by_agent` - terminal; agent chose abandon.

New event kinds:

- `attempt_origin`
- `commit_recorded`
- `push_succeeded`
- `recovery_decision_requested`
- `recovery_started`
- `recovery_iteration`
- `recovery_aborted_unconverged`
- `recovery_cap_hit`
- `test_failed_abandoned_by_agent`
- `operator_recovery_override`

Recovery iteration count is the count of `recovery_started` events for
the attempt.

## Dependencies

- Orchestrator and tool handlers:
  `kernos/kernel/improvement_loop_workflow.py:92`,
  `kernos/kernel/improvement_loop_workflow.py:451`,
  `kernos/kernel/improvement_loop_workflow.py:559`.
- Ledger schema and helpers:
  `kernos/kernel/instance_db.py:208`,
  `kernos/kernel/improvement_ledger.py:79`,
  `kernos/kernel/improvement_ledger.py:130`,
  `kernos/kernel/improvement_ledger.py:174`.
- Worktree path and branch ownership:
  `kernos/kernel/improvement_workspace.py:94`,
  `kernos/kernel/improvement_workspace.py:100`,
  `kernos/kernel/improvement_workspace.py:107`.
- Self-test gate:
  `kernos/kernel/self_test_gate.py:420`,
  `kernos/kernel/self_test_gate.py:565`,
  `kernos/kernel/self_test_gate.py:754`.
- Durable approval receipts and approval callback seam:
  `kernos/kernel/approval_receipts.py:105`,
  `kernos/messages/handler.py:5296`,
  `kernos/messages/handler.py:5374`.
- Git commit/push approval gate:
  `kernos/kernel/git_operations.py:410`,
  `kernos/kernel/git_operations.py:583`.
- Tool registration, dispatch, classification, and parity:
  `kernos/kernel/kernel_tool_registry.py:95`,
  `kernos/kernel/reasoning.py:723`,
  `kernos/kernel/reasoning.py:738`,
  `kernos/kernel/reasoning.py:832`,
  `kernos/kernel/reasoning.py:986`,
  `kernos/kernel/reasoning.py:1565`,
  `kernos/kernel/gate.py:323`,
  `kernos/kernel/gate.py:577`,
  `tests/test_kernel_tool_registry_parity.py:57`,
  `tests/test_kernel_tool_dispatch_paths.py:181`.
- Conditional surfacing and synthetic wake:
  `kernos/messages/phases/assemble.py:503`,
  `kernos/messages/phases/assemble.py:648`,
  `kernos/messages/handler.py:4711`.

## Acceptance Criteria

| AC | Description |
|---|---|
| AC1 | Approving a `git_commit_authorization` for an improvement attempt commits, pushes, records `commit_recorded`/`push_succeeded`, records a commit row, sets `final_state="awaiting_post_restart_test"`, and calls the restart function. |
| AC2 | Bring-up scans `awaiting_post_restart_test`, runs `run_self_test_suite`, and on failure sets `first_pass_green=0`, sets `final_state="awaiting_recovery_decision"`, appends `recovery_decision_requested`, and injects a synthetic agent wake. |
| AC3 | `proceed_with_recovery` and `abandon_attempt` are registered through the canonical registry, dispatch, path, and gate-classification surfaces; existing parity tests fail if any registration surface is missed. |
| AC4 | The two recovery tools are absent from normal turns and force-surfaced only when the active space owns an `awaiting_recovery_decision` attempt. |
| AC5 | `proceed_with_recovery` rejects unknown attempts, wrong-state attempts, and cap-exhausted attempts with prose; on a valid attempt it appends `recovery_started` and runs one fix-up cycle. |
| AC6 | A GREEN recovery cycle issues a new `git_commit_authorization`; no recovery commit or push occurs until the operator approves that receipt. |
| AC7 | A recovery commit records `recovery_trigger="post_restart_self_test_failed"` and then reuses the same restart plus post-restart self-test path. |
| AC8 | After two started recovery iterations, another post-restart test failure closes the attempt with `final_state="test_failed_unrecovered"` and appends `recovery_cap_hit`. |
| AC9 | `abandon_attempt(attempt_id, reason)` closes an awaiting attempt with `final_state="test_failed_abandoned_by_agent"` and appends the reason; it does not spawn consult or request approval. |
| AC10 | Operator override calls the same proceed/abandon service functions and appends `operator_recovery_override` before the shared action event. |

## Test Plan

- Extend `tests/test_improvement_loop_workflow.py` with:
  approval continuation, post-restart test pass, post-restart test
  failure, recovery proceed, recovery abandon, and cap exhaustion.
- Extend ledger tests to pin new event names, `final_state` values, and
  recovery iteration counting from `recovery_started`.
- Extend tool registry/dispatch parity tests only as required by the
  new tool names; existing tests should catch missed surfaces.
- Add a surfacing unit test that assembles a normal turn and an
  `awaiting_recovery_decision` turn, asserting the tools are absent in
  the former and present in the latter.
- Add a handler test for the `git_commit_authorization` approval
  callback path that stubs `git_commit`, `git_push`, and `restart_fn`.
- Add a synthetic wake test modeled on `inject_consult_completion_wake`
  that verifies the recovery message queues a turn in the origin
  space.
- Add a demo-style integration test using stubbed consult and
  self-test outcomes: fail initial test, agent proceeds, recovery
  commit is approval-gated, second self-test passes, final state is
  `completed`.

## Deferred To V2

- Shape 2: mid-attempt restart-resume.
- Shape 3: auto-rebase on origin drift.
- Shape 4: transient consult retries.
- Shape 5: bridge sentinel reset formalization.
- Shape 6: architect-bound escalation.
