"""Autonomous improvement loop orchestrator.

IMPROVEMENT-LOOP-WORKFLOW-V1 (2026-05-22).

Composes the 5 prior autonomous-loop sub-specs (workspace +
ledger + git-ops + review-protocol + self-test gate) +
receipts + consult + restart_self into the happy-path
autonomous-improvement attempt.

v1 ships the happy path. Recovery cycles after test failure,
mid-attempt restart-resume, auto-rebase on origin drift, and
per-step retry budgets are explicitly deferred to a follow-up
``IMPROVEMENT-LOOP-RECOVERY-V1`` spec — see the spec file at
``specs/IMPROVEMENT-LOOP-WORKFLOW-V1.md`` for the deferred
scope.

Trust boundary (load-bearing per parent spec D1): the worktree
is NOT a security sandbox. v1 ships against TRUSTED CODING
AGENTS only (`claude_code`, `codex`). The improve_kernos tool
enforces this at the input-schema enum level.

Per [[agent-facing-natural-simplicity]]: agent gets prose
status messages; operator inspects full state via
``/improvement_status``.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------


IMPROVE_KERNOS_TOOL: dict = {
    "name": "improve_kernos",
    "description": (
        "Start an autonomous improvement attempt against "
        "Kernos's own source. The substrate spawns trusted "
        "coding agents to draft + implement a spec, then asks "
        "the operator for approval before committing + "
        "restarting. Returns an attempt_id you can track via "
        "/improvement_status. The attempt continues "
        "asynchronously after this call returns."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "spec_requirement": {
                "type": "string",
                "description": (
                    "What you want Kernos to improve about "
                    "itself, in natural language. Will be "
                    "passed to the spec-author coding agent."
                ),
            },
            "primary_coding_agent": {
                "type": "string",
                "enum": ["claude_code", "codex"],
                "description": (
                    "Coding agent for spec authoring + "
                    "implementation. Defaults to claude_code."
                ),
            },
            "reviewer_coding_agent": {
                "type": "string",
                "enum": ["claude_code", "codex"],
                "description": (
                    "Coding agent for spec review + code "
                    "review. Defaults to codex. Independent-"
                    "perspective is the reason for the split; "
                    "matching primary works but loses that."
                ),
            },
        },
        "required": ["spec_requirement"],
    },
}


# ---------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------


class ImprovementLoopOrchestrator:
    """Composes the autonomous-improvement primitives into one
    end-to-end attempt. State persistence is via the ledger
    (no in-memory state survives restart).

    Stubs for restart_self + consult are accepted at __init__
    so tests can pin the orchestrator's behavior without firing
    a real process restart or external CLI subprocess.
    """

    def __init__(
        self,
        *,
        instance_id: str,
        data_dir: str,
        live_repo_dir: str,
        consult_fn: Any = None,           # async (target, prompt) -> str
        restart_fn: Any = None,           # async () -> None
        receipts_event_stream: Any = None,
    ) -> None:
        self._instance_id = instance_id
        self._data_dir = data_dir
        self._live_repo_dir = live_repo_dir
        self._consult_fn = consult_fn
        self._restart_fn = restart_fn
        self._receipts_event_stream = receipts_event_stream
        # Track running background tasks so tests + shutdown
        # can wait on them.
        self._running_tasks: set[asyncio.Task] = set()

    # --- Public entry points ---

    async def start_attempt(
        self,
        *,
        spec_requirement: str,
        primary_coding_agent: str = "claude_code",
        reviewer_coding_agent: str = "codex",
    ) -> str:
        """Synchronous-returning entry. Creates attempt_id,
        ledger row, worktree. Returns the attempt_id; spawns
        the rest of the loop as a background task.

        Validates that primary/reviewer are in the trusted-
        agent allowlist (claude_code | codex). Other values
        raise ValueError — improve_kernos's input-schema enum
        is the primary guard at the agent surface; this is the
        belt-and-suspenders substrate check.
        """
        from kernos.kernel import improvement_ledger as _ledger
        from kernos.kernel.improvement_workspace import (
            ImprovementWorkspace,
        )
        from kernos.kernel.instance_db import InstanceDB
        from kernos.utils import utc_now

        _TRUSTED_AGENTS = ("claude_code", "codex")
        if primary_coding_agent not in _TRUSTED_AGENTS:
            raise ValueError(
                f"primary_coding_agent {primary_coding_agent!r} not in "
                f"trusted-agent allowlist {_TRUSTED_AGENTS}"
            )
        if reviewer_coding_agent not in _TRUSTED_AGENTS:
            raise ValueError(
                f"reviewer_coding_agent {reviewer_coding_agent!r} not in "
                f"trusted-agent allowlist {_TRUSTED_AGENTS}"
            )

        attempt_id = f"att_{uuid.uuid4().hex[:12]}"
        ws = ImprovementWorkspace(
            data_dir=self._data_dir,
            instance_id=self._instance_id,
            live_repo_dir=self._live_repo_dir,
        )

        # Create attempt row in ledger.
        db = InstanceDB(self._data_dir)
        await db.connect()
        try:
            await _ledger.create_attempt(
                db._conn,
                instance_id=self._instance_id,
                attempt_id=attempt_id,
                spec_requirement=spec_requirement,
                primary_coding_agent=primary_coding_agent,
                reviewer_coding_agent=reviewer_coding_agent,
            )
            # Create worktree.
            try:
                worktree_path = await ws.create(attempt_id)
                await _ledger.update_attempt(
                    db._conn, attempt_id=attempt_id,
                    worktree_path=worktree_path,
                )
                await _ledger.append_event(
                    db._conn, attempt_id=attempt_id,
                    kind="workspace_created",
                    detail=worktree_path,
                )
            except Exception as exc:
                await _ledger.update_attempt(
                    db._conn, attempt_id=attempt_id,
                    final_state="workspace_create_failed",
                    ended_at=utc_now(),
                )
                await _ledger.append_event(
                    db._conn, attempt_id=attempt_id,
                    kind="workspace_create_failed",
                    detail=str(exc)[:200],
                )
                raise
        finally:
            await db.close()

        # Kick off the background task.
        task = asyncio.create_task(
            self._run_attempt(attempt_id=attempt_id),
        )
        self._running_tasks.add(task)
        task.add_done_callback(self._running_tasks.discard)

        return attempt_id

    async def _run_attempt(self, *, attempt_id: str) -> None:
        """Background task: spec cycle → impl cycle → request
        approval. Ledger writes at every step. On any uncaught
        exception, appends a terminal event so the operator can
        diagnose."""
        from kernos.kernel import improvement_ledger as _ledger
        from kernos.kernel.instance_db import InstanceDB
        from kernos.utils import utc_now

        try:
            db = InstanceDB(self._data_dir)
            await db.connect()
            try:
                attempt = await _ledger.get_attempt(db._conn, attempt_id)
                if attempt is None:
                    return  # row vanished; nothing to do
                worktree_path = attempt.get("worktree_path") or ""
                if not worktree_path:
                    await _ledger.update_attempt(
                        db._conn, attempt_id=attempt_id,
                        final_state="workspace_missing",
                        ended_at=utc_now(),
                    )
                    return

                # Spec cycle.
                spec_outcome = await self._run_spec_cycle(
                    db=db, attempt_id=attempt_id,
                    spec_requirement=attempt["spec_requirement"],
                    primary_agent=attempt["primary_coding_agent"],
                    reviewer_agent=attempt["reviewer_coding_agent"],
                    worktree_path=worktree_path,
                )
                if spec_outcome != "GREEN":
                    await _ledger.update_attempt(
                        db._conn, attempt_id=attempt_id,
                        spec_iterations_outcome=spec_outcome,
                        final_state="aborted_unconverged",
                        ended_at=utc_now(),
                    )
                    return

                # Impl cycle.
                impl_outcome = await self._run_impl_cycle(
                    db=db, attempt_id=attempt_id,
                    primary_agent=attempt["primary_coding_agent"],
                    reviewer_agent=attempt["reviewer_coding_agent"],
                    worktree_path=worktree_path,
                )
                if impl_outcome != "GREEN":
                    await _ledger.update_attempt(
                        db._conn, attempt_id=attempt_id,
                        impl_iterations_outcome=impl_outcome,
                        final_state="aborted_unconverged",
                        ended_at=utc_now(),
                    )
                    return

                # Request operator approval.
                await self._request_commit_approval(
                    db=db, attempt_id=attempt_id,
                    worktree_path=worktree_path,
                )
            finally:
                await db.close()
        except Exception as exc:
            logger.exception(
                "IMPROVE_KERNOS_ATTEMPT_FAILED attempt=%s",
                attempt_id,
            )
            # Belt-and-suspenders: append terminal event so
            # ledger inspection reveals the failure.
            try:
                from kernos.kernel.instance_db import InstanceDB
                db = InstanceDB(self._data_dir)
                await db.connect()
                try:
                    await _ledger.update_attempt(
                        db._conn, attempt_id=attempt_id,
                        final_state="aborted_consult_failure",
                        ended_at=utc_now(),
                    )
                    await _ledger.append_event(
                        db._conn, attempt_id=attempt_id,
                        kind="attempt_failed",
                        detail=str(exc)[:300],
                    )
                finally:
                    await db.close()
            except Exception:
                pass

    async def _run_spec_cycle(
        self, *, db, attempt_id: str, spec_requirement: str,
        primary_agent: str, reviewer_agent: str,
        worktree_path: str,
    ) -> str:
        """Run the spec author/reviewer convergence loop.
        Returns 'GREEN' or 'ABORTED_UNCONVERGED'."""
        from kernos.kernel import improvement_ledger as _ledger
        from kernos.kernel.improvement_review_protocol import (
            ReviewIterationState, detect_status,
            render_prompt, step_iteration,
        )

        state = ReviewIterationState.for_spec()
        prior_findings = ""
        spec_text = ""
        while not state.finished:
            iteration = state.iteration + 1
            # Author round
            author_prompt = render_prompt(
                "spec_author",
                spec_requirement=spec_requirement,
                iteration=iteration,
                prior_findings=prior_findings,
                workspace_dir=worktree_path,
            )
            author_text = await self._consult(
                target=primary_agent, prompt=author_prompt,
            )
            author_status, author_findings = detect_status(author_text)
            spec_text = author_text  # latest spec is the author's output

            # Reviewer round
            reviewer_prompt = render_prompt(
                "spec_reviewer",
                spec_requirement=spec_requirement,
                iteration=iteration,
                prior_findings="",
                workspace_dir=worktree_path,
                spec_text=spec_text,
            )
            reviewer_text = await self._consult(
                target=reviewer_agent, prompt=reviewer_prompt,
            )
            reviewer_status, reviewer_findings = detect_status(reviewer_text)

            step_iteration(
                state,
                author_status=author_status,
                reviewer_status=reviewer_status,
                author_findings=author_findings,
                reviewer_findings=reviewer_findings,
            )
            await _ledger.append_event(
                db._conn, attempt_id=attempt_id,
                kind="spec_iteration",
                detail=(
                    f"round {state.iteration}: author={author_status}, "
                    f"reviewer={reviewer_status}"
                ),
            )
            prior_findings = " | ".join(
                f for f in (author_findings, reviewer_findings) if f
            )

        # Persist final spec to worktree
        try:
            from pathlib import Path
            (Path(worktree_path) / "spec.md").write_text(spec_text)
        except OSError:
            pass

        await _ledger.update_attempt(
            db._conn, attempt_id=attempt_id,
            spec_iterations=state.iteration,
            spec_iterations_outcome=state.outcome,
        )
        return state.outcome

    async def _run_impl_cycle(
        self, *, db, attempt_id: str,
        primary_agent: str, reviewer_agent: str,
        worktree_path: str,
    ) -> str:
        """Run the impl author/reviewer convergence loop.
        Returns 'GREEN' or 'ABORTED_UNCONVERGED'."""
        from kernos.kernel import improvement_ledger as _ledger
        from kernos.kernel.improvement_review_protocol import (
            ReviewIterationState, detect_status,
            render_prompt, step_iteration,
        )

        state = ReviewIterationState.for_impl()
        prior_findings = ""
        while not state.finished:
            iteration = state.iteration + 1
            author_prompt = render_prompt(
                "impl_author",
                iteration=iteration,
                prior_findings=prior_findings,
                workspace_dir=worktree_path,
            )
            author_text = await self._consult(
                target=primary_agent, prompt=author_prompt,
            )
            author_status, author_findings = detect_status(author_text)

            reviewer_prompt = render_prompt(
                "impl_reviewer",
                iteration=iteration,
                prior_findings="",
                workspace_dir=worktree_path,
            )
            reviewer_text = await self._consult(
                target=reviewer_agent, prompt=reviewer_prompt,
            )
            reviewer_status, reviewer_findings = detect_status(reviewer_text)

            step_iteration(
                state,
                author_status=author_status,
                reviewer_status=reviewer_status,
                author_findings=author_findings,
                reviewer_findings=reviewer_findings,
            )
            await _ledger.append_event(
                db._conn, attempt_id=attempt_id,
                kind="impl_iteration",
                detail=(
                    f"round {state.iteration}: author={author_status}, "
                    f"reviewer={reviewer_status}"
                ),
            )
            prior_findings = " | ".join(
                f for f in (author_findings, reviewer_findings) if f
            )

        await _ledger.update_attempt(
            db._conn, attempt_id=attempt_id,
            impl_iterations=state.iteration,
            impl_iterations_outcome=state.outcome,
        )
        return state.outcome

    async def _request_commit_approval(
        self, *, db, attempt_id: str, worktree_path: str,
    ) -> None:
        """Capture pre-commit state + issue a
        git_commit_authorization receipt."""
        from kernos.kernel import approval_receipts as _approvals
        from kernos.kernel import improvement_ledger as _ledger
        from kernos.kernel.git_operations import (
            _compute_staged_diff_hash_async,
        )

        # We need to stage the impl_author's changes first.
        # The author writes files to worktree; we stage all
        # tracked changes (git add -u) so the receipt's
        # diff_hash + parent_sha match what the operator
        # approves. Note: agent-facing tools refuse `-A`; this
        # is the substrate orchestrator's only place where
        # bulk-staging is appropriate (the impl_author's
        # output IS the staged diff).
        rc, _out, _err = await _run_git_in(
            ["add", "-u"], cwd=worktree_path,
        )
        # Also pick up untracked files the author created.
        await _run_git_in(["add", "."], cwd=worktree_path)

        rc, head_sha, _ = await _run_git_in(
            ["rev-parse", "HEAD"], cwd=worktree_path,
        )
        expected_parent = head_sha.strip()
        expected_diff_hash = await _compute_staged_diff_hash_async(
            worktree_path,
        )

        approval_id = await _approvals.request_approval(
            data_dir=self._data_dir,
            instance_id=self._instance_id,
            kind="git_commit_authorization",
            requested_for_actor="improvement_loop",
            operator_actor_id="owner",
            request_summary=(
                f"Autonomous improvement attempt {attempt_id} "
                f"ready for commit approval."
            ),
            binding_payload={
                "kind": "git_commit_authorization",
                "attempt_id": attempt_id,
                "workspace_dir": worktree_path,
                "expected_parent_sha": expected_parent,
                "expected_diff_hash": expected_diff_hash,
                "target_branch": "main",
                "summary": f"improvement attempt {attempt_id}",
            },
            event_stream=self._receipts_event_stream,
        )
        await _ledger.append_event(
            db._conn, attempt_id=attempt_id,
            kind="approval_requested",
            detail=f"approval_id={approval_id}",
        )

    async def _consult(self, *, target: str, prompt: str) -> str:
        """Call the configured consult function. Stubbed in
        tests; in production wires to the consult kernel tool's
        underlying ACPX dispatch."""
        if self._consult_fn is None:
            raise RuntimeError(
                "ImprovementLoopOrchestrator: no consult_fn wired"
            )
        return await self._consult_fn(target=target, prompt=prompt)

    async def wait_for_running_tasks(
        self, *, timeout: float | None = None,
    ) -> None:
        """Wait for any in-flight background attempt tasks.
        Used by tests + clean shutdown."""
        if not self._running_tasks:
            return
        await asyncio.wait(
            self._running_tasks,
            timeout=timeout,
        )


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


async def _run_git_in(args: list[str], *, cwd: str) -> tuple[int, str, str]:
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


# ---------------------------------------------------------------------
# improve_kernos tool handler
# ---------------------------------------------------------------------


async def handle_improve_kernos(
    *, handler: Any, tool_input: dict, instance_id: str,
    data_dir: str,
) -> str:
    """Dispatch entry point. Builds an orchestrator + starts
    the attempt + returns prose containing the attempt_id."""
    spec_requirement = (tool_input.get("spec_requirement") or "").strip()
    if not spec_requirement:
        return (
            "`spec_requirement` is required — describe what you "
            "want me to improve about myself in natural language."
        )
    primary = tool_input.get("primary_coding_agent") or "claude_code"
    reviewer = tool_input.get("reviewer_coding_agent") or "codex"

    # Resolve live_repo_dir from the running process. The Kernos
    # binary lives in <repo>/kernos/server.py; walk up from this
    # module's path.
    from pathlib import Path
    import kernos
    kernos_pkg_dir = Path(kernos.__file__).resolve().parent
    live_repo_dir = str(kernos_pkg_dir.parent)

    orchestrator = ImprovementLoopOrchestrator(
        instance_id=instance_id,
        data_dir=data_dir,
        live_repo_dir=live_repo_dir,
        consult_fn=getattr(handler, "_consult_fn_for_loop", None),
        restart_fn=getattr(handler, "_restart_fn_for_loop", None),
        receipts_event_stream=getattr(handler, "events", None),
    )
    try:
        attempt_id = await orchestrator.start_attempt(
            spec_requirement=spec_requirement,
            primary_coding_agent=primary,
            reviewer_coding_agent=reviewer,
        )
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        logger.exception("IMPROVE_KERNOS_START_FAILED")
        return (
            f"Couldn't start the attempt: {exc}. Check "
            f"/improvement_status for any partial state."
        )
    return (
        f"Improvement attempt started: `{attempt_id}`. I'll work "
        f"through drafting + review and ping you when ready for "
        f"commit approval. Track progress via "
        f"`/improvement_status {attempt_id}`."
    )
