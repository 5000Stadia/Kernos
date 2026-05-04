from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.capability.registry import CapabilityInfo, CapabilityRegistry, CapabilityStatus
from kernos.kernel.engine import TaskEngine
from kernos.kernel.exceptions import (
    ReasoningConnectionError,
    ReasoningProviderError,
    ReasoningRateLimitError,
    ReasoningTimeoutError,
)
from kernos.kernel.reasoning import (
    ContentBlock,
    Provider,
    ProviderResponse,
    ReasoningService,
)
from kernos.kernel.task import TaskStatus, TaskType
from kernos.kernel.soul import Soul
from kernos.kernel.template import PRIMARY_TEMPLATE
from kernos.messages.handler import MessageHandler, _build_system_prompt
from kernos.messages.models import AuthLevel, NormalizedMessage
from kernos.capability.client import MCPClientManager
from kernos.persistence import AuditStore, ConversationStore, InstanceStore
from kernos.kernel.events import EventStream
from kernos.kernel.state import StateStore, InstanceProfile


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


def _make_mock_registry(tools: list[dict] | None = None) -> MagicMock:
    """Return a mock CapabilityRegistry.

    Builds a real CapabilityInfo for any provided tools, marking them as "read"
    by default so the dispatch gate classifies them correctly.
    """
    registry = MagicMock(spec=CapabilityRegistry)
    tools_list = tools or []
    registry.get_connected_tools.return_value = tools_list
    registry.get_tools_for_space.return_value = tools_list
    registry.build_capability_prompt.return_value = (
        "CURRENT CAPABILITIES — conversation only." if not tools_list
        else "CONNECTED CAPABILITIES — you can use these:\n- Calendar tools available."
    )
    registry.build_tool_directory.return_value = (
        "CAPABILITIES: None connected yet."
        if not tools_list
        else "CONNECTED SERVICES: Test Capability\nYour tools are listed in your tool definitions."
    )
    registry.get_preloaded_tools.return_value = tools_list  # All test tools are preloaded
    registry.get_lazy_tool_stubs.return_value = []  # No stubs in tests (all preloaded)
    registry.get_all_tool_names.return_value = {t["name"] for t in tools_list}
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
    return registry


def _make_handler(tools: list[dict] | None = None) -> tuple[MessageHandler, AsyncMock]:
    """Return a (handler, mock_provider) with mock MCP, stores, events, state, and registry."""
    mcp = MagicMock(spec=MCPClientManager)
    mcp.get_tools.return_value = tools or []

    conversations = AsyncMock(spec=ConversationStore)
    conversations.get_recent.return_value = []
    conversations.get_recent_full.return_value = []
    conversations.get_space_thread.return_value = []
    conversations.get_cross_domain_messages.return_value = []
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
    # Return an existing profile so provisioning is skipped in most tests
    state.get_instance_profile.return_value = InstanceProfile(
        instance_id="sms:+15555550100",
        status="active",
        created_at="2026-03-01T00:00:00Z",
    )
    state.get_conversation_summary.return_value = None
    state.save_conversation_summary.return_value = None
    state.save_instance_profile.return_value = None
    # Return a hatched soul with user_name set so name-ask doesn't fire.
    # Tests that specifically test first-interaction behavior use their own setup.
    state.get_soul.return_value = Soul(
        instance_id="sms:+15555550100",
        user_name="TestUser",
        hatched=True,
        interaction_count=5,
    )
    state.save_soul.return_value = None
    state.get_contract_rules.return_value = []
    state.query_covenant_rules.return_value = []
    state.list_context_spaces.return_value = []
    state.get_context_space.return_value = None
    state.get_knowledge_hashes.return_value = set()
    state.query_knowledge.return_value = []

    mock_provider = AsyncMock(spec=Provider)
    registry = _make_mock_registry(tools)
    reasoning = ReasoningService(mock_provider, events, mcp, audit)
    # TEST-INFRA-PARITY-V1 (2026-05-03): wire stub turn_runner_provider
    # so post-strike construction contract is honored.
    from tests._thin_path_test_fixture import wire_test_thin_path
    wire_test_thin_path(reasoning, provider=mock_provider, mcp=mcp)
    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(mcp, conversations, tenants, audit, events, state, reasoning, registry, engine)
    handler.preference_parsing_enabled = False  # Disable in tests — avoids consuming mock side_effects
    return handler, mock_provider


# --- Happy path ---


async def test_process_returns_string():
    handler, mock_provider = _make_handler()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert result == "Hi!"


# --- Error paths: each must return a friendly string, never raise ---


async def test_timeout_returns_friendly_string():
    handler, mock_provider = _make_handler()
    mock_provider.complete.side_effect = ReasoningTimeoutError("timeout")

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert "try again" in result.lower()


async def test_connection_error_returns_friendly_string():
    handler, mock_provider = _make_handler()
    mock_provider.complete.side_effect = ReasoningConnectionError("connection failed")

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert "try again" in result.lower()


async def test_rate_limit_returns_friendly_string():
    handler, mock_provider = _make_handler()
    mock_provider.complete.side_effect = ReasoningRateLimitError("rate limited")

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert "overloaded" in result.lower() or "minute" in result.lower()


async def test_api_status_error_returns_friendly_string():
    handler, mock_provider = _make_handler()
    mock_provider.complete.side_effect = ReasoningProviderError("API status 500: Internal Server Error")

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert "try again" in result.lower()


async def test_unexpected_error_returns_friendly_string():
    handler, mock_provider = _make_handler()
    mock_provider.complete.side_effect = RuntimeError("something broke")

    result = await handler.process(_make_message())
    assert isinstance(result, str)
    assert len(result) > 0


# --- System prompt ---


def test_system_prompt_includes_platform():
    base = dict(
        content="Hi",
        sender="+15555550100",
        sender_auth_level=AuthLevel.owner_unverified,
        platform_capabilities=["text"],
        conversation_id="+15555550100",
        timestamp=datetime.now(timezone.utc),
        instance_id="+15555550100",
    )

    cap_prompt = "CURRENT CAPABILITIES — conversation only."
    soul = Soul(instance_id="t1")

    discord_msg = NormalizedMessage(**base, platform="discord")
    assert "Discord" in _build_system_prompt(discord_msg, cap_prompt, soul, PRIMARY_TEMPLATE, [])
    assert "SMS" not in _build_system_prompt(discord_msg, cap_prompt, soul, PRIMARY_TEMPLATE, [])

    sms_msg = NormalizedMessage(**base, platform="sms")
    sms_prompt = _build_system_prompt(sms_msg, cap_prompt, soul, PRIMARY_TEMPLATE, [])
    assert "SMS" in sms_prompt
    # SMS posture now references Discord as a cross-channel suggestion target
    assert "send_to_channel" in sms_prompt


def test_system_prompt_includes_capability_prompt():
    msg = _make_message()
    soul = Soul(instance_id="t1")
    cap_prompt = "CONNECTED CAPABILITIES — you can use these:\n- Google Calendar."
    prompt = _build_system_prompt(msg, cap_prompt, soul, PRIMARY_TEMPLATE, [])
    assert "CONNECTED CAPABILITIES" in prompt
    assert "Google Calendar" in prompt


def test_system_prompt_includes_conversation_only_when_no_caps():
    msg = _make_message()
    soul = Soul(instance_id="t1")
    cap_prompt = "CURRENT CAPABILITIES — conversation only."
    prompt = _build_system_prompt(msg, cap_prompt, soul, PRIMARY_TEMPLATE, [])
    assert "conversation only" in prompt.lower()


def test_system_prompt_does_not_claim_cannot_remember():
    msg = _make_message()
    soul = Soul(instance_id="t1")
    prompt = _build_system_prompt(msg, "CURRENT CAPABILITIES — conversation only.", soul, PRIMARY_TEMPLATE, [])
    assert "cannot remember previous conversations" not in prompt


def test_handler_uses_registry_for_capability_prompt():
    """Verify handler calls registry.build_tool_directory() for lazy loading."""
    handler, mock_provider = _make_handler()
    handler.registry.build_tool_directory.return_value = "CONNECTED SERVICES: Custom\nYour tools are listed in your tool definitions."
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    import asyncio
    asyncio.get_event_loop().run_until_complete(handler.process(_make_message()))

    handler.registry.build_tool_directory.assert_called()


# --- MCP tool-use loop ---


async def test_handler_with_tool_use_brokers_call_and_returns_text():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_provider = _make_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Meeting at 10am")

    # Route responses: reasoning calls (with tools) get real responses;
    # cohort agents (no tools) get stub text.
    _tool_resp = _mock_provider_tool_response("list_events", "tu_001", {"date": "2026-03-01"})
    _text_resp = _mock_provider_response("You have a meeting at 10am.")
    _stub_resp = _mock_provider_response("{}")
    _reasoning_calls = iter([_tool_resp, _text_resp])

    async def _route_complete(**kwargs):
        if kwargs.get("tools"):
            return next(_reasoning_calls, _stub_resp)
        return _stub_resp

    mock_provider.complete.side_effect = _route_complete

    result = await handler.process(_make_message("What's on my schedule?"))
    assert "10am" in result
    handler.mcp.call_tool.assert_awaited_once_with("list_events", {"date": "2026-03-01"})


async def test_handler_safety_valve_returns_graceful_message():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_provider = _make_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="some result")

    _tool_resp = _mock_provider_tool_response("list_events", "tu_001", {})
    _stub = _mock_provider_response("{}")

    async def _route(**kwargs):
        return _tool_resp if kwargs.get("tools") else _stub

    mock_provider.complete.side_effect = _route

    result = await handler.process(_make_message("What's on my schedule?"))
    assert isinstance(result, str)
    assert "trouble" in result.lower() or "simpler" in result.lower()


async def test_handler_tool_failure_results_in_graceful_response():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_provider = _make_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Calendar tool error: connection failed")

    _tool_resp = _mock_provider_tool_response("list_events", "tu_001", {})
    _text_resp = _mock_provider_response("I had trouble checking your calendar.")
    _stub = _mock_provider_response("{}")
    _iter = iter([_tool_resp, _text_resp])

    async def _route(**kwargs):
        return next(_iter, _stub) if kwargs.get("tools") else _stub

    mock_provider.complete.side_effect = _route

    result = await handler.process(_make_message("What's on my schedule?"))
    assert isinstance(result, str)
    assert len(result) > 0


async def test_handler_no_tools_works_identically_to_1a2():
    handler, mock_provider = _make_handler(tools=[])

    _text = _mock_provider_response("Hello!")
    _stub = _mock_provider_response("{}")
    async def _route(**kwargs):
        return _text if kwargs.get("tools") else _stub
    mock_provider.complete.side_effect = _route

    result = await handler.process(_make_message())
    assert result == "Hello!"


# --- Persistence (AC19) ---


async def test_handler_stores_user_and_assistant_messages():
    handler, mock_provider = _make_handler()
    mock_provider.complete.return_value = _mock_provider_response("I'm good, thanks!")

    await handler.process(_make_message("How are you?"))

    assert handler.conversations.append.await_count == 2
    calls = handler.conversations.append.await_args_list

    user_entry = calls[0][0][2]
    assert user_entry["role"] == "user"
    assert user_entry["content"] == "How are you?"
    assert "instance_id" in user_entry

    assistant_entry = calls[1][0][2]
    assert assistant_entry["role"] == "assistant"
    assert assistant_entry["content"] == "I'm good, thanks!"
    assert "instance_id" in assistant_entry


async def test_handler_tool_calls_go_to_audit_not_conversation():
    tools = [{"name": "list_events", "description": "List events", "input_schema": {}}]
    handler, mock_provider = _make_handler(tools=tools)
    handler.mcp.call_tool = AsyncMock(return_value="Meeting at 10am")

    _tool_resp = _mock_provider_tool_response("list_events", "tu_001", {"date": "2026-03-01"})
    _text_resp = _mock_provider_response("You have a meeting at 10am.")
    _stub = _mock_provider_response("{}")
    _iter = iter([_tool_resp, _text_resp])

    async def _route(**kwargs):
        return next(_iter, _stub) if kwargs.get("tools") else _stub

    mock_provider.complete.side_effect = _route

    await handler.process(_make_message("What's on my schedule?"))

    assert handler.conversations.append.await_count == 2
    assert handler.audit.log.await_count == 2
    audit_calls = handler.audit.log.await_args_list
    audit_types = [c[0][1]["type"] for c in audit_calls]
    assert "tool_call" in audit_types
    assert "tool_result" in audit_types


async def test_handler_loads_history_into_messages():
    handler, mock_provider = _make_handler()
    # In v2, message history comes from get_space_thread (via _assemble_space_context)
    handler.conversations.get_space_thread.return_value = [
        {"role": "user", "content": "My name is Alice"},
        {"role": "assistant", "content": "Nice to meet you, Alice!"},
    ]
    mock_provider.complete.return_value = _mock_provider_response("Your name is Alice.")

    await handler.process(_make_message("What's my name?"))

    complete_call = mock_provider.complete.call_args
    messages_arg = complete_call.kwargs.get("messages") or complete_call[1].get("messages")
    assert messages_arg is not None
    assert len(messages_arg) == 3
    assert messages_arg[0]["content"] == "My name is Alice"
    assert messages_arg[2]["content"] == "What's my name?"


async def test_handler_calls_get_or_create_for_every_message():
    handler, mock_provider = _make_handler()
    mock_provider.complete.return_value = _mock_provider_response("Hello!")

    await handler.process(_make_message())
    handler.tenants.get_or_create.assert_awaited_once()


# --- Task creation ---


async def test_handler_creates_task_via_engine():
    """Handler delegates to TaskEngine — mock engine to verify task fields."""
    mcp = MagicMock(spec=MCPClientManager)
    conversations = AsyncMock(spec=ConversationStore)
    conversations.get_recent.return_value = []
    conversations.get_recent_full.return_value = []
    conversations.get_space_thread.return_value = []
    conversations.get_cross_domain_messages.return_value = []
    conversations.append.return_value = None
    tenants = AsyncMock(spec=InstanceStore)
    tenants.get_or_create.return_value = {"instance_id": "sms:+15555550100", "status": "active", "created_at": "2026-03-01T00:00:00Z", "capabilities": {}}
    audit = AsyncMock(spec=AuditStore)
    events = AsyncMock(spec=EventStream)
    events.emit.return_value = None
    state = AsyncMock(spec=StateStore)
    from kernos.kernel.state import InstanceProfile
    state.get_instance_profile.return_value = InstanceProfile(instance_id="sms:+15555550100", status="active", created_at="2026-03-01T00:00:00Z")
    state.get_conversation_summary.return_value = None
    state.save_conversation_summary.return_value = None
    state.save_instance_profile.return_value = None
    state.get_soul.return_value = None
    state.save_soul.return_value = None
    state.get_contract_rules.return_value = []
    state.query_covenant_rules.return_value = []
    state.list_context_spaces.return_value = []
    state.get_context_space.return_value = None

    mock_provider = AsyncMock(spec=Provider)
    registry = _make_mock_registry()
    reasoning = ReasoningService(mock_provider, events, mcp, audit)

    # Use a mock engine to capture what task was passed
    mock_engine = AsyncMock(spec=TaskEngine)
    from kernos.kernel.task import Task, TaskStatus
    captured_task = Task(
        id="task_captured",
        type=TaskType.REACTIVE_SIMPLE,
        instance_id="sms:+15555550100",
        conversation_id="+15555550100",
        status=TaskStatus.COMPLETED,
        result_text="Mock response",
    )
    mock_engine.execute.return_value = captured_task

    handler = MessageHandler(mcp, conversations, tenants, audit, events, state, reasoning, registry, mock_engine)

    await handler.process(_make_message("Hello from handler"))

    mock_engine.execute.assert_awaited_once()
    call_args = mock_engine.execute.call_args
    task_arg = call_args[0][0]  # first positional arg
    assert task_arg.type == TaskType.REACTIVE_SIMPLE
    assert task_arg.instance_id == "sms:+15555550100"
    assert task_arg.source == "user_message"
    assert task_arg.input_text == "Hello from handler"


async def test_handler_uses_task_result_text_as_response():
    """Handler reads task.result_text, not result.text."""
    mcp = MagicMock(spec=MCPClientManager)
    conversations = AsyncMock(spec=ConversationStore)
    conversations.get_recent.return_value = []
    conversations.get_recent_full.return_value = []
    conversations.get_space_thread.return_value = []
    conversations.get_cross_domain_messages.return_value = []
    conversations.append.return_value = None
    tenants = AsyncMock(spec=InstanceStore)
    tenants.get_or_create.return_value = {"instance_id": "sms:+15555550100", "status": "active", "created_at": "2026-03-01T00:00:00Z", "capabilities": {}}
    audit = AsyncMock(spec=AuditStore)
    events = AsyncMock(spec=EventStream)
    events.emit.return_value = None
    state = AsyncMock(spec=StateStore)
    from kernos.kernel.state import InstanceProfile
    state.get_instance_profile.return_value = InstanceProfile(instance_id="sms:+15555550100", status="active", created_at="2026-03-01T00:00:00Z")
    state.get_conversation_summary.return_value = None
    state.save_conversation_summary.return_value = None
    state.save_instance_profile.return_value = None
    state.get_soul.return_value = Soul(
        instance_id="sms:+15555550100", user_name="TestUser", hatched=True, interaction_count=5
    )
    state.save_soul.return_value = None
    state.get_contract_rules.return_value = []
    state.query_covenant_rules.return_value = []
    state.list_context_spaces.return_value = []
    state.get_context_space.return_value = None
    state.get_knowledge_hashes.return_value = set()
    state.query_knowledge.return_value = []

    mock_provider = AsyncMock(spec=Provider)
    registry = _make_mock_registry()
    reasoning = ReasoningService(mock_provider, events, mcp, audit)

    mock_engine = AsyncMock(spec=TaskEngine)
    from kernos.kernel.task import Task, TaskStatus
    mock_engine.execute.return_value = Task(
        id="task_x",
        type=TaskType.REACTIVE_SIMPLE,
        instance_id="sms:+15555550100",
        conversation_id="+15555550100",
        status=TaskStatus.COMPLETED,
        result_text="The answer from the task",
    )

    handler = MessageHandler(mcp, conversations, tenants, audit, events, state, reasoning, registry, mock_engine)

    response = await handler.process(_make_message())
    assert response == "The answer from the task"


# ---------------------------------------------------------------------------
# Selective knowledge injection (SPEC-SELECTIVE-KNOWLEDGE-INJECTION)
# ---------------------------------------------------------------------------


from kernos.messages.handler import _is_stale_knowledge


class TestStaleKnowledgeCheck:
    def test_recent_not_stale(self):
        from kernos.utils import utc_now
        entry = MagicMock()
        entry.last_referenced = utc_now()
        assert not _is_stale_knowledge(entry, days=14)

    def test_old_is_stale(self):
        entry = MagicMock()
        entry.last_referenced = "2020-01-01T00:00:00+00:00"
        assert _is_stale_knowledge(entry, days=14)

    def test_no_reference_not_stale(self):
        entry = MagicMock()
        entry.last_referenced = ""
        assert not _is_stale_knowledge(entry, days=14)

    def test_missing_attr_not_stale(self):
        entry = MagicMock(spec=[])
        assert not _is_stale_knowledge(entry, days=14)


class TestTopicHint:
    def test_empty_messages(self):
        from kernos.messages.handler import MessageHandler, TurnContext
        handler = MagicMock(spec=MessageHandler)
        handler._get_recent_context_summary = MessageHandler._get_recent_context_summary.__get__(handler)
        ctx = TurnContext(messages=[])
        assert handler._get_recent_context_summary(ctx) == "new conversation"

    def test_extracts_from_recent(self):
        from kernos.messages.handler import MessageHandler, TurnContext
        handler = MagicMock(spec=MessageHandler)
        handler._get_recent_context_summary = MessageHandler._get_recent_context_summary.__get__(handler)
        ctx = TurnContext(messages=[
            {"role": "user", "content": "Tell me about guitar"},
            {"role": "assistant", "content": "Guitar is great"},
        ])
        hint = handler._get_recent_context_summary(ctx)
        assert "guitar" in hint.lower()


# --- /dump diagnostic command ---


async def test_dump_writes_context_file(tmp_path, monkeypatch):
    """'/dump' writes assembled context to a diagnostics file and skips reasoning."""
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    handler, mock_provider = _make_handler()

    result = await handler.process(_make_message(content="/dump"))

    # Reasoning model should NOT have been called
    mock_provider.complete.assert_not_called()

    # Response tells the user where the file is
    assert "Context dumped to" in result
    assert "diagnostics" in result

    # File was written and contains expected sections
    diag_dir = tmp_path / "diagnostics"
    assert diag_dir.exists()
    dump_files = list(diag_dir.glob("context_*.txt"))
    assert len(dump_files) == 1

    content = dump_files[0].read_text()
    assert "=== SYSTEM PROMPT ===" in content
    assert "=== MESSAGES ===" in content
    assert "=== TOOLS ===" in content
    assert "=== SUMMARY ===" in content
    # Summary should contain token estimates and tool count
    assert "tokens" in content
    assert "schemas" in content


async def test_dump_does_not_persist_message(tmp_path, monkeypatch):
    """/dump should not be stored in conversation history."""
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    handler, mock_provider = _make_handler()

    await handler.process(_make_message(content="/dump"))

    # conversations.append should NOT have been called (no user message persisted)
    handler.conversations.append.assert_not_called()


async def test_dump_case_insensitive():
    """/DUMP and /Dump both trigger the diagnostic."""
    handler, mock_provider = _make_handler()

    for variant in ["/DUMP", " /dump ", "/Dump"]:
        result = await handler.process(_make_message(content=variant))
        assert "Context dumped to" in result
    mock_provider.complete.assert_not_called()


# --- SPEC-LIVE-AUDIT-POLISH-4D ---


class TestGateRequestToolBypass:
    """request_tool is a meta-tool — should be classified as read."""

    def test_request_tool_classified_as_read(self):
        from kernos.kernel.gate import DispatchGate
        gate = DispatchGate(
            reasoning_service=MagicMock(), registry=MagicMock(),
            state=MagicMock(), events=MagicMock(),
        )
        effect = gate.classify_tool_effect("request_tool", None)
        assert effect == "read"


class TestDepartureContext:
    """Space-switch departure context bridge."""

    async def test_departure_context_on_space_switch(self):
        handler, mock_provider = _make_handler()
        mock_provider.complete.return_value = _mock_provider_response("Got it!")

        handler.conv_logger = AsyncMock()
        handler.conv_logger.read_recent.return_value = [
            {"role": "user", "content": "My sister is Tina", "timestamp": "T1", "channel": "discord"},
            {"role": "assistant", "content": "Got it — Tina is your sister", "timestamp": "T2", "channel": "discord"},
        ]

        from kernos.kernel.spaces import ContextSpace
        from kernos.messages.handler import TurnContext
        ctx_obj = TurnContext()
        ctx_obj.instance_id = "test-tenant"
        ctx_obj.active_space_id = "space-b"
        ctx_obj.active_space = ContextSpace(id="space-b", instance_id="test-tenant", name="Architecture")

        handler.state.get_context_space.return_value = ContextSpace(id="space-a", instance_id="test-tenant", name="General")

        result = await handler._build_departure_context(ctx_obj, "space-a")

        assert result is not None
        assert result["role"] == "user"
        assert "Previous context — from space: General" in result["content"]
        assert "Tina" in result["content"]
        assert "Conversation continues in current space: Architecture" in result["content"]

    async def test_no_departure_context_without_switch(self):
        handler, _ = _make_handler()
        from kernos.messages.handler import TurnContext
        ctx_obj = TurnContext()
        ctx_obj.instance_id = "test-tenant"
        ctx_obj.active_space_id = "space-a"

        result = await handler._build_departure_context(ctx_obj, "space-a")
        assert result is None

    async def test_no_departure_context_when_empty(self):
        handler, _ = _make_handler()
        handler.conv_logger = AsyncMock()
        handler.conv_logger.read_recent.return_value = []

        from kernos.messages.handler import TurnContext
        from kernos.kernel.spaces import ContextSpace
        ctx_obj = TurnContext()
        ctx_obj.instance_id = "test-tenant"
        ctx_obj.active_space_id = "space-b"
        ctx_obj.active_space = ContextSpace(id="space-b", instance_id="test-tenant", name="Other")

        result = await handler._build_departure_context(ctx_obj, "space-a")
        assert result is None

    async def test_departure_context_respects_char_budget(self):
        handler, _ = _make_handler()
        handler.conv_logger = AsyncMock()
        handler.conv_logger.read_recent.return_value = [
            {"role": "user", "content": "A" * 300, "timestamp": f"T{i}", "channel": "discord"}
            for i in range(6)
        ]

        handler.state.get_context_space.return_value = None

        from kernos.messages.handler import TurnContext
        from kernos.kernel.spaces import ContextSpace
        ctx_obj = TurnContext()
        ctx_obj.instance_id = "test-tenant"
        ctx_obj.active_space_id = "space-b"
        ctx_obj.active_space = ContextSpace(id="space-b", instance_id="test-tenant", name="Other")

        result = await handler._build_departure_context(ctx_obj, "space-a")
        assert result is not None
        # With 300 chars each and 1200 budget, should get at most 4 entries
        entry_lines = [l for l in result["content"].split("\n") if l.startswith("[User]:") or l.startswith("[Assistant]:")]
        assert len(entry_lines) <= 4


# ---------------------------------------------------------------------------
# Turn Serialization (SPEC-TURN-SERIALIZATION)
# ---------------------------------------------------------------------------

from kernos.messages.handler import SpaceRunner, MERGE_WINDOW_MS
import asyncio


async def test_single_message_no_contention():
    """AC13: Single-message turns work normally with no behavioral change."""
    handler, mock_provider = _make_handler()
    mock_provider.complete.return_value = _mock_provider_response("Hello!")

    result = await handler.process(_make_message("Hi"))
    assert result == "Hello!"
    await handler.shutdown_runners()


async def test_turn_serialization_prevents_concurrent_turns():
    """AC1: Per-space runner serializes turns — no concurrent execution."""
    handler, mock_provider = _make_handler()

    execution_log = []

    original_assemble = handler._phase_assemble

    async def _tracking_assemble(ctx):
        execution_log.append(("assemble_start", ctx.message.content))
        await original_assemble(ctx)
        # Simulate slow assembly
        await asyncio.sleep(0.05)
        execution_log.append(("assemble_end", ctx.message.content))

    handler._phase_assemble = _tracking_assemble

    mock_provider.complete.return_value = _mock_provider_response("Done.")

    # Send two messages concurrently — they should serialize
    msg1 = _make_message("First")
    msg2 = _make_message("Second")

    t1 = asyncio.create_task(handler.process(msg1))
    # Small delay so msg1 enters the runner first
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(handler.process(msg2))

    r1, r2 = await asyncio.gather(t1, t2)

    # First message gets a real response
    assert r1 == "Done."
    # Second message was merged (gets empty string)
    assert r2 == ""

    await handler.shutdown_runners()


async def test_merged_messages_logged_to_conversation(caplog, tmp_path):
    """AC4: Merged messages are logged to conversation log so agent sees them."""
    import logging
    handler, mock_provider = _make_handler()
    mock_provider.complete.return_value = _mock_provider_response("Got it.")

    # Replace conv_logger with a mock so we can track calls
    mock_conv_logger = AsyncMock()
    handler.conv_logger = mock_conv_logger

    msg1 = _make_message("First message")
    msg2 = _make_message("Second message")

    t1 = asyncio.create_task(handler.process(msg1))
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(handler.process(msg2))

    await asyncio.gather(t1, t2)
    await handler.shutdown_runners()

    # conv_logger.append should have been called for the merged message
    append_calls = mock_conv_logger.append.call_args_list
    merged_found = any(
        "Second message" in str(c)
        for c in append_calls
    )
    assert merged_found, f"Expected merged message in conv_logger.append calls: {append_calls}"


async def test_different_spaces_run_concurrently():
    """AC8: Messages to different spaces can process concurrently."""
    handler, mock_provider = _make_handler()

    # Track execution timing
    execution_order = []

    original_assemble = handler._phase_assemble

    async def _tracking_assemble(ctx):
        execution_order.append(("start", ctx.active_space_id))
        await original_assemble(ctx)
        await asyncio.sleep(0.05)  # Simulate work
        execution_order.append(("end", ctx.active_space_id))

    handler._phase_assemble = _tracking_assemble
    mock_provider.complete.return_value = _mock_provider_response("Done.")

    # Force different space IDs by patching _phase_route
    original_route = handler._phase_route

    call_count = 0
    async def _route_to_different_spaces(ctx):
        nonlocal call_count
        await original_route(ctx)
        call_count += 1
        ctx.active_space_id = f"space-{call_count}"

    handler._phase_route = _route_to_different_spaces

    msg1 = _make_message("Task A")
    msg2 = _make_message("Task B")

    # Both should start before either finishes (different spaces = different runners)
    t1 = asyncio.create_task(handler.process(msg1))
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(handler.process(msg2))

    r1, r2 = await asyncio.gather(t1, t2)

    # Both get real responses (not merged — different spaces)
    assert r1 == "Done."
    assert r2 == "Done."

    await handler.shutdown_runners()


async def test_empty_string_for_merged_messages():
    """AC9: Merged messages return empty string so adapter sends nothing."""
    handler, mock_provider = _make_handler()
    mock_provider.complete.return_value = _mock_provider_response("Response.")

    msg1 = _make_message("Main")
    msg2 = _make_message("Follow-up 1")
    msg3 = _make_message("Follow-up 2")

    t1 = asyncio.create_task(handler.process(msg1))
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(handler.process(msg2))
    t3 = asyncio.create_task(handler.process(msg3))

    r1, r2, r3 = await asyncio.gather(t1, t2, t3)

    # Primary gets the response, merged get empty
    assert r1 == "Response."
    assert r2 == ""
    assert r3 == ""

    await handler.shutdown_runners()


async def test_shutdown_runners_resolves_pending():
    """Clean shutdown resolves pending futures."""
    handler, mock_provider = _make_handler()
    # Create a runner but don't process anything — just verify shutdown is clean
    runner = handler._get_runner("test-tenant", "test-space")
    assert runner._task is not None
    assert not runner._task.done()

    await handler.shutdown_runners()
    assert len(handler._runners) == 0


async def test_runner_handles_reasoning_error():
    """Runner handles reasoning errors gracefully and resolves futures."""
    handler, mock_provider = _make_handler()
    mock_provider.complete.side_effect = ReasoningTimeoutError("timeout")

    result = await handler.process(_make_message("will fail"))
    assert isinstance(result, str)
    assert "try again" in result.lower()

    await handler.shutdown_runners()


async def test_turn_submitted_logging(caplog):
    """AC10: TURN_SUBMITTED log line emitted."""
    import logging
    handler, mock_provider = _make_handler()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    with caplog.at_level(logging.INFO):
        await handler.process(_make_message("Test"))

    assert any("TURN_SUBMITTED" in rec.message for rec in caplog.records)
    await handler.shutdown_runners()


# ---------------------------------------------------------------------------
# Cohort Trace Enrichment (Closeout Fix 3)
# ---------------------------------------------------------------------------


async def test_route_input_logged(caplog):
    """Fix 3: ROUTE_INPUT log line emitted before routing."""
    import logging
    handler, mock_provider = _make_handler()
    mock_provider.complete.return_value = _mock_provider_response("Hi!")

    with caplog.at_level(logging.INFO):
        await handler.process(_make_message("What time is it?"))

    assert any("ROUTE_INPUT" in rec.message for rec in caplog.records)
    route_input_rec = [r for r in caplog.records if "ROUTE_INPUT" in r.message][0]
    assert "What time is it?" in route_input_rec.message
    await handler.shutdown_runners()
