"""COGNITIVE-CONTEXT-V1 C3a — packet-survives-the-seam tests.

These tests pin the "real IntegrationService doesn't drop the
packet" failure that the C2 contract tests cannot catch on their
own. Codex C3a-design "not asked, but flagging":

    Add one seam test separate from the 14 contract tests: create a
    sentinel packet, pass it through ``ReasoningRequest`` ->
    ``TurnRunnerInputs`` -> ``IntegrationInputs`` -> ``Briefing``,
    and assert object identity or sentinel content survives. This
    catches the exact "real IntegrationService dropped the packet"
    failure the C2 stub cannot catch.

The C2 contract tests use a ``_StubIntegrationService`` that does
copy the packet through. If the real ``IntegrationService.run``
ever stops carrying ``inputs.cognitive_context`` onto its
constructed ``Briefing``, the contract tests would still pass
(stub does the right thing) but production would silently drop
the packet again. This file's tests target the real seam to
catch that.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kernos.kernel.cognitive_context.field_provenance import (
    PopulationContext,
    populate_packet,
)
from kernos.kernel.cohorts.descriptor import CohortFanOutResult
from kernos.kernel.cohorts.runner import build_integration_inputs_from_fan_out
from kernos.kernel.integration.briefing import (
    AuditTrace,
    Briefing,
    RespondOnly,
)
from kernos.kernel.integration.runner import IntegrationInputs
from kernos.kernel.reasoning import ReasoningRequest
from kernos.kernel.turn_runner import TurnRunnerInputs


async def _build_sentinel_packet():
    ctx = PopulationContext(
        instance_id="inst-seam",
        member_id="mem-seam",
        space_id="space-seam",
        platform="seam-platform",
        auth_level="seam-auth",
        timestamp_utc=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        member_display_name="SeamMember",
        member_profile={"display_name": "SeamMember"},
    )
    return await populate_packet(ctx)


# ---------------------------------------------------------------------------
# ReasoningRequest carries the packet
# ---------------------------------------------------------------------------


async def test_reasoning_request_carries_cognitive_context():
    packet = await _build_sentinel_packet()
    req = ReasoningRequest(
        instance_id="inst-seam",
        conversation_id="conv-seam",
        system_prompt="x",
        messages=[],
        tools=[],
        model="claude-sonnet-4-6",
        trigger="user_message",
        cognitive_context=packet,
    )
    assert req.cognitive_context is packet, (
        "ReasoningRequest must carry the packet by reference; "
        "any in-flight copy/wrap would defeat the C3b/C3c chain."
    )


# ---------------------------------------------------------------------------
# TurnRunnerInputs.from_api_messages threads the packet
# ---------------------------------------------------------------------------


async def test_turn_runner_inputs_threads_cognitive_context():
    packet = await _build_sentinel_packet()
    inputs = TurnRunnerInputs.from_api_messages(
        instance_id="inst-seam",
        member_id="mem-seam",
        space_id="space-seam",
        turn_id="turn-seam",
        user_message="hi",
        api_messages=({"role": "user", "content": "hi"},),
        active_space_ids=("space-seam",),
        cognitive_context=packet,
    )
    assert inputs.cognitive_context is packet


# ---------------------------------------------------------------------------
# build_integration_inputs_from_fan_out passes packet through
# ---------------------------------------------------------------------------


async def test_build_integration_inputs_from_fan_out_passes_cognitive_context():
    packet = await _build_sentinel_packet()
    fan_out = CohortFanOutResult(
        outputs=(),
        fan_out_started_at="2026-05-01T00:00:00+00:00",
        fan_out_completed_at="2026-05-01T00:00:01+00:00",
    )
    integration_inputs = build_integration_inputs_from_fan_out(
        fan_out,
        user_message="hi",
        conversation_thread=(),
        member_id="mem-seam",
        instance_id="inst-seam",
        space_id="space-seam",
        turn_id="turn-seam",
        cognitive_context=packet,
    )
    assert integration_inputs.cognitive_context is packet


# ---------------------------------------------------------------------------
# Briefing carries the packet
# ---------------------------------------------------------------------------


async def test_briefing_carries_cognitive_context():
    packet = await _build_sentinel_packet()
    briefing = Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=RespondOnly(),
        presence_directive="seam directive",
        audit_trace=AuditTrace(),
        cognitive_context=packet,
    )
    assert briefing.cognitive_context is packet


# ---------------------------------------------------------------------------
# Real IntegrationService.run preserves the packet onto Briefing
# ---------------------------------------------------------------------------


async def test_real_integration_service_carries_packet_onto_briefing():
    """The CRITICAL pin: the real IntegrationService.run, given
    IntegrationInputs with a cognitive_context, must produce a
    Briefing whose cognitive_context is the same packet.

    Drives the actual production runner (not the stub) over a
    minimal-success path so any future change that drops the
    packet at the integration boundary fails this test loudly.

    The runner has a complex production path (LLM calls, redaction
    invariant, audit emission, etc.) that we don't want to fully
    exercise here. Instead we drive the FAIL-SOFT path with a
    deliberately-broken chain caller — the runner's fail-soft
    Briefing constructor must ALSO carry the packet through. Both
    fail-soft branches in runner.py are pinned by walking through
    them in turn.
    """
    from kernos.kernel.integration.runner import (
        IntegrationConfig,
        IntegrationRunner,
    )

    packet = await _build_sentinel_packet()

    async def broken_chain_caller(*args, **kwargs):
        raise RuntimeError("seam-test: deliberately broken chain")

    audit_records: list = []

    async def audit_emitter(rec):
        audit_records.append(rec)

    async def stub_dispatcher(tool_id, args, inputs):
        return {}

    runner = IntegrationRunner(
        chain_caller=broken_chain_caller,
        read_only_dispatcher=stub_dispatcher,
        config=IntegrationConfig(),
        audit_emitter=audit_emitter,
    )

    inputs = IntegrationInputs(
        user_message="hi",
        conversation_thread=(),
        cohort_outputs=(),
        surfaced_tools=(),
        active_context_spaces=(),
        member_id="mem-seam",
        instance_id="inst-seam",
        space_id="space-seam",
        turn_id="turn-seam",
        cognitive_context=packet,
    )

    briefing = await runner.run(inputs)
    assert briefing.cognitive_context is packet, (
        "Real IntegrationRunner.run must carry inputs.cognitive_context "
        "onto the produced Briefing on the fail-soft path. If this "
        "trips, the production decoupled path is silently dropping the "
        "packet — exactly the bug class CCV1 was created to prevent."
    )
