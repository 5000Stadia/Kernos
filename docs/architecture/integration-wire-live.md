# Integration Wire-Live

Production-wiring spec that closes the gap PDI's loud-error guardrail
surfaced. Wires concrete implementations of the four Protocol-typed
service hooks (Planner / StepDispatcher / DivergenceReasoner /
PresenceRenderer), the EnactmentOutcome → ReasoningResult translation
seam, audit emission compatibility, and TurnRunner wiring into
`server.py`.

After this spec ships, `features.use_decoupled_turn_runner=True` is
safe — turns route through the new path successfully;
`TurnRunnerNotWired` no longer fires when wiring is in place.

## Composition with PDI

PDI shipped 11 structural-impossibility invariants. IWL preserves
all 11 and adds the concrete hook implementations. None of the
invariants relax.

| PDI invariant | IWL preserves via |
|---|---|
| Feature flag default OFF, legacy unchanged | reasoning.py routing unchanged when flag off |
| Thin path NEVER dispatches tools | Hook absence on thin-path code path |
| Streaming disabled inside full machinery | Protocol return types lack `streamed` field |
| `enactment.plan_created` emits BEFORE first dispatch | Order preserved in EnactmentService loop |
| Tier classification as explicit pure function | classify_routing untouched |
| Vocabulary locked at 7 SignalKinds | New synthetic tools enumerate exactly the seven |
| No same-turn integration re-entry | EnactmentService still has no integration_service param |
| Reintegration caps at construction | ReintegrationContext untouched |
| Friction observer write-only | Friction Protocol untouched |
| Envelope validation at every plan-changing tier | Tier handlers preserved |
| `enactment.*` audit family complete + references-not-dumps | Audit shape preserved |

## The four concrete hooks

### Planner (`kernos/kernel/enactment/planner.py`)

Conforms to `PlannerLike.create_plan(PlanCreationInputs) ->
PlanCreationResult`. Tool catalog filtered to
`inputs.briefing.action_envelope.allowed_operations` (the design review-corrected
access path). Defense-in-depth filters layered on top:
`allowed_tool_classes`, `forbidden_moves`.

Synthetic finalize tool `__finalize_plan__` carries the locked
7-signal vocabulary as a closed JSON-schema enum. Plan parsing
raises `PlannerError` on structural failure so EnactmentService
routes through B1.

Stamps `created_via`:
- `"initial"` when `prior_plan_id` is empty
- `"tier_4_reassemble"` when `prior_plan_id` is set

`StaticToolCatalog` ships as the v1 default; production wiring binds
the workshop registry as the catalog source. v1 same-model default —
the chain caller is the same callable integration uses by default.

### StepDispatcher (`kernos/kernel/enactment/dispatcher.py`)

Conforms to `StepDispatcherLike.dispatch(StepDispatchInputs) ->
StepDispatchResult`. The shipped six fields exactly: `completed`,
`output`, `failure_kind`, `error_summary`, `corrective_signal`,
`duration_ms`. No invented fields.

Operation resolution at dispatch time uses PDI C1's
`operation_resolver`. Per-operation timeout uses
`OperationClassification.timeout_ms` enforced via `asyncio.wait_for`.
Default fallback `DEFAULT_TOOL_TIMEOUT_MS=30s` when the operation's
timeout is 0/unset.

Tool executor is dependency-injected via the `ToolExecutor` Protocol.
Production wiring provides an executor that bridges to the workshop
dispatch primitive in ReasoningService. Single source of truth
across legacy and new path.

Trace-sink seam: dispatcher appends per-call entries in the legacy
shape (`{name, input, success, result_preview}`) into a list shared
with ReasoningService. The handler drains via
`drain_tool_trace()` after `reason()` returns.

**Drain-ordering invariant:** dispatcher only appends. Never drains.
Never clears. The handler owns the drain.

### DivergenceReasoner (`kernos/kernel/enactment/divergence_reasoner.py`)

Conforms to `DivergenceReasonerLike` four-method surface:

- `judge_divergence(inputs) -> DivergenceJudgment`
- `emit_modified_step(inputs) -> Step`
- `emit_pivot_step(inputs) -> Step`
- `formulate_clarification(inputs) -> ClarificationFormulationResult`

Per the design review edit: deterministic-vs-prose split lives INSIDE
`judge_divergence`. When structured signals all passed AND dispatch
completed, the method short-circuits without invoking the model.
Otherwise the prose path fires with explicit divergence framing.

Per-method synthetic tools (`__judge_divergence__`,
`__emit_modified_step__`, `__emit_pivot_step__`,
`__formulate_clarification__`). Modified-step and pivot-step schemas
list the locked 7-signal vocabulary in expectation structures.
Clarification schema enumerates the closed `ambiguity_type` enum
(target | parameter | approach | intent | other).

ClarificationFormulationResult caps NOT enforced in the reasoner —
PDI C1's ClarificationPartialState dataclass enforces them at
construction time. Over-cap output raises BriefingValidationError
loudly so the model can be tuned.

### PresenceRenderer (`kernos/kernel/enactment/presence_renderer.py`)

Conforms to `PresenceRendererLike.render(briefing) ->
PresenceRenderResult`. Awaited, NOT AsyncIterator (the design review edit).

Single renderer with kind-aware prompting. Branches structurally on
`briefing.decided_action.kind` to pick the system prompt:

- respond_only / defer / constrained_response / pivot
- propose_tool (renders proposal awaiting user confirmation; does
  NOT execute)
- clarification_needed first-pass
- execute_tool (full machinery terminal)

**B1 / B2 structural safety (the design review edit, load-bearing):**

Dedicated frozen dataclasses `B1RenderInputs` and `B2RenderInputs`
structurally exclude `discovered_information`. The renderer's
`render_b1` and `render_b2` entry points consume these dedicated
input types. `B2RenderInputs.from_partial_state(question,
partial_state)` factory takes a ClarificationPartialState (which
DOES carry discovered_information per PDI C1's contract) and drops
the field on the floor by construction.

The unsafe `discovered_information` lives in audit / reintegration
only, where it's referenced by `audit_refs`. Sentinel test pin
seeds discovered_information with `"RESTRICTED_SENTINEL_XYZ"` and
verifies the sentinel is absent from BOTH the renderer's prompt
input AND the rendered output text.

## Translation seam

`enactment_outcome_to_reasoning_result(...)` lives in
`kernos/kernel/response_delivery.py`. Targets the live ReasoningResult
fields ONLY:

```python
@dataclass
class ReasoningResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    duration_ms: int
    tool_iterations: int
```

No `tool_calls`, `assistant_content`, `stop_reason`, `provider`, or
`event_id` fields. Public ReasoningResult shape NOT widened by this
spec.

Translation aggregates tokens / cost across all hook model calls in
the turn (integration + planner + per-step divergence + presence) via
the shared `AggregatedTelemetry` accumulator. The wiring layer wraps
each hook's chain caller with `wrap_chain_caller_with_telemetry`
binding the SAME telemetry instance per turn.

`tool_iterations`: 0 for thin-path turns; equals the dispatch count
for full-machinery turns. Wiring layer increments via
`ProductionResponseDelivery.increment_tool_iteration()` after each
StepDispatcher invocation.

## Audit emission compatibility

**No-double-count invariant (the design review edit, locked):** inner hook model
calls do NOT emit `reasoning.*` events. Synthetic outer
`reasoning.request` / `reasoning.response` events emitted ONCE per
turn at the TurnRunner boundary by `ProductionResponseDelivery`.
Test pin verifies count of `reasoning.response` per new-path turn
equals exactly 1.

Synthetic event shape:
- `reasoning.request`: emitted at turn start with
  `trigger="turn_runner"` distinguishing from legacy emissions.
- `reasoning.response`: emitted at turn end with aggregated payload
  (tokens, cost, duration, model, termination_subtype,
  decided_action_kind, `turn_completed_via="decoupled"`).

Legacy `reasoning.*` event consumers (cost tracking, downstream
tools, audit replay) work unchanged on both paths because the
synthetic outer event matches legacy shape.

## `reasoning.py` minimal additive change

Per the design review edit (locked): `reasoning.py` is MINIMALLY EXTENDED, not
unchanged. The constructor accepts an optional `trace_sink: list[dict]`
parameter. When provided, the underlying `_turn_tool_trace` list IS
that injected list — shared with the new path's StepDispatcher. The
reasoning loop logic itself is unchanged.

`drain_tool_trace()` clears in-place rather than swapping for a new
list, so the shared reference stays valid across turns.

The handler's persistence seam works identically across paths: it
calls `drain_tool_trace()` once after `reason()` returns; the drain
returns entries from whichever path produced them.

## `server.py` production wiring

**Per-turn binding (IWL C6, the design review-mandated):** the production wiring
constructs a `turn_runner_provider` closure passed to
`ReasoningService(turn_runner_provider=...)`. The provider is
invoked PER TURN by `ReasoningService._run_via_turn_runner_provider`
and produces a fresh `(TurnRunner, ProductionResponseDelivery)`
bound to the request and event emitter for that turn.

The per-turn shape is load-bearing for three architectural
properties:

1. **Synthetic event single-emission.** `ProductionResponseDelivery`
   binds `(request, telemetry, event_emitter)` per turn so
   `emit_request_event()` fires once at start and the
   `reasoning.response` fires once at end. No-double-count
   invariant satisfied structurally.

2. **Per-turn telemetry binding.** `AggregatedTelemetry` is fresh
   per turn. The shared chain caller is wrapped with
   `wrap_chain_caller_with_telemetry(telemetry)` and that wrapped
   caller is plumbed into all four hooks (Planner,
   DivergenceReasoner, PresenceRenderer, IntegrationService).
   Token aggregation accumulates across all hook calls in the
   turn; cost-tracking aggregates ONCE.

3. **Tool-iteration accuracy.** StepDispatcher's
   `on_dispatch_complete` callback is bound to
   `telemetry.add_tool_iteration` so each successful dispatch
   increments the per-turn counter; ProductionResponseDelivery
   reads it for `ReasoningResult.tool_iterations`.

Construction order at startup:
1. `chains` from `build_chains_from_env()` (existing).
2. Shared trace sink (`reasoning_trace_sink: list[dict]`).
3. `CohortRegistry` + register covenant cohort.
4. `CohortFanOutRunner` (shared across turns).
5. Shared `_shared_chain_caller` against the primary chain.
6. Shared `_UnwiredDescriptorLookup` placeholder (raises
   NotImplementedError loudly when full-machinery turns hit it
   — design review-lean (a)).
7. Shared `_UnwiredExecutor` placeholder.
8. `_dispatcher_event_emitter` and `_dispatcher_audit_emitter`
   bridges into the existing event stream / audit store.
9. **`_build_per_turn_runner(request, event_emitter) -> (TurnRunner,
   ProductionResponseDelivery)`** closure: per-turn factory that
   constructs telemetry + wrapped chain + four hooks + integration
   service + enactment service + delivery + turn runner.
10. `ReasoningService(turn_runner_provider=_build_per_turn_runner,
    trace_sink=reasoning_trace_sink)`.

ReasoningService.reason() with the feature flag set routes to
`_run_via_turn_runner_provider`, which:
1. Calls the provider with the request → gets (TurnRunner, delivery).
2. Calls `delivery.emit_request_event()` ONCE.
3. Builds TurnRunnerInputs from the request.
4. Calls `turn_runner.run_turn(inputs)`.
5. Returns the ReasoningResult the delivery hook produced.

The cohort registration in v1 covers the covenant cohort
unconditionally (its only dependency is `StateStore`, which is
already wired in `server.py`). Gardener and memory cohort
registration is a follow-up enhancement that lands when their
service dependencies (`GardenerService` / `RetrievalService`) are
restructured out of `MessageHandler`'s lazy-init path.

**v1 loud-failure surface for unwired full-machinery dispatch:**
`_UnwiredDescriptorLookup.descriptor_for(tool_id)` raises
`NotImplementedError` rather than returning None. A graceful
"tool-not-registered" StepDispatchResult would look indistinguishable
from a misconfigured tool catalog during soak; the loud failure
makes the deferred workshop binding observable.

## Feature flag semantics

`KERNOS_USE_DECOUPLED_TURN_RUNNER` env var (PDI C2-shipped). Default
OFF. When set AND a TurnRunner is wired, `ReasoningService.reason()`
routes to the decoupled path. With this spec shipped, flipping the
flag is safe — production turns route through the new path.

Per-turn flag check; operators can flip without restarting.

## Equivalence telemetry

`ProductionResponseDelivery` emits
`turn_completed_via="decoupled"` on the synthetic
`reasoning.response` event. Legacy path's `reasoning.response` events
do not carry this field today; equivalence-test infrastructure can
distinguish path origin by either presence/absence of the field or by
`trigger="turn_runner"`.

Per-turn duration_ms is on the same event, enabling latency overhead
measurement on representative scenarios.

## Architectural pins (load-bearing tests)

- ReasoningResult shape unchanged: exactly the seven shipped fields.
  Pin via `dataclasses.fields` introspection.
- Synthetic `reasoning.response` count per new-path turn = 1
  (no-double-count invariant).
- B1RenderInputs / B2RenderInputs do NOT carry
  `discovered_information` (structural redaction pin).
- B2 sentinel test: `RESTRICTED_SENTINEL_XYZ` absent from prompt
  input AND rendered output.
- `ProductionResponseDelivery` has no `_trace_sink` attribute and no
  `drain` / `drain_tool_trace` methods (drain-ordering invariant).
- Chain wrapper has no event-emission parameters (no-double-count).
- DivergenceJudgment / ClarificationFormulationResult have no
  `streamed` field (streaming-disabled-by-construction).

**Composition pins (IWL C6, the design review-mandated):**

- The production turn_runner_provider's TurnRunner.response_delivery
  IS an instance of `ProductionResponseDelivery` (not a stub or
  lambda). Pinned in `tests/test_iwl_composition.py`.
- Per-turn provider construction produces FRESH AggregatedTelemetry
  + ProductionResponseDelivery instances on each call. Pinned via
  identity check across two provider invocations.
- The four hooks' chain callers are wrapped with
  `wrap_chain_caller_with_telemetry`. Verified by invoking the
  wrapped chain and confirming the per-turn telemetry accumulates.
- Production StepDispatcher has non-None `event_emitter` AND
  `audit_emitter` references AND non-None `on_dispatch_complete`
  callback (for tool_iterations counting).
- `_UnwiredDescriptorLookup.descriptor_for` raises
  NotImplementedError (design review-lean (a)). Source-inspection pin
  verifies the method body contains `raise NotImplementedError` and
  does NOT contain `return None`.
- End-to-end: `ReasoningService.reason()` with the feature flag
  set routes through the provider, emits exactly one
  `reasoning.request` and one `reasoning.response`, and returns a
  ReasoningResult with non-stub duration_ms.

## Files

```
kernos/kernel/enactment/planner.py
kernos/kernel/enactment/dispatcher.py
kernos/kernel/enactment/divergence_reasoner.py
kernos/kernel/enactment/presence_renderer.py
kernos/kernel/response_delivery.py
kernos/kernel/reasoning.py                  (minimal additive trace_sink seam)
kernos/server.py                            (production wiring)

tests/test_enactment_planner.py
tests/test_enactment_dispatcher.py
tests/test_enactment_divergence_reasoner.py
tests/test_enactment_presence_renderer.py
tests/test_response_delivery.py
tests/test_iwl_integration.py               (end-to-end smoke)
```

## Rollout

1. **IWL ships** (this batch): hooks + translation seam +
   `server.py` wiring + integration tests + contract pins.
   `features.use_decoupled_turn_runner=True` becomes safe for
   **thin-path soak validation only** (see explicit constraint below).
2. **INTEGRATION-WIRE-LIVE-WORKSHOP-BINDING (follow-up spec):**
   threads real request context into full-machinery dispatcher
   event/audit records (the `instance_id` propagation noted below)
   AND wires the workshop dispatch primitive into `ToolExecutor`.
   Required before broad soak that exercises full-machinery turns.
3. **Broad soak validation:** owner flips the flag for an
   instance; new path runs on real turns including full-machinery
   dispatches. Friction observer accumulates tickets; equivalence
   telemetry observed via the synthetic `reasoning.response`
   event's `turn_completed_via` field.
4. **PRESENCE-DECOUPLING-ACTIVATE:** flips flag default ON;
   deprecates legacy reasoning loop after benchmarked clearly and
   consistently better with no remaining tuning needs.

## Push-approval constraint (design review, IWL v3 review)

IWL is approved for push and thin-path soak. Broad soak (full-
machinery turns) is gated on the workshop-binding follow-up.

**Open thread surfaced at v3 approval:** `Briefing` / `AuditTrace`
don't currently carry `instance_id`. The dispatcher's audit entries
read instance_id via best-effort fallbacks (`_instance_id_from_briefing`
helper) which return `""` today. For thin-path turns that produce
no dispatcher records, this is invisible. For full-machinery turns
the audit entries land in the `""` instance bucket, which is wrong
shape for production audit partitioning.

The fix lives in INTEGRATION-WIRE-LIVE-WORKSHOP-BINDING:

  - Either thread `instance_id` onto `Briefing` / `AuditTrace`
    (V1 schema extension), or onto a separate request-context
    object the dispatcher reads.
  - Audit entries should partition by the actual instance_id, not
    the empty bucket.

The thin-path soak the v3 approval covers does not exercise this
gap; full-machinery dispatch is gated behind the
`_UnwiredDescriptorLookup` loud-failure surface until the workshop
binding lands.
