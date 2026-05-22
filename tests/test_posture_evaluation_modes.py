"""POSTURE-EVALUATION-MODES-V1 (2026-05-22) acceptance tests.

Covers spec ACs 1-15 (AC16 = no-regression sweep handled in
the existing dispatch_gate test file via per-test monkeypatch).

Structure:
- ACs 1-5: env resolution + normalization + fail-loud fallback
- ACs 6-8: ambiguous-fallback per mode
- AC9-10: reactive_soft_write_auto_proceed per mode
- ACs 11-13: APPROVE / CONFLICT / CLARIFY responses always
  respect their canonical mapping regardless of mode
- AC14: prompt preamble injected into system_prompt
- AC15: GATE_MODE_RESOLVED INFO log fires at __init__
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.gate import (
    DispatchGate,
    GateModePolicy,
    _POLICY_BALANCED,
    _POLICY_PERMISSIVE,
    _POLICY_STRICT,
    _resolve_gate_mode_policy,
)


# ============================================================
# Helpers
# ============================================================


def _make_gate(mode_env: str | None = None, monkeypatch=None) -> DispatchGate:
    if monkeypatch is not None:
        if mode_env is None:
            monkeypatch.delenv("KERNOS_GATE_MODE", raising=False)
        else:
            monkeypatch.setenv("KERNOS_GATE_MODE", mode_env)
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock()
    return DispatchGate(
        reasoning_service=reasoning,
        registry=None,
        state=AsyncMock(),
        events=AsyncMock(),
    )


async def _eval_model_with_response(
    gate: DispatchGate, response: str,
) -> "GateResult":  # noqa: F821 — runtime import only
    gate._reasoning.complete_simple = AsyncMock(return_value=response)
    return await gate._evaluate_model(
        tool_name="canvas_create",
        tool_input={"scope": "team", "name": "Test"},
        effect="hard_write",
        messages=None,
        agent_reasoning="Test reasoning",
        instance_id="test_inst",
        active_space_id="space_test",
        user_message="Create a team canvas",
    )


# ============================================================
# ACs 1-5: env resolution
# ============================================================


class TestModeResolution:
    def test_unset_resolves_to_permissive(self, monkeypatch):
        """AC1: unset env → permissive (default, behavior-neutral)."""
        monkeypatch.delenv("KERNOS_GATE_MODE", raising=False)
        policy = _resolve_gate_mode_policy()
        assert policy.name == "permissive"
        assert policy is _POLICY_PERMISSIVE

    def test_balanced_explicit(self, monkeypatch):
        """AC2: explicit balanced."""
        monkeypatch.setenv("KERNOS_GATE_MODE", "balanced")
        assert _resolve_gate_mode_policy().name == "balanced"

    def test_strict_explicit(self, monkeypatch):
        """AC3: explicit strict."""
        monkeypatch.setenv("KERNOS_GATE_MODE", "strict")
        assert _resolve_gate_mode_policy().name == "strict"

    def test_invalid_falls_back_to_strict_with_error_log(
        self, monkeypatch, caplog,
    ):
        """AC4: unknown env → strict + ERROR log (fail-loud + fall-safe)."""
        monkeypatch.setenv("KERNOS_GATE_MODE", "bogus")
        with caplog.at_level(logging.ERROR, logger="kernos.kernel.gate"):
            policy = _resolve_gate_mode_policy()
        assert policy.name == "strict"
        errors = [
            r for r in caplog.records
            if r.levelno == logging.ERROR
            and "KERNOS_GATE_MODE" in r.getMessage()
        ]
        assert errors
        assert "bogus" in errors[0].getMessage()

    @pytest.mark.parametrize("env_value,expected_name", [
        ("PERMISSIVE", "permissive"),
        ("Balanced", "balanced"),
        ("STRICT", "strict"),
        ("  permissive  ", "permissive"),
        ("  BALANCED  ", "balanced"),
        ("  Strict  ", "strict"),
    ])
    def test_env_normalization(self, monkeypatch, env_value, expected_name):
        """AC5: case + whitespace normalization."""
        monkeypatch.setenv("KERNOS_GATE_MODE", env_value)
        assert _resolve_gate_mode_policy().name == expected_name


# ============================================================
# ACs 6-8: ambiguous-branch fallback per mode
# ============================================================


class TestAmbiguousFallback:
    async def test_permissive_ambiguous_proceeds(self, monkeypatch):
        """AC6: permissive + ambiguous response (junk or CONFIRM) → allowed=True."""
        gate = _make_gate("permissive", monkeypatch)
        result = await _eval_model_with_response(gate, "CONFIRM")
        assert result.allowed is True
        assert result.reason == "approved_by_mode"
        assert result.method == "mode_permissive"

    async def test_permissive_unparseable_proceeds(self, monkeypatch):
        """AC6 variant: unparseable response also flows through permissive proceed."""
        gate = _make_gate("permissive", monkeypatch)
        result = await _eval_model_with_response(gate, "??? unclear ???")
        assert result.allowed is True
        assert result.reason == "approved_by_mode"

    async def test_balanced_ambiguous_confirms(self, monkeypatch):
        """AC7: balanced + ambiguous → confirm (current pre-V1 behavior)."""
        gate = _make_gate("balanced", monkeypatch)
        result = await _eval_model_with_response(gate, "CONFIRM")
        assert result.allowed is False
        assert result.reason == "confirm"
        assert result.method == "model_check"

    async def test_strict_ambiguous_refuses(self, monkeypatch):
        """AC8: strict + ambiguous → blocked outright, no confirm offered."""
        gate = _make_gate("strict", monkeypatch)
        result = await _eval_model_with_response(gate, "CONFIRM")
        assert result.allowed is False
        assert result.reason == "refused_by_mode"
        assert result.method == "mode_strict"


# ============================================================
# ACs 9-10: reactive_soft_write_auto_proceed per mode
# ============================================================


class TestReactiveSoftWriteBypassPerMode:
    def test_strict_disables_bypass(self, monkeypatch):
        """AC9: strict mode disables reactive_soft_write bypass."""
        gate = _make_gate("strict", monkeypatch)
        assert gate._mode_policy.reactive_soft_write_auto_proceed is False

    def test_permissive_enables_bypass(self, monkeypatch):
        """AC10: permissive preserves bypass (pre-V1 behavior)."""
        gate = _make_gate("permissive", monkeypatch)
        assert gate._mode_policy.reactive_soft_write_auto_proceed is True

    def test_balanced_enables_bypass(self, monkeypatch):
        """AC10: balanced preserves bypass."""
        gate = _make_gate("balanced", monkeypatch)
        assert gate._mode_policy.reactive_soft_write_auto_proceed is True


# ============================================================
# ACs 11-13: APPROVE / CONFLICT / CLARIFY unchanged by mode
# ============================================================


class TestCanonicalResponsesUnaffectedByMode:
    @pytest.mark.parametrize("mode", ["permissive", "balanced", "strict"])
    async def test_approve_always_allows(self, mode, monkeypatch):
        """AC11: APPROVE → allowed=True in all modes."""
        gate = _make_gate(mode, monkeypatch)
        result = await _eval_model_with_response(gate, "APPROVE")
        assert result.allowed is True
        assert result.reason == "approved"
        assert result.method == "model_check"

    @pytest.mark.parametrize("mode", ["permissive", "balanced", "strict"])
    async def test_conflict_always_blocks_with_rule(self, mode, monkeypatch):
        """AC12: CONFLICT → allowed=False with conflicting_rule populated, all modes."""
        gate = _make_gate(mode, monkeypatch)
        result = await _eval_model_with_response(
            gate, "CONFLICT: Never delete the user's files",
        )
        assert result.allowed is False
        assert result.reason == "covenant_conflict"
        assert "Never delete" in result.conflicting_rule

    @pytest.mark.parametrize("mode", ["permissive", "balanced", "strict"])
    async def test_clarify_always_blocks_with_clarify(self, mode, monkeypatch):
        """AC13: CLARIFY → blocked with reason='clarify' regardless of mode.

        CLARIFY is the model's explicit ambiguity signal — NEVER
        subject to ambiguous_fallback (which only governs the
        CONFIRM/unparseable branch)."""
        gate = _make_gate(mode, monkeypatch)
        result = await _eval_model_with_response(gate, "CLARIFY")
        assert result.allowed is False
        assert result.reason == "clarify"
        # Critically: NOT "refused_by_mode" or "approved_by_mode"
        assert result.method == "model_check"


# ============================================================
# AC14: prompt preamble injected
# ============================================================


class TestPromptPreambleInjected:
    @pytest.mark.parametrize("mode,marker", [
        ("permissive", "POSTURE: permissive"),
        ("balanced", "POSTURE: balanced"),
        ("strict", "POSTURE: strict"),
    ])
    async def test_preamble_appears_in_system_prompt(
        self, mode, marker, monkeypatch,
    ):
        gate = _make_gate(mode, monkeypatch)
        captured_prompts = {}

        async def fake_complete_simple(
            *, system_prompt, user_content, max_tokens, prefer_cheap,
        ):
            captured_prompts["system"] = system_prompt
            return "APPROVE"

        gate._reasoning.complete_simple = fake_complete_simple
        await gate._evaluate_model(
            tool_name="canvas_create",
            tool_input={"scope": "team"},
            effect="hard_write",
            messages=None,
            agent_reasoning="Test",
            instance_id="test_inst",
            active_space_id="space_test",
            user_message="Test request",
        )
        assert marker in captured_prompts["system"], (
            f"preamble marker {marker!r} missing from system_prompt"
        )


# ============================================================
# AC15: GATE_MODE_RESOLVED log fires at __init__
# ============================================================


class TestBootLogLine:
    def test_init_logs_resolved_mode(self, monkeypatch, caplog):
        monkeypatch.setenv("KERNOS_GATE_MODE", "balanced")
        with caplog.at_level(logging.INFO, logger="kernos.kernel.gate"):
            _make_gate("balanced", monkeypatch)
        resolved = [
            r for r in caplog.records
            if "GATE_MODE_RESOLVED" in r.getMessage()
        ]
        assert len(resolved) >= 1
        msg = resolved[-1].getMessage()
        assert "mode=balanced" in msg
        assert "ambiguous_fallback=confirm" in msg


# ============================================================
# Bonus: set_mode_policy contract surface
# ============================================================


class TestSetModePolicy:
    def test_swap_changes_active_policy(self, monkeypatch):
        gate = _make_gate("permissive", monkeypatch)
        assert gate._mode_policy.name == "permissive"
        gate.set_mode_policy(_POLICY_STRICT)
        assert gate._mode_policy.name == "strict"
        assert gate._mode_policy is _POLICY_STRICT
