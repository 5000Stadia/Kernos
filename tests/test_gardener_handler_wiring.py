"""Exercise the handler's production Gardener construction and event bridge."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.cohorts.gardener import GardenerDecision
from kernos.kernel.instance_db import InstanceDB
from kernos.messages.handler import MessageHandler


@pytest.fixture
async def handler_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    db = InstanceDB(str(tmp_path))
    await db.connect()
    handler = object.__new__(MessageHandler)
    handler._canvas = None
    handler._gardener = None
    handler._instance_db = db
    handler.events = SimpleNamespace(emit=AsyncMock())
    handler.reasoning = SimpleNamespace(
        set_canvas=MagicMock(),
        complete_simple=AsyncMock(side_effect=AssertionError("no model calls")),
    )
    canvas = handler._get_canvas_service()
    gardener = handler._get_gardener_service()
    try:
        yield handler, canvas, gardener
    finally:
        await gardener.wait_idle()
        await db.close()


async def test_handler_created_gardener_emits_pattern_application(handler_env):
    handler, canvas, gardener = handler_env
    target = await canvas.create(
        instance_id="inst_test", creator_member_id="member_test",
        name="Test", scope="team",
    )
    await gardener.wait_idle()
    handler.events.emit.reset_mock()
    gardener.consult_initial_shape = AsyncMock(return_value=None)

    await gardener.apply_initial_shape(
        instance_id="inst_test", canvas_id=target.canvas_id,
        canvas_name="Test", scope="team", creator_member_id="member_test",
    )
    await gardener.wait_idle()

    defaults = await canvas._canvas_defaults("inst_test", target.canvas_id)
    assert defaults["pattern"] == "unmatched"
    events = [call.args[0] for call in handler.events.emit.await_args_list]
    applied = [event for event in events if event.type == "canvas.pattern_applied"]
    assert len(applied) == 1
    assert applied[0].instance_id == "inst_test"
    assert applied[0].payload["canvas_id"] == target.canvas_id
    assert applied[0].payload["pattern"] == "unmatched"
    assert applied[0].metadata == {"member_id": "member_test"}


async def test_handler_gardener_reshape_uses_event_bridge_without_cascade(handler_env):
    handler, _, gardener = handler_env
    gardener._dispatch = AsyncMock()
    await gardener._auto_apply(
        instance_id="inst_test", canvas_id="canvas_test", page_path="page.md",
        decision=GardenerDecision(
            action="regenerate_summary", confidence="high", rationale="test",
        ),
    )
    await gardener.wait_idle()

    handler.events.emit.assert_awaited_once()
    event = handler.events.emit.await_args.args[0]
    assert event.type == "canvas.reshaped"
    assert event.instance_id == "inst_test"
    assert event.payload["canvas_id"] == "canvas_test"
    assert event.payload["source"] == "gardener"
    gardener._dispatch.assert_not_awaited()
