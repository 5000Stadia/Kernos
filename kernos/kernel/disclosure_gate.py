"""Cross-member disclosure gate (DISCLOSURE-GATE).

Read-time filter applied at context assembly during member M's active turn.
Enforces the simplified three-value relationship permission model
(full-access / no-access / by-permission) at the kernel layer so the agent
never sees another member's protected content during a turn that isn't
theirs.

Design principle: sensitivity is enforced by the kernel, not the agent.
The agent thinks; the kernel enforces. The agent cannot be trusted to
self-filter content already in its context — so we keep it out.

Filter order per entry (first match wins):
    1. Author check           — entry owned by M → PASS
    2. Open-sensitivity pass  — sensitivity=="open" → PASS
    3. Permission check       — M's declared permission toward the author:
                                 full-access  → PASS
                                 no-access    → FILTER (relationship_is_no_access)
                                 by-permission/missing → FILTER (no_relationship_permission)

Fail-closed: on any error building the permission map or evaluating a rule,
the gate filters the entry. Preserving the disclosure invariant is worth
the occasional false-filter; the owner can fix over-restriction by
declaring relationships correctly.

Covenant topic-exception (step 4 of the spec) is NOT implemented here —
existing covenants don't support `relationship:A:B` scope or a `withhold`
action. Adding those is a primitive extension escalated separately.

The gate logs each filter event to the runtime trace with a reason code
and no filtered content. Reason codes:
    no_relationship_permission  — no declaration OR implicit by-permission
    relationship_is_no_access   — M's side is explicitly no-access
    gate_error                  — failure path, fail-closed
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


PASS_REASONS = {"author_self", "open_sensitivity", "full_access"}
FILTER_REASONS = {
    "no_relationship_permission",
    "relationship_is_no_access",
    "gate_error",
}


@dataclass
class GateVerdict:
    passed: bool
    reason: str  # one of PASS_REASONS ∪ FILTER_REASONS


async def build_permission_map(
    instance_db: Any, requesting_member_id: str,
) -> dict[str, str]:
    """Return {author_member_id: permission_M_has_toward_author}.

    Missing authors default to `by-permission` at check time. Called once
    per turn (not per entry) and cached by the caller.

    Fails soft: empty dict on any error. The gate fails closed when the
    map is empty (unknown authors → filter), so a failure here doesn't
    leak content.

    Codex CCV1 C3b-review CONCERN fold: stable return type. Coerce
    non-dict results (test fixtures sometimes wire MagicMock-shaped
    instance_db whose list_permissions_for returns a MagicMock instead
    of a dict) to ``{}``. The function's contract is dict-or-empty;
    making the return shape stable is safer for every caller.
    """
    if not instance_db or not requesting_member_id:
        return {}
    try:
        result = await instance_db.list_permissions_for(requesting_member_id)
    except Exception as exc:
        logger.warning(
            "DISCLOSURE_GATE: build_permission_map failed for %s: %s",
            requesting_member_id, exc,
        )
        return {}
    return result if isinstance(result, dict) else {}


def evaluate(
    *,
    entry_author_id: str,
    entry_sensitivity: str,
    requesting_member_id: str,
    permission_map: dict[str, str],
) -> GateVerdict:
    """Run the gate on a single entry. Pure, synchronous, fail-closed.

    entry_author_id may be empty (legacy data). When empty, we treat the
    entry as authored by the requesting member if this is the requesting
    member's turn — i.e., we assume own-data rather than someone else's,
    because spaces and stores are per-member. If this assumption ever
    surfaces a leak, tighten by treating empty as non-M (filter).
    """
    try:
        # 1. Author check — M's own data always passes.
        if not entry_author_id:
            # Legacy / missing owner_member_id: assume M-authored when no
            # other member could plausibly have authored it. The per-member
            # storage path means an entry visible to M's turn is almost
            # certainly M's. If this becomes a leak vector, flip to filter.
            return GateVerdict(True, "author_self")
        if entry_author_id == requesting_member_id:
            return GateVerdict(True, "author_self")

        # 2. Open-sensitivity pass.
        if (entry_sensitivity or "").strip().lower() == "open":
            return GateVerdict(True, "open_sensitivity")

        # 3. Relationship permission check — M's side toward the author.
        perm = permission_map.get(entry_author_id, "by-permission")
        if perm == "full-access":
            return GateVerdict(True, "full_access")
        if perm == "no-access":
            return GateVerdict(False, "relationship_is_no_access")
        # by-permission (explicit or implicit default)
        return GateVerdict(False, "no_relationship_permission")

    except Exception as exc:
        logger.warning("DISCLOSURE_GATE: evaluate error: %s", exc)
        return GateVerdict(False, "gate_error")


def filter_knowledge_entries(
    entries: list,
    *,
    requesting_member_id: str,
    permission_map: dict[str, str],
    trace: Any = None,
) -> list:
    """Filter a list of KnowledgeEntry-like objects in place-style.

    Returns a new list containing only entries that pass the gate.
    Records a single aggregated trace event summarizing the filter result
    for the turn. Never includes filtered entry content in the trace.
    """
    if not entries:
        return entries
    kept = []
    filtered_by_reason: dict[str, int] = {}
    filtered_by_author: dict[str, int] = {}

    for entry in entries:
        author = getattr(entry, "owner_member_id", "") or ""
        sensitivity = getattr(entry, "sensitivity", "") or ""
        verdict = evaluate(
            entry_author_id=author,
            entry_sensitivity=sensitivity,
            requesting_member_id=requesting_member_id,
            permission_map=permission_map,
        )
        if verdict.passed:
            kept.append(entry)
        else:
            filtered_by_reason[verdict.reason] = (
                filtered_by_reason.get(verdict.reason, 0) + 1
            )
            if author:
                filtered_by_author[author] = (
                    filtered_by_author.get(author, 0) + 1
                )

    if trace and (filtered_by_reason or filtered_by_author):
        detail = (
            f"filtered={sum(filtered_by_reason.values())} "
            f"kept={len(kept)} "
            f"reasons={filtered_by_reason} "
            f"authors={sorted(filtered_by_author.keys())}"
        )
        try:
            trace.record(
                "info", "disclosure_gate", "GATE_FILTER_KNOWLEDGE",
                detail, phase="assemble",
            )
        except Exception:
            pass  # trace must never break the pipeline

    return kept


def filter_canvases_by_membership(
    canvases: list[dict],
    *,
    requesting_member_id: str,
    canvas_member_lookup,
    trace: Any = None,
) -> list[dict]:
    """Filter a canvas list to those the requesting member can see.

    CANVAS-V1 parallel to :func:`filter_knowledge_entries`. A canvas is
    visible if:
      * its scope == 'team' (every instance member sees team canvases), OR
      * the calling member is in the canvas's explicit member list.

    ``canvas_member_lookup`` is a sync callable
    ``(canvas_id) -> list[member_id]`` that returns the explicit members
    for a canvas. Usually a lambda closing over a pre-fetched map.

    Fails closed: on any error the canvas is filtered out. Consistent
    with the disclosure-gate discipline elsewhere in this module.
    """
    if not canvases:
        return canvases
    kept: list[dict] = []
    filtered = 0
    filter_reasons: dict[str, int] = {}
    for c in canvases:
        try:
            canvas_id = c.get("canvas_id") or c.get("space_id", "")
            scope = (c.get("scope") or "").lower()
            if scope == "team":
                kept.append(c)
                continue
            members = canvas_member_lookup(canvas_id) or []
            if requesting_member_id and requesting_member_id in members:
                kept.append(c)
                continue
            filtered += 1
            filter_reasons["not_a_member"] = filter_reasons.get("not_a_member", 0) + 1
        except Exception as exc:
            logger.warning(
                "DISCLOSURE_GATE: filter_canvases error on canvas=%r: %s",
                c.get("canvas_id"), exc,
            )
            filtered += 1
            filter_reasons["gate_error"] = filter_reasons.get("gate_error", 0) + 1

    if trace and filtered:
        try:
            trace.record(
                "info", "disclosure_gate", "GATE_FILTER_CANVASES",
                f"filtered={filtered} kept={len(kept)} reasons={filter_reasons}",
                phase="assemble",
            )
        except Exception:
            pass
    return kept


def filter_log_entries(
    entries: list[dict],
    *,
    requesting_member_id: str,
    permission_map: dict[str, str],
    trace: Any = None,
) -> list[dict]:
    """Filter conversation-log line dicts by speaker author.

    Conversation-log lines are authored by the speaker member_id on each
    line. This is only exercised when logs from another member's space
    would surface during M's turn (rare path); per-member logs are
    already isolated by path. Included for defense-in-depth.
    """
    if not entries:
        return entries
    kept = []
    filtered_reasons: dict[str, int] = {}
    for e in entries:
        author = (e.get("member_id") or e.get("speaker_member_id") or "")
        # Conversation lines don't carry sensitivity; treat as personal.
        verdict = evaluate(
            entry_author_id=author,
            entry_sensitivity="personal",
            requesting_member_id=requesting_member_id,
            permission_map=permission_map,
        )
        if verdict.passed:
            kept.append(e)
        else:
            filtered_reasons[verdict.reason] = (
                filtered_reasons.get(verdict.reason, 0) + 1
            )

    if trace and filtered_reasons:
        try:
            trace.record(
                "info", "disclosure_gate", "GATE_FILTER_LOG",
                (f"filtered={sum(filtered_reasons.values())} "
                 f"kept={len(kept)} reasons={filtered_reasons}"),
                phase="assemble",
            )
        except Exception:
            pass

    return kept
