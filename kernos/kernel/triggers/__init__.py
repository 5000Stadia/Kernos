"""Unified time + event trigger runtime — WORKFLOW-TRIGGERS-CONSOLIDATION v1.

Shipped through C3:

* :mod:`predicate` — three-part predicate model (event_selector,
  temporal_relation, dispatch_policy) + deterministic
  fire_window_key derivation + fire_id derivation.
* :mod:`outbox` — :class:`FireOutbox` durable dispatch outbox over
  the existing ``trigger_fires`` table; CAS-based status
  transitions; recovery sweep helpers.
* :mod:`runtime` — :class:`TriggerEvaluationRuntime` (start / stop
  / register / deactivate / on_event_observed / evaluate_now /
  recover). C2 filled in cron walk + event-driven match path +
  WLP-fire_id reconciliation.
* :mod:`evaluator` — per-temporal-kind evaluation helpers
  (cron windowing, event-selector match, due-time math).
* :mod:`sources` — :class:`EventSource` protocol +
  :class:`InternalEventAdapter` (post-flush → on_event_observed)
  + :class:`CalendarSource` and :class:`SchedulerHeartbeatSource`
  emitters. C3 substrate.
* :mod:`errors` — typed error hierarchy.

Out of scope through C3: external source contracts (C4),
scheduler.py refactor + CRB compiler integration (C5),
missed-window catch_up semantics (C6), Pattern 05 strike + live
test sweep (C7).
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
from kernos.kernel.triggers.adapters import (
    CompiledTrigger,
    compile_descriptor_triggers,
    compile_trigger_descriptor,
    derive_trigger_id,
)
from kernos.kernel.triggers.external_sources import (
    EVENT_TYPE_EMAIL_MESSAGE_OBSERVED,
    EVENT_TYPE_NOTION_PAGE_OBSERVED,
    EmailMessageSource,
    NotionPageSource,
)
from kernos.kernel.triggers.runtime import TriggerEvaluationRuntime
from kernos.kernel.triggers.sources import (
    CALENDAR_SOURCE_MODULE,
    EVENT_TYPE_CALENDAR_OBSERVED,
    EVENT_TYPE_SCHEDULER_TICK_DUE,
    SCHEDULER_SOURCE_MODULE,
    CalendarSource,
    EventSource,
    InternalEventAdapter,
    SchedulerHeartbeatSource,
)


__all__ = [
    "CALENDAR_SOURCE_MODULE",
    "CalendarSource",
    "CompiledTrigger",
    "DispatchFailed",
    "DispatchPolicy",
    "DispatchPolicyError",
    "EVENT_TYPE_CALENDAR_OBSERVED",
    "EVENT_TYPE_EMAIL_MESSAGE_OBSERVED",
    "EVENT_TYPE_NOTION_PAGE_OBSERVED",
    "EVENT_TYPE_SCHEDULER_TICK_DUE",
    "EmailMessageSource",
    "EventSource",
    "FireOutbox",
    "FireOutboxError",
    "FireRecord",
    "FireWindowConflict",
    "InternalEventAdapter",
    "NotionPageSource",
    "PredicateValidationError",
    "SCHEDULER_SOURCE_MODULE",
    "SchedulerHeartbeatSource",
    "StaleClaimError",
    "StaleFireRecovery",
    "TemporalRelation",
    "TemporalRelationError",
    "TriggerError",
    "TriggerEvaluationRuntime",
    "TriggerPredicate",
    "compile_descriptor_triggers",
    "compile_trigger_descriptor",
    "derive_fire_id",
    "derive_trigger_id",
    "ensure_outbox_schema",
    "fire_window_key_for_after",
    "fire_window_key_for_before",
    "fire_window_key_for_every",
    "fire_window_key_for_on",
    "validate_dispatch_policy",
    "validate_predicate",
    "validate_temporal",
]
