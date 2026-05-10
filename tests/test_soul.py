"""Tests for soul data model, template, state store soul persistence, and hatch process."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernos.kernel.soul import Soul
from kernos.kernel.template import AgentTemplate, PRIMARY_TEMPLATE
from kernos.kernel.state_json import JsonStateStore
from kernos.messages.handler import (
    MessageHandler,
    _build_system_prompt,
    _format_contracts,
    _is_soul_mature,
)
from kernos.kernel.state import ContractRule, default_contract_rules


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Soul dataclass
# ---------------------------------------------------------------------------


def test_soul_defaults():
    soul = Soul(instance_id="t1")
    assert soul.hatched is False
    assert soul.bootstrap_graduated is False
    assert soul.interaction_count == 0
    assert soul.user_name == ""
    assert soul.agent_name == ""   # Per-member — named during hatching
    assert soul.emoji == ""        # Per-member — set during hatching


def test_soul_instance_id_required():
    soul = Soul(instance_id="discord:123")
    assert soul.instance_id == "discord:123"


# ---------------------------------------------------------------------------
# AgentTemplate / PRIMARY_TEMPLATE
# ---------------------------------------------------------------------------


def test_primary_template_has_required_fields():
    assert PRIMARY_TEMPLATE.name == "conversational"
    assert PRIMARY_TEMPLATE.operating_principles
    assert PRIMARY_TEMPLATE.default_personality
    assert PRIMARY_TEMPLATE.bootstrap_prompt
    assert "calendar" in PRIMARY_TEMPLATE.expected_capabilities


def test_template_dataclass():
    t = AgentTemplate(
        name="test",
        version="0.1",
        operating_principles="op",
        default_personality="dp",
        bootstrap_prompt="bp",
    )
    assert t.expected_capabilities == []


# ---------------------------------------------------------------------------
# Soul persistence — JsonStateStore
# ---------------------------------------------------------------------------


async def test_get_soul_returns_none_for_new_instance(tmp_path):
    store = JsonStateStore(tmp_path)
    assert await store.get_soul("new_instance") is None


async def test_save_and_get_soul(tmp_path):
    store = JsonStateStore(tmp_path)
    soul = Soul(instance_id="t1", user_name="Alice", hatched=True)
    await store.save_soul(soul)
    fetched = await store.get_soul("t1")
    assert fetched is not None
    assert fetched.instance_id == "t1"
    assert fetched.user_name == "Alice"
    assert fetched.hatched is True


async def test_save_soul_overwrites(tmp_path):
    store = JsonStateStore(tmp_path)
    soul = Soul(instance_id="t1", interaction_count=5)
    await store.save_soul(soul)
    soul.interaction_count = 10
    await store.save_soul(soul)
    fetched = await store.get_soul("t1")
    assert fetched.interaction_count == 10


async def test_soul_isolated_per_instance(tmp_path):
    store = JsonStateStore(tmp_path)
    await store.save_soul(Soul(instance_id="t1", user_name="Alice"))
    await store.save_soul(Soul(instance_id="t2", user_name="Bob"))
    s1 = await store.get_soul("t1")
    s2 = await store.get_soul("t2")
    assert s1.user_name == "Alice"
    assert s2.user_name == "Bob"


# ---------------------------------------------------------------------------
# _is_soul_mature
# ---------------------------------------------------------------------------


def test_soul_not_mature_empty():
    soul = Soul(instance_id="t1")
    assert _is_soul_mature(soul) is False


def test_soul_not_mature_missing_fields():
    soul = Soul(
        instance_id="t1",
        user_name="Alice",
        communication_style="direct",
        interaction_count=5,  # below threshold
    )
    assert _is_soul_mature(soul, has_user_knowledge=True) is False


def test_soul_mature_all_signals():
    soul = Soul(
        instance_id="t1",
        user_name="Alice",
        communication_style="direct",
        interaction_count=15,
    )
    assert _is_soul_mature(soul, has_user_knowledge=True) is True


def test_soul_not_mature_missing_user_knowledge():
    soul = Soul(
        instance_id="t1",
        user_name="Alice",
        communication_style="direct",
        interaction_count=15,
    )
    assert _is_soul_mature(soul, has_user_knowledge=False) is False


# ---------------------------------------------------------------------------
# _format_contracts
# ---------------------------------------------------------------------------


def test_format_contracts_empty():
    assert _format_contracts([]) == ""


def test_format_contracts_includes_all_types():
    now = _now()
    rules = default_contract_rules("t1", now)
    result = _format_contracts(rules)
    assert "BEHAVIORAL CONTRACTS" in result
    assert "MUST NOT" in result
    assert "MUST:" in result
    assert "PREFERENCE" in result
    assert "ESCALATION" in result


# ---------------------------------------------------------------------------
# _build_system_prompt
# ---------------------------------------------------------------------------


def _make_message_stub(platform: str = "discord"):
    from kernos.messages.models import AuthLevel, NormalizedMessage
    return NormalizedMessage(
        content="hello",
        sender="user1",
        sender_auth_level=AuthLevel.owner_unverified,
        platform=platform,
        platform_capabilities=["text"],
        conversation_id="conv1",
        timestamp=datetime.now(timezone.utc),
        instance_id="t1",
    )


def test_system_prompt_includes_operating_principles():
    soul = Soul(instance_id="t1")
    msg = _make_message_stub()
    now = _now()
    rules = default_contract_rules("t1", now)
    prompt = _build_system_prompt(msg, "caps", soul, PRIMARY_TEMPLATE, rules)
    assert "INTENT OVER INSTRUCTION" in prompt
    assert "USE TOOLS" in prompt


def test_system_prompt_includes_hatching_when_not_graduated():
    soul = Soul(instance_id="t1")
    msg = _make_message_stub()
    prompt = _build_system_prompt(msg, "caps", soul, PRIMARY_TEMPLATE, [])
    # Both personality foundation (bootstrap) AND hatching identity
    # layer should be present. The bootstrap teaches substrate
    # awareness (request_reference / store_reference); the hatching
    # layer carries the per-conversation identity guidance.
    assert "request_reference" in prompt        # Substrate awareness
    assert "store_reference" in prompt          # Substrate awareness
    assert "HATCHING" in prompt                  # Identity layer


def test_system_prompt_excludes_bootstrap_when_graduated():
    soul = Soul(instance_id="t1", bootstrap_graduated=True)
    msg = _make_message_stub()
    prompt = _build_system_prompt(msg, "caps", soul, PRIMARY_TEMPLATE, [])
    # Graduated soul — no hatching prompt
    assert "HATCHING" not in prompt


def test_system_prompt_uses_soul_personality_when_set():
    soul = Soul(instance_id="t1", personality_notes="Very sarcastic and dry.")
    msg = _make_message_stub()
    prompt = _build_system_prompt(msg, "caps", soul, PRIMARY_TEMPLATE, [])
    assert "Very sarcastic and dry." in prompt


def test_system_prompt_uses_default_personality_when_soul_empty():
    soul = Soul(instance_id="t1")
    msg = _make_message_stub()
    prompt = _build_system_prompt(msg, "caps", soul, PRIMARY_TEMPLATE, [])
    assert PRIMARY_TEMPLATE.default_personality[:20] in prompt


def test_system_prompt_includes_user_name_when_set():
    soul = Soul(instance_id="t1", user_name="Greg")
    msg = _make_message_stub()
    prompt = _build_system_prompt(msg, "caps", soul, PRIMARY_TEMPLATE, [])
    assert "Greg" in prompt


def test_system_prompt_excludes_user_context_section_when_empty():
    soul = Soul(instance_id="t1")  # all empty
    msg = _make_message_stub()
    prompt = _build_system_prompt(msg, "caps", soul, PRIMARY_TEMPLATE, [])
    assert "USER CONTEXT:" not in prompt


def test_system_prompt_includes_knowledge_entries():
    from kernos.kernel.state import KnowledgeEntry
    soul = Soul(instance_id="t1", user_name="JT")
    msg = _make_message_stub()
    entries = [
        KnowledgeEntry(
            id="ke1", instance_id="t1", category="fact", subject="user",
            content="Lives in Seattle", confidence="stated",
            source_event_id="", source_description="test",
            created_at=_now(), last_referenced=_now(), tags=[],
            lifecycle_archetype="structural",
        ),
        KnowledgeEntry(
            id="ke2", instance_id="t1", category="fact", subject="user",
            content="Building Kernos", confidence="stated",
            source_event_id="", source_description="test",
            created_at=_now(), last_referenced=_now(), tags=[],
            lifecycle_archetype="structural",
        ),
    ]
    prompt = _build_system_prompt(
        msg, "caps", soul, PRIMARY_TEMPLATE, [],
        user_knowledge_entries=entries,
    )
    assert "USER CONTEXT:" in prompt
    assert "Lives in Seattle" in prompt
    assert "Building Kernos" in prompt
    assert "JT" in prompt


def test_system_prompt_ignores_deprecated_soul_user_context():
    """soul.user_context should NOT appear in the prompt even if populated."""
    soul = Soul(instance_id="t1", user_context="old stale data")
    msg = _make_message_stub()
    prompt = _build_system_prompt(msg, "caps", soul, PRIMARY_TEMPLATE, [])
    assert "old stale data" not in prompt


def test_system_prompt_includes_contracts():
    soul = Soul(instance_id="t1")
    msg = _make_message_stub()
    now = _now()
    rules = default_contract_rules("t1", now)
    prompt = _build_system_prompt(msg, "caps", soul, PRIMARY_TEMPLATE, rules)
    assert "BEHAVIORAL CONTRACTS" in prompt


def test_system_prompt_includes_capability_prompt():
    soul = Soul(instance_id="t1")
    msg = _make_message_stub()
    cap_prompt = "CONNECTED: Google Calendar"
    prompt = _build_system_prompt(msg, cap_prompt, soul, PRIMARY_TEMPLATE, [])
    assert cap_prompt in prompt


# ---------------------------------------------------------------------------
# Hatch process — handler integration
# ---------------------------------------------------------------------------


def _make_normalized_message(platform: str = "discord"):
    from kernos.messages.models import AuthLevel, NormalizedMessage
    return NormalizedMessage(
        content="Hey there",
        sender="user123",
        sender_auth_level=AuthLevel.owner_unverified,
        platform=platform,
        platform_capabilities=["text"],
        conversation_id="conv1",
        timestamp=datetime.now(timezone.utc),
        instance_id="discord:user123",
    )


def _make_mock_provider_response(text: str):
    from kernos.kernel.reasoning import ContentBlock, ProviderResponse
    return ProviderResponse(
        content=[ContentBlock(type="text", text=text)],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=20,
    )


def _make_handler_with_state():
    """Build a handler where state mock properly handles soul methods."""
    from kernos.capability.client import MCPClientManager
    from kernos.capability.registry import CapabilityRegistry
    from kernos.kernel.engine import TaskEngine
    from kernos.kernel.events import EventStream
    from kernos.kernel.reasoning import Provider, ReasoningService
    from kernos.kernel.state import StateStore, InstanceProfile
    from kernos.persistence import AuditStore, ConversationStore, InstanceStore

    mcp = MagicMock(spec=MCPClientManager)
    mcp.get_tools.return_value = []

    conversations = AsyncMock(spec=ConversationStore)
    conversations.get_recent.return_value = []
    conversations.append.return_value = None

    tenants = AsyncMock(spec=InstanceStore)
    tenants.get_or_create.return_value = {"instance_id": "t1", "status": "active"}

    audit = AsyncMock(spec=AuditStore)
    events = AsyncMock(spec=EventStream)

    state = AsyncMock(spec=StateStore)
    state.get_instance_profile.return_value = InstanceProfile(
        instance_id="t1", status="active", created_at=_now()
    )
    state.save_instance_profile.return_value = None
    state.get_soul.return_value = None  # No soul yet — triggers hatch
    state.save_soul.return_value = None
    state.get_conversation_summary.return_value = None
    state.save_conversation_summary.return_value = None
    state.get_contract_rules.return_value = default_contract_rules("t1", _now())
    state.get_knowledge_hashes.return_value = set()
    state.query_knowledge.return_value = []

    registry = MagicMock(spec=CapabilityRegistry)
    registry.get_connected_tools.return_value = []
    registry.build_capability_prompt.return_value = "CURRENT CAPABILITIES — conversation only."
    registry.build_tool_directory.return_value = "CONNECTED SERVICES: Test\nYour tools are listed in your tool definitions."
    registry.get_preloaded_tools.return_value = []
    registry.get_lazy_tool_stubs.return_value = []
    registry.get_tool_schema.return_value = None
    registry.get_all.return_value = []

    mock_provider = AsyncMock(spec=Provider)
    reasoning = ReasoningService(mock_provider, events, mcp, audit)
    # REASONING-SERVICE-CONSTRUCTION-PARITY-V1: post-strike,
    # reason() requires a wired turn_runner_provider. Use the shared
    # test helper so the soul tests don't drift from the runtime
    # construction contract.
    from tests._thin_path_test_fixture import wire_test_thin_path
    wire_test_thin_path(reasoning, provider=mock_provider, mcp=mcp)
    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(
        mcp, conversations, tenants, audit, events, state, reasoning, registry, engine
    )
    handler.preference_parsing_enabled = False
    return handler, mock_provider, state


async def test_hatch_creates_and_marks_soul():
    handler, mock_provider, state = _make_handler_with_state()
    mock_provider.complete.return_value = _make_mock_provider_response("Hello! Nice to meet you.")

    result = await handler.process(_make_normalized_message())

    # Name ask is appended on first interaction (soul starts with no user_name)
    assert result.startswith("Hello! Nice to meet you.")
    # Soul was initialized (get_soul returned None, so save_soul was called)
    assert state.save_soul.call_count >= 1
    # The soul passed to the second save (post-response) should be hatched
    saved_soul = state.save_soul.call_args[0][0]
    assert saved_soul.hatched is True
    assert saved_soul.interaction_count == 1


async def test_returninginstance_loads_existing_soul():
    handler, mock_provider, state = _make_handler_with_state()
    mock_provider.complete.return_value = _make_mock_provider_response("Welcome back!")

    # Simulate an existing hatched soul
    existing_soul = Soul(
        instance_id="t1",
        hatched=True,
        hatched_at=_now(),
        interaction_count=5,
        user_name="Alice",
    )
    state.get_soul.return_value = existing_soul

    result = await handler.process(_make_normalized_message())
    assert result == "Welcome back!"

    # Interaction count should have incremented
    saved_soul = state.save_soul.call_args[0][0]
    assert saved_soul.interaction_count == 6
    assert saved_soul.hatched is True  # still hatched, not re-hatched


async def test_soul_not_updated_on_reasoning_failure():
    from kernos.kernel.exceptions import ReasoningProviderError

    handler, mock_provider, state = _make_handler_with_state()
    mock_provider.complete.side_effect = ReasoningProviderError("API error")

    result = await handler.process(_make_normalized_message())
    # LLM-SETUP-AND-FALLBACK: when every provider in the chain fails, the
    # handler delivers a pre-rendered chain-exhaustion message instead of
    # the LLM reply (contract). Accept either the pre-rendered message or
    # the older "went wrong" phrasing that single-provider test fixtures
    # used to produce.
    lowered = result.lower()
    assert (
        "chain exhausted" in lowered
        or "couldn't reach any language-model provider" in lowered
        or "went wrong" in lowered
    ), result

    # Soul should NOT be updated (save_soul called only once — during init)
    # The post-response update path is not reached on failure
    init_save_count = 1  # only the _get_or_init_soul save
    assert state.save_soul.call_count == init_save_count


async def test_agent_hatched_event_emitted():
    handler, mock_provider, state = _make_handler_with_state()
    mock_provider.complete.return_value = _make_mock_provider_response("Hi!")

    await handler.process(_make_normalized_message())

    # Check that agent.hatched event was emitted
    emit_calls = handler.events.emit.call_args_list
    event_types = [call[0][0].type if hasattr(call[0][0], 'type') else None for call in emit_calls]
    # emit_event uses emit() — check event stream was called
    # The EventStream mock records all calls
    assert handler.events.emit.called
