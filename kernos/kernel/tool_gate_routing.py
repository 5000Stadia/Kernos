"""Bridge between the workshop tool descriptor and the dispatch gate.

The dispatch gate consumes string effect tokens
("read", "soft_write", "hard_write", "unknown"). The workshop
descriptor's GateClassification enum is richer (it adds delete) and
supports per-operation overrides. This module is the small adapter
that turns a (descriptor, operation) pair into the existing gate's
effect token.

Mapping (the design review's response on question 1):

    READ        → "read"
    SOFT_WRITE  → "soft_write"
    HARD_WRITE  → "hard_write"
    DELETE      → "hard_write"   (no destructive_irreversible in v1;
                                  delete maps to hard_write so the
                                  gate fires confirmation; runtime
                                  enforcement adds the safety net)

A future spec may introduce destructive_irreversible as a separate
gate effect; the mapping changes here when that lands.
"""

from __future__ import annotations

from kernos.kernel.tool_descriptor import (
    DEFAULT_GATE_CLASSIFICATION,
    GateClassification,
    ToolDescriptor,
)


# Mapping table. Kept as a module-level constant so the gate can
# import it directly when wiring the workshop path in C5.
GATE_EFFECT_FOR_CLASSIFICATION: dict[GateClassification, str] = {
    GateClassification.READ: "read",
    GateClassification.SOFT_WRITE: "soft_write",
    GateClassification.HARD_WRITE: "hard_write",
    # Per the design review's response to question 1: delete maps to hard_write in v1.
    # Promoting to a separate destructive_irreversible category is a
    # future spec; this mapping changes when that lands.
    GateClassification.DELETE: "hard_write",
    # EXTERNAL-AGENT-CONSULTATION v1 (additive): external_agent_read
    # routes through the gate as a read-class operation for blast-
    # radius purposes, but the agent surface (consult tool) carries
    # a separate authority that the dispatch gate enforces before
    # the effect-token lookup runs.
    GateClassification.EXTERNAL_AGENT_READ: "read",
}


def gate_effect_for(
    descriptor: ToolDescriptor,
    operation: str | None = None,
) -> str:
    """Return the gate's effect token for a (descriptor, operation) pair.

    Resolution mirrors ToolDescriptor.classification_for: per-operation
    classification wins when present, otherwise the tool-level
    shorthand, otherwise the fail-closed default (soft_write).
    """
    classification = descriptor.classification_for(operation)
    return GATE_EFFECT_FOR_CLASSIFICATION[classification]


def gate_effect_for_unclassified() -> str:
    """Effect token used when no descriptor is available.

    Equivalent to applying the fail-closed default. Used by the gate
    as a safety net when a workshop tool's descriptor is somehow
    missing at routing time (e.g., the catalog entry exists but the
    descriptor failed to load).
    """
    return GATE_EFFECT_FOR_CLASSIFICATION[DEFAULT_GATE_CLASSIFICATION]
