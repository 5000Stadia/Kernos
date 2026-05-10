# FRICTION-PATTERN-STABLE-IDS-V1 — Implementation Spec

**Status:** DRAFT — pre-implementation. Architect provided framing 2026-05-10
(Notion `35cffafef4db81d0ad9ef705c802d313`); CC drafted spec body. Open for
Codex pre-spec review.

**Author:** CC, 2026-05-10. Resolves the architect's listed leans + four open
architectural questions; surfaces all substrate-side decisions explicitly.

**Source framing:** PHASE-3-AUTONOMY-LOOP design consideration (Notion
`35cffafef4db81da8107e562307bc738`) — friction-driven self-improvement
architecture. This spec is piece #1 of that arc: stable pattern IDs make the
loop's measurement layer real instead of vibes-based.

**Composes with:** Friction Observer V1 (`kernos/kernel/friction.py` —
existing signal detector that this spec layers patterns on top of);
RESPONSE-FIDELITY-V1 (every catalog op produces ActionStateRecord); future
WORKFLOW primitive integration (a separate spec; this one keeps trigger logic
out of scope so the workflow spec can pick it up cleanly).

**Sister-spec architectural template:** REFERENCE-PRIMITIVE-V1
(`kernos/kernel/reference/catalog.py`) — sqlite-backed catalog over
`instance.db`, dataclass + DDL + indexes, lifecycle states (tombstoned),
trust tiers. The friction pattern catalog mirrors this shape closely.

## What this spec ships

A new substrate primitive: a per-instance catalog of stable, named friction
patterns plus the wiring that ties existing friction reports to them. Seven
deliverables:

1. **`friction_pattern` table in `instance.db`** — sqlite-backed catalog
   with stable IDs, lifecycle state, parent grouping, time-window-queryable
   occurrence counter.
2. **`FrictionPattern` dataclass + `FrictionPatternStore` in
   `kernos/kernel/friction_patterns.py`** — public API mirroring
   `kernos/kernel/reference/catalog.py:CatalogStore` shape.
3. **Slug-based pattern ID generator** — collision-free; deterministic for
   the same seed text.
4. **Hybrid auto-classifier** — feeds new `FrictionSignal` instances through
   pattern matchers; auto-tags above confidence threshold; surfaces
   low-confidence as "needs manual classification" friction reports.
5. **Pattern lifecycle state machine** — `active` / `resolved` /
   `reactivated`; reactivation behavior pinned per architect lean.
6. **Spec frontmatter convention** — `addresses_friction_patterns: [...]`
   field; minimal parser for loop-closure verification reading from `specs/`
   directory.
7. **Embedded live tests** — substrate-fidelity assertion pattern: catalog
   round-trip, auto-classify behavior, reactivation. Member-isolation test
   per the disclosure-boundary discipline.

## What this spec does NOT ship

Per the architect's explicit framing:

- **Workflow primitive integration.** Triggers like "when pattern X frequency
  > threshold, fire workflow Y" are NOT here. The workflow spec consumes the
  catalog's frequency-query API; defining workflow trigger semantics is
  out of scope.
- **Restart-apply / completion-receipt protocol.** Separate spec.
- **Auto-pattern-creation from unclassified frictions at scale.** Manual
  creation by IA / operator suffices for v1's volume (~10 friction reports
  in the current data dir; cataloging by hand once is fine).
- **Cross-instance pattern aggregation as stored value.** Per-instance
  counter + cross-instance query via summing across `data/<instance>/`
  directories. Stored cross-instance counters introduce sync issues that
  v1 doesn't earn.
- **Workshop-V1 / autonomous spec drafting.** This spec is built by CC
  through the standard architect-framed handoff; it is NOT itself produced
  by the loop the catalog enables.

## Architectural decisions (resolving the architect's leans)

The architect's spec build directive listed seven decisions with leans.
Each is resolved below; deviations from the lean are flagged.

### Decision 1 — Pattern catalog structure (storage + ownership)

**Resolution:** sqlite-backed catalog rows in the existing `instance.db`,
mirroring `kernos/kernel/reference/catalog.py:CatalogStore` exactly. New
table `friction_pattern`. New module `kernos/kernel/friction_patterns.py`
exposing `FrictionPatternStore` with its own `aiosqlite` connection
(per-module-isolation pattern; `KERNEL-TOOL-REGISTRY-V1` sister convention).

`data/<instance>/friction_patterns/` is **not** a separate filesystem
directory. The catalog state lives in `instance.db` exclusively. Existing
`data/<instance>/diagnostics/friction/*.md` reports remain the durable
human-readable evidence; the catalog stores the structured layer (pattern
ID, occurrence counts, lifecycle) that the markdown reports lack.

**Aligns with architect's lean** (the lean said "probably under
`data/<instance>/friction_patterns/`" but per-instance sqlite is the
established pattern for structured state since SQLITE-BACKEND-V1 shipped
2026-04-12 — keeps state queryable, schema-managed, atomic).

### Decision 2 — Pattern ID format

**Resolution:** human-readable slug, kebab-case, lowercase, ASCII-only.
Example IDs: `compaction-fails-when-canvas-empty`, `tool-request-for-surfaced-tool`,
`integration-timeout-on-large-payload`.

Generator: `slugify(seed: str) -> str` — strips non-alphanumeric, lowercases,
collapses runs of non-alphanumeric to single hyphen, trims leading/trailing
hyphens. Collision detection: `FrictionPatternStore.create_pattern` queries
for existing rows with the candidate ID; on collision, suffixes a numeric
disambiguator (`-2`, `-3`). Deterministic for the same seed; collision-free
across the catalog.

Aliases: each pattern carries a `aliases: list[str]` field for renamed
patterns. Lookups check `id` first, then `aliases`. Renaming a pattern
moves the old ID to `aliases` and assigns a new `id`; counters and
lifecycle continue under the new ID without resetting frequency history.

**Aligns with architect's lean** (slug + aliases).

### Decision 3 — Friction-report → pattern tagging (hybrid classifier)

**Resolution:** hybrid auto+manual per architect lean. Implementation shape:

- **Auto-classify pass (per-friction-report):** the existing
  `FrictionObserver._write_report` path adds a classifier hook. The
  classifier compares the new `FrictionSignal` against established
  patterns using two cheap signals — first `signal_type` exact-match
  against pattern's `signal_type_keys` set (high confidence; deterministic),
  then a token-overlap score against pattern's `description` field
  (medium confidence; same algorithmic primitive as
  PAGE-SEARCH-TOKEN-OVERLAP-V1 in `kernos/kernel/canvas.py:1495`).

- **Confidence threshold:** auto-tag if either (a) `signal_type` exact
  match OR (b) token-overlap score above threshold (default
  `KERNOS_FRICTION_CLASSIFIER_THRESHOLD=0.6` — operator-tunable env var).
  Below threshold: leave `pattern_id` unset on the report; emit a new
  `friction.pattern_unclassified` event so a higher-tier observer (or
  operator) can hand-classify later.

- **Manual classification:** new tool `classify_friction_report(report_path,
  pattern_id)` exposed for operator + IA. Updates the report's metadata
  and bumps the pattern counter.

- **No LLM call on the classifier hot path.** The architect's framing flags
  "auto-classify with manual confirmation for new patterns." We do that
  via the threshold gate, not via per-report LLM classification — avoids
  adding a per-friction-event cohort call.

**Tightens the architect's lean** by pinning the auto-classifier to
deterministic + algorithmic signals rather than an LLM cohort call. Rationale
in the "Open architectural questions" section below.

### Decision 4 — Pattern grain (broad vs narrow with parent grouping)

**Resolution:** narrow patterns with `parent_pattern_id` field for hierarchy.
Example seed catalog (drawn from existing friction.py signal types):

| Parent | Child(ren) |
|---|---|
| `tool-surfacing-mismatch` | `tool-request-for-surfaced-tool`, `tool-available-but-not-used` |
| `integration-failure` | `integration-timeout`, `integration-briefing-validation`, `integration-read-only-violation`, `integration-max-iterations` |
| `response-quality` | `empty-response`, `merged-messages-dropped`, `stale-data-in-response` |
| `preference-pipeline` | `preference-stated-but-not-captured`, `schema-error-on-provider` |
| `provider-stability` | `provider-error-repeated` |
| `gate-policy` | `gate-confirm-on-reactive` |

Each child pattern carries: stable ID, parent_pattern_id, signal_type_keys
(set of `FrictionSignal.signal_type` values that map to this pattern),
description, lifecycle state, occurrence counter.

Parent patterns are themselves rows; their counters aggregate via SQL `SUM`
over children at query time (not stored). Avoids drift between parent
count and child counts.

**Aligns with architect's lean** (narrow + parent grouping).

### Decision 5 — Aggregation across instances

**Resolution:** per-instance counters; cross-instance aggregation is a query
that fans out across `data/<instance>/instance.db` files. No stored
cross-instance value.

This means cross-instance queries require multi-DB read access — not a
problem for the loop's eventual frequency-trigger logic since the trigger
fires within a single Kernos instance. Cross-instance is for operator
audit, not runtime loop closure.

**Aligns with architect's lean.**

### Decision 6 — Pattern lifecycle (active / resolved / reactivated)

**Resolution:** four states + clean transitions:

- `active` — pattern is being observed; counter accumulates.
- `resolved` — operator/loop marked it fixed. Counter no longer accumulates
  on auto-classify (matches surface a new `friction.pattern_recurrence` event
  with `resolved_pattern_id` instead of bumping the pattern's counter).
- `reactivated` — a `resolved` pattern hit a recurrence threshold; counter
  resumes accumulating. Surfaced as a friction signal of its own
  (`friction.pattern_reactivated`) for architect / loop attention.
- `archived` — pattern is no longer relevant; not auto-tagged; counter
  frozen. Opt-in operator-only state for cleanup.

**Reactivation discipline:** `resolved` → `reactivated` requires `N`
matching auto-classify hits within a configurable window. Defaults:
`N=3`, window `7 days`, both operator-tunable env vars
(`KERNOS_FRICTION_REACTIVATION_THRESHOLD=3`,
`KERNOS_FRICTION_REACTIVATION_WINDOW_DAYS=7`). The recurrence events
during the window count toward the reactivation threshold without
incrementing the pattern's main counter.

This is intentionally conservative — a single recurrence after a fix is
not enough signal to reactivate (might be a stale report from before the
fix landed; might be an edge case the fix didn't cover but isn't worth
re-opening). Three within a week is the cheapest threshold that
distinguishes "fix didn't take" from "fix mostly works."

**Aligns with architect's lean** (simple lifecycle); pins the reactivation
threshold explicitly rather than punting to ARTIFACT-LIFECYCLE-V1.

### Decision 7 — How specs reference patterns

**Resolution:** spec frontmatter is canonical; commit messages reference for
git-log discoverability. Loop closure verification reads from spec
frontmatter only.

Frontmatter format (YAML-style block at top of `specs/*.md` files):

```yaml
---
addresses_friction_patterns:
  - tool-request-for-surfaced-tool
  - tool-available-but-not-used
---
```

Optional field; specs without it parse fine. Parser:
`kernos/kernel/friction_patterns.py:parse_spec_pattern_refs(path: Path) -> list[str]`
— reads the frontmatter block, extracts the list, returns it. Loop closure
verification calls this at workflow-step time to know which patterns a
landed spec is supposed to have addressed.

Existing specs without frontmatter (including this one) get the field
added retroactively as patterns get cataloged.

**Aligns with architect's lean.**

## Open architectural questions (per architect's directive)

### Pattern grain trade-off — picked narrow with parent grouping

Decision 4 above. Trade-off rationale: the loop's measurement question is
"did this fix reduce friction?" — that question only resolves at the
narrow-pattern level (broad-pattern counts shift for many reasons; you
can't attribute a count change to a specific fix). Parent grouping
preserves the "show me the high-level picture" use case without losing
attribution.

The proliferation risk is real: if patterns are too narrow, every minor
edge case becomes its own pattern and the catalog bloats. Mitigation: the
classifier's threshold gate naturally consolidates near-misses into
existing patterns rather than creating new ones; new pattern creation
is an explicit operator/IA step, not auto-driven. Soak will reveal
whether the threshold + manual-creation pace produces a manageable
catalog or noise.

### Auto-classify confidence threshold — picked 0.6 with env-var tunability

Decision 3 above. Rationale: the algorithmic signal we use
(token-overlap-with-phrase-bonus, ranked tuple sort) ships in
PAGE-SEARCH-TOKEN-OVERLAP-V1 and is well-understood. 0.6 is a starting
guess — probably high enough that incidental token overlap doesn't
trigger false-positive auto-tags (signals like "INTEGRATION_TIMEOUT"
have very distinctive token surfaces) but low enough that paraphrased
descriptions still match. Env-var-tunable so operator can dial up or
down based on observed false-positive / false-negative rates during
soak.

### Pattern reactivation discipline — picked 3-occurrences-in-7-days

Decision 6 above. Resolution explicit + tunable. The architect's
question was "automatic reactivation OR explicit human/IA confirmation?"
— we land in between: automatic on threshold, but the threshold is
conservative enough that single false-positive recurrences don't
trigger it.

Stricter alternative (operator-confirm-only): every recurrence emits
the event; nothing happens automatically; operator must call a tool
to reactivate. Cheaper to implement (no threshold logic) but requires
operator vigilance.

Looser alternative (instant-reactivate): one recurrence after `resolved`
flips state. Catches "the fix didn't take" fast but risks reactivating
on stale reports that arrived from before the fix landed.

Threshold approach is the middle path; if soak shows the threshold
tuning matters, env vars let operator adjust without a code revert.

### Composition with REFERENCE-PRIMITIVE catalog — separate, but the
shape mirrors

Architect's question framed correctly: friction patterns are NOT
reference-eligible (different concern domain — references are
canonical documentation; patterns are runtime observed behavior). They
do not share a table.

The architectural shape mirrors closely (sqlite-backed catalog over
`instance.db`, dataclass + DDL + indexes, lifecycle states, separate
module with its own connection). That shape is *the* substrate
catalog convention now. Future "X-as-catalog" specs (workflow
definitions, agent personality variants, whatever) should adopt the
same shape unless they earn the deviation.

## Code-level shape

### New table

```sql
CREATE TABLE IF NOT EXISTS friction_pattern (
    pattern_id          TEXT PRIMARY KEY,
    instance_id         TEXT NOT NULL,
    parent_pattern_id   TEXT NOT NULL DEFAULT '',
    description         TEXT NOT NULL,
    signal_type_keys    TEXT NOT NULL DEFAULT '[]',  -- JSON array
    aliases             TEXT NOT NULL DEFAULT '[]',  -- JSON array
    lifecycle_state     TEXT NOT NULL DEFAULT 'active',
    occurrence_count    INTEGER NOT NULL DEFAULT 0,
    first_observed_at   TEXT NOT NULL DEFAULT '',
    last_observed_at    TEXT NOT NULL DEFAULT '',
    resolved_at         TEXT NOT NULL DEFAULT '',
    resolved_by_spec    TEXT NOT NULL DEFAULT '',
    reactivated_at      TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    CHECK (lifecycle_state IN ('active', 'resolved', 'reactivated', 'archived'))
);

CREATE INDEX IF NOT EXISTS idx_friction_pattern_instance_state
    ON friction_pattern (instance_id, lifecycle_state);
CREATE INDEX IF NOT EXISTS idx_friction_pattern_parent
    ON friction_pattern (instance_id, parent_pattern_id);

CREATE TABLE IF NOT EXISTS friction_pattern_occurrence (
    occurrence_id       TEXT PRIMARY KEY,
    instance_id         TEXT NOT NULL,
    pattern_id          TEXT NOT NULL,
    observed_at         TEXT NOT NULL,
    report_path         TEXT NOT NULL DEFAULT '',  -- friction report markdown path
    classifier_score    REAL NOT NULL DEFAULT 0.0,
    classified_by       TEXT NOT NULL DEFAULT 'auto',  -- 'auto' | 'manual' | 'recurrence'
    space_id            TEXT NOT NULL DEFAULT '',
    member_id           TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_friction_pattern_occurrence_pattern
    ON friction_pattern_occurrence (instance_id, pattern_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_friction_pattern_occurrence_window
    ON friction_pattern_occurrence (instance_id, observed_at);
```

Two-table design is deliberate: pattern row is the catalog entry (mostly
read-mostly); occurrence row is the append-only event log that drives
counters and time-window queries. Both tables together give frequency
analysis without scanning friction report files at query time.

### `FrictionPattern` dataclass + `FrictionPatternStore`

```python
# kernos/kernel/friction_patterns.py

@dataclass(frozen=True)
class FrictionPattern:
    pattern_id: str
    instance_id: str
    description: str
    signal_type_keys: tuple[str, ...]      # immutable; list of FrictionSignal.signal_type values
    aliases: tuple[str, ...] = ()
    parent_pattern_id: str = ""
    lifecycle_state: str = "active"        # 'active' | 'resolved' | 'reactivated' | 'archived'
    occurrence_count: int = 0
    first_observed_at: str = ""
    last_observed_at: str = ""
    resolved_at: str = ""
    resolved_by_spec: str = ""
    reactivated_at: str = ""
    created_at: str = ""


class FrictionPatternStore:
    """Sqlite-backed catalog over instance.db. Owns its own aiosqlite connection."""

    async def ensure_schema(self) -> None: ...
    async def create_pattern(
        self, *, instance_id: str, description: str,
        signal_type_keys: list[str], parent_pattern_id: str = "",
        seed_slug: str = "",  # if empty, derived from description
    ) -> FrictionPattern: ...
    async def get_pattern(
        self, instance_id: str, pattern_id_or_alias: str,
    ) -> FrictionPattern | None: ...
    async def list_patterns(
        self, instance_id: str, *, lifecycle_state: str | None = None,
        parent_pattern_id: str | None = None,
    ) -> list[FrictionPattern]: ...
    async def transition_lifecycle(
        self, instance_id: str, pattern_id: str, new_state: str,
        *, resolved_by_spec: str = "",
    ) -> FrictionPattern: ...
    async def rename_pattern(
        self, instance_id: str, current_id: str, new_id: str,
    ) -> FrictionPattern: ...
    async def record_occurrence(
        self, *, instance_id: str, pattern_id: str, observed_at: str,
        report_path: str = "", classifier_score: float = 0.0,
        classified_by: str = "auto", space_id: str = "", member_id: str = "",
    ) -> None: ...
    async def query_frequency(
        self, instance_id: str, pattern_id: str,
        *, window_start: str, window_end: str,
    ) -> int: ...
    async def query_top_patterns(
        self, instance_id: str, *, window_start: str, window_end: str,
        limit: int = 10,
    ) -> list[tuple[FrictionPattern, int]]: ...
    async def check_reactivation(
        self, instance_id: str, pattern_id: str,
    ) -> bool: ...   # returns True after transitioning state if threshold hit
```

Public API mirrors `kernos/kernel/reference/catalog.py:CatalogStore` —
`ensure_schema`, `create_*` / `get_*` / `list_*` / `transition_*`, all
with explicit `instance_id` argument for per-instance scoping.

### Classifier hook in `FrictionObserver`

Single seam: `FrictionObserver._write_report` gains an injected
`pattern_store: FrictionPatternStore | None` (default None preserves
backward-compat). When present:

```python
async def _write_report(self, signal: FrictionSignal, instance_id: str) -> None:
    # ... existing report-writing path unchanged ...

    if self._pattern_store is not None:
        try:
            classified = await self._classify_signal(signal, instance_id)
            if classified:
                pattern, score, observed_at = classified
                await self._pattern_store.record_occurrence(
                    instance_id=instance_id,
                    pattern_id=pattern.pattern_id,
                    observed_at=observed_at,
                    report_path=filepath,
                    classifier_score=score,
                    classified_by="auto",
                )
                # Reactivation check happens inside record_occurrence's
                # follow-on; keeps the happy path single-call.
            else:
                await self._emit_unclassified_event(signal, filepath, instance_id)
        except Exception as exc:
            logger.warning("FRICTION_CLASSIFY: failed: %s", exc)
```

Fail-open per per-space-fail-open-discipline ROUTER-EVIDENCE-V1 v2 risk B
established: classifier failure logs and continues; never blocks the
report write.

### Spec frontmatter parser

```python
def parse_spec_pattern_refs(spec_path: Path) -> list[str]:
    """Read addresses_friction_patterns from a spec's YAML frontmatter.

    Returns [] if the spec has no frontmatter or no field. Tolerant of
    formatting (YAML errors return [] rather than raise — frontmatter is
    optional metadata, not load-bearing).
    """
```

Implementation: read the first ~50 lines of the spec, look for `---\n`
fence, extract YAML between fences, parse, return list. Uses `yaml`
package (already in `pyproject.toml` for other uses; if not, falls
back to a manual line-parser since the format is narrow).

## Embedded live tests

Three test categories per architect directive plus member-isolation
probe per disclosure-boundary discipline.

### Catalog round-trip

`tests/test_friction_pattern_store.py::TestCatalogRoundtrip`

1. **`test_create_get_pattern`** — create pattern; query back by id;
   verify shape preserved (signal_type_keys round-trips, aliases empty,
   lifecycle defaults to active, occurrence_count starts at 0).
2. **`test_create_with_parent_grouping`** — create parent + child;
   list_patterns with parent_pattern_id filter returns child;
   list_patterns top-level returns both (no parent filter).
3. **`test_record_occurrence_increments_counter`** — record 3
   occurrences for a pattern; query_frequency over a window covering
   them returns 3; query_frequency over a window after them returns 0.
4. **`test_query_top_patterns`** — three patterns with 5/3/1
   occurrences in window; query_top_patterns returns them ordered
   correctly; respects limit.
5. **`test_lifecycle_transitions`** — active → resolved sets
   resolved_at; resolved → reactivated sets reactivated_at; archived
   stops accumulating; round-trip each.
6. **`test_rename_pattern_preserves_history`** — create pattern with
   3 occurrences; rename; old id appears in aliases; occurrences still
   queryable under new id; lookup by old id still returns the pattern
   (alias resolution).
7. **`test_action_state_record_per_op`** — RESPONSE-FIDELITY-V1
   discipline: each catalog mutation produces an ActionStateRecord
   (verified via fake action-record sink mirroring
   `tests/test_router_evidence.py`'s pattern).

### Auto-classify behavior

`tests/test_friction_pattern_store.py::TestAutoClassify`

1. **`test_signal_type_match_auto_tags`** — seed pattern with
   `signal_type_keys=["INTEGRATION_TIMEOUT"]`; feed FrictionSignal
   with that type; verify auto-tagged at high confidence.
2. **`test_token_overlap_above_threshold_auto_tags`** — pattern with
   description "tool request for already-surfaced tool"; signal with
   description matching by token overlap > 0.6; verify auto-tagged.
3. **`test_low_confidence_emits_unclassified_event`** — signal with
   no type match and low overlap; verify
   `friction.pattern_unclassified` event emitted via event_stream;
   verify pattern not silently auto-tagged.
4. **`test_threshold_env_var_tunable`** — set
   `KERNOS_FRICTION_CLASSIFIER_THRESHOLD=0.9`; signal that would have
   matched at 0.6; verify NOT auto-tagged at the higher threshold.
5. **`test_classifier_failure_does_not_block_report_write`** —
   inject store that raises on `record_occurrence`; verify friction
   report still written to disk; warning logged.

### Reactivation

`tests/test_friction_pattern_store.py::TestReactivation`

1. **`test_resolved_pattern_does_not_increment_counter`** — pattern
   in resolved state; record_occurrence emits recurrence event but
   pattern's occurrence_count unchanged.
2. **`test_threshold_recurrences_trigger_reactivation`** — pattern
   resolved; record 3 occurrences within 7 days; verify lifecycle
   transitions to reactivated; verify
   `friction.pattern_reactivated` event emitted.
3. **`test_below_threshold_recurrences_do_not_reactivate`** —
   pattern resolved; record 2 occurrences within 7 days (below
   default `KERNOS_FRICTION_REACTIVATION_THRESHOLD=3`); verify
   pattern stays resolved.
4. **`test_recurrences_outside_window_do_not_reactivate`** — record
   3 occurrences spread over 30 days (outside 7-day window); verify
   pattern stays resolved.
5. **`test_reactivated_pattern_resumes_counting`** — pattern
   reactivated; record_occurrence after reactivation; verify
   pattern's occurrence_count increments normally.

### Member-isolation probe

`tests/test_friction_pattern_store.py::TestMemberIsolation`

1. **`test_patterns_scoped_per_instance`** — create pattern P with
   instance_id A; pattern P with instance_id B (same slug, different
   instance); verify both exist independently; per-instance
   list_patterns returns only that instance's row.
2. **`test_occurrences_scoped_per_instance`** — record occurrences
   under instance A; query_frequency for instance B returns 0.

Patterns are per-instance, NOT per-member (same as REFERENCE-PRIMITIVE
catalog). Occurrences carry `member_id` for downstream provenance but
do not partition counts by member — frictions belong to the instance.

### Spec frontmatter parser

`tests/test_friction_pattern_store.py::TestFrontmatterParser`

1. **`test_parse_addresses_field`** — write a spec with the
   frontmatter block; parse; verify list returned.
2. **`test_parse_no_frontmatter_returns_empty`** — spec with no
   `---` fence; parser returns `[]` without raising.
3. **`test_parse_no_field_returns_empty`** — spec has frontmatter but
   no `addresses_friction_patterns` key; returns `[]`.
4. **`test_parse_malformed_yaml_returns_empty`** — frontmatter has
   YAML syntax error; parser returns `[]` with a logger.warning;
   does not raise.

## Composition notes

- **Existing FrictionObserver** (`kernos/kernel/friction.py`): unchanged
  except for the optional `pattern_store` injection. Existing signal
  detectors keep firing; `_write_report` writes the markdown report
  unchanged. Pattern catalog is additive.
- **RESPONSE-FIDELITY-V1**: `FrictionPatternStore.create_pattern`,
  `transition_lifecycle`, `rename_pattern`, `record_occurrence`,
  `manual classify` all produce ActionStateRecords through the per-turn
  collector. Reuses the `ctx.action_record_sink` shape established by
  ROUTER-EVIDENCE-V1's drain wiring (`kernos/messages/turn_runner_provider.py`).
- **Future workflow primitive**: consumes `query_top_patterns` and
  `query_frequency` to drive trigger conditions. Workflow spec defines
  trigger semantics; this spec just exposes the query API.
- **PAGE-SEARCH-TOKEN-OVERLAP-V1**: classifier reuses the
  phrase-bonus + token-overlap algorithm from
  `kernos/kernel/canvas.py:1495` for description matching.
- **Existing friction reports on disk** (10 files in
  `data/diagnostics/friction/`): one-shot backfill script populates the
  catalog from the historical reports. Scriptable; not a runtime concern.

## Risks and design constraints

| Risk | Mitigation |
|---|---|
| Pattern proliferation (every minor edge becomes a new pattern) | Threshold gate consolidates near-misses; new pattern creation is explicit operator/IA step, not auto-driven |
| Classifier false-positives (incidental token overlap) | Conservative default threshold (0.6); env-var tunable; `signal_type` exact-match takes precedence over token overlap |
| Classifier false-negatives (paraphrased descriptions miss) | Manual classification path always available; unclassified signals surface as their own friction event for human review |
| Reactivation noise (stale reports re-trigger) | 3-in-7-days threshold conservative enough that single stragglers don't reactivate |
| Cross-instance counter sync issues | Avoided entirely — per-instance counters; cross-instance is a query, not a stored value |
| Spec frontmatter format drift | Parser tolerant of malformed YAML (returns empty list rather than raising); commit-message ref still works as fallback |
| Catalog schema drift between `instance.db` consumers | Self-managed schema in `FrictionPatternStore.ensure_schema()` mirrors the per-module-isolation pattern from REFERENCE-PRIMITIVE-V1 |

## Open questions for Codex pre-spec review

1. **Slug generator collision strategy.** Numeric suffix (`-2`, `-3`) on
   collision is the simplest. Alternative: short hash suffix (`-a3f1`)
   keeps slugs stable when patterns are renamed (current numeric scheme
   could shuffle). Codex's call.

2. **Two-table vs one-table.** Patterns + occurrences in two tables is
   the cleanest for query patterns; could collapse to one table where
   each row is a pattern row with an embedded JSON array of recent
   occurrences. Two-table is simpler to query; one-table is simpler
   to schema-migrate. Two-table preferred but not load-bearing.

3. **Classifier threshold semantics.** Current spec uses a single
   threshold for both signal_type-match and token-overlap. Could
   bifurcate (signal_type match always tags; token-overlap has its
   own tunable threshold). Bifurcated is more honest semantically;
   single-threshold is one knob.

4. **Backfill timing.** One-shot script vs lazy backfill on first
   query? One-shot is cleaner; lazy means catalog accumulates over
   time as reports are queried. v1 instances have ~10 reports — one-shot
   is trivially small. Recommend one-shot.

5. **Who emits the `friction.pattern_recurrence` event for resolved
   patterns?** `record_occurrence` itself, or a separate
   `record_recurrence` method? The latter is cleaner if recurrence
   semantics drift from regular occurrence; the former is fewer
   surfaces. Lean: same method, internal switch on lifecycle_state.

## Sequence (per architect directive)

1. ✅ CC drafts spec at `specs/FRICTION-PATTERN-STABLE-IDS-V1.md` on branch
   `friction-pattern-stable-ids-v1` (this commit).
2. 🟡 **Codex pre-spec review** — pasteable blip via founder-relay; Codex
   reviews from repo state on this branch.
3. CC folds Codex review into spec.
4. **Architect ratification** of spec body.
5. CC implements per spec on the same branch.
6. **Codex post-implementation review** per established pattern.
7. CC any final changes.
8. Architect ratifies on close; merge to main.

No IA pre-spec review per architect directive — substrate concerns are
bounded enough that architect framing + Codex review covers it. IA
reviews CC's eventual workflow-primitive integration spec when that
spec gets drafted (separate spec, separate review).

## Linked artifacts

- Architect spec build directive: Notion `35cffafef4db81d0ad9ef705c802d313`
- PHASE-3-AUTONOMY-LOOP design consideration: Notion
  `35cffafef4db81da8107e562307bc738`
- Friction Observer V1 (composes-with): `kernos/kernel/friction.py`,
  `specs/completed/DESIGN-FRICTION-OBSERVER-V1.md`
- REFERENCE-PRIMITIVE-V1 (architectural template):
  `kernos/kernel/reference/catalog.py`,
  `docs/architecture/reference-primitive.md`
- RESPONSE-FIDELITY-V1 (every catalog op produces ActionStateRecord):
  Notion `35affafef4db8147a79adae3892df3e9`
- ROUTER-EVIDENCE-V1 (sister batch — schema-in-store + fail-open
  conventions): `specs/ROUTER-EVIDENCE-V1.md`
- PAGE-SEARCH-TOKEN-OVERLAP-V1 (classifier algorithmic primitive):
  `kernos/kernel/canvas.py:1495`
- Architect calls on parked items (this spec was pinned as IA's
  third architect task): Notion `359ffafef4db81d19ae5dd9dda4b3e8b`
