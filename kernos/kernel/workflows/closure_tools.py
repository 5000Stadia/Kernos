"""SELF-IMPROVEMENT-CLOSURE-V1 — autonomy-adapter handlers for the
three closure-machinery kernel tools.

Same calling convention as the existing autonomy handlers in
``kernos.kernel.workflows.autonomy_tools``: each handler accepts
``instance_id``, ``member_id``, and ``args`` (the workflow
``call_tool`` action's parameter dict) plus any substrate
dependencies. Returns a dict (the ``value`` payload the workflow
step exposes for downstream ``{step.X.value.Y}`` refs).

These handlers are registered in
``kernos/setup/bring_up_substrate.py:_call_tool_adapter`` for
the closure-machinery tools. Direct access (kernel-tool dispatch
via ``ReasoningService.execute_tool``) goes through
``kernos.kernel.reasoning._handle_closure_tool`` instead.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from kernos.kernel.closure_store import (
    ClosureStore,
    ClosureStoreError,
    lookup_pattern_invariants as _lookup_pattern_invariants,
    record_closure_attempt as _record_closure_attempt,
    run_closure_probe as _run_closure_probe,
)


logger = logging.getLogger(__name__)


async def handle_lookup_pattern_invariants(
    *,
    closure_store: ClosureStore,
    instance_id: str,
    member_id: str,
    args: dict,
) -> dict:
    """Workflow-adapter wrapper for lookup_pattern_invariants.

    Returns the same shape the workflow YAML references:
    ``has_invariants``, ``primary_invariant_id``,
    ``all_invariant_ids``.
    """
    pattern_id = args.get("pattern_id", "")
    if not pattern_id:
        raise ClosureStoreError(
            "lookup_pattern_invariants: pattern_id is required"
        )
    return await _lookup_pattern_invariants(
        store=closure_store,
        instance_id=instance_id,
        pattern_id=pattern_id,
    )


async def handle_record_closure_attempt(
    *,
    closure_store: ClosureStore,
    instance_id: str,
    member_id: str,
    args: dict,
) -> dict:
    """Workflow-adapter wrapper for record_closure_attempt.

    Per AC4: idempotent on (instance, pattern, invariant, episode).
    Per AC13: raises InvariantNotLinkedToPattern when no link row.
    Per AC8: hard-rejects probe_kind outside READ_ONLY_PROBE_KINDS.
    """
    pattern_id = args.get("pattern_id", "")
    invariant_id = args.get("invariant_id", "")
    if not pattern_id or not invariant_id:
        raise ClosureStoreError(
            "record_closure_attempt: pattern_id + invariant_id required"
        )
    return await _record_closure_attempt(
        store=closure_store,
        instance_id=instance_id,
        pattern_id=pattern_id,
        invariant_id=invariant_id,
        active_epoch=int(args.get("active_epoch", 0)),
        route=args.get("route", "code_change_via_cc"),
        route_payload=args.get("route_payload") or {},
        probe_kind=args.get("probe_kind", "deterministic_introspection"),
        probe_payload=args.get("probe_payload") or {},
        probe_payload_version=int(args.get("probe_payload_version", 1)),
    )


async def handle_run_closure_probe(
    *,
    closure_store: ClosureStore,
    instance_id: str,
    member_id: str,
    args: dict,
    pattern_transition_fn: Optional[Callable[..., Any]] = None,
    event_emit_fn: Optional[Callable[..., Any]] = None,
) -> dict:
    """Workflow-adapter wrapper for run_closure_probe.

    Per AC14: idempotent replay — returns stored outcome +
    ``replayed=True`` for non-pending rows without re-executing the
    probe, re-transitioning the pattern, or re-emitting
    ``closure.probe_failed``.

    On pending + pass: calls ``pattern_transition_fn`` to transition
    the friction pattern to ``resolved``. On pending + fail: calls
    ``event_emit_fn`` to emit ``closure.probe_failed`` with
    structured evidence.
    """
    closure_id = args.get("closure_id", "")
    if not closure_id:
        raise ClosureStoreError(
            "run_closure_probe: closure_id is required"
        )
    return await _run_closure_probe(
        store=closure_store,
        instance_id=instance_id,
        closure_id=closure_id,
        pattern_transition_fn=pattern_transition_fn,
        event_emit_fn=event_emit_fn,
    )
