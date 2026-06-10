"""TOOL-ARG-REPAIR-V1 guidance side — compact signature presentation.

Pins the two presentation surfaces (Codex consult 019eaf8e): the
``## TOOL CALL SIGNATURES`` developer-message endcap and the
SIGNATURE/EXAMPLE description prefix — both generated from the same
schemas the provider sends, rendered under provider wire names.
"""
from __future__ import annotations

from kernos.kernel.tool_signatures import (
    build_signature,
    build_signature_block,
    signature_prefix,
)


def _schedule_tool() -> dict:
    return {
        "name": "manage_schedule",
        "description": "Manage scheduled actions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "create", "update", "pause", "resume", "remove"],
                },
                "description": {"type": "string"},
                "trigger_id": {"type": "string"},
            },
            "required": ["action"],
        },
    }


class TestBuildSignature:
    def test_required_first_optional_marked_enums_inline(self):
        sig = build_signature(_schedule_tool())
        assert sig.startswith("manage_schedule(action: ")
        assert '"list"|"create"' in sig          # enum inline
        assert "description?: str" in sig        # optional marked
        assert sig.index("action") < sig.index("description?")  # required first

    def test_wire_name_used_when_given(self):
        sig = build_signature(_schedule_tool(), "schedule__manage_schedule")
        assert sig.startswith("schedule__manage_schedule(")

    def test_degrades_not_raises_on_garbage_schema(self):
        assert build_signature({"name": "x", "input_schema": "garbage"}) == "x(...)"
        assert build_signature({"name": "x"}) == "x()"

    def test_long_enum_summarized(self):
        tool = {"name": "t", "input_schema": {"type": "object", "properties": {
            "v": {"enum": list("abcdefghij")}}, "required": ["v"]}}
        sig = build_signature(tool)
        assert "more" in sig and sig.count('"') <= 12


class TestSignaturePrefix:
    def test_high_fumble_tool_gets_example(self):
        prefix = signature_prefix(_schedule_tool(), "schedule__manage_schedule")
        assert prefix.startswith("SIGNATURE: schedule__manage_schedule(")
        assert "EXAMPLE:" in prefix
        assert "due_at" in prefix  # the anti-pattern is named explicitly

    def test_ordinary_tool_signature_only(self):
        prefix = signature_prefix({"name": "read_file", "input_schema": {
            "type": "object", "properties": {"path": {"type": "string"}},
            "required": ["path"]}})
        assert prefix.startswith("SIGNATURE: read_file(path: str)")
        assert "EXAMPLE:" not in prefix


class TestSignatureBlock:
    def test_endcap_renders_wire_names_and_examples(self):
        tools = [_schedule_tool(), {"name": "consult", "input_schema": {
            "type": "object", "properties": {
                "harness": {"type": "string", "enum": ["claude_code", "codex", "gemini"]},
                "question": {"type": "string"}},
            "required": ["harness", "question"]}}]
        skin = {"manage_schedule": "schedule__manage_schedule",
                "consult": "external__consult"}
        block = build_signature_block(tools, skin=skin)
        assert block.startswith("## TOOL CALL SIGNATURES")
        assert "- schedule__manage_schedule(action:" in block
        assert '- external__consult(harness: "claude_code"|"codex"|"gemini"' in block
        # The live failure modes are named in the examples.
        assert "synchronous" not in block  # no stale references
        assert "agent enum" in block       # consult example's key insight

    def test_empty_tools_yields_empty_string(self):
        assert build_signature_block([]) == ""

    def test_assemble_endcap_lands_at_dynamic_tail(self):
        # The wiring contract: the block composes from ctx.tools with the
        # same skin source the provider uses; here just pin the block is a
        # single self-contained markdown section safe to append last.
        block = build_signature_block([_schedule_tool()])
        assert block.count("##") == 1
        assert "this block wins" in block


class TestProviderDescriptionPrefix:
    def test_translate_tools_leads_with_signature(self):
        from kernos.providers.codex_provider import OpenAICodexProvider
        out = OpenAICodexProvider._translate_tools(
            [_schedule_tool()], skin={"manage_schedule": "schedule__manage_schedule"},
        )
        assert len(out) == 1
        desc = out[0]["description"]
        assert desc.startswith("SIGNATURE: schedule__manage_schedule(")
        assert "Manage scheduled actions." in desc  # original prose preserved
        assert out[0]["strict"] is None             # load-bearing invariant intact
        assert out[0]["name"] == "schedule__manage_schedule"
