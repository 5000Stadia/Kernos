#!/usr/bin/env python3
"""PHASE-1-WIPE-VERIFICATION — eight-probe audit driver.

Spec: https://www.notion.so/358ffafef4db8102a4a6c615e2ab278f

After REFERENCE-CATALOG-BAKED-V1 + the hatching-block gate fix
shipped, the architect requested a clean-baseline /wipe verification:
boot Kernos against a fresh data directory and exercise eight
probes covering opener, capability surfacing, cohort/domain
awareness, discoverability fallback, documentation map reach,
substrate advocacy, investigation-vs-vibes posture, and identity-
vs-platform framing.

This driver mirrors the REPL boot path (``kernos.repl.build_dev_handler``)
so the substrate is production-shape — same providers, same chains,
same cohorts, same handler pipeline. The only divergence is the
adapter layer: instead of Discord, the driver constructs eight
``NormalizedMessage`` objects in sequence, calls ``handler.process``
for each, captures the verbatim response, and snapshots a log slice
for trace evidence.

Output: a markdown transcript at
``data/diagnostics/live-tests/PHASE-1-WIPE-VERIFICATION-<timestamp>.md``
suitable for pasting into the closeout.

Usage::

    python scripts/phase_1_wipe_verification.py

The script defaults the data dir to a fresh temp directory
(simulating a clean wipe) so production state is never touched.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import tempfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# .env load so KERNOS_LLM_PROVIDER and friends populate before chains build.
try:
    from dotenv import load_dotenv as _load_dotenv  # noqa: E402
    _load_dotenv()
except ImportError:
    pass


PROBES = [
    ("Probe 1 — Opener (no leading)", "Hey 👋"),
    (
        "Probe 2 — Tools and capabilities",
        "I'm developing Kernos and I want to suss out your onboarding. "
        "What tools do you have access to? In this space what can you do?",
    ),
    (
        "Probe 3 — Cohort/domain awareness (the original gap)",
        "How do cohorts work? And how do they relate to domains?",
    ),
    (
        "Probe 4 — Discoverability fallback (recovery patch #1)",
        "Tell me about flugelhornic discharge in the gate",
    ),
    (
        "Probe 5 — Documentation map reach (recovery patch #3)",
        "What's the lay of the land with kernos documentation? "
        "How do I (or you) find what's there?",
    ),
    (
        "Probe 6 — Substrate advocacy (orientation prompt's load-bearing piece)",
        "Looking at your context here & tool use and such, anything unclear "
        "that could surface cleaner?",
    ),
    (
        "Probe 7 — Investigation-vs-vibes (Kit's posture probe)",
        "I'm not sure how Kernos handles destructive writes. Can you explain?",
    ),
    ("Probe 8 — Identity-vs-platform", "What are you?"),
]


# Trace markers worth surfacing per-probe in the log slice. The driver
# captures every log line emitted between the start and end of each
# probe; this list narrows the slice into actionable signal.
TRACE_MARKERS = (
    "TOOL_CALLED",
    "TOOL_RESULT",
    "DISPATCHER_AUDIT",
    "REQUEST_REFERENCE",
    "REFERENCE_BAKED",
    "REFERENCE_FIRST_BOOT",
    "MESSAGE_ANALYSIS",
    "TOOL_SURFACING",
    "TOOL_BUDGET",
    "ROUTE",
    "TURN_TIMING",
    "TURN_SUBMITTED",
    "PHASE_TIMING",
    "PRIMARY",  # presence renderer phase
    "INTEGRATION",
    "DIRECTIVE",
    "CHAIN",
    "SIMPLE_RESPONSE",
)


class _RingHandler(logging.Handler):
    """Capture every log record into a deque.

    Driver uses index slicing per-probe to extract the records emitted
    during that probe's turn.
    """

    def __init__(self, maxlen: int = 50000) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: deque[logging.LogRecord] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        self.records.append(record)


def _slice_records(
    records: deque[logging.LogRecord],
    start_index: int,
    end_index: int,
) -> list[logging.LogRecord]:
    out: list[logging.LogRecord] = []
    for i, rec in enumerate(records):
        if start_index <= i < end_index:
            out.append(rec)
    return out


def _filter_trace_signal(records: list[logging.LogRecord]) -> list[str]:
    """Keep only records whose message contains a trace marker."""
    out: list[str] = []
    for rec in records:
        try:
            msg = rec.getMessage()
        except Exception:
            continue
        if any(marker in msg for marker in TRACE_MARKERS):
            out.append(f"{rec.levelname:7} {rec.name}: {msg}")
    return out


async def run() -> int:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = _REPO_ROOT / "data" / "diagnostics" / "live-tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"PHASE-1-WIPE-VERIFICATION-{timestamp}.md"

    # Fresh data directory — never touch the user's ./data
    data_dir = Path(tempfile.mkdtemp(prefix="kernos_wipe_test_"))
    instance_id = "repl:wipe_verification"

    # Capture log records into a ring buffer so we can slice per-probe.
    ring = _RingHandler()
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(ring)
    # Also surface progress to stderr so the operator can watch.
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.INFO)
    stderr_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root_logger.addHandler(stderr_handler)

    # Boot Kernos via the dev-handler seam — same wiring as production
    # minus Discord/SMS adapters.
    from kernos.repl import build_dev_handler

    print(f"\n=== PHASE-1-WIPE-VERIFICATION (data_dir={data_dir}) ===\n",
          file=sys.stderr, flush=True)

    pre_boot_index = len(ring.records)
    handler = await build_dev_handler(
        data_dir=str(data_dir),
        instance_id=instance_id,
        sender="operator",
        sender_display_name="bananapancake",
    )
    post_boot_index = len(ring.records)

    boot_records = _slice_records(ring.records, pre_boot_index, post_boot_index)
    boot_trace = _filter_trace_signal(boot_records)

    # Identify the registered REPL channel for the operator member so
    # the NormalizedMessage matches handler._resolve_member's lookup.
    from kernos.repl import select_member, _build_message

    identity = await select_member(handler, explicit_sender="operator")

    transcript_sections: list[str] = []
    transcript_sections.append(
        f"# PHASE-1-WIPE-VERIFICATION — {timestamp}\n\n"
        f"**Driver:** `scripts/phase_1_wipe_verification.py`  \n"
        f"**Data dir:** `{data_dir}` (fresh; production state untouched)  \n"
        f"**Instance ID:** `{instance_id}`  \n"
        f"**Code:** `main` branch with REFERENCE-CATALOG-BAKED-V1 "
        f"(`652ec86`) + hatching-block gate fix (`b4105b3`) shipped.  \n"
        f"\n"
    )

    transcript_sections.append("## Boot trace\n\n")
    if boot_trace:
        transcript_sections.append("```\n" + "\n".join(boot_trace) + "\n```\n\n")
    else:
        transcript_sections.append(
            "_(no markers matched during boot — see verbose log)_\n\n"
        )

    failures: list[str] = []
    for label, content in PROBES:
        probe_start = len(ring.records)
        message = _build_message(
            content, instance_id=instance_id, identity=identity,
        )
        try:
            response = await handler.process(message)
            response_text = response if isinstance(response, str) else repr(response)
            error = None
        except Exception as exc:  # noqa: BLE001 — driver must not raise
            response_text = ""
            error = repr(exc)
            failures.append(f"{label}: handler.process raised {error}")
        probe_end = len(ring.records)

        probe_records = _slice_records(ring.records, probe_start, probe_end)
        probe_trace = _filter_trace_signal(probe_records)

        transcript_sections.append(f"## {label}\n\n")
        transcript_sections.append("**Sent:**\n\n")
        transcript_sections.append("> " + content.replace("\n", "\n> ") + "\n\n")
        transcript_sections.append("**Response:**\n\n")
        if error:
            transcript_sections.append(f"_(error: {error})_\n\n")
        elif response_text:
            transcript_sections.append(
                "```\n" + response_text.rstrip() + "\n```\n\n"
            )
        else:
            transcript_sections.append("_(empty response)_\n\n")
        transcript_sections.append("**Trace markers:**\n\n")
        if probe_trace:
            transcript_sections.append(
                "```\n" + "\n".join(probe_trace) + "\n```\n\n"
            )
        else:
            transcript_sections.append("_(no markers matched)_\n\n")

    # Final summary
    transcript_sections.append("## Driver summary\n\n")
    transcript_sections.append(
        f"- Probes attempted: {len(PROBES)}\n"
        f"- Driver-level failures: {len(failures)}\n"
    )
    if failures:
        transcript_sections.append("\n**Failures:**\n\n")
        for f in failures:
            transcript_sections.append(f"- {f}\n")
    transcript_sections.append(
        "\n_The audit pass/fail call is a human judgment against the "
        "criteria in https://www.notion.so/358ffafef4db8102a4a6c615e2ab278f. "
        "This driver provides verbatim responses + trace markers; it does "
        "NOT score the probes itself._\n"
    )

    report_path.write_text("".join(transcript_sections), encoding="utf-8")
    print(f"\n=== TRANSCRIPT: {report_path} ===\n", file=sys.stderr, flush=True)

    # Cleanup the temp data dir
    try:
        shutil.rmtree(data_dir)
    except Exception:
        pass

    return 0 if not failures else 1


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
