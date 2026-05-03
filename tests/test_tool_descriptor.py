"""Tests for the extended tool descriptor model.

Covers:
- Original four required fields preserved (back-compat with existing
  workshop tools).
- Per-operation classification (the design review edit 1).
- Tool-level shorthand fallback.
- Fail-closed default (soft_write) when nothing declared.
- Service-id cross-validation against the registry.
- aggregation cross_member reserved-but-rejected with clear pointer.
- Domain hints, audit category defaulting.
"""

import pytest

from kernos.kernel.services import (
    ServiceRegistry,
    parse_service_descriptor,
)
from kernos.kernel.tool_descriptor import (
    Aggregation,
    CrossMemberAggregationReservedError,
    DEFAULT_GATE_CLASSIFICATION,
    GateClassification,
    OperationClassification,
    ToolDescriptor,
    ToolDescriptorError,
    parse_tool_descriptor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_minimal():
    """Minimal descriptor with just the original required fields."""
    return {
        "name": "list_invoices",
        "description": "List invoices in the workspace",
        "input_schema": {"type": "object", "properties": {}},
        "implementation": "list_invoices.py",
    }


def _valid_service_bound():
    """Descriptor declaring a service binding."""
    return {
        "name": "notion_reader",
        "description": "Read pages from a Notion workspace",
        "input_schema": {"type": "object", "properties": {}},
        "implementation": "notion_reader.py",
        "service_id": "notion",
        "authority": ["read_pages"],
        "operations": [
            {"operation": "read_pages", "classification": "read"},
        ],
        "audit_category": "notion",
    }


def _service_registry_with_notion():
    registry = ServiceRegistry()
    registry.register(parse_service_descriptor({
        "service_id": "notion",
        "display_name": "Notion",
        "auth_type": "api_token",
        "operations": ["read_pages", "write_pages", "delete_pages"],
    }))
    return registry


# ---------------------------------------------------------------------------
# Original fields preservation
# ---------------------------------------------------------------------------


def test_minimal_descriptor_parses_back_compat():
    desc = parse_tool_descriptor(_valid_minimal())
    assert desc.name == "list_invoices"
    assert desc.implementation == "list_invoices.py"
    # New fields default to safe values.
    assert desc.service_id == ""
    assert desc.authority == ()
    assert desc.gate_classification is None
    assert desc.aggregation == Aggregation.PER_MEMBER


def test_descriptor_rejects_invalid_name():
    raw = _valid_minimal()
    raw["name"] = "BadName"
    with pytest.raises(ToolDescriptorError, match="snake_case"):
        parse_tool_descriptor(raw)


def test_descriptor_rejects_implementation_with_path_traversal():
    raw = _valid_minimal()
    raw["implementation"] = "../escape.py"
    with pytest.raises(ToolDescriptorError, match="path separators"):
        parse_tool_descriptor(raw)


def test_descriptor_rejects_implementation_not_python():
    raw = _valid_minimal()
    raw["implementation"] = "tool.sh"
    with pytest.raises(ToolDescriptorError, match=".py"):
        parse_tool_descriptor(raw)


# ---------------------------------------------------------------------------
# Fail-closed default
# ---------------------------------------------------------------------------


def test_classification_defaults_to_soft_write_when_nothing_declared():
    desc = parse_tool_descriptor(_valid_minimal())
    assert desc.classification_for(None) == DEFAULT_GATE_CLASSIFICATION
    assert desc.classification_for(None) == GateClassification.SOFT_WRITE


def test_default_is_fail_closed_not_read():
    """Per design review's revision 1: missing classification fails closed,
    not open. Previously the implicit default was read; now it's soft_write."""
    desc = parse_tool_descriptor(_valid_minimal())
    assert desc.classification_for(None) != GateClassification.READ


# ---------------------------------------------------------------------------
# Tool-level shorthand
# ---------------------------------------------------------------------------


def test_tool_level_classification_shorthand_applies_when_no_per_op():
    raw = _valid_minimal()
    raw["gate_classification"] = "read"
    desc = parse_tool_descriptor(raw)
    assert desc.classification_for(None) == GateClassification.READ
    # Even with an operation name, shorthand applies when no per-op
    # classification overrides it.
    assert desc.classification_for("anything") == GateClassification.READ


def test_invalid_classification_raises():
    raw = _valid_minimal()
    raw["gate_classification"] = "transmute"
    with pytest.raises(ToolDescriptorError, match="gate_classification"):
        parse_tool_descriptor(raw)


# ---------------------------------------------------------------------------
# Per-operation classification (the design review edit 1)
# ---------------------------------------------------------------------------


def test_per_operation_classification_overrides_tool_shorthand():
    """the design review edit 1: per-operation overrides at gate-routing time when
    both are present."""
    raw = _valid_minimal()
    raw["gate_classification"] = "soft_write"  # tool-level shorthand
    raw["authority"] = ["read_pages", "write_pages"]
    raw["operations"] = [
        {"operation": "read_pages", "classification": "read"},
        {"operation": "write_pages", "classification": "hard_write"},
    ]
    desc = parse_tool_descriptor(raw)
    assert desc.classification_for("read_pages") == GateClassification.READ
    assert desc.classification_for("write_pages") == GateClassification.HARD_WRITE
    # Operations not classified per-op fall back to the tool-level shorthand.
    raw["authority"].append("delete_pages")
    desc2 = parse_tool_descriptor(raw)
    assert desc2.classification_for("delete_pages") == GateClassification.SOFT_WRITE


def test_per_operation_classification_fail_closed_when_no_shorthand():
    """An operation not in the per-op list and no tool-level shorthand
    falls back to the fail-closed default."""
    raw = _valid_minimal()
    raw["authority"] = ["read_pages", "write_pages"]
    raw["operations"] = [
        {"operation": "read_pages", "classification": "read"},
    ]
    desc = parse_tool_descriptor(raw)
    # write_pages has no per-op classification and no tool-level shorthand.
    assert desc.classification_for("write_pages") == GateClassification.SOFT_WRITE


def test_per_operation_duplicate_rejected():
    raw = _valid_minimal()
    raw["authority"] = ["read_pages"]
    raw["operations"] = [
        {"operation": "read_pages", "classification": "read"},
        {"operation": "read_pages", "classification": "soft_write"},
    ]
    with pytest.raises(ToolDescriptorError, match="more than once"):
        parse_tool_descriptor(raw)


def test_per_operation_unknown_classification_rejected():
    raw = _valid_minimal()
    raw["authority"] = ["read_pages"]
    raw["operations"] = [
        {"operation": "read_pages", "classification": "transmute"},
    ]
    with pytest.raises(ToolDescriptorError, match="gate_classification"):
        parse_tool_descriptor(raw)


def test_per_operation_must_be_in_authority():
    """An operation classified per-op but not declared in authority is a smell."""
    raw = _valid_minimal()
    raw["authority"] = ["read_pages"]
    raw["operations"] = [
        {"operation": "delete_pages", "classification": "delete"},
    ]
    with pytest.raises(ToolDescriptorError, match="authority"):
        parse_tool_descriptor(raw)


# ---------------------------------------------------------------------------
# Service-id cross-validation
# ---------------------------------------------------------------------------


def test_service_id_unknown_raises():
    raw = _valid_service_bound()
    raw["service_id"] = "phantom"
    registry = _service_registry_with_notion()
    with pytest.raises(ToolDescriptorError, match="not registered"):
        parse_tool_descriptor(raw, service_lookup=registry.get)


def test_authority_must_be_subset_of_service_operations():
    raw = _valid_service_bound()
    raw["authority"] = ["read_pages", "publish_to_facebook"]  # not a Notion op
    registry = _service_registry_with_notion()
    with pytest.raises(ToolDescriptorError, match="not in the declared"):
        parse_tool_descriptor(raw, service_lookup=registry.get)


def test_service_bound_descriptor_passes_with_lookup():
    raw = _valid_service_bound()
    registry = _service_registry_with_notion()
    desc = parse_tool_descriptor(raw, service_lookup=registry.get)
    assert desc.service_id == "notion"
    assert desc.is_service_bound is True
    assert desc.classification_for("read_pages") == GateClassification.READ


def test_per_operation_classification_must_match_service_operation():
    raw = _valid_service_bound()
    raw["operations"] = [
        {"operation": "publish_to_facebook", "classification": "hard_write"},
    ]
    raw["authority"] = ["publish_to_facebook"]
    registry = _service_registry_with_notion()
    with pytest.raises(ToolDescriptorError):
        parse_tool_descriptor(raw, service_lookup=registry.get)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_aggregation_defaults_to_per_member():
    desc = parse_tool_descriptor(_valid_minimal())
    assert desc.aggregation == Aggregation.PER_MEMBER


def test_aggregation_per_member_explicit_accepted():
    raw = _valid_minimal()
    raw["aggregation"] = "per_member"
    desc = parse_tool_descriptor(raw)
    assert desc.aggregation == Aggregation.PER_MEMBER


def test_aggregation_cross_member_rejected_with_pointer():
    """Per the design review's call: cross_member reserved-but-rejected in v1.
    Error message points at the future spec."""
    raw = _valid_minimal()
    raw["aggregation"] = "cross_member"
    with pytest.raises(CrossMemberAggregationReservedError) as excinfo:
        parse_tool_descriptor(raw)
    msg = str(excinfo.value)
    assert "WORKSHOP-CROSS-MEMBER-AGGREGATION" in msg
    assert "v1" in msg


def test_aggregation_unknown_value_rejected():
    raw = _valid_minimal()
    raw["aggregation"] = "neighborhood_shared"
    with pytest.raises(ToolDescriptorError, match="aggregation"):
        parse_tool_descriptor(raw)


# ---------------------------------------------------------------------------
# Domain hints + audit category
# ---------------------------------------------------------------------------


def test_domain_hints_parsed():
    raw = _valid_minimal()
    raw["domain_hints"] = ["finance", "invoices"]
    desc = parse_tool_descriptor(raw)
    assert desc.domain_hints == ("finance", "invoices")


def test_domain_hints_must_be_list_of_strings():
    raw = _valid_minimal()
    raw["domain_hints"] = ["ok", 42]
    with pytest.raises(ToolDescriptorError, match="domain_hint"):
        parse_tool_descriptor(raw)


def test_audit_category_defaults_to_tool_name_for_standalone():
    desc = parse_tool_descriptor(_valid_minimal())
    assert desc.audit_category == "list_invoices"


def test_audit_category_left_blank_for_service_bound_when_unspecified():
    raw = _valid_service_bound()
    raw.pop("audit_category")
    registry = _service_registry_with_notion()
    desc = parse_tool_descriptor(raw, service_lookup=registry.get)
    # Blank means the runtime resolves it from the service.
    assert desc.audit_category == ""


def test_audit_category_explicit_preserved():
    raw = _valid_minimal()
    raw["audit_category"] = "billing.invoices"
    desc = parse_tool_descriptor(raw)
    assert desc.audit_category == "billing.invoices"
