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
import json
import logging
import os
import uuid
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------


IMPROVE_KERNOS_TOOL: dict = {
    "name": "improve_kernos",
    "description": (
        "Start an autonomous improvement against Kernos's own "
        "source: trusted coding agents draft + implement the "
        "change, then commit + deploy automatically after review "
        "unless the change needs explicit approval. Returns immediately "
        "with a tracking handle; the work continues in the "
        "background. Tell the user about it like a person would "
        "— 'on it, I'll handle the review and tell you when it's "
        "live or if I need approval' — not as a status report; do "
        "NOT surface the raw "
        "attempt id or /improvement_status unless they ask for "
        "detail or you're in an admin/diagnostic context."
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


PROCEED_WITH_RECOVERY_TOOL: dict = {
    "name": "proceed_with_recovery",
    "description": (
        "Proceed with one bounded fix-up cycle for an autonomous "
        "improvement attempt whose post-restart self-test failed. "
        "Only available when the active space owns an attempt in "
        "awaiting_recovery_decision."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "attempt_id": {"type": "string"},
        },
        "required": ["attempt_id"],
        "additionalProperties": False,
    },
}


ABANDON_ATTEMPT_TOOL: dict = {
    "name": "abandon_attempt",
    "description": (
        "Abandon an autonomous improvement attempt whose post-restart "
        "self-test failed. Only available when the active space owns "
        "an attempt in awaiting_recovery_decision."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "attempt_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["attempt_id", "reason"],
        "additionalProperties": False,
    },
}


RECOVERY_TOOL_NAMES = frozenset({
    "proceed_with_recovery",
    "abandon_attempt",
})

_AUTO_PROCEED_MAX_FILES = 25
_AUTO_PROCEED_MAX_NET_DELETIONS = 400


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
        notify_fn: Any = None,            # async (attempt_id, final_state) -> None
        announce_fn: Any = None,          # async (space_id, message) -> None
        receipts_event_stream: Any = None,
    ) -> None:
        self._instance_id = instance_id
        self._data_dir = data_dir
        self._live_repo_dir = live_repo_dir
        self._consult_fn = consult_fn
        self._restart_fn = restart_fn
        self._notify_fn = notify_fn
        self._announce_fn = announce_fn
        self._receipts_event_stream = receipts_event_stream
        # Track running background tasks so tests + shutdown
        # can wait on them.
        self._running_tasks: set[asyncio.Task] = set()
        self._terminal_notify_sent: set[str] = set()

    # --- Public entry points ---

    async def start_attempt(
        self,
        *,
        spec_requirement: str,
        primary_coding_agent: str = "claude_code",
        reviewer_coding_agent: str = "codex",
        origin_space_id: str = "",
        origin_member_id: str = "",
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
        if self._consult_fn is None or not callable(self._consult_fn):
            raise ValueError(
                "Improvement loop consult seam is unavailable; refusing "
                "to create an attempt before the production consult path "
                "is wired."
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
            await _ledger.append_event(
                db._conn, attempt_id=attempt_id,
                kind="attempt_origin",
                detail=json.dumps(
                    {
                        "instance_id": self._instance_id,
                        "origin_space_id": origin_space_id,
                        "origin_member_id": origin_member_id,
                    },
                    separators=(",", ":"),
                ),
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
        # SELF-IMPROVEMENT-TERMINAL-NOTIFY: when the loop finishes (the
        # approval gate OR any abort), tell the user instead of going
        # silent. The done-callback is sync, so schedule the async
        # notifier as its own task.
        def _schedule_terminal_notify(_t, aid=attempt_id) -> None:
            notify_task = asyncio.create_task(
                self._notify_on_terminal(aid)
            )
            self._running_tasks.add(notify_task)
            notify_task.add_done_callback(self._running_tasks.discard)

        task.add_done_callback(_schedule_terminal_notify)

        return attempt_id

    async def _notify_on_terminal(self, attempt_id: str) -> None:
        """Read the attempt's terminal state and hand it to ``notify_fn``
        (wired in the handler to surface a whisper to the originating
        member). Best-effort: never raises into the event loop."""
        if self._notify_fn is None:
            return
        try:
            from kernos.kernel.instance_db import InstanceDB
            from kernos.kernel import improvement_ledger as _ledger
            db = InstanceDB(self._data_dir)
            await db.connect()
            try:
                attempts = await _ledger.list_recent_attempts(
                    db._conn, self._instance_id, limit=20,
                )
            finally:
                await db.close()
            row = next(
                (a for a in attempts if a.get("attempt_id") == attempt_id),
                None,
            )
            final_state = (row or {}).get("final_state") or ""
            if not final_state:
                return
            if attempt_id in self._terminal_notify_sent:
                return
            self._terminal_notify_sent.add(attempt_id)
            await self._notify_fn(attempt_id, final_state)
        except Exception as exc:
            logger.warning(
                "IMPROVE_KERNOS_TERMINAL_NOTIFY_FAILED attempt=%s: %s",
                attempt_id, exc,
            )

    async def _announce(self, space_id: str, message: str) -> None:
        """Best-effort user-facing proactive announcement seam."""
        if self._announce_fn is None:
            return
        try:
            await _maybe_await(self._announce_fn(space_id, message))
        except Exception as exc:
            logger.warning(
                "IMPROVE_KERNOS_ANNOUNCE_FAILED space=%s: %s",
                space_id, exc,
            )

    async def _run_attempt(self, *, attempt_id: str) -> None:
        """Background task: spec cycle → impl cycle → request
        approval. Ledger writes at every step. On any uncaught
        exception, appends a terminal event so the operator can
        diagnose."""
        from kernos.kernel import improvement_ledger as _ledger
        from kernos.kernel.instance_db import InstanceDB
        from kernos.utils import utc_now

        try:
            post_db_action: Any = None
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

                origin = await self._attempt_origin(
                    db._conn, attempt_id=attempt_id,
                )
                post_db_action = await self._finalize_after_impl_green(
                    db=db, attempt_id=attempt_id,
                    worktree_path=worktree_path,
                    origin_space_id=origin.get("origin_space_id", "") or "",
                    origin_member_id=origin.get("origin_member_id", "") or "",
                )
            finally:
                await db.close()
            if post_db_action is not None:
                await post_db_action()
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

    async def _attempt_origin(
        self, conn: Any, *, attempt_id: str,
    ) -> dict[str, str]:
        from kernos.kernel import improvement_ledger as _ledger

        events = await _ledger.get_attempt_events(conn, attempt_id)
        for event in events:
            if event.get("kind") != "attempt_origin":
                continue
            detail = _loads_detail(event.get("detail") or "")
            return {
                "origin_space_id": str(detail.get("origin_space_id") or ""),
                "origin_member_id": str(detail.get("origin_member_id") or ""),
            }
        return {"origin_space_id": "", "origin_member_id": ""}

    async def _finalize_after_impl_green(
        self, *, db: Any, attempt_id: str, worktree_path: str,
        origin_space_id: str, origin_member_id: str,
    ) -> Any:
        """Prepare the durable approval receipt with the open ledger DB.

        Returns a post-close async action for user notification and, when
        allowed, approval + commit continuation. The returned action must run
        after the caller closes ``db`` because the continuation opens its own
        InstanceDB connection.
        """
        from kernos.kernel import improvement_ledger as _ledger

        approval_id = await self._request_commit_approval(
            db=db, attempt_id=attempt_id, worktree_path=worktree_path,
        )
        require_approval = _env_truthy("KERNOS_IMPROVE_REQUIRE_APPROVAL")
        block_reason = await self._auto_proceed_block_reason(worktree_path)
        await _ledger.update_attempt(
            db._conn,
            attempt_id=attempt_id,
            final_state="awaiting_commit_approval",
        )

        if require_approval or block_reason:
            reasons: list[str] = []
            if require_approval:
                reasons.append(
                    "KERNOS_IMPROVE_REQUIRE_APPROVAL is set"
                )
            if block_reason:
                reasons.append(block_reason)
            reason_text = "; ".join(reasons)

            async def _pause_for_human() -> None:
                await self._notify_on_terminal(attempt_id)
                await self._announce(
                    origin_space_id,
                    (
                        "Implemented the change and it passed author+reviewer "
                        "review, but I need human approval before committing. "
                        f"Reason: {reason_text}. "
                        f"Approval `{approval_id}` is waiting.\n"
                        f"/approve {approval_id} CONFIRM"
                    ),
                )

            return _pause_for_human

        async def _auto_proceed() -> None:
            await self._announce(
                origin_space_id,
                (
                    "Implemented the change; it passed author+reviewer "
                    "review. Committing and deploying now - I'll tell you "
                    f"when it's live or if it rolls back. (attempt {attempt_id})"
                ),
            )
            from kernos.kernel import approval_receipts as _approvals
            ok, msg = await _approvals.approve(
                data_dir=self._data_dir,
                approval_id=approval_id,
                instance_id=self._instance_id,
                invoking_member_id=(origin_member_id or "owner"),
                event_stream=self._receipts_event_stream,
            )
            if not ok:
                await self._set_awaiting_commit_approval(attempt_id)
                await self._announce(
                    origin_space_id,
                    (
                        "Implemented the change and it passed review, but "
                        f"auto-approval failed: {msg} "
                        f"Approval `{approval_id}` is waiting.\n"
                        f"/approve {approval_id} CONFIRM"
                    ),
                )
                return

            await self._append_auto_approved_event(
                attempt_id=attempt_id, approval_id=approval_id,
            )
            result = await continue_approved_improvement_commit(
                data_dir=self._data_dir,
                instance_id=self._instance_id,
                approval_id=approval_id,
                restart_fn=self._restart_fn,
            )
            await self._announce(origin_space_id, result)

        return _auto_proceed

    async def _append_auto_approved_event(
        self, *, attempt_id: str, approval_id: str,
    ) -> None:
        from kernos.kernel import improvement_ledger as _ledger
        from kernos.kernel.instance_db import InstanceDB

        db = InstanceDB(self._data_dir)
        await db.connect()
        try:
            await _ledger.append_event(
                db._conn, attempt_id=attempt_id,
                kind="auto_approved",
                detail=f"approval_id={approval_id}",
            )
        finally:
            await db.close()

    async def _set_awaiting_commit_approval(self, attempt_id: str) -> None:
        from kernos.kernel import improvement_ledger as _ledger
        from kernos.kernel.instance_db import InstanceDB

        db = InstanceDB(self._data_dir)
        await db.connect()
        try:
            await _ledger.update_attempt(
                db._conn,
                attempt_id=attempt_id,
                final_state="awaiting_commit_approval",
            )
        finally:
            await db.close()

    async def _auto_proceed_block_reason(self, worktree_path: str) -> str:
        files = await _staged_files(worktree_path)
        if not files:
            files = await _changed_files(worktree_path)
        if any(path == "start.sh" for path in files):
            return (
                "change touches protected path start.sh, which is "
                "human-only and un-rollbackable"
            )
        if len(files) > _AUTO_PROCEED_MAX_FILES:
            return (
                f"change touches {len(files)} files, over the "
                f"auto-proceed limit of {_AUTO_PROCEED_MAX_FILES}"
            )

        net_deletions = await _net_deletions_against_head(worktree_path)
        if net_deletions > _AUTO_PROCEED_MAX_NET_DELETIONS:
            return (
                f"change deletes {net_deletions} more lines than it adds, "
                "over the auto-proceed limit of "
                f"{_AUTO_PROCEED_MAX_NET_DELETIONS}"
            )
        return ""

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
                workspace_dir=worktree_path,
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
                workspace_dir=worktree_path,
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
                workspace_dir=worktree_path,
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
                workspace_dir=worktree_path,
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
        recovery_iteration: int | None = None,
    ) -> str:
        """Capture pre-commit state + issue a
        git_commit_authorization receipt."""
        return await _request_commit_approval_for_attempt(
            db=db,
            data_dir=self._data_dir,
            instance_id=self._instance_id,
            attempt_id=attempt_id,
            worktree_path=worktree_path,
            receipts_event_stream=self._receipts_event_stream,
            recovery_iteration=recovery_iteration,
        )

    async def _consult(
        self, *, target: str, prompt: str, workspace_dir: str = "",
    ) -> str:
        """Call the configured consult function. Stubbed in
        tests; in production wires to the consult kernel tool's
        underlying ACPX dispatch."""
        if self._consult_fn is None:
            raise RuntimeError(
                "ImprovementLoopOrchestrator: no consult_fn wired"
            )
        return await _call_consult_fn(
            self._consult_fn,
            target=target,
            prompt=prompt,
            instance_id=self._instance_id,
            workspace_dir=workspace_dir,
        )

    async def wait_for_running_tasks(
        self, *, timeout: float | None = None,
    ) -> None:
        """Wait for any in-flight background attempt tasks.
        Used by tests + clean shutdown."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout is not None else None
        while self._running_tasks:
            wait_timeout = None
            if deadline is not None:
                wait_timeout = max(0.0, deadline - loop.time())
                if wait_timeout <= 0:
                    return
            _done, pending = await asyncio.wait(
                set(self._running_tasks),
                timeout=wait_timeout,
            )
            if pending:
                return
            await asyncio.sleep(0)


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


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


def _detail(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _loads_detail(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _env_truthy(name: str) -> bool:
    value = (os.environ.get(name) or "").strip().lower()
    return bool(value) and value not in {"0", "false", "no", "off"}


_STALL_THRESHOLD_SEC: int = int(
    os.environ.get("KERNOS_IMPROVEMENT_STALL_THRESHOLD_SEC", "720")
)


async def find_stalled_improvement_attempts(
    data_dir: str, *, stall_threshold_sec: int | None = None,
) -> list[dict[str, Any]]:
    """In-flight improvement attempts (final_state IS NULL) whose LAST ledger
    event is older than the stall threshold — i.e. the loop is stuck with no
    progress (a hung consult). Powers the self-monitoring surface so the user
    learns about a stall instead of waiting on silence (the gap that let an
    attempt die 2h unnoticed).

    Each dict: attempt_id, last_kind, last_seq, stalled_sec, origin_space_id,
    origin_member_id. Best-effort; never raises.
    """
    import datetime as _dt
    import aiosqlite
    threshold = (
        _STALL_THRESHOLD_SEC if stall_threshold_sec is None
        else int(stall_threshold_sec)
    )
    db_path = os.path.join(data_dir, "instance.db")
    out: list[dict[str, Any]] = []
    try:
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT attempt_id FROM improvement_attempts "
                "WHERE final_state IS NULL"
            ) as cur:
                ids = [r["attempt_id"] for r in await cur.fetchall()]
            now = _dt.datetime.now(_dt.timezone.utc)
            for aid in ids:
                async with conn.execute(
                    "SELECT kind, sequence, timestamp FROM "
                    "improvement_attempt_events WHERE attempt_id=? "
                    "ORDER BY sequence DESC LIMIT 1", (aid,),
                ) as cur:
                    last = await cur.fetchone()
                if not last:
                    continue
                try:
                    last_ts = _dt.datetime.fromisoformat(last["timestamp"])
                except Exception:
                    continue
                stalled = (now - last_ts).total_seconds()
                if stalled < threshold:
                    continue
                origin_space = origin_member = ""
                async with conn.execute(
                    "SELECT detail FROM improvement_attempt_events WHERE "
                    "attempt_id=? AND kind='attempt_origin' ORDER BY "
                    "sequence ASC LIMIT 1", (aid,),
                ) as cur:
                    orow = await cur.fetchone()
                if orow:
                    od = _loads_detail(orow["detail"] or "")
                    origin_space = od.get("origin_space_id", "") or ""
                    origin_member = od.get("origin_member_id", "") or ""
                out.append({
                    "attempt_id": aid,
                    "last_kind": last["kind"],
                    "last_seq": last["sequence"],
                    "stalled_sec": int(stalled),
                    "origin_space_id": origin_space,
                    "origin_member_id": origin_member,
                })
    except Exception as exc:
        logger.warning("IMPROVEMENT_STALL_CHECK_FAILED: %s", exc)
    return out


async def _call_consult_fn(
    consult_fn: Any,
    *,
    target: str,
    prompt: str,
    instance_id: str = "",
    workspace_dir: str = "",
) -> str:
    kwargs: dict[str, Any] = {"target": target, "prompt": prompt}
    try:
        import inspect
        sig = inspect.signature(consult_fn)
        params = sig.parameters
        accepts_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in params.values()
        )
        if instance_id and (accepts_kwargs or "instance_id" in params):
            kwargs["instance_id"] = instance_id
        if workspace_dir and (accepts_kwargs or "workspace_dir" in params):
            kwargs["workspace_dir"] = workspace_dir
    except (TypeError, ValueError):
        pass
    return await consult_fn(**kwargs)


async def _mark_attempt_failed(
    conn: Any,
    *,
    attempt_id: str,
    reason: str,
    event_kind: str = "attempt_failed",
    detail: dict[str, Any] | None = None,
) -> None:
    from kernos.kernel import improvement_ledger as _ledger
    from kernos.utils import utc_now

    await _ledger.update_attempt(
        conn,
        attempt_id=attempt_id,
        final_state="attempt_failed",
        ended_at=utc_now(),
    )
    payload = {"reason": reason}
    if detail:
        payload.update(detail)
    await _ledger.append_event(
        conn,
        attempt_id=attempt_id,
        kind=event_kind,
        detail=_detail(payload),
    )


async def _request_commit_approval_for_attempt(
    *,
    db: Any,
    data_dir: str,
    instance_id: str,
    attempt_id: str,
    worktree_path: str,
    receipts_event_stream: Any = None,
    recovery_iteration: int | None = None,
) -> str:
    """Stage the worktree diff and issue a commit-authorization receipt."""
    from kernos.kernel import approval_receipts as _approvals
    from kernos.kernel import improvement_ledger as _ledger
    from kernos.kernel.git_operations import (
        _compute_staged_diff_hash_async,
    )

    # The coding agent edits the worktree directly. This substrate
    # seam stages the whole worktree so the approval receipt binds
    # exactly to the diff the operator sees.
    await _run_git_in(["add", "-u"], cwd=worktree_path)
    await _run_git_in(["add", "."], cwd=worktree_path)

    _rc, head_sha, _ = await _run_git_in(
        ["rev-parse", "HEAD"], cwd=worktree_path,
    )
    expected_parent = head_sha.strip()
    expected_diff_hash = await _compute_staged_diff_hash_async(
        worktree_path,
    )
    is_recovery = recovery_iteration is not None
    summary = (
        f"improvement attempt {attempt_id} recovery {recovery_iteration}"
        if is_recovery else f"improvement attempt {attempt_id}"
    )
    request_summary = (
        f"Recovery iteration {recovery_iteration} for autonomous "
        f"improvement attempt {attempt_id} ready for commit approval."
        if is_recovery else
        f"Autonomous improvement attempt {attempt_id} ready for "
        f"commit approval."
    )
    binding_payload: dict[str, Any] = {
        "kind": "git_commit_authorization",
        "attempt_id": attempt_id,
        "workspace_dir": worktree_path,
        "expected_parent_sha": expected_parent,
        "expected_diff_hash": expected_diff_hash,
        "target_branch": "main",
        "summary": summary,
    }
    if is_recovery:
        binding_payload["recovery_iteration"] = recovery_iteration

    approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id=instance_id,
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary=request_summary,
        binding_payload=binding_payload,
        event_stream=receipts_event_stream,
    )
    detail = f"approval_id={approval_id}"
    if is_recovery:
        detail += f" recovery_iteration={recovery_iteration}"
    await _ledger.append_event(
        db._conn, attempt_id=attempt_id,
        kind="approval_requested",
        detail=detail,
    )
    return approval_id


async def _staged_files(workspace_dir: str) -> list[str]:
    rc, out, _ = await _run_git_in(
        ["diff", "--cached", "--name-only"], cwd=workspace_dir,
    )
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


async def _changed_files(workspace_dir: str) -> list[str]:
    rc, out, _ = await _run_git_in(
        ["diff", "--name-only", "HEAD"], cwd=workspace_dir,
    )
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


async def _net_deletions_against_head(workspace_dir: str) -> int:
    rc, out, _ = await _run_git_in(
        ["diff", "--cached", "--numstat"], cwd=workspace_dir,
    )
    if rc != 0 or not out.strip():
        rc, out, _ = await _run_git_in(
            ["diff", "--numstat", "HEAD"], cwd=workspace_dir,
        )
    if rc != 0:
        return 0
    additions = 0
    deletions = 0
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 2:
            continue
        try:
            additions += int(parts[0])
            deletions += int(parts[1])
        except ValueError:
            continue
    return max(0, deletions - additions)


async def _head_sha(workspace_dir: str) -> str:
    rc, out, _ = await _run_git_in(["rev-parse", "HEAD"], cwd=workspace_dir)
    return out.strip() if rc == 0 else ""


async def _recover_unrecorded_commit_if_safe(
    *,
    data_dir: str,
    approval_id: str,
    workspace_dir: str,
    expected_parent: str,
    expected_diff_hash: str,
) -> tuple[str, dict[str, Any]]:
    """Recover a commit made before the receipt outcome was written."""
    import hashlib
    from datetime import datetime, timezone
    from kernos.kernel import approval_receipts as _approvals

    current_head = await _head_sha(workspace_dir)
    detail: dict[str, Any] = {
        "expected_parent_sha": expected_parent,
        "current_head": current_head,
        "expected_diff_hash": expected_diff_hash,
    }
    if not expected_parent:
        detail["recovery_reason"] = "missing_expected_parent_sha"
        return "", detail
    if not expected_diff_hash:
        detail["recovery_reason"] = "missing_expected_diff_hash"
        return "", detail
    if not current_head:
        detail["recovery_reason"] = "head_unresolved"
        return "", detail
    if current_head == expected_parent:
        detail["recovery_reason"] = "head_unchanged"
        return "", detail

    rc, _out, stderr = await _run_git_in(
        ["merge-base", "--is-ancestor", expected_parent, current_head],
        cwd=workspace_dir,
    )
    if rc != 0:
        detail["recovery_reason"] = "head_not_descendant_of_expected_parent"
        if stderr.strip():
            detail["git_error"] = stderr.strip()[:300]
        return "", detail

    rc, diff_text, stderr = await _run_git_in(
        ["diff", expected_parent, current_head],
        cwd=workspace_dir,
    )
    if rc != 0:
        detail["recovery_reason"] = "committed_diff_unreadable"
        detail["git_error"] = (stderr.strip() or "unknown error")[:300]
        return "", detail
    actual_diff_hash = (
        f"sha256:{hashlib.sha256(diff_text.encode('utf-8')).hexdigest()}"
    )
    detail["actual_diff_hash"] = actual_diff_hash
    if actual_diff_hash != expected_diff_hash:
        detail["recovery_reason"] = "committed_diff_mismatch"
        return "", detail

    ok = await _approvals.set_outcome_field(
        data_dir=data_dir,
        approval_id=approval_id,
        field="commit_sha",
        value=current_head,
    )
    if not ok:
        detail["recovery_reason"] = "receipt_outcome_write_failed"
        return "", detail
    await _approvals.set_outcome_field(
        data_dir=data_dir,
        approval_id=approval_id,
        field="committed_at",
        value=datetime.now(timezone.utc).isoformat(),
    )
    await _approvals.set_outcome_field(
        data_dir=data_dir,
        approval_id=approval_id,
        field="recovered_unrecorded_commit",
        value=True,
    )
    detail["recovered_unrecorded_commit"] = True
    return current_head, detail


def _extract_commit_sha(text: str) -> str:
    import re
    match = re.search(r"`([0-9a-fA-F]{7,40})`", text or "")
    return match.group(1) if match else ""


def _push_message(push_result: Any) -> str:
    if isinstance(push_result, dict):
        return str(push_result.get("message") or push_result)
    return str(push_result or "")


def _structured_push_ok(push_result: Any, *, commit_sha: str) -> bool | None:
    if not isinstance(push_result, dict):
        return None
    ok = bool(
        push_result.get("ok")
        or push_result.get("success")
        or push_result.get("pushed")
    )
    result_sha = str(push_result.get("commit_sha") or "")
    if ok and result_sha and result_sha != commit_sha:
        return False
    return ok


_PUSH_UNCONFIRMED_STATE = "push_unconfirmed"


def _push_reason(push_result: Any) -> str:
    if not isinstance(push_result, dict):
        return ""
    return str(push_result.get("reason") or "")


def _push_result_confirmation_state(
    push_result: Any, *, commit_sha: str,
) -> str:
    if not isinstance(push_result, dict):
        return "unknown"
    origin_sha = str(push_result.get("origin_sha") or "").strip()
    if push_result.get("origin_confirmed") is True:
        result_sha = str(push_result.get("commit_sha") or "").strip()
        if (
            (not result_sha or result_sha == commit_sha)
            and (not origin_sha or origin_sha == commit_sha)
        ):
            return "confirmed"
    reason = _push_reason(push_result)
    if reason == "post_push_unconfirmed":
        return "negative"
    if reason == "post_push_fetch_failed":
        return "unknown"
    return "unknown"


def _origin_confirmation_state(
    origin_confirmed: bool, detail: dict[str, Any], *, commit_sha: str,
) -> str:
    if origin_confirmed:
        return "confirmed"
    reason = str(detail.get("origin_confirmation_reason") or "")
    if reason in {"fetch_failed", "origin_unresolved"}:
        return "unknown"
    if detail.get("origin_confirmation_error"):
        return "unknown"
    origin_sha = str(detail.get("origin_sha") or "").strip()
    if origin_sha:
        if origin_sha == commit_sha:
            return "confirmed"
        return "negative"
    return "unknown"


def _push_confirmation_retryable(
    push_result: Any, *, structured_ok: bool | None,
) -> bool:
    if structured_ok is not False:
        return True
    return _push_reason(push_result) == "post_push_fetch_failed"


async def _mark_push_unconfirmed(
    conn: Any,
    *,
    attempt_id: str,
    approval_id: str,
    commit_sha: str,
    target_branch: str,
    recovery_iteration: Any = None,
    detail: dict[str, Any] | None = None,
) -> None:
    from kernos.kernel import improvement_ledger as _ledger

    payload = {
        "approval_id": approval_id,
        "commit_sha": commit_sha,
        "is_recovery": recovery_iteration is not None,
        "target_branch": target_branch,
        "reason": "push_unconfirmed",
    }
    if recovery_iteration is not None:
        payload["recovery_iteration"] = recovery_iteration
    if detail:
        payload.update(detail)
    if not await _event_recorded_for_approval(
        conn,
        attempt_id=attempt_id,
        kind="push_unconfirmed",
        approval_id=approval_id,
    ):
        await _ledger.append_event(
            conn,
            attempt_id=attempt_id,
            kind="push_unconfirmed",
            detail=_detail(payload),
        )
    await _ledger.update_attempt(
        conn,
        attempt_id=attempt_id,
        final_state=_PUSH_UNCONFIRMED_STATE,
    )


async def _origin_head_matches_commit(
    *,
    git_ops: Any,
    workspace_dir: str,
    target_branch: str,
    commit_sha: str,
    instance_id: str,
    data_dir: str,
) -> tuple[bool, dict[str, Any]]:
    if (
        callable(getattr(git_ops, "handle_git_fetch", None))
        and callable(getattr(git_ops, "handle_git_rev_parse", None))
    ):
        fetch_text = await git_ops.handle_git_fetch(
            tool_input={"workspace_dir": workspace_dir, "remote": "origin"},
            instance_id=instance_id,
            data_dir=data_dir,
        )
        fetch_result = str(fetch_text or "").strip()
        if not fetch_result.lower().startswith("fetched"):
            return False, {
                "origin_confirmed": False,
                "origin_confirmation_reason": "fetch_failed",
                "fetch_result": (fetch_result or "unknown error")[:300],
            }
        origin_sha = await git_ops.handle_git_rev_parse(
            tool_input={
                "workspace_dir": workspace_dir,
                "ref": f"origin/{target_branch}",
            },
            instance_id=instance_id,
            data_dir=data_dir,
        )
        origin_sha_text = str(origin_sha).strip()
        if "couldn't be resolved" in origin_sha_text.lower():
            return False, {
                "origin_confirmed": False,
                "origin_confirmation_reason": "origin_unresolved",
                "fetch_result": fetch_result[:300],
                "origin_error": origin_sha_text[:300],
            }
        return origin_sha_text == commit_sha, {
            "origin_sha": origin_sha_text,
            "fetch_result": fetch_result[:300],
        }

    fetch_rc, fetch_out, fetch_err = await git_ops._run_git(
        ["fetch", "origin"], cwd=workspace_dir,
    )
    fetch_result = fetch_err.strip() or fetch_out.strip()
    if fetch_rc != 0:
        return False, {
            "origin_confirmed": False,
            "origin_confirmation_reason": "fetch_failed",
            "fetch_rc": fetch_rc,
            "fetch_result": (fetch_result or "unknown error")[:300],
        }

    rc, origin_out, origin_err = await git_ops._run_git(
        ["rev-parse", "--verify", f"origin/{target_branch}"],
        cwd=workspace_dir,
    )
    origin_sha_text = origin_out.strip()
    if rc != 0:
        return False, {
            "origin_confirmed": False,
            "origin_confirmation_reason": "origin_unresolved",
            "fetch_rc": fetch_rc,
            "fetch_result": fetch_result[:300],
            "origin_error": (origin_err.strip() or "not found")[:300],
        }
    return origin_sha_text == commit_sha, {
        "origin_sha": origin_sha_text,
        "fetch_rc": fetch_rc,
        "fetch_result": fetch_result[:300],
    }


def _default_live_repo_dir() -> str:
    from pathlib import Path
    import kernos

    return str(Path(kernos.__file__).resolve().parent.parent)


async def _live_repo_head_sha(live_repo_dir: str) -> tuple[str, dict[str, Any]]:
    from kernos.kernel import git_operations as _git_ops

    rc, out, stderr = await _git_ops._run_git(
        ["rev-parse", "--verify", "HEAD"], cwd=live_repo_dir,
    )
    if rc != 0:
        return "", {
            "live_repo_dir": live_repo_dir,
            "live_head_error": (stderr.strip() or out.strip() or "unknown"),
        }
    return out.strip(), {"live_repo_dir": live_repo_dir}


async def _worktree_has_diff(workspace_dir: str) -> bool:
    try:
        rc, out, _ = await _run_git_in(
            ["status", "--porcelain"], cwd=workspace_dir,
        )
    except Exception:
        return True
    if rc != 0:
        return True
    return bool(out.strip())


async def _event_recorded_for_approval(
    conn: Any,
    *,
    attempt_id: str,
    kind: str,
    approval_id: str,
) -> bool:
    from kernos.kernel import improvement_ledger as _ledger

    events = await _ledger.get_attempt_events(conn, attempt_id)
    for event in events:
        if event.get("kind") != kind:
            continue
        detail = _loads_detail(event.get("detail") or "")
        if detail.get("approval_id") == approval_id:
            return True
    return False


async def _commit_row_for_approval(
    conn: Any,
    *,
    attempt_id: str,
    approval_id: str,
) -> dict[str, Any] | None:
    import aiosqlite

    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        "SELECT * FROM improvement_attempt_commits "
        "WHERE attempt_id=? AND approval_id=? "
        "ORDER BY commit_sequence DESC LIMIT 1",
        (attempt_id, approval_id),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def _recovery_started_count(conn: Any, attempt_id: str) -> int:
    from kernos.kernel import improvement_ledger as _ledger

    events = await _ledger.get_attempt_events(conn, attempt_id)
    return sum(1 for e in events if e.get("kind") == "recovery_started")


async def _latest_event_detail(
    conn: Any, attempt_id: str, kind: str,
) -> str:
    from kernos.kernel import improvement_ledger as _ledger

    events = await _ledger.get_attempt_events(conn, attempt_id)
    for event in reversed(events):
        if event.get("kind") == kind:
            return event.get("detail") or ""
    return ""


async def _active_push_unconfirmed_approval_matches(
    conn: Any,
    *,
    attempt_id: str,
    approval_id: str,
    binding: dict[str, Any],
) -> bool:
    marker = _loads_detail(
        await _latest_event_detail(conn, attempt_id, "push_unconfirmed")
    )
    if str(marker.get("approval_id") or "") != approval_id:
        return False

    marker_commit_sha = str(marker.get("commit_sha") or "")
    if not marker_commit_sha:
        return False
    commit_row = await _commit_row_for_approval(
        conn, attempt_id=attempt_id, approval_id=approval_id,
    )
    if str((commit_row or {}).get("commit_sha") or "") != marker_commit_sha:
        return False

    target_branch = str(binding.get("target_branch") or "main")
    marker_branch = str(marker.get("target_branch") or "")
    if marker_branch and marker_branch != target_branch:
        return False

    binding_recovery_iteration = binding.get("recovery_iteration")
    binding_is_recovery = binding_recovery_iteration is not None
    if "is_recovery" in marker and bool(marker["is_recovery"]) != binding_is_recovery:
        return False
    if "recovery_iteration" in marker:
        if str(marker.get("recovery_iteration")) != str(binding_recovery_iteration):
            return False
    return True


async def _attempt_origin(conn: Any, attempt_id: str) -> dict[str, Any]:
    return _loads_detail(
        await _latest_event_detail(conn, attempt_id, "attempt_origin")
    )


def _failed_test_ids_from_summary(summary: str) -> list[str]:
    marker = "Failing:"
    if marker not in (summary or ""):
        return []
    tail = summary.split(marker, 1)[1].splitlines()[0].strip().rstrip(".")
    return [
        item.strip()
        for item in tail.replace("+", ",").split(",")
        if item.strip() and " more" not in item
    ][:10]


def _outcome_from_self_test_result(result: Any, attempt: dict) -> tuple[str, str]:
    if isinstance(result, dict):
        summary = str(result.get("summary") or result.get("prose") or "")
        outcome = str(result.get("test_outcome") or result.get("outcome") or "")
        return outcome, summary
    summary = str(result or "")
    outcome = str(attempt.get("test_outcome") or "")
    if not outcome:
        lowered = summary.lower()
        if "passed" in lowered and "failed" not in lowered and "error" not in lowered:
            outcome = "pass"
        elif summary:
            outcome = "fail"
    return outcome, summary


async def continue_approved_improvement_commit(
    *,
    data_dir: str,
    instance_id: str,
    approval_id: str,
    restart_fn: Any = None,
    git_commit_fn: Any = None,
    git_push_fn: Any = None,
) -> str:
    """Post-approval continuation for improvement commit receipts."""
    from kernos.kernel import approval_receipts as _approvals
    from kernos.kernel import improvement_ledger as _ledger
    from kernos.kernel import git_operations as _git_ops
    from kernos.kernel.instance_db import InstanceDB

    receipt = await _approvals.get_receipt(
        data_dir=data_dir, approval_id=approval_id,
    )
    if receipt is None:
        return f"Improvement continuation skipped: approval `{approval_id}` not found."
    if receipt.get("kind") != "git_commit_authorization":
        return "Improvement continuation skipped: wrong receipt kind."
    if receipt.get("state") != "approved":
        return (
            f"Improvement continuation skipped: approval is "
            f"`{receipt.get('state')}`."
        )
    binding = _loads_detail(receipt.get("binding_payload_json") or "{}")
    attempt_id = str(binding.get("attempt_id") or "")
    if not attempt_id:
        return "No improvement attempt binding; no improvement continuation ran."
    workspace_dir = str(binding.get("workspace_dir") or "")
    expected_parent = str(binding.get("expected_parent_sha") or "")
    target_branch = str(binding.get("target_branch") or "main")
    recovery_iteration = binding.get("recovery_iteration")

    db = InstanceDB(data_dir)
    await db.connect()
    try:
        attempt = await _ledger.get_attempt(db._conn, attempt_id)
        if attempt is None or attempt.get("instance_id") != instance_id:
            return f"Improvement attempt `{attempt_id}` not found for this instance."
        expected_state = (
            "awaiting_recovery_commit_approval"
            if recovery_iteration is not None else "awaiting_commit_approval"
        )
        state = str(attempt.get("final_state") or "")
        allowed_states = (
            _RECOVERY_APPROVAL_WAIT_STATES
            if recovery_iteration is not None else _INITIAL_APPROVAL_WAIT_STATES
        )
        if state not in allowed_states:
            if await _event_recorded_for_approval(
                db._conn,
                attempt_id=attempt_id,
                kind="commit_recorded",
                approval_id=approval_id,
            ):
                return (
                    f"Improvement continuation skipped: approval "
                    f"`{approval_id}` was already recorded."
                )
            return (
                f"Improvement continuation skipped: attempt `{attempt_id}` "
                f"is `{state or 'running'}`, not `{expected_state}`."
            )
        if state == _PUSH_UNCONFIRMED_STATE and not (
            await _active_push_unconfirmed_approval_matches(
                db._conn,
                attempt_id=attempt_id,
                approval_id=approval_id,
                binding=binding,
            )
        ):
            return (
                f"Improvement continuation skipped: approval `{approval_id}` "
                f"is not the active push confirmation for attempt "
                f"`{attempt_id}`."
            )
        if restart_fn is None:
            await _mark_attempt_failed(
                db._conn,
                attempt_id=attempt_id,
                reason="restart_seam_unavailable",
                event_kind="attempt_failed",
                detail={"approval_id": approval_id},
            )
            return (
                f"Improvement attempt `{attempt_id}` failed before commit: "
                "restart seam is unavailable, so post-restart self-tests "
                "cannot run."
            )
        commit_fn = git_commit_fn or _git_ops.handle_git_commit
        push_fn = git_push_fn or _git_ops.handle_git_push
        existing_commit = await _commit_row_for_approval(
            db._conn,
            attempt_id=attempt_id,
            approval_id=approval_id,
        )
        if existing_commit:
            commit_sha = str(existing_commit.get("commit_sha") or "")
            commit_recorded = await _event_recorded_for_approval(
                db._conn,
                attempt_id=attempt_id,
                kind="commit_recorded",
                approval_id=approval_id,
            )
            push_recorded = await _event_recorded_for_approval(
                db._conn,
                attempt_id=attempt_id,
                kind="push_succeeded",
                approval_id=approval_id,
            )
            push_detail: dict[str, Any] = {}
            if not push_recorded:
                try:
                    origin_confirmed, push_detail = await _origin_head_matches_commit(
                        git_ops=_git_ops,
                        workspace_dir=workspace_dir,
                        target_branch=target_branch,
                        commit_sha=commit_sha,
                        instance_id=instance_id,
                        data_dir=data_dir,
                    )
                except Exception as exc:
                    origin_confirmed = False
                    push_detail = {
                        "origin_confirmation_error": type(exc).__name__,
                        "origin_confirmation_detail": str(exc)[:300],
                    }
                confirmation_state = _origin_confirmation_state(
                    origin_confirmed,
                    push_detail,
                    commit_sha=commit_sha,
                )
                if confirmation_state == "unknown":
                    await _mark_push_unconfirmed(
                        db._conn,
                        attempt_id=attempt_id,
                        approval_id=approval_id,
                        commit_sha=commit_sha,
                        target_branch=target_branch,
                        recovery_iteration=recovery_iteration,
                        detail={
                            "recovered_existing_record": True,
                            **push_detail,
                        },
                    )
                    return (
                        f"Commit `{commit_sha[:12]}` is recorded, but "
                        f"`origin/{target_branch}` confirmation is still "
                        "unavailable; it will be retried on bring-up."
                    )
                if confirmation_state == "negative":
                    push_text = await push_fn(
                        tool_input={
                            "workspace_dir": workspace_dir,
                            "target_branch": target_branch,
                            "approval_id": approval_id,
                            "return_structured": True,
                        },
                        instance_id=instance_id,
                        data_dir=data_dir,
                    )
                    push_message = _push_message(push_text)
                    structured_ok = _structured_push_ok(
                        push_text, commit_sha=commit_sha,
                    )
                    try:
                        origin_confirmed, confirmation_detail = (
                            await _origin_head_matches_commit(
                                git_ops=_git_ops,
                                workspace_dir=workspace_dir,
                                target_branch=target_branch,
                                commit_sha=commit_sha,
                                instance_id=instance_id,
                                data_dir=data_dir,
                            )
                        )
                    except Exception as exc:
                        origin_confirmed = False
                        confirmation_detail = {
                            "origin_confirmation_error": type(exc).__name__,
                            "origin_confirmation_detail": str(exc)[:300],
                        }
                    push_detail.update(confirmation_detail)
                    push_detail.update({
                        "structured_push_ok": structured_ok,
                        "push_reason": (
                            push_text.get("reason")
                            if isinstance(push_text, dict) else ""
                        ),
                    })
                    confirmation_state = _origin_confirmation_state(
                        origin_confirmed,
                        push_detail,
                        commit_sha=commit_sha,
                    )
                    push_result_state = _push_result_confirmation_state(
                        push_text,
                        commit_sha=commit_sha,
                    )
                    if confirmation_state != "confirmed":
                        push_negative = (
                            confirmation_state == "negative"
                            or push_result_state == "negative"
                            or not _push_confirmation_retryable(
                                push_text,
                                structured_ok=structured_ok,
                            )
                        )
                        if not push_negative:
                            await _mark_push_unconfirmed(
                                db._conn,
                                attempt_id=attempt_id,
                                approval_id=approval_id,
                                commit_sha=commit_sha,
                                target_branch=target_branch,
                                recovery_iteration=recovery_iteration,
                                detail={
                                    "recovered_existing_record": True,
                                    "push_result": push_message[:300],
                                    **push_detail,
                                },
                            )
                            return (
                                f"Commit `{commit_sha[:12]}` is recorded, "
                                f"but `origin/{target_branch}` confirmation "
                                "is still unavailable; it will be retried "
                                "on bring-up."
                            )
                        await _mark_attempt_failed(
                            db._conn,
                            attempt_id=attempt_id,
                            reason="push_unconfirmed",
                            event_kind="push_failed",
                            detail={
                                "approval_id": approval_id,
                                "commit_sha": commit_sha,
                                "target_branch": target_branch,
                                "push_result": push_message[:300],
                                **push_detail,
                            },
                        )
                        return (
                            f"Commit `{commit_sha[:12]}` was recorded, "
                            f"but push did not confirm on "
                            f"`origin/{target_branch}`: {push_message}"
                        )
                else:
                    push_detail["origin_already_confirmed"] = True
            if not commit_recorded:
                await _ledger.append_event(
                    db._conn, attempt_id=attempt_id,
                    kind="commit_recorded",
                    detail=_detail({
                        "approval_id": approval_id,
                        "commit_sha": commit_sha,
                        "parent_sha": existing_commit.get("parent_sha") or "",
                        "recovered_existing_record": True,
                    }),
                )
            if not push_recorded:
                await _ledger.append_event(
                    db._conn, attempt_id=attempt_id,
                    kind="push_succeeded",
                    detail=_detail({
                        "approval_id": approval_id,
                        "commit_sha": commit_sha,
                        "target_branch": target_branch,
                        "recovered_existing_record": True,
                        **push_detail,
                    }),
                )
            await _ledger.update_attempt(
                db._conn, attempt_id=attempt_id,
                final_state="awaiting_post_restart_test",
            )
        else:
            recovered_commit_detail: dict[str, Any] = {}
            receipt_after_commit = await _approvals.get_receipt(
                data_dir=data_dir, approval_id=approval_id,
            )
            outcome = _loads_detail(
                (receipt_after_commit or {}).get("outcome_payload_json") or "{}"
            )
            commit_sha = str(outcome.get("commit_sha") or "")
            if commit_sha:
                current_head = await _head_sha(workspace_dir)
                if current_head != commit_sha:
                    return (
                        f"Approval `{approval_id}` says commit "
                        f"`{commit_sha[:12]}` already exists, but the "
                        f"worktree HEAD is `{current_head[:12]}`."
                    )
            else:
                files = await _staged_files(workspace_dir)
                if not files:
                    files = await _changed_files(workspace_dir)
                if not files:
                    commit_sha, recovered_commit_detail = (
                        await _recover_unrecorded_commit_if_safe(
                            data_dir=data_dir,
                            approval_id=approval_id,
                            workspace_dir=workspace_dir,
                            expected_parent=expected_parent,
                            expected_diff_hash=str(
                                binding.get("expected_diff_hash") or ""
                            ),
                        )
                    )
                    if not commit_sha:
                        await _mark_attempt_failed(
                            db._conn,
                            attempt_id=attempt_id,
                            reason="commit_unrecoverable",
                            event_kind="attempt_failed",
                            detail={
                                "approval_id": approval_id,
                                **recovered_commit_detail,
                            },
                        )
                        reason = recovered_commit_detail.get(
                            "recovery_reason", "unknown",
                        )
                        return (
                            f"Improvement attempt `{attempt_id}` has no "
                            "staged files to commit and no approved commit "
                            f"could be recovered ({reason})."
                        )
                if not commit_sha:
                    commit_text = await commit_fn(
                        tool_input={
                            "workspace_dir": workspace_dir,
                            "message": str(binding.get("summary") or attempt_id),
                            "approval_id": approval_id,
                            "files": files,
                        },
                        instance_id=instance_id,
                        data_dir=data_dir,
                    )
                    receipt_after_commit = await _approvals.get_receipt(
                        data_dir=data_dir, approval_id=approval_id,
                    )
                    outcome = _loads_detail(
                        (receipt_after_commit or {}).get("outcome_payload_json")
                        or "{}"
                    )
                    commit_sha = (
                        str(outcome.get("commit_sha") or "")
                        or _extract_commit_sha(str(commit_text))
                    )
                    if not commit_sha:
                        commit_sha, recovered_commit_detail = (
                            await _recover_unrecorded_commit_if_safe(
                                data_dir=data_dir,
                                approval_id=approval_id,
                                workspace_dir=workspace_dir,
                                expected_parent=expected_parent,
                                expected_diff_hash=str(
                                    binding.get("expected_diff_hash") or ""
                                ),
                            )
                        )
                    if not commit_sha:
                        await _mark_attempt_failed(
                            db._conn,
                            attempt_id=attempt_id,
                            reason="commit_refused",
                            event_kind="attempt_failed",
                            detail={
                                "approval_id": approval_id,
                                **recovered_commit_detail,
                            },
                        )
                        reason = recovered_commit_detail.get(
                            "recovery_reason", "unknown",
                        )
                        return (
                            f"Commit callback did not create a commit for "
                            f"`{attempt_id}` ({reason}); no push was attempted."
                        )
        if not commit_sha:
            return (
                f"Commit callback did not produce a commit SHA for "
                f"`{attempt_id}`."
            )

        if not existing_commit:
            push_text = await push_fn(
                tool_input={
                    "workspace_dir": workspace_dir,
                    "target_branch": target_branch,
                    "approval_id": approval_id,
                    "return_structured": True,
                },
                instance_id=instance_id,
                data_dir=data_dir,
            )
            push_message = _push_message(push_text)
            structured_ok = _structured_push_ok(push_text, commit_sha=commit_sha)
            try:
                origin_confirmed, confirmation_detail = await _origin_head_matches_commit(
                    git_ops=_git_ops,
                    workspace_dir=workspace_dir,
                    target_branch=target_branch,
                    commit_sha=commit_sha,
                    instance_id=instance_id,
                    data_dir=data_dir,
                )
            except Exception as exc:
                origin_confirmed = False
                confirmation_detail = {
                    "origin_confirmation_error": type(exc).__name__,
                    "origin_confirmation_detail": str(exc)[:300],
                }
            confirmation_detail.update({
                "structured_push_ok": structured_ok,
                "push_reason": (
                    push_text.get("reason") if isinstance(push_text, dict)
                    else ""
                ),
            })
            confirmation_state = _origin_confirmation_state(
                origin_confirmed,
                confirmation_detail,
                commit_sha=commit_sha,
            )
            push_result_state = _push_result_confirmation_state(
                push_text,
                commit_sha=commit_sha,
            )
            push_confirmed = confirmation_state == "confirmed"
            if not push_confirmed:
                push_negative = (
                    confirmation_state == "negative"
                    or push_result_state == "negative"
                    or not _push_confirmation_retryable(
                        push_text,
                        structured_ok=structured_ok,
                    )
                )
                if not push_negative:
                    recovery_trigger = (
                        "post_restart_self_test_failed"
                        if recovery_iteration is not None else ""
                    )
                    await _ledger.record_commit(
                        db._conn,
                        attempt_id=attempt_id,
                        commit_sha=commit_sha,
                        parent_sha=expected_parent,
                        approval_id=approval_id,
                        recovery_trigger=recovery_trigger,
                    )
                    event_payload = {
                        "approval_id": approval_id,
                        "commit_sha": commit_sha,
                        "parent_sha": expected_parent,
                    }
                    if recovered_commit_detail.get("recovered_unrecorded_commit"):
                        event_payload["recovered_unrecorded_commit"] = True
                    if recovery_iteration is not None:
                        event_payload["recovery_iteration"] = recovery_iteration
                    await _ledger.append_event(
                        db._conn, attempt_id=attempt_id,
                        kind="commit_recorded",
                        detail=_detail(event_payload),
                    )
                    await _mark_push_unconfirmed(
                        db._conn,
                        attempt_id=attempt_id,
                        approval_id=approval_id,
                        commit_sha=commit_sha,
                        target_branch=target_branch,
                        recovery_iteration=recovery_iteration,
                        detail={
                            "push_result": push_message[:300],
                            **confirmation_detail,
                        },
                    )
                    return (
                        f"Commit `{commit_sha[:12]}` was created, but "
                        f"`origin/{target_branch}` confirmation is still "
                        "unavailable; it will be retried on bring-up."
                    )
                await _mark_attempt_failed(
                    db._conn,
                    attempt_id=attempt_id,
                    reason="push_unconfirmed",
                    event_kind="push_failed",
                    detail={
                        "approval_id": approval_id,
                        "commit_sha": commit_sha,
                        "target_branch": target_branch,
                        "push_result": push_message[:300],
                        **confirmation_detail,
                    },
                )
                return (
                    f"Commit `{commit_sha[:12]}` was created, but push did "
                    f"not confirm on `origin/{target_branch}`: {push_message}"
                )

            recovery_trigger = (
                "post_restart_self_test_failed"
                if recovery_iteration is not None else ""
            )
            await _ledger.record_commit(
                db._conn,
                attempt_id=attempt_id,
                commit_sha=commit_sha,
                parent_sha=expected_parent,
                approval_id=approval_id,
                recovery_trigger=recovery_trigger,
            )
            event_payload = {
                "approval_id": approval_id,
                "commit_sha": commit_sha,
                "parent_sha": expected_parent,
            }
            if recovered_commit_detail.get("recovered_unrecorded_commit"):
                event_payload["recovered_unrecorded_commit"] = True
            if recovery_iteration is not None:
                event_payload["recovery_iteration"] = recovery_iteration
            await _ledger.append_event(
                db._conn, attempt_id=attempt_id,
                kind="commit_recorded",
                detail=_detail(event_payload),
            )
            await _ledger.append_event(
                db._conn, attempt_id=attempt_id,
                kind="push_succeeded",
                detail=_detail({
                    "approval_id": approval_id,
                    "commit_sha": commit_sha,
                    "target_branch": target_branch,
                }),
            )
            await _ledger.update_attempt(
                db._conn, attempt_id=attempt_id,
                final_state="awaiting_post_restart_test",
            )
    finally:
        await db.close()

    if restart_fn is not None:
        await _maybe_await(restart_fn())
    return (
        f"Improvement attempt `{attempt_id}` committed `{commit_sha[:12]}`, "
        f"pushed to `{target_branch}`, and is awaiting post-restart tests."
    )


async def _close_cap_hit(
    conn: Any, *, attempt_id: str, recovery_iterations_used: int,
) -> None:
    from kernos.kernel import improvement_ledger as _ledger
    from kernos.utils import utc_now

    await _ledger.update_attempt(
        conn, attempt_id=attempt_id,
        final_state="test_failed_unrecovered",
        ended_at=utc_now(),
    )
    await _ledger.append_event(
        conn, attempt_id=attempt_id,
        kind="recovery_cap_hit",
        detail=_detail({
            "attempt_id": attempt_id,
            "recovery_iterations_used": recovery_iterations_used,
        }),
    )


async def _surface_recovery_decision(
    conn: Any,
    *,
    attempt: dict,
    failure_summary: str,
    wake_fn: Any = None,
) -> None:
    from kernos.kernel import improvement_ledger as _ledger

    attempt_id = attempt["attempt_id"]
    used = await _recovery_started_count(conn, attempt_id)
    if attempt.get("first_pass_green") is None:
        await _ledger.update_attempt(
            conn, attempt_id=attempt_id, first_pass_green=0,
        )
    if used >= 2:
        await _close_cap_hit(
            conn, attempt_id=attempt_id,
            recovery_iterations_used=used,
        )
        return

    failed_test_ids = _failed_test_ids_from_summary(failure_summary)
    await _ledger.update_attempt(
        conn, attempt_id=attempt_id,
        final_state="awaiting_recovery_decision",
    )
    detail = {
        "attempt_id": attempt_id,
        "failure_summary": failure_summary,
        "failed_test_ids": failed_test_ids,
        "worktree_path": attempt.get("worktree_path") or "",
        "recovery_iterations_used": used,
    }
    await _ledger.append_event(
        conn, attempt_id=attempt_id,
        kind="recovery_decision_requested",
        detail=_detail(detail),
    )
    origin = await _attempt_origin(conn, attempt_id)
    if wake_fn is not None and origin.get("origin_space_id"):
        await _maybe_await(wake_fn({
            "instance_id": attempt.get("instance_id") or "",
            "originating_space": origin.get("origin_space_id") or "",
            "originating_member_id": origin.get("origin_member_id") or "",
            **detail,
        }))


_RECOVERY_APPROVAL_WAIT_STATES = frozenset({
    "recovery_in_progress",
    "awaiting_recovery_commit_approval",
    _PUSH_UNCONFIRMED_STATE,
})
_INITIAL_APPROVAL_WAIT_STATES = frozenset({
    "",
    "awaiting_commit_approval",
    _PUSH_UNCONFIRMED_STATE,
})


async def handle_improvement_commit_approval_terminal_decision(
    *,
    data_dir: str,
    approval_id: str,
) -> str:
    """Apply rejected/expired improvement commit-approval outcomes.

    The approval substrate owns receipt state. The improvement loop owns
    the attempt state that must move out of the approval wait.
    This handler is idempotent: once the attempt leaves the wait state,
    repeat calls return an empty string.
    """
    from kernos.kernel import approval_receipts as _approvals
    from kernos.kernel import improvement_ledger as _ledger
    from kernos.kernel.instance_db import InstanceDB
    from kernos.utils import utc_now

    receipt = await _approvals.get_receipt(
        data_dir=data_dir, approval_id=approval_id,
    )
    if receipt is None:
        return ""
    if receipt.get("kind") != "git_commit_authorization":
        return ""
    decision = str(receipt.get("state") or "")
    if decision not in {"rejected", "expired"}:
        return ""

    binding = _loads_detail(receipt.get("binding_payload_json") or "{}")
    attempt_id = str(binding.get("attempt_id") or "")
    recovery_iteration = binding.get("recovery_iteration")
    if not attempt_id:
        return ""
    is_recovery = recovery_iteration is not None

    db = InstanceDB(data_dir)
    await db.connect()
    try:
        attempt = await _ledger.get_attempt(db._conn, attempt_id)
        if attempt is None:
            return ""
        if attempt.get("instance_id") != receipt.get("instance_id"):
            return ""
        state = str(attempt.get("final_state") or "")
        wait_states = (
            _RECOVERY_APPROVAL_WAIT_STATES
            if is_recovery else _INITIAL_APPROVAL_WAIT_STATES
        )
        if state not in wait_states:
            return ""

        if not is_recovery:
            event_kind = f"attempt_{decision}_at_commit"
            await _ledger.append_event(
                db._conn,
                attempt_id=attempt_id,
                kind=event_kind,
                detail=_detail({
                    "approval_id": approval_id,
                    "decision": decision,
                    "reason": receipt.get("state_reason") or "",
                }),
            )
            await _ledger.update_attempt(
                db._conn,
                attempt_id=attempt_id,
                final_state=event_kind,
                ended_at=utc_now(),
            )
            return (
                f"Initial commit approval `{approval_id}` was {decision}; "
                f"attempt `{attempt_id}` was marked `{event_kind}`."
            )

        event_kind = f"recovery_commit_approval_{decision}"
        await _ledger.append_event(
            db._conn,
            attempt_id=attempt_id,
            kind=event_kind,
            detail=_detail({
                "approval_id": approval_id,
                "decision": decision,
                "reason": receipt.get("state_reason") or "",
                "recovery_iteration": recovery_iteration,
            }),
        )
        used = await _recovery_started_count(db._conn, attempt_id)
        if used >= 2:
            await _close_cap_hit(
                db._conn,
                attempt_id=attempt_id,
                recovery_iterations_used=used,
            )
            return (
                f"Recovery commit approval `{approval_id}` was {decision}; "
                f"attempt `{attempt_id}` has spent the two recovery "
                "iterations and was marked unrecovered."
            )

        await _ledger.update_attempt(
            db._conn,
            attempt_id=attempt_id,
            final_state="awaiting_recovery_decision",
        )
        return (
            f"Recovery commit approval `{approval_id}` was {decision}; "
            f"attempt `{attempt_id}` is back in "
            "`awaiting_recovery_decision` for retry or abandon."
        )
    finally:
        await db.close()


async def process_terminal_improvement_approval_decisions(
    *,
    data_dir: str,
    instance_id: str = "",
) -> int:
    """Sweep terminal commit receipts for missed improvement callbacks."""
    import aiosqlite
    from pathlib import Path

    db_path = Path(data_dir) / "instance.db"
    if not db_path.exists():
        return 0
    sql = (
        "SELECT approval_id FROM approval_receipts "
        "WHERE kind=? AND state IN ('rejected','expired')"
    )
    params: list[Any] = ["git_commit_authorization"]
    if instance_id:
        sql += " AND instance_id=?"
        params.append(instance_id)
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = None
        async with conn.execute(sql, params) as cur:
            rows = await cur.fetchall()

    processed = 0
    for (terminal_approval_id,) in rows:
        message = await handle_improvement_commit_approval_terminal_decision(
            data_dir=data_dir,
            approval_id=terminal_approval_id,
        )
        if message:
            processed += 1
    return processed


async def process_approved_improvement_commit_continuations(
    *,
    data_dir: str,
    instance_id: str = "",
    restart_fn: Any = None,
    git_commit_fn: Any = None,
    git_push_fn: Any = None,
) -> int:
    """Resume approved improvement commit receipts after an interrupted
    /approve callback.

    Only receipts whose bound attempts are still waiting for commit
    continuation are eligible. Repeated calls are idempotent because
    continuation moves the attempt out of the wait state before restart.
    """
    import aiosqlite
    from pathlib import Path
    from kernos.kernel.instance_db import InstanceDB
    from kernos.kernel import improvement_ledger as _ledger

    db_path = Path(data_dir) / "instance.db"
    if not db_path.exists():
        return 0
    sql = (
        "SELECT approval_id, instance_id, binding_payload_json "
        "FROM approval_receipts "
        "WHERE kind=? AND state='approved'"
    )
    params: list[Any] = ["git_commit_authorization"]
    if instance_id:
        sql += " AND instance_id=?"
        params.append(instance_id)
    sql += " ORDER BY decided_at ASC, requested_at ASC"
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(sql, params) as cur:
            receipt_rows = [dict(r) for r in await cur.fetchall()]

    eligible: list[tuple[str, str]] = []
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        for receipt in receipt_rows:
            binding = _loads_detail(receipt.get("binding_payload_json") or "{}")
            attempt_id = str(binding.get("attempt_id") or "")
            if not attempt_id:
                continue
            attempt = await _ledger.get_attempt(db._conn, attempt_id)
            if attempt is None:
                continue
            if attempt.get("instance_id") != receipt.get("instance_id"):
                continue
            recovery_iteration = binding.get("recovery_iteration")
            wait_states = (
                _RECOVERY_APPROVAL_WAIT_STATES
                if recovery_iteration is not None
                else _INITIAL_APPROVAL_WAIT_STATES
            )
            attempt_state = str(attempt.get("final_state") or "")
            if attempt_state not in wait_states:
                continue
            if attempt_state == _PUSH_UNCONFIRMED_STATE and not (
                await _active_push_unconfirmed_approval_matches(
                    db._conn,
                    attempt_id=attempt_id,
                    approval_id=str(receipt.get("approval_id") or ""),
                    binding=binding,
                )
            ):
                continue
            eligible.append((
                str(receipt.get("approval_id") or ""),
                str(receipt.get("instance_id") or ""),
            ))
    finally:
        await db.close()

    processed = 0
    for approval_id, receipt_instance_id in eligible:
        if not approval_id:
            continue
        message = await continue_approved_improvement_commit(
            data_dir=data_dir,
            instance_id=instance_id or receipt_instance_id,
            approval_id=approval_id,
            restart_fn=restart_fn,
            git_commit_fn=git_commit_fn,
            git_push_fn=git_push_fn,
        )
        if "skipped" not in message:
            processed += 1
    return processed


_RECOVERY_IN_PROGRESS_FOLLOW_ON_EVENTS = frozenset({
    "attempt_failed",
    "workspace_create_failed",
    "aborted_unconverged",
    "completed",
    "live_head_mismatch",
    "attempt_rejected_at_commit",
    "attempt_expired_at_commit",
    "test_failed_unrecovered",
    "test_failed_abandoned_by_agent",
    "recovery_cap_hit",
})


def _blocks_recovery_in_progress_reset(kind: str) -> bool:
    return (
        kind in _RECOVERY_IN_PROGRESS_FOLLOW_ON_EVENTS
        or kind.startswith("recovery_aborted")
    )


def _coerce_recovery_iteration(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _recovery_commit_receipt_for_iteration(
    conn: Any,
    *,
    attempt_id: str,
    recovery_iteration: int | None,
) -> dict[str, Any] | None:
    if recovery_iteration is None:
        return None
    conn.row_factory = None
    async with conn.execute(
        "SELECT approval_id, state FROM approval_receipts "
        "WHERE kind=? AND state IN ('pending','approved') "
        "AND json_extract(binding_payload_json, '$.attempt_id')=? "
        "AND json_extract(binding_payload_json, '$.recovery_iteration')=? "
        "ORDER BY requested_at DESC LIMIT 1",
        (
            "git_commit_authorization",
            attempt_id,
            recovery_iteration,
        ),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return {"approval_id": row[0], "state": row[1]}


async def reconcile_stale_recovery_in_progress_attempts(
    *,
    data_dir: str,
    instance_id: str = "",
) -> int:
    """Reset recovery consults interrupted before a durable follow-on.

    ``proceed_with_recovery_service`` first records
    ``recovery_in_progress`` + ``recovery_started``, then calls the
    external coding-agent consult. A process crash during that consult
    leaves no approval, abort, or terminal marker for another reconciler
    to pick up. This pass returns those attempts to the operator's
    recovery decision state, except when the interrupted start spent the
    second recovery iteration.
    """
    from pathlib import Path
    from kernos.kernel import improvement_ledger as _ledger
    from kernos.kernel.instance_db import InstanceDB

    db_path = Path(data_dir) / "instance.db"
    if not db_path.exists():
        return 0

    db = InstanceDB(data_dir)
    await db.connect()
    try:
        db._conn.row_factory = None
        sql = (
            "SELECT attempt_id FROM improvement_attempts "
            "WHERE final_state=?"
        )
        params: list[Any] = ["recovery_in_progress"]
        if instance_id:
            sql += " AND instance_id=?"
            params.append(instance_id)
        sql += " ORDER BY started_at ASC"
        async with db._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()

        processed = 0
        for (attempt_id,) in rows:
            attempt = await _ledger.get_attempt(db._conn, attempt_id)
            if not attempt:
                continue
            events = await _ledger.get_attempt_events(db._conn, attempt_id)
            recovery_started = [
                e for e in events if e.get("kind") == "recovery_started"
            ]
            latest_started = recovery_started[-1] if recovery_started else {}
            latest_started_seq = int(latest_started.get("sequence") or 0)
            detail = _loads_detail(latest_started.get("detail") or "{}")
            used = len(recovery_started)
            iteration = _coerce_recovery_iteration(detail.get("iteration"))
            if iteration is None and used:
                iteration = used
            receipt = await _recovery_commit_receipt_for_iteration(
                db._conn,
                attempt_id=attempt_id,
                recovery_iteration=iteration,
            )
            if receipt is not None:
                approval_id = str(receipt.get("approval_id") or "")
                approval_event_exists = any(
                    e.get("kind") == "approval_requested"
                    and approval_id in str(e.get("detail") or "")
                    for e in events
                )
                if not approval_event_exists:
                    await _ledger.append_event(
                        db._conn,
                        attempt_id=attempt_id,
                        kind="approval_requested",
                        detail=(
                            f"approval_id={approval_id} "
                            f"recovery_iteration={iteration} "
                            "recovered_after_crash=true"
                        ),
                    )
                await _ledger.update_attempt(
                    db._conn,
                    attempt_id=attempt_id,
                    final_state="awaiting_recovery_commit_approval",
                )
                processed += 1
                continue
            if any(
                int(e.get("sequence") or 0) > latest_started_seq
                and _blocks_recovery_in_progress_reset(
                    str(e.get("kind") or ""),
                )
                for e in events
            ):
                continue
            reset_detail = {
                "reason": "recovery_in_progress_without_follow_on",
                "recovery_iterations_used": used,
            }
            if iteration is not None:
                reset_detail["iteration"] = iteration
            await _ledger.append_event(
                db._conn,
                attempt_id=attempt_id,
                kind="recovery_reset_after_crash",
                detail=_detail(reset_detail),
            )
            if used >= 2:
                await _close_cap_hit(
                    db._conn,
                    attempt_id=attempt_id,
                    recovery_iterations_used=used,
                )
            else:
                await _ledger.update_attempt(
                    db._conn,
                    attempt_id=attempt_id,
                    final_state="awaiting_recovery_decision",
                )
            processed += 1
        return processed
    finally:
        await db.close()


async def run_pending_post_restart_tests(
    *,
    data_dir: str,
    instance_id: str,
    wake_fn: Any = None,
    self_test_fn: Any = None,
    live_repo_dir: str = "",
    live_head_fn: Any = None,
) -> int:
    """Bring-up continuation for commits awaiting post-restart tests."""
    from kernos.kernel import improvement_ledger as _ledger
    from kernos.kernel.instance_db import InstanceDB
    from kernos.kernel.self_test_gate import handle_run_self_test_suite
    from kernos.utils import utc_now

    db = InstanceDB(data_dir)
    await db.connect()
    processed = 0
    try:
        db._conn.row_factory = None
        async with db._conn.execute(
            "SELECT attempt_id FROM improvement_attempts "
            "WHERE instance_id=? AND final_state=? "
            "ORDER BY started_at ASC",
            (instance_id, "awaiting_post_restart_test"),
        ) as cur:
            rows = await cur.fetchall()
        attempt_ids = [r[0] for r in rows]
        test_fn = self_test_fn or handle_run_self_test_suite
        live_repo = live_repo_dir or _default_live_repo_dir()
        read_live_head = live_head_fn or _live_repo_head_sha
        for attempt_id in attempt_ids:
            attempt = await _ledger.get_attempt(db._conn, attempt_id)
            if not attempt:
                continue
            expected_live_head = str(attempt.get("final_commit_sha") or "")
            try:
                live_head_result = await read_live_head(live_repo)
            except TypeError:
                live_head_result = await read_live_head()
            if isinstance(live_head_result, tuple):
                live_head = str(live_head_result[0] or "")
                live_detail = dict(live_head_result[1] or {})
            else:
                live_head = str(live_head_result or "")
                live_detail = {"live_repo_dir": live_repo}
            if not expected_live_head or live_head != expected_live_head:
                await _ledger.update_attempt(
                    db._conn,
                    attempt_id=attempt_id,
                    final_state="live_head_mismatch",
                    ended_at=utc_now(),
                )
                await _ledger.append_event(
                    db._conn,
                    attempt_id=attempt_id,
                    kind="live_head_mismatch",
                    detail=_detail({
                        "expected_commit_sha": expected_live_head,
                        "live_head_sha": live_head,
                        **live_detail,
                    }),
                )
                processed += 1
                continue
            result = await test_fn(
                tool_input={
                    "workspace_dir": attempt.get("worktree_path") or "",
                    "attempt_id": attempt_id,
                    "include_soak": True,
                },
                instance_id=instance_id,
                data_dir=data_dir,
            )
            refreshed = await _ledger.get_attempt(db._conn, attempt_id)
            if not refreshed:
                continue
            outcome, summary = _outcome_from_self_test_result(
                result, refreshed,
            )
            if isinstance(result, dict) and outcome:
                await _ledger.update_attempt(
                    db._conn, attempt_id=attempt_id,
                    test_outcome=outcome,
                )
                refreshed["test_outcome"] = outcome
            if outcome == "pass":
                updates = {
                    "final_state": "completed",
                    "ended_at": utc_now(),
                }
                if refreshed.get("first_pass_green") is None:
                    updates["first_pass_green"] = 1
                await _ledger.update_attempt(
                    db._conn, attempt_id=attempt_id, **updates,
                )
            else:
                await _surface_recovery_decision(
                    db._conn,
                    attempt=refreshed,
                    failure_summary=summary,
                    wake_fn=wake_fn,
                )
            processed += 1
    finally:
        await db.close()
    return processed


def _recovery_prompt(
    *,
    attempt: dict,
    failure_summary: str,
    failed_test_ids: list[str],
) -> str:
    failed = ", ".join(failed_test_ids) if failed_test_ids else "(see summary)"
    return (
        "An autonomous Kernos improvement was committed and pushed, "
        "but the post-restart self-test failed.\n\n"
        f"Attempt: {attempt['attempt_id']}\n"
        f"Spec requirement: {attempt.get('spec_requirement') or ''}\n"
        f"Worktree: {attempt.get('worktree_path') or ''}\n"
        f"Failed tests: {failed}\n"
        f"Failure summary:\n{failure_summary}\n\n"
        "Edit the existing worktree in place. Keep the fix bounded to "
        "the failure. End with exactly one status line: "
        "STATUS: GREEN if the worktree is ready for approval, or "
        "STATUS: NEEDS_REVISION <reason> if you could not converge."
    )


async def proceed_with_recovery_service(
    *,
    data_dir: str,
    instance_id: str,
    attempt_id: str,
    consult_fn: Any,
    receipts_event_stream: Any = None,
    operator_override: bool = False,
) -> str:
    from kernos.kernel import improvement_ledger as _ledger
    from kernos.kernel.improvement_review_protocol import detect_status
    from kernos.kernel.instance_db import InstanceDB
    from kernos.utils import utc_now

    attempt_id = (attempt_id or "").strip()
    if not attempt_id:
        return "`attempt_id` is required."
    if consult_fn is None or not callable(consult_fn):
        return "Recovery consult function is not available."

    db = InstanceDB(data_dir)
    await db.connect()
    try:
        attempt = await _ledger.get_attempt(db._conn, attempt_id)
        if attempt is None or attempt.get("instance_id") != instance_id:
            return f"No improvement attempt `{attempt_id}` found."
        if attempt.get("final_state") != "awaiting_recovery_decision":
            return (
                f"Attempt `{attempt_id}` is "
                f"`{attempt.get('final_state') or 'running'}`, not "
                f"`awaiting_recovery_decision`."
            )
        used = await _recovery_started_count(db._conn, attempt_id)
        if used >= 2:
            await _close_cap_hit(
                db._conn, attempt_id=attempt_id,
                recovery_iterations_used=used,
            )
            return (
                f"Attempt `{attempt_id}` already used the two recovery "
                f"iterations; marked unrecovered."
            )
        iteration = used + 1
        if operator_override:
            await _ledger.append_event(
                db._conn, attempt_id=attempt_id,
                kind="operator_recovery_override",
                detail=_detail({
                    "action": "proceed_with_recovery",
                    "iteration": iteration,
                }),
            )
        await _ledger.update_attempt(
            db._conn, attempt_id=attempt_id,
            final_state="recovery_in_progress",
        )
        await _ledger.append_event(
            db._conn, attempt_id=attempt_id,
            kind="recovery_started",
            detail=_detail({
                "iteration": iteration,
                "trigger": "post_restart_self_test_failed",
            }),
        )
        decision_detail = _loads_detail(
            await _latest_event_detail(
                db._conn, attempt_id, "recovery_decision_requested",
            )
        )
        failure_summary = str(decision_detail.get("failure_summary") or "")
        failed_test_ids = list(decision_detail.get("failed_test_ids") or [])
        agent = attempt.get("primary_coding_agent") or "claude_code"

        async def _record_recovery_failure(exc: BaseException) -> str:
            await _ledger.append_event(
                db._conn,
                attempt_id=attempt_id,
                kind="recovery_failed",
                detail=_detail({
                    "iteration": iteration,
                    "agent": agent,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                }),
            )
            if iteration >= 2:
                await _close_cap_hit(
                    db._conn,
                    attempt_id=attempt_id,
                    recovery_iterations_used=iteration,
                )
                return (
                    f"Recovery iteration {iteration} failed and the "
                    f"two-iteration cap is spent; attempt `{attempt_id}` "
                    "was marked unrecovered."
                )
            await _ledger.update_attempt(
                db._conn,
                attempt_id=attempt_id,
                final_state="awaiting_recovery_decision",
            )
            return (
                f"Recovery iteration {iteration} failed before approval; "
                f"attempt `{attempt_id}` is back in "
                "`awaiting_recovery_decision` for retry or abandon."
            )

        try:
            worktree_path = attempt.get("worktree_path") or ""
            recovery_text = await _call_consult_fn(
                consult_fn,
                target=agent,
                prompt=_recovery_prompt(
                    attempt=attempt,
                    failure_summary=failure_summary,
                    failed_test_ids=failed_test_ids,
                ),
                instance_id=instance_id,
                workspace_dir=worktree_path,
            )
            status, findings = detect_status(recovery_text)
            await _ledger.append_event(
                db._conn, attempt_id=attempt_id,
                kind="recovery_iteration",
                detail=_detail({
                    "iteration": iteration,
                    "agent": agent,
                    "outcome": status,
                    "summary": findings or recovery_text[:300],
                }),
            )
            if status != "GREEN":
                await _ledger.update_attempt(
                    db._conn, attempt_id=attempt_id,
                    final_state="test_failed_unrecovered",
                    ended_at=utc_now(),
                )
                await _ledger.append_event(
                    db._conn, attempt_id=attempt_id,
                    kind="recovery_aborted_unconverged",
                    detail=_detail({
                        "iteration": iteration,
                        "outcome": status,
                        "reason": findings,
                    }),
                )
                return (
                    f"Recovery iteration {iteration} did not converge; "
                    f"attempt `{attempt_id}` marked unrecovered."
                )

            has_diff = await _worktree_has_diff(worktree_path)
            if not has_diff:
                await _ledger.append_event(
                    db._conn,
                    attempt_id=attempt_id,
                    kind="recovery_no_diff",
                    detail=_detail({
                        "iteration": iteration,
                        "workspace_dir": worktree_path,
                    }),
                )
                if iteration >= 2:
                    await _close_cap_hit(
                        db._conn,
                        attempt_id=attempt_id,
                        recovery_iterations_used=iteration,
                    )
                    return (
                        f"Recovery iteration {iteration} produced GREEN but "
                        f"no worktree diff; the two-iteration cap is spent "
                        f"and attempt `{attempt_id}` was marked unrecovered."
                    )
                await _ledger.update_attempt(
                    db._conn,
                    attempt_id=attempt_id,
                    final_state="awaiting_recovery_decision",
                )
                return (
                    f"Recovery iteration {iteration} produced GREEN but no "
                    f"worktree diff; attempt `{attempt_id}` is back in "
                    "`awaiting_recovery_decision`."
                )

            await _request_commit_approval_for_attempt(
                db=db,
                data_dir=data_dir,
                instance_id=instance_id,
                attempt_id=attempt_id,
                worktree_path=worktree_path,
                receipts_event_stream=receipts_event_stream,
                recovery_iteration=iteration,
            )
            await _ledger.update_attempt(
                db._conn,
                attempt_id=attempt_id,
                final_state="awaiting_recovery_commit_approval",
            )
            return (
                f"Recovery iteration {iteration} produced GREEN for "
                f"`{attempt_id}`. I requested operator commit approval."
            )
        except asyncio.CancelledError as exc:
            return await _record_recovery_failure(exc)
        except Exception as exc:
            return await _record_recovery_failure(exc)
    finally:
        await db.close()


async def abandon_attempt_service(
    *,
    data_dir: str,
    instance_id: str,
    attempt_id: str,
    reason: str,
    operator_override: bool = False,
) -> str:
    from kernos.kernel import improvement_ledger as _ledger
    from kernos.kernel.instance_db import InstanceDB
    from kernos.utils import utc_now

    attempt_id = (attempt_id or "").strip()
    reason = (reason or "").strip()
    if not attempt_id:
        return "`attempt_id` is required."
    if not reason:
        return "`reason` is required."

    db = InstanceDB(data_dir)
    await db.connect()
    try:
        attempt = await _ledger.get_attempt(db._conn, attempt_id)
        if attempt is None or attempt.get("instance_id") != instance_id:
            return f"No improvement attempt `{attempt_id}` found."
        if attempt.get("final_state") != "awaiting_recovery_decision":
            return (
                f"Attempt `{attempt_id}` is "
                f"`{attempt.get('final_state') or 'running'}`, not "
                f"`awaiting_recovery_decision`."
            )
        if operator_override:
            await _ledger.append_event(
                db._conn, attempt_id=attempt_id,
                kind="operator_recovery_override",
                detail=_detail({
                    "action": "abandon_attempt",
                    "reason": reason,
                }),
            )
        await _ledger.update_attempt(
            db._conn, attempt_id=attempt_id,
            final_state="test_failed_abandoned_by_agent",
            ended_at=utc_now(),
        )
        await _ledger.append_event(
            db._conn, attempt_id=attempt_id,
            kind="test_failed_abandoned_by_agent",
            detail=_detail({"reason": reason}),
        )
        return f"Attempt `{attempt_id}` abandoned: {reason}"
    finally:
        await db.close()


async def recovery_tools_visible_for_space(
    *,
    data_dir: str,
    instance_id: str,
    active_space_id: str,
) -> bool:
    """Return whether the active turn owns a recovery decision."""
    if not (instance_id and active_space_id):
        return False
    import aiosqlite
    from pathlib import Path

    db_path = Path(data_dir) / "instance.db"
    if not db_path.exists():
        return False
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = None
        async with conn.execute(
            "SELECT e.detail FROM improvement_attempts a "
            "JOIN improvement_attempt_events e "
            "  ON e.attempt_id = a.attempt_id "
            "WHERE a.instance_id=? "
            "  AND a.final_state=? "
            "  AND e.kind=?",
            (
                instance_id,
                "awaiting_recovery_decision",
                "attempt_origin",
            ),
        ) as cur:
            rows = await cur.fetchall()
    for (detail_text,) in rows:
        origin = _loads_detail(detail_text)
        if origin.get("origin_space_id") == active_space_id:
            return True
    return False


# ---------------------------------------------------------------------
# improve_kernos tool handler
# ---------------------------------------------------------------------


async def handle_improve_kernos(
    *, handler: Any, tool_input: dict, instance_id: str,
    data_dir: str, origin_space_id: str = "", origin_member_id: str = "",
) -> str:
    """Dispatch entry point. Builds an orchestrator + starts
    the attempt + returns prose containing the attempt_id."""
    spec_requirement = (tool_input.get("spec_requirement") or "").strip()
    if not spec_requirement:
        return (
            "`spec_requirement` is required — describe what you "
            "want me to improve about myself in natural language."
        )
    consult_fn = getattr(handler, "_consult_fn_for_loop", None)
    if consult_fn is None or not callable(consult_fn):
        return (
            "Improvement loop is unavailable: production consult seam "
            "`_consult_fn_for_loop` is not wired, so no attempt was "
            "created."
        )
    restart_fn = getattr(handler, "_restart_fn_for_loop", None)
    primary = tool_input.get("primary_coding_agent") or "claude_code"
    reviewer = tool_input.get("reviewer_coding_agent") or "codex"

    # Resolve live_repo_dir from the running process. The Kernos
    # binary lives in <repo>/kernos/server.py; walk up from this
    # module's path.
    from pathlib import Path
    import kernos
    kernos_pkg_dir = Path(kernos.__file__).resolve().parent
    live_repo_dir = str(kernos_pkg_dir.parent)

    async def _notify_terminal(attempt_id: str, final_state: str) -> None:
        """When the background attempt finishes (approval gate OR abort),
        queue a whisper to the originating member so the agent proactively
        tells the user on their next turn — in its own voice, with the
        conversation in context — instead of going silent. The whisper is
        agent-facing FRAMING, not a canned user message."""
        state = getattr(handler, "state", None)
        if state is None or not origin_member_id:
            return
        _msgs = {
            "awaiting_commit_approval": (
                f"The self-improvement the user asked for (`{attempt_id}`) "
                f"finished drafting + review and is WAITING AT THE APPROVAL "
                f"GATE — a proposed change is ready to show them before "
                f"anything commits or goes live. Bring it up and offer to "
                f"walk them through the diff and get their go/no-go."
            ),
            "aborted_unconverged": (
                f"The self-improvement attempt (`{attempt_id}`) used its "
                f"full review budget but the author and reviewer couldn't "
                f"agree on a change confident enough to ship, so it STOPPED "
                f"WITHOUT committing anything. Tell the user it didn't land "
                f"+ briefly why, and offer to retry or narrow the scope."
            ),
            "aborted_consult_failure": (
                f"The self-improvement attempt (`{attempt_id}`) hit a "
                f"tooling failure partway and stopped before making any "
                f"changes — nothing was committed. Tell the user and offer "
                f"to retry."
            ),
        }
        _default = (
            f"The self-improvement attempt (`{attempt_id}`) finished in "
            f"state `{final_state}` without landing a committed change. "
            f"Tell the user where it ended up and offer a next step."
        )
        try:
            from datetime import datetime, timezone
            from kernos.kernel.awareness import Whisper, generate_whisper_id
            w = Whisper(
                whisper_id=generate_whisper_id(),
                insight_text=_msgs.get(final_state, _default),
                delivery_class="stage",
                source_space_id=origin_space_id,
                target_space_id=origin_space_id,
                supporting_evidence=f"attempt={attempt_id} state={final_state}",
                reasoning_trace=(
                    f"improve_kernos attempt {attempt_id} reached terminal "
                    f"state {final_state}; surfacing so the user isn't left "
                    f"silent after a background self-improvement run."
                ),
                knowledge_entry_id="",
                foresight_signal=f"improvement_terminal:{attempt_id}",
                created_at=datetime.now(timezone.utc).isoformat(),
                owner_member_id=origin_member_id,
            )
            await state.save_whisper(instance_id, w)
        except Exception as exc:
            logger.warning(
                "IMPROVE_KERNOS_NOTIFY_WHISPER_FAILED %s: %s",
                attempt_id, exc,
            )

    async def _announce_to_origin(space_id: str, message: str) -> None:
        """Try immediate outbound delivery; fall back to a stage whisper."""
        member_id = origin_member_id
        if not member_id:
            try:
                from kernos.kernel.scheduler import resolve_owner_member_id
                member_id = resolve_owner_member_id(instance_id)
            except Exception:
                member_id = "owner"

        send_outbound = getattr(handler, "send_outbound", None)
        if callable(send_outbound):
            try:
                sent = await send_outbound(
                    instance_id, member_id, None, message,
                )
                if sent:
                    return
            except Exception as exc:
                logger.warning(
                    "IMPROVE_KERNOS_ANNOUNCE_OUTBOUND_FAILED %s: %s",
                    space_id, exc,
                )

        state = getattr(handler, "state", None)
        target_space_id = space_id or origin_space_id
        if state is None or not member_id or not target_space_id:
            return
        try:
            from datetime import datetime, timezone
            from kernos.kernel.awareness import Whisper, generate_whisper_id
            w = Whisper(
                whisper_id=generate_whisper_id(),
                insight_text=(
                    "Background self-improvement update for the user:\n"
                    f"{message}\n"
                    "Tell the user this proactively in your own voice."
                ),
                delivery_class="stage",
                source_space_id=target_space_id,
                target_space_id=target_space_id,
                supporting_evidence="improve_kernos proactive update",
                reasoning_trace=(
                    "improve_kernos could not push directly through an "
                    "outbound channel, so it staged a whisper in the "
                    "origin space."
                ),
                knowledge_entry_id="",
                foresight_signal="improvement_announce",
                created_at=datetime.now(timezone.utc).isoformat(),
                owner_member_id=member_id,
            )
            await state.save_whisper(instance_id, w)
        except Exception as exc:
            logger.warning(
                "IMPROVE_KERNOS_ANNOUNCE_WHISPER_FAILED %s: %s",
                target_space_id, exc,
            )

    orchestrator = ImprovementLoopOrchestrator(
        instance_id=instance_id,
        data_dir=data_dir,
        live_repo_dir=live_repo_dir,
        consult_fn=consult_fn,
        restart_fn=restart_fn,
        notify_fn=_notify_terminal,
        announce_fn=_announce_to_origin,
        receipts_event_stream=getattr(handler, "events", None),
    )
    try:
        setattr(handler, "_last_improvement_orchestrator", orchestrator)
    except Exception:
        pass
    try:
        attempt_id = await orchestrator.start_attempt(
            spec_requirement=spec_requirement,
            primary_coding_agent=primary,
            reviewer_coding_agent=reviewer,
            origin_space_id=origin_space_id,
            origin_member_id=origin_member_id,
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
        f"through drafting + review, then tell you when it's live "
        f"or if it needs approval. Track progress via "
        f"`/improvement_status {attempt_id}`."
    )


async def handle_proceed_with_recovery(
    *, handler: Any, tool_input: dict, instance_id: str,
    data_dir: str,
) -> str:
    return await proceed_with_recovery_service(
        data_dir=data_dir,
        instance_id=instance_id,
        attempt_id=tool_input.get("attempt_id") or "",
        consult_fn=getattr(handler, "_consult_fn_for_loop", None),
        receipts_event_stream=getattr(handler, "events", None),
    )


async def handle_abandon_attempt(
    *, tool_input: dict, instance_id: str, data_dir: str,
) -> str:
    return await abandon_attempt_service(
        data_dir=data_dir,
        instance_id=instance_id,
        attempt_id=tool_input.get("attempt_id") or "",
        reason=tool_input.get("reason") or "",
    )
