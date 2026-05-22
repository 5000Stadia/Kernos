"""Improvement workspace — git worktree lifecycle for the autonomous loop.

IMPROVEMENT-WORKSPACE-V1 (2026-05-22).

**Trust boundary (load-bearing):** the worktree is NOT a
security sandbox. The coding agent invoked via `consult`
inherits credentials + filesystem write outside the worktree +
ability to push to origin. v1 ships against TRUSTED CODING
AGENTS only (`claude_code`, `codex` — operator-vetted
commercial CLIs). v2 closes the trust gap by replacing the
worktree with a container-backed primitive without changing
the orchestration workflow.

This module is substrate-owned: the agent does NOT call
worktree create/remove directly. The orchestrator workflow
(`IMPROVEMENT-LOOP-WORKFLOW-V1`, future) calls them. Agent's
experience is via git-ops kernel tools that consume the
workspace guard.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------


class WorkspaceCreateError(Exception):
    """Worktree creation failed (git error, disk full, branch
    collision, etc.). Operator-facing message + structured
    context."""


class WorkspaceRemoveError(Exception):
    """Worktree removal refused (un-pushed commits + force=False)
    or failed (git error). Operator-facing message."""


# ---------------------------------------------------------------------
# ImprovementWorkspace — worktree lifecycle
# ---------------------------------------------------------------------


_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


def _validate_attempt_id(attempt_id: str) -> None:
    """Path-component safety check on attempt_id. Defends against
    traversal, special characters, and unbounded names."""
    if not attempt_id or not _ATTEMPT_ID_RE.match(attempt_id):
        raise WorkspaceCreateError(
            f"attempt_id {attempt_id!r} is invalid; must be 1-64 "
            f"alphanumeric / underscore / dash characters and not "
            f"start with a separator."
        )


class ImprovementWorkspace:
    """Owns the lifecycle of per-attempt git worktrees for the
    autonomous-improvement loop.

    Trust boundary: see module docstring. The worktree is an
    accidental-edit guard, not a security boundary.
    """

    def __init__(
        self, data_dir: str, instance_id: str, live_repo_dir: str,
    ) -> None:
        self._data_dir = Path(data_dir).resolve()
        self._instance_id = instance_id
        self._live_repo_dir = Path(live_repo_dir).resolve()

    # --- Path helpers ---

    def _safe_instance_segment(self) -> str:
        """Sanitize instance_id for use as a filesystem path
        component."""
        return re.sub(r"[^A-Za-z0-9_\-]", "_", self._instance_id)

    def _workspaces_root(self) -> Path:
        return self._data_dir / self._safe_instance_segment() / "improvement_workspace"

    def path_for(self, attempt_id: str) -> str:
        """Absolute path where this attempt's worktree lives.
        Pure path computation; doesn't create the directory."""
        _validate_attempt_id(attempt_id)
        return str(self._workspaces_root() / attempt_id)

    def branch_for(self, attempt_id: str) -> str:
        """Branch name for this attempt's worktree."""
        _validate_attempt_id(attempt_id)
        return f"improvement/{attempt_id}"

    # --- Lifecycle ---

    async def create(self, attempt_id: str) -> str:
        """Create a worktree at the documented path on branch
        ``improvement/<attempt_id>`` from ``origin/main``. Returns
        the absolute worktree path.

        Raises ``WorkspaceCreateError`` on git failure.
        """
        _validate_attempt_id(attempt_id)
        worktree_path = self.path_for(attempt_id)
        branch_name = self.branch_for(attempt_id)
        # Ensure the workspaces root exists.
        self._workspaces_root().mkdir(parents=True, exist_ok=True)
        if Path(worktree_path).exists():
            raise WorkspaceCreateError(
                f"worktree path already exists: {worktree_path}"
            )
        # Fetch first so origin/main is current.
        rc, out, err = await self._run_git(
            ["fetch", "origin"], cwd=self._live_repo_dir,
        )
        if rc != 0:
            raise WorkspaceCreateError(
                f"git fetch failed (rc={rc}): {err.strip() or out.strip()}"
            )
        # Create the worktree.
        rc, out, err = await self._run_git(
            [
                "worktree", "add", "-b", branch_name,
                worktree_path, "origin/main",
            ],
            cwd=self._live_repo_dir,
        )
        if rc != 0:
            raise WorkspaceCreateError(
                f"git worktree add failed (rc={rc}): "
                f"{err.strip() or out.strip()}"
            )
        logger.info(
            "IMPROVEMENT_WORKSPACE_CREATED attempt=%s path=%s branch=%s",
            attempt_id, worktree_path, branch_name,
        )
        return worktree_path

    async def remove(
        self, attempt_id: str, *, force: bool = False,
    ) -> None:
        """Remove the worktree + delete its branch.

        When ``force=False`` and the branch has un-pushed commits,
        raises ``WorkspaceRemoveError`` for operator review.
        ``force=True`` removes unconditionally.
        """
        _validate_attempt_id(attempt_id)
        worktree_path = self.path_for(attempt_id)
        branch_name = self.branch_for(attempt_id)

        if not force:
            # Check for un-pushed commits on the branch.
            rc, out, _ = await self._run_git(
                [
                    "log", f"origin/main..{branch_name}", "--oneline",
                ],
                cwd=self._live_repo_dir,
            )
            if rc == 0 and out.strip():
                raise WorkspaceRemoveError(
                    f"branch {branch_name!r} has un-pushed commits; "
                    f"call with force=True to remove anyway. "
                    f"Commits:\n{out.strip()}"
                )

        # Remove the worktree.
        cmd = ["worktree", "remove"]
        if force:
            cmd.append("--force")
        cmd.append(worktree_path)
        rc, out, err = await self._run_git(
            cmd, cwd=self._live_repo_dir,
        )
        if rc != 0:
            # Already-gone worktree is OK on force; otherwise raise.
            if not force or "not a working tree" not in (err + out).lower():
                raise WorkspaceRemoveError(
                    f"git worktree remove failed (rc={rc}): "
                    f"{err.strip() or out.strip()}"
                )

        # Delete the branch best-effort. -D forces delete.
        rc, out, err = await self._run_git(
            ["branch", "-D", branch_name],
            cwd=self._live_repo_dir,
        )
        if rc != 0:
            logger.warning(
                "IMPROVEMENT_WORKSPACE_BRANCH_DELETE_FAILED "
                "branch=%s rc=%d err=%s",
                branch_name, rc, err.strip() or out.strip(),
            )
        logger.info(
            "IMPROVEMENT_WORKSPACE_REMOVED attempt=%s force=%s",
            attempt_id, force,
        )

    async def list_active(self) -> list[dict]:
        """Return a list of active worktrees as dicts:
        ``{attempt_id, path, branch, created_at, age_days}``.
        Reads from ``git worktree list``."""
        rc, out, err = await self._run_git(
            ["worktree", "list", "--porcelain"],
            cwd=self._live_repo_dir,
        )
        if rc != 0:
            logger.warning(
                "IMPROVEMENT_WORKSPACE_LIST_FAILED rc=%d err=%s",
                rc, err.strip(),
            )
            return []
        # Parse porcelain output. Each worktree entry is a block
        # of `key value` lines separated by blank lines.
        entries: list[dict] = []
        block: dict[str, str] = {}
        for line in out.splitlines() + [""]:
            if not line.strip():
                if block:
                    entries.append(block)
                    block = {}
                continue
            if " " in line:
                key, _, value = line.partition(" ")
                block[key] = value
            else:
                block[line] = ""
        # Filter to those under our improvement_workspace root.
        workspaces_root = self._workspaces_root().resolve()
        results: list[dict] = []
        for e in entries:
            path_str = e.get("worktree", "")
            if not path_str:
                continue
            wt_path = Path(path_str).resolve()
            try:
                wt_path.relative_to(workspaces_root)
            except ValueError:
                continue  # outside our managed area
            attempt_id = wt_path.name
            branch = e.get("branch", "").replace("refs/heads/", "")
            created_at = ""
            age_days = 0.0
            try:
                st = wt_path.stat()
                created_at = datetime.fromtimestamp(
                    st.st_mtime, timezone.utc,
                ).isoformat()
                age_days = (
                    datetime.now(timezone.utc).timestamp() - st.st_mtime
                ) / 86400.0
            except OSError:
                pass
            results.append({
                "attempt_id": attempt_id,
                "path": str(wt_path),
                "branch": branch,
                "created_at": created_at,
                "age_days": round(age_days, 2),
            })
        return results

    async def cleanup_expired(
        self, *, max_age_days: int = 7,
    ) -> int:
        """Remove worktrees older than ``max_age_days``. Returns
        count removed. Force-removes (un-pushed branches at this
        age are presumed stale)."""
        active = await self.list_active()
        removed = 0
        for entry in active:
            if entry["age_days"] >= max_age_days:
                attempt_id = entry["attempt_id"]
                try:
                    await self.remove(attempt_id, force=True)
                    removed += 1
                except Exception as exc:
                    logger.warning(
                        "IMPROVEMENT_WORKSPACE_CLEANUP_FAILED "
                        "attempt=%s exc=%s", attempt_id, exc,
                    )
        if removed:
            logger.info(
                "IMPROVEMENT_WORKSPACE_CLEANUP removed=%d "
                "max_age_days=%d", removed, max_age_days,
            )
        return removed

    # --- Internal ---

    async def _run_git(
        self, args: list[str], *, cwd: Path,
    ) -> tuple[int, str, str]:
        """Run git in ``cwd`` with the given args. Returns
        (returncode, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await proc.communicate()
        return (
            proc.returncode or 0,
            out_b.decode("utf-8", errors="replace"),
            err_b.decode("utf-8", errors="replace"),
        )


# ---------------------------------------------------------------------
# Workspace guard — consumed by git-ops kernel tools (next sub-spec)
# ---------------------------------------------------------------------


def validate_workspace_path(
    *, claimed_path: str, instance_id: str, data_dir: str,
) -> tuple[bool, str]:
    """Validate that ``claimed_path`` is under the instance's
    improvement_workspace root.

    Returns ``(True, "")`` on valid, ``(False, reason)`` otherwise.
    The reason is operator-facing in logs AND doubles as the
    agent-facing prose surfaced via git-ops tools' error responses.

    Defends against:
      - path traversal (".." segments)
      - symlink escape (resolves to absolute path, checks parent)
      - typoed instance_id (path must be under THIS instance)
      - claimed_path pointing to a non-existent directory
      - claimed_path pointing outside improvement_workspace/
    """
    if not claimed_path:
        return (False, "workspace_dir is required.")
    if not instance_id:
        return (False, "instance_id is required for workspace validation.")
    if ".." in claimed_path.split(os.sep):
        return (
            False,
            "workspace_dir must not contain '..' path segments.",
        )
    # Compute the legal root for this instance.
    safe_inst = re.sub(r"[^A-Za-z0-9_\-]", "_", instance_id)
    root = (
        Path(data_dir).resolve()
        / safe_inst
        / "improvement_workspace"
    )
    try:
        resolved = Path(claimed_path).resolve()
    except (OSError, RuntimeError) as exc:
        return (False, f"workspace_dir resolution failed: {exc}")
    if not resolved.exists():
        return (
            False,
            f"workspace_dir does not exist: {claimed_path}",
        )
    if not resolved.is_dir():
        return (
            False,
            f"workspace_dir is not a directory: {claimed_path}",
        )
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return (
            False,
            (
                f"workspace_dir is outside the improvement_workspace "
                f"root for this instance. Expected under {root}; "
                f"got {resolved}."
            ),
        )
    # The path must be under an attempt_id directory, not the root
    # itself. The first segment of the relative path IS the
    # attempt_id.
    parts = rel.parts
    if not parts:
        return (
            False,
            "workspace_dir must be under an attempt_id directory, "
            "not the workspaces root itself.",
        )
    return (True, "")
