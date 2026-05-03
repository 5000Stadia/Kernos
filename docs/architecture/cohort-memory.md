# Cohort: Memory

Second cohort adapter targeting the **COHORT-FAN-OUT-RUNNER**
contract. Decouples memory retrieval from being a model-decided
`remember`-tool call inside reasoning into a per-turn pre-fan-out
cohort that surfaces structured retrieval results to integration.

## What the adapter does

The memory cohort is a **push** surface. Every turn, it embeds the
user's raw message, runs parallel knowledge + entity searches via
`RetrievalService.search_structured`, and packages the results as
a `CohortOutput`. The existing `remember`-tool **pull** path stays
available — integration calls it during its prep loop when a more
specific re-query or archive depth is needed.

```
ctx.user_message
    │
    ▼
RetrievalService.search_structured(include_archives=False)
    │  collect (knowledge + entity, parallel)
    │  policy  (uniform disclosure gate across every payload source)
    │  rank    (quality_score = recency + confidence + reinforcement
    │           + space + foresight)
    │  budget  (top-5 knowledge, top-5 entities; truncation flags)
    ▼
RetrievalSnapshot
    │
    ▼
memory_cohort_run(ctx) → CohortOutput
    │
    ▼
CohortFanOutRunner → IntegrationRunner → presence
```

## Query strategy: Option A (raw message)

v1 uses the user's raw message text as the embedding query. No
LLM call inside the cohort. Cheap, fast, parallel-friendly, and
matches what `RetrievalService` already does well.

Options B (cohort-generated structured query via cheap LLM call)
and C (hybrid embed + explicit-referent extraction) are explicit
future work — gated on empirical signal that A's recall is
insufficient.

## search_structured: collect + policy + rank + budget-shape

Same internal pipeline as the legacy `search()` text path. The
two cannot drift — they share the entity / knowledge / state-
intercept / disclosure-gate logic via the same internal helpers.
The only difference is output shape (`RetrievalSnapshot` vs.
formatted text) and budget unit (count vs. tokens).

| Stage | Behavior |
|---|---|
| Collect | Embedding query against knowledge; parallel entity-graph search via `asyncio.gather`. Archive search gated by `include_archives` flag. |
| Policy | One disclosure gate, every payload source: semantic knowledge, entity-linked knowledge, MAYBE_SAME_AS notes, state intercept, archives (when included). |
| Rank | quality_score on knowledge; entity ordering by knowledge_count. quality_score is **retrieval-local semantics** — useful for ordering inside one retrieval, NOT a cross-cohort universal score. |
| Budget-shape | Top-5 knowledge / top-5 entities for structured surface; token budget for text surface. Truncation flags surface so integration can decide whether to re-query. |

## Output payload

```
CohortOutput {
  cohort_id: "memory"
  cohort_run_id: "{turn_id}:memory:provisional"  # runner re-mints
  visibility: Public
  output: {
    query_used: str
    retrieval_attempted: bool  # False on embedding failure
    knowledge: List[KnowledgeSummary]
    entities: List[EntitySummary]
    archive_summary: ArchiveSummary | None  # always None in cohort path
    state_intercept: str | None  # populated only when source=state_intercept
    source: "normal" | "state_intercept"
    truncation: {
      knowledge_truncated: bool
      entities_truncated: bool
      ...
    }
  }
}

KnowledgeSummary {
  entry_id: str
  content_short: str  # truncated to 300 chars
  authored_by: str
  created_at: ISO 8601
  quality_score: float  # retrieval-local
  source_space_id: str
}

EntitySummary {
  entity_id: str
  name: str
  entity_type: str
  knowledge_count: int  # post-disclosure-filter
  uncertainty_notes: list[str]  # MAYBE_SAME_AS partner names that survived gating
}
```

## Visibility model

Whole-output `Public`. Per V1 invariant + gardener precedent:

- **Disclosure gate filters at source.** Entries the active
  member can't see are absent from the payload entirely (not
  marked, not redacted, just absent).
- **No payload-level visibility overrides.** Mixed-visibility
  cohort data is explicit future work, not a payload-level
  smuggle.

## Uniform disclosure gate

Per the design review edit #4. Inheritance from `_search_knowledge` was not
sufficient — entity resolution was pulling linked KnowledgeEntry
objects without the `requesting_member_id` filter, and
`_collect_maybe_same_as` had no member-aware filtering at all.
`search_structured` builds a permission map once at the top and
threads it through every payload source:

| Source | Gating |
|---|---|
| Semantic knowledge | Filtered before space scoping (existing) |
| Entity-linked knowledge | Filtered after merge of SAME_AS edges (added) |
| MAYBE_SAME_AS notes | Pair dropped if other-node has no visible knowledge (added) |
| Archives (when `include_archives=True`) | Existing scope-chain filter; archive content respects active member's space scope |
| State intercept | Existing intercept logic (relies on `inspect_state` already respecting member id) |

One policy. Every source. Negative cases tested per source.

## Archives excluded from cohort path

Per the design review edit #2. `_search_archives` calls `reasoning.complete_simple`
twice (Haiku) to select and extract archives. Including archive
search in the per-turn cohort would silently add LLM cost to every
Kernos turn — violating the cohort's no-LLM-call invariant.

v1 decision:
- Cohort path passes `include_archives=False`. The archive task
  is never even spawned. `compaction.load_index` is not called.
- Legacy `remember`-tool path passes `include_archives=True`.
  Existing archive coverage preserved.
- Integration accesses archive depth via `remember` during its
  prep loop when needed.

The complementary push/pull split is preserved. The cohort handles
always-on knowledge/entity awareness; `remember` handles depth
retrieval including archives. Future spec opportunity: a non-LLM
archive index (embedding over archive metadata) would let v1's
exclusion be revisited without paying the LLM cost.

## Empty / error distinction

Per the design review edit #6. Three distinct cases with distinct handling:

| Case | retrieval_attempted | knowledge/entities | runner outcome |
|---|---|---|---|
| Embedding/vector failure | `False` | `[]` | success |
| Searched, nothing matched | `True` | `[]` | success |
| Other unexpected bug | propagates | propagates | error |

The cohort's only `try/except` for graceful-empty is around the
embedding service call (handled inside `RetrievalService` itself).
Database errors mid-search, schema mismatches, programming errors —
all propagate. The runner catches them and produces `outcome=error`
with redacted `error_summary`. Don't blanket-swallow internal
failures.

## State intercept short-circuit

Per the design review edit #5 / spec Section 2b. When the preference/state
intercept fires (queries containing keywords like "preference",
"setting", "notification", etc.), `search_structured` returns a
snapshot with:

- `source: "state_intercept"`
- `state_intercept: "[Structured state — authoritative]\n..."`
- `knowledge: ()`, `entities: ()`, `archive: None`
- `retrieval_attempted: False` (no semantic search ran)

Mixing authoritative state with recalled memory blurs provenance
— the short-circuit prevents that. Integration's filter phase
reads `source: "state_intercept"` and treats the intercept as
authoritative state, separate from recalled memory.

## Cohort descriptor

| Field | Value |
|---|---|
| `cohort_id` | `"memory"` |
| `execution_mode` | `ASYNC` |
| `timeout_ms` | `1500` (embedding + parallel search; sub-second in practice) |
| `default_visibility` | `Public` |
| `required` | `False` (memory absence is non-fatal) |
| `safety_class` | `False` |

## What this adapter does NOT change

- Existing `RetrievalService.search()` public signature unchanged.
  All 55 existing test cases pass without modification (acceptance
  criterion 19).
- `remember`-tool dispatch in `ReasoningService` unchanged.
  Existing archive coverage preserved via `include_archives=True`
  in the legacy text path.
- V1 `CohortOutput` schema unchanged. No per-item visibility
  extension.

## Architectural placement

```
kernos/kernel/cohorts/
└── memory_cohort.py        # the adapter (this spec)

kernos/kernel/retrieval.py
├── RetrievalSnapshot       # frozen dataclass
├── KnowledgeMatch / EntityMatch / ArchiveMatch
└── search_structured()     # collect + policy + rank + budget-shape
```

## Path forward

- **COHORT-ADAPT-PATTERNS** — surface pattern heuristics
  standalone (currently inside gardener).
- **COHORT-ADAPT-COVENANT** — decouple covenant validation from
  its post-write state hook; safety-class.
- **PRESENCE-DECOUPLING + INTEGRATION-WIRE-LIVE** — wire the full
  pipeline.
- **Future Option B/C query strategies** — promoted from
  scope-out to spec when empirical signal warrants the LLM cost.
- **Future non-LLM archive index** — would let the cohort surface
  archive depth without paying per-turn Haiku.
