# FRICTION-PATTERN-STABLE-IDS-V1 — Implementation Spec

**Status:** DRAFT v3 — pre-implementation, TWO Codex pre-spec review
rounds folded 2026-05-10.

- **Round 1 (8 findings)** addressed per architect's calls at Notion
  `35cffafef4db8192a2efc3a0e3c23288`: composite per-instance PK,
  immutable `pattern_id`, storage path text rewrite, deferral of manual
  classification tool out of v1, CHECK/FK/UNIQUE constraints, bifurcated
  classifier threshold, explicit `record_recurrence` method with backfill
  excluded from reactivation, collision-resistant friction report
  filenames.
- **Round 2 (9 findings)** addressed direct from Codex's second review:
  stale classifier snippet (now dispatches by lifecycle + uses
  `auto-signal-type` / `auto-token-overlap` values), `PRAGMA
  foreign_keys=ON` mandatory in `ensure_schema` (SQLite default-off
  FK enforcement caught), Path B normalized scorer specified explicitly
  (Jaccard + phrase bonus on stopword-filtered tokens; not the raw
  canvas primitive output), single-label report uniqueness shifted to
  `(instance_id, report_path)`, `signal_type_keys` collision re-checked
  on lifecycle transitions into active/reactivated, `AliasCollision`
  error for alias conflicts across all pattern IDs + aliases, backfill
  rows counted in normal frequency queries (excluded ONLY from
  reactivation threshold), friction report path text consistency fix.

Awaiting architect ratification of revised spec body before CC
implementation begins.

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

Per the architect's explicit framing + Codex review fold:

- **Workflow primitive integration.** Triggers like "when pattern X frequency
  > threshold, fire workflow Y" are NOT here. The workflow spec consumes the
  catalog's frequency-query API; defining workflow trigger semantics is
  out of scope.
- **Restart-apply / completion-receipt protocol.** Separate spec.
- **Auto-pattern-creation from unclassified frictions at scale.** Manual
  creation by IA / operator suffices for v1's volume (~10 friction reports
  in the current data dir; cataloging by hand once is fine).
- **Cross-instance pattern aggregation as stored value.** Per-instance
  counter + cross-instance query via SQL over `instance_id`-tagged rows
  in the shared `data/instance.db`. Stored cross-instance counters
  introduce sync issues that v1 doesn't earn.
- **Workshop-V1 / autonomous spec drafting.** This spec is built by CC
  through the standard architect-framed handoff; it is NOT itself produced
  by the loop the catalog enables.
- **Manual classification tool surface.** Codex review Finding 4 deferred
  this out of v1. The Python `FrictionPatternStore` API supports
  `classified_by="manual"` for direct programmatic use (IA / operator
  scripts); a formal `classify_friction_report` tool surface waits for a
  follow-up spec when workflow primitive integration creates clearer
  need. Auto-classifier (Path A + Path B) ships in v1; manual override
  via Python API ships in v1; formal tool surface does not.
- **`rename_pattern` operation.** Codex review Blocker 2 established
  that rename contradicts the stable-ID promise. `update_description`,
  `set_display_name`, and `add_alias` cover the use cases without
  breaking immutability.

## Adjacent work in `kernos/kernel/friction.py` (Codex Finding 8)

The catalog's `friction_pattern_occurrence.report_path` becomes the
evidence key linking catalog rows back to the markdown report files in
`data/diagnostics/friction/`. Today's filename pattern in
`FrictionObserver._write_report` (`friction.py:417`) is:

```python
filename = f"FRICTION_{ts}_{safe_type}.md"
```

where `ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")`.
**Second-granularity timestamps are not collision-resistant** — two
friction signals fired in the same second (rare but observable in
integration-timeout cascades; see existing 2026-05-07 reports) produce
identical filenames, which would corrupt the catalog's evidence-key
discipline once `report_path` becomes a UNIQUE-indexed column.

**Required `friction.py` change** (small, ships alongside this spec's
implementation):

```python
import uuid
filename = f"FRICTION_{ts}_{safe_type}_{uuid.uuid4().hex[:8]}.md"
```

The 8-hex-char UUID slice (32 bits of entropy) is sufficient for
collision-avoidance at observed friction-emission rates without making
filenames unwieldy. Existing report files in `data/<instance>/
diagnostics/friction/` keep their current filenames; backfill picks
them up with the existing names (no rename needed).

This is a `friction.py` change, not a catalog change, but the catalog's
UNIQUE-index constraint on `report_path` requires it land before the
catalog goes live. Spec body: implementer ships both in the same
batch. (Folded from Codex review Finding 8.)

## Architectural decisions (resolving the architect's leans)

The architect's spec build directive listed seven decisions with leans.
Each is resolved below; deviations from the lean are flagged.

### Decision 1 — Pattern catalog structure (storage + ownership)

**Resolution:** sqlite-backed catalog rows in the **shared `data/instance.db`,
scoped by `instance_id` columns** — mirroring
`kernos/kernel/reference/catalog.py:CatalogStore` exactly. New tables
`friction_pattern` and `friction_pattern_occurrence`. New module
`kernos/kernel/friction_patterns.py` exposing `FrictionPatternStore` with
its own `aiosqlite` connection (per-module-isolation pattern;
`KERNEL-TOOL-REGISTRY-V1` sister convention).

**Cross-instance aggregation is a SQL query over `instance_id`-tagged rows**
in the same DB, not a fanout across per-instance DB files. Pattern IDs are
**per-instance namespaces** — the same slug can exist independently across
instances; cross-instance same-slug patterns are independent records with
no shared lifecycle.

The existing `data/diagnostics/friction/*.md` reports remain
the durable human-readable evidence; the catalog stores the structured
layer (pattern ID, occurrence counts, lifecycle) that the markdown
reports lack. There is **no** `data/<instance>/friction_patterns/`
filesystem directory.

**Aligns with REFERENCE-PRIMITIVE-V1.CatalogStore exactly.** Codex pre-spec
review (Finding 3, HIGH) caught that an earlier draft's text said the
wrong thing in places (suggesting per-instance DB files); this revision's
storage section now matches the implementation intent. The architect's
original lean ("probably under `data/<instance>/friction_patterns/`") is
explicitly superseded by this resolution.

### Decision 2 — Pattern ID format (immutable, slug + aliases)

**Resolution:** human-readable slug, kebab-case, lowercase, ASCII-only.
**`pattern_id` is IMMUTABLE — generated once at creation, never modified.**
Codex pre-spec review (Blocker 2) named the deeper architectural insight:
stable IDs that change aren't stable; aliases preserve human-readable
continuity without breaking the underlying stability. This spec adopts
that as a core invariant.

Example IDs: `compaction-fails-when-canvas-empty`, `tool-request-for-surfaced-tool`,
`integration-timeout-on-large-payload`.

Generator: `slugify(seed: str) -> str` — strips non-alphanumeric, lowercases,
collapses runs of non-alphanumeric to single hyphen, trims leading/trailing
hyphens. **Collision handling lives inside the write transaction** in
`create_pattern`: query for existing rows scoped by `instance_id`; on
collision, append a numeric disambiguator (`-2`, `-3`, ...) and retry
within the same transaction. Numeric suffix is sufficient because IDs are
immutable — there is no rename path that could reshuffle them; the suffix
is permanent for the lifetime of the row.

**Identity is moved into separate fields that CAN change:**

- `display_name: str` — human-readable label (free-form text, not slug-shaped).
  Mutable via `set_display_name(pattern_id, name)`.
- `description: str` — full prose description.
  Mutable via `update_description(pattern_id, new_desc)`.
- `aliases: tuple[str, ...]` — additional lookup keys for the same pattern.
  Append-only via `add_alias(pattern_id, alias)`. Aliases are never removed
  (lookup compatibility); they persist alongside the immutable `pattern_id`.

Lookups (`get_pattern(instance_id, pattern_id_or_alias)`) check `pattern_id`
first, then `aliases`, **always scoped by `instance_id`**.

**There is no `rename_pattern` method.** Codex pre-spec review (Blocker 2)
established that a rename operation directly contradicts the stable-ID
promise. The replacement methods (`update_description`, `set_display_name`,
`add_alias`) cover every continuity-of-meaning use case a rename would
have served, without breaking the stability invariant.

**Aligns with architect's lean** on slug + aliases; tightens lean on rename
per Codex review (Blocker 2).

### Decision 3 — Friction-report → pattern tagging (hybrid classifier, bifurcated threshold)

**Resolution:** hybrid auto+manual per architect lean. **Auto-classifier
uses TWO independent matching paths with a bifurcated threshold**
(Codex pre-spec review Finding 6):

- **Path A — `signal_type_keys` exact-match:** if the friction signal's
  `signal_type` is a member of any pattern's `signal_type_keys` set, the
  match scores **1.0 deterministically** and is **NOT subject to any
  threshold**. This is the canonical fast path for the existing
  `FrictionSignal.signal_type` vocabulary (the 10 types `FrictionObserver`
  emits today).

- **Path B — token-overlap against `description`:** when no `signal_type`
  match exists, score the new signal's description against each pattern's
  `description` using a **normalized scorer** specified below. The
  PAGE-SEARCH-TOKEN-OVERLAP-V1 primitive at `kernos/kernel/canvas.py:1495`
  returns `phrase_count * _PHRASE_BONUS + token_sum` — raw counts, NOT a
  0-1 normalized score. A 0.6 threshold against that raw value would be
  meaningless. Codex review round 2 Blocker 4 caught this: classifier
  must define an explicit normalized scorer rather than reusing the
  canvas primitive's raw output directly.

  **Normalized scorer specification (Path B):**

  ```
  signal_tokens = tokenize(signal.description)
  pattern_tokens = tokenize(pattern.description)
  if not signal_tokens or not pattern_tokens:
      return 0.0  # short-description guard

  # Stopwords and min token length: drop tokens len<3 OR in _STOPWORDS;
  # _STOPWORDS = {"the", "and", "for", "with", "that", "this", "from", "into", "are", "was"}
  signal_clean = {t for t in signal_tokens if len(t) >= 3 and t not in _STOPWORDS}
  pattern_clean = {t for t in pattern_tokens if len(t) >= 3 and t not in _STOPWORDS}
  if not signal_clean or not pattern_clean:
      return 0.0

  # Jaccard over cleaned tokens for the base score
  overlap = len(signal_clean & pattern_clean)
  union = len(signal_clean | pattern_clean)
  jaccard = overlap / union if union else 0.0

  # Phrase bonus: if pattern.description appears as a substring (lowercased,
  # trimmed) inside signal.description, boost by +0.3 (capped at 1.0)
  phrase_bonus = 0.3 if pattern.description.lower().strip() in signal.description.lower() else 0.0

  return min(1.0, jaccard + phrase_bonus)
  ```

  Tokenization mirrors `canvas.py:1526`: `re.split(r"\W+", text)` then
  lowercase. The canvas primitive is reused at the algorithmic level
  (regex-based tokenization + phrase-bonus concept) but the friction
  classifier defines its own normalized scoring on top so the 0.6
  threshold has well-defined semantics.

  Auto-tag if normalized score exceeds
  `KERNOS_FRICTION_TOKEN_OVERLAP_THRESHOLD` (default 0.6,
  operator-tunable). This threshold applies ONLY to Path B; it is
  semantically distinct from any signal-type-related knob.

  **Short-description behavior:** when either description has fewer
  than 3 cleaned tokens, score is 0.0 — the classifier refuses to
  match short descriptions to avoid false positives on
  low-information surfaces.

- **Confidence ranking when multiple matches:** Path A always wins over
  Path B (deterministic > algorithmic). Among Path A candidates,
  `signal_type_keys` uniqueness (below) guarantees there is exactly one.
  Among Path B candidates, highest score wins; ties broken by
  `created_at` (older pattern wins; preserves stable identity).

**`signal_type_keys` uniqueness invariant:** any given `signal_type` value
maps to **at most one active-or-reactivated pattern per instance**. This
is enforced at TWO write paths (Codex review round 2 Finding 6):

1. **`create_pattern`:** if the candidate `signal_type_keys` set
   intersects any existing active/reactivated pattern's keys for the same
   instance, the create raises `SignalTypeKeyCollision`.
2. **`transition_lifecycle` into `active` or `reactivated`:** when an
   archived or resolved pattern transitions back into active or
   reactivated state, the store re-checks its `signal_type_keys` against
   all other active/reactivated patterns for the same instance; if
   collision, the transition raises `SignalTypeKeyCollision`. Operator
   must `update_description` + change the keys first, OR archive the
   colliding pattern, before the transition can proceed.

Archived and resolved patterns are excluded from the uniqueness check
themselves (they aren't auto-classify targets), but lifecycle
transitions that put them BACK into an auto-classify-eligible state
must re-validate. Earlier draft only enforced at create-time, which
left a hole: a pattern could be created, archived, then have a
colliding sibling created, then transitioned back to active — at which
point Path A would have two canonical targets.

- **Below either threshold:** leave `pattern_id` unset on the report;
  emit a `friction.pattern_unclassified` event_stream event so a
  higher-tier observer (or operator) can hand-classify later.

- **Manual classification: deferred out of v1 tool surface** (Codex
  review Finding 4). The Python `FrictionPatternStore.record_occurrence`
  API supports `classified_by="manual"` for direct programmatic use
  (IA / operator scripts); a formal `classify_friction_report` tool
  surface is deferred to a follow-up spec when workflow primitive
  integration creates clearer need. See "What this spec does NOT ship"
  below.

- **No LLM call on the classifier hot path.** Tightens the architect's
  framing of "auto-classify with manual confirmation for new patterns."
  Both Path A and Path B are deterministic/algorithmic — no per-report
  cohort call.

**Tightens the architect's lean** by pinning the auto-classifier to
deterministic + algorithmic signals; bifurcates threshold per Codex review.

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

**Resolution:** per-instance counters in the shared `data/instance.db`;
cross-instance aggregation is a SQL query over `instance_id`-tagged
rows in the same DB. No stored cross-instance value; no fanout across
per-instance DB files (per Decision 1's storage shape, which Codex
review Finding 3 corrected).

The runtime loop's frequency trigger fires within a single Kernos
instance, so cross-instance aggregation is for operator audit, not
runtime loop closure. A simple SQL query (`SELECT instance_id,
SUM(...) FROM friction_pattern_occurrence WHERE ... GROUP BY
instance_id`) suffices; no application-level fanout needed.

**Aligns with architect's lean** (per-instance counters; cross-instance
as query). Storage shape clarified per Codex review.

### Decision 6 — Pattern lifecycle (active / resolved / reactivated / archived)

**Resolution:** four states with explicit method-to-state mapping.
Codex pre-spec review (Finding 7) established that recurrence and
regular occurrence have different lifecycle effects and should be
separate methods:

- `active` — pattern is being observed. `record_occurrence` increments
  `occurrence_count` and updates `last_observed_at`.
- `resolved` — operator/loop marked it fixed via `transition_lifecycle(
  pattern_id, new_state="resolved", resolved_by_spec=...)`. Subsequent
  matches go through **`record_recurrence`** (not `record_occurrence`)
  which: emits `friction.pattern_recurrence` event with
  `resolved_pattern_id`; does NOT increment the pattern's main
  `occurrence_count`; checks the reactivation threshold and may
  transition state.
- `reactivated` — a `resolved` pattern hit the recurrence threshold;
  `record_recurrence` flipped state and emitted
  `friction.pattern_reactivated`. Counter resumes accumulating on
  subsequent `record_occurrence` calls (not `record_recurrence`;
  reactivated patterns receive new occurrences as active patterns do).
- `archived` — pattern is no longer relevant; not auto-tagged;
  `record_occurrence` and `record_recurrence` both reject with
  `PatternArchived` error. Opt-in operator-only state for cleanup.

**Method-to-lifecycle mapping** (folded from Codex Finding 7):

| Method | Active | Resolved | Reactivated | Archived |
|---|---|---|---|---|
| `record_occurrence` | ✅ increment counter | ❌ rejects (use `record_recurrence`) | ✅ increment counter | ❌ rejects |
| `record_recurrence` | ❌ rejects (use `record_occurrence`) | ✅ recurrence event + threshold check | ❌ rejects (already reactivated) | ❌ rejects |
| `transition_lifecycle` | active→resolved/archived | resolved→active/archived | reactivated→resolved/archived | archived→active (operator override) |

The classifier hook in `FrictionObserver._write_report` selects the
right method based on the matched pattern's current `lifecycle_state`:
active/reactivated → `record_occurrence`; resolved → `record_recurrence`;
archived → no-op (and emit `friction.pattern_unclassified` so the
report doesn't lose its pattern association entirely).

**Reactivation discipline:** `resolved` → `reactivated` requires `N`
matching `record_recurrence` calls within a configurable window where
each call's `observed_at >= resolved_at`. Defaults: `N=3`, window
`7 days`, both operator-tunable env vars
(`KERNOS_FRICTION_REACTIVATION_THRESHOLD=3`,
`KERNOS_FRICTION_REACTIVATION_WINDOW_DAYS=7`).

**Backfill is excluded ONLY from the reactivation threshold check**
(Codex review round 1 Finding 7 + round 2 Finding 8): the one-shot
backfill script that populates the catalog from historical friction
reports MUST mark each occurrence with `classified_by="backfill"`.
The reactivation threshold check filters out `classified_by="backfill"`
rows so a fresh import of pre-resolution reports cannot re-trigger
reactivation.

**Backfill rows DO count in normal frequency queries** — `query_frequency`
and `query_top_patterns` include them by default. The whole point of
backfill is to provide the historical baseline that makes the
catalog's "before vs after a fix" measurement meaningful; unconditionally
excluding backfill from frequency queries would defeat that purpose
(Codex round 2 Finding 8 caught this — earlier draft excluded backfill
from `query_frequency` unconditionally).

The exclusion is implemented inside the store's internal reactivation
helper, NOT as a default-true `exclude_backfill` flag on the public
query API. External callers see backfill rows as first-class history.

This is intentionally conservative — a single recurrence after a fix is
not enough signal to reactivate. Three within a week (excluding
backfill) is the cheapest threshold that distinguishes "fix didn't
take" from "fix mostly works."

**Aligns with architect's lean** (simple lifecycle); pins the reactivation
threshold + method-to-lifecycle mapping per Codex review.

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

### New tables

DDL revised per Codex review (Blocker 1 — composite per-instance PK;
Finding 5 — CHECK on `classified_by`, composite FK, UNIQUE on
`report_path`):

```sql
CREATE TABLE IF NOT EXISTS friction_pattern (
    instance_id         TEXT NOT NULL,
    pattern_id          TEXT NOT NULL,
    parent_pattern_id   TEXT NOT NULL DEFAULT '',
    display_name        TEXT NOT NULL DEFAULT '',
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
    PRIMARY KEY (instance_id, pattern_id),
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
    classified_by       TEXT NOT NULL DEFAULT 'auto-signal-type',
    space_id            TEXT NOT NULL DEFAULT '',
    member_id           TEXT NOT NULL DEFAULT '',
    is_recurrence       INTEGER NOT NULL DEFAULT 0,  -- 1 when recorded post-resolved_at
    FOREIGN KEY (instance_id, pattern_id)
        REFERENCES friction_pattern(instance_id, pattern_id),
    CHECK (classified_by IN (
        'auto-signal-type', 'auto-token-overlap', 'manual', 'backfill'
    ))
);

CREATE INDEX IF NOT EXISTS idx_friction_pattern_occurrence_pattern
    ON friction_pattern_occurrence (instance_id, pattern_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_friction_pattern_occurrence_window
    ON friction_pattern_occurrence (instance_id, observed_at);

-- Codex round 1 Finding 5 + round 2 Finding 5: report-level
-- uniqueness scoped to (instance_id, report_path), NOT
-- (instance_id, pattern_id, report_path). A friction report
-- represents ONE friction event; it belongs to at most one pattern.
-- The earlier per-(pattern, report) index would have allowed the
-- same report to land under multiple patterns, breaking the
-- single-label invariant the catalog promises. Partial index
-- because empty report_path is legitimate (synthetic test rows;
-- direct API calls without a backing markdown file).
CREATE UNIQUE INDEX IF NOT EXISTS uniq_friction_pattern_occurrence_report
    ON friction_pattern_occurrence (instance_id, report_path)
    WHERE report_path != '';
```

**Single-label scope decision** (Codex round 2 Finding 5): a friction
report file is associated with **at most one pattern**. If the
classifier was wrong and the report was tagged to pattern A but should
have been pattern B, the operator path is:

1. `record_occurrence` rejects the second insert (UNIQUE constraint trips).
2. Operator deletes the original row via direct SQL (the catalog does NOT
   expose a `delete_occurrence` API in v1 — receipts are append-only).
3. Operator re-inserts under the correct pattern_id with
   `classified_by="manual"`.

Multi-label reports (one event matching multiple patterns simultaneously)
are NOT in v1 scope. If soak surfaces this need, a follow-up spec can
relax the constraint to `(instance_id, pattern_id, report_path)` plus
an explicit "multi-label allowed" flag.

**Schema notes per Codex review:**

- **Composite PK `(instance_id, pattern_id)`** (Blocker 1): pattern_id
  is per-instance namespace, not global. Same slug can exist
  independently across instances; cross-instance same-slug rows are
  independent records.
- **Composite FK on `friction_pattern_occurrence`** (Blocker 1 +
  Finding 5): occurrence rows reference both `instance_id` and
  `pattern_id` columns of the parent pattern. Prevents orphaned
  occurrences from cross-instance pattern deletion.
- **`PRAGMA foreign_keys=ON` is mandatory** (Codex review round 2
  Blocker 3). SQLite does NOT enforce declared FKs by default; the
  pragma must be set on every connection the store uses.
  `FrictionPatternStore.ensure_schema()` runs `PRAGMA foreign_keys=ON`
  before creating the tables, and the store's connection-bring-up
  helper (called on every new connection) re-asserts the pragma since
  it does not persist across connections. Embedded live test
  `test_orphan_insert_rejected` (below) verifies enforcement by
  attempting an insert with an unknown `(instance_id, pattern_id)`
  and asserting the FK constraint trips.
- **`CHECK (classified_by IN ...)`** (Finding 5): vocabulary is closed
  set. The four values map to the bifurcated classifier paths:
  `auto-signal-type` (Path A exact match), `auto-token-overlap`
  (Path B fuzzy match), `manual` (operator/IA via Python API),
  `backfill` (one-shot historical import; excluded from reactivation
  logic per Decision 6).
- **`UNIQUE (instance_id, pattern_id, report_path)`** (Finding 5):
  partial index where `report_path != ''`. A given friction report file
  can only be associated with a given pattern once, even if reprocessed
  (classifier rerun, manual reclassification, backfill of a report that
  was later cataloged via runtime path).
- **`is_recurrence` flag**: marks rows recorded via `record_recurrence`
  (post-`resolved_at`). Reactivation threshold query filters on this
  alongside `classified_by != 'backfill'`.

**`display_name` column** added per Decision 2 (immutable `pattern_id`
implies identity is split across stable + mutable fields). Free-form
text label distinct from the slug-shaped `pattern_id`.

Two-table design is deliberate: pattern row is the catalog entry
(read-mostly); occurrence row is the append-only event log that drives
counters and time-window queries. Both tables together give frequency
analysis without scanning friction report files at query time.

### `FrictionPattern` dataclass + `FrictionPatternStore`

```python
# kernos/kernel/friction_patterns.py

@dataclass(frozen=True)
class FrictionPattern:
    instance_id: str
    pattern_id: str                        # IMMUTABLE; never modified after creation
    description: str
    signal_type_keys: tuple[str, ...]      # FrictionSignal.signal_type values
    display_name: str = ""                 # mutable via set_display_name
    aliases: tuple[str, ...] = ()          # append-only via add_alias
    parent_pattern_id: str = ""
    lifecycle_state: str = "active"        # 'active' | 'resolved' | 'reactivated' | 'archived'
    occurrence_count: int = 0
    first_observed_at: str = ""
    last_observed_at: str = ""
    resolved_at: str = ""
    resolved_by_spec: str = ""
    reactivated_at: str = ""
    created_at: str = ""


class SignalTypeKeyCollision(ValueError):
    """Raised when create_pattern or transition_lifecycle into
    active/reactivated would result in signal_type_keys colliding with
    another active/reactivated pattern's keys for the same instance.
    Codex review round 1 Finding 6 + round 2 Finding 6: ties Path A
    exact-match to a single canonical target."""


class AliasCollision(ValueError):
    """Raised when add_alias would create an alias that collides with
    another pattern's pattern_id OR another pattern's existing alias
    in the same instance. Codex review round 2 Finding 7."""


class PatternArchived(RuntimeError):
    """Raised when record_occurrence / record_recurrence is called on
    an archived pattern. Operator must transition_lifecycle out of
    archived first if the pattern is still relevant."""


class FrictionPatternStore:
    """Sqlite-backed catalog over data/instance.db. Owns its own
    aiosqlite connection (per-module-isolation pattern; mirrors
    kernos/kernel/reference/catalog.py:CatalogStore)."""

    async def ensure_schema(self) -> None: ...

    # --- Creation (immutable pattern_id; collision-handled within transaction) ---
    async def create_pattern(
        self, *, instance_id: str, description: str,
        signal_type_keys: list[str], parent_pattern_id: str = "",
        display_name: str = "",
        seed_slug: str = "",  # if empty, derived from description
    ) -> FrictionPattern:
        """Create a new pattern. pattern_id is generated from seed_slug
        (or derived from description if seed_slug is empty) via the
        deterministic slugify helper, with numeric suffix retry on
        collision within the same write transaction.

        Raises SignalTypeKeyCollision if signal_type_keys intersects
        any existing active/reactivated pattern's keys for this instance
        (Codex Finding 6 — Path A uniqueness invariant)."""

    # --- Lookup (always instance-scoped) ---
    async def get_pattern(
        self, instance_id: str, pattern_id_or_alias: str,
    ) -> FrictionPattern | None:
        """Lookup by pattern_id, falling back to aliases match. Always
        scoped by instance_id (Blocker 1)."""

    async def list_patterns(
        self, instance_id: str, *, lifecycle_state: str | None = None,
        parent_pattern_id: str | None = None,
    ) -> list[FrictionPattern]: ...

    # --- Mutable identity fields (Decision 2: pattern_id stays immutable) ---
    async def update_description(
        self, instance_id: str, pattern_id: str, new_description: str,
    ) -> FrictionPattern: ...

    async def set_display_name(
        self, instance_id: str, pattern_id: str, name: str,
    ) -> FrictionPattern: ...

    async def add_alias(
        self, instance_id: str, pattern_id: str, alias: str,
    ) -> FrictionPattern:
        """Append-only. Aliases are never removed (lookup compatibility
        guarantee). Idempotent: adding an alias that already exists on
        THIS pattern is a no-op.

        Rejects with AliasCollision (Codex review round 2 Finding 7) if
        the alias (normalized: slugified, lowercased) collides with:
          - any other pattern's pattern_id in the same instance, OR
          - any other pattern's existing alias in the same instance.
        Operator must pick a different alias. Aliases are normalized
        via the same slugify helper as pattern_id generation so
        lookups can match either form consistently."""

    # --- Lifecycle transitions ---
    async def transition_lifecycle(
        self, instance_id: str, pattern_id: str, new_state: str,
        *, resolved_by_spec: str = "",
    ) -> FrictionPattern:
        """Move between {active, resolved, reactivated, archived}.
        Sets resolved_at / reactivated_at when transitioning into
        those states. Operator override is the only path out of
        archived."""

    # --- Occurrence recording (split per Codex Finding 7) ---
    async def record_occurrence(
        self, *, instance_id: str, pattern_id: str, observed_at: str,
        report_path: str = "", classifier_score: float = 0.0,
        classified_by: str = "auto-signal-type",
        space_id: str = "", member_id: str = "",
    ) -> None:
        """Record an occurrence on an active or reactivated pattern.
        Increments occurrence_count and updates last_observed_at.

        Rejects with ValueError if pattern lifecycle_state is 'resolved'
        (caller must use record_recurrence instead) or 'archived'
        (raises PatternArchived). Idempotent on (instance_id,
        pattern_id, report_path) via the partial UNIQUE index.

        classified_by must be one of: 'auto-signal-type',
        'auto-token-overlap', 'manual', 'backfill'."""

    async def record_recurrence(
        self, *, instance_id: str, pattern_id: str, observed_at: str,
        report_path: str = "", classifier_score: float = 0.0,
        classified_by: str = "auto-signal-type",
        space_id: str = "", member_id: str = "",
    ) -> bool:
        """Record a recurrence on a resolved pattern. Emits
        friction.pattern_recurrence event_stream event. Does NOT
        increment the pattern's occurrence_count.

        Returns True if the recurrence triggered reactivation (state
        flipped resolved -> reactivated), False otherwise. Reactivation
        threshold counts only post-resolved_at occurrences with
        classified_by != 'backfill' (Codex Finding 7).

        Rejects with ValueError if pattern lifecycle_state is
        'active' or 'reactivated' (caller must use record_occurrence)
        or 'archived' (raises PatternArchived)."""

    # --- Frequency queries ---
    async def query_frequency(
        self, instance_id: str, pattern_id: str,
        *, window_start: str, window_end: str,
        include_recurrences: bool = False,
        exclude_backfill: bool = False,
    ) -> int:
        """Count occurrences in [window_start, window_end).

        By default INCLUDES classified_by='backfill' rows (Codex review
        round 2 Finding 8: backfill exists to provide historical
        baseline; unconditionally excluding it from frequency queries
        loses that baseline). Pass exclude_backfill=True to drop
        backfill rows — used by the reactivation threshold check
        internally, NOT exposed to normal query callers.

        By default excludes is_recurrence=1 rows; pass
        include_recurrences=True to include them."""

    async def query_top_patterns(
        self, instance_id: str, *, window_start: str, window_end: str,
        limit: int = 10,
    ) -> list[tuple[FrictionPattern, int]]: ...
```

**`rename_pattern` removed** per Codex review Blocker 2: rename violates
the stable-ID promise. The replacement methods (`update_description`,
`set_display_name`, `add_alias`) cover every continuity-of-meaning use
case without breaking immutability.

**`classify_friction_report` (tool surface) removed** per Codex review
Finding 4: deferred out of v1. Manual classification is accessible via
direct `record_occurrence` / `record_recurrence` Python API calls with
`classified_by="manual"`. Formal tool-surface formalization waits for
clearer use case post-workflow-primitive integration.

Public API mirrors `kernos/kernel/reference/catalog.py:CatalogStore` —
`ensure_schema`, `create_*` / `get_*` / `list_*` / `transition_*`, all
with explicit `instance_id` argument for per-instance scoping.

### Classifier hook in `FrictionObserver`

Single seam: `FrictionObserver._write_report` gains an injected
`pattern_store: FrictionPatternStore | None` (default None preserves
backward-compat). The classifier returns the matched pattern, score,
and which path matched (Path A signal_type or Path B token-overlap)
so the hook can record the correct `classified_by` vocabulary value
AND dispatch to the right method based on the pattern's
`lifecycle_state` (Codex review round 2 Blocker — earlier snippet
called `record_occurrence` unconditionally with `classified_by="auto"`,
which violated both the CHECK vocabulary and the separate
`record_recurrence` design).

```python
async def _write_report(self, signal: FrictionSignal, instance_id: str) -> None:
    # ... existing report-writing path unchanged ...

    if self._pattern_store is not None:
        try:
            classified = await self._classify_signal(signal, instance_id)
            if classified is None:
                await self._emit_unclassified_event(signal, filepath, instance_id)
                return

            pattern, score, match_path = classified
            # match_path is 'signal-type' (Path A, score==1.0) or
            # 'token-overlap' (Path B, score from normalized scorer).
            classified_by = (
                "auto-signal-type" if match_path == "signal-type"
                else "auto-token-overlap"
            )

            # Dispatch by lifecycle: active/reactivated -> record_occurrence;
            # resolved -> record_recurrence; archived -> drop (emit
            # unclassified so the report still has an audit trail).
            if pattern.lifecycle_state in ("active", "reactivated"):
                await self._pattern_store.record_occurrence(
                    instance_id=instance_id,
                    pattern_id=pattern.pattern_id,
                    observed_at=utc_now(),
                    report_path=filepath,
                    classifier_score=score,
                    classified_by=classified_by,
                    space_id=signal.context.get("space", ""),
                    member_id=signal.context.get("member_id", ""),
                )
            elif pattern.lifecycle_state == "resolved":
                await self._pattern_store.record_recurrence(
                    instance_id=instance_id,
                    pattern_id=pattern.pattern_id,
                    observed_at=utc_now(),
                    report_path=filepath,
                    classifier_score=score,
                    classified_by=classified_by,
                    space_id=signal.context.get("space", ""),
                    member_id=signal.context.get("member_id", ""),
                )
            else:
                # archived
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
   display_name empty, lifecycle defaults to active, occurrence_count
   starts at 0).
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
6. **`test_pattern_id_immutable_with_alias_continuity`** — create
   pattern with 3 occurrences; verify there is no `rename_pattern`
   method on the store; call `add_alias(pattern_id, "old-name")`;
   verify old name resolves to the pattern via `get_pattern`;
   verify occurrences still queryable under the original immutable
   `pattern_id`. (Codex review Blocker 2 — stable IDs that change
   aren't stable.)
7. **`test_set_display_name_and_update_description`** — round-trip
   both mutator methods; verify pattern_id unchanged; verify
   display_name and description updated correctly.
8. **`test_action_state_record_per_op`** — RESPONSE-FIDELITY-V1
   discipline: each catalog mutation produces an ActionStateRecord
   (verified via fake action-record sink mirroring
   `tests/test_router_evidence.py`'s pattern).
9. **`test_create_pattern_signal_type_keys_uniqueness`** — create
   pattern A with `signal_type_keys=["INTEGRATION_TIMEOUT"]`;
   attempt to create pattern B with overlapping
   `signal_type_keys=["INTEGRATION_TIMEOUT", "OTHER"]`; verify
   raises `SignalTypeKeyCollision`. Archive pattern A; verify B can
   now be created (archived patterns excluded from uniqueness check).
   (Codex review Finding 6.)
10. **`test_slug_collision_appends_numeric_suffix`** — create pattern
    deriving slug `compaction-fails`; create second pattern with same
    seed; verify second gets `compaction-fails-2`; both pattern_ids
    immutable thereafter.
11. **`test_orphan_insert_rejected_via_fk`** — Codex round 2 Blocker 3.
    Insert into `friction_pattern_occurrence` with an unknown
    `(instance_id, pattern_id)` pair; verify FK constraint raises.
    Verifies `PRAGMA foreign_keys=ON` is actually enabled by
    `ensure_schema()` and on every new connection. Test is the
    canary that catches "FK declared but enforcement off" regressions.
12. **`test_single_label_report_uniqueness`** — Codex round 2 Finding 5.
    Pattern A and pattern B both exist; record an occurrence with
    `report_path="reports/x.md"` under pattern A; attempt to record
    an occurrence with the same `report_path` under pattern B;
    verify the second insert raises (UNIQUE constraint on
    `(instance_id, report_path)`). A friction report belongs to at
    most one pattern.
13. **`test_signal_type_keys_collision_on_transition_to_active`** —
    Codex round 2 Finding 6. Create pattern A with
    `signal_type_keys=["INTEGRATION_TIMEOUT"]`; archive A; create
    pattern B with same `signal_type_keys` (succeeds because A is
    archived); attempt to transition A back to active; verify
    raises `SignalTypeKeyCollision`. Operator must archive B or
    rewrite A's keys before A can return.
14. **`test_alias_collision_against_existing_pattern_id`** — Codex
    round 2 Finding 7. Pattern A has `pattern_id="foo-bar"`;
    pattern B has `pattern_id="baz-qux"`; attempt
    `add_alias(B, "foo-bar")`; verify raises `AliasCollision`.
15. **`test_alias_collision_against_existing_alias`** — pattern A
    has alias `"old-name"`; attempt `add_alias(B, "old-name")` on
    pattern B; verify raises `AliasCollision`.
16. **`test_alias_normalized_via_slugify`** — `add_alias(A, "Foo Bar!")`
    stores `"foo-bar"`; subsequent `add_alias(B, "foo-bar")` collides
    (Codex Finding 7 mandates normalization).

### Auto-classify behavior

`tests/test_friction_pattern_store.py::TestAutoClassify`

1. **`test_signal_type_path_a_match_scores_one_zero`** — seed pattern
   with `signal_type_keys=["INTEGRATION_TIMEOUT"]`; feed
   FrictionSignal with that type; verify auto-tagged with
   `classifier_score == 1.0` and `classified_by == 'auto-signal-type'`.
   Path A is deterministic; threshold does not apply.
2. **`test_token_overlap_path_b_uses_own_threshold`** — pattern with
   description "tool request for already-surfaced tool"; signal with
   description matching by token overlap above
   `KERNOS_FRICTION_TOKEN_OVERLAP_THRESHOLD` (default 0.6); verify
   auto-tagged with `classified_by == 'auto-token-overlap'` and
   `classifier_score < 1.0`.
3. **`test_path_a_wins_over_path_b`** — pattern A has matching
   signal_type_keys; pattern B has matching description (high
   token-overlap); feed signal that matches both; verify Path A
   chosen (deterministic > algorithmic).
4. **`test_low_confidence_emits_unclassified_event`** — signal with
   no type match and token-overlap below threshold; verify
   `friction.pattern_unclassified` event emitted via event_stream;
   verify NO row written to `friction_pattern_occurrence`.
5. **`test_token_overlap_threshold_env_var_tunable`** — set
   `KERNOS_FRICTION_TOKEN_OVERLAP_THRESHOLD=0.9`; signal that would
   have matched at 0.6; verify NOT auto-tagged at the higher
   threshold; verify the env var only affects Path B (Path A still
   scores 1.0 deterministically).
6. **`test_classifier_failure_does_not_block_report_write`** —
   inject store that raises on `record_occurrence`; verify friction
   report still written to disk; warning logged.
7. **`test_idempotent_on_report_path_unique_index`** — feed the same
   friction report twice; verify the second `record_occurrence` is a
   no-op (UNIQUE partial index dedupes); pattern occurrence_count
   incremented exactly once. (Codex review Finding 5.)
8. **`test_classifier_dispatches_record_occurrence_for_active`** —
   Codex round 2 Blocker 2. Active pattern; feed matching signal;
   verify classifier hook calls `record_occurrence` (NOT
   `record_recurrence`); verify `classified_by` is `auto-signal-type`
   or `auto-token-overlap`, never the stale `"auto"` value.
9. **`test_classifier_dispatches_record_recurrence_for_resolved`** —
   resolved pattern; feed matching signal; verify classifier hook
   calls `record_recurrence` (NOT `record_occurrence`); verify
   `friction.pattern_recurrence` event emitted.
10. **`test_classifier_drops_archived`** — archived pattern; feed
    matching signal; verify NEITHER record method called; verify
    `friction.pattern_unclassified` event emitted (preserves audit
    trail per the dispatch table in Decision 6).
11. **`test_path_b_normalized_scorer_short_description_returns_zero`**
    — Codex round 2 Blocker 4. Pattern with 2-token description;
    feed signal with 10-token description; verify Path B score is
    0.0 (short-description guard) regardless of overlap.
12. **`test_path_b_normalized_scorer_stopwords_dropped`** — pattern
    description "the tool was used for the request"; signal
    description "the canvas was used for the page"; verify only
    `{"tool", "was", "used", "for", "request"} ∩ {"canvas", "was",
    "used", "for", "page"}` cleaned tokens count; `"the"` and `"was"`
    drop (stopwords list); score reflects only meaningful overlap.

### Reactivation

`tests/test_friction_pattern_store.py::TestReactivation`

1. **`test_record_occurrence_rejects_on_resolved`** — pattern in
   resolved state; calling `record_occurrence` raises ValueError
   pointing the caller at `record_recurrence`. Pattern's
   occurrence_count unchanged. (Codex review Finding 7 — split
   methods.)
2. **`test_record_recurrence_emits_event_without_incrementing`** —
   pattern resolved; call `record_recurrence`; verify
   `friction.pattern_recurrence` event_stream event emitted with
   `resolved_pattern_id`; verify pattern's `occurrence_count` is
   unchanged; verify a row IS written to
   `friction_pattern_occurrence` with `is_recurrence=1`.
3. **`test_threshold_recurrences_trigger_reactivation`** — pattern
   resolved; call `record_recurrence` 3 times within 7 days, all
   `classified_by != 'backfill'`; verify lifecycle transitions to
   reactivated; verify `friction.pattern_reactivated` event emitted;
   verify `record_recurrence` returns True on the threshold-tripping
   call.
4. **`test_below_threshold_recurrences_do_not_reactivate`** —
   pattern resolved; record 2 recurrences within 7 days (below
   default `KERNOS_FRICTION_REACTIVATION_THRESHOLD=3`); verify
   pattern stays resolved.
5. **`test_recurrences_outside_window_do_not_reactivate`** — record
   3 recurrences spread over 30 days (outside 7-day window); verify
   pattern stays resolved.
6. **`test_backfill_recurrences_excluded_from_reactivation`** —
   pattern resolved; call `record_recurrence` 5 times with
   `classified_by="backfill"`; verify pattern stays resolved (Codex
   review Finding 7 — backfill excluded). Then add one
   non-backfill recurrence; if total non-backfill < threshold,
   still no reactivation.
7. **`test_reactivated_pattern_resumes_counting`** — pattern
   reactivated; `record_occurrence` after reactivation; verify
   pattern's `occurrence_count` increments normally; verify
   `record_recurrence` rejects on reactivated state (already past
   resolved).
8. **`test_record_recurrence_rejects_on_active_or_archived`** —
   active pattern: `record_recurrence` raises ValueError pointing
   at `record_occurrence`. Archived pattern: both methods raise
   `PatternArchived`.
9. **`test_backfill_counts_in_normal_frequency_query`** — Codex
   round 2 Finding 8. Pattern active; insert 5 occurrences with
   `classified_by="backfill"`; insert 3 with `classified_by="auto-signal-type"`;
   call `query_frequency(...)` with default arguments; verify result
   is 8 (backfill included). Then call with `exclude_backfill=True`
   (internal-use flag the reactivation logic sets); verify result
   is 3. Backfill rows are normal history for queries; only
   reactivation excludes them.

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
  `update_description`, `set_display_name`, `add_alias`,
  `transition_lifecycle`, `record_occurrence`, `record_recurrence`
  all produce ActionStateRecords through the per-turn collector.
  Reuses the `ctx.action_record_sink` shape established by
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
| Pattern proliferation (every minor edge becomes a new pattern) | Path B threshold gate consolidates near-misses; new pattern creation is explicit operator/IA step, not auto-driven |
| Classifier false-positives (incidental token overlap) | Conservative Path B default threshold (0.6); env-var tunable; Path A signal_type exact-match takes precedence and is itself bounded by the `signal_type_keys` uniqueness invariant (Codex Finding 6) |
| Classifier false-negatives (paraphrased descriptions miss) | Manual classification accessible via Python API (`classified_by="manual"`); unclassified signals surface as `friction.pattern_unclassified` event_stream events for human review |
| Reactivation noise (stale reports re-trigger) | 3-in-7-days threshold conservative; backfill explicitly excluded from reactivation logic (Codex Finding 7) so historical-import reports never re-trigger; only post-`resolved_at` non-backfill rows count |
| Stable-ID drift via rename | `pattern_id` is immutable post-creation; identity-mutation methods touch `display_name`, `description`, and `aliases` only (Codex Blocker 2) |
| Cross-instance pattern collision | Composite PK `(instance_id, pattern_id)` makes same-slug-across-instances independent records by design (Codex Blocker 1) |
| Cross-instance counter sync issues | Avoided entirely — per-instance counters in shared `data/instance.db`; cross-instance aggregation is a SQL query over `instance_id`-tagged rows, not a stored value |
| Same friction report double-counted | Partial UNIQUE index `(instance_id, pattern_id, report_path) WHERE report_path != ''` deduplicates reprocessed reports (Codex Finding 5) |
| Friction-report filename collisions | UUID8 suffix in `friction.py:_write_report` filename pattern (Codex Finding 8); ships in same batch as catalog implementation |
| Spec frontmatter format drift | Parser tolerant of malformed YAML (returns empty list rather than raising); commit-message ref still works as fallback |
| Catalog schema drift between `instance.db` consumers | Self-managed schema in `FrictionPatternStore.ensure_schema()` mirrors the per-module-isolation pattern from REFERENCE-PRIMITIVE-V1 |

## Open questions resolved by Codex pre-spec review

All five questions from v1 of this spec resolved by Codex review folded
2026-05-10 (architect's calls at Notion `35cffafef4db8192a2efc3a0e3c23288`):

1. ✅ **Slug collision strategy** — numeric suffix retained; immutable
   `pattern_id` (Blocker 2) means there's no rename path that could
   reshuffle suffixes, so the simpler scheme is sufficient.
2. ✅ **Two-table preserved** — pattern + occurrence rows in two tables.
   Composite FK + UNIQUE partial index (Finding 5) tightens the
   referential integrity story; collapse to one-table not pursued.
3. ✅ **Classifier threshold bifurcated** — Path A (signal_type exact)
   scores 1.0 deterministically; Path B (token-overlap) has its own
   tunable threshold. (Finding 6.)
4. ✅ **Backfill: one-shot script** — confirmed, with explicit
   `classified_by="backfill"` marking so reactivation logic excludes
   imported pre-`resolved_at` rows. (Finding 7.)
5. ✅ **`record_recurrence` is a separate method** — different
   lifecycle effects warrant separate surfaces. (Finding 7.)

## Sequence (per architect directive)

1. ✅ CC drafts spec at `specs/FRICTION-PATTERN-STABLE-IDS-V1.md` on branch
   `friction-pattern-stable-ids-v1` (commit `1f15069`).
2. ✅ **Codex pre-spec review round 1** — caught two real blockers
   (per-instance PK contradiction; rename-changing-ID weakens
   stable-ID promise) and six adjustments. Worth pinning as a worked
   example of why the three-tier review chain matters.
3. ✅ **CC folds Codex review round 1** into spec body — v2 revision
   (commit `d74677f`). Eight findings folded per architect's calls
   at Notion `35cffafef4db8192a2efc3a0e3c23288`.
4. ✅ **Codex pre-spec review round 2** — caught nine implementation
   blockers and tightenings on the v2 spec (stale classifier snippet,
   SQLite FK enforcement, Path B scoring undefined, report-uniqueness
   scope too narrow, signal_type_keys uniqueness not enforced on
   transitions, alias collisions unconstrained, backfill excluded too
   aggressively, friction path text inconsistent). All folded.
5. ✅ **CC folds Codex review round 2** into spec body — this v3
   revision.
6. 🟡 **Architect ratification** of revised spec body — pending.
   Architect reviews diff against `1f15069` (cumulative) and confirms
   folds across both Codex rounds match calls.
7. CC implements per ratified spec on the same branch (`friction.py`
   filename collision-resistance change ships in the same batch per
   round 1 Finding 8).
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
