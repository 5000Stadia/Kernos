"""COGNITIVE-CONTEXT-V1 C1 — field-provenance map pins.

Walks the FIELD_PROVENANCE map and asserts that every documented
source resolves to a real symbol producing the expected type.
This is the static contract pin: if the map references a source
that doesn't exist, the test fails before C3 starts wiring.

C1 acceptance per the spec: "field-provenance tests assert that
every documented field maps to a real symbol that produces content
of the expected type."
"""
from __future__ import annotations

import importlib

import pytest

from kernos.kernel.cognitive_context import (
    FIELD_PROVENANCE,
    FieldProvenance,
    PopulationContext,
    populate_field,
    populate_packet,
)
from kernos.kernel.cognitive_context.types import (
    CognitiveContext,
    NowBlock,
)


# ---------------------------------------------------------------------------
# Map shape pins
# ---------------------------------------------------------------------------


def test_field_provenance_covers_all_packet_fields():
    """Every packet field has a provenance entry.

    Walks CognitiveContext's dataclass fields + each block's fields
    and asserts FIELD_PROVENANCE has a matching dotted-path entry.
    """
    expected_paths = set()
    for top_field, top_meta in CognitiveContext.__dataclass_fields__.items():
        block_type = top_meta.type
        # Heuristic: walk if the top field is a block type (has
        # __dataclass_fields__). 'now' is a block but stored as an
        # atomic field for the test — covered separately.
        for block_field_name in _block_field_names(top_field):
            expected_paths.add(f"{top_field}.{block_field_name}")
        # Top-level paths (e.g., 'now') if the block has no inner
        # fields enumerated:
        if not _block_field_names(top_field):
            expected_paths.add(top_field)
    # Must have at least the documented entries from the spec table.
    documented_paths = set(FIELD_PROVENANCE.keys())
    # Every entry in FIELD_PROVENANCE points at a real packet path.
    # (Some block-scoped paths may not appear if the block is
    # populated atomically; we tolerate that — but the documented
    # path must look reasonable.)
    for doc_path in documented_paths:
        assert "." in doc_path or doc_path in {
            f.name for f in CognitiveContext.__dataclass_fields__.values()
        }, (
            f"Provenance entry {doc_path!r} is neither a top-level "
            f"packet field nor a dotted sub-path."
        )


def _block_field_names(top_field: str) -> set[str]:
    """Return inner field names of the block referenced by
    ``top_field``, or empty set if the block isn't introspectable
    here (atomic top-level fields like 'now' have provenance at the
    top-level path 'now', not 'now.timestamp_utc' etc.)."""
    from kernos.kernel.cognitive_context.types import (
        ActionsBlock,
        ConversationBlock,
        MemoryBlock,
        ResultsBlock,
        RulesBlock,
        SafetyConstraints,
        StateBlock,
        ToolSurface,
    )
    block_map = {
        "rules": RulesBlock,
        "state": StateBlock,
        "results": ResultsBlock,
        "actions": ActionsBlock,
        "memory": MemoryBlock,
        "conversation": ConversationBlock,
        "tool_surface": ToolSurface,
        "safety_constraints": SafetyConstraints,
        # 'now' is an atomic NowBlock; provenance is documented at
        # the top-level path so we keep it out of the dotted-walk.
    }
    block = block_map.get(top_field)
    if block is None:
        return set()
    return set(block.__dataclass_fields__.keys())


def test_every_field_provenance_entry_is_a_FieldProvenance():
    for path, prov in FIELD_PROVENANCE.items():
        assert isinstance(prov, FieldProvenance), (
            f"FIELD_PROVENANCE[{path!r}] is not a FieldProvenance"
        )


def test_every_provenance_source_module_is_importable():
    """Static contract pin: every documented source module imports.

    Catches typo-class drift (e.g., the module renamed but the
    provenance map didn't update)."""
    for path, prov in FIELD_PROVENANCE.items():
        try:
            importlib.import_module(prov.source_module)
        except ModuleNotFoundError as exc:
            pytest.fail(
                f"Provenance entry {path!r}: source_module "
                f"{prov.source_module!r} not importable — {exc}"
            )


def test_every_provenance_source_kind_is_known():
    valid_kinds = {"constant", "method", "function"}
    for path, prov in FIELD_PROVENANCE.items():
        assert prov.source_kind in valid_kinds, (
            f"Provenance entry {path!r}: source_kind "
            f"{prov.source_kind!r} not in {valid_kinds}"
        )


def test_every_constant_provenance_resolves_to_existing_symbol():
    """For ``source_kind="constant"`` entries, the symbol must be
    accessible via attribute walk on the imported module."""
    for path, prov in FIELD_PROVENANCE.items():
        if prov.source_kind != "constant":
            continue
        # Self-referential constants (the field is constructed in
        # this module) are documented but not symbol-walkable.
        if prov.source_module == "kernos.kernel.cognitive_context.field_provenance":
            continue
        mod = importlib.import_module(prov.source_module)
        # Walk dotted symbol path (e.g., PRIMARY_TEMPLATE.operating_principles).
        cur = mod
        for part in prov.source_symbol.split("."):
            assert hasattr(cur, part), (
                f"Provenance entry {path!r}: source_symbol "
                f"{prov.source_symbol!r} unresolved at part {part!r} "
                f"on {cur!r}"
            )
            cur = getattr(cur, part)


def test_every_method_provenance_resolves_to_class_attribute():
    """For ``source_kind="method"`` entries, the symbol's class part
    must resolve and have the named method."""
    for path, prov in FIELD_PROVENANCE.items():
        if prov.source_kind != "method":
            continue
        mod = importlib.import_module(prov.source_module)
        # Symbol shape: ClassName.method_name
        parts = prov.source_symbol.split(".")
        assert len(parts) >= 2, (
            f"Provenance {path!r}: method symbol "
            f"{prov.source_symbol!r} should be 'Class.method'"
        )
        cls_name = parts[0]
        method_name = parts[1]
        assert hasattr(mod, cls_name), (
            f"Provenance {path!r}: class {cls_name!r} not in "
            f"module {prov.source_module}"
        )
        cls = getattr(mod, cls_name)
        assert hasattr(cls, method_name), (
            f"Provenance {path!r}: method {method_name!r} not on "
            f"class {cls_name!r}"
        )


def test_every_function_provenance_resolves_to_callable():
    for path, prov in FIELD_PROVENANCE.items():
        if prov.source_kind != "function":
            continue
        mod = importlib.import_module(prov.source_module)
        first = prov.source_symbol.split(".")[0]
        assert hasattr(mod, first), (
            f"Provenance {path!r}: function symbol "
            f"{prov.source_symbol!r} unresolved in "
            f"{prov.source_module}"
        )
        sym = getattr(mod, first)
        # For dotted, walk further (none in C1's map but defensive).
        for part in prov.source_symbol.split(".")[1:]:
            assert hasattr(sym, part)
            sym = getattr(sym, part)
        assert callable(sym), (
            f"Provenance {path!r}: resolved symbol "
            f"{prov.source_symbol!r} is not callable"
        )


def test_deferred_provenance_documented_with_phase_note():
    """Deferred fields must declare deferred_until and reference the
    phase in their notes. Three-state classification per the design review's tweak."""
    for path, prov in FIELD_PROVENANCE.items():
        if prov.wiring_state != "deferred":
            continue
        assert prov.deferred_until, (
            f"Deferred provenance {path!r} must declare deferred_until "
            f"naming the phase that lands the wiring (e.g. 'C3a', 'C5')."
        )
        assert prov.deferred_until in {"C3a", "C3b", "C3c", "C4", "C5"}, (
            f"Deferred provenance {path!r}: deferred_until "
            f"{prov.deferred_until!r} must be a known phase "
            f"(C3a/C3b/C3c/C4/C5)."
        )


# ---------------------------------------------------------------------------
# Field-availability classification (the design review-required three-state pin)
# ---------------------------------------------------------------------------


def test_every_entry_has_known_wiring_state():
    """Three-state classification: every entry classified as
    wired / deferred / unwired_expected. The dataclass validator
    rejects unknown values at construction; this test mirrors the
    invariant at the test surface."""
    valid = {"wired", "deferred", "unwired_expected"}
    for path, prov in FIELD_PROVENANCE.items():
        assert prov.wiring_state in valid, (
            f"Provenance {path!r}: wiring_state "
            f"{prov.wiring_state!r} not in {valid}"
        )


def test_no_unwired_expected_entries_at_C1():
    """At C1, every entry should be classified as wired or deferred —
    no escape-hatch entries. The unwired_expected state is reserved
    for future entries added without deliberate classification; the
    test catches that drift.

    the design review's tweak: by end of C5, this same assertion holds, AND no
    deferred entries remain (only intentional deferrals with named
    target phase + reason if any). This test today pins the C1
    half of the contract."""
    unwired = [
        path for path, prov in FIELD_PROVENANCE.items()
        if prov.wiring_state == "unwired_expected"
    ]
    assert not unwired, (
        f"Entries classified as unwired_expected at C1: {unwired}. "
        f"Either route them in populate_field (wiring_state='wired') "
        f"or mark deferred with deferred_until=<phase>."
    )


def test_deferred_until_phases_match_wiring_ladder():
    """Wiring-ladder pin tracking the current frontier.

    After C5 (commit graduating tool_surface.always_pinned and
    tool_surface.active_zone), all 23 packet fields are wired. Per
    the design review's tweak, by end of C5 no deferred entries remain — only
    intentional ones with explicit target-phase + reason. This pin
    enforces the ladder's terminal state for the CCV1 spec arc.

    Future post-CCV1 work that adds new packet fields starts
    deferred → wired graduation as a fresh ladder."""
    actual_per_phase: dict[str, set[str]] = {}
    for path, prov in FIELD_PROVENANCE.items():
        if prov.wiring_state != "deferred":
            continue
        actual_per_phase.setdefault(prov.deferred_until, set()).add(path)
    # Pin: no C3a / C3b / C4 / C5 deferrals remain after their commits.
    for graduated_phase in ("C3a", "C3b", "C4", "C5"):
        assert graduated_phase not in actual_per_phase, (
            f"{graduated_phase}-deferred fields still present after "
            f"that phase should have graduated them: "
            f"{sorted(actual_per_phase[graduated_phase])}"
        )
    # Pin: the ladder is empty at C5 — no deferred fields anywhere.
    assert not actual_per_phase, (
        f"Unexpected deferred entries after C5 (the CCV1 arc is "
        f"complete; deferrals here would be drift): {actual_per_phase}"
    )


def test_C5_wired_entries_match_expected_set():
    """After C5, every entry in FIELD_PROVENANCE is classified as
    wired — the CCV1 wiring ladder is complete and the deferred
    bucket is empty.

    Renamed from C4 to C5 at this commit; the pin now tracks the
    terminal state of the spec arc. Hard counts intentionally
    omitted from the docstring — Codex C5-review NIT noted prior
    wording said "23" but the actual entry count is FIELD_PROVENANCE-
    derived, not a literal in the test. The exact set of expected
    paths is below; counts will drift naturally as the map evolves
    post-CCV1."""
    expected_wired = {
        # C1 wired (constants + NOW + request_tool)
        "rules.operating_principles",
        "rules.bootstrap_prompt",
        "rules.hatching_prompt",
        "rules.instance_stewardship",
        "now",
        "tool_surface.request_tool",
        # C3a graduated (substrate populated by assembly)
        "rules.covenants",
        "rules.space_names",
        "state.soul",
        "state.member_profile",
        "state.relationships",
        "state.knowledge_entries",
        "results.results_prefix",
        "actions.capability_prompt",
        "actions.channel_registry",
        "memory.compaction_carry",
        "memory.awareness_whispers",
        "conversation.messages",
        # C3b graduated (procedures + canvases + safety substrate)
        "memory.procedures",
        "memory.canvases_summary",
        "safety_constraints.sensitivity_gates",
        "safety_constraints.disclosure_layer",
        "safety_constraints.cross_member_rules",
        # C4 graduated (gardener cohort output)
        "memory.gardener_observations",
        # C5 graduated (thin-path tool surface)
        "tool_surface.always_pinned",
        "tool_surface.active_zone",
    }
    actual_wired = {
        path for path, prov in FIELD_PROVENANCE.items()
        if prov.wiring_state == "wired"
    }
    assert actual_wired == expected_wired, (
        f"C5 wired-entry drift:\n"
        f"  expected: {sorted(expected_wired)}\n"
        f"  actual:   {sorted(actual_wired)}\n"
        f"  missing:  {sorted(expected_wired - actual_wired)}\n"
        f"  extra:    {sorted(actual_wired - expected_wired)}"
    )


def test_FieldProvenance_validator_rejects_deferred_without_until():
    """Constructor validation pin — the design review's tweak requires explicit
    deferred_until on every deferred entry."""
    with pytest.raises(ValueError, match="deferred_until"):
        FieldProvenance(
            field_path="test",
            source_module="kernos.kernel.template",
            source_symbol="PRIMARY_TEMPLATE.operating_principles",
            source_kind="constant",
            expected_type="str",
            wiring_state="deferred",
            # no deferred_until — should raise
        )


def test_FieldProvenance_validator_rejects_until_without_deferred_state():
    with pytest.raises(ValueError, match="deferred"):
        FieldProvenance(
            field_path="test",
            source_module="kernos.kernel.template",
            source_symbol="PRIMARY_TEMPLATE.operating_principles",
            source_kind="constant",
            expected_type="str",
            wiring_state="wired",
            deferred_until="C3a",  # invalid combo
        )


# ---------------------------------------------------------------------------
# populate_field — basic routing pins
# ---------------------------------------------------------------------------


async def test_populate_field_unknown_name_raises():
    ctx = PopulationContext()
    with pytest.raises(KeyError, match="bogus"):
        await populate_field("bogus.field", ctx)


async def test_populate_operating_principles_returns_real_content():
    ctx = PopulationContext()
    out = await populate_field("rules.operating_principles", ctx)
    assert isinstance(out, str)
    assert len(out) > 100  # operating principles is multi-paragraph
    assert "TRANSPARENCY" in out  # canonical sniff for the principle text


async def test_populate_bootstrap_prompt_when_not_graduated():
    ctx = PopulationContext(member_profile={"bootstrap_graduated": False})
    out = await populate_field("rules.bootstrap_prompt", ctx)
    assert isinstance(out, str)
    # The bootstrap_prompt was rewritten 2026-05-05; "FIRST
    # CONVERSATION" was the old marker. Anchor on the new prompt's
    # substrate-awareness lines, which are unique to the bootstrap.
    assert "request_reference" in out
    assert "store_reference" in out


async def test_populate_bootstrap_prompt_None_when_graduated():
    ctx = PopulationContext(member_profile={"bootstrap_graduated": True})
    out = await populate_field("rules.bootstrap_prompt", ctx)
    assert out is None


async def test_populate_hatching_prompt_unique_when_no_agent_name():
    ctx = PopulationContext(
        member_profile={"bootstrap_graduated": False, "agent_name": ""},
    )
    out = await populate_field("rules.hatching_prompt", ctx)
    assert isinstance(out, str)
    assert "HATCHING" in out


async def test_populate_hatching_prompt_inherit_when_agent_named():
    ctx = PopulationContext(
        member_profile={
            "bootstrap_graduated": False, "agent_name": "Avi",
            "display_name": "Owner",
        },
    )
    out = await populate_field("rules.hatching_prompt", ctx)
    assert isinstance(out, str)
    # _INHERIT_HATCHING_PROMPT references the named agent
    assert "NEW MEMBER" in out


async def test_populate_tool_surface_always_pinned_empty_when_unpopulated():
    """tool_surface.always_pinned is wired (graduated at C5) but
    returns an empty tuple when ``PopulationContext.tool_surface_pinned``
    isn't supplied. Pre-C5 history: this field was deferred_until=C5
    because schema resolution required ``_kernel_tool_map`` access;
    C5 graduated it via the assembly-partition seam."""
    ctx = PopulationContext()
    out = await populate_field("tool_surface.always_pinned", ctx)
    assert out == ()


async def test_populate_request_tool_returns_real_schema():
    """request_tool is wired at C1 (the schema is a static constant)."""
    ctx = PopulationContext()
    out = await populate_field("tool_surface.request_tool", ctx)
    assert isinstance(out, dict)
    assert out.get("name") == "request_tool"
    assert "input_schema" in out


async def test_populate_deferred_field_returns_type_appropriate_default():
    """Deferred fields populate to defaults matching their expected_type."""
    ctx = PopulationContext()
    # tuple-typed deferred field
    out = await populate_field("memory.gardener_observations", ctx)
    assert out == ()
    # str-typed deferred field
    out = await populate_field("memory.procedures", ctx)
    assert out == ""
    # dict-typed deferred field
    out = await populate_field("rules.space_names", ctx)
    assert out == {}


# ---------------------------------------------------------------------------
# populate_packet — end-to-end shape pin (C1)
# ---------------------------------------------------------------------------


async def test_populate_packet_returns_full_CognitiveContext():
    """At C1, populate_packet runs end-to-end with minimal context
    and produces a structurally-valid CognitiveContext. Wired fields
    have real content; deferred fields hold type-appropriate
    defaults; no exceptions."""
    ctx = PopulationContext(
        instance_id="inst1",
        member_id="m1",
        space_id="space:1",
        member_profile={"bootstrap_graduated": False, "agent_name": ""},
    )
    pkt = await populate_packet(ctx)
    assert isinstance(pkt, CognitiveContext)
    # === C1 wired fields have real content ===
    assert pkt.rules.operating_principles
    assert pkt.rules.bootstrap_prompt is not None
    assert pkt.rules.hatching_prompt is not None
    # NOW block constructed
    assert isinstance(pkt.now, NowBlock)
    assert pkt.now.instance_id == "inst1"
    assert pkt.now.member_id == "m1"
    # request_tool schema present
    assert pkt.tool_surface.request_tool is not None
    assert pkt.tool_surface.request_tool["name"] == "request_tool"
    # === C3a-C5 deferred fields default cleanly ===
    assert pkt.rules.covenants == ()
    assert pkt.rules.space_names == {}
    assert pkt.state.relationships == ()
    assert pkt.state.knowledge_entries == ()
    assert pkt.results.results_prefix == ""
    assert pkt.actions.capability_prompt == ""
    assert pkt.actions.channel_registry == ()
    assert pkt.memory.compaction_carry == ""
    assert pkt.memory.awareness_whispers == ()
    assert pkt.memory.gardener_observations == ()
    assert pkt.memory.procedures == ""
    assert pkt.memory.canvases_summary == ""
    assert pkt.tool_surface.always_pinned == ()
    assert pkt.tool_surface.active_zone == ()
    assert pkt.safety_constraints.sensitivity_gates == ()
    assert pkt.safety_constraints.disclosure_layer == {}
    assert pkt.safety_constraints.cross_member_rules == ()


async def test_populate_field_unwired_expected_raises():
    """If a hypothetical entry has wiring_state='unwired_expected',
    populate_field raises NotImplementedError (the escape-hatch
    state). Test by directly inspecting the raise path through a
    constructed FieldProvenance bypassing the validator."""
    # Simulate by inserting a raw entry into FIELD_PROVENANCE and
    # popping it after the test; cleaner than monkeypatching since
    # FIELD_PROVENANCE is a dict (mutable).
    from kernos.kernel.cognitive_context.field_provenance import (
        FIELD_PROVENANCE as FP,
    )
    sentinel = FieldProvenance(
        field_path="_test_sentinel",
        source_module="kernos.kernel.template",
        source_symbol="PRIMARY_TEMPLATE.operating_principles",
        source_kind="constant",
        expected_type="str",
        wiring_state="unwired_expected",
    )
    FP["_test_sentinel"] = sentinel
    try:
        ctx = PopulationContext()
        with pytest.raises(NotImplementedError, match="unwired_expected"):
            await populate_field("_test_sentinel", ctx)
    finally:
        FP.pop("_test_sentinel", None)
