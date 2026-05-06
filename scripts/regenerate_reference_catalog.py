#!/usr/bin/env python3
"""Regenerate the baked reference catalog under ``docs/_catalog/``.

Walks every Markdown file under ``docs/``, splits it into H2 sections,
and asks the cataloging cohort for a one-line summary per section.
Writes a per-file artifact and updates ``docs/_catalog/_manifest.json``.

This is the contributor-side regen path for REFERENCE-CATALOG-BAKED-V1
(architect-locked, Path B+, 2026-05-06). It is the **only** place
where LLM calls are spent against the canonical docs tree; the
runtime never re-spends those calls on every install or wipe.

Idempotent: reads the existing manifest first, computes the current
source-hash of each Markdown file, and skips files whose hash already
matches a recorded manifest entry. Touching one file regenerates one
file, not the whole tree.

Usage::

    python scripts/regenerate_reference_catalog.py

Environment::

    KERNOS_DATA_DIR (optional, default ./data) — only used to satisfy
    services that touch on-disk state during cohort init; no per-tenant
    data is read or written.

The script is fail-soft on individual sections: if the cataloging
cohort errors on one section, the section's one_line falls back to
its title (matching ``cohort._catalog_file``'s in-loop fallback) and
the artifact is written with the rest. CI's freshness check will
catch any stale or absent entries; loud diagnostics on every drift.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

# Local imports happen after sys.path is patched below to support
# running the script from the repo root without an editable install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Load .env so KERNOS_LLM_PROVIDER and friends populate before chain
# construction runs. Other entry points (cli.py, app.py, repl.py) do
# the same thing at module import; the regen script is no different.
try:
    from dotenv import load_dotenv as _load_dotenv  # noqa: E402
    _load_dotenv()
except ImportError:
    pass

from kernos.kernel.reference.baked import (  # noqa: E402
    MANIFEST_VERSION,
    BakedArtifact,
    BakedManifest,
    BakedSection,
    artifact_path_for,
    manifest_path_for,
    source_relpath_for,
    write_baked_artifact,
    write_manifest,
)
from kernos.kernel.reference.catalog import compute_file_hash  # noqa: E402
from kernos.kernel.reference.cohort import (  # noqa: E402
    _ONE_LINE_PROMPT_TEMPLATE,
    _truncate_one_line,
    split_sections,
)
from kernos.utils import utc_now  # noqa: E402

logger = logging.getLogger("regenerate_reference_catalog")


async def _summarize_section(
    *,
    title: str,
    body: str,
    llm_complete,  # async callable: (str) -> str
) -> str:
    """One LLM call per section. Falls back to title on failure."""
    try:
        raw = await llm_complete(
            _ONE_LINE_PROMPT_TEMPLATE.format(title=title, body=body),
        )
    except Exception as exc:
        logger.warning(
            "section summary failed for %r — falling back to title (%s)",
            title, exc,
        )
        return _truncate_one_line(title)
    return _truncate_one_line(raw)


def _section_body(text: str, line_start: int, line_end: int) -> str:
    """Slice the original text by the cohort's reported bounds."""
    lines = text.splitlines()
    return "\n".join(lines[line_start - 1 : line_end])


async def _build_llm_complete():
    """Wire the cataloging cohort's LLM adapter against the cheap chain.

    Reuses ``ReferenceCheapLLMAdapter`` so the prompt template and chain
    selection are identical to the live cohort's path. Any change in the
    runtime cataloging cost surfaces here too — single source of truth.
    """
    from kernos.kernel.credentials import resolve_openai_codex_credential
    from kernos.kernel.reasoning import ReasoningService
    from kernos.kernel.reference.bringup_adapters import ReferenceCheapLLMAdapter
    from kernos.providers.chains import build_chains_from_env

    # ReasoningService construction goes through the shared turn-runner
    # provider helper to satisfy REASONING-SERVICE-CONSTRUCTION-PARITY-V1.
    # The regen path doesn't run a turn loop, so a no-op turn runner is
    # adequate; only complete_simple is exercised.
    resolve_openai_codex_credential()
    chains, primary_provider = build_chains_from_env()
    from unittest.mock import AsyncMock, MagicMock
    events = MagicMock()
    events.emit = AsyncMock(return_value=None)
    mcp = MagicMock()
    mcp.get_tools = MagicMock(return_value=[])
    audit = MagicMock()
    audit.log = AsyncMock(return_value=None)
    reasoning = ReasoningService(
        primary_provider, events, mcp, audit,
        turn_runner_provider=lambda *a, **kw: None,  # not used
    )
    reasoning._chains = chains  # type: ignore[attr-defined]
    adapter = ReferenceCheapLLMAdapter(reasoning=reasoning)
    return adapter.complete


async def regenerate(
    *,
    repo_root: Path,
    docs_root: Path,
    catalog_root: Path,
    only_changed: bool = True,
    dry_run: bool = False,
) -> dict[str, int]:
    """Walk the docs tree, regenerate stale/missing artifacts, update manifest.

    Returns a small summary dict::

        {"scanned": N, "skipped_unchanged": K, "regenerated": M,
         "deleted": D, "sections": S}
    """
    summary = {
        "scanned": 0,
        "skipped_unchanged": 0,
        "regenerated": 0,
        "deleted": 0,
        "sections": 0,
    }

    # Read existing manifest (if any) for idempotency
    existing_entries: dict[str, dict[str, str]] = {}
    manifest_p = manifest_path_for(catalog_root)
    if manifest_p.exists():
        try:
            existing = BakedManifest.from_json(
                manifest_p.read_text(encoding="utf-8"),
            )
            existing_entries = dict(existing.entries)
        except Exception as exc:
            logger.warning(
                "existing manifest unreadable (%s) — regenerating from scratch",
                exc,
            )

    new_entries: dict[str, dict[str, str]] = {}
    on_disk: list[Path] = []
    for md in sorted(docs_root.rglob("*.md")):
        if md.is_file() and not md.name.startswith("_"):
            on_disk.append(md)

    summary["scanned"] = len(on_disk)
    if dry_run:
        logger.info(
            "DRY RUN — would scan %d files; no LLM calls, no writes",
            len(on_disk),
        )

    llm_complete = None  # lazy init only if work to do

    for source_p in on_disk:
        source_relpath = source_relpath_for(repo_root, source_p)
        current_hash = compute_file_hash(source_p)

        prev = existing_entries.get(source_relpath)
        if (
            only_changed
            and prev is not None
            and prev.get("source_hash") == current_hash
            and (repo_root / prev["artifact_path"]).exists()
        ):
            # Unchanged — preserve manifest entry, skip LLM work
            new_entries[source_relpath] = dict(prev)
            summary["skipped_unchanged"] += 1
            continue

        if dry_run:
            logger.info("would regenerate: %s", source_relpath)
            summary["regenerated"] += 1
            continue

        if llm_complete is None:
            llm_complete = await _build_llm_complete()

        body_bytes = source_p.read_bytes()
        body_text = body_bytes.decode("utf-8", errors="replace")
        sections_in = split_sections(file_text=body_text, file_path=str(source_p))

        sections_out: list[BakedSection] = []
        for title, line_start, line_end in sections_in:
            body = _section_body(body_text, line_start, line_end)
            one_line = await _summarize_section(
                title=title, body=body, llm_complete=llm_complete,
            )
            sections_out.append(
                BakedSection(
                    section_title=title,
                    one_line=one_line,
                    line_start=line_start,
                    line_end=line_end,
                ),
            )

        artifact = BakedArtifact(
            file_path=source_relpath,
            source_hash=current_hash,
            generated_at=utc_now(),
            sections=tuple(sections_out),
        )
        artifact_p = write_baked_artifact(
            catalog_root=catalog_root,
            source_relpath=source_relpath,
            artifact=artifact,
        )
        artifact_relpath = artifact_p.relative_to(repo_root).as_posix()
        new_entries[source_relpath] = {
            "source_hash": current_hash,
            "artifact_path": artifact_relpath,
        }
        summary["regenerated"] += 1
        summary["sections"] += len(sections_out)
        logger.info(
            "regenerated %s (%d sections) → %s",
            source_relpath, len(sections_out), artifact_relpath,
        )

    # Drop manifest entries (and corresponding artifacts) for files
    # that no longer exist on disk
    for rel in sorted(set(existing_entries.keys()) - {
        source_relpath_for(repo_root, p) for p in on_disk
    }):
        if dry_run:
            logger.info("would drop deleted entry: %s", rel)
            summary["deleted"] += 1
            continue
        old_artifact = repo_root / existing_entries[rel]["artifact_path"]
        try:
            if old_artifact.exists():
                old_artifact.unlink()
                logger.info("dropped artifact for deleted doc: %s", rel)
        except Exception as exc:
            logger.warning(
                "failed to drop stale artifact %s: %s", old_artifact, exc,
            )
        summary["deleted"] += 1

    if not dry_run:
        manifest = BakedManifest(
            version=MANIFEST_VERSION,
            generated_at=utc_now(),
            entries=new_entries,
        )
        write_manifest(catalog_root=catalog_root, manifest=manifest)
        logger.info(
            "manifest written: %d entries (regenerated=%d skipped=%d deleted=%d sections=%d)",
            len(new_entries), summary["regenerated"],
            summary["skipped_unchanged"], summary["deleted"], summary["sections"],
        )

    return summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Regenerate the baked reference catalog under docs/_catalog/. "
            "Idempotent: re-runs only catalog work on files whose source "
            "hash has changed."
        )
    )
    p.add_argument(
        "--all",
        action="store_true",
        help=(
            "Force regeneration of every file, even if the source hash "
            "is unchanged. Default: only-changed."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be regenerated without making LLM calls or writing.",
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help="Repository root (default: detected from script location)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    docs_root = args.repo_root / "docs"
    catalog_root = docs_root / "_catalog"
    if not docs_root.is_dir():
        logger.error("docs root not found at %s", docs_root)
        return 2

    summary = asyncio.run(
        regenerate(
            repo_root=args.repo_root,
            docs_root=docs_root,
            catalog_root=catalog_root,
            only_changed=not args.all,
            dry_run=args.dry_run,
        )
    )
    print(
        f"\nREGEN_SUMMARY: scanned={summary['scanned']} "
        f"regenerated={summary['regenerated']} "
        f"skipped_unchanged={summary['skipped_unchanged']} "
        f"deleted={summary['deleted']} sections={summary['sections']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
