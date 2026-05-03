# SPEC: Soul Architecture Revision + Bootstrap Fix (`SOUL-REVISION`)

**Priority:** Urgent. Fixes soul contamination bug from Pass 1 and revises the soul model based on owner direction.
**Depends on:** Multi-Member Integration Pass 1 (shipped)

## Intention

Pass 1 split the soul into instance-level identity (agent_name, emoji, personality_notes) shared across all members, plus per-member profiles (display_name, timezone, etc.). This was the wrong boundary.

The owner's direction: "Kernos" is the platform name, not the agent's name. The agent doesn't have an identity until a member hatches it. Each member should be able to have their own uniquely named agent with its own personality — or inherit an existing one. The hatching process must always include naming the agent. There is no fallback to "Kernos" as the agent identity.

This spec moves the soul from instance-level to per-member, adds a hatching mode setting, and fixes the bootstrap consolidation bug where every member graduation was overwriting shared personality_notes.

## The New Model

**Instance level (platform):**
- "Kernos" is the system/platform name. It appears in system messages, error responses, the invite code flow, and infrastructure contexts. It is never the agent's conversational identity.
- Instance-level settings: adapters, providers, member list, admin config.

**Per-member (the agent relationship):**
- Each member has their own soul: agent_name, emoji, personality_notes.
- Each member has their own profile: display_name, timezone, communication_style, interaction_count, bootstrap_graduated.
- The soul defines who the agent IS to that person. Member A's agent might be "Nova" with dry humor. Member B's might be "Pip" with warmth. Same platform, different agents.

**Agent name as relay handle:**
- The agent name is the agent identity for that member relationship — not an alias for the person. But it's still resolvable in the relay system. "Let Tom know X" resolves via Tom's display_name. "Let Tina know X" resolves via Tina being Tom's agent identity. Same destination, different handle.
- The cross-member messaging relay should resolve recipients by checking both display_name and agent_name in member records.

**Hatching mode (initial setup preference, stored in instance config):**

This is set once during the initial Kernos setup process — it's a setup-time preference established by the first member, not something that changes dynamically after the fact.

- **Unique hatching (default):** Each new member goes through the full hatching process — the agent introduces itself without a name, the member names it, early conversation shapes the personality. This is the same experience the first user had. Every member gets their own distinct agent identity.

- **Auto-inherit:** Each new member gets a copy of an existing soul (typically the first member's). Same agent name, same personality. The new member can modify their copy later if they want. This is faster onboarding for teams that want a consistent agent identity.

## What Needs to Change

### 1. Soul becomes per-member

Move soul storage from per-instance to per-member. The soul file path changes from `{data_dir}/{instance_id}/state/soul.json` to a per-member location. CC should decide the best storage approach — either a soul.json per member in the file system, or soul fields added to the member_profiles table in instance.db. The latter is probably cleaner since member_profiles already holds per-member state.

The key fields that move from instance-level to per-member:
- agent_name (was shared, now per-member)
- emoji (was shared, now per-member)
- personality_notes (was shared, now per-member)
- hatched / hatched_at (now per-member — each member's agent hatches independently in unique hatching mode)

### 2. Hatching mode setting

Add a hatching_mode setting at the instance level. Two values: "unique" (default) and "inherit". This controls what happens when a new member first messages after claiming their invite code.

- **unique:** The new member's soul starts empty. The full bootstrap prompt runs. The agent does not have a name yet and the hatching process includes the member naming it.
- **inherit:** The new member gets a copy of the first member's soul (or a designated "template" soul). Bootstrap still runs but it's lighter — the agent already has a name and personality, it just needs to build the relationship with this new person.

Store this setting wherever instance-level config lives (instance.db or the future config layer).

### 3. Bootstrap prompt update

The current bootstrap prompt says "Do not introduce yourself by name unless they clearly don't know who you are." In unique hatching mode, the agent literally doesn't have a name yet. The hatching process needs to naturally include the moment where the member names the agent.

Update the bootstrap prompt to make naming the agent a part of hatching — not a throwaway aside, but a real moment in the relationship. "What should I call you?" is already part of hatching. "What do you want to call me?" should be equally natural.

The member bootstrap prompt (for non-first members in unique hatching mode) should also include the naming step. The agent arrives without a name and the member gives it one.

In auto-inherit mode, the agent already has a name from the inherited soul — the bootstrap just focuses on building the relationship, and casually mentions they can rename it.

### 4. Fix: Consolidation only writes to the hatching member's soul

The current bug: `_consolidate_bootstrap(soul)` runs on every member graduation and overwrites a shared `personality_notes` field. With per-member souls, this problem is structurally eliminated — each member's consolidation writes to their own soul. But verify this is the case after the refactor.

Consolidation should read from the member's own profile fields (display_name, communication_style) rather than the deprecated soul-level fields.

### 5. Fix: Graduation criteria simplified

Current graduation requires four signals: display_name, at least one knowledge entry, communication_style, interaction_count above threshold. The communication_style and knowledge entry requirements create an invisible checklist that neither the user nor the agent can see.

New graduation criteria:
- display_name is set (known from invite or discovered during hatching)
- agent_name is set (the member has named the agent — this IS the hatching moment)
- interaction_count reaches the threshold

That's it. Knowledge entries and communication_style develop naturally through conversation and compaction. They shouldn't gate graduation.

### 6. "Kernos" as platform name only

Update any code where "Kernos" appears as the agent's default conversational identity. The Soul dataclass currently has `agent_name: str = "Kernos"`. Change the default to empty string. "Kernos" should only appear in system/infrastructure contexts:
- Invite code messages ("This is a private Kernos instance...")
- System error messages
- Platform identification
- The README and documentation

In conversation, if the agent doesn't have a name yet (pre-hatching), it should introduce itself without a name and let the naming happen naturally. It should not default to "Kernos."

### 7. Context assembly update

NOW block and STATE block currently read agent_name from the instance-level Soul. Update to read from the member's per-member soul. The agent's identity in context should reflect whatever this specific member named it.

### 8. Migration

Existing single-user instances have a soul.json with agent_name, emoji, personality_notes. On first boot after this change:
- Copy these fields into the owner's per-member soul
- The instance-level soul.json can remain for backward compat but is no longer read for agent identity at runtime
- If the owner had already named the agent something other than "Kernos," that name carries into their per-member soul

---

## What This Spec Does NOT Include

- Soul governance policy (the design review's spec — versioning, approval flows, mutation rules). That's a layer on top of this.
- Shared spaces (parked)
- Per-member capability connections (Pass 3)

---

## Acceptance Criteria

1. New member in unique hatching mode goes through full hatching including naming the agent. The agent does not default to "Kernos" as its name.
2. New member in auto-inherit mode gets the first member's agent name and personality. Bootstrap is lighter, focused on relationship.
3. Member A names their agent "Nova." Member B names theirs "Pip." Each sees their own agent name in all interactions.
4. Consolidation on member A's graduation does not affect member B's soul.
5. Graduation triggers on display_name + agent_name + interaction count. No communication_style or knowledge entry requirement.
6. "Kernos" only appears in system/platform contexts, never as the agent's conversational identity.
7. Existing single-user instance migrates cleanly — owner keeps their agent name and personality.
8. Admin can set hatching_mode to "inherit" and new members get a copy of the existing soul.
9. All existing tests pass.

---

## Embedded Live Test

1. **Fresh instance unique hatching** — First member messages. Agent arrives without a name. Through natural conversation, the member names it. Verify soul stores the chosen name.

2. **Second member unique hatching** — Second member messages in unique hatching mode. Goes through their own hatching. Names the agent something different. Verify both members have distinct agent names.

3. **Auto-inherit mode** — Set hatching_mode to "inherit." Third member messages. Gets the first member's agent name and personality. Verify the inherited soul matches.

4. **No Kernos default** — At no point during any hatching does the agent call itself "Kernos" unless the member chooses that name.

5. **Graduation** — Verify a member graduates after enough interactions + naming, without needing communication_style set.

**Output:** `data/diagnostics/live-tests/SOUL-REVISION-live-test.md`
