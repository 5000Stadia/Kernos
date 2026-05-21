"""SELF-CONTROLLED-LOOP-LIVENESS-V1 — loop-health sentinel bring-up.

A harmless boot-smoke workflow that proves the substrate
event-trigger-workflow loop is alive on every restart. The sentinel
is substrate-owned, NOT creator-authored, so it does not depend on
``KERNOS_ARCHITECT_ACTOR_ID`` being set — unlike the self-improvement
workflow.

Loads ``specs/workflows/loop_health.workflow.yaml``, substitutes the
installer placeholders for the running instance, registers + activates
against the authoring layer with a synthetic substrate-owned architect
context, compiles + registers triggers with the WTC runtime. Mirrors
``self_improvement_helper`` but is unconditional — the sentinel must
come up whether or not the operator has wired self-improvement env
vars (it's the substrate's own liveness heartbeat).

Failure during bring-up logs a loud WARNING and continues; the rest
of substrate brings up regardless. The sentinel is diagnostic
infrastructure — its absence is a strong signal, but its failure
must not cascade into a substrate boot abort.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
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
from kernos.kernel.workflows.self_improvement_helper import (
    _format_authoring_errors,
    _substitute_installer_placeholders,
)

if TYPE_CHECKING:
    from kernos.kernel.triggers.runtime import TriggerEvaluationRuntime
    from kernos.kernel.workflows.execution_engine import ExecutionEngine

logger = logging.getLogger(__name__)


_DEFAULT_LOOP_HEALTH_YAML_PATH = "specs/workflows/loop_health.workflow.yaml"

# Substrate-owned architect identity. The sentinel registers under
# this identity unconditionally; the operator does NOT need to set
# KERNOS_ARCHITECT_ACTOR_ID for the sentinel to come up. The actor_id
# is namespaced under ``substrate.`` so it cannot collide with any
# real human architect identity.
_SENTINEL_ARCHITECT_ACTOR_ID = "substrate.loop_health_sentinel"
_SENTINEL_ARCHITECT = AuthoringContext(
    actor_id=_SENTINEL_ARCHITECT_ACTOR_ID,
    actor_kind=ACTOR_ARCHITECT,
)


@contextmanager
def _substrate_architect_env_override():
    """Codex round 2 finding 1: ``register_workflow`` /
    ``activate_workflow`` consult ``_is_architect(ctx)``, which is
    fail-closed against ``KERNOS_ARCHITECT_ACTOR_ID``. The sentinel
    is substrate-owned and must register even when the env var is
    unset OR set to a different operator-architect identity.

    Save/set/restore the env var for the duration of the sentinel's
    register+activate window. The sentinel's actor_id is namespaced
    under ``substrate.`` so it cannot collide with any human
    architect identity, and the override is scoped to this single
    bring-up call so other architect-gated paths see the original
    value (or its absence) on either side.
    """
    prior = os.environ.get("KERNOS_ARCHITECT_ACTOR_ID")
    os.environ["KERNOS_ARCHITECT_ACTOR_ID"] = _SENTINEL_ARCHITECT_ACTOR_ID
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("KERNOS_ARCHITECT_ACTOR_ID", None)
        else:
            os.environ["KERNOS_ARCHITECT_ACTOR_ID"] = prior


async def register_loop_health_workflow(
    *,
    engine: "ExecutionEngine",
    instance_id: str,
    trigger_runtime: "TriggerEvaluationRuntime",
    workflow_yaml_path: str | Path | None = None,
) -> str:
    """Register + activate the loop_health sentinel workflow and wire
    its trigger into the WTC runtime.

    Returns the registered workflow_id. Idempotent on re-call within
    the same instance — leverages Spec 5 13th amendment's idempotent
    register and Spec 5 H6 activation CAS.

    Raises ``RuntimeError`` on any authoring / activation failure. The
    caller (bring_up_substrate) catches and logs to a WARNING so the
    sentinel's failure does not cascade into a substrate boot abort.
    """
    if workflow_yaml_path is None:
        module_path = Path(__file__).resolve()
        for ancestor in module_path.parents:
            candidate = ancestor / _DEFAULT_LOOP_HEALTH_YAML_PATH
            if candidate.exists():
                workflow_yaml_path = candidate
                break
        if workflow_yaml_path is None:
            raise RuntimeError(
                f"register_loop_health_workflow could not locate "
                f"{_DEFAULT_LOOP_HEALTH_YAML_PATH!r} via module-anchored "
                f"search; pass workflow_yaml_path explicitly."
            )
    workflow_yaml_path = Path(workflow_yaml_path)
    if not workflow_yaml_path.exists():
        raise RuntimeError(
            f"register_loop_health_workflow: YAML not found at "
            f"{workflow_yaml_path}"
        )
    raw = workflow_yaml_path.read_text(encoding="utf-8")
    descriptor = yaml.safe_load(raw)
    if not isinstance(descriptor, dict):
        raise RuntimeError(
            f"register_loop_health_workflow: YAML at {workflow_yaml_path} "
            f"did not parse to a dict; got {type(descriptor).__name__}"
        )
    descriptor = _substitute_installer_placeholders(descriptor, instance_id)

    with _substrate_architect_env_override():
        register_result = await register_workflow(
            engine, _SENTINEL_ARCHITECT, descriptor, TIER_SUBSTRATE,
        )
        if not register_result.success:
            raise RuntimeError(
                f"register_loop_health_workflow: register_workflow failed: "
                f"{_format_authoring_errors(register_result.errors)}"
            )
        workflow_id = register_result.workflow_id
        logger.info(
            "LOOP_HEALTH_WORKFLOW_REGISTERED workflow_id=%s "
            "instance_id=%s idempotent_replay=%s",
            workflow_id, instance_id,
            register_result.extra.get("idempotent_replay", False),
        )

        activate_result = await activate_workflow(
            engine, _SENTINEL_ARCHITECT, workflow_id,
        )
        if not activate_result.success:
            raise RuntimeError(
                f"register_loop_health_workflow: activate_workflow failed: "
                f"{_format_authoring_errors(activate_result.errors)}"
            )
        logger.info(
            "LOOP_HEALTH_WORKFLOW_ACTIVATED workflow_id=%s already_active=%s",
            workflow_id,
            activate_result.extra.get("already_active", False),
        )

    # Compile + register triggers with the WTC runtime so the
    # boot_probe event routes to a known destination once the
    # bring-up emits it.
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
            member_id=_SENTINEL_ARCHITECT_ACTOR_ID,
        )
    logger.info(
        "LOOP_HEALTH_WORKFLOW_TRIGGERS_REGISTERED workflow_id=%s "
        "trigger_count=%d",
        workflow_id, len(compiled),
    )

    return workflow_id


def _generate_boot_id() -> str:
    """Codex round 2 finding: timestamp-only second resolution is
    weak in fast restart/crash-loop cases. Use UTC timestamp +
    time_ns() suffix so multiple boots within the same second remain
    distinguishable in logs and ledger entries."""
    import time
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{time.time_ns()}"


async def emit_boot_probe(
    *,
    instance_id: str,
    event_stream,
    boot_id: str | None = None,
) -> str:
    """Emit the synthetic ``loop_health.boot_probe`` event the
    sentinel workflow listens for. Called from bring_up_substrate
    AFTER ``register_loop_health_workflow`` succeeds AND the
    ``InternalEventAdapter`` is started.

    Returns the boot_id used (so the caller can correlate with the
    later completion subscriber). Logs ``LOOP_HEALTH_BOOT_PROBE_FIRED``
    at INFO so operators have a single-line grep target proving the
    sentinel was triggered.
    """
    from datetime import datetime, timezone

    if boot_id is None:
        boot_id = _generate_boot_id()
    booted_at = datetime.now(timezone.utc).isoformat()
    try:
        await event_stream.emit(
            instance_id, "loop_health.boot_probe",
            {"boot_id": boot_id, "booted_at": booted_at},
            space_id="",
        )
        logger.info(
            "LOOP_HEALTH_BOOT_PROBE_FIRED boot_id=%s booted_at=%s "
            "instance_id=%s",
            boot_id, booted_at, instance_id,
        )
    except Exception as exc:
        logger.warning(
            "LOOP_HEALTH_BOOT_PROBE_EMIT_FAILED instance_id=%s exc=%s",
            instance_id, exc,
        )
    return boot_id


def register_completion_logger(
    *,
    event_stream,
    instance_id: str,
    boot_id: str,
) -> None:
    """Subscribe a post-flush observer to the event stream that logs
    ``LOOP_HEALTH_EXECUTION_COMPLETED boot_id=X`` at INFO when the
    sentinel workflow's ``workflow.execution_terminated`` event
    arrives. Gives the operator a single-grep target for proving the
    loop completed end-to-end.

    Codex round 2 finding 2: the terminated-event payload does not
    natively carry ``boot_id``; we correlate via the in-process
    ``boot_id`` captured at boot-probe emission time. The subscriber
    matches the FIRST terminated event for workflow_id=loop_health
    after registration (one per boot is the expected cadence).
    """
    _completed = {"fired": False}

    async def _on_flush(batch):
        if _completed["fired"]:
            return  # one log per boot
        for event in batch:
            if event.event_type != "workflow.execution_terminated":
                continue
            if event.instance_id != instance_id:
                continue
            payload = event.payload or {}
            if payload.get("workflow_id") != "loop_health":
                continue
            outcome = payload.get("outcome", "")
            logger.info(
                "LOOP_HEALTH_EXECUTION_COMPLETED boot_id=%s "
                "workflow_id=loop_health outcome=%s instance_id=%s",
                boot_id, outcome, instance_id,
            )
            _completed["fired"] = True
            return

    try:
        # Codex round 3: actual event-stream API is
        # ``register_post_flush_hook`` (event_stream.py:87), not
        # ``add_post_flush_hook``. Calling the wrong name made the
        # AttributeError swallow path the silent default.
        event_stream.register_post_flush_hook(_on_flush)
    except AttributeError:
        logger.warning(
            "LOOP_HEALTH_COMPLETION_LOGGER_UNAVAILABLE: event_stream "
            "lacks register_post_flush_hook; LOOP_HEALTH_EXECUTION_COMPLETED "
            "will not log. Operator can still query the event-stream DB "
            "for workflow.execution_terminated entries with "
            "workflow_id=loop_health."
        )


__all__ = [
    "register_loop_health_workflow",
    "emit_boot_probe",
    "register_completion_logger",
    "_generate_boot_id",
]
