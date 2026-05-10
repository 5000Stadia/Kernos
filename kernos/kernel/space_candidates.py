"""Routing candidate selection — pure-over-state visibility filter.

ROUTER-EVIDENCE-V1 batch 2.1. Extracts the visibility filter that previously
lived inline in ``LLMRouter.route`` so both the route phase (when building
per-space evidence) and the router itself (when bundling for the prompt)
agree on a single candidate set. Routing policy, not persistence — kept out
of ``StateStore``.
"""
from __future__ import annotations

import logging

from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import StateStore

logger = logging.getLogger(__name__)


async def list_route_candidate_spaces(
    state: StateStore,
    instance_id: str,
    member_id: str = "",
) -> list[ContextSpace]:
    """Return active spaces visible to ``member_id`` for routing.

    Visibility shape mirrors the legacy filter at ``router.py:142``:

      - status == "active"
      - if member_id is provided: keep spaces the member owns, plus
        legacy unowned spaces (member_id == ""), plus system spaces.
      - if member_id is empty: keep all active spaces.

    Order is whatever ``state.list_context_spaces`` returns — callers
    that need a deterministic order should sort downstream.
    """
    spaces = await state.list_context_spaces(instance_id)
    active = [s for s in spaces if s.status == "active"]
    if not member_id:
        return active
    return [
        s for s in active
        if not s.member_id or s.member_id == member_id or s.space_type == "system"
    ]
