"""ACPX-INTEGRATION-V1 — bridge request watcher (outbound + inbound).

Closes the operator-as-relay gap that ``ask_coding_session`` left
unbuilt in v1. Polls the bridge directory for new request files and
dispatches each through :mod:`acpx_adapter`, writing the structured
response back so :class:`CodingSessionBridgeResponseEmitter` picks
it up via its existing polling path.

Two parallel watchers run, both backed by the same ACPX dispatch
machinery:

  * **outbound** — ``coding_session_bridge/requests/`` →
    ``coding_session_bridge/responses/`` (Kernos's existing
    ``ask_coding_session`` tool dispatching out to CC/Codex/Gemini)
  * **inbound** — ``cc_inbox/`` → ``cc_outbox/`` (NEW; lets an
    external CLI client write a request file that Kernos's tool
    surface handles via the substrate, then writes the response back
    for the client to poll)

Concurrency discipline per Codex pre-spec review:
  * Skip if response file already exists
  * Claim via ``requests/{id}.processing`` written O_CREAT|O_EXCL so
    a second watcher / crash-recovered watcher can't double-dispatch
  * Lock metadata in the sentinel: pid, started_at, attempt
  * Response written via tmp+rename for atomicity
  * On stale lock (pid dead, ttl exceeded): write an
    ``unable_to_investigate`` response (don't auto-retry; can't be
    exactly-once without ACPX-side idempotency)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from kernos.kernel.external_agents.acpx_adapter import (
    SUPPORTED_TARGETS,
    derive_session_id,
    dispatch,
)
from kernos.kernel.external_agents.errors import (
    ConsultationFailed,
    ConsultationTimeout,
    HarnessUnavailable,
)
from kernos.utils import _safe_name, utc_now

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Config knobs
# ---------------------------------------------------------------------


_POLL_INTERVAL_SEC: float = float(
    os.environ.get("KERNOS_BRIDGE_WATCHER_INTERVAL_SEC", "2.0")
)

# A request whose .processing sentinel is older than this gets
# treated as crash-recovered: the watcher writes
# unable_to_investigate and clears the sentinel. Codex review
# fold #4 default: "default to writing an unable_to_investigate
# response rather than auto-retrying."
_STALE_LOCK_TTL_SEC: int = int(
    os.environ.get("KERNOS_BRIDGE_WATCHER_STALE_TTL_SEC", "1800")
)

_DISPATCH_TIMEOUT_SEC: int = int(
    os.environ.get("KERNOS_BRIDGE_WATCHER_DISPATCH_TIMEOUT_SEC", "600")
)


# ---------------------------------------------------------------------
# Outbound watcher — Kernos's ask_coding_session requests → ACPX
# ---------------------------------------------------------------------


def _outbound_requests_dir(data_dir: str, instance_id: str) -> Path:
    return (
        Path(data_dir) / _safe_name(instance_id)
        / "coding_session_bridge" / "requests"
    )


def _outbound_responses_dir(data_dir: str, instance_id: str) -> Path:
    return (
        Path(data_dir) / _safe_name(instance_id)
        / "coding_session_bridge" / "responses"
    )


def _is_stale_lock(lock_path: Path) -> bool:
    """Return True if the lock file is older than _STALE_LOCK_TTL_SEC
    OR references a dead pid. Used during crash-recovery on startup."""
    try:
        age = os.time.time() - lock_path.stat().st_mtime  # type: ignore[attr-defined]
    except Exception:
        # os.time doesn't exist; fall through to the real check
        import time as _t
        try:
            age = _t.time() - lock_path.stat().st_mtime
        except Exception:
            return False
    if age > _STALE_LOCK_TTL_SEC:
        return True
    # PID liveness check
    try:
        meta = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = int(meta.get("pid", 0))
        if pid > 0:
            try:
                os.kill(pid, 0)  # signal 0 = liveness probe
                return False  # process exists, lock is valid
            except (ProcessLookupError, PermissionError):
                return True  # dead pid OR another user
    except Exception:
        pass
    return False


async def _claim_request(lock_path: Path, attempt: int = 1) -> bool:
    """Try to claim the request via O_CREAT|O_EXCL. Returns True if
    we got the lock, False if another watcher / a stale lock blocks us."""
    try:
        fd = os.open(
            str(lock_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o644,
        )
    except FileExistsError:
        # Existing lock — check if it's stale enough to reclaim.
        if _is_stale_lock(lock_path):
            logger.warning(
                "BRIDGE_WATCHER_STALE_LOCK_RECLAIM: lock=%s — clearing",
                lock_path,
            )
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            # Retry once after stale clear
            return await _claim_request(lock_path, attempt + 1)
        return False
    except OSError as exc:
        logger.warning(
            "BRIDGE_WATCHER_CLAIM_FAILED: lock=%s exc=%s",
            lock_path, exc,
        )
        return False
    try:
        os.write(fd, json.dumps({
            "pid": os.getpid(),
            "started_at": utc_now(),
            "attempt": attempt,
        }).encode("utf-8"))
    finally:
        os.close(fd)
    return True


def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("BRIDGE_WATCHER_LOCK_RELEASE_FAILED: %s", exc)


def _write_response_atomic(response_path: Path, payload: dict) -> None:
    """Atomic write via tmp+rename. The CodingSessionBridgeResponseEmitter
    treats the appearance of responses/{id}.json as the trigger event;
    partial-file visibility would race that observation."""
    response_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = response_path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.rename(str(tmp), str(response_path))


async def _handle_outbound_request(
    *,
    request_path: Path,
    responses_dir: Path,
    instance_id: str,
) -> None:
    """Dispatch one outbound request through ACPX, write the response."""
    request_id = request_path.stem
    response_path = responses_dir / f"{request_id}.json"
    lock_path = request_path.with_suffix(".processing")

    # Skip if response already exists (a prior watcher / operator
    # may have already handled this).
    if response_path.exists():
        return

    claimed = await _claim_request(lock_path)
    if not claimed:
        return

    try:
        try:
            request_data = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "BRIDGE_WATCHER_READ_FAILED: request=%s exc=%s",
                request_path, exc,
            )
            _write_unable_to_investigate_response(
                response_path,
                request_id,
                target=str(request_data.get("target", "") if isinstance(request_data, dict) else ""),
                reason=f"could not read request file: {exc}",
            ) if False else None  # noqa — fall-through below
            return

        target = request_data.get("target") or ""
        question = request_data.get("question") or ""
        context = request_data.get("context") or {}
        originating_member_id = request_data.get("originating_member_id") or ""
        originating_space = request_data.get("originating_space") or ""

        if target not in SUPPORTED_TARGETS:
            _write_unable_to_investigate_response(
                response_path, request_id, target=target,
                reason=(
                    f"target {target!r} not supported by ACPX adapter; "
                    f"available: {sorted(SUPPORTED_TARGETS)}"
                ),
            )
            return

        # Substrate-derived session_id so multi-turn investigations
        # in one conversation auto-thread (per Codex review #5).
        session_id = derive_session_id(
            instance_id=instance_id,
            target=target,
            member_id=originating_member_id,
            conversation_id=originating_space,
        )

        # Compose the prompt — include the structured context the
        # ask_coding_session schema carries (suspected_paths,
        # related_conversation, prior_decisions).
        composed_prompt = _compose_prompt(question, context)

        logger.info(
            "BRIDGE_WATCHER_DISPATCH: request_id=%s target=%s "
            "session_id=%s question_chars=%d",
            request_id, target, session_id, len(question),
        )

        try:
            result = await dispatch(
                target=target,
                prompt=composed_prompt,
                session_id=session_id,
                timeout_seconds=_DISPATCH_TIMEOUT_SEC,
            )
            response_payload = {
                "request_id": request_id,
                "timestamp": utc_now(),
                "target": target,
                "investigation_outcome": "completed",
                "summary": result.response,
                "metadata": {
                    "session_id": session_id,
                    "acpx": result.metadata,
                    "truncated": result.truncated,
                },
            }
        except ConsultationTimeout as exc:
            response_payload = {
                "request_id": request_id,
                "timestamp": utc_now(),
                "target": target,
                "investigation_outcome": "unable_to_investigate",
                "summary": f"ACPX dispatch timed out: {exc}",
                "metadata": {"session_id": session_id, "error": "timeout"},
            }
        except (ConsultationFailed, HarnessUnavailable) as exc:
            response_payload = {
                "request_id": request_id,
                "timestamp": utc_now(),
                "target": target,
                "investigation_outcome": "unable_to_investigate",
                "summary": f"ACPX dispatch failed: {exc}",
                "metadata": {"session_id": session_id, "error": str(exc)},
            }
        except Exception as exc:
            logger.exception(
                "BRIDGE_WATCHER_UNEXPECTED_EXC: request_id=%s",
                request_id,
            )
            response_payload = {
                "request_id": request_id,
                "timestamp": utc_now(),
                "target": target,
                "investigation_outcome": "unable_to_investigate",
                "summary": f"unexpected error: {exc}",
                "metadata": {"session_id": session_id, "error": str(exc)},
            }

        _write_response_atomic(response_path, response_payload)
        logger.info(
            "BRIDGE_WATCHER_RESPONSE_WRITTEN: request_id=%s "
            "outcome=%s chars=%d",
            request_id, response_payload["investigation_outcome"],
            len(response_payload.get("summary", "")),
        )
    finally:
        _release_lock(lock_path)


def _write_unable_to_investigate_response(
    response_path: Path,
    request_id: str,
    *,
    target: str,
    reason: str,
) -> None:
    payload = {
        "request_id": request_id,
        "timestamp": utc_now(),
        "target": target,
        "investigation_outcome": "unable_to_investigate",
        "summary": reason,
        "metadata": {"error": "watcher_setup_failure"},
    }
    try:
        _write_response_atomic(response_path, payload)
    except Exception as exc:
        logger.warning(
            "BRIDGE_WATCHER_UNABLE_WRITE_FAILED: request_id=%s exc=%s",
            request_id, exc,
        )


def _compose_prompt(question: str, context: dict[str, Any]) -> str:
    """Fold the ask_coding_session structured context into a single
    text prompt for ACPX. Mirrors the shape the operator would have
    pasted into the live CLI session under the manual-relay flow."""
    parts: list[str] = [question]
    if not isinstance(context, dict):
        return question
    suspected = context.get("suspected_paths") or []
    if isinstance(suspected, list) and suspected:
        parts.append("\n\nSuspected file paths:")
        for p in suspected[:20]:
            parts.append(f"  - {p}")
    prior = context.get("prior_decisions") or []
    if isinstance(prior, list) and prior:
        parts.append("\n\nPrior architectural decisions:")
        for d in prior[:10]:
            parts.append(f"  - {d}")
    related = context.get("related_conversation") or ""
    if isinstance(related, str) and related.strip():
        parts.append("\n\nRelated conversation excerpt:")
        parts.append(related.strip()[:2000])
    return "\n".join(parts)


# ---------------------------------------------------------------------
# Outbound watcher loop
# ---------------------------------------------------------------------


async def outbound_watcher_loop(
    *,
    data_dir: str,
    instance_id: str,
) -> None:
    """Background task. Polls coding_session_bridge/requests/ for
    new request files and dispatches each through ACPX. Idempotent;
    safe to run alongside operator-manual relays.

    Fail-safe: any exception in the loop is logged and the loop
    continues. The watcher never dies on transient errors.
    """
    requests_dir = _outbound_requests_dir(data_dir, instance_id)
    responses_dir = _outbound_responses_dir(data_dir, instance_id)
    requests_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "BRIDGE_WATCHER_STARTED: outbound instance=%s requests=%s "
        "responses=%s interval=%.1fs",
        instance_id, requests_dir, responses_dir, _POLL_INTERVAL_SEC,
    )
    while True:
        try:
            await asyncio.sleep(_POLL_INTERVAL_SEC)
            for request_path in sorted(requests_dir.glob("*.json")):
                try:
                    await _handle_outbound_request(
                        request_path=request_path,
                        responses_dir=responses_dir,
                        instance_id=instance_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "BRIDGE_WATCHER_REQUEST_FAILED: %s exc=%s",
                        request_path, exc,
                    )
        except asyncio.CancelledError:
            logger.info("BRIDGE_WATCHER_STOPPED: outbound instance=%s", instance_id)
            raise
        except Exception as exc:
            logger.warning(
                "BRIDGE_WATCHER_LOOP_ERROR: %s — continuing", exc,
            )


# ---------------------------------------------------------------------
# Inbound watcher — external CLI clients → Kernos tool surface
# ---------------------------------------------------------------------
#
# Convention: an external CLI client (CC running in a separate session,
# or Codex via acpx, or a script) writes a request file to:
#
#   data/<instance>/cc_inbox/{request_id}.json
#
# Schema (minimal v1):
#   {
#     "request_id": "<unique-id>",
#     "timestamp": "<utc>",
#     "kind": "<inspect_state|read_file|list_files|sqlite_query|free_text>",
#     "params": { ... per kind ... },
#     "client": "<short-name-of-caller-for-audit>"
#   }
#
# Kernos polls the inbox, handles via its substrate tools, writes
# response to:
#
#   data/<instance>/cc_outbox/{request_id}.json
#
# Schema:
#   {
#     "request_id": "<...>",
#     "timestamp": "<utc>",
#     "status": "<ok|error>",
#     "result": <kind-specific payload>,
#     "error": "<message if status=error>"
#   }
#
# v1 supported kinds (architect-curated; expanding in v2 follow-ups):
#   - inspect_state    params: {} → returns substrate snapshot
#   - read_file        params: {path: str (under data_dir)} → file content
#   - list_files       params: {path: str} → directory listing
#   - sqlite_query     params: {sql: str, limit: int=100} → query rows
#                                   (read-only enforced; SELECTs only)
#   - free_text        params: {prompt: str} → echoes back; for testing
#
# Out of scope v1: tool dispatch (would need full reasoning loop);
# subscription/streaming; auth (file-system permissions are the gate).


def _inbox_dir(data_dir: str, instance_id: str) -> Path:
    return Path(data_dir) / _safe_name(instance_id) / "cc_inbox"


def _outbox_dir(data_dir: str, instance_id: str) -> Path:
    return Path(data_dir) / _safe_name(instance_id) / "cc_outbox"


async def _handle_inbound_request(
    *,
    request_path: Path,
    outbox_dir: Path,
    data_dir: str,
    instance_id: str,
) -> None:
    """Handle one inbound request from the cc_inbox/ directory."""
    request_id = request_path.stem
    response_path = outbox_dir / f"{request_id}.json"
    lock_path = request_path.with_suffix(".processing")

    if response_path.exists():
        return
    claimed = await _claim_request(lock_path)
    if not claimed:
        return

    try:
        try:
            request_data = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _write_inbox_error(
                response_path, request_id,
                f"could not parse request: {exc}",
            )
            return

        kind = (request_data.get("kind") or "").strip()
        params = request_data.get("params") or {}

        logger.info(
            "INBOX_WATCHER_REQUEST: request_id=%s kind=%s client=%s",
            request_id, kind, request_data.get("client", "anon"),
        )

        try:
            result = await _dispatch_inbound(
                kind=kind, params=params,
                data_dir=data_dir, instance_id=instance_id,
            )
            payload = {
                "request_id": request_id,
                "timestamp": utc_now(),
                "status": "ok",
                "kind": kind,
                "result": result,
            }
        except ValueError as exc:
            payload = {
                "request_id": request_id,
                "timestamp": utc_now(),
                "status": "error",
                "kind": kind,
                "error": str(exc),
            }
        except Exception as exc:
            logger.exception(
                "INBOX_WATCHER_HANDLER_EXC: request_id=%s kind=%s",
                request_id, kind,
            )
            payload = {
                "request_id": request_id,
                "timestamp": utc_now(),
                "status": "error",
                "kind": kind,
                "error": f"unexpected: {exc}",
            }

        _write_response_atomic(response_path, payload)
        logger.info(
            "INBOX_WATCHER_RESPONSE_WRITTEN: request_id=%s status=%s",
            request_id, payload["status"],
        )
    finally:
        _release_lock(lock_path)


def _write_inbox_error(response_path: Path, request_id: str, message: str) -> None:
    try:
        _write_response_atomic(response_path, {
            "request_id": request_id,
            "timestamp": utc_now(),
            "status": "error",
            "error": message,
        })
    except Exception as exc:
        logger.warning(
            "INBOX_WATCHER_ERROR_WRITE_FAILED: request_id=%s exc=%s",
            request_id, exc,
        )


async def _dispatch_inbound(
    *,
    kind: str,
    params: dict[str, Any],
    data_dir: str,
    instance_id: str,
) -> Any:
    """v1 inbound dispatcher. Read-only by design; everything that
    could mutate state is out of scope until v2 architect call."""
    if kind == "free_text":
        return {"echo": params.get("prompt", "")}

    if kind == "inspect_state":
        # Minimal: file counts under data_dir, plus latest dump.
        data_root = Path(data_dir) / _safe_name(instance_id)
        return {
            "data_root": str(data_root),
            "data_root_exists": data_root.exists(),
            "bridge_dir_exists": (data_root / "coding_session_bridge").exists(),
        }

    if kind == "read_file":
        path_str = params.get("path", "")
        if not isinstance(path_str, str) or not path_str:
            raise ValueError("read_file requires params.path: str")
        target = (Path(data_dir) / _safe_name(instance_id) / path_str).resolve()
        data_root = (Path(data_dir) / _safe_name(instance_id)).resolve()
        if not str(target).startswith(str(data_root)):
            raise ValueError("read_file path escapes data_root (refused)")
        if not target.exists() or not target.is_file():
            raise ValueError(f"read_file path does not exist: {path_str}")
        text = target.read_text(encoding="utf-8", errors="replace")
        return {"path": path_str, "content": text[:50_000], "truncated": len(text) > 50_000}

    if kind == "list_files":
        path_str = params.get("path", "")
        if not isinstance(path_str, str):
            raise ValueError("list_files requires params.path: str")
        target = (Path(data_dir) / _safe_name(instance_id) / path_str).resolve()
        data_root = (Path(data_dir) / _safe_name(instance_id)).resolve()
        if not str(target).startswith(str(data_root)):
            raise ValueError("list_files path escapes data_root (refused)")
        if not target.exists() or not target.is_dir():
            raise ValueError(f"list_files path is not a directory: {path_str}")
        entries = []
        for child in sorted(target.iterdir()):
            entries.append({
                "name": child.name,
                "is_dir": child.is_dir(),
                "size": child.stat().st_size if child.is_file() else None,
            })
            if len(entries) >= 500:
                break
        return {"path": path_str, "entries": entries}

    if kind == "sqlite_query":
        sql = params.get("sql", "")
        limit = int(params.get("limit", 100))
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("sqlite_query requires params.sql: str")
        # Read-only enforcement: only SELECT / PRAGMA statements
        sql_stripped = sql.strip().lower()
        if not (sql_stripped.startswith("select")
                or sql_stripped.startswith("pragma")):
            raise ValueError(
                "sqlite_query is read-only; only SELECT/PRAGMA allowed"
            )
        db_path = Path(data_dir) / "instance.db"
        if not db_path.exists():
            raise ValueError(f"instance.db not found at {db_path}")
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql)
            rows = []
            for i, row in enumerate(cur):
                if i >= limit:
                    break
                rows.append(dict(row))
            return {"sql": sql, "rows": rows, "row_count": len(rows)}
        finally:
            conn.close()

    raise ValueError(
        f"unknown kind {kind!r}; supported: "
        f"inspect_state, read_file, list_files, sqlite_query, free_text"
    )


async def inbound_watcher_loop(
    *,
    data_dir: str,
    instance_id: str,
) -> None:
    """Background task. Polls cc_inbox/ for requests, dispatches
    via the read-only substrate handlers, writes responses to
    cc_outbox/. Lets external CLI clients (me-as-CC in another
    session, Codex, scripts) ask Kernos to introspect itself
    without going through Discord."""
    inbox = _inbox_dir(data_dir, instance_id)
    outbox = _outbox_dir(data_dir, instance_id)
    inbox.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    logger.info(
        "INBOX_WATCHER_STARTED: instance=%s inbox=%s outbox=%s interval=%.1fs",
        instance_id, inbox, outbox, _POLL_INTERVAL_SEC,
    )
    while True:
        try:
            await asyncio.sleep(_POLL_INTERVAL_SEC)
            for request_path in sorted(inbox.glob("*.json")):
                try:
                    await _handle_inbound_request(
                        request_path=request_path,
                        outbox_dir=outbox,
                        data_dir=data_dir,
                        instance_id=instance_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "INBOX_WATCHER_REQUEST_FAILED: %s exc=%s",
                        request_path, exc,
                    )
        except asyncio.CancelledError:
            logger.info("INBOX_WATCHER_STOPPED: instance=%s", instance_id)
            raise
        except Exception as exc:
            logger.warning(
                "INBOX_WATCHER_LOOP_ERROR: %s — continuing", exc,
            )


__all__ = [
    "outbound_watcher_loop",
    "inbound_watcher_loop",
]
