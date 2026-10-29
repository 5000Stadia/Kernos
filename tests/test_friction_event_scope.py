"""Unclassified friction retains its instance through the live event hook."""

from unittest.mock import AsyncMock

import pytest

from kernos.kernel import event_stream
from kernos.kernel.friction import FrictionObserver, FrictionSignal
from tests.test_handler import _make_handler


def signal():
    return FrictionSignal(
        signal_type="EXAMPLE_UNCLASSIFIED", description="synthetic detector result",
        evidence=[], context={},
    )


@pytest.mark.parametrize("instance_id", ["example-instance-a", "example-instance-b"])
async def test_handler_event_hook_retains_unclassified_instance(monkeypatch, instance_id):
    emit = AsyncMock()
    monkeypatch.setattr(event_stream, "emit", emit)
    handler, _ = _make_handler()
    await handler._friction._emit_pattern_unclassified(signal(), "reports/example.md", instance_id)
    emit.assert_awaited_once()
    actual_instance, event_type, payload = emit.await_args.args
    assert actual_instance == instance_id
    assert event_type == "friction.pattern_unclassified"
    assert payload["instance_id"] == instance_id
    assert payload["report_path"] == "reports/example.md"


async def test_fallback_event_has_the_same_instance_metadata(monkeypatch):
    emit = AsyncMock()
    monkeypatch.setattr(event_stream, "emit", emit)
    observer = FrictionObserver(emit_event=None)
    await observer._emit_pattern_unclassified(signal(), "reports/example.md", "example-instance")
    actual_instance, event_type, payload = emit.await_args.args
    assert actual_instance == "example-instance"
    assert event_type == "friction.pattern_unclassified"
    assert payload["instance_id"] == "example-instance"
