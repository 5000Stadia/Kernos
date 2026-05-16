"""Concrete PresenceRenderer implementing PDI's PresenceRendererLike (IWL C4).

Single renderer with kind-aware prompting. Branches internally on
`briefing.decided_action.kind` and produces user-facing text.

Method signature (per the design review edit, locked):

    await render(briefing) -> PresenceRenderResult

NOT AsyncIterator. Streaming happens internally through the
adapter/event sink; the `result.streamed` flag signals whether
streaming occurred during render. The renderer returns the final
accumulated text in `result.text` regardless.

B1 / B2 STRUCTURAL SAFETY (the design review edit, load-bearing):

For B1 termination rendering and B2-routed clarification rendering,
the renderer MUST NOT receive `discovered_information`. Dedicated
input dataclasses (`B1RenderInputs` / `B2RenderInputs`) structurally
exclude that field. The renderer's B1/B2 paths consume these
dedicated input types — the unsafe field literally cannot be passed
to the renderer's prompt construction because it does not exist on
the input type.

Construction-time invariant, not runtime check. Sentinel test pin
seeds discovered_information with "RESTRICTED_SENTINEL_XYZ" at the
upstream construction site and verifies the sentinel is absent from
both the renderer's prompt input AND the rendered output.

Streaming flag: thin path streams to adapter (PDI C5 invariant).
The streaming surface is the adapter/event sink; render() awaits
and returns PresenceRenderResult with the final accumulated text
plus a `streamed` flag.

Same-model default (the design review edit, locked): the renderer's chain_caller
is the same callable integration uses by default. Per-hook
differentiation deferred until soak telemetry justifies.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from kernos.kernel.enactment.service import (
    PresenceRenderResult,
)
from kernos.kernel.integration.briefing import (
    ActionKind,
    Briefing,
    ClarificationNeeded,
    ClarificationPartialState,
    ConstrainedResponse,
    Defer,
    ExecuteTool,
    Pivot,
    ProposeTool,
    RespondOnly,
)
from kernos.providers.base import ContentBlock, ProviderResponse


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ChainCaller — same shape integration uses
# ---------------------------------------------------------------------------


ChainCaller = Callable[
    [str | list[dict], list[dict], list[dict], int],
    Awaitable[ProviderResponse],
]


# INTEGRATION-CAPABILITY-FIRST-V1 (Batch 1, piece C): tool dispatcher
# protocol for the bounded tool-use loop. Returns the textual content
# to be wrapped into a tool_result block. Errors propagate to the
# loop's friendly-failure fallback (logged + reported as tool error
# text rather than crashing the render).
ToolDispatcher = Callable[
    ...,  # tool_name, tool_input, tool_use_id, conversation_id (all kw)
    Awaitable[str],
]


# ---------------------------------------------------------------------------
# B1 / B2 safe render inputs — structural redaction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class B2RenderInputs:
    """Safe input type for B2-routed clarification rendering.

    Per the design review edit (load-bearing): the renderer's B2 path consumes
    THIS type, NOT the full ClarificationPartialState. The
    `discovered_information` field is structurally absent — the
    renderer literally cannot access it because the type doesn't
    have the field.

    The unsafe `discovered_information` lives in audit / reintegration
    only, where it's referenced by audit_refs. Sentinel test pin
    seeds discovered_information at the upstream construction site
    and verifies the sentinel never reaches the renderer.

    Fields:
      - question: ≤200 chars, the user-facing sentence
      - blocking_ambiguity: ≤500 chars, only when the upstream
        constructor deems it safe (the partial_state's blocking
        description is generally safe; restricted material would
        be redacted by the divergence reasoner upstream)
      - safe_question_context: ≤500 chars, presence-safe context
      - audit_refs: references for audit-only deeper context

    NOTE on `discovered_information`: STRUCTURALLY ABSENT. Adding
    this field is a contract break that compromises the B2 safety
    invariant; sentinel test enforces.
    """

    question: str
    blocking_ambiguity: str = ""
    safe_question_context: str = ""
    audit_refs: tuple[str, ...] = ()

    @classmethod
    def from_partial_state(
        cls,
        *,
        question: str,
        partial_state: ClarificationPartialState | None,
    ) -> "B2RenderInputs":
        """Construct safe inputs from a ClarificationPartialState
        WITHOUT copying discovered_information. The partial state's
        discovered_information is the field the safety invariant
        protects — this constructor drops it on the floor by design."""
        if partial_state is None:
            return cls(question=question)
        return cls(
            question=question,
            blocking_ambiguity=partial_state.blocking_ambiguity,
            safe_question_context=partial_state.safe_question_context,
            audit_refs=partial_state.audit_refs,
        )


@dataclass(frozen=True)
class B1RenderInputs:
    """Safe input type for B1 termination rendering.

    Per the design review edit (load-bearing): the renderer's B1 path consumes
    THIS type. The `discovered_information` field is structurally
    absent. The unsafe field lives in the ReintegrationContext
    payload (audit-only at v1).

    Fields:
      - intended_outcome_summary: safe summary derived from the
        original briefing's action_envelope.intended_outcome
      - attempted_action_summary: capped per PDI C1's
        ClarificationPartialState contract; describes what was
        attempted at the surface level
      - audit_refs: references for audit-only deeper context

    NOTE on `discovered_information`: STRUCTURALLY ABSENT. Same
    safety invariant as B2.
    """

    intended_outcome_summary: str
    attempted_action_summary: str = ""
    audit_refs: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# System prompts — kind-aware
# ---------------------------------------------------------------------------


# INTEGRATION-CAPABILITY-FIRST-V1 (Batch 1, piece B): kind prompts
# rewritten capability-first per saved feedback memory
# `feedback_capability_first_posture`. Pre-spec these prompts contained
# explicit "No tool calls." / "Do NOT execute the tool" / "Plain text."
# directives that, combined with the surfaced_tools-empty bug, told the
# model not to use tools even when they were in its tool definitions.
# Per-prompt load-bearing check is documented inline.

_SYSTEM_PROMPT_RESPOND_ONLY = """\
You are Kernos's presence renderer. The decided action is a
conversational reply. Answer the user directly; keep tone consistent
with the briefing's directive. If a tool call would genuinely serve
the user's request, call it — the kind classification didn't surface
one because integration didn't see one as needed, but the model is
the authority on what helps. Brief and direct unless the directive
asks for depth.
"""
# Load-bearing check (RESPOND_ONLY): pre-spec the prompt forbade tool
# calls explicitly ("No tool calls."). That constraint was a legacy
# render-only assumption — there is no architectural reason a
# conversational reply cannot benefit from a quick tool consultation.
# Constraint dropped; capability-first posture restored.

_SYSTEM_PROMPT_DEFER = """\
You are Kernos's presence renderer. The decided action is to defer.
Acknowledge the user briefly, signal that this turn is being deferred,
and indicate the follow-up shape from the briefing. A tool call here
is unusual but not forbidden if it materially helps explain the
deferral.
"""
# Load-bearing check (DEFER): pre-spec said "Plain text response."
# Plain text is the typical output but not a hard constraint —
# capability-first means the model decides whether a tool helps.

_SYSTEM_PROMPT_CONSTRAINED_RESPONSE = """\
You are Kernos's presence renderer. The decided action is a constrained
response: respond partially under the named constraint. Surface what
can be said within the constraint; acknowledge what cannot. The
constraint applies to scope, not to capability — use tools where they
help fulfill the request within the named scope.
"""
# Load-bearing check (CONSTRAINED_RESPONSE): pre-spec ended with
# "Plain text." The scope constraint (respond partially under named
# constraint) IS load-bearing — that's what the kind means. The
# format constraint (plain text, no tools) was incidental and
# anti-capability. Scope stays explicit; capability framing replaces
# the plain-text-only directive.

_SYSTEM_PROMPT_PIVOT = """\
You are Kernos's presence renderer. The decided action is to pivot:
generate a different shape of response than the literal request
asked for. Use the briefing's reason and suggested_shape. Tool calls
are appropriate when the pivot benefits from real-world data.
"""
# Load-bearing check (PIVOT): pre-spec said "Plain text." Pivot is
# about response shape, not output format — capability-first allows
# tool use when it serves the pivoted shape.

_SYSTEM_PROMPT_PROPOSE_TOOL = """\
You are Kernos's presence renderer. The decided action is propose_tool.
If the proposed tool is read-only / non-destructive (effect "read"),
call it directly — the user is best served by a real result rather
than a confirmation prompt. Propose-then-confirm only when the effect
is irreversible or affects others (writes, deletions, sends, payments,
shared-state changes). When proposing, render the proposal as plain
text awaiting confirmation.
"""
# Load-bearing check (PROPOSE_TOOL): pre-spec said "Do NOT execute the
# tool — the proposal is the output." That blanket rule was a real
# anti-capability source: read-only tool calls have no consequence
# requiring confirmation. Distinguish by effect: read = inline,
# destructive = propose. Real safety constraint preserved (destructive
# tools still propose-then-confirm).

_SYSTEM_PROMPT_CLARIFICATION_FIRST_PASS = """\
You are Kernos's presence renderer. Integration emitted a
clarification_needed first-pass — critical info is missing. Render the
structured question naturally to the user. Plain text.
"""

_SYSTEM_PROMPT_CLARIFICATION_B2 = """\
You are Kernos's presence renderer. Mid-action ambiguity surfaced.
Render the user-facing question naturally and acknowledge that partial
work has happened (without exposing restricted material — the input
type structurally excludes it). Plain text.
"""

_SYSTEM_PROMPT_B1_TERMINATION = """\
You are Kernos's presence renderer. The action was invalidated mid-
execution. Render brief acknowledgment of the partial work plus the
new framing — "I started X but discovered Y; not proceeding." Plain
text. Do NOT include restricted material — the input type structurally
excludes it.
"""

_SYSTEM_PROMPT_FULL_MACHINERY_TERMINAL = """\
You are Kernos's presence renderer. A multi-step action has completed.
Render the terminal user-facing response naturally given the briefing's
directive and the action context. The full-machinery loop has
terminated; a follow-up tool call here would be unusual but is
permitted if it genuinely serves the wrap-up (e.g., a quick read to
confirm the final state). Streaming permitted.
"""
# Load-bearing check (FULL_MACHINERY_TERMINAL): pre-spec said "Plain
# text. Streaming permitted — the loop has terminated." Terminal
# framing is correct (the multi-step loop did finish). The plain-text
# directive was implicit no-tools for legacy reasons. Capability-first:
# the model can do a small follow-up read if it materially helps the
# wrap-up. Streaming retained as architectural fact.


def _system_prompt_for_kind(kind: ActionKind) -> str:
    """Pick the kind-aware system prompt. The renderer dispatches
    structurally on briefing.decided_action.kind (no model judgment
    in path selection)."""
    if kind is ActionKind.RESPOND_ONLY:
        return _SYSTEM_PROMPT_RESPOND_ONLY
    if kind is ActionKind.DEFER:
        return _SYSTEM_PROMPT_DEFER
    if kind is ActionKind.CONSTRAINED_RESPONSE:
        return _SYSTEM_PROMPT_CONSTRAINED_RESPONSE
    if kind is ActionKind.PIVOT:
        return _SYSTEM_PROMPT_PIVOT
    if kind is ActionKind.PROPOSE_TOOL:
        return _SYSTEM_PROMPT_PROPOSE_TOOL
    if kind is ActionKind.CLARIFICATION_NEEDED:
        # The B2 vs first-pass split happens at the user-message
        # construction layer based on partial_state presence.
        return _SYSTEM_PROMPT_CLARIFICATION_FIRST_PASS
    if kind is ActionKind.EXECUTE_TOOL:
        return _SYSTEM_PROMPT_FULL_MACHINERY_TERMINAL


# ---------------------------------------------------------------------------
# COGNITIVE-CONTEXT-V1 C3a/C3b — substrate rendering from the typed packet
# ---------------------------------------------------------------------------


def _render_substrate(packet: Any) -> str:
    """Render the cognitive substrate from the typed CognitiveContext
    packet to a system-prompt-shaped string.

    Phase scope (renamed from ``_render_c3a_substrate`` at C3b to
    reflect expanded zone coverage):

    * C3a renders RULES + NOW + STATE.
    * C3b adds ## RESULTS + ## ACTIONS + ## PROCEDURES +
      ## AVAILABLE CANVASES.
    * C3c adds the additive presence_directive into the same final
      system render.
    * C4 expands MEMORY (compaction carry + gardener observations).
    * C5 lands tool surface in the tools= argument (separate from
      this substrate string).

    Returns an empty string when ``packet`` is None so legacy /
    pre-C3a callers see the original kind-aware prompt unchanged.

    C3b renders RESULTS / ACTIONS / PROCEDURES / CANVASES but NOT
    ## MEMORY (compaction_carry + awareness_whispers). MEMORY zone
    rendering is deferred to C4 alongside the cohort registration
    work — keeps the C4 phase signal meaningful. Note: awareness
    whispers reach the model via legacy's results_prefix at C3b
    (the legacy assemble flows them into RESULTS), which means
    test 6 may flip green at C3b ahead of C4. That's a side-effect
    of legacy's own zone placement, not a wiring-ladder violation.
    """
    if packet is None:
        return ""

    parts: list[str] = []

    # ---- RULES ----
    rules_parts: list[str] = []
    rules = getattr(packet, "rules", None)
    if rules is not None:
        if rules.operating_principles:
            rules_parts.append(rules.operating_principles)
        if rules.instance_stewardship:
            rules_parts.append(
                f"INSTANCE PURPOSE:\n{rules.instance_stewardship}\n"
                f"This is what this Kernos instance is for. When values "
                f"conflict or tradeoffs exist, orient your judgment "
                f"toward this purpose."
            )
        if rules.covenants:
            cov_lines = []
            for cov in rules.covenants:
                desc = getattr(cov, "description", "")
                if desc:
                    cov_lines.append(f"- {desc}")
            if cov_lines:
                rules_parts.append("ACTIVE COVENANTS:\n" + "\n".join(cov_lines))
        if rules.bootstrap_prompt:
            rules_parts.append(rules.bootstrap_prompt)
        if rules.hatching_prompt:
            # The packet stores the raw template; substitute name +
            # name-instruction fields available from the state /
            # NOW slices (renderer handles substitution since the
            # packet keeps templates raw — see C1 design pass).
            state = getattr(packet, "state", None)
            mp = (
                state.member_profile if state is not None else {}
            ) or {}
            display_name = mp.get("display_name", "") or "there"
            agent_name = mp.get("agent_name", "")
            name_instruction = (
                f"You already know their name — {display_name}. "
                f"DO NOT ask for it again."
                if display_name and display_name != "there"
                else "You don't know their name yet. Ask naturally."
            )
            rendered = rules.hatching_prompt
            try:
                rendered = rendered.format(
                    display_name=display_name,
                    agent_name=agent_name,
                    name_instruction=name_instruction,
                )
            except (KeyError, IndexError):
                # Template has placeholders we don't know — keep the
                # raw template; absence of substitution still preserves
                # the substrate's reach to the model.
                pass
            rules_parts.append(rendered)
    if rules_parts:
        parts.append("## RULES\n" + "\n\n".join(rules_parts))

    # ---- NOW ----
    now = getattr(packet, "now", None)
    if now is not None:
        now_lines = []
        ts = getattr(now, "timestamp_utc", None)
        if ts is not None:
            now_lines.append(
                f"Current time: {ts.isoformat()} "
                f"({now.user_timezone or 'UTC'})"
            )
        if now.platform:
            now_lines.append(f"Platform: {now.platform}")
        if now.auth_level:
            now_lines.append(f"Sender auth level: {now.auth_level}")
        if now.member_display_name:
            now_lines.append(
                f"Speaking with: {now.member_display_name}"
            )
        if now.active_space_name:
            now_lines.append(
                f"Active space: {now.active_space_name}"
            )
        if now_lines:
            parts.append("## NOW\n" + "\n".join(now_lines))

    # ---- STATE ----
    state = getattr(packet, "state", None)
    if state is not None:
        state_parts: list[str] = []
        mp = state.member_profile or {}
        agent_name = mp.get("agent_name", "")
        soul = state.soul
        # Identity line: agent name + personality
        personality = mp.get("personality_notes", "") or (
            getattr(soul, "personality_notes", "") if soul else ""
        )
        if agent_name:
            state_parts.append(
                f"Identity: {agent_name}\n{personality}"
                if personality else f"Identity: {agent_name}"
            )
        elif personality:
            state_parts.append(personality)
        # User context — name + knowledge entries
        user_lines: list[str] = []
        display_name = (
            mp.get("display_name", "")
            or (getattr(soul, "user_name", "") if soul else "")
        )
        if display_name:
            user_lines.append(f"Name: {display_name}")
        for entry in state.knowledge_entries or ():
            content = getattr(entry, "content", "")
            if content:
                user_lines.append(content)
        if user_lines:
            state_parts.append("USER CONTEXT:\n" + "\n".join(user_lines))
        # Relationships — only render non-default declarations
        rel_lines: list[str] = []
        active_id = mp.get("member_id", "")
        for r in state.relationships or ():
            perm = r.get("permission", "by-permission")
            if perm == "by-permission":
                continue
            name = r.get("other_display_name", "?")
            if active_id and r.get("declarer_member_id") == active_id:
                rel_lines.append(f"{name} (you → {perm})")
            else:
                rel_lines.append(f"{name} ({perm} ← them)")
        if rel_lines:
            state_parts.append("RELATIONSHIPS:\n" + ", ".join(rel_lines))
        # C3b-review CONCERN fold: render a compact safety
        # subsection inside ## STATE when populated. Spec says
        # safety_constraints are "rules the model must honor"; legacy
        # has no explicit safety zone but the substrate must reach
        # the model. A subsection inside STATE keeps the reader
        # close to the relationships line without inventing a
        # whole new top-level zone.
        safety = getattr(packet, "safety_constraints", None)
        if safety is not None:
            sens = safety.sensitivity_gates or ()
            cross = safety.cross_member_rules or ()
            if sens or cross:
                safety_lines: list[str] = []
                if sens:
                    safety_lines.append(
                        "Sensitivity classifications in scope: "
                        + ", ".join(
                            sorted({
                                str(g.get("sensitivity", ""))
                                for g in sens
                                if g.get("sensitivity")
                            })
                        )
                    )
                if cross:
                    cross_lines = [
                        f"- {r.get('description', '')}"
                        for r in cross
                        if r.get("description")
                    ]
                    if cross_lines:
                        safety_lines.append(
                            "Cross-member rules:\n"
                            + "\n".join(cross_lines)
                        )
                if safety_lines:
                    state_parts.append(
                        "DISCLOSURE / SENSITIVITY:\n"
                        + "\n".join(safety_lines)
                    )
        if state_parts:
            parts.append("## STATE\n" + "\n\n".join(state_parts))

    # ---- C3b: RESULTS ----
    results = getattr(packet, "results", None)
    if results is not None and results.results_prefix:
        parts.append("## RESULTS\n" + results.results_prefix)

    # ---- C3b: ACTIONS ----
    actions = getattr(packet, "actions", None)
    if actions is not None:
        action_parts: list[str] = []
        if actions.capability_prompt:
            action_parts.append(actions.capability_prompt)
        if actions.channel_registry:
            channel_lines = []
            for ch in actions.channel_registry:
                if not isinstance(ch, dict):
                    name = getattr(ch, "name", "")
                    display = getattr(ch, "display_name", "")
                    can_send = getattr(ch, "can_send_outbound", False)
                else:
                    name = ch.get("name", "")
                    display = ch.get("display_name", "")
                    can_send = ch.get("can_send_outbound", False)
                if not name:
                    continue
                outbound = "can send" if can_send else "receive only"
                channel_lines.append(
                    f"- {name}: {display} [{outbound}]"
                )
            if channel_lines:
                action_parts.append(
                    "OUTBOUND CHANNELS (use send_to_channel to deliver to "
                    "a specific channel):\n"
                    + "\n".join(channel_lines)
                )
        if action_parts:
            parts.append("## ACTIONS\n" + "\n\n".join(action_parts))

    # ---- C3b: PROCEDURES ----
    memory = getattr(packet, "memory", None)
    if memory is not None and getattr(memory, "procedures", ""):
        parts.append("## PROCEDURES\n" + memory.procedures)

    # ---- C3b: AVAILABLE CANVASES ----
    if memory is not None and getattr(memory, "canvases_summary", ""):
        parts.append("## AVAILABLE CANVASES\n" + memory.canvases_summary)

    # ---- C4: MEMORY (compaction carry + gardener observations) ----
    if memory is not None:
        memory_parts: list[str] = []
        carry = getattr(memory, "compaction_carry", "")
        if carry:
            memory_parts.append(carry)
        gardener = getattr(memory, "gardener_observations", ()) or ()
        if gardener:
            obs_lines: list[str] = []
            for obs in gardener:
                if isinstance(obs, dict):
                    text = obs.get("rationale_short") or obs.get("text") or ""
                else:
                    text = getattr(obs, "rationale_short", "") or str(obs)
                if text:
                    obs_lines.append(f"- {text}")
            if obs_lines:
                memory_parts.append(
                    "GARDENER OBSERVATIONS:\n" + "\n".join(obs_lines)
                )
        if memory_parts:
            parts.append("## MEMORY\n" + "\n\n".join(memory_parts))

    return "\n\n".join(parts)
    # Defensive default — every ActionKind is handled.
    return _SYSTEM_PROMPT_RESPOND_ONLY


# ---------------------------------------------------------------------------
# User-message construction — branches on kind / partial_state
# ---------------------------------------------------------------------------


def _user_message_respond_only(briefing: Briefing) -> str:
    return (
        f"## Directive\n{briefing.presence_directive}\n\n"
        f"Generate the response."
    )


def _user_message_defer(briefing: Briefing) -> str:
    decided = briefing.decided_action
    if not isinstance(decided, Defer):
        return _user_message_respond_only(briefing)
    return (
        f"## Directive\n{briefing.presence_directive}\n\n"
        f"## Defer reason\n{decided.reason}\n\n"
        f"## Follow-up signal\n{decided.follow_up_signal}\n\n"
        f"Generate the deferral message."
    )


def _user_message_constrained(briefing: Briefing) -> str:
    decided = briefing.decided_action
    if not isinstance(decided, ConstrainedResponse):
        return _user_message_respond_only(briefing)
    return (
        f"## Directive\n{briefing.presence_directive}\n\n"
        f"## Constraint\n{decided.constraint}\n\n"
        f"## Partial satisfaction\n{decided.satisfaction_partial}\n\n"
        f"Generate the constrained response."
    )


def _user_message_pivot(briefing: Briefing) -> str:
    decided = briefing.decided_action
    if not isinstance(decided, Pivot):
        return _user_message_respond_only(briefing)
    return (
        f"## Directive\n{briefing.presence_directive}\n\n"
        f"## Pivot reason\n{decided.reason}\n\n"
        f"## Suggested shape\n{decided.suggested_shape}\n\n"
        f"Generate the pivot response."
    )


def _user_message_propose_tool(briefing: Briefing) -> str:
    decided = briefing.decided_action
    if not isinstance(decided, ProposeTool):
        return _user_message_respond_only(briefing)
    # INTEGRATION-CAPABILITY-FIRST-V1 Batch 2 Fold 2a: surface the
    # integration phase's effect classification so the model has the
    # deterministic context the kind prompt asks it to respect (read
    # → call inline, destructive → propose-then-confirm). Effect
    # comes from the integration LLM's classification + the gate's
    # tool-effects map; the renderer threads it through verbatim.
    # Empty effect (legacy / pre-Fold-2a callers) renders as
    # "unknown — treat as soft_write conservatively" so the prompt
    # still has an unambiguous signal even when the field isn't set.
    effect = decided.effect or "unknown"
    if effect == "unknown":
        effect_line = (
            "## Tool effect (deterministic substrate)\n"
            "unknown — treat as soft_write conservatively (propose first)"
        )
    elif effect == "read":
        effect_line = (
            "## Tool effect (deterministic substrate)\n"
            "read — non-destructive; safe to call inline rather than propose"
        )
    elif effect in ("soft_write", "hard_write"):
        effect_line = (
            f"## Tool effect (deterministic substrate)\n"
            f"{effect} — destructive or affects others; propose first"
        )
    else:
        effect_line = (
            f"## Tool effect (deterministic substrate)\n"
            f"{effect} — treat as soft_write conservatively (propose first)"
        )
    return (
        f"## Directive\n{briefing.presence_directive}\n\n"
        f"## Proposed tool\n{decided.tool_id}\n\n"
        f"{effect_line}\n\n"
        f"## Reason for proposal\n{decided.reason}\n\n"
        f"Render the proposal awaiting user confirmation if effect "
        f"warrants it; call inline if effect is read."
    )


def _user_message_clarification_first_pass(briefing: Briefing) -> str:
    decided = briefing.decided_action
    if not isinstance(decided, ClarificationNeeded):
        return _user_message_respond_only(briefing)
    return (
        f"## Directive\n{briefing.presence_directive}\n\n"
        f"## Question\n{decided.question}\n\n"
        f"## Ambiguity type\n{decided.ambiguity_type}\n\n"
        f"Render the question naturally to the user."
    )


def _user_message_b2(briefing: Briefing, safe: B2RenderInputs) -> str:
    """Build user message from B2-safe input type ONLY.

    Discovered_information is structurally absent from B2RenderInputs;
    the message construction here cannot access it because the input
    type doesn't have the field.
    """
    parts = []
    parts.append(f"## Directive\n{briefing.presence_directive}")
    parts.append(f"\n## Question\n{safe.question}")
    if safe.blocking_ambiguity:
        parts.append(f"\n## Blocking ambiguity\n{safe.blocking_ambiguity}")
    if safe.safe_question_context:
        parts.append(f"\n## Safe context\n{safe.safe_question_context}")
    parts.append(
        "\nRender the question naturally and acknowledge partial work."
    )
    return "\n".join(parts)


def _user_message_b1(briefing: Briefing, safe: B1RenderInputs) -> str:
    """Build user message from B1-safe input type ONLY.

    Same structural-redaction principle as B2: discovered_information
    is unreachable.
    """
    parts = []
    parts.append(f"## Directive\n{briefing.presence_directive}")
    parts.append(f"\n## Intended outcome (summary)\n{safe.intended_outcome_summary}")
    if safe.attempted_action_summary:
        parts.append(f"\n## Attempted action\n{safe.attempted_action_summary}")
    parts.append(
        "\nRender brief acknowledgment of partial work plus the new framing."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Response parsing — extract text from model output
# ---------------------------------------------------------------------------


def _extract_text_from_response(response: ProviderResponse) -> str:
    """Concatenate text blocks from the response into final user text."""
    parts: list[str] = []
    for block in response.content:
        if block.type == "text" and block.text:
            parts.append(block.text)
    return "".join(parts)


# ---------------------------------------------------------------------------
# PresenceRenderer
# ---------------------------------------------------------------------------


DEFAULT_PRESENCE_MAX_TOKENS = 2048


class PresenceRenderer:
    """Concrete PresenceRenderer conforming to PDI's
    PresenceRendererLike Protocol.

    Single renderer with kind-aware prompting:
      - render(briefing) → PresenceRenderResult: branches on
        briefing.decided_action.kind to pick the right system prompt
        and user-message shape.
      - For B1 / B2 paths (not exposed via render() directly — they
        use render_b1/render_b2 which take SAFE input types), the
        unsafe `discovered_information` is structurally unreachable.

    EnactmentService's B2 path constructs B2RenderInputs from the
    ClarificationPartialState (dropping discovered_information on
    the floor by construction) and calls render_b2(briefing, safe).
    Similarly for B1 via render_b1.

    For thin-path kinds (respond_only, defer, etc.), render(briefing)
    is the entry point. The full machinery happy path also calls
    render(briefing) for terminal text after all steps complete.
    """

    def __init__(
        self,
        *,
        chain_caller: ChainCaller,
        max_tokens: int = DEFAULT_PRESENCE_MAX_TOKENS,
        tool_dispatcher: "ToolDispatcher | None" = None,
        max_tool_iterations: int = 5,
    ) -> None:
        """
        INTEGRATION-CAPABILITY-FIRST-V1 (Batch 1, piece C):
        ``tool_dispatcher`` is the bounded tool-use-loop hook.
        When None (default), ``_render`` calls ``chain_caller`` once
        and extracts text — same behavior as pre-spec. When provided,
        ``_render`` runs a bounded loop: tool_use blocks in the
        response trigger dispatch, results append to messages,
        chain_caller fires again. Loop terminates on text-only
        response OR ``max_tool_iterations`` (whichever first).
        Telemetry increments once per actual dispatch (per spec
        acceptance criterion 6f).
        """
        self._chain_caller = chain_caller
        self._max_tokens = max_tokens
        self._tool_dispatcher = tool_dispatcher
        self._max_tool_iterations = max_tool_iterations

    async def render(self, briefing: Briefing) -> PresenceRenderResult:
        """Kind-aware render. Branches structurally on decided_action.kind.

        For ClarificationNeeded with populated partial_state (B2-routed),
        callers should use render_b2() with explicitly-constructed
        B2RenderInputs to engage structural redaction. The render()
        path for ClarificationNeeded handles first-pass (partial_state
        is None); B2-routed clarifications via render() do NOT receive
        the partial state in the prompt (defensive).

        COGNITIVE-CONTEXT-V1 C3a: when ``briefing.cognitive_context`` is
        populated, the renderer prepends the C3a substrate slice
        (RULES + NOW + STATE) to the kind-aware prompt so the model
        receives the canonical cognitive substrate. C3a deliberately
        renders only those three zones; RESULTS / ACTIONS / MEMORY
        / SAFETY land at C3b. Pre-C3a callers (legacy stub Briefings,
        fail-soft path) pass cognitive_context=None and the renderer
        falls back to the original kind-aware prompt unchanged.
        """
        kind = briefing.decided_action.kind
        kind_prompt = _system_prompt_for_kind(kind)
        packet = getattr(briefing, "cognitive_context", None)
        substrate = _render_substrate(packet)
        # C3c: combine substrate + kind prompt + additive directive
        # in the FINAL system render. The directive is turn-local
        # framing posture; substrate is the cognitive primitive. Both
        # reach the model via system; the user-message body still
        # carries the directive too for backward compat with the
        # kind_prompt's "consistent with briefing's directive"
        # phrasing — additive in BOTH places, never silently dropped.
        directive_text = (briefing.presence_directive or "").strip()
        system_parts: list[str] = []
        if substrate:
            system_parts.append(substrate)
        system_parts.append(kind_prompt)
        if directive_text:
            system_parts.append(f"## Directive\n{directive_text}")
        system = "\n\n".join(system_parts)
        user_message = _user_message_for_briefing(briefing)
        # INTEGRATION-RENDERER-RESULT-FORWARD-V1 (2026-05-07): if the
        # integration runner already dispatched read tools to fetch
        # content for this turn, prepend those results so the model
        # already has the content and doesn't re-dispatch the same
        # reads. Eliminates the redundant read_file the renderer
        # otherwise issues to fetch content the integration model
        # already fetched (observed kernos-main 2026-05-07: integration
        # read kernos-architecture-audit.md, then renderer re-read it
        # 6s later through the same seam).
        #
        # ACTION-RESULT-FORWARDING-V1 (2026-05-16): full-machinery
        # action-tier dispatches (StepDispatcher results — execute_code
        # stdout, etc.) come in via tool_results_during_enactment.
        # Both phases surface here with explicit "Prep" / "Enactment"
        # labels so the model can distinguish source: prep results
        # informed the action decision; enactment results are the
        # tool's actual output from carrying that action out. Without
        # this forwarding, the agent honestly reports "no receipt in
        # render context" because the dispatcher's output never reaches
        # the next turn's input_items.
        forwarded = _format_forwarded_tool_results(
            getattr(briefing.audit_trace, "tool_results_during_prep", ()),
            getattr(briefing.audit_trace, "tool_results_during_enactment", ()),
        )
        if forwarded:
            user_message = f"{forwarded}\n\n{user_message}"
        # C5: thin-path tool surface. PresenceRenderer's chain_caller
        # used to receive an empty tools list — the load-bearing bug
        # CCV1 C5 closes. The packet's tool_surface.all_tools()
        # returns ALWAYS_PINNED + active_zone + request_tool (deduped),
        # which the renderer now passes to the chain. Pre-C3a callers
        # with packet=None see no tools (unchanged behavior).
        #
        # Codex C5-review CONCERN fold: log the failure path loudly
        # rather than silently swallowing. The C5 fix is "chain_caller
        # receives a populated tools list, no longer empty"; a silent
        # except would re-create the very empty-tools failure mode
        # this commit closes. Log + fall back to empty so the
        # operator sees the gap surface.
        tool_list: tuple[dict, ...] = ()
        if packet is not None:
            tool_surface = getattr(packet, "tool_surface", None)
            if tool_surface is not None:
                try:
                    tool_list = tool_surface.all_tools()
                except Exception:
                    logger.warning(
                        "PRESENCE_RENDER_TOOL_SURFACE_READ_FAILED — "
                        "falling back to empty tools list. This "
                        "recreates the pre-C5 empty-tools failure "
                        "mode the renderer fix was meant to close; "
                        "investigate the packet's tool_surface state.",
                        exc_info=True,
                    )
                    tool_list = ()
        return await self._render(
            system, user_message, tools=tool_list,
            conversation_id=briefing.turn_id,
        )

    async def render_b2(
        self, briefing: Briefing, safe: B2RenderInputs
    ) -> PresenceRenderResult:
        """B2-routed clarification render. Caller constructs
        B2RenderInputs from the ClarificationPartialState — the
        `discovered_information` field is dropped on the floor at
        that construction step."""
        system = _SYSTEM_PROMPT_CLARIFICATION_B2
        user_message = _user_message_b2(briefing, safe)
        return await self._render(
            system, user_message, conversation_id=briefing.turn_id,
        )

    async def render_b1(
        self, briefing: Briefing, safe: B1RenderInputs
    ) -> PresenceRenderResult:
        """B1 termination render. Same structural-redaction principle
        as B2."""
        system = _SYSTEM_PROMPT_B1_TERMINATION
        user_message = _user_message_b1(briefing, safe)
        return await self._render(
            system, user_message, conversation_id=briefing.turn_id,
        )

    async def _render(
        self,
        system: str,
        user_message: str,
        *,
        tools: list[dict] | tuple[dict, ...] = (),
        conversation_id: str = "",
    ) -> PresenceRenderResult:
        """Internal helper: invoke the chain and extract text.

        Streaming flag: defaults to False here. Production wiring
        (IWL C5) wraps this call in an adapter that streams to the
        user-facing surface and sets the flag accordingly.

        COGNITIVE-CONTEXT-V1 C5: ``tools`` is now a deliberate
        argument (defaults to empty for backward compat with the
        B1 / B2 paths that don't surface tools). Thin-path render()
        passes the tool surface from ``briefing.cognitive_context``;
        legacy / pre-C5 callers see empty tools, unchanged.

        ``conversation_id`` is the briefing's turn_id (which IS the
        upstream conversation_id from ReasoningRequest, renamed at
        the TurnRunnerInputs boundary). The chain caller forwards
        it to the underlying provider; for the Codex provider this
        unlocks the wire-shape repair fields (prompt_cache_key +
        session correlation headers) that fix recurrent mid-stream
        ``server_error`` on payloads above ~50KB. See e50fb32 for
        the original wire-shape fix that this thin-path plumbing
        restores.

        WIRE-SHAPE PLUMBING SEAM — do NOT drop conversation_id from
        the chain_caller invocation below. It flows through
        response_delivery._wrapped → _shared_chain_caller →
        provider.complete to populate the Codex provider's
        prompt_cache_key + session correlation headers. See
        kernos/providers/codex_provider.py class docstring
        "WIRE SHAPE INVARIANTS" for the full contract. Pin tests:
        tests/test_thin_path_codex_wire_shape_plumbing.py.
        """
        messages: list[dict] = [{"role": "user", "content": user_message}]
        # conversation_id is forwarded only when non-empty to keep the
        # chain_caller protocol compatible with stubs that don't accept
        # it. The production C7 thin path passes briefing.turn_id (the
        # upstream conversation_id), which is non-empty for real turns.
        chain_kwargs: dict[str, Any] = {}
        if conversation_id:
            chain_kwargs["conversation_id"] = conversation_id

        # INTEGRATION-CAPABILITY-FIRST-V1 (Batch 1, piece C): bounded
        # tool-use loop. Pre-spec, this method called chain_caller
        # once and extracted text; tool_use blocks in the response
        # were silently dropped, breaking capability-first posture.
        # Loop semantics:
        #   - Each iteration: chain_caller produces a response.
        #   - If response carries tool_use blocks AND a dispatcher
        #     is wired, dispatch each, append assistant + tool_result
        #     messages, loop.
        #   - Terminate on text-only response OR max iterations.
        #   - On max iterations, surface a friendly text rather than
        #     silent drop (per spec acceptance criterion 6e).
        # When no dispatcher is wired, the legacy single-call shape
        # is preserved (loop body executes once, returns first text).
        for _iteration in range(max(1, self._max_tool_iterations)):
            response = await self._chain_caller(
                system, messages, list(tools), self._max_tokens,
                **chain_kwargs,
            )
            tool_use_blocks = [
                b for b in getattr(response, "content", []) or []
                if getattr(b, "type", None) == "tool_use"
            ]
            if not tool_use_blocks or self._tool_dispatcher is None:
                text = _extract_text_from_response(response)
                return PresenceRenderResult(text=text, streamed=False)

            # Append the assistant turn (with the tool_use blocks) so
            # the next chain call sees what was just decided. Use
            # provider-API shape (role + content list of dicts).
            assistant_blocks: list[dict] = []
            for b in getattr(response, "content", []) or []:
                btype = getattr(b, "type", None)
                if btype == "text":
                    assistant_blocks.append({
                        "type": "text",
                        "text": getattr(b, "text", "") or "",
                    })
                elif btype == "tool_use":
                    assistant_blocks.append({
                        "type": "tool_use",
                        "id": getattr(b, "id", "") or "",
                        "name": getattr(b, "name", "") or "",
                        "input": getattr(b, "input", {}) or {},
                    })
            messages.append({"role": "assistant", "content": assistant_blocks})

            # Dispatch each tool_use block, append tool_result blocks
            # in a single user message (Anthropic-style multi-result
            # turn). tool_use_id is preserved per dispatch so the
            # provider can correlate (spec acceptance criterion 6b).
            tool_result_blocks: list[dict] = []
            for tu in tool_use_blocks:
                tu_id = getattr(tu, "id", "") or ""
                tu_name = getattr(tu, "name", "") or ""
                tu_input = getattr(tu, "input", {}) or {}
                try:
                    result_content = await self._tool_dispatcher(
                        tool_name=tu_name,
                        tool_input=tu_input,
                        tool_use_id=tu_id,
                        conversation_id=conversation_id,
                    )
                except Exception as exc:
                    # Friendly tool-failure result rather than tearing
                    # down the loop — gives the model a chance to
                    # recover or surface the error to the user.
                    logger.warning(
                        "PRESENCE_TOOL_DISPATCH_FAILED: tool=%s err=%s",
                        tu_name, exc,
                    )
                    result_content = (
                        f"[tool error] {tu_name} failed: {exc}. "
                        f"You may try a different approach or report "
                        f"the limitation to the user."
                    )
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": result_content if isinstance(result_content, str)
                               else str(result_content),
                })
            messages.append({"role": "user", "content": tool_result_blocks})

        # Max iterations reached without text-only termination.
        # Friendly failure (spec acceptance criterion 6e).
        logger.warning(
            "PRESENCE_TOOL_LOOP_MAX_ITERATIONS: cap=%d — surfacing "
            "iteration-cap message rather than silent drop",
            self._max_tool_iterations,
        )
        return PresenceRenderResult(
            text=(
                "I worked through several tool calls but didn't reach a "
                "final response within the iteration limit. Let me know "
                "what to try next or which step to focus on."
            ),
            streamed=False,
        )


_FORWARDED_RESULT_PER_ENTRY_CHAR_CAP = 8000
_FORWARDED_RESULT_TOTAL_CHAR_CAP = 24000


def _format_forwarded_tool_results(
    prep_results: tuple[dict[str, str], ...] | list[dict[str, str]],
    enactment_results: tuple[dict[str, str], ...] | list[dict[str, str]] = (),
) -> str:
    """Render forwarded tool results into a block the renderer's chain
    prompt prepends to the user message.

    Two phases surface, each under an explicit "(prep)" or
    "(enactment)" label:

      * **prep** — per-call tool results from the integration runner's
        synthesis phase (read tools dispatched before the action
        decision). The model treats these as authoritative content
        already fetched.
      * **enactment** — per-step results from StepDispatcher during the
        full-machinery execution loop (e.g. execute_code stdout). Lets
        the model see what the action it decided on actually returned,
        rather than reporting "no receipt" because the dispatcher's
        output never reached the next turn's input_items.

    Empty inputs (both phases) → ``""``. Capped per-entry
    (``_FORWARDED_RESULT_PER_ENTRY_CHAR_CAP``) and cumulatively
    (``_FORWARDED_RESULT_TOTAL_CHAR_CAP``) across BOTH phases combined,
    so a large prep result can't starve enactment results (or vice
    versa) but the renderer prompt still doesn't balloon. Truncated
    entries carry an explicit marker so the model knows the result
    was clipped and can issue a fresh fetch if it truly needs the
    rest.
    """
    if not prep_results and not enactment_results:
        return ""
    sections: list[str] = []
    remaining = _FORWARDED_RESULT_TOTAL_CHAR_CAP

    def _emit_phase(
        heading: str,
        phase_label: str,
        entries: tuple[dict[str, str], ...] | list[dict[str, str]],
    ) -> None:
        nonlocal remaining
        if not entries:
            return
        sections.append(heading)
        for entry in entries:
            name = entry.get("tool_name", "")
            result = entry.get("result", "")
            if not name and not result:
                continue
            clipped = result
            truncated_marker = ""
            if len(clipped) > _FORWARDED_RESULT_PER_ENTRY_CHAR_CAP:
                clipped = clipped[:_FORWARDED_RESULT_PER_ENTRY_CHAR_CAP]
                truncated_marker = (
                    f"\n[TRUNCATED: {len(result) - len(clipped)} more "
                    f"chars; re-call the tool if you need the full "
                    f"content]"
                )
            if remaining <= 0:
                sections.append(
                    f"#### {name} ({phase_label})\n[OMITTED: total "
                    f"forward budget ({_FORWARDED_RESULT_TOTAL_CHAR_CAP} "
                    f"chars) exhausted by earlier results; re-call the "
                    f"tool if needed]"
                )
                continue
            if len(clipped) > remaining:
                clipped = clipped[:remaining]
                truncated_marker = (
                    f"\n[TRUNCATED: total forward budget "
                    f"({_FORWARDED_RESULT_TOTAL_CHAR_CAP} chars) reached]"
                )
            remaining -= len(clipped)
            sections.append(
                f"#### {name} ({phase_label})\n{clipped}{truncated_marker}"
            )

    _emit_phase(
        "### Prior tool results (already fetched this turn)",
        "prep",
        prep_results,
    )
    _emit_phase(
        "### Action results (dispatched this turn)",
        "enactment",
        enactment_results,
    )
    return "\n\n".join(sections)


def _user_message_for_briefing(briefing: Briefing) -> str:
    """Pick the user-message builder based on decided_action.kind."""
    kind = briefing.decided_action.kind
    if kind is ActionKind.RESPOND_ONLY:
        return _user_message_respond_only(briefing)
    if kind is ActionKind.DEFER:
        return _user_message_defer(briefing)
    if kind is ActionKind.CONSTRAINED_RESPONSE:
        return _user_message_constrained(briefing)
    if kind is ActionKind.PIVOT:
        return _user_message_pivot(briefing)
    if kind is ActionKind.PROPOSE_TOOL:
        return _user_message_propose_tool(briefing)
    if kind is ActionKind.CLARIFICATION_NEEDED:
        return _user_message_clarification_first_pass(briefing)
    # ActionKind.EXECUTE_TOOL: full machinery terminal. The directive
    # framing is sufficient since tool detail lives in audit.
    return _user_message_respond_only(briefing)


def build_presence_renderer(
    *,
    chain_caller: ChainCaller,
    max_tokens: int = DEFAULT_PRESENCE_MAX_TOKENS,
    tool_dispatcher: ToolDispatcher | None = None,
    max_tool_iterations: int = 5,
) -> PresenceRenderer:
    return PresenceRenderer(
        chain_caller=chain_caller,
        max_tokens=max_tokens,
        tool_dispatcher=tool_dispatcher,
        max_tool_iterations=max_tool_iterations,
    )


__all__ = [
    "B1RenderInputs",
    "B2RenderInputs",
    "ChainCaller",
    "DEFAULT_PRESENCE_MAX_TOKENS",
    "PresenceRenderer",
    "ToolDispatcher",
    "build_presence_renderer",
]
