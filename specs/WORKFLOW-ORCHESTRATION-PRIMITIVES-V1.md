# WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 — Implementation Spec

**Status:** DRAFT v1 — pre-implementation. Awaiting Codex pre-spec
review.

**Author:** CC, 2026-05-12. Resolves architect's framing in the
Spec 4a build directive at Notion
`35effafef4db8168855eeb2524d2ff4e`. Substrate gaps in the original
Spec 4 draft (CC pre-spec review at Notion
`35effafef4db81dcbc0cf432b425fb24`) drove the Option B split:
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

### Decision 0 — Step IDs on ActionDescriptor (CC-surfaced addition)

**Architect's framing did not name this explicitly,** but every
other extension assumes step IDs exist. The current
`ActionDescriptor` at `workflow_registry.py:135` has no `id` field;
steps are identified by their index in `action_sequence`. To
reference a prior step's output via `{step.<id>.output.<path>}`,
the step needs a stable string identifier.

**Resolution:** add an optional `id: str` field to
`ActionDescriptor`.

```python
@dataclass
class ActionDescriptor:
    action_type: str
    parameters: dict
    per_action_expectation: str = ""
    continuation_rules: ContinuationRules = field(default_factory=ContinuationRules)
    gate_ref: str | None = None
    resume_safe: bool = False
    id: str = ""  # NEW: optional, but required if this step is a reference target
```

**Validation at registration (extends `validate_workflow`):**

- Empty `id` allowed (legacy workflows continue to work).
- Non-empty IDs must be unique within the workflow, including
  across `terminal_branches` action lists.
- Branch verb's `branch_on_true` / `branch_on_false` targets must
  reference declared IDs.
- Template references `{step.<id>...}` to non-existent IDs are
  flagged at registration (static check); references to existing
  IDs whose output hasn't been captured at resolve time produce
  runtime errors (dynamic check).

**Backward compatibility:** existing workflows without `id` fields
continue to validate and execute. The reference resolver simply
can't target them — by construction, since their reference syntax
requires an ID.

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

### Decision 2 — Output value shape

**Architect's lean:** structured dict only (JSON-encoded);
non-serializable outputs surface as friction.

**Resolution (adopted with one refinement):** the captured output
shape is a uniform JSON envelope combining the four fields of
`ActionResult`:

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

### Decision 5 — `branch` verb

**Architect's lean (adopted):** new `branch` verb, not extension to
`mark_state`.

**Verb implementation** in `action_library.py`:

```python
class BranchAction:
    """Conditional control flow at workflow level.
    
    Reads ``condition`` parameter (resolved via template syntax),
    coerces to bool. The engine's _run_action_sequence reads the
    chosen target from the receipt and updates next_idx pointer.
    """

    action_type = "branch"

    def __init__(self) -> None:
        # No external deps; branch is pure control-flow.
        pass

    async def execute(self, context: Any, params: dict) -> ActionResult:
        # ``condition`` parameter is already resolved by the engine
        # via resolve_references_in_value before this handler fires.
        condition_value = params.get("condition")
        # Coerce to bool: explicit True/False win; truthy/falsy 
        # values follow Python semantics with an explicit cast.
        chosen_branch = bool(condition_value)
        target_step_id = (
            params["branch_on_true"] if chosen_branch
            else params["branch_on_false"]
        )
        return ActionResult(
            success=True,
            value={
                "condition_resolved_to": chosen_branch,
                "target_step_id": target_step_id,
            },
            receipt={
                "branched_to": target_step_id,
                "condition_value": condition_value,
            },
        )

    async def verify(self, context, params, result):
        # Branch verb has no world-effect to verify. Success means
        # the engine routed correctly.
        return result.success
```

**Verb parameters (validated at registration):**

- `condition: <reference string>` — REQUIRED. Template reference
  resolving to a boolean-coercible value.
- `branch_on_true: <step_id>` — REQUIRED. Must reference a declared
  step ID in main `action_sequence` or in a `terminal_branches`
  entry (with the `terminal:<name>:<step_id>` syntax).
- `branch_on_false: <step_id>` — REQUIRED. Same constraints.

**Engine sequencing:** the existing `for idx in range(start_idx,
len(wf.action_sequence)):` loop becomes a while loop with an
explicit `next_idx` pointer. When a `branch` verb succeeds, the
engine inspects the verb's receipt and sets `next_idx` to the
target step's index instead of the natural `idx + 1`.

**Branch target ID resolution:**

| Target ID form                       | Engine action                                                       |
|--------------------------------------|---------------------------------------------------------------------|
| `<step_id>` (bare)                   | Look up step in main `action_sequence`; set `next_idx`              |
| `terminal:<branch_name>:<step_id>`   | Look up step in `terminal_branches[<branch_name>]`; switch sequence |

Once a terminal branch is entered, the engine continues running the
terminal branch's action sequence to completion (or abort) and does
NOT return to the main sequence.

**ActionStateRecord composition (Spec 3):** the branch step's
ActionStateRecord captures:

- `surface = "workflow_step"`
- `operation = "branch"`
- `operation_class = "mutate"` (control-flow mutation)
- `risk_level = "low"` (no world effect; cheap to revert by replay)
- `receipt_refs` include `branch_target:<target_step_id>` and the
  resolved condition value
- `user_visible_summary = f"branch evaluated condition={condition_value} → {target_step_id}"`

### Decision 6 — Gate output capture

**Implementation:** in `_await_gate`'s post-resume path. The
satisfying event's payload gets captured into
`workflow_step_outputs` with `output_kind='gate'`,
`output_name=<gate_name>`.

**Capture happens AFTER gate release confirmation** (i.e., after
the post-flush hook has matched the event and signalled the
waiter), so capture is synchronous with the release. Inside the
`_clear_gate_and_advance` transaction from Spec 3, the gate-output
INSERT lands too — but the existing transaction shape doesn't
include this. Either:

**Option a (lean):** extend `_clear_gate_nonce_and_advance` to take
an optional `gate_output_payload` argument and INSERT it into
`workflow_step_outputs` in the same transaction.

**Option b:** capture the gate output in a separate
`_run_workflow_txn` body BEFORE the gate-release transaction.

Decision: **Option a.** Single transaction is cleaner for the
crash-window invariant (gate release and gate output land
atomically; restart sees consistent state). Slightly more SQL in
the helper but the contract is tighter.

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

### Decision 8 — Predicate-evaluator template substitution

**Implementation:** before evaluating each predicate against an
incoming event, walk the predicate AST and substitute references
in any `value:` field via the resolver from Decision 4. Cache
resolved predicates per `(execution_id, gate_name)` per execution.

**Cache semantics:**

- First evaluation: walk the AST; resolve references; cache the
  resolved AST.
- Subsequent evaluations on the same gate: reuse cached resolved
  AST.
- Cache invalidates when the gate is cleared (gate-release) — but
  by then the gate is gone, so this is a defensive cleanup, not a
  correctness requirement.

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

### Decision 9 — Engine wiring (the `_run_action_sequence` rewrite)

**Existing shape:** `for idx in range(start_idx, len(wf.action_sequence)):`
runs steps linearly. The branch verb requires a `next_idx` pointer
that the loop body can mutate.

**v1 shape:**

```python
async def _run_action_sequence(self, execution, wf):
    # ... existing context build, gate-resume re-entry from Spec 3 ...
    action_sink = self._execution_action_sink(execution)
    gate_by_name = {g.gate_name: g for g in wf.approval_gates}
    # New: index lookup for all step IDs (main + terminal branches)
    step_index_map = self._build_step_index_map(wf)
    # New: starting cursor — main sequence OR terminal branch
    cursor = self._initial_cursor(execution, wf)
    while cursor is not None:
        action_list, idx = cursor
        action = action_list[idx]
        # ... step execution as before ...
        # After step runs (success or continue-failure), determine next:
        if action.action_type == "branch":
            target_id = result.receipt.get("branched_to")
            cursor = self._cursor_from_target(target_id, wf, step_index_map)
        else:
            cursor = self._advance_cursor(cursor, action_list)
    await self._complete(execution)
```

**Cursor representation:** `(action_list, idx)` tuple where
`action_list` is either `wf.action_sequence` or a
`wf.terminal_branches[branch_name]` list. `idx` is the index within
that list.

**`_advance_cursor(cursor, action_list)`:** returns
`(action_list, idx + 1)` if `idx + 1 < len(action_list)`, else
`None` (end-of-sequence; workflow completes).

**`_cursor_from_target(target_id, wf, step_index_map)`:** parses
the target ID:

- Bare `<step_id>` → look up in main sequence map.
- `terminal:<branch_name>:<step_id>` → look up in terminal branches
  map; returns `(terminal_branches[branch_name], idx)`.

**Restart-resume compatibility:** the existing
`action_index_completed` cursor is preserved AS-IS for
backward-compatibility. But terminal branches and branch verbs add
complexity here. Two options:

**Option a:** add a `terminal_branch` column (already added per
Decision 7) and a `cursor_within_branch` column to track per-branch
position on restart.

**Option b:** the simpler v1 model: terminal branches are
explicitly NOT resume-safe in v1. Restart-resume always returns to
the main sequence; if the execution was in a terminal branch when
the engine crashed, the execution aborts with a clear message.

**Decision: Option b.** Simpler v1 implementation; matches the
"resume_safe defaults to False" conservative posture of the
existing primitive. If a future spec needs terminal-branch resume,
it extends here. The architect can override to Option a if soak
shows the simplification is wrong.

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

## Schema setup order (engine start)

```python
# Engine.start() runs:
await _ensure_schema(self._db)                           # workflow_executions (unchanged)
await ensure_workflow_action_records_schema(self._db)    # Spec 3 — unchanged
await ensure_workflow_step_outputs_schema(self._db)      # NEW: Spec 4a schema
```

The new schema:

- Creates `workflow_step_outputs` table + indexes if absent.
- Runs the `ALTER TABLE workflow_executions ADD COLUMN terminal_branch TEXT DEFAULT ''`
  migration with the same idempotent-on-duplicate-column-name
  pattern Spec 3 uses for `gate_nonce`.

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

### Test category 3: `branch` verb

`tests/test_workflow_orchestration_primitives.py::TestBranchVerb`

1. **`test_branch_routes_to_true_target`** — workflow has step1
   (returns `value={"approved": true}`), step2 (branch on `approved`),
   step3 (true_target), step4 (false_target); verify execution
   reaches step3 and skips step4.

2. **`test_branch_routes_to_false_target`** — same setup; step1
   returns `value={"approved": false}`; verify execution reaches
   step4 and skips step3.

3. **`test_branch_action_state_record_captures_target`** — after
   branch fires, the ActionStateRecord for the branch step has
   `operation = "branch"`, `receipt_refs` includes the resolved
   target; `user_visible_summary` describes the choice.

4. **`test_branch_terminal_target`** — branch_on_true target is
   `terminal:rejected:step_x`; verify execution switches into the
   terminal branch; verify main sequence's later steps don't run;
   verify `workflow_executions.terminal_branch = "rejected"`.

5. **`test_branch_validation_at_registration`** — workflow with
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
   containing the event payload.

2. **`test_gate_output_referenced_by_subsequent_step`** — subsequent
   step's parameter is `'{gate.<gate_name>.output.approved}'`;
   verify the step's verb receives the resolved bool.

3. **`test_gate_output_capture_atomic_with_release`** — simulate a
   crash AFTER `_clear_gate_and_advance` returns but BEFORE next
   step runs; restart; verify gate_nonce cleared, cursor advanced,
   AND gate output row present (proof of atomic transaction).

### Test category 6: Composition with Spec 3

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

## Sequence (per architect directive)

1. ✅ Architect-framed (Notion `35effafef4db8168855eeb2524d2ff4e`,
   build directive after Spec 4 verdict).
2. ✅ CC drafts spec at `specs/WORKFLOW-ORCHESTRATION-PRIMITIVES-V1.md`
   on branch `workflow-orchestration-primitives-v1` (this commit).
3. 🟡 **Codex pre-spec review** — pending. Architect provides
   pasteable blip after CC commits. Multi-round review expected
   per "substrate-touching with state-machine semantics" tier (Spec 1
   round 2 caught 9 findings; Spec 3 round 2 caught Blocker 1's
   per-outcome transaction matrix; this spec's per-step capture +
   reference graph + branch + terminal branches is comparable
   surface area).
4. CC folds Codex round 1.
5. Architect ratification of revised spec (potentially with architect
   calls on open architectural questions).
6. CC implements per ratified spec.
7. Codex post-implementation review.
8. CC any final changes.
9. Architect ratifies on close; merge to `main`.
10. **Spec 4b** — architect rewrites the self-improvement workflow
    YAML against this spec's primitives. CC pre-spec review
    (substrate composition only). Architect ratifies. CC
    implements (including production wiring sequence). Codex
    post-impl review. Architect ratifies on close.
11. First end-to-end autonomy loop run.

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
