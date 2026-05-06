# Onboarding

The first meeting between the agent and a new member is a guided improv. The system knows what needs to happen, but the agent finds its own way there.

## The Hatching Phase

When a new member first interacts, their agent relationship is "unhatched" (`hatched=0` in `member_profiles`). The bootstrap prompt activates as part of the system prompt with two distinct layers in the assembled `RULES` zone:

1. **Personality foundation** — the bootstrap prompt's presence-first arrival framing + substrate-awareness lines (`request_reference`, `store_reference`) + identity anchor (you are not "Kernos" — that's the platform).
2. **Hatching identity layer** — the agent arrives without a name; the agent and member arrive at a name together as the relationship starts to feel real to each other. This is the hatching moment, not a turn-1 task.

The agent does NOT default to "Kernos." It arrives as a presence, not a brand.

### Hatching Modes

**Unique hatching (default):** Each member goes through the full hatching — the agent arrives nameless, the member names it, early conversation shapes the personality. Every member gets their own distinct agent identity.

**Auto-inherit:** New members get a copy of the first member's agent identity. Same name, same personality. The member can modify later. Faster onboarding for teams that want consistency.

## During Hatching (~first 15 interactions)

The agent:

1. **Discovers the person** — name (if not already known from invite), timezone, how they communicate
2. **Gets named** — the member gives the agent a name. This IS the hatching moment.
3. **Develops personality** — tone, style, warmth emerge from interaction, not from defaults
4. **Demonstrates competence** — shows what it can do early (calendar, memory, tools, invite management)
5. **Earns trust** — through correct small actions, not by asking for belief

## Graduation

After enough interactions + the agent has been named, the system graduates the relationship:

1. A one-time LLM call consolidates everything learned into per-member `personality_notes`
2. The `bootstrap_graduated` flag is set on the member profile
3. The bootstrap prompt is removed from the system prompt
4. The agent continues with its evolved personality — no more guided discovery

**Graduation criteria:** display_name + agent_name + interaction_count threshold. The agent naming IS the graduation signal — no graduation without it.

## What Persists After Hatching

All per-member in `member_profiles`:
- **agent_name** — whatever the member chose
- **emoji** — identity marker discovered during onboarding
- **personality_notes** — consolidated from hatching observations
- **communication_style** — inferred from interaction patterns
- **display_name** — the member's name (from invite or discovered)
- All knowledge entries extracted during hatching conversations

## Design Philosophy

Competence first. The agent earns the right to be personal by being useful. It doesn't default to customer support energy. It arrives as a presence — warm, maybe a little dry, attentive, human — and lets the relationship develop naturally.
