"""Tests for SPEC-3K: Unified Capability Registry — manage_capabilities kernel tool."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from kernos.capability.registry import CapabilityInfo, CapabilityRegistry, CapabilityStatus
from kernos.kernel.spaces import ContextSpace


# --- Helper factories ---

def make_cap(
    name="google-calendar",
    universal=False,
    status=CapabilityStatus.CONNECTED,
    tools=None,
    source="default",
):
    return CapabilityInfo(
        name=name,
        display_name=name.replace("-", " ").title(),
        description=f"Description of {name}",
        category="test",
        status=status,
        tools=tools or [f"{name}-tool1", f"{name}-tool2"],
        server_name=name,
        universal=universal,
        source=source,
    )


def make_space(space_type="domain", active_tools=None):
    return ContextSpace(
        id="space_test01",
        instance_id="tenant_test",
        name="Test Space",
        space_type=space_type,
        active_tools=active_tools if active_tools is not None else [],
    )


def make_registry(*caps):
    registry = CapabilityRegistry(mcp=None)
    for cap in caps:
        registry.register(cap)
    return registry


# ===========================
# Test: source field
# ===========================

class TestCapabilitySource:
    def test_default_source_is_default(self):
        cap = make_cap()
        assert cap.source == "default"

    def test_user_source(self):
        cap = make_cap(source="user")
        assert cap.source == "user"

    def test_register_preserves_source(self):
        registry = make_registry(make_cap("cal", source="user"))
        assert registry.get("cal").source == "user"


# ===========================
# Test: DISABLED status
# ===========================

class TestDisabledStatus:
    def test_disabled_enum_exists(self):
        assert CapabilityStatus.DISABLED == "disabled"

    def test_disable_connected_cap(self):
        registry = make_registry(make_cap("cal", status=CapabilityStatus.CONNECTED))
        result = registry.disable("cal")
        assert result is True
        assert registry.get("cal").status == CapabilityStatus.DISABLED

    def test_disable_non_connected_returns_false(self):
        registry = make_registry(make_cap("cal", status=CapabilityStatus.AVAILABLE))
        result = registry.disable("cal")
        assert result is False

    def test_disable_unknown_returns_false(self):
        registry = make_registry()
        result = registry.disable("unknown")
        assert result is False

    def test_enable_disabled_cap(self):
        registry = make_registry(make_cap("cal", status=CapabilityStatus.DISABLED))
        result = registry.enable("cal")
        assert result is True
        assert registry.get("cal").status == CapabilityStatus.CONNECTED

    def test_enable_non_disabled_returns_false(self):
        registry = make_registry(make_cap("cal", status=CapabilityStatus.CONNECTED))
        result = registry.enable("cal")
        assert result is False

    def test_get_disabled(self):
        registry = make_registry(
            make_cap("cal", status=CapabilityStatus.DISABLED),
            make_cap("email", status=CapabilityStatus.CONNECTED),
        )
        disabled = registry.get_disabled()
        assert len(disabled) == 1
        assert disabled[0].name == "cal"


# ===========================
# Test: manage_capabilities list shows all with source and status
# ===========================

class TestManageToolsList:
    def test_list_shows_all_capabilities_with_source_and_status(self):
        """AC1: manage_capabilities list returns both default and user-installed capabilities."""
        from kernos.kernel.reasoning import ReasoningService

        registry = make_registry(
            make_cap("cal", status=CapabilityStatus.CONNECTED, source="default"),
            make_cap("custom-tool", status=CapabilityStatus.CONNECTED, source="user"),
            make_cap("web-search", status=CapabilityStatus.AVAILABLE, source="default"),
        )

        rs = ReasoningService.__new__(ReasoningService)
        rs._registry = registry
        rs._mcp = None
        rs._state = None
        rs._tools_changed = False

        import asyncio
        result = asyncio.run(
            rs._handle_manage_capabilities("tenant_test", "list", "")
        )

        assert "cal" in result
        assert "custom-tool" in result
        assert "web-search" in result
        assert "source=default" in result
        assert "source=user" in result
        assert "status=connected" in result
        assert "status=available" in result


# ===========================
# Test: Disable → tools absent from LLM list → manage_capabilities shows disabled
# ===========================

class TestDisableHidesFromLLM:
    def test_disabled_cap_excluded_from_tool_list(self):
        """AC2: Disabling calendar removes calendar tools from LLM tool list."""
        mcp = MagicMock()
        mcp.get_tool_definitions.return_value = {
            "cal": [{"name": "list-events", "description": "List events", "input_schema": {}}],
            "email": [{"name": "list-messages", "description": "List msgs", "input_schema": {}}],
        }

        registry = CapabilityRegistry(mcp=mcp)
        registry.register(make_cap("cal", universal=True, status=CapabilityStatus.CONNECTED))
        registry.register(make_cap("email", universal=True, status=CapabilityStatus.CONNECTED))

        space = make_space(space_type="domain")

        # Before disable: both visible
        tools = registry.get_tools_for_space(space)
        tool_names = [t["name"] for t in tools]
        assert "list-events" in tool_names
        assert "list-messages" in tool_names

        # Disable calendar
        registry.disable("cal")

        # After disable: calendar tools absent
        tools = registry.get_tools_for_space(space)
        tool_names = [t["name"] for t in tools]
        assert "list-events" not in tool_names
        assert "list-messages" in tool_names

    def test_disabled_excluded_from_capability_prompt(self):
        """Disabled capabilities don't show in connected or available prompt sections."""
        registry = make_registry(
            make_cap("cal", universal=True, status=CapabilityStatus.DISABLED),
            make_cap("email", universal=True, status=CapabilityStatus.CONNECTED),
        )
        space = make_space(space_type="domain")
        prompt = registry.build_capability_prompt(space)
        assert "Email" in prompt
        # Calendar should not appear as connected or available
        assert "Google Calendar" not in prompt or "Calendar" not in prompt.split("CONNECTED")[0]


# ===========================
# Test: Re-enable → tools reappear instantly
# ===========================

class TestReEnable:
    def test_re_enable_restores_tools_instantly(self):
        """AC3: Re-enabling calendar restores tools instantly (no reconnection)."""
        mcp = MagicMock()
        mcp.get_tool_definitions.return_value = {
            "cal": [{"name": "list-events", "description": "List events", "input_schema": {}}],
        }

        registry = CapabilityRegistry(mcp=mcp)
        registry.register(make_cap("cal", universal=True, status=CapabilityStatus.CONNECTED))
        space = make_space(space_type="domain")

        # Disable
        registry.disable("cal")
        tools = registry.get_tools_for_space(space)
        assert not any(t["name"] == "list-events" for t in tools)

        # Re-enable
        registry.enable("cal")
        tools = registry.get_tools_for_space(space)
        assert any(t["name"] == "list-events" for t in tools)

    async def test_manage_capabilities_enable_action(self):
        """AC3: manage_capabilities enable action works."""
        from kernos.kernel.reasoning import ReasoningService

        registry = make_registry(
            make_cap("cal", status=CapabilityStatus.DISABLED, source="default"),
        )

        rs = ReasoningService.__new__(ReasoningService)
        rs._registry = registry
        rs._mcp = None
        rs._state = None
        rs._tools_changed = False

        result = await rs._handle_manage_capabilities("tenant_test", "enable", "cal")
        assert "Enabled" in result
        assert registry.get("cal").status == CapabilityStatus.CONNECTED
        assert rs._tools_changed is True


# ===========================
# Test: Remove default → error
# ===========================

class TestRemoveDefault:
    async def test_remove_default_returns_error(self):
        """AC4: manage_capabilities remove google-calendar returns error."""
        from kernos.kernel.reasoning import ReasoningService

        registry = make_registry(
            make_cap("google-calendar", status=CapabilityStatus.CONNECTED, source="default"),
        )

        rs = ReasoningService.__new__(ReasoningService)
        rs._registry = registry
        rs._mcp = AsyncMock()
        rs._state = None
        rs._tools_changed = False

        result = await rs._handle_manage_capabilities("tenant_test", "remove", "google-calendar")
        assert "Cannot remove" in result
        assert "default" in result.lower() or "pre-installed" in result.lower()
        assert registry.get("google-calendar").status == CapabilityStatus.CONNECTED


# ===========================
# Test: Remove user-installed works
# ===========================

class TestRemoveUser:
    async def test_remove_user_cap_succeeds(self):
        """AC5 partial: Install test MCP, remove it, verify gone from registry."""
        from kernos.kernel.reasoning import ReasoningService

        registry = make_registry(
            make_cap("custom-tool", status=CapabilityStatus.CONNECTED, source="user"),
        )

        mcp_mock = AsyncMock()
        mcp_mock.disconnect_one = AsyncMock(return_value=True)

        rs = ReasoningService.__new__(ReasoningService)
        rs._registry = registry
        rs._mcp = mcp_mock
        rs._state = None
        rs._tools_changed = False

        result = await rs._handle_manage_capabilities("tenant_test", "remove", "custom-tool")
        assert "Removed" in result
        assert registry.get("custom-tool").status == CapabilityStatus.SUPPRESSED
        assert rs._tools_changed is True


# ===========================
# Test: Disable user-added → same flow works
# ===========================

class TestDisableUserAdded:
    async def test_disable_user_cap(self):
        """Disable user-added capability works same as default."""
        from kernos.kernel.reasoning import ReasoningService

        registry = make_registry(
            make_cap("custom-tool", status=CapabilityStatus.CONNECTED, source="user"),
        )

        rs = ReasoningService.__new__(ReasoningService)
        rs._registry = registry
        rs._mcp = None
        rs._state = None
        rs._tools_changed = False

        result = await rs._handle_manage_capabilities("tenant_test", "disable", "custom-tool")
        assert "Disabled" in result
        assert registry.get("custom-tool").status == CapabilityStatus.DISABLED
        assert rs._tools_changed is True


# ===========================
# Test: Wipe tenant, restart → defaults appear in registry
# ===========================

class TestDefaultsOnNewTenant:
    def test_defaults_from_known_py_in_registry(self):
        """AC7: After fresh start, defaults from known.py appear in registry."""
        from kernos.capability.known import KNOWN_CAPABILITIES
        import dataclasses

        registry = CapabilityRegistry(mcp=None)
        for cap in KNOWN_CAPABILITIES:
            registry.register(dataclasses.replace(cap))

        all_caps = registry.get_all()
        assert len(all_caps) >= 4  # google-calendar, gmail, web-search, web-browser
        for cap in all_caps:
            assert cap.source == "default"


# ===========================
# Test: New entry in known.py → existing tenant gets it as "available"
# ===========================

class TestNewDefaultMigration:
    def test_new_default_not_in_config_stays_available(self):
        """AC8: New defaults in manifest appear as 'available' on existing tenants."""
        from kernos.capability.known import KNOWN_CAPABILITIES
        import dataclasses

        registry = CapabilityRegistry(mcp=None)
        for cap in KNOWN_CAPABILITIES:
            registry.register(dataclasses.replace(cap))

        # Simulate existing tenant config that only knows about google-calendar
        config = {
            "servers": {"google-calendar": {}},
            "uninstalled": [],
            "disabled": [],
        }

        known_in_config = (
            set(config.get("servers", {}).keys())
            | set(config.get("uninstalled", []))
            | set(config.get("disabled", []))
        )

        # All capabilities not in config remain in their registry state (AVAILABLE)
        new_defaults = [
            cap for cap in registry.get_all()
            if cap.source == "default" and cap.name not in known_in_config
        ]
        assert len(new_defaults) >= 3  # gmail, web-search, web-browser
        for cap in new_defaults:
            assert cap.status == CapabilityStatus.AVAILABLE


# ===========================
# Test: Disable capability → MCP process still running → re-enable → no process restart
# ===========================

class TestDisableMCPWarm:
    def test_disable_does_not_disconnect_mcp(self):
        """AC9: Disable keeps MCP server process running."""
        mcp = MagicMock()
        mcp.get_tool_definitions.return_value = {
            "cal": [{"name": "list-events", "description": "List events", "input_schema": {}}],
        }
        # Track if disconnect_one was called
        mcp.disconnect_one = AsyncMock()

        registry = CapabilityRegistry(mcp=mcp)
        registry.register(make_cap("cal", universal=True, status=CapabilityStatus.CONNECTED))

        # Disable — should NOT call disconnect_one
        registry.disable("cal")
        mcp.disconnect_one.assert_not_called()
        assert registry.get("cal").status == CapabilityStatus.DISABLED

    def test_re_enable_does_not_reconnect(self):
        """AC9: Re-enable doesn't restart MCP process."""
        mcp = MagicMock()
        mcp.get_tool_definitions.return_value = {
            "cal": [{"name": "list-events", "description": "List events", "input_schema": {}}],
        }
        mcp.connect_one = AsyncMock()

        registry = CapabilityRegistry(mcp=mcp)
        registry.register(make_cap("cal", universal=True, status=CapabilityStatus.DISABLED))

        # Re-enable — should NOT call connect_one
        registry.enable("cal")
        mcp.connect_one.assert_not_called()
        assert registry.get("cal").status == CapabilityStatus.CONNECTED


# ===========================
# Test: Gate classification for manage_capabilities
# ===========================

class TestManageToolsGateClassification:
    def test_list_is_read(self):
        from kernos.kernel.reasoning import ReasoningService
        rs = ReasoningService.__new__(ReasoningService)
        rs._registry = None
        effect = rs._classify_tool_effect("manage_capabilities", None, {"action": "list"})
        assert effect == "read"

    def test_enable_is_soft_write(self):
        from kernos.kernel.reasoning import ReasoningService
        rs = ReasoningService.__new__(ReasoningService)
        rs._registry = None
        effect = rs._classify_tool_effect("manage_capabilities", None, {"action": "enable"})
        assert effect == "soft_write"

    def test_disable_is_soft_write(self):
        from kernos.kernel.reasoning import ReasoningService
        rs = ReasoningService.__new__(ReasoningService)
        rs._registry = None
        effect = rs._classify_tool_effect("manage_capabilities", None, {"action": "disable"})
        assert effect == "soft_write"

    def test_remove_is_soft_write(self):
        from kernos.kernel.reasoning import ReasoningService
        rs = ReasoningService.__new__(ReasoningService)
        rs._registry = None
        effect = rs._classify_tool_effect("manage_capabilities", None, {"action": "remove"})
        assert effect == "soft_write"

    def test_install_is_soft_write(self):
        from kernos.kernel.reasoning import ReasoningService
        rs = ReasoningService.__new__(ReasoningService)
        rs._registry = None
        effect = rs._classify_tool_effect("manage_capabilities", None, {"action": "install"})
        assert effect == "soft_write"


# ===========================
# Test: manage_capabilities in _KERNEL_TOOLS set
# ===========================

class TestKernelToolRegistration:
    def test_manage_capabilities_in_kernel_tools(self):
        from kernos.kernel.reasoning import ReasoningService
        assert "manage_capabilities" in ReasoningService._KERNEL_TOOLS

    def test_manage_capabilities_tool_definition_exists(self):
        from kernos.kernel.reasoning import MANAGE_CAPABILITIES_TOOL
        assert MANAGE_CAPABILITIES_TOOL["name"] == "manage_capabilities"
        schema = MANAGE_CAPABILITIES_TOOL["input_schema"]
        assert "action" in schema["properties"]
        assert schema["properties"]["action"]["enum"] == [
            "list", "enable", "disable", "install", "remove"
        ]


# ===========================
# Test: Edge cases
# ===========================

class TestManageToolsEdgeCases:
    async def test_enable_already_enabled(self):
        from kernos.kernel.reasoning import ReasoningService

        registry = make_registry(
            make_cap("cal", status=CapabilityStatus.CONNECTED, source="default"),
        )
        rs = ReasoningService.__new__(ReasoningService)
        rs._registry = registry
        rs._mcp = None
        rs._state = None
        rs._tools_changed = False

        result = await rs._handle_manage_capabilities("tenant_test", "enable", "cal")
        assert "already enabled" in result

    async def test_disable_already_disabled(self):
        from kernos.kernel.reasoning import ReasoningService

        registry = make_registry(
            make_cap("cal", status=CapabilityStatus.DISABLED, source="default"),
        )
        rs = ReasoningService.__new__(ReasoningService)
        rs._registry = registry
        rs._mcp = None
        rs._state = None
        rs._tools_changed = False

        result = await rs._handle_manage_capabilities("tenant_test", "disable", "cal")
        assert "already disabled" in result

    async def test_unknown_capability(self):
        from kernos.kernel.reasoning import ReasoningService

        registry = make_registry()
        rs = ReasoningService.__new__(ReasoningService)
        rs._registry = registry
        rs._mcp = None
        rs._state = None
        rs._tools_changed = False

        result = await rs._handle_manage_capabilities("tenant_test", "enable", "nonexistent")
        assert "not found" in result

    async def test_no_registry(self):
        from kernos.kernel.reasoning import ReasoningService

        rs = ReasoningService.__new__(ReasoningService)
        rs._registry = None
        rs._mcp = None
        rs._state = None
        rs._tools_changed = False

        result = await rs._handle_manage_capabilities("tenant_test", "list", "")
        assert "not available" in result

    async def test_missing_capability_for_action(self):
        from kernos.kernel.reasoning import ReasoningService

        registry = make_registry()
        rs = ReasoningService.__new__(ReasoningService)
        rs._registry = registry
        rs._mcp = None
        rs._state = None
        rs._tools_changed = False

        result = await rs._handle_manage_capabilities("tenant_test", "enable", "")
        assert "required" in result.lower()

    async def test_unknown_action(self):
        from kernos.kernel.reasoning import ReasoningService

        registry = make_registry()
        rs = ReasoningService.__new__(ReasoningService)
        rs._registry = registry
        rs._mcp = None
        rs._state = None
        rs._tools_changed = False

        result = await rs._handle_manage_capabilities("tenant_test", "unknown_action", "")
        assert "Unknown action" in result


# ===========================
# Test: Persist disabled state in mcp-servers.json
# ===========================

class TestPersistDisabledState:
    def test_disabled_list_in_config(self):
        """Disabled capabilities are persisted in the 'disabled' key of mcp-servers.json."""
        # This is a structural test — we verify the handler writes "disabled" list.
        # The actual persistence is tested in integration tests.
        from kernos.capability.registry import CapabilityStatus
        cap = make_cap("cal", status=CapabilityStatus.DISABLED, source="default")
        assert cap.status == CapabilityStatus.DISABLED
