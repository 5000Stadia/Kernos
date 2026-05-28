# LONG-HORIZON-PROJECT-V1

## Plain-English overview

The architect's 2026-05-27 directive: build what Kernos needs
to be "elegantly capable" for rich, user-focused multi-stage
work — wedding planning, business proposal drafts, multi-month
client projects. Substrate stability (watchdog, workflow
restart-resume, descriptor versioning) shipped in workstream A.
This spec is workstream B: the project primitive.

### What kind of work this enables

A user says "help me plan our wedding." Kernos:
- Creates a project space (existing primitive: spaces).
- Seeds a project canvas with the canonical sections (vision,
  budget, timeline, decisions, vendor list, guest list).
- Starts tracking decisions as they accumulate ("we'll do
  outdoor" → recorded under decisions).
- Schedules its own check-ins on Kernos's clock (existing
  primitive: manage_schedule + triggers — confirmed by the
  architect 2026-05-27 to already work for "remind me 10 min
  before X" patterns).
- Surfaces proactive updates when relevant ("the wedding is
  3 weeks out, you haven't sent invites").
- Weaves decisions made into future conversation (when the
  user later asks about food, Kernos remembers "outdoor"
  and suggests outdoor-friendly options).
- Handles long-horizon spans (months) without losing context
  across sessions or substrate restarts.

The same shape covers a business proposal draft, a multi-stage
client engagement, a research project, a course curriculum.
The substrate doesn't need to know "wedding" or "business";
it just needs to carry long-horizon multi-stage shape.

### Why this isn't a new substrate

Most of what's needed already exists in Kernos's primitives:

| Need | Existing primitive |
|------|--------------------|
| Per-project isolation | `ContextSpace` (member-owned) |
| Structured shared knowledge | Canvases + pages |
| Tactical step-by-step plan | `manage_plan` (per-turn) |
| Scheduled reminders | `manage_schedule` + triggers (WTC v1) |
| Multi-step orchestration | Workflows |
| Decision history | Knowledge entries + covenants |
| Proactive surfacing | Whispers, AwarenessEvaluator |

What's missing is a **shape** — a recognized convention that
binds these primitives into "a project" and the helper that
spins up that shape from a single user invocation. Plus a
small piece of new state: a `project_state` row per project,
tracking lifecycle stage and recent activity for the proactive-
surfacing trigger.

This spec ships the shape, not new substrate.

### What the user sees

- `/project start "<name>"` — spins up the project space,
  canvas, and initial check-in trigger
- `/project list` — shows active projects with last activity
- `/project status "<name>"` — current stage, recent decisions,
  upcoming reminders
- `/project complete "<name>"` — graceful close; project canvas
  preserved as reference
- Without slash commands: when conversation in a project space
  drifts toward a decision ("we'll do outdoor"), Kernos
  records it. When a deadline approaches, Kernos surfaces.
  When the user returns to the project after a week, Kernos
  surfaces a brief context refresh.

---

## Why this spec exists

Today's substrate handles substrate-self-improvement loops
(SELF-IMPROVEMENT-CLOSURE-V1, USER-INITIATED-IMPROVEMENT-TRIGGER-V1).
Those are loops where the user authorizes Kernos to fix
itself. The architecture for that landed cleanly.

The architecture for **user-facing long-horizon work** —
where Kernos is the user's project partner over months —
hasn't been articulated as its own spec. The pieces exist
but the binding shape doesn't. Without the binding shape,
each user would have to manually compose spaces + canvases +
schedules every time, and Kernos wouldn't have a coherent
way to track "what is this user's wedding planning project
in?" — let alone proactively surface updates from its own
clock.

This spec articulates the shape so:
1. The user has a one-command on-ramp (`/project start "..."`).
2. Kernos can recognize "we are in a project context" and
   behave accordingly (track decisions, proactively check in,
   weave decisions into responses).
3. Future capability work (project templates, shared projects
   between members, project export, etc.) has a foundation
   to build on.

---

## Audit findings (what exists, what's missing)

**Exists (load-bearing for this spec):**

- **Spaces**: `kernos/kernel/spaces.py` — per-member owned
  context spaces with conversation isolation. Project = space
  with a `project_kind=long_horizon` tag.

- **Canvases**: pages-of-structured-content. Project canvas
  = the user-readable + agent-editable shared state.

- **manage_plan** (per-turn): great for "right now, in this
  conversation, plan these 3 steps." NOT shaped for "over
  the next 6 months, plan these phases."

- **manage_schedule + triggers** (WTC v1): "fire this event
  at time T" or "fire when condition X holds." Confirmed by
  architect 2026-05-27 ("there used to be time triggers
  that could handle something like this"). The proactive-
  surfacing primitive is already here.

- **Knowledge entries + covenants**: per-member typed
  records. Decisions = knowledge entries with
  `entry_kind=project_decision`.

- **Whispers + AwarenessEvaluator**: ambient surfacing of
  context to the agent. Project proactive surfacing routes
  through this path.

**Missing (what this spec ships):**

- `project_state` SQLite table (per-instance): lifecycle
  state + last-activity timestamp + scheduled-checkin handle.

- `/project start`, `/project list`, `/project status`,
  `/project complete` slash commands.

- `start_project` kernel tool: composes space-creation +
  canvas-seeding + initial-trigger-scheduling in one call.

- `record_project_decision` kernel tool: adds a decision to
  the project canvas + indexes it as a knowledge entry
  tagged with `project_id`.

- `surface_project_update` kernel tool: posts a structured
  proactive message to the project space (used by the
  scheduled check-in trigger handler).

- Project canvas seed template: vision / phases / budget /
  decisions / open questions / next actions / completed.

- `project_checkin` workflow: fires on the scheduled check-
  in trigger. Reviews recent activity, surfaces an update or
  question if relevant, schedules the next check-in.

---

## Design principles (load-bearing)

1. **Composition, not new substrate.** A project = space +
   canvas + scheduled triggers + decision-knowledge-entries.
   The spec adds the BINDING (a `project_state` row tying
   them together) and the HELPERS (slash commands +
   composition tools), not new core substrate.

2. **User authority over project state.** Kernos can propose
   decisions, schedule check-ins, surface updates — but
   never marks a decision "final" or completes a project
   without user confirmation. Slash commands are owner-only
   for `start`, `complete`. `status` and `list` are member-
   readable.

3. **Cadence respects the user, not the substrate's clock.**
   Project check-in cadence defaults to weekly but the user
   can adjust per-project. Proactive surfacing is bounded
   (max one per 24h per project by default) so Kernos
   doesn't badger.

4. **Project state survives substrate restart.** All project
   state is durable (SQLite). The check-in trigger is
   durable (WTC v1 trigger registry). A bot restart does
   not lose project context or skip a scheduled check-in.

5. **Decisions weave into context.** When a project space is
   the active conversation context, the assembler injects
   recent project decisions into the prompt so Kernos's
   responses are coherent with what's been decided. Not all
   decisions — bounded to the most-recent N (default 10) so
   the prompt budget stays healthy.

6. **Templates are loose, not strict.** Project canvas
   sections (vision/phases/budget/etc.) are a default
   seed, not a schema. User can rename, delete, add. The
   substrate doesn't validate canvas shape after seed.

7. **Multi-member is a future extension, not v1.** v1 ships
   single-owner projects only. Shared projects (where
   multiple members can edit) compose with the existing
   relationship + sensitivity machinery and land as a
   v1.1 sub-spec.

---

## New primitives

### `project_state` table

```sql
CREATE TABLE project_state (
    instance_id          TEXT NOT NULL,
    project_id           TEXT NOT NULL,
    owner_member_id      TEXT NOT NULL,
    space_id             TEXT NOT NULL,     -- the project's ContextSpace
    canvas_id            TEXT NOT NULL,     -- the project's canvas
    name                 TEXT NOT NULL,     -- user-visible name
    project_kind         TEXT NOT NULL DEFAULT 'long_horizon',
    lifecycle_state      TEXT NOT NULL DEFAULT 'active',
                              -- 'active' | 'paused' | 'completed' | 'archived'
    created_at           TEXT NOT NULL,
    last_activity_at     TEXT NOT NULL,
    next_checkin_at      TEXT NOT NULL DEFAULT '',
    checkin_interval_sec INTEGER NOT NULL DEFAULT 604800,   -- weekly
    completed_at         TEXT NOT NULL DEFAULT '',
    completion_summary   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (instance_id, project_id),
    FOREIGN KEY (instance_id, space_id) REFERENCES context_spaces(instance_id, space_id),
    CHECK (lifecycle_state IN ('active', 'paused', 'completed', 'archived'))
);

CREATE INDEX idx_project_state_active
    ON project_state (instance_id, owner_member_id, lifecycle_state);
```

Lives in `instance.db` alongside the other per-instance state.

### `/project` slash commands

```
/project start "<name>"             # owner-only; creates the shape
/project list                       # member-readable; shows active projects
/project status "<name or id>"      # current stage + recent activity
/project complete "<name or id>"    # owner-only; marks complete + final summary
/project pause "<name or id>"       # owner-only; pauses check-ins
/project resume "<name or id>"      # owner-only; un-pauses
```

Handler additions to `kernos/messages/handler.py`. Owner check
mirrors `/fix` pattern (DURABLE-APPROVAL-RECEIPTS-V1 owner gate).

### `start_project` kernel tool

```python
async def start_project(
    *,
    instance_id: str,
    owner_member_id: str,
    name: str,
    initial_vision: str = "",
    checkin_interval_sec: int = 604800,  # 1 week default
) -> dict:
    """Compose-and-bind: creates a space, seeds the canvas,
    inserts the project_state row, schedules the first check-in.

    Returns:
        {project_id, space_id, canvas_id, first_checkin_at}
    """
```

Composes:
- `spaces.create_space` (existing)
- `canvas.create_canvas` + `canvas.seed_pages` (existing)
- `project_state` row insert (new)
- `manage_schedule.schedule_trigger` for the check-in (existing)

### `record_project_decision` kernel tool

```python
async def record_project_decision(
    *,
    instance_id: str,
    project_id: str,
    decision_text: str,
    decision_category: str = "general",  # e.g., venue/theme/budget
    confidence: str = "confirmed",        # 'tentative' | 'confirmed' | 'reversed'
) -> dict:
    """Record a decision on the project's canvas + index as a
    knowledge entry. Returns {decision_id, page_path}.

    Decisions are appended to the canvas's "Decisions" page
    AND tagged as KnowledgeEntry(kind='project_decision',
    project_id=X) so the assembler can surface them in
    future turns.
    """
```

### `surface_project_update` kernel tool

```python
async def surface_project_update(
    *,
    instance_id: str,
    project_id: str,
    update_kind: str,    # 'checkin' | 'deadline_approaching' | 'completion'
    body: str,
    suggested_actions: list[str] = [],
) -> dict:
    """Post a structured proactive message to the project's
    space. Used by the project_checkin workflow handler.

    Respects per-project surfacing budget: max one per 24h by
    default unless update_kind is 'deadline_approaching'.

    Returns: {surfaced_at, message_kind}.
    """
```

### `project_checkin` workflow

```yaml
workflow_id: project_checkin
trigger:
  event_type: project.checkin_due
  predicate:
    op: eq
    path: payload.instance_id
    value: '{installer.instance_id}'

action_sequence:
  - id: load_project_state
    action_type: call_tool
    parameters:
      tool_id: load_project_state
      args:
        project_id: '{idea_payload.project_id}'

  - id: classify_checkin_urgency
    action_type: call_tool
    parameters:
      tool_id: classify_project_checkin_urgency
      args:
        project_id: '{idea_payload.project_id}'
        last_activity_at: '{step.load_project_state.value.last_activity_at}'
        decisions_since_last_checkin: '{step.load_project_state.value.decisions_since_last_checkin}'

  - id: branch_on_urgency
    action_type: branch
    parameters:
      condition: '{step.classify_checkin_urgency.value.surface_update}'
      branch_on_true: surface_checkin_update
      branch_on_false: 'terminal:silent:reschedule_next_checkin'

  - id: surface_checkin_update
    action_type: call_tool
    parameters:
      tool_id: surface_project_update
      args:
        project_id: '{idea_payload.project_id}'
        update_kind: checkin
        body: '{step.classify_checkin_urgency.value.suggested_body}'

  - id: reschedule_next_checkin
    action_type: call_tool
    parameters:
      tool_id: schedule_next_project_checkin
      args:
        project_id: '{idea_payload.project_id}'

terminal_branches:
  silent:
    - id: reschedule_next_checkin
      # Same as above, silent path; no surfacing.
      action_type: call_tool
      parameters:
        tool_id: schedule_next_project_checkin
        args:
          project_id: '{idea_payload.project_id}'
```

The trigger fires from `manage_schedule` per the project's
`checkin_interval_sec`. The workflow handler decides whether
to surface (urgency-classified) or stay silent and reschedule.

### Canvas seed template

When `start_project` fires, the project canvas seeds these
pages (each editable by the user/agent after creation):

```
00-vision.md           — what's the goal? 1-2 paragraphs
01-phases.md           — major phases / milestones
02-decisions.md        — decisions made + when + by whom
03-open-questions.md   — unresolved questions, pending decisions
04-next-actions.md     — concrete next-steps the user owns
05-completed.md        — what's done + when
99-references.md       — external links, contacts, notes
```

Names are conventional but not enforced. The agent's
project-checkin workflow looks for these by path prefix when
preparing surfacings; if pages are renamed, the agent's
surfacing degrades gracefully (no checkin update body) rather
than crashing.

### Decision weaving (assembler integration)

When a turn's conversation is in a project space (resolved
via `space.project_id != ""`), the assembler injects the
project's most-recent N decisions (default 10) into the
prompt's contextual block, right before the user's current
message. So Kernos's responses are coherent with what's
been decided.

Implementation: extend `kernos/messages/phases/assemble.py`
to read `project_state` for the active space + load decisions
from the canvas's `02-decisions.md` page + format as a
"Recent project decisions:" block.

Bounded: N=10 decisions, max 50 chars each (longer get
truncated with ellipsis), total budget ~1500 chars. Below
tool-window budget; doesn't displace tool schemas.

---

## Modified workflow + new workflow

This spec ships ONE new workflow (`project_checkin.workflow.yaml`)
and does NOT modify any existing workflows.

The new workflow's YAML lives at
`specs/workflows/project_checkin.workflow.yaml` — registered
at bring-up via a new `register_project_checkin_workflow`
helper that mirrors `register_self_improvement_workflow`.

---

## Acceptance criteria

**AC1 — `project_state` table CRUD.** Insert with
`(instance_id, project_id, owner, space, canvas, name)`
succeeds. PK violation on duplicate
`(instance_id, project_id)`. Index supports the
"list active projects for owner" query in <10ms on 1000-row
fixture.

**AC2 — `/project start` creates the shape.** Sending
`/project start "Wedding planning"` (owner-only) creates:
- A new space with `project_kind="long_horizon"`
- A canvas with the seeded pages (`00-vision.md` etc.)
- A `project_state` row in `active` lifecycle
- A scheduled check-in trigger 1 week out

Returns the project_id to the user via ack.

**AC3 — Non-owner cannot `/project start`.** Member without
`role="owner"` sees friendly refusal; no state changes.

**AC4 — `/project list` returns active projects.** Shows
project name + last activity + next check-in. Sorted by
last activity descending. Members see their own projects;
the owner sees all.

**AC5 — `/project status` shows current state.** Returns
canvas summary, recent decisions (last 5), open questions,
next actions, scheduled check-in time.

**AC6 — `record_project_decision` updates canvas + index.**
Tool call writes the decision to `02-decisions.md` with
timestamp + author + category. ALSO creates a
KnowledgeEntry tagged with `project_id`. Both writes are
atomic (either both happen or neither — wrap in a write
lock).

**AC7 — `surface_project_update` respects 24h budget.** Two
surfacings of `update_kind="checkin"` within 24h: first
succeeds; second silently no-ops with reason logged. Single
`update_kind="deadline_approaching"` bypasses the budget
(bypasses for true-deadlines only).

**AC8 — `project_checkin` workflow registers.** Bring-up
calls `register_project_checkin_workflow` (architect-actor
auth via existing pattern). Trigger fires on
`project.checkin_due` events.

**AC9 — Scheduled trigger fires at the right time.** A
project created with `checkin_interval_sec=10` (test
fixture) emits `project.checkin_due` within 15s of
creation. The workflow handler runs, classifies urgency,
either surfaces or reschedules.

**AC10 — Decisions weave into prompt.** Assembler test:
project with 3 recorded decisions, user message in the
project's space → assembled prompt contains a "Recent
project decisions:" block with all 3 decisions in
reverse-chronological order, bounded to budget.

**AC11 — Project state survives substrate restart.**
Restart the bot mid-project. After bring-up:
- `/project list` still shows the project
- The scheduled check-in trigger still fires at the
  scheduled time
- Canvas content is unchanged
- Decision history is preserved

**AC12 — `/project complete` graceful close.** Owner-only.
Sets `lifecycle_state="completed"`, records
`completed_at` + optional `completion_summary`. Cancels
future check-in triggers. Canvas preserved as read-only
reference (not deleted).

**AC13 — Multi-member future-compat (no v1 regression).**
v1 ships single-owner projects only. The `project_state`
schema includes `owner_member_id` field; future shared-
project work adds a `project_members` link table. v1 must
not block this extension (the FK structure + index design
documented to accommodate).

**AC14 — Cadence respects user.** `checkin_interval_sec`
is configurable per project. Default 604800 (1 week).
Setting to 0 disables scheduled check-ins (manual-only
surfacing). Setting to <86400 (1 day) is rejected at the
tool layer (anti-badger).

---

## Out of scope (deferred)

- **Shared projects across members** — v1 is single-owner.
  v1.1 sub-spec (`SHARED-PROJECTS-V1`) composes with the
  existing relationship + sensitivity machinery.

- **Project templates** — v1 ships ONE seed template (the
  generic "vision/phases/decisions/..." shape). A template
  registry (`/project start --template=wedding` etc.) is
  v1.1 sub-spec.

- **Project export to external tools** — exporting project
  state to Notion / Google Docs / similar is a future
  integration spec, not v1 substrate.

- **Cross-project dependencies** — projects with sub-projects
  or dependencies between projects are future capability.
  v1 projects are independent.

- **LLM-driven proactive surfacing** — the v1
  classify_project_checkin_urgency uses a deterministic
  heuristic (decisions-count + elapsed-time + deadline-
  proximity). LLM-driven "should I surface this?" is a v1.1
  refinement (LONG-HORIZON-PROJECT-SURFACING-CLASSIFIER-V1).

- **Project budgets / cost tracking** — financial tracking
  is its own primitive. v1 projects record costs as
  free-form decisions; structured budget tracking is a
  follow-up.

- **Deadline-extraction from natural language** — v1 requires
  explicit `manage_schedule` calls to set deadlines. Auto-
  extracting "by next Tuesday" from user prose into
  scheduled triggers is a follow-up (compose with the
  conversational-trigger work also pending).

---

## Test plan (scoped per [[feedback-test-scope-proposal]])

New tests:

- `tests/test_project_state_store.py` — table CRUD,
  unique constraints, index performance pin (AC1, AC11).

- `tests/test_project_slash_commands.py` — `/project start`
  (owner + non-owner), `list`, `status`, `complete`, `pause`,
  `resume` (AC2, AC3, AC4, AC5, AC12).

- `tests/test_start_project_tool.py` — composition correctness:
  space created, canvas seeded with all template pages,
  state row inserted, trigger scheduled (AC2, AC9).

- `tests/test_record_project_decision.py` — canvas write +
  knowledge entry creation are atomic; decision_id returned
  uniquely identifies the decision; reverse-chrono ordering
  pin (AC6, AC10).

- `tests/test_surface_project_update_budget.py` — 24h budget
  enforcement; deadline_approaching bypass (AC7).

- `tests/test_project_checkin_workflow.py` — workflow YAML
  parses; both branches reachable; trigger fires; state
  loaded correctly (AC8, AC9).

- `tests/test_assembler_project_decisions.py` — decisions
  weave into prompt; budget bounded; reverse-chrono
  ordering (AC10).

Regression touch:
- `tests/test_spaces.py` — project spaces don't break
  existing space machinery
- `tests/test_canvas.py` — seeded pages register correctly
- `tests/test_manage_schedule.py` — project-scheduled
  triggers fire alongside existing schedule triggers

---

## Resolved pre-spec decisions

**Composition vs new substrate.** Resolved in design
principle 1: this spec is shape + helpers, not new core
substrate. Reduces blast radius + leverages existing primitives.

**Cadence default = 1 week.** Resolved per architect
2026-05-27 ("ambient, not demanding"). 1 week is the
default; user can adjust. Below 1 day rejected outright
(anti-badger).

**Decision weaving budget = 10 decisions max.** Resolved
per [[feedback-substrate-tests-over-count]] / tool-budget
patterns. Below tool-window budget; doesn't displace
tool schemas.

**Single-owner only in v1.** Resolved per architect's
"conservative defaults until confirmed" principle.
Multi-member projects compose cleanly with existing
relationship machinery as v1.1.

---

## Open questions for architect ratification

1. **Should `/project start` accept structured initial
   sections, or always seed the default template?** Spec
   defaults to "always seed default; user edits after." 
   Alternative: `/project start "Wedding" --vision "..." --phases "..."`.

2. **Should the project's canvas be a NEW canvas type or
   reuse the existing `canvas` primitive with a tag?**
   Spec defaults to "reuse + tag." Alternative: a
   `project_canvas` subtype. Tag-based is simpler;
   subtype gives stronger schema enforcement.

3. **How does the agent know to use `record_project_decision`
   vs writing prose?** Spec proposes a heuristic in the
   agent's template: "in a project space, when the user
   makes a decision-shaped statement, call
   `record_project_decision`." Could be enforced more
   strongly (LLM intent classifier) or left to template
   guidance (simpler, possibly less reliable).

4. **Should project completion auto-archive after N days
   of no activity?** Spec defaults to "no — user must
   explicitly complete." Alternative: auto-archive 90
   days post-completion. Auto-archive feels paternalistic;
   leaving it manual respects user agency.

5. **Should the classify_project_checkin_urgency
   heuristic be deterministic or LLM-driven in v1?**
   Spec defaults to deterministic (decisions-count +
   elapsed-time + deadline-proximity). Architect call on
   whether LLM-driven is worth the cost+latency in v1.
