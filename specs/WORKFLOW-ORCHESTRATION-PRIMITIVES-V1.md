# WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 — Implementation Spec

**Status:** DRAFT v2 — pre-implementation. ONE Codex pre-spec review
round folded (10 findings: 3 blockers, 3 high, 4 medium).

**Codex round 1 (10 findings, folded into v2):**

- **Blocker 1** — branch decisions weren't durable across restart.
  The branch verb mutated cursor in-memory only; on restart, the
  natural `action_index_completed + 1` would resume past the branch
  step instead of at the chosen target. Folded by adding a new
  `next_step_index` column on `workflow_executions` that the branch
  verb writes atomically with its record/cursor commit; restart-
  resume consults it before defaulting (Decision 9 below).
- **Blocker 2** — branch was implicitly described as a skip
  primitive but the engine's linear advance contradicts that.
  Folded by declaring branch IS a goto and clarifying that "skip
  downstream" requires terminal-branch routing or careful main-
  sequence layout (Decision 5 revised).
- **Blocker 3** — terminal branch step indices collide with Spec 3's
  `workflow_action_records` PK (which is keyed by
  `(instance_id, execution_id, step_index)`). Folded by introducing
  a **global step ordinal**: at workflow registration, every
  step (main sequence + every terminal branch) receives a unique
  globally-monotonic `step_index`. `step_id` remains the human-
  readable identifier for references; `step_index` is the substrate
  ordinal that Spec 3 keys against (Decision 0 revised).
- **High 4** — step output capture outcome matrix was
  underspecified. Folded by mirroring Spec 3's per-outcome shape:
  all five outcomes (completed / gated-completed / continue-failed
  / abort-failed / execute-raised) capture an envelope; the helper
  signatures explicitly thread output payload through each path
  (Decision 2 revised + Decision 9 revised).
- **High 5** — gate output capture lacked an event-payload handoff.
  Folded by having `_await_gate` return the matched event payload
  alongside the existing continuation bool; the engine threads
  that payload to `_clear_gate_and_advance` for atomic capture
  (Decision 6 revised).
- **High 6** — `bool(condition_value)` would route truthy strings
  like `"false"` to the true branch. Folded by requiring native
  bool only at branch dispatch; non-bool values fail loud
  (Decision 5 revised).
- **Medium 7** — branch verb's risk_level was `low` but Spec 3's
  derivation maps `mutate` → `medium`. Folded to `medium` for
  consistency (Decision 10 revised).
- **Medium 8** — predicate cache key was too weak. Folded to
  `(execution_id, gate_nonce)`; cleared on release / timeout /
  abort (Decision 8 revised).
- **Medium 9** — step / branch / gate ID grammar was undefined and
  could break reference parsing (paths split on `.`, terminal
  targets split on `:`). Folded by validating IDs against
  `[A-Za-z][A-Za-z0-9_-]*` at registration (Decision 0 revised).
- **Medium 10** — `ON CONFLICT DO UPDATE` on `workflow_step_outputs`
  could desync from Spec 3's append-only `workflow_action_records`.
  Folded by gating the step_output INSERT on the per-outcome
  helper's `inserted=True` return — step output and action record
  advance together (Decision 11, new).

**Author:** CC, 2026-05-12. Resolves architect's framing in the
Spec 4a build directive + folds Codex round 1.

**Source build directive:** Notion `35effafef4db8168855eeb2524d2ff4e`.
Substrate gaps in the original Spec 4 draft (CC pre-spec review at
Notion `35effafef4db81dcbc0cf432b425fb24`) drove the Option B split:
this spec is Spec 4a; Spec 4b (self-improvement workflow definition)
rebuilds against the primitives this spec ships.

**Source framing:** PHASE-3-AUTONOMY-LOOP design consideration
(Notion `35cffafef4db81da8107e562307bc738`). Spec 4a of the
five-spec autonomy-loop arc, sequencing after FRICTION-PATTERN-STABLE-IDS-V1
(Spec 1; merged at `452dbee`), CODING-SESSION-BRIDGE-V1 (Spec 2;
merged at `a16c1d9`), and ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1
(Spec 3; close-ratified, branch `actionstaterecord-workflow-composition-v1`
at `179ef4a`).

**Architect's lean (locked):** five primitive extensions. Each with
the architect's design lean on storage shape, failure mode, and
scope; CC's deviations (where any) are surfaced visibly in the
decision sections below.

**Composes with:**

- Workflow primitive (`kernos/kernel/workflows/`) — this spec
  extends the existing workflow primitive at the descriptor, engine,
  and predicate layers.
- Spec 3 (`kernos/kernel/workflows/action_sink.py`) — every step in
  any workflow (including branch verbs and template-resolved steps)
  produces an `ActionStateRecord` via the per-outcome transaction
  matrix Spec 3 ships.
- Spec 4b (NOT in this spec) — the self-improvement workflow
  definition consumes the primitives this spec ships.

## What this spec ships

Six deliverables (the architect's five plus one CC-surfaced
addition; see Decision 0 below):

0. **Step IDs on ActionDescriptor.** A new optional `id: str` field
   on the action descriptor. Required when a step is the target of
   a reference; optional otherwise. Validation: ids unique within
   a workflow including across `terminal_branches` (see Decision 7).

1. **Step output capture.** Each action's `ActionResult` persists
   into a new `workflow_step_outputs` table keyed by
   `(instance_id, workflow_execution_id, output_kind, output_name)`
   with `output_kind = 'step' | 'gate'`. Captured output is a
   JSON-serialized dict shape combining the result's `value`,
   `receipt`, `error`, and `success` fields. 64KB cap with
   truncation marker.

2. **Reference resolution.** Action parameters AND predicate
   `value:` fields support template references in four namespaces:

       {workflow.<key>}          existing fixed-placeholder set
       {idea_payload.<path>}     workflow trigger event payload
       {step.<step_id>.<scope>.<path>}    step output / receipt / error / success
       {gate.<gate_name>.output.<path>}   gate-release event payload

   Dispatch by prefix; missing references produce loud failures.

3. **`branch` verb.** New eighth verb. Reads a boolean condition via
   template syntax; sets the engine's next-step pointer to either
   `branch_on_true` or `branch_on_false` target step ID.

4. **Terminal branches.** New top-level `terminal_branches:
   {branch_name: [action_list]}` block on the workflow descriptor.
   Each terminal branch is a named action sub-sequence; reachable
   only via `branch` verb targeting `terminal:<branch_name>:<step_id>`.
   The engine's `terminal_state` stays `completed | aborted`; the
   branch_name is captured in the execution row's metadata for
   audit visibility.

5. **Predicate-evaluator template substitution.** The gate
   predicate's `value:` field resolves template references at gate
   evaluation time. Resolved values cache per
   `(execution_id, gate_name)` per execution; the cache invalidates
   if the referenced step is re-executed (which shouldn't happen in
   v1 but defensive).

6. **Gate output capture.** When `_await_gate` resolves successfully
   on a satisfying approval event, the engine captures the event's
   payload into the same `workflow_step_outputs` table under
   `output_kind = 'gate'`, `output_name = <gate_name>`. Reference
   syntax: `{gate.<gate_name>.output.<path>}`.

## What this spec does NOT ship

Per the architect's build directive:

- **NO consumer workflow definitions.** Spec 4b (the
  self-improvement workflow) is its own spec; this ships the
  primitives only.
- **NO adapter tools.** `transition_friction_pattern_lifecycle`,
  `record_friction_pattern_recurrence`, `surface_substrate_observation`,
  etc. are Spec 4b's wiring concerns.
- **NO Spec 2 bridge-tools production wiring.** Spec 2's bridge is
  shipped; Spec 4b's implementation brings it up against production.
- **NO conversational-request classifier.** Spec 4b.
- **NO IA / Kernos observation channel extensions.** Spec 4b.
- **NO awareness-layer rendering of workflow events.** Spec 4b.
- **NO new constraint:** this spec only extends the workflow
  primitive; doesn't define any consumer.

## Architectural decisions

### Decision 0 — Step IDs + global step ordinal + ID grammar (CC-surfaced addition; revised v2 per Codex Blocker 3 + Medium 9)

**Architect's framing did not name this explicitly,** but every
other extension assumes step IDs exist. The current
`ActionDescriptor` at `workflow_registry.py:135` has no `id` field;
steps are identified by their index in `action_sequence`. To
reference a prior step's output via `{step.<id>.output.<path>}`,
the step needs a stable string identifier.

**Codex round 1 Blocker 3** caught a further gap: Spec 3's
`workflow_action_records` table is PK'd by
`(instance_id, workflow_execution_id, step_index)`. With terminal
branches each running their own action list, a terminal step at
local index 0 would collide with main step 0 — the ON CONFLICT DO
NOTHING clause would silently swallow the terminal step's record.
The fix: introduce a **global step ordinal** assigned at workflow
registration, monotonically increasing across the main sequence
and every terminal branch.

**Resolution:** add `id: str` and `step_index: int` to
`ActionDescriptor`. The author writes `id` (human-readable);
`step_index` is assigned at registration time by `validate_workflow`.

```python
@dataclass
class ActionDescriptor:
    action_type: str
    parameters: dict
    per_action_expectation: str = ""
    continuation_rules: ContinuationRules = field(default_factory=ContinuationRules)
    gate_ref: str | None = None
    resume_safe: bool = False
    id: str = ""                  # NEW: human-readable identifier
    step_index: int = -1           # NEW: globally-assigned ordinal (registration-time)
```

**Global step ordinal assignment** at workflow registration:

```
Main sequence:                  step_index = 0 .. N-1
Terminal branch "rejected":     step_index = N .. N+M-1
Terminal branch "<branch_2>":   step_index = N+M .. N+M+P-1
...
```

The ordinal is assigned in declaration order. Once assigned, it's
stable for the lifetime of the workflow (workflow descriptors are
immutable post-registration; re-registration with the same workflow
ID overwrites).

**`workflow_action_records` PK remains `(instance_id,
workflow_execution_id, step_index)`** — no schema change needed.
Terminal branch records get unique step_index values via the global
ordinal assignment, so the PK collision Codex flagged disappears.

**`action_index_completed` cursor** on `workflow_executions`
remains the global step_index of the last completed step. Restart
logic continues to work: `next_idx = action_index_completed + 1`
maps to a global ordinal — but this is overridden by Decision 9's
`next_step_index` column for branch decisions.

**ID grammar (Codex Medium 9):** step IDs, gate names, and terminal
branch names MUST match the regex `^[A-Za-z][A-Za-z0-9_-]*$`. The
restriction rules out characters that would break reference
parsing (`.` splits namespaces; `:` splits terminal-target
segments). Validated at registration with explicit error per
offending field.

**Validation at registration (extends `validate_workflow`):**

- Step IDs match the grammar. Required iff the step is a reference
  target (branch verb target, parameter reference, predicate
  reference). Optional but encouraged for all steps for
  observability.
- Step IDs are unique within the workflow, including across all
  terminal branches.
- Terminal branch names match the grammar.
- Gate names match the grammar.
- Branch verb's `branch_on_true` / `branch_on_false` targets
  resolve to declared step IDs (bare) or
  `terminal:<branch_name>:<step_id>` (with the branch_name and
  step_id both validated against the grammar).
- Template references `{step.<id>...}` to non-existent IDs are
  flagged at registration (static check); references to existing
  IDs whose output hasn't been captured at resolve time produce
  runtime errors (dynamic check).
- The global step_index assignment is performed by
  `validate_workflow` as part of registration; if validation
  fails, no indices get assigned.

**Backward compatibility:** existing workflows without `id` fields
continue to validate and execute (step_index gets assigned per
main-sequence position). The reference resolver simply can't target
them — references to undeclared IDs fail at registration.

### Decision 1 — Step output storage shape

**Architect's lean (adopted):** sibling table
`workflow_step_outputs` with composite PK. Composes with Spec 3's
`workflow_action_records` convention.

```sql
CREATE TABLE IF NOT EXISTS workflow_step_outputs (
    instance_id             TEXT NOT NULL,
    workflow_execution_id   TEXT NOT NULL,
    output_kind             TEXT NOT NULL,   -- 'step' | 'gate'
    output_name             TEXT NOT NULL,   -- step.id OR gate_name
    output_json             TEXT NOT NULL,
    truncated               INTEGER NOT NULL DEFAULT 0,
    recorded_at             TEXT NOT NULL,
    PRIMARY KEY (instance_id, workflow_execution_id, output_kind, output_name),
    FOREIGN KEY (workflow_execution_id)
        REFERENCES workflow_executions(execution_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_workflow_step_outputs_kind
    ON workflow_step_outputs (instance_id, workflow_execution_id, output_kind);
```

**`PRAGMA foreign_keys=ON`** mandatory on every connection (same
discipline as Spec 3's `workflow_action_records`).

**ON DELETE RESTRICT** mirrors Spec 3's no-destructive-deletes
discipline; workflow_executions don't get deleted, so this is a
safety net.

**Storage co-location with Spec 3's table:** both Spec 3's
`workflow_action_records` and this spec's `workflow_step_outputs`
live in `instance.db` alongside `workflow_executions`. Both share
the engine's `aiosqlite` connection and the engine-level
`asyncio.Lock` write-serialization discipline from Spec 3's
`_run_workflow_txn` helper.

### Decision 2 — Output value shape + per-outcome capture matrix (revised v2 per Codex High 4)

**Architect's lean:** structured dict only (JSON-encoded);
non-serializable outputs surface as friction.

**Codex round 1 High 4** caught that the v1 spec's capture
description ("each action's result persists") was ambiguous across
the five outcome paths the engine actually runs. Resolution: mirror
Spec 3's per-outcome shape so capture is unambiguous per path.

**Per-outcome capture matrix:**

| Outcome                | Envelope `success` | Envelope `value`           | Envelope `error`              | Envelope `receipt`        | Captured? |
|------------------------|--------------------|----------------------------|-------------------------------|---------------------------|-----------|
| Non-gated success      | True               | result.value (dict)        | None                          | result.receipt            | YES       |
| Gated success          | True               | result.value (dict)        | None                          | result.receipt            | YES       |
| Continue-on-failure    | False              | None or partial            | result.error                  | result.receipt (if any)   | YES       |
| Aborting failure       | False              | None or partial            | result.error                  | result.receipt (if any)   | YES       |
| Execute-raised         | False              | None                       | f"execute_raised:..." string  | empty dict                | YES       |
| Verifier-raised        | False              | None                       | f"verify_raised:..." string   | empty dict                | YES       |

**All five outcomes capture an output envelope** (downstream
references may need to see WHY a prior step failed; an
unconditional capture also makes the audit trail more complete).
The per-outcome SQL helpers (Decision 9 + Spec 3's matrix) take a
mandatory envelope payload — no helper writes a record without
also writing the corresponding step output.

**Output envelope shape (adopted with one refinement):** the
captured output is a uniform JSON envelope combining the four
fields of `ActionResult`:

```python
{
    "success": bool,
    "value": dict | str | int | float | bool | list | None,
    "error": str | None,
    "receipt": dict,
}
```

**Reference path semantics:**

| Reference path                          | Resolves to                                |
|-----------------------------------------|--------------------------------------------|
| `{step.X.output.<path>}`                | `result.value.<path>` (value MUST be dict) |
| `{step.X.receipt.<path>}`               | `result.receipt.<path>`                    |
| `{step.X.error}`                        | `result.error` (string)                    |
| `{step.X.success}`                      | `result.success` (boolean)                 |
| `{step.X.value}` (no path)              | `result.value` (any JSON-encodable shape)  |

For the architect's draft pattern
`{step.ask_cc_spec_build.output.request_id}` to work, the
`ask_coding_session` tool must return a dict in `result.value`
(e.g., `{request_id: "...", target: "..."}`). The captured envelope
holds `{success: true, value: {request_id: "...", target: "..."},
receipt: {...}, error: null}`. The reference `output.request_id`
resolves against `value`.

**Non-serializable values:** the engine attempts `json.dumps` on
the envelope. If serialization raises:

- Log loud (`WORKFLOW_STEP_OUTPUT_SERIALIZATION_FAILED`).
- Capture a placeholder envelope:
  `{success: false, value: null, error: "non_serializable:<TypeName>",
  receipt: {}}`.
- Optionally emit a friction signal (composes with Spec 1's catalog
  — if the workflow's failure cascades from this, the friction
  pattern surfaces).

Subsequent reference resolution against this step's output fails
loudly (the value is null; path resolution can't proceed).

**Architect's lean on size cap (adopted):** 64KB per envelope.
Overflow truncates with `truncated: true` row-level marker AND a
loud log (`WORKFLOW_STEP_OUTPUT_TRUNCATED`). Truncation strategy:
serialize the envelope, measure bytes, if > 64KB, truncate the
`value` and `receipt` fields' string representations and replace
with `{ ..., "_truncated": true, "_original_size_bytes": <N>}`.
Subsequent references resolving into truncated sections also fail
loud (the path can't traverse a truncation marker).

### Decision 3 — Reference resolver

**Four reference namespaces:** each gets a prefix-dispatch resolver.

```
{workflow.<key>}      → existing 5-placeholder table (execution_id,
                        gate_nonce, correlation_id, workflow_id,
                        instance_id). Resolution preserved exactly
                        as today.

{idea_payload.<path>} → resolves against the workflow execution's
                        trigger_event_payload (Spec 4b's triggers
                        write this). Path is dotted; resolves into
                        the payload dict.

{step.<id>.<scope>.<path>}
                      → resolves against captured step output
                        envelope. <scope> is one of:
                        output (value), receipt, error, success, value.
                        For output / receipt, <path> walks the dict.

{gate.<name>.output.<path>}
                      → resolves against captured gate-release
                        event payload (Decision 6).
```

**Resolution timing:**

- **Action parameters:** at action dispatch time. Currently
  `_interpolate_params` substitutes the 5 `{workflow.*}`
  placeholders into action parameter values; this spec extends
  that function to handle all four namespaces.
- **Predicate `value:` fields:** at gate evaluation time (each
  satisfying-event candidate). Cached per
  `(execution_id, gate_name)` per execution to avoid repeated
  resolution on every event flush.

**Resolution failure modes:**

| Failure                                       | Timing      | Handling                                                                     |
|-----------------------------------------------|-------------|------------------------------------------------------------------------------|
| Static: `branch_on_true` references unknown ID| Registration| `WorkflowError` from `validate_workflow`; workflow rejected                  |
| Static: parameter references unknown ID        | Registration| `WorkflowError`; workflow rejected                                           |
| Dynamic: referenced step hasn't completed yet  | Dispatch    | `RefResolutionError`; workflow aborts; ActionStateRecord captures the error  |
| Dynamic: path doesn't exist on captured output | Dispatch    | `RefResolutionError`; workflow aborts                                        |
| Dynamic: value is non-dict but path > 0 depth  | Dispatch    | `RefResolutionError`; workflow aborts                                        |
| Dynamic: gate predicate references future step | Evaluation  | Returns False (no match); event passes through; gate stays paused            |

The last row is intentional: a gate predicate that references
`{step.future_step.output.X}` can't match anything until
`future_step` completes. This composes correctly with the request-
and-wait pattern where step K's gate predicate references step K's
own output (the request_id).

**Substitution syntax limitations (v1):**

- Reference forms only. No literal `{` / `}` escaping; values
  containing `{` will be misinterpreted as references and fail
  loud. (Tests pin this.)
- One-level depth at a time. References inside references
  (`{step.X.output.{step.Y.output.path}}`) are NOT resolved
  recursively. (Tests pin failure.)
- String-only substitution. The resolved value gets coerced to its
  natural string representation in parameter substitution (numbers,
  booleans become strings). For predicate evaluation, the resolved
  value's native type is preserved (so `value: '{step.X.output.approved}'`
  comparing against `payload.approved` works with both being bools).

### Decision 4 — Reference resolver implementation surface

**New module:** `kernos/kernel/workflows/refs.py`. Resolves
references against a per-execution context object.

```python
@dataclass
class ResolutionContext:
    """Bundles the per-execution state a resolver needs."""
    execution: WorkflowExecution            # workflow.<key> resolution
    trigger_payload: dict                   # idea_payload.<path>
    step_outputs: dict[str, dict]           # step.<id>.<scope>.<path>
    gate_outputs: dict[str, dict]           # gate.<name>.output.<path>


class RefResolutionError(ValueError):
    """Raised when a template reference can't be resolved."""


def resolve_references_in_string(template: str, ctx: ResolutionContext) -> str:
    """Substitute all references in a string template.
    Raises RefResolutionError on unresolved references."""


def resolve_references_in_value(value: Any, ctx: ResolutionContext) -> Any:
    """Recursively walk a parameter value (dict, list, scalar) and
    substitute references. Preserves native types for non-string
    scalar substitution."""
```

**Engine wiring:**

- `_interpolate_params` (existing) is replaced by
  `resolve_references_in_value` with the existing 5-placeholder
  behavior subsumed.
- Predicate evaluation (in `_on_post_flush_for_gates`) calls
  `resolve_references_in_value` on the predicate AST's `value:`
  fields before invoking `evaluate_predicate`.

### Decision 5 — `branch` verb (revised v2 per Codex Blocker 2 + High 6)

**Architect's lean (adopted):** new `branch` verb, not extension to
`mark_state`.

**Codex round 1 Blocker 2** clarified the semantics: `branch` is a
**goto**, not a skip. After branching to target step K, the engine
continues linearly from K. To produce "if-true-do-A-and-stop;
if-false-do-B-and-stop" semantics, the workflow author pairs
`branch` with `terminal_branches` so that one outcome routes into
a terminal branch (which runs to its end without falling back to
main sequence) and the other continues main sequence.

**Goto semantics example** (matches the architect's draft pattern
from Spec 4):

```yaml
action_sequence:
  - id: ask_cc
    action_type: call_tool
    parameters: {...}
  - id: branch_on_ratification
    action_type: branch
    parameters:
      condition: '{step.await_ratification.gate.output.payload.approved}'
      branch_on_true: ask_cc_implement     # fall-through main sequence
      branch_on_false: terminal:rejected:notify_rejection
  - id: ask_cc_implement
    action_type: call_tool
    parameters: {...}

terminal_branches:
  rejected:
    - id: notify_rejection
      action_type: notify_user
      parameters: {...}
```

When `approved=true`, the workflow continues at `ask_cc_implement`
and runs the rest of main sequence linearly. When `approved=false`,
the workflow switches into terminal branch `rejected` and runs
that branch to completion; main sequence is NOT re-entered.

**Codex round 1 High 6** caught a real safety bug: `bool(value)`
on Python strings like `"false"` or `"0"` returns True because
non-empty strings are truthy. A workflow author writing
`{step.X.output.approved}` against a step that returns the string
`"false"` (e.g., a tool that stringifies its outputs) would route
to the true branch — directly opposite intent.

**Resolution:** require the resolved condition to be a **native
bool**. Non-bool values raise a loud failure that aborts the
workflow. This composes with Decision 3's sole-reference-shortcut
which preserves native types: `{step.X.output.approved}` where
`approved` is `True` resolves to Python `True` (not `"True"`).

**Verb implementation** in `action_library.py`:

```python
class BranchAction:
    """Conditional control flow at workflow level.

    Goto semantics: routes to one of two named step IDs. The engine
    continues linearly from the chosen target. To produce
    skip-downstream semantics, pair with terminal_branches.
    """

    action_type = "branch"

    def __init__(self) -> None:
        # No external deps; branch is pure control-flow.
        pass

    async def execute(self, context: Any, params: dict) -> ActionResult:
        # ``condition`` parameter is already resolved by the engine
        # via resolve_references_in_value before this handler fires.
        # Codex round 1 High 6: require native bool, no coercion.
        condition_value = params.get("condition")
        if not isinstance(condition_value, bool):
            return ActionResult(
                success=False,
                error=(
                    f"branch_condition_not_bool:"
                    f"got {type(condition_value).__name__}={condition_value!r}"
                ),
            )
        target_step_id = (
            params["branch_on_true"] if condition_value
            else params["branch_on_false"]
        )
        return ActionResult(
            success=True,
            value={
                "condition_resolved_to": condition_value,
                "target_step_id": target_step_id,
            },
            receipt={
                "branched_to": target_step_id,
                "condition_value": condition_value,
            },
        )

    async def verify(self, context, params, result):
        # Branch verb has no world-effect to verify. Success means
        # the engine routed correctly. The strict-bool check inside
        # execute() means a False success result is a real failure
        # (caller routes through continuation_rules.on_failure).
        return result.success
```

**Verb parameters (validated at registration):**

- `condition: <reference string>` — REQUIRED. Template reference
  resolving to a **native bool**. Non-bool resolutions abort the
  workflow via continuation_rules (default: abort).
- `branch_on_true: <step_id>` — REQUIRED. Must reference a declared
  step ID in main `action_sequence` or in a `terminal_branches`
  entry (with the `terminal:<name>:<step_id>` syntax).
- `branch_on_false: <step_id>` — REQUIRED. Same constraints.

**Engine sequencing:** the existing `for idx in range(start_idx,
len(wf.action_sequence)):` loop becomes a while loop with an
explicit `next_step_index` pointer. When a `branch` verb succeeds,
the engine inspects the verb's receipt and sets `next_step_index`
to the target step's global ordinal (Decision 0) instead of the
natural `current_step_index + 1`. The `next_step_index` is
persisted to workflow_executions atomically with the branch step's
record commit (see Decision 9's revised state machine for the
durability guarantee).

**Branch target ID resolution:**

| Target ID form                       | Engine action                                                       |
|--------------------------------------|---------------------------------------------------------------------|
| `<step_id>` (bare)                   | Look up step in main `action_sequence` map; set `next_step_index`   |
| `terminal:<branch_name>:<step_id>`   | Look up step in `terminal_branches[<branch_name>]` map; switch sequence (the global step ordinal from Decision 0 makes this a uniform lookup) |

Once a terminal branch is entered, the engine continues running the
terminal branch's action sequence to its end and does NOT return to
the main sequence. The engine knows it's in a terminal branch
because `next_step_index` is within the global ordinal range
assigned to that branch.

**ActionStateRecord composition (Spec 3; revised v2 per Codex Medium 7):**
the branch step's ActionStateRecord captures:

- `surface = "workflow_step"`
- `operation = "branch"`
- `operation_class = "mutate"` (control-flow mutation of next_step_index)
- `risk_level = "medium"` (per Spec 3's `mutate` → `medium` derivation;
  v1 had `low` which contradicted Spec 3)
- `receipt_refs` include `branch_target:<target_step_id>` and the
  resolved condition value
- `user_visible_summary = f"branch evaluated condition={condition_value} → {target_step_id}"`

### Decision 6 — Gate output capture (revised v2 per Codex High 5)

**Codex round 1 High 5** caught that the v1 spec didn't define how
the satisfying event's payload reaches `_clear_gate_and_advance`.
Currently `_await_gate` signals only via `Event.set()`; the matched
event payload isn't preserved anywhere accessible to the
gate-release transaction. Resolution: extend `_await_gate` to
return the matched event payload alongside the continuation bool,
AND populate a per-execution payload buffer that the post-flush
match logic writes before calling `waiter.set()`.

**Event payload handoff path:**

1. `_on_post_flush_for_gates` matches a satisfying event for an
   awaited gate (predicate match + nonce check + execution_id
   binding all pass).
2. Before signalling `waiter.set()`, the hook writes the event's
   payload to `self._gate_release_payloads[execution_id] = event.payload`.
3. `_await_gate` returns `(True, event.payload)` after the waiter
   wakes (reading from the buffer).
4. `_run_action_sequence` threads the payload into
   `_clear_gate_and_advance` which writes it into
   `workflow_step_outputs` atomically with the gate-release SQL.

**Engine method signature changes:**

```python
async def _await_gate(
    self, execution: WorkflowExecution, gate: ApprovalGate,
) -> tuple[bool, dict | None]:
    """v2: returns (continue, matched_event_payload) instead of just
    the continuation bool. payload is None when the gate timed out
    (and the timeout handler chose continuation; e.g.,
    auto_proceed_with_default).
    """
    ...

async def _clear_gate_and_advance(
    self, execution, idx, *, gate_output_payload: dict | None,
) -> None:
    """v2: accepts the matched event payload; threads it into
    _clear_gate_nonce_and_advance which captures it atomically."""
    ...
```

**Per-flush buffer cleanup:** the
`self._gate_release_payloads[execution_id]` entry is read by
`_await_gate` once and then popped, so a subsequent gate on the
same execution doesn't accidentally read a stale payload.
Timeout handlers (auto_proceed_with_default) explicitly bypass the
buffer because no real event matched; in those cases
`_clear_gate_and_advance` is invoked with
`gate_output_payload={"timed_out": True, "default_value": ...}`
synthesized from the gate descriptor.

**Atomic capture inside `_clear_gate_nonce_and_advance`:**

```python
async def _clear_gate_nonce_and_advance(
    db, execution_id, *,
    step_index, expected_nonce="",
    gate_name="",
    gate_output_payload=None,
    instance_id="",   # threaded by the caller for the INSERT
) -> bool:
    """Decision 6 v2: clear gate_nonce + advance cursor + capture
    gate output in one transaction. Gate output INSERT uses the
    SAME table (workflow_step_outputs) with output_kind='gate'."""
    if expected_nonce:
        cursor = await db.execute(
            "UPDATE workflow_executions "
            "SET gate_nonce = '', action_index_completed = ?, "
            "next_step_index = -1, last_heartbeat = ? "
            "WHERE execution_id = ? AND gate_nonce = ?",
            (step_index, _now(), execution_id, expected_nonce),
        )
    else:
        cursor = await db.execute(
            "UPDATE workflow_executions "
            "SET gate_nonce = '', action_index_completed = ?, "
            "next_step_index = -1, last_heartbeat = ? "
            "WHERE execution_id = ?",
            (step_index, _now(), execution_id),
        )
    updated = cursor.rowcount == 1
    if updated and gate_name and gate_output_payload is not None:
        envelope = {
            "success": True,
            "value": {"payload": gate_output_payload},
            "error": None,
            "receipt": {},
        }
        await db.execute(
            "INSERT INTO workflow_step_outputs ("
            " instance_id, workflow_execution_id, output_kind, output_name,"
            " output_json, truncated, recorded_at"
            ") VALUES (?, ?, 'gate', ?, ?, 0, ?) "
            "ON CONFLICT(instance_id, workflow_execution_id, output_kind, output_name) "
            "DO UPDATE SET output_json = excluded.output_json, "
            "recorded_at = excluded.recorded_at",
            (
                instance_id, execution_id, gate_name,
                json.dumps(envelope), _now(),
            ),
        )
    return updated
```

The gate output is referenceable as
`{gate.<gate_name>.output.payload.<path>}` — the wrapping `payload`
key inside `value` makes the access shape uniform with step
outputs (where `value.<path>` accesses the result's value dict).

### Decision 7 — Terminal branches

**Workflow descriptor extension:** new top-level
`terminal_branches:` block.

```yaml
workflow:
  workflow_id: ...
  action_sequence:
    - id: step1
      action_type: ...
    - id: step2
      action_type: branch
      parameters:
        condition: '{step.step1.output.approved}'
        branch_on_true: continue
        branch_on_false: terminal:rejected:notify_rejection
    - id: continue
      action_type: ...
    
  terminal_branches:
    rejected:
      - id: notify_rejection
        action_type: notify_user
        parameters: {...}
      - id: append_rejection_receipt
        action_type: append_to_ledger
        parameters: {...}
```

**Dataclass extension** at `workflow_registry.py`:

```python
@dataclass
class Workflow:
    # ... existing fields ...
    action_sequence: list[ActionDescriptor]
    approval_gates: list[ApprovalGate] = field(default_factory=list)
    terminal_branches: dict[str, list[ActionDescriptor]] = field(default_factory=dict)  # NEW
```

**Descriptor parser extension** at `descriptor_parser.py`:
`_build_workflow` reads optional `terminal_branches:` block;
constructs `list[ActionDescriptor]` per branch_name.

**Validation at registration:**

- Each terminal branch's action list is non-empty.
- Step IDs across `action_sequence` AND all `terminal_branches`
  values are globally unique within the workflow.
- The architect's lean: `branch` verb's `terminal:<branch_name>:<step_id>`
  target references an existing terminal branch + step ID.

**Engine sequencing:**

- Main `action_sequence` execution proceeds as today.
- A `branch` verb targeting `terminal:<branch_name>:<step_id>`
  switches the engine into the terminal branch's action list at
  that step ID and continues to that branch's natural end (the
  last action in the list).
- On reaching the terminal branch's end, the engine emits
  `workflow.execution_terminated` with `outcome = "completed"` and
  a NEW payload field `terminal_branch = <branch_name>` so audit
  consumers can distinguish ratified-vs-rejected paths.

**Engine `terminal_state` column (workflow_executions row):**

The existing terminal_state value set is `completed | aborted`. The
architect's lean: the `terminal_state` column STAYS this two-value
set; the named branch lands in a NEW `terminal_branch` column
(empty string for main-sequence completion or aborts; `<branch_name>`
when entered via terminal branch).

```sql
ALTER TABLE workflow_executions ADD COLUMN terminal_branch TEXT DEFAULT '';
```

Defensive idempotent ALTER (mirrors Spec 3's gate_nonce migration
pattern).

### Decision 8 — Predicate-evaluator template substitution (revised v2 per Codex Medium 8)

**Implementation:** before evaluating each predicate against an
incoming event, walk the predicate AST and substitute references
in any `value:` field via the resolver from Decision 4. Cache
resolved predicates per `(execution_id, gate_nonce)` per execution.

**Codex round 1 Medium 8** caught that the v1 cache key
`(execution_id, gate_name)` was too weak. The same gate name can
be reused within an execution (re-attempted gate after timeout) or
across executions; reusing the cached resolved predicate could
match against a stale value. The gate_nonce is unique per gate
attempt (engine-minted UUID; cleared on resolution), so keying
the cache on `(execution_id, gate_nonce)` makes the cache scope
exactly match the gate-attempt lifetime.

**Cache semantics:**

- First evaluation for a given `(execution_id, gate_nonce)`: walk
  the AST; resolve references; cache the resolved AST.
- Subsequent evaluations on the same gate attempt: reuse cached
  resolved AST (no re-resolution).
- Cache invalidates explicitly on gate release / timeout / abort
  by removing the `(execution_id, gate_nonce)` entry. The nonce
  itself is cleared from `workflow_executions.gate_nonce` in the
  same transaction (Decision 6), so the cache state and the
  substrate state stay aligned.

**Predicate composite operators (AND, OR, NOT):** the resolver
walks recursively. Each `value:` leaf in the AST gets resolved.

**Reference failure during predicate evaluation:**

- If a reference resolves successfully → use the value.
- If a reference fails (e.g., referenced step output is missing) →
  return False from the predicate (the event doesn't match);
  preserve the gate's paused state. The next post-flush hook may
  re-evaluate with updated state; if the referenced output STILL
  isn't available by gate timeout, the gate's bound timeout
  behavior fires.

**This is intentionally NOT a workflow abort.** A gate predicate
referencing future output is a transient state; the predicate
should re-evaluate on the next flush. Loud failure here would
break the request-and-wait pattern (which intentionally references
a not-yet-complete step's output until that step completes).

### Decision 9 — Engine wiring + branch durability (revised v2 per Codex Blocker 1)

**Existing shape:** `for idx in range(start_idx, len(wf.action_sequence)):`
runs steps linearly. The branch verb requires a `next_step_index`
pointer that the engine respects on advancement AND can recover
across restart.

**Codex round 1 Blocker 1** caught a real durability hole: the v1
draft mutated cursor in memory only. After a branch step's commit
(record + cursor advance), if the engine crashed before the
chosen target step started, restart's natural
`action_index_completed + 1` advance would resume past the branch
step at the wrong target. The branch decision lived only in the
branch step's `receipt.branched_to` field — invisible to the
restart-resume pass.

**Resolution:** persist `next_step_index` on
`workflow_executions` atomically with the branch step's
record/cursor commit. Restart-resume reads it before defaulting.
Cleared when the chosen target step begins execution.

**New column on `workflow_executions`:**

```sql
ALTER TABLE workflow_executions ADD COLUMN next_step_index INTEGER DEFAULT -1;
```

Defensive idempotent ALTER (mirrors Spec 3's `gate_nonce` and
this spec's `terminal_branch` migrations). `-1` is the sentinel
meaning "no branch override; advance naturally."

**v2 engine shape:**

```python
async def _run_action_sequence(self, execution, wf):
    # ... existing context build, gate-resume re-entry from Spec 3 ...
    action_sink = self._execution_action_sink(execution)
    gate_by_name = {g.gate_name: g for g in wf.approval_gates}

    # Global step ordinal map for every step ID across main +
    # terminal branches (Decision 0's global step_index).
    step_index_map = self._build_step_index_map(wf)
    # Inverse: global step_index → action descriptor + (in main? terminal branch name)
    action_by_index = self._build_action_by_index(wf)

    # Resolve next step. Codex Blocker 1: branch decision persisted
    # on the execution row takes precedence over natural advance.
    next_step_index = self._resolve_next_step_index(execution, wf)

    while next_step_index is not None:
        action = action_by_index[next_step_index]
        # ... step execution: resolve params + verb.execute + verify ...

        # Determine what comes next:
        if action.action_type == "branch" and step_succeeded:
            target_id = result.receipt.get("branched_to")
            next_step_index = self._resolve_target_step_index(
                target_id, step_index_map,
            )
            # The next_step_index gets persisted INSIDE the per-outcome
            # transaction below (Spec 3 matrix extended).
        elif _terminal_branch_end_reached(next_step_index, wf):
            # End of a terminal branch's sequence → workflow completes.
            next_step_index = None
        else:
            # Natural advance.
            next_step_index = next_step_index + 1
            if next_step_index >= self._total_step_count(wf):
                next_step_index = None  # past end → completion
    await self._complete(execution)

def _resolve_next_step_index(self, execution, wf):
    """At workflow start AND after restart-resume, decide the first
    step to run.

    Restart-resume precedence (Codex Blocker 1 fix):
      1. If next_step_index is set on the row (>= 0), use it — a
         branch decision was persisted.
      2. Otherwise: action_index_completed + 1 (natural advance).
      3. Bounds-check against total step count.
    """
    if execution.next_step_index >= 0:
        return execution.next_step_index
    candidate = execution.action_index_completed + 1
    total = self._total_step_count(wf)
    return candidate if candidate < total else None
```

**Branch verb's per-outcome SQL helper extension:** the branch
step's record-append helper writes `next_step_index` alongside
the cursor advance. New helper:

```python
async def _append_and_advance_with_branch(
    db, sink, record, *,
    step_index, action_type,
    next_step_index_value,
) -> bool:
    """Branch verb's atomic boundary: append record, advance cursor
    to the branch step itself, AND set next_step_index on the
    execution row in one transaction. Restart will read
    next_step_index before defaulting to action_index_completed + 1.
    """
    inserted = await sink._insert_within_txn(...)
    await db.execute(
        "UPDATE workflow_executions "
        "SET action_index_completed = ?, next_step_index = ?, "
        "last_heartbeat = ? "
        "WHERE execution_id = ?",
        (step_index, next_step_index_value, _now(), sink.workflow_execution_id),
    )
    return inserted
```

**`next_step_index` cleared when target step starts:** the
non-branch per-outcome helpers (`_append_and_advance`,
`_append_and_persist_gate_nonce`, `_append_and_abort`) extended to
also clear `next_step_index = -1` in their UPDATE clauses, so the
override expires after the target step's first commit.

**Restart-resume semantics with branch durability:**

| State at crash                                            | Restart resolves to                                  |
|-----------------------------------------------------------|------------------------------------------------------|
| Branch step committed; target step not yet started         | next_step_index set → resume at target step          |
| Branch step + target step committed                        | next_step_index cleared by target's commit → natural advance |
| Pending-gate state (Spec 3)                                 | Spec 3's pending-gate-restart branch                  |
| No branch / no gate; cursor advanced naturally              | natural action_index_completed + 1                    |
| Mid-terminal-branch (no specific durability state)          | Per Decision 9 Option b below: aborts cleanly         |

**Cursor representation:** simplified to a single global
`next_step_index: int`. Eliminates the `(action_list, idx)` tuple
of v1 — the global ordinal handles main + terminal branch lookup
uniformly via the `action_by_index` map.

**Terminal-branch resume policy (preserved from v1 Option b):**
terminal branches are NOT resume-safe in v1. If the engine crashes
mid-terminal-branch (next_step_index falls within a terminal
branch's ordinal range AND no branch override is set AND it's not
the entry point), the execution aborts with
`aborted_by_restart_mid_terminal_branch`. The branch durability
fix above handles the entry-point case (branch chose a terminal;
target step starts running).

**Why terminal branches still aren't resume-safe in v1:** terminal
branches typically run housekeeping / cleanup / notification work
that's idempotent at the workflow-purpose level (the workflow ran
to its useful end before entering the terminal branch). Re-running
the terminal branch from scratch on restart may double-emit
notifications or duplicate ledger appends. v1 prefers the
conservative abort; a future spec can flip to per-step
resume_safe granularity on terminal branches.

### Decision 10 — Composition with Spec 3 ActionStateRecord

**Per-step ActionStateRecord still emits per Spec 3 matrix.**
Specific extensions for the new verbs and reference resolution:

- **`branch` verb:** record's `operation = "branch"`,
  `operation_class = "mutate"`, `risk_level = "low"`,
  `user_visible_summary` describes the condition resolution and
  chosen target. `receipt_refs` include
  `branch_target:<target_step_id>` and
  `condition_value:<value>`.

- **Template-resolved parameter values:** when an action's
  parameters contain resolved references, the record's
  `receipt_refs` capture the resolved values (so audit shows what
  was actually passed). Format:
  `param_resolved:<param_path>:<resolved_value_summary>`.

- **`RefResolutionError` aborts:** the failing step's
  ActionStateRecord captures the unresolved reference in
  `user_visible_summary`. Composes with Spec 3's per-outcome
  matrix's aborting-failure path (`_append_failed_and_abort`).

- **Terminal branch entry:** the branch verb's ActionStateRecord
  carries `terminal_branch:<branch_name>` if the target was a
  terminal-branch entry. Audit can chain from the workflow.* event
  to the branch verb's record to the terminal branch's step
  records.

### Decision 11 — Step output / action record consistency invariant (v2; Codex Medium 10)

**Codex round 1 Medium 10** caught a real desync between this
spec's `ON CONFLICT DO UPDATE` on `workflow_step_outputs` and
Spec 3's `ON CONFLICT DO NOTHING` on `workflow_action_records`.
The append-only audit table (Spec 3) keeps the FIRST observed
record on retry; the runtime-cache step output table (this spec)
overwrites with the LATEST. On a retry path, downstream references
would read the new output value while the audit trail says the
first attempt's value was authoritative.

**Resolution: gate the step output INSERT on the per-outcome
helper's `inserted=True` return.** The Spec 3 per-outcome helpers
already return True iff the action record was newly inserted; the
extended helpers from Decision 9 propagate that return AND skip
the step output INSERT when False:

```python
async def _append_and_advance(
    db, sink, record, *,
    step_index, action_type,
    step_output_envelope=None,
    step_id="",
) -> bool:
    inserted = await sink._insert_within_txn(...)
    # Spec 3: cursor advances regardless of inserted (idempotent on
    # WHERE cursor < step_index).
    await db.execute("UPDATE workflow_executions SET ...")
    if inserted and step_output_envelope is not None and step_id:
        # Spec 4a Decision 11: step output capture co-locates with
        # action record append. Skip on idempotency-skip path so
        # the two tables stay consistent.
        await _capture_step_output(
            db, instance_id=sink.instance_id,
            workflow_execution_id=sink.workflow_execution_id,
            step_id=step_id,
            envelope_json=json.dumps(step_output_envelope),
        )
    return inserted
```

**Invariant: every step output has a corresponding action record
at the same step_index, AND each was inserted by the same
transaction.** Downstream references can rely on this. The
truncation marker and serialization-failure placeholder still
land normally — the consistency invariant is about
NOT-WRITING the step output when the corresponding record was
skipped.

**Gate output INSERT does NOT gate on inserted** (gate outputs
have no corresponding action record; they're keyed by gate_name,
not step_index). The atomicity invariant for gate output is
"captured with the gate release" — which Decision 6's
`_clear_gate_nonce_and_advance` ensures via the same transaction.

## Schema setup order (engine start)

```python
# Engine.start() runs:
await _ensure_schema(self._db)                           # workflow_executions (unchanged)
await ensure_workflow_action_records_schema(self._db)    # Spec 3 — unchanged
await ensure_workflow_step_outputs_schema(self._db)      # NEW: Spec 4a schema
```

The new schema:

- Creates `workflow_step_outputs` table + indexes if absent.
- Runs `ALTER TABLE workflow_executions ADD COLUMN terminal_branch TEXT DEFAULT ''`
  migration (idempotent on duplicate column name).
- Runs `ALTER TABLE workflow_executions ADD COLUMN next_step_index INTEGER DEFAULT -1`
  migration (Codex Blocker 1 fix; same idempotent pattern).

## Code-level shape

### File map

- **NEW:** `kernos/kernel/workflows/refs.py` (~250 LOC) — reference
  resolver. `ResolutionContext`, `resolve_references_in_value`,
  `resolve_references_in_string`, `RefResolutionError`. Validation
  helpers for static reference checking at registration time.

- **NEW:** `kernos/kernel/workflows/step_outputs.py` (~200 LOC) —
  step output store. Schema setup, persistence helpers, reader
  helpers. Composes with Spec 3's `WorkflowActionSink` connection
  ownership pattern.

- **MODIFIED:** `kernos/kernel/workflows/action_library.py` —
  registers new `BranchAction` (~100 LOC addition).

- **MODIFIED:** `kernos/kernel/workflows/workflow_registry.py` —
  `ActionDescriptor` gains `id: str` field; `Workflow` gains
  `terminal_branches: dict[str, list[ActionDescriptor]]` field;
  `validate_workflow` extended with ID uniqueness, branch-target
  reachability, reference well-formedness static checks.

- **MODIFIED:** `kernos/kernel/workflows/descriptor_parser.py` —
  `_build_action` reads optional `id`; `_build_workflow` reads
  optional `terminal_branches` block.

- **MODIFIED:** `kernos/kernel/workflows/predicates.py` — predicate
  evaluator gets pre-resolution step before predicate matching;
  `evaluate_predicate` takes an optional `ResolutionContext`
  parameter; cache resolved AST per gate.

- **MODIFIED:** `kernos/kernel/workflows/execution_engine.py` —
  `_run_action_sequence` rewrites to while-loop with cursor;
  `_interpolate_params` replaced with reference-resolver call;
  step output capture after each handler returns; gate output
  capture in `_clear_gate_and_advance` (extended via Decision 6
  Option a); branch verb dispatch; terminal-branch entry handling.

- **MODIFIED:** `kernos/kernel/workflows/__init__.py` — exports
  the new public surface.

### `workflow_step_outputs` schema setup

```python
_WORKFLOW_STEP_OUTPUTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_step_outputs (
    instance_id             TEXT NOT NULL,
    workflow_execution_id   TEXT NOT NULL,
    output_kind             TEXT NOT NULL CHECK(output_kind IN ('step', 'gate')),
    output_name             TEXT NOT NULL,
    output_json             TEXT NOT NULL,
    truncated               INTEGER NOT NULL DEFAULT 0,
    recorded_at             TEXT NOT NULL,
    PRIMARY KEY (instance_id, workflow_execution_id, output_kind, output_name),
    FOREIGN KEY (workflow_execution_id)
        REFERENCES workflow_executions(execution_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_workflow_step_outputs_kind
    ON workflow_step_outputs (instance_id, workflow_execution_id, output_kind);
"""


async def ensure_workflow_step_outputs_schema(db: aiosqlite.Connection) -> None:
    await db.execute("PRAGMA foreign_keys=ON")
    for stmt in _WORKFLOW_STEP_OUTPUTS_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            await db.execute(stmt)
    # ALTER for terminal_branch column on workflow_executions
    async with db.execute(
        "SELECT name FROM pragma_table_info('workflow_executions')"
    ) as cur:
        cols = {row[0] for row in await cur.fetchall()}
    if "terminal_branch" not in cols:
        try:
            await db.execute(
                "ALTER TABLE workflow_executions "
                "ADD COLUMN terminal_branch TEXT DEFAULT ''"
            )
        except aiosqlite.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
```

### Capture write inside `_run_workflow_txn`

```python
async def _capture_step_output(
    db: aiosqlite.Connection,
    *,
    instance_id: str,
    workflow_execution_id: str,
    step_id: str,
    result: ActionResult,
) -> None:
    """Persist an action result's envelope under the step_id.
    Called inside the engine's per-outcome _run_workflow_txn body
    on success paths."""
    envelope = {
        "success": result.success,
        "value": result.value,
        "error": result.error,
        "receipt": result.receipt,
    }
    try:
        payload = json.dumps(envelope)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "WORKFLOW_STEP_OUTPUT_SERIALIZATION_FAILED execution_id=%s "
            "step_id=%s error=%s",
            workflow_execution_id, step_id, exc,
        )
        envelope = {
            "success": False, "value": None,
            "error": f"non_serializable:{type(exc).__name__}",
            "receipt": {},
        }
        payload = json.dumps(envelope)
    truncated = 0
    if len(payload.encode("utf-8")) > 65536:
        logger.warning(
            "WORKFLOW_STEP_OUTPUT_TRUNCATED execution_id=%s "
            "step_id=%s original_size=%d",
            workflow_execution_id, step_id, len(payload),
        )
        # Truncate value + receipt and re-serialize.
        envelope_value_str = json.dumps(envelope["value"])[:32768]
        envelope_receipt_str = json.dumps(envelope["receipt"])[:8192]
        truncated_envelope = {
            "success": envelope["success"],
            "value": {
                "_truncated": True,
                "_original_size_bytes": len(payload),
                "_partial": envelope_value_str,
            },
            "error": envelope["error"],
            "receipt": {
                "_truncated": True,
                "_partial": envelope_receipt_str,
            },
        }
        payload = json.dumps(truncated_envelope)
        truncated = 1
    await db.execute(
        "INSERT INTO workflow_step_outputs ("
        " instance_id, workflow_execution_id, output_kind, output_name,"
        " output_json, truncated, recorded_at"
        ") VALUES (?, ?, 'step', ?, ?, ?, ?) "
        "ON CONFLICT(instance_id, workflow_execution_id, output_kind, output_name) "
        "DO UPDATE SET output_json = excluded.output_json, "
        "truncated = excluded.truncated, recorded_at = excluded.recorded_at",
        (instance_id, workflow_execution_id, step_id, payload, truncated, _now()),
    )
```

**ON CONFLICT DO UPDATE** is intentional — on resume-retry of the
same step (rare; only triggers on retry or replay), the output
gets refreshed. This is different from Spec 3's
`workflow_action_records` which uses ON CONFLICT DO NOTHING because
audit history is append-only; step outputs are a runtime cache.

### Step + gate output reader

```python
async def load_workflow_outputs(
    db: aiosqlite.Connection,
    instance_id: str,
    workflow_execution_id: str,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Return (step_outputs, gate_outputs) dicts keyed by name.
    Used by _run_action_sequence to build the ResolutionContext.
    """
    step_outputs: dict[str, dict] = {}
    gate_outputs: dict[str, dict] = {}
    async with db.execute(
        "SELECT output_kind, output_name, output_json "
        "FROM workflow_step_outputs "
        "WHERE instance_id = ? AND workflow_execution_id = ?",
        (instance_id, workflow_execution_id),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        try:
            envelope = json.loads(row["output_json"])
        except (json.JSONDecodeError, TypeError):
            envelope = {}
        if row["output_kind"] == "step":
            step_outputs[row["output_name"]] = envelope
        elif row["output_kind"] == "gate":
            gate_outputs[row["output_name"]] = envelope
    return step_outputs, gate_outputs
```

The engine loads this BEFORE each step's parameter resolution so
the most recent outputs are in scope (composes with restart-resume:
on restart, prior-step outputs persist via the table).

### Reference resolver shape (`refs.py`)

```python
_REFERENCE_PATTERN = re.compile(r"\{([^{}]+)\}")


def resolve_references_in_value(
    value: Any, ctx: ResolutionContext,
) -> Any:
    """Recursively walk a parameter value and substitute references.
    
    Type preservation: when the entire value IS a single reference
    (e.g., '{step.X.output.flag}'), the resolved value's native type
    is returned (so '{step.X.output.flag}' → True bool, not 'True' string).
    Mixed strings (e.g., 'prefix-{step.X.output.id}-suffix') return
    a string with the references substituted as str().
    """
    if isinstance(value, str):
        return _resolve_string(value, ctx)
    if isinstance(value, dict):
        return {k: resolve_references_in_value(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_references_in_value(item, ctx) for item in value]
    if isinstance(value, tuple):
        return tuple(resolve_references_in_value(item, ctx) for item in value)
    return value


def _resolve_string(template: str, ctx: ResolutionContext) -> Any:
    """Substitute references in a string template.
    
    Sole-reference shortcut: if the entire template IS a single
    reference, return the resolved native value (preserves type).
    Otherwise: substitute each reference's resolved value as str().
    """
    matches = list(_REFERENCE_PATTERN.finditer(template))
    if not matches:
        return template
    # Sole-reference shortcut.
    if (
        len(matches) == 1
        and matches[0].start() == 0
        and matches[0].end() == len(template)
    ):
        return _resolve_one(matches[0].group(1), ctx)
    # Mixed: stringify each.
    out_parts: list[str] = []
    cursor = 0
    for match in matches:
        out_parts.append(template[cursor:match.start()])
        out_parts.append(str(_resolve_one(match.group(1), ctx)))
        cursor = match.end()
    out_parts.append(template[cursor:])
    return "".join(out_parts)


def _resolve_one(reference: str, ctx: ResolutionContext) -> Any:
    """Resolve a single reference path. Raises RefResolutionError
    if any segment can't resolve."""
    segments = reference.split(".")
    if not segments:
        raise RefResolutionError(f"empty reference: {reference!r}")
    head = segments[0]
    if head == "workflow":
        return _resolve_workflow(segments[1:], ctx)
    if head == "idea_payload":
        return _resolve_path(ctx.trigger_payload, segments[1:], reference)
    if head == "step":
        return _resolve_step(segments[1:], ctx, reference)
    if head == "gate":
        return _resolve_gate(segments[1:], ctx, reference)
    raise RefResolutionError(
        f"unknown reference namespace {head!r} in {reference!r}"
    )


# ... _resolve_workflow, _resolve_step, _resolve_gate, _resolve_path
```

### Engine `_run_action_sequence` rewrite

Pseudocode (illustrative; actual implementation preserves the
existing pending-gate-restart branch and per-outcome Spec 3 matrix):

```python
async def _run_action_sequence(self, execution, wf):
    context = await self._build_context(execution, wf)
    action_sink = self._execution_action_sink(execution)
    gate_by_name = {g.gate_name: g for g in wf.approval_gates}
    step_index_map = self._build_step_index_map(wf)
    
    # Pending-gate-restart branch (Spec 3 preserved)
    if await self._is_pending_gate_resume(execution, wf):
        # ... gate-resume flow ...
    
    # Build initial cursor
    cursor = self._initial_cursor(execution, wf)
    while cursor is not None:
        action_list, idx = cursor
        action = action_list[idx]
        # Resolve references in parameters
        step_outputs, gate_outputs = await load_workflow_outputs(
            self._db, execution.instance_id, execution.execution_id,
        )
        resolve_ctx = ResolutionContext(
            execution=execution,
            trigger_payload=execution.trigger_event_payload,
            step_outputs=step_outputs,
            gate_outputs=gate_outputs,
        )
        try:
            resolved_params = resolve_references_in_value(
                action.parameters, resolve_ctx,
            )
        except RefResolutionError as exc:
            # Spec 3 aborting-failure matrix: append failed record +
            # transition aborted in one txn.
            await self._append_failed_and_abort(
                execution, action_sink, idx, action,
                error=f"ref_resolution_failed:{exc}",
                aborted_reason=f"step_{action.id or idx}_ref_failed",
            )
            # ... emit terminated, return ...
            return
        
        # Existing dispatch: verb.execute + verify + gate handling
        verb = self._action_library.get(action.action_type)
        try:
            result = await verb.execute(context, resolved_params)
        # ... existing exception handling ...
        
        # Step output capture (new)
        if action.id:
            # Capture happens inside the per-outcome transaction
            # body that's already in flight (Spec 3 per-outcome).
            # Threaded through to the SQL helpers in action_sink.py.
            pass
        
        # Advance cursor: branch verb mutates; everything else
        # advances naturally.
        if action.action_type == "branch":
            target_id = result.receipt.get("branched_to")
            cursor = self._cursor_from_target(target_id, wf, step_index_map)
        else:
            cursor = self._advance_cursor(cursor, action_list)
    
    await self._complete(execution)
```

The integration of step-output capture into Spec 3's per-outcome
SQL helpers requires extending each helper to accept an optional
output payload:

```python
async def _append_and_advance(
    db, sink, record, *,
    step_index, action_type,
    step_output_envelope=None,    # NEW
    step_id="",                   # NEW
) -> bool:
    """Decision 6 / Spec 3 helper extended to also capture step output."""
    inserted = await sink._insert_within_txn(...)
    await db.execute("UPDATE workflow_executions SET action_index_completed = ?...")
    if step_output_envelope is not None and step_id:
        await _capture_step_output(
            db, instance_id=sink.instance_id,
            workflow_execution_id=sink.workflow_execution_id,
            step_id=step_id, result_envelope=step_output_envelope,
        )
    return inserted
```

Similar extensions for `_append_and_persist_gate_nonce` and
`_append_and_abort`.

### Gate output capture (Decision 6)

Extend `_clear_gate_nonce_and_advance` to take an optional
`gate_output_payload`:

```python
async def _clear_gate_nonce_and_advance(
    db, execution_id, *,
    step_index, expected_nonce="",
    gate_name="",              # NEW
    gate_output_payload=None,  # NEW
) -> bool:
    """Decision 6 extension: also capture gate output in same txn."""
    if expected_nonce:
        cursor = await db.execute(
            "UPDATE workflow_executions "
            "SET gate_nonce = '', action_index_completed = ?, last_heartbeat = ? "
            "WHERE execution_id = ? AND gate_nonce = ?",
            (step_index, _now(), execution_id, expected_nonce),
        )
    else:
        cursor = await db.execute(
            "UPDATE workflow_executions "
            "SET gate_nonce = '', action_index_completed = ?, last_heartbeat = ? "
            "WHERE execution_id = ?",
            (step_index, _now(), execution_id),
        )
    updated = cursor.rowcount == 1
    if updated and gate_name and gate_output_payload is not None:
        # Capture gate output in the same transaction.
        await db.execute(
            "INSERT INTO workflow_step_outputs ("
            " instance_id, workflow_execution_id, output_kind, output_name,"
            " output_json, truncated, recorded_at"
            ") VALUES (?, ?, 'gate', ?, ?, 0, ?) "
            "ON CONFLICT(instance_id, workflow_execution_id, output_kind, output_name) "
            "DO UPDATE SET output_json = excluded.output_json, "
            "recorded_at = excluded.recorded_at",
            (
                instance_id_for_execution(execution_id),  # resolved via prior load
                execution_id, gate_name,
                json.dumps(gate_output_payload), _now(),
            ),
        )
    return updated
```

Where the satisfying event's payload is captured by the post-flush
match logic and threaded through to `_clear_gate_and_advance` via
a new parameter.

## Embedded live tests

Substrate-fidelity assertion pattern: every test verifies substrate
state directly (`workflow_step_outputs` rows, action records' resolved
parameter values, terminal_branch column population), not just
behavioral outputs.

### Test category 1: Step output capture + reference resolution

`tests/test_workflow_orchestration_primitives.py::TestStepOutputAndReference`

1. **`test_step_output_persisted_with_envelope_shape`** — workflow
   with two steps; step 1's verb returns
   `ActionResult(success=True, value={"k": 42}, receipt={"r": 1})`;
   verify `workflow_step_outputs` row exists with `output_name = step1.id`
   and the JSON envelope contains `{success: true, value: {k: 42}, ...}`.

2. **`test_step_output_serialization_failure_captures_placeholder`** —
   step's verb returns non-JSON-serializable value (a function, a set);
   verify the captured envelope is the failure-placeholder shape;
   verify the WARNING log fires.

3. **`test_step_output_truncation_marker`** — step's verb returns
   value larger than 64KB; verify captured row has
   `truncated = 1`; verify the value field contains the truncation
   marker.

4. **`test_step_output_persists_across_restart`** — workflow runs
   step 1; engine stops; engine restarts; verify `workflow_step_outputs`
   row still present; verify a workflow that references step 1's
   output via `{step.step1.output.X}` can re-resolve the reference.

5. **`test_reference_resolution_in_action_parameters`** —
   step 2's parameter `args.id` is `'{step.step1.output.request_id}'`;
   step 1's value contains `{"request_id": "abc"}`; verify step 2's
   verb receives `args={"id": "abc"}` as the resolved parameter.

6. **`test_reference_sole_reference_preserves_type`** — step 1's
   value contains `{"approved": true}` (bool); step 2's parameter
   `condition` is `'{step.step1.output.approved}'`; verify step 2's
   verb receives a bool (True), not the string `"true"`.

7. **`test_reference_mixed_string_substitution`** — step 2's
   parameter `path` is `'prefix-{step.step1.output.id}-suffix'`;
   verify the resolved value is the concatenated string.

8. **`test_reference_failure_aborts_workflow`** — step 2 references
   `{step.step99.output.X}` (nonexistent step); verify workflow
   aborts with `ref_resolution_failed`; verify ActionStateRecord
   captures the unresolved reference.

9. **`test_reference_to_idea_payload`** — workflow's trigger event
   payload is `{"description": "test"}`; step 1's parameter
   `args.text` is `'{idea_payload.description}'`; verify resolution.

10. **`test_reference_to_workflow_placeholder_backward_compat`** —
    existing `{workflow.execution_id}` placeholder works as before;
    pin backward-compatibility.

### Test category 2: Predicate-evaluator template substitution

`tests/test_workflow_orchestration_primitives.py::TestPredicateSubstitution`

1. **`test_gate_predicate_resolves_step_reference`** — workflow with
   gated step; gate predicate is
   `op: eq, path: payload.request_id, value: '{step.ask.output.request_id}'`;
   step `ask` returns `value={"request_id": "abc"}`; emit event
   with `payload.request_id = "abc"`; verify gate releases.

2. **`test_gate_predicate_no_match_when_request_id_mismatch`** —
   same setup; emit event with `payload.request_id = "wrong"`;
   verify gate stays paused.

3. **`test_gate_predicate_no_match_when_reference_pending`** — gate
   predicate references future step output; emit candidate event
   before the referenced step has completed; verify predicate
   returns False (not raises); verify gate stays paused.

4. **`test_predicate_cache_per_gate`** — gate predicate references
   step output; first event triggers resolution; second event with
   matching payload uses cached resolution; verify only one
   `workflow_step_outputs` SELECT fires.

### Test category 3: `branch` verb (revised v2 per Codex Blocker 2 + High 6)

`tests/test_workflow_orchestration_primitives.py::TestBranchVerb`

1. **`test_branch_goto_to_true_target`** — workflow has step1
   (returns `value={"approved": true}`), step2 (branch on `approved`,
   `branch_on_true: step3`, `branch_on_false: terminal:rejected:r1`),
   step3 (main fall-through), step4 (main fall-through). Verify the
   engine goes to step3, then step4 (linear fall-through; branch is
   goto, not skip). False branch test below verifies the terminal
   path.

2. **`test_branch_to_terminal_false_routes_into_branch`** — same
   setup as test 1; step1 returns `value={"approved": false}`.
   Verify execution enters terminal branch `rejected` at step `r1`
   and runs to that branch's end; verify step3 and step4 in main
   sequence are NOT visited; verify
   `workflow_executions.terminal_branch = "rejected"`.

3. **`test_branch_native_bool_only_aborts_on_string`** — step1
   returns `value={"approved": "true"}` (string, not bool); step2
   branches on it. Verify the branch verb's execute returns
   `success=False` with `error="branch_condition_not_bool:..."`;
   verify continuation_rules.on_failure="abort" path routes to
   aborting failure (Decision 9's _append_failed_and_abort).

4. **`test_branch_action_state_record_captures_target`** — after
   branch fires successfully, the ActionStateRecord for the branch
   step has `operation = "branch"`, `operation_class = "mutate"`,
   `risk_level = "medium"` (Codex Medium 7), `receipt_refs`
   includes `branch_target:<target_step_id>` and
   `condition_value:<bool>`, `user_visible_summary` describes the
   choice.

5. **`test_branch_durability_across_restart`** — Codex Blocker 1:
   simulate a crash AFTER the branch step's transaction commits
   (record persisted + cursor advanced + `next_step_index` set) but
   BEFORE the target step runs. Restart the engine; verify the
   engine resumes at the chosen target step (via
   `_resolve_next_step_index` reading `next_step_index`), NOT at
   `action_index_completed + 1`.

6. **`test_branch_next_step_index_cleared_by_target_commit`** —
   verify that when the target step's per-outcome SQL helper runs
   (`_append_and_advance` etc.), it also clears
   `next_step_index = -1` so a subsequent crash + restart resumes
   naturally.

7. **`test_branch_validation_at_registration`** — workflow with
   branch verb whose target references unknown step ID; verify
   `validate_workflow` raises `WorkflowError` with the dangling
   reference name.

### Test category 4: Terminal branches

`tests/test_workflow_orchestration_primitives.py::TestTerminalBranches`

1. **`test_terminal_branch_runs_to_completion`** — main sequence
   has a branch to `terminal:rejected:notify_step`; the terminal
   branch has 2 steps; verify both steps run; verify the engine
   emits `workflow.execution_terminated` with
   `outcome = "completed"` AND `terminal_branch = "rejected"`.

2. **`test_terminal_branch_id_uniqueness_validation`** — workflow
   defines step with `id="dup"` in main sequence and another step
   with `id="dup"` in a terminal branch; verify registration
   raises `WorkflowError("duplicate step id")`.

3. **`test_terminal_branch_not_resume_safe`** — workflow enters a
   terminal branch; engine crashes; engine restarts; verify the
   execution aborts with `aborted_by_restart` AND a message
   indicating it was mid-terminal-branch (Option b semantics from
   Decision 9).

### Test category 5: Gate output capture

`tests/test_workflow_orchestration_primitives.py::TestGateOutputCapture`

1. **`test_gate_output_captured_on_release`** — workflow with gated
   step; gate releases on event with `payload={"approved": true, "by": "user"}`;
   verify `workflow_step_outputs` row exists with
   `output_kind='gate'`, `output_name=<gate_name>`, envelope
   containing the event payload nested under `value.payload`
   (Decision 6 v2 shape).

2. **`test_gate_output_referenced_by_subsequent_step`** — subsequent
   step's parameter is `'{gate.<gate_name>.output.payload.approved}'`;
   verify the step's verb receives the resolved bool.

3. **`test_gate_output_capture_atomic_with_release`** — simulate a
   crash AFTER `_clear_gate_and_advance` returns but BEFORE next
   step runs; restart; verify gate_nonce cleared, cursor advanced,
   AND gate output row present (proof of atomic transaction).

4. **`test_await_gate_returns_payload`** — Codex High 5: pin the
   `_await_gate` v2 signature returns `(True, matched_payload)`;
   the `self._gate_release_payloads[execution_id]` buffer gets
   populated by `_on_post_flush_for_gates` before `waiter.set()`;
   `_await_gate` reads + pops it after wake.

5. **`test_await_gate_timeout_synthesizes_payload`** — gate with
   `auto_proceed_with_default`; gate times out; verify
   `_clear_gate_and_advance` receives the synthesized payload
   `{timed_out: True, default_value: ...}` and captures it under
   the gate's output_name; subsequent reference resolves to the
   synthesized values.

### Test category 6: Per-outcome step output capture (Codex High 4)

`tests/test_workflow_orchestration_primitives.py::TestPerOutcomeOutputCapture`

1. **`test_capture_on_non_gated_success`** — successful step's
   envelope persists with `success=True`, value, receipt.

2. **`test_capture_on_gated_success`** — gated step's envelope
   persists alongside `output_kind='gate'` for the gate; verify
   both rows present.

3. **`test_capture_on_continue_failure`** — continue-failure
   step's envelope persists with `success=False`, error string,
   any partial receipt; cursor advances per Spec 3 matrix.

4. **`test_capture_on_aborting_failure`** — aborting-failure
   step's envelope persists with `success=False`, error string;
   execution transitions to aborted state atomically (Spec 3 +
   this spec).

5. **`test_capture_on_execute_raised`** — verb raises during
   execute; envelope persists with `success=False`,
   `error="execute_raised:<ExcType>:<msg>"`, empty receipt.

6. **`test_consistency_invariant_on_idempotency_skip`** — Codex
   Medium 10: on retry of the same step (ON CONFLICT DO NOTHING
   on action record), the corresponding workflow_step_outputs
   INSERT must SKIP too. Verify the step output keeps the
   original value (matching the original action record), not the
   retry's value.

### Test category 7: ID grammar + reference parsing (Codex Medium 9)

`tests/test_workflow_orchestration_primitives.py::TestIdentifierGrammar`

1. **`test_step_id_grammar_validated`** — step with id="bad.id"
   (contains dot) fails registration with `WorkflowError`
   referencing the grammar pattern.

2. **`test_gate_name_grammar_validated`** — gate with
   gate_name="bad:gate" fails registration.

3. **`test_terminal_branch_name_grammar_validated`** — terminal
   branch with name="bad name" (whitespace) fails registration.

4. **`test_valid_grammars_accepted`** — IDs like `ask_cc`,
   `step-3`, `Branch1` (alphanumeric + hyphen + underscore + mixed
   case) all validate.

### Test category 8: Composition with Spec 3

`tests/test_workflow_orchestration_primitives.py::TestSpec3Composition`

1. **`test_branch_verb_emits_action_state_record`** — branch step
   produces an ActionStateRecord via the Spec 3 per-outcome matrix;
   verify the record's `operation = "branch"`, `receipt_refs`
   include `branch_target` and `condition_value`.

2. **`test_resolved_parameters_visible_in_action_state_record`** —
   step's parameter references step1's output; the resolved
   parameter values land in the action record's `receipt_refs` as
   `param_resolved:<param_path>:<value_summary>`.

3. **`test_ref_resolution_failure_routes_through_abort_matrix`** —
   when reference resolution fails mid-workflow, the engine routes
   through Spec 3's aborting-failure path
   (`_append_failed_and_abort`); the ActionStateRecord captures
   the resolution error; the execution transitions to aborted state
   atomically.

## Open architectural questions (CC surfaces transparently)

Five questions for Codex pre-spec review + architect to land
before ratification:

1. **Output envelope value type — is `value` allowed to be non-dict?**
   The reference path `{step.X.output.<sub>}` requires `value` to
   be a dict for path traversal. But `value` can naturally be a
   scalar (bool, int, string) or list. Two options:

   - **Strict:** `value` must be a dict to be a reference target;
     scalar values are accessible only via `{step.X.value}` (no
     `.output.` namespace).
   - **Permissive:** `{step.X.output}` resolves to whatever `value`
     is; `{step.X.output.<path>}` only works if `value` is a dict.

   CC's lean: **permissive**. Matches Python attribute access
   semantics; failure on path access against scalar is loud-enough.

2. **Reference resolution timing — strict early or lazy?**
   Lazy: resolve only at the point a step's parameter is being
   dispatched. Strict: resolve all references at workflow start
   (against expected step outputs) and cache. Lazy is what I've
   drafted. Strict is more upfront-safe but requires the workflow
   to declare expected output schemas (which it doesn't currently
   do).

   CC's lean: **lazy**. Strict requires a schema declaration
   primitive that's out of scope for v1.

3. **Conditional control flow — branch verb only, or also skip?**
   The branch verb routes to a different step. Sometimes a workflow
   wants to skip a step entirely (e.g., "if condition X, skip
   step Y"). This is achievable today via branch + redirecting to
   the post-Y step, but the workflow author has to know step Y's
   successor's ID. A `skip_if` decorator on each step would be
   cleaner. v1 scope: branch only.

   CC's lean: **branch only for v1**; skip_if is a future extension
   if soak shows the need.

4. **Terminal branch nesting — can a terminal branch contain a
   branch verb to ANOTHER terminal branch?** Useful for nested
   error handling. Pure flat terminal_branches is simpler.

   CC's lean: **flat for v1**. Terminal branches cannot contain
   branch verbs targeting other terminal branches. Branches WITHIN
   a terminal branch can advance to other steps in the SAME
   terminal branch only. Validation at registration enforces this.

5. **Step output size cap — 64KB hard or configurable?**
   v1 is 64KB hardcoded. Codex review of a large workflow might
   produce findings text exceeding this; the truncation marker
   handles it but the workflow's downstream steps lose access to
   the full content. Configurable per-step (`output_size_cap:`
   on the action descriptor)? Or per-workflow?

   CC's lean: **64KB hardcoded for v1**; per-step cap is a future
   extension. The truncation marker plus the friction signal
   surface the limit clearly enough that workflows hitting it can
   request a redesign.

## Risks and design constraints

| Risk                                                              | Mitigation                                                                                                                                                                                                                                              |
|-------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Schema migration on `workflow_executions` (terminal_branch column) | Defensive idempotent ALTER pattern (mirrors Spec 3's gate_nonce migration).                                                                                                                                                                              |
| Step output capture cost (extra DB write per step)                 | Single INSERT per step; co-located connection means it's amortized against existing per-outcome transaction work (Spec 3 already writes 2-3 statements per step). Single-statement helper inside `_run_workflow_txn` body keeps the boundary atomic.    |
| Non-serializable step outputs                                       | Placeholder envelope + WARNING log + friction surface. Workflow doesn't abort on capture failure; downstream references can still fail loud, which is the intended signal.                                                                              |
| Truncation losing data                                              | Truncation marker present in envelope; full content surfaces via friction report; subsequent references into truncated regions fail loud.                                                                                                               |
| Cyclic branches (branch to step that branches back)                | Workflow registration validation: detect cycles via DAG traversal of branch targets; reject at registration time.                                                                                                                                       |
| RefResolutionError abort cascades (one step's failure aborts the workflow) | Loud-fail discipline aligns with Spec 3's per-outcome aborting-failure matrix. Audit trail is intact; operator can diagnose via ActionStateRecord + friction report.                                                                                    |
| Predicate cache invalidation                                        | Cache lifetime: tied to the gate's life. Cleared when gate is released or workflow terminates. If a referenced step's output ever changes mid-execution (which shouldn't happen in v1), cache becomes stale; defensive: invalidate on every step write. |
| Cross-step reference graph at registration                          | Static validation: ensure every `{step.<id>...}` reference's target step exists. Runtime: the engine builds the graph at workflow load time, not per-execution.                                                                                          |
| Conflict with Spec 3's per-outcome transaction matrix               | Each per-outcome SQL helper extended to accept optional step_output capture payload; the capture is part of the SAME transaction as the record append + state mutation. Atomic boundary preserved.                                                      |
| Terminal branch not resume-safe (Decision 9 Option b)              | Restart of a mid-terminal-branch execution aborts cleanly with a clear message. If soak shows the abort rate is high, Decision 9 can flip to Option a (full resume support).                                                                            |
| Branch decisions not durable across restart (Codex Blocker 1)       | v2 Decision 9 adds `next_step_index` column on workflow_executions, written atomically with the branch step's record commit; restart-resume reads it before defaulting to `action_index_completed + 1`; cleared by the target step's commit.            |
| Terminal branch step_index PK collision with Spec 3 (Codex Blocker 3)| v2 Decision 0 introduces a global step ordinal assigned at workflow registration; step_index is unique across main + terminal branches; Spec 3's `workflow_action_records` PK works unmodified.                                                          |
| Branch verb boolean coercion routes "false" to true (Codex High 6)   | v2 Decision 5 requires native bool only; non-bool resolutions surface as `branch_condition_not_bool` failures and route through continuation_rules.                                                                                                    |
| Step output and action record desync on retry (Codex Medium 10)      | v2 Decision 11 gates step output INSERT on the per-outcome helper's `inserted=True` return; idempotency-skip on the record also skips the output, keeping the two tables consistent.                                                                  |
| Gate output payload handoff (Codex High 5)                           | v2 Decision 6 extends `_await_gate` to return the matched event payload; the engine threads it into `_clear_gate_and_advance` for atomic capture.                                                                                                      |
| Predicate cache key weakness (Codex Medium 8)                        | v2 Decision 8 keys cache on `(execution_id, gate_nonce)`; nonce is per-attempt UUID; cache invalidates on release / timeout / abort.                                                                                                                   |
| Reference parsing ambiguity from `.` or `:` in IDs (Codex Medium 9)  | v2 Decision 0 validates step / gate / terminal-branch identifiers against `[A-Za-z][A-Za-z0-9_-]*` at registration.                                                                                                                                  |

## Sequence (per architect directive)

1. ✅ Architect-framed (Notion `35effafef4db8168855eeb2524d2ff4e`,
   build directive after Spec 4 verdict).
2. ✅ CC drafts spec v1 at `specs/WORKFLOW-ORCHESTRATION-PRIMITIVES-V1.md`
   on branch `workflow-orchestration-primitives-v1`
   (commit `43616a2`).
3. ✅ **Codex pre-spec review round 1** — 10 findings: 3 blockers,
   3 high, 4 medium. All implementation-surface; none challenged
   the architectural shape.
4. ✅ **CC folds Codex round 1** into spec v2 (this revision).
5. 🟡 **Codex pre-spec review round 2** (if architect requests) —
   verifies implementation surface of the v2 folds, particularly
   the branch durability + global step ordinal + per-outcome
   output capture matrix.
6. Architect ratification of v-final spec body (potentially with
   architect calls on the 5 open architectural questions surfaced
   below).
7. CC implements per ratified spec.
8. Codex post-implementation review.
9. CC any final changes.
10. Architect ratifies on close; merge to `main`.
11. **Spec 4b** — architect rewrites the self-improvement workflow
    YAML against this spec's primitives. CC pre-spec review
    (substrate composition only). Architect ratifies. CC
    implements (including production wiring sequence). Codex
    post-impl review. Architect ratifies on close.
12. First end-to-end autonomy loop run.

**Founder direction pending architect re-framing:** founder
indicated Kernos-authored workflows are essential and inherent to
the substrate (not a follow-up capability). CC filed the direction
to architect inbox at Notion
`35effafef4db8111be97c985c1d5ac33` with three options (A: expand
4a in place / B: insert 4a+ for authoring substrate / C:
restructure). The Codex round-1 folds in this commit are
orthogonal to that direction (they fix execution-side correctness
that any framing needs). Architect's framing call resolves whether
this spec's scope expands.

## Linked artifacts

- Architect build directive: Notion
  `35effafef4db8168855eeb2524d2ff4e`
- CC pre-spec review of Spec 4 (the gaps that drove the split):
  Notion `35effafef4db81dcbc0cf432b425fb24`
- Architect verdict on Spec 4: Notion
  `35effafef4db817ab773de6a059f9fde`
- PHASE-3-AUTONOMY-LOOP design consideration: Notion
  `35cffafef4db81da8107e562307bc738`
- Five-spec roadmap: Notion `35cffafef4db81c0b855cb0984dcd8df`
- Workflow primitive substrate review (current substrate state):
  Notion `35cffafef4db81f4a344e05ca9a2c9a8`
- Spec 1 close ratification (FRICTION-PATTERN-STABLE-IDS-V1):
  Notion `35dffafef4db818c982af6d7e69c5948`
- Spec 2 (CODING-SESSION-BRIDGE-V1): `specs/CODING-SESSION-BRIDGE-V1.md`
- Spec 3 (ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1):
  `specs/ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1.md`
- Spec 3 close ratification: Notion
  `35effafef4db8103ac6afe2d13da40a6`
- Workflow primitive code: `kernos/kernel/workflows/`
