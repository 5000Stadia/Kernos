"""Friction observer — write-only sink for tier-1/2 exhaustion (PDI C6).

Per spec Section 5g (and design review's C6 guidance):
  - The friction observer accumulates tickets describing tier-1 retry
    or tier-2 modify exhaustion on a specific (tool, operation) pair
    within a single turn.
  - **Write-only.** Tickets do NOT affect dispatch; do NOT
    short-circuit retry/modify; do NOT inform routing in v1.
    Operators read tickets to identify problematic tools and
    divergence patterns.
  - Architectural shape: observer with no feedback loop. v1 contract.

The Protocol's record() method returns None — there is no return-
value channel through which a ticket could affect subsequent
behaviour. Implementations write to a store / log / external sink;
the EnactmentService never reads back.

Composes with the existing Phase 6A friction observer primitive in
the broader Kernos codebase. The Protocol here is the enactment-side
contract; concrete adapters bridge to that primitive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# DivergencePattern — closed taxonomy
# ---------------------------------------------------------------------------


# Stable string labels for the tier-exhaustion patterns the friction
# observer records. Closed set so audit consumers and operator
# dashboards can filter cleanly. Adding new patterns is a coordinated
# extension; the existing two cover v1.
TIER_1_RETRY_EXHAUSTED = "tier_1_retry_exhausted"
TIER_2_MODIFY_EXHAUSTED = "tier_2_modify_exhausted"


# ---------------------------------------------------------------------------
# FrictionTicket
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrictionTicket:
    """A single record of tier-1/2 exhaustion within a turn.

    `tool_id`, `operation_name`: which tool surface struggled.
    `divergence_pattern`: TIER_1_RETRY_EXHAUSTED or TIER_2_MODIFY_EXHAUSTED.
    `attempt_count`: total attempts before the tier exhausted.
    `decided_action_kind`: forwarded for telemetry context.
    `instance_id`, `member_id`, `turn_id`: routing identifiers.
    `timestamp`: ISO 8601 UTC of when the ticket was written.

    Frozen so a ticket cannot be mutated after recording. Operators
    consume by aggregating across turns / instances.
    """

    tool_id: str
    operation_name: str
    divergence_pattern: str
    attempt_count: int
    decided_action_kind: str
    instance_id: str
    member_id: str
    turn_id: str
    timestamp: str

    def to_dict(self) -> dict:
        return {
            "tool_id": self.tool_id,
            "operation_name": self.operation_name,
            "divergence_pattern": self.divergence_pattern,
            "attempt_count": self.attempt_count,
            "decided_action_kind": self.decided_action_kind,
            "instance_id": self.instance_id,
            "member_id": self.member_id,
            "turn_id": self.turn_id,
            "timestamp": self.timestamp,
        }


def now_iso() -> str:
    """ISO 8601 UTC timestamp helper."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Protocol — write-only sink contract
# ---------------------------------------------------------------------------


@runtime_checkable
class FrictionObserverLike(Protocol):
    """Write-only sink. record() returns None.

    The Protocol is intentionally minimal: a single async record()
    method that takes a ticket and returns nothing. There is no
    query method on the Protocol — by construction, the
    EnactmentService cannot read back tickets to influence routing.
    Operators (and a future improvement-loop primitive) read tickets
    via separate query surfaces on the underlying store, not through
    this Protocol.
    """

    async def record(self, ticket: FrictionTicket) -> None: ...


# ---------------------------------------------------------------------------
# Null observer — used when no friction sink is wired
# ---------------------------------------------------------------------------


class NullFrictionObserver:
    """No-op observer.

    Used as the default when EnactmentService is constructed without
    a real friction sink. Calls succeed silently; tickets vanish.
    Useful for tests and for environments where operator
    diagnostics are out of scope.
    """

    async def record(self, ticket: FrictionTicket) -> None:
        return None


__all__ = [
    "FrictionObserverLike",
    "FrictionTicket",
    "NullFrictionObserver",
    "TIER_1_RETRY_EXHAUSTED",
    "TIER_2_MODIFY_EXHAUSTED",
    "now_iso",
]
