"""Concrete PresenceRenderer implementing PDI's PresenceRendererLike (IWL C4).

Single renderer with kind-aware prompting. Branches internally on
`briefing.decided_action.kind` and produces user-facing text.

Method signature (per Kit edit, locked):

    await render(briefing) -> PresenceRenderResult

NOT AsyncIterator. Streaming happens internally through the
adapter/event sink; the `result.streamed` flag signals whether
streaming occurred during render. The renderer returns the final
accumulated text in `result.text` regardless.

B1 / B2 STRUCTURAL SAFETY (Kit edit, load-bearing):

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

Same-model default (Kit edit, locked): the renderer's chain_caller
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


# ---------------------------------------------------------------------------
# B1 / B2 safe render inputs — structural redaction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class B2RenderInputs:
    """Safe input type for B2-routed clarification rendering.

    Per Kit edit (load-bearing): the renderer's B2 path consumes
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

    Per Kit edit (load-bearing): the renderer's B1 path consumes
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


_SYSTEM_PROMPT_RESPOND_ONLY = """\
You are Kernos's presence renderer. Generate a conversational reply that
answers the user. Keep tone consistent with the briefing's directive.
Brief and direct unless the directive asks for depth. Output the reply as
plain text. No tool calls.
"""

_SYSTEM_PROMPT_DEFER = """\
You are Kernos's presence renderer. The decided action is to defer.
Acknowledge the user briefly, signal that this turn is being deferred,
and indicate the follow-up shape from the briefing. Plain text response.
"""

_SYSTEM_PROMPT_CONSTRAINED_RESPONSE = """\
You are Kernos's presence renderer. The decided action is constrained
response: respond partially under the named constraint. Surface what
can be said within the constraint; acknowledge what cannot. Plain text.
"""

_SYSTEM_PROMPT_PIVOT = """\
You are Kernos's presence renderer. The decided action is pivot:
generate a different shape of response than the literal request asked
for. Use the briefing's reason and suggested_shape. Plain text.
"""

_SYSTEM_PROMPT_PROPOSE_TOOL = """\
You are Kernos's presence renderer. The decided action is propose_tool:
render a proposal text awaiting user confirmation. Do NOT execute the
tool — the proposal is the output. Plain text.
"""

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
directive and the action context. Plain text. Streaming permitted —
the loop has terminated.
"""


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
    return (
        f"## Directive\n{briefing.presence_directive}\n\n"
        f"## Proposed tool\n{decided.tool_id}\n\n"
        f"## Reason for proposal\n{decided.reason}\n\n"
        f"Render the proposal awaiting user confirmation."
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
    ) -> None:
        self._chain_caller = chain_caller
        self._max_tokens = max_tokens

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
        substrate = _render_substrate(
            getattr(briefing, "cognitive_context", None)
        )
        system = (
            f"{substrate}\n\n{kind_prompt}" if substrate else kind_prompt
        )
        user_message = _user_message_for_briefing(briefing)
        return await self._render(system, user_message)

    async def render_b2(
        self, briefing: Briefing, safe: B2RenderInputs
    ) -> PresenceRenderResult:
        """B2-routed clarification render. Caller constructs
        B2RenderInputs from the ClarificationPartialState — the
        `discovered_information` field is dropped on the floor at
        that construction step."""
        system = _SYSTEM_PROMPT_CLARIFICATION_B2
        user_message = _user_message_b2(briefing, safe)
        return await self._render(system, user_message)

    async def render_b1(
        self, briefing: Briefing, safe: B1RenderInputs
    ) -> PresenceRenderResult:
        """B1 termination render. Same structural-redaction principle
        as B2."""
        system = _SYSTEM_PROMPT_B1_TERMINATION
        user_message = _user_message_b1(briefing, safe)
        return await self._render(system, user_message)

    async def _render(
        self, system: str, user_message: str
    ) -> PresenceRenderResult:
        """Internal helper: invoke the chain and extract text.

        Streaming flag: defaults to False here. Production wiring
        (IWL C5) wraps this call in an adapter that streams to the
        user-facing surface and sets the flag accordingly.
        """
        messages: list[dict] = [{"role": "user", "content": user_message}]
        response = await self._chain_caller(
            system, messages, [], self._max_tokens
        )
        text = _extract_text_from_response(response)
        return PresenceRenderResult(text=text, streamed=False)


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
) -> PresenceRenderer:
    return PresenceRenderer(
        chain_caller=chain_caller, max_tokens=max_tokens
    )


__all__ = [
    "B1RenderInputs",
    "B2RenderInputs",
    "ChainCaller",
    "DEFAULT_PRESENCE_MAX_TOKENS",
    "PresenceRenderer",
    "build_presence_renderer",
]
