"""Tests for Kernos Self-Knowledge Reference System.

Covers: read_source tool (security, file reading, section extraction),
read_soul / update_soul tools (state-store backed), kernos-reference.md
provisioning, tool registration, soul defaults, bootstrap prompt, and
two-layer identity architecture.
"""
import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.reasoning import (
    READ_SOURCE_TOOL,
    READ_SOUL_TOOL,
    UPDATE_SOUL_TOOL,
    ReasoningService,
    _read_source,
    _SOUL_UPDATABLE_FIELDS,
)
from kernos.kernel.soul import Soul
from kernos.kernel.state_json import JsonStateStore
from kernos.kernel.template import PRIMARY_TEMPLATE


# ---------------------------------------------------------------------------
# read_source: Valid paths
# ---------------------------------------------------------------------------


class TestReadSourceValidPaths:
    def test_read_existing_file(self):
        result = _read_source("kernel/awareness.py")
        assert "AwarenessEvaluator" in result
        assert "class AwarenessEvaluator" in result

    def test_read_reasoning_module(self):
        result = _read_source("kernel/reasoning.py")
        assert "ReasoningService" in result

    def test_read_handler(self):
        result = _read_source("messages/handler.py", section="MessageHandler")
        assert "MessageHandler" in result

    def test_read_event_types(self):
        result = _read_source("kernel/event_types.py")
        assert "PROACTIVE_INSIGHT" in result

    def test_read_init(self):
        result = _read_source("__init__.py")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# read_source: Security
# ---------------------------------------------------------------------------


class TestReadSourceSecurity:
    def test_reject_path_traversal(self):
        result = _read_source("../secrets/api.key")
        assert "Error" in result
        assert "not allowed" in result

    def test_reject_absolute_path(self):
        result = _read_source("/etc/passwd")
        assert "Error" in result
        assert "Absolute paths" in result

    def test_reject_double_dot_in_middle(self):
        result = _read_source("kernel/../../../etc/passwd")
        assert "Error" in result

    def test_reject_backslash_absolute(self):
        result = _read_source("\\etc\\passwd")
        assert "Error" in result

    def test_nonexistent_file(self):
        result = _read_source("kernel/nonexistent_module.py")
        assert "Error" in result
        assert "not found" in result

    def test_reject_binary_file(self):
        # .pyc files should be rejected
        result = _read_source("__pycache__/something.pyc")
        if "not found" not in result:
            assert "Error" in result

    def test_directory_rejected(self):
        result = _read_source("kernel")
        assert "Error" in result
        assert "Not a file" in result


# ---------------------------------------------------------------------------
# read_source: Section extraction
# ---------------------------------------------------------------------------


class TestReadSourceSections:
    def test_extract_class(self):
        result = _read_source("kernel/awareness.py", "AwarenessEvaluator")
        assert "class AwarenessEvaluator" in result
        assert "def __init__" in result
        assert "run_time_pass" in result
        # Should NOT contain Whisper class (different section)
        assert "class Whisper" not in result

    def test_extract_method(self):
        result = _read_source("kernel/awareness.py", "run_time_pass")
        assert "async def run_time_pass" in result
        assert "foresight_expires" in result
        # Should be a focused extraction, not the whole file
        assert "class AwarenessEvaluator" not in result

    def test_extract_async_def(self):
        result = _read_source("kernel/awareness.py", "_evaluate")
        assert "async def _evaluate" in result

    def test_extract_format_time_insight(self):
        result = _read_source("kernel/awareness.py", "_format_time_insight")
        assert "def _format_time_insight" in result
        assert "urgency" in result

    def test_section_not_found(self):
        result = _read_source("kernel/awareness.py", "NonExistentClass")
        assert "Error" in result
        assert "not found" in result

    def test_extract_read_source_itself(self):
        result = _read_source("kernel/tools/schemas.py", "read_source")
        assert "def read_source" in result
        assert "Security" in result or "security" in result.lower()


# ---------------------------------------------------------------------------
# read_source: Truncation
# ---------------------------------------------------------------------------


class TestReadSourceTruncation:
    def test_large_file_truncated(self):
        result = _read_source("messages/handler.py")
        # handler.py is >500 lines, should be truncated
        assert "truncated" in result.lower()

    def test_small_file_not_truncated(self):
        result = _read_source("kernel/event_types.py")
        # event_types.py is <500 lines
        assert "truncated" not in result.lower()


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


class TestReadSourceToolDefinition:
    def test_tool_shape(self):
        assert READ_SOURCE_TOOL["name"] == "read_source"
        schema = READ_SOURCE_TOOL["input_schema"]
        assert "path" in schema["properties"]
        assert "section" in schema["properties"]
        assert schema["required"] == ["path"]

    def test_in_kernel_tools(self):
        assert "read_source" in ReasoningService._KERNEL_TOOLS

    def test_classified_as_read(self):
        provider = MagicMock()
        events = MagicMock()
        mcp = MagicMock()
        audit = MagicMock()
        r = ReasoningService(provider, events, mcp, audit)
        assert r._classify_tool_effect("read_source", None) == "read"


# ---------------------------------------------------------------------------
# kernos-reference.md
# ---------------------------------------------------------------------------


class TestDocsHint:
    def test_docs_hint_exists(self):
        from kernos.messages.reference import DOCS_HINT
        assert len(DOCS_HINT) > 20

    def test_docs_hint_references_request_reference(self):
        """REFERENCE-PRIMITIVE-V1: DOCS_HINT now points at the new
        catalog-mediated reach (request_reference) instead of the
        retired direct-path read_doc."""
        from kernos.messages.reference import DOCS_HINT
        assert "request_reference" in DOCS_HINT


class TestDocsDirectory:
    """Verify docs/ directory structure exists with expected files."""

    def test_index_exists(self):
        from pathlib import Path
        docs = Path(__file__).parent.parent / "docs"
        assert (docs / "index.md").exists()

    def test_architecture_docs_exist(self):
        from pathlib import Path
        docs = Path(__file__).parent.parent / "docs" / "architecture"
        for name in ["overview.md", "context-spaces.md", "memory.md", "soul.md", "event-stream.md"]:
            assert (docs / name).exists(), f"Missing: architecture/{name}"

    def test_capabilities_docs_exist(self):
        from pathlib import Path
        docs = Path(__file__).parent.parent / "docs" / "capabilities"
        for name in ["overview.md", "calendar.md", "web-browsing.md", "file-system.md", "memory-tools.md", "sms.md"]:
            assert (docs / name).exists(), f"Missing: capabilities/{name}"

    def test_behaviors_docs_exist(self):
        from pathlib import Path
        docs = Path(__file__).parent.parent / "docs" / "behaviors"
        for name in ["covenants.md", "dispatch-gate.md", "proactive-awareness.md", "instruction-types.md"]:
            assert (docs / name).exists(), f"Missing: behaviors/{name}"

    def test_identity_docs_exist(self):
        from pathlib import Path
        docs = Path(__file__).parent.parent / "docs" / "identity"
        for name in ["who-you-are.md", "soul-system.md", "onboarding.md"]:
            assert (docs / name).exists(), f"Missing: identity/{name}"

    def test_roadmap_docs_exist(self):
        from pathlib import Path
        docs = Path(__file__).parent.parent / "docs" / "roadmap"
        for name in ["vision.md", "whats-next.md", "future.md"]:
            assert (docs / name).exists(), f"Missing: roadmap/{name}"


class TestReadDocRetired:
    """REFERENCE-PRIMITIVE-V1: read_doc is retired. The replacement
    surface is request_reference; the old direct-path tool no
    longer exists in the kernel-tool catalog or dispatch."""

    def test_read_doc_not_in_kernel_tools(self):
        assert "read_doc" not in ReasoningService._KERNEL_TOOLS

    def test_request_reference_in_kernel_tools(self):
        assert "request_reference" in ReasoningService._KERNEL_TOOLS

    def test_request_reference_classified_as_read(self):
        provider = MagicMock()
        events = MagicMock()
        mcp = MagicMock()
        audit = MagicMock()
        r = ReasoningService(provider, events, mcp, audit)
        assert r._classify_tool_effect("request_reference", None) == "read"

    def test_vision_doc_still_present(self):
        """The canonical vision doc still ships from docs/. The agent
        reaches it via request_reference now (the old direct-path
        read_doc was retired in REFERENCE-PRIMITIVE-V1)."""
        from pathlib import Path
        vision = Path(__file__).parent.parent / "docs" / "roadmap" / "vision.md"
        assert vision.exists()
        body = vision.read_text(encoding="utf-8")
        assert "personal intelligence" in body.lower()


class TestSystemSpaceProvisioningSlimmed:
    """Verify _write_system_docs only writes capabilities-overview.md."""

    async def test_only_capabilities_overview(self, tmp_path):
        from kernos.kernel.files import FileService
        from kernos.capability.registry import CapabilityRegistry

        files = FileService(str(tmp_path))
        registry = CapabilityRegistry()

        handler = MagicMock()
        handler._files = files
        handler.registry = registry

        from kernos.messages.handler import MessageHandler
        handler._write_system_docs = MessageHandler._write_system_docs.__get__(handler)
        handler._write_capabilities_overview = MessageHandler._write_capabilities_overview.__get__(handler)

        await handler._write_system_docs("test_tenant", "space_sys_test")

        files_dir = tmp_path / "test_tenant" / "spaces" / "space_sys_test" / "files"
        assert (files_dir / "capabilities-overview.md").exists()
        # Deprecated files should NOT be written
        assert not (files_dir / "how-to-connect-tools.md").exists()
        assert not (files_dir / "kernos-reference.md").exists()
        assert not (files_dir / "how-i-work.md").exists()


# ---------------------------------------------------------------------------
# read_soul / update_soul
# ---------------------------------------------------------------------------


class TestReadSoul:
    """read_soul returns soul.json fields via state store."""

    async def test_read_soul_returns_soul_fields(self, tmp_path):
        """Call read_soul. Verify it returns soul.json fields (agent_name, emoji, etc.)."""
        store = JsonStateStore(str(tmp_path))
        soul = Soul(instance_id="t1", agent_name="Rex", emoji="🔥", personality_notes="bold")
        await store.save_soul(soul)

        # Wire up a ReasoningService with state
        provider = MagicMock()
        events = MagicMock()
        mcp = MagicMock()
        audit = MagicMock()
        r = ReasoningService(provider, events, mcp, audit)
        r.set_state(store)

        # Simulate read_soul via execute_tool
        from kernos.kernel.reasoning import ReasoningRequest
        request = ReasoningRequest(
            instance_id="t1", conversation_id="c1", system_prompt="",
            messages=[], tools=[], model="test", trigger="test",
        )
        result = await r.execute_tool("read_soul", {}, request)
        parsed = json.loads(result)
        assert parsed["agent_name"] == "Rex"
        assert parsed["emoji"] == "🔥"
        assert parsed["personality_notes"] == "bold"
        assert parsed["instance_id"] == "t1"

    async def test_read_soul_no_soul_found(self):
        """read_soul returns error when no soul exists."""
        state = AsyncMock()
        state.get_soul.return_value = None

        provider = MagicMock()
        events = MagicMock()
        mcp = MagicMock()
        audit = MagicMock()
        r = ReasoningService(provider, events, mcp, audit)
        r.set_state(state)

        from kernos.kernel.reasoning import ReasoningRequest
        request = ReasoningRequest(
            instance_id="t1", conversation_id="c1", system_prompt="",
            messages=[], tools=[], model="test", trigger="test",
        )
        result = await r.execute_tool("read_soul", {}, request)
        assert "No soul found" in result


class TestUpdateSoul:
    """update_soul accepts field name + value, writes to soul.json."""

    async def test_update_agent_name(self, tmp_path):
        """Call update_soul to change agent_name to 'Rex'. Call read_soul. Verify."""
        store = JsonStateStore(str(tmp_path))
        soul = Soul(instance_id="t1")
        await store.save_soul(soul)

        provider = MagicMock()
        events = MagicMock()
        mcp = MagicMock()
        audit = MagicMock()
        r = ReasoningService(provider, events, mcp, audit)
        r.set_state(store)

        from kernos.kernel.reasoning import ReasoningRequest
        request = ReasoningRequest(
            instance_id="t1", conversation_id="c1", system_prompt="",
            messages=[], tools=[], model="test", trigger="test",
        )

        # Update
        result = await r.execute_tool("update_soul", {"field": "agent_name", "value": "Rex"}, request)
        assert "Updated" in result
        assert "Rex" in result

        # Read back
        result = await r.execute_tool("read_soul", {}, request)
        parsed = json.loads(result)
        assert parsed["agent_name"] == "Rex"

        # Verify soul.json on disk
        disk_data = json.loads((tmp_path / "t1" / "state" / "soul.json").read_text())
        assert disk_data["agent_name"] == "Rex"

    async def test_reject_hatched_update(self, tmp_path):
        """Call update_soul to change hatched. Verify it returns an error."""
        store = JsonStateStore(str(tmp_path))
        soul = Soul(instance_id="t1")
        await store.save_soul(soul)

        provider = MagicMock()
        events = MagicMock()
        mcp = MagicMock()
        audit = MagicMock()
        r = ReasoningService(provider, events, mcp, audit)
        r.set_state(store)

        from kernos.kernel.reasoning import ReasoningRequest
        request = ReasoningRequest(
            instance_id="t1", conversation_id="c1", system_prompt="",
            messages=[], tools=[], model="test", trigger="test",
        )
        result = await r.execute_tool("update_soul", {"field": "hatched", "value": "true"}, request)
        assert "Cannot update" in result

    async def test_reject_interaction_count_update(self, tmp_path):
        """Call update_soul to change interaction_count. Verify it returns an error."""
        store = JsonStateStore(str(tmp_path))
        soul = Soul(instance_id="t1")
        await store.save_soul(soul)

        provider = MagicMock()
        events = MagicMock()
        mcp = MagicMock()
        audit = MagicMock()
        r = ReasoningService(provider, events, mcp, audit)
        r.set_state(store)

        from kernos.kernel.reasoning import ReasoningRequest
        request = ReasoningRequest(
            instance_id="t1", conversation_id="c1", system_prompt="",
            messages=[], tools=[], model="test", trigger="test",
        )
        result = await r.execute_tool("update_soul", {"field": "interaction_count", "value": "99"}, request)
        assert "Cannot update" in result

    async def test_reject_bootstrap_graduated_update(self, tmp_path):
        """bootstrap_graduated is not updatable via update_soul."""
        store = JsonStateStore(str(tmp_path))
        soul = Soul(instance_id="t1")
        await store.save_soul(soul)

        provider = MagicMock()
        events = MagicMock()
        mcp = MagicMock()
        audit = MagicMock()
        r = ReasoningService(provider, events, mcp, audit)
        r.set_state(store)

        from kernos.kernel.reasoning import ReasoningRequest
        request = ReasoningRequest(
            instance_id="t1", conversation_id="c1", system_prompt="",
            messages=[], tools=[], model="test", trigger="test",
        )
        result = await r.execute_tool("update_soul", {"field": "bootstrap_graduated", "value": "true"}, request)
        assert "Cannot update" in result

    async def test_reject_user_name_update(self, tmp_path):
        """user_name is not updatable via update_soul."""
        store = JsonStateStore(str(tmp_path))
        soul = Soul(instance_id="t1")
        await store.save_soul(soul)

        provider = MagicMock()
        events = MagicMock()
        mcp = MagicMock()
        audit = MagicMock()
        r = ReasoningService(provider, events, mcp, audit)
        r.set_state(store)

        from kernos.kernel.reasoning import ReasoningRequest
        request = ReasoningRequest(
            instance_id="t1", conversation_id="c1", system_prompt="",
            messages=[], tools=[], model="test", trigger="test",
        )
        result = await r.execute_tool("update_soul", {"field": "user_name", "value": "Bob"}, request)
        assert "Cannot update" in result


class TestSoulToolDefinitions:
    def test_read_soul_tool_shape(self):
        assert READ_SOUL_TOOL["name"] == "read_soul"
        schema = READ_SOUL_TOOL["input_schema"]
        assert schema["required"] == []

    def test_update_soul_tool_shape(self):
        assert UPDATE_SOUL_TOOL["name"] == "update_soul"
        schema = UPDATE_SOUL_TOOL["input_schema"]
        assert "field" in schema["properties"]
        assert "value" in schema["properties"]
        assert schema["required"] == ["field", "value"]

    def test_read_soul_in_kernel_tools(self):
        assert "read_soul" in ReasoningService._KERNEL_TOOLS

    def test_update_soul_in_kernel_tools(self):
        assert "update_soul" in ReasoningService._KERNEL_TOOLS

    def test_read_soul_classified_as_read(self):
        provider = MagicMock()
        events = MagicMock()
        mcp = MagicMock()
        audit = MagicMock()
        r = ReasoningService(provider, events, mcp, audit)
        assert r._classify_tool_effect("read_soul", None) == "read"

    def test_update_soul_classified_as_soft_write(self):
        provider = MagicMock()
        events = MagicMock()
        mcp = MagicMock()
        audit = MagicMock()
        r = ReasoningService(provider, events, mcp, audit)
        assert r._classify_tool_effect("update_soul", None) == "soft_write"


class TestDeveloperModeField:
    def test_default_false(self):
        from kernos.kernel.state import InstanceProfile
        tp = InstanceProfile(instance_id="t1", status="active", created_at="2026-01-01T00:00:00Z")
        assert tp.developer_mode is False

    def test_set_true(self):
        from kernos.kernel.state import InstanceProfile
        tp = InstanceProfile(instance_id="t1", status="active", created_at="2026-01-01T00:00:00Z", developer_mode=True)
        assert tp.developer_mode is True


class TestDocsCoverSoul:
    def test_soul_doc_exists(self):
        from pathlib import Path
        soul_doc = Path(__file__).parent.parent / "docs" / "architecture" / "soul.md"
        assert soul_doc.exists()

    def test_soul_doc_covers_tools(self):
        # Read directly from the local docs path; read_doc was
        # retired in REFERENCE-PRIMITIVE-V1, but the docs/ directory
        # still ships canonical content. The reach mechanism for
        # agents is now request_reference; for tests we read the
        # file directly.
        from pathlib import Path
        soul_doc = Path(__file__).parent.parent / "docs" / "architecture" / "soul.md"
        result = soul_doc.read_text(encoding="utf-8")
        assert "read_soul" in result
        assert "update_soul" in result


# ---------------------------------------------------------------------------
# Two-Layer Identity spec tests
# ---------------------------------------------------------------------------


class TestSoulDefaults:
    """Spec tests: soul defaults and migration."""

    def test_new_soul_has_empty_name(self):
        """Create new soul. Agent name is empty — named during per-member hatching."""
        soul = Soul(instance_id="new_t")
        assert soul.agent_name == ""

    def test_new_soul_has_empty_emoji(self):
        """Create new soul. Emoji is empty — set during per-member hatching."""
        soul = Soul(instance_id="new_t")
        assert soul.emoji == ""

    async def test_empty_soul_stays_empty(self, tmp_path):
        """Load soul.json with empty fields. No backfill — agent named during hatching."""
        store = JsonStateStore(str(tmp_path))
        state_dir = tmp_path / "t_migrate" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "soul.json").write_text(json.dumps({
            "instance_id": "t_migrate",
            "agent_name": "",
            "emoji": "",
            "personality_notes": "",
            "user_name": "",
            "user_context": "",
            "communication_style": "",
            "hatched": False,
            "hatched_at": "",
            "interaction_count": 0,
            "bootstrap_graduated": False,
            "bootstrap_graduated_at": "",
        }))
        soul = await store.get_soul("t_migrate")
        assert soul is not None
        assert soul.agent_name == ""  # No backfill — named during hatching
        assert soul.emoji == ""       # No backfill — set during hatching


class TestBootstrapPrompt:
    """Spec tests: bootstrap prompt content."""

    def test_contains_first_conversation(self):
        """Build system prompt for unhatched tenant. Assert contains first conversation marker."""
        assert "FIRST CONVERSATION" in PRIMARY_TEMPLATE.bootstrap_prompt

    def test_contains_arrival_guidance(self):
        """Assert bootstrap contains first-message arrival guidance."""
        assert "first moment" in PRIMARY_TEMPLATE.bootstrap_prompt.lower() or "let it breathe" in PRIMARY_TEMPLATE.bootstrap_prompt.lower()

    def test_no_you_just_came_online(self):
        """Assert bootstrap_prompt does NOT contain 'You just came online'."""
        assert "You just came online" not in PRIMARY_TEMPLATE.bootstrap_prompt

    def test_bootstrap_in_system_prompt_for_unhatched(self):
        """Build system prompt for unhatched member. Assert hatching prompt present."""
        from kernos.messages.handler import _build_system_prompt
        from kernos.messages.models import NormalizedMessage, AuthLevel
        from datetime import datetime, timezone

        soul = Soul(instance_id="t1")
        msg = NormalizedMessage(
            content="hello",
            sender="+15555550100",
            platform="sms",
            conversation_id="c1",
            sender_auth_level=AuthLevel.owner_verified,
            timestamp=datetime.now(timezone.utc),
            platform_capabilities=[],
            instance_id="t1",
        )
        prompt = _build_system_prompt(msg, "", soul, PRIMARY_TEMPLATE, [])
        # Both personality foundation AND hatching identity layer present
        assert "FIRST CONVERSATION" in prompt  # Personality foundation
        assert "HATCHING" in prompt             # Identity layer
        assert "don't have a name yet" in prompt.lower()


class TestNoSoulMd:
    """Spec test: SOUL.md must not appear in system prompt or codebase references."""

    def test_soul_md_not_in_codebase(self):
        """SOUL.md file should not exist in repo root."""
        from pathlib import Path
        soul_path = Path(__file__).parent.parent / "SOUL.md"
        assert not soul_path.exists(), "SOUL.md should have been deleted"

    def test_system_prompt_no_soul_md_content(self):
        """Build system prompt for any tenant. Assert SOUL.md content does not appear."""
        from kernos.messages.handler import _build_system_prompt
        from kernos.messages.models import NormalizedMessage, AuthLevel
        from datetime import datetime, timezone

        soul = Soul(instance_id="t1", bootstrap_graduated=True, user_name="Test")
        msg = NormalizedMessage(
            content="hello",
            sender="+15555550100",
            platform="sms",
            conversation_id="c1",
            sender_auth_level=AuthLevel.owner_verified,
            timestamp=datetime.now(timezone.utc),
            platform_capabilities=[],
            instance_id="t1",
        )
        prompt = _build_system_prompt(msg, "", soul, PRIMARY_TEMPLATE, [])
        # SOUL.md contained distinctive phrases that should not appear
        assert "SOUL.md" not in prompt
        assert "Owner built me" not in prompt
        assert "Trust Owner completely" not in prompt
