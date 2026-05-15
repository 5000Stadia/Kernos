"""Spec 6 commit 3: 3 autonomy-loop substrate-tier tool tests.

Pins each tool's substrate-tier authority (operator-actor enforcement),
substrate effects (FrictionPatternStore transitions + WorkflowLedger
writes), audit trail (workflow_autonomy.action_recorded events), and
functional flow under expected use.

Test shape per architect user-feedback: every mechanic has BOTH a
unit pin AND a functional pin where the mechanic is exercised under
its expected workflow-side use and the expected outcome is asserted.
"""
from __future__ import annotations

import asyncio

import pytest

from kernos.kernel import event_stream
from kernos.kernel.friction_patterns import (
    CLASSIFIED_AUTO_SIGNAL_TYPE,
    FrictionPatternStore,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_REACTIVATED,
    LIFECYCLE_RESOLVED,
)
from kernos.kernel.workflows.authoring import (
    ACTOR_ARCHITECT,
    ACTOR_KERNOS,
    ACTOR_OPERATOR,
    ACTOR_SYSTEM,
    AuthoringContext,
)
from kernos.kernel.workflows.autonomy_tools import (
    AutonomyToolResult,
    CAT_AUTONOMY_INVALID_ARGS,
    CAT_AUTONOMY_NOT_AUTHORIZED,
    CAT_AUTONOMY_SUBSTRATE_ERROR,
    VALID_AUTONOMY_TOOL_NAMES,
    emit_autonomy_loop_event,
    handle_emit_autonomy_loop_event_tool,
    handle_record_friction_pattern_recurrence_tool,
    handle_transition_friction_pattern_lifecycle_tool,
    record_friction_pattern_recurrence,
    transition_friction_pattern_lifecycle,
)
from kernos.kernel.workflows.ledger import WorkflowLedger


OPERATOR_ID = "op_kernos_autonomy"
ARCHITECT_ID = "op_architect"


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def operator_env(monkeypatch):
    """Set KERNOS_OPERATOR_ACTOR_ID for the duration of the test."""
    monkeypatch.setenv("KERNOS_OPERATOR_ACTOR_ID", OPERATOR_ID)


@pytest.fixture
def architect_env(monkeypatch):
    """Set KERNOS_ARCHITECT_ACTOR_ID for the duration of the test."""
    monkeypatch.setenv("KERNOS_ARCHITECT_ACTOR_ID", ARCHITECT_ID)


@pytest.fixture
async def store(tmp_path):
    """Fresh FrictionPatternStore."""
    s = FrictionPatternStore()
    await s.start(str(tmp_path))
    yield s
    await s.stop()


@pytest.fixture
async def ledger(tmp_path):
    """Fresh WorkflowLedger."""
    return WorkflowLedger(str(tmp_path))


@pytest.fixture
async def event_capture(tmp_path):
    """Start event_stream + capture all events emitted during the test."""
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path), flush_interval_s=0.05)
    captured: list[dict] = []

    async def _capture(instance_id, event_type, payload, **kw):
        captured.append({
            "instance_id": instance_id,
            "event_type": event_type,
            "payload": payload,
            **kw,
        })

    # Patch event_stream.emit to capture; preserve real emit so the
    # writer still runs (some tests read events back via the writer).
    real_emit = event_stream.emit

    async def _proxy_emit(*args, **kwargs):
        # Normalise positional/kwargs into the canonical shape.
        if args:
            instance_id = args[0]
            event_type = args[1] if len(args) > 1 else kwargs.get("event_type", "")
            payload = args[2] if len(args) > 2 else kwargs.get("payload", {})
        else:
            instance_id = kwargs.get("instance_id", "")
            event_type = kwargs.get("event_type", "")
            payload = kwargs.get("payload", {})
        member_id = kwargs.get("member_id")
        captured.append({
            "instance_id": instance_id,
            "event_type": event_type,
            "payload": payload,
            "member_id": member_id,
        })
        return await real_emit(*args, **kwargs)

    event_stream.emit = _proxy_emit  # type: ignore[assignment]
    try:
        yield captured
    finally:
        event_stream.emit = real_emit  # type: ignore[assignment]
        await event_stream.stop_writer()
        await event_stream._reset_for_tests()


def _operator_ctx() -> AuthoringContext:
    return AuthoringContext(actor_id=OPERATOR_ID, actor_kind=ACTOR_OPERATOR)


def _kernos_ctx() -> AuthoringContext:
    return AuthoringContext(actor_id="mem_user", actor_kind=ACTOR_KERNOS)


def _architect_ctx() -> AuthoringContext:
    return AuthoringContext(actor_id=ARCHITECT_ID, actor_kind=ACTOR_ARCHITECT)


# ===========================================================================
# transition_friction_pattern_lifecycle
# ===========================================================================


class TestTransitionFrictionPatternLifecycle:
    """Substrate-fidelity tests for the lifecycle transition tool."""

    async def test_operator_can_transition_active_to_resolved(
        self, store, operator_env,
    ):
        """Unit pin: operator-actor invocation succeeds; FrictionPattern
        dataclass returned reflects the new state."""
        p = await store.create_pattern(
            instance_id="inst_a",
            description="test pattern",
            signal_type_keys=["k1"],
        )
        result = await transition_friction_pattern_lifecycle(
            ctx=_operator_ctx(),
            pattern_store=store,
            instance_id="inst_a",
            pattern_id=p.pattern_id,
            new_state=LIFECYCLE_RESOLVED,
            resolved_by_spec="test-spec",
        )
        assert result.success is True
        assert result.value.lifecycle_state == LIFECYCLE_RESOLVED
        assert result.value.resolved_by_spec == "test-spec"
        # Substrate state pin: row reflects the transition.
        loaded = await store.get_pattern("inst_a", p.pattern_id)
        assert loaded.lifecycle_state == LIFECYCLE_RESOLVED

    async def test_non_operator_rejected_loud(
        self, store, operator_env,
    ):
        """Unit pin: actor that isn't operator gets CAT_AUTONOMY_NOT_AUTHORIZED."""
        p = await store.create_pattern(
            instance_id="inst_a",
            description="not-operator test",
            signal_type_keys=["k2"],
        )
        result = await transition_friction_pattern_lifecycle(
            ctx=_kernos_ctx(),
            pattern_store=store,
            instance_id="inst_a",
            pattern_id=p.pattern_id,
            new_state=LIFECYCLE_RESOLVED,
        )
        assert result.success is False
        assert result.errors[0].category == CAT_AUTONOMY_NOT_AUTHORIZED
        # Substrate state pin: pattern unchanged.
        loaded = await store.get_pattern("inst_a", p.pattern_id)
        assert loaded.lifecycle_state == LIFECYCLE_ACTIVE

    async def test_operator_env_unset_fails_closed(self, store, monkeypatch):
        """Fail-closed pin: KERNOS_OPERATOR_ACTOR_ID unset → no actor
        passes (mirrors the architect discipline)."""
        monkeypatch.delenv("KERNOS_OPERATOR_ACTOR_ID", raising=False)
        p = await store.create_pattern(
            instance_id="inst_a",
            description="fail-closed test",
            signal_type_keys=["k3"],
        )
        # Build operator ctx but env var is unset.
        ctx = AuthoringContext(actor_id=OPERATOR_ID, actor_kind=ACTOR_OPERATOR)
        result = await transition_friction_pattern_lifecycle(
            ctx=ctx,
            pattern_store=store,
            instance_id="inst_a",
            pattern_id=p.pattern_id,
            new_state=LIFECYCLE_RESOLVED,
        )
        assert result.success is False
        assert result.errors[0].category == CAT_AUTONOMY_NOT_AUTHORIZED

    async def test_invalid_args_rejected(self, store, operator_env):
        """Missing required args fail loud with CAT_AUTONOMY_INVALID_ARGS."""
        result = await transition_friction_pattern_lifecycle(
            ctx=_operator_ctx(),
            pattern_store=store,
            instance_id="",
            pattern_id="",
            new_state="",
        )
        assert result.success is False
        assert result.errors[0].category == CAT_AUTONOMY_INVALID_ARGS

    async def test_substrate_error_surfaces_loud(self, store, operator_env):
        """Forbidden lifecycle transition surfaces as
        CAT_AUTONOMY_SUBSTRATE_ERROR (the store raises
        InvalidLifecycleTransition; the tool wraps)."""
        p = await store.create_pattern(
            instance_id="inst_a",
            description="forbidden",
            signal_type_keys=["k4"],
        )
        # Active → Reactivated is NOT allowed via this path.
        result = await transition_friction_pattern_lifecycle(
            ctx=_operator_ctx(),
            pattern_store=store,
            instance_id="inst_a",
            pattern_id=p.pattern_id,
            new_state=LIFECYCLE_REACTIVATED,
        )
        assert result.success is False
        assert result.errors[0].category == CAT_AUTONOMY_SUBSTRATE_ERROR

    async def test_functional_transition_increments_active_epoch(
        self, store, operator_env,
    ):
        """FUNCTIONAL pin (architect's user-feedback request): exercise
        the tool under expected autonomy-loop use. Operator transitions
        a pattern through active → resolved → active cycle. The Spec 6
        commit 1 active_epoch increment fires on the resolved→active
        transition, observable via the tool's result."""
        p = await store.create_pattern(
            instance_id="inst_a",
            description="cycle pattern",
            signal_type_keys=["kcycle"],
        )
        # Active → Resolved.
        await transition_friction_pattern_lifecycle(
            ctx=_operator_ctx(),
            pattern_store=store,
            instance_id="inst_a",
            pattern_id=p.pattern_id,
            new_state=LIFECYCLE_RESOLVED,
        )
        # Resolved → Active (manual reactivation by operator).
        result = await transition_friction_pattern_lifecycle(
            ctx=_operator_ctx(),
            pattern_store=store,
            instance_id="inst_a",
            pattern_id=p.pattern_id,
            new_state=LIFECYCLE_ACTIVE,
        )
        # Functional outcome pin: tool returned the new active_epoch in
        # the extra dict so a workflow / autonomy loop can read it.
        assert result.success is True
        assert result.value.active_epoch == 2
        assert result.extra["active_epoch"] == 2
        assert result.extra["lifecycle_state"] == LIFECYCLE_ACTIVE


# ===========================================================================
# record_friction_pattern_recurrence
# ===========================================================================


class TestRecordFrictionPatternRecurrence:

    async def test_operator_can_record_recurrence_no_reactivation(
        self, store, operator_env, monkeypatch,
    ):
        """Unit pin: operator records a recurrence; below threshold so
        no reactivation. Result.value is False (triggered_reactivation)."""
        # High threshold so single recurrence won't reactivate.
        monkeypatch.setenv("KERNOS_FRICTION_REACTIVATION_THRESHOLD", "10")
        p = await store.create_pattern(
            instance_id="inst_a",
            description="recurrence test",
            signal_type_keys=["kr1"],
        )
        await store.transition_lifecycle(
            "inst_a", p.pattern_id, LIFECYCLE_RESOLVED,
        )
        result = await record_friction_pattern_recurrence(
            ctx=_operator_ctx(),
            pattern_store=store,
            instance_id="inst_a",
            pattern_id=p.pattern_id,
            observed_at=_now(),
            report_path="report-1.md",
            classified_by=CLASSIFIED_AUTO_SIGNAL_TYPE,
        )
        assert result.success is True
        assert result.value is False
        assert result.extra["triggered_reactivation"] is False

    async def test_non_operator_rejected(self, store, operator_env):
        """Unit pin: actor-kind enforcement."""
        p = await store.create_pattern(
            instance_id="inst_a",
            description="not operator",
            signal_type_keys=["kr2"],
        )
        await store.transition_lifecycle(
            "inst_a", p.pattern_id, LIFECYCLE_RESOLVED,
        )
        result = await record_friction_pattern_recurrence(
            ctx=_architect_ctx(),
            pattern_store=store,
            instance_id="inst_a",
            pattern_id=p.pattern_id,
            observed_at=_now(),
        )
        assert result.success is False
        assert result.errors[0].category == CAT_AUTONOMY_NOT_AUTHORIZED

    async def test_functional_recurrence_triggers_reactivation(
        self, store, operator_env, monkeypatch,
    ):
        """FUNCTIONAL pin: exercise the tool under expected autonomy-loop
        use. Resolved pattern + low threshold + operator records
        recurrence → tool returns triggered_reactivation=True; substrate
        state pin: pattern is REACTIVATED with active_epoch incremented."""
        monkeypatch.setenv("KERNOS_FRICTION_REACTIVATION_THRESHOLD", "1")
        monkeypatch.setenv(
            "KERNOS_FRICTION_REACTIVATION_WINDOW_DAYS", "365",
        )
        p = await store.create_pattern(
            instance_id="inst_a",
            description="threshold test",
            signal_type_keys=["kfunc"],
        )
        epoch_at_create = p.active_epoch
        await store.transition_lifecycle(
            "inst_a", p.pattern_id, LIFECYCLE_RESOLVED,
        )
        result = await record_friction_pattern_recurrence(
            ctx=_operator_ctx(),
            pattern_store=store,
            instance_id="inst_a",
            pattern_id=p.pattern_id,
            observed_at=_now(),
            report_path="threshold-report.md",
            classified_by=CLASSIFIED_AUTO_SIGNAL_TYPE,
        )
        # Functional outcome: triggered reactivation.
        assert result.success is True
        assert result.value is True
        assert result.extra["triggered_reactivation"] is True
        # Substrate state pin: substrate reflects the transition.
        loaded = await store.get_pattern("inst_a", p.pattern_id)
        assert loaded.lifecycle_state == LIFECYCLE_REACTIVATED
        assert loaded.active_epoch > epoch_at_create


# ===========================================================================
# emit_autonomy_loop_event
# ===========================================================================


class TestEmitAutonomyLoopEvent:

    async def test_operator_can_emit_event(self, ledger, operator_env):
        """Unit pin: operator emits the autonomy event; result carries
        the payload."""
        result = await emit_autonomy_loop_event(
            ctx=_operator_ctx(),
            ledger=ledger,
            instance_id="inst_a",
            workflow_id="self_improvement",
            outcome="completed",
            addresses_friction_patterns=["pattern-1", "pattern-2"],
        )
        assert result.success is True
        assert result.value["workflow_id"] == "self_improvement"
        assert result.value["outcome"] == "completed"
        assert result.value["addresses_friction_patterns"] == [
            "pattern-1", "pattern-2",
        ]
        # Substrate state pin: ledger has the entry.
        entries = await ledger.read_all("inst_a", "autonomy_loop_outcomes")
        assert len(entries) == 1
        assert entries[0]["workflow_id"] == "self_improvement"
        assert entries[0]["outcome"] == "completed"

    async def test_non_operator_rejected(self, ledger, operator_env):
        """Unit pin: non-operator rejected; ledger unchanged."""
        result = await emit_autonomy_loop_event(
            ctx=_kernos_ctx(),
            ledger=ledger,
            instance_id="inst_a",
            workflow_id="self_improvement",
            outcome="completed",
        )
        assert result.success is False
        assert result.errors[0].category == CAT_AUTONOMY_NOT_AUTHORIZED
        entries = await ledger.read_all("inst_a", "autonomy_loop_outcomes")
        assert entries == []

    async def test_invalid_args_rejected(self, ledger, operator_env):
        """Missing required args fail loud."""
        result = await emit_autonomy_loop_event(
            ctx=_operator_ctx(),
            ledger=ledger,
            instance_id="",
            workflow_id="",
            outcome="",
        )
        assert result.success is False
        assert result.errors[0].category == CAT_AUTONOMY_INVALID_ARGS

    async def test_extra_payload_merged_into_entry(
        self, ledger, operator_env,
    ):
        """Custom extra_payload fields land in the ledger entry."""
        result = await emit_autonomy_loop_event(
            ctx=_operator_ctx(),
            ledger=ledger,
            instance_id="inst_a",
            workflow_id="self_improvement",
            outcome="completed",
            addresses_friction_patterns=["pattern-1"],
            extra_payload={
                "execution_id": "exec_abc",
                "duration_seconds": 42,
            },
        )
        assert result.success is True
        entries = await ledger.read_all("inst_a", "autonomy_loop_outcomes")
        assert entries[0]["execution_id"] == "exec_abc"
        assert entries[0]["duration_seconds"] == 42

    async def test_functional_autonomy_loop_turn_records_outcome(
        self, ledger, operator_env,
    ):
        """FUNCTIONAL pin (architect's user-feedback request): exercise
        the tool under expected end-of-workflow-turn use. Multiple
        consecutive turns produce a chronological outcome ledger
        readable by the catalog for before/after measurement."""
        # Turn 1: addresses pattern-A.
        r1 = await emit_autonomy_loop_event(
            ctx=_operator_ctx(),
            ledger=ledger,
            instance_id="inst_a",
            workflow_id="self_improvement",
            outcome="completed",
            addresses_friction_patterns=["pattern-A"],
        )
        # Turn 2: addresses pattern-B.
        r2 = await emit_autonomy_loop_event(
            ctx=_operator_ctx(),
            ledger=ledger,
            instance_id="inst_a",
            workflow_id="self_improvement",
            outcome="completed",
            addresses_friction_patterns=["pattern-B"],
        )
        assert r1.success and r2.success
        # Substrate state pin: ledger contains both entries in order.
        entries = await ledger.read_all("inst_a", "autonomy_loop_outcomes")
        assert len(entries) == 2
        assert entries[0]["addresses_friction_patterns"] == ["pattern-A"]
        assert entries[1]["addresses_friction_patterns"] == ["pattern-B"]


# ===========================================================================
# Audit event emission
# ===========================================================================


class TestAutonomyAuditTrail:
    """Each autonomy-loop tool emits a workflow_autonomy.action_recorded
    event for the durable audit trail."""

    async def test_transition_emits_audit_event(
        self, store, operator_env, event_capture,
    ):
        """FUNCTIONAL pin: a successful tool invocation produces an
        audit event in the event stream with the operation, actor, and
        payload. Audit consumers (operators, the future autonomy-loop
        traceability layer) read these to reconstruct what the
        autonomy loop did."""
        p = await store.create_pattern(
            instance_id="inst_a",
            description="audit test",
            signal_type_keys=["kaudit"],
        )
        await transition_friction_pattern_lifecycle(
            ctx=_operator_ctx(),
            pattern_store=store,
            instance_id="inst_a",
            pattern_id=p.pattern_id,
            new_state=LIFECYCLE_RESOLVED,
            resolved_by_spec="self_improvement",
        )
        # Find the audit event.
        autonomy_events = [
            e for e in event_capture
            if e["event_type"] == "workflow_autonomy.action_recorded"
        ]
        assert len(autonomy_events) == 1
        evt = autonomy_events[0]
        assert evt["payload"]["operation"] == "transition_friction_pattern_lifecycle"
        assert evt["payload"]["actor_kind"] == ACTOR_OPERATOR
        assert evt["payload"]["actor_id"] == OPERATOR_ID
        assert evt["payload"]["pattern_id"] == p.pattern_id
        assert evt["payload"]["new_state"] == LIFECYCLE_RESOLVED
        assert evt["payload"]["resolved_by_spec"] == "self_improvement"

    async def test_emit_event_records_audit_event(
        self, ledger, operator_env, event_capture,
    ):
        """Audit event emitted alongside the ledger entry."""
        await emit_autonomy_loop_event(
            ctx=_operator_ctx(),
            ledger=ledger,
            instance_id="inst_a",
            workflow_id="self_improvement",
            outcome="completed",
            addresses_friction_patterns=["pattern-x"],
        )
        autonomy_events = [
            e for e in event_capture
            if e["event_type"] == "workflow_autonomy.action_recorded"
        ]
        assert len(autonomy_events) == 1
        assert autonomy_events[0]["payload"]["operation"] == "emit_autonomy_loop_event"
        assert autonomy_events[0]["payload"]["outcome"] == "completed"

    async def test_failed_call_emits_no_audit_event(
        self, store, operator_env, event_capture,
    ):
        """A non-operator rejected call does NOT pollute the audit
        trail (no row landed; no event fired). The architect / operator
        can read the audit stream as a faithful record of accepted
        operations."""
        await transition_friction_pattern_lifecycle(
            ctx=_kernos_ctx(),
            pattern_store=store,
            instance_id="inst_a",
            pattern_id="some_pattern",
            new_state=LIFECYCLE_RESOLVED,
        )
        autonomy_events = [
            e for e in event_capture
            if e["event_type"] == "workflow_autonomy.action_recorded"
        ]
        assert autonomy_events == []


# ===========================================================================
# Dispatch handlers (tool-dispatch shape)
# ===========================================================================


class TestDispatchHandlers:
    """The handle_*_tool wrappers translate the kernel-tool dispatch
    shape (instance_id + member_id + args dict) into the direct
    function signatures."""

    async def test_handle_transition_via_dispatch_shape(
        self, store, operator_env,
    ):
        """FUNCTIONAL pin: workflow → call_tool → kernel-tool dispatch →
        handle_transition_friction_pattern_lifecycle_tool. End-to-end
        shape pins the production path."""
        p = await store.create_pattern(
            instance_id="inst_a",
            description="dispatch test",
            signal_type_keys=["kdisp1"],
        )
        result = await handle_transition_friction_pattern_lifecycle_tool(
            pattern_store=store,
            instance_id="inst_a",
            member_id=OPERATOR_ID,
            args={
                "pattern_id": p.pattern_id,
                "new_state": LIFECYCLE_RESOLVED,
                "resolved_by_spec": "self_improvement",
            },
        )
        assert result.success is True
        loaded = await store.get_pattern("inst_a", p.pattern_id)
        assert loaded.lifecycle_state == LIFECYCLE_RESOLVED

    async def test_handle_recurrence_via_dispatch_shape(
        self, store, operator_env, monkeypatch,
    ):
        monkeypatch.setenv("KERNOS_FRICTION_REACTIVATION_THRESHOLD", "1")
        monkeypatch.setenv(
            "KERNOS_FRICTION_REACTIVATION_WINDOW_DAYS", "365",
        )
        p = await store.create_pattern(
            instance_id="inst_a",
            description="dispatch recurrence",
            signal_type_keys=["kdisp2"],
        )
        await store.transition_lifecycle(
            "inst_a", p.pattern_id, LIFECYCLE_RESOLVED,
        )
        result = await handle_record_friction_pattern_recurrence_tool(
            pattern_store=store,
            instance_id="inst_a",
            member_id=OPERATOR_ID,
            args={
                "pattern_id": p.pattern_id,
                "observed_at": _now(),
                "report_path": "dispatch-test.md",
                "classified_by": CLASSIFIED_AUTO_SIGNAL_TYPE,
            },
        )
        assert result.success is True
        assert result.value is True  # triggered reactivation

    async def test_handle_emit_via_dispatch_shape(
        self, ledger, operator_env,
    ):
        result = await handle_emit_autonomy_loop_event_tool(
            ledger=ledger,
            instance_id="inst_a",
            member_id=OPERATOR_ID,
            args={
                "workflow_id": "self_improvement",
                "outcome": "completed",
                "addresses_friction_patterns": ["pattern-z"],
            },
        )
        assert result.success is True
        entries = await ledger.read_all("inst_a", "autonomy_loop_outcomes")
        assert len(entries) == 1
        assert entries[0]["addresses_friction_patterns"] == ["pattern-z"]

    async def test_handle_dispatch_kernos_member_id_rejected(
        self, store, operator_env,
    ):
        """member_id is not the operator id → derive_actor_kind returns
        KERNOS → operator gate rejects → CAT_AUTONOMY_NOT_AUTHORIZED."""
        p = await store.create_pattern(
            instance_id="inst_a",
            description="kernos rejected",
            signal_type_keys=["kdisp3"],
        )
        result = await handle_transition_friction_pattern_lifecycle_tool(
            pattern_store=store,
            instance_id="inst_a",
            member_id="mem_random_user",  # not the operator id
            args={
                "pattern_id": p.pattern_id,
                "new_state": LIFECYCLE_RESOLVED,
            },
        )
        assert result.success is False
        assert result.errors[0].category == CAT_AUTONOMY_NOT_AUTHORIZED


# ===========================================================================
# Module-level invariants
# ===========================================================================


class TestModuleInvariants:
    def test_valid_autonomy_tool_names_pinned(self):
        """Pure-API probe (no substrate). Pins the canonical tool name
        set — drift here would silently break the call_tool routing in
        commit 6."""
        assert VALID_AUTONOMY_TOOL_NAMES == frozenset({
            "transition_friction_pattern_lifecycle",
            "record_friction_pattern_recurrence",
            "emit_autonomy_loop_event",
        })

    def test_autonomy_tool_names_overlap_substrate_tool_ids(self):
        """Cross-module invariant: every autonomy tool name appears in
        the Spec 5 SUBSTRATE_TOOL_IDS classifier set, so the governance
        classifier correctly flags workflows calling these tools."""
        from kernos.kernel.workflows.authoring import SUBSTRATE_TOOL_IDS

        assert VALID_AUTONOMY_TOOL_NAMES.issubset(SUBSTRATE_TOOL_IDS)
