"""CROSS_SPACE_REQUESTS_V1 — tool schema + awareness-preamble tests.

Covers:
  - request_space_action tool schema invariants (registered with
    the right shape; in _KERNEL_TOOLS + _KERNEL_TOOL_PATHS).
  - Target re-entry awareness block in assemble.py: query +
    rendering + bounded shape.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from kernos.kernel.cross_space.tool import (
    REQUEST_SPACE_ACTION_TOOL,
    build_request_from_tool_input,
)
from kernos.kernel.reasoning import ReasoningService


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------


class TestToolSchema:
    def test_tool_name_and_required_fields(self):
        assert REQUEST_SPACE_ACTION_TOOL["name"] == "request_space_action"
        schema = REQUEST_SPACE_ACTION_TOOL["input_schema"]
        assert schema["type"] == "object"
        assert set(schema["required"]) == {
            "target_space_id", "action_kind", "work_order",
        }

    def test_action_kind_enum_matches_whitelist(self):
        from kernos.kernel.cross_space import ALLOWED_ACTION_KINDS
        enum = set(REQUEST_SPACE_ACTION_TOOL["input_schema"][
            "properties"]["action_kind"]["enum"])
        assert enum == set(ALLOWED_ACTION_KINDS)


class TestKernelToolRegistration:
    def test_in_kernel_tools_set(self):
        assert "request_space_action" in ReasoningService._KERNEL_TOOLS

    def test_paths_include_loop_and_confirmed(self):
        paths = ReasoningService._KERNEL_TOOL_PATHS["request_space_action"]
        assert "loop" in paths
        assert "confirmed" in paths


class TestEnvelopeBuilder:
    def test_build_request_from_tool_input(self):
        req = build_request_from_tool_input(
            tool_input={
                "target_space_id": "tgt",
                "action_kind": "write_knowledge",
                "work_order": {"topic": "x", "content": "y"},
            },
            instance_id="inst1",
            origin_space_id="src",
            initiating_member_id="mem_owner",
            source_turn_id="conv1",
        )
        assert req.target_space_id == "tgt"
        assert req.origin_space_id == "src"
        assert req.action_kind == "write_knowledge"
        assert req.work_order == {"topic": "x", "content": "y"}
        assert req.request_id  # auto-generated when not supplied

    def test_build_request_preserves_explicit_request_id(self):
        req = build_request_from_tool_input(
            tool_input={
                "target_space_id": "tgt",
                "action_kind": "write_knowledge",
                "work_order": {"topic": "x", "content": "y"},
                "request_id": "csr_explicit",
            },
            instance_id="inst1",
            origin_space_id="src",
            initiating_member_id="mem_owner",
            source_turn_id="conv1",
        )
        assert req.request_id == "csr_explicit"


# ---------------------------------------------------------------------------
# Target re-entry awareness block
# ---------------------------------------------------------------------------


@dataclass
class _FakeEvent:
    id: str
    type: str
    instance_id: str
    timestamp: str
    source: str
    payload: dict


class _AwarenessFakeEvents:
    def __init__(self, events: list[_FakeEvent]) -> None:
        self._events = events

    async def query(
        self, instance_id, event_types=None, after=None, before=None, limit=50,
    ):
        out = []
        for e in self._events:
            if e.instance_id != instance_id:
                continue
            if event_types and e.type not in event_types:
                continue
            if after and e.timestamp < after:
                continue
            out.append(e)
        return out[:limit]


class _FakeHandler:
    def __init__(self, events) -> None:
        self.events = events


class TestAwarenessBlock:
    async def test_no_events_returns_empty(self):
        from kernos.messages.phases.assemble import (
            _cross_space_awareness_block,
        )
        handler = _FakeHandler(_AwarenessFakeEvents([]))
        block = await _cross_space_awareness_block(
            handler, "inst1", "space_target",
        )
        assert block == ""

    async def test_event_for_other_space_filtered_out(self):
        from kernos.messages.phases.assemble import (
            _cross_space_awareness_block,
        )
        events = [
            _FakeEvent(
                id="evt1", type="cross_space.action", instance_id="inst1",
                timestamp="2026-05-01T12:00:00Z", source="cross_space",
                payload={
                    "target_space_id": "different_space",
                    "request_id": "csr_x", "action_kind": "write_knowledge",
                    "origin_space_id": "o1", "initiating_member_id": "m",
                    "receipt": {"status": "completed", "created_refs": []},
                },
            ),
        ]
        handler = _FakeHandler(_AwarenessFakeEvents(events))
        block = await _cross_space_awareness_block(
            handler, "inst1", "space_target",
        )
        assert block == ""

    async def test_event_for_active_space_renders(self):
        from kernos.messages.phases.assemble import (
            _cross_space_awareness_block,
        )
        events = [
            _FakeEvent(
                id="evt1", type="cross_space.action", instance_id="inst1",
                timestamp="2026-05-01T12:00:00Z", source="cross_space",
                payload={
                    "target_space_id": "space_target",
                    "request_id": "csr_x",
                    "action_kind": "write_knowledge",
                    "origin_space_id": "space_origin",
                    "initiating_member_id": "mem_owner",
                    "receipt": {
                        "status": "completed",
                        "created_refs": [
                            {"type": "knowledge_entry", "id": "know_abc"},
                        ],
                    },
                },
            ),
        ]
        handler = _FakeHandler(_AwarenessFakeEvents(events))
        block = await _cross_space_awareness_block(
            handler, "inst1", "space_target",
        )
        assert "[CROSS_SPACE_INBOUND]" in block
        assert "csr_x" in block
        assert "space_origin" in block
        assert "knowledge_entry=know_abc" in block

    async def test_block_caps_at_5_entries(self):
        from kernos.messages.phases.assemble import (
            _cross_space_awareness_block,
        )
        events = []
        for i in range(10):
            events.append(_FakeEvent(
                id=f"evt{i}", type="cross_space.action",
                instance_id="inst1",
                timestamp=f"2026-05-01T12:0{i}:00Z",
                source="cross_space",
                payload={
                    "target_space_id": "space_target",
                    "request_id": f"csr_{i}",
                    "action_kind": "write_knowledge",
                    "origin_space_id": "space_origin",
                    "initiating_member_id": "mem_owner",
                    "receipt": {"status": "completed", "created_refs": []},
                },
            ))
        handler = _FakeHandler(_AwarenessFakeEvents(events))
        block = await _cross_space_awareness_block(
            handler, "inst1", "space_target",
        )
        # 10 events; cap at 5 most recent. Count bullet lines.
        bullets = [l for l in block.splitlines() if l.startswith("  - ")]
        assert len(bullets) == 5
