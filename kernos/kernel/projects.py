"""Long-horizon project tools.

Projects bind existing Kernos primitives: a ContextSpace, a pinned canvas,
project_decision knowledge entries, and one plain scheduler reminder.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from kernos.kernel.canvas import CanvasService
from kernos.kernel.scheduler import handle_manage_schedule
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import (
    InstanceProfile,
    KnowledgeEntry,
    ProjectState,
    StateStore,
    _knowledge_id,
)
from kernos.utils import utc_now

logger = logging.getLogger(__name__)


START_PROJECT_TOOL = {
    "name": "start_project",
    "description": (
        "Start a long-horizon project by creating a project space, pinned "
        "canvas, seeded project pages, durable project_state row, and a "
        "plain check-in reminder."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "initial_note": {"type": "string", "default": ""},
            "checkin_cadence": {"type": "string", "default": "weekly"},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
}


RECORD_PROJECT_DECISION_TOOL = {
    "name": "record_project_decision",
    "description": (
        "Record a project decision to the project's decisions canvas page "
        "and durable project_decision knowledge."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "default": ""},
            "decision": {"type": "string", "minLength": 1},
            "subject": {"type": "string", "default": ""},
        },
        "required": ["decision"],
        "additionalProperties": False,
    },
}


SURFACE_PROJECT_STATUS_TOOL = {
    "name": "surface_project_status",
    "description": (
        "Summarize a long-horizon project's state, recent decisions, "
        "timeline, open loops, next steps, and check-in reminder."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "default": ""},
        },
        "required": [],
        "additionalProperties": False,
    },
}


def _project_id() -> str:
    return f"project_{uuid.uuid4().hex[:8]}"


def _space_id() -> str:
    return f"space_{uuid.uuid4().hex[:8]}"


def _parse_schedule_result(result: str) -> tuple[str, str]:
    """Extract trigger id and next fire from manage_schedule text."""
    trigger_id = ""
    next_checkin_at = ""
    id_match = re.search(r"\bID:\s*([A-Za-z0-9_:-]+)", result or "")
    if id_match:
        trigger_id = id_match.group(1).strip().rstrip(".")
    next_match = re.search(r"Next fire:\s*([^\n]+)", result or "")
    if next_match:
        next_checkin_at = next_match.group(1).strip()
    return trigger_id, next_checkin_at


def _checkin_schedule_text(project_name: str, cadence: str) -> str:
    """Return a schedule phrase the scheduler extractor handles reliably."""
    normalized = (cadence or "weekly").strip().lower()
    message = f"check in on {project_name}"
    if normalized in {"daily", "day", "every day"}:
        return f"Every day at 9:00 AM, remind me to {message}."
    if normalized in {"monthly", "month", "every month"}:
        return (
            "On the first Monday of every month at 9:00 AM, "
            f"remind me to {message}."
        )
    if normalized in {"biweekly", "every two weeks", "every 2 weeks"}:
        return f"Every 2 weeks on Monday at 9:00 AM, remind me to {message}."
    if normalized in {"weekly", "week", "every week"}:
        return f"Every Monday at 9:00 AM, remind me to {message}."
    return f"Every {normalized} at 9:00 AM, remind me to {message}."


async def _set_active_space(
    state: StateStore,
    *,
    instance_id: str,
    member_id: str,
    space_id: str,
) -> None:
    """Persist active space the same way routing does after a space switch."""
    if not space_id:
        return
    now = utc_now()
    profile = await state.get_instance_profile(instance_id)
    if profile is None:
        profile = InstanceProfile(
            instance_id=instance_id,
            status="active",
            created_at=now,
        )
    profile.last_active_space_id = space_id
    await state.save_instance_profile(instance_id, profile)


def _most_recent_project(projects: list[ProjectState]) -> ProjectState | None:
    active = [p for p in projects if p.lifecycle_state == "active"]
    if not active:
        return None
    return max(
        active,
        key=lambda p: (
            p.last_activity_at or p.updated_at or p.created_at or "",
            p.updated_at or "",
            p.created_at or "",
        ),
    )


def _seed_pages(project_name: str, initial_note: str) -> dict[str, tuple[str, str]]:
    date = utc_now().split("T", 1)[0]
    note = initial_note.strip() or "No initial note."
    return {
        "index.md": (
            project_name,
            (
                f"# {project_name}\n\n"
                "- [Overview](overview.md)\n"
                "- [Decisions](decisions.md)\n"
                "- [Timeline](timeline.md)\n"
                "- [Open loops](open-loops.md)\n"
                "- [Next steps](next-steps.md)\n"
            ),
        ),
        "overview.md": (
            "Overview",
            (
                f"# {project_name} Overview\n\n"
                "## Goal\n\n"
                f"{note}\n\n"
                "## Constraints\n\n"
                "- None captured yet.\n\n"
                "## Current Shape\n\n"
                f"- Project started {date}.\n"
            ),
        ),
        "decisions.md": (
            "Decisions",
            "# Decisions\n\nDated decisions and reversals will accumulate here.\n",
        ),
        "timeline.md": (
            "Timeline",
            f"# Timeline\n\n- {date}: Project started.\n",
        ),
        "open-loops.md": (
            "Open Loops",
            "# Open Loops\n\n- None captured yet.\n",
        ),
        "next-steps.md": (
            "Next Steps",
            "# Next Steps\n\n- None captured yet.\n",
        ),
    }


async def _resolve_project(
    state: StateStore,
    *,
    instance_id: str,
    member_id: str = "",
    active_space_id: str = "",
    project_id: str = "",
    active_only: bool = True,
) -> ProjectState | None:
    if project_id:
        project = await state.get_project_state(instance_id, project_id)
    elif active_space_id:
        project = await state.get_project_state_by_space(
            instance_id,
            active_space_id,
            lifecycle_state="active" if active_only else None,
        )
    else:
        project = None
    if not project and active_only:
        try:
            projects = await state.list_active_projects(
                instance_id,
                owner_member_id=member_id,
            )
        except Exception as exc:  # noqa: BLE001 — fallback is ergonomic only
            logger.debug("PROJECT_RESOLVE_FALLBACK_FAILED: %s", exc)
            projects = []
        project = _most_recent_project(projects if isinstance(projects, list) else [])
    if active_only and project and project.lifecycle_state != "active":
        return None
    return project


async def start_project(
    *,
    state: StateStore,
    canvas: CanvasService | None,
    trigger_store: Any,
    reasoning_service: Any,
    instance_id: str,
    member_id: str = "",
    active_space_id: str = "",
    conversation_id: str = "",
    name: str,
    initial_note: str = "",
    checkin_cadence: str = "weekly",
    user_timezone: str = "",
) -> dict:
    """Create the V1 project shape and return a compact dict receipt."""
    project_name = name.strip()
    if not project_name:
        return {"ok": False, "error": "Project name is required."}
    if canvas is None:
        return {"ok": False, "error": "Canvas service is not available."}

    now = utc_now()
    pid = _project_id()
    sid = _space_id()
    owner = member_id or instance_id
    space = ContextSpace(
        id=sid,
        instance_id=instance_id,
        member_id=member_id,
        name=project_name,
        description=initial_note.strip(),
        space_type="domain",
        status="active",
        is_default=False,
        created_at=now,
        last_active_at=now,
    )
    await state.save_context_space(space)

    created = await canvas.create(
        instance_id=instance_id,
        creator_member_id=owner,
        name=project_name,
        scope="personal",
        description=initial_note.strip() or f"Long-horizon project: {project_name}",
        pinned_to_spaces=[sid],
    )
    if not created.ok:
        return {
            "ok": False,
            "failed_step": "canvas_create",
            "error": created.error or "Canvas create failed.",
            "project_id": pid,
            "space_id": sid,
        }

    canvas_id = created.canvas_id
    for page_slug, (title, body) in _seed_pages(project_name, initial_note).items():
        written = await canvas.page_write(
            instance_id=instance_id,
            canvas_id=canvas_id,
            page_slug=page_slug,
            body=body,
            writer_member_id=owner,
            title=title,
            page_type="note",
            state="current",
        )
        if not written.ok:
            return {
                "ok": False,
                "failed_step": f"page_write:{page_slug}",
                "error": written.error or "Canvas page write failed.",
                "project_id": pid,
                "space_id": sid,
                "canvas_id": canvas_id,
            }

    project = ProjectState(
        project_id=pid,
        instance_id=instance_id,
        owner_member_id=member_id,
        space_id=sid,
        canvas_id=canvas_id,
        name=project_name,
        lifecycle_state="active",
        created_at=now,
        updated_at=now,
        last_activity_at=now,
        data={"initial_note": initial_note, "checkin_cadence": checkin_cadence},
    )
    await state.insert_project_state(project)
    try:
        await _set_active_space(
            state,
            instance_id=instance_id,
            member_id=member_id,
            space_id=sid,
        )
    except Exception as exc:  # noqa: BLE001 — project already exists
        logger.warning(
            "PROJECT_ACTIVE_SPACE_SET_FAILED: project=%s space=%s error=%s",
            pid,
            sid,
            exc,
        )

    schedule_text = _checkin_schedule_text(project_name, checkin_cadence)
    schedule_result = ""
    checkin_trigger_id = ""
    next_checkin_at = ""
    reminder_created = False
    reminder_reason = "Reminder store or reasoning service was not available."
    if trigger_store is not None and reasoning_service is not None:
        reminder_reason = ""
        try:
            schedule_result = await handle_manage_schedule(
                trigger_store,
                instance_id,
                member_id,
                sid,
                action="create",
                description=schedule_text,
                reasoning_service=reasoning_service,
                conversation_id=conversation_id,
                user_timezone=user_timezone,
            )
            if schedule_result.startswith("Error:"):
                reminder_reason = schedule_result
            else:
                checkin_trigger_id, next_checkin_at = _parse_schedule_result(
                    schedule_result
                )
                if checkin_trigger_id:
                    reminder_created = True
                    await state.update_project_activity(
                        instance_id,
                        pid,
                        last_activity_at=now,
                        checkin_trigger_id=checkin_trigger_id,
                        next_checkin_at=next_checkin_at,
                    )
                else:
                    reminder_reason = (
                        "Schedule handler did not return a trigger ID."
                    )
                    logger.warning(
                        "PROJECT_CHECKIN_CREATE_FAILED: project=%s reason=%s result=%r",
                        pid,
                        reminder_reason,
                        schedule_result,
                    )
        except Exception as exc:  # noqa: BLE001 — reminder is best-effort
            logger.warning("PROJECT_CHECKIN_CREATE_FAILED: project=%s error=%s", pid, exc)
            schedule_result = f"Error: {exc}"
            reminder_reason = schedule_result

    result = {
        "ok": True,
        "project_id": pid,
        "space_id": sid,
        "active_space_id": sid,
        "canvas_id": canvas_id,
        "name": project_name,
        "lifecycle_state": "active",
        "checkin_trigger_id": checkin_trigger_id,
        "next_checkin_at": next_checkin_at,
        "checkin_result": schedule_result,
        "reminder_created": reminder_created,
    }
    if not reminder_created:
        result["partial"] = True
        result["reminder_reason"] = reminder_reason
    return result


async def record_project_decision(
    *,
    state: StateStore,
    canvas: CanvasService | None,
    instance_id: str,
    member_id: str = "",
    active_space_id: str = "",
    project_id: str = "",
    decision: str,
    subject: str = "",
) -> dict:
    """Append a decision to project canvas pages and knowledge."""
    decision_text = decision.strip()
    if not decision_text:
        return {"ok": False, "error": "Decision is required."}
    if canvas is None:
        return {"ok": False, "error": "Canvas service is not available."}
    project = await _resolve_project(
        state,
        instance_id=instance_id,
        member_id=member_id,
        active_space_id=active_space_id,
        project_id=project_id,
        active_only=True,
    )
    if not project:
        return {"ok": False, "error": "No active project found."}

    now = utc_now()
    date = now.split("T", 1)[0]
    subject_text = subject.strip() or project.name
    read = await canvas.page_read(
        instance_id=instance_id,
        canvas_id=project.canvas_id,
        page_slug="decisions.md",
    )
    existing = read.extra.get("body", "") if read.ok else "# Decisions\n"
    entry = f"\n\n## {date} - {subject_text}\n\n- {decision_text}\n"
    written = await canvas.page_write(
        instance_id=instance_id,
        canvas_id=project.canvas_id,
        page_slug="decisions.md",
        body=existing.rstrip() + entry,
        writer_member_id=member_id or project.owner_member_id or instance_id,
        title="Decisions",
        page_type="note",
        state="current",
    )
    if not written.ok:
        return {
            "ok": False,
            "partial": False,
            "failed_step": "decisions_canvas",
            "error": written.error or "Decision canvas write failed.",
            "project_id": project.project_id,
        }

    timeline_error = ""
    timeline_read = await canvas.page_read(
        instance_id=instance_id,
        canvas_id=project.canvas_id,
        page_slug="timeline.md",
    )
    if timeline_read.ok:
        timeline_body = timeline_read.extra.get("body", "")
        timeline_line = f"\n- {date}: Decision - {subject_text}.\n"
        timeline_write = await canvas.page_write(
            instance_id=instance_id,
            canvas_id=project.canvas_id,
            page_slug="timeline.md",
            body=timeline_body.rstrip() + timeline_line,
            writer_member_id=member_id or project.owner_member_id or instance_id,
            title="Timeline",
            page_type="note",
            state="current",
        )
        if not timeline_write.ok:
            timeline_error = timeline_write.error or "Timeline canvas write failed."

    try:
        knowledge = KnowledgeEntry(
            id=_knowledge_id(),
            instance_id=instance_id,
            category="project_decision",
            subject=subject_text,
            content=decision_text,
            confidence="stated",
            source_event_id="",
            source_description=f"Project decision for {project.name}",
            created_at=now,
            last_referenced=now,
            tags=[
                f"project:{project.project_id}",
                f"space:{project.space_id}",
                "project_decision",
            ],
            context_space=project.space_id,
            owner_member_id=member_id,
        )
        await state.add_knowledge(knowledge)
    except Exception as exc:  # noqa: BLE001 — spec requires explicit partial
        logger.warning(
            "PROJECT_DECISION_KNOWLEDGE_FAILED: project=%s error=%s",
            project.project_id,
            exc,
        )
        return {
            "ok": False,
            "partial": True,
            "failed_step": "knowledge",
            "error": str(exc),
            "project_id": project.project_id,
            "space_id": project.space_id,
            "canvas_id": project.canvas_id,
        }

    try:
        await state.update_project_activity(
            instance_id,
            project.project_id,
            last_activity_at=now,
        )
    except Exception as exc:  # noqa: BLE001 — activity timestamp is advisory
        logger.debug("PROJECT_ACTIVITY_UPDATE_FAILED: %s", exc)

    result = {
        "ok": True,
        "project_id": project.project_id,
        "space_id": project.space_id,
        "canvas_id": project.canvas_id,
        "subject": subject_text,
        "decision": decision_text,
    }
    if timeline_error:
        result["timeline_error"] = timeline_error
    return result


def _page_lines(body: str, limit: int = 6) -> list[str]:
    lines: list[str] = []
    for raw in (body or "").splitlines():
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        if text.lower().startswith("dated decisions"):
            continue
        if text.startswith("- "):
            text = text[2:].strip()
        lines.append(text)
        if len(lines) >= limit:
            break
    return lines


async def _read_page_summary(
    canvas: CanvasService | None,
    *,
    instance_id: str,
    canvas_id: str,
    page_slug: str,
    limit: int = 6,
) -> list[str]:
    if canvas is None:
        return []
    result = await canvas.page_read(
        instance_id=instance_id,
        canvas_id=canvas_id,
        page_slug=page_slug,
    )
    if not result.ok:
        return []
    return _page_lines(result.extra.get("body", ""), limit=limit)


async def _query_project_decisions(
    state: StateStore,
    *,
    instance_id: str,
    member_id: str,
    project: ProjectState,
    limit: int = 6,
) -> list[dict[str, str]]:
    project_tag = f"project:{project.project_id}"
    space_tag = f"space:{project.space_id}"
    knowledge = await state.query_knowledge(
        instance_id,
        category="project_decision",
        tags=[project_tag],
        active_only=True,
        limit=1000,
        member_id=member_id,
    )
    recent = []
    for entry in knowledge:
        tags = set(getattr(entry, "tags", []) or [])
        if project_tag in tags and (
            space_tag in tags or entry.context_space == project.space_id
        ):
            recent.append({
                "subject": entry.subject,
                "content": entry.content,
                "created_at": entry.created_at,
            })
    recent.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return recent[:limit]


async def surface_project_status(
    *,
    state: StateStore,
    canvas: CanvasService | None,
    instance_id: str,
    member_id: str = "",
    active_space_id: str = "",
    project_id: str = "",
) -> dict:
    """Assemble compact durable project status."""
    project = await _resolve_project(
        state,
        instance_id=instance_id,
        member_id=member_id,
        active_space_id=active_space_id,
        project_id=project_id,
        active_only=False if project_id else True,
    )
    if not project:
        return {"ok": False, "error": "No project found."}

    recent = await _query_project_decisions(
        state,
        instance_id=instance_id,
        member_id=member_id,
        project=project,
    )

    timeline = await _read_page_summary(
        canvas,
        instance_id=instance_id,
        canvas_id=project.canvas_id,
        page_slug="timeline.md",
    )
    open_loops = await _read_page_summary(
        canvas,
        instance_id=instance_id,
        canvas_id=project.canvas_id,
        page_slug="open-loops.md",
    )
    next_steps = await _read_page_summary(
        canvas,
        instance_id=instance_id,
        canvas_id=project.canvas_id,
        page_slug="next-steps.md",
    )

    return {
        "ok": True,
        "project_id": project.project_id,
        "name": project.name,
        "lifecycle_state": project.lifecycle_state,
        "space_id": project.space_id,
        "canvas_id": project.canvas_id,
        "recent_decisions": recent,
        "timeline": timeline,
        "open_loops": open_loops,
        "next_steps": next_steps,
        "checkin_trigger_id": project.checkin_trigger_id,
        "next_checkin_at": project.next_checkin_at,
        "last_activity_at": project.last_activity_at,
        "completed_at": project.completed_at,
        "completion_summary": project.completion_summary,
    }
