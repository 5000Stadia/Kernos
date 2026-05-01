"""Harness protocol + result types for external-agent consultation.

Each external coding-agent CLI (Claude Code, Codex, Gemini, Aider)
implements :class:`Harness`. The protocol is intentionally narrow:
two methods (``consult``, ``build``) plus a health probe. Backends
that don't support a mode raise
:class:`HarnessUnavailable` from that method.

Both modes return uniform result types (:class:`ConsultResult`,
:class:`BuildResult`) so the agent-facing tool surface and the
existing :mod:`kernos.kernel.builders` callers can compose
identically across backends.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HarnessHealth:
    """Result of :meth:`Harness.health_check`. Reports whether the
    CLI binary is on PATH AND the harness considers itself
    operational (auth present, version compatible).

    ``installed`` and ``authenticated`` are set independently so the
    operator can distinguish "binary missing" from "binary present
    but unauthenticated."
    """

    name: str
    installed: bool
    authenticated: bool = False
    version: str = ""
    detail: str = ""


@dataclass(frozen=True)
class ConsultResult:
    """Uniform shape returned by :meth:`Harness.consult` across all
    backends. Mirrors the agent-facing ``consult`` tool's return
    contract from the spec."""

    response: str
    harness: str
    session_id: str = ""
    native_session_ref: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False


# Codex post-impl review fold (AC9 alignment): the new harness
# layer reuses the legacy ``builders.base.BuildResult`` rather than
# defining a parallel dataclass. This keeps a single source of
# truth — both ``kernos.kernel.builders`` (legacy facade) and
# ``kernos.kernel.external_agents`` expose the same class, so the
# AiderHarness build adapter doesn't need to translate fields and
# downstream callers see one ``BuildResult`` shape regardless of
# which import path they used.
from kernos.kernel.builders.base import BuildResult  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class Harness(Protocol):
    """One implementation per external CLI. The protocol does NOT
    declare async methods — implementations may be sync or async.
    The registry adapts both to the agent-facing tool surface.
    """

    name: str

    def health_check(self) -> HarnessHealth:
        """Probe binary presence + auth. Idempotent + fast."""

    async def consult(
        self,
        *,
        question: str,
        context: dict | str,
        session_id: str,
        workspace_dir: Path,
        timeout_seconds: int,
        harness_options: dict[str, Any],
    ) -> ConsultResult:
        """Q&A mode. Implementations may raise:

        * :class:`HarnessUnavailable` — CLI missing / mode not supported.
        * :class:`ConsultationTimeout` — subprocess exceeded timeout.
        * :class:`ConsultationFailed` — subprocess exited non-zero.
        """

    async def build(
        self,
        *,
        task: str,
        workspace_dir: Path,
        timeout_seconds: int,
        harness_options: dict[str, Any],
    ) -> BuildResult:
        """Task-execution mode (existing ``code_exec`` semantics).
        Implementations that don't build raise
        :class:`HarnessUnavailable`. The default implementation in
        the harness base class does this; concrete harnesses
        override when they support build."""


__all__ = [
    "BuildResult",
    "ConsultResult",
    "Harness",
    "HarnessHealth",
]
