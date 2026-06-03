#!/usr/bin/env python3
"""Debug harness: reproduce the claude-acp turn-termination hang in
isolation with EXHAUSTIVE, timestamped logging.

Runs the EXACT acpx invocation the improvement loop uses
(`acpx --cwd <ws> --format json --approve-all claude exec <prompt>`)
plus `--verbose`, against a throwaway clone of the repo, capturing every
stdout (NDJSON) + stderr line prefixed with elapsed seconds. Lets us see
precisely what claude-acp does AFTER the `usage_update` event — whether
it emits anything, errors silently, or simply goes quiet until the idle
window. Non-destructive (temp workspace, deleted on exit unless --keep).

Usage:
  python scripts/debug_claude_acp_hang.py [--timeout 360] [--keep]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO = "/home/k/Kernos"
PROMPT = (
    # Mirror the live impl_author prompt's SHAPE: force real fs/write
    # tool calls (the trigger correlated with the hang) + the STATUS
    # convention, so the harness reproduces the live failure path.
    "You are implementing a change in this worktree. "
    "1) Edit kernos/kernel/tool_aliases.py to add a short module "
    "docstring explaining the alias map (add only; keep existing). "
    "2) Write a new file impl_notes.md summarizing what you changed. "
    "Use your file-editing tools to actually make these edits. "
    "End your response with a single final line exactly: STATUS: GREEN"
)
ACPX = os.environ.get("KERNOS_ACPX_BINARY", "/home/k/.npm-global/bin/acpx")


async def _pump(stream, tag: str, start: float, logf) -> int:
    n = 0
    while True:
        line = await stream.readline()
        if not line:
            break
        n += 1
        elapsed = time.monotonic() - start
        text = line.decode("utf-8", errors="replace").rstrip("\n")
        row = f"[{elapsed:7.1f}s {tag}] {text}"
        print(row, flush=True)
        logf.write(row + "\n")
        logf.flush()
    return n


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=360.0)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    ws = tempfile.mkdtemp(prefix="acpx_debug_ws_")
    log_path = f"/tmp/claude_acp_hang_{int(time.time())}.log"
    print(f"workspace: {ws}\nlog: {log_path}\n", flush=True)
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", "1",
         f"file://{REPO}", ws], check=True,
    )

    env = {
        **os.environ,
        # Best-effort: ask every layer for maximum verbosity. Harmless
        # if a given binary ignores its knob.
        "ANTHROPIC_LOG": "debug",
        "DEBUG": "*",
        "RUST_LOG": "debug",
        "ACP_LOG": "debug",
        "GIT_TERMINAL_PROMPT": "0",
    }
    cmd = [
        ACPX, "--verbose", "--cwd", ws, "--format", "json",
        "--approve-all", "claude", "exec", PROMPT,
    ]
    print("CMD: " + " ".join(cmd[:-1]) + " <prompt>\n", flush=True)

    start = time.monotonic()
    with open(log_path, "w", encoding="utf-8") as logf:
        logf.write(f"cmd: {cmd}\nworkspace: {ws}\n\n")
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env,
        )
        out_t = asyncio.create_task(_pump(proc.stdout, "OUT", start, logf))
        err_t = asyncio.create_task(_pump(proc.stderr, "ERR", start, logf))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=args.timeout)
        except asyncio.TimeoutError:
            timed_out = True
            print(f"\n=== TIMEOUT at {args.timeout}s — killing ===",
                  flush=True)
            try:
                proc.kill()
            except Exception:
                pass
        n_out, n_err = await asyncio.gather(out_t, err_t)
        last = time.monotonic() - start
        summary = (
            f"\n=== SUMMARY ===\n"
            f"exit: {'TIMED_OUT' if timed_out else proc.returncode}\n"
            f"stdout_lines: {n_out}  stderr_lines: {n_err}\n"
            f"total_wall: {last:.1f}s\n"
            f"workspace diff (did it edit anything?):\n"
        )
        print(summary, flush=True)
        logf.write(summary)
        diff = subprocess.run(
            ["git", "-C", ws, "--no-pager", "diff", "--stat"],
            capture_output=True, text=True,
        ).stdout
        print(diff, flush=True)
        logf.write(diff)

    if not args.keep:
        shutil.rmtree(ws, ignore_errors=True)
    print(f"\nFull log: {log_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
