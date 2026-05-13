# WORKFLOW-AUTHORING-PRIMITIVES-V1 — Implementation Spec

**Status:** DRAFT v2 — pre-implementation. ONE Codex pre-spec review
round folded (12 findings: 3 blockers, 4 high, 5 medium).

**Codex round 1 (12 findings, folded into v2):**

- **Blocker 1** — `register_trigger` could bypass architect
  activation: an already-active workflow's trigger set could be
  modified by Kernos without re-ratification. Folded: trigger
  registration restricted to `registered_not_activated` state.
  Active or deactivated workflows reject trigger registration; the
  pattern is deactivate → register triggers → re-activate
  (Decision 2 revised).
- **Blocker 2** — registration atomicity was underspecified
  against the existing `WorkflowRegistry` which owns its own
  connection. Folded: both inserts (`workflows` table via Spec 4's
  registry AND new `registered_workflows` table) land in one
  transaction via a new `_run_authoring_txn` helper that holds the
  Spec 3 `_run_workflow_write` lock and exposes an uncommitted
  insert path on the registry (Decision 1 revised).
- **Blocker 3** — trigger activation race: the existing
  TriggerRegistry uses an in-memory active cache and dispatches
  after predicate/idempotency work; deactivation could race with
  dispatch. Folded: activation-state check moves to dispatch time
  (the `_on_trigger_match` listener consults
  `registered_workflows.activation_state` immediately before
  enqueueing an execution); at-most-one in-flight execution can
  cross the deactivation boundary, and that semantics is documented
  + tested explicitly (Decision 2 revised, new section).
- **High 4** — Kernos could claim `substrate_tier` on a
  composition-tier descriptor and the spec's match-claim rule
  would accept (claim ≥ computed). Folded: Kernos can NEVER claim
  `substrate_tier`; any Kernos-issued request with
  `governance_tier="substrate_tier"` is rejected with
  `governance_tier_violation` regardless of computed (Decision 6
  revised).
- **High 5** — governance classifier too narrow: only
  `call_tool` tool_ids and `mark_state` namespaces. Folded:
  enumerated v1 substrate-tool-id list AND per-verb rules covering
  `append_to_ledger` ledger_id, `write_canvas` canvas_id,
  `post_to_service` service_id, plus the `mark_state` substrate
  state key namespace. Each verb has an explicit substrate-target
  list with empty-list defaults that documents the v1 scope
  (Decision 6 revised).
- **High 6** — activation state machine contradicted reactivation
  semantics. Folded: explicit state-transition table and CAS-style
  SQL with WHERE-clause check;
  `registered_not_activated → active`,
  `active → deactivated`, `deactivated → active` allowed;
  other transitions rejected (Decision 3 revised).
- **High 7** — deactivation behavior internally inconsistent
  (some text said complete-naturally, other said per-descriptor-
  policy). Folded: chose complete-naturally for v1 uniformly; the
  descriptor-policy claim was removed (Decision 4 revised).
- **Medium 8** — `registered_workflows.instance_id` could drift
  from `workflows.instance_id` (FK is only on `workflow_id`).
  Folded: writer invariant derives `instance_id` from the parent
  workflow row at insert time; caller-supplied `instance_id` is
  ignored, mirroring Spec 3's Codex round-2 Medium 6 fix on
  WorkflowExecutionActionSink (Decision 1 revised).
- **Medium 9** — architect identity check lacked a concrete caller
  context. Folded: new `AuthoringContext` dataclass with
  `actor_id` + `actor_kind` (`kernos` | `architect` | `system`)
  passed to all authoring tools; fail-closed when
  `KERNOS_ARCHITECT_ACTOR_ID` is unset OR `actor_kind != "architect"`
  for architect-only tools (Decision 3 revised, new Decision 9).
- **Medium 10** — validation error taxonomy missed
  `not_authorized` (used by tests) and didn't define message
  templates. Folded: 14 categories now (12 original + `not_authorized`
  + `governance_claim_violation`), each with an explicit message
  template (Decision 5 revised).
- **Medium 11** — `workflow_resolvable` storage was unresolved
  (heuristic in body vs schema column in open questions). Folded:
  schema column on `friction_pattern` table via idempotent ALTER
  pattern; recurrence event payload key `pattern_id` (Spec 1's
  existing event field) is what the disposition subscriber
  consumes (Decision 8 revised).
- **Medium 12** — activation risk_level was a Spec 3 override but
  the spec didn't surface that. Folded: Spec 5 ships a distinct
  `_build_authoring_action_state_record` helper that explicitly
  sets risk_level per an authoring-specific matrix; the
  `activate_workflow=high` override is intentional and surfaced in
  the audit comment (Decision 10, new section).

**Author:** CC, 2026-05-13. Resolves architect's framing in the
Spec 5 build directive at Notion
`35effafef4db81d69ba5ce7d380df3b3`. Folds founder direction that
Kernos-authored workflows are essential and inherent to the
substrate (not a follow-up capability) into a substrate-side
authoring layer that composes with Spec 4's execution-side
primitives.

**Source framing:** PHASE-3-AUTONOMY-LOOP design consideration
(Notion `35cffafef4db81da8107e562307bc738`), updated governance
reframing (architect's response). Spec 5 of the seven-spec
autonomy-loop arc, sequencing after WORKFLOW-ORCHESTRATION-PRIMITIVES-V1
(Spec 4; ratified v2 at `4856f5a`; implementation at `f06cc10` on
branch `workflow-orchestration-primitives-v1`).

**Architect's lean (locked):** eight architectural intents (per the
build directive). Each with explicit lean on activation discipline,
governance-tier classification, validation feedback shape, and
disposition-layer integration; CC's deviations (where any) are
surfaced visibly in the decision sections.

**Composes with:**

- Spec 4 (`kernos/kernel/workflows/`) — Spec 5 register_workflow
  produces a Workflow descriptor that Spec 4's
  `validate_workflow` validates against, then the execution
  engine consumes. The descriptor shape, validation rules, and
  runtime behavior are Spec 4's contract; Spec 5 ships the
  authoring surface on top.
- Spec 3 — every authoring operation (register / register_trigger
  / activate / deactivate) produces an ActionStateRecord via Spec 3's
  discipline. The audit trail is uniform.
- Spec 1 (friction patterns) — disposition guidance includes a
  soft-prompted "consider authoring a workflow" path when Kernos
  notices a recurring friction pattern. Kernos decides whether to
  author; the pattern proposes but doesn't force.

## Governance reframing (PHASE-3-AUTONOMY-LOOP)

This spec ships the substrate that operationalizes the architect's
reframing:

> Workflows are authored by EITHER architect OR Kernos. All
> workflows require architect ratification at activation. The
> governance tier determines who may author:
>
> - **substrate_tier** — modifies workflow primitive, authoring
>   layer, friction-pattern catalog, bridge primitive, or the loop
>   infrastructure itself. Architect-authored only.
> - **composition_tier** — composes substrate primitives to
>   accomplish goals (orchestration, automation, conditional
>   event-response, scheduled coordination). Kernos-authored OR
>   architect-authored.
>
> Architect ratification at activation is the safety boundary in
> both cases. Without activation, registered workflows exist but
> cannot fire.

The substrate ships the *enforcement*: governance-tier
classification at validation time, architect-only activation gate,
disposition-layer guidance that teaches Kernos when authoring is
the right shape.

## What this spec ships

Eight deliverables aligned with the build directive's architectural
intents:

1. **`register_workflow(descriptor, governance_tier)` tool** —
   Kernos's authoring entry point. Validates descriptor against
   Spec 4's expected shape; classifies governance tier; persists
   workflow as `registered_not_activated`; returns workflow_id (or
   structured error feedback if validation fails).

2. **`register_trigger(workflow_id, event_type, predicate)` tool**
   — binds triggers to a registered workflow. Triggers don't fire
   until the workflow is activated. Predicate format matches Spec 4's
   evaluator (the AST or DSL forms `trigger_compiler` already
   supports). Returns trigger_id; multiple triggers per workflow
   supported.

3. **`activate_workflow(workflow_id)` tool (architect-only)** —
   transitions a registered workflow to `active` state; triggers
   begin firing. Restricted permissions: only architect-issued
   activation events can invoke. Composes with Spec 6's gate
   protocol (operator-relayed architect approval).

4. **`deactivate_workflow(workflow_id)` tool (architect-only)** —
   transitions an active workflow to `deactivated` state; triggers
   stop firing; in-flight executions complete or abort per
   workflow descriptor's `aborted_by_deactivation` policy.
   Reversible — a deactivated workflow can be reactivated via
   `activate_workflow`.

5. **Validation feedback channel** — `register_workflow` failures
   produce structured error messages routed to Kernos's awareness
   layer. Specific enough for self-correction: "your workflow
   descriptor's step `step3` references `step_id=unknown_step` that
   doesn't exist in the action_sequence" — not a generic
   "validation failed."

6. **Governance-tier classification at validation time** —
   workflow descriptors get scanned during validation. References to
   substrate-modification surfaces (a hardcoded list per architect's
   lean) classify as `substrate_tier`; everything else is
   `composition_tier`. Kernos's `register_workflow` cannot bypass
   classification; substrate_tier workflows authored by Kernos are
   rejected with structured feedback explaining the boundary.

7. **Disposition-layer guidance** — orientation prompt addition
   plus per-tool description on `register_workflow`. Teaches Kernos
   when a workflow is the right shape (multi-step coordinated work,
   async signals, restart-resume, retry / abort branching) vs
   alternatives (immediate response, scheduled task, friction-
   pattern record only). The "intuitive understanding" piece the
   founder named explicitly.

8. **Composition with friction patterns** — when Kernos notices a
   recurring friction pattern (Spec 1 catalog frequency threshold),
   disposition surface soft-prompts "consider authoring a workflow
   that handles this." Soft-prompt (Kernos decides whether to
   author), not forced. The proposed workflow goes through normal
   register → activate path; closes the autonomy loop by *building*
   itself.

## What this spec does NOT ship

Per the architect's build directive:

- **NO modification API.** Kernos doesn't edit registered
  workflows; supersession is via deactivate + register new. Keeps
  the substrate simple and the audit trail linear.
- **NO workflow versioning** beyond what Spec 4's execution-side
  workflow_id + descriptor_json shape already provides.
- **NO self-improvement workflow definition** — that's Spec 6's
  domain, which consumes Spec 4 + Spec 5 primitives.
- **NO cross-instance workflow propagation** — per-instance for v1.
  A future spec can ship shared / template workflows if soak shows
  the need.
- **NO workflow marketplace / sharing primitives** — not needed for
  the autonomy loop.

## Architectural decisions

### Decision 1 — Workflow registration tool surface

**Architect's lean (adopted):**

```
register_workflow(descriptor: dict, governance_tier: str) -> dict
```

- `descriptor`: structured dict matching Spec 4's expected workflow
  shape. Can be YAML-or-JSON-decoded by the caller; this tool
  accepts the dict form. The descriptor parser (Spec 4's
  `descriptor_parser._build_workflow`) handles conversion to the
  Workflow dataclass.
- `governance_tier`: must be `"composition_tier"` (Kernos-authored)
  or `"substrate_tier"` (architect-only). Validated at the tool
  surface; mismatched tier vs descriptor classification fails loud.
- Returns: `{success: bool, workflow_id: str | None, errors: list[ValidationError]}`
  where ValidationError has `field_path`, `message`, `category`.

**Persistence:** new `registered_workflows` table. The workflow
itself is stored using Spec 4's existing `workflow_registry`
(workflows table); this new table adds the authoring-side metadata:
governance_tier, activation_state, authored_by (member_id),
created_at, activated_at, deactivated_at.

```sql
CREATE TABLE IF NOT EXISTS registered_workflows (
    workflow_id          TEXT PRIMARY KEY,
    instance_id          TEXT NOT NULL,
    governance_tier      TEXT NOT NULL CHECK(governance_tier IN ('composition_tier', 'substrate_tier')),
    activation_state     TEXT NOT NULL DEFAULT 'registered_not_activated'
                          CHECK(activation_state IN ('registered_not_activated', 'active', 'deactivated')),
    authored_by          TEXT NOT NULL DEFAULT '',  -- member_id; empty for architect
    architect_authored   INTEGER NOT NULL DEFAULT 0,  -- 0=Kernos, 1=architect
    created_at           TEXT NOT NULL,
    activated_at         TEXT NOT NULL DEFAULT '',
    deactivated_at       TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_registered_workflows_state
    ON registered_workflows (instance_id, activation_state);
```

**Flow (revised v2 per Codex Blocker 2 + Medium 8):**

1. Tool dispatched with `(ctx, descriptor, governance_tier)` where
   `ctx` is an `AuthoringContext` carrying `actor_id` + `actor_kind`
   (see Decision 9).
2. Spec 4 `_build_workflow(descriptor)` constructs Workflow dataclass.
3. Spec 4 `validate_workflow(wf)` runs (raises on structural error).
4. Spec 5 governance-tier classifier runs over the validated
   workflow (see Decision 6). Compute the canonical tier; verify
   the caller's claim is permitted:
   - Kernos + claimed substrate_tier → reject `governance_claim_violation`.
   - Kernos + claimed composition_tier + computed substrate_tier →
     reject `governance_tier_violation`.
   - Architect + any claim → accept; persist computed (or claimed,
     if architect over-classifies to substrate_tier intentionally).
5. Persist BOTH rows in one transaction via `_run_authoring_txn`
   (Spec 3's lock-protected helper extended for authoring):
   - INSERT into `workflows` table via a new
     `WorkflowRegistry._register_uncommitted_in_txn(wf, db)` method
     that accepts an external connection + does not COMMIT itself.
   - INSERT into `registered_workflows` table with
     `instance_id = wf.instance_id` (derived from the parent
     workflow row, NOT the caller's input — see writer invariant
     below).
   Both writes commit together or both roll back.
6. Return `(success=True, workflow_id, [])`.

**Writer invariant (Codex Medium 8):** the
`registered_workflows.instance_id` column is denormalization for
query locality. The value MUST be derived from `wf.instance_id`
at insert time, NOT from caller-supplied arguments. Any caller
attempt to pass `instance_id` separately is ignored (the API
surface does not accept it as a parameter). A test
(`test_registered_instance_id_derived_from_workflow_row`) pins
this discipline.

**Atomicity helper (Codex Blocker 2):**

```python
async def _run_authoring_txn(
    engine, body: Callable[[aiosqlite.Connection], Awaitable[Any]],
) -> Any:
    """Authoring-side transaction wrapper. Shares the engine's
    write-lock + BEGIN IMMEDIATE boundary so authoring inserts
    can land atomically with Spec 4 workflow inserts.

    The Spec 4 WorkflowRegistry exposes
    _register_uncommitted_in_txn(wf, db) for the authoring layer
    to call inside this transaction body. The registry's own
    insert path remains atomic for non-authoring callers.
    """
    return await engine._run_workflow_txn(body)
```

**ActionStateRecord** emitted per Spec 3 discipline:
`surface="workflow_authoring"`, `operation="register_workflow"`,
`operation_class="register"`, `risk_level="medium"`,
`affected_objects=[workflow_id]`.

### Decision 2 — Trigger registration tool surface (revised v2 per Codex Blocker 1 + 3)

```
register_trigger(ctx: AuthoringContext, workflow_id: str,
                 event_type: str, predicate: dict) -> dict
```

- `ctx`: AuthoringContext (Decision 9) carrying actor identity.
- `workflow_id`: must exist in `registered_workflows`. Tool checks.
- `event_type`: event stream type the trigger watches.
- `predicate`: Spec 4 predicate AST (or DSL string compiled via
  trigger_compiler at registration time).
- Returns: `{success: bool, trigger_id: str | None, errors: list[ValidationError]}`.

**Activation-state precondition (Codex Blocker 1):**
`register_trigger` REQUIRES the workflow to be in
`registered_not_activated` state. Active or deactivated workflows
reject trigger registration with `ValidationError(category="invalid_activation_state",
message="workflow <id> is in state <state>; triggers can only be registered before activation. Deactivate, register triggers, re-activate.")`.

This closes the bypass: an active workflow's trigger set is frozen
at the moment of architect ratification. Adding new triggers
requires a deactivate-modify-reactivate cycle so architect can
re-ratify the new trigger set.

**Persistence:** Spec 4 `TriggerRegistry` provides the underlying
storage. This tool composes against the registry; new column on
the triggers table to capture the authoring workflow_id (FK to
`registered_workflows`).

**Trigger active-state inheritance + dispatch-time check (Codex Blocker 3):**

Triggers physically register in the `TriggerRegistry` as soon as
`register_trigger` succeeds, BUT they don't fire until the
workflow is activated. The substrate-level enforcement runs at
DISPATCH TIME, not match time:

- TriggerRegistry's match listener (`_on_trigger_match` or its
  equivalent) consults `registered_workflows.activation_state`
  IMMEDIATELY before enqueueing the WorkflowExecution.
- `active` → proceed to enqueue.
- `registered_not_activated | deactivated` → silent skip; log the
  rejected fire at DEBUG level.

**At-most-one in-flight at deactivation boundary** (Codex Blocker
3 documented semantics):

A single in-flight dispatch may complete after the architect
deactivates the workflow, because:

1. Match listener reads activation_state = active.
2. Match listener begins enqueueing the execution.
3. Concurrently, architect calls deactivate_workflow.
4. activation_state flips to deactivated.
5. The in-flight enqueue completes; the WorkflowExecution runs to
   its natural end.

This is BY DESIGN. Deactivation stops NEW triggers from firing; it
does NOT cancel in-flight executions (Decision 4). A test
(`test_at_most_one_inflight_crosses_deactivation_boundary`) pins
the semantics: simulate a deactivation between match-listener-read
and enqueue-commit, verify the single execution completes, verify
no further triggers fire after deactivation.

**ActionStateRecord:** `operation="register_trigger"`,
`operation_class="register"`, `risk_level="medium"`,
`affected_objects=[workflow_id, trigger_id]` (per the Spec 5
authoring builder; see Decision 10).

### Decision 3 — Activation state machine (revised v2 per Codex High 6 + Medium 9)

```
activate_workflow(ctx: AuthoringContext, workflow_id: str) -> dict
deactivate_workflow(ctx: AuthoringContext, workflow_id: str, *,
                    reason: str = "") -> dict
```

**Architect-only enforcement** (the safety boundary). Both tools
check `ctx.actor_kind == "architect"`. Kernos / system callers
fail with `ValidationError(category="not_authorized", message="<tool> requires architect actor; got actor_kind=<kind>")`.

**Explicit state machine (Codex High 6):**

| From state                     | Transition           | To state         | Trigger                                |
|--------------------------------|----------------------|------------------|----------------------------------------|
| `registered_not_activated`     | `activate_workflow`  | `active`         | architect call; re-validation passes   |
| `active`                       | `deactivate_workflow`| `deactivated`    | architect call; in-flight runs complete|
| `deactivated`                  | `activate_workflow`  | `active`         | architect call; re-validation passes   |
| `active`                       | `activate_workflow`  | (no-op)          | architect call; `already_active=True`  |
| `deactivated`                  | `deactivate_workflow`| (no-op)          | architect call; `already_deactivated=True`|
| `registered_not_activated`     | `deactivate_workflow`| (rejected)       | nothing to deactivate; `invalid_activation_state`|
| any                            | other transitions    | (rejected)       | not supported                          |

**CAS-style SQL:**

```sql
-- activate: registered_not_activated OR deactivated → active
UPDATE registered_workflows
SET activation_state = 'active',
    activated_at = ?,
    last_transition_at = ?
WHERE workflow_id = ?
  AND activation_state IN ('registered_not_activated', 'deactivated')

-- deactivate: active → deactivated
UPDATE registered_workflows
SET activation_state = 'deactivated',
    deactivated_at = ?,
    last_transition_at = ?,
    deactivation_reason = ?
WHERE workflow_id = ?
  AND activation_state = 'active'
```

Caller checks `cursor.rowcount == 1`:
- `rowcount == 1` → transition succeeded.
- `rowcount == 0` → idempotent no-op OR rejected transition. Tool
  re-reads the row to distinguish: already in target state →
  return `{success: True, already_<state>: True}`; otherwise →
  return `{success: False, errors: [invalid_activation_state]}`.

**Activation flow (with re-validation):**

1. Tool dispatched with `(ctx, workflow_id)`.
2. Verify `ctx.actor_kind == "architect"`. If not, return
   `not_authorized`.
3. Look up workflow in `registered_workflows` + load Workflow
   dataclass via Spec 4's registry.
4. Re-run `validate_workflow` + governance-tier classifier
   (defensive: substrate primitives may have changed since
   registration; architect ratification at activation is the
   safety boundary).
5. Run the CAS UPDATE. If `rowcount == 1`, transition succeeded;
   if `0`, return either `already_active=True` or the rejected-
   transition error.
6. Triggers for this workflow's `registered_workflows.workflow_id`
   now pass the dispatch-time activation_state check (Decision 2).

**Deactivation flow:**

1. Tool dispatched with `(ctx, workflow_id, reason)`.
2. Verify architect actor.
3. Look up workflow. Re-validation NOT required for deactivation
   (deactivation is purely safety-side).
4. CAS UPDATE.
5. Triggers stop firing on the next dispatch attempt
   (Decision 2 dispatch-time check).

**ActionStateRecord:** see Decision 10 (Spec 5 authoring builder)
for `operation_class` + `risk_level` mapping.

**Composition with Spec 6 gate protocol:** in production, the
architect ratifies via an event-stream event (e.g.,
`autonomy_loop.architect_workflow_activation`). The
`activate_workflow` tool consumes such events through Spec 4's gate
predicate evaluator. For v1, the tool can also be invoked
synchronously by a developer in test contexts. The
`AuthoringContext.actor_kind` derives from the event's actor
identity (architect-emitted events carry `actor_kind="architect"`).

### Decision 4 — Deactivation semantics (revised v2 per Codex High 7)

Subsumed into Decision 3's state machine. The single v1 behavior
for in-flight executions on deactivation:

**Complete-naturally (Codex High 7):** the engine does NOT
interrupt running WorkflowExecutions. A deactivated workflow's
in-flight executions complete (or abort via their own paths) at
the engine's natural pace. Deactivation only stops NEW triggers
from firing (Decision 2 dispatch-time check). The earlier
"complete-or-abort per descriptor's policy" claim was removed —
v1 ships one behavior to keep the substrate-side state machine
simple.

**Tested invariant:** `test_deactivation_does_not_interrupt_inflight`
seeds an in-flight execution, calls deactivate, verifies the
execution completes naturally without engine-side cancellation.

If a future spec needs descriptor-controlled interruption, it
extends here. v1's complete-naturally pairs with the at-most-one-
in-flight-crosses-boundary semantics from Decision 2.

**Reactivation:** a deactivated workflow can be reactivated via
`activate_workflow` (per the state machine in Decision 3).
Re-validation runs at reactivation just like at first activation.

### Decision 5 — Validation feedback channel (revised v2 per Codex Medium 10)

**Architect's lean (adopted):** structured ValidationError shape
that's specific enough for Kernos to self-correct.

```python
@dataclass
class ValidationError:
    field_path: str        # "action_sequence[2].parameters.condition"
    category: str          # see table below
    message: str           # human-readable description per template
    severity: str = "error"  # "error" | "warning"
```

**Categories + message templates (Codex Medium 10):**

| Category                          | When raised                                                              | Message template                                                                                            |
|-----------------------------------|--------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------|
| `missing_required_field`          | A required descriptor field is absent                                    | `"required field <field_path> is missing"`                                                                  |
| `invalid_value`                   | Field value violates type / enum / format constraint                     | `"field <field_path> value <repr> violates constraint: <details>"`                                          |
| `unknown_action_type`             | Descriptor references an action_type not in KNOWN_ACTION_TYPES           | `"action_type <repr> at <field_path> is not in the known verb set"`                                         |
| `unknown_step_id`                 | Template reference points at a step_id not declared in the workflow      | `"step <step_id> referenced in <field_path> is not declared in the workflow"`                               |
| `unknown_gate_name`               | Gate reference points at a gate_name not declared                        | `"gate <gate_name> referenced in <field_path> is not declared in approval_gates"`                           |
| `duplicate_step_id`               | Two steps share an id (across main + terminal_branches)                  | `"step id <step_id> declared at <field_path> conflicts with another step in this workflow"`                 |
| `invalid_identifier`              | Step id / gate name / branch name violates the grammar regex             | `"identifier <repr> at <field_path> must match [A-Za-z][A-Za-z0-9_-]*"`                                     |
| `dangling_branch_target`          | Branch verb target doesn't resolve to a declared step                    | `"branch verb at <field_path> references unknown target <target>"`                                          |
| `circular_branch`                 | Branch graph forms a cycle (DAG-validation; Spec 4 substrate)            | `"branch graph contains a cycle: <cycle_description>"`                                                      |
| `governance_tier_violation`       | Kernos attempted to register a workflow whose computed tier is substrate | `"workflow's computed governance tier is substrate_tier (substrate-modifying action at <field_path>); Kernos cannot author substrate_tier workflows"` |
| `governance_claim_violation`      | Kernos claimed substrate_tier on register_workflow call (Codex High 4)   | `"actor_kind=kernos cannot claim governance_tier=substrate_tier; only architect may"`                       |
| `descriptor_shape_invalid`        | Descriptor isn't a dict / has unparseable structure                      | `"descriptor must be a dict; got <type>"`                                                                   |
| `predicate_invalid`               | Predicate AST fails Spec 4 predicate validation                          | `"predicate at <field_path> failed validation: <details>"`                                                  |
| `not_authorized`                  | Actor lacks permission for the tool (Codex Medium 10)                    | `"<tool_name> requires architect actor; got actor_kind=<kind>"`                                             |
| `invalid_activation_state`        | Operation incompatible with current activation_state (Codex Blocker 1 + High 6) | `"workflow <workflow_id> is in state <state>; operation <op> requires state(s) <allowed>"`           |

**Feedback delivery:** the tool's return shape carries
`errors: list[ValidationError]`. Kernos's reasoning surface reads
these errors in its awareness layer (orientation prompt teaches
Kernos to inspect them on `success=False`). The discipline:
Kernos's next attempt can fix specifically what the validator
named.

**Composes with friction patterns:** repeated failures of the same
category (e.g., Kernos repeatedly authoring `unknown_step_id`
references) surface as a friction pattern via Spec 1. The
friction signal proposes a disposition-layer refinement (clearer
prompt; per-tool example).

### Decision 6 — Governance-tier classification (revised v2 per Codex High 4 + 5)

**v1 substrate-target enumerations** (Codex High 5 — replace the
narrow tool_id-only check with enumerated lists per verb):

```python
SUBSTRATE_TOOL_IDS: frozenset[str] = frozenset({
    # Authoring layer (this spec)
    "register_workflow", "register_trigger",
    "activate_workflow", "deactivate_workflow",
    # Workflow primitive (Spec 4) — none currently exposed as tools;
    # workflow modification happens via deactivate + register new.
    # Future substrate-tier tools added here.
    # Friction-pattern catalog (Spec 1) — none currently exposed as
    # tools; future Spec 1 extension tools added here.
    # Bridge primitive (Spec 2) — none currently exposed as tools.
})

# State keys whose modification means substrate change.
SUBSTRATE_STATE_KEY_PATTERNS: tuple[str, ...] = (
    # Workflow primitive's own state
    "workflow.*", "registered_workflow.*",
    # Friction-pattern catalog
    "friction_pattern.*",
    # Bridge primitive
    "coding_session_bridge.*",
)

# Ledger names whose append means substrate change.
SUBSTRATE_LEDGER_NAMES: frozenset[str] = frozenset({
    # The autonomy loop ledger that records ratification outcomes
    "autonomy_loop_outcomes",
})

# Service IDs whose post means substrate change (none for v1;
# all current services are composition-tier).
SUBSTRATE_SERVICE_IDS: frozenset[str] = frozenset({})

# Canvas IDs whose write means substrate change (none for v1;
# canvases are content tier, not substrate tier).
SUBSTRATE_CANVAS_IDS: frozenset[str] = frozenset({})
```

**Classification rule** (walks `action_sequence` AND
`terminal_branches`):

For each action:

| Action verb        | Substrate when                                                                                                        |
|--------------------|-----------------------------------------------------------------------------------------------------------------------|
| `call_tool`        | `params.tool_id` in `SUBSTRATE_TOOL_IDS`                                                                              |
| `mark_state`       | `params.key` matches any pattern in `SUBSTRATE_STATE_KEY_PATTERNS` (fnmatch.fnmatchcase)                              |
| `append_to_ledger` | `params.ledger` in `SUBSTRATE_LEDGER_NAMES`                                                                           |
| `post_to_service`  | `params.service_id` in `SUBSTRATE_SERVICE_IDS`                                                                        |
| `write_canvas`     | `params.canvas_id` in `SUBSTRATE_CANVAS_IDS`                                                                          |
| `notify_user`      | NEVER substrate (notifications are content-tier)                                                                      |
| `route_to_agent`   | NEVER substrate (delegation is composition)                                                                           |
| `branch`           | NEVER substrate (pure control flow)                                                                                   |

The classifier returns the COMPUTED tier (`substrate_tier` if
ANY action matches; `composition_tier` otherwise).

**Kernos can never claim substrate_tier (Codex High 4):**

```
caller=Kernos    claim=composition_tier    computed=composition_tier  → ACCEPT (tier=composition)
caller=Kernos    claim=composition_tier    computed=substrate_tier    → REJECT (governance_tier_violation)
caller=Kernos    claim=substrate_tier      computed=*                 → REJECT (governance_claim_violation)
caller=architect claim=composition_tier    computed=composition_tier  → ACCEPT (tier=composition)
caller=architect claim=composition_tier    computed=substrate_tier    → ACCEPT (tier=substrate; loud audit)
caller=architect claim=substrate_tier      computed=*                 → ACCEPT (tier=substrate)
```

Kernos cannot manufacture a `substrate_tier` claim regardless of
the descriptor's contents. Tests pin both rejection paths.

**Persisted tier:** `registered_workflows.governance_tier`
captures the EFFECTIVE tier — the maximum of (claimed, computed)
filtered through the rules above. The classifier's computed tier
is also captured separately in metadata for audit:

```sql
ALTER TABLE registered_workflows
  ADD COLUMN computed_tier TEXT NOT NULL DEFAULT 'composition_tier';
```

(Idempotent ALTER pattern.)

**Substrate-target lists are hardcoded** for v1 (per architect's
lean). Each list lives in a single module constant for visibility.
If the list grows during operation, a future architect-authored
spec (substrate_tier itself) extends it.

### Decision 7 — Disposition-layer guidance

**Architect's lean:** per-tool description on `register_workflow`
plus a small orientation prompt addition. NOT a separate doctrine
document.

**Per-tool description (the model sees this in its tool list):**

> Compose a workflow when work is: (a) multi-step coordinated,
> (b) dependent on async signals from external sources (CC, Codex,
> operator response), (c) needs to resume after restart, OR (d)
> requires retry / abort / branching semantics. For ad-hoc
> coordination that fits none of those, prefer simpler primitives:
> immediate response, scheduled task, or friction-pattern record.
> All workflows require architect ratification at activation; the
> register call validates structure and persists, but the workflow
> is dormant until architect activates it.

**Orientation prompt addition:**

> You can author workflows for substrate composition — orchestrating
> tools, gates, and substrate primitives to accomplish goals that
> would otherwise require manual step-by-step execution. Use the
> `register_workflow` tool with a structured descriptor; iterate on
> validation feedback if errors surface. Workflows authored at the
> `composition_tier` go through architect ratification before
> activation. The architect ratifies; you can propose.

**Composition with friction patterns** (Decision 8 sketch): when a
friction pattern fires `record_recurrence` AND the pattern is
classified as workflow-resolvable, the disposition emits a
soft-prompted reflection: "this pattern keeps recurring; consider
authoring a workflow that handles it autonomously." Kernos
decides whether to act on the prompt.

### Decision 8 — Composition with friction patterns (revised v2 per Codex Medium 11)

**Schema-driven tagging (Codex Medium 11).** The
`workflow_resolvable` tag lives as a column on Spec 1's
`friction_pattern` table, not as a heuristic in this spec. This
spec ships the idempotent ALTER:

```sql
ALTER TABLE friction_pattern
  ADD COLUMN workflow_resolvable INTEGER NOT NULL DEFAULT 0;
```

(Idempotent pattern matches Spec 3's gate_nonce migration.)

Architect curates the tagged set by setting
`workflow_resolvable=1` on patterns whose recurrence should prompt
Kernos to consider authoring a workflow. v1 ships an empty default
(no patterns tagged); architect populates the tag set after
operation surfaces candidates.

**Recurrence event subscriber** (consumes Spec 1's existing
`friction.pattern_recurrence` event):

1. Spec 5's disposition layer registers as an event_stream
   subscriber for `friction.pattern_recurrence`.
2. On each event, the subscriber reads `event.payload.pattern_id`
   (Spec 1's existing field), looks up the pattern in
   `friction_pattern`, checks `workflow_resolvable`.
3. If `workflow_resolvable=1`, the subscriber adds a soft
   reflection to Kernos's next reasoning surface:
   `"Pattern <pattern_id> recurred (<description>). Consider authoring a workflow to handle this autonomously. Reference the pattern_id in your descriptor's metadata."`
4. Otherwise: silent skip.
5. Kernos decides whether to compose a workflow descriptor + call
   `register_workflow`. Architect ratifies activation.

**Recurrence event payload key documented:** `pattern_id` is the
Spec 1 event payload field the subscriber reads. No new payload
key shipped by this spec; this is composition, not extension.

**Not force-driven** — the disposition surfaces the reflection;
Kernos's reasoning decides. If Kernos chooses immediate response
or some other resolution, the friction pattern continues
recurring until something resolves it (could be Kernos eventually,
could be architect, could be a manual fix).

### Decision 9 — AuthoringContext dataclass (new v2 per Codex Medium 9)

All authoring tools take an `AuthoringContext` as the first
parameter, carrying the actor identity. Architect-only tools
check `ctx.actor_kind`; fail-closed when the environment variable
`KERNOS_ARCHITECT_ACTOR_ID` is unset.

```python
@dataclass(frozen=True)
class AuthoringContext:
    """Identity context for authoring tools. Constructed by the
    caller / tool-dispatcher; cannot be mutated mid-call.

    actor_id: the concrete actor identifier (member_id for Kernos,
        operator_id for architect-via-operator, "system" for
        engine-internal calls).
    actor_kind: discriminator. "kernos" | "architect" | "system".
        "system" is used for engine-internal calls during workflow
        execution (e.g., self-improvement workflow that calls
        register_workflow on its target's behalf — these still
        require architect ratification at activation).
    """
    actor_id: str
    actor_kind: str

    def is_architect(self) -> bool:
        return self.actor_kind == "architect"
```

**Architect identity check (Codex Medium 9):**

```python
def _is_architect(ctx: AuthoringContext) -> bool:
    """Architect-only tools call this. Fail-closed semantics: if
    KERNOS_ARCHITECT_ACTOR_ID is unset, NO actor passes the
    check.
    """
    expected = os.environ.get("KERNOS_ARCHITECT_ACTOR_ID", "")
    if not expected:
        return False  # fail closed
    return ctx.actor_kind == "architect" and ctx.actor_id == expected
```

**Tool dispatch:** when an authoring tool is invoked from a
workflow step (`call_tool` with the authoring tool_id), the
dispatcher constructs the `AuthoringContext` from the
WorkflowExecution's `member_id` + an actor_kind classifier:

- `member_id == os.environ.get("KERNOS_ARCHITECT_ACTOR_ID")` →
  `actor_kind = "architect"`.
- `member_id == ""` AND the workflow is system-authored (e.g., the
  self-improvement workflow that Spec 6 ships) → `actor_kind = "system"`.
- Otherwise → `actor_kind = "kernos"`.

**Test coverage** (Codex Medium 9): three actor paths covered:

1. `test_kernos_actor_fails_architect_only_tools`
2. `test_system_actor_fails_architect_only_tools`
3. `test_architect_actor_succeeds_with_env_var_set`
4. `test_unset_env_var_fails_all_architect_only_calls` (fail-closed)

### Decision 10 — Spec 5 authoring ActionStateRecord builder (new v2 per Codex Medium 12)

Spec 3's default mapping derives `risk_level` from `operation_class`
(`manage → medium`). Spec 5 needs explicit overrides for authoring
operations — particularly `activate_workflow` which is the safety
boundary and warrants `risk_level=high`. Rather than fight Spec 3's
derivation, Spec 5 ships its own ActionStateRecord builder
specifically for authoring operations.

```python
def _build_authoring_action_state_record(
    *,
    operation: str,          # "register_workflow" | "register_trigger" |
                             # "activate_workflow" | "deactivate_workflow"
    actor: AuthoringContext,
    workflow_id: str = "",
    trigger_id: str = "",
    execution_state: str = "completed",
    error: str = "",
    extra_affected_objects: tuple[str, ...] = (),
) -> ActionStateRecord:
    """Builder for authoring-operation ActionStateRecords.

    Overrides Spec 3's default risk derivation for the authoring
    layer's operations. The override is intentional and surfaced
    in audit by the explicit operation_class+risk_level mapping
    below.
    """
    op_class = "manage"  # all authoring operations are 'manage'
    # AUTHORING RISK-LEVEL MATRIX:
    #   register_workflow:   medium  (creates substrate row;
    #                                 not yet active)
    #   register_trigger:    medium  (binds trigger; not yet firing)
    #   activate_workflow:   HIGH    (the safety boundary; this is
    #                                 the moment architect ratifies)
    #   deactivate_workflow: medium  (reversible; in-flight executions
    #                                 complete naturally per Decision 4)
    risk_level = "high" if operation == "activate_workflow" else "medium"
    # ... construct ActionStateRecord with the explicit risk_level ...
```

The `activate_workflow=high` override is documented in code and in
audit consumers know to surface activation events at high
priority. The matrix is small (4 operations) and stable.

## What ships in implementation

- **NEW** `kernos/kernel/workflows/authoring.py` (~400 LOC) —
  authoring tool implementations, validation feedback shape,
  governance-tier classifier, soft-prompt emission helper.
- **NEW** `kernos/kernel/workflows/registered_workflows.py` (~150
  LOC) — schema setup + persistence + state-transition helpers for
  the `registered_workflows` table.
- **MODIFIED** Tool registry — register the four new tools
  (`register_workflow`, `register_trigger`, `activate_workflow`,
  `deactivate_workflow`).
- **MODIFIED** Spec 4 `TriggerRegistry` — match-time check against
  `registered_workflows.activation_state`. Inactive workflows'
  triggers don't fire even if a matching event arrives.
- **MODIFIED** Kernos orientation prompt — addition per Decision 7.
- **MODIFIED** Disposition layer — friction-pattern recurrence
  subscriber per Decision 8.
- **NEW** `tests/test_workflow_authoring.py` — embedded live tests
  across 7 categories.

## What this spec does NOT ship (re-stated for clarity)

- The self-improvement workflow definition itself (Spec 6).
- Spec 2 bridge-tools production wiring (Spec 6's responsibility).
- A workflow-modification primitive (deactivate + register new is
  the pattern).
- Workflow versioning beyond Spec 4's existing shape.

## Embedded live tests

Substrate-fidelity assertion pattern. Each test verifies substrate
state (rows in `registered_workflows`, triggers' fire/no-fire
behavior, ActionStateRecord emission) not just behavioral outputs.

### Test category 1: Kernos registers composition-tier workflow

`tests/test_workflow_authoring.py::TestRegisterComposition`

1. **`test_kernos_registers_valid_composition_workflow`** — Kernos
   calls `register_workflow` with a valid composition-tier descriptor.
   Verify: workflow persists in `workflows` table; row exists in
   `registered_workflows` with governance_tier='composition_tier',
   activation_state='registered_not_activated'; returns
   `{success: True, workflow_id: <uuid>}`; ActionStateRecord
   emitted with `operation="register_workflow"`.

2. **`test_kernos_can_register_trigger_for_own_workflow`** — after
   register_workflow, call register_trigger with the workflow_id;
   verify trigger persists; verify trigger doesn't fire (workflow
   still registered_not_activated).

3. **`test_register_returns_workflow_id_for_subsequent_tools`** —
   the returned workflow_id is usable as input to register_trigger.

### Test category 2: Substrate-tier governance enforcement

`tests/test_workflow_authoring.py::TestGovernanceTier`

1. **`test_kernos_substrate_tier_workflow_rejected`** — Kernos
   attempts to register a workflow with action targeting
   `transition_friction_pattern_lifecycle` tool_id; classifier
   computes substrate_tier; tool returns
   `{success: False, errors: [ValidationError(category="governance_tier_violation")]}`.

2. **`test_architect_substrate_tier_workflow_accepted`** — architect
   (identified via actor_id) registers the same substrate_tier
   workflow; tool returns success.

3. **`test_classifier_walks_main_and_terminal_branches`** — workflow
   with composition-tier main_sequence BUT a substrate-modifying
   action in a terminal_branch; classifier returns substrate_tier;
   Kernos-issued registration rejected.

4. **`test_kernos_cannot_claim_substrate_tier`** — Codex High 4:
   Kernos calls `register_workflow(ctx_kernos, descriptor, "substrate_tier")`
   where descriptor is itself composition-tier; tool returns
   `{success: False, errors: [ValidationError(category="governance_claim_violation")]}`.

5. **`test_architect_can_over_classify_to_substrate`** — architect
   calls register_workflow with composition-tier descriptor +
   claim `substrate_tier`; accepted; persisted tier is substrate
   (loud audit captures the override).

6. **`test_classifier_covers_all_substrate_verbs`** — Codex High 5:
   parametrized test across `mark_state` with substrate state key,
   `append_to_ledger` with substrate ledger name, `post_to_service`
   with substrate service_id, `write_canvas` with substrate
   canvas_id (the empty-list defaults mean the latter two are
   no-ops in v1 but the rule is wired in). Each variant
   classifies correctly.

### Test category 3: Architect-only activation + state machine

`tests/test_workflow_authoring.py::TestActivation`

1. **`test_kernos_cannot_activate`** — Kernos calls
   `activate_workflow`; tool returns
   `{success: False, errors: [ValidationError(category="not_authorized")]}`;
   activation_state remains registered_not_activated.

2. **`test_system_cannot_activate`** — Codex Medium 9:
   `actor_kind="system"` also fails the architect check.

3. **`test_unset_env_var_fails_all_architect_calls`** — Codex
   Medium 9 fail-closed: with `KERNOS_ARCHITECT_ACTOR_ID` unset,
   even `actor_kind="architect"` calls fail
   `not_authorized`.

4. **`test_architect_activates_registered_workflow`** — architect
   calls `activate_workflow`; activation_state transitions to
   `active`; activated_at populated; trigger fires on next match.

5. **`test_state_machine_transitions`** — Codex High 6: parametrized
   over the state-transition table from Decision 3. Each allowed
   transition succeeds; each rejected transition returns
   `invalid_activation_state`.

6. **`test_inactive_workflow_triggers_do_not_fire`** — workflow
   registered but not activated; matching event flushes; verify
   trigger does NOT fire (no execution row created).

7. **`test_activated_workflow_triggers_fire`** — after activation,
   verify trigger DOES fire on matching event.

8. **`test_reactivation_after_deactivation`** — Codex High 6:
   `deactivated → active` transition via re-call of
   `activate_workflow`; state transitions cleanly; re-validation
   runs.

9. **`test_reactivation_idempotent`** — calling activate on
   already-active workflow returns success with `already_active=True`;
   no state mutation.

10. **`test_register_trigger_on_active_workflow_rejected`** — Codex
    Blocker 1: workflow is active; Kernos calls register_trigger;
    rejected with `invalid_activation_state`.

11. **`test_at_most_one_inflight_crosses_deactivation_boundary`** —
    Codex Blocker 3: race between trigger match (dispatcher reads
    activation_state) and deactivate; the single in-flight
    execution completes naturally; no further triggers fire
    afterward.

### Test category 4: Deactivation

`tests/test_workflow_authoring.py::TestDeactivation`

1. **`test_architect_deactivates_active_workflow`** — workflow in
   `active` state; architect calls deactivate; state transitions to
   `deactivated`; triggers stop firing.

2. **`test_deactivated_workflow_can_be_reactivated`** — after
   deactivate, architect re-activates; state returns to active.

3. **`test_in_flight_executions_complete_naturally`** — workflow
   active with an in-flight execution; architect deactivates;
   verify in-flight execution completes (engine doesn't interrupt
   running tasks; only stops new triggers).

### Test category 5: Validation feedback

`tests/test_workflow_authoring.py::TestValidationFeedback`

1. **`test_unknown_step_id_reported_specifically`** — workflow with
   parameter `'{step.unknown_id.output.x}'`; verify returned
   ValidationError has `field_path="action_sequence[0].parameters"`,
   `category="unknown_step_id"`, `message` naming
   `unknown_id`.

2. **`test_dangling_branch_target_reported`** — branch verb's
   `branch_on_true` references undeclared step_id; verify category
   `dangling_branch_target`, specific message.

3. **`test_multiple_errors_aggregated`** — descriptor with several
   errors; verify all surface in `errors` list (not just first).

4. **`test_governance_tier_violation_includes_classification_details`**
   — substrate_tier rejection's error message identifies the
   triggering action_type + parameters that caused the
   classification.

### Test category 6: Disposition-layer guidance

`tests/test_workflow_authoring.py::TestDispositionLayer`

1. **`test_tool_description_present_in_orientation`** — verify the
   `register_workflow` tool's description matches Decision 7's text
   (substring check on the orientation prompt construction).

2. **`test_friction_recurrence_emits_soft_prompt`** — pre-seed an
   active friction pattern with workflow-resolvable tag; trigger
   recurrence; verify the disposition layer emits a soft-prompt
   reflection containing the pattern_id.

3. **`test_unflagged_pattern_no_soft_prompt`** — pattern not
   tagged as workflow-resolvable; trigger recurrence; verify no
   soft prompt fires.

### Test category 7: End-to-end (bootstrap-proves-it)

`tests/test_workflow_authoring.py::TestEndToEnd`

1. **`test_kernos_authors_workflow_architect_activates_workflow_fires`**
   — full path: (a) Kernos calls register_workflow → success;
   (b) Kernos calls register_trigger → success;
   (c) trigger fires from matching event → NO execution (workflow
   not activated yet); (d) architect calls activate_workflow →
   success; (e) trigger fires from another matching event →
   execution row created, workflow runs to completion.

### Test category 8: ActionStateRecord composition

`tests/test_workflow_authoring.py::TestSpec3Composition`

1. **`test_register_workflow_emits_action_state_record`** — verify
   ActionStateRecord with `operation="register_workflow"`,
   `operation_class="register"`, `risk_level="medium"`,
   `affected_objects=[workflow_id]`.

2. **`test_activate_workflow_high_risk_level`** — verify
   `risk_level="high"` for activation (this is the safety
   boundary).

3. **`test_register_failure_emits_failed_action_state_record`** —
   failed registration (governance_tier_violation) still emits an
   ActionStateRecord with `execution_state="failed"` and the
   ValidationError list serialized in `user_visible_summary`.

## Open architectural questions (CC surfaces transparently)

Five questions for Codex pre-spec review + architect to land
before ratification:

1. **Substrate-tool-id list scope.** The hardcoded list of
   substrate-modifying tool_ids — is it just the four authoring
   tools + friction-pattern + bridge methods, or does it extend to
   ALL substrate-side tools (e.g., the `mark_state` operations on
   substrate state keys, the workflow primitive's own audit/event
   surface)? CC's lean: narrow list initially (the obvious
   substrate-modifying surfaces); architect can extend over time.
   Risk: too narrow leaves loopholes; too broad blocks legitimate
   composition work.

2. **Architect identity check at activation.** v1 hardcodes a
   single architect actor_id (or matches against a pattern). When
   Spec 6's gate protocol lands, this should compose with the
   architect-gate event predicate. For v1 alone, what's the
   identity check shape? CC's lean: environment-variable
   `KERNOS_ARCHITECT_ACTOR_ID` set at instance bootstrap; tool
   compares calling-actor against this. Defensive: empty / unset
   means architect-only operations fail (no implicit architect).

3. **Validation re-run timing.** Decision 3 re-runs
   `validate_workflow` at activation time (defensive against
   substrate changes since registration). Should validation also
   re-run periodically while the workflow is active (e.g., daily
   for long-lived workflows)? CC's lean: no re-run while active for
   v1; the architect's activation is the ratification moment; once
   active, the workflow stays validated until explicitly
   deactivated.

4. **Workflow-resolvable tag on friction patterns.** Decision 8
   says patterns get tagged `workflow_resolvable: bool`. This is a
   new field on the friction pattern schema. Should this spec
   extend the schema, or should Spec 1's catalog already include
   it (and we're surfacing the gap)? CC's lean: this spec extends
   the friction-pattern schema with an `idempotent ALTER` migration
   adding the `workflow_resolvable` column (default 0). Tagging
   discipline lives in this spec's docs.

5. **Concurrent activation / deactivation safety.** The
   activate / deactivate tools are architect-issued; race conditions
   between two architects (unlikely but possible) need defensive
   handling. CC's lean: state transitions use the same
   `_run_workflow_write` lock-protected discipline Spec 3 ships
   for workflow_executions writes. Concurrent calls serialize;
   each transition is atomic.

## Risks and design constraints

| Risk                                                          | Mitigation                                                                                                                                                                                                                                                                                                                              |
|---------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Kernos bypasses governance check                                | Substrate-level enforcement: `register_workflow` validates governance_tier AND classifies independently; substrate_tier authored by Kernos returns failure with ValidationError. No bypass surface.                                                                                                                                      |
| Architect identity spoof                                       | v1 hardcoded check against `KERNOS_ARCHITECT_ACTOR_ID`; production-mode requires environment variable to be set; defensive default is "all architect-only operations fail." Spec 6 hardens this when its gate protocol lands.                                                                                                            |
| In-flight execution on deactivated workflow                    | Deactivation stops new triggers; in-flight executions complete naturally. The architect's deactivation reason captures whether this is the intended behavior or whether a Spec 6 workflow needs explicit cancellation primitives.                                                                                                        |
| Activation flips workflow that's no longer valid              | Re-run `validate_workflow` AND governance-tier classification at activation time. Substrate primitives may have evolved; the activation is the ratification moment.                                                                                                                                                                       |
| Disposition soft-prompt overuse                                | Friction-pattern subscriber only emits for `workflow_resolvable=True` patterns; v1's tagged list is small and architect-curated. False positives don't compose: Kernos may receive multiple soft prompts but isn't forced to act.                                                                                                        |
| Validation feedback too generic                                | Structured ValidationError with `field_path` + `category` + `message`. Each category has a specific message format. Kernos's awareness layer surfaces all errors, not just the first.                                                                                                                                                    |
| Substrate-tool-id list maintenance                              | Single module constant; documented inline; review at the close of each substrate-touching spec to verify the list is current. Adding to the list is a substrate_tier change (architect-authored).                                                                                                                                       |
| Composition with Spec 4 step output / reference graph          | The authoring tools use Spec 4's `_build_workflow` + `validate_workflow` directly. Any change to Spec 4's descriptor shape automatically composes with this spec's validation. The descriptor parser is the single source of truth.                                                                                                      |
| Concurrent register / activate races                          | All authoring writes serialize through Spec 3's `_run_workflow_write` lock-protected discipline. State transitions are atomic.                                                                                                                                                                                                          |
| Register-trigger bypassing architect ratification (Codex Blocker 1) | v2 Decision 2: `register_trigger` requires workflow in `registered_not_activated` state. Active or deactivated workflows reject trigger registration; the pattern is deactivate → register triggers → re-activate.                                                                                                                  |
| Cross-table registration atomicity (Codex Blocker 2)            | v2 Decision 1: `_run_authoring_txn` helper holds the Spec 3 write-lock and runs both inserts (workflows + registered_workflows) in one transaction. WorkflowRegistry exposes `_register_uncommitted_in_txn` for the authoring layer.                                                                                                  |
| Trigger activation race at dispatch (Codex Blocker 3)           | v2 Decision 2 dispatch-time check: TriggerRegistry consults `registered_workflows.activation_state` immediately before enqueueing. At-most-one in-flight execution may cross the deactivation boundary; the semantics is documented + tested.                                                                                          |
| Kernos claiming substrate_tier (Codex High 4)                   | v2 Decision 6: Kernos cannot claim `substrate_tier`; any Kernos-issued request with `governance_tier="substrate_tier"` returns `governance_claim_violation` regardless of computed tier.                                                                                                                                              |
| Classifier missing substrate paths (Codex High 5)               | v2 Decision 6: enumerated `SUBSTRATE_TOOL_IDS`, `SUBSTRATE_STATE_KEY_PATTERNS`, `SUBSTRATE_LEDGER_NAMES`, `SUBSTRATE_SERVICE_IDS`, `SUBSTRATE_CANVAS_IDS` lists; per-verb classification table (mark_state, append_to_ledger, post_to_service, write_canvas all covered).                                                              |
| State machine reactivation contradiction (Codex High 6)         | v2 Decision 3: explicit state-transition table with CAS-style SQL. `registered_not_activated→active`, `active→deactivated`, `deactivated→active` all allowed; other transitions rejected.                                                                                                                                              |
| Deactivation policy inconsistency (Codex High 7)                | v2 Decision 4: single v1 behavior — in-flight executions complete naturally. Descriptor-policy claim removed.                                                                                                                                                                                                                            |
| instance_id drift between tables (Codex Medium 8)              | v2 Decision 1 writer invariant: `registered_workflows.instance_id` derived from the parent workflow row at insert time; caller-supplied values ignored.                                                                                                                                                                                |
| Missing AuthoringContext (Codex Medium 9)                       | v2 Decision 9: new `AuthoringContext` dataclass passed to all authoring tools; fail-closed when `KERNOS_ARCHITECT_ACTOR_ID` unset; explicit `actor_kind` discriminator (`kernos`/`architect`/`system`).                                                                                                                              |
| Validation taxonomy incomplete (Codex Medium 10)                | v2 Decision 5: 14 categories with explicit message templates each. Adds `not_authorized` + `governance_claim_violation` + `invalid_activation_state`.                                                                                                                                                                                |
| workflow_resolvable storage ambiguity (Codex Medium 11)         | v2 Decision 8: schema column on `friction_pattern` via idempotent ALTER. Recurrence event payload key is Spec 1's existing `pattern_id`; subscriber reads from there.                                                                                                                                                                  |
| Risk-level conflict with Spec 3 derivation (Codex Medium 12)    | v2 Decision 10: Spec 5-specific `_build_authoring_action_state_record` with explicit risk-level matrix per operation. `activate_workflow=high` override is intentional and documented.                                                                                                                                                  |

## Sequence (per architect directive)

1. ✅ Architect-framed (Notion `35effafef4db81d69ba5ce7d380df3b3`,
   Spec 5 build directive).
2. ✅ CC drafts spec v1 at `specs/WORKFLOW-AUTHORING-PRIMITIVES-V1.md`
   on branch `workflow-authoring-primitives-v1` (commit `d492239`).
3. ✅ **Codex pre-spec review round 1** — 12 findings (3 blockers,
   4 high, 5 medium). All implementation-surface; none challenged
   the architectural shape.
4. ✅ **CC folds Codex round 1** into spec v2 (this revision).
5. 🟡 Architect ratification of v-final (potentially with
   architect calls on the 5 open architectural questions).
6. CC implements per ratified spec.
7. Codex post-implementation review.
8. CC any final changes.
9. Architect ratifies on close; merge to `main`.
10. **Spec 6** — architect drafts SELF-IMPROVEMENT-WORKFLOW-V1 once
    both Spec 4 and Spec 5 close. The self-improvement workflow
    consumes both Spec 4's execution primitives AND Spec 5's
    authoring primitives.
11. **Spec 7** — parallel-track RESPONSE-FIDELITY-V1 Batch 2.

## Linked artifacts

- Architect build directive: Notion
  `35effafef4db81d69ba5ce7d380df3b3`
- Founder direction (Kernos-authored workflows are essential and
  inherent): Notion `35effafef4db8111be97c985c1d5ac33`
- Architect verdict on Spec 4 split + governance reframing: Notion
  `35effafef4db817ab773de6a059f9fde`
- PHASE-3-AUTONOMY-LOOP design consideration: Notion
  `35cffafef4db81da8107e562307bc738`
- Seven-spec roadmap: Notion `35cffafef4db81c0b855cb0984dcd8df`
- Spec 4 (WORKFLOW-ORCHESTRATION-PRIMITIVES-V1) v2 ratification:
  Notion `35effafef4db819c8d37e1420920b0d5`
- Spec 4 implementation push: branch
  `workflow-orchestration-primitives-v1`, commit `f06cc10`
- Spec 1 close ratification (FRICTION-PATTERN-STABLE-IDS-V1):
  Notion `35dffafef4db818c982af6d7e69c5948`
- Spec 2 close ratification (CODING-SESSION-BRIDGE-V1): Notion
  `35dffafef4db8180b736d76dc6041196`
- Spec 3 close ratification (ACTIONSTATERECORD-WORKFLOW-COMPOSITION-V1):
  Notion `35effafef4db8103ac6afe2d13da40a6`
- Workflow primitive code (will be at HEAD once Spec 4 merges):
  `kernos/kernel/workflows/`
