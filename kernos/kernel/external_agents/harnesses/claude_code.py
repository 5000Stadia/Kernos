"""Claude Code harness — ``claude --print``.

Spec section "Per-harness implementations." Claude Code ships a
non-interactive ``--print`` mode and accepts a caller-supplied
``--session-id <uuid>``. v1 maps Kernos's sanitized hex
session_id to a valid UUID (the first 32 hex chars formatted as
8-4-4-4-12) so threading works across multiple ``consult`` calls
with the same Kernos session_id.

The Claude Code CLI is the most-tested external harness for
sessions; live integration tests in C6 cover it end-to-end.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

from kernos.kernel.external_agents.errors import (
    ConsultationFailed,
    ConsultationTimeout,
    HarnessUnavailable,
)
from kernos.kernel.external_agents.harness import (
    BuildResult,
    ConsultResult,
    HarnessHealth,
)
from kernos.kernel.external_agents.subprocess_substrate import (
    run_subprocess,
)

logger = logging.getLogger(__name__)


class ClaudeCodeHarness:
    """Subprocess wrapper around the ``claude`` CLI in
    non-interactive mode."""

    name = "claude_code"

    def __init__(self, *, binary: str = "claude") -> None:
        self._binary = binary

    def health_check(self) -> HarnessHealth:
        path = shutil.which(self._binary)
        if not path:
            return HarnessHealth(
                name=self.name, installed=False,
                detail=f"{self._binary!r} not on PATH",
            )
        return HarnessHealth(
            name=self.name, installed=True, authenticated=True,
            detail=f"binary at {path}",
        )

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
        # ACPX-INTEGRATION-V1 (2026-05-18): this harness is now a thin
        # compatibility shim — the actual dispatch happens through
        # acpx_adapter, which speaks the Agent Client Protocol. The
        # old per-CLI subprocess wrangling (env scrubbing, --session-id
        # UUID-shaping, --add-dir, --output-format flags, etc.) all
        # live inside the ACPX `claude` adapter now, not here.
        #
        # This shim is kept so existing tests + imports + the
        # ConsultationOrchestrator path don't break. v2 may collapse
        # the harness layer entirely once the orchestrator is
        # rewritten to call acpx_adapter directly.
        from kernos.kernel.external_agents.acpx_adapter import dispatch
        prompt = _compose_prompt(question, context)
        return await dispatch(
            target=self.name,  # "claude_code"
            prompt=prompt,
            session_id=session_id,
            workspace_dir=str(workspace_dir) if workspace_dir else "",
            timeout_seconds=timeout_seconds,
        )

    async def build(
        self, **_,
    ) -> BuildResult:
        # v1: claude_code is consult-mode only via the harness.
        # The existing builders/claude-code path remains accessible
        # via the compatibility facade in C3.
        raise HarnessUnavailable(
            "claude_code build mode is handled by the legacy "
            "builders/ facade, not the new harness"
        )


def _compose_prompt(question: str, context: dict | str) -> str:
    """Compose the question + optional context into a single prompt
    string. v1 keeps this minimal — agents can pass structured
    context via the ``context`` dict and the harness inlines it."""
    if not context:
        return question
    if isinstance(context, dict):
        import json
        ctx_text = json.dumps(context, indent=2)
        return f"{question}\n\n[Context]\n{ctx_text}"
    return f"{question}\n\n[Context]\n{context}"


def _hex_to_uuid(hex_id: str) -> str:
    """Format the first 32 chars of a sanitized hex SHA-256 as a
    UUID (8-4-4-4-12 with hyphens). Deterministic across processes
    so the same Kernos session_id maps to the same UUID, which is
    what Claude Code's --session-id needs to thread the session."""
    if len(hex_id) < 32:
        # Pad defensively; shouldn't happen since sanitize returns 64.
        hex_id = (hex_id + "0" * 32)[:32]
    h = hex_id[:32]
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


__all__ = ["ClaudeCodeHarness"]
