"""Briefing artifact + CohortOutput contract.

The briefing is the load-bearing contract between the integration
layer and the presence layer. Integration sees the full input
contract (raw cohort outputs, secret covenants, hidden memory,
restricted context-space material). Presence sees the briefing.
The schema here is the architecture's safety surface.

Per the revised INTEGRATION-LAYER spec (design review verdict
"REVISE TO MATCH REALITY", 2026-04-26):

- Section 1 fixes the Briefing schema: ContextItem array,
  FilteredItem array, tagged-union DecidedAction (six variants),
  bounded prose presence_directive, AuditTrace with references.

- Section 4 fixes the CohortOutput contract: cohort_id,
  cohort_run_id, output, visibility tagged union, produced_at.

- Section 3 (the load-bearing safety invariant): the briefing
  itself must be presence-safe. Integration uses restricted inputs
  to *shape* the briefing, but the briefing's text must contain
  no secret-covenant text, no hidden-memory content, no
  restricted context-space material. Restricted CohortOutputs
  carry a Visibility.Restricted tag so the runner can route them
  through the redaction path.

The schema cannot enforce semantic presence-safety automatically —
that's the runner's job — but it documents the contract clearly
so callers can't accidentally embed forbidden content.

The decided_action enum is final at:

    respond_only | execute_tool | propose_tool
                 | constrained_response | pivot | defer
                 | clarification_needed

The `clarification_needed` variant lands with the
PRESENCE-DECOUPLING-INTRODUCE batch (V1 schema extension). Used by
integration's first-pass when critical info is missing, and by
EnactmentService's B2 termination (Tier-5 surface) when mid-action
ambiguity blocks progress without same-turn integration re-entry.
The variant carries a bounded `ClarificationPartialState` so the
serialized briefing contract stays auditable rather than becoming
an unbounded dumping ground.

All dataclasses are frozen — briefings and cohort outputs are
produced once and consumed read-only. Round-trip serialisation
lives on each class via `to_dict()` / `from_dict()` for the
audit-log pipeline.

source_type convention (free-form string per spec): dotted
prefix `<category>.<id>`. Examples:

  - "cohort.memory" / "cohort.weather" / "cohort.gardener"
  - "tool.read" or "tool.read.<tool_id>"
  - "context_space" or "context_space.<space_id>"
  - "conversation"

Schema validates non-empty; conventions are caller responsibility
so new cohorts can adopt the source_type space without spec churn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BriefingValidationError(ValueError):
    """Raised when briefing or cohort schema validation fails."""


# ---------------------------------------------------------------------------
# Visibility tagged union (CohortOutput safety property)
# ---------------------------------------------------------------------------


class VisibilityKind(str, Enum):
    """Discriminator for the Visibility tagged union.

    A CohortOutput tagged Public is safe to summarise into the
    briefing's text fields (after distillation). A CohortOutput
    tagged Restricted may shape integration's decision but its
    output content must NOT appear quoted in the briefing — only
    behavioral instruction in presence_directive can reflect it.
    """

    PUBLIC = "public"
    RESTRICTED = "restricted"


@dataclass(frozen=True)
class Public:
    kind: ClassVar[VisibilityKind] = VisibilityKind.PUBLIC

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Public":
        if not isinstance(data, dict) or data.get("kind") != cls.kind.value:
            raise BriefingValidationError(
                f"Public.from_dict expected kind=public; got {data!r}"
            )
        return cls()


@dataclass(frozen=True)
class Restricted:
    """Restricted visibility carries a `reason` string for audit.

    The reason is itself part of the audit surface — it should
    describe the policy that restricts the cohort output (e.g.
    "covenant", "hidden_memory", "cross_space"), not the secret
    content itself.
    """

    kind: ClassVar[VisibilityKind] = VisibilityKind.RESTRICTED
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise BriefingValidationError(
                "Restricted.reason must be a non-empty string"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Restricted":
        if not isinstance(data, dict) or data.get("kind") != cls.kind.value:
            raise BriefingValidationError(
                f"Restricted.from_dict expected kind=restricted; got {data!r}"
            )
        return cls(reason=str(data.get("reason", "")))


Visibility = Public | Restricted


def visibility_from_dict(data: dict[str, Any]) -> Visibility:
    if not isinstance(data, dict):
        raise BriefingValidationError(
            f"visibility must deserialise from a dict; got "
            f"{type(data).__name__}"
        )
    raw = data.get("kind")
    try:
        kind = VisibilityKind(raw)
    except ValueError as exc:
        valid = ", ".join(k.value for k in VisibilityKind)
        raise BriefingValidationError(
            f"visibility.kind {raw!r} is not one of: {valid}"
        ) from exc
    if kind is VisibilityKind.PUBLIC:
        return Public.from_dict(data)
    return Restricted.from_dict(data)


# ---------------------------------------------------------------------------
# Outcome (CohortOutput runner-owned metadata; COHORT-FAN-OUT-RUNNER edit #4 + #8)
# ---------------------------------------------------------------------------


class Outcome(str, Enum):
    """Runner-owned outcome attribution for a CohortOutput.

    Synthetic outcomes (anything other than `success`) signal that the
    cohort did not produce its own output; the runner constructed the
    output to keep the result-list shape invariant. Integration's
    filter phase reads `outcome != success` to recognise failed
    cohorts.

    Per the design review edit #8: timeout cause is split into `timeout_per_cohort`
    (cohort exceeded its own `timeout_ms`) vs `timeout_global` (cohort
    cancelled because the global wall-clock cap was hit). The two
    cases differ in operator interpretation and downstream policy.
    """

    SUCCESS = "success"
    TIMEOUT_PER_COHORT = "timeout_per_cohort"
    TIMEOUT_GLOBAL = "timeout_global"
    ERROR = "error"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# CohortOutput
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CohortOutput:
    """Structured cohort output the integration runner consumes.

    The CohortOutput contract is part of the V1 spec because
    integration is the consumer; subsequent specs that build cohort
    adapters target this contract.

    `output` carries the cohort-specific payload. Integration's job
    is to interpret it; the schema only enforces that it's a dict
    so audit serialisation is straightforward. Restricted cohorts'
    output must NOT be copied into briefing text — see Visibility.

    Runner-owned metadata (added by COHORT-FAN-OUT-RUNNER, the design review edit
    #4): `outcome` and `error_summary`. These live OUTSIDE the
    cohort-specific `output` payload to avoid namespace collision —
    real cohorts may legitimately use a `status` key inside
    `output`. Synthetic CohortOutputs (timeout / error / cancelled)
    have `output: {}` and `outcome != Outcome.SUCCESS`; integration
    filters on `outcome` not on `output` content.
    """

    cohort_id: str
    cohort_run_id: str
    output: dict[str, Any]
    visibility: Visibility = field(default_factory=Public)
    produced_at: str = ""  # ISO 8601 UTC; runner fills if blank
    outcome: Outcome = Outcome.SUCCESS
    error_summary: str = ""  # populated for outcome != SUCCESS; redacted

    def __post_init__(self) -> None:
        if not isinstance(self.cohort_id, str) or not self.cohort_id.strip():
            raise BriefingValidationError(
                "CohortOutput.cohort_id must be a non-empty string"
            )
        if (
            not isinstance(self.cohort_run_id, str)
            or not self.cohort_run_id.strip()
        ):
            raise BriefingValidationError(
                "CohortOutput.cohort_run_id must be a non-empty string"
            )
        if not isinstance(self.output, dict):
            raise BriefingValidationError(
                f"CohortOutput.output must be a dict; got "
                f"{type(self.output).__name__}"
            )
        if not isinstance(self.visibility, (Public, Restricted)):
            raise BriefingValidationError(
                f"CohortOutput.visibility must be Public or Restricted; got "
                f"{type(self.visibility).__name__}"
            )
        if not isinstance(self.produced_at, str):
            raise BriefingValidationError(
                "CohortOutput.produced_at must be an ISO 8601 string"
            )
        if not isinstance(self.outcome, Outcome):
            raise BriefingValidationError(
                f"CohortOutput.outcome must be an Outcome; got "
                f"{type(self.outcome).__name__}"
            )
        if not isinstance(self.error_summary, str):
            raise BriefingValidationError(
                "CohortOutput.error_summary must be a string"
            )

    @property
    def is_restricted(self) -> bool:
        return isinstance(self.visibility, Restricted)

    @property
    def is_synthetic(self) -> bool:
        """True when the runner built this output (cohort failed or
        timed out). Integration filters on this rather than on
        `output` content."""
        return self.outcome is not Outcome.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "cohort_id": self.cohort_id,
            "cohort_run_id": self.cohort_run_id,
            "output": dict(self.output),
            "visibility": self.visibility.to_dict(),
            "produced_at": self.produced_at,
            "outcome": self.outcome.value,
            "error_summary": self.error_summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CohortOutput":
        if not isinstance(data, dict):
            raise BriefingValidationError(
                f"CohortOutput must deserialise from a dict; got "
                f"{type(data).__name__}"
            )
        try:
            outcome = Outcome(data.get("outcome", Outcome.SUCCESS.value))
        except ValueError as exc:
            valid = ", ".join(o.value for o in Outcome)
            raise BriefingValidationError(
                f"CohortOutput.outcome {data.get('outcome')!r} is not one "
                f"of: {valid}"
            ) from exc
        return cls(
            cohort_id=str(data.get("cohort_id", "")),
            cohort_run_id=str(data.get("cohort_run_id", "")),
            output=dict(data.get("output", {}) or {}),
            visibility=visibility_from_dict(
                data.get("visibility") or Public().to_dict()
            ),
            produced_at=str(data.get("produced_at", "")),
            outcome=outcome,
            error_summary=str(data.get("error_summary", "")),
        )


def now_iso() -> str:
    """Helper: ISO 8601 UTC timestamp for CohortOutput.produced_at."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Source type validation
# ---------------------------------------------------------------------------


def _validate_source_type(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BriefingValidationError(
            f"{field_name} must be a non-empty string (e.g. "
            f"'cohort.memory', 'tool.read', 'context_space')"
        )
    return value.strip()


# ---------------------------------------------------------------------------
# ContextItem / FilteredItem
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextItem:
    """A single relevant piece of context surfaced into the briefing.

    `summary` MUST be presence-safe. If the underlying material is
    a Restricted cohort or hidden memory, the summary carries the
    behavioral implication, not the source content. The runner is
    responsible for that translation; this dataclass is the surface
    presence sees.
    """

    source_type: str
    source_id: str
    summary: str
    confidence: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_type",
            _validate_source_type(self.source_type, "ContextItem.source_type"),
        )
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise BriefingValidationError(
                "ContextItem.source_id must be a non-empty string"
            )
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise BriefingValidationError(
                "ContextItem.summary must be a non-empty string"
            )
        if not isinstance(self.confidence, (int, float)):
            raise BriefingValidationError(
                "ContextItem.confidence must be a number in [0.0, 1.0]"
            )
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise BriefingValidationError(
                f"ContextItem.confidence must be in [0.0, 1.0]; got "
                f"{self.confidence}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "summary": self.summary,
            "confidence": float(self.confidence),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextItem":
        if not isinstance(data, dict):
            raise BriefingValidationError(
                f"ContextItem must deserialise from a dict; got "
                f"{type(data).__name__}"
            )
        return cls(
            source_type=str(data.get("source_type", "")),
            source_id=str(data.get("source_id", "")),
            summary=str(data.get("summary", "")),
            confidence=float(data.get("confidence", 0.0)),
        )


@dataclass(frozen=True)
class FilteredItem:
    """A piece of context integration considered and dismissed.

    Per spec Section 1: source_type, source_id, reason_filtered.
    No summary field — the filtered audit trail records what was
    weighed and why it was dismissed; downstream auditors can
    cross-reference the source by source_id if they need detail.
    """

    source_type: str
    source_id: str
    reason_filtered: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_type",
            _validate_source_type(self.source_type, "FilteredItem.source_type"),
        )
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise BriefingValidationError(
                "FilteredItem.source_id must be a non-empty string"
            )
        if (
            not isinstance(self.reason_filtered, str)
            or not self.reason_filtered.strip()
        ):
            raise BriefingValidationError(
                "FilteredItem.reason_filtered must be a non-empty string"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "reason_filtered": self.reason_filtered,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FilteredItem":
        if not isinstance(data, dict):
            raise BriefingValidationError(
                f"FilteredItem must deserialise from a dict; got "
                f"{type(data).__name__}"
            )
        return cls(
            source_type=str(data.get("source_type", "")),
            source_id=str(data.get("source_id", "")),
            reason_filtered=str(data.get("reason_filtered", "")),
        )


# ---------------------------------------------------------------------------
# DecidedAction tagged union
# ---------------------------------------------------------------------------


class ActionKind(str, Enum):
    """Discriminator for the DecidedAction tagged union.

    Seven variants after the PDI extension:
      - respond_only          presence generates a conversational reply
      - execute_tool          presence executes the named tool now
      - propose_tool          presence surfaces a confirmation to the user
      - constrained_response  presence generates partial satisfaction
                              under a named limit
      - pivot                 presence generates a different shape than
                              the literal request
      - defer                 presence acknowledges and signals delay
      - clarification_needed  integration could not resolve a question
                              without user input (first-pass) OR
                              EnactmentService surfaced a mid-action
                              ambiguity (B2 termination — no same-turn
                              integration re-entry).
    """

    RESPOND_ONLY = "respond_only"
    EXECUTE_TOOL = "execute_tool"
    PROPOSE_TOOL = "propose_tool"
    CONSTRAINED_RESPONSE = "constrained_response"
    PIVOT = "pivot"
    DEFER = "defer"
    CLARIFICATION_NEEDED = "clarification_needed"


@dataclass(frozen=True)
class RespondOnly:
    kind: ClassVar[ActionKind] = ActionKind.RESPOND_ONLY

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RespondOnly":
        _expect_kind(data, cls.kind)
        return cls()


@dataclass(frozen=True)
class ExecuteTool:
    kind: ClassVar[ActionKind] = ActionKind.EXECUTE_TOOL
    tool_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    narration_context: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.tool_id, str) or not self.tool_id.strip():
            raise BriefingValidationError(
                "ExecuteTool.tool_id must be a non-empty string"
            )
        if not isinstance(self.arguments, dict):
            raise BriefingValidationError(
                f"ExecuteTool.arguments must be a dict; got "
                f"{type(self.arguments).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "tool_id": self.tool_id,
            "arguments": dict(self.arguments),
            "narration_context": self.narration_context,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecuteTool":
        _expect_kind(data, cls.kind)
        return cls(
            tool_id=str(data.get("tool_id", "")),
            arguments=dict(data.get("arguments", {}) or {}),
            narration_context=str(data.get("narration_context", "")),
        )


@dataclass(frozen=True)
class ProposeTool:
    kind: ClassVar[ActionKind] = ActionKind.PROPOSE_TOOL
    tool_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    # INTEGRATION-CAPABILITY-FIRST-V1 Batch 2 Fold 2a (the design review
    # verdict 2026-05-03): the propose-tool kind prompt distinguishes
    # read-only (call inline) from destructive (propose-then-confirm).
    # Effect MUST reach the model as deterministic substrate so the
    # safety property doesn't depend on model inference from tool name
    # alone. Backward-compat default is the empty string (treated as
    # "unknown / propose conservatively" by the renderer); existing
    # callers pre-Fold-2a remain valid until they're migrated.
    # Recognized values: "read", "soft_write", "hard_write", "unknown".
    # Same architectural pattern as covenant determinism in CCV1:
    # substrate flows deterministically AND the integration LLM may
    # add framing on top.
    effect: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.tool_id, str) or not self.tool_id.strip():
            raise BriefingValidationError(
                "ProposeTool.tool_id must be a non-empty string"
            )
        if not isinstance(self.arguments, dict):
            raise BriefingValidationError(
                f"ProposeTool.arguments must be a dict; got "
                f"{type(self.arguments).__name__}"
            )
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise BriefingValidationError(
                "ProposeTool.reason must be a non-empty string"
            )
        if not isinstance(self.effect, str):
            raise BriefingValidationError(
                f"ProposeTool.effect must be a string; got "
                f"{type(self.effect).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "tool_id": self.tool_id,
            "arguments": dict(self.arguments),
            "reason": self.reason,
            "effect": self.effect,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProposeTool":
        _expect_kind(data, cls.kind)
        return cls(
            tool_id=str(data.get("tool_id", "")),
            arguments=dict(data.get("arguments", {}) or {}),
            reason=str(data.get("reason", "")),
            effect=str(data.get("effect", "")),
        )


@dataclass(frozen=True)
class ConstrainedResponse:
    """Presence generates partial satisfaction under a named limit.

    Per revised spec Section 1: fields are constraint and
    satisfaction_partial (renamed from earlier draft's
    partial_satisfaction).
    """

    kind: ClassVar[ActionKind] = ActionKind.CONSTRAINED_RESPONSE
    constraint: str = ""
    satisfaction_partial: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.constraint, str) or not self.constraint.strip():
            raise BriefingValidationError(
                "ConstrainedResponse.constraint must be a non-empty string"
            )
        if (
            not isinstance(self.satisfaction_partial, str)
            or not self.satisfaction_partial.strip()
        ):
            raise BriefingValidationError(
                "ConstrainedResponse.satisfaction_partial must be a "
                "non-empty string"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "constraint": self.constraint,
            "satisfaction_partial": self.satisfaction_partial,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConstrainedResponse":
        _expect_kind(data, cls.kind)
        return cls(
            constraint=str(data.get("constraint", "")),
            satisfaction_partial=str(data.get("satisfaction_partial", "")),
        )


@dataclass(frozen=True)
class Pivot:
    """Presence generates a different shape than the literal request.

    Per revised spec Section 1: reason + suggested_shape. reason is
    presence-safe behavioral framing; suggested_shape names the
    redirected response shape (e.g. "general planning conversation"
    rather than "the surprise party topic").
    """

    kind: ClassVar[ActionKind] = ActionKind.PIVOT
    reason: str = ""
    suggested_shape: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise BriefingValidationError(
                "Pivot.reason must be a non-empty string"
            )
        if (
            not isinstance(self.suggested_shape, str)
            or not self.suggested_shape.strip()
        ):
            raise BriefingValidationError(
                "Pivot.suggested_shape must be a non-empty string"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "reason": self.reason,
            "suggested_shape": self.suggested_shape,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Pivot":
        _expect_kind(data, cls.kind)
        return cls(
            reason=str(data.get("reason", "")),
            suggested_shape=str(data.get("suggested_shape", "")),
        )


@dataclass(frozen=True)
class Defer:
    """Presence acknowledges and signals delay or follow-up.

    Per revised spec Section 1: reason + follow_up_signal.
    follow_up_signal carries the specific marker presence should
    surface to the user (e.g. "I'll come back when the build
    finishes" or "queued; will follow up tomorrow").
    """

    kind: ClassVar[ActionKind] = ActionKind.DEFER
    reason: str = ""
    follow_up_signal: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise BriefingValidationError(
                "Defer.reason must be a non-empty string"
            )
        if (
            not isinstance(self.follow_up_signal, str)
            or not self.follow_up_signal.strip()
        ):
            raise BriefingValidationError(
                "Defer.follow_up_signal must be a non-empty string"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "reason": self.reason,
            "follow_up_signal": self.follow_up_signal,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Defer":
        _expect_kind(data, cls.kind)
        return cls(
            reason=str(data.get("reason", "")),
            follow_up_signal=str(data.get("follow_up_signal", "")),
        )


# ---------------------------------------------------------------------------
# ClarificationNeeded (PDI V1 extension)
# ---------------------------------------------------------------------------


# Bounded char caps for ClarificationPartialState fields. The
# serialized briefing contract carries these — without caps the
# state can grow unbounded (especially via B2-routed mid-action
# discovery payloads). Caps mirror the spec's Section 1 caps.
CLARIFICATION_ATTEMPTED_ACTION_SUMMARY_CAP = 1000
CLARIFICATION_DISCOVERED_INFORMATION_CAP = 1000
CLARIFICATION_BLOCKING_AMBIGUITY_CAP = 500
CLARIFICATION_SAFE_QUESTION_CONTEXT_CAP = 500
CLARIFICATION_QUESTION_CAP = 200

# Allowed ambiguity_type values. Closed enum so audit consumers can
# filter; "other" is the catch-all when none of the more specific
# kinds fit.
_ALLOWED_AMBIGUITY_TYPES = ("target", "parameter", "approach", "intent", "other")


@dataclass(frozen=True)
class ClarificationPartialState:
    """Bounded state attached to a B2-routed clarification.

    Per the design review edit: typed dataclass with explicit char caps rather
    than a free-form dict. First-pass clarifications carry
    `partial_state=None` (nothing has been attempted yet); B2
    clarifications carry a populated state with the attempted
    action, the information discovered, the blocking ambiguity,
    and safe context for the question. `audit_refs` lets the next
    turn's integration pull the full traces if it needs depth
    without inflating the in-prompt payload.

    All char caps are enforced at construction; over-cap fields
    raise `BriefingValidationError` so callers cannot accidentally
    let unbounded data slip into the serialized briefing.
    """

    attempted_action_summary: str = ""
    discovered_information: str = ""
    blocking_ambiguity: str = ""
    safe_question_context: str = ""
    audit_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_cap(
            self.attempted_action_summary,
            CLARIFICATION_ATTEMPTED_ACTION_SUMMARY_CAP,
            "ClarificationPartialState.attempted_action_summary",
        )
        _check_cap(
            self.discovered_information,
            CLARIFICATION_DISCOVERED_INFORMATION_CAP,
            "ClarificationPartialState.discovered_information",
        )
        _check_cap(
            self.blocking_ambiguity,
            CLARIFICATION_BLOCKING_AMBIGUITY_CAP,
            "ClarificationPartialState.blocking_ambiguity",
        )
        _check_cap(
            self.safe_question_context,
            CLARIFICATION_SAFE_QUESTION_CONTEXT_CAP,
            "ClarificationPartialState.safe_question_context",
        )
        if not isinstance(self.audit_refs, tuple):
            raise BriefingValidationError(
                "ClarificationPartialState.audit_refs must be a tuple of "
                "non-empty strings"
            )
        for ref in self.audit_refs:
            if not isinstance(ref, str) or not ref.strip():
                raise BriefingValidationError(
                    "ClarificationPartialState.audit_refs entries must be "
                    "non-empty strings"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted_action_summary": self.attempted_action_summary,
            "discovered_information": self.discovered_information,
            "blocking_ambiguity": self.blocking_ambiguity,
            "safe_question_context": self.safe_question_context,
            "audit_refs": list(self.audit_refs),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClarificationPartialState":
        if not isinstance(data, dict):
            raise BriefingValidationError(
                f"ClarificationPartialState must deserialise from a dict; "
                f"got {type(data).__name__}"
            )
        return cls(
            attempted_action_summary=str(
                data.get("attempted_action_summary", "")
            ),
            discovered_information=str(data.get("discovered_information", "")),
            blocking_ambiguity=str(data.get("blocking_ambiguity", "")),
            safe_question_context=str(data.get("safe_question_context", "")),
            audit_refs=tuple(data.get("audit_refs", []) or []),
        )


@dataclass(frozen=True)
class ClarificationNeeded:
    """Integration could not resolve a question without user input.

    Two production paths produce this variant:

      1. Integration first-pass: critical info is missing and
         signals (cohorts, memory, covenants, conversation thread)
         cannot resolve. `partial_state` is None — nothing has
         been attempted yet.

      2. EnactmentService B2 termination: mid-action ambiguity
         blocks progress. Enactment terminates and constructs
         this variant; thin path renders `question` directly to
         the user. NO same-turn integration re-entry; the
         populated `partial_state` is stored for the NEXT turn's
         integration alongside the user's reply.

    `question` is bounded at 200 chars (one-sentence, concise,
    user-facing). `ambiguity_type` is closed enum.
    """

    kind: ClassVar[ActionKind] = ActionKind.CLARIFICATION_NEEDED
    question: str = ""
    ambiguity_type: str = ""
    partial_state: ClarificationPartialState | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.question, str) or not self.question.strip():
            raise BriefingValidationError(
                "ClarificationNeeded.question must be a non-empty string"
            )
        _check_cap(
            self.question,
            CLARIFICATION_QUESTION_CAP,
            "ClarificationNeeded.question",
        )
        if (
            not isinstance(self.ambiguity_type, str)
            or self.ambiguity_type not in _ALLOWED_AMBIGUITY_TYPES
        ):
            valid = ", ".join(_ALLOWED_AMBIGUITY_TYPES)
            raise BriefingValidationError(
                f"ClarificationNeeded.ambiguity_type {self.ambiguity_type!r} "
                f"is not one of: {valid}"
            )
        if self.partial_state is not None and not isinstance(
            self.partial_state, ClarificationPartialState
        ):
            raise BriefingValidationError(
                "ClarificationNeeded.partial_state must be None or a "
                "ClarificationPartialState"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "question": self.question,
            "ambiguity_type": self.ambiguity_type,
            "partial_state": (
                self.partial_state.to_dict()
                if self.partial_state is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClarificationNeeded":
        _expect_kind(data, cls.kind)
        partial_raw = data.get("partial_state")
        partial = (
            ClarificationPartialState.from_dict(partial_raw)
            if isinstance(partial_raw, dict)
            else None
        )
        return cls(
            question=str(data.get("question", "")),
            ambiguity_type=str(data.get("ambiguity_type", "")),
            partial_state=partial,
        )


def _check_cap(value: str, cap: int, field_label: str) -> None:
    if not isinstance(value, str):
        raise BriefingValidationError(f"{field_label} must be a string")
    if len(value) > cap:
        raise BriefingValidationError(
            f"{field_label} exceeds {cap}-char cap (got {len(value)})"
        )


DecidedAction = (
    RespondOnly
    | ExecuteTool
    | ProposeTool
    | ConstrainedResponse
    | Pivot
    | Defer
    | ClarificationNeeded
)


_ACTION_VARIANTS: dict[ActionKind, type] = {
    ActionKind.RESPOND_ONLY: RespondOnly,
    ActionKind.EXECUTE_TOOL: ExecuteTool,
    ActionKind.PROPOSE_TOOL: ProposeTool,
    ActionKind.CONSTRAINED_RESPONSE: ConstrainedResponse,
    ActionKind.PIVOT: Pivot,
    ActionKind.DEFER: Defer,
    ActionKind.CLARIFICATION_NEEDED: ClarificationNeeded,
}


def _expect_kind(data: dict[str, Any], expected: ActionKind) -> None:
    if not isinstance(data, dict):
        raise BriefingValidationError(
            f"action variant must deserialise from a dict; got "
            f"{type(data).__name__}"
        )
    actual = data.get("kind")
    if actual != expected.value:
        raise BriefingValidationError(
            f"action variant kind mismatch: expected {expected.value!r}, "
            f"got {actual!r}"
        )


def decided_action_from_dict(data: dict[str, Any]) -> DecidedAction:
    """Dispatch on `kind` and parse the matching DecidedAction variant."""
    if not isinstance(data, dict):
        raise BriefingValidationError(
            f"decided_action must deserialise from a dict; got "
            f"{type(data).__name__}"
        )
    raw_kind = data.get("kind")
    try:
        kind = ActionKind(raw_kind)
    except ValueError as exc:
        valid = ", ".join(k.value for k in ActionKind)
        raise BriefingValidationError(
            f"decided_action.kind {raw_kind!r} is not one of: {valid}"
        ) from exc
    return _ACTION_VARIANTS[kind].from_dict(data)


# ---------------------------------------------------------------------------
# BudgetState
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetState:
    """Which limits the integration run hit, if any.

    V1 (INTEGRATION-LAYER-V1) Section 4c flags: iterations,
    timeout, cohort_entries, filtered_entries, tokens.

    COHORT-FAN-OUT-RUNNER Section 8 adds three flags integration's
    filter phase reads to apply downstream policy:

      - required_cohort_failed: at least one required cohort's
        outcome is not SUCCESS. Integration produces a constrained
        briefing that notes degraded context.
      - required_safety_cohort_failed: a required cohort with
        safety_class True failed. Integration's decided action
        defaults to constrained_response or defer rather than
        respond_only. Principle: if safety can't be verified, we
        don't proceed at full strength.
      - cohort_fan_out_global_timeout: the fan-out runner hit its
        wall-clock cap. Distinct from the other failure flags so
        downstream telemetry can attribute global vs per-cohort
        causes cleanly.

    `tokens_hit_limit` is provisional — the integration runner
    doesn't track cumulative token usage across iterations in v1
    (max_tokens is per-call). The flag is in the schema so the
    field exists when cumulative tracking lands without breaking
    serialised audit records.
    """

    iterations_hit_limit: bool = False
    timeout_hit_limit: bool = False
    cohort_entries_hit_limit: bool = False
    filtered_entries_hit_limit: bool = False
    tokens_hit_limit: bool = False
    required_cohort_failed: bool = False
    required_safety_cohort_failed: bool = False
    cohort_fan_out_global_timeout: bool = False

    def __post_init__(self) -> None:
        for name in (
            "iterations_hit_limit",
            "timeout_hit_limit",
            "cohort_entries_hit_limit",
            "filtered_entries_hit_limit",
            "tokens_hit_limit",
            "required_cohort_failed",
            "required_safety_cohort_failed",
            "cohort_fan_out_global_timeout",
        ):
            if not isinstance(getattr(self, name), bool):
                raise BriefingValidationError(
                    f"BudgetState.{name} must be a bool"
                )

    @property
    def any_hit(self) -> bool:
        return (
            self.iterations_hit_limit
            or self.timeout_hit_limit
            or self.cohort_entries_hit_limit
            or self.filtered_entries_hit_limit
            or self.tokens_hit_limit
            or self.required_cohort_failed
            or self.required_safety_cohort_failed
            or self.cohort_fan_out_global_timeout
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "iterations_hit_limit": self.iterations_hit_limit,
            "timeout_hit_limit": self.timeout_hit_limit,
            "cohort_entries_hit_limit": self.cohort_entries_hit_limit,
            "filtered_entries_hit_limit": self.filtered_entries_hit_limit,
            "tokens_hit_limit": self.tokens_hit_limit,
            "required_cohort_failed": self.required_cohort_failed,
            "required_safety_cohort_failed": (
                self.required_safety_cohort_failed
            ),
            "cohort_fan_out_global_timeout": (
                self.cohort_fan_out_global_timeout
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BudgetState":
        if not isinstance(data, dict):
            raise BriefingValidationError(
                f"BudgetState must deserialise from a dict; got "
                f"{type(data).__name__}"
            )
        return cls(
            iterations_hit_limit=bool(data.get("iterations_hit_limit", False)),
            timeout_hit_limit=bool(data.get("timeout_hit_limit", False)),
            cohort_entries_hit_limit=bool(
                data.get("cohort_entries_hit_limit", False)
            ),
            filtered_entries_hit_limit=bool(
                data.get("filtered_entries_hit_limit", False)
            ),
            tokens_hit_limit=bool(data.get("tokens_hit_limit", False)),
            required_cohort_failed=bool(
                data.get("required_cohort_failed", False)
            ),
            required_safety_cohort_failed=bool(
                data.get("required_safety_cohort_failed", False)
            ),
            cohort_fan_out_global_timeout=bool(
                data.get("cohort_fan_out_global_timeout", False)
            ),
        )


# ---------------------------------------------------------------------------
# AuditTrace
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditTrace:
    """References + telemetry for the integration run.

    Per revised spec Section 1: cohort_outputs (references to the
    cohort outputs the run consumed; not raw dumps),
    tools_called_during_prep (references to read-only tool
    invocations during integration), iterations_used,
    budget_state, fail_soft_engaged.

    `phase_durations_ms` and `notes` are telemetry not in the
    minimal schema but required for acceptance criterion #12
    ("internal phases observable in instrumentation"). They
    carry per-phase millisecond timings keyed by phase name (the
    five named phases plus per-iteration markers) and a free-form
    notes string the runner uses to record fail-soft cause.
    """

    cohort_outputs: tuple[str, ...] = ()
    tools_called_during_prep: tuple[str, ...] = ()
    # INTEGRATION-RENDERER-RESULT-FORWARD-V1 (2026-05-07): per-call
    # tool results captured during integration synthesis, forwarded to
    # the renderer so it doesn't re-dispatch the same read tool to
    # fetch content the integration model already fetched. Each entry:
    # ``{"tool_name": str, "result": str}`` (serialized result). Empty
    # tuple when no successful tool dispatches happened.
    tool_results_during_prep: tuple[dict[str, str], ...] = ()
    iterations_used: int = 0
    budget_state: BudgetState = field(default_factory=BudgetState)
    fail_soft_engaged: bool = False
    phase_durations_ms: dict[str, int] = field(default_factory=dict)
    notes: str = ""

    def __post_init__(self) -> None:
        for ref in self.cohort_outputs:
            if not isinstance(ref, str) or not ref.strip():
                raise BriefingValidationError(
                    "AuditTrace.cohort_outputs entries must be non-empty "
                    "strings"
                )
        for ref in self.tools_called_during_prep:
            if not isinstance(ref, str) or not ref.strip():
                raise BriefingValidationError(
                    "AuditTrace.tools_called_during_prep entries must be "
                    "non-empty strings"
                )
        for entry in self.tool_results_during_prep:
            if not isinstance(entry, dict):
                raise BriefingValidationError(
                    "AuditTrace.tool_results_during_prep entries must be "
                    "dicts with tool_name + result keys"
                )
            if not isinstance(entry.get("tool_name"), str):
                raise BriefingValidationError(
                    "AuditTrace.tool_results_during_prep entries must "
                    "carry a string tool_name"
                )
            if not isinstance(entry.get("result"), str):
                raise BriefingValidationError(
                    "AuditTrace.tool_results_during_prep entries must "
                    "carry a string result"
                )
        if (
            not isinstance(self.iterations_used, int)
            or self.iterations_used < 0
        ):
            raise BriefingValidationError(
                "AuditTrace.iterations_used must be a non-negative int"
            )
        if not isinstance(self.budget_state, BudgetState):
            raise BriefingValidationError(
                f"AuditTrace.budget_state must be a BudgetState; got "
                f"{type(self.budget_state).__name__}"
            )
        if not isinstance(self.fail_soft_engaged, bool):
            raise BriefingValidationError(
                "AuditTrace.fail_soft_engaged must be a bool"
            )
        if not isinstance(self.phase_durations_ms, dict):
            raise BriefingValidationError(
                "AuditTrace.phase_durations_ms must be a dict[str, int]"
            )
        for phase, dur in self.phase_durations_ms.items():
            if not isinstance(phase, str) or not phase.strip():
                raise BriefingValidationError(
                    "AuditTrace.phase_durations_ms keys must be non-empty "
                    "strings"
                )
            if not isinstance(dur, int) or dur < 0:
                raise BriefingValidationError(
                    f"AuditTrace.phase_durations_ms[{phase!r}] must be a "
                    f"non-negative int (milliseconds)"
                )
        if not isinstance(self.notes, str):
            raise BriefingValidationError("AuditTrace.notes must be a string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cohort_outputs": list(self.cohort_outputs),
            "tools_called_during_prep": list(self.tools_called_during_prep),
            "tool_results_during_prep": [
                dict(e) for e in self.tool_results_during_prep
            ],
            "iterations_used": self.iterations_used,
            "budget_state": self.budget_state.to_dict(),
            "fail_soft_engaged": self.fail_soft_engaged,
            "phase_durations_ms": dict(self.phase_durations_ms),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditTrace":
        if not isinstance(data, dict):
            raise BriefingValidationError(
                f"AuditTrace must deserialise from a dict; got "
                f"{type(data).__name__}"
            )
        return cls(
            cohort_outputs=tuple(data.get("cohort_outputs", []) or []),
            tools_called_during_prep=tuple(
                data.get("tools_called_during_prep", []) or []
            ),
            tool_results_during_prep=tuple(
                dict(e) for e in (
                    data.get("tool_results_during_prep", []) or []
                )
            ),
            iterations_used=int(data.get("iterations_used", 0)),
            budget_state=BudgetState.from_dict(
                data.get("budget_state") or {}
            ),
            fail_soft_engaged=bool(data.get("fail_soft_engaged", False)),
            phase_durations_ms=dict(data.get("phase_durations_ms", {}) or {}),
            notes=str(data.get("notes", "")),
        )


# ---------------------------------------------------------------------------
# ActionEnvelope (PDI V1 extension — the design review edit)
# ---------------------------------------------------------------------------


# Action-shape decided actions that require an envelope. Conversational
# kinds (respond_only, defer, constrained_response, pivot,
# clarification_needed) do not — they don't dispatch tools so there's
# nothing for the envelope to constrain. propose_tool also lives on
# the thin path (render-only proposal); the actual execute_tool that
# fires on the next turn carries its own envelope.
_ACTION_SHAPE_KINDS: frozenset[ActionKind] = frozenset(
    {ActionKind.EXECUTE_TOOL}
)


@dataclass(frozen=True)
class ActionEnvelope:
    """Explicit constraints on an action-shape decided_action.

    Per the design review edit: ActionEnvelope is structural, not computed from
    prose. EnactmentService validates every plan-changing tier
    (initial plan + Tier-2 modify + Tier-3 pivot + Tier-4
    reassemble) against this envelope. Violation → terminate B1.

    This is what makes "EnactmentService never changes
    decided_action" structurally testable rather than aspirational.

      - intended_outcome: explicit, not extracted from prose. The
        user-facing intent the action serves.
      - allowed_tool_classes: capability classes (e.g.
        ["email", "calendar"]) the action may use.
      - allowed_operations: explicit operation names (e.g.
        ["draft", "send"]). Confirmation-boundary case: a
        draft-only envelope omits "send" so an attempted draft→send
        chain fails validation.
      - constraints: must / must_not derived from briefing reasoning.
      - confirmation_requirements: operation names whose dispatch
        requires explicit user confirmation across a turn boundary
        (e.g. ["send"] when the envelope permits draft+send but
        send must wait for the user).
      - forbidden_moves: free-form patterns the envelope rejects
        (e.g. ["channel_switch", "operation_escalation"]).

    All fields are tuples (frozen-friendly) of stripped strings.
    Empty tuples are valid for `constraints`,
    `confirmation_requirements`, and `forbidden_moves`. The
    intended_outcome field MUST be non-empty.
    """

    intended_outcome: str = ""
    allowed_tool_classes: tuple[str, ...] = ()
    allowed_operations: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    confirmation_requirements: tuple[str, ...] = ()
    forbidden_moves: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.intended_outcome, str)
            or not self.intended_outcome.strip()
        ):
            raise BriefingValidationError(
                "ActionEnvelope.intended_outcome must be a non-empty string"
            )
        for label, value in (
            ("allowed_tool_classes", self.allowed_tool_classes),
            ("allowed_operations", self.allowed_operations),
            ("constraints", self.constraints),
            ("confirmation_requirements", self.confirmation_requirements),
            ("forbidden_moves", self.forbidden_moves),
        ):
            if not isinstance(value, tuple):
                raise BriefingValidationError(
                    f"ActionEnvelope.{label} must be a tuple of non-empty "
                    f"strings"
                )
            for entry in value:
                if not isinstance(entry, str) or not entry.strip():
                    raise BriefingValidationError(
                        f"ActionEnvelope.{label} entries must be non-empty "
                        f"strings"
                    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "intended_outcome": self.intended_outcome,
            "allowed_tool_classes": list(self.allowed_tool_classes),
            "allowed_operations": list(self.allowed_operations),
            "constraints": list(self.constraints),
            "confirmation_requirements": list(self.confirmation_requirements),
            "forbidden_moves": list(self.forbidden_moves),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionEnvelope":
        if not isinstance(data, dict):
            raise BriefingValidationError(
                f"ActionEnvelope must deserialise from a dict; got "
                f"{type(data).__name__}"
            )
        return cls(
            intended_outcome=str(data.get("intended_outcome", "")),
            allowed_tool_classes=tuple(
                data.get("allowed_tool_classes", []) or []
            ),
            allowed_operations=tuple(
                data.get("allowed_operations", []) or []
            ),
            constraints=tuple(data.get("constraints", []) or []),
            confirmation_requirements=tuple(
                data.get("confirmation_requirements", []) or []
            ),
            forbidden_moves=tuple(data.get("forbidden_moves", []) or []),
        )


def action_kind_requires_envelope(kind: ActionKind) -> bool:
    """True when a decided_action of this kind must carry a Briefing
    action_envelope. PDI the design review edit: only execute_tool dispatches; only
    execute_tool requires the envelope. Conversational + render-only
    kinds (including propose_tool, which is render-only) do not."""
    return kind in _ACTION_SHAPE_KINDS


# ---------------------------------------------------------------------------
# Briefing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Briefing:
    """The artifact integration hands to presence.

    Carries the architecture's safety property: every text field is
    expected to be presence-safe. Integration's runner is responsible
    for redacting Restricted CohortOutput content before populating
    these fields.
    """

    relevant_context: tuple[ContextItem, ...]
    filtered_context: tuple[FilteredItem, ...]
    decided_action: DecidedAction
    presence_directive: str
    audit_trace: AuditTrace
    turn_id: str = ""
    integration_run_id: str = ""
    action_envelope: ActionEnvelope | None = None
    # COGNITIVE-CONTEXT-V1 C3a: typed cognitive substrate carried
    # alongside presence_directive. PresenceRenderer (C3c) consumes
    # both — substrate flows through the packet, behavioral framing
    # flows through the directive. Optional + default-None for
    # backward compat with existing Briefing construction sites
    # (fail-soft path, tests, integration runner pre-C3a).
    cognitive_context: Any = None
    # INTEGRATION-CAPABILITY-FIRST-V1 Batch 2 Fold 6: turn-context
    # identifiers carried on the briefing so step dispatcher's
    # ToolExecutionInputs construction has real ids to populate
    # (rather than getattr-with-empty-default which lost identifiers
    # for full-machinery dispatch). The briefing is the canonical
    # artifact crossing the model-call seam (CCV1 invariant);
    # identifiers belong on it alongside the cognitive context.
    # Defaults to empty string for backward compat with Briefing
    # construction sites that don't yet thread the identifiers.
    instance_id: str = ""
    member_id: str = ""
    space_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.relevant_context, tuple):
            raise BriefingValidationError(
                "Briefing.relevant_context must be a tuple of ContextItem"
            )
        for item in self.relevant_context:
            if not isinstance(item, ContextItem):
                raise BriefingValidationError(
                    f"Briefing.relevant_context entries must be ContextItem; "
                    f"got {type(item).__name__}"
                )
        if not isinstance(self.filtered_context, tuple):
            raise BriefingValidationError(
                "Briefing.filtered_context must be a tuple of FilteredItem"
            )
        for item in self.filtered_context:
            if not isinstance(item, FilteredItem):
                raise BriefingValidationError(
                    f"Briefing.filtered_context entries must be FilteredItem; "
                    f"got {type(item).__name__}"
                )
        if not isinstance(
            self.decided_action,
            (
                RespondOnly,
                ExecuteTool,
                ProposeTool,
                ConstrainedResponse,
                Pivot,
                Defer,
                ClarificationNeeded,
            ),
        ):
            raise BriefingValidationError(
                f"Briefing.decided_action must be a DecidedAction variant; "
                f"got {type(self.decided_action).__name__}"
            )
        if not isinstance(self.presence_directive, str):
            raise BriefingValidationError(
                "Briefing.presence_directive must be a string"
            )
        if not self.presence_directive.strip():
            raise BriefingValidationError(
                "Briefing.presence_directive must be a non-empty string "
                "(presence needs framing every turn)"
            )
        if not isinstance(self.audit_trace, AuditTrace):
            raise BriefingValidationError(
                f"Briefing.audit_trace must be an AuditTrace; got "
                f"{type(self.audit_trace).__name__}"
            )
        if not isinstance(self.turn_id, str):
            raise BriefingValidationError("Briefing.turn_id must be a string")
        if not isinstance(self.integration_run_id, str):
            raise BriefingValidationError(
                "Briefing.integration_run_id must be a string"
            )
        if self.action_envelope is not None and not isinstance(
            self.action_envelope, ActionEnvelope
        ):
            raise BriefingValidationError(
                "Briefing.action_envelope must be None or an ActionEnvelope"
            )
        # Action-shape decided_actions require an envelope (the design review edit).
        # Render-only kinds must NOT carry an envelope — keeps the
        # contract crisp: an envelope only exists when there's a real
        # dispatch to constrain.
        kind = getattr(self.decided_action, "kind", None)
        if action_kind_requires_envelope(kind):
            if self.action_envelope is None:
                raise BriefingValidationError(
                    f"Briefing.action_envelope is required when "
                    f"decided_action.kind is {kind.value!r}"
                )
        elif self.action_envelope is not None:
            raise BriefingValidationError(
                f"Briefing.action_envelope must be None when "
                f"decided_action.kind is {kind.value!r} (no dispatch to "
                f"constrain)"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "relevant_context": [item.to_dict() for item in self.relevant_context],
            "filtered_context": [item.to_dict() for item in self.filtered_context],
            "decided_action": self.decided_action.to_dict(),
            "presence_directive": self.presence_directive,
            "audit_trace": self.audit_trace.to_dict(),
            "turn_id": self.turn_id,
            "integration_run_id": self.integration_run_id,
            "action_envelope": (
                self.action_envelope.to_dict()
                if self.action_envelope is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Briefing":
        if not isinstance(data, dict):
            raise BriefingValidationError(
                f"Briefing must deserialise from a dict; got "
                f"{type(data).__name__}"
            )
        envelope_raw = data.get("action_envelope")
        envelope = (
            ActionEnvelope.from_dict(envelope_raw)
            if isinstance(envelope_raw, dict)
            else None
        )
        return cls(
            relevant_context=tuple(
                ContextItem.from_dict(d)
                for d in (data.get("relevant_context") or [])
            ),
            filtered_context=tuple(
                FilteredItem.from_dict(d)
                for d in (data.get("filtered_context") or [])
            ),
            decided_action=decided_action_from_dict(
                data.get("decided_action") or {}
            ),
            presence_directive=str(data.get("presence_directive", "")),
            audit_trace=AuditTrace.from_dict(data.get("audit_trace") or {}),
            turn_id=str(data.get("turn_id", "")),
            integration_run_id=str(data.get("integration_run_id", "")),
            action_envelope=envelope,
        )


# ---------------------------------------------------------------------------
# Fail-soft fallback
# ---------------------------------------------------------------------------


_FAIL_SOFT_DIRECTIVE = (
    "integration prep was incomplete; respond conservatively and "
    "acknowledge limited context if relevant."
)


def minimal_fail_soft_briefing(
    *,
    turn_id: str = "",
    integration_run_id: str = "",
    notes: str = "",
    budget_state: BudgetState | None = None,
) -> Briefing:
    """DEPRECATED. Original Section 4c minimal fail-soft briefing.

    Replaced by :func:`system_error_briefing` in PHASE-1-WIPE-
    VERIFICATION (2026-05-07). The original approach softened all
    integration failures (timeout, max_iterations, validation errors,
    unexpected exceptions) into a generic "respond conservatively"
    apology that produced degenerate agent responses on concrete
    user questions. Founder direction: do not soften failures —
    surface them loudly so internal errors are identifiable and
    attributable.

    Kept around for now because some callers and tests still reference
    it; the integration runner no longer calls it. Removal scheduled
    once test fixtures are migrated to ``system_error_briefing``.
    """
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=RespondOnly(),
        presence_directive=_FAIL_SOFT_DIRECTIVE,
        audit_trace=AuditTrace(
            iterations_used=0,
            budget_state=budget_state or BudgetState(),
            fail_soft_engaged=True,
            notes=notes,
        ),
        turn_id=turn_id,
        integration_run_id=integration_run_id,
    )


# Component labels safe to surface to the user. Anything outside this
# set is rewritten to "unexpected_error" before the directive is built —
# raw exception text (which may contain provider payloads, file paths,
# secrets, or adversarial input) must never cross into the presence
# directive. Full diagnostic detail stays in audit_trace.notes for the
# operator.
_SAFE_COMPONENT_LABELS = frozenset({
    "integration_timeout",
    "max_iterations",
    "no_tool_use",
    "read_only_violation",
    "briefing_validation",
    "unexpected_error",
    "decoded_action_invalid",
    "no_finalize_block",
})


def _safe_component(component: str) -> str:
    """Constrain the user-facing component label to a known set."""
    return component if component in _SAFE_COMPONENT_LABELS else "unexpected_error"


def _format_tools_called_receipt(
    tools_called: list[str] | tuple[str, ...],
) -> str:
    """Compact receipt block listing the tools that successfully ran
    before the attempt failed. Intended to be included verbatim in the
    user-facing reply so the next turn's agent has receipts to reason
    from rather than reconstructing intent from natural language.

    Format: bulleted list of unique tool names + iteration positions,
    stable order. Empty input → ``""`` (caller should branch on
    truthiness).

    Each entry from the runner's ``tools_called`` list looks like
    ``"<tool>:iter<N>"`` — we keep the iteration index because order
    matters when the agent reasons about what happened in what
    sequence.
    """
    if not tools_called:
        return ""
    lines = [f"  - {entry}" for entry in tools_called]
    return "\n".join(lines)


_RECEIPT_DISCIPLINE_INSTRUCTION = (
    "RECEIPT DISCIPLINE. The block above lists the tools that "
    "ACTUALLY ran in this attempt. When the user re-asks or follows "
    "up next turn, treat that block as the canonical receipt of what "
    "happened. Do NOT claim a write/update/save succeeded unless its "
    "tool call is present in the receipt. If a prior turn's "
    "conversational language implied an action that isn't in the "
    "receipt, name the gap explicitly rather than inventing "
    "continuity."
)


_SYSTEM_ERROR_DIRECTIVE_TEMPLATE = (
    "INTEGRATION SYNTHESIS FAILED. This is an internal system error, "
    "not a content limitation. Synthesis was retried {attempts} times "
    "and the final attempt failed at component={component}. Surface "
    "this transparently to the user. Do NOT attempt to answer their "
    "original question; do NOT apologize for 'limited context'. Tell "
    "the user plainly that an internal synthesis error occurred, name "
    "the component if useful ('integration_timeout', 'max_iterations', "
    "'no_tool_use', 'read_only_violation', 'briefing_validation', "
    "'unexpected_error'), and offer that they can retry or that the "
    "operator can investigate the integration runner. Keep it short "
    "and direct.\n\n"
    "{receipt_block}"
    "{receipt_discipline}"
)


_ITERATION_CAP_DIRECTIVE_TEMPLATE = (
    "ITERATION CHECKPOINT REACHED. The integration runner consumed "
    "all {cap} reasoning iterations on this turn (across {attempts} "
    "retry attempts). The cap is a natural checkpoint cadence rather "
    "than a hard error — surface the situation to the user and let "
    "them choose the next step. Keep it short and direct.\n\n"
    "Tell them plainly: you've reached the {cap}-iteration "
    "checkpoint and want to check in before continuing. If they ask "
    "what you've been doing, summarize from the receipt block below "
    "and your conversation log. Otherwise, list these three options "
    "as exact phrases they can reply with:\n"
    "  - **continue** — keep going with a fresh {cap}-iteration "
    "budget on the same task.\n"
    "  - **always continue** — make {raised_cap} the new default "
    "permanently. On their next turn, if they reply with this exact "
    "phrase, append the line `KERNOS_INTEGRATION_MAX_ITERATIONS="
    "{raised_cap}` to `.env` using write_file (or update the "
    "existing line if one is present), then re-attempt the original "
    "task.\n"
    "  - **terminate** — stop here and acknowledge.\n\n"
    "Do NOT pretend to have finished their original task. Do NOT "
    "apologize for limited context. The user's reply on the next "
    "turn drives what happens next; honor it precisely.\n\n"
    "{receipt_block}"
    "{receipt_discipline}"
)


def iteration_cap_briefing(
    *,
    turn_id: str = "",
    integration_run_id: str = "",
    attempts: int,
    cap: int,
    cohort_refs: tuple[str, ...] = (),
    tools_called: list[str] | tuple[str, ...] = (),
    iterations: int = 0,
    phase_durations_ms: dict[str, int] | None = None,
    budget_state: BudgetState | None = None,
    cognitive_context: Any = None,
    instance_id: str = "",
    member_id: str = "",
    space_id: str = "",
) -> Briefing:
    """Construct the briefing the runner returns when retry attempts
    all fail at ``component='max_iterations'``.

    Distinct from :func:`system_error_briefing` because the failure
    mode is recoverable by user choice rather than purely operator-
    investigable. The directive instructs presence to surface a
    three-option choice (continue / always continue / terminate) with
    the exact phrases the user can reply with on the next turn. The
    "always continue" path appends a higher cap to ``.env`` so the
    user doesn't have to make this choice repeatedly for the same
    workload.
    """
    raised_cap = max(cap * 2, cap + 100)
    receipt_lines = _format_tools_called_receipt(tools_called)
    if receipt_lines:
        receipt_block = (
            f"### Tool receipts (this attempt's actual dispatch trace)\n"
            f"{receipt_lines}\n\n"
        )
        receipt_discipline = _RECEIPT_DISCIPLINE_INSTRUCTION
    else:
        receipt_block = ""
        receipt_discipline = ""
    directive = _ITERATION_CAP_DIRECTIVE_TEMPLATE.format(
        cap=cap, attempts=attempts, raised_cap=raised_cap,
        receipt_block=receipt_block,
        receipt_discipline=receipt_discipline,
    )
    notes = (
        f"iteration-cap-prompt after {attempts} attempts; "
        f"cap={cap}; raised_cap={raised_cap}"
    )
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=RespondOnly(),
        presence_directive=directive,
        audit_trace=AuditTrace(
            cohort_outputs=cohort_refs,
            tools_called_during_prep=tuple(tools_called),
            iterations_used=iterations,
            budget_state=budget_state or BudgetState(),
            fail_soft_engaged=True,
            phase_durations_ms=dict(phase_durations_ms or {}),
            notes=notes,
        ),
        turn_id=turn_id,
        integration_run_id=integration_run_id,
        cognitive_context=cognitive_context,
        instance_id=instance_id,
        member_id=member_id,
        space_id=space_id,
    )


def system_error_briefing(
    *,
    turn_id: str = "",
    integration_run_id: str = "",
    attempts: int,
    component: str,
    reason: str,
    cohort_refs: tuple[str, ...] = (),
    tools_called: list[str] | tuple[str, ...] = (),
    iterations: int = 0,
    phase_durations_ms: dict[str, int] | None = None,
    budget_state: BudgetState | None = None,
    cognitive_context: Any = None,
    instance_id: str = "",
    member_id: str = "",
    space_id: str = "",
) -> Briefing:
    """Construct the briefing the runner returns when ALL retry
    attempts have been exhausted.

    The directive instructs presence to surface the failure to the
    user transparently. The audit_trace carries the full per-attempt
    diagnostic state so operators can investigate. ``fail_soft_engaged``
    stays True for backward compat with existing audit consumers, but
    the directive content is now load-bearing for diagnostic
    transparency rather than apologetic minimalism.
    """
    safe_component = _safe_component(component)
    receipt_lines = _format_tools_called_receipt(tools_called)
    if receipt_lines:
        receipt_block = (
            f"### Tool receipts (this attempt's actual dispatch trace)\n"
            f"{receipt_lines}\n\n"
        )
        receipt_discipline = _RECEIPT_DISCIPLINE_INSTRUCTION
    else:
        receipt_block = ""
        receipt_discipline = ""
    directive = _SYSTEM_ERROR_DIRECTIVE_TEMPLATE.format(
        attempts=attempts, component=safe_component,
        receipt_block=receipt_block,
        receipt_discipline=receipt_discipline,
    )
    # Raw component + reason (may contain exception text, paths, payloads)
    # stay in audit-trace notes for operator inspection — never the
    # user-facing directive.
    notes = (
        f"system-error after {attempts} attempts; "
        f"final-component={component}; reason={reason}"
    )
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=RespondOnly(),
        presence_directive=directive,
        audit_trace=AuditTrace(
            cohort_outputs=cohort_refs,
            tools_called_during_prep=tuple(tools_called),
            iterations_used=iterations,
            budget_state=budget_state or BudgetState(),
            fail_soft_engaged=True,
            phase_durations_ms=dict(phase_durations_ms or {}),
            notes=notes,
        ),
        turn_id=turn_id,
        integration_run_id=integration_run_id,
        cognitive_context=cognitive_context,
        instance_id=instance_id,
        member_id=member_id,
        space_id=space_id,
    )
