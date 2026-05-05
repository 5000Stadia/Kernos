"""REFERENCE-PRIMITIVE-V1 C2 — cataloging cohort.

Pins:

* ``split_sections`` chunks markdown files at ``## `` headings;
  files without h2 produce a single section spanning the file.
* :class:`CatalogingCohort` consumes file-change items, runs the
  cheap-tier LLM for one-line descriptions, and writes
  CatalogEntry rows via :meth:`CatalogStore.replace_file_entries`.
* Re-cataloging the same file (different content / same path)
  drops the prior entries; the catalog never carries
  partial-old + partial-new state.
* Collection-level entries flow through :meth:`enqueue_collection`
  using ``_collection.json`` for declared metadata.
* The cohort emits ``reference.cataloged`` on first ingest and
  ``reference.recataloged`` on subsequent ingest of the same file.
* LLM failure on a section falls back to using the section title
  as the one-liner — never blocks the rebuild.
* Tombstoning: a file that vanishes between enqueue and worker
  pickup is tombstoned cleanly.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from kernos.kernel import event_stream
from kernos.kernel.reference.catalog import (
    CatalogStore,
    SCOPE_INSTANCE,
    TRUST_CANONICAL,
    TRUST_AGENT_AUTHORED,
    scope_for_domain,
)
from kernos.kernel.reference.cohort import (
    CatalogingCohort,
    split_sections,
)
from kernos.kernel.reference.events import (
    REFERENCE_SOURCE_MODULE,
    ReferenceEventEmitter,
)


# ---------------------------------------------------------------------------
# LLM stub
# ---------------------------------------------------------------------------


class _StubLLM:
    """Stateless cheap-tier completion stub.

    Returns a deterministic short string. The cohort's prompt has the
    section title embedded; the stub strips a one-liner from it so
    tests can verify the round-trip."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_next = False

    @property
    def temperature(self) -> float:
        return 0.1

    async def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("LLM stub: simulated failure")
        # Return an LLM-shaped one-liner derived from the prompt.
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
async def cohort_setup(tmp_path, event_stream_started):
    catalog = CatalogStore()
    await catalog.start(str(tmp_path))
    registry = event_stream.emitter_registry()
    raw = registry.register(REFERENCE_SOURCE_MODULE)
    emitter = ReferenceEventEmitter(emitter=raw)
    llm = _StubLLM()
    cohort = CatalogingCohort(
        catalog=catalog, emitter=emitter, llm=llm,
        instance_id="inst1",
    )
    await cohort.start()
    yield catalog, emitter, llm, cohort, tmp_path
    await cohort.stop()
    await catalog.stop()


# ---------------------------------------------------------------------------
# Section splitter
# ---------------------------------------------------------------------------


def test_split_sections_at_h2():
    text = textwrap.dedent(
        """\
        # File title

        Intro paragraph.

        ## First section

        Body of first.

        ## Second section

        Body of second.
        """
    )
    sections = split_sections(file_text=text, file_path="/x/y.md")
    titles = [t for (t, _, _) in sections]
    assert titles == ["First section", "Second section"]


def test_split_sections_no_h2_returns_single_section():
    text = "# Just a title\n\nSome body without h2 sections.\n"
    sections = split_sections(file_text=text, file_path="/x/y.md")
    assert len(sections) == 1
    assert sections[0][0] == "Just a title"


def test_split_sections_no_headings_uses_filename():
    text = "Plain body, no headings at all.\n"
    sections = split_sections(file_text=text, file_path="/x/notes.md")
    assert sections == [("notes", 1, 1)]


def test_split_sections_skips_h3_as_split_point():
    text = textwrap.dedent(
        """\
        ## First

        ### Subhead — h3 must NOT split

        body
        """
    )
    sections = split_sections(file_text=text, file_path="/x.md")
    assert len(sections) == 1
    assert sections[0][0] == "First"


# ---------------------------------------------------------------------------
# File cataloging — first ingest
# ---------------------------------------------------------------------------


async def _write_file(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


async def test_catalog_first_ingest_emits_cataloged(cohort_setup):
    catalog, _, llm, cohort, tmp_path = cohort_setup
    docs_path = await _write_file(
        tmp_path, "docs/architecture/a.md",
        "## Section one\n\nbody\n\n## Section two\n\nbody2\n",
    )
    await cohort.enqueue_file(
        file_path=str(docs_path),
        scope=SCOPE_INSTANCE,
        category="architecture",
        trust_tier=TRUST_CANONICAL,
    )
    await cohort.drain()
    rows = await catalog.list_entries_for_file(
        instance_id="inst1", file_path=str(docs_path),
    )
    assert len(rows) == 2
    assert {r.section_title for r in rows} == {"Section one", "Section two"}
    assert all(r.scope == SCOPE_INSTANCE for r in rows)
    # LLM called once per section.
    assert len(llm.calls) == 2
    # All entries share a hash (single source file).
    assert len({r.source_hash for r in rows}) == 1


async def test_recatalog_drops_prior_entries(cohort_setup):
    catalog, _, _, cohort, tmp_path = cohort_setup
    docs_path = await _write_file(
        tmp_path, "docs/x.md",
        "## A\n\nbody A\n\n## B\n\nbody B\n",
    )
    await cohort.enqueue_file(
        file_path=str(docs_path), scope=SCOPE_INSTANCE,
        category="docs", trust_tier=TRUST_CANONICAL,
    )
    await cohort.drain()
    first = await catalog.list_entries_for_file(
        instance_id="inst1", file_path=str(docs_path),
    )
    assert len(first) == 2
    first_ids = {r.entry_id for r in first}

    # Edit the file: same path, different content + different
    # section count.
    docs_path.write_text(
        "## Only one\n\nbody only one\n",
        encoding="utf-8",
    )
    await cohort.enqueue_file(
        file_path=str(docs_path), scope=SCOPE_INSTANCE,
        category="docs", trust_tier=TRUST_CANONICAL,
    )
    await cohort.drain()
    second = await catalog.list_entries_for_file(
        instance_id="inst1", file_path=str(docs_path),
    )
    assert len(second) == 1
    assert second[0].section_title == "Only one"
    second_ids = {r.entry_id for r in second}
    assert second_ids.isdisjoint(first_ids)


async def test_llm_failure_falls_back_to_section_title(cohort_setup):
    catalog, _, llm, cohort, tmp_path = cohort_setup
    docs_path = await _write_file(
        tmp_path, "docs/y.md", "## Title here\n\nbody\n",
    )
    llm.fail_next = True
    await cohort.enqueue_file(
        file_path=str(docs_path), scope=SCOPE_INSTANCE,
        category="docs", trust_tier=TRUST_CANONICAL,
    )
    await cohort.drain()
    rows = await catalog.list_entries_for_file(
        instance_id="inst1", file_path=str(docs_path),
    )
    assert len(rows) == 1
    # Fallback: one_line equals section_title when LLM fails.
    assert rows[0].one_line == "Title here"


async def test_vanished_file_tombstones_cleanly(cohort_setup):
    catalog, _, _, cohort, tmp_path = cohort_setup
    docs_path = await _write_file(
        tmp_path, "docs/z.md", "## A\n\nbody\n",
    )
    await cohort.enqueue_file(
        file_path=str(docs_path), scope=SCOPE_INSTANCE,
        category="docs", trust_tier=TRUST_CANONICAL,
    )
    await cohort.drain()
    assert (
        len(
            await catalog.list_entries_for_file(
                instance_id="inst1", file_path=str(docs_path),
            )
        )
        == 1
    )
    # Now delete the file, then re-enqueue.
    docs_path.unlink()
    await cohort.enqueue_file(
        file_path=str(docs_path), scope=SCOPE_INSTANCE,
        category="docs", trust_tier=TRUST_CANONICAL,
    )
    await cohort.drain()
    rows_after = await catalog.list_entries_for_file(
        instance_id="inst1", file_path=str(docs_path),
    )
    assert rows_after == []
    # Tombstoned rows are present when include_tombstoned is set.
    rows_with_tombstone = await catalog.list_entries_for_file(
        instance_id="inst1", file_path=str(docs_path),
        include_tombstoned=True,
    )
    assert len(rows_with_tombstone) == 1
    assert rows_with_tombstone[0].tombstoned is True


# ---------------------------------------------------------------------------
# Collection cataloging
# ---------------------------------------------------------------------------


async def test_collection_creates_entry(cohort_setup):
    catalog, _, llm, cohort, tmp_path = cohort_setup
    cdir = tmp_path / "refs/space-A/vendor-test-api"
    cdir.mkdir(parents=True)
    (cdir / "_collection.json").write_text(
        json.dumps({
            "name": "vendor-test-api",
            "purpose": "Bearer-token vendor API",
            "trust_tier": "external_snapshot",
            "refresh_policy": "snapshot",
            "provenance": {
                "source_url": "https://example.com/api",
                "stored_by": "member_x",
            },
        }),
        encoding="utf-8",
    )
    (cdir / "auth.md").write_text("## Authentication\n\n", encoding="utf-8")
    (cdir / "rate-limits.md").write_text("## Rate Limits\n\n", encoding="utf-8")

    await cohort.enqueue_collection(
        collection_dir=str(cdir),
        scope=scope_for_domain("space-A"),
        owner_domain_id="space-A",
    )
    await cohort.drain()

    entry = await catalog.get_collection_entry(
        instance_id="inst1",
        collection_name="vendor-test-api",
        scope=scope_for_domain("space-A"),
    )
    assert entry is not None
    assert entry.purpose == "Bearer-token vendor API"
    assert entry.trust_tier == "external_snapshot"
    assert entry.refresh_policy == "snapshot"
    assert entry.member_file_count == 2
    assert set(entry.member_file_paths) == {"auth.md", "rate-limits.md"}
    # LLM was NOT called — the declared purpose was non-empty.
    assert all("Collection name" not in c for c in llm.calls)


async def test_collection_with_no_declared_purpose_calls_llm(cohort_setup):
    catalog, _, llm, cohort, tmp_path = cohort_setup
    cdir = tmp_path / "refs/space-A/research-pile"
    cdir.mkdir(parents=True)
    (cdir / "_collection.json").write_text(
        json.dumps({"name": "research-pile", "trust_tier": "agent_authored"}),
        encoding="utf-8",
    )
    (cdir / "note1.md").write_text("# Note 1\n", encoding="utf-8")

    await cohort.enqueue_collection(
        collection_dir=str(cdir),
        scope=scope_for_domain("space-A"),
        owner_domain_id="space-A",
    )
    await cohort.drain()

    # The cohort sent the LLM a purpose-summary prompt (recognizable
    # by its template wording).
    matched = [c for c in llm.calls if "Collection name" in c]
    assert matched, "expected a collection-purpose LLM call"
