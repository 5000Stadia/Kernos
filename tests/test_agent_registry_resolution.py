"""Tests for resolve_natural + ranker fallback + default-agent
priority chain.

DOMAIN-AGENT-REGISTRY C3. Pins:

  - Resolution order (AC #6, AC #7):
      1. Exact agent_id (active only)
      2. Exact alias (active; multiple → Ambiguity)
      3. Optional ranker fallback (only if injected + flag set)
      4. Default-agent three-step priority chain
      5. NotFound
  - Paused records are NOT discovered by natural resolution
    (the design review edit, v1 → v2)
  - Ranker injection works without baked-in dependency (AC #12);
    no ranker = step 3 skipped
  - Default-agent priority chain four cases (AC #16):
      a. space+domain hit
      b. space-only hit when space+domain row absent
      c. domain-only hit when space+domain and space-only absent
      d. neither kwarg → defaults skipped
  - Ambiguity does not silently resolve (AC #7)
  - Workflow registration rejection of `@default:` syntax — that
    pin lives in C4 with the workflow_registry validation, not
    here in the resolver
"""
from __future__ import annotations

import pytest

from kernos.kernel.agents.providers import ProviderRegistry
from kernos.kernel.agents.registry import (
    AgentRecord,
    AgentRegistry,
    AgentResolverRanker,
    Ambiguity,
    Match,
    NotFound,
    RankedCandidate,
)
from kernos.kernel.workflows.agent_inbox import InMemoryAgentInbox


def _record(**overrides) -> AgentRecord:
    base = dict(
        agent_id="some-agent",
        instance_id="inst_a",
        display_name="",
        aliases=[],
        provider_key="inmemory",
        provider_config_ref="x",
        domain_summary="",
        capabilities_summary="",
        status="active",
        version=1,
    )
    base.update(overrides)
    return AgentRecord(**base)


def _provider_registry() -> ProviderRegistry:
    pr = ProviderRegistry()
    pr.register("inmemory", lambda config_ref: InMemoryAgentInbox())
    return pr


@pytest.fixture
async def registry(tmp_path):
    reg = AgentRegistry(provider_registry=_provider_registry())
    await reg.start(str(tmp_path))
    yield reg
    await reg.stop()


# ===========================================================================
# Step 1: exact agent_id
# ===========================================================================


class TestExactAgentId:
    async def test_exact_agent_id_match(self, registry):
        await registry.register_agent(_record(agent_id="spec-agent"))
        result = await registry.resolve_natural("spec-agent", "inst_a")
        assert isinstance(result, Match)
        assert result.record.agent_id == "spec-agent"

    async def test_paused_agent_id_not_resolved(self, registry):
        """Paused records must NOT be returned by resolve_natural;
        only get_by_id returns them. the design review edit v1 → v2."""
        await registry.register_agent(_record(
            agent_id="paused-agent", status="paused",
        ))
        result = await registry.resolve_natural("paused-agent", "inst_a")
        assert isinstance(result, NotFound)

    async def test_retired_agent_id_not_resolved(self, registry):
        await registry.register_agent(_record(
            agent_id="retired-agent", status="retired",
        ))
        result = await registry.resolve_natural("retired-agent", "inst_a")
        assert isinstance(result, NotFound)


# ===========================================================================
# Step 2: exact alias (AC #7)
# ===========================================================================


class TestExactAlias:
    async def test_single_active_alias_match(self, registry):
        await registry.register_agent(_record(
            agent_id="reviewer-a", aliases=["reviewer", "code review"],
        ))
        result = await registry.resolve_natural("reviewer", "inst_a")
        assert isinstance(result, Match)
        assert result.record.agent_id == "reviewer-a"

    async def test_alias_match_case_insensitive(self, registry):
        await registry.register_agent(_record(
            agent_id="reviewer-a", aliases=["Code Review"],
        ))
        result = await registry.resolve_natural("code review", "inst_a")
        assert isinstance(result, Match)

    async def test_two_active_aliases_collide_as_ambiguity(self, registry):
        """Multiple active records claiming the same alias should
        be impossible at registration (alias collision check), but
        the resolver still must handle the case defensively. Use
        update_status to inject the situation: register agent-a
        with alias, pause it, register agent-b with same alias,
        re-activate agent-a — now both active records claim the
        alias."""
        await registry.register_agent(_record(
            agent_id="agent-a", aliases=["reviewer"],
        ))
        await registry.update_status("agent-a", "inst_a", "paused")
        await registry.register_agent(_record(
            agent_id="agent-b", aliases=["reviewer"],
        ))
        await registry.update_status("agent-a", "inst_a", "active")
        # Both active, both claim "reviewer". Resolver must not
        # silently pick.
        result = await registry.resolve_natural("reviewer", "inst_a")
        assert isinstance(result, Ambiguity)
        candidate_ids = {r.agent_id for r in result.candidates}
        assert candidate_ids == {"agent-a", "agent-b"}

    async def test_alias_match_skips_paused_records(self, registry):
        await registry.register_agent(_record(
            agent_id="agent-a", aliases=["reviewer"],
        ))
        await registry.update_status("agent-a", "inst_a", "paused")
        # Paused records' aliases are not natural-discoverable.
        result = await registry.resolve_natural("reviewer", "inst_a")
        assert isinstance(result, NotFound)


# ===========================================================================
# Step 3: ranker fallback (AC #12)
# ===========================================================================


class _StubRanker:
    """Test-only ranker. Returns a hardcoded confidence ordering."""

    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores

    async def rank(
        self, phrase: str, candidates: list[AgentRecord],
    ) -> list[RankedCandidate]:
        return [
            RankedCandidate(record=r, confidence=self.scores.get(r.agent_id, 0.0))
            for r in candidates
        ]


class TestRankerFallback:
    async def test_no_ranker_skips_step_3(self, registry):
        # No ranker bound on the registry fixture. A phrase that
        # doesn't exact-match falls through to step 4 (defaults
        # skipped without scope kwargs) → NotFound.
        await registry.register_agent(_record(
            agent_id="spec-agent", domain_summary="drafts specs",
        ))
        result = await registry.resolve_natural(
            "send to whoever drafts specs", "inst_a",
        )
        assert isinstance(result, NotFound)

    async def test_ranker_clear_winner_returns_match(self, tmp_path):
        ranker = _StubRanker({"agent-a": 0.9, "agent-b": 0.3})
        reg = AgentRegistry(
            provider_registry=_provider_registry(), ranker=ranker,
        )
        await reg.start(str(tmp_path))
        try:
            await reg.register_agent(_record(
                agent_id="agent-a", domain_summary="drafts specs",
            ))
            await reg.register_agent(_record(
                agent_id="agent-b", domain_summary="reviews invoices",
            ))
            result = await reg.resolve_natural(
                "spec drafter please", "inst_a",
            )
            assert isinstance(result, Match)
            assert result.record.agent_id == "agent-a"
        finally:
            await reg.stop()

    async def test_ranker_close_scores_returns_ambiguity(self, tmp_path):
        ranker = _StubRanker({"agent-a": 0.6, "agent-b": 0.55})
        reg = AgentRegistry(
            provider_registry=_provider_registry(), ranker=ranker,
        )
        await reg.start(str(tmp_path))
        try:
            await reg.register_agent(_record(agent_id="agent-a"))
            await reg.register_agent(_record(agent_id="agent-b"))
            result = await reg.resolve_natural("ambiguous phrase", "inst_a")
            assert isinstance(result, Ambiguity)
            assert {r.agent_id for r in result.candidates} == {
                "agent-a", "agent-b",
            }
        finally:
            await reg.stop()

    async def test_allow_llm_fallback_false_skips_ranker(self, tmp_path):
        ranker = _StubRanker({"agent-a": 0.99})
        reg = AgentRegistry(
            provider_registry=_provider_registry(), ranker=ranker,
        )
        await reg.start(str(tmp_path))
        try:
            await reg.register_agent(_record(agent_id="agent-a"))
            # Even with a ranker bound, the flag opts out.
            result = await reg.resolve_natural(
                "any natural phrase", "inst_a",
                allow_llm_fallback=False,
            )
            assert isinstance(result, NotFound)
        finally:
            await reg.stop()


# ===========================================================================
# Step 4: default-agent priority chain (AC #16)
# ===========================================================================


class TestDefaultAgentChain:
    async def test_space_plus_domain_match_at_step_1(self, registry):
        # Register agents.
        await registry.register_agent(_record(agent_id="cc-batch-reviewer"))
        await registry.register_agent(_record(agent_id="work-default"))
        await registry.register_agent(_record(agent_id="invoicing-agent"))
        # Register defaults at all three tiers.
        await registry.register_default(
            "inst_a", "cc-batch-reviewer",
            space_id="work", domain_label="code-review",
        )
        await registry.register_default(
            "inst_a", "work-default", space_id="work",
        )
        await registry.register_default(
            "inst_a", "invoicing-agent", domain_label="invoicing",
        )
        # Resolver with both kwargs → most-specific wins.
        result = await registry.resolve_natural(
            "no exact match phrase", "inst_a",
            space_id="work", domain_label="code-review",
        )
        assert isinstance(result, Match)
        assert result.record.agent_id == "cc-batch-reviewer"

    async def test_space_only_match_at_step_2(self, registry):
        await registry.register_agent(_record(agent_id="work-default"))
        await registry.register_default(
            "inst_a", "work-default", space_id="work",
        )
        # Caller passes a domain_label that has no most-specific
        # row; falls through to space-only.
        result = await registry.resolve_natural(
            "no match", "inst_a",
            space_id="work", domain_label="some-domain",
        )
        assert isinstance(result, Match)
        assert result.record.agent_id == "work-default"

    async def test_domain_only_match_at_step_3(self, registry):
        await registry.register_agent(_record(agent_id="invoicing-agent"))
        await registry.register_default(
            "inst_a", "invoicing-agent", domain_label="invoicing",
        )
        # Caller passes both kwargs; only domain-only row exists.
        result = await registry.resolve_natural(
            "no match", "inst_a",
            space_id="some-space", domain_label="invoicing",
        )
        assert isinstance(result, Match)
        assert result.record.agent_id == "invoicing-agent"

    async def test_no_scope_kwargs_skips_default_resolution(self, registry):
        await registry.register_agent(_record(agent_id="default-agent"))
        await registry.register_default(
            "inst_a", "default-agent", space_id="work",
        )
        # Neither kwarg passed — defaults skipped entirely.
        result = await registry.resolve_natural(
            "no match no scope", "inst_a",
        )
        assert isinstance(result, NotFound)

    async def test_default_pointing_at_paused_agent_returns_notfound(
        self, registry,
    ):
        """If the default's target was paused after registration,
        the resolver re-checks status before returning Match. A
        paused default does not produce a Match — fall through to
        NotFound."""
        await registry.register_agent(_record(agent_id="paused-default"))
        await registry.register_default(
            "inst_a", "paused-default", domain_label="drafting",
        )
        await registry.update_status("paused-default", "inst_a", "paused")
        result = await registry.resolve_natural(
            "no match", "inst_a", domain_label="drafting",
        )
        assert isinstance(result, NotFound)


# ===========================================================================
# Cross-instance isolation
# ===========================================================================


class TestCrossInstanceResolution:
    async def test_resolve_natural_scoped_to_instance(self, registry):
        await registry.register_agent(_record(
            agent_id="agent-a", instance_id="inst_a", aliases=["reviewer"],
        ))
        # Same alias query against inst_b finds nothing.
        result = await registry.resolve_natural("reviewer", "inst_b")
        assert isinstance(result, NotFound)

    async def test_register_default_invalid_scope_rejected(self, registry):
        with pytest.raises(ValueError, match="at least one"):
            await registry.register_default("inst_a", "agent-a")


# ===========================================================================
# Empty / edge cases
# ===========================================================================


class TestEdgeCases:
    async def test_empty_phrase_returns_notfound(self, registry):
        result = await registry.resolve_natural("", "inst_a")
        assert isinstance(result, NotFound)

    async def test_whitespace_phrase_returns_notfound(self, registry):
        result = await registry.resolve_natural("   ", "inst_a")
        assert isinstance(result, NotFound)

    async def test_no_active_records_returns_notfound(self, registry):
        result = await registry.resolve_natural("anything", "inst_a")
        assert isinstance(result, NotFound)
