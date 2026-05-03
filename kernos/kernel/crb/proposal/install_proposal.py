"""InstallProposal types + state machine pins.

The CRB approval flow's state machine has five states (the design review pin v1->v2
must-fix #1 split 'approved' into pending + registered for crash
safety):

* ``proposed`` — initial; CRB authored proposal, surfaced to user.
* ``approved_pending_registration`` — user said yes; routine.approved
  (or routine.modification.approved) emitted durably; STS not yet
  confirmed.
* ``approved_registered`` — terminal success; STS register_workflow
  completed.
* ``modify_requested`` — user wants changes; crb.feedback.modify_request
  emitted; Drafter shapes; eventually new proposal.
* ``declined`` — terminal decline; not_now or abandon.

Permitted transitions (everything else raises
:class:`kernos.kernel.crb.proposal.install_proposal_store.InvalidStateTransition`):

* proposed -> approved_pending_registration
* proposed -> modify_requested
* proposed -> declined
* approved_pending_registration -> approved_registered

Notably: approved_pending_registration does NOT transition to
declined or back to proposed (user cannot un-approve once approval
event is durable). approved_registered, modify_requested, and
declined are terminal for the row — new proposals create new rows
(potentially with prev_proposal_id pointer).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# State + kind literals
# ---------------------------------------------------------------------------


ProposalState = Literal[
    "proposed",
    "approved_pending_registration",
    "approved_registered",
    "modify_requested",
    "declined",
]

ResponseKind = Literal["approve", "modify", "not_now", "abandon"]

AmbiguityKind = Literal["multiple_intents", "modification_target"]


# Permitted transitions. The set is the structural pin tested at the
# store boundary — any transition not in this map raises
# InvalidStateTransition.
PermittedTransitions: dict[str, frozenset[str]] = {
    "proposed": frozenset({
        "approved_pending_registration",
        "modify_requested",
        "declined",
    }),
    "approved_pending_registration": frozenset({"approved_registered"}),
    "approved_registered": frozenset(),  # terminal
    "modify_requested": frozenset(),     # terminal
    "declined": frozenset(),             # terminal
}


_TERMINAL_STATES: frozenset[str] = frozenset({
    "approved_registered",
    "modify_requested",
    "declined",
})


def is_terminal_state(state: str) -> bool:
    """True if the given state has no outgoing transitions."""
    return state in _TERMINAL_STATES


# ---------------------------------------------------------------------------
# InstallProposal record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallProposal:
    """A durable in-flight proposal.

    Persisted by :class:`InstallProposalStore`. Mutation goes through
    ``transition_state`` / ``mark_surfaced`` / etc — the dataclass
    itself is frozen so callers can't accidentally mutate state.
    """

    proposal_id: str
    correlation_id: str
    instance_id: str
    draft_id: str
    descriptor_hash: str
    state: ProposalState
    proposal_text: str
    member_id: str
    source_thread_id: str
    authored_at: str
    prev_workflow_id: str | None = None
    prev_proposal_id: str | None = None
    surfaced_at: str | None = None
    responded_at: str | None = None
    response_kind: ResponseKind | None = None
    approval_event_id: str | None = None
    expires_at: str | None = None
    metadata: dict = field(default_factory=dict)
    # Codex final-review fold (REAL #3): the descriptor as authored at
    # proposal creation time. Recovery sweep registers this snapshot
    # rather than re-deriving from the current draft, so a draft that
    # drifts after approval but before crash recovery cannot register
    # an unapproved descriptor. Required at create_proposal time.
    descriptor_snapshot: dict | None = None
    # Codex final-review hardening: the substrate event_id of the
    # routine.proposed event emitted at surface time. Persisted so
    # surface_and_emit is idempotent across crashes / duplicate
    # surfacing calls.
    proposed_event_id: str | None = None


# ---------------------------------------------------------------------------
# Flow types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowResponse:
    """Typed user response to a surfaced proposal."""

    kind: ResponseKind
    feedback_summary: str | None = None  # only for kind='modify'

    def __post_init__(self) -> None:
        if self.kind == "modify" and not self.feedback_summary:
            raise ValueError(
                "FlowResponse(kind='modify') requires feedback_summary"
            )


# Recognized FlowOutcome kinds. Kept as a literal alias for typing;
# the strings themselves are the test surface.
FlowOutcomeKind = Literal[
    "approved",                  # registration complete; state=approved_registered
    "registration_pending",      # routine.approved emitted; STS deferred / recovery sweep retries
    "modify_dispatched",         # crb.feedback.modify_request emitted
    "declined_pause",            # state=declined; draft stays shaping/blocked
    "declined_abandon",          # state=declined; routine.declined emitted
    "already_approved",          # duplicate yes (Seam C11 case 1)
    "descriptor_drifted",        # late approval after hash change (case 2)
    "draft_abandoned",           # late approval after draft abandoned (case 3)
    "sts_already_consumed",      # late approval after STS register (case 4)
    "draft_superseded",          # approval to old proposal whose draft was modified (case 5)
    "expired",                   # TTL elapsed (case 6; v1.x opt-in)
]


@dataclass(frozen=True)
class FlowOutcome:
    """Result of :meth:`CRBApprovalFlow.handle_response`."""

    kind: FlowOutcomeKind
    proposal: InstallProposal
    workflow_id: str | None = None
    new_proposal_id: str | None = None
    approval_event_id: str | None = None
    user_facing_message: str | None = None


@dataclass(frozen=True)
class ExplicitModificationOutcome:
    """Result of :meth:`CRBApprovalFlow.handle_explicit_modification_request`."""

    kind: Literal["dispatched", "ambiguous", "no_match"]
    target_workflow_id: str | None = None
    candidates: tuple[dict, ...] = ()
    user_facing_message: str | None = None


@dataclass(frozen=True)
class DisambiguationOutcome:
    """Result of :meth:`CRBApprovalFlow.handle_disambiguation_response`."""

    picked_candidate_id: str
    paused_draft_ids: tuple[str, ...] = ()
    ephemeral_candidate_ids: tuple[str, ...] = ()


__all__ = [
    "AmbiguityKind",
    "DisambiguationOutcome",
    "ExplicitModificationOutcome",
    "FlowOutcome",
    "FlowOutcomeKind",
    "FlowResponse",
    "InstallProposal",
    "PermittedTransitions",
    "ProposalState",
    "ResponseKind",
    "is_terminal_state",
]
