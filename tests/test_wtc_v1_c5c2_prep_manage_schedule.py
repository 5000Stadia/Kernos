"""WTC v1 C5c-2-prep — manage_schedule → unified-runtime translation.

Pre-staging commit: ships the translation logic + registration
helper without rewiring scheduler.py's handle_manage_schedule
yet. Pattern 05 path remains authoritative until C5c-2 + C7's
atomic flag-flip + strike.

Pins:

* ``schedule_to_descriptor`` produces a parseable workflow
  descriptor for both notify + tool_call action types and both
  time + event condition types.
* Compilation through compile_descriptor_triggers succeeds for
  every shape produced by schedule_to_descriptor.
* register_managed_schedule_workflow persists the workflow row
  via _register_workflow_unbound AND hydrates the runtime with
  each compiled predicate.
* Pre-compile happens BEFORE persist: a malformed
  descriptor.triggers raises and no workflow row is written.
* Metadata helpers correctly identify managed-schedule rows and
  surface their fields for future list/pause/resume use.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kernos.kernel import event_stream
from kernos.kernel.agents.providers import (
    ProviderRegistry as DARProviderRegistry,
)
from kernos.kernel.agents.registry import AgentRegistry
from kernos.kernel.triggers import (
    InternalEventAdapter,
    TriggerEvaluationRuntime,
)
from kernos.kernel.triggers.adapters.manage_schedule import (
    EVENT_TYPE_CALENDAR_OBSERVED,
    MANAGE_SCHEDULE_ACTION_NOTIFY,
    MANAGE_SCHEDULE_ACTION_TOOL_CALL,
    MANAGED_SCHEDULE_METADATA_KEY,
    is_managed_schedule_workflow,
    mint_managed_schedule_workflow_id,
    read_managed_schedule_metadata,
    register_managed_schedule_workflow,
    schedule_to_descriptor,
)
from kernos.kernel.triggers.adapters.crb_compiler import (
    compile_descriptor_triggers,
)
from kernos.kernel.triggers.errors import TriggerError
from kernos.kernel.workflows.agent_inbox import InMemoryAgentInbox
from kernos.kernel.workflows.descriptor_parser import _build_workflow
from kernos.kernel.workflows.trigger_registry import (
    TriggerRegistry,
    _reset_for_tests as _reset_trigger_registry,
)
from kernos.kernel.workflows.workflow_registry import WorkflowRegistry


# ---------------------------------------------------------------------------
# Translation — pure unit tests
# ---------------------------------------------------------------------------


def test_translate_notify_recurring():
    desc = schedule_to_descriptor(
        workflow_id="ms_a",
        instance_id="inst1",
        description="say hi every hour",
        action_type=MANAGE_SCHEDULE_ACTION_NOTIFY,
        action_params={"message": "hi"},
        notify_via="discord",
        condition_type="time",
        recurrence="0 * * * *",
    )
    assert desc["workflow_id"] == "ms_a"
    assert desc["action_sequence"][0]["action_type"] == "notify_user"
    assert desc["action_sequence"][0]["parameters"]["channel"] == "discord"
    assert desc["action_sequence"][0]["parameters"]["message"] == "hi"
    assert desc["triggers"][0]["temporal_relation"]["kind"] == "every"
    assert desc["triggers"][0]["temporal_relation"]["cron_expression"] == "0 * * * *"
    assert desc["metadata"][MANAGED_SCHEDULE_METADATA_KEY] is True


def test_translate_tool_call_recurring():
    desc = schedule_to_descriptor(
        workflow_id="ms_b",
        instance_id="inst1",
        description="check inbox every 5 min",
        action_type=MANAGE_SCHEDULE_ACTION_TOOL_CALL,
        action_params={
            "tool_name": "inbox.check",
            "tool_args": {"limit": 5},
        },
        condition_type="time",
        recurrence="*/5 * * * *",
    )
    a = desc["action_sequence"][0]
    assert a["action_type"] == "call_tool"
    assert a["parameters"]["tool_name"] == "inbox.check"
    assert a["parameters"]["tool_args"] == {"limit": 5}


def test_translate_calendar_event_with_filter():
    desc = schedule_to_descriptor(
        workflow_id="ms_c",
        instance_id="inst1",
        description="remind 15 min before standup",
        action_type=MANAGE_SCHEDULE_ACTION_NOTIFY,
        action_params={"message": "Standup soon"},
        condition_type="event",
        event_filter="standup",
        event_lead_minutes=15,
    )
    trig = desc["triggers"][0]
    assert trig["event_type"] == EVENT_TYPE_CALENDAR_OBSERVED
    assert trig["temporal_relation"]["kind"] == "before"
    assert trig["temporal_relation"]["minutes"] == 15
    selector = trig["event_selector"]
    assert selector["op"] == "AND"
    # Has the contains-filter on payload.summary
    contains_clauses = [
        op for op in selector["operands"] if op.get("op") == "contains"
    ]
    assert len(contains_clauses) == 1
    assert contains_clauses[0]["value"] == "standup"


def test_translate_calendar_event_no_filter():
    desc = schedule_to_descriptor(
        workflow_id="ms_d",
        instance_id="inst1",
        description="any calendar event",
        action_type=MANAGE_SCHEDULE_ACTION_NOTIFY,
        action_params={"message": "soon"},
        condition_type="event",
        event_filter="",
        event_lead_minutes=30,
    )
    selector = desc["triggers"][0]["event_selector"]
    # Without filter: simple eq selector (no AND wrapper).
    assert selector["op"] == "eq"
    assert selector["path"] == "event_type"
    assert selector["value"] == EVENT_TYPE_CALENDAR_OBSERVED


def test_translate_unsupported_action_type_raises():
    with pytest.raises(ValueError, match="action_type"):
        schedule_to_descriptor(
            workflow_id="x", instance_id="i", description="d",
            action_type="bogus",
            condition_type="time", recurrence="* * * * *",
        )


def test_translate_unsupported_condition_type_raises():
    with pytest.raises(ValueError, match="condition_type"):
        schedule_to_descriptor(
            workflow_id="x", instance_id="i", description="d",
            action_type="notify",
            action_params={"message": "x"},
            condition_type="bogus",
        )


def test_translate_one_shot_time_unsupported():
    """One-shot time triggers (specific datetime, no recurrence)
    have no representation in the v1 three-part predicate model.
    The translation MUST raise so the legacy fallback path can
    handle them until a future temporal-kind extension lands."""
    with pytest.raises(ValueError, match="one-shot"):
        schedule_to_descriptor(
            workflow_id="x", instance_id="i", description="d",
            action_type="notify",
            action_params={"message": "x"},
            condition_type="time",
            recurrence="",  # missing
        )


def test_translate_event_zero_lead_minutes_raises():
    with pytest.raises(ValueError, match="event_lead_minutes"):
        schedule_to_descriptor(
            workflow_id="x", instance_id="i", description="d",
            action_type="notify",
            action_params={"message": "x"},
            condition_type="event",
            event_lead_minutes=0,
        )


def test_descriptor_parses_via_build_workflow():
    """The translated descriptor must be consumable by the
    canonical _build_workflow parser without further massage."""
    desc = schedule_to_descriptor(
        workflow_id="ms_e",
        instance_id="inst1",
        description="every hour",
        action_type="notify",
        action_params={"message": "hi"},
        condition_type="time",
        recurrence="0 * * * *",
    )
    wf = _build_workflow(desc)
    assert wf.workflow_id == "ms_e"
    assert wf.instance_id == "inst1"
    assert len(wf.action_sequence) == 1
    assert wf.action_sequence[0].action_type == "notify_user"


def test_descriptor_compiles_via_crb_compiler():
    """The translated descriptor.triggers must compile cleanly via
    the same adapter the CRB Compiler uses (C5a)."""
    desc = schedule_to_descriptor(
        workflow_id="ms_f",
        instance_id="inst1",
        description="every 5 min",
        action_type="notify",
        action_params={"message": "hi"},
        condition_type="time",
        recurrence="*/5 * * * *",
    )
    compiled = compile_descriptor_triggers(
        workflow_id=desc["workflow_id"], descriptor=desc,
    )
    assert len(compiled) == 1
    assert compiled[0].predicate.temporal_relation.kind == "every"


# ---------------------------------------------------------------------------
# Registration — end-to-end through _register_workflow_unbound + runtime
# ---------------------------------------------------------------------------


@pytest.fixture
async def event_stream_started(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path), flush_interval_s=0.05)
    yield
    await event_stream.stop_writer()
    await event_stream._reset_for_tests()


@pytest.fixture
async def stack(tmp_path, event_stream_started):
    dar_pr = DARProviderRegistry()
    dar_pr.register("inmemory", lambda ref: InMemoryAgentInbox())
    agents = AgentRegistry(provider_registry=dar_pr)
    await agents.start(str(tmp_path))
    trig = TriggerRegistry()
    await trig.start(str(tmp_path))
    wfr = WorkflowRegistry()
    await wfr.start(str(tmp_path), trig)
    wfr.wire_agent_registry(agents)
    runtime = TriggerEvaluationRuntime()
    await runtime.start(
        data_dir=str(tmp_path), heartbeat_seconds=1,
    )
    yield {
        "agents": agents, "wfr": wfr, "trig": trig, "runtime": runtime,
        "tmp_path": tmp_path,
    }
    await runtime.stop()
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await agents.stop()


async def test_register_persists_workflow_and_hydrates_runtime(stack):
    desc = schedule_to_descriptor(
        workflow_id=mint_managed_schedule_workflow_id(),
        instance_id="inst1",
        description="hourly check",
        action_type="notify",
        action_params={"message": "ping"},
        condition_type="time",
        recurrence="0 * * * *",
    )
    registered = await register_managed_schedule_workflow(
        workflow_registry=stack["wfr"],
        runtime=stack["runtime"],
        descriptor=desc,
    )
    # Workflow row exists.
    found = await stack["wfr"].get_workflow(registered.workflow_id)
    assert found is not None
    assert found.metadata.get(MANAGED_SCHEDULE_METADATA_KEY) is True
    # Runtime has the predicate registered.
    active = await stack["runtime"].list_active()
    workflow_ids = {r["workflow_id"] for r in active}
    assert registered.workflow_id in workflow_ids


async def test_register_missing_workflow_id_raises(stack):
    desc = schedule_to_descriptor(
        workflow_id="ms_temp",
        instance_id="inst1",
        description="x",
        action_type="notify",
        action_params={"message": "x"},
        condition_type="time",
        recurrence="* * * * *",
    )
    desc.pop("workflow_id")  # simulate caller bug
    with pytest.raises(ValueError, match="workflow_id"):
        await register_managed_schedule_workflow(
            workflow_registry=stack["wfr"],
            runtime=stack["runtime"],
            descriptor=desc,
        )


async def test_register_malformed_trigger_aborts_before_persist(stack):
    """A descriptor with a malformed trigger MUST raise during
    pre-compile; no workflow row should be persisted, no runtime
    registration should occur."""
    desc = schedule_to_descriptor(
        workflow_id="ms_bad",
        instance_id="inst1",
        description="x",
        action_type="notify",
        action_params={"message": "x"},
        condition_type="time",
        recurrence="* * * * *",
    )
    # Corrupt the trigger AFTER schedule_to_descriptor (caller bug
    # or downstream mutation).
    desc["triggers"][0]["temporal_relation"]["kind"] = "bogus"

    with pytest.raises(TriggerError):
        await register_managed_schedule_workflow(
            workflow_registry=stack["wfr"],
            runtime=stack["runtime"],
            descriptor=desc,
        )

    # No workflow row.
    found = await stack["wfr"].get_workflow("ms_bad")
    assert found is None
    # No runtime entry.
    active = await stack["runtime"].list_active()
    workflow_ids = {r["workflow_id"] for r in active}
    assert "ms_bad" not in workflow_ids


async def test_register_calendar_event_descriptor(stack):
    """Round-trip a calendar event-based schedule. Persist + hydrate."""
    desc = schedule_to_descriptor(
        workflow_id=mint_managed_schedule_workflow_id(),
        instance_id="inst1",
        description="standup reminder",
        action_type="notify",
        action_params={"message": "Standup in 15"},
        condition_type="event",
        event_filter="standup",
        event_lead_minutes=15,
    )
    registered = await register_managed_schedule_workflow(
        workflow_registry=stack["wfr"],
        runtime=stack["runtime"],
        descriptor=desc,
    )
    found = await stack["wfr"].get_workflow(registered.workflow_id)
    assert found is not None
    md = read_managed_schedule_metadata(found.metadata)
    assert md["condition_type"] == "event"
    assert md["event_filter"] == "standup"
    assert md["event_lead_minutes"] == 15


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------


def test_is_managed_schedule_workflow_recognizes_marker():
    assert is_managed_schedule_workflow({MANAGED_SCHEDULE_METADATA_KEY: True})
    assert not is_managed_schedule_workflow({})
    assert not is_managed_schedule_workflow(None)
    assert not is_managed_schedule_workflow({"other": "stuff"})


def test_read_managed_schedule_metadata_returns_only_relevant_fields():
    md = {
        MANAGED_SCHEDULE_METADATA_KEY: True,
        "manage_schedule_action_type": "notify",
        "manage_schedule_action_params": {"message": "hi"},
        "condition_type": "time",
        "recurrence": "*/5 * * * *",
        "event_filter": "",
        "event_lead_minutes": 30,
        "member_id": "m1",
        "delivery_class": "stage",
        "notify_via": "discord",
        "space_id": "s1",
        "conversation_id": "c1",
        "unrelated_field": "should-not-leak",
    }
    out = read_managed_schedule_metadata(md)
    assert "unrelated_field" not in out
    assert out["manage_schedule_action_type"] == "notify"
    assert out["recurrence"] == "*/5 * * * *"
    assert out["member_id"] == "m1"


def test_read_managed_schedule_metadata_empty_for_non_managed():
    assert read_managed_schedule_metadata({}) == {}
    assert read_managed_schedule_metadata({"other": "x"}) == {}
    assert read_managed_schedule_metadata(None) == {}
