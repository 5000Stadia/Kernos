"""Tests for the gardener cohort adapter (C2 of CAG).

Covers the 14 spec scenarios from COHORT-ADAPT-GARDENER's Live
Test section. Synthetic GardenerService backed by a mock
reasoning service that fails any LLM invocation; cohort run
exercises the full happy path + edge cases.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.cohorts.gardener import GardenerDecision
from kernos.kernel.cohorts import (
    CohortContext,
    CohortFanOutConfig,
    CohortFanOutRunner,
    CohortRegistry,
    ContextSpaceRef,
    Turn,
    make_gardener_descriptor,
    register_gardener_cohort,
)
from kernos.kernel.cohorts.gardener_cohort import (
    COHORT_ID,
    RATIONALE_SHORT_CAP,
    RECENT_EVOLUTION_LIMIT,
    TIMEOUT_MS,
    make_canvas_resolver,
    make_gardener_cohort_run,
)
from kernos.kernel.gardener import (
    EvolutionRecord,
    GardenerService,
    PendingProposal,
)
from kernos.kernel.integration.briefing import (
    CohortOutput,
    Outcome,
    Public,
    Restricted,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _no_llm_reasoning():
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(
        side_effect=AssertionError("cohort must not call complete_simple"),
    )
    reasoning.reason = AsyncMock(
        side_effect=AssertionError("cohort must not call reason"),
    )
    return reasoning


def _service():
    return GardenerService(
        canvas_service=MagicMock(),
        instance_db=MagicMock(),
        reasoning_service=_no_llm_reasoning(),
    )


def _ctx(
    *,
    turn_id: str = "turn-1",
    member_id: str = "m-1",
    instance_id: str = "i-1",
    spaces: tuple[ContextSpaceRef, ...] = (),
    user_message: str = "hi",
) -> CohortContext:
    return CohortContext(
        member_id=member_id,
        user_message=user_message,
        conversation_thread=(Turn("user", user_message),),
        active_spaces=spaces,
        turn_id=turn_id,
        instance_id=instance_id,
        produced_at="2026-04-26T00:00:00+00:00",
    )


def _proposal(canvas_id: str = "c1", **kwargs) -> PendingProposal:
    base = dict(
        canvas_id=canvas_id,
        action="propose_split",
        confidence="high",
        rationale="growth pattern hit threshold; split recommended",
        affected_pages=["index.md"],
        captured_at=datetime.now(timezone.utc),
        payload={"pattern": "growth"},
    )
    base.update(kwargs)
    return PendingProposal(**base)


# ---------------------------------------------------------------------------
# 1. No active canvas (zero canvas spaces)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_1_no_active_canvas_zero_spaces():
    svc = _service()
    run = make_gardener_cohort_run(svc)
    out = await run(_ctx(spaces=()))
    assert isinstance(out, CohortOutput)
    assert out.output["has_active_canvas"] is False
    assert out.output["canvas_id"] is None
    assert out.output["pending_proposals"] == []
    assert isinstance(out.visibility, Public)


# ---------------------------------------------------------------------------
# 2. Multiple active canvases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_2_multiple_active_canvases_does_not_pick_winner():
    svc = _service()
    run = make_gardener_cohort_run(svc)
    out = await run(
        _ctx(
            spaces=(
                ContextSpaceRef("canvas-a"),
                ContextSpaceRef("canvas-b"),
            ),
        )
    )
    assert out.output["has_active_canvas"] is False
    assert out.output["canvas_id"] is None


# ---------------------------------------------------------------------------
# 3. Single active canvas, no pending proposals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_3_single_canvas_empty_state():
    svc = _service()
    run = make_gardener_cohort_run(svc)
    out = await run(_ctx(spaces=(ContextSpaceRef("c-empty"),)))
    assert out.output["has_active_canvas"] is True
    assert out.output["canvas_id"] == "c-empty"
    assert out.output["pending_proposals"] == []
    assert out.output["recent_evolution"] == []
    assert out.output["observation_age_seconds"] is None


# ---------------------------------------------------------------------------
# 4. Single active canvas, single pending proposal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_4_single_pending_proposal_with_truncated_rationale():
    svc = _service()
    long_rationale = "x" * 500
    svc.coalescer.add(_proposal("c-prop", rationale=long_rationale))
    run = make_gardener_cohort_run(svc)
    out = await run(_ctx(spaces=(ContextSpaceRef("c-prop"),)))

    assert len(out.output["pending_proposals"]) == 1
    summary = out.output["pending_proposals"][0]
    assert summary["action"] == "propose_split"
    assert summary["pattern"] == "growth"
    assert "proposal_id" in summary
    # rationale_short truncated to RATIONALE_SHORT_CAP.
    assert len(summary["rationale_short"]) == RATIONALE_SHORT_CAP


# ---------------------------------------------------------------------------
# 5. Multiple pending proposals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_5_multiple_proposals_all_present():
    svc = _service()
    for i in range(3):
        svc.coalescer.add(_proposal("c-multi", action=f"action-{i}"))
    run = make_gardener_cohort_run(svc)
    out = await run(_ctx(spaces=(ContextSpaceRef("c-multi"),)))
    assert len(out.output["pending_proposals"]) == 3
    actions = [p["action"] for p in out.output["pending_proposals"]]
    assert actions == ["action-0", "action-1", "action-2"]


# ---------------------------------------------------------------------------
# 6. Recent evolution decisions (cap at 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_6_recent_evolution_capped_at_three():
    svc = _service()
    for i in range(5):
        svc._record_evolution(
            "c-evo",
            GardenerDecision(
                action=f"a{i}",
                confidence="high",
                pattern="growth",
                affected_pages=["p.md"],
            ),
            consultation="evolution",
        )
    run = make_gardener_cohort_run(svc)
    out = await run(_ctx(spaces=(ContextSpaceRef("c-evo"),)))
    assert len(out.output["recent_evolution"]) == RECENT_EVOLUTION_LIMIT
    # Last three (most recent). Deque maxlen is 10, so all 5 retained;
    # cohort takes the trailing 3.
    actions = [r["action"] for r in out.output["recent_evolution"]]
    assert actions == ["a2", "a3", "a4"]


# ---------------------------------------------------------------------------
# 7. Restricted proposal filtered at source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_7_restricted_proposal_filtered_at_source():
    svc = _service()
    svc.coalescer.add(
        _proposal("c-mix", action="public_action", payload={"pattern": "open_pattern"})
    )
    svc.coalescer.add(
        _proposal(
            "c-mix",
            action="private_action",
            payload={"pattern": "personal_journal"},
        )
    )

    is_restricted = lambda pattern: pattern == "personal_journal"
    run = make_gardener_cohort_run(
        svc, restricted_pattern_check=is_restricted,
    )
    out = await run(_ctx(spaces=(ContextSpaceRef("c-mix"),)))

    assert isinstance(out.visibility, Public)  # output stays Public
    actions = [p["action"] for p in out.output["pending_proposals"]]
    assert "public_action" in actions
    # Restricted proposal entirely absent — not marked, not redacted.
    assert "private_action" not in actions
    serialised = str(out.to_dict())
    assert "personal_journal" not in serialised


# ---------------------------------------------------------------------------
# 8. Snapshot is non-mutating (cohort calls don't drain coalescer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_8_cohort_run_does_not_mutate_coalescer():
    svc = _service()
    svc.coalescer.add(_proposal("c-stable"))
    svc.coalescer.add(_proposal("c-stable", action="flag_stale"))

    before = svc.coalescer.buffered_count("c-stable")
    run = make_gardener_cohort_run(svc)
    await run(_ctx(spaces=(ContextSpaceRef("c-stable"),)))
    await run(_ctx(spaces=(ContextSpaceRef("c-stable"),)))
    after = svc.coalescer.buffered_count("c-stable")
    assert before == after == 2


# ---------------------------------------------------------------------------
# 9. Cohort run via fan-out runner (end-to-end)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_9_cohort_via_fan_out_runner():
    svc = _service()
    svc.coalescer.add(_proposal("c-runner"))

    registry = CohortRegistry()
    register_gardener_cohort(registry, svc)

    audit: list[dict] = []

    async def emit(entry: dict):
        audit.append(entry)

    runner = CohortFanOutRunner(
        registry=registry,
        audit_emitter=emit,
        config=CohortFanOutConfig(global_timeout_seconds=2.0),
    )
    result = await runner.run(_ctx(spaces=(ContextSpaceRef("c-runner"),)))

    assert len(result.outputs) == 1
    out = result.outputs[0]
    assert out.cohort_id == COHORT_ID
    assert out.outcome is Outcome.SUCCESS
    # Runner-minted cohort_run_id (overrides the cohort's provisional one).
    assert out.cohort_run_id == "turn-1:gardener:0"
    assert audit[0]["audit_category"] == "cohort.fan_out"


# ---------------------------------------------------------------------------
# 10. No model call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_10_no_model_call_during_run():
    """The mock reasoning service fixture is rigged to raise on any
    LLM invocation. A successful run = no calls made."""
    reasoning = _no_llm_reasoning()
    svc = GardenerService(
        canvas_service=MagicMock(),
        instance_db=MagicMock(),
        reasoning_service=reasoning,
    )
    svc.coalescer.add(_proposal("c-llm-check"))
    run = make_gardener_cohort_run(svc)
    out = await run(_ctx(spaces=(ContextSpaceRef("c-llm-check"),)))
    assert out.output["has_active_canvas"] is True
    reasoning.complete_simple.assert_not_called()
    reasoning.reason.assert_not_called()


# ---------------------------------------------------------------------------
# 11. Cohort output is CohortOutput, not dict (the design review edit #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_11_run_returns_cohort_output_type():
    svc = _service()
    run = make_gardener_cohort_run(svc)
    out = await run(_ctx(spaces=(ContextSpaceRef("c"),)))
    assert isinstance(out, CohortOutput)
    assert not isinstance(out, dict)


# ---------------------------------------------------------------------------
# 12. Cohort completes well under timeout_ms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_12_cohort_completes_under_timeout():
    svc = _service()
    for i in range(10):
        svc._record_evolution(
            "c-stress",
            GardenerDecision(action=f"a{i}", confidence="high"),
            consultation="evolution",
        )
    for i in range(20):
        svc.coalescer.add(_proposal("c-stress", action=f"p{i}"))

    run = make_gardener_cohort_run(svc)
    import time
    start = time.monotonic()
    await run(_ctx(spaces=(ContextSpaceRef("c-stress"),)))
    elapsed_ms = int((time.monotonic() - start) * 1000)
    # Spec timeout budget: 200ms. Common case sub-millisecond.
    assert elapsed_ms < TIMEOUT_MS


# ---------------------------------------------------------------------------
# 13. Integration consumption (V1 IntegrationRunner test fixture)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_13_integration_runner_consumes_gardener_output():
    from kernos.kernel.integration import (
        Briefing,
        IntegrationConfig,
        IntegrationInputs,
        IntegrationRunner,
        RespondOnly,
    )
    from kernos.providers.base import ContentBlock, ProviderResponse

    svc = _service()
    svc.coalescer.add(_proposal("c-int"))

    registry = CohortRegistry()
    register_gardener_cohort(registry, svc)

    async def emit(entry):
        pass

    fan_out = CohortFanOutRunner(
        registry=registry,
        audit_emitter=emit,
        config=CohortFanOutConfig(global_timeout_seconds=2.0),
    )
    fan_out_result = await fan_out.run(
        _ctx(spaces=(ContextSpaceRef("c-int"),)),
    )

    inputs = IntegrationInputs(
        user_message="anything new on the canvas?",
        conversation_thread=({"role": "user", "content": "anything new?"},),
        cohort_outputs=fan_out_result.outputs,
        surfaced_tools=(),
        active_context_spaces=({"space_id": "c-int", "domain": "general"},),
        member_id="m-1",
        instance_id="i-1",
        space_id="c-int",
        turn_id="turn-1",
    )

    async def chain(*_a, **_kw):
        return ProviderResponse(
            content=[
                ContentBlock(
                    type="tool_use",
                    id="tu_finalize",
                    name="__finalize_briefing__",
                    input={
                        "relevant_context": [
                            {
                                "source_type": "cohort.gardener",
                                "source_id": "turn-1:gardener:0",
                                "summary": "canvas has 1 pending proposal",
                                "confidence": 0.85,
                            }
                        ],
                        "filtered_context": [],
                        "decided_action": {"kind": "respond_only"},
                        "presence_directive": "acknowledge the pending proposal if relevant",
                    },
                )
            ],
            stop_reason="tool_use",
            input_tokens=10,
            output_tokens=20,
        )

    async def dispatcher(*_a, **_kw):
        return {}

    integration_audit: list[dict] = []

    async def emit2(entry):
        integration_audit.append(entry)

    integration = IntegrationRunner(
        chain_caller=chain,
        read_only_dispatcher=dispatcher,
        audit_emitter=emit2,
        config=IntegrationConfig(),
    )
    briefing = await integration.run(inputs)

    assert isinstance(briefing, Briefing)
    assert briefing.audit_trace.fail_soft_engaged is False
    assert isinstance(briefing.decided_action, RespondOnly)
    assert "turn-1:gardener:0" in briefing.audit_trace.cohort_outputs


# ---------------------------------------------------------------------------
# 14. Gardener service unchanged (existing public surface intact)
# ---------------------------------------------------------------------------


def test_scenario_14_gardener_public_surface_unchanged():
    svc = _service()
    assert callable(svc.on_canvas_event)
    assert callable(svc.consult_initial_shape)
    assert callable(svc.consult_evolution)
    assert callable(svc.consult_section)
    assert callable(svc.consult_preference_extraction)
    assert callable(svc.apply_initial_shape)
    assert callable(svc.wait_idle)
    assert hasattr(svc, "coalescer")
    assert hasattr(svc, "patterns")
    # Additive read surface from C1:
    assert callable(svc.current_observation_snapshot)


# ---------------------------------------------------------------------------
# Bonus: descriptor flags match spec
# ---------------------------------------------------------------------------


def test_descriptor_carries_spec_flags():
    """Acceptance criterion 2: cohort_id, timeout_ms, default_visibility,
    required, safety_class, execution_mode all match spec."""
    svc = _service()
    desc = make_gardener_descriptor(svc)
    assert desc.cohort_id == COHORT_ID
    assert desc.timeout_ms == TIMEOUT_MS
    assert isinstance(desc.default_visibility, Public)
    assert desc.required is False
    assert desc.safety_class is False
    from kernos.kernel.cohorts.descriptor import ExecutionMode
    assert desc.execution_mode is ExecutionMode.ASYNC


# ---------------------------------------------------------------------------
# Bonus: canvas resolver instance_db variant
# ---------------------------------------------------------------------------


def test_canvas_resolver_filters_by_instance_db_get_canvas():
    db = MagicMock()
    db.get_canvas.side_effect = (
        lambda sid: {"canvas_id": sid} if sid == "real" else None
    )

    resolver = make_canvas_resolver(instance_db=db)
    ctx = _ctx(
        spaces=(
            ContextSpaceRef("real"),
            ContextSpaceRef("not-a-canvas"),
        ),
    )
    assert resolver(ctx) == "real"


def test_canvas_resolver_returns_none_when_zero_canvases_after_filter():
    db = MagicMock()
    db.get_canvas.return_value = None
    resolver = make_canvas_resolver(instance_db=db)
    ctx = _ctx(
        spaces=(
            ContextSpaceRef("not-canvas-1"),
            ContextSpaceRef("not-canvas-2"),
        ),
    )
    assert resolver(ctx) is None
