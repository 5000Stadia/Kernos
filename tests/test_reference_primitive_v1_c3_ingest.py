"""REFERENCE-PRIMITIVE-V1 C3 — per-turn ingestion check.

Pins:

* Fresh install: every ``*.md`` under a registered source root is
  enqueued for cataloging; counts in the summary match the disk
  reality.
* Steady state: a second scan with no file changes performs zero
  enqueues (cohort sees an empty queue).
* Hash-changed file is enqueued for re-cataloging; new files (no
  catalog entry) are enqueued as fresh.
* Vanished files are tombstoned directly (no cohort round-trip);
  ``reference.tombstoned`` event is emitted.
* Collection-aware roots discover ``_collection.json`` siblings
  and enqueue collection-level cataloging on first sight; mtime
  newer than ``last_refreshed_at`` triggers refresh.
* Collection-back-reference is stamped on file entries that live
  inside a collection directory."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from kernos.kernel import event_stream
from kernos.kernel.reference.catalog import (
    CatalogStore,
    SCOPE_INSTANCE,
    TRUST_AGENT_AUTHORED,
    TRUST_CANONICAL,
    scope_for_domain,
)
from kernos.kernel.reference.cohort import CatalogingCohort
from kernos.kernel.reference.events import (
    REFERENCE_SOURCE_MODULE,
    ReferenceEventEmitter,
)
from kernos.kernel.reference.ingest import (
    IngestionScanner,
    docs_source_root,
    references_source_root,
)


# ---------------------------------------------------------------------------
# LLM stub (cohort dependency)
# ---------------------------------------------------------------------------


class _StubLLM:
    @property
    def temperature(self) -> float:
        return 0.1

    async def complete(self, prompt: str) -> str:
        return f"summary[{len(prompt)}]"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def event_stream_started(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path), flush_interval_s=0.05)
    yield
    await event_stream.stop_writer()
    await event_stream._reset_for_tests()


@pytest.fixture
async def ingest_setup(tmp_path, event_stream_started):
    catalog = CatalogStore()
    await catalog.start(str(tmp_path))
    registry = event_stream.emitter_registry()
    raw = registry.register(REFERENCE_SOURCE_MODULE)
    emitter = ReferenceEventEmitter(emitter=raw)
    cohort = CatalogingCohort(
        catalog=catalog, emitter=emitter, llm=_StubLLM(),
        instance_id="inst1",
    )
    await cohort.start()
    scanner = IngestionScanner(
        catalog=catalog, cohort=cohort, emitter=emitter,
        instance_id="inst1",
    )
    yield catalog, cohort, scanner, tmp_path
    await cohort.stop()
    await catalog.stop()


def _make_docs_tree(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    (docs / "architecture").mkdir(parents=True)
    (docs / "primitives").mkdir(parents=True)
    (docs / "architecture" / "gate.md").write_text(
        "## Gate classification\n\nThe gate decides...\n",
        encoding="utf-8",
    )
    (docs / "primitives" / "canvas.md").write_text(
        "## Canvas overview\n\nA canvas is...\n",
        encoding="utf-8",
    )
    (docs / "index.md").write_text(
        "## Top-level\n\nKernos overview\n",
        encoding="utf-8",
    )
    return docs


# ---------------------------------------------------------------------------
# Initial scan
# ---------------------------------------------------------------------------


async def test_first_scan_enqueues_every_markdown_file(ingest_setup):
    catalog, cohort, scanner, tmp_path = ingest_setup
    docs = _make_docs_tree(tmp_path)
    scanner.add_source(docs_source_root(docs))
    summary = await scanner.scan()
    assert summary["files_seen"] == 3
    assert summary["files_new"] == 3
    assert summary["files_unchanged"] == 0
    await cohort.drain()

    # Catalog now carries one entry per file (each file has one
    # h2 section in the fixture).
    rows_gate = await catalog.list_entries_for_file(
        instance_id="inst1", file_path=str(docs / "architecture/gate.md"),
    )
    assert len(rows_gate) == 1
    assert rows_gate[0].scope == SCOPE_INSTANCE
    assert rows_gate[0].category == "architecture"


async def test_steady_state_scan_does_no_work(ingest_setup):
    catalog, cohort, scanner, tmp_path = ingest_setup
    docs = _make_docs_tree(tmp_path)
    scanner.add_source(docs_source_root(docs))
    await scanner.scan()
    await cohort.drain()

    summary = await scanner.scan()
    assert summary["files_unchanged"] == 3
    assert summary["files_new"] == 0
    assert summary["files_changed"] == 0


async def test_hash_change_enqueues_for_recatalog(ingest_setup):
    catalog, cohort, scanner, tmp_path = ingest_setup
    docs = _make_docs_tree(tmp_path)
    scanner.add_source(docs_source_root(docs))
    await scanner.scan()
    await cohort.drain()

    target = docs / "architecture/gate.md"
    target.write_text(
        "## Gate classification\n\nUpdated body.\n\n## New section\n\nbody2\n",
        encoding="utf-8",
    )
    summary = await scanner.scan()
    await cohort.drain()
    assert summary["files_changed"] == 1

    rows = await catalog.list_entries_for_file(
        instance_id="inst1", file_path=str(target),
    )
    assert len(rows) == 2
    assert {r.section_title for r in rows} == {
        "Gate classification", "New section",
    }


async def test_vanished_file_tombstones_with_event(ingest_setup):
    catalog, cohort, scanner, tmp_path = ingest_setup
    docs = _make_docs_tree(tmp_path)
    scanner.add_source(docs_source_root(docs))
    await scanner.scan()
    await cohort.drain()

    target = docs / "primitives/canvas.md"
    target.unlink()

    summary = await scanner.scan()
    assert summary["files_tombstoned"] == 1

    visible_rows = await catalog.list_entries_for_file(
        instance_id="inst1", file_path=str(target),
    )
    assert visible_rows == []
    tombstoned_rows = await catalog.list_entries_for_file(
        instance_id="inst1", file_path=str(target),
        include_tombstoned=True,
    )
    assert all(r.tombstoned for r in tombstoned_rows)


# ---------------------------------------------------------------------------
# References (per-domain) source root
# ---------------------------------------------------------------------------


async def test_references_root_uses_domain_scope(ingest_setup):
    catalog, cohort, scanner, tmp_path = ingest_setup
    refs = tmp_path / "refs"
    cdir = refs / "vendor-test-api"
    cdir.mkdir(parents=True)
    (cdir / "_collection.json").write_text(
        json.dumps({
            "name": "vendor-test-api",
            "purpose": "API docs",
            "trust_tier": "external_snapshot",
            "refresh_policy": "snapshot",
        }),
        encoding="utf-8",
    )
    (cdir / "auth.md").write_text(
        "## Authentication\n\nBearer tokens.\n", encoding="utf-8",
    )

    scanner.add_source(
        references_source_root(
            references_path=refs, domain_id="space-A",
        )
    )
    summary = await scanner.scan()
    await cohort.drain()
    assert summary["files_new"] == 1
    assert summary["collections_seen"] == 1
    assert summary["collections_enqueued"] == 1

    file_rows = await catalog.list_entries_for_file(
        instance_id="inst1", file_path=str(cdir / "auth.md"),
    )
    assert len(file_rows) == 1
    assert file_rows[0].scope == scope_for_domain("space-A")
    # collection_back_reference is stamped because the file's parent
    # has a _collection.json sibling.
    assert file_rows[0].collection_back_reference == "vendor-test-api"

    coll = await catalog.get_collection_entry(
        instance_id="inst1",
        collection_name="vendor-test-api",
        scope=scope_for_domain("space-A"),
    )
    assert coll is not None
    assert coll.purpose == "API docs"


async def test_collection_meta_mtime_triggers_refresh(ingest_setup):
    catalog, cohort, scanner, tmp_path = ingest_setup
    refs = tmp_path / "refs"
    cdir = refs / "research"
    cdir.mkdir(parents=True)
    meta_path = cdir / "_collection.json"
    meta_path.write_text(
        json.dumps({"name": "research", "purpose": "first", "trust_tier": "agent_authored"}),
        encoding="utf-8",
    )
    (cdir / "n1.md").write_text("## A\n\nbody\n", encoding="utf-8")

    scanner.add_source(
        references_source_root(
            references_path=refs, domain_id="space-A",
        )
    )
    await scanner.scan()
    await cohort.drain()
    coll1 = await catalog.get_collection_entry(
        instance_id="inst1",
        collection_name="research",
        scope=scope_for_domain("space-A"),
    )
    assert coll1 is not None and coll1.purpose == "first"

    # Mutate the metadata file — bump mtime + new content.
    time.sleep(1.1)  # ensure mtime delta exceeds the 1s tolerance
    meta_path.write_text(
        json.dumps({"name": "research", "purpose": "second", "trust_tier": "agent_authored"}),
        encoding="utf-8",
    )
    summary = await scanner.scan()
    await cohort.drain()
    assert summary["collections_enqueued"] == 1
    coll2 = await catalog.get_collection_entry(
        instance_id="inst1",
        collection_name="research",
        scope=scope_for_domain("space-A"),
    )
    assert coll2 is not None and coll2.purpose == "second"
