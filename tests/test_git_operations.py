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
async def test_git_commit_succeeds_without_configured_identity(
    env, monkeypatch,
):
    """A fresh deploy clone that only ever pulls (e.g. kernos-main) has NO
    git author identity, so `git commit` dies with 'Author identity unknown'
    and silently caps every self-improvement run at the final commit step.
    The loop must commit anyway under a fallback Kernos identity. (Observed
    live: att_948028d50c69 — spec GREEN, impl GREEN, auto-approved, then
    commit_refused/head_unchanged purely from the missing identity.)"""
    data_dir, repo_dir, wt_path, _ = env
    # Neutralize ambient global/system identity so the test is deterministic.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    env_vars = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    for cfg_dir in (repo_dir, wt_path):
        for key in ("user.name", "user.email"):
            subprocess.run(
                ["git", "config", "--unset", key],
                cwd=cfg_dir, env=env_vars,
            )
    approval_id, _ = await _stage_and_get_receipt(
        data_dir=data_dir, wt_path=wt_path,
    )
    text = await gop.handle_git_commit(
        tool_input={
            "workspace_dir": wt_path, "message": "add change.txt",
            "approval_id": approval_id, "files": ["change.txt"],
        },
        instance_id="t1", data_dir=data_dir,
    )
    assert "Committed" in text, f"commit should succeed via fallback: {text}"
    author = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>"],
        cwd=wt_path, env=env_vars, capture_output=True, text=True,
    ).stdout.strip()
    assert author == "Kernos <kernos@kernos.local>", author


@pytest.mark.asyncio
async def test_git_commit_preserves_configured_identity(env):
    """When the worktree DOES have an author identity, the fallback must NOT
    override it."""
    data_dir, _, wt_path, _ = env  # fixture configures Test <test@example.com>
    approval_id, _ = await _stage_and_get_receipt(
        data_dir=data_dir, wt_path=wt_path,
    )
    text = await gop.handle_git_commit(
        tool_input={
            "workspace_dir": wt_path, "message": "add change.txt",
            "approval_id": approval_id, "files": ["change.txt"],
        },
        instance_id="t1", data_dir=data_dir,
    )
    assert "Committed" in text
    author = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>"],
        cwd=wt_path, capture_output=True, text=True,
    ).stdout.strip()
    assert author == "Test <test@example.com>", author


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
async def test_git_push_structured_success_requires_origin_confirmation(
    tmp_path, monkeypatch,
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    await _approvals.ensure_schema(str(data_dir))
    workspace = data_dir / "t1" / "improvement_workspace" / "att_push"
    workspace.mkdir(parents=True)
    approval_id = await _approvals.request_approval(
        data_dir=str(data_dir),
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="agent",
        operator_actor_id="owner",
        request_summary="push change",
        binding_payload={
            "kind": "git_commit_authorization",
            "workspace_dir": str(workspace),
            "expected_parent_sha": "parent_sha",
            "expected_diff_hash": "sha256:test",
            "target_branch": "main",
        },
    )
    await _approvals.approve(
        data_dir=str(data_dir),
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    await _approvals.set_outcome_field(
        data_dir=str(data_dir),
        approval_id=approval_id,
        field="commit_sha",
        value="commit_sha",
    )
    origin_reads = 0

    async def fake_run_git(args, *, cwd):
        nonlocal origin_reads
        if args == ["rev-parse", "HEAD"]:
            return 0, "commit_sha\n", ""
        if args == ["fetch", "origin"]:
            return 0, "fetched\n", ""
        if args == ["rev-parse", "--verify", "origin/main"]:
            origin_reads += 1
            if origin_reads == 1:
                return 0, "parent_sha\n", ""
            return 0, "other_sha\n", ""
        if args == ["push", "origin", "HEAD:main"]:
            return 0, "pushed\n", ""
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(gop, "_run_git", fake_run_git)

    result = await gop.handle_git_push(
        tool_input={
            "workspace_dir": str(workspace),
            "target_branch": "main",
            "approval_id": approval_id,
            "return_structured": True,
        },
        instance_id="t1",
        data_dir=str(data_dir),
    )
    assert result["ok"] is False
    assert result["reason"] == "post_push_unconfirmed"
    assert result["origin_confirmed"] is False


@pytest.mark.asyncio
async def test_git_push_already_pushed_commit_is_idempotent_success(
    tmp_path, monkeypatch,
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    await _approvals.ensure_schema(str(data_dir))
    workspace = data_dir / "t1" / "improvement_workspace" / "att_push"
    workspace.mkdir(parents=True)
    approval_id = await _approvals.request_approval(
        data_dir=str(data_dir),
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="agent",
        operator_actor_id="owner",
        request_summary="push change",
        binding_payload={
            "kind": "git_commit_authorization",
            "workspace_dir": str(workspace),
            "expected_parent_sha": "parent_sha",
            "expected_diff_hash": "sha256:test",
            "target_branch": "main",
        },
    )
    await _approvals.approve(
        data_dir=str(data_dir),
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    await _approvals.set_outcome_field(
        data_dir=str(data_dir),
        approval_id=approval_id,
        field="commit_sha",
        value="commit_sha",
    )

    async def fake_run_git(args, *, cwd):
        if args == ["rev-parse", "HEAD"]:
            return 0, "commit_sha\n", ""
        if args == ["fetch", "origin"]:
            return 0, "fetched\n", ""
        if args == ["rev-parse", "--verify", "origin/main"]:
            return 0, "commit_sha\n", ""
        if args == ["push", "origin", "HEAD:main"]:
            raise AssertionError("already-pushed commit must not push again")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(gop, "_run_git", fake_run_git)

    result = await gop.handle_git_push(
        tool_input={
            "workspace_dir": str(workspace),
            "target_branch": "main",
            "approval_id": approval_id,
            "return_structured": True,
        },
        instance_id="t1",
        data_dir=str(data_dir),
    )
    assert result["ok"] is True
    assert result["reason"] == "already_pushed"
    assert result["commit_sha"] == "commit_sha"
    assert result["origin_confirmed"] is True


@pytest.mark.asyncio
async def test_git_push_fetch_failure_does_not_confirm_stale_origin_ref(
    tmp_path, monkeypatch,
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    await _approvals.ensure_schema(str(data_dir))
    workspace = data_dir / "t1" / "improvement_workspace" / "att_push"
    workspace.mkdir(parents=True)
    approval_id = await _approvals.request_approval(
        data_dir=str(data_dir),
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="agent",
        operator_actor_id="owner",
        request_summary="push change",
        binding_payload={
            "kind": "git_commit_authorization",
            "workspace_dir": str(workspace),
            "expected_parent_sha": "parent_sha",
            "expected_diff_hash": "sha256:test",
            "target_branch": "main",
        },
    )
    await _approvals.approve(
        data_dir=str(data_dir),
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    await _approvals.set_outcome_field(
        data_dir=str(data_dir),
        approval_id=approval_id,
        field="commit_sha",
        value="commit_sha",
    )

    async def fake_run_git(args, *, cwd):
        if args == ["rev-parse", "HEAD"]:
            return 0, "commit_sha\n", ""
        if args == ["fetch", "origin"]:
            return 1, "", "network down"
        if args == ["rev-parse", "--verify", "origin/main"]:
            raise AssertionError("stale origin ref must not confirm push")
        if args == ["push", "origin", "HEAD:main"]:
            raise AssertionError("fetch failure must not push")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(gop, "_run_git", fake_run_git)

    result = await gop.handle_git_push(
        tool_input={
            "workspace_dir": str(workspace),
            "target_branch": "main",
            "approval_id": approval_id,
            "return_structured": True,
        },
        instance_id="t1",
        data_dir=str(data_dir),
    )
    assert result["ok"] is False
    assert result["reason"] == "origin_fetch_failed"
    assert result["origin_confirmed"] is False
    assert result["commit_sha"] == "commit_sha"


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
