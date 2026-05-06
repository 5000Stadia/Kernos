# Cohort: Cataloging

The cataloging cohort is the cheap-tier LLM worker that turns reference source files into catalog rows. It belongs to the [reference primitive](reference-primitive.md) and runs async, off any agent-facing hot path.

## What the cohort does

Two kinds of work, both async:

1. **Per-file cataloging.** When a markdown file is enqueued (by the ingestion scanner, by `store_reference` writes, or by an injection-time hash mismatch), the cohort:

   - Reads the file.
   - Splits it into sections at markdown `## ` (h2) headings. Files without h2 produce a single section spanning the whole file (title = h1 if present, else filename stem).
   - Computes a stable SHA-256 of the full file content as the `source_hash`.
   - For each section, asks the cheap-tier LLM for a one-line description (capped at ~120 chars). Small prompts; the LLM only sees the section title plus a bounded body excerpt, not the whole file.
   - Hands the new `CatalogEntry` list to `CatalogStore.replace_file_entries()` — transactional drop-and-rebuild.
   - Emits `reference.cataloged` (first ingest) or `reference.recataloged` (subsequent).

2. **Per-collection cataloging.** When `_collection.json` is enqueued:

   - Reads the metadata file.
   - If `purpose` is non-empty, uses it directly. Otherwise asks the cheap-tier LLM for a one-paragraph purpose summary derived from the collection's metadata + member-file titles.
   - Upserts the collection-level `CatalogEntry`.
   - Emits `reference.collection_created` (first time) or `reference.collection_refreshed` (subsequent).

## Why async

Cataloging is the only routine LLM work the reference primitive performs. Once cataloging is done, retrieval and auto-induction operate mechanically against the catalog (one cheap-tier navigator call on `request_reference`; zero LLM calls in auto-induction or injection). Cataloging is also relatively rare — only fires on file change. Most turns trigger zero LLM work for this primitive.

The cohort is a single asyncio task that drains a queue. The agent-facing hot path never blocks on cataloging; the cohort completes work in the background and the catalog converges within one or two turns of a file change landing.

## File-level rebuild is transactional

When a file's hash changes, the cohort **drops all existing catalog entries for that file and rebuilds the full set from scratch**. Either the whole file's new catalog set lands, or nothing changes. Partial cataloging is forbidden — any failure mid-rebuild rolls back via `BEGIN IMMEDIATE`/`ROLLBACK` on the catalog store; no orphan entries; no partial-old + partial-new state.

This is drift-proof and dedup-proof by construction. The catalog's relationship to a file is "this file's current sections, as cataloged at this hash." No accumulation possible.

Within a collection, per-file rebuild applies independently. If 1 of 30 member files changes, only that file's entries rebuild; other files' entries and the collection-level entry are untouched.

## LLM failure handling

If the cheap-tier LLM call for a section's one-liner fails (network error, rate limit, refusal), the cohort falls back to using the section title as the one-liner. The rebuild never blocks on LLM availability. Worst case: less-helpful navigation hints, but the catalog row exists, the line range is correct, and retrieval still works.

If the entire per-file rebuild raises (e.g., transactional commit failure), the catalog state is unchanged and `reference.recatalog_failed` emits with the reason. The next trigger (per-tool re-store, hash-validation retry) gets another shot.

## Vanished-file handling

A file enqueued for cataloging that no longer exists at pickup time is tombstoned cleanly via `CatalogStore.tombstone_file()`. The cohort does NOT raise on missing files; it logs and tombstones. The ingestion scanner (`IngestionScanner._tombstone_vanished_files`) handles steady-state deletion detection; this cohort path covers the race where a file existed at enqueue and not at pickup.

## In-flight tracking

The cohort keeps an `_in_flight: set[(kind, file_path)]` of items currently being processed. The `drain()` helper (test convenience) waits on both queue-empty AND `_in_flight` empty, so tests can deterministically wait for async work without race. Production callers don't await `drain()` — agent-facing turns must remain non-blocking.

## LLMClient contract

The cohort takes a generic `CatalogingLLMClient` Protocol with:

```python
@property
def temperature(self) -> float: ...
async def complete(self, prompt: str) -> str: ...
```

Same shape as `CRBProposalAuthor.LLMClient`. Production wiring is `ReferenceCheapLLMAdapter` over `ReasoningService.complete_simple(prefer_cheap=True)` with `max_tokens=256`. Tests inject stubs (deterministic short strings).

## Section split heuristic

`split_sections(file_text, file_path)` returns `[(section_title, line_start, line_end), ...]`. The rule:

- Lines matching `^##\s+` (exactly h2; h3+ excluded) split the file.
- Each section starts at its h2 line and runs to either the next h2 or EOF.
- Files with at least one h2 produce one section per h2.
- Files without h2 produce a single section spanning the whole file. Title = h1 if present, else filename stem (e.g. `notes` for `notes.md`).

This is stable across the docs/ tree. The docs that ship today use h2 as the section boundary consistently; no special-casing of frontmatter or h3-only structures needed.

## Where the cohort runs

Constructed at substrate bring-up (`bring_up_substrate`) and added to the substrate dataclass as `reference_cohort`. The cohort starts on bring-up and stops first in tear-down (before the catalog store closes its DB connection) so any in-flight rebuilds drain cleanly.

The cohort instance is shared across the whole process — single async task, single queue. Multiple enqueue paths (scanner, agent tools, injection hash-mismatch) all funnel into it.
