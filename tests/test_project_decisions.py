from __future__ import annotations

import pytest

from kernos.kernel.canvas import CanvasService
from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.projects import (
    record_project_decision,
    start_project,
    surface_project_status,
)
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import KnowledgeEntry
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

    async def fake_manage_schedule(*args, **kwargs):
        return (
            "Scheduled: Every week remind me.\n"
            "Next fire: 2026-06-01T09:00:00\n"
            "Type: notify | ID: trig_project"
        )

    monkeypatch.setattr(
        "kernos.kernel.projects.handle_manage_schedule",
        fake_manage_schedule,
    )
    yield state, idb, canvas
    await state.close_all()
    await idb.close()


async def _start(state, canvas, name: str):
    return await start_project(
        state=state,
        canvas=canvas,
        trigger_store=object(),
        reasoning_service=_Reasoning(),
        instance_id="inst_project",
        member_id="mem_a",
        name=name,
    )


async def test_record_project_decision_writes_canvas_and_knowledge(project_env):
    state, _, canvas = project_env
    started = await _start(state, canvas, "Kitchen remodel")

    result = await record_project_decision(
        state=state,
        canvas=canvas,
        instance_id="inst_project",
        member_id="mem_a",
        active_space_id=started["space_id"],
        decision="Use quartz counters.",
        subject="Counters",
    )

    decisions = await canvas.page_read(
        instance_id="inst_project",
        canvas_id=started["canvas_id"],
        page_slug="decisions.md",
    )
    timeline = await canvas.page_read(
        instance_id="inst_project",
        canvas_id=started["canvas_id"],
        page_slug="timeline.md",
    )
    entries = await state.query_knowledge(
        "inst_project",
        category="project_decision",
        limit=10,
    )

    assert result["ok"] is True
    assert result["project_id"] == started["project_id"]
    assert "Use quartz counters." in decisions.extra["body"]
    assert "Decision - Counters" in timeline.extra["body"]
    assert len(entries) == 1
    assert entries[0].category == "project_decision"
    assert entries[0].content == "Use quartz counters."
    assert f"project:{started['project_id']}" in entries[0].tags
    assert f"space:{started['space_id']}" in entries[0].tags
    assert entries[0].context_space == started["space_id"]


async def test_omitted_project_id_resolves_after_start_active_space_switch(project_env):
    state, _, canvas = project_env
    started = await _start(state, canvas, "Kitchen remodel")
    profile = await state.get_instance_profile("inst_project")

    assert profile is not None
    assert profile.last_active_space_id == started["space_id"]

    decision = await record_project_decision(
        state=state,
        canvas=canvas,
        instance_id="inst_project",
        member_id="mem_a",
        active_space_id=profile.last_active_space_id,
        decision="Use quartz counters.",
        subject="Counters",
    )
    status = await surface_project_status(
        state=state,
        canvas=canvas,
        instance_id="inst_project",
        member_id="mem_a",
        active_space_id=profile.last_active_space_id,
    )

    assert decision["ok"] is True
    assert decision["project_id"] == started["project_id"]
    assert status["ok"] is True
    assert status["project_id"] == started["project_id"]
    assert [d["content"] for d in status["recent_decisions"]] == [
        "Use quartz counters."
    ]


async def test_omitted_project_id_falls_back_to_recent_active_project(project_env):
    state, _, canvas = project_env
    started = await _start(state, canvas, "Kitchen remodel")
    await state.save_context_space(
        ContextSpace(
            id="space_elsewhere",
            instance_id="inst_project",
            member_id="mem_a",
            name="Elsewhere",
            created_at="2026-05-01T00:00:00+00:00",
            last_active_at="2026-05-01T00:00:00+00:00",
        )
    )

    decision = await record_project_decision(
        state=state,
        canvas=canvas,
        instance_id="inst_project",
        member_id="mem_a",
        active_space_id="space_elsewhere",
        decision="Use walnut shelves.",
        subject="Shelving",
    )
    status = await surface_project_status(
        state=state,
        canvas=canvas,
        instance_id="inst_project",
        member_id="mem_a",
        active_space_id="space_elsewhere",
    )

    assert decision["ok"] is True
    assert decision["project_id"] == started["project_id"]
    assert status["ok"] is True
    assert status["project_id"] == started["project_id"]
    assert [d["content"] for d in status["recent_decisions"]] == [
        "Use walnut shelves."
    ]


async def test_surface_status_filters_recent_decisions_by_project(project_env):
    state, _, canvas = project_env
    kitchen = await _start(state, canvas, "Kitchen remodel")
    wedding = await _start(state, canvas, "Wedding")
    await record_project_decision(
        state=state,
        canvas=canvas,
        instance_id="inst_project",
        member_id="mem_a",
        project_id=kitchen["project_id"],
        decision="Use quartz counters.",
        subject="Counters",
    )
    await record_project_decision(
        state=state,
        canvas=canvas,
        instance_id="inst_project",
        member_id="mem_a",
        project_id=wedding["project_id"],
        decision="Hold ceremony outdoors.",
        subject="Venue",
    )

    status = await surface_project_status(
        state=state,
        canvas=canvas,
        instance_id="inst_project",
        member_id="mem_a",
        active_space_id=kitchen["space_id"],
    )

    assert status["ok"] is True
    assert status["project_id"] == kitchen["project_id"]
    assert [d["content"] for d in status["recent_decisions"]] == [
        "Use quartz counters."
    ]
    assert any("Project started" in item for item in status["timeline"])
    assert status["open_loops"]
    assert status["next_steps"]


async def test_surface_status_finds_project_decisions_beyond_global_first_page(
    project_env,
):
    state, _, canvas = project_env
    kitchen = await _start(state, canvas, "Kitchen remodel")
    wedding = await _start(state, canvas, "Wedding")
    move = await _start(state, canvas, "Move")

    projects = [wedding, move]
    for i in range(60):
        project = projects[i % len(projects)]
        await state.add_knowledge(
            KnowledgeEntry(
                id=f"know_other_{i}",
                instance_id="inst_project",
                category="project_decision",
                subject=f"Other {i}",
                content=f"Other project decision {i}",
                confidence="stated",
                source_event_id="",
                source_description="test",
                created_at=f"2026-05-01T00:{i:02d}:00+00:00",
                last_referenced=f"2026-05-01T00:{i:02d}:00+00:00",
                tags=[
                    f"project:{project['project_id']}",
                    f"space:{project['space_id']}",
                    "project_decision",
                ],
                context_space=project["space_id"],
                owner_member_id="mem_a",
            )
        )
    await state.add_knowledge(
        KnowledgeEntry(
            id="know_target",
            instance_id="inst_project",
            category="project_decision",
            subject="Counters",
            content="Target project decision survives scoped status.",
            confidence="stated",
            source_event_id="",
            source_description="test",
            created_at="2026-05-01T01:00:00+00:00",
            last_referenced="2026-05-01T01:00:00+00:00",
            tags=[
                f"project:{kitchen['project_id']}",
                f"space:{kitchen['space_id']}",
                "project_decision",
            ],
            context_space=kitchen["space_id"],
            owner_member_id="mem_a",
        )
    )

    status = await surface_project_status(
        state=state,
        canvas=canvas,
        instance_id="inst_project",
        member_id="mem_a",
        active_space_id=kitchen["space_id"],
    )

    assert status["ok"] is True
    assert status["project_id"] == kitchen["project_id"]
    assert [d["content"] for d in status["recent_decisions"]] == [
        "Target project decision survives scoped status."
    ]


async def test_decision_reports_partial_failure_when_knowledge_write_fails(
    project_env,
    monkeypatch,
):
    state, _, canvas = project_env
    started = await _start(state, canvas, "Kitchen remodel")

    async def fail_add_knowledge(entry):
        raise RuntimeError("knowledge down")

    monkeypatch.setattr(state, "add_knowledge", fail_add_knowledge)

    result = await record_project_decision(
        state=state,
        canvas=canvas,
        instance_id="inst_project",
        member_id="mem_a",
        project_id=started["project_id"],
        decision="Use walnut shelves.",
        subject="Shelving",
    )
    decisions = await canvas.page_read(
        instance_id="inst_project",
        canvas_id=started["canvas_id"],
        page_slug="decisions.md",
    )

    assert result["ok"] is False
    assert result["partial"] is True
    assert result["failed_step"] == "knowledge"
    assert "Use walnut shelves." in decisions.extra["body"]
