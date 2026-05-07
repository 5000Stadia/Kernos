"""CANVAS-V1 Pillar 1 — primitive create/read/write/list/search + scopes.

Spec reference: SPEC-CANVAS-V1, Pillars 1+2 expected behaviors.
"""
from __future__ import annotations

import pytest

from kernos.kernel.canvas import (
    CanvasService,
    parse_frontmatter,
    serialize_frontmatter,
)
from kernos.kernel.instance_db import InstanceDB


INSTANCE = "inst_canvastest"


@pytest.fixture
async def env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member("alice", "Alice", "owner", "")
    await idb.create_member("bob", "Bob", "member", "")
    svc = CanvasService(instance_db=idb, data_dir=str(tmp_path))
    yield svc, idb, tmp_path
    await idb.close()


async def test_create_personal_canvas(env):
    svc, idb, _ = env
    r = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Valencia Notes", scope="personal", description="world",
    )
    assert r.ok
    assert r.canvas_id.startswith("canvas_")
    # Owner has access; another member does not.
    assert await idb.member_has_canvas_access(canvas_id=r.canvas_id, member_id="alice")
    assert not await idb.member_has_canvas_access(canvas_id=r.canvas_id, member_id="bob")


async def test_create_team_canvas_any_member_sees(env):
    svc, idb, _ = env
    r = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Team Plan", scope="team",
    )
    assert r.ok
    assert await idb.member_has_canvas_access(canvas_id=r.canvas_id, member_id="bob")


async def test_create_specific_canvas_only_declared_members(env):
    svc, idb, _ = env
    r = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="Shared", scope="specific", members=["bob"],
    )
    assert r.ok
    assert await idb.member_has_canvas_access(canvas_id=r.canvas_id, member_id="bob")
    assert not await idb.member_has_canvas_access(canvas_id=r.canvas_id, member_id="carol")


async def test_specific_requires_members_list(env):
    svc, _, _ = env
    r = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="x", scope="specific",
    )
    assert not r.ok
    assert "specific" in r.error.lower()


async def test_unknown_scope_rejected(env):
    svc, _, _ = env
    r = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="x", scope="global",
    )
    assert not r.ok
    assert "scope" in r.error.lower()


async def test_page_write_read_roundtrip(env):
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="World", scope="personal",
    )
    w = await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="regions/valencia", body="Capital of green.",
        writer_member_id="alice", title="Valencia",
        page_type="note", state="drafted",
    )
    assert w.ok
    assert w.extra["is_new"] is True
    assert w.extra["state"] == "drafted"

    r = await svc.page_read(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="regions/valencia",
    )
    assert r.ok
    assert r.extra["frontmatter"]["title"] == "Valencia"
    assert "Capital of green." in r.extra["body"]


async def test_page_write_version_retention(env):
    svc, _, tmp = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="n", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="v1", writer_member_id="alice", page_type="note", state="drafted",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="v2", writer_member_id="alice", page_type="note", state="current",
    )
    # A .v1.md version file should exist alongside p.md.
    from kernos.kernel.canvas import canvas_dir
    root = canvas_dir(str(tmp), INSTANCE, c.canvas_id)
    versions = list(root.glob("p.v*.md"))
    assert len(versions) == 1


async def test_state_change_detection(env):
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="n", scope="personal",
    )
    w1 = await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="x", writer_member_id="alice", page_type="note", state="drafted",
    )
    # First write: state_changed=True because prev_state was empty.
    assert w1.extra["state_changed"] is True
    w2 = await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="y", writer_member_id="alice", page_type="note", state="drafted",
    )
    assert w2.extra["state_changed"] is False
    assert w2.extra["prev_state"] == "drafted"
    w3 = await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="z", writer_member_id="alice", page_type="note", state="current",
    )
    assert w3.extra["state_changed"] is True
    assert w3.extra["prev_state"] == "drafted"


async def test_page_list_excludes_version_files(env):
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="n", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="v1", writer_member_id="alice",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="p",
        body="v2", writer_member_id="alice",
    )
    pages = await svc.page_list(instance_id=INSTANCE, canvas_id=c.canvas_id)
    paths = [p["path"] for p in pages]
    # index.md + p.md — NOT p.v1.md
    assert "index.md" in paths
    assert "p.md" in paths
    assert not any("v1" in p for p in paths)


async def test_page_search_ranks_by_match_count(env):
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="n", scope="personal",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="few",
        body="valencia", writer_member_id="alice",
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="many",
        body="valencia valencia valencia", writer_member_id="alice",
    )
    hits = await svc.page_search(
        instance_id=INSTANCE, canvas_ids=[c.canvas_id], query="valencia",
    )
    assert hits[0]["path"] == "many.md"
    assert hits[0]["matches"] >= hits[-1]["matches"]


async def test_page_search_finds_pages_via_token_overlap(env):
    """PAGE-SEARCH-TOKEN-OVERLAP-V1 (2026-05-07): the agent observed
    that ``"Canvas docs"`` returned zero hits against pages that
    clearly mention canvas — the old implementation was strict
    substring match. Now token-overlap finds near-misses while
    whole-phrase matches still win on rank."""
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="n", scope="personal",
    )
    # Page A: scattered tokens, no exact phrase.
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="scattered",
        body="The canvas system documentation lives here.",
        writer_member_id="alice",
    )
    # Page B: contains the exact phrase.
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id, page_slug="exact",
        body="See the canvas docs for full details.",
        writer_member_id="alice",
    )
    hits = await svc.page_search(
        instance_id=INSTANCE, canvas_ids=[c.canvas_id], query="canvas docs",
    )
    paths = {h["path"] for h in hits}
    # Both pages are returned (strict substring would have dropped A).
    assert "scattered.md" in paths
    assert "exact.md" in paths
    # Whole-phrase match still wins ranking.
    assert hits[0]["path"] == "exact.md"


async def test_frontmatter_parse_and_serialize_roundtrip():
    original = {"title": "Valencia", "type": "note", "watchers": ["alice"]}
    body = "Body text.\n"
    text = serialize_frontmatter(original, body)
    assert text.startswith("---\n")
    fm, parsed_body = parse_frontmatter(text)
    assert fm["title"] == "Valencia"
    assert fm["type"] == "note"
    assert fm["watchers"] == ["alice"]
    assert parsed_body == body


async def test_no_frontmatter_returns_empty_dict():
    fm, body = parse_frontmatter("No frontmatter here.")
    assert fm == {}
    assert body == "No frontmatter here."


async def test_page_escape_prevention(env):
    svc, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="n", scope="personal",
    )
    r = await svc.page_write(
        instance_id=INSTANCE, canvas_id=c.canvas_id,
        page_slug="../../../etc/passwd", body="x", writer_member_id="alice",
    )
    assert not r.ok
    assert "slug" in r.error.lower() or "escape" in r.error.lower()


async def test_list_for_member_filters_by_access(env):
    svc, idb, _ = env
    await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="alices", scope="personal",
    )
    await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="team", scope="team",
    )
    await svc.create(
        instance_id=INSTANCE, creator_member_id="alice",
        name="shared", scope="specific", members=["bob"],
    )
    alice_list = await svc.list_for_member(member_id="alice")
    bob_list = await svc.list_for_member(member_id="bob")
    # Alice sees all three (creator of each).
    assert len(alice_list) == 3
    # Bob sees team + specific-that-includes-him.
    names = sorted(c["name"] for c in bob_list)
    assert "team" in names
    assert "shared" in names
    assert "alices" not in names
