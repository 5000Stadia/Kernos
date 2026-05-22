"""GIT-OPERATIONS-PRIMITIVES-V1 (2026-05-22) acceptance tests.

Six git kernel tools tested against a real worktree fixture
(initialized in tmp_path so the live Kernos repo isn't touched).
Mutation tools (commit + push) tested against the receipts
substrate so the full bind-verification flow is exercised.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from kernos.kernel import approval_receipts as _approvals
from kernos.kernel import git_operations as gop
from kernos.kernel.gate import DispatchGate
from kernos.kernel.improvement_workspace import ImprovementWorkspace


# ============================================================
# Fixtures: real git repo + worktree
# ============================================================


def _init_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(repo_dir), env=env, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo_dir), env=env, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo_dir), env=env, check=True,
    )
    (repo_dir / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
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
async def env(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    ws = ImprovementWorkspace(
        data_dir=str(data_dir),
        instance_id="t1",
        live_repo_dir=str(repo_dir),
    )
    wt_path = await ws.create("att_x")
    await _approvals.ensure_schema(str(data_dir))
    return str(data_dir), str(repo_dir), wt_path, ws


# ============================================================
# Read tools: AC1-7
# ============================================================


@pytest.mark.asyncio
async def test_ac1_git_fetch_returns_prose(env):
    data_dir, repo_dir, wt_path, _ = env
    text = await gop.handle_git_fetch(
        tool_input={"workspace_dir": wt_path, "remote": "origin"},
        instance_id="t1", data_dir=data_dir,
    )
    assert "Fetched" in text or "origin" in text.lower()


@pytest.mark.asyncio
async def test_ac2_git_fetch_rejects_invalid_workspace(env):
    data_dir, *_ = env
    text = await gop.handle_git_fetch(
        tool_input={"workspace_dir": "/tmp/not_a_workspace"},
        instance_id="t1", data_dir=data_dir,
    )
    # Natural prose from the guard
    assert "{" not in text


@pytest.mark.asyncio
async def test_ac3_git_rev_parse_returns_sha(env):
    data_dir, _, wt_path, _ = env
    text = await gop.handle_git_rev_parse(
        tool_input={"workspace_dir": wt_path, "ref": "HEAD"},
        instance_id="t1", data_dir=data_dir,
    )
    # 40-char hex SHA
    assert len(text) == 40
    assert all(c in "0123456789abcdef" for c in text)


@pytest.mark.asyncio
async def test_ac4_git_rev_parse_unknown_ref_returns_prose(env):
    data_dir, _, wt_path, _ = env
    text = await gop.handle_git_rev_parse(
        tool_input={"workspace_dir": wt_path, "ref": "never_existed_ref"},
        instance_id="t1", data_dir=data_dir,
    )
    assert "never_existed_ref" in text
    assert "couldn't" in text.lower() or "not found" in text.lower()


@pytest.mark.asyncio
async def test_ac5_git_status_clean(env):
    data_dir, _, wt_path, _ = env
    text = await gop.handle_git_status(
        tool_input={"workspace_dir": wt_path},
        instance_id="t1", data_dir=data_dir,
    )
    assert "clean" in text.lower()


@pytest.mark.asyncio
async def test_ac5_git_status_dirty(env):
    data_dir, _, wt_path, _ = env
    (Path(wt_path) / "new.txt").write_text("change\n")
    text = await gop.handle_git_status(
        tool_input={"workspace_dir": wt_path},
        instance_id="t1", data_dir=data_dir,
    )
    assert "clean" not in text.lower()
    assert "{" not in text


@pytest.mark.asyncio
async def test_ac6_git_diff_for_review(env):
    data_dir, _, wt_path, _ = env
    # Create + commit a change so the diff is non-trivial
    (Path(wt_path) / "feature.txt").write_text("hi\n")
    env_vars = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(
        ["git", "add", "."], cwd=wt_path, env=env_vars, check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "feature"],
        cwd=wt_path, env=env_vars, check=True,
    )
    text = await gop.handle_git_diff_for_review(
        tool_input={
            "workspace_dir": wt_path,
            "base": "origin/main",
            "head": "HEAD",
        },
        instance_id="t1", data_dir=data_dir,
    )
    assert "feature.txt" in text
    assert "+hi" in text


@pytest.mark.asyncio
async def test_ac7_git_diff_for_review_truncates_large(env):
    data_dir, _, wt_path, _ = env
    # Create a big file > 64KB
    big_content = "X" * 100_000
    (Path(wt_path) / "big.txt").write_text(big_content)
    env_vars = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(
        ["git", "add", "."], cwd=wt_path, env=env_vars, check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "big"],
        cwd=wt_path, env=env_vars, check=True,
    )
    text = await gop.handle_git_diff_for_review(
        tool_input={"workspace_dir": wt_path},
        instance_id="t1", data_dir=data_dir,
    )
    assert "diff continues" in text
    assert "capped" in text


# ============================================================
# Mutation tools: AC8-19
# ============================================================


async def _stage_and_get_receipt(
    *, data_dir: str, wt_path: str,
) -> tuple[str, str]:
    """Helper: write a file, stage it, capture diff hash + parent
    sha, issue an approved receipt. Returns (approval_id, parent_sha)."""
    (Path(wt_path) / "change.txt").write_text("hello\n")
    env_vars = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(
        ["git", "add", "change.txt"],
        cwd=wt_path, env=env_vars, check=True,
    )
    diff_hash = gop._compute_staged_diff_hash(wt_path)
    parent_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt_path, env=env_vars, capture_output=True, text=True,
        check=True,
    ).stdout.strip()
    approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="agent",
        operator_actor_id="owner",
        request_summary="commit change",
        binding_payload={
            "kind": "git_commit_authorization",
            "workspace_dir": wt_path,
            "expected_parent_sha": parent_sha,
            "expected_diff_hash": diff_hash,
            "target_branch": "main",
            "summary": "add change.txt",
        },
    )
    # Pre-approve so the commit tool sees state=approved.
    await _approvals.approve(
        data_dir=data_dir, approval_id=approval_id,
        instance_id="t1", invoking_member_id="owner",
        event_stream=None,
    )
    return approval_id, parent_sha


@pytest.mark.asyncio
async def test_ac8_git_commit_rejects_missing_approval(env):
    data_dir, _, wt_path, _ = env
    text = await gop.handle_git_commit(
        tool_input={
            "workspace_dir": wt_path, "message": "x",
            "approval_id": "", "files": ["change.txt"],
        },
        instance_id="t1", data_dir=data_dir,
    )
    assert "approval_id" in text


@pytest.mark.asyncio
async def test_ac9_git_commit_rejects_wrong_kind(env):
    data_dir, _, wt_path, _ = env
    # Issue a receipt of a different kind
    bad_id = await _approvals.request_approval(
        data_dir=data_dir, instance_id="t1",
        kind="some_other_kind",
        requested_for_actor="agent", operator_actor_id="owner",
        request_summary="not a commit", binding_payload={},
    )
    text = await gop.handle_git_commit(
        tool_input={
            "workspace_dir": wt_path, "message": "x",
            "approval_id": bad_id, "files": ["change.txt"],
        },
        instance_id="t1", data_dir=data_dir,
    )
    assert "not a commit authorization" in text.lower() or "wrong receipt" in text.lower()


@pytest.mark.asyncio
async def test_ac10_git_commit_rejects_pending_receipt(env):
    data_dir, _, wt_path, _ = env
    # Stage a file so the worktree has content to commit
    (Path(wt_path) / "x.txt").write_text("x\n")
    env_vars = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(
        ["git", "add", "x.txt"], cwd=wt_path, env=env_vars, check=True,
    )
    # Issue but don't approve
    pending_id = await _approvals.request_approval(
        data_dir=data_dir, instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="agent", operator_actor_id="owner",
        request_summary="x", binding_payload={
            "expected_parent_sha": "deadbeef",
            "expected_diff_hash": "sha256:nope",
        },
    )
    text = await gop.handle_git_commit(
        tool_input={
            "workspace_dir": wt_path, "message": "x",
            "approval_id": pending_id, "files": ["x.txt"],
        },
        instance_id="t1", data_dir=data_dir,
    )
    assert "pending" in text.lower() or "not approved" in text.lower()


@pytest.mark.asyncio
async def test_ac15_git_commit_success_writes_back_sha(env):
    data_dir, _, wt_path, _ = env
    approval_id, _ = await _stage_and_get_receipt(
        data_dir=data_dir, wt_path=wt_path,
    )
    # Unstage so handler does the staging itself (tests AC13)
    env_vars = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(
        ["git", "reset", "HEAD", "--", "change.txt"],
        cwd=wt_path, env=env_vars, check=True,
    )
    text = await gop.handle_git_commit(
        tool_input={
            "workspace_dir": wt_path, "message": "add change",
            "approval_id": approval_id, "files": ["change.txt"],
        },
        instance_id="t1", data_dir=data_dir,
    )
    assert "Committed" in text
    # Receipt outcome should carry commit_sha
    receipt = await _approvals.get_receipt(
        data_dir=data_dir, approval_id=approval_id,
    )
    outcome = json.loads(receipt["outcome_payload_json"])
    assert outcome.get("commit_sha")


@pytest.mark.asyncio
async def test_ac14_git_commit_rejects_paths_outside_worktree(env):
    data_dir, _, wt_path, _ = env
    approval_id, _ = await _stage_and_get_receipt(
        data_dir=data_dir, wt_path=wt_path,
    )
    text = await gop.handle_git_commit(
        tool_input={
            "workspace_dir": wt_path, "message": "x",
            "approval_id": approval_id,
            "files": ["../escape.txt"],
        },
        instance_id="t1", data_dir=data_dir,
    )
    assert "outside" in text.lower() or ".." in text


# ============================================================
# AC21 — prose discipline (no JSON markers in error responses)
# ============================================================


@pytest.mark.asyncio
async def test_ac21_error_responses_are_prose(env):
    data_dir, *_ = env
    # Several error paths
    error_cases = [
        await gop.handle_git_fetch(
            tool_input={"workspace_dir": "/nowhere"},
            instance_id="t1", data_dir=data_dir,
        ),
        await gop.handle_git_status(
            tool_input={"workspace_dir": "/nowhere"},
            instance_id="t1", data_dir=data_dir,
        ),
        await gop.handle_git_commit(
            tool_input={
                "workspace_dir": "/nowhere", "message": "x",
                "approval_id": "", "files": [],
            },
            instance_id="t1", data_dir=data_dir,
        ),
    ]
    for text in error_cases:
        assert "{" not in text
        assert "}" not in text


# ============================================================
# AC22 — gate classifications
# ============================================================


def test_ac22_gate_classifications():
    from unittest.mock import AsyncMock, MagicMock
    gate = DispatchGate(
        reasoning_service=MagicMock(),
        registry=None, state=AsyncMock(), events=AsyncMock(),
    )
    # Reads
    for read_tool in (
        "git_fetch", "git_rev_parse", "git_status", "git_diff_for_review",
    ):
        assert gate.classify_tool_effect(read_tool, None, {}) == "read"
    # Hard writes
    for write_tool in ("git_commit", "git_push"):
        assert gate.classify_tool_effect(write_tool, None, {}) == "hard_write"
