"""Durable fire-intent outbox — the dispatch boundary's source of truth.

Codex D7 pin: triggers' state of record for dispatch. Mirrors the
CRB approval→STS posture: claim before emit; dispatch is
resumable; duplicate suppressed.

The shipped ``trigger_fires`` table from
:mod:`kernos.kernel.workflows.trigger_registry` is extended in
place — existing PK preserved, new columns added via ALTER TABLE.
The race-safe atomic claim relies on the existing composite PK
``(trigger_id, idempotency_key)`` (treated semantically as
``(trigger_id, fire_window_key)`` in v1 code).

Status state machine, enforced at the application layer (SQLite
forbids adding CHECK constraints via ALTER):

    pending → dispatched → completed
            \\
             ──→ failed   (from pending or dispatched)

CAS-style transitions: every ``mark_*`` UPDATE includes a WHERE
clause naming the prior status AND the claim_owner. ``rowcount=0``
means another process or a recovery sweep took ownership;
:class:`StaleClaimError` raises so the caller abandons.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import aiosqlite

from kernos.kernel.triggers.errors import (
    FireWindowConflict,
    StaleClaimError,
)
from kernos.kernel.triggers.predicate import derive_fire_id
from kernos.utils import utc_now

logger = logging.getLogger(__name__)


# Status state machine. Keys are the source state; values are the
# permitted destination states. Any transition not in this map
# is rejected at the application layer.
_PERMITTED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"dispatched", "failed"}),
    "dispatched": frozenset({"completed", "failed"}),
    "completed": frozenset(),  # terminal
    "failed": frozenset(),     # terminal
}


_VALID_STATUSES: frozenset[str] = frozenset(_PERMITTED_TRANSITIONS) | frozenset(
    {"completed", "failed"}
)


# ALTER TABLE migration. Each clause is idempotent via try/except
# on "duplicate column name" — same pattern as the WLP / CRB
# migration steps. Running on a fresh-install DB is a no-op since
# the columns are already present from the CREATE TABLE block in
# trigger_registry.
_MIGRATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("instance_id",            "TEXT"),
    ("status",                 "TEXT NOT NULL DEFAULT 'completed'"),
    ("claimed_at",             "TEXT"),
    ("claim_owner",            "TEXT"),
    ("dispatched_at",          "TEXT"),
    ("completed_at",           "TEXT"),
    ("workflow_execution_id",  "TEXT"),
    ("last_error",             "TEXT"),
    ("catch_up",               "INTEGER NOT NULL DEFAULT 0"),
    ("payload_json",           "TEXT NOT NULL DEFAULT '{}'"),
    ("fire_id",                "TEXT"),
)


_MIGRATION_INDEXES: tuple[str, ...] = (
    """CREATE INDEX IF NOT EXISTS idx_trigger_fires_status_pending
       ON trigger_fires (status, claimed_at)
       WHERE status IN ('pending', 'dispatched')""",
    """CREATE INDEX IF NOT EXISTS idx_trigger_fires_instance_status
       ON trigger_fires (instance_id, status)""",
    """CREATE INDEX IF NOT EXISTS idx_trigger_fires_fire_id
       ON trigger_fires (fire_id) WHERE fire_id IS NOT NULL""",
)


# ---------------------------------------------------------------------------
# FireRecord — the row shape used in code
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FireRecord:
    """One row of trigger_fires as the unified runtime sees it.

    ``fire_id`` is the application-layer identity (derived from
    ``(trigger_id, fire_window_key)`` via SHA truncation). The
    SQLite-level identity is the existing composite PK
    ``(trigger_id, idempotency_key)`` where ``idempotency_key`` is
    ``fire_window_key``.
    """

    trigger_id: str
    fire_window_key: str   # = idempotency_key column
    fire_id: str
    instance_id: str
    status: str            # pending | dispatched | completed | failed
    payload: dict[str, Any]
    fired_at: str
    claimed_at: str = ""
    claim_owner: str = ""
    dispatched_at: str = ""
    completed_at: str = ""
    workflow_execution_id: str = ""
    last_error: str = ""
    catch_up: bool = False


# ---------------------------------------------------------------------------
# Migration helper
# ---------------------------------------------------------------------------


async def ensure_outbox_schema(db: aiosqlite.Connection) -> None:
    """Bring an existing ``trigger_fires`` table forward to the v1
    outbox shape. Idempotent — safe to call on fresh installs and
    repeat callers.

    Each ALTER TABLE is wrapped to tolerate "duplicate column name"
    so concurrent startup paths don't race-fail. The CREATE INDEX
    ... IF NOT EXISTS clauses are inherently idempotent.
    """
    # Pull the column set once so we only ALTER for genuinely
    # missing columns.
    async with db.execute(
        "SELECT name FROM pragma_table_info('trigger_fires')"
    ) as cur:
        existing = {row[0] for row in await cur.fetchall()}

    for col_name, col_decl in _MIGRATION_COLUMNS:
        if col_name in existing:
            continue
        try:
            await db.execute(
                f"ALTER TABLE trigger_fires ADD COLUMN {col_name} {col_decl}"
            )
        except aiosqlite.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column name" in msg:
                continue
            raise

    # Backfill instance_id from the triggers table for any rows
    # that pre-date the v1 column. Defensive: only run if both
    # columns + the triggers table look populated.
    try:
        await db.execute(
            "UPDATE trigger_fires "
            "SET instance_id = ("
            "    SELECT t.instance_id FROM triggers t "
            "    WHERE t.trigger_id = trigger_fires.trigger_id"
            ") "
            "WHERE instance_id IS NULL"
        )
    except aiosqlite.OperationalError as exc:
        logger.debug(
            "WTC v1 trigger_fires backfill skipped: %s", exc,
        )

    # Indexes — idempotent CREATE INDEX IF NOT EXISTS.
    for idx_sql in _MIGRATION_INDEXES:
        try:
            await db.execute(idx_sql)
        except aiosqlite.OperationalError as exc:
            logger.warning(
                "WTC v1 trigger_fires index create failed: %s", exc,
            )


# ---------------------------------------------------------------------------
# FireOutbox
# ---------------------------------------------------------------------------


def _validate_status(status: str) -> None:
    if status not in _VALID_STATUSES:
        raise ValueError(
            f"invalid status {status!r}; must be one of "
            f"{sorted(_VALID_STATUSES)}"
        )


def _validate_transition(from_status: str, to_status: str) -> None:
    """Application-layer enforcement of the state machine."""
    _validate_status(from_status)
    _validate_status(to_status)
    permitted = _PERMITTED_TRANSITIONS.get(from_status, frozenset())
    if to_status not in permitted:
        raise StaleClaimError(
            f"transition {from_status!r} → {to_status!r} is not "
            f"permitted (allowed: {sorted(permitted) or 'terminal'})"
        )


class FireOutbox:
    """Mutator + query surface over ``trigger_fires`` in v1 outbox
    shape. One instance per Kernos installation; opens its own
    connection to ``instance.db`` so the outbox lifecycle is
    independent of the legacy ``TriggerRegistry``."""

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None

    async def start(self, data_dir: str) -> None:
        if self._db is not None:
            return
        self._db_path = Path(data_dir) / "instance.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(
            str(self._db_path), isolation_level=None,
        )
        self._db.row_factory = aiosqlite.Row
        # Defensive: ensure base trigger_fires table exists. Most
        # callers will have started TriggerRegistry first which
        # creates the table; running ensure_outbox_schema before
        # the base table exists would raise. We re-create the base
        # table idempotently.
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS trigger_fires ("
            " trigger_id TEXT NOT NULL,"
            " idempotency_key TEXT NOT NULL,"
            " event_id TEXT NOT NULL,"
            " fired_at TEXT NOT NULL,"
            " PRIMARY KEY (trigger_id, idempotency_key)"
            ")"
        )
        await ensure_outbox_schema(self._db)

    async def stop(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # -- claim ----------------------------------------------------------

    async def claim_fire(
        self,
        *,
        instance_id: str,
        trigger_id: str,
        fire_window_key: str,
        payload: Mapping[str, Any],
        claim_owner: str,
        catch_up: bool = False,
    ) -> FireRecord | None:
        """Atomic claim. INSERT ``status='pending'`` with claimed_at
        + claim_owner; conflict on the existing PK
        ``(trigger_id, idempotency_key)`` means another path
        already claimed → return None.

        Race-safety: the composite PK makes the claim atomic at
        SQLite level. No application lock needed.
        """
        if self._db is None:
            raise RuntimeError("FireOutbox not started")
        if not trigger_id or not fire_window_key:
            raise ValueError(
                "claim_fire requires non-empty trigger_id and "
                "fire_window_key"
            )

        import json as _json
        fire_id = derive_fire_id(trigger_id, fire_window_key)
        now = utc_now()
        try:
            await self._db.execute(
                "INSERT INTO trigger_fires ("
                " trigger_id, idempotency_key, event_id, fired_at,"
                " instance_id, status, claimed_at, claim_owner,"
                " payload_json, fire_id, catch_up"
                ") VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                (
                    trigger_id, fire_window_key,
                    payload.get("event_id", "") if isinstance(payload, dict) else "",
                    now, instance_id, now, claim_owner,
                    _json.dumps(dict(payload or {})),
                    fire_id, 1 if catch_up else 0,
                ),
            )
        except aiosqlite.IntegrityError:
            return None

        return FireRecord(
            trigger_id=trigger_id,
            fire_window_key=fire_window_key,
            fire_id=fire_id,
            instance_id=instance_id,
            status="pending",
            payload=dict(payload or {}),
            fired_at=now,
            claimed_at=now,
            claim_owner=claim_owner,
            catch_up=catch_up,
        )

    # -- transitions ----------------------------------------------------

    async def mark_dispatched(
        self,
        *,
        fire_id: str,
        claim_owner: str,
        workflow_execution_id: str,
    ) -> None:
        """Transition pending → dispatched. CAS-style: rowcount=0
        raises :class:`StaleClaimError`."""
        if self._db is None:
            raise RuntimeError("FireOutbox not started")
        cur = await self._db.execute(
            "UPDATE trigger_fires "
            "SET status = 'dispatched', "
            "    dispatched_at = ?, "
            "    workflow_execution_id = ? "
            "WHERE fire_id = ? "
            "  AND status = 'pending' "
            "  AND claim_owner = ?",
            (utc_now(), workflow_execution_id, fire_id, claim_owner),
        )
        if cur.rowcount == 0:
            raise StaleClaimError(
                f"mark_dispatched: no pending row matched "
                f"fire_id={fire_id!r} owner={claim_owner!r}"
            )

    async def mark_completed(
        self,
        *,
        fire_id: str,
        claim_owner: str,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        """Transition dispatched → completed. CAS on
        ``(status='dispatched' AND claim_owner=?)``. Idempotent on
        a duplicate call by the same owner: returns silently if the
        row is already completed by this owner."""
        if self._db is None:
            raise RuntimeError("FireOutbox not started")
        # Idempotent re-entry: detect already-completed rows by
        # this owner and return silently.
        async with self._db.execute(
            "SELECT status, claim_owner FROM trigger_fires "
            "WHERE fire_id = ?",
            (fire_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            current_status = row["status"]
            current_owner = row["claim_owner"]
            if current_status == "completed" and current_owner == claim_owner:
                return

        cur = await self._db.execute(
            "UPDATE trigger_fires "
            "SET status = 'completed', "
            "    completed_at = ? "
            "WHERE fire_id = ? "
            "  AND status = 'dispatched' "
            "  AND claim_owner = ?",
            (utc_now(), fire_id, claim_owner),
        )
        if cur.rowcount == 0:
            raise StaleClaimError(
                f"mark_completed: no dispatched row matched "
                f"fire_id={fire_id!r} owner={claim_owner!r}"
            )

    async def mark_failed(
        self,
        *,
        fire_id: str,
        claim_owner: str,
        error: str,
    ) -> None:
        """Transition pending|dispatched → failed. CAS on
        claim_owner."""
        if self._db is None:
            raise RuntimeError("FireOutbox not started")
        cur = await self._db.execute(
            "UPDATE trigger_fires "
            "SET status = 'failed', "
            "    completed_at = ?, "
            "    last_error = ? "
            "WHERE fire_id = ? "
            "  AND status IN ('pending', 'dispatched') "
            "  AND claim_owner = ?",
            (utc_now(), error, fire_id, claim_owner),
        )
        if cur.rowcount == 0:
            raise StaleClaimError(
                f"mark_failed: no pending/dispatched row matched "
                f"fire_id={fire_id!r} owner={claim_owner!r}"
            )

    async def reconcile_to_dispatched(
        self,
        *,
        fire_id: str,
        workflow_execution_id: str,
    ) -> bool:
        """Recovery-side reconciliation: when WLP already has the
        execution (lookup by fire_id returned a hit) but the outbox
        row is still pending, transition pending → dispatched
        without requiring claim_owner. Returns True iff the row was
        reconciled (was pending; now dispatched). False otherwise.

        Used by the recovery sweep to close the seam Kit identified
        between WLP accept and the runtime's persist.
        """
        if self._db is None:
            raise RuntimeError("FireOutbox not started")
        cur = await self._db.execute(
            "UPDATE trigger_fires "
            "SET status = 'dispatched', "
            "    dispatched_at = ?, "
            "    workflow_execution_id = ? "
            "WHERE fire_id = ? "
            "  AND status = 'pending'",
            (utc_now(), workflow_execution_id, fire_id),
        )
        return cur.rowcount > 0

    # -- recovery -------------------------------------------------------

    async def find_pending_past_lease(
        self, *, claim_lease_seconds: int,
    ) -> list[FireRecord]:
        """Recovery sweep input. Returns rows with
        ``status='pending'`` AND ``claimed_at`` older than
        ``claim_lease_seconds`` ago."""
        if self._db is None:
            return []
        from datetime import datetime, timedelta, timezone
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=claim_lease_seconds)
        ).isoformat()
        async with self._db.execute(
            "SELECT * FROM trigger_fires "
            "WHERE status = 'pending' AND claimed_at < ? "
            "ORDER BY claimed_at",
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def find_dispatched_past_lease(
        self, *, dispatch_lease_seconds: int,
    ) -> list[FireRecord]:
        """Recovery sweep input. Returns rows with
        ``status='dispatched'`` AND ``dispatched_at`` older than
        ``dispatch_lease_seconds``. These need WLP execution-status
        query before transition."""
        if self._db is None:
            return []
        from datetime import datetime, timedelta, timezone
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=dispatch_lease_seconds)
        ).isoformat()
        async with self._db.execute(
            "SELECT * FROM trigger_fires "
            "WHERE status = 'dispatched' AND dispatched_at < ? "
            "ORDER BY dispatched_at",
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def reclaim(
        self, *, fire_id: str, new_claim_owner: str,
    ) -> FireRecord | None:
        """Recovery sweep transitions an orphaned claim to a new
        owner. Conditional UPDATE — returns the record iff reclaim
        succeeded; None if another sweep raced and reclaimed first.

        Reclaim is permitted only on rows whose status is still
        pending or dispatched AND which the existing owner has
        clearly abandoned (caller is expected to call this only
        for rows it pulled from find_pending_past_lease /
        find_dispatched_past_lease).
        """
        if self._db is None:
            return None
        cur = await self._db.execute(
            "UPDATE trigger_fires "
            "SET claim_owner = ?, claimed_at = ? "
            "WHERE fire_id = ? AND status IN ('pending', 'dispatched')",
            (new_claim_owner, utc_now(), fire_id),
        )
        if cur.rowcount == 0:
            return None
        return await self.get_by_fire_id(fire_id)

    # -- queries --------------------------------------------------------

    async def get_by_fire_id(self, fire_id: str) -> FireRecord | None:
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT * FROM trigger_fires WHERE fire_id = ?",
            (fire_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_record(row) if row else None


# ---------------------------------------------------------------------------
# Row → record helpers
# ---------------------------------------------------------------------------


def _row_to_record(row) -> FireRecord:
    import json as _json
    payload_text = row["payload_json"] if "payload_json" in row.keys() else "{}"
    try:
        payload = _json.loads(payload_text or "{}")
    except Exception:
        payload = {}
    return FireRecord(
        trigger_id=row["trigger_id"],
        fire_window_key=row["idempotency_key"],
        fire_id=row["fire_id"] or "",
        instance_id=row["instance_id"] or "",
        status=row["status"] or "completed",
        payload=payload,
        fired_at=row["fired_at"] or "",
        claimed_at=row["claimed_at"] or "",
        claim_owner=row["claim_owner"] or "",
        dispatched_at=row["dispatched_at"] or "",
        completed_at=row["completed_at"] or "",
        workflow_execution_id=row["workflow_execution_id"] or "",
        last_error=row["last_error"] or "",
        catch_up=bool(row["catch_up"]),
    )


__all__ = [
    "FireOutbox",
    "FireRecord",
    "ensure_outbox_schema",
]
