"""Codex review (2026-06-07): self-directed plan steps must execute under the
member who CREATED the plan, not the global instance owner. A non-owner's plan
running with the owner's profile/spaces/credentials is a cross-member context
leak. The plan records created_by_member_id; the envelope carries it; the
synthetic step message carries it; the internal bypass only falls back to the
owner when it's absent (legacy plans).
"""
import inspect

from kernos.kernel.execution import build_envelope_from_plan, ExecutionEnvelope
from kernos.messages.handler import MessageHandler


def test_envelope_carries_plan_creator_member_id():
    plan = {
        "plan_id": "plan_x",
        "workspace_id": "space_1",
        "created_by_member_id": "mem_abc123",
        "budget": {}, "usage": {},
    }
    env = build_envelope_from_plan(plan, "s1", "do the thing")
    assert env.member_id == "mem_abc123"


def test_envelope_member_id_defaults_empty_for_legacy_plans():
    plan = {"plan_id": "p", "workspace_id": "w", "budget": {}, "usage": {}}
    env = build_envelope_from_plan(plan, "s1", "x")
    assert env.member_id == ""


def test_execution_envelope_has_member_id_field():
    env = ExecutionEnvelope(plan_id="p", step_id="s", workspace_id="w",
                            step_description="d")
    assert env.member_id == ""


def test_self_directed_message_carries_envelope_member_id():
    """Static pin: the synthetic plan-step message threads the envelope's
    member_id so the step runs under the plan owner's context."""
    src = inspect.getsource(MessageHandler._execute_self_directed_step)
    assert "member_id=envelope.member_id" in src, (
        "the self-directed step message must carry envelope.member_id"
    )


def test_manage_plan_handler_accepts_creator_member_id():
    sig = inspect.signature(MessageHandler._handle_manage_plan)
    assert "creator_member_id" in sig.parameters, (
        "_handle_manage_plan must accept the creating member's id to record "
        "created_by_member_id on the plan"
    )


def test_internal_bypass_prefers_existing_member_id_over_owner():
    """The owner fallback is gated behind `if not message.member_id`, so a
    plan creator already threaded onto the message is never overwritten."""
    src = inspect.getsource(MessageHandler._check_early_return)
    internal_pos = src.find('message.platform == "internal"')
    block = src[internal_pos: src.find('_resolve_incoming(')]
    assert "if not message.member_id" in block, (
        "owner resolution must only run when member_id is absent"
    )


def test_secure_input_excludes_internal_turns():
    """Codex review: secure-input capture is keyed only by instance_id, so a
    self-directed step firing while a credential session is open would store
    its '[PLAN STEP ...]' content as the credential and abort the step. The
    interception must exclude platform=internal turns."""
    src = inspect.getsource(MessageHandler._check_early_return)
    assert 'message.platform != "internal"' in src, (
        "secure-input interception must exclude internal/self-directed turns"
    )
    # Pin that the exclusion is on the secure_input_state guard specifically.
    guard_pos = src.find("instance_id in self._secure_input_state")
    assert guard_pos != -1
    line_end = src.find("\n", guard_pos)
    assert 'message.platform != "internal"' in src[guard_pos:line_end], (
        "the != 'internal' exclusion must be on the secure_input_state guard"
    )


def test_self_directed_auth_derived_not_hardcoded_owner():
    """Codex review: a non-owner's plan step must not hardcode owner_verified.
    The auth level is derived from the creator vs the owner."""
    src = inspect.getsource(MessageHandler._execute_self_directed_step)
    assert "sender_auth_level=_auth" in src, (
        "the step message auth level must be derived (_auth), not hardcoded "
        "to owner_verified"
    )
    assert "AuthLevel.trusted_contact" in src, (
        "a known non-owner creator must run as trusted_contact"
    )
