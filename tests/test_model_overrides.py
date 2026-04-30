"""Schema + InstanceDB mutator tests for model_overrides
(MODEL-AND-STATUS-V1, C1).

Covers AC #1 substrate (table + columns + CHECK + composite PK),
AC #3 / AC #5 storage half (chain switch + head override + reset
mutators), and the spec's idempotent same-value pin.
"""
from __future__ import annotations

import aiosqlite
import pytest

from kernos.kernel.instance_db import InstanceDB


@pytest.fixture
async def db(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    yield idb
    await idb.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchema:
    async def test_table_exists(self, db):
        async with db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='model_overrides'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None

    async def test_columns_present(self, db):
        async with db._conn.execute(
            "PRAGMA table_info(model_overrides)"
        ) as cur:
            cols = {r[1] for r in await cur.fetchall()}
        for required in (
            "instance_id", "member_id", "space_id",
            "chain_name", "override_provider", "override_model",
            "set_at",
        ):
            assert required in cols, f"missing column: {required}"

    async def test_composite_primary_key(self, db):
        async with db._conn.execute(
            "PRAGMA table_info(model_overrides)"
        ) as cur:
            pk_cols = sorted(
                r[1] for r in await cur.fetchall() if r[5] > 0
            )
        assert pk_cols == ["instance_id", "member_id", "space_id"]

    async def test_check_constraint_rejects_partial_pair(self, db):
        """Both override_provider and override_model must be set or
        both null. Spec section 'Storage' SQL CHECK clause."""
        with pytest.raises(aiosqlite.IntegrityError):
            await db._conn.execute(
                "INSERT INTO model_overrides "
                "(instance_id, member_id, space_id, chain_name, "
                " override_provider, override_model, set_at) "
                "VALUES (?, ?, ?, NULL, ?, NULL, ?)",
                ("i", "m", "s", "anthropic", "2026-04-30T00:00:00Z"),
            )
        with pytest.raises(aiosqlite.IntegrityError):
            await db._conn.execute(
                "INSERT INTO model_overrides "
                "(instance_id, member_id, space_id, chain_name, "
                " override_provider, override_model, set_at) "
                "VALUES (?, ?, ?, NULL, NULL, ?, ?)",
                ("i", "m", "s", "claude-haiku-4.5", "2026-04-30T00:00:00Z"),
            )


# ---------------------------------------------------------------------------
# Mutators
# ---------------------------------------------------------------------------


class TestGetEmpty:
    async def test_returns_none_when_no_row(self, db):
        out = await db.get_model_override(
            instance_id="i", member_id="m", space_id="s",
        )
        assert out is None


class TestSetChain:
    async def test_chain_switch_round_trip(self, db):
        await db.set_model_chain(
            instance_id="i", member_id="m", space_id="s",
            chain_name="lightweight",
        )
        out = await db.get_model_override(
            instance_id="i", member_id="m", space_id="s",
        )
        assert out is not None
        assert out["chain_name"] == "lightweight"
        assert out["override_provider"] is None
        assert out["override_model"] is None
        assert out["set_at"]

    async def test_chain_switch_clears_prior_head_override(self, db):
        """A chain switch resets any previously-set provider/model
        override since the new chain has its own head (spec section
        '/model <chain>')."""
        await db.set_model_head_override(
            instance_id="i", member_id="m", space_id="s",
            provider="anthropic", model="claude-haiku-4.5",
        )
        await db.set_model_chain(
            instance_id="i", member_id="m", space_id="s",
            chain_name="primary",
        )
        out = await db.get_model_override(
            instance_id="i", member_id="m", space_id="s",
        )
        assert out["chain_name"] == "primary"
        assert out["override_provider"] is None
        assert out["override_model"] is None

    async def test_chain_switch_idempotent_same_value(self, db):
        await db.set_model_chain(
            instance_id="i", member_id="m", space_id="s",
            chain_name="primary",
        )
        await db.set_model_chain(
            instance_id="i", member_id="m", space_id="s",
            chain_name="primary",
        )
        out = await db.get_model_override(
            instance_id="i", member_id="m", space_id="s",
        )
        assert out["chain_name"] == "primary"


class TestSetHeadOverride:
    async def test_head_override_round_trip(self, db):
        await db.set_model_head_override(
            instance_id="i", member_id="m", space_id="s",
            provider="anthropic", model="claude-haiku-4.5",
        )
        out = await db.get_model_override(
            instance_id="i", member_id="m", space_id="s",
        )
        assert out is not None
        assert out["chain_name"] is None
        assert out["override_provider"] == "anthropic"
        assert out["override_model"] == "claude-haiku-4.5"

    async def test_head_override_preserves_chain_selection(self, db):
        """Setting a head override after a chain switch keeps the
        chain selection intact — the override prepends the chain
        head; it doesn't replace the chain."""
        await db.set_model_chain(
            instance_id="i", member_id="m", space_id="s",
            chain_name="lightweight",
        )
        await db.set_model_head_override(
            instance_id="i", member_id="m", space_id="s",
            provider="anthropic", model="claude-haiku-4.5",
        )
        out = await db.get_model_override(
            instance_id="i", member_id="m", space_id="s",
        )
        assert out["chain_name"] == "lightweight"
        assert out["override_provider"] == "anthropic"
        assert out["override_model"] == "claude-haiku-4.5"

    async def test_head_override_idempotent_same_value(self, db):
        await db.set_model_head_override(
            instance_id="i", member_id="m", space_id="s",
            provider="anthropic", model="claude-haiku-4.5",
        )
        await db.set_model_head_override(
            instance_id="i", member_id="m", space_id="s",
            provider="anthropic", model="claude-haiku-4.5",
        )
        out = await db.get_model_override(
            instance_id="i", member_id="m", space_id="s",
        )
        assert out["override_provider"] == "anthropic"
        assert out["override_model"] == "claude-haiku-4.5"

    async def test_head_override_replaces_prior_value(self, db):
        await db.set_model_head_override(
            instance_id="i", member_id="m", space_id="s",
            provider="anthropic", model="claude-haiku-4.5",
        )
        await db.set_model_head_override(
            instance_id="i", member_id="m", space_id="s",
            provider="openrouter", model="glm-5.1",
        )
        out = await db.get_model_override(
            instance_id="i", member_id="m", space_id="s",
        )
        assert out["override_provider"] == "openrouter"
        assert out["override_model"] == "glm-5.1"


class TestReset:
    async def test_reset_removes_row(self, db):
        await db.set_model_chain(
            instance_id="i", member_id="m", space_id="s",
            chain_name="lightweight",
        )
        deleted = await db.reset_model_override(
            instance_id="i", member_id="m", space_id="s",
        )
        assert deleted is True
        out = await db.get_model_override(
            instance_id="i", member_id="m", space_id="s",
        )
        assert out is None

    async def test_reset_when_no_row_returns_false(self, db):
        deleted = await db.reset_model_override(
            instance_id="i", member_id="m", space_id="s",
        )
        assert deleted is False


class TestScopeIsolation:
    async def test_per_member_space_isolation(self, db):
        """Overrides set for (member_a, space_a) do not affect
        (member_a, space_b) or (member_b, space_a)."""
        await db.set_model_chain(
            instance_id="i", member_id="ma", space_id="sa",
            chain_name="lightweight",
        )
        await db.set_model_chain(
            instance_id="i", member_id="ma", space_id="sb",
            chain_name="primary",
        )
        sa = await db.get_model_override(
            instance_id="i", member_id="ma", space_id="sa",
        )
        sb = await db.get_model_override(
            instance_id="i", member_id="ma", space_id="sb",
        )
        mb_sa = await db.get_model_override(
            instance_id="i", member_id="mb", space_id="sa",
        )
        assert sa["chain_name"] == "lightweight"
        assert sb["chain_name"] == "primary"
        assert mb_sa is None
