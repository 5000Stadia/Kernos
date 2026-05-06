#!/usr/bin/env python3
"""REFERENCE-CATALOG-BAKED-V1 — embedded live test.

Three scenarios from the architect verdict's acceptance list:

1. Clean install loads from the baked artifact in milliseconds.
2. Partial drift (one doc mutated post-bake) surfaces a loud diagnostic
   and the runtime catalog ends up populated for fresh files only.
3. The freshness gate catches stale baked content (hash-only, no LLM).

The test stands up a synthetic temp docs tree, bakes a synthetic
manifest + per-file artifacts (no real cataloging cohort invoked),
and exercises ``load_baked_catalog`` + ``check_freshness`` end to
end. Writes its report to ``data/diagnostics/live-tests/`` matching
the existing live-test convention.

Run::

    python scripts/baked_catalog_live_test.py
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import time
from pathlib import Path
from tempfile import mkdtemp

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from kernos.kernel.reference.baked import (  # noqa: E402
    BakedArtifact,
    BakedManifest,
    BakedSection,
    CATALOG_DIRNAME,
    MANIFEST_VERSION,
    artifact_path_for,
    check_freshness,
    load_baked_catalog,
    source_relpath_for,
    write_baked_artifact,
    write_manifest,
)
from kernos.kernel.reference.catalog import (  # noqa: E402
    CatalogStore,
    SCOPE_INSTANCE,
    TRUST_CANONICAL,
    compute_file_hash,
)
from kernos.utils import utc_now  # noqa: E402

logger = logging.getLogger("baked_catalog_live_test")


async def _run() -> int:
    """Returns 0 on full success, 1 if any scenario failed."""
    failures: list[str] = []

    work = Path(mkdtemp(prefix="baked_live_"))
    try:
        repo = work
        docs = repo / "docs"
        catalog = docs / CATALOG_DIRNAME
        data = repo / "data"

        # --- Setup: 3 docs files + matching baked artifacts ---
        files: dict[str, Path] = {}
        artifacts: dict[str, BakedArtifact] = {}
        for name, body in [
            ("a.md", "## A\n\nbody A\n"),
            ("subdir/b.md", "## B\n\nbody B\n"),
            ("subdir/c.md", "## C\n\nbody C\n## D\n\nbody D\n"),
        ]:
            p = docs / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
            files[name] = p

        manifest_entries: dict[str, dict[str, str]] = {}
        for name, p in files.items():
            sections = []
            text = p.read_text(encoding="utf-8")
            line = 0
            current_title = None
            current_start = None
            collected = []
            for i, ln in enumerate(text.splitlines(), start=1):
                if ln.startswith("## "):
                    if current_title is not None:
                        collected.append(
                            BakedSection(current_title, f"summary of {current_title}",
                                         current_start, i - 1)
                        )
                    current_title = ln[3:].strip()
                    current_start = i
            if current_title is not None:
                collected.append(
                    BakedSection(current_title, f"summary of {current_title}",
                                 current_start, len(text.splitlines()))
                )
            artifact = BakedArtifact(
                file_path=source_relpath_for(repo, p),
                source_hash=compute_file_hash(p),
                generated_at=utc_now(),
                sections=tuple(collected),
            )
            artifact_p = write_baked_artifact(
                catalog_root=catalog,
                source_relpath=source_relpath_for(repo, p),
                artifact=artifact,
            )
            artifacts[name] = artifact
            manifest_entries[source_relpath_for(repo, p)] = {
                "source_hash": artifact.source_hash,
                "artifact_path": artifact_p.relative_to(repo).as_posix(),
            }

        write_manifest(
            catalog_root=catalog,
            manifest=BakedManifest(
                version=MANIFEST_VERSION,
                generated_at=utc_now(),
                entries=manifest_entries,
            ),
        )

        # --- Scenario 1: clean install loads in milliseconds ---
        store = CatalogStore()
        await store.start(str(data))
        try:
            t0 = time.perf_counter()
            summary = await load_baked_catalog(
                docs_root=docs,
                catalog_root=catalog,
                instance_id="live_test",
                catalog_store=store,
                scope=SCOPE_INSTANCE,
                trust_tier=TRUST_CANONICAL,
                owner_domain_id="",
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            print(
                f"SCENARIO 1: clean-install load: "
                f"loaded={summary.files_loaded} sections={summary.sections_imported} "
                f"elapsed={elapsed_ms:.1f}ms"
            )
            if summary.files_loaded != 3:
                failures.append(
                    f"clean install: expected 3 files loaded, got {summary.files_loaded}"
                )
            if summary.sections_imported != 4:
                failures.append(
                    f"clean install: expected 4 sections imported, got {summary.sections_imported}"
                )
            if elapsed_ms > 1000:
                failures.append(
                    f"clean install: hydration took {elapsed_ms:.1f}ms, expected <1000ms"
                )
        finally:
            await store.stop()

        # --- Scenario 2: partial drift, mixed-mode ---
        # Mutate one file post-bake; rebuild a fresh store; load again.
        files["a.md"].write_text("## A\n\nMUTATED BODY\n", encoding="utf-8")
        store2 = CatalogStore()
        await store2.start(str(data / "scenario2"))
        try:
            summary2 = await load_baked_catalog(
                docs_root=docs,
                catalog_root=catalog,
                instance_id="live_test_2",
                catalog_store=store2,
                scope=SCOPE_INSTANCE,
                trust_tier=TRUST_CANONICAL,
                owner_domain_id="",
            )
            print(
                f"SCENARIO 2: partial-drift load: "
                f"loaded={summary2.files_loaded} stale={summary2.files_stale}"
            )
            if summary2.files_loaded != 2:
                failures.append(
                    f"partial drift: expected 2 fresh files, got {summary2.files_loaded}"
                )
            if summary2.files_stale != 1:
                failures.append(
                    f"partial drift: expected 1 stale file, got {summary2.files_stale}"
                )
            stale_rows = await store2.list_entries_for_file(
                instance_id="live_test_2", file_path=str(files["a.md"]),
            )
            if stale_rows:
                failures.append(
                    f"partial drift: stale file should not be in catalog "
                    f"(found {len(stale_rows)} rows)"
                )
        finally:
            await store2.stop()

        # --- Scenario 3: freshness gate detects drift ---
        fresh, diagnostics = check_freshness(docs_root=docs, catalog_root=catalog)
        print(
            f"SCENARIO 3: freshness check: "
            f"fresh={fresh} diagnostics={len(diagnostics)}"
        )
        if fresh:
            failures.append(
                "freshness gate: should detect drift in scenario 2's mutated file"
            )
        if not any("stale" in d for d in diagnostics):
            failures.append(
                f"freshness gate: expected 'stale' diagnostic, got {diagnostics!r}"
            )

    finally:
        shutil.rmtree(work, ignore_errors=True)

    # --- Report ---
    out_dir = _REPO_ROOT / "data" / "diagnostics" / "live-tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = utc_now().replace(":", "-").replace("+00:00", "Z")
    report = out_dir / f"baked_catalog_live_{timestamp}.txt"
    if failures:
        report.write_text(
            "REFERENCE-CATALOG-BAKED-V1 LIVE TEST: FAIL\n\n"
            + "\n".join(f"- {f}" for f in failures)
            + "\n",
            encoding="utf-8",
        )
        print(f"\nFAIL ({len(failures)} issue(s)) — see {report}")
        for f in failures:
            print(f"  - {f}")
        return 1

    report.write_text(
        "REFERENCE-CATALOG-BAKED-V1 LIVE TEST: PASS\n\n"
        "All three scenarios passed:\n"
        "  - Clean install loads from baked in milliseconds.\n"
        "  - Partial drift skips stale entries with loud diagnostics.\n"
        "  - Freshness gate detects drift via hash-only check.\n",
        encoding="utf-8",
    )
    print(f"\nPASS — report at {report}")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
