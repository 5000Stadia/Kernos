"""Canonical descriptor hash tests (STS C2).

Spec reference: SPEC-STS-v2 AC #3, #20.

Pins:

* Canonical SHA-256 over a workflow descriptor AST.
* Equivalent descriptors hash identically; key reorder is invisible.
* Volatile registry metadata (id, workflow_id, created_at, updated_at,
  registered_at, version) is excluded.
* Non-volatile fields (display_name, aliases, intent_summary,
  action_sequence, predicate, trigger, verifier, bounds) are included.
* ``prev_version_id`` IS included (the design review edit v1 → v2) — modifications
  cannot be retargeted by swapping prev_version_id while keeping the
  same hash.
"""
from __future__ import annotations

import pytest

from kernos.kernel.substrate_tools.registration.descriptor_hash import (
    DESCRIPTOR_VOLATILE_FIELDS,
    compute_descriptor_hash,
)


def _canonical_descriptor(**overrides) -> dict:
    """A baseline descriptor with all the fields that matter for the
    hash. Tests override specific keys to verify the hash response."""
    base = {
        "name": "test-workflow",
        "display_name": "Test Workflow",
        "aliases": ["test", "spec"],
        "intent_summary": "verify the canonical hash",
        "owner": "owner",
        "instance_id": "inst_a",
        "bounds": {"iteration_count": 1},
        "verifier": {"flavor": "deterministic", "check": "x == y"},
        "action_sequence": [
            {
                "action_type": "mark_state",
                "parameters": {"key": "k", "value": "v", "scope": "ledger"},
            },
        ],
        "trigger": {"event_type": "tool.called", "predicate": "true"},
    }
    base.update(overrides)
    return base


# ===========================================================================
# AC #3: canonical hash basic properties
# ===========================================================================


class TestDeterminism:
    def test_hash_is_64_char_hex(self):
        h = compute_descriptor_hash(_canonical_descriptor())
        assert len(h) == 64
        int(h, 16)  # parses as hex

    def test_same_descriptor_same_hash(self):
        a = compute_descriptor_hash(_canonical_descriptor())
        b = compute_descriptor_hash(_canonical_descriptor())
        assert a == b

    def test_key_reorder_invisible(self):
        a = {"foo": 1, "bar": 2, "baz": 3}
        b = {"baz": 3, "foo": 1, "bar": 2}
        assert compute_descriptor_hash(a) == compute_descriptor_hash(b)

    def test_nested_key_reorder_invisible(self):
        a = {
            "verifier": {"flavor": "deterministic", "check": "x == y"},
            "bounds": {"iteration_count": 1},
        }
        b = {
            "bounds": {"iteration_count": 1},
            "verifier": {"check": "x == y", "flavor": "deterministic"},
        }
        assert compute_descriptor_hash(a) == compute_descriptor_hash(b)

    def test_list_order_matters(self):
        """Action sequences are ordered — reordering changes meaning."""
        a = _canonical_descriptor(action_sequence=[
            {"action_type": "first"},
            {"action_type": "second"},
        ])
        b = _canonical_descriptor(action_sequence=[
            {"action_type": "second"},
            {"action_type": "first"},
        ])
        assert compute_descriptor_hash(a) != compute_descriptor_hash(b)


# ===========================================================================
# AC #3: volatile fields are excluded
# ===========================================================================


class TestVolatileExclusion:
    def test_volatile_field_set_pinned(self):
        # Pin the exact set so adding a field is a deliberate substrate
        # change. prev_version_id MUST NOT be in this set (the design review edit v1→v2).
        assert DESCRIPTOR_VOLATILE_FIELDS == frozenset({
            "id",
            "workflow_id",
            "created_at",
            "updated_at",
            "registered_at",
            "version",
        })
        assert "prev_version_id" not in DESCRIPTOR_VOLATILE_FIELDS

    @pytest.mark.parametrize("field", [
        "id", "workflow_id", "created_at", "updated_at",
        "registered_at", "version",
    ])
    def test_volatile_field_does_not_change_hash(self, field):
        a = _canonical_descriptor()
        b = _canonical_descriptor()
        b[field] = "should-not-affect-hash"
        assert compute_descriptor_hash(a) == compute_descriptor_hash(b)

    def test_volatile_keys_preserved_at_nested_levels(self):
        """Codex final-pass fix (REAL #3): ``id`` and ``version`` keys
        nested inside the descriptor (action parameters, triggers,
        verifiers, future provider payloads) are part of the executable
        shape and MUST contribute to the hash. Two descriptors that
        differ only in a nested ``id`` MUST hash differently — otherwise
        semantically distinct descriptors collide."""
        a = _canonical_descriptor()
        b = _canonical_descriptor()
        b["action_sequence"][0]["id"] = "nested-id-must-affect-hash"
        assert compute_descriptor_hash(a) != compute_descriptor_hash(b)

    def test_volatile_keys_only_drop_at_top_level(self):
        """Belt-and-suspenders pin: a nested ``version`` field is
        preserved even though top-level ``version`` drops."""
        a = _canonical_descriptor(version="1")
        b = _canonical_descriptor(version="999")
        # Top-level version drops → identical hashes.
        assert compute_descriptor_hash(a) == compute_descriptor_hash(b)
        # Nested version is preserved.
        c = _canonical_descriptor()
        d = _canonical_descriptor()
        c["trigger"]["version"] = "v1"
        d["trigger"]["version"] = "v2"
        assert compute_descriptor_hash(c) != compute_descriptor_hash(d)


# ===========================================================================
# AC #3: non-volatile fields are included
# ===========================================================================


class TestNonVolatileInclusion:
    @pytest.mark.parametrize("field,value", [
        ("display_name", "different name"),
        ("aliases", ["different", "aliases"]),
        ("intent_summary", "different intent"),
        ("owner", "different owner"),
        ("name", "different-name"),
    ])
    def test_changing_non_volatile_changes_hash(self, field, value):
        a = compute_descriptor_hash(_canonical_descriptor())
        b = compute_descriptor_hash(_canonical_descriptor(**{field: value}))
        assert a != b

    def test_changing_action_sequence_changes_hash(self):
        a = compute_descriptor_hash(_canonical_descriptor())
        b = compute_descriptor_hash(_canonical_descriptor(action_sequence=[
            {"action_type": "different_action", "parameters": {}},
        ]))
        assert a != b

    def test_changing_trigger_predicate_changes_hash(self):
        a = compute_descriptor_hash(_canonical_descriptor())
        b = compute_descriptor_hash(_canonical_descriptor(
            trigger={"event_type": "tool.called", "predicate": "false"},
        ))
        assert a != b

    def test_changing_verifier_changes_hash(self):
        a = compute_descriptor_hash(_canonical_descriptor())
        b = compute_descriptor_hash(_canonical_descriptor(
            verifier={"flavor": "deterministic", "check": "different"},
        ))
        assert a != b

    def test_changing_bounds_changes_hash(self):
        a = compute_descriptor_hash(_canonical_descriptor())
        b = compute_descriptor_hash(_canonical_descriptor(
            bounds={"iteration_count": 99},
        ))
        assert a != b


# ===========================================================================
# AC #20: prev_version_id is in the hash (the design review edit v1 → v2)
# ===========================================================================


class TestPrevVersionIdInclusion:
    def test_prev_version_id_changes_hash(self):
        """The the design review edit guard: identical descriptors with different
        prev_version_id values MUST hash differently. Without this,
        a modification approved against routine A could be applied to
        routine B by swapping prev_version_id at registration."""
        a = compute_descriptor_hash(_canonical_descriptor(
            prev_version_id="wf-A",
        ))
        b = compute_descriptor_hash(_canonical_descriptor(
            prev_version_id="wf-B",
        ))
        assert a != b

    def test_prev_version_id_present_vs_absent_differs(self):
        """Adding prev_version_id (modification) vs no prev_version_id
        (initial registration) must produce different hashes."""
        a = compute_descriptor_hash(_canonical_descriptor())
        b = compute_descriptor_hash(_canonical_descriptor(
            prev_version_id="wf-original",
        ))
        assert a != b


# ===========================================================================
# Type / input handling
# ===========================================================================


class TestInputHandling:
    def test_non_dict_raises(self):
        with pytest.raises(TypeError):
            compute_descriptor_hash("not a dict")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            compute_descriptor_hash(["list"])  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            compute_descriptor_hash(None)  # type: ignore[arg-type]

    def test_empty_dict_hashable(self):
        h = compute_descriptor_hash({})
        assert len(h) == 64

    def test_unicode_in_descriptor(self):
        """Canonical JSON uses ensure_ascii=False; unicode strings
        (e.g. the user's display_name) hash deterministically."""
        a = compute_descriptor_hash(_canonical_descriptor(
            display_name="Café Routine ☕",
        ))
        b = compute_descriptor_hash(_canonical_descriptor(
            display_name="Café Routine ☕",
        ))
        assert a == b
