"""Codex wire-shape tipping point investigation.

Boot:
    cd /home/k/Kernos && source .venv/bin/activate
    python scripts/diagnostics/codex_tipping_point.py

What it does:
    Drives kernos.providers.codex_provider.OpenAICodexProvider directly
    against chatgpt.com/backend-api/codex/responses and measures where
    the current backend tips into mid-stream server_error.

    Sweeps: payload size, reasoning effort, optional body fields
    (text/verbosity, parallel_tool_calls, tool_choice).

    Each cell is N trials; reports successes / failures. Sequential —
    no concurrency, no overwhelming the backend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

# Reduce noise from kernos's own logging — keep our prints clean.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "kernos.providers.codex_provider"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from kernos.providers.codex_provider import OpenAICodexProvider
from kernos.kernel.credentials import resolve_openai_codex_credential


MODEL = os.getenv("INVESTIGATE_MODEL", "gpt-5.5")
TRIALS_PER_CELL = int(os.getenv("INVESTIGATE_TRIALS", "3"))
SLEEP_BETWEEN = float(os.getenv("INVESTIGATE_SLEEP_S", "1.0"))


def make_tool(idx: int, padding_chars: int = 0) -> dict:
    """Synthetic tool schema with optional description padding for size control."""
    pad = "x" * padding_chars
    return {
        "name": f"probe_tool_{idx}",
        "description": (f"Synthetic tool {idx} for tipping-point probe. {pad}").strip(),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Probe query"},
                "limit": {"type": "integer", "description": "Result limit"},
            },
            "required": ["query"],
        },
    }


def make_tools(count: int, padding_chars: int = 0) -> list[dict]:
    return [make_tool(i, padding_chars) for i in range(count)]


def estimate_payload_kb(messages: list[dict], tools: list[dict], system: str) -> int:
    """Approximate body size — tools dominate."""
    body_approx = json.dumps({
        "system": system, "messages": messages, "tools": tools,
    })
    return len(body_approx) // 1024


async def probe_once(
    provider: OpenAICodexProvider,
    *,
    system: str,
    user_message: str,
    tools: list[dict],
    conversation_id: str,
    timeout_s: float = 90.0,
) -> tuple[bool, str, float]:
    """One call. Returns (success, error_string, elapsed_s)."""
    messages = [{"role": "user", "content": user_message}]
    start = time.monotonic()
    try:
        await asyncio.wait_for(
            provider.complete(
                model=MODEL,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=200,
                conversation_id=conversation_id,
            ),
            timeout=timeout_s,
        )
        return (True, "", time.monotonic() - start)
    except asyncio.TimeoutError:
        return (False, f"TIMEOUT after {timeout_s}s", time.monotonic() - start)
    except Exception as exc:
        # Strip the noisy traceback into the most useful one-liner.
        msg = str(exc)
        if "server_error" in msg.lower():
            label = "server_error"
        elif "rate" in msg.lower():
            label = "rate_limit"
        else:
            label = type(exc).__name__
        return (False, f"{label}: {msg[:160]}", time.monotonic() - start)


async def run_cell(
    provider: OpenAICodexProvider,
    label: str,
    *,
    system: str,
    user_message: str,
    tools: list[dict],
    conversation_id_prefix: str,
    trials: int = TRIALS_PER_CELL,
) -> dict[str, Any]:
    payload_kb = estimate_payload_kb(
        [{"role": "user", "content": user_message}], tools, system
    )
    successes = 0
    errors: list[str] = []
    elapsed: list[float] = []
    print(f"  cell={label} payload≈{payload_kb}KB tools={len(tools)} trials={trials}")
    for t in range(trials):
        success, err, sec = await probe_once(
            provider,
            system=system,
            user_message=user_message,
            tools=tools,
            conversation_id=f"{conversation_id_prefix}-{t}",
        )
        elapsed.append(sec)
        if success:
            successes += 1
            print(f"    trial {t+1}: OK ({sec:.1f}s)")
        else:
            errors.append(err)
            print(f"    trial {t+1}: FAIL ({sec:.1f}s) {err[:80]}")
        await asyncio.sleep(SLEEP_BETWEEN)
    summary = {
        "label": label,
        "payload_kb": payload_kb,
        "tool_count": len(tools),
        "trials": trials,
        "successes": successes,
        "failures": trials - successes,
        "elapsed_avg_s": sum(elapsed) / max(len(elapsed), 1),
        "first_error": errors[0] if errors else "",
    }
    print(f"  → {successes}/{trials} succeeded\n")
    return summary


async def main() -> None:
    provider = OpenAICodexProvider(credential=resolve_openai_codex_credential())
    # Trigger token validation up front.
    await provider._ensure_valid_token()
    print(f"Codex token loaded. Model: {MODEL}\n")

    system_prompt = (
        "You are a helpful assistant probing the Codex backend. Reply to the "
        "user's message in 1-2 sentences. Use a tool only if needed."
    )
    user_msg = "Just say hi briefly. No tool needed."

    results: list[dict[str, Any]] = []
    conv_id_base = f"codex-probe-{int(time.time())}"

    # ---------------------------------------------------------------
    # SWEEP 1 — payload size via tool count (1KB schemas each)
    # ---------------------------------------------------------------
    print("=" * 70)
    print("SWEEP 1 — tool count vs failure rate (~1KB per tool)")
    print("=" * 70)
    for n_tools in [0, 5, 10, 15, 20, 25, 30, 40]:
        tools = make_tools(n_tools, padding_chars=600)
        summary = await run_cell(
            provider,
            f"sweep1.tools={n_tools}",
            system=system_prompt,
            user_message=user_msg,
            tools=tools,
            conversation_id_prefix=f"{conv_id_base}-sw1-{n_tools}",
        )
        results.append(summary)

    # ---------------------------------------------------------------
    # SWEEP 2 — payload size via per-tool padding (5 tools, varying size)
    # ---------------------------------------------------------------
    print("=" * 70)
    print("SWEEP 2 — per-tool size vs failure (5 tools each)")
    print("=" * 70)
    for pad in [0, 1000, 3000, 6000, 10000, 15000]:
        tools = make_tools(5, padding_chars=pad)
        summary = await run_cell(
            provider,
            f"sweep2.pad={pad}c",
            system=system_prompt,
            user_message=user_msg,
            tools=tools,
            conversation_id_prefix=f"{conv_id_base}-sw2-{pad}",
        )
        results.append(summary)

    # ---------------------------------------------------------------
    # SUMMARY TABLE
    # ---------------------------------------------------------------
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"{'label':<25} {'payload_kb':>10} {'tools':>6} {'pass':>6} {'avg_s':>7}  first_error")
    print("-" * 100)
    for r in results:
        rate = f"{r['successes']}/{r['trials']}"
        print(
            f"{r['label']:<25} {r['payload_kb']:>10} {r['tool_count']:>6} "
            f"{rate:>6} {r['elapsed_avg_s']:>6.1f}s  {r['first_error'][:50]}"
        )

    # Persist for follow-up analysis.
    out_path = "data/diagnostics/codex_tipping_point.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"model": MODEL, "results": results}, f, indent=2)
    print(f"\nResults saved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
