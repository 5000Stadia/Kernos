"""Catalog substrate — REFERENCE-PRIMITIVE-V1.

Live structured state over reference source files on disk. Append-
only-style ledger semantics (history, supersession-as-first-class)
don't earn their cost for read-mostly canonical content; the
catalog's correctness comes from "catalog matches source" verifiable
via hash.

Audit trail still exists at the event-stream level — see
:mod:`kernos.kernel.reference.events`. The catalog is the runtime
query surface; event stream is for replay + observability.

Connection model: opens its own :mod:`aiosqlite` connection to
``instance.db``, separate from event_stream / instance_db / etc per
the per-module-isolation pattern. ``isolation_level=None`` so
explicit BEGIN/COMMIT transactions are caller-controlled — used by
file-level rebuild to enforce transactional drop-and-rebuild.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


SCOPE_INSTANCE = "instance"
"""Scope value for ``docs/``-derived rows. Visible to every domain."""


def scope_for_domain(domain_id: str) -> str:
    """Return the scope discriminator for a domain-owned entry."""
    if not domain_id:
        raise ValueError("domain_id is required for domain-scoped entries")
    return f"domain:{domain_id}"


def parse_domain_from_scope(scope: str) -> str | None:
    """Return the ``domain_id`` if scope is ``domain:<X>``, else None."""
    if scope.startswith("domain:"):
        return scope[len("domain:") :]
    return None


# Trust tiers — first-class in the catalog (spec §"Trust tiers").
TRUST_CANONICAL = "canonical"
TRUST_AGENT_AUTHORED = "agent_authored"
TRUST_EXTERNAL_SNAPSHOT = "external_snapshot"
TRUST_QUARANTINED = "quarantined"

VALID_TRUST_TIERS: frozenset[str] = frozenset({
    TRUST_CANONICAL,
    TRUST_AGENT_AUTHORED,
    TRUST_EXTERNAL_SNAPSHOT,
    TRUST_QUARANTINED,
})


# Entry types — file-level vs collection-level.
ENTRY_TYPE_FILE = "file"
ENTRY_TYPE_COLLECTION = "collection"

VALID_ENTRY_TYPES: frozenset[str] = frozenset({
    ENTRY_TYPE_FILE,
    ENTRY_TYPE_COLLECTION,
})


# Refresh-policy sentinels for collection-level entries.
REFRESH_POLICY_SNAPSHOT = "snapshot"
REFRESH_POLICY_REFRESHABLE = "refreshable"
# ``expires_after_N_days`` is dynamic; v1 records the metadata but the
# automation is V2 (per §"Out of scope for V1").


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CatalogStoreError(Exception):
    """Base for catalog-level errors."""


class UnknownEntry(CatalogStoreError):
    """No catalog row for the given ``entry_id``."""


class InvalidTrustTier(CatalogStoreError):
    """Trust tier is not in :data:`VALID_TRUST_TIERS`."""


class InvalidEntryType(CatalogStoreError):
    """Entry type is not in :data:`VALID_ENTRY_TYPES`."""


# ---------------------------------------------------------------------------
# CatalogEntry dataclass
# ---------------------------------------------------------------------------


@dataclass
class CatalogEntry:
    """A single catalog row.

    File-level entries cover one section in one file. Collection-
    level entries surface a collection's purpose + member-file map
    (no content payload — that's still in the underlying member
    files, cataloged independently as file-level entries).
    """

    entry_id: str
    instance_id: str
    entry_type: str  # ENTRY_TYPE_FILE | ENTRY_TYPE_COLLECTION
    scope: str  # 'instance' | 'domain:<space_id>'
    category: str  # docs/ subfolder OR collection name
    indexed_at: str
    trust_tier: str = TRUST_CANONICAL
    auto_inducible: bool = True
    provenance_metadata: dict[str, Any] = field(default_factory=dict)

    # File-level
    file_path: str = ""
    section_title: str = ""
    one_line: str = ""
    line_start: int = 0
    line_end: int = 0
    source_hash: str = ""
    collection_back_reference: str = ""

    # Collection-level
    collection_name: str = ""
    purpose: str = ""
    refresh_policy: str = ""
    member_file_count: int = 0
    member_file_paths: list[str] = field(default_factory=list)
    last_refreshed_at: str = ""

    # Future-proof / scope-related
    owner_domain_id: str = ""
    promoted_from: str = ""
    docs_version: str = ""

    # Tombstone
    tombstoned: bool = False
    tombstoned_at: str = ""
    tombstoned_reason: str = ""

    def __post_init__(self) -> None:
        if self.entry_type not in VALID_ENTRY_TYPES:
            raise InvalidEntryType(
                f"entry_type={self.entry_type!r}; expected one of {sorted(VALID_ENTRY_TYPES)}"
            )
        if self.trust_tier not in VALID_TRUST_TIERS:
            raise InvalidTrustTier(
                f"trust_tier={self.trust_tier!r}; expected one of {sorted(VALID_TRUST_TIERS)}"
            )


# ---------------------------------------------------------------------------
# Hash helper
# ---------------------------------------------------------------------------


def compute_source_hash(content: bytes) -> str:
    """Stable SHA-256 hex digest of file content bytes."""
    return hashlib.sha256(content).hexdigest()


def compute_file_hash(file_path: Path | str) -> str:
    """Read a file and return its SHA-256 hex digest."""
    path = Path(file_path)
    return compute_source_hash(path.read_bytes())


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_REFERENCE_CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS reference_catalog (
    entry_id                  TEXT PRIMARY KEY,
    instance_id               TEXT NOT NULL,
    entry_type                TEXT NOT NULL,
    scope                     TEXT NOT NULL,
    category                  TEXT NOT NULL DEFAULT '',
    indexed_at                TEXT NOT NULL,
    trust_tier                TEXT NOT NULL DEFAULT 'canonical',
    auto_inducible            INTEGER NOT NULL DEFAULT 1,
    provenance_metadata       TEXT NOT NULL DEFAULT '{}',
    -- File-level fields
    file_path                 TEXT NOT NULL DEFAULT '',
    section_title             TEXT NOT NULL DEFAULT '',
    one_line                  TEXT NOT NULL DEFAULT '',
    line_start                INTEGER NOT NULL DEFAULT 0,
    line_end                  INTEGER NOT NULL DEFAULT 0,
    source_hash               TEXT NOT NULL DEFAULT '',
    collection_back_reference TEXT NOT NULL DEFAULT '',
    -- Collection-level fields
    collection_name           TEXT NOT NULL DEFAULT '',
    purpose                   TEXT NOT NULL DEFAULT '',
    refresh_policy            TEXT NOT NULL DEFAULT '',
    member_file_count         INTEGER NOT NULL DEFAULT 0,
    member_file_paths         TEXT NOT NULL DEFAULT '[]',
    last_refreshed_at         TEXT NOT NULL DEFAULT '',
    -- Future-proof / scope-related
    owner_domain_id           TEXT NOT NULL DEFAULT '',
    promoted_from             TEXT NOT NULL DEFAULT '',
    docs_version              TEXT NOT NULL DEFAULT '',
    -- Tombstone
    tombstoned                INTEGER NOT NULL DEFAULT 0,
    tombstoned_at             TEXT NOT NULL DEFAULT '',
    tombstoned_reason         TEXT NOT NULL DEFAULT '',
    CHECK (entry_type IN ('file', 'collection')),
    CHECK (trust_tier IN ('canonical', 'agent_authored',
                          'external_snapshot', 'quarantined'))
)
"""

_REFERENCE_CATALOG_INDEX_LOOKUP = """
CREATE INDEX IF NOT EXISTS idx_ref_catalog_visible
    ON reference_catalog (instance_id, scope, tombstoned)
"""

_REFERENCE_CATALOG_INDEX_FILE = """
CREATE INDEX IF NOT EXISTS idx_ref_catalog_file
    ON reference_catalog (instance_id, file_path, entry_type)
"""

_REFERENCE_CATALOG_INDEX_COLLECTION = """
CREATE INDEX IF NOT EXISTS idx_ref_catalog_collection
    ON reference_catalog (instance_id, collection_name, entry_type)
"""


# ---------------------------------------------------------------------------
# Row I/O
# ---------------------------------------------------------------------------


def _row_to_entry(row: aiosqlite.Row) -> CatalogEntry:
    try:
        provenance = json.loads(row["provenance_metadata"]) if row["provenance_metadata"] else {}
    except Exception:
        provenance = {}
    try:
        member_file_paths = json.loads(row["member_file_paths"]) if row["member_file_paths"] else []
    except Exception:
        member_file_paths = []
    return CatalogEntry(
        entry_id=row["entry_id"],
        instance_id=row["instance_id"],
        entry_type=row["entry_type"],
        scope=row["scope"],
        category=row["category"],
        indexed_at=row["indexed_at"],
        trust_tier=row["trust_tier"],
        auto_inducible=bool(row["auto_inducible"]),
        provenance_metadata=provenance,
        file_path=row["file_path"],
        section_title=row["section_title"],
        one_line=row["one_line"],
        line_start=row["line_start"],
        line_end=row["line_end"],
        source_hash=row["source_hash"],
        collection_back_reference=row["collection_back_reference"],
        collection_name=row["collection_name"],
        purpose=row["purpose"],
        refresh_policy=row["refresh_policy"],
        member_file_count=row["member_file_count"],
        member_file_paths=member_file_paths,
        last_refreshed_at=row["last_refreshed_at"],
        owner_domain_id=row["owner_domain_id"],
        promoted_from=row["promoted_from"],
        docs_version=row["docs_version"],
        tombstoned=bool(row["tombstoned"]),
        tombstoned_at=row["tombstoned_at"],
        tombstoned_reason=row["tombstoned_reason"],
    )


def _entry_to_insert_params(entry: CatalogEntry) -> tuple:
    return (
        entry.entry_id,
        entry.instance_id,
        entry.entry_type,
        entry.scope,
        entry.category,
        entry.indexed_at,
        entry.trust_tier,
        1 if entry.auto_inducible else 0,
        json.dumps(entry.provenance_metadata),
        entry.file_path,
        entry.section_title,
        entry.one_line,
        entry.line_start,
        entry.line_end,
        entry.source_hash,
        entry.collection_back_reference,
        entry.collection_name,
        entry.purpose,
        entry.refresh_policy,
        entry.member_file_count,
        json.dumps(entry.member_file_paths),
        entry.last_refreshed_at,
        entry.owner_domain_id,
        entry.promoted_from,
        entry.docs_version,
        1 if entry.tombstoned else 0,
        entry.tombstoned_at,
        entry.tombstoned_reason,
    )


_INSERT_COLUMNS = (
    "entry_id, instance_id, entry_type, scope, category, indexed_at, "
    "trust_tier, auto_inducible, provenance_metadata, "
    "file_path, section_title, one_line, line_start, line_end, "
    "source_hash, collection_back_reference, "
    "collection_name, purpose, refresh_policy, member_file_count, "
    "member_file_paths, last_refreshed_at, "
    "owner_domain_id, promoted_from, docs_version, "
    "tombstoned, tombstoned_at, tombstoned_reason"
)
_INSERT_PLACEHOLDERS = ",".join(["?"] * 28)


# ---------------------------------------------------------------------------
# CatalogStore
# ---------------------------------------------------------------------------


def _new_entry_id() -> str:
    return f"ref_{uuid.uuid4().hex[:16]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CatalogStore:
    """SQLite-backed durable store for the reference catalog.

    Owns the ``reference_catalog`` table on ``instance.db``.
    Concurrent reads supported (WAL); writes serialize via the
    asyncio lock + SQLite's single-writer model.
    """

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._write_lock = asyncio.Lock()

    # --- Lifecycle --------------------------------------------------

    async def start(self, data_dir: str) -> None:
        if self._db is not None:
            return  # idempotent
        self._db_path = Path(data_dir) / "instance.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(
            str(self._db_path), isolation_level=None,
        )
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute(_REFERENCE_CATALOG_DDL)
        await self._db.execute(_REFERENCE_CATALOG_INDEX_LOOKUP)
        await self._db.execute(_REFERENCE_CATALOG_INDEX_FILE)
        await self._db.execute(_REFERENCE_CATALOG_INDEX_COLLECTION)

    async def stop(self) -> None:
        if self._db is None:
            return
        try:
            await self._db.close()
        finally:
            self._db = None

    # --- Read paths -------------------------------------------------

    async def get_entry(self, *, entry_id: str) -> CatalogEntry | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM reference_catalog WHERE entry_id = ?", (entry_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_entry(row) if row else None

    async def list_entries_for_file(
        self,
        *,
        instance_id: str,
        file_path: str,
        include_tombstoned: bool = False,
    ) -> list[CatalogEntry]:
        assert self._db is not None
        sql = (
            "SELECT * FROM reference_catalog "
            "WHERE instance_id = ? AND file_path = ? AND entry_type = 'file'"
        )
        params: list[Any] = [instance_id, file_path]
        if not include_tombstoned:
            sql += " AND tombstoned = 0"
        sql += " ORDER BY line_start ASC"
        async with self._db.execute(sql, tuple(params)) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_entry(r) for r in rows]

    async def get_source_hash(
        self,
        *,
        instance_id: str,
        file_path: str,
    ) -> str | None:
        """Return the recorded source_hash for the file (any of its
        entries; all entries for the same file share a hash). Returns
        None if no entries exist for the file."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT source_hash FROM reference_catalog "
            "WHERE instance_id = ? AND file_path = ? AND entry_type = 'file' "
            "AND tombstoned = 0 LIMIT 1",
            (instance_id, file_path),
        ) as cursor:
            row = await cursor.fetchone()
        return row["source_hash"] if row else None

    async def list_visible(
        self,
        *,
        instance_id: str,
        domain_id: str,
        include_quarantined: bool = True,
        entry_type: str | None = None,
    ) -> list[CatalogEntry]:
        """Visibility rule: scope == 'instance' OR scope == 'domain:<X>'
        with X bound to the caller's identity. Tombstoned entries are
        excluded; quarantined entries surface only via explicit
        retrieval (caller may filter by trust_tier).
        """
        assert self._db is not None
        sql = (
            "SELECT * FROM reference_catalog "
            "WHERE instance_id = ? AND tombstoned = 0 "
            "AND (scope = 'instance' OR scope = ?)"
        )
        params: list[Any] = [instance_id, scope_for_domain(domain_id)] if domain_id else [
            instance_id, "domain:__never_matches__",
        ]
        if not include_quarantined:
            sql += " AND trust_tier != 'quarantined'"
        if entry_type:
            sql += " AND entry_type = ?"
            params.append(entry_type)
        sql += " ORDER BY indexed_at DESC"
        async with self._db.execute(sql, tuple(params)) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_entry(r) for r in rows]

    async def list_all_files(
        self, *, instance_id: str,
    ) -> list[tuple[str, str, str]]:
        """Return ``(file_path, source_hash, scope)`` for every file
        with at least one non-tombstoned entry. Used by the per-turn
        ingestion check to detect deletions and recompute hashes."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT DISTINCT file_path, source_hash, scope "
            "FROM reference_catalog "
            "WHERE instance_id = ? AND entry_type = 'file' "
            "AND tombstoned = 0 AND file_path != '' ",
            (instance_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(r["file_path"], r["source_hash"], r["scope"]) for r in rows]

    async def get_collection_entry(
        self, *, instance_id: str, collection_name: str, scope: str,
    ) -> CatalogEntry | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM reference_catalog "
            "WHERE instance_id = ? AND collection_name = ? "
            "AND scope = ? AND entry_type = 'collection' "
            "AND tombstoned = 0 LIMIT 1",
            (instance_id, collection_name, scope),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_entry(row) if row else None

    # --- Write paths ------------------------------------------------

    async def replace_file_entries(
        self,
        *,
        instance_id: str,
        file_path: str,
        new_entries: list[CatalogEntry],
    ) -> None:
        """Transactional drop-and-rebuild for a single file.

        Either every existing file-level row for the file is removed
        and every new row is inserted, or nothing changes. The spec
        forbids partial cataloging — see §"File-level rebuild on hash
        change (transactional)".
        """
        assert self._db is not None
        for entry in new_entries:
            if entry.entry_type != ENTRY_TYPE_FILE:
                raise InvalidEntryType(
                    "replace_file_entries only accepts file-level entries"
                )
            if entry.file_path != file_path:
                raise CatalogStoreError(
                    f"entry.file_path={entry.file_path!r} != {file_path!r}"
                )
            if entry.instance_id != instance_id:
                raise CatalogStoreError("instance_id mismatch")
        async with self._write_lock:
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                await self._db.execute(
                    "DELETE FROM reference_catalog "
                    "WHERE instance_id = ? AND file_path = ? "
                    "AND entry_type = 'file'",
                    (instance_id, file_path),
                )
                for entry in new_entries:
                    await self._db.execute(
                        f"INSERT INTO reference_catalog ({_INSERT_COLUMNS}) "
                        f"VALUES ({_INSERT_PLACEHOLDERS})",
                        _entry_to_insert_params(entry),
                    )
                await self._db.execute("COMMIT")
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:  # pragma: no cover
                    pass
                raise

    async def upsert_collection_entry(
        self, *, entry: CatalogEntry,
    ) -> None:
        """Insert or replace a collection-level catalog entry."""
        assert self._db is not None
        if entry.entry_type != ENTRY_TYPE_COLLECTION:
            raise InvalidEntryType(
                "upsert_collection_entry only accepts collection-level entries"
            )
        async with self._write_lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                await self._db.execute(
                    "DELETE FROM reference_catalog "
                    "WHERE instance_id = ? AND collection_name = ? "
                    "AND scope = ? AND entry_type = 'collection'",
                    (entry.instance_id, entry.collection_name, entry.scope),
                )
                await self._db.execute(
                    f"INSERT INTO reference_catalog ({_INSERT_COLUMNS}) "
                    f"VALUES ({_INSERT_PLACEHOLDERS})",
                    _entry_to_insert_params(entry),
                )
                await self._db.execute("COMMIT")
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:  # pragma: no cover
                    pass
                raise

    async def tombstone_file(
        self,
        *,
        instance_id: str,
        file_path: str,
        reason: str = "",
    ) -> int:
        """Mark all entries for a file as tombstoned. Returns the row
        count tombstoned. No-op if the file has no entries."""
        assert self._db is not None
        now = _now_iso()
        async with self._write_lock:
            cursor = await self._db.execute(
                "UPDATE reference_catalog SET tombstoned = 1, "
                "tombstoned_at = ?, tombstoned_reason = ? "
                "WHERE instance_id = ? AND file_path = ? "
                "AND entry_type = 'file' AND tombstoned = 0",
                (now, reason, instance_id, file_path),
            )
            count = cursor.rowcount or 0
        return count

    async def quarantine_entry(
        self,
        *,
        entry_id: str,
        reason: str,
        quarantined_by: str,
    ) -> CatalogEntry:
        """Set ``trust_tier='quarantined'`` and stamp provenance.
        Original trust tier is preserved in ``provenance_metadata``
        under ``prior_trust_tier`` so it can be restored."""
        assert self._db is not None
        entry = await self.get_entry(entry_id=entry_id)
        if entry is None:
            raise UnknownEntry(f"entry_id={entry_id!r}")
        if entry.trust_tier == TRUST_QUARANTINED:
            return entry
        provenance = dict(entry.provenance_metadata)
        provenance["prior_trust_tier"] = entry.trust_tier
        provenance["quarantine_reason"] = reason
        provenance["quarantined_at"] = _now_iso()
        provenance["quarantined_by"] = quarantined_by
        async with self._write_lock:
            await self._db.execute(
                "UPDATE reference_catalog SET trust_tier = 'quarantined', "
                "auto_inducible = 0, provenance_metadata = ? "
                "WHERE entry_id = ?",
                (json.dumps(provenance), entry_id),
            )
        updated = await self.get_entry(entry_id=entry_id)
        assert updated is not None
        return updated

    async def restore_entry(
        self,
        *,
        entry_id: str,
        restored_by: str,
    ) -> tuple[CatalogEntry, str]:
        """Restore from quarantine to the prior trust tier. Returns
        ``(entry, prior_trust_tier)``."""
        assert self._db is not None
        entry = await self.get_entry(entry_id=entry_id)
        if entry is None:
            raise UnknownEntry(f"entry_id={entry_id!r}")
        if entry.trust_tier != TRUST_QUARANTINED:
            return entry, entry.trust_tier
        prior = entry.provenance_metadata.get("prior_trust_tier", TRUST_AGENT_AUTHORED)
        if prior not in VALID_TRUST_TIERS or prior == TRUST_QUARANTINED:
            prior = TRUST_AGENT_AUTHORED
        provenance = dict(entry.provenance_metadata)
        provenance.pop("prior_trust_tier", None)
        provenance["restored_at"] = _now_iso()
        provenance["restored_by"] = restored_by
        # auto_inducible follows trust tier defaults (see spec
        # §"Conservative defaults for v1"); restoration enables it
        # for canonical/agent_authored, leaves external_snapshot
        # under tighter threshold downstream.
        new_inducible = 1 if prior in {TRUST_CANONICAL, TRUST_AGENT_AUTHORED, TRUST_EXTERNAL_SNAPSHOT} else 0
        async with self._write_lock:
            await self._db.execute(
                "UPDATE reference_catalog SET trust_tier = ?, "
                "auto_inducible = ?, provenance_metadata = ? "
                "WHERE entry_id = ?",
                (prior, new_inducible, json.dumps(provenance), entry_id),
            )
        updated = await self.get_entry(entry_id=entry_id)
        assert updated is not None
        return updated, prior

    async def supersede(
        self,
        *,
        old_entry_id: str,
        new_entry_id: str,
        reason: str,
    ) -> None:
        """Tombstone the old entry and record the supersession link
        in its provenance. The new entry is independently created;
        this method only links + tombstones."""
        assert self._db is not None
        old_entry = await self.get_entry(entry_id=old_entry_id)
        if old_entry is None:
            raise UnknownEntry(f"old_entry_id={old_entry_id!r}")
        provenance = dict(old_entry.provenance_metadata)
        provenance["superseded_by"] = new_entry_id
        provenance["supersede_reason"] = reason
        provenance["superseded_at"] = _now_iso()
        async with self._write_lock:
            await self._db.execute(
                "UPDATE reference_catalog SET tombstoned = 1, "
                "tombstoned_at = ?, tombstoned_reason = ?, "
                "provenance_metadata = ? "
                "WHERE entry_id = ?",
                (
                    _now_iso(),
                    f"superseded_by:{new_entry_id}",
                    json.dumps(provenance),
                    old_entry_id,
                ),
            )

    async def mark_moved_to_canvas(
        self,
        *,
        entry_id: str,
        target_canvas: str,
        moved_by: str,
    ) -> CatalogEntry:
        """Tombstone an entry and stamp the canvas-target provenance.
        The actual file move is performed by the tool layer; this
        only updates the catalog row."""
        assert self._db is not None
        entry = await self.get_entry(entry_id=entry_id)
        if entry is None:
            raise UnknownEntry(f"entry_id={entry_id!r}")
        provenance = dict(entry.provenance_metadata)
        provenance["moved_to_canvas"] = target_canvas
        provenance["moved_at"] = _now_iso()
        provenance["moved_by"] = moved_by
        async with self._write_lock:
            await self._db.execute(
                "UPDATE reference_catalog SET tombstoned = 1, "
                "tombstoned_at = ?, tombstoned_reason = ?, "
                "provenance_metadata = ? "
                "WHERE entry_id = ?",
                (
                    _now_iso(),
                    f"moved_to_canvas:{target_canvas}",
                    json.dumps(provenance),
                    entry_id,
                ),
            )
        updated = await self.get_entry(entry_id=entry_id)
        assert updated is not None
        return updated


__all__ = [
    "CatalogEntry",
    "CatalogStore",
    "CatalogStoreError",
    "ENTRY_TYPE_COLLECTION",
    "ENTRY_TYPE_FILE",
    "InvalidEntryType",
    "InvalidTrustTier",
    "REFRESH_POLICY_REFRESHABLE",
    "REFRESH_POLICY_SNAPSHOT",
    "SCOPE_INSTANCE",
    "TRUST_AGENT_AUTHORED",
    "TRUST_CANONICAL",
    "TRUST_EXTERNAL_SNAPSHOT",
    "TRUST_QUARANTINED",
    "UnknownEntry",
    "VALID_ENTRY_TYPES",
    "VALID_TRUST_TIERS",
    "compute_file_hash",
    "compute_source_hash",
    "parse_domain_from_scope",
    "scope_for_domain",
]
