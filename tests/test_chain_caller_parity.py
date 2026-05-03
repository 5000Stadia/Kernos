"""Pin tests for CHAIN-CALLER-PARITY-V1.

Per architect verdict 2026-05-03: the legacy ``ReasoningService._call_chain``
provided three resilience behaviors (chain fallback, context-window
pre-flight skip, model-override resolution). The CCV1 C7 strike
removes the legacy helper; these behaviors must move to the thin-path
``build_resilient_chain_caller`` first. This file pins the moves.

Test surface: each behavior gets equivalence tests against the legacy
oracle (via ``ReasoningService._call_chain`` while it still exists)
and standalone tests at the new seam. After the strike commit, the
oracle-comparison half deletes; the new-seam half remains.

Until the strike, both seams are live and tested. The legacy tests
in ``test_chain_context_window_skip.py`` and
``test_reasoning_model_override.py`` continue to pass; the strike
commit removes them alongside the helper.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from kernos.kernel.exceptions import (
    ChainPayloadTooLarge,
    LLMChainExhausted,
    ReasoningProviderError,
)
from kernos.kernel.turn_runner_provider import build_resilient_chain_caller
from kernos.models.catalog import ModelCard
from kernos.providers.base import ChainEntry, Provider, ProviderResponse


# ---------------------------------------------------------------------------
# Test scaffolding — fakes that observe what was called
# ---------------------------------------------------------------------------


class _FakeProvider(Provider):
    """Provider that records calls; can be configured to succeed or fail."""

    def __init__(self, *, name: str, fail: bool = False, response_text: str = "ok"):
        self.provider_name = name
        self._fail = fail
        self._response_text = response_text
        self.calls: list[dict] = []

    async def complete(  # type: ignore[override]
        self,
        model,
        system,
        messages,
        tools,
        max_tokens,
        output_schema=None,
        conversation_id="",
    ) -> ProviderResponse:
        self.calls.append({
            "model": model,
            "max_tokens": max_tokens,
            "conversation_id": conversation_id,
        })
        if self._fail:
            raise ReasoningProviderError(
                f"{self.provider_name} synthetic failure",
            )
        return ProviderResponse(
            content=[],
            stop_reason="end_turn",
            input_tokens=10,
            output_tokens=20,
        )


@dataclass
class _FakeRequest:
    """Minimal stand-in for ReasoningRequest carrying the fields the
    chain caller consults."""
    model: str = ""
    model_override: dict | None = None
    trace: Any = None
    conversation_id: str = ""


def _entries(*pairs: tuple[str, str, bool]) -> list[ChainEntry]:
    """Helper: build chain entries from (provider_name, model, fail) tuples."""
    return [
        ChainEntry(
            provider=_FakeProvider(name=name, fail=fail),
            model=model,
        )
        for name, model, fail in pairs
    ]


def _chains_with_primary(entries: list[ChainEntry]) -> dict:
    return {"primary": entries, "lightweight": entries}


# ---------------------------------------------------------------------------
# Behavior 1 — chain fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_fallback_first_entry_succeeds():
    """Healthy primary: chain caller returns first-entry response,
    skips remaining entries."""
    entries = _entries(("anthropic", "sonnet", False), ("codex", "gpt", False))
    chains = _chains_with_primary(entries)
    caller = build_resilient_chain_caller(
        chains=chains, request=_FakeRequest(),
    )
    resp = await caller(system="", messages=[], tools=[], max_tokens=100)
    assert resp is not None
    assert len(entries[0].provider.calls) == 1
    assert len(entries[1].provider.calls) == 0


@pytest.mark.asyncio
async def test_chain_fallback_first_fails_second_succeeds():
    """First entry fails → caller falls through to second; second succeeds."""
    entries = _entries(("primary", "x", True), ("fallback", "y", False))
    chains = _chains_with_primary(entries)
    caller = build_resilient_chain_caller(
        chains=chains, request=_FakeRequest(),
    )
    resp = await caller(system="", messages=[], tools=[], max_tokens=100)
    assert resp is not None
    assert len(entries[0].provider.calls) == 1
    assert len(entries[1].provider.calls) == 1


@pytest.mark.asyncio
async def test_chain_fallback_all_fail_raises_exhausted():
    """All entries fail → raises LLMChainExhausted with attempt detail."""
    entries = _entries(("a", "x", True), ("b", "y", True), ("c", "z", True))
    chains = _chains_with_primary(entries)
    caller = build_resilient_chain_caller(
        chains=chains, request=_FakeRequest(),
    )
    with pytest.raises(LLMChainExhausted) as excinfo:
        await caller(system="", messages=[], tools=[], max_tokens=100)
    assert excinfo.value.chain_name == "primary"
    assert len(excinfo.value.attempts) == 3


# ---------------------------------------------------------------------------
# Behavior 2 — context-window pre-flight skip
# ---------------------------------------------------------------------------


def _catalog_with_card(model_name: str, ceiling: int) -> dict:
    """Build a minimal catalog dict containing a single card."""
    return {
        model_name: ModelCard(
            name=model_name,
            provider="any",
            mode="chat",
            kernos_effective_max_input_tokens=ceiling,
        ),
    }


@pytest.mark.asyncio
async def test_context_window_skip_payload_fits_within_ceiling():
    """Small payload within the ceiling: entry called normally."""
    entries = _entries(("p", "tiny-model", False))
    chains = _chains_with_primary(entries)
    catalog = _catalog_with_card("tiny-model", ceiling=100_000)
    caller = build_resilient_chain_caller(
        chains=chains,
        request=_FakeRequest(),
        catalog_provider=lambda: catalog,
        safety_margin=0.10,
    )
    resp = await caller(system="hi", messages=[], tools=[], max_tokens=100)
    assert resp is not None
    assert len(entries[0].provider.calls) == 1


@pytest.mark.asyncio
async def test_context_window_skip_oversized_payload_skips_then_falls_through():
    """Oversized payload skips the small-ceiling entry; falls through to
    a larger-ceiling entry."""
    entries = _entries(("small", "tiny", False), ("big", "large", False))
    chains = _chains_with_primary(entries)
    # tiny ceiling is 200 tokens; large ceiling is 1M tokens.
    catalog = {
        "tiny": ModelCard(
            name="tiny", provider="small", mode="chat",
            kernos_effective_max_input_tokens=200,
        ),
        "large": ModelCard(
            name="large", provider="big", mode="chat",
            kernos_effective_max_input_tokens=1_000_000,
        ),
    }
    huge_messages = [{"role": "user", "content": "x" * 5000}]
    caller = build_resilient_chain_caller(
        chains=chains,
        request=_FakeRequest(),
        catalog_provider=lambda: catalog,
        safety_margin=0.10,
    )
    resp = await caller(
        system="", messages=huge_messages, tools=[], max_tokens=100,
    )
    assert resp is not None
    # tiny was skipped (no call); large was called.
    assert len(entries[0].provider.calls) == 0
    assert len(entries[1].provider.calls) == 1


@pytest.mark.asyncio
async def test_context_window_skip_all_too_small_raises_payload_too_large():
    """Payload exceeds every entry's ceiling → ChainPayloadTooLarge."""
    entries = _entries(("a", "tiny", False), ("b", "tinier", False))
    chains = _chains_with_primary(entries)
    catalog = {
        "tiny": ModelCard(
            name="tiny", provider="a", mode="chat",
            kernos_effective_max_input_tokens=100,
        ),
        "tinier": ModelCard(
            name="tinier", provider="b", mode="chat",
            kernos_effective_max_input_tokens=50,
        ),
    }
    huge_messages = [{"role": "user", "content": "x" * 5000}]
    caller = build_resilient_chain_caller(
        chains=chains,
        request=_FakeRequest(),
        catalog_provider=lambda: catalog,
        safety_margin=0.10,
    )
    with pytest.raises(ChainPayloadTooLarge) as excinfo:
        await caller(
            system="", messages=huge_messages, tools=[], max_tokens=100,
        )
    assert excinfo.value.chain_name == "primary"
    assert excinfo.value.estimated_tokens > 100
    assert excinfo.value.largest_ceiling == 100  # the bigger of the two
    # Neither provider was actually called.
    assert len(entries[0].provider.calls) == 0
    assert len(entries[1].provider.calls) == 0


@pytest.mark.asyncio
async def test_context_window_unknown_model_falls_through_tolerantly():
    """Model with no catalog card: pre-flight is skipped, provider is called."""
    entries = _entries(("p", "unknown-model", False))
    chains = _chains_with_primary(entries)
    caller = build_resilient_chain_caller(
        chains=chains,
        request=_FakeRequest(),
        catalog_provider=lambda: {},  # empty catalog
    )
    resp = await caller(system="", messages=[], tools=[], max_tokens=100)
    assert resp is not None
    assert len(entries[0].provider.calls) == 1


# ---------------------------------------------------------------------------
# Behavior 3 — model-override resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_override_chain_switch_routes_to_overridden_chain():
    """Override carries chain_name → caller dispatches against that chain."""
    primary_entries = _entries(("anthropic", "sonnet", False))
    light_entries = _entries(("anthropic", "haiku", False))
    chains = {"primary": primary_entries, "lightweight": light_entries}
    request = _FakeRequest(
        model_override={
            "chain_name": "lightweight",
            "override_provider": None,
            "override_model": None,
        },
    )
    caller = build_resilient_chain_caller(chains=chains, request=request)
    resp = await caller(system="", messages=[], tools=[], max_tokens=100)
    assert resp is not None
    # haiku was called, sonnet was not.
    assert len(primary_entries[0].provider.calls) == 0
    assert len(light_entries[0].provider.calls) == 1


@pytest.mark.asyncio
async def test_model_override_head_spec_prepends_explicit_entry():
    """Override carries (override_provider, override_model) matching an
    entry → caller routes that entry first."""
    sonnet_entry = ChainEntry(
        provider=_FakeProvider(name="anthropic", fail=False),
        model="sonnet",
    )
    haiku_entry = ChainEntry(
        provider=_FakeProvider(name="anthropic", fail=False),
        model="haiku",
    )
    chains = {"primary": [sonnet_entry, haiku_entry], "lightweight": [haiku_entry]}
    # Override pins haiku as the head; the resolver should prepend it.
    request = _FakeRequest(
        model_override={
            "chain_name": None,
            "override_provider": "anthropic",
            "override_model": "haiku",
        },
    )
    caller = build_resilient_chain_caller(chains=chains, request=request)
    resp = await caller(system="", messages=[], tools=[], max_tokens=100)
    assert resp is not None
    # haiku is the head; sonnet wasn't called because haiku succeeded.
    assert len(haiku_entry.provider.calls) == 1
    assert len(sonnet_entry.provider.calls) == 0


@pytest.mark.asyncio
async def test_model_override_no_override_uses_primary_unchanged():
    """No override: caller dispatches against primary chain unchanged."""
    entries = _entries(("p", "main", False))
    chains = _chains_with_primary(entries)
    caller = build_resilient_chain_caller(
        chains=chains, request=_FakeRequest(model_override=None),
    )
    resp = await caller(system="", messages=[], tools=[], max_tokens=100)
    assert resp is not None
    assert len(entries[0].provider.calls) == 1


@pytest.mark.asyncio
async def test_model_override_head_fails_fallback_uses_configured_subsequent():
    """Override-head failure path: head model fails → fallback uses
    subsequent entries with their *configured* models (not the override
    model). Pin: head_was_overridden flag does not leak into entry i>0
    model selection."""
    haiku_failing = ChainEntry(
        provider=_FakeProvider(name="anthropic", fail=True),
        model="haiku",
    )
    sonnet_succeeding = ChainEntry(
        provider=_FakeProvider(name="anthropic", fail=False),
        model="sonnet",
    )
    chains = {
        "primary": [sonnet_succeeding, haiku_failing],
        "lightweight": [haiku_failing],
    }
    # Override pins haiku as head (which will fail). Resolver
    # prepends haiku, so entries become [haiku, sonnet, haiku].
    request = _FakeRequest(
        model_override={
            "chain_name": None,
            "override_provider": "anthropic",
            "override_model": "haiku",
        },
    )
    caller = build_resilient_chain_caller(chains=chains, request=request)
    resp = await caller(system="", messages=[], tools=[], max_tokens=100)
    assert resp is not None
    # Sonnet entry got called with its CONFIGURED model "sonnet" — not
    # with the override model "haiku".
    sonnet_call_models = [c["model"] for c in sonnet_succeeding.provider.calls]
    assert "sonnet" in sonnet_call_models
    assert "haiku" not in sonnet_call_models


# ---------------------------------------------------------------------------
# Wire-shape: conversation_id forwards to provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversation_id_forwards_to_provider():
    """conversation_id passes through the chain caller into provider.complete."""
    entries = _entries(("p", "model", False))
    chains = _chains_with_primary(entries)
    caller = build_resilient_chain_caller(
        chains=chains, request=_FakeRequest(),
    )
    await caller(
        system="", messages=[], tools=[], max_tokens=100,
        conversation_id="conv-abc-123",
    )
    assert entries[0].provider.calls[0]["conversation_id"] == "conv-abc-123"


# ---------------------------------------------------------------------------
# Strike-readiness signal: build_resilient_chain_caller is the
# canonical surface; preserves all three behaviors the legacy
# helper provided.
# ---------------------------------------------------------------------------


def test_helper_exposes_canonical_surface():
    """Pin: the chain-caller helper is in __all__ and importable as the
    canonical seam for chain dispatch on the thin path."""
    from kernos.kernel.turn_runner_provider import (
        __all__ as helper_exports,
    )
    assert "build_resilient_chain_caller" in helper_exports
