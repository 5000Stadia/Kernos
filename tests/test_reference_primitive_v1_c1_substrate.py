"""REFERENCE-PRIMITIVE-V1 C1 — catalog substrate + events.

Pins:

* The ``"reference"`` source_module registers cleanly with the
  EmitterRegistry; :class:`ReferenceEventEmitter` enforces source
  identity at construction.
* :class:`CatalogStore` opens an aiosqlite connection to instance.db,
  creates the ``reference_catalog`` table + indexes, and survives
  start/stop cycles.
* File-level transactional rebuild: existing rows for a file are
  dropped before new rows are inserted; partial-state corruption
  cannot happen.
* Visibility rule: ``list_visible(domain_id=X)`` returns
  ``scope='instance'`` rows + ``scope='domain:X'`` rows but NOT
  ``scope='domain:Y'`` rows.
* Quarantine + restore round-trips through the prior trust tier.
* Supersede tombstones the old entry and links the new one in
  provenance.
* Trust-tier and entry-type validators reject bad inputs at the
  dataclass boundary.
"""
from __future__ import annotations

import pytest

from kernos.kernel import event_stream
from kernos.kernel.reference.catalog import (
    CatalogEntry,
    CatalogStore,
    ENTRY_TYPE_COLLECTION,
    ENTRY_TYPE_FILE,
    InvalidEntryType,
    InvalidTrustTier,
    SCOPE_INSTANCE,
    TRUST_AGENT_AUTHORED,
    TRUST_CANONICAL,
    TRUST_EXTERNAL_SNAPSHOT,
    TRUST_QUARANTINED,
    UnknownEntry,
    compute_source_hash,
    parse_domain_from_scope,
    scope_for_domain,
)
from kernos.kernel.reference.events import (
    REFERENCE_EVENT_TYPES,
    REFERENCE_SOURCE_MODULE,
    ReferenceEventEmitter,
)


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
async def catalog_store(tmp_path, event_stream_started):
    store = CatalogStore()
    await store.start(str(tmp_path))
    yield store
    await store.stop()


def _file_entry(
    *,
    instance_id: str = "inst1",
    file_path: str = "/tmp/docs/x.md",
    section_title: str = "Section",
    one_line: str = "An example section.",
    line_start: int = 1,
    line_end: int = 10,
    source_hash: str = "abc",
    scope: str = SCOPE_INSTANCE,
    category: str = "architecture",
    trust_tier: str = TRUST_CANONICAL,
    indexed_at: str = "2026-05-04T00:00:00+00:00",
    entry_id: str = "ref_test_1",
    owner_domain_id: str = "",
) -> CatalogEntry:
    return CatalogEntry(
        entry_id=entry_id,
        instance_id=instance_id,
        entry_type=ENTRY_TYPE_FILE,
        scope=scope,
        category=category,
        indexed_at=indexed_at,
        trust_tier=trust_tier,
        file_path=file_path,
        section_title=section_title,
        one_line=one_line,
        line_start=line_start,
        line_end=line_end,
        source_hash=source_hash,
        owner_domain_id=owner_domain_id,
    )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


async def test_reference_emitter_registers_with_correct_source_module(
    event_stream_started,
):
    registry = event_stream.emitter_registry()
    raw = registry.register(REFERENCE_SOURCE_MODULE)
    adapter = ReferenceEventEmitter(emitter=raw)
    assert adapter.source_module == REFERENCE_SOURCE_MODULE
    assert registry.is_registered(REFERENCE_SOURCE_MODULE)


async def test_reference_emitter_rejects_wrong_source_module(
    event_stream_started,
):
    registry = event_stream.emitter_registry()
    raw = registry.register("not_reference")
    with pytest.raises(ValueError):
        ReferenceEventEmitter(emitter=raw)


async def test_event_types_match_named_constants():
    """All twelve event-type constants must be enumerated in
    REFERENCE_EVENT_TYPES so downstream consumers can validate."""
    assert "reference.cataloged" in REFERENCE_EVENT_TYPES
    assert "reference.recataloged" in REFERENCE_EVENT_TYPES
    assert "reference.recatalog_failed" in REFERENCE_EVENT_TYPES
    assert "reference.tombstoned" in REFERENCE_EVENT_TYPES
    assert "reference.stored" in REFERENCE_EVENT_TYPES
    assert "reference.superseded" in REFERENCE_EVENT_TYPES
    assert "reference.quarantined" in REFERENCE_EVENT_TYPES
    assert "reference.restored_from_quarantine" in REFERENCE_EVENT_TYPES
    assert "reference.moved_to_canvas" in REFERENCE_EVENT_TYPES
    assert "reference.collection_created" in REFERENCE_EVENT_TYPES
    assert "reference.collection_refreshed" in REFERENCE_EVENT_TYPES
    assert (
        "reference.recatalog_requested_due_to_hash_mismatch"
        in REFERENCE_EVENT_TYPES
    )
    # Sanity: 12 named events.
    assert len(REFERENCE_EVENT_TYPES) == 12


# ---------------------------------------------------------------------------
# Catalog dataclass validators
# ---------------------------------------------------------------------------


def test_catalog_entry_rejects_invalid_trust_tier():
    with pytest.raises(InvalidTrustTier):
        CatalogEntry(
            entry_id="x", instance_id="i", entry_type=ENTRY_TYPE_FILE,
            scope=SCOPE_INSTANCE, category="c", indexed_at="t",
            trust_tier="bogus",
        )


def test_catalog_entry_rejects_invalid_entry_type():
    with pytest.raises(InvalidEntryType):
        CatalogEntry(
            entry_id="x", instance_id="i", entry_type="weird",
            scope=SCOPE_INSTANCE, category="c", indexed_at="t",
        )


def test_scope_helpers_round_trip():
    assert scope_for_domain("space-A") == "domain:space-A"
    assert parse_domain_from_scope("domain:space-A") == "space-A"
    assert parse_domain_from_scope("instance") is None


def test_compute_source_hash_is_deterministic():
    a = compute_source_hash(b"hello")
    b = compute_source_hash(b"hello")
    c = compute_source_hash(b"world")
    assert a == b
    assert a != c
    assert len(a) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# Store smoke: insert + read + transactional rebuild
# ---------------------------------------------------------------------------


async def test_store_round_trips_file_entry(catalog_store):
    entry = _file_entry(entry_id="ref_a", source_hash="hash1")
    await catalog_store.replace_file_entries(
        instance_id=entry.instance_id,
        file_path=entry.file_path,
        new_entries=[entry],
    )
    got = await catalog_store.get_entry(entry_id="ref_a")
    assert got is not None
    assert got.section_title == "Section"
    assert got.source_hash == "hash1"
    assert got.scope == SCOPE_INSTANCE
    assert got.tombstoned is False


async def test_replace_file_entries_drops_existing(catalog_store):
    """File-level rebuild on hash change: existing rows go, new rows
    arrive. No partial-state allowed."""
    e1 = _file_entry(entry_id="ref_old1", line_start=1, line_end=5,
                     source_hash="oldhash")
    e2 = _file_entry(entry_id="ref_old2", line_start=6, line_end=10,
                     source_hash="oldhash", section_title="S2")
    await catalog_store.replace_file_entries(
        instance_id="inst1", file_path="/tmp/docs/x.md",
        new_entries=[e1, e2],
    )
    existing = await catalog_store.list_entries_for_file(
        instance_id="inst1", file_path="/tmp/docs/x.md",
    )
    assert len(existing) == 2

    # Rebuild with a different shape — three new entries, fewer total
    new1 = _file_entry(entry_id="ref_new1", section_title="A",
                       line_start=1, line_end=3, source_hash="newhash")
    new2 = _file_entry(entry_id="ref_new2", section_title="B",
                       line_start=4, line_end=8, source_hash="newhash")
    await catalog_store.replace_file_entries(
        instance_id="inst1", file_path="/tmp/docs/x.md",
        new_entries=[new1, new2],
    )
    after = await catalog_store.list_entries_for_file(
        instance_id="inst1", file_path="/tmp/docs/x.md",
    )
    assert len(after) == 2
    assert {e.entry_id for e in after} == {"ref_new1", "ref_new2"}
    # Old rows are gone, not just tombstoned — drop-and-rebuild is
    # transactional removal.
    assert await catalog_store.get_entry(entry_id="ref_old1") is None
    assert await catalog_store.get_entry(entry_id="ref_old2") is None


async def test_replace_file_entries_rejects_cross_file_payload(
    catalog_store,
):
    """Each call rebuilds a single file's entries; a row for a
    different path is a programming error."""
    e1 = _file_entry(entry_id="r1", file_path="/a.md")
    e2 = _file_entry(entry_id="r2", file_path="/b.md")
    with pytest.raises(Exception):
        await catalog_store.replace_file_entries(
            instance_id="inst1", file_path="/a.md", new_entries=[e1, e2],
        )


async def test_get_source_hash_returns_recorded_hash(catalog_store):
    entry = _file_entry(entry_id="ref_a", source_hash="hash-xyz")
    await catalog_store.replace_file_entries(
        instance_id="inst1", file_path="/tmp/docs/x.md",
        new_entries=[entry],
    )
    h = await catalog_store.get_source_hash(
        instance_id="inst1", file_path="/tmp/docs/x.md",
    )
    assert h == "hash-xyz"


async def test_get_source_hash_returns_none_when_no_entries(
    catalog_store,
):
    assert (
        await catalog_store.get_source_hash(
            instance_id="inst1", file_path="/nope.md",
        )
        is None
    )


# ---------------------------------------------------------------------------
# Visibility rule
# ---------------------------------------------------------------------------


async def test_list_visible_includes_instance_and_own_domain(catalog_store):
    """The load-bearing scoping invariant: agent in domain X sees
    instance + domain:X, never domain:Y."""
    docs = _file_entry(entry_id="ref_docs", scope=SCOPE_INSTANCE,
                       file_path="/docs/a.md")
    own = _file_entry(
        entry_id="ref_own",
        scope=scope_for_domain("space-A"),
        owner_domain_id="space-A",
        trust_tier=TRUST_AGENT_AUTHORED,
        file_path="/refs/A/own.md",
    )
    other = _file_entry(
        entry_id="ref_other",
        scope=scope_for_domain("space-B"),
        owner_domain_id="space-B",
        trust_tier=TRUST_AGENT_AUTHORED,
        file_path="/refs/B/other.md",
    )
    await catalog_store.replace_file_entries(
        instance_id="inst1", file_path="/docs/a.md", new_entries=[docs],
    )
    await catalog_store.replace_file_entries(
        instance_id="inst1", file_path="/refs/A/own.md", new_entries=[own],
    )
    await catalog_store.replace_file_entries(
        instance_id="inst1", file_path="/refs/B/other.md",
        new_entries=[other],
    )
    visible = await catalog_store.list_visible(
        instance_id="inst1", domain_id="space-A",
    )
    visible_ids = {e.entry_id for e in visible}
    assert "ref_docs" in visible_ids
    assert "ref_own" in visible_ids
    assert "ref_other" not in visible_ids


async def test_list_visible_excludes_tombstoned(catalog_store):
    docs = _file_entry(entry_id="ref_docs", scope=SCOPE_INSTANCE)
    await catalog_store.replace_file_entries(
        instance_id="inst1", file_path="/tmp/docs/x.md", new_entries=[docs],
    )
    count_before = len(
        await catalog_store.list_visible(
            instance_id="inst1", domain_id="any",
        )
    )
    assert count_before == 1
    await catalog_store.tombstone_file(
        instance_id="inst1", file_path="/tmp/docs/x.md", reason="deleted",
    )
    count_after = len(
        await catalog_store.list_visible(
            instance_id="inst1", domain_id="any",
        )
    )
    assert count_after == 0


# ---------------------------------------------------------------------------
# Quarantine / restore
# ---------------------------------------------------------------------------


async def test_quarantine_round_trip_preserves_prior_tier(catalog_store):
    entry = _file_entry(
        entry_id="ref_q", trust_tier=TRUST_EXTERNAL_SNAPSHOT,
        file_path="/refs/A/snap.md",
    )
    await catalog_store.replace_file_entries(
        instance_id="inst1", file_path="/refs/A/snap.md",
        new_entries=[entry],
    )
    quarantined = await catalog_store.quarantine_entry(
        entry_id="ref_q",
        reason="source URL tampered",
        quarantined_by="member_x",
    )
    assert quarantined.trust_tier == TRUST_QUARANTINED
    assert quarantined.auto_inducible is False
    assert (
        quarantined.provenance_metadata["prior_trust_tier"]
        == TRUST_EXTERNAL_SNAPSHOT
    )

    restored, prior = await catalog_store.restore_entry(
        entry_id="ref_q", restored_by="member_x",
    )
    assert prior == TRUST_EXTERNAL_SNAPSHOT
    assert restored.trust_tier == TRUST_EXTERNAL_SNAPSHOT
    # External snapshot is auto-inducible (under tighter threshold
    # at the auto-induction layer; the catalog flag stays True).
    assert restored.auto_inducible is True


async def test_quarantine_unknown_entry_raises(catalog_store):
    with pytest.raises(UnknownEntry):
        await catalog_store.quarantine_entry(
            entry_id="ref_does_not_exist", reason="x", quarantined_by="m",
        )


# ---------------------------------------------------------------------------
# Supersede
# ---------------------------------------------------------------------------


async def test_supersede_tombstones_old_and_links_new(catalog_store):
    old = _file_entry(entry_id="ref_old", file_path="/refs/A/v1.md")
    new = _file_entry(entry_id="ref_new", file_path="/refs/A/v2.md")
    await catalog_store.replace_file_entries(
        instance_id="inst1", file_path="/refs/A/v1.md", new_entries=[old],
    )
    await catalog_store.replace_file_entries(
        instance_id="inst1", file_path="/refs/A/v2.md", new_entries=[new],
    )
    await catalog_store.supersede(
        old_entry_id="ref_old", new_entry_id="ref_new",
        reason="api-v2 published",
    )
    old_after = await catalog_store.get_entry(entry_id="ref_old")
    assert old_after is not None
    assert old_after.tombstoned is True
    assert old_after.provenance_metadata["superseded_by"] == "ref_new"
    new_after = await catalog_store.get_entry(entry_id="ref_new")
    assert new_after is not None
    assert new_after.tombstoned is False


# ---------------------------------------------------------------------------
# Collection-level entries
# ---------------------------------------------------------------------------


async def test_upsert_collection_entry_round_trips(catalog_store):
    coll = CatalogEntry(
        entry_id="ref_coll_1",
        instance_id="inst1",
        entry_type=ENTRY_TYPE_COLLECTION,
        scope=scope_for_domain("space-A"),
        category="vendor-test-api",
        indexed_at="2026-05-04T00:00:00+00:00",
        trust_tier=TRUST_EXTERNAL_SNAPSHOT,
        collection_name="vendor-test-api",
        purpose="Test vendor API",
        refresh_policy="snapshot",
        member_file_count=2,
        member_file_paths=["a.md", "b.md"],
        owner_domain_id="space-A",
    )
    await catalog_store.upsert_collection_entry(entry=coll)
    got = await catalog_store.get_collection_entry(
        instance_id="inst1",
        collection_name="vendor-test-api",
        scope=scope_for_domain("space-A"),
    )
    assert got is not None
    assert got.purpose == "Test vendor API"
    assert got.member_file_count == 2
    assert got.member_file_paths == ["a.md", "b.md"]
