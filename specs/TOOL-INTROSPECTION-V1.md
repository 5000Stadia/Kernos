# TOOL-INTROSPECTION-V1

**Date:** 2026-05-22
**Status:** Draft for review (sub-spec #4 of `TOOL-MAKING-ARC-V1`)
**Scope:** Two introspection surfaces over the catalog metadata
  shipped by `LIVE-DISPATCH-UNBLOCKER-V1`:
  1. **`/tools` slash command** (operator-facing) — structured
     catalog dump with filters. Classic operator surface;
     tabular text, all the metadata.
  2. **`inspect_tools` kernel tool** (agent-facing) — natural-
     prose response describing what's available. NOT a JSON
     dump. The agent reads English, not a dataset.
**Estimated size:** ~180 LOC source + ~120 LOC tests.

## Why this spec exists

Per `TOOL-MAKING-ARC-V1` D4: operator (and the agent) need a
single source of truth for "what tools exist right now,
classified how, where do they live?" Today there's no operator
slash command for catalog inspection AND the agent's only
visibility into the catalog is the per-turn surfaced subset.

Operator use case: catalog audit, classification audit before
flipping `LIVE-DISPATCH-UNBLOCKER-V1`'s gate-evaluate-on-live
behavior, post-incident "what changed in the catalog this week."

Agent use case: when composing a multi-step plan that requires
a capability the agent isn't sure exists in the catalog (e.g.,
"do I have a weather tool ANYWHERE, or do I need to register
one for this plan?"). Different from per-turn surface status —
the agent is asking about catalog membership across time, not
"is X loaded right now."

## Design principles (load-bearing)

Per [[agent-facing-natural-simplicity]]: the two surfaces are
**deliberately different shapes** because their audiences are
different.

- **`/tools` slash command** — operator-facing. Structured
  tabular text. Status enums, classifications, hashes,
  registration timestamps all in plain view. The operator
  WANTS this richness; it's how they do their job. Sophistication
  matches actual need.
- **`inspect_tools` kernel tool** — agent-facing. Natural-prose
  response. The agent receives a sentence-shaped overview, not
  a JSON dump. The substrate composes the prose from the catalog
  metadata; the agent doesn't translate.

This is the layered design principle made concrete: same
underlying catalog metadata, two surfaces shaped for two
audiences. Sophistication preserved (operator gets it);
naturalness preserved (agent gets it).

## Not a return to PREFLIGHT

This sub-spec was almost rejected as a PREFLIGHT-pattern
mistake (agent calls a checking tool to query state the
surfacer should handle). The distinction that keeps it in:

- **PREFLIGHT was:** "is X currently surfaced for this turn?"
  → that's surfacer's job; checking is a band-aid.
- **INTROSPECTION is:** "does capability X EXIST in the catalog
  across time?" → genuinely different question. The agent
  composes a 5-step plan; it needs to know whether a capability
  is in-catalog (so the plan is feasible) before committing.
  Per-turn surfacing answers a different question.

Trajectory-aware surfacing (the
`POSTURE-PREDICTIVE-SURFACING` migration in `da05a95`) reduces
how often the agent needs `inspect_tools` — well-ranked tools
appear naturally. `inspect_tools` is the BACKUP when ranking
misses or when the agent is genuinely planning ahead beyond
the current turn's signal.

If observed usage shows the agent over-calling
`inspect_tools` because surfacing isn't catching its intent,
the fix is in the surfacer, not in deprecating introspection.

## Current state

- `ToolCatalog` (`kernos/kernel/tool_catalog.py`) carries
  catalog entries with name, source, and (after
  `LIVE-DISPATCH-UNBLOCKER-V1`) classification, registration
  status, registration_hash. `get_metadata()` exposes this.
- Operator has no `/tools` command today; they inspect the
  catalog via `/dump` or the source code.
- Agent's only catalog visibility is the per-turn surfaced
  subset in the system prompt (built by
  `build_tool_directory`).

## Design

### `/tools` slash command (operator)

Owner-only (mirrors `/posture` auth pattern). Subcommands:

| Form | Output |
|---|---|
| `/tools` | Full catalog listing grouped by source: stock / workspace / mcp_capability. Per entry: name + classification + status + last-registered timestamp. |
| `/tools <name>` | Detail view for one tool: full metadata (source, classification, service_id, registration_hash, descriptor_file path, audit_category, recent invocation count). |
| `/tools source=<value>` | Filter to entries with matching source (stock / workspace / mcp_capability). |
| `/tools classification=<value>` | Filter to entries with matching gate_classification (read / soft_write / hard_write / unknown). |
| `/tools status=<value>` | Filter to entries with matching status (active / pending / archived). |

Output format: monospace tabular text, dense enough that the
operator can scan a 50-tool catalog comfortably. No
agent-facing transformation needed — the operator is the
audience.

### `inspect_tools` kernel tool (agent — natural prose)

Pinned in `ALWAYS_PINNED` (agent always reaches it; consistent
with other introspective tools like `inspect_state`,
`dump_context`). Read classification.

**Input shape (sophisticated where useful):**

```python
inspect_tools(
    focus: str = "",        # "" = overview; "tool_name" = focused view
    capability: str = "",   # optional: filter by capability area
                            # ("calendar", "files", "messaging", etc.)
                            # Used by the substrate to compose a relevant
                            # prose summary, not exposed as a hard taxonomy.
)
```

**Output shape (natural prose, the load-bearing piece):**

The substrate inspects the catalog AND the per-turn surface
state, then composes a sentence-shaped response. Examples:

**Overview call** (`inspect_tools()`):
> *"You have access to about 50 tools across four areas. Most of
> what you'll use day-to-day is already loaded: memory tools,
> files, conversation primitives. Beyond that: you can manage
> covenants and members, schedule reminders, work with canvases
> and pages. Connected services include Google Calendar
> (read/write events) and Notion (search + write pages). You've
> registered 3 workspace tools yourself: `weather_lookup`,
> `expense_tracker`, `meeting_notes_compiler`. If you want
> details on any specific one, pass `focus="tool_name"`."*

**Focused call** (`inspect_tools(focus="weather_lookup")`):
> *"`weather_lookup` is a workspace tool you registered two days
> ago. It takes a `location` (string) and returns current
> conditions + 3-day forecast. Loaded right now? Yes. Last used
> 4 hours ago. Classified as `read` — no side effects."*

**Capability call** (`inspect_tools(capability="calendar")`):
> *"For calendar work you have: Google Calendar's built-in tools
> (`get_calendar`, `create_event`, `update_event`, `delete_event`)
> — all connected and loaded. You can also use `manage_schedule`
> for substrate-level reminders that don't need a calendar
> service. No workspace-registered calendar tools."*

The substrate carries the prose-composition logic. The agent
receives a string it can include verbatim in its response to
the user OR reason about for planning. No JSON parsing, no
status enum lookups. The agent's surface stays elegant.

**Substrate detail (operator-visible via /tools or /dump):**
The full structured catalog data backs the prose. Operators
who want the raw shape use `/tools` (slash command), not
`inspect_tools`. The agent gets the natural-prose distillation.

### Substrate composition rules

The substrate's prose composer (a new helper in
`kernos/kernel/tool_introspection.py`) groups tools by source +
capability area, builds short natural sentences, and adapts
the response to the focus / capability filters. Implementation
sketch — keep it small + readable:

```python
def compose_overview_prose(catalog, surfaced_names) -> str:
    groups = group_by_capability(catalog)
    sentences = []
    sentences.append(natural_count_sentence(catalog))
    for area, tools in groups.items():
        sentences.append(area_sentence(area, tools, surfaced_names))
    sentences.append(closing_tip())
    return " ".join(sentences)
```

Groups by capability area (memory, files, conversation,
substrate-management, scheduling, canvases, messaging, services,
workspace-built). The mapping is heuristic per-tool-name; if a
tool doesn't match a known area, it lands under "other
substrate tools" rather than crashing.

**Why not LLM-compose the prose?** The composition is
deterministic, fast, and inspectable. An LLM call here would
be cost without value — the data is well-known shape, the
sentence templates fit cleanly, the agent doesn't need
creativity at this surface.

## Acceptance criteria

### `/tools` slash command

| AC | Description |
|---|---|
| AC1 | `/tools` (no args) outputs all catalog entries grouped by source. |
| AC2 | `/tools <name>` outputs detail view for a specific tool. |
| AC3 | `/tools <unknown_name>` returns a helpful "not found" message + suggestion to list. |
| AC4 | `/tools source=workspace` filters to workspace tools only. |
| AC5 | `/tools classification=hard_write` filters to hard_write tools only. |
| AC6 | `/tools status=pending` filters to pending registrations only. |
| AC7 | `/tools` from non-owner returns owner-only error, no state read. |

### `inspect_tools` kernel tool

| AC | Description |
|---|---|
| AC8 | `inspect_tools()` returns prose (not JSON, not enum codes). |
| AC9 | `inspect_tools()` overview mentions count, capability areas, surfaced subset. |
| AC10 | `inspect_tools(focus="X")` returns prose describing tool X's purpose + status. |
| AC11 | `inspect_tools(focus="unknown_tool")` returns prose explaining the tool isn't in the catalog + suggests `register_tool` or `request_tool`. |
| AC12 | `inspect_tools(capability="calendar")` returns prose grouped around calendar tools. |
| AC13 | Response prose is plain English — assertable absence of `{`, `}`, status enum tokens, JSON markers. |
| AC14 | `inspect_tools` is in `ALWAYS_PINNED` so it's always reachable. |
| AC15 | `inspect_tools` classifies as `read` at the gate. |
| AC16 | No regression on existing tool-catalog tests. |

## Soak gate

1. **Automated**: ACs pin both surfaces' shape + the agent-facing
   plain-English contract.
2. **Operator soak**:
   - `/tools` → see the grouped catalog.
   - `/tools manage_plan` → see detail view.
   - `/tools source=workspace` → see only workspace tools.
3. **Agent soak**: in a conversation, ask the agent "what tools
   do you have for working with calendars?" Verify the agent
   calls `inspect_tools(capability="calendar")` (or similar) AND
   that the agent's response to the user reads naturally — no
   JSON pasted, no status codes leaked.

## Out of scope

- Real-time surfacing visibility ("why was X NOT loaded
  this turn?") — that was PREFLIGHT-V1 and stays rejected.
- Per-user capability filtering — when an MCP capability is
  per-member, the introspection should respect member access.
  Not v1; member-scoped capability work has its own arc.
- Cost / usage statistics in the introspection output — costs
  live in the cost aggregator, not in the catalog. Future spec
  can join.

## Risks

- **Risk:** Agent over-calls `inspect_tools` because it doesn't
  trust per-turn surfacing.
  - **Mitigation:** Track usage. If the agent calls it more
    than 2x per session sustained, that's a signal that
    surfacing needs work, NOT that introspection is wrong.
    Surface this as a friction observer pattern. Capability
    works as intended; over-use signals a different problem.

- **Risk:** The prose composer becomes a complex template
  engine over time.
  - **Mitigation:** Keep it small + deterministic. If
    composition gets gnarly, simplify or pass less context
    to the agent (let it ask `focus=<X>` to drill in).
    Never call an LLM for composition.

- **Risk:** `/tools` output grows unwieldy as the catalog
  grows past 100 tools.
  - **Mitigation:** Filters cover the common slice cases.
    Pagination is a follow-up if 100+ catalog entries become
    realistic.

## Dependencies

- `LIVE-DISPATCH-UNBLOCKER-V1` — provides `catalog.get_metadata()`
  + the structured metadata both surfaces consume. Must ship
  first.
- `TOOL-AUDIT-NORMALIZATION-V1` — not a hard dependency, but
  `/tools <name>` detail view can show "recent invocation count"
  derived from audit entries if AUDIT lands first. If
  INTROSPECTION ships first, the count field omits.

## Migration

- No schema change.
- `inspect_tools` is a new kernel tool — additive. Existing
  test fixtures unaffected.
- `/tools` is a new slash command — additive. Existing slash
  commands unaffected.
- Catalog read API (`get_metadata`) was added by
  `LIVE-DISPATCH-UNBLOCKER-V1`; this spec just consumes it.
