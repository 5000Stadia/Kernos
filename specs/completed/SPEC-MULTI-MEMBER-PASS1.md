# SPEC: Multi-Member Integration Pass 1 (`MULTI-MEMBER-PASS1`)

**Kit review:** Approved across multiple rounds (2026-04-13).
**Design document:** https://www.notion.so/341ffafef4db81228c06e1c449f5a917
**Complete system description:** https://www.notion.so/341ffafef4db818c8f57ca4dd7fb327f
**Blocks:** Cross-Member Messaging, all subsequent multi-member work.

## Intention

The multi-member infrastructure exists — instance.db has members, invite codes, channel mappings. The handler resolves every incoming message to a member_id. But the reasoning pipeline is blind to who it's talking to. The Soul is instance-level with single-user fields. The NOW block has no sender identity. Knowledge queries are unscoped. Bootstrap runs once for the instance. Covenants are instance-scoped. Conversation logs and compaction are keyed to (instance, space) with no member dimension.

The concrete failure: Sarah gets invited with display_name="sarah", connects via Telegram, and Kernos asks "what's your name?" — because her identity never reaches the reasoning context.

This spec makes every member a complete, independent Kernos experience on the same instance. Same personality, different person, different context.

---

## The Model

Each member is a first-class person, not subordinate to the owner. The initial user is just the first member who bootstrapped the instance. Every member gets their own conversation threads, context spaces, knowledge, covenants, connected services, and bootstrap graduation.

Kernos has one core identity (the Soul) shared across all members — same name, same personality, same spirit. But the relationship with each member is independent.

---

## What Needs to Change

### 1. Split the Soul

The Soul currently holds both instance-level identity AND per-user relationship fields. These need to separate.

**Instance-level (stays on Soul):** agent_name, emoji, personality_notes, hatched, hatched_at. These define who Kernos IS — shared across all members.

**Per-member (moves to a member profile in instance.db):** display_name (was user_name), timezone, communication_style, interaction_count, bootstrap_graduated, bootstrap_graduated_at. These define the relationship with a specific person.

Create a member_profiles table in instance.db to hold these fields. When a member is created via invite code, seed their profile with the display_name from the invite. When the owner exists from pre-multi-member, migrate their fields from the Soul into their member profile.

Keep the deprecated fields on the Soul dataclass for JSON deserialization compatibility, but stop reading or writing them at runtime.

### 2. Member Identity in Context

The agent needs to know who it's talking to every turn.

**NOW block:** Add a line identifying the current member — their display name and role. The handler already resolves member_id; look up the profile from instance.db and pass it through to context assembly.

**STATE block:** Currently shows the Soul's user_name and the owner's knowledge. Change it to show the current member's display name, communication style, and knowledge entries. Each member sees their own context, not the owner's.

**TurnContext:** Already has member_id. Add the member profile so it's available throughout the pipeline without repeated lookups.

### 3. Per-Member Conversation Threads

Conversation logs are currently keyed to (instance_id, space_id). Two members talking in the General space share one thread — which means Sarah sees Bob's messages in her context window.

Add member_id as a dimension to conversation storage. Each member gets their own conversation thread per space.

For migration: existing logs belong to the owner. When the owner's member_id is resolved, read from the legacy path if the member-scoped path doesn't exist yet. New writes always go to the member-scoped path. Lazy migration — old data moves naturally on first compaction.

### 4. Per-Member Compaction

Compaction follows the conversation threads. Since each member now has their own thread per space, compaction state (Living State, ledger, archive documents) also needs the member dimension. Same compaction engine, same logic, just scoped to (instance, space, member).

Same lazy migration approach as conversation logs.

### 5. Basic Knowledge Visibility

Knowledge entries have existing fields that were never enforced: owner_member_id, sensitivity, visible_to. This pass activates basic enforcement.

**On write:** When fact_harvest creates a knowledge entry, tag it with the current member's member_id as owner_member_id.

**On read:** When querying knowledge for context assembly, filter by member. Entries with no owner (legacy data, instance-level) remain visible to everyone. Entries owned by the current member are visible. Entries owned by other members are not visible.

This is the minimum viable boundary. Pass 2 will add sensitivity classification and stewardship refinement.

### 6. Per-Member Covenants

Covenants are currently instance-scoped (with space sub-scoping). Each member needs their own set.

Add a member_id dimension to covenant storage. Empty member_id means instance-level (the spirit covenant stays shared). Non-empty means it belongs to that member.

When a new member is created, copy the default covenant rules into their member-scoped set. Each member starts with the same defaults but can modify their own without affecting anyone else.

Covenant selective injection should use the current member's covenants.

### 7. Member-Specific Bootstrap

Each member has their own bootstrap lifecycle, tracked in their member profile.

**If display_name is known from the invite:** The agent must NOT ask for the member's name. The member bootstrap prompt should make this explicit.

**Member bootstrap prompt:** Write a lighter variant of the existing bootstrap prompt for non-first members. The instance personality is already established. This version focuses on building a relationship with the specific person — learning their timezone, preferences, what they need. Not re-hatching the personality.

**Instance hatching vs member bootstrap:** The Soul's hatched/hatched_at tracks whether the instance completed its initial personality formation (happens with the first member). Subsequent members skip instance hatching and go through their own lighter bootstrap.

**Graduation:** Same maturity criteria, evaluated per-member independently.

### 8. Per-Member Spaces

When a new member first messages Kernos, ensure they have a default General space. Each member's space hierarchy is independent — Sarah's domains are hers, Bob's are his.

The router should route within the current member's space hierarchy using the member_id from TurnContext.

### 9. Operating Principles Language

Update template language that says "the user" or "this person" in singular terms. The agent serves multiple people. Language should be naturally member-aware.

### 10. Per-Member Timezone

Timezone discovery currently writes to the Soul. Move it to the member profile. Each member can be in a different timezone.

Resolution chain: member profile timezone → Soul timezone (instance default) → system local.

### 11. Per-Member Interaction Count

Move from Soul to member profile. Each member's count tracks their own interactions independently. Feeds into bootstrap graduation.

---

## Migration Strategy

This change affects stored state. Must be safe for existing single-user instances.

On first boot after this change: if the Soul has non-empty per-user fields, copy them into the owner's member profile in instance.db. The Soul's deprecated fields remain for backward compat but are never read at runtime.

Conversation logs and compaction state: lazy migration. Read from legacy path if member-scoped path doesn't exist. Write to member-scoped path.

---

## What This Spec Does NOT Include

- **Shared spaces** — parked for post-V1
- **Cross-member messaging** — next spec, depends on this one
- **Knowledge sensitivity classification** — Pass 2
- **Per-member capability connections** — Pass 3
- **Role-based tool restrictions** — future
- **Stewardship enforcement** — Pass 2

---

## Acceptance Criteria

1. Owner's existing experience is unchanged after migration.
2. New member invited with display_name gets greeted by name — no "what's your name?"
3. NOW block identifies who the agent is talking to.
4. STATE block shows the current member's name and knowledge, not the owner's.
5. Knowledge created by member A is not visible to member B.
6. Legacy knowledge (no owner tag) remains visible to all members.
7. Each member has their own conversation thread — no cross-member bleed.
8. Each member has their own compaction state.
9. Each member has their own bootstrap graduation.
10. Each member has their own covenants — changes by one don't affect another.
11. Soul identity fields (agent_name, emoji, personality_notes) remain shared.
12. Timezone is per-member.
13. All existing tests pass.

---

## Embedded Live Test

**Prerequisites:** Owner on Discord, TestUser on Telegram.

1. **Member identity** — TestUser: "Hey, what's up?" → Greeted by name. No name question.
2. **Owner unchanged** — Owner: "What's my name?" → Correct name. Context unaffected.
3. **Knowledge isolation** — Owner says favorite color is blue. TestUser asks theirs. → Kernos doesn't know TestUser's. Doesn't say blue.
4. **Per-member timezone** — TestUser asks the time. → Uses TestUser's timezone, not owner's.
5. **Conversation isolation** — Owner discusses topic X. TestUser messages. → Zero reference to topic X.
6. **Per-member covenants** — TestUser requests concise responses. Owner asks complex question. → Owner still gets normal depth.
7. **Bootstrap independence** — TestUser's interaction count increments independently.

**Output:** `data/diagnostics/live-tests/MULTI-MEMBER-PASS1-live-test.md`
