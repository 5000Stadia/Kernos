"""WTC v1 C4 — external source contracts (no polling).

Pins:

* ``email.message_observed`` payload-shape contract: required
  fields populated, optional fields default cleanly, message_id
  required.
* ``notion.page_observed`` payload-shape contract: required
  fields populated, change_kind validated.
* Predicate language works against the shapes — ``op: eq`` on
  payload.<field> matches end-to-end through the InternalEventAdapter.
* Selector targeting payload-specific fields fires only on
  matching events (no false positives across sources).
* Source contract test: a real-world predicate ("emails from a
  specific sender" / "new pages in a specific database") fires
  exactly once per matching event.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from kernos.kernel import event_stream
from kernos.kernel.triggers import (
    DispatchPolicy,
    EVENT_TYPE_EMAIL_MESSAGE_OBSERVED,
    EVENT_TYPE_NOTION_PAGE_OBSERVED,
    EmailMessageSource,
    InternalEventAdapter,
    NotionPageSource,
    TemporalRelation,
    TriggerEvaluationRuntime,
    TriggerPredicate,
)


# ---------------------------------------------------------------------------
# Stub WLP — same pattern as C2 / C3.
# ---------------------------------------------------------------------------


class _StubWLP:
    def __init__(self) -> None:
        self.executions: dict[str, str] = {}
        self.dispatch_calls: list[dict] = []

    async def execute_workflow(
        self,
        *,
        fire_id: str,
        workflow_id: str,
        instance_id: str,
        trigger_event_payload: Any = None,
        member_id: str = "",
        **kwargs: Any,
    ) -> str:
        self.dispatch_calls.append({
            "fire_id": fire_id,
            "workflow_id": workflow_id,
            "instance_id": instance_id,
            "payload": trigger_event_payload,
        })
        if fire_id in self.executions:
            return self.executions[fire_id]
        execution_id = f"exec_{uuid.uuid4().hex[:8]}"
        self.executions[fire_id] = execution_id
        return execution_id

    async def find_execution_by_fire_id(self, fire_id: str) -> str | None:
        return self.executions.get(fire_id)


@pytest.fixture
async def event_stream_started(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path), flush_interval_s=0.05)
    yield
    await event_stream.stop_writer()
    await event_stream._reset_for_tests()


@pytest.fixture
async def wlp() -> _StubWLP:
    return _StubWLP()


@pytest.fixture
async def runtime(tmp_path, wlp, event_stream_started):
    rt = TriggerEvaluationRuntime()
    await rt.start(
        data_dir=str(tmp_path),
        heartbeat_seconds=1,
        wlp_dispatch=wlp.execute_workflow,
        wlp_lookup_by_fire_id=wlp.find_execution_by_fire_id,
    )
    yield rt
    await rt.stop()


@pytest.fixture
async def adapter(runtime):
    a = InternalEventAdapter(runtime)
    await a.start()
    yield a
    await a.stop()


# ---------------------------------------------------------------------------
# EmailMessageSource — contract tests
# ---------------------------------------------------------------------------


async def test_email_source_message_id_required(event_stream_started):
    src = EmailMessageSource(instance_id="inst")
    await src.start()
    with pytest.raises(ValueError, match="message_id"):
        await src.emit_observed(message_id="", from_address="a@b.com")
    await src.stop()


async def test_email_source_payload_shape(event_stream_started):
    src = EmailMessageSource(instance_id="inst-email")
    await src.start()
    await src.emit_observed(
        message_id="msg_001",
        thread_id="thr_001",
        from_address="owner@example.com",
        from_name="the design review",
        to_addresses=["bob@example.com"],
        cc_addresses=["copilot@kernos.dev"],
        subject="WTC v1 review",
        received_iso="2026-04-30T18:00:00+00:00",
        snippet="Looks good — folding the verdict now.",
        labels=["INBOX", "Important"],
        mailbox="primary",
    )
    await event_stream.flush_now()

    from datetime import datetime, timedelta, timezone
    rows = await event_stream.events_in_window(
        "inst-email",
        datetime.now(timezone.utc) - timedelta(minutes=5),
        datetime.now(timezone.utc) + timedelta(minutes=5),
        event_types=[EVENT_TYPE_EMAIL_MESSAGE_OBSERVED],
    )
    assert len(rows) == 1
    p = rows[0].payload
    assert p["message_id"] == "msg_001"
    assert p["thread_id"] == "thr_001"
    assert p["from_address"] == "owner@example.com"
    assert p["from_name"] == "the design review"
    assert p["to_addresses"] == ["bob@example.com"]
    assert p["cc_addresses"] == ["copilot@kernos.dev"]
    assert p["subject"] == "WTC v1 review"
    assert p["received_iso"] == "2026-04-30T18:00:00+00:00"
    assert p["snippet"].startswith("Looks good")
    assert p["labels"] == ["INBOX", "Important"]
    assert p["mailbox"] == "primary"
    await src.stop()


async def test_email_source_optional_fields_default(event_stream_started):
    """Only message_id is required; everything else defaults
    cleanly so an observer with minimal payload data still emits a
    well-shaped event."""
    src = EmailMessageSource(instance_id="inst-min")
    await src.start()
    await src.emit_observed(message_id="msg_min")
    await event_stream.flush_now()

    from datetime import datetime, timedelta, timezone
    rows = await event_stream.events_in_window(
        "inst-min",
        datetime.now(timezone.utc) - timedelta(minutes=5),
        datetime.now(timezone.utc) + timedelta(minutes=5),
        event_types=[EVENT_TYPE_EMAIL_MESSAGE_OBSERVED],
    )
    p = rows[0].payload
    assert p["message_id"] == "msg_min"
    assert p["from_address"] == ""
    assert p["to_addresses"] == []
    assert p["cc_addresses"] == []
    assert p["labels"] == []
    await src.stop()


async def test_email_predicate_fires_on_sender_match(
    runtime, wlp, adapter,
):
    """Real-world contract: a predicate selecting on
    payload.from_address fires when an observed email matches
    that sender, and not otherwise."""
    await runtime.register(
        trigger_id="email-from-design review",
        instance_id="inst1",
        workflow_id="wf-respond-to-design review",
        predicate=TriggerPredicate(
            event_selector={
                "op": "AND",
                "operands": [
                    {"op": "eq", "path": "event_type",
                     "value": EVENT_TYPE_EMAIL_MESSAGE_OBSERVED},
                    {"op": "eq", "path": "payload.from_address",
                     "value": "owner@example.com"},
                ],
            },
            temporal_relation=TemporalRelation(kind="on"),
            dispatch_policy=DispatchPolicy(),
        ),
    )

    src = EmailMessageSource(instance_id="inst1")
    await src.start()
    # Match.
    await src.emit_observed(
        message_id="msg_a",
        from_address="owner@example.com",
        subject="hello",
    )
    # Non-match (different sender).
    await src.emit_observed(
        message_id="msg_b",
        from_address="someone-else@anthropic.com",
        subject="unrelated",
    )
    await event_stream.flush_now()

    assert len(wlp.dispatch_calls) == 1
    assert wlp.dispatch_calls[0]["workflow_id"] == "wf-respond-to-design review"
    await src.stop()


# ---------------------------------------------------------------------------
# NotionPageSource — contract tests
# ---------------------------------------------------------------------------


async def test_notion_source_page_id_required(event_stream_started):
    src = NotionPageSource(instance_id="inst")
    await src.start()
    with pytest.raises(ValueError, match="page_id"):
        await src.emit_observed(page_id="", change_kind="created")
    await src.stop()


async def test_notion_source_change_kind_validated(event_stream_started):
    src = NotionPageSource(instance_id="inst")
    await src.start()
    with pytest.raises(ValueError, match="change_kind"):
        await src.emit_observed(page_id="p1", change_kind="modified")
    await src.stop()


async def test_notion_source_payload_shape(event_stream_started):
    src = NotionPageSource(instance_id="inst-notion")
    await src.start()
    await src.emit_observed(
        page_id="page_abc",
        change_kind="updated",
        title="WTC v1 spec",
        parent_kind="database",
        parent_id="db_specs",
        url="https://notion.so/page_abc",
        last_edited_iso="2026-04-30T19:00:00+00:00",
        last_edited_by="user_kit",
        tags=["spec", "in-progress"],
    )
    await event_stream.flush_now()

    from datetime import datetime, timedelta, timezone
    rows = await event_stream.events_in_window(
        "inst-notion",
        datetime.now(timezone.utc) - timedelta(minutes=5),
        datetime.now(timezone.utc) + timedelta(minutes=5),
        event_types=[EVENT_TYPE_NOTION_PAGE_OBSERVED],
    )
    assert len(rows) == 1
    p = rows[0].payload
    assert p["page_id"] == "page_abc"
    assert p["change_kind"] == "updated"
    assert p["title"] == "WTC v1 spec"
    assert p["parent_kind"] == "database"
    assert p["parent_id"] == "db_specs"
    assert p["url"] == "https://notion.so/page_abc"
    assert p["last_edited_iso"] == "2026-04-30T19:00:00+00:00"
    assert p["last_edited_by"] == "user_kit"
    assert p["tags"] == ["spec", "in-progress"]
    await src.stop()


async def test_notion_predicate_fires_on_change_kind_filter(
    runtime, wlp, adapter,
):
    """Real-world contract: a predicate filtering on
    payload.change_kind == 'created' fires only on creations,
    ignoring updates within the same database."""
    await runtime.register(
        trigger_id="notion-new-page",
        instance_id="inst1",
        workflow_id="wf-greet-new-page",
        predicate=TriggerPredicate(
            event_selector={
                "op": "AND",
                "operands": [
                    {"op": "eq", "path": "event_type",
                     "value": EVENT_TYPE_NOTION_PAGE_OBSERVED},
                    {"op": "eq", "path": "payload.change_kind",
                     "value": "created"},
                    {"op": "eq", "path": "payload.parent_id",
                     "value": "db_inbox"},
                ],
            },
            temporal_relation=TemporalRelation(kind="on"),
            dispatch_policy=DispatchPolicy(),
        ),
    )

    src = NotionPageSource(instance_id="inst1")
    await src.start()
    # Match — created page in the targeted db.
    await src.emit_observed(
        page_id="p_created",
        change_kind="created",
        parent_id="db_inbox",
    )
    # Non-match — update of an existing page.
    await src.emit_observed(
        page_id="p_updated",
        change_kind="updated",
        parent_id="db_inbox",
    )
    # Non-match — created but in a different db.
    await src.emit_observed(
        page_id="p_other_db",
        change_kind="created",
        parent_id="db_other",
    )
    await event_stream.flush_now()

    assert len(wlp.dispatch_calls) == 1
    assert wlp.dispatch_calls[0]["workflow_id"] == "wf-greet-new-page"
    await src.stop()


# ---------------------------------------------------------------------------
# Cross-source isolation — predicates targeting one source don't
# match the other source's events even if other fields overlap.
# ---------------------------------------------------------------------------


async def test_email_predicate_does_not_match_notion_event(
    runtime, wlp, adapter,
):
    await runtime.register(
        trigger_id="t",
        instance_id="inst1",
        workflow_id="wf-email-only",
        predicate=TriggerPredicate(
            event_selector={
                "op": "eq", "path": "event_type",
                "value": EVENT_TYPE_EMAIL_MESSAGE_OBSERVED,
            },
            temporal_relation=TemporalRelation(kind="on"),
            dispatch_policy=DispatchPolicy(),
        ),
    )

    notion = NotionPageSource(instance_id="inst1")
    await notion.start()
    await notion.emit_observed(page_id="p1", change_kind="created")
    await event_stream.flush_now()
    assert wlp.dispatch_calls == []
    await notion.stop()


async def test_source_start_stop_idempotent(event_stream_started):
    e = EmailMessageSource(instance_id="inst")
    await e.start()
    await e.start()
    await e.stop()
    await e.stop()

    n = NotionPageSource(instance_id="inst")
    await n.start()
    await n.start()
    await n.stop()
    await n.stop()
