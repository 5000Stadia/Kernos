"""Side-by-side diff: Kernos failing body vs OpenClaw succeeding body.

Both are full Codex responses-API bodies. Print field-by-field comparison
to surface every difference. Then for each difference, group by
likely-trigger heuristic (top-level vs nested, value vs structure).

Usage:
    python scripts/codex_body_diff.py <kernos_failing.json> <openclaw_success.json>
"""

from __future__ import annotations

import json
import sys
from typing import Any


def load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def fmt_value(v: Any, max_len: int = 80) -> str:
    s = json.dumps(v, default=str) if not isinstance(v, str) else repr(v)
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s


def shape_summary(v: Any) -> str:
    if v is None:
        return "None"
    if isinstance(v, dict):
        keys = ", ".join(sorted(v.keys()))
        return f"dict[{len(v)}]: {{{keys}}}"
    if isinstance(v, list):
        return f"list[{len(v)}]"
    if isinstance(v, str):
        return f"str[{len(v)}]"
    return type(v).__name__


def diff_top_level(k_body: dict, o_body: dict) -> None:
    k_keys = set(k_body.keys())
    o_keys = set(o_body.keys())
    print("=" * 78)
    print("TOP-LEVEL FIELD COMPARISON")
    print("=" * 78)
    print(f"{'field':<28} {'KERNOS':<22} {'OPENCLAW':<22}")
    print("-" * 78)
    for k in sorted(k_keys | o_keys):
        kv = k_body.get(k, "<MISSING>")
        ov = o_body.get(k, "<MISSING>")
        kvs = "<MISSING>" if kv == "<MISSING>" else shape_summary(kv)
        ovs = "<MISSING>" if ov == "<MISSING>" else shape_summary(ov)
        marker = "  " if (k in k_keys and k in o_keys and shape_summary(kv) == shape_summary(ov)) else "* "
        print(f"{marker}{k:<26} {kvs:<22} {ovs:<22}")

    print()
    print("(*) marks differences in presence or shape.")


def diff_scalar_values(k_body: dict, o_body: dict, fields: list[str]) -> None:
    print("\n" + "=" * 78)
    print("SCALAR VALUE COMPARISON")
    print("=" * 78)
    for f in fields:
        kv = k_body.get(f)
        ov = o_body.get(f)
        marker = "  " if kv == ov else "* "
        print(f"{marker}{f}:")
        print(f"    kernos:   {fmt_value(kv)}")
        print(f"    openclaw: {fmt_value(ov)}")


def diff_tools(k_body: dict, o_body: dict) -> None:
    print("\n" + "=" * 78)
    print("TOOL SHAPE COMPARISON (first 5 of each)")
    print("=" * 78)
    k_tools = k_body.get("tools", [])
    o_tools = o_body.get("tools", [])
    print(f"Kernos: {len(k_tools)} tools, total bytes {len(json.dumps(k_tools)):,}")
    print(f"OpenClaw: {len(o_tools)} tools, total bytes {len(json.dumps(o_tools)):,}")

    # Top-level tool keys (e.g., {type, name, description, parameters} or +strict)
    k_tool_keys = set()
    o_tool_keys = set()
    for t in k_tools:
        k_tool_keys.update(t.keys())
    for t in o_tools:
        o_tool_keys.update(t.keys())
    print(f"\nKernos tool top-level keys:   {sorted(k_tool_keys)}")
    print(f"OpenClaw tool top-level keys: {sorted(o_tool_keys)}")
    diff = (k_tool_keys ^ o_tool_keys)
    if diff:
        print(f"  → KEYS DIFFER: {diff}")

    # Schema-shape audit: walk parameters for known sus patterns
    def audit(tools, label):
        addtl_count = 0
        empty_required = 0
        anyof = 0
        oneof = 0
        allof = 0
        ref_count = 0
        nullable_count = 0
        max_depth = 0

        def walk(node, depth=0):
            nonlocal addtl_count, empty_required, anyof, oneof, allof, ref_count, nullable_count, max_depth
            max_depth = max(max_depth, depth)
            if isinstance(node, dict):
                if "additionalProperties" in node:
                    addtl_count += 1
                if "required" in node and node["required"] == []:
                    empty_required += 1
                if "anyOf" in node:
                    anyof += 1
                if "oneOf" in node:
                    oneof += 1
                if "allOf" in node:
                    allof += 1
                if "$ref" in node:
                    ref_count += 1
                if "nullable" in node:
                    nullable_count += 1
                for v in node.values():
                    walk(v, depth + 1)
            elif isinstance(node, list):
                for v in node:
                    walk(v, depth + 1)

        for t in tools:
            walk(t.get("parameters", {}))

        print(f"\n{label} schema audit:")
        print(f"  additionalProperties:    {addtl_count}")
        print(f"  empty required arrays:   {empty_required}")
        print(f"  anyOf:                   {anyof}")
        print(f"  oneOf:                   {oneof}")
        print(f"  allOf:                   {allof}")
        print(f"  $ref:                    {ref_count}")
        print(f"  nullable:                {nullable_count}")
        print(f"  max nesting depth:       {max_depth}")

    audit(k_tools, "Kernos")
    audit(o_tools, "OpenClaw")


def diff_input_shape(k_body: dict, o_body: dict) -> None:
    print("\n" + "=" * 78)
    print("INPUT ITEM SHAPE COMPARISON")
    print("=" * 78)
    k_input = k_body.get("input", [])
    o_input = o_body.get("input", [])
    print(f"Kernos input items: {len(k_input)}")
    print(f"OpenClaw input items: {len(o_input)}")
    if k_input:
        print(f"\nKernos input[0] shape:")
        print(json.dumps(k_input[0], default=str)[:500])
    if o_input:
        print(f"\nOpenClaw input[0] shape:")
        print(json.dumps(o_input[0], default=str)[:500])


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: codex_body_diff.py <kernos.json> <openclaw.json>")
        sys.exit(2)
    k_body = load(sys.argv[1])
    o_body = load(sys.argv[2])

    print(f"Kernos body:   {sys.argv[1]} ({len(json.dumps(k_body)) // 1024} KB)")
    print(f"OpenClaw body: {sys.argv[2]} ({len(json.dumps(o_body)) // 1024} KB)")
    print()

    diff_top_level(k_body, o_body)
    diff_scalar_values(k_body, o_body, [
        "model", "store", "stream",
        "tool_choice", "parallel_tool_calls",
    ])
    diff_tools(k_body, o_body)
    diff_input_shape(k_body, o_body)

    # Reasoning + text + include
    print("\n" + "=" * 78)
    print("NESTED CONFIG COMPARISON")
    print("=" * 78)
    for f in ["reasoning", "text", "include"]:
        print(f"\n  {f}:")
        print(f"    kernos:   {fmt_value(k_body.get(f), 200)}")
        print(f"    openclaw: {fmt_value(o_body.get(f), 200)}")


if __name__ == "__main__":
    main()
