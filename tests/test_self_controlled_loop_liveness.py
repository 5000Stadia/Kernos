"""SELF-CONTROLLED-LOOP-LIVENESS-V1 (2026-05-21) acceptance tests.

Verifies the 9 spec checkpoints:

AC #1, #2 — Alias repair on both ingress points + both aliases.
AC #3   — Sentinel YAML loads and trigger registers.
AC #4   — Boot-smoke logs + event-stream rows produced on register.
AC #5   — Workflow ledger entry per boot (covered via the helper
          contract; integration verification happens post-merge).
AC #6   — Active-frequency threshold crossing emits exactly one event,
          deduped across repeat occurrences in same epoch.
AC #7   — Self-improvement workflow queues on canonical event.
AC #8   — Sentinel works without self-improvement env vars.
AC #9   — Pre-existing regression sweep (run separately).

This file focuses on AC #1, #2, #3, #6, #8 — the most readily
unit-testable surfaces. AC #4, #5, #7 are integration shapes that
require a live event stream / workflow runtime and are validated by
post-merge restart + log inspection (per the spec's roll-out plan).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ============================================================
# AC #1, #2 — Alias canonicalization
# ============================================================


class TestToolAliasCanonicalizer:
    """The static canonicalizer is the single source of truth for
    known model hallucinations. Both ingress points (reasoning.py
    execute_tool + gate.py classify_tool_effect) must consult it."""

    def test_known_alias_repairs_to_manage_plan(self):
        from kernos.kernel.tool_aliases import canonicalize_tool_name
        canonical, repaired = canonicalize_tool_name(
            "planning_orchestration.create_plan",
        )
        assert canonical == "manage_plan"
        assert repaired is True

    def test_second_known_alias_repairs_to_manage_plan(self):
        from kernos.kernel.tool_aliases import canonicalize_tool_name
        canonical, repaired = canonicalize_tool_name(
            "workspace_plan_artifact_write",
        )
        assert canonical == "manage_plan"
        assert repaired is True

    def test_namespaced_request_space_action_alias(self):
        """2026-05-22 hallucination: namespaced
        external_code_consultation.request_space_action → real
        request_space_action."""
        from kernos.kernel.tool_aliases import canonicalize_tool_name
        canonical, repaired = canonicalize_tool_name(
            "external_code_consultation.request_space_action",
        )
        assert canonical == "request_space_action"
        assert repaired is True

    def test_repository_inspection_report_alias(self):
        """2026-05-22 hallucination: repository_inspection.report
        → ask_coding_session (closest general-purpose investigator)."""
        from kernos.kernel.tool_aliases import canonicalize_tool_name
        canonical, repaired = canonicalize_tool_name(
            "repository_inspection.report",
        )
        assert canonical == "ask_coding_session"
        assert repaired is True

    def test_autonomous_improvement_start_attempt_alias(self):
        """2026-05-23 improve_kernos soak: dotted-namespace retry
        kernel.autonomous_improvement.start_attempt → improve_kernos."""
        from kernos.kernel.tool_aliases import canonicalize_tool_name
        canonical, repaired = canonicalize_tool_name(
            "kernel.autonomous_improvement.start_attempt",
        )
        assert canonical == "improve_kernos"
        assert repaired is True

    def test_autonomous_improvement_namespace_alias(self):
        """2026-05-23 improve_kernos soak: bare-namespace variant
        kernel.autonomous_improvement → improve_kernos."""
        from kernos.kernel.tool_aliases import canonicalize_tool_name
        canonical, repaired = canonicalize_tool_name(
            "kernel.autonomous_improvement",
        )
        assert canonical == "improve_kernos"
        assert repaired is True

    def test_advisory_spec_retrieval_consult_alias(self):
        """2026-05-24 spec alignment check: agent reached for
        advisory_spec_retrieval_consult to read a repo spec.
        No advisory-variant tool exists; canonical surface is
        plain consult (advisory mode via prompt framing)."""
        from kernos.kernel.tool_aliases import canonicalize_tool_name
        canonical, repaired = canonicalize_tool_name(
            "advisory_spec_retrieval_consult",
        )
        assert canonical == "consult"
        assert repaired is True

    def test_gate_classifies_request_space_action_as_soft_write(self):
        """2026-05-22: request_space_action was missing from the
        gate classification table, causing live-integration dispatcher
        to refuse it as unknown. Now classified as soft_write."""
        from kernos.kernel.gate import DispatchGate
        gate = DispatchGate(
            reasoning_service=None, registry=None, state=None, events=None,
        )
        assert gate.classify_tool_effect(
            "request_space_action", None, None,
        ) == "soft_write"

    def test_canonical_name_passes_through(self):
        from kernos.kernel.tool_aliases import canonicalize_tool_name
        canonical, repaired = canonicalize_tool_name("manage_plan")
        assert canonical == "manage_plan"
        assert repaired is False

    def test_unknown_tool_passes_through(self):
        from kernos.kernel.tool_aliases import canonicalize_tool_name
        canonical, repaired = canonicalize_tool_name("some_random_tool")
        assert canonical == "some_random_tool"
        assert repaired is False

    def test_empty_string_passes_through(self):
        from kernos.kernel.tool_aliases import canonicalize_tool_name
        canonical, repaired = canonicalize_tool_name("")
        assert canonical == ""
        assert repaired is False


class TestAliasRepairReceiptV1:
    """TOOL-ALIAS-RECEIPT-V1 (2026-05-23): every alias repair at the
    dispatch ingress leaves a first-class TOOL_ALIAS_REPAIRED event in
    the stream. Telemetry corpus for the semantic-action-envelope
    redesign."""

    @pytest.mark.asyncio
    async def test_emit_alias_repair_receipt_emits_event(self):
        from unittest.mock import AsyncMock
        from kernos.kernel.tool_aliases import emit_alias_repair_receipt
        from kernos.kernel.event_types import EventType

        events = AsyncMock()
        events.emit = AsyncMock()
        await emit_alias_repair_receipt(
            events,
            instance_id="t1",
            requested="kernel.autonomous_improvement",
            canonical="improve_kernos",
            context="dispatch",
        )
        # emit_event() wraps events.emit; verify emit was called
        # with an Event whose type matches the alias-receipt slot.
        assert events.emit.await_count == 1
        evt = events.emit.await_args.args[0]
        assert evt.type == EventType.TOOL_ALIAS_REPAIRED.value
        assert evt.instance_id == "t1"
        assert evt.payload["requested"] == "kernel.autonomous_improvement"
        assert evt.payload["canonical"] == "improve_kernos"
        assert evt.payload["context"] == "dispatch"
        assert evt.source == "kernel.tool_aliases"

    @pytest.mark.asyncio
    async def test_emit_alias_repair_receipt_handles_none_events(self):
        """Best-effort: None events handle (pre-init / test paths)
        MUST NOT raise."""
        from kernos.kernel.tool_aliases import emit_alias_repair_receipt
        # Just shouldn't raise.
        await emit_alias_repair_receipt(
            None,
            instance_id="t1", requested="x", canonical="y",
            context="dispatch",
        )

    @pytest.mark.asyncio
    async def test_emit_alias_repair_receipt_swallows_emit_failure(self):
        """Per kernel architecture: event emission is best-effort;
        a failure MUST NOT break dispatch."""
        from unittest.mock import AsyncMock
        from kernos.kernel.tool_aliases import emit_alias_repair_receipt

        events = AsyncMock()
        events.emit = AsyncMock(side_effect=RuntimeError("db gone"))
        # Should swallow + log, not raise.
        await emit_alias_repair_receipt(
            events,
            instance_id="t1", requested="x", canonical="y",
            context="dispatch",
        )

    def test_reasoning_dispatch_emits_receipt_ast_guard(self):
        """AST guard: reasoning.execute_tool's alias-repair block
        MUST call emit_alias_repair_receipt. Catches a future
        deletion of the receipt wiring."""
        src = (_REPO_ROOT / "kernos/kernel/reasoning.py").read_text(
            encoding="utf-8",
        )
        assert "emit_alias_repair_receipt" in src, (
            "reasoning.py lost its alias-receipt emission — "
            "TOOL-ALIAS-RECEIPT-V1 wiring missing"
        )


class TestAliasRepairIngressPoints:
    """AST guards: the two ingress points named in the spec
    (reasoning.py execute_tool, gate.py classify_tool_effect) must
    each contain a call to canonicalize_tool_name. Catches a future
    deletion of either wiring."""

    @staticmethod
    def _source(rel_path: str) -> ast.Module:
        return ast.parse(
            (_REPO_ROOT / rel_path).read_text(encoding="utf-8"),
        )

    @staticmethod
    def _find_func_calls_canonicalize(
        tree: ast.Module, target_func_name: str,
    ) -> bool:
        """Return True iff a function/method named target_func_name in
        the given module body contains a call to canonicalize_tool_name."""
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                continue
            if node.name != target_func_name:
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                func = sub.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "canonicalize_tool_name"
                ):
                    return True
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "canonicalize_tool_name"
                ):
                    return True
        return False

    def test_reasoning_execute_tool_calls_canonicalizer(self):
        tree = self._source("kernos/kernel/reasoning.py")
        assert self._find_func_calls_canonicalize(
            tree, "execute_tool",
        ), (
            "reasoning.py:execute_tool must call canonicalize_tool_name "
            "at the top so kernel-tool dispatch sees the canonical name. "
            "Without it, model hallucinations of manage_plan never reach "
            "the handler."
        )

    def test_gate_classify_tool_effect_calls_canonicalizer(self):
        tree = self._source("kernos/kernel/gate.py")
        assert self._find_func_calls_canonicalize(
            tree, "classify_tool_effect",
        ), (
            "gate.py:classify_tool_effect must call canonicalize_tool_name "
            "at the top. Live dispatch classifies before calling "
            "execute_tool; without repair here the gate returns 'unknown' "
            "and the live dispatcher refuses before reasoning ever sees "
            "the call."
        )


# ============================================================
# AC #3 — Sentinel YAML loads + trigger compiles
# ============================================================


class TestLoopHealthYamlLoads:
    """Codex round-3 sanity check: the loop_health.workflow.yaml must
    parse via the existing workflow loader, validate, and compile one
    trigger predicate. Without this, the sentinel can't register and
    the boot_probe goes nowhere."""

    def test_yaml_file_exists(self):
        path = _REPO_ROOT / "specs/workflows/loop_health.workflow.yaml"
        assert path.exists(), (
            f"loop_health.workflow.yaml missing at {path}"
        )

    def test_yaml_parses_and_compiles_one_trigger(self):
        import yaml
        from kernos.kernel.workflows.descriptor_parser import _build_workflow
        from kernos.kernel.workflows.workflow_registry import validate_workflow
        from kernos.kernel.triggers import compile_descriptor_triggers

        path = _REPO_ROOT / "specs/workflows/loop_health.workflow.yaml"
        raw = path.read_text(encoding="utf-8")
        descriptor = yaml.safe_load(raw)
        # Production passes a real instance_id; for the load test we
        # substitute the placeholder directly so build/validate succeed.
        descriptor["instance_id"] = "test_instance"
        # Same substitution for the trigger's event_selector operand.
        for trigger in descriptor.get("triggers", []):
            selector = trigger.get("event_selector", {})
            for operand in selector.get("operands", []):
                if (
                    operand.get("path") == "instance_id"
                    and operand.get("value") == "{installer.instance_id}"
                ):
                    operand["value"] = "test_instance"
        workflow = _build_workflow(descriptor)
        validate_workflow(workflow)
        compiled = compile_descriptor_triggers(
            workflow_id=workflow.workflow_id, descriptor=descriptor,
        )
        assert len(compiled) == 1, (
            f"Expected exactly one compiled trigger; got {len(compiled)}"
        )


# ============================================================
# AC #6 — Active-frequency threshold-crossing emission + dedup
# ============================================================


class TestActiveFrequencyThresholdCrossing:
    """The new emission path: record_occurrence on an active pattern
    fires friction.pattern_active_frequency_threshold_crossed once
    when count transitions from < reactivation_threshold to >= it.
    Does NOT re-emit on subsequent occurrences in the same epoch
    (downstream dedupe via active_epoch handles cross-restart cases)."""

    @pytest.mark.asyncio
    async def test_threshold_crossing_emits_event(self, tmp_path):
        from kernos.kernel.friction_patterns import FrictionPatternStore

        captured: list[tuple[str, dict]] = []

        async def _capture_emit(event_type, payload):
            captured.append((event_type, payload))

        store = FrictionPatternStore()
        await store.start(str(tmp_path))

        await store.create_pattern(
            instance_id="test_inst",
            display_name="test pattern",
            description="x" * 25,  # min length per validator
            seed_slug="test-pattern",
            signal_type_keys=["TEST_SIGNAL"],
            reactivation_threshold=3,
        )

        # First two occurrences: below threshold, no emission.
        for i in range(2):
            await store.record_occurrence(
                instance_id="test_inst",
                pattern_id="test-pattern",
                observed_at="",
                report_path=f"path_{i}.md",
                emit_event=_capture_emit,
            )
        assert captured == [], (
            f"Occurrences below threshold should not emit; got {captured}"
        )

        # Third occurrence: crosses 3 → emission fires exactly once.
        await store.record_occurrence(
            instance_id="test_inst",
            pattern_id="test-pattern",
            observed_at="",
            report_path="path_2.md",
            emit_event=_capture_emit,
        )
        assert len(captured) == 1, (
            f"Crossing the threshold should emit exactly one event; "
            f"got {captured}"
        )
        event_type, payload = captured[0]
        assert event_type == "friction.pattern_active_frequency_threshold_crossed"
        assert payload["pattern_id"] == "test-pattern"
        assert payload["count"] == 3
        assert payload["reactivation_threshold"] == 3
        assert "active_epoch" in payload

        # Fourth occurrence: already past threshold, no re-emission.
        await store.record_occurrence(
            instance_id="test_inst",
            pattern_id="test-pattern",
            observed_at="",
            report_path="path_3.md",
            emit_event=_capture_emit,
        )
        assert len(captured) == 1, (
            f"Subsequent occurrences should not re-emit; got {captured}"
        )

        await store.stop()

    @pytest.mark.asyncio
    async def test_no_emit_when_emit_event_not_supplied(self, tmp_path):
        """Backward-compat: callers that don't pass emit_event get
        the original no-emit behavior."""
        from kernos.kernel.friction_patterns import FrictionPatternStore

        store = FrictionPatternStore()
        await store.start(str(tmp_path))
        await store.create_pattern(
            instance_id="test_inst",
            display_name="test pattern",
            description="x" * 25,
            seed_slug="test-pattern-2",
            signal_type_keys=["TEST_SIGNAL"],
            reactivation_threshold=2,
        )
        # Three occurrences without emit_event — must not raise.
        for i in range(3):
            await store.record_occurrence(
                instance_id="test_inst",
                pattern_id="test-pattern-2",
                observed_at="",
                report_path=f"x_{i}.md",
            )
        await store.stop()


# ============================================================
# AC #8 — Sentinel works without self-improvement env vars
# ============================================================


class TestSentinelDoesNotDependOnSelfImprovementEnv:
    """The synthetic substrate architect lets the sentinel register
    even when KERNOS_ARCHITECT_ACTOR_ID is unset. Self-improvement
    silently skips in that case; the sentinel must NOT."""

    def test_sentinel_architect_is_architect_kind(self):
        from kernos.kernel.workflows.loop_health_helper import (
            _SENTINEL_ARCHITECT,
        )
        from kernos.kernel.workflows.authoring import ACTOR_ARCHITECT
        assert _SENTINEL_ARCHITECT.actor_kind == ACTOR_ARCHITECT
        assert _SENTINEL_ARCHITECT.actor_id == "substrate.loop_health_sentinel"
        # is_architect() helper returns True so authoring layer accepts it
        assert _SENTINEL_ARCHITECT.is_architect()

    def test_substrate_architect_env_override_round_trips(self, monkeypatch):
        """Codex round 2 finding 1: the env-override context manager
        must restore the prior env value (or absence) on exit, so
        other architect-gated paths see the original world."""
        import os
        from kernos.kernel.workflows.loop_health_helper import (
            _substrate_architect_env_override,
        )

        # Case 1: env unset before, must be absent again after
        monkeypatch.delenv("KERNOS_ARCHITECT_ACTOR_ID", raising=False)
        with _substrate_architect_env_override():
            assert (
                os.environ.get("KERNOS_ARCHITECT_ACTOR_ID")
                == "substrate.loop_health_sentinel"
            )
        assert "KERNOS_ARCHITECT_ACTOR_ID" not in os.environ

        # Case 2: env set to operator architect identity before, must
        # be restored to original value after
        monkeypatch.setenv("KERNOS_ARCHITECT_ACTOR_ID", "alice@example")
        with _substrate_architect_env_override():
            assert (
                os.environ["KERNOS_ARCHITECT_ACTOR_ID"]
                == "substrate.loop_health_sentinel"
            )
        assert os.environ["KERNOS_ARCHITECT_ACTOR_ID"] == "alice@example"

    def test_is_architect_passes_for_sentinel_inside_override(
        self, monkeypatch,
    ):
        """Pin the load-bearing semantics: while the override is
        active, the substrate's fail-closed _is_architect check
        accepts the sentinel actor. Without this, register_workflow
        rejects + the sentinel never comes up."""
        from kernos.kernel.workflows.authoring import _is_architect
        from kernos.kernel.workflows.loop_health_helper import (
            _SENTINEL_ARCHITECT, _substrate_architect_env_override,
        )

        monkeypatch.delenv("KERNOS_ARCHITECT_ACTOR_ID", raising=False)
        # Outside the override → fail-closed
        assert _is_architect(_SENTINEL_ARCHITECT) is False
        # Inside the override → accepted
        with _substrate_architect_env_override():
            assert _is_architect(_SENTINEL_ARCHITECT) is True
        # Restored
        assert _is_architect(_SENTINEL_ARCHITECT) is False


class TestBootIdGenerator:
    """Codex round 2 finding 3: boot_id must include sub-second
    distinguisher so fast-restart / crash-loop cases don't produce
    duplicate boot_ids."""

    def test_boot_id_includes_time_ns_suffix(self):
        from kernos.kernel.workflows.loop_health_helper import (
            _generate_boot_id,
        )
        boot_id_1 = _generate_boot_id()
        boot_id_2 = _generate_boot_id()
        # Same UTC second is likely; time_ns suffix must differ
        assert boot_id_1 != boot_id_2, (
            "Two boot_ids generated back-to-back should differ "
            "(sub-second resolution via time_ns suffix)."
        )
        # Format check: contains a "-" separator and digits after it
        assert "-" in boot_id_1
        prefix, suffix = boot_id_1.rsplit("-", 1)
        assert suffix.isdigit(), (
            f"boot_id suffix {suffix!r} must be a time_ns integer"
        )


# Behavior tests for alias canonicalization at the actual call sites
# (Codex round 2: previous tests were AST-only; behavior tests pin
# the load-bearing semantics).


class TestCompletionLoggerSubscribesAndFires:
    """Codex round 3 finding: the completion logger uses the actual
    event-stream API (register_post_flush_hook). This test asserts
    the subscription happens AND fires LOOP_HEALTH_EXECUTION_COMPLETED
    when a terminated event arrives."""

    def test_completion_logger_registers_and_fires(self, caplog):
        import logging
        from kernos.kernel.workflows.loop_health_helper import (
            register_completion_logger,
        )

        class _StubEvent:
            def __init__(self, event_type, instance_id, payload):
                self.event_type = event_type
                self.instance_id = instance_id
                self.payload = payload

        captured_hooks = []

        class _StubEventStream:
            def register_post_flush_hook(self, hook):
                captured_hooks.append(hook)

        stream = _StubEventStream()
        register_completion_logger(
            event_stream=stream,
            instance_id="test_inst",
            boot_id="boot-12345",
        )
        assert len(captured_hooks) == 1, (
            "register_completion_logger must subscribe exactly one "
            "post-flush hook via register_post_flush_hook."
        )

        hook = captured_hooks[0]
        caplog.set_level(
            logging.INFO,
            logger="kernos.kernel.workflows.loop_health_helper",
        )
        import asyncio
        asyncio.run(hook([
            _StubEvent(
                event_type="workflow.execution_terminated",
                instance_id="test_inst",
                payload={
                    "workflow_id": "loop_health",
                    "outcome": "completed",
                },
            ),
        ]))
        completion_logs = [
            r for r in caplog.records
            if "LOOP_HEALTH_EXECUTION_COMPLETED" in r.getMessage()
        ]
        assert len(completion_logs) == 1, (
            f"Expected exactly one LOOP_HEALTH_EXECUTION_COMPLETED "
            f"log line; got {len(completion_logs)}"
        )
        assert "boot_id=boot-12345" in completion_logs[0].getMessage()


class TestAliasCanonicalizationBehavior:
    """The canonicalizer must actually repair at runtime. Calling
    the gate's classify_tool_effect or the canonicalizer directly
    with an alias must produce the canonical name."""

    def test_gate_classify_returns_manage_plan_for_alias(self):
        """End-to-end: classify_tool_effect(alias, ...) returns the
        same classification it would return for manage_plan, not
        'unknown'."""
        from kernos.kernel.gate import DispatchGate

        # Minimal gate construction — registry can be None for kernel-tool
        # classification (manage_plan is in the action-dependent
        # branches at gate.py).
        gate = DispatchGate(
            reasoning_service=None,
            registry=None,
            state=None,
            events=None,
        )
        # Pretend "create" action so manage_plan resolves to soft_write
        canonical_eff = gate.classify_tool_effect(
            "manage_plan", None, {"action": "create"},
        )
        aliased_eff = gate.classify_tool_effect(
            "planning_orchestration.create_plan", None,
            {"action": "create"},
        )
        assert aliased_eff == canonical_eff, (
            f"Aliased classification ({aliased_eff!r}) must match "
            f"canonical classification ({canonical_eff!r}). Without "
            f"this, live dispatch refuses aliased calls as 'unknown'."
        )
        # Second alias
        aliased_eff_2 = gate.classify_tool_effect(
            "workspace_plan_artifact_write", None,
            {"action": "create"},
        )
        assert aliased_eff_2 == canonical_eff


# ============================================================
# Autonomy emitter accepts the new event type
# ============================================================


class TestAutonomyEmitterTranslatesNewEvent:
    """The FrictionPatternFrequencyEmitter must translate BOTH
    friction.pattern_reactivated AND
    friction.pattern_active_frequency_threshold_crossed into the
    canonical friction.pattern_frequency_threshold_exceeded event."""

    def test_emitter_source_contains_new_event_type(self):
        """AST/source guard: the new event type literal must appear in
        the emitter's translation logic. A regression that drops it
        would silently break the active-pattern path again."""
        src = (
            _REPO_ROOT / "kernos/kernel/workflows/autonomy_emitters.py"
        ).read_text(encoding="utf-8")
        assert "friction.pattern_active_frequency_threshold_crossed" in src, (
            "autonomy_emitters.py must reference the new event type "
            "so it translates active-frequency crossings into the "
            "canonical workflow trigger event. Dropping this string "
            "silently breaks the self-improvement loop's active-pattern "
            "path."
        )
        assert "friction.pattern_reactivated" in src, (
            "The reactivated translation path must remain — both "
            "paths feed the canonical event."
        )
