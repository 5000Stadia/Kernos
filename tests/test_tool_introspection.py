"""TOOL-INTROSPECTION-V1 (2026-05-22) acceptance tests.

Two surfaces, deliberately different shapes:
  - /tools slash command (operator): structured tabular text.
  - inspect_tools kernel tool (agent): natural-prose responses.

Same catalog metadata, two audiences. Tests pin both surfaces
+ the layered-design contract (agent prose absent of JSON
markers + status-enum-like tokens).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.tool_catalog import ALWAYS_PINNED, ToolCatalog
from kernos.kernel.tool_introspection import (
    INSPECT_TOOLS_TOOL,
    _area_for,
    compose_capability,
    compose_focus,
    compose_overview,
    handle_inspect_tools,
    render_operator_detail,
    render_operator_listing,
)


# ============================================================
# Capability area heuristic
# ============================================================


class TestAreaHeuristic:
    def test_memory_tools_grouped(self):
        assert _area_for("remember") == "memory"
        assert _area_for("note_this") == "memory"
        assert _area_for("request_reference") == "memory"

    def test_files_tools_grouped(self):
        assert _area_for("write_file") == "files"
        assert _area_for("read_file") == "files"

    def test_canvas_tools_grouped(self):
        assert _area_for("canvas_create") == "canvases"
        assert _area_for("page_write") == "canvases"

    def test_unknown_tool_falls_to_other(self):
        assert _area_for("never_heard_of_this") == "other"


# ============================================================
# AC8-9 — inspect_tools() overview prose
# ============================================================


def _populated_catalog() -> ToolCatalog:
    cat = ToolCatalog()
    cat.register("remember", "memory retrieval", "kernel")
    cat.register("write_file", "create files", "kernel")
    cat.register("read_file", "read files", "kernel")
    cat.register("send_to_channel", "send to channel", "kernel")
    cat.register("canvas_create", "make a canvas", "kernel")
    cat.register("weather_lookup", "get weather", "workspace")
    # Workspace tool with extra metadata
    entry = cat.get("weather_lookup")
    entry.service_id = "open_meteo"
    entry.registration_hash = "abc123def456"
    return cat


class TestOverviewProse:
    def test_ac8_returns_natural_prose_not_json(self):
        text = compose_overview(_populated_catalog())
        # No JSON markers
        assert "{" not in text
        assert "}" not in text
        # No status enum leak
        assert "status=" not in text

    def test_ac9_overview_mentions_count_and_areas(self):
        text = compose_overview(_populated_catalog())
        assert "tools" in text.lower()
        # Mentions multiple areas
        assert "memory" in text or "files" in text
        # Mentions workspace
        assert "weather_lookup" in text or "workspace" in text

    def test_empty_catalog_returns_coherent_message(self):
        text = compose_overview(ToolCatalog())
        assert "no tools" in text.lower() or "empty" in text.lower()
        # No crash, no empty string
        assert text


# ============================================================
# AC10 — inspect_tools(focus=...) focused prose
# ============================================================


class TestFocusProse:
    def test_ac10_known_tool_returns_description_and_source(self):
        text = compose_focus(_populated_catalog(), "weather_lookup")
        assert "weather_lookup" in text
        assert "workspace" in text
        assert "get weather" in text

    def test_focus_mentions_service_id_when_present(self):
        text = compose_focus(_populated_catalog(), "weather_lookup")
        assert "open_meteo" in text

    def test_ac11_unknown_tool_suggests_register_or_request(self):
        text = compose_focus(_populated_catalog(), "phantom_tool")
        assert "phantom_tool" in text
        # Suggestion mentions one of the two recovery paths
        assert "register_tool" in text or "request_tool" in text


# ============================================================
# AC12 — inspect_tools(capability=...) capability-scoped prose
# ============================================================


class TestCapabilityProse:
    def test_ac12_calendar_synonym_resolves(self):
        # "calendar" synonym → scheduling area
        cat = ToolCatalog()
        cat.register("manage_schedule", "schedule reminders", "kernel")
        text = compose_capability(cat, "calendar")
        assert "manage_schedule" in text

    def test_unknown_capability_returns_helpful_message(self):
        text = compose_capability(_populated_catalog(), "nonexistent_area")
        assert "no tools" in text.lower() or "overview" in text.lower()


# ============================================================
# AC13 — Prose is plain English (no JSON, no enum tokens, no codes)
# ============================================================


class TestProseDiscipline:
    def test_overview_absent_of_json_and_enums(self):
        text = compose_overview(_populated_catalog())
        for forbidden in ("{", "}", "status=", "tier=", "source:"):
            assert forbidden not in text

    def test_focus_absent_of_structured_data(self):
        text = compose_focus(_populated_catalog(), "weather_lookup")
        for forbidden in ("{", "}", "status="):
            assert forbidden not in text

    def test_capability_absent_of_structured_data(self):
        text = compose_capability(_populated_catalog(), "memory")
        for forbidden in ("{", "}", "status="):
            assert forbidden not in text


# ============================================================
# handle_inspect_tools dispatcher
# ============================================================


class TestEntryPoint:
    def test_no_args_returns_overview(self):
        text = handle_inspect_tools(catalog=_populated_catalog())
        assert "tools across" in text or "tools" in text

    def test_focus_routes_to_focus_composer(self):
        text = handle_inspect_tools(
            catalog=_populated_catalog(), focus="remember",
        )
        assert "remember" in text

    def test_capability_routes_to_capability_composer(self):
        text = handle_inspect_tools(
            catalog=_populated_catalog(), capability="memory",
        )
        assert "memory" in text.lower()

    def test_focus_takes_precedence_over_capability(self):
        text = handle_inspect_tools(
            catalog=_populated_catalog(),
            focus="weather_lookup",
            capability="files",
        )
        assert "weather_lookup" in text


# ============================================================
# AC14 — inspect_tools is in ALWAYS_PINNED
# ============================================================


def test_ac14_inspect_tools_in_always_pinned():
    assert "inspect_tools" in ALWAYS_PINNED


# ============================================================
# Schema sanity
# ============================================================


def test_inspect_tools_schema_fields():
    assert INSPECT_TOOLS_TOOL["name"] == "inspect_tools"
    props = INSPECT_TOOLS_TOOL["input_schema"]["properties"]
    assert "focus" in props
    assert "capability" in props


# ============================================================
# Operator-facing /tools listing
# ============================================================


class TestOperatorListing:
    def test_ac1_listing_groups_by_source(self):
        text = render_operator_listing(_populated_catalog())
        assert "kernel" in text
        assert "workspace" in text

    def test_ac4_filter_by_source(self):
        text = render_operator_listing(
            _populated_catalog(), filter_source="workspace",
        )
        assert "weather_lookup" in text
        assert "remember" not in text

    def test_ac3_detail_view_for_known_tool(self):
        text = render_operator_detail(
            _populated_catalog(), "weather_lookup",
        )
        # Operator gets structured tabular text
        assert "weather_lookup" in text
        assert "service_id" in text
        assert "open_meteo" in text
        assert "registration_hash" in text

    def test_ac3_detail_view_unknown_tool_returns_helpful_message(self):
        text = render_operator_detail(
            _populated_catalog(), "phantom",
        )
        assert "not found" in text.lower()
        assert "/tools" in text

    def test_ac5_classification_filter_returns_empty(self):
        """v1 doesn't store classification on entries; filter
        matches nothing rather than crashing."""
        text = render_operator_listing(
            _populated_catalog(), filter_classification="hard_write",
        )
        # Should return the "no tools match" message, not crash
        assert "no tools match" in text.lower() or text == ""

    def test_empty_catalog_returns_empty_message(self):
        text = render_operator_listing(ToolCatalog())
        assert "empty" in text.lower() or "no tools" in text.lower()
