"""Compact tool-call signature presentation — TOOL-ARG-REPAIR-V1 guidance side.

Root cause (Codex syntax-presentation consult, session 019eaf8e): the model
receives tool syntax ONLY as raw JSON Schema inside a ~50KB pile of 40+ tool
defs sent with ``strict: null`` (advisory, never enforced). It invents
argument shapes because no short, language-native call pattern sits near its
decision point. Live evidence: the consult description contained the harness
enum + worked examples and the model still wrote ``harness='synchronous_consult'``.

Two presentation surfaces, both generated from the SAME schemas the provider
sends (no second source of truth to drift):

1. ``build_signature_block(tools)`` — a ``## TOOL CALL SIGNATURES`` endcap
   appended as the LAST block of the dynamic developer message, so the
   developer message ENDS with the call patterns (recency placement).
   Signatures use the provider wire names (``area__tool``) the model must
   actually emit.
2. ``signature_prefix(tool, wire_name)`` — a two-line ``SIGNATURE:`` /
   ``EXAMPLE:`` header the provider prepends to each tool description at
   translation time, so the schema's prose starts with the call pattern
   instead of burying it.

Format rules (tuned for gpt-5.x non-strict adherence per the consult):
required args first, optional args marked ``?``, enums inline as
``"a"|"b"|"c"``, at most ONE example, keep each entry to 1-3 lines.
"""
from __future__ import annotations

from typing import Any

# Curated examples for the high-fumble tools (live self-test failures).
# Keyed by FLAT name; rendered with the wire name at build time. Keep this
# list short — an example earns its place by a live mis-call, not by being
# nice to have.
_EXAMPLES: dict[str, str] = {
    "manage_schedule": (
        '{"action": "create", "description": "Remind me to send the '
        'estimate Friday at 9am"}  — put the full WHAT + WHEN in '
        "`description`; do not invent fields like due_at/kind/title."
    ),
    "consult": (
        '{"harness": "codex", "question": "Review the staged diff for '
        'regressions."}  — `harness` is ONLY the agent enum; the task/label '
        "text belongs in `question`."
    ),
    "register_tool": (
        '{"descriptor_file": "my_tool.tool.json"}  — the .tool.json FILE '
        'must contain {"name", "description", "implementation": "my_tool.py"}; '
        "implementation is a field in the file, not an argument here."
    ),
}

# Keep the endcap honest about size: beyond this many enum members, summarize.
_MAX_ENUM_RENDER = 6


def _render_type(prop: dict[str, Any]) -> str:
    """Render one property's type compactly: enums inline, else short name."""
    enum = prop.get("enum")
    if isinstance(enum, list) and enum:
        shown = enum[:_MAX_ENUM_RENDER]
        rendered = "|".join(f'"{v}"' for v in shown)
        if len(enum) > _MAX_ENUM_RENDER:
            rendered += f"|…{len(enum) - _MAX_ENUM_RENDER} more"
        return rendered
    t = prop.get("type")
    if isinstance(t, list):
        t = "/".join(str(x) for x in t)
    return {
        "string": "str", "integer": "int", "number": "num",
        "boolean": "bool", "object": "obj", "array": "list",
    }.get(str(t), str(t) if t else "any")


def build_signature(tool: dict[str, Any], wire_name: str = "") -> str:
    """One-line call signature from a tool def's input_schema.

    Required args first, optionals suffixed ``?``. Defensive: any schema
    weirdness degrades to ``name(...)`` rather than raising — presentation
    must never break assembly.
    """
    name = wire_name or tool.get("name", "?")
    try:
        schema = tool.get("input_schema") or {}
        props: dict = schema.get("properties") or {}
        required = list(schema.get("required") or [])
        ordered = [k for k in required if k in props] + [
            k for k in props if k not in required
        ]
        parts = []
        for key in ordered:
            opt = "" if key in required else "?"
            parts.append(f"{key}{opt}: {_render_type(props[key])}")
        return f"{name}({', '.join(parts)})"
    except Exception:
        return f"{name}(...)"


def signature_prefix(tool: dict[str, Any], wire_name: str = "") -> str:
    """The two-line SIGNATURE/EXAMPLE header prepended to a tool description."""
    sig = f"SIGNATURE: {build_signature(tool, wire_name)}"
    example = _EXAMPLES.get(tool.get("name", ""))
    if example:
        return f"{sig}\nEXAMPLE: {example}\n\n"
    return f"{sig}\n\n"


def build_signature_block(
    tools: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    skin: dict[str, str] | None = None,
) -> str:
    """The ``## TOOL CALL SIGNATURES`` endcap for the dynamic developer message.

    One signature line per surfaced tool (wire names via ``skin``), plus the
    curated example for high-fumble tools. Empty string when no tools — never
    an empty header.
    """
    if not tools:
        return ""
    lines = [
        "## TOOL CALL SIGNATURES",
        "Call tools with EXACTLY these names and argument keys — required "
        "args first, `?` marks optional. If anything else in this prompt "
        "conflicts with this block, this block wins.",
        "",
    ]
    for t in tools:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        flat = t["name"]
        wire = (skin or {}).get(flat, flat)
        lines.append(f"- {build_signature(t, wire)}")
        example = _EXAMPLES.get(flat)
        if example:
            if "  — " in example:
                call, note = example.split("  — ", 1)
                lines.append(f"  e.g. {wire}({call})  — {note}")
            else:
                lines.append(f"  e.g. {wire}({example})")
    return "\n".join(lines)
