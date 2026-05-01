"""Harness registry — single source of truth for which external
agents are wired and what modes they support.

Adding a new harness is a single ``register(...)`` call here. The
agent-facing ``consult`` tool reads
:meth:`HarnessRegistry.list_consult_harnesses` to determine which
``harness=`` values are valid; same for build via
:meth:`list_build_harnesses`.

Aider note: aider participates in build mode but raises
:class:`HarnessUnavailable` for consult. The registry exposes
``consult_supported`` and ``build_supported`` per registered
harness so callers can validate at the boundary instead of waiting
for the harness method to raise.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from kernos.kernel.external_agents.errors import (
    HarnessRegistrationError,
    HarnessUnavailable,
)
from kernos.kernel.external_agents.harness import Harness, HarnessHealth

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _HarnessEntry:
    name: str
    harness: Harness
    consult_supported: bool
    build_supported: bool


class HarnessRegistry:
    """Container for registered :class:`Harness` instances. Not a
    singleton — engine bring-up constructs one and shares it; tests
    construct disposable instances."""

    def __init__(self) -> None:
        self._by_name: dict[str, _HarnessEntry] = {}

    # -- registration ----------------------------------------------------

    def register(
        self,
        harness: Harness,
        *,
        consult_supported: bool = True,
        build_supported: bool = False,
    ) -> None:
        """Register a harness. ``consult_supported`` /
        ``build_supported`` declare which modes the harness handles
        — modes the harness doesn't support raise
        :class:`HarnessUnavailable` regardless of binary presence."""
        if not consult_supported and not build_supported:
            raise HarnessRegistrationError(
                f"harness {harness.name!r} registers neither consult "
                f"nor build — at least one must be supported"
            )
        if harness.name in self._by_name:
            raise HarnessRegistrationError(
                f"harness {harness.name!r} already registered"
            )
        self._by_name[harness.name] = _HarnessEntry(
            name=harness.name,
            harness=harness,
            consult_supported=consult_supported,
            build_supported=build_supported,
        )

    # -- queries ---------------------------------------------------------

    def get(self, name: str, *, mode: str = "consult") -> Harness:
        """Return the harness or raise :class:`HarnessUnavailable`.

        ``mode`` must be ``"consult"`` or ``"build"``. The registry
        validates the requested mode against
        ``consult_supported`` / ``build_supported`` so a wrong-mode
        request fails at the boundary, not deep in the harness."""
        entry = self._by_name.get(name)
        if entry is None:
            available = ", ".join(sorted(self._by_name)) or "(none)"
            raise HarnessUnavailable(
                f"harness {name!r} not registered. Available: {available}"
            )
        if mode == "consult" and not entry.consult_supported:
            raise HarnessUnavailable(
                f"harness {name!r} does not support consult mode "
                f"in v1 — its CLI is task-shaped, not Q&A-shaped"
            )
        if mode == "build" and not entry.build_supported:
            raise HarnessUnavailable(
                f"harness {name!r} does not support build mode"
            )
        return entry.harness

    def list_consult_harnesses(self) -> list[str]:
        return sorted(
            entry.name for entry in self._by_name.values()
            if entry.consult_supported
        )

    def list_build_harnesses(self) -> list[str]:
        return sorted(
            entry.name for entry in self._by_name.values()
            if entry.build_supported
        )

    def list_all(self) -> list[str]:
        return sorted(self._by_name)

    def discover(self) -> dict[str, HarnessHealth]:
        """Probe every registered harness's health. Idempotent.
        Returns a mapping name → health snapshot."""
        out: dict[str, HarnessHealth] = {}
        for name, entry in self._by_name.items():
            try:
                out[name] = entry.harness.health_check()
            except Exception as exc:
                logger.warning(
                    "harness %s health check raised: %s", name, exc,
                )
                out[name] = HarnessHealth(
                    name=name, installed=False, detail=f"health-check error: {exc}",
                )
        return out


__all__ = ["HarnessRegistry"]
