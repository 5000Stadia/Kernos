"""Bridge / AgentInbox interface.

The ``route_to_agent`` action library verb writes payloads to an
AgentInbox provider. Multiple concrete implementations are possible
(Notion, local folder, S3, etc.); the workflow primitive itself only
depends on the abstract Protocol. v1 ships ``NotionAgentInbox`` as
the available concrete during the developing-Kernos period;
installations choose whether to bind it.

Provider-configuration-containment (the design review edit, narrow review):
``route_to_agent`` depends on a configured ``AgentInbox`` provider
rather than a hardcoded default. The action library's
``RouteToAgentAction`` raises ``AgentInboxUnavailable`` with a clear
message if no provider is bound. Pin: structural test verifies the
explicit unavailable-error path rather than silent fall-through to
Notion.

Provider-independence pin: only the concrete inbox class at the
bottom of this file may carry vendor-specific imports / URLs / tool
namespaces. The Protocol itself MUST stay provider-neutral.
Structural test scans the workflows + webhooks source tree for
vendor-integration patterns and only allows them inside the
concrete-inbox class body.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


class AgentInboxUnavailable(RuntimeError):
    """Raised when ``route_to_agent`` is invoked without a bound
    AgentInbox provider. Caller should treat this as a workflow
    failure mode, not a silent fallback."""


# ---------------------------------------------------------------------------
# Approval-request payload block (WLP-GATE-SCOPING C2)
#
# When a workflow's gate_ref action is a ``route_to_agent`` posting an
# approval-shape message, the action's payload includes a documented
# ``approval_request`` block. Receiving agents (owner UI, design review
# agent, future agents) read this block to know what response to
# compose; the response event MUST carry ``execution_id`` and
# ``gate_nonce`` in its payload so the engine's match logic resumes
# the right paused execution.
#
# This is **documented schema, not enforced wire format**. The
# engine's `_on_post_flush_for_gates` match logic enforces the
# ``execution_id`` + ``gate_nonce`` binding; the AgentInbox just
# carries the request data verbatim. Any concrete inbox provider
# is free in how it serialises the block, as long as receivers
# can read it back.
#
# A descriptor authoring an approval gate via route_to_agent uses
# template interpolation (`{workflow.execution_id}`,
# `{workflow.gate_nonce}`) so the engine fills in the runtime values
# before dispatching the verb.
# ---------------------------------------------------------------------------


APPROVAL_REQUEST_KEY = "approval_request"
"""Key inside an AgentInbox payload that carries the approval-request
block. Receivers look up `payload[APPROVAL_REQUEST_KEY]` to read it."""


APPROVAL_REQUEST_FIELDS = (
    "execution_id",
    "gate_nonce",
    "gate_name",
    "pause_reason",
    "response_event_type",
    "response_predicate",
)
"""The six fields a complete approval_request block carries.

Authoring example (descriptor `route_to_agent.parameters.payload`)::

    payload:
      approval_request:
        execution_id: "{workflow.execution_id}"
        gate_nonce:   "{workflow.gate_nonce}"
        gate_name:    "approve_deploy"
        pause_reason: "owner must confirm before publishing"
        response_event_type: "user.approval"
        response_predicate:
          op: actor_eq
          value: owner

The engine substitutes ``{workflow.execution_id}`` /
``{workflow.gate_nonce}`` before dispatching the action. The receiver
echoes both back in its response event payload to satisfy the
engine's match logic.
"""


def is_approval_request_payload(payload: dict) -> bool:
    """Return True if the payload carries a complete approval_request
    block. Useful for inbox implementations that route differently
    based on payload shape (e.g. surfacing approvals in a dedicated
    UI lane)."""
    if not isinstance(payload, dict):
        return False
    block = payload.get(APPROVAL_REQUEST_KEY)
    if not isinstance(block, dict):
        return False
    return all(field in block for field in APPROVAL_REQUEST_FIELDS)


@dataclass
class InboxPostResult:
    """Receipt returned from ``AgentInbox.post``. Verifier reads the
    receipt's ``persisted_id`` back from the inbox surface to confirm
    intent-satisfaction."""

    persisted_id: str
    posted_at: str
    metadata: dict = field(default_factory=dict)


@dataclass
class InboxItem:
    """A single payload retrieved from an inbox. ``persisted_id``
    matches the receipt that ``post`` returned."""

    persisted_id: str
    agent_id: str
    payload: dict
    posted_at: str
    metadata: dict = field(default_factory=dict)


class AgentInbox(Protocol):
    """Abstract bridge for routing payloads to agents.

    Implementations MUST be deterministic about ``persisted_id``
    issuance: a successful post returns a unique stable id that a
    later ``read`` can match. This is what makes the
    ``route_to_agent`` verifier work — the verifier reads the inbox
    by agent_id and checks that the posted ``persisted_id`` is
    visible.
    """

    async def post(
        self, agent_id: str, payload: dict, *, instance_id: str = "",
    ) -> InboxPostResult:
        ...

    async def read(
        self,
        agent_id: str,
        *,
        since: datetime | None = None,
        instance_id: str = "",
    ) -> list[InboxItem]:
        ...


class InMemoryAgentInbox:
    """Test-grade concrete useful for action-library unit tests and
    the C5 execution engine's integration tests. Not intended for
    production routing; production installations bind
    :class:`NotionAgentInbox` (or a future LocalFolderAgentInbox).

    Keys items by (instance_id, agent_id) so multi-tenant tests
    don't cross streams.
    """

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], list[InboxItem]] = {}
        self._counter = 0

    async def post(
        self, agent_id: str, payload: dict, *, instance_id: str = "",
    ) -> InboxPostResult:
        self._counter += 1
        persisted_id = f"in-mem-{self._counter}"
        posted_at = datetime.now(timezone.utc).isoformat()
        item = InboxItem(
            persisted_id=persisted_id,
            agent_id=agent_id,
            payload=payload,
            posted_at=posted_at,
        )
        self._items.setdefault((instance_id, agent_id), []).append(item)
        return InboxPostResult(persisted_id=persisted_id, posted_at=posted_at)

    async def read(
        self,
        agent_id: str,
        *,
        since: datetime | None = None,
        instance_id: str = "",
    ) -> list[InboxItem]:
        items = list(self._items.get((instance_id, agent_id), ()))
        if since is not None:
            cutoff = since.isoformat()
            items = [i for i in items if i.posted_at >= cutoff]
        return items


class NotionAgentInbox:
    """Concrete AgentInbox backed by Notion bridge inbox databases.

    Construction takes a callable that posts to the configured
    Notion inbox database and another that reads — operator-supplied
    at install time. Both should be Notion-tool-shaped invocations.
    Provider-configuration-containment is enforced at the
    route_to_agent level: if no NotionAgentInbox (or other
    AgentInbox) is bound, the verb fails loudly via
    ``AgentInboxUnavailable``. The action library's
    Notion-independence pin intentionally allows the string
    ``notion`` to appear in this file because this IS the Notion
    concrete; the pin's scan excludes this module.
    """

    def __init__(
        self,
        post_fn: Any | None = None,
        read_fn: Any | None = None,
    ) -> None:
        self._post_fn = post_fn
        self._read_fn = read_fn

    async def post(
        self, agent_id: str, payload: dict, *, instance_id: str = "",
    ) -> InboxPostResult:
        if self._post_fn is None:
            raise AgentInboxUnavailable(
                "NotionAgentInbox.post called without a configured post_fn — "
                "the operator must bind a Notion-backed callable at install time."
            )
        result = await self._post_fn(agent_id, payload, instance_id=instance_id)
        if isinstance(result, InboxPostResult):
            return result
        # Caller-supplied callable may return a dict; normalise.
        return InboxPostResult(
            persisted_id=str(result.get("persisted_id", "")),
            posted_at=str(result.get("posted_at", "")),
            metadata=result.get("metadata") or {},
        )

    async def read(
        self,
        agent_id: str,
        *,
        since: datetime | None = None,
        instance_id: str = "",
    ) -> list[InboxItem]:
        if self._read_fn is None:
            raise AgentInboxUnavailable(
                "NotionAgentInbox.read called without a configured read_fn"
            )
        items = await self._read_fn(agent_id, since=since, instance_id=instance_id)
        return [i if isinstance(i, InboxItem) else InboxItem(**i) for i in items]


__all__ = [
    "APPROVAL_REQUEST_FIELDS",
    "APPROVAL_REQUEST_KEY",
    "AgentInbox",
    "AgentInboxUnavailable",
    "InboxItem",
    "InboxPostResult",
    "InMemoryAgentInbox",
    "NotionAgentInbox",
    "is_approval_request_payload",
]
