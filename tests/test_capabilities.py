"""Tests for the capability source-of-truth module.

CLEANUP-BATCH-V1 items 5 + 9. Verifies the in-code capability list
renders correctly to both Markdown (README) and plain text
(/capabilities slash command), and that core invariants hold so the
list doesn't silently drift.
"""
from __future__ import annotations

import re

from kernos.kernel.capabilities import (
    CAPABILITIES,
    CapabilitySpec,
    CapabilityStatus,
    render_markdown_table,
    render_status_text,
)


class TestCapabilityList:
    def test_list_is_non_empty(self):
        assert len(CAPABILITIES) > 0

    def test_all_entries_are_capability_specs(self):
        for cap in CAPABILITIES:
            assert isinstance(cap, CapabilitySpec)

    def test_all_statuses_are_valid_enum(self):
        for cap in CAPABILITIES:
            assert isinstance(cap.status, CapabilityStatus)

    def test_names_are_unique(self):
        names = [cap.name for cap in CAPABILITIES]
        assert len(names) == len(set(names)), (
            f"duplicate capability names detected: {names}"
        )

    def test_no_empty_names_or_surfaces(self):
        for cap in CAPABILITIES:
            assert cap.name.strip(), "empty capability name"
            assert cap.surface_area.strip(), (
                f"capability {cap.name!r} has empty surface_area"
            )


class TestMarkdownRender:
    def test_renders_a_table_header(self):
        text = render_markdown_table()
        assert text.splitlines()[0].startswith("| Capability ")
        assert text.splitlines()[1].startswith("| --- ")

    def test_every_capability_appears_in_table(self):
        text = render_markdown_table()
        for cap in CAPABILITIES:
            assert cap.name in text
            assert cap.status.value in text

    def test_no_pipe_chars_in_capability_fields_break_the_table(self):
        # If anyone adds a `|` to a name / surface / notes field it
        # would break Markdown table rendering.
        for cap in CAPABILITIES:
            for field in (cap.name, cap.surface_area, cap.notes):
                assert "|" not in field, (
                    f"capability field contains literal pipe: {field!r}"
                )


class TestStatusTextRender:
    def test_renders_grouped_by_status(self):
        text = render_status_text()
        # All statuses present in the list should have a header.
        statuses_in_list = {cap.status for cap in CAPABILITIES}
        for status in statuses_in_list:
            assert f"**{status.value}**" in text

    def test_every_capability_appears(self):
        text = render_status_text()
        for cap in CAPABILITIES:
            assert cap.name in text
            assert cap.surface_area in text


class TestHandleCapabilities:
    def test_handler_method_renders_status_text(self):
        from kernos.messages.handler import MessageHandler
        out = MessageHandler._handle_capabilities()
        assert "Kernos capability matrix" in out
        # Source path mentioned so operators know where to edit.
        assert "kernos/kernel/capabilities.py" in out
        # At least one known capability surfaced.
        assert "External-agent consultation" in out
