#!/usr/bin/env python3
"""Hash-only freshness check for the baked reference catalog.

Walks ``docs/`` and verifies every Markdown file has a fresh entry
in ``docs/_catalog/_manifest.json`` whose source_hash matches the
file's current contents. Verifies the per-file artifacts referenced
by the manifest exist and their recorded source_hash matches too.

**This script never spends LLM calls.** That is principle (2) of the
REFERENCE-CATALOG-BAKED-V1 architect verdict: CI verifies the
contributor ran regen locally; CI never re-spends the cost itself.

Exit codes::

    0 — all fresh
    1 — drift detected (script prints diagnostics)
    2 — usage error (e.g., docs/ not found)

Usage::

    python scripts/check_reference_catalog_freshness.py

Wire into pre-commit, CI, or any other gate. The standalone script
is intentionally framework-agnostic.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from kernos.kernel.reference.baked import check_freshness  # noqa: E402

logger = logging.getLogger("check_reference_catalog_freshness")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Hash-only freshness check for the baked reference catalog."
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
        format="%(message)s",
    )
    args = _parse_args(argv)
    docs_root = args.repo_root / "docs"
    catalog_root = docs_root / "_catalog"
    if not docs_root.is_dir():
        print(f"docs root not found at {docs_root}", file=sys.stderr)
        return 2

    fresh, diagnostics = check_freshness(
        docs_root=docs_root,
        catalog_root=catalog_root,
    )
    if fresh:
        print("baked reference catalog is fresh")
        return 0

    print("baked reference catalog drift detected:", file=sys.stderr)
    for d in diagnostics:
        print(f"  - {d}", file=sys.stderr)
    print(
        "\nrun `python scripts/regenerate_reference_catalog.py` to refresh.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
