"""CROSS_SPACE_REQUESTS_V1 — dispatch flow + envelope shape tests.

Covers the kernel-side substrate without spinning up a full agent
turn. The acceptance-test surface (per-action-kind executors,
target re-entry awareness, end-to-end tool flow) lives in adjacent
files to keep failure surfaces narrow.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.cross_space import (
    ACTION_KIND_DISPATCH,
    ALLOWED_ACTION_KINDS,
    CrossSpaceReceipt,
    CrossSpaceRequest,
    dispatch_request,
    enter_cross_space,
    exit_cross_space,
    new_request_id,
)
from kernos.kernel.cross_space.dispatch import (
    DispatchEngine,
    _CROSS_SPACE_EVENT_TYPE,
)
from kernos.kernel.cross_space.envelopes import (
    CrossSpaceReceiptRef,
)
from kernos.kernel.cross_space.reentrancy import (
    current_cross_space_depth,
)
from kernos.kernel.external_agents.errors import (
    DepthExceeded,
    ReentrancyBlocked,
)
from kernos.kernel.external_agents.reentrancy import (
    CallingContext,
    set_calling_context,
    reset_calling_context,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeSpace:
    id: str
    instance_id: str
    member_id: str = ""


class _FakeState:
    """Minimal StateStore stand-in for dispatch tests."""

    def __init__(self) -> None:
        self.spaces: dict[tuple[str, str], _FakeSpace] = {}
        self.knowledge_writes: list = []
        self.covenant_writes: list = []

    def add_space(self, space: _FakeSpace) -> None:
        self.spaces[(space.instance_id, space.id)] = space

    async def get_context_space(self, instance_id, space_id):
        return self.spaces.get((instance_id, space_id))

    async def add_knowledge(self, entry):
        self.knowledge_writes.append(entry)

    async def add_contract_rule(self, rule):
        self.covenant_writes.append(rule)


class _CapturingEvents:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, str, str, dict]] = []
        self._events_for_query: list = []

    async def emit(self, event):
        self._events_for_query.append(event)

    async def query(
        self, instance_id, event_types=None, after=None, before=None, limit=50,
    ):
        out = []
        for evt in self._events_for_query:
            if instance_id and getattr(evt, "instance_id", None) != instance_id:
                continue
            if event_types and getattr(evt, "type", None) not in event_types:
                continue
            out.append(evt)
        return out[:limit]


class _CapturingAudit:
    def __init__(self) -> None:
        self.entries: list = []

    async def log(self, instance_id, entry):
        self.entries.append((instance_id, entry))


def _make_engine(state=None, events=None, audit=None, gate=None):
    return DispatchEngine(
        state=state or _FakeState(),
        events=events or _CapturingEvents(),
        audit=audit or _CapturingAudit(),
        gate=gate,
    )


def _make_request(
    *,
    action_kind: str = "write_knowledge",
    work_order: dict | None = None,
    origin_space_id: str = "space_origin",
    target_space_id: str = "space_target",
    instance_id: str = "inst1",
    member_id: str = "mem_owner",
    request_id: str | None = None,
) -> CrossSpaceRequest:
    return CrossSpaceRequest(
        request_id=request_id or new_request_id(),
        origin_space_id=origin_space_id,
        target_space_id=target_space_id,
        initiating_member_id=member_id,
        source_turn_id="conv1",
        action_kind=action_kind,
        work_order=work_order or {
            "topic": "test fact",
            "content": "the answer is 42",
            "sensitivity": "open",
        },
        instance_id=instance_id,
    )


@pytest.fixture
def conversational():
    """Set CONVERSATIONAL envelope; reset on teardown."""
    token = set_calling_context(CallingContext.CONVERSATIONAL)
    yield
    reset_calling_context(token)


# ---------------------------------------------------------------------------
# Envelope + reentrancy
# ---------------------------------------------------------------------------


class TestEnvelope:
    def test_action_kinds_registered(self):
        assert ALLOWED_ACTION_KINDS == frozenset(ACTION_KIND_DISPATCH)
        assert ALLOWED_ACTION_KINDS == {
            "write_knowledge",
            "propose_covenant",
            "create_plan_draft",
            "create_workflow_draft",
        }

    def test_request_id_uniqueness(self):
        ids = {new_request_id() for _ in range(50)}
        assert len(ids) == 50

    def test_receipt_serializes(self):
        r = CrossSpaceReceipt(
            request_id="csr_abc",
            status="completed",
            target_space_id="t1",
            timestamp="2026-05-01T00:00:00Z",
            created_refs=(
                CrossSpaceReceiptRef(type="knowledge_entry", id="know_xyz"),
            ),
            target_audit_ref="audit:csr_abc",
            provenance={"origin_space_id": "o1"},
            user_visible_summary="wrote it",
        )
        out = r.to_tool_result()
        assert out["status"] == "completed"
        assert out["created_refs"][0]["type"] == "knowledge_entry"
        assert out["target_audit_ref"] == "audit:csr_abc"


class TestReentrancyPolicy:
    def test_unknown_context_blocks(self):
        token = set_calling_context(CallingContext.UNKNOWN)
        try:
            with pytest.raises(ReentrancyBlocked):
                enter_cross_space()
        finally:
            reset_calling_context(token)

    def test_compaction_blocks(self):
        token = set_calling_context(CallingContext.COMPACTION)
        try:
            with pytest.raises(ReentrancyBlocked):
                enter_cross_space()
        finally:
            reset_calling_context(token)

    def test_conversational_allowed_depth_one(self):
        token = set_calling_context(CallingContext.CONVERSATIONAL)
        try:
            t1 = enter_cross_space()
            assert current_cross_space_depth() == 1
            with pytest.raises(DepthExceeded):
                enter_cross_space()
            exit_cross_space(t1)
            assert current_cross_space_depth() == 0
        finally:
            reset_calling_context(token)


# ---------------------------------------------------------------------------
# Dispatch flow
# ---------------------------------------------------------------------------


class TestDispatchValidation:
    async def test_invalid_action_kind_refused(self, conversational):
        engine = _make_engine()
        engine.state.add_space(_FakeSpace(id="space_origin", instance_id="inst1"))
        engine.state.add_space(_FakeSpace(id="space_target", instance_id="inst1"))
        req = _make_request()
        # Hand-roll an invalid kind.
        req = CrossSpaceRequest(
            request_id=req.request_id,
            origin_space_id=req.origin_space_id,
            target_space_id=req.target_space_id,
            initiating_member_id=req.initiating_member_id,
            source_turn_id=req.source_turn_id,
            action_kind="delete_everything",
            work_order={},
            instance_id=req.instance_id,
        )
        receipt = await dispatch_request(req, engine)
        assert receipt.status == "refused"
        assert "whitelist" in receipt.refusal_reason

    async def test_missing_target_refused(self, conversational):
        engine = _make_engine()
        engine.state.add_space(_FakeSpace(id="space_origin", instance_id="inst1"))
        # target not added — triggers cross-member-match path which
        # also catches missing target.
        req = _make_request()
        receipt = await dispatch_request(req, engine)
        assert receipt.status == "refused"
        assert "does not exist" in receipt.refusal_reason


class TestSameSpaceShortCircuit:
    async def test_short_circuit_no_audit(self, conversational):
        engine = _make_engine()
        engine.state.add_space(_FakeSpace(id="sX", instance_id="inst1"))
        req = _make_request(
            origin_space_id="sX", target_space_id="sX",
        )
        receipt = await dispatch_request(req, engine)
        assert receipt.status == "completed"
        # Short-circuit suppresses audit + event.
        assert engine.audit.entries == []
        assert receipt.target_audit_ref == ""


class TestCrossMemberRejected:
    async def test_different_member_target_refused(self, conversational):
        engine = _make_engine()
        engine.state.add_space(_FakeSpace(
            id="space_origin", instance_id="inst1", member_id="mem_a",
        ))
        engine.state.add_space(_FakeSpace(
            id="space_target", instance_id="inst1", member_id="mem_b",
        ))
        req = _make_request()
        receipt = await dispatch_request(req, engine)
        assert receipt.status == "refused"
        assert "cross-member" in receipt.refusal_reason


class TestIdempotency:
    async def test_duplicate_request_id_returns_prior_receipt(
        self, conversational,
    ):
        engine = _make_engine()
        engine.state.add_space(_FakeSpace(
            id="space_origin", instance_id="inst1",
        ))
        engine.state.add_space(_FakeSpace(
            id="space_target", instance_id="inst1",
        ))
        req = _make_request(request_id="csr_fixed")
        first = await dispatch_request(req, engine)
        assert first.status == "completed"
        first_count = len(engine.state.knowledge_writes)

        # Re-dispatch with the same request_id.
        second = await dispatch_request(req, engine)
        assert second.request_id == first.request_id
        assert second.status == first.status
        # No second mutation.
        assert len(engine.state.knowledge_writes) == first_count


class TestTargetLockTimeout:
    async def test_busy_target_returns_timeout(self, conversational):
        engine = _make_engine()
        engine.state.add_space(_FakeSpace(
            id="space_origin", instance_id="inst1",
        ))
        engine.state.add_space(_FakeSpace(
            id="space_target", instance_id="inst1",
        ))
        # Pre-acquire the target lock on a different task; never
        # release. dispatch_request must time out and return failed.
        target_lock = engine.get_target_lock("inst1", "space_target")
        await target_lock.acquire()
        try:
            req = _make_request()
            receipt = await dispatch_request(
                req, engine, target_lock_timeout_seconds=0.05,
            )
            assert receipt.status == "failed"
            assert receipt.refusal_reason == "timeout_waiting_for_target"
        finally:
            target_lock.release()


# ---------------------------------------------------------------------------
# Per-action-kind happy paths
# ---------------------------------------------------------------------------


class TestWriteKnowledge:
    async def test_writes_entry_with_provenance(self, conversational):
        engine = _make_engine()
        engine.state.add_space(_FakeSpace(
            id="space_origin", instance_id="inst1", member_id="mem_owner",
        ))
        engine.state.add_space(_FakeSpace(
            id="space_target", instance_id="inst1", member_id="mem_owner",
        ))
        req = _make_request(
            action_kind="write_knowledge",
            work_order={
                "topic": "client tos",
                "content": "auto-renews unless 30 days notice",
                "sensitivity": "contextual",
                "tags": ["legal"],
            },
        )
        receipt = await dispatch_request(req, engine)
        assert receipt.status == "completed"
        assert len(engine.state.knowledge_writes) == 1
        entry = engine.state.knowledge_writes[0]
        # Provenance stamped in source_description.
        assert "cross_space:write_knowledge" in entry.source_description
        assert req.request_id in entry.source_description
        # Receipt has a knowledge_entry ref pointing at the new entry.
        assert any(
            r.type == "knowledge_entry" and r.id == entry.id
            for r in receipt.created_refs
        )

    async def test_invalid_work_order_refused(self, conversational):
        engine = _make_engine()
        engine.state.add_space(_FakeSpace(
            id="space_origin", instance_id="inst1",
        ))
        engine.state.add_space(_FakeSpace(
            id="space_target", instance_id="inst1",
        ))
        req = _make_request(
            action_kind="write_knowledge",
            work_order={"content": "no topic"},
        )
        receipt = await dispatch_request(req, engine)
        assert receipt.status == "refused"
        assert "topic" in receipt.refusal_reason


class TestProposeCovenant:
    async def test_creates_proposal_not_active(self, conversational):
        engine = _make_engine()
        engine.state.add_space(_FakeSpace(
            id="space_origin", instance_id="inst1",
        ))
        engine.state.add_space(_FakeSpace(
            id="space_target", instance_id="inst1",
        ))
        req = _make_request(
            action_kind="propose_covenant",
            work_order={
                "description": "always confirm before sending invoices",
                "scope": "general",
                "tier": "must",
            },
        )
        receipt = await dispatch_request(req, engine)
        assert receipt.status == "proposed"
        assert len(engine.state.covenant_writes) == 1
        proposal = engine.state.covenant_writes[0]
        assert proposal.active is False
        assert proposal.source == "cross_space_proposal"

    async def test_bypasses_target_covenant_eval(self, conversational):
        # Q2 safety valve: even if the gate is wired, propose_covenant
        # bypasses it. Use a gate that would block everything; the
        # call still succeeds.
        class _BlockingGate:
            async def evaluate_cross_space(self, **kwargs):
                from kernos.kernel.gate import _CrossSpaceGateDecision
                return _CrossSpaceGateDecision(
                    decision="covenant_conflict",
                    reason="this gate blocks everything",
                )

        engine = _make_engine(gate=_BlockingGate())
        engine.state.add_space(_FakeSpace(
            id="space_origin", instance_id="inst1",
        ))
        engine.state.add_space(_FakeSpace(
            id="space_target", instance_id="inst1",
        ))
        req = _make_request(
            action_kind="propose_covenant",
            work_order={"description": "blocked by gate but still proposes"},
        )
        receipt = await dispatch_request(req, engine)
        assert receipt.status == "proposed", (
            "propose_covenant must bypass target covenant evaluation "
            "per Q2 safety valve"
        )


class TestCreatePlanDraft:
    async def test_creates_plan_with_draft_status(
        self, conversational, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
        engine = _make_engine()
        engine.state.add_space(_FakeSpace(
            id="space_origin", instance_id="inst1",
        ))
        engine.state.add_space(_FakeSpace(
            id="space_target", instance_id="inst1",
        ))
        req = _make_request(
            action_kind="create_plan_draft",
            work_order={
                "title": "launch sequence",
                "phases": [
                    {"id": "p1", "title": "Phase 1", "steps": [
                        {"id": "s1", "title": "first thing"},
                    ]},
                ],
            },
        )
        receipt = await dispatch_request(req, engine)
        assert receipt.status == "needs_confirmation"
        assert any(r.type == "plan_draft" for r in receipt.created_refs)


class TestCreateWorkflowDraft:
    async def test_creates_workflow_descriptor(
        self, conversational, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
        engine = _make_engine()
        engine.state.add_space(_FakeSpace(
            id="space_origin", instance_id="inst1",
        ))
        engine.state.add_space(_FakeSpace(
            id="space_target", instance_id="inst1",
        ))
        req = _make_request(
            action_kind="create_workflow_draft",
            work_order={
                "descriptor": {
                    "name": "morning_brief",
                    "description": "summarize overnight events",
                },
            },
        )
        receipt = await dispatch_request(req, engine)
        assert receipt.status == "completed"
        assert any(r.type == "workflow_draft" for r in receipt.created_refs)


# ---------------------------------------------------------------------------
# Audit + event emission
# ---------------------------------------------------------------------------


class TestAuditAndEvent:
    async def test_cross_space_writes_audit_entry(self, conversational):
        engine = _make_engine()
        engine.state.add_space(_FakeSpace(
            id="space_origin", instance_id="inst1",
        ))
        engine.state.add_space(_FakeSpace(
            id="space_target", instance_id="inst1",
        ))
        req = _make_request()
        receipt = await dispatch_request(req, engine)
        assert receipt.status == "completed"
        # Audit log carries the request capsule + receipt status.
        assert len(engine.audit.entries) == 1
        instance_id, entry = engine.audit.entries[0]
        assert instance_id == "inst1"
        assert entry["type"] == _CROSS_SPACE_EVENT_TYPE
        assert entry["origin_space_id"] == "space_origin"
        assert entry["target_space_id"] == "space_target"
        assert entry["status"] == "completed"
        assert receipt.target_audit_ref == f"audit:{req.request_id}"
