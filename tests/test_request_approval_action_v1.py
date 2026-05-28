from __future__ import annotations

import asyncio
import functools
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite
import pytest

from kernos.kernel import approval_receipts, event_stream
from kernos.kernel.workflows.action_classification import (
    KNOWN_ACTION_TYPES,
    is_irreversible,
)
from kernos.kernel.workflows.action_library import (
    ActionLibrary,
    ActionResult,
    BranchAction,
    MarkStateAction,
    RequestApprovalAction,
)
from kernos.kernel.workflows.action_sink import (
    ACTION_OPERATION_CLASS_BY_VERB,
    RISK_LEVEL_BY_OPERATION_CLASS,
)
from kernos.kernel.workflows.execution_engine import (
    ExecutionEngine,
    WorkflowExecution,
)
from kernos.kernel.workflows.ledger import WorkflowLedger
from kernos.kernel.workflows.refs import (
    _NOT_FOUND,
    RefResolutionError,
    ResolutionContext,
    resolve_references_in_value,
)
from kernos.kernel.workflows.step_outputs import (
    build_output_envelope,
    capture_step_output,
    load_workflow_outputs,
)
from kernos.kernel.workflows.trigger_registry import (
    TriggerRegistry,
    _reset_for_tests as _reset_trigger_registry,
)
from kernos.kernel.workflows.workflow_registry import (
    ActionDescriptor,
    ApprovalGate,
    Bounds,
    ContinuationRules,
    TriggerDescriptor,
    Verifier,
    Workflow,
    WorkflowRegistry,
    validate_workflow,
)
from kernos.setup.bring_up_substrate import _register_all_actions


@dataclass
class _Ctx:
    instance_id: str = "inst_a"
    member_id: str = "member_a"


def _action(
    action_type: str,
    *,
    id: str = "",
    gate_ref: str | None = None,
    **params,
) -> ActionDescriptor:
    return ActionDescriptor(
        id=id,
        action_type=action_type,
        parameters=params,
        gate_ref=gate_ref,
        continuation_rules=ContinuationRules(on_failure="abort"),
    )


def _gate(timeout_seconds: int = 5) -> ApprovalGate:
    return ApprovalGate(
        gate_name="await_approval",
        pause_reason="operator approval",
        approval_event_type="approval.decision_recorded",
        approval_event_predicate={
            "op": "eq",
            "path": "payload.approval_id",
            "value": "{step.req.value.approval_id}",
        },
        timeout_seconds=timeout_seconds,
        bound_behavior_on_timeout="abort_workflow",
    )


def _workflow(actions: list[ActionDescriptor], *, workflow_id="wf-approval"):
    return Workflow(
        workflow_id=workflow_id,
        instance_id="inst_a",
        name="approval workflow",
        description="",
        owner="owner",
        version="1.0",
        bounds=Bounds(iteration_count=10, wall_time_seconds=30),
        verifier=Verifier(flavor="deterministic", check="ok"),
        action_sequence=actions,
        approval_gates=[_gate()],
        trigger=TriggerDescriptor(
            event_type="cc.batch.report",
            predicate={"op": "exists", "path": "event_id"},
        ),
    )


def _state_store():
    store: dict = {}

    async def set_(*, key, value, scope, instance_id):
        store[(scope, instance_id, key)] = value

    async def get_(*, key, scope, instance_id):
        return store.get((scope, instance_id, key))

    return store, set_, get_


async def _wait_for(predicate, timeout=3.0, step=0.02):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(step)
    return False


async def _read_events(instance_id: str = "inst_a"):
    return await event_stream.events_in_window(
        instance_id,
        datetime.fromisoformat("2020-01-01T00:00:00+00:00"),
        datetime.fromisoformat("2099-01-01T00:00:00+00:00"),
    )


@pytest.fixture
async def approval_stack(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    await approval_receipts.ensure_schema(tmp_path)
    trig = TriggerRegistry()
    await trig.start(str(tmp_path))
    wfr = WorkflowRegistry()
    await wfr.start(str(tmp_path), trig)
    store, set_, get_ = _state_store()
    lib = ActionLibrary()
    lib.register(MarkStateAction(state_store_set=set_, state_store_get=get_))
    lib.register(BranchAction())
    lib.register(RequestApprovalAction(
        request_approval_fn=functools.partial(
            approval_receipts.request_approval,
            data_dir=tmp_path,
            event_stream=event_stream,
        ),
    ))
    ledger = WorkflowLedger(str(tmp_path))
    engine = ExecutionEngine()
    await engine.start(str(tmp_path), trig, wfr, lib, ledger)
    yield {
        "tmp_path": tmp_path,
        "trig": trig,
        "wfr": wfr,
        "lib": lib,
        "ledger": ledger,
        "engine": engine,
        "store": store,
    }
    await engine.stop()
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await event_stream._reset_for_tests()


class TestRequestApprovalAction:
    async def test_executes_defaults_and_verifies(self):
        calls: list[dict] = []

        async def request(**kwargs):
            calls.append(kwargs)
            return "approval-1"

        verb = RequestApprovalAction(request_approval_fn=request)
        params = {
            "kind": "deploy",
            "operator_actor_id": "operator",
            "request_summary": "Approve deploy",
            "binding_payload": {"sha": "abc"},
            "_workflow_execution_id": "exec-1",
            "_gate_nonce": "nonce-1",
        }
        result = await verb.execute(_Ctx(), params)
        assert result.success is True
        assert result.value == {"approval_id": "approval-1"}
        assert result.receipt["approval_id"] == "approval-1"
        assert calls[0]["ttl_seconds"] == 86400
        assert calls[0]["single_use"] is True
        assert calls[0]["requested_for_actor"] == "member_a"
        assert calls[0]["workflow_execution_id"] == "exec-1"
        assert calls[0]["gate_nonce"] == "nonce-1"
        assert await verb.verify(_Ctx(), params, result) is True
        assert await verb.verify(
            _Ctx(), params, ActionResult(success=True, value={}),
        ) is False

    @pytest.mark.parametrize("missing", [
        "kind", "operator_actor_id", "request_summary",
    ])
    async def test_missing_required_param_fails_cleanly(self, missing):
        async def request(**kwargs):
            return "approval-1"

        params = {
            "kind": "deploy",
            "operator_actor_id": "operator",
            "request_summary": "Approve deploy",
            "_workflow_execution_id": "exec-1",
            "_gate_nonce": "nonce-1",
        }
        params.pop(missing)
        result = await RequestApprovalAction(request).execute(_Ctx(), params)
        assert result.success is False
        assert result.error == f"missing_param:{missing}"

    @pytest.mark.parametrize(("field", "value", "error"), [
        ("kind", "", "invalid_param:kind"),
        ("kind", 123, "invalid_param:kind"),
        ("operator_actor_id", "", "invalid_param:operator_actor_id"),
        ("operator_actor_id", 123, "invalid_param:operator_actor_id"),
        ("request_summary", "", "invalid_param:request_summary"),
        ("request_summary", 123, "invalid_param:request_summary"),
    ])
    async def test_invalid_required_param_fails_without_receipt(
        self, tmp_path, field, value, error,
    ):
        await approval_receipts.ensure_schema(tmp_path)
        calls: list[dict] = []

        async def request(**kwargs):
            calls.append(kwargs)
            return await approval_receipts.request_approval(
                data_dir=tmp_path,
                event_stream=None,
                **kwargs,
            )

        params = {
            "kind": "deploy",
            "operator_actor_id": "operator",
            "request_summary": "Approve deploy",
            "_workflow_execution_id": "exec-1",
            "_gate_nonce": "nonce-1",
        }
        params[field] = value

        result = await RequestApprovalAction(request).execute(_Ctx(), params)

        assert result.success is False
        assert result.error == error
        assert calls == []
        assert await _receipt_count(tmp_path) == 0

    async def test_binding_payload_and_workflow_binding_validation(self):
        async def request(**kwargs):
            return "approval-1"

        verb = RequestApprovalAction(request)
        base = {
            "kind": "deploy",
            "operator_actor_id": "operator",
            "request_summary": "Approve deploy",
            "_workflow_execution_id": "exec-1",
            "_gate_nonce": "nonce-1",
        }
        result = await verb.execute(_Ctx(), {**base, "binding_payload": []})
        assert result.error == "invalid_binding_payload:not_a_mapping"

        result = await verb.execute(
            _Ctx(), {**base, "binding_payload": {"bad": object()}},
        )
        assert result.success is False
        assert result.error and result.error.startswith(
            "invalid_binding_payload:"
        )

        result = await verb.execute(_Ctx(), {
            "kind": "deploy",
            "operator_actor_id": "operator",
            "request_summary": "Approve deploy",
        })
        assert result.error == "missing_workflow_binding"

    async def test_covenant_denial_and_wrapped_failure(self):
        calls = 0

        async def request(**kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("db unavailable")

        denied = RequestApprovalAction(
            request_approval_fn=request,
            covenant_gate=lambda ctx, action_type, params: False,
        )
        result = await denied.execute(_Ctx(), {})
        assert result.error == "covenant_denied"
        assert calls == 0

        allowed = RequestApprovalAction(request_approval_fn=request)
        result = await allowed.execute(_Ctx(), {
            "kind": "deploy",
            "operator_actor_id": "operator",
            "request_summary": "Approve deploy",
            "_workflow_execution_id": "exec-1",
            "_gate_nonce": "nonce-1",
        })
        assert result.error == "approval_request_failed:db unavailable"


class TestFindTerminalByBinding:
    async def test_normalizes_terminal_state_and_reason(self, tmp_path):
        await approval_receipts.ensure_schema(tmp_path)
        pending = await approval_receipts.request_approval(
            data_dir=tmp_path,
            instance_id="inst_a",
            kind="deploy",
            requested_for_actor="member_a",
            operator_actor_id="operator",
            request_summary="Approve",
            binding_payload={},
            workflow_execution_id="exec-1",
            gate_nonce="nonce-1",
        )
        assert await approval_receipts.find_terminal_by_binding(
            data_dir=tmp_path,
            instance_id="inst_a",
            workflow_execution_id="exec-1",
            gate_nonce="nonce-1",
        ) is None

        ok, _ = await approval_receipts.approve(
            data_dir=tmp_path,
            approval_id=pending,
            invoking_member_id="operator",
            instance_id="inst_a",
            event_stream=None,
        )
        assert ok is True
        await approval_receipts.consume_approval(
            data_dir=tmp_path,
            approval_id=pending,
            instance_id="inst_a",
        )
        found = await approval_receipts.find_terminal_by_binding(
            data_dir=tmp_path,
            instance_id="inst_a",
            workflow_execution_id="exec-1",
            gate_nonce="nonce-1",
        )
        assert found is not None
        assert found["state"] == "consumed"
        assert found["decision"] == "approved"
        assert found["reason"] == ""
        assert found["multi_terminal"] is False

    async def test_reports_multi_terminal_and_uses_state_reason(self, tmp_path):
        await approval_receipts.ensure_schema(tmp_path)
        first = await approval_receipts.request_approval(
            data_dir=tmp_path,
            instance_id="inst_a",
            kind="deploy",
            requested_for_actor="member_a",
            operator_actor_id="operator",
            request_summary="Approve",
            binding_payload={},
            workflow_execution_id="exec-2",
            gate_nonce="nonce-2",
        )
        second = await approval_receipts.request_approval(
            data_dir=tmp_path,
            instance_id="inst_a",
            kind="deploy",
            requested_for_actor="member_a",
            operator_actor_id="operator",
            request_summary="Approve again",
            binding_payload={},
            workflow_execution_id="exec-2",
            gate_nonce="nonce-2",
        )
        await approval_receipts.approve(
            data_dir=tmp_path,
            approval_id=first,
            invoking_member_id="operator",
            instance_id="inst_a",
            event_stream=None,
        )
        ok, _ = await approval_receipts.reject(
            data_dir=tmp_path,
            approval_id=second,
            invoking_member_id="operator",
            instance_id="inst_a",
            reason="not safe",
            event_stream=None,
        )
        assert ok is True
        found = await approval_receipts.find_terminal_by_binding(
            data_dir=tmp_path,
            instance_id="inst_a",
            workflow_execution_id="exec-2",
            gate_nonce="nonce-2",
        )
        assert found is not None
        assert found["decision"] == "rejected"
        assert found["reason"] == "not safe"
        assert found["multi_terminal"] is True


class TestRefsAndValidation:
    def test_approval_outcome_ref_scope_param_and_predicate(self):
        execution = WorkflowExecution(
            execution_id="exec-1",
            workflow_id="wf",
            instance_id="inst_a",
            correlation_id="corr",
            state="running",
        )
        ctx = ResolutionContext(
            execution=execution,
            step_outputs={
                "req": {"approval_outcome": {
                    "approved": True,
                    "decision": "approved",
                }},
            },
        )
        assert resolve_references_in_value(
            "{step.req.approval_outcome.decision}", ctx,
        ) == "approved"

        missing_ctx = ResolutionContext(
            execution=execution,
            step_outputs={"req": {"approval_outcome": None}},
            mode="parameter",
        )
        with pytest.raises(RefResolutionError):
            resolve_references_in_value(
                "{step.req.approval_outcome.decision}", missing_ctx,
            )

        pred_ctx = ResolutionContext(
            execution=execution,
            step_outputs={"req": {"approval_outcome": None}},
            mode="predicate",
        )
        assert resolve_references_in_value(
            "{step.req.approval_outcome.decision}", pred_ctx,
        ) is _NOT_FOUND

    def test_registries_and_descriptor_validation_accept_request_approval(self):
        assert "request_approval" in KNOWN_ACTION_TYPES
        assert is_irreversible("request_approval") is True
        assert ACTION_OPERATION_CLASS_BY_VERB["request_approval"] == "register"
        assert RISK_LEVEL_BY_OPERATION_CLASS["register"] == "medium"

        wf = _workflow([
            _action(
                "request_approval",
                id="req",
                gate_ref="await_approval",
                kind="deploy",
                operator_actor_id="{idea_payload.operator_actor_id}",
                request_summary="Approve deploy",
                binding_payload={},
                _workflow_execution_id="{workflow.execution_id}",
                _gate_nonce="{workflow.gate_nonce}",
            ),
            _action(
                "branch",
                id="branch_on_approval",
                condition="{step.req.approval_outcome.approved}",
                branch_on_true="do_commit",
                branch_on_false="surface_rejection",
            ),
            _action("mark_state", id="do_commit", key="decision", value="yes"),
            _action(
                "mark_state", id="surface_rejection",
                key="decision", value="no",
            ),
        ])
        validate_workflow(wf)

    async def test_bringup_registers_bound_request_approval_action(self, tmp_path):
        await approval_receipts.ensure_schema(tmp_path)
        lib = ActionLibrary()
        _register_all_actions(
            lib, object(), object(), WorkflowLedger(str(tmp_path)), str(tmp_path),
        )
        action = lib.get("request_approval")
        assert isinstance(action, RequestApprovalAction)
        approval_id = await action._request_approval(
            instance_id="inst_a",
            kind="deploy",
            requested_for_actor="member_a",
            operator_actor_id="operator",
            request_summary="Approve",
            binding_payload={},
            workflow_execution_id="exec-1",
            gate_nonce="nonce-1",
        )
        assert await approval_receipts.get_receipt(
            data_dir=tmp_path, approval_id=approval_id,
        )


class TestEngineRequestApproval:
    async def test_full_approval_flow_merges_outcome_and_consumes(
        self, approval_stack,
    ):
        await approval_stack["wfr"]._register_workflow_unbound(_workflow([
            _action(
                "request_approval",
                id="req",
                gate_ref="await_approval",
                kind="deploy",
                operator_actor_id="operator",
                request_summary="Approve deploy",
                binding_payload={"sha": "abc"},
                _workflow_execution_id="{workflow.execution_id}",
                _gate_nonce="{workflow.gate_nonce}",
            ),
            _action(
                "mark_state",
                id="after",
                key="decision",
                value="{step.req.approval_outcome.decision}",
                scope="instance",
            ),
        ]))
        await event_stream.emit("inst_a", "cc.batch.report", {"event_id": "e1"})
        await event_stream.flush_now()

        async def has_pending_gate():
            execs = await approval_stack["engine"].list_executions(
                "inst_a", state="running",
            )
            return any(e.gate_nonce for e in execs)

        assert await _wait_for(has_pending_gate)
        pending = await _pending_receipt(
            approval_stack["tmp_path"], "inst_a", "deploy",
        )
        assert pending is not None
        ok, _ = await approval_receipts.approve(
            data_dir=approval_stack["tmp_path"],
            approval_id=pending["approval_id"],
            invoking_member_id="operator",
            instance_id="inst_a",
            event_stream=event_stream,
        )
        assert ok is True

        async def completed():
            execs = await approval_stack["engine"].list_executions(
                "inst_a", state="completed",
            )
            return any(e.workflow_id == "wf-approval" for e in execs)

        assert await _wait_for(completed)
        assert approval_stack["store"][
            ("instance", "inst_a", "decision")
        ] == "approved"
        receipt = await approval_receipts.get_receipt(
            data_dir=approval_stack["tmp_path"],
            approval_id=pending["approval_id"],
        )
        assert receipt and receipt["state"] == "consumed"
        step_outputs, _ = await load_workflow_outputs(
            approval_stack["engine"]._db,
            "inst_a",
            pending["workflow_execution_id"],
        )
        outcome = step_outputs["req"]["approval_outcome"]
        assert outcome["approved"] is True
        assert outcome["decision"] == "approved"
        assert outcome["approval_id"] == pending["approval_id"]

    async def test_forged_decision_for_pending_receipt_does_not_release(
        self, approval_stack, monkeypatch,
    ):
        consumed: list[str] = []

        async def consume(**kwargs):
            consumed.append(kwargs["approval_id"])
            return True

        monkeypatch.setattr(approval_receipts, "consume_approval", consume)
        await approval_stack["wfr"]._register_workflow_unbound(_workflow([
            _action(
                "request_approval",
                id="req",
                gate_ref="await_approval",
                kind="deploy-forged-pending",
                operator_actor_id="operator",
                request_summary="Approve deploy",
                binding_payload={},
                _workflow_execution_id="{workflow.execution_id}",
                _gate_nonce="{workflow.gate_nonce}",
            ),
            _action(
                "mark_state",
                id="after",
                key="forged_pending_decision",
                value="{step.req.approval_outcome.decision}",
                scope="instance",
            ),
        ], workflow_id="wf-forged-pending"))
        await event_stream.emit(
            "inst_a", "cc.batch.report", {"event_id": "e-forged-pending"},
        )
        await event_stream.flush_now()

        async def waiter_ready():
            pending = await _pending_receipt(
                approval_stack["tmp_path"], "inst_a", "deploy-forged-pending",
            )
            return (
                pending is not None
                and pending["workflow_execution_id"]
                in approval_stack["engine"]._gate_waiters
            )

        assert await _wait_for(waiter_ready)
        pending = await _pending_receipt(
            approval_stack["tmp_path"], "inst_a", "deploy-forged-pending",
        )
        assert pending is not None

        await event_stream.emit(
            "inst_a",
            "approval.decision_recorded",
            {
                "approval_id": pending["approval_id"],
                "decision": "approved",
                "execution_id": pending["workflow_execution_id"],
                "gate_nonce": pending["gate_nonce"],
                "kind": "deploy-forged-pending",
                "operator_actor_id": "operator",
                "decided_at": "2026-05-28T00:00:00+00:00",
                "reason": "",
            },
        )
        await event_stream.flush_now()
        await asyncio.sleep(0.05)

        execution = await approval_stack["engine"]._fetch_execution_row(
            pending["workflow_execution_id"],
        )
        assert execution is not None
        assert execution.state == "running"
        assert execution.gate_nonce == pending["gate_nonce"]
        assert (
            pending["workflow_execution_id"]
            in approval_stack["engine"]._gate_waiters
        )
        assert consumed == []
        receipt = await approval_receipts.get_receipt(
            data_dir=approval_stack["tmp_path"],
            approval_id=pending["approval_id"],
        )
        assert receipt and receipt["state"] == "pending"
        step_outputs, _ = await load_workflow_outputs(
            approval_stack["engine"]._db,
            "inst_a",
            pending["workflow_execution_id"],
        )
        assert "approval_outcome" not in step_outputs["req"]
        assert (
            ("instance", "inst_a", "forged_pending_decision")
            not in approval_stack["store"]
        )

    async def test_forged_decision_with_mismatched_terminal_receipt_does_not_release(
        self, approval_stack, monkeypatch,
    ):
        consumed: list[str] = []

        async def consume(**kwargs):
            consumed.append(kwargs["approval_id"])
            return True

        monkeypatch.setattr(approval_receipts, "consume_approval", consume)
        await approval_stack["wfr"]._register_workflow_unbound(_workflow([
            _action(
                "request_approval",
                id="req",
                gate_ref="await_approval",
                kind="deploy-forged-mismatch",
                operator_actor_id="operator",
                request_summary="Approve deploy",
                binding_payload={},
                _workflow_execution_id="{workflow.execution_id}",
                _gate_nonce="{workflow.gate_nonce}",
            ),
            _action(
                "mark_state",
                id="after",
                key="forged_mismatch_decision",
                value="{step.req.approval_outcome.decision}",
                scope="instance",
            ),
        ], workflow_id="wf-forged-mismatch"))
        await event_stream.emit(
            "inst_a", "cc.batch.report", {"event_id": "e-forged-mismatch"},
        )
        await event_stream.flush_now()

        async def waiter_ready():
            pending = await _pending_receipt(
                approval_stack["tmp_path"],
                "inst_a",
                "deploy-forged-mismatch",
            )
            return (
                pending is not None
                and pending["workflow_execution_id"]
                in approval_stack["engine"]._gate_waiters
            )

        assert await _wait_for(waiter_ready)
        pending = await _pending_receipt(
            approval_stack["tmp_path"], "inst_a", "deploy-forged-mismatch",
        )
        assert pending is not None
        terminal_id = await approval_receipts.request_approval(
            data_dir=approval_stack["tmp_path"],
            instance_id="inst_a",
            kind="deploy-forged-mismatch-extra",
            requested_for_actor="member_a",
            operator_actor_id="operator",
            request_summary="Approve duplicate",
            binding_payload={},
            workflow_execution_id=pending["workflow_execution_id"],
            gate_nonce=pending["gate_nonce"],
        )
        ok, _ = await approval_receipts.approve(
            data_dir=approval_stack["tmp_path"],
            approval_id=terminal_id,
            invoking_member_id="operator",
            instance_id="inst_a",
            event_stream=None,
        )
        assert ok is True

        await event_stream.emit(
            "inst_a",
            "approval.decision_recorded",
            {
                "approval_id": pending["approval_id"],
                "decision": "approved",
                "execution_id": pending["workflow_execution_id"],
                "gate_nonce": pending["gate_nonce"],
                "kind": "deploy-forged-mismatch",
                "operator_actor_id": "operator",
                "decided_at": "2026-05-28T00:00:00+00:00",
                "reason": "",
            },
        )
        await event_stream.flush_now()
        await asyncio.sleep(0.05)

        execution = await approval_stack["engine"]._fetch_execution_row(
            pending["workflow_execution_id"],
        )
        assert execution is not None
        assert execution.state == "running"
        assert execution.gate_nonce == pending["gate_nonce"]
        assert (
            pending["workflow_execution_id"]
            in approval_stack["engine"]._gate_waiters
        )
        assert consumed == []
        receipt = await approval_receipts.get_receipt(
            data_dir=approval_stack["tmp_path"],
            approval_id=pending["approval_id"],
        )
        assert receipt and receipt["state"] == "pending"
        terminal = await approval_receipts.get_receipt(
            data_dir=approval_stack["tmp_path"],
            approval_id=terminal_id,
        )
        assert terminal and terminal["state"] == "approved"
        step_outputs, _ = await load_workflow_outputs(
            approval_stack["engine"]._db,
            "inst_a",
            pending["workflow_execution_id"],
        )
        assert "approval_outcome" not in step_outputs["req"]
        assert (
            ("instance", "inst_a", "forged_mismatch_decision")
            not in approval_stack["store"]
        )

    async def test_empty_operator_from_trigger_payload_creates_no_receipt(
        self, approval_stack,
    ):
        await approval_stack["wfr"]._register_workflow_unbound(_workflow([
            _action(
                "request_approval",
                id="req",
                gate_ref="await_approval",
                kind="deploy-empty-operator",
                operator_actor_id="{idea_payload.operator_actor_id}",
                request_summary="Approve deploy",
                binding_payload={"sha": "abc"},
                _workflow_execution_id="{workflow.execution_id}",
                _gate_nonce="{workflow.gate_nonce}",
            ),
            _action(
                "mark_state",
                id="after",
                key="decision",
                value="{step.req.approval_outcome.decision}",
                scope="instance",
            ),
        ], workflow_id="wf-empty-operator"))
        await event_stream.emit(
            "inst_a", "cc.batch.report",
            {"event_id": "e-empty-operator", "operator_actor_id": ""},
        )
        await event_stream.flush_now()

        async def aborted():
            execs = await approval_stack["engine"].list_executions(
                "inst_a", state="aborted",
            )
            return any(e.workflow_id == "wf-empty-operator" for e in execs)

        assert await _wait_for(aborted)
        execs = await approval_stack["engine"].list_executions(
            "inst_a", state="aborted",
        )
        execution = next(
            e for e in execs if e.workflow_id == "wf-empty-operator"
        )
        assert execution.aborted_reason == "step_req_failed"

        assert await _pending_receipt(
            approval_stack["tmp_path"], "inst_a", "deploy-empty-operator",
        ) is None
        assert await _receipt_count(
            approval_stack["tmp_path"], kind="deploy-empty-operator",
        ) == 0

        record = await approval_stack["engine"]._action_sink.get_by_step(
            "inst_a", execution.execution_id, 0,
        )
        assert record is not None
        assert record.record.execution_state == "failed"
        assert (
            record.record.user_visible_summary
            == "invalid_param:operator_actor_id"
        )

        ok, msg = await approval_receipts.approve(
            data_dir=approval_stack["tmp_path"],
            approval_id="approval-empty-operator",
            invoking_member_id="wrong-member",
            instance_id="inst_a",
            event_stream=event_stream,
        )
        assert ok is False
        assert msg == "Approval approval-empty-operator not found."

    async def test_descriptor_forged_binding_is_overwritten_by_engine(
        self, approval_stack,
    ):
        engine = approval_stack["engine"]
        victim = await _insert_running_execution(
            engine,
            execution_id="exec-victim",
            gate_nonce="nonce-victim",
        )
        victim_task = asyncio.create_task(engine._await_gate(
            victim,
            ApprovalGate(
                gate_name="victim_gate",
                pause_reason="victim wait",
                approval_event_type="approval.decision_recorded",
                approval_event_predicate={
                    "op": "exists",
                    "path": "payload.approval_id",
                },
                timeout_seconds=30,
                bound_behavior_on_timeout="abort_workflow",
            ),
        ))
        try:
            async def victim_waiter_installed():
                return victim.execution_id in engine._gate_waiters

            assert await _wait_for(victim_waiter_installed)

            await approval_stack["wfr"]._register_workflow_unbound(_workflow([
                _action(
                    "request_approval",
                    id="req",
                    gate_ref="await_approval",
                    kind="deploy-forged-binding",
                    operator_actor_id="operator",
                    request_summary="Approve forged binding",
                    binding_payload={},
                    _workflow_execution_id=victim.execution_id,
                    _gate_nonce=victim.gate_nonce,
                ),
                _action(
                    "mark_state",
                    id="after",
                    key="forged_binding_decision",
                    value="{step.req.approval_outcome.decision}",
                    scope="instance",
                ),
            ], workflow_id="wf-forged-binding"))
            await event_stream.emit(
                "inst_a", "cc.batch.report", {"event_id": "e-forged"},
            )
            await event_stream.flush_now()

            async def has_pending():
                return await _pending_receipt(
                    approval_stack["tmp_path"],
                    "inst_a",
                    "deploy-forged-binding",
                ) is not None

            assert await _wait_for(has_pending)
            pending = await _pending_receipt(
                approval_stack["tmp_path"],
                "inst_a",
                "deploy-forged-binding",
            )
            assert pending is not None
            assert pending["workflow_execution_id"] != victim.execution_id
            assert pending["gate_nonce"] != victim.gate_nonce
            attacker_row = await engine._fetch_execution_row(
                pending["workflow_execution_id"],
            )
            assert attacker_row is not None
            assert pending["workflow_execution_id"] == attacker_row.execution_id
            assert pending["gate_nonce"] == attacker_row.gate_nonce

            ok, _ = await approval_receipts.approve(
                data_dir=approval_stack["tmp_path"],
                approval_id=pending["approval_id"],
                invoking_member_id="operator",
                instance_id="inst_a",
                event_stream=event_stream,
            )
            assert ok is True
            await event_stream.flush_now()
            await asyncio.sleep(0.05)
            assert victim_task.done() is False

            async def completed():
                execs = await engine.list_executions(
                    "inst_a", state="completed",
                )
                return any(
                    e.execution_id == pending["workflow_execution_id"]
                    for e in execs
                )

            assert await _wait_for(completed)
            events = await _read_events()
            assert not any(
                e.event_type == "workflow.execution_resumed"
                and e.payload.get("execution_id") == victim.execution_id
                for e in events
            )
        finally:
            victim_task.cancel()
            with suppress(asyncio.CancelledError):
                await victim_task

    async def test_rejected_flow_merges_outcome_and_does_not_consume(
        self, approval_stack,
    ):
        await approval_stack["wfr"]._register_workflow_unbound(_workflow([
            _action(
                "request_approval",
                id="req",
                gate_ref="await_approval",
                kind="deploy",
                operator_actor_id="operator",
                request_summary="Approve deploy",
                binding_payload={},
                _workflow_execution_id="{workflow.execution_id}",
                _gate_nonce="{workflow.gate_nonce}",
            ),
            _action(
                "mark_state",
                id="after",
                key="decision",
                value="{step.req.approval_outcome.decision}",
                scope="instance",
            ),
        ], workflow_id="wf-rejected"))
        await event_stream.emit("inst_a", "cc.batch.report", {"event_id": "e2"})
        await event_stream.flush_now()

        async def has_pending():
            return await _pending_receipt(
                approval_stack["tmp_path"], "inst_a", "deploy",
            ) is not None

        assert await _wait_for(has_pending)
        pending = await _pending_receipt(
            approval_stack["tmp_path"], "inst_a", "deploy",
        )
        ok, _ = await approval_receipts.reject(
            data_dir=approval_stack["tmp_path"],
            approval_id=pending["approval_id"],
            invoking_member_id="operator",
            instance_id="inst_a",
            reason="no",
            event_stream=event_stream,
        )
        assert ok is True

        async def completed():
            execs = await approval_stack["engine"].list_executions(
                "inst_a", state="completed",
            )
            return any(e.workflow_id == "wf-rejected" for e in execs)

        assert await _wait_for(completed)
        assert approval_stack["store"][
            ("instance", "inst_a", "decision")
        ] == "rejected"
        receipt = await approval_receipts.get_receipt(
            data_dir=approval_stack["tmp_path"],
            approval_id=pending["approval_id"],
        )
        assert receipt and receipt["state"] == "rejected"
        step_outputs, _ = await load_workflow_outputs(
            approval_stack["engine"]._db,
            "inst_a",
            pending["workflow_execution_id"],
        )
        outcome = step_outputs["req"]["approval_outcome"]
        assert outcome["approved"] is False
        assert outcome["decision"] == "rejected"
        assert outcome["rejection_reason"] == "no"

    async def test_await_gate_short_circuits_terminal_before_await(
        self, approval_stack,
    ):
        execution = await _insert_running_execution(
            approval_stack["engine"],
            execution_id="exec-short",
            gate_nonce="nonce-short",
        )
        approval_id = await approval_receipts.request_approval(
            data_dir=approval_stack["tmp_path"],
            instance_id="inst_a",
            kind="deploy",
            requested_for_actor="member_a",
            operator_actor_id="operator",
            request_summary="Approve",
            binding_payload={},
            workflow_execution_id=execution.execution_id,
            gate_nonce=execution.gate_nonce,
        )
        await approval_receipts.approve(
            data_dir=approval_stack["tmp_path"],
            approval_id=approval_id,
            invoking_member_id="operator",
            instance_id="inst_a",
            event_stream=None,
        )
        cont, payload = await approval_stack["engine"]._await_gate(
            execution,
            ApprovalGate(
                gate_name="g",
                pause_reason="",
                approval_event_type="approval.decision_recorded",
                approval_event_predicate={
                    "op": "exists",
                    "path": "payload.approval_id",
                },
                timeout_seconds=2,
                bound_behavior_on_timeout="abort_workflow",
            ),
        )
        assert cont is True
        assert payload["approval_id"] == approval_id
        assert payload["decision"] == "approved"
        events = await _read_events()
        assert any(
            e.event_type == "workflow.gate_receipt_short_circuited"
            for e in events
        )

    async def test_await_gate_ignores_nonmatching_terminal_receipt(
        self, approval_stack, monkeypatch,
    ):
        execution = await _insert_running_execution(
            approval_stack["engine"],
            execution_id="exec-stray-terminal",
            gate_nonce="nonce-stray-terminal",
        )
        matching_approval_id = await approval_receipts.request_approval(
            data_dir=approval_stack["tmp_path"],
            instance_id="inst_a",
            kind="deploy",
            requested_for_actor="member_a",
            operator_actor_id="operator",
            request_summary="Approve",
            binding_payload={},
            workflow_execution_id=execution.execution_id,
            gate_nonce=execution.gate_nonce,
        )
        stray_approval_id = await approval_receipts.request_approval(
            data_dir=approval_stack["tmp_path"],
            instance_id="inst_a",
            kind="deploy",
            requested_for_actor="member_a",
            operator_actor_id="operator",
            request_summary="Approve duplicate",
            binding_payload={},
            workflow_execution_id=execution.execution_id,
            gate_nonce=execution.gate_nonce,
        )
        ok, _ = await approval_receipts.approve(
            data_dir=approval_stack["tmp_path"],
            approval_id=matching_approval_id,
            invoking_member_id="operator",
            instance_id="inst_a",
            event_stream=None,
        )
        assert ok is True
        ok, _ = await approval_receipts.approve(
            data_dir=approval_stack["tmp_path"],
            approval_id=stray_approval_id,
            invoking_member_id="operator",
            instance_id="inst_a",
            event_stream=None,
        )
        assert ok is True
        async with aiosqlite.connect(
            str(approval_stack["tmp_path"] / "instance.db"),
        ) as db:
            await db.execute(
                "UPDATE approval_receipts SET decided_at = ? "
                "WHERE approval_id = ?",
                ("2026-05-28T00:00:00+00:00", matching_approval_id),
            )
            await db.execute(
                "UPDATE approval_receipts SET decided_at = ? "
                "WHERE approval_id = ?",
                ("2026-05-28T00:00:01+00:00", stray_approval_id),
            )
            await db.commit()

        lookup_finished = asyncio.Event()
        original_find = approval_receipts.find_terminal_by_binding
        returned_receipts: list[dict] = []

        async def find_and_signal(**kwargs):
            receipt = await original_find(**kwargs)
            returned_receipts.append(receipt)
            lookup_finished.set()
            return receipt

        monkeypatch.setattr(
            approval_receipts,
            "find_terminal_by_binding",
            find_and_signal,
        )
        task = asyncio.create_task(approval_stack["engine"]._await_gate(
            execution,
            ApprovalGate(
                gate_name="g",
                pause_reason="",
                approval_event_type="approval.decision_recorded",
                approval_event_predicate={
                    "op": "eq",
                    "path": "payload.approval_id",
                    "value": matching_approval_id,
                },
                timeout_seconds=3,
                bound_behavior_on_timeout="abort_workflow",
            ),
        ))
        await asyncio.wait_for(lookup_finished.wait(), timeout=1)
        assert returned_receipts[0]["approval_id"] == stray_approval_id

        await event_stream.emit(
            "inst_a",
            "approval.decision_recorded",
            {
                "approval_id": matching_approval_id,
                "decision": "approved",
                "execution_id": execution.execution_id,
                "gate_nonce": execution.gate_nonce,
                "kind": "deploy",
                "operator_actor_id": "operator",
                "decided_at": "2026-05-28T00:00:00+00:00",
                "reason": "",
            },
        )
        await event_stream.flush_now()

        cont, payload = await task
        assert cont is True
        assert payload["approval_id"] == matching_approval_id
        events = await _read_events()
        assert not any(
            e.event_type == "workflow.gate_receipt_short_circuited"
            for e in events
        )

    async def test_await_gate_catches_event_after_waiter_install(
        self, approval_stack, monkeypatch,
    ):
        execution = await _insert_running_execution(
            approval_stack["engine"],
            execution_id="exec-race",
            gate_nonce="nonce-race",
        )
        approval_id = await approval_receipts.request_approval(
            data_dir=approval_stack["tmp_path"],
            instance_id="inst_a",
            kind="deploy",
            requested_for_actor="member_a",
            operator_actor_id="operator",
            request_summary="Approve",
            binding_payload={},
            workflow_execution_id=execution.execution_id,
            gate_nonce=execution.gate_nonce,
        )
        lookup_started = asyncio.Event()
        original_find = approval_receipts.find_terminal_by_binding
        lookup_calls = 0

        async def lookup_slow_first_then_durable(**kwargs):
            nonlocal lookup_calls
            lookup_calls += 1
            if lookup_calls > 1:
                return await original_find(**kwargs)
            lookup_started.set()
            await asyncio.sleep(0.1)
            return None

        monkeypatch.setattr(
            approval_receipts,
            "find_terminal_by_binding",
            lookup_slow_first_then_durable,
        )
        gate_task = asyncio.create_task(approval_stack["engine"]._await_gate(
            execution,
            ApprovalGate(
                gate_name="g",
                pause_reason="",
                approval_event_type="approval.decision_recorded",
                approval_event_predicate={
                    "op": "exists",
                    "path": "payload.approval_id",
                },
                timeout_seconds=3,
                bound_behavior_on_timeout="abort_workflow",
            ),
        ))
        await asyncio.wait_for(lookup_started.wait(), timeout=1)
        ok, _ = await approval_receipts.approve(
            data_dir=approval_stack["tmp_path"],
            approval_id=approval_id,
            invoking_member_id="operator",
            instance_id="inst_a",
            event_stream=event_stream,
        )
        assert ok is True
        cont, payload = await gate_task
        assert cont is True
        assert payload["approval_id"] == approval_id
        assert approval_stack["engine"]._gate_release_payloads == {}

    async def test_lookup_failure_falls_through_to_wait_path(
        self, approval_stack, monkeypatch,
    ):
        execution = await _insert_running_execution(
            approval_stack["engine"],
            execution_id="exec-lookup-fail",
            gate_nonce="nonce-lookup-fail",
        )
        approval_id = await approval_receipts.request_approval(
            data_dir=approval_stack["tmp_path"],
            instance_id="inst_a",
            kind="deploy",
            requested_for_actor="member_a",
            operator_actor_id="operator",
            request_summary="Approve",
            binding_payload={},
            workflow_execution_id=execution.execution_id,
            gate_nonce=execution.gate_nonce,
        )
        lookup_called = asyncio.Event()
        original_find = approval_receipts.find_terminal_by_binding
        lookup_calls = 0

        async def lookup_raises_once(**kwargs):
            nonlocal lookup_calls
            lookup_calls += 1
            if lookup_calls > 1:
                return await original_find(**kwargs)
            lookup_called.set()
            raise RuntimeError("lookup failed")

        monkeypatch.setattr(
            approval_receipts,
            "find_terminal_by_binding",
            lookup_raises_once,
        )
        task = asyncio.create_task(approval_stack["engine"]._await_gate(
            execution,
            ApprovalGate(
                gate_name="g",
                pause_reason="",
                approval_event_type="approval.decision_recorded",
                approval_event_predicate={
                    "op": "exists",
                    "path": "payload.approval_id",
                },
                timeout_seconds=3,
                bound_behavior_on_timeout="abort_workflow",
            ),
        ))
        await asyncio.wait_for(lookup_called.wait(), timeout=1)
        ok, _ = await approval_receipts.approve(
            data_dir=approval_stack["tmp_path"],
            approval_id=approval_id,
            invoking_member_id="operator",
            instance_id="inst_a",
            event_stream=event_stream,
        )
        assert ok is True
        cont, payload = await task
        assert cont is True
        assert payload["approval_id"] == approval_id
        events = await _read_events()
        assert any(
            e.event_type == "workflow.gate_receipt_lookup_failed"
            for e in events
        )

    async def test_short_circuit_prefers_matched_payload_during_lookup(
        self, approval_stack, monkeypatch,
    ):
        execution = await _insert_running_execution(
            approval_stack["engine"],
            execution_id="exec-prefer",
            gate_nonce="nonce-prefer",
        )
        real_approval_id = await approval_receipts.request_approval(
            data_dir=approval_stack["tmp_path"],
            instance_id="inst_a",
            kind="deploy",
            requested_for_actor="member_a",
            operator_actor_id="operator",
            request_summary="Approve real",
            binding_payload={},
            workflow_execution_id=execution.execution_id,
            gate_nonce=execution.gate_nonce,
        )
        ok, _ = await approval_receipts.approve(
            data_dir=approval_stack["tmp_path"],
            approval_id=real_approval_id,
            invoking_member_id="operator",
            instance_id="inst_a",
            event_stream=None,
        )
        assert ok is True
        emitted = asyncio.Event()
        original_find = approval_receipts.find_terminal_by_binding
        lookup_calls = 0

        async def lookup_after_event(**kwargs):
            nonlocal lookup_calls
            lookup_calls += 1
            if lookup_calls > 1:
                return await original_find(**kwargs)
            await emitted.wait()
            return {
                "approval_id": "approval-synth",
                "decision": "approved",
                "kind": "deploy",
                "operator_actor_id": "operator",
                "decided_at": "synth-time",
                "reason": "synth",
                "multi_terminal": True,
            }

        monkeypatch.setattr(
            approval_receipts,
            "find_terminal_by_binding",
            lookup_after_event,
        )
        task = asyncio.create_task(approval_stack["engine"]._await_gate(
            execution,
            ApprovalGate(
                gate_name="g",
                pause_reason="",
                approval_event_type="approval.decision_recorded",
                approval_event_predicate={
                    "op": "OR",
                    "operands": [
                        {
                            "op": "eq",
                            "path": "payload.approval_id",
                            "value": real_approval_id,
                        },
                        {
                            "op": "eq",
                            "path": "payload.approval_id",
                            "value": "approval-synth",
                        },
                    ],
                },
                timeout_seconds=3,
                bound_behavior_on_timeout="abort_workflow",
            ),
        ))
        await asyncio.sleep(0)
        await event_stream.emit(
            "inst_a",
            "approval.decision_recorded",
            {
                "approval_id": real_approval_id,
                "decision": "approved",
                "execution_id": execution.execution_id,
                "gate_nonce": execution.gate_nonce,
                "kind": "deploy",
                "operator_actor_id": "operator",
                "decided_at": "real-time",
                "reason": "real",
            },
        )
        await event_stream.flush_now()
        emitted.set()
        cont, payload = await task
        assert cont is True
        assert payload["approval_id"] == real_approval_id
        assert payload["reason"] == "real"
        assert approval_stack["engine"]._gate_release_payloads == {}
        events = await _read_events()
        assert any(
            e.event_type == "workflow.gate_receipt_multi_terminal"
            for e in events
        )

    async def test_short_circuit_final_pop_prefers_late_matched_payload(
        self, approval_stack, monkeypatch,
    ):
        execution = await _insert_running_execution(
            approval_stack["engine"],
            execution_id="exec-final-pop-race",
            gate_nonce="nonce-final-pop-race",
        )
        real_payload = {
            "approval_id": "approval-real",
            "decision": "approved",
            "execution_id": execution.execution_id,
            "gate_nonce": execution.gate_nonce,
            "kind": "deploy",
            "operator_actor_id": "operator",
            "decided_at": "real-time",
            "reason": "real",
        }

        class _ReleasePayloadRace(dict):
            def __init__(self):
                super().__init__()
                self.injected = False

            def pop(self, key, default=None):
                value = super().pop(key, default)
                if key == execution.execution_id and not self.injected:
                    self.injected = True
                    self[key] = dict(real_payload)
                return value

        async def lookup_terminal(**kwargs):
            return {
                "approval_id": "approval-synth",
                "decision": "approved",
                "kind": "deploy",
                "operator_actor_id": "operator",
                "decided_at": "synth-time",
                "reason": "synth",
            }

        monkeypatch.setattr(
            approval_receipts,
            "find_terminal_by_binding",
            lookup_terminal,
        )
        race_payloads = _ReleasePayloadRace()
        approval_stack["engine"]._gate_release_payloads = race_payloads
        cont, payload = await approval_stack["engine"]._await_gate(
            execution,
            ApprovalGate(
                gate_name="g",
                pause_reason="",
                approval_event_type="approval.decision_recorded",
                approval_event_predicate={
                    "op": "OR",
                    "operands": [
                        {
                            "op": "eq",
                            "path": "payload.approval_id",
                            "value": "approval-real",
                        },
                        {
                            "op": "eq",
                            "path": "payload.approval_id",
                            "value": "approval-synth",
                        },
                    ],
                },
                timeout_seconds=2,
                bound_behavior_on_timeout="abort_workflow",
            ),
        )
        assert cont is True
        assert payload["approval_id"] == "approval-real"
        assert payload["reason"] == "real"
        assert dict(race_payloads) == {}

    async def test_restart_resume_picks_up_terminal_receipt(
        self, approval_stack,
    ):
        await approval_stack["wfr"]._register_workflow_unbound(_workflow([
            _action(
                "request_approval",
                id="req",
                gate_ref="await_approval",
                kind="deploy",
                operator_actor_id="operator",
                request_summary="Approve deploy",
                binding_payload={},
                _workflow_execution_id="{workflow.execution_id}",
                _gate_nonce="{workflow.gate_nonce}",
            ),
            _action(
                "mark_state",
                id="after",
                key="restart_decision",
                value="{step.req.approval_outcome.decision}",
                scope="instance",
            ),
        ], workflow_id="wf-restart"))
        await event_stream.emit("inst_a", "cc.batch.report", {"event_id": "e3"})
        await event_stream.flush_now()

        async def has_pending():
            return await _pending_receipt(
                approval_stack["tmp_path"], "inst_a", "deploy",
            ) is not None

        assert await _wait_for(has_pending)
        pending = await _pending_receipt(
            approval_stack["tmp_path"], "inst_a", "deploy",
        )
        await _crash_engine_without_gate_timeout(approval_stack["engine"])
        ok, _ = await approval_receipts.approve(
            data_dir=approval_stack["tmp_path"],
            approval_id=pending["approval_id"],
            invoking_member_id="operator",
            instance_id="inst_a",
            event_stream=None,
        )
        assert ok is True
        restarted = ExecutionEngine()
        await restarted.start(
            str(approval_stack["tmp_path"]),
            approval_stack["trig"],
            approval_stack["wfr"],
            approval_stack["lib"],
            approval_stack["ledger"],
        )
        approval_stack["engine"] = restarted

        async def completed():
            execs = await restarted.list_executions(
                "inst_a", state="completed",
            )
            return any(e.workflow_id == "wf-restart" for e in execs)

        assert await _wait_for(completed)
        assert approval_stack["store"][
            ("instance", "inst_a", "restart_decision")
        ] == "approved"
        events = await _read_events()
        assert any(
            e.event_type == "workflow.gate_receipt_short_circuited"
            for e in events
        )

    async def test_gate_release_merges_restart_roundtrip_and_missing_abort(
        self, approval_stack, monkeypatch,
    ):
        engine = approval_stack["engine"]
        execution = await _insert_running_execution(
            engine,
            execution_id="exec-merge",
            gate_nonce="nonce-merge",
        )
        await engine._run_workflow_txn(lambda db: capture_step_output(
            db,
            instance_id="inst_a",
            workflow_execution_id=execution.execution_id,
            step_id="req",
            envelope=build_output_envelope(
                success=True,
                value={"approval_id": "approval-merge"},
                error=None,
                receipt={},
            ),
        ))
        consumed: list[str] = []

        async def consume(**kwargs):
            consumed.append(kwargs["approval_id"])
            return True

        monkeypatch.setattr(approval_receipts, "consume_approval", consume)
        approval_merge = await approval_receipts.request_approval(
            data_dir=approval_stack["tmp_path"],
            instance_id="inst_a",
            kind="deploy-merge",
            requested_for_actor="member_a",
            operator_actor_id="operator",
            request_summary="Approve merge",
            binding_payload={},
            workflow_execution_id=execution.execution_id,
            gate_nonce=execution.gate_nonce,
        )
        ok, _ = await approval_receipts.approve(
            data_dir=approval_stack["tmp_path"],
            approval_id=approval_merge,
            invoking_member_id="operator",
            instance_id="inst_a",
            event_stream=None,
        )
        assert ok is True
        updated = await engine._clear_gate_and_advance(
            execution,
            0,
            gate_name="await_approval",
            gate_output_payload={
                "approval_id": approval_merge,
                "decision": "approved",
                "execution_id": execution.execution_id,
                "gate_nonce": "nonce-merge",
                "kind": "deploy",
                "operator_actor_id": "operator",
                "decided_at": "2026-05-28T00:00:00+00:00",
                "reason": "",
            },
            step_id="req",
            approval_event_type="approval.decision_recorded",
        )
        assert updated is True
        assert consumed == [approval_merge]
        async with aiosqlite.connect(
            str(approval_stack["tmp_path"] / "instance.db"),
        ) as db:
            db.row_factory = aiosqlite.Row
            step_outputs, _ = await load_workflow_outputs(
                db, "inst_a", execution.execution_id,
            )
        assert step_outputs["req"]["approval_outcome"]["approved"] is True

        rejected = await _insert_running_execution(
            engine,
            execution_id="exec-rejected-direct",
            gate_nonce="nonce-rejected-direct",
        )
        await engine._run_workflow_txn(lambda db: capture_step_output(
            db,
            instance_id="inst_a",
            workflow_execution_id=rejected.execution_id,
            step_id="req",
            envelope=build_output_envelope(
                success=True, value={}, error=None, receipt={},
            ),
        ))
        approval_rejected = await approval_receipts.request_approval(
            data_dir=approval_stack["tmp_path"],
            instance_id="inst_a",
            kind="deploy-rejected-direct",
            requested_for_actor="member_a",
            operator_actor_id="operator",
            request_summary="Reject direct",
            binding_payload={},
            workflow_execution_id=rejected.execution_id,
            gate_nonce=rejected.gate_nonce,
        )
        ok, _ = await approval_receipts.reject(
            data_dir=approval_stack["tmp_path"],
            approval_id=approval_rejected,
            invoking_member_id="operator",
            instance_id="inst_a",
            reason="no",
            event_stream=None,
        )
        assert ok is True
        await engine._clear_gate_and_advance(
            rejected,
            0,
            gate_output_payload={
                "approval_id": approval_rejected,
                "decision": "rejected",
                "operator_actor_id": "operator",
                "decided_at": "2026-05-28T00:00:00+00:00",
                "reason": "no",
            },
            step_id="req",
            approval_event_type="approval.decision_recorded",
        )
        expired = await _insert_running_execution(
            engine,
            execution_id="exec-expired-direct",
            gate_nonce="nonce-expired-direct",
        )
        await engine._run_workflow_txn(lambda db: capture_step_output(
            db,
            instance_id="inst_a",
            workflow_execution_id=expired.execution_id,
            step_id="req",
            envelope=build_output_envelope(
                success=True, value={}, error=None, receipt={},
            ),
        ))
        approval_expired = await approval_receipts.request_approval(
            data_dir=approval_stack["tmp_path"],
            instance_id="inst_a",
            kind="deploy-expired-direct",
            requested_for_actor="member_a",
            operator_actor_id="operator",
            request_summary="Expire direct",
            binding_payload={},
            workflow_execution_id=expired.execution_id,
            gate_nonce=expired.gate_nonce,
            ttl_seconds=-1,
        )
        expired_count = await approval_receipts.expire_pass(
            data_dir=approval_stack["tmp_path"],
            event_stream=None,
        )
        assert expired_count >= 1
        await engine._clear_gate_and_advance(
            expired,
            0,
            gate_output_payload={
                "approval_id": approval_expired,
                "decision": "expired",
                "operator_actor_id": "operator",
                "decided_at": "2026-05-28T00:00:00+00:00",
                "reason": "",
            },
            step_id="req",
            approval_event_type="approval.decision_recorded",
        )
        assert consumed == [approval_merge]

        missing = await _insert_running_execution(
            engine,
            execution_id="exec-missing",
            gate_nonce="nonce-missing",
        )
        approval_missing = await approval_receipts.request_approval(
            data_dir=approval_stack["tmp_path"],
            instance_id="inst_a",
            kind="deploy-missing-direct",
            requested_for_actor="member_a",
            operator_actor_id="operator",
            request_summary="Approve missing",
            binding_payload={},
            workflow_execution_id=missing.execution_id,
            gate_nonce=missing.gate_nonce,
        )
        ok, _ = await approval_receipts.approve(
            data_dir=approval_stack["tmp_path"],
            approval_id=approval_missing,
            invoking_member_id="operator",
            instance_id="inst_a",
            event_stream=None,
        )
        assert ok is True
        updated = await engine._clear_gate_and_advance(
            missing,
            0,
            gate_output_payload={
                "approval_id": approval_missing,
                "decision": "approved",
                "operator_actor_id": "operator",
                "decided_at": "2026-05-28T00:00:00+00:00",
                "reason": "",
            },
            step_id="req",
            approval_event_type="approval.decision_recorded",
        )
        assert updated is False
        row = await engine._fetch_execution_row("exec-missing")
        assert row is not None
        assert row.state == "aborted"
        assert row.aborted_reason == "gate_release_missing_step_output:req"

    async def test_non_approval_gate_payload_does_not_merge_or_consume(
        self, approval_stack, monkeypatch,
    ):
        engine = approval_stack["engine"]
        execution = await _insert_running_execution(
            engine,
            execution_id="exec-non-approval-gate",
            gate_nonce="nonce-non-approval-gate",
        )
        await engine._run_workflow_txn(lambda db: capture_step_output(
            db,
            instance_id="inst_a",
            workflow_execution_id=execution.execution_id,
            step_id="req",
            envelope=build_output_envelope(
                success=True, value={}, error=None, receipt={},
            ),
        ))
        consumed: list[str] = []

        async def consume(**kwargs):
            consumed.append(kwargs["approval_id"])
            return True

        monkeypatch.setattr(approval_receipts, "consume_approval", consume)
        updated = await engine._clear_gate_and_advance(
            execution,
            0,
            gate_name="await_domain_event",
            gate_output_payload={
                "approval_id": "approval-domain",
                "decision": "approved",
                "execution_id": execution.execution_id,
                "gate_nonce": "nonce-non-approval-gate",
                "kind": "deploy",
                "operator_actor_id": "operator",
                "decided_at": "2026-05-28T00:00:00+00:00",
                "reason": "",
            },
            step_id="req",
            approval_event_type="domain.event",
        )
        assert updated is True
        assert consumed == []
        async with aiosqlite.connect(
            str(approval_stack["tmp_path"] / "instance.db"),
        ) as db:
            db.row_factory = aiosqlite.Row
            step_outputs, gate_outputs = await load_workflow_outputs(
                db, "inst_a", execution.execution_id,
            )
        assert "approval_outcome" not in step_outputs["req"]
        assert gate_outputs["await_domain_event"]["value"]["approval_id"] == (
            "approval-domain"
        )


async def _pending_receipt(tmp_path, instance_id: str, kind: str) -> dict | None:
    async with aiosqlite.connect(str(tmp_path / "instance.db")) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM approval_receipts "
            "WHERE instance_id = ? AND kind = ? AND state = 'pending' "
            "ORDER BY requested_at DESC LIMIT 1",
            (instance_id, kind),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def _receipt_count(tmp_path, *, kind: str | None = None) -> int:
    async with aiosqlite.connect(str(tmp_path / "instance.db")) as db:
        if kind is None:
            async with db.execute(
                "SELECT COUNT(*) FROM approval_receipts",
            ) as cur:
                row = await cur.fetchone()
        else:
            async with db.execute(
                "SELECT COUNT(*) FROM approval_receipts WHERE kind = ?",
                (kind,),
            ) as cur:
                row = await cur.fetchone()
    return int(row[0])


async def _insert_running_execution(
    engine: ExecutionEngine,
    *,
    execution_id: str,
    gate_nonce: str,
) -> WorkflowExecution:
    execution = WorkflowExecution(
        execution_id=execution_id,
        workflow_id="wf-direct",
        instance_id="inst_a",
        correlation_id=f"corr-{execution_id}",
        state="running",
        started_at=datetime.now(timezone.utc).isoformat(),
        gate_nonce=gate_nonce,
    )
    await engine._run_workflow_write(lambda db: db.execute(
        "INSERT INTO workflow_executions ("
        " execution_id, workflow_id, instance_id, correlation_id,"
        " state, action_index_completed, intermediate_state,"
        " last_heartbeat, aborted_reason, started_at, terminated_at,"
        " trigger_event_payload, trigger_event_id, member_id,"
        " gate_nonce, fire_id"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        execution.to_row(),
    ))
    return execution


async def _crash_engine_without_gate_timeout(engine: ExecutionEngine) -> None:
    if engine._worker_task is not None:
        engine._worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await engine._worker_task
        engine._worker_task = None
    if engine._gate_hook_registered:
        event_stream.unregister_post_flush_hook(engine._on_post_flush_for_gates)
        engine._gate_hook_registered = False
    if engine._db is not None:
        await engine._db.close()
        engine._db = None
