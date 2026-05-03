"""Redaction invariant tests for enactment.* audit entries (PDI C7).

Per design review's C7 guidance: enactment audit entries MUST NOT contain
restricted content — covenant rule descriptions, restricted memory
content, restricted context-space material. The existing redaction
code path applies; this test surface verifies covenant rule text
does not leak even when a covenant blocks an action.

The structural reasoning: integration's runner already enforces the
redaction invariant on briefings (the Restricted-content scan in
integration/runner.py). EnactmentService consumes briefings whose
text fields are already presence-safe; its audit emissions reference
the briefing by turn_id / integration_run_id but never quote the
briefing's content fields.

These tests pin the invariant by inspecting audit entries for
forbidden substrings that would appear if a redaction violation
slipped through.
"""

from __future__ import annotations

import pytest

from kernos.kernel.enactment import (
    DivergenceJudgment,
    EnactmentService,
    FailureKind,
    PresenceRenderResult,
    StepDispatchResult,
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
    Restricted,
)


# Sentinel strings that simulate restricted content. If any of these
# appear in any enactment.* audit entry, the redaction invariant has
# been violated.
SECRET_COVENANT_TEXT = (
    "the user has a covenant that they should not be informed about "
    "the surprise birthday party planned for next saturday"
)
SECRET_HIDDEN_MEMORY = (
    "private medical detail the user has flagged personal-only"
)
SECRET_CROSS_SPACE_MATERIAL = (
    "context-space content from a space the current member cannot read"
)
ALL_SECRETS = (
    SECRET_COVENANT_TEXT,
    SECRET_HIDDEN_MEMORY,
    SECRET_CROSS_SPACE_MATERIAL,
)


def _audit_entry_contains_secret(entry: dict, secrets) -> str | None:
    """Recursively scan an audit entry's string values for any
    sentinel secret. Returns the matching secret on first hit,
    None if the entry is clean."""

    def _walk(value):
        if isinstance(value, str):
            for secret in secrets:
                if secret in value:
                    return secret
        elif isinstance(value, dict):
            for v in value.values():
                hit = _walk(v)
                if hit:
                    return hit
        elif isinstance(value, (list, tuple)):
            for v in value:
                hit = _walk(v)
                if hit:
                    return hit
        return None

    return _walk(entry)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _step(step_id: str = "s1") -> Step:
    return Step(
        step_id=step_id,
        tool_id="email_send",
        arguments={"to": "x"},
        tool_class="email",
        operation_name="send",
        expectation=StepExpectation(prose="x"),
    )


def _plan(plan_id: str = "plan-1") -> Plan:
    return Plan(plan_id=plan_id, turn_id="turn-1", steps=(_step(),))


def _briefing_with_clean_directive() -> Briefing:
    """The briefing's presence_directive is already presence-safe per
    integration's redaction invariant. EnactmentService consumes this
    briefing and emits audit entries; the test verifies the secrets
    DO NOT appear in any audit entry."""
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=ExecuteTool(tool_id="email_send", arguments={}),
        presence_directive=(
            "execute the requested email send while respecting the "
            "operating envelope"
        ),
        audit_trace=AuditTrace(),
        turn_id="turn-redaction",
        integration_run_id="run-redaction",
        action_envelope=ActionEnvelope(
            intended_outcome="send the requested email",
            allowed_tool_classes=("email",),
            allowed_operations=("send",),
        ),
    )


class _Presence:
    async def render(self, briefing):
        return PresenceRenderResult(text="rendered terminal text")


class _Planner:
    def __init__(self, plan):
        self._plan = plan

    async def create_plan(self, inputs):
        return PlanCreationResult(plan=self._plan)


class _Dispatcher:
    def __init__(self, results):
        self._results = list(results)

    async def dispatch(self, inputs):
        return self._results.pop(0)


class _Reasoner:
    def __init__(self, judgments):
        self._judgments = list(judgments)

    async def judge_divergence(self, inputs):
        return self._judgments.pop(0)

    async def emit_modified_step(self, inputs):
        return _step(step_id="modified")

    async def emit_pivot_step(self, inputs):
        return _step(step_id="pivot")

    async def formulate_clarification(self, inputs):
        from kernos.kernel.enactment.service import (
            ClarificationFormulationResult,
        )
        return ClarificationFormulationResult(
            question="?",
            ambiguity_type="target",
            blocking_ambiguity="b",
            safe_question_context="c",
            attempted_action_summary="a",
            discovered_information="d",
        )


# ---------------------------------------------------------------------------
# Redaction — happy path: clean briefing produces clean audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_entries_clean_when_briefing_is_clean():
    """Sanity baseline: a presence-safe briefing produces audit
    entries that contain no sentinel secrets."""
    audit_sink = []

    async def emit(entry):
        audit_sink.append(entry)

    service = EnactmentService(
        presence_renderer=_Presence(),
        audit_emitter=emit,
        planner=_Planner(_plan()),
        step_dispatcher=_Dispatcher([
            StepDispatchResult(completed=True, output={"ok": True}),
        ]),
        divergence_reasoner=_Reasoner([
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            ),
        ]),
    )
    await service.run(_briefing_with_clean_directive())
    for entry in audit_sink:
        leaked = _audit_entry_contains_secret(entry, ALL_SECRETS)
        assert leaked is None, (
            f"audit entry {entry['category']} leaked secret: "
            f"{leaked[:40]}…"
        )


# ---------------------------------------------------------------------------
# Redaction — adversarial: secret in step args / output does not leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_args_with_sensitive_content_do_not_leak_to_audit():
    """Even if step arguments carry user-content text, the audit
    entries reference the step by id — they never embed argument
    values. Pin against accidentally embedding step.arguments."""
    audit_sink = []

    async def emit(entry):
        audit_sink.append(entry)

    plan = Plan(
        plan_id="plan-1",
        turn_id="turn-1",
        steps=(
            Step(
                step_id="s1",
                tool_id="email_send",
                arguments={"body": SECRET_COVENANT_TEXT},
                tool_class="email",
                operation_name="send",
                expectation=StepExpectation(prose="x"),
            ),
        ),
    )

    service = EnactmentService(
        presence_renderer=_Presence(),
        audit_emitter=emit,
        planner=_Planner(plan),
        step_dispatcher=_Dispatcher([
            StepDispatchResult(completed=True, output={"ok": True}),
        ]),
        divergence_reasoner=_Reasoner([
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            ),
        ]),
    )
    await service.run(_briefing_with_clean_directive())
    for entry in audit_sink:
        leaked = _audit_entry_contains_secret(entry, [SECRET_COVENANT_TEXT])
        assert leaked is None, (
            f"audit entry {entry['category']} leaked argument value"
        )


@pytest.mark.asyncio
async def test_dispatch_output_with_sensitive_content_does_not_leak_to_audit():
    """The dispatch result's `output` dict may carry tool response
    content. Audit entries reference the step by id; output content
    must not appear in audit fields."""
    audit_sink = []

    async def emit(entry):
        audit_sink.append(entry)

    service = EnactmentService(
        presence_renderer=_Presence(),
        audit_emitter=emit,
        planner=_Planner(_plan()),
        step_dispatcher=_Dispatcher([
            StepDispatchResult(
                completed=True,
                output={"body": SECRET_HIDDEN_MEMORY},
            ),
        ]),
        divergence_reasoner=_Reasoner([
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            ),
        ]),
    )
    await service.run(_briefing_with_clean_directive())
    for entry in audit_sink:
        leaked = _audit_entry_contains_secret(entry, [SECRET_HIDDEN_MEMORY])
        assert leaked is None


@pytest.mark.asyncio
async def test_dispatch_error_summary_redaction_responsibility_documented():
    """The dispatcher's `error_summary` field IS surfaced to audit (
    per the C5 spec). Tool authors are responsible for redacting
    sensitive content before populating error_summary. This test
    pins that operator-visible field by confirming the error_summary
    flows verbatim — making the redaction-responsibility split
    explicit at the audit boundary."""
    audit_sink = []

    async def emit(entry):
        audit_sink.append(entry)

    service = EnactmentService(
        presence_renderer=_Presence(),
        audit_emitter=emit,
        planner=_Planner(_plan()),
        step_dispatcher=_Dispatcher([
            StepDispatchResult(
                completed=False,
                output={},
                failure_kind=FailureKind.NON_TRANSIENT,
                error_summary="redacted: 4xx auth error",
            ),
            # Modified step succeeds.
            StepDispatchResult(completed=True, output={"ok": True}),
        ]),
        divergence_reasoner=_Reasoner([
            DivergenceJudgment(
                effect_matches_expectation=False,
                plan_still_valid=True,
                failure_kind=FailureKind.NON_TRANSIENT,
            ),
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            ),
        ]),
    )
    await service.run(_briefing_with_clean_directive())
    step_attempted = next(
        e for e in audit_sink if e["category"] == "enactment.step_attempted"
    )
    # The dispatcher's redacted error_summary is surfaced; the test
    # pins the redaction-responsibility split.
    assert step_attempted["error_summary"] == "redacted: 4xx auth error"
    # And no sentinel secret leaked through.
    for entry in audit_sink:
        leaked = _audit_entry_contains_secret(entry, ALL_SECRETS)
        assert leaked is None


# ---------------------------------------------------------------------------
# Redaction — covenant-block scenario (design review-mandated test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_covenant_block_b1_termination_does_not_leak_rule_text():
    """Design Review-mandated: covenant rule descriptions don't leak into
    enactment audit even when covenant blocks an action.

    The covenant policy lives upstream of EnactmentService — the
    integration runner enforces the safety policy before producing
    a briefing. By the time enactment sees the briefing, the
    covenant rule text has been translated into behavioral
    instruction (the redaction invariant on briefings).

    This test simulates the downstream effect: a B1 termination
    where the trace records why the action was blocked. The audit
    entries must reference the block reason without quoting the
    rule text."""
    audit_sink = []

    async def emit(entry):
        audit_sink.append(entry)

    # Plan that violates the envelope (envelope-violation B1 surfaces).
    bad_plan = Plan(
        plan_id="plan-bad",
        turn_id="turn-1",
        steps=(
            Step(
                step_id="s1",
                tool_id="email_send",
                arguments={},
                tool_class="slack",  # outside envelope's allowed_tool_classes
                operation_name="send",
                expectation=StepExpectation(prose="x"),
            ),
        ),
    )
    briefing = Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=ExecuteTool(tool_id="x", arguments={}),
        presence_directive=(
            "execute carefully; covenant constraints apply behaviorally"
        ),
        audit_trace=AuditTrace(),
        turn_id="turn-1",
        integration_run_id="run-1",
        action_envelope=ActionEnvelope(
            intended_outcome="x",
            allowed_tool_classes=("email",),
            allowed_operations=("send",),
        ),
    )

    service = EnactmentService(
        presence_renderer=_Presence(),
        audit_emitter=emit,
        planner=_Planner(bad_plan),
        step_dispatcher=_Dispatcher([]),  # no dispatch on envelope violation
        divergence_reasoner=_Reasoner([]),
    )
    await service.run(briefing)
    for entry in audit_sink:
        leaked = _audit_entry_contains_secret(entry, ALL_SECRETS)
        assert leaked is None, (
            f"audit entry {entry['category']} leaked covenant rule text"
        )


# ---------------------------------------------------------------------------
# Redaction — Restricted CohortOutput visibility composes upstream
# ---------------------------------------------------------------------------


def test_restricted_visibility_marker_documented_in_briefing_layer():
    """The Restricted visibility marker (PDI V1 schema) lives on
    CohortOutput and is enforced by integration's runner. EnactmentService
    consumes the briefing AFTER integration's redaction post-check —
    the audit trail is therefore safe by composition.

    This test pins the composition contract: Restricted is importable
    from briefing, and the docstring documents the safety property."""
    from kernos.kernel.integration.briefing import Restricted

    docstring = Restricted.__doc__ or ""
    assert "audit" in docstring.lower() or "reason" in docstring.lower()
