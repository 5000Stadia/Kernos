"""ProviderRegistry — factory-based AgentInbox construction.

Per DOMAIN-AGENT-REGISTRY spec section 1: the runtime maintains an
in-memory ``ProviderRegistry`` mapping ``provider_key`` → factory
function that produces an ``AgentInbox`` given a
``provider_config_ref`` from an ``AgentRecord``. Engine bring-up
populates the registry from instance configuration; ``register_agent``
validates that the agent's ``provider_key`` is bound here before
persisting, and ``RouteToAgentAction.execute()`` looks up the
factory at dispatch time to construct the concrete inbox for the
post.

Why factories instead of pre-bound instances: the registry stores
**descriptors** (serializable AgentRecord rows), not live Python
objects. Constructing the concrete at dispatch keeps the registry
portable, makes restart semantics trivial (no objects to rehydrate),
and lets a single AgentInbox concrete (``NotionAgentInbox``,
``InMemoryAgentInbox``, future ``LocalFolderAgentInbox``) be reused
across many agent records that vary only in ``provider_config_ref``.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Protocol

from kernos.kernel.workflows.agent_inbox import AgentInbox


# A factory takes the agent's ``provider_config_ref`` (a free-form
# string the agent registry stores per agent — e.g. a database ID,
# folder path, or named config key) and returns a concrete inbox.
# Factories are typically simple closures bound at engine bring-up.
AgentInboxFactory = Callable[[str], AgentInbox]


class ProviderRegistry:
    """In-memory ``provider_key`` → factory map.

    Engine bring-up registers one factory per supported provider
    backend (Notion, in-memory, local-folder, etc.). At dispatch
    time, ``RouteToAgentAction`` looks up the agent's
    ``provider_key``, gets the factory, and calls it with the
    agent's ``provider_config_ref`` to construct the concrete
    inbox.

    Not persisted — rebuilt on every engine startup.
    """

    def __init__(self) -> None:
        self._factories: dict[str, AgentInboxFactory] = {}

    def register(
        self, provider_key: str, factory: AgentInboxFactory,
    ) -> None:
        """Bind a factory for the given provider_key. Idempotent
        re-binding is rejected so configuration mistakes surface
        loudly rather than silently overwriting."""
        if not provider_key:
            raise ValueError("provider_key must be non-empty")
        if provider_key in self._factories:
            raise ValueError(
                f"provider_key {provider_key!r} already registered; "
                f"use unregister() to replace"
            )
        self._factories[provider_key] = factory

    def unregister(self, provider_key: str) -> bool:
        """Remove a factory binding. Returns True if removed,
        False if absent."""
        return self._factories.pop(provider_key, None) is not None

    def get(self, provider_key: str) -> AgentInboxFactory | None:
        """Return the factory bound to ``provider_key``, if any."""
        return self._factories.get(provider_key)

    def has(self, provider_key: str) -> bool:
        return provider_key in self._factories

    def known_keys(self) -> tuple[str, ...]:
        return tuple(self._factories.keys())

    def construct(
        self, provider_key: str, provider_config_ref: str,
    ) -> AgentInbox:
        """Look up the factory and construct the concrete inbox.
        Raises ``ProviderKeyUnknown`` if the key isn't registered —
        callers (RouteToAgentAction in C4) translate this to the
        ``AgentInboxProviderUnavailable`` typed error per AC #10."""
        factory = self._factories.get(provider_key)
        if factory is None:
            raise ProviderKeyUnknown(provider_key)
        return factory(provider_config_ref)


class ProviderKeyUnknown(LookupError):
    """Raised by ``ProviderRegistry.construct`` when the requested
    ``provider_key`` has no factory bound. RouteToAgentAction
    catches this and re-raises as ``AgentInboxProviderUnavailable``
    so callers see a registry-shaped error rather than a generic
    LookupError."""

    def __init__(self, provider_key: str) -> None:
        super().__init__(
            f"no factory registered for provider_key {provider_key!r}"
        )
        self.provider_key = provider_key


__all__ = [
    "AgentInboxFactory",
    "ProviderKeyUnknown",
    "ProviderRegistry",
]
