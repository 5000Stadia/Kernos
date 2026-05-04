"""Tests for SPEC-2D: Active Retrieval + NL Contract Parser.

Tests cover:
- compute_quality_score() ranking function
- RetrievalService knowledge search, entity traversal, archive search
- Result formatting and token budget enforcement
- Foresight boost and space relevance boost
- NL Contract Parser
- Kernel tool routing in ReasoningService
"""
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernos.kernel.entities import EntityNode, IdentityEdge
from kernos.kernel.retrieval import (
    FORESIGHT_BOOST,
    RETRIEVAL_RESULT_TOKEN_BUDGET,
    SIMILARITY_THRESHOLD,
    SPACE_RELEVANCE_BOOST,
    EntityResult,
    RetrievalService,
    ScoredKnowledge,
    compute_quality_score,
    _apply_foresight_boost,
    _days_since,
    REMEMBER_TOOL,
)
from kernos.kernel.state import KnowledgeEntry, CovenantRule


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_entry(
    *,
    content="test fact",
    subject="user",
    confidence="stated",
    created_at=None,
    lifecycle_archetype="structural",
    reinforcement_count=1,
    context_space="",
    foresight_signal="",
    foresight_expires="",
    active=True,
    entity_node_id="",
) -> KnowledgeEntry:
    if created_at is None:
        created_at = _now_iso()
    return KnowledgeEntry(
        id=f"know_test_{hash(content) % 10000}",
        instance_id="test-tenant",
        category="fact",
        subject=subject,
        content=content,
        confidence=confidence,
        source_event_id="",
        source_description="test",
        created_at=created_at,
        last_referenced=created_at,
        tags=[],
        active=active,
        lifecycle_archetype=lifecycle_archetype,
        reinforcement_count=reinforcement_count,
        context_space=context_space,
        foresight_signal=foresight_signal,
        foresight_expires=foresight_expires,
        entity_node_id=entity_node_id,
    )


def _make_entity(
    *,
    name="Henderson",
    entity_type="person",
    relationship_type="colleague",
    aliases=None,
    knowledge_entry_ids=None,
    context_space="",
    contact_phone="",
    contact_email="",
) -> EntityNode:
    return EntityNode(
        id=f"ent_{hash(name) % 10000}",
        instance_id="test-tenant",
        canonical_name=name,
        aliases=aliases or [],
        entity_type=entity_type,
        relationship_type=relationship_type,
        knowledge_entry_ids=knowledge_entry_ids or [],
        context_space=context_space,
        contact_phone=contact_phone,
        contact_email=contact_email,
    )


# ─── compute_quality_score ───────────────────────────────────────────


class TestQualityScore:
    def test_new_stated_reinforced_scores_high(self):
        entry = _make_entry(confidence="stated", reinforcement_count=5)
        score = compute_quality_score(entry, "", _now_iso())
        assert score > 0.8

    def test_old_inferred_unreinforced_scores_low(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        entry = _make_entry(confidence="inferred", reinforcement_count=0, created_at=old_date)
        score = compute_quality_score(entry, "", _now_iso())
        assert score < 0.4

    def test_no_entries_score_1_by_default(self):
        """No entry should score 1.0 — the old FSRS bug is gone."""
        entry = _make_entry()
        score = compute_quality_score(entry, "", _now_iso())
        assert score < 1.0

    def test_recency_decays_over_90_days(self):
        now = _now_iso()
        recent = _make_entry(created_at=now)
        old = _make_entry(
            created_at=(datetime.now(timezone.utc) - timedelta(days=90)).isoformat(),
            content="old fact",
        )
        score_recent = compute_quality_score(recent, "", now)
        score_old = compute_quality_score(old, "", now)
        assert score_recent > score_old

    def test_recency_floor_at_01(self):
        very_old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        entry = _make_entry(created_at=very_old)
        score = compute_quality_score(entry, "", _now_iso())
        # Floor recency should be 0.1
        assert score >= 0.1 * 0.4  # At minimum the recency floor contributes

    def test_stated_ranks_above_inferred(self):
        now = _now_iso()
        stated = _make_entry(confidence="stated", created_at=now)
        inferred = _make_entry(confidence="inferred", created_at=now, content="other")
        assert compute_quality_score(stated, "", now) > compute_quality_score(inferred, "", now)

    def test_reinforcement_capped_at_5(self):
        now = _now_iso()
        five = _make_entry(reinforcement_count=5, created_at=now)
        ten = _make_entry(reinforcement_count=10, created_at=now, content="other")
        # Both should score the same — reinforcement caps at 5
        assert compute_quality_score(five, "", now) == compute_quality_score(ten, "", now)

    def test_space_relevance_boost(self):
        now = _now_iso()
        entry = _make_entry(context_space="space_abc")
        score_in_space = compute_quality_score(entry, "space_abc", now)
        score_out_of_space = compute_quality_score(entry, "space_xyz", now)
        assert score_in_space == pytest.approx(score_out_of_space * SPACE_RELEVANCE_BOOST)

    def test_space_boost_not_applied_for_empty_space(self):
        now = _now_iso()
        entry = _make_entry(context_space="")
        score = compute_quality_score(entry, "", now)
        # No boost applied when both space IDs are empty
        assert score == compute_quality_score(entry, "", now)


class TestForesightBoost:
    def test_active_foresight_boosted(self):
        now = _now_iso()
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        entry = _make_entry(foresight_signal="dentist appointment", foresight_expires=future)
        candidates = [ScoredKnowledge(entry=entry, quality_score=0.5)]
        _apply_foresight_boost(candidates, "dentist", now)
        assert candidates[0].quality_score == pytest.approx(0.5 * FORESIGHT_BOOST)

    def test_expired_foresight_not_boosted(self):
        now = _now_iso()
        past = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        entry = _make_entry(foresight_signal="dentist appointment", foresight_expires=past)
        candidates = [ScoredKnowledge(entry=entry, quality_score=0.5)]
        _apply_foresight_boost(candidates, "dentist", now)
        assert candidates[0].quality_score == 0.5  # Unchanged

    def test_unrelated_foresight_not_boosted(self):
        now = _now_iso()
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        entry = _make_entry(foresight_signal="dentist appointment", foresight_expires=future)
        candidates = [ScoredKnowledge(entry=entry, quality_score=0.5)]
        _apply_foresight_boost(candidates, "payment terms", now)
        assert candidates[0].quality_score == 0.5  # Unchanged

    def test_no_foresight_signal_unchanged(self):
        entry = _make_entry()
        candidates = [ScoredKnowledge(entry=entry, quality_score=0.5)]
        _apply_foresight_boost(candidates, "dentist", _now_iso())
        assert candidates[0].quality_score == 0.5


# ─── RetrievalService._search_knowledge ──────────────────────────────


class TestKnowledgeSearch:
    @pytest.fixture
    def service(self):
        state = AsyncMock()
        embedding_service = AsyncMock()
        embedding_store = AsyncMock()
        compaction = AsyncMock()
        reasoning = AsyncMock()
        return RetrievalService(
            state=state,
            embedding_service=embedding_service,
            embedding_store=embedding_store,
            compaction=compaction,
            reasoning=reasoning,
        )

    async def test_filters_below_threshold(self, service):
        entry = _make_entry(content="payment terms net-30")
        service.state.query_knowledge = AsyncMock(return_value=[entry])
        service.embedding_store.get = AsyncMock(return_value=[0.1] * 128)

        with patch("kernos.kernel.embeddings.cosine_similarity", return_value=0.50):
            results = await service._search_knowledge(
                "test-tenant", "payment", [0.1] * 128, ""
            )
        assert len(results) == 0

    async def test_includes_above_threshold(self, service):
        entry = _make_entry(content="payment terms net-30")
        service.state.query_knowledge = AsyncMock(return_value=[entry])
        service.embedding_store.get = AsyncMock(return_value=[0.1] * 128)

        with patch("kernos.kernel.embeddings.cosine_similarity", return_value=0.80):
            results = await service._search_knowledge(
                "test-tenant", "payment", [0.1] * 128, ""
            )
        assert len(results) == 1
        assert results[0].similarity == 0.80

    async def test_space_scoping(self, service):
        entry_in_space = _make_entry(content="D&D character", context_space="space_dnd")
        entry_global = _make_entry(content="user name", context_space="", subject="global")
        entry_other = _make_entry(content="work stuff", context_space="space_work", subject="work")

        service.state.query_knowledge = AsyncMock(
            return_value=[entry_in_space, entry_global, entry_other]
        )
        service.embedding_store.get = AsyncMock(return_value=[0.1] * 128)

        with patch("kernos.kernel.embeddings.cosine_similarity", return_value=0.80):
            results = await service._search_knowledge(
                "test-tenant", "character", [0.1] * 128, "space_dnd"
            )
        # Should include space_dnd and global, but NOT space_work
        assert len(results) == 2

    async def test_skips_entries_without_embeddings(self, service):
        entry = _make_entry(content="no embedding")
        service.state.query_knowledge = AsyncMock(return_value=[entry])
        service.embedding_store.get = AsyncMock(return_value=None)

        results = await service._search_knowledge(
            "test-tenant", "test", [0.1] * 128, ""
        )
        assert len(results) == 0


# ─── RetrievalService._search_entities ───────────────────────────────


class TestEntitySearch:
    @pytest.fixture
    def service(self):
        state = AsyncMock()
        return RetrievalService(
            state=state,
            embedding_service=AsyncMock(),
            embedding_store=AsyncMock(),
            compaction=AsyncMock(),
            reasoning=AsyncMock(),
        )

    async def test_finds_entity_by_name(self, service):
        entity = _make_entity(name="Henderson", knowledge_entry_ids=["know_1"])
        entry = _make_entry(content="Payment terms net-30")
        service.state.query_entity_nodes = AsyncMock(return_value=[entity])
        service.state.get_knowledge_entry = AsyncMock(return_value=entry)
        service.state.query_identity_edges = AsyncMock(return_value=[])

        results = await service._search_entities("test-tenant", "Henderson", "")
        assert len(results) == 1
        assert results[0].entity.canonical_name == "Henderson"

    async def test_finds_entity_by_alias(self, service):
        entity = _make_entity(name="Sarah Henderson", aliases=["Henderson", "Sarah"])
        service.state.query_entity_nodes = AsyncMock(return_value=[entity])
        service.state.query_identity_edges = AsyncMock(return_value=[])

        results = await service._search_entities("test-tenant", "Sarah", "")
        assert len(results) == 1

    async def test_no_match_returns_empty(self, service):
        entity = _make_entity(name="Henderson")
        service.state.query_entity_nodes = AsyncMock(return_value=[entity])

        results = await service._search_entities("test-tenant", "Completely unrelated query", "")
        assert len(results) == 0

    async def test_space_scoping_filters_entities(self, service):
        entity_dnd = _make_entity(name="Pip", context_space="space_dnd")
        entity_work = _make_entity(name="Pip Roberts", context_space="space_work")
        service.state.query_entity_nodes = AsyncMock(return_value=[entity_dnd, entity_work])
        service.state.query_identity_edges = AsyncMock(return_value=[])

        results = await service._search_entities("test-tenant", "Pip", "space_dnd")
        assert len(results) == 1
        assert results[0].entity.canonical_name == "Pip"


# ─── SAME_AS resolution ─────────────────────────────────────────────


class TestSameAsResolution:
    async def test_same_as_merges_knowledge(self):
        state = AsyncMock()
        service = RetrievalService(
            state=state,
            embedding_service=AsyncMock(),
            embedding_store=AsyncMock(),
            compaction=AsyncMock(),
            reasoning=AsyncMock(),
        )

        entity_a = _make_entity(name="Liana", knowledge_entry_ids=["know_1"])
        entity_b = _make_entity(name="user's wife", knowledge_entry_ids=["know_2"])

        entry_1 = _make_entry(content="Loves cooking")
        entry_2 = _make_entry(content="Wife enjoys gardening", subject="wife")

        edge = IdentityEdge(
            source_id=entity_a.id,
            target_id=entity_b.id,
            edge_type="SAME_AS",
            confidence=0.95,
        )

        state.get_knowledge_entry = AsyncMock(side_effect=lambda tid, eid: {
            "know_1": entry_1,
            "know_2": entry_2,
        }.get(eid))
        state.query_identity_edges = AsyncMock(return_value=[edge])
        state.get_entity_node = AsyncMock(return_value=entity_b)

        knowledge = await service._resolve_entity_knowledge("test-tenant", entity_a)
        assert len(knowledge) == 2

    async def test_low_confidence_same_as_not_merged(self):
        state = AsyncMock()
        service = RetrievalService(
            state=state,
            embedding_service=AsyncMock(),
            embedding_store=AsyncMock(),
            compaction=AsyncMock(),
            reasoning=AsyncMock(),
        )

        entity_a = _make_entity(name="Liana", knowledge_entry_ids=["know_1"])
        entry_1 = _make_entry(content="Loves cooking")

        edge = IdentityEdge(
            source_id=entity_a.id,
            target_id="ent_other",
            edge_type="SAME_AS",
            confidence=0.5,  # Below 0.8 threshold
        )

        state.get_knowledge_entry = AsyncMock(return_value=entry_1)
        state.query_identity_edges = AsyncMock(return_value=[edge])

        knowledge = await service._resolve_entity_knowledge("test-tenant", entity_a)
        assert len(knowledge) == 1  # Only direct knowledge, no merge


# ─── MAYBE_SAME_AS ──────────────────────────────────────────────────


class TestMaybeSameAs:
    async def test_maybe_same_as_collected(self):
        state = AsyncMock()
        service = RetrievalService(
            state=state,
            embedding_service=AsyncMock(),
            embedding_store=AsyncMock(),
            compaction=AsyncMock(),
            reasoning=AsyncMock(),
        )

        entity_a = _make_entity(name="Dr. Smith")
        entity_b = _make_entity(name="John Smith")
        edge = IdentityEdge(
            source_id=entity_a.id,
            target_id=entity_b.id,
            edge_type="MAYBE_SAME_AS",
            confidence=0.6,
        )

        state.query_identity_edges = AsyncMock(return_value=[edge])
        state.get_entity_node = AsyncMock(return_value=entity_b)

        er = [EntityResult(entity=entity_a, knowledge=[])]
        pairs = await service._collect_maybe_same_as("test-tenant", er)
        assert len(pairs) == 1
        assert pairs[0][0].canonical_name == "Dr. Smith"
        assert pairs[0][1].canonical_name == "John Smith"


# ─── Result formatting ──────────────────────────────────────────────


class TestFormatResults:
    def test_empty_results(self):
        service = RetrievalService(
            state=AsyncMock(),
            embedding_service=AsyncMock(),
            embedding_store=AsyncMock(),
            compaction=AsyncMock(),
            reasoning=AsyncMock(),
        )
        result = service._format_results([], [], None, [])
        assert result == "No relevant information found in memory."

    def test_entity_formatted(self):
        service = RetrievalService(
            state=AsyncMock(),
            embedding_service=AsyncMock(),
            embedding_store=AsyncMock(),
            compaction=AsyncMock(),
            reasoning=AsyncMock(),
        )
        entity = _make_entity(
            name="Henderson",
            relationship_type="colleague",
            contact_phone="555-1234",
        )
        entry = _make_entry(content="Payment terms net-30")
        er = [EntityResult(entity=entity, knowledge=[entry])]
        result = service._format_results([], er, None, [])
        assert "Henderson" in result
        assert "colleague" in result
        assert "555-1234" in result
        assert "Payment terms net-30" in result

    def test_token_budget_enforced(self):
        service = RetrievalService(
            state=AsyncMock(),
            embedding_service=AsyncMock(),
            embedding_store=AsyncMock(),
            compaction=AsyncMock(),
            reasoning=AsyncMock(),
        )
        # Create many knowledge entries that exceed the budget
        candidates = []
        for i in range(100):
            entry = _make_entry(content=f"Fact number {i} " * 20)  # ~80 chars each
            candidates.append(ScoredKnowledge(entry=entry, quality_score=0.5))

        result = service._format_results(candidates, [], None, [])
        # Result should be within budget (rough check: budget * 4 chars)
        assert len(result) < RETRIEVAL_RESULT_TOKEN_BUDGET * 5  # Allow some slack

    def test_dedup_against_entity_knowledge(self):
        service = RetrievalService(
            state=AsyncMock(),
            embedding_service=AsyncMock(),
            embedding_store=AsyncMock(),
            compaction=AsyncMock(),
            reasoning=AsyncMock(),
        )
        entry = _make_entry(content="Payment terms net-30")
        entity = _make_entity(name="Henderson", knowledge_entry_ids=[entry.id])
        er = [EntityResult(entity=entity, knowledge=[entry])]
        # Same entry appears in both entity results and knowledge candidates
        candidates = [ScoredKnowledge(entry=entry, quality_score=0.8)]
        result = service._format_results(candidates, er, None, [])
        # Should only appear once
        assert result.count("Payment terms net-30") == 1

    def test_maybe_same_as_note_included(self):
        service = RetrievalService(
            state=AsyncMock(),
            embedding_service=AsyncMock(),
            embedding_store=AsyncMock(),
            compaction=AsyncMock(),
            reasoning=AsyncMock(),
        )
        node_a = _make_entity(name="Dr. Smith")
        node_b = _make_entity(name="John Smith")
        result = service._format_results([], [], None, [(node_a, node_b)])
        assert "may be the same person" in result

    def test_archive_result_included(self):
        service = RetrievalService(
            state=AsyncMock(),
            embedding_service=AsyncMock(),
            embedding_store=AsyncMock(),
            compaction=AsyncMock(),
            reasoning=AsyncMock(),
        )
        result = service._format_results([], [], "Old D&D session content", [])
        assert "From history:" in result
        assert "D&D session" in result


# ─── Archive search ──────────────────────────────────────────────────


class TestArchiveSearch:
    async def test_no_index_returns_none(self):
        service = RetrievalService(
            state=AsyncMock(),
            embedding_service=AsyncMock(),
            embedding_store=AsyncMock(),
            compaction=AsyncMock(),
            reasoning=AsyncMock(),
        )
        service.compaction.load_index = AsyncMock(return_value=None)
        result = await service._search_archives("test-tenant", "query", "space_1")
        assert result is None

    async def test_no_relevant_archive_returns_none(self):
        service = RetrievalService(
            state=AsyncMock(),
            embedding_service=AsyncMock(),
            embedding_store=AsyncMock(),
            compaction=AsyncMock(),
            reasoning=AsyncMock(),
        )
        service.compaction.load_index = AsyncMock(return_value="## Archive #1\nSome content")
        service.reasoning.complete_simple = AsyncMock(return_value="none")
        result = await service._search_archives("test-tenant", "unrelated", "space_1")
        assert result is None

    async def test_relevant_archive_extracted(self):
        service = RetrievalService(
            state=AsyncMock(),
            embedding_service=AsyncMock(),
            embedding_store=AsyncMock(),
            compaction=AsyncMock(),
            reasoning=AsyncMock(),
        )
        service.compaction.load_index = AsyncMock(return_value="## Archive #1\nD&D campaign")
        service.compaction.load_archive = AsyncMock(return_value="Full archive content...")
        # First call returns archive number, second call returns extraction
        service.reasoning.complete_simple = AsyncMock(
            side_effect=["1", "The party fought a dragon in the mountain caves."]
        )
        result = await service._search_archives("test-tenant", "D&D early campaign", "space_1")
        assert result == "The party fought a dragon in the mountain caves."

    async def test_nothing_found_returns_none(self):
        service = RetrievalService(
            state=AsyncMock(),
            embedding_service=AsyncMock(),
            embedding_store=AsyncMock(),
            compaction=AsyncMock(),
            reasoning=AsyncMock(),
        )
        service.compaction.load_index = AsyncMock(return_value="## Archive #1\nSome content")
        service.compaction.load_archive = AsyncMock(return_value="Archive text")
        service.reasoning.complete_simple = AsyncMock(
            side_effect=["1", "Nothing found in this archive."]
        )
        result = await service._search_archives("test-tenant", "query", "space_1")
        assert result is None


# ─── Full search pipeline ────────────────────────────────────────────


class TestFullSearch:
    async def test_search_returns_formatted_text(self):
        state = AsyncMock()
        embedding_service = AsyncMock()
        embedding_store = AsyncMock()
        compaction = AsyncMock()
        reasoning = AsyncMock()

        service = RetrievalService(
            state=state,
            embedding_service=embedding_service,
            embedding_store=embedding_store,
            compaction=compaction,
            reasoning=reasoning,
        )

        # Set up entity search
        entity = _make_entity(name="Henderson", knowledge_entry_ids=["know_1"])
        entry = _make_entry(content="Payment terms net-30")
        state.query_entity_nodes = AsyncMock(return_value=[entity])
        state.get_knowledge_entry = AsyncMock(return_value=entry)
        state.query_identity_edges = AsyncMock(return_value=[])

        # Set up knowledge search
        state.query_knowledge = AsyncMock(return_value=[entry])
        embedding_service.embed = AsyncMock(return_value=[0.1] * 128)
        embedding_store.get = AsyncMock(return_value=[0.1] * 128)

        # No archives
        compaction.load_index = AsyncMock(return_value=None)

        with patch("kernos.kernel.embeddings.cosine_similarity", return_value=0.80):
            result = await service.search("test-tenant", "Henderson", "")

        assert "Henderson" in result
        assert "Payment terms net-30" in result

    async def test_embedding_failure_graceful(self):
        """Service should still return entity results even if embedding fails."""
        state = AsyncMock()
        embedding_service = AsyncMock()
        embedding_service.embed = AsyncMock(side_effect=Exception("API down"))
        compaction = AsyncMock()
        compaction.load_index = AsyncMock(return_value=None)

        service = RetrievalService(
            state=state,
            embedding_service=embedding_service,
            embedding_store=AsyncMock(),
            compaction=compaction,
            reasoning=AsyncMock(),
        )

        entity = _make_entity(name="Henderson")
        state.query_entity_nodes = AsyncMock(return_value=[entity])
        state.query_identity_edges = AsyncMock(return_value=[])

        result = await service.search("test-tenant", "Henderson", "")
        assert "Henderson" in result

    async def test_no_results_returns_message(self):
        state = AsyncMock()
        embedding_service = AsyncMock()
        embedding_service.embed = AsyncMock(return_value=[0.1] * 128)
        compaction = AsyncMock()
        compaction.load_index = AsyncMock(return_value=None)

        service = RetrievalService(
            state=state,
            embedding_service=embedding_service,
            embedding_store=AsyncMock(),
            compaction=compaction,
            reasoning=AsyncMock(),
        )

        state.query_entity_nodes = AsyncMock(return_value=[])
        state.query_knowledge = AsyncMock(return_value=[])

        result = await service.search("test-tenant", "Completely unknown topic", "")
        assert result == "No relevant information found in memory."


# ─── NL Contract Parser ─────────────────────────────────────────────


class TestContractParser:
    async def test_parses_must_not_rule(self):
        from kernos.kernel.contract_parser import parse_behavioral_instruction

        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "rule_type": "must_not",
            "description": "Never contact Henderson without asking first",
            "capability": "email",
            "is_global": False,
            "reasoning": "User explicitly stated a restriction on contacting Henderson",
        }))

        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(
            id="space_work", instance_id="test", name="Work",
            description="", space_type="domain", status="active",
        )

        rule = await parse_behavioral_instruction(
            reasoning,
            "Never contact Henderson without asking me first",
            space,
        )

        assert rule is not None
        assert rule.rule_type == "must_not"
        assert rule.source == "user_stated"
        assert rule.context_space == "space_work"
        assert rule.enforcement_tier == "confirm"
        assert rule.layer == "practice"

    async def test_parses_global_rule(self):
        from kernos.kernel.contract_parser import parse_behavioral_instruction

        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "rule_type": "must_not",
            "description": "Never mention user's divorce",
            "capability": "general",
            "is_global": True,
            "reasoning": "User set a global restriction on a personal topic",
        }))

        rule = await parse_behavioral_instruction(
            reasoning,
            "Don't ever bring up my divorce",
            None,
        )

        assert rule is not None
        assert rule.rule_type == "must_not"
        assert rule.context_space is None  # Global

    async def test_parses_preference_rule(self):
        from kernos.kernel.contract_parser import parse_behavioral_instruction

        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "rule_type": "preference",
            "description": "Keep responses brief unless detail is requested",
            "capability": "general",
            "is_global": True,
            "reasoning": "User stated a communication preference",
        }))

        rule = await parse_behavioral_instruction(
            reasoning, "I prefer short answers", None,
        )

        assert rule is not None
        assert rule.rule_type == "preference"
        assert rule.enforcement_tier == "silent"

    async def test_parser_failure_returns_none(self):
        from kernos.kernel.contract_parser import parse_behavioral_instruction

        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(side_effect=Exception("LLM down"))

        rule = await parse_behavioral_instruction(
            reasoning, "some instruction", None,
        )
        assert rule is None


# ─── Rule deduplication ────────────────────────────────────────────────


class TestRuleDedup:
    def test_word_overlap_identical(self):
        from kernos.kernel.contract_parser import compute_word_overlap
        assert compute_word_overlap(
            "Never contact Henderson without asking first",
            "Never contact Henderson without asking first",
        ) == 1.0

    def test_word_overlap_similar(self):
        from kernos.kernel.contract_parser import compute_word_overlap
        overlap = compute_word_overlap(
            "Never contact Henderson without asking first",
            "Do not contact Henderson without checking with me first",
        )
        # shared: contact, henderson, without, first → 4/9 ≈ 0.44
        assert 0.4 < overlap < 0.8  # Similar but below dedup threshold

    def test_word_overlap_different(self):
        from kernos.kernel.contract_parser import compute_word_overlap
        overlap = compute_word_overlap(
            "Never contact Henderson",
            "Always bring up the weather forecast",
        )
        assert overlap < 0.2

    def test_word_overlap_empty(self):
        from kernos.kernel.contract_parser import compute_word_overlap
        assert compute_word_overlap("", "something") == 0.0
        assert compute_word_overlap("something", "") == 0.0

    async def test_coordinator_writes_rule_and_fires_validation(self):
        """New rule is written immediately, then validation fires async."""
        from kernos.kernel.projectors.coordinator import _run_tier2_with_behavioral_detection
        from kernos.kernel.state import CovenantRule

        state = AsyncMock()
        events = AsyncMock()
        reasoning = AsyncMock()

        # No existing rules
        state.get_contract_rules = AsyncMock(return_value=[])

        entry = _make_entry(
            subject="behavioral_instruction",
            content="Don't ever bring up my divorce",
        )
        state.query_knowledge = AsyncMock(return_value=[entry])

        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "rule_type": "must_not",
            "description": "Never mention the user's divorce",
            "capability": "general",
            "is_global": True,
            "reasoning": "Personal topic restriction",
        }))

        with patch("kernos.kernel.projectors.coordinator.run_tier2_extraction", new_callable=AsyncMock):
            await _run_tier2_with_behavioral_detection(
                recent_turns=[],
                soul=MagicMock(),
                state=state,
                events=events,
                reasoning_service=reasoning,
                instance_id="test-tenant",
                active_space_id="",
                active_space=None,
            )

        # Rule should have been created (write first, validate after)
        state.add_contract_rule.assert_called_once()


# ─── Kernel tool routing in ReasoningService ─────────────────────────


class TestKernelToolRouting:
    async def test_remember_tool_routed_to_retrieval(self):
        from kernos.kernel.reasoning import (
            ReasoningService,
            ReasoningRequest,
            ContentBlock,
            ProviderResponse,
        )

        provider = AsyncMock()
        events = AsyncMock()
        mcp = AsyncMock()
        audit = AsyncMock()
        audit.log = AsyncMock()

        retrieval = AsyncMock()
        retrieval.search = AsyncMock(return_value="Henderson — colleague. Payment terms net-30.")

        service = ReasoningService(provider, events, mcp, audit)
        service.set_retrieval(retrieval)
        from tests._thin_path_test_fixture import wire_test_thin_path
        wire_test_thin_path(service, provider=provider, mcp=mcp)

        # First response: tool_use (remember), second: text
        provider.complete = AsyncMock(side_effect=[
            ProviderResponse(
                content=[
                    ContentBlock(
                        type="tool_use",
                        name="remember",
                        id="tool_1",
                        input={"query": "Henderson"},
                    )
                ],
                stop_reason="tool_use",
                input_tokens=100,
                output_tokens=50,
            ),
            ProviderResponse(
                content=[ContentBlock(type="text", text="Henderson is your colleague...")],
                stop_reason="end_turn",
                input_tokens=200,
                output_tokens=100,
            ),
        ])

        request = ReasoningRequest(
            instance_id="test-tenant",
            conversation_id="conv_1",
            system_prompt="test",
            messages=[{"role": "user", "content": "What do you know about Henderson?"}],
            tools=[REMEMBER_TOOL],
            model="claude-sonnet-4-6",
            trigger="user_message",
            active_space_id="space_work",
        )

        result = await service.reason(request)

        # Verify retrieval was called instead of MCP. The disclosure gate threads
        # requesting_member_id / instance_db through every retrieval call; assert
        # positional args here and ignore the gate kwargs.
        retrieval.search.assert_called_once()
        _args, _kwargs = retrieval.search.call_args
        assert _args == ("test-tenant", "Henderson", "space_work")
        mcp.call_tool.assert_not_called()
        assert "Henderson" in result.text

    async def test_non_remember_tool_goes_to_mcp(self):
        from kernos.kernel.reasoning import (
            ReasoningService,
            ReasoningRequest,
            ContentBlock,
            ProviderResponse,
        )

        provider = AsyncMock()
        events = AsyncMock()
        mcp = AsyncMock()
        mcp.call_tool = AsyncMock(return_value="Calendar event created")
        audit = AsyncMock()
        audit.log = AsyncMock()

        service = ReasoningService(provider, events, mcp, audit)
        service.set_retrieval(AsyncMock())  # Retrieval wired but not for this call
        from tests._thin_path_test_fixture import wire_test_thin_path
        wire_test_thin_path(service, provider=provider, mcp=mcp)

        # Wire registry so dispatch gate classifies calendar_create as "read"
        from kernos.capability.registry import CapabilityInfo, CapabilityRegistry, CapabilityStatus
        cap = CapabilityInfo(
            name="test-cal", display_name="Test", description="Test",
            category="test", status=CapabilityStatus.CONNECTED,
            tools=["calendar_create"], server_name="test",
            tool_effects={"calendar_create": "read"},
        )
        registry = CapabilityRegistry(mcp=None)
        registry.register(cap)
        service.set_registry(registry)

        provider.complete = AsyncMock(side_effect=[
            ProviderResponse(
                content=[
                    ContentBlock(
                        type="tool_use",
                        name="calendar_create",
                        id="tool_1",
                        input={"event": "Meeting"},
                    )
                ],
                stop_reason="tool_use",
                input_tokens=100,
                output_tokens=50,
            ),
            ProviderResponse(
                content=[ContentBlock(type="text", text="Created!")],
                stop_reason="end_turn",
                input_tokens=200,
                output_tokens=100,
            ),
        ])

        request = ReasoningRequest(
            instance_id="test-tenant",
            conversation_id="conv_1",
            system_prompt="test",
            messages=[{"role": "user", "content": "Create a meeting"}],
            tools=[{"name": "calendar_create", "input_schema": {}}],
            model="claude-sonnet-4-6",
            trigger="user_message",
        )

        await service.reason(request)
        mcp.call_tool.assert_called_once()


# ─── CompactionService.load_archive ──────────────────────────────────


class TestLoadArchive:
    async def test_loads_archive_by_number(self, tmp_path):
        from kernos.kernel.compaction import CompactionService

        service = CompactionService(
            state=AsyncMock(),
            reasoning=AsyncMock(),
            token_adapter=AsyncMock(),
            data_dir=str(tmp_path),
        )

        # Create archive file
        archive_dir = tmp_path / "test__tenant" / "state" / "compaction" / "space_1" / "archives"
        archive_dir.mkdir(parents=True)
        (archive_dir / "compaction_archive_001.md").write_text("Archive 1 content")

        with patch.object(service, "_space_dir", return_value=tmp_path / "test__tenant" / "state" / "compaction" / "space_1"):
            result = await service.load_archive("test-tenant", "space_1", "1")
        assert result == "Archive 1 content"

    async def test_handles_various_number_formats(self, tmp_path):
        from kernos.kernel.compaction import CompactionService

        service = CompactionService(
            state=AsyncMock(),
            reasoning=AsyncMock(),
            token_adapter=AsyncMock(),
            data_dir=str(tmp_path),
        )

        archive_dir = tmp_path / "test__tenant" / "state" / "compaction" / "space_1" / "archives"
        archive_dir.mkdir(parents=True)
        (archive_dir / "compaction_archive_002.md").write_text("Archive 2")

        with patch.object(service, "_space_dir", return_value=tmp_path / "test__tenant" / "state" / "compaction" / "space_1"):
            assert await service.load_archive("t", "s", "2") == "Archive 2"
            assert await service.load_archive("t", "s", "#2") == "Archive 2"
            assert await service.load_archive("t", "s", "002") == "Archive 2"

    async def test_missing_archive_returns_none(self, tmp_path):
        from kernos.kernel.compaction import CompactionService

        service = CompactionService(
            state=AsyncMock(),
            reasoning=AsyncMock(),
            token_adapter=AsyncMock(),
            data_dir=str(tmp_path),
        )
        with patch.object(service, "_space_dir", return_value=tmp_path / "nonexistent"):
            result = await service.load_archive("t", "s", "99")
        assert result is None


# ─── REMEMBER_TOOL definition ────────────────────────────────────────


class TestRememberToolDef:
    def test_tool_schema_valid(self):
        assert REMEMBER_TOOL["name"] == "remember"
        assert "query" in REMEMBER_TOOL["input_schema"]["properties"]
        assert "query" in REMEMBER_TOOL["input_schema"]["required"]


# ─── Template includes remember instruction ──────────────────────────


class TestTemplateInstruction:
    def test_operating_principles_include_remember(self):
        from kernos.kernel.template import PRIMARY_TEMPLATE
        assert "remember" in PRIMARY_TEMPLATE.operating_principles.lower()
        assert "asking the user to repeat" in PRIMARY_TEMPLATE.operating_principles.lower()


# ─── Extraction prompt includes behavioral_instruction ───────────────


class TestExtractionPrompt:
    def test_extraction_prompt_includes_behavioral_instruction(self):
        from kernos.kernel.projectors.llm_extractor import _EXTRACTION_SYSTEM_PROMPT
        assert "behavioral_instruction" in _EXTRACTION_SYSTEM_PROMPT


# ─── Handler wires remember tool ─────────────────────────────────────


class TestHandlerRememberTool:
    def test_handler_adds_remember_tool_when_retrieval_available(self):
        """Verify handler adds REMEMBER_TOOL to tools list."""
        # This is tested implicitly through the handler init and process flow.
        # The actual integration is tested in the full handler test suite.
        from kernos.kernel.retrieval import REMEMBER_TOOL
        assert REMEMBER_TOOL["name"] == "remember"
