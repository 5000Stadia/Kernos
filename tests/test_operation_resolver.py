"""Tests for the operation_resolver pattern (PDI the design review edit).

Resolution rules per spec:
  1. Explicit operation_name in the call → use it.
  2. Else if the descriptor has operation_resolver → call resolver(args).
  3. Else if operations map has exactly one entry → use that.
  4. Else ambiguous → conservative sensitive_action; tool NEVER
     surfaced to integration's catalog.

Also covers:
  - Resolver exception → ambiguous (with logged warning).
  - Resolver returning empty/non-string → ambiguous.
  - safety_for() derivation from GateClassification when no explicit
    safety override is set.
  - is_surfacable_to_integration filter (read_only only; ambiguous
    NEVER surfaced).
"""

from __future__ import annotations

import pytest

from kernos.kernel.tool_descriptor import (
    DEFAULT_AMBIGUOUS_SAFETY,
    GateClassification,
    OperationClassification,
    OperationSafety,
    SAFETY_FOR_GATE,
    ToolDescriptor,
)
from kernos.kernel.tools.operation_resolver import (
    OperationResolution,
    is_surfacable_to_integration,
    resolve_operation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _descriptor(
    *,
    name: str = "manage_widgets",
    operations: tuple[OperationClassification, ...] = (),
    operation_resolver=None,
    gate_classification=None,
):
    return ToolDescriptor(
        name=name,
        description="d",
        input_schema={"type": "object"},
        implementation="x.py",
        operations=operations,
        operation_resolver=operation_resolver,
        gate_classification=gate_classification,
    )


# ---------------------------------------------------------------------------
# Rule 1: explicit operation_name wins
# ---------------------------------------------------------------------------


def test_explicit_operation_resolves_directly():
    desc = _descriptor(
        operations=(
            OperationClassification(
                operation="read", classification=GateClassification.READ
            ),
            OperationClassification(
                operation="write", classification=GateClassification.HARD_WRITE
            ),
        ),
    )
    resolution = resolve_operation(desc, explicit_operation="read")
    assert resolution.operation_name == "read"
    assert resolution.safety is OperationSafety.READ_ONLY
    assert resolution.ambiguous is False
    assert resolution.reason == "explicit"


def test_explicit_operation_uses_safety_override_when_present():
    desc = _descriptor(
        operations=(
            OperationClassification(
                operation="cross_member_read",
                classification=GateClassification.READ,
                safety=OperationSafety.SENSITIVE_ACTION,
            ),
        ),
    )
    resolution = resolve_operation(
        desc, explicit_operation="cross_member_read"
    )
    assert resolution.safety is OperationSafety.SENSITIVE_ACTION


# ---------------------------------------------------------------------------
# Rule 2: operation_resolver derives from args
# ---------------------------------------------------------------------------


def test_resolver_derives_operation_from_args():
    def _resolve(args):
        return "read" if args.get("mode") == "read" else "write"

    desc = _descriptor(
        operations=(
            OperationClassification(
                operation="read", classification=GateClassification.READ
            ),
            OperationClassification(
                operation="write", classification=GateClassification.HARD_WRITE
            ),
        ),
        operation_resolver=_resolve,
    )

    read_resolution = resolve_operation(desc, arguments={"mode": "read"})
    assert read_resolution.operation_name == "read"
    assert read_resolution.safety is OperationSafety.READ_ONLY
    assert read_resolution.reason == "resolver"

    write_resolution = resolve_operation(desc, arguments={"mode": "write"})
    assert write_resolution.operation_name == "write"
    assert write_resolution.safety is OperationSafety.MUTATING


def test_resolver_exception_falls_back_to_ambiguous():
    def _resolve(args):
        return args["missing_key"]  # KeyError

    desc = _descriptor(
        operations=(
            OperationClassification(
                operation="read", classification=GateClassification.READ
            ),
            OperationClassification(
                operation="write", classification=GateClassification.HARD_WRITE
            ),
        ),
        operation_resolver=_resolve,
    )
    resolution = resolve_operation(desc, arguments={})
    assert resolution.ambiguous is True
    assert resolution.operation_name is None
    assert resolution.safety is DEFAULT_AMBIGUOUS_SAFETY
    assert resolution.reason == "ambiguous"


def test_resolver_returning_empty_string_falls_back_to_ambiguous():
    def _resolve(args):
        return ""

    desc = _descriptor(
        operations=(
            OperationClassification(
                operation="read", classification=GateClassification.READ
            ),
        ),
        operation_resolver=_resolve,
    )
    resolution = resolve_operation(desc, arguments={})
    assert resolution.ambiguous is True
    assert resolution.safety is DEFAULT_AMBIGUOUS_SAFETY


def test_resolver_returning_non_string_falls_back_to_ambiguous():
    def _resolve(args):
        return 42

    desc = _descriptor(
        operations=(
            OperationClassification(
                operation="read", classification=GateClassification.READ
            ),
        ),
        operation_resolver=_resolve,
    )
    resolution = resolve_operation(desc, arguments={})
    assert resolution.ambiguous is True


# ---------------------------------------------------------------------------
# Rule 3: single-entry operations map auto-resolves
# ---------------------------------------------------------------------------


def test_single_entry_operations_auto_resolves():
    desc = _descriptor(
        operations=(
            OperationClassification(
                operation="list",
                classification=GateClassification.READ,
            ),
        ),
    )
    resolution = resolve_operation(desc)
    assert resolution.operation_name == "list"
    assert resolution.safety is OperationSafety.READ_ONLY
    assert resolution.ambiguous is False
    assert resolution.reason == "single_entry"


# ---------------------------------------------------------------------------
# Rule 4: ambiguous fallback
# ---------------------------------------------------------------------------


def test_multi_entry_no_resolver_no_explicit_is_ambiguous():
    desc = _descriptor(
        operations=(
            OperationClassification(
                operation="read", classification=GateClassification.READ
            ),
            OperationClassification(
                operation="write", classification=GateClassification.HARD_WRITE
            ),
        ),
    )
    resolution = resolve_operation(desc)
    assert resolution.ambiguous is True
    assert resolution.operation_name is None
    assert resolution.safety is DEFAULT_AMBIGUOUS_SAFETY


def test_no_operations_no_resolver_is_ambiguous():
    """A tool with no operations declared and no resolver — there is
    no way to derive the operation. Conservative fallback."""
    desc = _descriptor()
    resolution = resolve_operation(desc)
    assert resolution.ambiguous is True
    assert resolution.safety is DEFAULT_AMBIGUOUS_SAFETY


# ---------------------------------------------------------------------------
# safety_for derivation rules
# ---------------------------------------------------------------------------


def test_safety_for_derives_from_gate_classification():
    desc = _descriptor(
        operations=(
            OperationClassification(
                operation="r", classification=GateClassification.READ
            ),
            OperationClassification(
                operation="sw", classification=GateClassification.SOFT_WRITE
            ),
            OperationClassification(
                operation="hw", classification=GateClassification.HARD_WRITE
            ),
            OperationClassification(
                operation="del", classification=GateClassification.DELETE
            ),
        ),
    )
    assert desc.safety_for("r") is OperationSafety.READ_ONLY
    assert desc.safety_for("sw") is OperationSafety.MUTATING
    assert desc.safety_for("hw") is OperationSafety.MUTATING
    assert desc.safety_for("del") is OperationSafety.SENSITIVE_ACTION


def test_safety_for_uses_explicit_safety_override():
    desc = _descriptor(
        operations=(
            OperationClassification(
                operation="cross_member_read",
                classification=GateClassification.READ,
                safety=OperationSafety.SENSITIVE_ACTION,
            ),
        ),
    )
    # Override beats the default-from-classification.
    assert desc.safety_for("cross_member_read") is OperationSafety.SENSITIVE_ACTION


def test_safety_for_falls_back_to_tool_level_gate_classification():
    desc = _descriptor(gate_classification=GateClassification.READ)
    assert desc.safety_for("any_op_not_declared") is OperationSafety.READ_ONLY


def test_safety_for_unclassified_tool_is_sensitive_action():
    """Tool with no gate_classification, no operations — safety_for
    falls back to the conservative sensitive_action so callers do
    not accidentally treat unclassified tools as read-only."""
    desc = _descriptor()
    assert desc.safety_for("anything") is DEFAULT_AMBIGUOUS_SAFETY
    assert desc.safety_for(None) is DEFAULT_AMBIGUOUS_SAFETY


# ---------------------------------------------------------------------------
# Integration catalog filter
# ---------------------------------------------------------------------------


def test_read_only_operation_is_surfacable():
    desc = _descriptor(
        operations=(
            OperationClassification(
                operation="list", classification=GateClassification.READ
            ),
        ),
    )
    assert is_surfacable_to_integration(desc) is True


def test_mutating_operation_is_not_surfacable():
    desc = _descriptor(
        operations=(
            OperationClassification(
                operation="update", classification=GateClassification.HARD_WRITE
            ),
        ),
    )
    assert is_surfacable_to_integration(desc) is False


def test_ambiguous_resolution_is_not_surfacable():
    """Per spec: ambiguous classification is NEVER surfaced to
    integration's catalog. Even though the conservative fallback
    is sensitive_action, the catalog filter rejects ambiguity
    structurally."""
    desc = _descriptor(
        operations=(
            OperationClassification(
                operation="read", classification=GateClassification.READ
            ),
            OperationClassification(
                operation="write", classification=GateClassification.HARD_WRITE
            ),
        ),
    )
    # No explicit operation, no resolver, multi-entry → ambiguous.
    assert is_surfacable_to_integration(desc) is False


def test_resolver_picking_read_is_surfacable():
    """When a resolver picks a read_only operation deterministically
    from the args, the call is surfacable. Mirrors manage_covenants
    with mode='read'."""
    def _resolve(args):
        return "read" if args.get("mode") == "read" else "write"

    desc = _descriptor(
        operations=(
            OperationClassification(
                operation="read", classification=GateClassification.READ
            ),
            OperationClassification(
                operation="write", classification=GateClassification.HARD_WRITE
            ),
        ),
        operation_resolver=_resolve,
    )
    assert is_surfacable_to_integration(
        desc, arguments={"mode": "read"}
    ) is True
    assert is_surfacable_to_integration(
        desc, arguments={"mode": "write"}
    ) is False


# ---------------------------------------------------------------------------
# OperationClassification additive fields
# ---------------------------------------------------------------------------


def test_operation_classification_default_safety_is_none():
    op = OperationClassification(
        operation="x", classification=GateClassification.READ
    )
    assert op.safety is None
    assert op.timeout_ms == 0
    assert op.effective_safety is OperationSafety.READ_ONLY


def test_operation_classification_carries_timeout_ms():
    op = OperationClassification(
        operation="x",
        classification=GateClassification.READ,
        timeout_ms=2500,
    )
    assert op.timeout_ms == 2500


# ---------------------------------------------------------------------------
# Descriptor accessors
# ---------------------------------------------------------------------------


def test_operation_for_returns_match_or_none():
    desc = _descriptor(
        operations=(
            OperationClassification(
                operation="list", classification=GateClassification.READ
            ),
            OperationClassification(
                operation="update",
                classification=GateClassification.HARD_WRITE,
            ),
        ),
    )
    assert desc.operation_for("list") is not None
    assert desc.operation_for("nonexistent") is None


def test_operations_map_returns_name_keyed_view():
    desc = _descriptor(
        operations=(
            OperationClassification(
                operation="list", classification=GateClassification.READ
            ),
            OperationClassification(
                operation="update",
                classification=GateClassification.HARD_WRITE,
            ),
        ),
    )
    m = desc.operations_map()
    assert set(m.keys()) == {"list", "update"}
    assert m["list"].classification is GateClassification.READ


# ---------------------------------------------------------------------------
# SAFETY_FOR_GATE table coverage
# ---------------------------------------------------------------------------


def test_safety_for_gate_table_covers_every_classification():
    for cls in GateClassification:
        assert cls in SAFETY_FOR_GATE
