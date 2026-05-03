"""End-to-end integration tests for the workflow loop primitive.

WORKFLOW-LOOP-PRIMITIVE C7. These tests wire the full stack and
exercise the documented happy paths:

  - register a portable .workflow.yaml descriptor → trigger fires
    on a matching event → engine runs the action sequence → ledger
    captures each step → execution row is "completed".
  - webhook POST → event_stream → trigger → engine: a webhook
    delivery routed through the receiver fires a workflow.
  - approval-gate happy path: action runs, engine pauses, approval
    event arrives, engine resumes.
  - Notion-leak whole-spec pin: structural scan over the workflow
    primitive source tree allowing Notion references ONLY inside
    agent_inbox.py's NotionAgentInbox class.
"""
from __future__ import annotations

import asyncio
import re
import textwrap
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kernos.kernel import event_stream
from kernos.kernel.webhooks.receiver import (
    WebhookRegistry,
    WebhookSourceConfig,
    register_routes,
)
from kernos.kernel.workflows.action_library import (
    ActionLibrary,
    AppendToLedgerAction,
    MarkStateAction,
    NotifyUserAction,
)
from kernos.kernel.workflows.execution_engine import ExecutionEngine
from kernos.kernel.workflows.ledger import WorkflowLedger
from kernos.kernel.workflows.trigger_registry import (
    TriggerRegistry,
    _reset_for_tests as _reset_trigger_registry,
)
from kernos.kernel.workflows.workflow_registry import WorkflowRegistry


@pytest.fixture
async def stack(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    trig = TriggerRegistry()
    await trig.start(str(tmp_path))
    wfr = WorkflowRegistry()
    await wfr.start(str(tmp_path), trig)
    state: dict = {}

    async def state_set(*, key, value, scope, instance_id):
        state[(scope, instance_id, key)] = value

    async def state_get(*, key, scope, instance_id):
        return state.get((scope, instance_id, key))

    delivered: list = []

    async def deliver(**kw):
        delivered.append(kw)
        return {"persisted_id": f"msg-{len(delivered)}"}

    lib = ActionLibrary()
    lib.register(MarkStateAction(state_store_set=state_set, state_store_get=state_get))
    lib.register(NotifyUserAction(deliver_fn=deliver))
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
        "state": state,
        "delivered": delivered,
    }
    await engine.stop()
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await event_stream._reset_for_tests()


async def _wait_for(predicate, timeout=2.0, step=0.02):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


# ===========================================================================
# Portable descriptor → registration → execution
# ===========================================================================


def _morning_briefing_yaml() -> str:
    return textwrap.dedent("""
        workflow_id: morning-briefing
        instance_id: inst_a
        name: Morning briefing
        version: "1.0"
        owner: owner
        bounds:
          iteration_count: 1
          wall_time_seconds: 30
        verifier:
          flavor: deterministic
          check: briefing_marked
        action_sequence:
          - action_type: mark_state
            parameters:
              key: morning_briefing
              value: 1
              scope: instance
          - action_type: notify_user
            parameters:
              channel: primary
              message: Good morning.
              urgency: low
        trigger:
          event_type: time.tick
          predicate: 'event.payload.cadence == "daily"'
    """).lstrip()


class TestPortableDescriptorEndToEnd:
    async def test_register_from_yaml_then_trigger_runs_workflow(self, stack):
        path = stack["tmp_path"] / "morning.workflow.yaml"
        path.write_text(_morning_briefing_yaml())
        await stack["wfr"].register_workflow_from_file(str(path))
        # Fire a matching event.
        await event_stream.emit("inst_a", "time.tick", {"cadence": "daily"})
        await event_stream.flush_now()
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "morning_briefing") in stack["state"],
        )
        assert ok, "engine did not execute the action sequence"
        assert stack["state"][("instance", "inst_a", "morning_briefing")] == 1
        # Notification delivered through the wrapped surface.
        assert len(stack["delivered"]) == 1
        assert stack["delivered"][0]["message"] == "Good morning."
        # Ledger captured both steps.
        entries = await stack["ledger"].read_all("inst_a", "morning-briefing")
        assert len(entries) == 2
        assert entries[0]["agent_or_action"] == "mark_state"
        assert entries[1]["agent_or_action"] == "notify_user"
        # Execution row is "completed".
        execs = await stack["engine"].list_executions(
            "inst_a", state="completed",
        )
        assert any(e.workflow_id == "morning-briefing" for e in execs)


# ===========================================================================
# Webhook → event_stream → trigger → engine
# ===========================================================================


class TestWebhookEndToEnd:
    async def test_webhook_post_drives_workflow(self, stack):
        # Register a workflow that fires on external.webhook events
        # carrying a specific source_id.
        path = stack["tmp_path"] / "wh.workflow.yaml"
        path.write_text(textwrap.dedent("""
            workflow_id: wh-demo
            instance_id: inst_a
            name: webhook demo
            version: "1.0"
            owner: owner
            bounds:
              iteration_count: 1
              wall_time_seconds: 30
            verifier:
              flavor: deterministic
              check: ok
            action_sequence:
              - action_type: mark_state
                parameters:
                  key: wh_seen
                  value: "yes"
                  scope: instance
            trigger:
              event_type: external.webhook
              predicate: 'event.payload.source_id == "github"'
        """).lstrip())
        await stack["wfr"].register_workflow_from_file(str(path))
        # Mount the webhook receiver on a TestClient.
        registry = WebhookRegistry()
        registry.register(WebhookSourceConfig(
            source_id="github",
            instance_id="inst_a",
            bearer_token="t",
        ))
        app = FastAPI()
        register_routes(app, registry)
        client = TestClient(app)
        resp = client.post(
            "/webhooks/github",
            json={"action": "push"},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        await event_stream.flush_now()
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "wh_seen") in stack["state"],
        )
        assert ok, "webhook → workflow chain did not complete"
        assert stack["state"][("instance", "inst_a", "wh_seen")] == "yes"


# ===========================================================================
# Approval gate happy path
# ===========================================================================


class TestApprovalGateEndToEnd:
    async def test_gate_pauses_then_resumes_on_approval(self, stack):
        path = stack["tmp_path"] / "gated.workflow.yaml"
        path.write_text(textwrap.dedent("""
            workflow_id: gated-demo
            instance_id: inst_a
            instance_local: true
            name: gated demo
            version: "1.0"
            owner: owner
            bounds:
              iteration_count: 1
              wall_time_seconds: 60
            verifier:
              flavor: deterministic
              check: ok
            approval_gates:
              - gate_name: g1
                pause_reason: confirm
                approval_event_type: user.approval
                approval_event_predicate:
                  op: actor_eq
                  value: owner
                timeout_seconds: 5
                bound_behavior_on_timeout: abort_workflow
            action_sequence:
              - action_type: mark_state
                parameters:
                  key: pre_gate
                  value: 1
                  scope: instance
                gate_ref: g1
              - action_type: mark_state
                parameters:
                  key: post_gate
                  value: 2
                  scope: instance
            trigger:
              event_type: cc.batch.report
              predicate:
                op: exists
                path: event_id
        """).lstrip())
        await stack["wfr"].register_workflow_from_file(str(path))
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        # Pre-gate action runs.
        await _wait_for(
            lambda: ("instance", "inst_a", "pre_gate") in stack["state"],
        )
        # Engine is now paused waiting for approval. Read the
        # engine-minted nonce + execution_id and emit a valid
        # approval (WLP-GATE-SCOPING C1: nonce binding required).
        execs = await stack["engine"].list_executions("inst_a")
        gated = next(e for e in execs if e.gate_nonce)
        await event_stream.emit(
            "inst_a", "user.approval",
            {"execution_id": gated.execution_id,
             "gate_nonce": gated.gate_nonce},
            member_id="owner",
        )
        await event_stream.flush_now()
        # Post-gate action runs.
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "post_gate") in stack["state"],
        )
        assert ok, "engine did not resume after approval event"


# ===========================================================================
# Notion-leak whole-spec pin
# ===========================================================================


class TestNotionLeakWholeSpec:
    """Spec invariant: the workflow primitive is independent of Notion.
    Direct Notion *integration* references (URLs, tool namespaces,
    Python imports) are allowed ONLY inside the NotionAgentInbox
    class body in agent_inbox.py. Prose-level mentions of "Notion"
    in docstrings are fine — the pin targets actual integration
    patterns, not narrative references."""

    # Patterns that indicate an actual Notion *integration* (vs. prose):
    #   - Notion URLs (notion.so / notion.com)
    #   - MCP Notion tool prefix
    #   - Python import / from-import naming a notion module
    #   - notion-tool / notion_tool tool-id patterns
    INTEGRATION_PATTERNS = [
        re.compile(r"notion\.(so|com)", re.IGNORECASE),
        re.compile(r"mcp__[\w]*[Nn]otion[\w]*"),
        re.compile(r"^\s*(from|import)\s+\S*notion", re.IGNORECASE | re.MULTILINE),
        re.compile(r"notion[-_]tool", re.IGNORECASE),
    ]

    def _notion_class_block(self, path: Path) -> tuple[int, int] | None:
        if path.name != "agent_inbox.py":
            return None
        text = path.read_text()
        lines = text.splitlines()
        start = None
        for idx, line in enumerate(lines):
            if line.startswith("class NotionAgentInbox"):
                start = idx
                break
        if start is None:
            return None
        end = len(lines)
        for idx in range(start + 1, len(lines)):
            line = lines[idx]
            if line and not line.startswith((" ", "\t", "#")):
                end = idx
                break
        return (start, end)

    def test_no_notion_integrations_outside_NotionAgentInbox(self):
        roots = [
            Path(__file__).resolve().parents[1] / "kernos" / "kernel" / "workflows",
            Path(__file__).resolve().parents[1] / "kernos" / "kernel" / "webhooks",
        ]
        offenders: list[str] = []
        for root in roots:
            for path in sorted(root.rglob("*.py")):
                text = path.read_text()
                lines = text.splitlines()
                allowed_block = self._notion_class_block(path)
                for idx, line in enumerate(lines):
                    if allowed_block and allowed_block[0] <= idx < allowed_block[1]:
                        continue
                    for pattern in self.INTEGRATION_PATTERNS:
                        if pattern.search(line):
                            offenders.append(
                                f"{path.name}:{idx + 1}: {line.strip()}"
                            )
                            break
        assert not offenders, (
            "Notion integration references found outside NotionAgentInbox:\n"
            + "\n".join(offenders)
        )
