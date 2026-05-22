# SELF-TEST-GATE-V1

**Date:** 2026-05-22
**Status:** Draft for review (sub-spec #6 of `KERNOS-AUTONOMOUS-IMPROVEMENT-LOOP-V1`)
**Scope:** `run_self_test_suite` kernel tool + curated smoke
  test set + result writer to the improvement ledger. The
  orchestrator workflow (future) calls this after the autonomous-
  loop commit lands to verify the commit didn't regress core
  substrate invariants.
**Estimated size:** ~150 LOC source + ~80 LOC tests.

## Why this spec exists

Per parent spec D6: the full pytest suite has a known stall at
~74% (`[[pytest-full-suite-stall]]`). Running it as the first-
pass-GREEN gate after every autonomous commit would block for
hours and surface unrelated flakes. v1 ships a CURATED smoke
set focused on the invariants the autonomous loop must not
break. v2 lands the full-suite stall diagnosis.

## Design principles (load-bearing)

Per [[agent-facing-natural-simplicity]]: the agent receives a
short natural-prose summary of the test outcome ("all 47 smoke
tests passed in 8.3s" or "3 failed: test_X, test_Y, test_Z —
re-running with a fresh recovery cycle is the next step"). The
operator gets the full pytest output via the ledger event.

## Current state

- No `run_self_test_suite` kernel tool exists today.
- Tests run via `pytest tests/` from the shell — full-suite
  stall documented.
- IMPROVEMENT-ATTEMPT-LEDGER-V1 (shipped) provides the events
  table the result-writer uses.

## Design

### Curated smoke set

```python
_SMOKE_TEST_FILES: tuple[str, ...] = (
    # Liveness contract — recursive testing of the loop's own
    # substrate.
    "tests/test_self_controlled_loop_liveness.py",
    # Substrate-bringup provider injection (catches the silent
    # parallel-module bug class that bit us in May).
    "tests/test_substrate_bringup_providers.py",
    # Gateway-health heartbeat cross-check.
    "tests/test_gateway_health_observer.py",
    # Autonomous-loop end-to-end (will be added when the
    # orchestrator ships).
    # "tests/test_self_improvement_workflow.py",
)
```

The autonomous loop's orchestrator dynamically appends the
spec-under-test's own test file when calling
`run_self_test_suite` — that's how spec-specific ACs get
exercised alongside the smoke set.

### Tool surface

```python
RUN_SELF_TEST_SUITE_TOOL: dict = {
    "name": "run_self_test_suite",
    "description": (
        "Run Kernos's curated smoke test set against the "
        "improvement worktree. Returns a natural-prose summary "
        "of the outcome (pass/fail counts + failing test "
        "names). Full output writes to the ledger event row "
        "for operator inspection. Optional extra_test_paths "
        "let the orchestrator include the spec-under-test's "
        "own test file alongside the smoke set."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "workspace_dir": {"type": "string"},
            "attempt_id": {
                "type": "string",
                "description": (
                    "Attempt id this test run belongs to. "
                    "Result + outcome are appended to the "
                    "improvement_attempt_events table."
                ),
            },
            "extra_test_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Spec-specific test files to include "
                    "alongside the smoke set."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Test-run timeout. Default 120s.",
            },
        },
        "required": ["workspace_dir", "attempt_id"],
    },
}
```

Gate classification: `read` (no source mutations; pytest
might write artifacts under `data/` but doesn't change the
worktree's git state).

### Handler

```python
async def handle_run_self_test_suite(
    *, tool_input: dict, instance_id: str, data_dir: str,
) -> str:
    """Run pytest against the smoke set + any extra paths.
    Workspace-guarded. Records result to the ledger.

    Returns natural-prose summary for the agent."""
    # validate workspace
    # build pytest command: pytest <smoke_files> <extra_paths> -q
    # run with timeout
    # parse outcome
    # write event to ledger (kind="self_test_result",
    #   detail=summary string)
    # update improvement_attempts.test_outcome
    # return prose summary
```

### Ledger integration

On test result:
1. Append `improvement_attempt_events` row with
   `kind="self_test_result"`, `detail=<short summary>`.
2. Update `improvement_attempts.test_outcome` to
   `pass | fail | timeout`.
3. If pass on first cycle: also update
   `improvement_attempts.first_pass_green = 1` (only if not
   already set — recovery cycles don't re-set the flag).

### After-boot probe (parent spec D6)

A separate helper called at substrate bring-up: asserts that
`LOOP_HEALTH_EXECUTION_COMPLETED` event fires within 30s of
boot. v1 ships the helper; the bring-up wires it in (or
defers to the orchestrator if the orchestrator owns the
boot-probe contract).

For this sub-spec's scope: ship the helper signature + tests;
defer the actual bring-up wire-up to IMPROVEMENT-LOOP-WORKFLOW-V1
where it lives more naturally.

## Acceptance criteria

| AC | Description |
|---|---|
| AC1 | `run_self_test_suite` is in `_KERNEL_TOOLS` + has policy `frozenset({"confirmed"})`. |
| AC2 | Gate classifies as `read`. |
| AC3 | Tool returns prose summary on pass: "N smoke tests passed in T seconds". |
| AC4 | Tool returns prose summary on fail: "K failed: test_x, test_y, ..." (truncated if >5). |
| AC5 | Tool returns prose summary on timeout: "Tests didn't complete in T seconds." |
| AC6 | Tool rejects invalid workspace_dir via the guard's natural-prose. |
| AC7 | Tool writes `improvement_attempt_events` row with `kind="self_test_result"`. |
| AC8 | Tool updates `improvement_attempts.test_outcome`. |
| AC9 | First-cycle pass sets `first_pass_green=1`; subsequent passes don't reset it. |
| AC10 | `extra_test_paths` are included alongside the smoke set. |
| AC11 | Error responses are natural prose (no `{`, `}`, JSON markers). |

## Out of scope

- Full pytest suite — v2, after stall diagnosis.
- After-boot probe bring-up wiring — IMPROVEMENT-LOOP-WORKFLOW-V1.
- Test-result rendering in `/improvement_status` — already
  covered (ledger renderer surfaces events).

## Risks

- **Risk:** A smoke test file added in the worktree but not on
  origin/main means pytest can't find it.
  - **Mitigation:** Smoke files are paths relative to worktree
    root. Orchestrator runs pytest IN the worktree, so the
    worktree's filesystem is the lookup source.

- **Risk:** Timeout fires on a slow CI environment masking
  actual passing runs.
  - **Mitigation:** Default 120s; env-configurable via
    `KERNOS_SELF_TEST_TIMEOUT_SEC`. Failures surface clearly
    so operators can raise the limit if needed.

## Dependencies

- IMPROVEMENT-ATTEMPT-LEDGER-V1 (shipped) — events table +
  `append_event` + `update_attempt` helpers.
- IMPROVEMENT-WORKSPACE-V1 (shipped) — workspace guard.
- `pytest` available in the worktree's environment.

## Migration

Additive. No schema change. New kernel tool registered + new
classification entry; both additive.
