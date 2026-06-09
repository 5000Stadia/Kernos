"""SELF-IMPROVEMENT-CLOSURE-V1 — closure-attempt substrate.

Owns three SQLite tables backing the closure-machinery:

* ``invariant`` — normative rules the substrate should honor.
* ``friction_pattern_invariant`` — many-to-many link between
  observed friction patterns (symptoms) and invariants (the
  contracts they violate).
* ``closure_attempt`` — durable record of each remediation
  attempt, including the route taken, the probe used to verify
  the fix, and the resulting outcome.

Mirrors the per-module-connection pattern used by
:class:`kernos.kernel.friction_patterns.FrictionPatternStore` —
own ``aiosqlite`` connection over the shared
``data/instance.db`` file, ``PRAGMA foreign_keys=ON`` set per-
connection, idempotent schema setup at ``start()``.

The high-level kernel-tool entry points (``record_closure_attempt``,
``run_closure_probe``, ``lookup_pattern_invariants``) live in this
module as module-level async functions; they receive a
``ClosureStore`` instance from the caller (matches the autonomy-
adapter calling convention).
"""
from __future__ import annotations

import asyncio
import json
import logging
from kernos.utils import utc_now
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import aiosqlite


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enumerated constants
# ---------------------------------------------------------------------------


# Route the closure attempt took. v1 ships ``code_change_via_cc``
# as the only implemented route; the rest are enumerated so a
# ClosureAttempt row can record what was tried when a future spec
# implements those handlers.
ROUTE_CLASSES: frozenset[str] = frozenset({
    "code_change_via_cc",
    "covenant_update",
    "prompt_change",
    "tool_surface_fix",
    "codex_review_only",
    "human_only",
})


# v1 ships only ``deterministic_introspection``.
# ``event_absence_window`` and ``manual_operator_confirmation`` are
# explicitly deferred (see spec section "PROBE_KINDS (v1
# enumerated)").
PROBE_KINDS: frozenset[str] = frozenset({
    "deterministic_introspection",
})


# AC8 — probe kinds that perform read-only substrate inspection.
# ``run_closure_probe`` hard-rejects any probe_kind NOT in this set
# even if extra fields like ``_approval_receipt`` are present in
# ``route_payload``. No bypass.
READ_ONLY_PROBE_KINDS: frozenset[str] = frozenset({
    "deterministic_introspection",
})


OUTCOME_PENDING = "pending"
OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_ABORTED = "aborted"


VALID_OUTCOMES: frozenset[str] = frozenset({
    OUTCOME_PENDING,
    OUTCOME_PASSED,
    OUTCOME_FAILED,
    OUTCOME_ABORTED,
})


VALID_INVARIANT_OWNERS: frozenset[str] = frozenset({
    "architect",
    "operator",
    "kernos",
})


VALID_INVARIANT_STATUSES: frozenset[str] = frozenset({
    "active",
    "deprecated",
})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ClosureStoreError(Exception):
    """Base for closure-store errors."""


class ProbeKindNotAllowed(ClosureStoreError):
    """Raised when ``run_closure_probe`` is called with a
    ``probe_kind`` not in :data:`READ_ONLY_PROBE_KINDS`.

    AC8 — no payload-based bypass; extra fields in
    ``route_payload`` cannot override the allowlist.
    """


class InvariantNotLinkedToPattern(ClosureStoreError):
    """Raised by ``record_closure_attempt`` when the requested
    ``(pattern_id, invariant_id)`` pair has no row in
    ``friction_pattern_invariant``.

    AC13 — the link table is the single source of truth for the
    pattern↔invariant relationship; closures cannot be created
    for unlinked pairs.
    """


class ClosureAttemptNotFound(ClosureStoreError):
    """Raised when ``run_closure_probe`` is called with a
    ``closure_id`` that does not exist."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_INVARIANT_DDL = """
CREATE TABLE IF NOT EXISTS invariant (
    instance_id  TEXT NOT NULL,
    invariant_id TEXT NOT NULL,
    statement    TEXT NOT NULL,
    owner        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL,
    last_edited  TEXT NOT NULL,
    PRIMARY KEY (instance_id, invariant_id),
    CHECK (owner IN ('architect', 'operator', 'kernos')),
    CHECK (status IN ('active', 'deprecated'))
)
"""


_FRICTION_PATTERN_INVARIANT_DDL = """
CREATE TABLE IF NOT EXISTS friction_pattern_invariant (
    instance_id  TEXT NOT NULL,
    pattern_id   TEXT NOT NULL,
    invariant_id TEXT NOT NULL,
    relation     TEXT NOT NULL DEFAULT 'violates',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (instance_id, pattern_id, invariant_id, relation),
    FOREIGN KEY (instance_id, pattern_id)
        REFERENCES friction_pattern(instance_id, pattern_id),
    FOREIGN KEY (instance_id, invariant_id)
        REFERENCES invariant(instance_id, invariant_id)
)
"""


_CLOSURE_ATTEMPT_DDL = """
CREATE TABLE IF NOT EXISTS closure_attempt (
    instance_id            TEXT NOT NULL,
    closure_id             TEXT NOT NULL,
    pattern_id             TEXT NOT NULL,
    invariant_id           TEXT NOT NULL,
    active_epoch           INTEGER NOT NULL,
    route                  TEXT NOT NULL,
    route_payload_json     TEXT NOT NULL,
    probe_kind             TEXT NOT NULL,
    probe_payload_json     TEXT NOT NULL,
    probe_payload_version  INTEGER NOT NULL,
    outcome                TEXT NOT NULL DEFAULT 'pending',
    outcome_evidence_json  TEXT NOT NULL DEFAULT '{}',
    started_at             TEXT NOT NULL,
    completed_at           TEXT,
    PRIMARY KEY (instance_id, closure_id),
    FOREIGN KEY (instance_id, pattern_id)
        REFERENCES friction_pattern(instance_id, pattern_id),
    FOREIGN KEY (instance_id, invariant_id)
        REFERENCES invariant(instance_id, invariant_id),
    CHECK (outcome IN ('pending', 'passed', 'failed', 'aborted'))
)
"""


# AC3 — one pending closure per (pattern, invariant, episode).
# Partial unique index allows re-pending after a failed attempt
# in the same episode (the failed row is excluded from the
# uniqueness check).
_CLOSURE_ATTEMPT_PENDING_UNIQ = """
CREATE UNIQUE INDEX IF NOT EXISTS closure_attempt_pending_unique
    ON closure_attempt(instance_id, pattern_id, invariant_id, active_epoch)
    WHERE outcome = 'pending'
"""


_CLOSURE_ATTEMPT_IDX_PATTERN = """
CREATE INDEX IF NOT EXISTS idx_closure_attempt_pattern
    ON closure_attempt (instance_id, pattern_id, started_at DESC)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------




def _gen_closure_id() -> str:
    """Compact hex token; collisions astronomically unlikely."""
    return secrets.token_hex(12)


# ---------------------------------------------------------------------------
# ClosureStore
# ---------------------------------------------------------------------------


class ClosureStore:
    """SQLite-backed catalog over ``data/instance.db``.

    Owns its own aiosqlite connection (per-module-isolation pattern;
    mirrors :class:`kernos.kernel.friction_patterns.FrictionPatternStore`).

    Schema setup is idempotent. ``PRAGMA foreign_keys=ON`` is set on
    the connection (does not persist across connections).
    """

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._write_lock = asyncio.Lock()

    # --- Lifecycle ----------------------------------------------------

    async def start(self, data_dir: str) -> None:
        """Connect + ensure schema. Idempotent."""
        if self._db is not None:
            return
        self._db_path = Path(data_dir) / "instance.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(
            str(self._db_path), isolation_level=None,
        )
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute(_INVARIANT_DDL)
        await self._db.execute(_FRICTION_PATTERN_INVARIANT_DDL)
        await self._db.execute(_CLOSURE_ATTEMPT_DDL)
        await self._db.execute(_CLOSURE_ATTEMPT_PENDING_UNIQ)
        await self._db.execute(_CLOSURE_ATTEMPT_IDX_PATTERN)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # --- Invariant CRUD -----------------------------------------------

    async def insert_invariant(
        self,
        *,
        instance_id: str,
        invariant_id: str,
        statement: str,
        owner: str,
        status: str = "active",
    ) -> None:
        """Insert a new invariant. Raises IntegrityError on duplicate
        (instance_id, invariant_id)."""
        assert self._db is not None, "ClosureStore not started"
        if owner not in VALID_INVARIANT_OWNERS:
            raise ClosureStoreError(
                f"invalid invariant owner {owner!r}; "
                f"must be one of {sorted(VALID_INVARIANT_OWNERS)}"
            )
        if status not in VALID_INVARIANT_STATUSES:
            raise ClosureStoreError(
                f"invalid invariant status {status!r}; "
                f"must be one of {sorted(VALID_INVARIANT_STATUSES)}"
            )
        now = utc_now()
        async with self._write_lock:
            await self._db.execute(
                """
                INSERT INTO invariant (
                    instance_id, invariant_id, statement, owner,
                    status, created_at, last_edited
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (instance_id, invariant_id, statement, owner,
                 status, now, now),
            )

    async def upsert_invariant(
        self,
        *,
        instance_id: str,
        invariant_id: str,
        statement: str,
        owner: str,
        status: str = "active",
    ) -> bool:
        """Insert or update. Returns True if newly created, False if
        an existing row was updated. Used by seed paths."""
        assert self._db is not None, "ClosureStore not started"
        existing = await self.get_invariant(
            instance_id=instance_id, invariant_id=invariant_id,
        )
        if existing is None:
            await self.insert_invariant(
                instance_id=instance_id,
                invariant_id=invariant_id,
                statement=statement,
                owner=owner,
                status=status,
            )
            return True
        now = utc_now()
        async with self._write_lock:
            await self._db.execute(
                """
                UPDATE invariant
                SET statement=?, owner=?, status=?, last_edited=?
                WHERE instance_id=? AND invariant_id=?
                """,
                (statement, owner, status, now,
                 instance_id, invariant_id),
            )
        return False

    async def get_invariant(
        self, *, instance_id: str, invariant_id: str,
    ) -> dict | None:
        assert self._db is not None, "ClosureStore not started"
        async with self._db.execute(
            "SELECT * FROM invariant WHERE instance_id=? AND invariant_id=?",
            (instance_id, invariant_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return dict(row)

    # --- Link table ----------------------------------------------------

    async def insert_link(
        self,
        *,
        instance_id: str,
        pattern_id: str,
        invariant_id: str,
        relation: str = "violates",
    ) -> None:
        """Link a friction pattern to an invariant. FK-enforced —
        raises IntegrityError if either side doesn't exist."""
        assert self._db is not None, "ClosureStore not started"
        now = utc_now()
        async with self._write_lock:
            await self._db.execute(
                """
                INSERT INTO friction_pattern_invariant (
                    instance_id, pattern_id, invariant_id, relation,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (instance_id, pattern_id, invariant_id, relation, now),
            )

    async def link_exists(
        self,
        *,
        instance_id: str,
        pattern_id: str,
        invariant_id: str,
    ) -> bool:
        """Return True if at least one link row exists (any relation)
        for this (instance, pattern, invariant) triple."""
        assert self._db is not None, "ClosureStore not started"
        async with self._db.execute(
            """
            SELECT 1 FROM friction_pattern_invariant
            WHERE instance_id=? AND pattern_id=? AND invariant_id=?
            LIMIT 1
            """,
            (instance_id, pattern_id, invariant_id),
        ) as cur:
            return (await cur.fetchone()) is not None

    async def list_invariants_for_pattern(
        self, *, instance_id: str, pattern_id: str,
    ) -> list[str]:
        """Return invariant_ids linked to this pattern, ordered
        ASC (deterministic — the workflow refs the first as
        ``primary_invariant_id``)."""
        assert self._db is not None, "ClosureStore not started"
        async with self._db.execute(
            """
            SELECT DISTINCT invariant_id
            FROM friction_pattern_invariant
            WHERE instance_id=? AND pattern_id=?
            ORDER BY invariant_id ASC
            """,
            (instance_id, pattern_id),
        ) as cur:
            rows = await cur.fetchall()
        return [r["invariant_id"] for r in rows]

    # --- ClosureAttempt CRUD ------------------------------------------

    async def insert_closure_attempt(
        self,
        *,
        instance_id: str,
        closure_id: str,
        pattern_id: str,
        invariant_id: str,
        active_epoch: int,
        route: str,
        route_payload: dict,
        probe_kind: str,
        probe_payload: dict,
        probe_payload_version: int,
    ) -> None:
        """Insert a new ClosureAttempt row with outcome='pending'."""
        assert self._db is not None, "ClosureStore not started"
        now = utc_now()
        async with self._write_lock:
            await self._db.execute(
                """
                INSERT INTO closure_attempt (
                    instance_id, closure_id, pattern_id, invariant_id,
                    active_epoch, route, route_payload_json,
                    probe_kind, probe_payload_json,
                    probe_payload_version, outcome,
                    outcome_evidence_json, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    instance_id, closure_id, pattern_id, invariant_id,
                    active_epoch, route,
                    json.dumps(route_payload, sort_keys=True),
                    probe_kind,
                    json.dumps(probe_payload, sort_keys=True),
                    probe_payload_version, OUTCOME_PENDING,
                    "{}", now,
                ),
            )

    async def get_closure_attempt(
        self, *, instance_id: str, closure_id: str,
    ) -> dict | None:
        assert self._db is not None, "ClosureStore not started"
        async with self._db.execute(
            "SELECT * FROM closure_attempt WHERE instance_id=? AND closure_id=?",
            (instance_id, closure_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return dict(row)

    async def get_pending_closure_for_episode(
        self,
        *,
        instance_id: str,
        pattern_id: str,
        invariant_id: str,
        active_epoch: int,
    ) -> dict | None:
        """Return the pending ClosureAttempt for this episode, or
        None if none exists. Used by ``record_closure_attempt`` for
        idempotent retry semantics."""
        assert self._db is not None, "ClosureStore not started"
        async with self._db.execute(
            """
            SELECT * FROM closure_attempt
            WHERE instance_id=? AND pattern_id=? AND invariant_id=?
              AND active_epoch=? AND outcome='pending'
            LIMIT 1
            """,
            (instance_id, pattern_id, invariant_id, active_epoch),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return dict(row)

    async def update_closure_outcome(
        self,
        *,
        instance_id: str,
        closure_id: str,
        outcome: str,
        evidence: dict,
    ) -> None:
        """Update a closure's outcome + evidence + completed_at.
        Validates outcome is one of the terminal states."""
        assert self._db is not None, "ClosureStore not started"
        if outcome not in (OUTCOME_PASSED, OUTCOME_FAILED, OUTCOME_ABORTED):
            raise ClosureStoreError(
                f"update_closure_outcome requires terminal outcome; "
                f"got {outcome!r}"
            )
        now = utc_now()
        async with self._write_lock:
            await self._db.execute(
                """
                UPDATE closure_attempt
                SET outcome=?, outcome_evidence_json=?, completed_at=?
                WHERE instance_id=? AND closure_id=?
                """,
                (
                    outcome, json.dumps(evidence, sort_keys=True),
                    now, instance_id, closure_id,
                ),
            )


# ---------------------------------------------------------------------------
# Kernel-tool entry points (called from both ReasoningService
# dispatch and the workflow autonomy adapter).
# ---------------------------------------------------------------------------


async def record_closure_attempt(
    *,
    store: ClosureStore,
    instance_id: str,
    pattern_id: str,
    invariant_id: str,
    active_epoch: int,
    route: str,
    route_payload: dict,
    probe_kind: str,
    probe_payload: dict,
    probe_payload_version: int,
) -> dict:
    """Insert a ClosureAttempt row with outcome='pending'.

    AC4 — Idempotent on (instance_id, pattern_id, invariant_id,
    active_epoch): if a pending row already exists for this key,
    return that row's closure_id rather than insert.

    AC13 — Rejects with ``InvariantNotLinkedToPattern`` if no row
    exists in ``friction_pattern_invariant`` for
    (instance_id, pattern_id, invariant_id).

    AC8 — Hard-rejects ``probe_kind`` not in
    :data:`READ_ONLY_PROBE_KINDS`.

    Returns: ``{"closure_id": str, "newly_created": bool}``.
    """
    if probe_kind not in READ_ONLY_PROBE_KINDS:
        raise ProbeKindNotAllowed(
            f"record_closure_attempt: probe_kind={probe_kind!r} "
            f"not in READ_ONLY_PROBE_KINDS={sorted(READ_ONLY_PROBE_KINDS)}"
        )
    if route not in ROUTE_CLASSES:
        raise ClosureStoreError(
            f"record_closure_attempt: route={route!r} "
            f"not in ROUTE_CLASSES={sorted(ROUTE_CLASSES)}"
        )

    # AC13: link must exist.
    linked = await store.link_exists(
        instance_id=instance_id,
        pattern_id=pattern_id,
        invariant_id=invariant_id,
    )
    if not linked:
        raise InvariantNotLinkedToPattern(
            f"record_closure_attempt: no friction_pattern_invariant "
            f"link row for instance_id={instance_id!r} "
            f"pattern_id={pattern_id!r} invariant_id={invariant_id!r}. "
            f"Closures cannot be created for unlinked pairs (AC13)."
        )

    # AC4: idempotent on (pattern, invariant, episode).
    existing = await store.get_pending_closure_for_episode(
        instance_id=instance_id,
        pattern_id=pattern_id,
        invariant_id=invariant_id,
        active_epoch=active_epoch,
    )
    if existing is not None:
        return {
            "closure_id": existing["closure_id"],
            "newly_created": False,
        }

    closure_id = _gen_closure_id()
    await store.insert_closure_attempt(
        instance_id=instance_id,
        closure_id=closure_id,
        pattern_id=pattern_id,
        invariant_id=invariant_id,
        active_epoch=active_epoch,
        route=route,
        route_payload=route_payload,
        probe_kind=probe_kind,
        probe_payload=probe_payload,
        probe_payload_version=probe_payload_version,
    )
    return {"closure_id": closure_id, "newly_created": True}


async def lookup_pattern_invariants(
    *,
    store: ClosureStore,
    instance_id: str,
    pattern_id: str,
) -> dict:
    """Return invariants linked to a pattern, in workflow-ref-
    friendly shape.

    Returns:
        ``{
            "has_invariants": bool,
            "primary_invariant_id": str,   # "" when has_invariants=False
            "all_invariant_ids": [str],
        }``

    The ref resolver walks dict keys only (no list indexing), so the
    workflow YAML references ``primary_invariant_id`` (scalar);
    ``all_invariant_ids`` is informational only.
    """
    invariants = await store.list_invariants_for_pattern(
        instance_id=instance_id, pattern_id=pattern_id,
    )
    return {
        "has_invariants": len(invariants) > 0,
        "primary_invariant_id": invariants[0] if invariants else "",
        "all_invariant_ids": invariants,
    }


# ProbeRunner protocol: takes (probe_payload, evidence_context) and
# returns (passed: bool, evidence: dict). Registered at bring-up.
ProbeRunner = Callable[[dict, dict], "asyncio.Future[tuple[bool, dict]]"]


_PROBE_RUNNERS: dict[str, Callable[[dict, dict], Any]] = {}


def register_probe_runner(
    probe_kind: str, runner: Callable[[dict, dict], Any],
) -> None:
    """Register a probe-handler for a probe_kind. Called from bring-up
    after substrate is constructed. ``runner`` may be sync or async
    (an awaitable result is awaited)."""
    if probe_kind not in READ_ONLY_PROBE_KINDS:
        raise ProbeKindNotAllowed(
            f"register_probe_runner: probe_kind={probe_kind!r} "
            f"not in READ_ONLY_PROBE_KINDS"
        )
    _PROBE_RUNNERS[probe_kind] = runner


def get_probe_runners() -> dict[str, Callable[[dict, dict], Any]]:
    """Return the live probe-runner registry (for tests + inspection)."""
    return dict(_PROBE_RUNNERS)


def clear_probe_runners() -> None:
    """Test helper: empty the registry."""
    _PROBE_RUNNERS.clear()


# ---------------------------------------------------------------------------
# Seed invariants (architect-curated)
# ---------------------------------------------------------------------------


TOOL_AVAILABILITY_HONESTY_INVARIANT_ID = "tool-availability-honesty"


TOOL_AVAILABILITY_HONESTY_STATEMENT = (
    "If the substrate's tool catalog registers a tool, that tool "
    "must be classifiable by the dispatch gate AND dispatchable "
    "through the kernel-tool execution path (or the MCP execution "
    "path for MCP-sourced tools). Tools present in the catalog "
    "but unclassifiable / unreachable represent a silent "
    "capability-claim vs callability divergence."
)


async def seed_v1_invariants(
    store: "ClosureStore", *, instance_id: str,
) -> dict[str, bool]:
    """Idempotent: insert the v1 architect-curated invariants for
    this instance. Returns ``{invariant_id: newly_created}``."""
    out: dict[str, bool] = {}
    out[TOOL_AVAILABILITY_HONESTY_INVARIANT_ID] = (
        await store.upsert_invariant(
            instance_id=instance_id,
            invariant_id=TOOL_AVAILABILITY_HONESTY_INVARIANT_ID,
            statement=TOOL_AVAILABILITY_HONESTY_STATEMENT,
            owner="architect",
            status="active",
        )
    )
    return out


# Architect-curated v1 friction_pattern_invariant link rows. Each
# entry says: "pattern X is a symptom of invariant Y being
# violated." Pattern seed lives in
# kernos.setup.seed_friction_patterns; invariant seed lives in
# seed_v1_invariants() above. The link binds the two.
_V1_PATTERN_INVARIANT_LINKS: tuple[tuple[str, str, str], ...] = (
    # (pattern_id, invariant_id, relation)
    (
        "capability-catalog-dispatch-divergence",
        TOOL_AVAILABILITY_HONESTY_INVARIANT_ID,
        "violates",
    ),
)


async def seed_v1_pattern_invariant_links(
    store: "ClosureStore", *, instance_id: str,
) -> dict[str, bool]:
    """Idempotent: insert v1 architect-curated friction_pattern_invariant
    link rows. Returns ``{(pattern_id, invariant_id, relation): newly_created}``
    flattened to string keys for log-friendliness.

    Skips silently when either side of the link doesn't exist yet
    (e.g. friction_pattern seed hasn't run, or pattern was
    excluded). Operators / future specs can add links manually
    via the link table once both sides exist.
    """
    out: dict[str, bool] = {}
    for pattern_id, invariant_id, relation in _V1_PATTERN_INVARIANT_LINKS:
        link_key = f"{pattern_id}::{invariant_id}::{relation}"
        if await store.link_exists(
            instance_id=instance_id,
            pattern_id=pattern_id,
            invariant_id=invariant_id,
        ):
            out[link_key] = False
            continue
        try:
            await store.insert_link(
                instance_id=instance_id,
                pattern_id=pattern_id,
                invariant_id=invariant_id,
                relation=relation,
            )
            out[link_key] = True
        except aiosqlite.IntegrityError:
            # FK violation: pattern or invariant doesn't exist yet
            # in this instance. Skip — operator can add the link
            # manually once both sides are present.
            out[link_key] = False
    return out


# ---------------------------------------------------------------------------
# Probe handlers
# ---------------------------------------------------------------------------


def build_tool_availability_honesty_probe(
    *,
    tool_catalog: Any,
    dispatch_gate: Any,
    get_dispatchable_kernel_tools: Callable[[], set[str]],
) -> Callable[[dict, dict], tuple[bool, dict]]:
    """Build the deterministic_introspection probe handler for the
    Tool Availability Honesty invariant.

    Walks every entry in ``tool_catalog.get_all()`` and verifies:
        (a) ``dispatch_gate.classify_tool_effect(name)`` != "unknown"
        (b) kernel-source tools are in
            ``get_dispatchable_kernel_tools()``; MCP-source tools
            have ``source == "mcp"`` or ``source.startswith("mcp:")``.

    Any entry failing either check is a divergence. Pure in-memory
    enumeration: no network, no subprocess, no SQLite writes.

    Returns a probe runner with signature
    ``(probe_payload: dict, evidence_context: dict) -> (bool, dict)``.
    """

    def _runner(payload: dict, ctx: dict) -> tuple[bool, dict]:
        try:
            entries = tool_catalog.get_all()
        except Exception as exc:
            return False, {
                "error": f"catalog_get_all_raised: {type(exc).__name__}: {exc}",
                "divergent_tools": [],
                "checked_count": 0,
            }
        dispatchable = get_dispatchable_kernel_tools()
        divergent: list[dict] = []
        checked = 0
        for entry in entries:
            checked += 1
            name = getattr(entry, "name", None) or (
                entry.get("name", "") if isinstance(entry, dict) else ""
            )
            source = getattr(entry, "source", None) or (
                entry.get("source", "")
                if isinstance(entry, dict) else ""
            )
            if not name:
                continue  # skip nameless rows (shouldn't happen)
            classification = dispatch_gate.classify_tool_effect(
                name, None, None,
            )
            if classification == "unknown":
                divergent.append({
                    "name": name,
                    "source": source,
                    "reason": "gate_classify_unknown",
                })
                continue
            # Source-based dispatchability check.
            if source == "mcp" or (
                isinstance(source, str) and source.startswith("mcp:")
            ):
                continue  # MCP-routed; not in kernel-dispatch set
            if name not in dispatchable:
                divergent.append({
                    "name": name,
                    "source": source,
                    "reason": (
                        "not_in_dispatchable_kernel_tools "
                        "(handler-branch missing or registry drift)"
                    ),
                })
        return (len(divergent) == 0), {
            "checked_count": checked,
            "divergent_tools": divergent,
            "divergent_count": len(divergent),
        }

    return _runner


# ---------------------------------------------------------------------------
# Tool schemas (agent-facing surface for the three closure tools).
# Wired into the registrar at kernos.kernel.kernel_tool_registry.
# ---------------------------------------------------------------------------


LOOKUP_PATTERN_INVARIANTS_TOOL: dict = {
    "name": "lookup_pattern_invariants",
    "description": (
        "Return the invariants linked to a friction pattern. "
        "Read-only. Returns has_invariants (bool), "
        "primary_invariant_id (str, first by ASC ordering or "
        "empty), and all_invariant_ids (list). Used by the "
        "self_improvement workflow to branch between the "
        "closure path (when invariants linked) and the legacy "
        "fallback (when none)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern_id": {
                "type": "string",
                "description": "Friction pattern_id to look up.",
            },
        },
        "required": ["pattern_id"],
        "additionalProperties": False,
    },
}


RECORD_CLOSURE_ATTEMPT_TOOL: dict = {
    "name": "record_closure_attempt",
    "description": (
        "Insert a ClosureAttempt row with outcome='pending'. "
        "Idempotent on (instance_id, pattern_id, invariant_id, "
        "active_epoch) — returns the existing closure_id when a "
        "pending row already exists. Rejects unlinked "
        "(pattern, invariant) pairs (the link table is the "
        "single source of truth) and probe_kinds outside "
        "READ_ONLY_PROBE_KINDS."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern_id": {"type": "string"},
            "invariant_id": {"type": "string"},
            "active_epoch": {"type": "integer"},
            "route": {
                "type": "string",
                "description": (
                    "ROUTE_CLASSES member; v1 only "
                    "code_change_via_cc is implemented."
                ),
            },
            "route_payload": {"type": "object"},
            "probe_kind": {
                "type": "string",
                "description": (
                    "READ_ONLY_PROBE_KINDS member; v1 only "
                    "deterministic_introspection."
                ),
            },
            "probe_payload": {"type": "object"},
            "probe_payload_version": {"type": "integer"},
        },
        "required": [
            "pattern_id", "invariant_id", "active_epoch",
            "route", "probe_kind", "probe_payload_version",
        ],
        "additionalProperties": False,
    },
}


RUN_CLOSURE_PROBE_TOOL: dict = {
    "name": "run_closure_probe",
    "description": (
        "Execute the stored probe for a ClosureAttempt. "
        "Idempotent replay: when the attempt's outcome is NOT "
        "'pending', returns the stored outcome + evidence with "
        "replayed=True without re-running the probe. On probe "
        "pass: transitions the friction pattern to 'resolved'. "
        "On probe fail: pattern stays in current state; emits "
        "closure.probe_failed with structured evidence."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "closure_id": {
                "type": "string",
                "description": (
                    "closure_id returned by "
                    "record_closure_attempt."
                ),
            },
        },
        "required": ["closure_id"],
        "additionalProperties": False,
    },
}


async def run_closure_probe(
    *,
    store: ClosureStore,
    instance_id: str,
    closure_id: str,
    pattern_transition_fn: Optional[Callable[..., Any]] = None,
    event_emit_fn: Optional[Callable[..., Any]] = None,
) -> dict:
    """Execute the stored probe for a ClosureAttempt.

    AC14 — IDEMPOTENT REPLAY: if the row's outcome is NOT 'pending',
    return the stored outcome + evidence WITHOUT re-running the
    probe, re-transitioning the pattern, or re-emitting
    closure.probe_failed.

    On outcome='pending':
        Reads probe_kind + probe_payload, dispatches to the
        registered handler.
        On pass: outcome='passed', completed_at, evidence;
            calls ``pattern_transition_fn`` (if supplied) to
            transition the friction pattern to 'resolved'.
        On fail: outcome='failed', completed_at, evidence;
            pattern stays in current state;
            calls ``event_emit_fn`` (if supplied) to emit
            closure.probe_failed.

    AC8 — Hard-rejects if stored probe_kind not in
    :data:`READ_ONLY_PROBE_KINDS`.

    Returns: ``{"outcome": str, "evidence": dict, "replayed": bool}``.
    """
    row = await store.get_closure_attempt(
        instance_id=instance_id, closure_id=closure_id,
    )
    if row is None:
        raise ClosureAttemptNotFound(
            f"run_closure_probe: no closure_attempt row for "
            f"instance_id={instance_id!r} closure_id={closure_id!r}"
        )

    # AC14: idempotent replay.
    if row["outcome"] != OUTCOME_PENDING:
        try:
            evidence = json.loads(row["outcome_evidence_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            evidence = {}
        return {
            "outcome": row["outcome"],
            "evidence": evidence,
            "replayed": True,
        }

    probe_kind = row["probe_kind"]
    if probe_kind not in READ_ONLY_PROBE_KINDS:
        raise ProbeKindNotAllowed(
            f"run_closure_probe: stored probe_kind={probe_kind!r} "
            f"not in READ_ONLY_PROBE_KINDS={sorted(READ_ONLY_PROBE_KINDS)}"
        )

    runner = _PROBE_RUNNERS.get(probe_kind)
    if runner is None:
        # No runner registered — treat as infrastructure error
        # rather than a probe failure. Substrate misconfiguration.
        raise ClosureStoreError(
            f"run_closure_probe: no probe runner registered for "
            f"probe_kind={probe_kind!r}; registry has "
            f"{sorted(_PROBE_RUNNERS.keys())}"
        )

    try:
        probe_payload = json.loads(row["probe_payload_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        probe_payload = {}
    evidence_context = {
        "instance_id": instance_id,
        "closure_id": closure_id,
        "pattern_id": row["pattern_id"],
        "invariant_id": row["invariant_id"],
        "active_epoch": row["active_epoch"],
    }

    result = runner(probe_payload, evidence_context)
    if asyncio.iscoroutine(result):
        result = await result
    passed, evidence = result

    terminal_outcome = OUTCOME_PASSED if passed else OUTCOME_FAILED
    await store.update_closure_outcome(
        instance_id=instance_id,
        closure_id=closure_id,
        outcome=terminal_outcome,
        evidence=evidence,
    )

    if passed and pattern_transition_fn is not None:
        try:
            transition_result = pattern_transition_fn(
                instance_id=instance_id,
                pattern_id=row["pattern_id"],
                new_state="resolved",
                resolved_by_spec="self_improvement_closure",
            )
            if asyncio.iscoroutine(transition_result):
                await transition_result
        except Exception as exc:
            logger.warning(
                "CLOSURE_PATTERN_TRANSITION_FAILED closure_id=%s "
                "pattern_id=%s error=%s",
                closure_id, row["pattern_id"], exc,
            )
    elif (not passed) and event_emit_fn is not None:
        try:
            emit_result = event_emit_fn(
                instance_id=instance_id,
                event_type="closure.probe_failed",
                payload={
                    "pattern_id": row["pattern_id"],
                    "invariant_id": row["invariant_id"],
                    "closure_id": closure_id,
                    "active_epoch": row["active_epoch"],
                    "evidence": evidence,
                },
            )
            if asyncio.iscoroutine(emit_result):
                await emit_result
        except Exception as exc:
            logger.warning(
                "CLOSURE_PROBE_FAILED_EVENT_EMIT_FAILED closure_id=%s "
                "error=%s",
                closure_id, exc,
            )

    return {
        "outcome": terminal_outcome,
        "evidence": evidence,
        "replayed": False,
    }
