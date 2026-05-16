"""EnactmentService — branch decision + thin-path rendering (PDI C4).

Consumes a Briefing and routes structurally:

  - decided_action.kind == execute_tool → full machinery (C5-C6)
  - everything else → thin path (render-only)

Render-only kinds (per the design review edit, propose_tool joins the conversational
set because the dispatch happens on the next turn after user confirms,
not in this turn):

  - respond_only
  - defer
  - constrained_response
  - pivot
  - clarification_needed
  - propose_tool

The thin path NEVER dispatches tools. This invariant is enforced
structurally: the thin-path code path takes only the presence renderer
as its dependency. There is no dispatcher reachable from this branch.

Audit category: enactment.terminated. Subtypes:
  - success_thin_path: any non-clarification thin-path render.
  - thin_path_proposal_rendered: propose_tool rendered (proposal is
    awaiting user confirmation; the dispatch happens on the next
    turn's execute_tool decision, NOT here).
  - b1_action_invalidated: full machinery termination (C5-C6).
  - b2_user_disambiguation_needed: full machinery surfaced a B2
    clarification or thin-path rendered a populated-partial_state
    clarification (the latter case — thin-path B2 echo — happens
    when integration emits clarification_needed with a populated
    partial_state because a previous turn's enactment surfaced it).

C4 only emits success_thin_path, thin_path_proposal_rendered, and
b2_user_disambiguation_needed. C5-C6 add b1_action_invalidated and
the per-step / per-tier audit events.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from kernos.kernel.enactment.envelope import (
    ValidationOutcome,
    validate_plan_against_envelope,
    validate_step_against_envelope,
)
from kernos.kernel.enactment.friction import (
    FrictionObserverLike,
    FrictionTicket,
    NullFrictionObserver,
    TIER_1_RETRY_EXHAUSTED,
    TIER_2_MODIFY_EXHAUSTED,
    now_iso as friction_now_iso,
)
from kernos.kernel.enactment.plan import (
    Plan,
    Step,
    StepExpectation,
    evaluate_expectation_signals,
    new_plan_id,
    now_iso,
)
from kernos.kernel.enactment.reintegration import (
    ExecutionTrace,
    PlanRef,
    ReintegrationContext,
)
from kernos.kernel.enactment.tiers import (
    DEFAULT_MODIFY_BUDGET,
    DEFAULT_PIVOT_BUDGET,
    DEFAULT_RETRY_BUDGET,
    FailureKind,
    ThreeQuestionResult,
    TierBudgets,
    TierRouting,
    classify_routing,
)
from kernos.kernel.integration.briefing import (
    ActionEnvelope,
    ActionKind,
    AuditTrace,
    Briefing,
    ClarificationNeeded,
    ClarificationPartialState,
)


# ---------------------------------------------------------------------------
# Reassembly budget (PDI C6)
# ---------------------------------------------------------------------------


# Default reassembly budgets per spec Section 5e (Tier 4).
DEFAULT_REASSEMBLY_PER_ENVELOPE = 2
DEFAULT_REASSEMBLY_PER_TURN = 3


@dataclass(frozen=True)
class ReassemblyBudget:
    """Tier-4 budget tracked at envelope-conceptual / per-turn-implementation
    level.

    Per design review's C6 guidance: under the current one-action-per-turn
    invariant, per-envelope and per-turn are equivalent. The budget
    structure maps to envelope; the per-turn cap is a safety net that
    becomes load-bearing if the one-action-per-turn invariant ever
    breaks.
    """

    per_envelope_remaining: int = DEFAULT_REASSEMBLY_PER_ENVELOPE
    per_turn_remaining: int = DEFAULT_REASSEMBLY_PER_TURN

    def can_reassemble(self) -> bool:
        return (
            self.per_envelope_remaining > 0
            and self.per_turn_remaining > 0
        )

    def consumed(self) -> "ReassemblyBudget":
        return ReassemblyBudget(
            per_envelope_remaining=max(0, self.per_envelope_remaining - 1),
            per_turn_remaining=max(0, self.per_turn_remaining - 1),
        )


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Termination subtypes
# ---------------------------------------------------------------------------


class TerminationSubtype(str, Enum):
    """Closed enum of enactment.terminated audit subtypes.

    Stable across C4-C8 — adding a value is a schema extension, not
    a tweak. The audit consumer (operator dashboards, friction
    observer) keys on these.

    Per the design review re-review on PDI: success_thin_path and
    success_full_machinery are kept distinct so audit filtering can
    distinguish where each completion came from. C5/C6 emitted the
    full-machinery happy path as success_thin_path; C8 corrects.
    """

    SUCCESS_THIN_PATH = "success_thin_path"
    SUCCESS_FULL_MACHINERY = "success_full_machinery"
    THIN_PATH_PROPOSAL_RENDERED = "thin_path_proposal_rendered"
    B1_ACTION_INVALIDATED = "b1_action_invalidated"
    B2_USER_DISAMBIGUATION_NEEDED = "b2_user_disambiguation_needed"


# Kinds that take the thin path (the design review edit — propose_tool included).
_THIN_PATH_KINDS: frozenset[ActionKind] = frozenset({
    ActionKind.RESPOND_ONLY,
    ActionKind.DEFER,
    ActionKind.CONSTRAINED_RESPONSE,
    ActionKind.PIVOT,
    ActionKind.CLARIFICATION_NEEDED,
    ActionKind.PROPOSE_TOOL,
})

# Kinds that take full machinery. Per spec post-the design review-edit: only
# execute_tool. propose_tool was historically considered for full
# machinery and the design review moved it to render-only because its actual
# dispatch happens on the *next* turn after the user confirms.
_FULL_MACHINERY_KINDS: frozenset[ActionKind] = frozenset({
    ActionKind.EXECUTE_TOOL,
})


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnactmentOutcome:
    """Result of one enactment turn.

    `text`: user-facing rendered response.
    `subtype`: enactment.terminated audit subtype.
    `decided_action_kind`: forwarded kind for telemetry.
    `streamed`: whether the presence renderer streamed (thin path
        supports streaming; full machinery disables it during
        execution and re-enables only after terminal response).
    `audit_refs`: operator-readable references to audit entries.
    `reintegration_context` (PDI C6): set on B1 / B2 termination —
        carries the capped payload the next turn's integration
        consumes alongside the user's reply.
    `clarification` (PDI C6): set on B2 termination — the
        ClarificationNeeded variant enactment constructed
        directly (no integration call). Stored on the outcome so the
        wiring layer can route to the next turn's integration with
        the full clarification context attached.
    """

    text: str
    subtype: TerminationSubtype
    decided_action_kind: ActionKind
    streamed: bool = False
    audit_refs: tuple[str, ...] = ()
    reintegration_context: ReintegrationContext | None = None
    clarification: ClarificationNeeded | None = None

    @property
    def is_thin_path(self) -> bool:
        return self.subtype in (
            TerminationSubtype.SUCCESS_THIN_PATH,
            TerminationSubtype.THIN_PATH_PROPOSAL_RENDERED,
        )


# ---------------------------------------------------------------------------
# Service-shaped dependencies (Protocols)
# ---------------------------------------------------------------------------


@runtime_checkable
class PresenceRendererLike(Protocol):
    """Renders user-facing text from a Briefing.

    The thin path's only dependency. Implementations bind to a model
    chain and the presence prompt; tests pass stub renderers that
    return canned text. Streaming is the renderer's concern; the
    EnactmentService records whether streaming occurred via the
    `streamed` attribute on the response object.
    """

    async def render(self, briefing: Briefing) -> "PresenceRenderResult": ...


@dataclass(frozen=True)
class PresenceRenderResult:
    """What a PresenceRendererLike returns.

    `text` is the final rendered response. `streamed` records whether
    the renderer streamed mid-generation. Audit/telemetry sources
    this; the spec mandates streaming-disabled-during-full-machinery
    so this flag becomes load-bearing in C5+.
    """

    text: str
    streamed: bool = False


AuditEmitter = Callable[[dict[str, Any]], Awaitable[None]]
"""(audit_entry) → None. enactment.* audit family populates here."""


# ---------------------------------------------------------------------------
# Full-machinery dependency Protocols (PDI C5)
#
# Streaming-disabled-by-construction: NONE of these Protocols expose a
# streaming affordance. The full machinery's loop never reaches the
# PresenceRendererLike (which DOES expose `streamed`); it consumes
# only structured-data callables. The `streamed: True` outcome is
# therefore unreachable from inside the execution loop — not a
# runtime check, an import-time impossibility.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanCreationInputs:
    """Inputs to PlannerLike.create_plan.

    Initial plans (created at the start of full machinery) carry only
    `briefing` and `library_suggestions`. Tier-4 reassembled plans
    additionally carry `prior_plan_id`, `triggering_context_summary`,
    and `audit_refs` so the planner has the context it needs to
    re-shape the plan.
    """

    briefing: Briefing
    library_suggestions: tuple = ()
    """Stub returns empty in v1; workflow primitive future-loads."""

    # PDI C6 extensions for tier-4 reassemble paths.
    prior_plan_id: str = ""
    """Empty for initial plans."""
    triggering_context_summary: str = ""
    """What invalidated the prior plan; empty for initial plans."""
    audit_refs: tuple[str, ...] = ()
    """References to audit entries for the prior plan / outcomes."""


@dataclass(frozen=True)
class PlanCreationResult:
    """Output of PlannerLike.create_plan. Pure structured data."""

    plan: Plan


@runtime_checkable
class PlannerLike(Protocol):
    """Produces a Plan from a Briefing.

    Implementations bind to a model chain that emits a structured
    plan via a synthetic finalize tool (similar to integration's
    __finalize_briefing__). The Protocol return type is structured
    data — no streaming — so the streaming surface is unreachable
    from the planner code path.
    """

    async def create_plan(
        self, inputs: PlanCreationInputs
    ) -> PlanCreationResult: ...


@dataclass(frozen=True)
class StepDispatchInputs:
    """Inputs to StepDispatcherLike.dispatch."""

    step: Step
    briefing: Briefing
    attempt_number: int = 1
    """1-indexed; incremented on tier-1 retry."""


@dataclass(frozen=True)
class StepDispatchResult:
    """What StepDispatcherLike returns.

    `completed` is True when the dispatch returned without exception
    (whether the call's logical outcome succeeded is captured in
    `output` — runtime parses for success_status etc).
    `output` is the tool's result dict; signal evaluator reads it.
    `failure_kind` is the dispatcher's classification of the result;
    NONE on clean success.
    `error_summary` is a redacted operator-readable label.
    `corrective_signal` carries the tool's guidance text when
    failure_kind is CORRECTIVE_SIGNAL (e.g. "rate-limit, batch too
    large"); empty otherwise.
    """

    completed: bool
    output: dict[str, Any]
    failure_kind: FailureKind = FailureKind.NONE
    error_summary: str = ""
    corrective_signal: str = ""
    duration_ms: int = 0


@runtime_checkable
class StepDispatcherLike(Protocol):
    """Executes one Step against a tool. Pure structured data return.

    Implementations bind to the workshop dispatch surface. The
    dispatcher classifies failures into FailureKind values per the
    closed taxonomy in tiers.py.
    """

    async def dispatch(
        self, inputs: StepDispatchInputs
    ) -> StepDispatchResult: ...


@dataclass(frozen=True)
class DivergenceJudgeInputs:
    """Inputs to DivergenceReasonerLike.judge_divergence."""

    step: Step
    expectation: StepExpectation
    dispatch_result: StepDispatchResult
    structured_pass: bool
    """Did all structured signals pass deterministically?"""


@dataclass(frozen=True)
class DivergenceJudgment:
    """Output of DivergenceReasonerLike.judge_divergence."""

    effect_matches_expectation: bool
    plan_still_valid: bool
    failure_kind: FailureKind
    """Reasoner may upgrade the dispatcher's NONE → INFORMATION_DIVERGENCE
    when prose comparison shows the result re-shapes the plan."""


@dataclass(frozen=True)
class TierTwoModifyInputs:
    """Inputs to DivergenceReasonerLike.emit_modified_step."""

    original_step: Step
    dispatch_result: StepDispatchResult
    briefing: Briefing


@dataclass(frozen=True)
class TierThreePivotInputs:
    """Inputs to DivergenceReasonerLike.emit_pivot_step."""

    original_step: Step
    dispatch_result: StepDispatchResult
    briefing: Briefing


@dataclass(frozen=True)
class ClarificationFormulationInputs:
    """Inputs to DivergenceReasonerLike.formulate_clarification (PDI C6).

    Used when tier 5 B2 fires — the full machinery has detected
    AMBIGUITY_NEEDS_USER and needs to construct a user-facing
    ClarificationNeeded variant directly (no integration call).
    """

    failed_step: Step
    dispatch_result: StepDispatchResult
    briefing: Briefing
    audit_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClarificationFormulationResult:
    """Output of DivergenceReasonerLike.formulate_clarification.

    All char fields will be enforced by the downstream
    ClarificationPartialState / ClarificationNeeded dataclasses (PDI
    C1). The reasoner is responsible for staying within caps; over-
    cap output causes BriefingValidationError on construction.

    `ambiguity_type` must be one of the closed enum values:
    target | parameter | approach | intent | other.
    """

    question: str
    """≤200 chars; one user-facing sentence."""
    ambiguity_type: str
    """closed enum: target | parameter | approach | intent | other"""
    blocking_ambiguity: str
    """≤500 chars; description of what cannot be resolved."""
    safe_question_context: str
    """≤500 chars; user-safe context for the question."""
    attempted_action_summary: str
    """≤1000 chars; what was being attempted before B2 fired."""
    discovered_information: str
    """≤1000 chars; what was learned that surfaced ambiguity."""


@runtime_checkable
class DivergenceReasonerLike(Protocol):
    """Model-judged divergence routing for full machinery.

    All return types are structured Step / DivergenceJudgment /
    ClarificationFormulationResult data. No streaming affordance.
    The full machinery loop never hands the user-facing presence
    renderer to this Protocol.
    """

    async def judge_divergence(
        self, inputs: DivergenceJudgeInputs
    ) -> DivergenceJudgment: ...

    async def emit_modified_step(
        self, inputs: TierTwoModifyInputs
    ) -> Step: ...

    async def emit_pivot_step(
        self, inputs: TierThreePivotInputs
    ) -> Step: ...

    async def formulate_clarification(
        self, inputs: ClarificationFormulationInputs
    ) -> ClarificationFormulationResult: ...


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EnactmentNotImplemented(NotImplementedError):
    """Raised when a code path is not yet wired (full machinery in
    C5-C6). Distinct subclass so wiring sites can route the error
    cleanly without confusing it with library NotImplementedError."""


# ---------------------------------------------------------------------------
# EnactmentService
# ---------------------------------------------------------------------------


class EnactmentService:
    """Branch decision + thin-path rendering.

    Invariants enforced structurally:
      - The thin path takes only the presence renderer; no
        dispatcher reachable from this branch. The "thin path never
        dispatches tools" rule is therefore not a runtime check, it's
        a code-shape guarantee.
      - The decided_action.kind drives branching; model judgment
        plays no role in path selection.
      - The full machinery branch is a stub in C4; calling it raises
        a clear EnactmentNotImplemented pointing at PDI C5.
    """

    def __init__(
        self,
        *,
        presence_renderer: PresenceRendererLike,
        audit_emitter: AuditEmitter | None = None,
        planner: PlannerLike | None = None,
        step_dispatcher: StepDispatcherLike | None = None,
        divergence_reasoner: DivergenceReasonerLike | None = None,
        friction_observer: FrictionObserverLike | None = None,
        retry_budget: int = DEFAULT_RETRY_BUDGET,
        modify_budget: int = DEFAULT_MODIFY_BUDGET,
        pivot_budget: int = DEFAULT_PIVOT_BUDGET,
        reassembly_per_envelope: int = DEFAULT_REASSEMBLY_PER_ENVELOPE,
        reassembly_per_turn: int = DEFAULT_REASSEMBLY_PER_TURN,
    ) -> None:
        # Same-turn integration re-entry is impossible by construction:
        # there is NO integration_service parameter on this constructor.
        # The B2 termination path constructs ClarificationNeeded directly
        # via the divergence_reasoner.formulate_clarification hook —
        # it never calls back into integration. Audit pin in test
        # asserts no "integration"-named parameter exists.
        self._presence = presence_renderer
        self._audit = audit_emitter
        self._planner = planner
        self._dispatcher = step_dispatcher
        self._reasoner = divergence_reasoner
        self._friction = friction_observer or NullFrictionObserver()
        self._retry_budget = retry_budget
        self._modify_budget = modify_budget
        self._pivot_budget = pivot_budget
        self._reassembly_per_envelope = reassembly_per_envelope
        self._reassembly_per_turn = reassembly_per_turn

    async def run(self, briefing: Briefing) -> EnactmentOutcome:
        """Branch on decided_action.kind and route to the right path.

        Branch decision is structural. Full machinery is reserved for
        execute_tool only (the design review edit). Everything else takes the thin
        path. An unrecognised kind is a contract bug — surfaced via
        ValueError so the wiring layer can catch it cleanly.
        """
        kind = briefing.decided_action.kind
        if kind in _FULL_MACHINERY_KINDS:
            return await self._run_full_machinery(briefing)
        if kind in _THIN_PATH_KINDS:
            return await self._run_thin_path(briefing)
        # Defensive — every ActionKind belongs to exactly one set; this
        # branch fires only if a future variant is added without
        # updating the route maps. Failing loudly is the right move.
        raise ValueError(
            f"EnactmentService cannot route decided_action.kind "
            f"{kind.value!r}: not in thin-path or full-machinery sets. "
            f"Update _THIN_PATH_KINDS / _FULL_MACHINERY_KINDS."
        )

    # ----- thin path -----

    async def _run_thin_path(self, briefing: Briefing) -> EnactmentOutcome:
        """Render-only execution.

        No tool dispatch. The presence renderer takes the briefing
        and produces user-facing text. Subtype routing handles the
        special cases:

          - propose_tool → THIN_PATH_PROPOSAL_RENDERED. The proposal
            is awaiting the user's next-turn confirmation; the
            corresponding execute_tool will land on the next turn,
            with its own envelope. This turn renders the proposal
            text only.
          - clarification_needed with populated partial_state →
            B2_USER_DISAMBIGUATION_NEEDED. The previous turn's
            full machinery surfaced this clarification; thin path
            renders the question. Reintegration context lives on
            the briefing's audit refs (C6 wires the storage).
          - clarification_needed with None partial_state → SUCCESS
            (first-pass clarification, integration-initiated).
          - everything else → SUCCESS_THIN_PATH.
        """
        result = await self._presence.render(briefing)
        subtype = self._thin_path_subtype(briefing)
        await self._emit_terminated(briefing, subtype, text=result.text)
        return EnactmentOutcome(
            text=result.text,
            subtype=subtype,
            decided_action_kind=briefing.decided_action.kind,
            streamed=result.streamed,
        )

    @staticmethod
    def _thin_path_subtype(briefing: Briefing) -> TerminationSubtype:
        kind = briefing.decided_action.kind
        if kind is ActionKind.PROPOSE_TOOL:
            return TerminationSubtype.THIN_PATH_PROPOSAL_RENDERED
        if isinstance(briefing.decided_action, ClarificationNeeded):
            # Populated partial_state means the previous turn's full
            # machinery surfaced a B2 clarification; the next turn's
            # integration emitted the variant; thin path renders. The
            # audit subtype reflects the B2 routing for telemetry.
            if briefing.decided_action.partial_state is not None:
                return TerminationSubtype.B2_USER_DISAMBIGUATION_NEEDED
        return TerminationSubtype.SUCCESS_THIN_PATH

    # ----- full machinery (PDI C5: plan + three-question check + tiers 1/2/3) -----
    #
    # Streaming-disabled-by-construction note: this branch consumes
    # only structured-data Protocols (PlannerLike, StepDispatcherLike,
    # DivergenceReasonerLike). It does NOT call self._presence inside
    # the loop. The terminal render (after all steps complete) is the
    # only point where the streaming-capable presence_renderer is
    # consulted, and only after the loop has terminated. There is no
    # code path inside the loop that can stream user-facing text.

    async def _run_full_machinery(
        self, briefing: Briefing
    ) -> EnactmentOutcome:
        if self._planner is None or self._dispatcher is None or self._reasoner is None:
            raise EnactmentNotImplemented(
                "Full machinery requires planner + step_dispatcher + "
                "divergence_reasoner. Wire all three in the service "
                "construction site."
            )
        envelope = briefing.action_envelope
        if envelope is None:
            # Per PDI C1: action-shape decided_actions REQUIRE an
            # envelope. The Briefing constructor already enforces
            # this; reaching here means a contract violation upstream.
            raise EnactmentNotImplemented(
                "execute_tool briefing arrived without action_envelope; "
                "contract violation upstream of EnactmentService."
            )

        trace = ExecutionTrace()
        reassembly_budget = ReassemblyBudget(
            per_envelope_remaining=self._reassembly_per_envelope,
            per_turn_remaining=self._reassembly_per_turn,
        )

        # 1. Initial plan creation.
        plan_result = await self._planner.create_plan(
            PlanCreationInputs(briefing=briefing)
        )
        plan = plan_result.plan
        trace.record_plan(plan)

        # 2. enactment.plan_created emits BEFORE any step dispatches
        # (audit invariant — observable-before-action).
        await self._emit_plan_created(plan, briefing)

        # 3. Initial envelope validation. Invalid → B1 with capped
        # reintegration context.
        plan_validation = validate_plan_against_envelope(plan, envelope)
        if not plan_validation.valid:
            trace.record_discovered_information(
                f"plan rejected at envelope validation: "
                f"{plan_validation.reason}"
            )
            return await self._terminate_b1(
                briefing,
                plan=plan,
                trace=trace,
                reason=f"envelope_violation:{plan_validation.reason}",
                detail=plan_validation.detail,
            )

        # 4. Per-step execution loop with tier 4 reassemble.
        return await self._execute_plan(
            plan=plan,
            briefing=briefing,
            envelope=envelope,
            trace=trace,
            reassembly_budget=reassembly_budget,
        )

    async def _execute_plan(
        self,
        *,
        plan: Plan,
        briefing: Briefing,
        envelope: ActionEnvelope,
        trace: ExecutionTrace,
        reassembly_budget: ReassemblyBudget,
        enactment_results: list[dict[str, str]] | None = None,
    ) -> EnactmentOutcome:
        """Execute every step of `plan`. On tier-4 reassemble, this
        method is called recursively with the new plan; reassembly
        budget decrements across the recursion.

        ``enactment_results`` accumulates per-step serialized dispatch
        outcomes (ACTION-RESULT-FORWARDING-V1) so they reach the
        terminal renderer's briefing — without it, full-machinery
        tool outputs (e.g. execute_code stdout) vanish between the
        dispatcher returning a StepDispatchResult and EnactmentService
        rendering the unmodified original briefing. The list is
        threaded through tier-4 reassemble recursion so results from
        the original plan's executed steps survive the rebuild.
        """
        if enactment_results is None:
            enactment_results = []
        for step in plan.steps:
            outcome = await self._run_step_with_tiers(
                step=step,
                plan=plan,
                briefing=briefing,
                envelope=envelope,
                trace=trace,
                reassembly_budget=reassembly_budget,
                enactment_results=enactment_results,
            )
            if outcome is not None:
                # Step terminated the loop. If it's a reassemble
                # signal (returned via a sentinel outcome subtype
                # B1 with reason starting "tier_4_reassemble:"), we
                # would handle here — but the actual reassemble
                # path is handled inline within
                # _run_step_with_tiers, which calls back into
                # _execute_plan with the new plan. So any returned
                # outcome here is genuinely terminating.
                return outcome

        # All steps completed cleanly. Terminal render via presence
        # renderer (the ONLY post-loop streaming point).
        #
        # Audit subtype is SUCCESS_FULL_MACHINERY (the design review re-review: keep
        # full-machinery completions distinct from thin-path
        # completions for diagnostic clarity in audit-trail filters).
        render_briefing = _briefing_with_enactment_results(
            briefing, enactment_results,
        )
        result = await self._presence.render(render_briefing)
        await self._emit_terminated(
            briefing,
            TerminationSubtype.SUCCESS_FULL_MACHINERY,
            text=result.text,
        )
        return EnactmentOutcome(
            text=result.text,
            subtype=TerminationSubtype.SUCCESS_FULL_MACHINERY,
            decided_action_kind=briefing.decided_action.kind,
            streamed=result.streamed,
        )

    async def _run_step_with_tiers(
        self,
        *,
        step: Step,
        plan: Plan,
        briefing: Briefing,
        envelope: ActionEnvelope,
        trace: ExecutionTrace,
        reassembly_budget: ReassemblyBudget,
        enactment_results: list[dict[str, str]],
    ) -> EnactmentOutcome | None:
        """Execute one step + three-question check + tier handling.

        Returns None when the step (after any tier handling) completed
        cleanly — caller advances to the next step. Returns an
        EnactmentOutcome when the step terminated the turn (B1 / B2)
        OR when tier-4 reassembled the plan (recursion through
        _execute_plan; the outcome bubbles up as the recursion's
        terminal outcome).
        """
        budgets = TierBudgets(
            retry_remaining=self._retry_budget,
            modify_remaining=self._modify_budget,
            pivot_remaining=self._pivot_budget,
        )
        current_step = step
        attempt_number = 1

        while True:
            # Dispatch.
            dispatch_result = await self._dispatcher.dispatch(
                StepDispatchInputs(
                    step=current_step,
                    briefing=briefing,
                    attempt_number=attempt_number,
                )
            )
            await self._emit_step_attempted(
                plan=plan,
                step=current_step,
                attempt_number=attempt_number,
                result=dispatch_result,
            )
            trace.record_step_outcome(
                _step_outcome_summary(current_step, dispatch_result)
            )
            # ACTION-RESULT-FORWARDING-V1: capture every attempt's
            # outcome so the terminal renderer's briefing carries it.
            # Recorded per attempt (not just final) so retry/modify
            # chains stay visible to the model — the agent sees what
            # was tried, not just what eventually worked.
            enactment_results.append(
                _serialise_step_result(current_step, dispatch_result)
            )

            # Three-question check.
            structured_pass, _ = evaluate_expectation_signals(
                current_step.expectation, dispatch_result.output
            )
            judgment = await self._reasoner.judge_divergence(
                DivergenceJudgeInputs(
                    step=current_step,
                    expectation=current_step.expectation,
                    dispatch_result=dispatch_result,
                    structured_pass=structured_pass,
                )
            )
            check = ThreeQuestionResult(
                step_completed=dispatch_result.completed,
                effect_matches_expectation=judgment.effect_matches_expectation,
                plan_still_valid=judgment.plan_still_valid,
            )

            # Tier classification — pure function.
            failure_kind = (
                judgment.failure_kind
                if dispatch_result.failure_kind is FailureKind.NONE
                else dispatch_result.failure_kind
            )
            routing = classify_routing(
                check=check,
                failure_kind=failure_kind,
                budgets=budgets,
            )

            if routing is TierRouting.PROCEED:
                return None  # advance to next step

            if routing is TierRouting.TIER_1_RETRY:
                budgets = budgets.with_retry_consumed()
                attempt_number += 1
                continue  # same step, same args, re-dispatch

            if routing is TierRouting.TIER_2_MODIFY:
                # If we just exhausted the retry budget on a transient
                # failure (and modify is the fallback), record a
                # friction ticket. Write-only sink — the ticket
                # is recorded after the routing decision is final;
                # it does NOT influence the subsequent dispatch.
                if (
                    failure_kind is FailureKind.TRANSIENT
                    and budgets.retry_remaining == 0
                ):
                    await self._record_friction_ticket(
                        briefing=briefing,
                        step=current_step,
                        divergence_pattern=TIER_1_RETRY_EXHAUSTED,
                        attempt_count=attempt_number,
                    )

                modified = await self._reasoner.emit_modified_step(
                    TierTwoModifyInputs(
                        original_step=current_step,
                        dispatch_result=dispatch_result,
                        briefing=briefing,
                    )
                )
                # Envelope validation BEFORE dispatch (the design review edit).
                step_validation = validate_step_against_envelope(
                    modified, envelope
                )
                if not step_validation.valid:
                    trace.record_discovered_information(
                        f"tier-2 modify produced an envelope-violating "
                        f"step: {step_validation.reason}"
                    )
                    return await self._terminate_b1(
                        briefing,
                        plan=plan,
                        trace=trace,
                        reason=(
                            f"envelope_violation_on_modify:"
                            f"{step_validation.reason}"
                        ),
                        detail=step_validation.detail,
                        enactment_results=enactment_results,
                    )
                await self._emit_step_modified(
                    plan=plan,
                    original_step=current_step,
                    modified_step=modified,
                    reason=dispatch_result.corrective_signal
                    or "tier_2_modify",
                    envelope_outcome=step_validation,
                )
                budgets = budgets.with_modify_consumed()
                current_step = modified
                attempt_number = 1
                continue

            if routing is TierRouting.TIER_3_PIVOT:
                pivoted = await self._reasoner.emit_pivot_step(
                    TierThreePivotInputs(
                        original_step=current_step,
                        dispatch_result=dispatch_result,
                        briefing=briefing,
                    )
                )
                step_validation = validate_step_against_envelope(
                    pivoted, envelope
                )
                if not step_validation.valid:
                    trace.record_discovered_information(
                        f"tier-3 pivot produced an envelope-violating "
                        f"step: {step_validation.reason}"
                    )
                    return await self._terminate_b1(
                        briefing,
                        plan=plan,
                        trace=trace,
                        reason=(
                            f"envelope_violation_on_pivot:"
                            f"{step_validation.reason}"
                        ),
                        detail=step_validation.detail,
                        enactment_results=enactment_results,
                    )
                await self._emit_step_pivoted(
                    plan=plan,
                    original_step=current_step,
                    replacement_step=pivoted,
                    reason="tier_3_pivot",
                    envelope_outcome=step_validation,
                )
                budgets = budgets.with_pivot_consumed()
                current_step = pivoted
                attempt_number = 1
                continue

            if routing is TierRouting.TIER_4_REASSEMBLE:
                # Tier-4: kickback to plan creation with new context.
                # Recursion through _execute_plan with the new plan;
                # reassembly_budget decrements once.
                if not reassembly_budget.can_reassemble():
                    return await self._terminate_b1(
                        briefing,
                        plan=plan,
                        trace=trace,
                        reason="reassembly_budget_exhausted",
                        detail=(
                            "tier-4 reassemble routing fired but "
                            "per-envelope or per-turn reassembly "
                            "budget is exhausted"
                        ),
                        enactment_results=enactment_results,
                    )

                # Friction ticket on tier-2 exhaustion (transient or
                # corrective failures cycling through modify before
                # reassembly fires).
                if (
                    failure_kind in (
                        FailureKind.TRANSIENT,
                        FailureKind.CORRECTIVE_SIGNAL,
                        FailureKind.NON_TRANSIENT,
                    )
                    and budgets.modify_remaining == 0
                ):
                    await self._record_friction_ticket(
                        briefing=briefing,
                        step=current_step,
                        divergence_pattern=TIER_2_MODIFY_EXHAUSTED,
                        attempt_count=attempt_number,
                    )

                trace.record_discovered_information(
                    f"plan reassembled after step {current_step.step_id}"
                )
                new_plan_inputs = PlanCreationInputs(
                    briefing=briefing,
                    prior_plan_id=plan.plan_id,
                    triggering_context_summary=(
                        f"step {current_step.step_id} of plan {plan.plan_id} "
                        f"invalidated; reassembly requested."
                    ),
                    audit_refs=tuple(trace.audit_refs),
                )
                new_plan_result = await self._planner.create_plan(
                    new_plan_inputs
                )
                # Stamp the new plan as reassembled.
                new_plan = _stamp_reassembled(
                    new_plan_result.plan,
                    triggering_context_summary=(
                        new_plan_inputs.triggering_context_summary
                    ),
                )
                trace.record_plan(new_plan)
                await self._emit_plan_reassembled(
                    plan=plan,
                    new_plan=new_plan,
                    reason="tier_4_reassemble",
                    triggering_context_summary=(
                        new_plan_inputs.triggering_context_summary
                    ),
                    reassembly_count=(
                        DEFAULT_REASSEMBLY_PER_TURN
                        - reassembly_budget.per_turn_remaining
                        + 1
                    ),
                )

                # Envelope validation on the new plan.
                new_validation = validate_plan_against_envelope(
                    new_plan, envelope
                )
                if not new_validation.valid:
                    trace.record_discovered_information(
                        f"reassembled plan rejected at envelope "
                        f"validation: {new_validation.reason}"
                    )
                    return await self._terminate_b1(
                        briefing,
                        plan=new_plan,
                        trace=trace,
                        reason=(
                            f"envelope_violation_on_reassemble:"
                            f"{new_validation.reason}"
                        ),
                        detail=new_validation.detail,
                        enactment_results=enactment_results,
                    )

                # Execute the new plan from step 1; consume one
                # reassembly slot. Forward accumulated enactment
                # results so the rebuild's terminal render still
                # sees what the original plan attempted before
                # reassembly was triggered.
                return await self._execute_plan(
                    plan=new_plan,
                    briefing=briefing,
                    envelope=envelope,
                    trace=trace,
                    reassembly_budget=reassembly_budget.consumed(),
                    enactment_results=enactment_results,
                )

            if routing is TierRouting.TIER_5_SURFACE_B1:
                # Friction ticket if budgets exhausted on the
                # transient/corrective path.
                if budgets.modify_remaining == 0 and failure_kind in (
                    FailureKind.TRANSIENT,
                    FailureKind.CORRECTIVE_SIGNAL,
                    FailureKind.NON_TRANSIENT,
                ):
                    await self._record_friction_ticket(
                        briefing=briefing,
                        step=current_step,
                        divergence_pattern=TIER_2_MODIFY_EXHAUSTED,
                        attempt_count=attempt_number,
                    )
                trace.record_discovered_information(
                    f"step {current_step.step_id} exhausted budgets"
                )
                return await self._terminate_b1(
                    briefing,
                    plan=plan,
                    trace=trace,
                    reason="tier_5_b1_budget_exhausted",
                    detail=(
                        f"step {current_step.step_id} reached "
                        f"TIER_5_SURFACE_B1 with failure_kind="
                        f"{failure_kind.value}"
                    ),
                    enactment_results=enactment_results,
                )

            if routing is TierRouting.TIER_5_SURFACE_B2:
                trace.record_discovered_information(
                    f"step {current_step.step_id} surfaced ambiguity "
                    f"requiring user input"
                )
                return await self._terminate_b2(
                    briefing,
                    plan=plan,
                    failed_step=current_step,
                    dispatch_result=dispatch_result,
                    trace=trace,
                    enactment_results=enactment_results,
                )

            # Unknown routing → defensive B1.
            return await self._terminate_b1(
                briefing,
                plan=plan,
                trace=trace,
                reason="unknown_routing",
                detail=f"classify_routing returned {routing!r}",
                enactment_results=enactment_results,
            )

    # ----- C6 terminations: B1 + B2 with capped reintegration -----

    async def _terminate_b1(
        self,
        briefing: Briefing,
        *,
        plan: Plan | None,
        trace: ExecutionTrace,
        reason: str,
        detail: str,
        enactment_results: list[dict[str, str]] | None = None,
    ) -> EnactmentOutcome:
        """B1 surface: action invalidated. Capped reintegration payload
        produced and stored on the EnactmentOutcome for the next turn's
        integration.

        User-facing text is the brief acknowledgment per spec ("I
        started X but discovered Y; not proceeding"). C6 renders via
        the presence_renderer with the original briefing — the
        renderer's prompt (tuned in C7) handles the partial-work
        acknowledgment.

        ACTION-RESULT-FORWARDING-V1 (Codex round-2 fold): when steps
        ran before termination, their dispatch outcomes are forwarded
        to the renderer so the failure-path acknowledgment can
        reference what was actually tried — "I tried X and got Y" beats
        a generic "I discovered an issue." Empty/None means the
        termination preceded any dispatch (e.g. envelope rejection on
        the initial plan); the rendered briefing is unchanged.
        """
        # Record audit refs for the trace BEFORE constructing the
        # reintegration so the caps include them.
        trace.record_audit_ref(
            f"enactment.terminated:{briefing.turn_id}:b1"
        )
        reintegration = trace.to_reintegration_context(
            original_decided_action_kind=briefing.decided_action.kind.value,
        )

        # Render terminal acknowledgment via presence_renderer. The
        # renderer can stream — that's "after action complete" per
        # the spec's streaming rule.
        render_briefing = _briefing_with_enactment_results(
            briefing, enactment_results or [],
        )
        result = await self._presence.render(render_briefing)

        await self._emit_terminated(
            briefing,
            TerminationSubtype.B1_ACTION_INVALIDATED,
            text=result.text,
            extra={
                "reason": reason,
                "detail": detail,
                "plan_id": plan.plan_id if plan is not None else "",
                "reintegration_truncated": reintegration.truncated,
                "reintegration_plans_attempted": (
                    len(reintegration.plans_attempted)
                ),
            },
        )
        return EnactmentOutcome(
            text=result.text,
            subtype=TerminationSubtype.B1_ACTION_INVALIDATED,
            decided_action_kind=briefing.decided_action.kind,
            streamed=result.streamed,
            reintegration_context=reintegration,
        )

    async def _terminate_b2(
        self,
        briefing: Briefing,
        *,
        plan: Plan,
        failed_step: Step,
        dispatch_result: StepDispatchResult,
        trace: ExecutionTrace,
        enactment_results: list[dict[str, str]] | None = None,
    ) -> EnactmentOutcome:
        """B2 surface: user disambiguation needed.

        Per the design review edit (load-bearing): NO same-turn integration re-entry.
        Enactment constructs the ClarificationNeeded variant directly
        via the divergence_reasoner.formulate_clarification hook. The
        thin path renders the question. Reintegration payload is
        attached to the outcome for the NEXT turn's integration.

        The structural invariant — "no same-turn integration re-entry"
        — is enforced by construction: EnactmentService has NO
        integration_service dependency. The B2 code path here cannot
        reach integration even if a future bug tried to.
        """
        formulation = await self._reasoner.formulate_clarification(
            ClarificationFormulationInputs(
                failed_step=failed_step,
                dispatch_result=dispatch_result,
                briefing=briefing,
                audit_refs=tuple(trace.audit_refs),
            )
        )

        # Construct the bounded partial state (caps enforced by the
        # ClarificationPartialState dataclass per PDI C1).
        partial_state = ClarificationPartialState(
            attempted_action_summary=formulation.attempted_action_summary,
            discovered_information=formulation.discovered_information,
            blocking_ambiguity=formulation.blocking_ambiguity,
            safe_question_context=formulation.safe_question_context,
            audit_refs=tuple(trace.audit_refs),
        )
        clarification = ClarificationNeeded(
            question=formulation.question,
            ambiguity_type=formulation.ambiguity_type,
            partial_state=partial_state,
        )

        # Render the question via the thin path. We synthesize a
        # briefing carrying the ClarificationNeeded variant so the
        # presence renderer's interface stays uniform.
        synthetic_briefing = _synthesize_clarification_briefing(
            briefing, clarification
        )
        # ACTION-RESULT-FORWARDING-V1 (Codex round-2 fold): the
        # clarification render also gets the attempted steps' receipts
        # so the question can ground in what was tried, not just what
        # was abstracted away.
        synthetic_briefing = _briefing_with_enactment_results(
            synthetic_briefing, enactment_results or [],
        )
        result = await self._presence.render(synthetic_briefing)

        # Record audit ref + build capped reintegration.
        trace.record_audit_ref(
            f"enactment.terminated:{briefing.turn_id}:b2"
        )
        reintegration = trace.to_reintegration_context(
            original_decided_action_kind=briefing.decided_action.kind.value,
        )

        await self._emit_terminated(
            briefing,
            TerminationSubtype.B2_USER_DISAMBIGUATION_NEEDED,
            text=result.text,
            extra={
                "reason": "tier_5_b2_user_disambiguation",
                "plan_id": plan.plan_id,
                "failed_step_id": failed_step.step_id,
                "ambiguity_type": clarification.ambiguity_type,
                "reintegration_truncated": reintegration.truncated,
            },
        )
        return EnactmentOutcome(
            text=result.text,
            subtype=TerminationSubtype.B2_USER_DISAMBIGUATION_NEEDED,
            decided_action_kind=briefing.decided_action.kind,
            streamed=result.streamed,
            reintegration_context=reintegration,
            clarification=clarification,
        )

    async def _record_friction_ticket(
        self,
        *,
        briefing: Briefing,
        step: Step,
        divergence_pattern: str,
        attempt_count: int,
    ) -> None:
        """Write-only sink + enactment.friction_observed audit emission.

        Per PDI C7: the friction observer is the operator-visible
        sink for tier-1/2 exhaustion patterns. The audit family also
        carries the same signal so the broader audit pipeline can
        cross-reference friction with other enactment.* events.

        Both emissions happen AFTER the routing decision is final.
        Neither return value is read by the EnactmentService — by
        construction, the ticket cannot influence subsequent routing.
        """
        ticket = FrictionTicket(
            tool_id=step.tool_id,
            operation_name=step.operation_name,
            divergence_pattern=divergence_pattern,
            attempt_count=attempt_count,
            decided_action_kind=briefing.decided_action.kind.value,
            instance_id="",
            member_id="",
            turn_id=briefing.turn_id,
            timestamp=friction_now_iso(),
        )
        try:
            await self._friction.record(ticket)
        except Exception:
            logger.exception("FRICTION_OBSERVER_RECORD_FAILED")

        # Audit emission. References-not-dumps: tool_id, operation_name,
        # and divergence_pattern are operator-readable references; no
        # arg values, no output payloads.
        await self._emit(
            {
                "category": "enactment.friction_observed",
                "turn_id": briefing.turn_id,
                "tool_id": step.tool_id,
                "operation_name": step.operation_name,
                "divergence_pattern": divergence_pattern,
                "attempt_count": attempt_count,
                "decided_action_kind": briefing.decided_action.kind.value,
            }
        )

    # ----- audit emitters for the enactment.* family (PDI C5 entries) -----

    async def _emit_plan_created(
        self, plan: Plan, briefing: Briefing
    ) -> None:
        """References-not-dumps: the plan_id is the reference; the
        plan payload is NOT embedded in the audit entry."""
        await self._emit(
            {
                "category": "enactment.plan_created",
                "turn_id": briefing.turn_id,
                "integration_run_id": briefing.integration_run_id,
                "plan_id": plan.plan_id,
                "step_count": len(plan.steps),
                "created_at": plan.created_at,
                "created_via": plan.created_via,
            }
        )

    async def _emit_step_attempted(
        self,
        *,
        plan: Plan,
        step: Step,
        attempt_number: int,
        result: StepDispatchResult,
    ) -> None:
        await self._emit(
            {
                "category": "enactment.step_attempted",
                "turn_id": plan.turn_id,
                "plan_id": plan.plan_id,
                "step_id": step.step_id,
                "attempt_number": attempt_number,
                "tool_id": step.tool_id,
                "operation_name": step.operation_name,
                "tool_class": step.tool_class,
                "completed": result.completed,
                "failure_kind": result.failure_kind.value,
                "error_summary": result.error_summary,
                "duration_ms": result.duration_ms,
            }
        )

    async def _emit_step_modified(
        self,
        *,
        plan: Plan,
        original_step: Step,
        modified_step: Step,
        reason: str,
        envelope_outcome: ValidationOutcome,
    ) -> None:
        await self._emit(
            {
                "category": "enactment.step_modified",
                "turn_id": plan.turn_id,
                "plan_id": plan.plan_id,
                "original_step_id": original_step.step_id,
                "modified_step_id": modified_step.step_id,
                "reason": reason,
                "envelope_validation_passed": envelope_outcome.valid,
            }
        )

    async def _emit_step_pivoted(
        self,
        *,
        plan: Plan,
        original_step: Step,
        replacement_step: Step,
        reason: str,
        envelope_outcome: ValidationOutcome,
    ) -> None:
        await self._emit(
            {
                "category": "enactment.step_pivoted",
                "turn_id": plan.turn_id,
                "plan_id": plan.plan_id,
                "original_step_id": original_step.step_id,
                "replacement_step_id": replacement_step.step_id,
                "reason": reason,
                "envelope_validation_passed": envelope_outcome.valid,
            }
        )

    async def _emit_plan_reassembled(
        self,
        *,
        plan: Plan,
        new_plan: Plan,
        reason: str,
        triggering_context_summary: str,
        reassembly_count: int,
    ) -> None:
        """References-not-dumps: prior_plan_id and new_plan_id only.
        The plan payloads stay in their own audit references."""
        await self._emit(
            {
                "category": "enactment.plan_reassembled",
                "turn_id": plan.turn_id,
                "prior_plan_id": plan.plan_id,
                "new_plan_id": new_plan.plan_id,
                "reason": reason,
                "triggering_context_summary": triggering_context_summary,
                "reassembly_count": reassembly_count,
                "new_step_count": len(new_plan.steps),
            }
        )

    async def _emit(self, entry: dict[str, Any]) -> None:
        if self._audit is None:
            return
        try:
            await self._audit(entry)
        except Exception:
            logger.exception(
                "ENACTMENT_AUDIT_EMIT_FAILED category=%s",
                entry.get("category", "?"),
            )

    # ----- audit emission -----

    async def _emit_terminated(
        self,
        briefing: Briefing,
        subtype: TerminationSubtype,
        *,
        text: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Emit enactment.terminated with the chosen subtype.

        References-not-dumps for plan/briefing payloads (V1
        invariant). `extra` carries per-subtype metadata (e.g. the
        plan_id and reason for a B1 termination).

        Audit emission is best-effort — failures are logged and
        swallowed so an audit-store outage cannot break the user's
        turn.
        """
        if self._audit is None:
            return
        entry: dict[str, Any] = {
            "category": "enactment.terminated",
            "turn_id": briefing.turn_id,
            "integration_run_id": briefing.integration_run_id,
            "decided_action_kind": briefing.decided_action.kind.value,
            "subtype": subtype.value,
            "text_length": len(text),
        }
        if extra:
            entry.update(extra)
        try:
            await self._audit(entry)
        except Exception:
            logger.exception("ENACTMENT_AUDIT_EMIT_FAILED")


def build_enactment_service(
    *,
    presence_renderer: PresenceRendererLike,
    audit_emitter: AuditEmitter | None = None,
    planner: PlannerLike | None = None,
    step_dispatcher: StepDispatcherLike | None = None,
    divergence_reasoner: DivergenceReasonerLike | None = None,
    friction_observer: FrictionObserverLike | None = None,
) -> EnactmentService:
    """Convenience factory mirroring the constructor."""
    return EnactmentService(
        presence_renderer=presence_renderer,
        audit_emitter=audit_emitter,
        planner=planner,
        step_dispatcher=step_dispatcher,
        divergence_reasoner=divergence_reasoner,
        friction_observer=friction_observer,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _step_outcome_summary(
    step: Step, dispatch_result: StepDispatchResult
) -> str:
    """Compact, redacted per-step summary for the trace's
    tool_outcomes_summary aggregator. Never includes argument values
    or output payloads — references only."""
    if dispatch_result.completed:
        outcome = "completed"
    else:
        outcome = f"failed:{dispatch_result.failure_kind.value}"
    return (
        f"step {step.step_id} ({step.tool_id}.{step.operation_name}): "
        f"{outcome}"
    )


def _serialise_step_result(
    step: Step, dispatch_result: StepDispatchResult,
) -> dict[str, str]:
    """Serialise a per-step enactment dispatch outcome for
    forwarding to the terminal renderer (ACTION-RESULT-FORWARDING-V1).

    The renderer's existing forwarded-tool-results infrastructure
    (``presence_renderer._format_forwarded_tool_results``) expects
    ``{tool_name: str, result: str}``. The full dispatch result —
    completion flag, output dict, failure classification, error
    summary, corrective signal, duration — is JSON-serialised into
    the ``result`` field so the model sees structured outcomes for
    every attempt, including failures, not just successes.

    ``tool_name`` uses the step's tool_id (the actual surface the
    LLM emitted a tool_use for); the operation_name is folded into
    the JSON payload so the model can distinguish overloaded
    surfaces. Default=str on json.dumps catches the rare non-JSON
    values that could appear in tool output payloads (Path, datetime,
    etc.) without breaking the forward.
    """
    payload = {
        "operation_name": step.operation_name,
        "completed": dispatch_result.completed,
        "output": dispatch_result.output,
        "failure_kind": dispatch_result.failure_kind.value,
        "error_summary": dispatch_result.error_summary,
        "corrective_signal": dispatch_result.corrective_signal,
        "duration_ms": dispatch_result.duration_ms,
    }
    return {
        "tool_name": step.tool_id,
        "result": json.dumps(payload, default=str),
    }


def _briefing_with_enactment_results(
    briefing: Briefing, results: list[dict[str, str]],
) -> Briefing:
    """Return a Briefing copy whose audit_trace carries the accumulated
    enactment-tier dispatch results in ``tool_results_during_enactment``.

    No-op when ``results`` is empty (returns the original briefing
    unchanged). Otherwise constructs a new AuditTrace with the
    extended field via ``dataclasses.replace`` — preserves every other
    audit field unchanged.
    """
    if not results:
        return briefing
    new_audit = replace(
        briefing.audit_trace,
        tool_results_during_enactment=tuple(results),
    )
    return replace(briefing, audit_trace=new_audit)


def _stamp_reassembled(plan: Plan, *, triggering_context_summary: str) -> Plan:
    """Return a copy of `plan` with `created_via='tier_4_reassemble'`.

    The Plan dataclass is frozen; we use `from_dict(to_dict(...))` to
    rebuild rather than `replace` to keep validation in the loop.
    Triggering context is intentionally not embedded in the Plan
    itself (it lives in the audit entry instead) — keeps the Plan
    payload clean.
    """
    if plan.created_via == "tier_4_reassemble":
        return plan
    payload = plan.to_dict()
    payload["created_via"] = "tier_4_reassemble"
    return Plan.from_dict(payload)


def _synthesize_clarification_briefing(
    original: Briefing, clarification: ClarificationNeeded
) -> Briefing:
    """Build a synthetic Briefing carrying the ClarificationNeeded
    variant for the thin-path renderer.

    The original briefing was an execute_tool briefing with an
    action_envelope. The synthetic briefing carries the
    clarification_needed variant and NO action_envelope (action_envelope
    must be None for non-action kinds per Briefing's structural rule).

    `presence_directive` is preserved from the original so the
    renderer has framing context. C7 prompt-tunes the renderer to
    handle the B2 case naturally.
    """
    return Briefing(
        relevant_context=original.relevant_context,
        filtered_context=original.filtered_context,
        decided_action=clarification,
        presence_directive=original.presence_directive,
        audit_trace=original.audit_trace,
        turn_id=original.turn_id,
        integration_run_id=original.integration_run_id,
        # action_envelope must be None for clarification_needed.
        action_envelope=None,
    )


__all__ = [
    "AuditEmitter",
    "EnactmentNotImplemented",
    "EnactmentOutcome",
    "EnactmentService",
    "PresenceRendererLike",
    "PresenceRenderResult",
    "TerminationSubtype",
    "build_enactment_service",
]
