"""Canonical kernel-tool registry — KERNEL-TOOL-REGISTRY-V1.

Per Kit's tightened phrasing of the canonical-source-derived-consumers
pattern: schema constants in their owning modules are the canonical
source; this registrar compiles them into one canonical list; consumers
(``MessageHandler._register_kernel_tools_in_catalog``, the legacy
``assemble.py:_all_kernel`` aggregation, and the surfacer LLM input)
derive views from this module.

The fix mechanism the spec exists to address: pre-V1, three hardcoded
registries drifted apart silently. Dispatch authority in
``ReasoningService._KERNEL_TOOLS`` knew 42 tools; the thin-path catalog
hand-maintained 27; the legacy assembly hand-maintained 19. Fifteen
tools dispatched-but-invisible to one or both surfacing paths. The
agent had substrate awareness of canvases / parcels but no callable
tools to act on them. Schema constants already carried name +
description as data; both consumer dicts re-stated the same
information by hand. This module honors the structure that already
exists.

Implementation discipline (Kit caution 2026-05-03):

  - **Explicit schema imports**, NOT blind module walking. Each
    schema lives in its owning module; this registrar imports them
    by name. Filesystem / introspection discovery is deliberately
    avoided — the goal is automatic derivation, not magical
    scanning.
  - **Policy read-through with disciplined facade.** Surfacer-policy
    metadata (always_pinned, etc.) lives at its natural owner
    (``kernos.kernel.tool_catalog.ALWAYS_PINNED``); this registrar
    is the normalized read model. Policy source map below documents
    field → owner → accessor → mutability.
  - **CRB parcels are an explicit architectural exclusion**, not
    drift. Parcel tools gate separately on parcel-applicable turns;
    they have their own parity pin in CI. Kept on a separate path
    by design — see ``crb_parcel_schemas()`` below.

WORKSHOP-TOOL PREP DESIGN NOTE
==============================

The future workshop-tool spec (out of scope here; this is design-
only) plugs into the same registrar / provider interface as kernel-
tool schema exports. Minimum future contract:

* **Descriptor fields:** ``name``, ``description``, ``input_schema``,
  ``dispatch_reference``, ``policy_metadata``. Workshop tools provide
  the same five fields kernel tools provide today; the registrar
  treats both the same way at the read seam.
* **Namespace / collision rules:** workshop tool names share the
  kernel-tool namespace. Collisions reject at registration time
  (workshop registration may not shadow a kernel tool); the registrar
  asserts uniqueness across both surfaces.
* **Security / gate-classification boundary:** workshop tools must
  declare effect class (read / soft_write / hard_write) at
  registration. The dispatch gate enforces at call time per the
  gate-at-dispatch principle (the registration-time declaration is
  metadata, not authority).
* **Persistence / home-space expectation:** workshop descriptors
  persist instance-scoped (per-instance workshop directory). On
  instance restart, the registrar enumerates persisted descriptors
  alongside kernel-tool schema exports.
* **Plug-in interface:** workshop descriptors implement the same
  ``KernelToolDescriptor`` shape via ``register_workshop_tool(...)``
  (future). The registrar's ``kernel_tool_descriptors()`` returns a
  unified list; consumers see one canonical surface regardless of
  origin.

Don't overbuild this; the future spec implements. This note is the
contract that future spec is held to.

POLICY SOURCE MAP
=================

| Field            | Owner                                                 | Accessor          | Mutability                   |
|------------------|-------------------------------------------------------|-------------------|------------------------------|
| always_pinned    | ``kernos.kernel.tool_catalog.ALWAYS_PINNED``          | ``_policy_for``   | stable (module-load constant)|

Adding a new policy field: pick the natural owner module, add a
read-through accessor here, document the row above, add a parity-
pin entry asserting "no orphan policy" (every entry references a
real tool name; every tool with policy has it accessible here).
"""

from __future__ import annotations

import dataclasses
from typing import Any


# ---------------------------------------------------------------------------
# Schema imports (explicit; not module-walking)
# ---------------------------------------------------------------------------


def _import_kernel_schemas() -> list[dict]:
    """Return the canonical list of kernel-tool schema dicts.

    Imports are inline so circular-import edges don't fire at module
    load. Each schema is the schema dict registered in its owning
    module.

    Per Kit caution 2026-05-03: the imports below are explicit by
    name. Filesystem discovery is intentionally avoided.
    """
    # Tools collected in the central schemas module.
    # READ_DOC_TOOL retired in REFERENCE-PRIMITIVE-V1; see
    # kernos.kernel.reference.tools for the replacement surface.
    from kernos.kernel.tools.schemas import (
        CANVAS_LIST_TOOL,
        CANVAS_CREATE_TOOL,
        PAGE_READ_TOOL,
        PAGE_WRITE_TOOL,
        PAGE_LIST_TOOL,
        PAGE_SEARCH_TOOL,
        CANVAS_PREFERENCE_EXTRACT_TOOL,
        CANVAS_PREFERENCE_CONFIRM_TOOL,
        REQUEST_TOOL,
        REMEMBER_DETAILS_TOOL,
        MANAGE_CAPABILITIES_TOOL,
        READ_SOURCE_TOOL,
        READ_SOUL_TOOL,
        UPDATE_SOUL_TOOL,
        INSPECT_STATE_TOOL,
        SET_CHAIN_MODEL_TOOL,
        DIAGNOSE_MESSENGER_TOOL,
        DIAGNOSE_LLM_CHAIN_TOOL,
    )
    # Tools that live next to their owning module.
    from kernos.kernel.awareness import DISMISS_WHISPER_TOOL
    from kernos.kernel.channels import MANAGE_CHANNELS_TOOL, SEND_TO_CHANNEL_TOOL
    from kernos.kernel.code_exec import EXECUTE_CODE_TOOL
    from kernos.kernel.covenant_manager import MANAGE_COVENANTS_TOOL
    from kernos.kernel.cross_space.tool import REQUEST_SPACE_ACTION_TOOL
    from kernos.kernel.diagnostics import (
        DIAGNOSE_ISSUE_TOOL,
        PROPOSE_FIX_TOOL,
        SUBMIT_SPEC_TOOL,
    )
    from kernos.kernel.execution import MANAGE_PLAN_TOOL
    from kernos.kernel.external_agents.tool import CONSULT_TOOL
    from kernos.kernel.files import FILE_TOOLS
    from kernos.kernel.members import MANAGE_MEMBERS_TOOL
    from kernos.kernel.note_this import NOTE_THIS_TOOL
    from kernos.kernel.relational_tools import (
        SEND_RELATIONAL_MESSAGE_TOOL,
        RESOLVE_RELATIONAL_MESSAGE_TOOL,
    )
    from kernos.kernel.retrieval import REMEMBER_TOOL
    from kernos.kernel.runtime_trace import READ_RUNTIME_TRACE_TOOL
    from kernos.kernel.scheduler import MANAGE_SCHEDULE_TOOL
    from kernos.kernel.reference.tools import REFERENCE_TOOL_SCHEMAS
    from kernos.kernel.workspace import MANAGE_WORKSPACE_TOOL, REGISTER_TOOL_TOOL

    schemas: list[dict] = []
    # File-system tools (write_file, read_file, list_files, delete_file)
    # ship as a list constant in their owning module.
    schemas.extend(FILE_TOOLS)

    schemas.extend([
        # Canvas surface (CANVAS-V1)
        CANVAS_LIST_TOOL,
        CANVAS_CREATE_TOOL,
        PAGE_READ_TOOL,
        PAGE_WRITE_TOOL,
        PAGE_LIST_TOOL,
        PAGE_SEARCH_TOOL,
        CANVAS_PREFERENCE_EXTRACT_TOOL,
        CANVAS_PREFERENCE_CONFIRM_TOOL,
        # Memory + retrieval
        REMEMBER_TOOL,
        REMEMBER_DETAILS_TOOL,
        # RESPONSE-FIDELITY-V1 Batch 1.2: synchronous receipt-backed
        # memory path (resolves G.1 from the Phase 1 audit).
        NOTE_THIS_TOOL,
        # Awareness + capabilities
        DISMISS_WHISPER_TOOL,
        MANAGE_CAPABILITIES_TOOL,
        REQUEST_TOOL,
        # Substrate introspection (read_doc retired —
        # REFERENCE-PRIMITIVE-V1 ships request_reference instead)
        READ_SOURCE_TOOL,
        READ_SOUL_TOOL,
        UPDATE_SOUL_TOOL,
        INSPECT_STATE_TOOL,
        # Covenants + member relations
        MANAGE_COVENANTS_TOOL,
        MANAGE_MEMBERS_TOOL,
        SEND_RELATIONAL_MESSAGE_TOOL,
        RESOLVE_RELATIONAL_MESSAGE_TOOL,
        # Channels + scheduling
        MANAGE_CHANNELS_TOOL,
        SEND_TO_CHANNEL_TOOL,
        MANAGE_SCHEDULE_TOOL,
        # Workshop / execution
        EXECUTE_CODE_TOOL,
        MANAGE_WORKSPACE_TOOL,
        REGISTER_TOOL_TOOL,
        MANAGE_PLAN_TOOL,
        # Diagnostics
        READ_RUNTIME_TRACE_TOOL,
        DIAGNOSE_ISSUE_TOOL,
        PROPOSE_FIX_TOOL,
        SUBMIT_SPEC_TOOL,
        SET_CHAIN_MODEL_TOOL,
        DIAGNOSE_MESSENGER_TOOL,
        DIAGNOSE_LLM_CHAIN_TOOL,
        # Cross-space + external
        REQUEST_SPACE_ACTION_TOOL,
        CONSULT_TOOL,
    ])
    # REFERENCE-PRIMITIVE-V1 — seven new tools (request_reference,
    # store_reference, create_reference_collection + four recovery
    # primitives). Schemas live in their owning module.
    schemas.extend(REFERENCE_TOOL_SCHEMAS)
    return schemas


# ---------------------------------------------------------------------------
# Descriptor + accessors
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class KernelToolDescriptor:
    """Canonical kernel-tool descriptor.

    Six fields per the workshop-tool prep design note's contract:

      - ``name`` / ``description`` / ``input_schema`` / ``policy_metadata``
        carry the surface every consumer reads.
      - ``schema`` carries the full original schema dict (Anthropic-
        style ``{name, description, input_schema}``) so consumers
        that pass the schema verbatim into the LLM tool call have
        the source-of-truth dict, not a derived view.
      - ``dispatch_reference`` is the workshop-tool contract field.
        For kernel tools it is ``None`` because dispatch is encoded
        in ``ReasoningService.execute_tool``'s elif chain (the elif
        chain IS the dispatch reference; encoding it as a string
        here would be lossy). For workshop tools (future spec) it
        carries an importable callable reference or service-id
        string the workshop dispatcher resolves at call time.

    Codex review (2026-05-04) caught the mismatch where the design
    note listed ``dispatch_reference`` but the dataclass omitted it;
    field added so kernel and workshop tools share the exact contract.
    """

    name: str
    description: str
    input_schema: dict
    schema: dict  # full original schema dict (Anthropic / OpenAI tool shape)
    policy_metadata: dict  # always_pinned, future fields
    dispatch_reference: Any = None  # None for kernel tools (elif-chain dispatch); populated for workshop tools


def kernel_tool_schemas() -> list[dict]:
    """Return the canonical list of kernel-tool schema dicts."""
    return _import_kernel_schemas()


def kernel_tool_names() -> set[str]:
    """Return the canonical set of kernel-tool names."""
    return {s["name"] for s in kernel_tool_schemas()}


def kernel_tool_descriptors() -> list[KernelToolDescriptor]:
    """Return the canonical list of descriptors with policy metadata.

    Consumers that need (name, description, schema, policy) tuples
    iterate this list. The handler catalog registration calls it; the
    legacy assembly aggregation calls it; the surfacer LLM input
    feeds from the same canonical surface.
    """
    return [
        KernelToolDescriptor(
            name=s["name"],
            description=s.get("description", ""),
            input_schema=s.get("input_schema", {}),
            schema=s,
            policy_metadata=_policy_for(s["name"]),
        )
        for s in kernel_tool_schemas()
    ]


def kernel_tool_schema_map() -> dict[str, dict]:
    """Return name → schema dict, the shape legacy assembly built by hand.

    Convenience for ``assemble.py``'s ``_kernel_tool_map`` consumer
    that wants O(1) lookup.
    """
    return {s["name"]: s for s in kernel_tool_schemas()}


# ---------------------------------------------------------------------------
# Policy source map (read-through facade)
# ---------------------------------------------------------------------------


def _policy_for(tool_name: str) -> dict:
    """Read-through accessor — returns policy metadata for a tool.

    Per the policy source map (in this module's docstring): policy
    fields live at their natural owner. This accessor reads from
    those owners and returns a normalized dict for the registrar's
    consumers.

    Adding a new policy field:
      1. Pick the natural owner module (where the policy is
         authoritative).
      2. Read-through here — single accessor per field, no
         duplication of policy state.
      3. Document the row in the policy source map.
      4. Update the no-orphan-policy parity pin to assert the new
         field's owner / mutability.
    """
    from kernos.kernel.tool_catalog import ALWAYS_PINNED

    return {
        "always_pinned": tool_name in ALWAYS_PINNED,
    }


# Documented policy source map. Tests assert this matches the
# accessor's actual behavior + the natural owner's contents.
POLICY_SOURCE_MAP: dict[str, dict[str, str]] = {
    "always_pinned": {
        "owner": "kernos.kernel.tool_catalog.ALWAYS_PINNED",
        "accessor": "_policy_for",
        "mutability": "stable (module-load constant; set at import time)",
    },
}


# ---------------------------------------------------------------------------
# CRB parcel tools — explicit exclusion (Kit 2026-05-03)
# ---------------------------------------------------------------------------


# CRB parcel tools gate on parcel-applicable turns rather than via
# the dispatch set in reasoning.py. Intentional architectural
# exclusion, not drift. These names are excluded from the primary
# parity check and have their own separate parity pin (CI asserts
# the CRB-gated tool set equals this registry's parcel surface
# during parcel-applicable turns).
#
# Future spec authors: do NOT fold these into the primary kernel
# registrar. Doing so breaks CRB's gating model. Adding a new
# parcel-style tool: extend ``crb_parcel_schemas`` below; the
# separate parity pin will catch drift.
CRB_PARCEL_TOOL_NAMES: frozenset[str] = frozenset({
    "pack_parcel",
    "respond_to_parcel",
    "list_parcels",
    "inspect_parcel",
})


def crb_parcel_schemas() -> list[dict]:
    """Return the canonical list of CRB parcel-tool schemas.

    Separate registry from primary kernel tools by design (see
    module docstring). Parcel tools gate on parcel-applicable turns;
    the cohort runner's parcel surface dispatches them. Pin test
    asserts this set matches the names declared in
    ``CRB_PARCEL_TOOL_NAMES``.
    """
    from kernos.kernel.tools.schemas import (
        PACK_PARCEL_TOOL,
        RESPOND_TO_PARCEL_TOOL,
        LIST_PARCELS_TOOL,
        INSPECT_PARCEL_TOOL,
    )
    return [
        PACK_PARCEL_TOOL,
        RESPOND_TO_PARCEL_TOOL,
        LIST_PARCELS_TOOL,
        INSPECT_PARCEL_TOOL,
    ]


def crb_parcel_names() -> set[str]:
    """Return the canonical set of CRB parcel-tool names."""
    return {s["name"] for s in crb_parcel_schemas()}


__all__ = [
    "KernelToolDescriptor",
    "POLICY_SOURCE_MAP",
    "CRB_PARCEL_TOOL_NAMES",
    "kernel_tool_schemas",
    "kernel_tool_names",
    "kernel_tool_descriptors",
    "kernel_tool_schema_map",
    "crb_parcel_schemas",
    "crb_parcel_names",
]
