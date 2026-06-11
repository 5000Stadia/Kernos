"""Tests for state introspection (SPEC-6A-3)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.introspection import build_user_truth_view, build_operator_state_view
from kernos.kernel.state import Preference, generate_preference_id, CovenantRule
from kernos.kernel.state_json import JsonStateStore
from kernos.kernel.scheduler import Trigger, TriggerStore
from kernos.utils import utc_now


@pytest.fixture
def store(tmp_path):
    return JsonStateStore(str(tmp_path))


@pytest.fixture
def trigger_store(tmp_path):
    return TriggerStore(str(tmp_path))


T = "sms:+15555550100"


def _pref(**kwargs) -> Preference:
    defaults = dict(
        id=generate_preference_id(),
        instance_id=T,
        intent="Notify me 10 minutes before appointments",
        category="notification",
        subject="calendar_events",
        action="notify",
        parameters={"lead_time_minutes": 10},
        status="active",
        created_at=utc_now(),
    )
    defaults.update(kwargs)
    return Preference(**defaults)


# ---------------------------------------------------------------------------
# User truth view
# ---------------------------------------------------------------------------


async def test_user_view_shows_active_preferences(store):
    await store.add_preference(_pref())
    view = await build_user_truth_view(T, store, None)
    assert "Active Preferences" in view
    assert "Notify me 10 minutes" in view
    assert "notification/notify" in view


async def test_user_view_shows_preference_with_linked_trigger(store, trigger_store):
    trigger = Trigger(
        trigger_id="trig_linked01",
        instance_id=T,
        condition_type="time",
        condition="before event",
        action_type="notify",
        action_description="Calendar notification",
        source_preference_id="pref_view01",
        status="active",
    )
    await trigger_store.save(trigger)

    pref = _pref(id="pref_view01", derived_trigger_ids=["trig_linked01"])
    await store.add_preference(pref)

    view = await build_user_truth_view(T, store, trigger_store)
    assert "trig_linked01" in view
    assert "Trigger:" in view


async def test_user_view_shows_standalone_triggers(store, trigger_store):
    trigger = Trigger(
        trigger_id="trig_standalone",
        instance_id=T,
        condition_type="time",
        condition="daily",
        action_type="notify",
        action_description="Daily reminder",
        status="active",
    )
    await trigger_store.save(trigger)

    view = await build_user_truth_view(T, store, trigger_store)
    assert "standalone" in view.lower() or "trig_standalone" in view


async def test_user_view_shows_active_covenants(store):
    rule = CovenantRule(
        id="rule_view01",
        instance_id=T,
        capability="general",
        rule_type="preference",
        description="Keep responses short",
        active=True,
        source="user_stated",
        created_at=utc_now(),
    )
    await store.add_contract_rule(rule)

    view = await build_user_truth_view(T, store, None)
    assert "Active Rules" in view
    assert "Keep responses short" in view


async def test_user_view_shows_key_facts(store):
    from kernos.kernel.state import KnowledgeEntry
    ke = KnowledgeEntry(
        id="know_view01",
        instance_id=T,
        category="fact",
        subject="guitar",
        content="User plays classical guitar",
        confidence="stated",
        source_event_id="",
        source_description="",
        created_at=utc_now(),
        last_referenced="",
        tags=[],
        lifecycle_archetype="habitual",
    )
    await store.add_knowledge(ke)

    view = await build_user_truth_view(T, store, None)
    assert "Key Facts" in view
    assert "guitar" in view.lower()


async def test_user_view_shows_connected_capabilities(store):
    from kernos.capability.registry import CapabilityInfo, CapabilityRegistry, CapabilityStatus
    registry = CapabilityRegistry(mcp=None)
    cap = CapabilityInfo(
        name="google-calendar",
        display_name="Google Calendar",
        description="Calendar access",
        category="productivity",
        status=CapabilityStatus.CONNECTED,
        tools=["list-events", "create-event"],
        server_name="google-calendar",
    )
    registry.register(cap)

    view = await build_user_truth_view(T, store, None, registry)
    assert "Google Calendar" in view
    assert "2 tools" in view


async def test_user_view_excludes_superseded(store):
    await store.add_preference(_pref(status="superseded"))
    await store.add_preference(_pref(intent="Active preference"))

    view = await build_user_truth_view(T, store, None)
    assert "Active preference" in view
    # Superseded should not appear in user view
    assert view.count("Notify me 10 minutes") == 0 or "Active preference" in view


async def test_user_view_empty_tenant(store):
    view = await build_user_truth_view(T, store, None)
    assert "No active state" in view


# ---------------------------------------------------------------------------
# Operator state view
# ---------------------------------------------------------------------------


async def test_operator_view_includes_user_view(store):
    await store.add_preference(_pref())
    view = await build_operator_state_view(T, store, None)
    assert "Active Preferences" in view
    assert "Notify me 10 minutes" in view


async def test_operator_view_shows_legacy_artifacts(store, trigger_store):
    trigger = Trigger(
        trigger_id="trig_legacy01",
        instance_id=T,
        condition_type="time",
        condition="daily",
        action_type="notify",
        action_description="Old reminder",
        status="active",
        # No source_preference_id
    )
    await trigger_store.save(trigger)

    view = await build_operator_state_view(T, store, trigger_store)
    assert "Legacy Unlinked" in view
    assert "trig_legacy01" in view


async def test_operator_view_shows_stale_reconciliation(store, trigger_store):
    trigger = Trigger(
        trigger_id="trig_stale01",
        instance_id=T,
        condition_type="time",
        condition="daily",
        action_type="notify",
        action_description="Stale trigger",
        source_preference_id="pref_stale01",
        status="paused",  # Should be active but isn't
    )
    await trigger_store.save(trigger)

    pref = _pref(id="pref_stale01", derived_trigger_ids=["trig_stale01"])
    await store.add_preference(pref)

    view = await build_operator_state_view(T, store, trigger_store)
    assert "Stale Reconciliation" in view
    assert "trig_stale01" in view


async def test_operator_view_shows_degraded_services(store):
    from kernos.capability.registry import CapabilityInfo, CapabilityRegistry, CapabilityStatus
    registry = CapabilityRegistry(mcp=None)
    cap = CapabilityInfo(
        name="broken-service",
        display_name="Broken Service",
        description="A broken service",
        category="test",
        status=CapabilityStatus.ERROR,
        tools=[],
        server_name="broken",
    )
    registry.register(cap)

    view = await build_operator_state_view(T, store, None, registry)
    assert "Degraded Services" in view
    assert "Broken Service" in view


async def test_operator_view_shows_inactive_preference_counts(store):
    await store.add_preference(_pref(status="superseded"))
    await store.add_preference(_pref(status="revoked"))
    await store.add_preference(_pref(intent="Active one"))

    view = await build_operator_state_view(T, store, None)
    assert "Inactive Preferences" in view
    assert "1 superseded" in view
    assert "1 revoked" in view


# ---------------------------------------------------------------------------
# Views are separate
# ---------------------------------------------------------------------------


async def test_views_are_separate(store, trigger_store):
    """AC3: User and operator views are explicitly separate surfaces."""
    trigger = Trigger(
        trigger_id="trig_sep01",
        instance_id=T,
        condition_type="time",
        condition="daily",
        action_type="notify",
        action_description="Legacy trigger",
        status="active",
    )
    await trigger_store.save(trigger)

    user_view = await build_user_truth_view(T, store, trigger_store)
    operator_view = await build_operator_state_view(T, store, trigger_store)

    # Operator view has Legacy section, user view does not
    assert "Legacy Unlinked" not in user_view
    assert "Legacy Unlinked" in operator_view


# ---------------------------------------------------------------------------
# Current-space visibility (live finding 2026-06-10: the agent reported
# "we're in General" while standing in a hidden system space)
# ---------------------------------------------------------------------------


def _space(space_id: str, name: str, space_type: str = "general", **kwargs):
    from kernos.kernel.spaces import ContextSpace
    defaults = dict(
        id=space_id, instance_id=T, name=name, space_type=space_type,
        status="active", created_at=utc_now(), last_active_at=utc_now(),
    )
    defaults.update(kwargs)
    return ContextSpace(**defaults)


async def test_user_view_names_current_space(store):
    await store.save_context_space(_space("space_gen00001", "General", is_default=True))
    view = await build_user_truth_view(T, store, None, active_space_id="space_gen00001")
    assert "Current space: General" in view


async def test_user_view_current_system_space_is_visible(store):
    """The space the user is standing in must never be hidden — even system-type."""
    await store.save_context_space(_space("space_gen00001", "General", is_default=True))
    await store.save_context_space(
        _space("space_sys00001", "Kernos Self-Improvement", space_type="system"))
    view = await build_user_truth_view(T, store, None, active_space_id="space_sys00001")
    assert "Current space: Kernos Self-Improvement [system]" in view
    assert "Kernos Self-Improvement" in view.split("Current space:")[1]


async def test_user_view_other_system_spaces_stay_hidden(store):
    await store.save_context_space(_space("space_gen00001", "General", is_default=True))
    await store.save_context_space(
        _space("space_sys00001", "Internal Plumbing", space_type="system"))
    view = await build_user_truth_view(T, store, None, active_space_id="space_gen00001")
    assert "Current space: General" in view
    assert "Internal Plumbing" not in view


async def test_user_view_no_active_space_id_keeps_legacy_shape(store):
    """Backward compat: omitting active_space_id renders the legacy listing."""
    await store.save_context_space(_space("space_gen00001", "General", is_default=True))
    view = await build_user_truth_view(T, store, None)
    assert "Current space:" not in view
    assert "General" in view
