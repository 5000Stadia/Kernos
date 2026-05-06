# Reference Primitive

REFERENCE-PRIMITIVE-V1 (shipped 2026-05-05). A referential self-documentation primitive distinct from canvas (mutable workspace) and memory (interaction-derived). It supports two use cases through one mechanism: canonical Kernos self-documentation reachable from inside the running agent, and agent-stored project-deep reference material that accumulates as the agent works.

The primitive is the reach mechanism agents use to find canonical information about how Kernos works. The previous direct-path `read_doc` tool was retired here under the retired-by-architectural-transition discipline.

## Why it exists

Pre-v1 the agent reached `docs/` via a single tool call (`read_doc`). The CANVAS-AUDIT-AND-SHAPE-V1 audit surfaced the failure mode empirically: the agent had substrate awareness of `docs/` but didn't reach for it reliably — surfacer-discretion gating produced a reachability gap. Three independent surfacing paths now replace the single tool-call gating: explicit retrieval via `request_reference`, mechanical auto-induction via signal-matching, and hatching-time substrate awareness baked into the bootstrap.

This composes with the substrate-with-awareness invariant: not just substrate that exists, but substrate Kernos knows about and can reach for from hatching forward.

## Architectural shape

A **catalog** (live structured state, not append-only ledger) sits over **source files on disk** in two locations: `docs/` (canonical, ships with install) and `data/references/<space_id>/` (agent-stored, accumulates per-domain). A **cataloging cohort** runs async on file change and produces per-section catalog entries containing title, one-line description, file path, line range, and source hash. **No content is duplicated into the catalog** — entries are pointers plus metadata.

Content reaches the agent through three paths:

- **Explicit retrieval** via `request_reference(brief_request)` — agent asks; reference cohort navigates the catalog (one cheap-tier LLM call); algorithmic injection delivers the matched section content into the next tool result with hash validation fail-closed before injection.
- **Auto-induction** via signal-matching against catalog metadata — mechanical, no LLM, conservative defaults.
- **Hatching-time awareness** — the bootstrap prompt teaches the agent that `request_reference` and `store_reference` exist and how to reach for them.

Most interactions cost zero LLM calls. Retrievals cost one cheap-tier call (cohort navigation). Cataloging amortizes over each file's lifetime.

## Module layout

The primitive lives in `kernos/kernel/reference/`:

| Module | Purpose |
|---|---|
| `catalog.py` | `CatalogEntry` dataclass + `CatalogStore` over a dedicated aiosqlite connection on `instance.db`. Owns the `reference_catalog` table. |
| `events.py` | Twelve event shapes + `ReferenceEventEmitter` mirroring the CRB pattern; registers `"reference"` with the EmitterRegistry. |
| `cohort.py` | Async `CatalogingCohort`. Section splitter at h2; cheap-tier one-line description per section; transactional drop-and-rebuild per file. |
| `ingest.py` | `IngestionScanner` walks source roots, hashes files, enqueues changes, tombstones vanished files. |
| `injection.py` | Algorithmic content delivery with hash validation — fail-closed on mismatch. |
| `induction.py` | Mechanical signal-matching against catalog metadata. Bounded set N=2, budget cap, trust-tier modulated thresholds. |
| `tools.py` | Seven agent-facing tools + `ReferenceService` dispatcher. |
| `bringup_adapters.py` | `ReferenceCheapLLMAdapter` over `ReasoningService.complete_simple(prefer_cheap=True)`. |

## Catalog entry shape

Metadata-only; no content payload. Per-section ~150-300 bytes typical. Two entry types share one table.

**File-level entry:**

| Field | Notes |
|---|---|
| `entry_id` | Stable identifier |
| `instance_id` | Multi-instance keying |
| `entry_type` | `'file'` |
| `scope` | `'instance'` or `'domain:<space_id>'` |
| `category` | `docs/` subfolder OR collection name under `references/` |
| `file_path` | Absolute path to source file |
| `section_title` | Section heading text (h2) |
| `one_line` | Cheap-LLM-generated description for navigation |
| `line_start`, `line_end` | Section bounds |
| `source_hash` | SHA-256 of full file content at indexing time |
| `indexed_at` | Timestamp |
| `collection_back_reference` | Collection identifier when the file lives inside a collection |
| `trust_tier` | `canonical` \| `agent_authored` \| `external_snapshot` \| `quarantined` |
| `auto_inducible` | Boolean — eligible for auto-induction |
| `provenance_metadata` | Tier-dependent dictionary |
| `tombstoned`, `tombstoned_at`, `tombstoned_reason` | Soft-deletion |

**Collection-level entry** (one per collection in `references/`): adds `collection_name`, `purpose`, `refresh_policy`, `member_file_count`, `member_file_paths`, `last_refreshed_at`. Surfaces as a map (not a content bundle) when the agent's signal matches the collection's purpose rather than a specific section.

**Future-proof scope fields** (Kit scoping revision): `owner_domain_id`, `promoted_from`, `docs_version`. Wired now even though only instance and domain scopes are exercised in v1, so V2 cross-domain promotion + mixed-version operation don't require migration.

## Trust tiers

Four classes; flow through both retrieval and auto-induction conservativeness.

| Tier | Source | Auto-induces? | Surface annotation |
|---|---|---|---|
| `canonical` | `docs/`-derived. Full canonical authority. | Yes, on confident matches | None (canonical is the default) |
| `agent_authored` | Kernos's own observations or compiled notes stored as reference. | Yes, on confident matches | "Agent-authored reference (stored by X)." |
| `external_snapshot` | Pulled from an external source at a specific time. Provenance required (`source_url`, `fetched_at`, `content_hash`, `refresh_policy`). | Only on **strong** matches (higher threshold) | "Snapshot from {url} on {fetched_at}; not canonical live truth." |
| `quarantined` | Uncertain reliability; pending verification or source disputed. | **Never** | "Quarantined: {reason}" |

## Scope and visibility rule

The catalog has a `scope` field on each entry:

- `instance` — `docs/`-derived. Visible to all domains.
- `domain:<space_id>` — `references/`-derived. Visible only to retrievals from agents acting in that space.

**Retrieval visibility rule (the load-bearing testable invariant):**

> The visible catalog set for a given retrieval is `(scope == "instance") OR (scope == "domain:<retrieval_context.domain_id>")`.

`retrieval_context.domain_id` is bound to the agent's current space at dispatch time, NOT a parameter the agent can pass. Pin tests verify mechanically that an agent acting in space X can never retrieve a catalog entry scoped to space Y.

The visibility check fires at three layers:

1. `CatalogStore.list_visible()` — read path (used by retrieval + auto-induction).
2. `induce()` — auto-induction filter, identical visibility rule.
3. `_caller_can_mutate()` in the service — defense-in-depth on the four recovery primitives, so an agent can't mutate a foreign-domain entry by guessing its `entry_id`.

**Domain mapping (CC implementation latitude per spec).** `domain_id ≡ space_id`. The architect primer's "General → Domain → Subdomain" hierarchy is colloquial language for the depth-keyed space hierarchy already in the codebase. The future-proof fields (`owner_domain_id`, `promoted_from`) are wired so a future literal `domain` primitive can layer on without migration.

## Storage layout on disk

```
docs/                                  # instance-scoped
  architecture/
  behaviors/
  capabilities/
  primitives/
  ...

data/references/
  <space_id>/                          # domain-owned
    <collection_name>/
      _collection.json
      <file_1>.md
      ...
```

`docs/` ships from the GitHub release at install time; auto-update re-pulls on subsequent boots. `data/references/` accumulates as agents work (operator-data, survives updates).

## Tool surface

Seven agent-facing tools wired through `ReferenceService` and dispatched via `ReasoningService._handle_reference_tool`. All seven appear in the canonical kernel-tool registry and pass through the dispatch gate.

| Tool | Effect | Purpose |
|---|---|---|
| `request_reference(brief_request)` | `read` | Agent describes what it wants; cohort navigates the catalog (one cheap-tier LLM call) and picks a matching `entry_id`; algorithmic injection delivers the section content with a trust-tier annotation. |
| `store_reference(content, collection, filename, trust_tier, metadata)` | `soft_write` | Writes a markdown file under `data/references/<space_id>/<collection>/<filename>` and enqueues async cataloging. `trust_tier` is `agent_authored` or `external_snapshot`. |
| `create_reference_collection(name, purpose, trust_tier, refresh_policy, provenance)` | `soft_write` | Creates a new collection by writing `_collection.json`; collection-level catalog entry generated async. |
| `move_reference_to_canvas(entry_id, target_canvas)` | `soft_write` | Recovery primitive: tombstones the catalog entry; the canvas write is a follow-up step. |
| `mark_reference_superseded(old_entry_id, new_entry_id, reason)` | `soft_write` | Recovery primitive: tombstones old, links new in old's provenance. Both entries must exist; new must be non-tombstoned. |
| `quarantine_reference(entry_id, reason)` | `soft_write` | Recovery primitive: flips trust tier to `quarantined`; auto_inducible flag flips off; prior tier preserved in provenance. |
| `restore_reference_from_quarantine(entry_id)` | `soft_write` | Recovery primitive: restores prior trust tier from provenance. |

Recovery primitives all enforce the cross-domain mutation guard — the caller must be able to SEE the entry under the visibility rule before they can mutate it. An agent in space X with a guessed `entry_id` from space Y is rejected.

## Algorithmic injection (with hash validation)

After the navigator returns an `entry_id`, content delivery is purely mechanical — no LLM call.

1. Look up entry. Unknown / tombstoned → fail closed.
2. Stat the file. Vanished → emit `reference.recatalog_requested_due_to_hash_mismatch` with `observed_hash="<vanished>"`, tombstone the catalog rows, return fail-closed.
3. Compute current hash; compare to `entry.source_hash`.
   - **Match** → read line range, build trust-tier annotation, return.
   - **Mismatch** → fail closed. Enqueue async re-cataloging via the cohort. Emit the audit event carrying both hashes. Return the user-facing message: `"Reference unavailable, recataloging in progress; please retry."`

Fail-closed behavior on hash mismatch guarantees the agent never receives stale content delivered with confidence. The mismatch path also self-heals: by next turn, the cohort has re-cataloged, and the retry succeeds.

## Auto-induction

Mechanical signal-matching, no LLM call. The integration layer assembles next-turn briefings; it includes a signal-matching pass against catalog metadata (one_line + section_title + category + collection_name + purpose). Catalog entries whose tokens overlap with current conversation signals become candidates.

Conservative defaults (per Kit's pre-spec review — "auto-induction should not try to be smart"):

- **Bounded set rule:** at most **N=2** sections auto-induce per turn. Beyond N, additional matches surface as `(section_title, one_line)` pairs without content; the agent can `request_reference` explicitly for any that look useful.
- **Budget cap:** total auto-induced content per turn is capped at **8000 tokens**. A single match exceeding the cap surfaces with the "section too large to auto-induce; explicitly request to retrieve" hint.
- **Trust-tier threshold modulation:** `canonical` and `agent_authored` auto-induce on confident matches (≥2 tokens overlap); `external_snapshot` requires stronger overlap (≥4); `quarantined` never auto-induces.
- **Collection-level vs file-level matching:** file match → algorithmic injection of the section. Collection match → surface the collection-level catalog entry (purpose, member_file_count, provenance). **Never a bundle of all member files.** The agent retrieves a specific member file explicitly if needed.

Tokenizer applies minimal plural-s normalization (`covenants` → `covenant`) so the matcher handles common English plurals without a full stemmer.

## Cataloging cohort

Cheap-tier LLM cohort that produces catalog entries from source files. Runs **async, off any agent-facing hot path**. Detailed in [`cohort-cataloging.md`](cohort-cataloging.md).

## Per-turn ingestion check (trigger-driven, not per-turn)

`IngestionScanner` walks registered source roots, hashes files, enqueues changes via the cohort, tombstones vanished files. The original spec permitted per-turn invocation; production wiring is trigger-driven instead — per-turn fan-out is wasteful given the actual change events.

Trigger points:

- **First-boot bring-up** — `bring_up_substrate` adds the `docs/` source root and dispatches `scan()` as a fire-and-forget background task. Catalog hydrates from a fresh install.
- **Auto-update path** — the existing self-update mechanism does `git pull --ff-only` then `execv` to restart. The new process's bring-up runs first-boot scan against the freshly-pulled `docs/`. So GitHub-driven docs updates flow through automatically.
- **Per-tool path** — `store_reference` and `create_reference_collection` enqueue cataloging directly through the tool layer. No scan needed.
- **Hash-validation-on-retrieval** — when an agent retrieves a stale entry, injection's hash check fails closed AND inline-enqueues a recatalog. Drift gets surfaced and corrected at first use.

Net result: steady-state cost of zero scans per turn. The only scenario that lacks immediate detection is a same-process out-of-band edit to `docs/` without restart, which the lazy hash-validation backstop covers.

## Event shapes

Twelve named event types emit through the event_stream substrate via the registered `"reference"` source_module:

| Event | When |
|---|---|
| `reference.cataloged` | First-time cataloging of a file |
| `reference.recataloged` | Hash changed; transactional drop-and-rebuild lands |
| `reference.recatalog_failed` | Per-file rebuild raised; partial-state never persists (transactional) |
| `reference.tombstoned` | File deleted on disk; catalog rows tombstoned |
| `reference.stored` | Agent stored new reference material via `store_reference` |
| `reference.superseded` | Explicit supersession via `mark_reference_superseded` |
| `reference.quarantined` | Entry flipped to quarantined trust tier |
| `reference.restored_from_quarantine` | Entry restored to prior trust tier |
| `reference.moved_to_canvas` | Recovery primitive: realize the material should be canvas-shaped |
| `reference.collection_created` | First-time collection cataloging |
| `reference.collection_refreshed` | `_collection.json` mtime newer than recorded `last_refreshed_at` |
| `reference.recatalog_requested_due_to_hash_mismatch` | Injection-side stale-hash trigger; observed both at file-vanished and content-drift paths |

The `"reference"` source_module is registered exactly once at substrate bring-up via the EmitterRegistry singleton; spoofing is structurally impossible.

## What this primitive is NOT for

- **Mutable workspace content** — that's canvas. If the agent encounters something that needs free-form editing, it should reach for canvas, not reference. The recovery primitive `move_reference_to_canvas` exists for the case where stored material later turns out to be workspace-shaped.
- **Interaction-derived facts** — that's memory. Things the agent learned by talking to someone go in the memory ledger, not as references.
- **Per-member private notes** — explicitly out of scope for v1. References are domain-owned, not member-owned. Private scratch material should reach for memory, canvas, or credentials, depending on shape.
- **Live-aliased shared sources** — references are snapshot-with-provenance. External-snapshot trust tier captures this explicitly with `source_url` and `fetched_at`.

## Out of scope for v1

- Refresh-policy automation for `expires_after_N_days` snapshots (metadata recorded; automation is V2).
- Cross-domain reference promotion (snapshot/copy with provenance shape pre-described in the spec; future-proof fields wired now).
- Multi-instance / cross-instance reference sharing (composes with KERNOS-MESH, deferred).
- Auto-promotion of agent-authored references to `docs/` (operator-mediated only).

## How to use it as the agent

Use `request_reference` when you need to know how Kernos works. Use `store_reference` when you've accumulated material you'll want to retrieve later. The bootstrap prompt teaches both at hatching time. See [`capabilities/references.md`](../capabilities/references.md) for the agent-facing usage guide.
