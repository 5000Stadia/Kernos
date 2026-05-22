# IMPROVEMENT-ATTEMPT-LEDGER-V1

**Date:** 2026-05-22
**Status:** Draft for review (sub-spec #4 of `KERNOS-AUTONOMOUS-IMPROVEMENT-LOOP-V1`)
**Scope:** Three SQLite tables (improvement_attempts +
  improvement_attempt_commits + improvement_attempt_events) +
  helper module + `/improvement_status` slash command. Operator-
  observer surface for the autonomous loop. Independent of
  workspace + git ops; parallel-able.
**Estimated size:** ~250 LOC source + ~150 LOC tests.

## Why this spec exists

Per `KERNOS-AUTONOMOUS-IMPROVEMENT-LOOP-V1` D8: the operator
needs to follow along with autonomous improvement attempts. The
ledger is the single observer-visible truth — what's running,
what converged, what failed, what got committed.

Three-table shape (per parent spec):
- **`improvement_attempts`** — top-level per-attempt state.
- **`improvement_attempt_commits`** — per-cycle commit truth (one
  attempt can produce multiple commits if recovery cycles fire).
- **`improvement_attempt_events`** — append-only per-iteration
  narrative for `/improvement_status` to render.

## Design principles (load-bearing)

Per [[agent-facing-natural-simplicity]]: the ledger is
operator-observer-facing. The agent does NOT consume it. No
agent prose layer needed — operator slash command outputs
structured tabular text directly. This is the textbook "no
agent layer needed" case.

## Current state

- No improvement-attempt ledger exists.
- `instance.db` holds member, channel, posture, etc. tables.
  Adding three more is additive.
- Existing slash-command pattern (e.g., `/posture`, `/approve`)
  used as the structural model for `/improvement_status`.

## Design

### Schema

Three tables in `instance.db`:

```sql
CREATE TABLE IF NOT EXISTS improvement_attempts (
    attempt_id              TEXT PRIMARY KEY,
    instance_id             TEXT NOT NULL,
    started_at              TEXT NOT NULL,
    ended_at                TEXT,
    spec_requirement        TEXT NOT NULL,
    primary_coding_agent    TEXT,
    reviewer_coding_agent   TEXT,
    worktree_path           TEXT,
    spec_iterations         INTEGER NOT NULL DEFAULT 0,
    spec_iterations_outcome TEXT,
    impl_iterations         INTEGER NOT NULL DEFAULT 0,
    impl_iterations_outcome TEXT,
    final_commit_sha        TEXT,
    test_outcome            TEXT,
    first_pass_green        INTEGER,
    final_state             TEXT
);

CREATE INDEX IF NOT EXISTS idx_improvement_attempts_started
    ON improvement_attempts (started_at DESC);

CREATE TABLE IF NOT EXISTS improvement_attempt_commits (
    attempt_id                       TEXT NOT NULL,
    commit_sequence                  INTEGER NOT NULL,
    commit_sha                       TEXT NOT NULL,
    parent_sha                       TEXT NOT NULL,
    pushed_at                        TEXT NOT NULL,
    approval_id                      TEXT,
    test_outcome_after_this_commit   TEXT,
    recovery_trigger                 TEXT,
    PRIMARY KEY (attempt_id, commit_sequence)
);

CREATE TABLE IF NOT EXISTS improvement_attempt_events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL,
    sequence   INTEGER NOT NULL,
    timestamp  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_improvement_events_attempt
    ON improvement_attempt_events (attempt_id, sequence);
```

### Helpers module

`kernos/kernel/improvement_ledger.py`:

```python
async def create_attempt(
    db, instance_id, attempt_id, spec_requirement, started_at,
    primary_coding_agent="", reviewer_coding_agent="",
    worktree_path="",
) -> None: ...

async def update_attempt(
    db, attempt_id, *,
    ended_at=None, spec_iterations=None, spec_iterations_outcome=None,
    impl_iterations=None, impl_iterations_outcome=None,
    final_commit_sha=None, test_outcome=None,
    first_pass_green=None, final_state=None,
) -> None: ...

async def append_event(
    db, attempt_id, kind, detail="", timestamp=None,
) -> None: ...

async def record_commit(
    db, attempt_id, commit_sha, parent_sha, pushed_at,
    approval_id="", test_outcome_after_this_commit="",
    recovery_trigger="",
) -> None: ...

async def get_attempt(db, attempt_id) -> dict | None: ...

async def list_recent_attempts(
    db, instance_id, limit=10,
) -> list[dict]: ...

async def get_attempt_commits(db, attempt_id) -> list[dict]: ...

async def get_attempt_events(db, attempt_id) -> list[dict]: ...
```

`db` is the existing `instance_db._conn` (the bring-up flow
already wires this); helpers use direct SQL through that
connection.

### `/improvement_status` slash command

Owner-only (mirrors `/posture`, `/tools` auth pattern). Two
forms:

| Form | Output |
|---|---|
| `/improvement_status` | Most recent N=5 attempts: id, started_at, final_state, spec_requirement preview |
| `/improvement_status <attempt_id>` | Full detail: attempt row + commits + recent events |

Structured tabular text. Operator audience.

### Layered design check

- **Operator**: structured slash-command output. Pinned in tests
  to assert key fields present.
- **Agent**: nothing. Agent doesn't read the ledger; the
  orchestrator workflow (future) writes to it but doesn't
  surface entries to the agent's context.

## Acceptance criteria

| AC | Description |
|---|---|
| AC1 | Three tables (improvement_attempts + improvement_attempt_commits + improvement_attempt_events) created on schema bootstrap with the documented columns. |
| AC2 | `create_attempt` inserts a row with `started_at` + `final_state=null`. |
| AC3 | `update_attempt` mutates only the fields passed (other fields preserved). |
| AC4 | `append_event` increments `sequence` per `attempt_id` (starts at 1). |
| AC5 | `record_commit` increments `commit_sequence` per `attempt_id`; also updates `improvement_attempts.final_commit_sha` to the latest sha. |
| AC6 | `get_attempt(attempt_id)` returns the row as dict, or None if missing. |
| AC7 | `list_recent_attempts(instance_id, limit=N)` returns N most recent by `started_at DESC`. |
| AC8 | `get_attempt_commits(attempt_id)` returns commits in `commit_sequence` order. |
| AC9 | `get_attempt_events(attempt_id)` returns events in `sequence` order. |
| AC10 | `/improvement_status` (no args) lists recent attempts. |
| AC11 | `/improvement_status <id>` returns detail view with row + commits + events. |
| AC12 | `/improvement_status` from non-owner returns owner-only error. |
| AC13 | Schema-parser-semicolons gotcha avoided (no `;` inside SQL comments — see [[instance-db-schema-semicolons]]). |
| AC14 | No regressions on existing instance_db tests. |

## Soak gate

1. **Automated**: ACs pin schema + CRUD + slash output shape.
2. **Operator soak**: insert a synthetic attempt via test helper;
   run `/improvement_status` and verify the detail view renders.

## Out of scope

- Notion-bridge writer for long-form attempt narratives —
  mentioned in parent spec as "maybe"; defer to future spec if
  operator demand emerges.
- Cross-instance attempt aggregation — single-instance per row.
- Attempt deletion / pruning — append-only; manual SQL only
  for v1.

## Risks

- **Risk:** Sequence collisions on `improvement_attempt_events`
  if multiple writers race.
  - **Mitigation:** Helpers compute next sequence atomically
    inside a transaction (`SELECT MAX(sequence)+1 FROM ... WHERE
    attempt_id = ?`). v1 is single-writer (orchestrator
    workflow), so race is theoretical.

- **Risk:** `final_commit_sha` ambiguity when multiple
  recovery commits exist.
  - **Mitigation:** Per parent spec: it's a "convenience pointer
    to the latest." Per-cycle truth lives in
    `improvement_attempt_commits`. Documented in schema
    comment.

## Dependencies

- `instance_db.py` for schema bootstrap + connection.
- Existing slash-command auth pattern.

## Migration

- **Schema**: three new tables, additive. No migration of
  existing data.
- **Lazy migration**: instances pre-dating this spec see the
  tables on next bring-up. Helpers handle empty tables
  gracefully (return [] / None).
