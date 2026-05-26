"""Tests for SUBSTRATE-SELF-TEST-V1 (2026-05-26) infrastructure.

Pins the runner mechanics (probe loading, evidence validation,
shallow-evidence rejection, CLI exit codes) using synthetic
probe modules. The 8 real probes live in ``tests/substrate_soak/``
and have their own per-probe regression coverage.
"""
from __future__ import annotations

import asyncio
import json
import sys

import pytest

from kernos.kernel.self_test_gate import (
    PROBE_MODULE_NAMES,
    ProbeResult,
    SoakSuiteResult,
    SubstrateSoakRunner,
    _is_shallow_value,
    _validate_evidence,
)


# ============================================================
# Shallow-evidence detection (AC2)
# ============================================================


class TestIsShallowValue:
    def test_none_is_shallow(self):
        assert _is_shallow_value(None) is True

    def test_empty_dict_is_shallow(self):
        assert _is_shallow_value({}) is True

    def test_empty_list_is_shallow(self):
        assert _is_shallow_value([]) is True

    def test_empty_string_is_shallow(self):
        assert _is_shallow_value("") is True

    def test_ok_string_is_shallow(self):
        assert _is_shallow_value("ok") is True
        assert _is_shallow_value("OK") is True
        assert _is_shallow_value("  Ok  ") is True

    def test_bool_alone_not_shallow_via_value_check(self):
        # A bare bool isn't shallow at the value level — the
        # shallow check is whether the OVERALL dict has any
        # non-sentinel values. Bools can be load-bearing
        # (passed: True/False) when paired with other keys.
        assert _is_shallow_value(True) is False
        assert _is_shallow_value(False) is False

    def test_real_string_not_shallow(self):
        assert _is_shallow_value("actual evidence text") is False

    def test_nonempty_dict_not_shallow(self):
        assert _is_shallow_value({"key": "value"}) is False


# ============================================================
# Evidence validation (AC2)
# ============================================================


class TestValidateEvidence:
    def test_non_dict_evidence_fails(self):
        ok, reason = _validate_evidence(
            "not a dict", frozenset({"key"}), "behavioral", "p",
        )
        assert ok is False
        assert "not a dict" in reason

    def test_empty_evidence_fails(self):
        ok, reason = _validate_evidence(
            {}, frozenset({"key"}), "behavioral", "p",
        )
        assert ok is False
        assert "empty" in reason

    def test_missing_required_key_fails(self):
        ok, reason = _validate_evidence(
            {"unrelated": "value"},
            frozenset({"required_key"}),
            "behavioral", "p",
        )
        assert ok is False
        assert "missing declared key" in reason
        assert "required_key" in reason

    def test_shallow_evidence_dict_fails(self):
        """{"ok": True} satisfies key presence but is sentinel-
        only — must be rejected per AC2."""
        ok, reason = _validate_evidence(
            {"key": None}, frozenset({"key"}), "substrate", "p",
        )
        assert ok is False
        assert "shallow" in reason

    def test_real_evidence_passes(self):
        ok, reason = _validate_evidence(
            {"key": "actual content", "count": 42},
            frozenset({"key", "count"}),
            "substrate", "p",
        )
        assert ok is True
        assert reason == ""

    def test_mixed_bool_with_real_evidence_passes(self):
        """A bool key paired with at least one non-sentinel value
        passes — the bool can be the load-bearing pass/fail flag."""
        ok, reason = _validate_evidence(
            {"passed_check": True, "evidence_detail": "value seen"},
            frozenset({"passed_check", "evidence_detail"}),
            "substrate", "p",
        )
        assert ok is True


# ============================================================
# Probe module loading
# ============================================================


def _install_synthetic_probe(
    monkeypatch, name: str, *,
    run_probe_fn, required_b=frozenset(), required_s=frozenset(),
):
    """Install a synthetic probe module at
    ``tests.substrate_soak.{name}`` so the runner can import it.
    """
    import types
    mod = types.ModuleType(f"tests.substrate_soak.{name}")
    mod.REQUIRED_BEHAVIORAL_KEYS = required_b
    mod.REQUIRED_SUBSTRATE_KEYS = required_s
    mod.run_probe = run_probe_fn
    monkeypatch.setitem(sys.modules, f"tests.substrate_soak.{name}", mod)


class TestSubstrateSoakRunnerProbeLoading:
    @pytest.mark.asyncio
    async def test_missing_probe_module_returns_failure(self):
        runner = SubstrateSoakRunner(
            probe_names=("nonexistent_probe_xyz",),
        )
        result = await runner.run_probe("nonexistent_probe_xyz")
        assert result.passed is False
        assert "import failed" in result.failure_reason

    @pytest.mark.asyncio
    async def test_probe_without_run_probe_fn_fails(self, monkeypatch):
        _install_synthetic_probe(
            monkeypatch, "no_run_fn",
            run_probe_fn=None,
        )
        runner = SubstrateSoakRunner(probe_names=("no_run_fn",))
        result = await runner.run_probe("no_run_fn")
        assert result.passed is False
        assert "async run_probe" in result.failure_reason

    @pytest.mark.asyncio
    async def test_probe_run_raises_caught_as_failure(self, monkeypatch):
        async def _raises():
            raise RuntimeError("boom")
        _install_synthetic_probe(
            monkeypatch, "raises",
            run_probe_fn=_raises,
        )
        runner = SubstrateSoakRunner(probe_names=("raises",))
        result = await runner.run_probe("raises")
        assert result.passed is False
        assert "probe raised" in result.failure_reason
        assert "RuntimeError" in result.failure_reason
        assert "boom" in result.failure_reason

    @pytest.mark.asyncio
    async def test_probe_returning_non_proberesult_fails(self, monkeypatch):
        async def _wrong_shape():
            return {"passed": True}  # not a ProbeResult
        _install_synthetic_probe(
            monkeypatch, "wrong_shape",
            run_probe_fn=_wrong_shape,
        )
        runner = SubstrateSoakRunner(probe_names=("wrong_shape",))
        result = await runner.run_probe("wrong_shape")
        assert result.passed is False
        assert "expected ProbeResult" in result.failure_reason

    @pytest.mark.asyncio
    async def test_probe_with_shallow_behavioral_evidence_fails(
        self, monkeypatch,
    ):
        async def _shallow_b():
            return ProbeResult(
                probe_name="shallow_b", passed=True,
                behavioral_evidence={"declared": None},
                substrate_evidence={
                    "declared": "real content",
                    "count": 5,
                },
                duration_ms=10,
            )
        _install_synthetic_probe(
            monkeypatch, "shallow_b",
            run_probe_fn=_shallow_b,
            required_b=frozenset({"declared"}),
            required_s=frozenset({"declared", "count"}),
        )
        runner = SubstrateSoakRunner(probe_names=("shallow_b",))
        result = await runner.run_probe("shallow_b")
        assert result.passed is False
        assert "shallow_evidence" in result.failure_reason

    @pytest.mark.asyncio
    async def test_probe_with_missing_required_key_fails(
        self, monkeypatch,
    ):
        async def _missing_key():
            return ProbeResult(
                probe_name="missing_key", passed=True,
                behavioral_evidence={"actual_key": "value"},
                substrate_evidence={"key": "value"},
                duration_ms=10,
            )
        _install_synthetic_probe(
            monkeypatch, "missing_key",
            run_probe_fn=_missing_key,
            required_b=frozenset({"declared_but_missing"}),
            required_s=frozenset({"key"}),
        )
        runner = SubstrateSoakRunner(probe_names=("missing_key",))
        result = await runner.run_probe("missing_key")
        assert result.passed is False
        assert "missing declared key" in result.failure_reason
        assert "declared_but_missing" in result.failure_reason

    @pytest.mark.asyncio
    async def test_probe_with_valid_evidence_passes(self, monkeypatch):
        async def _good_probe():
            return ProbeResult(
                probe_name="good", passed=True,
                behavioral_evidence={"output": "real value"},
                substrate_evidence={
                    "state": "real",
                    "count": 7,
                },
                duration_ms=42,
            )
        _install_synthetic_probe(
            monkeypatch, "good",
            run_probe_fn=_good_probe,
            required_b=frozenset({"output"}),
            required_s=frozenset({"state", "count"}),
        )
        runner = SubstrateSoakRunner(probe_names=("good",))
        result = await runner.run_probe("good")
        assert result.passed is True
        assert result.failure_reason == ""
        assert result.duration_ms == 42


# ============================================================
# Suite-level execution
# ============================================================


class TestSubstrateSoakRunnerSuite:
    @pytest.mark.asyncio
    async def test_run_all_returns_aggregate(self, monkeypatch):
        async def _good():
            return ProbeResult(
                probe_name="g", passed=True,
                behavioral_evidence={"out": "x"},
                substrate_evidence={"state": "y"},
                duration_ms=5,
            )
        _install_synthetic_probe(
            monkeypatch, "good_probe_1",
            run_probe_fn=_good,
            required_b=frozenset({"out"}),
            required_s=frozenset({"state"}),
        )
        _install_synthetic_probe(
            monkeypatch, "good_probe_2",
            run_probe_fn=_good,
            required_b=frozenset({"out"}),
            required_s=frozenset({"state"}),
        )
        runner = SubstrateSoakRunner(
            probe_names=("good_probe_1", "good_probe_2"),
        )
        suite = await runner.run_all()
        assert suite.all_passed is True
        assert len(suite.per_probe) == 2
        assert suite.failing_probe_names() == ()
        assert suite.total_duration_ms >= 0

    @pytest.mark.asyncio
    async def test_run_all_one_failure_marks_suite_failed(
        self, monkeypatch,
    ):
        async def _good():
            return ProbeResult(
                probe_name="g", passed=True,
                behavioral_evidence={"out": "x"},
                substrate_evidence={"state": "y"},
                duration_ms=5,
            )
        async def _bad():
            raise RuntimeError("no")
        _install_synthetic_probe(
            monkeypatch, "passes",
            run_probe_fn=_good,
            required_b=frozenset({"out"}),
            required_s=frozenset({"state"}),
        )
        _install_synthetic_probe(
            monkeypatch, "fails",
            run_probe_fn=_bad,
        )
        runner = SubstrateSoakRunner(probe_names=("passes", "fails"))
        suite = await runner.run_all()
        assert suite.all_passed is False
        assert suite.failing_probe_names() == ("fails",)

    @pytest.mark.asyncio
    async def test_one_probe_failure_does_not_block_next(
        self, monkeypatch,
    ):
        """Per spec: probes are isolated. A failing probe must not
        prevent subsequent probes from running."""
        order: list[str] = []

        async def _track_first():
            order.append("first")
            raise RuntimeError("first fails")

        async def _track_second():
            order.append("second")
            return ProbeResult(
                probe_name="second", passed=True,
                behavioral_evidence={"out": "x"},
                substrate_evidence={"state": "y"},
                duration_ms=1,
            )
        _install_synthetic_probe(
            monkeypatch, "first_fails",
            run_probe_fn=_track_first,
        )
        _install_synthetic_probe(
            monkeypatch, "second_after",
            run_probe_fn=_track_second,
            required_b=frozenset({"out"}),
            required_s=frozenset({"state"}),
        )
        runner = SubstrateSoakRunner(
            probe_names=("first_fails", "second_after"),
        )
        suite = await runner.run_all()
        assert order == ["first", "second"]
        assert suite.per_probe[0].passed is False
        assert suite.per_probe[1].passed is True


# ============================================================
# PROBE_MODULE_NAMES contract
# ============================================================


class TestProbeModuleNamesContract:
    def test_eight_probes_listed(self):
        assert len(PROBE_MODULE_NAMES) == 8

    def test_all_expected_probes_named(self):
        expected = {
            "agent_round_trip_soak",
            "self_knowledge_invariant",
            "consult_drain_invariant",
            "dispatch_canonicalization_invariant",
            "retry_with_feedback_invariant",
            "gateway_deafness_invariant",
            "approval_loop_invariant",
            "loop_health_completion_invariant",
        }
        assert set(PROBE_MODULE_NAMES) == expected


# ============================================================
# include_soak schema extension
# ============================================================


class TestIncludeSoakSchema:
    def test_include_soak_in_schema(self):
        from kernos.kernel.self_test_gate import RUN_SELF_TEST_SUITE_TOOL
        props = RUN_SELF_TEST_SUITE_TOOL["input_schema"]["properties"]
        assert "include_soak" in props
        assert props["include_soak"]["type"] == "boolean"

    def test_include_soak_not_required(self):
        """Default false for back-compat with existing
        improve_kernos orchestrator callers."""
        from kernos.kernel.self_test_gate import RUN_SELF_TEST_SUITE_TOOL
        required = RUN_SELF_TEST_SUITE_TOOL["input_schema"]["required"]
        assert "include_soak" not in required


# ============================================================
# CLI wrapper (AC11)
# ============================================================


class TestCliWrapper:
    def test_cli_no_include_soak_flag_returns_3(self, capsys):
        """Standalone CLI without --include-soak exits 3
        with explanatory message."""
        from kernos.kernel.self_test_gate import _cli_main
        # Patch argv since _cli_main uses argparse on sys.argv
        old_argv = sys.argv
        sys.argv = ["self_test_gate"]
        try:
            exit_code = _cli_main()
        finally:
            sys.argv = old_argv
        assert exit_code == 3
        captured = capsys.readouterr()
        assert "requires --include-soak" in captured.err

    def test_cli_json_no_include_soak_returns_3_with_json(self, capsys):
        from kernos.kernel.self_test_gate import _cli_main
        old_argv = sys.argv
        sys.argv = ["self_test_gate", "--json"]
        try:
            exit_code = _cli_main()
        finally:
            sys.argv = old_argv
        assert exit_code == 3
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["ok"] is False
        assert payload["reason"] == "cli_requires_include_soak"
