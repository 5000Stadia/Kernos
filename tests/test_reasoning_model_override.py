"""ReasoningService override-aware dispatch
(MODEL-AND-STATUS-V1, C3).

Pins:
  * Override head wins on the happy path (entries[0] is the
    overridden entry).
  * Override-head failure falls through to the natural chain with
    the overridden entry de-duped from later positions (Codex
    pre-spec refinement #1: "preferred first attempt, not hard
    pin").
  * Stale chain_name override falls back to the requested chain
    rather than crashing.
  * Stale (provider, model) override skips the prepend rather than
    crashing.
  * The shared resolver in kernos.kernel.model_routing is the
    single source of truth — same module the handler renderings
    use (Codex post-spec ask A note: no duplicate logic).
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.exceptions import (
    LLMChainExhausted,
    ReasoningProviderError,
)
from kernos.kernel.reasoning import ReasoningRequest, ReasoningService


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubResponse:
    text: str = "ok"
    model: str = ""
    input_tokens: int = 10
    output_tokens: int = 10
    estimated_cost_usd: float = 0.0
    stop_reason: str | None = "end_turn"
    tool_calls: list = None
    raw_provider_payload: dict | None = None
    chosen_provider_name: str = ""
    chosen_provider_model: str = ""

    def __post_init__(self):
        if self.tool_calls is None:
            self.tool_calls = []
        if self.raw_provider_payload is None:
            self.raw_provider_payload = {}


def _make_stub_provider(name: str, *, fail: bool = False):
    provider = MagicMock()
    provider.provider_name = name
    provider.main_model = f"{name}-main"
    provider.lightweight_model = f"{name}-lite"
    provider.context_window = 200_000

    async def _complete(*, model, system, messages, tools, max_tokens, **_):
        if fail:
            raise ReasoningProviderError(f"{name} stub failure")
        return _StubResponse(
            text=f"reply from {name}/{model}",
            model=model, chosen_provider_name=name,
            chosen_provider_model=model,
        )

    provider.complete = AsyncMock(side_effect=_complete)
    provider.context_safety_margin = MagicMock(return_value=0)
    return provider


@pytest.fixture
def reasoning_with_chains():
    """Build a ReasoningService with synthetic chains. The legacy
    ``__init__`` path expects a primary provider + optional
    fallbacks; we override ``_chains`` directly afterward to keep
    the test independent of build_chains_from_env."""
    from kernos.kernel.events import EventStream
    from kernos.persistence import AuditStore
    from kernos.capability.client import MCPClientManager
    from kernos.providers.base import ChainEntry

    primary = _make_stub_provider("anthropic")
    fallback = _make_stub_provider("openrouter")

    events = AsyncMock(spec=EventStream)
    audit = AsyncMock(spec=AuditStore)
    mcp = MagicMock(spec=MCPClientManager)
    mcp.get_tools.return_value = []

    svc = ReasoningService(primary, events, mcp, audit)
    # Replace the auto-built chains with our own synthetic shape.
    svc._chains = {
        "primary": [
            ChainEntry(provider=primary, model="claude-sonnet-4.6"),
            ChainEntry(provider=fallback, model="glm-5.1"),
        ],
        "lightweight": [
            ChainEntry(provider=primary, model="claude-haiku-4.5"),
            ChainEntry(provider=fallback, model="glm-5.1-flash"),
        ],
    }
    return svc, primary, fallback


def _request(*, model_override=None, model: str = "claude-sonnet-4.6"):
    return ReasoningRequest(
        instance_id="i", conversation_id="c",
        system_prompt="s", messages=[], tools=[],
        model=model, trigger="user_message",
        model_override=model_override,
    )


# ---------------------------------------------------------------------------
# Happy path: override head wins
# ---------------------------------------------------------------------------


class TestOverrideHeadWins:
    async def test_chain_switch_routes_to_lightweight(
        self, reasoning_with_chains,
    ):
        svc, anthropic, _ = reasoning_with_chains
        override = {
            "chain_name": "lightweight",
            "override_provider": None,
            "override_model": None,
        }
        await svc._call_chain(
            chain_name="primary", system="s", messages=[], tools=[],
            max_tokens=1024,
            request=_request(model_override=override),
        )
        # Anthropic answered with the lightweight model
        # (claude-haiku-4.5), not the primary head (claude-sonnet-4.6).
        called_models = [
            call.kwargs["model"]
            for call in anthropic.complete.await_args_list
        ]
        assert called_models == ["claude-haiku-4.5"]

    async def test_head_override_within_chain_dedupes(
        self, reasoning_with_chains,
    ):
        """Override entry that's already in the active chain moves
        to position 0 and is removed from later positions, so
        failover doesn't retry the same model twice."""
        svc, anthropic, openrouter = reasoning_with_chains
        # Force first call (override head) to fail so we exercise
        # the dedupe.
        override = {
            "chain_name": None,
            "override_provider": "openrouter",
            "override_model": "glm-5.1",
        }

        async def _fail_then_succeed(*, model, **_):
            if not openrouter.complete.call_count:
                openrouter.complete.call_count += 1
                raise ReasoningProviderError("first attempt fails")
            return _StubResponse(
                text="ok", model=model, chosen_provider_name="openrouter",
            )

        # Reset the AsyncMock call_count and side_effect.
        openrouter.complete.reset_mock()
        openrouter.complete.side_effect = ReasoningProviderError("openrouter fails")
        anthropic.complete.reset_mock()
        await svc._call_chain(
            chain_name="primary", system="s", messages=[], tools=[],
            max_tokens=1024,
            request=_request(model_override=override),
        )
        # openrouter called exactly ONCE (no retry as a duplicate),
        # anthropic answered as the natural fallback.
        assert openrouter.complete.await_count == 1
        assert anthropic.complete.await_count == 1


# ---------------------------------------------------------------------------
# Override-head failure → natural chain fallthrough
# ---------------------------------------------------------------------------


class TestOverrideHeadFailureFallsThrough:
    async def test_override_failure_continues_to_next_entry(
        self, reasoning_with_chains,
    ):
        """Override is 'preferred first attempt,' not a hard pin —
        when it fails, the natural chain sequence runs."""
        svc, anthropic, openrouter = reasoning_with_chains
        # Make the override entry (anthropic/claude-haiku-4.5) fail
        # but leave the natural chain head (anthropic/claude-sonnet-4.6)
        # working.
        async def _selective(*, model, **_):
            if model == "claude-haiku-4.5":
                raise ReasoningProviderError("haiku unavailable")
            return _StubResponse(
                text="ok", model=model, chosen_provider_name="anthropic",
            )

        anthropic.complete.side_effect = _selective
        override = {
            "chain_name": None,
            "override_provider": "anthropic",
            "override_model": "claude-haiku-4.5",
        }
        result = await svc._call_chain(
            chain_name="primary", system="s", messages=[], tools=[],
            max_tokens=1024,
            request=_request(model_override=override),
        )
        # Two anthropic calls: failed haiku + successful sonnet.
        called_models = [
            call.kwargs["model"]
            for call in anthropic.complete.await_args_list
        ]
        assert called_models == ["claude-haiku-4.5", "claude-sonnet-4.6"]
        assert result is not None


# ---------------------------------------------------------------------------
# Stale-config skip behavior
# ---------------------------------------------------------------------------


class TestStaleOverrideHandling:
    async def test_stale_chain_name_falls_back_to_requested(
        self, reasoning_with_chains,
    ):
        svc, anthropic, _ = reasoning_with_chains
        override = {
            "chain_name": "vintage",  # not in chains
            "override_provider": None,
            "override_model": None,
        }
        await svc._call_chain(
            chain_name="primary", system="s", messages=[], tools=[],
            max_tokens=1024,
            request=_request(model_override=override),
        )
        # Anthropic answered with primary head, NOT crashed on stale
        # vintage chain.
        called_models = [
            call.kwargs["model"]
            for call in anthropic.complete.await_args_list
        ]
        assert "claude-sonnet-4.6" in called_models

    async def test_stale_head_spec_skips_prepend(
        self, reasoning_with_chains,
    ):
        svc, anthropic, _ = reasoning_with_chains
        override = {
            "chain_name": None,
            "override_provider": "imaginary",
            "override_model": "ghost-1",
        }
        await svc._call_chain(
            chain_name="primary", system="s", messages=[], tools=[],
            max_tokens=1024,
            request=_request(model_override=override),
        )
        # Stale spec is skipped — natural primary chain runs as if no
        # override was set.
        called_models = [
            call.kwargs["model"]
            for call in anthropic.complete.await_args_list
        ]
        assert called_models == ["claude-sonnet-4.6"]


# ---------------------------------------------------------------------------
# Backward compat: no override behaves as before
# ---------------------------------------------------------------------------


class TestRequestModelDoesNotClobberHeadOverride:
    """Codex post-impl fold REAL bug: the loop at _call_chain
    replaces entry 0's model with request_model when present. With
    a head override active, entry 0 IS the override and the
    request_model substitution would silently call the natural head
    model on the overridden provider — defeating the user's pick.

    This test pins: when override head + request_model both set,
    the override head wins.
    """

    async def test_head_override_respected_under_request_model(
        self, reasoning_with_chains,
    ):
        svc, anthropic, openrouter = reasoning_with_chains
        override = {
            "chain_name": None,
            "override_provider": "anthropic",
            "override_model": "claude-haiku-4.5",
        }
        # Caller passes request_model=claude-sonnet-4.6 (the natural
        # primary head) — without the fold, this would override
        # entry 0's model and the call would hit sonnet.
        await svc._call_chain(
            chain_name="primary", system="s", messages=[], tools=[],
            max_tokens=1024,
            request_model="claude-sonnet-4.6",
            request=_request(
                model_override=override, model="claude-sonnet-4.6",
            ),
        )
        called_models = [
            call.kwargs["model"]
            for call in anthropic.complete.await_args_list
        ]
        # Override head wins as entry 0 (claude-haiku-4.5), not sonnet.
        assert called_models[0] == "claude-haiku-4.5"

    async def test_chain_switch_alone_still_uses_request_model(
        self, reasoning_with_chains,
    ):
        """Chain-only switches do NOT carry a head spec — entry 0's
        model under request_model substitution is the right
        behaviour (preserves the principal's model selection
        within the new chain)."""
        svc, anthropic, _ = reasoning_with_chains
        override = {
            "chain_name": "lightweight",
            "override_provider": None,
            "override_model": None,
        }
        await svc._call_chain(
            chain_name="primary", system="s", messages=[], tools=[],
            max_tokens=1024,
            request_model="explicit-model-from-handler",
            request=_request(model_override=override),
        )
        called_models = [
            call.kwargs["model"]
            for call in anthropic.complete.await_args_list
        ]
        # request_model wins entry 0 (no head override prevents it).
        assert called_models[0] == "explicit-model-from-handler"


class TestBackwardCompat:
    async def test_no_override_uses_default_chain(
        self, reasoning_with_chains,
    ):
        svc, anthropic, _ = reasoning_with_chains
        await svc._call_chain(
            chain_name="primary", system="s", messages=[], tools=[],
            max_tokens=1024,
            request=_request(),  # model_override=None
        )
        called_models = [
            call.kwargs["model"]
            for call in anthropic.complete.await_args_list
        ]
        assert called_models == ["claude-sonnet-4.6"]

    async def test_request_none_does_not_crash(
        self, reasoning_with_chains,
    ):
        """Internal callers (legacy paths) may pass request=None.
        The override resolver must tolerate that."""
        svc, anthropic, _ = reasoning_with_chains
        await svc._call_chain(
            chain_name="primary", system="s", messages=[], tools=[],
            max_tokens=1024,
            request=None,
        )
        called_models = [
            call.kwargs["model"]
            for call in anthropic.complete.await_args_list
        ]
        assert called_models == ["claude-sonnet-4.6"]
