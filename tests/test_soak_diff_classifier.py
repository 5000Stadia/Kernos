"""Pin tests for the soak-harness diff-report classifier.

Per architect verdict 2026-05-03 ("equivalence is algorithmic, not
vibes-based"), the dual-path equivalence soak's comparator is the
mechanical surface the architect classifies divergences on. If the
classifier itself silently misclassifies, the equivalence verdict
loses meaning. These tests pin the classifier's bucket / severity /
reason-code outputs against representative inputs.

ARCHITECTURAL EXTENSION POINTS — adding new shape tests as
architecture grows. The harness adds new shape checks in two
places (per-turn baseline assertions and the diff classifier);
this test file pins both. When new architecture lands, add a
test here mirroring the shape check.

  - New baseline console signal lands → add a test that asserts
    the baseline regex matches expected stdout AND fails when
    absent (mirror `test_normalize_log_for_prose_strips_logger_lines`
    pattern: positive + negative cases).
  - New baseline dump zone lands → add an extractor test
    (`test_extract_dump_zones_*`) covering the new zone name
    pattern, plus a structural assertion test that the new zone's
    absence registers as `_REASON_ZONE_MISSING`.
  - New dispatcher seam lands → add a tool-call test that captures
    a TOOL_CALLED line with the new seam and verifies the
    classifier's seam-specific rules (if any) fire correctly.
  - New severity code or reason code lands → add a pin test that
    asserts the constant exists with the expected string value
    (rename detection — every reason code in the test imports
    above pins the spelling).
  - New zone-shape tolerance rule lands (e.g., MEMORY zone gets
    its own LLM-variance tolerance) → add a tolerance test pair:
    one inside-tolerance case → REVIEW; one outside-tolerance →
    STRUCTURAL.
  - New tool-surface contract (e.g., when KERNEL-TOOL-REGISTRY-V1
    lands and asserts every ALWAYS_PINNED tool reaches the model)
    → add baseline dump assertion tests for each newly-required
    tool name in the surface.

Pattern for every new shape test: build a representative pair of
(legacy, thin) artifacts, run `_compare_scenario`, then assert
the bucket + severity + reason code on the resulting Divergence.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from kernos.soak import (
    Divergence,
    ScenarioComparison,
    ScenarioResult,
    _compare_scenario,
    _extract_dump_zones,
    _extract_tool_calls,
    _extract_tool_results,
    _format_coverage_audit,
    _format_diff_report,
    _length_bucket,
    _normalize_log_for_prose,
    _refusal_heuristic,
    _terminal_outcome,
    _zone_shape,
    # Severity + reason constants pinned by name so a rename of any
    # of these is a structural change that breaks the equivalence
    # contract — the test suite catches it.
    _SEVERITY_STRUCTURAL,
    _SEVERITY_STYLISTIC,
    _SEVERITY_REVIEW,
    _REASON_TOOL_SET_DIFFERS,
    _REASON_TOOL_COUNT_DIFFERS,
    _REASON_TOOL_ORDER_ONLY,
    _REASON_TOOL_ERROR_DIFFERS,
    _REASON_ZONE_MISSING,
    _REASON_ZONE_SHAPE_DIFFERS,
    _REASON_REPLY_PRESENCE_DIFFERS,
    _REASON_REPLY_LENGTH_BUCKET_DIFFERS,
    _REASON_REPLY_TOOL_USE_VS_TEXT,
    _REASON_REPLY_REFUSAL_HEURISTIC,
    _REASON_TERMINAL_OUTCOME_DIFFERS,
)


# --- Helpers --------------------------------------------------------------


def _write_pair(tmp_path: Path, name: str, log: str, dump: str) -> tuple[ScenarioResult, ScenarioResult]:
    """Materialize legacy/thin ScenarioResult pair backed by tmp files."""
    legacy_log = tmp_path / f"{name}.legacy.log"
    legacy_dump = tmp_path / f"{name}.legacy.dump"
    thin_log = tmp_path / f"{name}.thin.log"
    thin_dump = tmp_path / f"{name}.thin.dump"

    legacy_log.write_text(log[0])
    legacy_dump.write_text(dump[0])
    thin_log.write_text(log[1])
    thin_dump.write_text(dump[1])

    base = dict(
        scenario_name=name,
        automated=True,
        skipped=False,
        skip_reason="",
        duration_ms=100,
    )
    legacy = ScenarioResult(
        log_path=str(legacy_log),
        dump_path=str(legacy_dump),
        path_label="legacy",
        **base,
    )
    thin = ScenarioResult(
        log_path=str(thin_log),
        dump_path=str(thin_dump),
        path_label="thin",
        **base,
    )
    return legacy, thin


def _div_by_reason(comp: ScenarioComparison, reason: str) -> Divergence | None:
    for d in comp.divergences:
        if d.reason == reason:
            return d
    return None


# --- Extractor tests ------------------------------------------------------


def test_extract_tool_calls_orders_by_log_position():
    log = (
        "TOOL_CALLED: tool=write_file seam=full classification=soft_write\n"
        "TOOL_RESULT: tool=write_file seam=full is_error=False\n"
        "TOOL_CALLED: tool=remember seam=full classification=read\n"
    )
    calls = _extract_tool_calls(log)
    assert calls[0].startswith("write_file|")
    assert calls[1].startswith("remember|")


def test_extract_tool_results_includes_error_flag():
    log = (
        "TOOL_RESULT: tool=write_file seam=full is_error=False\n"
        "TOOL_RESULT: tool=read_file seam=full is_error=True\n"
    )
    results = _extract_tool_results(log)
    assert ("write_file", "False") in results
    assert ("read_file", "True") in results


def test_extract_dump_zones_splits_on_h2_headers():
    dump = "preamble\n\n## NOW\nnow body\n\n## STATE\nstate body line\nanother\n"
    zones = _extract_dump_zones(dump)
    assert "NOW" in zones
    assert "STATE" in zones
    assert "now body" in zones["NOW"]
    assert "state body line" in zones["STATE"]


def test_extract_dump_zones_empty_input():
    assert _extract_dump_zones("") == {}


def test_zone_shape_counts_non_empty_lines_and_subitems():
    body = "\nline 1\n- item a\n- item b\n  not a top-level\n"
    lines, headers = _zone_shape(body)
    assert lines == 4
    assert headers == 2  # the two top-level "- " items


def test_normalize_log_for_prose_strips_logger_lines():
    log = (
        "INFO: logger noise here\n"
        "agent reply prose\n"
        "2026-05-03T18:00:00 logger noise\n"
        "more agent prose\n"
    )
    out = _normalize_log_for_prose(log)
    assert "logger noise" not in out
    assert "agent reply prose" in out
    assert "more agent prose" in out


def test_length_bucket_thresholds():
    assert _length_bucket(0) == "empty"
    assert _length_bucket(50) == "short"
    assert _length_bucket(300) == "medium"
    assert _length_bucket(1000) == "long"


def test_refusal_heuristic_picks_up_common_phrases():
    assert _refusal_heuristic("I can't do that")
    assert _refusal_heuristic("I'm not able to help with this")
    assert not _refusal_heuristic("Happy to help! Here's the answer.")


def test_terminal_outcome_detects_timeout_and_exception():
    assert _terminal_outcome("normal output\n[TIMEOUT]\n") == "timeout"
    assert _terminal_outcome(
        "Traceback (most recent call last):\n  File foo, line 1\n",
    ) == "exception"
    assert _terminal_outcome("normal output\n") == "ok"


# --- Classifier tests -----------------------------------------------------


def test_compare_no_divergences_for_identical_artifacts(tmp_path):
    log = (
        "TOOL_CALLED: tool=remember seam=full classification=read\n"
        "TOOL_RESULT: tool=remember seam=full is_error=False\n"
        "agent reply text\n"
    )
    dump = "preamble\n## NOW\nnow body line\n## STATE\nstate body\n"
    legacy, thin = _write_pair(tmp_path, "identical", (log, log), (dump, dump))
    comp = _compare_scenario(legacy, thin)
    assert comp.divergences == []
    assert comp.structural_count == 0
    assert not comp.blocks_flip


def test_tool_set_differs_is_structural(tmp_path):
    legacy_log = (
        "TOOL_CALLED: tool=remember seam=full classification=read\n"
        "TOOL_RESULT: tool=remember seam=full is_error=False\n"
    )
    thin_log = (
        "TOOL_CALLED: tool=write_file seam=full classification=soft_write\n"
        "TOOL_RESULT: tool=write_file seam=full is_error=False\n"
    )
    dump = "## NOW\nbody\n"
    legacy, thin = _write_pair(
        tmp_path, "tool_set", (legacy_log, thin_log), (dump, dump),
    )
    comp = _compare_scenario(legacy, thin)
    d = _div_by_reason(comp, _REASON_TOOL_SET_DIFFERS)
    assert d is not None
    assert d.severity == _SEVERITY_STRUCTURAL
    assert d.bucket == "tool_call"
    assert comp.blocks_flip


def test_tool_count_differs_is_structural(tmp_path):
    one_call = (
        "TOOL_CALLED: tool=remember seam=full classification=read\n"
        "TOOL_RESULT: tool=remember seam=full is_error=False\n"
    )
    two_calls = one_call + one_call
    dump = "## NOW\nbody\n"
    legacy, thin = _write_pair(
        tmp_path, "tool_count", (one_call, two_calls), (dump, dump),
    )
    comp = _compare_scenario(legacy, thin)
    d = _div_by_reason(comp, _REASON_TOOL_COUNT_DIFFERS)
    assert d is not None
    assert d.severity == _SEVERITY_STRUCTURAL


def test_tool_order_only_is_review_not_structural(tmp_path):
    a = "TOOL_CALLED: tool=remember seam=full classification=read\n"
    b = "TOOL_CALLED: tool=read_file seam=full classification=read\n"
    legacy_log = a + b
    thin_log = b + a
    dump = "## NOW\nbody\n"
    legacy, thin = _write_pair(
        tmp_path, "order_only", (legacy_log, thin_log), (dump, dump),
    )
    comp = _compare_scenario(legacy, thin)
    d = _div_by_reason(comp, _REASON_TOOL_ORDER_ONLY)
    assert d is not None
    assert d.severity == _SEVERITY_REVIEW
    # Order-only never blocks flip on its own.
    assert not comp.blocks_flip


def test_tool_error_divergence_is_structural(tmp_path):
    legacy_log = (
        "TOOL_CALLED: tool=write_file seam=full classification=soft_write\n"
        "TOOL_RESULT: tool=write_file seam=full is_error=False\n"
    )
    thin_log = (
        "TOOL_CALLED: tool=write_file seam=full classification=soft_write\n"
        "TOOL_RESULT: tool=write_file seam=full is_error=True\n"
    )
    dump = "## NOW\nbody\n"
    legacy, thin = _write_pair(
        tmp_path, "tool_error", (legacy_log, thin_log), (dump, dump),
    )
    comp = _compare_scenario(legacy, thin)
    d = _div_by_reason(comp, _REASON_TOOL_ERROR_DIFFERS)
    assert d is not None
    assert d.severity == _SEVERITY_STRUCTURAL


def test_zone_missing_is_structural(tmp_path):
    log = ""
    legacy_dump = "## NOW\nbody\n## MEMORY\nmem body\n"
    thin_dump = "## NOW\nbody\n"  # MEMORY missing on thin
    legacy, thin = _write_pair(
        tmp_path, "zone_missing", (log, log), (legacy_dump, thin_dump),
    )
    comp = _compare_scenario(legacy, thin)
    d = _div_by_reason(comp, _REASON_ZONE_MISSING)
    assert d is not None
    assert d.severity == _SEVERITY_STRUCTURAL
    assert "MEMORY" in d.description


def test_zone_shape_drift_small_is_review(tmp_path):
    log = ""
    legacy_dump = "## NOW\nline a\nline b\nline c\n"
    thin_dump = "## NOW\nline a\nline b\n"  # 1-line drift, < 50%
    legacy, thin = _write_pair(
        tmp_path, "zone_shape_small", (log, log), (legacy_dump, thin_dump),
    )
    comp = _compare_scenario(legacy, thin)
    d = _div_by_reason(comp, _REASON_ZONE_SHAPE_DIFFERS)
    assert d is not None
    assert d.severity == _SEVERITY_REVIEW


def test_zone_shape_drift_large_is_structural(tmp_path):
    log = ""
    body_legacy = "\n".join(f"line {i}" for i in range(50))
    body_thin = "line a\n"  # huge drop
    legacy_dump = f"## NOW\n{body_legacy}\n"
    thin_dump = f"## NOW\n{body_thin}\n"
    legacy, thin = _write_pair(
        tmp_path, "zone_shape_large", (log, log), (legacy_dump, thin_dump),
    )
    comp = _compare_scenario(legacy, thin)
    d = _div_by_reason(comp, _REASON_ZONE_SHAPE_DIFFERS)
    assert d is not None
    assert d.severity == _SEVERITY_STRUCTURAL


def test_reply_presence_divergence_is_structural(tmp_path):
    legacy_log = "agent gave a reply here\n"
    thin_log = "INFO: only logger lines\n"
    dump = "## NOW\nbody\n"
    legacy, thin = _write_pair(
        tmp_path, "reply_presence", (legacy_log, thin_log), (dump, dump),
    )
    comp = _compare_scenario(legacy, thin)
    d = _div_by_reason(comp, _REASON_REPLY_PRESENCE_DIFFERS)
    assert d is not None
    assert d.severity == _SEVERITY_STRUCTURAL


def test_reply_length_bucket_drift_is_review(tmp_path):
    short = "ok\n"
    long_reply = "x " * 600 + "\n"
    dump = "## NOW\nbody\n"
    legacy, thin = _write_pair(
        tmp_path, "reply_length", (short, long_reply), (dump, dump),
    )
    comp = _compare_scenario(legacy, thin)
    d = _div_by_reason(comp, _REASON_REPLY_LENGTH_BUCKET_DIFFERS)
    assert d is not None
    assert d.severity == _SEVERITY_REVIEW


def test_tool_use_vs_text_only_is_structural(tmp_path):
    legacy_log = (
        "TOOL_CALLED: tool=remember seam=full classification=read\n"
        "TOOL_RESULT: tool=remember seam=full is_error=False\n"
        "agent reply\n"
    )
    thin_log = "agent reply text only no tools\n"
    dump = "## NOW\nbody\n"
    legacy, thin = _write_pair(
        tmp_path, "tool_use_vs_text", (legacy_log, thin_log), (dump, dump),
    )
    comp = _compare_scenario(legacy, thin)
    d = _div_by_reason(comp, _REASON_REPLY_TOOL_USE_VS_TEXT)
    assert d is not None
    assert d.severity == _SEVERITY_STRUCTURAL


def test_refusal_heuristic_only_flagged_when_no_tools_and_short(tmp_path):
    # Both paths produced no tool calls. Legacy refused, thin answered.
    legacy_log = "I can't do that, sorry\n"
    thin_log = "Sure, the answer is forty-two\n"
    dump = "## NOW\nbody\n"
    legacy, thin = _write_pair(
        tmp_path, "refusal", (legacy_log, thin_log), (dump, dump),
    )
    comp = _compare_scenario(legacy, thin)
    d = _div_by_reason(comp, _REASON_REPLY_REFUSAL_HEURISTIC)
    assert d is not None
    assert d.severity == _SEVERITY_REVIEW


def test_terminal_outcome_divergence_is_structural(tmp_path):
    legacy_log = "normal output\n"
    thin_log = "normal output\n[TIMEOUT]\n"
    dump = "## NOW\nbody\n"
    legacy, thin = _write_pair(
        tmp_path, "terminal", (legacy_log, thin_log), (dump, dump),
    )
    comp = _compare_scenario(legacy, thin)
    d = _div_by_reason(comp, _REASON_TERMINAL_OUTCOME_DIFFERS)
    assert d is not None
    assert d.severity == _SEVERITY_STRUCTURAL


def test_skipped_scenarios_yield_empty_comparison(tmp_path):
    base = dict(
        scenario_name="op_only",
        automated=False,
        skipped=True,
        skip_reason="operator-driven",
        duration_ms=0,
        log_path="",
        dump_path="",
    )
    legacy = ScenarioResult(path_label="legacy", **base)
    thin = ScenarioResult(path_label="thin", **base)
    comp = _compare_scenario(legacy, thin)
    assert comp.divergences == []
    assert comp.legacy_skipped and comp.thin_skipped


# --- Formatter tests ------------------------------------------------------


def test_coverage_audit_includes_known_deferred_gaps():
    md = _format_coverage_audit()
    assert "Batch 3 acceptance" in md
    assert "send_relational_message" in md
    assert "canvas_list" in md
    assert "KERNEL-TOOL-REGISTRY-V1" in md
    # Pre-decided architect classification must be visible to reviewer.
    assert "NOT a flip-blocker" in md or "does NOT block flip" in md


def test_diff_report_renders_per_scenario_blocks(tmp_path):
    log = "TOOL_CALLED: tool=remember seam=full classification=read\n"
    dump = "## NOW\nbody\n"
    legacy, thin = _write_pair(tmp_path, "demo", (log, log), (dump, dump))
    comp = _compare_scenario(legacy, thin)
    md = _format_diff_report([comp], tmp_path)
    assert "Equivalence diff report" in md
    assert "demo" in md
    # Counters present.
    assert "Structural:" in md
    assert "Review:" in md
    assert "Stylistic:" in md
