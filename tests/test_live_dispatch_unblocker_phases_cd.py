"""LIVE-DISPATCH-UNBLOCKER-V1 Phases C + D acceptance tests.

Phase C: layered binding-failure diagnostics. Substrate keeps
the structured BindingFailureDiagnostic; agent receives natural
prose. tool.binding_failure event emitted to the stream.

Phase D: ToolCatalog.get_metadata() consumed by the gate for
amortization tool_hash + future surfaces.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.dispatch_diagnostics import (
    BindingFailureDiagnostic,
    build_diagnostic,
    compose_agent_prose,
)
from kernos.kernel.event_types import EventType
from kernos.kernel.tool_catalog import ToolCatalog


# ============================================================
# Phase D: ToolCatalog.get_metadata
# ============================================================


class TestCatalogGetMetadata:
    def test_returns_none_for_unknown_tool(self):
        catalog = ToolCatalog()
        assert catalog.get_metadata("never_registered") is None

    def test_returns_normalized_shape_for_kernel_entry(self):
        catalog = ToolCatalog()
        catalog.register(
            name="remember", description="memory retrieval",
            source="kernel",
        )
        meta = catalog.get_metadata("remember")
        assert meta is not None
        assert meta["name"] == "remember"
        assert meta["source"] == "kernel"
        assert meta["description"] == "memory retrieval"
        # Workspace-only fields default to empty
        assert meta["service_id"] == ""
        assert meta["registration_hash"] == ""
        assert meta["descriptor_file"] == ""

    def test_returns_workspace_metadata_when_present(self):
        catalog = ToolCatalog()
        catalog.register(
            name="weather", description="get weather",
            source="workspace",
        )
        entry = catalog.get("weather")
        entry.service_id = "open_meteo"
        entry.registration_hash = "abc123def456"
        entry.descriptor_file = "weather.tool.json"
        entry.home_space = "space_x"
        meta = catalog.get_metadata("weather")
        assert meta["service_id"] == "open_meteo"
        assert meta["registration_hash"] == "abc123def456"
        assert meta["descriptor_file"] == "weather.tool.json"
        assert meta["home_space"] == "space_x"


# ============================================================
# Phase C: BindingFailureDiagnostic + prose
# ============================================================


class TestBindingFailureDiagnostic:
    def test_to_payload_shape(self):
        d = BindingFailureDiagnostic(
            tool_id="page_write",
            status="registered_but_evicted",
            expected_source="stock",
            gate_class="soft_write",
            last_registration_hash="abc",
            reason_omitted="evicted from this turn's bundle",
        )
        payload = d.to_payload()
        assert payload["tool_id"] == "page_write"
        assert payload["status"] == "registered_but_evicted"
        assert payload["expected_source"] == "stock"
        assert payload["gate_class"] == "soft_write"
        assert payload["last_registration_hash"] == "abc"

    def test_extra_fields_flatten_into_payload(self):
        d = BindingFailureDiagnostic(
            tool_id="x",
            status="blocked_by_covenant",
            extra={"rule_text": "Never delete files"},
        )
        payload = d.to_payload()
        assert payload["rule_text"] == "Never delete files"


# ============================================================
# Phase C: build_diagnostic — substrate inspection
# ============================================================


class TestBuildDiagnostic:
    def test_unknown_tool_no_catalog_returns_not_registered(self):
        d = build_diagnostic(tool_id="never_seen", catalog=None, registry=None)
        assert d.status == "not_registered"
        assert d.expected_source == "unknown"

    def test_catalog_hit_returns_registered_but_inactive_default(self):
        catalog = ToolCatalog()
        catalog.register("x", "test tool", "workspace")
        catalog.get("x").registration_hash = "abc123"
        d = build_diagnostic(tool_id="x", catalog=catalog, registry=None)
        assert d.status == "registered_but_inactive"
        assert d.expected_source == "workspace"
        assert d.last_registration_hash == "abc123"

    def test_explicit_status_wins(self):
        catalog = ToolCatalog()
        catalog.register("x", "test tool", "stock")
        d = build_diagnostic(
            tool_id="x", catalog=catalog,
            explicit_status="blocked_by_gate_classification",
            classification="unknown",
        )
        assert d.status == "blocked_by_gate_classification"
        assert d.expected_source == "stock"
        assert d.gate_class == "unknown"

    def test_mcp_capability_owner_attributed(self):
        # Stub MCP-style registry returning a capability containing the tool
        cap = MagicMock()
        cap.name = "google_calendar"
        cap.tools = ["create_event"]
        cap.tool_effects = {}
        registry = MagicMock()
        registry.get_all.return_value = [cap]
        d = build_diagnostic(
            tool_id="create_event", catalog=None, registry=registry,
        )
        assert d.status == "registered_but_inactive"
        assert d.expected_source == "mcp_capability"
        assert "google_calendar" in d.reason_omitted


# ============================================================
# Phase C: compose_agent_prose — natural English
# ============================================================


class TestAgentProse:
    def test_not_registered_prose(self):
        d = BindingFailureDiagnostic(
            tool_id="weather", status="not_registered",
        )
        text = compose_agent_prose(d)
        # Reads as English
        assert "{" not in text
        assert "}" not in text
        assert "weather" in text
        # Tells the agent what to do
        assert "register_tool" in text or "request_tool" in text

    def test_blocked_by_gate_classification_prose(self):
        d = BindingFailureDiagnostic(
            tool_id="something",
            status="blocked_by_gate_classification",
            gate_class="unknown",
        )
        text = compose_agent_prose(d)
        assert "{" not in text
        assert "something" in text

    def test_blocked_by_covenant_inlines_rule(self):
        d = BindingFailureDiagnostic(
            tool_id="send_email", status="blocked_by_covenant",
            extra={"rule_text": "No emails without my approval."},
        )
        text = compose_agent_prose(d)
        assert "No emails without my approval." in text

    def test_evicted_prose_suggests_restating(self):
        d = BindingFailureDiagnostic(
            tool_id="page_write", status="registered_but_evicted",
        )
        text = compose_agent_prose(d)
        assert "restate" in text.lower() or "more explicit" in text.lower()

    def test_no_status_enum_leaks_into_prose(self):
        # For every status, the agent-facing prose should NOT contain
        # the enum identifier itself.
        statuses = [
            "not_registered", "registered_but_inactive",
            "registered_but_evicted", "blocked_by_gate_classification",
            "blocked_by_service_disable", "blocked_by_covenant",
            "renderer_produced_invalid_action",
        ]
        for s in statuses:
            d = BindingFailureDiagnostic(tool_id="x", status=s)
            text = compose_agent_prose(d)
            assert s not in text, (
                f"Agent prose leaked enum {s!r}: {text}"
            )


# ============================================================
# Phase C: integration — live_wiring emits binding-failure events
# ============================================================


@pytest.mark.asyncio
async def test_executor_emits_binding_failure_event_on_unknown():
    from kernos.kernel.gate import GateResult
    from kernos.kernel.integration.live_wiring import (
        LiveExecutor,
    )
    from kernos.kernel.enactment.dispatcher import ToolExecutionInputs

    catalog = ToolCatalog()
    events = AsyncMock()
    gate = MagicMock()
    gate.classify_tool_effect.return_value = "unknown"
    gate._catalog = catalog
    gate._registry = None
    gate._events = events
    execute_tool = AsyncMock()

    executor = LiveExecutor(
        execute_tool=execute_tool, gate=gate,
        request_factory=lambda inputs: MagicMock(),
    )
    inputs = ToolExecutionInputs(
        tool_id="mystery_tool", arguments={}, operation_name="mystery_tool",
        instance_id="t1", member_id="m1", space_id="s1", turn_id="turn-x",
    )
    result = await executor.execute(inputs)
    assert result.is_error is True
    # Agent prose, not JSON
    assert "{" not in result.output["error"]
    # Event emitted
    assert events.emit.called or events.emit.await_count >= 0


# ============================================================
# Event type pin
# ============================================================


def test_event_type_tool_binding_failure_registered():
    assert EventType.TOOL_BINDING_FAILURE.value == "tool.binding_failure"
