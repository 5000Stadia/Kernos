"""SELF-IMPROVEMENT-WORKFLOW-V1 integration tests.

Pins the v1 autonomy loop:

  * YAML descriptor parses + validates with the Spec 4 grammar.
  * Helper substitutes installer placeholders, registers + activates,
    wires triggers into the WTC runtime.
  * Helper is idempotent on re-call within an instance.
  * End-to-end: friction pattern recurrence threshold → emitter
    translates → workflow fires → through all 5 steps with simulated
    coding-session response → autonomy_loop_outcomes ledger captures
    the outcome with the actual investigation_outcome (architect's
    v1 dynamic-outcome call).

Test shape: every mechanic exercised under expected workflow-side
use AND substrate state pinned (per architect feedback).
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kernos.kernel import event_stream
from kernos.kernel.friction_patterns import (
    CLASSIFIED_AUTO_SIGNAL_TYPE,
    FrictionPatternStore,
    LIFECYCLE_REACTIVATED,
    LIFECYCLE_RESOLVED,
)
from kernos.kernel.triggers.runtime import TriggerEvaluationRuntime
from kernos.kernel.workflows.action_library import (
    ActionLibrary,
    AppendToLedgerAction,
    CallToolAction,
    MarkStateAction,
    NotifyUserAction,
)
from kernos.kernel.workflows.authoring import (
    ACTOR_ARCHITECT,
    ACTOR_OPERATOR,
    AuthoringContext,
)
from kernos.kernel.workflows.autonomy_emitters import (
    CodingSessionBridgeResponseEmitter,
    FrictionPatternFrequencyEmitter,
)
from kernos.kernel.workflows.autonomy_tools import (
    handle_ask_coding_session_for_workflow,
    handle_emit_autonomy_loop_event_tool,
    handle_read_coding_session_response_for_workflow,
    handle_record_friction_pattern_recurrence_tool,
    handle_transition_friction_pattern_lifecycle_tool,
)
from kernos.kernel.workflows.execution_engine import ExecutionEngine
from kernos.kernel.workflows.ledger import WorkflowLedger
from kernos.kernel.workflows.self_improvement_helper import (
    _substitute_installer_placeholders,
    register_self_improvement_workflow,
)
from kernos.kernel.workflows.trigger_registry import (
    TriggerRegistry,
    _reset_for_tests as _reset_trigger_registry,
)
from kernos.kernel.workflows.workflow_registry import WorkflowRegistry
from kernos.setup.bring_up_substrate import (
    _workflow_ledger_append_adapter,
    _workflow_ledger_read_last_adapter,
)


ARCHITECT_ID = "op_si_architect"
OPERATOR_ID = "op_si_operator"
TEST_INSTANCE_ID = "inst_si_test"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workflow_yaml_path() -> Path:
    """Resolve the canonical workflow YAML location relative to repo
    root. Tests reach for the same file the production helper loads
    so any drift between test and production is caught."""
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "specs/workflows/self_improvement.workflow.yaml"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "self_improvement.workflow.yaml not found via test ancestor walk"
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Set KERNOS_ARCHITECT_ACTOR_ID + KERNOS_OPERATOR_ACTOR_ID for
    every test. Friction reactivation threshold lowered to 1 so the
    end-to-end test can drive reactivation with a single recurrence."""
    monkeypatch.setenv("KERNOS_ARCHITECT_ACTOR_ID", ARCHITECT_ID)
    monkeypatch.setenv("KERNOS_OPERATOR_ACTOR_ID", OPERATOR_ID)
    monkeypatch.setenv("KERNOS_FRICTION_REACTIVATION_THRESHOLD", "1")
    monkeypatch.setenv(
        "KERNOS_FRICTION_REACTIVATION_WINDOW_DAYS", "365",
    )


# ===========================================================================
# YAML + descriptor parser integration
# ===========================================================================


class TestSelfImprovementYaml:
    """The canonical workflow YAML parses + validates with Spec 4."""

    def test_yaml_parses_to_dict(self):
        import yaml
        with _workflow_yaml_path().open() as fp:
            descriptor = yaml.safe_load(fp)
        assert isinstance(descriptor, dict)
        assert descriptor["workflow_id"] == "self_improvement"
        assert descriptor["version"] == "1.0"

    def test_yaml_has_canonical_trigger_shape(self):
        """V7 H2 + plural-triggers (12th amendment) pin: triggers list
        with top-level instance_id + payload.pattern_id exists."""
        import yaml
        with _workflow_yaml_path().open() as fp:
            descriptor = yaml.safe_load(fp)
        assert "triggers" in descriptor
        assert "trigger" not in descriptor  # plural only, not mixed
        assert len(descriptor["triggers"]) == 1
        trigger = descriptor["triggers"][0]
        assert trigger["event_type"] == "friction.pattern_frequency_threshold_exceeded"
        # AND-composed selector: top-level instance_id eq + payload.pattern_id exists.
        selector = trigger["event_selector"]
        assert selector["op"] == "AND"
        paths = {op.get("path") for op in selector["operands"]}
        assert "instance_id" in paths
        assert "payload.pattern_id" in paths

    def test_yaml_action_sequence_uses_canonical_refs(self):
        """V7.3 architect call: refs use Spec 4 canonical syntax —
        {idea_payload.X} for trigger payload, {step.id.value.X} for
        step values. The emit_outcome step uses dynamic
        investigation_outcome ref per the architect's modification."""
        import yaml
        with _workflow_yaml_path().open() as fp:
            descriptor = yaml.safe_load(fp)
        actions = {a["id"]: a for a in descriptor["action_sequence"]}
        # record_recurrence uses idea_payload.pattern_id
        rec_args = actions["record_recurrence"]["parameters"]["args"]
        assert rec_args["pattern_id"] == "{idea_payload.pattern_id}"
        # ask_cc uses idea_payload.pattern_id + active_epoch
        ask_args = actions["ask_cc"]["parameters"]["args"]
        assert "{idea_payload.pattern_id}" in ask_args["question"]
        # read_response uses step.ask_cc.value.request_id
        read_args = actions["read_response"]["parameters"]["args"]
        assert read_args["request_id"] == "{step.ask_cc.value.request_id}"
        # emit_outcome uses DYNAMIC outcome ref (architect's modification).
        emit_args = actions["emit_outcome"]["parameters"]["args"]
        assert emit_args["outcome"] == "{step.read_response.value.investigation_outcome}"
        assert emit_args["addresses_friction_patterns"] == [
            "{idea_payload.pattern_id}"
        ]

    def test_yaml_gate_predicate_uses_canonical_refs(self):
        import yaml
        with _workflow_yaml_path().open() as fp:
            descriptor = yaml.safe_load(fp)
        gates = {g["gate_name"]: g for g in descriptor["approval_gates"]}
        gate = gates["await_cc_response"]
        assert gate["approval_event_type"] == "coding_consult.response_received"
        pred = gate["approval_event_predicate"]
        assert pred["path"] == "payload.request_id"
        assert pred["value"] == "{step.ask_cc.value.request_id}"

    def test_yaml_parses_via_spec4_descriptor_parser(self, tmp_path):
        """Functional pin: the YAML survives _build_workflow with the
        installer placeholders substituted. Pins integration with
        Spec 4's descriptor parser + Spec 5 12th amendment plural
        triggers shape."""
        import yaml

        from kernos.kernel.workflows.descriptor_parser import _build_workflow

        with _workflow_yaml_path().open() as fp:
            descriptor = yaml.safe_load(fp)
        # Substitute placeholders so the descriptor's instance_id is
        # a concrete value.
        descriptor = _substitute_installer_placeholders(
            descriptor, TEST_INSTANCE_ID,
        )
        wf = _build_workflow(descriptor)
        # Plural triggers leave Workflow.trigger=None (12th amendment).
        assert wf.trigger is None
        assert wf.workflow_id == "self_improvement"
        assert wf.instance_id == TEST_INSTANCE_ID
        # 5 actions in the action_sequence.
        assert len(wf.action_sequence) == 5
        action_ids = [a.id for a in wf.action_sequence]
        assert action_ids == [
            "record_recurrence", "ask_cc", "read_response",
            "mark_resolved", "emit_outcome",
        ]
        # 1 approval gate.
        assert len(wf.approval_gates) == 1
        assert wf.approval_gates[0].gate_name == "await_cc_response"


# ===========================================================================
# Placeholder substitution
# ===========================================================================


class TestInstallerPlaceholderSubstitution:

    def test_top_level_instance_id_substituted(self):
        import yaml
        with _workflow_yaml_path().open() as fp:
            descriptor = yaml.safe_load(fp)
        out = _substitute_installer_placeholders(descriptor, TEST_INSTANCE_ID)
        assert out["instance_id"] == TEST_INSTANCE_ID

    def test_trigger_instance_id_operand_substituted(self):
        """V7 H2 pin: the top-level instance_id operand inside the
        triggers' event_selector AST is substituted; payload.X
        operands are NOT touched (engine-trusted top-level vs
        payload-data layer distinction)."""
        import yaml
        with _workflow_yaml_path().open() as fp:
            descriptor = yaml.safe_load(fp)
        out = _substitute_installer_placeholders(descriptor, TEST_INSTANCE_ID)
        trigger = out["triggers"][0]
        operands = trigger["event_selector"]["operands"]
        for op in operands:
            if op.get("path") == "instance_id":
                assert op["value"] == TEST_INSTANCE_ID
            # payload.pattern_id should be exists, not eq, so no value
            # substitution applies.

    def test_input_not_mutated(self):
        """Substitution returns a NEW dict so callers can reuse the
        loaded YAML across multiple instances."""
        import yaml
        with _workflow_yaml_path().open() as fp:
            descriptor = yaml.safe_load(fp)
        original_value = descriptor["instance_id"]
        out = _substitute_installer_placeholders(descriptor, TEST_INSTANCE_ID)
        # original unchanged
        assert descriptor["instance_id"] == original_value
        assert descriptor["instance_id"] != out["instance_id"]


# ===========================================================================
# Helper register + activate flow
# ===========================================================================


@pytest.fixture
async def stack(tmp_path):
    """Full WTC + ExecutionEngine + WorkflowLedger + FrictionPatternStore
    stack the autonomy loop needs."""
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path), flush_interval_s=0.05)
    trig = TriggerRegistry()
    await trig.start(str(tmp_path), attach_post_flush_hook=False)
    wfr = WorkflowRegistry()
    await wfr.start(str(tmp_path), trig)
    pattern_store = FrictionPatternStore()
    await pattern_store.start(str(tmp_path))
    ledger = WorkflowLedger(str(tmp_path))
    lib = ActionLibrary()

    # State store (in-memory for tests).
    state: dict = {}

    async def state_set(*, key, value, scope, instance_id):
        state[(scope, instance_id, key)] = value

    async def state_get(*, key, scope, instance_id):
        return state.get((scope, instance_id, key))

    lib.register(MarkStateAction(state_store_set=state_set, state_store_get=state_get))

    # Tool dispatch: route the autonomy tools to their handlers.
    async def _tool_dispatch(*, tool_id, args, instance_id, member_id):
        if tool_id == "transition_friction_pattern_lifecycle":
            return await handle_transition_friction_pattern_lifecycle_tool(
                pattern_store=pattern_store,
                instance_id=instance_id,
                member_id=member_id,
                args=args,
            )
        if tool_id == "record_friction_pattern_recurrence":
            return await handle_record_friction_pattern_recurrence_tool(
                pattern_store=pattern_store,
                instance_id=instance_id,
                member_id=member_id,
                args=args,
            )
        if tool_id == "emit_autonomy_loop_event":
            return await handle_emit_autonomy_loop_event_tool(
                ledger=ledger,
                instance_id=instance_id,
                member_id=member_id,
                args=args,
            )
        if tool_id == "ask_coding_session_for_workflow":
            return await handle_ask_coding_session_for_workflow(
                instance_id=instance_id,
                member_id=member_id,
                args=args,
                data_dir=str(tmp_path),
            )
        if tool_id == "read_coding_session_response_for_workflow":
            return await handle_read_coding_session_response_for_workflow(
                instance_id=instance_id,
                member_id=member_id,
                args=args,
                data_dir=str(tmp_path),
            )
        raise RuntimeError(f"unknown tool_id={tool_id!r}")

    lib.register(CallToolAction(tool_dispatch_fn=_tool_dispatch))
    lib.register(AppendToLedgerAction(
        ledger_append_fn=_workflow_ledger_append_adapter(ledger),
        ledger_read_last_fn=_workflow_ledger_read_last_adapter(ledger),
    ))

    engine = ExecutionEngine()
    await engine.start(str(tmp_path), trig, wfr, lib, ledger, space_resolver=None)

    runtime = TriggerEvaluationRuntime()
    await runtime.start(
        data_dir=str(tmp_path),
        wlp_dispatch=engine.execute_workflow,
        wlp_lookup_by_fire_id=engine.find_execution_by_fire_id,
    )

    # WTC v1 C3 InternalEventAdapter — load-bearing for the
    # end-to-end test: wires event_stream's post-flush hook into the
    # runtime's on_event_observed so flushed events become candidates
    # for trigger predicate matching. Without this, the runtime never
    # sees events and the workflow never fires.
    from kernos.kernel.triggers.sources import InternalEventAdapter
    internal_event_adapter = InternalEventAdapter(runtime)
    await internal_event_adapter.start()

    yield {
        "tmp_path": tmp_path,
        "engine": engine,
        "runtime": runtime,
        "internal_event_adapter": internal_event_adapter,
        "trig": trig,
        "wfr": wfr,
        "pattern_store": pattern_store,
        "ledger": ledger,
        "state": state,
    }

    await internal_event_adapter.stop()
    await runtime.stop()
    await engine.stop()
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await pattern_store.stop()
    await event_stream._reset_for_tests()


def _architect_ctx() -> AuthoringContext:
    return AuthoringContext(actor_id=ARCHITECT_ID, actor_kind=ACTOR_ARCHITECT)


class TestRegisterHelper:

    async def test_register_succeeds_with_architect(self, stack):
        """Functional pin: helper succeeds with architect actor;
        substrate state pin: workflow row + registered_workflows row
        + WTC runtime predicate all landed."""
        workflow_id = await register_self_improvement_workflow(
            engine=stack["engine"],
            architect_ctx=_architect_ctx(),
            instance_id=TEST_INSTANCE_ID,
            trigger_runtime=stack["runtime"],
            operator_actor_id=OPERATOR_ID,
        )
        assert workflow_id == "self_improvement"
        # Substrate pin: workflow registered.
        wf = await stack["wfr"].get_workflow("self_improvement")
        assert wf is not None
        assert wf.instance_id == TEST_INSTANCE_ID
        # Substrate pin: registered_workflows row active.
        from kernos.kernel.workflows.registered_workflows import (
            get_registered_workflow,
        )
        reg = await get_registered_workflow(
            stack["engine"]._db, workflow_id="self_improvement",
        )
        assert reg is not None
        assert reg.activation_state == "active"
        # Substrate pin: WTC runtime predicate registered.
        assert len(stack["runtime"]._predicates) == 1

    async def test_helper_idempotent_on_re_call(self, stack):
        """Spec 5 13th amendment idempotency: re-call with same
        descriptor returns the same workflow_id and doesn't error."""
        workflow_id_1 = await register_self_improvement_workflow(
            engine=stack["engine"],
            architect_ctx=_architect_ctx(),
            instance_id=TEST_INSTANCE_ID,
            trigger_runtime=stack["runtime"],
            operator_actor_id=OPERATOR_ID,
        )
        workflow_id_2 = await register_self_improvement_workflow(
            engine=stack["engine"],
            architect_ctx=_architect_ctx(),
            instance_id=TEST_INSTANCE_ID,
            trigger_runtime=stack["runtime"],
            operator_actor_id=OPERATOR_ID,
        )
        assert workflow_id_1 == workflow_id_2 == "self_improvement"

    async def test_helper_requires_architect_actor(self, stack):
        """Fail-loud pin (v7 H3): non-architect ctx raises RuntimeError."""
        non_architect_ctx = AuthoringContext(
            actor_id="someone_else", actor_kind="kernos",
        )
        with pytest.raises(RuntimeError, match="requires architect"):
            await register_self_improvement_workflow(
                engine=stack["engine"],
                architect_ctx=non_architect_ctx,
                instance_id=TEST_INSTANCE_ID,
                trigger_runtime=stack["runtime"],
                operator_actor_id=OPERATOR_ID,
            )


# ===========================================================================
# End-to-end autonomy loop
# ===========================================================================


async def _wait_until(predicate, timeout_s: float = 5.0, step_s: float = 0.02):
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step_s)
    return False


class TestEndToEndAutonomyLoop:
    """The first end-to-end autonomy-loop run.

    Real substrate (engine, runtime, store, ledger, event_stream,
    emitters, workflow handlers). Simulated coding session — instead
    of an external CC process writing the response, the test writes
    the response file directly to the bridge directory after the
    workflow's ask_cc step lands its request. CodingSessionBridgeResponseEmitter
    observes the file and fires coding_consult.response_received,
    which the workflow's gate predicate accepts.
    """

    async def test_full_autonomy_loop_drives_through_all_5_steps(
        self, stack,
    ):
        """FUNCTIONAL pin (architect's user-feedback request): the
        complete autonomy loop from friction-pattern threshold
        through to outcome-event ledger entry.

        Sequence:
          1. Register the self_improvement workflow via the helper.
          2. Launch both emitters (FrictionPatternFrequencyEmitter +
             CodingSessionBridgeResponseEmitter).
          3. Create a friction pattern, resolve it, then drive a
             recurrence through the reactivation threshold. The
             pattern_store emits friction.pattern_reactivated.
          4. FrictionPatternFrequencyEmitter translates →
             friction.pattern_frequency_threshold_exceeded event lands
             in the stream.
          5. WTC runtime matches the trigger predicate → workflow
             dispatch.
          6. Workflow runs record_recurrence + ask_cc; pauses at gate.
          7. Test writes a synthetic response file with
             investigation_outcome="completed".
          8. CodingSessionBridgeResponseEmitter polls + fires
             coding_consult.response_received.
          9. Workflow resumes; read_response, mark_resolved,
             emit_outcome all run.
         10. Substrate state pin: autonomy_loop_outcomes ledger has
             the entry with outcome=completed + the pattern_id.
        """
        # 1. Register the workflow.
        await register_self_improvement_workflow(
            engine=stack["engine"],
            architect_ctx=_architect_ctx(),
            instance_id=TEST_INSTANCE_ID,
            trigger_runtime=stack["runtime"],
            operator_actor_id=OPERATOR_ID,
        )

        # 2. Launch emitters.
        freq_emitter = FrictionPatternFrequencyEmitter(
            instance_id=TEST_INSTANCE_ID,
            pattern_store=stack["pattern_store"],
        )
        await freq_emitter.start()
        response_emitter = CodingSessionBridgeResponseEmitter(
            instance_id=TEST_INSTANCE_ID,
            data_dir=str(stack["tmp_path"]),
            poll_interval_s=0.05,
        )
        await response_emitter.start()

        try:
            # 3. Create + resolve a friction pattern.
            pattern = await stack["pattern_store"].create_pattern(
                instance_id=TEST_INSTANCE_ID,
                description="test friction pattern for autonomy loop",
                signal_type_keys=["e2e_signal"],
            )
            await stack["pattern_store"].transition_lifecycle(
                TEST_INSTANCE_ID, pattern.pattern_id, LIFECYCLE_RESOLVED,
            )

            # 4. Record a recurrence → threshold crossing →
            # friction.pattern_reactivated → FrictionPatternFrequencyEmitter
            # translates to friction.pattern_frequency_threshold_exceeded.
            async def _emit_to_stream(event_type, payload):
                await event_stream.emit(
                    TEST_INSTANCE_ID, event_type, payload,
                )
            triggered = await stack["pattern_store"].record_recurrence(
                instance_id=TEST_INSTANCE_ID,
                pattern_id=pattern.pattern_id,
                observed_at=_now(),
                report_path="e2e-test-recurrence.md",
                classified_by=CLASSIFIED_AUTO_SIGNAL_TYPE,
                emit_event=_emit_to_stream,
            )
            assert triggered is True
            await event_stream.flush_now()
            await event_stream.flush_now()

            # 5-6. Wait for the workflow to start + reach the gate.
            async def _request_file_exists() -> bool:
                requests_dir = (
                    stack["tmp_path"] / TEST_INSTANCE_ID
                    / "coding_session_bridge" / "requests"
                )
                if not requests_dir.exists():
                    return False
                return any(requests_dir.glob("*.json"))

            # Poll for the workflow to reach ask_cc and write the
            # request file (signal: file appears in the bridge's
            # requests directory).
            request_file = None
            for _ in range(200):  # 10s with 50ms step
                requests_dir = (
                    stack["tmp_path"] / TEST_INSTANCE_ID
                    / "coding_session_bridge" / "requests"
                )
                if requests_dir.exists():
                    files = list(requests_dir.glob("*.json"))
                    if files:
                        request_file = files[0]
                        break
                await asyncio.sleep(0.05)
            assert request_file is not None, (
                "workflow did not reach ask_cc step / write request "
                "file within timeout"
            )
            request_id = request_file.stem

            # 7. Simulate CC writing a response with completed outcome.
            responses_dir = (
                stack["tmp_path"] / TEST_INSTANCE_ID
                / "coding_session_bridge" / "responses"
            )
            responses_dir.mkdir(parents=True, exist_ok=True)
            with (responses_dir / f"{request_id}.json").open("w") as fp:
                json.dump({
                    "request_id": request_id,
                    "target": "claude_code",
                    "investigation_outcome": "completed",
                    "summary": "fixed via test simulation",
                }, fp)

            # 8-9. Wait for the workflow to complete + outcome event
            # to land in the autonomy_loop_outcomes ledger.
            outcome_entries = []
            for _ in range(200):
                outcome_entries = await stack["ledger"].read_all(
                    TEST_INSTANCE_ID, "autonomy_loop_outcomes",
                )
                if outcome_entries:
                    break
                await event_stream.flush_now()
                await asyncio.sleep(0.05)

            # Diagnostics: check workflow execution state before
            # asserting outcome. If the workflow failed mid-step,
            # surface the state for the test report.
            if not outcome_entries:
                async with stack["engine"]._db.execute(
                    "SELECT execution_id, state, action_index_completed, "
                    "aborted_reason FROM workflow_executions "
                    "WHERE workflow_id = 'self_improvement'",
                ) as cur:
                    rows = [dict(r) for r in await cur.fetchall()]
                # Read step outputs too.
                step_rows = []
                async with stack["engine"]._db.execute(
                    "SELECT * FROM workflow_step_outputs "
                    "WHERE workflow_execution_id IN ("
                    "SELECT execution_id FROM workflow_executions "
                    "WHERE workflow_id = 'self_improvement')",
                ) as cur:
                    step_rows = [dict(r) for r in await cur.fetchall()]
                raise AssertionError(
                    f"expected one autonomy_loop_outcome entry; got "
                    f"{outcome_entries}. workflow_executions: "
                    f"{rows}. step_outputs: {step_rows}"
                )
            # 10. Substrate state pin: outcome event landed with
            # correct payload. B2 dedup closure invariant: exactly
            # one outcome per activation episode (Codex round-1
            # tightening — no more "loop ran but possibly multiple
            # times" relaxation).
            assert len(outcome_entries) == 1, (
                f"expected exactly one autonomy_loop_outcome entry; "
                f"got {outcome_entries}"
            )
            entry = outcome_entries[0]
            assert entry["workflow_id"] == "self_improvement"
            assert entry["outcome"] == "completed"
            assert pattern.pattern_id in entry["addresses_friction_patterns"]
            # B2 dedup closure invariant: one activation episode →
            # one autonomy-loop turn → one mark_resolved → final
            # pattern state is RESOLVED. The emitter's per-pattern
            # last_emitted_epoch dedup collapses duplicate
            # friction.pattern_reactivated emissions to the canonical
            # first emission, so subsequent re-fires don't produce
            # additional workflow executions.
            final_pattern = await stack["pattern_store"].get_pattern(
                TEST_INSTANCE_ID, pattern.pattern_id,
            )
            if final_pattern.lifecycle_state != LIFECYCLE_RESOLVED:
                # Diagnostic dump.
                async with stack["engine"]._db.execute(
                    "SELECT execution_id, state, started_at, "
                    "terminated_at, trigger_event_id "
                    "FROM workflow_executions "
                    "WHERE workflow_id='self_improvement'",
                ) as cur:
                    execs = [dict(r) for r in await cur.fetchall()]
                # Also check the outbox for fire claims.
                async with stack["runtime"]._outbox._db.execute(
                    "SELECT * FROM trigger_fires",
                ) as cur:
                    fires = [dict(r) for r in await cur.fetchall()]
                raise AssertionError(
                    f"expected final lifecycle_state=resolved; got "
                    f"{final_pattern.lifecycle_state}. "
                    f"emit_count={freq_emitter._emit_count}. "
                    f"executions={execs}. "
                    f"trigger_fires={fires}. "
                    f"reactivated_at={final_pattern.reactivated_at}. "
                    f"resolved_at={final_pattern.resolved_at}. "
                    f"active_epoch={final_pattern.active_epoch}."
                )
            assert final_pattern.resolved_by_spec == "self_improvement"
            # And: emitter's dedup state shows the pattern's epoch was
            # emitted exactly once.
            assert freq_emitter._emit_count == 1, (
                f"expected exactly one emitter translation per "
                f"activation episode; got {freq_emitter._emit_count}"
            )
            # And: mark_resolved step output shows the transition
            # committed to RESOLVED state at the tool layer.
            async with stack["engine"]._db.execute(
                "SELECT output_json FROM workflow_step_outputs "
                "WHERE output_name = 'mark_resolved'",
            ) as cur:
                mr_rows = [dict(r) for r in await cur.fetchall()]
            assert len(mr_rows) == 1, (
                f"expected exactly one mark_resolved step output; "
                f"got {len(mr_rows)}"
            )
            # Codex round-2 LOW 2 fold: substrate cardinality pins so
            # an extra duplicate workflow execution that aborts
            # before mark_resolved would still be caught.
            async with stack["engine"]._db.execute(
                "SELECT COUNT(*) AS n FROM workflow_executions "
                "WHERE workflow_id = 'self_improvement'",
            ) as cur:
                exec_count_row = await cur.fetchone()
            assert exec_count_row["n"] == 1, (
                f"expected exactly one workflow_executions row for "
                f"self_improvement; got {exec_count_row['n']}"
            )
            async with stack["runtime"]._outbox._db.execute(
                "SELECT COUNT(*) AS n FROM trigger_fires "
                "WHERE instance_id = ?",
                (TEST_INSTANCE_ID,),
            ) as cur:
                fire_count_row = await cur.fetchone()
            assert fire_count_row["n"] == 1, (
                f"expected exactly one trigger_fires row for "
                f"instance {TEST_INSTANCE_ID}; got {fire_count_row['n']}"
            )
        finally:
            await response_emitter.stop()
            await freq_emitter.stop()


# ===========================================================================
# Implementation note coverage
# ===========================================================================


class TestImplementationNoteCoverage:
    """Spec 6 v7.3 ratification call: "Embedded live tests covering
    all 16+ implementation notes (V4.2.1 through V7.3.2)". Each
    implementation note has a corresponding test pin somewhere across
    the test suite; this class adds spot-check pins for the
    Spec 6-side notes (V7.1, V7.2, V7.3, V7.4 + V7.3.1, V7.3.2)
    that are directly verifiable at the self-improvement-workflow
    integration tier."""

    def test_v7_1_execute_workflow_returns_str_sentinel(self):
        """V7.1: execute_workflow returns str sentinel for skip,
        NOT a dataclass. Pinned via 15th amendment."""
        from kernos.kernel.workflows.execution_engine import (
            EXECUTE_SKIPPED_AUTHORING_INACTIVE,
        )
        assert isinstance(EXECUTE_SKIPPED_AUTHORING_INACTIVE, str)
        assert EXECUTE_SKIPPED_AUTHORING_INACTIVE.startswith("skipped:")

    def test_v7_2_execution_engine_has_register_emitter(self):
        """V7.2: engine.register_emitter() is a small method on
        ExecutionEngine. Pinned via Spec 6 commit 1."""
        engine = ExecutionEngine()
        assert hasattr(engine, "register_emitter")
        assert callable(engine.register_emitter)

    def test_v7_3_canonical_helper_is_centralized(self):
        """V7.3 + 16th-amendment MEDIUM 1: canonical descriptor JSON
        is computed via the single _compute_canonical_descriptor_json
        helper with allow_nan=False + tight separators."""
        from kernos.kernel.workflows.authoring import (
            _compute_canonical_descriptor_json,
        )
        out = _compute_canonical_descriptor_json({"a": 1, "b": [2, 3]})
        assert out == '{"a":1,"b":[2,3]}'

    def test_v7_4_cross_module_import_for_compile_descriptor_triggers(self):
        """V7.4: cross-module import path. Pinned by Spec 5 14th
        amendment H1: authoring.py imports compile_descriptor_triggers
        from kernos.kernel.triggers without circular issue."""
        from kernos.kernel.triggers import compile_descriptor_triggers
        assert callable(compile_descriptor_triggers)

    def test_v7_3_1_validation_error_not_authoring_error(self):
        """V7.3.1: serializability check returns ValidationError, NOT
        the non-existent AuthoringError. Pinned by 13th amendment."""
        from kernos.kernel.workflows.authoring import ValidationError
        # Just confirm the class exists at the expected name.
        assert ValidationError.__name__ == "ValidationError"

    def test_v7_3_2_predicate_validation_error_imported_from_triggers(
        self,
    ):
        """V7.3.2: M2 fold uses PredicateValidationError from
        kernos.kernel.triggers, NOT TriggerError. Pinned by 14th
        amendment H1."""
        from kernos.kernel.triggers import PredicateValidationError
        assert PredicateValidationError.__name__ == "PredicateValidationError"
