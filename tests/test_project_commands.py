from __future__ import annotations

import pytest

from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.state_sqlite import SqliteStateStore
from tests.test_handler import _make_handler
from tests.test_handler_model_command import _ctx


@pytest.fixture
async def command_stack(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    handler, _ = _make_handler()
    state = SqliteStateStore(str(tmp_path))
    handler.state = state
    handler.reasoning.set_state(state)
    handler._trigger_store = object()
    handler.reasoning.set_trigger_store(handler._trigger_store)
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member("mem_a", "Alice", "owner", "")
    handler._instance_db = idb
    ctx = _ctx(handler, member_id="mem_a", space_id="space_general")

    calls = []

    async def fake_projects_schedule(
        trigger_store,
        instance_id,
        member_id,
        space_id,
        action,
        **kwargs,
    ):
        calls.append({"module": "projects", "action": action, "space_id": space_id, **kwargs})
        return (
            "Scheduled: Every week remind me.\n"
            "Next fire: 2026-06-01T09:00:00\n"
            "Type: notify | ID: trig_project"
        )

    async def fake_scheduler_schedule(
        trigger_store,
        instance_id,
        member_id,
        space_id,
        action,
        **kwargs,
    ):
        calls.append({"module": "scheduler", "action": action, "space_id": space_id, **kwargs})
        return f"Removed trigger {kwargs.get('trigger_id', '')}."

    monkeypatch.setattr(
        "kernos.kernel.projects.handle_manage_schedule",
        fake_projects_schedule,
    )
    monkeypatch.setattr(
        "kernos.kernel.scheduler.handle_manage_schedule",
        fake_scheduler_schedule,
    )

    yield handler, state, idb, ctx, calls
    await state.close_all()
    await idb.close()


async def test_project_start_status_list_complete_commands(command_stack):
    handler, state, _, ctx, calls = command_stack

    started = await handler._handle_project_command(
        ctx,
        '/project start "Kitchen remodel"',
    )
    projects = await state.list_active_projects(ctx.instance_id)
    project = projects[0]
    status = await handler._handle_project_command(
        ctx,
        f"/project status {project.project_id}",
    )
    listed = await handler._handle_project_command(ctx, "/project list")
    completed = await handler._handle_project_command(
        ctx,
        f'/project complete {project.project_id} "Finished demo"',
    )
    row = await state.get_project_state(ctx.instance_id, project.project_id)

    assert "Started **Kitchen remodel**" in started
    assert project.project_id in started
    assert project.space_id in started
    assert project.canvas_id in started
    assert "**Kitchen remodel** — active" in status
    assert "Canvas:" in status
    assert "**Active Projects**" in listed
    assert "Kitchen remodel" in listed
    assert "Completed **Kitchen remodel**" in completed
    assert row is not None
    assert row.lifecycle_state == "completed"
    assert row.completion_summary == "Finished demo"
    assert any(c["action"] == "create" for c in calls)
    assert any(c["action"] == "remove" and c["trigger_id"] == "trig_project" for c in calls)


async def test_project_status_without_active_project_returns_text(command_stack):
    handler, _, _, ctx, _ = command_stack

    out = await handler._handle_project_command(ctx, "/project status")

    assert isinstance(out, str)
    assert "Project not found" in out

