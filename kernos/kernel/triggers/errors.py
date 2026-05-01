"""Error hierarchy for the unified trigger runtime.

Single base ``TriggerError`` so callers can ``except`` the whole
module's failure surface in one line. Specific subclasses
distinguish failure modes that matter to recovery / observability.
"""
from __future__ import annotations


class TriggerError(Exception):
    """Base for the triggers module."""


class PredicateValidationError(TriggerError):
    """``TriggerPredicate`` failed shape/contract validation at
    register time. Atomic-register guarantee: when this raises,
    nothing is persisted."""


class TemporalRelationError(PredicateValidationError):
    """``TemporalRelation`` has invalid kind or missing required
    field for that kind (e.g. ``every`` without a cron expression)."""


class DispatchPolicyError(PredicateValidationError):
    """``DispatchPolicy`` has an invalid field combination
    (e.g. negative retry budget, unknown missed_window value)."""


class FireOutboxError(TriggerError):
    """Outbox-level error."""


class FireWindowConflict(FireOutboxError):
    """UNIQUE constraint hit — another path already claimed this
    ``(trigger_id, fire_window_key)``. Caller should treat as
    'already fired,' no-op."""


class StaleClaimError(FireOutboxError):
    """CAS rejection on a `mark_*` transition. Another process or a
    recovery sweep took ownership of the row; the caller's view of
    state is stale and the operation must be abandoned."""


class StaleFireRecovery(FireOutboxError):
    """Recovery sweep found a fire record older than the recovery
    threshold; manual triage required."""


class DispatchFailed(TriggerError):
    """Runtime → WLP dispatch failed beyond retry budget. NOT raised
    for workflow-internal execution failures; those are WLP's
    domain and surface through the workflow_executions table."""


__all__ = [
    "DispatchFailed",
    "DispatchPolicyError",
    "FireOutboxError",
    "FireWindowConflict",
    "PredicateValidationError",
    "StaleClaimError",
    "StaleFireRecovery",
    "TemporalRelationError",
    "TriggerError",
]
