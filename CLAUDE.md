# CLAUDE.md — Instructions for Claude Code

## Project: KERNOS

A personal agentic operating system. As-built architecture: `docs/TECHNICAL-ARCHITECTURE.md`.

## Before You Do Anything

1. **Read `DECISIONS.md` first.** The NOW block tells you what to do. It contains the Active Spec you should execute and recent architectural decisions. If something in DECISIONS.md conflicts with other documents, DECISIONS.md wins (it's more recent).
2. **Read `docs/TECHNICAL-ARCHITECTURE.md`** for as-built architecture — components, data flows, interfaces. (`docs/reference/blueprint-original-vision.md` is a historical vision document, not the current reference.)
3. **Execute only the Active Spec** in DECISIONS.md. Don't jump ahead to future phases. Don't build things not in the current spec. Planning lives in Notion; specs come to you via DECISIONS.md.
4. **Check instance_id naming** — the codebase uses `instance_id` (not `tenant_id`). All state is keyed to `instance_id`.

## Kernel Architecture Context

Read `docs/reference/kernel-architecture-outline.md` for the kernel design vision. Key conventions for the kernel layer (`kernos/kernel/`):

- **Event emission is best-effort.** Every `emit()` call is wrapped in try/except. Event logging failures never break the user's message flow.
- **State Store is the query surface.** Runtime lookups go to the State Store, not the Event Stream. The Event Stream is for append, replay, and audit.
- **Concurrency:** SQLite with WAL mode (shipped 2026-04-12). Concurrent reads supported. Write serialization handled by SQLite's single-writer model. busy_timeout=5000ms. Legacy JSON backend available via `KERNOS_STORE_BACKEND=json`.
- **Shadow archive:** No method permanently deletes data. "Removal" sets `active: false`.
- **Cost logging:** Every reasoning call logs model, tokens, estimated cost, duration via events.
- **Multi-member:** Every member is first-class. Per-member profiles in instance.db (display_name, agent_name, personality_notes, timezone, interaction_count, hatched, bootstrap_graduated). Per-member conversation logs keyed to (instance, space, member). Knowledge entries tagged with `owner_member_id` and `sensitivity`. The Soul dataclass is deprecated for identity — all per-member state lives in member_profiles.
- **Hatching:** Each member's agent hatches independently through organic 15-turn conversation. Graduation produces personality_notes via LLM consolidation. The agent names itself when the moment is right, not on turn 1.
- **Stewardship + sensitivity:** Compaction harvest extracts values, detects tensions, classifies sensitivity (open/contextual/personal). Operational insights surface as whispers only when there's a concrete actionable idea.
- **Relationships:** Declared pairwise between members. Four permission profiles. Conservative defaults until confirmed. Topic exceptions are covenants with `relationship:` scope.
- **Abuse prevention:** The 24 Escalation — each failed sender attempt escalates immediately (24s→24m→24h→24d→24y→24c). Spamming while blocked accelerates.

## Architectural Constraints (Always Enforced)

These are non-negotiable. Violating any of these is a build failure regardless of what the Active Spec says:

- **Adapter/handler isolation:** The handler NEVER imports from adapters. Adapters NEVER import from the handler. They share only the NormalizedMessage model.
- **instance_id from day one:** Every piece of state is keyed to a `instance_id`. No code ever assumes a single user.
- **Protect user data based on loss cost:** Destructive actions on user data require judgment at the dispatch boundary. Low ambiguity + low loss cost = execute ("delete the 5:00 entry we just made"). High loss cost = confirm first ("delete all my calendar events"). Ambiguity + any loss cost = clarify ("clear my reminders" — which ones?). Internal operational artifacts (expired tokens, whispers, suppression entries) are housekeeping — delete freely. This is not a universal ban on deletes; it's a principle of proportional caution.
- **Graceful errors:** Every failure mode produces a friendly user-facing response. Never a silent crash, never a raw exception.
- **MCP for capabilities:** Tools and data are accessed through MCP. No direct API integrations that bypass the capability abstraction layer.

## Spec Execution Principles

**Implementation latitude:** Specs define the intention, not a literal recipe. Where a spec prescribes specific implementation details, treat them as guidance. If a cleaner approach achieves the same goal within Kernos's architecture and conventions, use your judgment. When choosing between options, pick what best serves the intention of the feature being specced and the foundational principles of Kernos: conservative by default, memory as the moat, ambient not demanding, and earning trust through thousands of correct small actions.

## When You're Done

- Run the acceptance criteria listed in the Active Spec. All must pass.
- Run `pytest` — all tests green.
- Verify architectural constraints (especially import isolation — grep for it).
- Update the relevant `docs/` section to reflect any new or changed components from this spec. The docs are the canonical reference for how the system works.
- Do NOT update DECISIONS.md status yourself. The owner and design review handle that.

## Code Style

- Python 3.11+
- Type hints on all function signatures
- `logging` module for all logging (no print statements)
- Docstrings on public classes and functions
- Keep it simple — no premature abstraction beyond what the spec requires