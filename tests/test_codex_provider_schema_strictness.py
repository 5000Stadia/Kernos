"""Codex API 400 fix (2026-05-22): _force_strict_object_schema
normalizes output_schemas to satisfy the API's strict
``additionalProperties: false`` requirement on every object level.

Pre-fix production error:
  "Codex API error (400): Invalid schema for response_format 'output':
   In context=(), 'additionalProperties' is required to be supplied
   and to be false."
"""
from __future__ import annotations

from kernos.providers.codex_provider import _force_strict_object_schema


class TestForceStrictObjectSchema:
    def test_root_object_gets_additionalProperties_false(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        normalized = _force_strict_object_schema(schema)
        assert normalized["additionalProperties"] is False

    def test_root_object_overrides_explicit_true(self):
        # The API requires FALSE specifically (not True; not absent).
        # Caller schemas that declared additionalProperties:true are
        # normalized down.
        schema = {
            "type": "object",
            "additionalProperties": True,
            "properties": {"x": {"type": "string"}},
        }
        normalized = _force_strict_object_schema(schema)
        assert normalized["additionalProperties"] is False

    def test_nested_object_in_properties_gets_normalized(self):
        schema = {
            "type": "object",
            "properties": {
                "inner": {
                    "type": "object",
                    "properties": {"y": {"type": "integer"}},
                },
            },
        }
        normalized = _force_strict_object_schema(schema)
        assert normalized["additionalProperties"] is False
        assert normalized["properties"]["inner"]["additionalProperties"] is False

    def test_array_items_object_normalized(self):
        schema = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"a": {"type": "string"}},
            },
        }
        normalized = _force_strict_object_schema(schema)
        assert normalized["items"]["additionalProperties"] is False

    def test_oneof_branches_normalized(self):
        schema = {
            "oneOf": [
                {"type": "object", "properties": {"a": {"type": "string"}}},
                {"type": "object", "properties": {"b": {"type": "integer"}}},
            ]
        }
        normalized = _force_strict_object_schema(schema)
        for branch in normalized["oneOf"]:
            assert branch["additionalProperties"] is False

    def test_defs_normalized(self):
        schema = {
            "$defs": {
                "Item": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                }
            },
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"$ref": "#/$defs/Item"}}
            },
        }
        normalized = _force_strict_object_schema(schema)
        assert normalized["additionalProperties"] is False
        assert normalized["$defs"]["Item"]["additionalProperties"] is False

    def test_non_object_types_unchanged_otherwise(self):
        schema = {"type": "string", "enum": ["a", "b"]}
        normalized = _force_strict_object_schema(schema)
        # Non-object types don't get additionalProperties tacked on
        assert "additionalProperties" not in normalized
        assert normalized["enum"] == ["a", "b"]

    def test_does_not_mutate_input(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        original = dict(schema)
        original_props = dict(schema["properties"])
        _force_strict_object_schema(schema)
        # Input intact (no additionalProperties added on the original)
        assert "additionalProperties" not in schema
        assert schema == original
        assert schema["properties"] == original_props

    def test_real_router_shape(self):
        """Smoke check against the actual router schema shape
        (kernos/kernel/router.py:84 already declares False, but
        confirm the normalizer is a no-op + doesn't break it)."""
        router_like = {
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"type": "string"}},
                "focus": {"type": "string"},
                "continuation": {"type": "boolean"},
                "query_mode": {"type": "boolean"},
                "work_mode": {"type": "boolean"},
            },
            "required": ["tags", "focus", "continuation", "query_mode", "work_mode"],
            "additionalProperties": False,
        }
        normalized = _force_strict_object_schema(router_like)
        assert normalized["additionalProperties"] is False
        assert normalized["required"] == router_like["required"]
