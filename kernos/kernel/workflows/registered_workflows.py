"""Registered-workflows store for Spec 5 authoring layer.

WORKFLOW-AUTHORING-PRIMITIVES-V1 ships authoring substrate on top
of Spec 4's execution primitives. The ``registered_workflows`` table
captures the authoring-side metadata that doesn't fit on Spec 4's
``workflows`` table: governance tier (substrate vs composition),
activation state machine, who authored it.

Schema (per Decision 1 v2):

    registered_workflows(
        workflow_id          TEXT PRIMARY KEY,
        instance_id          TEXT NOT NULL,
        governance_tier      TEXT NOT NULL CHECK(
            governance_tier IN ('composition_tier', 'substrate_tier')
        ),
        activation_state     TEXT NOT NULL DEFAULT 'registered_not_activated'
                              CHECK(activation_state IN (
                                  'registered_not_activated',
                                  'active',
                                  'deactivated'
                              )),
        authored_by          TEXT NOT NULL DEFAULT '',
        architect_authored   INTEGER NOT NULL DEFAULT 0,
        computed_tier        TEXT NOT NULL DEFAULT 'composition_tier',
        deactivation_reason  TEXT NOT NULL DEFAULT '',
        created_at           TEXT NOT NULL,
        activated_at         TEXT NOT NULL DEFAULT '',
        deactivated_at       TEXT NOT NULL DEFAULT '',
        last_transition_at   TEXT NOT NULL DEFAULT '',
        FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id)
            ON DELETE RESTRICT
    )

State machine (Decision 3 v2):

    registered_not_activated → active        via activate_workflow
    active                   → deactivated   via deactivate_workflow
    deactivated              → active        via activate_workflow

CAS-style SQL UPDATEs ensure atomic transitions; architect Q3
ruling: first-writer-wins, loser sees ``invalid_activation_state``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# Activation-state values.
STATE_REGISTERED = "registered_not_activated"
STATE_ACTIVE = "active"
STATE_DEACTIVATED = "deactivated"

VALID_ACTIVATION_STATES = frozenset({
    STATE_REGISTERED, STATE_ACTIVE, STATE_DEACTIVATED,
})


# Governance tier values.
TIER_COMPOSITION = "composition_tier"
TIER_SUBSTRATE = "substrate_tier"

VALID_GOVERNANCE_TIERS = frozenset({TIER_COMPOSITION, TIER_SUBSTRATE})


_REGISTERED_WORKFLOWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS registered_workflows (
    workflow_id          TEXT PRIMARY KEY,
    instance_id          TEXT NOT NULL,
    governance_tier      TEXT NOT NULL CHECK(governance_tier IN ('composition_tier', 'substrate_tier')),
    activation_state     TEXT NOT NULL DEFAULT 'registered_not_activated'
                          CHECK(activation_state IN ('registered_not_activated', 'active', 'deactivated')),
    authored_by          TEXT NOT NULL DEFAULT '',
    architect_authored   INTEGER NOT NULL DEFAULT 0,
    computed_tier        TEXT NOT NULL DEFAULT 'composition_tier',
    deactivation_reason  TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL,
    activated_at         TEXT NOT NULL DEFAULT '',
    deactivated_at       TEXT NOT NULL DEFAULT '',
    last_transition_at   TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_registered_workflows_state
    ON registered_workflows (instance_id, activation_state);
"""


async def ensure_registered_workflows_schema(
    db: aiosqlite.Connection,
) -> None:
    """Create the registered_workflows table + indexes if absent.

    Idempotent on re-call. PRAGMA foreign_keys=ON is required for
    the FK to workflows(workflow_id) to enforce referential
    integrity.
    """
    await db.execute("PRAGMA foreign_keys=ON")
    for stmt in _REGISTERED_WORKFLOWS_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            await db.execute(stmt)


@dataclass(frozen=True)
class RegisteredWorkflow:
    """Snapshot of a row in the registered_workflows table."""

    workflow_id: str
    instance_id: str
    governance_tier: str
    activation_state: str
    authored_by: str
    architect_authored: bool
    computed_tier: str
    deactivation_reason: str
    created_at: str
    activated_at: str
    deactivated_at: str
    last_transition_at: str


def _row_to_registered_workflow(row: aiosqlite.Row) -> RegisteredWorkflow:
    return RegisteredWorkflow(
        workflow_id=row["workflow_id"],
        instance_id=row["instance_id"],
        governance_tier=row["governance_tier"],
        activation_state=row["activation_state"],
        authored_by=row["authored_by"] or "",
        architect_authored=bool(row["architect_authored"]),
        computed_tier=row["computed_tier"] or row["governance_tier"],
        deactivation_reason=row["deactivation_reason"] or "",
        created_at=row["created_at"],
        activated_at=row["activated_at"] or "",
        deactivated_at=row["deactivated_at"] or "",
        last_transition_at=row["last_transition_at"] or "",
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def insert_registered_workflow_within_txn(
    db: aiosqlite.Connection,
    *,
    workflow_id: str,
    instance_id: str,
    governance_tier: str,
    computed_tier: str,
    authored_by: str,
    architect_authored: bool,
) -> None:
    """INSERT a new registered_workflows row inside the caller's
    transaction. Used by the authoring layer's _run_authoring_txn
    helper (Spec 5 v2 Decision 1 / Codex Blocker 2) so the workflows
    + registered_workflows inserts land atomically.
    """
    now = _now()
    await db.execute(
        "INSERT INTO registered_workflows ("
        " workflow_id, instance_id, governance_tier, activation_state,"
        " authored_by, architect_authored, computed_tier, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workflow_id, instance_id, governance_tier, STATE_REGISTERED,
            authored_by, 1 if architect_authored else 0,
            computed_tier, now,
        ),
    )


async def transition_to_active(
    db: aiosqlite.Connection,
    *,
    workflow_id: str,
) -> tuple[bool, str]:
    """CAS-style transition to ``active``. Per architect Q3:
    first-writer-wins; loser sees False + the current state.

    Returns ``(updated, current_state)``. ``updated`` is True iff
    the transition succeeded; the row's prior state was either
    ``registered_not_activated`` OR ``deactivated``.
    """
    now = _now()
    cursor = await db.execute(
        "UPDATE registered_workflows "
        "SET activation_state = ?, activated_at = ?, "
        "    last_transition_at = ?, deactivation_reason = '' "
        "WHERE workflow_id = ? "
        "  AND activation_state IN (?, ?)",
        (STATE_ACTIVE, now, now, workflow_id, STATE_REGISTERED, STATE_DEACTIVATED),
    )
    if cursor.rowcount == 1:
        return True, STATE_ACTIVE
    # Re-read to disambiguate "already active" vs other state.
    current = await get_activation_state(db, workflow_id=workflow_id)
    return False, current


async def transition_to_deactivated(
    db: aiosqlite.Connection,
    *,
    workflow_id: str,
    reason: str = "",
) -> tuple[bool, str]:
    """CAS-style transition to ``deactivated``. Source state must be
    ``active``; transition from ``registered_not_activated`` is
    rejected (nothing to deactivate yet).
    """
    now = _now()
    cursor = await db.execute(
        "UPDATE registered_workflows "
        "SET activation_state = ?, deactivated_at = ?, "
        "    last_transition_at = ?, deactivation_reason = ? "
        "WHERE workflow_id = ? AND activation_state = ?",
        (STATE_DEACTIVATED, now, now, reason, workflow_id, STATE_ACTIVE),
    )
    if cursor.rowcount == 1:
        return True, STATE_DEACTIVATED
    current = await get_activation_state(db, workflow_id=workflow_id)
    return False, current


async def get_activation_state(
    db: aiosqlite.Connection,
    *,
    workflow_id: str,
) -> str:
    """Read the current activation_state for a workflow. Returns
    empty string if the workflow is not registered.
    """
    async with db.execute(
        "SELECT activation_state FROM registered_workflows "
        "WHERE workflow_id = ? LIMIT 1",
        (workflow_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return ""
    return row["activation_state"]


async def get_registered_workflow(
    db: aiosqlite.Connection,
    *,
    workflow_id: str,
) -> RegisteredWorkflow | None:
    """Load the full registered_workflows row."""
    async with db.execute(
        "SELECT * FROM registered_workflows WHERE workflow_id = ? LIMIT 1",
        (workflow_id,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_registered_workflow(row) if row is not None else None


async def is_workflow_active(
    db: aiosqlite.Connection,
    *,
    workflow_id: str,
) -> bool:
    """Convenience: True iff activation_state == 'active'. Used by
    the TriggerRegistry's dispatch-time check (Codex round-1
    Blocker 3 fold).
    """
    state = await get_activation_state(db, workflow_id=workflow_id)
    return state == STATE_ACTIVE


__all__ = [
    "RegisteredWorkflow",
    "STATE_ACTIVE",
    "STATE_DEACTIVATED",
    "STATE_REGISTERED",
    "TIER_COMPOSITION",
    "TIER_SUBSTRATE",
    "VALID_ACTIVATION_STATES",
    "VALID_GOVERNANCE_TIERS",
    "ensure_registered_workflows_schema",
    "get_activation_state",
    "get_registered_workflow",
    "insert_registered_workflow_within_txn",
    "is_workflow_active",
    "transition_to_active",
    "transition_to_deactivated",
]
