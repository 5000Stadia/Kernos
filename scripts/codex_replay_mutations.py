"""Replay a captured Codex body with field-stripping mutations.

Use after capturing a real failing body via the codex_provider's
KERNOS_CODEX_CAPTURE_BODY hook. Replays the exact body N times to
confirm reproducibility, then mutates one variable at a time to
identify which field flips the failure rate.

Usage:
    # 1. capture
    KERNOS_CODEX_CAPTURE_BODY=1 ./start.sh
    # send a message that triggers the failure, then Ctrl+C
    # body will be at /tmp/codex_bodies/body_<ts>_<size>KB.json

    # 2. replay
    python scripts/codex_replay_mutations.py /tmp/codex_bodies/body_*.json
"""

from __future__ import annotations

import asyncio
import copy
import glob
import json
import logging
import os
import sys
import time
from typing import Any

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "kernos.providers.codex_provider"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from kernos.providers.codex_provider import OpenAICodexProvider
from kernos.kernel.credentials import resolve_openai_codex_credential


TRIALS = int(os.getenv("REPLAY_TRIALS", "3"))
SLEEP_BETWEEN = float(os.getenv("REPLAY_SLEEP_S", "1.0"))


async def replay_body(provider: OpenAICodexProvider, body: dict, label: str) -> tuple[int, int, list[str]]:
    """Send the body verbatim N times. Return (successes, trials, errors)."""
    payload_kb = len(json.dumps(body)) // 1024
    n_tools = len(body.get("tools", []))
    print(f"  cell={label} payload={payload_kb}KB tools={n_tools}")
    successes = 0
    errors: list[str] = []
    for t in range(TRIALS):
        url = provider._resolve_url()
        headers = provider._headers(session_id=body.get("prompt_cache_key", ""))
        headers["accept"] = "text/event-stream"
        await provider._ensure_valid_token()
        # Refresh auth header in case token rotated.
        headers["Authorization"] = f"Bearer {provider._credential['access']}"
        http = await provider._ensure_http()
        start = time.monotonic()
        try:
            async with http.stream(
                "POST", url, headers=headers, json=body, timeout=90.0,
            ) as resp:
                resp.raise_for_status()
                data = await provider._collect_sse_response(resp)
            sec = time.monotonic() - start
            successes += 1
            print(f"    trial {t+1}: OK ({sec:.1f}s)")
        except Exception as exc:
            sec = time.monotonic() - start
            msg = str(exc)
            label_err = "server_error" if "server_error" in msg.lower() else type(exc).__name__
            errors.append(f"{label_err}: {msg[:120]}")
            print(f"    trial {t+1}: FAIL ({sec:.1f}s) {label_err}")
        await asyncio.sleep(SLEEP_BETWEEN)
    print(f"  → {successes}/{TRIALS}\n")
    return successes, TRIALS, errors


def mutate_strip_field(body: dict, field: str) -> dict:
    """Return a deep-copy of body with `field` removed from top level."""
    m = copy.deepcopy(body)
    m.pop(field, None)
    return m


def mutate_replace_tools_with_simple(body: dict) -> dict:
    """Replace real tools with synthetic equivalents (same count, simple schemas)."""
    m = copy.deepcopy(body)
    n = len(m.get("tools", []))
    m["tools"] = [
        {
            "type": "function",
            "name": f"synthetic_tool_{i}",
            "description": f"Synthetic replacement for real tool {i}.",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
            },
        }
        for i in range(n)
    ]
    return m


def mutate_truncate_tools(body: dict, keep_n: int) -> dict:
    """Keep only the first `keep_n` tools."""
    m = copy.deepcopy(body)
    m["tools"] = m.get("tools", [])[:keep_n]
    return m


def mutate_strip_instructions(body: dict) -> dict:
    """Replace instructions with a minimal placeholder."""
    m = copy.deepcopy(body)
    m["instructions"] = "Be helpful."
    return m


def mutate_strip_input(body: dict) -> dict:
    """Replace input items with a minimal placeholder."""
    m = copy.deepcopy(body)
    m["input"] = [
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    ]
    return m


def mutate_lower_reasoning(body: dict) -> dict:
    """Set reasoning effort to 'low'."""
    m = copy.deepcopy(body)
    if "reasoning" in m:
        m["reasoning"] = {**m["reasoning"], "effort": "low"}
    return m


def mutate_higher_reasoning(body: dict) -> dict:
    """Set reasoning effort to 'high' (openclaw default)."""
    m = copy.deepcopy(body)
    if "reasoning" in m:
        m["reasoning"] = {**m["reasoning"], "effort": "high"}
    return m


def mutate_add_strict_null(body: dict) -> dict:
    """Add strict: null to every tool (openclaw default)."""
    m = copy.deepcopy(body)
    for t in m.get("tools", []):
        t["strict"] = None
    return m


def mutate_lower_text_verbosity(body: dict) -> dict:
    """Set text.verbosity to 'low' (openclaw default)."""
    m = copy.deepcopy(body)
    if "text" in m and isinstance(m["text"], dict):
        m["text"] = {**m["text"], "verbosity": "low"}
    return m


def mutate_strip_addtl_props(body: dict) -> dict:
    """Remove additionalProperties from every tool's parameter schema."""
    m = copy.deepcopy(body)
    def walk(node):
        if isinstance(node, dict):
            node.pop("additionalProperties", None)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    for t in m.get("tools", []):
        walk(t.get("parameters", {}))
    return m


def mutate_drop_empty_required(body: dict) -> dict:
    """Remove empty `required: []` arrays from every tool's schema."""
    m = copy.deepcopy(body)
    def walk(node):
        if isinstance(node, dict):
            if node.get("required") == []:
                node.pop("required", None)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    for t in m.get("tools", []):
        walk(t.get("parameters", {}))
    return m


async def main(body_path: str) -> None:
    print(f"Loading body: {body_path}")
    with open(body_path) as f:
        original = json.load(f)
    payload_kb = len(json.dumps(original)) // 1024
    n_tools = len(original.get("tools", []))
    print(f"Original: {payload_kb}KB / {n_tools} tools / model={original.get('model', '?')}")
    print(f"Top-level keys: {sorted(original.keys())}\n")

    provider = OpenAICodexProvider(credential=resolve_openai_codex_credential())
    await provider._ensure_valid_token()

    cells: list[tuple[str, dict]] = [
        ("baseline (verbatim)", original),
        # OpenClaw-suspect: tool schema patterns
        ("add strict:null to tools", mutate_add_strict_null(original)),
        ("strip additionalProperties", mutate_strip_addtl_props(original)),
        ("drop empty required arrays", mutate_drop_empty_required(original)),
        ("synthetic tools (same count)", mutate_replace_tools_with_simple(original)),
        # OpenClaw-suspect: defaults differ from openclaw
        ("text.verbosity=low (openclaw default)", mutate_lower_text_verbosity(original)),
        ("reasoning effort=high (openclaw default)", mutate_higher_reasoning(original)),
        ("reasoning effort=low", mutate_lower_reasoning(original)),
        # Sanity: tool count + content reduction
        ("keep 10 tools only", mutate_truncate_tools(original, 10)),
        ("keep 5 tools only", mutate_truncate_tools(original, 5)),
        # Sanity: top-level field strips (likely not the trigger per openclaw)
        ("strip text", mutate_strip_field(original, "text")),
        ("strip tool_choice", mutate_strip_field(original, "tool_choice")),
        ("strip parallel_tool_calls", mutate_strip_field(original, "parallel_tool_calls")),
        # Content strips
        ("strip instructions content", mutate_strip_instructions(original)),
        ("strip input content", mutate_strip_input(original)),
    ]

    print("=" * 70)
    print("REPLAY MUTATIONS")
    print("=" * 70)
    results = []
    for label, body in cells:
        succ, total, errs = await replay_body(provider, body, label)
        kb = len(json.dumps(body)) // 1024
        nt = len(body.get("tools", []))
        results.append({
            "label": label, "payload_kb": kb, "tools": nt,
            "pass": succ, "total": total,
        })

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"{'label':<35} {'KB':>5} {'tools':>6} {'pass':>6}")
    print("-" * 70)
    for r in results:
        pf = f"{r['pass']}/{r['total']}"
        print(f"{r['label']:<35} {r['payload_kb']:>5} {r['tools']:>6} {pf:>6}")

    out_path = "data/diagnostics/codex_replay_mutations.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"body_path": body_path, "results": results}, f, indent=2)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        # Default: pick most recent capture
        candidates = sorted(glob.glob("/tmp/codex_bodies/body_*.json"))
        if not candidates:
            print("Usage: codex_replay_mutations.py <body.json>")
            print("(Or set KERNOS_CODEX_CAPTURE_BODY=1 and trigger a real call first.)")
            sys.exit(2)
        body_path = candidates[-1]
        print(f"No path given, using most recent: {body_path}\n")
    else:
        body_path = sys.argv[1]
    asyncio.run(main(body_path))
