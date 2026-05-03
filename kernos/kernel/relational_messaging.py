"""Relational Messaging primitive (RELATIONAL-MESSAGING v5).

Envelope model and lifecycle constants. One member's agent can initiate a
purposeful exchange (request_action / ask_question / inform) with another
member's agent. The receiving agent's turn naturally applies the disclosure
gate to what it can see — we don't need to gate the conversation itself.

The dispatcher (see kernos/kernel/relational_dispatch.py) orchestrates
permission checks, atomic state transitions, and two delivery paths:
  - Immediate push for time_sensitive urgency (via the adapter layer)
  - Next-turn surfacing for elevated and normal urgency (queued, picked
    up during the recipient's assemble phase)

the design review's sole implementation hazard in v5 review was atomic state transitions.
The StateStore's new transition_relational_message_state() is implemented
as genuine CAS (SQLite: UPDATE ... WHERE state=? + rowcount check; JSON:
read-check-write under filelock). Never read-check-write at the call site.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# --- Enums (intentionally simple strings, not Enum, to match existing style) ---

INTENTS = ("request_action", "ask_question", "inform")
URGENCIES = ("time_sensitive", "elevated", "normal")
STATES = ("pending", "delivered", "surfaced", "resolved", "expired")

# Expiration windows (seconds).
EXPIRATION_BY_URGENCY = {
    "time_sensitive": 24 * 3600,        # 24h
    "elevated": 7 * 24 * 3600,           # 7d
    "normal": 3 * 24 * 3600,             # 72h
}

# Permission matrix — sender's side toward addressee × intent → allowed.
# Missing relationship row is the implicit "by-permission" default.
_PERMISSION_MATRIX = {
    "no-access": {
        "request_action": False, "inform": False, "ask_question": False,
    },
    "by-permission": {
        "request_action": True, "inform": True, "ask_question": False,
    },
    "full-access": {
        "request_action": True, "inform": True, "ask_question": True,
    },
}


def dispatch_permitted(sender_side_permission: str, intent: str) -> bool:
    """Per spec's Addressing and Dispatch Permission table.

    sender_side_permission: what the ORIGIN member has declared toward the
    addressee (M→X direction from the simplified relationship model).
    Missing declaration defaults to "by-permission".
    """
    side = (sender_side_permission or "by-permission").strip().lower()
    row = _PERMISSION_MATRIX.get(side, _PERMISSION_MATRIX["by-permission"])
    return bool(row.get(intent, False))


@dataclass
class RelationalMessage:
    """Envelope for one agent-to-agent message.

    All identifiers are plain strings; the origin_agent_identity is the name
    the originating member's agent uses in its own space (e.g., "Slate" or
    the display name of the agent if it hasn't named itself).
    """
    id: str                              # rm_{ts}_{rand}
    instance_id: str
    origin_member_id: str                # the member whose agent sent
    origin_agent_identity: str           # agent's self-name at send time
    addressee_member_id: str             # the member whose agent receives
    intent: str                          # request_action | ask_question | inform
    content: str                         # body
    urgency: str                         # time_sensitive | elevated | normal
    conversation_id: str                 # rm_conv_{uuid} — threads multi-turn
    state: str                           # pending | delivered | surfaced | resolved | expired
    created_at: str                      # ISO 8601
    target_space_hint: str = ""          # advisory; recipient's spaces are authority
    delivered_at: str = ""
    surfaced_at: str = ""
    resolved_at: str = ""
    expired_at: str = ""
    resolution_reason: str = ""          # outcome category for trace
    reply_to_id: str = ""                # chains replies to a prior message
    #: PARCEL-PRIMITIVE-V1: discriminates free-form messages (default) from
    #: structured envelope types like ``parcel_offer``. Recipients'
    #: surfacing code reads this to render structured metadata without
    #: relying on content parsing.
    envelope_type: str = "message"
    #: Optional back-reference to a parcel when envelope_type='parcel_offer'.
    parcel_id: str = ""
    #: CANVAS-V1 back-reference when envelope_type in {'canvas_offer',
    #: 'route_fire'}. Empty for non-canvas envelopes.
    canvas_id: str = ""


def generate_message_id() -> str:
    """Generate a new relational-message id: rm_{ts_us}_{rand4}."""
    import secrets
    import time
    ts_us = int(time.time() * 1_000_000)
    rand = secrets.token_hex(2)
    return f"rm_{ts_us}_{rand}"


def generate_conversation_id() -> str:
    """Namespaced so relational-message conversations never collide with
    platform conversation_ids (which are platform-assigned channel ids).
    """
    import uuid
    return f"rm_conv_{uuid.uuid4().hex[:12]}"
