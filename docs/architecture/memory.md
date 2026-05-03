# Memory: Ledger and Facts

> Two stores, two jobs. The Ledger holds the conversational arc. The Facts store holds structured, reconciled knowledge. Neither degrades into the other.

## The problem

Agent memory is usually one thing shaped like two different jobs at once. Either the system rolls conversation history into running summaries (and the summaries drift; tiny errors compound; the arc of what actually happened gets smoothed into an approximation), or it tries to build a structured knowledge graph by extracting facts turn-by-turn (and ends up with a graph full of duplicates, contradictions, and entries nobody can find because the extractor didn't know what was already there).

Neither shape is wrong on its face. Both are asking one store to do two jobs:

- **Carry the narrative.** *What actually happened, in what order, so the assistant can ground itself in the arc of events.*
- **Carry the durable truth.** *What is stably true about the user, so the assistant doesn't have to re-derive it every turn.*

These jobs have different retention profiles, different reconciliation rules, and different failure modes. When a single store does both, the failure modes of the narrative side (drift, smoothing) mix with the failure modes of the facts side (duplication, contradiction), and the result is a memory that's neither reliable nor referenceable.

Kernos separates them.

## Two stores

```
LEDGER  →  conversational arc, compressed at boundaries, lossless at its own resolution
FACTS   →  structured truths, reconciled against the existing store in a single LLM call
```

Each store has one job. Each is refreshed at boundary transitions — not turn-by-turn — so the reasoning that maintains them can be deliberate instead of panicked.

### The Ledger

The Ledger is the conversational arc. It is the assistant's answer to *"what actually happened, and in what order, in this space?"*

Per-space logs are the raw material (`kernos/kernel/conversation_log.py`). Every turn appends to the space's log. At compaction boundaries, the log is transformed into a two-layer structure:

- **Living State** — the short, rewritten summary of the current working context. This is the part the agent reads back in the `MEMORY` zone. It's rewritten at each compaction, not patched.
- **Archive Ledger** — append-only entries preserving the spans that were compressed, with enough anchoring to retrieve them if needed. The agent doesn't read this directly by default; it's retrievable through the archive index when older context becomes relevant.

The operational distinction matters. Summary-based memory systems degrade because the summary is the *only* representation of history; when the summary drifts, history drifts with it. The Ledger keeps the narrative **recoverable**: the Living State may be a summary, but the Archive Ledger is the source of truth, and the Living State can always be regenerated against it.

At compaction boundaries, the compaction cohort rewrites the Living State and appends a new archive block. Entry point: `kernos/kernel/compaction.py:383` (`CompactionService`), with rewrites at `compaction.py:771` (`compact`) and `compaction.py:1099` (`compact_from_log`).

### The Facts store

The Facts store is the durable structured knowledge about the user. It is the assistant's answer to *"what is stably true about this member, this household, this team, this project?"*

Each fact is a `KnowledgeEntry` carrying:

- `content` — the plain-language statement
- `owner_member_id` — which member this fact is about (`kernos/kernel/state.py:127`)
- `sensitivity` — `"open" | "contextual" | "personal"` — the disclosure-gate modifier
- `lifecycle_archetype` — `"identity" | "structural" | "habitual" | "contextual"` — how durable this fact is
- `confidence` — `"stated" | "inferred" | "observed"`
- `active` — soft-delete flag; no fact is ever permanently removed

Facts are never extracted turn-by-turn. Turn-by-turn extraction is the shape that produces duplicates, contradictions, and orphaned entries. Facts are extracted at compaction boundaries by the **fact harvester** cohort, and they are extracted **against the current fact store**.

## Single-call reconciliation

The distinctive move in Kernos's fact architecture is that the fact harvester does its work **in a single LLM call, with the existing fact store in the prompt, and with its output already reconciled**.

```python
# kernos/kernel/fact_harvest.py:199-215
all_facts = await state_store.query_knowledge(
    instance_id, subject="user", active_only=True, limit=200,
    member_id=member_id,
)
facts_text = "\n".join(
    f'- [{e.id}] "{e.content}" ({e.lifecycle_archetype})'
    for e in all_facts
)

user_content = (
    f"CURRENT FACTS:\n{facts_text}\n\n"
    f"CONVERSATION SPAN TO HARVEST:\n{conversation_text}"
)
```

The harvester's prompt is shaped around three verbs:

- **add** — a new fact
- **update** — the ID of an existing fact and a replacement content; the old version is archived, the ID is re-used
- **reinforce** — the ID of an existing fact, confirming it was re-observed in this span

(`kernos/kernel/fact_harvest.py:30-35`)

Because the existing facts are in the prompt, the LLM can make the add-vs-update-vs-reinforce decision *during extraction*. There is no separate deduplication pass, no post-hoc "is this a new fact or a restatement of an old one" reconciliation, no growing pile of near-duplicates the next pass has to sort out. What the harvester produces is already reconciled against what was there.

The failure modes are contained. If the primary harvest call fails, the outcome dict records `primary_ok: False` and no fact changes are applied (`fact_harvest.py:184`). A secondary stewardship pass runs separately for value extraction, tension detection, and operational insight surfacing, with its own try/except so a failure there doesn't cascade back and cost the primary harvest.

## Boundary-triggered, not turn-triggered

Both stores are refreshed at **boundary transitions**, not every turn.

The two kinds of boundary:

- **Compaction boundary** — the space's log has reached the token threshold that would cost the next turn's context window too much. Compaction runs; the Living State is rewritten; an archive block is appended; the fact harvester runs on the compacted span.
- **Space-switch boundary** — the user moved from one space to another. The departing space's recent span is eligible for harvest; its last-active timestamp is updated.

Turn-by-turn extraction is deliberately not the model. The reasons are the same as for one-store-trying-to-do-both-jobs:

- **Cost.** An LLM call every turn for fact extraction is expensive in both money and latency.
- **Noise.** Most turns don't change what's durably true about the user. The harvester that runs every turn spends most of its attention finding nothing.
- **Reconciliation.** The per-turn harvester either doesn't see the existing fact store (produces duplicates) or has to see all of it (expensive; same problem as above).

Boundary triggering resolves all three. A compaction boundary is the right moment to step back and ask *"what durable truths emerged from this span?"* The compacted span is bounded, the LLM's attention is focused, and the harvester's job is to reconcile — not to catch everything the moment it happens.

Call site for the boundary-triggered harvest:

```
kernos/messages/handler.py:6601    _outcome = await harvest_facts(...)
```

This call lives inside the boundary condition block at `handler.py:6533` (`# Compaction (with concurrency guard + backoff)`). When compaction doesn't run, harvest doesn't run.

## Member-scoping

In a multi-member installation, both stores are keyed per-member:

- **Ledger** per-space-per-member — the same space's log is different for member A and member B if both are present in it.
- **Facts** per-member via `owner_member_id` — a fact about member A is retrieved when member A's agent asks for it, and is not visible to member B's agent unless a disclosure gate explicitly permits it.

The harvester's `query_knowledge` call passes `member_id` (`fact_harvest.py:202`) so the current facts loaded into the reconciliation prompt are the *current facts about this member*. The harvester isn't reconciling member A's conversation span against member B's fact store.

This is what makes the per-member architecture durable over time. A family of three doesn't slowly accumulate a tangled shared fact graph where nobody knows what applies to whom; a small team doesn't lose the ability to distinguish *"person X's typical working hours"* from *"person Y's typical working hours"* just because both were once discussed in the same space.

## The two failure modes, addressed

| Failure | How single-store systems fail | How the two-store model addresses it |
|---|---|---|
| **Summary drift** | The summary is rewritten each pass on top of the previous summary; small errors compound | Living State is regenerated from the Archive Ledger, not patched on top of the previous version |
| **Fact duplication** | Extractor doesn't see existing facts; every pass adds near-duplicates | Existing facts are in the prompt; add/update/reinforce decisions happen at extraction time |
| **Contradiction** | Extractor adds a new "X is true" fact without noticing an old "X is false" fact | `update` with the existing ID replaces and archives the old version atomically |
| **Orphaned truth** | A durable fact exists in an old conversation but was never structurally extracted | Compaction boundary runs harvest; the durable truths in the compacted span are reconciled into the store before the span is archived |

## What this architecture makes easy

- **Long-arc grounding.** A household that has used Kernos for six months, or a team that has used it across three quarterly cycles, can retrieve the arc of what happened — not just a summary of what the system thinks happened. The Archive Ledger is addressable.
- **Trustworthy fact reference.** When the agent says *"you mentioned you prefer morning meetings"* or *"your co-owner asked about the Q3 runway two weeks ago"*, the fact has a stable ID, a known source span, and a reconciled history.
- **Graceful supersession.** When a fact changes (the member's role, the team's process, the kid's teacher this year), the `update` verb supersedes cleanly — the old value is archived, the new value replaces it under the same ID, and references to the ID keep working.
- **Sensitivity at harvest time.** Each new or updated fact is classified (open/contextual/personal) at the moment it's extracted, so the disclosure-gate (see [Messenger](disclosure-and-messenger.md)) has the information it needs from day one.
- **Per-member privacy.** Because facts are `owner_member_id`-keyed, cross-member disclosure is deliberate, not accidental. A fact about member A is not in member B's agent's context unless the permission matrix and the Messenger both say it belongs there.

## What this architecture explicitly does not try to do

- **It does not build a semantic knowledge graph.** Facts are structured records with anchors (subject, archetype, confidence), not nodes in a graph with typed edges. The harvester is not a graph builder; it is a reconciler.
- **It does not attempt to re-derive the present from history each turn.** The Living State is the agent's working picture, and it is refreshed at compaction. Turn-to-turn, the agent reads the Living State, not the Archive Ledger.
- **It does not prevent all stale facts.** A fact that was true but stops being true without an explicit contradiction in a later span can persist. This is an acceptable failure mode — the fix is the user saying *"actually that's not the case anymore"*, which the harvester will pick up on the next boundary as an `update`.
- **It does not attempt turn-level extraction of "anything the agent might need to know."** The harvester extracts what is durable. Transient turn-level context (what the user asked three messages ago) lives in the recent history, not in the Facts store.

## Related architecture

- **[Context spaces](context-spaces.md)** — how the Ledger and Facts stores are partitioned per-space
- **[Multi-member disclosure layering](disclosure-and-messenger.md)** — how `sensitivity` and `owner_member_id` are consumed downstream
- **[Cohort architecture](cohort-and-judgment.md)** — the fact harvester and the compaction service as two of the six cohorts

## Code entry points

- `kernos/kernel/fact_harvest.py:167` — `harvest_facts`; the boundary-triggered, reconciling entry point
- `kernos/kernel/fact_harvest.py:25-50` — the primary harvest prompt, with the `add`/`update`/`reinforce` schema
- `kernos/kernel/compaction.py:383` — `CompactionService`; the Living-State rewriter and archive appender
- `kernos/kernel/compaction.py:708` — `should_compact`; the boundary check
- `kernos/kernel/compaction.py:1099` — `compact_from_log`; the log-consuming compaction path
- `kernos/kernel/state.py:127` — `KnowledgeEntry.owner_member_id`; the per-member scoping
- `kernos/messages/handler.py:6601` — the turn-pipeline call site for boundary-triggered harvest
