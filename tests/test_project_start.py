from __future__ import annotations

import json

import pytest

from kernos.kernel.canvas import CanvasService
from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.projects import start_project
from kernos.kernel.state import InstanceProfile
from kernos.kernel.state_sqlite import SqliteStateStore


class _Reasoning:
    async def complete_simple(self, **kwargs):
        return "{}"


@pytest.fixture
async def project_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    state = SqliteStateStore(str(tmp_path))
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member("mem_a", "Alice", "owner", "")
    canvas = CanvasService(instance_db=idb, data_dir=str(tmp_path))
    yield state, idb, canvas
    await state.close_all()
    await idb.close()


async def test_start_project_creates_space_canvas_pages_state_and_reminder(
    project_env,
    monkeypatch,
):
    state, idb, canvas = project_env
    calls = []
    await state.save_instance_profile(
        "inst_project",
        InstanceProfile(
            instance_id="inst_project",
            status="active",
            created_at="2026-05-01T00:00:00+00:00",
            last_active_space_id="space_general",
        ),
    )

    async def fake_manage_schedule(
        trigger_store,
        instance_id,
        member_id,
        space_id,
        action,
        **kwargs,
    ):
        calls.append({
            "trigger_store": trigger_store,
            "instance_id": instance_id,
            "member_id": member_id,
            "space_id": space_id,
            "action": action,
            **kwargs,
        })
        return (
            "Scheduled: Every week remind me: time to check in on Kitchen remodel.\n"
            "Next fire: 2026-06-01T09:00:00\n"
            "Type: notify | ID: trig_project"
        )

    monkeypatch.setattr(
        "kernos.kernel.projects.handle_manage_schedule",
        fake_manage_schedule,
    )

    result = await start_project(
        state=state,
        canvas=canvas,
        trigger_store=object(),
        reasoning_service=_Reasoning(),
        instance_id="inst_project",
        member_id="mem_a",
        conversation_id="conv_a",
        name="Kitchen remodel",
        initial_note="Replace counters and lighting.",
    )

    assert result["ok"] is True
    assert result["project_id"].startswith("project_")
    assert result["space_id"].startswith("space_")
    assert result["canvas_id"].startswith("canvas_")
    assert result["checkin_trigger_id"] == "trig_project"
    assert result["next_checkin_at"] == "2026-06-01T09:00:00"
    assert result["reminder_created"] is True

    space = await state.get_context_space("inst_project", result["space_id"])
    row = await state.get_project_state("inst_project", result["project_id"])
    profile = await state.get_instance_profile("inst_project")
    canvas_row = await idb.get_canvas(result["canvas_id"])
    seeded = []
    for page in (
        "overview.md",
        "decisions.md",
        "timeline.md",
        "open-loops.md",
        "next-steps.md",
    ):
        read = await canvas.page_read(
            instance_id="inst_project",
            canvas_id=result["canvas_id"],
            page_slug=page,
        )
        seeded.append(read.ok)

    assert space is not None
    assert space.name == "Kitchen remodel"
    assert row is not None
    assert row.space_id == result["space_id"]
    assert row.canvas_id == result["canvas_id"]
    assert row.checkin_trigger_id == "trig_project"
    assert profile is not None
    assert profile.last_active_space_id == result["space_id"]
    assert canvas_row is not None
    assert result["space_id"] in json.loads(canvas_row["pinned_to_spaces"])
    assert all(seeded)
    assert calls[0]["action"] == "create"
    assert calls[0]["space_id"] == result["space_id"]
    assert "Every Monday at 9:00 AM" in calls[0]["description"]
    assert "remind me to check in on Kitchen remodel" in calls[0]["description"]


async def test_start_project_marks_reminder_not_created_when_schedule_parse_fails(
    project_env,
    monkeypatch,
    caplog,
):
    state, _, canvas = project_env

    async def fake_manage_schedule(*args, **kwargs):
        return "I couldn't determine when to schedule that. Can you be more specific?"

    monkeypatch.setattr(
        "kernos.kernel.projects.handle_manage_schedule",
        fake_manage_schedule,
    )

    caplog.set_level("WARNING")
    result = await start_project(
        state=state,
        canvas=canvas,
        trigger_store=object(),
        reasoning_service=_Reasoning(),
        instance_id="inst_project",
        member_id="mem_a",
        conversation_id="conv_a",
        name="Kitchen remodel",
        initial_note="Replace counters and lighting.",
    )
    row = await state.get_project_state("inst_project", result["project_id"])

    assert result["ok"] is True
    assert result["project_id"].startswith("project_")
    assert result["reminder_created"] is False
    assert result["partial"] is True
    assert result["checkin_trigger_id"] == ""
    assert "trigger ID" in result["reminder_reason"]
    assert row is not None
    assert row.checkin_trigger_id == ""
    assert "PROJECT_CHECKIN_CREATE_FAILED" in caplog.text
