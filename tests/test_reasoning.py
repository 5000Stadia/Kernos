"""Tests for ReasoningService and AnthropicProvider."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest

from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream
from kernos.kernel.exceptions import (
    ReasoningConnectionError,
    ReasoningProviderError,
    ReasoningRateLimitError,
    ReasoningTimeoutError,
)
from kernos.capability.registry import CapabilityInfo, CapabilityRegistry, CapabilityStatus
from kernos.kernel.reasoning import (
    AnthropicProvider,
    ContentBlock,
    Provider,
    ProviderResponse,
    ReasoningRequest,
    ReasoningResult,
    ReasoningService,
)
from kernos.kernel.scheduler import MANAGE_SCHEDULE_TOOL
from kernos.persistence import AuditStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(**kwargs) -> ReasoningRequest:
    defaults = dict(
        instance_id="sms:+15555550100",
        conversation_id="+15555550100",
        system_prompt="You are Kernos.",
        messages=[{"role": "user", "content": "Hello"}],
        tools=[],
        model="claude-sonnet-4-6",
        trigger="user_message",
    )
    defaults.update(kwargs)
    return ReasoningRequest(**defaults)


def _make_service(mock_provider=None, tools_for_mcp=None, read_tool_names: list[str] | None = None):
    """Return a ReasoningService with mock provider, events, mcp, audit.

    read_tool_names: MCP tool names to register as "read" in the dispatch gate registry.
    These bypass the gate automatically. Used for tests that exercise MCP tool routing.
    """
    if mock_provider is None:
        mock_provider = AsyncMock(spec=Provider)

    events = AsyncMock(spec=EventStream)
    events.emit.return_value = None

    mcp = MagicMock()
    mcp.call_tool = AsyncMock(return_value="tool result")

    audit = AsyncMock(spec=AuditStore)
    audit.log.return_value = None

    service = ReasoningService(mock_provider, events, mcp, audit)

    # TEST-INFRA-PARITY-V1 (2026-05-03): wire stub turn_runner_provider
    # so post-strike construction contract is honored. Runs a minimal
    # tool-use loop against the mocks.
    from tests._thin_path_test_fixture import wire_test_thin_path
    wire_test_thin_path(service, provider=mock_provider, mcp=mcp)

    # Wire a registry so the dispatch gate can classify tools
    if read_tool_names:
        cap = CapabilityInfo(
            name="test-capability",
            display_name="Test Capability",
            description="Test",
            category="test",
            status=CapabilityStatus.CONNECTED,
            tools=read_tool_names,
            server_name="test",
            tool_effects={name: "read" for name in read_tool_names},
        )
        registry = CapabilityRegistry(mcp=None)
        registry.register(cap)
        service.set_registry(registry)

    return service, mock_provider, events, mcp, audit


def _text_response(text: str) -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="text", text=text)],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=20,
    )


def _tool_response(name: str, id: str, input: dict) -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="tool_use", name=name, id=id, input=input)],
        stop_reason="tool_use",
        input_tokens=15,
        output_tokens=5,
    )


# ---------------------------------------------------------------------------
# ReasoningService — basic behavior
# ---------------------------------------------------------------------------


async def test_reason_returns_text_on_simple_response():
    service, mock_provider, events, mcp, audit = _make_service()
    mock_provider.complete.return_value = _text_response("Hello there!")

    result = await service.reason(_make_request())

    assert isinstance(result, ReasoningResult)
    assert result.text == "Hello there!"
    assert result.input_tokens == 10
    assert result.output_tokens == 20
    assert result.tool_iterations == 0


async def test_reason_tool_use_loop_calls_mcp_and_continues():
    service, mock_provider, events, mcp, audit = _make_service(read_tool_names=["list_events"])
    mcp.call_tool.return_value = "Meeting at 10am"

    mock_provider.complete.side_effect = [
        _tool_response("list_events", "tu_001", {"date": "2026-03-01"}),
        _text_response("You have a meeting at 10am."),
    ]

    result = await service.reason(
        _make_request(
            tools=[{"name": "list_events", "description": "List events", "input_schema": {}}]
        )
    )

    assert result.text == "You have a meeting at 10am."
    assert result.tool_iterations == 1
    mcp.call_tool.assert_awaited_once_with("list_events", {"date": "2026-03-01"})


async def test_reason_safety_valve_at_max_iterations():
    service, mock_provider, events, mcp, audit = _make_service(read_tool_names=["some_tool"])
    # Always return tool_use to trigger safety valve
    mock_provider.complete.return_value = _tool_response("some_tool", "tu_x", {})
    mcp.call_tool.return_value = "result"

    result = await service.reason(
        _make_request(
            tools=[{"name": "some_tool", "description": "A tool", "input_schema": {}}]
        )
    )

    assert result.tool_iterations == ReasoningService.MAX_TOOL_ITERATIONS
    assert "trouble" in result.text.lower() or "simpler" in result.text.lower()


# ---------------------------------------------------------------------------
# ReasoningService — event emission
# ---------------------------------------------------------------------------


async def test_reason_emits_reasoning_request_and_response():
    service, mock_provider, events, mcp, audit = _make_service()
    mock_provider.complete.return_value = _text_response("Hi!")

    await service.reason(_make_request())

    emitted_types = [c.args[0].type for c in events.emit.call_args_list]
    assert EventType.REASONING_REQUEST in emitted_types
    assert EventType.REASONING_RESPONSE in emitted_types


async def test_reason_emits_tool_called_and_result():
    service, mock_provider, events, mcp, audit = _make_service(read_tool_names=["my_tool"])
    mcp.call_tool.return_value = "some data"

    mock_provider.complete.side_effect = [
        _tool_response("my_tool", "tu_001", {"key": "val"}),
        _text_response("Done."),
    ]

    await service.reason(
        _make_request(
            tools=[{"name": "my_tool", "description": "My tool", "input_schema": {}}]
        )
    )

    emitted_types = [c.args[0].type for c in events.emit.call_args_list]
    assert EventType.TOOL_CALLED in emitted_types
    assert EventType.TOOL_RESULT in emitted_types


async def test_reason_reasoning_response_has_token_counts():
    service, mock_provider, events, mcp, audit = _make_service()
    mock_provider.complete.return_value = _text_response("Hi!")

    await service.reason(_make_request())

    emitted = [c.args[0] for c in events.emit.call_args_list]
    rr = next(e for e in emitted if e.type == EventType.REASONING_RESPONSE)
    assert rr.payload["input_tokens"] == 10
    assert rr.payload["output_tokens"] == 20
    assert "estimated_cost_usd" in rr.payload
    assert "duration_ms" in rr.payload


# ---------------------------------------------------------------------------
# AnthropicProvider — exception mapping
# ---------------------------------------------------------------------------


def _mock_stream_error(exc):
    """Return a mock messages.stream that raises *exc* on __aenter__."""
    @asynccontextmanager
    async def _stream(**kwargs):
        raise exc
        yield  # pragma: no cover — makes this an async generator
    return _stream


def _mock_stream_ok(response):
    """Return a mock messages.stream that yields a stream whose get_final_message returns *response*."""
    @asynccontextmanager
    async def _stream(**kwargs):
        stream = MagicMock()
        stream.get_final_message = AsyncMock(return_value=response)
        yield stream
    return _stream


async def test_anthropic_provider_maps_timeout_to_reasoning_error():
    mock_client = MagicMock()
    mock_client.messages.stream = _mock_stream_error(
        anthropic.APITimeoutError(request=MagicMock())
    )

    with patch("kernos.providers.anthropic_provider.anthropic.AsyncAnthropic", return_value=mock_client):
        provider = AnthropicProvider(api_key="test")
        with pytest.raises(ReasoningTimeoutError):
            await provider.complete("model", "system", [], [], 1024)


async def test_anthropic_provider_maps_connection_error():
    mock_client = MagicMock()
    mock_client.messages.stream = _mock_stream_error(
        anthropic.APIConnectionError(request=MagicMock())
    )

    with patch("kernos.providers.anthropic_provider.anthropic.AsyncAnthropic", return_value=mock_client):
        provider = AnthropicProvider(api_key="test")
        with pytest.raises(ReasoningConnectionError):
            await provider.complete("model", "system", [], [], 1024)


async def test_anthropic_provider_maps_rate_limit_error():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.headers = {}
    mock_client.messages.stream = _mock_stream_error(
        anthropic.RateLimitError(
            message="rate limited", response=mock_response, body=None
        )
    )

    with patch("kernos.providers.anthropic_provider.anthropic.AsyncAnthropic", return_value=mock_client):
        provider = AnthropicProvider(api_key="test")
        with pytest.raises(ReasoningRateLimitError):
            await provider.complete("model", "system", [], [], 1024)


async def test_anthropic_provider_maps_api_status_error():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.headers = {}
    mock_client.messages.stream = _mock_stream_error(
        anthropic.APIStatusError(
            message="Internal Server Error", response=mock_response, body=None
        )
    )

    with patch("kernos.providers.anthropic_provider.anthropic.AsyncAnthropic", return_value=mock_client):
        provider = AnthropicProvider(api_key="test")
        with pytest.raises(ReasoningProviderError):
            await provider.complete("model", "system", [], [], 1024)


async def test_anthropic_provider_returns_provider_response_on_success():
    mock_client = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "Hello!"
    mock_client.messages.stream = _mock_stream_ok(MagicMock(
        content=[text_block],
        stop_reason="end_turn",
        usage=MagicMock(input_tokens=5, output_tokens=10),
    ))

    with patch("kernos.providers.anthropic_provider.anthropic.AsyncAnthropic", return_value=mock_client):
        provider = AnthropicProvider(api_key="test")
        result = await provider.complete("model", "system", [], [], 1024)

    assert isinstance(result, ProviderResponse)
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert result.content[0].text == "Hello!"
    assert result.stop_reason == "end_turn"
    assert result.input_tokens == 5
    assert result.output_tokens == 10


# ---------------------------------------------------------------------------
# Hallucination detector (observe-only mode)
# ---------------------------------------------------------------------------


class TestHallucinationDetector:
    """Tests for the hallucination detector in hands-off mode.

    The detector logs warnings and runs Haiku analysis but does NOT
    intervene — the agent's response passes through unchanged.
    """

    async def test_hallucination_detected_but_response_passes_through(self):
        """Agent hallucinates — detector logs but response reaches user unchanged."""
        hallucinated = _text_response("I scheduled your reminder! ✅")
        analysis = _text_response("Analysis: tool not called.")  # diagnostic

        service, mock_provider, events, mcp, audit = _make_service()
        mock_provider.complete.side_effect = [hallucinated, analysis]
        request = _make_request()
        result = await service.reason(request)

        # Response passes through unchanged (hands-off mode)
        assert "✅" in result.text
        assert "scheduled" in result.text.lower()
        # Only 2 calls: original + diagnostic analysis (no coaching retries)
        assert mock_provider.complete.call_count == 2

    async def test_no_detection_for_normal_response(self):
        """Normal text response without tool-claiming language is not flagged."""
        normal = _text_response("Sure, I can help with that. What would you like?")

        service, mock_provider, events, mcp, audit = _make_service()
        mock_provider.complete.return_value = normal
        request = _make_request()
        result = await service.reason(request)

        assert result.text == "Sure, I can help with that. What would you like?"
        assert mock_provider.complete.call_count == 1

    async def test_no_detection_when_tool_was_called(self):
        """If the agent actually called a tool (iterations > 0), no detection."""
        tool_resp = ProviderResponse(
            content=[
                ContentBlock(type="text", text=""),
                ContentBlock(type="tool_use", name="remember", id="t1",
                             input={"query": "meetings"}),
            ],
            stop_reason="tool_use",
            input_tokens=10,
            output_tokens=20,
        )
        final_resp = _text_response("I've created a memory entry for that.")

        service, mock_provider, events, mcp, audit = _make_service()
        mock_provider.complete.side_effect = [tool_resp, final_resp]
        service._retrieval = MagicMock()
        service._retrieval.remember = AsyncMock(return_value="Stored.")
        request = _make_request(
            tools=[{"name": "remember", "input_schema": {"type": "object", "properties": {}}}],
        )
        result = await service.reason(request)

        # "I've created" is in text but tool was actually called — no detection
        assert "I've created" in result.text
        # Only 2 calls: tool_use + final response (no detection overhead)
        assert mock_provider.complete.call_count == 2


# ---------------------------------------------------------------------------
# Tool Result Budgeting
# ---------------------------------------------------------------------------


from kernos.kernel.reasoning import TOOL_RESULT_CHAR_BUDGET


async def test_mcp_result_under_budget_injected_raw():
    """MCP results under the budget are injected as-is."""
    service, mock_provider, events, mcp, audit = _make_service(read_tool_names=["web_search"])
    short_result = "x" * (TOOL_RESULT_CHAR_BUDGET - 1)
    mcp.call_tool.return_value = short_result

    mock_provider.complete.side_effect = [
        _tool_response("web_search", "tu_ws1", {"q": "test"}),
        _text_response("Done."),
    ]

    await service.reason(
        _make_request(
            tools=[{"name": "web_search", "description": "Search", "input_schema": {}}],
            active_space_id="space-1",
        )
    )

    # The continuation message should contain the raw result
    continuation_msg = mock_provider.complete.call_args_list[1]
    user_msg = continuation_msg.kwargs.get("messages", continuation_msg.args[2] if len(continuation_msg.args) > 2 else None)
    # Find the tool_result in messages
    tool_result_content = None
    for msg in user_msg:
        if msg.get("role") == "user":
            for item in msg.get("content", []):
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_result_content = item["content"]
    assert tool_result_content == short_result


async def test_mcp_result_over_budget_persisted_and_preview_injected(tmp_path):
    """MCP results over the budget are persisted and a preview is injected."""
    from kernos.kernel.files import FileService

    service, mock_provider, events, mcp, audit = _make_service(read_tool_names=["markdown"])
    big_result = "Line of content here.\n" * 500  # Well over 4000 chars
    mcp.call_tool.return_value = big_result

    files = FileService(str(tmp_path))
    service.set_files(files)

    mock_provider.complete.side_effect = [
        _tool_response("markdown", "tu_md1", {"url": "https://example.com"}),
        _text_response("Here's a summary."),
    ]

    await service.reason(
        _make_request(
            tools=[{"name": "markdown", "description": "Get markdown", "input_schema": {}}],
            active_space_id="space-1",
        )
    )

    # Continuation message should contain preview, not raw result
    continuation_call = mock_provider.complete.call_args_list[1]
    messages = continuation_call.kwargs.get("messages", continuation_call.args[2] if len(continuation_call.args) > 2 else None)
    tool_result_content = None
    for msg in messages:
        if msg.get("role") == "user":
            for item in msg.get("content", []):
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_result_content = item["content"]

    assert tool_result_content is not None
    assert "[Tool result from markdown" in tool_result_content
    assert "persisted]" in tool_result_content
    assert "read_file" in tool_result_content
    assert len(tool_result_content) <= TOOL_RESULT_CHAR_BUDGET + 200  # preview + wrapper

    # Verify file was persisted to disk
    space_files = tmp_path / "sms_+15555550100" / "spaces" / "space-1" / "files"
    persisted = list(space_files.glob("tr_markdown_*.txt"))
    assert len(persisted) == 1
    assert persisted[0].read_text() == big_result


async def test_kernel_tool_results_not_budgeted():
    """Kernel tool results are never budgeted, even if large."""
    service, mock_provider, events, mcp, audit = _make_service()
    service._retrieval = MagicMock()
    big_knowledge = "Fact " * 2000  # Over budget
    service._retrieval.search = AsyncMock(return_value=big_knowledge)

    mock_provider.complete.side_effect = [
        _tool_response("remember", "tu_r1", {"query": "test"}),
        _text_response("Noted."),
    ]

    await service.reason(
        _make_request(
            tools=[{"name": "remember", "input_schema": {"type": "object", "properties": {}}}],
            active_space_id="space-1",
        )
    )

    # Kernel tool result should be injected raw (no budgeting)
    continuation_call = mock_provider.complete.call_args_list[1]
    messages = continuation_call.kwargs.get("messages", continuation_call.args[2] if len(continuation_call.args) > 2 else None)
    tool_result_content = None
    for msg in messages:
        if msg.get("role") == "user":
            for item in msg.get("content", []):
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_result_content = item["content"]
    assert tool_result_content == big_knowledge


async def test_mcp_error_result_not_budgeted():
    """Error results are never budgeted, even if over threshold."""
    service, mock_provider, events, mcp, audit = _make_service(read_tool_names=["browser"])
    error_result = "Tool error: " + "x" * 5000
    mcp.call_tool.return_value = error_result

    mock_provider.complete.side_effect = [
        _tool_response("browser", "tu_b1", {}),
        _text_response("Error occurred."),
    ]

    await service.reason(
        _make_request(
            tools=[{"name": "browser", "description": "Browse", "input_schema": {}}],
            active_space_id="space-1",
        )
    )

    continuation_call = mock_provider.complete.call_args_list[1]
    messages = continuation_call.kwargs.get("messages", continuation_call.args[2] if len(continuation_call.args) > 2 else None)
    tool_result_content = None
    for msg in messages:
        if msg.get("role") == "user":
            for item in msg.get("content", []):
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_result_content = item["content"]
    assert tool_result_content == error_result


async def test_budgeting_logs_result_budgeted(tmp_path, caplog):
    """RESULT_BUDGETED log line emitted when a result is budgeted."""
    import logging
    from kernos.kernel.files import FileService

    service, mock_provider, events, mcp, audit = _make_service(read_tool_names=["markdown"])
    mcp.call_tool.return_value = "x" * 10000

    files = FileService(str(tmp_path))
    service.set_files(files)

    mock_provider.complete.side_effect = [
        _tool_response("markdown", "tu_md2", {"url": "https://example.com"}),
        _text_response("Done."),
    ]

    with caplog.at_level(logging.INFO):
        await service.reason(
            _make_request(
                tools=[{"name": "markdown", "description": "Markdown", "input_schema": {}}],
                active_space_id="space-1",
            )
        )

    assert any("RESULT_BUDGETED" in r.message for r in caplog.records)


async def test_budgeting_preview_collapses_excessive_newlines(tmp_path):
    """Preview collapses runs of 3+ newlines to double newlines."""
    from kernos.kernel.files import FileService

    service, mock_provider, events, mcp, audit = _make_service(read_tool_names=["scraper"])
    # Content with excessive blank lines
    big_result = ("content\n\n\n\n\ncontent\n" * 300)
    mcp.call_tool.return_value = big_result

    files = FileService(str(tmp_path))
    service.set_files(files)

    mock_provider.complete.side_effect = [
        _tool_response("scraper", "tu_sc1", {}),
        _text_response("Done."),
    ]

    await service.reason(
        _make_request(
            tools=[{"name": "scraper", "description": "Scrape", "input_schema": {}}],
            active_space_id="space-1",
        )
    )

    continuation_call = mock_provider.complete.call_args_list[1]
    messages = continuation_call.kwargs.get("messages", continuation_call.args[2] if len(continuation_call.args) > 2 else None)
    tool_result_content = None
    for msg in messages:
        if msg.get("role") == "user":
            for item in msg.get("content", []):
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_result_content = item["content"]

    # No runs of 3+ newlines in the preview body
    assert "\n\n\n" not in tool_result_content


async def test_budgeting_graceful_when_file_service_unavailable():
    """If file service is None, oversized results are still injected raw."""
    service, mock_provider, events, mcp, audit = _make_service(read_tool_names=["fetcher"])
    big_result = "z" * 10000
    mcp.call_tool.return_value = big_result
    # _files is None by default

    mock_provider.complete.side_effect = [
        _tool_response("fetcher", "tu_f1", {}),
        _text_response("Done."),
    ]

    await service.reason(
        _make_request(
            tools=[{"name": "fetcher", "description": "Fetch", "input_schema": {}}],
            active_space_id="space-1",
        )
    )

    continuation_call = mock_provider.complete.call_args_list[1]
    messages = continuation_call.kwargs.get("messages", continuation_call.args[2] if len(continuation_call.args) > 2 else None)
    tool_result_content = None
    for msg in messages:
        if msg.get("role") == "user":
            for item in msg.get("content", []):
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_result_content = item["content"]
    # Falls back to raw injection
    assert tool_result_content == big_result


# ---------------------------------------------------------------------------
# Concurrent tool execution (Fix 1)
# ---------------------------------------------------------------------------


def _multi_tool_response(
    tool_specs: list[tuple[str, str, dict]],
) -> ProviderResponse:
    """Create a response with multiple tool_use blocks.

    tool_specs: list of (name, id, input) tuples.
    """
    content = [
        ContentBlock(type="tool_use", name=name, id=tid, input=inp)
        for name, tid, inp in tool_specs
    ]
    return ProviderResponse(
        content=content,
        stop_reason="tool_use",
        input_tokens=15,
        output_tokens=10,
    )


async def test_two_read_tools_execute_concurrently():
    """AC1: Multiple read-only tool_use blocks execute via asyncio.gather."""
    service, mock_provider, events, mcp, audit = _make_service(
        read_tool_names=["tool_a", "tool_b"],
    )

    call_order = []
    async def _track_call(name, args, **kwargs):
        call_order.append(name)
        return f"result_{name}"

    mcp.call_tool = _track_call

    mock_provider.complete.side_effect = [
        _multi_tool_response([
            ("tool_a", "tu_a", {}),
            ("tool_b", "tu_b", {}),
        ]),
        _text_response("Done."),
    ]

    result = await service.reason(
        _make_request(tools=[
            {"name": "tool_a", "description": "A", "input_schema": {}},
            {"name": "tool_b", "description": "B", "input_schema": {}},
        ])
    )

    assert result.text == "Done."
    # Both tools were called
    assert "tool_a" in call_order
    assert "tool_b" in call_order


async def test_tool_results_order_matches_block_order():
    """AC2: Returned tool_result order matches original tool_use block order."""
    service, mock_provider, events, mcp, audit = _make_service(
        read_tool_names=["first_tool", "second_tool"],
    )

    async def _return_name(name, args, **kwargs):
        return f"result_{name}"

    mcp.call_tool = _return_name

    mock_provider.complete.side_effect = [
        _multi_tool_response([
            ("first_tool", "tu_1", {}),
            ("second_tool", "tu_2", {}),
        ]),
        _text_response("Done."),
    ]

    await service.reason(
        _make_request(tools=[
            {"name": "first_tool", "description": "First", "input_schema": {}},
            {"name": "second_tool", "description": "Second", "input_schema": {}},
        ])
    )

    # Check continuation message has results in original order
    continuation_call = mock_provider.complete.call_args_list[1]
    messages = continuation_call.kwargs.get("messages", continuation_call.args[2] if len(continuation_call.args) > 2 else None)
    user_msg = [m for m in messages if m.get("role") == "user"][-1]
    tool_results = user_msg["content"]
    assert tool_results[0]["tool_use_id"] == "tu_1"
    assert tool_results[1]["tool_use_id"] == "tu_2"
    assert "result_first_tool" in tool_results[0]["content"]
    assert "result_second_tool" in tool_results[1]["content"]


def test_write_tool_not_concurrent_safe():
    """AC3: Write tools are classified as NOT concurrent-safe."""
    from kernos.capability.registry import CapabilityInfo, CapabilityRegistry, CapabilityStatus

    service, _, _, _, _ = _make_service()

    cap = CapabilityInfo(
        name="test-cap",
        display_name="Test",
        description="Test",
        category="test",
        status=CapabilityStatus.CONNECTED,
        tools=["read_tool", "write_tool"],
        server_name="test",
        tool_effects={"read_tool": "read", "write_tool": "soft_write"},
    )
    registry = CapabilityRegistry(mcp=None)
    registry.register(cap)
    service.set_registry(registry)

    assert service._is_concurrent_safe("read_tool") is True
    assert service._is_concurrent_safe("write_tool") is False


def test_unknown_effect_tool_not_concurrent_safe():
    """AC4: Unknown-effect tool stays sequential (not concurrent-safe)."""
    service, _, _, _, _ = _make_service()
    # No registry — tool effect will be "unknown"
    assert service._is_concurrent_safe("mystery_tool") is False
    # Kernel reads are concurrent-safe
    assert service._is_concurrent_safe("remember") is True
    assert service._is_concurrent_safe("read_doc") is True
    # Kernel writes are not
    assert service._is_concurrent_safe("write_file") is False
    assert service._is_concurrent_safe("delete_file") is False


async def test_budgeting_still_applies_per_tool_in_concurrent(tmp_path):
    """AC12: Result budgeting still applies per-tool, post-execution."""
    from kernos.kernel.files import FileService

    service, mock_provider, events, mcp, audit = _make_service(
        read_tool_names=["big_tool"],
    )
    file_service = FileService(str(tmp_path))
    service.set_files(file_service)

    big_result = "x" * 10000
    mcp.call_tool = AsyncMock(return_value=big_result)

    mock_provider.complete.side_effect = [
        _tool_response("big_tool", "tu_big", {}),
        _text_response("Done."),
    ]

    await service.reason(
        _make_request(
            tools=[{"name": "big_tool", "description": "Big", "input_schema": {}}],
            active_space_id="space-1",
        )
    )

    continuation_call = mock_provider.complete.call_args_list[1]
    messages = continuation_call.kwargs.get("messages", continuation_call.args[2] if len(continuation_call.args) > 2 else None)
    tool_result_content = None
    for msg in messages:
        if msg.get("role") == "user":
            for item in msg.get("content", []):
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_result_content = item["content"]
    # Should be budgeted (not the raw 10000 chars)
    assert tool_result_content is not None
    assert len(tool_result_content) < 5000
    assert "persisted" in tool_result_content
