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
# Runner
# ---------------------------------------------------------------------------


def _kernos_root() -> Path:
    """The Kernos project root, derived from this module's path."""
    return Path(__file__).resolve().parent.parent


async def _run_scenario(
    s: Scenario, run_dir: Path,
) -> ScenarioResult:
    """Execute a single automated scenario and return its results."""
    started_at = datetime.now(timezone.utc)
    log_path = run_dir / f"{s.name}.log"
    dump_dir_root = Path(s.env.get("KERNOS_DATA_DIR", "./data-soak-default"))
    dump_dir = dump_dir_root.expanduser().resolve()
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
        )

    env = os.environ.copy()
    env.update(s.env)
    env.setdefault("KERNOS_LOG_LEVEL", "INFO")

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
        # Copy a snapshot into the run dir for later inspection.
        snapshot = run_dir / f"{s.name}.dump.txt"
        snapshot.write_text(dump_text)
        dump_path_str = str(snapshot)

    console_results = _validate_console(stdout, s.console_assertions)
    dump_results = _validate_dump(dump_text, s.dump_assertions)

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
        "compaction carry (Kit's explicitly-named fourth probe)"
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
    selected: list[Scenario], run_dir: Path,
) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    for s in selected:
        print(f"--- running: {s.name} ({'auto' if s.automated else 'operator'})", flush=True)
        result = await _run_scenario(s, run_dir)
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
    args = parser.parse_args()

    if args.list:
        for s in SCENARIOS:
            tag = "[auto]" if s.automated else "[operator]"
            print(f"  {tag} {s.name} — {s.description}")
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

    results = asyncio.run(run_scenarios_async(selected, run_dir))

    summary = _format_summary(results, run_dir)
    (run_dir / "report.md").write_text(summary)
    (run_dir / "results.json").write_text(_format_results_json(results))

    print()
    print(summary)

    # Exit 0 if all automated passed; non-zero otherwise.
    automated = [r for r in results if r.automated and not r.skipped]
    failed = [r for r in automated if not r.passed]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
