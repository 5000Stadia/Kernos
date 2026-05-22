"""IMPROVEMENT-WORKSPACE-V1 (2026-05-22) acceptance tests.

Pins worktree lifecycle (create/remove/list/cleanup) + the
workspace-guard primitive (validate_workspace_path).

The lifecycle tests use a real git repo (initialized in tmp_path
to avoid touching the live Kernos repo). Guard tests are
path-validation only and don't need git.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from kernos.kernel.improvement_workspace import (
    ImprovementWorkspace,
    WorkspaceCreateError,
    WorkspaceRemoveError,
    validate_workspace_path,
)


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


def _init_git_repo(repo_dir: Path) -> None:
    """Initialize a bare-ish git repo with an `origin/main` ref so
    `git worktree add ... origin/main` succeeds. Self-contained:
    creates a fake remote pointing at itself."""
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
    # Initial commit so HEAD has something
    (repo_dir / "README.md").write_text("test repo\n")
    subprocess.run(
        ["git", "add", "."], cwd=str(repo_dir), env=env, check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=str(repo_dir), env=env, check=True,
    )
    # Add self as a remote named origin so `origin/main` resolves
    subprocess.run(
        ["git", "remote", "add", "origin", str(repo_dir)],
        cwd=str(repo_dir), env=env, check=True,
    )
    subprocess.run(
        ["git", "fetch", "origin"],
        cwd=str(repo_dir), env=env, check=True,
    )


@pytest.fixture
def workspace_env(tmp_path):
    """Create a (data_dir, repo_dir, ImprovementWorkspace) triple."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_dir = tmp_path / "live_repo"
    _init_git_repo(repo_dir)
    ws = ImprovementWorkspace(
        data_dir=str(data_dir),
        instance_id="test_inst",
        live_repo_dir=str(repo_dir),
    )
    return data_dir, repo_dir, ws


# ============================================================
# AC1-4: create() + path_for()
# ============================================================


@pytest.mark.asyncio
async def test_ac1_create_makes_worktree_on_branch(workspace_env):
    data_dir, repo_dir, ws = workspace_env
    wt_path = await ws.create("attempt_001")
    assert Path(wt_path).is_dir()
    # Branch exists
    result = subprocess.run(
        ["git", "branch", "--list", "improvement/attempt_001"],
        cwd=str(repo_dir), capture_output=True, text=True,
    )
    assert "improvement/attempt_001" in result.stdout


@pytest.mark.asyncio
async def test_ac3_create_raises_on_existing_path(workspace_env):
    data_dir, repo_dir, ws = workspace_env
    await ws.create("attempt_002")
    with pytest.raises(WorkspaceCreateError):
        await ws.create("attempt_002")  # second time should fail


def test_ac4_path_for_is_deterministic(workspace_env):
    data_dir, repo_dir, ws = workspace_env
    p1 = ws.path_for("attempt_x")
    p2 = ws.path_for("attempt_x")
    assert p1 == p2
    assert "improvement_workspace" in p1
    assert "attempt_x" in p1
    assert "test_inst" in p1


def test_invalid_attempt_id_rejected(workspace_env):
    data_dir, repo_dir, ws = workspace_env
    # path traversal
    with pytest.raises(WorkspaceCreateError):
        ws.path_for("../escape")
    # special chars
    with pytest.raises(WorkspaceCreateError):
        ws.path_for("a/b")
    # too long
    with pytest.raises(WorkspaceCreateError):
        ws.path_for("a" * 100)


# ============================================================
# ACs 5-7: remove()
# ============================================================


@pytest.mark.asyncio
async def test_ac5_remove_clean_branch_succeeds(workspace_env):
    data_dir, repo_dir, ws = workspace_env
    await ws.create("attempt_clean")
    # No commits on branch beyond origin/main → clean removal
    await ws.remove("attempt_clean")
    # Worktree gone
    assert not Path(ws.path_for("attempt_clean")).exists()
    # Branch gone
    result = subprocess.run(
        ["git", "branch", "--list", "improvement/attempt_clean"],
        cwd=str(repo_dir), capture_output=True, text=True,
    )
    assert not result.stdout.strip()


@pytest.mark.asyncio
async def test_ac6_remove_refuses_unpushed_commits(workspace_env):
    data_dir, repo_dir, ws = workspace_env
    wt_path = await ws.create("attempt_dirty")
    # Add a commit on the branch
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    (Path(wt_path) / "new.txt").write_text("change\n")
    subprocess.run(
        ["git", "add", "."], cwd=wt_path, env=env, check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "test commit"],
        cwd=wt_path, env=env, check=True,
    )
    # Now remove without force should refuse
    with pytest.raises(WorkspaceRemoveError) as excinfo:
        await ws.remove("attempt_dirty", force=False)
    assert "un-pushed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_ac7_remove_force_removes_anyway(workspace_env):
    data_dir, repo_dir, ws = workspace_env
    wt_path = await ws.create("attempt_force")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    (Path(wt_path) / "x.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=wt_path, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "x"],
        cwd=wt_path, env=env, check=True,
    )
    # force=True removes regardless
    await ws.remove("attempt_force", force=True)
    assert not Path(wt_path).exists()


# ============================================================
# AC8: list_active()
# ============================================================


@pytest.mark.asyncio
async def test_ac8_list_active_returns_attempts(workspace_env):
    data_dir, repo_dir, ws = workspace_env
    await ws.create("attempt_alpha")
    await ws.create("attempt_beta")
    active = await ws.list_active()
    ids = {e["attempt_id"] for e in active}
    assert "attempt_alpha" in ids
    assert "attempt_beta" in ids
    # Each entry has the documented shape
    for entry in active:
        assert "attempt_id" in entry
        assert "path" in entry
        assert "branch" in entry
        assert "created_at" in entry
        assert "age_days" in entry


# ============================================================
# AC9: cleanup_expired()
# ============================================================


@pytest.mark.asyncio
async def test_ac9_cleanup_expired_removes_old_worktrees(
    workspace_env, monkeypatch,
):
    data_dir, repo_dir, ws = workspace_env
    await ws.create("attempt_fresh")
    # Set max_age_days=0 → everything is "old"
    removed = await ws.cleanup_expired(max_age_days=0)
    assert removed >= 1


# ============================================================
# AC10-14: validate_workspace_path
# ============================================================


class TestValidateWorkspacePath:
    def test_ac10_valid_path_accepted(self, tmp_path):
        data_dir = tmp_path / "data"
        ws_root = (
            data_dir / "inst_a" / "improvement_workspace" / "att_1"
        )
        ws_root.mkdir(parents=True)
        ok, reason = validate_workspace_path(
            claimed_path=str(ws_root),
            instance_id="inst_a",
            data_dir=str(data_dir),
        )
        assert ok is True
        assert reason == ""

    def test_ac11_path_traversal_rejected(self, tmp_path):
        ok, reason = validate_workspace_path(
            claimed_path=str(tmp_path / "improvement_workspace" / "../escape"),
            instance_id="inst_a",
            data_dir=str(tmp_path),
        )
        assert ok is False
        assert ".." in reason or "outside" in reason or "exist" in reason

    def test_ac12_symlink_escape_rejected(self, tmp_path):
        """Symlink that resolves outside the improvement_workspace
        root should be rejected."""
        data_dir = tmp_path / "data"
        ws_root = data_dir / "inst_a" / "improvement_workspace" / "att_1"
        ws_root.mkdir(parents=True)
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        # symlink inside the workspace pointing outside
        link = ws_root / "escape_link"
        try:
            link.symlink_to(outside_dir)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        ok, reason = validate_workspace_path(
            claimed_path=str(link),
            instance_id="inst_a",
            data_dir=str(data_dir),
        )
        # Should be rejected — link.resolve() lands outside the root
        assert ok is False

    def test_ac13_typoed_instance_id_rejected(self, tmp_path):
        data_dir = tmp_path / "data"
        ws_root = data_dir / "inst_a" / "improvement_workspace" / "att_1"
        ws_root.mkdir(parents=True)
        ok, reason = validate_workspace_path(
            claimed_path=str(ws_root),
            instance_id="inst_b",  # different instance
            data_dir=str(data_dir),
        )
        assert ok is False
        assert "outside" in reason

    def test_ac14_nonexistent_path_rejected(self, tmp_path):
        data_dir = tmp_path / "data"
        ok, reason = validate_workspace_path(
            claimed_path=str(
                data_dir / "inst_a" / "improvement_workspace" / "phantom"
            ),
            instance_id="inst_a",
            data_dir=str(data_dir),
        )
        assert ok is False
        assert "exist" in reason

    def test_root_itself_rejected(self, tmp_path):
        """Caller must point at an attempt_id directory, not the
        workspaces root itself."""
        data_dir = tmp_path / "data"
        ws_root = data_dir / "inst_a" / "improvement_workspace"
        ws_root.mkdir(parents=True)
        ok, reason = validate_workspace_path(
            claimed_path=str(ws_root),
            instance_id="inst_a",
            data_dir=str(data_dir),
        )
        assert ok is False

    def test_empty_path_rejected(self):
        ok, reason = validate_workspace_path(
            claimed_path="", instance_id="x", data_dir="/tmp",
        )
        assert ok is False

    def test_empty_instance_rejected(self):
        ok, reason = validate_workspace_path(
            claimed_path="/tmp/x", instance_id="", data_dir="/tmp",
        )
        assert ok is False


# ============================================================
# AC15: trust-boundary docstring presence
# ============================================================


def test_ac15_trust_boundary_in_module_docstring():
    """Trust-boundary statement should be present in the module
    docstring (drift defense)."""
    import kernos.kernel.improvement_workspace as mod
    doc = (mod.__doc__ or "")
    # Normalize whitespace to allow the canonical phrase to span
    # a line break.
    normalized = " ".join(doc.split())
    assert "NOT a security sandbox" in normalized
    assert "trusted" in normalized.lower()


def test_ac15_trust_boundary_in_class_docstring():
    """Class docstring should reference the boundary too."""
    doc = ImprovementWorkspace.__doc__ or ""
    assert "Trust boundary" in doc or "trust boundary" in doc
    assert "security boundary" in doc.lower() or "NOT a security" in doc
