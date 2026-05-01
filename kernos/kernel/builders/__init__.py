"""Builder backend package — compatibility facade.

EXTERNAL-AGENT-CONSULTATION v1 C3 (spec D1 fold + AC9):
``kernos/kernel/external_agents/`` is the unified primitive that
covers both build and consult modes for external coding-agent CLIs.
This module is preserved as the **compatibility facade** so:

* ``KERNOS_BUILDER`` env var keeps working unchanged.
* ``kernos.kernel.code_exec`` keeps importing
  ``BuilderBackend``, ``BuildResult``, ``UnknownBuilderError``, and
  ``get_builder`` without modification.
* ``kernos.kernel.setup.workspace_config`` keeps importing
  ``BUILDER_TIER`` and ``VALID_BUILDERS`` without modification.

The facade re-exports the exact name set existing callers depend
on. AC9 verifies every import path with a structural test.

For NEW code, prefer ``kernos.kernel.external_agents``: it covers
both build and consult; the registry exposes per-call backend
choice; the substrate composes uniformly across harnesses.
"""
from __future__ import annotations

from kernos.kernel.builders.aider import AiderBuilder
from kernos.kernel.builders.base import (
    BUILDER_TIER,
    VALID_BUILDERS,
    BuildResult,
    BuilderBackend,
)
from kernos.kernel.builders.external_stub import ExternalStubBuilder
from kernos.kernel.builders.native import NativeBuilder


class UnknownBuilderError(ValueError):
    """Raised when ``KERNOS_BUILDER`` names a backend that does not
    exist. Preserved at this import path for compatibility with
    existing callers (``kernos.kernel.code_exec``)."""


def get_builder(name: str) -> BuilderBackend:
    """Return a backend instance for ``name``.

    Raises ``UnknownBuilderError`` if ``name`` is not in
    ``VALID_BUILDERS``. Behavior is unchanged from the pre-facade
    implementation; existing callers keep working without
    modification.
    """
    if name not in VALID_BUILDERS:
        raise UnknownBuilderError(
            f"unknown KERNOS_BUILDER={name!r}; "
            f"valid values are {list(VALID_BUILDERS)}"
        )
    if name == "native":
        return NativeBuilder()
    if name == "aider":
        return AiderBuilder()
    return ExternalStubBuilder(name=name)


__all__ = [
    "BUILDER_TIER",
    "VALID_BUILDERS",
    "AiderBuilder",
    "BuildResult",
    "BuilderBackend",
    "ExternalStubBuilder",
    "NativeBuilder",
    "UnknownBuilderError",
    "get_builder",
]
