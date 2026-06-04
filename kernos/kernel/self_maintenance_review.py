"""SELF-MAINTENANCE-REVIEW-V1 — KERNOS's daily self-stewardship review.

Once a day, KERNOS holds ONE slice of its own code + systems up to the light
through two lenses:

  * **Corrective** — is this still the healthiest implementation of its
    intention, or has it drifted / decayed / grown an unguarded edge?
  * **Generative** — even when healthy, is there a more efficient or effective
    way, and does this function's validity + role still hold up against the
    overarching intention of the WHOLE KERNOS system?

It produces a short, honest report and surfaces it as a whisper to the main
agent **to consider** — never to act on autonomously. Every actual change still
flows through approval-gated ``improve_kernos``. Thoughtful evolution, not
out-of-hand mutation: at most ONE minor, reversible, well-justified evolution
idea per review.

Design mirrors recursive_self_heal: seam-injected (consult_fn / whisper_fn),
inert unless ``KERNOS_SELF_MAINTENANCE_REVIEW`` is set (default OFF for v1),
deterministic + unit-testable. The orchestration (``maybe_run_daily``) is
idle-aware and runs at most once per 24h.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Kill switch (default OFF for v1) + cadence
# ---------------------------------------------------------------------------

MIN_HOURS_BETWEEN_REVIEWS = 20.0  # "~once a day", with slack so a slightly
#                                   early daily tick still fires.
DEDUP_TTL_DAYS = 14.0  # don't re-surface the same observation for two weeks.


def is_enabled() -> bool:
    """v1 is inert unless explicitly enabled — a new (even read-only) recurring
    behavior ships default-off and is turned on after one watched cycle."""
    return os.environ.get("KERNOS_SELF_MAINTENANCE_REVIEW", "").strip().lower() in (
        "1", "true", "on", "yes",
    )


# ---------------------------------------------------------------------------
# The rotating slices — one reviewed per day, cursor advances. Over ~a week
# the whole system is covered. Each carries an intent pointer so the review
# reads intention (docs/spec) against as-built code.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewSlice:
    name: str
    intent: str           # the documented intention, in one line
    paths: tuple[str, ...]  # the as-built code to read
    constitutional: bool = False  # self-governance/maintenance machinery —
    #   reviewable + ponderable, but any evolution is HUMAN-GATED (never
    #   self-applied). Nothing is exempt from review; the methodology audits
    #   itself, but it cannot quietly rewrite its own rules.


REVIEW_SLICES: tuple[ReviewSlice, ...] = (
    ReviewSlice(
        "message-pipeline",
        "Turn pipeline + context assembly: provision→route→assemble→reason→"
        "consequence→persist, adapter/handler isolation.",
        ("kernos/messages/handler.py",),
    ),
    ReviewSlice(
        "reasoning",
        "Tool loop, provider chains, kernel-tool dispatch, cost logging.",
        ("kernos/kernel/reasoning.py", "kernos/providers/chains.py"),
    ),
    ReviewSlice(
        "state-store",
        "Instance state: per-member profiles, relationships, abuse prevention "
        "— the runtime query surface keyed to instance_id.",
        ("kernos/kernel/instance_db.py",),
    ),
    ReviewSlice(
        "stewardship",
        "Compaction harvest: value extraction, tension detection, sensitivity "
        "classification, operational insights as whispers.",
        ("kernos/kernel/compaction.py", "kernos/kernel/fact_harvest.py"),
    ),
    ReviewSlice(
        "awareness",
        "Whispers + suppression: surface insight only when there's a concrete "
        "actionable idea; ambient, not demanding.",
        ("kernos/kernel/awareness.py",),
    ),
    ReviewSlice(
        "dispatch-gate",
        "Action-based tool classification + scoped amortization at the "
        "dispatch boundary; proportional caution on user data.",
        ("kernos/kernel/gate.py", "kernos/kernel/spaces.py"),
    ),
    ReviewSlice(
        "improvement-loop",
        "Autonomous self-improvement: spec→impl→approval→commit→deploy→verify, "
        "with request-fidelity + proportionality.",
        ("kernos/kernel/improvement_loop_workflow.py",
         "kernos/kernel/improvement_review_protocol.py"),
    ),
    ReviewSlice(
        "workflows",
        "Background trigger-driven workflows on the event-stream post-flush "
        "hook; compose existing surfaces, no parallel substrate.",
        ("kernos/kernel/workflows/",),
    ),
    # --- The methodology reviews itself (constitutional: human-gated) ------
    ReviewSlice(
        "self-maintenance-methodology",
        "HOW KERNOS reviews + evolves itself: the daily two-lens review, the "
        "request-fidelity + proportionality gates, the evolution discipline "
        "(thoughtful, one minor step at a time). Is the way I improve myself "
        "still the healthiest, most effective approach, and does it serve the "
        "whole? Nothing is exempt — the methodology audits itself.",
        ("kernos/kernel/self_maintenance_review.py",
         "kernos/kernel/improvement_review_protocol.py",
         "specs/SELF-MAINTENANCE-REVIEW-V1.md"),
        constitutional=True,
    ),
    ReviewSlice(
        "self-healing",
        "The bounded recovery lane: classify machinery-vs-task failure, the "
        "durable runaway bound, constitutional guard, hermetic verification. "
        "Is recovery still bounded, legible, and proportionate?",
        ("kernos/kernel/recursive_self_heal.py",),
        constitutional=True,
    ),
    ReviewSlice(
        "governing-intention",
        "The constitution the rest serves: operating principles, identity, "
        "hatching guidance, conservative-by-default posture. Does the lived "
        "system still embody these, and do they still serve the whole?",
        ("kernos/kernel/template.py",),
        constitutional=True,
    ),
)


def slice_for_cursor(cursor: int) -> ReviewSlice:
    return REVIEW_SLICES[cursor % len(REVIEW_SLICES)]


# ---------------------------------------------------------------------------
# Durable cursor + dedup state (a small JSON in data_dir)
# ---------------------------------------------------------------------------


def _state_path(data_dir: str) -> Path:
    return Path(data_dir) / "self_maintenance_review.json"


def load_state(data_dir: str) -> dict:
    p = _state_path(data_dir)
    if not p.exists():
        return {"cursor": 0, "last_run_iso": "", "seen": {}}
    try:
        data = json.loads(p.read_text())
        data.setdefault("cursor", 0)
        data.setdefault("last_run_iso", "")
        data.setdefault("seen", {})
        return data
    except Exception:
        return {"cursor": 0, "last_run_iso": "", "seen": {}}


def save_state(data_dir: str, state: dict) -> None:
    _state_path(data_dir).write_text(json.dumps(state, separators=(",", ":")))


def _hours_between(a_iso: str, b_iso: str) -> float | None:
    """Hours from a_iso to b_iso, or None if either is unparseable."""
    from datetime import datetime

    try:
        a = datetime.fromisoformat(a_iso)
        b = datetime.fromisoformat(b_iso)
    except (ValueError, TypeError):
        return None
    return (b - a).total_seconds() / 3600.0


def due_for_review(state: dict, now_iso: str) -> bool:
    last = state.get("last_run_iso") or ""
    if not last:
        return True
    gap = _hours_between(last, now_iso)
    if gap is None:
        return True
    return gap >= MIN_HOURS_BETWEEN_REVIEWS


# ---------------------------------------------------------------------------
# The two-lens review prompt + parsing
# ---------------------------------------------------------------------------


def build_review_prompt(slice_: ReviewSlice) -> str:
    """The single bounded reasoning consult: read intent + as-built, assess
    through both lenses, honour the evolution discipline."""
    paths = "\n".join(f"  - {p}" for p in slice_.paths)
    constitutional_note = ""
    if slice_.constitutional:
        constitutional_note = (
            "\nNOTE — this slice IS part of your self-governance / maintenance "
            "machinery (how you review, heal, and govern yourself). Review and "
            "ponder it as freely and honestly as any other — nothing is exempt "
            "— but any evolution here is CONSTITUTIONAL: it is human-gated and "
            "must NOT be self-applied. Frame any idea as something for the "
            "founder to weigh, not something to route into an autonomous "
            "change.\n"
        )
    return (
        "You are KERNOS performing your DAILY SELF-MAINTENANCE REVIEW of one "
        f"slice of yourself: `{slice_.name}`.\n\n"
        f"Documented intention of this slice:\n  {slice_.intent}\n"
        f"{constitutional_note}\n"
        f"As-built code to read (use your source-reading tools):\n{paths}\n\n"
        "Review through TWO lenses:\n\n"
        "1. CORRECTIVE — does the implementation still serve that intention, "
        "or has it drifted / decayed? Dead code, redundancy, an unguarded "
        "failure mode, a violated principle or covenant, a simpler/healthier "
        "shape it should already have?\n\n"
        "2. GENERATIVE (do this EVEN IF the slice is healthy) — is there a more "
        "EFFICIENT or EFFECTIVE way to handle this function? And does this "
        "function's validity and role still hold up against the OVERARCHING "
        "INTENTION OF THE WHOLE KERNOS SYSTEM — is it still pulling its weight, "
        "in the right place, worth its complexity? This is creative, holistic "
        "pondering, not bug-hunting.\n\n"
        "DISCIPLINE (binding): thoughtful evolution, NOT out-of-hand mutation. "
        "Propose AT MOST ONE minor, reversible, well-justified evolution idea "
        "— one step, serving the whole. If nothing is genuinely worth "
        "evolving, propose nothing. Be honest when the slice is healthy and "
        "honest when there's nothing to evolve; do NOT manufacture concerns or "
        "ideas to seem useful.\n\n"
        "End your response with EXACTLY ONE fenced JSON block of this shape:\n"
        "```json\n"
        "{\n"
        '  "overall_health": "healthy" | "minor_concerns" | "needs_attention",\n'
        '  "corrective_findings": ["short finding", ...],\n'
        '  "evolution_idea": "one minor step, or null",\n'
        '  "serves_the_whole": true | false,\n'
        '  "serves_the_whole_why": "one sentence",\n'
        '  "suggested_direction": "what (if anything) you would consider next"\n'
        "}\n"
        "```"
    )


def parse_review(text: str, slice_name: str) -> dict:
    """Parse the trailing JSON block; fall back to a freeform report so a
    malformed block never loses the review."""
    report: dict[str, Any] = {
        "slice": slice_name,
        "overall_health": "unknown",
        "corrective_findings": [],
        "evolution_idea": None,
        "serves_the_whole": None,
        "serves_the_whole_why": "",
        "suggested_direction": "",
        "raw": text.strip()[-4000:],
    }
    block = _last_json_block(text)
    if block is not None:
        for k in (
            "overall_health", "corrective_findings", "evolution_idea",
            "serves_the_whole", "serves_the_whole_why", "suggested_direction",
        ):
            if k in block:
                report[k] = block[k]
    # Enforce the discipline at the parse boundary: at most ONE evolution idea.
    ev = report.get("evolution_idea")
    if isinstance(ev, list):
        report["evolution_idea"] = ev[0] if ev else None
    if not isinstance(report.get("corrective_findings"), list):
        report["corrective_findings"] = (
            [str(report["corrective_findings"])]
            if report.get("corrective_findings") else []
        )
    return report


def _last_json_block(text: str) -> dict | None:
    import re

    matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    for raw in reversed(matches):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


# ---------------------------------------------------------------------------
# Dedup + whisper framing
# ---------------------------------------------------------------------------


def _fingerprint(slice_name: str, finding: str) -> str:
    norm = " ".join(str(finding).lower().split())[:200]
    return hashlib.sha256(f"{slice_name}|{norm}".encode()).hexdigest()[:16]


def filter_seen(report: dict, state: dict, now_iso: str) -> dict:
    """Drop corrective findings + an evolution idea already surfaced within
    the TTL, so the same observation doesn't nag every rotation. Returns a new
    report; mutates state['seen'] with freshly-surfaced fingerprints."""
    seen: dict[str, str] = state.setdefault("seen", {})
    # Expire stale fingerprints.
    fresh = {}
    for fp, iso in seen.items():
        gap = _hours_between(iso, now_iso)
        if gap is None or gap < DEDUP_TTL_DAYS * 24:
            fresh[fp] = iso
    seen.clear()
    seen.update(fresh)

    slice_name = report.get("slice", "")
    kept_findings = []
    for f in report.get("corrective_findings", []):
        fp = _fingerprint(slice_name, f)
        if fp in seen:
            continue
        seen[fp] = now_iso
        kept_findings.append(f)

    ev = report.get("evolution_idea")
    kept_ev = None
    if ev:
        fp = _fingerprint(slice_name, f"evolve:{ev}")
        if fp not in seen:
            seen[fp] = now_iso
            kept_ev = ev

    out = dict(report)
    out["corrective_findings"] = kept_findings
    out["evolution_idea"] = kept_ev
    return out


def has_anything_to_say(report: dict) -> bool:
    """Honest-when-healthy: only surface when there's a fresh finding, an
    evolution idea, or a non-healthy verdict."""
    return bool(
        report.get("corrective_findings")
        or report.get("evolution_idea")
        or report.get("overall_health") not in ("healthy", "unknown", None)
        or report.get("serves_the_whole") is False
    )


def to_whisper_text(report: dict) -> str:
    """Agent-facing framing — a thought to CONSIDER, not an instruction."""
    slice_name = report.get("slice", "?")
    lines = [
        f"Daily self-review of `{slice_name}` "
        f"(health: {report.get('overall_health', 'unknown')}).",
    ]
    findings = report.get("corrective_findings") or []
    if findings:
        lines.append("Corrective notes:")
        lines.extend(f"  • {f}" for f in findings[:5])
    ev = report.get("evolution_idea")
    if ev:
        lines.append(f"One thoughtful evolution to consider: {ev}")
    if report.get("serves_the_whole") is False:
        lines.append(
            "Role check: this may not be earning its place in the whole — "
            f"{report.get('serves_the_whole_why', '')}".rstrip()
        )
    if report.get("constitutional"):
        lines.append(
            "This slice is governance/maintenance machinery — CONSTITUTIONAL. "
            "Raise any idea to the founder to weigh; it is human-gated, not "
            "something to self-apply."
        )
    else:
        lines.append(
            "Consider whether any of this is worth raising to the founder or "
            "proposing as a single minor improvement (through the normal gate). "
            "Thoughtful evolution, one step at a time — no obligation to act."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration — idle-aware, once/24h, behind the kill switch
# ---------------------------------------------------------------------------


async def maybe_run_daily(
    *,
    data_dir: str,
    now_iso: str,
    consult_fn: Callable[..., Any],   # async (prompt) -> str
    whisper_fn: Callable[..., Any] | None = None,  # async (text, report) -> None
    busy: bool = False,
) -> dict:
    """Run today's review iff enabled, not busy, and due. Returns a result
    dict with ``outcome``: disabled | busy | not_due | reviewed_quiet |
    reviewed_surfaced | error."""
    if not is_enabled():
        return {"outcome": "disabled"}
    if busy:
        # Idle-aware: never compete with a live turn or an in-flight attempt.
        return {"outcome": "busy"}

    state = load_state(data_dir)
    if not due_for_review(state, now_iso):
        return {"outcome": "not_due"}

    slice_ = slice_for_cursor(int(state.get("cursor", 0)))
    try:
        text = await consult_fn(build_review_prompt(slice_))
    except Exception as exc:
        return {"outcome": "error", "slice": slice_.name, "error": str(exc)[:200]}

    report = parse_review(text or "", slice_.name)
    report["constitutional"] = slice_.constitutional
    report = filter_seen(report, state, now_iso)
    report["constitutional"] = slice_.constitutional  # filter_seen returns a copy

    surfaced = False
    if has_anything_to_say(report) and whisper_fn is not None:
        try:
            await whisper_fn(to_whisper_text(report), report)
            surfaced = True
        except Exception:
            surfaced = False

    # Advance the cursor + stamp the run + persist dedup, regardless of whether
    # we surfaced (a quiet healthy slice still counts as reviewed).
    state["cursor"] = int(state.get("cursor", 0)) + 1
    state["last_run_iso"] = now_iso
    save_state(data_dir, state)

    return {
        "outcome": "reviewed_surfaced" if surfaced else "reviewed_quiet",
        "slice": slice_.name,
        "report": report,
    }


__all__ = [
    "is_enabled",
    "REVIEW_SLICES",
    "ReviewSlice",
    "slice_for_cursor",
    "load_state",
    "save_state",
    "due_for_review",
    "build_review_prompt",
    "parse_review",
    "filter_seen",
    "has_anything_to_say",
    "to_whisper_text",
    "maybe_run_daily",
]
