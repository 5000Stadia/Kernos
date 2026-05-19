"""Gemini CLI harness — ``gemini --prompt``.

Gemini's CLI session model is less stable across versions than
Claude Code or Codex. v1 uses a "rebuild context per call"
posture: when ``session_id`` is provided, the harness persists
prior turns in a JSONL file under
``data/<instance>/consultations/<sanitized_hex_id>/gemini.jsonl``
and replays them as part of the prompt on subsequent calls. If
the CLI exposes a stable session-id flag in a future version, the
harness can switch to that path without changing the public
contract.

Native session ref recorded in ``consultation_log.native_session_ref``
is the path to the persisted history file when threading; empty
otherwise.
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


class GeminiHarness:
    name = "gemini"

    def __init__(
        self,
        *,
        binary: str = "gemini",
        history_root: Path | None = None,
    ) -> None:
        self._binary = binary
        self._history_root = history_root

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
        # (gemini --prompt --yolo, history JSONL replay, etc.) all
        # live inside the ACPX `gemini` adapter now.
        #
        # Founder ratified 2026-05-18: skip developing Gemini
        # integration depth for now. ACPX speaks gemini and that's
        # the supported path; we don't invest in CLI-specific
        # Gemini features here.
        from kernos.kernel.external_agents.acpx_adapter import dispatch
        prompt = _compose_prompt_for_acpx(question, context)
        return await dispatch(
            target=self.name,  # "gemini"
            prompt=prompt,
            session_id=session_id,
            workspace_dir=str(workspace_dir) if workspace_dir else "",
            timeout_seconds=timeout_seconds,
        )

    async def build(self, **_) -> BuildResult:
        raise HarnessUnavailable(
            "gemini build mode is not implemented in v1"
        )

    def _history_path(self, session_id: str) -> Path:
        if self._history_root is None:
            return Path("/tmp") / "kernos" / "consultations" / session_id / "gemini.jsonl"
        return self._history_root / session_id / "gemini.jsonl"


def _is_safe_session_id(session_id: str) -> bool:
    """Codex mid-batch fold: enforce that session_id is in the
    sanitized 64-char lowercase hex format produced by
    :func:`sanitize_session_id`. Anything else risks path traversal
    when the value is interpolated into the on-disk history path."""
    if len(session_id) != 64:
        return False
    return all(c in "0123456789abcdef" for c in session_id)


def _compose_prompt_for_acpx(question: str, context: dict | str) -> str:
    """ACPX-INTEGRATION-V1: minimal question+context blend for the
    ACPX adapter path. ACPX's named sessions handle prior-turn
    continuity natively, so this shim doesn't need the prior_turns
    replay the legacy _compose_prompt did."""
    if not context:
        return question
    if isinstance(context, dict):
        return f"{question}\n\n[Context]\n{json.dumps(context, indent=2)}"
    return f"{question}\n\n[Context]\n{context}"


def _compose_prompt(
    question: str, context: dict | str, prior_turns: list[dict],
) -> str:
    parts: list[str] = []
    if prior_turns:
        parts.append("[Prior conversation]")
        for turn in prior_turns:
            user = turn.get("user", "")
            assistant = turn.get("assistant", "")
            if user:
                parts.append(f"User: {user}")
            if assistant:
                parts.append(f"Assistant: {assistant}")
        parts.append("")
    if context:
        if isinstance(context, dict):
            parts.append("[Context]")
            parts.append(json.dumps(context, indent=2))
        else:
            parts.append(f"[Context]\n{context}")
        parts.append("")
    parts.append(question)
    return "\n".join(parts)


def _load_history(path: Path) -> list[dict]:
    if not path or not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def _append_history(path: Path, *, user: str, assistant: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps({"user": user, "assistant": assistant})
    with path.open("a", encoding="utf-8") as f:
        f.write(record + "\n")


__all__ = ["GeminiHarness"]
