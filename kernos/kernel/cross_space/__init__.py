"""CROSS_SPACE_REQUESTS_V1 — bounded cross-space mutation primitive.

The agent in space A submits a typed CrossSpaceRequest naming
target space B and one of four whitelisted action kinds. The kernel
validates, evaluates target covenants, and dispatches the mutation
under target's rules. Origin receives a typed CrossSpaceReceipt as
the tool result.

Architectural invariant (kept across the spec arc):
**Agent thinks; kernel enforces.** No nested agent in target. No
origin conversation or reasoning trace pollutes target. Target
records the request capsule (provenance) only.

This package owns:

* :class:`CrossSpaceRequest` / :class:`CrossSpaceReceipt` envelope shapes
* :data:`ACTION_KIND_DISPATCH` — registry of validator+executor per
  whitelisted action kind
* :func:`dispatch_request` — kernel entry point, runs the validation
  → covenant-evaluation → mutation → audit pipeline behind the
  target-space lock
* Reentrancy policy (``_CROSS_SPACE_POLICY``) + depth ContextVar
  in :mod:`kernos.kernel.cross_space.reentrancy`

Out of scope for v1: cross-instance (KERNOS-MESH), generated text
in target's voice, deletes, external side effects, recursive
cross-space requests (depth=1 ceiling).
"""
from __future__ import annotations

from kernos.kernel.cross_space.dispatch import (
    ACTION_KIND_DISPATCH,
    ALLOWED_ACTION_KINDS,
    CrossSpaceRequest,
    CrossSpaceReceipt,
    dispatch_request,
    new_request_id,
)
from kernos.kernel.cross_space.reentrancy import (
    enter_cross_space,
    exit_cross_space,
)
# Import executors to populate the action-kind dispatch registry at
# package-import time. The four action kinds register themselves via
# register_action_kind on import; ACTION_KIND_DISPATCH is empty
# without this side effect.
from kernos.kernel.cross_space import executors  # noqa: F401


__all__ = [
    "ACTION_KIND_DISPATCH",
    "ALLOWED_ACTION_KINDS",
    "CrossSpaceRequest",
    "CrossSpaceReceipt",
    "dispatch_request",
    "enter_cross_space",
    "exit_cross_space",
    "new_request_id",
]
