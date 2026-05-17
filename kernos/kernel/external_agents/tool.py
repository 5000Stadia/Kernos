"""Agent-facing ``consult`` tool — schema + service factory.

C7 wiring: the orchestrator + harnesses + log already exist; this
module exposes the agent surface so the primary agent can actually
call them. Mirrors the shape of ``EXECUTE_CODE_TOOL`` in
``code_exec.py``: a tool schema dict (consumed by the assemble
phase) plus a small singleton helper that builds a registered
orchestrator on first use and reuses it thereafter.

The service is process-singleton because the consultation_log
opens a sqlite connection on ``start()`` and must not be opened
twice. Engine bring-up (server.py, app.py, etc.) calls
:func:`get_service` once during init; later turns reuse the same
instance via the ``data_dir`` it was constructed with.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from kernos.kernel.external_agents.consultation_log import ConsultationLog
from kernos.kernel.external_agents.harnesses.aider import AiderHarness
from kernos.kernel.external_agents.harnesses.claude_code import (
    ClaudeCodeHarness,
)
from kernos.kernel.external_agents.harnesses.codex import CodexHarness
from kernos.kernel.external_agents.harnesses.gemini import GeminiHarness
from kernos.kernel.external_agents.orchestrator import (
    ConsultationOrchestrator,
    WorkspacePolicy,
)
from kernos.kernel.external_agents.registry import HarnessRegistry

logger = logging.getLogger(__name__)


CONSULT_TOOL = {
    "name": "consult",
    "description": (
        "Dispatch work to or get input from an external coding-agent "
        "CLI (Claude Code, Codex, Gemini). These are full general-"
        "purpose agentic CLIs with access to the repo via --add-dir: "
        "they can implement, refactor, debug, architect, review, "
        "test, document, explore, explain, generate, experiment — "
        "anything within their capabilities. Each call spawns a real "
        "CLI instance (not a stub); the agent reasons, uses its own "
        "tools, and returns a free-text response. Use whenever "
        "another agent's leverage adds value: when you're acting as "
        "broker and need to dispatch implementation; when you want "
        "a second opinion; when you need work done in a domain or "
        "code area where another agent's perspective fits better; "
        "when a fresh-context CLI is the right tool for the job. "
        "Don't use for simple lookups (just grep / read), routine "
        "bug fixes (just fix it), or user-facing answers (you "
        "answer directly). Aider is BUILD-only — use execute_code "
        "with backend='aider' for task-shaped CLI work. Threading: "
        "pass the same session_id across calls to preserve context "
        "where the harness CLI supports it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "harness": {
                "type": "string",
                "enum": ["claude_code", "codex", "gemini"],
                "description": (
                    "Which external CLI to consult. All three are "
                    "general-purpose agentic CLIs — they can do any "
                    "task within their capabilities. Pick based on "
                    "what fits the work, not on prescribed domains: "
                    "claude_code and codex are full coding agents "
                    "with repo access via --add-dir; gemini brings "
                    "Google's training to the same generality. "
                    "Threading support varies by harness."
                ),
            },
            "question": {
                "type": "string",
                "description": "The prompt / question to ask.",
            },
            "context": {
                "type": "string",
                "description": (
                    "Optional context to inline with the prompt — "
                    "code snippets, prior analysis, etc."
                ),
            },
            "session_id": {
                "type": "string",
                "description": (
                    "Optional. Pass the same value across calls to "
                    "thread a multi-turn consultation. Empty for a "
                    "fresh single-turn ask."
                ),
            },
            "workspace_dir": {
                "type": "string",
                "description": (
                    "Optional path the harness can read. Defaults to "
                    "the Kernos repo root."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "description": (
                    "Per-call timeout. Default 600, max 1800."
                ),
            },
        },
        "required": ["harness", "question"],
    },
}


# ----- service singleton -------------------------------------------

_service_lock = asyncio.Lock()
_service: "ExternalAgentService | None" = None


class ExternalAgentService:
    """Process-singleton wrapping the orchestrator + log.

    Engine bring-up calls :func:`get_service` to obtain the instance;
    the first call triggers ``start()`` which opens the sqlite
    connection backing ``consultation_log``. Subsequent calls reuse
    the same instance.
    """

    def __init__(
        self,
        *,
        data_dir: str,
        registry: HarnessRegistry,
        workspace_policy: WorkspacePolicy | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._registry = registry
        self._log = ConsultationLog()
        self._orchestrator = ConsultationOrchestrator(
            registry=registry,
            log=self._log,
            workspace_policy=workspace_policy,
        )
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        await self._log.start(self._data_dir)
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        await self._log.stop()
        self._started = False

    @property
    def orchestrator(self) -> ConsultationOrchestrator:
        return self._orchestrator

    @property
    def registry(self) -> HarnessRegistry:
        return self._registry

    @property
    def log(self) -> ConsultationLog:
        return self._log

    @property
    def data_dir(self) -> str:
        return self._data_dir


def _build_default_registry(*, data_dir: str) -> HarnessRegistry:
    """Register the four shipped harnesses with their mode flags.
    claude_code / codex / gemini support consult; aider supports
    build only (its CLI is task-shaped). Gemini history is rooted
    under ``<data_dir>/consultations`` so threading state lives
    alongside other instance-scoped state instead of /tmp."""
    registry = HarnessRegistry()
    registry.register(
        ClaudeCodeHarness(),
        consult_supported=True, build_supported=False,
    )
    registry.register(
        CodexHarness(),
        consult_supported=True, build_supported=False,
    )
    registry.register(
        GeminiHarness(
            history_root=Path(data_dir) / "consultations",
        ),
        consult_supported=True, build_supported=False,
    )
    registry.register(
        AiderHarness(),
        consult_supported=False, build_supported=True,
    )
    return registry


async def get_service(
    *, data_dir: str | None = None,
) -> ExternalAgentService:
    """Return the process-wide service, constructing + starting it
    on first call. ``data_dir`` only matters on first call; later
    callers pass ``None`` and get the same instance.

    Repo-root workspace allowlist is enforced when
    ``KERNOS_EXTERNAL_AGENT_ALLOWLIST`` is set (colon-separated
    paths). Default: no allowlist (any workspace_dir accepted).
    """
    global _service
    async with _service_lock:
        if _service is not None:
            return _service
        resolved_data = data_dir or os.getenv(
            "KERNOS_DATA_DIR", "./data",
        )
        allowlist_env = os.getenv(
            "KERNOS_EXTERNAL_AGENT_ALLOWLIST", "",
        )
        if allowlist_env:
            paths = tuple(
                Path(p).expanduser().resolve()
                for p in allowlist_env.split(":")
                if p.strip()
            )
            policy = WorkspacePolicy(allowlist=paths)
        else:
            policy = None
        service = ExternalAgentService(
            data_dir=resolved_data,
            registry=_build_default_registry(data_dir=resolved_data),
            workspace_policy=policy,
        )
        await service.start()
        _service = service
        logger.info(
            "EXTERNAL_AGENT_SERVICE_STARTED data_dir=%s allowlist=%s",
            resolved_data,
            len(allowlist_env.split(":")) if allowlist_env else 0,
        )
        return _service


async def reset_service_for_tests() -> None:
    """Test helper: stop and clear the singleton so the next
    ``get_service`` call rebuilds. Production callers must not use
    this — tearing down the log mid-turn would lose audit rows."""
    global _service
    async with _service_lock:
        if _service is not None:
            await _service.stop()
            _service = None


__all__ = [
    "CONSULT_TOOL",
    "ExternalAgentService",
    "get_service",
    "reset_service_for_tests",
]
