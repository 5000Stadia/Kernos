"""Translate ``descriptor.triggers`` entries into :class:`TriggerPredicate`.

WTC v1 C5a substrate. CRB Compiler's structural-shape pass
(``assert_triggers_well_formed``) only enforces "list of dicts
with event_type"; the actual three-part predicate model
(event_selector / temporal_relation / dispatch_policy) lives
here in the trigger runtime.

Why a separate adapter rather than baking translation into the
CRB Compiler: the runtime owns the :class:`TriggerPredicate`
shape. If the predicate model evolves (v1.x adds a new temporal
kind, for example), only this adapter needs to know — the CRB
Compiler keeps producing the same descriptor shape.

Accepted descriptor.triggers entries:

1. **Minimal (legacy / Drafter v1):**

   .. code-block:: python

       {"event_type": "user.message"}

   Translates to ``on(event_type==user.message)`` with default
   :class:`DispatchPolicy`. Keeps the existing CRB v1 fixtures
   working without modification.

2. **Rich (WTC v1):**

   .. code-block:: python

       {
           "event_type": "user.message",
           "event_selector": {<AST>},          # optional override
           "temporal_relation": {
               "kind": "every", "cron_expression": "*/5 * * * *",
           },
           "dispatch_policy": {
               "dedup_window_seconds": 60,
               "missed_window": "skip",
               "retry_on_dispatch_failure": 3,
           },
       }

   Each of the three rich fields is optional with documented
   defaults. ``event_type`` remains required (the CRB Compiler's
   shape assertion already enforces this; we re-check defensively).

Translation contract:

* Pure deterministic function. Same input dict → same
  :class:`TriggerPredicate`. No I/O, no LLM, no clock reads.
* Errors raise :class:`TriggerError` (concretely
  :class:`TemporalRelationError` / :class:`DispatchPolicyError`)
  so callers can distinguish CRB-Compiler-level shape issues
  (raised earlier by the existing CRB shape assertions) from
  runtime-level model issues (raised here).

Out of scope for C5a: STS ``register_workflow`` integration that
calls this and registers each translated predicate atomically
with the workflow. That wiring lands in C5b once the integration
test shape is locked.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from kernos.kernel.triggers.errors import (
    DispatchPolicyError,
    PredicateValidationError,
    TemporalRelationError,
)
from kernos.kernel.triggers.predicate import (
    DispatchPolicy,
    TemporalRelation,
    TriggerPredicate,
    validate_dispatch_policy,
    validate_predicate,
    validate_temporal,
)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def _default_event_selector(event_type: str) -> dict[str, Any]:
    """Default selector when the trigger descriptor doesn't supply
    its own: match on event_type equality."""
    return {"op": "eq", "path": "event_type", "value": event_type}


def _default_temporal_relation() -> TemporalRelation:
    """Default temporal posture is ``on(Y)`` — fire when Y is observed."""
    return TemporalRelation(kind="on")


def _default_dispatch_policy() -> DispatchPolicy:
    """Default policy: substrate-level :class:`DispatchPolicy` defaults."""
    return DispatchPolicy()


# ---------------------------------------------------------------------------
# Compiled trigger record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledTrigger:
    """A descriptor trigger translated to runtime shape.

    ``trigger_id`` is deterministic over (workflow_id,
    descriptor-trigger-index, predicate fingerprint) so the same
    descriptor always produces the same trigger_id — registration
    is idempotent across re-registrations of the same workflow.
    """

    trigger_id: str
    workflow_id: str
    predicate: TriggerPredicate


# ---------------------------------------------------------------------------
# Helpers — parse temporal_relation / dispatch_policy from dict form
# ---------------------------------------------------------------------------


def _parse_temporal_relation(raw: Any) -> TemporalRelation:
    if raw is None:
        return _default_temporal_relation()
    if isinstance(raw, TemporalRelation):
        return raw
    if not isinstance(raw, dict):
        raise TemporalRelationError(
            f"temporal_relation must be a dict or TemporalRelation; "
            f"got {type(raw).__name__}"
        )
    kind = raw.get("kind")
    if not isinstance(kind, str):
        raise TemporalRelationError(
            "temporal_relation.kind is required and must be a string"
        )
    minutes = raw.get("minutes", 0)
    cron_expression = raw.get("cron_expression", "")
    if not isinstance(minutes, int):
        raise TemporalRelationError(
            f"temporal_relation.minutes must be int; got "
            f"{type(minutes).__name__}"
        )
    if not isinstance(cron_expression, str):
        raise TemporalRelationError(
            f"temporal_relation.cron_expression must be a string; got "
            f"{type(cron_expression).__name__}"
        )
    return TemporalRelation(
        kind=kind, minutes=minutes, cron_expression=cron_expression,
    )


def _parse_dispatch_policy(raw: Any) -> DispatchPolicy:
    if raw is None:
        return _default_dispatch_policy()
    if isinstance(raw, DispatchPolicy):
        return raw
    if not isinstance(raw, dict):
        raise DispatchPolicyError(
            f"dispatch_policy must be a dict or DispatchPolicy; got "
            f"{type(raw).__name__}"
        )
    # Substrate-level DispatchPolicy fields. New fields added in
    # later phases need to be added here defensively.
    kwargs: dict[str, Any] = {}
    for key in (
        "dedup_window_seconds",
        "missed_window",
        "retry_on_dispatch_failure",
    ):
        if key in raw:
            kwargs[key] = raw[key]
    return DispatchPolicy(**kwargs)


# ---------------------------------------------------------------------------
# Trigger ID derivation — deterministic over workflow + index + predicate
# ---------------------------------------------------------------------------


def derive_trigger_id(
    *,
    workflow_id: str,
    index: int,
    predicate: TriggerPredicate,
) -> str:
    """Derive a deterministic trigger_id for a compiled descriptor
    trigger. Same workflow + same descriptor position + same
    predicate shape → same trigger_id, so re-registration of an
    unchanged workflow doesn't churn ids."""
    if not workflow_id:
        raise PredicateValidationError(
            "derive_trigger_id requires non-empty workflow_id"
        )
    fingerprint = hashlib.sha256()
    fingerprint.update(workflow_id.encode("utf-8"))
    fingerprint.update(b"\x00")
    fingerprint.update(str(index).encode("utf-8"))
    fingerprint.update(b"\x00")
    fingerprint.update(repr(predicate.event_selector).encode("utf-8"))
    fingerprint.update(b"\x00")
    fingerprint.update(repr(predicate.temporal_relation).encode("utf-8"))
    fingerprint.update(b"\x00")
    fingerprint.update(repr(predicate.dispatch_policy).encode("utf-8"))
    return f"trig_{fingerprint.hexdigest()[:16]}"


# ---------------------------------------------------------------------------
# Per-trigger compilation
# ---------------------------------------------------------------------------


def compile_trigger_descriptor(trigger: dict) -> TriggerPredicate:
    """Translate a single ``descriptor.triggers`` entry into a
    :class:`TriggerPredicate`.

    The CRB Compiler's structural-shape pass already enforces
    ``"event_type"`` presence; we re-check here so this function
    is safely callable outside the CRB Compiler's pipeline (e.g.,
    from manage_schedule's tool path that builds trigger
    descriptors directly).
    """
    if not isinstance(trigger, dict):
        raise PredicateValidationError(
            f"trigger descriptor must be a dict; got "
            f"{type(trigger).__name__}"
        )
    event_type = trigger.get("event_type")
    if not isinstance(event_type, str) or not event_type:
        raise PredicateValidationError(
            "trigger descriptor missing required 'event_type' field"
        )

    selector_override = trigger.get("event_selector")
    if selector_override is None:
        event_selector = _default_event_selector(event_type)
    elif isinstance(selector_override, dict):
        event_selector = selector_override
    else:
        raise PredicateValidationError(
            f"event_selector must be a dict (predicate AST); got "
            f"{type(selector_override).__name__}"
        )

    temporal_relation = _parse_temporal_relation(
        trigger.get("temporal_relation"),
    )
    dispatch_policy = _parse_dispatch_policy(
        trigger.get("dispatch_policy"),
    )

    predicate = TriggerPredicate(
        event_selector=event_selector,
        temporal_relation=temporal_relation,
        dispatch_policy=dispatch_policy,
    )

    # Substrate-level validation — atomic over all three fields.
    # If any field is invalid, the typed sub-error surfaces. Using
    # validate_predicate consolidates the three sub-validators so a
    # change to either remains in sync.
    validate_temporal(temporal_relation)
    validate_dispatch_policy(dispatch_policy)
    validate_predicate(predicate)

    return predicate


# ---------------------------------------------------------------------------
# Workflow-level compilation
# ---------------------------------------------------------------------------


def compile_descriptor_triggers(
    *,
    workflow_id: str,
    descriptor: dict,
) -> list[CompiledTrigger]:
    """Translate every entry in ``descriptor.triggers`` into a
    :class:`CompiledTrigger`.

    Raises:
        PredicateValidationError: descriptor.triggers missing or
            malformed at the workflow level (not a list, empty, or
            an entry isn't a dict).
        TemporalRelationError / DispatchPolicyError /
        PredicateValidationError: per-trigger shape violations.

    Returns:
        Same length as ``descriptor.triggers``. Caller iterates and
        invokes ``runtime.register(...)`` for each — the wiring
        from STS register_workflow lands in C5b.
    """
    if not workflow_id:
        raise PredicateValidationError(
            "compile_descriptor_triggers requires non-empty workflow_id"
        )
    if not isinstance(descriptor, dict):
        raise PredicateValidationError(
            f"descriptor must be a dict; got {type(descriptor).__name__}"
        )
    triggers = descriptor.get("triggers")
    if not isinstance(triggers, list):
        raise PredicateValidationError(
            f"descriptor.triggers must be a list; got "
            f"{type(triggers).__name__}"
        )
    if not triggers:
        raise PredicateValidationError(
            "descriptor.triggers must be non-empty"
        )

    compiled: list[CompiledTrigger] = []
    for idx, raw in enumerate(triggers):
        try:
            predicate = compile_trigger_descriptor(raw)
        except (
            TemporalRelationError,
            DispatchPolicyError,
            PredicateValidationError,
        ) as exc:
            # Re-raise with descriptor-position context so a
            # malformed entry in a multi-trigger descriptor doesn't
            # surface as an opaque "kind required" error.
            raise type(exc)(
                f"descriptor.triggers[{idx}]: {exc}"
            ) from exc
        trigger_id = derive_trigger_id(
            workflow_id=workflow_id, index=idx, predicate=predicate,
        )
        compiled.append(CompiledTrigger(
            trigger_id=trigger_id,
            workflow_id=workflow_id,
            predicate=predicate,
        ))
    return compiled


__all__ = [
    "CompiledTrigger",
    "compile_descriptor_triggers",
    "compile_trigger_descriptor",
    "derive_trigger_id",
]
