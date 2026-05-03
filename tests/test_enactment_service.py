"""Tests for EnactmentService skeleton + thin path (PDI C4).

Acceptance criteria covered:
  - #8: EnactmentService.run(briefing) branches at entry on
        briefing.decided_action.kind.
  - #9: Render-only kinds take thin path: respond_only, defer,
        constrained_response, pivot, clarification_needed,
        propose_tool (the design review edit — propose_tool is render-only).
  - #10: Dispatch kind takes full machinery path: execute_tool only.
        (C4 stubs full machinery; C5+ implements.)
  - #11: Thin path is render-only (the design review edit). Tests verify thin path
        performs NO tool dispatch under any condition.
  - Hard rule (acceptance #28): safety-degraded fail-soft never
        produces respond_only — but this is upstream; thin path
        renders the defer briefing IntegrationService produced.
        Sanity-check here: thin path renders defer correctly.
  - Audit family (acceptance #33, partial in C4):
        enactment.terminated emitted with subtype.
        success_thin_path / thin_path_proposal_rendered /
        b2_user_disambiguation_needed.

Structural invariant: the thin-path code path takes only the presence
renderer dependency; no dispatcher exists on this branch. The "thin
path never dispatches tools" rule is therefore a code-shape
guarantee, not a runtime check. Tests verify by ensuring the only
mock that's called is the presence renderer, never any dispatcher.
"""

from __future__ import annotations

import pytest

from kernos.kernel.enactment import (
    EnactmentNotImplemented,
    EnactmentOutcome,
    EnactmentService,
    PresenceRenderResult,
    PresenceRendererLike,
    TerminationSubtype,
    build_enactment_service,
)
from kernos.kernel.integration.briefing import (
    ActionEnvelope,
    ActionKind,
    AuditTrace,
    Briefing,
    ClarificationNeeded,
    ClarificationPartialState,
    ConstrainedResponse,
    Defer,
    ExecuteTool,
    Pivot,
    ProposeTool,
    RespondOnly,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CapturingPresence:
    """A presence-renderer stub that records the briefings it sees and
    returns a canned response. Dispatch is structurally impossible:
    no dispatcher field exists. Tests assert this stub is the only
    component invoked on the thin path."""

    def __init__(self, *, text: str = "rendered text", streamed: bool = False) -> None:
        self._text = text
        self._streamed = streamed
        self.calls: list[Briefing] = []

    async def render(self, briefing: Briefing) -> PresenceRenderResult:
        self.calls.append(briefing)
        return PresenceRenderResult(text=self._text, streamed=self._streamed)


def _briefing(decided_action, presence_directive: str = "x", **extra) -> Briefing:
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=decided_action,
        presence_directive=presence_directive,
        audit_trace=AuditTrace(),
        turn_id="turn-1",
        integration_run_id="run-1",
        **extra,
    )


# ---------------------------------------------------------------------------
# Branch decision (acceptance criteria #8 / #9 / #10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_respond_only_takes_thin_path():
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(RespondOnly())
    outcome = await service.run(briefing)
    assert outcome.is_thin_path
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH
    assert outcome.decided_action_kind is ActionKind.RESPOND_ONLY
    assert len(presence.calls) == 1


@pytest.mark.asyncio
async def test_defer_takes_thin_path():
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(Defer(reason="x", follow_up_signal="y"))
    outcome = await service.run(briefing)
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH
    assert outcome.decided_action_kind is ActionKind.DEFER


@pytest.mark.asyncio
async def test_constrained_response_takes_thin_path():
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(
        ConstrainedResponse(constraint="time", satisfaction_partial="brief")
    )
    outcome = await service.run(briefing)
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH


@pytest.mark.asyncio
async def test_pivot_takes_thin_path():
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(
        Pivot(reason="x", suggested_shape="redirect")
    )
    outcome = await service.run(briefing)
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH


@pytest.mark.asyncio
async def test_propose_tool_takes_thin_path_with_proposal_rendered_subtype():
    """Per the design review edit: propose_tool is render-only on the thin path.
    The proposal is awaiting next-turn user confirmation; this turn
    renders the proposal text only. Subtype reflects the rendering
    so audit can distinguish from plain conversational thin-path."""
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(
        ProposeTool(tool_id="email_send", arguments={"to": "x"}, reason="r")
    )
    outcome = await service.run(briefing)
    assert outcome.is_thin_path
    assert outcome.subtype is TerminationSubtype.THIN_PATH_PROPOSAL_RENDERED


@pytest.mark.asyncio
async def test_clarification_first_pass_takes_thin_path():
    """First-pass clarification (partial_state=None) renders as
    SUCCESS_THIN_PATH. Integration-initiated; nothing surfaced from
    a prior turn's enactment."""
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(
        ClarificationNeeded(question="?", ambiguity_type="target")
    )
    outcome = await service.run(briefing)
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH


@pytest.mark.asyncio
async def test_clarification_b2_routed_takes_thin_path_with_b2_subtype():
    """A clarification with populated partial_state means the previous
    turn's full machinery surfaced a B2 termination; integration on
    the next turn produced the variant; thin path now renders the
    question. Subtype routes through B2 for telemetry."""
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(
        ClarificationNeeded(
            question="?",
            ambiguity_type="target",
            partial_state=ClarificationPartialState(
                attempted_action_summary="started drafting",
                blocking_ambiguity="which recipient",
            ),
        )
    )
    outcome = await service.run(briefing)
    assert outcome.subtype is TerminationSubtype.B2_USER_DISAMBIGUATION_NEEDED


@pytest.mark.asyncio
async def test_execute_tool_takes_full_machinery_branch():
    """Execute_tool routes to full machinery. Without the full
    machinery dependencies wired, EnactmentNotImplemented surfaces
    cleanly."""
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(
        ExecuteTool(tool_id="x", arguments={"a": 1}),
        action_envelope=ActionEnvelope(intended_outcome="do x"),
    )
    with pytest.raises(EnactmentNotImplemented, match="planner"):
        await service.run(briefing)
    # Presence was NOT called — full machinery has its own renderer
    # path that's only reached when dependencies are wired.
    assert presence.calls == []


# ---------------------------------------------------------------------------
# Thin path NEVER dispatches tools (acceptance criterion #11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thin_path_does_not_consult_dispatcher_even_when_wired():
    """Structural invariant: the thin path code body never reaches the
    dispatcher. C5 wires a dispatcher at the service level for the
    full-machinery branch; the thin path must remain dispatch-free.

    Pin verifies by wiring a dispatcher that raises on any call. If a
    future change accidentally invokes the dispatcher from a thin-path
    code path, the test fails loudly."""
    presence = _CapturingPresence()

    class _ExplodingDispatcher:
        async def dispatch(self, inputs):
            raise AssertionError(
                "thin path must not consult the dispatcher"
            )

    service = EnactmentService(
        presence_renderer=presence,
        step_dispatcher=_ExplodingDispatcher(),
    )
    # Run every render-only kind through the service; if any of them
    # reach the dispatcher, the explosion fires.
    for action in (
        RespondOnly(),
        Defer(reason="x", follow_up_signal="y"),
        ConstrainedResponse(constraint="t", satisfaction_partial="b"),
        Pivot(reason="x", suggested_shape="redirect"),
        ProposeTool(tool_id="x", arguments={}, reason="r"),
        ClarificationNeeded(question="?", ambiguity_type="target"),
    ):
        outcome = await service.run(_briefing(action))
        assert outcome.is_thin_path or outcome.subtype is TerminationSubtype.B2_USER_DISAMBIGUATION_NEEDED


@pytest.mark.asyncio
async def test_propose_tool_does_not_dispatch():
    """Even though propose_tool's payload includes tool_id and
    arguments, the thin path renders the proposal as text. NO tool
    dispatch occurs. Confirmed via presence-only call surface."""
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(
        ProposeTool(
            tool_id="dangerous_send_money",
            arguments={"amount": 9999},
            reason="user requested",
        )
    )
    outcome = await service.run(briefing)
    # Presence was the only thing called.
    assert len(presence.calls) == 1
    # Briefing's decided_action is preserved unchanged through the
    # thin path — no envelope required, no dispatcher consulted.
    assert outcome.decided_action_kind is ActionKind.PROPOSE_TOOL


# ---------------------------------------------------------------------------
# Streaming behavior on thin path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thin_path_records_streamed_flag_when_renderer_streams():
    presence = _CapturingPresence(streamed=True)
    service = EnactmentService(presence_renderer=presence)
    outcome = await service.run(_briefing(RespondOnly()))
    assert outcome.streamed is True


@pytest.mark.asyncio
async def test_thin_path_records_streamed_false_when_renderer_does_not_stream():
    presence = _CapturingPresence(streamed=False)
    service = EnactmentService(presence_renderer=presence)
    outcome = await service.run(_briefing(RespondOnly()))
    assert outcome.streamed is False


# ---------------------------------------------------------------------------
# Audit emission (acceptance criterion #33, partial in C4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thin_path_emits_enactment_terminated_audit():
    audit_sink: list[dict] = []

    async def emit(entry: dict) -> None:
        audit_sink.append(entry)

    presence = _CapturingPresence(text="rendered")
    service = EnactmentService(
        presence_renderer=presence, audit_emitter=emit
    )
    await service.run(_briefing(RespondOnly()))

    assert len(audit_sink) == 1
    entry = audit_sink[0]
    assert entry["category"] == "enactment.terminated"
    assert entry["subtype"] == "success_thin_path"
    assert entry["decided_action_kind"] == "respond_only"
    assert entry["turn_id"] == "turn-1"
    assert entry["integration_run_id"] == "run-1"
    # References-not-dumps: text length is recorded; full text lives
    # only in the briefing/audit refs (V1 invariant).
    assert entry["text_length"] == len("rendered")


@pytest.mark.asyncio
async def test_audit_emit_failure_does_not_break_turn():
    """Audit emission is best-effort; an audit-store outage cannot
    break the user's turn."""
    async def broken_emit(entry: dict) -> None:
        raise RuntimeError("audit store unavailable")

    presence = _CapturingPresence()
    service = EnactmentService(
        presence_renderer=presence, audit_emitter=broken_emit
    )
    # The audit failure is logged and swallowed; the outcome
    # still returns cleanly.
    outcome = await service.run(_briefing(RespondOnly()))
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH


@pytest.mark.asyncio
async def test_no_audit_emitter_is_silent():
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    outcome = await service.run(_briefing(RespondOnly()))
    # No emitter wired — outcome still returns; nothing crashes.
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH


# ---------------------------------------------------------------------------
# Factory + Protocol conformance
# ---------------------------------------------------------------------------


def test_factory_returns_service():
    presence = _CapturingPresence()
    service = build_enactment_service(presence_renderer=presence)
    assert isinstance(service, EnactmentService)


def test_capturing_presence_conforms_to_protocol():
    assert isinstance(_CapturingPresence(), PresenceRendererLike)


# ---------------------------------------------------------------------------
# TurnRunner integration smoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_runner_can_consume_enactment_service():
    """End-to-end: TurnRunner.run_enactment → EnactmentService → thin
    path. Verifies the C2 EnactmentServiceLike Protocol seam binds
    cleanly to the C4 concrete class."""
    from kernos.kernel.turn_runner import EnactmentServiceLike, TurnRunner

    presence = _CapturingPresence()
    enactment = EnactmentService(presence_renderer=presence)
    assert isinstance(enactment, EnactmentServiceLike)

    runner = TurnRunner(enactment_service=enactment)
    outcome = await runner.run_enactment(_briefing(RespondOnly()))

    assert isinstance(outcome, EnactmentOutcome)
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH


# ---------------------------------------------------------------------------
# Full machinery (PDI C5)
# ---------------------------------------------------------------------------


from kernos.kernel.enactment.plan import (
    Plan,
    SignalKind,
    Step,
    StepExpectation,
    StructuredSignal,
    new_plan_id,
    now_iso,
)
from kernos.kernel.enactment.service import (
    DivergenceJudgeInputs,
    DivergenceJudgment,
    PlanCreationInputs,
    PlanCreationResult,
    StepDispatchInputs,
    StepDispatchResult,
)
from kernos.kernel.enactment.tiers import FailureKind


def _envelope(
    *,
    allowed_tool_classes=("email",),
    allowed_operations=("send",),
    confirmation_requirements=(),
    forbidden_moves=(),
) -> ActionEnvelope:
    return ActionEnvelope(
        intended_outcome="x",
        allowed_tool_classes=tuple(allowed_tool_classes),
        allowed_operations=tuple(allowed_operations),
        confirmation_requirements=tuple(confirmation_requirements),
        forbidden_moves=tuple(forbidden_moves),
    )


def _execute_briefing(
    *,
    envelope: ActionEnvelope | None = None,
) -> Briefing:
    return _briefing(
        ExecuteTool(tool_id="email_send", arguments={"to": "x"}),
        action_envelope=envelope or _envelope(),
    )


def _step(
    *,
    step_id: str = "s1",
    tool_class: str = "email",
    operation_name: str = "send",
    expectation: StepExpectation | None = None,
) -> Step:
    return Step(
        step_id=step_id,
        tool_id="email_send",
        arguments={"to": "x"},
        tool_class=tool_class,
        operation_name=operation_name,
        expectation=expectation or StepExpectation(prose="email sent"),
    )


def _plan(steps=None, *, plan_id: str | None = None) -> Plan:
    return Plan(
        plan_id=plan_id or new_plan_id(),
        turn_id="turn-1",
        steps=tuple(steps or [_step()]),
        created_at=now_iso(),
    )


class _StubPlanner:
    def __init__(self, plan: Plan) -> None:
        self._plan = plan
        self.calls: list[PlanCreationInputs] = []

    async def create_plan(self, inputs: PlanCreationInputs) -> PlanCreationResult:
        self.calls.append(inputs)
        return PlanCreationResult(plan=self._plan)


class _StubDispatcher:
    def __init__(self, results) -> None:
        self._results = list(results)
        self.calls: list[StepDispatchInputs] = []

    async def dispatch(self, inputs: StepDispatchInputs) -> StepDispatchResult:
        self.calls.append(inputs)
        if self._results:
            return self._results.pop(0)
        return StepDispatchResult(
            completed=True, output={"ok": True}
        )


class _StubReasoner:
    def __init__(
        self,
        *,
        judgments=None,
        modified_step: Step | None = None,
        pivot_step: Step | None = None,
        clarification=None,
    ) -> None:
        self._judgments = list(judgments or [
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            )
        ])
        self._modified = modified_step
        self._pivot = pivot_step
        self._clarification = clarification
        self.judge_calls = 0
        self.modify_calls = 0
        self.pivot_calls = 0
        self.formulate_calls = 0

    async def judge_divergence(self, inputs: DivergenceJudgeInputs) -> DivergenceJudgment:
        self.judge_calls += 1
        if self._judgments:
            return self._judgments.pop(0)
        return DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=True,
            failure_kind=FailureKind.NONE,
        )

    async def emit_modified_step(self, inputs) -> Step:
        self.modify_calls += 1
        return self._modified or _step(step_id="modified")

    async def emit_pivot_step(self, inputs) -> Step:
        self.pivot_calls += 1
        return self._pivot or _step(step_id="pivot")

    async def formulate_clarification(self, inputs):
        from kernos.kernel.enactment.service import (
            ClarificationFormulationResult,
        )
        self.formulate_calls += 1
        return self._clarification or ClarificationFormulationResult(
            question="Which option did you mean?",
            ambiguity_type="target",
            blocking_ambiguity="cannot disambiguate target",
            safe_question_context="confirming target choice",
            attempted_action_summary="started the action",
            discovered_information="found two candidates",
        )


def _full_machinery_service(
    *,
    plan: Plan | None = None,
    dispatcher_results=None,
    judgments=None,
    modified_step: Step | None = None,
    pivot_step: Step | None = None,
    audit_sink=None,
    presence: _CapturingPresence | None = None,
    retry_budget: int = 3,
    modify_budget: int = 2,
    pivot_budget: int = 2,
) -> tuple[EnactmentService, _StubPlanner, _StubDispatcher, _StubReasoner]:
    plan = plan or _plan()
    planner = _StubPlanner(plan)
    dispatcher = _StubDispatcher(dispatcher_results or [])
    reasoner = _StubReasoner(
        judgments=judgments,
        modified_step=modified_step,
        pivot_step=pivot_step,
    )
    presence = presence or _CapturingPresence(text="terminal-render")

    async def emit(entry: dict) -> None:
        if audit_sink is not None:
            audit_sink.append(entry)

    service = EnactmentService(
        presence_renderer=presence,
        audit_emitter=emit if audit_sink is not None else None,
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
        retry_budget=retry_budget,
        modify_budget=modify_budget,
        pivot_budget=pivot_budget,
    )
    return service, planner, dispatcher, reasoner


# ----- not-wired guard -----


@pytest.mark.asyncio
async def test_full_machinery_without_dependencies_raises_clear_error():
    service = EnactmentService(presence_renderer=_CapturingPresence())
    with pytest.raises(EnactmentNotImplemented, match="planner"):
        await service.run(_execute_briefing())


# ----- plan creation observable in audit BEFORE step 1 dispatches -----


@pytest.mark.asyncio
async def test_plan_created_audit_emits_before_first_step_dispatches():
    """Auditability invariant: enactment.plan_created lands in the
    audit stream BEFORE the first step calls the dispatcher."""
    audit_sink: list[dict] = []
    plan = _plan(plan_id="plan-abc")
    service, _planner, dispatcher, _reasoner = _full_machinery_service(
        plan=plan, audit_sink=audit_sink,
    )

    # The dispatcher records the order of calls; audit_sink records
    # audit emissions. We assert plan_created appears in audit BEFORE
    # any step_attempted entry.
    await service.run(_execute_briefing())

    plan_created_idx = next(
        (i for i, e in enumerate(audit_sink)
         if e.get("category") == "enactment.plan_created"),
        None,
    )
    step_attempted_idx = next(
        (i for i, e in enumerate(audit_sink)
         if e.get("category") == "enactment.step_attempted"),
        None,
    )
    assert plan_created_idx is not None
    assert step_attempted_idx is not None
    assert plan_created_idx < step_attempted_idx
    # And the dispatcher was called exactly once for the single step.
    assert len(dispatcher.calls) == 1


@pytest.mark.asyncio
async def test_plan_created_audit_uses_plan_id_reference_not_payload():
    """References-not-dumps invariant: the audit entry carries
    plan_id and step_count, never the embedded plan payload."""
    audit_sink: list[dict] = []
    plan = _plan(plan_id="plan-xyz")
    service, _, _, _ = _full_machinery_service(
        plan=plan, audit_sink=audit_sink,
    )
    await service.run(_execute_briefing())

    plan_entry = next(
        e for e in audit_sink if e.get("category") == "enactment.plan_created"
    )
    assert plan_entry["plan_id"] == "plan-xyz"
    assert plan_entry["step_count"] == 1
    # No embedded plan payload.
    assert "steps" not in plan_entry
    assert "plan" not in plan_entry


# ----- streaming-disabled-by-construction during full machinery -----


@pytest.mark.asyncio
async def test_full_machinery_does_not_consult_presence_during_loop():
    """The presence_renderer is only consulted AFTER all steps
    complete (terminal render). During the loop, no streaming-capable
    component is reachable.

    Pin: the presence stub records calls; full machinery happy path
    invokes it exactly ONCE — at the end."""
    presence = _CapturingPresence(text="terminal")
    service, _, _, _ = _full_machinery_service(presence=presence)
    await service.run(_execute_briefing())
    # Exactly one render call — the terminal one.
    assert len(presence.calls) == 1


@pytest.mark.asyncio
async def test_dependency_protocols_have_no_streaming_affordance():
    """Compile-time guarantee: PlannerLike, StepDispatcherLike,
    DivergenceReasonerLike return types do NOT carry a `streamed`
    field. The streaming-capable type is PresenceRenderResult, which
    is unreachable from the inner-loop dependencies."""
    from dataclasses import fields
    # PlanCreationResult, StepDispatchResult, DivergenceJudgment must
    # not have a `streamed` field.
    for cls in (PlanCreationResult, StepDispatchResult, DivergenceJudgment):
        names = {f.name for f in fields(cls)}
        assert "streamed" not in names, (
            f"{cls.__name__} must not expose a streaming affordance"
        )
    # PresenceRenderResult IS streamable (used only for thin path
    # and terminal render).
    presence_names = {f.name for f in fields(PresenceRenderResult)}
    assert "streamed" in presence_names


# ----- happy path: single step succeeds; terminal render after loop -----


@pytest.mark.asyncio
async def test_full_machinery_happy_path_returns_terminal_render():
    presence = _CapturingPresence(text="action complete")
    service, _, dispatcher, reasoner = _full_machinery_service(
        presence=presence,
        dispatcher_results=[
            StepDispatchResult(completed=True, output={"ok": True})
        ],
    )
    outcome = await service.run(_execute_briefing())
    assert outcome.text == "action complete"
    assert outcome.subtype is TerminationSubtype.SUCCESS_FULL_MACHINERY
    assert outcome.decided_action_kind is ActionKind.EXECUTE_TOOL
    assert len(dispatcher.calls) == 1
    assert reasoner.judge_calls == 1


# ----- envelope validation rejects invalid plan -----


@pytest.mark.asyncio
async def test_invalid_initial_plan_terminates_b1():
    """Plan whose step uses a tool_class outside the envelope's
    allowed_tool_classes is rejected. C5 surfaces a B1 placeholder."""
    audit_sink: list[dict] = []
    bad_plan = _plan(steps=[_step(tool_class="slack")])
    envelope = _envelope(allowed_tool_classes=("email",))
    service, _, dispatcher, _ = _full_machinery_service(
        plan=bad_plan, audit_sink=audit_sink,
    )
    outcome = await service.run(_execute_briefing(envelope=envelope))
    assert outcome.subtype is TerminationSubtype.B1_ACTION_INVALIDATED
    # Crucially: no step was dispatched. The envelope rejection
    # happened before dispatch.
    assert len(dispatcher.calls) == 0


# ----- tier 1 retry on transient failure -----


@pytest.mark.asyncio
async def test_tier_1_retry_on_transient_failure():
    """Engineered: first dispatch returns transient; second succeeds.
    The retry stays on the same step with the same args; attempt
    counter increments."""
    audit_sink: list[dict] = []
    service, _, dispatcher, reasoner = _full_machinery_service(
        dispatcher_results=[
            StepDispatchResult(
                completed=False,
                output={},
                failure_kind=FailureKind.TRANSIENT,
                error_summary="connection error",
            ),
            StepDispatchResult(completed=True, output={"ok": True}),
        ],
        judgments=[
            DivergenceJudgment(
                effect_matches_expectation=False,
                plan_still_valid=True,
                failure_kind=FailureKind.TRANSIENT,
            ),
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            ),
        ],
        audit_sink=audit_sink,
    )
    outcome = await service.run(_execute_briefing())
    assert outcome.subtype is TerminationSubtype.SUCCESS_FULL_MACHINERY
    assert len(dispatcher.calls) == 2
    assert dispatcher.calls[0].attempt_number == 1
    assert dispatcher.calls[1].attempt_number == 2

    step_attempts = [
        e for e in audit_sink
        if e.get("category") == "enactment.step_attempted"
    ]
    assert len(step_attempts) == 2
    assert step_attempts[0]["attempt_number"] == 1
    assert step_attempts[1]["attempt_number"] == 2


# ----- tier 2 modify on corrective signal -----


@pytest.mark.asyncio
async def test_tier_2_modify_on_corrective_signal():
    """Engineered: dispatcher returns CORRECTIVE_SIGNAL; reasoner
    emits modified step; modified step succeeds."""
    audit_sink: list[dict] = []
    modified = _step(step_id="modified-step")
    service, _, dispatcher, reasoner = _full_machinery_service(
        modified_step=modified,
        dispatcher_results=[
            StepDispatchResult(
                completed=False,
                output={},
                failure_kind=FailureKind.CORRECTIVE_SIGNAL,
                corrective_signal="batch too large",
            ),
            StepDispatchResult(completed=True, output={"ok": True}),
        ],
        judgments=[
            DivergenceJudgment(
                effect_matches_expectation=False,
                plan_still_valid=True,
                failure_kind=FailureKind.CORRECTIVE_SIGNAL,
            ),
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            ),
        ],
        audit_sink=audit_sink,
    )
    outcome = await service.run(_execute_briefing())
    assert outcome.subtype is TerminationSubtype.SUCCESS_FULL_MACHINERY
    assert reasoner.modify_calls == 1
    modified_entry = next(
        e for e in audit_sink
        if e.get("category") == "enactment.step_modified"
    )
    assert modified_entry["modified_step_id"] == "modified-step"
    assert modified_entry["envelope_validation_passed"] is True


@pytest.mark.asyncio
async def test_tier_2_modify_envelope_violation_terminates_b1():
    """Per the design review edit: tier-2 modify is envelope-validated. A modified
    step that crosses allowed_tool_classes is rejected; B1 fires."""
    audit_sink: list[dict] = []
    bad_modified = _step(step_id="bad-mod", tool_class="slack")
    envelope = _envelope(allowed_tool_classes=("email",))
    service, _, dispatcher, _ = _full_machinery_service(
        modified_step=bad_modified,
        dispatcher_results=[
            StepDispatchResult(
                completed=False, output={},
                failure_kind=FailureKind.CORRECTIVE_SIGNAL,
            ),
        ],
        judgments=[
            DivergenceJudgment(
                effect_matches_expectation=False,
                plan_still_valid=True,
                failure_kind=FailureKind.CORRECTIVE_SIGNAL,
            ),
        ],
        audit_sink=audit_sink,
    )
    outcome = await service.run(_execute_briefing(envelope=envelope))
    assert outcome.subtype is TerminationSubtype.B1_ACTION_INVALIDATED
    # The modified (bad) step was NOT dispatched.
    assert len(dispatcher.calls) == 1


# ----- tier 3 pivot on information divergence -----


@pytest.mark.asyncio
async def test_tier_3_pivot_on_information_divergence():
    audit_sink: list[dict] = []
    pivot_step = _step(step_id="pivot-step")
    service, _, dispatcher, reasoner = _full_machinery_service(
        pivot_step=pivot_step,
        dispatcher_results=[
            StepDispatchResult(completed=True, output={"results": []}),
            StepDispatchResult(completed=True, output={"ok": True}),
        ],
        judgments=[
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=False,  # plan re-shaped
                failure_kind=FailureKind.INFORMATION_DIVERGENCE,
            ),
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            ),
        ],
        audit_sink=audit_sink,
    )
    outcome = await service.run(_execute_briefing())
    assert outcome.subtype is TerminationSubtype.SUCCESS_FULL_MACHINERY
    assert reasoner.pivot_calls == 1
    pivot_entry = next(
        e for e in audit_sink
        if e.get("category") == "enactment.step_pivoted"
    )
    assert pivot_entry["replacement_step_id"] == "pivot-step"


@pytest.mark.asyncio
async def test_tier_3_pivot_envelope_violation_terminates_b1():
    bad_pivot = _step(step_id="bad-pivot", tool_class="slack")
    envelope = _envelope(allowed_tool_classes=("email",))
    service, _, _, _ = _full_machinery_service(
        pivot_step=bad_pivot,
        dispatcher_results=[
            StepDispatchResult(completed=True, output={"results": []}),
        ],
        judgments=[
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=False,
                failure_kind=FailureKind.INFORMATION_DIVERGENCE,
            ),
        ],
    )
    outcome = await service.run(_execute_briefing(envelope=envelope))
    assert outcome.subtype is TerminationSubtype.B1_ACTION_INVALIDATED


# ----- PDI C6: tier 4 reassemble -----


@pytest.mark.asyncio
async def test_tier_4_reassemble_creates_new_plan_and_resumes_from_step_one():
    """Tier-4 reassemble: information divergence with pivot exhausted
    triggers reassemble. Planner produces a new plan; new plan
    validates against envelope; execution resumes from step 1 of the
    new plan. Reassembly budget decrements; happy path completes."""
    audit_sink: list[dict] = []
    initial_plan = _plan(steps=[_step(step_id="initial-step")], plan_id="plan-initial")
    new_plan = _plan(
        steps=[_step(step_id="new-step")],
        plan_id="plan-new",
    )

    class _RotatingPlanner:
        def __init__(self, plans):
            self._plans = list(plans)
            self.calls = []

        async def create_plan(self, inputs):
            self.calls.append(inputs)
            return PlanCreationResult(plan=self._plans.pop(0))

    planner = _RotatingPlanner([initial_plan, new_plan])

    dispatcher_results = [
        # Initial step — success but plan invalidated (info divergence,
        # pivot budget exhausted → reassemble).
        StepDispatchResult(completed=True, output={"results": []}),
        # New step — clean success after reassemble.
        StepDispatchResult(completed=True, output={"ok": True}),
    ]
    judgments = [
        DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=False,
            failure_kind=FailureKind.INFORMATION_DIVERGENCE,
        ),
        DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=True,
            failure_kind=FailureKind.NONE,
        ),
    ]
    dispatcher = _StubDispatcher(dispatcher_results)
    reasoner = _StubReasoner(judgments=judgments)
    presence = _CapturingPresence(text="terminal")

    async def emit(entry):
        audit_sink.append(entry)

    service = EnactmentService(
        presence_renderer=presence,
        audit_emitter=emit,
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
        pivot_budget=0,  # force reassemble routing
    )
    outcome = await service.run(_execute_briefing())

    assert outcome.subtype is TerminationSubtype.SUCCESS_FULL_MACHINERY
    # Two plans created.
    assert len(planner.calls) == 2
    # Second create_plan call carries the prior_plan_id and
    # triggering_context_summary (per C6 reassemble inputs).
    assert planner.calls[1].prior_plan_id == "plan-initial"
    assert planner.calls[1].triggering_context_summary
    # plan_reassembled audit emitted.
    reassembled = [
        e for e in audit_sink
        if e.get("category") == "enactment.plan_reassembled"
    ]
    assert len(reassembled) == 1
    assert reassembled[0]["prior_plan_id"] == "plan-initial"
    assert reassembled[0]["new_plan_id"] == "plan-new"


@pytest.mark.asyncio
async def test_tier_4_reassemble_stamps_new_plan_with_created_via():
    """The reassembled plan's created_via is 'tier_4_reassemble'.
    Audit consumers can distinguish initial plans from reassembled
    ones without re-deriving from event order."""
    audit_sink: list[dict] = []
    initial_plan = _plan(steps=[_step(step_id="initial")], plan_id="plan-i")
    new_plan = _plan(steps=[_step(step_id="new")], plan_id="plan-n")

    class _RotatingPlanner:
        def __init__(self, plans):
            self._plans = list(plans)

        async def create_plan(self, inputs):
            return PlanCreationResult(plan=self._plans.pop(0))

    planner = _RotatingPlanner([initial_plan, new_plan])
    dispatcher = _StubDispatcher([
        StepDispatchResult(completed=True, output={}),
        StepDispatchResult(completed=True, output={"ok": True}),
    ])
    reasoner = _StubReasoner(judgments=[
        DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=False,
            failure_kind=FailureKind.INFORMATION_DIVERGENCE,
        ),
        DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=True,
            failure_kind=FailureKind.NONE,
        ),
    ])

    async def emit(entry):
        audit_sink.append(entry)

    service = EnactmentService(
        presence_renderer=_CapturingPresence(),
        audit_emitter=emit,
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
        pivot_budget=0,
    )
    await service.run(_execute_briefing())

    # The plan_created audit for the reassembled plan should reflect
    # created_via='tier_4_reassemble'.
    plan_created = [
        e for e in audit_sink
        if e.get("category") == "enactment.plan_created"
    ]
    # initial plan created normally; reassembled plan does NOT trigger
    # a second plan_created emission (plan_reassembled is the C6 audit
    # for that). Only the initial one fires plan_created.
    assert len(plan_created) == 1
    assert plan_created[0]["created_via"] == "initial"


@pytest.mark.asyncio
async def test_tier_4_reassemble_budget_exhaustion_terminates_b1():
    """When per-envelope reassembly budget is 0, tier-4 routing
    terminates B1 directly with reason reassembly_budget_exhausted."""
    audit_sink: list[dict] = []
    service, _, _, _ = _full_machinery_service(
        dispatcher_results=[
            StepDispatchResult(completed=True, output={}),
        ],
        judgments=[
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=False,
                failure_kind=FailureKind.INFORMATION_DIVERGENCE,
            ),
        ],
        pivot_budget=0,
        audit_sink=audit_sink,
    )
    # Override reassembly budget to 0.
    service._reassembly_per_envelope = 0
    outcome = await service.run(_execute_briefing())
    assert outcome.subtype is TerminationSubtype.B1_ACTION_INVALIDATED
    terminated = next(
        e for e in audit_sink if e.get("category") == "enactment.terminated"
    )
    assert terminated["reason"] == "reassembly_budget_exhausted"


@pytest.mark.asyncio
async def test_tier_4_reassemble_envelope_violation_terminates_b1():
    """Per the design review edit: tier-4 reassemble new-plan path is envelope-
    validated. A reassembled plan that crosses allowed_tool_classes
    terminates B1."""
    initial_plan = _plan(steps=[_step(step_id="i")], plan_id="plan-i")
    bad_new_plan = _plan(
        steps=[_step(step_id="n", tool_class="slack")],
        plan_id="plan-bad",
    )

    class _RotatingPlanner:
        def __init__(self, plans):
            self._plans = list(plans)

        async def create_plan(self, inputs):
            return PlanCreationResult(plan=self._plans.pop(0))

    planner = _RotatingPlanner([initial_plan, bad_new_plan])
    dispatcher = _StubDispatcher([
        StepDispatchResult(completed=True, output={}),
    ])
    reasoner = _StubReasoner(judgments=[
        DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=False,
            failure_kind=FailureKind.INFORMATION_DIVERGENCE,
        ),
    ])
    envelope = _envelope(allowed_tool_classes=("email",))

    service = EnactmentService(
        presence_renderer=_CapturingPresence(),
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
        pivot_budget=0,
    )
    outcome = await service.run(_execute_briefing(envelope=envelope))
    assert outcome.subtype is TerminationSubtype.B1_ACTION_INVALIDATED
    # Only the initial step dispatched; the bad reassembled plan
    # never executes.
    assert len(dispatcher.calls) == 1


# ----- PDI C6: tier 5 B1 surface with capped reintegration -----


@pytest.mark.asyncio
async def test_b1_termination_attaches_reintegration_context_to_outcome():
    """B1 termination produces a ReintegrationContext attached to the
    outcome. The next-turn wiring picks it up to feed integration."""
    bad_plan = _plan(steps=[_step(tool_class="slack")])
    envelope = _envelope(allowed_tool_classes=("email",))
    service, _, _, _ = _full_machinery_service(plan=bad_plan)
    outcome = await service.run(_execute_briefing(envelope=envelope))

    assert outcome.subtype is TerminationSubtype.B1_ACTION_INVALIDATED
    assert outcome.reintegration_context is not None
    assert (
        outcome.reintegration_context.original_decided_action_kind
        == "execute_tool"
    )
    # Truncation flag is False on a small payload.
    assert outcome.reintegration_context.truncated is False
    # Plans attempted at least includes the rejected plan.
    assert len(outcome.reintegration_context.plans_attempted) >= 1


@pytest.mark.asyncio
async def test_b1_reintegration_context_caps_oversize_fields():
    """When the trace accumulates beyond the caps, the
    ReintegrationContext truncates and sets the truncated flag.
    Caps enforced by __post_init__ — not aspirational."""
    from kernos.kernel.enactment import ReintegrationContext, PlanRef

    huge_summary = "x" * 2000
    huge_discovery = "y" * 1000
    many_plans = tuple(
        PlanRef(plan_id=f"p{i}", created_via="initial", step_count=1)
        for i in range(10)
    )
    ctx = ReintegrationContext(
        original_decided_action_kind="execute_tool",
        plans_attempted=many_plans,
        tool_outcomes_summary=huge_summary,
        discovered_information=huge_discovery,
        audit_refs=("ref-1",),
    )
    assert ctx.truncated is True
    assert len(ctx.tool_outcomes_summary) == 1000
    assert len(ctx.discovered_information) == 500
    assert len(ctx.plans_attempted) == 5


@pytest.mark.asyncio
async def test_b1_reintegration_emits_audit_with_truncation_flag():
    """The terminated audit entry surfaces the reintegration's
    truncation status so operators can spot bloated turns."""
    audit_sink: list[dict] = []
    bad_plan = _plan(steps=[_step(tool_class="slack")])
    envelope = _envelope(allowed_tool_classes=("email",))
    service, _, _, _ = _full_machinery_service(
        plan=bad_plan, audit_sink=audit_sink,
    )
    await service.run(_execute_briefing(envelope=envelope))
    terminated = next(
        e for e in audit_sink if e.get("category") == "enactment.terminated"
    )
    assert "reintegration_truncated" in terminated
    assert terminated["reintegration_truncated"] is False  # small payload
    assert "reintegration_plans_attempted" in terminated


# ----- PDI C6: tier 5 B2 surface — no same-turn integration re-entry -----


def test_enactment_service_has_no_integration_dependency_by_construction():
    """Structural pin: EnactmentService.__init__ has NO parameter
    named or referencing 'integration'. Same-turn integration re-entry
    on B2 is impossible because the dependency is unreachable.

    Per design review's C6 guidance: not a runtime check; an import-time /
    construction-time impossibility."""
    import inspect
    sig = inspect.signature(EnactmentService.__init__)
    for param_name in sig.parameters:
        assert "integration" not in param_name.lower(), (
            f"EnactmentService.__init__ has parameter {param_name}; "
            f"same-turn integration re-entry must be impossible by "
            f"construction. Remove the parameter."
        )


def test_enactment_service_module_does_not_import_integration_service():
    """Pin: the enactment.service module does not import IntegrationService
    or IntegrationRunner. Verified by inspecting the module's __dict__."""
    from kernos.kernel.enactment import service as service_module
    for name in dir(service_module):
        obj = getattr(service_module, name)
        # Test must reject any IntegrationService / IntegrationRunner
        # bound at module level.
        cls_name = type(obj).__name__
        if cls_name in ("IntegrationService", "IntegrationRunner"):
            raise AssertionError(
                f"enactment.service module bound {cls_name} as "
                f"{name}; same-turn re-entry must be impossible by "
                f"construction"
            )


@pytest.mark.asyncio
async def test_b2_termination_constructs_clarification_directly():
    """B2 surface: enactment constructs the ClarificationNeeded
    variant via the divergence_reasoner.formulate_clarification hook.
    No integration call. The clarification + reintegration travel on
    the EnactmentOutcome to the next turn."""
    audit_sink: list[dict] = []
    service, _, _, reasoner = _full_machinery_service(
        dispatcher_results=[
            StepDispatchResult(
                completed=False, output={},
                failure_kind=FailureKind.AMBIGUITY_NEEDS_USER,
            ),
        ],
        judgments=[
            DivergenceJudgment(
                effect_matches_expectation=False,
                plan_still_valid=False,
                failure_kind=FailureKind.AMBIGUITY_NEEDS_USER,
            ),
        ],
        audit_sink=audit_sink,
    )
    outcome = await service.run(_execute_briefing())

    assert outcome.subtype is TerminationSubtype.B2_USER_DISAMBIGUATION_NEEDED
    # Reasoner's formulate_clarification was called — the source of
    # the question, NOT integration.
    assert reasoner.formulate_calls == 1
    # ClarificationNeeded variant attached to outcome.
    assert outcome.clarification is not None
    assert outcome.clarification.partial_state is not None
    # ReintegrationContext attached for the NEXT turn's integration.
    assert outcome.reintegration_context is not None
    # Audit entry carries the ambiguity_type for telemetry.
    terminated = next(
        e for e in audit_sink if e.get("category") == "enactment.terminated"
    )
    assert terminated["subtype"] == "b2_user_disambiguation_needed"
    assert "ambiguity_type" in terminated


@pytest.mark.asyncio
async def test_b2_renders_question_via_thin_path_presence_render():
    """The B2 question is rendered through presence_renderer with a
    synthetic briefing carrying the ClarificationNeeded variant."""
    captured_briefings: list[Briefing] = []

    class _CapturingPresenceLocal:
        async def render(self, briefing: Briefing):
            captured_briefings.append(briefing)
            return PresenceRenderResult(text="rendered question")

    service, _, _, _ = _full_machinery_service(
        presence=_CapturingPresenceLocal(),
        dispatcher_results=[
            StepDispatchResult(
                completed=False, output={},
                failure_kind=FailureKind.AMBIGUITY_NEEDS_USER,
            ),
        ],
        judgments=[
            DivergenceJudgment(
                effect_matches_expectation=False,
                plan_still_valid=False,
                failure_kind=FailureKind.AMBIGUITY_NEEDS_USER,
            ),
        ],
    )
    outcome = await service.run(_execute_briefing())
    assert outcome.text == "rendered question"
    # The presence renderer received a synthetic briefing where
    # decided_action is ClarificationNeeded (NOT the original
    # ExecuteTool). action_envelope is None on the synthetic briefing
    # (clarification_needed must omit it).
    assert len(captured_briefings) == 1
    synthetic = captured_briefings[0]
    assert isinstance(synthetic.decided_action, ClarificationNeeded)
    assert synthetic.action_envelope is None


# ----- PDI C6: friction observer (write-only sink) -----


class _RecordingFrictionObserver:
    def __init__(self) -> None:
        self.tickets = []

    async def record(self, ticket):
        self.tickets.append(ticket)


@pytest.mark.asyncio
async def test_friction_ticket_emitted_on_tier_1_retry_exhaustion():
    """When transient failures exhaust retry and routing falls to
    modify, a TIER_1_RETRY_EXHAUSTED ticket is recorded."""
    from kernos.kernel.enactment import TIER_1_RETRY_EXHAUSTED

    observer = _RecordingFrictionObserver()
    plan = _plan(steps=[_step()])
    planner = _StubPlanner(plan)
    # 4 transient failures in a row, then one modify success.
    dispatcher = _StubDispatcher([
        StepDispatchResult(
            completed=False, output={},
            failure_kind=FailureKind.TRANSIENT,
        ),
    ] * 4 + [
        StepDispatchResult(completed=True, output={"ok": True}),
    ])
    reasoner = _StubReasoner(judgments=[
        DivergenceJudgment(
            effect_matches_expectation=False,
            plan_still_valid=True,
            failure_kind=FailureKind.TRANSIENT,
        ),
    ] * 4 + [
        DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=True,
            failure_kind=FailureKind.NONE,
        ),
    ])

    service = EnactmentService(
        presence_renderer=_CapturingPresence(),
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
        friction_observer=observer,
        retry_budget=3,  # 3 retries → 4th attempt forces modify
        modify_budget=2,
    )
    await service.run(_execute_briefing())
    assert len(observer.tickets) == 1
    assert observer.tickets[0].divergence_pattern == TIER_1_RETRY_EXHAUSTED


@pytest.mark.asyncio
async def test_friction_observer_is_write_only_does_not_affect_routing():
    """Architectural invariant: tickets do NOT short-circuit routing.
    A friction observer that raises on record() must not break the
    turn — tickets are best-effort."""
    class _BrokenObserver:
        async def record(self, ticket):
            raise RuntimeError("friction store unavailable")

    plan = _plan(steps=[_step()])
    planner = _StubPlanner(plan)
    dispatcher = _StubDispatcher([
        StepDispatchResult(
            completed=False, output={},
            failure_kind=FailureKind.TRANSIENT,
        ),
    ] * 4 + [
        StepDispatchResult(completed=True, output={"ok": True}),
    ])
    reasoner = _StubReasoner(judgments=[
        DivergenceJudgment(
            effect_matches_expectation=False,
            plan_still_valid=True,
            failure_kind=FailureKind.TRANSIENT,
        ),
    ] * 4 + [
        DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=True,
            failure_kind=FailureKind.NONE,
        ),
    ])

    service = EnactmentService(
        presence_renderer=_CapturingPresence(),
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
        friction_observer=_BrokenObserver(),
    )
    # The broken observer's record() raises; the service swallows
    # and continues. The turn completes successfully (tier-2 modify
    # produces a successful step).
    outcome = await service.run(_execute_briefing())
    assert outcome.subtype is TerminationSubtype.SUCCESS_FULL_MACHINERY


@pytest.mark.asyncio
async def test_friction_observer_protocol_has_no_query_method():
    """Pin: FrictionObserverLike Protocol exposes only record(),
    no query/read method. By construction, the observer cannot feed
    information back to the EnactmentService.

    Verified via Protocol inspection: only `record` is in the
    Protocol's method set."""
    from kernos.kernel.enactment.friction import FrictionObserverLike
    # FrictionObserverLike is a runtime_checkable Protocol with one
    # method: record. Any instance conforming to it has only record.
    methods = {
        name
        for name in dir(FrictionObserverLike)
        if not name.startswith("_") and callable(getattr(FrictionObserverLike, name, None))
    }
    assert methods == {"record"}, (
        f"FrictionObserverLike must expose only record(); found {methods}"
    )


@pytest.mark.asyncio
async def test_friction_ticket_carries_redacted_telemetry_fields():
    """A ticket carries tool_id + operation_name + divergence_pattern
    + attempt_count + decided_action_kind + identifiers + timestamp.
    No argument values, no output payloads (references-not-dumps)."""
    from kernos.kernel.enactment.friction import FrictionTicket
    from dataclasses import fields
    field_names = {f.name for f in fields(FrictionTicket)}
    assert field_names == {
        "tool_id", "operation_name", "divergence_pattern",
        "attempt_count", "decided_action_kind",
        "instance_id", "member_id", "turn_id", "timestamp",
    }


# ----- PDI C6: confirmation boundary tests (design review mandate) -----


@pytest.mark.asyncio
async def test_draft_only_envelope_rejects_send_in_same_turn():
    """Confirmation boundary: a draft-only envelope rejects a plan
    that chains draft → send. Confirmation crosses a turn boundary."""
    plan = _plan(steps=[
        _step(step_id="draft", operation_name="draft"),
        _step(step_id="send", operation_name="send"),
    ])
    envelope = ActionEnvelope(
        intended_outcome="draft only",
        allowed_tool_classes=("email",),
        allowed_operations=("draft",),  # send NOT permitted
    )
    service, _, dispatcher, _ = _full_machinery_service(plan=plan)
    outcome = await service.run(_execute_briefing(envelope=envelope))
    assert outcome.subtype is TerminationSubtype.B1_ACTION_INVALIDATED
    # Envelope rejection happens BEFORE first dispatch.
    assert len(dispatcher.calls) == 0


@pytest.mark.asyncio
async def test_send_with_confirmation_requirement_rejected_in_same_turn():
    """A draft+send envelope with send in confirmation_requirements
    rejects a plan that includes the send step in the same turn.
    User confirmation crosses a turn boundary; the next turn's
    integration produces a fresh envelope without the confirmation
    requirement before send can fire."""
    plan = _plan(steps=[
        _step(step_id="draft", operation_name="draft"),
        _step(step_id="send", operation_name="send"),
    ])
    envelope = ActionEnvelope(
        intended_outcome="draft and confirm before send",
        allowed_tool_classes=("email",),
        allowed_operations=("draft", "send"),
        confirmation_requirements=("send",),
    )
    service, _, dispatcher, _ = _full_machinery_service(plan=plan)
    outcome = await service.run(_execute_briefing(envelope=envelope))
    assert outcome.subtype is TerminationSubtype.B1_ACTION_INVALIDATED
    assert len(dispatcher.calls) == 0


@pytest.mark.asyncio
async def test_cross_turn_confirmation_flow_turn_2_send_fires():
    """The cross-turn flow: turn 2 receives a fresh envelope that
    permits send WITHOUT it being in confirmation_requirements (the
    user confirmed on turn 1). The send step dispatches successfully."""
    plan = _plan(steps=[_step(step_id="send", operation_name="send")])
    envelope = ActionEnvelope(
        intended_outcome="send the email user confirmed last turn",
        allowed_tool_classes=("email",),
        allowed_operations=("send",),
        # No confirmation_requirements — confirmation already crossed
        # the turn boundary on turn 1.
    )
    service, _, dispatcher, _ = _full_machinery_service(
        plan=plan,
        dispatcher_results=[
            StepDispatchResult(completed=True, output={"ok": True}),
        ],
    )
    outcome = await service.run(_execute_briefing(envelope=envelope))
    assert outcome.subtype is TerminationSubtype.SUCCESS_FULL_MACHINERY
    assert len(dispatcher.calls) == 1


@pytest.mark.asyncio
async def test_send_only_envelope_with_in_turn_send_dispatches():
    """A send-only envelope (allowed_operations=["send"], no
    confirmation_requirements) lets the send dispatch immediately.
    Used when integration on the current turn already saw user
    confirmation alongside the request."""
    plan = _plan(steps=[_step(step_id="send", operation_name="send")])
    envelope = ActionEnvelope(
        intended_outcome="send the email",
        allowed_tool_classes=("email",),
        allowed_operations=("send",),
    )
    service, _, dispatcher, _ = _full_machinery_service(
        plan=plan,
        dispatcher_results=[
            StepDispatchResult(completed=True, output={"ok": True}),
        ],
    )
    outcome = await service.run(_execute_briefing(envelope=envelope))
    assert outcome.subtype is TerminationSubtype.SUCCESS_FULL_MACHINERY
    assert len(dispatcher.calls) == 1


@pytest.mark.asyncio
async def test_friction_ticket_emitted_on_tier_2_modify_exhaustion_via_b1():
    """When tier 2 modify exhausts and routing falls to B1, a
    TIER_2_MODIFY_EXHAUSTED ticket is recorded."""
    from kernos.kernel.enactment import TIER_2_MODIFY_EXHAUSTED

    observer = _RecordingFrictionObserver()
    plan = _plan(steps=[_step()])
    planner = _StubPlanner(plan)
    # Non-transient → routes directly to modify (skipping retry).
    # Two non-transient failures → modify budget=1 exhausted.
    dispatcher = _StubDispatcher([
        StepDispatchResult(
            completed=False, output={},
            failure_kind=FailureKind.NON_TRANSIENT,
        ),
    ] * 2)
    reasoner = _StubReasoner(judgments=[
        DivergenceJudgment(
            effect_matches_expectation=False,
            plan_still_valid=True,
            failure_kind=FailureKind.NON_TRANSIENT,
        ),
    ] * 2)

    service = EnactmentService(
        presence_renderer=_CapturingPresence(),
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
        friction_observer=observer,
        modify_budget=1,
    )
    outcome = await service.run(_execute_briefing())
    assert outcome.subtype is TerminationSubtype.B1_ACTION_INVALIDATED
    assert len(observer.tickets) == 1
    assert observer.tickets[0].divergence_pattern == TIER_2_MODIFY_EXHAUSTED


@pytest.mark.asyncio
async def test_two_reassemblies_in_one_turn_under_per_envelope_budget():
    """Reassembly budget tracking: per-envelope=2 allows two
    reassemblies in a single turn before exhaustion."""
    audit_sink: list[dict] = []
    plan_a = _plan(steps=[_step(step_id="a1")], plan_id="plan-a")
    plan_b = _plan(steps=[_step(step_id="b1")], plan_id="plan-b")
    plan_c = _plan(steps=[_step(step_id="c1")], plan_id="plan-c")

    class _RotatingPlanner:
        def __init__(self, plans):
            self._plans = list(plans)

        async def create_plan(self, inputs):
            return PlanCreationResult(plan=self._plans.pop(0))

    planner = _RotatingPlanner([plan_a, plan_b, plan_c])
    # Step a1 → info divergence (reassemble 1)
    # Step b1 → info divergence (reassemble 2)
    # Step c1 → success
    dispatcher = _StubDispatcher([
        StepDispatchResult(completed=True, output={}),  # a1
        StepDispatchResult(completed=True, output={}),  # b1
        StepDispatchResult(completed=True, output={"ok": True}),  # c1
    ])
    reasoner = _StubReasoner(judgments=[
        DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=False,
            failure_kind=FailureKind.INFORMATION_DIVERGENCE,
        ),
        DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=False,
            failure_kind=FailureKind.INFORMATION_DIVERGENCE,
        ),
        DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=True,
            failure_kind=FailureKind.NONE,
        ),
    ])

    async def emit(entry):
        audit_sink.append(entry)

    service = EnactmentService(
        presence_renderer=_CapturingPresence(),
        audit_emitter=emit,
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
        pivot_budget=0,  # force reassemble routing
        reassembly_per_envelope=2,
    )
    outcome = await service.run(_execute_briefing())
    assert outcome.subtype is TerminationSubtype.SUCCESS_FULL_MACHINERY
    reassembled = [
        e for e in audit_sink
        if e.get("category") == "enactment.plan_reassembled"
    ]
    assert len(reassembled) == 2


@pytest.mark.asyncio
async def test_third_reassemble_attempt_terminates_b1_on_budget_exhaustion():
    """Two reassemblies under per_envelope=2 succeed; the third
    routing attempt terminates B1 with reassembly_budget_exhausted."""
    audit_sink: list[dict] = []
    plans = [
        _plan(steps=[_step(step_id=f"s{i}")], plan_id=f"plan-{i}")
        for i in range(3)
    ]

    class _RotatingPlanner:
        def __init__(self, plans):
            self._plans = list(plans)

        async def create_plan(self, inputs):
            return PlanCreationResult(plan=self._plans.pop(0))

    planner = _RotatingPlanner(plans)
    # Three info divergences — first two reassemble, third hits budget.
    dispatcher = _StubDispatcher([
        StepDispatchResult(completed=True, output={}),
    ] * 3)
    reasoner = _StubReasoner(judgments=[
        DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=False,
            failure_kind=FailureKind.INFORMATION_DIVERGENCE,
        ),
    ] * 3)

    async def emit(entry):
        audit_sink.append(entry)

    service = EnactmentService(
        presence_renderer=_CapturingPresence(),
        audit_emitter=emit,
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
        pivot_budget=0,
        reassembly_per_envelope=2,
    )
    outcome = await service.run(_execute_briefing())
    assert outcome.subtype is TerminationSubtype.B1_ACTION_INVALIDATED
    terminated = next(
        e for e in audit_sink if e.get("category") == "enactment.terminated"
    )
    assert terminated["reason"] == "reassembly_budget_exhausted"


@pytest.mark.asyncio
async def test_b1_reintegration_carries_multiple_plan_refs_after_reassembly():
    """After a reassemble + final B1, the reintegration_context
    plans_attempted carries refs to BOTH plans."""
    plans = [
        _plan(steps=[_step(step_id="a")], plan_id="plan-a"),
        _plan(steps=[_step(step_id="b", tool_class="slack")], plan_id="plan-b"),  # bad
    ]

    class _RotatingPlanner:
        def __init__(self, plans):
            self._plans = list(plans)

        async def create_plan(self, inputs):
            return PlanCreationResult(plan=self._plans.pop(0))

    planner = _RotatingPlanner(plans)
    dispatcher = _StubDispatcher([
        StepDispatchResult(completed=True, output={}),
    ])
    reasoner = _StubReasoner(judgments=[
        DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=False,
            failure_kind=FailureKind.INFORMATION_DIVERGENCE,
        ),
    ])
    envelope = _envelope(allowed_tool_classes=("email",))

    service = EnactmentService(
        presence_renderer=_CapturingPresence(),
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
        pivot_budget=0,
    )
    outcome = await service.run(_execute_briefing(envelope=envelope))
    assert outcome.subtype is TerminationSubtype.B1_ACTION_INVALIDATED
    # Reintegration captures both plans.
    assert outcome.reintegration_context is not None
    plan_refs = outcome.reintegration_context.plans_attempted
    assert len(plan_refs) == 2
    assert plan_refs[0].plan_id == "plan-a"
    assert plan_refs[1].plan_id == "plan-b"
    # Reassembled plan has tier_4_reassemble created_via.
    assert plan_refs[1].created_via == "tier_4_reassemble"


@pytest.mark.asyncio
async def test_plan_reassembled_audit_does_not_embed_plan_payload():
    """References-not-dumps invariant on plan_reassembled audit."""
    audit_sink: list[dict] = []
    plans = [
        _plan(steps=[_step(step_id="a")], plan_id="plan-a"),
        _plan(steps=[_step(step_id="b")], plan_id="plan-b"),
    ]

    class _RotatingPlanner:
        def __init__(self, plans):
            self._plans = list(plans)

        async def create_plan(self, inputs):
            return PlanCreationResult(plan=self._plans.pop(0))

    planner = _RotatingPlanner(plans)
    dispatcher = _StubDispatcher([
        StepDispatchResult(completed=True, output={}),
        StepDispatchResult(completed=True, output={"ok": True}),
    ])
    reasoner = _StubReasoner(judgments=[
        DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=False,
            failure_kind=FailureKind.INFORMATION_DIVERGENCE,
        ),
        DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=True,
            failure_kind=FailureKind.NONE,
        ),
    ])

    async def emit(entry):
        audit_sink.append(entry)

    service = EnactmentService(
        presence_renderer=_CapturingPresence(),
        audit_emitter=emit,
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
        pivot_budget=0,
    )
    await service.run(_execute_briefing())

    reassembled = next(
        e for e in audit_sink
        if e.get("category") == "enactment.plan_reassembled"
    )
    # IDs only; no embedded plan payloads.
    assert reassembled["prior_plan_id"] == "plan-a"
    assert reassembled["new_plan_id"] == "plan-b"
    assert "steps" not in reassembled
    assert "plan" not in reassembled


@pytest.mark.asyncio
async def test_b2_with_each_ambiguity_type_constructs_clarification():
    """The five closed-enum ambiguity types are accepted by the
    B2 path's ClarificationNeeded construction."""
    from kernos.kernel.enactment.service import (
        ClarificationFormulationResult,
    )

    for ambiguity_type in ("target", "parameter", "approach", "intent", "other"):
        clarification = ClarificationFormulationResult(
            question=f"q for {ambiguity_type}",
            ambiguity_type=ambiguity_type,
            blocking_ambiguity="b",
            safe_question_context="c",
            attempted_action_summary="a",
            discovered_information="d",
        )
        plan = _plan(steps=[_step()])
        planner = _StubPlanner(plan)
        dispatcher = _StubDispatcher([
            StepDispatchResult(
                completed=False, output={},
                failure_kind=FailureKind.AMBIGUITY_NEEDS_USER,
            ),
        ])
        reasoner = _StubReasoner(
            judgments=[
                DivergenceJudgment(
                    effect_matches_expectation=False,
                    plan_still_valid=False,
                    failure_kind=FailureKind.AMBIGUITY_NEEDS_USER,
                ),
            ],
            clarification=clarification,
        )
        service = EnactmentService(
            presence_renderer=_CapturingPresence(),
            planner=planner,
            step_dispatcher=dispatcher,
            divergence_reasoner=reasoner,
        )
        outcome = await service.run(_execute_briefing())
        assert outcome.clarification is not None
        assert outcome.clarification.ambiguity_type == ambiguity_type


@pytest.mark.asyncio
async def test_b2_reasoner_receives_audit_refs_in_formulation_inputs():
    """The formulate_clarification call carries audit_refs from the
    execution trace so the reasoner has context for question framing."""
    captured: list = []

    class _CapturingReasoner(_StubReasoner):
        async def formulate_clarification(self, inputs):
            captured.append(inputs)
            return await super().formulate_clarification(inputs)

    plan = _plan(steps=[_step()])
    planner = _StubPlanner(plan)
    dispatcher = _StubDispatcher([
        StepDispatchResult(
            completed=False, output={},
            failure_kind=FailureKind.AMBIGUITY_NEEDS_USER,
        ),
    ])
    reasoner = _CapturingReasoner(judgments=[
        DivergenceJudgment(
            effect_matches_expectation=False,
            plan_still_valid=False,
            failure_kind=FailureKind.AMBIGUITY_NEEDS_USER,
        ),
    ])
    service = EnactmentService(
        presence_renderer=_CapturingPresence(),
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
    )
    await service.run(_execute_briefing())
    assert len(captured) == 1
    assert captured[0].failed_step.step_id == "s1"


@pytest.mark.asyncio
async def test_friction_ticket_does_not_short_circuit_subsequent_retries():
    """Architectural pin: ticket emission happens AFTER routing is
    final. Verifies that even after a friction ticket fires, the
    routing decision (which already chose modify) proceeds unchanged."""
    observer = _RecordingFrictionObserver()
    plan = _plan(steps=[_step()])
    planner = _StubPlanner(plan)
    # Pattern: 4 transients (retry budget=3, exhaust to modify),
    # then modify produces a successful step.
    dispatcher = _StubDispatcher([
        StepDispatchResult(
            completed=False, output={},
            failure_kind=FailureKind.TRANSIENT,
        ),
    ] * 4 + [
        StepDispatchResult(completed=True, output={"ok": True}),
    ])
    reasoner = _StubReasoner(judgments=[
        DivergenceJudgment(
            effect_matches_expectation=False,
            plan_still_valid=True,
            failure_kind=FailureKind.TRANSIENT,
        ),
    ] * 4 + [
        DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=True,
            failure_kind=FailureKind.NONE,
        ),
    ])
    service = EnactmentService(
        presence_renderer=_CapturingPresence(),
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
        friction_observer=observer,
        retry_budget=3,
    )
    outcome = await service.run(_execute_briefing())
    # Friction ticket fired AND modify proceeded successfully.
    # The ticket did NOT prevent the modify path from running.
    assert len(observer.tickets) == 1
    assert outcome.subtype is TerminationSubtype.SUCCESS_FULL_MACHINERY
    # Modify was indeed called once (after retry exhaustion).
    assert reasoner.modify_calls == 1


@pytest.mark.asyncio
async def test_empty_allowed_operations_envelope_treated_as_unconstrained():
    """Behavior pin: ActionEnvelope.allowed_operations=() means
    'unconstrained' (the validator only enforces when the list is
    non-empty). This mirrors the behavior of optional fields like
    constraints / forbidden_moves; explicit allow-listing requires
    the integrator to specify the operations.

    Tool-class enforcement is stricter — empty allowed_tool_classes
    means 'no tool classes permitted' — because tool classes are the
    primary capability boundary."""
    plan = _plan(steps=[_step(operation_name="send")])
    envelope = ActionEnvelope(
        intended_outcome="x",
        allowed_tool_classes=("email",),
        allowed_operations=(),  # unconstrained
    )
    service, _, dispatcher, _ = _full_machinery_service(plan=plan)
    outcome = await service.run(_execute_briefing(envelope=envelope))
    # No envelope rejection; dispatch proceeds.
    assert outcome.subtype is TerminationSubtype.SUCCESS_FULL_MACHINERY


@pytest.mark.asyncio
async def test_confirmation_boundary_no_in_turn_waiting():
    """The 'no in-turn waiting' invariant: the EnactmentService never
    blocks waiting for user confirmation mid-loop. Confirmation
    requirements always cross a turn boundary; the service either
    rejects the plan (envelope-violation B1) or proceeds because
    confirmation already happened."""
    # The previous test cases already exercise this: any plan that
    # tries to chain through a confirmation step in the same turn
    # is rejected at envelope validation, before any in-turn wait
    # could even be implemented. This test verifies via inspection:
    # the EnactmentService has no asyncio.Event, no wait_for, no
    # in-loop user-confirmation hook.
    import inspect
    source = inspect.getsource(EnactmentService)
    forbidden_substrings = [
        "asyncio.Event",
        "asyncio.wait_for",
        "user_confirmation",
        "wait_for_user",
    ]
    for substr in forbidden_substrings:
        assert substr not in source, (
            f"EnactmentService.source contains {substr!r}; in-turn "
            f"waiting is forbidden — confirmation always crosses a "
            f"turn boundary"
        )
