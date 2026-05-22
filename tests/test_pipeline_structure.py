"""Structural tests for the decomposed turn pipeline.

Spec reference: SPEC-HANDLER-PIPELINE-DECOMPOSE. Asserts the pipeline's
shape and import surface rather than behavior — behavioral regressions
are caught by the existing test suite.
"""
from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest


class TestPhaseModulesExist:
    """Spec expected behavior #3: each phase module is independently importable."""

    @pytest.mark.parametrize("phase_name", [
        "provision", "route", "assemble", "reason", "consequence", "persist",
    ])
    def test_phase_module_importable(self, phase_name):
        mod = importlib.import_module(f"kernos.messages.phases.{phase_name}")
        assert hasattr(mod, "run"), (
            f"phases.{phase_name} must export an async run(ctx) entry point"
        )

    @pytest.mark.parametrize("phase_name", [
        "provision", "route", "assemble", "reason", "consequence", "persist",
    ])
    def test_phase_run_is_async(self, phase_name):
        mod = importlib.import_module(f"kernos.messages.phases.{phase_name}")
        assert inspect.iscoroutinefunction(mod.run), (
            f"phases.{phase_name}.run must be async"
        )

    @pytest.mark.parametrize("phase_name", [
        "provision", "route", "assemble", "reason", "consequence", "persist",
    ])
    def test_phase_run_single_ctx_arg(self, phase_name):
        mod = importlib.import_module(f"kernos.messages.phases.{phase_name}")
        sig = inspect.signature(mod.run)
        params = list(sig.parameters.values())
        assert len(params) == 1, (
            f"phases.{phase_name}.run must take a single ctx argument; got {params}"
        )


class TestPipelineOrdering:
    """Spec expected behavior: pipeline wires phases in the documented order."""

    def test_all_phases_contains_six_in_order(self):
        from kernos.messages.pipeline import ALL_PHASES
        from kernos.messages.phases import (
            assemble, consequence, persist, provision, reason, route,
        )
        assert ALL_PHASES == (provision, route, assemble, reason, consequence, persist), (
            "ALL_PHASES must list the six phases in documented execution order"
        )

    def test_lightweight_phases_are_provision_route(self):
        from kernos.messages.pipeline import LIGHTWEIGHT_PHASES
        from kernos.messages.phases import provision, route
        assert LIGHTWEIGHT_PHASES == (provision, route)

    def test_heavy_phases_are_assemble_reason_consequence_persist(self):
        from kernos.messages.pipeline import HEAVY_PHASES
        from kernos.messages.phases import assemble, consequence, persist, reason
        assert HEAVY_PHASES == (assemble, reason, consequence, persist)

    def test_run_turn_is_async(self):
        from kernos.messages.pipeline import run_turn
        assert inspect.iscoroutinefunction(run_turn)


class TestPhaseContext:
    """Spec expected behavior #4: PhaseContext import works from a bare module path."""

    def test_bare_import_works(self):
        from kernos.messages.phase_context import PhaseContext
        # Dataclass check — PhaseContext aliases TurnContext, which is a
        # @dataclass in handler.py.
        assert hasattr(PhaseContext, "__dataclass_fields__")

    def test_handler_field_present(self):
        """PhaseContext carries the back-reference phase modules use to
        reach kernel services. The field is populated at turn start by
        MessageHandler.process()."""
        from kernos.messages.phase_context import PhaseContext
        assert "handler" in PhaseContext.__dataclass_fields__


class TestHandlerShimDelegation:
    """Each handler._phase_* method delegates to its phase module.

    Spec's ~30-80 line shim target isn't hit by this batch (MessageRunner +
    slash handlers + zone builders still live in handler.py). What IS
    verified: the phase logic is exclusively in phase modules, and the
    handler methods are thin delegators.
    """

    @pytest.mark.parametrize("method_name,module_name", [
        ("_phase_provision", "provision"),
        ("_phase_route", "route"),
        ("_phase_assemble", "assemble"),
        ("_phase_reason", "reason"),
        ("_phase_consequence", "consequence"),
        ("_phase_persist", "persist"),
    ])
    def test_phase_shim_is_thin(self, method_name, module_name):
        """Each _phase_* shim must be under 20 source lines — the phase
        logic lives in phases/<module_name>.py, not on MessageHandler."""
        from kernos.messages.handler import MessageHandler
        method = getattr(MessageHandler, method_name)
        source = inspect.getsource(method)
        line_count = len(source.splitlines())
        assert line_count < 20, (
            f"{method_name} is {line_count} lines — phase logic should live "
            f"in phases/{module_name}.py, not on MessageHandler"
        )

    @pytest.mark.parametrize("method_name,module_name", [
        ("_phase_provision", "provision"),
        ("_phase_route", "route"),
        ("_phase_assemble", "assemble"),
        ("_phase_reason", "reason"),
        ("_phase_consequence", "consequence"),
        ("_phase_persist", "persist"),
    ])
    def test_phase_shim_mentions_phase_module(self, method_name, module_name):
        from kernos.messages.handler import MessageHandler
        method = getattr(MessageHandler, method_name)
        source = inspect.getsource(method)
        assert f"phases import {module_name}" in source or f"phases.{module_name}" in source, (
            f"{method_name} must delegate to kernos.messages.phases.{module_name}"
        )


class TestPhaseFilesHaveSensibleSize:
    """Phase modules should be meaningfully sized — not stubs, not unbounded."""

    @pytest.mark.parametrize("phase_name,min_lines,max_lines", [
        # Bounds measured after the verbatim move. Min guards against a
        # revert-to-stub; max flags accidental growth from unrelated code
        # creeping into a phase. Upper bound for "assemble" was raised
        # at COGNITIVE-CONTEXT-V1 C3a — the assemble phase now owns
        # both legacy string construction and the typed cognitive
        # substrate (CognitiveContext) build, so the size growth is
        # intentional substrate work, not unrelated creep.
        # Raised again at POSTURE-SURFACING-CALIBRATION-V1 (2026-05-22)
        # to accommodate the intent-classifier hook, co-surfacing
        # pair promotion, and the tool.withheld_from_surface
        # event-emit sites in the surfacing loop. ~30 LOC of
        # substrate-tier additions, not unrelated creep.
        # Raised once more at POSTURE-PREFLIGHT-V1 (2026-05-22) for
        # the per-turn surfacing-snapshot capture (~50 LOC) the
        # inspect_tool_availability tool queries.
        ("provision", 30, 200),
        ("route", 80, 250),
        ("assemble", 400, 1200),
        ("reason", 20, 100),
        ("consequence", 50, 200),
        ("persist", 150, 400),
    ])
    def test_phase_module_size_in_range(self, phase_name, min_lines, max_lines):
        module_path = (
            Path(__file__).parent.parent
            / "kernos" / "messages" / "phases" / f"{phase_name}.py"
        )
        lines = module_path.read_text().count("\n")
        assert min_lines <= lines <= max_lines, (
            f"phases/{phase_name}.py is {lines} lines, outside "
            f"expected range [{min_lines}, {max_lines}]"
        )
