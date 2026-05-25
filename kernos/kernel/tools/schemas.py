"""Kernel tool JSON schemas and pure helper functions."""

# ---------------------------------------------------------------------------
# CANVAS-V1: shared-state primitive (scoped directories of markdown pages)
# ---------------------------------------------------------------------------

CANVAS_LIST_TOOL = {
    "name": "canvas_list",
    "description": (
        "List canvases accessible to the calling member. Returns a list of "
        "{canvas_id, name, scope, owner_member_id, pinned_to_spaces, last_updated}. "
        "Out-of-scope canvases do not appear — the member cannot see they exist."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "include_archived": {
                "type": "boolean",
                "description": "If true, include archived canvases. Default false.",
            },
        },
    },
}


CANVAS_CREATE_TOOL = {
    "name": "canvas_create",
    "description": (
        "Create a new canvas: a named directory of markdown pages with "
        "YAML frontmatter. Three scopes: 'personal' (caller only), "
        "'specific' (an explicit member list), 'team' (all instance "
        "members current and future). Caller becomes owner. "
        "For scope='specific', the members list is required. "
        "If 'intent' is provided, the Gardener cohort will asynchronously "
        "match the intent to a workflow pattern and instantiate that "
        "pattern's initial pages — the canvas is returned immediately "
        "while pages populate in the background."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Human-readable canvas name.",
            },
            "scope": {
                "type": "string",
                "enum": ["personal", "specific", "team"],
                "description": "Visibility tier. Fixed at creation.",
            },
            "members": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Required for scope='specific'. Explicit member_id list. "
                    "Caller is auto-included."
                ),
            },
            "description": {
                "type": "string",
                "description": "Short description seeded into index.md.",
            },
            "default_page_type": {
                "type": "string",
                "enum": ["note", "decision", "log"],
                "description": "Default page type for new pages. Default 'note'.",
            },
            "pinned_to_spaces": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of space_ids. If set, the canvas appears "
                    "in the Available Canvases zone ONLY when operating in "
                    "those spaces. Unset = universal visibility."
                ),
            },
            "intent": {
                "type": "string",
                "description": (
                    "Natural-language description of what the canvas is for. "
                    "Used by the Gardener to match against workflow patterns "
                    "for initial-shape instantiation. Optional; omit to skip "
                    "Gardener matching and create a minimal canvas."
                ),
            },
            "pattern": {
                "type": "string",
                "description": (
                    "Optional explicit pattern name (e.g. 'software-development', "
                    "'long-form-campaign') that bypasses Gardener matching. Use "
                    "when you already know which pattern fits the work."
                ),
            },
        },
        "required": ["name", "scope"],
    },
}


PAGE_READ_TOOL = {
    "name": "page_read",
    "description": (
        "Read a page's body + frontmatter from a canvas. Returns "
        "{frontmatter, body}. Page paths are relative to the canvas "
        "(e.g., 'index.md', 'decisions/launch.md')."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "canvas_id": {"type": "string"},
            "page_path": {
                "type": "string",
                "description": "Relative page path. '.md' optional.",
            },
        },
        "required": ["canvas_id", "page_path"],
    },
}


PAGE_WRITE_TOOL = {
    "name": "page_write",
    "description": (
        "Create or update a page in a canvas. Reversible: old versions "
        "retained as '{slug}.v{N}.md' files. For cross-member canvases, "
        "a page_write to a shared (non-log) page without confirmed=true "
        "returns requires_confirmation=true and does NOT write — the "
        "agent must surface to the user and re-call with confirmed=true.\n\n"
        "Cross-page references: when your page body mentions another page "
        "in the same canvas, use explicit wiki-link syntax [[page-path]] "
        "(without the .md extension, e.g. [[specs/launch]] or [[charter]]). "
        "These links feed the canvas's reference index, which drives "
        "structural heuristics (back-reference-based index-hub promotion, "
        "broken-link detection). Bare prose mentions of a page name are "
        "not recognized — be explicit."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "canvas_id": {"type": "string"},
            "page_path": {"type": "string"},
            "body": {
                "type": "string",
                "description": "Markdown body (without the '---' frontmatter fences).",
            },
            "title": {"type": "string"},
            "page_type": {
                "type": "string",
                "enum": ["note", "decision", "log"],
            },
            "state": {
                "type": "string",
                "description": (
                    "Optional state for this page. Advisory; type-specific "
                    "vocabularies: note (drafted/current/archived), "
                    "decision (proposed/ratified/superseded), log (none)."
                ),
            },
            "confirmed": {
                "type": "boolean",
                "description": (
                    "Required true when writing to a cross-member "
                    "(non-log) page in a shared canvas. Signals the user "
                    "has explicitly approved this write."
                ),
            },
        },
        "required": ["canvas_id", "page_path", "body"],
    },
}


PAGE_LIST_TOOL = {
    "name": "page_list",
    "description": "Enumerate pages in a canvas. Returns {path, title, type, state, last_updated} per page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "canvas_id": {"type": "string"},
        },
        "required": ["canvas_id"],
    },
}


CANVAS_PREFERENCE_EXTRACT_TOOL = {
    "name": "canvas_preference_extract",
    "description": (
        "Extract a canvas-behavior preference from a member utterance on a "
        "canvas that has a declared pattern. Runs the Gardener's "
        "preference-extraction consultation; if the utterance maps cleanly "
        "to a suppression-class or threshold-class effect at high confidence, "
        "the preference lands in the canvas's pending_preferences list and "
        "the tool returns the extracted shape for you to surface to the user "
        "for explicit confirmation. Preferences whose effect isn't wired in "
        "v1 (routing, scope, authority delegation) silently no-op — the "
        "utterance flows through the normal covenant/standing-order path. "
        "Also no-ops on a canvas without a declared pattern, or on neutral "
        "utterances that don't match the pattern's intent-hook vocabulary."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "canvas_id": {"type": "string"},
            "utterance": {
                "type": "string",
                "description": (
                    "The member's verbatim utterance — not a paraphrase. "
                    "The consultation validates subject matter; passing a "
                    "covenant-shaped utterance (agent-behavior-across-contexts) "
                    "gets rejected at the extraction layer."
                ),
            },
        },
        "required": ["canvas_id", "utterance"],
    },
}


CANVAS_PREFERENCE_CONFIRM_TOOL = {
    "name": "canvas_preference_confirm",
    "description": (
        "Resolve a pending preference: either confirm it (promotes to the "
        "canvas's confirmed preferences, where heuristic dispatch honors it) "
        "or discard it (moves to declined_preferences for audit). Only the "
        "member's explicit consent drives confirmation — never auto-apply, "
        "even on a canvas with gardener_consent=auto-all. Pending preferences "
        "older than 24 hours auto-expire on the next Gardener dispatch; this "
        "tool returns an error if the named pending preference is already "
        "gone."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "canvas_id": {"type": "string"},
            "preference_name": {
                "type": "string",
                "description": "Name of the pending preference, from canvas_preference_extract's output.",
            },
            "action": {
                "type": "string",
                "enum": ["confirm", "discard"],
                "description": "confirm to persist; discard to drop with audit trail.",
            },
        },
        "required": ["canvas_id", "preference_name", "action"],
    },
}


PAGE_SEARCH_TOOL = {
    "name": "page_search",
    "description": (
        "Search page bodies + titles for a query string. Case-insensitive "
        "substring match, ranked by match count. If canvas_id is omitted, "
        "searches across all canvases the caller has access to."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "canvas_id": {
                "type": "string",
                "description": "Optional. If omitted, search across accessible canvases.",
            },
            "limit": {
                "type": "integer",
                "description": "Max hits. Default 20.",
            },
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# PARCEL-PRIMITIVE-V1: cross-space file transfer
# ---------------------------------------------------------------------------

PACK_PARCEL_TOOL = {
    "name": "pack_parcel",
    "description": (
        "Pack one or more files from your active space and offer them to "
        "another member. Files are copied into a staged parcel directory; "
        "the recipient gets an offer via relational messaging and can "
        "accept or decline. On accept, files copy to the recipient's "
        "space with sha256 verification. Use when a member has asked you "
        "to 'send' or 'share' files to another member."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Filenames in the active space to include in the parcel. "
                    "Paths must resolve inside the space. Max 50 files."
                ),
            },
            "recipient_member_id": {
                "type": "string",
                "description": (
                    "Target member's ID. Use list_members / declare_relationship "
                    "output to find IDs."
                ),
            },
            "note": {
                "type": "string",
                "description": "Optional short context for the recipient.",
            },
            "ttl_days": {
                "type": "integer",
                "description": (
                    "Days the offer stays open before auto-expiring. "
                    "Default 7, max 30."
                ),
            },
        },
        "required": ["files", "recipient_member_id"],
    },
}


RESPOND_TO_PARCEL_TOOL = {
    "name": "respond_to_parcel",
    "description": (
        "Respond to a parcel offer you received. Accept copies the files "
        "into your active space at parcels/{parcel_id}/ with sha256 "
        "verification; decline leaves the sender's staged files alone "
        "and returns a reason to the sender."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "parcel_id": {
                "type": "string",
                "description": "The parcel's ID (from the offer envelope).",
            },
            "action": {
                "type": "string",
                "enum": ["accept", "decline"],
                "description": "accept | decline",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional; surfaced to the sender on decline."
                ),
            },
        },
        "required": ["parcel_id", "action"],
    },
}


LIST_PARCELS_TOOL = {
    "name": "list_parcels",
    "description": (
        "List parcels involving the calling member. Direction filters "
        "by whether the member is the sender or recipient; status filters "
        "by lifecycle state. Results are scoped — a member only sees "
        "parcels they sent or received."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["sent", "received", "all"],
                "description": "Default 'all'.",
            },
            "status": {
                "type": "string",
                "enum": [
                    "packed", "accepted", "delivered", "declined",
                    "expired", "failed", "all",
                ],
                "description": "Default 'all'.",
            },
        },
    },
}


INSPECT_PARCEL_TOOL = {
    "name": "inspect_parcel",
    "description": (
        "Return full detail for a single parcel: manifest (filenames, "
        "sizes, sha256 hashes), all lifecycle timestamps, current status. "
        "Scoped — a member can only inspect parcels they sent or received."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "parcel_id": {
                "type": "string",
                "description": "The parcel's ID.",
            },
        },
        "required": ["parcel_id"],
    },
}


REQUEST_TOOL = {
    "name": "request_tool",
    "description": (
        "Request activation of a tool capability that is NOT in your current tool set. "
        "Only use this as a LAST RESORT when no existing tool in your set can do what "
        "you need. If a tool like create-event or list-events is already available to "
        "you, call it directly — do not use request_tool."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "capability_name": {
                "type": "string",
                "description": (
                    "The name of the capability to activate, if known. "
                    "Use 'unknown' if you know what you need but not the exact name."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "Thorough description of what you need the tool to do. "
                    "Be exhaustive — include the function needed, the context, "
                    "and why it's needed. This helps match the right tool."
                ),
            },
        },
        "required": ["capability_name", "description"],
    },
}


# READ_DOC_TOOL was retired in REFERENCE-PRIMITIVE-V1 — canonical
# documentation reaches via request_reference now. See
# kernos/kernel/reference/tools.py for the replacement surface and
# the spec body §"read_doc retirement" for the six-criteria
# discipline applied. read_source and read_soul stay (distinct
# purposes — Python source code and per-member identity).


REMEMBER_DETAILS_TOOL = {
    "name": "remember_details",
    "description": (
        "Retrieve exact conversation text from a specific archived source log. "
        "Use after remember() when a Ledger entry includes 'source: log_NNN'. "
        "Optional query narrows to the relevant section within that log. "
        "This is a read-only operation — no state is changed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "source_ref": {
                "type": "string",
                "description": (
                    "The log reference to retrieve, e.g., 'log_003'. "
                    "Get this from a Ledger entry returned by remember()."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "Optional keyword to find the relevant section within "
                    "the log. Returns matching lines with surrounding context. "
                    "If omitted, returns the full log (bounded)."
                ),
            },
        },
        "required": ["source_ref"],
    },
}


# read_doc helper was retired in REFERENCE-PRIMITIVE-V1. Canonical
# docs/ content is reached via request_reference; the catalog +
# cohort path replaces direct path-based reads. See
# kernos/kernel/reference/tools.py.


MANAGE_CAPABILITIES_TOOL = {
    "name": "manage_capabilities",
    "description": (
        "Manage connected services — list, enable, disable, install, or remove capabilities. "
        "Use 'list' to see all services and their connection status. "
        "Use 'enable' or 'disable' to toggle a service on or off. "
        "Use 'install' to add a new MCP server. "
        "Use 'remove' to uninstall a user-added capability "
        "(defaults can only be disabled, not removed). "
        "Note: to see which tools are available, check the TOOLS section in your instructions — "
        "this command manages services, not individual tools."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "enable", "disable", "install", "remove"],
                "description": "The action to perform.",
            },
            "capability": {
                "type": "string",
                "description": "The capability name (required for enable/disable/remove).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


READ_SOURCE_TOOL = {
    "name": "read_source",
    "description": (
        "Read a file from the Kernos repository. Allowed roots: "
        "the kernos/ package (source code), specs/ (architecture "
        "specs), and docs/ (technical documentation). Use when the "
        "user asks how something works technically, when you need "
        "to read your own architecture specs, or to check "
        "implementation details. Paths starting with kernos/, "
        "specs/, or docs/ are interpreted relative to the repo "
        "root; bare paths like 'kernel/awareness.py' stay relative "
        "to kernos/ for backward compatibility."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Repo-relative or kernos-relative path. "
                    "Examples: 'kernel/awareness.py' (resolves to "
                    "kernos/kernel/awareness.py), "
                    "'specs/SELF-IMPROVEMENT-CLOSURE-V1.md', "
                    "'docs/TECHNICAL-ARCHITECTURE.md', "
                    "'kernos/messages/handler.py' (explicit-root form)."
                ),
            },
            "section": {
                "type": "string",
                "description": (
                    "Optional class or function name to extract. "
                    "Examples: 'AwarenessEvaluator', 'run_time_pass', "
                    "'_gate_tool_call'. If omitted, returns the full "
                    "file. Section extraction works on .py files; "
                    "for markdown specs/docs, omit and read the full "
                    "file."
                ),
            },
        },
        "required": ["path"],
    },
}


READ_SOUL_TOOL = {
    "name": "read_soul",
    "description": (
        "Read your own identity — who you are, your personality, your relationship "
        "with this user. Use this when you want to understand or verify your own state."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


UPDATE_SOUL_TOOL = {
    "name": "update_soul",
    "description": (
        "Update your own identity — name, emoji, personality notes, communication "
        "style. Use when the user asks you to change something about yourself, or "
        "when you and the user agree on a change."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "field": {
                "type": "string",
                "description": (
                    "The soul field to update. Allowed: agent_name, emoji, "
                    "personality_notes, communication_style."
                ),
            },
            "value": {
                "type": "string",
                "description": "The new value for the field.",
            },
        },
        "required": ["field", "value"],
    },
}


INSPECT_STATE_TOOL = {
    "name": "inspect_state",
    "description": (
        "Inspect what Kernos currently believes is true about this user. "
        "Returns active preferences (with linked triggers/covenants), "
        "active triggers, behavioral rules, key facts, and connected "
        "capabilities. Use this when the user asks 'what notifications "
        "do I have?', 'what preferences are active?', or 'what do you "
        "know about me?'"
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# LLM-SETUP-AND-FALLBACK admin tools — system space, admin-only.
# Gate-authorized. No LLM call in the handler path.

SET_CHAIN_MODEL_TOOL = {
    "name": "set_chain_model",
    "description": (
        "Admin-only. Change the model used by a (chain, provider) pair in "
        "the on-disk LLM chain config. Validates that the provider is "
        "configured and that the model appears in the provider's /models "
        "response before writing. Emits a CHAIN_MODEL_CHANGED event. "
        "Use this when the user wants to swap the model for a provider "
        "they've already set up, without re-running `kernos setup llm`."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "chain": {
                "type": "string",
                "enum": ["primary", "lightweight", "simple", "cheap"],
                "description": (
                    "The chain tier to change. 'simple' and 'cheap' are "
                    "deprecated aliases for 'lightweight'."
                ),
            },
            "provider_id": {
                "type": "string",
                "description": (
                    "Provider id from the registry (anthropic, openai, "
                    "google, groq, xai, openrouter, ollama)."
                ),
            },
            "model_id": {
                "type": "string",
                "description": "The new model id (e.g. 'claude-opus-4-7').",
            },
        },
        "required": ["chain", "provider_id", "model_id"],
        "additionalProperties": False,
    },
}


DIAGNOSE_MESSENGER_TOOL = {
    "name": "diagnose_messenger",
    "description": (
        "Admin-only. Return a readable view of what the Messenger cohort "
        "would see for a (member_a, member_b) pair: the covenants scoped to "
        "this pair, the unexpired ephemeral permissions, and the current "
        "relationship profile. Does not surface recent Messenger decisions "
        "themselves — Messenger outcomes are friction-trace-only per the "
        "MESSENGER-COHORT contract and never reach this tool. System space "
        "only."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "member_a_id": {
                "type": "string",
                "description": "The disclosing member's id.",
            },
            "member_b_id": {
                "type": "string",
                "description": "The requesting member's id.",
            },
        },
        "required": ["member_a_id", "member_b_id"],
        "additionalProperties": False,
    },
}


DIAGNOSE_LLM_CHAIN_TOOL = {
    "name": "diagnose_llm_chain",
    "description": (
        "Admin-only. Return a readable view of the current LLM chain "
        "configuration: every chain, providers in fallback order, the "
        "model per provider, the backing storage backend, and whether each "
        "provider has a stored credential. Optionally include recent "
        "FALLBACK_USED events for diagnostic purposes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "include_fallback_events": {
                "type": "boolean",
                "description": (
                    "Also include recent FALLBACK_USED events (last 50). "
                    "Default false."
                ),
            },
        },
        "additionalProperties": False,
    },
}


# Allowed fields for update_soul — lifecycle and user fields are read-only
SOUL_UPDATABLE_FIELDS = {"agent_name", "emoji", "personality_notes", "communication_style"}


def read_source(path: str, section: str = "") -> str:
    """Read a file from the Kernos repository.

    Allowed roots:
      - kernos/  (source code) — backward-compatible bare paths like
        'kernel/awareness.py' resolve here.
      - specs/   (architecture specs) — explicit prefix required.
      - docs/    (technical documentation) — explicit prefix required.

    Path semantics:
      - If path starts with 'kernos/', 'specs/', or 'docs/', it is
        interpreted relative to the repo root.
      - Otherwise the path is treated as kernos-relative (preserves
        prior callers that wrote 'kernel/awareness.py' meaning
        'kernos/kernel/awareness.py').

    Security: rejects absolute paths, rejects path traversal ('..'),
    and ensures the resolved path lives within one of the allowed
    roots. Self-improvement spec reads against specs/ land cleanly
    via 'specs/SPEC-NAME.md'.
    """
    import importlib
    from pathlib import Path

    # Security: reject absolute paths
    if path.startswith("/") or path.startswith("\\"):
        return (
            "Error: Absolute paths are not allowed. Use a relative "
            "path like 'kernel/awareness.py' or "
            "'specs/MY-SPEC.md'."
        )

    # Security: reject path traversal
    if ".." in path:
        return "Error: Path traversal ('..') is not allowed."

    # Compute allowed roots
    kernos_root = Path(importlib.import_module("kernos").__file__).parent
    repo_root = kernos_root.parent
    specs_root = repo_root / "specs"
    docs_root = repo_root / "docs"
    allowed_roots = (kernos_root, specs_root, docs_root)

    # Resolve target. Explicit-root prefixes anchor against repo_root;
    # bare paths anchor against kernos_root for backward compatibility.
    if path.startswith(("kernos/", "specs/", "docs/")):
        target = (repo_root / path).resolve()
    else:
        target = (kernos_root / path).resolve()

    # Security: must resolve within one of the allowed roots.
    if not any(
        str(target) == str(root) or str(target).startswith(str(root) + "/")
        for root in allowed_roots
    ):
        return (
            "Error: Path resolves outside the allowed roots "
            "(kernos/, specs/, docs/)."
        )

    # Report a repo-relative path in error messages so the agent
    # sees what was actually attempted, regardless of which root it
    # anchored against.
    try:
        display_path = target.relative_to(repo_root)
    except ValueError:
        display_path = target

    if not target.exists():
        return f"Error: File not found: {display_path}"

    if not target.is_file():
        return f"Error: Not a file: {display_path}"

    if target.suffix not in (".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml"):
        return f"Error: Unsupported file type: {target.suffix}"

    content = target.read_text(encoding="utf-8")

    if not section:
        # Cap at 500 lines for full files
        lines = content.split("\n")
        if len(lines) > 500:
            return "\n".join(lines[:500]) + f"\n\n... (truncated — {len(lines)} total lines)"
        return content

    # Extract a class or function section
    lines = content.split("\n")
    start_idx = None
    start_indent = None

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"class {section}") or stripped.startswith(f"def {section}"):
            start_idx = i
            start_indent = len(line) - len(stripped)
            break
        # Also match async def
        if stripped.startswith(f"async def {section}"):
            start_idx = i
            start_indent = len(line) - len(stripped)
            break

    if start_idx is None:
        return f"Error: Section '{section}' not found in {display_path}"

    # Find end: next definition at same or lower indent level
    result_lines = [lines[start_idx]]
    for i in range(start_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            result_lines.append(line)
            continue
        current_indent = len(line) - len(line.lstrip())
        stripped = line.lstrip()
        # Same-level or lower-level class/def = end of section
        if current_indent <= start_indent and (
            stripped.startswith("class ")
            or stripped.startswith("def ")
            or stripped.startswith("async def ")
            or stripped.startswith("# ---")
        ):
            break
        result_lines.append(line)

    # Strip trailing blank lines
    while result_lines and not result_lines[-1].strip():
        result_lines.pop()

    return "\n".join(result_lines)

