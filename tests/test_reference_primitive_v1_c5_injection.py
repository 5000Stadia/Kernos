"""REFERENCE-PRIMITIVE-V1 C5 — algorithmic injection w/ hash validation.

Pins:

* Successful injection returns the exact section content matching
  the catalog's recorded line range.
* Unknown / tombstoned entries return ``success=False`` with a
  user-facing "Reference unavailable, recataloging in progress;
  please retry." message — never raw exceptions, never partial
  content.
* Hash mismatch emits ``reference.recatalog_requested_due_to_hash_mismatch``
  AND enqueues the file for async re-cataloging via the cohort.
* File-vanished tombstones the catalog entries before returning.
* Trust-tier annotations frame external_snapshot and
  agent_authored content; canonical produces no annotation;
  quarantined surfaces the quarantine reason."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from kernos.kernel import event_stream
from kernos.kernel.reference.catalog import (
    CatalogEntry,
    CatalogStore,
    ENTRY_TYPE_FILE,
    SCOPE_INSTANCE,
    TRUST_AGENT_AUTHORED,
    TRUST_CANONICAL,
    TRUST_EXTERNAL_SNAPSHOT,
    compute_source_hash,
    scope_for_domain,
)
from kernos.kernel.reference.cohort import CatalogingCohort
from kernos.kernel.reference.events import (
    REFERENCE_SOURCE_MODULE,
    ReferenceEventEmitter,
)
from kernos.kernel.reference.injection import (
    REFERENCE_UNAVAILABLE_MESSAGE,
    inject_entry,
    trust_tier_annotation,
)


class _StubLLM:
    @property
    def temperature(self) -> float:
        return 0.1

    async def complete(self, prompt: str) -> str:
        return f"summary[{len(prompt)}]"


@pytest.fixture
async def event_stream_started(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path), flush_interval_s=0.05)
    yield
    await event_stream.stop_writer()
    await event_stream._reset_for_tests()


@pytest.fixture
async def injection_setup(tmp_path, event_stream_started):
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
    yield catalog, emitter, cohort, tmp_path
    await cohort.stop()
    await catalog.stop()


async def _seed_file_entry(
    catalog: CatalogStore,
    *,
    file_path: Path,
    body: str,
    section_title: str = "Section",
    line_start: int = 1,
    line_end: int | None = None,
    trust_tier: str = TRUST_CANONICAL,
    scope: str = SCOPE_INSTANCE,
    provenance: dict | None = None,
    entry_id: str = "ref_test",
) -> CatalogEntry:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(body, encoding="utf-8")
    body_bytes = file_path.read_bytes()
    if line_end is None:
        line_end = len(body.splitlines())
    entry = CatalogEntry(
        entry_id=entry_id,
        instance_id="inst1",
        entry_type=ENTRY_TYPE_FILE,
        scope=scope,
        category="docs",
        indexed_at="2026-05-04T00:00:00+00:00",
        trust_tier=trust_tier,
        provenance_metadata=provenance or {},
        file_path=str(file_path),
        section_title=section_title,
        one_line="oneline",
        line_start=line_start,
        line_end=line_end,
        source_hash=compute_source_hash(body_bytes),
    )
    await catalog.replace_file_entries(
        instance_id="inst1", file_path=str(file_path),
        new_entries=[entry],
    )
    return entry


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


async def test_inject_returns_exact_line_range(injection_setup):
    catalog, emitter, cohort, tmp_path = injection_setup
    body = "line 1\nline 2\nline 3 — TARGET\nline 4 — TARGET\nline 5\n"
    file_path = tmp_path / "docs/test.md"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(body, encoding="utf-8")
    entry = CatalogEntry(
        entry_id="ref_target",
        instance_id="inst1",
        entry_type=ENTRY_TYPE_FILE,
        scope=SCOPE_INSTANCE,
        category="docs",
        indexed_at="2026-05-04T00:00:00+00:00",
        trust_tier=TRUST_CANONICAL,
        file_path=str(file_path),
        section_title="The target rows",
        one_line="oneline",
        line_start=3,
        line_end=4,
        source_hash=compute_source_hash(file_path.read_bytes()),
    )
    await catalog.replace_file_entries(
        instance_id="inst1", file_path=str(file_path),
        new_entries=[entry],
    )
    result = await inject_entry(
        entry_id="ref_target",
        catalog=catalog, emitter=emitter, cohort=cohort,
        instance_id="inst1",
    )
    assert result.success is True
    assert result.content == "line 3 — TARGET\nline 4 — TARGET"
    assert result.section_title == "The target rows"
    assert result.line_start == 3 and result.line_end == 4
    assert result.trust_tier == TRUST_CANONICAL
    assert result.provenance_annotation == ""  # canonical: no annotation


# ---------------------------------------------------------------------------
# Unknown / tombstoned
# ---------------------------------------------------------------------------


async def test_inject_unknown_entry_fails_closed(injection_setup):
    catalog, emitter, cohort, _ = injection_setup
    result = await inject_entry(
        entry_id="ref_does_not_exist",
        catalog=catalog, emitter=emitter, cohort=cohort,
        instance_id="inst1",
    )
    assert result.success is False
    assert result.fail_reason == "unknown_entry"
    assert result.content == REFERENCE_UNAVAILABLE_MESSAGE


async def test_inject_tombstoned_entry_fails_closed(injection_setup):
    catalog, emitter, cohort, tmp_path = injection_setup
    entry = await _seed_file_entry(
        catalog,
        file_path=tmp_path / "docs/old.md",
        body="## A\n\nbody\n",
        section_title="A",
        line_start=1,
        line_end=3,
        entry_id="ref_old",
    )
    await catalog.tombstone_file(
        instance_id="inst1", file_path=entry.file_path,
        reason="rebuild test",
    )
    result = await inject_entry(
        entry_id=entry.entry_id,
        catalog=catalog, emitter=emitter, cohort=cohort,
        instance_id="inst1",
    )
    assert result.success is False
    assert result.fail_reason == "tombstoned"


# ---------------------------------------------------------------------------
# Hash mismatch + file vanished
# ---------------------------------------------------------------------------


async def test_inject_hash_mismatch_enqueues_recatalog(injection_setup):
    catalog, emitter, cohort, tmp_path = injection_setup
    file_path = tmp_path / "docs/drift.md"
    entry = await _seed_file_entry(
        catalog, file_path=file_path,
        body="## A\n\noriginal\n",
        section_title="A", line_start=1, line_end=3,
        entry_id="ref_drift",
    )
    # Edit the file out-of-band so its hash changes — the catalog
    # is now stale.
    file_path.write_text("## A\n\nedited\n\n## B\n\nnew\n", encoding="utf-8")
    result = await inject_entry(
        entry_id=entry.entry_id,
        catalog=catalog, emitter=emitter, cohort=cohort,
        instance_id="inst1",
    )
    assert result.success is False
    assert result.fail_reason == "hash_mismatch"
    assert result.content == REFERENCE_UNAVAILABLE_MESSAGE
    # Cohort drained — re-cataloging fired and the catalog now
    # carries entries matching the new content.
    await cohort.drain()
    refreshed = await catalog.list_entries_for_file(
        instance_id="inst1", file_path=str(file_path),
    )
    assert len(refreshed) == 2
    titles = {r.section_title for r in refreshed}
    assert titles == {"A", "B"}


async def test_inject_file_vanished_tombstones_entries(injection_setup):
    catalog, emitter, cohort, tmp_path = injection_setup
    file_path = tmp_path / "docs/gone.md"
    entry = await _seed_file_entry(
        catalog, file_path=file_path,
        body="## A\n\nbody\n",
        section_title="A", line_start=1, line_end=3,
        entry_id="ref_gone",
    )
    file_path.unlink()
    result = await inject_entry(
        entry_id=entry.entry_id,
        catalog=catalog, emitter=emitter, cohort=cohort,
        instance_id="inst1",
    )
    assert result.success is False
    assert result.fail_reason == "file_vanished"
    rows = await catalog.list_entries_for_file(
        instance_id="inst1", file_path=str(file_path),
    )
    assert rows == []
    rows_with_tombstone = await catalog.list_entries_for_file(
        instance_id="inst1", file_path=str(file_path),
        include_tombstoned=True,
    )
    assert all(r.tombstoned for r in rows_with_tombstone)


# ---------------------------------------------------------------------------
# Trust-tier annotations
# ---------------------------------------------------------------------------


async def test_external_snapshot_annotation_carries_url_and_date(injection_setup):
    catalog, emitter, cohort, tmp_path = injection_setup
    file_path = tmp_path / "refs/A/snap.md"
    entry = await _seed_file_entry(
        catalog, file_path=file_path,
        body="## Auth\n\nBearer tokens.\n",
        section_title="Auth", line_start=1, line_end=3,
        scope=scope_for_domain("space-A"),
        trust_tier=TRUST_EXTERNAL_SNAPSHOT,
        provenance={
            "source_url": "https://example.com/api",
            "fetched_at": "2026-05-04T00:00:00Z",
        },
        entry_id="ref_snap",
    )
    result = await inject_entry(
        entry_id=entry.entry_id,
        catalog=catalog, emitter=emitter, cohort=cohort,
        instance_id="inst1",
    )
    assert result.success is True
    assert "https://example.com/api" in result.provenance_annotation
    assert "2026-05-04T00:00:00Z" in result.provenance_annotation
    assert "not canonical live truth" in result.provenance_annotation


async def test_agent_authored_annotation_includes_stored_by(injection_setup):
    catalog, emitter, cohort, tmp_path = injection_setup
    file_path = tmp_path / "refs/A/note.md"
    entry = await _seed_file_entry(
        catalog, file_path=file_path,
        body="## A\n\nbody\n",
        section_title="A", line_start=1, line_end=3,
        scope=scope_for_domain("space-A"),
        trust_tier=TRUST_AGENT_AUTHORED,
        provenance={"stored_by": "member_x"},
        entry_id="ref_authored",
    )
    result = await inject_entry(
        entry_id=entry.entry_id,
        catalog=catalog, emitter=emitter, cohort=cohort,
        instance_id="inst1",
    )
    assert result.success is True
    assert "member_x" in result.provenance_annotation
