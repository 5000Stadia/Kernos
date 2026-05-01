"""CLEANUP-BATCH-V1 item 13: NOW-block freshness guard.

DECISIONS.md is Claude Code's stated entry point. The NOW block at
the top has decayed in the past — listing wrong test counts and
stale "next spec" pointers — because nothing pinned it. This test
is the pin.

Rules enforced:

1. The NOW block exists and is the first non-empty section of the
   file, by convention.
2. The Tests field either (a) names a real test count that matches
   what pytest --collect-only reports, OR (b) defers to the Phase
   Summary table (the deferral is acceptable when the doc
   explicitly says so, since the table is the canonical place).

Rule 2 catches the bad case: hardcoded outdated test count. The
deferral lane is allowed because the architect prefers to keep the
block structurally stable across batches.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DECISIONS_PATH = REPO_ROOT / "DECISIONS.md"


def _read_now_block() -> str:
    """Return the NOW block text, including the heading line, from
    the top of DECISIONS.md. Stops at the next ``---`` separator
    or the next ``## `` heading."""
    text = DECISIONS_PATH.read_text(encoding="utf-8")
    if not text.startswith("## NOW"):
        raise AssertionError(
            "DECISIONS.md must start with '## NOW' — Claude Code's "
            "entry-point convention is broken if it doesn't."
        )
    lines: list[str] = []
    for line in text.splitlines():
        if lines and (line.startswith("---") or
                      (line.startswith("## ") and line != "## NOW")):
            break
        lines.append(line)
    return "\n".join(lines)


def _extract_tests_value(now_block: str) -> str:
    """Extract the value after ``**Tests:**`` on the Tests line of
    the NOW block. Returns the raw value, stripped."""
    for line in now_block.splitlines():
        m = re.match(r"\*\*Tests:\*\*\s*(.+?)\s*$", line)
        if m:
            return m.group(1).strip()
    raise AssertionError(
        "DECISIONS.md NOW block has no '**Tests:**' line. The "
        "convention requires it."
    )


def _collected_test_count() -> int:
    """Run ``pytest --collect-only -q`` and parse the trailing
    ``<N> tests collected`` summary line."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # pytest's collect-only output ends with e.g. "4922 tests collected in 1.05s"
    output = result.stdout + result.stderr
    m = re.search(r"^(\d+)\s+tests?\s+collected", output, re.MULTILINE)
    if not m:
        raise AssertionError(
            "could not parse test count from pytest --collect-only "
            f"output. Last 500 chars: {output[-500:]!r}"
        )
    return int(m.group(1))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNowBlockExists:
    def test_now_block_is_first(self):
        block = _read_now_block()
        assert block.startswith("## NOW")
        assert "**Status:**" in block
        assert "**Action:**" in block
        assert "**Tests:**" in block

    def test_owner_field_present(self):
        block = _read_now_block()
        assert "**Owner:**" in block


class TestTestsFieldFreshness:
    """Tests value must be either a number that matches collected
    count, OR a phrase deferring to the Phase Summary table.
    Anything else (stale hardcoded number) fails."""

    DEFER_PHRASES = (
        "see phase summary",
        "see phase-summary",
        "see phase summary table",
    )

    def test_tests_value_matches_or_defers(self):
        block = _read_now_block()
        value = _extract_tests_value(block).lower()

        if any(phrase in value for phrase in self.DEFER_PHRASES):
            pytest.skip(
                "NOW block defers to phase summary — accepted "
                "convention; no further check"
            )

        # Otherwise the value must be a positive integer matching
        # the actual collected test count.
        m = re.match(r"^(\d+(?:[,\s]\d+)*)$", value.replace(",", ""))
        if not m:
            pytest.fail(
                f"NOW block Tests field {value!r} is neither a "
                "deferral phrase ('see phase summary table') nor a "
                "plain integer count. Update DECISIONS.md."
            )
        declared = int(value.replace(",", ""))
        actual = _collected_test_count()
        assert declared == actual, (
            f"NOW block claims {declared} tests; pytest collects "
            f"{actual}. Update the NOW block in DECISIONS.md."
        )
