"""Durable per-instance event stream — SQLite-backed append-only timeline.

EVENT-STREAM-TO-SQLITE. A single queryable timeline of every meaningful
event across Kernos subsystems: relational dispatch, tool calls, gate
verdicts, compaction, plan step transitions, friction observations.

Design contract:

* :func:`emit` is fire-and-forget — callers append to an in-memory queue
  and return immediately. A background writer task flushes to SQLite
  every 2 seconds or when the queue reaches 100 events (whichever
  first). Ungraceful crash may lose up to 2 seconds of in-flight events.
* Shutdown drain: :func:`stop_writer` flushes the pending queue before
  returning, so a clean Kernos shutdown loses no events.
* Events are append-only. No updates, no deletes outside a future
  retention-eviction path (out of scope for this batch; 90-day default
  documented).
* Multi-tenancy: every query requires ``instance_id`` and returns only
  that instance's events.
* Name note: this module coexists with the older ``kernos.kernel.events``
  (``EventStream`` class, typed event taxonomy). The older layer stays
  untouched; this module is the new unified timeline V2's Cognition
  Kernel will consume.

Not part of this batch: consumers (reflection pass, situation model,
cross-member analytics), retention-eviction, cross-instance federation.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiosqlite

logger = logging.getLogger(__name__)


DEFAULT_FLUSH_INTERVAL_S = 2.0
DEFAULT_FLUSH_THRESHOLD = 100


# ---------------------------------------------------------------------------
# Post-flush hook (WORKFLOW-LOOP-PRIMITIVE C1)
#
# Hooks fire after a successful SQLite batch write + commit, so they
# only see durably-persisted events. emit() is unchanged — its
# fire-and-forget contract is preserved; emit never awaits a hook.
#
# Execution scope: hooks run wherever _flush_once() runs successfully.
# That includes the writer task's periodic flush (the common path),
# the threshold-trigger background task spawned by emit(), the read
# APIs' pre-read flush, and explicit flush_now() / stop_writer drains.
# A slow hook therefore can slow callers that explicitly await a flush
# (reads, drain) — but it cannot affect emit's fast-path latency
# because emit either enqueues-and-returns or schedules the flush as
# a task.
#
# Failure isolation invariant: exceptions raised by any hook are
# caught + logged inside _fire_post_flush_hooks and MUST NOT propagate
# into event_stream's flush path or block other hooks. Durable event
# persistence stays independent of hook code health.
#
# The hook registry is module-level so multiple subsystems (trigger
# registry being the first; reflection-pass / improvement-loop in
# future specs) can attach without coordinating through the writer
# singleton's internal state.
# ---------------------------------------------------------------------------


PostFlushHook = Callable[[list["Event"]], Awaitable[None] | None]
"""(events_just_flushed) → optionally awaitable. Hook receives the
batch of events that were just durably persisted. Return value is
ignored; raising is caught + logged."""


_POST_FLUSH_HOOKS: list[PostFlushHook] = []


def register_post_flush_hook(hook: PostFlushHook) -> None:
    """Register a callback that fires after a successful SQLite flush.

    The callback receives the list of Events that were just durably
    persisted in that flush. Multiple hooks may register; they fire in
    registration order. Each hook is wrapped in a try/except so an
    exception in one does not affect the others or the writer's flush
    path.

    Hooks may be sync or async (`async def` callables awaited;
    plain callables called directly). Async hooks are the recommended
    shape for non-trivial work since the writer task is async.

    Idempotent for the same callable identity — registering the same
    hook twice still results in a single entry.
    """
    if hook in _POST_FLUSH_HOOKS:
        return
    _POST_FLUSH_HOOKS.append(hook)


def unregister_post_flush_hook(hook: PostFlushHook) -> bool:
    """Remove a previously-registered hook. Returns True if removed,
    False if it wasn't registered. Used by tests for clean teardown."""
    if hook not in _POST_FLUSH_HOOKS:
        return False
    _POST_FLUSH_HOOKS.remove(hook)
    return True


def _registered_post_flush_hooks() -> tuple[PostFlushHook, ...]:
    """Snapshot of current hooks. Test-only inspection surface; not
    part of the public API."""
    return tuple(_POST_FLUSH_HOOKS)


async def _fire_post_flush_hooks(batch: list["Event"]) -> None:
    """Invoke each registered hook with the freshly-flushed batch.

    Failure-isolation contract: exceptions are caught + logged with
    enough context to diagnose the offending hook; they do NOT
    propagate. Async hooks are awaited; sync hooks are called.
    """
    if not _POST_FLUSH_HOOKS:
        return
    # Snapshot the registry so a hook that re-registers (or another
    # task that mutates the list during iteration) doesn't disturb
    # this firing pass.
    hooks_snapshot = tuple(_POST_FLUSH_HOOKS)
    for hook in hooks_snapshot:
        try:
            result = hook(batch)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning(
                "EVENT_STREAM_POST_FLUSH_HOOK_FAILED hook=%s error=%s",
                getattr(hook, "__qualname__", repr(hook)),
                exc,
                exc_info=True,
            )

#: Retention window documented in install/architecture docs. Eviction is
#: a separate batch; this constant is currently informational only.
RETENTION_DAYS = 90


# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Event envelope (STS C0)
#
# The envelope is a substrate-set bundle of authority metadata that lives
# alongside every event but is NEVER constructable from caller-supplied
# payload. Three fields:
#
#   - source_module: identity of the emitter, set by the substrate from
#     the registered EmitterRegistry entry. Caller cannot override.
#   - emitted_at:    timestamp set by the substrate at emit time.
#   - event_id:      uuid set by the substrate at emit time.
#
# Downstream consumers (notably STS's approval-binding gate) read source
# authority from `event.envelope.source_module`, NEVER from the payload.
# This is the trust boundary that makes approval source authority
# structurally enforceable.
#
# Legacy callers using the module-level :func:`emit` continue to work;
# their events get a default envelope with source_module="unregistered",
# which downstream gates can treat as a fail-closed signal.
# ---------------------------------------------------------------------------


UNREGISTERED_SOURCE_MODULE = "unregistered"
"""Sentinel envelope source for events emitted via the legacy module-level
:func:`emit` path (no registered emitter). Source-authority gates MUST
treat this as fail-closed."""


@dataclass(frozen=True)
class EventEnvelope:
    """Substrate-set authority metadata bundled with every event.

    Distinct from caller-supplied payload. ``source_module`` is set by
    the substrate from the registered emitter's identity at emit time.
    Caller cannot construct an envelope with an arbitrary ``source_module``
    — registration through :class:`EmitterRegistry` is the only path."""

    source_module: str
    emitted_at: str
    event_id: str


@dataclass
class Event:
    """A single event on the stream.

    The fields ``event_id``, ``timestamp``, and ``source_module`` are
    substrate-set; together they form :attr:`envelope`. ``payload`` is
    caller-supplied. Source-authority gates MUST read ``envelope.source_module``,
    never ``payload.source_module``."""

    event_id: str
    instance_id: str
    timestamp: str
    event_type: str
    payload: dict[str, Any]
    member_id: str | None = None
    space_id: str | None = None
    correlation_id: str | None = None
    source_module: str = UNREGISTERED_SOURCE_MODULE

    @property
    def envelope(self) -> EventEnvelope:
        return EventEnvelope(
            source_module=self.source_module,
            emitted_at=self.timestamp,
            event_id=self.event_id,
        )

    def to_row(self) -> tuple:
        return (
            self.event_id, self.instance_id, self.member_id, self.space_id,
            self.timestamp, self.event_type, json.dumps(self.payload),
            self.correlation_id, self.source_module,
        )

    @classmethod
    def from_row(cls, row) -> "Event":
        """Rehydrate from a DB row. Accepts aiosqlite.Row or a plain tuple."""
        try:
            payload_raw = row["payload"]
        except (KeyError, IndexError, TypeError):
            payload_raw = row[6]
        try:
            payload = json.loads(payload_raw) if payload_raw else {}
        except Exception:
            payload = {}
        # Row-access: aiosqlite.Row supports both __getitem__ by name and
        # positional index. Use names for clarity.
        try:
            source_module = row["source_module"]
        except (KeyError, IndexError, TypeError):
            try:
                source_module = row[8]
            except (IndexError, TypeError):
                source_module = None
        if source_module is None:
            source_module = UNREGISTERED_SOURCE_MODULE
        try:
            return cls(
                event_id=row["event_id"],
                instance_id=row["instance_id"],
                member_id=row["member_id"],
                space_id=row["space_id"],
                timestamp=row["timestamp"],
                event_type=row["event_type"],
                payload=payload,
                correlation_id=row["correlation_id"],
                source_module=source_module,
            )
        except Exception:
            # Positional fallback
            return cls(
                event_id=row[0], instance_id=row[1], member_id=row[2],
                space_id=row[3], timestamp=row[4], event_type=row[5],
                payload=payload, correlation_id=row[7],
                source_module=source_module,
            )


# ---------------------------------------------------------------------------
# Writer singleton — module-level for the fire-and-forget shape
# ---------------------------------------------------------------------------


def _is_event_data_error(exc: Exception) -> bool:
    """Errors tied to one row, rather than database availability/health."""
    if isinstance(
        exc, (sqlite3.IntegrityError, sqlite3.DataError, OverflowError, UnicodeError),
    ):
        return True
    # ProgrammingError also covers a closed connection: only binding errors
    # identify malformed event data that retrying cannot repair.
    return (
        isinstance(exc, sqlite3.ProgrammingError)
        and str(exc).startswith("Error binding parameter ")
    )


class _EventWriter:
    """Background writer — owns the queue, the flusher task, the DB conn."""

    def __init__(self) -> None:
        self._queue: list[Event] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._flush_interval_s = DEFAULT_FLUSH_INTERVAL_S
        self._flush_threshold = DEFAULT_FLUSH_THRESHOLD

    async def start(
        self,
        data_dir: str,
        *,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        flush_threshold: int = DEFAULT_FLUSH_THRESHOLD,
    ) -> None:
        """Open the DB, ensure schema, start the flusher task."""
        if self._task is not None:
            return  # already started — idempotent
        self._flush_interval_s = flush_interval_s
        self._flush_threshold = flush_threshold
        self._db_path = Path(data_dir) / "instance.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await _ensure_schema(self._db)
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="event_stream_writer")

    async def stop(self) -> None:
        """Signal stop, drain the queue, close the connection."""
        if self._task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("EVENT_STREAM_STOP_TIMEOUT: forcing cancel")
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        # Final drain of anything the run loop didn't pick up
        await self._flush_once()
        if self._db is not None:
            await self._db.close()
            self._db = None

    def enqueue(self, event: Event) -> None:
        """Synchronous enqueue — the API surface of the fire-and-forget contract."""
        # Bounded queue: reject beyond a very generous ceiling rather than
        # grow unbounded if the writer is stalled or not started.
        if len(self._queue) >= 10_000:
            logger.warning("EVENT_STREAM_QUEUE_OVERFLOW: dropping event %s", event.event_type)
            return
        self._queue.append(event)

    async def _run(self) -> None:
        """Flush loop — every flush_interval or on threshold."""
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._flush_interval_s,
                )
            except asyncio.TimeoutError:
                pass
            # Either stop was set or interval elapsed — flush
            try:
                await self._flush_once()
            except Exception as exc:
                logger.warning("EVENT_STREAM_FLUSH_FAILED: %s", exc)

    async def _flush_once(self) -> None:
        """Commit valid events; reject bad rows and retry database failures."""
        if self._db is None:
            return
        if not self._queue:
            return
        async with self._lock:
            # Aiosqlite serializes statements, not transactions. Keep the
            # snapshot, writes, commit and any rollback under one flush lock.
            batch = list(self._queue)
            self._queue.clear()
            if not batch:
                return
            try:
                prepared = []
                for event in batch:
                    try:
                        prepared.append((event, event.to_row()))
                    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
                        logger.warning(
                            "EVENT_STREAM_EVENT_REJECTED event_id=%s event_type=%s error=%s",
                            event.event_id, event.event_type, exc,
                        )
                batch = [event for event, _ in prepared]
                if not batch:
                    return
                insert_sql = (
                    "INSERT INTO events "
                    "(event_id, instance_id, member_id, space_id, timestamp, "
                    " event_type, payload, correlation_id, source_module) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                )
                try:
                    await self._db.executemany(insert_sql, [row for _, row in prepared])
                except Exception as exc:
                    if not _is_event_data_error(exc):
                        raise
                    # Executemany may have inserted a valid prefix. Undo it
                    # before isolating individual bad rows in a fresh batch.
                    await self._db.rollback()
                    accepted = []
                    for index, (event, row) in enumerate(prepared):
                        try:
                            async with self._db.execute(insert_sql, row):
                                pass
                        except Exception as row_exc:
                            if not _is_event_data_error(row_exc):
                                # Retain accepted and not-yet-tried events if
                                # a database failure interrupts row isolation.
                                batch = accepted + [e for e, _ in prepared[index:]]
                                raise
                            logger.warning(
                                "EVENT_STREAM_EVENT_REJECTED event_id=%s event_type=%s error=%s",
                                event.event_id, event.event_type, row_exc,
                            )
                            continue
                        accepted.append(event)
                    batch = accepted
                await self._db.commit()
            except Exception as exc:
                logger.warning(
                    "EVENT_STREAM_WRITE_FAILED: batch=%d error=%s", len(batch), exc,
                )
                try:
                    await self._db.rollback()
                except Exception as rollback_exc:
                    logger.warning("EVENT_STREAM_ROLLBACK_FAILED: %s", rollback_exc)
                # Enqueues can arrive while SQLite is awaited. Retry older
                # valid events before them, without replaying rejected rows.
                self._queue = batch + self._queue
                return
        # Successful durable persist — fire post-flush hooks. Hook
        # exceptions are caught inside _fire_post_flush_hooks so they
        # cannot disturb the writer task's flush path.
        if batch:
            await _fire_post_flush_hooks(batch)

    async def read_db(self) -> aiosqlite.Connection | None:
        """Return the live DB connection for queries. Used by read functions."""
        return self._db

    @property
    def queue_depth(self) -> int:
        return len(self._queue)


_WRITER = _EventWriter()


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    """Create the events table + indices if absent.

    Lazy migration: pre-STS instance.db files have no ``source_module``
    column. Detect via PRAGMA and add it if missing. Existing rows get
    NULL, which :meth:`Event.from_row` materialises as
    :data:`UNREGISTERED_SOURCE_MODULE`."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id        TEXT PRIMARY KEY,
            instance_id     TEXT NOT NULL,
            member_id       TEXT,
            space_id        TEXT,
            timestamp       TEXT NOT NULL,
            event_type      TEXT NOT NULL,
            payload         TEXT NOT NULL,
            correlation_id  TEXT,
            source_module   TEXT
        )
        """
    )
    # Lazy migration for existing databases.
    #
    # Race safety: under WAL with multiple connections, two startup
    # paths could both observe the missing column via PRAGMA. The
    # first ALTER wins; the second raises ``OperationalError: duplicate
    # column``. Catch that specific error and treat as success — the
    # net effect is the column exists, which is what we wanted.
    async with db.execute("PRAGMA table_info(events)") as cur:
        cols = [row[1] for row in await cur.fetchall()]
    if "source_module" not in cols:
        try:
            await db.execute(
                "ALTER TABLE events ADD COLUMN source_module TEXT"
            )
        except aiosqlite.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_instance_ts "
        "ON events(instance_id, timestamp)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_instance_member_ts "
        "ON events(instance_id, member_id, timestamp)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_instance_type_ts "
        "ON events(instance_id, event_type, timestamp)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_correlation "
        "ON events(correlation_id)"
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def start_writer(
    data_dir: str,
    *,
    flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
    flush_threshold: int = DEFAULT_FLUSH_THRESHOLD,
) -> None:
    """Start the background writer. Idempotent."""
    await _WRITER.start(
        data_dir,
        flush_interval_s=flush_interval_s,
        flush_threshold=flush_threshold,
    )


async def stop_writer() -> None:
    """Drain the queue and close the DB. Safe to call without start."""
    await _WRITER.stop()


async def flush_now() -> None:
    """Force a flush — used by tests and explicit checkpoint callers."""
    await _WRITER._flush_once()


def queue_depth() -> int:
    """Current in-memory queue depth — for tests and diagnostics."""
    return _WRITER.queue_depth


async def emit(
    instance_id: str,
    event_type: str,
    payload: dict | None = None,
    *,
    member_id: str | None = None,
    space_id: str | None = None,
    correlation_id: str | None = None,
) -> str:
    """Enqueue an event for write. Returns the substrate-set ``event_id``
    immediately (write is batched).

    This is the canonical emission entry point for legacy / unregistered
    callers. Events emitted through this path get
    ``envelope.source_module == UNREGISTERED_SOURCE_MODULE``; downstream
    source-authority gates treat that as fail-closed. Production code that
    needs to claim authority (e.g. CRB emitting ``routine.proposed`` /
    ``routine.approved``) MUST go through :class:`EmitterRegistry`.

    Over-threshold enqueue triggers an opportunistic background flush
    without blocking the caller.

    Returns the substrate-generated ``event_id`` so callers (e.g. CRB
    using the approval_event_id for STS register_workflow) have eager
    visibility into the durable identifier without a round-trip read.
    """
    return _enqueue_with_envelope(
        instance_id=instance_id,
        event_type=event_type,
        payload=payload,
        member_id=member_id,
        space_id=space_id,
        correlation_id=correlation_id,
        source_module=UNREGISTERED_SOURCE_MODULE,
    )


def _enqueue_with_envelope(
    *,
    instance_id: str,
    event_type: str,
    payload: dict | None,
    member_id: str | None,
    space_id: str | None,
    correlation_id: str | None,
    source_module: str,
) -> str:
    """Substrate-internal enqueue that sets the envelope's ``source_module``
    from a substrate-controlled identity. Callers do not pass ``source_module``
    directly; only :class:`EventEmitter` (registered via :class:`EmitterRegistry`)
    and the legacy :func:`emit` shim invoke this.

    Returns the substrate-generated ``event_id`` so callers can correlate
    the emission with a durable identifier (CRB: approval_event_id for
    STS register_workflow).
    """
    event = Event(
        event_id=str(uuid.uuid4()),
        instance_id=instance_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        event_type=event_type,
        payload=payload or {},
        member_id=member_id,
        space_id=space_id,
        correlation_id=correlation_id,
        source_module=source_module,
    )
    _WRITER.enqueue(event)
    if _WRITER.queue_depth >= _WRITER._flush_threshold:
        asyncio.create_task(_WRITER._flush_once())
    return event.event_id


# ---------------------------------------------------------------------------
# EmitterRegistry (STS C0)
#
# Each subsystem that needs to claim source authority on its events
# registers an :class:`EventEmitter` once at engine bring-up. The registry
# enforces source_module uniqueness — only one emitter may claim
# ``source_module="crb"`` for a given lifecycle. Modules emit through
# their registered emitter; the emitter is the only path that can set
# ``envelope.source_module`` to a registered identity.
#
# The legacy module-level :func:`emit` writes events with
# ``envelope.source_module == UNREGISTERED_SOURCE_MODULE`` regardless of
# payload contents, so a caller cannot smuggle ``source_module="crb"``
# in via the payload.
# ---------------------------------------------------------------------------


class EmitterAlreadyRegistered(Exception):
    """Raised when a second :class:`EventEmitter` registration attempts to
    claim a ``source_module`` already held by another emitter."""


class EmitterRegistry:
    """Substrate-side registry mapping ``source_module`` -> :class:`EventEmitter`.

    Uniqueness is enforced at registration time. ``register`` raises
    :class:`EmitterAlreadyRegistered` if the source_module is taken.

    The registry is a process-level singleton; a fresh process starts
    empty, and :func:`_reset_for_tests` clears it for test isolation."""

    def __init__(self) -> None:
        self._emitters: dict[str, "EventEmitter"] = {}
        # Threading lock — defends the check-then-register sequence
        # against a race if engine bring-up ever fans out across
        # threads. Single-loop callers see no contention.
        self._lock = threading.Lock()

    def register(self, source_module: str) -> "EventEmitter":
        """Register an emitter for ``source_module`` and return it.

        Raises:
            ValueError: if ``source_module`` is empty or equal to
                :data:`UNREGISTERED_SOURCE_MODULE` (reserved sentinel).
            EmitterAlreadyRegistered: if a different emitter has already
                registered with this ``source_module``.
        """
        if not source_module:
            raise ValueError("source_module is required")
        if source_module == UNREGISTERED_SOURCE_MODULE:
            raise ValueError(
                f"source_module={source_module!r} is reserved as the "
                "unregistered sentinel and cannot be registered"
            )
        with self._lock:
            if source_module in self._emitters:
                raise EmitterAlreadyRegistered(
                    f"source_module={source_module!r} is already registered"
                )
            emitter = EventEmitter(
                source_module=source_module,
                _registry_token=_EMITTER_REGISTRY_TOKEN,
            )
            self._emitters[source_module] = emitter
            return emitter

    def get(self, source_module: str) -> "EventEmitter | None":
        return self._emitters.get(source_module)

    def is_registered(self, source_module: str) -> bool:
        return source_module in self._emitters

    def _clear(self) -> None:
        """Test-only reset hook."""
        with self._lock:
            self._emitters.clear()


# Opaque token used to gate :class:`EventEmitter` construction. Only
# :class:`EmitterRegistry` holds a reference to this object; direct
# callers that try ``EventEmitter(source_module="crb")`` raise
# :class:`RuntimeError`. This is defense-in-depth on top of the
# bypass-grep test (C3): a runtime fail-loud check for the in-process
# spoof vector.
_EMITTER_REGISTRY_TOKEN = object()


_EMITTER_REGISTRY = EmitterRegistry()


def emitter_registry() -> EmitterRegistry:
    """Return the process-level :class:`EmitterRegistry` singleton."""
    return _EMITTER_REGISTRY


class EventEmitter:
    """A bound emitter that always stamps events with its registered
    ``source_module``. Constructed ONLY by :class:`EmitterRegistry`.

    Direct construction (``EventEmitter(source_module="crb")``) raises
    :class:`RuntimeError` — the constructor checks for a registry-issued
    token. This closes the in-process spoof vector where any caller
    could otherwise instantiate an emitter claiming arbitrary
    ``source_module`` authority.

    Caller-supplied payloads are ignored for source authority; the
    envelope's ``source_module`` is set from this emitter's frozen
    identity, never from payload contents."""

    def __init__(self, *, source_module: str, _registry_token: object = None) -> None:
        if _registry_token is not _EMITTER_REGISTRY_TOKEN:
            raise RuntimeError(
                "EventEmitter cannot be constructed directly; use "
                "EmitterRegistry.register(source_module) to obtain an emitter"
            )
        self._source_module = source_module

    @property
    def source_module(self) -> str:
        return self._source_module

    async def emit(
        self,
        instance_id: str,
        event_type: str,
        payload: dict | None = None,
        *,
        member_id: str | None = None,
        space_id: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        """Enqueue an event with ``envelope.source_module`` set from this
        emitter's registered identity. Payload contents do NOT influence
        the envelope's source authority.

        Returns the substrate-generated ``event_id`` for caller correlation."""
        return _enqueue_with_envelope(
            instance_id=instance_id,
            event_type=event_type,
            payload=payload,
            member_id=member_id,
            space_id=space_id,
            correlation_id=correlation_id,
            source_module=self._source_module,
        )


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


async def events_for_member(
    instance_id: str,
    member_id: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    event_types: list[str] | None = None,
    limit: int = 1000,
) -> list[Event]:
    """Events for a single member in ascending timestamp order."""
    db = await _WRITER.read_db()
    if db is None:
        return []
    # Ensure pending writes land before we read.
    await _WRITER._flush_once()
    clauses = ["instance_id = ?", "member_id = ?"]
    args: list[Any] = [instance_id, member_id]
    if since:
        clauses.append("timestamp >= ?")
        args.append(since.isoformat())
    if until:
        clauses.append("timestamp <= ?")
        args.append(until.isoformat())
    if event_types:
        placeholders = ",".join("?" * len(event_types))
        clauses.append(f"event_type IN ({placeholders})")
        args.extend(event_types)
    args.append(limit)
    query = (
        "SELECT * FROM events WHERE "
        + " AND ".join(clauses)
        + " ORDER BY timestamp ASC LIMIT ?"
    )
    async with db.execute(query, args) as cur:
        rows = await cur.fetchall()
    return [Event.from_row(r) for r in rows]


async def events_in_window(
    instance_id: str,
    since: datetime,
    until: datetime,
    *,
    event_types: list[str] | tuple[str, ...] | frozenset[str] | None = None,
    after_event_id: str | None = None,
    limit: int = 1000,
) -> list[Event]:
    """All events for an instance in a time window, ascending.

    Optional ``event_types`` filter is pushed into the SQL query so
    callers (e.g. cohort cursors) don't read raw events and Python-
    filter — that pattern starves when the next N raw events are all
    non-matching.

    Optional ``after_event_id`` breaks same-timestamp ties: when the
    caller has already consumed the event whose timestamp equals
    ``since``, passing its ``event_id`` excludes it from the result
    and avoids the strict ``>`` ordering skipping legitimate later
    events at the same timestamp.
    """
    db = await _WRITER.read_db()
    if db is None:
        return []
    await _WRITER._flush_once()
    clauses = ["instance_id = ?", "timestamp >= ?", "timestamp <= ?"]
    args: list[Any] = [instance_id, since.isoformat(), until.isoformat()]
    if event_types:
        types_list = list(event_types)
        placeholders = ",".join("?" * len(types_list))
        clauses.append(f"event_type IN ({placeholders})")
        args.extend(types_list)
    if after_event_id:
        # Exclude the named event so same-timestamp ties don't replay.
        clauses.append("event_id != ?")
        args.append(after_event_id)
    args.append(limit)
    query = (
        "SELECT * FROM events WHERE "
        + " AND ".join(clauses)
        + " ORDER BY timestamp ASC, event_id ASC LIMIT ?"
    )
    async with db.execute(query, args) as cur:
        rows = await cur.fetchall()
    return [Event.from_row(r) for r in rows]


async def events_by_correlation(
    instance_id: str,
    correlation_id: str,
) -> list[Event]:
    """All events for a given correlation id within an instance, ascending."""
    db = await _WRITER.read_db()
    if db is None:
        return []
    await _WRITER._flush_once()
    async with db.execute(
        "SELECT * FROM events WHERE instance_id = ? AND correlation_id = ? "
        "ORDER BY timestamp ASC",
        (instance_id, correlation_id),
    ) as cur:
        rows = await cur.fetchall()
    return [Event.from_row(r) for r in rows]


async def event_by_id(
    instance_id: str,
    event_id: str,
) -> Event | None:
    """Look up a single event by its event_id, scoped to an instance.

    Returns None if no event matches. The instance scope is part of the
    lookup so cross-instance approval events cannot be referenced from
    another instance — STS's approval validation depends on this.
    """
    db = await _WRITER.read_db()
    if db is None:
        return None
    await _WRITER._flush_once()
    async with db.execute(
        "SELECT * FROM events WHERE instance_id = ? AND event_id = ?",
        (instance_id, event_id),
    ) as cur:
        row = await cur.fetchone()
    return Event.from_row(row) if row is not None else None


# ---------------------------------------------------------------------------
# Test helpers — explicit reset for isolation in the test suite
# ---------------------------------------------------------------------------


async def _reset_for_tests() -> None:
    """Tear down the writer singleton so the next test can start clean."""
    await stop_writer()
    # Rebind — previous instance kept its state dicts, which is fine for
    # idempotent restart, but tests want a perfectly clean slate.
    global _WRITER
    _WRITER = _EventWriter()
    # Clear post-flush hook registrations too — a hook left over from
    # an earlier test would otherwise fire on subsequent test flushes
    # and silently mutate state across tests.
    _POST_FLUSH_HOOKS.clear()
    # Clear the EmitterRegistry — a registration left over from an
    # earlier test would otherwise collide with re-registration.
    _EMITTER_REGISTRY._clear()


__all__ = [
    "EmitterAlreadyRegistered",
    "EmitterRegistry",
    "Event",
    "EventEmitter",
    "EventEnvelope",
    "PostFlushHook",
    "RETENTION_DAYS",
    "UNREGISTERED_SOURCE_MODULE",
    "emit",
    "emitter_registry",
    "event_by_id",
    "events_by_correlation",
    "events_for_member",
    "events_in_window",
    "flush_now",
    "queue_depth",
    "register_post_flush_hook",
    "start_writer",
    "stop_writer",
    "unregister_post_flush_hook",
]
