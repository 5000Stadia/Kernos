"""Pins for the DOCS-INTRODUCTION-INTEGRATION-ARC surface (DOCS-INTRO C2).

Two structural pins + one self-description template pin:

  1. Notion-leakage pin (the design review edit 3): scans the SHIPPED surface
     (canonical doc + README link + reach mechanism code path) for
     notion.so / notion.com / Notion-URL references. Fails loudly
     on leak. Structural enforcement, not review-time inspection.

  2. Self-description template pin: confirms the operating-principles
     template routes "what are you?" reads via read_doc to
     `kernos-introduction.md` (the new canonical), NOT to the
     deprecated `identity/about-kernos.md` target.

  3. read_doc reach pin: confirms that calling
     read_doc("kernos-introduction.md") returns the canonical doc
     content (not an error or a fallback). Verifies the reach
     mechanism is actually wired to the right target.

The Notion-leakage pin is the load-bearing one — Kernos's
documentation surface ships from the local repo and must continue
working without any Notion dependency.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_ROOT = REPO_ROOT / "docs"
CANONICAL_DOC = DOCS_ROOT / "kernos-introduction.md"


# Forbidden tokens — any reference to a Notion URL or Notion
# integration in the shipped surface fails the pin.
FORBIDDEN_NOTION_TOKENS = (
    "notion.so",
    "notion.com",
    "www.notion.",
)


# ---------------------------------------------------------------------------
# Pin 1: Notion-leakage — structural scan of the shipped surface
# ---------------------------------------------------------------------------


def _scan_for_notion(text: str) -> list[str]:
    """Return list of forbidden Notion tokens found in the text.

    Case-insensitive. The scan is over the full file content, not
    line-by-line, so multi-token URLs like "https://notion.so/..."
    are caught even when they cross word boundaries."""
    lowered = text.lower()
    return [tok for tok in FORBIDDEN_NOTION_TOKENS if tok in lowered]


def test_canonical_introduction_has_no_notion_reference():
    """The canonical introduction document does not contain any
    Notion URL or notion.so / notion.com reference. Notion is not
    part of the shipped documentation surface."""
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
    """The README link to the canonical introduction does not route
    via Notion. The link must point to the local repo path
    `docs/kernos-introduction.md`, not a Notion URL."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    # Find the section containing the canonical-intro link reference.
    # The link text "Canonical introduction" anchors the search.
    assert "Canonical introduction" in readme, (
        "README must reference the canonical introduction"
    )
    # The link itself must use the docs/ relative path; reject any
    # version that routes through Notion.
    canonical_anchor_idx = readme.find("Canonical introduction")
    nearby = readme[canonical_anchor_idx : canonical_anchor_idx + 500]
    leaks = _scan_for_notion(nearby)
    assert not leaks, (
        f"Notion leakage near README canonical-intro link: {leaks}"
    )
    # Positive pin: the link points at the local docs path.
    assert "docs/kernos-introduction.md" in nearby, (
        "README canonical-intro link must target docs/kernos-introduction.md"
    )


def test_docs_index_canonical_pointer_has_no_notion_reference():
    """The docs/index.md pointer to the canonical introduction does
    not route via Notion."""
    index = (DOCS_ROOT / "index.md").read_text(encoding="utf-8")
    assert "kernos-introduction.md" in index, (
        "docs/index.md must reference the canonical introduction"
    )
    leaks = _scan_for_notion(index)
    assert not leaks, (
        f"Notion leakage in docs/index.md: {leaks}"
    )


def test_self_description_template_has_no_notion_reference():
    """The agent's self-description guidance in template.py does not
    contain any Notion reference. The reach mechanism routes via
    read_doc against a local path."""
    from kernos.kernel import template

    src = inspect.getsource(template)
    leaks = _scan_for_notion(src)
    assert not leaks, (
        f"Notion leakage in kernos/kernel/template.py: {leaks}"
    )


def test_read_doc_tool_implementation_has_no_notion_reference():
    """The read_doc tool code path is the reach mechanism. Its
    implementation (kernos/kernel/tools/schemas.py) must not route
    through Notion."""
    from kernos.kernel.tools import schemas

    src = inspect.getsource(schemas)
    leaks = _scan_for_notion(src)
    assert not leaks, (
        f"Notion leakage in kernos/kernel/tools/schemas.py: {leaks}"
    )


# ---------------------------------------------------------------------------
# Pin 2: self-description template routes to kernos-introduction.md
# ---------------------------------------------------------------------------


def test_template_routes_self_description_to_canonical_introduction():
    """Per the design review edit 2: when asked 'what are you?', the agent's
    standing instructions point at read_doc('kernos-introduction.md').
    NOT the deprecated identity/about-kernos.md target."""
    from kernos.kernel import template

    src = inspect.getsource(template)
    # Positive pin: the canonical reach is referenced.
    assert "read_doc('kernos-introduction.md')" in src, (
        "template.py must route self-description via "
        "read_doc('kernos-introduction.md')"
    )
    # Negative pin: the previous deprecated reach target is gone.
    assert "read_doc('identity/about-kernos.md')" not in src, (
        "template.py must not still reference the deprecated "
        "identity/about-kernos.md target. Update the IDENTITY block."
    )


# ---------------------------------------------------------------------------
# Pin 3: read_doc reach mechanism returns canonical content
# ---------------------------------------------------------------------------


def test_read_doc_reaches_kernos_introduction_canonical():
    """The reach mechanism (existing read_doc tool) actually returns
    the canonical introduction content when invoked with the
    target the template specifies. No fallback, no error, no
    placeholder — the doc is reachable as the template promises."""
    from kernos.kernel.tools import read_doc

    content = read_doc("kernos-introduction.md")
    # Negative pins: not an error response.
    assert not content.startswith("Error:"), (
        f"read_doc returned an error response: {content[:200]}"
    )
    # Positive pin: content is the canonical doc.
    assert "Kernos — Introduction" in content, (
        "read_doc('kernos-introduction.md') must return the canonical "
        "introduction with the documented title"
    )
    # The canonical doc references its own role.
    assert "canonical introduction" in content.lower()
    # And it cross-links to architecture (the design review edit 5).
    assert "architecture/" in content


# ---------------------------------------------------------------------------
# Pin 4: cross-links in canonical doc resolve to real files
# ---------------------------------------------------------------------------


def test_canonical_doc_cross_links_resolve_to_real_files():
    """All in-repo cross-links in the canonical introduction must
    resolve to existing files. Catches link-rot regressions when
    referenced docs get renamed or moved."""
    import re

    content = CANONICAL_DOC.read_text(encoding="utf-8")
    # Extract markdown link hrefs that aren't external URLs.
    hrefs = re.findall(r"\]\(([^)]+)\)", content)
    missing = []
    for href in hrefs:
        if href.startswith(("http://", "https://", "#")):
            continue
        # Skip backtick-quoted code references with no link
        # (shouldn't appear in extracted hrefs but defensive).
        if "`" in href:
            continue
        target = (DOCS_ROOT / href).resolve()
        if not target.exists():
            missing.append(href)
    assert not missing, (
        f"Canonical introduction has broken cross-links: {missing}"
    )


# ---------------------------------------------------------------------------
# Two-part live-test asserter helpers (per the design review edit 4)
# ---------------------------------------------------------------------------
#
# Live tests for this batch run against a real reasoning service
# with a real conversation — they live in the live-test runbook,
# not in the unit test suite. The asserters below are utility
# functions the runbook scenarios use to enforce the two-part
# assertion contract per the design review edit:
#
#   (a) trace evidence that read_doc was actually invoked with
#       'kernos-introduction.md' as the target during the turn
#   (b) response content consistent with the canonical narrative
#
# Both must pass per scenario.


def assert_read_doc_was_invoked_with_canonical_target(
    tool_trace: list[dict],
) -> None:
    """Assert that the tool trace from a turn contains a read_doc
    invocation with `kernos-introduction.md` as the target.

    Used by live-test scenarios after `drain_tool_trace()` to
    enforce part (a) of the design review edit 4: trace evidence the reach
    mechanism actually fired against the right target.
    """
    matching = [
        entry for entry in tool_trace
        if entry.get("name") == "read_doc"
        and isinstance(entry.get("input"), dict)
        and entry["input"].get("path") == "kernos-introduction.md"
    ]
    assert matching, (
        "Live-test contract violation: no read_doc tool call with "
        "path='kernos-introduction.md' found in turn trace. The "
        "agent did not actually reach the canonical document. "
        f"Trace contained: {[e.get('name') for e in tool_trace]}"
    )


def assert_response_consistent_with_canonical_narrative(
    response_text: str,
) -> None:
    """Assert that the agent's response text contains language
    consistent with the canonical introduction's narrative shape.

    Used by live-test scenarios to enforce part (b) of the design review edit 4:
    response content reflects the canonical narrative, not a
    fabricated or stale alternative.

    The check is intentionally loose — the agent's response is
    natural language, not a copy of the doc — but it pins three
    load-bearing concepts the canonical narrative establishes.
    """
    text = response_text.lower()
    # Concept 1: Kernos as a personal agent / agentic OS.
    assert any(token in text for token in (
        "personal agent", "agentic operating system", "agent operating system",
        "personal assistant", "agent that",
    )), (
        "response does not establish Kernos's positioning as a "
        "personal agent / agentic OS"
    )
    # Concept 2: persistent or stateful memory / context across
    # conversations (the system's main differentiator). The agent
    # may phrase this as memory, context, persistence, or domains.
    assert any(token in text for token in (
        "memory", "remember", "persist", "context", "domain",
        "across", "ongoing", "conversation",
    )), (
        "response does not reference the persistent memory / context "
        "axis of the canonical narrative"
    )


# ---------------------------------------------------------------------------
# Pin 5: live-test asserter helpers exist and have stable signatures
# ---------------------------------------------------------------------------


def test_live_test_asserter_helpers_have_stable_signatures():
    """The asserter helpers above are imported by the live-test
    runbook scenarios. Pin their signatures so a future refactor
    can't silently break the runbook."""
    sig = inspect.signature(assert_read_doc_was_invoked_with_canonical_target)
    params = list(sig.parameters)
    assert params == ["tool_trace"]

    sig = inspect.signature(assert_response_consistent_with_canonical_narrative)
    params = list(sig.parameters)
    assert params == ["response_text"]


def test_asserter_helpers_reject_missing_read_doc_invocation():
    """Negative pin on the trace asserter: when no read_doc with
    canonical target appears, it raises AssertionError with a
    clear message."""
    with pytest.raises(AssertionError, match="canonical document"):
        assert_read_doc_was_invoked_with_canonical_target([])
    with pytest.raises(AssertionError, match="canonical document"):
        assert_read_doc_was_invoked_with_canonical_target([
            {"name": "read_doc", "input": {"path": "wrong/path.md"}},
        ])


def test_asserter_helpers_accept_valid_read_doc_invocation():
    """Positive pin: valid trace passes the asserter."""
    assert_read_doc_was_invoked_with_canonical_target([
        {"name": "read_doc", "input": {"path": "kernos-introduction.md"}},
    ])


def test_asserter_response_consistency_rejects_off_topic_text():
    """Negative pin on the content asserter: a response that doesn't
    reference the canonical narrative concepts fails."""
    with pytest.raises(AssertionError):
        assert_response_consistent_with_canonical_narrative(
            "The weather is nice today."
        )


def test_asserter_response_consistency_accepts_canonical_response():
    """Positive pin: a response with canonical-narrative language
    passes the asserter."""
    assert_response_consistent_with_canonical_narrative(
        "Kernos is a personal agentic operating system that remembers "
        "your context across conversations and learns over time."
    )
