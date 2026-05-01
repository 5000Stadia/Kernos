"""Reentrancy policy for cross-space requests.

Independent of :mod:`kernos.kernel.external_agents.reentrancy`'s
``_POLICY`` (consult). Each tool family carries its own depth budget
to keep limits clear and audit-traceable; a single CONVERSATIONAL
turn may consume one cross-space request AND up to the allowed
number of consults independently.

Depth ceiling = 1: a request running inside a target space cannot
itself issue a cross-space request. The policy table rejects
recursion structurally, so callers don't have to handle it.
"""
from __future__ import annotations

import contextvars

from kernos.kernel.external_agents.errors import (
    DepthExceeded,
    ReentrancyBlocked,
)
from kernos.kernel.external_agents.reentrancy import (
    CallingContext,
    current_calling_context,
)


# v1 policy table. Allowed contexts have a depth limit; blocked
# contexts have None (no calls permitted).
_CROSS_SPACE_POLICY: dict[CallingContext, int | None] = {
    CallingContext.CONVERSATIONAL: 1,    # one cross-space per turn
    CallingContext.WLP_EXECUTION:  1,    # one per plan step (future plan-step integration)
    CallingContext.DRAFTER:        None,
    CallingContext.CRB_DISPATCH:   None,
    CallingContext.TRIGGER_EVAL:   None,
    CallingContext.COMPACTION:     None,
    CallingContext.RECOVERY_SWEEP: None,
    CallingContext.UNKNOWN:        None,  # safe default
}


_cross_space_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "cross_space.depth", default=0,
)


def current_cross_space_depth() -> int:
    return _cross_space_depth.get()


def enter_cross_space() -> contextvars.Token[int]:
    """Called by the kernel BEFORE executing a cross-space request.
    Raises :class:`ReentrancyBlocked` when the calling context
    disallows cross-space; raises :class:`DepthExceeded` when the
    depth ceiling has been hit (recursion).

    Returns a token the caller MUST reset (via
    :func:`exit_cross_space`) when the request completes — typically
    in a ``finally`` block.
    """
    ctx = current_calling_context()
    limit = _CROSS_SPACE_POLICY.get(ctx)
    if limit is None:
        allowed = ", ".join(
            sorted(c.value for c, l in _CROSS_SPACE_POLICY.items() if l is not None)
        )
        raise ReentrancyBlocked(
            f"cross-space request is not permitted from calling "
            f"context {ctx.value!r}. Allowed contexts: {allowed}"
        )
    depth = current_cross_space_depth()
    if depth >= limit:
        raise DepthExceeded(
            f"cross-space depth {depth} >= limit {limit} for context "
            f"{ctx.value!r}; rejecting recursion"
        )
    return _cross_space_depth.set(depth + 1)


def exit_cross_space(token: contextvars.Token[int]) -> None:
    _cross_space_depth.reset(token)


__all__ = [
    "current_cross_space_depth",
    "enter_cross_space",
    "exit_cross_space",
]
