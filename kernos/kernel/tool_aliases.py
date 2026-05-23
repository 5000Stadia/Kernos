"""Static tool-alias canonicalizer (SELF-CONTROLLED-LOOP-LIVENESS-V1).

Problem: the model occasionally calls critical kernel primitives by
hallucinated names (e.g. ``planning_orchestration.create_plan`` for
``manage_plan``). When that happens today, the substrate refuses with
"tool not registered" or returns "unknown" classification, and the
self-controlled loop never admits — even though the canonical tool
IS surfaced and the system prompt names it.

A critical kernel primitive cannot depend on the model remembering
one exact tool name. This module provides a deterministic
canonicalization step at dispatch ingress: if the model calls a
known alias, the substrate repairs to the canonical name and logs
the repair for audit. No regex, no fuzzy match, no LLM "did you
mean" — just a static dict and a one-line lookup.

The dict starts small (the actually-observed model hallucinations).
Extending is a one-line edit; every repair logs ``TOOL_ALIAS_REPAIR``
so operator review surfaces new entries cleanly.

V1 only repairs the tool NAME. Argument-shape repair is out of scope —
if a known alias is called with wrong args, the canonical tool's own
validation returns an error message.
"""
from __future__ import annotations


_TOOL_ALIASES: dict[str, str] = {
    # 2026-05-21 smoke test: agent hallucinated this for manage_plan.
    "planning_orchestration.create_plan": "manage_plan",
    # 2026-05-21 smoke test: agent hallucinated this for manage_plan
    # (intended action='continue' or workspace-side artifact write,
    # but neither tool exists by that name).
    "workspace_plan_artifact_write": "manage_plan",
    # 2026-05-22 canvas test: agent hallucinated a namespaced
    # cross-space dispatch — intended request_space_action.
    "external_code_consultation.request_space_action": "request_space_action",
    # Same agent batch — the bot tried a generic "give me a
    # repo inspection report" name. No exact canonical lives at
    # the same surface; ask_coding_session is the closest
    # general-purpose external-investigator. Repair to that so
    # the call at least reaches a working surface; the agent's
    # `question` parameter still carries the inspection intent.
    "repository_inspection.report": "ask_coding_session",
    # 2026-05-23 improve_kernos soak: after one successful
    # improve_kernos(...) call, the agent retried with two
    # dotted-namespace variants that don't exist. Both intended
    # the same surface — start a new autonomous-improvement
    # attempt. Repair to the canonical name so the retry lands.
    "kernel.autonomous_improvement.start_attempt": "improve_kernos",
    "kernel.autonomous_improvement": "improve_kernos",
}


def canonicalize_tool_name(name: str) -> tuple[str, bool]:
    """Return ``(canonical_name, was_repaired)``.

    Callers MUST log ``TOOL_ALIAS_REPAIR alias=X canonical=Y`` at
    INFO level when ``was_repaired=True`` so the agent's misuse stays
    auditable + new entries can be detected by operator log review.
    """
    canonical = _TOOL_ALIASES.get(name)
    if canonical is None:
        return (name, False)
    return (canonical, True)


__all__ = ["canonicalize_tool_name"]
