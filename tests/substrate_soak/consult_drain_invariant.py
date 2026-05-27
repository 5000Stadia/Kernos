"""Probe 3 — consult_drain_invariant (SUBSTRATE-SELF-TEST-V1).

Spawns a real local subprocess that emits a single NDJSON line
larger than 64 KiB on stdout. Drains it via _read_lines_unbounded
(the helper that fixes the bug). Asserts no LimitOverrunError
crash + full content arrives intact.

Regression bug: dbfbdab. ACPX stdout drain crashed on lines
>64 KiB; surfaced as opaque ConsultationFailed at the substrate.
"""
from __future__ import annotations

import asyncio
import time

from kernos.kernel.self_test_gate import ProbeResult


REQUIRED_BEHAVIORAL_KEYS = frozenset({
    "dispatch_return_value",
    "accumulated_content_length",
})

REQUIRED_SUBSTRATE_KEYS = frozenset({
    "largest_line_bytes_observed",
    "drain_completed_without_exception",
})


# Synthetic ACPX-shaped NDJSON line: a single session/update event
# whose agent_message_chunk content is 80 KiB of repeated bytes.
# Matches the shape claude-code emits when asked to read a large
# file inline.
_GIANT_PAYLOAD_BYTES = 80 * 1024  # 80 KiB


async def run_probe() -> ProbeResult:
    start = time.monotonic()

    # Lazy import so monkeypatch.setattr on
    # acpx_adapter._read_lines_unbounded reaches the actual
    # function this probe uses (a module-level `from ... import`
    # would bind to the original at probe-module load time and
    # bypass mutations applied later).
    from kernos.kernel.external_agents.acpx_adapter import (
        _read_lines_unbounded,
    )

    # Spawn a Python subprocess that prints one giant NDJSON line
    # then exits. The exact event shape doesn't matter for this
    # probe — only that the LINE LENGTH exceeds asyncio's default
    # 64 KiB StreamReader limit.
    payload_size = _GIANT_PAYLOAD_BYTES
    # Build the script as a one-liner. The payload is "X" * size.
    # Print it followed by a newline to terminate the line.
    script = (
        f'import sys; '
        f'sys.stdout.write("{{\\"giant\\":\\"" + "X" * {payload_size} '
        f'+ "\\"}}\\n"); '
        f'sys.stdout.flush()'
    )

    drain_completed_without_exception = True
    drain_exception_text = ""
    largest_line_bytes = 0
    collected_lines: list[bytes] = []

    proc = await asyncio.create_subprocess_exec(
        "python3", "-c", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    try:
        async for line in _read_lines_unbounded(proc.stdout):
            collected_lines.append(line)
            if len(line) > largest_line_bytes:
                largest_line_bytes = len(line)
    except Exception as exc:
        drain_completed_without_exception = False
        drain_exception_text = f"{type(exc).__name__}: {exc}"

    await proc.wait()
    accumulated_content_length = sum(len(line) for line in collected_lines)

    duration_ms = int((time.monotonic() - start) * 1000)

    # Pass conditions:
    # - drain completed without exception (LimitOverrunError would
    #   set this False; this is the core bug repro)
    # - largest line observed >= payload size (proves the drain
    #   actually saw the giant line, not just truncated silently)
    # - accumulated content >= payload size (proves nothing was lost)
    cond_no_exception = drain_completed_without_exception
    # The script writes payload_size "X"s plus the surrounding
    # JSON envelope (~15 bytes of {"giant":"..."}). Allow some
    # slack for the envelope but require ≥ payload_size.
    cond_largest_line = largest_line_bytes >= payload_size
    cond_accumulated = accumulated_content_length >= payload_size

    all_passed = cond_no_exception and cond_largest_line and cond_accumulated

    failure_reason = ""
    if not all_passed:
        failed = []
        if not cond_no_exception:
            failed.append(
                f"drain_raised ({drain_exception_text})"
            )
        if not cond_largest_line:
            failed.append(
                f"largest_line_too_small "
                f"({largest_line_bytes}B < {payload_size}B threshold)"
            )
        if not cond_accumulated:
            failed.append(
                f"content_lost "
                f"(accumulated {accumulated_content_length}B < "
                f"expected {payload_size}B)"
            )
        failure_reason = (
            f"consult-drain invariant violated: {', '.join(failed)}. "
            f"Likely regression of dbfbdab "
            f"(ACPX-DRAIN-OVERRUN-FIX-V1) — the drain code reverted "
            f"to `async for line in proc.stdout` instead of the "
            f"chunk-and-split helper."
        )

    # If we got a clean drain return, summarize the value;
    # otherwise quote the exception text.
    if drain_completed_without_exception:
        dispatch_return_value = (
            f"clean_drain ({len(collected_lines)} line(s), "
            f"{accumulated_content_length} bytes)"
        )
    else:
        dispatch_return_value = f"RAISED: {drain_exception_text}"

    return ProbeResult(
        probe_name="consult_drain_invariant",
        passed=all_passed,
        behavioral_evidence={
            "dispatch_return_value": dispatch_return_value,
            "accumulated_content_length": accumulated_content_length,
        },
        substrate_evidence={
            "largest_line_bytes_observed": largest_line_bytes,
            "drain_completed_without_exception": (
                drain_completed_without_exception
            ),
        },
        duration_ms=duration_ms,
        failure_reason=failure_reason,
    )
