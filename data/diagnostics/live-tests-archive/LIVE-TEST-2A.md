# Live Test: SPEC-2A — Entity Resolution + Fact Deduplication

**Tenant ID (copy-paste):** `discord_000000000000000000`

**Date tested:** 2026-03-08

**Prerequisites:**
- KERNOS running on Discord
- `VOYAGE_API_KEY` set in `.env` (enhanced path active)
- Bot is live (start with `./start.sh` or equivalent)

**Check enhanced path is active:**
```bash
source .venv/bin/activate
python -c "import os; from dotenv import load_dotenv; load_dotenv(); key = os.getenv('VOYAGE_API_KEY',''); print('Enhanced path: ACTIVE' if key else 'Enhanced path: INACTIVE (legacy hash-only)')"
```
**Result:** Enhanced path ACTIVE

---

## Step-by-Step Test Table

### Phase 1: Entity creation and alias resolution

**Step 1:** Send in Discord:
```
I need to call back two clients later at around 5:00 David Finchman 782-39-2328 and Tina Muchacha 328-583-5324
```
Expected: Agent responds naturally, confirms both names and numbers.

**Result:** PASS
```
Got it — two callbacks around 5:00:
David Finchman — 782-39-2328
Tina Muchacha — 328-583-5324
```

**Step 2:** Check entities:
```bash
./kernos-cli entities discord_000000000000000000
```
Expected: David Finchman and Tina Muchacha appear with phone numbers and relationship_type: client.

**Result:** PASS — entities appeared after the NEXT message (expected; Tier 2 is async background task)
```
David Finchman (person) — client
  id: ent_cf14d913
  phone: 782-39-2328
  first seen: 2026-03-07  last seen: 2026-03-07

Tina Muchacha (person) — client
  id: ent_1f0482f5
  phone: 328-583-5324
  first seen: 2026-03-07  last seen: 2026-03-07
```

**Timing note:** Entities created by Tier 2 (background task) show up after the NEXT message from the user. This is expected behavior — Tier 2 fires as `asyncio.create_task()` after the response is assembled.

---

### Phase 2: Contact info as deterministic resolution signal

Covered by Phase 1 above. Phone numbers extracted and stored on EntityNode. Tier 1 resolution uses contact match as a deterministic signal — same phone number → same entity regardless of name variation.

---

### Phase 3: Vague references do NOT create entities

**Step 3:** Send in Discord:
```
My friend told me about a great Italian restaurant near downtown
```
Expected: Agent responds normally. No entity created for "friend."

**Step 4:** Check entities:
```bash
./kernos-cli entities discord_000000000000000000
```
Expected: Entity count UNCHANGED. No EntityNode for "friend." Restaurant may appear as a KnowledgeEntry.

**Result:** PASS — no new entity for "friend." No restaurant entry appeared (below salience threshold).

---

### Phase 4: Unique relationship roles DO create entities

**Step 5:** Send in Discord:
```
My wife loves Italian food, she would enjoy that place
```
Expected: Agent responds naturally. May ask for wife's name.

**Result:** PASS — agent responded and asked for name:
```
Good to know — Potatoland could be a date night spot. What's her name?
```

**Step 6:** Send in Discord:
```
My wife's name is Liana
```
Expected: EntityNode created for wife once the name is provided.

**Step 7:** Check entities:
```bash
./kernos-cli entities discord_000000000000000000
```
Expected: EntityNode for wife with relationship_type: spouse.

**Result:** PASS — entity appeared after next message:
```
user's wife (person) — spouse
  id: ent_cb0bed64
  first seen: 2026-03-08  last seen: 2026-03-08
```

**Known gap:** "user's wife" and "Liana" resolved as two separate entities (no relationship-role → name linkage). Deferred to Phase 2C.
```
Liana (person) — spouse
  id: ent_decd315d
  first seen: 2026-03-08  last seen: 2026-03-08
```

---

### Phase 5: Fact deduplication — NOOP reinforcement

**Step 8:** Send in Discord:
```
I'm working with Sarah Henderson on a contract, she's a client
```
Expected: Agent responds naturally.

**Step 9:** Check knowledge:
```bash
./kernos-cli knowledge discord_000000000000000000 --subject Henderson
```
Expected: No new duplicate entry. Existing entry's reinforcement_count may be higher. Knowledge store does NOT grow from semantic duplicates.

**Result:** PASS — reinforcement events confirmed:
```
[2026-03-08T00:23:31] knowledge.reinforced — entry: know_1772929247278842_4b61, count: 2
[2026-03-08T00:26:15] knowledge.reinforced — entry: know_1772929435329858_d44b, count: 2
[2026-03-08T00:26:18] knowledge.reinforced — entry: know_1772929435329858_d44b, count: 3
```

---

### Phase 6: Fact UPDATE — supersedes chain

**Step 10:** Send in Discord:
```
Actually she moved her practice to Seattle last month
```
Expected: Agent acknowledges the update.

**Result:** PASS — agent acknowledged the correction.

**Step 11:** Check knowledge with archived entries:
```bash
./kernos-cli knowledge discord_000000000000000000 --include-archived
```
Expected: Previous location entry archived. New Seattle entry shows with correct subject.

**Result:** PASS
```
[high] fact: "Moved her practice to Seattle last month" (2026-03-08)
  subject: Sarah Henderson | archetype: structural | R: 1.00 | salience: 0.50
```

---

### Phase 7: Present, don't presume (name collision)

**Step 12:** Send in Discord:
```
I met this girl named Sarah today, she seems really cool — not Henderson, different person
```
Expected: Agent does NOT auto-merge with Sarah Henderson. Treats as new person.

**Result:** PASS
```
Nice — two Sarahs in the mix now. What's this one's last name, or is she just "cool Sarah" for now?
```

**Step 13:** Check entities:
```bash
./kernos-cli entities discord_000000000000000000
```
Expected: Sarah Henderson is UNCHANGED. A MAYBE_SAME_AS edge or a second entity exists.

**Result:** PARTIAL PASS — the resolver correctly did not merge. However, no new entity was created for "cool Sarah" either (the user didn't confirm a last name or unique contact info, so the resolver had insufficient signals to create a new EntityNode). Sarah Henderson record was not touched.

---

### Phase 8: Classification logging verification

**Step 14:** Check entity creation events:
```bash
source .venv/bin/activate && python -m kernos.cli events discord_000000000000000000 --type entity.created --limit 10
```
Expected: entity.created events with resolution_type: new_entity for each created entity.

**Result:** PASS
```
[2026-03-07T22:05:56] entity.created — name: Sarah Henderson, resolution_type: new_entity, entity_id: ent_36038b5f
[2026-03-07T22:33:17] entity.created — name: Greg, resolution_type: new_entity, entity_id: ent_811b3b13
[2026-03-07T22:36:08] entity.created — name: David Finchman, resolution_type: new_entity, entity_id: ent_cf14d913
[2026-03-07T22:36:08] entity.created — name: Tina Muchacha, resolution_type: new_entity, entity_id: ent_1f0482f5
[2026-03-08T00:26:12] entity.created — name: user's wife, resolution_type: new_entity, entity_id: ent_cb0bed64
[2026-03-08T00:27:24] entity.created — name: Liana, resolution_type: new_entity, entity_id: ent_decd315d
```

**Step 15:** Check reinforcement events:
```bash
source .venv/bin/activate && python -m kernos.cli events discord_000000000000000000 --type knowledge.reinforced --limit 10
```
Expected: knowledge.reinforced events with reinforcement_count values.

**Result:** PASS — 7 reinforcement events recorded across session.

---

## Quick Reference Commands

```bash
# View all entities
./kernos-cli entities discord_000000000000000000

# View entities including inactive
./kernos-cli entities discord_000000000000000000 --include-inactive

# View knowledge about Henderson
./kernos-cli knowledge discord_000000000000000000 --subject Henderson

# View all knowledge including archived (limit 50)
./kernos-cli knowledge discord_000000000000000000 --include-archived

# View entity events
./kernos-cli events discord_000000000000000000 --type entity.created
./kernos-cli events discord_000000000000000000 --type entity.merged
./kernos-cli events discord_000000000000000000 --type entity.linked

# View reinforcement events
./kernos-cli events discord_000000000000000000 --type knowledge.reinforced
```

---

## Findings and Issues Surfaced During Testing

### Finding 1: Knowledge CLI limit bug (FIXED during session)

**Symptom:** `./kernos-cli knowledge` showed exactly 20 entries from the prior session — nothing from the current session appeared.

**Root cause:** `query_knowledge()` defaults to `limit=20`. CLI called it without specifying a limit. Today's entries (positions 21+) were silently cut off.

**Fix applied:** Added `--limit` argument (default 50) to `cmd_knowledge()`. Added newest-first sort so recent entries always appear at the top.

### Finding 2: `[high]` confidence label (FIXED during session)

**Symptom:** Knowledge entries displayed `[high]` confidence instead of valid values (`stated`/`inferred`/`observed`).

**Root cause:** LLM returning `confidence: "high"` — EXTRACTION_SCHEMA had no enum constraint on the confidence field.

**Fix applied:** Added `_normalize_confidence()` function mapping `"high"` / `"certain"` → `"stated"`, any other non-standard value → `"inferred"`. Applied at all three call sites (facts, preferences, corrections).

### Finding 3: Tier 2 is async — entities appear after NEXT message (expected, documented)

**Observation:** Entity and knowledge entries from a given message appear in CLI output only after the user sends another message. This is by design — Tier 2 fires as `asyncio.create_task()` after the response is assembled, so it runs concurrently with the next message cycle.

**Status:** Expected behavior. Documented in live test results.

### Finding 4: Relationship-role + name not linked (known gap, deferred)

**Observation:** "user's wife" and "Liana" created as two separate EntityNodes. The resolver cannot connect a relationship-role entity to a subsequently provided name without explicit relationship-role matching.

**Status:** Known gap. Documented in SPEC-2A. Deferred to Phase 2C.

### Finding 5: Phone extraction fails with `#` prefix (LLM parsing limitation)

**Observation:** When phone number was preceded by `#` symbol (e.g., `# is 142-555-9234`), the LLM did not extract it. The `#` reads as a hashtag/ID marker.

**Status:** LLM parsing edge case, not a code bug. No fix needed — rephrasing works correctly.

---

## Troubleshooting

**No entities appear after sending messages:**
- Check VOYAGE_API_KEY is set: `grep VOYAGE_API_KEY .env`
- Check bot logs for "entity resolution services" initialization message
- If VOYAGE_API_KEY is missing, enhanced path is inactive — entity resolution won't run
- Remember: entities appear after the NEXT message (Tier 2 is async)

**Entity count keeps growing (no deduplication):**
- Embeddings may be failing; check logs for "embedding failed" warnings
- NOOP zone requires cosine similarity >0.92 — very different phrasings legitimately ADD

**Knowledge entries show `[high]` confidence:**
- Indicates `_normalize_confidence()` is not being called
- Check that the fix is present in `llm_extractor.py`

**All facts being skipped (zero knowledge entries):**
- Check EXTRACTION_SCHEMA accepts `relationship_type`, `phone`, `email` fields in entities
- Run: `source .venv/bin/activate && python -c "from kernos.kernel.projectors.llm_extractor import EXTRACTION_SCHEMA; print('Schema OK')"`

**"present_not_presume" not firing:**
- Only triggers when strong new-person signals ("met today", "just met", "seems nice") are in context AND an entity with that exact name already exists
- Test requires an existing entity first — complete entity creation steps before testing this phase
