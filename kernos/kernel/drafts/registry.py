"""Draft registry — persistent, conversational workflow drafts.

WDP C1: schema + ``WorkflowDraft`` dataclass + ``create_draft`` +
``get_draft``. C2 adds the state-machine mutations
(``update_draft`` / ``mark_committed`` / ``abandon_draft``) +
envelope validation + concurrency. C3 adds ``list_drafts`` +
``cleanup_abandoned_older_than`` + the live sweep.

**Future-composition requirement** (design review-stated invariant
for downstream specs): WorkflowDraft persistence must remain
substrate-neutral while carrying enough lightweight context for
future surfaces to attach intelligently. Drafts MUST NOT depend
directly on Canvas, tools, domains, agents, or any specific
future subsystem. Instead, they expose stable identity
(``draft_id``), lifecycle state (``status``, the state machine),
provenance (``source_thread_id``, ``created_at``,
``last_touched_at``), mutable ``home_space_id`` for "most-relevant-
to" semantics, human-readable intent/resolution notes
(``intent_summary``, ``resolution_notes``), and factual lifecycle
events (the five ``draft.*`` event types) so other surfaces can
project drafts into their own world. Canvas can render or
annotate pending routines; tools can validate capabilities;
domains can provide briefs; future systems can attach by
reference. The elegant shape is not WDP knowing every surface —
it is WDP being a small, durable coordination object that other
systems can discover, reference, and enrich without coupling.
Reviewers of any WDP follow-on spec should reject changes that
introduce direct dependencies on adjacent subsystems.

Design notes:

* **Composite primary key ``(instance_id, draft_id)``** — DAR's
  pattern. Two instances may both have a ``draft-001`` without
  collision. Cross-instance lookups return ``None`` / empty.
* **NOT NULL defaults** for ``aliases`` (``'[]'``) and
  ``partial_spec_json`` (``'{}'``) so the Python class never
  holds None for those fields. Schema rejects NULL inserts at
  the SQL layer (AC #20).
* **All public methods are keyword-only with ``instance_id`` as
  the first declared keyword parameter** (AC #19) — prevents
  positional inversion bugs across multi-tenant calls.
* **Optimistic concurrency** via a ``version`` column. C2's
  mutation methods take ``expected_version`` and increment
  atomically; mismatches raise ``DraftConcurrentModification``.
* **Event emission is optional**: an injected callable
  ``(event_type, payload, instance_id) -> Awaitable[None]``.
  ``None`` means no-op (test-fixture path). Production wires the
  emitter to ``event_stream.emit`` at engine bring-up.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiosqlite

from kernos.kernel.drafts.envelope import validate_envelope
from kernos.kernel.drafts.errors import (
    DraftAliasCollision,
    DraftConcurrentModification,
    DraftEnvelopeInvalid,
    DraftError,
    DraftNotFound,
    DraftTerminal,
    InvalidDraftTransition,
    ReadyStateMutationRequiresDemotion,
    WorkflowReferenceMissing,
)

logger = logging.getLogger(__name__)


VALID_DRAFT_STATUSES = frozenset({
    "shaping", "blocked", "ready", "committed", "abandoned",
})

TERMINAL_STATUSES = frozenset({"committed", "abandoned"})


# State machine matrix per spec. Maps each non-terminal status to
# the set of allowed *direct* targets via update_draft. Note:
# ``ready → committed`` is intentionally NOT in this matrix —
# that transition is gated by ``mark_committed`` only (AC #6).
_ALLOWED_TRANSITIONS = {
    "shaping": frozenset({"shaping", "blocked", "ready"}),
    "blocked": frozenset({"shaping", "blocked"}),
    "ready":   frozenset({"ready", "shaping"}),
    # Terminal states have no outbound; mutations on them raise
    # DraftTerminal before this matrix is consulted.
}


# Substantive content fields (AC #14). Mutating any of these on
# a ``status='ready'`` draft requires explicit demotion to
# ``status='shaping'`` in the same call.
_SUBSTANTIVE_CONTENT_FIELDS = frozenset({
    "partial_spec_json", "display_name", "aliases",
    "intent_summary", "resolution_notes",
})


# Sentinel used by update_draft to distinguish "don't change this
# field" from "set to None". We use a unique class-typed singleton
# so type checkers can keep the union shapes honest.
class _UnsetT:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "_UNSET"


_UNSET = _UnsetT()


# Event emitter signature: (event_type, payload, *, instance_id) →
# Awaitable[None] | None. Tests pass None and skip; production
# wires this to a closure around event_stream.emit.
EventEmitter = Callable[..., "Awaitable[None] | None"]


# ---------------------------------------------------------------------------
# WorkflowDraft dataclass
# ---------------------------------------------------------------------------


@dataclass
class WorkflowDraft:
    """In-memory representation of a row in ``workflow_drafts``.

    ``aliases`` and ``partial_spec_json`` are NEVER None — schema
    NOT NULL with safe defaults (the design review edit, v1 → v2). The Python
    class enforces the same: empty list / empty dict, never None.
    """

    draft_id: str
    instance_id: str
    status: str = "shaping"
    home_space_id: str | None = None
    display_name: str | None = None
    aliases: list[str] = field(default_factory=list)
    intent_summary: str = ""
    partial_spec_json: dict = field(default_factory=dict)
    resolution_notes: str | None = None
    source_thread_id: str | None = None
    version: int = 0
    created_at: str = ""
    updated_at: str = ""
    last_touched_at: str = ""
    committed_workflow_id: str | None = None

    def to_row(self) -> tuple:
        return (
            self.draft_id,
            self.instance_id,
            self.home_space_id,
            self.status,
            self.display_name,
            json.dumps(self.aliases),
            self.intent_summary,
            json.dumps(self.partial_spec_json),
            self.resolution_notes,
            self.source_thread_id,
            self.version,
            self.created_at,
            self.updated_at,
            self.last_touched_at,
            self.committed_workflow_id,
        )

    @classmethod
    def from_row(cls, row) -> "WorkflowDraft":
        try:
            aliases = json.loads(row["aliases"]) if row["aliases"] else []
        except Exception:
            aliases = []
        try:
            partial = json.loads(row["partial_spec_json"]) if row["partial_spec_json"] else {}
        except Exception:
            partial = {}
        if not isinstance(aliases, list):
            aliases = []
        if not isinstance(partial, dict):
            partial = {}
        return cls(
            draft_id=row["draft_id"],
            instance_id=row["instance_id"],
            home_space_id=row["home_space_id"],
            status=row["status"],
            display_name=row["display_name"],
            aliases=aliases,
            intent_summary=row["intent_summary"] or "",
            partial_spec_json=partial,
            resolution_notes=row["resolution_notes"],
            source_thread_id=row["source_thread_id"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_touched_at=row["last_touched_at"],
            committed_workflow_id=row["committed_workflow_id"],
        )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_DRAFT_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_drafts (
    draft_id            TEXT NOT NULL,
    instance_id         TEXT NOT NULL,
    home_space_id       TEXT,
    status              TEXT NOT NULL CHECK (
        status IN ('shaping','blocked','ready','committed','abandoned')
    ),
    display_name        TEXT,
    aliases             TEXT NOT NULL DEFAULT '[]',
    intent_summary      TEXT,
    partial_spec_json   TEXT NOT NULL DEFAULT '{}',
    resolution_notes    TEXT,
    source_thread_id    TEXT,
    version             INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    last_touched_at     TEXT NOT NULL,
    committed_workflow_id TEXT,
    PRIMARY KEY (instance_id, draft_id)
);
CREATE INDEX IF NOT EXISTS idx_workflow_drafts_status
    ON workflow_drafts (instance_id, status);
CREATE INDEX IF NOT EXISTS idx_workflow_drafts_home
    ON workflow_drafts (instance_id, home_space_id);
CREATE INDEX IF NOT EXISTS idx_workflow_drafts_touched
    ON workflow_drafts (instance_id, last_touched_at);
"""


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    for stmt in _DRAFT_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            await db.execute(stmt)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it's awaitable; otherwise return as-is.
    Lets ``mark_committed`` accept both sync and async
    ``workflow_registry.exists`` implementations."""
    import inspect as _inspect
    if _inspect.isawaitable(value):
        return await value
    return value


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class DraftRegistry:
    """Persistence + lifecycle layer for workflow drafts.

    All public methods are keyword-only with ``instance_id`` listed
    first (AC #19). The class follows DAR's per-subsystem
    aiosqlite connection pattern (``isolation_level=None`` for
    explicit transaction control where needed).
    """

    def __init__(
        self,
        *,
        event_emitter: EventEmitter | None = None,
    ) -> None:
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._lock = asyncio.Lock()
        self._event_emitter = event_emitter

    async def start(self, data_dir: str) -> None:
        """Open the SQLite connection and ensure schema. Idempotent."""
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

    # -- create_draft ---------------------------------------------------

    async def create_draft(
        self,
        *,
        instance_id: str,
        intent_summary: str,
        home_space_id: str | None = None,
        source_thread_id: str | None = None,
    ) -> WorkflowDraft:
        """Atomic insert. Generates ``draft_id`` (UUIDv4). Initial
        ``status='shaping'``, ``version=0``.
        ``created_at == updated_at == last_touched_at == now``.
        Emits ``draft.created`` after persistence.
        """
        if self._db is None:
            raise RuntimeError("DraftRegistry not started")
        if not instance_id:
            raise ValueError("instance_id is required")
        now = _now()
        draft = WorkflowDraft(
            draft_id=str(uuid.uuid4()),
            instance_id=instance_id,
            home_space_id=home_space_id,
            status="shaping",
            intent_summary=intent_summary,
            source_thread_id=source_thread_id,
            version=0,
            created_at=now,
            updated_at=now,
            last_touched_at=now,
        )
        async with self._lock:
            await self._db.execute(
                "INSERT INTO workflow_drafts ("
                " draft_id, instance_id, home_space_id, status,"
                " display_name, aliases, intent_summary,"
                " partial_spec_json, resolution_notes, source_thread_id,"
                " version, created_at, updated_at, last_touched_at,"
                " committed_workflow_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                draft.to_row(),
            )
        await self._emit("draft.created", {
            "draft_id": draft.draft_id,
            "instance_id": draft.instance_id,
            "home_space_id": draft.home_space_id,
            "intent_summary": draft.intent_summary,
            "source_thread_id": draft.source_thread_id,
            "created_at": draft.created_at,
        }, instance_id=draft.instance_id)
        return draft

    # -- get_draft ------------------------------------------------------

    async def get_draft(
        self,
        *,
        instance_id: str,
        draft_id: str,
    ) -> WorkflowDraft | None:
        """Deterministic lookup. Returns the row regardless of
        status — terminal-state drafts are still readable (audit
        surfaces consume this). Cross-instance lookups return None."""
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT * FROM workflow_drafts "
            "WHERE instance_id = ? AND draft_id = ?",
            (instance_id, draft_id),
        ) as cur:
            row = await cur.fetchone()
        return WorkflowDraft.from_row(row) if row else None

    # -- update_draft (C2) --------------------------------------------

    async def update_draft(
        self,
        *,
        instance_id: str,
        draft_id: str,
        expected_version: int,
        home_space_id: "str | None | _UnsetT" = _UNSET,
        display_name: "str | None | _UnsetT" = _UNSET,
        aliases: "list[str] | _UnsetT" = _UNSET,
        intent_summary: "str | _UnsetT" = _UNSET,
        partial_spec_json: "dict | _UnsetT" = _UNSET,
        resolution_notes: "str | None | _UnsetT" = _UNSET,
        status: "str | _UnsetT" = _UNSET,
    ) -> WorkflowDraft:
        """Compare-and-swap mutation with envelope validation,
        state-machine enforcement, ready-state demotion guard,
        alias collision check, and event emission.

        See spec section "DraftRegistry API" for the full
        contract; the inline comments below trace the validation
        order and lock the spec invariants in place.
        """
        if self._db is None:
            raise RuntimeError("DraftRegistry not started")
        async with self._lock:
            current = await self.get_draft(
                instance_id=instance_id, draft_id=draft_id,
            )
            if current is None:
                raise DraftNotFound(
                    f"draft_id {draft_id!r} not found in instance "
                    f"{instance_id!r}"
                )
            if current.status in TERMINAL_STATUSES:
                raise DraftTerminal(
                    f"draft {draft_id!r} is in terminal status "
                    f"{current.status!r}; mutations forbidden"
                )
            if current.version != expected_version:
                raise DraftConcurrentModification(
                    f"expected version {expected_version}, "
                    f"current is {current.version}"
                )

            # Status transition enforcement (AC #4, #6).
            if not isinstance(status, _UnsetT):
                if status not in VALID_DRAFT_STATUSES:
                    raise InvalidDraftTransition(
                        f"unknown status {status!r}"
                    )
                # mark_committed is the only path to committed (AC #6).
                if status == "committed":
                    raise InvalidDraftTransition(
                        "status='committed' must be set via "
                        "mark_committed, not update_draft"
                    )
                # abandon_draft is the only path to abandoned.
                if status == "abandoned":
                    raise InvalidDraftTransition(
                        "status='abandoned' must be set via "
                        "abandon_draft, not update_draft"
                    )
                allowed = _ALLOWED_TRANSITIONS.get(
                    current.status, frozenset(),
                )
                if status not in allowed:
                    raise InvalidDraftTransition(
                        f"transition {current.status!r} → {status!r} "
                        f"not in allowed set {sorted(allowed)}"
                    )

            # Ready-state demotion guard (AC #14). Substantive
            # content fields per spec; non-substantive
            # (home_space_id, status alone) doesn't trigger.
            #
            # Codex consolidated review: detect substantive
            # mutations by VALUE-CHANGE, not parameter presence.
            # A no-op set of display_name to its current value
            # should not demand demotion. Compare each provided
            # field to the row's current value; only count it as
            # mutation when they differ.
            def _changes(name: str, provided: Any) -> bool:
                if isinstance(provided, _UnsetT):
                    return False
                return getattr(current, name) != provided
            substantive_mutation = (
                _changes("display_name", display_name)
                or _changes("aliases", aliases)
                or _changes("intent_summary", intent_summary)
                or _changes("partial_spec_json", partial_spec_json)
                or _changes("resolution_notes", resolution_notes)
            )
            if (
                current.status == "ready"
                and substantive_mutation
                and (isinstance(status, _UnsetT) or status != "shaping")
            ):
                raise ReadyStateMutationRequiresDemotion(
                    f"draft {draft_id!r} is ready; substantive content "
                    f"mutations require explicit status='shaping' "
                    f"demotion in the same call"
                )

            # Envelope validation on partial_spec_json (AC #8, #9).
            if not isinstance(partial_spec_json, _UnsetT):
                validate_envelope(partial_spec_json)

            # Alias collision check (AC #15) — distinct typed error.
            if not isinstance(aliases, _UnsetT):
                if not isinstance(aliases, list):
                    raise DraftEnvelopeInvalid(
                        f"aliases must be a list, got "
                        f"{type(aliases).__name__}"
                    )
                conflict = await self._find_alias_conflict(
                    instance_id=instance_id,
                    proposed_aliases=aliases,
                    self_draft_id=draft_id,
                )
                if conflict is not None:
                    alias, owner_id = conflict
                    raise DraftAliasCollision(
                        f"alias {alias!r} already claimed by active "
                        f"draft {owner_id!r} in instance {instance_id!r}"
                    )

            # Apply field updates to a working copy.
            new_status = (
                current.status if isinstance(status, _UnsetT)
                else status
            )
            new_home_space_id = (
                current.home_space_id
                if isinstance(home_space_id, _UnsetT)
                else home_space_id
            )
            new_display_name = (
                current.display_name
                if isinstance(display_name, _UnsetT)
                else display_name
            )
            new_aliases = (
                current.aliases
                if isinstance(aliases, _UnsetT)
                else list(aliases)
            )
            new_intent_summary = (
                current.intent_summary
                if isinstance(intent_summary, _UnsetT)
                else intent_summary
            )
            new_partial_spec = (
                current.partial_spec_json
                if isinstance(partial_spec_json, _UnsetT)
                else partial_spec_json
            )
            new_resolution_notes = (
                current.resolution_notes
                if isinstance(resolution_notes, _UnsetT)
                else resolution_notes
            )
            now = _now()
            new_version = current.version + 1

            # Codex consolidated review (HIGH): include version in
            # the WHERE clause so the CAS happens at the SQL layer,
            # not just in Python. Without this, a writer on a
            # different connection could slip a write between our
            # read and update; the in-process self._lock only
            # serialises this DraftRegistry instance. After the
            # UPDATE we check rowcount; if 0, another writer
            # advanced the version after our read — re-raise as
            # DraftConcurrentModification.
            async with self._db.execute(
                "UPDATE workflow_drafts SET"
                " home_space_id = ?, status = ?, display_name = ?,"
                " aliases = ?, intent_summary = ?,"
                " partial_spec_json = ?, resolution_notes = ?,"
                " version = ?, updated_at = ?, last_touched_at = ? "
                "WHERE instance_id = ? AND draft_id = ? "
                "AND version = ?",
                (
                    new_home_space_id, new_status, new_display_name,
                    json.dumps(new_aliases), new_intent_summary,
                    json.dumps(new_partial_spec), new_resolution_notes,
                    new_version, now, now,
                    instance_id, draft_id, expected_version,
                ),
            ) as cur:
                if cur.rowcount == 0:
                    raise DraftConcurrentModification(
                        f"version advanced concurrently for draft "
                        f"{draft_id!r} (expected {expected_version})"
                    )

        # Read back the persisted row.
        updated = await self.get_draft(
            instance_id=instance_id, draft_id=draft_id,
        )
        # Event emission. Order matters only for downstream
        # observers — we emit status_changed first, then the more
        # specific home_space_changed if applicable.
        if updated.status != current.status:
            await self._emit("draft.status_changed", {
                "draft_id": draft_id,
                "instance_id": instance_id,
                "old_status": current.status,
                "new_status": updated.status,
                "version": updated.version,
                "changed_at": updated.updated_at,
            }, instance_id=instance_id)
        if updated.home_space_id != current.home_space_id:
            await self._emit("draft.home_space_changed", {
                "draft_id": draft_id,
                "instance_id": instance_id,
                "old_home_space_id": current.home_space_id,
                "new_home_space_id": updated.home_space_id,
                "changed_at": updated.updated_at,
            }, instance_id=instance_id)
        return updated

    # -- mark_committed (C2) ------------------------------------------

    async def mark_committed(
        self,
        *,
        instance_id: str,
        draft_id: str,
        expected_version: int,
        committed_workflow_id: str,
        workflow_registry: Any | None = None,
    ) -> WorkflowDraft:
        """Sole path from ``status='ready'`` to ``status='committed'``
        (AC #6). CAS on version. Optional runtime existence check
        via the passed ``workflow_registry`` (AC #12). Emits
        ``draft.status_changed`` AND ``draft.committed`` after
        persistence (AC #11).
        """
        if self._db is None:
            raise RuntimeError("DraftRegistry not started")
        if not committed_workflow_id:
            raise ValueError("committed_workflow_id is required")
        async with self._lock:
            current = await self.get_draft(
                instance_id=instance_id, draft_id=draft_id,
            )
            if current is None:
                raise DraftNotFound(
                    f"draft_id {draft_id!r} not found in instance "
                    f"{instance_id!r}"
                )
            if current.status in TERMINAL_STATUSES:
                raise DraftTerminal(
                    f"draft {draft_id!r} is in terminal status "
                    f"{current.status!r}"
                )
            if current.version != expected_version:
                raise DraftConcurrentModification(
                    f"expected version {expected_version}, "
                    f"current is {current.version}"
                )
            # AC #13: mark_committed requires status='ready'.
            if current.status != "ready":
                raise InvalidDraftTransition(
                    f"mark_committed requires status='ready'; "
                    f"draft {draft_id!r} is in status "
                    f"{current.status!r}"
                )
            # AC #12: optional runtime existence check.
            if workflow_registry is not None:
                exists = await _maybe_await(workflow_registry.exists(
                    committed_workflow_id, instance_id,
                ))
                if not exists:
                    raise WorkflowReferenceMissing(
                        f"committed_workflow_id "
                        f"{committed_workflow_id!r} not found in "
                        f"workflow_registry for instance "
                        f"{instance_id!r}"
                    )
            now = _now()
            new_version = current.version + 1
            # Codex iteration: SQL-layer CAS on version.
            async with self._db.execute(
                "UPDATE workflow_drafts SET status = 'committed', "
                "committed_workflow_id = ?, version = ?, "
                "updated_at = ?, last_touched_at = ? "
                "WHERE instance_id = ? AND draft_id = ? "
                "AND version = ?",
                (
                    committed_workflow_id, new_version, now, now,
                    instance_id, draft_id, expected_version,
                ),
            ) as cur:
                if cur.rowcount == 0:
                    raise DraftConcurrentModification(
                        f"version advanced concurrently for draft "
                        f"{draft_id!r} (expected {expected_version})"
                    )
        updated = await self.get_draft(
            instance_id=instance_id, draft_id=draft_id,
        )
        # AC #11: emit BOTH status_changed AND committed.
        await self._emit("draft.status_changed", {
            "draft_id": draft_id,
            "instance_id": instance_id,
            "old_status": current.status,
            "new_status": "committed",
            "version": updated.version,
            "changed_at": updated.updated_at,
        }, instance_id=instance_id)
        await self._emit("draft.committed", {
            "draft_id": draft_id,
            "instance_id": instance_id,
            "committed_workflow_id": committed_workflow_id,
            "version": updated.version,
            "committed_at": updated.updated_at,
        }, instance_id=instance_id)
        return updated

    # -- abandon_draft (C2) -------------------------------------------

    async def abandon_draft(
        self,
        *,
        instance_id: str,
        draft_id: str,
        expected_version: int,
    ) -> WorkflowDraft:
        """Transition any non-terminal status to ``abandoned``.
        Row preserved for audit (no destructive deletion). CAS on
        version. Emits ``draft.status_changed`` AND
        ``draft.abandoned`` after persistence (AC #11)."""
        if self._db is None:
            raise RuntimeError("DraftRegistry not started")
        async with self._lock:
            current = await self.get_draft(
                instance_id=instance_id, draft_id=draft_id,
            )
            if current is None:
                raise DraftNotFound(
                    f"draft_id {draft_id!r} not found in instance "
                    f"{instance_id!r}"
                )
            if current.status in TERMINAL_STATUSES:
                raise DraftTerminal(
                    f"draft {draft_id!r} is in terminal status "
                    f"{current.status!r}"
                )
            if current.version != expected_version:
                raise DraftConcurrentModification(
                    f"expected version {expected_version}, "
                    f"current is {current.version}"
                )
            now = _now()
            new_version = current.version + 1
            # Codex iteration: SQL-layer CAS on version.
            async with self._db.execute(
                "UPDATE workflow_drafts SET status = 'abandoned', "
                "version = ?, updated_at = ?, last_touched_at = ? "
                "WHERE instance_id = ? AND draft_id = ? "
                "AND version = ?",
                (new_version, now, now, instance_id, draft_id,
                 expected_version),
            ) as cur:
                if cur.rowcount == 0:
                    raise DraftConcurrentModification(
                        f"version advanced concurrently for draft "
                        f"{draft_id!r} (expected {expected_version})"
                    )
        updated = await self.get_draft(
            instance_id=instance_id, draft_id=draft_id,
        )
        await self._emit("draft.status_changed", {
            "draft_id": draft_id,
            "instance_id": instance_id,
            "old_status": current.status,
            "new_status": "abandoned",
            "version": updated.version,
            "changed_at": updated.updated_at,
        }, instance_id=instance_id)
        await self._emit("draft.abandoned", {
            "draft_id": draft_id,
            "instance_id": instance_id,
            "prior_status": current.status,
            "version": updated.version,
            "abandoned_at": updated.updated_at,
        }, instance_id=instance_id)
        return updated

    # -- list_drafts (C3) ----------------------------------------------

    async def list_drafts(
        self,
        *,
        instance_id: str,
        status: str | None = None,
        home_space_id: str | None = None,
        include_terminal: bool = False,
    ) -> list[WorkflowDraft]:
        """Return drafts in an instance, ordered by ``last_touched_at``
        descending (most recently touched first — useful for surfacing
        what the user was working on most recently).

        Default: excludes ``committed`` AND ``abandoned``
        (AC #16). ``include_terminal=True`` or an explicit ``status``
        filter surfaces them. ``home_space_id`` narrows further.
        """
        if self._db is None:
            return []
        clauses = ["instance_id = ?"]
        args: list = [instance_id]
        if status is not None:
            if status not in VALID_DRAFT_STATUSES:
                raise ValueError(
                    f"unknown status filter {status!r}; "
                    f"valid: {sorted(VALID_DRAFT_STATUSES)}"
                )
            clauses.append("status = ?")
            args.append(status)
        elif not include_terminal:
            clauses.append(
                "status NOT IN ('committed', 'abandoned')"
            )
        if home_space_id is not None:
            clauses.append("home_space_id = ?")
            args.append(home_space_id)
        query = (
            "SELECT * FROM workflow_drafts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY last_touched_at DESC"
        )
        async with self._db.execute(query, args) as cur:
            rows = await cur.fetchall()
        return [WorkflowDraft.from_row(r) for r in rows]

    # -- cleanup (C3) --------------------------------------------------

    async def cleanup_abandoned_older_than(
        self,
        *,
        instance_id: str,
        days: int,
    ) -> int:
        """Delete abandoned rows older than ``days`` in this instance.

        Pin (AC #17): only ``abandoned`` rows are eligible. Active /
        blocked / ready / committed rows are NEVER touched, regardless
        of their ``updated_at``. Cross-instance: scoped to the calling
        ``instance_id`` via the WHERE clause; instance B's abandoned
        rows are invisible to a cleanup call scoped to instance A.

        Returns the count of rows deleted.

        Operator-explicit, time-bounded — this is the one method on
        WDP's surface that can remove rows. There is no scheduler;
        callers pick when to run it. The kernel's standing
        no-destructive-deletions principle is satisfied because the
        deletion is opt-in retention policy, not substrate behaviour.
        """
        if self._db is None:
            return 0
        if days < 0:
            raise ValueError("days must be non-negative")
        from datetime import timedelta
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).isoformat()
        async with self._lock:
            async with self._db.execute(
                "DELETE FROM workflow_drafts "
                "WHERE instance_id = ? "
                "AND status = 'abandoned' "
                "AND updated_at < ?",
                (instance_id, cutoff),
            ) as cur:
                deleted = cur.rowcount
        return int(deleted) if deleted is not None else 0

    # -- alias-collision helper ----------------------------------------

    async def _find_alias_conflict(
        self,
        *,
        instance_id: str,
        proposed_aliases: list[str],
        self_draft_id: str,
    ) -> tuple[str, str] | None:
        """Walk active (non-terminal) drafts in this instance;
        return (offending_alias, conflicting_draft_id) on first
        overlap. Aliases compared case-insensitively, matching
        DAR's pattern."""
        if self._db is None or not proposed_aliases:
            return None
        proposed_lower_to_authored = {
            a.lower(): a for a in proposed_aliases
        }
        proposed_lower = set(proposed_lower_to_authored)
        async with self._db.execute(
            "SELECT draft_id, aliases FROM workflow_drafts "
            "WHERE instance_id = ? AND status IN "
            "('shaping','blocked','ready')",
            (instance_id,),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            if row["draft_id"] == self_draft_id:
                continue
            try:
                claimed_raw = json.loads(row["aliases"]) or []
            except Exception:
                claimed_raw = []
            claimed_lower = {a.lower() for a in claimed_raw}
            overlap = proposed_lower & claimed_lower
            if overlap:
                offending_lower = next(iter(overlap))
                authored = proposed_lower_to_authored[offending_lower]
                return (authored, row["draft_id"])
        return None

    # -- event emission helper ----------------------------------------

    async def _emit(
        self, event_type: str, payload: dict, *, instance_id: str,
    ) -> None:
        """Fire-and-forget event emission. ``self._event_emitter``
        of None is the no-op test-fixture path; production wires it
        to ``event_stream.emit``. Failures are caught + logged so
        emission never breaks the caller's mutation flow."""
        if self._event_emitter is None:
            return
        try:
            result = self._event_emitter(
                event_type=event_type,
                payload=payload,
                instance_id=instance_id,
            )
            # Codex consolidated review: the type contract is
            # ``Awaitable[None] | None``. asyncio.iscoroutine only
            # catches bare coroutines; Tasks / Futures / custom
            # __await__ objects need inspect.isawaitable.
            import inspect as _inspect
            if _inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning(
                "DRAFT_EVENT_EMIT_FAILED type=%s draft_id=%s error=%s",
                event_type, payload.get("draft_id"), exc,
            )


__all__ = [
    "DraftRegistry",
    "EventEmitter",
    "TERMINAL_STATUSES",
    "VALID_DRAFT_STATUSES",
    "WorkflowDraft",
]
