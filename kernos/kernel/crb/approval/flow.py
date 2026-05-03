"""CRBApprovalFlow state machine (CRB C4).

Owns ``handle_response`` (with crash-safe approval-to-registration
handoff per the design review pin v1->v2 must-fix #1), branches event emission by
proposal type (must-fix #2: modifications emit
``routine.modification.approved``, not the generic event),
``handle_explicit_modification_request`` fallback when Drafter misses
the modification intent, ``handle_disambiguation_response`` with
permission gate per Drafter v2 AC #12, and the
``recover_pending_registrations`` engine-startup sweep.

Six duplicate / late approval cases (Seam C11) are handled inline in
``handle_response``:

1. Already-approved proposal (terminal) + approve -> already_approved.
1b. Approve while pending registration -> trigger recovery sweep
    synchronously; either already_approved (recovery succeeded) or
    registration_pending (still deferred).
2. Descriptor drift between proposal and approval -> re-author against
   current draft; new install_proposals row with prev_proposal_id.
3. Draft abandoned since proposal -> draft_abandoned outcome; no
   approval event.
4. STS already_consumed (race winner) -> sts_already_consumed outcome
   when find_workflow_by_approval_event_id returns None despite the
   error (substrate inconsistency).
5. Draft superseded into newer version -> draft_superseded outcome;
   re-author against newer draft.
6. TTL expired -> expired outcome (v1.x; field present but enforcement
   only when expires_at is populated by caller).

Elegance latitude: spec called for a separate ``duplicate_handling.py``
module for the C11 cases. v1 keeps them inline in ``handle_response``
because they're branch logic specific to that one method; a separate
module would add indirection without reuse. Will note in batch report.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Literal

from kernos.kernel.crb.approval.ports import (
    CRBEventPort,
    DraftReadPort,
    STSRegistrationPort,
    STSTransientError,
)
from kernos.kernel.crb.compiler.translation import (
    draft_to_descriptor_candidate,
)
from kernos.kernel.crb.proposal.author import CRBProposalAuthor
from kernos.kernel.crb.proposal.install_proposal import (
    DisambiguationOutcome,
    ExplicitModificationOutcome,
    FlowOutcome,
    FlowResponse,
    InstallProposal,
)
from kernos.kernel.crb.proposal.install_proposal_store import (
    InstallProposalStore,
    InvalidStateTransition,
    StaleStateError,
    UnknownProposal,
)

# STS error class lives in the substrate_tools package; we catch it
# at this layer and translate into FlowOutcome(kind='sts_already_consumed').
try:
    from kernos.kernel.substrate_tools.errors import (
        ApprovalAlreadyConsumed,
    )
except Exception:  # pragma: no cover - defensive
    ApprovalAlreadyConsumed = type(  # type: ignore[assignment]
        "ApprovalAlreadyConsumed", (Exception,), {},
    )


if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.workflows.workflow_registry import Workflow


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ApprovalFlowError(Exception):
    """Base for ApprovalFlow errors."""


class ProposalNotPendingForRecovery(ApprovalFlowError):
    """recover_pending_registration called against a proposal not in
    state=approved_pending_registration."""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# CRBApprovalFlow
# ---------------------------------------------------------------------------


class CRBApprovalFlow:
    """State machine: proposed -> approved_pending_registration ->
    approved_registered | modify_requested | declined.

    Owns duplicate / late approval handling at the CRB layer. STS
    error classes are caught here and translated into typed
    FlowOutcomes so the principal cohort sees a clean conversational
    surface.
    """

    def __init__(
        self,
        *,
        install_proposal_store: InstallProposalStore,
        draft_port: DraftReadPort,
        sts_port: STSRegistrationPort,
        event_emitter: CRBEventPort,
        author: CRBProposalAuthor,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = install_proposal_store
        self._draft_port = draft_port
        self._sts_port = sts_port
        self._event_emitter = event_emitter
        self._author = author
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # === handle_response ===========================================

    async def handle_response(
        self,
        *,
        proposal_id: str,
        response: FlowResponse,
        source_turn_id: str,
        member_id: str,
    ) -> FlowOutcome:
        """Map a user response to event emission, store mutation, and
        a typed FlowOutcome. Six duplicate/late cases handled inline.
        """
        proposal = await self._store.get_proposal(proposal_id=proposal_id)
        if proposal is None:
            raise UnknownProposal(
                f"no proposal with proposal_id={proposal_id!r}"
            )

        # Case 1: duplicate "yes" — already approved (terminal).
        if (
            proposal.state == "approved_registered"
            and response.kind == "approve"
        ):
            workflow = await self._sts_port.find_workflow_by_approval_event_id(
                instance_id=proposal.instance_id,
                approval_event_id=proposal.approval_event_id or "",
            )
            return FlowOutcome(
                kind="already_approved",
                proposal=proposal,
                workflow_id=workflow.workflow_id if workflow else None,
            )

        # Case 1b: duplicate "yes" while still pending registration.
        if (
            proposal.state == "approved_pending_registration"
            and response.kind == "approve"
        ):
            recovered = await self._try_recover_pending_registration(proposal)
            if recovered is not None:
                # Recovery completed; fetch refreshed proposal.
                latest = await self._store.get_proposal(
                    proposal_id=proposal.proposal_id,
                )
                return FlowOutcome(
                    kind="already_approved",
                    proposal=latest or proposal,
                    workflow_id=recovered.workflow_id,
                )
            return FlowOutcome(
                kind="registration_pending",
                proposal=proposal,
                approval_event_id=proposal.approval_event_id,
            )

        # Case 6: TTL expired (v1.x opt-in; only fires when expires_at
        # was populated at create time).
        if proposal.expires_at:
            if self._clock().isoformat() > proposal.expires_at:
                return FlowOutcome(kind="expired", proposal=proposal)

        # Case 3: draft abandoned since proposal.
        current_draft = await self._draft_port.get_draft(
            instance_id=proposal.instance_id, draft_id=proposal.draft_id,
        )
        if current_draft is None or current_draft.status == "abandoned":
            return FlowOutcome(
                kind="draft_abandoned", proposal=proposal,
            )

        # Case 5: draft superseded by newer modified version. v1 uses
        # the partial_spec_json metadata next_version_id pointer if
        # set; otherwise no supersede path.
        next_version_id = (
            (current_draft.partial_spec_json or {}).get("next_version_id")
        )
        if next_version_id:
            return FlowOutcome(
                kind="draft_superseded",
                proposal=proposal,
                new_proposal_id=None,  # filled by re-author callers in v1.x
            )

        # Approve branch (Cases 2, 4, happy path with crash-safe handoff).
        if response.kind == "approve":
            # Case 2: descriptor drifted between proposal and approval.
            current_hash = self._compute_descriptor_hash(current_draft)
            if current_hash != proposal.descriptor_hash:
                return FlowOutcome(
                    kind="descriptor_drifted",
                    proposal=proposal,
                    new_proposal_id=None,
                )

            # Crash-safe handoff (the design review pin v1->v2 must-fix #1, hardened
            # by Codex final review REAL #1 + #4):
            #
            # Step 1: CLAIM via state transition BEFORE the approval
            # event is emitted. The transition is conditional on
            # state='proposed'; only one concurrent approve can
            # succeed. Losers fall through to the actual stored
            # state and return a FlowOutcome that matches the
            # row, never overwriting and never orphaning an emitted
            # event.
            try:
                proposal = await self._store.transition_state(
                    proposal_id=proposal.proposal_id,
                    new_state="approved_pending_registration",
                    response_kind="approve",
                )
            except (StaleStateError, InvalidStateTransition):
                # StaleStateError: another path won the conditional
                # UPDATE inside transition_state. InvalidStateTransition:
                # the row is already in a non-`proposed` state when
                # transition_state read it (e.g. modify_requested,
                # declined, approved_registered). Both cases mean the
                # claim is unavailable; re-fetch and map to the actual
                # stored state's outcome.
                latest = (
                    await self._store.get_proposal(
                        proposal_id=proposal.proposal_id,
                    )
                ) or proposal
                return await self._outcome_for_concurrent_state(latest)

            # Step 2: we won the claim — emit the appropriate approval
            # event. Modification branch per the design review pin v1->v2 must-fix #2.
            if proposal.prev_workflow_id:
                approval_event_id = await self._event_emitter.emit_routine_modification_approved(
                    correlation_id=proposal.correlation_id,
                    proposal_id=proposal.proposal_id,
                    instance_id=proposal.instance_id,
                    descriptor_hash=proposal.descriptor_hash,
                    prev_workflow_id=proposal.prev_workflow_id,
                    change_summary=self._compute_change_summary(
                        current_draft, proposal,
                    ),
                    member_id=member_id,
                    source_turn_id=source_turn_id,
                )
            else:
                approval_event_id = await self._event_emitter.emit_routine_approved(
                    correlation_id=proposal.correlation_id,
                    proposal_id=proposal.proposal_id,
                    instance_id=proposal.instance_id,
                    descriptor_hash=proposal.descriptor_hash,
                    member_id=member_id,
                    source_turn_id=source_turn_id,
                )

            # Step 3: idempotently bind approval_event_id to the row.
            proposal = await self._store.set_approval_event_id_for_pending(
                proposal_id=proposal.proposal_id,
                approval_event_id=approval_event_id,
            )

            # Step 4: attempt STS registration. Codex REAL #3 fold:
            # use the persisted descriptor snapshot rather than
            # re-deriving from the current draft, so a draft mutation
            # between proposal creation and approval cannot register
            # an unapproved descriptor.
            descriptor = self._descriptor_for_registration(
                proposal, current_draft,
            )
            try:
                workflow = await self._sts_port.register_workflow(
                    instance_id=proposal.instance_id,
                    descriptor=descriptor,
                    approval_event_id=approval_event_id,
                )
                proposal = await self._idempotent_transition_to_registered(
                    proposal,
                )
                return FlowOutcome(
                    kind="approved",
                    proposal=proposal,
                    workflow_id=workflow.workflow_id,
                )
            except ApprovalAlreadyConsumed:
                # Race or replay: STS already registered with this
                # approval_event_id. Recover by lookup.
                workflow = await self._sts_port.find_workflow_by_approval_event_id(
                    instance_id=proposal.instance_id,
                    approval_event_id=approval_event_id,
                )
                if workflow is not None:
                    proposal = await self._idempotent_transition_to_registered(
                        proposal,
                    )
                    return FlowOutcome(
                        kind="approved",
                        proposal=proposal,
                        workflow_id=workflow.workflow_id,
                    )
                # Case 4: substrate inconsistency.
                return FlowOutcome(
                    kind="sts_already_consumed",
                    proposal=proposal,
                )
            except STSTransientError:
                logger.warning(
                    "CRB_PENDING_REGISTRATION: proposal=%s "
                    "approval_event_id=%s left pending after STS "
                    "transient failure",
                    proposal.proposal_id, approval_event_id,
                )
                return FlowOutcome(
                    kind="registration_pending",
                    proposal=proposal,
                    approval_event_id=approval_event_id,
                )

        # Modify branch.
        if response.kind == "modify":
            await self._event_emitter.emit_crb_feedback_modify_request(
                instance_id=proposal.instance_id,
                draft_id=proposal.draft_id,
                original_proposal_id=proposal.proposal_id,
                feedback_summary=response.feedback_summary or "",
                source_turn_id=source_turn_id,
                member_id=member_id,
            )
            proposal = await self._store.transition_state(
                proposal_id=proposal.proposal_id,
                new_state="modify_requested",
                response_kind="modify",
            )
            return FlowOutcome(
                kind="modify_dispatched", proposal=proposal,
            )

        # Decline branches.
        if response.kind == "not_now":
            proposal = await self._store.transition_state(
                proposal_id=proposal.proposal_id,
                new_state="declined",
                response_kind="not_now",
            )
            return FlowOutcome(kind="declined_pause", proposal=proposal)

        if response.kind == "abandon":
            proposal = await self._store.transition_state(
                proposal_id=proposal.proposal_id,
                new_state="declined",
                response_kind="abandon",
            )
            await self._event_emitter.emit_routine_declined(
                correlation_id=proposal.correlation_id,
                proposal_id=proposal.proposal_id,
                instance_id=proposal.instance_id,
                draft_id=proposal.draft_id,
                decline_reason="user_explicit_stop",
                member_id=member_id,
            )
            return FlowOutcome(
                kind="declined_abandon", proposal=proposal,
            )

        raise ApprovalFlowError(
            f"unknown response kind {response.kind!r}"
        )

    # === handle_explicit_modification_request =====================

    async def handle_explicit_modification_request(
        self,
        *,
        instance_id: str,
        target_workflow_id: str,
        feedback_summary: str,
        source_turn_id: str,
        member_id: str,
        candidate_workflow_lookup: Callable[
            [str, str], "Awaitable[list[Workflow]]"  # type: ignore[name-defined]
        ] | None = None,
    ) -> ExplicitModificationOutcome:
        """Fallback when Drafter misses a modification intent (the design review pin
        C4). Resolves ``target_workflow_id`` via the supplied lookup;
        on a single match, emits ``crb.feedback.modify_request`` to
        drive the Drafter shaping path. On ambiguity, returns an
        outcome the principal can use to author a clarification. On
        no-match, returns a gap-shaped outcome.

        ``candidate_workflow_lookup`` is the resolver: ``(instance_id,
        target_workflow_id) -> list[Workflow]``. Engine bring-up wires
        STS list_workflows + DAR. Tests inject deterministic stubs.
        """
        if not target_workflow_id:
            raise ValueError("target_workflow_id is required")

        if candidate_workflow_lookup is None:
            return ExplicitModificationOutcome(
                kind="no_match",
                target_workflow_id=target_workflow_id,
            )

        candidates = await candidate_workflow_lookup(
            instance_id, target_workflow_id,
        )
        if not candidates:
            return ExplicitModificationOutcome(
                kind="no_match",
                target_workflow_id=target_workflow_id,
            )
        if len(candidates) > 1:
            return ExplicitModificationOutcome(
                kind="ambiguous",
                target_workflow_id=target_workflow_id,
                candidates=tuple(
                    {
                        "workflow_id": w.workflow_id,
                        "name": getattr(w, "name", ""),
                    }
                    for w in candidates
                ),
            )
        # Single match: emit modify_request.
        target = candidates[0]
        # We need a draft_id to drive Drafter shaping. v1 derives it
        # via the existing draft for this workflow, OR creates a
        # synthetic draft_id. For simplicity in v1, the principal is
        # responsible for ensuring a draft exists; the explicit-modify
        # entry point just emits the feedback signal.
        await self._event_emitter.emit_crb_feedback_modify_request(
            instance_id=instance_id,
            draft_id=target.workflow_id,  # use workflow_id as draft hint
            original_proposal_id="",  # no proposal in flight yet
            feedback_summary=feedback_summary,
            source_turn_id=source_turn_id,
            member_id=member_id,
        )
        return ExplicitModificationOutcome(
            kind="dispatched",
            target_workflow_id=target.workflow_id,
        )

    # === handle_disambiguation_response ===========================

    async def handle_disambiguation_response(
        self,
        *,
        instance_id: str,
        signal_id: str,
        picked_candidate_id: str,
        unselected_candidates: list[dict],
        source_turn_id: str,
        member_id: str,
    ) -> DisambiguationOutcome:
        """Route the user's disambiguation choice. Picked candidate
        becomes active; unselected candidates evaluated INDEPENDENTLY
        for ``permission_to_make_durable``:

        * permission=True -> paused-for-later WDP draft (mediated by
          principal via Drafter port; CRB just reports the decision).
        * permission=False -> ephemeral; NO WDP row.

        Codex / the design review pin: Drafter v2 AC #12 invariant preserved across
        this CRB entry point — weak candidates stay ephemeral.
        """
        if not picked_candidate_id:
            raise ValueError("picked_candidate_id is required")

        paused_draft_ids: list[str] = []
        ephemeral_ids: list[str] = []
        for cand in unselected_candidates:
            cand_id = cand.get("candidate_id") or ""
            if not cand_id:
                logger.warning(
                    "CRB_DISAMBIG_MISSING_CANDIDATE_ID: signal=%s "
                    "candidate=%r — treating as ephemeral",
                    signal_id, cand,
                )
                continue
            permission = bool(cand.get("permission_to_make_durable"))
            if permission:
                # Principal-mediated paused-draft creation. CRB reports
                # the decision; the actual port call is done by the
                # principal (Drafter port write capability is not on
                # CRB's surface).
                paused_draft_ids.append(cand_id)
            else:
                ephemeral_ids.append(cand_id)

        return DisambiguationOutcome(
            picked_candidate_id=picked_candidate_id,
            paused_draft_ids=tuple(paused_draft_ids),
            ephemeral_candidate_ids=tuple(ephemeral_ids),
        )

    # === recover_pending_registrations ============================

    async def recover_pending_registrations(self) -> list[InstallProposal]:
        """Engine-startup sweep. Walks every install_proposals row in
        state=approved_pending_registration and attempts to resume
        registration. Returns the list of proposals successfully
        transitioned to approved_registered.

        Idempotent: running the sweep N times produces the same
        outcome as running it once. Safe to invoke synchronously
        from Case 1b.
        """
        pending = await self._store.find_by_state(
            state="approved_pending_registration",
        )
        recovered: list[InstallProposal] = []
        for proposal in pending:
            workflow = await self._try_recover_pending_registration(proposal)
            if workflow is not None:
                latest = await self._store.get_proposal(
                    proposal_id=proposal.proposal_id,
                )
                if latest is not None:
                    recovered.append(latest)
        return recovered

    async def _try_recover_pending_registration(
        self, proposal: InstallProposal,
    ) -> "Workflow | None":
        """Idempotent recovery for a single pending proposal. See
        spec section "Crash recovery for pending registrations"."""
        if proposal.state != "approved_pending_registration":
            return None
        approval_event_id = proposal.approval_event_id or ""
        if not approval_event_id:
            # Codex C6.1 follow-on: claim-before-emit can leave the
            # row in pending with no recorded approval_event_id if
            # the process crashed between the claim and emission.
            # Recovery cannot guess the missing id; expose for
            # operator triage via
            # ``InstallProposalStore.find_orphaned_approval_claims``.
            logger.warning(
                "CRB_RECOVERY_ORPHAN_CLAIM: proposal=%s in pending "
                "state but approval_event_id is NULL — likely a "
                "process crash between state claim and approval "
                "emit; expose via find_orphaned_approval_claims for "
                "triage",
                proposal.proposal_id,
            )
            return None

        # Common case: STS already registered before crash.
        workflow = await self._sts_port.find_workflow_by_approval_event_id(
            instance_id=proposal.instance_id,
            approval_event_id=approval_event_id,
        )
        if workflow is not None:
            try:
                await self._store.transition_state(
                    proposal_id=proposal.proposal_id,
                    new_state="approved_registered",
                )
            except (StaleStateError, InvalidStateTransition):
                # Concurrent recovery sweep / handle_response already
                # moved the row to approved_registered (terminal -> no
                # outgoing transitions). Both errors are idempotent
                # successes here.
                pass
            return workflow

        # Otherwise: retry STS registration with the SAME approval_event_id
        # AND the persisted descriptor snapshot. Codex final-review fold
        # (REAL #3): never re-derive the descriptor from the live draft
        # at recovery time — a draft mutation after approval would
        # otherwise register an unapproved routine.
        if proposal.descriptor_snapshot is None:
            logger.warning(
                "CRB_RECOVERY_NO_SNAPSHOT: proposal=%s missing "
                "descriptor_snapshot; refusing recovery — pending for "
                "triage",
                proposal.proposal_id,
            )
            return None
        # Defensive: still consult the draft to detect explicit user
        # abandon. Abandon-after-approval is rare but informative; we
        # leave the row pending for triage rather than registering a
        # routine the user actively abandoned.
        current_draft = await self._draft_port.get_draft(
            instance_id=proposal.instance_id, draft_id=proposal.draft_id,
        )
        if current_draft is not None and current_draft.status == "abandoned":
            logger.warning(
                "CRB_RECOVERY_DRAFT_ABANDONED: proposal=%s draft=%s "
                "abandoned after approval; leaving pending for triage",
                proposal.proposal_id, proposal.draft_id,
            )
            return None

        descriptor = proposal.descriptor_snapshot
        try:
            workflow = await self._sts_port.register_workflow(
                instance_id=proposal.instance_id,
                descriptor=descriptor,
                approval_event_id=approval_event_id,
            )
            try:
                await self._store.transition_state(
                    proposal_id=proposal.proposal_id,
                    new_state="approved_registered",
                )
            except (StaleStateError, InvalidStateTransition):
                pass
            return workflow
        except ApprovalAlreadyConsumed:
            workflow = await self._sts_port.find_workflow_by_approval_event_id(
                instance_id=proposal.instance_id,
                approval_event_id=approval_event_id,
            )
            if workflow is not None:
                try:
                    await self._store.transition_state(
                        proposal_id=proposal.proposal_id,
                        new_state="approved_registered",
                    )
                except (StaleStateError, InvalidStateTransition):
                    pass
                return workflow
            return None
        except STSTransientError:
            return None

    # === surface_and_emit =========================================

    async def surface_and_emit(
        self,
        *,
        proposal_id: str,
    ) -> str:
        """Idempotently surface a proposal and emit
        ``routine.proposed`` exactly once.

        Codex final-review hardening: ``routine.proposed`` emission
        was previously decoupled from surfacing — a caller could mark
        the proposal surfaced via ``mark_surfaced`` while a separate
        path emitted ``routine.proposed`` directly, with no joint
        idempotency. This unifies them: the store's ``claim_surface``
        atomically claims the right to emit; the resulting
        ``proposed_event_id`` is bound to the row via
        ``record_proposed_event_id``. Replays return the existing id.
        """
        latest = await self._store.get_proposal(proposal_id=proposal_id)
        if latest is None:
            raise UnknownProposal(
                f"no proposal with proposal_id={proposal_id!r}"
            )
        if latest.proposed_event_id:
            return latest.proposed_event_id
        won = await self._store.claim_surface(proposal_id=proposal_id)
        if not won:
            # Surface was already claimed. Re-fetch and inspect.
            refreshed = await self._store.get_proposal(
                proposal_id=proposal_id,
            )
            if refreshed and refreshed.proposed_event_id:
                return refreshed.proposed_event_id
            # Codex C6.1 follow-on: orphan-claim recovery. The prior
            # surfacer set surfaced_at then crashed before recording
            # proposed_event_id. Retry emission now; record_proposed_
            # event_id is conditional on (NULL OR == new value), so
            # only one final id ever wins. The substrate may now hold
            # two routine.proposed events for the same proposal in
            # this rare crash race; STS approval-binding tolerates
            # multiple matches and "first match wins."
            latest = refreshed or latest
        proposed_event_id = await self._event_emitter.emit_routine_proposed(
            correlation_id=latest.correlation_id,
            proposal_id=latest.proposal_id,
            instance_id=latest.instance_id,
            draft_id=latest.draft_id,
            descriptor_hash=latest.descriptor_hash,
            member_id=latest.member_id,
            source_thread_id=latest.source_thread_id,
            prev_workflow_id=latest.prev_workflow_id,
        )
        try:
            await self._store.record_proposed_event_id(
                proposal_id=proposal_id,
                proposed_event_id=proposed_event_id,
            )
        except InvalidStateTransition:
            # A racing caller recorded a different id first; re-fetch
            # and return the canonical recorded id.
            refreshed = await self._store.get_proposal(
                proposal_id=proposal_id,
            )
            return (refreshed and refreshed.proposed_event_id) or proposed_event_id
        return proposed_event_id

    # === helpers ===================================================

    async def _idempotent_transition_to_registered(
        self, proposal: InstallProposal,
    ) -> InstallProposal:
        """Transition to ``approved_registered``, treating same-state
        and stale-state observations as idempotent success.

        Codex final-review fold (REAL #2): when the recovery sweep has
        already moved the row, ``transition_state`` raises
        ``InvalidStateTransition`` (terminal state has no outgoing
        transitions) rather than ``StaleStateError``. Both must be
        absorbed here so a successful STS registration that races a
        sweep-triggered transition does not bubble up as a crash.
        """
        if proposal.state == "approved_registered":
            return proposal
        try:
            return await self._store.transition_state(
                proposal_id=proposal.proposal_id,
                new_state="approved_registered",
            )
        except (StaleStateError, InvalidStateTransition):
            latest = await self._store.get_proposal(
                proposal_id=proposal.proposal_id,
            )
            return latest or proposal

    async def _outcome_for_concurrent_state(
        self, proposal: InstallProposal,
    ) -> FlowOutcome:
        """Map a non-``proposed`` state observed after a stale claim
        to the correct :class:`FlowOutcome`.

        Codex final-review fold (REAL #4): the previous fallthrough
        reported ``registration_pending`` for any non-pending stored
        state, including ``modify_requested`` and ``declined``. With
        claim-before-emit the loser of an approve race re-fetches and
        is routed here; the outcome matches the row, never the
        loser's intent.
        """
        if proposal.state == "approved_pending_registration":
            return FlowOutcome(
                kind="registration_pending",
                proposal=proposal,
                approval_event_id=proposal.approval_event_id,
            )
        if proposal.state == "approved_registered":
            workflow = None
            if proposal.approval_event_id:
                workflow = await self._sts_port.find_workflow_by_approval_event_id(
                    instance_id=proposal.instance_id,
                    approval_event_id=proposal.approval_event_id,
                )
            return FlowOutcome(
                kind="already_approved",
                proposal=proposal,
                workflow_id=workflow.workflow_id if workflow else None,
            )
        if proposal.state == "modify_requested":
            return FlowOutcome(kind="modify_dispatched", proposal=proposal)
        if proposal.state == "declined":
            kind = (
                "declined_abandon"
                if proposal.response_kind == "abandon"
                else "declined_pause"
            )
            return FlowOutcome(kind=kind, proposal=proposal)
        # Defensive fallback for an unexpected concurrent state.
        return FlowOutcome(
            kind="registration_pending",
            proposal=proposal,
            approval_event_id=proposal.approval_event_id,
        )

    @staticmethod
    def _descriptor_for_registration(
        proposal: InstallProposal, current_draft: Any,
    ) -> dict:
        """Pick the descriptor STS will register against.

        Codex final-review fold (REAL #3): prefer the snapshot
        captured at proposal creation. Falling back to the live
        draft is only safe when the descriptor hash matches (verified
        upstream by the descriptor-drift Case 2 gate); the snapshot
        is the durable source of truth and what the recovery sweep
        also uses, so happy-path and recovery-path are uniform.
        """
        if proposal.descriptor_snapshot is not None:
            return proposal.descriptor_snapshot
        return draft_to_descriptor_candidate(current_draft)

    @staticmethod
    def _compute_descriptor_hash(draft: Any) -> str:
        """Compute the descriptor hash for the draft as it stands now.
        Used to detect Case 2 (descriptor drift between proposal and
        approval). Imports STS canonical hash to keep parity with
        STS's approval-binding gate."""
        from kernos.kernel.substrate_tools.registration.descriptor_hash import (
            compute_descriptor_hash,
        )

        candidate = draft_to_descriptor_candidate(draft)
        return compute_descriptor_hash(candidate)

    @staticmethod
    def _compute_change_summary(
        current_draft: Any, proposal: InstallProposal,
    ) -> str:
        """v1 deterministic change-summary string for modification
        approvals. The user-facing diff narration lives in
        CRBProposalAuthor.author_modification_proposal; this is the
        machine-readable summary that flows into the
        ``routine.modification.approved`` event payload."""
        spec = current_draft.partial_spec_json or {}
        triggers = spec.get("triggers") or []
        actions = spec.get("action_sequence") or []
        return (
            f"draft_id={current_draft.draft_id} "
            f"intent={current_draft.intent_summary or ''} "
            f"triggers={len(triggers)} actions={len(actions)} "
            f"prev_workflow_id={proposal.prev_workflow_id or ''}"
        )


__all__ = [
    "ApprovalFlowError",
    "CRBApprovalFlow",
    "ProposalNotPendingForRecovery",
]
