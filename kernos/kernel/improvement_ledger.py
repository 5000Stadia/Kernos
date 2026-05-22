"""Improvement-attempt ledger helpers.

IMPROVEMENT-ATTEMPT-LEDGER-V1 (2026-05-22).

Operator-observer surface for the autonomous-improvement loop.
Three tables (live in instance.db, created by the
``_INSTANCE_SCHEMA`` bootstrap):

  - ``improvement_attempts`` — top-level per-attempt state.
  - ``improvement_attempt_commits`` — per-cycle commit truth.
  - ``improvement_attempt_events`` — append-only narrative.

The orchestrator workflow (future ``IMPROVEMENT-LOOP-WORKFLOW-V1``)
calls these helpers; ``/improvement_status`` slash command
reads from them. Agent does NOT consume the ledger — purely
operator-observer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------
# CRUD on improvement_attempts
# ---------------------------------------------------------------------


async def create_attempt(
    conn: aiosqlite.Connection,
    *,
    instance_id: str,
    attempt_id: str,
    spec_requirement: str,
    started_at: str = "",
    primary_coding_agent: str = "",
    reviewer_coding_agent: str = "",
    worktree_path: str = "",
) -> None:
    """Insert a new attempt row. Defaults: iterations=0,
    state=null (set on completion)."""
    started_at = started_at or _now_iso()
    await conn.execute(
        "INSERT INTO improvement_attempts ("
        "  attempt_id, instance_id, started_at, spec_requirement, "
        "  primary_coding_agent, reviewer_coding_agent, "
        "  worktree_path "
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            attempt_id, instance_id, started_at, spec_requirement,
            primary_coding_agent, reviewer_coding_agent, worktree_path,
        ),
    )
    await conn.commit()
    logger.info(
        "IMPROVEMENT_ATTEMPT_CREATED attempt=%s spec=%r",
        attempt_id, spec_requirement[:60],
    )


_MUTABLE_ATTEMPT_FIELDS = frozenset({
    "ended_at", "spec_iterations", "spec_iterations_outcome",
    "impl_iterations", "impl_iterations_outcome", "final_commit_sha",
    "test_outcome", "first_pass_green", "final_state",
    "primary_coding_agent", "reviewer_coding_agent",
    "worktree_path",
})


async def update_attempt(
    conn: aiosqlite.Connection, *, attempt_id: str, **fields: Any,
) -> None:
    """Mutate only the fields passed. Unknown fields raise
    ValueError to catch typos at the call site."""
    if not fields:
        return
    unknown = set(fields) - _MUTABLE_ATTEMPT_FIELDS
    if unknown:
        raise ValueError(
            f"unknown improvement_attempts field(s): {sorted(unknown)}"
        )
    sets = ", ".join(f"{k}=?" for k in fields)
    params = list(fields.values()) + [attempt_id]
    await conn.execute(
        f"UPDATE improvement_attempts SET {sets} WHERE attempt_id=?",
        params,
    )
    await conn.commit()


async def get_attempt(
    conn: aiosqlite.Connection, attempt_id: str,
) -> dict | None:
    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        "SELECT * FROM improvement_attempts WHERE attempt_id=?",
        (attempt_id,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def list_recent_attempts(
    conn: aiosqlite.Connection, instance_id: str, *, limit: int = 10,
) -> list[dict]:
    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        "SELECT * FROM improvement_attempts "
        "WHERE instance_id=? ORDER BY started_at DESC LIMIT ?",
        (instance_id, int(limit)),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------
# improvement_attempt_events
# ---------------------------------------------------------------------


async def append_event(
    conn: aiosqlite.Connection, *,
    attempt_id: str, kind: str, detail: str = "",
    timestamp: str = "",
) -> int:
    """Append an event. Sequence is computed atomically inside a
    transaction so concurrent writers don't collide. Returns the
    assigned sequence."""
    timestamp = timestamp or _now_iso()
    async with conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 "
        "FROM improvement_attempt_events WHERE attempt_id=?",
        (attempt_id,),
    ) as cur:
        row = await cur.fetchone()
    next_seq = int(row[0]) if row else 1
    await conn.execute(
        "INSERT INTO improvement_attempt_events ("
        "  attempt_id, sequence, timestamp, kind, detail"
        ") VALUES (?, ?, ?, ?, ?)",
        (attempt_id, next_seq, timestamp, kind, detail),
    )
    await conn.commit()
    return next_seq


async def get_attempt_events(
    conn: aiosqlite.Connection, attempt_id: str,
) -> list[dict]:
    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        "SELECT * FROM improvement_attempt_events "
        "WHERE attempt_id=? ORDER BY sequence ASC",
        (attempt_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------
# improvement_attempt_commits
# ---------------------------------------------------------------------


async def record_commit(
    conn: aiosqlite.Connection, *,
    attempt_id: str, commit_sha: str, parent_sha: str,
    pushed_at: str = "",
    approval_id: str = "",
    test_outcome_after_this_commit: str = "",
    recovery_trigger: str = "",
) -> int:
    """Insert a commit row + bump `improvement_attempts.final_commit_sha`
    to the latest. Returns the assigned commit_sequence."""
    pushed_at = pushed_at or _now_iso()
    async with conn.execute(
        "SELECT COALESCE(MAX(commit_sequence), 0) + 1 "
        "FROM improvement_attempt_commits WHERE attempt_id=?",
        (attempt_id,),
    ) as cur:
        row = await cur.fetchone()
    next_seq = int(row[0]) if row else 1
    await conn.execute(
        "INSERT INTO improvement_attempt_commits ("
        "  attempt_id, commit_sequence, commit_sha, parent_sha, "
        "  pushed_at, approval_id, test_outcome_after_this_commit, "
        "  recovery_trigger"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            attempt_id, next_seq, commit_sha, parent_sha, pushed_at,
            approval_id, test_outcome_after_this_commit,
            recovery_trigger,
        ),
    )
    # Bump final_commit_sha to this latest.
    await conn.execute(
        "UPDATE improvement_attempts SET final_commit_sha=? "
        "WHERE attempt_id=?",
        (commit_sha, attempt_id),
    )
    await conn.commit()
    return next_seq


async def get_attempt_commits(
    conn: aiosqlite.Connection, attempt_id: str,
) -> list[dict]:
    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        "SELECT * FROM improvement_attempt_commits "
        "WHERE attempt_id=? ORDER BY commit_sequence ASC",
        (attempt_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------
# /improvement_status slash-command renderer (operator-facing)
# ---------------------------------------------------------------------


def render_recent_attempts(attempts: list[dict]) -> str:
    """Operator-facing structured text for the no-arg form."""
    if not attempts:
        return "No improvement attempts recorded yet."
    lines = ["**Recent improvement attempts**", ""]
    for a in attempts:
        state = a.get("final_state") or "running"
        spec = (a.get("spec_requirement") or "")[:60]
        if len(a.get("spec_requirement") or "") > 60:
            spec += "…"
        lines.append(
            f"- `{a['attempt_id']}` — `{state}` — "
            f"started {a.get('started_at', '?')}\n  _{spec}_"
        )
    return "\n".join(lines)


def render_attempt_detail(
    attempt: dict | None, commits: list[dict], events: list[dict],
) -> str:
    """Operator-facing structured detail view for a specific attempt."""
    if attempt is None:
        return "No such attempt id."
    lines = [
        f"# Attempt `{attempt['attempt_id']}`",
        f"- started: {attempt.get('started_at', '?')}",
    ]
    if attempt.get("ended_at"):
        lines.append(f"- ended: {attempt['ended_at']}")
    lines.append(
        f"- final_state: `{attempt.get('final_state') or 'running'}`"
    )
    if attempt.get("primary_coding_agent"):
        lines.append(
            f"- primary_agent: `{attempt['primary_coding_agent']}`"
        )
    if attempt.get("reviewer_coding_agent"):
        lines.append(
            f"- reviewer_agent: `{attempt['reviewer_coding_agent']}`"
        )
    if attempt.get("worktree_path"):
        lines.append(f"- worktree: `{attempt['worktree_path']}`")
    lines.append(
        f"- spec_iterations: {attempt.get('spec_iterations', 0)} "
        f"({attempt.get('spec_iterations_outcome') or 'pending'})"
    )
    lines.append(
        f"- impl_iterations: {attempt.get('impl_iterations', 0)} "
        f"({attempt.get('impl_iterations_outcome') or 'pending'})"
    )
    if attempt.get("final_commit_sha"):
        lines.append(
            f"- final_commit_sha: `{attempt['final_commit_sha'][:12]}`"
        )
    if attempt.get("test_outcome"):
        lines.append(f"- test_outcome: `{attempt['test_outcome']}`")
    if attempt.get("first_pass_green") is not None:
        lines.append(
            f"- first_pass_green: {bool(attempt['first_pass_green'])}"
        )
    lines.append("")
    lines.append("## Spec requirement")
    lines.append(attempt.get("spec_requirement", "") or "(empty)")
    lines.append("")
    lines.append("## Commits")
    if commits:
        for c in commits:
            recovery = c.get("recovery_trigger")
            rec_note = (
                f" (recovery: {recovery})" if recovery else ""
            )
            lines.append(
                f"- #{c['commit_sequence']}: "
                f"`{c['commit_sha'][:12]}` "
                f"(parent `{c['parent_sha'][:12]}`) "
                f"pushed {c.get('pushed_at', '?')}{rec_note}"
            )
    else:
        lines.append("_no commits yet_")
    lines.append("")
    lines.append("## Event narrative (most recent 20)")
    if events:
        for e in events[-20:]:
            detail = (e.get("detail") or "")[:80]
            extra = "…" if len(e.get("detail") or "") > 80 else ""
            lines.append(
                f"- #{e['sequence']} {e['timestamp']}: "
                f"`{e['kind']}` {detail}{extra}".rstrip()
            )
    else:
        lines.append("_no events yet_")
    return "\n".join(lines)
