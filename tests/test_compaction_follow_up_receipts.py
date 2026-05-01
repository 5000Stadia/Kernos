"""CLEANUP-BATCH-V1 item 8: regression tests for compaction follow-up
receipts. The audit observed that the silent-no-op bug "appears
fixed" but coverage was for the parsing path, not the explicit
success/failure receipt. These tests pin the receipt behavior so a
future regression to silent operation fails CI.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.event_types import EventType
from kernos.messages.handler import MessageHandler


def _make_handler() -> MessageHandler:
    """Construct a handler stub minimal enough to drive
    ``_process_compaction_follow_ups`` without spinning up the full
    MessageHandler dependency tree. We monkey-patch the attributes
    the method actually reads."""
    h = MessageHandler.__new__(MessageHandler)
    h.events = MagicMock()
    h._trigger_store = MagicMock()
    h._trigger_store.list_all = AsyncMock(return_value=[])
    h._trigger_store.save = AsyncMock(return_value=None)
    return h


def _captured_receipts(handler: MessageHandler) -> list[dict]:
    """Pull receipt-event payloads out of the mocked event stream."""
    out: list[dict] = []
    for call in handler.events.method_calls:
        # emit_event lives at module scope, not on the stream — but we
        # patch emit_event below to capture into this list, so the
        # method-calls path is unused here. Kept for clarity.
        pass
    return out


class TestEmptyCommitments:
    async def test_emits_empty_receipt(self, monkeypatch):
        """No follow-ups in input → still emits a receipt with
        status='empty' so observers can distinguish 'ran with no
        work' from 'never ran'."""
        h = _make_handler()
        captured: list[dict] = []

        async def _emit(events, etype, instance_id, source, **kwargs):
            captured.append({
                "type": etype, "instance_id": instance_id,
                "source": source, **kwargs,
            })

        monkeypatch.setattr("kernos.messages.handler.emit_event", _emit)

        await h._process_compaction_follow_ups("inst1", "space1", [])

        assert len(captured) == 1
        evt = captured[0]
        assert evt["type"] == EventType.COMPACTION_FOLLOW_UP_PROCESSED
        assert evt["payload"]["status"] == "empty"
        assert evt["payload"]["input_count"] == 0
        assert evt["payload"]["created_count"] == 0
        assert evt["payload"]["skip_reasons"] == []


class TestSuccessReceipt:
    async def test_creates_trigger_and_emits_succeeded(self, monkeypatch):
        h = _make_handler()
        captured: list[dict] = []

        async def _emit(events, etype, instance_id, source, **kwargs):
            captured.append({"type": etype, **kwargs})

        monkeypatch.setattr("kernos.messages.handler.emit_event", _emit)

        commits = [{
            "type": "FOLLOW_UP",
            "description": "check on the migration plan",
            "due": "soon",
            "context": "user mentioned it Monday",
        }]
        await h._process_compaction_follow_ups("inst1", "space1", commits)

        # Trigger was saved
        assert h._trigger_store.save.await_count == 1

        # Single receipt with status=succeeded, created_count=1
        receipts = [c for c in captured
                    if c["type"] == EventType.COMPACTION_FOLLOW_UP_PROCESSED]
        assert len(receipts) == 1
        payload = receipts[0]["payload"]
        assert payload["status"] == "succeeded"
        assert payload["input_count"] == 1
        assert payload["created_count"] == 1
        assert payload["skipped_count"] == 0


class TestSkipReasonsTracked:
    async def test_skipped_rows_show_up_in_payload(self, monkeypatch):
        """Missing description + beyond-90-days both record skip
        reasons in the receipt so triage can see why nothing was
        created."""
        h = _make_handler()
        captured: list[dict] = []

        async def _emit(events, etype, instance_id, source, **kwargs):
            captured.append({"type": etype, **kwargs})

        monkeypatch.setattr("kernos.messages.handler.emit_event", _emit)

        commits = [
            {"type": "FOLLOW_UP", "description": ""},  # missing
            {"type": "FOLLOW_UP", "description": "old item",
             "due": "2030-01-01"},  # beyond 90 days
        ]
        await h._process_compaction_follow_ups("inst1", "space1", commits)

        receipts = [c for c in captured
                    if c["type"] == EventType.COMPACTION_FOLLOW_UP_PROCESSED]
        assert len(receipts) == 1
        payload = receipts[0]["payload"]
        assert payload["status"] == "succeeded"
        assert payload["created_count"] == 0
        assert payload["skipped_count"] == 2
        assert "missing_description" in payload["skip_reasons"]
        assert "beyond_90_days" in payload["skip_reasons"]
        # No trigger save happened.
        assert h._trigger_store.save.await_count == 0


class TestFailureReceipt:
    async def test_emits_failed_when_processing_raises(self, monkeypatch):
        """If the processing loop raises (e.g. trigger store down),
        a status='failed' receipt is emitted before the exception
        propagates. Pins the contract that operators always see a
        receipt event."""
        h = _make_handler()
        h._trigger_store.list_all = AsyncMock(
            side_effect=RuntimeError("store unavailable"),
        )
        captured: list[dict] = []

        async def _emit(events, etype, instance_id, source, **kwargs):
            captured.append({"type": etype, **kwargs})

        monkeypatch.setattr("kernos.messages.handler.emit_event", _emit)

        with pytest.raises(RuntimeError, match="store unavailable"):
            await h._process_compaction_follow_ups(
                "inst1", "space1",
                [{"type": "FOLLOW_UP", "description": "x"}],
            )

        receipts = [c for c in captured
                    if c["type"] == EventType.COMPACTION_FOLLOW_UP_PROCESSED]
        assert len(receipts) == 1
        payload = receipts[0]["payload"]
        assert payload["status"] == "failed"
        assert "store unavailable" in payload["error"]


class TestSilentNoOpRegression:
    async def test_silent_no_op_would_fail_this_test(self, monkeypatch):
        """Pin: if someone refactors _process_compaction_follow_ups
        and accidentally drops the receipt-emission path, this test
        fails. The original silent-no-op bug looked like 'function
        returns without emitting anything'; that is exactly what we
        forbid here."""
        h = _make_handler()
        captured: list[dict] = []

        async def _emit(events, etype, instance_id, source, **kwargs):
            captured.append({"type": etype, **kwargs})

        monkeypatch.setattr("kernos.messages.handler.emit_event", _emit)

        # Both empty input and non-empty input must produce a receipt.
        await h._process_compaction_follow_ups("inst1", "space1", [])
        await h._process_compaction_follow_ups("inst1", "space1",
            [{"type": "FOLLOW_UP", "description": "x"}])

        receipts = [c for c in captured
                    if c["type"] == EventType.COMPACTION_FOLLOW_UP_PROCESSED]
        assert len(receipts) == 2, (
            "compaction follow-up processing must emit a receipt for "
            "every invocation; silent-no-op regression detected"
        )
