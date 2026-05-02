"""Trigger registry — workflows fire when matching events flush.

Subscribes to ``event_stream``'s post-flush hook (shipped in
WORKFLOW-LOOP-PRIMITIVE C1). The registry attaches one hook callable;
on each successful SQLite flush it iterates the durable batch, looks
up active triggers per event, evaluates each trigger's structured
predicate AST, applies idempotency suppression, and calls registered
match-listeners with ``(trigger, event)``.

The registry creates **NO parallel event substrate** — the only
event surface is the shipped event_stream. Multi-tenancy is keyed
to ``instance_id`` from day one; cross-instance triggers cannot fire.

Failure isolation: hook execution itself is wrapped by
``event_stream._fire_post_flush_hooks`` (per-hook try/except). Within
this hook, each trigger evaluation + listener dispatch is also
wrapped in a try/except so one bad trigger or listener cannot
suppress matches against the rest of the batch.

Persistence: triggers live in the ``triggers`` SQLite table alongside
the shipped ``events`` table. Idempotency fires are recorded in
``trigger_fires`` with a UNIQUE constraint that makes duplicate-fire
suppression atomic.

Restart-resume: ``start()`` reloads all active triggers from SQLite
into the in-memory cache before attaching the post-flush hook, so a
restart with no other state change preserves matching behaviour.

Concurrency assumption: this registry runs on a single asyncio event
loop. Cache reads (``_candidates_for_event``) are unlocked because
in-loop dict reads cannot interleave with in-loop dict writes;
mutations take ``_cache_lock`` purely to serialise interleavings of
multiple await-points within mutation paths. Cross-thread access is
not supported.

``update_status`` transition window: when a trigger flips between
``active`` and ``paused``/``retired`` there is a brief window
between the DB commit and the cache swap during which a concurrent
post-flush dispatch can observe the pre-transition state. This is
acceptable for the paused/retired transitions — at most one extra
fire — and the design favours simplicity over making transitions
fully synchronous.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import string
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiosqlite

from kernos.kernel import event_stream
from kernos.kernel.event_stream import Event
from kernos.kernel.workflows.predicates import (
    PredicateError,
    evaluate as evaluate_predicate,
    validate as validate_predicate,
)

logger = logging.getLogger(__name__)


MatchListener = Callable[["Trigger", Event], Awaitable[None] | None]
"""(trigger, event) → optionally awaitable. Listeners are notified
when a trigger's predicate matches a durable event. Listeners are
where C5's execution engine plugs in to enqueue workflow runs."""


_WILDCARD_EVENT_TYPE = "*"


# ---------------------------------------------------------------------------
# Idempotency template formatter
#
# `str.format` accepts both ``[key]`` index access and ``.attr`` attribute
# access. Index access against a payload dict is the intended template
# surface; attribute access is not — and `{payload.__class__}` style
# templates would otherwise leak Python internals into rendered keys.
# This formatter rejects attribute access at render time; the renderer
# treats the resulting ValueError the same as a missing field (key
# renders to None and the trigger does not fire).
# ---------------------------------------------------------------------------


class _IdempotencyKeyFormatter(string.Formatter):
    """Subset formatter: bare-name + `[key]` lookup only. Attribute
    access (`name.attr`) raises ValueError."""

    def get_field(self, field_name, args, kwargs):
        first, rest = string._string.formatter_field_name_split(field_name)  # type: ignore[attr-defined]
        obj = self.get_value(first, args, kwargs)
        for is_attr, key in rest:
            if is_attr:
                raise ValueError(
                    f"attribute access not allowed in idempotency template: "
                    f"{field_name!r}"
                )
            obj = obj[key]
        return obj, first


_IDEMPOTENCY_FORMATTER = _IdempotencyKeyFormatter()


# ---------------------------------------------------------------------------
# Trigger dataclass
# ---------------------------------------------------------------------------


@dataclass
class Trigger:
    """A single trigger record.

    ``event_type`` is the exact dotted event type the trigger is
    interested in (or ``"*"`` for any). The predicate AST is the
    second-stage filter applied after the event_type prefilter.

    ``actor_filter`` and ``correlation_filter`` are convenience
    prefilters expressed at the dataclass level; they could equally
    be encoded inside the predicate, but pulling them out here lets
    the registry skip predicate evaluation entirely on misses.

    ``idempotency_key_template`` is a Python-format string evaluated
    against the event (e.g. ``"{payload[task_id]}"``); when set, the
    rendered key is recorded in ``trigger_fires`` and a duplicate
    rendering for the same trigger is suppressed.
    """

    trigger_id: str
    workflow_id: str
    instance_id: str
    event_type: str
    predicate: dict
    predicate_source: str = ""
    description: str = ""
    actor_filter: str | None = None
    correlation_filter: str | None = None
    idempotency_key_template: str | None = None
    owner: str = ""
    version: int = 1
    status: str = "active"
    created_at: str = ""

    def to_row(self) -> tuple:
        return (
            self.trigger_id,
            self.workflow_id,
            self.instance_id,
            self.event_type,
            json.dumps(self.predicate),
            self.predicate_source,
            self.description,
            self.actor_filter or "",
            self.correlation_filter or "",
            self.idempotency_key_template or "",
            self.owner,
            self.version,
            self.status,
            self.created_at,
        )

    @classmethod
    def from_row(cls, row) -> "Trigger":
        try:
            predicate = json.loads(row["predicate"]) if row["predicate"] else {}
        except Exception:
            predicate = {}
        return cls(
            trigger_id=row["trigger_id"],
            workflow_id=row["workflow_id"],
            instance_id=row["instance_id"],
            event_type=row["event_type"],
            predicate=predicate,
            predicate_source=row["predicate_source"] or "",
            description=row["description"] or "",
            actor_filter=row["actor_filter"] or None,
            correlation_filter=row["correlation_filter"] or None,
            idempotency_key_template=row["idempotency_key_template"] or None,
            owner=row["owner"] or "",
            version=row["version"],
            status=row["status"],
            created_at=row["created_at"],
        )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_TRIGGERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS triggers (
    trigger_id              TEXT PRIMARY KEY,
    workflow_id             TEXT NOT NULL,
    instance_id             TEXT NOT NULL,
    event_type              TEXT NOT NULL,
    predicate               TEXT NOT NULL,
    predicate_source        TEXT DEFAULT '',
    description             TEXT DEFAULT '',
    actor_filter            TEXT DEFAULT '',
    correlation_filter      TEXT DEFAULT '',
    idempotency_key_template TEXT DEFAULT '',
    owner                   TEXT DEFAULT '',
    version                 INTEGER DEFAULT 1,
    status                  TEXT NOT NULL DEFAULT 'active',
    created_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_triggers_active
    ON triggers(instance_id, status, event_type);

CREATE TABLE IF NOT EXISTS trigger_fires (
    trigger_id      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    fired_at        TEXT NOT NULL,
    PRIMARY KEY (trigger_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_trigger_fires_lookup
    ON trigger_fires(trigger_id, idempotency_key);
"""


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    for stmt in _TRIGGERS_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            await db.execute(stmt)
    await db.commit()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TriggerRegistry:
    """Owns the triggers table, the in-memory cache, and the post-flush
    hook subscription.

    Lifecycle:
      ``start(data_dir)`` → open DB, ensure schema, reload active
      triggers, attach post-flush hook.
      ``stop()`` → detach hook, close DB. Idempotent.

    The registry is constructed without arguments; ``start()`` opens
    the SQLite connection. This mirrors the shipped ``_EventWriter``
    pattern.
    """

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._cache: dict[str, dict[str, list[Trigger]]] = {}
        # cache shape: {instance_id: {event_type: [Trigger, ...]}}
        self._cache_lock = asyncio.Lock()
        self._listeners: list[MatchListener] = []
        self._hook_attached: bool = False
        self._hook_callable: Callable[[list[Event]], Awaitable[None]] | None = None

    # -- lifecycle ------------------------------------------------------

    async def start(
        self,
        data_dir: str,
        *,
        attach_post_flush_hook: bool = True,
    ) -> None:
        """Start the registry and (optionally) attach the legacy
        post-flush hook for event-driven trigger evaluation.

        ``attach_post_flush_hook`` defaults to True for backward
        compatibility with the broad set of tests that assert the
        hook is registered after start(). Production bring-up
        (kernos/setup/bring_up_substrate.py, WTC v1 C5c-bringup)
        passes ``False`` because the unified TriggerEvaluationRuntime
        + InternalEventAdapter handle event flow there — running the
        legacy hook in parallel would produce double evaluation.
        """
        if self._db is not None:
            return
        self._db_path = Path(data_dir) / "instance.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await _ensure_schema(self._db)
        await self._reload_cache()
        if attach_post_flush_hook:
            self._hook_callable = self._on_post_flush
            event_stream.register_post_flush_hook(self._hook_callable)
            self._hook_attached = True

    async def stop(self) -> None:
        if self._hook_attached and self._hook_callable is not None:
            event_stream.unregister_post_flush_hook(self._hook_callable)
            self._hook_attached = False
            self._hook_callable = None
        if self._db is not None:
            await self._db.close()
            self._db = None

    # -- registration ---------------------------------------------------

    async def register_trigger(self, trigger: Trigger) -> Trigger:
        """Validate + persist + cache. Returns the trigger with
        defaults filled in (trigger_id and created_at if absent).

        Atomic-registration contract: predicate is validated BEFORE
        any I/O so a malformed AST never reaches SQLite. After the
        INSERT commits, the cache is updated under the registry's
        cache lock. If the cache update raises (extremely unlikely
        for in-memory dicts), the persisted row is rolled back so the
        on-disk state cannot disagree with the in-memory state.
        """
        if self._db is None:
            raise RuntimeError("TriggerRegistry not started")
        # Validate predicate before doing any I/O.
        validate_predicate(trigger.predicate)
        if not trigger.trigger_id:
            trigger.trigger_id = str(uuid.uuid4())
        if not trigger.created_at:
            trigger.created_at = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO triggers ("
            " trigger_id, workflow_id, instance_id, event_type, predicate,"
            " predicate_source, description, actor_filter, correlation_filter,"
            " idempotency_key_template, owner, version, status, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            trigger.to_row(),
        )
        await self._db.commit()
        try:
            async with self._cache_lock:
                self._cache_insert(trigger)
        except Exception:
            # Roll back the persisted row so on-disk state matches
            # the in-memory state. If the rollback itself fails, both
            # exceptions are surfaced — the caller can inspect via
            # list_triggers and decide whether to retry.
            try:
                await self._db.execute(
                    "DELETE FROM triggers WHERE trigger_id = ?",
                    (trigger.trigger_id,),
                )
                await self._db.commit()
            except Exception as rb_exc:
                logger.error(
                    "TRIGGER_REGISTRATION_ROLLBACK_FAILED trigger_id=%s error=%s",
                    trigger.trigger_id, rb_exc, exc_info=True,
                )
            raise
        return trigger

    async def get_trigger(self, trigger_id: str) -> Trigger | None:
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT * FROM triggers WHERE trigger_id = ?", (trigger_id,),
        ) as cur:
            row = await cur.fetchone()
        return Trigger.from_row(row) if row else None

    async def list_triggers(
        self, instance_id: str, *, status: str | None = None,
    ) -> list[Trigger]:
        if self._db is None:
            return []
        if status is None:
            query = "SELECT * FROM triggers WHERE instance_id = ? ORDER BY created_at"
            args: tuple = (instance_id,)
        else:
            query = (
                "SELECT * FROM triggers WHERE instance_id = ? AND status = ? "
                "ORDER BY created_at"
            )
            args = (instance_id, status)
        async with self._db.execute(query, args) as cur:
            rows = await cur.fetchall()
        return [Trigger.from_row(r) for r in rows]

    async def update_status(self, trigger_id: str, status: str) -> bool:
        if self._db is None:
            return False
        if status not in {"active", "paused", "retired"}:
            raise ValueError(f"invalid status: {status!r}")
        await self._db.execute(
            "UPDATE triggers SET status = ? WHERE trigger_id = ?",
            (status, trigger_id),
        )
        await self._db.commit()
        # Reload the affected trigger to update the cache.
        trigger = await self.get_trigger(trigger_id)
        async with self._cache_lock:
            self._cache_remove(trigger_id)
            if trigger is not None and trigger.status == "active":
                self._cache_insert(trigger)
        return trigger is not None

    # -- listeners ------------------------------------------------------

    def add_match_listener(self, listener: MatchListener) -> None:
        """Register a callback that fires when any trigger matches an
        event. Listeners are how C5's execution engine plugs in.
        Idempotent for the same callable."""
        if listener in self._listeners:
            return
        self._listeners.append(listener)

    def remove_match_listener(self, listener: MatchListener) -> bool:
        if listener not in self._listeners:
            return False
        self._listeners.remove(listener)
        return True

    # -- cache helpers --------------------------------------------------

    def _cache_insert(self, trigger: Trigger) -> None:
        if trigger.status != "active":
            return
        per_instance = self._cache.setdefault(trigger.instance_id, {})
        per_instance.setdefault(trigger.event_type, []).append(trigger)

    def _cache_remove(self, trigger_id: str) -> None:
        for per_instance in self._cache.values():
            for event_type, triggers in list(per_instance.items()):
                kept = [t for t in triggers if t.trigger_id != trigger_id]
                if kept:
                    per_instance[event_type] = kept
                else:
                    del per_instance[event_type]

    async def _reload_cache(self) -> None:
        if self._db is None:
            return
        async with self._db.execute(
            "SELECT * FROM triggers WHERE status = 'active'"
        ) as cur:
            rows = await cur.fetchall()
        async with self._cache_lock:
            self._cache.clear()
            for row in rows:
                self._cache_insert(Trigger.from_row(row))

    def _candidates_for_event(self, event: Event) -> list[Trigger]:
        per_instance = self._cache.get(event.instance_id, {})
        candidates: list[Trigger] = []
        candidates.extend(per_instance.get(event.event_type, ()))
        candidates.extend(per_instance.get(_WILDCARD_EVENT_TYPE, ()))
        return candidates

    # -- post-flush hook -----------------------------------------------

    async def _on_post_flush(self, batch: list[Event]) -> None:
        """Hook callback. For each event in the freshly-flushed batch,
        evaluate matching triggers and dispatch listeners. Per-trigger
        try/except contains failures so one bad trigger doesn't
        suppress evaluation of the rest."""
        for event in batch:
            try:
                await self._evaluate_for_event(event)
            except Exception as exc:
                logger.warning(
                    "TRIGGER_REGISTRY_EVENT_EVALUATION_FAILED event_id=%s error=%s",
                    event.event_id, exc, exc_info=True,
                )

    async def _evaluate_for_event(self, event: Event) -> None:
        for trigger in self._candidates_for_event(event):
            try:
                if not self._prefilter(trigger, event):
                    continue
                if not evaluate_predicate(trigger.predicate, event):
                    continue
                if trigger.idempotency_key_template:
                    key = self._render_idempotency_key(trigger, event)
                    if key is None:
                        continue
                    fired_already = not await self._record_fire(
                        trigger, key, event,
                    )
                    if fired_already:
                        continue
                await self._dispatch_listeners(trigger, event)
            except PredicateError as exc:
                logger.warning(
                    "TRIGGER_PREDICATE_INVALID trigger_id=%s error=%s",
                    trigger.trigger_id, exc,
                )
            except Exception as exc:
                logger.warning(
                    "TRIGGER_EVALUATION_FAILED trigger_id=%s error=%s",
                    trigger.trigger_id, exc, exc_info=True,
                )

    def _prefilter(self, trigger: Trigger, event: Event) -> bool:
        if trigger.actor_filter and event.member_id != trigger.actor_filter:
            return False
        if trigger.correlation_filter and event.correlation_id != trigger.correlation_filter:
            return False
        return True

    def _render_idempotency_key(self, trigger: Trigger, event: Event) -> str | None:
        """Render the idempotency_key_template against the event.
        Returns None if the template references a missing field or
        uses disallowed attribute traversal. Templates use a subset of
        Python format syntax: bare names + ``[key]`` index access.
        Attribute traversal (``{payload.something}``) is rejected to
        prevent templates from reading arbitrary Python attributes
        of bound values."""
        if trigger.idempotency_key_template is None:
            return None
        try:
            return _IDEMPOTENCY_FORMATTER.format(
                trigger.idempotency_key_template,
                event_id=event.event_id,
                instance_id=event.instance_id,
                member_id=event.member_id or "",
                space_id=event.space_id or "",
                correlation_id=event.correlation_id or "",
                event_type=event.event_type,
                timestamp=event.timestamp,
                payload=event.payload,
            )
        except (KeyError, IndexError, AttributeError, ValueError, TypeError):
            return None

    async def _record_fire(
        self, trigger: Trigger, key: str, event: Event,
    ) -> bool:
        """Try to insert (trigger_id, key) into trigger_fires. Returns
        True if this is a fresh fire, False if it was already
        recorded (suppress duplicate)."""
        if self._db is None:
            return True  # no persistence, treat as fresh
        try:
            await self._db.execute(
                "INSERT INTO trigger_fires "
                "(trigger_id, idempotency_key, event_id, fired_at) "
                "VALUES (?, ?, ?, ?)",
                (trigger.trigger_id, key, event.event_id,
                 datetime.now(timezone.utc).isoformat()),
            )
            await self._db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def _dispatch_listeners(self, trigger: Trigger, event: Event) -> None:
        if not self._listeners:
            return
        for listener in tuple(self._listeners):
            try:
                result = listener(trigger, event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.warning(
                    "TRIGGER_LISTENER_FAILED listener=%s trigger_id=%s error=%s",
                    getattr(listener, "__qualname__", repr(listener)),
                    trigger.trigger_id, exc, exc_info=True,
                )


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


async def _reset_for_tests(registry: TriggerRegistry) -> None:
    """Stop a registry and drop its state so the next test starts clean.
    Idempotent."""
    await registry.stop()
    registry._cache.clear()
    registry._listeners.clear()


__all__ = [
    "MatchListener",
    "Trigger",
    "TriggerRegistry",
]
