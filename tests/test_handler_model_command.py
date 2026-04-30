"""Handler integration tests for /model + /status Models block
(MODEL-AND-STATUS-V1, C2).

Tests the dispatch + rendering layer above InstanceDB and
model_routing. Uses the real handler fixture (`_make_handler`) with
a synthetic ChainConfig injected onto the ReasoningService and a
real InstanceDB on tmp_path.

Pins AC #1 (Models block in /status), AC #2 (no-args /model list),
AC #3 (chain switch), AC #4 (head override happy + reject), AC #5
(reset clears both fields), and the spec's stale-config marker
behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.spaces import ContextSpace

from tests.test_handler import _make_handler


# ---------------------------------------------------------------------------
# Stub chain config injection
# ---------------------------------------------------------------------------


@dataclass
class _StubProvider:
    provider_name: str


@dataclass
class _Entry:
    provider: _StubProvider
    model: str


def _stub_chains():
    anthropic = _StubProvider("anthropic")
    openrouter = _StubProvider("openrouter")
    return {
        "primary": [
            _Entry(anthropic, "claude-sonnet-4.6"),
            _Entry(openrouter, "glm-5.1"),
        ],
        "lightweight": [
            _Entry(anthropic, "claude-haiku-4.5"),
            _Entry(openrouter, "glm-5.1-flash"),
        ],
    }


@pytest.fixture
async def stack(tmp_path):
    handler, _ = _make_handler()
    # Replace the abstract chain config on the reasoning service with
    # a synthetic one we control.
    handler.reasoning._chains = _stub_chains()
    # Real InstanceDB on tmp_path so override mutators round-trip.
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    handler._instance_db = idb
    yield handler, idb
    await idb.close()


def _ctx(handler, *, member_id="mem_a", space_id="sp_a"):
    """Build a minimal TurnContext sufficient for the Models block
    and /model dispatch. Real handler tests construct full contexts;
    here we only need member_id, instance_id, and active_space_id."""
    from kernos.messages.handler import TurnContext
    space = ContextSpace(
        id=space_id, instance_id="inst_a", name="Test Space",
        member_id=member_id,
    )
    ctx = TurnContext(
        instance_id="inst_a",
        member_id=member_id,
        active_space=space,
        active_space_id=space_id,
        member_profile={"display_name": "Alice"},
    )
    return ctx


# ---------------------------------------------------------------------------
# /model — no args (list)
# ---------------------------------------------------------------------------


class TestModelListNoArgs:
    async def test_lists_chains_with_active_marker(self, stack):
        handler, _ = stack
        ctx = _ctx(handler)
        out = await handler._handle_model_command(ctx, "/model")
        assert "Active chain: primary" in out
        assert "Effective head: anthropic/claude-sonnet-4.6 (active)" in out
        assert "Available chains:" in out
        assert "• primary (active) —" in out
        assert "• lightweight —" in out
        assert "Switch with:" in out

    async def test_reflects_persisted_chain_switch(self, stack):
        handler, idb = stack
        await idb.set_model_chain(
            instance_id="inst_a", member_id="mem_a", space_id="sp_a",
            chain_name="lightweight",
        )
        ctx = _ctx(handler)
        out = await handler._handle_model_command(ctx, "/model")
        assert "Active chain: lightweight" in out
        assert "Effective head: anthropic/claude-haiku-4.5 (active)" in out


# ---------------------------------------------------------------------------
# /model <chain>
# ---------------------------------------------------------------------------


class TestModelChainSwitch:
    async def test_switch_to_lightweight(self, stack):
        handler, idb = stack
        ctx = _ctx(handler)
        out = await handler._handle_model_command(ctx, "/model lightweight")
        assert "Switched chain to **lightweight**" in out
        assert "claude-haiku-4.5" in out
        # Persisted.
        row = await idb.get_model_override(
            instance_id="inst_a", member_id="mem_a", space_id="sp_a",
        )
        assert row["chain_name"] == "lightweight"
        assert row["override_provider"] is None

    async def test_switch_clears_prior_head_override(self, stack):
        handler, idb = stack
        ctx = _ctx(handler)
        await handler._handle_model_command(
            ctx, "/model anthropic/claude-haiku-4.5",
        )
        await handler._handle_model_command(ctx, "/model primary")
        row = await idb.get_model_override(
            instance_id="inst_a", member_id="mem_a", space_id="sp_a",
        )
        assert row["chain_name"] == "primary"
        assert row["override_provider"] is None
        assert row["override_model"] is None


# ---------------------------------------------------------------------------
# /model <provider>/<model>
# ---------------------------------------------------------------------------


class TestModelHeadOverride:
    async def test_head_override_in_chain_accepts(self, stack):
        handler, idb = stack
        ctx = _ctx(handler)
        out = await handler._handle_model_command(
            ctx, "/model anthropic/claude-haiku-4.5",
        )
        assert (
            "Override head set to **anthropic/claude-haiku-4.5**"
        ) in out
        assert "preferred first attempt" in out
        row = await idb.get_model_override(
            instance_id="inst_a", member_id="mem_a", space_id="sp_a",
        )
        assert row["override_provider"] == "anthropic"
        assert row["override_model"] == "claude-haiku-4.5"

    async def test_head_override_unknown_rejected_with_available_list(
        self, stack,
    ):
        handler, _ = stack
        ctx = _ctx(handler)
        out = await handler._handle_model_command(
            ctx, "/model imaginary/ghost-1",
        )
        assert "is not in any configured chain" in out
        assert "anthropic/claude-sonnet-4.6" in out
        assert "openrouter/glm-5.1-flash" in out


# ---------------------------------------------------------------------------
# /model reset
# ---------------------------------------------------------------------------


class TestModelReset:
    async def test_reset_clears_chain_and_override(self, stack):
        handler, idb = stack
        ctx = _ctx(handler)
        await handler._handle_model_command(ctx, "/model lightweight")
        out = await handler._handle_model_command(ctx, "/model reset")
        assert "Cleared model override" in out
        row = await idb.get_model_override(
            instance_id="inst_a", member_id="mem_a", space_id="sp_a",
        )
        assert row is None

    async def test_reset_with_no_override_returns_friendly_message(
        self, stack,
    ):
        handler, _ = stack
        ctx = _ctx(handler)
        out = await handler._handle_model_command(ctx, "/model reset")
        assert "No override was set" in out


# ---------------------------------------------------------------------------
# Unknown args → usage
# ---------------------------------------------------------------------------


class TestModelUnknownArgs:
    async def test_unknown_returns_usage(self, stack):
        handler, _ = stack
        ctx = _ctx(handler)
        out = await handler._handle_model_command(ctx, "/model nonsense")
        assert "Usage:" in out
        assert "/model <chain>" in out
        assert "/model <provider>/<model>" in out


# ---------------------------------------------------------------------------
# /status Models block
# ---------------------------------------------------------------------------


class TestStatusModelsBlock:
    async def test_models_block_shows_active_chain_and_head(self, stack):
        handler, _ = stack
        ctx = _ctx(handler)
        block = await handler._render_models_block(ctx)
        assert block.startswith("**Models** (this space)")
        assert "Active chain: primary" in block
        assert "Effective head: anthropic/claude-sonnet-4.6" in block
        assert "Fallback: openrouter/glm-5.1" in block

    async def test_models_block_shows_override_line(self, stack):
        handler, idb = stack
        await idb.set_model_chain(
            instance_id="inst_a", member_id="mem_a", space_id="sp_a",
            chain_name="lightweight",
        )
        ctx = _ctx(handler)
        block = await handler._render_models_block(ctx)
        assert "Override (this space): chain=lightweight" in block

    async def test_models_block_shows_head_override_line(self, stack):
        handler, idb = stack
        await idb.set_model_head_override(
            instance_id="inst_a", member_id="mem_a", space_id="sp_a",
            provider="anthropic", model="claude-haiku-4.5",
        )
        ctx = _ctx(handler)
        block = await handler._render_models_block(ctx)
        assert "Override (this space):" in block
        assert "head=anthropic/claude-haiku-4.5" in block

    async def test_no_override_no_override_line(self, stack):
        handler, _ = stack
        ctx = _ctx(handler)
        block = await handler._render_models_block(ctx)
        assert "Override (this space):" not in block

    async def test_stale_chain_name_surfaces_in_block(self, stack):
        """Persisted override naming a chain that's been removed
        from env should render the unavailable marker."""
        handler, idb = stack
        await idb.set_model_chain(
            instance_id="inst_a", member_id="mem_a", space_id="sp_a",
            chain_name="vintage",  # not in current chains
        )
        ctx = _ctx(handler)
        block = await handler._render_models_block(ctx)
        assert "vintage" in block
        assert "unavailable" in block

    async def test_stale_head_spec_surfaces_in_block(self, stack):
        handler, idb = stack
        await idb.set_model_head_override(
            instance_id="inst_a", member_id="mem_a", space_id="sp_a",
            provider="imaginary", model="ghost-1",
        )
        ctx = _ctx(handler)
        block = await handler._render_models_block(ctx)
        assert "imaginary/ghost-1" in block
        assert "unavailable" in block


# ---------------------------------------------------------------------------
# /model with stale persisted override (read-only side, no auto-delete)
# ---------------------------------------------------------------------------


class TestStaleConfigInModelList:
    async def test_stale_chain_does_not_auto_delete(self, stack):
        """Spec section 'Stale-config behavior': stale row is NOT
        auto-deleted; the user can /model reset explicitly."""
        handler, idb = stack
        await idb.set_model_chain(
            instance_id="inst_a", member_id="mem_a", space_id="sp_a",
            chain_name="vintage",
        )
        ctx = _ctx(handler)
        # /model list reads the stale row and surfaces the marker.
        out = await handler._handle_model_command(ctx, "/model")
        assert "vintage" in out
        assert "unavailable" in out
        # Row still present.
        row = await idb.get_model_override(
            instance_id="inst_a", member_id="mem_a", space_id="sp_a",
        )
        assert row is not None
        assert row["chain_name"] == "vintage"
