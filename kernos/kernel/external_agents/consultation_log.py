"""Durable audit log for external-agent consultations.

Spec section "D6 — `consultation_log` table" + AC7 + AC16 + AC19.
Captures every consultation attempt (succeeded, failed, timed_out)
with the question, response, context, metadata, workspace_dir,
session ids (both Kernos-owned hex + harness-native ref), and
timing.

Backing storage is ``instance.db`` (the same SQLite file InstanceDB
uses for member-level state). The log opens its own
``aiosqlite`` connection rather than reusing InstanceDB's so the
module has clean lifecycle boundaries — start / stop are separate
from instance setup. Same backing file means cross-table queries
(joining consultation_log with members or model_overrides) stay
trivial when the operator wants them.

Status state machine: ``pending → succeeded | failed | timed_out``.
Pending rows that survive a crash are visible to recovery sweeps;
v1 doesn't auto-resolve them — operator triage via
``find_pending`` is the v1 path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import aiosqlite

logger = logging.getLogger(__name__)


_CONSULTATION_LOG_DDL = """
CREATE TABLE IF NOT EXISTS consultation_log (
    consultation_id      TEXT PRIMARY KEY,
    instance_id          TEXT NOT NULL,
    member_id            TEXT NOT NULL,
    harness              TEXT NOT NULL,
    session_id           TEXT,
    native_session_ref   TEXT,
    question             TEXT NOT NULL,
    response             TEXT NOT NULL DEFAULT '',
    context              TEXT,
    metadata_json        TEXT,
    workspace_dir        TEXT,
    timeout_seconds      INTEGER NOT NULL,
    truncated            INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'pending',
    started_at           TEXT NOT NULL,
    ended_at             TEXT,
    exit_status          INTEGER,
    error                TEXT,
    CHECK (harness IN ('claude_code', 'codex', 'gemini', 'aider')),
    CHECK (status IN ('pending', 'succeeded', 'failed', 'timed_out'))
);
"""

_INDEXES_DDL = [
    """CREATE INDEX IF NOT EXISTS idx_consultation_log_member
       ON consultation_log (instance_id, member_id, started_at)""",
    """CREATE INDEX IF NOT EXISTS idx_consultation_log_session
       ON consultation_log (session_id) WHERE session_id IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS idx_consultation_log_status_pending
       ON consultation_log (status) WHERE status = 'pending'""",
]


ConsultationStatus = Literal["pending", "succeeded", "failed", "timed_out"]


@dataclass(frozen=True)
class ConsultationRecord:
    """One row of consultation_log. ``response`` may be empty when
    status='pending' (the call is still in flight or crashed before
    completion); empty when status='failed' / 'timed_out' if the
    subprocess produced no output."""

    consultation_id: str
    instance_id: str
    member_id: str
    harness: str
    session_id: str
    native_session_ref: str
    question: str
    response: str
    context: str
    metadata_json: str
    workspace_dir: str
    timeout_seconds: int
    truncated: bool
    status: ConsultationStatus
    started_at: str
    ended_at: str
    exit_status: int | None
    error: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_consultation_id() -> str:
    return f"cns_{uuid.uuid4().hex[:16]}"


class ConsultationLog:
    """Mutator + query surface for the consultation_log table."""

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._lock = asyncio.Lock()

    async def start(self, data_dir: str) -> None:
        if self._db is not None:
            return
        self._db_path = Path(data_dir) / "instance.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(
            str(self._db_path), isolation_level=None,
        )
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(_CONSULTATION_LOG_DDL)
        for stmt in _INDEXES_DDL:
            await self._db.execute(stmt)
        await self._db.commit()

    async def stop(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def begin(
        self,
        *,
        instance_id: str,
        member_id: str,
        harness: str,
        session_id: str = "",
        question: str,
        context: str = "",
        metadata: dict[str, Any] | None = None,
        workspace_dir: str = "",
        timeout_seconds: int,
    ) -> str:
        """Persist a fresh consultation row in state ``pending``.
        Returns the consultation_id. Calling this BEFORE invoking
        the harness gives recovery sweeps + triage queries a stable
        record even if the process crashes mid-call."""
        if self._db is None:
            raise RuntimeError("ConsultationLog not started")
        cid = _new_consultation_id()
        await self._db.execute(
            "INSERT INTO consultation_log "
            "(consultation_id, instance_id, member_id, harness, "
            " session_id, question, context, metadata_json, "
            " workspace_dir, timeout_seconds, status, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                cid, instance_id, member_id, harness,
                session_id or None, question, context or None,
                json.dumps(metadata or {}),
                workspace_dir or None, timeout_seconds,
                _now_iso(),
            ),
        )
        return cid

    async def mark_succeeded(
        self,
        *,
        consultation_id: str,
        response: str,
        native_session_ref: str = "",
        truncated: bool = False,
        metadata: dict[str, Any] | None = None,
        exit_status: int = 0,
    ) -> None:
        """Transition pending → succeeded. Idempotent on the same
        consultation_id (UPDATE is unconditional on the row)."""
        if self._db is None:
            return
        await self._db.execute(
            "UPDATE consultation_log "
            "SET status = 'succeeded', "
            "    response = ?, "
            "    native_session_ref = ?, "
            "    truncated = ?, "
            "    metadata_json = COALESCE(?, metadata_json), "
            "    ended_at = ?, "
            "    exit_status = ?, "
            "    error = NULL "
            "WHERE consultation_id = ?",
            (
                response,
                native_session_ref or None,
                1 if truncated else 0,
                json.dumps(metadata) if metadata is not None else None,
                _now_iso(), exit_status,
                consultation_id,
            ),
        )

    async def mark_failed(
        self,
        *,
        consultation_id: str,
        error: str,
        exit_status: int = 0,
        partial_response: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Transition pending → failed. Subprocess exited non-zero
        OR raised; ``error`` text captures the failure detail."""
        if self._db is None:
            return
        await self._db.execute(
            "UPDATE consultation_log "
            "SET status = 'failed', "
            "    response = ?, "
            "    error = ?, "
            "    metadata_json = COALESCE(?, metadata_json), "
            "    ended_at = ?, "
            "    exit_status = ? "
            "WHERE consultation_id = ?",
            (
                partial_response or "",
                error,
                json.dumps(metadata) if metadata is not None else None,
                _now_iso(), exit_status,
                consultation_id,
            ),
        )

    async def mark_timed_out(
        self,
        *,
        consultation_id: str,
        partial_response: str = "",
        timeout_seconds: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Transition pending → timed_out. Distinct from `failed`
        (AC16) so triage can distinguish soft timeouts from
        subprocess errors."""
        if self._db is None:
            return
        error_text = (
            f"timed out after {timeout_seconds}s"
            if timeout_seconds else "timed out"
        )
        await self._db.execute(
            "UPDATE consultation_log "
            "SET status = 'timed_out', "
            "    response = ?, "
            "    error = ?, "
            "    metadata_json = COALESCE(?, metadata_json), "
            "    ended_at = ? "
            "WHERE consultation_id = ?",
            (
                partial_response or "",
                error_text,
                json.dumps(metadata) if metadata is not None else None,
                _now_iso(),
                consultation_id,
            ),
        )

    async def get(self, consultation_id: str) -> ConsultationRecord | None:
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT * FROM consultation_log WHERE consultation_id = ?",
            (consultation_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def find_pending(
        self, *, instance_id: str | None = None,
    ) -> list[ConsultationRecord]:
        """Rows with ``status='pending'``. Used by recovery / triage.
        v1 doesn't auto-resolve pending rows; the operator decides."""
        if self._db is None:
            return []
        if instance_id is None:
            query = (
                "SELECT * FROM consultation_log "
                "WHERE status = 'pending' ORDER BY started_at"
            )
            args: tuple = ()
        else:
            query = (
                "SELECT * FROM consultation_log "
                "WHERE status = 'pending' AND instance_id = ? "
                "ORDER BY started_at"
            )
            args = (instance_id,)
        async with self._db.execute(query, args) as cur:
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def find_by_session(
        self, *, session_id: str,
    ) -> list[ConsultationRecord]:
        if self._db is None:
            return []
        async with self._db.execute(
            "SELECT * FROM consultation_log "
            "WHERE session_id = ? ORDER BY started_at",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]


def _row_to_record(row) -> ConsultationRecord:
    return ConsultationRecord(
        consultation_id=row["consultation_id"],
        instance_id=row["instance_id"],
        member_id=row["member_id"],
        harness=row["harness"],
        session_id=row["session_id"] or "",
        native_session_ref=row["native_session_ref"] or "",
        question=row["question"],
        response=row["response"] or "",
        context=row["context"] or "",
        metadata_json=row["metadata_json"] or "{}",
        workspace_dir=row["workspace_dir"] or "",
        timeout_seconds=row["timeout_seconds"],
        truncated=bool(row["truncated"]),
        status=row["status"],
        started_at=row["started_at"],
        ended_at=row["ended_at"] or "",
        exit_status=row["exit_status"],
        error=row["error"] or "",
    )


__all__ = [
    "ConsultationLog",
    "ConsultationRecord",
    "ConsultationStatus",
]
