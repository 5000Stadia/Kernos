"""Cohort fan-out runner.

Per the COHORT-FAN-OUT-RUNNER spec:

- Section 4 (fan-out execution model) — `asyncio.wait` with explicit
  task bookkeeping per Kit edit #3 (NOT `asyncio.gather`); per-cohort
  timeout via `asyncio.wait_for`; global wall-clock budget caps the
  whole run; pending cohorts past the cap are cancelled.

- Section 5a — `cohort_run_id` minted by the runner deterministically
  from `turn_id + cohort_id + sequence`. Cohorts cannot mint their
  own.

- Section 5 — synthetic CohortOutputs for failure paths so the
  result-list shape is invariant: every registered cohort produces
  exactly one CohortOutput, never zero. Synthetic outputs use
  `output: {}` (empty dict); `outcome` carries the failure cause.

- Section 6 (failure isolation, narrowed) — async-task-per-cohort
  isolates yielding coroutines from each other. The runner cannot
  isolate against synchronous infinite loops, CPU-bound work, or
  blocking I/O without await. Sync callables are rejected at
  registration (registry.py).

- Section 10 — fan-out runs emit an audit entry under category
  `cohort.fan_out` with per-cohort outcome + timing.

The runner is opt-in callable (acceptance criterion #13). Nothing in
the existing reasoning loop or message handler invokes it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from kernos.kernel.cohorts.descriptor import (
    CohortContext,
    CohortDescriptor,
    CohortFanOutResult,
)
from kernos.kernel.cohorts.redaction import sanitize_exception
from kernos.kernel.cohorts.registry import CohortRegistry
from kernos.kernel.integration.briefing import (
    CohortOutput,
    Outcome,
    Public,
    Restricted,
    Visibility,
)
from kernos.utils import utc_now


logger = logging.getLogger(__name__)


# Audit emit callback shape matches V1's: receives a dict; subsequent
# specs wire to the existing tool_audit substrate.
AuditEmitter = Callable[[dict[str, Any]], Awaitable[None]]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CohortFanOutConfig:
    """Tunables for the fan-out runner.

    `global_timeout_seconds` caps the entire wall-clock duration of
    the fan-out (Kit edit #6). When the cap is hit, any cohorts
    still pending get cancelled and produce synthetic
    `Outcome.TIMEOUT_GLOBAL` outputs.

    `cancellation_drain_seconds` bounds how long the runner waits
    for cancelled tasks to finish settling before returning. Real
    cooperative coroutines settle in milliseconds; this exists to
    prevent a pathological cohort from blocking the runner's
    return.
    """

    global_timeout_seconds: float = 5.0
    cancellation_drain_seconds: float = 1.0


# ---------------------------------------------------------------------------
# Per-task result envelope
# ---------------------------------------------------------------------------


@dataclass
class _TaskOutcome:
    """Internal per-task outcome the runner reconstructs into a
    CohortOutput. Carries enough metadata for both the synthetic
    output construction and the audit-log entry."""

    outcome: Outcome
    duration_ms: int
    cohort_output: CohortOutput | None = None  # populated on success
    error_summary: str = ""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class CohortFanOutRunner:
    """Fires every registered cohort in parallel; collects outputs.

    Construction:
        runner = CohortFanOutRunner(
            registry=my_registry,
            audit_emitter=emit_callback,
            config=CohortFanOutConfig(global_timeout_seconds=5.0),
        )

    Use:
        result = await runner.run(cohort_context)
        # result.outputs has one CohortOutput per registered cohort,
        # in registration order, success or synthetic.
    """

    def __init__(
        self,
        *,
        registry: CohortRegistry,
        audit_emitter: AuditEmitter,
        config: CohortFanOutConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._audit_emitter = audit_emitter
        self._config = config or CohortFanOutConfig()
        self._clock = clock

    async def run(self, context: CohortContext) -> CohortFanOutResult:
        cohorts = self._registry.list_cohorts()
        started_iso = utc_now()

        if not cohorts:
            completed_iso = utc_now()
            result = CohortFanOutResult(
                outputs=(),
                fan_out_started_at=started_iso,
                fan_out_completed_at=completed_iso,
            )
            await self._emit_audit(
                context=context,
                cohorts=cohorts,
                outcomes=[],
                result=result,
            )
            return result

        # Spawn one task per cohort. Each task wraps the cohort's run
        # callable in `asyncio.wait_for` to enforce the per-cohort
        # timeout; exceptions inside the cohort surface to the task
        # for outer-level reconstruction.
        tasks: dict[asyncio.Task, CohortDescriptor] = {}
        for desc in cohorts:
            task = asyncio.create_task(
                self._run_one_cohort(desc, context),
                name=f"cohort:{desc.cohort_id}",
            )
            tasks[task] = desc

        # Global wall-clock cap. asyncio.wait with explicit
        # bookkeeping (Kit edit #3) — not asyncio.gather. wait gives
        # us partial completion + ordered reconstruction.
        global_timeout = self._config.global_timeout_seconds
        done, pending = await asyncio.wait(
            list(tasks.keys()),
            timeout=global_timeout if global_timeout > 0 else None,
            return_when=asyncio.ALL_COMPLETED,
        )

        global_timeout_engaged = bool(pending)
        if pending:
            for task in pending:
                task.cancel()
            # Drain cancellations bounded by config so we always
            # return in finite time.
            try:
                await asyncio.wait(
                    pending,
                    timeout=self._config.cancellation_drain_seconds,
                )
            except Exception:  # pragma: no cover
                logger.exception("cohort cancellation drain raised")

        # Reconstruct outputs in registration order.
        outputs: list[CohortOutput] = []
        per_cohort_outcomes: list[_TaskOutcome] = []
        sequence = 0  # reserved for future stateful/multi-fire cohorts
        task_by_descriptor: dict[str, asyncio.Task] = {
            desc.cohort_id: t for t, desc in tasks.items()
        }

        for desc in cohorts:
            task = task_by_descriptor[desc.cohort_id]
            run_id = _mint_cohort_run_id(context.turn_id, desc.cohort_id, sequence)
            task_outcome = self._extract_task_outcome(
                desc=desc,
                task=task,
                run_id=run_id,
                context=context,
                global_timeout_engaged=global_timeout_engaged,
            )
            per_cohort_outcomes.append(task_outcome)

            if task_outcome.cohort_output is not None:
                outputs.append(task_outcome.cohort_output)
            else:
                # Synthesize a CohortOutput for the failure case.
                outputs.append(
                    CohortOutput(
                        cohort_id=desc.cohort_id,
                        cohort_run_id=run_id,
                        output={},
                        visibility=desc.default_visibility,
                        produced_at=utc_now(),
                        outcome=task_outcome.outcome,
                        error_summary=task_outcome.error_summary,
                    )
                )

        required_failures = tuple(
            desc.cohort_id
            for desc, out in zip(cohorts, outputs)
            if desc.required and out.outcome is not Outcome.SUCCESS
        )
        required_safety_failures = tuple(
            desc.cohort_id
            for desc, out in zip(cohorts, outputs)
            if desc.required
            and desc.safety_class
            and out.outcome is not Outcome.SUCCESS
        )

        completed_iso = utc_now()
        result = CohortFanOutResult(
            outputs=tuple(outputs),
            fan_out_started_at=started_iso,
            fan_out_completed_at=completed_iso,
            global_timeout_engaged=global_timeout_engaged,
            required_cohort_failures=required_failures,
            required_safety_cohort_failures=required_safety_failures,
        )

        await self._emit_audit(
            context=context,
            cohorts=cohorts,
            outcomes=per_cohort_outcomes,
            result=result,
        )
        return result

    # ----- per-cohort task -----

    async def _run_one_cohort(
        self, desc: CohortDescriptor, context: CohortContext
    ) -> tuple[Outcome, int, CohortOutput | None, str]:
        """Run one cohort and return a tuple the outer level decodes.

        Returns (outcome, duration_ms, cohort_output_or_None,
        error_summary). The outer level reconstructs a final
        CohortOutput; this layer just packages.

        Per-cohort timeout: `asyncio.wait_for` raises TimeoutError
        which we catch and return as TIMEOUT_PER_COHORT. Other
        exceptions get caught and returned as ERROR with redacted
        error_summary. CancelledError is NOT caught — it
        propagates so the outer-level cancel() from a global
        timeout is observed via task.cancelled().
        """
        started = self._clock()
        timeout_seconds = max(0.001, desc.timeout_ms / 1000.0)
        try:
            raw = await asyncio.wait_for(
                desc.run(context), timeout=timeout_seconds
            )
        except asyncio.CancelledError:
            # Propagate so the outer-level cancel() is observable.
            raise
        except asyncio.TimeoutError:
            return (
                Outcome.TIMEOUT_PER_COHORT,
                _ms_since(started, self._clock),
                None,
                f"cohort exceeded per-cohort timeout {desc.timeout_ms}ms",
            )
        except Exception as exc:
            return (
                Outcome.ERROR,
                _ms_since(started, self._clock),
                None,
                sanitize_exception(exc),
            )

        # Validate that the cohort returned the contract type.
        if not isinstance(raw, CohortOutput):
            return (
                Outcome.ERROR,
                _ms_since(started, self._clock),
                None,
                sanitize_exception(
                    TypeError(
                        f"cohort {desc.cohort_id!r} returned "
                        f"{type(raw).__name__}; expected CohortOutput"
                    )
                ),
            )

        # Mint cohort_run_id at the outer level — cohorts cannot mint
        # their own (Section 5a). If the cohort populated
        # cohort_run_id with a value, we replace it with the runner's
        # deterministic mint at finalize-time (the outer level does
        # the actual mint; we just signal success and pass the raw
        # output back).
        return (
            Outcome.SUCCESS,
            _ms_since(started, self._clock),
            raw,
            "",
        )

    def _extract_task_outcome(
        self,
        *,
        desc: CohortDescriptor,
        task: asyncio.Task,
        run_id: str,
        context: CohortContext,
        global_timeout_engaged: bool,
    ) -> _TaskOutcome:
        # Cancelled task → global timeout (in v1, the only cancel
        # source is the runner's global cap).
        if task.cancelled():
            return _TaskOutcome(
                outcome=Outcome.TIMEOUT_GLOBAL,
                duration_ms=0,  # task didn't produce timing
                error_summary=(
                    "cohort cancelled — fan-out global wall-clock cap "
                    "exceeded"
                ),
            )

        exc = task.exception()
        if exc is not None:
            # The task raised something we didn't expect (e.g.,
            # CancelledError mid-flight before our return path
            # caught it, or a wait_for timeout that escaped). Fall
            # back to ERROR with sanitized class+message.
            return _TaskOutcome(
                outcome=Outcome.ERROR,
                duration_ms=0,
                error_summary=sanitize_exception(exc),
            )

        # Normal return: tuple from _run_one_cohort.
        outcome, duration_ms, cohort_output, error_summary = task.result()

        if outcome is Outcome.SUCCESS and cohort_output is not None:
            # Mint cohort_run_id deterministically here — Section 5a.
            # Override whatever the cohort might have set.
            normalised = CohortOutput(
                cohort_id=desc.cohort_id,
                cohort_run_id=run_id,
                output=cohort_output.output,
                visibility=cohort_output.visibility,
                produced_at=cohort_output.produced_at or utc_now(),
                outcome=Outcome.SUCCESS,
                error_summary="",
            )
            return _TaskOutcome(
                outcome=Outcome.SUCCESS,
                duration_ms=duration_ms,
                cohort_output=normalised,
            )

        return _TaskOutcome(
            outcome=outcome,
            duration_ms=duration_ms,
            error_summary=error_summary,
        )

    # ----- audit -----

    async def _emit_audit(
        self,
        *,
        context: CohortContext,
        cohorts: tuple[CohortDescriptor, ...],
        outcomes: list[_TaskOutcome],
        result: CohortFanOutResult,
    ) -> None:
        # Per Section 10: member-scoped, per-cohort outcome + timing.
        try:
            audit_entry = {
                "audit_category": "cohort.fan_out",
                "instance_id": context.instance_id,
                "member_id": context.member_id,
                "turn_id": context.turn_id,
                "fan_out_started_at": result.fan_out_started_at,
                "fan_out_completed_at": result.fan_out_completed_at,
                "global_timeout_engaged": result.global_timeout_engaged,
                "registered_cohort_ids": [d.cohort_id for d in cohorts],
                "outcomes": [
                    {
                        "cohort_id": result.outputs[i].cohort_id,
                        "cohort_run_id": result.outputs[i].cohort_run_id,
                        "outcome": result.outputs[i].outcome.value,
                        "duration_ms": outcomes[i].duration_ms,
                        "error_summary": result.outputs[i].error_summary,
                    }
                    for i in range(len(result.outputs))
                ],
                "required_cohort_failures": list(
                    result.required_cohort_failures
                ),
                "required_safety_cohort_failures": list(
                    result.required_safety_cohort_failures
                ),
            }
            await self._audit_emitter(audit_entry)
        except Exception:  # pragma: no cover
            # Audit emission is best-effort, in line with the
            # kernel convention. Never fail the fan-out on an
            # audit write.
            logger.exception("cohort.fan_out audit emit failed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mint_cohort_run_id(turn_id: str, cohort_id: str, sequence: int) -> str:
    """Deterministic cohort_run_id format per Section 5a.

    `{turn_id}:{cohort_id}:{sequence}`. The sequence counter is
    reserved for future stateful or multi-fire cohorts; effectively
    always 0 in v1.
    """
    return f"{turn_id}:{cohort_id}:{sequence}"


def _ms_since(start: float, clock: Callable[[], float]) -> int:
    return max(0, int((clock() - start) * 1000))


def build_integration_inputs_from_fan_out(
    fan_out_result,  # CohortFanOutResult — typed lazily to avoid import cycle
    *,
    user_message: str,
    conversation_thread: tuple,
    surfaced_tools: tuple = (),
    active_context_spaces: tuple = (),
    member_id: str,
    instance_id: str,
    space_id: str,
    turn_id: str,
    cognitive_context=None,
):
    """Construct IntegrationInputs from a CohortFanOutResult.

    Per COHORT-ADAPT-COVENANT Section 2c: this is the boundary
    where required_safety_cohort_failures crosses from the
    fan-out runner's CohortFanOutResult into V1's IntegrationInputs.
    The integration runner's filter phase reads the field and
    enforces the safety policy (defer / constrained_response only).

    Wiring callers (test fixtures today, INTEGRATION-WIRE-LIVE in
    production) should use this helper rather than constructing
    IntegrationInputs by hand so the safety-policy plumbing stays
    consistent across call sites.

    COGNITIVE-CONTEXT-V1 C3a: optional ``cognitive_context`` arg
    threads the typed packet from TurnRunnerInputs through into
    IntegrationInputs, where the integration runner copies it onto
    Briefing for the renderer to consume.
    """
    from kernos.kernel.integration.runner import IntegrationInputs

    return IntegrationInputs(
        user_message=user_message,
        conversation_thread=conversation_thread,
        cohort_outputs=fan_out_result.outputs,
        surfaced_tools=surfaced_tools,
        active_context_spaces=active_context_spaces,
        member_id=member_id,
        instance_id=instance_id,
        space_id=space_id,
        turn_id=turn_id,
        required_safety_cohort_failures=tuple(
            fan_out_result.required_safety_cohort_failures
        ),
        cognitive_context=cognitive_context,
    )


__all__ = [
    "AuditEmitter",
    "CohortFanOutConfig",
    "CohortFanOutRunner",
    "build_integration_inputs_from_fan_out",
]
