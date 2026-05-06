# Canvases — Scoped Directories of Markdown Pages

> A primitive for accumulating structured content across turns and members: world-building notes, decision history, project logs, household planning, campaign state.

## The gap this fills

Kernos has good primitives for short-lived thought (the conversation log), long-lived facts (knowledge entries), and procedural memory (`_procedures.md` per space). None of those are the right shape for a body of content that *grows* — that a member returns to, revises, adds to, shares with another member. Writing a world bible in chat doesn't work. Stuffing it into procedures.md collapses the structure. Covenants are rules, not content.

A **canvas** is a named directory of markdown pages, with YAML frontmatter, that lives under a declared visibility scope. Members create canvases the way they'd create a shared Google Drive folder — named, scoped, durable — and the agent works the pages alongside them.

## Scope model

Three scope tiers, fixed at creation, non-negotiable:

| Scope       | Who can see it                                                                   | Typical use                                         |
| ----------- | -------------------------------------------------------------------------------- | --------------------------------------------------- |
| `personal`  | only the creator                                                                 | private world-building, draft decisions             |
| `specific`  | the creator + the members listed at creation                                     | a couple's shared household planning                |
| `team`      | every instance member, current and future                                        | team roadmap, shared reference                      |

Out-of-scope members don't see that the canvas exists. Enforcement layers at both the registry query (`list_canvases_for_member` filters by scope + `canvas_members` rows) and at context-assembly time (`filter_canvases_by_membership` in the disclosure gate — belt-and-braces).

## On-disk layout

```
data/{instance_id}/canvases/{canvas_id}/
    canvas.yaml         # canvas-level metadata
    index.md            # landing page (auto-created)
    {slug}.md           # content pages
    {slug}.v{N}.md      # prior versions (retained on page_write)
```

Pages are markdown with a YAML frontmatter block. Fields the system reads:

- `title`, `type`, `state` — the page's lifecycle state
- `last_updated`, `last_updated_by` — written by `page_write`
- `watchers` — list of member_ids notified on state change
- `routes` — `{state_name: [targets...]}`; fired on state transition
- `consult_operator_at` — overrides the canvas / instance default

Anything else the agent wants to include is preserved round-trip.

## Page types — advisory only

Page types are a vocabulary suggestion, not an enforcement axis:

| Type       | States                                 | Notes                                |
| ---------- | -------------------------------------- | ------------------------------------ |
| `note`     | `drafted` → `current` → `archived`     | default                              |
| `decision` | `proposed` → `ratified` → `superseded` | routes often declared here           |
| `log`      | (none — append-only semantics)         | skips the cross-member consent gate  |

`v1` accepts writes with any state string. The type table drives the default route detector and seeds templates; it does not restrict what the agent can write.

## Registry — repurposed shared_spaces

The `shared_spaces` table in `instance.db` was declared for V2 shared spaces but never used. CANVAS-V1 repurposes it as the canvas registry — each canvas has one row (`canvas_id` aliases `space_id`) plus the canvas-specific columns (`scope`, `owner_member_id`, `pinned_to_spaces`, `canvas_yaml_path`). A companion table `canvas_members(canvas_id, member_id, added_at, active)` holds explicit memberships for `personal` + `specific` scopes. Team scope is served by scope checks alone — no per-member row is required.

`InstanceDB.add_canvas_member(canvas_id, member_id)` writes the per-member row at canvas-creation time (called four times in `canvas.py` — for the creator, the explicit specific-scope members, optionally the inherits target, and any post-creation team-scope adjustment that internal code performs). **It is not currently agent-exposed as a kernel tool.** Post-creation membership management (adding or removing a member from an existing canvas after `canvas_create`) is a deferred follow-on — the substrate exists, the agent-callable surface does not. Tracked as a parked follow-up; will land if real friction surfaces.

## The dispatch gate + consent

Canvas tools classify through the standard `DispatchGate`:

| Tool            | Effect       | Why                                               |
| --------------- | ------------ | ------------------------------------------------- |
| `canvas_list`   | `read`       |                                                   |
| `page_read`     | `read`       |                                                   |
| `page_list`     | `read`       |                                                   |
| `page_search`   | `read`       |                                                   |
| `page_write`    | `soft_write` | reversible — prior versions retained as `.vN.md`  |
| `canvas_create` | `hard_write` | provisions shared state + fires notifications     |

A separate layer sits **above** the gate: the **cross-member consent gate**. A `page_write` to a `team` or `specific` canvas (non-log pages only) without `confirmed=true` in the input does **not** write — it returns `{ok: false, requires_confirmation: true, proposed_summary, other_members}` so the agent can surface the edit to the user and re-call with `confirmed=true` after they approve. This keeps cross-member writes explicit and user-authorized without requiring judgment at the gate boundary.

## Routes-lite

A state transition on `page_write` (detected by comparing new `state` to `prev_state`) fires routes declared in the page's frontmatter:

```yaml
routes:
  ratified: [operator, member:bob]
```

Targets recognized in v1:

- `operator` — resolves to the canvas owner
- `member:<id>` — a specific member by member_id
- `space:<id>` — **not supported in v1**; logged as `route_target_not_supported_in_v1` and skipped

Each target addressee receives a `route_fire` envelope through the relational-message pipeline.

### `consult_operator_at` — shallow inheritance

A separate, non-bypassable precedence mechanism. When a state transition lands in the page's resolved `consult_operator_at` list, the operator is **added** to the target set regardless of whether `routes` declared them. Resolution:

```
page_value  →  canvas_default  →  instance_default
```

Replacing, not merging. An explicit `[]` at any level is a valid "never consult" override. Module default is `('shipped', 'on_conflict')`.

## Notifications

Two distinct notification paths, both routed through `RelationalDispatcher`:

- **`canvas_offer`** — sent on successful `canvas_create` to each declared member (or every other instance member, for `team` scope). Carries the `canvas_id` in a typed back-reference field parallel to `parcel_id`.
- **`canvas_watch`** — sent to watchers declared in the page's frontmatter when a page's **state** changes (not on plain body edits). In-process coalescing window of 10 minutes per `(canvas_id, page_path, watcher)` prevents rapid-fire whispers during active editing.

Both are best-effort: emission failures log and swallow; canvas ops never break on notification failure.

## Event stream

`CanvasService` emits the following events through the unified event stream:

| Event                        | Fires on                                             |
| ---------------------------- | ---------------------------------------------------- |
| `canvas.created`             | successful `canvas_create`                           |
| `canvas.page.created`        | first `page_write` to a slug                         |
| `canvas.page.changed`        | subsequent `page_write` to an existing slug          |
| `canvas.page.state_changed`  | state transition (new ≠ prev)                        |
| `canvas.page.archived`       | state transition → `archived`                        |

## Context surface

The **Available Canvases** zone in the assembled system prompt lists the canvases the member can see. It sits alongside `PROCEDURES` and `MEMORY` in the dynamic region — cacheable-prefix-eligible, changes only when a canvas is created / archived / repinned.

Per-space pinning:

- `pinned_to_spaces = []` (or unset) — universal visibility
- `pinned_to_spaces = [space_ids...]` — shown only when the active space matches

A world-building canvas pinned to the "Valencia campaign" space doesn't clutter a spreadsheet space; a team-wide reference left unpinned shows up everywhere.

## Relationship to other primitives

- **Not conversation.** Canvas writes bypass `conversation_log` and compaction entirely. Canvases are artifacts, not turn-level dialogue.
- **Not covenants.** Covenants are short behavioral rules; canvases are bodies of content. The agent asks "is this a rule (covenant) or a workflow (procedure) or a body of work (canvas)?" at instruction time.
- **Not parcels.** A parcel is a one-shot file transfer between member spaces. A canvas is a durable shared artifact.

## Section markers and the Gardener

Canvas v1 shipped the primitive. A follow-on batch (CANVAS-SECTION-MARKERS + GARDENER) layered two pieces on top:

- **Section markers** make individual pages legible as content accumulates. Sections are H2-delimited with HTML-comment metadata (`summary`, `tokens`, `last_updated`) that stays invisible in Markdown renderers. `page_read` gains `mode=summary|section` for navigable outlines and targeted reads; `page_write` can target a single section surgically.
- **The Gardener cohort** makes canvases stay well-shaped over their whole life. It picks initial patterns at creation time by consulting the Workflow Patterns library, and runs continuous-evolution heuristics on page events. See [Gardener architecture →](gardener.md) for the full discipline.

## Scope deliberately deferred

- **Friction Observer canvas-opportunity signal** — detection of the shape "this is a canvas-worthy accumulation" is left to a follow-on batch. V1 ships only the default canvas-recognition procedure in the agent template.
- **`space:<id>` route targets** — v1 returns the structured `route_target_not_supported_in_v1` marker; space-scoped routing lands with V2 shared spaces.
- **Persistent watcher coalescing** — coalescing windows are in-process; restart resets the window. Acceptable for v1; persistent coalescing would need a new table.
- **Relationship-aware auto-offer** — canvas membership changes after creation are possible via `add_canvas_member` in the DB but aren't exposed as an agent tool yet.
