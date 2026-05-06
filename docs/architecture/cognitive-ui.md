# Cognitive UI Grammar

> The system prompt as a typed document with named zones. A cacheable static prefix. Provenance-tagged knowledge fragments. Selective zone refresh without rebuilding the prompt.

## The problem

An agent's "system prompt" in most frameworks is a long string. The user's identity is in there somewhere; the operating principles are in there somewhere; the current time, the active tools, the recent conversation, the long-term memory, the active behavioral rules are all there too — concatenated, often in ad-hoc order, often re-rendered every turn.

Three consequences follow from this shape:

1. **The model reads the whole thing every turn.** No amount of the prompt is stable across turns, so prompt caching (where the provider charges less for a reused prefix) doesn't help.
2. **The agent cannot tell where information came from.** A fact in the prompt is just text; the model has no structural cue that "this came from memory harvested two weeks ago" versus "this was said in the current message."
3. **Updates are destructive.** Refreshing any piece — the current time, the active space, a whispered notice — means regenerating the whole prompt. There's no selective update path because there's no structure to update.

The framework-level fix is usually *"put the prompt in a better order and add some section headers."* That's a cosmetic fix. The structural fix is to treat the system prompt as a typed document with named zones, each owned by a specific builder, each refreshable on its own terms.

Kernos does the structural fix. The agent's context is assembled from seven named zones, composed in a stable order, with a deliberate split between the parts that stay the same across turns and the parts that change every turn.

## The zones

Eight zones in the production compose, in fixed order:

```
RULES               operating principles, instance stewardship, covenants, bootstrap
ACTIONS             capabilities, outbound channels, references hint
NOW                 turn-local situation: time, platform, auth, space, member
STATE               current truth the agent should act from
RESULTS             receipts, system events, awareness whispers, cross-domain notices
PROCEDURES          domain-specific workflows loaded from _procedures.md
AVAILABLE CANVASES  CANVAS-V1 — passive awareness of canvases the member can see
MEMORY              compaction context: Living State, archived history index
```

Each zone is built by a dedicated Python function that produces one well-formed string. Builders live in `kernos/messages/handler.py`:

| Zone | Builder | What it contains |
|---|---|---|
| `RULES` | `_build_rules_block` | Operating principles, instance stewardship purpose, active covenants, pre-graduation bootstrap prompt |
| `ACTIONS` | `_build_actions_block` | Active capability prompt, outbound channel inventory, references hint pointing at `request_reference` |
| `NOW` | `_build_now_block` | Current time (in user TZ + UTC), platform context, auth level, member identity |
| `STATE` | `_build_state_block` | Member identity, active space, relationships, knowledge fragments |
| `RESULTS` | `_build_results_block` | Tool receipts, system events, whispers, cross-domain notices |
| `PROCEDURES` | `_build_procedures_block` | Domain-specific workflows loaded from the active space's `_procedures.md` plus team-canvas pages |
| `AVAILABLE CANVASES` | `_build_canvases_block` | CANVAS-V1 member-scoped canvas index — names + scopes + recent-update hints |
| `MEMORY` | `_build_memory_block` | The space's Living State + archived history index |

The production compose is in `MessageHandler._phase_assemble` — it threads through all eight blocks plus a `_compose_blocks(*blocks)` helper that joins non-empty blocks with double newlines.

There's also a `_build_system_prompt(...)` compatibility wrapper at module scope used by tests; it composes a six-block subset (RULES, ACTIONS, NOW, STATE, RESULTS, MEMORY) and is intentionally narrower than the production path. Production code uses the phase-based block builders; the wrapper exists so older direct-call tests still pass.

The discipline: **no zone contains content that belongs in another zone.** The Living State lives in `MEMORY`, not spread through `RULES`. The active covenants live in `RULES`, not in `STATE`. The agent reads a zone and knows what kind of information it's looking at.

## The static/dynamic split

Not every zone changes every turn. `RULES` and `ACTIONS` are stable across turns — the operating principles, the active capabilities, the outbound channels, the covenant set. These don't change because the user sent one message.

Everything else does change:

- `NOW` — the time advanced; possibly the platform or the member changed
- `STATE` — the active space may have changed; recent knowledge may have been loaded
- `RESULTS` — receipts, system events, and whispers specific to this turn
- `PROCEDURES` — loaded per-space; may differ from the last turn's space
- `MEMORY` — may include a freshly-refreshed Living State after compaction

The handler separates these explicitly:

```python
# kernos/messages/handler.py:141-142
system_prompt_static: str = ""   # Cacheable prefix (RULES + ACTIONS)
system_prompt_dynamic: str = ""  # Fresh each turn (NOW + STATE + RESULTS + MEMORY)
```

And in the turn pipeline, the cache-boundary comment is explicit:

```python
# kernos/messages/handler.py:6295-6296
# Cache boundary: static prefix (RULES + ACTIONS) is stable across turns,
# dynamic suffix (NOW + STATE + RESULTS + PROCEDURES + MEMORY) changes every turn.
```

The payoff is real. With Anthropic's prompt caching, the stable prefix stays cached across turns — the model provider charges the reduced rate for the reused prefix and only re-reads the dynamic suffix. For a user in a long-running conversation, this is the difference between paying full-prompt cost every turn and paying only for what changed.

Just as important: the static/dynamic split makes it *structurally possible* to update the dynamic zones without rebuilding anything static. A whisper arriving doesn't touch `RULES`; a space switch doesn't touch `ACTIONS`; the agent's turn-level context can be rebuilt cheaply because the expensive parts aren't in scope.

## Provenance on knowledge fragments

Inside the `STATE` zone, knowledge fragments carry provenance — the model sees not just the fact but a hint about where it came from:

```python
# kernos/messages/handler.py:1531-1536
sens = getattr(e, "sensitivity", "") or ""
# ...
fragment = _format_knowledge_fragment(
    content=e.content,
    sensitivity=sens,
    # ... additional provenance fields
)
```

A fragment includes, at a minimum, its sensitivity hint (`open` / `contextual` / `personal`) and enough surrounding formatting that the agent can reason about how to handle it. A `personal`-classified fact in the STATE zone tells the agent *"this is something the user has told me that they'd want me to treat carefully."*

The provenance isn't invasive. The agent doesn't read a giant metadata table per fact. It reads the fact with enough structural cues that the difference between *"the user said this in this conversation"* and *"this was harvested from a compaction two months ago"* is available when relevant.

## The disclosure gate: a final filter

Before a knowledge fragment lands in `STATE`, a disclosure gate runs — the last chance for the system to redact or elide a fragment based on who the current member is and what the cross-member disclosure rules permit:

```python
# kernos/messages/handler.py:5987
# DISCLOSURE-GATE: final read-time filter before knowledge reaches STATE.
```

The gate filters facts that belong to another member but aren't supposed to appear in the current member's context. This is what makes the per-member privacy model (see [Multi-member disclosure layering](disclosure-and-messenger.md)) durable end-to-end: even if the wrong fact were queried, the disclosure gate would strip it before it reached the prompt the agent reads.

## Selective zone refresh

Because each zone is its own builder consuming its own inputs, refreshing one zone doesn't require touching the others:

- A whisper arriving: `_build_results_block` runs; `RESULTS` is replaced; nothing else moves.
- A space switch: `_build_state_block` runs with the new space's member/relationship/knowledge; `_build_memory_block` runs with the new space's Living State; `_build_procedures_block` runs with the new space's procedures. `RULES`, `ACTIONS`, `NOW` stay as-is.
- A compaction: `_build_memory_block` runs with the refreshed Living State. Nothing else changes.

This is not a performance optimization — it's a clarity property. The agent's context for the next turn is produced by composing the zones that need rebuilding. What doesn't need to rebuild, doesn't rebuild. The diff between turn N and turn N+1 is legible.

## Why this isn't just "sectioned prompts"

The obvious critique: *"every framework has section headers. So what?"*

Three things distinguish a typed zone model from a sectioned prompt:

**1. Zones have owners.** Each zone is built by one Python function that is the single source of truth for what goes in it. This is enforceable and auditable — you cannot grep for an active covenant and find it appearing in `STATE`, because `STATE`'s builder does not touch covenants.

**2. Zones have contracts.** `RULES` is for rules, not for transient state. `NOW` is turn-local. `MEMORY` is compaction-scoped. The placement of a piece of content is a semantic statement about its retention and refresh profile. Violating the contract breaks something downstream — a fact put in `NOW` won't get compacted; a rule put in `RESULTS` won't persist across turns.

**3. Zones enable the cache boundary.** The split between static and dynamic is only possible because the static zones are fully self-contained — they don't reach into turn-local state. A free-form "sectioned prompt" can't do this because the section that's supposed to be stable keeps accidentally including something that changes every turn.

A sectioned prompt has headers; it doesn't have these properties.

## What this architecture makes easy

- **Affordable long-running conversations.** The cache boundary means a user in their 500th turn isn't paying full-prompt cost each time — the static prefix rides cached.
- **Legible context.** When the agent behaves unexpectedly, the question *"what did the agent actually see?"* is tractable. The zones are named, the builders are traceable, and the content of each zone at any turn is reconstructable.
- **Per-turn updates without rebuilds.** A whisper arrives; the `RESULTS` block gets it. A receipt from a tool call; same place. The agent reads the next turn with the update in scope.
- **Provenance discipline.** Knowledge fragments carry where they came from into the context the agent reads. The agent can distinguish a just-said fact from a year-old fact.
- **Disclosure at the prompt boundary.** The same gate that filters cross-member facts can be inserted in the zone pipeline without touching anything else. The `STATE` builder is where the filter lives; everything downstream is untouched.

## What this architecture explicitly does not try to do

- **It does not hide the prompt from the agent.** The zones are named in the prompt itself (`## RULES`, `## STATE`, etc.); the agent reads them and knows the structure. This is deliberate — the agent's ability to reason about the structure of its own context is a feature.
- **It does not prevent a badly-written builder from leaking turn-local content into a stable zone.** If `_build_rules_block` starts referencing `datetime.now()`, the cache boundary breaks. The discipline is enforced by convention and code review, not by the framework.
- **It does not replace prompt iteration.** The zones are containers; the content in the containers is still authored by humans and iterated on. A confused agent is usually a signal to iterate the prompt of a zone, not to invent a new zone.
- **It does not pretend the agent perfectly uses every zone.** A fact in `STATE` may still be overlooked by the agent in a given turn. The zones make the content visible and legible; they don't guarantee perfect retrieval. That's a prompt-iteration problem, not a container-shape problem.

## Related architecture

- **[Memory](memory.md)** — the `MEMORY` zone consumes the compaction service's Living State + archive index
- **[Context spaces](context-spaces.md)** — the space boundary is what determines which `PROCEDURES`, which `STATE` knowledge, which `MEMORY` view the agent sees
- **[Multi-member disclosure layering](disclosure-and-messenger.md)** — the disclosure gate between knowledge store and `STATE` zone

## Code entry points

- `kernos/messages/handler.py:505` — `_build_rules_block`
- `kernos/messages/handler.py:548` — `_build_now_block`
- `kernos/messages/handler.py:634` — `_build_state_block`
- `kernos/messages/handler.py:695` — `_build_results_block`
- `kernos/messages/handler.py:704` — `_build_actions_block`
- `kernos/messages/handler.py:733` — `_build_memory_block`
- `kernos/messages/handler.py:743` — `_build_procedures_block`
- `kernos/messages/handler.py:750` — `_compose_blocks`; the fixed composition order
- `kernos/messages/handler.py:141-142` — the static/dynamic prompt split on `TurnContext`
- `kernos/messages/handler.py:6295-6296` — the cache-boundary comment in the turn pipeline
- `kernos/messages/handler.py:5987` — the disclosure-gate filter before knowledge reaches `STATE`
- `kernos/messages/handler.py:1531-1536` — provenance on knowledge fragments
