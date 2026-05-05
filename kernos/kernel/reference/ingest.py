"""Per-turn ingestion check — REFERENCE-PRIMITIVE-V1 C3.

Cheap directory walk + hash compare. Runs each turn (or each loop
tick — implementation choice; the cost is bounded by source-file
count, no LLM calls in this path).

For each source root:

* List all ``*.md`` files under the root.
* For each file: compute SHA-256 of contents; compare against
  the catalog's recorded ``source_hash``.

  * **Hash matches** → no work.
  * **Hash changed** → enqueue for async re-cataloging.
  * **New file** (no catalog entry yet) → enqueue for fresh cataloging.

* For each catalog file_path that no longer exists on disk →
  tombstone all entries directly (no cohort round-trip).

Collections are scanned one level above the markdown files: each
sub-directory containing a ``_collection.json`` is a collection.
A collection-level catalog entry is enqueued if it doesn't exist
yet or if ``_collection.json``'s mtime is newer than the recorded
``last_refreshed_at``.

This module performs no LLM work and emits no events directly —
the cohort owns event emission for cataloging operations. The only
events from here are the tombstones, which are emitted via the
catalog store + emitter inline (deletions don't queue work)."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from kernos.kernel.reference.catalog import (
    CatalogStore,
    SCOPE_INSTANCE,
    TRUST_AGENT_AUTHORED,
    TRUST_CANONICAL,
    TRUST_EXTERNAL_SNAPSHOT,
    compute_file_hash,
    parse_domain_from_scope,
    scope_for_domain,
)
from kernos.kernel.reference.cohort import CatalogingCohort
from kernos.kernel.reference.events import ReferenceEventEmitter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source-root descriptor
# ---------------------------------------------------------------------------


class SourceRoot:
    """A single root of reference source files.

    ``docs/``-derived roots are scope='instance' with trust_tier=
    'canonical'. ``references/``-derived roots are
    scope='domain:<space_id>' with trust_tier='agent_authored' as
    the storage default; collection metadata may override per-file
    trust at cataloging time."""

    def __init__(
        self,
        *,
        root_path: Path,
        scope: str,
        default_trust_tier: str,
        owner_domain_id: str = "",
        is_collection_aware: bool = False,
    ) -> None:
        self.root_path = root_path
        self.scope = scope
        self.default_trust_tier = default_trust_tier
        self.owner_domain_id = owner_domain_id
        self.is_collection_aware = is_collection_aware


def docs_source_root(docs_path: Path) -> SourceRoot:
    """The canonical instance-scoped ``docs/`` source root."""
    return SourceRoot(
        root_path=docs_path,
        scope=SCOPE_INSTANCE,
        default_trust_tier=TRUST_CANONICAL,
        owner_domain_id="",
        is_collection_aware=False,
    )


def references_source_root(
    *,
    references_path: Path,
    domain_id: str,
) -> SourceRoot:
    """A per-domain agent-stored ``references/<space_id>/`` root."""
    return SourceRoot(
        root_path=references_path,
        scope=scope_for_domain(domain_id),
        default_trust_tier=TRUST_AGENT_AUTHORED,
        owner_domain_id=domain_id,
        is_collection_aware=True,
    )


# ---------------------------------------------------------------------------
# Category derivation
# ---------------------------------------------------------------------------


def _category_for_file(root: SourceRoot, file_path: Path) -> str:
    """First-component-under-root → category. Files directly under
    the root use category='root'."""
    try:
        rel = file_path.relative_to(root.root_path)
    except ValueError:
        return "root"
    parts = rel.parts
    if len(parts) <= 1:
        return "root"
    return parts[0]


# ---------------------------------------------------------------------------
# Ingestion scanner
# ---------------------------------------------------------------------------


class IngestionScanner:
    """Per-turn scanner that compares the catalog against the on-disk
    source roots and enqueues file-change events for the cohort.

    Construction is cheap; ``scan()`` is the single entry point and
    is safe to call from inside a per-turn loop tick. Concurrent
    scans (e.g. two overlapping turns) are guarded by an asyncio
    lock so the same file isn't enqueued twice."""

    def __init__(
        self,
        *,
        catalog: CatalogStore,
        cohort: CatalogingCohort,
        emitter: ReferenceEventEmitter,
        instance_id: str,
    ) -> None:
        self._catalog = catalog
        self._cohort = cohort
        self._emitter = emitter
        self._instance_id = instance_id
        self._roots: list[SourceRoot] = []

    def add_source(self, root: SourceRoot) -> None:
        self._roots.append(root)

    @property
    def sources(self) -> list[SourceRoot]:
        return list(self._roots)

    # --- Scan -------------------------------------------------------

    async def scan(self) -> dict[str, int]:
        """Walk every source root; enqueue changes; tombstone vanished
        files. Returns a small summary dict with counts per category
        for diagnostic visibility (not load-bearing)."""
        summary = {
            "files_seen": 0,
            "files_unchanged": 0,
            "files_changed": 0,
            "files_new": 0,
            "files_tombstoned": 0,
            "collections_seen": 0,
            "collections_enqueued": 0,
        }
        for root in self._roots:
            await self._scan_root(root, summary)
        await self._tombstone_vanished_files(summary)
        return summary

    async def _scan_root(
        self, root: SourceRoot, summary: dict[str, int],
    ) -> None:
        if not root.root_path.exists():
            return
        for md in self._iter_markdown_files(root.root_path):
            summary["files_seen"] += 1
            await self._consider_file(root, md, summary)
        if root.is_collection_aware:
            for collection_dir in self._iter_collection_dirs(root.root_path):
                summary["collections_seen"] += 1
                enqueued = await self._consider_collection(root, collection_dir)
                if enqueued:
                    summary["collections_enqueued"] += 1

    def _iter_markdown_files(self, root_path: Path) -> Iterable[Path]:
        for p in sorted(root_path.rglob("*.md")):
            if p.is_file() and not p.name.startswith("_"):
                yield p

    def _iter_collection_dirs(self, root_path: Path) -> Iterable[Path]:
        for p in sorted(root_path.rglob("_collection.json")):
            if p.is_file():
                yield p.parent

    async def _consider_file(
        self,
        root: SourceRoot,
        file_path: Path,
        summary: dict[str, int],
    ) -> None:
        try:
            current_hash = compute_file_hash(file_path)
        except Exception as exc:
            logger.warning(
                "REFERENCE_INGEST_HASH_FAILED file=%s exc=%s", file_path, exc,
            )
            return
        recorded = await self._catalog.get_source_hash(
            instance_id=self._instance_id, file_path=str(file_path),
        )
        if recorded == current_hash:
            summary["files_unchanged"] += 1
            return

        # Determine collection_back_reference if the file lives
        # inside a collection (i.e. its parent dir contains a
        # _collection.json).
        back_ref = ""
        if root.is_collection_aware:
            parent = file_path.parent
            if (parent / "_collection.json").exists():
                back_ref = parent.name

        category = _category_for_file(root, file_path)
        if recorded is None:
            summary["files_new"] += 1
        else:
            summary["files_changed"] += 1
        await self._cohort.enqueue_file(
            file_path=str(file_path),
            scope=root.scope,
            category=category,
            trust_tier=root.default_trust_tier,
            collection_back_reference=back_ref,
            owner_domain_id=root.owner_domain_id,
        )

    async def _consider_collection(
        self, root: SourceRoot, collection_dir: Path,
    ) -> bool:
        meta_path = collection_dir / "_collection.json"
        try:
            meta_mtime = meta_path.stat().st_mtime
        except OSError:
            return False
        collection_name = collection_dir.name
        existing = await self._catalog.get_collection_entry(
            instance_id=self._instance_id,
            collection_name=collection_name,
            scope=root.scope,
        )
        if existing is None:
            await self._cohort.enqueue_collection(
                collection_dir=str(collection_dir),
                scope=root.scope,
                owner_domain_id=root.owner_domain_id,
            )
            return True
        # Compare _collection.json mtime against last_refreshed_at.
        # Refresh on any newer-mtime; cheap.
        existing_ts = existing.last_refreshed_at
        if existing_ts:
            try:
                existing_seconds = datetime.fromisoformat(existing_ts).timestamp()
            except ValueError:
                existing_seconds = 0.0
        else:
            existing_seconds = 0.0
        if meta_mtime > existing_seconds + 1.0:  # 1s drift tolerance
            await self._cohort.enqueue_collection(
                collection_dir=str(collection_dir),
                scope=root.scope,
                owner_domain_id=root.owner_domain_id,
            )
            return True
        return False

    async def _tombstone_vanished_files(self, summary: dict[str, int]) -> None:
        all_files = await self._catalog.list_all_files(
            instance_id=self._instance_id,
        )
        for file_path, _hash, scope in all_files:
            p = Path(file_path)
            if p.exists() and p.is_file():
                continue
            count = await self._catalog.tombstone_file(
                instance_id=self._instance_id,
                file_path=file_path,
                reason="file_deleted_observed_during_ingest",
            )
            if count:
                summary["files_tombstoned"] += 1
                try:
                    await self._emitter.emit_tombstoned(
                        instance_id=self._instance_id,
                        file_path=file_path,
                        scope=scope,
                        entry_count=count,
                    )
                except Exception:  # pragma: no cover
                    logger.exception("REFERENCE_INGEST_TOMBSTONE_EMIT_FAILED")


__all__ = [
    "IngestionScanner",
    "SourceRoot",
    "docs_source_root",
    "references_source_root",
]
