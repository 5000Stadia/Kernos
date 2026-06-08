"""Tests for RetrievalService.search_structured + uniform disclosure gate.

Covers C1 of COHORT-ADAPT-MEMORY: the structured retrieval surface
shipped alongside the existing search() text path. Both paths share
the collect+policy+rank+budget-shape pipeline; this file exercises
the structured shape and the uniform-disclosure-gate behavior across
every payload source.

Existing search() test surface (tests/test_retrieval.py, 55 cases)
verified separately to keep passing without modification.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.entities import EntityNode, IdentityEdge
from kernos.kernel.retrieval import (
    ArchiveMatch,
    EntityMatch,
    KnowledgeMatch,
    RetrievalService,
    RetrievalSnapshot,
    SNAPSHOT_TOP_ENTITIES,
    SNAPSHOT_TOP_KNOWLEDGE,
)
from kernos.kernel.state import KnowledgeEntry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _knowledge(
    *,
    id: str,
    content: str = "memo",
    owner_member_id: str = "m-owner",
    sensitivity: str = "open",
    context_space: str = "default",
    confidence: str = "stated",
    reinforcement_count: int = 1,
    created_at: str | None = None,
) -> KnowledgeEntry:
    ts = created_at or datetime.now(timezone.utc).isoformat()
    return KnowledgeEntry(
        id=id,
        instance_id="i-1",
        subject="topic",
        content=content,
        owner_member_id=owner_member_id,
        sensitivity=sensitivity,
        context_space=context_space,
        confidence=confidence,
        reinforcement_count=reinforcement_count,
        created_at=ts,
        last_referenced=ts,
        category="fact",
        source_event_id="evt-1",
        source_description="test fixture",
        tags=[],
        active=True,
    )


def _entity(
    *,
    id: str,
    name: str = "Alice",
    knowledge_ids: list[str] | None = None,
    context_space: str = "default",
) -> EntityNode:
    return EntityNode(
        id=id,
        instance_id="i-1",
        canonical_name=name,
        aliases=[],
        entity_type="person",
        context_space=context_space,
        knowledge_entry_ids=knowledge_ids or [],
        active=True,
    )


def _service(
    *,
    knowledge_entries: list[KnowledgeEntry] | None = None,
    entities: list[EntityNode] | None = None,
    identity_edges: list[IdentityEdge] | None = None,
    embedding_succeeds: bool = True,
    archive_text: str | None = None,
) -> RetrievalService:
    """Build a RetrievalService stitched up against MagicMock state."""
    state = MagicMock()
    state.query_knowledge = AsyncMock(return_value=knowledge_entries or [])
    state.query_entity_nodes = AsyncMock(return_value=entities or [])

    edges = identity_edges or []
    state.query_identity_edges = AsyncMock(side_effect=lambda iid, eid: [
        e for e in edges if eid in (e.source_id, e.target_id)
    ])
    state.get_entity_node = AsyncMock(side_effect=lambda iid, nid: next(
        (e for e in (entities or []) if e.id == nid), None,
    ))
    state.get_knowledge_entry = AsyncMock(side_effect=lambda iid, eid: next(
        (k for k in (knowledge_entries or []) if k.id == eid), None,
    ))
    state.get_space = AsyncMock(return_value=None)
    state.get_context_space = AsyncMock(return_value=None)

    embeddings = MagicMock()
    if embedding_succeeds:
        embeddings.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
    else:
        embeddings.embed = AsyncMock(side_effect=RuntimeError("embed down"))

    embedding_store = MagicMock()
    # Always return a vector that exceeds the similarity threshold (0.65).
    embedding_store.get = AsyncMock(return_value=[0.1, 0.2, 0.3])

    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value="extracted text")
    reasoning._trigger_store = None
    reasoning._registry = None

    compaction = MagicMock()
    compaction.load_index = AsyncMock(return_value=archive_text)
    compaction.load_archive = AsyncMock(return_value=archive_text)

    svc = RetrievalService(
        state=state,
        embedding_service=embeddings,
        embedding_store=embedding_store,
        reasoning=reasoning,
        compaction=compaction,
    )
    return svc


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------


def test_retrieval_snapshot_is_frozen():
    snap = RetrievalSnapshot(
        knowledge=(),
        entities=(),
        archive=None,
        maybe_same_as=(),
        state_intercept=None,
        source="normal",
        query="q",
        scope_chain=(),
        retrieval_attempted=True,
        truncated=False,
    )
    with pytest.raises((AttributeError, Exception)):
        snap.query = "tampered"  # type: ignore[misc]


def test_knowledge_match_carries_quality_score_and_similarity():
    km = KnowledgeMatch(
        entry_id="k1",
        content="hello",
        authored_by="m-1",
        created_at="2026-01-01T00:00:00+00:00",
        quality_score=0.87,
        similarity=0.91,
        source_space_id="default",
    )
    assert km.quality_score == 0.87
    assert km.similarity == 0.91


# ---------------------------------------------------------------------------
# Empty / state-intercept / embedding-failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_structured_empty_state_returns_empty_snapshot():
    svc = _service()
    snap = await svc.search_structured(
        instance_id="i-1",
        query="something specific",
        active_space_id="default",
    )
    assert isinstance(snap, RetrievalSnapshot)
    assert snap.knowledge == ()
    assert snap.entities == ()
    assert snap.archive is None
    assert snap.state_intercept is None
    assert snap.source == "normal"
    assert snap.retrieval_attempted is True


@pytest.mark.asyncio
async def test_search_structured_embedding_failure_marks_retrieval_attempted_false():
    svc = _service(embedding_succeeds=False)
    snap = await svc.search_structured(
        instance_id="i-1",
        query="any query",
        active_space_id="default",
    )
    assert snap.retrieval_attempted is False
    assert snap.knowledge == ()
    assert snap.source == "normal"


@pytest.mark.asyncio
async def test_search_structured_state_intercept_short_circuits():
    svc = _service()
    # Mock build_user_truth_view to return a non-empty state view.
    from kernos.kernel import introspection
    original = introspection.build_user_truth_view

    async def fake_view(*_a, **_kw):
        return "## Active Preferences\n- foo: bar"

    introspection.build_user_truth_view = fake_view  # type: ignore[assignment]
    try:
        snap = await svc.search_structured(
            instance_id="i-1",
            query="what notification preference do I have?",
            active_space_id="default",
        )
    finally:
        introspection.build_user_truth_view = original  # type: ignore[assignment]

    assert snap.source == "state_intercept"
    assert snap.state_intercept is not None
    assert "Structured state" in snap.state_intercept
    # Knowledge / entities / archive empty per Section 2b
    assert snap.knowledge == ()
    assert snap.entities == ()
    assert snap.archive is None


@pytest.mark.asyncio
async def test_search_structured_unexpected_error_propagates():
    """Per the design review edit #6: only embedding failure swallows. Other errors
    (e.g., state-store raising mid-query) bubble to the runner."""
    svc = _service()
    svc.state.query_knowledge = AsyncMock(
        side_effect=RuntimeError("DB exploded")
    )
    with pytest.raises(RuntimeError, match="DB exploded"):
        await svc.search_structured(
            instance_id="i-1",
            query="q",
            active_space_id="default",
        )


# ---------------------------------------------------------------------------
# include_archives gating (the design review edit #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_structured_does_not_invoke_archive_when_excluded():
    svc = _service(archive_text="this would otherwise extract")
    snap = await svc.search_structured(
        instance_id="i-1",
        query="ancient context",
        active_space_id="default",
        include_archives=False,
    )
    # _search_archives invokes reasoning.complete_simple twice when
    # archives match. With include_archives=False, that path is
    # never entered.
    assert snap.archive is None
    svc.reasoning.complete_simple.assert_not_called()


@pytest.mark.asyncio
async def test_search_structured_invokes_archive_when_included():
    svc = _service(archive_text="archive index match")
    snap = await svc.search_structured(
        instance_id="i-1",
        query="ancient context",
        active_space_id="default",
        include_archives=True,
    )
    # Archive search ran (Haiku may or may not produce a non-"none"
    # extract depending on the mock; the key invariant is that it's
    # gated to run only when include_archives=True).
    assert svc.reasoning.complete_simple.called


# ---------------------------------------------------------------------------
# Uniform disclosure gate — entity-linked knowledge (the design review edit #4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entity_resolution_filters_linked_knowledge_by_member():
    """Entity matches; one linked knowledge owned by another member
    should be gated out. The entity surfaces but knowledge_count
    reflects only visible linked entries."""
    visible = _knowledge(
        id="k-mine",
        owner_member_id="m-active",
        content="my note about Alice",
    )
    gated = _knowledge(
        id="k-other",
        owner_member_id="m-other",
        sensitivity="personal",
        content="other member's note about Alice",
    )
    alice = _entity(
        id="e-alice",
        name="Alice",
        knowledge_ids=["k-mine", "k-other"],
    )
    svc = _service(
        knowledge_entries=[visible, gated],
        entities=[alice],
    )

    instance_db = MagicMock()
    instance_db.list_permissions_for = AsyncMock(return_value={})

    snap = await svc.search_structured(
        instance_id="i-1",
        query="alice",
        active_space_id="default",
        requesting_member_id="m-active",
        instance_db=instance_db,
    )

    # Entity is surfaced; linked knowledge filtered.
    assert len(snap.entities) == 1
    em = snap.entities[0]
    assert em.canonical_name == "Alice"
    assert em.knowledge_count == 1
    assert em.linked_knowledge_ids == ("k-mine",)


@pytest.mark.asyncio
async def test_search_structured_returns_top_n_with_truncation_flag():
    """Budget-shape: 6 matching knowledge entries → top 5 returned;
    truncated flag set."""
    entries = [
        _knowledge(
            id=f"k-{i}",
            content=f"content {i}",
            owner_member_id="m-active",
            confidence="stated",
            reinforcement_count=10,
        )
        for i in range(6)
    ]
    svc = _service(knowledge_entries=entries)

    snap = await svc.search_structured(
        instance_id="i-1",
        query="anything",
        active_space_id="default",
    )
    assert len(snap.knowledge) == SNAPSHOT_TOP_KNOWLEDGE
    assert snap.truncated is True


@pytest.mark.asyncio
async def test_search_structured_no_truncation_when_below_top_n():
    entries = [
        _knowledge(id="k-1", owner_member_id="m-active"),
        _knowledge(id="k-2", owner_member_id="m-active"),
    ]
    svc = _service(knowledge_entries=entries)
    snap = await svc.search_structured(
        instance_id="i-1",
        query="anything",
        active_space_id="default",
    )
    assert len(snap.knowledge) == 2
    assert snap.truncated is False


# ---------------------------------------------------------------------------
# search() and search_structured(include_archives=True) consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_text_path_unchanged_for_simple_match():
    """The legacy text path keeps its existing behavior. We don't
    assert exact byte-identity to search_structured (different
    output shape) — just that search() still produces formatted
    output for a knowledge match."""
    entry = _knowledge(id="k1", owner_member_id="m-active", content="hello")
    svc = _service(knowledge_entries=[entry])
    text = await svc.search(
        instance_id="i-1",
        query="hello",
        active_space_id="default",
    )
    assert isinstance(text, str)
    assert "hello" in text


@pytest.mark.asyncio
async def test_search_and_search_structured_share_pipeline_for_matches():
    """Same query against the same fixture: the text path and the
    structured path both surface the same underlying knowledge.
    Different output shape; same pipeline."""
    entry = _knowledge(
        id="k-shared",
        owner_member_id="m-active",
        content="shared knowledge piece",
    )
    svc1 = _service(knowledge_entries=[entry])
    text = await svc1.search(
        instance_id="i-1",
        query="shared knowledge",
        active_space_id="default",
    )
    svc2 = _service(knowledge_entries=[entry])
    snap = await svc2.search_structured(
        instance_id="i-1",
        query="shared knowledge",
        active_space_id="default",
        include_archives=True,
    )
    assert "shared knowledge piece" in text
    assert any(km.entry_id == "k-shared" for km in snap.knowledge)


# ---------------------------------------------------------------------------
# Source provenance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_path_uses_source_normal():
    svc = _service()
    snap = await svc.search_structured(
        instance_id="i-1",
        query="anything",
        active_space_id="default",
    )
    assert snap.source == "normal"


# ---------------------------------------------------------------------------
# ② lexical fallback is scoped to NO-EMBEDDING entries only — it must NOT
# override the semantic threshold for embedded entries (Codex review).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_embedding_full_lexical_rescue():
    from unittest.mock import AsyncMock
    # Legacy entry with NO embedding → full lexical fallback (content + key)
    # is the only signal (the live cerulean case).
    legacy = _knowledge(
        id="know_legacy", content="Kabe's favorite test color is cerulean."
    )
    legacy.subject = "favorite_test_color"
    svc = _service(knowledge_entries=[legacy])
    svc.embeddings.embed = AsyncMock(return_value=[1.0, 0.0, 0.0])
    svc.embedding_store.get = AsyncMock(return_value=None)  # not embedded
    results = await svc._search_knowledge(
        "i-1", "what is my favorite test color", [1.0, 0.0, 0.0], "default",
    )
    assert any(r.entry.id == "know_legacy" for r in results)


@pytest.mark.asyncio
async def test_embedded_subthreshold_rescued_by_subject_key_not_content():
    from unittest.mock import AsyncMock
    # Embedded entry whose CONTENT embedding misses semantically, but the query
    # names its SUBJECT key → rescued (the embedding is on content only).
    keyed = _knowledge(id="know_keyed", content="cerulean")
    keyed.subject = "favorite_test_color"
    svc = _service(knowledge_entries=[keyed])
    svc.embeddings.embed = AsyncMock(return_value=[1.0, 0.0, 0.0])
    svc.embedding_store.get = AsyncMock(return_value=[0.0, 1.0, 0.0])  # cosine 0
    res = await svc._search_knowledge(
        "i-1", "what is my favorite test color", [1.0, 0.0, 0.0], "default",
    )
    assert any(r.entry.id == "know_keyed" for r in res)

    # Embedded entry whose SUBJECT is unrelated but whose CONTENT shares a token
    # with the query → NOT rescued (content overlap must not bypass the
    # semantic threshold).
    noisy = _knowledge(id="know_noisy", content="the payment was late")
    noisy.subject = "topic"
    svc2 = _service(knowledge_entries=[noisy])
    svc2.embeddings.embed = AsyncMock(return_value=[1.0, 0.0, 0.0])
    svc2.embedding_store.get = AsyncMock(return_value=[0.0, 1.0, 0.0])  # cosine 0
    res2 = await svc2._search_knowledge(
        "i-1", "payment", [1.0, 0.0, 0.0], "default",
    )
    assert not any(r.entry.id == "know_noisy" for r in res2)

    # Generic one-token subject (fact-harvest uses subject="user") must NOT be
    # rescued just because the query contains that word — it would flood every
    # general fact and reintroduce the bypass (Codex review).
    generic = _knowledge(id="know_generic", content="something unrelated")
    generic.subject = "user"
    svc3 = _service(knowledge_entries=[generic])
    svc3.embeddings.embed = AsyncMock(return_value=[1.0, 0.0, 0.0])
    svc3.embedding_store.get = AsyncMock(return_value=[0.0, 1.0, 0.0])  # cosine 0
    res3 = await svc3._search_knowledge(
        "i-1", "user timezone", [1.0, 0.0, 0.0], "default",
    )
    assert not any(r.entry.id == "know_generic" for r in res3)
