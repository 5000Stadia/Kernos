# ROUTER-EVIDENCE-V1 — Implementation Approach

**Status:** DRAFT v3 — pre-implementation, two Codex review rounds folded 2026-05-09. v1 review (six findings): member-scoped Layer 3 index, evidence-aware global fallback, `read_recent()` reuse, shared candidate selector, honest framing of Layer 3 as a second-stage cohort call + derived substrate, schema-in-store convention. v2 review (four design answers + five additional risks): candidate-selector backward-compat fallback, ledger-tail ordering + n-clamp, `token_budget` API correction + final formatted-string cap, slot-reservation for current focus instead of numeric tie-breaker, /dump-bypass evidence short-circuit, per-space fail-open in evidence loader, prompt-contents assertion in tests over LLM-citation, member-isolation test in 2.2. Sequencing locked: ship 2.1 + 2.2 together, defer 2.3 to a follow-up.
**Spec source:** Notion page `35cffafef4db814ca1cfeac4ac922144` (📢 ROUTER-EVIDENCE-V1 — feed the router cohort what the substrate already produces).
**Deliberation source:** Notion page `35bffafef4db8127af4bda14c01a52af` (🤔 ROUTER-EVIDENCE-V1 — surface compaction outputs and lexical anchors to the routing cohort).
**Composes-with prior art:** PAGE-SEARCH-TOKEN-OVERLAP-V1 (commit `4065341`, `kernos/kernel/canvas.py:1495`).

This document specifies the concrete code-level shape of ROUTER-EVIDENCE-V1: which seams change, what data each layer reads, where caps live, how the existing router prompt is extended, and what each sub-batch's tests look like. The Notion spec carries the doctrine, invariants, and Phase 1 audit framing — this document is the implementation contract.

## Scope

Sequenced sub-batches that wire substrate-derived evidence into the existing single LLM router call. No changes to the routing model, no new gardener, no description mutation, no two-pass logic, no new substrate.

| Sub-batch | What it adds | Surfaces touched |
|---|---|---|
| 2.1 | Recent activity tail per candidate space | `router.py` (read), `route.py` (loader call) |
| 2.2 | Compacted Living State + last N Ledger entries per candidate space | `router.py` (read), `compaction.py` (helpers) |
| 2.3 | Lexical anchor extraction + per-space inverted index | `router.py` (read), new `kernos/kernel/space_index.py`, `conversation_log.py` (incremental indexer hook), tiny LLM prelude in `LLMRouter.route` |

Each sub-batch is independently shippable. Heat-map findings from the optional Phase 1 audit may stop sequencing early.

## Architectural anchors

- **Routing-cohort discipline:** Layers 1 and 2 are zero-LLM-cost (disk reads, single router call). **Layer 3 is explicitly a second cohort-stage LLM call** — the anchor-extraction prelude is a small dedicated cohort step, not a transparent enrichment. The spec accepts this cost in exchange for the long-tail recall win, and Layer 3 is feature-flagged so it can be disabled wholesale.
- **Layer 3 introduces derived substrate.** The per-space inverted index is new derived state (rebuildable from conv-log + files at any time). It is NOT source-of-truth substrate — but it IS a new persistence surface, and the spec treats it as one (member-scoped, schema-owned by `SpaceIndex`, fail-open on update failure).
- **Bounded prompt invariant:** Per-space caps enforced inside the prompt builder. A global ceiling (default 8K tokens for the bundles section) triggers a candidate-filter fallback when many spaces exist. **Ranking for the fallback uses cheap evidence overlap (recent-tail + living-state token overlap with the new message), NOT static-description overlap** — descriptions are the signal we are explicitly distrusting, and using them as the fallback ranker would silently re-defeat Layer 2's purpose. **Current focus is handled via slot-reservation, not a numeric tie-breaker:** always reserve one slot for `current_focus_id` (so the active space is never dropped from the bundle), then rank the remaining slots by evidence overlap. (Codex review 2026-05-09 v2 finding 4.) Avoids tuning a magic-number bonus.
- **Substrate-respecting:** Layers 1 and 2 read existing files (`active_document.md`, conv-log files) via existing public APIs. No mutation of source-of-truth substrate.
- **Static descriptions stay static.** This spec never rewrites `ContextSpace.description`.
- **Fail-open on Layer 3 failures.** Anchor extraction errors, index-lookup errors, or index-update errors are logged + swallowed — they never block the main router call or the conv-log write that triggered them. Layers 1 and 2 carry routing on their own.

## File-level call map

```
kernos/messages/phases/route.py
    run()
        recent_full = handler.conversations.get_recent_full(...)        # existing
        candidates = await list_route_candidate_spaces(                 # NEW shared helper
            state, instance_id, member_id
        )
        bundles = await build_space_evidence(                           # NEW (2.1+)
            handler, instance_id, member_id, candidates
        )
        ctx.router_result = await handler._router.route(
            ..., candidate_spaces=candidates,                           # NEW arg
            space_evidence=bundles                                      # NEW arg
        )

kernos/kernel/router.py
    LLMRouter.route(..., candidate_spaces, space_evidence)
        # Candidate set is now passed in (computed once, shared with route.py).
        # Layer 1 (2.1): recent_tail per space already in `space_evidence`.
        # Layer 2 (2.2): living_state + ledger_tail per space already in `space_evidence`.
        # Layer 3 (2.3): pre-call anchor extraction → algorithmic index lookup.
        try:
            anchors = await _extract_anchors(message_content)           # NEW (2.3) — fail-open
            hits = await self._space_index.lookup(                      # NEW (2.3) — fail-open
                instance_id, anchors, member_id=member_id
            )
        except Exception:
            anchors, hits = [], []                                       # log + continue
        prompt = _build_prompt(candidate_spaces, space_evidence, hits, ...)
        result_str = await self._reasoning.complete_simple(...)         # main router call

kernos/kernel/space_candidates.py                                        # NEW (2.1) — shared helper
    async def list_route_candidate_spaces(
        state, instance_id, member_id
    ) -> list[ContextSpace]:
        """Single source of truth for "which spaces does the router consider for
        this member's message". Mirrors the filter at router.py:142.
        Used by both route.py (to build evidence) and the router (to bundle).
        """

kernos/kernel/space_evidence.py                                          # NEW (2.1, 2.2)
    async def build_space_evidence(
        handler, instance_id, member_id, candidate_spaces, message_content
    ) -> dict[str, SpaceEvidence]:
        # Per-space: read recent conv-log tail (via ConversationLogger.read_recent —
        #   it already handles multiline entries, token budget, missing logs,
        #   member scope, chronological return — so we don't reinvent the tail
        #   parser). Then read compaction document, extract Living State and
        #   most-recent Ledger entries via NEW public CompactionService helpers.
        # Apply per-space caps.
        # If aggregate exceeds GLOBAL_BUNDLE_CEILING: rank candidates by
        #   evidence-overlap (recent_tail + living_state token-overlap with the
        #   new message) + sticky-current-focus bonus, keep top-N.

kernos/kernel/space_index.py                                             # NEW (2.3)
    class SpaceIndex:
        def __init__(self, instance_db_path)                             # owns its own connection
        async def ensure_schema()                                        # CREATE TABLE IF NOT EXISTS
        async def update(instance_id, space_id, member_id, text,        # NEW per-member key
                         source: str)                                    # "conv_log" | "file"
        async def lookup(instance_id, anchors,
                         member_id) -> list[SpaceHit]
        # Phrase + token-overlap ranking (PAGE-SEARCH-TOKEN-OVERLAP-V1 reuse).
        # All update/lookup paths fail-open — log + return empty.

kernos/kernel/compaction.py
    # NEW public helpers (replacing reach-into-private from a separate module):
    #   async def load_living_state(instance_id, space_id, member_id) -> str
    #   async def load_recent_ledger_entries(
    #       instance_id, space_id, member_id, n: int = 3
    #   ) -> list[str]
    # These wrap existing private extractors (_extract_living_state,
    # _parse_ledger_entries) and load_document() with the
    # member-scoped path resolution that already lives in _space_dir.

kernos/kernel/conversation_log.py
    # Existing read API reused (no signature change for 2.1):
    #   async def read_recent(instance_id, space_id, member_id, ...) — handles
    #     multiline entries, token budget, missing-log fallback. Use this; do
    #     NOT reinvent tail-parsing in space_evidence.py.
    # NEW for 2.3 only: post-append hook into SpaceIndex.update() — wrapped
    # in try/except so index update never blocks the conv-log write itself.
```

## Data shapes

```python
# kernos/kernel/space_evidence.py (NEW — 2.1+)

@dataclass(frozen=True)
class SpaceEvidence:
    space_id: str
    recent_tail: str = ""          # Layer 1 — last K conv-log entries, capped
    living_state: str = ""         # Layer 2 — capped
    ledger_tail: str = ""          # Layer 2 — last N entries joined, capped
    truncated: bool = False        # surfaced in router prompt for transparency

# Caps (defaults, env-overridable):
RECENT_TAIL_CAP_TOKENS   = 400
LIVING_STATE_CAP_TOKENS  = 500
LEDGER_TAIL_CAP_TOKENS   = 200
LEDGER_TAIL_ENTRIES      = 3
RECENT_TAIL_K            = 5
GLOBAL_BUNDLE_CEILING    = 8000   # triggers candidate filter when exceeded


# kernos/kernel/space_index.py (NEW — 2.3)

@dataclass(frozen=True)
class SpaceHit:
    space_id: str
    member_id: str                 # carry the scope so callers cannot misuse hits
    score: int                     # phrase_count * _PHRASE_BONUS + token_sum
    snippet: str                   # ≤ 120 chars around the highest-scoring anchor
    anchors_matched: tuple[str, ...]
```

## Prompt extension

The user-content block in `LLMRouter.route` (`router.py:183`) gains two new sections, both bounded:

```
Active spaces:
- {space_id}: {name}{markers}{hierarchy} — {description}
  · Recent activity (last 5):
    [ts] (role): {content[:200]}
    ...
  · Living State:
    {living_state[:cap]}
  · Recent Ledger entries:
    {ledger_tail[:cap]}
...

Anchor matches:
- Anchors extracted from new message: ["audit", "kernos-architecture", "shape-canvases"]
- Hits:
  · space_f2ec32b8 (System): 4 anchor hits — snippet: "...kernos-architecture-audit.md..."
  · space_f5c3079d (General): 1 anchor hit — snippet: "..."

Recent history:
...
```

The router prompt's existing rules already steer behavior; adding evidence does not require a system-prompt rewrite. We do append one sentence to clarify weighting:

> When evidence (recent activity, living state, ledger, anchor matches) contradicts a space's static description, weigh the evidence — descriptions are labels, not authoritative routing oracles.

## Sub-batch 2.1 — Recent activity tail wiring

**Goal:** the smallest, lowest-risk wiring change. After this batch, every routing call sees the last 5 conv-log entries from each candidate space.

**Files touched:**
- NEW: `kernos/kernel/space_candidates.py` — `list_route_candidate_spaces(state, instance_id, member_id)` extracts the existing visibility filter at `router.py:142` into a shared helper. Both `route.py` (for evidence build) and `LLMRouter.route` (for bundling) consume the same function.
- NEW: `kernos/kernel/space_evidence.py` — implements `build_space_evidence` (Layer 1 only at this point — Layer 2/3 fields default empty).
- `kernos/kernel/router.py` — `LLMRouter.route` accepts `candidate_spaces: list[ContextSpace] | None` and `space_evidence: dict[str, SpaceEvidence] | None`. When `candidate_spaces is None`, the router internally calls `list_route_candidate_spaces(...)` (same helper) — backward compat for tests and old call sites is preserved by the router doing the work itself when not supplied, NOT by reverting to the legacy inline filter. (Codex review 2026-05-09 v2 finding 1.) Prompt builder includes `recent_tail` block per space when present.
- `kernos/messages/phases/route.py` — `run()` calls `list_route_candidate_spaces`, builds `space_evidence`, passes both into the router call.

**Implementation detail:** Use `ConversationLogger.read_recent(instance_id, space_id, member_id=..., token_budget=RECENT_TAIL_CAP_TOKENS)` for per-space tail reads. `read_recent()` already handles multiline entries, token budget, missing logs, member scope, and chronological order — re-implementing tail parsing in `space_evidence.py` would be a regression. (Codex review 2026-05-09 v1 finding 3.) `read_current_log_text()` is the wrong API here — it raises on missing logs and returns a tuple.

**Final-cap detail:** `read_recent()` accepts `token_budget=...` (NOT `max_tokens=...`) and may return one oversized entry that exceeds the budget. `space_evidence.py` MUST apply a final formatted-string-length truncation after rendering the tail to its prompt block, so an oversized last entry can't blow past the per-space cap into the global ceiling. (Codex review 2026-05-09 v2 finding 3.)

**Per-space fail-open:** evidence loading per space is wrapped in try/except. A read failure for one space logs and returns an empty `SpaceEvidence` for that space; routing continues with whatever evidence loaded successfully. (Codex review 2026-05-09 v2 risk B.)

**/dump and /status diagnostic bypass MUST short-circuit evidence build.** The bypass at `route.py:62` already routes diagnostic slash-commands to current focus without consulting the router cohort. Evidence loading is identically expensive and equally unnecessary in that path — `route.py:run` MUST check the bypass condition BEFORE calling `build_space_evidence`. (Codex review 2026-05-09 v2 risk A.)

**Rollback:** Pass `space_evidence=None` (or feature-flag via `KERNOS_ROUTER_EVIDENCE_LAYER1=0`).

**Embedded live tests:**

Test shape note: tests assert against the **captured router prompt contents** (Layer 1 evidence appears in the prompt) plus the **mocked router result routes correctly** — NOT against a real LLM citation. The router has no rationale field today and we don't add one in this PR. (Codex review 2026-05-09 v2 risk C.)

1. **Substrate-fidelity probe:** in a 2-space instance, post recent activity matching the message topic into Space A's conv-log. Assert the captured router prompt contains the expected `recent_tail` block for Space A. Mock the router LLM to return Space A; verify routing dispatches accordingly.
2. **Negative probe:** post the same recent activity into Space B (a non-target space). Assert the prompt also contains Space B's `recent_tail`. With a router-LLM mock that uses description-match heuristic, verify routing chooses the description-match space — i.e. the router still has agency over evidence; tail does not auto-route.
3. **Cap probe:** synthesize a long conv-log; verify per-space `recent_tail` fits within `RECENT_TAIL_CAP_TOKENS` after the final formatted-string cap, and `truncated=True` surfaces in `SpaceEvidence`.
4. **/dump bypass probe:** send `/dump` from a non-current-focus space; verify `build_space_evidence` is NOT called (the bypass short-circuits evidence build per route.py:62). Mocked or counted via a spy on the evidence builder.
5. **Per-space fail-open probe:** synthesize a `read_recent` that raises for one of three spaces. Verify the other two spaces' bundles populate normally and routing continues. Logged at WARN; never raises out of `build_space_evidence`.

## Sub-batch 2.2 — Compacted summaries wiring

**Goal:** Living State + last 3 Ledger entries per space surfaced to router.

**Files touched:**
- `kernos/kernel/compaction.py` — add two new public methods:
  - `async def load_living_state(self, instance_id, space_id, member_id="") -> str` — wraps `load_document()` + `_extract_living_state()`. Returns empty string if document missing.
  - `async def load_recent_ledger_entries(self, instance_id, space_id, member_id="", n=3) -> list[str]` — wraps `load_document()` + `_parse_ledger_entries()`. Returns the **last n entries in document order, oldest-to-newest among the tail** (i.e. `entries[-n:]`). **Clamp `n <= 0` to `[]`.** (Codex review 2026-05-09 v2 finding 2.) `member_id=""` parity with `load_document()`.

  `space_evidence.py` calls only these public methods — no reaching into private compaction internals from a sibling module.
- `kernos/kernel/space_evidence.py` — populate `living_state` and `ledger_tail` fields via the new `CompactionService` public helpers when documents exist for `(instance_id, space_id, member_id)`. Member scope is mandatory; the existing `_space_dir` member-scoped path is the disclosure boundary that prevents the scenario-04 leak path.
- `kernos/kernel/router.py` — prompt builder appends `living_state` and `ledger_tail` blocks per space when present.

**Implementation detail:** When both `recent_tail` and compacted summaries exist, prefer including the Living State (more authoritative current-truth) and trim `recent_tail` to a smaller cap (default 200 tokens) to stay under per-space ceiling. If `living_state` is empty (space hasn't compacted), keep full `RECENT_TAIL_CAP_TOKENS` for `recent_tail`.

**Rollback:** Feature-flag via `KERNOS_ROUTER_EVIDENCE_LAYER2=0`.

**Embedded live tests:**

Same prompt-contents-plus-mocked-router shape as 2.1 — assert the captured prompt contains expected Living State + ledger blocks; the mocked router routes accordingly.

1. **Compaction-bridges-the-gap probe:** create a space with conversational activity from 100+ turns ago (out of recent tail). Trigger compaction. Send a message related to that activity. Assert prompt contains the Living State block; mocked router routes to the compacted space.
2. **Description-vs-Living-State conflict probe:** a space whose description is generic ("System config") but whose Living State details specific audit work. Assert both blocks appear in the prompt; mocked-router-with-evidence-weighing chooses the Living-State-match space.
3. **Member-isolation probe (NEW):** Member A and Member B both have compacted documents in the same shared/system space. Verify Member A's evidence bundle for that space surfaces only Member A's `active_document.md` content via the existing `_space_dir(instance_id, space_id, member_id)` boundary. Member B's content must never appear. (Codex review 2026-05-09 v2 risk D.)
4. **Cap probe:** Living State exceeding cap is truncated; Ledger truncation preserves most-recent entries via `entries[-n:]`; `n=0` returns `[]`; `truncated=True` surfaces.
5. **Layer-1+Layer-2 cap interplay probe:** when Living State is non-empty, `recent_tail` is trimmed to ~200 tokens; when Living State is empty, `recent_tail` gets full 400 tokens.

## Sub-batch 2.3 — Lexical anchor retrieval

**Goal:** when neither recent activity nor compacted summaries pin a space, distinctive tokens in the new message lookup against per-space inverted indexes.

**New module:** `kernos/kernel/space_index.py`.

**Index storage:** SQLite table owned by `SpaceIndex` (mirrors the in-store schema convention used by `instance_db.py` rather than a separate `kernos/migrations/` framework, which doesn't exist in this repo). `SpaceIndex.ensure_schema()` runs `CREATE TABLE IF NOT EXISTS` on first use and any guarded `ALTER TABLE` for forward changes — same pattern as `InstanceDB.connect()`. Member ID is part of the primary key; lookups are member-scoped to prevent another member's lexical anchors from leaking into routing for shared/system/legacy spaces (Codex review 2026-05-09 finding 1 — same disclosure boundary that compaction enforces via `_space_dir`).

```sql
CREATE TABLE IF NOT EXISTS space_token_index (
    instance_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    member_id TEXT NOT NULL DEFAULT '',  -- '' = unowned/legacy/system
    token TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT NOT NULL,
    sample_snippet TEXT,
    source TEXT NOT NULL DEFAULT 'conv_log',  -- 'conv_log' | 'file'
    PRIMARY KEY (instance_id, space_id, member_id, token)
);
CREATE INDEX IF NOT EXISTS idx_space_token_lookup
    ON space_token_index(instance_id, member_id, token);
```

Lookup MUST filter by `member_id` (the requesting member's id, plus the empty-string scope for unowned/system rows the member is allowed to see — same visibility shape as `list_route_candidate_spaces`).

**Update path:** `conversation_log.py` append flow gets a hook — after the conv-log line is written, call `space_index.update(instance_id, space_id, member_id=..., text=..., source="conv_log")`. **The hook is wrapped in try/except so an index-update failure NEVER blocks or unwinds the conv-log write itself** (Codex review 2026-05-09 finding 5 — fail-open). Tokenization mirrors `canvas.py:1526` (`re.split(r"\W+", text)`), lowercased, dropping single-char tokens. File-write paths additionally call `space_index.update(..., source="file")` on file titles + first-line description at create/update time.

**Knowledge-entry indexing is deferred for v1** (Codex review 2026-05-09 recommendation). Visibility rules for `owner_member_id` + `sensitivity` warrant their own design pass before knowledge content feeds the router cohort. Conv-log + files are the v1 sources.

**Anchor extraction:** a small dedicated cohort-stage LLM call ahead of the main router call — `_extract_anchors(message_content) -> list[str]`. Schema enforces 3-5 distinctive lowercase tokens; uses `complete_simple` on `prefer_cheap=True` with `max_tokens=64`. Skipped when message is shorter than 4 tokens (continuation-style). **Failure is non-blocking — a raised exception is logged and treated as "no anchors", which collapses Layer 3 to a no-op for that turn.** This is a real second cohort step, not a transparent enrichment; the spec accepts that cost in exchange for long-tail recall, and Layer 3 is feature-flagged so it can be disabled wholesale.

**Lookup:** `space_index.lookup(instance_id, anchors, member_id) -> list[SpaceHit]`. Algorithmic — phrase count weighted by `_PHRASE_BONUS=100` plus token sum, sorted by `(phrase_count, token_sum)` tuple per the PAGE-SEARCH-TOKEN-OVERLAP-V1 contract. Filtered by member_id (plus the empty-string scope for unowned/system rows the member is allowed to see). Top 3 hits surfaced. Lookup failure is non-blocking — empty list returned, main router call proceeds.

**Files touched:**
- NEW: `kernos/kernel/space_index.py` — `SpaceIndex` class owns its own SQLite connection, schema (`ensure_schema()` with `CREATE TABLE IF NOT EXISTS`), `update`, and `lookup`. Schema lives in the store, not in a separate migrations directory — matches the convention in `instance_db.py`. (Codex review 2026-05-09 finding 6.)
- `kernos/kernel/conversation_log.py` — append path calls `space_index.update(...)` post-write inside try/except (fail-open); never blocks the log write.
- File-write paths (workspace tools, attachment ingestion) — call `space_index.update(..., source="file")` at create/update time. (Locate via grep for existing `_handle_file_upload` and workspace write_file paths.)
- `kernos/kernel/router.py` — `LLMRouter` constructor accepts a `SpaceIndex`. `route()` calls `_extract_anchors()` (gated by message length and `KERNOS_ROUTER_EVIDENCE_LAYER3` env), then `space_index.lookup(..., member_id=member_id)`, surfaces hits in prompt. Both calls wrapped in try/except.
- Backfill: one-shot script that walks existing conv-logs + file metadata per (instance, space, member) and feeds `space_index.update`. Lazy-rebuild on first router call per (instance, space, member) is an acceptable alternative for low-traffic instances.

**Rollback:** Feature-flag via `KERNOS_ROUTER_EVIDENCE_LAYER3=0`. Index is derived; can be dropped and rebuilt at any time.

**Embedded live tests:**
1. **Long-tail recall probe:** create six spaces, post distinctive named-entity content (e.g. "kernos-architecture-audit.md") into one. Send a message referencing that entity from a different focused space. Verify anchor extraction returns the entity, lookup pins the right space, router routes to it.
2. **Index-update probe:** post content, immediately verify the SQLite table contains the expected tokens for that (instance, space, member).
3. **Member-isolation probe:** two members in an instance, each posts distinctive tokens into a member-owned space. Verify Member A's lookup never returns Member B's space-private rows. Verify both members CAN see unowned/system-space rows. (Mirrors the disclosure boundary compaction enforces via `_space_dir`.)
4. **Tangential-anchor robustness probe:** a message with a token that incidentally exists in many spaces (e.g. "send"). Verify top hits are not nonsense — either Layer 3 yields no strong signal (router falls back to Layers 1/2) or the highest-frequency space wins legitimately.
5. **Fail-open probe:** synthesize a `SpaceIndex` that raises on `update` — verify conv-log writes still succeed. Synthesize one that raises on `lookup` — verify routing still completes via Layers 1/2 only.
6. **Cross-layer integration probe:** Layer 1 suggests Space A, Layer 2 suggests Space B, Layer 3 suggests Space C. Verify the router picks one defensibly and cites evidence (a tracing field or via post-hoc decision-log inspection).

## Phase 1 audit (optional precursor — architect's call)

If Phase 1 is ratified before Phase 2.1 implementation, the audit produces:

- **Counterfactual replay tool:** `kernos/tools/router_audit.py` — re-runs the router on historical messages with each layer's evidence injected, comparing to the originally-chosen space. Outputs a report per instance.
- **Failure-mode classifier:** label each routing miss as (a) recent-tail-would-fix, (b) summary-would-fix, (c) anchor-would-fix, (d) all three help, (e) none help.
- **Recommendation:** which sub-batches are highest-leverage; whether sequencing should be 2.1→2.2→2.3 or reordered.

This phase is read-only and produces no code changes to production paths.

## Bounded-cost math (worst case)

For an instance with 6 active spaces, all compacted, all evidence layers active:

| Source | Per-space tokens | × 6 spaces |
|---|---|---|
| Recent tail (when compacted, reduced cap) | 200 | 1.2K |
| Living State | 500 | 3.0K |
| Ledger tail (3 entries) | 200 | 1.2K |
| Anchor hits + snippets | shared | 0.5K |
| Anchor extraction prelude (output) | shared | 0.05K |

**Total bundle:** ~5.9K extra tokens on the cheap chain. Existing router context (~1K) brings total to ~7K. Well under the cheap-chain context budget.

When the global ceiling (8K) is exceeded — e.g. 12+ active spaces — fallback ranks candidate spaces by description-token overlap with the new message and bundles only the top 6.

## Adjacent fixes (drive-by)

- **`kernos/messages/phases/route.py:122`** — log line uses label `confident=%s` while the value is `ctx.router_result.continuation`. Mislabeled. Either rename the label to `continuation=%s` or thread a real confidence field. Cheap one-liner; ship as part of Batch 2.1 to keep the trace honest while we add structure to it.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Prompt bloat at scale (50+ spaces) | Global ceiling + candidate filter ranked by **evidence-overlap** (recent_tail + living_state token-overlap with the new message); current focus handled via **slot-reservation** (one slot guaranteed for `current_focus_id`), NOT a numeric tie-breaker. Static-description overlap is explicitly NOT used. (Codex review 2026-05-09 v1 finding 2 + v2 finding 4) |
| Lexical retrieval over-influencing routing | LLM weighs hits as evidence, not verdict; tangential-anchor test category |
| Inverted-index drift / staleness | Incremental update on conv-log append + file-write hooks; one-shot backfill |
| Anchor extraction adds latency | ≤ 64 max_tokens on cheap chain; gated by message length; feature-flag killswitch |
| Per-member compaction disclosure leakage | Reuse existing `_space_dir(instance_id, space_id, member_id)` member-scoped path; mirrors existing compaction disclosure boundary; never read another member's `active_document.md` |
| Layer 3 SQLite contention with main DB writes | Use existing instance-DB connection pool with `busy_timeout=5000`; index updates are small |
| Composition with `downward_search` | Layer 3 happens BEFORE routing; `downward_search` happens AFTER (when `query_mode=true`). Distinct stages — no overlap. Confirm during 2.3 implementation review. |

## Open implementation questions (for Codex review)

Resolved by Codex review 2026-05-09:
1. ✅ Separate `space_evidence.py` module (kept for testability).
2. ✅ Anchor-extraction skip when Layers 1+2 strong: deferred — adds complexity for marginal cost savings.
3. ✅ SQLite over JSON sidecar (schema owned by `SpaceIndex` per repo convention).
4. ✅ Knowledge-entry indexing deferred for v1 — visibility rules need their own design pass.
5. ✅ Lazy-rebuild acceptable; one-shot backfill recommended for predictability.

Resolved by Codex v2 review 2026-05-09:
6. ✅ Global fallback ranker — slot-reservation for current focus (no magic-number bonus), then evidence-overlap rank for remaining slots.
7. ✅ `list_route_candidate_spaces` lives in `kernos/kernel/space_candidates.py` (routing policy, not persistence).
8. ✅ Ledger-tail ordering — `entries[-n:]`, oldest-to-newest among the tail; `n <= 0` returns `[]`.
9. ✅ `read_recent` API — `token_budget`, not `max_tokens`; final formatted-string cap in `space_evidence.py` after rendering.
10. ✅ Tests assert prompt contents + mocked router result — not LLM rationale.
11. ✅ /dump and /status bypass short-circuits evidence build.
12. ✅ Per-space fail-open in evidence loader.
13. ✅ Member-isolation test for 2.2.

No remaining design opens for 2.1 + 2.2. Ready to implement.

## Sequencing

1. ✅ Spec drafted in Notion (`35cffafef4db814ca1cfeac4ac922144`).
2. ✅ Codex review of v1 implementation approach (six findings folded — this is v2).
3. 🟡 Architect ratification of v2 (this document).
4. Optional: Phase 1 audit (counterfactual heat-map).
5. Phase 2.1 implementation + Codex review (Codex flagged 2.1/2.2 as approve-after-fixes; 2.1 should ship cleanly with the v2 corrections).
6. Phase 2.2 implementation + Codex review.
7. Phase 2.3 implementation + Codex review (Codex held 2.3 in v1 review pending privacy/storage shape; v2 addresses both via member-scoped index + schema-in-store + fail-open).
8. Architect ratification on close.

Each Phase 2 batch is independently shippable behind a feature flag, allowing layered rollout in production instances.

## Linked artifacts

- Spec doc: Notion `35cffafef4db814ca1cfeac4ac922144`
- Deliberation: Notion `35bffafef4db8127af4bda14c01a52af`
- Algorithmic primitive (prior art): `kernos/kernel/canvas.py:1495` (PAGE-SEARCH-TOKEN-OVERLAP-V1, commit `4065341`)
- Compaction primitives: `kernos/kernel/compaction.py:499` (`load_document`), `:744` (Ledger parse), `:754` (Living State extract)
- Existing router: `kernos/kernel/router.py:114-230`
- Existing route phase: `kernos/messages/phases/route.py:26-202`
