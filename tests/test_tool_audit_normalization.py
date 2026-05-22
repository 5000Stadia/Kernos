"""TOOL-AUDIT-NORMALIZATION-V1 (2026-05-22) acceptance tests.

Pins the canonical audit-entry contract:
  - One ToolInvocationAuditEntry per dispatch (success + failure
    paths both produce exactly one).
  - Carries audit_entry_id for join with tool.called / tool.result.
  - Replaces legacy "tool_call_succeeded" / "tool_call_failed" dict
    shapes.
  - Workspace's _emit_audit suppresses when audit_entry_id is set
    (preparing for future source-aware routing landing).
  - tool.called + tool.result events carry audit_entry_id.

Audit is operator-facing only — no agent-prose layer (per
[[agent-facing-natural-simplicity]] this is the "no agent layer
needed" case).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.enactment.dispatcher import (
    ToolExecutionInputs, ToolExecutionResult,
)
from kernos.kernel.gate import GateResult
from kernos.kernel.integration.live_wiring import (
    LiveIntegrationDispatcher,
)
from kernos.kernel.reasoning import ReasoningRequest


def _gate(classification: str = "read", allowed: bool = True) -> MagicMock:
    g = MagicMock()
    g.classify_tool_effect.return_value = classification
    g.evaluate = AsyncMock(return_value=GateResult(
        allowed=allowed, reason="approved", method="model_check",
    ))
    g._catalog = None
    g._registry = None
    g._events = None
    return g


def _inputs(**overrides) -> ToolExecutionInputs:
    defaults = dict(
        tool_id="list-events", arguments={}, operation_name="list-events",
        instance_id="t1", member_id="m1", space_id="s1", turn_id="turn-x",
    )
    defaults.update(overrides)
    return ToolExecutionInputs(**defaults)


# ============================================================
# AC1 — Success: exactly one audit entry
# ============================================================


@pytest.mark.asyncio
async def test_ac1_success_produces_one_canonical_entry():
    audits: list[dict] = []
    async def audit_emit(e):
        audits.append(e)
    dispatcher = LiveIntegrationDispatcher(
        execute_tool=AsyncMock(return_value="ok"),
        gate=_gate("read"),
        request_factory=lambda t, a, i: MagicMock(),
        audit_emitter=audit_emit,
    )
    await dispatcher("read-tool", {}, _inputs())
    assert len(audits) == 1
    entry = audits[0]
    assert entry["type"] == "tool_call"  # canonical, not legacy
    assert entry["success"] is True
    assert entry["audit_entry_id"]  # non-empty
    assert entry["tool_name"] == "read-tool"


# ============================================================
# AC3 — Failure: one canonical entry with success=False + error
# ============================================================


@pytest.mark.asyncio
async def test_ac3_failure_produces_canonical_entry_with_error():
    audits: list[dict] = []
    async def audit_emit(e):
        audits.append(e)
    dispatcher = LiveIntegrationDispatcher(
        execute_tool=AsyncMock(side_effect=RuntimeError("upstream broke")),
        gate=_gate("read"),
        request_factory=lambda t, a, i: MagicMock(),
        audit_emitter=audit_emit,
    )
    await dispatcher("read-tool", {}, _inputs())
    assert len(audits) == 1
    entry = audits[0]
    assert entry["type"] == "tool_call"
    assert entry["success"] is False
    assert "upstream broke" in entry["error"]
    assert entry["audit_entry_id"]


# ============================================================
# AC5 — Canonical entry carries audit_category (from classification
#       when catalog metadata doesn't provide it)
# ============================================================


@pytest.mark.asyncio
async def test_ac5_audit_category_populated_from_classification():
    audits: list[dict] = []
    async def audit_emit(e):
        audits.append(e)
    dispatcher = LiveIntegrationDispatcher(
        execute_tool=AsyncMock(return_value="ok"),
        gate=_gate("soft_write"),
        request_factory=lambda t, a, i: MagicMock(),
        audit_emitter=audit_emit,
    )
    await dispatcher("write_file", {"name": "x"}, _inputs())
    assert audits[0]["audit_category"] == "soft_write"


# ============================================================
# AC6 — service_id pulled from catalog metadata when present
# ============================================================


@pytest.mark.asyncio
async def test_ac6_service_id_from_catalog_metadata():
    audits: list[dict] = []
    async def audit_emit(e):
        audits.append(e)
    # Stub a catalog whose get_metadata returns a service_id
    catalog = MagicMock()
    catalog.get_metadata.return_value = {
        "name": "weather",
        "source": "workspace",
        "service_id": "open_meteo",
    }
    gate = _gate("read")
    gate._catalog = catalog
    dispatcher = LiveIntegrationDispatcher(
        execute_tool=AsyncMock(return_value="sunny"),
        gate=gate,
        request_factory=lambda t, a, i: MagicMock(),
        audit_emitter=audit_emit,
    )
    await dispatcher("weather", {"location": "berkeley"}, _inputs())
    assert audits[0]["service_id"] == "open_meteo"


# ============================================================
# AC8 — payload_digest populated (SHA-256 hex)
# ============================================================


@pytest.mark.asyncio
async def test_ac8_payload_digest_populated():
    audits: list[dict] = []
    async def audit_emit(e):
        audits.append(e)
    dispatcher = LiveIntegrationDispatcher(
        execute_tool=AsyncMock(return_value="ok"),
        gate=_gate("read"),
        request_factory=lambda t, a, i: MagicMock(),
        audit_emitter=audit_emit,
    )
    await dispatcher("read-tool", {"q": "test"}, _inputs())
    digest = audits[0]["payload_digest"]
    assert len(digest) == 64  # SHA-256 hex length
    # All hex chars
    assert all(c in "0123456789abcdef" for c in digest)


# ============================================================
# ACs 9-11 — tool.called + tool.result events still fire + include
#            audit_entry_id
# ============================================================


@pytest.mark.asyncio
async def test_ac9_10_11_events_still_fire_with_audit_entry_id():
    events: list[dict] = []
    audits: list[dict] = []
    async def event_emit(e):
        events.append(e)
    async def audit_emit(e):
        audits.append(e)
    dispatcher = LiveIntegrationDispatcher(
        execute_tool=AsyncMock(return_value="ok"),
        gate=_gate("read"),
        request_factory=lambda t, a, i: MagicMock(),
        event_emitter=event_emit,
        audit_emitter=audit_emit,
    )
    await dispatcher("read-tool", {}, _inputs())
    called = [e for e in events if e["type"] == "tool.called"]
    result = [e for e in events if e["type"] == "tool.result"]
    assert len(called) == 1
    assert len(result) == 1
    # AC11: both events carry the audit_entry_id (matches audit)
    audit_id = audits[0]["audit_entry_id"]
    assert called[0]["audit_entry_id"] == audit_id
    assert result[0]["audit_entry_id"] == audit_id


# ============================================================
# AC12 — legacy "tool_call_succeeded" / "tool_call_failed" types
#        are NOT emitted on the canonical path
# ============================================================


@pytest.mark.asyncio
async def test_ac12_legacy_shapes_no_longer_emitted_on_success():
    audits: list[dict] = []
    async def audit_emit(e):
        audits.append(e)
    dispatcher = LiveIntegrationDispatcher(
        execute_tool=AsyncMock(return_value="ok"),
        gate=_gate("read"),
        request_factory=lambda t, a, i: MagicMock(),
        audit_emitter=audit_emit,
    )
    await dispatcher("read-tool", {}, _inputs())
    # No entry has the legacy type
    for entry in audits:
        assert entry.get("type") != "tool_call_succeeded"
        assert entry.get("type") != "tool_call_failed"


# ============================================================
# AC13 — workspace _emit_audit suppresses when audit_entry_id set
# ============================================================


class TestWorkspaceAuditSuppression:
    """Direct test of WorkspaceManager._emit_audit suppression logic
    (no need to spin up the full subprocess dispatch — the suppression
    check is the load-bearing piece)."""

    @pytest.mark.asyncio
    async def test_emit_audit_writes_when_no_entry_id(self, tmp_path):
        from kernos.kernel.workspace import WorkspaceManager
        from kernos.kernel.tool_catalog import ToolCatalog
        audit_store = MagicMock()
        audit_store.log = AsyncMock()
        ws = WorkspaceManager(
            data_dir=str(tmp_path),
            catalog=ToolCatalog(),
            audit_store=audit_store,
        )
        descriptor = MagicMock()
        descriptor.name = "x"
        descriptor.service_id = "svc"
        descriptor.authority = ()
        descriptor.audit_category = "test"
        await ws._emit_audit(
            instance_id="t1", member_id="m1", space_id="s1",
            descriptor=descriptor, operation="op",
            payload={}, success=True,
            audit_entry_id="",  # empty → legacy path, writes
        )
        audit_store.log.assert_called_once()

    @pytest.mark.asyncio
    async def test_emit_audit_skips_when_entry_id_set(self, tmp_path):
        from kernos.kernel.workspace import WorkspaceManager
        from kernos.kernel.tool_catalog import ToolCatalog
        audit_store = MagicMock()
        audit_store.log = AsyncMock()
        ws = WorkspaceManager(
            data_dir=str(tmp_path),
            catalog=ToolCatalog(),
            audit_store=audit_store,
        )
        descriptor = MagicMock()
        descriptor.name = "x"
        await ws._emit_audit(
            instance_id="t1", member_id="m1", space_id="s1",
            descriptor=descriptor, operation="op",
            payload={}, success=True,
            audit_entry_id="canonical_id_123",  # non-empty → skip
        )
        audit_store.log.assert_not_called()


# ============================================================
# AC14 — ToolExecutionInputs + ReasoningRequest carry audit_entry_id
# ============================================================


def test_ac14_tool_execution_inputs_carries_audit_entry_id():
    inputs = ToolExecutionInputs(
        tool_id="x", arguments={}, operation_name="x",
        instance_id="t", member_id="m", space_id="s", turn_id="t",
    )
    assert inputs.audit_entry_id == ""  # default
    inputs2 = ToolExecutionInputs(
        tool_id="x", arguments={}, operation_name="x",
        instance_id="t", member_id="m", space_id="s", turn_id="t",
        audit_entry_id="abc123",
    )
    assert inputs2.audit_entry_id == "abc123"


def test_ac14_reasoning_request_carries_audit_entry_id():
    req = ReasoningRequest(
        instance_id="t1", conversation_id="c1",
        system_prompt="x", messages=[], tools=[],
        model="m", trigger="t",
    )
    assert req.audit_entry_id == ""
    req.audit_entry_id = "set_by_dispatcher"
    assert req.audit_entry_id == "set_by_dispatcher"
