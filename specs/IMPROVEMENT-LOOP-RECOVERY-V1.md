# IMPROVEMENT-LOOP-RECOVERY-V1

**Date:** 2026-05-28
**Status:** Draft for architect review (sub-spec #8 — recovery —
  of `KERNOS-AUTONOMOUS-IMPROVEMENT-LOOP-V1`; parked at parent
  closure 2026-05-22 awaiting operational evidence)
**Scope:** Four explicit scope cuts deferred from
  IMPROVEMENT-LOOP-WORKFLOW-V1, with two additional recovery
  shapes surfaced by USER-INITIATED-IMPROVEMENT-TRIGGER-V1's
  shape (architectural-bound completion failures, gate-pending
  bridge sentinel drift). All four shipped behind feature
  flags so the operator can audit each recovery surface in
  isolation before promoting to default-on.
**Estimated size:** ~450 LOC source + ~350 LOC tests.

## Why this spec exists

Per IMPROVEMENT-LOOP-WORKFLOW-V1 closure: v1 explicitly ships
the happy path + simple terminal failure paths. Four recovery
shapes were deferred for two reasons:

1. **Evidence-first.** The right shape of each recovery cycle
   depends on how the loop actually fails in practice — not on
   pre-spec speculation.
2. **Trust gradient.** Recovery shapes that auto-spawn coding
   agents on previously-failed work require operator trust
   that v1 hasn't yet earned.

The 2026-05-26 → 2026-05-28 operator-soak window produced
enough live evidence to inform each shape. Five attempts
through the `/fix` surface (USER-INITIATED-IMPROVEMENT-TRIGGER-
V1) surfaced:

- 0 mid-attempt crashes that needed resume (the loop completed
  end-to-end on every well-formed attempt)
- 2 attempts that aborted at `validate_investigation_response`
  due to bridge-schema drift — fixed by
  BRIDGE-RESPONSE-SCHEMA-V1 (commit 8af7f9a's predecessor),
  not by recovery
- 1 attempt that stuck at the gate after Kernos crash because
  the bridge sentinel was claimed pre-crash and the bridge
  wouldn't re-emit on resume — fixed by execution-engine
  sentinel-reset (commit 9916fa0)
- 0 push-time drift events (single-developer instance)
- 0 transient `consult` failures (Codex was stable across all
  five attempts)

Surfacing this means v1 of recovery doesn't need to predict
shapes — it can ship targeted handlers for the four originally-
deferred items, plus formalize the two operational fixes that
already shipped ad-hoc.

## v1 happy path (what recovery looks like end-to-end)

**Test failure recovery path (the most-common recovery shape):**

1. Post-restart bring-up hook runs `run_self_test_suite`.
2. Ledger event `self_test_result` written with `passed=False`,
   `failure_summary`, `failed_test_ids[]`, `diff_at_failure`.
3. **Recovery is automatic only if** `improvement_recovery_mode`
   covenant resolves to `"auto"`; default is `"surface_only"`
   (operator gets a prose surface + `/recover <attempt_id>` to
   opt in).
4. On auto: orchestrator spawns the REVIEWER agent (not the
   original primary — fresh eyes per [[multi-round-codex-
   convergence]]) with: `failing_test_ids`, the diff that
   landed, the spec, the original failure_summary.
5. Reviewer returns either:
   - A diff that fixes the test (success path) → mints a new
     approval gate (`git_recovery_authorization` receipt
     type); operator approves → commit + push + restart →
     post-restart test runs again.
   - "I can't fix this — needs human" (abort path) → final_state
     set to `test_failed_unrecovered`, attempt closed.
6. Recovery iterations are capped at `2` (post-fix, post-fix-
   of-fix). On the second failure: final_state set to
   `test_failed_unrecovered`; loop closes.

## The four recovery shapes (each behind a flag)

### Shape 1: Test failure recovery (default off → operator opt-in)

**Flag:** `IMPROVEMENT_RECOVERY_TEST_FAILURE`
  (env: `KERNOS_IMPROVEMENT_RECOVERY_TEST_FAILURE`,
  covenant: `improvement.test_failure_recovery`).
  Default: `surface_only`. Values: `auto`, `surface_only`, `off`.

**Trigger:** `self_test_result.passed=False` from post-restart hook.

**Action:**
- `surface_only`: write whisper to operator with the failure
  summary + a `/recover <attempt_id>` instruction; mark
  attempt `awaiting_recovery_decision`; pause until operator
  invokes `/recover` (auto) or `/abandon` (closes attempt).
- `auto`: directly spawn reviewer agent with failure context;
  proceed through bounded iterations as above.
- `off`: skip recovery entirely; mark final_state
  `test_failed_unrecovered`.

**Bounds:**
- Max 2 recovery iterations per attempt.
- Each recovery commit goes through the full approval gate
  (operator still gates every source mutation).

**Ledger events added:**
- `recovery_started` (recovery_reason, recovery_mode)
- `recovery_iteration` (iteration_num, agent, outcome)
- `recovery_converged` (final_diff_sha)
- `recovery_aborted_unconverged` (iteration_cap_hit)
- `recovery_abandoned_by_operator` (via /abandon)

### Shape 2: Mid-attempt restart-resume (default surface-only)

**Flag:** `IMPROVEMENT_RECOVERY_MID_ATTEMPT_RESUME`
  (env: `KERNOS_IMPROVEMENT_RESUME_MID_ATTEMPT`).
  Default: `surface_only`. Values: `auto`, `surface_only`, `off`.

**Trigger:** Bring-up hook scans ledger for attempts with:
- `final_state IS NULL`
- Last event's timestamp > 60s before current boot time
  (i.e., the attempt didn't write a terminal event before the
  crash)

**Action per detected attempt:**
- Determine resume point by walking the last 10 events:
  - `spec_iteration` (no `spec_converged`/`spec_aborted`) →
    **resume_point=spec_cycle** (re-enter from last iteration's
    state if saved; otherwise abandon)
  - `spec_converged` (no `impl_iteration`) →
    **resume_point=impl_cycle_start**
  - `impl_iteration` (no `impl_converged`/`impl_aborted`) →
    **resume_point=impl_cycle** (worktree state may be partial)
  - `impl_converged` (no `approval_requested`) →
    **resume_point=approval_gate**
  - `approval_requested` (no `commit_recorded`) →
    **resume_point=awaiting_approval** (re-bind orchestrator's
    continuation; nothing else needed, /approve still works)
  - `commit_recorded` (no `push_succeeded`) →
    **resume_point=push** (re-push idempotently)
  - `push_succeeded` (no restart-related event) →
    **resume_point=restart_self**
- On `surface_only`: write whisper listing detected attempts +
  resume_points + `/resume <attempt_id>` instruction; mark
  attempts `in_flight_at_crash`; await operator decision.
- On `auto`: re-enter at the detected resume_point IFF the
  resume_point is safe (spec_cycle, impl_cycle_start,
  approval_gate, awaiting_approval, push, restart_self).
  Unsafe points (mid-impl_cycle: worktree may be inconsistent)
  always surface for operator decision regardless of mode.

**Safety:**
- `impl_iteration` mid-flight is treated as unsafe (worktree
  could have partial changes from the coding agent that
  weren't committed). v1 ALWAYS surfaces for operator
  decision on these; auto-resume never fires here.
- A resumed attempt re-uses the original `attempt_id` and
  the original worktree (which IMPROVEMENT-WORKSPACE-V1
  preserves across restarts via the per-attempt-id branch).

**Ledger events added:**
- `attempt_resumed_post_crash` (resume_point, mode)
- `attempt_abandoned_post_crash` (resume_point, reason)
- `attempt_awaiting_resume_decision` (resume_point)

### Shape 3: Auto-rebase on origin/main drift (default off)

**Flag:** `IMPROVEMENT_RECOVERY_AUTO_REBASE`
  (env: `KERNOS_IMPROVEMENT_AUTO_REBASE`).
  Default: `off`. Values: `auto`, `surface_only`, `off`.

**Trigger:** `git_push` raises with
  `improvement.head_drifted_pre_push` (non-fast-forward error
  surfaced by GIT-OPERATIONS-PRIMITIVES-V1).

**Action:**
- `off`: surface push failure to operator via prose
  (`/improvement_status` shows the drift); operator resolves
  manually; attempt closes with `final_state=push_failed_drift`.
- `surface_only`: identical to `off` but with explicit
  `/rebase_and_retry <attempt_id>` instruction in the prose.
- `auto`:
  1. `git fetch origin main`
  2. `git rebase origin/main` on the improvement branch
  3. If conflict: surface to operator with conflict files
     list; mark attempt `rebase_conflict`; bail.
  4. If clean: re-run `run_self_test_suite` in worktree
     (covenant-driven; suite-required by default for
     auto-rebase). If green: re-push. If red: surface to
     operator; mark `rebase_self_test_failed`.

**Bounds:**
- Max 1 auto-rebase per attempt (no rebase-of-rebase). A
  second drift event closes the attempt with
  `push_failed_drift_twice`.

**Ledger events added:**
- `auto_rebase_started` (origin_main_sha_before)
- `auto_rebase_succeeded` (origin_main_sha_after)
- `auto_rebase_conflict` (conflict_files[])
- `rebase_self_test_failed` (failed_test_ids[])

### Shape 4: Per-step retry budgets (default on, conservative)

**Flag:** `IMPROVEMENT_RECOVERY_TRANSIENT_RETRIES`
  (env: `KERNOS_IMPROVEMENT_RETRY_TRANSIENT`).
  Default: `on`. Values: `on`, `off`.

**Trigger:** Any `consult` call inside the loop raises with
a class of exception classified as **transient** (per
`AGENT_CALL_TRANSIENT_RETRY_V1`'s existing transient-error
catalog: connection refused, ECONNRESET, HTTP 502/503/504,
asyncio.TimeoutError).

**Action:**
- Retry the same `consult` up to 2 times with exponential
  backoff: 5s, then 30s.
- Each retry writes a `consult_retry_attempted` event with
  the iteration_num, error_class, backoff_sec.
- If all retries exhausted: classify as terminal; attempt
  fails with `final_state=aborted_consult_failure` (existing
  AC19 path, now with `retries_exhausted=True` marker).

**Non-transient errors** (CodingAgentTimeout from a true
hang, ContextWindowExceeded, AuthFailed, etc.) skip retry
entirely and go straight to AC19 termination.

**Bounds:**
- 2 retries × 2 calls per round × ~4 rounds = ~16 retries
  worst-case per attempt. Hard cap of 10 total retries per
  attempt across all consult calls; cap exceeded → attempt
  closes with `final_state=aborted_retry_cap`.

**Ledger events added:**
- `consult_retry_attempted` (iteration, error_class, backoff_sec)
- `consult_retry_exhausted` (final_error_class)
- `consult_retry_cap_hit` (total_retries_consumed)

## Two operational fixes that already shipped ad-hoc (formalize here)

### Shape 5: Bridge sentinel reset for gate-pending attempts

**Already implemented:** commit `9916fa0` —
`ExecutionEngine._reset_bridge_sentinel_for_gate` walks
pending-gate executions at `restart_resume` and deletes
the `*.emitted` sentinel so the bridge re-emits naturally.

**Recovery-spec formalization:**
- The bring-up hook MUST run this reset before the workflow
  runtime starts processing events (sequencing: sentinel
  reset → runtime start → ledger scan for `awaiting_recovery_*`).
- A regression test pins the ordering against the bring-up
  registry.

**Ledger events added (for telemetry parity with Shape 2):**
- `bridge_sentinel_reset_post_crash` (request_id, gate_id)

### Shape 6: Architectural-bound completion failure

**Trigger:** Bridge response carries `investigation_outcome=
architectural_bound_completion` or
`investigation_outcome=requires_architect_input` (CC determined
the asked work needs architect-level decision before
proceeding).

**Action:**
- Loop ALWAYS surfaces to operator. No auto-recovery is
  legal — by definition the work needs architect input.
- Mark attempt `awaiting_architect_input`; closed in
  ledger with terminal event `attempt_escalated_to_architect`.
- The operator-facing prose includes CC's summary of WHY it
  bounded out (the bridge response's `summary` field) +
  `/abandon <attempt_id>` instruction.

**Recovery-spec formalization:**
- This case already routes through the bridge today (CC writes
  the outcome; bridge surfaces it). v1 of recovery just adds
  the named final_state + the structured ledger event so
  telemetry can distinguish "architect-bound" from other
  abort modes.

**Ledger events added:**
- `attempt_escalated_to_architect` (cc_summary, related_pattern_id)

## Tool surface

### `/recover <attempt_id>` slash command

Operator opt-in for Shape 1 when mode is `surface_only`.

Parameters:
- `attempt_id` (positional, required).

Behavior:
- Validates `attempt_id` exists + is in `awaiting_recovery_
  decision` state. If not: prose error.
- Loads the attempt's failure_summary + failed_test_ids from
  the ledger. Calls `IMPROVementLoopOrchestrator.recover_
  test_failure(attempt_id)`.
- Returns prose: "Recovery started for attempt
  {attempt_id} — reviewer will analyze the failure and
  propose a fix. I'll ping you for approval when ready."

### `/abandon <attempt_id>` slash command

Operator opt-out for Shape 1 (`awaiting_recovery_decision`),
Shape 2 (`in_flight_at_crash`), and Shape 6
(`awaiting_architect_input`).

Parameters:
- `attempt_id` (positional, required).
- `reason` (optional free text).

Behavior:
- Validates `attempt_id` exists + is in an abandonable state.
- Calls `IMPROVementLoopOrchestrator.abandon(attempt_id,
  reason)`.
- Marks final_state appropriately (`abandoned_by_operator_*`).

### `/resume <attempt_id>` slash command

Operator opt-in for Shape 2 (`in_flight_at_crash`).

Parameters:
- `attempt_id` (positional, required).

Behavior:
- Validates state. Calls `IMPROVementLoopOrchestrator.
  resume_post_crash(attempt_id)`.
- Returns prose with the resume_point + what happens next.

### `/rebase_and_retry <attempt_id>` slash command

Operator opt-in for Shape 3 when mode is `surface_only`.

Parameters:
- `attempt_id` (positional, required).

Behavior:
- Validates the attempt is in `push_failed_drift` state.
- Calls `IMPROVementLoopOrchestrator.rebase_and_retry(
  attempt_id)`.

### No new agent-callable tools

All four shapes are operator-surface only. The agent never
gets recovery tools — recovery is operator-gated by design.

## Architecture

```
kernos/kernel/improvement_loop_recovery.py
  - class ImprovementLoopRecovery:
      # All methods take (attempt_id, instance_id) and return
      # async results that the orchestrator awaits.

      async def detect_post_crash_attempts() -> list[CrashedAttempt]
      async def resume_post_crash(attempt_id, mode) -> None
      async def abandon(attempt_id, reason) -> None

      async def recover_test_failure(attempt_id) -> None
      # Called by post-restart bring-up hook when test fails.

      async def rebase_and_retry(attempt_id) -> None
      # Called by orchestrator's push-step error handler.

      async def retry_consult(call_fn, *args, **kwargs) -> Any
      # Wrapped around every consult call in the orchestrator.
      # Returns the consult result or raises after exhaustion.
```

Recovery lives in its own module — the orchestrator imports
it and calls into it at the four trigger sites. Keeps the
orchestrator focused on happy-path composition; recovery
shape stays auditable in isolation.

The orchestrator's resume entry points (`_run_attempt`,
`continue_after_approval`, `continue_after_restart`) all gain
a small dispatcher that consults the ledger for the resume
point and routes accordingly.

## Acceptance criteria

### Shape 1: Test failure recovery

| AC | Description |
|---|---|
| AC1 | When mode=`surface_only` and test fails: whisper sent to operator with `/recover <id>` + `/abandon <id>`; attempt state = `awaiting_recovery_decision`. |
| AC2 | When mode=`auto` and test fails: reviewer agent spawned with failure context; first iteration written to ledger. |
| AC3 | Reviewer success path: new approval receipt issued; commit + push + restart fire; second post-restart test runs. |
| AC4 | Recovery iteration cap (2) enforced; on 3rd failure: `final_state=test_failed_unrecovered`. |
| AC5 | Reviewer "can't fix" path: `final_state=test_failed_unrecovered`; whisper with explanation. |
| AC6 | `/recover <id>` invalid state: prose error explaining current state. |
| AC7 | Recovery commits go through full approval gate (cannot skip). |

### Shape 2: Mid-attempt restart-resume

| AC | Description |
|---|---|
| AC8 | Bring-up scan detects all attempts with `final_state IS NULL` + last event >60s before boot. |
| AC9 | Resume-point classification correctly maps each terminal-event-state to one of the 7 resume points (test fixture per state). |
| AC10 | Mode=`surface_only` writes whisper listing each attempt + resume_point + `/resume` + `/abandon` instructions. |
| AC11 | Mode=`auto` resumes only safe resume_points (5 of 7); always surfaces unsafe (`spec_cycle`, `impl_cycle`). |
| AC12 | Resumed attempt re-uses original `attempt_id` + original worktree branch. |
| AC13 | `awaiting_approval` resume: orchestrator re-binds continuation; existing `/approve` still works. |
| AC14 | `push` resume: idempotent re-push (skip if local + remote SHAs already match). |
| AC15 | Worktree-corruption detection: if `impl_cycle` resume_point + worktree shows uncommitted changes, ALWAYS surfaces (never auto-resumes) regardless of mode. |

### Shape 3: Auto-rebase

| AC | Description |
|---|---|
| AC16 | Push raises with `head_drifted_pre_push` → mode dispatch fires (off / surface_only / auto). |
| AC17 | Mode=`auto`: fetch + rebase happen in worktree (not main checkout). |
| AC18 | Mode=`auto`: rebase conflict → `final_state=rebase_conflict`; whisper with conflict files. |
| AC19 | Mode=`auto`: rebase clean + self-test green → re-push fires. |
| AC20 | Mode=`auto`: rebase clean + self-test red → `final_state=rebase_self_test_failed`. |
| AC21 | Second drift after auto-rebase → `final_state=push_failed_drift_twice` (no rebase-of-rebase). |

### Shape 4: Transient retries

| AC | Description |
|---|---|
| AC22 | Transient error class triggers retry; non-transient does not. |
| AC23 | Backoff sequence is 5s then 30s (test with mocked sleep). |
| AC24 | Total retries cap (10/attempt) enforced; cap-hit → `final_state=aborted_retry_cap`. |
| AC25 | Ledger event `consult_retry_attempted` per retry with correct iteration + error_class. |
| AC26 | When flag=`off`: first error terminates (no retry attempt). |

### Shape 5: Bridge sentinel reset (regression-pin only)

| AC | Description |
|---|---|
| AC27 | Bring-up registry test pins ordering: `_reset_bridge_sentinel_for_gate` runs before workflow runtime starts. |
| AC28 | Sentinel reset emits `bridge_sentinel_reset_post_crash` event per pending-gate execution detected. |

### Shape 6: Architect-bound

| AC | Description |
|---|---|
| AC29 | Bridge response with `investigation_outcome=architectural_bound_completion` → `final_state=awaiting_architect_input`; whisper sent. |
| AC30 | `/abandon` works for `awaiting_architect_input` state. |
| AC31 | `attempt_escalated_to_architect` ledger event includes the CC summary + related_pattern_id (when present). |

### Cross-shape

| AC | Description |
|---|---|
| AC32 | All recovery modes default per spec (Shape 1: `surface_only`, Shape 2: `surface_only`, Shape 3: `off`, Shape 4: `on`). |
| AC33 | Covenant + env var both surface each flag's value; covenant takes precedence (covenants are operator-set, env vars are fallback). |
| AC34 | `/improvement_status <id>` shows recovery state for every shape (current state + last 5 recovery events). |
| AC35 | Each recovery shape's ledger event additions documented in `docs/improvement-attempt-ledger.md` schema section. |

## Soak gate

1. **Automated**: ACs above via test fixtures. Recovery path
   tests stub `consult` to return scripted reviewer-fix diffs
   or "can't fix" prose; push-drift tests use a temp git repo
   with a second clone forcing the drift; retry tests use
   monkeypatch on the consult function.
2. **Operator soak per shape (separate sessions, each behind
   its flag)**:
   - **Shape 1**: trigger a known-bad commit (e.g., add a
     test that asserts `False`); approve through the loop;
     post-restart test fails; opt into `/recover`; observe
     reviewer fix; approve recovery commit; observe second
     post-restart test pass.
   - **Shape 2**: kill Kernos mid-attempt (during spec cycle,
     during impl cycle, during awaiting_approval, during
     push). On restart: verify each crashed attempt detected;
     verify resume_point classified correctly; verify
     `surface_only` whisper shape; opt into `/resume` for
     each safe point; verify `/abandon` works for unsafe.
   - **Shape 3**: trigger drift by manually pushing a commit
     to origin/main from another clone during an attempt;
     observe `surface_only` whisper; opt into
     `/rebase_and_retry`; verify rebase + re-push.
   - **Shape 4**: monkeypatch consult to raise transient
     errors twice then succeed; observe retries in ledger;
     verify final attempt succeeds.
3. **Failure soak**: each shape's failure-mode AC verified
   by inducing the specific failure during operator soak.

## Cognition-path migration soak gate

Per [[cognition-migration-soak-gate]]: no production-default
flip on cognition-path migrations until automated tests pin
seams AND operator soak verifies lived cognition through
substrate inspection.

This spec ships every shape behind a flag with conservative
default (off or surface_only). Promotion to default-on is
per-shape, requires:
1. Automated soak passes consistently.
2. At least 3 operator-soak runs in `auto`/`on` mode
   verifying the substrate state matches the user-visible
   prose.
3. Ledger inspection during soak confirms the expected
   event sequence.

Default-on promotions land as separate one-line spec patches
post-evidence-gathering, not as part of this initial spec.

## Out of scope (deferred to v2 if evidence supports)

- **Parallel attempts.** v1 assumes one active attempt at a
  time. Concurrent `/fix` triggers serialize; second attempt
  waits in queue. Parallel safe-execution is a v2 question
  (worktree isolation already supports it; the question is
  receipt + ledger contention).
- **Recovery reviewer rotation.** v1 always uses the original
  reviewer agent for recovery. If the reviewer is the source
  of the bad call, recovery loops on the same blind spot.
  v2 question: round-robin or fresh-coding-agent for recovery.
- **Cross-attempt learning.** When a recovery succeeds, the
  fix-pattern is recorded in the ledger but not surfaced as a
  workflow pattern. v2 could mine the ledger for recovery-
  pattern frequency and propose patterns when a class of fix
  recurs.
- **Approval re-issue on TTL expiry.** Per IMPROVEMENT-LOOP-
  WORKFLOW-V1 Risk #3: if the operator takes >24h to approve,
  the receipt expires and the attempt is stuck. v2 adds an
  operator-callable `/reissue_approval <attempt_id>`.

## Risks

- **Risk:** Shape 1's recovery iteration cap (2) is too low.
  A 3rd attempt might succeed with a different angle; cutting
  off at 2 may abandon recoverable failures.
  - **Mitigation:** Cap is operator-configurable via
    `KERNOS_IMPROVEMENT_RECOVERY_ITERATION_CAP`. v1 ships
    default 2 because each iteration consumes operator
    approval bandwidth; bumping requires operator decision.

- **Risk:** Shape 2's resume_point classification is
  heuristic-based on the last ledger event. If the orchestrator
  crashes BETWEEN appending two ledger events (e.g., after
  `impl_converged` is appended but before `approval_requested`),
  the resume_point will be `impl_cycle_start` instead of
  `approval_gate`. Re-running the spec cycle wastes work.
  - **Mitigation:** Acceptable v1 trade-off — wasted work is
    cheaper than auto-resuming into an inconsistent state.
    The orchestrator can detect spec.md already exists in
    the worktree and skip spec_cycle when resuming.
    Documented in the Shape 2 README.

- **Risk:** Shape 3's auto-rebase + self-test gate can mask
  test failures introduced by main's drift (e.g., a new
  test in main that the rebased branch now fails).
  - **Mitigation:** The gate IS the self-test; if it fails,
    `rebase_self_test_failed` surfaces. Operator can decide
    whether the test failure is a true regression or pre-
    existing flake.

- **Risk:** Shape 4's transient catalog (502/503/504/etc.)
  may misclassify a real failure as transient and retry into
  noise. Codex's 600s timeout, for example, looks like a
  hang — is it transient?
  - **Mitigation:** Lean conservative on the catalog (already
    decided in AGENT_CALL_TRANSIENT_RETRY_V1). Hard cap of
    10 retries/attempt prevents pathological loops.

- **Risk:** Shape 6's `attempt_escalated_to_architect` final
  state could become a dumping ground for "CC is confused."
  If recovery fires for too many "architect-bound" cases that
  weren't truly architectural, the operator loses signal.
  - **Mitigation:** The bridge's investigation_outcome is set
    by CC explicitly via the structured-fields protocol; CC
    has to actively choose this outcome. Audit the rate
    monthly; if escalation becomes >25% of attempts, revisit
    the prompt guidance.

## Dependencies

All shipped:
- IMPROVEMENT-WORKSPACE-V1 — worktree persists across crash
- IMPROVEMENT-ATTEMPT-LEDGER-V1 — recovery's durability surface
- IMPROVEMENT-LOOP-WORKFLOW-V1 — happy-path orchestrator
- GIT-OPERATIONS-PRIMITIVES-V1 — drift detection at push
- DURABLE-APPROVAL-RECEIPTS-V1 — recovery commit gates
- USER-INITIATED-IMPROVEMENT-TRIGGER-V1 — `/fix` surface
- BRIDGE-RESPONSE-SCHEMA-V1 — structured `investigation_outcome` extraction
- AGENT-CALL-TRANSIENT-RETRY-V1 — transient error catalog (Shape 4 reference)
- WORKFLOW-DESCRIPTOR-VERSIONING-V1 — recovery YAML lifts (if any) survive upgrades

## Migration

Additive. New module + new ledger event types + new slash
commands + new bring-up hook. No schema change to existing
ledger columns (event_type is already TEXT). Bring-up hook
ordering update is documented in the bring-up registry test.

The orchestrator gains four new entry points and one wrapped-
call helper (`retry_consult`), but the existing happy-path
behavior is unchanged when all flags are at default values.

## Open architect questions

1. **Default for Shape 1.** `surface_only` is conservative
   but adds friction (operator opts in per attempt). `auto`
   ships the recovery experience the loop was built for but
   trusts the reviewer agent more. v1 defaults to
   `surface_only` — is that right for the soak window?

2. **Recovery commit attribution.** A recovery commit lands
   on the same branch as the original attempt. Should the
   commit author/message reflect that it's a recovery
   (e.g., commit message prefix `[RECOVERY]`)? Currently no
   — recovery commits look like normal commits.

3. **Multiple in-flight attempts on resume.** If Kernos
   crashes with 3 attempts in flight, Shape 2 detects all 3.
   Does the operator get 3 separate whispers or one
   consolidated? v1 leans separate (per-attempt prose); could
   consolidate if that's noisy.

4. **Retry-cap interaction with iteration-cap.** Shape 4's
   retry cap (10/attempt) is across ALL consult calls in the
   attempt — but each iteration of spec/impl cycle has its
   own internal review-protocol retries. The interaction can
   surprise: an attempt could fail with `retry_cap` even
   though no individual iteration hit its convergence cap.
   Worth surfacing in `/improvement_status` more prominently?

5. **Shape 6 boundary clarity.** "Architectural-bound" is a
   judgement call CC makes. v1 trusts CC's classification
   absolutely. Is there a hard rule the substrate should
   enforce (e.g., "if CC asked for /architect, route here
   regardless of investigation_outcome")?
