"""Predicate evaluator + four temporal-relation evaluators.

WTC v1 C2 substrate. Both event-driven (``on(Y)``,
``before(Y,N)``, ``after(Y,N)``) and time-driven (``every(cron)``)
paths converge at ``_claim_and_dispatch``: claim the fire window,
call the wired WLP dispatcher with the fire_id, mark dispatched
on success or failed on exhaustion of retries.

The dispatch boundary itself lives in ``runtime.py``. This module
houses the per-kind evaluation logic so the runtime stays small.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from kernos.kernel.event_stream import Event
from kernos.kernel.triggers.predicate import (
    TriggerPredicate,
    fire_window_key_for_after,
    fire_window_key_for_before,
    fire_window_key_for_every,
    fire_window_key_for_on,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cron-time bucketing
# ---------------------------------------------------------------------------


def normalize_cron_fire_time(
    cron_expression: str, fire_time: datetime,
) -> str:
    """Bucket ``fire_time`` to the cron's intended resolution and
    return it in ISO. The fire_window_key for ``every(cron)`` uses
    this — same cron + same minute = same key, so concurrent ticks
    landing in the same minute dedup at the outbox.

    Implementation: ``fire_time`` is the cron's intended fire
    moment (already aligned to the cron grid by croniter). We
    canonicalize to UTC seconds-truncated ISO. Sub-second drift
    from heartbeat scheduling is normalized away.
    """
    if fire_time.tzinfo is None:
        fire_time = fire_time.replace(tzinfo=timezone.utc)
    aligned = fire_time.replace(microsecond=0)
    return aligned.isoformat()


def cron_fires_in_window(
    cron_expression: str,
    *,
    after: datetime,
    until: datetime,
) -> list[datetime]:
    """Return all cron fires that fall in the half-open window
    ``(after, until]``. Caller passes ``after = last_evaluated``
    and ``until = now`` on the heartbeat tick. Empty list when the
    cron has no fires in that window.

    Deferred to a lightweight croniter call. Failures are logged
    and swallowed; the runtime continues with the next predicate.
    """
    try:
        from croniter import croniter
    except ImportError:
        logger.warning(
            "WTC v1 evaluator: croniter not installed; "
            "every(cron) predicates cannot fire"
        )
        return []
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    try:
        cron = croniter(cron_expression, after)
    except Exception as exc:
        logger.warning(
            "WTC v1 evaluator: invalid cron %r — %s",
            cron_expression, exc,
        )
        return []
    fires: list[datetime] = []
    # Bound the loop so a permissive cron doesn't blow memory if
    # the runtime hasn't ticked in months. We cap at 1024 fires
    # per evaluation cycle — well above any sane heartbeat
    # cadence. Beyond the cap, the catch-up semantics in C6 will
    # collapse to "latest missed window."
    for _ in range(1024):
        try:
            nxt = cron.get_next(datetime)
        except Exception:
            break
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        if nxt > until:
            break
        fires.append(nxt)
    return fires


# ---------------------------------------------------------------------------
# Event match — wraps the existing predicate AST evaluator
# ---------------------------------------------------------------------------


def event_matches_selector(
    selector: dict, event: Event,
) -> bool:
    """Run the existing AST evaluator. Returns False when the AST
    is malformed (logged) rather than raising — runtime continues
    to the next predicate."""
    try:
        from kernos.kernel.workflows.predicates import evaluate
        return evaluate(selector, event)
    except Exception as exc:
        logger.warning(
            "WTC v1 evaluator: predicate match raised for "
            "event_id=%s — %s",
            getattr(event, "event_id", "?"), exc,
        )
        return False


# ---------------------------------------------------------------------------
# Pending due-fire queue (before/after temporal relations)
# ---------------------------------------------------------------------------


@dataclass
class PendingDueFire:
    """An ``after(Y, N)`` or ``before(Y, N)`` match has computed a
    fire time but it's in the future. The cron walk drains these
    on each tick — when ``due_at`` has passed, the fire is claimed
    + dispatched.

    fire_window_key derives from ``(kind, Y_event_id, N)`` so the
    same Y observed twice produces the same key — the outbox PK
    catches the duplicate at claim time. No internal dedup needed
    here; the queue can hold duplicates and the loser's claim_fire
    returns None.
    """

    trigger_id: str
    instance_id: str
    workflow_id: str
    fire_window_key: str
    payload: dict[str, Any]
    due_at: datetime
    catch_up: bool = False


def compute_due_at_for_temporal(
    *, kind: str, y_timestamp: str, minutes: int,
) -> datetime | None:
    """Return the absolute ``due_at`` time for a ``before/after``
    match given Y's timestamp and the predicate's offset minutes.
    None when the timestamp can't be parsed."""
    try:
        ts = datetime.fromisoformat(y_timestamp)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if kind == "after":
        return ts + timedelta(minutes=minutes)
    if kind == "before":
        return ts - timedelta(minutes=minutes)
    return None


def fire_window_key_for_temporal_match(
    *, kind: str, y_event_id: str, minutes: int,
) -> str:
    """Pick the right deterministic fire_window_key derivation per
    temporal kind."""
    if kind == "before":
        return fire_window_key_for_before(y_event_id, minutes)
    if kind == "after":
        return fire_window_key_for_after(y_event_id, minutes)
    if kind == "on":
        return fire_window_key_for_on(y_event_id)
    raise ValueError(
        f"fire_window_key_for_temporal_match: unsupported kind {kind!r}"
    )


__all__ = [
    "PendingDueFire",
    "compute_due_at_for_temporal",
    "cron_fires_in_window",
    "event_matches_selector",
    "fire_window_key_for_temporal_match",
    "normalize_cron_fire_time",
]
