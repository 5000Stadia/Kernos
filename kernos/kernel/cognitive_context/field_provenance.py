"""Field-provenance map for :class:`CognitiveContext`.

For each packet field, documents the deterministic source that
populates it. The map is the architectural contract per the
COGNITIVE-CONTEXT-V1 spec's field-provenance table.

C1 (this commit) ships the map + a :func:`populate_field` helper
that resolves sources at call time. No consumer reads the packet
yet; C3a-c wires the packet through the production path. C1's
tests prove each documented source is a real, importable symbol
that produces content of the expected type.

The ``populate_field`` function takes a :class:`PopulationContext`
(handle to the running substrate: state store, instance_db, member
context, etc.) and returns the field content. It's the single
seam through which all field population flows; future C-arc work
that adds new sources (e.g., gardener cohort wired in C4) extends
this function rather than fanning the wiring across multiple files.

Why ship the map separately from the dataclass: keeping types in
:mod:`types` pure-data (frozen dataclasses, no behavior) lets
tests + the integration layer reason about the structure without
pulling the substrate's runtime dependencies. The behavior side
lives here, isolated.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.cognitive_context.types import CognitiveContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provenance descriptor — what a field's source looks like
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldProvenance:
    """Documents a packet field's deterministic content source.

    * ``field_path`` — dotted path on the packet
      (e.g. ``"rules.operating_principles"``).
    * ``source_module`` — Python module the source lives in.
    * ``source_symbol`` — symbol name within the module. May be a
      dotted path to walk into a constant struct
      (e.g. ``"PRIMARY_TEMPLATE.operating_principles"``).
    * ``source_kind`` — ``"constant"``, ``"method"``, ``"function"``.
      Used by the symbol-resolution test (constants are imported +
      attribute-walked; methods resolve on a class; functions are
      callable lookups).
    * ``expected_type`` — a string description of the expected type
      (kept as text rather than a runtime type so optional / union
      types are documented without import gymnastics in the map).
    * ``wiring_state`` (the design review-required, three-state):

      - ``"wired"`` — populate_field has explicit routing returning
        real content.
      - ``"deferred"`` — explicitly deferred; ``deferred_until``
        names the phase that will land the wiring. populate_field
        returns the type-appropriate default; tests treat this as
        intentional.
      - ``"unwired_expected"`` — ESCAPE HATCH for entries added
        without explicit classification. populate_field raises
        :class:`NotImplementedError` so the gap surfaces. By end of
        C5 the field-availability test pins zero unwired_expected
        entries.

    * ``deferred_until`` — required when ``wiring_state="deferred"``;
      names the phase (e.g. ``"C3a"``, ``"C4"``, ``"C5"``).
    * ``notes`` — human-readable notes about the source's wiring
      status, defaulting behavior, or known follow-up phases.
    """

    field_path: str
    source_module: str
    source_symbol: str
    source_kind: str  # constant | method | function
    expected_type: str
    wiring_state: str = "unwired_expected"  # wired | deferred | unwired_expected
    deferred_until: str | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        # Validation: deferred entries must declare deferred_until;
        # other states must not.
        if self.wiring_state not in ("wired", "deferred", "unwired_expected"):
            raise ValueError(
                f"FieldProvenance({self.field_path!r}): wiring_state "
                f"must be one of wired / deferred / unwired_expected; "
                f"got {self.wiring_state!r}"
            )
        if self.wiring_state == "deferred" and not self.deferred_until:
            raise ValueError(
                f"FieldProvenance({self.field_path!r}): wiring_state="
                f"'deferred' requires deferred_until to name the phase "
                f"that will land the wiring (e.g. 'C3a', 'C5')"
            )
        if self.wiring_state != "deferred" and self.deferred_until:
            raise ValueError(
                f"FieldProvenance({self.field_path!r}): deferred_until "
                f"is only valid when wiring_state='deferred'"
            )


# ---------------------------------------------------------------------------
# Authoritative provenance map. Each entry pins a deterministic source
# and its kind. Tests at C1 walk this map and assert resolvability.
# ---------------------------------------------------------------------------


FIELD_PROVENANCE: dict[str, FieldProvenance] = {
    # === RULES zone ===
    "rules.operating_principles": FieldProvenance(
        field_path="rules.operating_principles",
        source_module="kernos.kernel.template",
        source_symbol="PRIMARY_TEMPLATE.operating_principles",
        source_kind="constant",
        expected_type="str",
        wiring_state="wired",
        notes="Universal substrate principles — transparency, intent, stewardship.",
    ),
    "rules.bootstrap_prompt": FieldProvenance(
        field_path="rules.bootstrap_prompt",
        source_module="kernos.kernel.template",
        source_symbol="PRIMARY_TEMPLATE.bootstrap_prompt",
        source_kind="constant",
        expected_type="str | None",
        wiring_state="wired",
        notes="Gated by member_profile.bootstrap_graduated. None when graduated.",
    ),
    "rules.hatching_prompt": FieldProvenance(
        field_path="rules.hatching_prompt",
        source_module="kernos.messages.handler",
        source_symbol="_UNIQUE_HATCHING_PROMPT",
        source_kind="constant",
        expected_type="str | None",
        wiring_state="wired",
        notes=(
            "Stored as the RAW template at C1; substitution with "
            "display_name / agent_name / name_instruction happens at "
            "C3c via NowBlock + StateBlock fields available to the "
            "renderer (avoids storing rendered text on the packet, "
            "preserves substitution flexibility). Selection is "
            "structural: _UNIQUE_HATCHING_PROMPT when "
            "member_profile.agent_name is empty; "
            "_INHERIT_HATCHING_PROMPT when set. None when graduated."
        ),
    ),
    "rules.covenants": FieldProvenance(
        field_path="rules.covenants",
        source_module="kernos.kernel.state",
        source_symbol="StateStore.query_covenant_rules",
        source_kind="method",
        expected_type="tuple[CovenantRule, ...]",
        wiring_state="wired",
        notes=(
            "Active covenants in the current member + space scope. "
            "Production selection is pinned + situational "
            "(MessageAnalyzer-selected per turn, not 'all active') — "
            "see assemble.py:404-417. C3a wiring reads the selected "
            "tuple from PopulationContext.covenants (assembly resolves "
            "the selection; populate_field consumes the resolution)."
        ),
    ),
    "rules.space_names": FieldProvenance(
        field_path="rules.space_names",
        source_module="kernos.kernel.spaces",
        source_symbol="ContextSpace",
        source_kind="constant",
        expected_type="dict[str, str]",
        wiring_state="wired",
        notes=(
            "Mapping space_id → space name. Used by _format_contracts to "
            "render space-scoped covenants. C3a wiring reads from "
            "PopulationContext.space_names (assembly resolves from the "
            "spaces a member has access to)."
        ),
    ),
    "rules.instance_stewardship": FieldProvenance(
        field_path="rules.instance_stewardship",
        source_module="kernos.kernel.instance_db",
        source_symbol="InstanceDB.get_instance_stewardship",
        source_kind="method",
        expected_type="str",
        wiring_state="wired",
        notes=(
            "Per-instance purpose statement. Note: spec field-provenance "
            "table identified template.py as source; current production "
            "reads via instance_db (handler.py:747). Map reflects code "
            "reality. Wired in populate_field at C1; returns empty "
            "string when ctx.instance_db is None (test contexts)."
        ),
    ),
    # === NOW zone ===
    "now": FieldProvenance(
        field_path="now",
        source_module="kernos.kernel.cognitive_context.field_provenance",
        source_symbol="_construct_now_block",
        source_kind="function",
        expected_type="NowBlock",
        wiring_state="wired",
        notes=(
            "Constructed from turn provisioning context (member_id, "
            "space_id, platform, message timestamp, auth_level). The "
            "construction helper lives in this module so the source "
            "is self-contained."
        ),
    ),
    # === STATE zone ===
    "state.soul": FieldProvenance(
        field_path="state.soul",
        source_module="kernos.kernel.state",
        source_symbol="StateStore.get_soul",
        source_kind="method",
        expected_type="Soul | None",
        wiring_state="wired",
        notes=(
            "Deprecated for identity (per multi-member migration); kept "
            "for compat. C3a wiring reads from PopulationContext.soul "
            "(assembly resolves via state_store.get_soul)."
        ),
    ),
    "state.member_profile": FieldProvenance(
        field_path="state.member_profile",
        source_module="kernos.kernel.instance_db",
        source_symbol="InstanceDB.get_member_profile",
        source_kind="method",
        expected_type="dict[str, Any]",
        wiring_state="wired",
        notes=(
            "Dict-typed — production read shape. C3a wiring reads from "
            "PopulationContext.member_profile (assembly resolves via "
            "instance_db.get_member_profile in provision phase)."
        ),
    ),
    "state.relationships": FieldProvenance(
        field_path="state.relationships",
        source_module="kernos.kernel.instance_db",
        source_symbol="InstanceDB.list_relationships",
        source_kind="method",
        expected_type="tuple[dict[str, Any], ...]",
        wiring_state="wired",
        notes=(
            "Pairwise relationship records visible to the active "
            "member. C3a wiring reads from PopulationContext.relationships "
            "(assembly resolves via instance_db.list_relationships)."
        ),
    ),
    "state.knowledge_entries": FieldProvenance(
        field_path="state.knowledge_entries",
        source_module="kernos.kernel.retrieval",
        source_symbol="RetrievalService.search",
        source_kind="method",
        expected_type="tuple[KnowledgeEntry, ...]",
        wiring_state="wired",
        notes=(
            "Knowledge selected for the turn via retrieval. C3a wiring "
            "reads from PopulationContext.knowledge_entries (assembly's "
            "MessageAnalyzer + disclosure-gate already filtered the "
            "always-inject + ranked-relevant set)."
        ),
    ),
    # === RESULTS zone ===
    "results.results_prefix": FieldProvenance(
        field_path="results.results_prefix",
        source_module="kernos.messages.handler",
        source_symbol="_build_results_block",
        source_kind="function",
        expected_type="str",
        wiring_state="wired",
        notes=(
            "Rendered prefix from prior-turn results. C3a wiring reads "
            "from PopulationContext.results_prefix (assembly resolves "
            "via _assemble_space_context)."
        ),
    ),
    # === ACTIONS zone ===
    "actions.capability_prompt": FieldProvenance(
        field_path="actions.capability_prompt",
        source_module="kernos.capability.registry",
        source_symbol="CapabilityRegistry.build_tool_directory",
        source_kind="method",
        expected_type="str",
        wiring_state="wired",
        notes=(
            "Capability descriptions for the active context space. "
            "C3a wiring reads from PopulationContext.capability_prompt "
            "(assembly already calls registry.build_tool_directory)."
        ),
    ),
    "actions.channel_registry": FieldProvenance(
        field_path="actions.channel_registry",
        source_module="kernos.kernel.channels",
        source_symbol="ChannelRegistry.get_outbound_capable",
        source_kind="method",
        expected_type="tuple[dict[str, Any], ...]",
        wiring_state="wired",
        notes=(
            "Outbound-capable channels for the active member. C3a "
            "wiring reads from PopulationContext.channel_registry "
            "(assembly resolves via handler._channel_registry)."
        ),
    ),
    # === MEMORY zone ===
    "memory.compaction_carry": FieldProvenance(
        field_path="memory.compaction_carry",
        source_module="kernos.kernel.compaction",
        source_symbol="CompactionService.load_state",
        source_kind="method",
        expected_type="str",
        wiring_state="wired",
        notes=(
            "Living State accumulated at the active space's last "
            "compact boundary. C3a wiring reads from "
            "PopulationContext.compaction_carry (assembly's "
            "_assemble_space_context already extracts the carry text)."
        ),
    ),
    "memory.awareness_whispers": FieldProvenance(
        field_path="memory.awareness_whispers",
        source_module="kernos.kernel.scheduler",
        source_symbol="TriggerStore.list_all",
        source_kind="method",
        expected_type="tuple[dict[str, Any], ...]",
        wiring_state="wired",
        notes=(
            "Pending whispers queued for delivery. C3a wiring reads "
            "from PopulationContext.awareness_whispers (assembly's "
            "_get_pending_awareness already filtered by status=pending "
            "+ member scope). Legacy renders to RESULTS zone; renderer "
            "at C3c preserves that placement when emitting from packet."
        ),
    ),
    "memory.gardener_observations": FieldProvenance(
        field_path="memory.gardener_observations",
        source_module="kernos.kernel.cohorts.gardener_cohort",
        source_symbol="register_gardener_cohort",
        source_kind="function",
        expected_type="tuple[dict[str, Any], ...]",
        wiring_state="wired",
        notes=(
            "Gardener-cohort output. C4 graduates this field to wired "
            "via PopulationContext.gardener_observations. Production "
            "wiring (C4) registers register_gardener_cohort in server.py "
            "and the integration runner copies the cohort output through "
            "into the packet's memory zone. Pre-cohort-output turns see "
            "an empty tuple — decoupled-only enrichment per the design review's spec "
            "clarification (legacy doesn't render gardener observations)."
        ),
    ),
    "memory.procedures": FieldProvenance(
        field_path="memory.procedures",
        source_module="kernos.messages.handler",
        source_symbol="_build_procedures_block",
        source_kind="function",
        expected_type="str",
        wiring_state="wired",
        notes=(
            "Active procedures (_procedures.md content) for the member/"
            "space scope. C3b wiring reads from "
            "PopulationContext.procedures_prefix (assembly's "
            "_assemble_space_context already collects via "
            "_files.read_file). Codex C1 review flagged as missing "
            "from the initial packet shape — added in fold; "
            "graduated to wired at C3b."
        ),
    ),
    "memory.canvases_summary": FieldProvenance(
        field_path="memory.canvases_summary",
        source_module="kernos.messages.handler",
        source_symbol="_build_canvases_block",
        source_kind="function",
        expected_type="str",
        wiring_state="wired",
        notes=(
            "Pinned canvases summary text. C3b wiring reads from "
            "PopulationContext.canvases_prefix (assembly's "
            "_assemble_space_context already collects via "
            "_build_canvases_prefix). Codex C1 review flagged as "
            "missing from the initial packet shape — added in fold; "
            "graduated to wired at C3b."
        ),
    ),
    # === CONVERSATION zone ===
    "conversation.messages": FieldProvenance(
        field_path="conversation.messages",
        source_module="kernos.kernel.conversation_log",
        source_symbol="ConversationLogger.read_recent",
        source_kind="method",
        expected_type="tuple[dict[str, Any], ...]",
        wiring_state="wired",
        notes=(
            "Messages array since last compact boundary. C3a wiring "
            "reads from PopulationContext.conversation_messages "
            "(assembly resolves via ConversationLogger.read_recent + "
            "_assemble_space_context)."
        ),
    ),
    # === TOOL SURFACE ===
    "tool_surface.always_pinned": FieldProvenance(
        field_path="tool_surface.always_pinned",
        source_module="kernos.messages.phases.assemble",
        source_symbol="run",
        source_kind="function",
        expected_type="tuple[dict[str, Any], ...]",
        wiring_state="wired",
        notes=(
            "Tuple of tool SCHEMAS (dicts) — the always-loaded subset "
            "of the final surfaced tool list. C5 wiring reads from "
            "PopulationContext.tool_surface_pinned, which assembly's "
            "run() partitions from ``ctx.tools`` using the "
            "``kernos.kernel.tool_catalog.ALWAYS_PINNED`` name set "
            "(schemas whose ``name`` is in ALWAYS_PINNED). "
            "PresenceRenderer reads tool_surface.all_tools() and passes "
            "to chain_caller's tools= argument (replaces the empty list "
            "the renderer used pre-C5). C5-fold: source metadata "
            "updated to point at the assembly partition site rather "
            "than the ALWAYS_PINNED set constant — the constant is the "
            "FILTER, not the SHAPE-of-content (which is schemas)."
        ),
    ),
    "tool_surface.active_zone": FieldProvenance(
        field_path="tool_surface.active_zone",
        source_module="kernos.messages.phases.assemble",
        source_symbol="run",
        source_kind="function",
        expected_type="tuple[dict[str, Any], ...]",
        wiring_state="wired",
        notes=(
            "Surfacer-selected tools — the rest of ``ctx.tools`` "
            "beyond the always_pinned subset. C5 wiring reads from "
            "PopulationContext.tool_surface_active. The legacy "
            "assembly's surfacer runs first (covering ALWAYS_PINNED + "
            "COMMON_MCP_NAMES + local affordances + activated "
            "capabilities + catalog-scan + budget/LRU eviction); the "
            "C5 partition then splits the resulting final list into "
            "pinned + active using the ALWAYS_PINNED name set. The "
            "active zone therefore includes everything the assembler "
            "surfaced that's not in ALWAYS_PINNED. C5-fold: source "
            "metadata corrected to point at the assembly partition "
            "rather than ``ToolCatalog.build_catalog_text`` (which is "
            "only one input the surfacer consults)."
        ),
    ),
    "tool_surface.request_tool": FieldProvenance(
        field_path="tool_surface.request_tool",
        source_module="kernos.kernel.tools.schemas",
        source_symbol="REQUEST_TOOL",
        source_kind="constant",
        expected_type="dict[str, Any]",
        wiring_state="wired",
        notes=(
            "Always-present meta-tool for capability activation. "
            "C5 moves into ALWAYS_PINNED; field stays for contract "
            "test targeting."
        ),
    ),
    # === SAFETY CONSTRAINTS ===
    "safety_constraints.sensitivity_gates": FieldProvenance(
        field_path="safety_constraints.sensitivity_gates",
        source_module="kernos.kernel.state",
        source_symbol="KnowledgeEntry",
        source_kind="constant",
        expected_type="tuple[dict[str, Any], ...]",
        wiring_state="wired",
        notes=(
            "Sensitivity classification rules relevant to active "
            "member/space scope. C3b wiring derives a tuple of "
            "{author, sensitivity} records from the surfaced "
            "knowledge entries (entries already filtered by the "
            "disclosure gate; this field carries the residual "
            "policy data the model must reason about)."
        ),
    ),
    "safety_constraints.disclosure_layer": FieldProvenance(
        field_path="safety_constraints.disclosure_layer",
        source_module="kernos.kernel.disclosure_gate",
        source_symbol="build_permission_map",
        source_kind="function",
        expected_type="dict[str, str]",
        wiring_state="wired",
        notes=(
            "Viewer-aware permission profile for the active member, "
            "mapping target_member_id → permission. C3b wiring reads "
            "the pre-resolved map from PopulationContext.disclosure_layer "
            "(assembly already calls build_permission_map at "
            "ctx._disclosure_perm_map; we re-use that resolution to "
            "avoid duplicate work)."
        ),
    ),
    "safety_constraints.cross_member_rules": FieldProvenance(
        field_path="safety_constraints.cross_member_rules",
        source_module="kernos.kernel.state",
        source_symbol="StateStore.query_covenant_rules",
        source_kind="method",
        expected_type="tuple[dict[str, Any], ...]",
        wiring_state="wired",
        notes=(
            "Cross-member visibility rules — covenants tagged with "
            "relationship: scope. C3b wiring reads from "
            "PopulationContext.cross_member_rules (assembly resolves "
            "by filtering covenants with rule.context_space starting "
            "with 'relationship:')."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Population context — the runtime substrate handle
# ---------------------------------------------------------------------------


@dataclass
class PopulationContext:
    """Handle to the running substrate needed to populate the packet.

    Mutable on purpose: assembly populates this incrementally during
    turn construction. The frozen :class:`CognitiveContext` is built
    from the populated context at the assembly seam.

    Fields are typed loosely (``Any``) at C1 because the substrate's
    public surface is a fluid mix of typed and dict-shaped APIs.
    Future C-phase tightens types as the wiring lands.

    C3a expanded the resolved-substrate fields so populate_field can
    read pre-resolved values from the context rather than re-querying
    services. Codex C3a-design Q2 verdict: "construct in assemble.py
    from already-loaded locals" — the assembly already owns the
    selected substrate (covenant selection, knowledge entries after
    MessageAnalyzer + disclosure gate, awareness whispers, results
    prefix, compaction carry, etc.); populating PopulationContext
    from those locals avoids re-querying and makes populate_field
    a thin pass-through. Service handles (state_store, instance_db,
    etc.) remain on the context so future fields with bespoke
    queries can reach them without an additional construction site.
    """

    # Identity / addressing.
    instance_id: str = ""
    member_id: str = ""
    space_id: str = ""

    # Service handles (kept for fields that prefer direct queries
    # over pre-resolved values).
    state_store: Any = None
    instance_db: Any = None
    handler: Any = None  # for block-builder access (tool_catalog, channels)
    retrieval_service: Any = None
    compaction_service: Any = None
    trigger_store: Any = None

    # NOW block raw fields.
    user_timezone: str = ""
    platform: str = ""
    auth_level: str = ""
    timestamp_utc: datetime | None = None
    active_space_name: str = ""
    member_display_name: str = ""
    agent_name: str = ""
    execution_envelope: dict[str, Any] | None = None

    # Identity / state substrate (resolved by assembly).
    member_profile: dict[str, Any] = field(default_factory=dict)
    soul: Any = None

    # Resolved substrate (filled by assembly's already-loaded locals).
    covenants: tuple[Any, ...] = ()
    space_names: dict[str, str] = field(default_factory=dict)
    instance_stewardship: str = ""
    relationships: tuple[dict[str, Any], ...] = ()
    knowledge_entries: tuple[Any, ...] = ()
    results_prefix: str = ""
    capability_prompt: str = ""
    channel_registry: tuple[dict[str, Any], ...] = ()
    compaction_carry: str = ""
    awareness_whispers: tuple[dict[str, Any], ...] = ()
    conversation_messages: tuple[dict[str, Any], ...] = ()

    # C3b additions — procedures + canvases + safety substrate.
    procedures_prefix: str = ""
    canvases_prefix: str = ""
    sensitivity_gates: tuple[dict[str, Any], ...] = ()
    disclosure_layer: dict[str, str] = field(default_factory=dict)
    cross_member_rules: tuple[dict[str, Any], ...] = ()

    # C4 additions — gardener cohort output. Empty until production
    # wiring extracts the gardener_cohort's CohortOutput onto this
    # field. Decoupled-only enrichment per the design review's spec clarification
    # (legacy doesn't render gardener observations).
    gardener_observations: tuple[dict[str, Any], ...] = ()

    # C5 additions — tool surface partitions. Assembly partitions
    # ctx.tools using the ALWAYS_PINNED name set: schemas whose name
    # is in ALWAYS_PINNED land in ``tool_surface_pinned``; the rest
    # (surfacer-selected dynamic zone) lands in ``tool_surface_active``.
    tool_surface_pinned: tuple[dict[str, Any], ...] = ()
    tool_surface_active: tuple[dict[str, Any], ...] = ()


async def populate_field(name: str, ctx: PopulationContext) -> Any:
    """Resolve a packet field from its documented source.

    Single seam through which all field population flows. Routes by
    ``wiring_state``:

    * ``wired`` — explicit per-field routing returns real content.
    * ``deferred`` — returns the type-appropriate default
      (matches the ``deferred_until`` phase's not-yet-wired
      contract).
    * ``unwired_expected`` — raises :class:`NotImplementedError`.
      The escape-hatch state catches entries added without
      explicit classification; the field-availability test pins
      zero ``unwired_expected`` entries by end of C5.

    Raises :class:`KeyError` if ``name`` is not in
    :data:`FIELD_PROVENANCE`.
    """
    if name not in FIELD_PROVENANCE:
        raise KeyError(f"unknown packet field: {name!r}")
    prov = FIELD_PROVENANCE[name]

    if prov.wiring_state == "deferred":
        return _default_for(prov.expected_type)

    if prov.wiring_state == "unwired_expected":
        raise NotImplementedError(
            f"field {name!r} has wiring_state='unwired_expected' — "
            f"either route it explicitly in populate_field or mark it "
            f"deferred with deferred_until=<phase>"
        )

    # wiring_state == "wired" — explicit routing follows.
    if name == "rules.operating_principles":
        from kernos.kernel.template import PRIMARY_TEMPLATE
        return PRIMARY_TEMPLATE.operating_principles
    if name == "rules.bootstrap_prompt":
        from kernos.kernel.template import PRIMARY_TEMPLATE
        graduated = bool(ctx.member_profile.get("bootstrap_graduated", False))
        return None if graduated else PRIMARY_TEMPLATE.bootstrap_prompt
    if name == "rules.hatching_prompt":
        from kernos.messages.handler import (
            _INHERIT_HATCHING_PROMPT,
            _UNIQUE_HATCHING_PROMPT,
        )
        graduated = bool(ctx.member_profile.get("bootstrap_graduated", False))
        if graduated:
            return None
        return (
            _INHERIT_HATCHING_PROMPT
            if ctx.member_profile.get("agent_name")
            else _UNIQUE_HATCHING_PROMPT
        )
    if name == "rules.covenants":
        # Assembly resolves the per-turn selection (pinned + situational)
        # and stores the tuple on PopulationContext.covenants. C3a
        # populate_field consumes the resolution; falls back to a direct
        # query when no pre-resolved value is set (test fixtures).
        if ctx.covenants:
            return tuple(ctx.covenants)
        if ctx.state_store is None:
            return ()
        rules = await ctx.state_store.query_covenant_rules(
            ctx.instance_id,
            capability=None,
            context_space_scope=[ctx.space_id, None] if ctx.space_id else [None],
            active_only=True,
        )
        return tuple(rules)
    if name == "rules.space_names":
        return dict(ctx.space_names or {})
    if name == "rules.instance_stewardship":
        # Prefer the pre-resolved value from PopulationContext; fall
        # back to a direct query for legacy callers.
        if ctx.instance_stewardship:
            return ctx.instance_stewardship
        if ctx.instance_db is None:
            return ""
        try:
            return await ctx.instance_db.get_instance_stewardship()
        except Exception:
            return ""
    if name == "now":
        return _construct_now_block(ctx)
    if name == "state.soul":
        return ctx.soul
    if name == "state.member_profile":
        return dict(ctx.member_profile or {})
    if name == "state.relationships":
        return tuple(ctx.relationships or ())
    if name == "state.knowledge_entries":
        return tuple(ctx.knowledge_entries or ())
    if name == "results.results_prefix":
        return ctx.results_prefix or ""
    if name == "actions.capability_prompt":
        return ctx.capability_prompt or ""
    if name == "actions.channel_registry":
        return tuple(ctx.channel_registry or ())
    if name == "memory.compaction_carry":
        return ctx.compaction_carry or ""
    if name == "memory.awareness_whispers":
        return tuple(ctx.awareness_whispers or ())
    if name == "conversation.messages":
        return tuple(ctx.conversation_messages or ())
    if name == "memory.procedures":
        return ctx.procedures_prefix or ""
    if name == "memory.canvases_summary":
        return ctx.canvases_prefix or ""
    if name == "memory.gardener_observations":
        return tuple(ctx.gardener_observations or ())
    if name == "safety_constraints.sensitivity_gates":
        return tuple(ctx.sensitivity_gates or ())
    if name == "safety_constraints.disclosure_layer":
        return dict(ctx.disclosure_layer or {})
    if name == "safety_constraints.cross_member_rules":
        return tuple(ctx.cross_member_rules or ())
    if name == "tool_surface.always_pinned":
        return tuple(ctx.tool_surface_pinned or ())
    if name == "tool_surface.active_zone":
        return tuple(ctx.tool_surface_active or ())
    if name == "tool_surface.request_tool":
        from kernos.kernel.tools.schemas import REQUEST_TOOL
        return REQUEST_TOOL

    # Wired entries must have explicit routing above. If we reach
    # here, the entry's wiring_state was "wired" but no per-field
    # branch matched — that's a bug in the map or in this function.
    raise NotImplementedError(
        f"field {name!r} has wiring_state='wired' but no explicit "
        f"population route in populate_field. Either add the route "
        f"above or change wiring_state to 'deferred'/'unwired_expected'."
    )


def _construct_now_block(ctx: PopulationContext) -> Any:
    """Build a :class:`NowBlock` from the population context."""
    from kernos.kernel.cognitive_context.types import NowBlock
    return NowBlock(
        timestamp_utc=ctx.timestamp_utc or datetime.now(timezone.utc),
        user_timezone=ctx.user_timezone,
        platform=ctx.platform,
        auth_level=ctx.auth_level,
        instance_id=ctx.instance_id,
        member_id=ctx.member_id,
        member_display_name=ctx.member_display_name,
        active_space_id=ctx.space_id,
        active_space_name=ctx.active_space_name,
        agent_name=ctx.agent_name,
        execution_envelope=ctx.execution_envelope,
    )


def _default_for(expected_type: str) -> Any:
    """Return a type-appropriate default value for a field whose
    population isn't wired at this commit. Used at C1 so contract
    tests at C2 can walk the map and assert each documented source
    resolves to something — even un-wired fields produce the right
    SHAPE so the test for "field has expected type" can pass while
    the test for "field has expected CONTENT" remains red until
    the wiring lands."""
    if expected_type.startswith("tuple"):
        return ()
    if expected_type.startswith("dict"):
        return {}
    if expected_type.startswith("set"):
        return set()
    if "None" in expected_type:
        return None
    if "str" in expected_type:
        return ""
    return None


async def populate_packet(ctx: PopulationContext) -> "CognitiveContext":
    """Populate the full packet from the substrate.

    At C1 only the fields with ``wiring_state="wired"`` are routed
    to real sources via :func:`populate_field`; deferred fields
    populate to their type-appropriate default. C3a-c extends
    routing as deferred fields graduate to wired.

    The function calls :func:`populate_field` for every field — this
    is intentional. The single-seam discipline keeps wiring changes
    isolated to that function rather than fanning across packet
    construction sites; the deferred default returns are part of
    the contract.
    """
    from kernos.kernel.cognitive_context.types import (
        ActionsBlock,
        CognitiveContext,
        ConversationBlock,
        MemoryBlock,
        ResultsBlock,
        RulesBlock,
        SafetyConstraints,
        StateBlock,
        ToolSurface,
    )

    rules = RulesBlock(
        operating_principles=await populate_field("rules.operating_principles", ctx),
        bootstrap_prompt=await populate_field("rules.bootstrap_prompt", ctx),
        hatching_prompt=await populate_field("rules.hatching_prompt", ctx),
        covenants=await populate_field("rules.covenants", ctx),
        space_names=await populate_field("rules.space_names", ctx),
        instance_stewardship=await populate_field("rules.instance_stewardship", ctx),
    )
    now = await populate_field("now", ctx)
    state = StateBlock(
        soul=await populate_field("state.soul", ctx),
        member_profile=await populate_field("state.member_profile", ctx),
        relationships=await populate_field("state.relationships", ctx),
        knowledge_entries=await populate_field("state.knowledge_entries", ctx),
    )
    results = ResultsBlock(
        results_prefix=await populate_field("results.results_prefix", ctx),
    )
    actions = ActionsBlock(
        capability_prompt=await populate_field("actions.capability_prompt", ctx),
        channel_registry=await populate_field("actions.channel_registry", ctx),
    )
    memory = MemoryBlock(
        compaction_carry=await populate_field("memory.compaction_carry", ctx),
        awareness_whispers=await populate_field("memory.awareness_whispers", ctx),
        gardener_observations=await populate_field("memory.gardener_observations", ctx),
        procedures=await populate_field("memory.procedures", ctx),
        canvases_summary=await populate_field("memory.canvases_summary", ctx),
    )
    conversation = ConversationBlock(
        messages=await populate_field("conversation.messages", ctx) or ctx.conversation_messages,
    )
    tool_surface = ToolSurface(
        always_pinned=await populate_field("tool_surface.always_pinned", ctx),
        active_zone=await populate_field("tool_surface.active_zone", ctx),
        request_tool=await populate_field("tool_surface.request_tool", ctx),
    )
    safety = SafetyConstraints(
        sensitivity_gates=await populate_field(
            "safety_constraints.sensitivity_gates", ctx,
        ),
        disclosure_layer=await populate_field(
            "safety_constraints.disclosure_layer", ctx,
        ),
        cross_member_rules=await populate_field(
            "safety_constraints.cross_member_rules", ctx,
        ),
    )
    return CognitiveContext(
        rules=rules,
        now=now,
        state=state,
        results=results,
        actions=actions,
        memory=memory,
        conversation=conversation,
        tool_surface=tool_surface,
        safety_constraints=safety,
    )


__all__ = [
    "FIELD_PROVENANCE",
    "FieldProvenance",
    "PopulationContext",
    "populate_field",
    "populate_packet",
]
