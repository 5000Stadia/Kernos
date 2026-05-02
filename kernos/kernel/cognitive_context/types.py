"""Typed primitive carrying cognitive substrate from assembly to model.

COGNITIVE-CONTEXT-V1 C1. The packet is a frozen dataclass with nine
deterministic fields; each field has a documented content source
(see :mod:`field_provenance`). Tuple-typed sequences keep the
structure immutable; the integration layer extends specific fields
via :meth:`CognitiveContext.with_updates` (which returns a new
packet).

The packet holds **structured content**, not rendered system-prompt
text. Rendering to text happens at the presence-renderer boundary
(C3c) so that:

* The integration layer can transform structured fields without
  touching string formatting (e.g., extending ``memory`` with cohort
  outputs preserves the field's typing).
* Tests can assert on structured content directly rather than
  parsing rendered text.
* The same packet can be rendered differently for different model
  contexts (legacy single-string vs. cache-aware static/dynamic
  split) without each renderer having to recompose substrate.

Field provenance — the deterministic source per field — lives in
:mod:`field_provenance`. The mapping there is authoritative and
testable: each documented source resolves to a real symbol that
produces content of the expected type.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.soul import Soul
    from kernos.kernel.state import CovenantRule, KnowledgeEntry


# ---------------------------------------------------------------------------
# Sub-block types — one per legacy assembly grammar zone, plus tool surface
# and safety constraints.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RulesBlock:
    """RULES zone: operating principles, identity formation, behavioral
    contracts, instance purpose. Maps to legacy
    :func:`_build_rules_block` content.

    Fields:

    * ``operating_principles`` — universal substrate principles
      (transparency, intent-over-instruction, stewardship, etc.).
      Source: ``PRIMARY_TEMPLATE.operating_principles``.
    * ``bootstrap_prompt`` — first-conversation guidance shipped
      while ``bootstrap_graduated == False``. None when graduated.
      Source: ``PRIMARY_TEMPLATE.bootstrap_prompt`` gated by
      ``member_profile.bootstrap_graduated``.
    * ``hatching_prompt`` — UNIQUE or INHERIT hatching prompt.
      Stored as the **raw template** at C1; rendering with
      ``display_name`` / ``agent_name`` / ``name_instruction``
      substitution happens at C3c via NowBlock + StateBlock fields
      that the renderer has access to (avoids storing rendered
      text on the packet, preserving substitution flexibility).
      Decided structurally: UNIQUE when ``member_profile.agent_name``
      is empty; INHERIT when set. None when graduated.
    * ``covenants`` — active covenant rules visible in the current
      member + space scope. Includes both pinned (always-on) and
      situational (MessageAnalyzer-selected per turn) covenants.
      Codex C1 design pass note: production selection is
      pinned + situational (assemble.py:404-417), not "all active".
      C3a wiring honors that selection.
    * ``space_names`` — mapping of space_id → space name for
      formatting space-scoped covenants. Sourced at the assembly
      seam from spaces the member has access to.
    * ``instance_stewardship`` — per-instance purpose statement set
      via ``manage_stewardship``. Empty string when unset.
    """

    operating_principles: str
    bootstrap_prompt: str | None
    hatching_prompt: str | None
    covenants: tuple["CovenantRule", ...]
    space_names: dict[str, str]
    instance_stewardship: str


@dataclass(frozen=True)
class NowBlock:
    """NOW zone: turn-local operating situation. Time, platform, auth,
    space, member.

    Holds the raw fields rather than the rendered string so the
    renderer at the presence boundary can format consistently with
    legacy output (and so tests can assert on individual fields).
    """

    timestamp_utc: datetime
    user_timezone: str
    platform: str
    auth_level: str
    instance_id: str
    member_id: str
    member_display_name: str
    active_space_id: str
    active_space_name: str
    agent_name: str
    execution_envelope: dict[str, Any] | None = None


@dataclass(frozen=True)
class StateBlock:
    """STATE zone: current truth the agent should act from. Soul
    (deprecated for identity but kept for compat),
    member profile, relationships, knowledge entries selected for the
    turn.

    ``member_profile`` stays dict-typed at C1 because the legacy
    surface is a dict and a typed dataclass would require touching
    instance_db's read shape — out of scope for the typing commit.
    """

    soul: "Soul | None"
    member_profile: dict[str, Any]
    relationships: tuple[dict[str, Any], ...]
    knowledge_entries: tuple["KnowledgeEntry", ...]


@dataclass(frozen=True)
class ResultsBlock:
    """RESULTS zone: prior-turn results carry-over.

    Stored as the rendered prefix because that's what
    :func:`_build_results_block` produces today; future C-arc may
    decompose into structured records.
    """

    results_prefix: str


@dataclass(frozen=True)
class ActionsBlock:
    """ACTIONS zone: what the agent can do this turn — capability
    prompt + outbound channel surface.

    ``channel_registry`` is a tuple of channel-info dicts (legacy
    shape). ``capability_prompt`` is the rendered string the legacy
    block-builder produces.
    """

    capability_prompt: str
    channel_registry: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class MemoryBlock:
    """MEMORY zone: compaction Living State + retrieved knowledge +
    awareness whispers + gardener observations + procedures + canvases.

    Field provenance:

    * ``compaction_carry`` — output of compaction's Living State at
      the active space's last compact boundary.
    * ``awareness_whispers`` — pending whispers queued for delivery
      from the awareness loop. Legacy renders these to the RESULTS
      zone (handler.py:2768-2773, 3090-3188); the renderer at C3c
      preserves that placement when emitting from the packet.
    * ``gardener_observations`` — gardener-cohort output. Empty tuple
      until C4 wires gardener_cohort into production fan-out.
    * ``procedures`` — _procedures.md content active for the
      member/space scope. Codex review: was missing from initial
      C1 packet shape.
    * ``canvases_summary`` — pinned canvases summary (rendered
      string from the legacy ``_build_canvases_block``).
    """

    compaction_carry: str
    awareness_whispers: tuple[dict[str, Any], ...]
    gardener_observations: tuple[dict[str, Any], ...]
    procedures: str = ""
    canvases_summary: str = ""


@dataclass(frozen=True)
class ConversationBlock:
    """CONVERSATION zone: the messages array carrying history into
    the model.

    Held as a tuple of message dicts (legacy provider shape: role +
    content + optional tool fields). The conversation already reaches
    the decoupled path via ``TurnRunnerInputs.from_api_messages``;
    storing it on the packet centralizes the contract so renderers
    don't need a separate seam.
    """

    messages: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ToolSurface:
    """The tool surface the model invocation receives.

    Splits ``ALWAYS_PINNED`` (always available, never evicted) from
    the surfacer-driven ``active_zone`` (token-budgeted, LRU
    eviction). ``request_tool`` is split out for clarity even though
    C5 will move it into ``ALWAYS_PINNED`` — the field stays so the
    contract test can target the meta-tool directly.
    """

    always_pinned: tuple[dict[str, Any], ...]
    active_zone: tuple[dict[str, Any], ...]
    request_tool: dict[str, Any] | None

    def all_tools(self) -> tuple[dict[str, Any], ...]:
        """Convenience: the full tool list to pass into the
        provider's ``tools=`` argument."""
        out: list[dict[str, Any]] = list(self.always_pinned)
        out.extend(self.active_zone)
        if self.request_tool is not None:
            # Avoid duplicate if request_tool already in always_pinned
            # (will land that way in C5).
            already = any(
                t.get("name") == self.request_tool.get("name")
                for t in out
            )
            if not already:
                out.append(self.request_tool)
        return tuple(out)


@dataclass(frozen=True)
class SafetyConstraints:
    """Safety + disclosure rules the model must honor on this turn.

    Holds **policy/rule data**, not callable filters. The actual
    cross-member filtering is performed at content-population time
    (per-knowledge-entry sensitivity classification) before the
    packet is constructed; this block carries the residual rules
    the model needs to apply during reasoning.

    * ``sensitivity_gates`` — tuple of sensitivity rule records
      relevant to the active member/space scope. Each record
      describes a class of content the model must not surface
      cross-member.
    * ``disclosure_layer`` — viewer-aware permission profile dict
      for the active member: ``{target_member_id → permission}``.
    * ``cross_member_rules`` — tuple of rule records describing
      cross-member content visibility. Behavioral filter is a
      separate function; rules here are data the model can reason
      about.
    """

    sensitivity_gates: tuple[dict[str, Any], ...]
    disclosure_layer: dict[str, str]
    cross_member_rules: tuple[dict[str, Any], ...]


# ---------------------------------------------------------------------------
# The packet
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CognitiveContext:
    """Canonical cognitive-substrate primitive.

    Carries the legacy seven-block grammar plus tool surface + safety
    constraints between assembly, integration, and presence. Each
    field is deterministically populated from a documented source
    (see :mod:`field_provenance`).

    The packet is **frozen**: integration's incremental enrichment
    (e.g., adding cohort outputs to ``memory``) goes through
    :meth:`with_updates` which returns a new packet. This preserves
    the integration layer's freedom to transform substrate while
    keeping the type immutable.

    The field order here matches the legacy assembly grammar order
    (RULES → NOW → STATE → RESULTS → ACTIONS → MEMORY → CONVERSATION)
    plus tool_surface + safety_constraints (per the spec's typed
    canonical packet).
    """

    rules: RulesBlock
    now: NowBlock
    state: StateBlock
    results: ResultsBlock
    actions: ActionsBlock
    memory: MemoryBlock
    conversation: ConversationBlock
    tool_surface: ToolSurface
    safety_constraints: SafetyConstraints

    def with_updates(self, **kwargs: Any) -> "CognitiveContext":
        """Return a new packet with the given top-level fields
        replaced. Use this from the integration layer to extend
        specific fields (e.g., ``packet.with_updates(memory=
        memory.replace(gardener_observations=cohort_output))``).

        Each block is itself frozen; updating a sub-field requires
        ``dataclasses.replace`` on the block first, then
        ``with_updates`` on the packet.
        """
        return replace(self, **kwargs)


__all__ = [
    "ActionsBlock",
    "CognitiveContext",
    "ConversationBlock",
    "MemoryBlock",
    "NowBlock",
    "ResultsBlock",
    "RulesBlock",
    "SafetyConstraints",
    "StateBlock",
    "ToolSurface",
]
