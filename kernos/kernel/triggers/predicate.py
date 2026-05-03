"""Three-part predicate model for the unified trigger runtime.

the design review must-fix #1 split the predicate into three independently-
extensible axes:

* **EventSelector** — the existing AST from
  :mod:`kernos.kernel.workflows.predicates`, unchanged. Composite
  (AND/OR/NOT) + leaf operators (eq, contains, exists, in_set,
  time_window, event_type_starts_with, actor_eq, correlation_eq).
  v1 reuses the shipped language verbatim.
* **TemporalRelation** — one of four kinds (`before`, `after`,
  `on`, `every`). Frozen dataclass; v1 enforces the four-kind
  whitelist; v1.x can extend.
* **DispatchPolicy** — how a fire actually happens (dedup window,
  missed-window posture, retry budget on dispatch failure).

Codex D2 pin: event-vs-time evaluation path is DERIVED from
``temporal_relation.kind``, not a public axis. Both paths converge
at the same durable claim_fire / dispatch runtime (D7).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

from kernos.kernel.triggers.errors import (
    DispatchPolicyError,
    PredicateValidationError,
    TemporalRelationError,
)


# Re-exported for clarity. Callers typing event selectors should
# treat this as the structured AST shape from the existing
# predicates module — we don't wrap it here.
EventSelector = dict[str, Any]


# ---------------------------------------------------------------------------
# TemporalRelation
# ---------------------------------------------------------------------------


_TEMPORAL_KINDS = frozenset({"before", "after", "on", "every"})


@dataclass(frozen=True)
class TemporalRelation:
    """One of four shapes. v1 enforces frozen via validate_temporal();
    v1.x can extend the kind whitelist.

    Kinds:

    * ``before(Y, minutes=N)`` — fires N minutes before next Y match.
    * ``after(Y, minutes=N)``  — fires N minutes after Y observed.
    * ``on(Y)``                — fires immediately when Y observed.
    * ``every(cron)``          — fires when cron expression matches now().
    """

    kind: Literal["before", "after", "on", "every"]
    minutes: int = 0
    cron_expression: str = ""


def validate_temporal(rel: TemporalRelation) -> None:
    """Raise :class:`TemporalRelationError` on invalid kind /
    missing-required-field combinations."""
    if rel.kind not in _TEMPORAL_KINDS:
        raise TemporalRelationError(
            f"temporal_relation.kind must be one of "
            f"{sorted(_TEMPORAL_KINDS)}; got {rel.kind!r}"
        )
    if rel.kind == "every":
        if not rel.cron_expression or not rel.cron_expression.strip():
            raise TemporalRelationError(
                "temporal_relation.kind='every' requires a non-empty "
                "cron_expression"
            )
        if rel.minutes != 0:
            raise TemporalRelationError(
                "temporal_relation.kind='every' must have minutes=0; "
                f"got minutes={rel.minutes}"
            )
    elif rel.kind in ("before", "after"):
        if rel.minutes <= 0:
            raise TemporalRelationError(
                f"temporal_relation.kind={rel.kind!r} requires "
                f"minutes > 0; got {rel.minutes}"
            )
        if rel.cron_expression:
            raise TemporalRelationError(
                f"temporal_relation.kind={rel.kind!r} must not "
                "carry a cron_expression"
            )
    elif rel.kind == "on":
        if rel.minutes != 0 or rel.cron_expression:
            raise TemporalRelationError(
                "temporal_relation.kind='on' must have minutes=0 "
                "and empty cron_expression"
            )


# ---------------------------------------------------------------------------
# DispatchPolicy
# ---------------------------------------------------------------------------


_MISSED_WINDOW_VALUES = frozenset({"skip", "catch_up"})


@dataclass(frozen=True)
class DispatchPolicy:
    """How a fire actually happens when conditions match.

    * ``dedup_window_seconds`` — within this window, the same trigger
      + same Y-match key cannot fire twice. Default 300s.
    * ``missed_window`` — ``'skip'`` (default) | ``'catch_up'``. the design review
      must-fix W10. ``catch_up`` fires exactly once per actual missed
      window keyed by the missed window itself, not by the restart
      event (per the design review's post-fold AC7 wording).
    * ``retry_on_dispatch_failure`` — bounded retry count for the
      runtime → WLP handoff. NOT for execution failures inside
      workflows; those route through WLP's own retry posture.
    """

    dedup_window_seconds: int = 300
    missed_window: Literal["skip", "catch_up"] = "skip"
    retry_on_dispatch_failure: int = 3


def validate_dispatch_policy(policy: DispatchPolicy) -> None:
    """Raise :class:`DispatchPolicyError` on invalid combinations."""
    if policy.dedup_window_seconds < 0:
        raise DispatchPolicyError(
            f"dedup_window_seconds must be >= 0; got "
            f"{policy.dedup_window_seconds}"
        )
    if policy.missed_window not in _MISSED_WINDOW_VALUES:
        raise DispatchPolicyError(
            f"missed_window must be one of "
            f"{sorted(_MISSED_WINDOW_VALUES)}; got "
            f"{policy.missed_window!r}"
        )
    if policy.retry_on_dispatch_failure < 0:
        raise DispatchPolicyError(
            f"retry_on_dispatch_failure must be >= 0; got "
            f"{policy.retry_on_dispatch_failure}"
        )


# ---------------------------------------------------------------------------
# TriggerPredicate (the registered unit)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriggerPredicate:
    """The unit registered with :class:`TriggerEvaluationRuntime`.

    Codex deliberation D2 pin: event-vs-time evaluation path is
    DERIVED from ``temporal_relation.kind``, not a public axis. Both
    paths converge at the same durable claim_fire / dispatch
    runtime (D7).
    """

    event_selector: EventSelector
    temporal_relation: TemporalRelation
    dispatch_policy: DispatchPolicy = field(default_factory=DispatchPolicy)


def validate_predicate(pred: TriggerPredicate) -> None:
    """Top-level validation. Raises a
    :class:`PredicateValidationError` subclass when shape is
    invalid. Atomic-register invariant: register() calls this
    BEFORE any persistence work."""
    if not isinstance(pred, TriggerPredicate):
        raise PredicateValidationError(
            f"validate_predicate expected TriggerPredicate; got "
            f"{type(pred).__name__}"
        )
    if not isinstance(pred.event_selector, dict):
        raise PredicateValidationError(
            "event_selector must be a dict (PredicateAST shape)"
        )
    validate_temporal(pred.temporal_relation)
    validate_dispatch_policy(pred.dispatch_policy)


# ---------------------------------------------------------------------------
# fire_window_key — deterministic idempotency key
# ---------------------------------------------------------------------------


def fire_window_key_for_every(
    cron_expression: str, normalized_fire_time_iso: str,
) -> str:
    """``every(cron)`` window key. ``normalized_fire_time_iso`` is
    the cron's intended fire time bucketed to the cron's
    resolution — NOT now()."""
    return f"every::{cron_expression}::{normalized_fire_time_iso}"


def fire_window_key_for_on(y_event_id: str) -> str:
    """``on(Y)`` window key — Y identified by its substrate event_id
    which is durable across restart."""
    return f"on::{y_event_id}"


def fire_window_key_for_before(y_event_id: str, minutes: int) -> str:
    """``before(Y, minutes=N)`` window key."""
    return f"before::{y_event_id}::{minutes}"


def fire_window_key_for_after(y_event_id: str, minutes: int) -> str:
    """``after(Y, minutes=N)`` window key."""
    return f"after::{y_event_id}::{minutes}"


def derive_fire_id(trigger_id: str, fire_window_key: str) -> str:
    """Application-layer fire identity. Same inputs produce the
    same fire_id across processes — recovery and restart can
    reference a fire by id without ambiguity. Uses a SHA hex
    truncation so the value is bounded length and printable.
    """
    if not trigger_id or not fire_window_key:
        raise ValueError(
            "derive_fire_id requires non-empty trigger_id and "
            "fire_window_key"
        )
    raw = f"{trigger_id}::{fire_window_key}".encode("utf-8")
    return f"fire_{hashlib.sha256(raw).hexdigest()[:16]}"


__all__ = [
    "DispatchPolicy",
    "EventSelector",
    "TemporalRelation",
    "TriggerPredicate",
    "derive_fire_id",
    "fire_window_key_for_after",
    "fire_window_key_for_before",
    "fire_window_key_for_every",
    "fire_window_key_for_on",
    "validate_dispatch_policy",
    "validate_predicate",
    "validate_temporal",
]
