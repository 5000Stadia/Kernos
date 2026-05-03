"""Crash-idempotent action log for cohort side effects.

A system cohort's side effects (WDP writes, signal emissions, receipts)
must be replay-safe: if the process crashes between performing a side
effect and committing the cursor that consumed the originating event,
restart will replay the event. Replay must NOT duplicate the side
effect.

The action log records each side effect keyed by

    (cohort_id, instance_id, source_event_id, action_type, target_id)

with NOT NULL ``target_id`` (the design review pin v1→v2). SQLite NULL values in
composite PKs do NOT compare equal under UNIQUE constraint semantics —
two NULL ``target_id`` rows would coexist and defeat dedupe. Every v1
action type produces a deterministic, non-null ``target_id`` (draft_id,
signal_id, or receipt_id derived from the source event + action key),
so the constraint is achievable without sentinel values.

Claim-first protocol (Codex mid-batch fix). The original spec called
for "side effect + log row in same transaction" — but the side effect
runs through a different SQLite connection (WDP / event_stream), so a
single SQL transaction cannot actually wrap both. The implementation
instead uses a three-phase claim protocol:

1. **Claim:** atomically INSERT the log row with ``status='pending'``.
   The composite PRIMARY KEY enforces uniqueness; concurrent claimants
   see :class:`aiosqlite.IntegrityError` and read back the prior row.
2. **Perform:** invoke the caller's ``perform()`` coroutine. No lock
   held during the await, so re-entrancy and concurrent unrelated
   actions are safe.
3. **Mark:** UPDATE the log row to ``status='performed'`` with the
   result summary on success, or ``status='failed'`` on raise.

Recovery semantics on restart:

* ``status='performed'`` → return prior summary (already done).
* ``status='pending'`` → previous process crashed mid-perform. v1
  conservative behavior: treat as already-done and return the
  (possibly empty) summary. This means a rare crash may "lose" a
  signal — a worse failure mode than duplication for a tool-starved
  cohort whose work product is non-essential.
* ``status='failed'`` → previous perform raised; safe to retry. The
  caller observes a fresh perform invocation.

The directory placement (``cohorts/_substrate/action_log.py``) reflects
reusable intent: future Pattern Observer / Curator cohorts can import
this primitive directly without Drafter-specific assumptions.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import aiosqlite

logger = logging.getLogger(__name__)


T = TypeVar("T")


# ---------------------------------------------------------------------------
# Action types
# ---------------------------------------------------------------------------


# Pinned set: adding a new action type is a deliberate substrate change.
# Tests (test_cohort_action_log.py) assert this exact set so a future
# refactor can't quietly extend the surface.
ALLOWED_ACTION_TYPES: frozenset[str] = frozenset({
    "create_draft",
    "update_draft",
    "abandon_draft",
    "emit_signal",
    "emit_receipt",
})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ActionLogError(Exception):
    """Base for action_log errors."""


class ActionLogInvalidActionType(ActionLogError):
    """Raised when ``action_type`` is not in :data:`ALLOWED_ACTION_TYPES`."""


class ActionLogInvalidTarget(ActionLogError):
    """Raised when ``target_id`` is empty or ``None``. The substrate
    requires NOT NULL composite-key components (the design review pin v1→v2)."""


class ActionLogConflict(ActionLogError):
    """Raised when ``record_and_perform`` finds a conflicting prior result
    for the same composite key. Should never occur in production; pin
    guards against logic-bug regression."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_ACTION_LOG_DDL = """
CREATE TABLE IF NOT EXISTS cohort_action_log (
    cohort_id        TEXT NOT NULL,
    instance_id      TEXT NOT NULL,
    source_event_id  TEXT NOT NULL,
    action_type      TEXT NOT NULL,
    target_id        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    result_summary   TEXT,
    performed_at     TEXT NOT NULL,
    PRIMARY KEY (cohort_id, instance_id, source_event_id, action_type, target_id),
    CHECK (status IN ('pending', 'performed', 'failed'))
)
"""

_ACTION_LOG_INDEX = """
CREATE INDEX IF NOT EXISTS idx_cohort_action_log_event
    ON cohort_action_log (cohort_id, instance_id, source_event_id)
"""


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    """Create the action-log table + index. Lazy migration: pre-Drafter
    databases (none expected at the time of this batch, but defensive)
    get the ``status`` column added in place via ALTER TABLE."""
    await db.execute(_ACTION_LOG_DDL)
    await db.execute(_ACTION_LOG_INDEX)
    # Lazy migration for pre-existing tables that lacked the status
    # column. SQLite ALTER TABLE ADD COLUMN cannot include CHECK or
    # NOT NULL with a default in older versions; the application-level
    # validation in record_and_perform enforces the invariant for new
    # rows regardless.
    async with db.execute("PRAGMA table_info(cohort_action_log)") as cur:
        cols = [row[1] for row in await cur.fetchall()]
    if "status" not in cols:
        try:
            await db.execute(
                "ALTER TABLE cohort_action_log ADD COLUMN status TEXT "
                "NOT NULL DEFAULT 'performed'"
            )
        except aiosqlite.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    await db.commit()


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


STATUS_PENDING = "pending"
STATUS_PERFORMED = "performed"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class ActionRecord:
    cohort_id: str
    instance_id: str
    source_event_id: str
    action_type: str
    target_id: str
    status: str
    result_summary: dict
    performed_at: str

    @classmethod
    def from_row(cls, row) -> "ActionRecord":
        try:
            summary = json.loads(row["result_summary"]) if row["result_summary"] else {}
        except Exception:
            summary = {}
        try:
            status = row["status"]
        except (KeyError, IndexError):
            status = STATUS_PERFORMED  # legacy rows lacking status column
        return cls(
            cohort_id=row["cohort_id"],
            instance_id=row["instance_id"],
            source_event_id=row["source_event_id"],
            action_type=row["action_type"],
            target_id=row["target_id"],
            status=status or STATUS_PERFORMED,
            result_summary=summary,
            performed_at=row["performed_at"],
        )


# ---------------------------------------------------------------------------
# ActionLog
# ---------------------------------------------------------------------------


class ActionLog:
    """Crash-idempotent action recording for cohort side effects.

    Construction binds a ``cohort_id``; lookups and inserts are scoped
    to that cohort. The log shares ``instance.db`` with other registries
    via its own ``aiosqlite`` connection (separate from event_stream and
    workflow_registry, per the same connection-isolation pattern those
    use).
    """

    def __init__(self, *, cohort_id: str) -> None:
        if not cohort_id:
            raise ValueError("cohort_id is required")
        self._cohort_id = cohort_id
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._lock = asyncio.Lock()

    @property
    def cohort_id(self) -> str:
        return self._cohort_id

    async def start(self, data_dir: str) -> None:
        if self._db is not None:
            return
        self._db_path = Path(data_dir) / "instance.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(
            str(self._db_path), isolation_level=None,
        )
        self._db.row_factory = aiosqlite.Row
        await _ensure_schema(self._db)

    async def stop(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def is_already_done(
        self,
        *,
        instance_id: str,
        source_event_id: str,
        action_type: str,
        target_id: str,
    ) -> ActionRecord | None:
        """Return the recorded :class:`ActionRecord` if the action has
        already been performed; ``None`` otherwise. Cohorts use this
        as an explicit "did I do this already?" check before a side
        effect; ``record_and_perform`` is the atomic-record helper.
        """
        self._validate_action_type(action_type)
        self._validate_target_id(target_id)
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT * FROM cohort_action_log WHERE "
            "cohort_id = ? AND instance_id = ? AND source_event_id = ? "
            "AND action_type = ? AND target_id = ?",
            (self._cohort_id, instance_id, source_event_id, action_type, target_id),
        ) as cur:
            row = await cur.fetchone()
        return ActionRecord.from_row(row) if row else None

    async def record_and_perform(
        self,
        *,
        instance_id: str,
        source_event_id: str,
        action_type: str,
        target_id: str,
        perform: Callable[[], Awaitable[T]],
        result_to_summary: Callable[[T], dict] | None = None,
    ) -> T:
        """Claim-first crash-idempotent side-effect wrapper.

        Three phases (see module docstring for full semantics):

        1. INSERT log row with ``status='pending'`` (atomic claim via
           composite PK).
        2. ``await perform()`` (no lock held during the await).
        3. UPDATE log row to ``status='performed'`` + result summary,
           or ``status='failed'`` on raise.

        On replay (subsequent call with same composite key):

        * ``performed`` → return prior summary, do NOT re-invoke.
        * ``pending`` → conservative skip (return prior summary).
          Documented v1 limitation: rare crash mid-perform may "lose"
          a signal. Acceptable for tool-starved cohorts.
        * ``failed`` → safe to retry; perform invoked again.

        Args:
            instance_id: scope.
            source_event_id: the event whose processing produced this
                side effect; must be stable across replay so the
                composite key collides.
            action_type: must be in :data:`ALLOWED_ACTION_TYPES`.
            target_id: deterministic identifier for the side effect's
                target. NOT NULL.
            perform: async callable producing the side effect's result.
                Invoked at most once per (composite key) per process
                lifetime.
            result_to_summary: optional callable converting ``perform``'s
                return value to a JSON-serializable summary stored in
                the log row. Default: best-effort dict.

        Returns:
            The result of ``perform`` (or, on replay, the recorded
            ``result_summary``).
        """
        self._validate_action_type(action_type)
        self._validate_target_id(target_id)
        if self._db is None:
            raise RuntimeError("ActionLog not started")

        # Phase 0: replay check.
        prior = await self.is_already_done(
            instance_id=instance_id,
            source_event_id=source_event_id,
            action_type=action_type,
            target_id=target_id,
        )
        if prior is not None:
            if prior.status == STATUS_FAILED:
                # Safe to retry: prior perform raised; no side effect.
                # Drop the row so the claim INSERT below succeeds.
                await self._delete_record(
                    instance_id=instance_id,
                    source_event_id=source_event_id,
                    action_type=action_type,
                    target_id=target_id,
                )
            else:
                # 'performed' or 'pending' — return cached summary.
                return self._replay_return(prior)

        # Phase 1: atomic claim. Race-safe across processes via UNIQUE
        # constraint on the composite PK.
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self._db.execute(
                "INSERT INTO cohort_action_log "
                "(cohort_id, instance_id, source_event_id, action_type, "
                " target_id, status, result_summary, performed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._cohort_id, instance_id, source_event_id,
                    action_type, target_id, STATUS_PENDING,
                    "{}", now,
                ),
            )
        except aiosqlite.IntegrityError:
            # Concurrent claimant won the race; read back and return.
            prior = await self.is_already_done(
                instance_id=instance_id,
                source_event_id=source_event_id,
                action_type=action_type,
                target_id=target_id,
            )
            if prior is None:
                raise ActionLogConflict(
                    f"action_log claim violation but prior record "
                    f"not found: cohort={self._cohort_id!r} "
                    f"instance={instance_id!r} "
                    f"source_event={source_event_id!r} "
                    f"action={action_type!r} target={target_id!r}"
                )
            return self._replay_return(prior)

        # Phase 2: perform. No lock held; re-entrant and concurrent
        # unrelated actions are safe.
        try:
            result = await perform()
        except Exception:
            # Mark failed so a future replay can retry the perform.
            try:
                await self._db.execute(
                    "UPDATE cohort_action_log SET status = ?, "
                    "performed_at = ? "
                    "WHERE cohort_id = ? AND instance_id = ? "
                    "AND source_event_id = ? AND action_type = ? "
                    "AND target_id = ?",
                    (
                        STATUS_FAILED,
                        datetime.now(timezone.utc).isoformat(),
                        self._cohort_id, instance_id, source_event_id,
                        action_type, target_id,
                    ),
                )
            except Exception:
                pass
            raise

        # Phase 3: mark performed with summary.
        summary = (
            result_to_summary(result)
            if result_to_summary is not None
            else self._default_summary(result)
        )
        await self._db.execute(
            "UPDATE cohort_action_log SET status = ?, result_summary = ?, "
            "performed_at = ? "
            "WHERE cohort_id = ? AND instance_id = ? "
            "AND source_event_id = ? AND action_type = ? "
            "AND target_id = ?",
            (
                STATUS_PERFORMED,
                json.dumps(summary),
                datetime.now(timezone.utc).isoformat(),
                self._cohort_id, instance_id, source_event_id,
                action_type, target_id,
            ),
        )
        return result

    async def _delete_record(
        self,
        *,
        instance_id: str,
        source_event_id: str,
        action_type: str,
        target_id: str,
    ) -> None:
        """Internal: remove a failed-status row so a fresh claim can
        succeed. Used by the retry-after-failure path."""
        await self._db.execute(
            "DELETE FROM cohort_action_log WHERE "
            "cohort_id = ? AND instance_id = ? AND source_event_id = ? "
            "AND action_type = ? AND target_id = ?",
            (
                self._cohort_id, instance_id, source_event_id,
                action_type, target_id,
            ),
        )

    @staticmethod
    def _validate_action_type(action_type: str) -> None:
        if action_type not in ALLOWED_ACTION_TYPES:
            raise ActionLogInvalidActionType(
                f"action_type {action_type!r} not in allowed set "
                f"{sorted(ALLOWED_ACTION_TYPES)}"
            )

    @staticmethod
    def _validate_target_id(target_id: str) -> None:
        if not target_id:
            raise ActionLogInvalidTarget(
                "target_id is required and must be non-empty (NOT NULL "
                "composite-key invariant per spec v1→v2)"
            )

    @staticmethod
    def _default_summary(result: Any) -> dict:
        """Fallback for callers that don't supply ``result_to_summary``."""
        if hasattr(result, "__dict__"):
            return {"result_repr": repr(result)}
        return {"result": result if isinstance(result, (str, int, float, bool, type(None))) else repr(result)}

    @staticmethod
    def _replay_return(prior: ActionRecord) -> Any:
        """On replay, we cannot reconstruct the live object that
        ``perform`` originally returned. Return the recorded
        ``result_summary`` so callers can detect replay. Cohorts that
        need the live object on replay must look it up themselves
        (e.g., ``DraftRegistry.get_draft(draft_id=summary['draft_id'])``).
        """
        return prior.result_summary


__all__ = [
    "ActionLog",
    "ActionLogConflict",
    "ActionLogError",
    "ActionLogInvalidActionType",
    "ActionLogInvalidTarget",
    "ActionRecord",
    "ALLOWED_ACTION_TYPES",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_PERFORMED",
]
