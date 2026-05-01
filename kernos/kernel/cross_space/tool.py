"""Agent-facing tool surface for cross-space requests.

Mirrors the shape of CONSULT_TOOL / EXECUTE_CODE_TOOL: a schema
dict the assemble phase puts into the agent's tool catalog, plus a
process-singleton service that holds the dispatch engine. The
dispatch engine bundles state/events/audit/gate dependencies so
each request doesn't re-thread them through every call site.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from kernos.kernel.cross_space.dispatch import (
    DispatchEngine,
    dispatch_request,
)
from kernos.kernel.cross_space.envelopes import (
    ALLOWED_ACTION_KINDS,
    CrossSpaceReceipt,
    CrossSpaceRequest,
    new_request_id,
)

logger = logging.getLogger(__name__)


REQUEST_SPACE_ACTION_TOOL: dict[str, Any] = {
    "name": "request_space_action",
    "description": (
        "Submit a typed, bounded mutation request against another "
        "context space. The kernel evaluates target's covenants and "
        "applies the action under target's rules. You receive a "
        "structured receipt; you do NOT enter or reason inside the "
        "target. Use sparingly and only for one of four action "
        "kinds: write_knowledge, propose_covenant, create_plan_draft, "
        "create_workflow_draft. Cross-member targets are rejected — "
        "use send_relational_message for member-to-member "
        "communication. Recursive cross-space requests are rejected "
        "(depth=1)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target_space_id": {
                "type": "string",
                "description": (
                    "ID of the destination space. Must be in the "
                    "same instance and same member-set as the "
                    "origin space."
                ),
            },
            "action_kind": {
                "type": "string",
                "enum": sorted(ALLOWED_ACTION_KINDS),
                "description": (
                    "Which whitelisted mutation to perform. "
                    "write_knowledge: append a knowledge entry. "
                    "propose_covenant: create a covenant proposal "
                    "(always proposed-not-applied). "
                    "create_plan_draft: create a plan with "
                    "status='draft' (activation requires user "
                    "confirmation in target). "
                    "create_workflow_draft: create a workflow "
                    "descriptor with status='draft'."
                ),
            },
            "work_order": {
                "type": "object",
                "description": (
                    "Structured payload per action_kind. "
                    "write_knowledge: {topic, content, sensitivity, "
                    "tags?}. propose_covenant: {description, scope, "
                    "tier?, context_space?}. create_plan_draft: "
                    "{title, phases[], budget_override?}. "
                    "create_workflow_draft: {descriptor, triggers?, "
                    "actions?, gates?}."
                ),
                "additionalProperties": True,
            },
            "request_id": {
                "type": "string",
                "description": (
                    "Optional. Idempotency key — repeated calls with "
                    "the same request_id return the original "
                    "receipt. Auto-generated when omitted."
                ),
            },
            "safety_class": {
                "type": "string",
                "description": (
                    "Optional. Informational; routes audit category. "
                    "Default 'default'."
                ),
            },
        },
        "required": ["target_space_id", "action_kind", "work_order"],
    },
}


# ---------------------------------------------------------------------------
# Service singleton
# ---------------------------------------------------------------------------


_service_lock = asyncio.Lock()
_service: "CrossSpaceService | None" = None


class CrossSpaceService:
    """Process-singleton wrapping the DispatchEngine. Holds the
    per-(instance, space) lock dict so all cross-space requests
    contend on the same target lock for any given pair."""

    def __init__(self, *, engine: DispatchEngine) -> None:
        self._engine = engine

    @property
    def engine(self) -> DispatchEngine:
        return self._engine

    async def dispatch(
        self, request: CrossSpaceRequest,
    ) -> CrossSpaceReceipt:
        return await dispatch_request(request, self._engine)


async def get_service(
    *,
    state: Any | None = None,
    events: Any | None = None,
    audit: Any | None = None,
    gate: Any | None = None,
    space_locks: Any | None = None,
) -> CrossSpaceService:
    """Return the process-wide service, constructing it on first
    call. Substrate dependencies (state, events, audit, gate,
    space_locks) are captured on first construction; later callers
    pass None and get the same instance.

    ``space_locks`` SHOULD be the handler's
    ``_space_locks: dict[(instance_id, space_id), asyncio.Lock]``
    so the dispatch engine and the turn processor share the same
    lock for any given (instance, space) pair (Q1 invariant).

    Engine bring-up calls this once with the real dependencies; the
    handler / reasoning dispatch sites call it without args.
    """
    global _service
    async with _service_lock:
        if _service is not None:
            return _service
        engine = DispatchEngine(
            state=state, events=events, audit=audit, gate=gate,
            space_locks=space_locks,
        )
        _service = CrossSpaceService(engine=engine)
        logger.info(
            "CROSS_SPACE_SERVICE_STARTED state=%s events=%s audit=%s gate=%s shared_locks=%s",
            "yes" if state else "no", "yes" if events else "no",
            "yes" if audit else "no", "yes" if gate else "no",
            "yes" if space_locks is not None else "no",
        )
        return _service


async def reset_service_for_tests() -> None:
    """Test helper: clear the singleton so the next ``get_service``
    call rebuilds. Production callers must not use this."""
    global _service
    async with _service_lock:
        _service = None


def build_request_from_tool_input(
    *,
    tool_input: dict[str, Any],
    instance_id: str,
    origin_space_id: str,
    initiating_member_id: str,
    source_turn_id: str,
) -> CrossSpaceRequest:
    """Translate the agent's tool input dict into a
    CrossSpaceRequest envelope with origin context filled in."""
    return CrossSpaceRequest(
        request_id=str(tool_input.get("request_id") or new_request_id()),
        origin_space_id=origin_space_id,
        target_space_id=str(tool_input.get("target_space_id", "")),
        initiating_member_id=initiating_member_id,
        source_turn_id=source_turn_id,
        action_kind=str(tool_input.get("action_kind", "")),
        work_order=dict(tool_input.get("work_order") or {}),
        instance_id=instance_id,
        safety_class=str(tool_input.get("safety_class", "default")),
    )


__all__ = [
    "CrossSpaceService",
    "REQUEST_SPACE_ACTION_TOOL",
    "build_request_from_tool_input",
    "get_service",
    "reset_service_for_tests",
]
