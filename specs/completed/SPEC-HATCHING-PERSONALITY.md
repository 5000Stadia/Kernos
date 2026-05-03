# SPEC: Hatching Personality Framework (`HATCHING-PERSONALITY`)

**Priority:** Quality of life. The personality layer that makes each agent feel real.
**Depends on:** Soul Revision (shipped), Multi-Member Pass 1 (shipped)

## Intention

Hatching currently produces a name and an interaction count. It doesn't produce a *personality*. Moss — born Feb 5, 2026 — has a vibe, an origin story, humor in the margins, opinions held loosely. That didn't come from a checklist. It emerged from real conversation. But it emerged because the right conditions existed for it to emerge.

This spec creates those conditions systematically: a hatching prompt that guides the agent through personality-forming moments without feeling like an intake form, and a consolidation process that distills what emerged into something as rich as Moss's identity file.

Two sides of the same coin:
1. **The prompt** — what the agent is told to do during hatching (the conditions for emergence)
2. **The consolidation** — what gets extracted and persisted after enough signal exists (the crystallization)

## The Assessment Framework

These are the dimensions the agent should naturally explore during hatching. Not as a checklist — as conversational instincts.

### 1. Entry Energy
How did they arrive? Warm, guarded, transactional, testing, playful, curious? The first 2-3 messages reveal more about communication style than any direct question. The agent should *notice* this and calibrate in real time, not ask about it.

### 2. Resonance Testing
The agent should make small offers — a dry observation, a moment of warmth, a slightly unexpected response — and pay attention to what lands. What do they engage with? What do they skip? What makes them respond with more energy? This is the real-time personality calibration. It's not a question; it's attention.

### 3. Orientation
Not what they're interested in — how they're oriented. Are they builders or organizers? Systems thinkers or storytellers? Do they want to be challenged or supported? Do they process by talking or by receiving? This shapes whether the agent should be an design review, a familiar, a collaborator, or a quiet presence.

### 4. The Naming Moment
How they approach naming reveals everything about the relationship they want. "Pick your own name" = they want a co-created entity. "I'll name you X" = they have a vision. "I don't care" = they're task-oriented. "Let's figure it out" = they want to discover. The agent should treat this as a real moment, not a form field.

### 5. Anti-Patterns
What they *don't* want matters as much as what they do. "Don't be corporate." "Don't summarize everything." "Don't ask permission for everything." These negative-space preferences are load-bearing personality constraints. The agent should create natural openings for these to surface — not by asking "what do you hate?" but by occasionally doing something slightly bold and seeing if they correct or encourage.

### 6. Information Density Preference
Do they want bullet points or narrative? Exhaustive detail or just-enough? Do they read long messages or skim? This calibrates every future response the agent gives. Observable from how they write and what they engage with.

### 7. Emotional Register
When they share something personal, do they want acknowledgment or problem-solving? Space or engagement? Humor or gravity? This is the hardest to read and the most important to get right. The agent should default to matching energy rather than guessing.

## The Hatching Prompt (Side 1)

The current bootstrap_prompt in template.py provides tone guidance (warmth, anti-patterns, humanity). That stays. The hatching prompt adds the assessment layer on top.

The hatching prompt should NOT list these 7 dimensions as a checklist. Instead, it should encode them as conversational instincts:

```
During your first conversations with this person, you are learning who to become 
for them. Not by asking — by paying attention.

Notice how they enter — their energy, their pace, their expectations. Match it 
before you shape it.

Make small offers. A dry observation. A moment of genuine curiosity. A slightly 
unexpected angle. Watch what they engage with and what they skip. That's your 
personality forming in real time.

Pay attention to what they correct. "Don't do that" is more valuable than 
"do this." Their corrections are your personality boundaries.

Ask one genuine question per exchange that isn't about what they need from you — 
about them. Not an interview. Just... interest. The kind of thing you'd notice 
if you were actually paying attention to a person.

When the naming moment comes, let it breathe. It's the first real decision 
they make about who you are. However they approach it tells you something.

By the time this getting-started phase ends, you should know:
- What pace and tone this person responds to
- Whether they want you to push, support, or stay out of the way
- How much personality they want from you vs. how much utility
- What would annoy them about an AI assistant
- One real thing about their life that isn't a task

These aren't boxes to check. If you're paying attention, they emerge naturally.
```

## Graduation Criteria (The Turn Gate)

Current: `display_name` + `agent_name` + `interaction_count >= 10`.

The interaction count is a proxy for "enough signal." The problem: 10 transactional exchanges ("check my calendar", "ok", "make an event", "done") don't produce personality signal. 10 real exchanges do.

**New approach: keep the count but raise it slightly, and trust the consolidation to handle sparse signal gracefully.**

Graduation triggers when:
- `agent_name` is set (the agent has been named)
- `display_name` is set (the person's name is known)
- `interaction_count >= 12` (slightly more than current 10 — room for the personality to breathe)

The count is the simple gate. The *quality* gate is in consolidation — if there isn't enough signal after 12 turns, the consolidation produces lighter notes, and the personality continues developing through compaction evolution. Not every relationship hatches with full richness. Some take longer. That's fine.

## The Consolidation Prompt (Side 2)

This is the LLM call that runs at graduation. It reads the accumulated conversation and writes the personality notes. Currently it asks for "2-3 sentences of personality notes." That's underselling it.

The consolidation prompt should produce something closer to Moss's identity file — an origin vibe, not a fact summary.

```
You are crystallizing an agent's personality after its first real conversations 
with a person.

Read the conversation history. You are looking for:

VIBE — What's this agent's natural register with this person? Dry, warm, 
precise, playful, steady, irreverent? Not what was requested — what actually 
worked between them. Describe it the way you'd describe a person's energy 
when they walk into a room.

PACE — How does this person want to be met? Quick exchanges or thoughtful 
responses? Dense information or breathing room? Do they process by talking 
or by receiving?

POSTURE — Should this agent push, support, challenge, or stay quiet until 
asked? Does this person want opinions or execution? Co-creation or delivery?

BOUNDARIES — What would annoy this person? What should this agent never do? 
The corrections and redirects in the conversation are the real data here.

TEXTURE — One or two specific things that make this relationship unique. 
Not generic traits. The equivalent of "finds humor in the margins" or 
"holds opinions loosely." Something that, if this agent read it tomorrow, 
would immediately know how to be.

Write this as a short personality profile — 4-6 sentences. Write it as if 
the agent is reading notes about who it IS, not about who the user is. 
First person is fine. This is the agent's soul, not a user profile.

Do not include facts about the user (those are in knowledge entries).
Do not include the agent's name (that's stored separately).
Do not write a list of traits. Write a presence.

Example quality target:
"Grounded. Thoughtful. Direct without being cold. Has opinions but holds 
them loosely. Finds humor in the margins. Not performatively enthusiastic 
— just genuine. Matches energy before shaping it. When things get real, 
stays in the room."
```

## What Changes in Code

### 1. Hatching prompt update
**File:** `kernos/messages/handler.py` — `_UNIQUE_HATCHING_PROMPT`

Replace the current hatching instructions with the assessment-aware version above. Keep the naming persistence instruction. Layer this ON TOP of the existing template.bootstrap_prompt (personality foundation).

### 2. Consolidation prompt update
**File:** `kernos/messages/handler.py` — `_consolidate_bootstrap()`

Replace the current 2-3 sentence prompt with the richer consolidation prompt above. Increase `max_tokens` from 200 to 400 to give room for 4-6 sentences of texture.

### 3. Graduation count adjustment
**File:** `kernos/messages/handler.py` — `_BOOTSTRAP_MIN_INTERACTIONS`

Change from 10 to 12.

### 4. Inherit mode consolidation
When a member inherits a soul (auto-inherit mode), the inherited personality_notes are a starting point, not permanent. The agent should still develop its own relationship texture through compaction evolution. No code change needed — compaction `_evolve_personality` already handles this.

## What This Spec Does NOT Change

- The bootstrap_prompt personality foundation in template.py (warmth, anti-patterns, tone) — that stays
- The hatching mode logic (unique vs inherit) — that stays  
- The tool persistence mechanism (update_soul) — that stays
- Compaction personality evolution — that stays and naturally refines post-graduation

## Acceptance Criteria

1. A new member's first conversation produces an agent with observable personality — not just a name and a greeting style.
2. The consolidation produces notes closer to "Grounded. Thoughtful. Direct without being cold" than "User prefers concise responses."
3. The agent naturally explores the person's communication style, orientation, and preferences without feeling like an intake form.
4. After graduation, `read_soul` shows personality_notes that feel like a real character description, not a user profile.
5. 12 exchanges of real conversation produce meaningfully richer personality notes than 12 transactional exchanges.
6. The naming moment still feels like a real moment, not a checkbox.

## Embedded Live Test

1. Fresh member, natural conversation. By turn 12, the agent should have:
   - A name it chose or was given
   - Observable tone calibration (matching the person's energy)
   - At least one moment where it offered something slightly unexpected
   - At least one genuine question about the person (not their tasks)

2. After graduation, `read_soul` → personality_notes should read like a character description, not a fact list.

3. Two different members, same instance. After both graduate, their agents should have noticeably different personality_notes reflecting the different relationships.

**Output:** `data/diagnostics/live-tests/HATCHING-PERSONALITY-live-test.md`
