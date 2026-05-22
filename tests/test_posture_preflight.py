"""POSTURE-PREFLIGHT-V1 (2026-05-22) acceptance tests.

Covers spec ACs 1-15. AC16 = no regressions on existing
surfacing tests (handled by the broader sweep).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from kernos.kernel.inspect_tool_availability import (
    INSPECT_TOOL_AVAILABILITY_TOOL,
    _suggestion_for,
    handle_inspect_tool_availability_tool,
)
from kernos.kernel.tool_catalog import ALWAYS_PINNED
from kernos.messages.surfacing_snapshot import (
    SurfacingSnapshot,
    ToolSurfacingEntry,
)


# ============================================================
# Helpers
# ============================================================


def _entry(
    name: str, tier: str, source: str = "kernel", reason: str = "",
) -> ToolSurfacingEntry:
    return ToolSurfacingEntry(
        name=name, tier=tier, source=source, reason_if_absent=reason,
    )


def _handler_with_snapshot(entries: list[ToolSurfacingEntry]) -> SimpleNamespace:
    snap = SurfacingSnapshot(
        entries={e.name: e for e in entries},
        turn_id="turn_test",
    )
    return SimpleNamespace(_surfacing_snapshot=snap)


# ============================================================
# ACs 1-3: response shape for pinned / active / absent
# ============================================================


class TestResponseShape:
    def test_ac1_pinned_tool_available(self):
        handler = _handler_with_snapshot([
            _entry("write_file", "pinned", "kernel"),
        ])
        result = handle_inspect_tool_availability_tool(
            handler=handler, tool_input={"tool_name": "write_file"},
        )
        assert result["ok"] is True
        assert result["available"] is True
        assert result["tier"] == "pinned"
        assert result["source"] == "kernel"
        assert result["reason_if_absent"] == ""
        assert result["request_tool_suggestion"] == ""

    def test_ac2_active_tool_available(self):
        handler = _handler_with_snapshot([
            _entry("page_write", "active", "kernel"),
        ])
        result = handle_inspect_tool_availability_tool(
            handler=handler, tool_input={"tool_name": "page_write"},
        )
        assert result["available"] is True
        assert result["tier"] == "active"

    def test_ac2_evicted_tool_unavailable(self):
        handler = _handler_with_snapshot([
            _entry("page_write", "absent", "kernel", "evicted_for_budget"),
        ])
        result = handle_inspect_tool_availability_tool(
            handler=handler, tool_input={"tool_name": "page_write"},
        )
        assert result["available"] is False
        assert result["tier"] == "absent"
        assert result["reason_if_absent"] == "evicted_for_budget"
        assert "KERNOS_TOOL_TOKEN_BUDGET" in result["request_tool_suggestion"]

    def test_ac3_never_registered_synthesizes_not_registered(self):
        handler = _handler_with_snapshot([])
        result = handle_inspect_tool_availability_tool(
            handler=handler, tool_input={"tool_name": "never_registered_tool"},
        )
        assert result["ok"] is True
        assert result["available"] is False
        assert result["tier"] == "absent"
        assert result["source"] == "unknown"
        assert result["reason_if_absent"] == "not_registered"


# ============================================================
# ACs 4-5: input validation
# ============================================================


class TestInputValidation:
    def test_ac4_empty_tool_name_errors(self):
        handler = _handler_with_snapshot([])
        result = handle_inspect_tool_availability_tool(
            handler=handler, tool_input={"tool_name": ""},
        )
        assert result["ok"] is False
        assert "tool_name" in result["error"]

    def test_ac4_missing_tool_name_errors(self):
        handler = _handler_with_snapshot([])
        result = handle_inspect_tool_availability_tool(
            handler=handler, tool_input={},
        )
        assert result["ok"] is False
        assert "tool_name" in result["error"]

    def test_ac5_no_snapshot_errors(self):
        handler = SimpleNamespace()  # no _surfacing_snapshot attr
        result = handle_inspect_tool_availability_tool(
            handler=handler, tool_input={"tool_name": "write_file"},
        )
        assert result["ok"] is False
        assert "snapshot" in result["error"].lower()

    def test_none_tool_input_handled(self):
        handler = _handler_with_snapshot([])
        result = handle_inspect_tool_availability_tool(
            handler=handler, tool_input=None,  # type: ignore[arg-type]
        )
        assert result["ok"] is False


# ============================================================
# ACs 6-8: source-aware suggestions
# ============================================================


class TestSuggestionRecipes:
    def test_ac6_mcp_capability_suggests_request_tool(self):
        entry = _entry("get_calendar", "absent", "mcp_capability")
        suggestion = _suggestion_for(entry)
        assert "request_tool" in suggestion
        assert "capability_name" in suggestion
        assert "get_calendar" in suggestion

    def test_ac7_kernel_evicted_mentions_budget(self):
        entry = _entry(
            "page_write", "absent", "kernel", "evicted_for_budget",
        )
        suggestion = _suggestion_for(entry)
        assert "KERNOS_TOOL_TOKEN_BUDGET" in suggestion
        assert "re-rank" in suggestion or "rerank" in suggestion

    def test_ac8_stock_source_mentions_intent_ranker(self):
        entry = _entry("canvas_create", "absent", "stock")
        suggestion = _suggestion_for(entry)
        assert "intent" in suggestion.lower()

    def test_workspace_source_documented(self):
        entry = _entry("ws_tool", "absent", "workspace")
        suggestion = _suggestion_for(entry)
        assert "workspace" in suggestion.lower()

    def test_disabled_service_suggestion(self):
        entry = _entry(
            "calendar_create", "absent", "unknown", "disabled_service",
        )
        suggestion = _suggestion_for(entry)
        assert "disabled" in suggestion.lower()

    def test_pinned_tool_no_suggestion(self):
        entry = _entry("write_file", "pinned", "kernel")
        assert _suggestion_for(entry) == ""

    def test_active_tool_no_suggestion(self):
        entry = _entry("page_write", "active", "kernel")
        assert _suggestion_for(entry) == ""


# ============================================================
# ACs 9-10: pin + classification contracts
# ============================================================


class TestPinAndClassification:
    def test_ac9_pinned_in_always_pinned(self):
        assert "inspect_tool_availability" in ALWAYS_PINNED

    def test_ac10_gate_classifies_as_read(self):
        """Spec contract: inspect_tool_availability is read,
        not soft_write. Verify via direct gate construction."""
        from unittest.mock import AsyncMock, MagicMock

        from kernos.kernel.gate import DispatchGate

        gate = DispatchGate(
            reasoning_service=MagicMock(),
            registry=None,
            state=AsyncMock(),
            events=AsyncMock(),
        )
        assert gate.classify_tool_effect(
            "inspect_tool_availability", None, {"tool_name": "x"},
        ) == "read"


# ============================================================
# ACs 11-15: snapshot tier accuracy
# ============================================================


class TestSnapshotTierContract:
    def test_ac11_pinned_tier_lookup(self):
        snap = SurfacingSnapshot(entries={
            "write_file": _entry("write_file", "pinned", "kernel"),
        })
        assert snap.get("write_file").tier == "pinned"

    def test_ac12_active_tier_lookup(self):
        snap = SurfacingSnapshot(entries={
            "page_write": _entry("page_write", "active", "kernel"),
        })
        assert snap.get("page_write").tier == "active"

    def test_ac13_evicted_tier_with_reason(self):
        snap = SurfacingSnapshot(entries={
            "x": _entry("x", "absent", "kernel", "evicted_for_budget"),
        })
        e = snap.get("x")
        assert e.tier == "absent"
        assert e.reason_if_absent == "evicted_for_budget"

    def test_ac14_disabled_service_tier_with_reason(self):
        snap = SurfacingSnapshot(entries={
            "gcal_create": _entry(
                "gcal_create", "absent", "mcp_capability",
                "disabled_service",
            ),
        })
        e = snap.get("gcal_create")
        assert e.tier == "absent"
        assert e.reason_if_absent == "disabled_service"

    def test_ac15_catalog_but_unranked_tier(self):
        snap = SurfacingSnapshot(entries={
            "obscure_tool": _entry("obscure_tool", "catalog", "stock"),
        })
        e = snap.get("obscure_tool")
        assert e.tier == "catalog"
        assert e.reason_if_absent == ""


# ============================================================
# Schema sanity
# ============================================================


class TestSchema:
    def test_schema_has_required_fields(self):
        assert INSPECT_TOOL_AVAILABILITY_TOOL["name"] == "inspect_tool_availability"
        props = INSPECT_TOOL_AVAILABILITY_TOOL["input_schema"]["properties"]
        assert "tool_name" in props
        assert (
            "tool_name" in INSPECT_TOOL_AVAILABILITY_TOOL["input_schema"]["required"]
        )
