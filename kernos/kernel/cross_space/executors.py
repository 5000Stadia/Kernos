"""Per-action-kind executors for cross-space requests.

Four executors register themselves at import time via
``register_action_kind``. Each pair: (validator, executor). The
validator catches structural errors in ``work_order``; the executor
performs the mutation through existing state APIs with provenance
fields populated.

Provenance discipline: every write stamps the entity with the
request_id, origin_space_id, source_turn_id, initiating_member_id,
action_kind so the target agent can answer "why is this here?"
from target-local provenance + audit alone (the spec's re-entry
acceptance test).
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from kernos.kernel.cross_space.dispatch import (
    DispatchEngine,
    register_action_kind,
)
from kernos.kernel.cross_space.envelopes import (
    CrossSpaceReceiptRef,
    CrossSpaceRequest,
    ReceiptStatus,
)
from kernos.utils import utc_now

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provenance helpers
# ---------------------------------------------------------------------------


def _provenance_blurb(req: CrossSpaceRequest) -> str:
    """Human-readable provenance line for source_description-style
    fields. The target agent reads this when surfacing 'why is
    this here?'."""
    return (
        f"cross_space:{req.action_kind} "
        f"from={req.origin_space_id} "
        f"member={req.initiating_member_id} "
        f"request_id={req.request_id}"
    )


# ---------------------------------------------------------------------------
# write_knowledge
# ---------------------------------------------------------------------------


def _validate_write_knowledge(
    req: CrossSpaceRequest,
) -> tuple[bool, str]:
    wo = req.work_order
    if not isinstance(wo.get("topic"), str) or not wo["topic"].strip():
        return (False, "work_order.topic must be a non-empty string")
    if not isinstance(wo.get("content"), str) or not wo["content"].strip():
        return (False, "work_order.content must be a non-empty string")
    sensitivity = wo.get("sensitivity", "open")
    if sensitivity not in ("open", "contextual", "personal"):
        return (False, (
            f"work_order.sensitivity must be one of "
            f"open/contextual/personal; got {sensitivity!r}"
        ))
    return (True, "")


async def _execute_write_knowledge(
    req: CrossSpaceRequest, engine: DispatchEngine,
) -> tuple[ReceiptStatus, list[CrossSpaceReceiptRef], str]:
    from kernos.kernel.state import KnowledgeEntry

    wo = req.work_order
    entry_id = f"know_{uuid.uuid4().hex[:8]}"
    now = utc_now()

    entry = KnowledgeEntry(
        id=entry_id,
        instance_id=req.instance_id,
        category="fact",
        subject=wo["topic"].strip()[:200],
        content=wo["content"].strip(),
        confidence="stated",
        # Provenance: source_event_id stays empty for now (cross-space
        # event id isn't known until the audit/event step); the
        # cross-space event payload carries the request_id, and the
        # source_description names the cross-space request explicitly.
        source_event_id="",
        source_description=_provenance_blurb(req),
        created_at=now,
        last_referenced=now,
        tags=list(wo.get("tags", []) or []),
    )
    # Stamp owner_member_id where the target's member context applies.
    target_space = await engine.state.get_context_space(
        req.instance_id, req.target_space_id,
    )
    if target_space and target_space.member_id:
        # KnowledgeEntry has owner_member_id on multi-member-shipped
        # instances; setattr defensively in case a legacy schema is
        # active.
        try:
            setattr(entry, "owner_member_id", target_space.member_id)
        except Exception:
            pass

    await engine.state.add_knowledge(entry)
    summary = (
        f'wrote knowledge entry "{entry.subject}" to '
        f"{req.target_space_id}"
    )
    return (
        "completed",
        [CrossSpaceReceiptRef(type="knowledge_entry", id=entry.id)],
        summary,
    )


register_action_kind(
    "write_knowledge",
    validator=_validate_write_knowledge,
    executor=_execute_write_knowledge,
    bypass_target_covenants=False,
)


# ---------------------------------------------------------------------------
# propose_covenant — Q2 safety valve: bypass target covenant evaluation
# ---------------------------------------------------------------------------


def _validate_propose_covenant(
    req: CrossSpaceRequest,
) -> tuple[bool, str]:
    wo = req.work_order
    desc = wo.get("description", "")
    if not isinstance(desc, str) or not desc.strip():
        return (False, "work_order.description must be a non-empty string")
    return (True, "")


async def _execute_propose_covenant(
    req: CrossSpaceRequest, engine: DispatchEngine,
) -> tuple[ReceiptStatus, list[CrossSpaceReceiptRef], str]:
    """Propose-only: a covenant proposal is recorded in target's
    state with status='proposed'. Activation is the user's call in
    target. The proposal entity records the request capsule
    completely so the target agent can surface "this proposal came
    from {origin_space} via {member}" without reaching into origin's
    conversation.

    Storage: covenant rules table, with ``active=False`` and
    ``source='cross_space_proposal'``. The target's covenant
    surface (``manage_covenants``) lists proposals separately from
    active rules.
    """
    from kernos.kernel.state import CovenantRule

    wo = req.work_order
    proposal_id = f"prop_{uuid.uuid4().hex[:8]}"
    now = utc_now()

    target_space = await engine.state.get_context_space(
        req.instance_id, req.target_space_id,
    )
    member_id = target_space.member_id if target_space else ""
    scope = wo.get("scope", "general")

    proposal = CovenantRule(
        id=proposal_id,
        instance_id=req.instance_id,
        capability=str(scope) if scope else "general",
        rule_type=str(wo.get("tier", "preference")),
        description=wo["description"].strip(),
        active=False,  # proposed-not-applied
        source="cross_space_proposal",
        source_event_id=None,
        created_at=now,
        updated_at=now,
        context_space=str(wo.get("context_space") or req.target_space_id),
    )
    try:
        # Stamp the member owner where supported.
        if member_id:
            setattr(proposal, "member_id", member_id)
    except Exception:
        pass
    # Record provenance compactly in escalation_message field — that
    # field is human-readable and surfaces with the rule.
    try:
        setattr(
            proposal, "escalation_message",
            _provenance_blurb(req),
        )
    except Exception:
        pass

    await engine.state.add_contract_rule(proposal)
    summary = (
        f"proposed a covenant in {req.target_space_id}; "
        f"target user reviews to activate"
    )
    return (
        "proposed",
        [CrossSpaceReceiptRef(type="covenant_proposal", id=proposal.id)],
        summary,
    )


register_action_kind(
    "propose_covenant",
    validator=_validate_propose_covenant,
    executor=_execute_propose_covenant,
    # Q2 safety valve: bypass target covenant evaluation. Proposals
    # are always proposed-not-applied; the proposal entity records
    # the request capsule and is auditable. No live mutation to
    # gate.
    bypass_target_covenants=True,
)


# ---------------------------------------------------------------------------
# create_plan_draft
# ---------------------------------------------------------------------------


def _validate_create_plan_draft(
    req: CrossSpaceRequest,
) -> tuple[bool, str]:
    wo = req.work_order
    title = wo.get("title", "")
    if not isinstance(title, str) or not title.strip():
        return (False, "work_order.title must be a non-empty string")
    phases = wo.get("phases", [])
    if not isinstance(phases, list):
        return (False, "work_order.phases must be a list")
    return (True, "")


async def _execute_create_plan_draft(
    req: CrossSpaceRequest, engine: DispatchEngine,
) -> tuple[ReceiptStatus, list[CrossSpaceReceiptRef], str]:
    """Create a plan in target space with status='draft'.
    Activation requires same-turn user confirmation in target;
    v1 does not auto-start.
    """
    from kernos.kernel.execution import generate_plan_id, save_plan

    wo = req.work_order
    plan_id = generate_plan_id()
    phases = list(wo.get("phases", []))
    for phase in phases:
        if isinstance(phase, dict):
            for step in phase.get("steps", []) or []:
                if isinstance(step, dict):
                    step.setdefault("status", "pending")

    plan = {
        "plan_id": plan_id,
        "title": wo["title"].strip(),
        "status": "draft",
        "workspace_id": req.target_space_id,
        "phases": phases,
        "budget": dict(wo.get("budget_override") or {}),
        "usage": {"steps_used": 0, "tokens_used": 0, "elapsed_s": 0},
        "discoveries": [],
        "show_progress": False,
        "created_at": utc_now(),
        "cross_space_provenance": {
            "request_id": req.request_id,
            "origin_space_id": req.origin_space_id,
            "source_turn_id": req.source_turn_id,
            "initiating_member_id": req.initiating_member_id,
        },
    }

    import os as _os
    data_dir = _os.getenv("KERNOS_DATA_DIR", "./data")
    await save_plan(data_dir, req.instance_id, req.target_space_id, plan)

    summary = (
        f'created plan draft "{plan["title"]}" in '
        f"{req.target_space_id}; awaiting user confirmation to start"
    )
    return (
        "needs_confirmation",
        [CrossSpaceReceiptRef(type="plan_draft", id=plan_id)],
        summary,
    )


register_action_kind(
    "create_plan_draft",
    validator=_validate_create_plan_draft,
    executor=_execute_create_plan_draft,
    bypass_target_covenants=False,
)


# ---------------------------------------------------------------------------
# create_workflow_draft
# ---------------------------------------------------------------------------


def _validate_create_workflow_draft(
    req: CrossSpaceRequest,
) -> tuple[bool, str]:
    wo = req.work_order
    descriptor = wo.get("descriptor")
    if not isinstance(descriptor, dict):
        return (False, "work_order.descriptor must be a dict")
    name = descriptor.get("name") or wo.get("name")
    if not name or not str(name).strip():
        return (False, "work_order.descriptor.name (or work_order.name) is required")
    return (True, "")


async def _execute_create_workflow_draft(
    req: CrossSpaceRequest, engine: DispatchEngine,
) -> tuple[ReceiptStatus, list[CrossSpaceReceiptRef], str]:
    """Create a workflow descriptor with status='draft' in target
    space. v1: persisted as a JSON file under target's space dir
    (parallel to plans). When CRB / WORKFLOW-TRIGGERS-CONSOLIDATION
    lands its persistence model, this executor migrates to use
    that registry's draft path.
    """
    from kernos.utils import _safe_name
    from pathlib import Path
    import os as _os

    wo = req.work_order
    descriptor = dict(wo["descriptor"])
    descriptor["status"] = "draft"
    descriptor["created_at"] = utc_now()
    descriptor["cross_space_provenance"] = {
        "request_id": req.request_id,
        "origin_space_id": req.origin_space_id,
        "source_turn_id": req.source_turn_id,
        "initiating_member_id": req.initiating_member_id,
    }
    if "workflow_id" not in descriptor:
        descriptor["workflow_id"] = f"wfd_{uuid.uuid4().hex[:8]}"
    if "instance_id" not in descriptor:
        descriptor["instance_id"] = req.instance_id

    if "triggers" in wo and "trigger" not in descriptor:
        descriptor["trigger"] = wo["triggers"]
    if "actions" in wo and "action_sequence" not in descriptor:
        descriptor["action_sequence"] = wo["actions"]
    if "gates" in wo and "approval_gates" not in descriptor:
        descriptor["approval_gates"] = wo["gates"]

    data_dir = _os.getenv("KERNOS_DATA_DIR", "./data")
    drafts_dir = (
        Path(data_dir) / _safe_name(req.instance_id) / "spaces"
        / _safe_name(req.target_space_id) / "workflow_drafts"
    )
    drafts_dir.mkdir(parents=True, exist_ok=True)
    draft_path = drafts_dir / f"{descriptor['workflow_id']}.json"
    draft_path.write_text(
        json.dumps(descriptor, indent=2), encoding="utf-8",
    )

    summary = (
        f'created workflow draft "{descriptor.get("name")}" in '
        f"{req.target_space_id}; activation deferred to target"
    )
    return (
        "completed",
        [CrossSpaceReceiptRef(
            type="workflow_draft", id=descriptor["workflow_id"],
        )],
        summary,
    )


register_action_kind(
    "create_workflow_draft",
    validator=_validate_create_workflow_draft,
    executor=_execute_create_workflow_draft,
    bypass_target_covenants=False,
)


__all__ = [
    # Re-exports for tests.
    "_execute_create_plan_draft",
    "_execute_create_workflow_draft",
    "_execute_propose_covenant",
    "_execute_write_knowledge",
    "_provenance_blurb",
]
