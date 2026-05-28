"""BRIDGE-RESPONSE-SCHEMA-V1 (2026-05-28): tests for structured-
field lift from CC's trailing JSON code block.

The bridge_watcher's _merge_structured_fields_from_response parses
a ```json {...}``` fenced block at the end of CC's text response
and merges allowed structured fields into the response_payload as
top-level keys.

Coverage:
- Happy path: well-formed JSON block → fields lifted
- No block / malformed JSON: silent no-op (fall back to summary)
- Allowlist enforced: unknown fields dropped
- Reserved fields rejected: CC can't inject request_id etc.
- Trailing whitespace tolerated after fence
- Multiple code blocks: only the trailing one extracted
"""
from __future__ import annotations

from kernos.kernel.external_agents.bridge_watcher import (
    _BRIDGE_RESERVED_FIELDS,
    _BRIDGE_STRUCTURED_FIELD_ALLOWLIST,
    _extract_trailing_json_block,
    _merge_structured_fields_from_response,
)


# ---------------------------------------------------------------------
# _extract_trailing_json_block
# ---------------------------------------------------------------------


def test_extract_basic_json_block():
    text = (
        "Investigation prose here.\n\n"
        "```json\n"
        '{"failure_mode": "x", "touches_paths": ["a.py"]}\n'
        "```\n"
    )
    result = _extract_trailing_json_block(text)
    assert result == {
        "failure_mode": "x",
        "touches_paths": ["a.py"],
    }


def test_extract_no_block_returns_none():
    assert _extract_trailing_json_block(
        "Just plain prose, no code block.",
    ) is None


def test_extract_empty_text_returns_none():
    assert _extract_trailing_json_block("") is None
    assert _extract_trailing_json_block(None) is None


def test_extract_malformed_json_returns_none():
    text = (
        "Prose\n```json\n{malformed: not json}\n```\n"
    )
    assert _extract_trailing_json_block(text) is None


def test_extract_non_object_json_returns_none():
    """Reject arrays / scalars at top level. The bridge expects a
    dict it can merge into the response_payload."""
    text = 'Prose\n```json\n["array", "not", "object"]\n```\n'
    assert _extract_trailing_json_block(text) is None


def test_extract_tolerates_trailing_whitespace():
    text = (
        "Prose\n```json\n"
        '{"failure_mode": "x"}\n```\n\n\n   \n'
    )
    assert _extract_trailing_json_block(text) == {
        "failure_mode": "x",
    }


def test_extract_without_json_language_tag():
    """``` without ``json`` still works."""
    text = 'Prose\n```\n{"failure_mode": "x"}\n```\n'
    assert _extract_trailing_json_block(text) == {
        "failure_mode": "x",
    }


def test_extract_picks_trailing_block_when_multiple():
    """Several code blocks in response; only the trailing one is
    treated as the structured-fields carrier."""
    text = (
        "First example:\n"
        "```json\n"
        '{"failure_mode": "not_this_one"}\n'
        "```\n"
        "Body text.\n\n"
        "```json\n"
        '{"failure_mode": "actual_value"}\n'
        "```\n"
    )
    result = _extract_trailing_json_block(text)
    assert result == {"failure_mode": "actual_value"}


# ---------------------------------------------------------------------
# _merge_structured_fields_from_response
# ---------------------------------------------------------------------


def test_merge_lifts_allowed_fields():
    payload = {
        "request_id": "req1",
        "investigation_outcome": "completed",
        "summary": "...",
    }
    text = (
        "Prose\n```json\n"
        '{"failure_mode": "got it", "touches_paths": ["a.py"], '
        '"proposed_fix_diff": "diff body"}\n'
        "```\n"
    )
    _merge_structured_fields_from_response(payload, text)
    assert payload["failure_mode"] == "got it"
    assert payload["touches_paths"] == ["a.py"]
    assert payload["proposed_fix_diff"] == "diff body"
    # Pre-existing fields preserved.
    assert payload["request_id"] == "req1"


def test_merge_no_block_is_noop():
    payload = {"request_id": "x", "summary": "y"}
    _merge_structured_fields_from_response(payload, "no block")
    assert payload == {"request_id": "x", "summary": "y"}


def test_merge_reserved_fields_dropped():
    """CC can't inject request_id/timestamp/etc. via the JSON
    block. The bridge owns those."""
    payload = {"request_id": "real_req_id", "summary": "y"}
    text = (
        "Prose\n```json\n"
        '{"request_id": "FAKE_INJECTION", '
        '"summary": "FAKE_SUMMARY", '
        '"failure_mode": "legit"}\n'
        "```\n"
    )
    _merge_structured_fields_from_response(payload, text)
    # Reserved fields untouched.
    assert payload["request_id"] == "real_req_id"
    assert payload["summary"] == "y"
    # Allowed field lifted.
    assert payload["failure_mode"] == "legit"


def test_merge_unknown_fields_dropped():
    """Forward-compat: CC may emit fields the bridge doesn't know
    about. Drop silently rather than failing or polluting payload."""
    payload = {"summary": "y"}
    text = (
        "Prose\n```json\n"
        '{"failure_mode": "ok", '
        '"some_future_field_kernos_does_not_know": "ignored"}\n'
        "```\n"
    )
    _merge_structured_fields_from_response(payload, text)
    assert payload["failure_mode"] == "ok"
    assert "some_future_field_kernos_does_not_know" not in payload


def test_merge_malformed_json_is_noop():
    payload = {"summary": "y"}
    text = "Prose\n```json\n{malformed\n```\n"
    _merge_structured_fields_from_response(payload, text)
    assert payload == {"summary": "y"}


def test_allowlist_includes_all_uii_workflow_refs():
    """USER-INITIATED-IMPROVEMENT-TRIGGER-V1 workflow YAML refs
    these structured fields. If any are missing from the allowlist,
    the workflow's ref resolution would silently drop them."""
    uii_refs = {
        "failure_mode",
        "proposed_fix_summary",
        "proposed_fix_diff",
        "touches_paths",
        "external_action",
        "related_pattern_id",
    }
    missing = uii_refs - _BRIDGE_STRUCTURED_FIELD_ALLOWLIST
    assert not missing, (
        f"USER-INITIATED-IMPROVEMENT-TRIGGER-V1 references "
        f"{sorted(missing)} but they're not in the bridge's "
        f"structured-field allowlist. Workflow YAML refs will "
        f"silently fail to populate."
    )


def test_reserved_fields_do_not_overlap_with_allowlist():
    """Bridge integrity: no field can be both reserved-from-CC
    AND allowed-from-CC."""
    overlap = _BRIDGE_RESERVED_FIELDS & _BRIDGE_STRUCTURED_FIELD_ALLOWLIST
    assert not overlap, (
        f"Allowlist and reserved sets overlap on {sorted(overlap)} — "
        f"the bridge would attempt to drop CC's value AND merge it. "
        f"Pick one bucket per field."
    )
