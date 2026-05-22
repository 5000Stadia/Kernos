"""POSTURE-GATE-CLASSIFICATION-V1 (2026-05-22) acceptance tests.

Covers spec ACs 1-10:
- AC1-AC6 — canvas_create scope-aware classification + fallbacks
- AC7-AC9 — restart_self + respond_to_parcel retain prior behavior
- AC10 — regression-sweep on dispatch-gate tests runs separately

The DispatchGate is constructed standalone here (no registry,
no state, no events) — classify_tool_effect's per-tool branches
fire before any registry/MCP lookup, so the stub gate is
sufficient to pin classification behavior.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from kernos.kernel.gate import DispatchGate


def _make_gate() -> DispatchGate:
    return DispatchGate(
        reasoning_service=MagicMock(),
        registry=None,
        state=AsyncMock(),
        events=AsyncMock(),
    )


# ============================================================
# AC1-AC6 — canvas_create scope-aware classification
# ============================================================


class TestCanvasCreateScopeAware:
    def test_personal_scope_is_soft_write(self):
        """AC1: scope=personal → soft_write (owner-only, tombstone-able)."""
        gate = _make_gate()
        assert gate.classify_tool_effect(
            "canvas_create", None, {"scope": "personal"},
        ) == "soft_write"

    def test_specific_scope_is_hard_write(self):
        """AC2: scope=specific → hard_write (cross-member notification)."""
        gate = _make_gate()
        assert gate.classify_tool_effect(
            "canvas_create", None, {"scope": "specific", "members": ["m1"]},
        ) == "hard_write"

    def test_team_scope_is_hard_write(self):
        """AC3: scope=team → hard_write (cross-member shared state)."""
        gate = _make_gate()
        assert gate.classify_tool_effect(
            "canvas_create", None, {"scope": "team"},
        ) == "hard_write"

    def test_missing_scope_defaults_to_hard_write(self):
        """AC4: tool_input has no scope → conservative hard_write."""
        gate = _make_gate()
        assert gate.classify_tool_effect(
            "canvas_create", None, {},
        ) == "hard_write"

    def test_unknown_scope_defaults_to_hard_write(self):
        """AC5: scope value outside the enum → fail-safe hard_write."""
        gate = _make_gate()
        assert gate.classify_tool_effect(
            "canvas_create", None, {"scope": "bogus"},
        ) == "hard_write"

    def test_none_tool_input_tolerated(self):
        """AC6: tool_input=None → conservative hard_write (no crash)."""
        gate = _make_gate()
        assert gate.classify_tool_effect(
            "canvas_create", None, None,
        ) == "hard_write"


# ============================================================
# AC7-AC9 — restart_self + respond_to_parcel UNCHANGED
# ============================================================


class TestUnchangedHardWrites:
    def test_restart_self_still_hard_write(self):
        """AC7: restart_self classification unchanged regardless of input."""
        gate = _make_gate()
        assert gate.classify_tool_effect(
            "restart_self", None, None,
        ) == "hard_write"
        assert gate.classify_tool_effect(
            "restart_self", None, {"confirm": True},
        ) == "hard_write"
        assert gate.classify_tool_effect(
            "restart_self", None, {"confirm": False},
        ) == "hard_write"

    def test_respond_to_parcel_accept_still_hard_write(self):
        """AC8: respond_to_parcel(accept) classification unchanged."""
        gate = _make_gate()
        assert gate.classify_tool_effect(
            "respond_to_parcel", None, {"action": "accept"},
        ) == "hard_write"

    def test_respond_to_parcel_decline_still_soft_write(self):
        """AC9: respond_to_parcel(decline) classification unchanged."""
        gate = _make_gate()
        assert gate.classify_tool_effect(
            "respond_to_parcel", None, {"action": "decline"},
        ) == "soft_write"
