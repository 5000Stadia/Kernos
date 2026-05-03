"""Cohort registry.

Per Section 2 of the COHORT-FAN-OUT-RUNNER spec: design review-controlled,
not user-extensible (matches V1's deferral of dynamic user-built
cohorts). Cohorts register at boot via explicit calls; the runner
consumes the registered list.

Per the design review edit #1: sync callables are rejected at registration with a
clear error pointing at the requirement. The runner's failure-
isolation guarantee only holds for cooperative coroutines, so v1
refuses anything else at the boundary.

Per the design review edit #2: `execution_mode` field on the descriptor; only
`ASYNC` accepted in v1. `THREAD` is reserved for a future spec; the
registry produces a clear error pointing at that future-spec
landing zone rather than a generic value error.
"""

from __future__ import annotations

import inspect
import logging
import re
from typing import Iterable

from kernos.kernel.cohorts.descriptor import (
    CohortDescriptor,
    CohortDescriptorError,
    ExecutionMode,
)


logger = logging.getLogger(__name__)


_COHORT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class CohortRegistry:
    """Design Review-controlled cohort registry.

    The fan-out runner asks the registry for the registered list at
    invocation time and fires every member in parallel. Registration
    order is preserved — `CohortFanOutResult.outputs` reflects it.
    """

    def __init__(self) -> None:
        self._cohorts: list[CohortDescriptor] = []
        self._ids: set[str] = set()

    def register(self, descriptor: CohortDescriptor) -> None:
        """Register a cohort. Validates the design review edit #1/#2 + uniqueness.

        Raises:
            CohortDescriptorError on validation failure.
        """
        if not isinstance(descriptor, CohortDescriptor):
            raise CohortDescriptorError(
                f"register expected a CohortDescriptor; got "
                f"{type(descriptor).__name__}"
            )

        if not isinstance(descriptor.cohort_id, str) or not _COHORT_ID_RE.match(
            descriptor.cohort_id
        ):
            raise CohortDescriptorError(
                f"cohort_id {descriptor.cohort_id!r} must be snake_case "
                f"(lowercase letters, digits, underscores; starting with a "
                f"letter)"
            )

        if descriptor.cohort_id in self._ids:
            raise CohortDescriptorError(
                f"cohort {descriptor.cohort_id!r} is already registered"
            )

        if not isinstance(descriptor.timeout_ms, int) or descriptor.timeout_ms <= 0:
            raise CohortDescriptorError(
                f"cohort {descriptor.cohort_id!r}: timeout_ms must be a "
                f"positive int (milliseconds); got {descriptor.timeout_ms!r}"
            )

        if descriptor.execution_mode is not ExecutionMode.ASYNC:
            if descriptor.execution_mode is ExecutionMode.THREAD:
                raise CohortDescriptorError(
                    f"cohort {descriptor.cohort_id!r}: execution_mode "
                    f"'thread' is reserved for a future spec "
                    f"(bounded-executor isolation). v1 only accepts 'async'. "
                    f"If your cohort wraps sync work, offload it explicitly "
                    f"via loop.run_in_executor inside an async run callable."
                )
            raise CohortDescriptorError(
                f"cohort {descriptor.cohort_id!r}: execution_mode "
                f"{descriptor.execution_mode!r} is not supported in v1; "
                f"use ExecutionMode.ASYNC"
            )

        if not callable(descriptor.run):
            raise CohortDescriptorError(
                f"cohort {descriptor.cohort_id!r}: run must be a callable; "
                f"got {type(descriptor.run).__name__}"
            )

        if not inspect.iscoroutinefunction(descriptor.run):
            # Catch async callables wrapped behind a thin sync facade
            # by inspecting `inspect.unwrap` first. The check is
            # iscoroutinefunction on the unwrapped target. Anything
            # not async is rejected per the design review edit #1.
            unwrapped = inspect.unwrap(descriptor.run)
            if not inspect.iscoroutinefunction(unwrapped):
                raise CohortDescriptorError(
                    f"cohort {descriptor.cohort_id!r}: run callable must be "
                    f"async (defined with `async def` or returning an "
                    f"Awaitable). Synchronous callables are rejected at "
                    f"registration because the fan-out runner's failure-"
                    f"isolation guarantee only holds for cooperative "
                    f"coroutines. If your cohort wraps blocking work, "
                    f"offload it via loop.run_in_executor inside an async "
                    f"run callable. See docs/architecture/cohort-fan-out.md."
                )

        self._cohorts.append(descriptor)
        self._ids.add(descriptor.cohort_id)
        logger.debug(
            "COHORT_REGISTERED: id=%s required=%s safety_class=%s timeout_ms=%d",
            descriptor.cohort_id,
            descriptor.required,
            descriptor.safety_class,
            descriptor.timeout_ms,
        )

    def has(self, cohort_id: str) -> bool:
        return cohort_id in self._ids

    def get(self, cohort_id: str) -> CohortDescriptor:
        for d in self._cohorts:
            if d.cohort_id == cohort_id:
                return d
        raise CohortDescriptorError(
            f"cohort {cohort_id!r} is not registered"
        )

    def list_cohorts(self) -> tuple[CohortDescriptor, ...]:
        """Return registered cohorts in registration order."""
        return tuple(self._cohorts)

    def __len__(self) -> int:
        return len(self._cohorts)

    def __iter__(self) -> Iterable[CohortDescriptor]:
        return iter(self._cohorts)


__all__ = ["CohortRegistry"]
