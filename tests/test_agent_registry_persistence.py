"""Tests for AgentRegistry persistence + get_by_id.

DOMAIN-AGENT-REGISTRY C1. Pins:

  - schema persistence (composite PK on agent_records + composite
    PK on default_agents)
  - get_by_id round-trip
  - cross-instance isolation: same agent_id in two instances
    resolves to different records (AC #1, #4)
  - get_by_id returns the record regardless of status (caller
    checks status; registry doesn't filter; AC #5)
  - list_agents filtered by status
  - default_agents row insertion + cross-instance isolation
"""
from __future__ import annotations

import pytest

from kernos.kernel.agents.registry import (
    AgentRecord,
    AgentRegistry,
    DefaultAgentRecord,
)


@pytest.fixture
async def registry(tmp_path):
    reg = AgentRegistry()
    await reg.start(str(tmp_path))
    yield reg
    await reg.stop()


def _basic_record(**overrides) -> AgentRecord:
    base = dict(
        agent_id="spec-agent",
        instance_id="inst_a",
        display_name="Spec drafting agent",
        aliases=["spec drafter", "drafter"],
        provider_key="inmemory",
        provider_config_ref="default-inbox",
        domain_summary="Drafts specifications and reviews their structure.",
        capabilities_summary="Spec writing, review, structural feedback.",
        status="active",
        version=1,
    )
    base.update(overrides)
    return AgentRecord(**base)


# ===========================================================================
# Schema + persistence
# ===========================================================================


class TestSchemaPersistence:
    async def test_insert_and_read_back_round_trip(self, registry):
        record = _basic_record()
        await registry._insert_record(record)
        loaded = await registry.get_by_id("spec-agent", "inst_a")
        assert loaded is not None
        assert loaded.agent_id == "spec-agent"
        assert loaded.instance_id == "inst_a"
        assert loaded.display_name == "Spec drafting agent"
        assert loaded.aliases == ["spec drafter", "drafter"]
        assert loaded.provider_key == "inmemory"
        assert loaded.status == "active"

    async def test_aliases_stored_as_json(self, registry):
        # Empty alias list round-trips.
        await registry._insert_record(_basic_record(
            agent_id="no-aliases", aliases=[],
        ))
        loaded = await registry.get_by_id("no-aliases", "inst_a")
        assert loaded.aliases == []

    async def test_created_at_auto_filled(self, registry):
        record = _basic_record()
        record.created_at = ""  # let the registry fill in
        await registry._insert_record(record)
        loaded = await registry.get_by_id("spec-agent", "inst_a")
        assert loaded.created_at  # non-empty ISO 8601


# ===========================================================================
# Cross-instance non-collision (AC #1, #4)
# ===========================================================================


class TestCrossInstanceNonCollision:
    """The composite PK ``(instance_id, agent_id)`` is the structural
    enforcement of multi-tenancy. Two instances may both have a
    ``spec-agent`` without colliding."""

    async def test_same_agent_id_different_instances_persist_independently(
        self, registry,
    ):
        await registry._insert_record(_basic_record(
            agent_id="spec-agent", instance_id="inst_a",
            provider_config_ref="inst_a-inbox",
        ))
        await registry._insert_record(_basic_record(
            agent_id="spec-agent", instance_id="inst_b",
            provider_config_ref="inst_b-inbox",
        ))
        # Both rows persist without collision.
        a = await registry.get_by_id("spec-agent", "inst_a")
        b = await registry.get_by_id("spec-agent", "inst_b")
        assert a is not None and b is not None
        assert a.provider_config_ref == "inst_a-inbox"
        assert b.provider_config_ref == "inst_b-inbox"

    async def test_get_by_id_in_other_instance_returns_none(self, registry):
        await registry._insert_record(_basic_record(
            agent_id="instance-a-agent", instance_id="inst_a",
        ))
        # Same agent_id queried against inst_b returns None.
        miss = await registry.get_by_id("instance-a-agent", "inst_b")
        assert miss is None

    async def test_list_agents_scoped_to_instance(self, registry):
        await registry._insert_record(_basic_record(
            agent_id="agent-1", instance_id="inst_a",
        ))
        await registry._insert_record(_basic_record(
            agent_id="agent-2", instance_id="inst_a",
        ))
        await registry._insert_record(_basic_record(
            agent_id="agent-3", instance_id="inst_b",
        ))
        a_agents = await registry.list_agents("inst_a")
        b_agents = await registry.list_agents("inst_b")
        assert {r.agent_id for r in a_agents} == {"agent-1", "agent-2"}
        assert {r.agent_id for r in b_agents} == {"agent-3"}


# ===========================================================================
# Status-agnostic get_by_id (AC #5)
# ===========================================================================


class TestStatusAgnosticLookup:
    """``get_by_id`` returns the record regardless of status. The
    caller (``RouteToAgentAction`` in C4) checks ``record.status``
    and raises typed errors for ``paused`` / ``retired``. This is
    the design review's v1 → v2 lifecycle clarification."""

    async def test_get_by_id_returns_paused_record(self, registry):
        await registry._insert_record(_basic_record(status="paused"))
        loaded = await registry.get_by_id("spec-agent", "inst_a")
        assert loaded is not None
        assert loaded.status == "paused"

    async def test_get_by_id_returns_retired_record(self, registry):
        await registry._insert_record(_basic_record(status="retired"))
        loaded = await registry.get_by_id("spec-agent", "inst_a")
        assert loaded is not None
        assert loaded.status == "retired"

    async def test_list_agents_filter_by_status(self, registry):
        await registry._insert_record(_basic_record(
            agent_id="active-agent", status="active",
        ))
        await registry._insert_record(_basic_record(
            agent_id="paused-agent", status="paused",
        ))
        await registry._insert_record(_basic_record(
            agent_id="retired-agent", status="retired",
        ))
        active = await registry.list_agents("inst_a", status="active")
        paused = await registry.list_agents("inst_a", status="paused")
        retired = await registry.list_agents("inst_a", status="retired")
        assert {r.agent_id for r in active} == {"active-agent"}
        assert {r.agent_id for r in paused} == {"paused-agent"}
        assert {r.agent_id for r in retired} == {"retired-agent"}


# ===========================================================================
# Composite PK rejects duplicate (instance_id, agent_id)
# ===========================================================================


class TestCompositePrimaryKey:
    async def test_duplicate_composite_key_rejected(self, registry):
        """The composite PK is a structural integrity boundary —
        same ``(instance_id, agent_id)`` cannot be inserted twice.
        C2's register_agent will catch this earlier with a clean
        error; the SQL constraint is the backstop."""
        await registry._insert_record(_basic_record())
        import aiosqlite
        with pytest.raises(aiosqlite.IntegrityError):
            await registry._insert_record(_basic_record())


# ===========================================================================
# default_agents table (foundation for C3)
# ===========================================================================


class TestDefaultAgentsTable:
    async def test_insert_default_round_trip(self, registry):
        # Most-specific row: space + domain.
        await registry._insert_default(DefaultAgentRecord(
            instance_id="inst_a",
            scope_kind="space_id",
            scope_id="work",
            domain_label="code-review",
            agent_id="cc-batch-reviewer",
        ))
        # Verify by querying directly (resolver in C3).
        async with registry._db.execute(
            "SELECT * FROM default_agents WHERE instance_id = ?",
            ("inst_a",),
        ) as cur:
            rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["agent_id"] == "cc-batch-reviewer"
        assert rows[0]["scope_id"] == "work"
        assert rows[0]["domain_label"] == "code-review"

    async def test_default_agents_cross_instance_isolated(self, registry):
        await registry._insert_default(DefaultAgentRecord(
            instance_id="inst_a", scope_kind="domain",
            scope_id="", domain_label="invoicing",
            agent_id="inst-a-invoicer",
        ))
        await registry._insert_default(DefaultAgentRecord(
            instance_id="inst_b", scope_kind="domain",
            scope_id="", domain_label="invoicing",
            agent_id="inst-b-invoicer",
        ))
        async with registry._db.execute(
            "SELECT agent_id FROM default_agents WHERE instance_id = ?",
            ("inst_a",),
        ) as cur:
            rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["agent_id"] == "inst-a-invoicer"


# ===========================================================================
# Lifecycle
# ===========================================================================


class TestLifecycle:
    async def test_start_idempotent(self, registry):
        # Second start() is a no-op.
        await registry.start("/tmp/should-not-be-used")
        # Still works.
        await registry._insert_record(_basic_record())
        loaded = await registry.get_by_id("spec-agent", "inst_a")
        assert loaded is not None

    async def test_stop_idempotent(self, tmp_path):
        reg = AgentRegistry()
        await reg.start(str(tmp_path))
        await reg.stop()
        await reg.stop()  # no raise

    async def test_get_by_id_before_start_returns_none(self):
        reg = AgentRegistry()
        # Not started — connection is None.
        result = await reg.get_by_id("anything", "inst_a")
        assert result is None
