"""Reference primitive event emission (REFERENCE-PRIMITIVE-V1).

Concrete adapter that registers ``"reference"`` with the
EmitterRegistry once at substrate bring-up and exposes one typed
emit method per event shape. Mirrors :mod:`kernos.kernel.crb.events`
in shape and discipline.

Substrate enforcement:

* :class:`EmitterRegistry` uniqueness ensures at most one emitter
  claims ``source_module="reference"`` per process lifecycle.
* :class:`EventEmitter.emit` stamps ``envelope.source_module="reference"``
  from the registered identity, NOT from caller payload. Callers
  cannot smuggle a different source module via the payload.

Twelve event shapes total: the eleven cataloging / store / recovery
shapes named in the spec body plus
``reference.recatalog_requested_due_to_hash_mismatch`` named in the
algorithmic-injection section. The eleven primary shapes are the
ones the AC list pins; the twelfth is the injection-side stale-hash
trigger (the spec body describes it explicitly under "Algorithmic
injection / Hash validation fail-closed")."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.event_stream import EventEmitter


REFERENCE_SOURCE_MODULE = "reference"


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


# Cataloging-cohort lifecycle.
EVENT_REFERENCE_CATALOGED = "reference.cataloged"
EVENT_REFERENCE_RECATALOGED = "reference.recataloged"
EVENT_REFERENCE_RECATALOG_FAILED = "reference.recatalog_failed"
EVENT_REFERENCE_TOMBSTONED = "reference.tombstoned"

# Agent-store + lifecycle.
EVENT_REFERENCE_STORED = "reference.stored"
EVENT_REFERENCE_SUPERSEDED = "reference.superseded"
EVENT_REFERENCE_QUARANTINED = "reference.quarantined"
EVENT_REFERENCE_RESTORED_FROM_QUARANTINE = "reference.restored_from_quarantine"
EVENT_REFERENCE_MOVED_TO_CANVAS = "reference.moved_to_canvas"

# Collection-level.
EVENT_REFERENCE_COLLECTION_CREATED = "reference.collection_created"
EVENT_REFERENCE_COLLECTION_REFRESHED = "reference.collection_refreshed"

# Algorithmic-injection-side stale-hash trigger (spec body, not the
# eleven AC-listed shapes — but emitted from the injection path so a
# recatalog request is auditable independently of the recataloging
# outcome).
EVENT_REFERENCE_RECATALOG_REQUESTED_DUE_TO_HASH_MISMATCH = (
    "reference.recatalog_requested_due_to_hash_mismatch"
)


REFERENCE_EVENT_TYPES: frozenset[str] = frozenset({
    EVENT_REFERENCE_CATALOGED,
    EVENT_REFERENCE_RECATALOGED,
    EVENT_REFERENCE_RECATALOG_FAILED,
    EVENT_REFERENCE_TOMBSTONED,
    EVENT_REFERENCE_STORED,
    EVENT_REFERENCE_SUPERSEDED,
    EVENT_REFERENCE_QUARANTINED,
    EVENT_REFERENCE_RESTORED_FROM_QUARANTINE,
    EVENT_REFERENCE_MOVED_TO_CANVAS,
    EVENT_REFERENCE_COLLECTION_CREATED,
    EVENT_REFERENCE_COLLECTION_REFRESHED,
    EVENT_REFERENCE_RECATALOG_REQUESTED_DUE_TO_HASH_MISMATCH,
})


# ---------------------------------------------------------------------------
# ReferenceEventEmitter
# ---------------------------------------------------------------------------


class ReferenceEventEmitter:
    """Concrete reference-primitive event-emitter adapter.

    Constructor takes the registered :class:`EventEmitter` (with
    ``source_module="reference"``). The adapter verifies the source
    identity at construction so misconfiguration surfaces at engine
    bring-up, not at first emission.
    """

    def __init__(self, *, emitter: "EventEmitter") -> None:
        if emitter.source_module != REFERENCE_SOURCE_MODULE:
            raise ValueError(
                f"ReferenceEventEmitter requires an emitter registered "
                f"with source_module={REFERENCE_SOURCE_MODULE!r}, got "
                f"{emitter.source_module!r}"
            )
        self._emitter = emitter

    @property
    def source_module(self) -> str:
        return self._emitter.source_module

    # ------------------------------------------------------------------
    # Cataloging-cohort lifecycle
    # ------------------------------------------------------------------

    async def emit_cataloged(
        self,
        *,
        instance_id: str,
        file_path: str,
        category: str,
        scope: str,
        entry_count: int,
        source_hash: str,
    ) -> str:
        return await self._emitter.emit(
            instance_id,
            EVENT_REFERENCE_CATALOGED,
            {
                "file_path": file_path,
                "category": category,
                "scope": scope,
                "entry_count": entry_count,
                "source_hash": source_hash,
            },
        )

    async def emit_recataloged(
        self,
        *,
        instance_id: str,
        file_path: str,
        scope: str,
        previous_entry_count: int,
        new_entry_count: int,
        previous_hash: str,
        new_hash: str,
    ) -> str:
        return await self._emitter.emit(
            instance_id,
            EVENT_REFERENCE_RECATALOGED,
            {
                "file_path": file_path,
                "scope": scope,
                "previous_entry_count": previous_entry_count,
                "new_entry_count": new_entry_count,
                "previous_hash": previous_hash,
                "new_hash": new_hash,
            },
        )

    async def emit_recatalog_failed(
        self,
        *,
        instance_id: str,
        file_path: str,
        reason: str,
    ) -> str:
        return await self._emitter.emit(
            instance_id,
            EVENT_REFERENCE_RECATALOG_FAILED,
            {"file_path": file_path, "reason": reason},
        )

    async def emit_tombstoned(
        self,
        *,
        instance_id: str,
        file_path: str,
        scope: str,
        entry_count: int,
    ) -> str:
        return await self._emitter.emit(
            instance_id,
            EVENT_REFERENCE_TOMBSTONED,
            {
                "file_path": file_path,
                "scope": scope,
                "entry_count": entry_count,
            },
        )

    # ------------------------------------------------------------------
    # Agent-store + lifecycle
    # ------------------------------------------------------------------

    async def emit_stored(
        self,
        *,
        instance_id: str,
        file_path: str,
        scope: str,
        collection_name: str,
        trust_tier: str,
        stored_by: str,
    ) -> str:
        return await self._emitter.emit(
            instance_id,
            EVENT_REFERENCE_STORED,
            {
                "file_path": file_path,
                "scope": scope,
                "collection_name": collection_name,
                "trust_tier": trust_tier,
                "stored_by": stored_by,
            },
        )

    async def emit_superseded(
        self,
        *,
        instance_id: str,
        old_entry_id: str,
        new_entry_id: str,
        reason: str,
    ) -> str:
        return await self._emitter.emit(
            instance_id,
            EVENT_REFERENCE_SUPERSEDED,
            {
                "old_entry_id": old_entry_id,
                "new_entry_id": new_entry_id,
                "reason": reason,
            },
        )

    async def emit_quarantined(
        self,
        *,
        instance_id: str,
        entry_id: str,
        reason: str,
        quarantined_by: str,
    ) -> str:
        return await self._emitter.emit(
            instance_id,
            EVENT_REFERENCE_QUARANTINED,
            {
                "entry_id": entry_id,
                "reason": reason,
                "quarantined_by": quarantined_by,
            },
        )

    async def emit_restored_from_quarantine(
        self,
        *,
        instance_id: str,
        entry_id: str,
        restored_by: str,
        prior_trust_tier: str,
    ) -> str:
        return await self._emitter.emit(
            instance_id,
            EVENT_REFERENCE_RESTORED_FROM_QUARANTINE,
            {
                "entry_id": entry_id,
                "restored_by": restored_by,
                "prior_trust_tier": prior_trust_tier,
            },
        )

    async def emit_moved_to_canvas(
        self,
        *,
        instance_id: str,
        entry_id: str,
        target_canvas: str,
        moved_by: str,
    ) -> str:
        return await self._emitter.emit(
            instance_id,
            EVENT_REFERENCE_MOVED_TO_CANVAS,
            {
                "entry_id": entry_id,
                "target_canvas": target_canvas,
                "moved_by": moved_by,
            },
        )

    # ------------------------------------------------------------------
    # Collection-level
    # ------------------------------------------------------------------

    async def emit_collection_created(
        self,
        *,
        instance_id: str,
        collection_name: str,
        scope: str,
        purpose: str,
        trust_tier: str,
        refresh_policy: str,
        created_by: str,
    ) -> str:
        return await self._emitter.emit(
            instance_id,
            EVENT_REFERENCE_COLLECTION_CREATED,
            {
                "collection_name": collection_name,
                "scope": scope,
                "purpose": purpose,
                "trust_tier": trust_tier,
                "refresh_policy": refresh_policy,
                "created_by": created_by,
            },
        )

    async def emit_collection_refreshed(
        self,
        *,
        instance_id: str,
        collection_name: str,
        scope: str,
        member_file_count: int,
    ) -> str:
        return await self._emitter.emit(
            instance_id,
            EVENT_REFERENCE_COLLECTION_REFRESHED,
            {
                "collection_name": collection_name,
                "scope": scope,
                "member_file_count": member_file_count,
            },
        )

    # ------------------------------------------------------------------
    # Algorithmic-injection stale-hash trigger
    # ------------------------------------------------------------------

    async def emit_recatalog_requested_due_to_hash_mismatch(
        self,
        *,
        instance_id: str,
        entry_id: str,
        file_path: str,
        catalog_hash: str,
        observed_hash: str,
    ) -> str:
        return await self._emitter.emit(
            instance_id,
            EVENT_REFERENCE_RECATALOG_REQUESTED_DUE_TO_HASH_MISMATCH,
            {
                "entry_id": entry_id,
                "file_path": file_path,
                "catalog_hash": catalog_hash,
                "observed_hash": observed_hash,
            },
        )


__all__ = [
    "EVENT_REFERENCE_CATALOGED",
    "EVENT_REFERENCE_COLLECTION_CREATED",
    "EVENT_REFERENCE_COLLECTION_REFRESHED",
    "EVENT_REFERENCE_MOVED_TO_CANVAS",
    "EVENT_REFERENCE_QUARANTINED",
    "EVENT_REFERENCE_RECATALOG_FAILED",
    "EVENT_REFERENCE_RECATALOG_REQUESTED_DUE_TO_HASH_MISMATCH",
    "EVENT_REFERENCE_RECATALOGED",
    "EVENT_REFERENCE_RESTORED_FROM_QUARANTINE",
    "EVENT_REFERENCE_STORED",
    "EVENT_REFERENCE_SUPERSEDED",
    "EVENT_REFERENCE_TOMBSTONED",
    "REFERENCE_EVENT_TYPES",
    "REFERENCE_SOURCE_MODULE",
    "ReferenceEventEmitter",
]
