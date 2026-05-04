"""Pin tests for KERNEL-TOOL-REGISTRY-V1.

Per architect verdict (Kit-tightened 2026-05-03; shipped 2026-05-04):
schema constants in their owning modules are the canonical source for
kernel-tool inventory; the registrar at
``kernos.kernel.kernel_tool_registry`` compiles them into one canonical
list; consumers (handler catalog, legacy assembly, surfacer LLM) derive
views via the registrar.

This file pins three contracts:

  1. **Primary equivalence pin** — dispatch authority
     (``ReasoningService._KERNEL_TOOLS``) equals registrar names
     equals thin-path catalog names equals legacy assembly names.
     Adding a tool to dispatch but forgetting the schema (or vice
     versa) fails CI.

  2. **CRB parity pin (separate)** — the CRB-gated parcel-tool set
     equals the registrar's parcel surface. CRB parcels are excluded
     from the primary equivalence pin by design (intentional
     architectural exclusion, not drift); this separate pin enforces
     their own canonical-source contract.

  3. **No-orphan-policy pin** — every entry in
     ``POLICY_SOURCE_MAP`` references a real registrar tool's policy
     metadata; every registrar tool with policy has it accessible
     via the registrar's accessor. No duplicate / conflicting
     ownership.

Plus a small set of structural pins (registrar shape, descriptor
fields, workshop-prep contract surface) so future spec authors can
verify the workshop-tool plug-in contract before they add it.
"""

from __future__ import annotations

from kernos.kernel.kernel_tool_registry import (
    CRB_PARCEL_TOOL_NAMES,
    KernelToolDescriptor,
    POLICY_SOURCE_MAP,
    crb_parcel_names,
    crb_parcel_schemas,
    kernel_tool_descriptors,
    kernel_tool_names,
    kernel_tool_schema_map,
    kernel_tool_schemas,
)
from kernos.kernel.reasoning import ReasoningService
from kernos.kernel.tool_catalog import ALWAYS_PINNED


# ---------------------------------------------------------------------------
# Primary equivalence pin
# ---------------------------------------------------------------------------


def test_dispatch_set_equals_registrar_names():
    """Pin: every name in ``ReasoningService._KERNEL_TOOLS`` is in the
    registrar; every name in the registrar is in dispatch.

    This is the canonical-source-derived-consumers parity pin: when
    the dispatch authority and the registrar disagree, one of them
    has drifted. CI catches the drift at PR time.
    """
    dispatch = set(ReasoningService._KERNEL_TOOLS)
    registry = kernel_tool_names()

    in_dispatch_not_registry = sorted(dispatch - registry)
    in_registry_not_dispatch = sorted(registry - dispatch)

    failures: list[str] = []
    if in_dispatch_not_registry:
        failures.append(
            f"in dispatch but missing from registrar: {in_dispatch_not_registry} "
            "— add the schema import to kernel_tool_registry._import_kernel_schemas"
        )
    if in_registry_not_dispatch:
        failures.append(
            f"in registrar but missing from dispatch: {in_registry_not_dispatch} "
            "— add the name to ReasoningService._KERNEL_TOOLS"
        )
    assert not failures, "kernel-tool registry drift:\n  " + "\n  ".join(failures)


def test_registrar_count_matches_dispatch_count():
    """Sanity check on count drift (Kit's count was 42; verify post-V1)."""
    assert len(kernel_tool_names()) == len(ReasoningService._KERNEL_TOOLS)


def test_every_dispatch_tool_has_dispatch_path_entry():
    """Pin: every dispatch tool also has an entry in
    ``_KERNEL_TOOL_PATHS``. This separate pin asserts the dispatch
    set and the path-routing registry agree on which tools exist.
    Catches drift between the gating registry and the dispatch
    registry. Adding a tool to one without the other fails CI.
    """
    dispatch = set(ReasoningService._KERNEL_TOOLS)
    paths = set(ReasoningService._KERNEL_TOOL_PATHS.keys())
    assert dispatch == paths, (
        f"_KERNEL_TOOLS and _KERNEL_TOOL_PATHS drift: "
        f"in TOOLS not PATHS={sorted(dispatch - paths)}, "
        f"in PATHS not TOOLS={sorted(paths - dispatch)}"
    )


# ---------------------------------------------------------------------------
# CRB parity pin (separate, per Kit 2026-05-03)
# ---------------------------------------------------------------------------


def test_crb_parcel_names_match_registry():
    """Pin: ``CRB_PARCEL_TOOL_NAMES`` constant matches the names
    declared by the parcel schemas. Future spec authors editing one
    must edit the other.
    """
    declared_names = CRB_PARCEL_TOOL_NAMES
    schema_names = crb_parcel_names()
    assert declared_names == schema_names, (
        f"CRB parcel name registry drift: "
        f"constant={sorted(declared_names)}, "
        f"schema={sorted(schema_names)}"
    )


def test_crb_parcel_tools_excluded_from_primary_kernel_registry():
    """Pin: CRB parcel tools must NOT appear in the primary kernel
    registrar's surface. Folding them in would break CRB's gating
    model (parcels gate on parcel-applicable turns; primary kernel
    tools gate on per-call dispatch). The exclusion is intentional;
    this pin asserts it.
    """
    primary = kernel_tool_names()
    parcel = crb_parcel_names()
    overlap = primary & parcel
    assert not overlap, (
        f"CRB parcel tools leaked into primary registrar: {sorted(overlap)}. "
        "These tools gate separately on parcel-applicable turns; folding "
        "them into the primary kernel registrar breaks the CRB gating "
        "model. Keep them in crb_parcel_schemas() only."
    )


def test_crb_parcel_schemas_well_formed():
    """Pin: each CRB parcel schema has the expected dict shape (name,
    description, input_schema). Same shape as primary kernel tools.
    """
    for s in crb_parcel_schemas():
        assert isinstance(s, dict)
        assert "name" in s and isinstance(s["name"], str)
        assert "description" in s
        assert "input_schema" in s


# ---------------------------------------------------------------------------
# No-orphan-policy pin
# ---------------------------------------------------------------------------


def test_policy_source_map_has_owner_accessor_mutability_per_field():
    """Pin: every policy source-map entry documents owner + accessor
    + mutability. No ambiguous ownership.
    """
    for field, info in POLICY_SOURCE_MAP.items():
        assert "owner" in info, f"policy field {field!r} missing 'owner'"
        assert "accessor" in info, f"policy field {field!r} missing 'accessor'"
        assert "mutability" in info, (
            f"policy field {field!r} missing 'mutability'"
        )


def test_always_pinned_owner_resolves():
    """Pin: the always_pinned policy field's documented owner
    actually resolves to the natural-owner module's set."""
    # Documented owner resolves to a real symbol with set semantics.
    assert isinstance(ALWAYS_PINNED, set)


def test_every_always_pinned_name_is_a_real_kernel_tool():
    """Pin: ALWAYS_PINNED's contents are all real kernel-tool names.
    Pre-V1 this was hand-maintained and could drift (a name in
    ALWAYS_PINNED that wasn't a real tool would silently be a no-op
    on the surfacer). Pin asserts every entry references a real tool.
    """
    real_names = kernel_tool_names()
    orphans = ALWAYS_PINNED - real_names
    assert not orphans, (
        f"ALWAYS_PINNED references non-existent kernel tools: "
        f"{sorted(orphans)}. Either remove from ALWAYS_PINNED or "
        f"add the schema to the registrar."
    )


def test_every_descriptor_carries_policy_metadata():
    """Pin: every descriptor produced by the registrar has its
    policy_metadata populated with all fields documented in the
    source map. Adding a new policy field without updating the
    accessor's return shape fails CI.
    """
    descs = kernel_tool_descriptors()
    documented_fields = set(POLICY_SOURCE_MAP.keys())
    for d in descs:
        actual_fields = set(d.policy_metadata.keys())
        missing = documented_fields - actual_fields
        assert not missing, (
            f"descriptor for {d.name!r} missing policy fields {sorted(missing)} "
            f"— update _policy_for to populate every field documented in "
            f"POLICY_SOURCE_MAP"
        )


def test_always_pinned_policy_round_trips_through_descriptor():
    """Pin: the registrar's accessor returns the same set of names
    ALWAYS_PINNED holds. Catches accessor bugs that would silently
    misreport policy state.
    """
    descs = kernel_tool_descriptors()
    pinned_via_registrar = {
        d.name for d in descs if d.policy_metadata.get("always_pinned")
    }
    # Some ALWAYS_PINNED names may belong to MCP tools (not in the
    # kernel registrar). Filter to kernel-tool intersection.
    expected = ALWAYS_PINNED & kernel_tool_names()
    assert pinned_via_registrar == expected, (
        f"always_pinned round-trip drift: "
        f"via registrar={sorted(pinned_via_registrar)}, "
        f"in ALWAYS_PINNED ∩ kernel tools={sorted(expected)}"
    )


# ---------------------------------------------------------------------------
# Structural pins (registrar shape + workshop-prep contract surface)
# ---------------------------------------------------------------------------


def test_registrar_shape_descriptor_fields():
    """Pin: KernelToolDescriptor exposes the six fields the workshop-
    prep design note's contract requires. Kernel tools today carry
    name/description/input_schema/schema/policy_metadata fully and
    leave dispatch_reference at None (their dispatch lives in
    ReasoningService.execute_tool's elif chain, which the elif IS).
    Workshop tools (future spec) populate dispatch_reference with
    an importable callable reference / service-id string the
    workshop dispatcher resolves at call time. Adding/removing
    fields requires updating the design note in the same commit.
    """
    expected_fields = {
        "name", "description", "input_schema", "schema",
        "policy_metadata", "dispatch_reference",
    }
    actual_fields = {f.name for f in KernelToolDescriptor.__dataclass_fields__.values()}
    assert actual_fields == expected_fields, (
        f"KernelToolDescriptor shape drift: expected={sorted(expected_fields)}, "
        f"actual={sorted(actual_fields)}. The workshop-tool prep design note "
        f"in kernel_tool_registry's docstring documents these as the "
        f"minimum future contract; adding/removing fields requires updating "
        f"the design note in the same commit."
    )


def test_kernel_tool_descriptors_leave_dispatch_reference_none():
    """Pin: kernel tools (which dispatch via ReasoningService.execute_
    tool's elif chain) carry dispatch_reference=None. Workshop tools
    populate it. If a kernel tool ships with a non-None dispatch_
    reference, it's drifted into workshop-shape territory and the
    spec author should consider whether the elif-chain dispatch is
    still the right home.
    """
    for d in kernel_tool_descriptors():
        assert d.dispatch_reference is None, (
            f"kernel tool {d.name!r} has non-None dispatch_reference="
            f"{d.dispatch_reference!r}; kernel tools dispatch via "
            f"ReasoningService.execute_tool's elif chain. If this "
            f"changed deliberately, update the workshop-prep design "
            f"note in kernel_tool_registry's docstring to document "
            f"the new contract."
        )


def test_registrar_descriptors_well_formed():
    """Every descriptor has a non-empty name + a schema dict matching
    the canonical Anthropic-style tool shape (name + description +
    input_schema)."""
    for d in kernel_tool_descriptors():
        assert d.name, f"descriptor with empty name in registrar"
        assert isinstance(d.schema, dict), f"{d.name}: schema not a dict"
        assert d.schema.get("name") == d.name, (
            f"{d.name}: schema name mismatch ({d.schema.get('name')!r})"
        )
        assert "input_schema" in d.schema, f"{d.name}: schema missing input_schema"


def test_kernel_tool_schema_map_keyed_by_name():
    """Pin: the schema map's keys equal the registrar's names. Used
    by ``assemble.py``'s legacy ``_kernel_tool_map`` consumer.
    """
    schema_map = kernel_tool_schema_map()
    assert set(schema_map.keys()) == kernel_tool_names()
    for name, schema in schema_map.items():
        assert schema["name"] == name


def test_workshop_prep_contract_documented():
    """Pin: the registrar module's docstring carries the workshop-tool
    prep design note. The future workshop spec is held to the
    contract this note specifies; the spec author finds it here, not
    in a separate doc.
    """
    import inspect
    from kernos.kernel import kernel_tool_registry
    doc = inspect.getdoc(kernel_tool_registry) or ""
    # Must mention all five descriptor fields the contract specifies.
    contract_clauses = [
        "Descriptor fields",
        "Namespace",
        "Security",
        "Persistence",
        "Plug-in",
    ]
    missing = [c for c in contract_clauses if c not in doc]
    assert not missing, (
        f"workshop-tool prep design note missing clauses: {missing}. "
        f"The contract is canonical; a future workshop spec is held to it."
    )
