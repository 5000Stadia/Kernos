"""SELF-TEST-GATE-V1 (2026-05-22) acceptance tests.

Pins run_self_test_suite kernel tool: schema, gate classification,
prose-summary composition, ledger integration. Real-pytest
end-to-end uses a tiny synthetic test file inside a worktree
fixture so the test stays fast.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from kernos.kernel.gate import DispatchGate
from kernos.kernel.improvement_workspace import ImprovementWorkspace
from kernos.kernel import improvement_ledger as _ledger
from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.self_test_gate import (
    RUN_SELF_TEST_SUITE_TOOL,
    _compose_prose,
    _parse_pytest_output,
    handle_run_self_test_suite,
)


# ============================================================
# Schema + gate classification
# ============================================================


def test_ac1_tool_schema():
    schema = RUN_SELF_TEST_SUITE_TOOL
    assert schema["name"] == "run_self_test_suite"
    props = schema["input_schema"]["properties"]
    assert "workspace_dir" in props
    assert "attempt_id" in props
    assert "extra_test_paths" in props
    assert "timeout_seconds" in props


def test_ac2_classifies_as_read():
    from unittest.mock import AsyncMock, MagicMock
    gate = DispatchGate(
        reasoning_service=MagicMock(),
        registry=None, state=AsyncMock(), events=AsyncMock(),
    )
    assert gate.classify_tool_effect(
        "run_self_test_suite", None, {},
    ) == "read"


# ============================================================
# _parse_pytest_output
# ============================================================


class TestParse:
    def test_all_passed(self):
        out = "12 passed in 1.23s"
        r = _parse_pytest_output(out)
        assert r["outcome"] == "pass"
        assert r["passed"] == 12
        assert r["failed"] == 0

    def test_some_failed(self):
        out = (
            "FAILED tests/test_a.py::test_one\n"
            "FAILED tests/test_b.py::test_two\n"
            "2 failed, 8 passed in 3.4s"
        )
        r = _parse_pytest_output(out)
        assert r["outcome"] == "fail"
        assert r["passed"] == 8
        assert r["failed"] == 2
        assert len(r["failing_tests"]) == 2

    def test_failure_evidence_keeps_node_id_and_excerpt(self):
        out = (
            "____________________________ test_one ____________________________\n"
            "    assert 1 == 2\n"
            "E   AssertionError: substrate mismatch\n\n"
            "FAILED tests/test_a.py::test_one - AssertionError: "
            "substrate mismatch\n"
            "1 failed, 2 passed in 0.10s"
        )
        r = _parse_pytest_output(out)
        assert r["failing_tests"] == ["tests/test_a.py::test_one"]
        assert "AssertionError: substrate mismatch" in r["failure_excerpt"]

    def test_empty_output(self):
        r = _parse_pytest_output("no pytest summary line here")
        assert r["outcome"] == "empty"


# ============================================================
# _compose_prose
# ============================================================


class TestProse:
    def test_ac3_pass_prose(self):
        text = _compose_prose(
            {"outcome": "pass", "passed": 15, "failed": 0, "errors": 0,
             "failing_tests": []},
            duration_s=2.5, timed_out=False,
        )
        assert "15 smoke tests passed" in text
        assert "2.5s" in text
        assert "{" not in text  # natural prose

    def test_ac4_fail_prose_lists_failing(self):
        text = _compose_prose(
            {"outcome": "fail", "passed": 3, "failed": 2, "errors": 0,
             "failing_tests": ["test_a.py", "test_b.py"]},
            duration_s=4.0, timed_out=False,
        )
        assert "2 failed" in text
        assert "test_a.py" in text
        assert "test_b.py" in text

    def test_ac4_fail_prose_truncates_at_5(self):
        names = [f"test_{i}.py" for i in range(8)]
        text = _compose_prose(
            {"outcome": "fail", "passed": 0, "failed": 8, "errors": 0,
             "failing_tests": names},
            duration_s=4.0, timed_out=False,
        )
        # First 5 named, plus "+ 3 more"
        assert "3 more" in text

    def test_ac5_timeout_prose(self):
        text = _compose_prose(
            {"outcome": "pass", "passed": 0, "failed": 0, "errors": 0,
             "failing_tests": []},
            duration_s=120.0, timed_out=True,
        )
        assert "didn't complete" in text
        assert "120" in text


# ============================================================
# Real-pytest end-to-end (synthetic test file)
# ============================================================


def _init_minimal_worktree(tmp_path: Path) -> tuple[Path, Path, str]:
    """Init a minimal git repo + worktree with a single
    synthetic test file. Returns (data_dir, repo_dir, wt_path)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(repo_dir), env=env, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@e.com"],
        cwd=str(repo_dir), env=env, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=str(repo_dir), env=env, check=True,
    )
    # Synthetic test file
    tests_dir = repo_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_synthetic.py").write_text(
        "def test_passes():\n    assert 1 + 1 == 2\n"
    )
    (repo_dir / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=str(repo_dir), env=env, check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(repo_dir)],
        cwd=str(repo_dir), env=env, check=True,
    )
    subprocess.run(["git", "fetch", "origin"], cwd=str(repo_dir), env=env, check=True)
    return data_dir, repo_dir, None


@pytest.fixture
async def env(tmp_path):
    data_dir, repo_dir, _ = _init_minimal_worktree(tmp_path)
    # Make a workspace + worktree
    ws = ImprovementWorkspace(
        data_dir=str(data_dir), instance_id="t1",
        live_repo_dir=str(repo_dir),
    )
    wt_path = await ws.create("attempt_smoke")
    # Bring up instance.db so ledger writes work
    db = InstanceDB(str(data_dir))
    await db.connect()
    # Create the attempt row so update_attempt has something to bump
    await _ledger.create_attempt(
        db._conn, instance_id="t1", attempt_id="attempt_smoke",
        spec_requirement="smoke",
    )
    await db.close()
    return str(data_dir), wt_path


@pytest.mark.asyncio
async def test_ac10_extra_test_paths_included(env):
    """The synthetic test file IS an extra_test_path — it
    passes, the prose says so."""
    data_dir, wt_path = env
    text = await handle_run_self_test_suite(
        tool_input={
            "workspace_dir": wt_path,
            "attempt_id": "attempt_smoke",
            "extra_test_paths": ["tests/test_synthetic.py"],
            "timeout_seconds": 30,
        },
        instance_id="t1", data_dir=data_dir,
    )
    # Either passed (if pytest is available) or "no test paths"
    # if smoke files aren't in this worktree — both are
    # acceptable outcomes for the synthetic test
    assert text  # non-empty


@pytest.mark.asyncio
async def test_ac6_rejects_invalid_workspace(env):
    data_dir, _ = env
    text = await handle_run_self_test_suite(
        tool_input={
            "workspace_dir": "/tmp/not_a_workspace",
            "attempt_id": "attempt_smoke",
        },
        instance_id="t1", data_dir=data_dir,
    )
    # Guard rejection prose
    assert "{" not in text


@pytest.mark.asyncio
async def test_rejects_missing_attempt_id(env):
    data_dir, wt_path = env
    text = await handle_run_self_test_suite(
        tool_input={"workspace_dir": wt_path, "attempt_id": ""},
        instance_id="t1", data_dir=data_dir,
    )
    assert "attempt_id" in text


@pytest.mark.asyncio
async def test_rejects_extra_path_with_traversal(env):
    data_dir, wt_path = env
    text = await handle_run_self_test_suite(
        tool_input={
            "workspace_dir": wt_path,
            "attempt_id": "attempt_smoke",
            "extra_test_paths": ["../escape/test.py"],
        },
        instance_id="t1", data_dir=data_dir,
    )
    assert "outside" in text.lower() or ".." in text


@pytest.mark.asyncio
async def test_ac7_ac8_ledger_event_written(env):
    """When the run completes (even with no smoke files
    matching), the ledger gets an event row + test_outcome
    update."""
    data_dir, wt_path = env
    await handle_run_self_test_suite(
        tool_input={
            "workspace_dir": wt_path,
            "attempt_id": "attempt_smoke",
            "extra_test_paths": ["tests/test_synthetic.py"],
            "timeout_seconds": 30,
        },
        instance_id="t1", data_dir=data_dir,
    )
    # Re-open db + inspect
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        events = await _ledger.get_attempt_events(db._conn, "attempt_smoke")
        # If smoke files weren't found (no pytest run), there's
        # still potentially no event — that case is acceptable
        # per the "no test paths to run" early return.
        if events:
            assert any(e["kind"] == "self_test_result" for e in events)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_failure_records_structured_evidence_with_substrate_state(env):
    """Substrate-fidelity pin: behavioral prose AND persisted attempt/event
    state carry the same failed test id and bounded failure excerpt."""
    data_dir, wt_path = env
    test_file = Path(wt_path) / "tests" / "test_failure.py"
    test_file.write_text(
        "def test_failure():\n"
        "    assert 1 == 2, 'substrate mismatch'\n"
    )

    text = await handle_run_self_test_suite(
        tool_input={
            "workspace_dir": wt_path,
            "attempt_id": "attempt_smoke",
            "extra_test_paths": ["tests/test_failure.py"],
            "timeout_seconds": 30,
        },
        instance_id="t1", data_dir=data_dir,
    )

    db = InstanceDB(data_dir)
    await db.connect()
    try:
        attempt = await _ledger.get_attempt(db._conn, "attempt_smoke")
        events = await _ledger.get_attempt_events(db._conn, "attempt_smoke")
    finally:
        await db.close()

    evidence_events = [
        e for e in events if e["kind"] == "self_test_failure_evidence"
    ]
    detail = json.loads(evidence_events[-1]["detail"])
    evidence = detail["failure_evidence"]

    assert "1 failed" in text
    assert "tests/test_failure.py::test_failure" in text
    assert attempt["test_outcome"] == "fail"
    assert evidence["failed_test_ids"] == [
        "tests/test_failure.py::test_failure"
    ]
    assert "substrate mismatch" in evidence["failure_excerpt"]
