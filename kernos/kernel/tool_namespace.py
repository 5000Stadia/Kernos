"""Tool namespace map (SEMANTIC-ACTION-ENVELOPE-V1 Phase 1).

The model's whole environment is namespaced (provider meta-tools, MCP
``mcp__server__tool``, capability-domain groupings), so it reliably reaches for
a ``domain.verb`` shape. Rather than fight that with a flat catalog + repair
forever, we present kernel tools in a namespaced ``area__tool`` form (provider-
valid; ``.`` is rejected by function-name regexes, ``__`` matches the MCP
convention). See specs/SEMANTIC-ACTION-ENVELOPE-V1.md.

This module is the single source of truth for the kernel-tool → namespace
mapping. It is a PRESENTATION concern only: the flat name stays the canonical
internal id everywhere (gate, receipts, dispatch, tests). The namespaced string
is what the provider sees; inbound namespaced tool_calls canonicalize back to
the flat name via :func:`kernos.kernel.tool_aliases.canonicalize_tool_name`
(which strips a known ``area__`` / ``area.`` prefix when the suffix is a real
tool).

Separator is ``__`` (double underscore): provider-valid and visually identical
to the MCP tools the model already sees. MCP/connector and agent-built workshop
tools are NOT namespaced here — they keep their registered names.
"""
from __future__ import annotations

# namespace -> kernel tools it owns (single-owner). Keep in sync with
# ReasoningService._KERNEL_TOOLS — test_tool_namespace_complete guards drift.
_NAMESPACE_TOOLS: dict[str, tuple[str, ...]] = {
    "files":       ("write_file", "read_file", "list_files", "delete_file",
                    "read_source"),
    "memory":      ("remember", "remember_details", "note_this",
                    "dismiss_whisper"),
    "references":  ("request_reference", "store_reference",
                    "create_reference_collection", "move_reference_to_canvas",
                    "mark_reference_superseded", "quarantine_reference",
                    "restore_reference_from_quarantine"),
    "self":        ("read_soul", "update_soul", "inspect_state",
                    "inspect_tools", "dump_context", "restart_self"),
    "covenants":   ("manage_covenants",),
    "capabilities": ("manage_capabilities", "request_tool"),
    "channels":    ("manage_channels", "send_to_channel"),
    "schedule":    ("manage_schedule",),
    "projects":    ("start_project", "record_project_decision",
                    "surface_project_status"),
    "workspace":   ("manage_workspace", "execute_code", "register_tool"),
    "planning":    ("manage_plan",),
    "members":     ("manage_members", "send_relational_message",
                    "resolve_relational_message"),
    "canvas":      ("canvas_list", "canvas_create", "page_read", "page_write",
                    "page_list", "page_search", "canvas_preference_extract",
                    "canvas_preference_confirm"),
    "external":    ("consult", "ask_coding_session",
                    "read_coding_session_response"),
    "spaces":      ("request_space_action",),
    "diagnostics": ("read_runtime_trace", "diagnose_issue", "propose_fix",
                    "submit_spec", "set_chain_model", "diagnose_llm_chain",
                    "diagnose_messenger"),
    "improvement": ("improve_kernos", "run_self_test_suite", "run_self_review",
                    "proceed_with_recovery", "abandon_attempt",
                    "record_closure_attempt", "run_closure_probe",
                    "lookup_pattern_invariants", "record_fix_authorization",
                    "classify_proposed_fix", "validate_investigation_response",
                    "maybe_run_closure_for_fix"),
    "git":         ("git_fetch", "git_rev_parse", "git_status",
                    "git_diff_for_review", "git_commit", "git_push"),
    "surfacing":   ("surface_to_user",),
}

SEPARATOR = "__"

#: flat tool name -> namespace
NAMESPACE_FOR: dict[str, str] = {
    tool: ns for ns, tools in _NAMESPACE_TOOLS.items() for tool in tools
}

#: all namespace prefixes (for parsing/validation)
NAMESPACES: frozenset[str] = frozenset(_NAMESPACE_TOOLS.keys())


def to_namespaced(flat_name: str) -> str:
    """Return the provider-facing ``area__tool`` name for a kernel tool.

    Tools without a namespace mapping (MCP/connector, agent-built workshop
    tools) are returned unchanged — they keep their registered names.
    """
    ns = NAMESPACE_FOR.get(flat_name)
    return f"{ns}{SEPARATOR}{flat_name}" if ns else flat_name


def build_skin_maps(
    tools: "list[dict] | tuple[dict, ...]",
) -> tuple[dict[str, str], dict[str, str]]:
    """Build request-local (skin, unskin) maps from a flat tools list.

    ``skin``   : flat tool id  -> provider wire name (``area__tool`` for kernel
                 tools, identity for MCP/workshop tools).
    ``unskin`` : provider wire name -> flat tool id, ONLY for names that were
                 actually namespaced. Used for EXACT reverse lookup on inbound
                 tool calls (more reliable than suffix-stripping, which is
                 ambiguous for shapes like ``area__mcp__tool``).

    Per SEMANTIC-ACTION-ENVELOPE-V1 (Codex review, option A): the skin lives at
    the provider boundary only; every internal surface stays flat.
    """
    skin: dict[str, str] = {}
    unskin: dict[str, str] = {}
    for t in tools:
        flat = t.get("name", "") if isinstance(t, dict) else ""
        if not flat:
            continue
        wire = to_namespaced(flat)
        skin[flat] = wire
        if wire != flat:
            unskin[wire] = flat
    return skin, unskin


__all__ = [
    "NAMESPACE_FOR", "NAMESPACES", "SEPARATOR",
    "to_namespaced", "build_skin_maps",
]
