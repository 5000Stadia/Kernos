# Live Test Protocol

## When to Run

After every significant code change before pushing to production.
Two tiers: automated (Claude Code runs) and manual (founder runs via Discord).

## Tier 1: Automated (run_live_smoke.py)

Tests that invoke the handler directly with the real LLM provider.
No MCP servers needed. Run from Claude Code after changes.

```
source .venv/bin/activate && python tests/live/run_live_smoke.py
```

**What it validates:**
- LLM provider returns non-empty responses (catches Codex parsing regressions)
- Router fires and produces valid routing decisions
- Knowledge shaping produces results (structured output works)
- Tool surfacing logs correctly
- Code execution works end-to-end
- /dump produces a context file (verifiable for DEPTH, USER CONTEXT, etc.)
- Context dump content checks (DEPTH paragraph, source tags, no duplicates)

**Results:** Written to `tests/live/SMOKE_RESULTS.md`

## Tier 2: Manual (Discord — requires MCP + real tenant)

Tests that require MCP connections (calendar, search) and the real
tenant's data (knowledge entries, spaces, preferences). Founder runs
these via Discord after /restart.

### Test 1: Tool Surfacing Across Spaces
**Setup:** Switch to D&D space, then request calendar action.
```
"Let's play some D&D"
"Make a calendar entry called Test Event at noon tomorrow"
```
**Expected:**
- Calendar tools surface via Tier 2 catalog scan
- Event created successfully
- Console: `TOOL_SURFACING: tier=catalog_scan`
- No "I can't create it from here"

### Test 2: Tool Promotion
**Setup:** Still in D&D, query calendar.
```
"What's on my calendar today?"
```
**Expected:**
- Calendar tools now in D&D's local_affordance_set (promoted)
- Console: `TOOL_PROMOTED` from Test 1
- Console: `TOOL_SURFACING: tier=common` this time (no catalog scan)

### Test 3: Work Mode Routing
**Setup:** From D&D, schedule-related request.
```
"Let me check my schedule for the week"
```
**Expected:**
- Console shows `work_mode` field in router response
- Either stays in D&D (calendar is universal) or routes to General
- Key: router distinguishes work intent from query intent

### Test 4: General Bloat Guard
**Setup:** From General, check local_affordance_set.
```
/spaces
```
**Expected:**
- General's local_affordance_set should NOT have domain-specific tools
- Calendar/search/time are OK (universal)
- Console: `TOOL_PROMOTE_SKIP` if domain-specific tool was used in General

### Test 5: DEPTH Structural Confidence
**Setup:** Run /dump from any space.
```
/dump
```
**Expected in dump file (RULES block):**
- "You are precisely briefed for this turn with full retrieval capability behind you"
- Appears after MEMORY instruction, before SCHEDULING

### Test 6: USER CONTEXT Cleanup
**Setup:** Check /dump STATE block.
**Expected:**
- No duplicate knowledge entries (same content twice)
- Each fact has source tag: `[stated]`, `[observed]`, `[established]`, etc.
- No "The user's current identity/name in the system state is Kernos"

### Test 7: Domain Posture
**Setup:** Check /spaces for any compaction-created domain.
**Expected:**
- Domain created via assessment should have a posture field
- e.g., "Creative and improvisational" for D&D
- Skip if no compaction-created domain exists this session

### Test 8: Downward Search
**Setup:** While in D&D, ask about General-space knowledge.
```
"Quick question, what's my zip code?"
```
**Expected:**
- Console: `DOWNWARD_SEARCH` fires
- User stays in D&D (no space switch)
- Agent answers with the ZIP code

### Test 9: Space Switching + Departure Context
**Setup:** From D&D, trigger a space switch.
```
"Actually what time is it?"
```
Then return:
```
"Back to D&D"
```
**Expected:**
- Console: `SPACE_SWITCH` + `DEPARTURE_CONTEXT`
- Routes to General for time query
- Returns to D&D on "Back to D&D"

### Test 10: Scope Chain Retrieval
**Setup:** While in D&D (child space), query parent knowledge.
```
"What preferences do I have?"
```
**Expected:**
- Preferences live in General's knowledge
- Scope chain walks up and finds them
- Agent answers with preference list

### Test 11: Context Size + Timing
**Setup:** Check any console output.
**Expected:**
- `REASON_START: ctx_tokens_est=7000-9000` range
- `TURN_TIMING: assemble=~1000-2000ms route=~700-1500ms`
- No regression from baseline

### Test 12: Server Restart Persistence
**Setup:** Mid-session, run /restart. Then send a continuation.
```
/restart
"Continue where we left off"
```
**Expected:**
- Console: `CONTEXT_SOURCE: entries=N` with N > 0
- Agent knows the prior context (not blank slate)
- Conversation log persisted across restart

## Recording Results

After running Tier 2, note pass/fail for each test in the Discord
conversation or in a friction report. Major failures should be
filed as hotfixes before the next session.
