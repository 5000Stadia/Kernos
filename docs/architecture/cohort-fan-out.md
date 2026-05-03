# Cohort Fan-Out Runner

First follow-on spec to **INTEGRATION-LAYER-V1**. Builds the per-turn
parallel cohort execution infrastructure that produces `CohortOutput`
artifacts matching the V1 schema.

The runner is opt-in callable — nothing in the existing reasoning loop
or message handler invokes it. `INTEGRATION-WIRE-LIVE` later in the
arc wires fan-out → integration → presence into the production turn
pipeline.

## What the runner does

Given a registered set of cohorts and a per-turn `CohortContext`, the
runner fires every cohort in parallel, collects their outputs, and
returns when all cohorts have completed, timed out, or errored.
Failure isolation is structural: one cohort raising, hanging past
its timeout, or being cancelled does not poison the others.

```
CohortRegistry  ──┐
                  ├──►  CohortFanOutRunner.run(context)  ──►  CohortFanOutResult
CohortContext   ──┘
```

Result shape:

| Field | Type |
|---|---|
| `outputs` | `tuple[CohortOutput, ...]` — one per registered cohort, in registration order |
| `fan_out_started_at` / `fan_out_completed_at` | ISO-8601 UTC |
| `global_timeout_engaged` | `bool` |
| `required_cohort_failures` | `tuple[str, ...]` — cohort_ids of failed required cohorts |
| `required_safety_cohort_failures` | `tuple[str, ...]` — subset where `safety_class: True` |

## Cohort descriptor

Each cohort registers via a `CohortDescriptor`:

| Field | Type | Notes |
|---|---|---|
| `cohort_id` | `str` (snake_case) | Matches the source_type prefix in V1's CohortOutput taxonomy |
| `run` | `async (CohortContext) -> CohortOutput` | Sync callables rejected at registration |
| `timeout_ms` | `int > 0` | Per-cohort wall-clock budget |
| `default_visibility` | `Visibility` | Applied if the cohort doesn't set its own |
| `required` | `bool` | If True and the cohort fails, the fan-out is degraded |
| `safety_class` | `bool` | When True + required, failure forces constrained_response/defer |
| `execution_mode` | `ExecutionMode` | Only `ASYNC` accepted in v1; `THREAD` reserved |

## Registry

Design Review-controlled, not user-extensible (matches V1's deferral of
dynamic user-built cohorts). Cohorts register at boot via explicit
calls; the runner consumes the registered list. The registry enforces:

- snake_case `cohort_id` format
- positive-int `timeout_ms`
- `inspect.iscoroutinefunction` on the unwrapped run callable
- unique `cohort_id` across registrations
- only `ExecutionMode.ASYNC` (the `THREAD` value produces a future-
  spec landing-zone error rather than a generic value error)

## Failure isolation guarantee (narrowed)

**Async-task-per-cohort isolates yielding coroutines from each other.**
A cohort that awaits cooperatively and respects its timeout can be
cancelled without affecting other cohorts.

The runner does **NOT** isolate against:

- Synchronous infinite loops in cohort code
- CPU-bound work that doesn't yield
- Blocking I/O that doesn't release the event loop
- Memory exhaustion (no process-level isolation in v1)

Mitigations:

- v1 rejects sync callables at registration. The error names the
  cohort and points at `loop.run_in_executor` as the offload pattern
  for cohorts that wrap blocking work.
- Cohort review checks for CPU-bound and blocking patterns.
- A future spec may add `execution_mode: thread` with bounded-executor
  isolation if a real use case emerges.

This narrowing is honest about what asyncio actually buys us. The
runner enforces what it can (timeout, registration-time async-only
check) and documents what it cannot.

## Fan-out execution model

- All registered cohorts fire in parallel via `asyncio.create_task`.
- Each task wraps the cohort's run callable in
  `asyncio.wait_for(timeout=timeout_ms/1000)`.
- The runner awaits `asyncio.wait(tasks, timeout=global_timeout,
  return_when=ALL_COMPLETED)` — explicit task bookkeeping per the design review
  edit #3, NOT `asyncio.gather`. `wait` cleanly handles partial
  completion + ordered reconstruction + cancellation/drain.
- Pending tasks past the global cap get `.cancel()`'d and drained
  within a configurable bound (default 1.0 s).

Outputs are reconstructed in **registration order** regardless of
completion order — integration's filter phase relies on stable
ordering.

## Synthetic outputs (the design review edit #4)

Failure paths yield synthetic CohortOutputs so the result-list shape
is invariant: every registered cohort produces exactly one
CohortOutput.

| Path | `outcome` | `output` | `error_summary` |
|---|---|---|---|
| Cohort returned successfully | `SUCCESS` | cohort-supplied | `""` |
| Per-cohort timeout (`wait_for` fired) | `TIMEOUT_PER_COHORT` | `{}` | "exceeded {N}ms" |
| Global wall-clock timeout | `TIMEOUT_GLOBAL` | `{}` | "fan-out global cap exceeded" |
| Cohort raised | `ERROR` | `{}` | sanitized exception |
| Cancelled (reserved) | `CANCELLED` | `{}` | reason |

Synthetic outputs use `output: {}` (empty dict). The `outcome` field
is the canonical signal; integration filters on `outcome != SUCCESS`,
not on output content. This avoids namespace collision with cohorts
that legitimately use a `status` key inside `output`.

## cohort_run_id minted by runner

`{turn_id}:{cohort_id}:{sequence}` (Section 5a). Cohorts cannot mint
their own IDs — the runner overrides any value the cohort sets. The
deterministic shape lets audit references resolve correctly across
the turn's audit trail. `sequence` is reserved for future stateful
or multi-fire cohorts; effectively always 0 in v1.

## Redaction policy

`error_summary` and any text bound for synthetic outputs go through
`kernos.kernel.cohorts.redaction.sanitize`:

1. Strip stack-trace tail (`Traceback (most recent call last):`,
   `File "..."`, JS-style frames). No traceback content survives.
2. Strip header-shaped patterns (Authorization, Bearer, X-API-Key).
3. Strip credential directory paths (`.config/kernos/credentials`,
   `.aws/credentials`, `.ssh/`, etc.).
4. Strip token shapes (sk-/pk-, xoxb-, gh*_, ya29., JWT, generic
   32+ alphanumeric runs).
5. Truncate to 500 chars (configurable).

Conservative — false positives over false negatives. Operator-only
audit logs may carry full stack traces elsewhere; this function never
produces them in CohortOutput surface text.

## Required-cohort failure policy (Section 8)

V1's `BudgetState` gains three flags this spec uses:

- `required_cohort_failed` — any required cohort's outcome != SUCCESS
- `required_safety_cohort_failed` — required + safety_class subset
- `cohort_fan_out_global_timeout` — runner hit its wall-clock cap

Integration's filter phase reads them and applies downstream policy:

| Failure mode | Integration's response |
|---|---|
| Required (non-safety) cohort failed | Constrained briefing; `presence_directive` notes missing context |
| Required + `safety_class` cohort failed | Decided action defaults to `constrained_response` or `defer` |
| Non-required cohort failed | Fan-out continues; missing cohort filtered from `relevant_context` |

The runner produces the **signal**; integration applies the **policy**.

## Audit log

One entry per fan-out under audit_category `cohort.fan_out`
(Section 10). Member-scoped (turn-keyed). Includes:

- `registered_cohort_ids` (list)
- `outcomes`: per-cohort `cohort_id`, `cohort_run_id`, `outcome`,
  `duration_ms`, `error_summary`
- `required_cohort_failures` / `required_safety_cohort_failures`
- `global_timeout_engaged`
- `fan_out_started_at` / `fan_out_completed_at`

The audit emit is best-effort; failure to emit never fails the
fan-out, in line with kernel convention.

## Architectural placement

```
kernos/kernel/cohorts/
├── __init__.py                 # public API
├── descriptor.py               # CohortDescriptor, CohortContext, …
├── registry.py                 # CohortRegistry; sync rejection
├── runner.py                   # CohortFanOutRunner (asyncio.wait)
├── redaction.py                # error_summary sanitizer
└── synthetic_test_cohort.py    # parameterised fixture
```

V1 schema extensions live in `kernos/kernel/integration/briefing.py`
(Outcome enum, CohortOutput.outcome/error_summary, BudgetState
fan-out flags) — backwards-compatible additions.

## Path forward

Subsequent specs target this contract:

1. **COHORT-ADAPT-GARDENER** — wrap `GardenerDecision` as a
   CohortOutput
2. **COHORT-ADAPT-MEMORY** — decouple memory retrieval from being a
   model-decided tool call
3. **COHORT-ADAPT-PATTERNS** — surface pattern heuristics as a
   standalone cohort
4. **COHORT-ADAPT-COVENANT** — split covenant validation's
   judgment-vs-apply concerns; emit findings with `Restricted`
   visibility
5. **PRESENCE-DECOUPLING** — separate generation from the reasoning
   tool loop
6. **INTEGRATION-WIRE-LIVE** — wire fan-out → integration → presence
   into the production turn pipeline

Each adapter spec registers its cohort against this runner and
publishes a CohortOutput shape. The runner stays untouched.
