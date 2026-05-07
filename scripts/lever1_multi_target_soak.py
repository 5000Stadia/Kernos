#!/usr/bin/env python3
"""LEVER-1 multi-target retrieval soak — substrate-fidelity validation.

Drives the live `request_reference` tool against the actual baked
catalog with a real navigator (whatever chain build_chains_from_env
selects: gpt-5.4-mini via Codex, falling back to ollama). Five
multi-topic briefs are issued; for each, the script logs:

    - navigator wall-time
    - raw navigator output (first 200 chars)
    - parsed entry_ids (deduped, ranked)
    - returned entries: status + section_title per pick

Compares max_targets=1 (legacy single-pick) vs max_targets=3 (lever 1)
on the same briefs to show the architectural shift's effect at the
substrate level — without going through the full handler / integration
runner / presence pipeline (those were broken by an unrelated Codex
auth issue at the time of writing).

Usage:

    .venv/bin/python scripts/lever1_multi_target_soak.py

Output: a markdown report at
``data/diagnostics/live-tests/LEVER1-MULTI-TARGET-SOAK-<ts>.md``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv as _load_dotenv  # noqa: E402
    _load_dotenv()
except ImportError:
    pass


PROBES: list[tuple[str, str, int]] = [
    # (label, brief, expected K-or-more matches)
    ("cohorts + domains + members",
     "How do cohorts work, how do they relate to domains, "
     "and what are members?", 3),
    ("gate + dispatch + safety",
     "How does the gate decide what's destructive, how does "
     "dispatch route tools, and what's the safety policy?", 3),
    ("hatching + members + spaces",
     "How does hatching work, what is a member, and how do "
     "spaces relate?", 3),
    ("simple single-topic",
     "What does the gate do?", 1),
    ("ambiguous brief",
     "Tell me about substrate", 2),
]


async def _run_one(
    *,
    service,
    ctx,
    brief: str,
    max_targets: int,
    nav_log: list[str],
) -> dict:
    """Issue a single request_reference call; return timing + outcome."""
    start = time.monotonic()
    result = await service.handle_request_reference(
        ctx=ctx, brief_request=brief, max_targets=max_targets,
    )
    elapsed = time.monotonic() - start
    return {
        "max_targets": max_targets,
        "wall_seconds": round(elapsed, 3),
        "status": result.get("status"),
        "entries": [
            {
                "entry_id": e.get("entry_id"),
                "section_title": e.get("section_title"),
                "status": e.get("status"),
                "content_chars": len(e.get("content") or ""),
            }
            for e in (result.get("entries") or [])
        ],
        "nav_raw_first": nav_log[-1][:300] if nav_log else "",
    }


async def main() -> int:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = _REPO_ROOT / "data" / "diagnostics" / "live-tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"LEVER1-MULTI-TARGET-SOAK-{timestamp}.md"

    data_dir = Path(tempfile.mkdtemp(prefix="lever1_soak_"))
    os.environ["KERNOS_DATA_DIR"] = str(data_dir)

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    print(f"\n=== LEVER1 SOAK (data_dir={data_dir}) ===\n",
          file=sys.stderr, flush=True)

    # Boot the dev handler so wire_reference_substrate runs and
    # builds a ReferenceService with the live navigator-LLM adapter.
    from kernos.repl import build_dev_handler
    handler = await build_dev_handler(
        data_dir=str(data_dir),
        instance_id="lever1:soak",
        sender="operator",
        sender_display_name="soaker",
    )

    service = getattr(handler, "_reference_service", None)
    if service is None:
        print("ERROR: handler did not bind _reference_service",
              file=sys.stderr, flush=True)
        return 1

    # Capture navigator prompts so we can include the first ~200
    # chars of each call's raw response in the report (the navigator
    # wraps the live LLM via ReferenceCheapLLMAdapter).
    nav_log: list[str] = []
    original_complete = service._navigator.complete

    async def _wrapped_complete(prompt: str) -> str:
        result = await original_complete(prompt)
        nav_log.append(str(result))
        return result

    service._navigator.complete = _wrapped_complete  # type: ignore

    # Bind the dispatch ctx to a real space so the catalog visibility
    # rule resolves consistently.
    from kernos.kernel.reference.tools import ReferenceServiceContext
    ctx = ReferenceServiceContext(
        instance_id="lever1:soak",
        domain_id="space:lever1",
        member_id="m1",
    )

    # Run each probe in BOTH modes back-to-back so wall-time
    # comparisons are apples-to-apples (same catalog, same brief).
    sections: list[str] = []
    sections.append(
        f"# LEVER1 multi-target retrieval soak — {timestamp}\n\n"
        f"**Data dir:** `{data_dir}`\n\n"
        f"Each probe runs twice: once with max_targets=1 (legacy "
        f"single-pick) and once with max_targets=3 (lever 1). Same "
        f"brief, same catalog. Lever 1 should return up to 3 "
        f"distinct entries in one navigator call where the legacy "
        f"path would have required the agent to issue 3 sequential "
        f"calls.\n\n"
    )

    summary_rows: list[tuple[str, float, float, int, int]] = []
    for label, brief, expected_k in PROBES:
        sections.append(f"## {label}\n\n")
        sections.append(f"**Brief:** {brief}\n\n")

        legacy = await _run_one(
            service=service, ctx=ctx, brief=brief,
            max_targets=1, nav_log=nav_log,
        )
        sections.append(
            f"### Legacy (max_targets=1)\n"
            f"- wall_seconds: {legacy['wall_seconds']}\n"
            f"- status: {legacy['status']}\n"
            f"- entries: {legacy['entries']}\n"
            f"- nav_raw (first 300): `{legacy['nav_raw_first']!r}`\n\n"
        )

        lever1 = await _run_one(
            service=service, ctx=ctx, brief=brief,
            max_targets=3, nav_log=nav_log,
        )
        sections.append(
            f"### Lever 1 (max_targets=3)\n"
            f"- wall_seconds: {lever1['wall_seconds']}\n"
            f"- status: {lever1['status']}\n"
            f"- entries ({len(lever1['entries'])}): "
            f"{lever1['entries']}\n"
            f"- nav_raw (first 300): `{lever1['nav_raw_first']!r}`\n\n"
        )

        summary_rows.append((
            label,
            legacy["wall_seconds"],
            lever1["wall_seconds"],
            len(legacy["entries"]),
            len(lever1["entries"]),
        ))

    sections.append("## Summary\n\n")
    sections.append(
        "| Probe | legacy wall_s | lever1 wall_s | legacy K | lever1 K |\n"
        "|---|---:|---:|---:|---:|\n"
    )
    for row in summary_rows:
        sections.append(
            f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} |\n"
        )

    # Architectural-intent assertion: lever 1 should return MORE
    # entries on at least the multi-topic probes, in roughly the
    # SAME wall-time as a single legacy call (one navigator LLM
    # round-trip serves K picks).
    multi_topic_rows = [r for r in summary_rows if r[0].startswith(
        ("cohorts", "gate", "hatching"),
    )]
    lever1_returns_more = sum(
        1 for r in multi_topic_rows if r[4] > r[3]
    )
    sections.append(
        f"\n**Multi-topic probes where lever 1 returned more "
        f"entries than legacy:** {lever1_returns_more}/"
        f"{len(multi_topic_rows)}\n\n"
    )
    if multi_topic_rows:
        avg_legacy = (
            sum(r[1] for r in multi_topic_rows) / len(multi_topic_rows)
        )
        avg_lever1 = (
            sum(r[2] for r in multi_topic_rows) / len(multi_topic_rows)
        )
        sections.append(
            f"**Avg multi-topic wall-time** — legacy: "
            f"{avg_legacy:.2f}s, lever 1: {avg_lever1:.2f}s.\n\n"
        )

    report_path.write_text("".join(sections), encoding="utf-8")
    print(f"\n=== TRANSCRIPT: {report_path} ===\n",
          file=sys.stderr, flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
