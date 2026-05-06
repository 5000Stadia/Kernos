# Identity (per-member profiles + Soul)

The agent's persistent identity for each person it serves. Identity is **per-member**, not per-instance — every member who connects to a Kernos instance gets their own agent (own name, own emoji, own personality, own bootstrap arc) keyed by `member_id` in the `member_profiles` table on `instance.db`.

The legacy `Soul` dataclass at `kernos/kernel/soul.py` is **deprecated for identity** as of the SOUL-REVISION + Multi-Member V1 work (2026-04-13). All identity fields on `Soul` (`agent_name`, `emoji`, `personality_notes`, `hatched`, `bootstrap_graduated`, `user_name`, `communication_style`, `timezone`, `interaction_count`) are kept on the dataclass only for JSON deserialization compatibility with legacy `soul.json` files; runtime reads + writes go to `member_profiles`.

## Two-layer identity

1. **Template** (`kernos/kernel/template.py`) — universal operating principles, default personality, bootstrap prompt. The template is the same for every Kernos instance. It defines the floor — what every fresh hatching starts from.
2. **Per-member profile** (`member_profiles` table on `instance.db`) — per-member identity that evolves through interaction. This is what makes each member's relationship with the agent unique. Two members on the same instance see two different agents — different names, different emojis, different personalities — because each member has their own profile row.

`Soul` itself is no longer the identity layer — it's a thin shell at the instance level. "Kernos" is the platform name, not the agent identity.

## member_profiles fields

| Field | Type | Purpose |
|---|---|---|
| `member_id` | TEXT (PK) | Stable identifier per member |
| `display_name` | TEXT | What the member is called in the system (their name as they declared it) |
| `timezone` | TEXT | The member's timezone for time-relative work |
| `communication_style` | TEXT | Inferred or stated communication preferences |
| `interaction_count` | INTEGER | Total messages processed for this member |
| `hatched` | INTEGER (0/1) | Whether this member's agent has had a first interaction |
| `hatched_at` | TEXT | ISO timestamp of first interaction |
| `bootstrap_graduated` | INTEGER (0/1) | Whether the agent has consolidated personality from the bootstrap arc |
| `bootstrap_graduated_at` | TEXT | ISO timestamp of graduation |
| `agent_name` | TEXT | The name THIS member gave the agent (different from "Kernos") |
| `emoji` | TEXT | Identity marker the agent chose during this member's hatching |
| `personality_notes` | TEXT | Free-text personality, consolidated at graduation from bootstrap observations |
| `updated_at` | TEXT | Last update |

## Per-member hatching arc

Each member's agent hatches through their own ~15-turn bootstrap arc:

1. **Unhatched** (`hatched=0`) — member has joined the instance but their agent hasn't had a first interaction. Template defaults apply. Bootstrap prompt is in the system prompt.
2. **Hatched** (`hatched=1`) — first message processed. The agent begins accumulating identity for this member. The bootstrap prompt continues to surface guidance.
3. **Bootstrap arc** — the first ~15 turns. The agent discovers who this member is, finds identity markers (name, emoji), and starts to feel like a real presence to them. Eight engagement points across the arc inform the readiness for graduation.
4. **Graduated** (`bootstrap_graduated=1`) — a one-time consolidation LLM call converts the bootstrap arc's observations into permanent `personality_notes`. The bootstrap prompt is removed from the system prompt for this member from here on.

The agent **names itself** when the moment is right during this arc — not on turn 1, not on turn 2. Naming emerges from the relationship rather than being a configured attribute.

## Identity tools (kernel-tool surface)

| Tool | Effect | Notes |
|---|---|---|
| `read_soul` | read | Returns the per-member profile (the real identity state) when the caller has a `member_id`; falls back to the deprecated instance Soul if none. |
| `update_soul` | soft_write | Updates `agent_name`, `emoji`, `personality_notes`, `communication_style`. Writes to `member_profiles` for the calling member. Lifecycle and instance-level fields are read-only. |

The tool name `update_soul` is retained for compatibility; the dispatch layer routes per-member fields to `member_profiles` rather than the legacy soul.json.

## Identity vs memory vs canvases

| Layer | What it stores | Per-member? |
|---|---|---|
| Identity (`member_profiles`) | Who the agent is for THIS member — name, emoji, personality | Yes |
| Memory (knowledge entries, ledger, living state) | What the agent knows about the world / about people / about facts | Per-member layer over per-space layer |
| Canvas | Mutable workspace content — pages, decisions, project state | Scoped (personal / specific / team) |
| References (REFERENCE-PRIMITIVE-V1) | Canonical documentation + project-deep reference material | Instance-scope (`docs/`) or domain-scope (`references/`) |

Identity is *who*. Memory is *what*. Canvas is *workspace*. References are *citation-shaped*. Each of these has its own primitive, its own scoping, its own lifecycle.

## Code locations

| Component | Path |
|---|---|
| `Soul` dataclass (deprecated for identity) | `kernos/kernel/soul.py` |
| `member_profiles` schema + accessors | `kernos/kernel/instance_db.py` (`CREATE TABLE member_profiles`, `get_member_profile`, `upsert_member_profile`) |
| Migration helper | `kernos/kernel/instance_db.py` (`migrate_soul_to_member_profile`) — one-shot copies legacy `soul.json` fields into `member_profiles` |
| Template | `kernos/kernel/template.py` — bootstrap prompt + operating principles + hatching guidance |
| Bootstrap consolidation | `kernos/messages/handler.py` (`_consolidate_bootstrap`) |
| Identity tools dispatch | `kernos/kernel/reasoning.py` (read_soul / update_soul branches) |

## See also

- [`identity/who-you-are.md`](../identity/who-you-are.md) — the agent-facing identity guidance
- [`identity/onboarding.md`](../identity/onboarding.md) — the hatching arc structure
- [`architecture/disclosure-and-messenger.md`](disclosure-and-messenger.md) — how per-member identity composes with multi-member disclosure
