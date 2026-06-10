"""IMPROVEMENT-LOOP-RECOVERY-V1 focused tests."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from kernos.kernel import approval_receipts as _approvals
from kernos.kernel import git_operations as gop
from kernos.kernel import improvement_ledger as _ledger
from kernos.kernel import improvement_loop_workflow as iwf
from kernos.kernel.gate import DispatchGate
from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.reasoning import ReasoningRequest, ReasoningService
from kernos.messages.handler import MessageHandler
from kernos.messages.handler import TurnContext
from kernos.messages.models import NormalizedMessage


@pytest.fixture
async def recovery_env(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = InstanceDB(str(data_dir))
    await db.connect()
    await db.close()
    await _approvals.ensure_schema(str(data_dir))
    return str(data_dir)


def test_recovery_tools_registered_and_not_always_pinned():
    from unittest.mock import AsyncMock, MagicMock
    from kernos.kernel.kernel_tool_registry import kernel_tool_schema_map
    from kernos.kernel.reasoning import ReasoningService
    from kernos.kernel.tool_catalog import ALWAYS_PINNED

    names = {"proceed_with_recovery", "abandon_attempt"}
    schema_map = kernel_tool_schema_map()
    assert names <= set(schema_map)
    assert names <= ReasoningService._KERNEL_TOOLS
    assert names <= ReasoningService._DISPATCHABLE_KERNEL_TOOLS
    assert not (names & ALWAYS_PINNED)

    gate = DispatchGate(
        reasoning_service=MagicMock(),
        registry=None, state=AsyncMock(), events=AsyncMock(),
    )
    assert gate.classify_tool_effect(
        "proceed_with_recovery", None, {},
    ) == "soft_write"
    assert gate.classify_tool_effect(
        "abandon_attempt", None, {},
    ) == "soft_write"


def test_handler_constructor_wires_improvement_loop_seams():
    source = inspect.getsource(MessageHandler.__init__)
    assert "_consult_fn_for_loop" in source
    assert "orchestrator.consult" in source
    assert "_restart_fn_for_loop" in source
    assert "handle_restart_self_tool" in source


async def test_real_handler_reasoning_wiring_drives_improve_and_recovery(
    tmp_path, monkeypatch,
):
    from types import SimpleNamespace
    from tests.test_handler import _make_handler
    from kernos.kernel.improvement_workspace import ImprovementWorkspace

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("KERNOS_DATA_DIR", str(data_dir))
    await _approvals.ensure_schema(str(data_dir))
    db = InstanceDB(str(data_dir))
    await db.connect()
    await db.close()

    handler, _provider = _make_handler()
    reasoning = handler.reasoning
    assert isinstance(reasoning, ReasoningService)
    assert getattr(handler, "_consult_fn_for_loop", None) is not None
    assert getattr(handler, "_restart_fn_for_loop", None) is not None

    consult_calls = []

    class _FakeExternalOrchestrator:
        async def consult(self, **kwargs):
            consult_calls.append(kwargs)
            return SimpleNamespace(
                response="patched and reviewed\n\nSTATUS: GREEN",
                harness=kwargs["harness"],
                session_id="",
                truncated=False,
                metadata={},
            )

    async def fake_get_service(*, data_dir=None):
        return SimpleNamespace(orchestrator=_FakeExternalOrchestrator())

    monkeypatch.setattr(
        "kernos.kernel.external_agents.tool.get_service",
        fake_get_service,
    )

    async def fake_create(self, attempt_id):
        path = Path(self.path_for(attempt_id))
        path.mkdir(parents=True, exist_ok=False)
        return str(path)

    monkeypatch.setattr(ImprovementWorkspace, "create", fake_create)
    monkeypatch.setattr(
        iwf, "_request_commit_approval_for_attempt", _fake_request_approval,
    )
    monkeypatch.setattr(iwf, "_staged_files", lambda _path: _async(["README.md"]))

    async def fake_commit(*, tool_input, instance_id, data_dir):
        await _approvals.set_outcome_field(
            data_dir=data_dir,
            approval_id=tool_input["approval_id"],
            field="commit_sha",
            value="abc123def456",
        )
        return "Committed `abc123def456` in the worktree."

    async def fake_push(*, tool_input, instance_id, data_dir):
        return {
            "ok": True,
            "message": "Pushed `abc123def456` to `origin/main`.",
            "commit_sha": "abc123def456",
            "origin_confirmed": True,
        }

    async def fake_origin_head_matches_commit(**kwargs):
        return True, {"origin_sha": kwargs["commit_sha"]}

    restarts = []

    def fake_restart_tool(**kwargs):
        restarts.append(kwargs)
        return "restart stubbed"

    monkeypatch.setattr(
        "kernos.kernel.git_operations.handle_git_commit", fake_commit,
    )
    monkeypatch.setattr(
        "kernos.kernel.git_operations.handle_git_push", fake_push,
    )
    monkeypatch.setattr(
        iwf, "_origin_head_matches_commit", fake_origin_head_matches_commit,
    )
    monkeypatch.setattr(
        "kernos.kernel.self_admin_tools.handle_restart_self_tool",
        fake_restart_tool,
    )

    request = ReasoningRequest(
        instance_id="t1",
        conversation_id="conv",
        system_prompt="",
        messages=[],
        tools=[],
        model="test",
        trigger="test",
        active_space_id="space_origin",
        member_id="owner",
    )
    start_text = await reasoning.execute_tool(
        "improve_kernos",
        {"spec_requirement": "prove production wiring"},
        request,
    )
    attempt_id = re.search(r"`(att_[^`]+)`", start_text).group(1)

    for _ in range(20):
        events = await _events(str(data_dir), attempt_id)
        if any(e["kind"] == "approval_requested" for e in events):
            break
        await asyncio.sleep(0.01)
    approval_id = await _latest_approval_id(str(data_dir), attempt_id)
    assert len(consult_calls) >= 4
    assert {call["instance_id"] for call in consult_calls} == {"t1"}

    approve_text = await handler._handle_approve_command(
        TurnContext(instance_id="t1", member_id="owner"),
        f"/approve {approval_id} CONFIRM",
    )
    assert "awaiting post-restart tests" in approve_text
    assert restarts

    async def failing_self_test(*, tool_input, instance_id, data_dir):
        return {
            "test_outcome": "fail",
            "summary": "Self-test failed. Failing: tests/test_demo.py.",
        }

    processed = await iwf.run_pending_post_restart_tests(
        data_dir=str(data_dir),
        instance_id="t1",
        self_test_fn=failing_self_test,
        live_head_fn=_matching_live_head("abc123def456"),
    )
    assert processed == 1
    assert (await _attempt(str(data_dir), attempt_id))["final_state"] == (
        "awaiting_recovery_decision"
    )

    recovery_text = await reasoning.execute_tool(
        "proceed_with_recovery",
        {"attempt_id": attempt_id},
        request,
    )
    assert "requested operator commit approval" in recovery_text
    row = await _attempt(str(data_dir), attempt_id)
    assert row["final_state"] == "awaiting_recovery_commit_approval"
    assert len(consult_calls) >= 5


async def _create_attempt(
    data_dir: str,
    *,
    attempt_id: str = "att_recovery",
    instance_id: str = "t1",
    final_state: str = "awaiting_post_restart_test",
    worktree_path: str = "/tmp/recovery-worktree",
    origin_space_id: str = "space_origin",
    origin_member_id: str = "mem_origin",
) -> None:
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        await _ledger.create_attempt(
            db._conn,
            instance_id=instance_id,
            attempt_id=attempt_id,
            spec_requirement="demo requirement",
            primary_coding_agent="codex",
            reviewer_coding_agent="claude_code",
        )
        await _ledger.update_attempt(
            db._conn,
            attempt_id=attempt_id,
            worktree_path=worktree_path,
            final_state=final_state,
        )
        await _ledger.append_event(
            db._conn,
            attempt_id=attempt_id,
            kind="attempt_origin",
            detail=json.dumps({
                "instance_id": instance_id,
                "origin_space_id": origin_space_id,
                "origin_member_id": origin_member_id,
            }),
        )
    finally:
        await db.close()


async def _attempt(data_dir: str, attempt_id: str = "att_recovery") -> dict:
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        return await _ledger.get_attempt(db._conn, attempt_id)
    finally:
        await db.close()


async def _events(data_dir: str, attempt_id: str = "att_recovery") -> list[dict]:
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        return await _ledger.get_attempt_events(db._conn, attempt_id)
    finally:
        await db.close()


async def _commits(data_dir: str, attempt_id: str = "att_recovery") -> list[dict]:
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        return await _ledger.get_attempt_commits(db._conn, attempt_id)
    finally:
        await db.close()


async def _latest_approval_id(data_dir: str, attempt_id: str) -> str:
    events = await _events(data_dir, attempt_id)
    for event in reversed(events):
        if event["kind"] == "approval_requested":
            return event["detail"].split("approval_id=", 1)[1].split()[0]
    raise AssertionError("approval_requested event not found")


async def _fake_request_approval(**kwargs) -> str:
    recovery_iteration = kwargs.get("recovery_iteration")
    binding = {
        "kind": "git_commit_authorization",
        "attempt_id": kwargs["attempt_id"],
        "workspace_dir": kwargs["worktree_path"],
        "expected_parent_sha": "parent_sha",
        "expected_diff_hash": "sha256:test",
        "target_branch": "main",
        "summary": f"recovery {recovery_iteration}",
    }
    if recovery_iteration is not None:
        binding["recovery_iteration"] = recovery_iteration
    approval_id = await _approvals.request_approval(
        data_dir=kwargs["data_dir"],
        instance_id=kwargs["instance_id"],
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="recovery approval",
        binding_payload=binding,
        event_stream=None,
    )
    detail = f"approval_id={approval_id}"
    if recovery_iteration is not None:
        detail += f" recovery_iteration={recovery_iteration}"
    await _ledger.append_event(
        kwargs["db"]._conn,
        attempt_id=kwargs["attempt_id"],
        kind="approval_requested",
        detail=detail,
    )
    return approval_id


async def test_approval_continuation_records_commit_push_and_restart(
    recovery_env, monkeypatch,
):
    data_dir = recovery_env
    await _create_attempt(data_dir, final_state="awaiting_commit_approval")
    approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="commit it",
        binding_payload={
            "kind": "git_commit_authorization",
            "attempt_id": "att_recovery",
            "workspace_dir": "/tmp/recovery-worktree",
            "expected_parent_sha": "parent_sha",
            "expected_diff_hash": "sha256:test",
            "target_branch": "main",
            "summary": "demo commit",
        },
        event_stream=None,
    )
    ok, _ = await _approvals.approve(
        data_dir=data_dir,
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    assert ok
    monkeypatch.setattr(iwf, "_staged_files", lambda _path: _async(["README.md"]))

    async def fake_commit(*, tool_input, instance_id, data_dir):
        await _approvals.set_outcome_field(
            data_dir=data_dir,
            approval_id=tool_input["approval_id"],
            field="commit_sha",
            value="abc123def456",
        )
        return "Committed `abc123def456` in the worktree."

    async def fake_push(*, tool_input, instance_id, data_dir):
        return {
            "ok": True,
            "message": "Pushed `abc123def456` to `origin/main`.",
            "commit_sha": "abc123def456",
        }

    async def fake_origin_head_matches_commit(**kwargs):
        assert kwargs["commit_sha"] == "abc123def456"
        return True, {
            "origin_sha": "abc123def456",
            "fetch_result": "Fetched origin.",
        }

    monkeypatch.setattr(
        iwf, "_origin_head_matches_commit", fake_origin_head_matches_commit,
    )
    restarted = []

    async def restart_fn():
        restarted.append(True)

    text = await iwf.continue_approved_improvement_commit(
        data_dir=data_dir,
        instance_id="t1",
        approval_id=approval_id,
        restart_fn=restart_fn,
        git_commit_fn=fake_commit,
        git_push_fn=fake_push,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    commits = await _commits(data_dir)
    assert "awaiting post-restart tests" in text
    assert row["final_state"] == "awaiting_post_restart_test"
    assert restarted == [True]
    assert [e["kind"] for e in events][-2:] == [
        "commit_recorded", "push_succeeded",
    ]
    assert commits[0]["commit_sha"] == "abc123def456"


async def test_approval_continuation_missing_restart_seam_fails_terminal(
    recovery_env,
):
    data_dir = recovery_env
    await _create_attempt(data_dir, final_state="awaiting_commit_approval")
    approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="commit it",
        binding_payload={
            "kind": "git_commit_authorization",
            "attempt_id": "att_recovery",
            "workspace_dir": "/tmp/recovery-worktree",
            "expected_parent_sha": "parent_sha",
            "expected_diff_hash": "sha256:test",
            "target_branch": "main",
            "summary": "demo commit",
        },
        event_stream=None,
    )
    ok, _ = await _approvals.approve(
        data_dir=data_dir,
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    assert ok
    called = []

    async def fake_commit(*, tool_input, instance_id, data_dir):
        called.append("commit")
        return "Committed `abc123def456` in the worktree."

    async def fake_push(*, tool_input, instance_id, data_dir):
        called.append("push")
        return {"ok": True, "commit_sha": "abc123def456"}

    text = await iwf.continue_approved_improvement_commit(
        data_dir=data_dir,
        instance_id="t1",
        approval_id=approval_id,
        restart_fn=None,
        git_commit_fn=fake_commit,
        git_push_fn=fake_push,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    assert "restart seam is unavailable" in text
    assert row["final_state"] == "attempt_failed"
    assert called == []
    assert not await _commits(data_dir)
    assert any(
        e["kind"] == "attempt_failed"
        and "restart_seam_unavailable" in e["detail"]
        for e in events
    )


async def test_approval_continuation_structured_unconfirmed_push_does_not_restart(
    recovery_env, monkeypatch,
):
    data_dir = recovery_env
    await _create_attempt(data_dir, final_state="awaiting_commit_approval")
    approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="commit it",
        binding_payload={
            "kind": "git_commit_authorization",
            "attempt_id": "att_recovery",
            "workspace_dir": "/tmp/recovery-worktree",
            "expected_parent_sha": "parent_sha",
            "expected_diff_hash": "sha256:test",
            "target_branch": "main",
            "summary": "demo commit",
        },
        event_stream=None,
    )
    ok, _ = await _approvals.approve(
        data_dir=data_dir,
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    assert ok
    monkeypatch.setattr(iwf, "_staged_files", lambda _path: _async(["README.md"]))

    async def fake_commit(*, tool_input, instance_id, data_dir):
        await _approvals.set_outcome_field(
            data_dir=data_dir,
            approval_id=tool_input["approval_id"],
            field="commit_sha",
            value="abc123def456",
        )
        return "Committed `abc123def456` in the worktree."

    async def fake_push(*, tool_input, instance_id, data_dir):
        return {
            "ok": True,
            "message": "Pushed `abc123def456` to `origin/main`.",
            "commit_sha": "abc123def456",
        }

    async def fake_origin_head_matches_commit(**kwargs):
        assert kwargs["commit_sha"] == "abc123def456"
        return False, {"origin_sha": "other_sha", "fetch_result": "Fetched origin."}

    monkeypatch.setattr(
        iwf, "_origin_head_matches_commit", fake_origin_head_matches_commit,
    )
    restarted = []

    text = await iwf.continue_approved_improvement_commit(
        data_dir=data_dir,
        instance_id="t1",
        approval_id=approval_id,
        restart_fn=lambda: restarted.append(True),
        git_commit_fn=fake_commit,
        git_push_fn=fake_push,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    kinds = [e["kind"] for e in events]
    assert "push did not confirm" in text
    assert row["final_state"] == "attempt_failed"
    assert restarted == []
    assert "push_failed" in kinds
    assert "push_succeeded" not in kinds
    assert "commit_recorded" not in kinds
    assert not await _commits(data_dir)
    assert any("push_unconfirmed" in e["detail"] for e in events)


async def test_post_push_confirmation_failure_retries_until_origin_confirms(
    recovery_env, monkeypatch,
):
    data_dir = recovery_env
    await _create_attempt(data_dir, final_state="awaiting_commit_approval")
    approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="commit it",
        binding_payload={
            "kind": "git_commit_authorization",
            "attempt_id": "att_recovery",
            "workspace_dir": "/tmp/recovery-worktree",
            "expected_parent_sha": "parent_sha",
            "expected_diff_hash": "sha256:test",
            "target_branch": "main",
            "summary": "demo commit",
        },
        event_stream=None,
    )
    ok, _ = await _approvals.approve(
        data_dir=data_dir,
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    assert ok
    monkeypatch.setattr(iwf, "_staged_files", lambda _path: _async(["README.md"]))
    calls = {"commit": 0, "push": 0, "restart": 0}

    async def fake_commit(*, tool_input, instance_id, data_dir):
        calls["commit"] += 1
        await _approvals.set_outcome_field(
            data_dir=data_dir,
            approval_id=tool_input["approval_id"],
            field="commit_sha",
            value="abc123def456",
        )
        return "Committed `abc123def456` in the worktree."

    async def fake_push(*, tool_input, instance_id, data_dir):
        calls["push"] += 1
        return {
            "ok": False,
            "reason": "post_push_fetch_failed",
            "message": (
                "`git push` exited 0, but post-push confirmation failed "
                "during fetch: network down."
            ),
            "commit_sha": "abc123def456",
            "origin_confirmed": False,
        }

    origin_confirmations = iter([
        (
            False,
            {
                "origin_confirmed": False,
                "origin_confirmation_reason": "fetch_failed",
                "fetch_result": "network down",
            },
        ),
        (
            True,
            {
                "origin_sha": "abc123def456",
                "fetch_result": "Fetched origin.",
            },
        ),
    ])

    async def fake_origin_head_matches_commit(**kwargs):
        assert kwargs["commit_sha"] == "abc123def456"
        return next(origin_confirmations)

    monkeypatch.setattr(
        iwf, "_origin_head_matches_commit", fake_origin_head_matches_commit,
    )

    text = await iwf.continue_approved_improvement_commit(
        data_dir=data_dir,
        instance_id="t1",
        approval_id=approval_id,
        restart_fn=lambda: calls.__setitem__("restart", calls["restart"] + 1),
        git_commit_fn=fake_commit,
        git_push_fn=fake_push,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    commits = await _commits(data_dir)
    kinds = [e["kind"] for e in events]
    assert "confirmation is still unavailable" in text
    assert row["final_state"] == "push_unconfirmed"
    assert calls == {"commit": 1, "push": 1, "restart": 0}
    assert commits[0]["commit_sha"] == "abc123def456"
    assert "commit_recorded" in kinds
    assert "push_unconfirmed" in kinds
    assert "push_failed" not in kinds
    assert "push_succeeded" not in kinds

    async def fail_push(**_kwargs):
        raise AssertionError("confirmed retryable push must not push again")

    processed = await iwf.process_approved_improvement_commit_continuations(
        data_dir=data_dir,
        instance_id="t1",
        restart_fn=lambda: calls.__setitem__("restart", calls["restart"] + 1),
        git_push_fn=fail_push,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    kinds = [e["kind"] for e in events]
    assert processed == 1
    assert row["final_state"] == "awaiting_post_restart_test"
    assert calls == {"commit": 1, "push": 1, "restart": 1}
    assert kinds.count("commit_recorded") == 1
    assert "push_succeeded" in kinds
    assert "push_failed" not in kinds


async def test_push_unconfirmed_sweep_ignores_stale_initial_receipt_for_recovery(
    recovery_env, monkeypatch,
):
    data_dir = recovery_env
    await _create_attempt(data_dir, final_state="awaiting_commit_approval")
    initial_approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="commit it",
        binding_payload={
            "kind": "git_commit_authorization",
            "attempt_id": "att_recovery",
            "workspace_dir": "/tmp/recovery-worktree",
            "expected_parent_sha": "parent_sha",
            "expected_diff_hash": "sha256:test",
            "target_branch": "main",
            "summary": "demo commit",
        },
        event_stream=None,
    )
    ok, _ = await _approvals.approve(
        data_dir=data_dir,
        approval_id=initial_approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    assert ok

    monkeypatch.setattr(iwf, "_staged_files", lambda _path: _async(["README.md"]))
    shas = {
        initial_approval_id: "initialabc123",
    }
    calls = {"commit": [], "push": [], "restart": 0}
    recovery_origin_calls = 0
    recovery_push_calls = 0

    async def fake_commit(*, tool_input, instance_id, data_dir):
        approval_id = tool_input["approval_id"]
        calls["commit"].append(approval_id)
        commit_sha = shas[approval_id]
        await _approvals.set_outcome_field(
            data_dir=data_dir,
            approval_id=approval_id,
            field="commit_sha",
            value=commit_sha,
        )
        return f"Committed `{commit_sha}` in the worktree."

    async def fake_push(*, tool_input, instance_id, data_dir):
        nonlocal recovery_push_calls
        approval_id = tool_input["approval_id"]
        calls["push"].append(approval_id)
        commit_sha = shas[approval_id]
        if commit_sha == "initialabc123":
            return {
                "ok": True,
                "message": "Pushed `initialabc123` to `origin/main`.",
                "commit_sha": commit_sha,
                "origin_confirmed": True,
            }
        recovery_push_calls += 1
        if recovery_push_calls == 1:
            return {
                "ok": False,
                "reason": "post_push_fetch_failed",
                "message": "push succeeded but fetch confirmation failed",
                "commit_sha": commit_sha,
                "origin_confirmed": False,
            }
        return {
            "ok": True,
            "message": "Pushed `recoveryabc123` to `origin/main`.",
            "commit_sha": commit_sha,
            "origin_confirmed": True,
        }

    async def fake_origin_head_matches_commit(**kwargs):
        nonlocal recovery_origin_calls
        commit_sha = kwargs["commit_sha"]
        if commit_sha == "initialabc123":
            return True, {"origin_sha": commit_sha}
        assert commit_sha == "recoveryabc123"
        recovery_origin_calls += 1
        if recovery_origin_calls == 1:
            return False, {
                "origin_confirmed": False,
                "origin_confirmation_reason": "fetch_failed",
            }
        if recovery_origin_calls == 2:
            return False, {"origin_sha": "initialabc123"}
        return True, {"origin_sha": commit_sha}

    monkeypatch.setattr(
        iwf, "_origin_head_matches_commit", fake_origin_head_matches_commit,
    )

    await iwf.continue_approved_improvement_commit(
        data_dir=data_dir,
        instance_id="t1",
        approval_id=initial_approval_id,
        restart_fn=lambda: calls.__setitem__("restart", calls["restart"] + 1),
        git_commit_fn=fake_commit,
        git_push_fn=fake_push,
    )
    assert (await _attempt(data_dir))["final_state"] == (
        "awaiting_post_restart_test"
    )

    db = InstanceDB(data_dir)
    await db.connect()
    try:
        await _ledger.append_event(
            db._conn,
            attempt_id="att_recovery",
            kind="recovery_started",
            detail=json.dumps({
                "iteration": 1,
                "trigger": "post_restart_self_test_failed",
            }),
        )
        await _ledger.update_attempt(
            db._conn,
            attempt_id="att_recovery",
            final_state="awaiting_recovery_commit_approval",
        )
    finally:
        await db.close()

    recovery_approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="recovery approval",
        binding_payload={
            "kind": "git_commit_authorization",
            "attempt_id": "att_recovery",
            "workspace_dir": "/tmp/recovery-worktree",
            "expected_parent_sha": "parent_sha",
            "expected_diff_hash": "sha256:test",
            "target_branch": "main",
            "summary": "recovery 1",
            "recovery_iteration": 1,
        },
        event_stream=None,
    )
    shas[recovery_approval_id] = "recoveryabc123"
    ok, _ = await _approvals.approve(
        data_dir=data_dir,
        approval_id=recovery_approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    assert ok

    await iwf.continue_approved_improvement_commit(
        data_dir=data_dir,
        instance_id="t1",
        approval_id=recovery_approval_id,
        restart_fn=lambda: calls.__setitem__("restart", calls["restart"] + 1),
        git_commit_fn=fake_commit,
        git_push_fn=fake_push,
    )
    assert (await _attempt(data_dir))["final_state"] == "push_unconfirmed"

    processed = await iwf.process_approved_improvement_commit_continuations(
        data_dir=data_dir,
        instance_id="t1",
        restart_fn=lambda: calls.__setitem__("restart", calls["restart"] + 1),
        git_commit_fn=fake_commit,
        git_push_fn=fake_push,
    )
    events = await _events(data_dir)
    push_succeeded = [
        json.loads(e["detail"]) for e in events if e["kind"] == "push_succeeded"
    ]
    assert processed == 1
    assert calls["push"] == [
        initial_approval_id,
        recovery_approval_id,
        recovery_approval_id,
    ]
    assert [e["approval_id"] for e in push_succeeded] == [
        initial_approval_id,
        recovery_approval_id,
    ]
    assert push_succeeded[-1]["commit_sha"] == "recoveryabc123"
    assert (await _attempt(data_dir))["final_state"] == (
        "awaiting_post_restart_test"
    )


async def test_approved_commit_reconciler_resumes_once(recovery_env, monkeypatch):
    data_dir = recovery_env
    await _create_attempt(data_dir, final_state="awaiting_commit_approval")
    approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="commit it",
        binding_payload={
            "kind": "git_commit_authorization",
            "attempt_id": "att_recovery",
            "workspace_dir": "/tmp/recovery-worktree",
            "expected_parent_sha": "parent_sha",
            "expected_diff_hash": "sha256:test",
            "target_branch": "main",
            "summary": "demo commit",
        },
        event_stream=None,
    )
    ok, _ = await _approvals.approve(
        data_dir=data_dir,
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    assert ok
    monkeypatch.setattr(iwf, "_staged_files", lambda _path: _async(["README.md"]))
    calls = {"commit": 0, "push": 0, "restart": 0}

    async def fake_commit(*, tool_input, instance_id, data_dir):
        calls["commit"] += 1
        await _approvals.set_outcome_field(
            data_dir=data_dir,
            approval_id=tool_input["approval_id"],
            field="commit_sha",
            value="abc123def456",
        )
        return "Committed `abc123def456` in the worktree."

    async def fake_push(*, tool_input, instance_id, data_dir):
        calls["push"] += 1
        return {
            "ok": True,
            "message": "Pushed `abc123def456` to `origin/main`.",
            "commit_sha": "abc123def456",
        }

    async def fake_origin_head_matches_commit(**kwargs):
        return True, {"origin_sha": kwargs["commit_sha"]}

    monkeypatch.setattr(
        iwf, "_origin_head_matches_commit", fake_origin_head_matches_commit,
    )

    def restart_fn():
        calls["restart"] += 1

    processed = await iwf.process_approved_improvement_commit_continuations(
        data_dir=data_dir,
        instance_id="t1",
        restart_fn=restart_fn,
        git_commit_fn=fake_commit,
        git_push_fn=fake_push,
    )
    processed_again = await iwf.process_approved_improvement_commit_continuations(
        data_dir=data_dir,
        instance_id="t1",
        restart_fn=restart_fn,
        git_commit_fn=fake_commit,
        git_push_fn=fake_push,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    assert processed == 1
    assert processed_again == 0
    assert calls == {"commit": 1, "push": 1, "restart": 1}
    assert row["final_state"] == "awaiting_post_restart_test"
    assert sum(e["kind"] == "commit_recorded" for e in events) == 1


async def test_approved_commit_reconciler_records_already_pushed_commit(
    recovery_env, monkeypatch,
):
    data_dir = recovery_env
    await _create_attempt(data_dir, final_state="awaiting_commit_approval")
    approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="commit it",
        binding_payload={
            "kind": "git_commit_authorization",
            "attempt_id": "att_recovery",
            "workspace_dir": "/tmp/recovery-worktree",
            "expected_parent_sha": "parent_sha",
            "expected_diff_hash": "sha256:test",
            "target_branch": "main",
            "summary": "demo commit",
        },
        event_stream=None,
    )
    ok, _ = await _approvals.approve(
        data_dir=data_dir,
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    assert ok
    await _approvals.set_outcome_field(
        data_dir=data_dir,
        approval_id=approval_id,
        field="commit_sha",
        value="abc123def456",
    )
    monkeypatch.setattr(iwf, "_head_sha", lambda _path: _async("abc123def456"))
    calls = {"commit": 0, "push": 0, "restart": 0}

    async def fake_commit(**_kwargs):
        calls["commit"] += 1
        raise AssertionError("existing receipt commit_sha must not recommit")

    async def fake_push(*, tool_input, instance_id, data_dir):
        calls["push"] += 1
        return {
            "ok": True,
            "reason": "already_pushed",
            "message": "`origin/main` already points at `abc123def456`.",
            "commit_sha": "abc123def456",
            "origin_confirmed": True,
        }

    async def fake_origin_head_matches_commit(**kwargs):
        return True, {"origin_sha": kwargs["commit_sha"]}

    monkeypatch.setattr(
        iwf, "_origin_head_matches_commit", fake_origin_head_matches_commit,
    )

    processed = await iwf.process_approved_improvement_commit_continuations(
        data_dir=data_dir,
        instance_id="t1",
        restart_fn=lambda: calls.__setitem__("restart", calls["restart"] + 1),
        git_commit_fn=fake_commit,
        git_push_fn=fake_push,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    commits = await _commits(data_dir)
    kinds = [e["kind"] for e in events]
    assert processed == 1
    assert calls == {"commit": 0, "push": 1, "restart": 1}
    assert row["final_state"] == "awaiting_post_restart_test"
    assert commits[0]["commit_sha"] == "abc123def456"
    assert "commit_recorded" in kinds
    assert "push_succeeded" in kinds
    assert "push_failed" not in kinds


async def test_approval_continuation_recovers_clean_advanced_head_missing_receipt_commit_sha(
    recovery_env, tmp_path, monkeypatch,
):
    data_dir = recovery_env
    worktree = tmp_path / "recovery-worktree"
    parent_sha = _init_clean_repo(worktree)
    await _create_attempt(
        data_dir,
        final_state="awaiting_commit_approval",
        worktree_path=str(worktree),
    )
    (worktree / "README.md").write_text("initial\napproved change\n")
    _git(worktree, "add", "README.md")
    expected_diff_hash = gop._compute_staged_diff_hash(str(worktree))
    approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="commit it",
        binding_payload={
            "kind": "git_commit_authorization",
            "attempt_id": "att_recovery",
            "workspace_dir": str(worktree),
            "expected_parent_sha": parent_sha,
            "expected_diff_hash": expected_diff_hash,
            "target_branch": "main",
            "summary": "demo commit",
        },
        event_stream=None,
    )
    ok, _ = await _approvals.approve(
        data_dir=data_dir,
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    assert ok
    _git(worktree, "commit", "-q", "-m", "approved change")
    advanced_sha = _git(worktree, "rev-parse", "HEAD")
    calls = {"commit": 0, "push": 0, "restart": 0}

    async def fail_commit(**_kwargs):
        calls["commit"] += 1
        raise AssertionError("clean advanced HEAD must be recovered")

    async def fake_push(*, tool_input, instance_id, data_dir):
        calls["push"] += 1
        receipt = await _approvals.get_receipt(
            data_dir=data_dir,
            approval_id=tool_input["approval_id"],
        )
        outcome = json.loads(receipt["outcome_payload_json"] or "{}")
        assert outcome["commit_sha"] == advanced_sha
        return {
            "ok": True,
            "message": f"Pushed `{advanced_sha}` to `origin/main`.",
            "commit_sha": advanced_sha,
            "origin_confirmed": True,
        }

    async def fake_origin_head_matches_commit(**kwargs):
        assert kwargs["commit_sha"] == advanced_sha
        return True, {"origin_sha": advanced_sha}

    monkeypatch.setattr(
        iwf, "_origin_head_matches_commit", fake_origin_head_matches_commit,
    )

    text = await iwf.continue_approved_improvement_commit(
        data_dir=data_dir,
        instance_id="t1",
        approval_id=approval_id,
        restart_fn=lambda: calls.__setitem__("restart", calls["restart"] + 1),
        git_commit_fn=fail_commit,
        git_push_fn=fake_push,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    commits = await _commits(data_dir)
    receipt = await _approvals.get_receipt(
        data_dir=data_dir,
        approval_id=approval_id,
    )
    outcome = json.loads(receipt["outcome_payload_json"] or "{}")
    assert "awaiting post-restart tests" in text
    assert calls == {"commit": 0, "push": 1, "restart": 1}
    assert row["final_state"] == "awaiting_post_restart_test"
    assert commits[0]["commit_sha"] == advanced_sha
    assert outcome["commit_sha"] == advanced_sha
    assert outcome["recovered_unrecorded_commit"] is True
    assert any(
        e["kind"] == "commit_recorded"
        and "recovered_unrecorded_commit" in e["detail"]
        for e in events
    )


@pytest.mark.parametrize("mode", ["unchanged", "mismatch"])
async def test_approval_continuation_clean_missing_commit_sha_unrecoverable_terminal(
    recovery_env, tmp_path, mode,
):
    data_dir = recovery_env
    worktree = tmp_path / f"{mode}-worktree"
    parent_sha = _init_clean_repo(worktree)
    await _create_attempt(
        data_dir,
        final_state="awaiting_commit_approval",
        worktree_path=str(worktree),
    )
    approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="commit it",
        binding_payload={
            "kind": "git_commit_authorization",
            "attempt_id": "att_recovery",
            "workspace_dir": str(worktree),
            "expected_parent_sha": parent_sha,
            "expected_diff_hash": "sha256:not-the-approved-diff",
            "target_branch": "main",
            "summary": "demo commit",
        },
        event_stream=None,
    )
    ok, _ = await _approvals.approve(
        data_dir=data_dir,
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    assert ok
    if mode == "mismatch":
        (worktree / "README.md").write_text("initial\nwrong change\n")
        _git(worktree, "add", "README.md")
        _git(worktree, "commit", "-q", "-m", "wrong change")

    async def fail_commit(**_kwargs):
        raise AssertionError("unrecoverable clean worktree must not recommit")

    async def fail_push(**_kwargs):
        raise AssertionError("unrecoverable clean worktree must not push")

    text = await iwf.continue_approved_improvement_commit(
        data_dir=data_dir,
        instance_id="t1",
        approval_id=approval_id,
        restart_fn=lambda: None,
        git_commit_fn=fail_commit,
        git_push_fn=fail_push,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    reason = (
        "head_unchanged" if mode == "unchanged"
        else "committed_diff_mismatch"
    )
    assert "no approved commit could be recovered" in text
    assert row["final_state"] == "attempt_failed"
    assert not await _commits(data_dir)
    assert any(
        e["kind"] == "attempt_failed"
        and "commit_unrecoverable" in e["detail"]
        and reason in e["detail"]
        for e in events
    )


async def test_approval_continuation_commit_refusal_does_not_push_parent_head(
    recovery_env, tmp_path,
):
    data_dir = recovery_env
    worktree = tmp_path / "refused-commit-worktree"
    parent_sha = _init_clean_repo(worktree)
    await _create_attempt(
        data_dir,
        final_state="awaiting_commit_approval",
        worktree_path=str(worktree),
    )
    (worktree / "README.md").write_text("initial\napproved change\n")
    _git(worktree, "add", "README.md")
    expected_diff_hash = gop._compute_staged_diff_hash(str(worktree))
    approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="commit it",
        binding_payload={
            "kind": "git_commit_authorization",
            "attempt_id": "att_recovery",
            "workspace_dir": str(worktree),
            "expected_parent_sha": parent_sha,
            "expected_diff_hash": expected_diff_hash,
            "target_branch": "main",
            "summary": "demo commit",
        },
        event_stream=None,
    )
    ok, _ = await _approvals.approve(
        data_dir=data_dir,
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    assert ok
    calls = {"commit": 0, "push": 0, "restart": 0}

    async def refusing_commit(*, tool_input, instance_id, data_dir):
        calls["commit"] += 1
        assert tool_input["approval_id"] == approval_id
        return {
            "ok": False,
            "reason": "substrate_health_failed",
            "message": "Commit refused before git commit.",
        }

    async def fail_push(**_kwargs):
        calls["push"] += 1
        raise AssertionError("refused commit must not push")

    text = await iwf.continue_approved_improvement_commit(
        data_dir=data_dir,
        instance_id="t1",
        approval_id=approval_id,
        restart_fn=lambda: calls.__setitem__("restart", calls["restart"] + 1),
        git_commit_fn=refusing_commit,
        git_push_fn=fail_push,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    assert "did not create a commit" in text
    assert "no push was attempted" in text
    assert _git(worktree, "rev-parse", "HEAD") == parent_sha
    assert row["final_state"] == "attempt_failed"
    assert calls == {"commit": 1, "push": 0, "restart": 0}
    assert not await _commits(data_dir)
    assert not any(e["kind"] == "push_failed" for e in events)
    assert any(
        e["kind"] == "attempt_failed"
        and "commit_refused" in e["detail"]
        and "head_unchanged" in e["detail"]
        for e in events
    )


async def test_origin_confirmation_requires_successful_fetch():
    calls = []

    class FakeGitOps:
        async def _run_git(self, args, *, cwd):
            calls.append(args)
            if args == ["fetch", "origin"]:
                return 1, "", "network down"
            if args == ["rev-parse", "--verify", "origin/main"]:
                raise AssertionError("stale origin ref must not be read")
            raise AssertionError(f"unexpected git args: {args}")

    matched, detail = await iwf._origin_head_matches_commit(
        git_ops=FakeGitOps(),
        workspace_dir="/tmp/worktree",
        target_branch="main",
        commit_sha="abc123",
        instance_id="t1",
        data_dir="/tmp/data",
    )
    assert matched is False
    assert detail["origin_confirmed"] is False
    assert detail["origin_confirmation_reason"] == "fetch_failed"
    assert calls == [["fetch", "origin"]]


async def test_approved_commit_reconciler_repairs_commit_recorded_before_push(
    recovery_env, monkeypatch,
):
    data_dir = recovery_env
    await _create_attempt(data_dir, final_state="awaiting_commit_approval")
    approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="commit it",
        binding_payload={
            "kind": "git_commit_authorization",
            "attempt_id": "att_recovery",
            "workspace_dir": "/tmp/recovery-worktree",
            "expected_parent_sha": "parent_sha",
            "expected_diff_hash": "sha256:test",
            "target_branch": "main",
            "summary": "demo commit",
        },
        event_stream=None,
    )
    ok, _ = await _approvals.approve(
        data_dir=data_dir,
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    assert ok
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        await _ledger.record_commit(
            db._conn,
            attempt_id="att_recovery",
            commit_sha="abc123def456",
            parent_sha="parent_sha",
            approval_id=approval_id,
        )
        await _ledger.append_event(
            db._conn,
            attempt_id="att_recovery",
            kind="commit_recorded",
            detail=json.dumps({
                "approval_id": approval_id,
                "commit_sha": "abc123def456",
                "parent_sha": "parent_sha",
            }),
        )
    finally:
        await db.close()
    calls = {"push": 0, "restart": 0}

    async def fail_push(**_kwargs):
        calls["push"] += 1
        raise AssertionError("confirmed existing commit must not push")

    async def fake_origin_head_matches_commit(**kwargs):
        return True, {
            "origin_sha": kwargs["commit_sha"],
            "fetch_result": "Fetched origin.",
        }

    monkeypatch.setattr(
        iwf, "_origin_head_matches_commit", fake_origin_head_matches_commit,
    )

    processed = await iwf.process_approved_improvement_commit_continuations(
        data_dir=data_dir,
        instance_id="t1",
        restart_fn=lambda: calls.__setitem__("restart", calls["restart"] + 1),
        git_push_fn=fail_push,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    kinds = [e["kind"] for e in events]
    assert processed == 1
    assert calls == {"push": 0, "restart": 1}
    assert row["final_state"] == "awaiting_post_restart_test"
    assert sum(e["kind"] == "commit_recorded" for e in events) == 1
    assert "push_succeeded" in kinds
    assert "push_failed" not in kinds


def _async(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner()


def _init_clean_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=repo_dir, env=env, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_dir, env=env, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo_dir, env=env, check=True,
    )
    (repo_dir / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=repo_dir, env=env, check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir, env=env, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _git(repo_dir: Path, *args: str) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


async def _record_final_commit(
    data_dir: str,
    *,
    attempt_id: str = "att_recovery",
    commit_sha: str = "abc123def456",
) -> None:
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        await _ledger.record_commit(
            db._conn,
            attempt_id=attempt_id,
            commit_sha=commit_sha,
            parent_sha="parent_sha",
            approval_id="approval_recorded",
        )
    finally:
        await db.close()


def _matching_live_head(commit_sha: str):
    async def _inner(_repo):
        return commit_sha, {"live_repo_dir": "/tmp/live"}

    return _inner


async def test_post_restart_pass_completes_attempt(recovery_env):
    data_dir = recovery_env
    await _create_attempt(data_dir)
    await _record_final_commit(data_dir, commit_sha="liveabc123")

    async def fake_self_test(**_kwargs):
        return {"outcome": "pass", "summary": "12 passed in 1.0s"}

    async def fake_live_head(_repo):
        return "liveabc123", {"live_repo_dir": "/tmp/live"}

    count = await iwf.run_pending_post_restart_tests(
        data_dir=data_dir,
        instance_id="t1",
        self_test_fn=fake_self_test,
        live_head_fn=fake_live_head,
    )
    row = await _attempt(data_dir)
    assert count == 1
    assert row["final_state"] == "completed"
    assert row["test_outcome"] == "pass"
    assert row["first_pass_green"] == 1


async def test_post_restart_pass_fires_completion_wake(recovery_env):
    """Success must NOT be silent: on `completed`, the reconciler wakes the
    origin space so the agent proactively tells the user it landed (the loop
    deploys via restart, so the in-process terminal-notify never fires for
    completed). Regression for att_4e9e3f283080 finishing without any ack."""
    data_dir = recovery_env
    await _create_attempt(data_dir)
    await _record_final_commit(data_dir, commit_sha="liveabc123")

    async def fake_self_test(**_kwargs):
        return {"outcome": "pass", "summary": "107 passed in 3.7s"}

    async def fake_live_head(_repo):
        return "liveabc123", {"live_repo_dir": "/tmp/live"}

    woke: list[dict] = []

    async def completed_wake_fn(payload):
        woke.append(payload)

    count = await iwf.run_pending_post_restart_tests(
        data_dir=data_dir,
        instance_id="t1",
        self_test_fn=fake_self_test,
        live_head_fn=fake_live_head,
        completed_wake_fn=completed_wake_fn,
    )
    row = await _attempt(data_dir)
    assert count == 1
    assert row["final_state"] == "completed"
    assert len(woke) == 1, "completion must wake the origin exactly once"
    p = woke[0]
    assert p["originating_space"] == "space_origin"
    assert p["attempt_id"] == "att_recovery"
    assert p["commit_sha"] == "liveabc123"
    assert "107 passed" in p["self_test_summary"]


async def test_post_restart_live_head_mismatch_is_not_completed_or_tested(
    recovery_env,
):
    data_dir = recovery_env
    await _create_attempt(data_dir)
    await _record_final_commit(data_dir, commit_sha="pushedabc123")

    calls = []

    async def fake_self_test(**_kwargs):
        calls.append(True)
        return {"outcome": "pass", "summary": "12 passed in 1.0s"}

    async def fake_live_head(_repo):
        return "oldlive999", {"live_repo_dir": "/tmp/live"}

    count = await iwf.run_pending_post_restart_tests(
        data_dir=data_dir,
        instance_id="t1",
        self_test_fn=fake_self_test,
        live_head_fn=fake_live_head,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    assert count == 1
    assert row["final_state"] == "live_head_mismatch"
    assert row["test_outcome"] is None
    assert row["first_pass_green"] is None
    assert calls == []
    assert any(
        e["kind"] == "live_head_mismatch"
        and "pushedabc123" in e["detail"]
        and "oldlive999" in e["detail"]
        for e in events
    )


async def test_post_restart_live_head_mismatch_wins_over_failing_tests(
    recovery_env,
):
    data_dir = recovery_env
    await _create_attempt(data_dir)
    await _record_final_commit(data_dir, commit_sha="pushedabc123")
    calls = []

    async def fake_self_test(**_kwargs):
        calls.append(True)
        return {
            "outcome": "fail",
            "summary": "1 failed. Failing: tests/test_demo.py.",
        }

    async def fake_live_head(_repo):
        return "wronglive999", {"live_repo_dir": "/tmp/live"}

    count = await iwf.run_pending_post_restart_tests(
        data_dir=data_dir,
        instance_id="t1",
        self_test_fn=fake_self_test,
        live_head_fn=fake_live_head,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    kinds = [e["kind"] for e in events]
    assert count == 1
    assert row["final_state"] == "live_head_mismatch"
    assert row["test_outcome"] is None
    assert calls == []
    assert "live_head_mismatch" in kinds
    assert "recovery_decision_requested" not in kinds


async def test_post_restart_failure_requests_recovery_and_wakes_origin(
    recovery_env,
):
    data_dir = recovery_env
    await _create_attempt(data_dir)
    await _record_final_commit(data_dir, commit_sha="pushedabc123")
    wakes = []

    async def fake_self_test(**_kwargs):
        return {
            "outcome": "fail",
            "summary": "1 failed, 0 errors. Failing: tests/test_demo.py.",
            "failure_evidence": {
                "failed_test_ids": ["tests/test_demo.py::test_specific"],
                "failure_excerpt": (
                    "E   AssertionError: substrate mismatch at recovery"
                ),
            },
        }

    async def wake_fn(payload):
        wakes.append(payload)

    await iwf.run_pending_post_restart_tests(
        data_dir=data_dir,
        instance_id="t1",
        self_test_fn=fake_self_test,
        wake_fn=wake_fn,
        live_head_fn=_matching_live_head("pushedabc123"),
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    assert row["final_state"] == "awaiting_recovery_decision"
    assert row["first_pass_green"] == 0
    assert any(e["kind"] == "recovery_decision_requested" for e in events)
    assert wakes and wakes[0]["originating_space"] == "space_origin"
    assert wakes[0]["failed_test_ids"] == [
        "tests/test_demo.py::test_specific"
    ]
    assert "substrate mismatch" in wakes[0]["failure_excerpt"]
    decision = json.loads([
        e for e in events if e["kind"] == "recovery_decision_requested"
    ][-1]["detail"])
    assert decision["failure_evidence"]["failed_test_ids"] == [
        "tests/test_demo.py::test_specific"
    ]
    assert (
        "substrate mismatch"
        in decision["failure_evidence"]["failure_excerpt"]
    )


async def test_proceed_recovery_green_requests_approval_not_commit(
    recovery_env, monkeypatch,
):
    data_dir = recovery_env
    await _create_attempt(
        data_dir, final_state="awaiting_recovery_decision",
    )
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        await _ledger.append_event(
            db._conn,
            attempt_id="att_recovery",
            kind="recovery_decision_requested",
            detail=json.dumps({
                "failure_summary": "fail summary",
                "failed_test_ids": ["tests/test_demo.py"],
                "failure_evidence": {
                    "failed_test_ids": ["tests/test_demo.py"],
                    "failure_excerpt": (
                        "E   AssertionError: recovery prompt detail"
                    ),
                },
            }),
        )
    finally:
        await db.close()
    monkeypatch.setattr(
        iwf, "_request_commit_approval_for_attempt", _fake_request_approval,
    )

    async def consult_fn(*, target, prompt):
        assert target == "codex"
        assert "tests/test_demo.py" in prompt
        assert "Failure excerpt:" in prompt
        assert "recovery prompt detail" in prompt
        return "fixed\n\nSTATUS: GREEN"

    text = await iwf.proceed_with_recovery_service(
        data_dir=data_dir,
        instance_id="t1",
        attempt_id="att_recovery",
        consult_fn=consult_fn,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    approval_id = await _latest_approval_id(data_dir, "att_recovery")
    receipt = await _approvals.get_receipt(
        data_dir=data_dir, approval_id=approval_id,
    )
    assert "requested operator commit approval" in text
    assert row["final_state"] == "awaiting_recovery_commit_approval"
    assert receipt["state"] == "pending"
    assert not await _commits(data_dir)
    assert "recovery_started" in [e["kind"] for e in events]
    assert "recovery_iteration" in [e["kind"] for e in events]


async def test_recovery_consult_uses_worktree_and_green_empty_diff_no_approval(
    recovery_env, tmp_path, monkeypatch,
):
    data_dir = recovery_env
    worktree_path = tmp_path / "recovery-worktree"
    _init_clean_repo(worktree_path)
    await _create_attempt(
        data_dir,
        final_state="awaiting_recovery_decision",
        worktree_path=str(worktree_path),
    )
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        await _ledger.append_event(
            db._conn,
            attempt_id="att_recovery",
            kind="recovery_decision_requested",
            detail=json.dumps({
                "failure_summary": "fail summary",
                "failed_test_ids": ["tests/test_demo.py"],
            }),
        )
    finally:
        await db.close()

    async def fail_request_approval(**_kwargs):
        raise AssertionError("empty recovery diff must not request approval")

    monkeypatch.setattr(
        iwf, "_request_commit_approval_for_attempt", fail_request_approval,
    )
    consult_calls = []

    async def consult_fn(*, target, prompt, instance_id, workspace_dir):
        consult_calls.append({
            "target": target,
            "prompt": prompt,
            "instance_id": instance_id,
            "workspace_dir": workspace_dir,
        })
        return "claimed fixed\n\nSTATUS: GREEN"

    text = await iwf.proceed_with_recovery_service(
        data_dir=data_dir,
        instance_id="t1",
        attempt_id="att_recovery",
        consult_fn=consult_fn,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    assert consult_calls[0]["workspace_dir"] == str(worktree_path)
    assert consult_calls[0]["instance_id"] == "t1"
    assert "no worktree diff" in text
    assert row["final_state"] == "awaiting_recovery_decision"
    assert "recovery_no_diff" in [e["kind"] for e in events]
    assert "approval_requested" not in [e["kind"] for e in events]


async def test_recovery_commit_approval_rejected_returns_to_decision(
    recovery_env, monkeypatch,
):
    data_dir = recovery_env
    await _create_attempt(
        data_dir, final_state="awaiting_recovery_decision",
    )
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        await _ledger.append_event(
            db._conn,
            attempt_id="att_recovery",
            kind="recovery_decision_requested",
            detail=json.dumps({
                "failure_summary": "fail summary",
                "failed_test_ids": ["tests/test_demo.py"],
            }),
        )
    finally:
        await db.close()
    monkeypatch.setattr(
        iwf, "_request_commit_approval_for_attempt", _fake_request_approval,
    )

    async def consult_fn(*, target, prompt):
        return "fixed\n\nSTATUS: GREEN"

    await iwf.proceed_with_recovery_service(
        data_dir=data_dir,
        instance_id="t1",
        attempt_id="att_recovery",
        consult_fn=consult_fn,
    )
    approval_id = await _latest_approval_id(data_dir, "att_recovery")
    ok, _ = await _approvals.reject(
        data_dir=data_dir,
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        reason="needs narrower fix",
        event_stream=None,
    )
    assert ok

    text = await iwf.handle_improvement_commit_approval_terminal_decision(
        data_dir=data_dir,
        approval_id=approval_id,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    assert "awaiting_recovery_decision" in text
    assert row["final_state"] == "awaiting_recovery_decision"
    assert any(
        e["kind"] == "recovery_commit_approval_rejected"
        and "needs narrower fix" in e["detail"]
        for e in events
    )


async def test_recovery_commit_approval_expired_returns_to_decision(
    recovery_env, monkeypatch,
):
    import aiosqlite

    data_dir = recovery_env
    await _create_attempt(
        data_dir, final_state="awaiting_recovery_decision",
    )
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        await _ledger.append_event(
            db._conn,
            attempt_id="att_recovery",
            kind="recovery_decision_requested",
            detail=json.dumps({
                "failure_summary": "fail summary",
                "failed_test_ids": ["tests/test_demo.py"],
            }),
        )
    finally:
        await db.close()
    monkeypatch.setattr(
        iwf, "_request_commit_approval_for_attempt", _fake_request_approval,
    )

    async def consult_fn(*, target, prompt):
        return "fixed\n\nSTATUS: GREEN"

    await iwf.proceed_with_recovery_service(
        data_dir=data_dir,
        instance_id="t1",
        attempt_id="att_recovery",
        consult_fn=consult_fn,
    )
    approval_id = await _latest_approval_id(data_dir, "att_recovery")
    async with aiosqlite.connect(str(Path(data_dir) / "instance.db")) as conn:
        await conn.execute(
            "UPDATE approval_receipts SET expires_at=? WHERE approval_id=?",
            ("2020-01-01T00:00:00+00:00", approval_id),
        )
        await conn.commit()

    expired = await _approvals.expire_pass(
        data_dir=data_dir,
        event_stream=None,
    )
    processed = await iwf.process_terminal_improvement_approval_decisions(
        data_dir=data_dir,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    assert expired == 1
    assert processed == 1
    assert row["final_state"] == "awaiting_recovery_decision"
    assert any(
        e["kind"] == "recovery_commit_approval_expired"
        for e in events
    )


async def test_initial_commit_approval_rejected_marks_terminal(recovery_env):
    data_dir = recovery_env
    await _create_attempt(data_dir, final_state="awaiting_commit_approval")
    approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="commit it",
        binding_payload={
            "kind": "git_commit_authorization",
            "attempt_id": "att_recovery",
            "workspace_dir": "/tmp/recovery-worktree",
            "expected_parent_sha": "parent_sha",
            "expected_diff_hash": "sha256:test",
            "target_branch": "main",
            "summary": "demo commit",
        },
        event_stream=None,
    )
    ok, _ = await _approvals.reject(
        data_dir=data_dir,
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        reason="do not ship",
        event_stream=None,
    )
    assert ok

    text = await iwf.handle_improvement_commit_approval_terminal_decision(
        data_dir=data_dir,
        approval_id=approval_id,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    assert "attempt_rejected_at_commit" in text
    assert row["final_state"] == "attempt_rejected_at_commit"
    assert row["ended_at"]
    assert any(
        e["kind"] == "attempt_rejected_at_commit"
        and "do not ship" in e["detail"]
        for e in events
    )


async def test_initial_commit_approval_expired_marks_terminal(recovery_env):
    import aiosqlite

    data_dir = recovery_env
    await _create_attempt(data_dir, final_state="awaiting_commit_approval")
    approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="commit it",
        binding_payload={
            "kind": "git_commit_authorization",
            "attempt_id": "att_recovery",
            "workspace_dir": "/tmp/recovery-worktree",
            "expected_parent_sha": "parent_sha",
            "expected_diff_hash": "sha256:test",
            "target_branch": "main",
            "summary": "demo commit",
        },
        event_stream=None,
    )
    async with aiosqlite.connect(str(Path(data_dir) / "instance.db")) as conn:
        await conn.execute(
            "UPDATE approval_receipts SET expires_at=? WHERE approval_id=?",
            ("2020-01-01T00:00:00+00:00", approval_id),
        )
        await conn.commit()

    expired = await _approvals.expire_pass(
        data_dir=data_dir,
        event_stream=None,
    )
    processed = await iwf.process_terminal_improvement_approval_decisions(
        data_dir=data_dir,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    assert expired == 1
    assert processed == 1
    assert row["final_state"] == "attempt_expired_at_commit"
    assert row["ended_at"]
    assert any(e["kind"] == "attempt_expired_at_commit" for e in events)


async def test_proceed_recovery_consult_exception_returns_to_decision(
    recovery_env,
):
    data_dir = recovery_env
    await _create_attempt(
        data_dir, final_state="awaiting_recovery_decision",
    )
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        await _ledger.append_event(
            db._conn,
            attempt_id="att_recovery",
            kind="recovery_decision_requested",
            detail=json.dumps({
                "failure_summary": "fail summary",
                "failed_test_ids": ["tests/test_demo.py"],
            }),
        )
    finally:
        await db.close()

    async def consult_fn(*, target, prompt):
        raise RuntimeError("consult crashed")

    text = await iwf.proceed_with_recovery_service(
        data_dir=data_dir,
        instance_id="t1",
        attempt_id="att_recovery",
        consult_fn=consult_fn,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    kinds = [e["kind"] for e in events]
    assert "awaiting_recovery_decision" in text
    assert row["final_state"] == "awaiting_recovery_decision"
    assert "recovery_started" in kinds
    assert "recovery_failed" in kinds
    assert "approval_requested" not in kinds


async def test_proceed_recovery_consult_exception_on_second_hit_caps(
    recovery_env,
):
    data_dir = recovery_env
    await _create_attempt(
        data_dir, final_state="awaiting_recovery_decision",
    )
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        await _ledger.append_event(
            db._conn,
            attempt_id="att_recovery",
            kind="recovery_started",
            detail=json.dumps({"iteration": 1}),
        )
        await _ledger.append_event(
            db._conn,
            attempt_id="att_recovery",
            kind="recovery_decision_requested",
            detail=json.dumps({
                "failure_summary": "fail summary",
                "failed_test_ids": ["tests/test_demo.py"],
            }),
        )
    finally:
        await db.close()

    async def consult_fn(*, target, prompt):
        raise RuntimeError("consult crashed again")

    await iwf.proceed_with_recovery_service(
        data_dir=data_dir,
        instance_id="t1",
        attempt_id="att_recovery",
        consult_fn=consult_fn,
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    kinds = [e["kind"] for e in events]
    assert row["final_state"] == "test_failed_unrecovered"
    assert "recovery_failed" in kinds
    assert "recovery_cap_hit" in kinds


async def test_boot_reconciler_resets_stale_recovery_in_progress(
    recovery_env,
):
    data_dir = recovery_env
    await _create_attempt(
        data_dir, final_state="recovery_in_progress",
    )
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        await _ledger.append_event(
            db._conn,
            attempt_id="att_recovery",
            kind="recovery_started",
            detail=json.dumps({
                "iteration": 1,
                "trigger": "post_restart_self_test_failed",
            }),
        )
    finally:
        await db.close()

    processed = await iwf.reconcile_stale_recovery_in_progress_attempts(
        data_dir=data_dir,
        instance_id="t1",
    )
    processed_again = await iwf.reconcile_stale_recovery_in_progress_attempts(
        data_dir=data_dir,
        instance_id="t1",
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    kinds = [e["kind"] for e in events]
    assert processed == 1
    assert processed_again == 0
    assert row["final_state"] == "awaiting_recovery_decision"
    assert "recovery_reset_after_crash" in kinds
    assert "recovery_cap_hit" not in kinds


async def test_boot_reconciler_preserves_recovery_receipt_inserted_before_ledger_event(
    recovery_env, monkeypatch,
):
    data_dir = recovery_env
    await _create_attempt(
        data_dir,
        final_state="recovery_in_progress",
    )
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        await _ledger.append_event(
            db._conn,
            attempt_id="att_recovery",
            kind="recovery_started",
            detail=json.dumps({
                "iteration": 1,
                "trigger": "post_restart_self_test_failed",
            }),
        )
    finally:
        await db.close()
    approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="recovery approval",
        binding_payload={
            "kind": "git_commit_authorization",
            "attempt_id": "att_recovery",
            "workspace_dir": "/tmp/recovery-worktree",
            "expected_parent_sha": "parent_sha",
            "expected_diff_hash": "sha256:test",
            "target_branch": "main",
            "summary": "recovery 1",
            "recovery_iteration": 1,
        },
        event_stream=None,
    )

    processed = await iwf.reconcile_stale_recovery_in_progress_attempts(
        data_dir=data_dir,
        instance_id="t1",
    )
    processed_again = await iwf.reconcile_stale_recovery_in_progress_attempts(
        data_dir=data_dir,
        instance_id="t1",
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    assert processed == 1
    assert processed_again == 0
    assert row["final_state"] == "awaiting_recovery_commit_approval"
    assert any(
        e["kind"] == "approval_requested"
        and approval_id in e["detail"]
        and "recovered_after_crash=true" in e["detail"]
        for e in events
    )
    assert not any(e["kind"] == "recovery_reset_after_crash" for e in events)

    ok, _ = await _approvals.approve(
        data_dir=data_dir,
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    assert ok
    monkeypatch.setattr(iwf, "_staged_files", lambda _path: _async(["README.md"]))

    async def fake_commit(*, tool_input, instance_id, data_dir):
        await _approvals.set_outcome_field(
            data_dir=data_dir,
            approval_id=tool_input["approval_id"],
            field="commit_sha",
            value="recoveryabc123",
        )
        return "Committed `recoveryabc123` in the worktree."

    async def fake_push(*, tool_input, instance_id, data_dir):
        return {
            "ok": True,
            "message": "Pushed `recoveryabc123` to `origin/main`.",
            "commit_sha": "recoveryabc123",
            "origin_confirmed": True,
        }

    async def fake_origin_head_matches_commit(**kwargs):
        return True, {"origin_sha": kwargs["commit_sha"]}

    monkeypatch.setattr(
        iwf, "_origin_head_matches_commit", fake_origin_head_matches_commit,
    )

    continued = await iwf.process_approved_improvement_commit_continuations(
        data_dir=data_dir,
        instance_id="t1",
        restart_fn=lambda: None,
        git_commit_fn=fake_commit,
        git_push_fn=fake_push,
    )
    assert continued == 1
    assert (await _attempt(data_dir))["final_state"] == (
        "awaiting_post_restart_test"
    )
    commits = await _commits(data_dir)
    assert commits[0]["commit_sha"] == "recoveryabc123"
    assert commits[0]["recovery_trigger"] == "post_restart_self_test_failed"


async def test_boot_reconciler_preserves_recovery_receipt_after_approval_event_crash(
    recovery_env,
):
    data_dir = recovery_env
    await _create_attempt(
        data_dir,
        final_state="recovery_in_progress",
    )
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        await _ledger.append_event(
            db._conn,
            attempt_id="att_recovery",
            kind="recovery_started",
            detail=json.dumps({
                "iteration": 1,
                "trigger": "post_restart_self_test_failed",
            }),
        )
    finally:
        await db.close()
    approval_id = await _approvals.request_approval(
        data_dir=data_dir,
        instance_id="t1",
        kind="git_commit_authorization",
        requested_for_actor="improvement_loop",
        operator_actor_id="owner",
        request_summary="recovery approval",
        binding_payload={
            "kind": "git_commit_authorization",
            "attempt_id": "att_recovery",
            "workspace_dir": "/tmp/recovery-worktree",
            "expected_parent_sha": "parent_sha",
            "expected_diff_hash": "sha256:test",
            "target_branch": "main",
            "summary": "recovery 1",
            "recovery_iteration": 1,
        },
        event_stream=None,
    )
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        await _ledger.append_event(
            db._conn,
            attempt_id="att_recovery",
            kind="approval_requested",
            detail=f"approval_id={approval_id} recovery_iteration=1",
        )
    finally:
        await db.close()

    processed = await iwf.reconcile_stale_recovery_in_progress_attempts(
        data_dir=data_dir,
        instance_id="t1",
    )
    processed_again = await iwf.reconcile_stale_recovery_in_progress_attempts(
        data_dir=data_dir,
        instance_id="t1",
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    approval_events = [
        e for e in events if e["kind"] == "approval_requested"
    ]
    assert processed == 1
    assert processed_again == 0
    assert row["final_state"] == "awaiting_recovery_commit_approval"
    assert len(approval_events) == 1
    assert approval_id in approval_events[0]["detail"]
    assert "recovered_after_crash=true" not in approval_events[0]["detail"]
    assert not any(e["kind"] == "recovery_reset_after_crash" for e in events)


async def test_boot_reconciler_caps_stale_second_recovery_start(
    recovery_env,
):
    data_dir = recovery_env
    await _create_attempt(
        data_dir, final_state="recovery_in_progress",
    )
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        for iteration in (1, 2):
            await _ledger.append_event(
                db._conn,
                attempt_id="att_recovery",
                kind="recovery_started",
                detail=json.dumps({"iteration": iteration}),
            )
    finally:
        await db.close()

    processed = await iwf.reconcile_stale_recovery_in_progress_attempts(
        data_dir=data_dir,
        instance_id="t1",
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    kinds = [e["kind"] for e in events]
    assert processed == 1
    assert row["final_state"] == "test_failed_unrecovered"
    assert "recovery_reset_after_crash" in kinds
    assert "recovery_cap_hit" in kinds


async def test_abandon_attempt_closes_without_consult_or_approval(recovery_env):
    data_dir = recovery_env
    await _create_attempt(
        data_dir, final_state="awaiting_recovery_decision",
    )
    text = await iwf.abandon_attempt_service(
        data_dir=data_dir,
        instance_id="t1",
        attempt_id="att_recovery",
        reason="not worth demo risk",
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    assert "abandoned" in text
    assert row["final_state"] == "test_failed_abandoned_by_agent"
    assert any(
        e["kind"] == "test_failed_abandoned_by_agent"
        and "not worth demo risk" in e["detail"]
        for e in events
    )
    assert not any(e["kind"] == "approval_requested" for e in events)


async def test_operator_abandon_override_precedes_shared_event(recovery_env):
    data_dir = recovery_env
    await _create_attempt(
        data_dir, final_state="awaiting_recovery_decision",
    )
    await iwf.abandon_attempt_service(
        data_dir=data_dir,
        instance_id="t1",
        attempt_id="att_recovery",
        reason="operator stop",
        operator_override=True,
    )
    kinds = [e["kind"] for e in await _events(data_dir)]
    assert kinds[-2:] == [
        "operator_recovery_override",
        "test_failed_abandoned_by_agent",
    ]


async def test_recovery_cap_hit_after_two_started_iterations(recovery_env):
    data_dir = recovery_env
    await _create_attempt(data_dir)
    await _record_final_commit(data_dir, commit_sha="pushedabc123")
    db = InstanceDB(data_dir)
    await db.connect()
    try:
        for i in (1, 2):
            await _ledger.append_event(
                db._conn,
                attempt_id="att_recovery",
                kind="recovery_started",
                detail=json.dumps({"iteration": i}),
            )
    finally:
        await db.close()

    async def fake_self_test(**_kwargs):
        return {"outcome": "fail", "summary": "still failing"}

    await iwf.run_pending_post_restart_tests(
        data_dir=data_dir,
        instance_id="t1",
        self_test_fn=fake_self_test,
        live_head_fn=_matching_live_head("pushedabc123"),
    )
    row = await _attempt(data_dir)
    events = await _events(data_dir)
    assert row["final_state"] == "test_failed_unrecovered"
    assert any(e["kind"] == "recovery_cap_hit" for e in events)


async def test_recovery_tool_surfacing_helper_absent_and_present(recovery_env):
    data_dir = recovery_env
    assert not await iwf.recovery_tools_visible_for_space(
        data_dir=data_dir,
        instance_id="t1",
        active_space_id="space_origin",
    )
    await _create_attempt(
        data_dir, final_state="awaiting_recovery_decision",
    )
    assert await iwf.recovery_tools_visible_for_space(
        data_dir=data_dir,
        instance_id="t1",
        active_space_id="space_origin",
    )
    assert not await iwf.recovery_tools_visible_for_space(
        data_dir=data_dir,
        instance_id="t1",
        active_space_id="other_space",
    )


class _CaptureRecoveryWake:
    def __init__(self):
        self.process_calls: list[NormalizedMessage] = []

    async def process(self, message: NormalizedMessage) -> str:
        self.process_calls.append(message)
        return "ok"

    inject_improvement_recovery_wake = (
        MessageHandler.inject_improvement_recovery_wake
    )


async def test_synthetic_recovery_wake_queues_origin_turn():
    capture = _CaptureRecoveryWake()
    await capture.inject_improvement_recovery_wake({
        "instance_id": "t1",
        "originating_space": "space_origin",
        "originating_member_id": "mem_origin",
        "attempt_id": "att_recovery",
        "failure_summary": "failure summary",
        "failed_test_ids": ["tests/test_demo.py"],
        "worktree_path": "/tmp/recovery-worktree",
        "recovery_iterations_used": 0,
    })
    import asyncio
    for _ in range(10):
        await asyncio.sleep(0.01)
        if capture.process_calls:
            break
    msg = capture.process_calls[0]
    assert msg.platform == "system"
    assert msg.conversation_id == "space_origin"
    assert msg.member_id == "mem_origin"
    assert "proceed_with_recovery" in msg.content
    assert "abandon_attempt" in msg.content
    env = msg.context["execution_envelope"]
    assert env["source"] == "improvement_recovery_decision_wake"


async def test_demo_recovery_flow_fail_proceed_approve_pass(
    recovery_env, monkeypatch,
):
    data_dir = recovery_env
    await _create_attempt(data_dir)
    await _record_final_commit(data_dir, commit_sha="initialabc123")
    outcomes = [
        {
            "outcome": "fail",
            "summary": "1 failed, 0 errors. Failing: tests/test_demo.py.",
        },
        {"outcome": "pass", "summary": "12 passed in 1.0s"},
    ]

    async def fake_self_test(**_kwargs):
        return outcomes.pop(0)

    await iwf.run_pending_post_restart_tests(
        data_dir=data_dir,
        instance_id="t1",
        self_test_fn=fake_self_test,
        live_head_fn=_matching_live_head("initialabc123"),
    )
    assert (await _attempt(data_dir))["final_state"] == (
        "awaiting_recovery_decision"
    )

    monkeypatch.setattr(
        iwf, "_request_commit_approval_for_attempt", _fake_request_approval,
    )

    async def consult_fn(*, target, prompt):
        return "bounded fix applied\n\nSTATUS: GREEN"

    await iwf.proceed_with_recovery_service(
        data_dir=data_dir,
        instance_id="t1",
        attempt_id="att_recovery",
        consult_fn=consult_fn,
    )
    approval_id = await _latest_approval_id(data_dir, "att_recovery")
    receipt = await _approvals.get_receipt(
        data_dir=data_dir, approval_id=approval_id,
    )
    assert receipt["state"] == "pending"
    commits = await _commits(data_dir)
    assert len(commits) == 1
    assert commits[0]["commit_sha"] == "initialabc123"

    ok, _ = await _approvals.approve(
        data_dir=data_dir,
        approval_id=approval_id,
        instance_id="t1",
        invoking_member_id="owner",
        event_stream=None,
    )
    assert ok
    monkeypatch.setattr(iwf, "_staged_files", lambda _path: _async(["README.md"]))

    async def fake_commit(*, tool_input, instance_id, data_dir):
        await _approvals.set_outcome_field(
            data_dir=data_dir,
            approval_id=tool_input["approval_id"],
            field="commit_sha",
            value="recoveryabc123",
        )
        return "Committed `recoveryabc123` in the worktree."

    async def fake_push(*, tool_input, instance_id, data_dir):
        return {
            "ok": True,
            "message": "Pushed `recoveryabc123` to `origin/main`.",
            "commit_sha": "recoveryabc123",
        }

    async def fake_origin_head_matches_commit(**kwargs):
        assert kwargs["commit_sha"] == "recoveryabc123"
        return True, {
            "origin_sha": "recoveryabc123",
            "fetch_result": "Fetched origin.",
        }

    monkeypatch.setattr(
        iwf, "_origin_head_matches_commit", fake_origin_head_matches_commit,
    )
    restarted = []
    await iwf.continue_approved_improvement_commit(
        data_dir=data_dir,
        instance_id="t1",
        approval_id=approval_id,
        restart_fn=lambda: restarted.append(True),
        git_commit_fn=fake_commit,
        git_push_fn=fake_push,
    )
    commits = await _commits(data_dir)
    assert restarted == [True]
    assert commits[-1]["recovery_trigger"] == (
        "post_restart_self_test_failed"
    )
    assert (await _attempt(data_dir))["final_state"] == (
        "awaiting_post_restart_test"
    )

    async def fake_live_head(_repo):
        return "recoveryabc123", {"live_repo_dir": "/tmp/live"}

    await iwf.run_pending_post_restart_tests(
        data_dir=data_dir,
        instance_id="t1",
        self_test_fn=fake_self_test,
        live_head_fn=fake_live_head,
    )
    assert (await _attempt(data_dir))["final_state"] == "completed"
