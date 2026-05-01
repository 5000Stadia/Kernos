"""Shared subprocess-spawning substrate for harness implementations.

Every harness shells out to its CLI in roughly the same shape:
spawn → capture stdout/stderr → enforce timeout → return text.
The plumbing is centralized here so each harness implementation
focuses on its CLI-specific concerns (flags, session model, env
vars) instead of reinventing subprocess + asyncio handling.

Provides:

* :func:`run_subprocess` — async spawn + capture + timeout. Returns
  a :class:`SubprocessResult` regardless of exit status; callers
  decide what counts as failure.
* :func:`sanitize_session_id` — Codex spec-review fold #7. Hex-
  encodes SHA-256 of the agent-supplied session_id so it's safe as
  a filesystem-path component without leaking the raw value.
* :func:`response_truncate` — clip oversize CLI output and flag.

No business logic about consultation vs. build; both modes use
this substrate identically.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)


# Default cap for captured CLI response text. Larger outputs are
# truncated and the SubprocessResult flags ``truncated=True``. Codex
# spec-review fold added the truncated field; v1 default is 1MB.
DEFAULT_RESPONSE_CAP_BYTES: int = 1_048_576


@dataclass(frozen=True)
class SubprocessResult:
    """Uniform shape regardless of exit status. Callers (the harness
    implementations) decide whether non-zero exit + non-empty stderr
    counts as failure for that CLI's semantics."""

    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    timed_out: bool = False
    truncated: bool = False
    cmd: tuple[str, ...] = field(default_factory=tuple)


def sanitize_session_id(raw: str) -> str:
    """Return a filesystem-safe 64-char hex string deterministic on
    the input. Empty / whitespace-only input returns an empty string;
    callers must check before using.

    Rationale (Codex spec-review fold #7): agent-supplied session
    ids flow into ``data/<instance>/consultations/<session_id>/``
    paths. Without sanitization a malicious or buggy agent could
    request paths with ``../`` or other path-injection sequences.
    SHA-256 hex bounds length at 64 chars and never contains
    path-special characters.
    """
    trimmed = (raw or "").strip().lower()
    if not trimmed:
        return ""
    return hashlib.sha256(trimmed.encode("utf-8")).hexdigest()


def response_truncate(
    text: str, *, cap_bytes: int = DEFAULT_RESPONSE_CAP_BYTES,
) -> tuple[str, bool]:
    """Clip ``text`` to ``cap_bytes`` (counting UTF-8 bytes). Returns
    ``(possibly_clipped_text, truncated_flag)``. The clip happens at
    the byte boundary; multi-byte characters straddling the cap are
    dropped to keep the result valid UTF-8."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= cap_bytes:
        return text, False
    clipped = encoded[:cap_bytes]
    # Drop trailing bytes that would form a partial multibyte char.
    return clipped.decode("utf-8", errors="ignore"), True


async def run_subprocess(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout_seconds: int,
    stdin_text: str | None = None,
    response_cap_bytes: int = DEFAULT_RESPONSE_CAP_BYTES,
) -> SubprocessResult:
    """Spawn ``cmd`` asynchronously, capture stdout + stderr, enforce
    ``timeout_seconds``. Always returns a :class:`SubprocessResult`;
    never raises for non-zero exit. Raises :class:`OSError` if the
    binary itself cannot be spawned (e.g. not on PATH).
    """
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else os.environ.copy(),
        # When no stdin_text is supplied, give the child a closed
        # stdin (DEVNULL) rather than inheriting the parent's. Some
        # CLIs (notably claude --print) check stdin readability and
        # error on inherited-but-broken stdin under pytest. DEVNULL
        # is the explicit "no input" signal.
        stdin=(
            asyncio.subprocess.PIPE if stdin_text
            else asyncio.subprocess.DEVNULL
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdin_bytes = stdin_text.encode("utf-8") if stdin_text else None
    timed_out = False
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(input=stdin_bytes),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        timed_out = True
        process.kill()
        try:
            stdout_bytes, stderr_bytes = await process.communicate()
        except Exception:
            stdout_bytes = b""
            stderr_bytes = b""

    duration = loop.time() - started_at
    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    stdout_text, stdout_clipped = response_truncate(
        stdout_text, cap_bytes=response_cap_bytes,
    )
    stderr_text, stderr_clipped = response_truncate(
        stderr_text, cap_bytes=response_cap_bytes,
    )
    return SubprocessResult(
        stdout=stdout_text,
        stderr=stderr_text,
        exit_code=process.returncode if process.returncode is not None else -1,
        duration_seconds=duration,
        timed_out=timed_out,
        truncated=stdout_clipped or stderr_clipped,
        cmd=tuple(cmd),
    )


__all__ = [
    "DEFAULT_RESPONSE_CAP_BYTES",
    "SubprocessResult",
    "response_truncate",
    "run_subprocess",
    "sanitize_session_id",
]
