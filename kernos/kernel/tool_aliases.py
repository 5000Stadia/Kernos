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
    # 2026-05-24 spec alignment check: agent reached for an
    # advisory variant of consult to read a repo spec. No such
    # tool — closest surface is plain consult (advisory-mode is
    # achievable via prompt framing rather than a separate tool).
    "advisory_spec_retrieval_consult": "consult",
    # 2026-06-05 v1 self-test: agent reached for the consult tool under a
    # dotted namespace; it isn't registered, so the dispatcher rejected it.
    "external_agent.consult": "consult",
    # 2026-06-06 v1 self-test: the agent repeatedly reached for the file
    # tools under a dotted "files." namespace and for a "context_space_read"
    # variant. None are registered; all map to the flat file primitives.
    "files.write_file": "write_file",
    "files.read_file": "read_file",
    "files.read": "read_file",
    "files.list_files": "list_files",
    "files.delete_file": "delete_file",
    "context_space_read": "read_file",
    # 2026-06-06 v1 self-test: agent reached for manage_plan under a dotted
    # "planning." namespace before correcting to the flat name.
    "planning.manage_plan": "manage_plan",
    "planning.create_plan": "manage_plan",
    # 2026-06-07 live self-test: the model reaches for the plan primitive by
    # several invented names. They all mean manage_plan.
    "self_directed_plan": "manage_plan",
    "self_directed_execution": "manage_plan",
    "create_plan": "manage_plan",
    "start_plan": "manage_plan",
    # 2026-06-06 v1 self-test: agent reached for a bare "reminders"/"reminder"
    # tool for scheduling before correcting to manage_schedule.
    "reminders": "manage_schedule",
    "reminder": "manage_schedule",
    # 2026-06-06 v1 self-test: agent reached for manage_members under a dotted
    # "member_management." namespace; the rejection triggered a malformed
    # retry (empty action). Aliasing the dotted name lets the well-formed
    # first call land, avoiding the broken retry.
    "member_management.manage_members": "manage_members",
    "member_management.list_members": "manage_members",
}


def canonicalize_tool_name(
    name: str, known_tools: "frozenset[str] | set[str] | None" = None
) -> tuple[str, bool]:
    """Return ``(canonical_name, was_repaired)``.

    Two repair stages:

    1. **Curated exact-match** — the ``_TOOL_ALIASES`` dict above (specific
       observed hallucinations, including non-suffix ones like
       ``repository_inspection.report`` → ``ask_coding_session``).

    2. **Registry-aware dotted-suffix** (only when ``known_tools`` is supplied) —
       the model habitually namespaces tools as ``domain.verb``
       (``files.write_file``, ``planning.manage_plan``). When a dotted name is
       NOT itself a real tool but its final segment IS, repair to that segment.
       This is the safe form of the rule Codex flagged earlier: a *legitimate*
       dotted/MCP tool is present in ``known_tools``, so the ``name not in
       known_tools`` guard means it's never rewritten — only genuine
       hallucinations (whose full dotted name resolves to nothing) are. Without
       ``known_tools`` this stage is skipped, so the function stays pure for
       callers that can't supply the dispatch set.

    Callers MUST log ``TOOL_ALIAS_REPAIR alias=X canonical=Y`` at INFO level when
    ``was_repaired=True`` so the agent's misuse stays auditable.
    """
    canonical = _TOOL_ALIASES.get(name)
    if canonical is not None:
        return (canonical, True)
    if known_tools is not None and name not in known_tools:
        # Try each namespace separator the model reaches for: dot (its raw
        # reflex, provider-invalid so only ever seen as a hallucination) and
        # double-underscore (the SEMANTIC-ACTION-ENVELOPE `area__tool`
        # presentation form + the MCP convention). A legitimate dotted/__ tool
        # is in known_tools (caught by the guard above), so only genuine
        # hallucinations whose full name resolves to nothing are repaired.
        for _sep in (".", "__"):
            if _sep in name:
                suffix = name.rsplit(_sep, 1)[-1]
                if suffix and suffix in known_tools:
                    return (suffix, True)
    return (name, False)


async def emit_alias_repair_receipt(
    events,
    *,
    instance_id: str,
    requested: str,
    canonical: str,
    context: str,
) -> None:
    """Emit a structured TOOL_ALIAS_REPAIRED event.

    TOOL-ALIAS-RECEIPT-V1 (2026-05-23). Each repair leaves a
    first-class event in the stream so we have telemetry on
    *which* cognitive name-shapes the agent reaches for + how
    often, rather than burying the repair in a single log line.
    That corpus is the input for SEMANTIC-ACTION-ENVELOPE-V1
    design.

    Per kernel architecture, event emission is best-effort: a
    failure here MUST NOT break dispatch. Caller passes the
    events handle (may be None for tests / pre-init paths).
    """
    if events is None:
        return
    try:
        from kernos.kernel.event_types import EventType
        from kernos.kernel.events import emit_event
        await emit_event(
            events,
            EventType.TOOL_ALIAS_REPAIRED,
            instance_id or "",
            "kernel.tool_aliases",
            payload={
                "requested": requested,
                "canonical": canonical,
                "context": context,
            },
        )
    except Exception:
        # Best-effort: never break dispatch over a receipt failure.
        import logging
        logging.getLogger(__name__).warning(
            "TOOL_ALIAS_RECEIPT_EMIT_FAILED requested=%s canonical=%s "
            "context=%s",
            requested, canonical, context,
            exc_info=True,
        )


__all__ = ["canonicalize_tool_name", "emit_alias_repair_receipt"]
