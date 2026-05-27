"""Self-test gate kernel tool for the autonomous-improvement loop.

SELF-TEST-GATE-V1 (2026-05-22).

Runs a curated smoke test set against the improvement worktree
to verify the autonomous loop's commit didn't regress core
substrate invariants. v1 ships a focused set targeting the
loop's own substrate (liveness contract, provider injection,
gateway health); v2 lands the full pytest suite after the 74%
stall is diagnosed.

SUBSTRATE-SELF-TEST-V1 (2026-05-26) extends this module with:
  * `SubstrateSoakRunner` — drives the 8 substrate-soak probes
    (round-trip composition, self-knowledge, consult drain,
    dispatch canonicalization, retry feedback, gateway
    deafness, approval loop, loop-health completion) against
    the current process state.
  * `include_soak` flag on the kernel tool — when True, runs
    the smoke suite AND the soak suite, reporting under
    separate result keys.
  * `__main__` CLI entry — `python -m kernos.kernel.
    self_test_gate --include-soak --json` is the single
    canonical contract CI + local script + post-bring-up hook
    all invoke.

Per [[agent-facing-natural-simplicity]]: agent reads a short
prose summary; operator inspects full output via the ledger
event or the JSON CLI output.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Curated smoke set — files that exercise the loop's own
# substrate invariants. The orchestrator appends the spec-
# under-test's own test file via extra_test_paths.
SMOKE_TEST_FILES: tuple[str, ...] = (
    "tests/test_self_controlled_loop_liveness.py",
    "tests/test_substrate_bringup_providers.py",
    "tests/test_gateway_health_observer.py",
)


def _default_timeout() -> int:
    return int(os.environ.get("KERNOS_SELF_TEST_TIMEOUT_SEC", "120"))


# ---------------------------------------------------------------------
# SUBSTRATE-SELF-TEST-V1 (2026-05-26) — soak runner + dataclasses
# ---------------------------------------------------------------------


# Hand-listed per spec Open Question 2: v1 enumerates the 8 probes
# explicitly; formal registration is v2 work if the suite grows past
# ~15 probes. Order matters for serial execution + the mutation matrix.
PROBE_MODULE_NAMES: tuple[str, ...] = (
    "agent_round_trip_soak",
    "self_knowledge_invariant",
    "consult_drain_invariant",
    "dispatch_canonicalization_invariant",
    "retry_with_feedback_invariant",
    "gateway_deafness_invariant",
    "approval_loop_invariant",
    "loop_health_completion_invariant",
)


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one soak probe. Per spec AC2, both evidence dicts
    must be present + populated with the probe's declared required
    keys + carry non-sentinel values; SubstrateSoakRunner enforces
    this at validate-time and sets passed=False with
    failure_reason="shallow_evidence" otherwise."""
    probe_name: str
    passed: bool
    behavioral_evidence: dict
    substrate_evidence: dict
    duration_ms: int
    failure_reason: str = ""


@dataclass(frozen=True)
class SoakSuiteResult:
    """Aggregate of all probe outcomes from one
    SubstrateSoakRunner.run_all() call."""
    all_passed: bool
    per_probe: tuple[ProbeResult, ...]
    total_duration_ms: int

    def failing_probe_names(self) -> tuple[str, ...]:
        return tuple(p.probe_name for p in self.per_probe if not p.passed)


# AC2: shallow evidence must be rejected. Sentinel patterns are
# evaluated in _is_shallow_value() rather than via membership in a
# frozenset (dict/list values aren't hashable, so a literal sentinel
# set wouldn't include {} or []).


def _is_shallow_value(value: Any) -> bool:
    """Return True if `value` is too vague to count as substrate
    evidence per AC2. Booleans are accepted when they're the
    declared payload type (e.g. a Pass/Fail signal) but only when
    the dict has multiple keys — a single-key dict like
    {"ok": True} fails."""
    if isinstance(value, bool):
        # Bools are sentinel-like on their own; only acceptable as
        # part of a richer evidence dict (caller-side check).
        return False  # individual bool not shallow by itself
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in {"ok", ""}:
        return True
    if value in ({}, []):
        return True
    return False


def _validate_evidence(
    evidence: dict,
    required_keys: frozenset,
    evidence_type: str,
    probe_name: str,
) -> tuple[bool, str]:
    """Validate one evidence dict against its declared required keys.
    Returns (ok, failure_reason). evidence_type is "behavioral" or
    "substrate" for the failure reason text."""
    if not isinstance(evidence, dict):
        return False, (
            f"probe {probe_name!r} {evidence_type}_evidence is not "
            f"a dict (got {type(evidence).__name__})"
        )
    if not evidence:
        return False, (
            f"probe {probe_name!r} {evidence_type}_evidence is empty"
        )
    missing = required_keys - set(evidence.keys())
    if missing:
        return False, (
            f"probe {probe_name!r} {evidence_type}_evidence missing "
            f"declared key(s): {sorted(missing)}"
        )
    # AC2 shallow-evidence check: if the dict is just one or two
    # sentinel-only keys (e.g. {"ok": True}), reject.
    non_shallow_count = sum(
        1 for v in evidence.values() if not _is_shallow_value(v)
    )
    if non_shallow_count == 0:
        return False, (
            f"probe {probe_name!r} {evidence_type}_evidence is "
            f"shallow (only sentinel values like None/empty/'ok'); "
            f"per AC2 evidence must carry real substrate signal"
        )
    return True, ""


class SubstrateSoakRunner:
    """Drives the eight substrate-soak probes with shared fixtures.

    SUBSTRATE-SELF-TEST-V1 (2026-05-26). Probes are deterministic
    — no real external providers, no real Discord gateway, no real
    Anthropic/OpenAI dispatch. Fake binaries / fake subprocesses
    ARE acceptable for testing the dispatch pipeline shape (Probes
    3 + 7). A probe failure points at Kernos's substrate, not at
    flaky external dependencies.

    Each probe runs serially against a fresh fixture set so failures
    don't contaminate subsequent probes. Evidence keys declared at
    module level by each probe (REQUIRED_BEHAVIORAL_KEYS,
    REQUIRED_SUBSTRATE_KEYS) are enforced by validate — shallow
    evidence (sentinel-only) is rejected with failure_reason=
    "shallow_evidence".
    """

    def __init__(
        self,
        *,
        probe_names: tuple[str, ...] = PROBE_MODULE_NAMES,
    ) -> None:
        self._probe_names = probe_names

    async def run_all(self) -> SoakSuiteResult:
        """Run every probe serially. Each probe gets a fresh fixture
        set. Returns SoakSuiteResult with per-probe outcomes."""
        suite_start = time.monotonic()
        results: list[ProbeResult] = []
        for probe_name in self._probe_names:
            result = await self.run_probe(probe_name)
            results.append(result)
        total_ms = int((time.monotonic() - suite_start) * 1000)
        all_passed = all(r.passed for r in results)
        return SoakSuiteResult(
            all_passed=all_passed,
            per_probe=tuple(results),
            total_duration_ms=total_ms,
        )

    async def run_probe(self, probe_name: str) -> ProbeResult:
        """Run a single probe by name. For targeted retries +
        operator-driven post-mortems."""
        probe_start = time.monotonic()
        try:
            module = importlib.import_module(
                f"tests.substrate_soak.{probe_name}",
            )
        except ImportError as exc:
            return ProbeResult(
                probe_name=probe_name, passed=False,
                behavioral_evidence={}, substrate_evidence={},
                duration_ms=int(
                    (time.monotonic() - probe_start) * 1000,
                ),
                failure_reason=(
                    f"probe module import failed: {exc}"
                ),
            )
        required_b = getattr(
            module, "REQUIRED_BEHAVIORAL_KEYS", frozenset(),
        )
        required_s = getattr(
            module, "REQUIRED_SUBSTRATE_KEYS", frozenset(),
        )
        run_fn = getattr(module, "run_probe", None)
        if run_fn is None or not asyncio.iscoroutinefunction(run_fn):
            return ProbeResult(
                probe_name=probe_name, passed=False,
                behavioral_evidence={}, substrate_evidence={},
                duration_ms=int(
                    (time.monotonic() - probe_start) * 1000,
                ),
                failure_reason=(
                    f"probe module does not expose async run_probe()"
                ),
            )
        try:
            result = await run_fn()
        except Exception as exc:
            logger.exception(
                "PROBE_RAISED probe=%s exc=%s", probe_name, exc,
            )
            return ProbeResult(
                probe_name=probe_name, passed=False,
                behavioral_evidence={}, substrate_evidence={},
                duration_ms=int(
                    (time.monotonic() - probe_start) * 1000,
                ),
                failure_reason=(
                    f"probe raised: {type(exc).__name__}: {exc}"
                ),
            )
        # Validate result shape + evidence keys.
        if not isinstance(result, ProbeResult):
            return ProbeResult(
                probe_name=probe_name, passed=False,
                behavioral_evidence={}, substrate_evidence={},
                duration_ms=int(
                    (time.monotonic() - probe_start) * 1000,
                ),
                failure_reason=(
                    f"probe returned {type(result).__name__}, "
                    f"expected ProbeResult"
                ),
            )
        # Per AC2: validate declared keys + reject shallow evidence
        # even if the probe reported passed=True.
        b_ok, b_reason = _validate_evidence(
            result.behavioral_evidence, required_b,
            "behavioral", probe_name,
        )
        s_ok, s_reason = _validate_evidence(
            result.substrate_evidence, required_s,
            "substrate", probe_name,
        )
        if not b_ok or not s_ok:
            reason = b_reason if not b_ok else s_reason
            return ProbeResult(
                probe_name=probe_name, passed=False,
                behavioral_evidence=result.behavioral_evidence,
                substrate_evidence=result.substrate_evidence,
                duration_ms=result.duration_ms,
                failure_reason=f"shallow_evidence: {reason}",
            )
        return result


def _format_soak_result_prose(result: SoakSuiteResult) -> str:
    """Compose agent-facing prose for a SoakSuiteResult."""
    if result.all_passed:
        return (
            f"All {len(result.per_probe)} substrate soak probes "
            f"passed in {result.total_duration_ms / 1000:.1f}s. "
            f"Substrate composition invariants hold."
        )
    failing = result.failing_probe_names()
    return (
        f"{len(failing)}/{len(result.per_probe)} substrate soak "
        f"probes FAILED in {result.total_duration_ms / 1000:.1f}s. "
        f"Failing: {', '.join(failing)}. Inspect per-probe "
        f"evidence for substrate state at failure."
    )


# ---------------------------------------------------------------------
# AC9 — autonomous-mutation gate (SUBSTRATE-SELF-TEST-V1)
# ---------------------------------------------------------------------


class SubstrateUnhealthyError(RuntimeError):
    """Raised when autonomous substrate mutation is attempted while
    the most recent substrate-soak result is failed.

    Per SUBSTRATE-SELF-TEST-V1 AC9: while a substrate.self_test_
    failed event is the most recent self-test event in the stream,
    autonomous-path git_commit + git_push refuse with this error.
    Operator-initiated paths bypass the gate (they invoke git
    directly rather than the kernel-tool handler).
    """


# Module-level health state. Updated by the post-bring-up hook in
# bring_up_substrate.py after each soak run. Default True at
# import time so callers don't get spurious "unhealthy" before
# the first soak runs (e.g. during unit tests that don't bring
# up the full substrate).
_last_self_test_passed: bool = True
_last_self_test_failing_probes: tuple[str, ...] = ()


def mark_substrate_health(
    passed: bool, failing_probes: tuple[str, ...] = (),
) -> None:
    """Update the module-level substrate-health flag. Called from
    bring_up_substrate.py's post-bring-up hook after each soak.

    Tests that exercise unhealthy paths can call this directly to
    toggle state; the default is healthy=True.
    """
    global _last_self_test_passed, _last_self_test_failing_probes
    _last_self_test_passed = passed
    _last_self_test_failing_probes = tuple(failing_probes)


def is_substrate_healthy() -> tuple[bool, tuple[str, ...]]:
    """Return (healthy, failing_probe_names). Healthy means the
    most recent soak passed AND no autonomous mutation should be
    gated."""
    return _last_self_test_passed, _last_self_test_failing_probes


def check_substrate_healthy_or_raise(*, autonomous_path: str) -> None:
    """Raise SubstrateUnhealthyError if the last soak failed.
    Called from the top of autonomous-path mutating handlers
    (git_commit, git_push) per AC9.

    ``autonomous_path`` is the surface that's being gated, used
    in the error message so the operator sees which tool was
    refused (e.g. "git_commit", "git_push").
    """
    healthy, failing = is_substrate_healthy()
    if not healthy:
        raise SubstrateUnhealthyError(
            f"{autonomous_path} refused: substrate-self-test "
            f"most recent run failed. Failing probes: "
            f"{', '.join(failing) if failing else '(unknown)'}. "
            f"Autonomous mutation paused per SUBSTRATE-SELF-TEST-V1 "
            f"AC9 until next soak passes."
        )


# ---------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------


RUN_SELF_TEST_SUITE_TOOL: dict = {
    "name": "run_self_test_suite",
    "description": (
        "Run Kernos's curated smoke test set against the "
        "improvement worktree. Returns a natural-prose summary "
        "of the outcome (pass/fail counts + failing test "
        "names). Full pytest output is recorded in the ledger "
        "event so the operator can inspect via "
        "/improvement_status. Optional extra_test_paths let "
        "the orchestrator include the spec-under-test's own "
        "test file alongside the smoke set. Optional "
        "include_soak runs the 8-probe substrate-soak suite "
        "(SUBSTRATE-SELF-TEST-V1) against the current process "
        "state — gates pre-merge + post-bring-up via the same "
        "code path."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "workspace_dir": {"type": "string"},
            "attempt_id": {
                "type": "string",
                "description": (
                    "Attempt this test run belongs to; the "
                    "result is appended to the ledger's events."
                ),
            },
            "extra_test_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Additional test file paths (relative to "
                    "worktree) to include alongside the smoke set."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Test-run timeout. Default 120s.",
            },
            "include_soak": {
                "type": "boolean",
                "description": (
                    "SUBSTRATE-SELF-TEST-V1 (2026-05-26). When "
                    "True, runs the 8-probe substrate-soak "
                    "suite alongside the smoke test set. Soak "
                    "results report under a separate `soak_*` "
                    "key in the human-readable prose; in JSON "
                    "(via CLI) under `soak_results`. Default "
                    "False for back-compat with existing "
                    "improve_kernos orchestrator callers."
                ),
            },
        },
        "required": ["workspace_dir", "attempt_id"],
    },
}


# ---------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------


_PYTEST_SUMMARY_RE = re.compile(
    r"(?P<passed>\d+) passed|(?P<failed>\d+) failed|"
    r"(?P<errors>\d+) error",
    re.IGNORECASE,
)
_FAILED_TEST_RE = re.compile(
    r"^FAILED (?P<path>[^\s:]+)(::\S+)?", re.MULTILINE,
)


def _parse_pytest_output(text: str) -> dict[str, Any]:
    """Parse the pytest output for pass/fail counts + failing names.
    Returns a dict with: outcome (pass|fail|empty), passed, failed,
    errors, failing_tests (list[str])."""
    passed = 0
    failed = 0
    errors = 0
    for m in _PYTEST_SUMMARY_RE.finditer(text):
        if m.group("passed"):
            passed = int(m.group("passed"))
        if m.group("failed"):
            failed = int(m.group("failed"))
        if m.group("errors"):
            errors = int(m.group("errors"))
    failing_tests = [m.group("path") for m in _FAILED_TEST_RE.finditer(text)]
    if failed == 0 and errors == 0 and passed > 0:
        outcome = "pass"
    elif passed == 0 and failed == 0 and errors == 0:
        outcome = "empty"
    else:
        outcome = "fail"
    return {
        "outcome": outcome,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "failing_tests": failing_tests,
    }


def _compose_prose(
    parsed: dict[str, Any], duration_s: float, timed_out: bool,
) -> str:
    """Compose the agent-facing prose summary."""
    if timed_out:
        return (
            f"Tests didn't complete within the timeout "
            f"({duration_s:.0f}s). Consider raising "
            f"KERNOS_SELF_TEST_TIMEOUT_SEC or scoping the run "
            f"narrower."
        )
    if parsed["outcome"] == "empty":
        return (
            "Test run produced no pytest results — possibly "
            "the smoke files weren't found in the worktree, "
            "or pytest itself failed to start. Inspect the "
            "ledger event for the raw output."
        )
    if parsed["outcome"] == "pass":
        return (
            f"{parsed['passed']} smoke tests passed in "
            f"{duration_s:.1f}s. Substrate invariants hold."
        )
    # Failure case
    failing = parsed["failing_tests"][:5]
    extra = (
        f" + {len(parsed['failing_tests']) - 5} more"
        if len(parsed["failing_tests"]) > 5 else ""
    )
    fail_list = ", ".join(failing) if failing else "(see ledger for names)"
    return (
        f"{parsed['failed']} failed, {parsed['errors']} errors, "
        f"{parsed['passed']} passed in {duration_s:.1f}s. "
        f"Failing: {fail_list}{extra}."
    )


# ---------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------


async def handle_run_self_test_suite(
    *, tool_input: dict, instance_id: str, data_dir: str,
) -> str:
    """Run the curated smoke set + any extra_test_paths against
    the improvement worktree. When include_soak=True, also runs
    the SUBSTRATE-SELF-TEST-V1 soak suite against the current
    process state. Returns natural-prose summary."""
    from kernos.kernel.improvement_workspace import (
        validate_workspace_path,
    )

    workspace_dir = tool_input.get("workspace_dir", "")
    attempt_id = tool_input.get("attempt_id", "")
    extra_test_paths = tool_input.get("extra_test_paths", []) or []
    timeout_seconds = int(
        tool_input.get("timeout_seconds", 0) or _default_timeout()
    )
    include_soak = bool(tool_input.get("include_soak", False))

    if not attempt_id:
        return "`attempt_id` is required so the result writes to the ledger."

    ok, reason = validate_workspace_path(
        claimed_path=workspace_dir,
        instance_id=instance_id, data_dir=data_dir,
    )
    if not ok:
        return reason

    # Build the test path list. Smoke files first, then extras.
    # Filter to files that exist in the worktree (best-effort —
    # pytest itself reports the rest).
    from pathlib import Path
    test_paths: list[str] = []
    wt = Path(workspace_dir)
    for p in SMOKE_TEST_FILES:
        if (wt / p).is_file():
            test_paths.append(p)
    for p in extra_test_paths:
        # Reject anything outside the worktree
        if ".." in p.split(os.sep) or os.path.isabs(p):
            return (
                f"extra_test_path `{p}` is outside the worktree "
                f"or contains `..` segments. Only worktree-relative "
                f"paths are allowed."
            )
        if (wt / p).is_file():
            test_paths.append(p)
    if not test_paths:
        return (
            "No test paths to run — smoke files weren't found in "
            "the worktree, and no extra_test_paths resolved to "
            "real files. Verify the worktree is a Kernos "
            "checkout."
        )

    # Run pytest.
    import time
    start = time.monotonic()
    cmd = ["python", "-m", "pytest", "-q", "--no-header"] + test_paths
    timed_out = False
    out_text = ""
    err_text = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workspace_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds,
        )
        out_text = out_b.decode("utf-8", errors="replace")
        err_text = err_b.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
    except FileNotFoundError as exc:
        return (
            f"Couldn't launch pytest: {exc}. Is python + pytest "
            f"installed in the worktree's environment?"
        )

    duration = time.monotonic() - start
    parsed = _parse_pytest_output(out_text + err_text)
    prose = _compose_prose(parsed, duration, timed_out)

    # Write to ledger.
    try:
        from kernos.kernel import improvement_ledger as _ledger
        # Need the instance_db connection. Lazy-open since
        # this kernel tool runs outside the handler's normal
        # request scope.
        import aiosqlite
        from pathlib import Path as _Path
        db_path = _Path(data_dir) / "instance.db"
        async with aiosqlite.connect(str(db_path)) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await _ledger.append_event(
                conn,
                attempt_id=attempt_id,
                kind="self_test_result",
                detail=prose,
            )
            test_outcome = (
                "timeout" if timed_out
                else ("pass" if parsed["outcome"] == "pass" else "fail")
            )
            # Update test_outcome.
            await _ledger.update_attempt(
                conn, attempt_id=attempt_id, test_outcome=test_outcome,
            )
            # First-cycle pass sets first_pass_green.
            if test_outcome == "pass":
                row = await _ledger.get_attempt(conn, attempt_id)
                if row and row.get("first_pass_green") is None:
                    await _ledger.update_attempt(
                        conn, attempt_id=attempt_id, first_pass_green=1,
                    )
    except Exception as exc:
        logger.warning(
            "SELF_TEST_LEDGER_WRITE_FAILED attempt=%s exc=%s",
            attempt_id, exc,
        )

    # SUBSTRATE-SELF-TEST-V1 (2026-05-26): optional soak suite.
    # Runs against the CURRENT process state (not the worktree)
    # so it exercises the substrate the agent is currently
    # running with, not the snapshot in the improvement workspace.
    # Reported under a separate prose segment so smoke + soak
    # outcomes don't get conflated.
    if include_soak:
        try:
            runner = SubstrateSoakRunner()
            soak_result = await runner.run_all()
            soak_prose = _format_soak_result_prose(soak_result)
            prose = f"{prose}\n\nSoak suite: {soak_prose}"
            # Loud per-probe log on any failure for operator triage.
            if not soak_result.all_passed:
                for probe in soak_result.per_probe:
                    if not probe.passed:
                        logger.warning(
                            "SOAK_PROBE_FAILED probe=%s reason=%s "
                            "duration_ms=%d",
                            probe.probe_name, probe.failure_reason,
                            probe.duration_ms,
                        )
        except Exception as exc:
            logger.exception(
                "SOAK_RUNNER_RAISED exc=%s — surfacing as soak failure",
                exc,
            )
            prose = (
                f"{prose}\n\nSoak suite: runner raised "
                f"{type(exc).__name__}: {exc} — soak considered "
                f"failed."
            )

    return prose


# ---------------------------------------------------------------------
# CLI entry — SUBSTRATE-SELF-TEST-V1 AC11
# ---------------------------------------------------------------------


def _cli_main() -> int:
    """`python -m kernos.kernel.self_test_gate` entry.

    Single canonical contract per spec — CI workflow, local
    developer script, and post-bring-up hook all invoke this
    same path so "what gates merges" never drifts from "what
    the substrate runs against itself."

    Exit codes:
      0 — all gates passed
      1 — one or more soak probes failed
      2 — runner-level error (import failure, etc.)
      3 — CLI invocation error (bad flag, etc.)

    Flags:
      --include-soak    Run the 8-probe substrate-soak suite.
                        Without this flag the CLI just reports
                        "smoke gate not runnable standalone"
                        because smoke requires a worktree +
                        attempt_id from the orchestrator path.
      --json            Emit machine-readable JSON. Default
                        human-readable prose.
    """
    import argparse
    import json as _json
    import sys as _sys

    parser = argparse.ArgumentParser(
        prog="python -m kernos.kernel.self_test_gate",
        description=(
            "Substrate self-test gate. SUBSTRATE-SELF-TEST-V1 "
            "(2026-05-26)."
        ),
    )
    parser.add_argument(
        "--include-soak", action="store_true",
        help="Run the 8-probe substrate-soak suite.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of prose.",
    )
    args = parser.parse_args()

    if not args.include_soak:
        msg = (
            "Standalone CLI invocation requires --include-soak. "
            "The smoke gate path requires a worktree + attempt_id "
            "from the improve_kernos orchestrator path; only the "
            "soak suite runs standalone."
        )
        if args.json:
            print(_json.dumps({
                "ok": False, "reason": "cli_requires_include_soak",
                "message": msg,
            }))
        else:
            print(msg, file=_sys.stderr)
        return 3

    async def _run() -> SoakSuiteResult:
        runner = SubstrateSoakRunner()
        return await runner.run_all()

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        if args.json:
            print(_json.dumps({
                "ok": False, "reason": "runner_error",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }))
        else:
            print(
                f"Substrate soak runner raised "
                f"{type(exc).__name__}: {exc}",
                file=_sys.stderr,
            )
        return 2

    if args.json:
        payload = {
            "ok": result.all_passed,
            "total_duration_ms": result.total_duration_ms,
            "soak_results": {
                p.probe_name: {
                    "passed": p.passed,
                    "duration_ms": p.duration_ms,
                    "failure_reason": p.failure_reason,
                    "behavioral_evidence": p.behavioral_evidence,
                    "substrate_evidence": p.substrate_evidence,
                }
                for p in result.per_probe
            },
        }
        print(_json.dumps(payload, indent=2, default=str))
    else:
        print(_format_soak_result_prose(result))
        if not result.all_passed:
            for probe in result.per_probe:
                if not probe.passed:
                    print(
                        f"  FAIL {probe.probe_name}: "
                        f"{probe.failure_reason}",
                        file=_sys.stderr,
                    )

    return 0 if result.all_passed else 1


if __name__ == "__main__":
    # Module-double-load fix: when invoked via
    # `python -m kernos.kernel.self_test_gate`, this module loads
    # as __main__. Probes import ProbeResult from
    # kernos.kernel.self_test_gate — a separate module instance
    # under Python's import system. The runner's
    # `isinstance(result, ProbeResult)` check then compares
    # __main__.ProbeResult vs kernos.kernel.self_test_gate.ProbeResult
    # (different class objects → spurious "expected ProbeResult"
    # failures across all probes).
    #
    # Fix: re-import _cli_main via the canonical module path so
    # all isinstance checks use a single ProbeResult class object.
    import sys
    from kernos.kernel.self_test_gate import _cli_main as _canonical_cli
    sys.exit(_canonical_cli())
