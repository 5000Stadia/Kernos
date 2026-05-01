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
        if not shutil.which(self._binary):
            raise HarnessUnavailable(
                f"gemini binary not on PATH; install Gemini CLI "
                f"or pass binary= to the harness constructor"
            )
        if session_id:
            # Codex mid-batch fold: harness boundary enforces sanitized
            # session_id format. Caller MUST sanitize via
            # sanitize_session_id() before invoking; raw session_id
            # could path-traverse otherwise. v1 expects exactly the
            # 64-char lowercase hex SHA-256 produced by
            # sanitize_session_id.
            if not _is_safe_session_id(session_id):
                raise ConsultationFailed(
                    f"gemini: session_id {session_id!r} is not in "
                    f"the sanitized 64-char hex format the harness "
                    f"requires; orchestrator must call "
                    f"sanitize_session_id() before invoking"
                )
        history_file = self._history_path(session_id) if session_id else None
        prior_turns = _load_history(history_file) if history_file else []

        try:
            prompt = _compose_prompt(question, context, prior_turns)
        except (TypeError, ValueError) as exc:
            raise ConsultationFailed(
                f"gemini: context not JSON-serializable: {exc}"
            ) from exc

        cmd = [
            self._binary,
            "--prompt", prompt,
            "--yolo",  # auto-approve for non-interactive use
        ]
        try:
            result = await run_subprocess(
                cmd,
                cwd=workspace_dir if workspace_dir else None,
                timeout_seconds=timeout_seconds,
            )
        except (OSError, FileNotFoundError) as exc:
            raise HarnessUnavailable(
                f"gemini subprocess spawn failed: {exc}"
            ) from exc
        if result.timed_out:
            raise ConsultationTimeout(
                f"gemini consultation timed out after {timeout_seconds}s"
            )
        if result.exit_code != 0:
            raise ConsultationFailed(
                f"gemini exited {result.exit_code}: "
                f"{(result.stderr or 'no stderr')[:500]}",
                exit_status=result.exit_code,
            )

        if history_file:
            try:
                _append_history(
                    history_file,
                    user=question, assistant=result.stdout,
                )
            except OSError as exc:
                # History persistence failure is non-fatal — the
                # consultation succeeded, but threading on the next
                # call will lose this turn. Log and continue.
                logger.warning(
                    "gemini: history append failed for %s: %s",
                    history_file, exc,
                )

        return ConsultResult(
            response=result.stdout,
            harness=self.name,
            session_id=session_id,
            native_session_ref=str(history_file) if history_file else "",
            metadata={
                "duration_seconds": result.duration_seconds,
                "exit_status": result.exit_code,
                "history_replay_turns": len(prior_turns),
            },
            truncated=result.truncated,
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
