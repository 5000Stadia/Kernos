"""Codex CLI harness — ``codex exec --json``.

Codex's session model differs from Claude Code's: the CLI assigns
a ``thread_id`` on first call (visible in ``--json`` output's
``thread.started`` event) and accepts ``codex exec resume <id>``
for follow-ups. v1 captures the native ``thread_id`` from JSON
output and stores it via the orchestrator's
``harness_options["prior_native_session_ref"]`` parameter so the
next call with the same Kernos session_id resumes the thread.

Spec kick-back trigger #2 covers what to do if ``codex exec``
behavior diverges from this assumption.
"""
from __future__ import annotations

import json
import logging
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


class CodexHarness:
    name = "codex"

    def __init__(self, *, binary: str = "codex") -> None:
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
        # ACPX-INTEGRATION-V1 (2026-05-18): thin compatibility shim —
        # actual dispatch goes through acpx_adapter, which speaks the
        # Agent Client Protocol. The old per-CLI subprocess wrangling
        # (codex exec --json, JSONL parsing, thread-id capture, etc.)
        # all live inside the ACPX `codex` adapter now.
        from kernos.kernel.external_agents.acpx_adapter import dispatch
        prompt = _compose_prompt(question, context)
        return await dispatch(
            target=self.name,  # "codex"
            prompt=prompt,
            session_id=session_id,
            workspace_dir=str(workspace_dir) if workspace_dir else "",
            timeout_seconds=timeout_seconds,
        )

    async def build(self, **_) -> BuildResult:
        raise HarnessUnavailable(
            "codex build mode is handled by the legacy builders/ "
            "facade, not the new harness"
        )


def _compose_prompt(question: str, context: dict | str) -> str:
    if not context:
        return question
    if isinstance(context, dict):
        ctx_text = json.dumps(context, indent=2)
        return f"{question}\n\n[Context]\n{ctx_text}"
    return f"{question}\n\n[Context]\n{context}"


def _parse_codex_jsonl(stdout: str) -> tuple[str, str, dict]:
    """Parse ``codex exec --json`` output. Codex emits one JSON
    object per line:

    * ``thread.started`` carries ``thread_id`` (the native session
      ref).
    * ``item.completed`` with ``item.type == 'agent_message'`` carries
      the response text.
    * ``turn.completed`` carries ``usage`` (tokens etc.).

    Returns ``(thread_id, response_text, usage_dict)``. Missing
    fields default to empty string / empty dict.
    """
    thread_id = ""
    response_parts: list[str] = []
    usage: dict = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started":
            tid = event.get("thread_id")
            if isinstance(tid, str):
                thread_id = tid
        elif kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    response_parts.append(text)
        elif kind == "turn.completed":
            usage_field = event.get("usage")
            if isinstance(usage_field, dict):
                usage = usage_field
    return thread_id, "\n".join(response_parts), usage


__all__ = ["CodexHarness"]
