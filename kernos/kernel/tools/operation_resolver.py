"""Per-operation safety resolution at dispatch time (PDI the design review edit).

Many tools have a single tool name with multiple operations selected
by argument values. `manage_covenants` is the canonical example: same
tool, different operations depending on `mode`. The runtime cannot
classify the call's safety from the tool descriptor alone — it needs
to look at the args.

This module implements the resolution rules from the PDI spec:

  1. If `explicit_operation` is provided, use it.
  2. Else if the descriptor has `operation_resolver`, call
     `resolver(args)` to derive the operation name.
  3. Else if the descriptor's `operations` map has exactly one entry,
     use that.
  4. Else the operation is ambiguous → conservative
     `OperationSafety.SENSITIVE_ACTION`. Tool is NEVER surfaced to
     integration's catalog when ambiguous.

The runtime also turns the resolved operation_name into an
`OperationSafety` via the descriptor's `safety_for(operation)` helper,
applying the SAFETY_FOR_GATE derivation when no explicit safety
override is set on the per-operation classification.

Resolver exception handling: if a tool's `operation_resolver` raises
(e.g., args missing a required key), the resolver is treated as
ambiguous-by-construction. The conservative fallback applies; a
warning is logged so the tool author can fix the resolver. Surfacing
the exception would make tool authoring brittle for the (common)
case where args may legitimately omit a discriminator field.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from kernos.kernel.tool_descriptor import (
    DEFAULT_AMBIGUOUS_SAFETY,
    OperationSafety,
    ToolDescriptor,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OperationResolution:
    """The outcome of resolving an operation against a descriptor.

    `operation_name` is the resolved name when the descriptor knew
    enough to pick one (rules 1-3). It is None when the resolution
    was ambiguous (rule 4); callers must not surface an ambiguous
    operation to integration's catalog.

    `safety` is always populated. Ambiguous resolutions carry
    `DEFAULT_AMBIGUOUS_SAFETY` (sensitive_action) so the dispatch
    routing remains conservative even when the name is unknown.

    `ambiguous` is True only for rule 4. The audit trail uses this
    flag to record that the tool was rejected from integration's
    catalog due to ambiguity.

    `reason` is an operator-readable label describing which rule
    fired (`explicit`, `resolver`, `single_entry`, `ambiguous`).
    """

    operation_name: str | None
    safety: OperationSafety
    ambiguous: bool
    reason: str


def resolve_operation(
    descriptor: ToolDescriptor,
    *,
    explicit_operation: str | None = None,
    arguments: Mapping[str, Any] | None = None,
) -> OperationResolution:
    """Resolve an operation against a tool descriptor.

    See module docstring for the resolution rules. The function never
    raises for ambiguous classifications; ambiguity is signalled
    via `OperationResolution.ambiguous` so callers can surface it
    cleanly (e.g., audit trail, catalog filter).
    """
    args = dict(arguments or {})

    # Rule 1: explicit operation_name in the call wins.
    if explicit_operation:
        safety = descriptor.safety_for(explicit_operation)
        return OperationResolution(
            operation_name=explicit_operation,
            safety=safety,
            ambiguous=False,
            reason="explicit",
        )

    # Rule 2: descriptor's operation_resolver derives from args.
    if descriptor.operation_resolver is not None:
        try:
            resolved = descriptor.operation_resolver(args)
        except Exception:
            logger.warning(
                "operation_resolver raised for tool %r; falling back to "
                "ambiguous (sensitive_action). Tool authors: ensure the "
                "resolver handles missing keys.",
                descriptor.name,
                exc_info=True,
            )
            return OperationResolution(
                operation_name=None,
                safety=DEFAULT_AMBIGUOUS_SAFETY,
                ambiguous=True,
                reason="ambiguous",
            )

        if not isinstance(resolved, str) or not resolved.strip():
            logger.warning(
                "operation_resolver for tool %r returned a non-string or "
                "empty value (%r); falling back to ambiguous.",
                descriptor.name,
                resolved,
            )
            return OperationResolution(
                operation_name=None,
                safety=DEFAULT_AMBIGUOUS_SAFETY,
                ambiguous=True,
                reason="ambiguous",
            )

        safety = descriptor.safety_for(resolved)
        return OperationResolution(
            operation_name=resolved,
            safety=safety,
            ambiguous=False,
            reason="resolver",
        )

    # Rule 3: single-entry operations map → use that.
    if len(descriptor.operations) == 1:
        only = descriptor.operations[0]
        return OperationResolution(
            operation_name=only.operation,
            safety=only.effective_safety,
            ambiguous=False,
            reason="single_entry",
        )

    # Rule 4: ambiguous. Multi-entry operations map without a resolver,
    # or no operations declared at all and no explicit operation. Tool
    # MUST NOT be surfaced to integration's catalog.
    return OperationResolution(
        operation_name=None,
        safety=DEFAULT_AMBIGUOUS_SAFETY,
        ambiguous=True,
        reason="ambiguous",
    )


def is_surfacable_to_integration(
    descriptor: ToolDescriptor,
    *,
    explicit_operation: str | None = None,
    arguments: Mapping[str, Any] | None = None,
) -> bool:
    """True when the operation resolves cleanly AND its safety is
    `read_only`. Per the spec: integration's catalog only sees
    read_only operations; ambiguous classifications are NEVER
    surfaced.
    """
    resolution = resolve_operation(
        descriptor,
        explicit_operation=explicit_operation,
        arguments=arguments,
    )
    if resolution.ambiguous:
        return False
    return resolution.safety is OperationSafety.READ_ONLY


__all__ = [
    "OperationResolution",
    "is_surfacable_to_integration",
    "resolve_operation",
]
