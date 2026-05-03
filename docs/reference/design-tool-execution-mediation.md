# DESIGN: Tool Execution Mediation — The Surgeon's Assistant

**Status:** Design vision. Frames the V1 spec and future direction.
**Date:** 2026-03-31
**Source:** Owner + the design review conversation on tool result handling.

---

## The insight

Tool surfacing and tool result handling are the same problem
from opposite ends:

- **Tool surfacing** mediates between raw capabilities and the
  agent's awareness: "which tools should the agent even see?"
- **Result mediation** mediates between raw tool output and the
  agent's working context: "how should this result enter the
  agent's world?"

Both are contextual packaging problems. Both sit between the raw
system and the agent's scarce reasoning window. They belong to
the same architectural family.

---

## The surgeon's assistant model

The main agent is the surgeon. It focuses on the patient (the
user's need) and trusts its operating environment.

When the agent needs a tool:
1. The assistant surfaces the right tool (or options)
2. The surgeon picks and uses it
3. The assistant receives the raw result
4. The assistant packages the result for the surgeon — cleaned,
   right-sized, in the most useful form for the current operation
5. The assistant decides where the full artifact lives

The surgeon never directly metabolizes raw tool exhaust. The
assistant is the mediation layer.

---

## Three decisions after every tool execution

### Decision 1: What is the operative representation?

What should the agent actually see in hot context RIGHT NOW?

This is NOT the raw result. It's the result transformed into the
most useful form for the current conversational task. Examples:

- "What's the top Reddit story?" → The agent needs one headline
  and maybe a summary, not 50k of page HTML
- "Search for guitar exercises" → The agent needs 3-5 relevant
  results with titles and URLs, not raw search API JSON
- "Get my calendar for tomorrow" → The agent needs a clean event
  list, not the full Google Calendar API response
- "Pull the full HTML of example.com" → The agent actually needs
  the raw HTML (because that's what was requested)

The operative representation is task-dependent. The same tool
result might need different packaging depending on what the
conversation is about.

### Decision 2: What is the storage class?

Where does the full result belong?

| Class | When | Example |
|-------|------|---------|
| **Inline only** | Small, consumed in the moment, no life beyond this turn | get-current-time, simple API responses |
| **Inline + artifact** | Useful now AND has a life beyond the turn | Search results the user might revisit, calendar data |
| **Artifact only** | Too large for inline but may be needed later | Full web page, large API response, research report |
| **Fact extraction** | Only the operative consequence matters | "What's Alex's birthday?" → extract fact, discard the search result |
| **Raw preservation** | Fidelity itself is the point | Explicitly requested HTML, raw JSON for debugging |

### Decision 3: Does the raw source need cleaning?

Not all raw results are worth preserving as-is:

- **Raw source is itself valuable:** Full HTML requested
  explicitly, raw API JSON for debugging, exact transcript.
  Preserve raw.
- **Raw source is transport garbage:** Scraped page with nav
  junk, cookie banners, formatting cruft. The artifact worth
  saving is a CLEANED extraction, not the raw blob.
- **Only distilled consequences matter:** "What's the top
  story?" → neither the raw HTML nor a cleaned version matters.
  Only the answer matters.

---

## The full architecture (future)

```
User request
    │
    ▼
┌─────────────────────────┐
│  Tool Surfacing Layer    │  ← "Which tools should the
│  (already built - 4D)   │     agent even see?"
└─────────────────────────┘
    │
    ▼
Agent selects + calls tool
    │
    ▼
Tool executes, returns raw result
    │
    ▼
┌─────────────────────────┐
│  Result Mediation Layer  │  ← "How should this result
│  (the cohort agent)      │     enter the agent's world?"
│                          │
│  1. Classify result type │
│  2. Clean transport junk │
│  3. Build operative repr │
│  4. Decide storage class │
│  5. Persist if needed    │
│  6. Hand agent the right │
│     package for THIS turn│
└─────────────────────────┘
    │
    ▼
Agent reasons with clean, right-sized context
```

This is the same pattern three times:
- Facts: raw knowledge store → selective injection → agent sees
  relevant facts
- Tools: raw tool catalog → dynamic surfacing → agent sees
  relevant tools
- Results: raw tool output → result mediation → agent sees
  useful operative package

---

## How V1 spec fits

The current SPEC-TOOL-RESULT-BUDGETING is step 1 of this vision.
It does the simplest possible version of result mediation:

```
V1 (current spec):
  Result > 4000 chars?
    Yes → persist raw, inject preview + reference
    No  → inject raw as-is

Future (full mediation):
  Result arrives →
    Classify type (search result / web page / API data / file)
    Clean if transport garbage (strip HTML junk, nav, cookies)
    Build operative representation for current task
    Decide storage class (inline / artifact / fact / raw)
    Inject the right package into context
```

V1 is a containment guardrail. It stops the worst case (50k of
raw HTML in context). But it doesn't clean, doesn't classify,
doesn't task-adapt, and doesn't fact-extract. Those are the
layers that make the mediation truly intelligent.

**V1 is still worth shipping** because:
- It stops the immediate bleeding (unbounded results erasing 4D)
- It establishes the persist + preview + reference pattern
- It creates the infrastructure (file persistence, read_doc
  recovery) that the future mediation layer will use
- It doesn't block anything — the mediation layer replaces the
  simple threshold check, keeping everything else

**V1 is NOT the end state** because:
- A dumb 4000-char cutoff doesn't know that a search result
  needs different packaging than a web page
- Saving raw HTML garbage to disk is not always the right
  artifact to persist
- "First N chars" is not an operative representation
- There's no task-awareness in the packaging decision

---

## The term: "cohort agent" vs alternatives

The owner is looking for a term for this out-of-context
filtering/mediation layer. Options:

| Term | Connotation |
|------|-------------|
| **Cohort agent** | Implies peer-level agent working alongside |
| **Mediator** | Accurate but generic |
| **Operative** | Implies task-aware packaging |
| **Steward** | Implies careful management of what enters context |
| **Valet** | Implies preparing the right things for the principal |
| **Preprocessing layer** | Technical, the design review's earlier term |

the design review used "preprocessing capability layer" in the earlier Internal
Capability Grammar design. The owner's "cohort agent" adds the
nuance that this layer may itself need lightweight reasoning, not
just structural processing.

The term doesn't need to be decided now, but the concept is clear:
**a mediation layer that sits between raw execution and the agent's
context, with enough intelligence to classify, clean, package, and
route tool results appropriately for the current task.**

---

## Implementation staging

### Stage 1: Containment (SPEC-TOOL-RESULT-BUDGETING — current)
- Fixed threshold
- Dumb preview (first N chars)
- Persist raw to file store
- MCP tools only
- No cleaning, no classification, no task-awareness

### Stage 2: Cleaning (future — probably next)
- Strip known transport garbage from web results (HTML boilerplate,
  nav, cookie banners, scripts, style tags)
- Clean before persisting (save the useful content, not the junk)
- Maybe per-tool-type cleaning rules (browser tools get HTML
  cleanup, search tools get result normalization)

### Stage 3: Classification + smart packaging (future)
- Classify result type (search result, web page, API data,
  calendar data, file content)
- Build task-appropriate operative representation
- Lightweight model call: "given this tool result and the current
  conversation, what's the most useful summary for the agent?"
- This is where the "cohort agent" reasoning lives

### Stage 4: Storage policy (future)
- Decide storage class per result (inline / artifact / fact / raw)
- Fact extraction from tool results (search result about Alex's
  birthday → extract fact into knowledge store)
- Artifact lifecycle (cleanup old tool result files)

### Stage 5: Unified mediation layer (Phase 8-10)
- Same layer handles tool surfacing AND result mediation
- Preprocessing capability layer from the design review's grammar design
- Full surgeon's assistant model

---

## Relationship to existing architecture

| System | What it mediates | Built? |
|--------|-----------------|--------|
| Selective injection | Facts → agent STATE | ✅ Shipped |
| Dynamic tool surfacing | Tools → agent ACTIONS | ✅ Shipped |
| Checkpointed harvest | Conversation → durable facts | ✅ Shipped |
| Ledger architecture | History → bounded MEMORY | ✅ Shipped |
| **Result mediation** | **Tool output → agent RESULTS** | **Stage 1 next** |

Result mediation is the last unmediated channel between the raw
system and the agent's context. Everything else has a boundary
layer. Tool results are still raw injection.

---

## The principle (from the owner + the design review)

> A tool result should be inline when it is primarily consumed
> in the moment. A tool result should become an artifact when it
> has a life beyond the moment. A tool result should become
> extracted context when only its operative consequence matters.
> And only when fidelity itself is the point should raw source
> be preserved as raw source.

That's the theory. V1 is the first mechanical step toward it.
