"""Cohort descriptor + per-turn context + fan-out result.

Per the COHORT-FAN-OUT-RUNNER spec:

- `CohortDescriptor` (Section 1) — registration record. Declares
  the cohort's id, run callable, per-cohort timeout, default
  visibility, required-flag, safety-class flag, and execution
  mode. the design review edit #2 added `execution_mode`; v1 only accepts
  `async`. Sync callables are rejected at registration (the design review edit
  #1).

- `CohortContext` (Section 3) — bundle of per-turn inputs the
  fan-out runner hands each cohort. Cohorts that need additional
  state (e.g. canvas) read it via their own access patterns; the
  runner does not pre-fetch cohort-specific dependencies.

- `CohortFanOutResult` (Section 7) — runner output. List of
  CohortOutputs in registration order plus telemetry.

- `ExecutionMode` (the design review edit #2) — only `ASYNC` accepted in v1;
  reserved values for future extensions (`THREAD` for bounded
  thread-pool offload of sync work) live as enum members so
  registration errors can name the future-spec landing zone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from kernos.kernel.integration.briefing import (
    CohortOutput,
    Public,
    Visibility,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CohortDescriptorError(ValueError):
    """Raised when a cohort descriptor fails validation at registration."""


# ---------------------------------------------------------------------------
# Execution mode
# ---------------------------------------------------------------------------


class ExecutionMode(str, Enum):
    """How a cohort's run callable is invoked.

    Per the design review edit #2: v1 ONLY accepts `async`. Synchronous and
    blocking work must explicitly offload via `loop.run_in_executor`
    inside an async run callable. The `THREAD` enum value is
    reserved for a future spec (bounded-executor isolation); it
    exists so the registry can produce a clear error pointing to
    the future-spec landing zone rather than a generic "unsupported
    value" message.
    """

    ASYNC = "async"
    THREAD = "thread"  # reserved for future spec


# ---------------------------------------------------------------------------
# Per-turn context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    """One turn in the conversation thread.

    Light wrapper to keep CohortContext.conversation_thread typed
    rather than `list[dict]`. Maps trivially to API-message format
    (role/content) so cohorts that just want to forward the thread
    to a model don't have to translate.
    """

    role: str
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role.strip():
            raise CohortDescriptorError("Turn.role must be a non-empty string")
        if not isinstance(self.content, str):
            raise CohortDescriptorError("Turn.content must be a string")

    def to_api_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class ContextSpaceRef:
    """Reference to an active context space.

    `space_id` is the stable identifier; `domain` is operator-set
    metadata (e.g. "work", "personal", "general"). Cohorts that
    need to know which space is active read this; cohorts that
    operate space-blind can ignore.
    """

    space_id: str
    domain: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.space_id, str) or not self.space_id.strip():
            raise CohortDescriptorError(
                "ContextSpaceRef.space_id must be a non-empty string"
            )
        if not isinstance(self.domain, str):
            raise CohortDescriptorError(
                "ContextSpaceRef.domain must be a string"
            )


@dataclass(frozen=True)
class CohortContext:
    """Per-turn input to each cohort.

    Built by the fan-out runner once per turn and reused for all
    cohorts firing that turn. Frozen so cohorts can't accidentally
    mutate shared state.
    """

    member_id: str
    user_message: str
    conversation_thread: tuple[Turn, ...]
    active_spaces: tuple[ContextSpaceRef, ...]
    turn_id: str
    instance_id: str = ""
    produced_at: str = ""  # ISO 8601 UTC; turn start

    def __post_init__(self) -> None:
        if not isinstance(self.member_id, str) or not self.member_id.strip():
            raise CohortDescriptorError(
                "CohortContext.member_id must be a non-empty string"
            )
        if not isinstance(self.user_message, str):
            raise CohortDescriptorError(
                "CohortContext.user_message must be a string"
            )
        if not isinstance(self.conversation_thread, tuple):
            raise CohortDescriptorError(
                "CohortContext.conversation_thread must be a tuple of Turn"
            )
        for t in self.conversation_thread:
            if not isinstance(t, Turn):
                raise CohortDescriptorError(
                    f"CohortContext.conversation_thread entries must be "
                    f"Turn; got {type(t).__name__}"
                )
        if not isinstance(self.active_spaces, tuple):
            raise CohortDescriptorError(
                "CohortContext.active_spaces must be a tuple of ContextSpaceRef"
            )
        for s in self.active_spaces:
            if not isinstance(s, ContextSpaceRef):
                raise CohortDescriptorError(
                    f"CohortContext.active_spaces entries must be "
                    f"ContextSpaceRef; got {type(s).__name__}"
                )
        if not isinstance(self.turn_id, str) or not self.turn_id.strip():
            raise CohortDescriptorError(
                "CohortContext.turn_id must be a non-empty string"
            )
        if not isinstance(self.produced_at, str):
            raise CohortDescriptorError(
                "CohortContext.produced_at must be an ISO 8601 string"
            )


# ---------------------------------------------------------------------------
# Cohort descriptor
# ---------------------------------------------------------------------------


# A cohort's run callable must be async (returns Awaitable[CohortOutput])
# in v1. The type annotation accepts the broader Callable shape so the
# registry can detect and reject sync callables with a clear error.
CohortRunCallable = Callable[[CohortContext], Awaitable[CohortOutput]]


@dataclass(frozen=True)
class CohortDescriptor:
    """Cohort registration record.

    Per Section 1 of the COHORT-FAN-OUT-RUNNER spec:

      - `cohort_id` — matches the source_type prefix in V1's
        CohortOutput taxonomy ("memory", "weather", "gardener", …).
        Snake-case alphanumeric.

      - `run` — async callable `(CohortContext) -> CohortOutput`.
        Sync callables are rejected at registration (the design review edit #1);
        the registry validates `inspect.iscoroutinefunction`.

      - `timeout_ms` — per-cohort wall-clock budget. The runner
        wraps each cohort's task in `asyncio.wait_for(..., timeout)`.

      - `default_visibility` — applied to the cohort's CohortOutput
        if the cohort doesn't specify one. Cohorts producing
        secret-shaped material (covenant, hidden memory) should
        register with `default_visibility=Restricted(...)`.

      - `required` — if True, a failure or timeout marks the turn's
        fan-out as degraded; integration's filter phase reads the
        flag to apply downstream policy (Section 8).

      - `safety_class` — orthogonal to `required`. When True AND
        `required` is True, integration treats the failure as
        safety-degraded and defaults to `constrained_response` or
        `defer` rather than `respond_only`.

      - `execution_mode` — only ASYNC accepted in v1. The enum's
        existence reserves THREAD for a future spec.
    """

    cohort_id: str
    run: CohortRunCallable
    timeout_ms: int = 1000
    default_visibility: Visibility = field(default_factory=Public)
    required: bool = False
    safety_class: bool = False
    execution_mode: ExecutionMode = ExecutionMode.ASYNC


# ---------------------------------------------------------------------------
# Fan-out result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CohortFanOutResult:
    """Runner output for a fan-out invocation.

    Per Section 7. Integration consumes `outputs`; the other fields
    feed integration's audit_trace and BudgetState (Section 8).

    `outputs` length always equals the number of registered cohorts.
    Failed cohorts get synthetic CohortOutputs (output={},
    outcome != SUCCESS, error_summary populated and redacted) so
    callers can iterate uniformly.
    """

    outputs: tuple[CohortOutput, ...]
    fan_out_started_at: str  # ISO 8601 UTC
    fan_out_completed_at: str  # ISO 8601 UTC
    global_timeout_engaged: bool = False
    required_cohort_failures: tuple[str, ...] = ()
    required_safety_cohort_failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.outputs, tuple):
            raise CohortDescriptorError(
                "CohortFanOutResult.outputs must be a tuple of CohortOutput"
            )
        for o in self.outputs:
            if not isinstance(o, CohortOutput):
                raise CohortDescriptorError(
                    f"CohortFanOutResult.outputs entries must be CohortOutput; "
                    f"got {type(o).__name__}"
                )
        for ref in self.required_cohort_failures:
            if not isinstance(ref, str) or not ref.strip():
                raise CohortDescriptorError(
                    "CohortFanOutResult.required_cohort_failures entries "
                    "must be non-empty strings"
                )
        for ref in self.required_safety_cohort_failures:
            if not isinstance(ref, str) or not ref.strip():
                raise CohortDescriptorError(
                    "CohortFanOutResult.required_safety_cohort_failures "
                    "entries must be non-empty strings"
                )
        if not isinstance(self.global_timeout_engaged, bool):
            raise CohortDescriptorError(
                "CohortFanOutResult.global_timeout_engaged must be a bool"
            )

    @property
    def degraded(self) -> bool:
        """At least one required cohort failed."""
        return bool(self.required_cohort_failures)

    @property
    def safety_degraded(self) -> bool:
        """At least one required safety_class cohort failed."""
        return bool(self.required_safety_cohort_failures)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outputs": [o.to_dict() for o in self.outputs],
            "fan_out_started_at": self.fan_out_started_at,
            "fan_out_completed_at": self.fan_out_completed_at,
            "global_timeout_engaged": self.global_timeout_engaged,
            "required_cohort_failures": list(self.required_cohort_failures),
            "required_safety_cohort_failures": list(
                self.required_safety_cohort_failures
            ),
        }


__all__ = [
    "CohortContext",
    "CohortDescriptor",
    "CohortDescriptorError",
    "CohortFanOutResult",
    "CohortRunCallable",
    "ContextSpaceRef",
    "ExecutionMode",
    "Turn",
]
