"""Probe 2 — self_knowledge_invariant (SUBSTRATE-SELF-TEST-V1).

Asserts read_source can reach specs/, docs/, and kernos/ paths,
that bare paths still resolve to kernos/ for back-compat, and
that path-traversal is still rejected.

Regression bug: 07226c8. Kernos couldn't read its own specs
because read_source was scoped to kernos/ only — blocked
substrate-spec alignment checks until repaired.
"""
from __future__ import annotations

import importlib
import time
from pathlib import Path

from kernos.kernel.self_test_gate import ProbeResult
from kernos.kernel.tools.schemas import read_source


REQUIRED_BEHAVIORAL_KEYS = frozenset({
    "spec_read_result",
    "doc_read_result",
    "kernos_read_result",
    "traversal_reject_result",
})

REQUIRED_SUBSTRATE_KEYS = frozenset({
    "allowed_roots",
    "repo_root_resolved",
})


def _compute_allowed_roots() -> dict:
    """Reproduce the allowed-root computation read_source uses, so
    the probe can assert against the substrate's own truth rather
    than hardcoding paths."""
    kernos_root = Path(
        importlib.import_module("kernos").__file__,
    ).parent
    repo_root = kernos_root.parent
    return {
        "kernos": str(kernos_root),
        "specs": str(repo_root / "specs"),
        "docs": str(repo_root / "docs"),
        "repo_root": str(repo_root),
    }


async def run_probe() -> ProbeResult:
    start = time.monotonic()

    allowed_roots = _compute_allowed_roots()

    # Pick a spec file we know exists. Use SUBSTRATE-SELF-TEST-V1
    # itself — it's load-bearing for this very probe to verify.
    spec_path = "specs/SUBSTRATE-SELF-TEST-V1.md"
    spec_result = read_source(path=spec_path)

    # docs/ — TECHNICAL-ARCHITECTURE.md is the long-standing
    # canonical reference per CLAUDE.md.
    doc_path = "docs/TECHNICAL-ARCHITECTURE.md"
    doc_result = read_source(path=doc_path)

    # Bare-path back-compat — kernel/awareness.py resolves to
    # kernos/kernel/awareness.py per the back-compat fallback.
    kernos_path = "kernel/awareness.py"
    kernos_result = read_source(path=kernos_path)

    # Security boundary — traversal must still reject.
    traversal_result = read_source(path="../etc/passwd")

    duration_ms = int((time.monotonic() - start) * 1000)

    # Pass conditions:
    # - spec read returns content beginning with the spec title
    # - doc read returns non-error content
    # - kernos read returns content with class/def
    # - traversal returns "not allowed" error
    spec_ok = (
        not spec_result.startswith("Error")
        and "SUBSTRATE-SELF-TEST-V1" in spec_result
    )
    doc_ok = not doc_result.startswith("Error")
    kernos_ok = (
        not kernos_result.startswith("Error")
        and ("class" in kernos_result or "def" in kernos_result)
    )
    traversal_rejected = (
        traversal_result.startswith("Error")
        and "not allowed" in traversal_result
    )

    all_passed = spec_ok and doc_ok and kernos_ok and traversal_rejected

    failure_reason = ""
    if not all_passed:
        failed = []
        if not spec_ok:
            failed.append(f"spec_read({spec_path})")
        if not doc_ok:
            failed.append(f"doc_read({doc_path})")
        if not kernos_ok:
            failed.append(f"kernos_read({kernos_path})")
        if not traversal_rejected:
            failed.append("traversal_reject")
        failure_reason = (
            f"self-knowledge invariant violated: {', '.join(failed)} "
            f"did not behave as expected. Likely regression of 07226c8 "
            f"(read_source scope) or security check."
        )

    return ProbeResult(
        probe_name="self_knowledge_invariant",
        passed=all_passed,
        behavioral_evidence={
            "spec_read_result": (
                "ok" if spec_ok else f"FAILED: {spec_result[:200]}"
            ),
            "doc_read_result": (
                "ok" if doc_ok else f"FAILED: {doc_result[:200]}"
            ),
            "kernos_read_result": (
                "ok" if kernos_ok else f"FAILED: {kernos_result[:200]}"
            ),
            "traversal_reject_result": (
                "rejected" if traversal_rejected
                else f"NOT-REJECTED: {traversal_result[:200]}"
            ),
        },
        substrate_evidence={
            "allowed_roots": allowed_roots,
            "repo_root_resolved": allowed_roots["repo_root"],
        },
        duration_ms=duration_ms,
        failure_reason=failure_reason,
    )
