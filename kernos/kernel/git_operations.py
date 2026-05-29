"""Git kernel tools for the autonomous-improvement loop.

GIT-OPERATIONS-PRIMITIVES-V1 (2026-05-22).

Six agent-callable git tools that operate on an already-created
improvement worktree. Worktree create/remove is owned by
``IMPROVEMENT-WORKSPACE-V1`` — these tools consume the
``validate_workspace_path`` guard from that module.

Read tools (gate classification: read):
  - git_fetch          — update remote-tracking refs
  - git_rev_parse      — resolve a ref to its SHA
  - git_status         — verify clean state
  - git_diff_for_review — get diff text for human/agent review

Mutation tools (gate classification: hard_write; receipt-bound):
  - git_commit — verifies receipt's expected_parent_sha +
    expected_diff_hash; stages only listed files; writes
    commit_sha back to receipt outcome.
  - git_push   — verifies origin/main hasn't drifted + HEAD
    matches receipt's commit_sha; refuses --force always.

Per [[agent-facing-natural-simplicity]]: every tool's response
to the agent is natural prose. Substrate keeps structured
data (receipt mutations, audit entries, git output) for
operator inspection but the agent reads sentences.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Output cap for git_diff_for_review — full diffs can be huge;
# the agent doesn't need the entire thing for review-level
# judgment. Operator inspects the full diff via the worktree
# directly.
_DIFF_OUTPUT_CAP_BYTES = 64 * 1024


# ---------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------


GIT_FETCH_TOOL: dict = {
    "name": "git_fetch",
    "description": (
        "Update the improvement worktree's remote-tracking refs "
        "from `origin` (or a named remote). No mutations to "
        "local branches. Use at the start of an attempt or "
        "before a push to check for drift on origin/main."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "workspace_dir": {
                "type": "string",
                "description": (
                    "Absolute path to the improvement worktree "
                    "(must be under data/<instance>/improvement_workspace/)."
                ),
            },
            "remote": {
                "type": "string",
                "description": "Remote name. Defaults to 'origin'.",
            },
        },
        "required": ["workspace_dir"],
    },
}


GIT_REV_PARSE_TOOL: dict = {
    "name": "git_rev_parse",
    "description": (
        "Resolve a git ref (branch, tag, or SHA prefix) to its "
        "full SHA in the improvement worktree. Used to capture "
        "the base SHA at attempt start, or verify the current "
        "parent before committing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "workspace_dir": {"type": "string"},
            "ref": {
                "type": "string",
                "description": (
                    "Ref to resolve. Examples: 'HEAD', 'origin/main', "
                    "'improvement/abc'."
                ),
            },
        },
        "required": ["workspace_dir", "ref"],
    },
}


GIT_STATUS_TOOL: dict = {
    "name": "git_status",
    "description": (
        "Check the working state of the improvement worktree. "
        "Returns a natural-prose summary: clean working tree, "
        "or modified/untracked file counts. Use at attempt "
        "start to verify the worktree is clean before "
        "starting changes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "workspace_dir": {"type": "string"},
        },
        "required": ["workspace_dir"],
    },
}


GIT_DIFF_FOR_REVIEW_TOOL: dict = {
    "name": "git_diff_for_review",
    "description": (
        "Get the diff text from `base` to `head` for human or "
        "agent review. Output is truncated at 64KB with a "
        "continuation note when needed — full diffs are "
        "available via the worktree directly. Defaults: "
        "base='origin/main', head='HEAD'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "workspace_dir": {"type": "string"},
            "base": {
                "type": "string",
                "description": "Base ref. Defaults to 'origin/main'.",
            },
            "head": {
                "type": "string",
                "description": "Head ref. Defaults to 'HEAD'.",
            },
        },
        "required": ["workspace_dir"],
    },
}


GIT_COMMIT_TOOL: dict = {
    "name": "git_commit",
    "description": (
        "Commit the staged files in the improvement worktree. "
        "Requires `approval_id` — an operator-approved receipt "
        "whose `expected_parent_sha` and `expected_diff_hash` "
        "must match the current worktree state. Stages ONLY "
        "the listed files (never `git add -A`); files outside "
        "the worktree are refused. On success, writes the new "
        "commit SHA back to the receipt outcome so `git_push` "
        "can verify the worktree HEAD matches what the operator "
        "approved."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "workspace_dir": {"type": "string"},
            "message": {
                "type": "string",
                "description": "Commit message.",
            },
            "approval_id": {
                "type": "string",
                "description": (
                    "Operator-approved receipt id. Must be "
                    "kind=git_commit_authorization and in "
                    "state=approved."
                ),
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of paths (relative to worktree) to "
                    "stage and commit."
                ),
            },
        },
        "required": ["workspace_dir", "message", "approval_id", "files"],
    },
}


GIT_PUSH_TOOL: dict = {
    "name": "git_push",
    "description": (
        "Push the operator-approved commit to origin. Requires "
        "the same `approval_id` used for `git_commit` (its "
        "outcome carries the `commit_sha` to push). Verifies "
        "origin/main hasn't drifted since approval and that "
        "the worktree HEAD matches the receipt's commit_sha. "
        "Refuses --force always."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "workspace_dir": {"type": "string"},
            "target_branch": {
                "type": "string",
                "description": "Branch on origin to push to. Defaults to 'main'.",
            },
            "approval_id": {
                "type": "string",
                "description": (
                    "Same approval_id used for git_commit; its "
                    "outcome carries the commit_sha to push."
                ),
            },
        },
        "required": ["workspace_dir", "approval_id"],
    },
}


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------


async def _run_git(
    args: list[str], *, cwd: str,
) -> tuple[int, str, str]:
    """Run git with args in cwd. Returns (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
    return (
        proc.returncode or 0,
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )


def _validate_workspace_or_prose_error(
    *, workspace_dir: str, instance_id: str, data_dir: str,
) -> str:
    """Run the guard. Returns "" on success, agent-prose error
    on rejection (caller returns this as the tool's response)."""
    from kernos.kernel.improvement_workspace import (
        validate_workspace_path,
    )
    ok, reason = validate_workspace_path(
        claimed_path=workspace_dir,
        instance_id=instance_id,
        data_dir=data_dir,
    )
    return "" if ok else reason


def _compute_staged_diff_hash(workspace_dir: str) -> str:
    """Synchronous helper — used by tests + the orchestrator
    when capturing the expected_diff_hash before issuing the
    receipt. Returns 'sha256:<hex>'. Reads via subprocess to
    match what git_commit sees at verification time."""
    import subprocess
    result = subprocess.run(
        ["git", "diff", "--cached"],
        cwd=workspace_dir,
        capture_output=True,
        check=False,
    )
    digest = hashlib.sha256(result.stdout).hexdigest()
    return f"sha256:{digest}"


async def _compute_staged_diff_hash_async(workspace_dir: str) -> str:
    rc, out, _ = await _run_git(["diff", "--cached"], cwd=workspace_dir)
    digest = hashlib.sha256(out.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------


async def handle_git_fetch(
    *, tool_input: dict, instance_id: str, data_dir: str,
) -> str:
    workspace_dir = tool_input.get("workspace_dir", "")
    remote = tool_input.get("remote", "") or "origin"
    err = _validate_workspace_or_prose_error(
        workspace_dir=workspace_dir,
        instance_id=instance_id, data_dir=data_dir,
    )
    if err:
        return err
    rc, out, stderr = await _run_git(["fetch", remote], cwd=workspace_dir)
    if rc != 0:
        return (
            f"`git fetch {remote}` failed in the worktree: "
            f"{stderr.strip() or out.strip() or 'unknown error'}."
        )
    # git fetch is usually silent on stdout when no new commits
    note = "No new commits to fetch."
    if "->" in stderr or "[new branch]" in stderr:
        note = stderr.strip().splitlines()[-1] if stderr.strip() else note
    return f"Fetched `{remote}`. {note}"


async def handle_git_rev_parse(
    *, tool_input: dict, instance_id: str, data_dir: str,
) -> str:
    workspace_dir = tool_input.get("workspace_dir", "")
    ref = tool_input.get("ref", "")
    if not ref:
        return "`ref` is required for git_rev_parse."
    err = _validate_workspace_or_prose_error(
        workspace_dir=workspace_dir,
        instance_id=instance_id, data_dir=data_dir,
    )
    if err:
        return err
    rc, out, stderr = await _run_git(
        ["rev-parse", "--verify", ref],
        cwd=workspace_dir,
    )
    if rc != 0:
        return (
            f"Ref `{ref}` couldn't be resolved in the worktree. "
            f"({stderr.strip() or 'not found'})"
        )
    return out.strip()


async def handle_git_status(
    *, tool_input: dict, instance_id: str, data_dir: str,
) -> str:
    workspace_dir = tool_input.get("workspace_dir", "")
    err = _validate_workspace_or_prose_error(
        workspace_dir=workspace_dir,
        instance_id=instance_id, data_dir=data_dir,
    )
    if err:
        return err
    rc, out, stderr = await _run_git(
        ["status", "--porcelain"],
        cwd=workspace_dir,
    )
    if rc != 0:
        return (
            f"`git status` failed: {stderr.strip() or 'unknown error'}."
        )
    lines = [line for line in out.splitlines() if line.strip()]
    if not lines:
        return "Working tree is clean. No changes."
    modified = sum(1 for line in lines if line[:2].strip() in ("M", "A", "D", "R", "C", "AM"))
    untracked = sum(1 for line in lines if line.startswith("??"))
    return (
        f"{len(lines)} files changed "
        f"({modified} modified/added, {untracked} untracked). "
        f"Run `git_diff_for_review` for the full diff."
    )


async def handle_git_diff_for_review(
    *, tool_input: dict, instance_id: str, data_dir: str,
) -> str:
    workspace_dir = tool_input.get("workspace_dir", "")
    base = tool_input.get("base", "") or "origin/main"
    head = tool_input.get("head", "") or "HEAD"
    err = _validate_workspace_or_prose_error(
        workspace_dir=workspace_dir,
        instance_id=instance_id, data_dir=data_dir,
    )
    if err:
        return err
    rc, out, stderr = await _run_git(
        ["diff", f"{base}..{head}"],
        cwd=workspace_dir,
    )
    if rc != 0:
        return (
            f"`git diff {base}..{head}` failed: "
            f"{stderr.strip() or 'unknown error'}."
        )
    if not out.strip():
        return f"No diff between `{base}` and `{head}`."
    out_bytes = out.encode("utf-8")
    if len(out_bytes) > _DIFF_OUTPUT_CAP_BYTES:
        truncated = out_bytes[:_DIFF_OUTPUT_CAP_BYTES].decode(
            "utf-8", errors="replace",
        )
        return (
            f"{truncated}\n\n"
            f"... (diff continues — output capped at "
            f"{_DIFF_OUTPUT_CAP_BYTES // 1024}KB; total "
            f"{len(out_bytes) // 1024}KB. Inspect the worktree "
            f"directly for the full diff.)"
        )
    return out


async def handle_git_commit(
    *, tool_input: dict, instance_id: str, data_dir: str,
) -> str:
    from kernos.kernel import approval_receipts as _approvals
    from kernos.kernel.self_test_gate import (
        SubstrateUnhealthyError,
        check_substrate_healthy_or_raise,
    )

    # SUBSTRATE-SELF-TEST-V1 AC9: autonomous-mutation gate.
    # When the most recent substrate-soak failed, refuse
    # autonomous git_commit calls. Operator-initiated paths
    # (manual git CLI) bypass this entirely — the gate is on
    # the kernel-tool dispatch surface only.
    try:
        check_substrate_healthy_or_raise(autonomous_path="git_commit")
    except SubstrateUnhealthyError as exc:
        return str(exc)

    workspace_dir = tool_input.get("workspace_dir", "")
    message = tool_input.get("message", "")
    approval_id = tool_input.get("approval_id", "")
    files = tool_input.get("files", []) or []

    if not approval_id:
        return (
            "`git_commit` requires `approval_id` — the operator-"
            "approved receipt id. Obtain one via the orchestrator "
            "workflow's approval step."
        )
    if not message:
        return "Commit message is required."
    if not files or not isinstance(files, list):
        return (
            "`files` must be a non-empty list of paths to stage. "
            "Wildcard staging (`git add -A`) is not supported by "
            "this primitive."
        )

    err = _validate_workspace_or_prose_error(
        workspace_dir=workspace_dir,
        instance_id=instance_id, data_dir=data_dir,
    )
    if err:
        return err

    # Read receipt.
    receipt = await _approvals.get_receipt(
        data_dir=data_dir, approval_id=approval_id,
    )
    if receipt is None:
        return f"Approval `{approval_id}` not found."
    if receipt.get("kind") != "git_commit_authorization":
        return (
            f"Approval `{approval_id}` is for `"
            f"{receipt.get('kind') or 'unknown'}`, not a commit "
            f"authorization. Wrong receipt for this action."
        )
    if receipt.get("state") != "approved":
        return (
            f"Approval `{approval_id}` is `{receipt.get('state')}`, "
            f"not approved. Operator hasn't confirmed yet (or it's "
            f"already consumed)."
        )

    binding = {}
    try:
        binding = json.loads(receipt.get("binding_payload_json") or "{}")
    except json.JSONDecodeError:
        return (
            "Approval receipt's binding payload is corrupt. "
            "Re-issue the approval."
        )

    expected_parent = binding.get("expected_parent_sha", "")
    expected_diff_hash = binding.get("expected_diff_hash", "")

    # Validate file paths — must be within the worktree.
    worktree_root = Path(workspace_dir).resolve()
    for f in files:
        if ".." in f.split(os.sep) or os.path.isabs(f):
            return (
                f"File `{f}` is outside the worktree or contains "
                f"`..` segments. Only paths relative to the "
                f"workspace are allowed."
            )
        target = (worktree_root / f).resolve()
        try:
            target.relative_to(worktree_root)
        except ValueError:
            return (
                f"File `{f}` resolves outside the worktree. "
                f"Refusing to stage."
            )

    # Verify current parent matches expected.
    rc, head_sha, _ = await _run_git(
        ["rev-parse", "HEAD"], cwd=workspace_dir,
    )
    if rc != 0:
        return f"Couldn't read HEAD in the worktree."
    head_sha = head_sha.strip()
    if expected_parent and head_sha != expected_parent:
        return (
            f"The worktree's HEAD (`{head_sha[:12]}`) has drifted "
            f"since the operator approved (expected "
            f"`{expected_parent[:12]}`). The approval is stale; "
            f"re-issue for the current state."
        )

    # Stage only the listed files.
    rc, out, stderr = await _run_git(
        ["add", "--"] + list(files), cwd=workspace_dir,
    )
    if rc != 0:
        return (
            f"`git add` failed: {stderr.strip() or 'unknown error'}."
        )

    # Verify staged-diff hash matches expected.
    actual_diff_hash = await _compute_staged_diff_hash_async(workspace_dir)
    if expected_diff_hash and actual_diff_hash != expected_diff_hash:
        # Unstage to leave the worktree in a recoverable state.
        await _run_git(
            ["reset", "HEAD", "--"] + list(files),
            cwd=workspace_dir,
        )
        return (
            f"The staged diff has changed since the operator "
            f"approved (expected `{expected_diff_hash[:20]}`, "
            f"got `{actual_diff_hash[:20]}`). Approval is stale "
            f"for the current contents; re-issue."
        )

    # Run the commit.
    rc, out, stderr = await _run_git(
        ["commit", "-m", message], cwd=workspace_dir,
    )
    if rc != 0:
        return (
            f"`git commit` failed: {stderr.strip() or out.strip() or 'unknown error'}."
        )

    # Read the new commit SHA.
    rc, new_sha, _ = await _run_git(
        ["rev-parse", "HEAD"], cwd=workspace_dir,
    )
    if rc != 0:
        return (
            "Commit succeeded but couldn't read the new HEAD. "
            "Investigate the worktree."
        )
    new_sha = new_sha.strip()

    # Write commit_sha back to the receipt outcome (atomic).
    committed_at = datetime.now(timezone.utc).isoformat()
    await _approvals.set_outcome_field(
        data_dir=data_dir, approval_id=approval_id,
        field="commit_sha", value=new_sha,
    )
    await _approvals.set_outcome_field(
        data_dir=data_dir, approval_id=approval_id,
        field="committed_at", value=committed_at,
    )

    first_line = (message.splitlines() or [message])[0][:80]
    return (
        f"Committed `{new_sha[:12]}` in the worktree. "
        f"Message: '{first_line}'. Ready for `git_push` with the "
        f"same approval id."
    )


async def handle_git_push(
    *, tool_input: dict, instance_id: str, data_dir: str,
) -> str | dict[str, Any]:
    from kernos.kernel import approval_receipts as _approvals
    from kernos.kernel.self_test_gate import (
        SubstrateUnhealthyError,
        check_substrate_healthy_or_raise,
    )

    workspace_dir = tool_input.get("workspace_dir", "")
    target_branch = tool_input.get("target_branch", "") or "main"
    approval_id = tool_input.get("approval_id", "")
    return_structured = bool(tool_input.get("return_structured", False))

    def _result(
        *,
        ok: bool,
        message: str,
        reason: str = "",
        commit_sha: str = "",
        origin_sha: str = "",
        origin_confirmed: bool | None = None,
    ) -> str | dict[str, Any]:
        if not return_structured:
            return message
        payload: dict[str, Any] = {
            "ok": ok,
            "message": message,
            "target_branch": target_branch,
        }
        if reason:
            payload["reason"] = reason
        if commit_sha:
            payload["commit_sha"] = commit_sha
        if origin_sha:
            payload["origin_sha"] = origin_sha
        if origin_confirmed is not None:
            payload["origin_confirmed"] = origin_confirmed
        return payload

    # SUBSTRATE-SELF-TEST-V1 AC9: autonomous-mutation gate.
    try:
        check_substrate_healthy_or_raise(autonomous_path="git_push")
    except SubstrateUnhealthyError as exc:
        return _result(
            ok=False,
            reason="substrate_unhealthy",
            message=str(exc),
        )

    if not approval_id:
        return _result(
            ok=False,
            reason="missing_approval_id",
            message=(
                "`git_push` requires the same `approval_id` used for "
                "`git_commit`. Its outcome carries the commit_sha to push."
            ),
        )

    err = _validate_workspace_or_prose_error(
        workspace_dir=workspace_dir,
        instance_id=instance_id, data_dir=data_dir,
    )
    if err:
        return _result(ok=False, reason="invalid_workspace", message=err)

    receipt = await _approvals.get_receipt(
        data_dir=data_dir, approval_id=approval_id,
    )
    if receipt is None:
        return _result(
            ok=False,
            reason="approval_not_found",
            message=f"Approval `{approval_id}` not found.",
        )
    if receipt.get("kind") != "git_commit_authorization":
        return _result(
            ok=False,
            reason="wrong_receipt_kind",
            message=(
                f"Approval `{approval_id}` isn't a commit authorization. "
                f"Wrong receipt."
            ),
        )
    if receipt.get("state") != "approved":
        return _result(
            ok=False,
            reason="approval_not_approved",
            message=(
                f"Approval `{approval_id}` is `{receipt.get('state')}`, "
                f"not approved. Can't push without an approved commit."
            ),
        )

    binding = json.loads(receipt.get("binding_payload_json") or "{}")
    outcome = json.loads(receipt.get("outcome_payload_json") or "{}")
    expected_parent = binding.get("expected_parent_sha", "")
    receipt_commit_sha = outcome.get("commit_sha", "")

    if not receipt_commit_sha:
        return _result(
            ok=False,
            reason="missing_commit_sha",
            message=(
                "Receipt has no `commit_sha` outcome — `git_commit` "
                "wasn't run yet (or it failed). Run commit first."
            ),
        )

    # Verify worktree HEAD matches the receipt's commit_sha.
    rc, head_sha, _ = await _run_git(
        ["rev-parse", "HEAD"], cwd=workspace_dir,
    )
    head_sha = head_sha.strip()
    if rc != 0 or head_sha != receipt_commit_sha:
        return _result(
            ok=False,
            reason="head_commit_mismatch",
            commit_sha=receipt_commit_sha,
            message=(
                f"Worktree HEAD (`{head_sha[:12]}`) doesn't match the "
                f"receipt's commit (`{receipt_commit_sha[:12]}`). "
                f"Did someone else commit?"
            ),
        )

    # Verify origin hasn't drifted since approval. A stale local
    # remote-tracking ref is not proof of remote state, so the fetch
    # must succeed before any origin/<branch> equality can confirm.
    fetch_rc, fetch_out, fetch_stderr = await _run_git(
        ["fetch", "origin"], cwd=workspace_dir,
    )
    if fetch_rc != 0:
        return _result(
            ok=False,
            reason="origin_fetch_failed",
            commit_sha=receipt_commit_sha,
            origin_confirmed=False,
            message=(
                f"Couldn't confirm `origin/{target_branch}` because "
                f"`git fetch origin` failed: "
                f"{fetch_stderr.strip() or fetch_out.strip() or 'unknown error'}."
            ),
        )
    rc, origin_sha, _ = await _run_git(
        ["rev-parse", "--verify", f"origin/{target_branch}"],
        cwd=workspace_dir,
    )
    origin_sha = origin_sha.strip()
    if rc != 0:
        return _result(
            ok=False,
            reason="origin_unresolved",
            commit_sha=receipt_commit_sha,
            message=(
                f"Couldn't resolve `origin/{target_branch}`. Operator "
                f"needs to verify remote state."
            ),
        )
    if origin_sha == receipt_commit_sha:
        return _result(
            ok=True,
            reason="already_pushed",
            commit_sha=receipt_commit_sha,
            origin_sha=origin_sha,
            origin_confirmed=True,
            message=(
                f"`origin/{target_branch}` already points at "
                f"`{receipt_commit_sha[:12]}`. Treating the approved "
                "push as complete."
            ),
        )
    if expected_parent and origin_sha != expected_parent:
        return _result(
            ok=False,
            reason="origin_drifted",
            commit_sha=receipt_commit_sha,
            message=(
                f"`origin/{target_branch}` has drifted since the "
                f"operator approved (expected `{expected_parent[:12]}`, "
                f"got `{origin_sha[:12]}`). Operator decides whether to "
                f"abort or rebase + re-approve."
            ),
        )

    # Run the push — no --force, ever.
    rc, out, stderr = await _run_git(
        ["push", "origin", f"HEAD:{target_branch}"],
        cwd=workspace_dir,
    )
    if rc != 0:
        return _result(
            ok=False,
            reason="git_push_failed",
            commit_sha=receipt_commit_sha,
            message=(
                f"`git push` failed: "
                f"{stderr.strip() or out.strip() or 'unknown error'}."
            ),
        )

    fetch_rc, fetch_out, fetch_err = await _run_git(
        ["fetch", "origin"], cwd=workspace_dir,
    )
    if fetch_rc != 0:
        return _result(
            ok=False,
            reason="post_push_fetch_failed",
            commit_sha=receipt_commit_sha,
            origin_confirmed=False,
            message=(
                "`git push` exited 0, but post-push confirmation failed "
                f"during fetch: "
                f"{fetch_err.strip() or fetch_out.strip() or 'unknown error'}."
            ),
        )
    rc, confirmed_origin_sha, _ = await _run_git(
        ["rev-parse", "--verify", f"origin/{target_branch}"],
        cwd=workspace_dir,
    )
    confirmed_origin_sha = confirmed_origin_sha.strip()
    if rc != 0 or confirmed_origin_sha != receipt_commit_sha:
        return _result(
            ok=False,
            reason="post_push_unconfirmed",
            commit_sha=receipt_commit_sha,
            origin_sha=confirmed_origin_sha,
            origin_confirmed=False,
            message=(
                "`git push` exited 0, but `origin/"
                f"{target_branch}` did not confirm commit "
                f"`{receipt_commit_sha[:12]}` "
                f"(got `{confirmed_origin_sha[:12] or 'unresolved'}`)."
            ),
        )

    return _result(
        ok=True,
        reason="pushed",
        commit_sha=receipt_commit_sha,
        origin_sha=receipt_commit_sha,
        origin_confirmed=True,
        message=(
            f"Pushed `{receipt_commit_sha[:12]}` to "
            f"`origin/{target_branch}`. Cycle complete."
        ),
    )
