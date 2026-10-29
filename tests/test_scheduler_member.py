"""A reminder belongs to the member who set it.

manage_schedule recorded the space id as the trigger's member, so firings
were logged in a folder named after the space, which no agent reads.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from kernos.kernel.scheduler import Trigger, trigger_member_id


def _trigger(member_id, space_id="space_1"):
    return Trigger(trigger_id="t", instance_id="inst", member_id=member_id, space_id=space_id)


async def test_creator_is_kept():
    handler = SimpleNamespace(_instance_db=None)
    assert await trigger_member_id(_trigger("mem_b"), handler) == "mem_b"


async def test_space_id_as_member_falls_back_to_the_real_owner():
    db = MagicMock()
    db.get_owner_member_id = AsyncMock(return_value="mem_owner")
    handler = SimpleNamespace(_instance_db=db)
    assert await trigger_member_id(_trigger("space_1"), handler) == "mem_owner"
    assert await trigger_member_id(_trigger(""), handler) == "mem_owner"


async def test_without_an_instance_db_the_placeholder_owner_stands():
    handler = SimpleNamespace(_instance_db=None)
    assert await trigger_member_id(_trigger(""), handler) == "member:inst:owner"


async def test_manage_schedule_passes_the_requesting_member():
    from unittest.mock import patch
    from kernos.kernel.reasoning import ReasoningService
    svc = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
    svc._trigger_store = MagicMock()
    request = MagicMock()
    request.instance_id, request.member_id, request.active_space_id = "inst", "mem_b", "space_1"
    request.conversation_id = "c"
    with patch("kernos.kernel.scheduler.handle_manage_schedule",
               new=AsyncMock(return_value="ok")) as handle:
        await svc.execute_tool("manage_schedule", {"action": "list"}, request)
    assert handle.call_args.kwargs["member_id"] == "mem_b"
    assert handle.call_args.kwargs["space_id"] == "space_1"
