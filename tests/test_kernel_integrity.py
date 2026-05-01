"""Kernel integrity tests: restart survival, event completeness, cost tracking,
shadow archive, and behavioral contract defaults.

These verify the Phase 1B completion criteria that aren't covered by isolation tests.
"""
import dataclasses
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from kernos.capability.client import MCPClientManager
from kernos.capability.known import KNOWN_CAPABILITIES
from kernos.capability.registry import CapabilityRegistry
from kernos.kernel.engine import TaskEngine
from kernos.kernel.events import JsonEventStream, emit_event, estimate_cost
from kernos.kernel.reasoning import (
    ContentBlock,
    Provider,
    ProviderResponse,
    ReasoningService,
)
from kernos.kernel.soul import Soul
from kernos.kernel.state import (
    ConversationSummary,
    KnowledgeEntry,
    InstanceProfile,
    default_contract_rules,
)
from kernos.kernel.state_json import JsonStateStore
from kernos.messages.handler import MessageHandler
from kernos.messages.models import AuthLevel, NormalizedMessage
from kernos.persistence import AuditStore, ConversationStore, InstanceStore
from kernos.persistence.json_file import JsonConversationStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_message(content: str = "Hello") -> NormalizedMessage:
    return NormalizedMessage(
        content=content,
        sender="123456789",
        sender_auth_level=AuthLevel.owner_verified,
        platform="discord",
        platform_capabilities=["text"],
        conversation_id="123456789",
        timestamp=datetime.now(timezone.utc),
        instance_id="discord:123456789",
    )


def _mock_response(text: str, input_tokens: int = 100, output_tokens: int = 50) -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="text", text=text)],
        stop_reason="end_turn",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _make_real_handler(tmp_path):
    """Handler with real JsonStateStore and JsonEventStream (writes to disk)."""
    mcp = MagicMock(spec=MCPClientManager)
    mcp.get_tools.return_value = []

    conversations = AsyncMock(spec=ConversationStore)
    conversations.get_recent.return_value = []
    conversations.append.return_value = None

    tenants = AsyncMock(spec=InstanceStore)
    tenants.get_or_create.return_value = {
        "instance_id": "discord:123456789",
        "status": "active",
        "created_at": _now(),
        "capabilities": {},
    }

    audit = AsyncMock(spec=AuditStore)
    audit.log.return_value = None

    events = JsonEventStream(tmp_path)
    state = JsonStateStore(tmp_path)

    registry = CapabilityRegistry(mcp=mcp)
    for cap in KNOWN_CAPABILITIES:
        registry.register(dataclasses.replace(cap))

    mock_provider = AsyncMock(spec=Provider)
    mock_provider.main_model = "claude-sonnet-4-6"
    reasoning = ReasoningService(mock_provider, events, mcp, audit)
    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(
        mcp, conversations, tenants, audit, events, state, reasoning, registry, engine
    )
    handler.preference_parsing_enabled = False
    return handler, mock_provider, events, state


# ============================================================================
# 3.1 Restart Survival
# ============================================================================


async def test_restart_soul_survives(tmp_path):
    store = JsonStateStore(tmp_path)
    await store.save_soul(Soul(instance_id="t1", user_name="Alice", hatched=True, interaction_count=5))
    store2 = JsonStateStore(tmp_path)
    loaded = await store2.get_soul("t1")
    assert loaded is not None
    assert loaded.user_name == "Alice"
    assert loaded.hatched is True
    assert loaded.interaction_count == 5


async def test_restart_profile_survives(tmp_path):
    store = JsonStateStore(tmp_path)
    await store.save_instance_profile("t1", InstanceProfile(instance_id="t1", status="active", created_at=_now()))
    store2 = JsonStateStore(tmp_path)
    loaded = await store2.get_instance_profile("t1")
    assert loaded is not None
    assert loaded.status == "active"


async def test_restart_contracts_survive(tmp_path):
    store = JsonStateStore(tmp_path)
    for rule in default_contract_rules("t1", _now()):
        await store.add_contract_rule(rule)
    store2 = JsonStateStore(tmp_path)
    assert len(await store2.get_contract_rules("t1")) == 9


async def test_restart_events_queryable(tmp_path):
    stream = JsonEventStream(tmp_path)
    await emit_event(stream, "message.received", "t1", "test", payload={"content": "Hello"})
    await emit_event(stream, "message.sent", "t1", "test", payload={"content": "Hi"})
    stream2 = JsonEventStream(tmp_path)
    events = await stream2.query("t1")
    assert len(events) == 2
    assert events[0].type == "message.received"
    assert events[1].type == "message.sent"


async def test_restart_conversation_summary_survives(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    await store.save_conversation_summary(ConversationSummary(
        instance_id="t1", conversation_id="conv_1", platform="discord",
        message_count=5, first_message_at=now, last_message_at=now,
    ))
    store2 = JsonStateStore(tmp_path)
    loaded = await store2.get_conversation_summary("t1", "conv_1")
    assert loaded is not None
    assert loaded.message_count == 5


async def test_restart_knowledge_survives(tmp_path):
    store = JsonStateStore(tmp_path)
    now = _now()
    await store.add_knowledge(KnowledgeEntry(
        id="know_test", instance_id="t1", category="entity", subject="Alice",
        content="Alice is a developer", confidence="stated",
        source_event_id="evt_1", source_description="test",
        created_at=now, last_referenced=now, tags=["person"],
    ))
    store2 = JsonStateStore(tmp_path)
    results = await store2.query_knowledge("t1")
    assert len(results) == 1
    assert results[0].subject == "Alice"


# ============================================================================
# 3.2 Event Completeness
# ============================================================================


async def test_full_message_flow_emits_required_events(tmp_path):
    handler, mock_provider, events, state = _make_real_handler(tmp_path)
    mock_provider.complete.return_value = _mock_response("Hello! Nice to meet you.")

    await handler.process(_make_message("Hi there"))

    instance_id = "discord:123456789"
    all_events = await events.query(instance_id, limit=100)
    event_types = [e.type for e in all_events]

    assert "message.received" in event_types
    assert "reasoning.request" in event_types
    assert "reasoning.response" in event_types
    assert "task.completed" in event_types
    assert "message.sent" in event_types


async def test_full_message_flow_event_order(tmp_path):
    handler, mock_provider, events, state = _make_real_handler(tmp_path)
    mock_provider.complete.return_value = _mock_response("Hello!")

    await handler.process(_make_message("Hi"))

    instance_id = "discord:123456789"
    all_events = await events.query(instance_id, limit=100)
    event_types = [e.type for e in all_events]

    idx_received = event_types.index("message.received")
    idx_rr = event_types.index("reasoning.request")
    idx_resp = event_types.index("reasoning.response")
    idx_completed = event_types.index("task.completed")
    idx_sent = event_types.index("message.sent")

    assert idx_received < idx_rr
    assert idx_rr < idx_resp
    assert idx_resp < idx_completed
    assert idx_completed < idx_sent


async def test_all_events_have_required_fields(tmp_path):
    handler, mock_provider, events, state = _make_real_handler(tmp_path)
    mock_provider.complete.return_value = _mock_response("Hello!")

    await handler.process(_make_message("Hi"))

    instance_id = "discord:123456789"
    for event in await events.query(instance_id, limit=100):
        assert event.id
        assert event.type
        assert event.instance_id == instance_id
        assert event.timestamp
        assert event.source
        assert isinstance(event.payload, dict)


async def test_reasoning_events_have_model_tokens_cost_duration(tmp_path):
    handler, mock_provider, events, state = _make_real_handler(tmp_path)
    mock_provider.complete.return_value = _mock_response("Hello!", input_tokens=500, output_tokens=100)

    await handler.process(_make_message("Hi"))

    instance_id = "discord:123456789"
    reasoning_responses = await events.query(
        instance_id, event_types=["reasoning.response"], limit=10
    )
    assert len(reasoning_responses) >= 1
    payload = reasoning_responses[0].payload
    assert "model" in payload
    assert "input_tokens" in payload
    assert "output_tokens" in payload
    assert "estimated_cost_usd" in payload
    assert "duration_ms" in payload


# ============================================================================
# 3.3 Cost Tracking
# ============================================================================


async def test_cost_tracking_task_completed_has_accurate_fields(tmp_path):
    handler, mock_provider, events, state = _make_real_handler(tmp_path)
    mock_provider.complete.return_value = _mock_response("Response", input_tokens=1000, output_tokens=500)

    await handler.process(_make_message("Hi"))

    instance_id = "discord:123456789"
    completed = await events.query(instance_id, event_types=["task.completed"], limit=5)
    assert len(completed) >= 1
    payload = completed[0].payload
    assert payload["input_tokens"] == 1000
    assert payload["output_tokens"] == 500
    expected_cost = estimate_cost("claude-sonnet-4-6", 1000, 500)
    assert abs(payload["estimated_cost_usd"] - expected_cost) < 1e-9
    assert isinstance(payload["duration_ms"], int)
    assert payload["duration_ms"] >= 0


async def test_cost_tracking_unknown_model_returns_zero():
    assert estimate_cost("gpt-9999-turbo", 1_000_000, 1_000_000) == 0.0


async def test_cost_tracking_known_model_formula():
    # claude-sonnet-4-6: $3.00/M input, $15.00/M output
    cost = estimate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert abs(cost - 18.00) < 0.001


# ============================================================================
# 3.4 Shadow Archive
# ============================================================================


async def test_shadow_archive_removes_original(tmp_path):
    store = JsonConversationStore(tmp_path)
    await store.append("t1", "conv_1", {"role": "user", "content": "Hello"})
    original = tmp_path / "t1" / "conversations" / "conv_1.json"
    assert original.exists()
    await store.archive("t1", "conv_1")
    assert not original.exists()


async def test_shadow_archive_creates_copy_in_archive_dir(tmp_path):
    store = JsonConversationStore(tmp_path)
    await store.append("t1", "conv_1", {"role": "user", "content": "Hello"})
    await store.archive("t1", "conv_1")
    archive_dir = tmp_path / "t1" / "archive" / "conversations"
    assert archive_dir.exists()
    assert len(list(archive_dir.rglob("*.json"))) == 1


async def test_shadow_archive_copy_has_full_metadata(tmp_path):
    store = JsonConversationStore(tmp_path)
    await store.append("t1", "conv_1", {"role": "user", "content": "Hello"})
    await store.archive("t1", "conv_1")
    archive_dir = tmp_path / "t1" / "archive" / "conversations"
    archived_file = list(archive_dir.rglob("*.json"))[0]
    with open(archived_file) as f:
        data = json.load(f)
    assert "archived_at" in data
    assert data["instance_id"] == "t1"
    assert data["conversation_id"] == "conv_1"
    assert "entries" in data
    assert len(data["entries"]) == 1


async def test_shadow_archive_noop_for_missing_conversation(tmp_path):
    store = JsonConversationStore(tmp_path)
    await store.archive("t1", "nonexistent")  # Must not raise


# ============================================================================
# 3.5 Behavioral Contract Defaults
# ============================================================================


def test_default_contract_rules_count():
    assert len(default_contract_rules("t1", _now())) == 9


def test_default_contract_rules_all_active():
    assert all(r.active for r in default_contract_rules("t1", _now()))


def test_default_contract_rules_source_is_default():
    assert all(r.source == "default" for r in default_contract_rules("t1", _now()))


def test_default_contract_rules_correct_tenant():
    assert all(r.instance_id == "t1" for r in default_contract_rules("t1", _now()))


def test_default_contract_rules_type_breakdown():
    rules = default_contract_rules("t1", _now())
    assert len([r for r in rules if r.rule_type == "must_not"]) == 3
    assert len([r for r in rules if r.rule_type == "must"]) == 2
    assert len([r for r in rules if r.rule_type == "preference"]) == 2
    assert len([r for r in rules if r.rule_type == "escalation"]) == 1


async def test_newinstance_provisioned_with_seven_default_rules(tmp_path):
    """Handler provisioning a new tenant creates 7 default contract rules."""
    handler, mock_provider, events, state = _make_real_handler(tmp_path)
    mock_provider.complete.return_value = _mock_response("Hello!")

    await handler.process(_make_message())

    instance_id = "discord:123456789"
    rules = await state.get_contract_rules(instance_id)
    assert len(rules) == 9
    assert all(r.instance_id == instance_id for r in rules)
    assert all(r.active for r in rules)
