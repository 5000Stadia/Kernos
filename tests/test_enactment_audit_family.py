"""Tests for the enactment.* audit family — completeness + invariants
(PDI C7).

Per design review's C7 guidance:
  - Seven enactment.* event types ship and are consistently shaped.
  - References-not-dumps: plan payloads in audit entries reference
    plans by ID; never embed plan content.
  - Reassembly events reference prior plan by ID, never embed.

Seven events:
  enactment.plan_created
  enactment.step_attempted
  enactment.step_modified
  enactment.step_pivoted
  enactment.plan_reassembled
  enactment.terminated
  enactment.friction_observed

Each is exercised by an engineered scenario; the audit shape is
verified for required fields and for the references-not-dumps
invariant.
"""

from __future__ import annotations

import pytest

from kernos.kernel.enactment import (
    DivergenceJudgment,
    EnactmentService,
    FailureKind,
    PresenceRenderResult,
    StepDispatchResult,
    TIER_1_RETRY_EXHAUSTED,
    TerminationSubtype,
)
from kernos.kernel.enactment.plan import (
    Plan,
    Step,
    StepExpectation,
    new_plan_id,
)
from kernos.kernel.enactment.service import PlanCreationResult
from kernos.kernel.integration.briefing import (
    ActionEnvelope,
    AuditTrace,
    Briefing,
    ExecuteTool,
    RespondOnly,
)


# ---------------------------------------------------------------------------
# Fixtures (lighter helpers than test_enactment_service.py)
# ---------------------------------------------------------------------------


def _step(
    *,
    step_id: str = "s1",
    tool_id: str = "email_send",
    tool_class: str = "email",
    operation_name: str = "send",
) -> Step:
    return Step(
        step_id=step_id,
        tool_id=tool_id,
        arguments={"to": "x"},
        tool_class=tool_class,
        operation_name=operation_name,
        expectation=StepExpectation(prose="x"),
    )


def _plan(steps=None, plan_id: str | None = None) -> Plan:
    return Plan(
        plan_id=plan_id or new_plan_id(),
        turn_id="turn-1",
        steps=tuple(steps or [_step()]),
    )


def _execute_briefing() -> Briefing:
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=ExecuteTool(tool_id="email_send", arguments={}),
        presence_directive="x",
        audit_trace=AuditTrace(),
        turn_id="turn-1",
        integration_run_id="run-1",
        action_envelope=ActionEnvelope(
            intended_outcome="x",
            allowed_tool_classes=("email",),
            allowed_operations=("send",),
        ),
    )


class _Presence:
    async def render(self, briefing):
        return PresenceRenderResult(text="t")


class _Planner:
    def __init__(self, plans):
        self._plans = list(plans)

    async def create_plan(self, inputs):
        return PlanCreationResult(plan=self._plans.pop(0))


class _Dispatcher:
    def __init__(self, results):
        self._results = list(results)

    async def dispatch(self, inputs):
        return self._results.pop(0)


class _Reasoner:
    def __init__(
        self,
        *,
        judgments,
        modified=None,
        pivot=None,
        clarification=None,
    ):
        self._judgments = list(judgments)
        self._modified = modified
        self._pivot = pivot
        self._clarification = clarification

    async def judge_divergence(self, inputs):
        return self._judgments.pop(0)

    async def emit_modified_step(self, inputs):
        return self._modified or _step(step_id="modified")

    async def emit_pivot_step(self, inputs):
        return self._pivot or _step(step_id="pivot")

    async def formulate_clarification(self, inputs):
        from kernos.kernel.enactment.service import (
            ClarificationFormulationResult,
        )
        return self._clarification or ClarificationFormulationResult(
            question="?",
            ambiguity_type="target",
            blocking_ambiguity="b",
            safe_question_context="c",
            attempted_action_summary="a",
            discovered_information="d",
        )


def _make_service(*, planner, dispatcher, reasoner, audit_sink, **overrides):
    async def emit(entry):
        audit_sink.append(entry)

    return EnactmentService(
        presence_renderer=_Presence(),
        audit_emitter=emit,
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
        **overrides,
    )


# ---------------------------------------------------------------------------
# Seven categories — completeness
# ---------------------------------------------------------------------------


SEVEN_AUDIT_CATEGORIES = {
    "enactment.plan_created",
    "enactment.step_attempted",
    "enactment.step_modified",
    "enactment.step_pivoted",
    "enactment.plan_reassembled",
    "enactment.terminated",
    "enactment.friction_observed",
}


def test_seven_audit_categories_locked():
    """The enactment.* family is exactly seven categories. Adding an
    eighth is a coordinated extension. Test pin makes that explicit."""
    assert len(SEVEN_AUDIT_CATEGORIES) == 7


@pytest.mark.asyncio
async def test_plan_created_emits_with_required_fields():
    audit_sink = []
    plan = _plan(plan_id="plan-x")
    service = _make_service(
        planner=_Planner([plan]),
        dispatcher=_Dispatcher([
            StepDispatchResult(completed=True, output={"ok": True})
        ]),
        reasoner=_Reasoner(judgments=[
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            )
        ]),
        audit_sink=audit_sink,
    )
    await service.run(_execute_briefing())
    entry = next(
        e for e in audit_sink if e["category"] == "enactment.plan_created"
    )
    for required in (
        "category",
        "turn_id",
        "integration_run_id",
        "plan_id",
        "step_count",
        "created_at",
        "created_via",
    ):
        assert required in entry


@pytest.mark.asyncio
async def test_step_attempted_emits_with_required_fields():
    audit_sink = []
    plan = _plan()
    service = _make_service(
        planner=_Planner([plan]),
        dispatcher=_Dispatcher([
            StepDispatchResult(completed=True, output={"ok": True})
        ]),
        reasoner=_Reasoner(judgments=[
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            )
        ]),
        audit_sink=audit_sink,
    )
    await service.run(_execute_briefing())
    entry = next(
        e for e in audit_sink if e["category"] == "enactment.step_attempted"
    )
    for required in (
        "category",
        "turn_id",
        "plan_id",
        "step_id",
        "attempt_number",
        "tool_id",
        "operation_name",
        "tool_class",
        "completed",
        "failure_kind",
        "duration_ms",
    ):
        assert required in entry


@pytest.mark.asyncio
async def test_step_modified_emits_with_required_fields():
    audit_sink = []
    plan = _plan()
    service = _make_service(
        planner=_Planner([plan]),
        dispatcher=_Dispatcher([
            StepDispatchResult(
                completed=False,
                output={},
                failure_kind=FailureKind.CORRECTIVE_SIGNAL,
                corrective_signal="batch too large",
            ),
            StepDispatchResult(completed=True, output={"ok": True}),
        ]),
        reasoner=_Reasoner(judgments=[
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
        ]),
        audit_sink=audit_sink,
    )
    await service.run(_execute_briefing())
    entry = next(
        e for e in audit_sink if e["category"] == "enactment.step_modified"
    )
    for required in (
        "category",
        "turn_id",
        "plan_id",
        "original_step_id",
        "modified_step_id",
        "reason",
        "envelope_validation_passed",
    ):
        assert required in entry


@pytest.mark.asyncio
async def test_step_pivoted_emits_with_required_fields():
    audit_sink = []
    plan = _plan()
    service = _make_service(
        planner=_Planner([plan]),
        dispatcher=_Dispatcher([
            StepDispatchResult(completed=True, output={"results": []}),
            StepDispatchResult(completed=True, output={"ok": True}),
        ]),
        reasoner=_Reasoner(judgments=[
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
        ]),
        audit_sink=audit_sink,
    )
    await service.run(_execute_briefing())
    entry = next(
        e for e in audit_sink if e["category"] == "enactment.step_pivoted"
    )
    for required in (
        "category",
        "turn_id",
        "plan_id",
        "original_step_id",
        "replacement_step_id",
        "reason",
        "envelope_validation_passed",
    ):
        assert required in entry


@pytest.mark.asyncio
async def test_plan_reassembled_emits_with_required_fields():
    audit_sink = []
    plans = [
        _plan(plan_id="plan-a"),
        _plan(plan_id="plan-b"),
    ]
    service = _make_service(
        planner=_Planner(plans),
        dispatcher=_Dispatcher([
            StepDispatchResult(completed=True, output={}),
            StepDispatchResult(completed=True, output={"ok": True}),
        ]),
        reasoner=_Reasoner(judgments=[
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
        ]),
        pivot_budget=0,
        audit_sink=audit_sink,
    )
    await service.run(_execute_briefing())
    entry = next(
        e for e in audit_sink
        if e["category"] == "enactment.plan_reassembled"
    )
    for required in (
        "category",
        "turn_id",
        "prior_plan_id",
        "new_plan_id",
        "reason",
        "triggering_context_summary",
        "reassembly_count",
        "new_step_count",
    ):
        assert required in entry


@pytest.mark.asyncio
async def test_terminated_emits_with_required_fields():
    audit_sink = []
    plan = _plan()
    service = _make_service(
        planner=_Planner([plan]),
        dispatcher=_Dispatcher([
            StepDispatchResult(completed=True, output={"ok": True})
        ]),
        reasoner=_Reasoner(judgments=[
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            )
        ]),
        audit_sink=audit_sink,
    )
    await service.run(_execute_briefing())
    entry = next(
        e for e in audit_sink if e["category"] == "enactment.terminated"
    )
    for required in (
        "category",
        "turn_id",
        "integration_run_id",
        "decided_action_kind",
        "subtype",
        "text_length",
    ):
        assert required in entry


@pytest.mark.asyncio
async def test_full_machinery_success_emits_dedicated_subtype():
    """Per the design review re-review: full-machinery completions emit
    success_full_machinery, not success_thin_path. Path-distinguished
    subtypes give audit consumers diagnostic value when filtering by
    termination shape.

    This pin guards against regression to the C5/C6 bug where the
    full-machinery happy path mislabelled itself as thin-path."""
    audit_sink = []
    plan = _plan()
    service = _make_service(
        planner=_Planner([plan]),
        dispatcher=_Dispatcher([
            StepDispatchResult(completed=True, output={"ok": True})
        ]),
        reasoner=_Reasoner(judgments=[
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            )
        ]),
        audit_sink=audit_sink,
    )
    await service.run(_execute_briefing())
    entry = next(
        e for e in audit_sink if e["category"] == "enactment.terminated"
    )
    assert entry["subtype"] == "success_full_machinery"
    # The decided_action_kind also distinguishes execute_tool from
    # thin-path kinds, but the subtype split is the load-bearing
    # diagnostic for audit-trail filters.
    assert entry["decided_action_kind"] == "execute_tool"


@pytest.mark.asyncio
async def test_thin_path_success_keeps_thin_path_subtype():
    """Counterpart to the full-machinery pin: thin-path success keeps
    the success_thin_path subtype. The two are kept distinct so audit
    filters can grep by subtype."""
    audit_sink = []

    async def emit(entry):
        audit_sink.append(entry)

    from kernos.kernel.enactment import (
        EnactmentService,
        PresenceRenderResult,
    )

    class _Presence:
        async def render(self, briefing):
            return PresenceRenderResult(text="hi")

    service = EnactmentService(
        presence_renderer=_Presence(),
        audit_emitter=emit,
    )
    briefing = Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=RespondOnly(),
        presence_directive="x",
        audit_trace=AuditTrace(),
        turn_id="turn-1",
        integration_run_id="run-1",
    )
    await service.run(briefing)
    entry = next(
        e for e in audit_sink if e["category"] == "enactment.terminated"
    )
    assert entry["subtype"] == "success_thin_path"


@pytest.mark.asyncio
async def test_friction_observed_emits_with_required_fields():
    audit_sink = []
    plan = _plan()
    # 4 transients exhaust retry budget; modify proceeds successfully.
    service = _make_service(
        planner=_Planner([plan]),
        dispatcher=_Dispatcher([
            StepDispatchResult(
                completed=False,
                output={},
                failure_kind=FailureKind.TRANSIENT,
            ),
        ] * 4 + [
            StepDispatchResult(completed=True, output={"ok": True}),
        ]),
        reasoner=_Reasoner(judgments=[
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
        ]),
        retry_budget=3,
        audit_sink=audit_sink,
    )
    await service.run(_execute_briefing())
    entry = next(
        e for e in audit_sink
        if e["category"] == "enactment.friction_observed"
    )
    for required in (
        "category",
        "turn_id",
        "tool_id",
        "operation_name",
        "divergence_pattern",
        "attempt_count",
        "decided_action_kind",
    ):
        assert required in entry
    assert entry["divergence_pattern"] == TIER_1_RETRY_EXHAUSTED


# ---------------------------------------------------------------------------
# References-not-dumps invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_audit_entry_embeds_plan_payload():
    """V1 invariant: audit entries reference plans by ID, never embed
    plan content. Verifies across every emitted enactment.* entry
    in a tier-4 reassemble scenario (which exercises the most
    plan-payload-prone events)."""
    audit_sink = []
    plans = [_plan(plan_id="p-a"), _plan(plan_id="p-b")]
    service = _make_service(
        planner=_Planner(plans),
        dispatcher=_Dispatcher([
            StepDispatchResult(completed=True, output={}),
            StepDispatchResult(completed=True, output={"ok": True}),
        ]),
        reasoner=_Reasoner(judgments=[
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
        ]),
        pivot_budget=0,
        audit_sink=audit_sink,
    )
    await service.run(_execute_briefing())

    forbidden_keys = {"steps", "plan", "expectation", "structured", "arguments"}
    for entry in audit_sink:
        for key in forbidden_keys:
            assert key not in entry, (
                f"audit entry {entry['category']} contains plan-payload "
                f"key {key!r}; references-not-dumps invariant violated"
            )


@pytest.mark.asyncio
async def test_terminated_audit_carries_subtype_only_no_briefing_payload():
    """The terminated audit must NOT embed the briefing's text fields
    (presence_directive, etc.). Only subtype + structured metadata."""
    audit_sink = []
    plan = _plan()
    service = _make_service(
        planner=_Planner([plan]),
        dispatcher=_Dispatcher([
            StepDispatchResult(completed=True, output={"ok": True})
        ]),
        reasoner=_Reasoner(judgments=[
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            )
        ]),
        audit_sink=audit_sink,
    )
    await service.run(_execute_briefing())
    entry = next(
        e for e in audit_sink if e["category"] == "enactment.terminated"
    )
    forbidden = {"presence_directive", "relevant_context", "filtered_context"}
    for k in forbidden:
        assert k not in entry


# ---------------------------------------------------------------------------
# Audit shape consistency — every entry has category + turn_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_audit_entry_carries_category_and_turn_id():
    """Cross-event invariant: every enactment.* entry has these two
    fields. Operator dashboards key on (category, turn_id) for
    grouping."""
    audit_sink = []
    plans = [_plan(plan_id="p-a"), _plan(plan_id="p-b")]
    service = _make_service(
        planner=_Planner(plans),
        dispatcher=_Dispatcher([
            StepDispatchResult(completed=True, output={}),
            StepDispatchResult(completed=True, output={"ok": True}),
        ]),
        reasoner=_Reasoner(judgments=[
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
        ]),
        pivot_budget=0,
        audit_sink=audit_sink,
    )
    await service.run(_execute_briefing())
    for entry in audit_sink:
        assert entry["category"].startswith("enactment.")
        assert entry["turn_id"]
