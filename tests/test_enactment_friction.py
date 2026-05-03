"""Tests for the friction observer (PDI C6).

Per design review's C6 guidance: write-only sink. Tickets accumulate;
they do NOT affect dispatch. The Protocol exposes only record() —
no query method through which a ticket could feed back to routing.
"""

from __future__ import annotations

import pytest

from kernos.kernel.enactment.friction import (
    FrictionObserverLike,
    FrictionTicket,
    NullFrictionObserver,
    TIER_1_RETRY_EXHAUSTED,
    TIER_2_MODIFY_EXHAUSTED,
    now_iso,
)


# ---------------------------------------------------------------------------
# FrictionTicket shape
# ---------------------------------------------------------------------------


def test_friction_ticket_round_trip_via_dict():
    ticket = FrictionTicket(
        tool_id="email_send",
        operation_name="send",
        divergence_pattern=TIER_1_RETRY_EXHAUSTED,
        attempt_count=4,
        decided_action_kind="execute_tool",
        instance_id="inst-1",
        member_id="mem-1",
        turn_id="turn-1",
        timestamp=now_iso(),
    )
    payload = ticket.to_dict()
    assert payload["tool_id"] == "email_send"
    assert payload["divergence_pattern"] == TIER_1_RETRY_EXHAUSTED
    assert payload["attempt_count"] == 4


def test_divergence_patterns_locked():
    """Closed taxonomy — adding a pattern is a coordinated extension."""
    assert TIER_1_RETRY_EXHAUSTED == "tier_1_retry_exhausted"
    assert TIER_2_MODIFY_EXHAUSTED == "tier_2_modify_exhausted"


# ---------------------------------------------------------------------------
# Protocol contract — write-only
# ---------------------------------------------------------------------------


def test_protocol_exposes_only_record():
    """FrictionObserverLike must have only record() — no query/read
    method that could feed back to the EnactmentService."""
    methods = {
        name for name in dir(FrictionObserverLike)
        if not name.startswith("_")
        and callable(getattr(FrictionObserverLike, name, None))
    }
    assert methods == {"record"}, (
        f"FrictionObserverLike must expose only record(); found {methods}"
    )


def test_record_returns_none():
    """The record() return type is None. There is no return-value
    channel through which a ticket could affect subsequent routing."""
    import inspect
    sig = inspect.signature(FrictionObserverLike.record)
    assert sig.return_annotation in (None, type(None), "None")


# ---------------------------------------------------------------------------
# NullFrictionObserver — default when no real sink wired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_observer_swallows_tickets():
    observer = NullFrictionObserver()
    ticket = FrictionTicket(
        tool_id="t",
        operation_name="o",
        divergence_pattern=TIER_1_RETRY_EXHAUSTED,
        attempt_count=1,
        decided_action_kind="execute_tool",
        instance_id="",
        member_id="",
        turn_id="t1",
        timestamp=now_iso(),
    )
    result = await observer.record(ticket)
    assert result is None


def test_null_observer_conforms_to_protocol():
    assert isinstance(NullFrictionObserver(), FrictionObserverLike)


# ---------------------------------------------------------------------------
# Concrete observer instance — record-only contract
# ---------------------------------------------------------------------------


class _RecordingObserver:
    def __init__(self):
        self.tickets = []

    async def record(self, ticket):
        self.tickets.append(ticket)


@pytest.mark.asyncio
async def test_recording_observer_accumulates_tickets():
    observer = _RecordingObserver()
    for i in range(3):
        await observer.record(
            FrictionTicket(
                tool_id=f"tool-{i}",
                operation_name="op",
                divergence_pattern=TIER_1_RETRY_EXHAUSTED,
                attempt_count=i + 1,
                decided_action_kind="execute_tool",
                instance_id="i",
                member_id="m",
                turn_id="t",
                timestamp=now_iso(),
            )
        )
    assert len(observer.tickets) == 3


def test_recording_observer_conforms_to_protocol():
    assert isinstance(_RecordingObserver(), FrictionObserverLike)
