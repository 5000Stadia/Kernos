"""ACPX-INTEGRATION-V1 — Kernos's dispatch layer over the Agent Client Protocol.

ACPX (https://github.com/openclaw/acpx) is a headless CLI client for
the Agent Client Protocol (ACP), with built-in adapters for Claude
Code, Codex, Gemini, Cursor, GitHub Copilot, and others. Kernos
delegates all external-coding-agent dispatch through ACPX so the
substrate doesn't carry bespoke per-CLI subprocess wrangling.

This module is the single entry point Kernos uses to talk to any
ACPX-supported agent. The legacy per-CLI harness modules
(``harnesses/claude_code.py``, ``harnesses/codex.py``,
``harnesses/gemini.py``) become thin delegates to this adapter — see
:func:`dispatch` for the contract.

Architecture pinned by founder + Codex pre-spec review (2026-05-18):
  * ACPX version pinned to 0.8.0 (alpha upstream; override via
    ``KERNOS_ACPX_VERSION``)
  * Fail-loud-by-default on missing binary; opt-in auto-install via
    ``KERNOS_ACPX_AUTO_INSTALL=1``
  * Substrate-derived session IDs from (instance_id, member_id,
    target, conversation_id) using the existing sanitize helper
  * NDJSON parse accumulates final text from ``session/update`` events
    where ``params.update.sessionUpdate == "agent_message_chunk"``;
    completion = process exit + JSON-RPC ``prompt`` response with
    ``stopReason`` (or EOF)
  * ``--cwd`` (not ``--add-dir``) for repo scoping
  * Alias map: ACPX agent name ``claude`` ↔ Kernos boundary name
    ``claude_code`` (consultation_log CHECK enum requires the
    Kernos-side names)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import Any

from kernos.kernel.external_agents.errors import (
    ConsultationFailed,
    ConsultationTimeout,
    HarnessUnavailable,
)
from kernos.kernel.external_agents.harness import ConsultResult
from kernos.kernel.external_agents.subprocess_substrate import (
    response_truncate,
    sanitize_session_id,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Configuration constants (env-tunable)
# ---------------------------------------------------------------------


def _acpx_binary() -> str:
    """Return the ACPX binary path.

    Lookup order:
      1. ``KERNOS_ACPX_BINARY`` env override (tests / non-standard installs)
      2. ``shutil.which("acpx")`` — standard PATH lookup
      3. Common npm-global fallbacks: ``~/.npm-global/bin/acpx``,
         ``~/.local/bin/acpx``, ``/usr/local/bin/acpx``

    The fallback handles the deploy gap where the bot's launch PATH
    (systemd, double-clicked start.sh) lacks the npm-global bin dir
    even though ``acpx`` is installed. Without this, every dispatch
    came back ``unable_to_investigate`` with "'acpx' not on PATH".
    """
    override = os.environ.get("KERNOS_ACPX_BINARY", "").strip()
    if override:
        return override
    import shutil
    found = shutil.which("acpx")
    if found:
        return found
    home = os.path.expanduser("~")
    for candidate in (
        f"{home}/.npm-global/bin/acpx",
        f"{home}/.local/bin/acpx",
        "/usr/local/bin/acpx",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "acpx"


# Kernos-boundary target name → ACPX subcommand agent name. Kernos
# keeps the ``claude_code`` / ``codex`` / ``gemini`` namespace at its
# tool surface + DB CHECK constraint; ACPX itself uses ``claude``
# instead of ``claude_code``. Map at the boundary.
_TARGET_ALIAS_MAP: dict[str, str] = {
    "claude_code": "claude",
    "codex": "codex",
    "gemini": "gemini",
}


# Supported Kernos-side target names (matches the
# consultation_log.harness CHECK enum minus 'aider', which is
# build-only and dispatched through a separate path).
SUPPORTED_TARGETS: frozenset[str] = frozenset(_TARGET_ALIAS_MAP.keys())


DEFAULT_TIMEOUT_SECONDS: int = int(
    os.environ.get("KERNOS_ACPX_TIMEOUT_SEC", "600")
)
MAX_TIMEOUT_SECONDS: int = int(
    os.environ.get("KERNOS_ACPX_MAX_TIMEOUT_SEC", "1800")
)


# ---------------------------------------------------------------------
# Availability probe
# ---------------------------------------------------------------------


# Pinned ACPX version. ACPX is alpha upstream (per Codex pre-spec
# review); any version drift may shift CLI flags or NDJSON event
# shapes that this adapter parses. Operators can override via
# KERNOS_ACPX_VERSION if they're testing a newer pin.
EXPECTED_ACPX_VERSION: str = os.environ.get(
    "KERNOS_ACPX_VERSION", "0.8.0",
).strip() or "0.8.0"


def is_acpx_available() -> tuple[bool, str]:
    """Return (available, detail). Used by bring-up's startup check
    to fail loud when ACPX is missing.

    Codex review fold #8: also enforce the version pin softly — log
    a warning if the installed version doesn't match
    ``EXPECTED_ACPX_VERSION``. Doesn't fail (operators may want to
    test newer versions); just signals the drift so a parse / CLI-
    flag regression on an alpha bump has a paper trail.
    """
    binary = _acpx_binary()
    path = shutil.which(binary)
    if not path:
        return (False, f"{binary!r} not on PATH")
    # Cheap version probe; intentional sync so this can be called
    # from synchronous startup code paths.
    import subprocess
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return (False, f"{binary} probe failed: {exc}")
    if result.returncode != 0:
        return (False, f"{binary} --version returned {result.returncode}")
    detail = (result.stdout or result.stderr).strip()
    if detail and detail != EXPECTED_ACPX_VERSION:
        logger.warning(
            "ACPX_VERSION_DRIFT: installed=%s expected=%s — adapter is "
            "pinned at the expected version; CLI flag / NDJSON event "
            "shape changes in alpha bumps may break dispatches. "
            "Override the expected version via KERNOS_ACPX_VERSION "
            "or downgrade via `npm install -g acpx@%s`.",
            detail, EXPECTED_ACPX_VERSION, EXPECTED_ACPX_VERSION,
        )
    return (True, detail)


# ---------------------------------------------------------------------
# Substrate-derived session ID
# ---------------------------------------------------------------------


def derive_session_id(
    *,
    instance_id: str,
    target: str,
    member_id: str = "",
    conversation_id: str = "",
) -> str:
    """Derive a deterministic ACPX named-session id from substrate
    coordinates. Same (instance_id, target, member_id, conversation_id)
    always returns the same session id so multi-turn dispatches in one
    conversation auto-thread without the agent managing session
    state.

    Mirrors the architect's call (Codex review #5, founder ratified):
    "Derive from (instance_id, member_id or conversation_id, target,
    active_space_id/conversation_id), sanitized with the existing
    hash helper."

    Returns a 16-char prefix of the sanitized SHA-256 hex so the
    session name fits comfortably in ACPX session storage and remains
    operator-readable in logs without being so long it bloats
    command lines.
    """
    raw = "|".join([
        instance_id or "",
        target or "",
        member_id or "",
        conversation_id or "",
    ])
    full = sanitize_session_id(raw)
    return full[:16] if full else ""


# ---------------------------------------------------------------------
# NDJSON stream parsing
# ---------------------------------------------------------------------


class _ParseFailure(Exception):
    """Internal: signals the line was non-blank but not parseable as
    JSON. Used to distinguish 'blank line / control output' (skip
    silently) from 'malformed JSON' (count + skip). Codex review fold
    #5."""


def _parse_ndjson_event(line: str) -> dict[str, Any] | None:
    """Parse one NDJSON line.

    Returns:
      - dict on parsed JSON object (the only useful shape — see fold #4)
      - None on blank/whitespace-only line
    Raises:
      - _ParseFailure on non-blank input that isn't valid JSON or that
        decodes to a non-dict value (bool/number/string/array/null —
        all valid JSON but not ACP event shapes). The streaming loop
        catches this, counts it via parse_errors, and continues
        (per Codex review fold #5).
    """
    trimmed = line.strip()
    if not trimmed:
        return None
    try:
        parsed = json.loads(trimmed)
    except json.JSONDecodeError as exc:
        raise _ParseFailure(str(exc)) from exc
    if not isinstance(parsed, dict):
        # Codex review fold #4: valid JSON that's not a dict
        # (e.g., bare string/number/array/null) would cause
        # downstream .get() calls to crash. Treat as malformed.
        raise _ParseFailure(
            f"event is not a JSON object (got {type(parsed).__name__})"
        )
    return parsed


def _extract_agent_message_chunk(event: dict[str, Any]) -> str | None:
    """Per Codex's pre-spec review:
    'Accumulate final text from session/update events where
    params.update.sessionUpdate == "agent_message_chunk"; also tolerate
    normalized text_delta/agent_message_chunk shapes for forward
    compatibility.'

    Returns the text fragment if this event is an agent message
    chunk in any of the recognized shapes; otherwise None.

    Codex review fold #4: each nested level is dict-checked
    explicitly so a malformed event with a non-dict 'params' /
    'update' / 'content' doesn't crash the drain task.
    """
    method = event.get("method") or ""
    if method == "session/update":
        params = event.get("params")
        if isinstance(params, dict):
            update = params.get("update")
            if isinstance(update, dict):
                kind = update.get("sessionUpdate") or ""
                if kind == "agent_message_chunk":
                    content = update.get("content")
                    if isinstance(content, dict):
                        # ACP content envelope variants seen in the wild:
                        text = content.get("text")
                        if isinstance(text, str):
                            return text
                        # Some adapters wrap as {type: text_delta, value: ...}
                        value = content.get("value")
                        if isinstance(value, str):
                            return value
    # Forward-compat: top-level text_delta shape (some emerging
    # implementations route deltas at the event root rather than
    # inside session/update).
    if event.get("type") == "text_delta":
        delta = event.get("delta")
        if isinstance(delta, str):
            return delta
    return None


def _extract_stop_reason(event: dict[str, Any]) -> str | None:
    """Detect a JSON-RPC ``prompt`` response carrying ``stopReason``,
    which signals turn completion per Codex's pre-spec review.
    """
    # JSON-RPC response envelope: has 'result' and optionally 'id'.
    result = event.get("result")
    if isinstance(result, dict):
        stop_reason = result.get("stopReason")
        if isinstance(stop_reason, str) and stop_reason:
            return stop_reason
    return None


# ---------------------------------------------------------------------
# Session-ensure helper (for multi-turn dispatches)
# ---------------------------------------------------------------------


async def _ensure_session(
    *,
    binary: str,
    workspace_dir: str,
    acpx_agent_name: str,
    session_id: str,
) -> None:
    """Idempotently create the ACPX named session if it doesn't exist.

    `acpx <agent> sessions ensure --name <session>` returns 0
    whether the session was created fresh or already existed —
    intended for exactly this "set up before dispatch" pattern.
    Cheap probe (one subprocess call, no model inference); fail-loud
    on errors so the caller can decide whether to fall back to
    one-shot dispatch.
    """
    cmd = [
        binary,
        "--cwd", workspace_dir,
        acpx_agent_name,
        "sessions", "ensure",
        "--name", session_id,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        raise ConsultationFailed(
            f"failed to spawn {binary!r} for sessions ensure: {exc}"
        ) from exc

    # Codex review fold #2 (mirrored from dispatch): drain stdout
    # and stderr concurrently with wait() so a chatty child can't
    # block on a full pipe and false-timeout the ensure.
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    async def _read_pipe(reader, sink) -> None:
        if reader is None:
            return
        async for line in reader:
            sink.append(line)

    drain_stdout = asyncio.create_task(_read_pipe(proc.stdout, stdout_chunks))
    drain_stderr = asyncio.create_task(_read_pipe(proc.stderr, stderr_chunks))
    timed_out = False

    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout=30)
        except asyncio.TimeoutError:
            timed_out = True
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except Exception:
                pass
        for task in (drain_stdout, drain_stderr):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            drain_stdout, drain_stderr, return_exceptions=True,
        )

    if timed_out:
        raise ConsultationFailed(
            f"acpx sessions ensure for {session_id!r} timed out"
        )
    if proc.returncode != 0:
        stderr_text = (
            b"".join(stderr_chunks)
            .decode("utf-8", errors="replace")
        )[:300]
        raise ConsultationFailed(
            f"acpx sessions ensure for {session_id!r} returned "
            f"rc={proc.returncode}: {stderr_text}"
        )


# ---------------------------------------------------------------------
# The dispatch entry point
# ---------------------------------------------------------------------


async def dispatch(
    *,
    target: str,
    prompt: str,
    session_id: str = "",
    workspace_dir: str = "",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    approve_all: bool = True,
) -> ConsultResult:
    """Dispatch ``prompt`` to ``target`` via ACPX and return a
    :class:`ConsultResult`.

    Args:
        target: Kernos-boundary agent name (``claude_code``, ``codex``,
            or ``gemini``). Translated to the ACPX agent name
            internally via the alias map.
        prompt: Free-text prompt. Sent verbatim to the agent.
        session_id: Substrate-derived ACPX session name for multi-turn
            continuity. Empty = ephemeral one-shot session.
        workspace_dir: Repo root path passed to ``--cwd``. Defaults
            to the current working directory (Kernos's source root).
        timeout_seconds: Wall-clock cap on the dispatch. Clamped to
            ``MAX_TIMEOUT_SECONDS``.
        approve_all: If True, pass ``--approve-all`` so the agent's
            permission prompts auto-approve. Default True for broker
            use; set False for paths that need permission gating.

    Returns:
        ConsultResult with ``.response`` populated from accumulated
        agent message chunks. Raw NDJSON event count + last
        ``stopReason`` (if any) land in ``.metadata``.

    Raises:
        HarnessUnavailable: ACPX binary not on PATH or target name
            not in the alias map.
        ConsultationTimeout: dispatch exceeded ``timeout_seconds``.
        ConsultationFailed: subprocess error, malformed stream, etc.
    """
    if target not in _TARGET_ALIAS_MAP:
        available = ", ".join(sorted(SUPPORTED_TARGETS))
        raise HarnessUnavailable(
            f"target {target!r} not supported by ACPX adapter. "
            f"Available: {available}"
        )
    binary = _acpx_binary()
    if not shutil.which(binary):
        raise HarnessUnavailable(
            f"{binary!r} not on PATH. Install with "
            f"`npm install -g acpx@0.8.0` or set "
            f"KERNOS_ACPX_AUTO_INSTALL=1 to auto-install at bring-up."
        )

    if timeout_seconds <= 0:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    timeout_seconds = min(timeout_seconds, MAX_TIMEOUT_SECONDS)

    acpx_agent_name = _TARGET_ALIAS_MAP[target]
    workspace_dir = workspace_dir or os.getcwd()

    # ACPX requires an explicit session in --format json mode. For
    # one-shot dispatches (no session_id), use the `exec` subcommand
    # which spawns an ephemeral session that the agent doesn't have
    # to track. For multi-turn (session_id supplied), `sessions
    # ensure` the named session first so the second + subsequent
    # turns can reuse it.
    if session_id:
        await _ensure_session(
            binary=binary,
            workspace_dir=workspace_dir,
            acpx_agent_name=acpx_agent_name,
            session_id=session_id,
        )
        cmd: list[str] = [
            binary,
            "--cwd", workspace_dir,
            "--format", "json",
        ]
        if approve_all:
            cmd.append("--approve-all")
        cmd.extend([acpx_agent_name, "-s", session_id, prompt])
    else:
        cmd = [
            binary,
            "--cwd", workspace_dir,
            "--format", "json",
        ]
        if approve_all:
            cmd.append("--approve-all")
        cmd.extend([acpx_agent_name, "exec", prompt])

    logger.info(
        "ACPX_DISPATCH: target=%s acpx_agent=%s session_id=%s "
        "timeout=%ds cwd=%s",
        target, acpx_agent_name, session_id or "(ephemeral)",
        timeout_seconds, workspace_dir,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        raise HarnessUnavailable(
            f"failed to spawn {binary!r}: {exc}"
        ) from exc

    accumulated_chunks: list[str] = []
    stderr_chunks: list[bytes] = []
    event_count = 0
    last_stop_reason: str = ""
    parse_errors = 0

    async def _drain_stdout() -> None:
        nonlocal event_count, last_stop_reason, parse_errors
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            try:
                line = raw_line.decode("utf-8", errors="replace")
            except Exception:
                parse_errors += 1
                continue
            try:
                event = _parse_ndjson_event(line)
            except _ParseFailure:
                # Codex review fold #5: count malformed JSON
                # (non-blank lines that didn't parse) so metadata
                # reports the real signal.
                parse_errors += 1
                continue
            if event is None:
                # Blank / whitespace-only line — skip silently.
                continue
            event_count += 1
            chunk = _extract_agent_message_chunk(event)
            if chunk is not None:
                accumulated_chunks.append(chunk)
            stop = _extract_stop_reason(event)
            if stop:
                last_stop_reason = stop

    async def _drain_stderr() -> None:
        # Codex review fold #2: drain stderr concurrently with
        # stdout so a noisy child can't block on a full stderr
        # pipe (which would cause a false timeout on otherwise-
        # healthy dispatches).
        assert proc.stderr is not None
        async for raw_line in proc.stderr:
            stderr_chunks.append(raw_line)

    drain_stdout_task = asyncio.create_task(_drain_stdout())
    drain_stderr_task = asyncio.create_task(_drain_stderr())
    timed_out = False
    drain_incomplete = False

    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
    finally:
        # Codex review fold #1: guarantee subprocess cleanup on any
        # exit from the wait — including caller-side cancellation
        # (CancelledError will propagate AFTER this finally runs).
        # Without this, a cancelled coroutine leaks the child.
        if proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except Exception:
                pass
        # Codex review fold #3: cancel AND await the drain tasks so
        # exceptions are observed and we don't get "Task was
        # destroyed but it is pending" warnings.
        for task in (drain_stdout_task, drain_stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            drain_stdout_task, drain_stderr_task,
            return_exceptions=True,
        )

    if timed_out:
        raise ConsultationTimeout(
            f"ACPX dispatch to {target!r} exceeded {timeout_seconds}s"
        )

    # Codex review fold #6: if drain task hit an unexpected exception
    # we couldn't observe with the gather above, surface it via the
    # task's exception() call. Skipped for CancelledError (expected
    # cleanup), but real exceptions become a failure.
    for task in (drain_stdout_task, drain_stderr_task):
        if task.done() and not task.cancelled():
            exc = task.exception()
            if exc is not None:
                drain_incomplete = True
                logger.warning(
                    "ACPX_DRAIN_EXCEPTION: target=%s task=%s exc=%s",
                    target, task.get_name(), exc,
                )

    response_text = "".join(accumulated_chunks)
    response_text, truncated = response_truncate(response_text)
    stderr_text = (
        b"".join(stderr_chunks)
        .decode("utf-8", errors="replace")
    )[:500]

    # Codex review fold #6 + #7: completion = process exit cleanly
    # AND we got an explicit stopReason (or, lenient fallback, a
    # clean exit with rc=0 and SOME response text). Treat a non-zero
    # exit with partial text as a failure — without that distinction,
    # a crash mid-response is indistinguishable from a complete answer.
    if proc.returncode != 0:
        # Hard failure: non-zero exit. Even if some chunks
        # arrived, we don't know if the response is complete.
        raise ConsultationFailed(
            f"ACPX dispatch to {target!r} exited rc={proc.returncode}. "
            f"Accumulated {len(response_text)} chars before failure. "
            f"stderr: {stderr_text}"
        )
    if drain_incomplete:
        raise ConsultationFailed(
            f"ACPX dispatch to {target!r} completed but stdout drain "
            f"raised an exception — response may be truncated. "
            f"stderr: {stderr_text}"
        )
    if not last_stop_reason and not response_text:
        # Clean exit, no stopReason, no text. Almost certainly a
        # silent failure mode.
        raise ConsultationFailed(
            f"ACPX dispatch to {target!r} exited cleanly but produced "
            f"no response and no stopReason. stderr: {stderr_text}"
        )

    metadata: dict[str, Any] = {
        "acpx_event_count": event_count,
        "acpx_parse_errors": parse_errors,
        "acpx_stop_reason": last_stop_reason,
        "acpx_target": acpx_agent_name,
        "acpx_returncode": proc.returncode,
    }

    logger.info(
        "ACPX_DISPATCH_COMPLETE: target=%s response_chars=%d events=%d "
        "stop_reason=%s",
        target, len(response_text), event_count,
        last_stop_reason or "(none)",
    )

    return ConsultResult(
        response=response_text,
        harness=target,
        session_id=session_id,
        native_session_ref=session_id,
        metadata=metadata,
        truncated=truncated,
    )


__all__ = [
    "SUPPORTED_TARGETS",
    "derive_session_id",
    "dispatch",
    "is_acpx_available",
]
