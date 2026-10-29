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

- **Living State** — cited commitments and adopted decisions retained until supported closure, plus a short narrative generated fresh from relevant source turns. The agent reads this bounded view in the `MEMORY` zone. The previous generated narrative is not input to its successor.
- **Ledger** — append-only entries, one per compacted span: a short topical index whose header names its source log (`## Compaction #N (source: log_NNN) — first → last`). The agent sees the most recent entries (the hot tail, about 2,000 tokens) and an archive story for the older ones; every entry stays searchable through `remember`. New compactions keep one continuous Ledger; sealing and the model-written archive index are retired. Existing sealed entries remain readable and searchable, even without an index.

The operational distinction matters. Summary-based memory systems degrade because the summary is the *only* representation of history; when the summary drifts, history drifts with it. The Ledger keeps the narrative **recoverable**: each entry points into the raw log it indexes, and the logs are the source of truth. Compaction selects what supports the intent and cohesive continuation of the space; preserving every incidental example detail is not its quality standard.

**Append-only by construction.** The compaction call writes only the new entry's bullets and the new Living State. Code writes the entry header from the span's own timestamps and appends the entry to the existing ones, which never pass through the model, so earlier entries stay exactly as written. Truncated or malformed output raises `CompactionOutputError`; rejected output cannot replace the Ledger or accepted state. Migration can already have saved its pinned inputs and completed batch checkpoints, which remain resumable. (Until this was built, the model rewrote the whole document each cycle and earlier entries could be lost; `kernos.cli rebuild-ledger` restores such entries from their source logs.)

Entry point: `CompactionService.compact_from_log` in `kernos/kernel/compaction.py`.

### The Facts store

The Facts store is the durable structured knowledge about the user. It is the assistant's answer to *"what is stably true about this member, this household, this team, this project?"*

Each fact is a `KnowledgeEntry` carrying:

- `content` — the plain-language statement
- `owner_member_id` — which member this fact is about (`KnowledgeEntry` in `kernos/kernel/state.py`)
- `sensitivity` — `"open" | "contextual" | "personal"` — the disclosure-gate modifier
- `lifecycle_archetype` — `"identity" | "structural" | "habitual" | "contextual"` — how durable this fact is
- `confidence` — `"stated" | "inferred" | "observed"`
- `active` — soft-delete flag; no fact is ever permanently removed

Facts are never extracted turn-by-turn. Turn-by-turn extraction is the shape that produces duplicates, contradictions, and orphaned entries. Facts are extracted at compaction boundaries by the **fact harvester** cohort, and they are extracted **against the current fact store**.

## Single-call reconciliation

The distinctive move in Kernos's fact architecture is that the fact harvester does its work **in a single LLM call, with the existing fact store in the prompt, and with its output already reconciled**.

```python
# kernos/kernel/fact_harvest.py — harvest_facts
all_facts = await state_store.query_knowledge(
    instance_id, active_only=True, limit=200, member_id=member_id,
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
- **update** — the ID of an existing fact and a replacement content; the old version is archived as its own inactive entry (`invalid_at` set) and the current one keeps the ID, with `supersedes` pointing at the archived version
- **reinforce** — the ID of an existing fact, confirming it was re-observed in this span; it raises the fact's `storage_strength` and `reinforcement_count`

A member's harvest can update or reinforce only that member's facts. Each new fact is also given a **scope** — `member` (true of the person everywhere, so `context_space` is empty) or `space` (it belongs to this context space and its children) — a content hash, and an embedding of its content, so it can be found by meaning.

(`kernos/kernel/fact_harvest.py`: the prompt, `_apply_adds`, `_apply_updates`, `_apply_reinforces`)

Because the existing facts are in the prompt, the LLM can make the add-vs-update-vs-reinforce decision *during extraction*. There is no separate deduplication pass, no post-hoc "is this a new fact or a restatement of an old one" reconciliation, no growing pile of near-duplicates the next pass has to sort out. What the harvester produces is already reconciled against what was there.

The failure modes are contained. If the primary harvest call fails, the outcome dict records `primary_ok: False` and no fact changes are applied. A secondary stewardship pass runs separately for value extraction, tension detection, and operational insight surfacing, with its own try/except so a failure there doesn't cascade back and cost the primary harvest.

## Boundary-triggered, not turn-triggered

Both stores are refreshed at **boundary transitions**, not every turn.

The two kinds of boundary:

- **Compaction boundary** — the member's log in this space has gained `KERNOS_COMPACTION_THRESHOLD` new tokens (default 8,000). Compaction runs; a Ledger entry is appended, supported thread changes are recorded, and the short narrative is regenerated; the fact harvester runs on the compacted span. The entries seeded into a new log from the previous one are left out, since they were compacted there.
- **Space-switch boundary** — the user moved from one space to another. The part of the departing space's log not yet harvested is harvested, and a mark records how far harvesting has reached.

Boundary work runs **after the reply** is delivered, still inside the space's turn: the user never waits on it, and the next message in the space waits for it.

Turn-by-turn extraction is deliberately not the model. The reasons are the same as for one-store-trying-to-do-both-jobs:

- **Cost.** An LLM call every turn for fact extraction is expensive in both money and latency.
- **Noise.** Most turns don't change what's durably true about the user. The harvester that runs every turn spends most of its attention finding nothing.
- **Reconciliation.** The per-turn harvester either doesn't see the existing fact store (produces duplicates) or has to see all of it (expensive; same problem as above).

Boundary triggering resolves all three. A compaction boundary is the right moment to step back and ask *"what durable truths emerged from this span?"* The compacted span is bounded, the LLM's attention is focused, and the harvester's job is to reconcile — not to catch everything the moment it happens.

Call site for the boundary-triggered harvest: `run_boundary` in `kernos/messages/phases/persist.py`, which the space runner calls once the reply is out. When compaction doesn't run, harvest doesn't run there; the space-switch harvest is `harvest_log_tail` in `fact_harvest.py`.

## Member-scoping

In a multi-member installation, both stores are keyed per-member:

- **Ledger** per-space-per-member — the same space's log is different for member A and member B if both are present in it.
- **Facts** per-member via `owner_member_id` — a fact about member A is retrieved when member A's agent asks for it, and is not visible to member B's agent unless a disclosure gate explicitly permits it.

In SQLite the `member_id` and `space_id` columns mirror `owner_member_id` and `context_space`, and a member-scoped `query_knowledge` returns that member's facts only. A fact with no owner belongs to the instance owner (see `KnowledgeEntry`), and legacy ownerless facts are attributed to the owner at boot. The harvester's `query_knowledge` passes `member_id`, so the reconciliation prompt holds *the current facts about this member*, and its updates are refused for any other member's fact.

Every member-scoped read — logs, Ledger, archives, archive story — uses only the member's own folder, never the shared legacy one.

This is what makes the per-member architecture durable over time. A family of three doesn't slowly accumulate a tangled shared fact graph where nobody knows what applies to whom; a small team doesn't lose the ability to distinguish *"person X's typical working hours"* from *"person Y's typical working hours"* just because both were once discussed in the same space.

## The two failure modes, addressed

| Failure | How single-store systems fail | How the two-store model addresses it |
|---|---|---|
| **Summary drift** | The summary is rewritten each pass on top of the previous summary; small errors compound | The Ledger is append-only by construction and each entry points into its raw log, so the arc is recoverable however the Living State drifts. (The new Living State also keeps supported open items separately and generates its narrative without the previous prose.) |
| **Fact duplication** | Extractor doesn't see existing facts; every pass adds near-duplicates | Existing facts are in the prompt; add/update/reinforce decisions happen at extraction time |
| **Contradiction** | Extractor adds a new "X is true" fact without noticing an old "X is false" fact | `update` with the existing ID archives the old version and keeps the ID for the new one |
| **Orphaned truth** | A durable fact exists in an old conversation but was never structurally extracted | Compaction boundary runs harvest; the durable truths in the compacted span are reconciled into the store before the span is archived |

## What this architecture makes easy

- **Long-arc grounding.** A household that has used Kernos for six months, or a team that has used it across three quarterly cycles, can retrieve the arc of what happened — not just a summary of what the system thinks happened. `remember` ranks every one of the member's Ledger entries in scope (BM25, active and sealed alike) alongside facts and entities. By default it also searches the member's raw historical turns in the active space and its ancestors. Each match includes neighbouring turns, their original speakers and timestamps, and space/log/turn coordinates. Overlapping windows merge before the top three are selected; rollover seeds do not consume duplicate slots. The current log in every searched space is excluded because the agent already sees it. `remember_details` opens the cited log, in that space, for the same member.
- **Trustworthy fact reference.** When the agent says *"you mentioned you prefer morning meetings"* or *"your co-owner asked about the Q3 runway two weeks ago"*, the fact has a stable ID, a known source span, and a reconciled history.
- **Graceful supersession.** When a fact changes (the member's role, the team's process, the kid's teacher this year), the `update` verb supersedes cleanly — the old value is archived, the new value replaces it under the same ID, and references to the ID keep working.
- **Sensitivity at harvest time.** Each new or updated fact is classified (open/contextual/personal) at the moment it's extracted, so the disclosure-gate (see [Messenger](disclosure-and-messenger.md)) has the information it needs from day one.
- **Per-member privacy.** Because facts are `owner_member_id`-keyed, cross-member disclosure is deliberate, not accidental. A fact about member A is not in member B's agent's context unless the permission matrix and the Messenger both say it belongs there.

## What this architecture explicitly does not try to do

- **It does not build a semantic knowledge graph.** Facts are structured records with anchors (subject, archetype, confidence), not nodes in a graph with typed edges. The harvester is not a graph builder; it is a reconciler.
- **It does not attempt to re-derive the present from history each turn.** The Living State is the agent's working picture, and it is refreshed at compaction. Turn-to-turn, the agent reads the Living State and the recent Ledger, and reaches older entries through `remember`.
- **It does not prevent all stale facts.** A fact that was true but stops being true without an explicit contradiction in a later span can persist. This is an acceptable failure mode — the fix is the user saying *"actually that's not the case anymore"*, which the harvester will pick up on the next boundary as an `update`.
- **It does not attempt turn-level extraction of "anything the agent might need to know."** The harvester extracts what is durable. Transient turn-level context (what the user asked three messages ago) lives in the recent history, not in the Facts store.

## Raw-history recall: an architectural upgrade

`KERNOS_MEMORY_RAW_SEARCH=1` enables lexical search of closed conversation logs inside `remember` (the default). Setting it to `0` retains Ledger, fact and entity retrieval for comparison. Raw search requires a member and never falls back to legacy shared logs. It uses the existing BM25 implementation with an in-process cache of parsed closed files, invalidated when their names, sizes or modification times change; there is no persistent index, extra model call or embedding requirement.

Returned windows are labelled as historical reference. The final output shares a 1,500-token budget (the project's 6,000-character estimate), including wrappers and source pointers. Raw evidence receives room before facts can fill the response. Ordinary windows are returned whole; an oversized window gets an explicitly marked excerpt centred on matching terms, with its speaker and source preserved and a `remember_details` pointer to the full conversation. Up to three distinct windows fit; the budget can reduce that number.

This is a lexical baseline, not a semantic search or a resolution of evolving truth. Token matching follows the existing ASCII word/number tokenizer; paraphrases and non-ASCII-only queries can miss. Recall quality is measured separately from the isolation and source-preservation checks.

## Living State: cohesive continuation without recursive prose

This is an architectural upgrade from the original rewritten Living State. `KERNOS_MEMORY_THREAD_STATE=1` enables it for member-scoped compaction (the default). An unmigrated data copy with `0` retains the previous writer for comparison. A migrated register is never discarded by disabling the flag. Anonymous legacy calls retain their old contract and do not acquire access to member source folders.

The durable layer holds real commitments and adopted decisions that matter to the space's intent, including established creative premises. Code assigns evidence IDs to exact passages in physical source turns, preserving speaker, time, space, log, turn and offsets. The model selects short source labels local to each call; code resolves them to the original coordinates and copies their words. An accepted item's text is immutable. OPEN and CLOSE events are appended; age, omission from a summary, and a display cap never close an item.

A separate semantic verifier checks proposals against the quoted sources and their context. A real citation alone is not support. Unknown source IDs receive one correction attempt; repeated invalid IDs abort the affected operation instead of committing its output. Speaker and channel are preserved: internal plan dispatches labelled as user turns cannot establish a human choice, user commitment or completion confirmation. Decisions require user adoption; completion needs user confirmation or a completed receipt, not an unsupported assistant claim. Partial completion and uncertain identity leave an obligation open. The verifier also checks narrative claims. These semantic judgments remain fallible: missed or false openings and wrong or missed closures must be measured, not inferred from source validity or verbatim survival.

The short narrative is generated from recent source passages, the topical Ledger and selected verified open and newly resolved items. Original partial-progress passages are retrieved when checking later completion; completed and cancelled plans carry both their opening and closing evidence. Source words are shared once in verifier input, with candidates referring to their IDs. The narrative never receives its previous version. It should retain purpose, salient current context and what a participant needs to continue; flippant examples and incidental colours may properly disappear. Detail-access recall and cohesive compaction are evaluated separately.

Migration retains the entire original summary as explicitly unverified history. It reviews the member's whole source history in this space and its permitted ancestors in bounded, pinned batches. Early openings remain provisional until all later evidence through the cutoff has been considered. Unrelated sibling conversations are excluded; a closure recorded only there can remain stale until relevant evidence reaches the permitted scope. Unresolved legacy text remains searchable through `remember`, labelled unverified rather than promoted to current truth.

The rendered thread section is capped at 6,000 characters, including recent resolutions, and the narrative at 1,600. An overflow indicator names omitted open items; their records remain stored and searchable with authoritative status and opening/closing sources. Subject-specific open-item queries rank matching records; a bare overview returns the newest three. This lookup is lexical, so paraphrases can miss. Reconciliation considers all persisted open records in bounded batches, including items hidden from the per-turn view. The original legacy summary is retained for explicit lookup rather than repeatedly injected into current context.

One versioned metadata block in `active_document.md` commits thread events, source frontiers, narrative, counters and retry-required trailers together by atomic replacement. Source frontiers include physical turn counts and prefix digests from the same reads as the evidence. Interrupted migrations retain their original fresh-span classification and resume their pinned source batches; retries reuse committed work or process a newly appended suffix. `state.json` is repairable bookkeeping; a current cache retains later retry/backoff and runtime updates. Ledger repair preserves the metadata block byte for byte. This does not promise exactly-once delivery of the scheduler's existing follow-up side effects.

## Related architecture

- **[Context spaces](context-spaces.md)** — how the Ledger and Facts stores are partitioned per-space
- **[Multi-member disclosure layering](disclosure-and-messenger.md)** — how `sensitivity` and `owner_member_id` are consumed downstream
- **[Cohort architecture](cohort-and-judgment.md)** — the fact harvester and the compaction service as two of the six cohorts

## Code entry points

- `kernos/kernel/fact_harvest.py` — `harvest_facts`, the boundary-triggered reconciling entry point, with the primary prompt and its `add`/`update`/`reinforce` schema; `harvest_log_tail` for space switches
- `kernos/kernel/compaction.py` — `CompactionService.compact_from_log` (append-only compaction), `load_context_document` (what the agent sees), `load_all_ledger_entries`, `rebuild_missing_entries`
- `kernos/messages/phases/persist.py` — `run_boundary`, the after-the-reply trigger for compaction and harvest
- `kernos/kernel/retrieval.py` — `RetrievalService.search` (`remember`), `_search_ledger`, `bm25_rank`
- `kernos/kernel/raw_recall.py` — neighbour-window BM25 search and bounded historical excerpts
- `kernos/kernel/conversation_log.py` — `read_historical_turns`, member-scoped closed-log reads and parsed-file cache
- `kernos/kernel/living_state.py` — supported thread transitions, bounded rendering, source migration and retry metadata
- `kernos/kernel/reasoning.py` — `_handle_remember_details`
- `kernos/kernel/state.py` — `KnowledgeEntry.owner_member_id`; the per-member scoping
