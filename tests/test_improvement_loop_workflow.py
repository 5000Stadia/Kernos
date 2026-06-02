"""IMPROVEMENT-LOOP-WORKFLOW-V1 (2026-05-22) acceptance tests.

Pins the orchestrator's happy-path composition + the
improve_kernos tool surface. Uses stubbed consult_fn so the
test doesn't fire real coding-agent subprocesses; uses a
minimal git repo fixture (mirrors the pattern from
test_git_operations.py) so the workspace + git tools work
end-to-end.

Recovery cycles, mid-attempt resume, restart_self firing — all
deferred per the spec's scope cuts. Tests pin v1 happy-path
behavior + the documented failure paths only.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from kernos.kernel import approval_receipts as _approvals
from kernos.kernel import improvement_ledger as _ledger
from kernos.kernel.gate import DispatchGate
from kernos.kernel.improvement_loop_workflow import (
    IMPROVE_KERNOS_TOOL,
    ImprovementLoopOrchestrator,
)
from kernos.kernel import improvement_loop_workflow as _workflow
from kernos.kernel.instance_db import InstanceDB


def _init_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
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
    (repo_dir / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=str(repo_dir), env=env, check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(repo_dir)],
        cwd=str(repo_dir), env=env, check=True,
    )
    subprocess.run(
        ["git", "fetch", "origin"], cwd=str(repo_dir), env=env, check=True,
    )


@pytest.fixture
async def loop_env(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    # InstanceDB connect (creates the schema)
    db = InstanceDB(str(data_dir))
    await db.connect()
    await db.close()
    await _approvals.ensure_schema(str(data_dir))
    return str(data_dir), str(repo_dir)


# ============================================================
# Tool surface
# ============================================================


def test_ac13_schema_is_pinned():
    from kernos.kernel.tool_catalog import ALWAYS_PINNED
    assert "improve_kernos" in ALWAYS_PINNED


def test_ac13_classified_hard_write():
    from unittest.mock import AsyncMock, MagicMock
    gate = DispatchGate(
        reasoning_service=MagicMock(),
        registry=None, state=AsyncMock(), events=AsyncMock(),
    )
    assert gate.classify_tool_effect(
        "improve_kernos", None, {},
    ) == "hard_write"


def test_ac14_schema_enum_restricts_agents():
    props = IMPROVE_KERNOS_TOOL["input_schema"]["properties"]
    assert props["primary_coding_agent"]["enum"] == ["claude_code", "codex"]
    assert props["reviewer_coding_agent"]["enum"] == ["claude_code", "codex"]


def test_ac14_required_field():
    assert "spec_requirement" in IMPROVE_KERNOS_TOOL["input_schema"]["required"]


# ============================================================
# Trusted-agent allowlist enforcement (substrate level)
# ============================================================


@pytest.mark.asyncio
async def test_trusted_agent_allowlist_rejects_other(loop_env):
    data_dir, repo_dir = loop_env
    orch = ImprovementLoopOrchestrator(
        instance_id="t1", data_dir=data_dir,
        live_repo_dir=repo_dir,
        consult_fn=_make_converging_consult(),
    )
    with pytest.raises(ValueError) as excinfo:
        await orch.start_attempt(
            spec_requirement="x",
            primary_coding_agent="untrusted_agent",
        )
    assert "trusted-agent" in str(excinfo.value)


# ============================================================
# Happy path: ACs 1-6 (start → spec cycle → impl cycle → approval)
# ============================================================


def _make_converging_consult(*, converge_at_round: int = 1):
    """Build a stub consult_fn that returns convergence text
    after `converge_at_round` round, NEEDS_REVISION before."""
    call_count = {"n": 0}

    async def _consult(*, target: str, prompt: str) -> str:
        call_count["n"] += 1
        # Iterations involve author + reviewer per round.
        # Round 1 = calls 1+2, Round 2 = calls 3+4, etc.
        round_num = (call_count["n"] + 1) // 2
        if round_num < converge_at_round:
            return (
                "draft spec content\n\n"
                "STATUS: NEEDS_REVISION refine further"
            )
        return "final spec content\n\nSTATUS: GREEN"
    return _consult


def _make_diverging_consult():
    async def _consult(*, target: str, prompt: str) -> str:
        return (
            "endless drafting\n\n"
            "STATUS: NEEDS_REVISION still not ready"
        )
    return _consult


def _make_converging_consult_with_edit(edit_fn):
    edited = {"done": False}

    async def _consult(
        *, target: str, prompt: str, workspace_dir: str = "",
    ) -> str:
        if workspace_dir and not edited["done"]:
            edit_fn(Path(workspace_dir))
            edited["done"] = True
        return "final spec content\n\nSTATUS: GREEN"

    return _consult


def _approval_id_from_events(events: list[dict]) -> str:
    event = next(e for e in events if e["kind"] == "approval_requested")
    detail = event["detail"]
    return detail.split("approval_id=", 1)[1].split()[0]


@pytest.mark.asyncio
async def test_ac1_ac2_ac3_start_returns_attempt_id_creates_workspace(
    loop_env,
):
    data_dir, repo_dir = loop_env
    orch = ImprovementLoopOrchestrator(
        instance_id="t1", data_dir=data_dir,
        live_repo_dir=repo_dir,
        consult_fn=_make_converging_consult(),
    )
    attempt_id = await orch.start_attempt(
        spec_requirement="add a one-line comment to README",
    )
    assert attempt_id.startswith("att_")
    # Wait for background to complete
    await orch.wait_for_running_tasks(timeout=10)
    # Ledger row exists
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        row = await _ledger.get_attempt(db._conn, attempt_id)
        assert row is not None
        assert row["spec_requirement"] == (
            "add a one-line comment to README"
        )
        assert row["worktree_path"]
        # Worktree exists
        assert Path(row["worktree_path"]).is_dir()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ac4_ac5_spec_and_impl_cycles_converge(loop_env):
    data_dir, repo_dir = loop_env
    orch = ImprovementLoopOrchestrator(
        instance_id="t1", data_dir=data_dir,
        live_repo_dir=repo_dir,
        consult_fn=_make_converging_consult(converge_at_round=1),
    )
    attempt_id = await orch.start_attempt(
        spec_requirement="trivial change",
    )
    await orch.wait_for_running_tasks(timeout=10)
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        events = await _ledger.get_attempt_events(db._conn, attempt_id)
        kinds = [e["kind"] for e in events]
        # workspace + spec_iteration + impl_iteration + approval_requested
        assert "workspace_created" in kinds
        assert "spec_iteration" in kinds
        assert "impl_iteration" in kinds
        assert "approval_requested" in kinds
        # Attempt's iteration outcomes
        row = await _ledger.get_attempt(db._conn, attempt_id)
        assert row["spec_iterations_outcome"] == "GREEN"
        assert row["impl_iterations_outcome"] == "GREEN"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ac6_approval_receipt_issued_with_binding(loop_env):
    data_dir, repo_dir = loop_env
    orch = ImprovementLoopOrchestrator(
        instance_id="t1", data_dir=data_dir,
        live_repo_dir=repo_dir,
        consult_fn=_make_converging_consult(),
    )
    attempt_id = await orch.start_attempt(spec_requirement="x")
    await orch.wait_for_running_tasks(timeout=10)
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        events = await _ledger.get_attempt_events(db._conn, attempt_id)
        approval_events = [
            e for e in events if e["kind"] == "approval_requested"
        ]
        assert len(approval_events) == 1
        detail = approval_events[0]["detail"]
        assert "approval_id=" in detail
        approval_id = detail.split("approval_id=")[1].strip()
        # Receipt exists with the expected kind + binding
        receipt = await _approvals.get_receipt(
            data_dir=data_dir, approval_id=approval_id,
        )
        assert receipt is not None
        assert receipt["kind"] == "git_commit_authorization"
        binding = json.loads(receipt["binding_payload_json"])
        assert binding["attempt_id"] == attempt_id
        assert binding["expected_parent_sha"]
        assert binding["expected_diff_hash"].startswith("sha256:")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_impl_green_auto_proceeds_by_default(loop_env, monkeypatch):
    monkeypatch.delenv("KERNOS_IMPROVE_REQUIRE_APPROVAL", raising=False)
    data_dir, repo_dir = loop_env
    announcements: list[tuple[str, str]] = []
    approvals: list[dict] = []
    continuations: list[dict] = []

    async def _announce(space_id: str, message: str) -> None:
        announcements.append((space_id, message))

    async def _approve(**kwargs):
        approvals.append(kwargs)
        return True, "approved"

    async def _continue(**kwargs):
        continuations.append(kwargs)
        return "deployed"

    monkeypatch.setattr(_approvals, "approve", _approve)
    monkeypatch.setattr(
        _workflow, "continue_approved_improvement_commit", _continue,
    )

    orch = ImprovementLoopOrchestrator(
        instance_id="t1", data_dir=data_dir,
        live_repo_dir=repo_dir,
        consult_fn=_make_converging_consult(),
        restart_fn=lambda: None,
        announce_fn=_announce,
    )
    attempt_id = await orch.start_attempt(
        spec_requirement="x",
        origin_space_id="space_1",
        origin_member_id="member_1",
    )
    await orch.wait_for_running_tasks(timeout=10)

    db = InstanceDB(data_dir)
    await db.connect()
    try:
        events = await _ledger.get_attempt_events(db._conn, attempt_id)
        kinds = [e["kind"] for e in events]
        assert "auto_approved" in kinds
        approval_id = _approval_id_from_events(events)
    finally:
        await db.close()

    assert approvals and approvals[0]["approval_id"] == approval_id
    assert approvals[0]["invoking_member_id"] == "member_1"
    assert continuations and continuations[0]["approval_id"] == approval_id
    assert any(
        space_id == "space_1" and "deploying" in message
        for space_id, message in announcements
    )


@pytest.mark.asyncio
async def test_impl_green_require_approval_parks(loop_env, monkeypatch):
    monkeypatch.setenv("KERNOS_IMPROVE_REQUIRE_APPROVAL", "1")
    data_dir, repo_dir = loop_env
    announcements: list[tuple[str, str]] = []
    continuations: list[dict] = []
    approvals: list[dict] = []

    async def _announce(space_id: str, message: str) -> None:
        announcements.append((space_id, message))

    async def _approve(**kwargs):
        approvals.append(kwargs)
        return True, "approved"

    async def _continue(**kwargs):
        continuations.append(kwargs)
        return "deployed"

    monkeypatch.setattr(_approvals, "approve", _approve)
    monkeypatch.setattr(
        _workflow, "continue_approved_improvement_commit", _continue,
    )

    orch = ImprovementLoopOrchestrator(
        instance_id="t1", data_dir=data_dir,
        live_repo_dir=repo_dir,
        consult_fn=_make_converging_consult(),
        restart_fn=lambda: None,
        announce_fn=_announce,
    )
    attempt_id = await orch.start_attempt(
        spec_requirement="x",
        origin_space_id="space_1",
        origin_member_id="member_1",
    )
    await orch.wait_for_running_tasks(timeout=10)

    db = InstanceDB(data_dir)
    await db.connect()
    try:
        row = await _ledger.get_attempt(db._conn, attempt_id)
        events = await _ledger.get_attempt_events(db._conn, attempt_id)
        approval_id = _approval_id_from_events(events)
    finally:
        await db.close()

    assert row["final_state"] == "awaiting_commit_approval"
    assert approvals == []
    assert continuations == []
    assert any(
        f"/approve {approval_id} CONFIRM" in message
        for _space_id, message in announcements
    )


@pytest.mark.asyncio
async def test_impl_green_protected_path_parks(loop_env, monkeypatch):
    monkeypatch.delenv("KERNOS_IMPROVE_REQUIRE_APPROVAL", raising=False)
    data_dir, repo_dir = loop_env
    announcements: list[tuple[str, str]] = []
    continuations: list[dict] = []

    async def _announce(space_id: str, message: str) -> None:
        announcements.append((space_id, message))

    async def _continue(**kwargs):
        continuations.append(kwargs)
        return "deployed"

    monkeypatch.setattr(
        _workflow, "continue_approved_improvement_commit", _continue,
    )

    def _touch_start_sh(workspace: Path) -> None:
        (workspace / "start.sh").write_text("human-only\n")

    orch = ImprovementLoopOrchestrator(
        instance_id="t1", data_dir=data_dir,
        live_repo_dir=repo_dir,
        consult_fn=_make_converging_consult_with_edit(_touch_start_sh),
        restart_fn=lambda: None,
        announce_fn=_announce,
    )
    attempt_id = await orch.start_attempt(
        spec_requirement="x",
        origin_space_id="space_1",
        origin_member_id="member_1",
    )
    await orch.wait_for_running_tasks(timeout=10)

    db = InstanceDB(data_dir)
    await db.connect()
    try:
        row = await _ledger.get_attempt(db._conn, attempt_id)
    finally:
        await db.close()

    assert row["final_state"] == "awaiting_commit_approval"
    assert continuations == []
    assert any("start.sh" in message for _space_id, message in announcements)


@pytest.mark.asyncio
async def test_impl_green_oversized_diff_parks(loop_env, monkeypatch):
    monkeypatch.delenv("KERNOS_IMPROVE_REQUIRE_APPROVAL", raising=False)
    data_dir, repo_dir = loop_env
    announcements: list[tuple[str, str]] = []
    continuations: list[dict] = []

    async def _announce(space_id: str, message: str) -> None:
        announcements.append((space_id, message))

    async def _continue(**kwargs):
        continuations.append(kwargs)
        return "deployed"

    monkeypatch.setattr(
        _workflow, "continue_approved_improvement_commit", _continue,
    )

    def _write_many_files(workspace: Path) -> None:
        for index in range(_workflow._AUTO_PROCEED_MAX_FILES + 1):
            (workspace / f"large_{index}.txt").write_text(f"{index}\n")

    orch = ImprovementLoopOrchestrator(
        instance_id="t1", data_dir=data_dir,
        live_repo_dir=repo_dir,
        consult_fn=_make_converging_consult_with_edit(_write_many_files),
        restart_fn=lambda: None,
        announce_fn=_announce,
    )
    attempt_id = await orch.start_attempt(
        spec_requirement="x",
        origin_space_id="space_1",
        origin_member_id="member_1",
    )
    await orch.wait_for_running_tasks(timeout=10)

    db = InstanceDB(data_dir)
    await db.connect()
    try:
        row = await _ledger.get_attempt(db._conn, attempt_id)
    finally:
        await db.close()

    assert row["final_state"] == "awaiting_commit_approval"
    assert continuations == []
    assert any(
        "auto-proceed limit" in message
        for _space_id, message in announcements
    )


# ============================================================
# Failure paths: ACs 16-17
# ============================================================


@pytest.mark.asyncio
async def test_ac16_spec_cap_aborts_unconverged(
    loop_env, monkeypatch,
):
    monkeypatch.setenv("KERNOS_IMPROVEMENT_SPEC_ITERATION_MAX", "2")
    data_dir, repo_dir = loop_env
    orch = ImprovementLoopOrchestrator(
        instance_id="t1", data_dir=data_dir,
        live_repo_dir=repo_dir,
        consult_fn=_make_diverging_consult(),
    )
    attempt_id = await orch.start_attempt(spec_requirement="x")
    await orch.wait_for_running_tasks(timeout=10)
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        row = await _ledger.get_attempt(db._conn, attempt_id)
        assert row["final_state"] == "aborted_unconverged"
        assert row["spec_iterations_outcome"] == "ABORTED_UNCONVERGED"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ac19_consult_failure_logged(loop_env):
    """AC19: consult raises → attempt aborts with consult_failure."""
    data_dir, repo_dir = loop_env

    async def _broken_consult(*, target: str, prompt: str) -> str:
        raise RuntimeError("coding-agent unavailable")

    orch = ImprovementLoopOrchestrator(
        instance_id="t1", data_dir=data_dir,
        live_repo_dir=repo_dir,
        consult_fn=_broken_consult,
    )
    attempt_id = await orch.start_attempt(spec_requirement="x")
    await orch.wait_for_running_tasks(timeout=10)
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        row = await _ledger.get_attempt(db._conn, attempt_id)
        assert row["final_state"] == "aborted_consult_failure"
        events = await _ledger.get_attempt_events(db._conn, attempt_id)
        assert any(e["kind"] == "attempt_failed" for e in events)
    finally:
        await db.close()


# ============================================================
# Tool handler — prose responses
# ============================================================


@pytest.mark.asyncio
async def test_handle_returns_prose_with_attempt_id(loop_env):
    from kernos.kernel.improvement_loop_workflow import (
        handle_improve_kernos,
    )

    data_dir, _ = loop_env

    # Build a stub "handler" object that exposes consult_fn
    # the orchestrator can find.
    class _StubHandler:
        events = None
        _consult_fn_for_loop = staticmethod(_make_converging_consult())

    handler = _StubHandler()
    text = await handle_improve_kernos(
        handler=handler,
        tool_input={"spec_requirement": "trivial fix"},
        instance_id="t1",
        data_dir=data_dir,
    )
    await handler._last_improvement_orchestrator.wait_for_running_tasks(
        timeout=10,
    )
    assert "att_" in text
    # Natural prose, no JSON markers
    assert "{" not in text
    assert "/improvement_status" in text


@pytest.mark.asyncio
async def test_handle_missing_consult_seam_fails_before_attempt(loop_env):
    from kernos.kernel.improvement_loop_workflow import (
        handle_improve_kernos,
    )

    data_dir, _ = loop_env

    class _UnwiredHandler:
        events = None

    text = await handle_improve_kernos(
        handler=_UnwiredHandler(),
        tool_input={"spec_requirement": "trivial fix"},
        instance_id="t1",
        data_dir=data_dir,
    )
    assert "consult seam" in text
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        attempts = await _ledger.list_recent_attempts(
            db._conn, instance_id="t1", limit=5,
        )
    finally:
        await db.close()
    assert attempts == []


@pytest.mark.asyncio
async def test_handle_rejects_empty_spec_requirement():
    from kernos.kernel.improvement_loop_workflow import (
        handle_improve_kernos,
    )

    text = await handle_improve_kernos(
        handler=None, tool_input={"spec_requirement": ""},
        instance_id="t1", data_dir="/tmp",
    )
    assert "spec_requirement" in text
    assert "required" in text
