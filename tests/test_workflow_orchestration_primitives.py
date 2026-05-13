"""WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 substrate-fidelity tests.

Pins the v-final contract for Spec 4:

  * Step output capture across all 5 outcomes (Decision 2 / Codex
    round-1 High 4): non-gated success, gated success, continue-
    failure, aborting failure, execute-raised. All capture an
    envelope; the per-outcome SQL helpers thread it.
  * Reference resolution (Decision 3): four namespaces with prefix
    dispatch, sole-reference type preservation, mixed-string
    substitution, RefResolutionError abort in parameter context vs
    no-match in predicate context.
  * Branch verb (Decision 5): goto semantics, native-bool only,
    durability across restart via next_step_index column (Codex
    round-1 Blocker 1).
  * Global step ordinal (Decision 0): unique step_index across main
    + terminal_branches; Spec 3's PK composes (Codex round-1
    Blocker 3).
  * Terminal branches (Decision 7): top-level descriptor block;
    reachable only via branch verb's terminal:<name>:<id> target;
    terminal_branch column captures branch_name; engine
    terminal_state stays completed/aborted.
  * Predicate-evaluator template substitution (Decision 8): cached
    per (execution_id, gate_nonce); reference-failure returns False
    (composes with request-and-wait).
  * Gate output capture (Decision 6): atomic with gate release;
    matched event payload threaded via _await_gate's new return
    shape; referenceable as {gate.<name>.output.payload.<path>}.
  * ID grammar (Decision 0 / Codex round-1 Medium 9):
    [A-Za-z][A-Za-z0-9_-]* enforced at registration.
  * Consistency invariant (Decision 11 / Codex round-1 Medium 10):
    step_output INSERT gated on action_record inserted=True.
"""
from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest

from kernos.kernel import event_stream
from kernos.kernel.workflows.action_library import (
    ActionLibrary,
    ActionResult,
    AppendToLedgerAction,
    BranchAction,
    MarkStateAction,
    NotifyUserAction,
)
from kernos.kernel.workflows.execution_engine import (
    ExecutionEngine,
    WorkflowExecution,
    _ensure_schema,
)
from kernos.kernel.workflows.ledger import WorkflowLedger
from kernos.kernel.workflows.refs import (
    IdentifierGrammarError,
    RefResolutionError,
    ResolutionContext,
    extract_references,
    resolve_references_in_value,
    validate_identifier,
)
from kernos.kernel.workflows.step_outputs import (
    build_output_envelope,
    capture_gate_output,
    capture_step_output,
    ensure_workflow_step_outputs_schema,
    load_workflow_outputs,
)
from kernos.kernel.workflows.trigger_registry import (
    TriggerRegistry,
    _reset_for_tests as _reset_trigger_registry,
)
from kernos.kernel.workflows.workflow_registry import (
    ActionDescriptor,
    ApprovalGate,
    Bounds,
    ContinuationRules,
    TriggerDescriptor,
    Verifier,
    Workflow,
    WorkflowError,
    WorkflowRegistry,
    validate_workflow,
)


# ===========================================================================
# Fixtures + helpers
# ===========================================================================


def _make_action(
    action_type="mark_state", *, id="", gate_ref=None, on_failure="abort",
    resume_safe=False, **params,
) -> ActionDescriptor:
    return ActionDescriptor(
        action_type=action_type,
        parameters=params,
        gate_ref=gate_ref,
        resume_safe=resume_safe,
        continuation_rules=ContinuationRules(on_failure=on_failure),
        id=id,
    )


def _make_workflow(
    actions, *, workflow_id="wf-orch", approval_gates=None,
    terminal_branches=None, **overrides,
) -> Workflow:
    base = dict(
        workflow_id=workflow_id,
        instance_id="inst_a",
        name="orchestration test",
        description="",
        owner="owner",
        version="1.0",
        bounds=Bounds(iteration_count=10, wall_time_seconds=30),
        verifier=Verifier(flavor="deterministic", check="ok"),
        action_sequence=actions,
        approval_gates=approval_gates or [],
        trigger=TriggerDescriptor(
            event_type="cc.batch.report",
            predicate={"op": "exists", "path": "event_id"},
        ),
        terminal_branches=terminal_branches or {},
    )
    base.update(overrides)
    return Workflow(**base)


def _state_store():
    store: dict = {}

    async def set_(*, key, value, scope, instance_id):
        store[(scope, instance_id, key)] = value

    async def get_(*, key, scope, instance_id):
        return store.get((scope, instance_id, key))

    return store, set_, get_


@pytest.fixture
async def stack(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    trig = TriggerRegistry()
    await trig.start(str(tmp_path))
    wfr = WorkflowRegistry()
    await wfr.start(str(tmp_path), trig)
    store, set_, get_ = _state_store()
    lib = ActionLibrary()
    lib.register(MarkStateAction(state_store_set=set_, state_store_get=get_))
    lib.register(BranchAction())
    delivered: list = []

    async def deliver(**kw):
        delivered.append(kw)
        return {"persisted_id": f"msg-{len(delivered)}"}
    lib.register(NotifyUserAction(deliver_fn=deliver))
    ledger = WorkflowLedger(str(tmp_path))
    engine = ExecutionEngine()
    await engine.start(str(tmp_path), trig, wfr, lib, ledger, space_resolver=None)
    yield {
        "tmp_path": tmp_path,
        "trig": trig,
        "wfr": wfr,
        "lib": lib,
        "ledger": ledger,
        "engine": engine,
        "store": store,
        "delivered": delivered,
    }
    await engine.stop()
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await event_stream._reset_for_tests()


async def _wait_for(predicate, timeout=2.0, step=0.02):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


async def _wait_for_completed(engine, instance_id, timeout=2.0, step=0.02):
    """Wait until at least one execution for ``instance_id`` is in
    state='completed'. Avoids the race window between a workflow's
    last verb returning (verb's side effect lands in the store) and
    the engine's _complete writing state='completed' to the row.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        execs = await engine.list_executions(instance_id, state="completed")
        if execs:
            return execs
        await asyncio.sleep(step)
    return []


async def _open_db(tmp_path) -> aiosqlite.Connection:
    db = await aiosqlite.connect(
        str(tmp_path / "instance.db"), isolation_level=None,
    )
    db.row_factory = aiosqlite.Row
    await _ensure_schema(db)
    from kernos.kernel.workflows.action_sink import (
        ensure_workflow_action_records_schema,
    )
    await ensure_workflow_action_records_schema(db)
    await ensure_workflow_step_outputs_schema(db)
    return db


def _make_execution(
    *, workflow_id="wf-orch", instance_id="inst_a",
    execution_id="exec_orch_0001", correlation_id="corr_0001",
    member_id="mem_a",
) -> WorkflowExecution:
    return WorkflowExecution(
        execution_id=execution_id,
        workflow_id=workflow_id,
        instance_id=instance_id,
        correlation_id=correlation_id,
        state="running",
        action_index_completed=-1,
        member_id=member_id,
        started_at="2026-05-12T00:00:00+00:00",
    )


# ===========================================================================
# Decision 0: ID grammar
# ===========================================================================


class TestIdentifierGrammar:
    def test_valid_ids_accepted(self):
        validate_identifier("ask_cc")
        validate_identifier("step-3")
        validate_identifier("Branch1")
        validate_identifier("a")

    def test_invalid_ids_rejected(self):
        with pytest.raises(IdentifierGrammarError):
            validate_identifier("bad.id")
        with pytest.raises(IdentifierGrammarError):
            validate_identifier("bad:gate")
        with pytest.raises(IdentifierGrammarError):
            validate_identifier("bad name")  # whitespace
        with pytest.raises(IdentifierGrammarError):
            validate_identifier("1leading_digit")
        with pytest.raises(IdentifierGrammarError):
            validate_identifier("")


# ===========================================================================
# Decision 2 + 11: Output envelope shape + capture
# ===========================================================================


class TestOutputCapture:
    async def test_envelope_shape(self):
        env = build_output_envelope(
            success=True, value={"k": 1}, error=None, receipt={"r": "x"},
        )
        assert env == {
            "success": True,
            "value": {"k": 1},
            "error": None,
            "receipt": {"r": "x"},
        }

    async def test_capture_round_trip(self, tmp_path):
        db = await _open_db(tmp_path)
        execution = _make_execution()
        # insert execution row (FK)
        await db.execute(
            "INSERT INTO workflow_executions ("
            " execution_id, workflow_id, instance_id, correlation_id,"
            " state, action_index_completed, intermediate_state,"
            " last_heartbeat, aborted_reason, started_at, terminated_at,"
            " trigger_event_payload, trigger_event_id, member_id,"
            " gate_nonce, fire_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            execution.to_row(),
        )
        envelope = build_output_envelope(
            success=True, value={"request_id": "abc"},
            error=None, receipt={"called_at": "x"},
        )
        await capture_step_output(
            db,
            instance_id=execution.instance_id,
            workflow_execution_id=execution.execution_id,
            step_id="ask_cc",
            envelope=envelope,
        )
        step_outs, gate_outs = await load_workflow_outputs(
            db, execution.instance_id, execution.execution_id,
        )
        assert "ask_cc" in step_outs
        assert step_outs["ask_cc"]["value"] == {"request_id": "abc"}
        assert step_outs["ask_cc"]["success"] is True
        assert gate_outs == {}
        await db.close()

    async def test_capture_gate_output(self, tmp_path):
        db = await _open_db(tmp_path)
        execution = _make_execution()
        await db.execute(
            "INSERT INTO workflow_executions ("
            " execution_id, workflow_id, instance_id, correlation_id,"
            " state, action_index_completed, intermediate_state,"
            " last_heartbeat, aborted_reason, started_at, terminated_at,"
            " trigger_event_payload, trigger_event_id, member_id,"
            " gate_nonce, fire_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            execution.to_row(),
        )
        await capture_gate_output(
            db,
            instance_id=execution.instance_id,
            workflow_execution_id=execution.execution_id,
            gate_name="ratification",
            event_payload={"approved": True, "by": "operator"},
        )
        step_outs, gate_outs = await load_workflow_outputs(
            db, execution.instance_id, execution.execution_id,
        )
        assert step_outs == {}
        # Spec 4 post-impl High 4: gate envelope value IS the
        # event payload directly (no wrapper key).
        assert gate_outs["ratification"]["value"] == {
            "approved": True, "by": "operator",
        }
        await db.close()

    async def test_truncation_marker(self, tmp_path):
        db = await _open_db(tmp_path)
        execution = _make_execution()
        await db.execute(
            "INSERT INTO workflow_executions ("
            " execution_id, workflow_id, instance_id, correlation_id,"
            " state, action_index_completed, intermediate_state,"
            " last_heartbeat, aborted_reason, started_at, terminated_at,"
            " trigger_event_payload, trigger_event_id, member_id,"
            " gate_nonce, fire_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            execution.to_row(),
        )
        big_value = "x" * 100_000  # well over 64KB
        envelope = build_output_envelope(
            success=True, value={"big": big_value},
            error=None, receipt={},
        )
        await capture_step_output(
            db,
            instance_id=execution.instance_id,
            workflow_execution_id=execution.execution_id,
            step_id="big_step", envelope=envelope,
        )
        async with db.execute(
            "SELECT truncated, output_json FROM workflow_step_outputs "
            "WHERE output_name = 'big_step'"
        ) as cur:
            row = await cur.fetchone()
        assert row["truncated"] == 1
        payload = json.loads(row["output_json"])
        assert payload["value"]["_truncated"] is True
        await db.close()

    async def test_non_serializable_placeholder(self, tmp_path):
        db = await _open_db(tmp_path)
        execution = _make_execution()
        await db.execute(
            "INSERT INTO workflow_executions ("
            " execution_id, workflow_id, instance_id, correlation_id,"
            " state, action_index_completed, intermediate_state,"
            " last_heartbeat, aborted_reason, started_at, terminated_at,"
            " trigger_event_payload, trigger_event_id, member_id,"
            " gate_nonce, fire_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            execution.to_row(),
        )
        # Set is not JSON-serializable.
        envelope = build_output_envelope(
            success=True, value={"weird": {1, 2, 3}},
            error=None, receipt={},
        )
        await capture_step_output(
            db,
            instance_id=execution.instance_id,
            workflow_execution_id=execution.execution_id,
            step_id="bad_step", envelope=envelope,
        )
        async with db.execute(
            "SELECT output_json FROM workflow_step_outputs "
            "WHERE output_name = 'bad_step'"
        ) as cur:
            row = await cur.fetchone()
        payload = json.loads(row["output_json"])
        assert payload["success"] is False
        assert payload["error"].startswith("non_serializable:")
        await db.close()


# ===========================================================================
# Decision 3: Reference resolver
# ===========================================================================


class TestReferenceResolver:
    def _ctx(self, *, step_outputs=None, gate_outputs=None,
            trigger_payload=None, mode="parameter"):
        execution = _make_execution()
        return ResolutionContext(
            execution=execution,
            trigger_payload=trigger_payload or {},
            step_outputs=step_outputs or {},
            gate_outputs=gate_outputs or {},
            mode=mode,
        )

    def test_sole_reference_preserves_native_type_bool(self):
        ctx = self._ctx(step_outputs={
            "step1": {"success": True, "value": {"approved": True},
                      "error": None, "receipt": {}}
        })
        out = resolve_references_in_value(
            "{step.step1.output.approved}", ctx,
        )
        assert out is True

    def test_sole_reference_preserves_dict(self):
        ctx = self._ctx(step_outputs={
            "step1": {"success": True, "value": {"data": {"k": 1}},
                      "error": None, "receipt": {}}
        })
        out = resolve_references_in_value(
            "{step.step1.output.data}", ctx,
        )
        assert out == {"k": 1}

    def test_mixed_string_substitution_stringifies(self):
        ctx = self._ctx(step_outputs={
            "step1": {"success": True, "value": {"id": "abc"},
                      "error": None, "receipt": {}}
        })
        out = resolve_references_in_value(
            "prefix-{step.step1.output.id}-suffix", ctx,
        )
        assert out == "prefix-abc-suffix"

    def test_workflow_namespace_resolves(self):
        ctx = self._ctx()
        out = resolve_references_in_value(
            "{workflow.execution_id}", ctx,
        )
        assert out == "exec_orch_0001"

    def test_idea_payload_resolves(self):
        ctx = self._ctx(trigger_payload={"description": "hi"})
        out = resolve_references_in_value(
            "{idea_payload.description}", ctx,
        )
        assert out == "hi"

    def test_gate_namespace_resolves(self):
        # Spec 4 post-impl High 4: gate envelope value IS the event
        # payload directly. Reference is {gate.<name>.output.<path>}.
        ctx = self._ctx(gate_outputs={
            "ratify": {
                "success": True,
                "value": {"approved": True},
                "error": None, "receipt": {},
            }
        })
        out = resolve_references_in_value(
            "{gate.ratify.output.approved}", ctx,
        )
        assert out is True

    def test_missing_reference_raises_in_parameter_mode(self):
        ctx = self._ctx(mode="parameter")
        with pytest.raises(RefResolutionError):
            resolve_references_in_value(
                "{step.unknown.output.x}", ctx,
            )

    def test_missing_reference_returns_not_found_in_predicate_mode(self):
        from kernos.kernel.workflows.refs import _NOT_FOUND
        ctx = self._ctx(mode="predicate")
        out = resolve_references_in_value(
            "{step.unknown.output.x}", ctx,
        )
        assert out is _NOT_FOUND

    def test_extract_references_finds_all(self):
        refs = extract_references(
            "x={step.a.output.k} y={gate.b.output.payload.v}"
        )
        assert "step.a.output.k" in refs
        assert "gate.b.output.payload.v" in refs

    def test_dict_recursion(self):
        ctx = self._ctx(step_outputs={
            "step1": {"success": True, "value": {"id": "abc"},
                      "error": None, "receipt": {}}
        })
        out = resolve_references_in_value(
            {"outer": {"inner": "{step.step1.output.id}", "static": 5}},
            ctx,
        )
        assert out == {"outer": {"inner": "abc", "static": 5}}


# ===========================================================================
# Decision 0 + 5 + 7: Workflow validation
# ===========================================================================


class TestWorkflowValidation:
    def test_global_step_ordinal_assigned(self):
        wf = _make_workflow([
            _make_action("mark_state", id="a", key="x", value=1, scope="instance"),
            _make_action("mark_state", id="b", key="y", value=2, scope="instance"),
        ])
        validate_workflow(wf)
        assert wf.action_sequence[0].step_index == 0
        assert wf.action_sequence[1].step_index == 1

    def test_terminal_branch_global_step_ordinal(self):
        wf = _make_workflow(
            [
                _make_action("mark_state", id="a", key="x", value=1, scope="instance"),
            ],
            terminal_branches={
                "rejected": [
                    _make_action("mark_state", id="r1", key="y", value=2, scope="instance"),
                    _make_action("mark_state", id="r2", key="z", value=3, scope="instance"),
                ],
            },
        )
        validate_workflow(wf)
        assert wf.action_sequence[0].step_index == 0
        assert wf.terminal_branches["rejected"][0].step_index == 1
        assert wf.terminal_branches["rejected"][1].step_index == 2

    def test_duplicate_step_id_rejected(self):
        wf = _make_workflow([
            _make_action("mark_state", id="dup", key="x", value=1, scope="instance"),
            _make_action("mark_state", id="dup", key="y", value=2, scope="instance"),
        ])
        with pytest.raises(WorkflowError, match="duplicate step id"):
            validate_workflow(wf)

    def test_duplicate_id_across_main_and_terminal_rejected(self):
        wf = _make_workflow(
            [
                _make_action("mark_state", id="shared", key="x", value=1, scope="instance"),
            ],
            terminal_branches={
                "rejected": [
                    _make_action("mark_state", id="shared", key="y", value=2, scope="instance"),
                ],
            },
        )
        with pytest.raises(WorkflowError, match="duplicate step id"):
            validate_workflow(wf)

    def test_invalid_step_id_grammar_rejected(self):
        wf = _make_workflow([
            _make_action("mark_state", id="bad.id", key="x", value=1, scope="instance"),
        ])
        with pytest.raises(WorkflowError):
            validate_workflow(wf)

    def test_invalid_gate_name_rejected(self):
        wf = _make_workflow(
            [_make_action("mark_state", key="x", value=1, scope="instance")],
            approval_gates=[ApprovalGate(
                gate_name="bad:gate",
                approval_event_type="approval.granted",
                approval_event_predicate={"op": "exists", "path": "event_id"},
                timeout_seconds=30,
                bound_behavior_on_timeout="abort_workflow",
                pause_reason="x",
            )],
        )
        with pytest.raises(WorkflowError):
            validate_workflow(wf)

    def test_branch_verb_target_must_exist(self):
        wf = _make_workflow([
            _make_action("mark_state", id="a", key="x", value=1, scope="instance"),
            _make_action(
                "branch", id="b",
                condition=True, branch_on_true="nonexistent",
                branch_on_false="a",
            ),
        ])
        with pytest.raises(WorkflowError, match="branch_on_true"):
            validate_workflow(wf)

    def test_branch_terminal_target_validated(self):
        # Forward-only branch graph (no back-edge): step a → branch b →
        # either step c (main forward) or terminal r1.
        wf = _make_workflow(
            [
                _make_action("mark_state", id="a", key="x", value=1, scope="instance"),
                _make_action(
                    "branch", id="b",
                    condition=True,
                    branch_on_true="c",
                    branch_on_false="terminal:rejected:r1",
                ),
                _make_action("mark_state", id="c", key="y", value=2, scope="instance"),
            ],
            terminal_branches={
                "rejected": [
                    _make_action("mark_state", id="r1", key="z", value=3, scope="instance"),
                ],
            },
        )
        validate_workflow(wf)  # should not raise

    def test_branch_cycle_rejected(self):
        # Spec 4 post-impl High 5: cycle detection. Branch at step 1
        # targets step 0 on true (back-edge) → cycle.
        wf = _make_workflow(
            [
                _make_action("mark_state", id="a", key="x", value=1, scope="instance"),
                _make_action(
                    "branch", id="b",
                    condition=True,
                    branch_on_true="a",  # back-edge → cycle
                    branch_on_false="terminal:rejected:r1",
                ),
            ],
            terminal_branches={
                "rejected": [
                    _make_action("mark_state", id="r1", key="y", value=2, scope="instance"),
                ],
            },
        )
        with pytest.raises(WorkflowError, match="cycle"):
            validate_workflow(wf)

    def test_reference_to_unknown_step_rejected(self):
        wf = _make_workflow([
            _make_action(
                "mark_state", id="a",
                key="x", value="{step.unknown.output.id}", scope="instance",
            ),
        ])
        with pytest.raises(WorkflowError, match="unknown step"):
            validate_workflow(wf)


# ===========================================================================
# Decision 5: Branch verb (native bool, durability)
# ===========================================================================


class TestBranchVerb:
    async def test_branch_verb_native_bool_only(self):
        action = BranchAction()
        result = await action.execute(
            None,
            {"condition": "true", "branch_on_true": "a", "branch_on_false": "b"},
        )
        assert result.success is False
        assert "branch_condition_not_bool" in result.error

    async def test_branch_verb_routes_on_true(self):
        action = BranchAction()
        result = await action.execute(
            None,
            {"condition": True, "branch_on_true": "a", "branch_on_false": "b"},
        )
        assert result.success is True
        assert result.receipt["branched_to"] == "a"
        assert result.receipt["condition_value"] is True

    async def test_branch_verb_routes_on_false(self):
        action = BranchAction()
        result = await action.execute(
            None,
            {"condition": False, "branch_on_true": "a", "branch_on_false": "b"},
        )
        assert result.success is True
        assert result.receipt["branched_to"] == "b"

    async def test_branch_end_to_end_goto(self, stack):
        # Three main steps + branch routing to step3 vs terminal.
        # When branch_on_true="step3", execution goes step1 → branch → step3
        # (skipping step2 which is sequentially between branch and step3 only
        # if we set up properly... since branch IS goto, with this layout
        # step1 → branch → step3 directly; step2 if placed AFTER step3 in
        # main sequence would run normally).
        actions = [
            _make_action(
                "mark_state", id="step1",
                key="first", value=True, scope="instance",
            ),
            _make_action(
                "branch", id="bx",
                condition=True,  # literal True; not a reference
                branch_on_true="step3",
                branch_on_false="terminal:rejected:r1",
            ),
            _make_action(
                "mark_state", id="step2",
                key="skipped", value=True, scope="instance",
            ),
            _make_action(
                "mark_state", id="step3",
                key="target", value=True, scope="instance",
            ),
        ]
        wf = _make_workflow(
            actions, workflow_id="wf-branch-true",
            terminal_branches={
                "rejected": [
                    _make_action(
                        "mark_state", id="r1",
                        key="rejected_marker", value=True, scope="instance",
                    ),
                ],
            },
        )
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        ok = await _wait_for(
            lambda: ("instance", "inst_a", "target") in stack["store"],
        )
        assert ok
        # step2 should have been skipped (branch went directly to step3).
        assert ("instance", "inst_a", "skipped") not in stack["store"]
        # rejected terminal not visited.
        assert ("instance", "inst_a", "rejected_marker") not in stack["store"]

    async def test_branch_to_terminal_branch_runs_to_completion(self, stack):
        actions = [
            _make_action(
                "mark_state", id="step1",
                key="first", value=True, scope="instance",
            ),
            _make_action(
                "branch", id="bx",
                condition=False,
                branch_on_true="step2",
                branch_on_false="terminal:rejected:r1",
            ),
            _make_action(
                "mark_state", id="step2",
                key="main_target", value=True, scope="instance",
            ),
        ]
        wf = _make_workflow(
            actions, workflow_id="wf-branch-terminal",
            terminal_branches={
                "rejected": [
                    _make_action(
                        "mark_state", id="r1",
                        key="rejected_marker", value=True, scope="instance",
                    ),
                ],
            },
        )
        await stack["wfr"]._register_workflow_unbound(wf)
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.flush_now()
        executions = await _wait_for_completed(
            stack["engine"], "inst_a",
        )
        assert len(executions) == 1
        # rejected_marker should have landed via r1 in the terminal branch.
        assert ("instance", "inst_a", "rejected_marker") in stack["store"]
        # main sequence's step2 should NOT have run.
        assert ("instance", "inst_a", "main_target") not in stack["store"]
        async with stack["engine"]._db.execute(
            "SELECT terminal_branch FROM workflow_executions WHERE execution_id = ?",
            (executions[0].execution_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row["terminal_branch"] == "rejected"


# ===========================================================================
# Decision 9: Branch durability across restart (Codex Blocker 1)
# ===========================================================================


class TestBranchDurability:
    async def test_next_step_index_persisted_atomically(self, tmp_path):
        # Lower-level test: bypass the engine; verify that
        # _append_and_advance_with_branch lands record + next_step_index
        # in one transaction.
        from kernos.kernel.workflows.action_sink import (
            WorkflowActionSink, _append_and_advance_with_branch,
        )
        db = await _open_db(tmp_path)
        execution = _make_execution()
        await db.execute(
            "INSERT INTO workflow_executions ("
            " execution_id, workflow_id, instance_id, correlation_id,"
            " state, action_index_completed, intermediate_state,"
            " last_heartbeat, aborted_reason, started_at, terminated_at,"
            " trigger_event_payload, trigger_event_id, member_id,"
            " gate_nonce, fire_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            execution.to_row(),
        )
        sink = WorkflowActionSink(db)
        per_exec = sink.for_execution(execution)
        from kernos.kernel.integration.briefing import ActionStateRecord
        record = ActionStateRecord(
            action_id="act_test",
            surface="workflow_step",
            operation="branch",
            operation_class="mutate",
            authorization_state="not_required",
            execution_state="completed",
            risk_level="medium",
        )
        envelope = build_output_envelope(
            success=True, value={"branched_to": "step5"},
            error=None, receipt={"branched_to": "step5"},
        )
        await db.execute("BEGIN IMMEDIATE")
        inserted = await _append_and_advance_with_branch(
            db, per_exec, record,
            step_index=2, action_type="branch",
            next_step_index=5,
            step_output_envelope=envelope, step_id="branch_a",
        )
        await db.execute("COMMIT")
        assert inserted is True
        async with db.execute(
            "SELECT action_index_completed, next_step_index FROM workflow_executions "
            "WHERE execution_id = ?", (execution.execution_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row["action_index_completed"] == 2
        assert row["next_step_index"] == 5
        await db.close()


# ===========================================================================
# Decision 6: Gate output capture
# ===========================================================================


class TestGateOutputCapture:
    async def test_gate_output_referenceable_via_namespace(self, tmp_path):
        db = await _open_db(tmp_path)
        execution = _make_execution()
        await db.execute(
            "INSERT INTO workflow_executions ("
            " execution_id, workflow_id, instance_id, correlation_id,"
            " state, action_index_completed, intermediate_state,"
            " last_heartbeat, aborted_reason, started_at, terminated_at,"
            " trigger_event_payload, trigger_event_id, member_id,"
            " gate_nonce, fire_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            execution.to_row(),
        )
        await capture_gate_output(
            db,
            instance_id=execution.instance_id,
            workflow_execution_id=execution.execution_id,
            gate_name="ratify",
            event_payload={"approved": True, "by": "operator"},
        )
        step_outs, gate_outs = await load_workflow_outputs(
            db, execution.instance_id, execution.execution_id,
        )
        ctx = ResolutionContext(
            execution=execution,
            trigger_payload={},
            step_outputs=step_outs,
            gate_outputs=gate_outs,
            mode="parameter",
        )
        # Spec 4 post-impl High 4: {gate.<name>.output.<path>}
        # resolves to event_payload[path] directly.
        resolved = resolve_references_in_value(
            "{gate.ratify.output.approved}", ctx,
        )
        assert resolved is True
        await db.close()


# ===========================================================================
# Decision 11: Consistency invariant
# ===========================================================================


class TestConsistencyInvariant:
    async def test_step_output_skipped_when_action_record_skipped(
        self, tmp_path,
    ):
        # Spec 3's ON CONFLICT DO NOTHING means a retry on the same
        # (instance_id, workflow_execution_id, step_index) action
        # record returns inserted=False. The Spec 4 helper must NOT
        # update the step output when inserted is False.
        from kernos.kernel.workflows.action_sink import (
            WorkflowActionSink, _append_and_advance,
        )
        db = await _open_db(tmp_path)
        execution = _make_execution()
        await db.execute(
            "INSERT INTO workflow_executions ("
            " execution_id, workflow_id, instance_id, correlation_id,"
            " state, action_index_completed, intermediate_state,"
            " last_heartbeat, aborted_reason, started_at, terminated_at,"
            " trigger_event_payload, trigger_event_id, member_id,"
            " gate_nonce, fire_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            execution.to_row(),
        )
        sink = WorkflowActionSink(db)
        per_exec = sink.for_execution(execution)
        from kernos.kernel.integration.briefing import ActionStateRecord
        first_record = ActionStateRecord(
            action_id="act_first",
            surface="workflow_step",
            operation="mark_state",
            operation_class="mutate",
            authorization_state="not_required",
            execution_state="completed",
            risk_level="medium",
        )
        first_env = build_output_envelope(
            success=True, value={"v": "FIRST"}, error=None, receipt={},
        )
        await db.execute("BEGIN IMMEDIATE")
        await _append_and_advance(
            db, per_exec, first_record,
            step_index=0, action_type="mark_state",
            step_output_envelope=first_env, step_id="step0",
        )
        await db.execute("COMMIT")

        # Retry with a different envelope; the action record's ON CONFLICT
        # DO NOTHING should skip, AND the step output should NOT be updated.
        second_record = ActionStateRecord(
            action_id="act_second",  # different action_id
            surface="workflow_step",
            operation="mark_state",
            operation_class="mutate",
            authorization_state="not_required",
            execution_state="completed",
            risk_level="medium",
        )
        second_env = build_output_envelope(
            success=True, value={"v": "SECOND"}, error=None, receipt={},
        )
        await db.execute("BEGIN IMMEDIATE")
        inserted = await _append_and_advance(
            db, per_exec, second_record,
            step_index=0, action_type="mark_state",
            step_output_envelope=second_env, step_id="step0",
        )
        await db.execute("COMMIT")
        assert inserted is False  # PK conflict
        step_outs, _ = await load_workflow_outputs(
            db, execution.instance_id, execution.execution_id,
        )
        # Step output should still hold the FIRST envelope's value.
        assert step_outs["step0"]["value"] == {"v": "FIRST"}
        await db.close()
