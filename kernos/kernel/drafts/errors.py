"""Typed error hierarchy for the workflow draft primitive.

Per SPEC-WDP: 1 base + 8 typed subclasses. Distinct error types
let callers handle different failure modes without parsing
strings. ``DraftAliasCollision`` is intentionally distinct from
``DraftEnvelopeInvalid`` (the design review edit, v1 → v2) because the failure
mode and remediation differ.
"""
from __future__ import annotations


class DraftError(Exception):
    """Base for all WDP-raised errors."""


class DraftNotFound(DraftError):
    """Raised by mutations when ``(draft_id, instance_id)`` doesn't
    resolve to a row."""


class DraftTerminal(DraftError):
    """Raised on any mutation against committed or abandoned rows.
    Reads still succeed via ``get_draft`` and ``list_drafts`` with
    ``include_terminal=True`` or an explicit status filter."""


class InvalidDraftTransition(DraftError):
    """Raised on a forbidden status transition. The state machine
    matrix lives in the spec; in code we enforce it via an
    allowed-pairs table consulted on every status mutation."""


class DraftConcurrentModification(DraftError):
    """Raised when ``expected_version`` doesn't match the current
    row's version (optimistic concurrency / compare-and-swap)."""


class DraftEnvelopeInvalid(DraftError):
    """Raised when ``partial_spec_json`` fails substrate-level
    envelope validation: not a JSON object, oversize, executable
    blob pattern, or secret-keyword pattern.

    Pin per AC #9: error messages NEVER echo matched secret
    values. The offending KEY is named; the value is not.
    """


class DraftAliasCollision(DraftError):
    """Raised when a create or update introduces an alias already
    used by another active (non-terminal) draft in the same
    instance. Distinct from ``DraftEnvelopeInvalid`` (the design review edit,
    v1 → v2) — the remediation is different (rename vs. reshape)."""


class ReadyStateMutationRequiresDemotion(DraftError):
    """Raised when ``update_draft`` mutates substantive content
    fields on a ``status='ready'`` draft without explicit
    ``status='shaping'`` demotion in the same call. Prevents
    ready-validated drafts from accumulating edits and ending up
    structurally invalid while still appearing committable
    (the design review edit, v1 → v2).

    Substantive content fields:
      - partial_spec_json
      - display_name
      - aliases
      - intent_summary
      - resolution_notes

    Non-substantive mutations (e.g. ``home_space_id``) on a ready
    draft do NOT trigger demotion.
    """


class WorkflowReferenceMissing(DraftError):
    """Raised by ``mark_committed`` when a ``workflow_registry`` was
    passed for runtime existence check and the target
    ``committed_workflow_id`` doesn't exist in it. Without
    ``workflow_registry``, ``mark_committed`` persists the soft
    reference without checking."""


__all__ = [
    "DraftAliasCollision",
    "DraftConcurrentModification",
    "DraftEnvelopeInvalid",
    "DraftError",
    "DraftNotFound",
    "DraftTerminal",
    "InvalidDraftTransition",
    "ReadyStateMutationRequiresDemotion",
    "WorkflowReferenceMissing",
]
