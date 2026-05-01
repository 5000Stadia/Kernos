"""Reentrancy / context policy for external-agent consultation.

Codex pre-spec D7 pin: consultations CAN fan out recursively
(consult-within-consult; runtime-evaluator-spawning-consult; etc.)
without guardrails. v1 declares which Kernos calling contexts may
invoke the ``consult`` tool and enforces a depth limit per context.

Implemented via :class:`contextvars.ContextVar` (NOT thread-local —
asyncio uses contextvars for per-task isolation; thread-locals leak
across awaits). Calling-context entries set/reset the var with a
token so nested + concurrent async tasks have independent state.

Usage from a calling context (e.g. the message handler):

    from kernos.kernel.external_agents.reentrancy import (
        CallingContext, set_calling_context,
    )

    token = set_calling_context(CallingContext.CONVERSATIONAL)
    try:
        ...handle the turn...
    finally:
        reset_calling_context(token)

The ``consult`` tool reads :func:`current_calling_context` and
:func:`current_consult_depth` to decide whether to permit the call;
it raises :class:`ReentrancyBlocked` or :class:`DepthExceeded` when
the policy rejects.
"""
from __future__ import annotations

import contextvars
from enum import Enum

from kernos.kernel.external_agents.errors import (
    DepthExceeded,
    ReentrancyBlocked,
)


class CallingContext(Enum):
    """Where the current async task is running. Used by the
    reentrancy guard to decide whether ``consult`` is permitted."""

    UNKNOWN = "unknown"
    CONVERSATIONAL = "conversational"
    DRAFTER = "drafter"
    COMPACTION = "compaction"
    CRB_DISPATCH = "crb_dispatch"
    TRIGGER_EVAL = "trigger_eval"
    WLP_EXECUTION = "wlp_execution"
    RECOVERY_SWEEP = "recovery_sweep"


# v1 policy table. Allowed contexts have a depth limit; blocked
# contexts have None (no calls permitted).
_POLICY: dict[CallingContext, int | None] = {
    CallingContext.CONVERSATIONAL: 2,
    CallingContext.DRAFTER: 1,
    CallingContext.COMPACTION: None,
    CallingContext.CRB_DISPATCH: None,
    CallingContext.TRIGGER_EVAL: None,
    CallingContext.WLP_EXECUTION: None,
    CallingContext.RECOVERY_SWEEP: None,
    CallingContext.UNKNOWN: None,  # safe default — nothing can call
                                   # without an explicit context entry
}


_calling_context: contextvars.ContextVar[CallingContext] = (
    contextvars.ContextVar(
        "external_agents.calling_context",
        default=CallingContext.UNKNOWN,
    )
)
_consult_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "external_agents.consult_depth", default=0,
)


def set_calling_context(
    ctx: CallingContext,
) -> contextvars.Token[CallingContext]:
    """Mark the current async task's calling context. Returns a token
    the caller MUST reset (via :func:`reset_calling_context`) when
    leaving the context — typically in a ``finally`` block."""
    return _calling_context.set(ctx)


def reset_calling_context(
    token: contextvars.Token[CallingContext],
) -> None:
    _calling_context.reset(token)


def current_calling_context() -> CallingContext:
    return _calling_context.get()


def current_consult_depth() -> int:
    return _consult_depth.get()


def enter_consult() -> contextvars.Token[int]:
    """Called by the consult tool BEFORE invoking a harness. Raises
    :class:`ReentrancyBlocked` if the calling context disallows
    consultation; raises :class:`DepthExceeded` if the depth limit
    has been hit. On success returns a token for the caller to
    reset on exit."""
    ctx = current_calling_context()
    limit = _POLICY.get(ctx)
    if limit is None:
        raise ReentrancyBlocked(
            f"external-agent consultation is not permitted from "
            f"calling context {ctx.value!r}. Allowed contexts: "
            + ", ".join(
                sorted(c.value for c, l in _POLICY.items() if l is not None)
            )
        )
    depth = current_consult_depth()
    if depth >= limit:
        raise DepthExceeded(
            f"consult depth {depth} >= limit {limit} for context "
            f"{ctx.value!r}; rejecting nested consult"
        )
    return _consult_depth.set(depth + 1)


def exit_consult(token: contextvars.Token[int]) -> None:
    _consult_depth.reset(token)


__all__ = [
    "CallingContext",
    "current_calling_context",
    "current_consult_depth",
    "enter_consult",
    "exit_consult",
    "reset_calling_context",
    "set_calling_context",
]
