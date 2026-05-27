"""Probe 8 — loop_health_completion_invariant (SUBSTRATE-SELF-TEST-V1).

Asserts the loop_health helper's emit_boot_probe + completion-
subscriber contract holds: emit_boot_probe sends a
loop_health.boot_probe event with a boot_id; the post-flush
hook registered by register_completion_logger fires when a
matching workflow.execution_terminated event arrives.

Closes a gap from the parent KERNOS-AUTONOMOUS-IMPROVEMENT-
LOOP-V1 spec that called for end-to-end completion within 30s
of boot but never landed as a probe.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from kernos.kernel.self_test_gate import ProbeResult


REQUIRED_BEHAVIORAL_KEYS = frozenset({
    "boot_probe_emitted",
    "execution_completed",
    "elapsed_seconds",
})

REQUIRED_SUBSTRATE_KEYS = frozenset({
    "boot_probe_event_payload",
    "completion_log_seen",
    "workflow_execution_outcome",
})


@dataclass
class _FakeEvent:
    event_type: str
    instance_id: str
    payload: dict = field(default_factory=dict)


@dataclass
class _FakeEventStream:
    """Minimal event-stream double that supports emit() +
    register_post_flush_hook() — the two surfaces emit_boot_probe
    and register_completion_logger use."""
    emitted: list[tuple] = field(default_factory=list)
    hooks: list = field(default_factory=list)

    async def emit(self, instance_id, event_type, payload, *, space_id=""):
        self.emitted.append(
            (instance_id, event_type, payload, space_id),
        )

    def register_post_flush_hook(self, hook):
        self.hooks.append(hook)


async def run_probe() -> ProbeResult:
    start = time.monotonic()

    from kernos.kernel.workflows.loop_health_helper import (
        emit_boot_probe,
        register_completion_logger,
    )

    stream = _FakeEventStream()
    instance_id = "probe8_test"

    # (1) emit_boot_probe should send a single event with type
    # loop_health.boot_probe and carry a boot_id.
    boot_id = await emit_boot_probe(
        instance_id=instance_id, event_stream=stream,
    )

    boot_probe_emitted = (
        len(stream.emitted) == 1
        and stream.emitted[0][1] == "loop_health.boot_probe"
        and stream.emitted[0][0] == instance_id
        and stream.emitted[0][2].get("boot_id") == boot_id
    )
    boot_probe_payload = (
        stream.emitted[0][2] if stream.emitted else {}
    )

    # (2) register_completion_logger should register a post-flush
    # hook against the same event_stream.
    completion_log_lines: list[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record):
            completion_log_lines.append(record.getMessage())

    helper_logger = logging.getLogger(
        "kernos.kernel.workflows.loop_health_helper",
    )
    handler = _CaptureHandler()
    handler.setLevel(logging.INFO)
    helper_logger.addHandler(handler)
    prev_level = helper_logger.level
    helper_logger.setLevel(logging.INFO)

    try:
        register_completion_logger(
            event_stream=stream,
            instance_id=instance_id,
            boot_id=boot_id,
        )

        # The hook should have been registered.
        hook_registered = len(stream.hooks) == 1

        # (3) Simulate the workflow.execution_terminated event
        # arriving via the post-flush hook. The hook should fire
        # LOOP_HEALTH_EXECUTION_COMPLETED log with our boot_id.
        if hook_registered:
            terminated_event = _FakeEvent(
                event_type="workflow.execution_terminated",
                instance_id=instance_id,
                payload={
                    "workflow_id": "loop_health",
                    "outcome": "completed",
                },
            )
            await stream.hooks[0]([terminated_event])
    finally:
        helper_logger.removeHandler(handler)
        helper_logger.setLevel(prev_level)

    completion_log_seen = any(
        "LOOP_HEALTH_EXECUTION_COMPLETED" in line
        and f"boot_id={boot_id}" in line
        and "outcome=completed" in line
        for line in completion_log_lines
    )

    duration_ms = int((time.monotonic() - start) * 1000)
    elapsed_seconds = duration_ms / 1000.0

    cond_emitted = boot_probe_emitted
    cond_completion = completion_log_seen
    cond_timing = elapsed_seconds < 30

    all_passed = cond_emitted and cond_completion and cond_timing

    failure_reason = ""
    if not all_passed:
        failed = []
        if not cond_emitted:
            failed.append(
                f"boot_probe_not_emitted_correctly "
                f"(emitted={len(stream.emitted)} events)"
            )
        if not cond_completion:
            failed.append(
                "completion_hook_did_not_fire_for_matching_event"
            )
        if not cond_timing:
            failed.append(
                f"elapsed_too_long ({elapsed_seconds:.1f}s > 30s)"
            )
        failure_reason = (
            f"loop-health-completion invariant violated: "
            f"{', '.join(failed)}. Likely regression of "
            f"emit_boot_probe or register_completion_logger contract."
        )

    return ProbeResult(
        probe_name="loop_health_completion_invariant",
        passed=all_passed,
        behavioral_evidence={
            "boot_probe_emitted": boot_probe_emitted,
            "execution_completed": completion_log_seen,
            "elapsed_seconds": elapsed_seconds,
        },
        substrate_evidence={
            "boot_probe_event_payload": boot_probe_payload,
            "completion_log_seen": completion_log_seen,
            "workflow_execution_outcome": (
                "completed" if completion_log_seen
                else "NOT_OBSERVED"
            ),
        },
        duration_ms=duration_ms,
        failure_reason=failure_reason,
    )
