"""Drafter receipt pattern + timeout (DRAFTER spec D5 receipt + Codex audit).

Every cross-phase async hand-off produces an observable receipt event.
Four receipt types:

* ``drafter.receipt.signal_emitted`` — Drafter emits immediately after
  any ``drafter.signal.*``.
* ``drafter.receipt.signal_acknowledged`` — principal cohort emits
  when it ingests the signal into its turn-assembly queue. NOT when
  the principal speaks to the user — that may be much later.
* ``drafter.receipt.draft_updated`` — Drafter emits after WDP
  ``update_draft`` succeeds.
* ``drafter.receipt.dry_run_completed`` — Drafter emits after STS
  ``register_workflow(dry_run=True)`` returns.

Receipt timeout pin (DRAFTER spec, the design review pin v1→v2): default 60s at
substrate-delivery level — the principal cohort acks when it ingests
into its queue, NOT when it speaks to the user. Threshold configurable
per-instance via :class:`ReceiptTimeoutConfig`. When principal is in a
known-paused state (degraded startup, soak-mode, manual pause), timeout
is disabled or escalated to diagnostic-only event without raising —
avoids paging on intentional latency.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Receipt type constants
# ---------------------------------------------------------------------------


RECEIPT_SIGNAL_EMITTED = "drafter.receipt.signal_emitted"
RECEIPT_SIGNAL_ACKNOWLEDGED = "drafter.receipt.signal_acknowledged"
RECEIPT_DRAFT_UPDATED = "drafter.receipt.draft_updated"
RECEIPT_DRY_RUN_COMPLETED = "drafter.receipt.dry_run_completed"
# v1.1: emitted on successful handling of a crb.feedback.modify_request
# event. Confirms to CRB that the feedback was ingested + bound to the
# named draft for re-shaping in the next semantic evaluation pass.
RECEIPT_FEEDBACK_RECEIVED = "drafter.receipt.feedback_received"


RECEIPT_TYPES: frozenset[str] = frozenset({
    RECEIPT_SIGNAL_EMITTED,
    RECEIPT_SIGNAL_ACKNOWLEDGED,
    RECEIPT_DRAFT_UPDATED,
    RECEIPT_DRY_RUN_COMPLETED,
    RECEIPT_FEEDBACK_RECEIVED,
})
"""The exact set. Adding a receipt type is a deliberate substrate
change; tests assert this exact set."""


# ---------------------------------------------------------------------------
# Receipt timeout configuration (the design review pin v1→v2)
# ---------------------------------------------------------------------------


DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_PAUSED_STATES: frozenset[str] = frozenset({
    "degraded_startup",
    "soak_paused",
    "manual_pause",
})


@dataclass(frozen=True)
class ReceiptTimeoutConfig:
    """Per-instance receipt-timeout knobs.

    When the principal's known state is one of ``paused_states``, the
    timeout is suppressed (or escalated to diagnostic-only event)
    instead of raising :class:`DrafterReceiptTimeout`. This avoids
    paging on intentional latency (degraded startup, soak-mode, etc.).
    """

    threshold_seconds: int = DEFAULT_TIMEOUT_SECONDS
    enabled: bool = True
    paused_states: frozenset[str] = field(
        default_factory=lambda: DEFAULT_PAUSED_STATES,
    )

    def __post_init__(self) -> None:
        if self.threshold_seconds <= 0:
            raise ValueError("threshold_seconds must be positive")
        if not isinstance(self.paused_states, frozenset):
            object.__setattr__(
                self, "paused_states", frozenset(self.paused_states),
            )

    def is_paused(self, *, principal_state: str | None) -> bool:
        """True if the configured paused-state list includes the
        provided principal state."""
        if not principal_state:
            return False
        return principal_state in self.paused_states


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def build_signal_emitted_payload(
    *,
    signal_type: str,
    signal_id: str,
    target_cohort: str = "principal",
    emitted_at: str | None = None,
) -> dict:
    """Payload for :data:`RECEIPT_SIGNAL_EMITTED`."""
    if not signal_type:
        raise ValueError("signal_type is required")
    if not signal_id:
        raise ValueError("signal_id is required")
    return {
        "signal_type": signal_type,
        "signal_id": signal_id,
        "target_cohort": target_cohort,
        "emitted_at": emitted_at or _now(),
    }


def build_signal_acknowledged_payload(
    *,
    signal_id: str,
    acknowledged_at: str | None = None,
) -> dict:
    """Payload for :data:`RECEIPT_SIGNAL_ACKNOWLEDGED`. Emitted by the
    principal cohort, NOT by Drafter; documented here for shape
    completeness."""
    if not signal_id:
        raise ValueError("signal_id is required")
    return {
        "signal_id": signal_id,
        "acknowledged_at": acknowledged_at or _now(),
    }


def build_draft_updated_payload(
    *,
    draft_id: str,
    instance_id: str,
    version_after: int,
) -> dict:
    """Payload for :data:`RECEIPT_DRAFT_UPDATED`."""
    if not draft_id:
        raise ValueError("draft_id is required")
    return {
        "draft_id": draft_id,
        "instance_id": instance_id,
        "version_after": version_after,
    }


def build_feedback_received_payload(
    *,
    draft_id: str,
    instance_id: str,
    original_proposal_id: str,
    source_event_id: str,
) -> dict:
    """Payload for :data:`RECEIPT_FEEDBACK_RECEIVED` (v1.1).

    Confirms CRB's modification request was ingested + bound to the
    named draft for re-shaping. ``original_proposal_id`` provides
    provenance back to the InstallProposal that triggered the
    feedback flow."""
    if not draft_id:
        raise ValueError("draft_id is required")
    if not original_proposal_id:
        raise ValueError("original_proposal_id is required")
    if not source_event_id:
        raise ValueError("source_event_id is required")
    return {
        "draft_id": draft_id,
        "instance_id": instance_id,
        "original_proposal_id": original_proposal_id,
        "source_event_id": source_event_id,
    }


def build_dry_run_completed_payload(
    *,
    draft_id: str,
    descriptor_hash: str,
    valid: bool,
    issue_count: int,
    capability_gap_count: int,
) -> dict:
    """Payload for :data:`RECEIPT_DRY_RUN_COMPLETED`."""
    if not draft_id:
        raise ValueError("draft_id is required")
    if not descriptor_hash:
        raise ValueError("descriptor_hash is required")
    return {
        "draft_id": draft_id,
        "descriptor_hash": descriptor_hash,
        "valid": valid,
        "issue_count": issue_count,
        "capability_gap_count": capability_gap_count,
    }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "DEFAULT_PAUSED_STATES",
    "DEFAULT_TIMEOUT_SECONDS",
    "RECEIPT_DRAFT_UPDATED",
    "RECEIPT_DRY_RUN_COMPLETED",
    "RECEIPT_FEEDBACK_RECEIVED",
    "RECEIPT_SIGNAL_ACKNOWLEDGED",
    "RECEIPT_SIGNAL_EMITTED",
    "RECEIPT_TYPES",
    "ReceiptTimeoutConfig",
    "build_draft_updated_payload",
    "build_dry_run_completed_payload",
    "build_feedback_received_payload",
    "build_signal_acknowledged_payload",
    "build_signal_emitted_payload",
]
