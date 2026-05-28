from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import ProjectState
from kernos.kernel.state_sqlite import SqliteStateStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
async def store(tmp_path):
    s = SqliteStateStore(str(tmp_path))
    yield s
    await s.close_all()


async def _space(store: SqliteStateStore, instance_id: str, space_id: str) -> None:
    now = _now()
    await store.save_context_space(
        ContextSpace(
            id=space_id,
            instance_id=instance_id,
            name="Kitchen remodel",
            created_at=now,
            last_active_at=now,
        )
    )


def _project(project_id: str, space_id: str, *, updated_at: str) -> ProjectState:
    return ProjectState(
        project_id=project_id,
        instance_id="inst_project",
        owner_member_id="mem_a",
        space_id=space_id,
        canvas_id=f"canvas_{project_id}",
        name=project_id.replace("_", " ").title(),
        created_at=updated_at,
        updated_at=updated_at,
        last_activity_at=updated_at,
        data={"source": "test"},
    )


async def test_project_state_crud_and_fetch_by_space(store):
    await _space(store, "inst_project", "space_a")
    project = _project("project_a", "space_a", updated_at="2026-05-01T00:00:00+00:00")

    await store.insert_project_state(project)

    by_id = await store.get_project_state("inst_project", "project_a")
    by_space = await store.get_project_state_by_space("inst_project", "space_a")

    assert by_id is not None
    assert by_id.project_id == "project_a"
    assert by_id.data == {"source": "test"}
    assert by_space is not None
    assert by_space.project_id == "project_a"


async def test_list_active_sorted_by_updated_at_desc(store):
    await _space(store, "inst_project", "space_old")
    await _space(store, "inst_project", "space_new")
    await store.insert_project_state(
        _project("project_old", "space_old", updated_at="2026-05-01T00:00:00+00:00")
    )
    await store.insert_project_state(
        _project("project_new", "space_new", updated_at="2026-05-03T00:00:00+00:00")
    )

    projects = await store.list_active_projects("inst_project")

    assert [p.project_id for p in projects] == ["project_new", "project_old"]


async def test_mark_completed_and_update_activity_fields(store):
    await _space(store, "inst_project", "space_a")
    await store.insert_project_state(
        _project("project_a", "space_a", updated_at="2026-05-01T00:00:00+00:00")
    )

    updated = await store.update_project_activity(
        "inst_project",
        "project_a",
        last_activity_at="2026-05-02T00:00:00+00:00",
        checkin_trigger_id="trig_abc",
        next_checkin_at="2026-05-08T09:00:00",
    )
    completed = await store.mark_project_completed(
        "inst_project",
        "project_a",
        completion_summary="Done.",
        completed_at="2026-05-04T00:00:00+00:00",
    )
    active = await store.list_active_projects("inst_project")

    assert updated is not None
    assert updated.checkin_trigger_id == "trig_abc"
    assert updated.next_checkin_at == "2026-05-08T09:00:00"
    assert completed is not None
    assert completed.lifecycle_state == "completed"
    assert completed.completed_at == "2026-05-04T00:00:00+00:00"
    assert completed.completion_summary == "Done."
    assert active == []


async def test_insert_rejects_orphan_space_id(store):
    project = _project(
        "project_orphan",
        "space_missing",
        updated_at="2026-05-01T00:00:00+00:00",
    )

    with pytest.raises(ValueError, match="space_id"):
        await store.insert_project_state(project)

