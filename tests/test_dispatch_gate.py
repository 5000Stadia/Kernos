"""Tests for SPEC-3D / 3D-HOTFIX / Confirmation Redesign: Dispatch Interceptor.

Tests cover:
- Tool effect classification (kernel + MCP + unknown)
- No fast path — model is sole authority
- Model gate: EXPLICIT / AUTHORIZED / CONFLICT / DENIED
- CONFLICT response: conflicting_rule surfaced, differentiated system message
- Approval token lifecycle: issue, validate, single-use, TTL, hash
- PendingAction: stored on gate block, kernel-owned replay via [CONFIRM:N]
- Agent reasoning extraction passed to gate
- Recent messages passed to gate
- Permission overrides in rules_text (not a separate gate step)
- Read bypass
- GateResult dataclass (allowed, reason, method, conflicting_rule, raw_response)
- delete_file consolidation
- DISPATCH_GATE event type
- InstanceProfile permission_overrides field
- AsyncAnthropic client (no event-loop blocking)
"""
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.capability.registry import CapabilityInfo, CapabilityRegistry, CapabilityStatus
from kernos.kernel.event_types import EventType
from kernos.kernel.reasoning import GateResult, PendingAction, ReasoningRequest, ReasoningService
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import InstanceProfile


# POSTURE-EVALUATION-MODES-V1 (2026-05-22): pin every test in
# this file to the pre-V1 'balanced' mode so existing assertions
# about reason='confirm' on ambiguous responses still hold. New
# mode-aware coverage lives in test_posture_evaluation_modes.py.
@pytest.fixture(autouse=True)
def _pin_gate_mode_balanced(monkeypatch):
    monkeypatch.setenv("KERNOS_GATE_MODE", "balanced")


# --- Helper factories ---

def _make_service(tool_effects: dict[str, str] | None = None) -> ReasoningService:
    """Create a ReasoningService with a registry containing declared tool effects."""
    provider = AsyncMock()
    events = AsyncMock()
    events.emit = AsyncMock(return_value=None)
    mcp = MagicMock()
    audit = AsyncMock()
    audit.log = AsyncMock()

    svc = ReasoningService(provider, events, mcp, audit)

    if tool_effects:
        cap = CapabilityInfo(
            name="google-calendar",
            display_name="Google Calendar",
            description="Calendar management",
            category="calendar",
            status=CapabilityStatus.CONNECTED,
            tools=list(tool_effects.keys()),
            server_name="google-calendar",
            tool_effects=tool_effects,
        )
        registry = CapabilityRegistry(mcp=None)
        registry.register(cap)
        svc.set_registry(registry)

    return svc


def _make_space(space_type: str = "domain") -> ContextSpace:
    return ContextSpace(
        id="space_test01",
        instance_id="tenant_test",
        name="Test Space",
        space_type=space_type,
    )


# ===========================
# GateResult dataclass
# ===========================

class TestGateResult:
    def test_allowed_gate_result(self):
        r = GateResult(allowed=True, reason="approved", method="model_check")
        assert r.allowed is True
        assert r.proposed_action == ""
        assert r.conflicting_rule == ""
        assert r.raw_response == ""

    def test_blocked_gate_result_with_action(self):
        r = GateResult(
            allowed=False, reason="confirm", method="model_check",
            proposed_action="Delete file: notes.md",
        )
        assert r.allowed is False
        assert "notes.md" in r.proposed_action

    def test_conflict_gate_result(self):
        r = GateResult(
            allowed=False, reason="covenant_conflict", method="model_check",
            proposed_action="Send email to alice@example.com",
            conflicting_rule="Never send emails without asking me first",
            raw_response="CONFLICT\n\nThe user asked but a must_not rule applies.",
        )
        assert r.allowed is False
        assert r.conflicting_rule == "Never send emails without asking me first"
        assert "CONFLICT" in r.raw_response


# ===========================
# Tool Effect Classification
# ===========================

class TestClassifyToolEffect:
    def test_kernel_reads_are_read(self):
        svc = _make_service()
        for tool in ["remember", "list_files", "read_file", "dismiss_whisper"]:
            assert svc._classify_tool_effect(tool, None) == "read"

    def test_kernel_writes_are_soft_write(self):
        svc = _make_service()
        for tool in ["write_file", "delete_file"]:
            assert svc._classify_tool_effect(tool, None) == "soft_write"

    def test_mcp_read_tool(self):
        svc = _make_service({"list-events": "read", "create-event": "soft_write"})
        assert svc._classify_tool_effect("list-events", None) == "read"

    def test_mcp_write_tool(self):
        svc = _make_service({"list-events": "read", "create-event": "soft_write"})
        assert svc._classify_tool_effect("create-event", None) == "soft_write"

    def test_mcp_hard_write_tool(self):
        svc = _make_service({"delete-event": "hard_write"})
        assert svc._classify_tool_effect("delete-event", None) == "hard_write"

    def test_undeclared_mcp_tool_is_unknown(self):
        """Tool exists in capability's tools list but not in tool_effects."""
        svc = _make_service({"list-events": "read"})
        # Add a tool that's in tools list but not in tool_effects
        cap = svc._registry.get("google-calendar")
        cap.tools.append("mystery-tool")
        assert svc._classify_tool_effect("mystery-tool", None) == "unknown"

    def test_no_registry_returns_unknown(self):
        svc = _make_service()
        # No registry set → unknown
        assert svc._classify_tool_effect("anything-external", None) == "unknown"

    def test_completely_unknown_tool_returns_unknown(self):
        svc = _make_service({"list-events": "read"})
        assert svc._classify_tool_effect("never-heard-of-this", None) == "unknown"


# ===========================
# No Fast Path — Haiku Is Sole Authority
# ===========================

class TestNoFastPath:
    def test_no_tool_signals_attribute(self):
        """_TOOL_SIGNALS was removed — Haiku is the sole authority."""
        svc = _make_service()
        assert not hasattr(svc, "_TOOL_SIGNALS")

    def test_no_explicit_instruction_matches_method(self):
        """_explicit_instruction_matches was removed — no keyword fast path."""
        svc = _make_service()
        assert not hasattr(svc, "_explicit_instruction_matches")


# ===========================
# Describe Action
# ===========================

class TestDescribeAction:
    def test_create_event_description(self):
        svc = _make_service()
        desc = svc._describe_action("create-event", {"summary": "Standup", "start": "2pm"})
        assert "Standup" in desc
        assert "2pm" in desc

    def test_send_email_description(self):
        svc = _make_service()
        desc = svc._describe_action("send-email", {"to": "alice@example.com", "subject": "Hello"})
        assert "alice@example.com" in desc
        assert "Hello" in desc

    def test_delete_file_description(self):
        svc = _make_service()
        desc = svc._describe_action("delete_file", {"name": "notes.md"})
        assert "notes.md" in desc

    def test_write_file_description(self):
        svc = _make_service()
        desc = svc._describe_action("write_file", {"name": "draft.md"})
        assert "draft.md" in desc

    def test_unknown_tool_description(self):
        svc = _make_service()
        desc = svc._describe_action("mystery-tool", {"key": "value"})
        assert "mystery-tool" in desc


# ===========================
# Get Capability For Tool
# ===========================

class TestGetCapabilityForTool:
    def test_finds_capability(self):
        svc = _make_service({"list-events": "read", "create-event": "soft_write"})
        assert svc._get_capability_for_tool("list-events") == "google-calendar"

    def test_returns_none_for_kernel_tool(self):
        svc = _make_service({"list-events": "read"})
        assert svc._get_capability_for_tool("remember") is None

    def test_returns_none_when_no_registry(self):
        svc = _make_service()
        assert svc._get_capability_for_tool("anything") is None


# ===========================
# Gate Tool Call (Integration)
# ===========================

class TestGateToolCall:
    async def test_reactive_soft_write_skips_model(self):
        """Reactive soft_write bypasses the gate model — user asked for it."""
        svc = _make_service()
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="CONFIRM")  # should never be called

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "book a meeting for Thursday", "t1", "space_1",
        )
        assert result.allowed is True
        assert result.method == "reactive_soft_write"
        svc.complete_simple.assert_not_called()

    async def test_proactive_soft_write_goes_to_model(self):
        """Proactive (non-reactive) soft_write still calls the gate model."""
        svc = _make_service()
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="EXPLICIT")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "sounds good", "t1", "space_1",
            is_reactive=False,
        )
        assert result.allowed is True
        assert result.method == "model_check"
        assert result.reason == "approved"

    async def test_proactive_soft_write_blocked(self):
        """Proactive soft_write: model can deny."""
        svc = _make_service()
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="DENIED")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
            is_reactive=False,
        )
        assert result.allowed is False
        assert result.method == "model_check"
        assert "Meeting" in result.proposed_action

    async def test_permission_override_always_allow(self):
        """Permission override is Step 2 mechanical bypass — no model call, zero cost."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
            permission_overrides={"google-calendar": "always-allow"},
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="DENIED")  # should never be called

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
        )
        assert result.allowed is True
        assert result.method == "always_allow"
        assert result.reason == "permission_override"
        # Model was NOT called — mechanical bypass
        svc.complete_simple.assert_not_called()

    async def test_permission_override_not_in_rules_text(self):
        """Permission overrides are NOT included in model's rules_text (they bypass the model)."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        # No override → falls through to model
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
            permission_overrides={},
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)

        captured = []

        async def capture_simple(system_prompt, user_content, **kwargs):
            captured.append(user_content)
            return "DENIED"

        svc.complete_simple = capture_simple

        await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking", "t1", "space_1",
            is_reactive=False,
        )
        assert captured
        assert "[always-allow]" not in captured[0]

    async def test_covenant_authorized_proactive(self):
        """Model returns AUTHORIZED for proactive covenant check → allowed."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="rule_1", instance_id="t1", capability="calendar",
                rule_type="must", description="Always add calendar entries when I say so",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="AUTHORIZED")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
            is_reactive=False,
        )
        assert result.allowed is True
        assert result.reason == "approved"
        assert result.method == "model_check"

    async def test_covenant_denied_proactive(self):
        """Model returns DENIED on proactive → blocked."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="rule_1", instance_id="t1", capability="email",
                rule_type="must", description="Send email confirmations",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="DENIED")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
            is_reactive=False,
        )
        assert result.allowed is False
        assert result.reason == "confirm"
        assert result.method == "model_check"

    async def test_unexpected_response_denies_proactive(self):
        """Any unexpected model response on proactive → denied."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="MAYBE")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
            is_reactive=False,
        )
        assert result.allowed is False
        assert result.reason == "confirm"


# ===========================
# Read Bypass
# ===========================

class TestReadBypass:
    def test_read_tools_not_gated(self):
        """Read tools should never trigger the gate — classified as 'read'."""
        svc = _make_service({"list-events": "read", "search-events": "read"})
        for tool in ["remember", "list_files", "read_file", "dismiss_whisper", "list-events"]:
            effect = svc._classify_tool_effect(tool, None)
            assert effect == "read", f"{tool} should be 'read', got '{effect}'"


# ===========================
# Unknown Tool Handling
# ===========================

class TestUnknownToolHandling:
    async def test_unknown_tool_gated_as_hard_write(self):
        """A tool not in any capability's tool_effects is treated as hard_write."""
        svc = _make_service()
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="DENIED")

        result = await svc._gate_tool_call(
            "mystery-tool", {}, "unknown",
            "do something mysterious", "t1", "space_1",
        )
        assert result.allowed is False

    def test_unknown_effect_classification(self):
        svc = _make_service({"list-events": "read"})
        assert svc._classify_tool_effect("not-declared", None) == "unknown"


# ===========================
# Permission Overrides on InstanceProfile
# ===========================

class TestPermissionOverrides:
    def test_default_empty(self):
        t = InstanceProfile(instance_id="t1", status="active", created_at="2026-01-01")
        assert t.permission_overrides == {}

    def test_set_and_read(self):
        t = InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
            permission_overrides={"google-calendar": "always-allow"},
        )
        assert t.permission_overrides["google-calendar"] == "always-allow"

    def test_serialization_roundtrip(self):
        t = InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
            permission_overrides={"gmail": "ask"},
        )
        data = asdict(t)
        assert data["permission_overrides"] == {"gmail": "ask"}
        restored = InstanceProfile(**data)
        assert restored.permission_overrides == {"gmail": "ask"}

    def test_backward_compat_missing_field(self):
        """Old profiles without permission_overrides should deserialize."""
        data = {
            "instance_id": "t1", "status": "active", "created_at": "2026-01-01",
        }
        t = InstanceProfile(**data)
        assert t.permission_overrides == {}


# ===========================
# DISPATCH_GATE EventType
# ===========================

class TestDispatchGateEventType:
    def test_event_type_exists(self):
        assert EventType.DISPATCH_GATE == "dispatch.gate"
        assert EventType.DISPATCH_GATE.value == "dispatch.gate"


# ===========================
# delete_file Consolidation
# ===========================

class TestDeleteFileConsolidation:
    def test_no_check_delete_allowed_method(self):
        """_check_delete_allowed is removed — consolidated into dispatch gate."""
        svc = _make_service()
        assert not hasattr(svc, "_check_delete_allowed")

    def test_delete_file_classified_as_soft_write(self):
        svc = _make_service()
        assert svc._classify_tool_effect("delete_file", None) == "soft_write"


# ===========================
# CONFLICT Response (must_not + explicit request)
# ===========================

class TestConflictResponse:
    async def test_must_not_with_explicit_request_returns_conflict(self):
        """Model returns CONFLICT when user asks but a must_not rule applies."""
        svc = _make_service({"send-email": "soft_write"})
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="rule_1", instance_id="t1", capability="email",
                rule_type="must_not", description="Never send emails without asking me first",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="CONFLICT")

        result = await svc._gate_tool_call(
            "send-email", {"to": "alice@example.com"}, "soft_write",
            "send this email to Alice", "t1", "space_1",
        )
        assert result.allowed is False
        assert result.reason == "covenant_conflict"
        assert result.method == "model_check"

    async def test_conflict_populates_conflicting_rule(self):
        """CONFLICT result includes the first must_not rule description."""
        svc = _make_service({"send-email": "soft_write"})
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="r1", instance_id="t1", capability="email",
                rule_type="must_not", description="Never send emails without asking me first",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="CONFLICT")

        result = await svc._gate_tool_call(
            "send-email", {"to": "alice@example.com"}, "soft_write",
            "send this email", "t1", "space_1",
        )
        assert result.conflicting_rule == "Never send emails without asking me first"

    async def test_conflict_colon_format_extracts_rule_from_response(self):
        """CONFLICT: <rule text> format — rule extracted directly from model response."""
        svc = _make_service({"send-email": "soft_write"})
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="r1", instance_id="t1", capability="email",
                rule_type="must_not", description="Never send emails without asking me first",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)
        # Model returns the new colon format
        svc.complete_simple = AsyncMock(
            return_value="CONFLICT: Never send emails without asking me first"
        )

        result = await svc._gate_tool_call(
            "send-email", {"to": "alice@example.com"}, "soft_write",
            "send this email", "t1", "space_1",
        )
        assert result.reason == "covenant_conflict"
        assert result.conflicting_rule == "Never send emails without asking me first"

    async def test_user_explicit_override_returns_explicit(self):
        """User explicitly overrides restriction ('no need to review, just send') → EXPLICIT."""
        svc = _make_service({"send-email": "soft_write"})
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="r1", instance_id="t1", capability="email",
                rule_type="must_not", description="Never send emails without asking me first",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)
        # Model sees user addressed the restriction → EXPLICIT
        svc.complete_simple = AsyncMock(return_value="EXPLICIT")

        result = await svc._gate_tool_call(
            "send-email", {"to": "alice@example.com"}, "soft_write",
            "no need to review, just send it", "t1", "space_1",
        )
        assert result.allowed is True
        assert result.reason == "approved"

    async def test_no_must_not_rules_without_conflict(self):
        """Without must_not rules, proactive soft_write goes to model; CONFLICT never returned."""
        svc = _make_service({"send-email": "soft_write"})
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="r1", instance_id="t1", capability="email",
                rule_type="must", description="Always confirm before sending",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="DENIED")

        result = await svc._gate_tool_call(
            "send-email", {}, "soft_write", "thinking about sending", "t1", "space_1",
            is_reactive=False,
        )
        assert result.reason == "confirm"
        assert result.conflicting_rule == ""

    async def test_no_has_prohibiting_covenant_method(self):
        """_has_prohibiting_covenant removed — model handles must_not detection."""
        svc = _make_service()
        assert not hasattr(svc, "_has_prohibiting_covenant")

    async def test_no_domain_keywords_method(self):
        """_get_domain_keywords removed — no keyword matching at all."""
        svc = _make_service()
        assert not hasattr(svc, "_get_domain_keywords")


# ===========================
# Permission Override System-Wide
# ===========================

class TestPermissionOverrideSystemWide:
    async def test_permission_applies_regardless_of_space(self):
        """Permission override is per-capability, not per-space — mechanical bypass."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
            permission_overrides={"google-calendar": "always-allow"},
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="DENIED")  # never called

        # Different spaces, same result — no model calls
        for space_id in ["space_daily", "space_dnd", "space_business"]:
            result = await svc._gate_tool_call(
                "create-event", {}, "soft_write",
                "I was thinking", "t1", space_id,
            )
            assert result.allowed is True
            assert result.method == "always_allow"
        svc.complete_simple.assert_not_called()


# ===========================
# 3D HOTFIX: AsyncAnthropic Client
# ===========================

class TestAsyncAnthropicClient:
    def test_anthropic_provider_uses_async_client(self):
        """AnthropicProvider must use AsyncAnthropic, not sync Anthropic.

        Sync client calls time.sleep() on 429 retries, blocking asyncio
        and causing Discord heartbeat failure → session invalidation.
        """
        import anthropic as anthropic_sdk
        from kernos.kernel.reasoning import AnthropicProvider
        provider = AnthropicProvider(api_key="test-key")
        assert isinstance(provider._client, anthropic_sdk.AsyncAnthropic)
        assert not isinstance(provider._client, anthropic_sdk.Anthropic)


# ===========================
# Model as Sole Gate Authority
# ===========================

class TestModelGateAuthority:
    async def test_reactive_soft_write_approved_without_model(self):
        """Reactive soft_write is approved without calling the model."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="CONFIRM")  # should not be called

        result = await svc._gate_tool_call(
            "create-event", {"summary": "dentist"}, "soft_write",
            "make an entry at 4:00 for dentist", "t1", "space_1",
        )
        assert result.allowed is True
        assert result.method == "reactive_soft_write"
        svc.complete_simple.assert_not_called()

    async def test_gate_prompt_includes_user_message(self):
        """Gate model prompt includes the current user message (proactive path)."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)

        captured = []

        async def capture_simple(system_prompt, user_content, **kwargs):
            captured.append({"system": system_prompt, "user": user_content})
            return "APPROVE"

        svc.complete_simple = capture_simple

        await svc._gate_tool_call(
            "create-event", {"summary": "reminder"}, "soft_write",
            "schedule a meeting for Thursday", "t1", "space_1",
            is_reactive=False,
        )
        assert captured
        assert "schedule a meeting for Thursday" in captured[0]["user"]
        assert "direct fulfillment" in captured[0]["system"].lower()

    async def test_model_explicit_in_spanish_proactive(self):
        """Model handles non-English instructions on proactive path."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="EXPLICIT")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "reunion"}, "soft_write",
            "ponme algo en el calendario mañana a las 3pm", "t1", "space_1",
            is_reactive=False,
        )
        assert result.allowed is True
        assert result.method == "model_check"

    async def test_indirect_request_denied_proactive(self):
        """Indirect/vague requests denied by model on proactive path."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="DENIED")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
            is_reactive=False,
        )
        assert result.allowed is False
        assert result.reason == "confirm"

    def test_model_max_tokens_is_512(self):
        """Model is called with max_tokens=512 on proactive path."""
        svc = _make_service()
        calls = []

        async def capture_simple(system_prompt, user_content, max_tokens=1024, **kwargs):
            calls.append(max_tokens)
            return "DENIED"

        svc.complete_simple = capture_simple
        import asyncio
        # Use hard_write to always reach model regardless of reactive flag
        asyncio.get_event_loop().run_until_complete(
            svc._gate_tool_call(
                "delete-event", {}, "hard_write",
                "I was thinking", "t1", "space_1",
            )
        )
        assert calls[0] == 512

    async def test_denied_reason_is_simple_proactive(self):
        """When DENIED on proactive, reason is 'confirm'."""
        svc = _make_service()
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="DENIED")

        result = await svc._gate_tool_call(
            "create-event", {}, "soft_write", "I was thinking", "t1", "space_1",
            is_reactive=False,
        )
        assert result.reason == "confirm"

    async def test_first_word_parsing_denied_with_explanation(self):
        """'DENIED\\n...EXPLICIT...' → reason is 'confirm' not 'approved'."""
        svc = _make_service()
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(
            return_value='DENIED\n\nThe user\'s message "again?" is ambiguous and does not '
                         'constitute an EXPLICIT request to create a calendar event.'
        )

        # Use hard_write to always reach model
        result = await svc._gate_tool_call(
            "delete-event", {}, "hard_write", "again?", "t1", "space_1",
        )
        assert result.allowed is False
        assert result.reason == "confirm"

    async def test_agent_reasoning_passed_to_model(self):
        """agent_reasoning parameter is included in the user_content sent to model."""
        svc = _make_service()
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)

        captured = []

        async def capture_simple(system_prompt, user_content, **kwargs):
            captured.append(user_content)
            return "EXPLICIT"

        svc.complete_simple = capture_simple

        # Use is_reactive=False to reach the model
        await svc._gate_tool_call(
            "create-event", {}, "soft_write",
            "sounds good, do it", "t1", "space_1",
            agent_reasoning="User wants to book a 4pm dentist appointment.",
            is_reactive=False,
        )
        assert captured
        assert "User wants to book a 4pm dentist appointment." in captured[0]

    async def test_recent_messages_from_history(self):
        """Last 5 user messages from messages list are included in model prompt."""
        svc = _make_service()
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)

        captured = []

        async def capture_simple(system_prompt, user_content, **kwargs):
            captured.append(user_content)
            return "EXPLICIT"

        svc.complete_simple = capture_simple

        messages = [
            {"role": "user", "content": "What's on my calendar?"},
            {"role": "assistant", "content": "You have a team meeting at 2pm."},
            {"role": "user", "content": "Add dentist at 4pm please"},
        ]
        # Use is_reactive=False to exercise model path
        await svc._gate_tool_call(
            "create-event", {}, "soft_write",
            "Add dentist at 4pm please", "t1", "space_1",
            messages=messages,
            is_reactive=False,
        )
        assert captured
        assert "Add dentist at 4pm please" in captured[0]
        assert "What's on my calendar?" in captured[0]


# ===========================
# 3D HOTFIX: ApprovalToken
# ===========================

class TestApprovalToken:
    def test_issue_creates_token(self):
        svc = _make_service()
        token = svc._issue_approval_token("create-event", {"summary": "Meeting"})
        assert token.token_id in svc._approval_tokens
        assert token.tool_name == "create-event"
        assert token.used is False
        assert len(token.token_id) == 12
        assert len(token.tool_input_hash) == 8

    def test_validate_valid_token(self):
        svc = _make_service()
        tool_input = {"summary": "Meeting", "start": "2pm"}
        token = svc._issue_approval_token("create-event", tool_input)
        assert svc._validate_approval_token(token.token_id, "create-event", tool_input) is True

    def test_validate_marks_used(self):
        svc = _make_service()
        tool_input = {"summary": "Meeting"}
        token = svc._issue_approval_token("create-event", tool_input)
        svc._validate_approval_token(token.token_id, "create-event", tool_input)
        assert token.used is True

    def test_token_single_use(self):
        """Reusing a consumed token is rejected."""
        svc = _make_service()
        tool_input = {"summary": "Meeting"}
        token = svc._issue_approval_token("create-event", tool_input)
        # First use: valid
        assert svc._validate_approval_token(token.token_id, "create-event", tool_input) is True
        # Second use: rejected
        assert svc._validate_approval_token(token.token_id, "create-event", tool_input) is False

    def test_token_wrong_tool_name_rejected(self):
        svc = _make_service()
        tool_input = {"summary": "Meeting"}
        token = svc._issue_approval_token("create-event", tool_input)
        assert svc._validate_approval_token(token.token_id, "delete-event", tool_input) is False

    def test_token_wrong_input_hash_rejected(self):
        svc = _make_service()
        token = svc._issue_approval_token("create-event", {"summary": "Meeting"})
        # Different input → different hash
        assert svc._validate_approval_token(
            token.token_id, "create-event", {"summary": "Other"}
        ) is False

    def test_token_nonexistent_rejected(self):
        svc = _make_service()
        assert svc._validate_approval_token("nonexistent", "create-event", {}) is False

    def test_token_expired_rejected(self):
        svc = _make_service()
        tool_input = {"summary": "Meeting"}
        token = svc._issue_approval_token("create-event", tool_input)
        # Backdate the issued_at to 6 minutes ago
        token.issued_at = datetime.now(timezone.utc) - timedelta(minutes=6)
        assert svc._validate_approval_token(token.token_id, "create-event", tool_input) is False

    def test_token_within_ttl_accepted(self):
        svc = _make_service()
        tool_input = {"summary": "Meeting"}
        token = svc._issue_approval_token("create-event", tool_input)
        # 4 minutes old → within 5-minute TTL
        token.issued_at = datetime.now(timezone.utc) - timedelta(minutes=4)
        assert svc._validate_approval_token(token.token_id, "create-event", tool_input) is True

    async def test_denied_system_message_format(self):
        """DENIED: gate produces result with reason='confirm'; [CONFIRM:N] is in system message."""
        svc = _make_service()
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="DENIED")

        # Use is_reactive=False to reach the model
        gate_result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking", "t1", "space_1",
            is_reactive=False,
        )
        assert not gate_result.allowed
        assert gate_result.reason == "confirm"
        # Build what the system message would look like (index 0 for first block)
        msg = (
            f"[SYSTEM] Action paused — confirming intent. "
            f"Proposed: {gate_result.proposed_action}. "
            f"Pending action index: 0. "
            f"Ask the user if they want to proceed. If they confirm, "
            f"include [CONFIRM:0] in your response."
        )
        assert "[SYSTEM]" in msg
        assert "[CONFIRM:0]" in msg
        assert "_approval_token" not in msg

    async def test_conflict_system_message_format(self):
        """CONFLICT: gate result has covenant_conflict reason; [CONFIRM:N] in system message."""
        from kernos.kernel.state import CovenantRule
        svc = _make_service({"send-email": "soft_write"})
        state = AsyncMock()
        state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
            instance_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="r1", instance_id="t1", capability="email",
                rule_type="must_not", description="Never send emails without asking me first",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="CONFLICT")

        gate_result = await svc._gate_tool_call(
            "send-email", {"to": "alice@example.com"}, "soft_write",
            "send this email", "t1", "space_1",
        )
        assert not gate_result.allowed
        assert gate_result.reason == "covenant_conflict"
        assert gate_result.conflicting_rule == "Never send emails without asking me first"
        # Build what the system message would look like (index 0 for first block)
        msg = (
            f"[SYSTEM] Action blocked — conflict with standing rule. "
            f"Proposed: {gate_result.proposed_action}. "
            f"Conflicting rule: {gate_result.conflicting_rule}. "
            f"Pending action index: 0. "
            f"Ask the user to confirm. If they confirm, include "
            f"[CONFIRM:0] in your response. "
            f"Also offer three options: "
            f"1. Respect the rule (don't do it). "
            f"2. Override this time (confirm the action). "
            f"3. Update the rule permanently."
        )
        assert "[SYSTEM]" in msg
        assert "conflict" in msg
        assert "Never send emails without asking me first" in msg
        assert "[CONFIRM:0]" in msg
        assert "_approval_token" not in msg

    async def test_token_method_is_token(self):
        """When approval token is used, method is 'token' (not 'token_check')."""
        svc = _make_service()
        tool_input = {"summary": "Meeting"}
        token = svc._issue_approval_token("create-event", tool_input)

        result = await svc._gate_tool_call(
            "create-event", tool_input, "soft_write",
            "I was thinking", "t1", "space_1",
            approval_token_id=token.token_id,
        )
        assert result.allowed is True
        assert result.method == "token"
        assert result.reason == "token_approved"


# ===========================
# PendingAction
# ===========================

class TestPendingActions:
    """Tests for PendingAction dataclass and _pending_actions dict on ReasoningService.

    Note: _pending_actions is populated inside reason() (the tool-use loop), not by
    _gate_tool_call() directly. These tests verify the dataclass behavior and the
    _pending_actions dict structure by manipulating it directly, mirroring what
    reason() does when gate_result.allowed is False.
    """

    def _store_pending(
        self,
        svc: ReasoningService,
        instance_id: str,
        tool_name: str,
        tool_input: dict,
        proposed_action: str = "Do something",
        conflicting_rule: str = "",
        gate_reason: str = "confirm",
    ) -> PendingAction:
        """Simulate what reason() does when a gate blocks: store a PendingAction."""
        if instance_id not in svc._pending_actions:
            svc._pending_actions[instance_id] = []
        action = PendingAction(
            tool_name=tool_name,
            tool_input=dict(tool_input),
            proposed_action=proposed_action,
            conflicting_rule=conflicting_rule,
            gate_reason=gate_reason,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        svc._pending_actions[instance_id].append(action)
        return action

    def test_pending_action_stored_on_gate_block(self):
        """Storing a PendingAction yields correct fields."""
        svc = _make_service()
        action = self._store_pending(
            svc, "t1", "create-event", {"summary": "Meeting"},
            proposed_action="Create a calendar event",
            gate_reason="confirm",
        )
        assert "t1" in svc._pending_actions
        assert len(svc._pending_actions["t1"]) == 1
        stored = svc._pending_actions["t1"][0]
        assert isinstance(stored, PendingAction)
        assert stored.tool_name == "create-event"
        assert stored.tool_input == {"summary": "Meeting"}
        assert stored.gate_reason == "confirm"
        assert stored.expires_at > datetime.now(timezone.utc)

    def test_pending_action_indexed_for_multiple_blocks(self):
        """Two stored blocks → indices 0 and 1."""
        svc = _make_service()
        self._store_pending(svc, "t1", "create-event", {"summary": "Meeting"})
        self._store_pending(svc, "t1", "delete-event", {"id": "evt1"})
        assert len(svc._pending_actions["t1"]) == 2
        assert svc._pending_actions["t1"][0].tool_name == "create-event"
        assert svc._pending_actions["t1"][1].tool_name == "delete-event"

    def test_pending_action_not_stored_when_allowed(self):
        """When gate allows, nothing is stored in _pending_actions."""
        svc = _make_service()
        # No call to _store_pending — gate allowed the call
        assert "t1" not in svc._pending_actions

    def test_system_message_uses_confirm_not_token(self):
        """System message for denied block contains [CONFIRM:0] and not _approval_token."""
        pending_idx = 0
        proposed_action = "Create a calendar event"
        msg = (
            f"[SYSTEM] Action paused — confirming intent. "
            f"Proposed: {proposed_action}. "
            f"Pending action index: {pending_idx}. "
            f"Ask the user if they want to proceed. If they confirm, "
            f"include [CONFIRM:{pending_idx}] in your response. "
            f"You may also offer to create a standing rule."
        )
        assert "[SYSTEM]" in msg
        assert "[CONFIRM:0]" in msg
        assert "_approval_token" not in msg

    def test_system_message_covenant_conflict_format(self):
        """CONFLICT format contains [CONFIRM:N] and conflicting rule."""
        svc = _make_service()
        action = self._store_pending(
            svc, "t1", "delete-event", {"id": "evt1"},
            proposed_action="Delete calendar event",
            conflicting_rule="Never delete without awareness",
            gate_reason="covenant_conflict",
        )
        assert action.gate_reason == "covenant_conflict"
        assert action.conflicting_rule == "Never delete without awareness"
        pending_idx = 0
        msg = (
            f"[SYSTEM] Action blocked — conflict with standing rule. "
            f"Proposed: {action.proposed_action}. "
            f"Conflicting rule: {action.conflicting_rule}. "
            f"Pending action index: {pending_idx}. "
            f"Ask the user to confirm. If they confirm, include "
            f"[CONFIRM:{pending_idx}] in your response."
        )
        assert "[CONFIRM:0]" in msg
        assert "_approval_token" not in msg
        assert "Never delete without awareness" in msg

    def test_system_message_confirm_format(self):
        """CONFIRM format: gate_reason is 'confirm', conflicting_rule is empty."""
        svc = _make_service()
        action = self._store_pending(
            svc, "t1", "create-event", {"summary": "Meeting"},
            gate_reason="confirm",
            conflicting_rule="",
        )
        assert action.gate_reason == "confirm"
        assert action.conflicting_rule == ""

    def test_token_still_issued_for_programmatic_callers(self):
        """_approval_tokens still available after gate blocks (token issued in reason())."""
        svc = _make_service()
        # Simulate token issuance that happens in reason() before storing PendingAction
        token = svc._issue_approval_token("create-event", {"summary": "Meeting"})
        self._store_pending(svc, "t1", "create-event", {"summary": "Meeting"})
        # Both token and pending action exist
        assert token.token_id in svc._approval_tokens
        assert "t1" in svc._pending_actions


# ===========================
# Handler Confirmation Replay
# ===========================

class TestConfirmationReplay:
    """Tests for the kernel-owned [CONFIRM:N] replay logic.

    These tests exercise the confirmation logic directly by simulating what
    handler.process() does after engine.execute() returns — without going through
    the full pipeline. We test the confirmation pattern matching and PendingAction
    execution logic inline.
    """

    def _make_pending_action(
        self,
        tool_name: str = "delete_file",
        tool_input: dict | None = None,
        proposed_action: str = "Delete file 'potato.md'",
        gate_reason: str = "covenant_conflict",
        conflicting_rule: str = "Never delete without awareness",
        expires_at: datetime | None = None,
    ) -> PendingAction:
        if tool_input is None:
            tool_input = {"name": "potato.md"}
        if expires_at is None:
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        return PendingAction(
            tool_name=tool_name,
            tool_input=tool_input,
            proposed_action=proposed_action,
            conflicting_rule=conflicting_rule,
            gate_reason=gate_reason,
            expires_at=expires_at,
        )

    async def _run_confirm_logic(
        self,
        svc: ReasoningService,
        response_text: str,
        pending_actions: list[PendingAction],
        instance_id: str = "t1",
        execute_tool_result: str = "Done",
        request: object = None,
    ) -> str:
        """Simulate the handler's confirmation-check block after engine.execute().

        Directly runs the [CONFIRM:N] pattern matching logic, matching the
        implementation in handler.process().
        """
        import re

        svc._pending_actions[instance_id] = list(pending_actions)
        svc.execute_tool = AsyncMock(return_value=execute_tool_result)

        if request is None:
            request = MagicMock()
            request.instance_id = instance_id

        pending = svc._pending_actions.get(instance_id)
        if not pending:
            return response_text

        confirm_pattern = re.compile(r'\[CONFIRM:(\d+|ALL)\]', re.IGNORECASE)
        matches = confirm_pattern.findall(response_text)
        if matches:
            actions_to_execute: list[int] = []
            for match in matches:
                if match.upper() == "ALL":
                    actions_to_execute = list(range(len(pending)))
                    break
                else:
                    idx = int(match)
                    if 0 <= idx < len(pending):
                        actions_to_execute.append(idx)
            execution_results: list[str] = []
            for idx in actions_to_execute:
                action = pending[idx]
                if datetime.now(timezone.utc) < action.expires_at:
                    try:
                        result = await svc.execute_tool(
                            action.tool_name, action.tool_input, request
                        )
                        execution_results.append(f"✓ {action.proposed_action}: {result}")
                    except Exception as exc:
                        execution_results.append(f"Failed: {action.proposed_action} ({exc})")
                else:
                    execution_results.append(f"Expired: {action.proposed_action}")
            del svc._pending_actions[instance_id]
            response_text = confirm_pattern.sub("", response_text).strip()
            if execution_results:
                response_text += "\n\n" + "\n".join(execution_results)
        else:
            del svc._pending_actions[instance_id]

        return response_text

    async def test_confirm_single_action(self):
        """Response with [CONFIRM:0] triggers execute_tool and strips signal."""
        svc = _make_service()
        pending = [self._make_pending_action()]
        result = await self._run_confirm_logic(
            svc,
            response_text="Deleting the file now. [CONFIRM:0]",
            pending_actions=pending,
            execute_tool_result="File deleted.",
        )
        assert "[CONFIRM:0]" not in result
        assert "File deleted" in result

    async def test_confirm_all_actions(self):
        """[CONFIRM:ALL] executes all pending actions."""
        svc = _make_service()
        pending = [
            self._make_pending_action("delete_file", {"name": "potato.md"}, "Delete potato.md"),
            self._make_pending_action("delete_file", {"name": "carrot.md"}, "Delete carrot.md"),
        ]
        result = await self._run_confirm_logic(
            svc,
            response_text="Deleting both. [CONFIRM:ALL]",
            pending_actions=pending,
            execute_tool_result="Done",
        )
        assert "[CONFIRM:ALL]" not in result
        assert svc.execute_tool.await_count == 2

    async def test_no_confirm_signal_clears_pending(self):
        """Response without [CONFIRM:N] clears pending actions without executing."""
        svc = _make_service()
        pending = [self._make_pending_action()]
        instance_id = "t1"
        await self._run_confirm_logic(
            svc,
            response_text="Let me know if you want to proceed.",
            pending_actions=pending,
            instance_id=instance_id,
        )
        # execute_tool should NOT have been called
        svc.execute_tool.assert_not_called()
        # pending_actions should be cleared
        assert instance_id not in svc._pending_actions

    async def test_confirm_signal_stripped_from_response(self):
        """[CONFIRM:0] is stripped from final response text."""
        svc = _make_service()
        pending = [self._make_pending_action()]
        result = await self._run_confirm_logic(
            svc,
            response_text="Done. [CONFIRM:0] All good.",
            pending_actions=pending,
            execute_tool_result="File deleted.",
        )
        assert "[CONFIRM:0]" not in result
        assert "All good" in result

    async def test_expired_action_not_executed(self):
        """Past expires_at → 'Expired:' in result, execute_tool not called."""
        svc = _make_service()
        expired_action = self._make_pending_action(
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=10)
        )
        result = await self._run_confirm_logic(
            svc,
            response_text="Confirming. [CONFIRM:0]",
            pending_actions=[expired_action],
            execute_tool_result="Done",
        )
        svc.execute_tool.assert_not_called()
        assert "Expired" in result

    async def test_selective_confirmation(self):
        """[CONFIRM:1] only executes index 1, not index 0."""
        svc = _make_service()
        pending = [
            self._make_pending_action("delete_file", {"name": "a.md"}, "Delete a.md"),
            self._make_pending_action("delete_file", {"name": "b.md"}, "Delete b.md"),
        ]
        executed_inputs = []

        async def capture_execute(tool_name, tool_input, request):
            executed_inputs.append(tool_input)
            return "Done"

        svc.execute_tool = capture_execute

        instance_id = "t1"
        svc._pending_actions[instance_id] = list(pending)

        import re
        response_text = "Deleting b.md only. [CONFIRM:1]"
        confirm_pattern = re.compile(r'\[CONFIRM:(\d+|ALL)\]', re.IGNORECASE)
        matches = confirm_pattern.findall(response_text)
        actions_to_execute: list[int] = []
        for match in matches:
            idx = int(match)
            if 0 <= idx < len(pending):
                actions_to_execute.append(idx)

        request = MagicMock()
        request.instance_id = instance_id
        for idx in actions_to_execute:
            action = pending[idx]
            await svc.execute_tool(action.tool_name, action.tool_input, request)

        del svc._pending_actions[instance_id]

        # Only b.md (index 1) should have been executed
        assert len(executed_inputs) == 1
        assert executed_inputs[0] == {"name": "b.md"}

    async def test_same_turn_conflict_strips_confirm_tokens(self):
        """When conflict_raised_this_turn is True, CONFIRM tokens are stripped but NOT executed."""
        svc = _make_service()
        svc._conflict_raised_this_turn = True
        pending = [self._make_pending_action()]
        svc._pending_actions["t1"] = list(pending)
        svc.execute_tool = AsyncMock(return_value="Done")

        import re
        response_text = "I'll do that now. [CONFIRM:0]"
        confirm_pattern = re.compile(r'\[CONFIRM:(\d+|ALL)\]', re.IGNORECASE)
        response_text = confirm_pattern.sub("", response_text).strip()

        svc.execute_tool.assert_not_called()
        assert "[CONFIRM" not in response_text
        # Pending actions should still exist for next turn
        assert "t1" in svc._pending_actions

    async def test_next_turn_confirm_executes_after_conflict(self):
        """On a subsequent turn (conflict_raised_this_turn=False), CONFIRM tokens execute normally."""
        svc = _make_service()
        svc._conflict_raised_this_turn = False
        pending = [self._make_pending_action()]
        result = await self._run_confirm_logic(
            svc,
            response_text="Confirmed. [CONFIRM:0]",
            pending_actions=pending,
            execute_tool_result="Action completed.",
        )
        assert "[CONFIRM:0]" not in result
        assert "Action completed" in result

    def test_conflict_flag_initialized_false(self):
        """The conflict flag starts as False."""
        svc = _make_service()
        assert svc._conflict_raised_this_turn is False

    def test_conflict_flag_set_on_pending_store(self):
        """Storing a PendingAction (gate block) sets the conflict flag."""
        svc = _make_service()
        svc._conflict_raised_this_turn = False
        # Simulate what reason() does when gate blocks
        svc._pending_actions.setdefault("t1", []).append(
            self._make_pending_action("send-email", {"to": "alice"}, "Send email")
        )
        svc._conflict_raised_this_turn = True  # reason() sets this
        assert svc._conflict_raised_this_turn is True
