"""Tests for event emission in MessageHandler.process()."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.capability.client import MCPClientManager
from kernos.capability.known import KNOWN_CAPABILITIES
from kernos.capability.registry import CapabilityInfo, CapabilityRegistry, CapabilityStatus
from kernos.kernel.engine import TaskEngine
from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, JsonEventStream
from kernos.kernel.exceptions import ReasoningTimeoutError
from kernos.kernel.reasoning import (
    ContentBlock,
    Provider,
    ProviderResponse,
    ReasoningService,
)
from kernos.kernel.state import StateStore, InstanceProfile
from kernos.kernel.state_json import JsonStateStore
from kernos.messages.handler import MessageHandler
from kernos.messages.models import AuthLevel, NormalizedMessage
from kernos.persistence import AuditStore, ConversationStore, InstanceStore


# ---------------------------------------------------------------------------
# Local test helpers (mirrors test_handler.py, kept self-contained)
# ---------------------------------------------------------------------------


def _make_message(content: str = "Hello", platform: str = "sms") -> NormalizedMessage:
    return NormalizedMessage(
        content=content,
        sender="+15555550100",
        sender_auth_level=AuthLevel.owner_unverified,
        platform=platform,
        platform_capabilities=["text", "mms"],
        conversation_id="+15555550100",
        timestamp=datetime.now(timezone.utc),
        instance_id="sms:+15555550100",
    )


def _mock_provider_response(text: str) -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="text", text=text)],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=20,
    )


def _mock_provider_tool_response(name: str, id: str, input: dict) -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="tool_use", name=name, id=id, input=input)],
        stop_reason="tool_use",
        input_tokens=15,
        output_tokens=5,
    )


def _make_mock_handler(tools: list[dict] | None = None):
    """Handler with mock EventStream and StateStore (no disk I/O)."""
    mcp = MagicMock(spec=MCPClientManager)
    mcp.get_tools.return_value = tools or []

    conversations = AsyncMock(spec=ConversationStore)
    conversations.get_recent.return_value = []
    conversations.append.return_value = None

    tenants = AsyncMock(spec=InstanceStore)
    tenants.get_or_create.return_value = {
        "instance_id": "sms:+15555550100",
        "status": "active",
        "created_at": "2026-03-01T00:00:00Z",
        "capabilities": {},
    }

    audit = AsyncMock(spec=AuditStore)
    audit.log.return_value = None

    events = AsyncMock(spec=EventStream)
    events.emit.return_value = None

    state = AsyncMock(spec=StateStore)
    state.get_instance_profile.return_value = InstanceProfile(
        instance_id="sms:+15555550100",
        status="active",
        created_at="2026-03-01T00:00:00Z",
    )
    state.get_conversation_summary.return_value = None
    state.save_conversation_summary.return_value = None
    state.save_instance_profile.return_value = None
    state.get_soul.return_value = None
    state.save_soul.return_value = None
    state.get_contract_rules.return_value = []
    state.get_knowledge_hashes.return_value = set()
    state.query_knowledge.return_value = []

    # Same events and audit mocks shared between handler and ReasoningService
    mock_provider = AsyncMock(spec=Provider)
    registry = MagicMock(spec=CapabilityRegistry)
    tools_list = tools or []
    registry.get_connected_tools.return_value = tools_list
    registry.get_tools_for_space.return_value = tools_list
    registry.build_capability_prompt.return_value = "CURRENT CAPABILITIES — conversation only."
    registry.build_tool_directory.return_value = "CONNECTED SERVICES: Test\nYour tools are listed in your tool definitions."
    registry.get_preloaded_tools.return_value = tools_list
    registry.get_lazy_tool_stubs.return_value = []
    _tool_by_name = {t["name"]: t for t in tools_list}
    registry.get_tool_schema.side_effect = lambda name: _tool_by_name.get(name)
    if tools_list:
        tool_names = [t["name"] for t in tools_list]
        cap = CapabilityInfo(
            name="test-capability",
            display_name="Test Capability",
            description="Test capability",
            category="test",
            status=CapabilityStatus.CONNECTED,
            tools=tool_names,
            server_name="test",
            tool_effects={name: "read" for name in tool_names},
        )
        registry.get_all.return_value = [cap]
    else:
        registry.get_all.return_value = []
    reasoning = ReasoningService(mock_provider, events, mcp, audit)
    from tests._thin_path_test_fixture import wire_test_thin_path
    wire_test_thin_path(reasoning, provider=mock_provider, mcp=mcp)
    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(mcp, conversations, tenants, audit, events, state, reasoning, registry, engine)
    handler.preference_parsing_enabled = False
    # Register test tools in catalog so the surfacer can find them
    for t in tools_list:
        handler._tool_catalog.register(t["name"], t.get("description", "test"), "mcp")
    return handler, mock_provider


def _make_real_handler(tmp_path):
    """Handler with real JsonStateStore and JsonEventStream (writes to tmp_path)."""
    mcp = MagicMock(spec=MCPClientManager)
    mcp.get_tools.return_value = []

    conversations = AsyncMock(spec=ConversationStore)
    conversations.get_recent.return_value = []
    conversations.append.return_value = None

    tenants = AsyncMock(spec=InstanceStore)
    tenants.get_or_create.return_value = {
        "instance_id": "sms:+15555550100",
        "status": "active",
        "created_at": "2026-03-01T00:00:00Z",
        "capabilities": {},
    }

    audit = AsyncMock(spec=AuditStore)
    audit.log.return_value = None

    events = JsonEventStream(tmp_path)
    state = JsonStateStore(tmp_path)

    import dataclasses
    registry = CapabilityRegistry(mcp=mcp)
    for cap in KNOWN_CAPABILITIES:
        registry.register(dataclasses.replace(cap))

    mock_provider = AsyncMock(spec=Provider)
    reasoning = ReasoningService(mock_provider, events, mcp, audit)
    from tests._thin_path_test_fixture import wire_test_thin_path
    wire_test_thin_path(reasoning, provider=mock_provider, mcp=mcp)
    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(mcp, conversations, tenants, audit, events, state, reasoning, registry, engine)
    handler.preference_parsing_enabled = False
    return handler, mock_provider, events, state


def _emitted_types(handler: MessageHandler) -> list[str]:
    """Return event types emitted via the mock EventStream.

    Because handler and ReasoningService share the same events mock, this captures
    all events from both.
    """
    return [c.args[0].type for c in handler.events.emit.call_args_list]


# ---------------------------------------------------------------------------
# Basic flow event emission
# ---------------------------------------------------------------------------


async def test_handler_emits_message_received():
    handler, mock_provider = _make_mock_handler()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    await handler.process(_make_message())

    assert EventType.MESSAGE_RECEIVED in _emitted_types(handler)


async def test_handler_emits_reasoning_request_and_response():
    handler, mock_provider = _make_mock_handler()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    await handler.process(_make_message())

    types = _emitted_types(handler)
    assert EventType.REASONING_REQUEST in types
    assert EventType.REASONING_RESPONSE in types


async def test_handler_emits_message_sent():
    handler, mock_provider = _make_mock_handler()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    await handler.process(_make_message())

    assert EventType.MESSAGE_SENT in _emitted_types(handler)


async def test_reasoning_response_has_token_counts():
    handler, mock_provider = _make_mock_handler()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    await handler.process(_make_message())

    emitted = [c.args[0] for c in handler.events.emit.call_args_list]
    rr = next(e for e in emitted if e.type == EventType.REASONING_RESPONSE)
    assert rr.payload["input_tokens"] == 10
    assert rr.payload["output_tokens"] == 20
    assert "estimated_cost_usd" in rr.payload
    assert "duration_ms" in rr.payload


async def test_message_received_has_content_and_platform():
    handler, mock_provider = _make_mock_handler()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    await handler.process(_make_message("Hello there"))

    emitted = [c.args[0] for c in handler.events.emit.call_args_list]
    mr = next(e for e in emitted if e.type == EventType.MESSAGE_RECEIVED)
    assert mr.payload["content"] == "Hello there"
    assert mr.payload["platform"] == "sms"


async def test_message_sent_has_content():
    handler, mock_provider = _make_mock_handler()
    mock_provider.complete.return_value = _mock_provider_response("I'm good!")

    await handler.process(_make_message())

    emitted = [c.args[0] for c in handler.events.emit.call_args_list]
    ms = next(e for e in emitted if e.type == EventType.MESSAGE_SENT)
    # Name ask may be appended on first interaction (soul has no user_name)
    assert ms.payload["content"].startswith("I'm good!")


async def test_all_events_have_instance_id():
    handler, mock_provider = _make_mock_handler()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    await handler.process(_make_message())

    emitted = [c.args[0] for c in handler.events.emit.call_args_list]
    for event in emitted:
        assert event.instance_id == "sms:+15555550100"


# ---------------------------------------------------------------------------
# Tool-use events
# ---------------------------------------------------------------------------


async def test_handler_emits_tool_called_and_result():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_provider = _make_mock_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Meeting at 10am")

    _tool_resp = _mock_provider_tool_response("list_events", "tu_001", {"date": "2026-03-01"})
    _text_resp = _mock_provider_response("You have a meeting at 10am.")
    _stub = _mock_provider_response("{}")
    _iter = iter([_tool_resp, _text_resp])

    async def _route(**kwargs):
        return next(_iter, _stub) if kwargs.get("tools") else _stub

    mock_provider.complete.side_effect = _route

    await handler.process(_make_message("What's on my schedule?"))

    types = _emitted_types(handler)
    assert EventType.TOOL_CALLED in types
    assert EventType.TOOL_RESULT in types


async def test_tool_called_event_has_tool_name_and_input():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_provider = _make_mock_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Event data")

    _tool_resp = _mock_provider_tool_response("list_events", "tu_001", {"date": "2026-03-01"})
    _text_resp = _mock_provider_response("Done")
    _stub = _mock_provider_response("{}")
    _iter = iter([_tool_resp, _text_resp])
    async def _route(**kwargs):
        return next(_iter, _stub) if kwargs.get("tools") else _stub
    mock_provider.complete.side_effect = _route

    await handler.process(_make_message())

    emitted = [c.args[0] for c in handler.events.emit.call_args_list]
    tc = next(e for e in emitted if e.type == EventType.TOOL_CALLED)
    assert tc.payload["tool_name"] == "list_events"
    assert tc.payload["tool_input"] == {"date": "2026-03-01"}


async def test_tool_result_success_flag_on_success():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_provider = _make_mock_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Meeting at 10am")

    _tr = _mock_provider_tool_response("list_events", "tu_001", {})
    _tx = _mock_provider_response("Done")
    _st = _mock_provider_response("{}")
    _it = iter([_tr, _tx])
    async def _r(**kw): return next(_it, _st) if kw.get("tools") else _st
    mock_provider.complete.side_effect = _r

    await handler.process(_make_message())

    emitted = [c.args[0] for c in handler.events.emit.call_args_list]
    tr = next(e for e in emitted if e.type == EventType.TOOL_RESULT)
    assert tr.payload["success"] is True
    assert tr.payload["error"] is None


async def test_tool_result_error_flag_on_tool_error():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_provider = _make_mock_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Tool error: connection failed")

    _tr = _mock_provider_tool_response("list_events", "tu_001", {})
    _tx = _mock_provider_response("I had trouble with that.")
    _st = _mock_provider_response("{}")
    _it = iter([_tr, _tx])
    async def _r(**kw): return next(_it, _st) if kw.get("tools") else _st
    mock_provider.complete.side_effect = _r

    await handler.process(_make_message())

    emitted = [c.args[0] for c in handler.events.emit.call_args_list]
    tr = next(e for e in emitted if e.type == EventType.TOOL_RESULT)
    assert tr.payload["success"] is False
    assert tr.payload["error"] is not None


# ---------------------------------------------------------------------------
# Error events
# ---------------------------------------------------------------------------


async def test_handler_emits_handler_error_on_timeout():
    handler, mock_provider = _make_mock_handler()
    mock_provider.complete.side_effect = ReasoningTimeoutError("timeout")

    await handler.process(_make_message())

    assert EventType.HANDLER_ERROR in _emitted_types(handler)


async def test_handler_emits_handler_error_on_connection_error():
    from kernos.kernel.exceptions import ReasoningConnectionError
    handler, mock_provider = _make_mock_handler()
    mock_provider.complete.side_effect = ReasoningConnectionError("connection failed")

    await handler.process(_make_message())

    assert EventType.HANDLER_ERROR in _emitted_types(handler)


async def test_handler_emits_handler_error_on_unexpected():
    handler, mock_provider = _make_mock_handler()
    mock_provider.complete.side_effect = RuntimeError("boom")

    await handler.process(_make_message())

    assert EventType.HANDLER_ERROR in _emitted_types(handler)


async def test_handler_error_event_has_error_type():
    handler, mock_provider = _make_mock_handler()
    mock_provider.complete.side_effect = ReasoningTimeoutError("timeout")

    await handler.process(_make_message())

    emitted = [c.args[0] for c in handler.events.emit.call_args_list]
    he = next(e for e in emitted if e.type == EventType.HANDLER_ERROR)
    assert he.payload["error_type"] == "ReasoningTimeoutError"
    assert "stage" in he.payload


# ---------------------------------------------------------------------------
# Tenant provisioning — real StateStore + EventStream (integration)
# ---------------------------------------------------------------------------


async def test_newinstance_gets_profile_and_default_contracts(tmp_path, monkeypatch):
    # POSTURE-SEEDED-COVENANTS-V1 (2026-05-22): pin against strict so
    # the full pre-change 9-rule seed lands. New default is minimal (5).
    monkeypatch.setenv("KERNOS_POSTURE_PROFILE", "strict")
    handler, mock_provider, events, state = _make_real_handler(tmp_path)
    mock_provider.complete.return_value = _mock_provider_response("Hello!")

    await handler.process(_make_message())

    instance_id = "sms:+15555550100"
    profile = await state.get_instance_profile(instance_id)
    assert profile is not None
    assert profile.status == "active"

    rules = await state.get_contract_rules(instance_id)
    assert len(rules) == 9  # spirit + 3 must_not + 2 must + 2 preference + 1 escalation


async def test_newinstance_emits_provisioned_event(tmp_path):
    handler, mock_provider, events, state = _make_real_handler(tmp_path)
    mock_provider.complete.return_value = _mock_provider_response("Hello!")

    await handler.process(_make_message())

    instance_id = "sms:+15555550100"
    emitted = await events.query(instance_id, limit=100)
    event_types = [e.type for e in emitted]
    assert EventType.TENANT_PROVISIONED in event_types


async def test_existinginstance_skips_provisioning(tmp_path):
    handler, mock_provider, events, state = _make_real_handler(tmp_path)
    mock_provider.complete.return_value = _mock_provider_response("Hello!")

    # First call provisions the instance
    await handler.process(_make_message())
    rules_after_first = await state.get_contract_rules("sms:+15555550100")

    # Second call must not double-seed contracts
    await handler.process(_make_message())
    rules_after_second = await state.get_contract_rules("sms:+15555550100")

    assert len(rules_after_second) == len(rules_after_first)


async def test_handler_updates_conversation_summary(tmp_path):
    handler, mock_provider, events, state = _make_real_handler(tmp_path)
    mock_provider.complete.return_value = _mock_provider_response("Hello!")

    await handler.process(_make_message())

    instance_id = "sms:+15555550100"
    summary = await state.get_conversation_summary(instance_id, "+15555550100")
    assert summary is not None
    assert summary.message_count >= 1
    assert summary.platform == "sms"


async def test_full_event_sequence_written_to_disk(tmp_path):
    """End-to-end: events emitted in process() are queryable from disk."""
    handler, mock_provider, events, state = _make_real_handler(tmp_path)
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    await handler.process(_make_message())

    instance_id = "sms:+15555550100"
    all_events = await events.query(instance_id, limit=100)
    types = [e.type for e in all_events]

    assert EventType.MESSAGE_RECEIVED in types
    assert EventType.REASONING_REQUEST in types
    assert EventType.REASONING_RESPONSE in types
    assert EventType.MESSAGE_SENT in types


async def test_disk_events_all_have_correct_instance_id(tmp_path):
    handler, mock_provider, events, state = _make_real_handler(tmp_path)
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    await handler.process(_make_message())

    instance_id = "sms:+15555550100"
    all_events = await events.query(instance_id, limit=100)
    assert len(all_events) > 0
    for event in all_events:
        assert event.instance_id == instance_id
