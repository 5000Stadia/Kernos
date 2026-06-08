"""Retrieval Service — handles remember() tool calls.

Searches KnowledgeEntries, the entity graph, and compaction archives.
Returns formatted readable text within a token budget.

The `remember` tool is a kernel-managed tool — not an MCP tool.
When the reasoning service encounters a `remember` tool call, it routes
to this service instead of MCPClientManager.
"""
import asyncio
from kernos.utils import utc_now
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from kernos.kernel.entities import EntityNode
from kernos.kernel.state import KnowledgeEntry, StateStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SIMILARITY_THRESHOLD = 0.65  # Tunable — live test validates this cutoff
# Lexical fallback (v1 self-test, 2026-06-08): pure vector search skips any
# KnowledgeEntry that has no stored embedding (note_this writes without
# embed-on-write), so a freshly-noted fact like "favorite test color =
# cerulean" returned knowledge=0 on recall. When an entry has no embedding we
# fall back to token containment against the entry text AND its key. The bar is
# lower than the semantic cutoff because recall queries are often verbose/noisy
# ("favorite test color cerulean v1 self-test tests 1 2 3 …"), which dilutes a
# raw content-overlap fraction; the key-token path keeps precision (a fact
# keyed `favorite_test_color` recalled via "…favorite test color…" scores 1.0).
LEXICAL_FALLBACK_THRESHOLD = 0.5
_LEXICAL_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on",
    "for", "and", "or", "my", "your", "you", "me", "it", "this", "that",
    "what", "whats", "do", "does", "can", "could", "with", "about", "tell",
})


def _content_tokens(text: str) -> set[str]:
    """Lowercased content tokens (len>2, minus function words) for lexical match."""
    import re
    return {
        t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(t) > 2 and t not in _LEXICAL_STOPWORDS
    }


def lexical_overlap_score(query: str, text: str, key: str = "") -> float:
    """Token-containment score in [0, 1] for the no-embedding fallback.

    max of (fraction of query content tokens present in the entry text/key,
    fraction of the entry KEY's tokens present in the query). The key path is
    what rescues canonical facts from verbose recall queries.
    """
    q = _content_tokens(query)
    if not q:
        return 0.0
    # Token-equality, NOT substring: a substring check would let "dog" match
    # "dogma" or "son" match "reasoning" and admit unrelated memories (Codex
    # review). Tokenize the haystack and compare by set membership.
    hay = _content_tokens(
        (text or "") + " " + (key or "").replace("_", " ")
    )
    frac_q = len(q & hay) / len(q)
    score = frac_q
    if key:
        ktoks = _content_tokens(key.replace("_", " "))
        if ktoks:
            key_in_q = len(ktoks & q) / len(ktoks)
            score = max(score, key_in_q)
    return score


RETRIEVAL_RESULT_TOKEN_BUDGET = 1500
FORESIGHT_BOOST = 1.5  # Multiplier for active foresight signals matching the query
SPACE_RELEVANCE_BOOST = 1.2  # Multiplier for entries extracted in the active space

# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

REMEMBER_TOOL = {
    "name": "remember",
    "description": (
        "Search your memory for information about people, facts, events, "
        "past conversations, or anything you've been told. Use this before "
        "asking the user to repeat themselves. Returns a readable summary "
        "of what you know."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you want to remember — a natural language question or topic.",
            }
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ScoredKnowledge:
    """A KnowledgeEntry with similarity and quality scores."""

    entry: KnowledgeEntry
    similarity: float = 0.0
    quality_score: float = 0.0


@dataclass
class EntityResult:
    """An entity and its linked knowledge entries."""

    entity: EntityNode
    knowledge: list[KnowledgeEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Structured snapshot types (COHORT-ADAPT-MEMORY)
#
# Per the spec's Section 2a + the design review edits #3, #5, #6, #7: structured
# retrieval surface that shares the collect+policy+rank+budget-shape
# pipeline with the existing formatted `search()` path.
# ---------------------------------------------------------------------------


# Default top-N caps applied at structured-snapshot budget-shape time.
# search() uses token budget for the same shape; same pipeline, different
# units (token-budget for text, count-budget for structured snapshot).
SNAPSHOT_TOP_KNOWLEDGE = 5
SNAPSHOT_TOP_ENTITIES = 5
SNAPSHOT_KNOWLEDGE_CONTENT_CAP = 300  # chars
SNAPSHOT_ARCHIVE_SUMMARY_CAP = 500  # chars


@dataclass(frozen=True)
class KnowledgeMatch:
    """Public, structured form of a ranked knowledge entry.

    Mirrors KnowledgeEntry's fields the cohort surface exposes,
    minus internal state. `quality_score` is retrieval-local
    semantics — useful for ordering inside a single retrieval but
    NOT a cross-cohort universal score (per spec criterion 21).
    """

    entry_id: str
    content: str  # untruncated; cohort layer truncates per its budget
    authored_by: str  # owner_member_id; "" = instance owner
    created_at: str
    quality_score: float
    similarity: float
    source_space_id: str  # entry.context_space


@dataclass(frozen=True)
class EntityMatch:
    """Public, structured form of an entity-graph match.

    `knowledge_count` reflects post-disclosure-filter linked entries
    (the design review edit #4). `uncertainty_notes` carries MAYBE_SAME_AS edges
    after filtering to entities the requesting member can see.
    """

    entity_id: str
    canonical_name: str
    entity_type: str
    knowledge_count: int
    linked_knowledge_ids: tuple[str, ...]
    uncertainty_notes: tuple[str, ...]


@dataclass(frozen=True)
class ArchiveMatch:
    """Public, structured form of a compaction-archive match.

    Always None when `include_archives=False` (the cohort path).
    Populated only when the legacy `remember`-tool path passes
    `include_archives=True` and an archive was extracted. Rich
    span metadata (spans_from, spans_to) is future work — the
    existing archive substrate doesn't carry it cleanly today.
    """

    archive_id: str
    span_summary: str  # the extract; truncated at the cohort layer
    ancestor_space_id: str


@dataclass(frozen=True)
class RetrievalSnapshot:
    """Structured retrieval result.

    Returned by `search_structured()`. Same collect+policy+rank+
    budget-shape pipeline as `search()`; this is the cohort-friendly
    output shape.

    `source`:
      - "normal" — regular semantic + entity-graph retrieval
      - "state_intercept" — preference/state intercept fired;
        knowledge/entities/archive empty, state_intercept populated
        (the design review edit #5; spec Section 2b)

    `retrieval_attempted` distinguishes graceful-empty-on-
    embedding-failure (False) from genuine empty results (True).
    Per the design review edit #6 / spec criterion 16. Other unexpected bugs
    are NOT swallowed here; they propagate to the runner.
    """

    knowledge: tuple[KnowledgeMatch, ...]
    entities: tuple[EntityMatch, ...]
    archive: ArchiveMatch | None
    maybe_same_as: tuple[tuple[str, str], ...]  # (entity_a_name, entity_b_name)
    state_intercept: str | None
    source: str  # "normal" | "state_intercept"
    query: str
    scope_chain: tuple[str, ...]
    retrieval_attempted: bool
    truncated: bool


# ---------------------------------------------------------------------------
# Ranking function
# ---------------------------------------------------------------------------




def _days_since(iso_timestamp: str, now_iso: str) -> float:
    """Days between two ISO timestamps."""
    try:
        then = datetime.fromisoformat(iso_timestamp)
        now = datetime.fromisoformat(now_iso)
        return max((now - then).total_seconds() / 86400, 0)
    except (ValueError, TypeError):
        return 0.0


def compute_quality_score(
    entry: KnowledgeEntry, active_space_id: str, now_iso: str
) -> float:
    """Simple ranking: recency + confidence + reinforcement.

    Weighted sum, not multiplication.
    Space-scoped entries get a boost when queried from their space.
    """
    # Recency: linear decay over 90 days, floor at 0.1
    days_old = _days_since(entry.created_at, now_iso)
    recency = max(1.0 - (days_old / 90.0), 0.1)

    # Confidence
    confidence_map = {
        "stated": 1.0,
        "observed": 0.8,
        "inferred": 0.6,
        "high": 0.9,
        "medium": 0.7,
        "low": 0.5,
    }
    confidence = confidence_map.get(entry.confidence, 0.6)

    # Reinforcement: capped at 5 confirmations = max score
    reinforcement = min(entry.reinforcement_count / 5.0, 1.0)

    # Weighted sum
    score = (recency * 0.4) + (confidence * 0.3) + (reinforcement * 0.3)

    # Space relevance boost
    if entry.context_space == active_space_id and active_space_id:
        score *= SPACE_RELEVANCE_BOOST

    return score


def _apply_foresight_boost(
    candidates: list[ScoredKnowledge],
    query_lower: str,
    now_iso: str,
) -> list[ScoredKnowledge]:
    """Boost active, relevant foresight signals in the ranking."""
    for c in candidates:
        entry = c.entry
        if not entry.foresight_signal:
            continue
        # Check if foresight is still active
        if entry.foresight_expires and entry.foresight_expires < now_iso:
            continue
        # Check if query is related to foresight signal
        signal_words = set(entry.foresight_signal.lower().split())
        query_words = set(query_lower.split())
        if signal_words & query_words:
            c.quality_score *= FORESIGHT_BOOST
    return candidates


# ---------------------------------------------------------------------------
# RetrievalService
# ---------------------------------------------------------------------------


class RetrievalService:
    """Handles remember() tool calls.

    Searches KnowledgeEntries, the entity graph, and compaction archives.
    Returns formatted readable text within a token budget.
    """

    def __init__(
        self,
        state: StateStore,
        embedding_service,  # EmbeddingService — Any to avoid import
        embedding_store,  # JsonEmbeddingStore — Any to avoid import
        compaction,  # CompactionService — Any to avoid circular import
        reasoning,  # ReasoningService — Any to avoid circular import
    ) -> None:
        self.state = state
        self.embeddings = embedding_service
        self.embedding_store = embedding_store
        self.compaction = compaction
        self.reasoning = reasoning

    async def _build_scope_chain(self, instance_id: str, space_id: str) -> list[str]:
        """Walk up the parent chain. Returns [space_id, parent_id, grandparent_id, ...].

        Only includes IDs of spaces that actually exist. Stops at root or cycle.
        """
        chain: list[str] = []
        current = space_id
        seen: set[str] = set()
        while current and current not in seen:
            space = await self.state.get_context_space(instance_id, current)
            if not space:
                break
            chain.append(current)
            seen.add(current)
            if not space.parent_id:
                break
            current = space.parent_id
        return chain

    async def search(
        self,
        instance_id: str,
        query: str,
        active_space_id: str,
        requesting_member_id: str = "",
        instance_db: Any = None,
        trace: Any = None,
    ) -> str:
        """Execute a remember() query. Returns formatted readable text.

        Preference/state-shaped queries are automatically augmented with
        structured state from inspect_state — the agent gets authoritative
        data regardless of which tool it called.

        DISCLOSURE-GATE: when requesting_member_id is provided, entries
        authored by other members are filtered per the simplified relationship
        permission model. Callers in turn-path should always pass it.

        Per COHORT-ADAPT-MEMORY: this path now shares its pipeline with
        `search_structured(include_archives=True)`. Output shape is text
        (existing remember-tool surface); structured shape lives on the
        sibling method.
        """
        now = utc_now()
        query_lower = query.lower()

        # Structural intercept: preference/state queries get inspect_state data
        _state_keywords = ["preference", "setting", "notification", "trigger",
                          "what's set up", "what do i have", "what is set"]
        if any(kw in query_lower for kw in _state_keywords):
            try:
                from kernos.kernel.introspection import build_user_truth_view
                state_view = await build_user_truth_view(
                    instance_id, self.state,
                    getattr(self.reasoning, '_trigger_store', None),
                    getattr(self.reasoning, '_registry', None),
                )
                if state_view:
                    logger.info("REMEMBER_STATE_AUGMENT: query=%s — augmenting with inspect_state", query[:60])
                    return f"[Structured state — authoritative]\n{state_view}"
            except Exception as exc:
                logger.warning("REMEMBER_STATE_AUGMENT: failed: %s", exc)

        # Build scope chain for hierarchical search
        scope_chain = await self._build_scope_chain(instance_id, active_space_id)
        if len(scope_chain) > 1:
            logger.info("SCOPE_CHAIN: space=%s depth=%d chain=%s",
                active_space_id, len(scope_chain) - 1, scope_chain)

        # Build the permission map once and thread it through every
        # disclosure-gate-aware payload source (the design review edit #4).
        permission_map = None
        if requesting_member_id and instance_db is not None:
            from kernos.kernel.disclosure_gate import build_permission_map
            permission_map = await build_permission_map(
                instance_db, requesting_member_id,
            )

        # Embed the query
        try:
            query_embedding = await self.embeddings.embed(query)
            embedding_succeeded = True
        except Exception as exc:
            logger.warning("Retrieval: embedding failed for query %r: %s", query[:60], exc)
            query_embedding = None
            embedding_succeeded = False

        # Stage 1: Gather candidates (concurrent via asyncio.gather)
        async def _empty_knowledge() -> list[ScoredKnowledge]:
            return []

        if query_embedding is not None:
            knowledge_task = self._search_knowledge(
                instance_id, query, query_embedding, active_space_id, scope_chain,
                requesting_member_id=requesting_member_id,
                instance_db=instance_db, trace=trace,
            )
        else:
            knowledge_task = _empty_knowledge()

        entity_task = self._search_entities(
            instance_id, query, active_space_id, scope_chain,
            requesting_member_id=requesting_member_id,
            permission_map=permission_map,
            trace=trace,
        )
        archive_task = self._search_archives(instance_id, query, active_space_id, scope_chain)

        knowledge_candidates, entity_results, archive_result = await asyncio.gather(
            knowledge_task, entity_task, archive_task,
            return_exceptions=True,
        )

        # Handle exceptions from gather
        if isinstance(knowledge_candidates, Exception):
            logger.warning("Retrieval: knowledge search failed: %s", knowledge_candidates)
            knowledge_candidates = []
        if isinstance(entity_results, Exception):
            logger.warning("Retrieval: entity search failed: %s", entity_results)
            entity_results = []
        if isinstance(archive_result, Exception):
            logger.warning("Retrieval: archive search failed: %s", archive_result)
            archive_result = None

        # Collect MAYBE_SAME_AS for uncertainty notes (gated per the design review edit #4)
        maybe_same_as = await self._collect_maybe_same_as(
            instance_id, entity_results,
            requesting_member_id=requesting_member_id,
            permission_map=permission_map,
        )

        # Stage 2: Rank by quality
        for c in knowledge_candidates:
            c.quality_score = compute_quality_score(c.entry, active_space_id, now)
        knowledge_candidates = _apply_foresight_boost(
            knowledge_candidates, query_lower, now
        )
        knowledge_candidates.sort(key=lambda c: c.quality_score, reverse=True)

        # Stage 3: Format within token budget
        logger.info(
            "REMEMBER query=%s space=%s knowledge=%d entities=%d archive=%s",
            query[:60], active_space_id, len(knowledge_candidates),
            len(entity_results), bool(archive_result),
        )
        return self._format_results(
            knowledge_candidates, entity_results, archive_result, maybe_same_as
        )

    async def _search_knowledge(
        self,
        instance_id: str,
        query: str,
        query_embedding: list[float],
        active_space_id: str,
        scope_chain: list[str] | None = None,
        requesting_member_id: str = "",
        instance_db: Any = None,
        trace: Any = None,
    ) -> list[ScoredKnowledge]:
        """Semantic search over KnowledgeEntries. Walks scope chain.

        DISCLOSURE-GATE: entries authored by other members are filtered
        before space scoping when requesting_member_id is provided.
        """
        from kernos.kernel.embeddings import cosine_similarity  # noqa: F811

        all_entries = await self.state.query_knowledge(
            instance_id, active_only=True, limit=500
        )

        # Disclosure gate — drop cross-member content before it ranks.
        if requesting_member_id and instance_db is not None:
            from kernos.kernel.disclosure_gate import (
                build_permission_map, filter_knowledge_entries,
            )
            _perm_map = await build_permission_map(
                instance_db, requesting_member_id,
            )
            all_entries = filter_knowledge_entries(
                all_entries,
                requesting_member_id=requesting_member_id,
                permission_map=_perm_map,
                trace=trace,
            )

        # Space scoping — include all ancestor spaces + global entries
        _scope = set(scope_chain) if scope_chain else {active_space_id}
        entries = [
            e
            for e in all_entries
            if e.context_space in _scope or e.context_space in ("", None)
        ]

        candidates = []
        for entry in entries:
            entry_embedding = await self.embedding_store.get(instance_id, entry.id)
            if entry_embedding is not None:
                # Embedded entry: the semantic threshold is the precision
                # filter. A sub-threshold cosine is an INTENTIONAL "not relevant
                # enough" decision — do NOT lexically override it, or short
                # queries bypass the threshold entirely (Codex review). New
                # facts get real embeddings via embed-on-write (②), so they're
                # found here without needing the lexical path.
                similarity = cosine_similarity(query_embedding, entry_embedding)
                if similarity >= SIMILARITY_THRESHOLD:
                    candidates.append(
                        ScoredKnowledge(entry=entry, similarity=similarity)
                    )
            else:
                # No stored embedding — can't be semantically scored at all, so
                # pure vector search would skip it entirely. This is the only
                # legitimate lexical-fallback case: legacy entries written
                # before embed-on-write (e.g. the live cerulean fact that
                # returned knowledge=0). (v1 self-test.)
                lex = lexical_overlap_score(
                    query, entry.content, getattr(entry, "subject", "")
                )
                if lex >= LEXICAL_FALLBACK_THRESHOLD:
                    candidates.append(
                        ScoredKnowledge(entry=entry, similarity=lex)
                    )

        return candidates

    async def _search_entities(
        self,
        instance_id: str,
        query: str,
        active_space_id: str,
        scope_chain: list[str] | None = None,
        *,
        requesting_member_id: str = "",
        permission_map: Any | None = None,
        trace: Any = None,
    ) -> list[EntityResult]:
        """Search entities by name/alias match. Walks scope chain.

        Per COHORT-ADAPT-MEMORY the design review edit #4: the disclosure gate
        applies to the linked KnowledgeEntries pulled by
        `_resolve_entity_knowledge` too — not just the
        `_search_knowledge` semantic path. Pass requesting_member_id
        + permission_map through; the resolver filters at the
        merge step.
        """
        entities = await self.state.query_entity_nodes(
            instance_id, active_only=True
        )

        # Space scoping — include all ancestor spaces + global entries
        _scope = set(scope_chain) if scope_chain else {active_space_id}
        entities = [
            e
            for e in entities
            if e.context_space in _scope or e.context_space in ("", None)
        ]

        matched = []
        query_lower = query.lower()
        for entity in entities:
            names = [entity.canonical_name.lower()] + [
                a.lower() for a in entity.aliases
            ]
            if any(
                name in query_lower or query_lower in name for name in names
            ):
                # Resolve SAME_AS edges — merge linked entities
                merged_knowledge = await self._resolve_entity_knowledge(
                    instance_id,
                    entity,
                    requesting_member_id=requesting_member_id,
                    permission_map=permission_map,
                    trace=trace,
                )
                matched.append(
                    EntityResult(entity=entity, knowledge=merged_knowledge)
                )

        return matched

    async def _resolve_entity_knowledge(
        self,
        instance_id: str,
        entity: EntityNode,
        *,
        requesting_member_id: str = "",
        permission_map: Any | None = None,
        trace: Any = None,
    ) -> list[KnowledgeEntry]:
        """Gather all knowledge linked to this entity, resolving SAME_AS edges.

        Per the design review edit #4: when requesting_member_id + permission_map
        are supplied, filter linked entries through the disclosure
        gate before returning. Without that filter, entity resolution
        would surface entries the active member shouldn't see.
        """
        knowledge: list[KnowledgeEntry] = []

        # Direct knowledge links
        for entry_id in entity.knowledge_entry_ids:
            entry = await self.state.get_knowledge_entry(instance_id, entry_id)
            if entry and entry.active:
                knowledge.append(entry)

        # Traverse identity edges
        edges = await self.state.query_identity_edges(instance_id, entity.id)
        for edge in edges:
            other_id = (
                edge.target_id
                if edge.source_id == entity.id
                else edge.source_id
            )
            if edge.edge_type == "SAME_AS" and edge.confidence >= 0.8:
                # Merge: pull knowledge from the linked node
                other_node = await self.state.get_entity_node(
                    instance_id, other_id
                )
                if other_node and other_node.active:
                    for entry_id in other_node.knowledge_entry_ids:
                        entry = await self.state.get_knowledge_entry(
                            instance_id, entry_id
                        )
                        if (
                            entry
                            and entry.active
                            and entry.id not in [k.id for k in knowledge]
                        ):
                            knowledge.append(entry)

        # Uniform disclosure gate: apply the same filter the semantic
        # knowledge path uses. Without requesting_member_id this is
        # a no-op (legacy callers pre-CAM see no behavior change).
        if requesting_member_id and permission_map is not None and knowledge:
            from kernos.kernel.disclosure_gate import filter_knowledge_entries
            knowledge = filter_knowledge_entries(
                knowledge,
                requesting_member_id=requesting_member_id,
                permission_map=permission_map,
                trace=trace,
            )

        return knowledge

    async def _search_archives(
        self,
        instance_id: str,
        query: str,
        active_space_id: str,
        scope_chain: list[str] | None = None,
    ) -> str | None:
        """Search compaction archives via the index. Walks scope chain."""
        _chain = scope_chain or [active_space_id]

        for ancestor_id in _chain:
            index_text = await self.compaction.load_index(instance_id, ancestor_id)
            if not index_text:
                continue

            # Ask Haiku: which archive (if any) is relevant to this query?
            result = await self.reasoning.complete_simple(
                system_prompt=(
                    "Given this archive index and a query, determine if any archive "
                    "is relevant. If yes, return the archive number (just the digit). "
                    "If no, return 'none'. Return only the archive number or 'none'."
                ),
                user_content=f"Index:\n{index_text}\n\nQuery: {query}",
                max_tokens=32,
                prefer_cheap=True,
            )

            result = result.strip().lower()
            if result == "none" or not result:
                continue

            # Load the matching archive
            archive_text = await self.compaction.load_archive(
                instance_id, ancestor_id, result
            )
            if not archive_text:
                continue

            # Second Haiku call: extract relevant section
            extract = await self.reasoning.complete_simple(
                system_prompt=(
                    "Extract the information relevant to this query from the archive. "
                    "Return a concise, readable summary. If nothing is relevant, "
                    "return 'nothing found'."
                ),
                user_content=f"Query: {query}\n\nArchive:\n{archive_text}",
                max_tokens=800,
                prefer_cheap=True,
            )

            if extract and "nothing found" not in extract.lower():
                if ancestor_id != active_space_id:
                    logger.info("SCOPE_CHAIN_HIT: query=%r found_in=%s (ancestor)", query[:60], ancestor_id)
                return extract

        return None

    async def _collect_maybe_same_as(
        self,
        instance_id: str,
        entity_results: list[EntityResult],
        *,
        requesting_member_id: str = "",
        permission_map: Any | None = None,
    ) -> list[tuple[EntityNode, EntityNode]]:
        """Collect MAYBE_SAME_AS edges for matched entities.

        Per the design review edit #4: filter pairs to those where the OTHER node
        references at least one knowledge entry the requesting
        member can see. If all the other node's referencing
        knowledge is gated out, the uncertainty note can't be
        surfaced — it would otherwise leak the existence of an
        entity tied to gated content.
        """
        maybe_pairs: list[tuple[EntityNode, EntityNode]] = []
        for er in entity_results:
            edges = await self.state.query_identity_edges(
                instance_id, er.entity.id
            )
            for edge in edges:
                if edge.edge_type == "MAYBE_SAME_AS":
                    other_id = (
                        edge.target_id
                        if edge.source_id == er.entity.id
                        else edge.source_id
                    )
                    other_node = await self.state.get_entity_node(
                        instance_id, other_id
                    )
                    if not (other_node and other_node.active):
                        continue
                    # Disclosure check: drop the pair if the other
                    # node has zero visible knowledge for the
                    # requesting member.
                    if requesting_member_id and permission_map is not None:
                        from kernos.kernel.disclosure_gate import (
                            filter_knowledge_entries,
                        )
                        other_knowledge: list[KnowledgeEntry] = []
                        for entry_id in other_node.knowledge_entry_ids:
                            entry = await self.state.get_knowledge_entry(
                                instance_id, entry_id,
                            )
                            if entry and entry.active:
                                other_knowledge.append(entry)
                        visible = filter_knowledge_entries(
                            other_knowledge,
                            requesting_member_id=requesting_member_id,
                            permission_map=permission_map,
                        )
                        if not visible and other_knowledge:
                            # Other node exists but has no visible
                            # knowledge — drop the uncertainty note.
                            continue
                    maybe_pairs.append((er.entity, other_node))
        return maybe_pairs

    # -----------------------------------------------------------------------
    # COHORT-ADAPT-MEMORY: search_structured surface
    # -----------------------------------------------------------------------

    async def search_structured(
        self,
        instance_id: str,
        query: str,
        active_space_id: str,
        requesting_member_id: str = "",
        instance_db: Any = None,
        trace: Any = None,
        *,
        include_archives: bool = False,
    ) -> RetrievalSnapshot:
        """Structured retrieval surface — collect + policy + rank + budget-shape.

        Same pipeline as `search()`; differs only in output shape
        (`RetrievalSnapshot` vs. formatted text). Per the design review edit #3
        the two paths cannot drift — they share the entity / knowledge /
        archive / state-intercept logic via the same internal helpers.

        Per the design review edit #2: archive search is gated behind
        `include_archives` because today's `_search_archives`
        invokes Haiku twice. The cohort path passes `False`; the
        legacy `remember`-tool path passes `True` (preserves
        existing archive coverage).

        Per the design review edit #6: only embedding/vector failure produces
        ``retrieval_attempted=False`` with empty arrays. Every
        other unexpected error propagates so the cohort runner
        (or the caller) can register an outcome=error rather than
        silently swallow.
        """
        now = utc_now()
        query_lower = query.lower()

        # State intercept short-circuit (the design review edit #5).
        _state_keywords = [
            "preference", "setting", "notification", "trigger",
            "what's set up", "what do i have", "what is set",
        ]
        if any(kw in query_lower for kw in _state_keywords):
            try:
                from kernos.kernel.introspection import build_user_truth_view
                state_view = await build_user_truth_view(
                    instance_id, self.state,
                    getattr(self.reasoning, "_trigger_store", None),
                    getattr(self.reasoning, "_registry", None),
                )
            except Exception as exc:
                logger.warning(
                    "REMEMBER_STATE_AUGMENT_STRUCTURED: failed: %s", exc,
                )
                state_view = ""
            if state_view:
                return RetrievalSnapshot(
                    knowledge=(),
                    entities=(),
                    archive=None,
                    maybe_same_as=(),
                    state_intercept=f"[Structured state — authoritative]\n{state_view}",
                    source="state_intercept",
                    query=query,
                    scope_chain=(active_space_id,),
                    retrieval_attempted=False,
                    truncated=False,
                )

        # Build the permission map once for uniform disclosure gating.
        permission_map = None
        if requesting_member_id and instance_db is not None:
            from kernos.kernel.disclosure_gate import build_permission_map
            permission_map = await build_permission_map(
                instance_db, requesting_member_id,
            )

        # Scope chain.
        scope_chain = await self._build_scope_chain(
            instance_id, active_space_id,
        )

        # Embed the query. Embedding failure is the only error path
        # that produces ``retrieval_attempted=False`` with empty
        # arrays; everything else propagates (the design review edit #6).
        try:
            query_embedding = await self.embeddings.embed(query)
        except Exception as exc:
            logger.warning(
                "Retrieval: embedding failed for query %r: %s",
                query[:60], exc,
            )
            return RetrievalSnapshot(
                knowledge=(),
                entities=(),
                archive=None,
                maybe_same_as=(),
                state_intercept=None,
                source="normal",
                query=query,
                scope_chain=tuple(scope_chain),
                retrieval_attempted=False,
                truncated=False,
            )

        # Parallel collect with uniform disclosure-gate threading.
        knowledge_task = self._search_knowledge(
            instance_id, query, query_embedding, active_space_id, scope_chain,
            requesting_member_id=requesting_member_id,
            instance_db=instance_db, trace=trace,
        )
        entity_task = self._search_entities(
            instance_id, query, active_space_id, scope_chain,
            requesting_member_id=requesting_member_id,
            permission_map=permission_map,
            trace=trace,
        )
        if include_archives:
            archive_task = self._search_archives(
                instance_id, query, active_space_id, scope_chain,
            )
        else:
            async def _no_archive():
                return None
            archive_task = _no_archive()

        knowledge_candidates, entity_results, archive_result = (
            await asyncio.gather(
                knowledge_task, entity_task, archive_task,
                return_exceptions=False,  # propagate unexpected bugs
            )
        )

        # MAYBE_SAME_AS, gated.
        maybe_same_as = await self._collect_maybe_same_as(
            instance_id, entity_results,
            requesting_member_id=requesting_member_id,
            permission_map=permission_map,
        )

        # Rank knowledge by quality score.
        for c in knowledge_candidates:
            c.quality_score = compute_quality_score(
                c.entry, active_space_id, now,
            )
        knowledge_candidates = _apply_foresight_boost(
            knowledge_candidates, query_lower, now,
        )
        knowledge_candidates.sort(
            key=lambda c: c.quality_score, reverse=True,
        )

        # Budget-shape: top-N each.
        knowledge_truncated = (
            len(knowledge_candidates) > SNAPSHOT_TOP_KNOWLEDGE
        )
        entities_truncated = (
            len(entity_results) > SNAPSHOT_TOP_ENTITIES
        )
        knowledge_top = knowledge_candidates[:SNAPSHOT_TOP_KNOWLEDGE]
        entities_top = entity_results[:SNAPSHOT_TOP_ENTITIES]

        # Project rich types into the public structured surface.
        knowledge_matches = tuple(
            KnowledgeMatch(
                entry_id=sk.entry.id,
                content=sk.entry.content,
                authored_by=sk.entry.owner_member_id,
                created_at=sk.entry.created_at,
                quality_score=sk.quality_score,
                similarity=sk.similarity,
                source_space_id=sk.entry.context_space or "",
            )
            for sk in knowledge_top
        )

        entity_matches = []
        for er in entities_top:
            uncertainty: list[str] = []
            for pair_a, pair_b in maybe_same_as:
                if pair_a.id == er.entity.id:
                    uncertainty.append(pair_b.canonical_name)
                elif pair_b.id == er.entity.id:
                    uncertainty.append(pair_a.canonical_name)
            entity_matches.append(
                EntityMatch(
                    entity_id=er.entity.id,
                    canonical_name=er.entity.canonical_name,
                    entity_type=er.entity.entity_type or "",
                    knowledge_count=len(er.knowledge),
                    linked_knowledge_ids=tuple(
                        e.id for e in er.knowledge
                    ),
                    uncertainty_notes=tuple(uncertainty),
                )
            )

        archive_match: ArchiveMatch | None = None
        if include_archives and archive_result:
            archive_match = ArchiveMatch(
                archive_id=f"{active_space_id}:archive",
                span_summary=archive_result,
                ancestor_space_id=active_space_id,
            )

        return RetrievalSnapshot(
            knowledge=knowledge_matches,
            entities=tuple(entity_matches),
            archive=archive_match,
            maybe_same_as=tuple(
                (a.canonical_name, b.canonical_name) for a, b in maybe_same_as
            ),
            state_intercept=None,
            source="normal",
            query=query,
            scope_chain=tuple(scope_chain),
            retrieval_attempted=True,
            truncated=knowledge_truncated or entities_truncated,
        )

    def _format_results(
        self,
        knowledge_results: list[ScoredKnowledge],
        entity_results: list[EntityResult],
        archive_result: str | None,
        maybe_same_as: list[tuple[EntityNode, EntityNode]],
    ) -> str:
        """Format retrieval results as readable prose within token budget."""
        parts: list[str] = []
        budget_remaining = RETRIEVAL_RESULT_TOKEN_BUDGET

        # Entity context
        for er in entity_results:
            entity_text = self._format_entity(er.entity, er.knowledge)
            tokens = len(entity_text) // 4
            if tokens <= budget_remaining:
                parts.append(entity_text)
                budget_remaining -= tokens

        # Knowledge entries (ranked, deduplicated against entity knowledge)
        seen_ids = {e.id for er in entity_results for e in er.knowledge}
        for sk in knowledge_results:
            if sk.entry.id in seen_ids:
                continue
            entry_text = f"{sk.entry.subject}: {sk.entry.content}"
            tokens = len(entry_text) // 4
            if tokens <= budget_remaining:
                parts.append(entry_text)
                budget_remaining -= tokens
                seen_ids.add(sk.entry.id)

        # Archive extract
        if archive_result and budget_remaining > 100:
            archive_tokens = len(archive_result) // 4
            if archive_tokens <= budget_remaining:
                parts.append(f"From history: {archive_result}")
                budget_remaining -= archive_tokens
            else:
                # Truncate archive to fit
                chars = budget_remaining * 4
                parts.append(f"From history: {archive_result[:chars]}...")

        # MAYBE_SAME_AS notes
        for node_a, node_b in maybe_same_as:
            note = (
                f"Note: {node_a.canonical_name} and {node_b.canonical_name} "
                f"may be the same person — treat carefully."
            )
            tokens = len(note) // 4
            if tokens <= budget_remaining:
                parts.append(note)
                budget_remaining -= tokens

        return (
            "\n\n".join(parts)
            if parts
            else "No relevant information found in memory."
        )

    def _format_entity(
        self, entity: EntityNode, knowledge: list[KnowledgeEntry]
    ) -> str:
        """Format an entity and its linked knowledge as readable text."""
        lines = [f"{entity.canonical_name}"]

        if entity.relationship_type:
            lines[0] += f" ({entity.relationship_type})"
        if entity.entity_type and entity.entity_type != "person":
            lines[0] += f" [{entity.entity_type}]"

        if entity.contact_phone:
            lines.append(f"  Phone: {entity.contact_phone}")
        if entity.contact_email:
            lines.append(f"  Email: {entity.contact_email}")
        if entity.aliases:
            display_aliases = [
                a for a in entity.aliases if a != entity.canonical_name
            ]
            if display_aliases:
                lines.append(f"  Also known as: {', '.join(display_aliases)}")

        for entry in knowledge[:5]:  # Cap at 5 most relevant facts per entity
            lines.append(f"  - {entry.content}")

        return "\n".join(lines)
