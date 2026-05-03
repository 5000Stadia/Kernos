"""Substrate Tools error hierarchy.

All STS-raised errors derive from :class:`SubstrateToolsError`. C1
introduced the base; C2 fills in the approval-validation hierarchy
(binding-missing, event-not-found, authority-spoofed, hash-mismatch,
modification-target binding, terminal-already-consumed).
"""
from __future__ import annotations


class SubstrateToolsError(Exception):
    """Base for all STS-raised errors."""


class ApprovalBindingMissing(SubstrateToolsError):
    """Raised when ``register_workflow(dry_run=False)`` is called without
    an ``approval_event_id``. Production registration is approval-bound
    by design; the registry has no fallback path."""


class ApprovalEventNotFound(SubstrateToolsError):
    """Raised when ``approval_event_id`` does not resolve to any event in
    the event stream for the calling instance."""


class ApprovalEventTypeInvalid(SubstrateToolsError):
    """Raised when the event resolved by ``approval_event_id`` is not a
    ``routine.approved`` or ``routine.modification.approved`` event."""


class ApprovalAuthoritySpoofed(SubstrateToolsError):
    """Raised when an approval event's substrate-set envelope reports a
    ``source_module`` other than ``"crb"``.

    STS reads from the substrate-set envelope, NEVER from caller-supplied
    payload — payload contents have no bearing on this check. See the
    EmitterRegistry trust boundary in :mod:`kernos.kernel.event_stream`.
    """


class ApprovalAuthorityIncomplete(SubstrateToolsError):
    """Raised when an approval event is missing one or more required
    provenance fields: ``approved_by``, ``member_id``, ``source_turn_id``,
    ``correlation_id``, ``descriptor_hash``, ``instance_id`` (and
    ``prev_workflow_id`` for modifications)."""


class ApprovalProvenanceUnverifiable(SubstrateToolsError):
    """Raised when an approval's ``correlation_id`` does not resolve to
    a ``routine.proposed`` event in the same instance, or when the
    proposed event itself was not emitted under the CRB envelope."""


class ApprovalProposalMismatch(SubstrateToolsError):
    """Raised when the proposed event's ``descriptor_hash`` does not
    match the approval event's ``descriptor_hash``."""


class ApprovalInstanceMismatch(SubstrateToolsError):
    """Raised when the approval event's ``instance_id`` does not match
    the calling instance, or when the approval and proposal events
    disagree on instance."""


class ApprovalModificationTargetMismatch(SubstrateToolsError):
    """Raised when a modification approval's ``prev_workflow_id`` does
    not match the descriptor's ``prev_version_id`` (the design review edit v1→v2).

    This closes the failure mode where the same modification descriptor
    body could be approved against routine A and applied to routine B
    by swapping ``prev_workflow_id`` in the registration call."""


class ApprovalModificationTargetMissing(SubstrateToolsError):
    """Raised when a modification approval's ``prev_workflow_id`` does
    not exist as a workflow in the calling instance."""


class ApprovalDescriptorMismatch(SubstrateToolsError):
    """Raised when the recomputed descriptor hash at registration time
    does not match the approval event's ``descriptor_hash``. Catches
    descriptor mutation between proposal and registration AND any caller
    bug feeding a different descriptor than was approved.

    Now that ``prev_version_id`` is in the hash (the design review edit v1→v2), this
    also catches modification-target swap attempts as a belt-and-
    suspenders backstop to
    :class:`ApprovalModificationTargetMismatch`."""


class ApprovalAlreadyConsumed(SubstrateToolsError):
    """Raised when ``approval_event_id`` has already been consumed by a
    prior successful registration. TERMINAL FAILURE MODE.

    Translated from SQLite UNIQUE constraint violation on
    ``(instance_id, approval_event_id)``. Caller MUST NOT retry.
    v1 does NOT implement idempotent return-of-existing-workflow."""


class RegistrationValidationFailed(SubstrateToolsError):
    """Raised when revalidation at registration time produces error-
    severity issues. Carries the ``issues`` list as the ``issues``
    attribute so callers can inspect failures programmatically."""

    def __init__(self, message: str, issues: list = None) -> None:  # type: ignore[assignment]
        super().__init__(message)
        self.issues = list(issues or [])


__all__ = [
    "ApprovalAlreadyConsumed",
    "ApprovalAuthorityIncomplete",
    "ApprovalAuthoritySpoofed",
    "ApprovalBindingMissing",
    "ApprovalDescriptorMismatch",
    "ApprovalEventNotFound",
    "ApprovalEventTypeInvalid",
    "ApprovalInstanceMismatch",
    "ApprovalModificationTargetMismatch",
    "ApprovalModificationTargetMissing",
    "ApprovalProposalMismatch",
    "ApprovalProvenanceUnverifiable",
    "RegistrationValidationFailed",
    "SubstrateToolsError",
]
