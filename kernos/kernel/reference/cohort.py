"""Cataloging cohort — REFERENCE-PRIMITIVE-V1 C2.

A cheap-tier async worker that turns reference source files into
catalog entries. Runs off any agent-facing hot path; the per-turn
ingestion check (C3) enqueues file-change requests, the worker
drains.

Per-section work (file-level entries):

* Read the file.
* Split into sections at markdown ``##`` headings (h2). Files
  with no h2 produce a single section spanning the entire file
  (title = h1 if present, else filename stem).
* Compute a stable SHA-256 of the full file content as the
  ``source_hash``.
* For each section, ask the cheap-tier LLM for a one-line
  description (under \\~120 chars) — small prompt, structured
  expected output.
* Hand the new ``CatalogEntry`` list to
  :meth:`CatalogStore.replace_file_entries` — transactional
  drop-and-rebuild.
* Emit ``reference.cataloged`` or ``reference.recataloged`` per
  whether prior entries existed.

Per-collection work (collection-level entries):

* Read ``_collection.json``.
* Optionally ask the cheap-tier LLM for a one-paragraph purpose
  summary (skipped when ``_collection.json`` already provides a
  non-trivial purpose).
* Upsert the collection-level entry.
* Emit ``reference.collection_created`` (first time) or
  ``reference.collection_refreshed`` (subsequent).

Failure handling:

* Per-file rebuild is transactional. Any error mid-rebuild rolls
  back; the catalog never holds partial-old + partial-new state.
* On rebuild error, ``reference.recatalog_failed`` is emitted with
  ``reason``.
* Tombstone for deleted files is handled by the ingestion check
  (C3) directly via :meth:`CatalogStore.tombstone_file` — the
  cohort sees only present-files."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Protocol

from kernos.utils import utc_now
from kernos.kernel.reference.catalog import (
    CatalogEntry,
    CatalogStore,
    ENTRY_TYPE_COLLECTION,
    ENTRY_TYPE_FILE,
    SCOPE_INSTANCE,
    TRUST_AGENT_AUTHORED,
    TRUST_CANONICAL,
    TRUST_EXTERNAL_SNAPSHOT,
    compute_source_hash,
    parse_domain_from_scope,
    scope_for_domain,
)
from kernos.kernel.reference.events import ReferenceEventEmitter

logger = logging.getLogger(__name__)


# Cheap-tier completions are short — one-line descriptions fit
# comfortably in \\~120 chars; one-paragraph purpose summaries fit in
# \\~400 chars. Caps are conservative.
ONE_LINE_TOKEN_CAP = 120
PURPOSE_SUMMARY_TOKEN_CAP = 256


# Section split heuristic: lines starting with exactly ``## ``
# (h2 — ``###`` is excluded). Stable across the docs/ tree.
_H2_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_H1_RE = re.compile(r"^#\s+(?P<title>.+?)\s*$")


# ---------------------------------------------------------------------------
# LLMClient protocol (cheap-tier completions)
# ---------------------------------------------------------------------------


class CatalogingLLMClient(Protocol):
    """Stateless cheap-tier completion contract.

    Same shape as ``CRBProposalAuthor.LLMClient`` — temperature
    declared as a property, ``complete(prompt) -> str`` is the
    single async surface. The cataloging cohort doesn't need
    structured output; one-line / one-paragraph summaries are plain
    text returned verbatim."""

    @property
    def temperature(self) -> float: ...

    async def complete(self, prompt: str) -> str: ...


# ---------------------------------------------------------------------------
# Section splitter
# ---------------------------------------------------------------------------


def _filename_stem(file_path: str) -> str:
    return Path(file_path).stem


def split_sections(
    *,
    file_text: str,
    file_path: str,
) -> list[tuple[str, int, int]]:
    """Return ``[(section_title, line_start, line_end), ...]``.

    Files with at least one ``## `` heading split at h2 boundaries.
    Files without ``## `` produce a single section spanning the
    whole file (title = h1 if present, else filename stem).
    """
    lines = file_text.splitlines()
    if not lines:
        return [(_filename_stem(file_path), 1, 1)]

    h2_indices: list[tuple[int, str]] = []
    h1_title: str | None = None
    for idx, line in enumerate(lines, 1):
        if h1_title is None:
            m1 = _H1_RE.match(line)
            if m1:
                h1_title = m1.group("title").strip()
        m2 = _H2_RE.match(line)
        if m2:
            h2_indices.append((idx, m2.group("title").strip()))

    if not h2_indices:
        title = h1_title or _filename_stem(file_path)
        return [(title, 1, len(lines))]

    sections: list[tuple[str, int, int]] = []
    for i, (start, title) in enumerate(h2_indices):
        end = h2_indices[i + 1][0] - 1 if i + 1 < len(h2_indices) else len(lines)
        sections.append((title, start, end))
    return sections


# ---------------------------------------------------------------------------
# Section content extraction (best-effort short body for the LLM)
# ---------------------------------------------------------------------------


def _section_body(file_text: str, line_start: int, line_end: int, *, max_lines: int = 40) -> str:
    """Return the lines in the section, capped at ``max_lines``.

    The cheap-tier LLM only needs enough context to write a
    one-liner; sending the full section would inflate prompt costs
    for long sections."""
    lines = file_text.splitlines()
    section = lines[max(line_start - 1, 0): line_end]
    if len(section) > max_lines:
        section = section[:max_lines]
    return "\n".join(section)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


_ONE_LINE_PROMPT_TEMPLATE = """\
You are cataloging a section of Kernos documentation. Write ONE LINE
(no markdown, no quotes, no leading verbs like "describes" or
"explains") that names what this section is for.

Maximum 120 characters. Plain English. Topical, not stylistic.

Section title: {title}
Section body:
---
{body}
---

One-line description:"""


_COLLECTION_PURPOSE_PROMPT_TEMPLATE = """\
You are cataloging a reference collection. Given the collection's
declared metadata and a list of its member-file titles, write ONE
SHORT PARAGRAPH (under 400 characters) describing the collection's
purpose so an agent can decide if it's relevant to the current
question. No markdown. No quotes.

Collection name: {name}
Declared purpose: {declared_purpose}
Trust tier: {trust_tier}
Member file titles:
{member_titles}

Collection purpose paragraph:"""


# ---------------------------------------------------------------------------
# Cataloging payloads (input to the cohort)
# ---------------------------------------------------------------------------


def _new_entry_id() -> str:
    return f"ref_{uuid.uuid4().hex[:16]}"


def _trust_tier_from_collection_meta(meta: dict[str, Any], default: str) -> str:
    tier = meta.get("trust_tier")
    if isinstance(tier, str) and tier:
        return tier
    return default


# ---------------------------------------------------------------------------
# Cataloging cohort
# ---------------------------------------------------------------------------


class CatalogingCohort:
    """Async worker that turns file-change events into catalog rows.

    The cohort owns no DB connection of its own; it delegates to the
    injected :class:`CatalogStore` for persistence and to the
    injected :class:`ReferenceEventEmitter` for event audit. The
    worker task is started/stopped explicitly; consumers ``await``
    :meth:`enqueue_file` to schedule work and may ``await``
    :meth:`drain` to block until the queue is empty (test
    convenience).
    """

    def __init__(
        self,
        *,
        catalog: CatalogStore,
        emitter: ReferenceEventEmitter,
        llm: CatalogingLLMClient,
        instance_id: str,
    ) -> None:
        self._catalog = catalog
        self._emitter = emitter
        self._llm = llm
        self._instance_id = instance_id
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        # Tracks files currently in flight so duplicate enqueues
        # don't fan out into multiple in-progress rebuilds for the
        # same (instance, file_path) — the most-recent enqueue wins.
        self._in_flight: set[tuple[str, str]] = set()

    # --- Lifecycle --------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(
            self._run(), name="reference_cataloging_cohort",
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopped.set()
        # Push a sentinel so the worker wakes and exits.
        try:
            self._queue.put_nowait({"_sentinel": True})
        except Exception:  # pragma: no cover
            pass
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:  # pragma: no cover
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def drain(self) -> None:
        """Block until the queue is empty AND no item is in flight.

        Test-convenience hook. Production callers don't await this
        — agent-facing turns must remain non-blocking on cataloging.
        """
        while not self._queue.empty() or self._in_flight:
            await asyncio.sleep(0.005)

    # --- Enqueue API ------------------------------------------------

    async def enqueue_file(
        self,
        *,
        file_path: str,
        scope: str,
        category: str,
        trust_tier: str = TRUST_CANONICAL,
        provenance_metadata: dict[str, Any] | None = None,
        collection_back_reference: str = "",
        owner_domain_id: str = "",
    ) -> None:
        await self._queue.put(
            {
                "kind": "file",
                "file_path": file_path,
                "scope": scope,
                "category": category,
                "trust_tier": trust_tier,
                "provenance_metadata": provenance_metadata or {},
                "collection_back_reference": collection_back_reference,
                "owner_domain_id": owner_domain_id,
            },
        )

    async def enqueue_collection(
        self,
        *,
        collection_dir: str,
        scope: str,
        owner_domain_id: str = "",
    ) -> None:
        await self._queue.put(
            {
                "kind": "collection",
                "collection_dir": collection_dir,
                "scope": scope,
                "owner_domain_id": owner_domain_id,
            },
        )

    # --- Worker loop ------------------------------------------------

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:  # pragma: no cover
                break
            if item.get("_sentinel"):
                self._queue.task_done()
                break
            kind = item.get("kind")
            key = (kind, item.get("file_path") or item.get("collection_dir") or "")
            self._in_flight.add(key)
            try:
                if kind == "file":
                    await self._catalog_file(item)
                elif kind == "collection":
                    await self._catalog_collection(item)
                else:
                    logger.warning(
                        "REFERENCE_COHORT_UNKNOWN_KIND: %r", kind,
                    )
            except Exception as exc:
                logger.exception(
                    "REFERENCE_COHORT_ITEM_FAILED kind=%s payload=%r",
                    kind, item,
                )
                # File-level recatalog failure event surfaces the reason.
                if kind == "file":
                    try:
                        await self._emitter.emit_recatalog_failed(
                            instance_id=self._instance_id,
                            file_path=item.get("file_path", ""),
                            reason=f"{type(exc).__name__}: {exc}",
                        )
                    except Exception:  # pragma: no cover
                        pass
            finally:
                self._in_flight.discard(key)
                self._queue.task_done()

    # --- File cataloging --------------------------------------------

    async def _catalog_file(self, item: dict[str, Any]) -> None:
        file_path: str = item["file_path"]
        scope: str = item["scope"]
        category: str = item["category"]
        trust_tier: str = item["trust_tier"]
        provenance: dict[str, Any] = dict(item["provenance_metadata"])
        collection_back_reference: str = item["collection_back_reference"]
        owner_domain_id: str = item["owner_domain_id"]

        path = Path(file_path)
        if not path.exists() or not path.is_file():
            logger.info(
                "REFERENCE_COHORT_FILE_VANISHED file_path=%s — tombstoning",
                file_path,
            )
            await self._catalog.tombstone_file(
                instance_id=self._instance_id,
                file_path=file_path,
                reason="file_vanished_during_catalog",
            )
            return

        body_bytes = path.read_bytes()
        source_hash = compute_source_hash(body_bytes)
        body_text = body_bytes.decode("utf-8", errors="replace")

        prior_hash = await self._catalog.get_source_hash(
            instance_id=self._instance_id, file_path=file_path,
        )
        prior_entries = await self._catalog.list_entries_for_file(
            instance_id=self._instance_id, file_path=file_path,
        )
        prior_count = len(prior_entries)

        sections = split_sections(file_text=body_text, file_path=file_path)
        new_entries: list[CatalogEntry] = []
        for section_title, line_start, line_end in sections:
            section_body = _section_body(body_text, line_start, line_end)
            try:
                one_line = await self._llm.complete(
                    _ONE_LINE_PROMPT_TEMPLATE.format(
                        title=section_title, body=section_body,
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "REFERENCE_COHORT_ONE_LINE_FAILED file=%s title=%r exc=%s",
                    file_path, section_title, exc,
                )
                one_line = section_title  # fallback — never block on LLM
            one_line = _truncate_one_line(one_line)
            new_entries.append(
                CatalogEntry(
                    entry_id=_new_entry_id(),
                    instance_id=self._instance_id,
                    entry_type=ENTRY_TYPE_FILE,
                    scope=scope,
                    category=category,
                    indexed_at=utc_now(),
                    trust_tier=trust_tier,
                    auto_inducible=trust_tier != "quarantined",
                    provenance_metadata=provenance,
                    file_path=file_path,
                    section_title=section_title,
                    one_line=one_line,
                    line_start=line_start,
                    line_end=line_end,
                    source_hash=source_hash,
                    collection_back_reference=collection_back_reference,
                    owner_domain_id=owner_domain_id,
                ),
            )

        await self._catalog.replace_file_entries(
            instance_id=self._instance_id,
            file_path=file_path,
            new_entries=new_entries,
        )

        if prior_count == 0:
            await self._emitter.emit_cataloged(
                instance_id=self._instance_id,
                file_path=file_path,
                category=category,
                scope=scope,
                entry_count=len(new_entries),
                source_hash=source_hash,
            )
        else:
            await self._emitter.emit_recataloged(
                instance_id=self._instance_id,
                file_path=file_path,
                scope=scope,
                previous_entry_count=prior_count,
                new_entry_count=len(new_entries),
                previous_hash=prior_hash or "",
                new_hash=source_hash,
            )

    # --- Collection cataloging --------------------------------------

    async def _catalog_collection(self, item: dict[str, Any]) -> None:
        collection_dir: str = item["collection_dir"]
        scope: str = item["scope"]
        owner_domain_id: str = item["owner_domain_id"]

        cdir = Path(collection_dir)
        meta_path = cdir / "_collection.json"
        if not meta_path.exists():
            logger.info(
                "REFERENCE_COHORT_COLLECTION_NO_META dir=%s — skipping",
                collection_dir,
            )
            return

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "REFERENCE_COHORT_COLLECTION_META_BAD dir=%s exc=%s",
                collection_dir, exc,
            )
            return

        collection_name = (
            meta.get("name")
            or cdir.name
        )
        declared_purpose = (meta.get("purpose") or "").strip()
        trust_tier = _trust_tier_from_collection_meta(
            meta, default=TRUST_AGENT_AUTHORED,
        )
        refresh_policy = meta.get("refresh_policy", "snapshot")
        provenance = dict(meta.get("provenance", {}))

        member_files = sorted(
            p.name for p in cdir.iterdir()
            if p.is_file() and p.name != "_collection.json"
        )

        member_titles_str = "\n".join(
            f"- {name}" for name in member_files
        ) or "(no member files yet)"

        if not declared_purpose:
            try:
                purpose = await self._llm.complete(
                    _COLLECTION_PURPOSE_PROMPT_TEMPLATE.format(
                        name=collection_name,
                        declared_purpose="(none)",
                        trust_tier=trust_tier,
                        member_titles=member_titles_str,
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "REFERENCE_COHORT_COLLECTION_LLM_FAILED dir=%s exc=%s",
                    collection_dir, exc,
                )
                purpose = collection_name
        else:
            purpose = declared_purpose
        purpose = _truncate_purpose(purpose)

        existing = await self._catalog.get_collection_entry(
            instance_id=self._instance_id,
            collection_name=collection_name,
            scope=scope,
        )
        first_create = existing is None

        entry = CatalogEntry(
            entry_id=existing.entry_id if existing else _new_entry_id(),
            instance_id=self._instance_id,
            entry_type=ENTRY_TYPE_COLLECTION,
            scope=scope,
            category=collection_name,
            indexed_at=utc_now(),
            trust_tier=trust_tier,
            auto_inducible=trust_tier != "quarantined",
            provenance_metadata=provenance,
            collection_name=collection_name,
            purpose=purpose,
            refresh_policy=refresh_policy,
            member_file_count=len(member_files),
            member_file_paths=member_files,
            last_refreshed_at=utc_now(),
            owner_domain_id=owner_domain_id,
        )
        await self._catalog.upsert_collection_entry(entry=entry)

        if first_create:
            await self._emitter.emit_collection_created(
                instance_id=self._instance_id,
                collection_name=collection_name,
                scope=scope,
                purpose=purpose,
                trust_tier=trust_tier,
                refresh_policy=refresh_policy,
                created_by=str(provenance.get("stored_by", "")),
            )
        else:
            await self._emitter.emit_collection_refreshed(
                instance_id=self._instance_id,
                collection_name=collection_name,
                scope=scope,
                member_file_count=len(member_files),
            )


# ---------------------------------------------------------------------------
# Truncation helpers
# ---------------------------------------------------------------------------


def _truncate_one_line(text: str) -> str:
    text = text.strip().splitlines()[0] if text.strip() else ""
    if len(text) <= ONE_LINE_TOKEN_CAP:
        return text
    return text[: ONE_LINE_TOKEN_CAP - 1] + "…"


def _truncate_purpose(text: str) -> str:
    text = text.strip()
    if len(text) <= 400:
        return text
    return text[:399] + "…"


__all__ = [
    "CatalogingCohort",
    "CatalogingLLMClient",
    "ONE_LINE_TOKEN_CAP",
    "PURPOSE_SUMMARY_TOKEN_CAP",
    "split_sections",
]
