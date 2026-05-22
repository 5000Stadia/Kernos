"""Self-test gate kernel tool for the autonomous-improvement loop.

SELF-TEST-GATE-V1 (2026-05-22).

Runs a curated smoke test set against the improvement worktree
to verify the autonomous loop's commit didn't regress core
substrate invariants. v1 ships a focused set targeting the
loop's own substrate (liveness contract, provider injection,
gateway health); v2 lands the full pytest suite after the 74%
stall is diagnosed.

Per [[agent-facing-natural-simplicity]]: agent reads a short
prose summary; operator inspects full output via the ledger
event.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
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
        "test file alongside the smoke set."
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
    the improvement worktree. Returns natural-prose summary."""
    from kernos.kernel.improvement_workspace import (
        validate_workspace_path,
    )

    workspace_dir = tool_input.get("workspace_dir", "")
    attempt_id = tool_input.get("attempt_id", "")
    extra_test_paths = tool_input.get("extra_test_paths", []) or []
    timeout_seconds = int(
        tool_input.get("timeout_seconds", 0) or _default_timeout()
    )

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

    return prose
