"""COGNITIVE-CONTEXT-V1 C1 — type construction + frozen-ness pins.

Asserts the packet types are constructible, frozen, and roundtrip
through ``with_updates``. The contract tests at C2 will later
assert content; C1 only pins the type surface.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from kernos.kernel.cognitive_context import (
    ActionsBlock,
    CognitiveContext,
    ConversationBlock,
    MemoryBlock,
    NowBlock,
    ResultsBlock,
    RulesBlock,
    SafetyConstraints,
    StateBlock,
    ToolSurface,
)


def _minimal_packet() -> CognitiveContext:
    """Construct a minimal packet for type-surface tests."""
    return CognitiveContext(
        rules=RulesBlock(
            operating_principles="ops principles",
            bootstrap_prompt="bootstrap",
            hatching_prompt="hatching",
            covenants=(),
            space_names={},
            instance_stewardship="purpose",
        ),
        now=NowBlock(
            timestamp_utc=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            user_timezone="America/Los_Angeles",
            platform="discord",
            auth_level="owner",
            instance_id="inst1",
            member_id="m1",
            member_display_name="Owner",
            active_space_id="space:1",
            active_space_name="general",
            agent_name="",
        ),
        state=StateBlock(
            soul=None,
            member_profile={"display_name": "Owner"},
            relationships=(),
            knowledge_entries=(),
        ),
        results=ResultsBlock(results_prefix=""),
        actions=ActionsBlock(capability_prompt="", channel_registry=()),
        memory=MemoryBlock(
            compaction_carry="",
            awareness_whispers=(),
            gardener_observations=(),
        ),
        conversation=ConversationBlock(messages=()),
        tool_surface=ToolSurface(
            always_pinned=({"name": "remember"},),
            active_zone=(),
            request_tool={"name": "request_tool"},
        ),
        safety_constraints=SafetyConstraints(
            sensitivity_gates=(),
            disclosure_layer={},
            cross_member_rules=(),
        ),
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_packet_constructs_from_all_nine_blocks():
    pkt = _minimal_packet()
    assert isinstance(pkt, CognitiveContext)
    assert pkt.rules.operating_principles == "ops principles"
    assert pkt.now.platform == "discord"
    assert pkt.state.member_profile["display_name"] == "Owner"
    assert pkt.results.results_prefix == ""
    assert pkt.actions.capability_prompt == ""
    assert pkt.memory.compaction_carry == ""
    assert pkt.conversation.messages == ()
    assert pkt.tool_surface.always_pinned == ({"name": "remember"},)
    assert pkt.safety_constraints.disclosure_layer == {}


def test_all_nine_blocks_are_frozen():
    """Frozen-ness pin: each block raises FrozenInstanceError on
    direct attribute assignment. This is the structural guarantee
    the integration layer relies on — substrate updates go through
    `with_updates`/`replace`, not in-place mutation."""
    pkt = _minimal_packet()
    blocks = [
        pkt.rules, pkt.now, pkt.state, pkt.results, pkt.actions,
        pkt.memory, pkt.conversation, pkt.tool_surface,
        pkt.safety_constraints,
    ]
    for block in blocks:
        first_field = next(iter(block.__dataclass_fields__))
        with pytest.raises(FrozenInstanceError):
            setattr(block, first_field, "mutated")


def test_packet_itself_is_frozen():
    pkt = _minimal_packet()
    with pytest.raises(FrozenInstanceError):
        pkt.rules = None  # type: ignore[misc]


# ---------------------------------------------------------------------------
# .with_updates roundtrip
# ---------------------------------------------------------------------------


def test_with_updates_produces_new_packet_with_changed_field():
    pkt = _minimal_packet()
    new_actions = ActionsBlock(
        capability_prompt="upgraded",
        channel_registry=({"name": "discord"},),
    )
    pkt2 = pkt.with_updates(actions=new_actions)
    assert pkt2 is not pkt
    assert pkt2.actions.capability_prompt == "upgraded"
    assert pkt.actions.capability_prompt == ""  # original unchanged
    # Other fields preserved by reference.
    assert pkt2.rules is pkt.rules
    assert pkt2.now is pkt.now


def test_block_replace_then_packet_with_updates_roundtrip():
    """Integration's update pattern: dataclasses.replace on a block
    to get the new block, then with_updates on the packet."""
    pkt = _minimal_packet()
    enriched_memory = replace(
        pkt.memory,
        gardener_observations=({"signal": "x", "weight": 0.5},),
    )
    pkt2 = pkt.with_updates(memory=enriched_memory)
    assert pkt2.memory.gardener_observations == (
        {"signal": "x", "weight": 0.5},
    )
    assert pkt.memory.gardener_observations == ()


# ---------------------------------------------------------------------------
# ToolSurface convenience
# ---------------------------------------------------------------------------


def test_tool_surface_all_tools_concatenates_pinned_active_request():
    surface = ToolSurface(
        always_pinned=({"name": "remember"}, {"name": "send_to_channel"}),
        active_zone=({"name": "list-events"},),
        request_tool={"name": "request_tool"},
    )
    all_tools = surface.all_tools()
    names = [t["name"] for t in all_tools]
    assert names == ["remember", "send_to_channel", "list-events", "request_tool"]


def test_tool_surface_all_tools_dedups_request_when_already_pinned():
    surface = ToolSurface(
        always_pinned=(
            {"name": "remember"},
            {"name": "request_tool"},  # already in pinned
        ),
        active_zone=(),
        request_tool={"name": "request_tool"},
    )
    all_tools = surface.all_tools()
    names = [t["name"] for t in all_tools]
    assert names == ["remember", "request_tool"]
    assert names.count("request_tool") == 1


def test_tool_surface_all_tools_handles_missing_request_tool():
    surface = ToolSurface(
        always_pinned=({"name": "remember"},),
        active_zone=({"name": "list-events"},),
        request_tool=None,
    )
    all_tools = surface.all_tools()
    names = [t["name"] for t in all_tools]
    assert names == ["remember", "list-events"]


# ---------------------------------------------------------------------------
# Bootstrap-graduation gating intent (no logic at C1, just shape pin)
# ---------------------------------------------------------------------------


def test_rules_block_supports_none_for_graduated_bootstrap():
    """Graduated members have None bootstrap_prompt + None
    hatching_prompt. The type signature must allow this."""
    block = RulesBlock(
        operating_principles="ops",
        bootstrap_prompt=None,
        hatching_prompt=None,
        covenants=(),
        space_names={},
        instance_stewardship="",
    )
    assert block.bootstrap_prompt is None
    assert block.hatching_prompt is None


def test_memory_block_procedures_and_canvases_default_empty():
    """Codex C1 review fold: MemoryBlock gained procedures +
    canvases_summary fields with empty defaults."""
    block = MemoryBlock(
        compaction_carry="",
        awareness_whispers=(),
        gardener_observations=(),
    )
    assert block.procedures == ""
    assert block.canvases_summary == ""
