"""Action verb reversibility classification.

WORKFLOW-LOOP-PRIMITIVE C3 / C4. The classification feeds the
safe-deny constraint on ``auto_proceed_with_default`` approval
gates: if a gate's timeout behaviour auto-proceeds with a default
value, the action sequence between that gate and the next gate (or
workflow end) must contain no irreversible actions — otherwise a
silent timeout could permit world-effecting downstream work without
human approval.

Classification is intentionally conservative for ``call_tool``: in
v1 every tool dispatch is treated as irreversible. C4's action
library can refine the lookup once individual tools declare their
own reversibility metadata; until then the safe default is "must be
gated explicitly".
"""
from __future__ import annotations


WORLD_EFFECT_VERBS = frozenset({
    "notify_user",
    "write_canvas",
    "route_to_agent",
    "call_tool",
    "post_to_service",
})

DIRECT_EFFECT_VERBS = frozenset({
    "mark_state",
    "append_to_ledger",
})

# WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 5 / Codex round 1
# Medium 7: branch is a control-flow verb (mutates next_step_index;
# no world effect). operation_class=mutate; risk_level=medium per
# Spec 3's derivation table.
CONTROL_FLOW_VERBS = frozenset({"branch"})

KNOWN_ACTION_TYPES = WORLD_EFFECT_VERBS | DIRECT_EFFECT_VERBS | CONTROL_FLOW_VERBS


def is_irreversible(action_type: str, parameters: dict | None = None) -> bool:
    """Return True if executing this action cannot be safely undone.

    Used by workflow validation to enforce the safe-deny constraint
    on ``auto_proceed_with_default`` approval gates. Unknown action
    types raise ``ValueError`` so workflow descriptors cannot smuggle
    in unaudited verbs.
    """
    parameters = parameters or {}
    if action_type == "notify_user":
        return True
    if action_type == "write_canvas":
        # append-mode is reversible; replace-mode on canvases without
        # versioning is not.
        return parameters.get("append_or_replace", "append") == "replace"
    if action_type == "route_to_agent":
        return True
    if action_type == "call_tool":
        # Conservative default. C4's action library can refine when
        # individual tools declare reversibility metadata.
        return True
    if action_type == "post_to_service":
        return True
    if action_type == "mark_state":
        return False
    if action_type == "append_to_ledger":
        return False
    if action_type == "branch":
        # Pure control flow; no world effect. Reversible by replay.
        return False
    raise ValueError(f"unknown action_type: {action_type!r}")


__all__ = [
    "CONTROL_FLOW_VERBS",
    "DIRECT_EFFECT_VERBS",
    "KNOWN_ACTION_TYPES",
    "WORLD_EFFECT_VERBS",
    "is_irreversible",
]
