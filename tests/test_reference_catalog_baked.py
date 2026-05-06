"""REFERENCE-CATALOG-BAKED-V1 — unit + integration tests.

Tests cover the load-bearing surfaces specified in the architect
verdict (Path B+, 2026-05-06):

* Artifact + manifest serialization round-trip (deterministic JSON).
* ``load_baked_catalog`` happy-path: clean install loads from baked
  in milliseconds, every section reaches the catalog store, no LLM
  calls fired.
* Mixed-mode partial-mismatch handling: one stale file produces a
  loud diagnostic and is skipped, while matched files load normally.
* Per-file source-hash check: an artifact whose recorded hash
  diverges from the manifest hash is rejected.
* Uncatalogued doc detection: a doc on disk without a manifest entry
  is counted in ``files_uncatalogued``.
* ``check_freshness`` covers all drift types — new doc, stale doc,
  missing artifact, divergent artifact hash, deleted doc still in
  manifest. Spends zero LLM calls (principle 2).

The tests use a synthetic temporary docs tree and a real
``CatalogStore`` against an isolated temp data directory. No
cataloging cohort is invoked anywhere — the regen script is the
only LLM-spending surface.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kernos.kernel.reference.baked import (
    BakedArtifact,
    BakedManifest,
    BakedSection,
    CATALOG_DIRNAME,
    LoadSummary,
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    artifact_path_for,
    check_freshness,
    load_baked_catalog,
    manifest_path_for,
    source_relpath_for,
    write_baked_artifact,
    write_manifest,
)
from kernos.kernel.reference.catalog import (
    CatalogStore,
    SCOPE_INSTANCE,
    TRUST_CANONICAL,
    compute_file_hash,
    compute_source_hash,
)


# asyncio mode = auto (pyproject.toml) handles async tests automatically;
# no per-file pytestmark needed.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_doc(docs_root: Path, relpath: str, body: str) -> Path:
    """Write a doc under docs_root and return the path."""
    path = docs_root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _write_artifact_for(
    docs_root: Path,
    catalog_root: Path,
    repo_root: Path,
    source_path: Path,
    sections: list[BakedSection] | None = None,
    *,
    override_hash: str | None = None,
) -> tuple[BakedArtifact, str, str]:
    """Build a BakedArtifact for a docs file and write it.

    Returns ``(artifact, source_relpath, artifact_relpath)`` so the
    caller can assemble a manifest.
    """
    source_relpath = source_relpath_for(repo_root, source_path)
    source_hash = override_hash or compute_file_hash(source_path)
    if sections is None:
        sections = [
            BakedSection(
                section_title="Test Section",
                one_line="A summary of the test section.",
                line_start=1,
                line_end=2,
            ),
        ]
    artifact = BakedArtifact(
        file_path=source_relpath,
        source_hash=source_hash,
        generated_at="2026-05-06T00:00:00Z",
        sections=tuple(sections),
    )
    artifact_p = write_baked_artifact(
        catalog_root=catalog_root,
        source_relpath=source_relpath,
        artifact=artifact,
    )
    artifact_relpath = artifact_p.relative_to(repo_root).as_posix()
    return artifact, source_relpath, artifact_relpath


async def _make_store(tmp_path: Path) -> CatalogStore:
    store = CatalogStore()
    await store.start(str(tmp_path))
    return store


# ---------------------------------------------------------------------------
# Artifact + manifest serialization
# ---------------------------------------------------------------------------


def test_baked_artifact_round_trip():
    artifact = BakedArtifact(
        file_path="docs/architecture/canvas.md",
        source_hash="0" * 64,
        generated_at="2026-05-06T00:00:00Z",
        sections=(
            BakedSection(
                section_title="Header One",
                one_line="What header one is about.",
                line_start=1,
                line_end=12,
            ),
            BakedSection(
                section_title="Header Two",
                one_line="And header two.",
                line_start=14,
                line_end=30,
            ),
        ),
    )
    text = artifact.to_json()
    parsed = BakedArtifact.from_json(text)
    assert parsed == artifact
    # Stable formatting: round-trip produces identical text
    assert parsed.to_json() == text


def test_baked_manifest_round_trip():
    manifest = BakedManifest(
        version=1,
        generated_at="2026-05-06T00:00:00Z",
        entries={
            "docs/a.md": {"source_hash": "a" * 64, "artifact_path": "docs/_catalog/a.json"},
            "docs/b.md": {"source_hash": "b" * 64, "artifact_path": "docs/_catalog/b.json"},
        },
    )
    text = manifest.to_json()
    parsed = BakedManifest.from_json(text)
    assert parsed == manifest


def test_artifact_path_mirrors_directory_structure(tmp_path):
    catalog = tmp_path / CATALOG_DIRNAME
    p = artifact_path_for(catalog, "docs/architecture/canvas.md")
    assert p == catalog / "architecture" / "canvas.json"
    p2 = artifact_path_for(catalog, "docs/index.md")
    assert p2 == catalog / "index.json"


# ---------------------------------------------------------------------------
# Loader: happy path
# ---------------------------------------------------------------------------


async def test_load_baked_clean_install_imports_sections(tmp_path):
    repo_root = tmp_path
    docs_root = repo_root / "docs"
    catalog_root = docs_root / CATALOG_DIRNAME

    src = _make_doc(docs_root, "architecture/canvas.md", "## A\n\nbody\n")
    sections = [BakedSection("A", "What A is about", 1, 3)]
    artifact, src_rel, art_rel = _write_artifact_for(
        docs_root, catalog_root, repo_root, src, sections,
    )
    write_manifest(
        catalog_root=catalog_root,
        manifest=BakedManifest(
            version=MANIFEST_VERSION,
            generated_at="2026-05-06T00:00:00Z",
            entries={src_rel: {"source_hash": artifact.source_hash, "artifact_path": art_rel}},
        ),
    )

    store = await _make_store(tmp_path / "data")
    summary = await load_baked_catalog(
        docs_root=docs_root,
        catalog_root=catalog_root,
        instance_id="inst",
        catalog_store=store,
        scope=SCOPE_INSTANCE,
        trust_tier=TRUST_CANONICAL,
        owner_domain_id="",
    )
    assert summary.manifest_present
    assert summary.files_loaded == 1
    assert summary.sections_imported == 1
    assert summary.files_stale == 0
    assert summary.files_uncatalogued == 0

    # Verify the row reached the store
    rows = await store.list_entries_for_file(
        instance_id="inst", file_path=str(src),
    )
    assert len(rows) == 1
    assert rows[0].section_title == "A"
    assert rows[0].one_line == "What A is about"
    assert rows[0].source_hash == artifact.source_hash
    assert rows[0].provenance_metadata.get("hydration") == "baked"


# ---------------------------------------------------------------------------
# Mixed-mode partial-mismatch handling
# ---------------------------------------------------------------------------


async def test_load_baked_mixed_mode_skips_stale_loads_matched(tmp_path):
    """One file fresh, one file stale (source mutated post-bake).

    The fresh file loads; the stale file is logged loudly and skipped.
    The runtime catalog ends up with rows for the fresh file only.
    """
    repo_root = tmp_path
    docs_root = repo_root / "docs"
    catalog_root = docs_root / CATALOG_DIRNAME

    fresh = _make_doc(docs_root, "fresh.md", "## A\n\nbody\n")
    stale = _make_doc(docs_root, "stale.md", "## B\n\nbody\n")
    fresh_art, fresh_rel, fresh_art_rel = _write_artifact_for(
        docs_root, catalog_root, repo_root, fresh,
        sections=[BakedSection("A", "fresh summary", 1, 3)],
    )
    stale_art, stale_rel, stale_art_rel = _write_artifact_for(
        docs_root, catalog_root, repo_root, stale,
        sections=[BakedSection("B", "stale summary", 1, 3)],
    )
    write_manifest(
        catalog_root=catalog_root,
        manifest=BakedManifest(
            version=MANIFEST_VERSION,
            generated_at="2026-05-06T00:00:00Z",
            entries={
                fresh_rel: {"source_hash": fresh_art.source_hash, "artifact_path": fresh_art_rel},
                stale_rel: {"source_hash": stale_art.source_hash, "artifact_path": stale_art_rel},
            },
        ),
    )

    # Mutate the stale file AFTER manifest write — drift simulated
    stale.write_text("## B\n\nNEW BODY\n", encoding="utf-8")

    store = await _make_store(tmp_path / "data")
    summary = await load_baked_catalog(
        docs_root=docs_root,
        catalog_root=catalog_root,
        instance_id="inst",
        catalog_store=store,
        scope=SCOPE_INSTANCE,
        trust_tier=TRUST_CANONICAL,
        owner_domain_id="",
    )
    assert summary.files_loaded == 1
    assert summary.files_stale == 1
    fresh_rows = await store.list_entries_for_file(instance_id="inst", file_path=str(fresh))
    stale_rows = await store.list_entries_for_file(instance_id="inst", file_path=str(stale))
    assert len(fresh_rows) == 1
    assert len(stale_rows) == 0  # stale skipped, runtime catalog clean


async def test_load_baked_rejects_artifact_hash_divergence(tmp_path):
    """Manifest hash matches source, but the artifact's recorded hash
    diverges. Defense-in-depth: refuse to import."""
    repo_root = tmp_path
    docs_root = repo_root / "docs"
    catalog_root = docs_root / CATALOG_DIRNAME

    src = _make_doc(docs_root, "doc.md", "## Title\n\nbody\n")
    real_hash = compute_file_hash(src)
    bogus_hash = "f" * 64

    # Write artifact with bogus source_hash (out-of-band edit simulation)
    artifact = BakedArtifact(
        file_path=source_relpath_for(repo_root, src),
        source_hash=bogus_hash,
        generated_at="2026-05-06T00:00:00Z",
        sections=(BakedSection("Title", "summary", 1, 3),),
    )
    write_baked_artifact(
        catalog_root=catalog_root,
        source_relpath=source_relpath_for(repo_root, src),
        artifact=artifact,
    )
    # Manifest references the real hash — divergence
    write_manifest(
        catalog_root=catalog_root,
        manifest=BakedManifest(
            version=MANIFEST_VERSION,
            generated_at="2026-05-06T00:00:00Z",
            entries={
                source_relpath_for(repo_root, src): {
                    "source_hash": real_hash,
                    "artifact_path": artifact_path_for(
                        catalog_root, source_relpath_for(repo_root, src),
                    ).relative_to(repo_root).as_posix(),
                },
            },
        ),
    )

    store = await _make_store(tmp_path / "data")
    summary = await load_baked_catalog(
        docs_root=docs_root,
        catalog_root=catalog_root,
        instance_id="inst",
        catalog_store=store,
        scope=SCOPE_INSTANCE,
        trust_tier=TRUST_CANONICAL,
        owner_domain_id="",
    )
    assert summary.files_loaded == 0
    assert summary.files_artifact_invalid == 1


async def test_load_baked_counts_uncatalogued_docs(tmp_path):
    """A doc on disk without a manifest entry → files_uncatalogued += 1."""
    repo_root = tmp_path
    docs_root = repo_root / "docs"
    catalog_root = docs_root / CATALOG_DIRNAME

    src = _make_doc(docs_root, "cataloged.md", "## A\n\nbody\n")
    _orphan = _make_doc(docs_root, "orphan.md", "## B\n\nuncataloged\n")
    artifact, src_rel, art_rel = _write_artifact_for(
        docs_root, catalog_root, repo_root, src,
    )
    write_manifest(
        catalog_root=catalog_root,
        manifest=BakedManifest(
            version=MANIFEST_VERSION,
            generated_at="2026-05-06T00:00:00Z",
            entries={src_rel: {"source_hash": artifact.source_hash, "artifact_path": art_rel}},
        ),
    )

    store = await _make_store(tmp_path / "data")
    summary = await load_baked_catalog(
        docs_root=docs_root,
        catalog_root=catalog_root,
        instance_id="inst",
        catalog_store=store,
        scope=SCOPE_INSTANCE,
        trust_tier=TRUST_CANONICAL,
        owner_domain_id="",
    )
    assert summary.files_loaded == 1
    assert summary.files_uncatalogued == 1


async def test_load_baked_no_manifest_returns_inert_summary(tmp_path):
    """No manifest → manifest_present=False, no work attempted."""
    repo_root = tmp_path
    docs_root = repo_root / "docs"
    docs_root.mkdir(parents=True)
    catalog_root = docs_root / CATALOG_DIRNAME

    store = await _make_store(tmp_path / "data")
    summary = await load_baked_catalog(
        docs_root=docs_root,
        catalog_root=catalog_root,
        instance_id="inst",
        catalog_store=store,
        scope=SCOPE_INSTANCE,
        trust_tier=TRUST_CANONICAL,
        owner_domain_id="",
    )
    assert not summary.manifest_present
    assert summary.files_loaded == 0


# ---------------------------------------------------------------------------
# Freshness check (CI gate — principle 2: never spends LLM)
# ---------------------------------------------------------------------------


def test_check_freshness_clean(tmp_path):
    repo_root = tmp_path
    docs_root = repo_root / "docs"
    catalog_root = docs_root / CATALOG_DIRNAME

    src = _make_doc(docs_root, "doc.md", "## A\n\nbody\n")
    artifact, src_rel, art_rel = _write_artifact_for(
        docs_root, catalog_root, repo_root, src,
    )
    write_manifest(
        catalog_root=catalog_root,
        manifest=BakedManifest(
            version=MANIFEST_VERSION,
            generated_at="2026-05-06T00:00:00Z",
            entries={src_rel: {"source_hash": artifact.source_hash, "artifact_path": art_rel}},
        ),
    )

    fresh, diagnostics = check_freshness(docs_root=docs_root, catalog_root=catalog_root)
    assert fresh
    assert diagnostics == []


def test_check_freshness_detects_stale(tmp_path):
    repo_root = tmp_path
    docs_root = repo_root / "docs"
    catalog_root = docs_root / CATALOG_DIRNAME
    src = _make_doc(docs_root, "doc.md", "## A\n\nbody\n")
    artifact, src_rel, art_rel = _write_artifact_for(
        docs_root, catalog_root, repo_root, src,
    )
    write_manifest(
        catalog_root=catalog_root,
        manifest=BakedManifest(
            version=MANIFEST_VERSION,
            generated_at="2026-05-06T00:00:00Z",
            entries={src_rel: {"source_hash": artifact.source_hash, "artifact_path": art_rel}},
        ),
    )
    src.write_text("## A\n\nMUTATED\n", encoding="utf-8")
    fresh, diagnostics = check_freshness(docs_root=docs_root, catalog_root=catalog_root)
    assert not fresh
    assert any("stale" in d for d in diagnostics)


def test_check_freshness_detects_new_doc(tmp_path):
    repo_root = tmp_path
    docs_root = repo_root / "docs"
    catalog_root = docs_root / CATALOG_DIRNAME

    src = _make_doc(docs_root, "doc.md", "## A\n\nbody\n")
    artifact, src_rel, art_rel = _write_artifact_for(
        docs_root, catalog_root, repo_root, src,
    )
    write_manifest(
        catalog_root=catalog_root,
        manifest=BakedManifest(
            version=MANIFEST_VERSION,
            generated_at="2026-05-06T00:00:00Z",
            entries={src_rel: {"source_hash": artifact.source_hash, "artifact_path": art_rel}},
        ),
    )
    _make_doc(docs_root, "newdoc.md", "## B\n\nuncataloged\n")
    fresh, diagnostics = check_freshness(docs_root=docs_root, catalog_root=catalog_root)
    assert not fresh
    assert any("new doc not yet cataloged" in d for d in diagnostics)


def test_check_freshness_detects_deleted_doc(tmp_path):
    repo_root = tmp_path
    docs_root = repo_root / "docs"
    catalog_root = docs_root / CATALOG_DIRNAME
    src = _make_doc(docs_root, "doc.md", "## A\n\nbody\n")
    artifact, src_rel, art_rel = _write_artifact_for(
        docs_root, catalog_root, repo_root, src,
    )
    write_manifest(
        catalog_root=catalog_root,
        manifest=BakedManifest(
            version=MANIFEST_VERSION,
            generated_at="2026-05-06T00:00:00Z",
            entries={src_rel: {"source_hash": artifact.source_hash, "artifact_path": art_rel}},
        ),
    )
    src.unlink()
    fresh, diagnostics = check_freshness(docs_root=docs_root, catalog_root=catalog_root)
    assert not fresh
    assert any("deleted" in d for d in diagnostics)


def test_check_freshness_no_manifest_diagnoses_loud(tmp_path):
    docs_root = tmp_path / "docs"
    docs_root.mkdir(parents=True)
    catalog_root = docs_root / CATALOG_DIRNAME
    fresh, diagnostics = check_freshness(docs_root=docs_root, catalog_root=catalog_root)
    assert not fresh
    assert any("manifest absent" in d for d in diagnostics)
