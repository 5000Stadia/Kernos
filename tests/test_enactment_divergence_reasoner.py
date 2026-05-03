"""Tests for the concrete DivergenceReasoner (IWL C3).

Coverage:
  - Conforms to PDI's shipped DivergenceReasonerLike Protocol with
    all four methods.
  - judge_divergence: deterministic short-circuit when structured
    pass + completed; prose path invokes model with explicit
    divergence framing.
  - emit_modified_step: model produces modified Step for Tier-2.
  - emit_pivot_step: model produces replacement Step for Tier-3.
  - formulate_clarification: produces ClarificationFormulationResult
    with closed-enum ambiguity_type.
  - All four hooks compose with same chain_caller (v1 same-model
    default).
  - Synthetic tools name the locked 7-signal vocabulary.
  - Failure kinds are the closed FailureKind taxonomy.
"""

from __future__ import annotations

import pytest

from kernos.kernel.enactment.divergence_reasoner import (
    CLARIFICATION_TOOL_NAME,
    DEFAULT_REASONER_MAX_TOKENS,
    DivergenceReasoner,
    DivergenceReasonerError,
    JUDGE_DIVERGENCE_TOOL_NAME,
    MODIFIED_STEP_TOOL_NAME,
    PIVOT_STEP_TOOL_NAME,
    build_divergence_reasoner,
)
from kernos.kernel.enactment.plan import (
    SignalKind,
    Step,
    StepExpectation,
    StructuredSignal,
)
from kernos.kernel.enactment.service import (
    ClarificationFormulationInputs,
    ClarificationFormulationResult,
    DivergenceJudgeInputs,
    DivergenceJudgment,
    DivergenceReasonerLike,
    StepDispatchResult,
    TierThreePivotInputs,
    TierTwoModifyInputs,
)
from kernos.kernel.enactment.tiers import FailureKind
from kernos.kernel.integration.briefing import (
    ActionEnvelope,
    AuditTrace,
    Briefing,
    ExecuteTool,
)
from kernos.providers.base import ContentBlock, ProviderResponse


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _step(
    *,
    step_id: str = "s1",
    expectation: StepExpectation | None = None,
) -> Step:
    return Step(
        step_id=step_id,
        tool_id="email_send",
        arguments={"to": "x"},
        tool_class="email",
        operation_name="send",
        expectation=expectation or StepExpectation(prose="email sent"),
    )


def _briefing() -> Briefing:
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=ExecuteTool(tool_id="email_send", arguments={}),
        presence_directive="execute",
        audit_trace=AuditTrace(),
        turn_id="turn-dr",
        integration_run_id="run-dr",
        action_envelope=ActionEnvelope(
            intended_outcome="send the email",
            allowed_tool_classes=("email",),
            allowed_operations=("send",),
        ),
    )


def _tool_use(name: str, payload: dict) -> ContentBlock:
    return ContentBlock(type="tool_use", id="tu_1", name=name, input=payload)


def _resp(*blocks: ContentBlock) -> ProviderResponse:
    return ProviderResponse(
        content=list(blocks),
        stop_reason="tool_use",
        input_tokens=10,
        output_tokens=20,
    )


def _make_chain(payloads_by_tool: dict[str, dict], captured: list | None = None):
    """Build a chain_caller stub that returns a tool_use block matching
    the requested tool. payloads_by_tool maps tool_name → payload."""

    async def chain(system, messages, tools, max_tokens, **_):
        # Identify which tool the call is for via the tools list.
        tool_name = tools[0]["name"] if tools else ""
        if captured is not None:
            captured.append(
                {
                    "system": system,
                    "messages": messages,
                    "tools": tools,
                    "max_tokens": max_tokens,
                }
            )
        payload = payloads_by_tool.get(tool_name, {})
        return _resp(_tool_use(tool_name, payload))

    return chain


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_divergence_reasoner_conforms_to_protocol():
    reasoner = DivergenceReasoner(
        chain_caller=_make_chain({}),
    )
    assert isinstance(reasoner, DivergenceReasonerLike)


def test_factory_returns_reasoner():
    reasoner = build_divergence_reasoner(
        chain_caller=_make_chain({}),
    )
    assert isinstance(reasoner, DivergenceReasoner)


def test_default_max_tokens_documented():
    assert DEFAULT_REASONER_MAX_TOKENS == 1024


# ---------------------------------------------------------------------------
# judge_divergence — deterministic short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_divergence_short_circuits_on_clean_success():
    """When structured signals all passed AND dispatch completed,
    judge_divergence returns a clean judgment WITHOUT calling the
    model. Pin: chain_caller is never invoked."""
    captured = []
    reasoner = DivergenceReasoner(
        chain_caller=_make_chain({}, captured=captured),
    )
    judgment = await reasoner.judge_divergence(
        DivergenceJudgeInputs(
            step=_step(),
            expectation=StepExpectation(prose="x"),
            dispatch_result=StepDispatchResult(
                completed=True, output={"ok": True}
            ),
            structured_pass=True,
        )
    )
    assert judgment.effect_matches_expectation is True
    assert judgment.plan_still_valid is True
    assert judgment.failure_kind is FailureKind.NONE
    # Model NOT called.
    assert captured == []


@pytest.mark.asyncio
async def test_judge_divergence_invokes_model_when_structured_fails():
    """When the structured check failed, the prose path fires —
    model invoked with explicit divergence framing."""
    captured = []
    reasoner = DivergenceReasoner(
        chain_caller=_make_chain(
            {
                JUDGE_DIVERGENCE_TOOL_NAME: {
                    "effect_matches_expectation": False,
                    "plan_still_valid": True,
                    "failure_kind": "transient",
                },
            },
            captured=captured,
        ),
    )
    judgment = await reasoner.judge_divergence(
        DivergenceJudgeInputs(
            step=_step(),
            expectation=StepExpectation(prose="x"),
            dispatch_result=StepDispatchResult(
                completed=False, output={}
            ),
            structured_pass=False,
        )
    )
    assert judgment.failure_kind is FailureKind.TRANSIENT
    assert judgment.plan_still_valid is True
    # Model WAS called.
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_judge_divergence_invokes_model_when_dispatch_failed():
    """Even with structured_pass=True, a failed dispatch (completed=False)
    triggers the prose path."""
    captured = []
    reasoner = DivergenceReasoner(
        chain_caller=_make_chain(
            {
                JUDGE_DIVERGENCE_TOOL_NAME: {
                    "effect_matches_expectation": False,
                    "plan_still_valid": False,
                    "failure_kind": "information_divergence",
                },
            },
            captured=captured,
        ),
    )
    judgment = await reasoner.judge_divergence(
        DivergenceJudgeInputs(
            step=_step(),
            expectation=StepExpectation(prose="x"),
            dispatch_result=StepDispatchResult(
                completed=False, output={}
            ),
            structured_pass=True,
        )
    )
    assert judgment.failure_kind is FailureKind.INFORMATION_DIVERGENCE
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_judge_divergence_rejects_unknown_failure_kind():
    reasoner = DivergenceReasoner(
        chain_caller=_make_chain(
            {
                JUDGE_DIVERGENCE_TOOL_NAME: {
                    "effect_matches_expectation": False,
                    "plan_still_valid": False,
                    "failure_kind": "frobnicate",
                },
            }
        ),
    )
    with pytest.raises(DivergenceReasonerError, match="not one of"):
        await reasoner.judge_divergence(
            DivergenceJudgeInputs(
                step=_step(),
                expectation=StepExpectation(prose="x"),
                dispatch_result=StepDispatchResult(
                    completed=False, output={}
                ),
                structured_pass=False,
            )
        )


# ---------------------------------------------------------------------------
# emit_modified_step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_modified_step_returns_step_dataclass():
    reasoner = DivergenceReasoner(
        chain_caller=_make_chain(
            {
                MODIFIED_STEP_TOOL_NAME: {
                    "step_id": "modified-1",
                    "tool_id": "email_send",
                    "tool_class": "email",
                    "operation_name": "send",
                    "arguments": {"to": "x", "batch_size": 10},
                    "expectation": {"prose": "smaller batch should work"},
                },
            }
        ),
    )
    modified = await reasoner.emit_modified_step(
        TierTwoModifyInputs(
            original_step=_step(),
            dispatch_result=StepDispatchResult(
                completed=False,
                output={},
                failure_kind=FailureKind.CORRECTIVE_SIGNAL,
                corrective_signal="batch too large",
            ),
            briefing=_briefing(),
        )
    )
    assert isinstance(modified, Step)
    assert modified.step_id == "modified-1"
    assert modified.arguments["batch_size"] == 10


@pytest.mark.asyncio
async def test_emit_modified_step_raises_when_model_skips_finalize():
    reasoner = DivergenceReasoner(
        chain_caller=_make_chain({}),  # no payload → no tool_use
    )
    with pytest.raises(DivergenceReasonerError):
        await reasoner.emit_modified_step(
            TierTwoModifyInputs(
                original_step=_step(),
                dispatch_result=StepDispatchResult(
                    completed=False, output={}
                ),
                briefing=_briefing(),
            )
        )


# ---------------------------------------------------------------------------
# emit_pivot_step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_pivot_step_returns_step_dataclass():
    reasoner = DivergenceReasoner(
        chain_caller=_make_chain(
            {
                PIVOT_STEP_TOOL_NAME: {
                    "step_id": "pivot-1",
                    "tool_id": "email_send",
                    "tool_class": "email",
                    "operation_name": "send",
                    "arguments": {"to": "alt@example.com"},
                    "expectation": {"prose": "email to alt recipient"},
                },
            }
        ),
    )
    pivot = await reasoner.emit_pivot_step(
        TierThreePivotInputs(
            original_step=_step(),
            dispatch_result=StepDispatchResult(
                completed=True, output={"results": []}
            ),
            briefing=_briefing(),
        )
    )
    assert isinstance(pivot, Step)
    assert pivot.step_id == "pivot-1"


# ---------------------------------------------------------------------------
# formulate_clarification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_formulate_clarification_returns_structured_result():
    reasoner = DivergenceReasoner(
        chain_caller=_make_chain(
            {
                CLARIFICATION_TOOL_NAME: {
                    "question": "Did you mean Henry from work?",
                    "ambiguity_type": "target",
                    "blocking_ambiguity": "two recipients match",
                    "safe_question_context": "confirming target choice",
                    "attempted_action_summary": "started drafting email",
                    "discovered_information": "two Henry contacts found",
                },
            }
        ),
    )
    result = await reasoner.formulate_clarification(
        ClarificationFormulationInputs(
            failed_step=_step(),
            dispatch_result=StepDispatchResult(
                completed=False,
                output={},
                failure_kind=FailureKind.AMBIGUITY_NEEDS_USER,
            ),
            briefing=_briefing(),
        )
    )
    assert isinstance(result, ClarificationFormulationResult)
    assert result.question.startswith("Did you mean")
    assert result.ambiguity_type == "target"


@pytest.mark.asyncio
async def test_clarification_tool_schema_carries_closed_ambiguity_enum():
    """The ambiguity_type enum is closed: target | parameter | approach |
    intent | other. Exposed in the synthetic tool's JSON schema."""
    captured = []
    reasoner = DivergenceReasoner(
        chain_caller=_make_chain(
            {
                CLARIFICATION_TOOL_NAME: {
                    "question": "?",
                    "ambiguity_type": "target",
                    "blocking_ambiguity": "b",
                    "safe_question_context": "c",
                    "attempted_action_summary": "a",
                    "discovered_information": "d",
                },
            },
            captured=captured,
        ),
    )
    await reasoner.formulate_clarification(
        ClarificationFormulationInputs(
            failed_step=_step(),
            dispatch_result=StepDispatchResult(completed=False, output={}),
            briefing=_briefing(),
        )
    )
    tools = captured[0]["tools"]
    sig_enum = tools[0]["input_schema"]["properties"]["ambiguity_type"]["enum"]
    assert set(sig_enum) == {
        "target", "parameter", "approach", "intent", "other"
    }


# ---------------------------------------------------------------------------
# Same-model default — chain_caller invoked uniformly across hooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_chain_caller_invoked_for_all_four_methods():
    """v1 same-model default: every reasoner method uses the same
    chain caller. Per-hook differentiation deferred."""
    captured = []
    reasoner = DivergenceReasoner(
        chain_caller=_make_chain(
            {
                JUDGE_DIVERGENCE_TOOL_NAME: {
                    "effect_matches_expectation": False,
                    "plan_still_valid": True,
                    "failure_kind": "transient",
                },
                MODIFIED_STEP_TOOL_NAME: {
                    "step_id": "m",
                    "tool_id": "email_send",
                    "tool_class": "email",
                    "operation_name": "send",
                    "arguments": {},
                    "expectation": {"prose": "x"},
                },
                PIVOT_STEP_TOOL_NAME: {
                    "step_id": "p",
                    "tool_id": "email_send",
                    "tool_class": "email",
                    "operation_name": "send",
                    "arguments": {},
                    "expectation": {"prose": "x"},
                },
                CLARIFICATION_TOOL_NAME: {
                    "question": "?",
                    "ambiguity_type": "target",
                    "blocking_ambiguity": "b",
                    "safe_question_context": "c",
                    "attempted_action_summary": "a",
                    "discovered_information": "d",
                },
            },
            captured=captured,
        ),
    )

    await reasoner.judge_divergence(
        DivergenceJudgeInputs(
            step=_step(),
            expectation=StepExpectation(prose="x"),
            dispatch_result=StepDispatchResult(completed=False, output={}),
            structured_pass=False,
        )
    )
    await reasoner.emit_modified_step(
        TierTwoModifyInputs(
            original_step=_step(),
            dispatch_result=StepDispatchResult(completed=False, output={}),
            briefing=_briefing(),
        )
    )
    await reasoner.emit_pivot_step(
        TierThreePivotInputs(
            original_step=_step(),
            dispatch_result=StepDispatchResult(completed=True, output={}),
            briefing=_briefing(),
        )
    )
    await reasoner.formulate_clarification(
        ClarificationFormulationInputs(
            failed_step=_step(),
            dispatch_result=StepDispatchResult(completed=False, output={}),
            briefing=_briefing(),
        )
    )
    # All four hooks called the same chain_caller (4 captured calls).
    assert len(captured) == 4


# ---------------------------------------------------------------------------
# Synthetic tools list the locked 7-signal vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modified_step_schema_lists_seven_signal_kinds():
    captured = []
    reasoner = DivergenceReasoner(
        chain_caller=_make_chain(
            {
                MODIFIED_STEP_TOOL_NAME: {
                    "step_id": "m",
                    "tool_id": "x",
                    "tool_class": "email",
                    "operation_name": "send",
                    "arguments": {},
                    "expectation": {"prose": "x"},
                },
            },
            captured=captured,
        ),
    )
    await reasoner.emit_modified_step(
        TierTwoModifyInputs(
            original_step=_step(),
            dispatch_result=StepDispatchResult(completed=False, output={}),
            briefing=_briefing(),
        )
    )
    schema = captured[0]["tools"][0]["input_schema"]
    sig_enum = (
        schema["properties"]["expectation"]["properties"]
        ["structured"]["items"]["properties"]["kind"]["enum"]
    )
    assert set(sig_enum) == {k.value for k in SignalKind}
    assert len(sig_enum) == 7


# ---------------------------------------------------------------------------
# Streaming-disabled-by-construction (PDI invariant)
# ---------------------------------------------------------------------------


def test_divergence_judgment_has_no_streamed_field():
    """Per PDI invariant: DivergenceJudgment is structured data; no
    streamed field. C3 must not add one."""
    from dataclasses import fields
    names = {f.name for f in fields(DivergenceJudgment)}
    assert "streamed" not in names


def test_clarification_formulation_result_has_no_streamed_field():
    from dataclasses import fields
    names = {f.name for f in fields(ClarificationFormulationResult)}
    assert "streamed" not in names
