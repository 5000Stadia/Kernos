"""Canonical cognitive-context primitive (COGNITIVE-CONTEXT-V1).

The decoupled-cognition migration silently dropped 17 of 19 cognitive-
substrate content classes between assembly and the model. This package
introduces a typed canonical primitive that flows through the
assembly → integration → presence pipeline with deterministic fields,
preserving the legacy seven-block grammar while leaving the decoupled
architecture's freedom on *who* computes/mediates context intact.

The load-bearing invariant (Kit, 2026-05-01):

    Decoupling may change *who* computes/mediates context, but it
    may not silently change *what* cognitive substrate reaches the
    model.

C1 (this commit) ships the type + field-provenance map only — no
wiring; no consumer reads the packet yet. C2 lands the 14 contract
tests as red bars. C3a-c wires the packet through the
reasoning → integration → presence chain. C4 registers the missing
cohorts (memory, gardener) and wires their outputs into the packet.
C5 ships the thin-path tool surface. C6 extends the equivalence
suite with content/tool-surface/context-zone fidelity dimensions.
C7 cuts production over with legacy retained as oracle behind the
existing ``KERNOS_USE_DECOUPLED_TURN_RUNNER`` flag.
"""
from __future__ import annotations

from kernos.kernel.cognitive_context.field_provenance import (
    FIELD_PROVENANCE,
    FieldProvenance,
    PopulationContext,
    populate_field,
    populate_packet,
)
from kernos.kernel.cognitive_context.types import (
    ActionsBlock,
    CognitiveContext,
    ConversationBlock,
    MemoryBlock,
    NowBlock,
    ResultsBlock,
    RulesBlock,
    SafetyConstraints,
    StateBlock,
    ToolSurface,
)


__all__ = [
    "ActionsBlock",
    "CognitiveContext",
    "ConversationBlock",
    "FIELD_PROVENANCE",
    "FieldProvenance",
    "MemoryBlock",
    "NowBlock",
    "PopulationContext",
    "ResultsBlock",
    "RulesBlock",
    "SafetyConstraints",
    "StateBlock",
    "ToolSurface",
    "populate_field",
    "populate_packet",
]
