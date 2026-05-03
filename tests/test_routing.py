"""Tests for SPEC-2B-v2: LLM Context Space Routing.

Covers: LLM router (mocked), get_space_thread, get_cross_domain_messages,
get_recent_full, token budget truncation, topic hint tracking (Gate 1),
Gate 2 space creation, session exit, LRU sunset, handler integration,
query_covenant_rules scoping, system prompt posture injection,
knowledge scoping.
"""
import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernos.kernel.event_types import EventType
from kernos.kernel.events import JsonEventStream
from kernos.kernel.router import LLMRouter, RouterResult
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.soul import Soul
from kernos.kernel.state import CovenantRule, InstanceProfile, default_covenant_rules
from kernos.kernel.state_json import JsonStateStore
from kernos.messages.models import AuthLevel, NormalizedMessage
from kernos.persistence.json_file import JsonConversationStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _daily_space(instance_id: str, space_id: str = "space_daily") -> ContextSpace:
    return ContextSpace(
        id=space_id,
        instance_id=instance_id,
        name="General",
        description="General conversation and daily life",
        space_type="general",
        status="active",
        is_default=True,
        created_at=_now(),
        last_active_at="2026-03-08T01:00:00+00:00",
    )


def _project_space(
    instance_id: str,
    space_id: str = "space_project",
    name: str = "Test Project",
    posture: str = "",
    description: str = "A test project space",
    last_active_at: str = "2026-03-08T02:00:00+00:00",
) -> ContextSpace:
    return ContextSpace(
        id=space_id,
        instance_id=instance_id,
        name=name,
        description=description,
        space_type="project",
        status="active",
        is_default=False,
        posture=posture,
        created_at=_now(),
        last_active_at=last_active_at,
    )


def _msg(content: str, platform: str = "discord", sender: str = "user1"):
    return NormalizedMessage(
        content=content,
        sender=sender,
        sender_auth_level=AuthLevel.owner_verified,
        platform=platform,
        platform_capabilities=["text"],
        conversation_id="conv_test",
        timestamp=datetime.now(timezone.utc),
        instance_id=f"{platform}:{sender}",
    )


# ---------------------------------------------------------------------------
# TestLLMRouterSingleSpace
# ---------------------------------------------------------------------------


class TestLLMRouterSingleSpace:
    """Router calls LLM even with a single space — bypass removed."""

    async def test_single_space_calls_llm(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t1"
        daily = _daily_space(tid)
        await state.save_context_space(daily)

        llm_response = json.dumps({
            "tags": [daily.id],
            "focus": daily.id,
            "continuation": False,
        })
        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(return_value=llm_response)

        router = LLMRouter(state, mock_reasoning)
        result = await router.route(tid, "hello world", [], "")

        assert result.focus == daily.id
        assert daily.id in result.tags
        assert result.continuation is False
        # LLM is called even for single space (bypass removed)
        mock_reasoning.complete_simple.assert_called_once()

    async def test_single_space_llm_failure_falls_back(self, tmp_path):
        """When LLM fails with single space, fallback returns default space."""
        state = JsonStateStore(tmp_path)
        tid = "t1"
        daily = _daily_space(tid)
        await state.save_context_space(daily)

        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(side_effect=RuntimeError("LLM down"))

        router = LLMRouter(state, mock_reasoning)
        result = await router.route(tid, "hello", [], "")

        # Falls back to default space
        assert result.focus == daily.id
        assert result.continuation is True  # fallback sets continuation=True


# ---------------------------------------------------------------------------
# TestLLMRouterNoSpaces
# ---------------------------------------------------------------------------


class TestLLMRouterNoSpaces:
    """Router returns empty strings when no spaces exist."""

    async def test_no_spaces_returns_empty(self, tmp_path):
        state = JsonStateStore(tmp_path)
        mock_reasoning = AsyncMock()

        router = LLMRouter(state, mock_reasoning)
        result = await router.route("t_empty", "hello", [], "")

        assert result.focus == ""
        assert result.tags == []
        assert result.continuation is False


# ---------------------------------------------------------------------------
# TestLLMRouterMultiSpace
# ---------------------------------------------------------------------------


class TestLLMRouterMultiSpace:
    """Router calls LLM and parses result correctly for multi-space tenants."""

    async def test_llm_called_and_result_parsed(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t_multi"
        daily = _daily_space(tid)
        project = _project_space(tid)
        await state.save_context_space(daily)
        await state.save_context_space(project)

        llm_response = json.dumps({
            "tags": [project.id],
            "focus": project.id,
            "continuation": False,
        })

        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(return_value=llm_response)

        router = LLMRouter(state, mock_reasoning)
        result = await router.route(tid, "Let's work on the test project", [], "")

        assert result.focus == project.id
        assert project.id in result.tags
        assert result.continuation is False
        mock_reasoning.complete_simple.assert_called_once()

    async def test_focus_always_in_tags(self, tmp_path):
        """If LLM returns focus not in tags, focus is prepended to tags."""
        state = JsonStateStore(tmp_path)
        tid = "t_multi2"
        daily = _daily_space(tid)
        project = _project_space(tid)
        await state.save_context_space(daily)
        await state.save_context_space(project)

        llm_response = json.dumps({
            "tags": [daily.id],       # LLM forgot to include focus in tags
            "focus": project.id,
            "continuation": False,
        })
        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(return_value=llm_response)

        router = LLMRouter(state, mock_reasoning)
        result = await router.route(tid, "project work", [], "")

        assert project.id in result.tags


# ---------------------------------------------------------------------------
# TestLLMRouterFallback
# ---------------------------------------------------------------------------


class TestLLMRouterFallback:
    """When LLM fails, falls back to current_focus_id."""

    async def test_fallback_to_current_focus_on_exception(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t_fallback"
        daily = _daily_space(tid)
        project = _project_space(tid)
        await state.save_context_space(daily)
        await state.save_context_space(project)

        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(side_effect=Exception("API down"))

        router = LLMRouter(state, mock_reasoning)
        result = await router.route(tid, "hello", [], project.id)

        assert result.focus == project.id
        assert result.continuation is True

    async def test_fallback_to_daily_when_no_focus(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t_fallback2"
        daily = _daily_space(tid)
        project = _project_space(tid)
        await state.save_context_space(daily)
        await state.save_context_space(project)

        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(side_effect=Exception("API down"))

        router = LLMRouter(state, mock_reasoning)
        result = await router.route(tid, "hello", [], "")

        assert result.focus == daily.id


# ---------------------------------------------------------------------------
# TestLLMRouterFocusValidation
# ---------------------------------------------------------------------------


class TestLLMRouterFocusValidation:
    """When LLM returns an invalid focus (e.g. topic hint), falls back to daily."""

    async def test_topic_hint_focus_falls_back_to_daily(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t_focus_val"
        daily = _daily_space(tid)
        project = _project_space(tid)
        await state.save_context_space(daily)
        await state.save_context_space(project)

        llm_response = json.dumps({
            "tags": [daily.id, "legal_work"],
            "focus": "legal_work",   # Not a known space ID
            "continuation": False,
        })
        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(return_value=llm_response)

        router = LLMRouter(state, mock_reasoning)
        result = await router.route(tid, "legal stuff", [], "")

        # Focus should fall back to daily (not the topic hint)
        assert result.focus == daily.id
        # But the hint can still be in tags
        assert "legal_work" in result.tags


# ---------------------------------------------------------------------------
# TestGetRecentFull
# ---------------------------------------------------------------------------


class TestGetRecentFull:
    """get_recent_full returns full metadata including timestamp and space_tags."""

    async def test_returns_full_entries(self, tmp_path):
        store = JsonConversationStore(tmp_path)
        tid = "t_full"
        cid = "conv1"

        entry = {
            "role": "user",
            "content": "hello",
            "timestamp": "2026-03-10T10:00:00+00:00",
            "platform": "discord",
            "instance_id": tid,
            "conversation_id": cid,
            "space_tags": ["space_daily"],
        }
        await store.append(tid, cid, entry)

        results = await store.get_recent_full(tid, cid, limit=20)
        assert len(results) == 1
        assert results[0]["timestamp"] == "2026-03-10T10:00:00+00:00"
        assert results[0]["space_tags"] == ["space_daily"]
        assert results[0]["role"] == "user"

    async def test_returns_empty_for_missing_conversation(self, tmp_path):
        store = JsonConversationStore(tmp_path)
        results = await store.get_recent_full("no_tenant", "no_conv")
        assert results == []

    async def test_limit_respected(self, tmp_path):
        store = JsonConversationStore(tmp_path)
        tid = "t_limit"
        cid = "conv2"
        for i in range(10):
            await store.append(tid, cid, {"role": "user", "content": f"msg{i}", "space_tags": []})

        results = await store.get_recent_full(tid, cid, limit=5)
        assert len(results) == 5
        assert results[-1]["content"] == "msg9"


# ---------------------------------------------------------------------------
# TestGetSpaceThread
# ---------------------------------------------------------------------------


class TestGetSpaceThread:
    """get_space_thread filters messages by space_id in space_tags."""

    async def test_filters_by_space_id(self, tmp_path):
        store = JsonConversationStore(tmp_path)
        tid = "t_thread"
        cid = "conv_thread"

        await store.append(tid, cid, {"role": "user", "content": "daily msg", "space_tags": ["space_daily"]})
        await store.append(tid, cid, {"role": "user", "content": "project msg", "space_tags": ["space_project"]})
        await store.append(tid, cid, {"role": "assistant", "content": "project reply", "space_tags": ["space_project"]})

        thread = await store.get_space_thread(tid, cid, "space_project")
        assert len(thread) == 2
        assert all("project" in m["content"] for m in thread)

    async def test_returns_role_and_content_only(self, tmp_path):
        store = JsonConversationStore(tmp_path)
        tid = "t_thread2"
        cid = "conv_thread2"

        await store.append(tid, cid, {
            "role": "user", "content": "test", "space_tags": ["space_x"],
            "timestamp": "2026-03-10T10:00:00+00:00", "instance_id": tid,
        })
        thread = await store.get_space_thread(tid, cid, "space_x")
        assert len(thread) == 1
        assert set(thread[0].keys()) == {"role", "content"}


# ---------------------------------------------------------------------------
# TestGetSpaceThreadUntagged
# ---------------------------------------------------------------------------


class TestGetSpaceThreadUntagged:
    """include_untagged=True includes messages without space_tags."""

    async def test_include_untagged_true(self, tmp_path):
        store = JsonConversationStore(tmp_path)
        tid = "t_untagged"
        cid = "conv_untagged"

        await store.append(tid, cid, {"role": "user", "content": "old msg"})  # no space_tags key
        await store.append(tid, cid, {"role": "user", "content": "tagged msg", "space_tags": ["space_daily"]})

        thread = await store.get_space_thread(tid, cid, "space_daily", include_untagged=True)
        assert len(thread) == 2

    async def test_include_untagged_false_excludes_untagged(self, tmp_path):
        store = JsonConversationStore(tmp_path)
        tid = "t_untagged2"
        cid = "conv_untagged2"

        await store.append(tid, cid, {"role": "user", "content": "old msg"})  # no space_tags key
        await store.append(tid, cid, {"role": "user", "content": "tagged msg", "space_tags": ["space_daily"]})

        thread = await store.get_space_thread(tid, cid, "space_daily", include_untagged=False)
        assert len(thread) == 1
        assert thread[0]["content"] == "tagged msg"


# ---------------------------------------------------------------------------
# TestGetSpaceThreadExcludes
# ---------------------------------------------------------------------------


class TestGetSpaceThreadExcludes:
    """Messages from other spaces are not included in a space thread."""

    async def test_other_space_excluded(self, tmp_path):
        store = JsonConversationStore(tmp_path)
        tid = "t_excl"
        cid = "conv_excl"

        await store.append(tid, cid, {"role": "user", "content": "space a msg", "space_tags": ["space_a"]})
        await store.append(tid, cid, {"role": "user", "content": "space b msg", "space_tags": ["space_b"]})

        thread_a = await store.get_space_thread(tid, cid, "space_a")
        assert len(thread_a) == 1
        assert thread_a[0]["content"] == "space a msg"


# ---------------------------------------------------------------------------
# TestGetCrossDomainMessages
# ---------------------------------------------------------------------------


class TestGetCrossDomainMessages:
    """get_cross_domain_messages returns messages from other spaces."""

    async def test_returns_other_space_messages(self, tmp_path):
        store = JsonConversationStore(tmp_path)
        tid = "t_cross"
        cid = "conv_cross"

        await store.append(tid, cid, {"role": "user", "content": "active msg", "space_tags": ["space_active"]})
        await store.append(tid, cid, {"role": "user", "content": "other msg", "space_tags": ["space_other"]})
        await store.append(tid, cid, {"role": "assistant", "content": "other reply", "space_tags": ["space_other"]})

        cross = await store.get_cross_domain_messages(tid, cid, "space_active", last_n_turns=5)
        assert len(cross) == 2
        assert all("other" in m["content"] for m in cross)

    async def test_includes_timestamp(self, tmp_path):
        store = JsonConversationStore(tmp_path)
        tid = "t_cross2"
        cid = "conv_cross2"

        await store.append(tid, cid, {
            "role": "user", "content": "other msg",
            "space_tags": ["space_other"], "timestamp": "2026-03-10T10:00:00+00:00"
        })

        cross = await store.get_cross_domain_messages(tid, cid, "space_active")
        assert cross[0]["timestamp"] == "2026-03-10T10:00:00+00:00"


# ---------------------------------------------------------------------------
# TestGetCrossDomainExcludesUntagged
# ---------------------------------------------------------------------------


class TestGetCrossDomainExcludesUntagged:
    """Untagged messages (pre-v2) not included in cross-domain results."""

    async def test_untagged_excluded(self, tmp_path):
        store = JsonConversationStore(tmp_path)
        tid = "t_excl_untag"
        cid = "conv_excl_untag"

        await store.append(tid, cid, {"role": "user", "content": "old msg"})  # no space_tags
        await store.append(tid, cid, {"role": "user", "content": "other msg", "space_tags": ["space_other"]})

        cross = await store.get_cross_domain_messages(tid, cid, "space_active")
        assert len(cross) == 1
        assert cross[0]["content"] == "other msg"

    async def test_active_space_messages_excluded(self, tmp_path):
        store = JsonConversationStore(tmp_path)
        tid = "t_excl_active"
        cid = "conv_excl_active"

        await store.append(tid, cid, {"role": "user", "content": "active msg", "space_tags": ["space_active"]})
        await store.append(tid, cid, {"role": "user", "content": "other msg", "space_tags": ["space_other"]})

        cross = await store.get_cross_domain_messages(tid, cid, "space_active")
        assert len(cross) == 1
        assert cross[0]["content"] == "other msg"


# ---------------------------------------------------------------------------
# TestTokenBudgetTruncation
# ---------------------------------------------------------------------------


class TestTokenBudgetTruncation:
    """_truncate_to_budget drops oldest messages to fit within token budget."""

    def test_truncates_oldest_first(self, tmp_path):
        from kernos.messages.handler import MessageHandler

        # Build a minimal handler just to test the method
        handler = _make_handler(tmp_path)[0]

        # 5 messages, each ~100 chars = ~25 tokens
        messages = [{"role": "user", "content": "x" * 100} for _ in range(5)]
        # budget = 60 tokens allows ~2-3 messages (each ~25 tokens)
        result = handler._truncate_to_budget(messages, budget_tokens=60)
        assert len(result) < len(messages)
        # The newest messages are preserved
        assert result[-1] == messages[-1]

    def test_preserves_at_least_two_messages(self, tmp_path):
        handler = _make_handler(tmp_path)[0]
        messages = [{"role": "user", "content": "x" * 400} for _ in range(5)]
        result = handler._truncate_to_budget(messages, budget_tokens=10)
        # Never drops below 2
        assert len(result) >= 2

    def test_no_truncation_needed(self, tmp_path):
        handler = _make_handler(tmp_path)[0]
        messages = [{"role": "user", "content": "short"} for _ in range(3)]
        result = handler._truncate_to_budget(messages, budget_tokens=4000)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# TestTopicHintCounting
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestQueryCovenantRulesScoped
# ---------------------------------------------------------------------------


class TestQueryCovenantRulesScoped:
    """context_space_scope filters correctly."""

    async def test_scope_returns_scoped_and_global(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t_rules"
        now = _now()
        global_rule = CovenantRule(
            id="rule_global", instance_id=tid, capability="general",
            rule_type="must", description="Global rule", active=True,
            source="default", context_space=None, created_at=now, updated_at=now,
        )
        scoped_a = CovenantRule(
            id="rule_a", instance_id=tid, capability="general",
            rule_type="must", description="Space A rule", active=True,
            source="default", context_space="space_a", created_at=now, updated_at=now,
        )
        scoped_b = CovenantRule(
            id="rule_b", instance_id=tid, capability="general",
            rule_type="must", description="Space B rule", active=True,
            source="default", context_space="space_b", created_at=now, updated_at=now,
        )
        await state.add_contract_rule(global_rule)
        await state.add_contract_rule(scoped_a)
        await state.add_contract_rule(scoped_b)

        rules = await state.query_covenant_rules(tid, context_space_scope=["space_a", None])
        rule_ids = {r.id for r in rules}
        assert "rule_global" in rule_ids
        assert "rule_a" in rule_ids
        assert "rule_b" not in rule_ids


# ---------------------------------------------------------------------------
# TestQueryCovenantRulesGlobal
# ---------------------------------------------------------------------------


class TestQueryCovenantRulesGlobal:
    """scope=None returns all rules."""

    async def test_scope_none_returns_all(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t_rules_all"
        now = _now()
        r1 = CovenantRule(
            id="rule_1", instance_id=tid, capability="general",
            rule_type="must", description="Rule 1", active=True,
            source="default", context_space=None, created_at=now, updated_at=now,
        )
        r2 = CovenantRule(
            id="rule_2", instance_id=tid, capability="general",
            rule_type="must", description="Rule 2", active=True,
            source="default", context_space="space_x", created_at=now, updated_at=now,
        )
        await state.add_contract_rule(r1)
        await state.add_contract_rule(r2)

        rules = await state.query_covenant_rules(tid, context_space_scope=None)
        assert len(rules) == 2


# ---------------------------------------------------------------------------
# TestSystemPromptPosture
# ---------------------------------------------------------------------------


class TestSystemPromptPosture:
    """Posture injected for non-daily space."""

    def _make_soul(self) -> Soul:
        return Soul(instance_id="t_prompt", user_name="the design review")

    def _make_message(self):
        return NormalizedMessage(
            content="hello",
            sender="user123",
            sender_auth_level=AuthLevel.owner_verified,
            platform="discord",
            platform_capabilities=["text"],
            conversation_id="conv1",
            timestamp=datetime.now(timezone.utc),
            instance_id="discord:user123",
        )

    def test_posture_injected_for_non_daily_space(self):
        from kernos.messages.handler import _build_system_prompt
        from kernos.kernel.template import PRIMARY_TEMPLATE

        soul = self._make_soul()
        msg = self._make_message()
        space = _project_space("t_prompt", posture="Be focused and methodical.")
        prompt = _build_system_prompt(
            msg, "caps", soul, PRIMARY_TEMPLATE, [], active_space=space,
        )
        assert "Current operating context: Test Project" in prompt
        assert "Be focused and methodical." in prompt

    def test_no_posture_for_daily_space(self):
        from kernos.messages.handler import _build_system_prompt
        from kernos.kernel.template import PRIMARY_TEMPLATE

        soul = self._make_soul()
        msg = self._make_message()
        space = _daily_space("t_prompt")
        prompt = _build_system_prompt(
            msg, "caps", soul, PRIMARY_TEMPLATE, [], active_space=space,
        )
        assert "Current operating context" not in prompt

    def test_no_posture_when_empty(self):
        from kernos.messages.handler import _build_system_prompt
        from kernos.kernel.template import PRIMARY_TEMPLATE

        soul = self._make_soul()
        msg = self._make_message()
        space = _project_space("t_prompt", posture="")
        prompt = _build_system_prompt(
            msg, "caps", soul, PRIMARY_TEMPLATE, [], active_space=space,
        )
        assert "Current operating context" not in prompt


# ---------------------------------------------------------------------------
# TestSystemPromptNoPostureDaily
# ---------------------------------------------------------------------------


class TestSystemPromptNoPostureDaily:
    """No posture injected for daily space."""

    def test_no_posture_when_no_space(self):
        from kernos.messages.handler import _build_system_prompt
        from kernos.kernel.template import PRIMARY_TEMPLATE

        soul = Soul(instance_id="t2", user_name="the design review")
        msg = NormalizedMessage(
            content="hello", sender="u", sender_auth_level=AuthLevel.owner_verified,
            platform="discord", platform_capabilities=["text"],
            conversation_id="c1", timestamp=datetime.now(timezone.utc), instance_id="u",
        )
        prompt = _build_system_prompt(msg, "caps", soul, PRIMARY_TEMPLATE, [], active_space=None)
        assert "Current operating context" not in prompt


# ---------------------------------------------------------------------------
# TestSystemPromptCrossDomainPrefix
# ---------------------------------------------------------------------------


class TestSystemPromptCrossDomainPrefix:
    """cross_domain_prefix appears in system prompt."""

    def test_cross_domain_prefix_injected(self):
        from kernos.messages.handler import _build_system_prompt
        from kernos.kernel.template import PRIMARY_TEMPLATE

        soul = Soul(instance_id="t3", user_name="the design review")
        msg = NormalizedMessage(
            content="hello", sender="u", sender_auth_level=AuthLevel.owner_verified,
            platform="discord", platform_capabilities=["text"],
            conversation_id="c1", timestamp=datetime.now(timezone.utc), instance_id="u",
        )
        # The prefix now comes pre-formatted from _assemble_space_context with section headers
        prefix = (
            "## Recent activity in other areas (background — read but do not dwell on):\n"
            "[User, 2026-03-10T09:00:00]: Legal work stuff"
        )
        prompt = _build_system_prompt(
            msg, "caps", soul, PRIMARY_TEMPLATE, [],
            cross_domain_prefix=prefix,
        )
        assert "Recent activity in other areas" in prompt
        assert "Legal work stuff" in prompt

    def test_no_cross_domain_when_none(self):
        from kernos.messages.handler import _build_system_prompt
        from kernos.kernel.template import PRIMARY_TEMPLATE

        soul = Soul(instance_id="t4", user_name="the design review")
        msg = NormalizedMessage(
            content="hello", sender="u", sender_auth_level=AuthLevel.owner_verified,
            platform="discord", platform_capabilities=["text"],
            conversation_id="c1", timestamp=datetime.now(timezone.utc), instance_id="u",
        )
        prompt = _build_system_prompt(msg, "caps", soul, PRIMARY_TEMPLATE, [], cross_domain_prefix=None)
        assert "Recent activity in other areas" not in prompt


# ---------------------------------------------------------------------------
# Handler fixture
# ---------------------------------------------------------------------------


def _make_handler(tmp_path):
    """Create a MessageHandler with mocked conversations and provider."""
    from kernos.capability.client import MCPClientManager
    from kernos.capability.registry import CapabilityRegistry
    from kernos.kernel.engine import TaskEngine
    from kernos.kernel.reasoning import (
        ContentBlock,
        Provider,
        ProviderResponse,
        ReasoningService,
    )
    from kernos.messages.handler import MessageHandler
    from kernos.persistence import AuditStore, ConversationStore, InstanceStore

    mcp = MagicMock(spec=MCPClientManager)
    mcp.get_tools.return_value = []

    conversations = AsyncMock(spec=ConversationStore)
    conversations.get_recent.return_value = []
    conversations.get_recent_full.return_value = []
    conversations.get_space_thread.return_value = []
    conversations.get_cross_domain_messages.return_value = []
    conversations.append.return_value = None

    tenants = AsyncMock(spec=InstanceStore)
    tenants.get_or_create.return_value = {
        "instance_id": "discord:user1",
        "status": "active",
        "created_at": "2026-03-01T00:00:00Z",
        "capabilities": {},
    }

    audit = AsyncMock(spec=AuditStore)
    audit.log.return_value = None
    events = JsonEventStream(tmp_path)
    state = JsonStateStore(tmp_path)

    registry = MagicMock(spec=CapabilityRegistry)
    registry.get_connected_tools.return_value = []
    registry.build_capability_prompt.return_value = "CURRENT CAPABILITIES — conversation only."
    registry.build_tool_directory.return_value = "CONNECTED SERVICES: Test\nYour tools are listed in your tool definitions."
    registry.get_preloaded_tools.return_value = []
    registry.get_lazy_tool_stubs.return_value = []
    registry.get_tool_schema.return_value = None
    registry.get_all.return_value = []

    mock_provider = AsyncMock(spec=Provider)
    mock_provider.complete.return_value = ProviderResponse(
        content=[ContentBlock(type="text", text="Hello back!")],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=20,
    )

    reasoning = ReasoningService(mock_provider, events, mcp, audit)
    engine = TaskEngine(reasoning=reasoning, events=events)

    handler = MessageHandler(
        mcp=mcp,
        conversations=conversations,
        tenants=tenants,
        audit=audit,
        events=events,
        state=state,
        reasoning=reasoning,
        registry=registry,
        engine=engine,
    )
    handler.preference_parsing_enabled = False
    return handler, mock_provider


# ---------------------------------------------------------------------------
# TestHandlerSpaceSwitch
# ---------------------------------------------------------------------------


class TestHandlerSpaceSwitch:
    """Space switch updates last_active_space_id and emits event."""

    async def test_space_switch_updates_profile(self, tmp_path):
        handler, _ = _make_handler(tmp_path)
        tid = "discord:user1"

        # Set up daily space and profile with daily as focus
        daily = _daily_space(tid)
        project = _project_space(tid)
        await handler.state.save_context_space(daily)
        await handler.state.save_context_space(project)

        now = _now()
        profile = InstanceProfile(
            instance_id=tid, status="active", created_at=now,
            last_active_space_id=daily.id,
        )
        await handler.state.save_instance_profile(tid, profile)

        # Mock router to say we're switching to project
        mock_router = AsyncMock()
        mock_router.route = AsyncMock(return_value=RouterResult(
            tags=[project.id], focus=project.id, continuation=False,
        ))
        handler._router = mock_router

        await handler.process(_msg("project work"))

        loaded = await handler.state.get_instance_profile(tid)
        assert loaded.last_active_space_id == project.id

    async def test_space_switch_emits_event(self, tmp_path):
        handler, _ = _make_handler(tmp_path)
        tid = "discord:user1"

        daily = _daily_space(tid)
        project = _project_space(tid)
        await handler.state.save_context_space(daily)
        await handler.state.save_context_space(project)

        now = _now()
        profile = InstanceProfile(
            instance_id=tid, status="active", created_at=now,
            last_active_space_id=daily.id,
        )
        await handler.state.save_instance_profile(tid, profile)

        mock_router = AsyncMock()
        mock_router.route = AsyncMock(return_value=RouterResult(
            tags=[project.id], focus=project.id, continuation=False,
        ))
        handler._router = mock_router

        await handler.process(_msg("project work"))

        events = await handler.events.query(
            instance_id=tid,
            event_types=[EventType.CONTEXT_SPACE_SWITCHED],
        )
        assert len(events) >= 1
        payload = events[-1].payload
        assert payload["from_space"] == daily.id
        assert payload["to_space"] == project.id


# ---------------------------------------------------------------------------
# TestHandlerSpaceTagsSaved
# ---------------------------------------------------------------------------


class TestHandlerSpaceTagsSaved:
    """Messages saved with router_result.tags."""

    async def test_user_message_saved_with_space_tags(self, tmp_path):
        handler, _ = _make_handler(tmp_path)
        tid = "discord:user1"

        daily = _daily_space(tid)
        await handler.state.save_context_space(daily)

        # Track appended entries
        appended = []
        original_append = handler.conversations.append

        async def track_append(t, c, entry):
            appended.append(entry)
            return None

        handler.conversations.append = track_append

        mock_router = AsyncMock()
        mock_router.route = AsyncMock(return_value=RouterResult(
            tags=[daily.id], focus=daily.id, continuation=False,
        ))
        handler._router = mock_router

        await handler.process(_msg("hello"))

        # First appended entry should be the user message with space_tags
        user_entries = [e for e in appended if e.get("role") == "user"]
        assert len(user_entries) >= 1
        assert user_entries[0]["space_tags"] == [daily.id]


# ---------------------------------------------------------------------------
# TestHandlerDailyOnly
# ---------------------------------------------------------------------------


class TestHandlerSingleSpace:
    """Single-space tenant: LLM router fires (bypass removed), routes to General."""

    async def test_single_space_calls_llm_router(self, tmp_path):
        handler, mock_provider = _make_handler(tmp_path)
        tid = "discord:user1"

        # Create only a general space
        general = _daily_space(tid)
        await handler.state.save_context_space(general)

        # Replace router's reasoning to return valid JSON
        import json
        mock_router_reasoning = AsyncMock()
        mock_router_reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "tags": [general.id], "focus": general.id, "continuation": False,
        }))
        handler._router = LLMRouter(handler.state, mock_router_reasoning)

        response = await handler.process(_msg("hello"))
        assert response  # Got a response

        # LLM router IS called even for single-space tenant (bypass removed)
        mock_router_reasoning.complete_simple.assert_called_once()

    async def test_single_space_no_switch_event(self, tmp_path):
        handler, _ = _make_handler(tmp_path)
        tid = "discord:user1"

        # No spaces yet (will be created during process)
        response = await handler.process(_msg("hello"))
        assert response

        events = await handler.events.query(
            instance_id=tid,
            event_types=[EventType.CONTEXT_SPACE_SWITCHED],
        )
        assert len(events) == 0


# ---------------------------------------------------------------------------
# TestKnowledgeScoping
# ---------------------------------------------------------------------------


class TestKnowledgeScoping:
    """User structural facts stay global, others get space_id."""

    async def test_user_structural_fact_always_global(self):
        from kernos.kernel.projectors.llm_extractor import _write_entry
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            state = JsonStateStore(td)
            events = JsonEventStream(td)
            now = _now()

            wrote = await _write_entry(
                state=state, events=events, instance_id="t_scope",
                category="fact", subject="user", content="Lives in Portland",
                confidence="stated", lifecycle_archetype="structural",
                source_description="test", existing_hashes=set(), now=now,
                tags=["fact"], context_space="",
            )
            assert wrote == 1
            entries = await state.query_knowledge("t_scope")
            assert len(entries) == 1
            assert entries[0].context_space == ""

    async def test_non_user_fact_gets_space_id(self):
        from kernos.kernel.projectors.llm_extractor import _write_entry
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            state = JsonStateStore(td)
            events = JsonEventStream(td)
            now = _now()

            wrote = await _write_entry(
                state=state, events=events, instance_id="t_scope2",
                category="fact", subject="Sarah", content="Working on Project X",
                confidence="stated", lifecycle_archetype="contextual",
                source_description="test", existing_hashes=set(), now=now,
                tags=["fact"], context_space="space_project",
            )
            assert wrote == 1
            entries = await state.query_knowledge("t_scope2")
            assert len(entries) == 1
            assert entries[0].context_space == "space_project"


# ---------------------------------------------------------------------------
# TestInstanceProfileSpaceField
# ---------------------------------------------------------------------------


class TestInstanceProfileSpaceField:
    """last_active_space_id persists correctly."""

    async def test_new_field_defaults_empty(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t_field"
        profile = InstanceProfile(
            instance_id=tid, status="active", created_at=_now(),
        )
        await state.save_instance_profile(tid, profile)
        loaded = await state.get_instance_profile(tid)
        assert loaded.last_active_space_id == ""

    async def test_field_persists(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t_field2"
        profile = InstanceProfile(
            instance_id=tid, status="active", created_at=_now(),
            last_active_space_id="space_abc",
        )
        await state.save_instance_profile(tid, profile)
        loaded = await state.get_instance_profile(tid)
        assert loaded.last_active_space_id == "space_abc"
