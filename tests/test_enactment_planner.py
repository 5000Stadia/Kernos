"""Tests for the concrete Planner (IWL C1).

Coverage:
  - Conforms to PDI's shipped PlannerLike Protocol.
  - Tool catalog filtered to inputs.briefing.action_envelope.allowed_operations
    AND allowed_tool_classes AND forbidden_moves.
  - Plan structure carries [Step, StepExpectation] pairs.
  - 7-signal vocabulary: locked enum exposed in synthetic finalize tool;
    parser accepts each; rejects unknown kinds via the StructuredSignal
    dataclass validator.
  - Reassembly inputs (prior_plan_id) stamp created_via correctly.
  - Errors when the model fails to call __finalize_plan__ surface
    cleanly via PlannerError so EnactmentService can route through B1.
  - The chain caller's max_tokens parameter is plumbed through.
"""

from __future__ import annotations

import pytest

from kernos.kernel.enactment.plan import (
    Plan,
    PlanValidationError,
    SignalKind,
    Step,
    StepExpectation,
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
from kernos.kernel.enactment.service import (
    PlanCreationInputs,
    PlanCreationResult,
    PlannerLike,
)
from kernos.kernel.integration.briefing import (
    ActionEnvelope,
    AuditTrace,
    Briefing,
    ExecuteTool,
)
from kernos.providers.base import ContentBlock, ProviderResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    *,
    tool_id: str = "email_send",
    tool_class: str = "email",
    operation_name: str = "send",
    description: str = "send an email",
) -> ToolCatalogEntry:
    return ToolCatalogEntry(
        tool_id=tool_id,
        tool_class=tool_class,
        operation_name=operation_name,
        description=description,
    )


def _briefing(
    *,
    envelope: ActionEnvelope | None = None,
) -> Briefing:
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=ExecuteTool(tool_id="email_send", arguments={}),
        presence_directive="execute",
        audit_trace=AuditTrace(),
        turn_id="turn-plan",
        integration_run_id="run-plan",
        action_envelope=envelope or ActionEnvelope(
            intended_outcome="send the email",
            allowed_tool_classes=("email",),
            allowed_operations=("send",),
        ),
    )


def _inputs(
    *,
    briefing: Briefing | None = None,
    prior_plan_id: str = "",
) -> PlanCreationInputs:
    return PlanCreationInputs(
        briefing=briefing or _briefing(),
        prior_plan_id=prior_plan_id,
    )


def _finalize_block(payload: dict) -> ContentBlock:
    return ContentBlock(
        type="tool_use",
        id="tu_plan_1",
        name=PLAN_FINALIZE_TOOL_NAME,
        input=payload,
    )


def _resp(*blocks: ContentBlock) -> ProviderResponse:
    return ProviderResponse(
        content=list(blocks),
        stop_reason="tool_use",
        input_tokens=10,
        output_tokens=20,
    )


def _capture_chain(payload: dict, captured: dict | None = None):
    """Build a chain_caller stub that returns a finalize block with
    the given payload. If `captured` is provided, records the call
    args for assertions."""

    async def chain(system, messages, tools, max_tokens, **_):
        if captured is not None:
            captured["system"] = system
            captured["messages"] = messages
            captured["tools"] = tools
            captured["max_tokens"] = max_tokens
        return _resp(_finalize_block(payload))

    return chain


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_planner_conforms_to_planner_like_protocol():
    """The concrete Planner must satisfy PDI's shipped Protocol so
    TurnRunner / EnactmentService bind cleanly."""
    planner = Planner(
        chain_caller=_capture_chain({"steps": []}),
        tool_catalog=StaticToolCatalog(),
    )
    assert isinstance(planner, PlannerLike)


def test_factory_returns_planner():
    planner = build_planner(
        chain_caller=_capture_chain({"steps": []}),
        tool_catalog=StaticToolCatalog(),
    )
    assert isinstance(planner, Planner)


# ---------------------------------------------------------------------------
# Tool catalog filtering — load-bearing per acceptance criterion #3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_filtered_to_allowed_operations():
    """Only entries whose operation_name is in
    inputs.briefing.action_envelope.allowed_operations reach the
    prompt."""
    captured: dict = {}
    catalog = StaticToolCatalog(entries=(
        _entry(tool_id="email_send", operation_name="send"),
        _entry(tool_id="email_draft", operation_name="draft"),
        _entry(tool_id="slack_send", tool_class="slack", operation_name="send"),
    ))
    envelope = ActionEnvelope(
        intended_outcome="x",
        allowed_tool_classes=("email", "slack"),
        allowed_operations=("send",),  # only send permitted
    )
    planner = Planner(
        chain_caller=_capture_chain(
            {"steps": [{
                "tool_id": "email_send",
                "tool_class": "email",
                "operation_name": "send",
                "arguments": {},
                "expectation": {"prose": "sent"},
            }]},
            captured=captured,
        ),
        tool_catalog=catalog,
    )
    await planner.create_plan(_inputs(briefing=_briefing(envelope=envelope)))

    # The user message lists only the send operations (draft excluded).
    user_msg = captured["messages"][0]["content"]
    assert "operation=send" in user_msg
    assert "operation=draft" not in user_msg
    # Both email_send and slack_send are present (operation matches).
    assert "tool_id=email_send" in user_msg
    assert "tool_id=slack_send" in user_msg


@pytest.mark.asyncio
async def test_catalog_filtered_by_allowed_tool_classes():
    """Defense-in-depth: even when operation matches, tool_class must
    also be in allowed_tool_classes."""
    captured: dict = {}
    catalog = StaticToolCatalog(entries=(
        _entry(tool_id="email_send", tool_class="email"),
        _entry(tool_id="slack_send", tool_class="slack"),
    ))
    envelope = ActionEnvelope(
        intended_outcome="x",
        allowed_tool_classes=("email",),  # email only
        allowed_operations=("send",),
    )
    planner = Planner(
        chain_caller=_capture_chain(
            {"steps": [{
                "tool_id": "email_send",
                "tool_class": "email",
                "operation_name": "send",
                "arguments": {},
                "expectation": {"prose": "x"},
            }]},
            captured=captured,
        ),
        tool_catalog=catalog,
    )
    await planner.create_plan(_inputs(briefing=_briefing(envelope=envelope)))
    user_msg = captured["messages"][0]["content"]
    assert "tool_id=email_send" in user_msg
    assert "tool_id=slack_send" not in user_msg


@pytest.mark.asyncio
async def test_catalog_filtered_by_forbidden_moves():
    """Operations matching forbidden_moves are excluded from the
    catalog even if they're in allowed_operations."""
    captured: dict = {}
    catalog = StaticToolCatalog(entries=(
        _entry(tool_id="email_send", operation_name="send"),
        _entry(tool_id="email_escalate", operation_name="operation_escalation"),
    ))
    envelope = ActionEnvelope(
        intended_outcome="x",
        allowed_tool_classes=("email",),
        allowed_operations=("send", "operation_escalation"),
        forbidden_moves=("operation_escalation",),
    )
    planner = Planner(
        chain_caller=_capture_chain(
            {"steps": [{
                "tool_id": "email_send",
                "tool_class": "email",
                "operation_name": "send",
                "arguments": {},
                "expectation": {"prose": "x"},
            }]},
            captured=captured,
        ),
        tool_catalog=catalog,
    )
    await planner.create_plan(_inputs(briefing=_briefing(envelope=envelope)))
    user_msg = captured["messages"][0]["content"]
    assert "tool_id=email_escalate" not in user_msg


@pytest.mark.asyncio
async def test_empty_allowed_operations_treated_as_unconstrained():
    """Mirrors validate_step_against_envelope semantics: empty
    allowed_operations means unconstrained — the planner sees the
    full operation surface (still filtered by tool_class if any)."""
    captured: dict = {}
    catalog = StaticToolCatalog(entries=(
        _entry(tool_id="email_send", operation_name="send"),
        _entry(tool_id="email_draft", operation_name="draft"),
    ))
    envelope = ActionEnvelope(
        intended_outcome="x",
        allowed_tool_classes=("email",),
        allowed_operations=(),  # unconstrained
    )
    planner = Planner(
        chain_caller=_capture_chain(
            {"steps": [{
                "tool_id": "email_send",
                "tool_class": "email",
                "operation_name": "send",
                "arguments": {},
                "expectation": {"prose": "x"},
            }]},
            captured=captured,
        ),
        tool_catalog=catalog,
    )
    await planner.create_plan(_inputs(briefing=_briefing(envelope=envelope)))
    user_msg = captured["messages"][0]["content"]
    # Both operations appear since allowed_operations is unconstrained.
    assert "operation=send" in user_msg
    assert "operation=draft" in user_msg


# ---------------------------------------------------------------------------
# Plan structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_has_step_expectation_pairs():
    """Plans carry ordered [Step, StepExpectation] pairs per the
    locked Plan dataclass."""
    planner = Planner(
        chain_caller=_capture_chain({"steps": [{
            "tool_id": "email_send",
            "tool_class": "email",
            "operation_name": "send",
            "arguments": {"to": "x"},
            "expectation": {"prose": "email sent"},
        }]}),
        tool_catalog=StaticToolCatalog(),
    )
    result = await planner.create_plan(_inputs())
    assert isinstance(result, PlanCreationResult)
    plan = result.plan
    assert isinstance(plan, Plan)
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert isinstance(step, Step)
    assert step.tool_id == "email_send"
    assert step.tool_class == "email"
    assert step.operation_name == "send"
    assert isinstance(step.expectation, StepExpectation)
    assert step.expectation.prose == "email sent"


@pytest.mark.asyncio
async def test_plan_carries_turn_id_from_briefing():
    planner = Planner(
        chain_caller=_capture_chain({"steps": [{
            "tool_id": "x",
            "tool_class": "email",
            "operation_name": "send",
            "arguments": {},
            "expectation": {"prose": "x"},
        }]}),
        tool_catalog=StaticToolCatalog(),
    )
    briefing = _briefing()
    result = await planner.create_plan(_inputs(briefing=briefing))
    assert result.plan.turn_id == briefing.turn_id


# ---------------------------------------------------------------------------
# 7-signal vocabulary handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_tool_schema_lists_all_seven_signal_kinds():
    """Per architectural invariant: vocabulary locked at exactly 7
    SignalKinds. The synthetic finalize tool's schema must enumerate
    exactly those seven values for the model's expectation enum."""
    planner = Planner(
        chain_caller=_capture_chain({"steps": []}),
        tool_catalog=StaticToolCatalog(),
    )
    captured: dict = {}
    planner_w_capture = Planner(
        chain_caller=_capture_chain({"steps": [{
            "tool_id": "x",
            "tool_class": "email",
            "operation_name": "send",
            "arguments": {},
            "expectation": {"prose": "x"},
        }]}, captured=captured),
        tool_catalog=StaticToolCatalog(),
    )
    await planner_w_capture.create_plan(_inputs())

    tools = captured["tools"]
    assert len(tools) == 1
    tool = tools[0]
    assert tool["name"] == PLAN_FINALIZE_TOOL_NAME
    # Drill into the structured-signal kind enum.
    schema = tool["input_schema"]
    sig_enum = (
        schema["properties"]["steps"]["items"]
        ["properties"]["expectation"]
        ["properties"]["structured"]
        ["items"]["properties"]["kind"]["enum"]
    )
    assert set(sig_enum) == {k.value for k in SignalKind}
    assert len(sig_enum) == 7


@pytest.mark.asyncio
async def test_planner_accepts_each_signal_kind_in_expectation():
    """Round-trip test: the model can produce each of the seven
    structured signal kinds and the parser builds them correctly."""
    for kind in SignalKind:
        planner = Planner(
            chain_caller=_capture_chain({"steps": [{
                "tool_id": "email_send",
                "tool_class": "email",
                "operation_name": "send",
                "arguments": {},
                "expectation": {
                    "prose": "email sent",
                    "structured": [
                        {"kind": kind.value, "args": {}},
                    ],
                },
            }]}),
            tool_catalog=StaticToolCatalog(),
        )
        result = await planner.create_plan(_inputs())
        sig = result.plan.steps[0].expectation.structured[0]
        assert sig.kind is kind


@pytest.mark.asyncio
async def test_planner_rejects_unknown_signal_kind_via_dataclass_validator():
    """The StructuredSignal dataclass validator enforces the closed
    enum — an unknown kind raises PlanValidationError, surfaced as
    PlannerError so EnactmentService routes B1."""
    planner = Planner(
        chain_caller=_capture_chain({"steps": [{
            "tool_id": "email_send",
            "tool_class": "email",
            "operation_name": "send",
            "arguments": {},
            "expectation": {
                "prose": "x",
                "structured": [{"kind": "frobnicate", "args": {}}],
            },
        }]}),
        tool_catalog=StaticToolCatalog(),
    )
    with pytest.raises(PlannerError):
        await planner.create_plan(_inputs())


# ---------------------------------------------------------------------------
# Reassembly inputs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_plan_stamps_created_via_initial():
    planner = Planner(
        chain_caller=_capture_chain({"steps": [{
            "tool_id": "x",
            "tool_class": "email",
            "operation_name": "send",
            "arguments": {},
            "expectation": {"prose": "x"},
        }]}),
        tool_catalog=StaticToolCatalog(),
    )
    result = await planner.create_plan(_inputs(prior_plan_id=""))
    assert result.plan.created_via == "initial"


@pytest.mark.asyncio
async def test_reassembled_plan_stamps_created_via_tier_4_reassemble():
    """When prior_plan_id is set, the planner stamps the new plan
    as a tier-4 reassemble. EnactmentService doesn't re-stamp; the
    planner does it."""
    planner = Planner(
        chain_caller=_capture_chain({"steps": [{
            "tool_id": "x",
            "tool_class": "email",
            "operation_name": "send",
            "arguments": {},
            "expectation": {"prose": "x"},
        }]}),
        tool_catalog=StaticToolCatalog(),
    )
    result = await planner.create_plan(
        _inputs(prior_plan_id="prior-plan-id")
    )
    assert result.plan.created_via == "tier_4_reassemble"


@pytest.mark.asyncio
async def test_reassembly_context_appears_in_user_message():
    captured: dict = {}
    planner = Planner(
        chain_caller=_capture_chain(
            {"steps": [{
                "tool_id": "x",
                "tool_class": "email",
                "operation_name": "send",
                "arguments": {},
                "expectation": {"prose": "x"},
            }]},
            captured=captured,
        ),
        tool_catalog=StaticToolCatalog(),
    )
    await planner.create_plan(
        PlanCreationInputs(
            briefing=_briefing(),
            prior_plan_id="prior-1",
            triggering_context_summary="step s1 invalidated",
        )
    )
    user_msg = captured["messages"][0]["content"]
    assert "prior_plan_id: prior-1" in user_msg
    assert "step s1 invalidated" in user_msg


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_finalize_block_raises_planner_error():
    """Model didn't call __finalize_plan__. The planner surfaces a
    clear error so EnactmentService routes through B1."""

    async def chain(system, messages, tools, max_tokens, **_):
        return _resp(ContentBlock(type="text", text="here is my plan"))

    planner = Planner(
        chain_caller=chain, tool_catalog=StaticToolCatalog()
    )
    with pytest.raises(PlannerError, match="finalize"):
        await planner.create_plan(_inputs())


@pytest.mark.asyncio
async def test_empty_steps_array_raises_planner_error():
    planner = Planner(
        chain_caller=_capture_chain({"steps": []}),
        tool_catalog=StaticToolCatalog(),
    )
    with pytest.raises(PlannerError, match="non-empty"):
        await planner.create_plan(_inputs())


@pytest.mark.asyncio
async def test_invalid_step_construction_raises_planner_error():
    """A step missing tool_id is structurally invalid; surfaces via
    PlannerError."""
    planner = Planner(
        chain_caller=_capture_chain({"steps": [{
            "tool_id": "",  # invalid
            "tool_class": "email",
            "operation_name": "send",
            "arguments": {},
            "expectation": {"prose": "x"},
        }]}),
        tool_catalog=StaticToolCatalog(),
    )
    with pytest.raises(PlannerError):
        await planner.create_plan(_inputs())


# ---------------------------------------------------------------------------
# Chain caller plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_caller_receives_max_tokens_from_constructor():
    captured: dict = {}
    planner = Planner(
        chain_caller=_capture_chain(
            {"steps": [{
                "tool_id": "x",
                "tool_class": "email",
                "operation_name": "send",
                "arguments": {},
                "expectation": {"prose": "x"},
            }]},
            captured=captured,
        ),
        tool_catalog=StaticToolCatalog(),
        max_tokens=4096,
    )
    await planner.create_plan(_inputs())
    assert captured["max_tokens"] == 4096


def test_default_max_tokens_is_documented():
    assert DEFAULT_PLANNER_MAX_TOKENS == 2048


# ---------------------------------------------------------------------------
# Streaming-disabled-by-construction (PDI invariant carried over)
# ---------------------------------------------------------------------------


def test_plan_creation_result_has_no_streamed_field():
    """PDI invariant: PlanCreationResult has no `streamed` field —
    streaming is unreachable from the planner code path. C1 must not
    add one."""
    from dataclasses import fields
    names = {f.name for f in fields(PlanCreationResult)}
    assert "streamed" not in names
