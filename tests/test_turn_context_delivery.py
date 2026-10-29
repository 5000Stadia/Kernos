"""Turn context survives interleaved spaces and deferred delivery."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kernos.kernel.awareness import Whisper
from kernos.kernel.scheduler import Trigger, _notify_retirement
from kernos.kernel.soul import Soul
from kernos.messages.handler import TurnContext
from tests.test_handler import _make_handler, _make_message


@pytest.fixture
def handler(tmp_path):
    handler, _ = _make_handler()
    handler.compaction = SimpleNamespace(
        load_state=AsyncMock(return_value=None),
        load_context_document=AsyncMock(return_value=""),
    )
    handler.conv_logger.read_recent = AsyncMock(return_value=[])
    handler.state.drain_space_notices = AsyncMock(return_value=[])
    handler.state.get_pending_whispers = AsyncMock(return_value=[])
    handler._build_canvases_prefix = AsyncMock(return_value=None)
    handler._trigger_store = SimpleNamespace(
        list_all=AsyncMock(return_value=[]), save=AsyncMock(),
    )
    handler._whispers_offered = {}
    return handler


def turn(handler, member, space):
    return TurnContext(
        handler=handler, instance_id="example-instance", member_id=member,
        conversation_id=f"conversation-{member}", active_space_id=space,
        router_result=SimpleNamespace(tags=[space]),
        message=_make_message(f"current message for {member}"),
        soul=Soul(instance_id="example-instance", hatched=True),
    )


def whisper(member, text):
    return Whisper(
        whisper_id=f"whisper-{member}", insight_text=text, delivery_class="stage",
        source_space_id="shared-space", target_space_id="shared-space",
        supporting_evidence=[], reasoning_trace="synthetic observation",
        knowledge_entry_id=f"knowledge-{member}", foresight_signal=text,
        created_at="2026-01-01T00:00:00Z", owner_member_id=member,
    )


async def test_reply_without_offer_does_not_consume_another_members_notes(handler):
    note = whisper("member-a", "member-a background note")
    handler.state.get_pending_whispers.return_value = [note]
    await handler._get_pending_awareness(
        "example-instance", "shared-space", requesting_member_id="member-a",
    )
    reply = await handler._deliver_pending_whispers(
        turn(handler, "member-b", "other-space"), "reply b",
    )
    assert reply == "reply b"
    handler.state.mark_whisper_surfaced.assert_not_awaited()
    reply_a = await handler._deliver_pending_whispers(
        turn(handler, "member-a", "shared-space"), "reply a",
    )
    assert note.insight_text in reply_a
    handler.state.mark_whisper_surfaced.assert_awaited_once_with(
        "example-instance", note.whisper_id,
    )


async def test_offers_for_same_space_keep_each_members_notes(handler):
    a, b = whisper("member-a", "note for a"), whisper("member-b", "note for b")
    handler.state.get_pending_whispers.return_value = [a, b]
    for member in ("member-a", "member-b"):
        await handler._get_pending_awareness(
            "example-instance", "shared-space", requesting_member_id=member,
        )
    reply_a = await handler._deliver_pending_whispers(
        turn(handler, "member-a", "shared-space"), "reply a",
    )
    reply_b = await handler._deliver_pending_whispers(
        turn(handler, "member-b", "shared-space"), "reply b",
    )
    assert a.insight_text in reply_a and b.insight_text not in reply_a
    assert b.insight_text in reply_b and a.insight_text not in reply_b


async def test_interleaved_spaces_retain_their_own_orphaned_messages(handler):
    a = turn(handler, "member-a", "space-a")
    b = turn(handler, "member-b", "space-b")
    a_waiting, release_a = asyncio.Event(), asyncio.Event()

    async def history(instance, space, **kwargs):
        return [{"role": "user", "content": f"unfinished message in {space}"}]

    async def canvases(**kwargs):
        if kwargs["active_space_id"] == "space-a":
            a_waiting.set()
            await release_a.wait()
        return None

    handler.conv_logger.read_recent.side_effect = history
    handler._build_canvases_prefix.side_effect = canvases
    task_a = asyncio.create_task(handler._phase_assemble(a))
    try:
        await asyncio.wait_for(a_waiting.wait(), timeout=2)
        await handler._phase_assemble(b)
    finally:
        release_a.set()
        await task_a
    for ctx, own_space, other_space in ((a, "space-a", "space-b"), (b, "space-b", "space-a")):
        content = ctx.messages[-1]["content"]
        assert f"Earlier message: unfinished message in {own_space}" in content
        assert f"unfinished message in {other_space}" not in content


async def test_pending_scheduled_result_waits_for_its_member(handler):
    trigger = Trigger(
        trigger_id="example-trigger", instance_id="example-instance",
        member_id="member-a", pending_delivery="the expected result",
        action_description="example scheduled action",
    )
    handler._trigger_store.list_all.return_value = [trigger]
    b = turn(handler, "member-b", "space-b")
    await handler._phase_assemble(b)
    assert "the expected result" not in b.messages[-1]["content"]
    assert trigger.pending_delivery == "the expected result"
    handler._trigger_store.save.assert_not_awaited()
    a = turn(handler, "member-a", "space-a")
    await handler._phase_assemble(a)
    assert "the expected result" in a.messages[-1]["content"]
    assert trigger.pending_delivery == ""
    handler._trigger_store.save.assert_awaited_once_with(trigger)


async def test_trigger_notice_is_not_consumed_by_next_unrelated_speaker(handler):
    trigger = Trigger(
        trigger_id="example-trigger", instance_id="example-instance",
        member_id="member-a", action_description="example scheduled action",
        failure_reason="missing example tool",
    )
    _notify_retirement(handler, trigger)
    b = turn(handler, "member-b", "space-b")
    await handler._phase_assemble(b)
    assert "trigger_retired" not in (b.results_prefix or "")
    a = turn(handler, "member-a", "space-a")
    await handler._phase_assemble(a)
    assert "trigger_retired" in a.results_prefix
    again = turn(handler, "member-a", "space-a")
    await handler._phase_assemble(again)
    assert "trigger_retired" not in (again.results_prefix or "")
