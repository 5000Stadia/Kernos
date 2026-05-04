"""Per-turn coordinator for the decoupled reasoning pipeline.

Introduced by PRESENCE-DECOUPLING-INTRODUCE C2. The TurnRunner is the
orchestration shell sitting above the four cognition layers:

    cohort fan-out → IntegrationService → EnactmentService → response

It owns nothing except the orchestration. Domain logic lives in the
services it composes:

  - cohort fan-out: existing CohortFanOutRunner produces a
    CohortFanOutResult with required_safety_cohort_failures metadata.
  - IntegrationService (lands in C3): consumes the fan-out result
    plus conversation thread plus surfaced read-only tool catalog
    and produces a Briefing.
  - EnactmentService (lands in C4-C6): consumes the Briefing and
    routes between thin path (render-only) and full machinery
    (plan + tier hierarchy + envelope validation).
  - Response delivery: translates the enactment outcome back into a
    ReasoningResult so the existing message-handler call sites are
    unchanged.

C2 scope: skeleton + feature-flag routing. The services are typed via
Protocols so they can be stubbed for tests; concrete classes wire in
later commits.

CCV1 C7 strike (2026-05-03): the legacy reasoning loop has been
removed; thin path is THE path. The KERNOS_USE_DECOUPLED_TURN_RUNNER
feature flag is gone. ReasoningService.reason() always routes through
the TurnRunner provider per the construction contract.

Acceptance criterion 7 (the design review edit) — load-bearing seam:
required_safety_cohort_failures must flow TurnRunner →
IntegrationService at the boundary, not just be representable on the
runner. The TurnRunner uses build_integration_inputs_from_fan_out so
the plumbing is consistent across call sites.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from kernos.kernel.cohorts.descriptor import (
    CohortContext,
    CohortFanOutResult,
    ContextSpaceRef,
    Turn,
)
from kernos.kernel.cohorts.runner import (
    CohortFanOutRunner,
    build_integration_inputs_from_fan_out,
)
from kernos.kernel.integration.briefing import Briefing
from kernos.kernel.integration.runner import IntegrationInputs


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Service protocols (concrete classes live in enactment + integration)
# ---------------------------------------------------------------------------


@runtime_checkable
class IntegrationServiceLike(Protocol):
    """Protocol for the IntegrationService introduced in C3.

    The TurnRunner depends on this shape, not on the concrete class,
    so C2's skeleton can be tested with mocks ahead of C3's full
    extraction. The concrete IntegrationService will conform to this
    Protocol; tests can wire stubs.
    """

    async def run(self, inputs: IntegrationInputs) -> Briefing: ...


@runtime_checkable
class EnactmentServiceLike(Protocol):
    """Protocol for the EnactmentService introduced in C4-C6.

    Returns whatever shape the enactment service settles on. C2 uses
    `Any` here so the Protocol stays loose until C4 lands the concrete
    return type. The TurnRunner translates the enactment outcome into
    a ReasoningResult; the translation seam is the only place the
    return type is consumed.
    """

    async def run(self, briefing: Briefing) -> Any: ...


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnRunnerInputs:
    """Inputs to TurnRunner.run_turn.

    The shape mirrors what the message handler hands ReasoningService
    today, but structured so the orchestration can lift fields out
    without re-parsing strings or guessing.

    `cohort_thread` is a tuple of cohort-Turn (role/content); a
    convenience for callers that already have API-message dicts is
    provided via `from_api_messages`.

    `active_spaces` is the cohort-shaped tuple of ContextSpaceRef.

    `integration_thread` is the API-message-format conversation thread
    integration consumes. Kept distinct from cohort_thread because the
    two consumers want subtly different shapes (Turn vs dict[str, Any]).

    `integration_active_spaces` is integration's looser dict shape
    for active context spaces; cohort_active_spaces is the strongly
    typed cohort shape.
    """

    instance_id: str
    member_id: str
    space_id: str
    turn_id: str
    user_message: str
    cohort_thread: tuple[Turn, ...] = ()
    cohort_active_spaces: tuple[ContextSpaceRef, ...] = ()
    integration_thread: tuple[dict[str, Any], ...] = ()
    integration_active_spaces: tuple[dict[str, Any], ...] = ()
    surfaced_tools: tuple = ()
    produced_at: str = ""
    # COGNITIVE-CONTEXT-V1 C3a: typed cognitive substrate carried
    # alongside the existing inputs. The runner threads this into
    # IntegrationInputs so integration's runner (real or stub) can
    # copy it onto Briefing for the renderer. Optional + default-
    # None preserves backward compat for existing callers.
    cognitive_context: Any = None

    @staticmethod
    def from_api_messages(
        *,
        instance_id: str,
        member_id: str,
        space_id: str,
        turn_id: str,
        user_message: str,
        api_messages: tuple[dict[str, Any], ...] = (),
        active_space_ids: tuple[str, ...] = (),
        surfaced_tools: tuple = (),
        produced_at: str = "",
        cognitive_context: Any = None,
    ) -> "TurnRunnerInputs":
        """Build inputs from the API-message thread shape callers
        usually have on hand. Synthesises cohort-shaped Turn entries
        from API messages with a string content (skips multi-block
        content gracefully — cohorts read the thread for context, not
        for raw block playback)."""
        cohort_turns: list[Turn] = []
        for msg in api_messages:
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                cohort_turns.append(
                    Turn(role=str(msg.get("role", "")), content=content)
                )
        cohort_spaces = tuple(
            ContextSpaceRef(space_id=sid) for sid in active_space_ids if sid
        )
        integration_spaces = tuple(
            {"space_id": sid} for sid in active_space_ids if sid
        )
        return TurnRunnerInputs(
            instance_id=instance_id,
            member_id=member_id,
            space_id=space_id,
            turn_id=turn_id,
            user_message=user_message,
            cohort_thread=tuple(cohort_turns),
            cohort_active_spaces=cohort_spaces,
            integration_thread=tuple(api_messages),
            integration_active_spaces=integration_spaces,
            surfaced_tools=surfaced_tools,
            produced_at=produced_at,
            cognitive_context=cognitive_context,
        )


# ---------------------------------------------------------------------------
# TurnRunner
# ---------------------------------------------------------------------------


class TurnRunner:
    """Orchestrates the four-layer cognition pipeline for a single turn.

    Pure orchestration: cohort fan-out → IntegrationService →
    EnactmentService → response delivery. Each composed dependency is
    optional at construction so the skeleton can land before the real
    services do; missing dependencies raise clear errors at run time
    rather than producing degenerate output.
    """

    def __init__(
        self,
        *,
        cohort_runner: CohortFanOutRunner | None = None,
        integration_service: IntegrationServiceLike | None = None,
        enactment_service: EnactmentServiceLike | None = None,
        response_delivery: Callable[[Briefing, Any], Awaitable[Any]] | None = None,
    ) -> None:
        self._cohort_runner = cohort_runner
        self._integration_service = integration_service
        self._enactment_service = enactment_service
        self._response_delivery = response_delivery

    # ----- public entry points -----

    async def run_turn(self, inputs: TurnRunnerInputs) -> Any:
        """Run one turn end-to-end.

        Skeleton wiring: cohort fan-out, then integration, then
        enactment. The signature returns Any because EnactmentService's
        return type is finalised in C4; the response_delivery hook
        translates that into whatever shape the caller expects.

        Raises a clear `TurnRunnerNotWired` when a dependency is
        missing — surfacing the wiring gap loudly rather than letting
        the runner produce half-formed output.
        """
        fan_out = await self.run_cohort_fan_out(inputs)
        briefing = await self.run_integration(fan_out, inputs)
        outcome = await self.run_enactment(briefing)
        return await self.deliver(briefing, outcome)

    # ----- composable seams (each independently testable) -----

    async def run_cohort_fan_out(
        self, inputs: TurnRunnerInputs
    ) -> CohortFanOutResult:
        if self._cohort_runner is None:
            raise TurnRunnerNotWired(
                "TurnRunner.cohort_runner is not wired. Provide a "
                "CohortFanOutRunner at construction (see C2 wiring)."
            )
        ctx = CohortContext(
            instance_id=inputs.instance_id,
            member_id=inputs.member_id,
            user_message=inputs.user_message,
            conversation_thread=inputs.cohort_thread,
            active_spaces=inputs.cohort_active_spaces,
            turn_id=inputs.turn_id,
            produced_at=inputs.produced_at,
        )
        return await self._cohort_runner.run(ctx)

    async def run_integration(
        self,
        fan_out: CohortFanOutResult,
        inputs: TurnRunnerInputs,
    ) -> Briefing:
        """Build IntegrationInputs (threading required_safety_cohort_failures
        per acceptance criterion 7) and hand them to IntegrationService.

        This is the load-bearing seam the design review's edit asserts: the safety
        metadata MUST cross the boundary, not just be representable.
        We use build_integration_inputs_from_fan_out to keep the
        plumbing consistent with all other wiring sites.
        """
        if self._integration_service is None:
            raise TurnRunnerNotWired(
                "TurnRunner.integration_service is not wired. "
                "IntegrationService lands in PDI C3."
            )
        integration_inputs = build_integration_inputs_from_fan_out(
            fan_out,
            user_message=inputs.user_message,
            conversation_thread=inputs.integration_thread,
            surfaced_tools=inputs.surfaced_tools,
            active_context_spaces=inputs.integration_active_spaces,
            member_id=inputs.member_id,
            instance_id=inputs.instance_id,
            space_id=inputs.space_id,
            turn_id=inputs.turn_id,
            cognitive_context=inputs.cognitive_context,
        )
        return await self._integration_service.run(integration_inputs)

    async def run_enactment(self, briefing: Briefing) -> Any:
        if self._enactment_service is None:
            raise TurnRunnerNotWired(
                "TurnRunner.enactment_service is not wired. "
                "EnactmentService lands in PDI C4."
            )
        return await self._enactment_service.run(briefing)

    async def deliver(self, briefing: Briefing, outcome: Any) -> Any:
        """Translate the enactment outcome into the caller-shaped result.

        When no delivery hook is wired, return the outcome verbatim;
        callers in C2 (skeleton tests) inspect the outcome directly.
        Production wiring (INTEGRATION-WIRE-LIVE) will install a hook
        that produces the legacy ReasoningResult shape so the message
        handler stays unchanged.
        """
        if self._response_delivery is None:
            return outcome
        return await self._response_delivery(briefing, outcome)


class TurnRunnerNotWired(RuntimeError):
    """Raised when TurnRunner is invoked without a required dependency.

    Surfaces the wiring gap loudly so an operator who flips the
    feature flag without finishing the wiring sees the cause
    immediately rather than chasing a half-formed turn.
    """


__all__ = [
    "EnactmentServiceLike",
    "IntegrationServiceLike",
    "TurnRunner",
    "TurnRunnerInputs",
    "TurnRunnerNotWired",
]
