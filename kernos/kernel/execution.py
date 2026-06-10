"""Self-Directed Execution — plan creation, step execution, budget enforcement.

The agent creates a multi-step plan and executes it autonomously by sending
messages to itself. Each step is a turn through the existing pipeline with
a lighter cohort configuration (skip preference detection, cross-domain signals).

The plan is a JSON file (_plan.json) in the workspace space. The agent reads
it at the start of each step, updates it after each step, and the kernel
enforces budget ceilings (steps, tokens, time).
"""
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kernos.utils import utc_now, _safe_name

logger = logging.getLogger(__name__)


@dataclass
class ExecutionEnvelope:
    """Metadata for a self-directed turn."""
    plan_id: str
    step_id: str
    workspace_id: str
    step_description: str
    budget_steps: int = 30
    budget_tokens: int = 500000
    budget_time_s: int = 3600
    steps_used: int = 0
    tokens_used: int = 0
    elapsed_s: int = 0
    interruptible: bool = True
    source: str = "self_directed"
    is_final_step: bool = False
    member_id: str = ""  # the member who owns/created the plan (routing context)
    # STEP-COMPLETION DISCIPLINE (#189): set on the single bounded re-dispatch
    # of a step whose named actions didn't all run. The continuation turn
    # carries the deficit so the model finishes ONLY what's missing; a
    # continuation is never re-dispatched again (bounded at one).
    completion_retry: bool = False
    completion_deficit: str = ""


async def verify_step_completion(
    reasoning: Any, step_description: str, response_text: str,
) -> tuple[bool, str]:
    """Check whether a plan step's named actions actually ran (#189).

    The spine previously marked a step complete because the turn produced
    text — even when the step's own report said an action was skipped
    (live: "registration was not attempted", the unwritten final report).
    The failures were honestly NARRATED but dishonestly BOOKKEPT, so the
    verifier only has to read the narration: one cheap, strict-contract
    model call comparing the step's stated actions against the agent's
    own report.

    Returns ``(complete, missing)``. Fail-OPEN: any error returns
    ``(True, "")`` — a broken verifier must never stall a plan.
    """
    import json as _json
    try:
        result = await reasoning.complete_simple(
            system_prompt=(
                "You audit whether a plan step's work is COMPLETE, using the "
                "step's stated actions and the agent's own report of what it "
                "did. A step is INCOMPLETE only when the step text names a "
                "concrete action (e.g. 'register the tool', 'write the "
                "report', 'send X') that the report shows was NOT performed "
                "— skipped, deferred, 'not attempted', or simply absent. "
                "Judge ONLY actions the step names; do not invent extra "
                "requirements; honest partial results with all named actions "
                "attempted count as complete. Respond ONLY with JSON: "
                '{"complete": true|false, "missing": "<missing action(s), '
                'brief>"}'
            ),
            user_content=(
                f"STEP: {step_description}\n\n"
                f"AGENT REPORT:\n{(response_text or '')[:4000]}"
            ),
            output_schema={
                "type": "object",
                "properties": {
                    "complete": {"type": "boolean"},
                    "missing": {"type": "string"},
                },
                "required": ["complete", "missing"],
            },
            max_tokens=150,
            prefer_cheap=True,
        )
        parsed = _json.loads(result)
        complete = bool(parsed.get("complete", True))
        missing = str(parsed.get("missing") or "").strip()
        if complete:
            return True, ""
        if not missing:
            # An INCOMPLETE verdict with no named deficit is unactionable —
            # treat as complete rather than dispatch a continuation with no
            # instruction.
            return True, ""
        return False, missing[:400]
    except Exception as exc:  # fail-open — verifier must never stall a plan
        logger.warning("STEP_COMPLETION_VERIFY: failed open: %s", exc)
        return True, ""


MANAGE_PLAN_TOOL = {
    "name": "manage_plan",
    "description": (
        "Create, execute, and manage self-directed plans. Use 'create' to start "
        "a new plan, 'continue' to execute the next step, 'status' to check "
        "progress, 'pause' to stop execution. "
        "For 'create' the EASIEST way is to pass `steps` as a flat list of "
        "short descriptions, e.g. steps=[\"read the file\", \"summarize it\", "
        "\"write the result\"] — the rest of the structure is filled in for you. "
        "A full `phases` structure is also accepted if you want sub-grouping."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "continue", "status", "pause"],
                "description": (
                    "create: Build a new plan and kick off the first step. "
                    "continue: Execute the next step in an active/paused plan. "
                    "status: Return current plan state. "
                    "pause: Pause an active plan."
                ),
            },
            "title": {
                "type": "string",
                "description": "Plan title (optional for 'create').",
            },
            "steps": {
                "type": "array",
                "description": (
                    "Simplest way to create a plan: a flat list of step "
                    "descriptions (strings). ids/structure are auto-built. "
                    "Provide EITHER `steps` OR `phases`, not both."
                ),
                "items": {"type": "string"},
            },
            "phases": {
                "type": "array",
                "description": "Advanced alternative to `steps`: explicit phases, each with id, title, and steps.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "title": {"type": "string"},
                                },
                                "required": ["id", "title"],
                            },
                        },
                    },
                    "required": ["id", "title", "steps"],
                },
            },
            "plan_id": {
                "type": "string",
                "description": "Plan ID (required for continue/status/pause; auto-generated for create)",
            },
            "step_id": {
                "type": "string",
                "description": "Next step to execute (required for 'continue')",
            },
            "step_description": {
                "type": "string",
                "description": "What this step should accomplish (required for 'continue')",
            },
            "notify_user": {
                "type": "string",
                "description": "Optional message to send the user (progress, discovery, completion)",
            },
            "budget_override": {
                "type": "object",
                "description": "Optional budget overrides. Only set when the user explicitly asks to change limits.",
                "properties": {
                    "max_steps": {"type": "integer", "description": "Max steps (0 = no limit)"},
                    "max_tokens": {"type": "integer", "description": "Max tokens (0 = no limit)"},
                    "max_time_s": {"type": "integer", "description": "Max time in seconds (0 = no limit)"},
                },
            },
            "show_progress": {
                "type": "boolean",
                "description": "Show/hide step progress messages to the user. Default true. Set false to run silently.",
            },
        },
        "required": ["action"],
    },
}

# Backward compat alias
CONTINUE_PLAN_TOOL = MANAGE_PLAN_TOOL


def scan_active_plans(data_dir: str) -> list[tuple[str, str, dict]]:
    """Scan all instances/spaces for active plans with in-progress steps.

    Returns list of (instance_id, space_id, plan) tuples.
    """
    results = []
    data_path = Path(data_dir)
    if not data_path.exists():
        return results
    for instance_dir in data_path.iterdir():
        if not instance_dir.is_dir() or instance_dir.name.startswith("."):
            continue
        spaces_dir = instance_dir / "spaces"
        if not spaces_dir.exists():
            continue
        for space_dir in spaces_dir.iterdir():
            plan_file = space_dir / "files" / "_plan.json"
            if not plan_file.exists():
                continue
            try:
                plan = json.loads(plan_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if plan.get("status") != "active":
                continue
            # Check for any in-progress steps
            has_in_progress = any(
                step.get("status") == "in_progress"
                for phase in plan.get("phases", [])
                for step in phase.get("steps", [])
            )
            if has_in_progress:
                # Reconstruct instance_id from directory name
                # tenant dirs are safe_name encoded (colons → underscores etc.)
                # but we store instance_id in the plan or can derive from convention
                # Discord tenants: discord_364303223047323649 → discord:364303223047323649
                raw_name = instance_dir.name
                if raw_name.startswith("discord_"):
                    instance_id = "discord:" + raw_name[len("discord_"):]
                elif raw_name.startswith("sms_"):
                    instance_id = "sms:" + raw_name[len("sms_"):]
                else:
                    instance_id = raw_name
                results.append((instance_id, space_dir.name, plan))
    return results


def generate_plan_id() -> str:
    return f"plan_{uuid.uuid4().hex[:12]}"


def _plan_path(data_dir: str, instance_id: str, space_id: str) -> Path:
    return (
        Path(data_dir) / _safe_name(instance_id) / "spaces" / space_id / "files" / "_plan.json"
    )


async def load_plan(data_dir: str, instance_id: str, space_id: str) -> dict | None:
    """Load _plan.json from a space. Returns None if not found."""
    path = _plan_path(data_dir, instance_id, space_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


async def save_plan(data_dir: str, instance_id: str, space_id: str, plan: dict) -> None:
    """Save _plan.json to a space."""
    path = _plan_path(data_dir, instance_id, space_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    # Generate markdown view
    md_path = path.with_name("_plan.md")
    md_path.write_text(_plan_to_markdown(plan), encoding="utf-8")


def _plan_to_markdown(plan: dict) -> str:
    """Generate a readable markdown view of the plan."""
    lines = [f"# {plan.get('title', 'Plan')}"]
    lines.append(f"**Status:** {plan.get('status', 'unknown')}")
    usage = plan.get("usage", {})
    budget = plan.get("budget", {})
    lines.append(f"**Progress:** {usage.get('steps_used', 0)}/{budget.get('max_steps', '?')} steps, "
                 f"{usage.get('tokens_used', 0):,} tokens")
    lines.append("")

    for phase in plan.get("phases", []):
        lines.append(f"## Phase {phase['id']}: {phase.get('title', '')}")
        for step in phase.get("steps", []):
            status_icon = {"complete": "[x]", "in_progress": "[>]", "pending": "[ ]",
                          "skipped": "[-]", "blocked": "[!]"}.get(step.get("status", ""), "[ ]")
            lines.append(f"  {status_icon} {step['id']}: {step.get('title', '')}")
        lines.append("")

    discoveries = plan.get("discoveries", [])
    if discoveries:
        lines.append("## Discoveries")
        for d in discoveries:
            surfaced = " (surfaced)" if d.get("surfaced") else ""
            lines.append(f"- [{d.get('step', '?')}] {d.get('finding', '')}{surfaced}")

    return "\n".join(lines)


def check_budget(plan: dict) -> str | None:
    """Check if any budget ceiling is hit. Returns reason string or None."""
    usage = plan.get("usage", {})
    budget = plan.get("budget", {})
    if usage.get("steps_used", 0) >= budget.get("max_steps", 30):
        return "step_limit"
    if usage.get("tokens_used", 0) >= budget.get("max_tokens", 500000):
        return "token_budget"
    if usage.get("elapsed_s", 0) >= budget.get("max_time_s", 3600):
        return "time_limit"
    return None


def build_envelope_from_plan(plan: dict, step_id: str, step_description: str) -> ExecutionEnvelope:
    """Build an ExecutionEnvelope from a plan dict."""
    budget = plan.get("budget", {})
    usage = plan.get("usage", {})
    return ExecutionEnvelope(
        plan_id=plan.get("plan_id", ""),
        step_id=step_id,
        workspace_id=plan.get("workspace_id", ""),
        step_description=step_description,
        budget_steps=budget.get("max_steps", 30),
        budget_tokens=budget.get("max_tokens", 500000),
        budget_time_s=budget.get("max_time_s", 3600),
        steps_used=usage.get("steps_used", 0),
        tokens_used=usage.get("tokens_used", 0),
        elapsed_s=usage.get("elapsed_s", 0),
        member_id=plan.get("created_by_member_id", ""),
    )
