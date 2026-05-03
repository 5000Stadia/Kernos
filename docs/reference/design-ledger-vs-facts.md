# DESIGN: Ledger Memory vs Facts — Why They're Separate and How Extraction Should Work

**Status:** Design exploration for owner + the design review review before spec.
**Date:** 2026-03-30
**Context:** Challenging the per-turn extraction pipeline. the design review proposed
checkpointed boundary-driven extraction. Owner asks: what IS the
dividing line between ledger memory and facts?

---

## The exhaustive case for two separate systems

### What ledger memory IS

Ledger memory is the **chronological narrative** of what happened in
a context space. It is time-ordered, event-shaped, and tells the
story of a relationship or project.

A ledger entry says:
- "On March 25, we discussed Alex's birthday dinner. The user
  mentioned a shellfish allergy and a $50/person budget."
- "On March 27, the user tested calendar reminders at multiple
  lead times and we resolved several scheduler issues."

**Ledger is oriented by TIME.** When did this happen? What was
the sequence? What was the conversation about?

**Ledger is per-space.** The "Daily" space has one narrative arc.
An "Architecture" space would have a different one. They don't
cross because they represent different threads of life/work.

**Ledger compresses over time.** Recent entries are detailed.
Older entries become the archive story. Ancient history becomes
a one-paragraph orientation. This is INTENTIONAL — the narrative
loses granularity because most of it isn't needed for current
operation. The raw logs remain recoverable for drill-down.

**Ledger answers:** "What has happened between us?" / "What was
this conversation about?" / "Where should I look to find when
we discussed X?"

### What facts ARE

Facts are **extracted durable truths** about the user and their
world. They are subject-oriented, truth-valued, and represent
the system's current understanding of reality.

A fact says:
- "User is 32 years old." (identity)
- "Alex has a shellfish allergy." (structural)
- "User prefers no extra confirmation for cross-channel messaging." (habitual)
- "User is learning guitar, knows G/C/D/Em." (structural)

**Facts are oriented by SUBJECT.** What is this about? Is it still
true? When did it become true? What did it supersede?

**Facts are cross-space.** The user's age is true regardless of
which context space is active. Alex's shellfish allergy matters
in the "Daily" space when planning dinner AND in a hypothetical
"Cooking" space when suggesting recipes. Facts belong to the
tenant, not to a space.

**Facts should NOT compress over time.** "User is 32 years old"
should remain precise and current until it's superseded by "User
is 33 years old." Unlike ledger entries, facts don't benefit from
summarization — a summarized fact is a less accurate fact. They
benefit from SUPERSESSION: old value → new value, with temporal
provenance.

**Facts answer:** "What is currently true?" / "What should I act
from right now?" / "What do I know about this person/entity?"

### Why you can't fold facts into ledger

**Temptation:** "Just make the ledger richer. When compaction runs,
it already sees the conversation. Have it extract facts as part
of the ledger entry. Then the ledger IS the fact store."

**Why this fails:**

1. **Ledger compresses; facts shouldn't.** If "User is 32" lives
   in ledger entry #5, and entry #5 gets archived into the archive
   story, the fact becomes part of a paragraph-level narrative
   summary. It's no longer a precise, addressable, queryable truth.
   It's buried in prose. You can't supersede it cleanly. You can't
   filter it by archetype. You can't decide whether to inject it
   into STATE or not.

2. **Ledger is per-space; facts are cross-space.** Alex's shellfish
   allergy was mentioned in the "Daily" space. If facts live in
   the ledger, they're trapped in that space's narrative. A future
   "Cooking" space can't see them without cross-space ledger
   queries — which is exactly the kind of deep retrieval we're
   trying to avoid in the hot path.

3. **Ledger is chronological; facts are subject-centric.** When
   I need "what do I know about income?" I want to query by subject,
   not scan a chronological narrative for mentions. Facts organized
   by subject support efficient lookup. Ledger organized by time
   supports efficient "what happened when?"

4. **Ledger entries accumulate; facts supersede.** If the user's
   income changes from $85k to $95k, the ledger should have BOTH
   entries (in March we discussed $85k; in June they mentioned
   $95k). The fact store should have ONE active entry ($95k) with
   provenance showing it superseded the $85k entry. These are
   different lifecycle models.

5. **Ledger is the EVIDENCE; facts are the CONCLUSIONS.** The
   ledger says "the user mentioned earning about $85k." The fact
   store says "User's income is approximately $85,000/year
   (confidence: stated, source: log_002)." The fact is an
   extracted, typed, queryable conclusion WITH provenance pointing
   back to the ledger/log evidence. Collapsing them destroys the
   evidence → conclusion separation.

### Why you can't fold ledger into facts

**Temptation:** "Just extract everything as facts. 'On March 25
we discussed dinner planning' becomes a fact. The ledger is just
a collection of facts."

**Why this fails:**

1. **Narrative coherence.** The value of ledger memory is the
   STORY — the arc, the sequence, the context of why things
   happened. "We discussed dinner planning" as an isolated fact
   loses the narrative thread that connects it to "we also
   discussed guitar" and "we tested calendar reminders." The
   story IS the point.

2. **Unbounded growth.** Every turn produces conversational content.
   If all of it becomes facts, the fact store grows linearly with
   conversation length. Facts should be a SMALL, curated set of
   durable truths — not a transcript database.

3. **Wrong retrieval model.** "What were we talking about last
   week?" is a ledger query (chronological, narrative, bounded
   by time). "What is the user's income?" is a fact query (subject-
   centric, truth-valued, returns current state). These want
   different retrieval paths.

### The complementary relationship

```
LEDGER                          FACTS
─────────────────────────       ─────────────────────────
Oriented by: time               Oriented by: subject
Scope: per-space                Scope: cross-space (tenant)
Lifecycle: compress over time   Lifecycle: supersede, don't compress
Answers: what happened?         Answers: what is true?
Shape: narrative entries        Shape: typed key-value with provenance
Role: evidence store            Role: conclusion store
Hot-path form: archive story    Hot-path form: selective injection
  + hot tail                      into STATE
Deep form: raw archived logs    Deep form: supersession chain
                                  + temporal provenance
```

They are complementary because:
- **Ledger without facts** = you know what happened but have to
  re-derive current truth from narrative every turn
- **Facts without ledger** = you know what's true but have no
  provenance, no narrative context, no way to answer "how do
  I know this?" or "when did we discuss this?"
- **Both together** = facts tell you what to act from now; ledger
  tells you why you believe it and where to look if challenged

This is the same separation as:
- a lab notebook (ledger) vs a published finding (fact)
- meeting minutes (ledger) vs action items (facts)
- a case file (ledger) vs a diagnosis (fact)
- git log (ledger) vs current codebase state (fact)

---

## The extraction redesign: checkpointed boundary-driven harvest

### Current pipeline (per-turn, aggressive)

```
Every turn:
  1. Agent responds
  2. Tier 2 extractor reads conversation → proposes candidates
  3. Each candidate → embedding similarity vs existing entries
  4. Similarity gate: AUTO-ADD / LLM-REVIEW / AUTO-NOOP
  5. Store results

Problems:
  - Same fact mentioned across 5 turns → 5 extraction attempts
  - Each attempt evaluated independently against different
    existing-entry snapshots
  - Threshold determines whether LLM even gets asked
  - Duplicate pressure is structural, not incidental
```

### Proposed pipeline (boundary-driven, holistic)

```
At visibility boundaries (compaction trigger / space switch):
  1. Identify unharvested conversation span
     (checkpoint → current position)
  2. ONE LLM call reads:
     - The full unharvested span
     - ALL current active facts for this tenant
  3. LLM outputs a RECONCILED fact set:
     - New facts to add (with content, archetype, confidence)
     - Existing facts to update (entry ID + new content)
     - Existing facts to confirm/reinforce (entry ID)
     - Existing facts to supersede (old ID + new content)
  4. Apply all changes atomically
  5. Advance checkpoint

Why this is better:
  - LLM sees full context, naturally deduplicates
  - LLM sees ALL existing facts, naturally detects updates
  - No embedding threshold needed for dedup
  - No per-candidate similarity computation
  - One call replaces many per-turn extraction + dedup calls
  - Reconciliation is holistic, not incremental
```

### When boundaries fire

**Compaction trigger:** The most natural boundary. When the
conversation log reaches the compaction threshold and compaction
runs, the fact harvest runs in the same pass. The compaction
LLM already sees the full conversation span — a second cheap
call (or an expanded compaction prompt) can harvest facts from
the same material.

**Space switch:** When the user switches context spaces, the
departing space's unharvested span gets harvested. This ensures
facts aren't lost when the user moves on before compaction fires.

**Explicit checkpoint advance:** A diagnostic/maintenance tool
could force a harvest. Not for normal use.

### The checkpoint

Each space tracks:
```python
@dataclass
class FactHarvestState:
    space_id: str
    last_harvested_log: str     # e.g., "log_067"
    last_harvested_offset: int  # message index within log
    last_harvested_at: str      # ISO timestamp
```

When harvest runs, it reads from checkpoint to current position,
processes, then advances the checkpoint. If the user stays in one
space until compaction, the full hot span gets harvested. If they
switch at 85% full, harvest runs on that 85%, and when they
return, only the new material since then gets harvested.

### The reconciliation call

This is the key architectural improvement. Instead of per-candidate
dedup, ONE call sees everything:

```
You are maintaining a durable fact store about a user. Below is
the current fact store and a new conversation span to harvest.

CURRENT FACTS (all active entries):
1. [know_abc] "User is 32 years old" (identity, valid_at: 2026-03-25)
2. [know_def] "User's income is ~$85k/year" (structural, valid_at: 2026-03-25)
3. [know_ghi] "User is learning guitar, knows G/C/D/Em" (structural)
4. [know_jkl] "Alex birthday April 12, shellfish allergy" (structural)
...

NEW CONVERSATION SPAN (unharvested):
[User]: I've been practicing guitar, any tips for barre chords?
[Agent]: ... mini-barres, short reps ...
[User]: I'm planning dinner for Alex's birthday
[Agent]: ... restaurant suggestions ...
[User]: Should I put more into retirement?
[Agent]: At 32 and ~$85k ...

INSTRUCTIONS:
Analyze the conversation and reconcile with existing facts.
Return a JSON object with:
- "add": new facts not in the store [{content, archetype, confidence}]
- "update": existing facts that need updating [{id, new_content, reason}]
- "reinforce": existing facts confirmed by this conversation [{id}]
- "no_change": if nothing durable was said, return empty arrays

Rules:
- Only extract facts that are durable and worth remembering
- A fact still visible in active conversation does NOT need to
  be extracted yet — it will be harvested when it approaches
  the visibility boundary
- Do NOT extract transient conversational content
- Do NOT extract facts that are already accurately represented
  in the current store
- If a fact updates an existing one, specify which entry to update
- Use the user's actual words as ground truth, not the agent's
  paraphrasing
```

### What this eliminates

- **Embedding similarity computation** for dedup → not needed
- **Per-candidate threshold tuning** → not needed
- **Ambiguous-zone LLM calls** → folded into one holistic call
- **Multiple extraction calls per turn** → zero per turn
- **Duplicate accumulation** → structurally impossible (LLM sees
  all existing facts and deduplicates in context)

### What this preserves

- **Fact store as separate entity** → yes, still cross-space,
  still subject-oriented, still typed with archetypes
- **Supersession chain** → yes, the reconciliation call outputs
  "update entry X with new content"
- **Provenance** → the harvest records which log span produced
  each fact
- **Selective injection** → unchanged, still uses the same
  three-tier system to decide what enters STATE each turn
- **Temporal fields** → populated naturally (valid_at on create,
  invalid_at on supersede)

### Cost comparison

Current: ~2-4 Haiku calls per turn (extraction + dedup per candidate)
Proposed: 0 calls per turn + 1 larger call per compaction/boundary

At 15-20 turns between compactions, this is:
- Current: 30-80 Haiku calls
- Proposed: 1 call (larger, but still Haiku-class)

Net: significantly cheaper AND more accurate.

---

## What the spec should look like

### SPEC-CHECKPOINTED-FACT-HARVEST (replaces SPEC-FACT-LIFECYCLE-HARDENING)

1. **Remove per-turn Tier 2 extraction** for facts/preferences
   (keep corrections — those are time-sensitive)
2. **Add checkpoint tracking** per space
3. **Add harvest trigger** at compaction boundaries and space switch
4. **Implement reconciliation call** — one LLM pass over
   unharvested span + all existing facts
5. **Populate temporal fields** (valid_at, invalid_at) as part
   of reconciliation
6. **Preserve the embedding dedup system** as a fallback/safety
   net, but it should rarely fire because the reconciliation
   call handles dedup holistically

### What stays unchanged

- Selective injection (STATE shaping) — still tier-based + Haiku
- Ledger architecture — still hot tail + archive story
- Knowledge entry schema — same fields, same store
- remember_details — still works for deep retrieval
- Corrections pipeline — still per-turn (time-sensitive)

---

## The dividing line, stated as a principle

> **Ledger memory is the story of what happened. Facts are the
> current truths extracted from that story.**
>
> Ledger compresses over time because narrative detail becomes
> less valuable. Facts supersede over time because old truths
> get replaced by new truths.
>
> Ledger is per-space because different areas of life have
> different narratives. Facts are cross-space because truths
> about a person don't change depending on which room you're in.
>
> Ledger is the evidence. Facts are the conclusions. You need
> both: conclusions without evidence have no provenance;
> evidence without conclusions requires re-derivation every turn.
>
> They are complementary systems with different lifecycles,
> different scopes, different retrieval models, and different
> hot-path representations. Collapsing either into the other
> destroys the property that makes each one valuable.
