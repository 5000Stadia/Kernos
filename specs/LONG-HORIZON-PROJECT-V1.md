# LONG-HORIZON-PROJECT-V1

Document revision: buildable v2. Scope: zero-user POC / portfolio demo.
Optimize for visible demo impact with minimal substrate work. Ignore security
and multi-member hardening in this revision.

## Goal

Give Kernos a small "long-horizon project" shape that makes it look and feel
like a project partner across weeks:

- The user hands Kernos a remodel, wedding, proposal, trip, or client project.
- Kernos creates a project space and a pinned project canvas.
- Decisions, timeline notes, open loops, and next steps have durable homes.
- The user can ask for status later and Kernos recalls week-1 decisions in
  week-3 context.
- Kernos creates a plain scheduled reminder so the existing scheduler can
  proactively nudge: "time to check in on {project}".

This is not a new substrate. A project is a binding over existing primitives:
`ContextSpace` + canvas pages + `KnowledgeEntry` decisions + one
`manage_schedule` reminder + a small `project_state` row.

## User Surface

Slash commands:

```text
/project start "<name>"
/project status [name-or-project-id]
/project list
/project complete [name-or-project-id]
```

Defer `/project pause` and `/project resume`.

Agent-callable kernel tools:

```python
start_project(name: str, initial_note: str = "", checkin_cadence: str = "weekly") -> dict
record_project_decision(project_id: str = "", decision: str, subject: str = "") -> dict
surface_project_status(project_id: str = "") -> dict
```

Tool results are Python dicts, not JSON strings. Slash commands can render those
dicts into terse user-facing text.

## Project State

Add `project_state` to the per-instance state database owned by
`SqliteStateStore`: `data/{instance_id}/kernos.db`. Do not add it to a global
`instance.db`.

Minimal schema:

```sql
CREATE TABLE IF NOT EXISTS project_state (
    project_id           TEXT PRIMARY KEY,
    instance_id          TEXT NOT NULL,
    owner_member_id      TEXT NOT NULL DEFAULT '',
    space_id             TEXT NOT NULL,
    canvas_id            TEXT NOT NULL,
    name                 TEXT NOT NULL,
    lifecycle_state      TEXT NOT NULL DEFAULT 'active',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    last_activity_at     TEXT NOT NULL,
    checkin_trigger_id   TEXT NOT NULL DEFAULT '',
    next_checkin_at      TEXT NOT NULL DEFAULT '',
    completed_at         TEXT NOT NULL DEFAULT '',
    completion_summary   TEXT NOT NULL DEFAULT '',
    data                 TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (space_id) REFERENCES context_spaces(id),
    CHECK (lifecycle_state IN ('active', 'completed'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_project_state_instance_space
    ON project_state(instance_id, space_id);

CREATE INDEX IF NOT EXISTS idx_project_state_owner_lifecycle
    ON project_state(instance_id, owner_member_id, lifecycle_state, updated_at);
```

`context_spaces` uses column `id`, not `space_id`. If SQLite foreign keys are
not enabled in the current connection path, enforce the space existence check
manually before insert.

No `ContextSpace.project_id`. Resolve the active project by querying
`project_state` for the current `active_space_id`.

## Start Flow

`start_project` performs four steps:

1. Create a space inline using the same pattern as
   `MessageHandler._handle_spaces`: instantiate `ContextSpace(id=..., ...)`
   and call `StateStore.save_context_space`.
2. Create the project canvas with
   `CanvasService.create(..., pinned_to_spaces=[space_id])`.
3. Seed pages using repeated `CanvasService.page_write(...)`.
4. Insert the `project_state` row and create a plain check-in reminder through
   the existing `manage_schedule` create path.

Do not call non-existent APIs such as `spaces.create_space`,
`canvas.create_canvas`, or `canvas.seed_pages`.

Seeded canvas pages:

```text
overview.md      # goal, constraints, current shape
decisions.md     # dated decisions and reversals
timeline.md      # dated project events and milestone notes
open-loops.md    # unresolved questions, blockers, waiting-on items
next-steps.md    # immediate actions
```

`CanvasService.create` already creates `index.md`; leave it as the landing page
or make it point at the five project pages.

The reminder is deliberately plain:

```text
Every week remind me: time to check in on {project_name}.
```

This uses `kernos.kernel.scheduler.handle_manage_schedule(action="create", ...)`
via the shipped extraction and notify path. Store `checkin_trigger_id` and
`next_checkin_at` when parseable from the result; if only the trigger id is
available, status can show the id and rely on `manage_schedule list` for detail.

## Decisions And Recall

`record_project_decision` records a decision in two places, sequentially:

1. Append a dated entry to `decisions.md`; optionally append a one-line event to
   `timeline.md`.
2. Add a `KnowledgeEntry` through `StateStore.add_knowledge`.

Use the real knowledge model:

```python
KnowledgeEntry(
    category="project_decision",
    subject=subject or project_name,
    content=decision,
    tags=[
        f"project:{project_id}",
        f"space:{space_id}",
        "project_decision",
    ],
    context_space=space_id,
    ...
)
```

No `kind` column. No `project_id` column. Link decisions to the project through
tags plus the project's space. Query by `category="project_decision"` and filter
tags in memory if needed.

AC6 is intentionally softened: there is no atomic canvas+knowledge transaction
in this codebase. If the canvas write succeeds and the knowledge write fails,
return a dict with `ok=False`, `partial=True`, and the failed step. Do not build
a new cross-store transaction layer for the POC.

## Status Surfacing

`surface_project_status` returns a dict assembled from:

- `project_state`
- current active space lookup when `project_id` is omitted
- recent `project_decision` knowledge entries filtered by project tag / space
- canvas page summaries from `decisions.md`, `timeline.md`, `open-loops.md`,
  and `next-steps.md`
- reminder fields from `project_state`

The status payload should be compact enough to render directly in chat:

```python
{
    "project_id": "...",
    "name": "Kitchen remodel",
    "lifecycle_state": "active",
    "space_id": "...",
    "canvas_id": "...",
    "recent_decisions": [...],
    "timeline": [...],
    "open_loops": [...],
    "next_steps": [...],
    "checkin_trigger_id": "...",
    "next_checkin_at": "...",
}
```

This is the primary "first-class recalled entity" demo: Kernos can answer
"where are we on the remodel?" with decisions, timeline, open loops, and next
steps without inventing a new memory subsystem.

## Completion

`/project complete` marks the row `completed`, records `completed_at` and an
optional summary, and best-effort removes the stored check-in trigger through
the existing `manage_schedule remove` path.

Do not archive the space. Do not make the canvas read-only. Those are follow-up
polish, not needed for the showcase.

## Kernel Tool Registration

Register the three new tools the real way:

- Add schemas to `kernos/kernel/kernel_tool_registry.py` through the explicit
  schema import path.
- Add tool names to `ReasoningService._KERNEL_TOOLS`,
  `_DISPATCHABLE_KERNEL_TOOLS`, and `_KERNEL_TOOL_PATHS`.
- Add concrete `ReasoningService.execute_tool` dispatch branches.
- Update `tests/test_kernel_tool_registry_parity.py` expectations by satisfying
  the existing parity tests, not by weakening them.

No v1 workflow calls these tools. If a future workflow calls them, it must also
add direct or compatible adapter coverage in
`kernos/setup/bring_up_substrate.py:_call_tool_adapter`.

## Dependencies

- `kernos/kernel/state_sqlite.py` — `SqliteStateStore`, per-instance
  `data/{instance_id}/kernos.db`, `context_spaces(id)`, knowledge persistence,
  and the new `project_state` CRUD.
- `kernos/kernel/spaces.py` — `ContextSpace` dataclass. No project fields added.
- `kernos/messages/handler.py` — slash command handling and the existing inline
  space creation pattern in `_handle_spaces`.
- `kernos/kernel/canvas.py` — `CanvasService.create` and
  `CanvasService.page_write`.
- `kernos/kernel/state.py` — `KnowledgeEntry` shape.
- `kernos/kernel/scheduler.py` — `MANAGE_SCHEDULE_TOOL`,
  `handle_manage_schedule`, `Trigger`, and the existing notify fire path.
- `kernos/kernel/kernel_tool_registry.py`,
  `kernos/kernel/reasoning.py`, and
  `tests/test_kernel_tool_registry_parity.py` — canonical kernel tool
  registration and dispatch parity.

## Acceptance Criteria

**AC1 — Durable per-instance project state.** `project_state` lives in
`data/{instance_id}/kernos.db` under `SqliteStateStore`. Insert, fetch by
`project_id`, fetch by `space_id`, list active projects, and mark completed all
work. The implementation references `context_spaces(id)` or manually rejects
orphan `space_id` values.

**AC2 — `/project start` creates the project shape.** Starting `"Kitchen
remodel"` creates a `ContextSpace`, a pinned canvas, the five seeded pages, a
`project_state` row, and a scheduler notify reminder. The response includes
`project_id`, `space_id`, `canvas_id`, and reminder information.

**AC3 — Canvas seeding uses real CanvasService APIs.** Tests or logs show
`CanvasService.create(..., pinned_to_spaces=[space_id])` followed by repeated
`page_write(...)` calls. No spec-only canvas helper is introduced.

**AC4 — Decisions are first-class recall records.** `record_project_decision`
returns a dict and writes a dated decision to the canvas plus a
`KnowledgeEntry(category="project_decision", tags=[...])` via `add_knowledge`.
The decision is linked by `project:{project_id}` and `space:{space_id}` tags.

**AC5 — Decision writes are sequential best effort.** Canvas and knowledge
writes are not claimed atomic. Partial failure is reported explicitly in the
returned dict and does not crash the turn.

**AC6 — Active project resolution uses project_state.** When `project_id` is
omitted, decision and status tools resolve the project by querying
`project_state.space_id == active_space_id`. No `ContextSpace.project_id` field
is added.

**AC7 — `/project status` and `surface_project_status` show the arc.** Status
includes lifecycle, recent decisions, timeline notes, open loops, next steps,
canvas id, and reminder id/time. This works after a process restart because the
source data is durable.

**AC8 — `/project list` and `/project complete` match the small scope.** List
shows active projects sorted by recent activity. Complete marks the row
completed, records the summary/time, and best-effort removes the reminder. No
pause/resume behavior ships.

**AC9 — Kernel tool registration passes existing parity tests.** The three
project tools appear in the canonical schema registry, ReasoningService tool
sets/path registry, and concrete dispatch. Existing kernel registry parity tests
remain strict and pass.

**AC10 — Portfolio demo succeeds.** Demo script: start a remodel/wedding
project, record two decisions, record one open loop and next step on the canvas,
ask status from the project space, restart the process, ask status again, and
observe the scheduled "time to check in on {project}" reminder delivered by the
existing scheduler notify path.

## Test Plan

- `tests/test_project_state_store.py` — table creation, CRUD, list active,
  fetch by space, complete, orphan-space rejection.
- `tests/test_project_start.py` — start flow composes space, canvas pages,
  state row, and scheduler reminder.
- `tests/test_project_decisions.py` — decision canvas write, knowledge entry,
  tag filtering, partial-failure reporting.
- `tests/test_project_commands.py` — `/project start`, `status`, `list`,
  `complete`.
- `tests/test_project_tool_registration.py` or existing parity coverage —
  confirms schemas and ReasoningService dispatch remain aligned.

## Deferred to LONG-HORIZON-PROJECT-V2

- Smart `project_checkin` workflow: urgency classification, silent paths, and
  auto-reschedule.
- Workflow-to-trigger bridge for `project.checkin_due`.
- Proactive 24h notification budgets.
- `/project pause` and `/project resume`.
- Canvas read-only mode or archive-on-complete.
- Shared projects, templates, exports, and cross-project dependencies.

Reason: the workflow trigger bridge (`schedule_trigger` /
`project.checkin_due`) does not exist in the shipped codebase, and building it
is disproportionate for the demo. V1 gets the proactive-reminder beat through
the existing scheduler notify path, which is already wired from
`manage_schedule create` through trigger firing to user notification.

Future workflow descriptors must use the real descriptor shape:
`instance_id`, `name`, `version`, `bounds`, `verifier`, `action_sequence`, and
unique step ids, with validation through `descriptor_parser.py` and
`workflow_registry.py`.
