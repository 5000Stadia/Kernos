"""REFERENCE-CATALOG-BAKED-V1 — pre-built catalog artifacts shipped in the repo.

The reference primitive's catalog is a derived index over canonical
documentation. Its rows — section title, one-line summary, line range,
source hash — are deterministic given the source tree and the
cataloging cohort's prompt template. They do not need to be regenerated
on every install or every wipe; that path was the cause of the
~40-minute LLM-call deluge per restart that the env-var hot-fix
(``KERNOS_REFERENCE_FIRST_BOOT_SCAN``, default off) now gates.

This module ships the architecturally-correct path: bake the catalog
into the repository at contribution time, hydrate from the baked
artifacts at bring-up time. Three load-bearing principles, set by
the architect verdict (Path B+, 2026-05-06):

1. **Bake speeds hydration; hash validation is the trust mechanism.**
   The runtime always validates source-hash before injection regardless
   of whether the catalog row was hydrated from baked artifact or live
   scan. The two concerns are separable and stay separable.
2. **The CI gate is hash-comparison only — never an LLM-spending
   surface.** The freshness check verifies that contributors ran regen
   locally before committing; it never spends LLM calls itself. This
   keeps regen cost contained at contribution time.
3. **Degradation is loud, never silent.** Stale or missing baked
   entries produce explicit diagnostic log lines, not silent fallback.
   Operator visibility on every drift between baked and source.

This module ships in REFERENCE-CATALOG-BAKED-V1.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kernos.kernel.reference.catalog import (
    CatalogEntry,
    CatalogStore,
    ENTRY_TYPE_FILE,
    compute_file_hash,
    compute_source_hash,
)
from kernos.utils import utc_now

logger = logging.getLogger(__name__)


# Per-section payload baked into the artifact. Mirrors the load-bearing
# fields of CatalogEntry that the cataloging cohort produces; the rest
# (entry_id, indexed_at, scope, category, trust_tier, etc.) are
# reconstructed at load time from source-root metadata + utc_now.
@dataclass(frozen=True)
class BakedSection:
    section_title: str
    one_line: str
    line_start: int
    line_end: int


# Per-file artifact. Lives at ``docs/_catalog/<rel_path>.json`` —
# mirroring the source directory structure rather than flattening with
# separators. Cleaner PR diffs (the catalog change shows next to the
# doc change in tree views) and natural collision-free naming.
@dataclass(frozen=True)
class BakedArtifact:
    file_path: str          # path relative to repo root, e.g. "docs/architecture/canvas.md"
    source_hash: str        # SHA-256 of the source bytes, matches catalog.compute_source_hash
    generated_at: str       # ISO-8601 UTC timestamp from utc_now()
    sections: tuple[BakedSection, ...]

    def to_json(self) -> str:
        """Stable, sorted-key serialization for deterministic git diffs."""
        return json.dumps(
            {
                "file_path": self.file_path,
                "source_hash": self.source_hash,
                "generated_at": self.generated_at,
                "sections": [
                    {
                        "section_title": s.section_title,
                        "one_line": s.one_line,
                        "line_start": s.line_start,
                        "line_end": s.line_end,
                    }
                    for s in self.sections
                ],
            },
            indent=2,
            sort_keys=False,
            ensure_ascii=False,
        ) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "BakedArtifact":
        data = json.loads(text)
        sections = tuple(
            BakedSection(
                section_title=s["section_title"],
                one_line=s["one_line"],
                line_start=int(s["line_start"]),
                line_end=int(s["line_end"]),
            )
            for s in data["sections"]
        )
        return cls(
            file_path=data["file_path"],
            source_hash=data["source_hash"],
            generated_at=data["generated_at"],
            sections=sections,
        )


# Whole-tree integrity manifest. Maps each cataloged source file to its
# source_hash + artifact_path. Two purposes:
#   1. Fast staleness check at bring-up — read one small file, compare
#      hashes, decide load-vs-skip per entry without opening 100+ JSON
#      files.
#   2. CI gate — hash-comparison only; the freshness check walks docs/,
#      computes current hashes, fails if any drift from manifest.
# v1 ships this; v2 may layer additional per-collection metadata.
@dataclass(frozen=True)
class BakedManifest:
    version: int
    generated_at: str
    entries: dict[str, dict[str, str]]  # file_path -> {source_hash, artifact_path}

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "generated_at": self.generated_at,
                "entries": {
                    k: dict(v) for k, v in sorted(self.entries.items())
                },
            },
            indent=2,
            sort_keys=False,
            ensure_ascii=False,
        ) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "BakedManifest":
        data = json.loads(text)
        return cls(
            version=int(data["version"]),
            generated_at=data["generated_at"],
            entries={
                k: {"source_hash": v["source_hash"], "artifact_path": v["artifact_path"]}
                for k, v in data["entries"].items()
            },
        )


MANIFEST_VERSION = 1
MANIFEST_FILENAME = "_manifest.json"
CATALOG_DIRNAME = "_catalog"


def manifest_path_for(catalog_root: Path) -> Path:
    """Where the manifest lives. Always one path; one source of truth."""
    return catalog_root / MANIFEST_FILENAME


def artifact_path_for(catalog_root: Path, source_relpath: str) -> Path:
    """Map a source file's repo-relative path to its artifact path.

    ``docs/architecture/canvas.md`` →
    ``<catalog_root>/architecture/canvas.json``

    The leading source-root directory ("docs") is stripped because the
    catalog root is itself rooted there; remaining path components
    mirror the source structure with ``.md`` replaced by ``.json``.
    """
    parts = source_relpath.split("/")
    # Strip the first component (the source-root name, e.g. "docs"); the
    # catalog root is already inside that directory.
    if parts and parts[0] == "docs":
        parts = parts[1:]
    if not parts:
        raise ValueError(f"refusing to map empty path: {source_relpath!r}")
    parts[-1] = parts[-1].replace(".md", ".json")
    return catalog_root.joinpath(*parts)


def source_relpath_for(repo_root: Path, source_path: Path) -> str:
    """Repo-relative POSIX-style path for a source file."""
    return source_path.relative_to(repo_root).as_posix()


# --- Loader -----------------------------------------------------------


@dataclass
class LoadSummary:
    """Per-bring-up summary returned by ``load_baked_catalog``.

    Fields are intentionally simple counts; the loud diagnostic lines
    (per-file mismatch, missing artifact, etc.) go through ``logger``
    so operators see them in console output. The summary is for the
    single ``REFERENCE_BAKED_HYDRATION`` log line at the end.
    """

    manifest_present: bool = False
    manifest_invalid: bool = False
    files_loaded: int = 0
    files_stale: int = 0
    files_missing_artifact: int = 0
    files_artifact_invalid: int = 0
    files_uncatalogued: int = 0  # in docs but not in manifest
    sections_imported: int = 0


def _category_for_doc(source_path: Path, docs_root: Path) -> str:
    """First subdirectory under docs/ → category; root files → ``root``.

    Mirrors ``kernos.kernel.reference.ingest._category_for_file``; kept
    inline here so the loader doesn't import scanner internals.
    """
    try:
        rel = source_path.relative_to(docs_root)
    except ValueError:
        return "root"
    parts = rel.parts
    if len(parts) <= 1:
        return "root"
    return parts[0]


async def load_baked_catalog(
    *,
    docs_root: Path,
    catalog_root: Path,
    instance_id: str,
    catalog_store: CatalogStore,
    scope: str,
    trust_tier: str,
    owner_domain_id: str,
) -> LoadSummary:
    """Load baked catalog artifacts into the runtime catalog store.

    Walks the manifest, validates each entry's source hash against the
    current source file, and bulk-imports matching files. Files that
    fail validation (stale baked, missing artifact, malformed
    artifact) are skipped with a loud per-file warning; the live-scan
    path picks them up later via ``request_reference``'s
    hash-mismatch-on-retrieval recatalog trigger.

    Files present in ``docs/`` but absent from the manifest are
    counted under ``files_uncatalogued`` and surfaced as a single
    summary diagnostic — this is the "someone added docs without
    running regen" case.

    Returns a ``LoadSummary`` for diagnostic surfacing. Caller should
    log the summary at INFO and any nonzero stale/missing/uncatalogued
    counts at WARNING — see ``bring_up_substrate.py``'s integration.
    """
    summary = LoadSummary()

    manifest_p = manifest_path_for(catalog_root)
    if not manifest_p.exists():
        summary.manifest_present = False
        return summary
    summary.manifest_present = True

    try:
        manifest = BakedManifest.from_json(manifest_p.read_text(encoding="utf-8"))
    except Exception as exc:
        summary.manifest_invalid = True
        logger.warning(
            "REFERENCE_BAKED_MANIFEST_INVALID path=%s exc=%s — falling back to "
            "live-scan path for all files",
            manifest_p, exc,
        )
        return summary

    # Discover docs not in the manifest (ran regen forgotten / new doc added)
    repo_root = docs_root.parent
    on_disk: set[str] = set()
    for md in sorted(docs_root.rglob("*.md")):
        if md.is_file() and not md.name.startswith("_"):
            on_disk.add(source_relpath_for(repo_root, md))
    summary.files_uncatalogued = len(on_disk - set(manifest.entries.keys()))

    for source_relpath, entry in sorted(manifest.entries.items()):
        source_p = repo_root / source_relpath
        if not source_p.exists():
            # Manifest stale on the other axis (deletion). Live-scan's
            # tombstone-vanished path will eventually catch up; here we
            # just decline to hydrate a row that points at nothing.
            logger.warning(
                "REFERENCE_BAKED_SOURCE_VANISHED path=%s — manifest entry "
                "skipped; recommend rerunning regen to drop the stale row",
                source_relpath,
            )
            summary.files_stale += 1
            continue

        try:
            current_hash = compute_file_hash(source_p)
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "REFERENCE_BAKED_HASH_READ_FAILED path=%s exc=%s",
                source_relpath, exc,
            )
            summary.files_stale += 1
            continue

        if current_hash != entry["source_hash"]:
            logger.warning(
                "REFERENCE_BAKED_STALE path=%s baked_hash=%s current_hash=%s — "
                "skipping baked load; live-scan will pick up via hash-mismatch",
                source_relpath, entry["source_hash"][:12], current_hash[:12],
            )
            summary.files_stale += 1
            continue

        artifact_p = repo_root / entry["artifact_path"]
        if not artifact_p.exists():
            logger.warning(
                "REFERENCE_BAKED_ARTIFACT_MISSING path=%s artifact=%s — "
                "manifest references absent file; rerun regen",
                source_relpath, entry["artifact_path"],
            )
            summary.files_missing_artifact += 1
            continue

        try:
            artifact = BakedArtifact.from_json(
                artifact_p.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "REFERENCE_BAKED_ARTIFACT_INVALID path=%s artifact=%s exc=%s",
                source_relpath, entry["artifact_path"], exc,
            )
            summary.files_artifact_invalid += 1
            continue

        # Final defense-in-depth: artifact's recorded source_hash must
        # also match. Manifest hash + artifact hash should be identical
        # because regen writes both atomically; a divergence indicates
        # manual editing of one file out-of-band. Fail closed and force
        # a regen.
        if artifact.source_hash != entry["source_hash"]:
            logger.warning(
                "REFERENCE_BAKED_HASH_DIVERGENCE path=%s manifest_hash=%s "
                "artifact_hash=%s — skipping; rerun regen to resync",
                source_relpath, entry["source_hash"][:12],
                artifact.source_hash[:12],
            )
            summary.files_artifact_invalid += 1
            continue

        # All checks passed. Build CatalogEntry rows for every section
        # and bulk-import via replace_file_entries.
        new_entries: list[CatalogEntry] = []
        from kernos.kernel.reference.catalog import _new_entry_id  # type: ignore[attr-defined]
        now = utc_now()
        category = _category_for_doc(source_p, docs_root)
        for section in artifact.sections:
            new_entries.append(
                CatalogEntry(
                    entry_id=_new_entry_id(),
                    instance_id=instance_id,
                    entry_type=ENTRY_TYPE_FILE,
                    scope=scope,
                    category=category,
                    indexed_at=now,
                    trust_tier=trust_tier,
                    auto_inducible=trust_tier != "quarantined",
                    provenance_metadata={"hydration": "baked"},
                    file_path=str(source_p),
                    section_title=section.section_title,
                    one_line=section.one_line,
                    line_start=section.line_start,
                    line_end=section.line_end,
                    source_hash=artifact.source_hash,
                    collection_back_reference="",
                    owner_domain_id=owner_domain_id,
                ),
            )
        await catalog_store.replace_file_entries(
            instance_id=instance_id,
            file_path=str(source_p),
            new_entries=new_entries,
        )
        summary.files_loaded += 1
        summary.sections_imported += len(new_entries)

    return summary


def write_baked_artifact(
    *,
    catalog_root: Path,
    source_relpath: str,
    artifact: BakedArtifact,
) -> Path:
    """Write a single artifact, creating parent directories.

    The regen script calls this once per cataloged file. Returns the
    path written so the caller can update the manifest entry.
    """
    out = artifact_path_for(catalog_root, source_relpath)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(artifact.to_json(), encoding="utf-8")
    return out


def write_manifest(
    *,
    catalog_root: Path,
    manifest: BakedManifest,
) -> Path:
    """Write the manifest, creating ``catalog_root`` if needed."""
    catalog_root.mkdir(parents=True, exist_ok=True)
    out = manifest_path_for(catalog_root)
    out.write_text(manifest.to_json(), encoding="utf-8")
    return out


def check_freshness(
    *,
    docs_root: Path,
    catalog_root: Path,
) -> tuple[bool, list[str]]:
    """Hash-only freshness check — never reads the cataloging cohort.

    Returns ``(is_fresh, diagnostics)``. ``is_fresh`` is True when
    every doc on disk has a manifest entry whose source_hash matches
    the file's current hash. Diagnostic strings describe each drift.

    This is the load-bearing CI surface from principle (2): freshness
    verification spends no LLM calls. A regen that produced 851
    section-summaries cost real cheap-tier LLM dollars; CI must not
    re-spend them just to check.
    """
    diagnostics: list[str] = []
    manifest_p = manifest_path_for(catalog_root)
    if not manifest_p.exists():
        diagnostics.append(
            f"manifest absent at {manifest_p}; run "
            f"scripts/regenerate_reference_catalog.py to bootstrap"
        )
        return False, diagnostics

    try:
        manifest = BakedManifest.from_json(manifest_p.read_text(encoding="utf-8"))
    except Exception as exc:
        diagnostics.append(f"manifest unreadable: {exc}")
        return False, diagnostics

    repo_root = docs_root.parent
    on_disk: dict[str, str] = {}
    for md in sorted(docs_root.rglob("*.md")):
        if md.is_file() and not md.name.startswith("_"):
            rel = source_relpath_for(repo_root, md)
            on_disk[rel] = compute_file_hash(md)

    fresh = True
    # Files with stale or missing manifest entries
    for rel, current_hash in sorted(on_disk.items()):
        entry = manifest.entries.get(rel)
        if entry is None:
            diagnostics.append(f"new doc not yet cataloged: {rel}")
            fresh = False
            continue
        if entry["source_hash"] != current_hash:
            diagnostics.append(
                f"stale: {rel} (source has changed since regen; "
                f"manifest={entry['source_hash'][:12]} current={current_hash[:12]})"
            )
            fresh = False
            continue
        # Per-file artifact must exist and parse and agree on hash.
        artifact_p = repo_root / entry["artifact_path"]
        if not artifact_p.exists():
            diagnostics.append(
                f"manifest entry references missing artifact: {rel} → "
                f"{entry['artifact_path']}"
            )
            fresh = False
            continue
        try:
            artifact = BakedArtifact.from_json(
                artifact_p.read_text(encoding="utf-8")
            )
        except Exception as exc:
            diagnostics.append(
                f"artifact unreadable: {entry['artifact_path']} ({exc})"
            )
            fresh = False
            continue
        if artifact.source_hash != current_hash:
            diagnostics.append(
                f"artifact hash diverges from current source: {rel} "
                f"(artifact={artifact.source_hash[:12]} current={current_hash[:12]})"
            )
            fresh = False

    # Files in manifest but absent from disk (deletion forgotten)
    for rel in sorted(set(manifest.entries.keys()) - set(on_disk.keys())):
        diagnostics.append(
            f"manifest references deleted doc: {rel} (rerun regen to drop)"
        )
        fresh = False

    return fresh, diagnostics


async def wire_reference_substrate(
    *,
    handler: Any,
    data_dir: str,
    substrate_instance_id: str = "default",
) -> dict[str, Any]:
    """Build the reference-primitive substrate and bind it to a handler.

    Constructs the CatalogStore, EventEmitter, CatalogingCohort,
    IngestionScanner, and ReferenceService that REFERENCE-PRIMITIVE-V1
    composes, runs the REFERENCE-CATALOG-BAKED-V1 baked-loader against
    the canonical docs tree, and registers source roots so the live-
    scan path remains available as the env-var-gated recovery hatch.

    Sets ``handler._reference_service`` so the reasoning dispatcher's
    test-convenience seam picks up the service.
    (``_handle_reference_tool`` resolves the service from
    ``handler._wlp_substrate.reference_service`` first, then falls
    back to ``handler._reference_service`` — production sets the
    former via the Substrate bundle; lighter-weight callers like the
    REPL or scripted audits set the latter directly via this helper.)

    Returns a dict of the constructed objects so callers that ALSO
    need to compose them into a larger bundle (e.g. bring_up_substrate
    folding into the Substrate dataclass) can pick them up:

    .. code-block:: python

        wired = await wire_reference_substrate(handler=h, data_dir=d)
        wired["catalog"]            # CatalogStore
        wired["event_emitter"]      # ReferenceEventEmitter
        wired["cohort"]             # CatalogingCohort
        wired["ingestion_scanner"]  # IngestionScanner
        wired["service"]            # ReferenceService
        wired["load_summary"]       # LoadSummary from baked hydration

    The function is idempotent within a process (CatalogStore.start
    is itself idempotent against the same data dir), but should not
    be called twice with different data dirs without a stop in
    between.
    """
    import os
    from pathlib import Path
    from kernos.kernel import event_stream as _event_stream
    from kernos.kernel.reference.bringup_adapters import ReferenceCheapLLMAdapter
    from kernos.kernel.reference.catalog import (
        CatalogStore,
        SCOPE_INSTANCE,
        TRUST_CANONICAL,
    )
    from kernos.kernel.reference.cohort import CatalogingCohort
    from kernos.kernel.reference.events import (
        REFERENCE_SOURCE_MODULE,
        ReferenceEventEmitter,
    )
    from kernos.kernel.reference.ingest import (
        IngestionScanner,
        docs_source_root,
    )
    from kernos.kernel.reference.tools import ReferenceService

    catalog = CatalogStore()
    await catalog.start(data_dir)

    registry = _event_stream.emitter_registry()
    raw_emitter = registry.get(REFERENCE_SOURCE_MODULE) or registry.register(
        REFERENCE_SOURCE_MODULE,
    )
    event_emitter = ReferenceEventEmitter(emitter=raw_emitter)

    reference_llm = ReferenceCheapLLMAdapter(reasoning=handler.reasoning)
    cohort = CatalogingCohort(
        catalog=catalog,
        emitter=event_emitter,
        llm=reference_llm,
        instance_id=substrate_instance_id,
    )
    await cohort.start()

    ingestion_scanner = IngestionScanner(
        catalog=catalog,
        cohort=cohort,
        emitter=event_emitter,
        instance_id=substrate_instance_id,
    )

    references_root = Path(data_dir) / "references"
    references_root.mkdir(parents=True, exist_ok=True)

    service = ReferenceService(
        catalog=catalog,
        cohort=cohort,
        emitter=event_emitter,
        navigator_llm=reference_llm,
        references_root=references_root,
        instance_id=substrate_instance_id,
    )

    # Baked-catalog hydration. Always runs; the loader is inert when
    # the manifest is absent (returns LoadSummary with manifest_present
    # = False and no other work).
    docs_root = Path(__file__).resolve().parent.parent.parent.parent / "docs"
    load_summary = LoadSummary()
    if docs_root.exists() and docs_root.is_dir():
        ingestion_scanner.add_source(docs_source_root(docs_root))
        catalog_root = docs_root / CATALOG_DIRNAME
        load_summary = await load_baked_catalog(
            docs_root=docs_root,
            catalog_root=catalog_root,
            instance_id=substrate_instance_id,
            catalog_store=catalog,
            scope=SCOPE_INSTANCE,
            trust_tier=TRUST_CANONICAL,
            owner_domain_id="",
        )
        if not load_summary.manifest_present:
            logger.info(
                "REFERENCE_BAKED_HYDRATION: no manifest at %s — catalog "
                "starts empty. Run the regen script to seed, or set "
                "KERNOS_REFERENCE_FIRST_BOOT_SCAN=1 to live-scan once.",
                catalog_root,
            )
        else:
            level = (
                logger.warning
                if (
                    load_summary.files_stale
                    or load_summary.files_missing_artifact
                    or load_summary.files_artifact_invalid
                    or load_summary.files_uncatalogued
                )
                else logger.info
            )
            level(
                "REFERENCE_BAKED_HYDRATION: loaded=%d sections=%d "
                "stale=%d missing_artifact=%d artifact_invalid=%d "
                "uncatalogued=%d",
                load_summary.files_loaded,
                load_summary.sections_imported,
                load_summary.files_stale,
                load_summary.files_missing_artifact,
                load_summary.files_artifact_invalid,
                load_summary.files_uncatalogued,
            )

        if os.environ.get("KERNOS_REFERENCE_FIRST_BOOT_SCAN", "0") == "1":
            import asyncio
            asyncio.create_task(
                ingestion_scanner.scan(),
                name="reference_first_boot_scan",
            )
            logger.info(
                "REFERENCE_FIRST_BOOT_SCAN: launched (env opt-in)",
            )

    # Bind the service to the handler via the test-convenience seam.
    # Production also stashes it under ``_wlp_substrate.reference_service``;
    # the dispatcher checks both. This binding is sufficient for any
    # caller that hasn't built the full Substrate bundle.
    handler._reference_service = service

    return {
        "catalog": catalog,
        "event_emitter": event_emitter,
        "cohort": cohort,
        "ingestion_scanner": ingestion_scanner,
        "service": service,
        "load_summary": load_summary,
    }


__all__ = [
    "BakedSection",
    "BakedArtifact",
    "BakedManifest",
    "LoadSummary",
    "MANIFEST_VERSION",
    "MANIFEST_FILENAME",
    "CATALOG_DIRNAME",
    "manifest_path_for",
    "artifact_path_for",
    "source_relpath_for",
    "load_baked_catalog",
    "write_baked_artifact",
    "write_manifest",
    "check_freshness",
    "wire_reference_substrate",
]
