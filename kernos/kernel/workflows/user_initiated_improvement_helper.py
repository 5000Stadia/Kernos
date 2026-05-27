"""USER-INITIATED-IMPROVEMENT-TRIGGER-V1 — workflow registration helper.

Mirrors :mod:`kernos.kernel.workflows.self_improvement_helper` but
for the ``user_initiated_improvement`` workflow that fires on
``user.fix_authorization_received`` events.

Loads ``specs/workflows/user_initiated_improvement.workflow.yaml``,
substitutes installer placeholders, registers the workflow with
the engine, activates it, and registers its triggers with the WTC
runtime.

Idempotent on re-call within the same instance (Spec 5 13th
amendment).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from kernos.kernel.workflows.authoring import (
    ACTOR_ARCHITECT,
    AuthoringContext,
    TIER_SUBSTRATE,
    activate_workflow,
    register_workflow,
)

if TYPE_CHECKING:
    from kernos.kernel.triggers.runtime import TriggerEvaluationRuntime
    from kernos.kernel.workflows.execution_engine import ExecutionEngine


logger = logging.getLogger(__name__)


_DEFAULT_WORKFLOW_YAML_PATH = (
    "specs/workflows/user_initiated_improvement.workflow.yaml"
)


def _substitute_installer_placeholders(
    descriptor: dict, instance_id: str,
) -> dict:
    """Substitute ``{installer.instance_id}`` placeholder with the
    concrete instance_id. Mirrors self_improvement_helper's helper.
    Walks the descriptor recursively (top-level + nested dict/list
    structures); the event_selector predicates reference the same
    placeholder."""
    placeholder = "{installer.instance_id}"

    def _walk(node):
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        if isinstance(node, str) and placeholder in node:
            return node.replace(placeholder, instance_id)
        return node

    return _walk(descriptor)


def _format_authoring_errors(errors) -> str:
    return "; ".join(
        f"{err.category}@{err.field_path}: {err.message}"
        for err in errors
    )


async def register_user_initiated_improvement_workflow(
    *,
    engine: "ExecutionEngine",
    architect_ctx: AuthoringContext,
    instance_id: str,
    trigger_runtime: "TriggerEvaluationRuntime",
    operator_actor_id: str = "",
    workflow_yaml_path: str | Path | None = None,
) -> str:
    """Register + activate the user_initiated_improvement workflow
    and wire its trigger into the WTC runtime.

    Returns the registered workflow_id. Idempotent.

    Raises RuntimeError on any failure — bring-up halts loudly
    rather than producing a partially-initialised user-initiated
    loop.
    """
    if architect_ctx.actor_kind != ACTOR_ARCHITECT:
        raise RuntimeError(
            f"register_user_initiated_improvement_workflow requires "
            f"architect actor; got actor_kind="
            f"{architect_ctx.actor_kind!r}"
        )

    if workflow_yaml_path is None:
        module_path = Path(__file__).resolve()
        for ancestor in module_path.parents:
            candidate = ancestor / _DEFAULT_WORKFLOW_YAML_PATH
            if candidate.exists():
                workflow_yaml_path = candidate
                break
        if workflow_yaml_path is None:
            raise RuntimeError(
                f"register_user_initiated_improvement_workflow "
                f"could not locate {_DEFAULT_WORKFLOW_YAML_PATH!r} "
                f"via module-anchored search; pass workflow_yaml_path "
                f"explicitly."
            )
    workflow_yaml_path = Path(workflow_yaml_path)
    if not workflow_yaml_path.exists():
        raise RuntimeError(
            f"register_user_initiated_improvement_workflow: YAML "
            f"not found at {workflow_yaml_path}"
        )
    raw = workflow_yaml_path.read_text(encoding="utf-8")
    descriptor = yaml.safe_load(raw)
    if not isinstance(descriptor, dict):
        raise RuntimeError(
            f"register_user_initiated_improvement_workflow: YAML "
            f"at {workflow_yaml_path} did not parse to a dict; "
            f"got {type(descriptor).__name__}"
        )
    descriptor = _substitute_installer_placeholders(
        descriptor, instance_id,
    )

    register_result = await register_workflow(
        engine, architect_ctx, descriptor, TIER_SUBSTRATE,
    )
    if not register_result.success:
        raise RuntimeError(
            f"register_user_initiated_improvement_workflow: "
            f"register_workflow failed: "
            f"{_format_authoring_errors(register_result.errors)}"
        )
    workflow_id = register_result.workflow_id
    logger.info(
        "USER_INITIATED_IMPROVEMENT_WORKFLOW_REGISTERED "
        "workflow_id=%s instance_id=%s idempotent_replay=%s",
        workflow_id, instance_id,
        register_result.extra.get("idempotent_replay", False),
    )

    activate_result = await activate_workflow(
        engine, architect_ctx, workflow_id,
    )
    if not activate_result.success:
        raise RuntimeError(
            f"register_user_initiated_improvement_workflow: "
            f"activate_workflow failed: "
            f"{_format_authoring_errors(activate_result.errors)}"
        )
    logger.info(
        "USER_INITIATED_IMPROVEMENT_WORKFLOW_ACTIVATED workflow_id=%s "
        "already_active=%s",
        workflow_id,
        activate_result.extra.get("already_active", False),
    )

    from kernos.kernel.triggers import compile_descriptor_triggers
    compiled = compile_descriptor_triggers(
        workflow_id=workflow_id, descriptor=descriptor,
    )
    for ct in compiled:
        await trigger_runtime.register(
            trigger_id=ct.trigger_id,
            instance_id=instance_id,
            workflow_id=workflow_id,
            predicate=ct.predicate,
            member_id=operator_actor_id,
        )
    logger.info(
        "USER_INITIATED_IMPROVEMENT_WORKFLOW_TRIGGERS_REGISTERED "
        "workflow_id=%s trigger_count=%d",
        workflow_id, len(compiled),
    )

    return workflow_id


__all__ = [
    "register_user_initiated_improvement_workflow",
]
