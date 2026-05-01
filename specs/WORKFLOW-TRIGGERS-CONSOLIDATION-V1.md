# WORKFLOW-TRIGGERS-CONSOLIDATION v1

**Status:** Draft for Kit review-before-CC. Framing-pass approved at
`352ffafef4db817db26fe8e6471ccb5c` (Kit verdict: APPROVE DIRECTION;
all 7 must-fix edits + 1 missed-seam folded). v1 spec drafted by CC
(architect-on-rotation), deliberated with Codex on architectural
decisions D1–D7, posted here for Kit review.

**Substrate position:** rides on shipped CRB main v1, Drafter v2, STS,
WDP, WLP, MODEL-AND-STATUS-V1. Closes the precursor-arc gap between
"workflows are recognized + drafted + approved + registered" and
"workflows actually fire on their declared triggers."

## Why

Three trigger-shaped subsystems plus dead-by-accident code accumulated
across separate ships. CRB shipped without consolidating them. The
runtime that evaluates `descriptor.triggers` field on registered
workflows and fires the workflow's action sequence is missing —
shipping CRB without it half-meets the user-facing claim.

The architect's framing pinned **Option B with A as convergence
trajectory**: existing user-facing surfaces (`manage_schedule`,
calendar-event-trigger preferences) preserved; underneath, all three
shapes evaluate through one runtime. That runtime ships in this spec.

The framing also pinned **Founder's generalization** — `before/after Y`
is a relationship operator that should keep `Y` neutral. v1 ships
calendar / scheduler / internal Kernos events as first-class sources,
with email + Notion source contracts defined for v1.x polling
implementations. The predicate language is source-neutral by
construction.

## Scope

### Ships in v1

* **`TriggerEvaluationRuntime`** (`kernos/kernel/triggers/runtime.py`).
  One runtime that owns the heartbeat tick + event-stream subscription.
  Walks registered triggers, evaluates predicates, claims fire intents
  in a durable outbox, dispatches to WLP execution.
* **Three-part `TriggerPredicate`** (`kernos/kernel/triggers/predicate.py`).
  Typed wrapper around the existing `predicates.py` AST. Three axes:
  `event_selector` (existing AST), `temporal_relation` (one of `before`
  / `after` / `on` / `every`), `dispatch_policy` (dedup, missed-fire,
  retry).
* **Durable fire-intent outbox** — extension of the shipped
  `trigger_fires` table with deterministic `fire_window_key`, `status`
  (`pending` / `dispatched` / `completed` / `failed`),
  `dispatched_at`, `completed_at`, `last_error`. Restart-resume via
  recovery sweep on engine startup.
* **Adapters for shipped subsystems** (real, not parallel):
  * `kernos/kernel/scheduler.py` becomes input adapter only — cron
    polling + calendar polling produce typed events / due-time
    notifications; no independent firing or idempotency decisions.
  * CRB Compiler extension translates `descriptor.triggers` field
    into `TriggerPredicate` registrations during STS-bound
    registration.
* **Pattern 05 strike** — `kernos/kernel/pattern_heuristics.py` time-
  shape heuristics removed; reusable test scenarios ported to
  triggers test suite.
* **Email + Notion source contracts** — typed `EventSource` protocol
  + contract specs for `email.message_observed` and
  `notion.page_observed`. Real polling implementations deferred.
* **Missed-window semantics** — default skip-stale; opt-in `catch_up`
  fires at most once on restart per missed window.

### Defers to v1.x

* **Temporal composite predicates** (AND/OR/NOT *at the temporal-
  relation level*). *In scope:* event-selector composites stay
  available — the existing AST supports AND/OR/NOT over selector
  leaves. *Deferred:* composing multiple `TemporalRelation`s within
  one predicate (e.g., `every(cron) AND on(event)`). Multiple
  registered triggers per workflow are sufficient for v1.
* Email + Notion polling implementations.
* Stateful predicates ("only fire if last fire > N ago").
* Pattern-over-stream predicates ("after 3 occurrences within 1
  hour").
* Hard migration of `manage_schedule` + calendar-event-trigger user-
  facing surfaces to a unified API.
* User-facing trigger inspection / management UI.

## Three-part predicate model

The framing's must-fix #1 (Kit pin) splits the predicate into three
independently-extensible axes. Source code shape:

```python
# kernos/kernel/triggers/predicate.py

from dataclasses import dataclass
from typing import Literal

# Event selector: the existing predicate AST from
# kernos.kernel.workflows.predicates is the v1 event-selector
# language unchanged. Composite (AND/OR/NOT) + leaf operators
# (eq/contains/exists/in_set/time_window/event_type_starts_with/
# actor_eq/correlation_eq).
EventSelector = dict  # PredicateAST


@dataclass(frozen=True)
class TemporalRelation:
    """One of four shapes. v1 enforces frozen; v1.x can extend."""

    kind: Literal["before", "after", "on", "every"]
    # `before(Y, minutes=N)` — fires N minutes before next Y match.
    # `after(Y, minutes=N)`  — fires N minutes after Y observed.
    # `on(Y)`                — fires immediately when Y observed.
    # `every(cron)`          — fires when cron expression matches now().
    minutes: int = 0
    cron_expression: str = ""


@dataclass(frozen=True)
class DispatchPolicy:
    """How a fire actually happens when conditions match.

    `dedup_window_seconds` — within this window, the same trigger +
    same Y-match key cannot fire twice. Default 300s.
    `missed_window`        — `skip` (default) | `catch_up`. Framing
    must-fix W10. catch_up fires at most once on restart per missed
    window.
    `retry_on_dispatch_failure` — bounded retry count for
    runtime→workflow handoff (NOT for execution failures inside
    workflows; those route through WLP).
    """

    dedup_window_seconds: int = 300
    missed_window: Literal["skip", "catch_up"] = "skip"
    retry_on_dispatch_failure: int = 3


@dataclass(frozen=True)
class TriggerPredicate:
    """The unit registered with TriggerEvaluationRuntime.

    Codex deliberation D2 pin: event-vs-time evaluation path is
    DERIVED from temporal_relation.kind, not a public axis. Both
    paths converge at the same durable claim_fire / dispatch
    runtime (D7).
    """

    event_selector: EventSelector
    temporal_relation: TemporalRelation
    dispatch_policy: DispatchPolicy = DispatchPolicy()
```

### Path derivation (Codex D2)

Evaluation path is derived from `temporal_relation.kind`:

* `on(...)` → event-driven path. Subscribes to event_stream
  post-flush; fires immediately when a matching event arrives.
* `before/after(Y, minutes=N)` → mixed. Y is detected via event-
  driven path (calendar.event_observed published into event_stream
  by the scheduler.py input adapter); the actual fire-time is
  computed from Y.timestamp ± N minutes; the heartbeat path drains
  due fire-windows.
* `every(cron)` → time-driven path. Heartbeat walks every-cron
  predicates each tick and fires those whose next due time has
  elapsed.

Both paths converge at the same `claim_fire(...)` step that writes
to the trigger_fires outbox. Below this point, dispatch is
identical regardless of how the fire was decided.

## Substrate additions

### Schema — extend existing `trigger_fires`

The shipped `trigger_fires` table (from
`kernos/kernel/workflows/trigger_registry.py`) is extended to act
as the dispatch outbox. Existing schema and primary key are
preserved (SQLite forbids altering primary keys via ALTER TABLE);
new columns are added via ALTER TABLE; v1 logic uses the existing
PK as the fire identity.

**Existing PK (preserved):** `PRIMARY KEY (trigger_id,
idempotency_key)`. v1 reads `idempotency_key` AS the
`fire_window_key` — same column, new semantic name in code.
Application-level `fire_id` (derived value, e.g.,
`f"{trigger_id}::{fire_window_key}"` or a SHA thereof) is the
caller-facing identity but the SQLite PK stays as the existing
composite for race-safe atomic claim.

**Migration on existing installs (ALTER TABLE; existing PK preserved;
status enforced at the application layer):**

```sql
ALTER TABLE trigger_fires ADD COLUMN instance_id TEXT;
ALTER TABLE trigger_fires ADD COLUMN status TEXT NOT NULL
    DEFAULT 'completed';  -- new fires use the v1 state machine;
                           -- legacy rows stay 'completed'
ALTER TABLE trigger_fires ADD COLUMN claimed_at TEXT;
ALTER TABLE trigger_fires ADD COLUMN claim_owner TEXT;
ALTER TABLE trigger_fires ADD COLUMN dispatched_at TEXT;
ALTER TABLE trigger_fires ADD COLUMN completed_at TEXT;
ALTER TABLE trigger_fires ADD COLUMN workflow_execution_id TEXT;
ALTER TABLE trigger_fires ADD COLUMN last_error TEXT;
ALTER TABLE trigger_fires ADD COLUMN catch_up INTEGER NOT NULL
    DEFAULT 0;  -- 1 when fire was a catch-up replay on restart

-- Backfill instance_id on existing rows from the triggers table
-- (one-time migration step):
UPDATE trigger_fires
   SET instance_id = (
       SELECT t.instance_id FROM triggers t
        WHERE t.trigger_id = trigger_fires.trigger_id
   )
 WHERE instance_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_trigger_fires_status_pending
    ON trigger_fires (status, claimed_at)
    WHERE status IN ('pending', 'dispatched');
CREATE INDEX IF NOT EXISTS idx_trigger_fires_instance_status
    ON trigger_fires (instance_id, status);
```

**Race-safe atomic claim** uses the existing PK
`(trigger_id, idempotency_key)`. INSERT new fire row → SQLite
returns IntegrityError on conflict → caller treats as
"already claimed." UNIQUE on the composite PK is what makes the
claim atomic; no separate UNIQUE INDEX needed.

**Fresh-install DDL (new instances + test fixtures):** the
`CREATE TABLE trigger_fires` statement is updated to carry the
new columns + a SQL `CHECK (status IN ('pending', 'dispatched',
'completed', 'failed'))`. SQLite forbids adding CHECK constraints
via ALTER TABLE, so existing installs rely on application-layer
enforcement (the `FireOutbox` mutator methods reject invalid
transitions before the SQL layer). This mirrors the CRB v1 pattern
(`PermittedTransitions` map at the store boundary).

**Column purposes:**

* `idempotency_key` — existing column; v1 uses it as
  `fire_window_key` (deterministic per Idempotency posture). PK
  with `trigger_id` enforces "one fire per (trigger, window)."
* `instance_id` — added for query convenience and per-instance
  recovery scoping. Backfilled from the `triggers` table on
  migration; required NOT NULL at the application layer for new
  rows (fresh-install DDL makes it NOT NULL).
* `status` — state machine. Default `'completed'` for legacy rows
  (pre-v1 fires were one-shot, already done when the row was written).
* `claimed_at` + `claim_owner` — who claimed this fire and when.
  Recovery sweep uses `claimed_at` age vs. `claim_lease` to detect
  orphaned claims; `claim_owner` is the runtime instance id (PID +
  start-time hash) so a recovered claim can be re-distinguished
  from a duplicate-claim race.
* `workflow_execution_id` — WLP's stable execution id returned by
  `execute_workflow`. Recovery uses this to query WLP for outcome
  on `dispatched` rows whose completion we missed.
* `dispatched_at` / `completed_at` / `last_error` — observability +
  recovery cadence.
* `catch_up` — 1 when this fire was the catch-up replay; 0 for
  normal fires. Carried into the dispatch payload for workflow
  visibility.
* Partial indexes on `status IN ('pending', 'dispatched')` keep the
  recovery sweep cheap.

### New module: `kernos/kernel/triggers/`

```
kernos/kernel/triggers/
├── __init__.py        — public surface
├── predicate.py       — TriggerPredicate + TemporalRelation +
│                         DispatchPolicy (the three-part model)
├── runtime.py         — TriggerEvaluationRuntime
├── outbox.py          — fire_intent state machine, recovery sweep
├── adapters/
│   ├── __init__.py
│   ├── crb_compiler.py    — descriptor.triggers → TriggerPredicate
│   ├── scheduler_adapter.py — wraps shipped scheduler.py
│   └── calendar_adapter.py  — wraps shipped calendar polling
└── errors.py
```

Existing `kernos/kernel/workflows/trigger_registry.py` is **NOT**
deleted. Its `TriggerRegistry` becomes the persistence + cache
layer the new runtime sits on. The new runtime extends behaviour;
the existing storage stays as-is to keep the migration surface
minimal.

## Public API

```python
# kernos/kernel/triggers/runtime.py

class TriggerEvaluationRuntime:
    """One runtime. Time-driven via heartbeat; event-driven via
    event_stream post-flush hook. Dispatch through durable outbox.

    Existing TriggerRegistry composes underneath as the persistence
    layer. Existing scheduler.py provides input-adapter behaviour
    (cron observation + calendar polling) feeding typed events
    into event_stream.
    """

    async def start(
        self, *, db_path: str, event_stream_hook,
        heartbeat_seconds: int = 30,
    ) -> None:
        """Boot: open DB, attach event-stream hook, start
        heartbeat loop, run recovery sweep."""

    async def stop(self) -> None: ...

    async def register(self, *, trigger_id: str, instance_id: str,
                        workflow_id: str, predicate: TriggerPredicate,
                        member_id: str = "") -> None:
        """Persist + activate. Atomic: fails if predicate
        validation fails; never partially registered."""

    async def deactivate(self, trigger_id: str) -> None: ...

    async def evaluate_now(self) -> None:
        """Heartbeat tick. Walks time-shape predicates; due
        fires call _claim_fire → _dispatch. Idempotent: safe to
        call twice in the same window."""

    async def recover(self) -> int:
        """Engine-startup sweep. Walks status='pending' and
        status='dispatched' rows in trigger_fires; resumes or
        completes. Returns count recovered. Idempotent."""


# kernos/kernel/triggers/outbox.py

class FireOutbox:
    """The durable fire-intent log. Codex D7 pin: triggers'
    source of truth for dispatch. Mirrors CRB approval→STS
    posture: claim before emit; dispatch is resumable; duplicate
    suppressed.

    `fire_id` is the application-level identity for a fire — a
    deterministic value derived from `(trigger_id, fire_window_key)`
    (e.g., `f"{trigger_id}::{fire_window_key}"`). The same inputs
    produce the same fire_id across processes, so recovery and
    restart can reference a fire by id without ambiguity. The
    SQLite-level identity is the existing composite PK
    `(trigger_id, idempotency_key)` where `idempotency_key` is
    treated as `fire_window_key` — the schema PK is preserved for
    SQLite-forbids-ALTER-PRIMARY-KEY reasons; race-safe atomic
    claim relies on the composite UNIQUE that the PK already
    provides.
    """

    async def claim_fire(
        self, *, instance_id: str, trigger_id: str,
        fire_window_key: str, payload: dict, claim_owner: str,
    ) -> "FireRecord | None":
        """Atomic claim. INSERT pending with claimed_at + claim_owner;
        conflict on the deterministic UNIQUE means another path
        already claimed → return None. Returns the FireRecord
        (including fire_id) iff this caller won the claim.

        Race-safety: the conditional UNIQUE makes the claim atomic
        at SQLite level. No application lock needed.
        """

    async def mark_dispatched(
        self, *, fire_id: str, claim_owner: str,
        workflow_execution_id: str,
    ) -> None:
        """Transition pending → dispatched. CAS-style: the UPDATE
        is conditional on `WHERE fire_id = ? AND status = 'pending'
        AND claim_owner = ?`. rowcount=0 raises StaleClaimError;
        another process / a recovery sweep took ownership and the
        caller must abandon dispatch."""

    async def mark_completed(
        self, *, fire_id: str, claim_owner: str, result: dict,
    ) -> None:
        """Transition dispatched → completed. CAS on (status =
        'dispatched' AND claim_owner = ?). Idempotent on second
        call with same fire_id from same owner."""

    async def mark_failed(
        self, *, fire_id: str, claim_owner: str, error: str,
    ) -> None:
        """Transition pending|dispatched → failed. CAS on claim_owner."""

    async def find_pending(self, *, claim_lease_seconds: int) -> list[FireRecord]:
        """Recovery sweep input. Returns rows with
        status='pending' AND claimed_at < now() - claim_lease."""

    async def find_dispatched_unfinished(
        self, *, dispatch_lease_seconds: int,
    ) -> list[FireRecord]:
        """Recovery sweep input. Returns rows with
        status='dispatched' AND dispatched_at < now() - dispatch_lease.
        These need WLP execution-status query before transition."""

    async def reclaim(
        self, *, fire_id: str, new_claim_owner: str,
    ) -> "FireRecord | None":
        """Recovery sweep transitions an orphaned claim to a new
        owner. Conditional UPDATE on (status IN ('pending',
        'dispatched') AND claimed_at < now() - lease). Returns the
        record iff reclaim succeeded; None if another sweep raced
        and reclaimed first."""
```

## Dispatch boundary (Codex D7)

The runtime does **NOT** invoke WLP execution synchronously. v1 ships
the durable-outbox pattern matching the CRB approval→STS posture:

1. **Claim.** `_claim_fire(instance_id, trigger_id, fire_window_key,
   payload)` writes `status='pending'` to trigger_fires. The
   existing PK `(trigger_id, idempotency_key)` — where
   `idempotency_key` carries the v1 `fire_window_key` — provides
   the composite UNIQUE that makes the claim race-safe. Concurrent
   claimants get IntegrityError; one wins. `instance_id` is added
   to the row for query convenience and recovery scoping; it is
   not part of the uniqueness gate.
2. **Dispatch.** Claim winner advances to `_dispatch(fire_id)` which
   invokes WLP `execute_workflow(workflow_id, payload)`. On dispatch
   call success, status → `'dispatched'`. On dispatch call failure
   (RuntimeError on the call itself, not workflow-internal failure),
   status → `'failed'` with `last_error`; bounded retry per
   `dispatch_policy.retry_on_dispatch_failure`.
3. **Complete.** WLP execution finishes. Runtime marks status →
   `'completed'`. WLP-internal execution failures are workflow-
   level concerns handled by existing WLP/CRB/STS contracts; the
   runtime records dispatch outcome only.
4. **Recover.** Engine startup sweep:
   * `status='pending'` rows whose `claimed_at < now() - claim_lease`:
     re-dispatch (claim was orphaned by crash).
   * `status='dispatched'` rows with no completion record after
     dispatch lease window: query WLP execution status; if WLP
     completed, mark `completed`; otherwise re-dispatch.
   * `catch_up=True` predicates whose missed-window timestamp is
     within their catch_up_seconds: claim ONE catch-up fire with
     `catch_up=1` flag.

## Composition with shipped substrate

* **CRB:** `kernos/kernel/crb/compiler/translation.py` extends to
  call `triggers.adapters.crb_compiler.compile_predicates(...)` and
  produce `TriggerPredicate` objects alongside the descriptor
  candidate. STS register_workflow receives both; registration is
  atomic in one transaction (Codex D5 pin).
* **WDP:** unaffected.
* **STS:** `register_workflow` extends to also call
  `runtime.register(...)` for each predicate. Registration is one
  unit; partial state is impossible.
* **WLP:** runtime invokes WLP `execute_workflow` for dispatch.
  Existing WLP contract preserved.
* **Drafter v2:** unaffected.
* **scheduler.py:** transforms to input adapter only (Codex D4 pin).
  Cron polling continues as-is but emits `scheduler.tick_due` events
  for cron predicates. Calendar polling continues but emits
  `calendar.event_observed` events for `before/after(calendar.event,
  ...)` predicates. ALL fire decisions and idempotency live in the
  runtime; scheduler.py owns no fire logic.
* **Pattern 05 (`pattern_heuristics.py`):** strike. Scenarios
  preserved as test fixtures in the v1 test suite.
* **Event stream:** runtime subscribes; doesn't introduce a parallel
  event taxonomy.
* **action_log idempotency (Drafter v2):** runtime composes the
  same claim-first protocol. The trigger_fires outbox IS the
  triggers' equivalent of cohort_action_log — different table
  because the key model differs (Codex D3 pin).
* **Receipt pattern:** runtime emits `workflow.fired` event on
  successful dispatch; `workflow.dispatch_failed` on dispatch
  failure. Friction observer (future) consumes.

## Idempotency posture

Mirrors CRB approval→STS pattern (framing must-fix #6):

### Deterministic idempotency key (`fire_window_key`)

* **`every(cron)`** — `f"every::{cron_expression}::{normalized_fire_time_iso}"`
  where `normalized_fire_time_iso` is the cron's intended fire time
  (NOT now()) bucketed to the cron's resolution.
* **`before(Y, N)`** — `f"before::{Y_event_id}::{N}"`. Y is identified
  by its substrate event_id which is durable across restart.
* **`after(Y, N)`** — `f"after::{Y_event_id}::{N}"`.
* **`on(Y)`** — `f"on::{Y_event_id}"`.

Same trigger + same Y → same key → UNIQUE constraint catches
duplicates. Replay produces identical key.

### WLP idempotent dispatch (Kit must-fix, post-fold)

The dispatch boundary has two distinct idempotency layers and they
must compose cleanly across the crash window between
`WLP.execute_workflow` accepting the request and the trigger-runtime
`mark_dispatched` persisting `workflow_execution_id`:

1. **Runtime side (trigger_fires):** the `fire_id` (= the
   deterministic `fire_window_key` above) is the row's primary
   identity. Replay produces identical key, UNIQUE catches
   duplicates at claim time.
2. **WLP side (workflow_executions):** `WLP.execute_workflow`
   accepts `fire_id` as a stable idempotency key. WLP stores
   incoming requests by `fire_id` and returns the original
   `workflow_execution_id` on a duplicate request — never creates
   a second execution row for the same `fire_id`.

Because `fire_id` is derived from `(trigger_id, fire_window_key)`,
the same logical fire produces the same `fire_id` across crashes
and recovery passes. This mirrors the CRB approval-to-STS pattern
the spec already cites: explicit idempotency key is what makes
that pattern survive crash windows.

**Recovery sweep contract.** Before re-dispatching any
`status='pending'` row past its `claim_lease`, the sweep queries
WLP by `fire_id`:

* WLP reports an existing execution → outbox row reconciles
  directly to `dispatched` (or `completed`) with that
  `workflow_execution_id`; no second WLP invocation.
* WLP has no record → re-dispatch with the same `fire_id`. The
  re-dispatch is itself idempotent at WLP, so a network-retry
  storm cannot produce duplicate executions.

This closes the seam Kit identified: a crash between WLP accept
and `mark_dispatched` no longer looks like a re-dispatch
opportunity to the recovery sweep.

### Restart resume

`runtime.recover()` runs on engine startup:

* `status='pending'` AND `claimed_at` older than `claim_lease`
  (default 60s): query WLP by `fire_id` first; reconcile to
  `dispatched`/`completed` if WLP has the execution, otherwise
  re-dispatch. (Covers crash before dispatch AND crash after
  WLP accept before `mark_dispatched`.)
* `status='dispatched'` AND `dispatched_at` older than `dispatch_lease`
  (default 600s): query WLP for execution outcome; transition to
  `completed` if WLP done, else re-dispatch. (Crash after dispatch
  before mark-complete.)
* `status IN ('completed', 'failed')`: terminal; no action.

### Duplicate suppression

* Two paths attempting same trigger + same window → claim-time
  IntegrityError → second path no-ops cleanly.
* Same predicate observes Y twice (e.g., calendar API returns the
  same event twice in overlapping polls) → fire_window_key is a
  function of `Y_event_id` which is durable → second observation
  hits UNIQUE → no-op.
* WLP receives the same `fire_id` twice (e.g., recovery sweep
  re-dispatches before WLP's first response lands) → WLP returns
  the original `workflow_execution_id` from the first call; no
  second execution row.

### Four required crash-recovery test scenarios

1. **Crash before dispatch.** Runtime claims fire (`pending`); crash.
   Restart: sweep finds pending; queries WLP by `fire_id` (no
   record); re-dispatches.
2. **Crash after WLP accept before mark-dispatched.** Runtime
   claims fire (`pending`); calls `WLP.execute_workflow(fire_id)`;
   WLP creates the execution row and returns; runtime crashes
   before persisting `workflow_execution_id` / `status='dispatched'`
   on `trigger_fires`. Restart: sweep finds pending past lease;
   queries WLP by `fire_id`; WLP returns the existing
   `workflow_execution_id`; outbox row reconciles to `dispatched`
   without a second `execute_workflow` call. **Exactly one
   workflow execution.**
3. **Crash after dispatch, before mark-complete.** Runtime claims +
   dispatches (`dispatched`); WLP runs to completion; crash before
   `mark_completed`. Restart: sweep finds dispatched; queries WLP
   for execution result; marks completed WITHOUT re-firing.
4. **Duplicate event observation.** Two `calendar.event_observed`
   events with same `Y_event_id` flush in overlapping batches.
   Idempotency key catches the second; only one fire intent
   created; only one dispatch.

Each scenario must produce **exactly one workflow execution**.

## Missed-window semantics (must-fix W10)

`DispatchPolicy.missed_window` is `'skip'` (default) or `'catch_up'`.

* `'skip'`: missed fires emit a diagnostic `workflow.missed_fire`
  event for observability and do NOT execute the workflow.
* `'catch_up'`: at engine startup, runtime walks catch-up-eligible
  triggers and computes the missed window since last fire. If a
  missed window exists within `catch_up_seconds` (default 24h for
  daily; pinned per predicate at registration), claim ONE fire
  with `catch_up=1` flag in payload AND in the trigger_fires row.
  Multiple missed windows in the same predicate fire **at most
  once** (the most recent missed window wins). 30 days of downtime
  ≠ 30 catch-up fires.

## Acceptance criteria

1. **AC1** — `TriggerEvaluationRuntime.register(...)` accepts a
   valid `TriggerPredicate` and persists + activates it; rejects
   invalid predicates with typed errors; partial state impossible.
2. **AC2** — `every(cron)` predicates fire on the heartbeat at the
   correct cron-resolution time; idempotency key derived from cron
   expression + normalized fire time prevents double-fire within a
   window.
3. **AC3** — `on(event_type, filter)` predicates fire when matching
   events flush in event_stream's post-flush hook; idempotency key
   is the matching event's `event_id`.
4. **AC4** — `before/after(calendar.event, N)` predicates derive
   fire-time from the matched calendar event's timestamp ± N
   minutes; calendar polling emits `calendar.event_observed`
   events; runtime computes the fire window and claims at the
   correct time.
5. **AC5** — Dispatch outbox: every fire goes through claim →
   dispatch → complete. Concurrent claims race-safe via the
   existing PK `(trigger_id, idempotency_key)` where
   `idempotency_key` carries v1's `fire_window_key`. State transitions
   are CAS: `mark_dispatched` updates `WHERE fire_id = ? AND
   status = 'pending' AND claim_owner = ?`; rowcount=0 raises
   `StaleClaimError`. `mark_completed` likewise CAS on
   `(status='dispatched' AND claim_owner=?)`. Recovery's
   `reclaim()` is the only path that may take ownership from a
   prior owner, conditional on lease expiry. **Test pin:** two
   concurrent claim attempts on the same `(instance, trigger,
   window)` produce exactly one winner; loser's claim returns
   None; only the winner can `mark_dispatched` (loser's attempt
   raises StaleClaimError). Recovery from an expired pending
   claim transitions ownership atomically.
6. **AC6** — Four crash-recovery scenarios verified by integration
   tests, each producing exactly one workflow execution:
   (1) crash before dispatch;
   (2) crash after WLP accept before `mark_dispatched` — runtime
       claims fire, calls `WLP.execute_workflow(fire_id)`, WLP
       creates execution row and returns, runtime crashes before
       persisting `workflow_execution_id`. Restart sweep queries
       WLP by `fire_id`, gets the existing execution_id, reconciles
       outbox row to `dispatched` without a second WLP call.
       **Test pin:** assert exactly one row in WLP's
       `workflow_executions` table for that `fire_id`, even though
       recovery would otherwise have re-dispatched the
       still-`pending` outbox row;
   (3) crash after dispatch before mark-complete;
   (4) duplicate event observation.
7. **AC7** — Missed-window default `skip` produces no execution +
   `workflow.missed_fire` diagnostic event. `catch_up` fires
   exactly once **per actual missed window** (keyed by the missed
   window itself, not by the restart event) regardless of
   downtime length. 30 days of downtime ≠ 30 catch-up fires; the
   most recent missed window wins.
8. **AC8** — Existing `manage_schedule` tool surface unchanged; user
   reminders continue firing; behavior identical from user
   perspective. **Test pin:** when a `manage_schedule` cron fires,
   the test asserts (a) exactly one row in `trigger_fires` reaches
   `status='completed'` for the fire window, (b) exactly one WLP
   execution exists with `execution_id` matching that row's
   `workflow_execution_id` column, (c) no row in
   `manage_schedule`'s legacy `triggers` table records a fire for
   the same window. The substrate-link assertion (a)+(b) is the
   load-bearing test; trace inspection is supporting.
9. **AC9** — Existing calendar-event-trigger preferences unchanged
   from user perspective; "remind me 15 minutes before next
   meeting" still works. **Test pin:** when calendar polling emits
   `calendar.event_observed` for a matching event, the test
   asserts exactly one `trigger_fires` row + one linked WLP
   execution per the AC8 shape. Verifies no second fire from
   residual scheduler.py logic by counting outbox rows for the
   `fire_window_key`.
10. **AC10** — CRB-registered workflows with `descriptor.triggers`
    fire on registered triggers. Registration is atomic with
    `register_workflow`; STS gate stays as-is.
11. **AC11** — `kernos/kernel/pattern_heuristics.py` removed from
    the repo. No remaining import paths reference it. Reusable
    test scenarios ported to triggers test suite. Verified by
    structural test + grep.
12. **AC12** — `EventSource` protocol defined; calendar adapter +
    scheduler adapter + internal-event adapter implement it.
    Email + Notion contract specs documented (no polling impl).
13. **AC13** — Adapter isolation invariant: handler does not import
    triggers/runtime adapters; adapters do not import handler
    internals. Pinned by structural test.
14. **AC14** — No regression on shipped substrate: full repo test
    suite remains green; Drafter v2, CRB, STS, WDP, MODEL-AND-
    STATUS-V1 tests untouched.

## Tests

### Unit

* Predicate three-part model: round-trip, validation, frozen
  dataclass invariants.
* `fire_window_key` derivation: deterministic across processes for
  every / on / before / after; same inputs → same key.
* Outbox state machine: claim race-safety (concurrent claimants),
  legal vs. illegal transitions, terminal-state idempotence.
* Path derivation from `temporal_relation.kind`: time-shape vs.
  event-shape routing.

### Integration

* End-to-end happy path per predicate kind: register → predicate
  matches → claim → dispatch → workflow executes → completed.
* Four crash-recovery scenarios (AC6).
* Missed-window skip + catch_up (AC7).
* CRB workflow registration produces working triggers (AC10).
* Adapter migration tests: manage_schedule + calendar reminders
  fire exactly once via unified path (AC8 + AC9 test pins).
* Pattern 05 strike: import path removed; ported test scenarios
  pass against new runtime (AC11).

### Live test sweep

* Boot Kernos with a small set of registered triggers (cron, on,
  before, after); idle for ≥2 heartbeat windows; verify exactly
  the expected fires, exactly once each, with no missed events
  in the journal.
* Restart mid-firing: kill -TERM during dispatch; restart;
  verify recovery sweep completes the in-flight fire without
  duplication.

## Error class hierarchy

```python
# kernos/kernel/triggers/errors.py

class TriggerError(Exception):
    """Base for triggers module."""

class PredicateValidationError(TriggerError):
    """TriggerPredicate failed shape/contract validation at register."""

class TemporalRelationError(PredicateValidationError):
    """TemporalRelation has invalid kind or missing required field."""

class DispatchPolicyError(PredicateValidationError):
    """DispatchPolicy has invalid field combination."""

class FireOutboxError(TriggerError):
    """Outbox-level error."""

class FireWindowConflict(FireOutboxError):
    """UNIQUE constraint hit — another path already claimed this
    fire window. Caller should treat as 'already fired,' no-op."""

class StaleFireRecovery(FireOutboxError):
    """Recovery sweep found a fire record older than the recovery
    threshold; manual triage required."""

class DispatchFailed(TriggerError):
    """Runtime → WLP dispatch failed beyond retry budget. NOT raised
    for workflow-internal execution failures; those are WLP's
    domain."""
```

## Commit strategy

5-7 commits. **C1-first constraint (must-fix #7) — runtime contract
+ persistence/idempotency skeleton BEFORE adapters.**

* **C1 — Runtime contract + persistence/idempotency skeleton.**
  New module `kernos/kernel/triggers/`. `TriggerPredicate` +
  `TemporalRelation` + `DispatchPolicy` types. `FireOutbox` with
  CAS-based claim/mark methods + recovery sweep skeleton + lease
  semantics. Schema migration on `trigger_fires` adding all v1
  columns: `instance_id`, `status`, `claimed_at`, `claim_owner`,
  `dispatched_at`, `completed_at`, `workflow_execution_id`,
  `last_error`, `catch_up`. Plus the `instance_id` backfill and
  the two partial recovery indexes (status+claimed_at and
  instance_id+status). Application-layer status validation
  (`PermittedTransitions`-style map) since SQLite forbids adding
  CHECK via ALTER. `runtime.py` interface shell —
  `start/stop/register/deactivate/evaluate_now/recover`. NO firing
  logic. NO adapters. Tests: predicate round-trip, fire_window_key
  determinism, outbox claim race-safety, CAS rejection of stale
  claims, schema migration round-trip, app-layer status enforcement.
* **C2 — Predicate evaluator + four temporal relations.**
  `evaluate_time_predicates` (cron + before/after due-time math)
  and `on_event` (subscribed to event_stream post-flush). Both
  paths converge at `_claim_fire → _dispatch`. Tests: per-relation
  evaluation correctness; three crash-recovery scenarios on
  outbox; race-safe claim under concurrent matches.
* **C3 — First-class event sources + internal event adapter.**
  `EventSource` protocol. `internal_event_adapter` for
  workflow.completion / user.message / page.edit (event_stream
  events). Calendar + scheduler input adapters wrap shipped
  scheduler.py — emit `calendar.event_observed` and
  `scheduler.tick_due` events instead of firing directly.
  Tests: end-to-end via internal events; calendar adapter emits
  observed events; scheduler adapter emits tick_due events.
* **C4 — External source contracts (no polling).**
  `email.message_observed` + `notion.page_observed` event-shape
  specs and EventSource stubs. Source contract tests verify the
  predicate language works against the shapes (mock event flush
  produces fires) without requiring real polling.
* **C5 — Migration adapters + scheduler.py refactor + CRB Compiler
  integration.** scheduler.py strips fire logic. All time-driven
  and event-driven decisions route through the unified runtime.
  Heartbeat consolidates: scheduler.py's existing heartbeat now
  drives `runtime.evaluate_now()`. Adapter test pin (must-fix #4):
  any manage_schedule cron / calendar event-trigger fires exactly
  once via the unified runtime, asserted via the AC8/AC9
  substrate-link tests (outbox row + WLP execution_id linkage).
  **CRB Compiler extension lands here** (Codex spec-review fold
  #5): `descriptor.triggers → TriggerPredicate` translation +
  registration atomic with STS `register_workflow`. This is a
  load-bearing seam (D5); promoting it out of C7 isolates the
  CRB↔runtime contract from C7's integration cleanup.
* **C6 — Missed-window semantics + catch_up.**
  `DispatchPolicy.missed_window` honored. `workflow.missed_fire`
  emitted for skip-default. `catch_up=True` recovery walks
  missed windows and claims at most one catch-up fire per
  predicate. Tests: simulated downtime, single catch-up fire,
  no fan-out for long downtime.
* **C7 — Pattern 05 strike + live test sweep + no-regression.**
  Integration-only commit. Pattern 05 source files removed;
  reusable test scenarios ported to the v1 test suite. Live
  sweep covers boot/idle/firing/restart-during-dispatch. Adapter
  isolation structural test (AC13). Full repo test suite green
  (AC14). NO new substrate or load-bearing logic in C7 —
  everything substantive lands in C1-C6.

Codex review confer:
* Mid-batch after C3 (event sources + the path-derivation pattern
  is locked).
* Final after C7.
* Confirmation pass on the fold (per CC contract).

## Kick-back triggers

Implementer escalates to architect when:

1. **WLP `execute_workflow` contract doesn't match dispatch shape.**
   v1 assumes WLP exposes `execute_workflow(workflow_id, payload) →
   awaitable`. If the actual shape requires significant change,
   surface before C2 lands.
2. **Existing `trigger_fires` schema can't be cleanly extended.**
   v1 assumes ALTER TABLE works on the shipped table. If column
   conflicts or data shape blocks the migration, surface before
   C1.
3. **Calendar polling produces events that don't carry stable
   event_ids across polls.** v1's `before/after(calendar.event)`
   idempotency depends on stable event_ids. If calendar API
   returns different identifiers per poll, the predicate's
   idempotency key model needs revision.
4. **Pattern 05 strike reveals dependent imports.** If
   `pattern_heuristics.py` is referenced by code other than
   gardener (where the framing says it's dead-by-accident), the
   strike requires architect call on each reference.
5. **CRB Compiler extension breaks an existing CRB v1 test pin.**
   If translating `descriptor.triggers → TriggerPredicate` requires
   modifying CRB v1 acceptance criteria, surface before C5 (the
   commit where the integration lands).
6. **WLP cannot return / query a stable execution_id.** v1's
   recovery sweep needs to query WLP for the outcome of a
   `dispatched` row whose completion was missed — that requires
   WLP's `execute_workflow` returning a stable id and a
   `query_execution_status(execution_id)` (or equivalent) read
   surface. If WLP doesn't expose this and exposing it requires
   changes to WLP's substrate, surface before C2.
7. **STS cannot atomically register triggers in the same
   transaction as `register_workflow`.** D5 pin: workflow + its
   triggers register or fail together. If STS's existing
   transaction shape can't accommodate the additional
   trigger-registration step, surface before C5 — the alternative
   (best-effort registration with a recovery sweep) is a different
   substrate posture and requires architect-level decision.
8. **Catch-up cannot derive one canonical "latest missed window."**
   v1 pins "claim ONE catch-up fire per predicate after restart,
   regardless of downtime length." This requires the predicate's
   missed-window function to be deterministic — given (last_fire_at,
   now, predicate config) → exactly one missed-window-key. If a
   real predicate produces ambiguous "latest missed window" (e.g.,
   `before(Y, N)` where Y has multiple unfired matches in the
   downtime window), surface before C6.

Implementer also surfaces for design ambiguity: e.g., a fifth
temporal relation appears necessary; missed-window semantics need
extending; an event source's contract is structurally
incompatible.

## Out of scope

* Multi-user trigger sharing.
* External trigger sources beyond email/Notion (Slack/GitHub/etc.).
* Stateful predicates beyond simple deduplication.
* Pattern-over-stream predicates.
* Hard migration of `manage_schedule` + calendar-event-trigger
  user-facing surfaces (v1 ships soft migration; hard is v1.x).
* OR/NOT *at the temporal-relation level* in composite predicates
  (event-selector OR/NOT/AND remains in scope via the existing AST).
* User-facing trigger inspection / management UI.
* Retry policy for WLP-internal execution failures (those route
  through existing WLP/CRB/STS contracts unchanged).

## References

* Framing page (Notion): `352ffafef4db817db26fe8e6471ccb5c`.
* Roadmap: `352ffafef4db816c898ed73a2d879666`.
* Codex audit smell #5: `351ffafef4db81cf863fdbfb880f7cf0`.
* CRB precursor closure: `352ffafef4db81498e08d76b17d9d895`.
* Substrate composed against:
  * `kernos/kernel/workflows/trigger_registry.py` (existing
    persistence + cache)
  * `kernos/kernel/workflows/predicates.py` (existing AST,
    becomes event_selector axis)
  * `kernos/kernel/scheduler.py` (becomes input adapter)
  * `kernos/kernel/cohorts/_substrate/action_log.py` (Drafter v2
    claim-first protocol — informs the trigger_fires outbox
    design but trigger_fires has its own table because the key
    model differs per Codex D3)
  * `kernos/kernel/crb/compiler/translation.py` (extended in C5
    to produce TriggerPredicate alongside descriptor candidate;
    integration is atomic with STS register_workflow per D5)
  * Shipped specs: `MODEL-AND-STATUS-V1.md`,
    `SPEC-WORKFLOW-LOOP-PRIMITIVE.md`.

## Codex deliberation outcomes (D1–D7)

Decisions confirmed during pre-spec deliberation:

* **D1 AGREE** — Three-part model is parallel typed wrapper around
  existing AST. AST stays pure as `event_selector`. Folded.
* **D2 REVISE** — Path derivation is internal, not a public axis.
  Path derived from `temporal_relation.kind`. Both paths converge
  at the same durable claim/dispatch step. Folded.
* **D3 AGREE** — Extend existing `trigger_fires` table; do not reuse
  cohort_action_log (key model differs). Folded.
* **D4 AGREE** — `scheduler.py` becomes input adapter only; no
  independent firing or idempotency. Folded.
* **D5 AGREE** — CRB Compiler / STS register_workflow extends to
  produce TriggerPredicate; registration atomic. Folded.
* **D6 AGREE** — Reuse existing scheduler heartbeat, refactored to
  drive runtime time-evaluation. Folded.
* **D7 NEW** — Dispatch boundary ownership: trigger_fires IS the
  outbox / source of truth; dispatch resumable; duplicate-suppressed
  after restart. Folded as the spec's "Dispatch boundary" section.
