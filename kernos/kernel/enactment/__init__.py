"""EnactmentService — the doing layer of the four-layer cognition stack.

Introduced by PRESENCE-DECOUPLING-INTRODUCE C4-C6. EnactmentService
consumes a Briefing (from IntegrationService) and routes between
two structurally distinct paths:

  - **Thin path (render-only)** — for conversational and proposal
    decided_actions. The model renders the user-facing response
    directly from the briefing. NO tool dispatch on this path; any
    real tool execution belongs to full machinery. (the design review edit.)

  - **Full machinery** — for execute_tool only. Plan creation +
    three-question check + five-tier response hierarchy + envelope
    validation at every plan-changing step. Streaming disabled
    during execution. (Lands in C5-C6.)

The branch decision at .run() entry is structural — based on
briefing.decided_action.kind, not model judgment. C4 implements the
thin path; full machinery raises NotImplementedError until C5.

Hard invariants enforced structurally:
  - EnactmentService never changes decided_action (envelope
    validation in C5-C6 enforces).
  - Safety-degraded fail-soft never produces respond_only (handled
    upstream by IntegrationService; thin path simply renders defer).
  - No same-turn integration re-entry (B2 in C6 stores reintegration
    context for the NEXT turn, never re-runs integration mid-turn).
  - Thin path never dispatches tools (this module's primary
    invariant; structurally guaranteed by absence of dispatcher
    dependency on the thin-path code path).
  - Confirmation boundaries respected (envelope validation, C5-C6).
"""

from kernos.kernel.enactment.dispatcher import (
    DEFAULT_TOOL_TIMEOUT_MS,
    StepDispatcher,
    ToolDescriptorLookup,
    ToolExecutionInputs,
    ToolExecutionResult,
    ToolExecutor,
    TraceSink,
    build_step_dispatcher,
)
from kernos.kernel.enactment.divergence_reasoner import (
    DEFAULT_REASONER_MAX_TOKENS,
    DivergenceReasoner,
    DivergenceReasonerError,
    build_divergence_reasoner,
)
from kernos.kernel.enactment.envelope import (
    ValidationOutcome,
    validate_plan_against_envelope,
    validate_step_against_envelope,
)
from kernos.kernel.enactment.planner import (
    DEFAULT_PLANNER_MAX_TOKENS,
    PLAN_FINALIZE_TOOL_NAME,
    Planner,
    PlannerError,
    StaticToolCatalog,
    ToolCatalogEntry,
    ToolCatalogProvider,
    build_planner,
)
from kernos.kernel.enactment.presence_renderer import (
    B1RenderInputs,
    B2RenderInputs,
    DEFAULT_PRESENCE_MAX_TOKENS,
    PresenceRenderer,
    build_presence_renderer,
)
from kernos.kernel.enactment.friction import (
    FrictionObserverLike,
    FrictionTicket,
    NullFrictionObserver,
    TIER_1_RETRY_EXHAUSTED,
    TIER_2_MODIFY_EXHAUSTED,
)
from kernos.kernel.enactment.plan import (
    Plan,
    PlanValidationError,
    SignalKind,
    Step,
    StepExpectation,
    StructuredSignal,
    evaluate_expectation_signals,
    evaluate_signal,
    new_plan_id,
    now_iso,
)
from kernos.kernel.enactment.reintegration import (
    DISCOVERED_INFORMATION_CAP,
    ExecutionTrace,
    PLANS_ATTEMPTED_CAP,
    PlanRef,
    ReintegrationContext,
    TOOL_OUTCOMES_SUMMARY_CAP,
)
from kernos.kernel.enactment.service import (
    ClarificationFormulationInputs,
    ClarificationFormulationResult,
    DEFAULT_REASSEMBLY_PER_ENVELOPE,
    DEFAULT_REASSEMBLY_PER_TURN,
    DivergenceJudgeInputs,
    DivergenceJudgment,
    DivergenceReasonerLike,
    EnactmentNotImplemented,
    EnactmentOutcome,
    EnactmentService,
    PlanCreationInputs,
    PlanCreationResult,
    PlannerLike,
    PresenceRenderResult,
    PresenceRendererLike,
    ReassemblyBudget,
    StepDispatcherLike,
    StepDispatchInputs,
    StepDispatchResult,
    TerminationSubtype,
    TierThreePivotInputs,
    TierTwoModifyInputs,
    build_enactment_service,
)
from kernos.kernel.enactment.tiers import (
    DEFAULT_MODIFY_BUDGET,
    DEFAULT_PIVOT_BUDGET,
    DEFAULT_RETRY_BUDGET,
    FailureKind,
    ThreeQuestionResult,
    TierBudgets,
    TierRouting,
    all_three_pass,
    classify_routing,
)

__all__ = [
    "B1RenderInputs",
    "B2RenderInputs",
    "ClarificationFormulationInputs",
    "ClarificationFormulationResult",
    "DEFAULT_MODIFY_BUDGET",
    "DEFAULT_PIVOT_BUDGET",
    "DEFAULT_PLANNER_MAX_TOKENS",
    "DEFAULT_PRESENCE_MAX_TOKENS",
    "DEFAULT_REASONER_MAX_TOKENS",
    "DEFAULT_REASSEMBLY_PER_ENVELOPE",
    "DEFAULT_REASSEMBLY_PER_TURN",
    "DEFAULT_RETRY_BUDGET",
    "DEFAULT_TOOL_TIMEOUT_MS",
    "DISCOVERED_INFORMATION_CAP",
    "DivergenceJudgeInputs",
    "DivergenceJudgment",
    "DivergenceReasoner",
    "DivergenceReasonerError",
    "DivergenceReasonerLike",
    "EnactmentNotImplemented",
    "EnactmentOutcome",
    "EnactmentService",
    "ExecutionTrace",
    "FailureKind",
    "FrictionObserverLike",
    "FrictionTicket",
    "NullFrictionObserver",
    "PLANS_ATTEMPTED_CAP",
    "PLAN_FINALIZE_TOOL_NAME",
    "Plan",
    "PlanCreationInputs",
    "PlanCreationResult",
    "PlanRef",
    "PlanValidationError",
    "Planner",
    "PlannerError",
    "PlannerLike",
    "PresenceRenderer",
    "StepDispatcher",
    "PresenceRenderResult",
    "PresenceRendererLike",
    "ReassemblyBudget",
    "ReintegrationContext",
    "SignalKind",
    "StaticToolCatalog",
    "Step",
    "StepDispatchInputs",
    "StepDispatchResult",
    "StepDispatcherLike",
    "StepExpectation",
    "StructuredSignal",
    "TIER_1_RETRY_EXHAUSTED",
    "TIER_2_MODIFY_EXHAUSTED",
    "TOOL_OUTCOMES_SUMMARY_CAP",
    "TerminationSubtype",
    "ThreeQuestionResult",
    "TierBudgets",
    "TierRouting",
    "ToolCatalogEntry",
    "ToolCatalogProvider",
    "ToolDescriptorLookup",
    "ToolExecutionInputs",
    "ToolExecutionResult",
    "ToolExecutor",
    "TierThreePivotInputs",
    "TierTwoModifyInputs",
    "TraceSink",
    "ValidationOutcome",
    "all_three_pass",
    "build_divergence_reasoner",
    "build_enactment_service",
    "build_planner",
    "build_presence_renderer",
    "build_step_dispatcher",
    "classify_routing",
    "evaluate_expectation_signals",
    "evaluate_signal",
    "new_plan_id",
    "now_iso",
    "validate_plan_against_envelope",
    "validate_step_against_envelope",
]
