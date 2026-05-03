"""Tests for the audit-log integration + gate-routing bridge."""

import json

import pytest

from kernos.kernel.tool_audit import (
    NORMALIZED_TOOL_INVOCATION_EXTERNAL_SERVICE,
    NORMALIZED_TOOL_INVOCATION_INTERNAL,
    ToolInvocationAuditEntry,
    build_audit_entry,
    canonicalize_json,
    normalized_category_for,
    payload_digest,
)
from kernos.kernel.tool_descriptor import (
    GateClassification,
    parse_tool_descriptor,
)
from kernos.kernel.tool_gate_routing import (
    GATE_EFFECT_FOR_CLASSIFICATION,
    gate_effect_for,
    gate_effect_for_unclassified,
)


# ---------------------------------------------------------------------------
# Canonicalised digest
# ---------------------------------------------------------------------------


def test_canonicalize_sorts_object_keys():
    a = canonicalize_json({"b": 1, "a": 2, "c": [3, 1, 2]})
    b = canonicalize_json({"c": [3, 1, 2], "a": 2, "b": 1})
    # Different input order → identical canonical bytes.
    assert a == b
    # Lists preserve order (canonical for arrays).
    assert b'"c":[3,1,2]' in a


def test_canonicalize_no_insignificant_whitespace():
    out = canonicalize_json({"a": 1, "b": [1, 2]})
    # No spaces between separators, no leading whitespace.
    assert b" " not in out


def test_canonicalize_utf8_strings_pass_through():
    out = canonicalize_json({"name": "héllo"})
    assert "héllo".encode("utf-8") in out


def test_canonicalize_rejects_nan_and_infinity():
    """JCS does not represent NaN/Infinity. allow_nan=False makes this
    a clear error rather than a silent garbage encoding."""
    with pytest.raises(ValueError):
        canonicalize_json({"bad": float("nan")})


def test_payload_digest_is_stable_across_key_order():
    a = payload_digest({"x": "y", "a": 1, "list": [3, 2, 1]})
    b = payload_digest({"a": 1, "list": [3, 2, 1], "x": "y"})
    assert a == b
    # SHA-256 hex is 64 chars.
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)


def test_payload_digest_changes_with_value_change():
    a = payload_digest({"a": 1})
    b = payload_digest({"a": 2})
    assert a != b


# ---------------------------------------------------------------------------
# Normalized category vocabulary
# ---------------------------------------------------------------------------


def test_normalized_category_for_internal_tool():
    assert (
        normalized_category_for(service_id="")
        == NORMALIZED_TOOL_INVOCATION_INTERNAL
    )


def test_normalized_category_for_external_service_tool():
    assert (
        normalized_category_for(service_id="notion")
        == NORMALIZED_TOOL_INVOCATION_EXTERNAL_SERVICE
    )


def test_normalized_category_constants_are_dotted_strings():
    """Downstream filters should treat them as opaque tokens; sanity-
    check the shape so they don't accidentally become free-form."""
    assert NORMALIZED_TOOL_INVOCATION_INTERNAL.startswith("tool.invocation.")
    assert NORMALIZED_TOOL_INVOCATION_EXTERNAL_SERVICE.startswith("tool.invocation.")


# ---------------------------------------------------------------------------
# build_audit_entry
# ---------------------------------------------------------------------------


def _payload():
    return {"page_id": "abc-123", "include": ["title", "body"]}


def test_build_audit_entry_internal_tool():
    entry = build_audit_entry(
        timestamp="2026-04-25T22:00:00Z",
        instance_id="discord:i",
        member_id="mem_alice",
        space_id="space_a",
        tool_name="list_invoices",
        operation="",
        service_id="",
        authority=(),
        audit_category="list_invoices",
        payload={"month": "2026-04"},
        success=True,
    )
    assert entry.normalized_category == NORMALIZED_TOOL_INVOCATION_INTERNAL
    assert entry.audit_category == "list_invoices"
    assert entry.success is True
    # Digest is deterministic.
    assert entry.payload_digest == payload_digest({"month": "2026-04"})


def test_build_audit_entry_external_service_tool():
    entry = build_audit_entry(
        timestamp="2026-04-25T22:00:00Z",
        instance_id="discord:i",
        member_id="mem_alice",
        space_id="space_a",
        tool_name="notion_reader",
        operation="read_pages",
        service_id="notion",
        authority=("read_pages",),
        audit_category="notion",
        payload=_payload(),
        success=True,
    )
    assert entry.normalized_category == NORMALIZED_TOOL_INVOCATION_EXTERNAL_SERVICE
    assert entry.service_id == "notion"
    assert entry.authority == ("read_pages",)


def test_audit_entry_to_dict_is_json_serialisable():
    entry = build_audit_entry(
        timestamp="t",
        instance_id="i",
        member_id="m",
        space_id="s",
        tool_name="t",
        operation="o",
        service_id="svc",
        authority=("read", "write"),
        audit_category="cat",
        payload={"a": 1},
        success=True,
    )
    d = entry.to_dict()
    # Authority is a list (not a tuple) so json.dumps does not balk.
    assert d["authority"] == ["read", "write"]
    json.dumps(d)  # round-trip without TypeError


def test_audit_entry_failure_carries_error():
    entry = build_audit_entry(
        timestamp="t",
        instance_id="i",
        member_id="m",
        space_id="s",
        tool_name="t",
        operation="",
        service_id="",
        authority=(),
        audit_category="t",
        payload={},
        success=False,
        error="connection refused",
    )
    assert entry.success is False
    assert entry.error == "connection refused"


def test_audit_entry_does_not_carry_raw_payload():
    """Smoke check that the digest is the only payload representation:
    a sentinel value placed in the payload must not appear in the
    JSON-serialised entry."""
    entry = build_audit_entry(
        timestamp="t",
        instance_id="i",
        member_id="m",
        space_id="s",
        tool_name="t",
        operation="",
        service_id="",
        authority=(),
        audit_category="t",
        payload={"secret": "DO_NOT_LEAK_TOKEN"},
        success=True,
    )
    rendered = json.dumps(entry.to_dict())
    assert "DO_NOT_LEAK_TOKEN" not in rendered


# ---------------------------------------------------------------------------
# Gate routing bridge
# ---------------------------------------------------------------------------


def _descriptor(**overrides):
    raw = {
        "name": "tool",
        "description": "x",
        "input_schema": {"type": "object"},
        "implementation": "tool.py",
    }
    raw.update(overrides)
    return parse_tool_descriptor(raw)


def test_gate_effect_for_classification_table_complete():
    """Every GateClassification must map to a recognised gate effect."""
    for cls in GateClassification:
        assert cls in GATE_EFFECT_FOR_CLASSIFICATION
        assert GATE_EFFECT_FOR_CLASSIFICATION[cls] in {"read", "soft_write", "hard_write"}


def test_gate_effect_for_descriptor_uses_fail_closed_default():
    desc = _descriptor()
    assert gate_effect_for(desc) == "soft_write"


def test_gate_effect_for_descriptor_honours_shorthand():
    desc = _descriptor(gate_classification="read")
    assert gate_effect_for(desc) == "read"


def test_gate_effect_for_descriptor_honours_per_op():
    desc = _descriptor(
        gate_classification="soft_write",
        authority=["read_pages", "delete_pages"],
        operations=[
            {"operation": "read_pages", "classification": "read"},
            {"operation": "delete_pages", "classification": "delete"},
        ],
    )
    assert gate_effect_for(desc, "read_pages") == "read"
    # Per the design review: delete maps to hard_write in v1.
    assert gate_effect_for(desc, "delete_pages") == "hard_write"
    # An operation not in the per-op list and outside authority falls
    # back to the tool-level shorthand.
    assert gate_effect_for(desc, "uninvolved") == "soft_write"


def test_gate_effect_for_unclassified_is_fail_closed():
    """When no descriptor is available, the safety-net default fires."""
    assert gate_effect_for_unclassified() == "soft_write"


def test_gate_effect_delete_maps_to_hard_write_v1():
    """the design review's response to question 1: no destructive_irreversible in v1;
    delete maps to hard_write so the gate fires confirmation."""
    desc = _descriptor(gate_classification="delete")
    assert gate_effect_for(desc) == "hard_write"
