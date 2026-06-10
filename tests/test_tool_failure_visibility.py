"""TOOL-ARG-REPAIR-V1 Phase 0 — typed tool-failure visibility.

Root cause (spec §1.4, live-verified 2026-06-09): a tool that RETURNS its
failure (rather than raising) was wrapped ``is_error=False`` at both live
dispatch boundaries, so semantic failures were invisible to the
orchestration layer — the plan-spine marked steps complete over a failed
schedule/consult/register call and nothing retried.

These tests pin the Phase 0 contract end to end:
  1. ``ToolFailure`` is a ``str`` — legacy consumers unchanged.
  2. The three regression-corpus tools return it on validation rejection.
  3. ``LiveExecutor`` and ``LiveIntegrationDispatcher`` map it to
     ``is_error=True`` (events included).
  4. ``StepDispatcher`` yields ``completed=False`` over it — the plan
     does NOT advance over a returned failure.
"""
from __future__ import annotations

import json

from unittest.mock import AsyncMock, MagicMock

from kernos.kernel.tool_failure import ToolFailure
from kernos.kernel.enactment.dispatcher import (
    StepDispatcher,
    StepDispatchInputs,
    ToolExecutionInputs,
    ToolExecutionResult,
)
from kernos.kernel.gate import GateResult
from kernos.kernel.integration.live_wiring import (
    LiveExecutor,
    LiveIntegrationDispatcher,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inputs(tool_id: str = "manage_schedule", args: dict | None = None) -> ToolExecutionInputs:
    return ToolExecutionInputs(
        tool_id=tool_id,
        arguments=args or {},
        operation_name=tool_id,
        instance_id="inst-x",
        member_id="mem-x",
        space_id="space-x",
        turn_id="turn-x",
    )


def _gate(classification: str = "read") -> MagicMock:
    gate = MagicMock()
    gate.classify_tool_effect.return_value = classification
    gate.evaluate = AsyncMock(return_value=GateResult(
        allowed=True, reason="approved", method="model_check",
    ))
    return gate


# ---------------------------------------------------------------------------
# 1. ToolFailure str semantics — legacy paths unchanged by construction
# ---------------------------------------------------------------------------


class TestToolFailureIsAString:
    def test_is_str_and_carries_metadata(self):
        f = ToolFailure("Error: nope", code="schedule_underspecified",
                        pre_side_effect=True)
        assert isinstance(f, str)
        assert f == "Error: nope"
        assert "nope" in f
        assert f.code == "schedule_underspecified"
        assert f.pre_side_effect is True

    def test_json_serializes_as_plain_string(self):
        f = ToolFailure("boom", code="x")
        assert json.dumps({"result": f}) == '{"result": "boom"}'

    def test_defaults_are_conservative(self):
        # Unsafe-to-retry unless explicitly tagged otherwise.
        f = ToolFailure("err")
        assert f.pre_side_effect is False
        assert f.code == "tool_error"

    def test_repr_surfaces_the_type_for_logs(self):
        f = ToolFailure("err", code="c")
        assert "ToolFailure" in repr(f) and "c" in repr(f)


# ---------------------------------------------------------------------------
# 2. The three regression-corpus tools return typed failures
# ---------------------------------------------------------------------------


class TestManageScheduleReturnsTypedFailure:
    async def test_create_without_description(self, tmp_path):
        from kernos.kernel.scheduler import TriggerStore, handle_manage_schedule
        store = TriggerStore(str(tmp_path))
        res = await handle_manage_schedule(
            store, "inst", "mem", "space", "create", description="",
        )
        assert isinstance(res, ToolFailure)
        assert res.pre_side_effect is True
        assert "description" in res

    async def test_extraction_failure_is_typed(self, tmp_path):
        from kernos.kernel.scheduler import TriggerStore, handle_manage_schedule

        class _NoWhen:
            async def complete_simple(self, **kw):
                return json.dumps({
                    "action_type": "notify", "when": "", "message": "x",
                    "recurrence": "", "delivery_class": "stage",
                    "notify_via": "", "tool_name": "", "tool_args": "",
                    "condition_type": "time", "event_source": "",
                    "event_filter": "", "event_lead_minutes": 0,
                })

        store = TriggerStore(str(tmp_path))
        res = await handle_manage_schedule(
            store, "inst", "mem", "space", "create",
            description="note with no time at all",
            reasoning_service=_NoWhen(),
        )
        assert isinstance(res, ToolFailure)
        assert res.code == "schedule_underspecified"
        assert res.pre_side_effect is True
        # Nothing was written.
        assert await store.list_all("inst") == []

    async def test_unknown_action_is_typed(self, tmp_path):
        from kernos.kernel.scheduler import TriggerStore, handle_manage_schedule
        store = TriggerStore(str(tmp_path))
        res = await handle_manage_schedule(
            store, "inst", "mem", "space", "frobnicate",
        )
        assert isinstance(res, ToolFailure)

    async def test_successful_create_is_not_a_failure(self, tmp_path):
        from kernos.kernel.scheduler import TriggerStore, handle_manage_schedule

        class _Good:
            async def complete_simple(self, **kw):
                return json.dumps({
                    "action_type": "notify", "when": "2026-06-09T23:00:00",
                    "message": "x", "recurrence": "",
                    "delivery_class": "stage", "notify_via": "",
                    "tool_name": "", "tool_args": "",
                    "condition_type": "time", "event_source": "",
                    "event_filter": "", "event_lead_minutes": 0,
                })

        store = TriggerStore(str(tmp_path))
        res = await handle_manage_schedule(
            store, "inst", "mem", "space", "create",
            description="remind me at 11pm",
            reasoning_service=_Good(),
            user_timezone="UTC",
        )
        assert not isinstance(res, ToolFailure)
        assert "Scheduled" in res


class TestRegisterToolReturnsTypedFailure:
    async def test_missing_implementation_field(self, tmp_path):
        # The exact live Test-7 shape: descriptor with NO implementation key.
        from kernos.kernel.workspace import WorkspaceManager
        from kernos.kernel.tool_catalog import ToolCatalog
        ws = WorkspaceManager(data_dir=str(tmp_path), catalog=ToolCatalog())
        space_dir = tmp_path / "t1" / "spaces" / "sp1" / "files"
        space_dir.mkdir(parents=True)
        (space_dir / "coin.tool.json").write_text(json.dumps({
            "name": "coin", "description": "flip",
        }))
        res = await ws.register_tool("t1", "sp1", "coin.tool.json")
        assert isinstance(res, ToolFailure)
        assert res.code == "descriptor_invalid"
        assert res.pre_side_effect is True
        assert "implementation" in res

    async def test_missing_descriptor_file(self, tmp_path):
        from kernos.kernel.workspace import WorkspaceManager
        from kernos.kernel.tool_catalog import ToolCatalog
        ws = WorkspaceManager(data_dir=str(tmp_path), catalog=ToolCatalog())
        res = await ws.register_tool("t1", "sp1", "ghost.tool.json")
        assert isinstance(res, ToolFailure)
        assert res.pre_side_effect is True


class TestConsultReturnsTypedFailure:
    async def test_invalid_consult_call_is_typed(self):
        # A still-hard-failing shape: harness names a KNOWN other agent
        # (denylist — never silently rerouted). InvalidConsultCall is raised
        # BEFORE orchestrator.consult → pre_side_effect=True, and crucially
        # the real ExternalAgentService is never started. (Label-shaped
        # harnesses like "s16" now RECOVER to codex by design — covered in
        # test_acpx_adapter — so they no longer exercise this failure path.)
        from kernos.kernel.reasoning import ReasoningService
        svc = ReasoningService(AsyncMock(), AsyncMock(), MagicMock(), AsyncMock())
        request = MagicMock()
        request.instance_id = "inst-x"
        res = await svc.execute_tool(
            "consult",
            {"harness": "aider", "question": "what is daily mode for?"},
            request,
        )
        assert isinstance(res, ToolFailure)
        assert res.code == "invalid_consult_call"
        assert res.pre_side_effect is True
        # The agent-visible text is unchanged: the same JSON error payload.
        payload = json.loads(res)
        assert payload["error"] == "InvalidConsultCall"


# ---------------------------------------------------------------------------
# 3. Dispatch boundaries record the failure
# ---------------------------------------------------------------------------


class TestLiveExecutorMapsToolFailure:
    async def test_tool_failure_becomes_is_error_true(self):
        failure = ToolFailure("Error: 'description' is required",
                              code="schedule_underspecified",
                              pre_side_effect=True)
        executor = LiveExecutor(
            execute_tool=AsyncMock(return_value=failure),
            gate=_gate("soft_write"),
            request_factory=lambda inputs: MagicMock(),
        )
        result = await executor.execute(_inputs())
        assert result.is_error is True
        assert "description" in result.output["error"]
        assert result.corrective_signal  # agent gets the failure text back

    async def test_plain_string_success_still_succeeds(self):
        executor = LiveExecutor(
            execute_tool=AsyncMock(return_value="Scheduled: x"),
            gate=_gate("soft_write"),
            request_factory=lambda inputs: MagicMock(),
        )
        result = await executor.execute(_inputs())
        assert result.is_error is False
        assert result.output == {"text": "Scheduled: x"}


class TestLiveIntegrationDispatcherMapsToolFailure:
    async def test_tool_failure_emits_error_event_and_audit(self):
        failure = ToolFailure("InvalidConsultCall ...",
                              code="invalid_consult_call",
                              pre_side_effect=True)
        events: list[dict] = []

        async def _emit(payload: dict) -> None:
            events.append(payload)

        dispatcher = LiveIntegrationDispatcher(
            execute_tool=AsyncMock(return_value=failure),
            gate=_gate("read"),
            request_factory=lambda tool_id, args, inputs: MagicMock(),
            event_emitter=_emit,
        )
        inputs = MagicMock()
        inputs.instance_id = "inst-x"
        inputs.member_id = "mem-x"
        inputs.space_id = "space-x"
        result = await dispatcher("consult", {"harness": "bad"}, inputs)
        assert result["is_error"] is True
        results = [e for e in events if e.get("type") == "tool.result"]
        assert results and results[-1]["is_error"] is True
        assert results[-1]["failure_code"] == "invalid_consult_call"
        assert results[-1]["pre_side_effect"] is True


# ---------------------------------------------------------------------------
# 4. The plan does NOT advance over a returned failure
# ---------------------------------------------------------------------------


class TestStepDoesNotCompleteOverFailure:
    async def test_step_dispatch_completed_false(self):
        # End to end: real LiveExecutor wired to a tool returning ToolFailure,
        # behind the real StepDispatcher — the step must NOT be completed.
        from kernos.kernel.enactment.plan import Step, StepExpectation
        from kernos.kernel.tool_descriptor import (
            ToolDescriptor as ExtToolDescriptor,
        )

        failure = ToolFailure("I couldn't determine when to schedule that.",
                              code="schedule_underspecified",
                              pre_side_effect=True)
        executor = LiveExecutor(
            execute_tool=AsyncMock(return_value=failure),
            gate=_gate("soft_write"),
            request_factory=lambda inputs: MagicMock(),
        )

        descriptor = ExtToolDescriptor(
            name="manage_schedule",
            description="d",
            input_schema={"type": "object"},
            implementation="x.py",
        )

        class _Lookup:
            def descriptor_for(self, tool_id):
                return descriptor

        dispatcher = StepDispatcher(executor=executor, descriptor_lookup=_Lookup())
        step = Step(
            step_id="s6",
            tool_id="manage_schedule",
            arguments={"action": "create"},
            tool_class="kernel",
            operation_name="manage_schedule",
            expectation=StepExpectation(prose="create the reminder"),
        )
        briefing = MagicMock()
        briefing.turn_id = "turn-x"
        briefing.integration_run_id = "run-x"
        result = await dispatcher.dispatch(StepDispatchInputs(step=step, briefing=briefing))
        assert result.completed is False
        assert result.corrective_signal  # failure text flows to the corrective loop
