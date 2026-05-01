"""Single source of truth for Kernos capability status.

CLEANUP-BATCH-V1 items 5 + 9. The README capability matrix and the
``/capabilities`` slash command both read from this module so they
cannot drift apart. Status descriptions are authored here; surfaces
that display them render from this list, never hand-maintain a copy.

Maintenance:

* When a capability ships, flip its status to LIVE.
* When a capability moves between Partial / Experimental / Planned,
  update its row here. The README and ``/capabilities`` pick up the
  new value at the next render / next call respectively.
* Keep ``surface_area`` short — it answers "what does this give the
  user / agent" in one phrase, not "how is it implemented."
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CapabilityStatus(str, Enum):
    """Status enum used by README rendering and ``/capabilities``."""

    LIVE = "Live"
    PARTIAL = "Partial"
    EXPERIMENTAL = "Experimental"
    PLANNED = "Planned"


@dataclass(frozen=True)
class CapabilitySpec:
    """One row of the capability matrix.

    ``name`` — short label (e.g. "External-agent consultation").
    ``status`` — Live / Partial / Experimental / Planned.
    ``surface_area`` — single phrase describing what the capability
    gives the user or agent.
    ``notes`` — short detail string. Empty when nothing nuanced needs
    saying. Used for caveats ("Aider build mode only", etc.).
    """

    name: str
    status: CapabilityStatus
    surface_area: str
    notes: str = ""


# Authoritative capability list. Trimmed to ~14 highest-impact rows
# per CLEANUP-BATCH-V1 kick-back guidance. Aspirational cohorts and
# adapter sub-rows live in the roadmap, not here.
CAPABILITIES: tuple[CapabilitySpec, ...] = (
    # ---- User-facing surfaces -------------------------------------------
    CapabilitySpec(
        name="Messaging adapters",
        status=CapabilityStatus.LIVE,
        surface_area="Conversational turns over Discord, Twilio SMS, Telegram, and CLI",
        notes="Discord-native slash commands (e.g. /debug); text commands universal across adapters",
    ),
    CapabilitySpec(
        name="Workspace code execution",
        status=CapabilityStatus.LIVE,
        surface_area="Agent writes Python in a subprocess with best-effort isolation; registers tools",
        notes="Best-effort isolation, not a hard sandbox; hostile code can escape via ctypes",
    ),
    CapabilitySpec(
        name="Builder: Aider",
        status=CapabilityStatus.LIVE,
        surface_area="code_exec(backend='aider') hands task-shaped CLI work to Aider",
        notes="Build mode only; consult is unsupported",
    ),
    CapabilitySpec(
        name="External-agent consultation",
        status=CapabilityStatus.LIVE,
        surface_area="Agent invokes Claude Code / Codex / Gemini for review or task delegation",
        notes="consult tool + code_exec(backend=...); reentrancy guard scopes by calling context",
    ),
    # ---- Memory + automation --------------------------------------------
    CapabilitySpec(
        name="Memory recall + compaction",
        status=CapabilityStatus.LIVE,
        surface_area="remember tool over accumulated knowledge; ledger + facts + personality at compaction",
        notes="Bjork dual-strength ranking; FTS5 over event stream parked for follow-on",
    ),
    CapabilitySpec(
        name="Workflow loops (WLP)",
        status=CapabilityStatus.LIVE,
        surface_area="Approval-gated workflow execution, restart-resume, action library",
    ),
    CapabilitySpec(
        name="AgentInbox",
        status=CapabilityStatus.LIVE,
        surface_area="Workflow route_to_agent verb persists into the AgentInbox",
        notes="Provider unavailability surfaces as typed AgentInboxUnavailable",
    ),
    CapabilitySpec(
        name="Scheduler + triggers",
        status=CapabilityStatus.LIVE,
        surface_area="Time-based + event-driven trigger evaluation; manage_schedule tool",
    ),
    # ---- Cohorts (high-impact ones; future cohorts live in roadmap) -----
    CapabilitySpec(
        name="Cohort: Drafter v2",
        status=CapabilityStatus.LIVE,
        surface_area="Tool-starved cohort that consumes shared context surfaces and proposes drafts",
    ),
    CapabilitySpec(
        name="Cohort: Friction Observer",
        status=CapabilityStatus.LIVE,
        surface_area="Post-turn signal detection for friction patterns + diagnostic reports",
    ),
    CapabilitySpec(
        name="Cohort: Stewardship + sensitivity",
        status=CapabilityStatus.LIVE,
        surface_area="Value extraction + tension detection at compaction; sensitivity classification at harvest",
    ),
    # ---- Substrate ------------------------------------------------------
    CapabilitySpec(
        name="Event stream durability",
        status=CapabilityStatus.LIVE,
        surface_area="Per-instance SQLite event stream with flush + graceful-shutdown guarantees",
        notes="Durable after flush; up to 2 seconds of in-flight events lost on ungraceful crash",
    ),
    CapabilitySpec(
        name="Multi-member identity",
        status=CapabilityStatus.LIVE,
        surface_area="Per-member profiles, spaces, conversations, hatching, relationships",
    ),
    # ---- In-flight ------------------------------------------------------
    CapabilitySpec(
        name="Decoupled turn runner",
        status=CapabilityStatus.PARTIAL,
        surface_area="Thin-path turns succeed; full-machinery dispatch awaits workshop binding",
        notes="INTEGRATION-WIRE-LIVE-WORKSHOP-BINDING follow-up; loud-error placeholders mark the seam",
    ),
    # ---- Planned (named follow-on specs) --------------------------------
    CapabilitySpec(
        name="Domain pass",
        status=CapabilityStatus.PLANNED,
        surface_area="Agent or workflow acts inside another space without the user manually entering it",
        notes="KERNOS-DOMAIN-PASS v1 follow-on spec",
    ),
)


def render_markdown_table() -> str:
    """Render the capability matrix as a Markdown table for README.

    The README copy is regenerated from this function at batch ship
    time. Run from a Python repl when the list changes:

        from kernos.kernel.capabilities import render_markdown_table
        print(render_markdown_table())

    Output format keeps each row to one line for readability."""
    lines = [
        "| Capability | Status | What it gives | Notes |",
        "| --- | --- | --- | --- |",
    ]
    for cap in CAPABILITIES:
        notes = cap.notes or "—"
        lines.append(
            f"| {cap.name} | {cap.status.value} | {cap.surface_area} | {notes} |"
        )
    return "\n".join(lines)


def render_status_text() -> str:
    """Render the capability matrix as plain text for the
    ``/capabilities`` slash command output. Compact format suited
    for chat reply: name + status + one-line surface, with notes
    appended when present."""
    lines: list[str] = []
    by_status: dict[CapabilityStatus, list[CapabilitySpec]] = {}
    for cap in CAPABILITIES:
        by_status.setdefault(cap.status, []).append(cap)

    for status in CapabilityStatus:
        rows = by_status.get(status, [])
        if not rows:
            continue
        lines.append(f"**{status.value}**")
        for cap in rows:
            note_suffix = f" — {cap.notes}" if cap.notes else ""
            lines.append(f"  - {cap.name}: {cap.surface_area}{note_suffix}")
        lines.append("")
    return "\n".join(lines).rstrip()


__all__ = [
    "CAPABILITIES",
    "CapabilitySpec",
    "CapabilityStatus",
    "render_markdown_table",
    "render_status_text",
]
