"""Pins for the canonical-introduction surface.

REFERENCE-PRIMITIVE-V1 retargeted these pins. Pre-spec they were
guarded read_doc('kernos-introduction.md'); post-spec the reach
mechanism is request_reference, but the load-bearing invariants
remain:

* The shipped surface (canonical doc + README link + the agent's
  self-description guidance + the reach mechanism's source code)
  must be Notion-independent — Kernos's documentation must
  continue working without any Notion dependency.
* The self-description guidance routes to the canonical
  introduction via the new reach mechanism (request_reference);
  the previous deprecated `identity/about-kernos.md` target is
  gone.
* The canonical doc cross-links to architecture content so the
  agent's discovery from the introduction page works.

Reach-mechanism unit testing (the old "read_doc actually returns
content" pin) is now covered by the reference-primitive tests +
the embedded live test."""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_ROOT = REPO_ROOT / "docs"
CANONICAL_DOC = DOCS_ROOT / "kernos-introduction.md"


FORBIDDEN_NOTION_TOKENS = (
    "notion.so",
    "notion.com",
    "www.notion.",
)


def _scan_for_notion(text: str) -> list[str]:
    lowered = text.lower()
    return [tok for tok in FORBIDDEN_NOTION_TOKENS if tok in lowered]


# ---------------------------------------------------------------------------
# Pin 1: Notion-leakage — structural scan of the shipped surface
# ---------------------------------------------------------------------------


def test_canonical_introduction_has_no_notion_reference():
    assert CANONICAL_DOC.exists(), (
        "docs/kernos-introduction.md must exist (DOCS-INTRO C1 artifact)"
    )
    content = CANONICAL_DOC.read_text(encoding="utf-8")
    leaks = _scan_for_notion(content)
    assert not leaks, (
        f"Notion leakage in canonical introduction: {leaks}. The "
        f"shipped doc must be Notion-independent."
    )


def test_readme_canonical_link_has_no_notion_reference():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Canonical introduction" in readme
    canonical_anchor_idx = readme.find("Canonical introduction")
    nearby = readme[canonical_anchor_idx : canonical_anchor_idx + 500]
    leaks = _scan_for_notion(nearby)
    assert not leaks, (
        f"Notion leakage near README canonical-intro link: {leaks}"
    )
    assert "docs/kernos-introduction.md" in nearby


def test_docs_index_canonical_pointer_has_no_notion_reference():
    index = (DOCS_ROOT / "index.md").read_text(encoding="utf-8")
    assert "kernos-introduction.md" in index
    leaks = _scan_for_notion(index)
    assert not leaks


def test_self_description_template_has_no_notion_reference():
    """The agent's self-description guidance in template.py must
    not contain any Notion reference. The reach mechanism is now
    request_reference; the local-only invariant still holds."""
    from kernos.kernel import template

    src = inspect.getsource(template)
    leaks = _scan_for_notion(src)
    assert not leaks, (
        f"Notion leakage in kernos/kernel/template.py: {leaks}"
    )


def test_reference_primitive_tools_have_no_notion_reference():
    """REFERENCE-PRIMITIVE-V1: the request_reference tool's owning
    module replaces read_doc as the reach mechanism. Same Notion-
    independence invariant applies — the catalog routes through
    local-only paths, never Notion."""
    from kernos.kernel.reference import tools as reference_tools

    src = inspect.getsource(reference_tools)
    leaks = _scan_for_notion(src)
    assert not leaks, (
        f"Notion leakage in kernos/kernel/reference/tools.py: {leaks}"
    )


# ---------------------------------------------------------------------------
# Pin 2: self-description template routes to the canonical introduction
# via the new reach mechanism (request_reference)
# ---------------------------------------------------------------------------


def test_template_routes_self_description_via_request_reference():
    """REFERENCE-PRIMITIVE-V1: the IDENTITY guidance in template.py
    routes to request_reference (the new reach mechanism), not the
    retired read_doc."""
    from kernos.kernel import template

    src = inspect.getsource(template)
    assert "request_reference(" in src, (
        "template.py must route self-description via request_reference"
    )
    # The retired direct-path call must be gone.
    assert "read_doc('kernos-introduction.md')" not in src
    assert "read_doc('identity/about-kernos.md')" not in src


# ---------------------------------------------------------------------------
# Pin 3: cross-links in the canonical doc resolve to real files
# ---------------------------------------------------------------------------


def test_canonical_introduction_cross_links_resolve():
    """The canonical introduction cross-links to architecture
    content; that link must point at an existing file so an agent
    that follows the link doesn't hit a dead end."""
    content = CANONICAL_DOC.read_text(encoding="utf-8")
    assert "architecture/" in content
    # At least one architecture/*.md path mentioned in the doc must
    # actually exist.
    import re
    arch_paths = re.findall(r"architecture/[\w\-./]+\.md", content)
    assert arch_paths, "canonical introduction must reference architecture/*.md"
    matched_existing = [
        p for p in arch_paths if (DOCS_ROOT / p).exists()
    ]
    assert matched_existing, (
        f"None of the architecture cross-links resolve: {arch_paths}"
    )


# ---------------------------------------------------------------------------
# Pin 4: read_doc retirement is structural — the old surface is gone
# ---------------------------------------------------------------------------


def test_read_doc_tool_retired_from_kernel_catalog():
    """REFERENCE-PRIMITIVE-V1: read_doc must no longer be in the
    kernel-tool catalog. request_reference replaces it."""
    from kernos.kernel.kernel_tool_registry import kernel_tool_schemas

    names = {s["name"] for s in kernel_tool_schemas()}
    assert "read_doc" not in names, (
        "read_doc was retired in REFERENCE-PRIMITIVE-V1; the "
        "kernel-tool catalog must not list it."
    )
    assert "request_reference" in names


def test_read_doc_helper_function_retired():
    """The pure helper function _read_doc / read_doc must no longer
    be importable from kernos.kernel.tools."""
    import kernos.kernel.tools as kt
    assert not hasattr(kt, "read_doc"), (
        "tools.read_doc must be removed; reference primitive owns "
        "documentation reach now."
    )
    # And the schema constant is gone too.
    assert not hasattr(kt, "READ_DOC_TOOL")
