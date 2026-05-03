"""ReintegrationContext — capped payload for next-turn integration (PDI C6).

Per the design review edit on PDI: the reintegration payload is hard-capped at
construction, not aspirational. Long action failures otherwise become
prompt bloat at the moment the system is already degraded — exactly
the canonical case where bounded context matters most.

Caps (locked):
  - tool_outcomes_summary: ≤1000 chars
  - discovered_information: ≤500 chars
  - plans_attempted: ≤5 PlanRefs
  - audit_refs: unbounded (references-not-dumps; full traces live
    in audit, not in the payload)

Truncation flag is set at construction. ANY over-cap field that gets
truncated → `truncated=True`. Downstream consumers (next-turn
integration) read the flag and may pull specific audit refs for
deeper context.

Construction is the only enforcement layer: __post_init__ truncates
and flags. There is no path where an over-cap ReintegrationContext
exists in memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from kernos.kernel.enactment.plan import Plan


# ---------------------------------------------------------------------------
# Caps (locked)
# ---------------------------------------------------------------------------


TOOL_OUTCOMES_SUMMARY_CAP = 1000
DISCOVERED_INFORMATION_CAP = 500
PLANS_ATTEMPTED_CAP = 5


# ---------------------------------------------------------------------------
# PlanRef
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanRef:
    """Lightweight plan reference for the reintegration payload.

    References-not-dumps: the audit holds the full Plan; the
    reintegration carries only the id + minimal metadata so the next
    turn's integration can locate the plan if it needs depth without
    inflating the in-prompt payload.
    """

    plan_id: str
    created_via: str
    step_count: int

    @classmethod
    def from_plan(cls, plan: Plan) -> "PlanRef":
        return cls(
            plan_id=plan.plan_id,
            created_via=plan.created_via,
            step_count=len(plan.steps),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "created_via": self.created_via,
            "step_count": self.step_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlanRef":
        return cls(
            plan_id=str(data.get("plan_id", "")),
            created_via=str(data.get("created_via", "")),
            step_count=int(data.get("step_count", 0)),
        )


# ---------------------------------------------------------------------------
# ReintegrationContext
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReintegrationContext:
    """Capped payload stored for the NEXT user turn's integration.

    Per spec Section 5h:
      - original_decided_action_kind: the kind that was attempted (e.g.
        execute_tool). Stored as the enum value string for
        serialisation simplicity.
      - plans_attempted: max 5 PlanRefs (refs only, never embedded
        plans).
      - tool_outcomes_summary: ≤1000 chars; concise summary of step
        outcomes across the turn.
      - discovered_information: ≤500 chars; what was learned that
        invalidated the action.
      - audit_refs: references to full traces; integration consumes
        these if it needs depth.
      - truncated: True when ANY field was clipped at construction.

    The dataclass is frozen so callers cannot accidentally mutate the
    payload after construction. __post_init__ truncates over-cap
    fields and flips `truncated` to True via object.__setattr__ — the
    construction surface can never produce an over-cap instance.

    Used by:
      - B1 surface: stored in the EnactmentOutcome; the next turn's
        wiring picks it up and feeds it to integration alongside the
        user's reply.
      - B2 surface: same mechanism (B2 also stores reintegration
        context for the next turn since same-turn re-entry is
        removed).
    """

    original_decided_action_kind: str
    plans_attempted: tuple[PlanRef, ...] = ()
    tool_outcomes_summary: str = ""
    discovered_information: str = ""
    audit_refs: tuple[str, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        truncated = bool(self.truncated)

        # Truncate tool_outcomes_summary.
        if len(self.tool_outcomes_summary) > TOOL_OUTCOMES_SUMMARY_CAP:
            object.__setattr__(
                self,
                "tool_outcomes_summary",
                self.tool_outcomes_summary[:TOOL_OUTCOMES_SUMMARY_CAP],
            )
            truncated = True

        # Truncate discovered_information.
        if len(self.discovered_information) > DISCOVERED_INFORMATION_CAP:
            object.__setattr__(
                self,
                "discovered_information",
                self.discovered_information[:DISCOVERED_INFORMATION_CAP],
            )
            truncated = True

        # Truncate plans_attempted.
        if not isinstance(self.plans_attempted, tuple):
            object.__setattr__(
                self, "plans_attempted", tuple(self.plans_attempted)
            )
        if len(self.plans_attempted) > PLANS_ATTEMPTED_CAP:
            object.__setattr__(
                self,
                "plans_attempted",
                tuple(self.plans_attempted[:PLANS_ATTEMPTED_CAP]),
            )
            truncated = True

        # Audit refs: unbounded count but each must be a non-empty
        # string. Type coercion to tuple if list was passed.
        if not isinstance(self.audit_refs, tuple):
            object.__setattr__(
                self, "audit_refs", tuple(self.audit_refs)
            )
        for ref in self.audit_refs:
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError(
                    "ReintegrationContext.audit_refs entries must be "
                    "non-empty strings"
                )

        # Validate plans_attempted are PlanRef instances.
        for ref in self.plans_attempted:
            if not isinstance(ref, PlanRef):
                raise ValueError(
                    "ReintegrationContext.plans_attempted entries must "
                    "be PlanRef instances"
                )

        if truncated and not self.truncated:
            object.__setattr__(self, "truncated", True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_decided_action_kind": self.original_decided_action_kind,
            "plans_attempted": [r.to_dict() for r in self.plans_attempted],
            "tool_outcomes_summary": self.tool_outcomes_summary,
            "discovered_information": self.discovered_information,
            "audit_refs": list(self.audit_refs),
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReintegrationContext":
        return cls(
            original_decided_action_kind=str(
                data.get("original_decided_action_kind", "")
            ),
            plans_attempted=tuple(
                PlanRef.from_dict(r)
                for r in (data.get("plans_attempted") or [])
            ),
            tool_outcomes_summary=str(data.get("tool_outcomes_summary", "")),
            discovered_information=str(data.get("discovered_information", "")),
            audit_refs=tuple(data.get("audit_refs", []) or []),
            truncated=bool(data.get("truncated", False)),
        )


# ---------------------------------------------------------------------------
# ExecutionTrace — accumulator for the full machinery loop
# ---------------------------------------------------------------------------


@dataclass
class ExecutionTrace:
    """Mutable accumulator captured across step iterations.

    Captures enough context to construct a ReintegrationContext at
    B1 / B2 termination. Not frozen because it grows during the loop;
    the ReintegrationContext built from it IS frozen and capped.

    The accumulators here are unbounded; capping happens at
    ReintegrationContext construction time. This keeps the loop's
    accumulation logic simple — it appends without thinking about
    caps — and centralises the cap enforcement in one place.
    """

    plans_attempted: list[PlanRef] = field(default_factory=list)
    tool_outcomes: list[str] = field(default_factory=list)
    discovered_information_chunks: list[str] = field(default_factory=list)
    audit_refs: list[str] = field(default_factory=list)

    def record_plan(self, plan: Plan) -> None:
        self.plans_attempted.append(PlanRef.from_plan(plan))

    def record_step_outcome(self, summary: str) -> None:
        if summary.strip():
            self.tool_outcomes.append(summary)

    def record_discovered_information(self, chunk: str) -> None:
        if chunk.strip():
            self.discovered_information_chunks.append(chunk)

    def record_audit_ref(self, ref: str) -> None:
        if ref.strip():
            self.audit_refs.append(ref)

    def to_reintegration_context(
        self, *, original_decided_action_kind: str
    ) -> ReintegrationContext:
        """Build the capped, frozen ReintegrationContext. Caps applied
        in ReintegrationContext.__post_init__."""
        return ReintegrationContext(
            original_decided_action_kind=original_decided_action_kind,
            plans_attempted=tuple(self.plans_attempted),
            tool_outcomes_summary=" | ".join(self.tool_outcomes),
            discovered_information=" ".join(
                self.discovered_information_chunks
            ),
            audit_refs=tuple(self.audit_refs),
        )


__all__ = [
    "DISCOVERED_INFORMATION_CAP",
    "ExecutionTrace",
    "PLANS_ATTEMPTED_CAP",
    "PlanRef",
    "ReintegrationContext",
    "TOOL_OUTCOMES_SUMMARY_CAP",
]
