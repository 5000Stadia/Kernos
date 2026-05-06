# Reference Catalog — Baked Artifacts

> Pre-built reference-catalog artifacts shipped in the repository.
> Hydration on bring-up is hash-validated, instant, and spends no LLM
> calls. Architect verdict: REFERENCE-CATALOG-BAKED-V1, Path B+
> (2026-05-06).

## Why this exists

The reference primitive's catalog — section title, one-line summary,
line range, source hash — is a derived index over the canonical docs
tree. It does not need to be regenerated on every install or every
wipe. The first version of the primitive triggered a full live scan
on every substrate bring-up, which produced a ~40-minute LLM-call
deluge per restart and a guaranteed runaway after every wipe (the
catalog table lived inside the wiped per-instance store).

The baked path moves that work to contribution time. The live agent
no longer pays the canonical-docs cataloging cost on every install;
contributors regenerate the baked artifacts once after a docs change,
commit them, and bring-up loads them in milliseconds.

## Three load-bearing principles

These come from the architect verdict and are non-negotiable:

1. **Bake speeds hydration; hash validation is the trust mechanism.**
   The runtime always validates source-hash before injection
   regardless of whether the catalog row was hydrated from baked
   artifact or live scan. The two concerns stay separable.
2. **The CI gate is hash-comparison only — never an LLM-spending
   surface.** The freshness check verifies that contributors ran
   regen locally; CI never re-spends the regen cost itself.
3. **Degradation is loud, never silent.** Stale or missing baked
   entries produce explicit diagnostic log lines; the runtime never
   silently falls back. Operator visibility on every drift.

## Contributor workflow

After modifying any file under `docs/`:

```bash
python scripts/regenerate_reference_catalog.py
```

The script is idempotent: it only re-catalogs files whose source hash
has changed. Touching one doc costs one regen pass on that file
(typically 5–10 cheap-tier LLM calls, a few seconds).

Pre-commit hook (run once after cloning):

```bash
bash scripts/install_baked_catalog_hook.sh
```

The hook runs the freshness check before every commit. If a docs
change is staged without a corresponding regen, the commit is
blocked with a clear diagnostic. The hook spends zero LLM calls.

## Files in the repo

```
docs/_catalog/
├── _manifest.json                # whole-tree integrity manifest
├── architecture/
│   ├── canvas.json               # one artifact per source file
│   └── …
├── concepts/
│   └── …
└── …
```

The directory structure mirrors the source tree: `docs/architecture/canvas.md`
becomes `docs/_catalog/architecture/canvas.json`. PR diffs show the
catalog change next to the doc change.

## Manifest format

```json
{
  "version": 1,
  "generated_at": "2026-05-06T00:00:00Z",
  "entries": {
    "docs/architecture/canvas.md": {
      "source_hash": "<sha256-of-source>",
      "artifact_path": "docs/_catalog/architecture/canvas.json"
    }
  }
}
```

Sorted entries; trailing newline; deterministic for git diffs.

## Artifact format

```json
{
  "file_path": "docs/architecture/canvas.md",
  "source_hash": "<sha256>",
  "generated_at": "2026-05-06T00:00:00Z",
  "sections": [
    {
      "section_title": "Scope model",
      "one_line": "Three scope tiers, fixed at creation, non-negotiable.",
      "line_start": 12,
      "line_end": 28
    }
  ]
}
```

## What happens on bring-up

1. Loader reads `docs/_catalog/_manifest.json`.
2. For each manifest entry, it computes the current source-file hash
   and compares against the manifest entry's recorded hash.
3. Matched files: the per-file artifact is loaded and bulk-imported
   into the runtime catalog store via `replace_file_entries`.
4. Mismatched files (source mutated since regen): a per-file warning
   logs and the file is skipped. The live-scan path picks up the
   mismatch on first `request_reference` via hash-mismatch-on-retrieval.
5. A single summary log line surfaces totals:
   `REFERENCE_BAKED_HYDRATION: loaded=N sections=S stale=M missing_artifact=K …`

If the manifest is absent (fresh checkout, first-ever install before
regen runs), the loader returns an inert summary and the existing
`KERNOS_REFERENCE_FIRST_BOOT_SCAN` opt-in flag is the explicit
override path.

## Recovery / dev hatch

`KERNOS_REFERENCE_FIRST_BOOT_SCAN=1` triggers the live cataloging
scan as before. Use cases:

- The baked artifact is missing or stale and you want to populate the
  catalog without running the contributor regen script.
- You're iterating on docs and don't want to regen between every save.
- You're testing recovery behavior end-to-end.

The flag is the explicit recovery hatch; it is not the default path.

## What is unchanged

The live cataloging path for `data/references/` (agent-authored
content) is untouched. The cataloging cohort, the injection layer,
the tool surface, the navigator — all unchanged. The baked path
only intercepts the canonical `docs/` tree at bring-up.

## Verification

The freshness check runs as a pytest test, so every `pytest` invocation
gates on a stale catalog. The test skips with a clear message until
the first regen bootstraps the manifest. The standalone check is also
available for any CI surface that wants to gate without running tests:

```bash
python scripts/check_reference_catalog_freshness.py
```

## Embedded live test

`python scripts/baked_catalog_live_test.py` exercises the full
hydration path against a synthetic temp tree:

- Clean install loads from baked in milliseconds.
- Partial drift surfaces loud diagnostics + correct mixed-mode behavior.
- Freshness check catches stale baked content.

A report lands under `data/diagnostics/live-tests/`.
