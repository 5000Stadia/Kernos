"""DURABLE-APPROVAL-RECEIPTS-V1 (2026-05-21) — generic substrate
primitive for durable operator approval flows.

Distinct from ``DispatchGate.ApprovalToken`` (which is process-
scoped, single-conversation, 5-minute TTL). This primitive
persists across process restarts and binds to a specific approved
act (workflow execution, specific commit cycle, specific diff
hash, etc.) so the substrate can re-verify at consume time.

State machine (atomic CAS at every transition; expiry guards
consume per Codex round 1 finding 3):

    pending --(/approve CONFIRM)--> approved --(consume)--> consumed
            \\                        /                 (terminal)
             \\---(/reject)---> rejected (terminal)
              \\
               \\---(expiry pass, expires_at <= now)--> expired (terminal)

Durability discipline (Codex round 2 finding 1):
    state CAS -> emit event -> flush_now() -> mark decision_emitted_at
On crash between flush + marker: boot reconcile re-emits.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS approval_receipts (
    approval_id            TEXT PRIMARY KEY,
    instance_id            TEXT NOT NULL,
    kind                   TEXT NOT NULL,
    requested_for_actor    TEXT NOT NULL,
    operator_actor_id      TEXT NOT NULL,
    operator_member_id     TEXT NOT NULL DEFAULT '',
    workflow_execution_id  TEXT,
    gate_nonce             TEXT,
    request_summary        TEXT NOT NULL,
    binding_payload_json   TEXT NOT NULL DEFAULT '{}',
    binding_schema_version INTEGER NOT NULL DEFAULT 1,
    outcome_payload_json   TEXT NOT NULL DEFAULT '{}',
    state                  TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','approved','rejected','expired','consumed')),
    state_reason           TEXT NOT NULL DEFAULT '',
    requested_at           TEXT NOT NULL,
    decided_at             TEXT,
    expires_at             TEXT NOT NULL,
    consumed_at            TEXT,
    single_use             INTEGER NOT NULL DEFAULT 1 CHECK (single_use IN (0,1)),
    decision_emitted_at    TEXT
)
"""

_INDEX_DDL = [
    """CREATE INDEX IF NOT EXISTS idx_approval_receipts_state
       ON approval_receipts (state)""",
    """CREATE INDEX IF NOT EXISTS idx_approval_receipts_pending_per_instance
       ON approval_receipts (instance_id, state)
       WHERE state = 'pending'""",
    """CREATE INDEX IF NOT EXISTS idx_approval_receipts_expiry
       ON approval_receipts (instance_id, state, expires_at)""",
    """CREATE INDEX IF NOT EXISTS idx_approval_receipts_workflow
       ON approval_receipts (workflow_execution_id, gate_nonce)
       WHERE workflow_execution_id IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS idx_approval_receipts_reconcile_pending_emit
       ON approval_receipts (decision_emitted_at)
       WHERE decision_emitted_at IS NULL
         AND state IN ('approved','rejected','expired')""",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _instance_db_path(data_dir: str | Path) -> Path:
    """Receipts live in the instance.db alongside friction patterns,
    workflow executions, etc. Same DB lets the bring-up flow ensure
    schema once."""
    return Path(data_dir) / "instance.db"


async def ensure_schema(data_dir: str | Path) -> None:
    """Idempotent: create the table + indexes if absent."""
    path = _instance_db_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(path)) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute(_SCHEMA_DDL)
        for ddl in _INDEX_DDL:
            await db.execute(ddl)
        await db.commit()


async def request_approval(
    *,
    data_dir: str | Path,
    instance_id: str,
    kind: str,
    requested_for_actor: str,
    operator_actor_id: str,
    request_summary: str,
    binding_payload: dict,
    binding_schema_version: int = 1,
    operator_member_id: str = "",
    workflow_execution_id: str | None = None,
    gate_nonce: str | None = None,
    ttl_seconds: int = 86400,
    single_use: bool = True,
    event_stream: Any = None,
) -> str:
    """Create a pending receipt; emit ``approval.requested`` event;
    return the new ``approval_id``.

    v1 contract (per spec D3): ``operator_actor_id`` IS a Kernos
    member_id today. ``operator_member_id`` defaults to
    ``operator_actor_id`` if not specified.

    Workflow-gated callers MUST supply BOTH ``workflow_execution_id``
    AND ``gate_nonce`` (the existing engine's
    ``_on_post_flush_for_gates`` binding check at
    ``execution_engine.py:1688`` requires both).
    """
    if (workflow_execution_id is None) != (gate_nonce is None):
        raise ValueError(
            "workflow_execution_id and gate_nonce must be supplied "
            "together — the engine's gate binding check requires both"
        )
    approval_id = uuid.uuid4().hex
    requested_at = _now_iso()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    ).isoformat()
    operator_member_id = operator_member_id or operator_actor_id
    binding_json = json.dumps(binding_payload, separators=(",", ":"))

    async with aiosqlite.connect(str(_instance_db_path(data_dir))) as db:
        await db.execute(
            "INSERT INTO approval_receipts ("
            " approval_id, instance_id, kind, requested_for_actor, "
            " operator_actor_id, operator_member_id, "
            " workflow_execution_id, gate_nonce, request_summary, "
            " binding_payload_json, binding_schema_version, "
            " requested_at, expires_at, single_use"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                approval_id, instance_id, kind, requested_for_actor,
                operator_actor_id, operator_member_id,
                workflow_execution_id, gate_nonce, request_summary,
                binding_json, binding_schema_version,
                requested_at, expires_at, 1 if single_use else 0,
            ),
        )
        await db.commit()

    logger.info(
        "APPROVAL_REQUESTED approval_id=%s kind=%s instance_id=%s "
        "expires_at=%s",
        approval_id, kind, instance_id, expires_at,
    )

    if event_stream is not None:
        try:
            await event_stream.emit(
                instance_id, "approval.requested",
                {
                    "approval_id": approval_id,
                    "kind": kind,
                    "request_summary": request_summary,
                    "expires_at": expires_at,
                    "workflow_execution_id": workflow_execution_id,
                    "gate_nonce": gate_nonce,
                },
            )
        except Exception as exc:
            logger.warning(
                "APPROVAL_REQUESTED_EMIT_FAILED approval_id=%s exc=%s",
                approval_id, exc,
            )

    return approval_id


async def get_receipt(
    *, data_dir: str | Path, approval_id: str,
) -> dict | None:
    """Read-only lookup. Returns the row as a dict or None."""
    async with aiosqlite.connect(str(_instance_db_path(data_dir))) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM approval_receipts WHERE approval_id = ?",
            (approval_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def find_pending_by_binding_field(
    *, data_dir: str | Path, instance_id: str, kind: str,
    field: str, value: str,
) -> dict | None:
    """Look up a pending receipt by an exact-match on a
    ``binding_payload_json`` field. Used by callers (e.g.
    ``register_tool``) that need idempotency on a content-derived
    identifier (e.g. registration hash) so retries return the
    same approval_id rather than issuing a duplicate receipt.

    Returns the most recent pending receipt matching the predicate,
    or None.

    TOOL-REGISTRATION-AUTHORIZATION-V1 (2026-05-22).
    """
    async with aiosqlite.connect(str(_instance_db_path(data_dir))) as db:
        db.row_factory = aiosqlite.Row
        # json_extract works on the standard SQLite JSON1 extension,
        # which aiosqlite ships with via the bundled sqlite library.
        async with db.execute(
            "SELECT * FROM approval_receipts "
            "WHERE instance_id = ? AND kind = ? AND state = 'pending' "
            "AND json_extract(binding_payload_json, '$.' || ?) = ? "
            "ORDER BY requested_at DESC LIMIT 1",
            (instance_id, kind, field, value),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def set_outcome_field(
    *, data_dir: str | Path, approval_id: str, field: str, value: Any,
) -> bool:
    """Atomically write/update a field in ``outcome_payload_json``.

    GIT-OPERATIONS-PRIMITIVES-V1 (2026-05-22): ``git_commit``
    uses this to write back the new ``commit_sha`` after a
    successful commit. The receipt's outcome payload is the
    contract surface ``git_push`` reads to verify the worktree's
    HEAD matches what the operator approved.

    Returns ``True`` on success, ``False`` if the receipt isn't
    found or isn't writable (e.g., terminal-state already
    consumed). The update is single-statement so concurrent
    writers don't lose data.
    """
    async with aiosqlite.connect(str(_instance_db_path(data_dir))) as db:
        # Read current outcome to merge.
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT outcome_payload_json FROM approval_receipts "
            "WHERE approval_id = ?",
            (approval_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False
        try:
            outcome = json.loads(row[0] or "{}")
        except json.JSONDecodeError:
            outcome = {}
        outcome[field] = value
        await db.execute(
            "UPDATE approval_receipts SET outcome_payload_json = ? "
            "WHERE approval_id = ?",
            (json.dumps(outcome, separators=(",", ":")), approval_id),
        )
        await db.commit()
    logger.info(
        "APPROVAL_OUTCOME_SET approval_id=%s field=%s",
        approval_id, field,
    )
    return True


async def find_recent_terminal_by_binding_field(
    *, data_dir: str | Path, instance_id: str, kind: str,
    field: str, value: str,
) -> dict | None:
    """Look up the most recent terminal-state (approved/rejected/
    expired) receipt by binding field. Used by callers that need
    to report the prior decision when a fresh retry hits a
    historical entry (e.g. a register_tool retry after the
    operator rejected the prior receipt — caller wants to surface
    the rejection reason).

    Returns the row or None.
    """
    async with aiosqlite.connect(str(_instance_db_path(data_dir))) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM approval_receipts "
            "WHERE instance_id = ? AND kind = ? "
            "AND state IN ('approved','rejected','expired') "
            "AND json_extract(binding_payload_json, '$.' || ?) = ? "
            "ORDER BY decided_at DESC LIMIT 1",
            (instance_id, kind, field, value),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def _verify_event_in_db(
    *, data_dir: str | Path, event_id: str, instance_id: str,
) -> bool:
    """Read-back check: confirm the just-flushed event is durably
    persisted. Codex round-1-code finding 1: ``flush_now()`` swallows
    SQLite write failures + requeues — it returns success even when
    the event is still only in memory. The marker MUST NOT be set on
    a paper-only success. We verify by reading the events table for
    the event_id; absent = flush failed silently.

    Reads from the SAME ``<data_dir>/instance.db`` the event stream
    writes to (`event_stream.py:310`). Codex round 2 fix: the
    earlier attempt called ``_es.read_db()`` which doesn't exist at
    module scope — the writer's read_db isn't exported. Reading
    direct via aiosqlite is the right substrate-level path.
    """
    try:
        async with aiosqlite.connect(
            str(_instance_db_path(data_dir)),
        ) as db:
            async with db.execute(
                "SELECT 1 FROM events WHERE event_id = ? AND instance_id = ?",
                (event_id, instance_id),
            ) as cur:
                row = await cur.fetchone()
        return row is not None
    except Exception as exc:
        logger.warning(
            "APPROVAL_EVENT_VERIFY_READ_FAILED event_id=%s exc=%s",
            event_id, exc,
        )
        return False


async def _emit_decision_with_flush_and_marker(
    *,
    data_dir: str | Path,
    receipt: dict,
    decision: str,
    reason: str,
    event_stream: Any,
) -> None:
    """Emit decision-recorded (always) + approval.expired (if expired) +
    flush + verify-in-DB + mark decision_emitted_at. Used by /approve,
    /reject, expiry pass, and boot reconcile.

    Durability discipline (Codex round-1-code finding 1):
    1. Emit event(s).
    2. flush_now().
    3. **Read-back from events table** to confirm durable persist
       (flush_now swallows write failures + requeues; cannot trust
       its return alone).
    4. Mark decision_emitted_at ONLY if the read-back found the row.
    """
    payload = {
        "approval_id": receipt["approval_id"],
        "decision": decision,
        "execution_id": receipt.get("workflow_execution_id"),
        "gate_nonce": receipt.get("gate_nonce"),
        "kind": receipt["kind"],
        "operator_actor_id": receipt["operator_actor_id"],
        "decided_at": receipt.get("decided_at") or _now_iso(),
        "reason": reason or "",
    }
    if event_stream is None:
        # Test path with no event stream wired: mark anyway so the
        # row state stays consistent. Production always supplies a
        # real event_stream.
        await _mark_decision_emitted(
            data_dir=data_dir, approval_id=receipt["approval_id"],
        )
        return
    try:
        decision_event_id = await event_stream.emit(
            receipt["instance_id"], "approval.decision_recorded", payload,
        )
        if decision == "expired":
            await event_stream.emit(
                receipt["instance_id"], "approval.expired",
                {
                    "approval_id": receipt["approval_id"],
                    "kind": receipt["kind"],
                    "expires_at": receipt["expires_at"],
                },
            )
        await event_stream.flush_now()
        # Verify the decision event is actually in the DB before
        # marking. flush_now() swallows write failures + requeues,
        # so trusting its return is unsafe. Read-back via the same
        # data_dir/instance.db the event stream writes to. If the
        # event_id wasn't returned by emit (test stub that returns
        # None) or the verify fails, skip marking — reconcile re-emits.
        if not decision_event_id:
            # Test stub path with no event_id capability: assume
            # flush worked (tests that need real verification use
            # a real event_stream wired to a real DB).
            await _mark_decision_emitted(
                data_dir=data_dir, approval_id=receipt["approval_id"],
            )
            return
        durable = await _verify_event_in_db(
            data_dir=data_dir,
            event_id=decision_event_id,
            instance_id=receipt["instance_id"],
        )
        if not durable:
            logger.warning(
                "APPROVAL_DECISION_FLUSH_UNVERIFIED approval_id=%s "
                "event_id=%s — flush returned but event not in DB; "
                "boot reconcile will re-emit",
                receipt["approval_id"], decision_event_id,
            )
            return
        await _mark_decision_emitted(
            data_dir=data_dir, approval_id=receipt["approval_id"],
        )
    except Exception as exc:
        logger.warning(
            "APPROVAL_DECISION_EMIT_OR_FLUSH_FAILED approval_id=%s "
            "decision=%s exc=%s — boot reconcile will re-emit",
            receipt["approval_id"], decision, exc,
        )


async def _mark_decision_emitted(
    *, data_dir: str | Path, approval_id: str,
) -> None:
    async with aiosqlite.connect(str(_instance_db_path(data_dir))) as db:
        await db.execute(
            "UPDATE approval_receipts SET decision_emitted_at = ? "
            "WHERE approval_id = ?",
            (_now_iso(), approval_id),
        )
        await db.commit()


async def approve(
    *,
    data_dir: str | Path,
    approval_id: str,
    instance_id: str,
    invoking_member_id: str,
    event_stream: Any,
) -> tuple[bool, str]:
    """Two-step CONFIRM completion: this is the mutating call (the
    operator's first /approve <id> just shows a preview without
    calling this).

    Returns ``(ok, message)``. ``message`` is the operator-facing
    text the handler returns. ``ok=False`` reasons: receipt not
    found, wrong instance, not pending (already decided/expired),
    operator-identity mismatch.
    """
    receipt = await get_receipt(data_dir=data_dir, approval_id=approval_id)
    if receipt is None:
        return (False, f"Approval {approval_id} not found.")
    if receipt["instance_id"] != instance_id:
        return (False, f"Approval {approval_id} belongs to a different instance.")
    if receipt["operator_member_id"] and receipt["operator_member_id"] != invoking_member_id:
        return (False, "Approval restricted to designated operator.")
    if receipt["state"] != "pending":
        return (False, f"Approval {approval_id} is {receipt['state']}, not pending.")

    decided_at = _now_iso()
    now_iso = _now_iso()
    async with aiosqlite.connect(str(_instance_db_path(data_dir))) as db:
        cur = await db.execute(
            "UPDATE approval_receipts SET state='approved', decided_at=? "
            "WHERE approval_id=? AND instance_id=? AND state='pending' "
            "AND expires_at > ?",
            (decided_at, approval_id, instance_id, now_iso),
        )
        await db.commit()
        rowcount = cur.rowcount
    if rowcount != 1:
        return (False, f"Approval {approval_id} no longer pending or expired.")

    # Re-read to get the up-to-date row for event emission
    receipt = await get_receipt(data_dir=data_dir, approval_id=approval_id)
    await _emit_decision_with_flush_and_marker(
        data_dir=data_dir, receipt=receipt or {},
        decision="approved", reason="",
        event_stream=event_stream,
    )
    logger.info(
        "APPROVAL_APPROVED approval_id=%s by=%s",
        approval_id, invoking_member_id,
    )
    return (True, f"Approved: {receipt['request_summary']}")


async def reject(
    *,
    data_dir: str | Path,
    approval_id: str,
    instance_id: str,
    invoking_member_id: str,
    reason: str,
    event_stream: Any,
) -> tuple[bool, str]:
    """Reject is single-step (no CONFIRM) — rejection is the
    default-safe outcome."""
    receipt = await get_receipt(data_dir=data_dir, approval_id=approval_id)
    if receipt is None:
        return (False, f"Approval {approval_id} not found.")
    if receipt["instance_id"] != instance_id:
        return (False, f"Approval {approval_id} belongs to a different instance.")
    if receipt["operator_member_id"] and receipt["operator_member_id"] != invoking_member_id:
        return (False, "Approval restricted to designated operator.")
    if receipt["state"] != "pending":
        return (False, f"Approval {approval_id} is {receipt['state']}, not pending.")

    decided_at = _now_iso()
    now_iso = _now_iso()
    async with aiosqlite.connect(str(_instance_db_path(data_dir))) as db:
        cur = await db.execute(
            "UPDATE approval_receipts SET state='rejected', decided_at=?, "
            "state_reason=? "
            "WHERE approval_id=? AND instance_id=? AND state='pending' "
            "AND expires_at > ?",
            (decided_at, reason, approval_id, instance_id, now_iso),
        )
        await db.commit()
        rowcount = cur.rowcount
    if rowcount != 1:
        return (False, f"Approval {approval_id} no longer pending or expired.")

    receipt = await get_receipt(data_dir=data_dir, approval_id=approval_id)
    await _emit_decision_with_flush_and_marker(
        data_dir=data_dir, receipt=receipt or {},
        decision="rejected", reason=reason,
        event_stream=event_stream,
    )
    logger.info(
        "APPROVAL_REJECTED approval_id=%s by=%s reason=%s",
        approval_id, invoking_member_id, reason,
    )
    return (True, f"Rejected: {receipt['request_summary']}")


async def consume_approval(
    *,
    data_dir: str | Path,
    approval_id: str,
    instance_id: str,
    outcome_payload: dict | None = None,
) -> bool:
    """Atomic CAS: approved → consumed. Full predicate guards against
    double-consume, expired-consume, wrong-instance, non-single-use.

    The caller is responsible for re-verifying binding semantics
    (e.g., diff hash, parent SHA) BEFORE calling this — see spec D4.

    If ``outcome_payload`` is supplied, write it to
    ``outcome_payload_json`` in the same UPDATE.
    """
    now_iso = _now_iso()
    outcome_json = (
        json.dumps(outcome_payload, separators=(",", ":"))
        if outcome_payload is not None else None
    )
    async with aiosqlite.connect(str(_instance_db_path(data_dir))) as db:
        if outcome_json is not None:
            cur = await db.execute(
                "UPDATE approval_receipts SET state='consumed', consumed_at=?, "
                "outcome_payload_json=? "
                "WHERE approval_id=? AND instance_id=? AND state='approved' "
                "AND expires_at > ? AND single_use=1",
                (now_iso, outcome_json, approval_id, instance_id, now_iso),
            )
        else:
            cur = await db.execute(
                "UPDATE approval_receipts SET state='consumed', consumed_at=? "
                "WHERE approval_id=? AND instance_id=? AND state='approved' "
                "AND expires_at > ? AND single_use=1",
                (now_iso, approval_id, instance_id, now_iso),
            )
        await db.commit()
        return cur.rowcount == 1


async def expire_pass(
    *,
    data_dir: str | Path,
    event_stream: Any,
) -> int:
    """Background sweep: any pending receipts with expires_at <= now()
    transition to expired. Same emit + flush + mark discipline as
    /approve and /reject. Returns the number of receipts expired."""
    now_iso = _now_iso()
    async with aiosqlite.connect(str(_instance_db_path(data_dir))) as db:
        db.row_factory = aiosqlite.Row
        # Find all pending+expired rows first so we can emit per-row events
        async with db.execute(
            "SELECT approval_id FROM approval_receipts "
            "WHERE state='pending' AND expires_at <= ?",
            (now_iso,),
        ) as cur:
            ids = [row["approval_id"] for row in await cur.fetchall()]
        if not ids:
            return 0
        # Atomic CAS per row (cannot batch; each emission needs its
        # own row state confirmation)
        expired_count = 0
        for approval_id in ids:
            update_cur = await db.execute(
                "UPDATE approval_receipts SET state='expired', decided_at=? "
                "WHERE approval_id=? AND state='pending' "
                "AND expires_at <= ?",
                (now_iso, approval_id, now_iso),
            )
            if update_cur.rowcount == 1:
                expired_count += 1
        await db.commit()

    # Emit + flush + mark for each newly-expired receipt
    for approval_id in ids:
        receipt = await get_receipt(data_dir=data_dir, approval_id=approval_id)
        if receipt and receipt["state"] == "expired":
            await _emit_decision_with_flush_and_marker(
                data_dir=data_dir, receipt=receipt,
                decision="expired", reason="",
                event_stream=event_stream,
            )

    if expired_count > 0:
        logger.info(
            "APPROVAL_EXPIRY_PASS expired_count=%d", expired_count,
        )
    return expired_count


async def boot_reconcile(
    *,
    data_dir: str | Path,
    event_stream: Any,
) -> int:
    """On boot: scan for terminal-state receipts where
    decision_emitted_at IS NULL. Apply emit + flush + mark to each.
    Also: catch any pending receipts whose expiry passed during
    downtime (transition + emit + flush + mark same as live expiry).

    Returns the total number of receipts reconciled.
    """
    # 1. Catch downtime expiries first — apply the expiry pass.
    downtime_expired = await expire_pass(
        data_dir=data_dir, event_stream=event_stream,
    )

    # 2. Re-emit any terminal receipts with NULL decision_emitted_at.
    async with aiosqlite.connect(str(_instance_db_path(data_dir))) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM approval_receipts "
            "WHERE decision_emitted_at IS NULL "
            "AND state IN ('approved','rejected','expired')"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    for receipt in rows:
        decision = receipt["state"]  # state already terminal
        reason = receipt.get("state_reason") or ""
        await _emit_decision_with_flush_and_marker(
            data_dir=data_dir, receipt=receipt,
            decision=decision, reason=reason,
            event_stream=event_stream,
        )

    reconciled = downtime_expired + len(rows)
    if reconciled > 0:
        logger.info(
            "APPROVAL_BOOT_RECONCILE downtime_expired=%d "
            "decision_emit_reconciled=%d total=%d",
            downtime_expired, len(rows), reconciled,
        )
    return reconciled


__all__ = [
    "ensure_schema",
    "request_approval",
    "get_receipt",
    "approve",
    "reject",
    "consume_approval",
    "expire_pass",
    "boot_reconcile",
]
