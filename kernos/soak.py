"""Substrate-fidelity soak-test harness.

A standardized verification process that exercises Kernos end-to-end
against a declarative scenario list, capturing console logs and
``/dump`` output, then validating that:

1. The right tool/event/log line surfaces in the console (behavioral
   evidence — the right function ran).
2. The expected substrate zones / content reach the model in the
   ``/dump`` output (structural evidence — the substrate is present).

Both classes of assertion together pin substrate fidelity at the
operator-experienced layer, on top of the in-process contract tests
that pin it at the seam.

Usage:

  python -m kernos.soak --all       # run every automatable scenario
  python -m kernos.soak --scenario probe_c_procedures
  python -m kernos.soak --list      # print available scenarios

Artifacts land under ``data/soak-runs/<timestamp>/`` — log file +
dump file per scenario plus a JSON results blob and a markdown
summary report.

Architecture note: this harness uses subprocess invocation of the
existing dev REPL launcher (``./cli.sh``). It is NOT a pytest test
because it exercises the real boot path with real LLM calls;
in-process unit tests use ``_make_handler``-style mocks. The two
verification surfaces are complementary: contract tests pin the
seam in-process; this harness pins behavior + substrate end-to-end
on the real production-equivalent boot.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Scenario + assertion dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsoleAssertion:
    """A pattern that must appear (or must NOT appear) in the
    captured stdout + stderr log of a scenario run.

    ``pattern`` is a Python regex. ``must_appear=True`` (default)
    asserts at least one match exists; ``must_appear=False`` asserts
    no matches exist (useful for "this MUST NOT happen" checks like
    BLOCKED_SENDER).
    """

    description: str
    pattern: str
    must_appear: bool = True


@dataclass(frozen=True)
class DumpAssertion:
    """A substring or pattern that must appear (or must NOT appear)
    in the ``/dump`` output of a scenario run.

    ``mode="substring"`` does a plain ``in`` check; ``mode="regex"``
    uses re.search. ``must_appear`` semantics match
    :class:`ConsoleAssertion`.
    """

    description: str
    needle: str
    mode: str = "substring"  # "substring" | "regex"
    must_appear: bool = True


@dataclass(frozen=True)
class SetupFile:
    """A file the harness writes into the scenario's data dir
    BEFORE the launcher starts. Use for producer-side fixturing
    (e.g., placing _procedures.md in a space's file dir so the
    consumer's substrate-rendering can be tested without
    depending on the agent to execute write_file)."""

    path_relative_to_data_dir: str
    content: str


@dataclass(frozen=True)
class Scenario:
    """A scripted soak scenario.

    ``input_lines`` are sent verbatim to the launcher's stdin. The
    last two lines are typically ``/dump`` then ``/quit`` so the
    scenario captures a dump and exits cleanly.

    ``automated=False`` means the scenario can't be CC-driven (e.g.,
    needs Discord interaction); the harness prints instructions and
    skips actual execution.

    ``setup_files`` are written into the data dir before the
    launcher starts. Use for producer-side fixturing (e.g., place
    a real _procedures.md so the substrate-rendering test isn't
    contingent on the LLM agent's behavioral choice to execute
    write_file).
    """

    name: str
    description: str
    launcher: str  # "cli.sh" or "start.sh"
    automated: bool
    env: dict[str, str] = field(default_factory=dict)
    input_lines: tuple[str, ...] = ()
    console_assertions: tuple[ConsoleAssertion, ...] = ()
    dump_assertions: tuple[DumpAssertion, ...] = ()
    setup_files: tuple[SetupFile, ...] = ()
    timeout_seconds: int = 180
    notes: str = ""


@dataclass
class AssertionResult:
    description: str
    passed: bool
    detail: str = ""


@dataclass
class ScenarioResult:
    scenario_name: str
    automated: bool
    skipped: bool
    skip_reason: str
    duration_ms: int
    log_path: str
    dump_path: str
    console_results: list[AssertionResult] = field(default_factory=list)
    dump_results: list[AssertionResult] = field(default_factory=list)
    # Path label for dual-path equivalence soak: "legacy" | "thin" | ""
    # Empty string means single-path run (back-compat with --paths defaults).
    path_label: str = ""

    @property
    def passed(self) -> bool:
        if self.skipped:
            return False
        return all(a.passed for a in self.console_results) and all(
            a.passed for a in self.dump_results
        )

    @property
    def total_assertions(self) -> int:
        return len(self.console_results) + len(self.dump_results)

    @property
    def passed_assertions(self) -> int:
        return sum(1 for a in self.console_results if a.passed) + sum(
            1 for a in self.dump_results if a.passed
        )


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _validate_console(
    log_text: str, assertions: tuple[ConsoleAssertion, ...],
) -> list[AssertionResult]:
    out: list[AssertionResult] = []
    for a in assertions:
        try:
            matches = re.findall(a.pattern, log_text, flags=re.MULTILINE)
        except re.error as exc:
            out.append(AssertionResult(
                description=a.description, passed=False,
                detail=f"regex compile error: {exc}",
            ))
            continue
        found = bool(matches)
        if a.must_appear:
            out.append(AssertionResult(
                description=a.description, passed=found,
                detail=(
                    f"matched {len(matches)} time(s)" if found
                    else f"pattern not found: {a.pattern!r}"
                ),
            ))
        else:
            out.append(AssertionResult(
                description=a.description, passed=not found,
                detail=(
                    "no matches (correct)" if not found
                    else f"unexpected match: {matches[:3]!r}"
                ),
            ))
    return out


def _validate_dump(
    dump_text: str, assertions: tuple[DumpAssertion, ...],
) -> list[AssertionResult]:
    out: list[AssertionResult] = []
    for a in assertions:
        if a.mode == "regex":
            try:
                found = bool(re.search(a.needle, dump_text, flags=re.MULTILINE))
            except re.error as exc:
                out.append(AssertionResult(
                    description=a.description, passed=False,
                    detail=f"regex compile error: {exc}",
                ))
                continue
        else:
            found = a.needle in dump_text
        if a.must_appear:
            out.append(AssertionResult(
                description=a.description, passed=found,
                detail=(
                    "found" if found
                    else f"missing — looked for {a.needle!r}"
                ),
            ))
        else:
            out.append(AssertionResult(
                description=a.description, passed=not found,
                detail=(
                    "absent (correct)" if not found
                    else f"unexpected presence of {a.needle!r}"
                ),
            ))
    return out


# ---------------------------------------------------------------------------
# Dual-path comparison + diff classification (Batch 3 equivalence soak)
# ---------------------------------------------------------------------------
#
# Architecture (architect verdict 2026-05-03):
#
# `--paths both` runs each automated scenario twice — once with
# KERNOS_USE_DECOUPLED_TURN_RUNNER=0 (legacy oracle) and once with
# =1 (thin path). Per-scenario artifacts land under
# `run_dir/{legacy,thin}/{scenario}.{log,dump.txt}`. The classifier
# walks each pair and emits divergences bucketed into three
# categories with a 3-state severity (structural / stylistic /
# review) plus a reason code so the architect's classification is
# mechanical.
#
# Severity semantics:
#   structural — different tool / args / count / zone presence /
#                gating outcome. Default-blocks-flip; architect can
#                downgrade to "intentional improvement" or
#                "intentional removal of legacy quirk."
#   review     — order-only drift, zone shape drift within tolerance,
#                refusal-keyword heuristics. Defaults to "needs human
#                eyes," not auto-block.
#   stylistic  — same tools + args + zones, different prose. Note,
#                don't block.
#
# Reason codes are stable strings; future architecting can add
# rule-based downgrades keyed off them without re-classifying.

# Severity is a Literal so the JSON serialization stays stable.
_SEVERITY_STRUCTURAL = "structural"
_SEVERITY_STYLISTIC = "stylistic"
_SEVERITY_REVIEW = "review"

# Reason codes — stable strings used by both the report and any
# future rule-based downgrades.
_REASON_TOOL_SET_DIFFERS = "tool_set_differs"
_REASON_TOOL_ARGS_DIFFER = "tool_args_differ"
_REASON_TOOL_COUNT_DIFFERS = "tool_count_differs"
_REASON_TOOL_ORDER_ONLY = "tool_order_only"
_REASON_TOOL_ERROR_DIFFERS = "tool_error_differs"
_REASON_ZONE_MISSING = "zone_missing"
_REASON_ZONE_SHAPE_DIFFERS = "zone_shape_differs"
_REASON_REPLY_PRESENCE_DIFFERS = "reply_presence_differs"
_REASON_REPLY_LENGTH_BUCKET_DIFFERS = "reply_length_bucket_differs"
_REASON_REPLY_TOOL_USE_VS_TEXT = "reply_tool_use_vs_text_only"
_REASON_REPLY_REFUSAL_HEURISTIC = "reply_refusal_heuristic"
_REASON_TERMINAL_OUTCOME_DIFFERS = "terminal_outcome_differs"


@dataclass(frozen=True)
class Divergence:
    """A single structural/stylistic/review divergence between paths."""

    bucket: str               # "tool_call" | "dump_zone" | "response_signal"
    severity: str             # _SEVERITY_*
    reason: str               # _REASON_*
    description: str
    legacy_value: str
    thin_value: str


@dataclass
class ScenarioComparison:
    scenario_name: str
    legacy_skipped: bool
    thin_skipped: bool
    skip_reason: str
    divergences: list[Divergence] = field(default_factory=list)
    # Normalized fingerprints stored alongside raw values so the
    # report is explainable without re-deriving extraction logic.
    legacy_tool_calls: tuple[str, ...] = ()
    thin_tool_calls: tuple[str, ...] = ()
    legacy_zones: tuple[str, ...] = ()
    thin_zones: tuple[str, ...] = ()

    @property
    def structural_count(self) -> int:
        return sum(1 for d in self.divergences if d.severity == _SEVERITY_STRUCTURAL)

    @property
    def stylistic_count(self) -> int:
        return sum(1 for d in self.divergences if d.severity == _SEVERITY_STYLISTIC)

    @property
    def review_count(self) -> int:
        return sum(1 for d in self.divergences if d.severity == _SEVERITY_REVIEW)

    @property
    def blocks_flip(self) -> bool:
        """structural divergences default-block-flip until architect-classified."""
        return self.structural_count > 0


# --- Extractors -----------------------------------------------------------

# Tool call markers come from server.py + repl.py:
#   TOOL_CALLED: tool=<name> seam=<seam> classification=<cls>
#   TOOL_RESULT: tool=<name> seam=<seam> is_error=<bool>
#
# ARCHITECTURAL EXTENSION POINT — when new dispatcher seams ship
# (e.g., a workshop-tool dispatcher distinct from
# live_integration_dispatcher, or a subconscious-layer dispatcher),
# the seam string in TOOL_CALLED widens. The regex below is
# already permissive enough to capture any seam value, but the
# classifier may want seam-specific routing rules (e.g., "tool fired
# through workshop seam = check workshop registration as well").
# When that happens, add a dispatch-seam fingerprint extractor here
# and route the divergence rule in `_compare_scenario` accordingly.
_TOOL_CALLED_RE = re.compile(
    r"TOOL_CALLED: tool=(\S+) seam=(\S*) classification=(\S*)"
)
_TOOL_RESULT_RE = re.compile(
    r"TOOL_RESULT: tool=(\S+) seam=(\S*) is_error=(\S+)"
)

# Logger lines we strip before measuring "prose" so reply-length
# buckets don't drift on log noise. Both regexes match whole lines
# (anchored ^...$ with .*$ tail) so substitution removes the entire
# line rather than just the prefix; subsequent blank-line collapse
# handles cleanup.
_LOGGER_LINE_RE = re.compile(
    r"^(?:\[[\d\-:T\.\+Z\s]+\]\s+)?[A-Z]+[A-Z_]*:\s.*$",
    re.MULTILINE,
)
_TIMESTAMPED_LOG_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}.*$",
    re.MULTILINE,
)


def _extract_tool_calls(log_text: str) -> tuple[str, ...]:
    """Ordered tuple of `tool_name(args_marker)` strings, in dispatch order.

    The dispatcher event payload doesn't expose full args in the log
    (only input-shape-keys for the legacy emitter); the comparator
    treats tool-name + seam as the canonical signal and notes
    arg-shape divergence as a separate rule when shape data is
    available in the underlying event stream.
    """
    out: list[str] = []
    for m in _TOOL_CALLED_RE.finditer(log_text):
        tool, seam, cls = m.group(1), m.group(2), m.group(3)
        out.append(f"{tool}|{seam}|{cls}")
    return tuple(out)


def _extract_tool_results(log_text: str) -> tuple[tuple[str, str], ...]:
    """Ordered tuple of (tool_name, is_error) results."""
    out: list[tuple[str, str]] = []
    for m in _TOOL_RESULT_RE.finditer(log_text):
        out.append((m.group(1), m.group(3)))
    return tuple(out)


def _extract_dump_zones(dump_text: str) -> dict[str, str]:
    """Map zone name -> body text. Zones are `## ZONE_NAME` headers.

    Body runs from the header until the next `## ` header or end.
    Returns ordered dict (insertion = source order).

    ARCHITECTURAL EXTENSION POINT — zone-name regex below accepts
    any uppercase header (`[A-Z][A-Z0-9 _\\-/]*`) so new zones from
    future architecture surface automatically. When a new zone
    type ships (subconscious, shared spaces, member capabilities,
    canvases-as-active-zone, etc.), it appears in the comparison
    without code change. If a zone needs special-cased shape
    tolerance (e.g., MEMORY zone is LLM-driven and varies turn-to-
    turn — see the 2026-05-03 soak's only structural divergence),
    add a per-zone tolerance map and key off the zone name in the
    shape-comparison block of `_compare_scenario`.
    """
    zones: dict[str, str] = {}
    if not dump_text:
        return zones
    parts = re.split(r"^## ([A-Z][A-Z0-9 _\-/]*)\s*$", dump_text, flags=re.MULTILINE)
    # parts[0] is the preamble; then alternating (zone_name, body).
    for i in range(1, len(parts) - 1, 2):
        name = parts[i].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        zones[name] = body
    return zones


def _zone_shape(body: str) -> tuple[int, int]:
    """Cheap shape signal: (non-empty line count, top-level header count)."""
    lines = [ln for ln in body.splitlines() if ln.strip()]
    headers = sum(
        1 for ln in lines
        if ln.startswith("### ") or ln.startswith("- ") or ln.startswith("* ")
    )
    return (len(lines), headers)


def _normalize_log_for_prose(log_text: str) -> str:
    """Strip logger/timestamp lines so reply-length buckets aren't
    inflated by log noise. Keeps lines that look like the agent's
    REPL output (no logger prefix, no timestamp marker)."""
    if not log_text:
        return ""
    text = _LOGGER_LINE_RE.sub("", log_text)
    text = _TIMESTAMPED_LOG_RE.sub("", text)
    # Drop blank-collapsing artifacts.
    return "\n".join(ln for ln in text.splitlines() if ln.strip())


def _length_bucket(length: int) -> str:
    if length == 0:
        return "empty"
    if length < 100:
        return "short"
    if length < 500:
        return "medium"
    return "long"


def _refusal_heuristic(text: str) -> bool:
    """Coarse refusal signal — only triggers when paired with no
    tool calls + short reply. Heuristic; classifier marks as
    review-not-structural so architect can adjudicate."""
    if not text:
        return False
    needles = ("can't ", "cannot ", "won't ", "unable to ", "I'm not able")
    lower = text.lower()
    return any(n.lower() in lower for n in needles)


def _terminal_outcome(log_text: str) -> str:
    """Coarse terminal-outcome signal: ok | timeout | exception."""
    if "[TIMEOUT]" in log_text:
        return "timeout"
    if re.search(r"^Traceback \(most recent call last\):", log_text, re.MULTILINE):
        return "exception"
    return "ok"


# --- Classifier -----------------------------------------------------------


def _compare_scenario(
    legacy: ScenarioResult, thin: ScenarioResult,
) -> ScenarioComparison:
    """Walk paired (legacy, thin) artifacts, emit bucketed divergences."""
    divs: list[Divergence] = []

    if legacy.skipped or thin.skipped:
        return ScenarioComparison(
            scenario_name=legacy.scenario_name,
            legacy_skipped=legacy.skipped,
            thin_skipped=thin.skipped,
            skip_reason=legacy.skip_reason or thin.skip_reason,
        )

    legacy_log = _read_or_empty(legacy.log_path)
    thin_log = _read_or_empty(thin.log_path)
    legacy_dump = _read_or_empty(legacy.dump_path)
    thin_dump = _read_or_empty(thin.dump_path)

    # Bucket 1 — tool-call divergences -------------------------------
    legacy_calls = _extract_tool_calls(legacy_log)
    thin_calls = _extract_tool_calls(thin_log)
    legacy_results = _extract_tool_results(legacy_log)
    thin_results = _extract_tool_results(thin_log)

    legacy_set = set(legacy_calls)
    thin_set = set(thin_calls)
    if legacy_set != thin_set:
        # Different set of tools fired — structural.
        only_legacy = sorted(legacy_set - thin_set)
        only_thin = sorted(thin_set - legacy_set)
        divs.append(Divergence(
            bucket="tool_call",
            severity=_SEVERITY_STRUCTURAL,
            reason=_REASON_TOOL_SET_DIFFERS,
            description="set of tools fired differs between paths",
            legacy_value="; ".join(only_legacy) or "(none)",
            thin_value="; ".join(only_thin) or "(none)",
        ))
    elif sorted(legacy_calls) != sorted(thin_calls):
        # Same set but different multiplicity — structural (count
        # divergence usually matters).
        divs.append(Divergence(
            bucket="tool_call",
            severity=_SEVERITY_STRUCTURAL,
            reason=_REASON_TOOL_COUNT_DIFFERS,
            description="tool call counts differ between paths",
            legacy_value=f"{len(legacy_calls)} calls",
            thin_value=f"{len(thin_calls)} calls",
        ))
    elif legacy_calls != thin_calls:
        # Same multiset, different order — review (architect decides
        # if order matters for this scenario).
        divs.append(Divergence(
            bucket="tool_call",
            severity=_SEVERITY_REVIEW,
            reason=_REASON_TOOL_ORDER_ONLY,
            description="tool calls match but order differs",
            legacy_value=" → ".join(c.split("|")[0] for c in legacy_calls),
            thin_value=" → ".join(c.split("|")[0] for c in thin_calls),
        ))

    # Tool-result error divergences (independent of call order).
    legacy_errors = {(t, e) for t, e in legacy_results if e.lower() == "true"}
    thin_errors = {(t, e) for t, e in thin_results if e.lower() == "true"}
    if legacy_errors != thin_errors:
        divs.append(Divergence(
            bucket="tool_call",
            severity=_SEVERITY_STRUCTURAL,
            reason=_REASON_TOOL_ERROR_DIFFERS,
            description="tool error outcomes differ between paths",
            legacy_value=", ".join(sorted(t for t, _ in legacy_errors)) or "(no errors)",
            thin_value=", ".join(sorted(t for t, _ in thin_errors)) or "(no errors)",
        ))

    # Bucket 2 — dump-zone divergences -------------------------------
    legacy_zones = _extract_dump_zones(legacy_dump)
    thin_zones = _extract_dump_zones(thin_dump)
    legacy_zone_names = set(legacy_zones)
    thin_zone_names = set(thin_zones)
    missing_in_thin = legacy_zone_names - thin_zone_names
    missing_in_legacy = thin_zone_names - legacy_zone_names
    for zone in sorted(missing_in_thin):
        divs.append(Divergence(
            bucket="dump_zone",
            severity=_SEVERITY_STRUCTURAL,
            reason=_REASON_ZONE_MISSING,
            description=f"zone present on legacy, missing on thin: ## {zone}",
            legacy_value="present",
            thin_value="missing",
        ))
    for zone in sorted(missing_in_legacy):
        divs.append(Divergence(
            bucket="dump_zone",
            severity=_SEVERITY_STRUCTURAL,
            reason=_REASON_ZONE_MISSING,
            description=f"zone present on thin, missing on legacy: ## {zone}",
            legacy_value="missing",
            thin_value="present",
        ))
    # Zones present on both — shape compare.
    for zone in sorted(legacy_zone_names & thin_zone_names):
        l_shape = _zone_shape(legacy_zones[zone])
        t_shape = _zone_shape(thin_zones[zone])
        if l_shape == t_shape:
            continue
        # Within-tolerance shape drift = review; large drift = structural.
        line_drift = abs(l_shape[0] - t_shape[0])
        max_lines = max(l_shape[0], t_shape[0], 1)
        ratio = line_drift / max_lines
        severity = (
            _SEVERITY_REVIEW
            if ratio < 0.5 and line_drift < 20
            else _SEVERITY_STRUCTURAL
        )
        divs.append(Divergence(
            bucket="dump_zone",
            severity=severity,
            reason=_REASON_ZONE_SHAPE_DIFFERS,
            description=f"zone shape drift on ## {zone}",
            legacy_value=f"{l_shape[0]} lines / {l_shape[1]} subitems",
            thin_value=f"{t_shape[0]} lines / {t_shape[1]} subitems",
        ))

    # Bucket 3 — response-signal divergences -------------------------
    legacy_prose = _normalize_log_for_prose(legacy_log)
    thin_prose = _normalize_log_for_prose(thin_log)
    legacy_bucket = _length_bucket(len(legacy_prose))
    thin_bucket = _length_bucket(len(thin_prose))

    legacy_present = bool(legacy_prose.strip())
    thin_present = bool(thin_prose.strip())
    if legacy_present != thin_present:
        divs.append(Divergence(
            bucket="response_signal",
            severity=_SEVERITY_STRUCTURAL,
            reason=_REASON_REPLY_PRESENCE_DIFFERS,
            description="one path produced no response",
            legacy_value="present" if legacy_present else "absent",
            thin_value="present" if thin_present else "absent",
        ))
    elif legacy_bucket != thin_bucket:
        divs.append(Divergence(
            bucket="response_signal",
            severity=_SEVERITY_REVIEW,
            reason=_REASON_REPLY_LENGTH_BUCKET_DIFFERS,
            description="reply-length bucket changed",
            legacy_value=legacy_bucket,
            thin_value=thin_bucket,
        ))

    # tool-use-vs-text-only divergence — independent of length.
    legacy_used_tools = bool(legacy_calls)
    thin_used_tools = bool(thin_calls)
    if legacy_used_tools != thin_used_tools:
        divs.append(Divergence(
            bucket="response_signal",
            severity=_SEVERITY_STRUCTURAL,
            reason=_REASON_REPLY_TOOL_USE_VS_TEXT,
            description="one path used tools, the other text-only",
            legacy_value="tools" if legacy_used_tools else "text-only",
            thin_value="tools" if thin_used_tools else "text-only",
        ))

    # Refusal heuristic — only flag when paired with no-tool + short reply.
    if not legacy_used_tools and not thin_used_tools:
        legacy_refusal = (
            _refusal_heuristic(legacy_prose) and legacy_bucket in ("short", "medium")
        )
        thin_refusal = (
            _refusal_heuristic(thin_prose) and thin_bucket in ("short", "medium")
        )
        if legacy_refusal != thin_refusal:
            divs.append(Divergence(
                bucket="response_signal",
                severity=_SEVERITY_REVIEW,
                reason=_REASON_REPLY_REFUSAL_HEURISTIC,
                description="refusal-keyword heuristic divergent (review needed)",
                legacy_value="refusal-like" if legacy_refusal else "answer-like",
                thin_value="refusal-like" if thin_refusal else "answer-like",
            ))

    # Terminal outcome (timeout / exception).
    legacy_outcome = _terminal_outcome(legacy_log)
    thin_outcome = _terminal_outcome(thin_log)
    if legacy_outcome != thin_outcome:
        divs.append(Divergence(
            bucket="response_signal",
            severity=_SEVERITY_STRUCTURAL,
            reason=_REASON_TERMINAL_OUTCOME_DIFFERS,
            description="terminal outcomes differ",
            legacy_value=legacy_outcome,
            thin_value=thin_outcome,
        ))

    return ScenarioComparison(
        scenario_name=legacy.scenario_name,
        legacy_skipped=False,
        thin_skipped=False,
        skip_reason="",
        divergences=divs,
        legacy_tool_calls=legacy_calls,
        thin_tool_calls=thin_calls,
        legacy_zones=tuple(sorted(legacy_zones)),
        thin_zones=tuple(sorted(thin_zones)),
    )


def _read_or_empty(path: str) -> str:
    try:
        return Path(path).read_text() if path else ""
    except (OSError, FileNotFoundError):
        return ""


# --- Coverage audit + diff report formatting -----------------------------


_BATCH_3_ACCEPTANCE: tuple[tuple[str, str, str, str], ...] = (
    # (acceptance_label, mapped_scenario, coverage, notes)
    (
        "Read-only tool capability",
        "scenario_4_memory_recall (auto)",
        "partial",
        "knowledge-recall covered; explicit web/calendar/file_read covered by probe_b_external_mcp (operator)",
    ),
    (
        "Write/destructive tool capability",
        "—",
        "MISSING",
        "no scripted propose-then-execute scenario; consider write_file/send_to_channel scripted scenario",
    ),
    (
        "No-tool conversational",
        "scenario_1_hatching (auto)",
        "adequate",
        "hatching turn is conversational without tool fire",
    ),
    (
        "Hatching turn",
        "scenario_1_hatching (auto)",
        "full",
        "bootstrap_prompt + UNIQUE hatching prompt + RULES/NOW/STATE",
    ),
    (
        "Multi-member disclosure",
        "scenario_3_multi_member_disclosure (operator)",
        "operator-only",
        "two-member setup needs OAuth or instance.db manipulation",
    ),
    (
        "Covenant-conflict",
        "scenario_2_covenant_conflict (operator)",
        "operator-only",
        "covenant pre-population needs first-turn establishment or direct SQL",
    ),
)


_KNOWN_DEFERRED_GAPS = (
    (
        "Asymmetric (4 tools, thin missing vs legacy)",
        "send_relational_message, resolve_relational_message, consult, request_space_action",
        "addressed by KERNEL-TOOL-REGISTRY-V1 in stabilization, NOT a flip-blocker",
    ),
    (
        "Symmetric (11 tools, both missing)",
        "canvas (8): canvas_list, canvas_create, page_read, page_write, page_list, page_search, canvas_preference_extract, canvas_preference_confirm; model diagnostics (3): set_chain_model, diagnose_llm_chain, diagnose_messenger",
        "addressed by KERNEL-TOOL-REGISTRY-V1 in stabilization, NOT a flip-blocker",
    ),
)


def _format_coverage_audit() -> str:
    lines: list[str] = []
    lines.append("# Batch 3 acceptance — scenario coverage audit")
    lines.append("")
    lines.append("Per architect verdict 2026-05-03: equivalence soak runs on the "
                 "existing 27-tool catalog. Known-and-deferred tool-surface gaps "
                 "are addressed by KERNEL-TOOL-REGISTRY-V1 during stabilization "
                 "and are NOT flip-blockers.")
    lines.append("")
    lines.append("## Acceptance scenario → existing scenario mapping")
    lines.append("")
    lines.append("| Acceptance scenario | Mapped scenario | Coverage | Notes |")
    lines.append("|---|---|---|---|")
    for acc, mapped, cov, notes in _BATCH_3_ACCEPTANCE:
        lines.append(f"| {acc} | {mapped} | {cov} | {notes} |")
    lines.append("")
    lines.append("## Known-and-deferred tool-surface gaps")
    lines.append("")
    lines.append("These gaps will surface in the diff-report as structural "
                 "divergences. Architect classification is pre-decided: "
                 "**known, addressed by KERNEL-TOOL-REGISTRY-V1, does NOT block flip.**")
    lines.append("")
    for label, tools, disposition in _KNOWN_DEFERRED_GAPS:
        lines.append(f"* **{label}**: {tools}")
        lines.append(f"  * disposition: {disposition}")
    lines.append("")
    lines.append("Founder confirms scenario coverage above before running "
                 "`./scripts/run-soak.sh --paths both --auto-only`. Any gap "
                 "marked MISSING needs scripted scenario or explicit "
                 "operator-driven coverage before equivalence verdict.")
    return "\n".join(lines)


def _format_diff_report(
    comparisons: list[ScenarioComparison], run_dir: Path,
) -> str:
    lines: list[str] = []
    lines.append(f"# Equivalence diff report — {run_dir.name}")
    lines.append("")
    total_struct = sum(c.structural_count for c in comparisons)
    total_review = sum(c.review_count for c in comparisons)
    total_styl = sum(c.stylistic_count for c in comparisons)
    blocking = [c for c in comparisons if c.blocks_flip]
    lines.append(
        f"**Structural:** {total_struct} (default-blocks-flip until "
        f"architect classifies)  \n"
        f"**Review:** {total_review} (needs human eyes)  \n"
        f"**Stylistic:** {total_styl} (note, doesn't block)"
    )
    lines.append("")
    if blocking:
        names = ", ".join(c.scenario_name for c in blocking)
        lines.append(f"**Scenarios with structural divergences:** {names}")
    else:
        lines.append("**No structural divergences.** Default flip eligible "
                     "pending architect review of `review`-severity items.")
    lines.append("")
    lines.append("## Per-scenario comparison")
    lines.append("")
    for c in comparisons:
        if c.legacy_skipped or c.thin_skipped:
            lines.append(f"### ⚪ SKIPPED — {c.scenario_name}")
            lines.append(f"* skip reason: {c.skip_reason}")
            lines.append("")
            continue
        glyph = "❌" if c.blocks_flip else (
            "🟡" if c.review_count else "✅"
        )
        lines.append(f"### {glyph} {c.scenario_name}")
        lines.append(
            f"* structural: {c.structural_count} · review: {c.review_count} "
            f"· stylistic: {c.stylistic_count}"
        )
        lines.append(
            f"* legacy tool calls: {len(c.legacy_tool_calls)} · "
            f"thin tool calls: {len(c.thin_tool_calls)}"
        )
        lines.append(
            f"* legacy zones: {', '.join(c.legacy_zones) or '(none)'}"
        )
        lines.append(
            f"* thin zones: {', '.join(c.thin_zones) or '(none)'}"
        )
        if not c.divergences:
            lines.append("* no divergences detected")
            lines.append("")
            continue
        for bucket in ("tool_call", "dump_zone", "response_signal"):
            in_bucket = [d for d in c.divergences if d.bucket == bucket]
            if not in_bucket:
                continue
            lines.append(f"#### {bucket}")
            for d in in_bucket:
                sev_glyph = {
                    _SEVERITY_STRUCTURAL: "🔴",
                    _SEVERITY_REVIEW: "🟡",
                    _SEVERITY_STYLISTIC: "🔵",
                }[d.severity]
                lines.append(
                    f"* {sev_glyph} `{d.reason}` — {d.description}"
                )
                lines.append(f"  * legacy: {d.legacy_value}")
                lines.append(f"  * thin: {d.thin_value}")
        lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        "* Severity reasons map to stable codes (see `_REASON_*` "
        "constants in kernos/soak.py) so future rule-based "
        "downgrades are possible without re-classifying."
    )
    lines.append(
        "* Known-and-deferred gaps from `kernos.soak --coverage-audit` "
        "(asymmetric 4-tool + symmetric 11-tool registry-drift) "
        "appear here as structural; architect's pre-decided "
        "classification is **does NOT block flip**."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-turn shape checklist (baseline assertions, applied to every scenario)
# ---------------------------------------------------------------------------
#
# Every automated scenario runs these assertions in addition to its
# scenario-specific ones. Captures "the proper shape of a turn"
# mechanically — boot succeeded, dump emitted, no traceback, core
# substrate zones assembled, request_tool reaches the LLM. Mismatch
# on any of these means something fundamental is broken even before
# scenario-specific signals matter.
#
# Founder-driven posture (2026-05-03): CC runs soaks autonomously
# and surfaces anomalies; the per-turn shape checklist is the
# mechanical surface for "did this turn fire correctly." Failures
# here route to "structural / blocks-flip" by definition.

# ARCHITECTURAL EXTENSION POINT — adding new console signals.
#
# Each signal here represents "a shape every successful Kernos turn
# should produce in stdout." When new architecture lands that has a
# new console signal worth pinning, add a ConsoleAssertion below. Examples
# of future architecture that would warrant a new entry:
#
#   - Subconscious layer ships → assert background-pondering log line
#     fires when subconscious cycles run
#   - New dispatcher seam (e.g., distinct workshop-tool dispatcher)
#     ships → assert seam-specific TOOL_CALLED log line
#   - Capability-readiness probe runs at boot → assert the probe log line
#   - Multi-instance coordination ships → assert per-instance boot log
#
# Keep entries here scoped to "every turn should show this." Scenario-
# specific console signals stay in the per-scenario `console_assertions`
# tuple (see SCENARIO_1_HATCHING for examples).
_BASELINE_CONSOLE_ASSERTIONS: tuple[ConsoleAssertion, ...] = (
    ConsoleAssertion(
        description="checklist-1: REPL boot succeeded (handler ready)",
        pattern=r"repl: handler ready",
    ),
    ConsoleAssertion(
        description="checklist-2: /dump command emitted (context captured)",
        pattern=r"DUMP: context written to",
    ),
    ConsoleAssertion(
        description="checklist-3: no exception traceback in run",
        pattern=r"^Traceback \(most recent call last\):",
        must_appear=False,
    ),
    ConsoleAssertion(
        description="checklist-4: no abuse-prevention block (BLOCKED_SENDER)",
        pattern=r"BLOCKED_SENDER",
        must_appear=False,
    ),
)

# ARCHITECTURAL EXTENSION POINT — adding new dump shape checks.
#
# Each entry pins "this substrate element reaches the model on every
# turn." The split below mirrors how the substrate composes: zones
# come from the assembly pipeline; tool surface comes from the
# catalog-resolved tool list; identifiers come from the briefing.
# When new substrate ships, route the new shape check to its category.
#
# Examples of future architecture that would warrant new entries:
#
#   Substrate zones (## SOMETHING headers in dump):
#     - Subconscious layer ships → ## SUBCONSCIOUS or ## PONDERINGS zone
#     - Cross-member shared spaces ship → ## SHARED CONTEXT zone
#     - Per-member capability connections ship → ## MY CAPABILITIES
#       zone visible to that member
#     - System space tooling ships → ## SYSTEM SPACE zone for
#       admin-mode turns
#
#   Tool surface (`"name": "..."` in tools array):
#     - KERNEL-TOOL-REGISTRY-V1 lands → assert each ALWAYS_PINNED
#       entry resolves through the registrar (right now only
#       request_tool is asserted; widen here when registry refactor
#       lands)
#     - Workshop tools become first-class peers → assert at least
#       one workshop tool registers and is reachable
#     - Member-relational tools surface on thin path (currently in
#       the asymmetric gap) → assert send_relational_message reaches
#       the model
#
#   Briefing / identifier shape (preamble + cache_key fields):
#     - When the dump format pins instance/member/space identifiers
#       in a discoverable header → add identifier presence checks
#     - Cache_key surfaces the thin-path indicator → add as
#       path-specific check toggled by path_label
#
# Failure of any baseline item is structural-by-definition; the
# diff-report classifier routes it to blocks-flip.
_BASELINE_DUMP_ASSERTIONS: tuple[DumpAssertion, ...] = (
    DumpAssertion(
        description="checklist-5: ## RULES zone present (operating principles)",
        needle="## RULES",
    ),
    DumpAssertion(
        description="checklist-6: ## NOW zone present (current time + state)",
        needle="## NOW",
    ),
    DumpAssertion(
        description="checklist-7: ## STATE zone present (preferences + spaces)",
        needle="## STATE",
    ),
    DumpAssertion(
        description="checklist-8: request_tool reaches tools list (CCV1 C5)",
        needle='"name": "request_tool"',
    ),
)


def _format_checklist() -> str:
    """Return a human-readable checklist for `--checklist` flag."""
    lines: list[str] = []
    lines.append("# Per-turn shape checklist (applied to every automated scenario)")
    lines.append("")
    lines.append("Captures the mechanical signals every successful Kernos turn "
                 "should produce. Failures here are structural by definition; "
                 "the diff-report classifier routes them to blocks-flip.")
    lines.append("")
    lines.append("## Console signals")
    for a in _BASELINE_CONSOLE_ASSERTIONS:
        polarity = "must appear" if a.must_appear else "must NOT appear"
        lines.append(f"* **{a.description}** — pattern `{a.pattern}` ({polarity})")
    lines.append("")
    lines.append("## Dump zones + tool surface")
    for a in _BASELINE_DUMP_ASSERTIONS:
        polarity = "must appear" if a.must_appear else "must NOT appear"
        lines.append(f"* **{a.description}** — looks for `{a.needle!r}` ({polarity})")
    lines.append("")
    lines.append("## Path-specific notes")
    lines.append("* Thin path: tool dispatch with `seam=live_integration_dispatcher` "
                 "is read-only; full machinery dispatches use the executor seam")
    lines.append("* Legacy path: same checklist applies; differences vs thin "
                 "are surfaced by the diff-report classifier, not this checklist")
    lines.append("* Known-and-deferred tool surface gaps (4 asymmetric + 11 "
                 "symmetric, see --coverage-audit) do NOT fail the checklist; "
                 "they're flagged as known divergences in the diff-report")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _kernos_root() -> Path:
    """The Kernos project root, derived from this module's path."""
    return Path(__file__).resolve().parent.parent


async def _run_scenario(
    s: Scenario, run_dir: Path, path_label: str = "",
) -> ScenarioResult:
    """Execute a single automated scenario and return its results.

    ``path_label`` is "legacy" | "thin" | "" for dual-path equivalence
    soak. Non-empty path_label routes artifacts under
    ``run_dir/{path_label}/`` and isolates per-path data dirs by
    suffixing ``KERNOS_DATA_DIR`` so concurrent (or sequential) runs
    don't leak state across paths. The flag value forced into the
    child env: legacy=0, thin=1.
    """
    started_at = datetime.now(timezone.utc)
    artifact_dir = run_dir / path_label if path_label else run_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    log_path = artifact_dir / f"{s.name}.log"

    # Per-path data dir suffix prevents legacy/thin from sharing
    # instance state. Scenarios that hardcode KERNOS_DATA_DIR get
    # their value rewritten with a __{path_label} suffix.
    base_data_dir = s.env.get("KERNOS_DATA_DIR", "./data-soak-default")
    if path_label:
        scoped_data_dir = f"{base_data_dir}__{path_label}"
    else:
        scoped_data_dir = base_data_dir
    dump_dir = Path(scoped_data_dir).expanduser().resolve()
    dump_dir.mkdir(parents=True, exist_ok=True)
    diag_dir = dump_dir / "diagnostics"

    if not s.automated:
        return ScenarioResult(
            scenario_name=s.name,
            automated=False,
            skipped=True,
            skip_reason=(
                "operator-driven (production launcher / Discord); see "
                "runbook"
            ),
            duration_ms=0,
            log_path=str(log_path),
            dump_path="",
            path_label=path_label,
        )

    launcher_path = _kernos_root() / s.launcher
    if not launcher_path.exists():
        return ScenarioResult(
            scenario_name=s.name,
            automated=True,
            skipped=True,
            skip_reason=f"launcher not found: {launcher_path}",
            duration_ms=0,
            log_path=str(log_path),
            dump_path="",
            path_label=path_label,
        )

    env = os.environ.copy()
    env.update(s.env)
    env.setdefault("KERNOS_LOG_LEVEL", "INFO")
    env["KERNOS_DATA_DIR"] = scoped_data_dir
    # Force the cognition-path flag for dual-path mode. Without this
    # the child inherits whatever the operator set (or .env default),
    # which would silently undermine equivalence soak.
    #
    # path_label="default" runs the post-flip verification: leave the
    # env var unset so the production default (thin path post-CCV1-C7-
    # flip) decides. Strip any inherited value from .env so the test
    # actually exercises "what happens when no override is set."
    if path_label == "legacy":
        env["KERNOS_USE_DECOUPLED_TURN_RUNNER"] = "0"
    elif path_label == "thin":
        env["KERNOS_USE_DECOUPLED_TURN_RUNNER"] = "1"
    elif path_label == "default":
        env.pop("KERNOS_USE_DECOUPLED_TURN_RUNNER", None)

    # Pre-launch setup files: harness writes producer-side
    # artifacts into the scenario's data dir before the launcher
    # starts. Lets scenarios test consumer-side substrate
    # rendering without depending on the LLM agent's behavioral
    # choices to execute the writes itself.
    for setup_file in s.setup_files:
        target = dump_dir / setup_file.path_relative_to_data_dir
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(setup_file.content)

    stdin_payload = "\n".join(s.input_lines) + "\n"

    proc = subprocess.Popen(
        ["bash", str(launcher_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(_kernos_root()),
        env=env,
        text=True,
    )
    try:
        stdout, _ = proc.communicate(
            input=stdin_payload, timeout=s.timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, _ = proc.communicate()
        log_path.write_text(stdout + "\n[TIMEOUT]\n")
        return ScenarioResult(
            scenario_name=s.name,
            automated=True,
            skipped=False,
            skip_reason="",
            duration_ms=int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000),
            log_path=str(log_path),
            dump_path="",
            console_results=[AssertionResult(
                description="scenario completed within timeout",
                passed=False,
                detail=f"timeout after {s.timeout_seconds}s",
            )],
            path_label=path_label,
        )

    log_path.write_text(stdout)

    # Find the latest dump file in the diagnostics dir.
    dump_files = sorted(
        diag_dir.glob("context_*.txt"),
        key=lambda p: p.stat().st_mtime,
    ) if diag_dir.exists() else []
    dump_text = ""
    dump_path_str = ""
    if dump_files:
        latest_dump = dump_files[-1]
        dump_text = latest_dump.read_text()
        # Copy a snapshot into the artifact dir (per-path subdir
        # under run_dir when path_label is set, run_dir otherwise).
        snapshot = artifact_dir / f"{s.name}.dump.txt"
        snapshot.write_text(dump_text)
        dump_path_str = str(snapshot)

    # Baseline + scenario assertions stack: baseline first so the
    # checklist items appear at the top of the per-scenario report.
    all_console = _BASELINE_CONSOLE_ASSERTIONS + s.console_assertions
    all_dump = _BASELINE_DUMP_ASSERTIONS + s.dump_assertions
    console_results = _validate_console(stdout, all_console)
    dump_results = _validate_dump(dump_text, all_dump)

    return ScenarioResult(
        scenario_name=s.name,
        automated=True,
        skipped=False,
        skip_reason="",
        duration_ms=int(
            (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
        ),
        log_path=str(log_path),
        dump_path=dump_path_str,
        console_results=console_results,
        dump_results=dump_results,
        path_label=path_label,
    )


# ---------------------------------------------------------------------------
# Scenarios — declarative source of truth
# ---------------------------------------------------------------------------


PROBE_C_PROCEDURES = Scenario(
    name="probe_c_procedures",
    description=(
        "Live procedures probe — verify ## PROCEDURES zone "
        "populates from a real _procedures.md file on disk. "
        "Operator-driven because the auto-generated space_id "
        "is per-boot non-deterministic, so the harness can't "
        "pre-place the file at the right path. The structural "
        "invariant (renderer surfaces procedures-prefix when "
        "loaded) is pinned by the in-process producer-to-"
        "consumer tests at "
        "tests/test_compaction_carry_producer_to_consumer.py "
        "and the C2 contract test for the procedures zone. "
        "This probe converts contract-tested → dump-observed "
        "for the operator's confidence."
    ),
    launcher="cli.sh",
    automated=False,
    notes=(
        "Operator runs cli.sh, sends a hello, lets the instance "
        "auto-create its General space, exits. Then drops a real "
        "_procedures.md into "
        "data-dev/<safe_instance_id>/spaces/<space_id>/files/ "
        "with two test lines. Re-runs cli.sh, sends a hello, "
        "types /dump, /quit. Inspects the dump file: ## PROCEDURES "
        "zone present + content matches. The probe converts the "
        "contract-tested invariant into dump-observed evidence on "
        "a real boot."
    ),
)


PROBE_D_COMPACTION = Scenario(
    name="probe_d_compaction",
    description=(
        "Compaction-carry probe — converse until KERNOS_COMPACTION_THRESHOLD=500 "
        "is crossed; verify ## MEMORY zone populates from real "
        "compaction carry (the design review's explicitly-named fourth probe)"
    ),
    launcher="cli.sh",
    automated=True,
    env={
        "KERNOS_DATA_DIR": "./data-soak/probe-d",
        "KERNOS_INSTANCE_ID": "soak:probe-d",
        "KERNOS_REPL_SENDER": "operator",
        "KERNOS_COMPACTION_THRESHOLD": "500",
    },
    input_lines=(
        "Hi! I want to tell you about a project I'm working on. It's a "
        "research effort to map historical climate data from the 1850s "
        "to the present, focused on Atlantic hurricanes specifically. "
        "I have ten years of weekly ship logs from the period.",
        "Tell me what stands out to you about that timeframe and "
        "topic. What patterns do you anticipate I might find?",
        "Specifically, I'm curious about the El Niño correlation. The "
        "ship logs I have run from 1855 through 1865, so the early "
        "data should help me anchor the methodology. What questions "
        "should I be asking?",
        "/dump",
        "/quit",
    ),
    console_assertions=(
        ConsoleAssertion(
            description="dump command emitted",
            pattern=r"DUMP: context written to",
        ),
        ConsoleAssertion(
            description="no abuse-prevention block",
            pattern=r"BLOCKED_SENDER",
            must_appear=False,
        ),
    ),
    dump_assertions=(
        DumpAssertion(
            description=(
                "## MEMORY zone present (compaction carry "
                "populated — KIT_REQUIRED probe)"
            ),
            needle="## MEMORY",
        ),
    ),
    notes=(
        "Note: compaction is multi-turn-dependent. If the threshold "
        "isn't crossed within the scripted turns, the dump won't "
        "show ## MEMORY. Increase turn count or lower threshold "
        "further if needed."
    ),
    timeout_seconds=300,
)


SCENARIO_1_HATCHING = Scenario(
    name="scenario_1_hatching",
    description=(
        "Hatching turn — fresh-instance first conversation; verify "
        "bootstrap_prompt + UNIQUE hatching prompt reach the model"
    ),
    launcher="cli.sh",
    automated=True,
    env={
        "KERNOS_DATA_DIR": "./data-soak/scenario-1",
        "KERNOS_INSTANCE_ID": "soak:scenario-1",
        "KERNOS_REPL_SENDER": "operator",
    },
    input_lines=(
        "Hi! Just dropping in to say hello.",
        "/dump",
        "/quit",
    ),
    console_assertions=(
        ConsoleAssertion(
            description="dump command emitted",
            pattern=r"DUMP: context written to",
        ),
        ConsoleAssertion(
            description="no abuse-prevention block",
            pattern=r"BLOCKED_SENDER",
            must_appear=False,
        ),
    ),
    dump_assertions=(
        DumpAssertion(
            description="bootstrap_prompt reaches model (FIRST CONVERSATION)",
            needle="FIRST CONVERSATION",
        ),
        DumpAssertion(
            description="UNIQUE hatching prompt reaches model",
            needle="HATCHING. This is your first moment of existence",
        ),
        DumpAssertion(
            description="## RULES zone present",
            needle="## RULES",
        ),
        DumpAssertion(
            description="## NOW zone present",
            needle="## NOW",
        ),
        DumpAssertion(
            description="## STATE zone present",
            needle="## STATE",
        ),
        DumpAssertion(
            description="request_tool in tools list (CCV1 C5)",
            needle='"name": "request_tool"',
        ),
    ),
    timeout_seconds=120,
)


SCENARIO_4_MEMORY_RECALL = Scenario(
    name="scenario_4_memory_recall",
    description=(
        "Memory-recall turn — knowledge-bearing conversation; verify "
        "the agent's response references prior turn content (knowledge "
        "entries reach STATE / USER CONTEXT)"
    ),
    launcher="cli.sh",
    automated=True,
    env={
        "KERNOS_DATA_DIR": "./data-soak/scenario-4",
        "KERNOS_INSTANCE_ID": "soak:scenario-4",
        "KERNOS_REPL_SENDER": "operator",
    },
    input_lines=(
        "I want to tell you something important: my favorite color is "
        "specifically marigold yellow. Please remember this.",
        "What's my favorite color?",
        "/dump",
        "/quit",
    ),
    console_assertions=(
        ConsoleAssertion(
            description="dump command emitted",
            pattern=r"DUMP: context written to",
        ),
        ConsoleAssertion(
            description="no abuse-prevention block",
            pattern=r"BLOCKED_SENDER",
            must_appear=False,
        ),
    ),
    dump_assertions=(
        DumpAssertion(
            description="## STATE zone present",
            needle="## STATE",
        ),
        DumpAssertion(
            description="conversation messages reach the model",
            needle="marigold",
        ),
    ),
    notes=(
        "Lighter version of the spec scenario 4. Doesn't exercise "
        "compaction carry directly (that's probe D); confirms "
        "knowledge-bearing turn lands in conversation context."
    ),
    timeout_seconds=120,
)


PROBE_A_ADAPTERS = Scenario(
    name="probe_a_adapters_channel_registry",
    description=(
        "Production-boot adapters and channel registry — operator "
        "must run via start.sh against a real Discord/SMS connection"
    ),
    launcher="start.sh",
    automated=False,
    notes=(
        "From /home/k/Kernos-main run ./start.sh; send a Discord "
        "hello; observe via /dump that ## ACTIONS contains "
        "OUTBOUND CHANNELS line + send_to_channel in tools."
    ),
)


PROBE_B_EXTERNAL_MCP = Scenario(
    name="probe_b_external_mcp",
    description=(
        "External MCP surface — operator exercises one external "
        "MCP tool (calendar / web search / browser) via Discord on "
        "the production server"
    ),
    launcher="start.sh",
    automated=False,
    notes=(
        "Continue from probe A. Send a Discord message that exercises "
        "calendar / web / browser. Verify the tool was invoked + "
        "result reached the model."
    ),
)


SCENARIO_2_COVENANT_CONFLICT = Scenario(
    name="scenario_2_covenant_conflict",
    description=(
        "Covenant-conflict turn — pre-set a covenant; send a request "
        "that conflicts; verify deterministic substrate carries the "
        "rule and the agent surfaces conflict (does not silently "
        "bypass)"
    ),
    launcher="cli.sh",
    automated=False,
    notes=(
        "Pre-population requires either direct instance.db SQL or a "
        "first conversational turn that establishes the covenant. "
        "Operator runs interactively for now; a future automation "
        "extension sets up the covenant via a setup helper."
    ),
)


SCENARIO_3_MULTI_MEMBER = Scenario(
    name="scenario_3_multi_member_disclosure",
    description=(
        "Multi-member disclosure turn — two members with declared "
        "relationship; verify cross-member content respects the "
        "permission profile"
    ),
    launcher="cli.sh",
    automated=False,
    notes=(
        "Two-member setup needs OAuth flow on production OR direct "
        "instance.db manipulation in dev. Operator runs interactively "
        "for now; a future automation extension scripts the second-"
        "member registration."
    ),
)


SCENARIOS: tuple[Scenario, ...] = (
    PROBE_C_PROCEDURES,
    PROBE_D_COMPACTION,
    SCENARIO_1_HATCHING,
    SCENARIO_4_MEMORY_RECALL,
    # Operator-driven (printed for the operator; harness skips):
    PROBE_A_ADAPTERS,
    PROBE_B_EXTERNAL_MCP,
    SCENARIO_2_COVENANT_CONFLICT,
    SCENARIO_3_MULTI_MEMBER,
)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _format_summary(results: list[ScenarioResult], run_dir: Path) -> str:
    lines: list[str] = []
    lines.append(f"# Soak run report — {run_dir.name}")
    lines.append("")
    automated_results = [r for r in results if r.automated]
    operator_results = [r for r in results if not r.automated]
    auto_passed = [r for r in automated_results if r.passed and not r.skipped]
    auto_failed = [
        r for r in automated_results if not r.passed and not r.skipped
    ]
    auto_skipped = [r for r in automated_results if r.skipped]
    lines.append(
        f"**Automated:** {len(auto_passed)}/{len(automated_results)} green "
        f"({len(auto_failed)} fail, {len(auto_skipped)} skip)"
    )
    lines.append(
        f"**Operator-driven (manual):** {len(operator_results)} pending"
    )
    lines.append("")
    lines.append("## Per-scenario results")
    lines.append("")
    for r in results:
        status = (
            "🟡 OPERATOR"
            if not r.automated
            else "⚪ SKIPPED"
            if r.skipped
            else "✅ PASS"
            if r.passed
            else "❌ FAIL"
        )
        lines.append(f"### {status} — {r.scenario_name}")
        if r.skipped:
            lines.append(f"* skip reason: {r.skip_reason}")
            lines.append("")
            continue
        if not r.automated:
            lines.append("* operator-driven; see runbook")
            lines.append("")
            continue
        lines.append(
            f"* duration: {r.duration_ms}ms; "
            f"assertions: {r.passed_assertions}/{r.total_assertions} pass"
        )
        lines.append(f"* log: {r.log_path}")
        if r.dump_path:
            lines.append(f"* dump: {r.dump_path}")
        if r.console_results:
            lines.append("* console assertions:")
            for a in r.console_results:
                glyph = "✓" if a.passed else "✗"
                lines.append(f"  * {glyph} {a.description} — {a.detail}")
        if r.dump_results:
            lines.append("* dump assertions:")
            for a in r.dump_results:
                glyph = "✓" if a.passed else "✗"
                lines.append(f"  * {glyph} {a.description} — {a.detail}")
        lines.append("")
    return "\n".join(lines)


def _format_results_json(results: list[ScenarioResult]) -> str:
    return json.dumps([asdict(r) for r in results], indent=2, default=str)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _make_run_dir() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    base = _kernos_root() / "data" / "soak-runs"
    base.mkdir(parents=True, exist_ok=True)
    run_dir = base / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


async def run_scenarios_async(
    selected: list[Scenario], run_dir: Path, path_label: str = "",
) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    pl_tag = f" path={path_label}" if path_label else ""
    for s in selected:
        print(f"--- running: {s.name} ({'auto' if s.automated else 'operator'}){pl_tag}", flush=True)
        result = await _run_scenario(s, run_dir, path_label)
        results.append(result)
        if result.skipped:
            print(f"    skipped — {result.skip_reason}", flush=True)
        elif result.automated:
            glyph = "PASS" if result.passed else "FAIL"
            print(
                f"    {glyph} ({result.passed_assertions}/"
                f"{result.total_assertions}) in {result.duration_ms}ms",
                flush=True,
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Substrate-fidelity soak-test harness for Kernos.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="run every scenario (automated runs end-to-end; operator-driven prints instructions)",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="run a specific scenario by name (repeatable)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="print available scenarios and exit",
    )
    parser.add_argument(
        "--auto-only", action="store_true",
        help="run only automated scenarios (skip operator-driven)",
    )
    parser.add_argument(
        "--paths", choices=["legacy", "thin", "both", "default"], default="thin",
        help=(
            "cognition path to exercise. 'legacy' = "
            "KERNOS_USE_DECOUPLED_TURN_RUNNER=0; 'thin' = =1; 'both' = "
            "run each automated scenario twice and emit a diff-report. "
            "'default' = leave the env var unset and let the production "
            "default decide (thin post-CCV1-C7-flip 2026-05-03); used "
            "for post-flip default verification turns. Default 'thin' "
            "matches single-path operator runs; use 'both' for Batch 3 "
            "equivalence soak."
        ),
    )
    parser.add_argument(
        "--coverage-audit", action="store_true",
        help=(
            "print the Batch 3 acceptance-scenario coverage audit "
            "table and exit. Use to confirm scenario coverage + "
            "known-and-deferred gap classification before running "
            "the equivalence soak."
        ),
    )
    parser.add_argument(
        "--checklist", action="store_true",
        help=(
            "print the per-turn shape checklist (the baseline "
            "assertions applied to every automated scenario) and "
            "exit. Useful for understanding what 'a correctly fired "
            "turn' should look like, mechanically."
        ),
    )
    args = parser.parse_args()

    if args.list:
        for s in SCENARIOS:
            tag = "[auto]" if s.automated else "[operator]"
            print(f"  {tag} {s.name} — {s.description}")
        return 0

    if args.coverage_audit:
        print(_format_coverage_audit())
        return 0

    if args.checklist:
        print(_format_checklist())
        return 0

    selected: list[Scenario]
    if args.scenario:
        named = {s.name: s for s in SCENARIOS}
        selected = []
        for name in args.scenario:
            if name not in named:
                print(f"error: unknown scenario {name!r}", file=sys.stderr)
                return 2
            selected.append(named[name])
    elif args.all or args.auto_only:
        selected = list(SCENARIOS)
        if args.auto_only:
            selected = [s for s in selected if s.automated]
    else:
        parser.print_help()
        return 1

    run_dir = _make_run_dir()
    print(f"soak run dir: {run_dir}", flush=True)

    paths_to_run = (
        ["legacy", "thin"] if args.paths == "both" else [args.paths]
    )

    # Coverage audit always lands in the run dir for the
    # architect's reference, regardless of path mode.
    (run_dir / "coverage_audit.md").write_text(_format_coverage_audit())

    per_path_results: dict[str, list[ScenarioResult]] = {}
    for path_label in paths_to_run:
        if len(paths_to_run) > 1:
            print(f"\n=== running paths={path_label} ===", flush=True)
        # Empty path_label preserves single-path layout (artifacts in
        # run_dir directly); dual-path mode forces subdirs.
        label_for_run = path_label if len(paths_to_run) > 1 else ""
        per_path_results[path_label] = asyncio.run(
            run_scenarios_async(selected, run_dir, label_for_run),
        )

    # Single-path or default summary.
    flat_results: list[ScenarioResult] = []
    for path_label in paths_to_run:
        flat_results.extend(per_path_results[path_label])

    summary = _format_summary(flat_results, run_dir)
    (run_dir / "report.md").write_text(summary)
    (run_dir / "results.json").write_text(_format_results_json(flat_results))

    print()
    print(summary)

    # Dual-path: emit equivalence diff report.
    if len(paths_to_run) == 2:
        legacy_by_name = {r.scenario_name: r for r in per_path_results["legacy"]}
        thin_by_name = {r.scenario_name: r for r in per_path_results["thin"]}
        comparisons: list[ScenarioComparison] = []
        for s in selected:
            legacy_r = legacy_by_name.get(s.name)
            thin_r = thin_by_name.get(s.name)
            if legacy_r is None or thin_r is None:
                continue
            comparisons.append(_compare_scenario(legacy_r, thin_r))
        diff_md = _format_diff_report(comparisons, run_dir)
        (run_dir / "diff_report.md").write_text(diff_md)
        print()
        print(diff_md)

    # Exit 0 if all automated passed; non-zero otherwise.
    automated = [r for r in flat_results if r.automated and not r.skipped]
    failed = [r for r in automated if not r.passed]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
