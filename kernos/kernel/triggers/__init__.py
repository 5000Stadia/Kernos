"""Unified time + event trigger runtime — WORKFLOW-TRIGGERS-CONSOLIDATION v1.

C1 ships:

* :mod:`predicate` — three-part predicate model (event_selector,
  temporal_relation, dispatch_policy) + deterministic
  fire_window_key derivation + fire_id derivation.
* :mod:`outbox` — :class:`FireOutbox` durable dispatch outbox over
  the existing ``trigger_fires`` table; CAS-based status
  transitions; recovery sweep helpers.
* :mod:`runtime` — :class:`TriggerEvaluationRuntime` interface
  shell (start / stop / register / deactivate / evaluate_now /
  recover). C1's evaluate_now and recover are no-ops; C2 fills
  them in.
* :mod:`errors` — typed error hierarchy.

Out of scope for C1: cron walk, before/after due-time math,
event-driven match path, recovery-sweep WLP reconciliation,
adapters (CRB compiler / scheduler / calendar). Those land in
C2-C7.
"""
from __future__ import annotations

from kernos.kernel.triggers.errors import (
    DispatchFailed,
    DispatchPolicyError,
    FireOutboxError,
    FireWindowConflict,
    PredicateValidationError,
    StaleClaimError,
    StaleFireRecovery,
    TemporalRelationError,
    TriggerError,
)
from kernos.kernel.triggers.outbox import (
    FireOutbox,
    FireRecord,
    ensure_outbox_schema,
)
from kernos.kernel.triggers.predicate import (
    DispatchPolicy,
    TemporalRelation,
    TriggerPredicate,
    derive_fire_id,
    fire_window_key_for_after,
    fire_window_key_for_before,
    fire_window_key_for_every,
    fire_window_key_for_on,
    validate_dispatch_policy,
    validate_predicate,
    validate_temporal,
)
from kernos.kernel.triggers.runtime import TriggerEvaluationRuntime


__all__ = [
    "DispatchFailed",
    "DispatchPolicy",
    "DispatchPolicyError",
    "FireOutbox",
    "FireOutboxError",
    "FireRecord",
    "FireWindowConflict",
    "PredicateValidationError",
    "StaleClaimError",
    "StaleFireRecovery",
    "TemporalRelation",
    "TemporalRelationError",
    "TriggerError",
    "TriggerEvaluationRuntime",
    "TriggerPredicate",
    "derive_fire_id",
    "ensure_outbox_schema",
    "fire_window_key_for_after",
    "fire_window_key_for_before",
    "fire_window_key_for_every",
    "fire_window_key_for_on",
    "validate_dispatch_policy",
    "validate_predicate",
    "validate_temporal",
]
