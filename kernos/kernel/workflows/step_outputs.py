"""Workflow step output store.

WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 ships a sibling table to
Spec 3's ``workflow_action_records`` that captures the result
envelope of every workflow step. Subsequent steps reference prior
outputs via template syntax (Spec 4a Decision 3) — the resolver
loads from this table to substitute values into action parameters
or predicate ``value:`` fields.

Schema:

    workflow_step_outputs(
        instance_id, workflow_execution_id,
        output_kind ('step' | 'gate'),
        output_name (step.id OR gate_name),
        output_json,
        truncated,
        recorded_at,
        PRIMARY KEY (instance_id, workflow_execution_id, output_kind,
                     output_name),
        FOREIGN KEY (workflow_execution_id)
            REFERENCES workflow_executions(execution_id)
            ON DELETE RESTRICT
    )

Capture envelope shape (per Decision 2):

    {
        "success": bool,
        "value":   dict | scalar | None,
        "error":   str | None,
        "receipt": dict,
    }

Discipline:

- ``ON CONFLICT DO UPDATE`` — different from Spec 3's append-only
  workflow_action_records (DO NOTHING). Step outputs are a runtime
  cache that survives across the workflow execution; on retry of
  the same step (rare; idempotency-skip), the corresponding step
  output INSERT must be gated by the action record's
  ``inserted=True`` to keep the two tables consistent (Decision 11).
  This module exposes the unconditional capture; the engine call
  sites pass the inserted-gate.

- Non-serializable result values surface as friction (placeholder
  envelope) — log loud + capture envelope with
  ``error="non_serializable:<TypeName>"``.

- 64KB cap with truncation marker. Subsequent references into
  truncated regions fail loud at resolution time.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


# 64 KB per envelope; Decision 2.
_ENVELOPE_SIZE_CAP_BYTES = 65536


_WORKFLOW_STEP_OUTPUTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_step_outputs (
    instance_id             TEXT NOT NULL,
    workflow_execution_id   TEXT NOT NULL,
    output_kind             TEXT NOT NULL CHECK(output_kind IN ('step', 'gate')),
    output_name             TEXT NOT NULL,
    output_json             TEXT NOT NULL,
    truncated               INTEGER NOT NULL DEFAULT 0,
    recorded_at             TEXT NOT NULL,
    PRIMARY KEY (instance_id, workflow_execution_id, output_kind, output_name),
    FOREIGN KEY (workflow_execution_id)
        REFERENCES workflow_executions(execution_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_workflow_step_outputs_kind
    ON workflow_step_outputs (instance_id, workflow_execution_id, output_kind);
"""


async def ensure_workflow_step_outputs_schema(
    db: aiosqlite.Connection,
) -> None:
    """Create the workflow_step_outputs table + indexes.

    NOTE: ALTERs for ``terminal_branch`` + ``next_step_index`` on
    workflow_executions are co-located in execution_engine._ensure_schema
    so any code path opening a workflow_executions table picks them up
    (Spec 3 helpers reference next_step_index in UPDATE clauses).

    Called by ExecutionEngine.start() AFTER the existing
    workflow_executions ensure + Spec 3's ALTER migrations.
    """
    await db.execute("PRAGMA foreign_keys=ON")
    for stmt in _WORKFLOW_STEP_OUTPUTS_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            await db.execute(stmt)


def build_output_envelope(
    *,
    success: bool,
    value: Any,
    error: str | None,
    receipt: dict | None,
) -> dict:
    """Construct the uniform output envelope shape from per-outcome
    components. Decision 2 v2 per-outcome capture matrix.
    """
    return {
        "success": success,
        "value": value,
        "error": error,
        "receipt": receipt or {},
    }


def serialize_envelope(envelope: dict) -> tuple[str, bool, bool]:
    """Serialize an output envelope.

    Returns (payload_str, truncated, was_serializable).

    On non-serializable values: returns a placeholder envelope with
    ``error="non_serializable:<TypeName>"`` and logs loud. Returns
    ``was_serializable=False`` so the caller can surface friction.

    On envelopes exceeding the 64KB cap: truncates the value /
    receipt fields and inserts truncation markers. Returns
    ``truncated=True``.
    """
    try:
        payload = json.dumps(envelope)
        was_serializable = True
    except (TypeError, ValueError) as exc:
        logger.warning(
            "WORKFLOW_STEP_OUTPUT_SERIALIZATION_FAILED error=%s", exc,
        )
        envelope = {
            "success": False,
            "value": None,
            "error": f"non_serializable:{type(exc).__name__}",
            "receipt": {},
        }
        payload = json.dumps(envelope)
        was_serializable = False

    truncated = False
    if len(payload.encode("utf-8")) > _ENVELOPE_SIZE_CAP_BYTES:
        original_size = len(payload)
        logger.warning(
            "WORKFLOW_STEP_OUTPUT_TRUNCATED original_size=%d cap=%d",
            original_size, _ENVELOPE_SIZE_CAP_BYTES,
        )
        # Truncate value + receipt and re-serialize.
        try:
            value_str = json.dumps(envelope["value"])[:32768]
        except Exception:
            value_str = "<unserializable>"
        try:
            receipt_str = json.dumps(envelope["receipt"])[:8192]
        except Exception:
            receipt_str = "<unserializable>"
        truncated_envelope = {
            "success": envelope["success"],
            "value": {
                "_truncated": True,
                "_original_size_bytes": original_size,
                "_partial": value_str,
            },
            "error": envelope["error"],
            "receipt": {
                "_truncated": True,
                "_partial": receipt_str,
            },
        }
        payload = json.dumps(truncated_envelope)
        truncated = True

    return payload, truncated, was_serializable


async def capture_step_output(
    db: aiosqlite.Connection,
    *,
    instance_id: str,
    workflow_execution_id: str,
    step_id: str,
    envelope: dict,
) -> None:
    """Persist a step output envelope under output_kind='step'.

    Called inside the engine's per-outcome ``_run_workflow_txn``
    body. Gated by the per-outcome helper's ``inserted=True`` per
    Decision 11 (consistency invariant with Spec 3 action records).
    """
    payload, truncated, _ = serialize_envelope(envelope)
    await db.execute(
        "INSERT INTO workflow_step_outputs ("
        " instance_id, workflow_execution_id, output_kind, output_name,"
        " output_json, truncated, recorded_at"
        ") VALUES (?, ?, 'step', ?, ?, ?, ?) "
        "ON CONFLICT(instance_id, workflow_execution_id, output_kind, output_name) "
        "DO UPDATE SET output_json = excluded.output_json, "
        "truncated = excluded.truncated, recorded_at = excluded.recorded_at",
        (
            instance_id, workflow_execution_id, step_id,
            payload, 1 if truncated else 0, _now(),
        ),
    )


async def capture_gate_output(
    db: aiosqlite.Connection,
    *,
    instance_id: str,
    workflow_execution_id: str,
    gate_name: str,
    event_payload: dict,
) -> None:
    """Persist a gate output (the satisfying approval event's
    payload) under output_kind='gate'.

    Spec 4 post-impl High 4: the envelope's ``value`` IS the
    matched event's payload directly (no ``payload`` wrapper key).
    Reference syntax ``{gate.<gate_name>.output.<path>}`` resolves
    to ``event_payload[path]``, matching the spec's documented form.
    """
    envelope = {
        "success": True,
        "value": event_payload,
        "error": None,
        "receipt": {},
    }
    payload, truncated, _ = serialize_envelope(envelope)
    await db.execute(
        "INSERT INTO workflow_step_outputs ("
        " instance_id, workflow_execution_id, output_kind, output_name,"
        " output_json, truncated, recorded_at"
        ") VALUES (?, ?, 'gate', ?, ?, ?, ?) "
        "ON CONFLICT(instance_id, workflow_execution_id, output_kind, output_name) "
        "DO UPDATE SET output_json = excluded.output_json, "
        "truncated = excluded.truncated, recorded_at = excluded.recorded_at",
        (
            instance_id, workflow_execution_id, gate_name,
            payload, 1 if truncated else 0, _now(),
        ),
    )


async def load_workflow_outputs(
    db: aiosqlite.Connection,
    instance_id: str,
    workflow_execution_id: str,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Return (step_outputs, gate_outputs) dicts keyed by name.

    Used by the engine to build the ResolutionContext before each
    step's parameter resolution. Composes with restart-resume: on
    restart, prior step outputs persist via the table.
    """
    step_outputs: dict[str, dict] = {}
    gate_outputs: dict[str, dict] = {}
    async with db.execute(
        "SELECT output_kind, output_name, output_json "
        "FROM workflow_step_outputs "
        "WHERE instance_id = ? AND workflow_execution_id = ?",
        (instance_id, workflow_execution_id),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        try:
            envelope = json.loads(row["output_json"])
        except (json.JSONDecodeError, TypeError):
            envelope = {}
        if row["output_kind"] == "step":
            step_outputs[row["output_name"]] = envelope
        elif row["output_kind"] == "gate":
            gate_outputs[row["output_name"]] = envelope
    return step_outputs, gate_outputs


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "build_output_envelope",
    "capture_gate_output",
    "capture_step_output",
    "ensure_workflow_step_outputs_schema",
    "load_workflow_outputs",
    "serialize_envelope",
]
