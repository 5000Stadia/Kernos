"""Tests for the memory cohort adapter (C2 of CAM).

Covers the 21 spec scenarios from COHORT-ADAPT-MEMORY's Live Test
section. RetrievalService backed by mock state stores; cohort run
exercises the full happy path + edge cases.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.cohorts import (
    CohortContext,
    CohortFanOutConfig,
    CohortFanOutRunner,
    CohortRegistry,
    ContextSpaceRef,
    Turn,
    register_memory_cohort,
)
from kernos.kernel.cohorts.memory_cohort import (
    ARCHIVE_SUMMARY_CAP,
    COHORT_ID,
    KNOWLEDGE_CONTENT_CAP,
    TIMEOUT_MS,
    make_memory_cohort_run,
    make_memory_descriptor,
)
from kernos.kernel.entities import EntityNode, IdentityEdge
from kernos.kernel.integration.briefing import (
    CohortOutput,
    Outcome,
    Public,
)
from kernos.kernel.retrieval import RetrievalService
from kernos.kernel.state import KnowledgeEntry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _knowledge(
    *,
    id: str,
    content: str = "memo",
    owner_member_id: str = "m-active",
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


def _retrieval_service(
    *,
    knowledge_entries: list[KnowledgeEntry] | None = None,
    entities: list[EntityNode] | None = None,
    identity_edges: list[IdentityEdge] | None = None,
    embedding_succeeds: bool = True,
    archive_text: str | None = None,
    knowledge_query_raises: bool = False,
) -> RetrievalService:
    state = MagicMock()
    if knowledge_query_raises:
        state.query_knowledge = AsyncMock(
            side_effect=RuntimeError("DB exploded mid-search"),
        )
    else:
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
    state.get_context_space = AsyncMock(return_value=None)

    embeddings = MagicMock()
    if embedding_succeeds:
        embeddings.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
    else:
        embeddings.embed = AsyncMock(side_effect=RuntimeError("embed down"))

    embedding_store = MagicMock()
    embedding_store.get = AsyncMock(return_value=[0.1, 0.2, 0.3])

    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(
        side_effect=AssertionError("memory cohort must not call LLM"),
    )
    reasoning.reason = AsyncMock(
        side_effect=AssertionError("memory cohort must not call LLM"),
    )
    reasoning._trigger_store = None
    reasoning._registry = None

    compaction = MagicMock()
    compaction.load_index = AsyncMock(return_value=archive_text)
    compaction.load_archive = AsyncMock(return_value=archive_text)

    return RetrievalService(
        state=state,
        embedding_service=embeddings,
        embedding_store=embedding_store,
        reasoning=reasoning,
        compaction=compaction,
    )


def _ctx(
    *,
    user_message: str = "tell me about Alice",
    member_id: str = "m-active",
    instance_id: str = "i-1",
    turn_id: str = "turn-1",
    spaces: tuple[ContextSpaceRef, ...] = (ContextSpaceRef("default"),),
) -> CohortContext:
    return CohortContext(
        member_id=member_id,
        user_message=user_message,
        conversation_thread=(Turn("user", user_message),),
        active_spaces=spaces,
        turn_id=turn_id,
        instance_id=instance_id,
        produced_at="2026-04-26T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# 1. Empty memory state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_1_empty_memory_state():
    svc = _retrieval_service()
    run = make_memory_cohort_run(svc)
    out = await run(_ctx())
    assert isinstance(out, CohortOutput)
    assert out.output["retrieval_attempted"] is True
    assert out.output["knowledge"] == []
    assert out.output["entities"] == []
    assert out.output["archive_summary"] is None
    assert out.output["source"] == "normal"


# ---------------------------------------------------------------------------
# 2. Single knowledge entry, exact-topic message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_2_single_entry_with_truncated_content():
    long_content = "Alice prefers tea " * 30  # ~540 chars
    entry = _knowledge(id="k-1", content=long_content)
    svc = _retrieval_service(knowledge_entries=[entry])
    run = make_memory_cohort_run(svc)
    out = await run(_ctx())
    assert len(out.output["knowledge"]) == 1
    summary = out.output["knowledge"][0]
    assert summary["entry_id"] == "k-1"
    assert len(summary["content_short"]) == KNOWLEDGE_CONTENT_CAP
    assert summary["quality_score"] > 0


# ---------------------------------------------------------------------------
# 3. Multiple knowledge entries ranked by quality
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_3_multiple_entries_ranked():
    entries = [
        _knowledge(
            id=f"k-{i}",
            content=f"content {i}",
            confidence="stated" if i % 2 == 0 else "inferred",
            reinforcement_count=10 - i,
        )
        for i in range(5)
    ]
    svc = _retrieval_service(knowledge_entries=entries)
    run = make_memory_cohort_run(svc)
    out = await run(_ctx())
    assert len(out.output["knowledge"]) == 5
    scores = [k["quality_score"] for k in out.output["knowledge"]]
    # Descending order.
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# 4. Truncation flag set when more than top-N matched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_4_truncation_flag_set():
    entries = [_knowledge(id=f"k-{i}") for i in range(8)]
    svc = _retrieval_service(knowledge_entries=entries)
    run = make_memory_cohort_run(svc)
    out = await run(_ctx())
    assert len(out.output["knowledge"]) == 5
    assert out.output["truncation"]["knowledge_truncated"] is True


# ---------------------------------------------------------------------------
# 5. Entity match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_5_entity_with_knowledge_count():
    knowledge = [
        _knowledge(id="k-1", content="met Alice yesterday"),
        _knowledge(id="k-2", content="Alice likes coffee"),
        _knowledge(id="k-3", content="Alice plays piano"),
    ]
    alice = _entity(id="e-alice", name="Alice", knowledge_ids=["k-1", "k-2", "k-3"])
    svc = _retrieval_service(knowledge_entries=knowledge, entities=[alice])
    run = make_memory_cohort_run(svc)
    out = await run(_ctx(user_message="tell me about Alice"))
    assert len(out.output["entities"]) == 1
    em = out.output["entities"][0]
    assert em["name"] == "Alice"
    assert em["knowledge_count"] == 3


# ---------------------------------------------------------------------------
# 6. No archive in cohort path (the design review edit #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_6_archive_excluded_from_cohort_path():
    """include_archives=False → _search_archives never runs →
    no Haiku LLM call. The reasoning mock raises if called."""
    svc = _retrieval_service(archive_text="archive index would match")
    run = make_memory_cohort_run(svc)
    out = await run(_ctx())
    assert out.output["archive_summary"] is None
    svc.reasoning.complete_simple.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Disclosure gate — semantic knowledge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_7_disclosure_gate_filters_semantic_knowledge():
    visible = [
        _knowledge(id="k-mine-1", owner_member_id="m-active"),
        _knowledge(id="k-mine-2", owner_member_id="m-active"),
    ]
    gated = [
        _knowledge(
            id="k-other-1",
            owner_member_id="m-other",
            sensitivity="personal",
        ),
        _knowledge(
            id="k-other-2",
            owner_member_id="m-other",
            sensitivity="personal",
        ),
    ]
    svc = _retrieval_service(knowledge_entries=visible + gated)

    instance_db = MagicMock()
    instance_db.list_permissions_for = AsyncMock(return_value={})

    run = make_memory_cohort_run(svc, instance_db=instance_db)
    out = await run(_ctx())
    ids = [k["entry_id"] for k in out.output["knowledge"]]
    assert "k-mine-1" in ids
    assert "k-mine-2" in ids
    # Other-member personal entries gated out.
    assert "k-other-1" not in ids
    assert "k-other-2" not in ids


# ---------------------------------------------------------------------------
# 8. Disclosure gate — entity-linked knowledge (the design review edit #4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_8_disclosure_gate_on_entity_linked_knowledge():
    visible = [
        _knowledge(id="k-mine-1", owner_member_id="m-active"),
        _knowledge(id="k-mine-2", owner_member_id="m-active"),
    ]
    gated = [
        _knowledge(
            id="k-other",
            owner_member_id="m-other",
            sensitivity="personal",
        ),
    ]
    alice = _entity(
        id="e-alice",
        name="Alice",
        knowledge_ids=["k-mine-1", "k-mine-2", "k-other"],
    )
    svc = _retrieval_service(
        knowledge_entries=visible + gated,
        entities=[alice],
    )

    instance_db = MagicMock()
    instance_db.list_permissions_for = AsyncMock(return_value={})

    run = make_memory_cohort_run(svc, instance_db=instance_db)
    out = await run(_ctx(user_message="alice"))
    em = out.output["entities"][0]
    # knowledge_count reflects only visible linked entries.
    assert em["knowledge_count"] == 2


# ---------------------------------------------------------------------------
# 9. Disclosure gate — entity dropped when all linked knowledge gated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_9_entity_filtered_when_all_linked_knowledge_gated():
    """When the disclosure gate filters every linked knowledge
    entry, the entity surface gets knowledge_count=0. The cohort
    surfaces it (informational), but downstream integration can
    filter on knowledge_count > 0."""
    gated = [
        _knowledge(
            id="k-other",
            owner_member_id="m-other",
            sensitivity="personal",
        ),
    ]
    alice = _entity(id="e-alice", name="Alice", knowledge_ids=["k-other"])
    svc = _retrieval_service(knowledge_entries=gated, entities=[alice])

    instance_db = MagicMock()
    instance_db.list_permissions_for = AsyncMock(return_value={})

    run = make_memory_cohort_run(svc, instance_db=instance_db)
    out = await run(_ctx(user_message="alice"))
    if out.output["entities"]:
        # Per the design review edit #4 the entity may surface but its knowledge_count
        # reflects only visible entries (zero in this case).
        assert out.output["entities"][0]["knowledge_count"] == 0


# ---------------------------------------------------------------------------
# 10. Disclosure gate — MAYBE_SAME_AS notes filtered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_10_maybe_same_as_filtered_when_other_node_gated():
    """Active member sees Alice. Bob is MAYBE_SAME_AS Alice but Bob's
    only knowledge is owner=m-other/personal. The pair should be
    dropped from uncertainty notes."""
    own = _knowledge(id="k-mine", owner_member_id="m-active")
    gated = _knowledge(
        id="k-other", owner_member_id="m-other", sensitivity="personal",
    )
    alice = _entity(id="e-alice", name="Alice", knowledge_ids=["k-mine"])
    bob = _entity(id="e-bob", name="Bob", knowledge_ids=["k-other"])
    edge = IdentityEdge(
        source_id="e-alice",
        target_id="e-bob",
        edge_type="MAYBE_SAME_AS",
        confidence=0.7,
    )
    svc = _retrieval_service(
        knowledge_entries=[own, gated],
        entities=[alice, bob],
        identity_edges=[edge],
    )

    instance_db = MagicMock()
    instance_db.list_permissions_for = AsyncMock(return_value={})

    run = make_memory_cohort_run(svc, instance_db=instance_db)
    out = await run(_ctx(user_message="alice"))

    em = out.output["entities"][0]
    # Bob shouldn't appear as an uncertainty note since none of his
    # knowledge is visible.
    assert "Bob" not in em["uncertainty_notes"]


# ---------------------------------------------------------------------------
# 11. State intercept short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_11_state_intercept_short_circuits():
    svc = _retrieval_service()
    from kernos.kernel import introspection
    original = introspection.build_user_truth_view

    async def fake_view(*_a, **_kw):
        return "## Active Preferences\n- digest: weekly"

    introspection.build_user_truth_view = fake_view  # type: ignore[assignment]
    try:
        run = make_memory_cohort_run(svc)
        out = await run(_ctx(user_message="what notification preference do I have?"))
    finally:
        introspection.build_user_truth_view = original  # type: ignore[assignment]

    assert out.output["source"] == "state_intercept"
    assert out.output["state_intercept"] is not None
    assert "Structured state" in out.output["state_intercept"]
    assert out.output["knowledge"] == []
    assert out.output["entities"] == []
    assert out.output["archive_summary"] is None
    assert out.output["retrieval_attempted"] is False


# ---------------------------------------------------------------------------
# 12. Embedding service failure → graceful empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_12_embedding_failure_graceful_empty():
    svc = _retrieval_service(embedding_succeeds=False)
    run = make_memory_cohort_run(svc)
    out = await run(_ctx())
    assert out.output["retrieval_attempted"] is False
    assert out.output["knowledge"] == []
    assert out.output["entities"] == []


# ---------------------------------------------------------------------------
# 13. Unexpected retrieval bug propagates (runner outcome=error)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_13_unexpected_error_propagates_to_runner():
    """Per the design review edit #6: only embedding/vector failure swallows.
    Other unexpected bugs propagate so the runner registers
    outcome=error rather than silently swallowing as graceful empty."""
    svc = _retrieval_service(knowledge_query_raises=True)
    run = make_memory_cohort_run(svc)
    with pytest.raises(RuntimeError, match="DB exploded"):
        await run(_ctx())


@pytest.mark.asyncio
async def test_scenario_13b_runner_observes_outcome_error_for_unexpected_bug():
    """Wire the cohort through the fan-out runner: an unexpected
    error from retrieval surfaces as outcome=error, NOT as a
    silent graceful-empty success."""
    svc = _retrieval_service(knowledge_query_raises=True)
    registry = CohortRegistry()
    register_memory_cohort(registry, svc)
    audit: list[dict] = []

    async def emit(entry):
        audit.append(entry)

    runner = CohortFanOutRunner(
        registry=registry,
        audit_emitter=emit,
        config=CohortFanOutConfig(global_timeout_seconds=2.0),
    )
    result = await runner.run(_ctx())
    out = result.outputs[0]
    assert out.outcome is Outcome.ERROR
    assert out.error_summary  # non-empty


# ---------------------------------------------------------------------------
# 14. No model call during cohort run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_14_no_model_call_during_run():
    entry = _knowledge(id="k-1")
    svc = _retrieval_service(knowledge_entries=[entry])
    run = make_memory_cohort_run(svc)
    out = await run(_ctx())
    # Reasoning mock raises if called; reaching here = no LLM call.
    assert out.output["retrieval_attempted"] is True
    svc.reasoning.complete_simple.assert_not_called()
    svc.reasoning.reason.assert_not_called()


# ---------------------------------------------------------------------------
# 15. Cohort returns CohortOutput, not dict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_15_run_returns_cohort_output_type():
    svc = _retrieval_service()
    run = make_memory_cohort_run(svc)
    out = await run(_ctx())
    assert isinstance(out, CohortOutput)
    assert not isinstance(out, dict)


# ---------------------------------------------------------------------------
# 16. Cohort completes within timeout under stress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_16_cohort_completes_within_timeout():
    entries = [_knowledge(id=f"k-{i}") for i in range(50)]
    entities = [
        _entity(id=f"e-{i}", name=f"Person{i}", knowledge_ids=[f"k-{i}"])
        for i in range(20)
    ]
    svc = _retrieval_service(knowledge_entries=entries, entities=entities)
    run = make_memory_cohort_run(svc)
    import time
    start = time.monotonic()
    await run(_ctx(user_message="any person"))
    elapsed_ms = int((time.monotonic() - start) * 1000)
    assert elapsed_ms < TIMEOUT_MS


# ---------------------------------------------------------------------------
# 17. End-to-end via fan-out runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_17_cohort_via_fan_out_runner():
    entry = _knowledge(id="k-1")
    svc = _retrieval_service(knowledge_entries=[entry])
    registry = CohortRegistry()
    register_memory_cohort(registry, svc)
    audit: list[dict] = []

    async def emit(entry: dict):
        audit.append(entry)

    runner = CohortFanOutRunner(
        registry=registry,
        audit_emitter=emit,
        config=CohortFanOutConfig(global_timeout_seconds=2.0),
    )
    result = await runner.run(_ctx())
    out = result.outputs[0]
    assert out.cohort_id == COHORT_ID
    assert out.outcome is Outcome.SUCCESS
    assert out.cohort_run_id == "turn-1:memory:0"
    assert audit[0]["audit_category"] == "cohort.fan_out"


# ---------------------------------------------------------------------------
# 18. Integration consumption (V1 IntegrationRunner)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_18_integration_runner_consumes_memory_output():
    from kernos.kernel.integration import (
        Briefing,
        IntegrationConfig,
        IntegrationInputs,
        IntegrationRunner,
        RespondOnly,
    )
    from kernos.providers.base import ContentBlock, ProviderResponse

    entry = _knowledge(id="k-1", content="user prefers terse responses")
    svc = _retrieval_service(knowledge_entries=[entry])
    registry = CohortRegistry()
    register_memory_cohort(registry, svc)

    async def fan_emit(entry):
        pass

    fan_out = CohortFanOutRunner(
        registry=registry,
        audit_emitter=fan_emit,
        config=CohortFanOutConfig(global_timeout_seconds=2.0),
    )
    fan_out_result = await fan_out.run(_ctx())

    inputs = IntegrationInputs(
        user_message="anything I should remember about user prefs?",
        conversation_thread=({"role": "user", "content": "remind me"},),
        cohort_outputs=fan_out_result.outputs,
        surfaced_tools=(),
        active_context_spaces=({"space_id": "default", "domain": "general"},),
        member_id="m-active",
        instance_id="i-1",
        space_id="default",
        turn_id="turn-1",
    )

    async def chain(*_a, **_kw):
        return ProviderResponse(
            content=[
                ContentBlock(
                    type="tool_use",
                    id="tu_finalize",
                    name="__finalize_briefing__",
                    input={
                        "relevant_context": [
                            {
                                "source_type": "cohort.memory",
                                "source_id": "turn-1:memory:0",
                                "summary": "user prefers terse responses",
                                "confidence": 0.85,
                            }
                        ],
                        "filtered_context": [],
                        "decided_action": {"kind": "respond_only"},
                        "presence_directive": "be concise",
                    },
                )
            ],
            stop_reason="tool_use",
            input_tokens=10,
            output_tokens=20,
        )

    async def dispatcher(*_a, **_kw):
        return {}

    async def emit(entry):
        pass

    integration = IntegrationRunner(
        chain_caller=chain,
        read_only_dispatcher=dispatcher,
        audit_emitter=emit,
        config=IntegrationConfig(),
    )
    briefing = await integration.run(inputs)
    assert isinstance(briefing, Briefing)
    assert briefing.audit_trace.fail_soft_engaged is False
    assert isinstance(briefing.decided_action, RespondOnly)
    assert "turn-1:memory:0" in briefing.audit_trace.cohort_outputs


# ---------------------------------------------------------------------------
# 19. remember tool path unchanged (existing tests are the proof)
# ---------------------------------------------------------------------------


def test_scenario_19_remember_tool_path_signature_unchanged():
    """Existing search() public signature unchanged. The full
    test_retrieval.py suite (55 cases) verifies this; here we
    pin the surface."""
    import inspect
    sig = inspect.signature(RetrievalService.search)
    params = list(sig.parameters.keys())
    # self + the 6 documented parameters.
    assert params == [
        "self",
        "instance_id",
        "query",
        "active_space_id",
        "requesting_member_id",
        "instance_db",
        "trace",
    ]


# ---------------------------------------------------------------------------
# 20. search and search_structured share pipeline for matches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_20_search_and_search_structured_consistent():
    entry = _knowledge(id="k-shared", content="shared knowledge piece")
    svc1 = _retrieval_service(knowledge_entries=[entry])
    text = await svc1.search(
        instance_id="i-1",
        query="shared knowledge",
        active_space_id="default",
        requesting_member_id="m-active",
    )
    svc2 = _retrieval_service(knowledge_entries=[entry])
    snap = await svc2.search_structured(
        instance_id="i-1",
        query="shared knowledge",
        active_space_id="default",
        requesting_member_id="m-active",
        include_archives=True,
    )
    assert "shared knowledge piece" in text
    assert any(km.entry_id == "k-shared" for km in snap.knowledge)


# ---------------------------------------------------------------------------
# 21. Archive disclosure gating in remember path (the design review's correction)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_21_archive_gated_off_in_cohort_path():
    """The cohort path always passes include_archives=False. Even
    if compaction.load_index would return content, the archive
    task is never even spawned."""
    svc = _retrieval_service(archive_text="this would otherwise extract")
    run = make_memory_cohort_run(svc)
    out = await run(_ctx())
    assert out.output["archive_summary"] is None
    svc.compaction.load_index.assert_not_called()


# ---------------------------------------------------------------------------
# Bonus: descriptor flags match spec
# ---------------------------------------------------------------------------


def test_descriptor_carries_spec_flags():
    svc = _retrieval_service()
    desc = make_memory_descriptor(svc)
    assert desc.cohort_id == COHORT_ID
    assert desc.timeout_ms == TIMEOUT_MS
    assert isinstance(desc.default_visibility, Public)
    assert desc.required is False
    assert desc.safety_class is False
    from kernos.kernel.cohorts.descriptor import ExecutionMode
    assert desc.execution_mode is ExecutionMode.ASYNC
