"""Concrete DivergenceReasoner implementing PDI's four-method
DivergenceReasonerLike Protocol (IWL C3).

Four methods (locked surface):

    judge_divergence(inputs) -> DivergenceJudgment
    emit_modified_step(inputs) -> Step
    emit_pivot_step(inputs) -> Step
    formulate_clarification(inputs) -> ClarificationFormulationResult

Per the design review edit (locked): the deterministic vs. prose split lives
INSIDE judge_divergence. Modify, pivot, and clarification
generation are SEPARATE hooks — they don't compose under a single
evaluate() call.

judge_divergence path semantics:
  - When the expectation has structured signals AND the structured
    check passed → effect_matches_expectation=True, plan_still_valid
    derived from FailureKind classification.
  - When the structured check failed → model invoked with explicit
    divergence framing (prose expectation, actual outcome, plan
    context, envelope) to classify the failure kind and judge
    plan_still_valid.
  - When the expectation has only prose → model evaluates prose
    expectation against actual outcome; runtime classifies failure
    kind from the model's verdict.

All four methods use the same chain caller (v1 same-model default
locked per the design review edit). Per-hook differentiation deferred until soak
telemetry justifies. Per-hook timing telemetry recorded so soak can
justify later optimization.

No streaming affordance: PDI Protocol return types are structured
data (DivergenceJudgment / Step / ClarificationFormulationResult),
not iterators. Streaming is unreachable from the reasoner code path.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from kernos.kernel.enactment.plan import (
    PlanValidationError,
    SignalKind,
    Step,
    StepExpectation,
    StructuredSignal,
    evaluate_expectation_signals,
)
from kernos.kernel.enactment.service import (
    ClarificationFormulationInputs,
    ClarificationFormulationResult,
    DivergenceJudgeInputs,
    DivergenceJudgment,
    StepDispatchResult,
    TierThreePivotInputs,
    TierTwoModifyInputs,
)
from kernos.kernel.enactment.tiers import FailureKind
from kernos.kernel.integration.briefing import (
    BriefingValidationError,
    CLARIFICATION_QUESTION_CAP,
    CLARIFICATION_BLOCKING_AMBIGUITY_CAP,
    CLARIFICATION_SAFE_QUESTION_CONTEXT_CAP,
    CLARIFICATION_ATTEMPTED_ACTION_SUMMARY_CAP,
    CLARIFICATION_DISCOVERED_INFORMATION_CAP,
)
from kernos.providers.base import ContentBlock, ProviderResponse


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DivergenceReasonerError(RuntimeError):
    """Raised when a reasoner method fails to produce well-formed output."""


# ---------------------------------------------------------------------------
# ChainCaller — same shape integration uses
# ---------------------------------------------------------------------------


ChainCaller = Callable[..., Awaitable[ProviderResponse]]


# ---------------------------------------------------------------------------
# Synthetic finalize tools (one per method)
# ---------------------------------------------------------------------------


JUDGE_DIVERGENCE_TOOL_NAME = "__judge_divergence__"
MODIFIED_STEP_TOOL_NAME = "__emit_modified_step__"
PIVOT_STEP_TOOL_NAME = "__emit_pivot_step__"
CLARIFICATION_TOOL_NAME = "__formulate_clarification__"


_AMBIGUITY_TYPES = ("target", "parameter", "approach", "intent", "other")


def _judge_divergence_tool_schema() -> dict[str, Any]:
    return {
        "name": JUDGE_DIVERGENCE_TOOL_NAME,
        "description": (
            "Emit the structured divergence judgment for the step's "
            "outcome. Use this when prose evaluation is needed; "
            "structured signals already passed/failed deterministically. "
            "Call exactly once."
        ),
        "input_schema": {
            "type": "object",
            "required": [
                "effect_matches_expectation",
                "plan_still_valid",
                "failure_kind",
            ],
            "properties": {
                "effect_matches_expectation": {"type": "boolean"},
                "plan_still_valid": {"type": "boolean"},
                "failure_kind": {
                    "type": "string",
                    "enum": [k.value for k in FailureKind],
                },
            },
        },
    }


def _step_input_schema() -> dict[str, Any]:
    """Reusable JSON schema for a single Step (modified or pivoted).

    Mirrors the planner's __finalize_plan__ step shape so the model
    has consistent input expectations across hooks.
    """
    return {
        "type": "object",
        "required": [
            "tool_id",
            "tool_class",
            "operation_name",
            "expectation",
        ],
        "properties": {
            "step_id": {"type": "string"},
            "tool_id": {"type": "string"},
            "tool_class": {"type": "string"},
            "operation_name": {"type": "string"},
            "arguments": {"type": "object"},
            "expectation": {
                "type": "object",
                "required": ["prose"],
                "properties": {
                    "prose": {"type": "string"},
                    "structured": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["kind"],
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": [
                                        k.value for k in SignalKind
                                    ],
                                },
                                "args": {"type": "object"},
                            },
                        },
                    },
                },
            },
        },
    }


def _modified_step_tool_schema() -> dict[str, Any]:
    return {
        "name": MODIFIED_STEP_TOOL_NAME,
        "description": (
            "Emit the modified step for a Tier-2 modify path. Same "
            "intent as original; different mechanism (e.g., smaller "
            "batch, different endpoint). Stay inside the envelope."
        ),
        "input_schema": _step_input_schema(),
    }


def _pivot_step_tool_schema() -> dict[str, Any]:
    return {
        "name": PIVOT_STEP_TOOL_NAME,
        "description": (
            "Emit the replacement step for a Tier-3 pivot path. The "
            "original step succeeded but the result re-shaped the plan. "
            "Produce a different intermediate goal that still serves "
            "the decided_action. Stay inside the envelope."
        ),
        "input_schema": _step_input_schema(),
    }


def _clarification_tool_schema() -> dict[str, Any]:
    return {
        "name": CLARIFICATION_TOOL_NAME,
        "description": (
            "Emit a structured clarification for a Tier-5 B2 path. "
            "Question: ≤200 chars, one user-facing sentence. "
            "ambiguity_type: target | parameter | approach | intent | "
            "other. Other fields capped per ClarificationPartialState."
        ),
        "input_schema": {
            "type": "object",
            "required": [
                "question",
                "ambiguity_type",
                "blocking_ambiguity",
                "safe_question_context",
                "attempted_action_summary",
                "discovered_information",
            ],
            "properties": {
                "question": {"type": "string"},
                "ambiguity_type": {
                    "type": "string",
                    "enum": list(_AMBIGUITY_TYPES),
                },
                "blocking_ambiguity": {"type": "string"},
                "safe_question_context": {"type": "string"},
                "attempted_action_summary": {"type": "string"},
                "discovered_information": {"type": "string"},
            },
        },
    }


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


_JUDGE_SYSTEM_PROMPT = """\
You are Kernos's divergence reasoner. A step has been dispatched and its
outcome needs evaluation against the expectation.

Your job: produce a structured judgment via __judge_divergence__.

  - effect_matches_expectation: did the dispatch result match what the
    expectation described (prose layer)? The structured-signal layer is
    already evaluated separately; you reason about prose match.
  - plan_still_valid: with the new state, does the rest of the plan
    still make sense? Information divergence (step succeeded but result
    re-shapes plan) → plan_still_valid=False.
  - failure_kind: closed enum
      none | transient | non_transient | corrective_signal |
      information_divergence | ambiguity_needs_user | envelope_violation

Pick the kind that most accurately describes the outcome. Be decisive;
the runtime routes on this.

Call __judge_divergence__ exactly once.
"""


_MODIFY_SYSTEM_PROMPT = """\
You are Kernos's divergence reasoner. A step's dispatch returned a
corrective signal — the same intent should be retried with a different
mechanism. Produce a modified step via __emit_modified_step__.

  - Keep the same intent as the original step.
  - Change the mechanism (smaller batch, different endpoint, etc.).
  - Stay inside the envelope (allowed_tool_classes,
    allowed_operations, forbidden_moves).
  - The runtime validates the modified step against the envelope; an
    envelope-violating step terminates B1.

Call __emit_modified_step__ exactly once.
"""


_PIVOT_SYSTEM_PROMPT = """\
You are Kernos's divergence reasoner. The original step succeeded, but
its result re-shaped the plan. Produce a replacement step via
__emit_pivot_step__.

  - The replacement step pursues a DIFFERENT intermediate goal that
    still serves the decided_action.
  - Stay inside the envelope.
  - The runtime validates against the envelope; violation → B1.

Call __emit_pivot_step__ exactly once.
"""


_CLARIFICATION_SYSTEM_PROMPT = """\
You are Kernos's divergence reasoner. Mid-action ambiguity surfaced and
the user must disambiguate. Produce a structured clarification via
__formulate_clarification__.

  - question: ONE concise user-facing sentence (≤200 chars).
  - ambiguity_type: pick one of target | parameter | approach | intent
    | other.
  - blocking_ambiguity: brief description of what cannot be resolved
    without the user (≤500 chars).
  - safe_question_context: user-safe context framing the question
    (≤500 chars). Do NOT include restricted material.
  - attempted_action_summary: what was being attempted before B2 fired
    (≤1000 chars).
  - discovered_information: what was learned that surfaced ambiguity
    (≤1000 chars). This field IS bounded but the renderer does NOT
    consume it — it stays in audit/reintegration only.

Call __formulate_clarification__ exactly once.
"""


def _serialize_step_for_prompt(step: Step) -> str:
    """Compact serialization of a Step for prompt context."""
    return json.dumps(step.to_dict(), default=str, indent=2)


def _serialize_dispatch_result(result: StepDispatchResult) -> str:
    """Compact serialization of a StepDispatchResult for prompt context."""
    return json.dumps(
        {
            "completed": result.completed,
            "output": result.output,
            "failure_kind": result.failure_kind.value,
            "error_summary": result.error_summary,
            "corrective_signal": result.corrective_signal,
            "duration_ms": result.duration_ms,
        },
        default=str,
        indent=2,
    )


def _serialize_envelope(briefing) -> str:
    envelope = briefing.action_envelope
    if envelope is None:
        return "(no envelope on briefing)"
    return json.dumps(envelope.to_dict(), indent=2)


# ---------------------------------------------------------------------------
# Tool-call output parsing helpers
# ---------------------------------------------------------------------------


def _find_tool_use(
    response: ProviderResponse, tool_name: str
) -> dict[str, Any]:
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            payload = block.input or {}
            if not isinstance(payload, dict):
                raise DivergenceReasonerError(
                    f"{tool_name} payload must be a dict; got "
                    f"{type(payload).__name__}"
                )
            return payload
    raise DivergenceReasonerError(
        f"model did not call {tool_name}"
    )


def _parse_step_from_payload(payload: dict[str, Any]) -> Step:
    expectation_raw = payload.get("expectation") or {"prose": " "}
    try:
        expectation = StepExpectation.from_dict(expectation_raw)
        return Step(
            step_id=str(payload.get("step_id") or "modified"),
            tool_id=str(payload.get("tool_id", "")),
            arguments=dict(payload.get("arguments", {}) or {}),
            tool_class=str(payload.get("tool_class", "")),
            operation_name=str(payload.get("operation_name", "")),
            expectation=expectation,
        )
    except PlanValidationError as exc:
        raise DivergenceReasonerError(
            f"step construction failed: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# DivergenceReasoner
# ---------------------------------------------------------------------------


DEFAULT_REASONER_MAX_TOKENS = 1024


class DivergenceReasoner:
    """Concrete DivergenceReasoner conforming to PDI's four-method
    DivergenceReasonerLike Protocol.

    All four methods compose with the same chain_caller (v1 same-model
    default). Per-hook timing/cost telemetry can be observed via the
    audit family if the wiring layer chooses; this class does not
    emit reasoning.* events directly (the response_delivery hook
    handles synthetic outer aggregation per the no-double-count
    invariant).
    """

    def __init__(
        self,
        *,
        chain_caller: ChainCaller,
        max_tokens: int = DEFAULT_REASONER_MAX_TOKENS,
    ) -> None:
        self._chain_caller = chain_caller
        self._max_tokens = max_tokens

    # ------------------------------------------------------------------
    # judge_divergence — deterministic vs. prose split lives HERE
    # ------------------------------------------------------------------

    async def judge_divergence(
        self, inputs: DivergenceJudgeInputs
    ) -> DivergenceJudgment:
        """Per the design review edit: the deterministic vs. prose split lives
        inside this method. When the expectation has structured
        signals, those evaluate as a pure function — no model call.
        When prose-only OR structured passed but a deeper match needs
        prose judgment, the model is invoked with explicit divergence
        framing.

        Returns a DivergenceJudgment carrying:
          - effect_matches_expectation
          - plan_still_valid
          - failure_kind
        """
        # Deterministic short-circuit: structured signals all passed,
        # dispatch completed cleanly → effect matches; assume plan
        # still valid (prose layer can override with a model call when
        # the runtime chooses to invoke this path with a deeper check).
        if inputs.structured_pass and inputs.dispatch_result.completed:
            # Fast path: clean success with structured pass. No model
            # call needed. Plan still valid; no failure.
            return DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            )

        # Otherwise invoke the model for prose judgment + classification.
        system = _JUDGE_SYSTEM_PROMPT
        user_message = _build_judge_user_message(inputs)
        messages = [{"role": "user", "content": user_message}]
        tools = [_judge_divergence_tool_schema()]

        response = await self._chain_caller(
            system, messages, tools, self._max_tokens
        )
        payload = _find_tool_use(response, JUDGE_DIVERGENCE_TOOL_NAME)
        return _parse_judgment(payload)

    # ------------------------------------------------------------------
    # emit_modified_step — Tier-2 modify ownership
    # ------------------------------------------------------------------

    async def emit_modified_step(
        self, inputs: TierTwoModifyInputs
    ) -> Step:
        """Tier-2 modify: same intent, different mechanism. The runtime
        validates the modified step against the envelope downstream;
        envelope violation → B1."""
        system = _MODIFY_SYSTEM_PROMPT
        user_message = _build_modify_user_message(inputs)
        messages = [{"role": "user", "content": user_message}]
        tools = [_modified_step_tool_schema()]

        response = await self._chain_caller(
            system, messages, tools, self._max_tokens
        )
        payload = _find_tool_use(response, MODIFIED_STEP_TOOL_NAME)
        return _parse_step_from_payload(payload)

    # ------------------------------------------------------------------
    # emit_pivot_step — Tier-3 pivot
    # ------------------------------------------------------------------

    async def emit_pivot_step(
        self, inputs: TierThreePivotInputs
    ) -> Step:
        """Tier-3 pivot: original step succeeded but result re-shaped
        plan. Replacement step pursues a different intermediate goal
        that still serves the decided_action."""
        system = _PIVOT_SYSTEM_PROMPT
        user_message = _build_pivot_user_message(inputs)
        messages = [{"role": "user", "content": user_message}]
        tools = [_pivot_step_tool_schema()]

        response = await self._chain_caller(
            system, messages, tools, self._max_tokens
        )
        payload = _find_tool_use(response, PIVOT_STEP_TOOL_NAME)
        return _parse_step_from_payload(payload)

    # ------------------------------------------------------------------
    # formulate_clarification — Tier-5 B2 (no same-turn integration re-entry)
    # ------------------------------------------------------------------

    async def formulate_clarification(
        self, inputs: ClarificationFormulationInputs
    ) -> ClarificationFormulationResult:
        """Tier-5 B2: produces ClarificationFormulationResult; the
        EnactmentService constructs ClarificationNeeded directly
        from this output (no integration call). Per the design review edit, NO
        same-turn integration re-entry — the dependency is
        structurally absent on EnactmentService."""
        system = _CLARIFICATION_SYSTEM_PROMPT
        user_message = _build_clarification_user_message(inputs)
        messages = [{"role": "user", "content": user_message}]
        tools = [_clarification_tool_schema()]

        response = await self._chain_caller(
            system, messages, tools, self._max_tokens
        )
        payload = _find_tool_use(response, CLARIFICATION_TOOL_NAME)
        return _parse_clarification(payload)


# ---------------------------------------------------------------------------
# User-message builders
# ---------------------------------------------------------------------------


def _build_judge_user_message(inputs: DivergenceJudgeInputs) -> str:
    """Per PDI's shipped DivergenceJudgeInputs shape: no briefing /
    envelope fields. The judge reasons about the step + expectation +
    dispatch result + structured-pass flag only. Envelope-aware
    routing happens upstream (classify_routing) and downstream
    (validate_step_against_envelope on tier-2/3 outputs)."""
    parts = []
    parts.append("## Step")
    parts.append(_serialize_step_for_prompt(inputs.step))
    parts.append("\n## Expectation")
    parts.append(json.dumps(inputs.expectation.to_dict(), indent=2))
    parts.append("\n## Dispatch result")
    parts.append(_serialize_dispatch_result(inputs.dispatch_result))
    parts.append("\n## Structured signals passed deterministically")
    parts.append(str(inputs.structured_pass))
    parts.append("\nProduce the judgment now via __judge_divergence__.")
    return "\n".join(parts)


def _build_modify_user_message(inputs: TierTwoModifyInputs) -> str:
    parts = []
    parts.append("## Original step")
    parts.append(_serialize_step_for_prompt(inputs.original_step))
    parts.append("\n## Dispatch result")
    parts.append(_serialize_dispatch_result(inputs.dispatch_result))
    if inputs.dispatch_result.corrective_signal:
        parts.append("\n## Corrective signal from tool")
        parts.append(inputs.dispatch_result.corrective_signal)
    parts.append("\n## Briefing envelope")
    parts.append(_serialize_envelope(inputs.briefing))
    parts.append("\nProduce the modified step via __emit_modified_step__.")
    return "\n".join(parts)


def _build_pivot_user_message(inputs: TierThreePivotInputs) -> str:
    parts = []
    parts.append("## Original step")
    parts.append(_serialize_step_for_prompt(inputs.original_step))
    parts.append("\n## Dispatch result")
    parts.append(_serialize_dispatch_result(inputs.dispatch_result))
    parts.append("\n## Briefing envelope")
    parts.append(_serialize_envelope(inputs.briefing))
    parts.append("\nProduce the pivot step via __emit_pivot_step__.")
    return "\n".join(parts)


def _build_clarification_user_message(
    inputs: ClarificationFormulationInputs,
) -> str:
    parts = []
    parts.append("## Failed step")
    parts.append(_serialize_step_for_prompt(inputs.failed_step))
    parts.append("\n## Dispatch result")
    parts.append(_serialize_dispatch_result(inputs.dispatch_result))
    parts.append("\n## Briefing presence_directive")
    parts.append(inputs.briefing.presence_directive)
    parts.append("\nProduce the clarification via __formulate_clarification__.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Output parsers
# ---------------------------------------------------------------------------


def _parse_judgment(payload: dict[str, Any]) -> DivergenceJudgment:
    raw_kind = payload.get("failure_kind", FailureKind.NONE.value)
    try:
        kind = FailureKind(raw_kind)
    except ValueError as exc:
        valid = ", ".join(k.value for k in FailureKind)
        raise DivergenceReasonerError(
            f"failure_kind {raw_kind!r} is not one of: {valid}"
        ) from exc
    return DivergenceJudgment(
        effect_matches_expectation=bool(
            payload.get("effect_matches_expectation", False)
        ),
        plan_still_valid=bool(payload.get("plan_still_valid", False)),
        failure_kind=kind,
    )


def _parse_clarification(
    payload: dict[str, Any]
) -> ClarificationFormulationResult:
    """Parse the model's clarification payload. Caps are NOT enforced
    here — the downstream ClarificationPartialState / ClarificationNeeded
    dataclasses enforce them at construction time. Over-cap output
    raises BriefingValidationError when EnactmentService constructs
    the variant; we surface that loudly so the model can be tuned."""
    return ClarificationFormulationResult(
        question=str(payload.get("question", "")),
        ambiguity_type=str(payload.get("ambiguity_type", "other")),
        blocking_ambiguity=str(payload.get("blocking_ambiguity", "")),
        safe_question_context=str(
            payload.get("safe_question_context", "")
        ),
        attempted_action_summary=str(
            payload.get("attempted_action_summary", "")
        ),
        discovered_information=str(
            payload.get("discovered_information", "")
        ),
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_divergence_reasoner(
    *,
    chain_caller: ChainCaller,
    max_tokens: int = DEFAULT_REASONER_MAX_TOKENS,
) -> DivergenceReasoner:
    return DivergenceReasoner(
        chain_caller=chain_caller, max_tokens=max_tokens
    )


__all__ = [
    "CLARIFICATION_TOOL_NAME",
    "ChainCaller",
    "DEFAULT_REASONER_MAX_TOKENS",
    "DivergenceReasoner",
    "DivergenceReasonerError",
    "JUDGE_DIVERGENCE_TOOL_NAME",
    "MODIFIED_STEP_TOOL_NAME",
    "PIVOT_STEP_TOOL_NAME",
    "build_divergence_reasoner",
]
