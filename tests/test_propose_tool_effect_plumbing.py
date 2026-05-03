"""Pin tests for INTEGRATION-CAPABILITY-FIRST-V1 Batch 2 Fold 2a.

Architect verdict 2026-05-03 on Batch 1 closeout: defense in depth on
the propose-tool effect distinction. (a) Add an ``effect`` field to
``ProposeTool`` and thread it through the propose user-message
renderer so the model sees the classification it should respect.
(b) Dispatch-time enforcement using actual call arguments (Batch 2
Fold 2b — separate test file).

This file pins (a). Same architectural pattern as covenant
determinism in CCV1: substrate flows deterministically AND the
integration LLM may add framing on top.
"""

from __future__ import annotations

import pytest

from kernos.kernel.enactment.presence_renderer import (
    _user_message_propose_tool,
)
from kernos.kernel.integration.briefing import (
    ActionEnvelope,
    AuditTrace,
    BriefingValidationError,
    Briefing,
    ProposeTool,
)


def _briefing_with_propose(propose: ProposeTool) -> Briefing:
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=propose,
        presence_directive="Be helpful and direct.",
        audit_trace=AuditTrace(),
        turn_id="conv-pt-effect",
        integration_run_id="run-pt-effect",
    )


# ---------------------------------------------------------------------------
# Dataclass pins
# ---------------------------------------------------------------------------


def test_propose_tool_carries_effect_field():
    """Pin: ProposeTool dataclass exposes ``effect`` so the integration
    runner can populate it from the gate's tool-effects map and the
    presence renderer can surface it deterministically to the model.
    Without this field the safety property of the propose-tool kind
    prompt depends on model inference from tool name alone."""
    pt = ProposeTool(
        tool_id="brave_web_search",
        arguments={"q": "weather"},
        reason="user asked for live weather",
        effect="read",
    )
    assert pt.effect == "read"


def test_propose_tool_effect_defaults_to_empty_string():
    """Pin: backward-compat default is empty string. Existing callers
    pre-Fold-2a remain valid; renderer treats empty as "unknown /
    propose conservatively"."""
    pt = ProposeTool(
        tool_id="brave_web_search",
        arguments={"q": "weather"},
        reason="user asked for live weather",
    )
    assert pt.effect == ""


def test_propose_tool_rejects_non_string_effect():
    """Pin: validation enforces string type so accidental None /
    list / int values surface at construction rather than being
    rendered as ``str(None)`` / ``str([])`` in the user message."""
    with pytest.raises(BriefingValidationError):
        ProposeTool(
            tool_id="x",
            arguments={},
            reason="r",
            effect=None,  # type: ignore[arg-type]
        )


def test_propose_tool_to_dict_round_trips_effect():
    """Pin: serialization carries effect; from_dict restores it.
    Critical for any persistence / replay path that round-trips
    briefings as JSON."""
    original = ProposeTool(
        tool_id="list-events",
        arguments={"calendar": "primary"},
        reason="user asked about calendar",
        effect="read",
    )
    data = original.to_dict()
    assert data["effect"] == "read"
    restored = ProposeTool.from_dict(data)
    assert restored.effect == "read"


def test_propose_tool_from_dict_handles_missing_effect_field():
    """Pin: a pre-Fold-2a serialized briefing (no ``effect`` key)
    deserializes to empty effect rather than raising. Backward-compat
    invariant for any persisted briefings."""
    legacy = {
        "kind": "propose_tool",
        "tool_id": "x",
        "arguments": {},
        "reason": "r",
        # no "effect" key
    }
    pt = ProposeTool.from_dict(legacy)
    assert pt.effect == ""


# ---------------------------------------------------------------------------
# User-message renderer pins — effect surfaces to the model
# ---------------------------------------------------------------------------


def test_user_message_surfaces_read_effect_with_inline_guidance():
    """Pin: when effect=read, the user message tells the model the
    tool is non-destructive and safe to call inline. Aligns with
    the kind prompt's "If read-only / non-destructive, call inline"
    directive."""
    pt = ProposeTool(
        tool_id="list-events",
        arguments={},
        reason="user asked about calendar",
        effect="read",
    )
    msg = _user_message_propose_tool(_briefing_with_propose(pt))
    assert "read" in msg
    assert "inline" in msg.lower()
    assert "non-destructive" in msg.lower()


def test_user_message_surfaces_soft_write_effect_with_propose_guidance():
    """Pin: when effect=soft_write, the user message tells the
    model to propose-then-confirm. Aligns with the kind prompt's
    "propose only when effect is irreversible or affects others"."""
    pt = ProposeTool(
        tool_id="create-event",
        arguments={"title": "test"},
        reason="user asked to create event",
        effect="soft_write",
    )
    msg = _user_message_propose_tool(_briefing_with_propose(pt))
    assert "soft_write" in msg
    assert "propose first" in msg.lower()


def test_user_message_surfaces_hard_write_effect_with_propose_guidance():
    """Pin: hard_write also propose-first, with the destructive
    classification visible in the prompt."""
    pt = ProposeTool(
        tool_id="delete-event",
        arguments={"id": "x"},
        reason="user asked to delete event",
        effect="hard_write",
    )
    msg = _user_message_propose_tool(_briefing_with_propose(pt))
    assert "hard_write" in msg
    assert "propose first" in msg.lower()


def test_user_message_treats_empty_effect_as_conservative_unknown():
    """Pin: backward-compat — when effect is empty (pre-Fold-2a
    callers), the user message renders as "unknown — treat as
    soft_write conservatively". The model still has an unambiguous
    signal even when the integration phase didn't populate the
    field. This is the conservative-fallback half of defense in
    depth."""
    pt = ProposeTool(
        tool_id="mystery_tool",
        arguments={},
        reason="testing legacy briefing",
    )
    msg = _user_message_propose_tool(_briefing_with_propose(pt))
    assert "unknown" in msg.lower()
    assert "conservatively" in msg.lower() or "propose first" in msg.lower()


def test_user_message_unrecognized_effect_falls_back_conservatively():
    """Pin: future / unrecognized effect tokens render as
    "treat as soft_write conservatively". Defensive — never let an
    unrecognized classification render as silently safe to inline."""
    pt = ProposeTool(
        tool_id="future_tool",
        arguments={},
        reason="testing forward-compat",
        effect="experimental_classification",
    )
    msg = _user_message_propose_tool(_briefing_with_propose(pt))
    assert "experimental_classification" in msg
    # Must NOT promote unknown effect to inline
    lower = msg.lower()
    assert "propose first" in lower or "conservatively" in lower
