# Presence Decoupling

The four-layer cognition architecture introduced by
PRESENCE-DECOUPLING-INTRODUCE (PDI). Replaces the central reasoning
loop with three composable services routed by a coordinator:

```
cohort fan-out → IntegrationService → EnactmentService → response
```

PDI ships behind a feature flag (default OFF) so the legacy
reasoning loop and the new path coexist while equivalence is
validated. PRESENCE-DECOUPLING-ACTIVATE (Spec 2, future) flips the
default and removes the legacy loop after benchmarked clearly and
consistently better.

For canonical loop semantics — three-question check, five-tier
response hierarchy, the step-as-hypothesis frame — refer to the
ENACTMENT-LOOP-SEMANTICS deep-work artifact (settled
2026-04-26 with input from Owner, the design review, CC). The artifact is the
source of truth for any architectural question this document does
not explicitly cover.

## The three services

### TurnRunner (`kernos/kernel/turn_runner.py`)

Pure orchestration. No domain logic. Composes:

```
TurnRunner.run_turn(inputs)
  ├─ run_cohort_fan_out → CohortFanOutResult
  ├─ run_integration   → Briefing      (uses build_integration_inputs_from_fan_out)
  ├─ run_enactment     → outcome
  └─ deliver(briefing, outcome) → response
```

Each composable seam (`run_cohort_fan_out`, `run_integration`,
`run_enactment`, `deliver`) is independently testable. Dependencies
are injected as Protocols (`IntegrationServiceLike`,
`EnactmentServiceLike`) so the skeleton can land before the concrete
services do and tests can use stubs.

**Load-bearing seam (the design review edit, acceptance criterion #7):**
`required_safety_cohort_failures` flows from `CohortFanOutResult` to
`IntegrationInputs` at the TurnRunner → IntegrationService boundary.
The plumbing uses `build_integration_inputs_from_fan_out` (added in
COHORT-ADAPT-COVENANT C1) so the safety-policy plumbing stays
consistent across call sites.

### IntegrationService (`kernos/kernel/integration/service.py`)

Production-shaped façade over the V1 `IntegrationRunner`. Conforms
to TurnRunner's `IntegrationServiceLike` Protocol:

```
async def run(inputs: IntegrationInputs) -> Briefing
```

The runner already enforces:

- Redaction invariant (no Restricted cohort content in briefing
  text fields).
- Safety policy (required+safety_class cohort failures force defer
  / constrained_response).
- ActionEnvelope contract: execute_tool requires a well-formed
  envelope; non-action kinds reject one (Briefing-level structural
  rule).
- Fail-soft on errors (Defer briefing when safety-degraded; minimal
  RespondOnly otherwise — never respond_only on safety-degraded).

The service is a thin delegate; the substantive C3 work was the
prompt update teaching the model the new variants.

### EnactmentService (`kernos/kernel/enactment/`)

Branch decision at entry, by `briefing.decided_action.kind`:

- **Render-only kinds → thin path:** `respond_only`, `defer`,
  `constrained_response`, `pivot`, `clarification_needed`,
  `propose_tool` (the design review edit — propose_tool is render-only because
  the actual dispatch lives on the next turn after user confirms).
- **Dispatch kind → full machinery:** `execute_tool` only.

Branch decision is structural — based on the enum kind, not model
judgment.

#### Thin path (render-only)

The thin-path code path takes only the presence renderer. NO
dispatcher reachable from this branch. The "thin path never
dispatches tools" rule is therefore a code-shape guarantee, not a
runtime check. Streaming permitted — the renderer drives.

#### Full machinery

```
plan creation
  ├─ enactment.plan_created (audit BEFORE first dispatch)
  └─ envelope validation on initial plan

per step:
  ├─ dispatch
  ├─ enactment.step_attempted
  ├─ three-question check (completed / effect / plan-validity)
  ├─ classify_routing(check, failure_kind, budgets)  ← pure function
  └─ tier handler:
       Tier 1 retry  (transient, retry budget)
       Tier 2 modify (corrective signal, modify budget, envelope-validated)
       Tier 3 pivot  (information divergence, pivot budget, envelope-validated)
       Tier 4 reassemble (plan invalidated, decided_action valid; envelope-validated)
       Tier 5 surface:
         B1 — action invalidated (capped reintegration → next turn)
         B2 — user disambiguation (ClarificationNeeded → next turn; NO same-turn re-entry)

terminal render via presence_renderer (only post-loop streaming point)
```

**Streaming-disabled-by-construction:** the inner-loop dependency
Protocols (`PlannerLike`, `StepDispatcherLike`,
`DivergenceReasonerLike`) return structured-data dataclasses
(`PlanCreationResult`, `StepDispatchResult`, `DivergenceJudgment`)
that have no `streamed` field. The streaming-capable type
(`PresenceRenderResult`) is reachable only from the thin path AND
from the terminal render after all steps complete. There is no
code path inside the loop where a streaming surface is reachable.

**No same-turn integration re-entry:** EnactmentService has no
`integration_service` parameter on its constructor. The B2
termination path constructs `ClarificationNeeded` directly via
`DivergenceReasonerLike.formulate_clarification` — never calls back
into integration. Same-turn re-entry is unreachable because the
dependency is structurally absent.

## Explicit ActionEnvelope contract

Per the design review edit on PDI: `ActionEnvelope` is structural, not prose-derived.
A `Briefing` whose `decided_action.kind` is `execute_tool` MUST carry
an `action_envelope` field; non-action kinds MUST omit it. The
`Briefing` dataclass enforces both rules at construction.

Envelope fields:

```
ActionEnvelope:
  intended_outcome: str
  allowed_tool_classes: tuple[str, ...]
  allowed_operations: tuple[str, ...]
  constraints: tuple[str, ...]
  confirmation_requirements: tuple[str, ...]
  forbidden_moves: tuple[str, ...]
```

Validation runs at every plan-changing tier:

- Initial plan creation
- Tier-2 modify (the design review edit — `same intent` is model assertion, not
  runtime guarantee)
- Tier-3 pivot
- Tier-4 reassemble (new plan path)

Single pure function `validate_step_against_envelope` /
`validate_plan_against_envelope` — used by every tier. Validation
violation → terminate B1 with envelope-violation reason.

This makes "EnactmentService never changes decided_action"
structurally testable rather than aspirational.

## Operation resolver pattern

Tools whose read/write classification depends on argument values
(`manage_covenants` is canonical) declare an `operation_resolver`
callable on `ToolDescriptor`:

```python
operation_resolver: Callable[[Mapping[str, Any]], str] | None
```

Resolution rules at dispatch time (`kernos/kernel/tools/operation_resolver.py`):

1. Explicit operation_name in the call → use it.
2. Else `operation_resolver(args)` → use the result.
3. Else single-entry operations map → use that.
4. Else ambiguous → conservative `sensitive_action` fallback. Tool
   NEVER surfaced to integration's catalog when ambiguous.

`is_surfacable_to_integration(descriptor, ...)` returns True only
when resolution is unambiguous AND safety is `read_only`. The
catalog filter is structural — ambiguous tools are not just
deprioritized, they are absent from integration's surfaced catalog.

## No same-turn integration re-entry

Architecturally critical. By construction, not runtime check:

- `EnactmentService.__init__` has no `integration_service` parameter.
- The B2 code path constructs `ClarificationNeeded` directly via
  `DivergenceReasonerLike.formulate_clarification`.
- Reintegration payload (capped per Section 5h) is stored on the
  `EnactmentOutcome` for the NEXT turn's integration.

When the user replies on turn N+1, integration sees the partial
state alongside the user's reply and either resolves into a refined
action OR produces a new `clarification_needed` if still ambiguous.

The cap on reintegration is enforced at construction
(`ReintegrationContext.__post_init__`):

- `tool_outcomes_summary` ≤ 1000 chars
- `discovered_information` ≤ 500 chars
- `plans_attempted` ≤ 5 PlanRefs
- `audit_refs` unbounded (references-not-dumps)
- `truncated` flag set when any field is clipped

Construction is the only enforcement point; over-cap instances
cannot exist in memory.

## Friction observer

Write-only sink (`kernos/kernel/enactment/friction.py`). Records
tier-1/2 exhaustion patterns:

- `TIER_1_RETRY_EXHAUSTED` — transient failures cycled through retry
  budget without success
- `TIER_2_MODIFY_EXHAUSTED` — corrective signals or non-transient
  failures cycled through modify budget without success

The Protocol exposes only `record(ticket: FrictionTicket) -> None`.
No query method through which a ticket could feed back to routing.
Tickets accumulate; operators read them; the EnactmentService never
reads back. v1 contract: friction tickets do NOT affect dispatch;
do NOT short-circuit retry/modify.

The audit family also emits `enactment.friction_observed` so the
broader audit pipeline can cross-reference friction with other
enactment.* events.

## Audit family

Seven categories. References-not-dumps invariant: plan payloads in
audit entries reference plans by ID, never embed plan content.

| Category | Required fields |
|---|---|
| `enactment.plan_created` | turn_id, integration_run_id, plan_id, step_count, created_at, created_via |
| `enactment.step_attempted` | turn_id, plan_id, step_id, attempt_number, tool_id, operation_name, tool_class, completed, failure_kind, error_summary, duration_ms |
| `enactment.step_modified` | turn_id, plan_id, original_step_id, modified_step_id, reason, envelope_validation_passed |
| `enactment.step_pivoted` | turn_id, plan_id, original_step_id, replacement_step_id, reason, envelope_validation_passed |
| `enactment.plan_reassembled` | turn_id, prior_plan_id, new_plan_id, reason, triggering_context_summary, reassembly_count, new_step_count |
| `enactment.terminated` | turn_id, integration_run_id, decided_action_kind, subtype, text_length |
| `enactment.friction_observed` | turn_id, tool_id, operation_name, divergence_pattern, attempt_count, decided_action_kind |

Termination subtypes (closed enum):

- `success_thin_path` — thin-path render of any conversational
  kind (respond_only, defer, constrained_response, pivot,
  first-pass clarification_needed)
- `success_full_machinery` — full machinery happy path; all steps
  completed cleanly with terminal render
- `thin_path_proposal_rendered` — propose_tool render
- `b1_action_invalidated` — full machinery B1
- `b2_user_disambiguation_needed` — full machinery B2 OR thin-path
  B2-routed clarification

`success_thin_path` and `success_full_machinery` are kept distinct
so audit filters can distinguish where each completion came from.

Redaction invariants apply. EnactmentService consumes briefings
that are already presence-safe (integration's runner enforces);
audit entries reference fields by ID and never quote covenant rule
text, restricted memory content, or restricted context-space
material.

## Feature flag

`KERNOS_USE_DECOUPLED_TURN_RUNNER`. Default OFF.

When set (`1`, `true`, `yes`, `on`) AND a `TurnRunner` is wired into
`ReasoningService`, `ReasoningService.reason()` routes to the
decoupled path. Otherwise the legacy reasoning loop runs unchanged.

When the flag is set but no `TurnRunner` is wired, the service
raises `TurnRunnerNotWired` with a pointer to PDI C2-C7 /
INTEGRATION-WIRE-LIVE rather than producing a half-formed turn.

## Equivalence testing

Per the design review edit, equivalence is asserted along five dimensions:

1. **User-facing outcome** — functional equivalence; precise wording
   may differ (model variance is acceptable).
2. **Tool calls / args** — same tools fired with equivalent args.
3. **Side-effect ordering** — mutating tool sequences fire in the
   same order on both paths. Tested with fakes / dry-run stores; no
   live irreversible side effects in equivalence test
   infrastructure.
4. **Audit / redaction** — legacy categories preserved; new path
   ADDS `enactment.*` entries without removing existing categories.
   No restricted content leaks into audit.
5. **Latency telemetry** — both paths emit comparable latency
   telemetry; new path's overhead is observable (expected;
   quantified in equivalence reports).

Equivalence test infrastructure ships in `tests/test_pdi_equivalence.py`.
Real-provider end-to-end equivalence runs against the live test
markdown at
`data/diagnostics/live-tests/PRESENCE-DECOUPLING-INTRODUCE-live-test.md`.

## Rollout plan

1. **PDI (this spec)**: ship feature-flag-gated decoupled path with
   equivalence infrastructure and the seven-event audit family.
2. **INTEGRATION-WIRE-LIVE**: wire the production hookup —
   ReasoningService construction site builds the TurnRunner with
   real cohort runner, IntegrationService, EnactmentService, and
   response delivery. Equivalence runs against real-provider
   scenarios.
3. **PRESENCE-DECOUPLING-ACTIVATE (Spec 2)**: flip the feature flag
   default to ON after soak validation. Deprecate legacy reasoning
   loop. Full removal after benchmarked clearly and consistently
   better with no remaining tuning needs.

## Architectural pins (load-bearing tests)

- `EnactmentService.__init__` has no `integration_service` parameter
  (no same-turn re-entry, by construction).
- The thin-path code body never invokes the dispatcher even when
  one is wired (verified by routing every render-only kind through
  a service whose dispatcher raises on call).
- `PlanCreationResult`, `StepDispatchResult`, `DivergenceJudgment`
  have no `streamed` field (streaming unreachable inside the loop).
- `enactment.plan_created` audit emits BEFORE the first dispatch
  (audit ordering pin).
- The seven `SignalKind` enum values are exactly:
  `count_at_least`, `count_at_most`, `contains_field`,
  `returns_truthy`, `success_status`, `value_equality`,
  `value_in_set`. Adding an eighth is a coordinated migration.
- The seven enactment audit categories ship and are consistently
  shaped.

## Files

```
kernos/kernel/turn_runner.py
kernos/kernel/integration/service.py
kernos/kernel/integration/briefing.py     (extended: ClarificationNeeded, ActionEnvelope)
kernos/kernel/integration/template.py     (prompt update — clarification + envelope)
kernos/kernel/integration/runner.py       (envelope-required parsing)
kernos/kernel/tool_descriptor.py          (extended: OperationSafety, operation_resolver)
kernos/kernel/tools/operation_resolver.py
kernos/kernel/enactment/__init__.py
kernos/kernel/enactment/service.py
kernos/kernel/enactment/plan.py           (Plan + Step + 7 SignalKinds)
kernos/kernel/enactment/envelope.py       (validation pure function)
kernos/kernel/enactment/tiers.py          (classify_routing pure function)
kernos/kernel/enactment/reintegration.py  (capped payload + ExecutionTrace)
kernos/kernel/enactment/friction.py       (write-only sink)
kernos/kernel/reasoning.py                (façade routing on feature flag)

tests/test_integration_briefing.py        (PDI extensions)
tests/test_integration_safety_policy.py   (envelope-required execute_tool)
tests/test_integration_service.py
tests/test_operation_resolver.py
tests/test_tool_descriptor.py
tests/test_turn_runner.py
tests/test_enactment_plan.py
tests/test_enactment_envelope.py
tests/test_enactment_tiers.py
tests/test_enactment_service.py
tests/test_enactment_reintegration.py
tests/test_enactment_friction.py
tests/test_enactment_audit_family.py
tests/test_enactment_redaction.py
tests/test_pdi_equivalence.py
```
