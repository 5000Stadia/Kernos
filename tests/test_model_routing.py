"""Pure-function tests for the chain-and-head resolver
(MODEL-AND-STATUS-V1, C2 supporting).

Pins the load-bearing helpers in ``kernos.kernel.model_routing`` —
the same module consumed by both ``_handle_status`` /
``_handle_model_command`` (rendering) and ``ReasoningService``
(dispatch).
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from kernos.kernel.model_routing import (
    EffectiveChain,
    find_entry_in_any_chain,
    head_spec_in_any_chain,
    list_configured_entries,
    parse_provider_model_spec,
    resolve_effective_chain,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubProvider:
    provider_name: str


@dataclass
class _Entry:
    """Mirrors ChainEntry shape (provider + model)."""
    provider: _StubProvider
    model: str


def _chains():
    """Two-chain config matching production (primary + lightweight)."""
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


# ---------------------------------------------------------------------------
# parse_provider_model_spec
# ---------------------------------------------------------------------------


class TestParseSpec:
    def test_valid(self):
        assert parse_provider_model_spec("anthropic/claude-sonnet-4.6") == (
            "anthropic", "claude-sonnet-4.6",
        )

    def test_strips_whitespace(self):
        assert parse_provider_model_spec("  anthropic / claude-haiku-4.5  ") == (
            "anthropic", "claude-haiku-4.5",
        )

    def test_no_slash_returns_none(self):
        assert parse_provider_model_spec("primary") is None
        assert parse_provider_model_spec("anthropic") is None

    def test_empty_halves_return_none(self):
        assert parse_provider_model_spec("/claude-sonnet-4.6") is None
        assert parse_provider_model_spec("anthropic/") is None
        assert parse_provider_model_spec("/") is None

    def test_empty_string_returns_none(self):
        assert parse_provider_model_spec("") is None


# ---------------------------------------------------------------------------
# list_configured_entries / head_spec_in_any_chain
# ---------------------------------------------------------------------------


class TestListConfiguredEntries:
    def test_returns_unique_pairs_in_order(self):
        entries = list_configured_entries(_chains())
        assert entries == [
            ("anthropic", "claude-sonnet-4.6"),
            ("openrouter", "glm-5.1"),
            ("anthropic", "claude-haiku-4.5"),
            ("openrouter", "glm-5.1-flash"),
        ]

    def test_dedupes_across_chains(self):
        anthropic = _StubProvider("anthropic")
        chains = {
            "primary": [_Entry(anthropic, "shared")],
            "lightweight": [_Entry(anthropic, "shared")],
        }
        assert list_configured_entries(chains) == [("anthropic", "shared")]


class TestHeadSpecInAnyChain:
    def test_match_in_primary(self):
        chains = _chains()
        assert head_spec_in_any_chain(chains, "anthropic", "claude-sonnet-4.6")

    def test_match_in_lightweight(self):
        chains = _chains()
        assert head_spec_in_any_chain(chains, "anthropic", "claude-haiku-4.5")

    def test_no_match(self):
        chains = _chains()
        assert not head_spec_in_any_chain(chains, "anthropic", "claude-imaginary")
        assert not head_spec_in_any_chain(chains, "imaginary", "any-model")


class TestFindEntryInAnyChain:
    def test_returns_actual_entry_with_provider(self):
        chains = _chains()
        entry = find_entry_in_any_chain(chains, "openrouter", "glm-5.1-flash")
        assert entry is not None
        assert entry.model == "glm-5.1-flash"
        assert entry.provider.provider_name == "openrouter"

    def test_missing_returns_none(self):
        chains = _chains()
        assert find_entry_in_any_chain(chains, "imaginary", "any") is None


# ---------------------------------------------------------------------------
# resolve_effective_chain
# ---------------------------------------------------------------------------


class TestResolveEffectiveChainNoOverride:
    def test_default_primary(self):
        eff = resolve_effective_chain(
            chains=_chains(), requested_chain="primary", override=None,
        )
        assert eff.chain_name == "primary"
        assert eff.head_provider == "anthropic"
        assert eff.head_model == "claude-sonnet-4.6"
        assert len(eff.entries) == 2
        assert eff.override_in_effect is False
        assert eff.stale_chain_name is None
        assert eff.stale_head_spec is None

    def test_default_lightweight(self):
        eff = resolve_effective_chain(
            chains=_chains(), requested_chain="lightweight", override=None,
        )
        assert eff.chain_name == "lightweight"
        assert eff.head_provider == "anthropic"
        assert eff.head_model == "claude-haiku-4.5"


class TestResolveEffectiveChainSwitchesChain:
    def test_chain_switch_overrides_request(self):
        override = {
            "chain_name": "lightweight",
            "override_provider": None,
            "override_model": None,
        }
        eff = resolve_effective_chain(
            chains=_chains(), requested_chain="primary", override=override,
        )
        assert eff.chain_name == "lightweight"
        assert eff.head_provider == "anthropic"
        assert eff.head_model == "claude-haiku-4.5"
        assert eff.override_in_effect is True

    def test_same_as_requested_is_not_in_effect(self):
        """Override that names the chain already requested is a no-op
        from the user's perspective — override_in_effect stays False
        so the handler doesn't render an Override line for nothing."""
        override = {
            "chain_name": "primary",
            "override_provider": None,
            "override_model": None,
        }
        eff = resolve_effective_chain(
            chains=_chains(), requested_chain="primary", override=override,
        )
        assert eff.chain_name == "primary"
        assert eff.override_in_effect is False


class TestResolveEffectiveChainHeadOverride:
    def test_head_override_within_active_chain_dedupes(self):
        """When the override head is already in the active chain
        (primary), it moves to position 0 and the duplicate is
        removed from later positions."""
        override = {
            "chain_name": None,
            "override_provider": "openrouter",
            "override_model": "glm-5.1",
        }
        eff = resolve_effective_chain(
            chains=_chains(), requested_chain="primary", override=override,
        )
        assert eff.head_provider == "openrouter"
        assert eff.head_model == "glm-5.1"
        # Length stays 2 because dedupe removed the original second entry.
        assert len(eff.entries) == 2
        assert eff.entries[0].provider.provider_name == "openrouter"
        assert eff.entries[1].provider.provider_name == "anthropic"
        assert eff.override_in_effect is True

    def test_head_override_from_other_chain_prepended(self):
        """When the override head is in a DIFFERENT chain than the
        active one, prepend it to the active chain's entries
        (sourced from the other chain to preserve Provider ref)."""
        override = {
            "chain_name": None,
            "override_provider": "anthropic",
            "override_model": "claude-haiku-4.5",  # only in lightweight
        }
        eff = resolve_effective_chain(
            chains=_chains(), requested_chain="primary", override=override,
        )
        assert eff.chain_name == "primary"
        assert eff.head_provider == "anthropic"
        assert eff.head_model == "claude-haiku-4.5"
        # Three entries: override head + two original primary entries.
        assert len(eff.entries) == 3
        assert eff.override_in_effect is True

    def test_head_override_combined_with_chain_switch(self):
        """Both override fields can be set together — chain switch
        chooses the chain, head override prepends to it."""
        override = {
            "chain_name": "lightweight",
            "override_provider": "openrouter",
            "override_model": "glm-5.1",  # primary chain entry
        }
        eff = resolve_effective_chain(
            chains=_chains(), requested_chain="primary", override=override,
        )
        assert eff.chain_name == "lightweight"
        assert eff.head_provider == "openrouter"
        assert eff.head_model == "glm-5.1"
        # Lightweight has 2 entries; override prepends a third (no
        # dedupe match because glm-5.1 is not in lightweight).
        assert len(eff.entries) == 3
        assert eff.override_in_effect is True


class TestResolveEffectiveChainStale:
    def test_stale_chain_name_falls_back_to_requested(self):
        override = {
            "chain_name": "vintage",  # not in chains
            "override_provider": None,
            "override_model": None,
        }
        eff = resolve_effective_chain(
            chains=_chains(), requested_chain="primary", override=override,
        )
        assert eff.chain_name == "primary"  # fell back
        assert eff.stale_chain_name == "vintage"
        assert eff.override_in_effect is False  # nothing actually applied

    def test_stale_head_spec_skips_prepend(self):
        override = {
            "chain_name": None,
            "override_provider": "imaginary",
            "override_model": "ghost-1",
        }
        eff = resolve_effective_chain(
            chains=_chains(), requested_chain="primary", override=override,
        )
        # Effective head is the natural primary head, not the stale spec.
        assert eff.head_provider == "anthropic"
        assert eff.head_model == "claude-sonnet-4.6"
        assert eff.stale_head_spec == "imaginary/ghost-1"
        assert eff.override_in_effect is False

    def test_stale_chain_with_valid_head_override(self):
        """Stale chain_name does not poison a valid head override —
        each stale check is independent."""
        override = {
            "chain_name": "vintage",  # stale
            "override_provider": "anthropic",
            "override_model": "claude-haiku-4.5",  # valid
        }
        eff = resolve_effective_chain(
            chains=_chains(), requested_chain="primary", override=override,
        )
        assert eff.chain_name == "primary"  # fell back from stale
        assert eff.stale_chain_name == "vintage"
        # Valid head override still applied.
        assert eff.head_model == "claude-haiku-4.5"
        assert eff.override_in_effect is True


class TestResolveEffectiveChainEdgeCases:
    def test_empty_chains_returns_synthetic_shell(self):
        eff = resolve_effective_chain(
            chains={}, requested_chain="primary", override=None,
        )
        assert eff.entries == ()
        assert eff.head_provider == "(none)"
        assert eff.head_model == "(none)"

    def test_unknown_requested_chain_falls_back_to_primary(self):
        eff = resolve_effective_chain(
            chains=_chains(), requested_chain="vintage", override=None,
        )
        # No primary key match either → primary fallback
        assert eff.chain_name == "vintage"  # requested name surfaces
        assert eff.head_provider == "anthropic"
        # The first primary entry is the head.
        assert eff.head_model == "claude-sonnet-4.6"
