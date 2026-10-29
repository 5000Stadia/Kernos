"""NOW context reaches the final model call with local time and plan guidance."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from kernos.kernel.cognitive_context.field_provenance import (
    PopulationContext,
    populate_packet,
)
from kernos.kernel.enactment.presence_renderer import PresenceRenderer
from kernos.kernel.integration.briefing import AuditTrace, Briefing, RespondOnly
from kernos.kernel.soul import Soul
from kernos.messages.handler import _build_now_block
from kernos.messages.models import AuthLevel
from kernos.providers.base import ContentBlock, ProviderResponse


async def _capture_system(*, timestamp, user_timezone="UTC", envelope=None,
                          platform="internal", knowledge_entries=()):
    packet = await populate_packet(PopulationContext(
        timestamp_utc=timestamp,
        user_timezone=user_timezone,
        platform=platform,
        knowledge_entries=knowledge_entries,
        member_profile={"hatched": True, "bootstrap_graduated": True},
        execution_envelope=envelope,
    ))
    captured = {}

    async def chain(system, messages, tools, max_tokens, **kwargs):
        captured["system"] = system
        return ProviderResponse(
            content=[ContentBlock(type="text", text="done")],
            stop_reason="end_turn", input_tokens=1, output_tokens=1,
        )

    await PresenceRenderer(chain_caller=chain).render(Briefing(
        relevant_context=(), filtered_context=(), decided_action=RespondOnly(),
        presence_directive="Complete the requested work.", audit_trace=AuditTrace(),
        turn_id="synthetic-turn", integration_run_id="synthetic-run",
        cognitive_context=packet,
    ))
    return captured["system"]


@pytest.mark.parametrize("zone, local_time", [
    ("America/Los_Angeles", "Wednesday, October 28, 2026 — 07:30 PM"),
    ("Asia/Tokyo", "Thursday, October 29, 2026 — 11:30 AM"),
])
async def test_packet_time_is_converted_and_keeps_utc_reference(zone, local_time):
    system = await _capture_system(
        timestamp=datetime(2026, 10, 29, 2, 30, tzinfo=timezone.utc),
        user_timezone=zone,
    )
    assert f"Current time: {local_time} ({zone}) / 2026-10-29 02:30 UTC" in system


def _envelope(**updates):
    return {
        "plan_id": "plan_example", "step_id": "s2",
        "step_description": "Write the fictional report",
        "budget_steps": 5, "steps_used": 2,
        **updates,
    }


async def test_final_step_reaches_model_with_full_deliverable_guidance():
    system = await _capture_system(
        timestamp=datetime(2026, 10, 29, tzinfo=timezone.utc),
        envelope=_envelope(is_final_step=True),
    )
    assert "Plan: plan_example | Step: s2 | Budget remaining: 3 steps" in system
    assert "Objective: Write the fictional report" in system
    assert "FINAL STEP" in system
    assert "produce the full detailed deliverable (not a summary)" in system
    assert "Your response will be sent directly to the user" in system


async def test_paused_plan_reaches_model_with_resume_and_limit_context():
    system = await _capture_system(
        timestamp=datetime(2026, 10, 29, tzinfo=timezone.utc),
        envelope=_envelope(paused=True, paused_reason="token_budget"),
    )
    assert "Plan plan_example is paused at step s2 (token budget)" in system
    assert "Used 2/5 steps" in system
    assert "Next step was: Write the fictional report" in system
    assert "action='continue' and the same plan_id" in system
    assert "budget_override with max_steps, max_tokens, or max_time_s" in system
    assert "Set to 0 for no limit" in system
    assert "FINAL STEP" not in system


async def test_running_plan_uses_auto_advance_guidance_on_both_prompt_paths():
    envelope = _envelope()
    system = await _capture_system(
        timestamp=datetime(2026, 10, 29, tzinfo=timezone.utc), envelope=envelope,
    )
    legacy = _build_now_block(
        SimpleNamespace(platform="internal", sender_auth_level=AuthLevel.owner_verified),
        Soul(instance_id="synthetic-instance"), None, execution_envelope=envelope,
    )
    for prompt in (system, legacy):
        assert "Plan: plan_example | Step: s2 | Budget remaining: 3 steps" in prompt
        assert "Objective: Write the fictional report" in prompt
        assert "The system advances the plan after this step completes" in prompt
        assert "then call manage_plan" not in prompt
        assert "FINAL STEP" not in prompt


@pytest.mark.parametrize("platform, guidance", [
    ("sms", "Keep responses very short"),
    ("discord", "Keep responses concise and clear"),
])
async def test_live_prompt_keeps_platform_response_guidance(platform, guidance):
    system = await _capture_system(
        timestamp=datetime(2026, 10, 29, tzinfo=timezone.utc), platform=platform,
    )
    assert guidance in system


async def test_observed_fact_keeps_its_source_label_in_live_prompt():
    system = await _capture_system(
        timestamp=datetime(2026, 10, 29, tzinfo=timezone.utc),
        knowledge_entries=(SimpleNamespace(
            content="Usually chooses a short report", lifecycle_archetype="habitual",
        ),),
    )
    assert "Usually chooses a short report [observed]" in system
