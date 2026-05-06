"""REFERENCE-PRIMITIVE-V1 C4 — agent-facing tool surface.

Pins:

* request_reference: cohort navigates the catalog (single LLM call),
  inject_entry materializes content with trust-tier annotation.
  Cross-domain isolation enforced — visibility filter prevents the
  navigator from ever seeing entries it shouldn't return.
* store_reference: writes a markdown file under
  data/references/<domain_id>/<collection>/<filename>; cataloging
  fires async; reference.stored event emitted.
* create_reference_collection: writes _collection.json with
  metadata; collection-level cataloging fires async.
* Recovery primitives round-trip through catalog + emit events.
* Filename validators reject path-component injection."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kernos.kernel import event_stream
from kernos.kernel.reference.catalog import (
    CatalogEntry,
    CatalogStore,
    ENTRY_TYPE_COLLECTION,
    ENTRY_TYPE_FILE,
    SCOPE_INSTANCE,
    TRUST_AGENT_AUTHORED,
    TRUST_CANONICAL,
    TRUST_EXTERNAL_SNAPSHOT,
    TRUST_QUARANTINED,
    compute_source_hash,
    scope_for_domain,
)
from kernos.kernel.reference.cohort import CatalogingCohort
from kernos.kernel.reference.events import (
    REFERENCE_SOURCE_MODULE,
    ReferenceEventEmitter,
)
from kernos.kernel.reference.tools import (
    ReferenceService,
    ReferenceServiceContext,
)


class _StubLLM:
    @property
    def temperature(self) -> float:
        return 0.1

    async def complete(self, prompt: str) -> str:
        return f"summary[{len(prompt)}]"


class _NavigatorLLM:
    """Pretends to be the cohort navigator. Caller pre-loads the
    answer for each upcoming brief."""

    def __init__(self) -> None:
        self.next_response: str | None = None
        self.calls: list[str] = []
        self.fail_next: bool = False

    @property
    def temperature(self) -> float:
        return 0.1

    async def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("navigator-stub-failed")
        return self.next_response or "NONE"


@pytest.fixture
async def event_stream_started(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path), flush_interval_s=0.05)
    yield
    await event_stream.stop_writer()
    await event_stream._reset_for_tests()


@pytest.fixture
async def service_setup(tmp_path, event_stream_started):
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
    nav = _NavigatorLLM()
    refs_root = tmp_path / "data" / "references"
    refs_root.mkdir(parents=True, exist_ok=True)
    service = ReferenceService(
        catalog=catalog, cohort=cohort, emitter=emitter,
        navigator_llm=nav, references_root=refs_root,
        instance_id="inst1",
    )
    yield service, catalog, cohort, nav, refs_root, tmp_path
    await cohort.stop()
    await catalog.stop()


# ---------------------------------------------------------------------------
# request_reference
# ---------------------------------------------------------------------------


async def test_request_reference_returns_content_for_match(service_setup):
    service, catalog, cohort, nav, refs_root, tmp_path = service_setup

    # Seed a catalog entry for a real on-disk file so injection
    # can hash-validate.
    file_path = tmp_path / "docs/architecture/gate.md"
    file_path.parent.mkdir(parents=True)
    body = "## Gate classification\n\nThe gate decides destructive writes.\n"
    file_path.write_text(body, encoding="utf-8")
    entry = CatalogEntry(
        entry_id="ref_gate",
        instance_id="inst1",
        entry_type=ENTRY_TYPE_FILE,
        scope=SCOPE_INSTANCE,
        category="architecture",
        indexed_at="2026-05-04T00:00:00+00:00",
        trust_tier=TRUST_CANONICAL,
        file_path=str(file_path),
        section_title="Gate classification",
        one_line="How the gate decides destructive writes",
        line_start=1,
        line_end=3,
        source_hash=compute_source_hash(file_path.read_bytes()),
    )
    await catalog.replace_file_entries(
        instance_id="inst1", file_path=str(file_path), new_entries=[entry],
    )
    nav.next_response = "ref_gate"
    ctx = ReferenceServiceContext(
        instance_id="inst1", domain_id="space-A", member_id="m1",
    )
    result = await service.handle_request_reference(
        ctx=ctx, brief_request="how does the gate decide what's destructive",
    )
    assert result["status"] == "ok"
    assert result["entry_id"] == "ref_gate"
    assert "destructive writes" in result["content"]
    assert result["section_title"] == "Gate classification"
    assert result["trust_tier"] == TRUST_CANONICAL
    assert nav.calls, "navigator should have been called"


async def test_request_reference_no_match_returns_no_match(service_setup):
    service, catalog, cohort, nav, refs_root, tmp_path = service_setup
    # Empty catalog — no_catalog status.
    ctx = ReferenceServiceContext(
        instance_id="inst1", domain_id="space-A", member_id="m1",
    )
    result = await service.handle_request_reference(
        ctx=ctx, brief_request="anything",
    )
    assert result["status"] == "no_catalog"


async def test_request_reference_no_match_surfaces_candidates(service_setup):
    """DOCS-AUDIT-RECOVERY #1: when the navigator returns NONE, the
    response carries a candidate list scored by token-overlap so the
    agent has something concrete to refine against. Recovers the
    discoverability affordance the retired read_doc had."""
    service, catalog, cohort, nav, refs_root, tmp_path = service_setup

    # Seed three real catalog entries under different categories.
    for i, (cat, title, oneline) in enumerate([
        ("architecture", "Gate classification",
         "How the gate decides destructive writes"),
        ("architecture", "Memory ledger architecture",
         "Two-store dual-memory design"),
        ("primitives", "Canvas primitive overview",
         "Scoped markdown directories"),
    ]):
        fp = tmp_path / f"docs/{cat}/{i}.md"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(f"## {title}\n\n{oneline}\n", encoding="utf-8")
        e = CatalogEntry(
            entry_id=f"ref_seed_{i}",
            instance_id="inst1",
            entry_type=ENTRY_TYPE_FILE,
            scope=SCOPE_INSTANCE,
            category=cat,
            indexed_at="2026-05-04T00:00:00+00:00",
            trust_tier=TRUST_CANONICAL,
            file_path=str(fp),
            section_title=title,
            one_line=oneline,
            line_start=1,
            line_end=3,
            source_hash=compute_source_hash(fp.read_bytes()),
        )
        await catalog.replace_file_entries(
            instance_id="inst1", file_path=str(fp), new_entries=[e],
        )

    # Force the navigator to return NONE so the fallback fires.
    nav.next_response = "NONE"
    ctx = ReferenceServiceContext(
        instance_id="inst1", domain_id="space-A", member_id="m1",
    )
    result = await service.handle_request_reference(
        ctx=ctx, brief_request="how does the gate classification work",
    )
    assert result["status"] == "no_match"
    assert "candidates" in result
    candidates = result["candidates"]
    assert isinstance(candidates, list)
    assert len(candidates) > 0
    # Highest-overlap candidate should be the gate-classification entry.
    top = candidates[0]
    assert top["section_title"] == "Gate classification"
    assert top["category"] == "architecture"
    assert "destructive writes" in top["one_line"]
    # Each candidate carries entry_id so the agent can cite it.
    for c in candidates:
        assert "entry_id" in c
        assert "kind" in c


async def test_request_reference_no_match_no_overlap_falls_back_to_slice(
    service_setup,
):
    """When the brief shares zero tokens with any catalog entry, the
    candidate list falls back to a representative instance-scope slice
    so the agent always has SOMETHING to anchor against."""
    service, catalog, _, nav, _, tmp_path = service_setup
    fp = tmp_path / "docs/x.md"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("## Topic\n\nbody\n", encoding="utf-8")
    await catalog.replace_file_entries(
        instance_id="inst1", file_path=str(fp),
        new_entries=[CatalogEntry(
            entry_id="ref_x",
            instance_id="inst1",
            entry_type=ENTRY_TYPE_FILE,
            scope=SCOPE_INSTANCE,
            category="architecture",
            indexed_at="2026-05-04T00:00:00+00:00",
            trust_tier=TRUST_CANONICAL,
            file_path=str(fp),
            section_title="Topic",
            one_line="aaa bbb ccc",
            line_start=1, line_end=3,
            source_hash=compute_source_hash(fp.read_bytes()),
        )],
    )
    nav.next_response = "NONE"
    ctx = ReferenceServiceContext(
        instance_id="inst1", domain_id="space-A", member_id="m1",
    )
    result = await service.handle_request_reference(
        ctx=ctx, brief_request="zzzzzzz unrelated brief xyzzy",
    )
    assert result["status"] == "no_match"
    # No-overlap path still surfaces something (representative slice)
    # so the agent isn't left empty-handed.
    assert len(result["candidates"]) >= 1


async def test_request_reference_navigator_picks_invisible_entry(service_setup):
    """Defense-in-depth: even if the navigator returns an entry_id
    that's outside the visible catalog, the service rejects it
    rather than injecting."""
    service, catalog, cohort, nav, refs_root, tmp_path = service_setup
    # Seed an entry in domain B.
    file_path = tmp_path / "refs/B/secret.md"
    file_path.parent.mkdir(parents=True)
    file_path.write_text("## Secret\n\nstuff\n", encoding="utf-8")
    other = CatalogEntry(
        entry_id="ref_other_domain",
        instance_id="inst1",
        entry_type=ENTRY_TYPE_FILE,
        scope=scope_for_domain("space-B"),
        category="refs",
        indexed_at="2026-05-04T00:00:00+00:00",
        trust_tier=TRUST_AGENT_AUTHORED,
        file_path=str(file_path),
        section_title="Secret",
        one_line="secret",
        line_start=1,
        line_end=3,
        source_hash=compute_source_hash(file_path.read_bytes()),
        owner_domain_id="space-B",
    )
    await catalog.replace_file_entries(
        instance_id="inst1", file_path=str(file_path),
        new_entries=[other],
    )
    # Also seed an entry in domain A so the catalog isn't empty.
    file_a = tmp_path / "refs/A/note.md"
    file_a.parent.mkdir(parents=True)
    file_a.write_text("## A\n\nstuff\n", encoding="utf-8")
    own = CatalogEntry(
        entry_id="ref_own",
        instance_id="inst1",
        entry_type=ENTRY_TYPE_FILE,
        scope=scope_for_domain("space-A"),
        category="refs",
        indexed_at="2026-05-04T00:00:00+00:00",
        trust_tier=TRUST_AGENT_AUTHORED,
        file_path=str(file_a),
        section_title="A",
        one_line="own",
        line_start=1,
        line_end=3,
        source_hash=compute_source_hash(file_a.read_bytes()),
        owner_domain_id="space-A",
    )
    await catalog.replace_file_entries(
        instance_id="inst1", file_path=str(file_a),
        new_entries=[own],
    )
    # Navigator returns the OUT-OF-DOMAIN entry_id.
    nav.next_response = "ref_other_domain"
    ctx = ReferenceServiceContext(
        instance_id="inst1", domain_id="space-A", member_id="m1",
    )
    result = await service.handle_request_reference(
        ctx=ctx, brief_request="anything",
    )
    assert result["status"] == "no_match"


# ---------------------------------------------------------------------------
# store_reference
# ---------------------------------------------------------------------------


async def test_store_reference_writes_file_and_enqueues(service_setup):
    service, catalog, cohort, nav, refs_root, tmp_path = service_setup
    ctx = ReferenceServiceContext(
        instance_id="inst1", domain_id="space-A", member_id="m1",
    )
    result = await service.handle_store_reference(
        ctx=ctx,
        content="## Auth\n\nBearer tokens.\n",
        collection="vendor-test-api",
        filename="auth.md",
        trust_tier=TRUST_EXTERNAL_SNAPSHOT,
        metadata={"source_url": "https://x.example", "fetched_at": "2026-05-04"},
    )
    assert result["status"] == "ok"
    written = Path(result["file_path"])
    assert written.exists()
    assert "Bearer tokens" in written.read_text()
    await cohort.drain()
    rows = await catalog.list_entries_for_file(
        instance_id="inst1", file_path=str(written),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.scope == scope_for_domain("space-A")
    assert row.collection_back_reference == "vendor-test-api"
    assert row.trust_tier == TRUST_EXTERNAL_SNAPSHOT
    assert row.provenance_metadata["stored_by"] == "m1"
    assert row.provenance_metadata["source_url"] == "https://x.example"


async def test_store_reference_rejects_path_injection(service_setup):
    service, _, _, _, _, _ = service_setup
    ctx = ReferenceServiceContext(
        instance_id="inst1", domain_id="space-A", member_id="m1",
    )
    bad = await service.handle_store_reference(
        ctx=ctx, content="x", collection="c", filename="../escape.md",
    )
    assert bad["status"] == "error"
    assert "filename" in bad["error"]


async def test_store_reference_requires_domain_context(service_setup):
    service, _, _, _, _, _ = service_setup
    ctx = ReferenceServiceContext(
        instance_id="inst1", domain_id="", member_id="m1",
    )
    bad = await service.handle_store_reference(
        ctx=ctx, content="x", collection="c", filename="x.md",
    )
    assert bad["status"] == "error"


# ---------------------------------------------------------------------------
# create_reference_collection
# ---------------------------------------------------------------------------


async def test_create_reference_collection_writes_meta(service_setup):
    service, catalog, cohort, _, refs_root, _ = service_setup
    ctx = ReferenceServiceContext(
        instance_id="inst1", domain_id="space-A", member_id="m1",
    )
    result = await service.handle_create_reference_collection(
        ctx=ctx,
        name="vendor-test-api",
        purpose="API docs for the test vendor.",
        trust_tier=TRUST_EXTERNAL_SNAPSHOT,
        refresh_policy="snapshot",
        provenance={"source_url": "https://x.example"},
    )
    assert result["status"] == "ok"
    meta_path = refs_root / "space-A" / "vendor-test-api" / "_collection.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text())
    assert meta["purpose"] == "API docs for the test vendor."
    assert meta["trust_tier"] == TRUST_EXTERNAL_SNAPSHOT
    assert meta["provenance"]["stored_by"] == "m1"
    await cohort.drain()
    coll = await catalog.get_collection_entry(
        instance_id="inst1",
        collection_name="vendor-test-api",
        scope=scope_for_domain("space-A"),
    )
    assert coll is not None
    assert coll.purpose == "API docs for the test vendor."


async def test_create_reference_collection_idempotent_on_second_call(
    service_setup,
):
    service, _, _, _, _, _ = service_setup
    ctx = ReferenceServiceContext(
        instance_id="inst1", domain_id="space-A", member_id="m1",
    )
    first = await service.handle_create_reference_collection(
        ctx=ctx, name="research", purpose="x",
    )
    second = await service.handle_create_reference_collection(
        ctx=ctx, name="research", purpose="y",
    )
    assert first["status"] == "ok"
    assert second["status"] == "exists"


# ---------------------------------------------------------------------------
# Recovery primitives
# ---------------------------------------------------------------------------


async def _seed_authored_entry(
    catalog: CatalogStore, tmp_path: Path,
) -> CatalogEntry:
    fp = tmp_path / "refs/A/n.md"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("## A\n\nbody\n", encoding="utf-8")
    e = CatalogEntry(
        entry_id="ref_auth",
        instance_id="inst1",
        entry_type=ENTRY_TYPE_FILE,
        scope=scope_for_domain("space-A"),
        category="refs",
        indexed_at="2026-05-04T00:00:00+00:00",
        trust_tier=TRUST_AGENT_AUTHORED,
        file_path=str(fp),
        section_title="A",
        one_line="oneline",
        line_start=1, line_end=3,
        source_hash=compute_source_hash(fp.read_bytes()),
    )
    await catalog.replace_file_entries(
        instance_id="inst1", file_path=str(fp), new_entries=[e],
    )
    return e


async def test_quarantine_then_restore(service_setup):
    service, catalog, _, _, _, tmp_path = service_setup
    e = await _seed_authored_entry(catalog, tmp_path)
    ctx = ReferenceServiceContext(
        instance_id="inst1", domain_id="space-A", member_id="m1",
    )
    qr = await service.handle_quarantine_reference(
        ctx=ctx, entry_id=e.entry_id, reason="suspect content",
    )
    assert qr["status"] == "ok"
    assert qr["trust_tier"] == TRUST_QUARANTINED
    rr = await service.handle_restore_reference_from_quarantine(
        ctx=ctx, entry_id=e.entry_id,
    )
    assert rr["status"] == "ok"
    assert rr["trust_tier"] == TRUST_AGENT_AUTHORED


async def test_supersede_recovery(service_setup):
    service, catalog, _, _, _, tmp_path = service_setup
    old = await _seed_authored_entry(catalog, tmp_path)
    new_path = tmp_path / "refs/A/n2.md"
    new_path.write_text("## A2\n\nbody\n", encoding="utf-8")
    new = CatalogEntry(
        entry_id="ref_new",
        instance_id="inst1",
        entry_type=ENTRY_TYPE_FILE,
        scope=scope_for_domain("space-A"),
        category="refs",
        indexed_at="2026-05-04T00:00:00+00:00",
        trust_tier=TRUST_AGENT_AUTHORED,
        file_path=str(new_path),
        section_title="A2",
        one_line="oneline2",
        line_start=1, line_end=3,
        source_hash=compute_source_hash(new_path.read_bytes()),
    )
    await catalog.replace_file_entries(
        instance_id="inst1", file_path=str(new_path), new_entries=[new],
    )
    ctx = ReferenceServiceContext(
        instance_id="inst1", domain_id="space-A", member_id="m1",
    )
    res = await service.handle_mark_reference_superseded(
        ctx=ctx,
        old_entry_id=old.entry_id,
        new_entry_id=new.entry_id,
        reason="v2 published",
    )
    assert res["status"] == "ok"
    old_after = await catalog.get_entry(entry_id=old.entry_id)
    assert old_after is not None and old_after.tombstoned is True


async def test_move_to_canvas_recovery(service_setup):
    service, catalog, _, _, _, tmp_path = service_setup
    e = await _seed_authored_entry(catalog, tmp_path)
    ctx = ReferenceServiceContext(
        instance_id="inst1", domain_id="space-A", member_id="m1",
    )
    res = await service.handle_move_reference_to_canvas(
        ctx=ctx, entry_id=e.entry_id, target_canvas="My Tools / Notes",
    )
    assert res["status"] == "ok"
    after = await catalog.get_entry(entry_id=e.entry_id)
    assert after is not None and after.tombstoned is True
    assert after.provenance_metadata["moved_to_canvas"] == "My Tools / Notes"


# ---------------------------------------------------------------------------
# Cross-domain mutation guard (Codex review fix)
# ---------------------------------------------------------------------------


async def test_recovery_primitives_reject_cross_domain_mutation(
    service_setup,
):
    """An agent in domain X with a guessed entry_id from domain Y
    must NOT be able to quarantine / restore / move-to-canvas /
    supersede the foreign entry. Visibility leak via mutation was
    the gap Codex flagged."""
    service, catalog, _, _, _, tmp_path = service_setup
    fp = tmp_path / "refs/B/secret.md"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("## A\n\nbody\n", encoding="utf-8")
    domain_b_entry = CatalogEntry(
        entry_id="ref_other_domain",
        instance_id="inst1",
        entry_type=ENTRY_TYPE_FILE,
        scope=scope_for_domain("space-B"),
        category="refs",
        indexed_at="2026-05-04T00:00:00+00:00",
        trust_tier=TRUST_AGENT_AUTHORED,
        file_path=str(fp),
        section_title="A",
        one_line="oneline",
        line_start=1, line_end=3,
        source_hash=compute_source_hash(fp.read_bytes()),
        owner_domain_id="space-B",
    )
    await catalog.replace_file_entries(
        instance_id="inst1", file_path=str(fp),
        new_entries=[domain_b_entry],
    )
    ctx_a = ReferenceServiceContext(
        instance_id="inst1", domain_id="space-A", member_id="attacker",
    )
    # quarantine across domain — refused.
    qr = await service.handle_quarantine_reference(
        ctx=ctx_a, entry_id="ref_other_domain",
        reason="trying to quarantine someone else's data",
    )
    assert qr["status"] == "error"
    # restore across domain — refused.
    rr = await service.handle_restore_reference_from_quarantine(
        ctx=ctx_a, entry_id="ref_other_domain",
    )
    assert rr["status"] == "error"
    # move_to_canvas across domain — refused.
    mr = await service.handle_move_reference_to_canvas(
        ctx=ctx_a, entry_id="ref_other_domain",
        target_canvas="My Stuff / X",
    )
    assert mr["status"] == "error"
    # The entry remains untouched.
    untouched = await catalog.get_entry(entry_id="ref_other_domain")
    assert untouched is not None
    assert untouched.tombstoned is False
    assert untouched.trust_tier == TRUST_AGENT_AUTHORED


async def test_supersede_rejects_cross_domain_pairs(service_setup):
    """Supersede requires both old AND new entries visible in the
    caller's domain; mixing scopes is refused."""
    service, catalog, _, _, _, tmp_path = service_setup
    own = await _seed_authored_entry(catalog, tmp_path)
    fp_b = tmp_path / "refs/B/x.md"
    fp_b.parent.mkdir(parents=True, exist_ok=True)
    fp_b.write_text("## A\n\nbody\n", encoding="utf-8")
    other = CatalogEntry(
        entry_id="ref_b_only",
        instance_id="inst1",
        entry_type=ENTRY_TYPE_FILE,
        scope=scope_for_domain("space-B"),
        category="refs",
        indexed_at="2026-05-04T00:00:00+00:00",
        trust_tier=TRUST_AGENT_AUTHORED,
        file_path=str(fp_b),
        section_title="A",
        one_line="oneline",
        line_start=1, line_end=3,
        source_hash=compute_source_hash(fp_b.read_bytes()),
        owner_domain_id="space-B",
    )
    await catalog.replace_file_entries(
        instance_id="inst1", file_path=str(fp_b), new_entries=[other],
    )
    ctx_a = ReferenceServiceContext(
        instance_id="inst1", domain_id="space-A", member_id="attacker",
    )
    res = await service.handle_mark_reference_superseded(
        ctx=ctx_a,
        old_entry_id=own.entry_id,
        new_entry_id="ref_b_only",
        reason="x",
    )
    assert res["status"] == "error"
    # Old entry NOT tombstoned because supersede refused.
    untouched = await catalog.get_entry(entry_id=own.entry_id)
    assert untouched is not None
    assert untouched.tombstoned is False


async def test_supersede_validates_new_entry_exists(service_setup):
    """supersede() raises UnknownEntry if the new_entry_id doesn't
    exist; supersession chains stay consistent."""
    service, catalog, _, _, _, tmp_path = service_setup
    own = await _seed_authored_entry(catalog, tmp_path)
    ctx = ReferenceServiceContext(
        instance_id="inst1", domain_id="space-A", member_id="m1",
    )
    res = await service.handle_mark_reference_superseded(
        ctx=ctx,
        old_entry_id=own.entry_id,
        new_entry_id="ref_does_not_exist",
        reason="x",
    )
    assert res["status"] == "error"
    untouched = await catalog.get_entry(entry_id=own.entry_id)
    assert untouched is not None and untouched.tombstoned is False


# ---------------------------------------------------------------------------
# Collection-level navigator response (Codex review fix)
# ---------------------------------------------------------------------------


async def test_request_reference_collection_returns_map_not_inject(
    service_setup,
):
    """When the navigator picks a collection-level entry,
    handle_request_reference returns a map shape (purpose +
    member-file count), NOT an inject_entry result that would fail
    on the missing file_path."""
    service, catalog, cohort, nav, refs_root, tmp_path = service_setup
    coll = CatalogEntry(
        entry_id="ref_collection_only",
        instance_id="inst1",
        entry_type=ENTRY_TYPE_COLLECTION,
        scope=scope_for_domain("space-A"),
        category="vendor-x",
        indexed_at="2026-05-04T00:00:00+00:00",
        trust_tier=TRUST_EXTERNAL_SNAPSHOT,
        collection_name="vendor-x",
        purpose="Vendor X API documentation",
        refresh_policy="snapshot",
        member_file_count=3,
        member_file_paths=["auth.md", "rate-limits.md", "errors.md"],
        owner_domain_id="space-A",
    )
    await catalog.upsert_collection_entry(entry=coll)
    nav.next_response = "ref_collection_only"
    ctx = ReferenceServiceContext(
        instance_id="inst1", domain_id="space-A", member_id="m1",
    )
    result = await service.handle_request_reference(
        ctx=ctx, brief_request="anything about vendor X",
    )
    assert result["status"] == "ok_collection"
    assert result["collection_name"] == "vendor-x"
    assert result["purpose"] == "Vendor X API documentation"
    assert result["member_file_count"] == 3
    assert "content" not in result  # no inject; this is a map.
