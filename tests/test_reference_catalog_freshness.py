"""REFERENCE-CATALOG-BAKED-V1 — freshness gate run inside pytest.

Architect verdict principle (2): the freshness check spends zero LLM
calls. This file ports the standalone hash-comparison check at
``scripts/check_reference_catalog_freshness.py`` into pytest so the
existing test infrastructure becomes the CI surface — `pytest`
itself fails when a docs change is committed without running regen.

Bootstrap exemption: when the manifest is absent (the repository has
not yet been bootstrapped with a baked catalog), the test is skipped
rather than failed. This keeps the gate from blocking the very first
commit that introduces the manifest. After the first regen, the gate
becomes load-bearing — every subsequent docs change must run regen.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kernos.kernel.reference.baked import (
    check_freshness,
    manifest_path_for,
)


_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCS_ROOT = _REPO_ROOT / "docs"
_CATALOG_ROOT = _DOCS_ROOT / "_catalog"


def test_baked_reference_catalog_is_fresh():
    """Hash-only freshness gate. No LLM calls; cheap; runs every pytest.

    Skips with a clear message if the baked catalog has not yet been
    bootstrapped (manifest absent). After bootstrap, any drift between
    docs/ and the baked manifest fails this test loudly.
    """
    if not _DOCS_ROOT.is_dir():
        pytest.skip(f"docs/ not present at {_DOCS_ROOT}")
    manifest_p = manifest_path_for(_CATALOG_ROOT)
    if not manifest_p.exists():
        pytest.skip(
            "baked reference catalog not bootstrapped yet — run "
            "`python scripts/regenerate_reference_catalog.py` to seed. "
            "After the first run, this test enforces freshness on every commit."
        )

    fresh, diagnostics = check_freshness(
        docs_root=_DOCS_ROOT,
        catalog_root=_CATALOG_ROOT,
    )
    assert fresh, (
        "baked reference catalog is stale relative to docs/.\n\n"
        + "\n".join(f"  - {d}" for d in diagnostics)
        + "\n\nrun `python scripts/regenerate_reference_catalog.py` to refresh."
    )
