"""SELF-IMPROVEMENT-WORKFLOW-V1 bring-up helper.

Loads ``specs/workflows/self_improvement.workflow.yaml``, substitutes
the installer placeholders for the running instance_id, and registers
+ activates the workflow against the Spec 5 authoring layer + WTC
runtime. Called during ``bring_up_substrate`` (Spec 6 B3 ordering:
emitters launch AFTER this helper succeeds so trigger predicates
exist before the events fire).

Idempotent on re-call within an instance — leverages Spec 5 13th
amendment's idempotent register (matching descriptor digest →
``already_registered=True``); activate is idempotent on already-
active. The triggers are deterministically derived from
``(workflow_id, descriptor)`` via Spec 4's compile_descriptor_triggers,
so re-registration produces the same trigger_id and the WTC
runtime's register is also idempotent.

The helper enforces fail-loud semantics per v7 H3: every
AuthoringResult / activation result is checked; failure raises
``RuntimeError`` with formatted error context so the bring-up halt
is loud rather than a partial-state startup.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from kernos.kernel.workflows.authoring import (
    ACTOR_ARCHITECT,
    AuthoringContext,
    activate_workflow,
    register_workflow,
)
from kernos.kernel.workflows.registered_workflows import TIER_SUBSTRATE

if TYPE_CHECKING:
    from kernos.kernel.triggers.runtime import TriggerEvaluationRuntime
    from kernos.kernel.workflows.execution_engine import ExecutionEngine

logger = logging.getLogger(__name__)


# Default workflow YAML location relative to repo root. The helper
# accepts an override path for tests (tests construct a minimal
# fixture workflow) while production wiring uses the canonical
# location.
_DEFAULT_WORKFLOW_YAML_PATH = "specs/workflows/self_improvement.workflow.yaml"


# Placeholder substitutions the helper applies to the loaded
# descriptor before register. The architect's v7 H2 fold pinned
# top-level instance_id substitution as a programmatic operation
# (NOT a payload.<path> substitution); this helper mirrors that
# pattern for any future installer placeholders the workflow needs.
def _substitute_installer_placeholders(descriptor: dict, instance_id: str) -> dict:
    """Walk the descriptor and replace ``{installer.instance_id}``
    occurrences with the actual instance_id. Returns a NEW dict
    (input is not mutated) so callers can re-use the loaded YAML
    across instances.

    Substitution scope per v7 H2 fold:
      * top-level ``instance_id``
      * ``triggers[*].event_selector.operands[*].value`` when the
        operand's ``path`` is exactly ``"instance_id"`` (top-level
        Event field — engine-trusted; NOT ``payload.instance_id``).

    Other ``{installer.X}`` placeholders pass through unchanged for
    Spec 4's descriptor parser to handle via its standard
    parameter-resolution path.
    """
    import copy

    out = copy.deepcopy(descriptor)
    placeholder = "{installer.instance_id}"
    # Top-level instance_id.
    if out.get("instance_id") == placeholder:
        out["instance_id"] = instance_id
    # Plural-triggers (Spec 5 12th amendment) event_selector operands
    # whose path == "instance_id" (top-level Event field per v7 H2).
    for trigger in out.get("triggers") or []:
        selector = trigger.get("event_selector") or {}
        for operand in _walk_selector_operands(selector):
            if operand.get("path") == "instance_id" and operand.get("value") == placeholder:
                operand["value"] = instance_id
    return out


def _walk_selector_operands(selector: dict):
    """Yield every operand dict reachable from a (possibly composite)
    event_selector AST. Used by the placeholder substituter to find
    all instance_id-bound operands without hardcoding AST depth."""
    op = selector.get("op", "")
    if op in ("AND", "OR"):
        for child in selector.get("operands") or []:
            yield from _walk_selector_operands(child)
    elif op == "NOT":
        operand = selector.get("operand")
        if isinstance(operand, dict):
            yield from _walk_selector_operands(operand)
    else:
        # Leaf operand (eq, exists, etc.) — yield as-is.
        yield selector


def _format_authoring_errors(errors) -> str:
    """Format a list of ValidationError dataclasses into a single
    diagnostic string for the RuntimeError surface."""
    return "; ".join(
        f"{err.category}@{err.field_path}: {err.message}" for err in errors
    )


async def register_self_improvement_workflow(
    *,
    engine: "ExecutionEngine",
    architect_ctx: AuthoringContext,
    instance_id: str,
    trigger_runtime: "TriggerEvaluationRuntime",
    operator_actor_id: str = "",
    workflow_yaml_path: str | Path | None = None,
) -> str:
    """Register + activate the self_improvement workflow and wire its
    triggers into the WTC runtime.

    ``operator_actor_id`` is the actor identity the workflow's
    autonomy-tool calls will carry. Triggers register with
    ``member_id=operator_actor_id`` so the workflow execution
    context's member_id flows through to ``call_tool`` actions; the
    autonomy tools' operator gate (``_is_operator``) sees the
    operator kind via ``derive_actor_kind`` and accepts the call.
    Empty operator_actor_id is permitted at registration time but
    will cause the workflow's autonomy-tool calls to fail at
    execution time with CAT_AUTONOMY_NOT_AUTHORIZED — caller should
    pass the value from ``KERNOS_OPERATOR_ACTOR_ID`` for production.

    Returns the registered workflow_id. Idempotent on re-call within
    the same instance (Spec 5 13th amendment + Spec 5 H6 activate
    CAS + Spec 4 deterministic trigger_id derivation).

    Raises ``RuntimeError`` on any authoring / activation failure
    with formatted error context — bring-up halts loudly rather
    than producing a partially-initialised autonomy loop (v7 H3
    fail-loud pattern).
    """
    if architect_ctx.actor_kind != ACTOR_ARCHITECT:
        raise RuntimeError(
            f"register_self_improvement_workflow requires architect "
            f"actor; got actor_kind={architect_ctx.actor_kind!r}"
        )
    # Resolve the YAML path. Production callers omit the kwarg →
    # canonical location; tests pass an override.
    if workflow_yaml_path is None:
        # Repo-root-relative resolution: walk up from this module to
        # find specs/workflows/. Bring-up runs from arbitrary cwd in
        # production, so use a module-anchored search.
        module_path = Path(__file__).resolve()
        for ancestor in module_path.parents:
            candidate = ancestor / _DEFAULT_WORKFLOW_YAML_PATH
            if candidate.exists():
                workflow_yaml_path = candidate
                break
        if workflow_yaml_path is None:
            raise RuntimeError(
                f"register_self_improvement_workflow could not locate "
                f"{_DEFAULT_WORKFLOW_YAML_PATH!r} via module-anchored "
                f"search; pass workflow_yaml_path explicitly."
            )
    workflow_yaml_path = Path(workflow_yaml_path)
    if not workflow_yaml_path.exists():
        raise RuntimeError(
            f"register_self_improvement_workflow: YAML not found at "
            f"{workflow_yaml_path}"
        )
    raw = workflow_yaml_path.read_text(encoding="utf-8")
    descriptor = yaml.safe_load(raw)
    if not isinstance(descriptor, dict):
        raise RuntimeError(
            f"register_self_improvement_workflow: YAML at "
            f"{workflow_yaml_path} did not parse to a dict; got "
            f"{type(descriptor).__name__}"
        )
    descriptor = _substitute_installer_placeholders(descriptor, instance_id)

    # Spec 5 13th amendment makes register idempotent on existing
    # match — same descriptor → success with idempotent_replay flag.
    register_result = await register_workflow(
        engine, architect_ctx, descriptor, TIER_SUBSTRATE,
    )
    if not register_result.success:
        raise RuntimeError(
            f"register_self_improvement_workflow: register_workflow "
            f"failed: {_format_authoring_errors(register_result.errors)}"
        )
    workflow_id = register_result.workflow_id
    logger.info(
        "SELF_IMPROVEMENT_WORKFLOW_REGISTERED workflow_id=%s "
        "instance_id=%s idempotent_replay=%s",
        workflow_id, instance_id,
        register_result.extra.get("idempotent_replay", False),
    )

    # Activate. Idempotent on already-active per Spec 5 H6 CAS.
    activate_result = await activate_workflow(
        engine, architect_ctx, workflow_id,
    )
    if not activate_result.success:
        raise RuntimeError(
            f"register_self_improvement_workflow: activate_workflow "
            f"failed: {_format_authoring_errors(activate_result.errors)}"
        )
    logger.info(
        "SELF_IMPROVEMENT_WORKFLOW_ACTIVATED workflow_id=%s "
        "already_active=%s",
        workflow_id,
        activate_result.extra.get("already_active", False),
    )

    # Compile + register triggers with the WTC runtime so the
    # FrictionPatternFrequencyEmitter-emitted events route to a
    # known destination once the emitter starts (B3 ordering: this
    # registration MUST happen before emitters launch).
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
        "SELF_IMPROVEMENT_WORKFLOW_TRIGGERS_REGISTERED workflow_id=%s "
        "trigger_count=%d",
        workflow_id, len(compiled),
    )

    return workflow_id


__all__ = [
    "register_self_improvement_workflow",
]
