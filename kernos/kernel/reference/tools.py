"""Reference primitive tool surface — REFERENCE-PRIMITIVE-V1 C4.

Seven agent-facing tools. The schemas live in this module (mirroring
the kernel-tool registry's "schema constants in their owning
modules" pattern); tool dispatch wires through
:class:`ReferenceService`.

* ``request_reference(brief_request)`` — agent describes what it
  wants; the cohort navigates the catalog (one cheap-tier LLM
  call) and identifies the matching ``entry_id``; algorithmic
  injection delivers the section content with a trust-tier
  annotation.
* ``store_reference(content, collection, filename, metadata)`` —
  agent stores new reference material under
  ``data/references/<domain_id>/<collection>/<filename>``. Async
  cataloging fires after the file lands.
* ``create_reference_collection(name, purpose, provenance,
  trust_tier, refresh_policy)`` — agent creates a new collection by
  writing ``_collection.json``. Async cataloging produces the
  collection-level catalog entry.
* ``move_reference_to_canvas(entry_id, target)`` — recovery
  primitive: tombstone the catalog entry and stamp the canvas-
  target provenance. The actual canvas write is the agent's job
  on a follow-up turn (the catalog tracks the move; canvas
  composition is the destination's concern).
* ``mark_reference_superseded(old_entry_id, new_entry_id, reason)``
  — recovery primitive: tombstone old, link to new in old's
  provenance metadata.
* ``quarantine_reference(entry_id, reason)`` — recovery primitive:
  flip trust tier to ``quarantined``, preserve prior tier in
  provenance for restoration.
* ``restore_reference_from_quarantine(entry_id)`` — recovery
  primitive: restore the prior trust tier."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from kernos.kernel.reference.catalog import (
    CatalogEntry,
    CatalogStore,
    SCOPE_INSTANCE,
    TRUST_AGENT_AUTHORED,
    TRUST_CANONICAL,
    TRUST_EXTERNAL_SNAPSHOT,
    TRUST_QUARANTINED,
    UnknownEntry,
    VALID_TRUST_TIERS,
    parse_domain_from_scope,
    scope_for_domain,
)
from kernos.kernel.reference.cohort import CatalogingCohort
from kernos.kernel.reference.events import ReferenceEventEmitter
from kernos.kernel.reference.injection import (
    REFERENCE_UNAVAILABLE_MESSAGE,
    inject_entry,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schemas (canonical-source-derived-consumers pattern)
# ---------------------------------------------------------------------------


REQUEST_REFERENCE_TOOL = {
    "name": "request_reference",
    "description": (
        "Ask for canonical Kernos documentation or domain-stored reference "
        "material. Provide a brief natural-language description of what you "
        "want to know. The reference cohort navigates the catalog and "
        "delivers the matching section content. Examples: "
        "'how does the gate decide what's destructive', "
        "'authentication for the test vendor API'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "brief_request": {
                "type": "string",
                "description": (
                    "Natural-language description of what you want to know. "
                    "Specific is better than vague."
                ),
            },
        },
        "required": ["brief_request"],
    },
}


STORE_REFERENCE_TOOL = {
    "name": "store_reference",
    "description": (
        "Store new reference material — vendor API docs, research "
        "snapshots, project-specific reference. Material is keyed to "
        "this domain; members in the same domain share the catalog. "
        "Use trust_tier='external_snapshot' for material pulled from "
        "a specific URL at a specific time; 'agent_authored' for your "
        "own observations or compiled notes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Markdown body to store.",
            },
            "collection": {
                "type": "string",
                "description": (
                    "Collection name to store under. Use an existing "
                    "collection or call create_reference_collection first."
                ),
            },
            "filename": {
                "type": "string",
                "description": (
                    "Filename within the collection (e.g., 'auth.md'). "
                    "Defaults to a generated name."
                ),
            },
            "trust_tier": {
                "type": "string",
                "enum": [
                    "agent_authored", "external_snapshot",
                ],
                "description": (
                    "agent_authored | external_snapshot. Canonical and "
                    "quarantined are not selectable here — canonical is "
                    "reserved for docs/, quarantined is set via "
                    "quarantine_reference."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Provenance metadata. For external_snapshot, include "
                    "source_url and fetched_at. Free-form keys allowed."
                ),
            },
        },
        "required": ["content", "collection"],
    },
}


CREATE_REFERENCE_COLLECTION_TOOL = {
    "name": "create_reference_collection",
    "description": (
        "Create a new reference collection in this domain. A collection "
        "groups related files (vendor API docs, research packets, "
        "project deep-reference). Returns the collection name."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Collection name. Filesystem-safe slug; will be the "
                    "directory name under references/."
                ),
            },
            "purpose": {
                "type": "string",
                "description": (
                    "What this collection is for, in one sentence. The "
                    "cataloger uses this to surface the collection when "
                    "a future signal matches."
                ),
            },
            "trust_tier": {
                "type": "string",
                "enum": ["agent_authored", "external_snapshot"],
                "description": "Default trust tier for member files.",
            },
            "refresh_policy": {
                "type": "string",
                "enum": ["snapshot", "refreshable"],
                "description": (
                    "snapshot = frozen at fetch time; refreshable = may "
                    "be re-fetched (V2 automation)."
                ),
            },
            "provenance": {
                "type": "object",
                "description": (
                    "Free-form provenance metadata. Recommended keys: "
                    "source_url, fetched_at."
                ),
            },
        },
        "required": ["name", "purpose"],
    },
}


MOVE_REFERENCE_TO_CANVAS_TOOL = {
    "name": "move_reference_to_canvas",
    "description": (
        "Recovery primitive: realize a stored reference should actually "
        "be canvas-shaped. Tombstones the catalog entry; the canvas "
        "write is your follow-up work."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entry_id": {"type": "string"},
            "target_canvas": {
                "type": "string",
                "description": (
                    "Canvas name + page hint, e.g. 'My Tools / Auth notes'."
                ),
            },
        },
        "required": ["entry_id", "target_canvas"],
    },
}


MARK_REFERENCE_SUPERSEDED_TOOL = {
    "name": "mark_reference_superseded",
    "description": (
        "Recovery primitive: explicit supersession when a new reference "
        "entry replaces an older one. Tombstones the old; links the new "
        "in old's provenance for audit."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "old_entry_id": {"type": "string"},
            "new_entry_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["old_entry_id", "new_entry_id", "reason"],
    },
}


QUARANTINE_REFERENCE_TOOL = {
    "name": "quarantine_reference",
    "description": (
        "Recovery primitive: flag a reference entry as quarantined when "
        "its reliability is uncertain. Quarantined entries don't auto-"
        "induce; explicit request_reference still surfaces them with a "
        "quarantine caveat. Reversible via "
        "restore_reference_from_quarantine."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entry_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["entry_id", "reason"],
    },
}


RESTORE_REFERENCE_FROM_QUARANTINE_TOOL = {
    "name": "restore_reference_from_quarantine",
    "description": (
        "Recovery primitive: restore a quarantined entry to its prior "
        "trust tier. Pair with quarantine_reference."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entry_id": {"type": "string"},
        },
        "required": ["entry_id"],
    },
}


REFERENCE_TOOL_NAMES: frozenset[str] = frozenset({
    "request_reference",
    "store_reference",
    "create_reference_collection",
    "move_reference_to_canvas",
    "mark_reference_superseded",
    "quarantine_reference",
    "restore_reference_from_quarantine",
})


REFERENCE_TOOL_SCHEMAS: list[dict] = [
    REQUEST_REFERENCE_TOOL,
    STORE_REFERENCE_TOOL,
    CREATE_REFERENCE_COLLECTION_TOOL,
    MOVE_REFERENCE_TO_CANVAS_TOOL,
    MARK_REFERENCE_SUPERSEDED_TOOL,
    QUARANTINE_REFERENCE_TOOL,
    RESTORE_REFERENCE_FROM_QUARANTINE_TOOL,
]


# ---------------------------------------------------------------------------
# Cohort navigator LLM client (for request_reference)
# ---------------------------------------------------------------------------


class ReferenceNavigatorLLM(Protocol):
    """Cheap-tier completion contract for request_reference's
    catalog navigation. Same shape as the cataloging LLM client."""

    @property
    def temperature(self) -> float: ...

    async def complete(self, prompt: str) -> str: ...


_NAVIGATOR_PROMPT_TEMPLATE = """\
You are the reference cohort. The agent asked for reference material.
Pick the SINGLE BEST matching entry from the catalog. Return ONLY
the entry_id. If nothing matches well, return NONE.

Brief: {brief}

Catalog:
{catalog_listing}

Reply with one line: either an entry_id (e.g. ref_abc123) or NONE."""


_ENTRY_ID_RE = re.compile(r"\bref_[A-Za-z0-9_]+\b")


# ---------------------------------------------------------------------------
# ReferenceService
# ---------------------------------------------------------------------------


@dataclass
class ReferenceServiceContext:
    """Per-call dispatch context passed by the dispatch layer.

    ``domain_id`` is bound to the caller's current space — the
    visibility rule's load-bearing input. The dispatch layer
    enforces this; agent-supplied parameters never override.
    """

    instance_id: str
    domain_id: str
    member_id: str = ""


class ReferenceService:
    """Dispatch-layer helper that owns catalog + cohort + emitter +
    navigator-LLM dependencies and exposes one method per tool.

    Each method returns a JSON-serializable dict; the reasoning
    layer stringifies via ``json.dumps`` for the agent's tool
    result, mirroring how other kernel tools work.
    """

    def __init__(
        self,
        *,
        catalog: CatalogStore,
        cohort: CatalogingCohort,
        emitter: ReferenceEventEmitter,
        navigator_llm: ReferenceNavigatorLLM,
        references_root: Path,
        instance_id: str,
    ) -> None:
        self._catalog = catalog
        self._cohort = cohort
        self._emitter = emitter
        self._navigator = navigator_llm
        self._refs_root = references_root
        self._instance_id = instance_id

    # ------------------------------------------------------------------
    # request_reference
    # ------------------------------------------------------------------

    async def handle_request_reference(
        self,
        *,
        ctx: ReferenceServiceContext,
        brief_request: str,
    ) -> dict[str, Any]:
        if not brief_request or not brief_request.strip():
            return {
                "status": "error",
                "error": "brief_request is required.",
            }

        rows = await self._catalog.list_visible(
            instance_id=self._instance_id,
            domain_id=ctx.domain_id,
            include_quarantined=True,
        )
        if not rows:
            return {
                "status": "no_catalog",
                "message": (
                    "The reference catalog is empty for this domain. "
                    "Use store_reference to add material."
                ),
            }
        listing = self._render_catalog_listing(rows)
        prompt = _NAVIGATOR_PROMPT_TEMPLATE.format(
            brief=brief_request.strip(), catalog_listing=listing,
        )
        try:
            raw = await self._navigator.complete(prompt)
        except Exception as exc:
            logger.exception("REFERENCE_NAVIGATE_LLM_FAILED")
            return {
                "status": "error",
                "error": f"Navigator LLM failed: {type(exc).__name__}: {exc}",
            }
        match = _ENTRY_ID_RE.search(raw or "")
        if not match:
            return {
                "status": "no_match",
                "message": "No catalog entry matches the brief.",
                "navigator_response": (raw or "").strip(),
            }
        entry_id = match.group(0)
        # Visibility re-check: the cohort can only return entry_ids
        # from the listing it was given (which is already domain-
        # filtered). Defense-in-depth: re-verify visibility before
        # injecting.
        chosen = next((r for r in rows if r.entry_id == entry_id), None)
        if chosen is None:
            return {
                "status": "no_match",
                "message": (
                    "Navigator chose an entry not in the visible catalog."
                ),
            }
        result = await inject_entry(
            entry_id=entry_id,
            catalog=self._catalog,
            emitter=self._emitter,
            cohort=self._cohort,
            instance_id=self._instance_id,
        )
        if not result.success:
            return {
                "status": "unavailable",
                "fail_reason": result.fail_reason,
                "message": result.content,
                "entry_id": entry_id,
            }
        return {
            "status": "ok",
            "entry_id": result.entry_id,
            "section_title": result.section_title,
            "file_path": result.file_path,
            "line_start": result.line_start,
            "line_end": result.line_end,
            "trust_tier": result.trust_tier,
            "provenance_annotation": result.provenance_annotation,
            "content": result.content,
        }

    def _render_catalog_listing(self, rows: list[CatalogEntry]) -> str:
        # Only file-level entries (sections) are inject-able for
        # content; collection-level entries are surfaced as map
        # references. We include both shapes in the listing so the
        # navigator can pick a collection-level entry when the
        # brief is broad.
        lines: list[str] = []
        for r in rows:
            if r.entry_type == "collection":
                lines.append(
                    f"{r.entry_id} | [collection] {r.collection_name} | "
                    f"{r.purpose[:140]}"
                )
            else:
                lines.append(
                    f"{r.entry_id} | {r.category}/{Path(r.file_path).name} "
                    f"| {r.section_title} | {r.one_line[:140]}"
                )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # store_reference
    # ------------------------------------------------------------------

    async def handle_store_reference(
        self,
        *,
        ctx: ReferenceServiceContext,
        content: str,
        collection: str,
        filename: str = "",
        trust_tier: str = TRUST_AGENT_AUTHORED,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not content:
            return {"status": "error", "error": "content is required."}
        if not collection:
            return {"status": "error", "error": "collection is required."}
        if trust_tier not in {TRUST_AGENT_AUTHORED, TRUST_EXTERNAL_SNAPSHOT}:
            return {
                "status": "error",
                "error": (
                    f"trust_tier must be agent_authored or external_snapshot; "
                    f"got {trust_tier!r}."
                ),
            }
        if not ctx.domain_id:
            return {
                "status": "error",
                "error": (
                    "store_reference requires a domain context "
                    "(retrieval_context.domain_id is empty)."
                ),
            }

        safe_collection = _safe_slug(collection)
        cdir = self._refs_root / ctx.domain_id / safe_collection
        cdir.mkdir(parents=True, exist_ok=True)
        # Auto-generate a filename if not provided.
        fname = (filename or "").strip()
        if not fname:
            from uuid import uuid4
            fname = f"note_{uuid4().hex[:8]}.md"
        if not fname.endswith(".md"):
            fname = f"{fname}.md"
        if "/" in fname or fname.startswith("."):
            return {
                "status": "error",
                "error": (
                    "filename must be a flat *.md basename, no path "
                    "components."
                ),
            }
        target = cdir / fname
        target.write_text(content, encoding="utf-8")

        provenance: dict[str, Any] = dict(metadata or {})
        provenance.setdefault(
            "stored_by", ctx.member_id or "agent",
        )
        provenance.setdefault("stored_at_scope", scope_for_domain(ctx.domain_id))

        await self._cohort.enqueue_file(
            file_path=str(target),
            scope=scope_for_domain(ctx.domain_id),
            category=safe_collection,
            trust_tier=trust_tier,
            provenance_metadata=provenance,
            collection_back_reference=safe_collection,
            owner_domain_id=ctx.domain_id,
        )
        try:
            await self._emitter.emit_stored(
                instance_id=self._instance_id,
                file_path=str(target),
                scope=scope_for_domain(ctx.domain_id),
                collection_name=safe_collection,
                trust_tier=trust_tier,
                stored_by=str(provenance["stored_by"]),
            )
        except Exception:  # pragma: no cover
            logger.exception("REFERENCE_STORED_EMIT_FAILED")
        return {
            "status": "ok",
            "file_path": str(target),
            "collection": safe_collection,
            "trust_tier": trust_tier,
            "message": (
                "Stored. Cataloging is async — may take a turn or two "
                "before the entry surfaces in retrieval."
            ),
        }

    # ------------------------------------------------------------------
    # create_reference_collection
    # ------------------------------------------------------------------

    async def handle_create_reference_collection(
        self,
        *,
        ctx: ReferenceServiceContext,
        name: str,
        purpose: str,
        trust_tier: str = TRUST_AGENT_AUTHORED,
        refresh_policy: str = "snapshot",
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not name or not purpose:
            return {
                "status": "error",
                "error": "name and purpose are required.",
            }
        if trust_tier not in {TRUST_AGENT_AUTHORED, TRUST_EXTERNAL_SNAPSHOT}:
            return {
                "status": "error",
                "error": (
                    "trust_tier must be agent_authored or external_snapshot."
                ),
            }
        if not ctx.domain_id:
            return {
                "status": "error",
                "error": "domain context required.",
            }
        safe = _safe_slug(name)
        cdir = self._refs_root / ctx.domain_id / safe
        if cdir.exists() and (cdir / "_collection.json").exists():
            return {
                "status": "exists",
                "collection": safe,
                "message": "Collection already exists.",
            }
        cdir.mkdir(parents=True, exist_ok=True)
        meta = {
            "name": safe,
            "purpose": purpose,
            "trust_tier": trust_tier,
            "refresh_policy": refresh_policy,
            "provenance": provenance or {},
        }
        meta["provenance"].setdefault("stored_by", ctx.member_id or "agent")
        (cdir / "_collection.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8",
        )
        await self._cohort.enqueue_collection(
            collection_dir=str(cdir),
            scope=scope_for_domain(ctx.domain_id),
            owner_domain_id=ctx.domain_id,
        )
        return {
            "status": "ok",
            "collection": safe,
            "message": (
                f"Collection {safe!r} created. Use store_reference to add "
                "content."
            ),
        }

    # ------------------------------------------------------------------
    # Recovery primitives
    # ------------------------------------------------------------------

    async def handle_move_reference_to_canvas(
        self,
        *,
        ctx: ReferenceServiceContext,
        entry_id: str,
        target_canvas: str,
    ) -> dict[str, Any]:
        try:
            updated = await self._catalog.mark_moved_to_canvas(
                entry_id=entry_id,
                target_canvas=target_canvas,
                moved_by=ctx.member_id or "agent",
            )
        except UnknownEntry:
            return {"status": "error", "error": f"unknown entry_id {entry_id!r}"}
        try:
            await self._emitter.emit_moved_to_canvas(
                instance_id=self._instance_id,
                entry_id=entry_id,
                target_canvas=target_canvas,
                moved_by=ctx.member_id or "agent",
            )
        except Exception:  # pragma: no cover
            logger.exception("REFERENCE_MOVED_TO_CANVAS_EMIT_FAILED")
        return {
            "status": "ok",
            "entry_id": entry_id,
            "target_canvas": target_canvas,
            "message": (
                "Reference entry tombstoned. Now write the content to the "
                "target canvas as your follow-up step."
            ),
        }

    async def handle_mark_reference_superseded(
        self,
        *,
        ctx: ReferenceServiceContext,
        old_entry_id: str,
        new_entry_id: str,
        reason: str,
    ) -> dict[str, Any]:
        try:
            await self._catalog.supersede(
                old_entry_id=old_entry_id,
                new_entry_id=new_entry_id,
                reason=reason,
            )
        except UnknownEntry as exc:
            return {"status": "error", "error": str(exc)}
        try:
            await self._emitter.emit_superseded(
                instance_id=self._instance_id,
                old_entry_id=old_entry_id,
                new_entry_id=new_entry_id,
                reason=reason,
            )
        except Exception:  # pragma: no cover
            logger.exception("REFERENCE_SUPERSEDED_EMIT_FAILED")
        return {
            "status": "ok",
            "old_entry_id": old_entry_id,
            "new_entry_id": new_entry_id,
        }

    async def handle_quarantine_reference(
        self,
        *,
        ctx: ReferenceServiceContext,
        entry_id: str,
        reason: str,
    ) -> dict[str, Any]:
        try:
            entry = await self._catalog.quarantine_entry(
                entry_id=entry_id,
                reason=reason,
                quarantined_by=ctx.member_id or "agent",
            )
        except UnknownEntry:
            return {"status": "error", "error": f"unknown entry_id {entry_id!r}"}
        try:
            await self._emitter.emit_quarantined(
                instance_id=self._instance_id,
                entry_id=entry_id,
                reason=reason,
                quarantined_by=ctx.member_id or "agent",
            )
        except Exception:  # pragma: no cover
            logger.exception("REFERENCE_QUARANTINED_EMIT_FAILED")
        return {
            "status": "ok",
            "entry_id": entry_id,
            "trust_tier": entry.trust_tier,
        }

    async def handle_restore_reference_from_quarantine(
        self,
        *,
        ctx: ReferenceServiceContext,
        entry_id: str,
    ) -> dict[str, Any]:
        try:
            entry, prior = await self._catalog.restore_entry(
                entry_id=entry_id,
                restored_by=ctx.member_id or "agent",
            )
        except UnknownEntry:
            return {"status": "error", "error": f"unknown entry_id {entry_id!r}"}
        try:
            await self._emitter.emit_restored_from_quarantine(
                instance_id=self._instance_id,
                entry_id=entry_id,
                restored_by=ctx.member_id or "agent",
                prior_trust_tier=prior,
            )
        except Exception:  # pragma: no cover
            logger.exception("REFERENCE_RESTORED_EMIT_FAILED")
        return {
            "status": "ok",
            "entry_id": entry_id,
            "trust_tier": entry.trust_tier,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SAFE_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _safe_slug(name: str) -> str:
    """Filesystem-safe collection slug. Strips path separators,
    spaces collapse to hyphens, only alnum + ``_`` + ``-`` survive."""
    cleaned = _SAFE_SLUG_RE.sub("-", name.strip())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned or "untitled"


__all__ = [
    "CREATE_REFERENCE_COLLECTION_TOOL",
    "MARK_REFERENCE_SUPERSEDED_TOOL",
    "MOVE_REFERENCE_TO_CANVAS_TOOL",
    "QUARANTINE_REFERENCE_TOOL",
    "REFERENCE_TOOL_NAMES",
    "REFERENCE_TOOL_SCHEMAS",
    "REQUEST_REFERENCE_TOOL",
    "RESTORE_REFERENCE_FROM_QUARANTINE_TOOL",
    "ReferenceNavigatorLLM",
    "ReferenceService",
    "ReferenceServiceContext",
    "STORE_REFERENCE_TOOL",
]
