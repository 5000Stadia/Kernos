"""Tests for the safety-policy plumbing — COHORT-ADAPT-COVENANT C1.

Covers acceptance criteria 16, 17, 18 + the design review's load-bearing input
(safety-degraded fail-soft must never be respond_only):

  - IntegrationInputs.required_safety_cohort_failures field exists
    with a default empty tuple (backwards-compatible).
  - When non-empty, the integration runner injects safety-degradation
    guidance into the prompt body.
  - When non-empty, the runner post-validates the model's
    decided_action and forces fail-soft (to a Defer briefing) when
    the model produces respond_only / execute_tool / propose_tool.
  - When fail-soft engages on a safety-degraded turn for ANY reason
    (model bug, redaction violation, embedding error, etc.), the
    fallback briefing is a Defer — never a RespondOnly.
  - Control case: when required_safety_cohort_failures is empty,
    behavior is unchanged from V1 (respond_only is allowed; minimal
    fail-soft is RespondOnly).
  - build_integration_inputs_from_fan_out helper threads the field
    correctly from CohortFanOutResult → IntegrationInputs.
"""

from __future__ import annotations

import pytest

from kernos.kernel.cohorts import (
    CohortContext,
    CohortFanOutConfig,
    CohortFanOutRunner,
    CohortRegistry,
    ContextSpaceRef,
    Turn,
    build_integration_inputs_from_fan_out,
    make_synthetic_cohort,
)
from kernos.kernel.cohorts.descriptor import CohortFanOutResult
from kernos.kernel.cohorts.synthetic_test_cohort import SyntheticBehaviour
from kernos.kernel.integration import (
    Briefing,
    ConstrainedResponse,
    Defer,
    ExecuteTool,
    IntegrationConfig,
    IntegrationInputs,
    IntegrationRunner,
    Outcome,
    Pivot,
    ProposeTool,
    Public,
    RespondOnly,
    Restricted,
)
from kernos.providers.base import ContentBlock, ProviderResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resp(payload: dict) -> ProviderResponse:
    return ProviderResponse(
        content=[
            ContentBlock(
                type="tool_use",
                id="tu_finalize",
                name="__finalize_briefing__",
                input=payload,
            )
        ],
        stop_reason="tool_use",
        input_tokens=10,
        output_tokens=20,
    )


def _make_runner(payload: dict | None = None):
    audit: list[dict] = []

    async def chain(*_a, **_kw):
        return _resp(payload or {
            "relevant_context": [],
            "filtered_context": [],
            "decided_action": {"kind": "respond_only"},
            "presence_directive": "answer simply",
        })

    async def emit(entry):
        audit.append(entry)

    async def dispatcher(*_a, **_kw):
        return {}

    runner = IntegrationRunner(
        chain_caller=chain,
        read_only_dispatcher=dispatcher,
        audit_emitter=emit,
        config=IntegrationConfig(),
    )
    return runner, audit


def _ctx(*, turn_id="turn-safety") -> CohortContext:
    return CohortContext(
        member_id="m-1",
        user_message="hello",
        conversation_thread=(Turn("user", "hello"),),
        active_spaces=(ContextSpaceRef("default"),),
        turn_id=turn_id,
        instance_id="i-1",
        produced_at="2026-04-26T00:00:00+00:00",
    )


def _inputs(
    *,
    required_safety_cohort_failures: tuple[str, ...] = (),
    cohort_outputs: tuple = (),
    user_message: str = "hello",
    turn_id: str = "turn-safety",
) -> IntegrationInputs:
    return IntegrationInputs(
        user_message=user_message,
        conversation_thread=({"role": "user", "content": user_message},),
        cohort_outputs=cohort_outputs,
        surfaced_tools=(),
        active_context_spaces=(),
        member_id="m-1",
        instance_id="i-1",
        space_id="default",
        turn_id=turn_id,
        required_safety_cohort_failures=required_safety_cohort_failures,
    )


# ---------------------------------------------------------------------------
# Schema extension: field exists with default
# ---------------------------------------------------------------------------


def test_integration_inputs_field_defaults_to_empty_tuple():
    inputs = _inputs()
    assert inputs.required_safety_cohort_failures == ()


def test_integration_inputs_carries_failed_cohorts():
    inputs = _inputs(required_safety_cohort_failures=("covenant",))
    assert inputs.required_safety_cohort_failures == ("covenant",)


# ---------------------------------------------------------------------------
# Backwards compat: field empty → V1 behavior unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_safety_failures_respond_only_still_allowed():
    """Control case: when required_safety_cohort_failures is empty,
    the integration runner accepts respond_only as it always has."""
    runner, audit = _make_runner()
    briefing = await runner.run(_inputs())
    assert isinstance(briefing, Briefing)
    assert isinstance(briefing.decided_action, RespondOnly)
    assert not briefing.audit_trace.fail_soft_engaged


@pytest.mark.asyncio
async def test_no_safety_failures_execute_tool_still_allowed():
    runner, _ = _make_runner({
        "relevant_context": [],
        "filtered_context": [],
        "decided_action": {
            "kind": "execute_tool",
            "tool_id": "drive_read_doc",
            "arguments": {"file_id": "abc"},
            "narration_context": "reading the doc",
        },
        "presence_directive": "execute and narrate",
        # PDI the design review edit: action-shape decided_actions REQUIRE an
        # explicit ActionEnvelope. Integration constructs the envelope
        # alongside the decision; the runner rejects action-shape
        # output without one.
        "action_envelope": {
            "intended_outcome": "read the requested document",
            "allowed_tool_classes": ["drive"],
            "allowed_operations": ["read"],
        },
    })
    briefing = await runner.run(_inputs())
    assert isinstance(briefing.decided_action, ExecuteTool)
    assert briefing.action_envelope is not None
    assert briefing.action_envelope.intended_outcome.startswith("read")


# ---------------------------------------------------------------------------
# Safety policy: prompt injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safety_degradation_guidance_appears_in_prompt():
    captured = {}

    async def chain(system, messages, tools, max_tokens, **_):
        captured["body"] = messages[0]["content"]
        return _resp({
            "relevant_context": [],
            "filtered_context": [],
            "decided_action": {
                "kind": "defer",
                "reason": "safety degraded",
                "follow_up_signal": "retry once cohort recovers",
            },
            "presence_directive": "acknowledge and defer",
        })

    async def dispatcher(*_a, **_kw):
        return {}

    async def emit(entry):
        pass

    runner = IntegrationRunner(
        chain_caller=chain,
        read_only_dispatcher=dispatcher,
        audit_emitter=emit,
        config=IntegrationConfig(),
    )
    await runner.run(_inputs(required_safety_cohort_failures=("covenant",)))
    body = captured["body"]
    assert "<safety_policy>" in body
    assert "covenant" in body
    assert "defer" in body.lower()
    assert "respond_only" in body
    assert "forbidden" in body.lower()


@pytest.mark.asyncio
async def test_safety_preamble_absent_when_no_failures():
    captured = {}

    async def chain(system, messages, tools, max_tokens, **_):
        captured["body"] = messages[0]["content"]
        return _resp({
            "relevant_context": [],
            "filtered_context": [],
            "decided_action": {"kind": "respond_only"},
            "presence_directive": "answer simply",
        })

    async def dispatcher(*_a, **_kw):
        return {}

    async def emit(entry):
        pass

    runner = IntegrationRunner(
        chain_caller=chain,
        read_only_dispatcher=dispatcher,
        audit_emitter=emit,
        config=IntegrationConfig(),
    )
    await runner.run(_inputs())
    assert "<safety_policy>" not in captured["body"]


# ---------------------------------------------------------------------------
# Safety policy: post-finalize coercion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safety_failure_forces_defer_when_model_returns_respond_only():
    """Acceptance criterion 17: covenant cohort error →
    required_safety_cohort_failures non-empty → briefing's
    decided_action is defer or constrained_response (not respond_only)."""
    runner, audit = _make_runner({
        "relevant_context": [],
        "filtered_context": [],
        "decided_action": {"kind": "respond_only"},  # model disobeyed
        "presence_directive": "answer simply",
    })
    briefing = await runner.run(
        _inputs(required_safety_cohort_failures=("covenant",))
    )
    # The model produced respond_only; runner must have coerced via
    # fail-soft to a Defer briefing.
    assert isinstance(briefing.decided_action, Defer)
    assert briefing.audit_trace.fail_soft_engaged is True
    assert briefing.audit_trace.budget_state.required_safety_cohort_failed is True
    assert "covenant" in briefing.decided_action.follow_up_signal
    assert audit[0]["success"] is False


@pytest.mark.asyncio
async def test_safety_failure_forces_defer_when_model_returns_execute_tool():
    runner, _ = _make_runner({
        "relevant_context": [],
        "filtered_context": [],
        "decided_action": {
            "kind": "execute_tool",
            "tool_id": "send_email",
            "arguments": {},
            "narration_context": "sending",
        },
        "presence_directive": "execute",
    })
    briefing = await runner.run(
        _inputs(required_safety_cohort_failures=("covenant",))
    )
    assert isinstance(briefing.decided_action, Defer)


@pytest.mark.asyncio
async def test_safety_failure_forces_defer_when_model_returns_propose_tool():
    runner, _ = _make_runner({
        "relevant_context": [],
        "filtered_context": [],
        "decided_action": {
            "kind": "propose_tool",
            "tool_id": "send_email",
            "arguments": {},
            "reason": "ask user",
        },
        "presence_directive": "propose",
    })
    briefing = await runner.run(
        _inputs(required_safety_cohort_failures=("covenant",))
    )
    assert isinstance(briefing.decided_action, Defer)


@pytest.mark.asyncio
async def test_safety_failure_accepts_defer_when_model_complies():
    runner, _ = _make_runner({
        "relevant_context": [],
        "filtered_context": [],
        "decided_action": {
            "kind": "defer",
            "reason": "covenant cohort failed",
            "follow_up_signal": "retry once recovered",
        },
        "presence_directive": "acknowledge and defer",
    })
    briefing = await runner.run(
        _inputs(required_safety_cohort_failures=("covenant",))
    )
    assert isinstance(briefing.decided_action, Defer)
    # Compliant briefing → not fail-soft.
    assert briefing.audit_trace.fail_soft_engaged is False


@pytest.mark.asyncio
async def test_safety_failure_accepts_constrained_response_when_model_complies():
    runner, _ = _make_runner({
        "relevant_context": [],
        "filtered_context": [],
        "decided_action": {
            "kind": "constrained_response",
            "constraint": "covenant verification unavailable",
            "satisfaction_partial": "respond cautiously",
        },
        "presence_directive": "answer cautiously",
    })
    briefing = await runner.run(
        _inputs(required_safety_cohort_failures=("covenant",))
    )
    assert isinstance(briefing.decided_action, ConstrainedResponse)
    assert briefing.audit_trace.fail_soft_engaged is False


@pytest.mark.asyncio
async def test_safety_failure_rejects_pivot():
    """Pivot is also forbidden — only defer + constrained_response are
    acceptable on a safety-degraded turn."""
    runner, _ = _make_runner({
        "relevant_context": [],
        "filtered_context": [],
        "decided_action": {
            "kind": "pivot",
            "reason": "covenant constraint",
            "suggested_shape": "redirect",
        },
        "presence_directive": "redirect gently",
    })
    briefing = await runner.run(
        _inputs(required_safety_cohort_failures=("covenant",))
    )
    assert isinstance(briefing.decided_action, Defer)


# ---------------------------------------------------------------------------
# the design review's load-bearing input: safety-degraded fail-soft is never respond_only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safety_degraded_fail_soft_is_defer_not_respond_only():
    """When the model produces no tool_use AND safety is degraded,
    fail-soft is a Defer briefing — NOT the standard
    minimal_fail_soft_briefing's RespondOnly."""

    async def chain(*_a, **_kw):
        # No tool_use block at all.
        return ProviderResponse(
            content=[ContentBlock(type="text", text="just thinking")],
            stop_reason="end_turn",
            input_tokens=10,
            output_tokens=20,
        )

    async def dispatcher(*_a, **_kw):
        return {}

    async def emit(entry):
        pass

    runner = IntegrationRunner(
        chain_caller=chain,
        read_only_dispatcher=dispatcher,
        audit_emitter=emit,
        config=IntegrationConfig(),
    )
    briefing = await runner.run(
        _inputs(required_safety_cohort_failures=("covenant",))
    )
    assert isinstance(briefing.decided_action, Defer)
    assert briefing.audit_trace.fail_soft_engaged is True
    assert briefing.audit_trace.budget_state.required_safety_cohort_failed


@pytest.mark.asyncio
async def test_safety_degraded_fail_soft_on_iteration_exhaustion_is_defer():
    """Same invariant on the iteration-exhaustion fail-soft path."""

    async def chain(*_a, **_kw):
        # Always asks for a tool call, never finalizes.
        return ProviderResponse(
            content=[
                ContentBlock(
                    type="tool_use",
                    id="tu_loop",
                    name="search_memory",
                    input={"q": "loop"},
                )
            ],
            stop_reason="tool_use",
            input_tokens=10,
            output_tokens=20,
        )

    async def dispatcher(*_a, **_kw):
        return {}

    async def emit(entry):
        pass

    from kernos.kernel.integration import SurfacedTool
    surfaced = (
        SurfacedTool(
            tool_id="search_memory",
            description="Search.",
            input_schema={"type": "object"},
            gate_classification="read",
            surfacing_rationale="x",
        ),
    )

    runner = IntegrationRunner(
        chain_caller=chain,
        read_only_dispatcher=dispatcher,
        audit_emitter=emit,
        config=IntegrationConfig(max_iterations=2),
    )
    inputs = IntegrationInputs(
        user_message="hi",
        conversation_thread=({"role": "user", "content": "hi"},),
        cohort_outputs=(),
        surfaced_tools=surfaced,
        active_context_spaces=(),
        member_id="m-1",
        instance_id="i-1",
        space_id="default",
        turn_id="turn-iter",
        required_safety_cohort_failures=("covenant",),
    )
    briefing = await runner.run(inputs)
    assert isinstance(briefing.decided_action, Defer)
    assert briefing.audit_trace.fail_soft_engaged
    assert briefing.audit_trace.budget_state.iterations_hit_limit


@pytest.mark.asyncio
async def test_no_safety_failures_fail_soft_remains_respond_only():
    """Control case: V1 fail-soft is unchanged when safety is not
    degraded. Pre-CAC behavior preserved."""

    async def chain(*_a, **_kw):
        return ProviderResponse(
            content=[ContentBlock(type="text", text="just text")],
            stop_reason="end_turn",
            input_tokens=10,
            output_tokens=20,
        )

    async def dispatcher(*_a, **_kw):
        return {}

    async def emit(entry):
        pass

    runner = IntegrationRunner(
        chain_caller=chain,
        read_only_dispatcher=dispatcher,
        audit_emitter=emit,
        config=IntegrationConfig(),
    )
    briefing = await runner.run(_inputs())
    assert isinstance(briefing.decided_action, RespondOnly)
    assert briefing.audit_trace.fail_soft_engaged
    assert not briefing.audit_trace.budget_state.required_safety_cohort_failed


# ---------------------------------------------------------------------------
# Acceptance criterion 18: control case (non-safety cohort failure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_required_non_safety_cohort_failure_does_not_force_defer():
    """A required cohort with safety_class=False that fails does NOT
    populate required_safety_cohort_failures — only safety_class
    cohorts do. Integration proceeds with normal action options."""
    inputs = _inputs(
        # Empty: gardener-style failure does NOT land here.
        required_safety_cohort_failures=(),
    )
    runner, _ = _make_runner({
        "relevant_context": [],
        "filtered_context": [],
        "decided_action": {"kind": "respond_only"},
        "presence_directive": "answer simply",
    })
    briefing = await runner.run(inputs)
    assert isinstance(briefing.decided_action, RespondOnly)


# ---------------------------------------------------------------------------
# build_integration_inputs_from_fan_out helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_integration_inputs_threads_safety_failures():
    """End-to-end: fan-out with a required+safety_class cohort that
    fails → CohortFanOutResult.required_safety_cohort_failures
    populated → helper threads it into IntegrationInputs."""
    safety_cohort = make_synthetic_cohort(
        "covenant",
        behaviour=SyntheticBehaviour.RAISE,
        required=True,
        safety_class=True,
    )
    registry = CohortRegistry()
    registry.register(safety_cohort)
    audit: list[dict] = []

    async def emit(entry):
        audit.append(entry)

    fan_out = CohortFanOutRunner(
        registry=registry,
        audit_emitter=emit,
        config=CohortFanOutConfig(global_timeout_seconds=2.0),
    )
    result = await fan_out.run(_ctx())
    assert result.required_safety_cohort_failures == ("covenant",)

    inputs = build_integration_inputs_from_fan_out(
        result,
        user_message="hi",
        conversation_thread=({"role": "user", "content": "hi"},),
        member_id="m-1",
        instance_id="i-1",
        space_id="default",
        turn_id="turn-helper",
    )
    assert inputs.required_safety_cohort_failures == ("covenant",)


@pytest.mark.asyncio
async def test_build_integration_inputs_with_no_failures_carries_empty_tuple():
    safe_cohort = make_synthetic_cohort("ok", behaviour=SyntheticBehaviour.SUCCEED)
    registry = CohortRegistry()
    registry.register(safe_cohort)

    async def emit(entry):
        pass

    fan_out = CohortFanOutRunner(
        registry=registry,
        audit_emitter=emit,
        config=CohortFanOutConfig(global_timeout_seconds=2.0),
    )
    result = await fan_out.run(_ctx())
    inputs = build_integration_inputs_from_fan_out(
        result,
        user_message="hi",
        conversation_thread=({"role": "user", "content": "hi"},),
        member_id="m-1",
        instance_id="i-1",
        space_id="default",
        turn_id="turn-helper-2",
    )
    assert inputs.required_safety_cohort_failures == ()
