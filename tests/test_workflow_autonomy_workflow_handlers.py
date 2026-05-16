"""Spec 6 commit 5: parallel workflow handlers + WorkflowLedger wiring tests.

Pins:
  * ``handle_ask_coding_session_for_workflow`` — workflow-facing
    wrapper over the bridge's existing ask handler.
  * ``handle_read_coding_session_response_for_workflow`` — workflow-
    facing wrapper over the bridge's existing read handler.
  * WorkflowLedger production-wiring adapters
    (``_workflow_ledger_append_adapter`` /
    ``_workflow_ledger_read_last_adapter``) that bridge
    AppendToLedgerAction's kwargs shape to WorkflowLedger's positional
    signature.

Test shape per architect user-feedback: every mechanic has BOTH a
unit pin AND a functional pin where the mechanic is exercised under
its expected workflow-side use and the expected outcome is asserted.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kernos.kernel import event_stream
from kernos.kernel.workflows.autonomy_tools import (
    handle_ask_coding_session_for_workflow,
    handle_read_coding_session_response_for_workflow,
)
from kernos.kernel.workflows.ledger import WorkflowLedger
from kernos.setup.bring_up_substrate import (
    _workflow_ledger_append_adapter,
    _workflow_ledger_read_last_adapter,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
async def event_stream_writer(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path), flush_interval_s=0.05)
    yield tmp_path
    await event_stream.stop_writer()
    await event_stream._reset_for_tests()


# ===========================================================================
# handle_ask_coding_session_for_workflow
# ===========================================================================


class TestAskCodingSessionForWorkflow:
    """Workflow-facing wrapper over the bridge's ask handler. Adapts
    member-facing arg shape to the workflow's call_tool args dict."""

    async def test_writes_request_file(self, event_stream_writer):
        """Unit pin: workflow handler writes the request file via the
        bridge's existing handler. Substrate state pin: the request
        file lands in the canonical bridge location."""
        result = await handle_ask_coding_session_for_workflow(
            instance_id="inst_a",
            member_id="system",
            args={
                "target": "claude_code",
                "question": "fix the integration timeout pattern",
                "context": {"pattern_id": "int-timeout-1"},
            },
            data_dir=str(event_stream_writer),
        )
        assert result["success"] is True
        assert result["execution_state"] == "attempted"
        assert result["request_id"]  # non-empty request_id
        # Substrate state pin: request file exists at the canonical path.
        requests_dir = (
            Path(event_stream_writer) / "inst_a"
            / "coding_session_bridge" / "requests"
        )
        request_files = list(requests_dir.glob("*.json"))
        assert len(request_files) == 1
        with request_files[0].open() as fp:
            request_data = json.load(fp)
        assert request_data["target"] == "claude_code"
        assert request_data["question"] == "fix the integration timeout pattern"
        assert request_data["context"]["pattern_id"] == "int-timeout-1"

    async def test_invalid_target_returns_failed_state(
        self, event_stream_writer,
    ):
        """Unit pin: failed validation flows through to a workflow-
        friendly result with success=False + failed execution_state."""
        result = await handle_ask_coding_session_for_workflow(
            instance_id="inst_a",
            member_id="system",
            args={
                "target": "nonexistent-target",
                "question": "hello",
            },
            data_dir=str(event_stream_writer),
        )
        assert result["success"] is False
        assert result["execution_state"] == "failed"
        assert result["request_id"] == ""

    async def test_functional_workflow_can_use_request_id_for_subsequent_read(
        self, event_stream_writer,
    ):
        """FUNCTIONAL pin (architect's user-feedback request): full
        workflow round-trip — ask returns request_id, write response
        externally, read returns completed with the response payload.
        Pins the canonical two-step pattern the self_improvement
        workflow's action_sequence uses."""
        # Step 1: ask (writes request file).
        ask_result = await handle_ask_coding_session_for_workflow(
            instance_id="inst_a",
            member_id="system",
            args={
                "target": "claude_code",
                "question": "investigate pattern xyz",
            },
            data_dir=str(event_stream_writer),
        )
        assert ask_result["success"] is True
        request_id = ask_result["request_id"]
        # Step 2: external tooling (simulated) writes response file.
        response_dir = (
            Path(event_stream_writer) / "inst_a"
            / "coding_session_bridge" / "responses"
        )
        response_dir.mkdir(parents=True, exist_ok=True)
        with (response_dir / f"{request_id}.json").open("w") as fp:
            json.dump({
                "request_id": request_id,
                "target": "claude_code",
                "investigation_outcome": "completed",
                "summary": "fixed via commit abc123",
            }, fp)
        # Step 3: workflow's next step calls read.
        read_result = await handle_read_coding_session_response_for_workflow(
            instance_id="inst_a",
            member_id="system",
            args={"request_id": request_id},
            data_dir=str(event_stream_writer),
        )
        assert read_result["success"] is True
        assert read_result["execution_state"] == "completed"
        assert read_result["request_id"] == request_id


# ===========================================================================
# handle_read_coding_session_response_for_workflow
# ===========================================================================


class TestReadCodingSessionResponseForWorkflow:

    async def test_no_request_returns_failed(self, event_stream_writer):
        """Unit pin: unknown request_id → workflow gets failed state
        so its branch logic can route to abort / retry / fallback."""
        result = await handle_read_coding_session_response_for_workflow(
            instance_id="inst_a",
            member_id="system",
            args={"request_id": "non_existent_request"},
            data_dir=str(event_stream_writer),
        )
        assert result["success"] is False
        assert result["execution_state"] == "failed"

    async def test_pending_request_returns_attempted(
        self, event_stream_writer,
    ):
        """Unit pin: request exists, no response yet, within timeout —
        attempted state (workflow continues / re-polls)."""
        # Write a request file directly.
        requests_dir = (
            Path(event_stream_writer) / "inst_a"
            / "coding_session_bridge" / "requests"
        )
        requests_dir.mkdir(parents=True, exist_ok=True)
        request_id = "req_pending"
        with (requests_dir / f"{request_id}.json").open("w") as fp:
            json.dump({
                "request_id": request_id,
                "timestamp": _now(),
                "target": "claude_code",
                "originating_kernos_instance": "inst_a",
                "originating_space": "",
                "originating_member_id": "system",
                "question": "hello",
            }, fp)
        result = await handle_read_coding_session_response_for_workflow(
            instance_id="inst_a",
            member_id="system",
            args={"request_id": request_id},
            data_dir=str(event_stream_writer),
        )
        # Pending — success-but-not-complete shape.
        assert result["success"] is True
        assert result["execution_state"] == "attempted"


# ===========================================================================
# WorkflowLedger production-wiring adapters
# ===========================================================================


class TestWorkflowLedgerAdapters:
    """Spec 6 commit 5: AppendToLedgerAction kwargs shape ↔
    WorkflowLedger positional signature."""

    async def test_append_adapter_writes_via_ledger(self, tmp_path):
        """Unit pin: adapter bridges kwargs → positional and the
        underlying WorkflowLedger writes the entry."""
        ledger = WorkflowLedger(str(tmp_path))
        adapter = _workflow_ledger_append_adapter(ledger)
        await adapter(
            workflow_id="wf-1",
            entry={"step": "test", "value": 42},
            instance_id="inst_a",
        )
        # Substrate state pin: the entry lands on the underlying
        # ledger file at the canonical path.
        entries = await ledger.read_all("inst_a", "wf-1")
        assert len(entries) == 1
        assert entries[0]["step"] == "test"
        assert entries[0]["value"] == 42

    async def test_read_last_adapter_reads_via_ledger(self, tmp_path):
        """Unit pin: companion adapter for the verifier path."""
        ledger = WorkflowLedger(str(tmp_path))
        await ledger.append("inst_a", "wf-1", {"step": "first"})
        await ledger.append("inst_a", "wf-1", {"step": "second"})
        adapter = _workflow_ledger_read_last_adapter(ledger)
        last = await adapter(workflow_id="wf-1", instance_id="inst_a")
        assert last is not None
        assert last["step"] == "second"

    async def test_functional_append_then_verify_roundtrip(
        self, tmp_path,
    ):
        """FUNCTIONAL pin (architect's user-feedback request): exercise
        the AppendToLedgerAction's full execute → verify roundtrip
        with the production-wired adapters. Mimics what the workflow's
        engine does when it runs an append_to_ledger action — append
        the entry, then verify by re-reading the last entry.
        Substrate state pin: the written entry's user-supplied fields
        all survive the round-trip (logged_at is added by the writer
        but doesn't fail the verifier check)."""
        from kernos.kernel.workflows.action_library import (
            AppendToLedgerAction,
        )

        ledger = WorkflowLedger(str(tmp_path))
        action = AppendToLedgerAction(
            ledger_append_fn=_workflow_ledger_append_adapter(ledger),
            ledger_read_last_fn=_workflow_ledger_read_last_adapter(ledger),
        )

        class _Ctx:
            instance_id = "inst_a"

        entry = {
            "step": "autonomy_loop_complete",
            "workflow_id": "self_improvement",
            "pattern_id": "int-timeout-1",
            "outcome": "completed",
        }
        params = {"workflow_id": "self_improvement", "entry": entry}
        result = await action.execute(_Ctx(), params)
        assert result.success is True
        verified = await action.verify(_Ctx(), params, result)
        assert verified is True
        # Substrate state pin: read-all returns the entry with all
        # caller-supplied fields preserved.
        entries = await ledger.read_all("inst_a", "self_improvement")
        assert len(entries) == 1
        assert entries[0]["step"] == "autonomy_loop_complete"
        assert entries[0]["pattern_id"] == "int-timeout-1"
        assert entries[0]["outcome"] == "completed"
        # logged_at is writer-injected.
        assert "logged_at" in entries[0]


# ===========================================================================
# Module-level invariants
# ===========================================================================


class TestModuleInvariants:
    def test_workflow_handlers_in_autonomy_tools_all(self):
        """Pure-API probe: the new for_workflow handlers are exported
        from autonomy_tools.__all__ so the production wiring (commit 6)
        can import them cleanly."""
        from kernos.kernel.workflows import autonomy_tools

        assert "handle_ask_coding_session_for_workflow" in autonomy_tools.__all__
        assert "handle_read_coding_session_response_for_workflow" in autonomy_tools.__all__
