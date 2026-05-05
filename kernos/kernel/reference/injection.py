"""Algorithmic injection with hash validation — REFERENCE-PRIMITIVE-V1 C5.

After the reference cohort returns an ``entry_id`` (from
``request_reference`` or auto-induction), content delivery is purely
mechanical: stat the file, recompute the hash, compare to the
catalog's recorded ``source_hash``, read the line range, return.

Fail-closed on mismatch:

* If the file no longer exists, tombstone the catalog entries and
  return an ``InjectionResult`` with ``success=False`` and a
  human-readable fail reason. Emit
  ``reference.recatalog_requested_due_to_hash_mismatch`` for audit.
* If the file content's hash differs from the catalog's recorded
  hash, enqueue the file for async re-cataloging via the cohort,
  emit the same audit event, and return a fail-closed result with
  the user-visible message ``"Reference unavailable, recataloging
  in progress; please retry."``

A successful result carries the section content plus a
trust-tier-aware ``provenance_annotation`` so callers can frame the
content (e.g. ``"Snapshot from {url} on {fetched_at}; not canonical
live truth."`` for ``external_snapshot``-tier entries).

The injection layer doesn't make any LLM call — it's pure
substrate. The cohort navigation (deciding which entry_id matches
a brief) is the LLM step; injection is what runs after."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from kernos.kernel.reference.catalog import (
    CatalogEntry,
    CatalogStore,
    TRUST_AGENT_AUTHORED,
    TRUST_CANONICAL,
    TRUST_EXTERNAL_SNAPSHOT,
    TRUST_QUARANTINED,
    compute_file_hash,
)
from kernos.kernel.reference.cohort import CatalogingCohort
from kernos.kernel.reference.events import ReferenceEventEmitter

logger = logging.getLogger(__name__)


REFERENCE_UNAVAILABLE_MESSAGE = (
    "Reference unavailable, recataloging in progress; please retry."
)


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class InjectionResult:
    """The outcome of attempting to inject content for an entry_id."""

    success: bool
    entry_id: str
    file_path: str = ""
    line_start: int = 0
    line_end: int = 0
    trust_tier: str = ""
    section_title: str = ""
    content: str = ""
    """The section content on success; the user-facing fail-closed
    message on failure."""
    provenance_annotation: str = ""
    """Tier-aware annotation framing the content for the caller.
    Empty for ``canonical`` (no annotation needed).
    """
    fail_reason: str = ""
    """Machine-readable reason on failure: ``unknown_entry`` |
    ``tombstoned`` | ``file_vanished`` | ``hash_mismatch``."""


# ---------------------------------------------------------------------------
# Provenance annotation
# ---------------------------------------------------------------------------


def trust_tier_annotation(entry: CatalogEntry) -> str:
    if entry.trust_tier == TRUST_CANONICAL:
        return ""
    if entry.trust_tier == TRUST_AGENT_AUTHORED:
        stored_by = entry.provenance_metadata.get("stored_by", "")
        if stored_by:
            return f"Agent-authored reference (stored by {stored_by})."
        return "Agent-authored reference."
    if entry.trust_tier == TRUST_EXTERNAL_SNAPSHOT:
        meta = entry.provenance_metadata
        url = meta.get("source_url", "<unknown source>")
        fetched = meta.get("fetched_at", "<unknown date>")
        return f"Snapshot from {url} on {fetched}; not canonical live truth."
    if entry.trust_tier == TRUST_QUARANTINED:
        reason = entry.provenance_metadata.get(
            "quarantine_reason", "unspecified",
        )
        return f"Quarantined: {reason}"
    return ""


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------


async def inject_entry(
    *,
    entry_id: str,
    catalog: CatalogStore,
    emitter: ReferenceEventEmitter,
    cohort: CatalogingCohort | None = None,
    instance_id: str,
) -> InjectionResult:
    """Mechanical content delivery for a catalog entry.

    Steps:

    1. Look up the entry. Unknown / tombstoned entries return
       ``success=False`` with the matching ``fail_reason``.
    2. Stat the file. A vanished file emits the audit event,
       tombstones the catalog rows, and returns the user-facing
       fail message.
    3. Compute the file's current hash. Mismatch enqueues async
       re-cataloging (when ``cohort`` is provided), emits the
       audit event, and returns the user-facing fail message.
    4. Read the line range and return the content with a
       trust-tier-aware annotation.
    """
    entry = await catalog.get_entry(entry_id=entry_id)
    if entry is None:
        return InjectionResult(
            success=False,
            entry_id=entry_id,
            fail_reason="unknown_entry",
            content=REFERENCE_UNAVAILABLE_MESSAGE,
        )
    if entry.tombstoned:
        return InjectionResult(
            success=False,
            entry_id=entry.entry_id,
            file_path=entry.file_path,
            fail_reason="tombstoned",
            content=REFERENCE_UNAVAILABLE_MESSAGE,
        )

    path = Path(entry.file_path)
    if not path.exists() or not path.is_file():
        await _emit_hash_mismatch(
            emitter=emitter,
            instance_id=instance_id,
            entry=entry,
            observed_hash="<vanished>",
        )
        await catalog.tombstone_file(
            instance_id=instance_id,
            file_path=entry.file_path,
            reason="file_vanished_at_injection_time",
        )
        return InjectionResult(
            success=False,
            entry_id=entry.entry_id,
            file_path=entry.file_path,
            fail_reason="file_vanished",
            content=REFERENCE_UNAVAILABLE_MESSAGE,
        )

    try:
        current_hash = compute_file_hash(path)
    except OSError as exc:
        logger.warning(
            "REFERENCE_INJECT_HASH_FAILED entry_id=%s exc=%s",
            entry.entry_id, exc,
        )
        return InjectionResult(
            success=False,
            entry_id=entry.entry_id,
            file_path=entry.file_path,
            fail_reason="hash_mismatch",
            content=REFERENCE_UNAVAILABLE_MESSAGE,
        )

    if current_hash != entry.source_hash:
        await _emit_hash_mismatch(
            emitter=emitter,
            instance_id=instance_id,
            entry=entry,
            observed_hash=current_hash,
        )
        if cohort is not None:
            try:
                await cohort.enqueue_file(
                    file_path=entry.file_path,
                    scope=entry.scope,
                    category=entry.category,
                    trust_tier=entry.trust_tier,
                    provenance_metadata=entry.provenance_metadata,
                    collection_back_reference=entry.collection_back_reference,
                    owner_domain_id=entry.owner_domain_id,
                )
            except Exception:  # pragma: no cover
                logger.exception(
                    "REFERENCE_INJECT_RECATALOG_ENQUEUE_FAILED entry=%s",
                    entry.entry_id,
                )
        return InjectionResult(
            success=False,
            entry_id=entry.entry_id,
            file_path=entry.file_path,
            fail_reason="hash_mismatch",
            content=REFERENCE_UNAVAILABLE_MESSAGE,
        )

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start_idx = max(entry.line_start - 1, 0)
    end_idx = min(entry.line_end, len(lines))
    section = "\n".join(lines[start_idx:end_idx])

    return InjectionResult(
        success=True,
        entry_id=entry.entry_id,
        file_path=entry.file_path,
        line_start=entry.line_start,
        line_end=entry.line_end,
        trust_tier=entry.trust_tier,
        section_title=entry.section_title,
        content=section,
        provenance_annotation=trust_tier_annotation(entry),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _emit_hash_mismatch(
    *,
    emitter: ReferenceEventEmitter,
    instance_id: str,
    entry: CatalogEntry,
    observed_hash: str,
) -> None:
    try:
        await emitter.emit_recatalog_requested_due_to_hash_mismatch(
            instance_id=instance_id,
            entry_id=entry.entry_id,
            file_path=entry.file_path,
            catalog_hash=entry.source_hash,
            observed_hash=observed_hash,
        )
    except Exception:  # pragma: no cover
        logger.exception(
            "REFERENCE_INJECT_HASH_MISMATCH_EMIT_FAILED entry=%s",
            entry.entry_id,
        )


__all__ = [
    "InjectionResult",
    "REFERENCE_UNAVAILABLE_MESSAGE",
    "inject_entry",
    "trust_tier_annotation",
]
