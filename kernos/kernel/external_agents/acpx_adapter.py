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
import uuid
from typing import Any, AsyncIterator

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


def _collect_descendants(root_pid: int) -> list[int]:
    """Return all descendant PIDs of ``root_pid`` (BFS), Linux-only via
    ``/proc/<pid>/task/<tid>/children``.

    Why we need this: ACPX spawns ``codex-acp`` (and ``claude-acp`` /
    ``gemini-acp``) via ``npm exec`` → ``sh -c`` → ``node`` → the ACP
    binary itself. The intermediate ``npm exec`` chain calls ``setsid``
    so process-group reap via ``killpg`` doesn't reach the leaves —
    they escape our group. We have to walk the descendant tree
    explicitly and SIGKILL each leaf to fully tear down the dispatch.

    Returns an empty list on non-Linux platforms or if ``/proc`` is
    unavailable. The caller should always be defensive — orphan
    cleanup is best-effort.
    """
    descendants: list[int] = []
    queue: list[int] = [root_pid]
    while queue:
        pid = queue.pop(0)
        children_path = f"/proc/{pid}/task/{pid}/children"
        try:
            with open(children_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if not content:
            continue
        for child_str in content.split():
            try:
                child_pid = int(child_str)
            except ValueError:
                continue
            descendants.append(child_pid)
            queue.append(child_pid)
    return descendants


def _kill_tree(root_pid: int) -> int:
    """SIGKILL the descendant tree rooted at ``root_pid``. Returns the
    number of PIDs we sent the signal to.

    The root itself is NOT killed here — the caller already handles
    the direct child via ``proc.kill()``. We only need the descendants
    that escaped the process group.

    Leaves-first ordering so a parent's death doesn't reparent its
    children to init mid-kill.
    """
    import signal
    descendants = _collect_descendants(root_pid)
    if not descendants:
        return 0
    killed = 0
    for pid in reversed(descendants):
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return killed


_ACP_AGENT_MARKERS: tuple[str, ...] = (
    "codex-acp", "claude-acp", "gemini-acp",
)


def reap_orphaned_acp_agents(max_age_sec: int | None = None) -> int:
    """SIGKILL orphaned ACP agent processes (``codex-acp`` / ``claude-acp`` /
    ``gemini-acp``) older than ``max_age_sec`` (default
    :data:`MAX_TIMEOUT_SECONDS`). Returns the number killed.

    Why this exists: the per-dispatch teardown (:func:`_kill_tree`) only walks
    DOWN from the acpx subprocess. But these agents ``setsid`` out of that
    subtree (via the ``npm exec`` chain) and, when their intermediate parent
    dies, reparent UP to ``systemd --user`` — escaping the descendant walk
    entirely. So a dispatch that times out (or whose leaf detaches) leaves an
    orphan that survives every server restart and accumulates indefinitely,
    eventually wedging the ACP layer so new consults hang. Observed live:
    8 orphans up to ~13 days old, correlated with impl-consult hangs.

    SAFE: the age guard guarantees we never touch a LIVE consult — no consult
    can exceed ``MAX_TIMEOUT_SECONDS``, so any ACP agent older than that is
    provably orphaned. Matches the exact agent binary names (not a loose
    ``-acp`` substring, which would hit ``acpi`` kernel threads). Best-effort,
    Linux/ps-based, never raises. Intended to run at server boot (frequent now
    that auto-update restarts on a short interval) so orphans never pile up.
    """
    import signal
    import subprocess
    cutoff = MAX_TIMEOUT_SECONDS if max_age_sec is None else int(max_age_sec)
    killed = 0
    try:
        out = subprocess.run(
            ["ps", "-eo", "etimes,pid,cmd"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return 0
        for line in out.stdout.splitlines()[1:]:
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            etimes_s, pid_s, cmd = parts
            if not any(m in cmd for m in _ACP_AGENT_MARKERS):
                continue
            try:
                age = int(etimes_s)
                pid = int(pid_s)
            except ValueError:
                continue
            if age <= cutoff:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError, OSError):
                pass
    except Exception as exc:  # best-effort; never break boot
        logger.warning("ACPX_ORPHAN_REAP_FAILED: %s", exc)
        return killed
    if killed:
        logger.info(
            "ACPX_ORPHAN_REAP: killed %d orphaned ACP agent(s) older than %ds",
            killed, cutoff,
        )
    return killed


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
    os.environ.get("KERNOS_ACPX_TIMEOUT_SEC", "1200")
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


async def _read_lines_unbounded(
    reader: asyncio.StreamReader | None,
    chunk_size: int = 65536,
) -> AsyncIterator[bytes]:
    """Async iterator over lines from ``reader`` with NO per-line limit.

    ACPX-DRAIN-OVERRUN-FIX-V1 (2026-05-24). The default
    ``asyncio.StreamReader`` enforces a 64 KiB per-line limit;
    ``async for line in reader`` raises ``LimitOverrunError`` ("Separator
    is found, but chunk is longer than limit") when a single line exceeds
    it. Coding-agent JSON-RPC events can carry inlined file contents that
    routinely breach 64 KiB — for instance, asking Claude Code to read a
    multi-hundred-line spec produces a single ``session/update`` event
    well over the limit. The drain task then crashes, ``drain_incomplete``
    is set, and the substrate surfaces ``ConsultationFailed`` to the
    agent — even though the underlying process was healthy.

    This helper reads bytes in fixed-size chunks, buffers them, and
    splits on ``b"\\n"`` manually, sidestepping the per-line limit
    entirely. Each yielded line includes its trailing newline (parity
    with the ``async for line in reader`` shape it replaces). EOF flushes
    any residual buffered bytes as a final unterminated line so partial
    last-line content isn't dropped. Cancellation propagates from
    ``reader.read()`` cleanly.

    chunk_size defaults to 64 KiB — matching asyncio's default high-water
    so memory profile is unchanged in the common case where lines are
    small. Lines larger than chunk_size accumulate across multiple reads
    in the buffer.
    """
    if reader is None:
        return
    buffer = bytearray()
    while True:
        chunk = await reader.read(chunk_size)
        if not chunk:
            # EOF — flush any remaining buffered bytes as a final line.
            if buffer:
                yield bytes(buffer)
                buffer.clear()
            return
        buffer.extend(chunk)
        while True:
            newline_idx = buffer.find(b"\n")
            if newline_idx == -1:
                break
            line = bytes(buffer[: newline_idx + 1])
            del buffer[: newline_idx + 1]
            yield line


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


def _extract_error_message(event: dict[str, Any]) -> str | None:
    """Pull a human-readable error from a JSON-RPC error response.

    Surfaced after the 2026-05-20 root-cause: claude-acp returned
    ``{"jsonrpc":"2.0","id":null,"error":{"code":-32603,"message":
    "Internal error: Credit balance is too low",
    "data":{"errorKind":"billing_error"}}}`` on stdout. The dispatch
    error message only logged stderr (empty), hiding the real cause.
    This extractor turns those JSON-RPC error envelopes into a
    short string we surface in the rc!=0 raise.

    Returns short error description or None if event isn't an error
    envelope. Defensive against malformed shapes.
    """
    err = event.get("error")
    if not isinstance(err, dict):
        return None
    msg = err.get("message")
    if not isinstance(msg, str) or not msg:
        return None
    data = err.get("data")
    kind = ""
    if isinstance(data, dict):
        ek = data.get("errorKind")
        if isinstance(ek, str) and ek:
            kind = f" [{ek}]"
    return f"{msg}{kind}"


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


async def _close_session(
    *,
    binary: str,
    workspace_dir: str,
    acpx_agent_name: str,
    session_id: str,
) -> bool:
    """Force-close an ACPX named session via ``acpx <agent> sessions
    close <name>``. Returns True on clean close (or "session didn't
    exist anyway"), False on error.

    Why: when an ACPX named session's underlying agent process dies,
    ``sessions ensure`` reports the session "exists" but every
    subsequent dispatch fails with stderr ``agent needs reconnect``.
    There's no ``sessions reset`` — close + re-ensure is the only
    way to refresh the agent process bound to the name.

    Fire-and-forget shape: any failure here just means the next
    ``sessions ensure`` may also fail; we log + continue rather
    than blow up the dispatch path.
    """
    cmd = [
        binary,
        "--cwd", workspace_dir,
        acpx_agent_name,
        "sessions", "close",
        session_id,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.warning(
            "ACPX_SESSION_CLOSE_SPAWN_FAILED: target=%s session=%s exc=%s",
            acpx_agent_name, session_id, exc,
        )
        return False
    try:
        await asyncio.wait_for(proc.wait(), timeout=15)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
        logger.warning(
            "ACPX_SESSION_CLOSE_TIMEOUT: target=%s session=%s",
            acpx_agent_name, session_id,
        )
        return False
    # rc=0 means closed. Non-zero with "no session" or similar still
    # leaves us in the "session doesn't exist" state which is what
    # we want — log + return success.
    if proc.returncode == 0:
        logger.info(
            "ACPX_SESSION_CLOSED: target=%s session=%s",
            acpx_agent_name, session_id,
        )
        return True
    # Non-zero close — common when session already gone. Either
    # way the next ensure will create fresh.
    logger.info(
        "ACPX_SESSION_CLOSE_NONZERO: target=%s session=%s rc=%d "
        "(treating as already-gone)",
        acpx_agent_name, session_id, proc.returncode,
    )
    return True


# Stderr markers indicating a named session exists but its bound
# agent process has died. Detection triggers a force-close + retry.
# Conservative list — any marker MUST be a near-certain indicator of
# stale-agent, not a generic error.
_STALE_AGENT_MARKERS: tuple[str, ...] = (
    "agent needs reconnect",
    "agent disconnected",
)


def _stderr_indicates_stale_agent(stderr_text: str) -> bool:
    """True iff stderr contains a stale-agent marker. Case-insensitive
    substring match; the markers are stable in ACPX's stderr."""
    if not stderr_text:
        return False
    lowered = stderr_text.lower()
    return any(marker.lower() in lowered for marker in _STALE_AGENT_MARKERS)


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
            # Same process-group reap rationale as dispatch — mirror
            # so ensure-time leaks (probably none in practice since
            # `sessions ensure` exits fast, but defensive) can't
            # accumulate.
            start_new_session=True,
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
        # ACPX-DRAIN-OVERRUN-FIX-V1: use the unbounded helper instead
        # of ``async for line in reader`` so >64 KiB lines don't crash
        # the drain. Same fix applied to the dispatch-side drains.
        async for line in _read_lines_unbounded(reader):
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
        # Descendant tree reap (see dispatch's matching block for
        # the full rationale).
        try:
            _kill_tree(proc.pid)
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
        # 2026-05-20 Codex audit fix: mirror dispatch's stdout-error
        # surfacing here too. If sessions ensure fails because of a
        # provider/auth error (which comes via JSON-RPC on stdout
        # for the same reason claude-acp's billing_error did), the
        # raise message needs the stdout payload, not just stderr.
        stdout_text = (
            b"".join(stdout_chunks)
            .decode("utf-8", errors="replace")
        )[:300]
        raise ConsultationFailed(
            f"acpx sessions ensure for {session_id!r} returned "
            f"rc={proc.returncode}: stderr={stderr_text!r} "
            f"stdout={stdout_text!r}"
        )


# ---------------------------------------------------------------------
# Timeout diagnostic writer (ACPX_TIMEOUT_DIAGNOSTICS — 2026-05-22)
# ---------------------------------------------------------------------


def _write_acpx_timeout_friction_report(
    *,
    target: str,
    acpx_agent_name: str,
    session_id: str,
    timeout_seconds: int,
    dispatch_started_at: float,
    last_event_at: float | None,
    last_event_kind: str,
    event_count: int,
    parse_errors: int,
    stdout_errors: list[str],
    stderr_chunks: list[bytes],
    last_stop_reason: str,
    workspace_dir: str,
    prompt_preview: str,
) -> str:
    """Drop a markdown friction report when an ACPX dispatch
    exceeds its timeout. Captures the diagnostic state that
    prior ConsultationTimeout exceptions lacked (events seen,
    last-event timing, stderr tail, stdout-channel errors) so
    the next investigation has evidence the prior incident
    couldn't surface. Returns the absolute path written, or ""
    if no friction folder is configured.

    Mirrors the FrictionObserver / IntegrationRunner naming +
    location convention so existing surfacing tooling (`/debug
    friction`, session-start scans) picks the report up.
    """
    import time as _t
    from datetime import datetime, timezone
    from pathlib import Path

    data_dir = os.environ.get("KERNOS_DATA_DIR", "./data")
    friction_dir = Path(data_dir) / "diagnostics" / "friction"
    try:
        friction_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return ""

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    unique = uuid.uuid4().hex[:8]
    filename = f"FRICTION_{ts}_{unique}_ACPX_TIMEOUT_{target.upper()}.md"
    filepath = friction_dir / filename

    elapsed_total = _t.monotonic() - dispatch_started_at
    silence_seconds: str = "n/a (no events received)"
    if last_event_at is not None:
        silence_seconds = (
            f"{_t.monotonic() - last_event_at:.1f}s since last event"
        )

    stderr_tail = b"".join(stderr_chunks[-50:]).decode(
        "utf-8", errors="replace",
    )[-2000:]

    lines: list[str] = []
    lines.append(f"# Friction Report: ACPX_TIMEOUT_{target.upper()}")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append("")
    lines.append("## Description")
    lines.append(
        f"ACPX dispatch to `{target}` (agent=`{acpx_agent_name}`) "
        f"exceeded the `{timeout_seconds}s` timeout. This report "
        f"captures the state at the moment of timeout so the root "
        f"cause (process never started / auth hang / mid-response "
        f"stall / Codex API drift) can be attributed."
    )
    lines.append("")
    lines.append("## Diagnostic state")
    lines.append(f"- target: `{target}`")
    lines.append(f"- acpx_agent: `{acpx_agent_name}`")
    lines.append(f"- session_id: `{session_id or '(ephemeral)'}`")
    lines.append(f"- workspace_dir: `{workspace_dir}`")
    lines.append(f"- configured_timeout: `{timeout_seconds}s`")
    lines.append(f"- actual_elapsed: `{elapsed_total:.1f}s`")
    lines.append(f"- events_received: `{event_count}`")
    lines.append(f"- last_event_kind: `{last_event_kind or '(none)'}`")
    lines.append(f"- silence_at_timeout: `{silence_seconds}`")
    lines.append(f"- parse_errors: `{parse_errors}`")
    lines.append(f"- last_stop_reason: `{last_stop_reason or '(none)'}`")
    lines.append(f"- stderr_bytes: `{sum(len(c) for c in stderr_chunks)}`")
    lines.append("")
    lines.append("## Triage hints")
    if event_count == 0:
        lines.append(
            "- **No events ever received** — most likely process "
            "never produced parseable output. Check stderr tail for "
            "auth failures (`401`, `Unauthorized`), missing binary, "
            "or Codex CLI startup errors. Also see "
            "[[reference_codex_creds_resync]] in case credentials "
            "are stale."
        )
    elif last_event_at is not None and (
        _t.monotonic() - last_event_at
    ) > timeout_seconds * 0.5:
        lines.append(
            "- **Stalled mid-stream** — events flowed for a while "
            "then stopped. Check last_event_kind for the last "
            "successful step. Could be a tool call inside the "
            "ACP server hanging on a downstream API."
        )
    else:
        lines.append(
            "- **Long-running normal dispatch** — events kept "
            "flowing right up to timeout. The work itself may "
            "legitimately exceed the budget. Consider raising "
            "`KERNOS_ACPX_TIMEOUT_SEC` for this kind of consultation."
        )
    if stdout_errors:
        lines.append(
            "- **stdout error channel populated** — the ACP server "
            "reported errors via JSON-RPC. See stdout_errors below."
        )
    lines.append("")
    if stdout_errors:
        lines.append("## stdout_errors (JSON-RPC channel)")
        for e in stdout_errors[-10:]:
            lines.append(f"- `{e}`")
        lines.append("")
    lines.append("## Prompt preview")
    lines.append("```")
    lines.append(prompt_preview or "(empty)")
    lines.append("```")
    lines.append("")
    lines.append("## stderr tail (last ~2KB)")
    lines.append("```")
    lines.append(stderr_tail or "(empty)")
    lines.append("```")

    try:
        filepath.write_text("\n".join(lines), encoding="utf-8")
        logger.info(
            "ACPX_TIMEOUT_DIAG_REPORT_WRITTEN path=%s events=%d "
            "last_event_kind=%r", filepath, event_count,
            last_event_kind,
        )
        return str(filepath)
    except Exception as exc:
        logger.warning(
            "ACPX_TIMEOUT_DIAG_REPORT_WRITE_FAILED exc=%s", exc,
        )
        return ""


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
    _stale_session_retry: int = 0,
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
            # Put acpx in its own process group so we can kill the
            # whole tree on teardown — codex-acp / claude-acp / etc.
            # are spawned as grandchildren via npx, and acpx itself
            # doesn't always reap them on its own exit. Without this,
            # every dispatch leaks a long-lived ACP server process
            # (parent reassigns to systemd-user / PID 1).
            start_new_session=True,
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
    # 2026-05-20 root-cause fix: accumulate JSON-RPC error messages
    # from stdout (e.g. claude-acp's "Credit balance is too low"
    # billing_error). Without this, errors that come via the
    # JSON-RPC channel are invisible to the dispatch failure message
    # — only stderr gets surfaced, which is often empty for these.
    stdout_errors: list[str] = []
    # 2026-05-22 ACPX_TIMEOUT_DIAGNOSTICS: track last-event timing so
    # ConsultationTimeout can attribute the hang. "no events ever" vs
    # "events stopped at T+45s" point at very different root causes
    # (process never started / auth-401-hang vs mid-response stall).
    import time as _time_diag
    dispatch_started_at = _time_diag.monotonic()
    last_event_at: float | None = None
    last_event_kind: str = ""

    async def _drain_stdout() -> None:
        nonlocal event_count, last_stop_reason, parse_errors
        nonlocal last_event_at, last_event_kind
        assert proc.stdout is not None
        # ACPX-DRAIN-OVERRUN-FIX-V1: coding-agent JSON-RPC events can
        # exceed asyncio's default 64 KiB per-line limit when they
        # inline file contents (e.g. claude_code reading a multi-
        # hundred-line spec). Use the unbounded helper so the drain
        # task can't crash with LimitOverrunError — that was surfacing
        # to callers as opaque ConsultationFailed at the substrate.
        async for raw_line in _read_lines_unbounded(proc.stdout):
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
            # ACPX_TIMEOUT_DIAGNOSTICS: stamp last-event time + kind
            # so a stalled dispatch attributes correctly post-hoc.
            last_event_at = _time_diag.monotonic()
            try:
                last_event_kind = str(event.get("type", "") or event.get("kind", ""))
            except AttributeError:
                last_event_kind = "?"
            chunk = _extract_agent_message_chunk(event)
            if chunk is not None:
                accumulated_chunks.append(chunk)
            err_msg = _extract_error_message(event)
            if err_msg:
                stdout_errors.append(err_msg)
            stop = _extract_stop_reason(event)
            if stop:
                last_stop_reason = stop

    async def _drain_stderr() -> None:
        # Codex review fold #2: drain stderr concurrently with
        # stdout so a noisy child can't block on a full stderr
        # pipe (which would cause a false timeout on otherwise-
        # healthy dispatches).
        # ACPX-DRAIN-OVERRUN-FIX-V1: stderr can also exceed the 64
        # KiB per-line limit (less common, but possible on Python
        # traceback dumps with embedded source); using the unbounded
        # helper here for symmetry with stdout.
        assert proc.stderr is not None
        async for raw_line in _read_lines_unbounded(proc.stderr):
            stderr_chunks.append(raw_line)

    # Descendant-tracking task: walks /proc/<acpx_pid>/.../children
    # every ~0.5s and accumulates a set of every PID acpx ever
    # spawned during the dispatch. Required because by the time the
    # finally block runs, acpx itself may have exited and its
    # children reparented to PID 1 — at that point a post-hoc walk
    # finds nothing, but the codex-acp / claude-acp daemons keep
    # running. Snapshotting WHILE acpx is alive is the only reliable
    # way to track them for cleanup.
    known_descendants: set[int] = set()

    async def _track_descendants() -> None:
        while True:
            try:
                for pid in _collect_descendants(proc.pid):
                    known_descendants.add(pid)
            except Exception:
                pass
            await asyncio.sleep(0.5)

    drain_stdout_task = asyncio.create_task(_drain_stdout())
    drain_stderr_task = asyncio.create_task(_drain_stderr())
    tracker_task = asyncio.create_task(_track_descendants())
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
        # Stop the descendant tracker and fold in any final snapshot
        # before we kill (in case the very last children appeared in
        # the gap between the tracker's last tick and now).
        if not tracker_task.done():
            tracker_task.cancel()
        try:
            for pid in _collect_descendants(proc.pid):
                known_descendants.add(pid)
        except Exception:
            pass

        # Descendant tree reap: acpx spawns codex-acp / claude-acp as
        # grandchildren via `npm exec` → `sh -c` → `node` → the ACP
        # binary. `npm exec` calls setsid internally, so process-group
        # reap (killpg) doesn't reach the leaves — they escape our
        # group. We tracked descendants live during the dispatch
        # (see _track_descendants above) so we have their PIDs even
        # though they've been reparented away from us by the time
        # acpx exits. SIGKILL each one. Run on EVERY teardown — not
        # just timeout — because the leak shows up even on rc=0
        # happy-path dispatches.
        import signal as _signal
        killed = 0
        for pid in sorted(known_descendants, reverse=True):
            try:
                os.kill(pid, _signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if killed:
            logger.info(
                "ACPX_DESCENDANT_REAP: target=%s killed=%d "
                "tracked=%d",
                target, killed, len(known_descendants),
            )
        # Codex review fold #3: cancel AND await the drain tasks so
        # exceptions are observed and we don't get "Task was
        # destroyed but it is pending" warnings.
        for task in (drain_stdout_task, drain_stderr_task, tracker_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            drain_stdout_task, drain_stderr_task, tracker_task,
            return_exceptions=True,
        )

    if timed_out:
        # ACPX_TIMEOUT_DIAGNOSTICS (2026-05-22): on every timeout,
        # drop a friction report so the next investigation has the
        # evidence the prior incident lacked. Best-effort write —
        # never let the diagnostic raise.
        _report_path: str = ""
        try:
            _report_path = _write_acpx_timeout_friction_report(
                target=target,
                acpx_agent_name=acpx_agent_name,
                session_id=session_id or "",
                timeout_seconds=timeout_seconds,
                dispatch_started_at=dispatch_started_at,
                last_event_at=last_event_at,
                last_event_kind=last_event_kind,
                event_count=event_count,
                parse_errors=parse_errors,
                stdout_errors=list(stdout_errors),
                stderr_chunks=list(stderr_chunks),
                last_stop_reason=last_stop_reason,
                workspace_dir=workspace_dir,
                prompt_preview=(prompt or "")[:300],
            )
        except Exception as _diag_exc:
            logger.warning(
                "ACPX_TIMEOUT_DIAG_REPORT_FAILED exc=%s", _diag_exc,
            )
        _hint = (
            f" Diagnostic report: {_report_path}"
            if _report_path else ""
        )
        raise ConsultationTimeout(
            f"ACPX dispatch to {target!r} exceeded {timeout_seconds}s "
            f"(events_received={event_count}, "
            f"last_event_kind={last_event_kind!r}, "
            f"stderr_bytes={sum(len(c) for c in stderr_chunks)}).{_hint}"
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
        # Stale-session detection (2026-05-19 founder report): when an
        # ACPX named session's underlying agent died, `sessions ensure`
        # reports the session "exists" and every subsequent dispatch
        # fails with ``stderr=[acpx] agent needs reconnect``. There's
        # no `sessions reset` in ACPX — we have to close + re-ensure
        # to bind a fresh agent process to the name. One retry; if
        # the retry also fails we surface the failure honestly.
        if (
            session_id
            and _stale_session_retry == 0
            and _stderr_indicates_stale_agent(stderr_text)
        ):
            logger.warning(
                "ACPX_STALE_SESSION_DETECTED: target=%s session=%s "
                "stderr_head=%r — force-closing + retrying once",
                target, session_id, stderr_text[:150],
            )
            await _close_session(
                binary=binary,
                workspace_dir=workspace_dir,
                acpx_agent_name=acpx_agent_name,
                session_id=session_id,
            )
            return await dispatch(
                target=target,
                prompt=prompt,
                session_id=session_id,
                workspace_dir=workspace_dir,
                timeout_seconds=timeout_seconds,
                approve_all=approve_all,
                _stale_session_retry=1,
            )
        # Hard failure: non-zero exit. Even if some chunks
        # arrived, we don't know if the response is complete.
        # 2026-05-20 root-cause fix: include JSON-RPC error messages
        # captured from stdout. Without this, billing_error /
        # auth_error / etc. that come via JSON-RPC are invisible —
        # only stderr surfaces, which is often empty.
        stdout_err_text = ""
        if stdout_errors:
            # Dedup + cap to keep the failure message readable;
            # multiple identical errors collapse to one count.
            from collections import Counter
            err_counter = Counter(stdout_errors)
            err_parts = [
                f"{msg} (×{count})" if count > 1 else msg
                for msg, count in err_counter.most_common()
            ]
            stdout_err_text = " | ".join(err_parts)[:500]
        raise ConsultationFailed(
            f"ACPX dispatch to {target!r} exited rc={proc.returncode}. "
            f"Accumulated {len(response_text)} chars before failure. "
            f"stderr: {stderr_text} | "
            f"stdout_errors: {stdout_err_text or '(none)'}",
            # 2026-05-20 Codex audit fix: pass real rc through so
            # consult_log's exit_status column reflects reality
            # instead of defaulting to 0.
            exit_status=proc.returncode,
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
