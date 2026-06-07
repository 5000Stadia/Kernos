"""v1 self-test (2026-06-07): the plain-English self-test was blocked at step
zero — 0/17 — because it could not read docs/V1-SELF-TEST.md. read_source (the
repo-side reader) was evicted from the surfaced tool set on every turn.

Root cause: the ALWAYS_PINNED set alone (~8.3k tokens) EXCEEDED the old 8000
TOOL_TOKEN_BUDGET, so active_budget went negative and EVERY dynamically-selected
tool (read_source, brave_web_search, calendar, run_self_test_suite, ...) was
evicted — the active-surfacing layer was effectively dead.

These tests pin the two invariants: read_source is always pinned, and the
pinned set must fit inside the budget with real headroom for active tools.
"""
import json

from kernos.kernel.tool_catalog import ALWAYS_PINNED, TOOL_TOKEN_BUDGET
from kernos.kernel.kernel_tool_registry import kernel_tool_schema_map


def _schema_tokens(schema: dict) -> int:
    # mirrors assemble._schema_tokens
    return len(json.dumps(schema)) // 4


def test_read_source_is_always_pinned():
    assert "read_source" in ALWAYS_PINNED, (
        "read_source (the repo-side reader for specs/docs/kernos) must be "
        "pinned — read_file is current-space only and rejects repo paths, so "
        "without read_source pinned every repo-introspection task (self-test, "
        "self-improvement, debugging) is blocked when budget eviction drops it"
    )


def test_pinned_set_fits_budget_with_headroom():
    m = kernel_tool_schema_map()
    resolved = [m[n] for n in ALWAYS_PINNED if n in m]
    pinned_tokens = sum(_schema_tokens(t) for t in resolved)
    # The pinned set must not consume the whole budget — if it does,
    # active_budget = TOOL_TOKEN_BUDGET - pinned_tokens goes <= 0 and the
    # dynamic active-surfacing layer dies (the exact 0/17 self-test failure).
    assert pinned_tokens < TOOL_TOKEN_BUDGET, (
        f"pinned set ({pinned_tokens} tok) must fit inside TOOL_TOKEN_BUDGET "
        f"({TOOL_TOKEN_BUDGET} tok); otherwise no active tool ever surfaces"
    )
    # Require meaningful headroom for the common MCP tools (web search,
    # calendar) + the analyzer's per-turn picks — not just a hair under.
    headroom = TOOL_TOKEN_BUDGET - pinned_tokens
    assert headroom >= 2000, (
        f"only {headroom} tok of active headroom after pinning — too tight to "
        f"surface web search, calendar, and the analyzer's selections"
    )
