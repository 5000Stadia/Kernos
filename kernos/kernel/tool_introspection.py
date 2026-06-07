"""Tool introspection — two-surface design (operator + agent).

TOOL-INTROSPECTION-V1 (2026-05-22).

Per [[agent-facing-natural-simplicity]]: same catalog metadata,
two surfaces shaped for two audiences.

  - **Operator** uses ``/tools`` slash command (in handler.py)
    for structured tabular text. Sophistication intact —
    operators do parse this and need the richness.
  - **Agent** uses ``inspect_tools`` kernel tool for natural-
    prose responses. Sentences the agent can naturally use or
    relay; substrate composes the prose from the catalog
    metadata so the agent never touches structured data.

Both share the same underlying ``ToolCatalog`` metadata; the
difference is purely in the rendering layer.
"""
from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


# NOTE: inspect_tools renders FLAT names (canonical catalog ids), NOT the
# area__tool wire form. The namespace skin is a Codex-provider wire concern
# only; rendering namespaced here would advertise names absent from a flat-list
# provider's (Anthropic/Ollama) tool set, and on Codex the model already calls
# from its namespaced function LIST — this prose is guidance, and flat names
# dispatch on every provider. (SAE-V1; Codex review r5.) The focus path still
# accepts a namespaced arg via _flatten_focus, so a model copying a namespaced
# name from its Codex list still resolves.


# ---------------------------------------------------------------------
# Capability area heuristic
# ---------------------------------------------------------------------
#
# Tool-name → area mapping for grouping. Heuristic, not declarative
# (would require descriptor extension). Per spec: tools that don't
# match a known area land in "other substrate tools" rather than
# crashing.


_AREA_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("memory",       ("remember", "remember_details", "note_this",
                      "request_reference", "store_reference",
                      "create_reference_collection",
                      "move_reference_to_canvas",
                      "mark_reference_superseded",
                      "quarantine_reference",
                      "restore_reference_from_quarantine")),
    ("files",        ("read_file", "write_file", "list_files",
                      "delete_file", "read_source")),
    ("conversation", ("send_to_channel", "send_relational_message",
                      "resolve_relational_message",
                      "ask_coding_session", "read_coding_session_response",
                      "dismiss_whisper", "consult", "request_space_action")),
    ("canvases",     ("canvas_list", "canvas_create", "page_read",
                      "page_write", "page_list", "page_search",
                      "canvas_preference_extract",
                      "canvas_preference_confirm")),
    ("scheduling",   ("manage_schedule",)),
    ("substrate",    ("inspect_state", "manage_covenants",
                      "manage_capabilities", "manage_channels",
                      "manage_members", "manage_workspace",
                      "manage_plan", "register_tool", "request_tool",
                      "execute_code", "read_soul", "update_soul",
                      "diagnose_issue", "propose_fix", "submit_spec",
                      "set_chain_model", "diagnose_llm_chain",
                      "diagnose_messenger", "read_runtime_trace",
                      "dump_context", "restart_self",
                      "inspect_tools")),
]


def _area_for(tool_name: str) -> str:
    for area, names in _AREA_RULES:
        if tool_name in names:
            return area
    return "other"


def _group_by_area(catalog: Any) -> dict[str, list[Any]]:
    groups: dict[str, list[Any]] = {}
    if catalog is None:
        return groups
    try:
        entries = catalog.get_all()
    except Exception:
        return groups
    for entry in entries:
        area = _area_for(entry.name)
        groups.setdefault(area, []).append(entry)
    return groups


# ---------------------------------------------------------------------
# Agent-facing prose composer
# ---------------------------------------------------------------------


_AREA_ORDER = (
    "memory", "files", "conversation", "canvases",
    "scheduling", "substrate", "other",
)

_AREA_LABELS = {
    "memory":       "memory & reference",
    "files":        "files",
    "conversation": "conversation & coordination",
    "canvases":     "canvases & pages",
    "scheduling":   "scheduling",
    "substrate":    "substrate controls",
    "other":        "other substrate tools",
}


def compose_overview(catalog: Any) -> str:
    """Overview prose for ``inspect_tools()`` with no focus."""
    groups = _group_by_area(catalog)
    total = sum(len(v) for v in groups.values())
    if total == 0:
        return (
            "No tools registered yet — register one via "
            "`register_tool` once you've built it."
        )

    parts: list[str] = []
    parts.append(
        f"You have access to {total} tools across "
        f"{len(groups)} areas."
    )
    for area in _AREA_ORDER:
        if area not in groups:
            continue
        names = sorted(e.name for e in groups[area])
        if not names:
            continue
        # Sample up to 5 names per area to keep prose terse. Flat names.
        sample = ", ".join(f"`{n}`" for n in names[:5])
        extra = (
            f" plus {len(names) - 5} more"
            if len(names) > 5 else ""
        )
        parts.append(
            f"For {_AREA_LABELS[area]}: {sample}{extra}."
        )
    parts.append(
        "Pass `focus=\"tool_name\"` for details on a specific one, "
        "or `capability=\"area\"` to scope to one area."
    )
    return " ".join(parts)


def _flatten_focus(tool_name: str) -> str:
    """Strip a presented ``area__tool`` namespace back to the flat catalog id
    so a model that copied a namespaced name from the surfaced list still
    resolves (SEMANTIC-ACTION-ENVELOPE-V1)."""
    from kernos.kernel.tool_namespace import NAMESPACES, SEPARATOR
    if SEPARATOR in tool_name:
        head, _, tail = tool_name.partition(SEPARATOR)
        if head in NAMESPACES and tail:
            return tail
    return tool_name


def _get_meta(catalog: Any, name: str) -> Any:
    try:
        get_meta = getattr(catalog, "get_metadata", None)
        if callable(get_meta):
            return get_meta(name)
    except Exception:
        return None
    return None


def compose_focus(catalog: Any, tool_name: str) -> str:
    """Focused prose for ``inspect_tools(focus="X")``."""
    if catalog is None:
        return (
            f"No catalog wired into the substrate, so `{tool_name}` "
            f"can't be checked here."
        )
    # Try the EXACT name first so a real non-kernel tool whose catalog name
    # happens to start with a namespace prefix (e.g. a workshop tool named
    # `files__summarize_csv`) still resolves. Only flatten an area__tool
    # presentation name on a miss (SAE-V1; Codex review P3).
    meta = _get_meta(catalog, tool_name)
    if meta is None:
        _flat = _flatten_focus(tool_name)
        if _flat != tool_name:
            _flat_meta = _get_meta(catalog, _flat)
            if _flat_meta is not None:
                tool_name, meta = _flat, _flat_meta

    if meta is None:
        return (
            f"`{tool_name}` isn't in the catalog. If it's a "
            f"workspace tool you want to build, use `register_tool`. "
            f"If it's a connected-service capability, use "
            f"`request_tool` (MCP capabilities only)."
        )

    source = meta.get("source") or "unknown"
    desc = meta.get("description") or "(no description registered)"
    service_id = meta.get("service_id") or ""

    sentences = [f"`{tool_name}` is a `{source}` tool. {desc}"]
    if service_id:
        sentences.append(
            f"It's bound to the {service_id!r} external service."
        )
    if meta.get("registered_at"):
        sentences.append(
            f"Registered at {meta['registered_at']}."
        )
    if meta.get("home_space"):
        sentences.append(
            f"Its data lives in space `{meta['home_space']}`."
        )
    return " ".join(sentences)


def compose_capability(catalog: Any, capability: str) -> str:
    """Capability-scoped prose for ``inspect_tools(capability="X")``."""
    capability = (capability or "").strip().lower()
    if not capability:
        return compose_overview(catalog)
    # Resolve capability → area (accept exact area name or some
    # natural synonyms).
    synonyms = {
        "calendar": "scheduling", "schedule": "scheduling",
        "file": "files", "fs": "files",
        "message": "conversation", "messaging": "conversation",
        "chat": "conversation",
        "canvas": "canvases", "page": "canvases",
        "memory": "memory", "reference": "memory",
        "admin": "substrate", "manage": "substrate",
    }
    area = synonyms.get(capability, capability)
    groups = _group_by_area(catalog)
    if area not in groups:
        return (
            f"No tools tagged under `{capability}` right now. "
            f"Try `inspect_tools()` for the full overview."
        )
    names = sorted(e.name for e in groups[area])
    listing = ", ".join(f"`{n}`" for n in names)
    return (
        f"For {_AREA_LABELS.get(area, area)}: {listing}. "
        f"Pass `focus=\"tool_name\"` for details on a specific one."
    )


def handle_inspect_tools(
    *, catalog: Any, focus: str = "", capability: str = "",
) -> str:
    """Entry-point: route to overview / focused / capability prose
    based on which (if any) parameter is set."""
    focus = (focus or "").strip()
    capability = (capability or "").strip()
    if focus:
        return compose_focus(catalog, focus)
    if capability:
        return compose_capability(catalog, capability)
    return compose_overview(catalog)


INSPECT_TOOLS_TOOL: dict = {
    "name": "inspect_tools",
    "description": (
        "See what tools you have access to right now, grouped by "
        "capability area. Returns natural-prose descriptions, not "
        "raw catalog data. Pass `focus=\"tool_name\"` for a "
        "specific tool's details (purpose, source, status), or "
        "`capability=\"area\"` to scope to an area like "
        "\"calendar\", \"files\", \"canvases\", \"memory\". With "
        "no parameters: returns the overview."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "focus": {
                "type": "string",
                "description": (
                    "Exact tool name to get details on. Leave empty "
                    "for the overview."
                ),
            },
            "capability": {
                "type": "string",
                "description": (
                    "Capability area to scope to. Natural names like "
                    "\"calendar\", \"files\", \"canvases\", \"memory\", "
                    "\"messaging\" work. Leave empty for the overview."
                ),
            },
        },
    },
}


# ---------------------------------------------------------------------
# Operator-facing structured listing (for /tools slash command)
# ---------------------------------------------------------------------


def render_operator_listing(
    catalog: Any, *,
    filter_source: str = "",
    filter_classification: str = "",
    filter_status: str = "",
) -> str:
    """Structured tabular text for ``/tools`` slash command.

    Operator audience — sophisticated, dense, all the metadata.
    Filters narrow the listing when supplied.
    """
    if catalog is None:
        return "No catalog wired."
    try:
        entries = catalog.get_all()
    except Exception:
        return "Catalog read failed."
    if not entries:
        return "Catalog is empty."

    # Apply filters
    def _matches(entry: Any) -> bool:
        if filter_source and (entry.source or "") != filter_source:
            return False
        # status / classification filters require metadata; skip
        # entries that don't carry them.
        if filter_classification:
            # We don't carry gate_classification on the entry yet
            # (would need TOOL-MAKING-ARC's descriptor work).
            # For v1, classification filter is best-effort: match
            # nothing rather than crash.
            return False
        if filter_status:
            # No status field today; reserve for future.
            return False
        return True

    filtered = [e for e in entries if _matches(e)]
    if not filtered:
        return "No tools match the filter."

    # Group by source for the listing
    groups: dict[str, list[Any]] = {}
    for e in filtered:
        groups.setdefault(e.source or "unknown", []).append(e)

    lines: list[str] = []
    for source in sorted(groups.keys()):
        lines.append(f"## {source} ({len(groups[source])})")
        for e in sorted(groups[source], key=lambda x: x.name):
            extras: list[str] = []
            if getattr(e, "service_id", ""):
                extras.append(f"service={e.service_id}")
            if getattr(e, "registration_hash", ""):
                extras.append(f"hash={e.registration_hash[:12]}")
            if getattr(e, "home_space", ""):
                extras.append(f"space={e.home_space}")
            extra_text = (
                " (" + ", ".join(extras) + ")" if extras else ""
            )
            # Operator surface stays FLAT — /tools detail is flat-keyed, and
            # the namespace skin is an agent-/model-facing concern only
            # (SAE-V1; Codex review P2). Operators see catalog reality.
            lines.append(f"- `{e.name}`{extra_text}")
            if e.description:
                lines.append(f"  _{e.description[:120]}_")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_operator_detail(catalog: Any, tool_name: str) -> str:
    """Detail view for ``/tools <name>``."""
    if catalog is None:
        return "No catalog wired."
    meta = None
    try:
        get_meta = getattr(catalog, "get_metadata", None)
        if callable(get_meta):
            meta = get_meta(tool_name)
    except Exception:
        meta = None
    if meta is None:
        return (
            f"`{tool_name}` not found in catalog. Use `/tools` "
            f"(no args) to list all registered tools."
        )
    lines: list[str] = [
        f"# `{tool_name}`",
        f"- source: `{meta.get('source') or 'unknown'}`",
    ]
    if meta.get("description"):
        lines.append(f"- description: {meta['description']}")
    if meta.get("service_id"):
        lines.append(f"- service_id: `{meta['service_id']}`")
    if meta.get("registration_hash"):
        lines.append(
            f"- registration_hash: `{meta['registration_hash']}`"
        )
    if meta.get("descriptor_file"):
        lines.append(
            f"- descriptor_file: `{meta['descriptor_file']}`"
        )
    if meta.get("home_space"):
        lines.append(f"- home_space: `{meta['home_space']}`")
    if meta.get("registered_at"):
        lines.append(f"- registered_at: {meta['registered_at']}")
    if meta.get("force_registered"):
        lines.append("- force_registered: true (author bypassed validation)")
    return "\n".join(lines)
