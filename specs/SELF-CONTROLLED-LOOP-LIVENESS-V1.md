# SELF-CONTROLLED-LOOP-LIVENESS-V1

**Date:** 2026-05-21
**Status:** Draft for review
**Scope:** Make the self-controlled loop **provably alive** end-to-end.
  Closes two distinct admission-path failures: (A) `manage_plan` is
  surfaced but never invoked because the model hallucinates wrong
  tool names, and (B) workflows are registered but never execute
  because trigger semantics are starved by lifecycle.
**Estimated size:** ~150 LOC source + ~120 LOC tests + 1 workflow YAML.

## Why this spec exists

Codex architecture deliberation (2026-05-21) on the operator's report
that "self-controlled loop for plan and workflow execution" is broken
identified two distinct, both wired-but-starved failures:

1. **`manage_plan` never admits.** The handler, recovery, and async
   step execution all work (`handler.py:5566`, `handler.py:5836`,
   `handler.py:1081`). The break is at admission: the substrate
   leaves the critical entry into self-control to *prompt compliance*.
   Live evidence from 5+ recent context dumps: agent has `manage_plan`
   surfaced AND the system prompt explicitly names it AND no
   `PLAN_CREATE` log line has ever appeared. Agent hallucinates
   `planning_orchestration.create_plan` and `workspace_plan_artifact_write`
   instead. A critical kernel primitive cannot depend on the model
   remembering one exact tool name.

2. **Workflows fire registration but never execution.** The substrate
   is wired correctly: `TriggerEvaluationRuntime` starts at
   `bring_up_substrate.py:221`, `InternalEventAdapter` is connected
   to the event stream, `ExecutionEngine` is the executor, the
   `self_improvement` workflow is registered with its trigger on
   `friction.pattern_frequency_threshold_exceeded`. But that event
   is emitted by `autonomy_emitters.py:162` ONLY in response to
   `friction.pattern_reactivated`. Live patterns accumulate
   occurrences as active and never reactivate, so the trigger never
   matches, so the workflow never executes. Lifecycle-starved.

Both failures together: the loop exists in code; nothing has ever
proven it runs end-to-end in production.

Fix shape (Codex's recommendation): one tight spec, two pieces +
self-improvement repair, with a substrate-level liveness sentinel
that proves the loop alive in the first restart after the fix.

## Current state (truth)

- **`manage_plan` handler:** `handler.py:5566` — handles
  `create | continue | status | pause | cancel`. Plan creation
  spawns `asyncio.create_task(_execute_self_directed_step(...))`,
  which builds a `NormalizedMessage` with `sender="self_directed"`,
  processes it through the full pipeline (which should then call
  `manage_plan(action="continue")` to advance), and writes
  `PLAN_CREATE` / `PLAN_STEP_STARTED` / `PLAN_STEP_COMPLETED`
  log lines. None of those log lines appear in the last 14h.
- **`manage_plan` in catalog:** registered at every boot as kernel
  source, version 35. Visible in the agent's tool block.
- **System prompt:** EXPLICITLY tells the agent "Use manage_plan with
  action='create'" and frames it as the self-directed primitive.
- **Live model behavior:** hallucinated `planning_orchestration.create_plan`
  and `workspace_plan_artifact_write` (smoke test, 2026-05-21). Neither
  is a real tool name anywhere in the codebase.
- **`TriggerEvaluationRuntime` runtime:** `triggers/runtime.py:344`
  `on_event_observed` claims fires and dispatches workflows via
  the `ExecutionEngine.execute_workflow` callable.
- **`InternalEventAdapter`:** wires event stream → trigger runtime.
- **`self_improvement` workflow:** registered + activated at boot
  (every restart shows `SELF_IMPROVEMENT_WORKFLOW_REGISTERED` +
  `_ACTIVATED` + `SELF_IMPROVEMENT_AUTONOMY_LOOP_LIVE`). Zero
  execution events ever appear in the log.
- **`FrictionPatternFrequencyEmitter` (`autonomy_emitters.py:160`):**
  translates `friction.pattern_reactivated` → canonical
  `friction.pattern_frequency_threshold_exceeded`. Only that
  translation path exists; active-pattern threshold crossings are
  silent.
- **`record_occurrence` (`friction_patterns.py:1204`):** active
  occurrences increment the count but do not emit any
  threshold-crossed signal regardless of how many occurrences
  accumulate.
- **`system.started` event:** emitted by `server.py:673` BEFORE
  `bring_up_substrate` runs at `server.py:1126`. So a workflow
  trigger registered during substrate bring-up that listens for
  `system.started` would miss the event. There is no `startup`
  trigger type.
- **Existing wiring path** (Codex callout): event_stream post-flush
  hook → `InternalEventAdapter` → `TriggerEvaluationRuntime` →
  `ExecutionEngine`. This is the path; do not add another runner
  or polling loop.

## Three-piece fix

### Piece 1 — `manage_plan` admission via static alias canonicalizer

A shared static canonicalizer module: `kernos/kernel/tool_aliases.py`.

```python
# Static alias map. Only known model hallucinations. No regex, no
# fuzzy match, no LLM "did you mean." Deterministic + auditable.
_TOOL_ALIASES: dict[str, str] = {
    "planning_orchestration.create_plan": "manage_plan",
    "workspace_plan_artifact_write": "manage_plan",
}


def canonicalize_tool_name(name: str) -> tuple[str, bool]:
    """Return (canonical_name, was_repaired). Caller logs a
    TOOL_ALIAS_REPAIR line on was_repaired=True so the agent's
    misuse stays auditable."""
    canonical = _TOOL_ALIASES.get(name)
    if canonical is None:
        return (name, False)
    return (canonical, True)
```

Insertion points:
- `kernos/kernel/reasoning.py` `ReasoningService.execute_tool` —
  TOP of the function, before `if tool_name in self._KERNEL_TOOLS`.
  If repaired, log `TOOL_ALIAS_REPAIR alias=X canonical=Y` then
  continue with the canonical name.
- `kernos/kernel/gate.py` `DispatchGate.classify_tool_effect` —
  TOP of the function. Live dispatch classifies BEFORE calling
  execute_tool; without repair here, the gate returns "unknown"
  and the live dispatcher refuses before reasoning ever sees
  the call.

What's NOT in this piece:
- Argument shape repair. V1 only repairs the tool name. If a known
  alias is called with wrong args, the canonical tool's own
  validation returns an error message. Argument-shape shims can
  be added per-alias later if a specific case demands.
- Repair for non-planning hallucinations. V1 ships just the two
  known cases; extending is a one-line dict edit later.

### Piece 2 — Boot-smoke "loop_health" sentinel workflow

A harmless internal workflow whose only job is to prove
`trigger registered → fired → execution → terminal state` on every
boot. If liveness ever breaks again, this fires-or-doesn't on every
restart and the absence is loud.

**Workflow YAML:** `specs/workflows/loop_health.workflow.yaml`. Shape
mirrors `self_improvement.workflow.yaml` (Codex round 1 finding 1:
existing loader expects `instance_id`, `name`, `bounds`, `verifier`,
`action_sequence`; ref grammar uses `{idea_payload.X}` not
`${trigger.X}`):

```yaml
workflow_id: loop_health
instance_id: '{installer.instance_id}'
name: Self-Control Loop Liveness Sentinel
description: |
  Proves the substrate event-trigger-workflow loop is alive on
  every boot. Fires once per restart after substrate bring-up
  completes. The presence of a new ledger entry per boot AND a
  workflow.execution_terminated event with outcome=completed is
  the load-bearing operator-visible proof of liveness.
version: "1.0"
owner: architect
instance_local: true

bounds:
  iteration_count: 1
  wall_time_seconds: 30

verifier:
  flavor: deterministic
  check: terminated

triggers:
  - event_type: loop_health.boot_probe
    event_selector:
      op: AND
      operands:
        - op: eq
          path: event_type
          value: loop_health.boot_probe
        - op: eq
          path: instance_id
          value: '{installer.instance_id}'
        - op: exists
          path: payload.boot_id

action_sequence:
  - id: record_boot_smoke
    action_type: append_to_ledger
    parameters:
      workflow_id: loop_health
      entry:
        kind: boot_smoke
        boot_id: '{idea_payload.boot_id}'
        booted_at: '{idea_payload.booted_at}'
    continuation_rules:
      on_failure: abort
```

**Registration helper:** new file `kernos/kernel/workflows/loop_health_helper.py`,
modeled on `self_improvement_helper.py`. Must register-AND-trigger-compile
explicitly (Codex round 1 finding 2 — a normal workflow file
registration is not enough for the live path).

**Synthetic substrate architect for activation** (Codex round 2
finding 1 — activation requires architect context backed by
`KERNOS_ARCHITECT_ACTOR_ID` per `authoring.py:172, 1040`; the sentinel
must work even when self-improvement env vars are unset). The sentinel
is substrate-owned, not creator-authored, so it does not require a
real human architect. The helper provides a synthetic substrate-owner
context (`actor_id="substrate.loop_health_sentinel"`, role marker
sufficient for `register_workflow + activate_workflow` to succeed)
unconditionally. The sentinel is the substrate's own liveness
heartbeat; it must come up whether or not the operator has wired
self-improvement env vars.

```python
from kernos.kernel.workflows.authoring import ArchitectActor

_SENTINEL_ARCHITECT = ArchitectActor(
    actor_id="substrate.loop_health_sentinel",
    display_name="Substrate Loop-Health Sentinel",
)

async def register_loop_health_workflow(
    *, execution_engine, workflow_registry, trigger_runtime,
    instance_id, ...,
) -> str:
    # Load YAML, register_workflow with _SENTINEL_ARCHITECT,
    # activate_workflow with same, compile_descriptor_triggers,
    # runtime.register each. Mirrors self_improvement_helper:225+
    # pattern but does NOT depend on KERNOS_ARCHITECT_ACTOR_ID.
```

If the existing `ArchitectActor` shape rejects a synthetic actor_id,
the helper uses the lowest-friction internal-registration path the
substrate exposes — the goal is unconditional registration, NOT
spec-bound to a specific code path.

**Event emission:** in `bring_up_substrate.py`, AFTER
`InternalEventAdapter.start()` AND AFTER `register_loop_health_workflow`
completes (so trigger is registered before event fires). Use the
same instance-namespace logic as self-improvement (Codex round 1
finding 3 — `bring_up_substrate` has no `instance_id` local):

```python
_loop_health_instance_id = (
    os.getenv("KERNOS_INSTANCE_ID", "")
    or _substrate_instance_id
    or "default"
)
await event_stream.emit(
    _loop_health_instance_id, "loop_health.boot_probe",
    {"boot_id": _generate_boot_id(), "booted_at": utc_now()},
    space_id="",
)
```

Register `loop_health.workflow.yaml` independently of the
`self_improvement` env vars — the sentinel is unconditional.

### Piece 3 — Self-improvement trigger semantics fix

Keep the workflow trigger pointing at `friction.pattern_frequency_threshold_exceeded`.
Don't lie by emitting `pattern_reactivated` for active patterns.

Add a new internal event:
`friction.pattern_active_frequency_threshold_crossed`.

- **Producer:** `friction_patterns.py` `record_occurrence`. After
  incrementing the count, if the active pattern's count just crossed
  its `reactivation_threshold` (Codex round 1 finding 4 — the field
  is named `reactivation_threshold`, not `threshold`; clarifying so
  acceptance tests match the actual schema), emit the new internal
  event with `{instance_id, pattern_id, count, reactivation_threshold,
  active_epoch}`. Dedup via the existing `active_epoch` field so
  multiple crossings within an epoch don't re-trigger.
- **Translator:** `FrictionPatternFrequencyEmitter` in
  `autonomy_emitters.py:160` — extend to translate BOTH:
  - `friction.pattern_reactivated` → `friction.pattern_frequency_threshold_exceeded`
  - `friction.pattern_active_frequency_threshold_crossed` → `friction.pattern_frequency_threshold_exceeded`
  The output stays canonical so the workflow trigger is unchanged.

This avoids:
- Lying with pattern_reactivated (false history).
- Duplicate translations (active_epoch dedupe stays in one place).
- Changing the workflow trigger contract.

## What does NOT change

- `manage_plan` handler, `_execute_self_directed_step`, recovery
  path — all working, untouched.
- `TriggerEvaluationRuntime`, `InternalEventAdapter`,
  `ExecutionEngine` — wiring untouched. Same path the rest of
  the substrate uses.
- `self_improvement` workflow YAML — trigger contract unchanged.
- `friction_patterns.py` schema — only the producer side of
  `record_occurrence` gains an emit call; storage stays the same.
- Existing tool surface, system prompt, model-output parser —
  alias repair lives at dispatch ingress, NOT in the parser.

## Acceptance criteria

Ship only when all of these pass:

1. **Alias repair, reasoning side, both aliases.** Calling
   `execute_tool("planning_orchestration.create_plan", manage_plan-shaped args, request)`
   reaches the `manage_plan` handler and produces a `PLAN_CREATE`
   log line. A `TOOL_ALIAS_REPAIR alias=planning_orchestration.create_plan canonical=manage_plan`
   INFO log accompanies it. SAME test repeated for
   `workspace_plan_artifact_write` (Codex round 1 finding — test
   both aliases, not just one).
2. **Alias repair, gate side, both aliases.** `DispatchGate.classify_tool_effect("planning_orchestration.create_plan", ...)`
   returns the classification for `manage_plan`, not `"unknown"`.
   Repeat for `workspace_plan_artifact_write`.
3. **Sentinel workflow loads + trigger registers.** `loop_health.workflow.yaml`
   loads via the existing workflow loader (`name`, `bounds`,
   `verifier`, `action_sequence` schema all parse), and after
   `register_loop_health_workflow()`, the `TriggerEvaluationRuntime`
   reports a trigger registered for `event_type=loop_health.boot_probe`
   (introspection via existing `runtime` accessor).
4. **Boot-smoke workflow fires on every restart.** (Codex round 2
   finding 2 — workflow.execution_* events emit to event_stream,
   NOT logger per `execution_engine.py:856,2391`; ACs split between
   log-grep and event-stream-DB-query for honesty.) After restart:
   - **Logs** (grep `data/discord_*/diagnostics/server.log`) contain:
     - `LOOP_HEALTH_WORKFLOW_REGISTERED workflow_id=loop_health`
     - `LOOP_HEALTH_WORKFLOW_TRIGGERS_REGISTERED workflow_id=loop_health trigger_count=1`
     - `LOOP_HEALTH_BOOT_PROBE_FIRED boot_id=X booted_at=Y` (new
       INFO log emitted by the helper at the moment it queues the
       boot_probe event — gives operator a single-line grep target)
     - `LOOP_HEALTH_EXECUTION_COMPLETED boot_id=X` (new INFO log
       emitted by the helper when a `workflow.execution_terminated`
       event for `loop_health` arrives; helper subscribes to the
       event stream to surface this for operator-visibility)
   - **Event stream DB** (query for `instance_id` + `event_type IN
     ('workflow.execution_started','workflow.execution_terminated')`
     filtered by `workflow_id=loop_health`) contains exactly one
     started + one terminated event per restart, with
     `terminated.outcome=completed`.
5. **Workflow ledger contains the boot-smoke entry** with the
   current `boot_id` after restart. Operator can inspect
   `loop_health` ledger and see one new row per restart.
6. **Active-frequency threshold crossing emits canonical event.**
   Synthetic test: seed an active friction pattern with
   `reactivation_threshold=3` (the actual schema field name), call
   `record_occurrence` 3 times, assert exactly one
   `friction.pattern_active_frequency_threshold_crossed` event AND
   exactly one downstream `friction.pattern_frequency_threshold_exceeded`
   event. Verify dedupe: a 4th `record_occurrence` in the same
   active_epoch does NOT re-emit.
7. **Self-improvement workflow queues** on the canonical event.
   Synthetic test: emit `friction.pattern_frequency_threshold_exceeded`
   directly into the event stream; assert
   `workflow.execution_started workflow_id=self_improvement` follows.
8. **Sentinel works without self-improvement env vars.** (Codex
   round 2 finding 1.) Test: with `KERNOS_ARCHITECT_ACTOR_ID` AND
   `KERNOS_OPERATOR_ACTOR_ID` both UNSET, the loop_health workflow
   still registers, the trigger still wires, the boot_probe still
   fires, the workflow still executes to completion. The sentinel
   is unconditional; it does not share self-improvement's gating.
9. **No regressions.** Existing tests for `manage_plan`,
   trigger runtime, workflow execution engine, friction patterns,
   autonomy emitters all pass.

The 4 logged-line invariants (1-3, plus 6) are the
**startup-visible proof** the operator can verify with a single
`grep` after the fix-restart.

## Out of scope

- Argument-shape repair for hallucinated tools (V1 repairs only the
  name; canonical tool's own validator handles malformed args).
- Repair for any tool other than `manage_plan` (the two known
  hallucinations BOTH map there; other tools can be added as
  one-liner dict entries when evidence appears).
- A "did you mean" LLM repair path (deterministic static dict is
  cheaper, more predictable, easier to audit).
- Per-instance configuration of the alias map (V1 ships a single
  process-wide dict; if instances need different aliases, that's
  a follow-up).
- Changing the `system.started` event timing (Codex finding —
  it fires before bring_up_substrate, so workflows registered there
  miss it; using `loop_health.boot_probe` sidesteps this without
  refactoring boot order).
- The `KERNOS-AUTONOMOUS-IMPROVEMENT-LOOP` design spec (operator's
  Layer-1 ask). That spec depends on this one shipping first —
  the autonomous improvement loop literally IS a long-running
  `manage_plan` + workflow execution.

## Risk

- **Alias map drift.** If new model hallucinations appear post-ship,
  the static dict needs updating. Mitigation: every repair logs
  `TOOL_ALIAS_REPAIR`; periodic operator review of those logs
  surfaces additions cleanly. The list will stay small (the model's
  hallucinations cluster on the same few wrong names).
- **Workflow YAML mismatch.** The `loop_health.workflow.yaml` shape
  must match the existing workflow loader's expectations. If
  `append_to_ledger` action verb isn't surfaced as expected,
  the workflow won't load. Mitigation: pre-test the YAML by loading
  it in isolation before wiring; existing workflow YAMLs in
  `specs/workflows/` are reference examples.
- **Active-epoch dedupe edge cases.** If `record_occurrence` is
  called concurrently for the same pattern, two threshold-crossing
  emissions could race. Mitigation: existing
  `FrictionPatternFrequencyEmitter` already does
  `active_epoch`-based dedupe; reuse that path. The new event
  carries `active_epoch` so downstream dedupe still works.
- **Boot-probe event timing.** If the event fires before
  `InternalEventAdapter.start()` is fully ready, the workflow
  trigger won't see it. Mitigation: spec explicitly places
  emission AFTER adapter start AND workflow registration.

## Roll-out

Single batch. Manual verification post-merge:

1. `git pull && /restart`
2. After boot:
   - `grep TOOL_ALIAS_REPAIR data/discord_*/diagnostics/server.log`
     — should be empty until the agent next hallucinates (will
     fire automatically when it does).
   - `grep "loop_health" data/discord_*/diagnostics/server.log`
     — should see WORKFLOW_REGISTERED, boot_probe, execution_started,
     execution_terminated outcome=completed.
3. Send agent a multi-step task. Watch for `PLAN_CREATE` log.
4. If agent still hallucinates: confirm `TOOL_ALIAS_REPAIR` fires
   and the call reaches `manage_plan` successfully.
5. Observe over the next hour: any organic friction pattern that
   reaches its threshold should produce a `self_improvement`
   workflow execution.

The operator sees liveness in #2 within the first 30 seconds of
boot. No waiting for organic friction.
