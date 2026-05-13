# WORKFLOW-AUTHORING-PRIMITIVES-V1 — Implementation Spec

**Status:** DRAFT v1 — pre-implementation. Awaiting Codex pre-spec
review.

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

**Flow:**

1. Tool dispatched with `(descriptor, governance_tier)`.
2. Spec 4 `_build_workflow(descriptor)` constructs Workflow dataclass.
3. Spec 4 `validate_workflow(wf)` runs (raises on structural error).
4. Spec 5 governance-tier classifier runs over the validated
   workflow (see Decision 6). If `Kernos-issued + substrate_tier`,
   rejection.
5. Persist to Spec 4 `workflows` table (via existing
   `_register_workflow_unbound`) AND to new `registered_workflows`
   table (within the same transaction).
6. Return `(success=True, workflow_id, [])`.

**ActionStateRecord** emitted per Spec 3 discipline:
`surface="workflow_authoring"`, `operation="register_workflow"`,
`operation_class="register"`, `risk_level="medium"`,
`affected_objects=[workflow_id]`.

### Decision 2 — Trigger registration tool surface

```
register_trigger(workflow_id: str, event_type: str, predicate: dict) -> dict
```

- `workflow_id`: must exist in `registered_workflows`. Tool checks.
- `event_type`: event stream type the trigger watches.
- `predicate`: Spec 4 predicate AST (or DSL string compiled via
  trigger_compiler at registration time).
- Returns: `{success: bool, trigger_id: str | None, errors: list[ValidationError]}`.

**Persistence:** Spec 4 `TriggerRegistry` already supports the
underlying storage. This tool composes against the registry's
`register_trigger` method; new column to track which workflow
authored the trigger (mostly equal to the workflow's
`authored_by`).

**Trigger active-state inheritance:** triggers fire only while
their workflow's activation_state is `active`. The
TriggerRegistry consults `registered_workflows.activation_state`
on every match candidate; rejects matches against
`registered_not_activated` or `deactivated` workflows. This is the
substrate-level enforcement of the activation gate.

**ActionStateRecord:** `operation="register_trigger"`,
`operation_class="register"`, `risk_level="medium"`,
`affected_objects=[workflow_id, trigger_id]`.

### Decision 3 — Workflow activation tool (architect-only)

```
activate_workflow(workflow_id: str) -> dict
```

**Architect-only enforcement** (the safety boundary). The tool
checks the calling context (turn-side or event-side) against an
architect-actor predicate. Kernos calling this tool fails with
`not_authorized`.

**Activation flow:**

1. Verify caller is architect (via Spec 6's architect-gate identity
   or, for v1, a hardcoded actor_id check).
2. Look up workflow in `registered_workflows`. Must exist and be in
   `registered_not_activated` state (idempotent: already-active
   returns `success=True` with `already_active=True`).
3. Re-run `validate_workflow` and governance-tier classification
   (defensive: substrate primitives may have changed since
   registration; architect ratification at activation is the
   safety boundary, so re-validation here is intentional).
4. Transition activation_state to `active`, set `activated_at`.
5. Triggers for this workflow begin firing on the next
   TriggerRegistry match cycle.

**ActionStateRecord:** `operation="activate_workflow"`,
`operation_class="manage"`, `risk_level="high"` (this is the safety
boundary; high risk_level surfaces it loudly in audit).

**Composition with Spec 6 gate protocol:** in production, the
architect ratifies via an event-stream event (e.g.,
`autonomy_loop.architect_workflow_activation`). The
`activate_workflow` tool consumes such events through Spec 4's gate
predicate evaluator. For v1, the tool can also be invoked
synchronously by a developer in test contexts.

### Decision 4 — Workflow deactivation tool (architect-only)

```
deactivate_workflow(workflow_id: str, *, reason: str = "") -> dict
```

**Architect-only.** Same authorization check as `activate_workflow`.

**Deactivation flow:**

1. Verify caller is architect.
2. Look up workflow; must be `active`.
3. Transition activation_state to `deactivated`, set `deactivated_at`,
   capture `reason` in metadata.
4. Triggers stop firing on the next match cycle.
5. In-flight executions: per Spec 4's `aborted_by_restart` policy
   semantics, the engine doesn't actively interrupt running
   executions; they complete naturally or terminate via their
   workflow's own termination paths.

**Reversible:** a deactivated workflow can be reactivated via
`activate_workflow` (re-runs validation; transitions to `active`).

**ActionStateRecord:** `operation="deactivate_workflow"`,
`operation_class="manage"`, `risk_level="medium"`.

### Decision 5 — Validation feedback channel

**Architect's lean (adopted):** structured ValidationError shape
that's specific enough for Kernos to self-correct.

```python
@dataclass
class ValidationError:
    field_path: str        # "action_sequence[2].parameters.condition"
    category: str          # "missing_required" | "unknown_step_id" | ...
    message: str           # human-readable description
    severity: str = "error"  # "error" | "warning"
```

**Categories the validator surfaces:**

| Category                          | When raised                                                              |
|-----------------------------------|--------------------------------------------------------------------------|
| `missing_required_field`          | A required descriptor field is absent                                    |
| `invalid_value`                   | Field value violates type / enum / format constraint                     |
| `unknown_action_type`             | Descriptor references an action_type not in KNOWN_ACTION_TYPES           |
| `unknown_step_id`                 | Template reference points at a step_id not declared in the workflow      |
| `unknown_gate_name`               | Gate reference points at a gate_name not declared                        |
| `duplicate_step_id`               | Two steps share an id (across main + terminal_branches)                  |
| `invalid_identifier`              | Step id / gate name / branch name violates the grammar regex             |
| `dangling_branch_target`          | Branch verb target doesn't resolve to a declared step                    |
| `circular_branch`                 | Branch graph forms a cycle (DAG-validation; Spec 4 substrate)            |
| `governance_tier_violation`       | Kernos attempted to register a substrate_tier workflow                   |
| `descriptor_shape_invalid`        | Descriptor isn't a dict / has unparseable structure                      |
| `predicate_invalid`               | Predicate AST fails Spec 4 predicate validation                          |

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

### Decision 6 — Governance-tier classification

**Architect's lean (adopted):** hardcoded substrate-surface list
for v1; classify on whether the descriptor references any
substrate-modification target.

**Substrate-modification surfaces (hardcoded for v1):**

- Workflow primitive itself (any `mark_state` / `append_to_ledger` /
  `call_tool` action whose target is the workflow registry, engine,
  or descriptor parser surfaces)
- Authoring layer (this spec's tools — `register_workflow`,
  `register_trigger`, `activate_workflow`, `deactivate_workflow`)
- Friction-pattern catalog (any action targeting the
  `friction_pattern_store` table or its methods)
- Bridge primitive (any action targeting the bridge directory or
  its session tooling)
- The loop infrastructure (any action that modifies
  `registered_workflows.activation_state` outside of the
  architect-only activation tool)

**Detection rule:** the classifier walks `action_sequence` +
`terminal_branches`. For each action:

- If `action_type == "call_tool"` and `tool_id` is one of the
  hardcoded substrate-tool-ids → substrate_tier
- If `action_type == "mark_state"` and target key namespace matches
  substrate state keys → substrate_tier
- Otherwise → composition_tier (so far)

The classifier returns the COMPUTED tier. The
`register_workflow` tool compares against the caller-claimed
`governance_tier`:

- Match → proceed
- Caller claims `substrate_tier` and computed is `composition_tier`
  → proceed (architect can voluntarily over-classify; rare)
- Caller claims `composition_tier` and computed is `substrate_tier`
  → reject (this is the safety boundary)
- Computed `substrate_tier` AND caller is Kernos (not architect)
  → reject with structured ValidationError category
  `governance_tier_violation`

**Architect's `register_workflow` calls can override** the
classifier with an explicit governance_tier argument. This is rare
but supported (an architect intentionally registers a
substrate_tier workflow). Audit captures the override loudly.

**Substrate-surface list is hardcoded** for v1 (per architect's
lean). If the list grows, a future spec extends it. The list lives
in a single module constant for visibility.

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

### Decision 8 — Composition with friction patterns

**Soft-prompted suggestion path:**

1. Spec 1's `friction.pattern_recurrence` event fires (a friction
   pattern's resolved-then-recurred path).
2. Spec 5 disposition layer subscribes to these events via the
   event_stream.
3. The disposition's heuristic checks if the pattern's
   `description` / `signal_type_keys` match a "workflow-resolvable"
   profile (architect-curated tags on the pattern catalog; v1
   ships a small initial set).
4. If matching, the disposition adds a soft reflection to Kernos's
   next reasoning surface: `"Pattern <pattern_id> recurred again.
   Consider authoring a workflow to handle this autonomously.
   Reference the pattern_id in your spec."`
5. Kernos decides whether to compose a workflow descriptor + call
   `register_workflow`. Architect ratifies activation.

**Not force-driven** — the disposition surfaces the reflection;
Kernos's reasoning decides. If Kernos chooses immediate response or
some other resolution, the friction pattern continues recurring
until something resolves it (could be Kernos eventually, could be
architect, could be a manual fix).

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
   `friction_pattern_store.transition_lifecycle`; classifier
   computes substrate_tier; tool returns
   `{success: False, errors: [ValidationError(category="governance_tier_violation")]}`.

2. **`test_architect_substrate_tier_workflow_accepted`** — architect
   (identified via actor_id) registers the same substrate_tier
   workflow; tool returns success.

3. **`test_classifier_walks_main_and_terminal_branches`** — workflow
   with composition-tier main_sequence BUT a substrate-modifying
   action in a terminal_branch; classifier returns substrate_tier;
   Kernos-issued registration rejected.

### Test category 3: Architect-only activation

`tests/test_workflow_authoring.py::TestActivation`

1. **`test_kernos_cannot_activate`** — Kernos calls
   `activate_workflow`; tool returns
   `{success: False, errors: [ValidationError(category="not_authorized")]}`;
   activation_state remains registered_not_activated.

2. **`test_architect_activates_registered_workflow`** — architect
   calls `activate_workflow`; activation_state transitions to
   `active`; activated_at populated; trigger fires on next match.

3. **`test_inactive_workflow_triggers_do_not_fire`** — workflow
   registered but not activated; matching event flushes; verify
   trigger does NOT fire (no execution row created).

4. **`test_activated_workflow_triggers_fire`** — after activation,
   verify trigger DOES fire on matching event.

5. **`test_reactivation_idempotent`** — calling activate on
   already-active workflow returns success with `already_active=True`;
   no state mutation.

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

## Sequence (per architect directive)

1. ✅ Architect-framed (Notion `35effafef4db81d69ba5ce7d380df3b3`,
   Spec 5 build directive).
2. ✅ CC drafts spec at `specs/WORKFLOW-AUTHORING-PRIMITIVES-V1.md`
   on branch `workflow-authoring-primitives-v1` (this commit).
3. 🟡 **Codex pre-spec review** — pending. Architect provides
   pasteable blip after CC commits. Multi-round review expected
   given schema-touching + governance-tier complexity.
4. CC folds Codex round 1.
5. Architect ratification of revised spec.
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
