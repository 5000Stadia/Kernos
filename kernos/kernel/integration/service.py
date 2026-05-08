"""IntegrationService — production-shaped façade over IntegrationRunner.

PRESENCE-DECOUPLING-INTRODUCE C3. Wraps the V1 IntegrationRunner with
a stable production entry point so:

  - TurnRunner consumes a clean Protocol-conforming dependency
    (`async def run(inputs) -> Briefing`) without knowing about the
    runner's chain_caller / dispatcher / audit_emitter dependencies.
  - Construction is centralised — wiring sites build the service once
    with their concrete chain caller, read-only dispatcher, and audit
    emitter, then hand the service to the TurnRunner.
  - Future tuning lands here without disturbing the runner's V1
    contract.

The runner already implements:
  - First-pass clarification_needed production (the model emits it
    via __finalize_briefing__ when critical info is missing).
  - Explicit ActionEnvelope construction for action-shape
    decided_actions (added in PDI C1 to runner._finalize).
  - Safety-degraded routing (defer / constrained_response when
    required+safety_class cohorts failed).
  - Iterative loop with read-only tools; iteration cap from
    IntegrationConfig.

C3's substance:
  - IntegrationService factory + run() façade.
  - Updated system prompt teaching the model the new variants
    (clarification_needed, action_envelope) — see
    kernos/kernel/integration/template.py.
  - Tests for the first-pass clarification scenario and the envelope
    construction path.

C3 does NOT change the runner's internals. The runner's _finalize
already validates the envelope-required rule structurally; the
service simply provides the clean caller-facing surface.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from kernos.kernel.integration.briefing import Briefing
from kernos.kernel.integration.runner import (
    AuditEmitter,
    ChainCaller,
    IntegrationConfig,
    IntegrationInputs,
    IntegrationRunner,
    ReadOnlyToolDispatcher,
)


logger = logging.getLogger(__name__)


class IntegrationService:
    """Production-shaped façade over IntegrationRunner.

    Conforms to TurnRunner's IntegrationServiceLike Protocol:
    `async def run(inputs: IntegrationInputs) -> Briefing`.

    The service holds the runner and delegates to it. Construction
    accepts the runner's dependencies (chain_caller, dispatcher,
    audit_emitter, config) so callers wire once and then forward.
    Existing code that constructs IntegrationRunner directly continues
    to work; this service is additive.

    Threading rule: one service instance per ReasoningService is fine
    — the underlying runner is request-scoped (each `.run()` call
    builds its own state). The service holds no per-request state.
    """

    def __init__(
        self,
        *,
        chain_caller: ChainCaller,
        read_only_dispatcher: ReadOnlyToolDispatcher,
        audit_emitter: AuditEmitter,
        config: IntegrationConfig | None = None,
        action_record_drainer: Any = None,
    ) -> None:
        self._runner = IntegrationRunner(
            chain_caller=chain_caller,
            read_only_dispatcher=read_only_dispatcher,
            audit_emitter=audit_emitter,
            config=config,
            action_record_drainer=action_record_drainer,
        )

    async def run(self, inputs: IntegrationInputs) -> Briefing:
        """Run integration prep for one turn; produce a Briefing.

        Pure delegate to the V1 runner. The runner enforces:
          - The redaction invariant (no Restricted cohort content
            in briefing text fields).
          - The safety policy (required+safety_class cohort failures
            force defer / constrained_response).
          - The action_envelope contract (execute_tool requires a
            well-formed envelope; non-action kinds reject one).
          - Fail-soft on errors (Defer briefing when safety-degraded;
            minimal RespondOnly briefing otherwise).
        """
        return await self._runner.run(inputs)


def build_integration_service(
    *,
    chain_caller: ChainCaller,
    read_only_dispatcher: ReadOnlyToolDispatcher,
    audit_emitter: AuditEmitter,
    config: IntegrationConfig | None = None,
    action_record_drainer: Any = None,
) -> IntegrationService:
    """Convenience factory mirroring the IntegrationRunner constructor.

    Wiring sites prefer this factory over calling the constructor
    directly so the service's internal composition can change without
    breaking call sites.
    """
    return IntegrationService(
        chain_caller=chain_caller,
        read_only_dispatcher=read_only_dispatcher,
        audit_emitter=audit_emitter,
        config=config,
        action_record_drainer=action_record_drainer,
    )


__all__ = [
    "IntegrationService",
    "build_integration_service",
]
