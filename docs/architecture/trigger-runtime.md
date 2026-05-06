# Trigger Evaluation Runtime

WORKFLOW-TRIGGERS-CONSOLIDATION-v1 (WTC v1, shipped C1-C7 + C5c-bringup-crb 2026-05-01 to 2026-05-04). The unified time + event trigger runtime that fires workflows from the system's event stream. WTC v1 collapsed three pre-existing trigger paths (legacy WLP post-flush hook, scheduler heartbeat, ad-hoc Pattern 05 helpers) into one substrate.

This page is the as-built reality. For the broader workflow-loop substrate (workflow descriptors, action verbs, approval gates, ledgers), see [`workflow-loops.md`](workflow-loops.md).

## Where WTC v1 sits

```
event_stream
    │
    │ post-flush hook (failure-isolated; legacy hook retired in production)
    ▼
InternalEventAdapter
    │
    │ on_event_observed(event)
    ▼
TriggerEvaluationRuntime  ◄──── unified path: time-driven AND event-driven
    │
    ├─ event selector match
    ├─ temporal relation evaluation (every / on / before / after)
    ├─ FireOutbox CAS state machine (fail-closed fire_id idempotency)
    ▼
WLP dispatch hook (ExecutionEngine.execute_workflow)
    │
    ▼
WorkflowRegistry / agent inboxes
```

The runtime owns both the cron walk (time-driven) and the event-driven match path. It dispatches into the existing `ExecutionEngine.execute_workflow`, so workflows run through the same engine regardless of trigger source.

## Module layout

WTC v1 lives in `kernos/kernel/triggers/`:

| Module | Purpose |
|---|---|
| `predicate.py` | Three-part predicate model (event_selector, temporal_relation, dispatch_policy) + deterministic fire_window_key + fire_id derivation. |
| `evaluator.py` | Per-temporal-kind evaluation helpers — cron windowing, event-selector match, due-time math. |
| `outbox.py` | `FireOutbox` durable dispatch outbox over the existing `trigger_fires` table. CAS-based status transitions; recovery sweep helpers. |
| `runtime.py` | `TriggerEvaluationRuntime`: start/stop, register, deactivate, `on_event_observed`, `evaluate_now`, `recover`. |
| `sources.py` | `EventSource` protocol + production sources: `InternalEventAdapter`, `CalendarSource`, `SchedulerHeartbeatSource`. |
| `external_sources.py` | External source contracts (C4): payload shape stubs for `email.message_observed`, `notion.page_observed`. No polling shipped — predicate language verified against the shapes. |
| `adapters/` | Compilers from descriptor.triggers (CRB Compiler) into runtime-shape `CompiledTrigger`. |
| `errors.py` | Typed error hierarchy. |

## Three-part predicate model

A `TriggerPredicate` is the registered unit. Three independent dimensions:

| Dimension | Type | Purpose |
|---|---|---|
| `event_selector` | Structured AST from the existing predicates module | Match against event payloads (event_type prefilter, key-value matchers, etc.) |
| `temporal_relation` | `TemporalRelation` (every / on / before / after) | Time semantics layered on the event match |
| `dispatch_policy` | `DispatchPolicy` (skip / catch_up / fan_out_within_window) | What to do when missed-window fires accumulate |

The three compose orthogonally. `every 1h` with `event.calendar.fired` selector and `catch_up` policy fires once per hour against calendar events; if downtime spans 8 hours, exactly one catch-up fire dispatches (not 8 fan-out fires).

## Fire ID idempotency (the critical invariant)

`derive_fire_id(predicate, fire_window_key)` produces a deterministic ID for a (predicate, window) pair. Insertions into `trigger_fires` use a partial unique index keyed on `fire_id`. Duplicate fire attempts (same predicate, same window) hit the unique constraint and fail closed — the second attempt knows the first already claimed the window.

Codex mid-batch fold (2026-05-01) tightened this from a log-and-continue to a fail-closed partial unique index after a race-unsafe path was identified during review.

The `fire_window_key` derivation is per-temporal-kind:

- `every`: window-aligned timestamp (e.g., the start of the current 1h window).
- `on`: the specific datetime.
- `before` / `after`: the relative timestamp.

Combined with the predicate's hash, this produces a stable fire_id that's safe to retry under crash-recovery.

## FireOutbox CAS state machine

`FireOutbox` over the `trigger_fires` table. State machine:

| State | Meaning |
|---|---|
| `claimed` | Runtime won the window race; dispatch in progress. |
| `dispatched` | WLP `execute_workflow` returned successfully; persisted as the dispatch receipt. |
| `dispatched_failed` | Dispatch raised; the row stays for diagnostics. |
| `recovered` | Recovery sweep observed an in-flight `claimed` row from a prior process and reconciled. |

CAS transitions: every UPDATE is conditional on the prior state via composite WHERE; a `rowcount = 0` means another path (concurrent recovery, late dispatch ack) got there first and the caller must re-read.

The recovery sweep runs once at runtime startup. It walks `claimed` rows from prior process lifetimes and reconciles them with WLP's execution registry by `fire_id` lookup — closing the design-review must-fix seam between the dispatch-claim and the actual workflow execution.

## EventSource protocol + post-flush adapter

The legacy `TriggerRegistry` post-flush hook is **retired in production** (per the WTC v1 C5c-bringup-crb design direction). Production wires `InternalEventAdapter` instead — a `EventSource` protocol implementation that bridges `event_stream`'s post-flush hook into the runtime's `on_event_observed`.

```python
class EventSource(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    # Implementations call runtime.on_event_observed(event) when
    # they have an event to surface. Source identity is the
    # registered EmitterRegistry source_module — sources cannot
    # spoof identity via payload.
```

Three production sources ship in `kernos/kernel/triggers/sources.py`:

| Source | Identity | What it emits |
|---|---|---|
| `InternalEventAdapter` | `internal` (post-flush bridge) | Every event the in-process event_stream flushes |
| `CalendarSource` | `calendar` | Calendar event observations (emit-only stub C3) |
| `SchedulerHeartbeatSource` | `scheduler.heartbeat` | Periodic heartbeat for cron-walk evaluation |

External sources (`email`, `notion`) have payload-shape stubs in `external_sources.py`. The predicate language is verified against these shapes; polling implementations are deferred.

## Missed-window semantics

`DispatchPolicy` controls what happens when a cron walk discovers more than one missed fire in `(last_evaluated, now]`:

| Policy | Behavior |
|---|---|
| `skip` (default) | Emit `workflow.missed_fire` event; dispatch nothing. |
| `catch_up` | Emit `workflow.missed_fire` for the missed windows; dispatch the **single most recent** as one catch-up fire. No fan-out for long downtime. |

### Why no `fan_out_within_window` policy

A third policy was contemplated during design — `fan_out_within_window` — that would dispatch one workflow per missed window in the lookback range. Eight hours of process downtime on an `every 1h` predicate would fire eight workflows, one per hour gap.

This was **deliberately scoped out**. Long-downtime fan-out is a bug magnet:

- A reminder workflow that fires "you have a meeting in 30 minutes" would dispatch eight times after eight hours of downtime — by which point all eight reminders are stale and most are about meetings that have already happened.
- An aggregation workflow that processes hourly batches would re-process the same eight hours that some other recovery path already handled, producing duplicate side-effects.
- A notification workflow that pings the user on schedule would deliver eight rapid-fire pings the moment the system comes back, which is uniformly worse than `catch_up`'s single catch-up fire.

The fan-out shape only earns its keep when the workflow's side-effects are genuinely per-window distinct AND idempotent under repeated dispatch — a narrow case. `_MISSED_WINDOW_VALUES = frozenset({"skip", "catch_up"})` is the hard cap.

If a future use case needs per-missed-window dispatch, it'll be added explicitly with idempotency requirements named in the spec — never as a default. The substrate's CAS-based `fire_id` deduplication closes part of the safety gap (re-dispatching the same `(predicate, window)` is idempotent), but the broader "do you actually want N fires?" question is deliberately surfaced to the workflow author rather than absorbed into a default.

The substrate-emitted `workflow.missed_fire` event records the missed window so audit/diagnostics show what the runtime chose not to fire (skip) or collapsed into the catch-up fire (catch_up).

This was the spec's C6 contract. Pattern 05 (which used to do its own one-off scheduling) is being retired in favor of the unified runtime; the C5c-2 manage_schedule translation adapter (`register_managed_schedule_workflow`) bridges the legacy `manage_schedule` tool surface onto the unified runtime via synthetic workflows.

## STS atomic registration (C5b)

`register_workflow` on `SubstrateTools` accepts a `descriptor.triggers` block and registers the workflow + its triggers atomically. Step 1b pre-compiles the descriptor's triggers into `TriggerPredicate` shape; step 10 hydrates them into the runtime at registration time. Either the workflow row + all triggers land together, or nothing changes.

This closes the gap where a workflow could be registered but its triggers fail to install, leaving an unfireable workflow in the registry.

## Heartbeat consolidation (C5c-1)

The `AwarenessEvaluator` drives a unified runtime heartbeat — Phase 2b additive after legacy Phase 2. The recovery sweep runs once at start; the heartbeat is error-isolated so a single tick failure doesn't take down the runtime.

## What composes against this

CRB consumes the trigger runtime via `register_workflow` — when a user approves a routine, CRB hands the descriptor to STS, which atomically registers the workflow and its triggers into the runtime.

The scheduler tool (`manage_schedule`) consumes via the `register_managed_schedule_workflow` translation adapter (C5c-2-prep): user-facing schedule operations turn into synthetic workflow registrations under the unified runtime.

External event-driven flows (email-triggered, notion-triggered) compose against the EventSource protocol; the runtime evaluates predicates uniformly regardless of source.

## What WTC v1 replaced

- Legacy `TriggerRegistry` post-flush hook → retired in production (test fixtures may still attach it).
- Pattern 05 ad-hoc scheduling → being collapsed onto unified runtime via translation adapter.
- Scheduler-only one-off heartbeat → consolidated into the AwarenessEvaluator-driven heartbeat.
- Per-component fire-id derivations → unified deterministic derivation.

This is one of the larger architectural consolidations in Kernos's history. See spec body in Notion for the full C1-C7 phase tracker.
