"""WTC v1 C5a — CRB Compiler descriptor.triggers translation.

Pins:

* Minimal trigger ``{"event_type": "X"}`` translates to
  ``on(event_type==X)`` with default DispatchPolicy. Backward-
  compatible with shipped CRB v1 fixtures.
* Rich trigger ``{"event_type", "event_selector",
  "temporal_relation", "dispatch_policy"}`` produces the right
  TriggerPredicate.
* Invalid temporal/dispatch shapes raise typed errors.
* Trigger ID derivation is deterministic (same descriptor →
  same ids).
* End-to-end: compile descriptor.triggers → register each with
  TriggerEvaluationRuntime → matching event fires the workflow.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from kernos.kernel import event_stream
from kernos.kernel.triggers import (
    CompiledTrigger,
    DispatchPolicy,
    InternalEventAdapter,
    TemporalRelation,
    TriggerEvaluationRuntime,
    TriggerPredicate,
    compile_descriptor_triggers,
    compile_trigger_descriptor,
    derive_trigger_id,
)
from kernos.kernel.triggers.errors import (
    DispatchPolicyError,
    PredicateValidationError,
    TemporalRelationError,
)


# ---------------------------------------------------------------------------
# Per-trigger translation
# ---------------------------------------------------------------------------


def test_minimal_trigger_translates_to_on_eq_event_type():
    pred = compile_trigger_descriptor({"event_type": "user.message"})
    assert pred.event_selector == {
        "op": "eq", "path": "event_type", "value": "user.message",
    }
    assert pred.temporal_relation.kind == "on"
    assert pred.temporal_relation.minutes == 0
    assert pred.temporal_relation.cron_expression == ""
    # Default DispatchPolicy — no override.
    assert pred.dispatch_policy == DispatchPolicy()


def test_rich_trigger_with_explicit_temporal_relation():
    pred = compile_trigger_descriptor({
        "event_type": "schedule.tick",
        "temporal_relation": {
            "kind": "every", "cron_expression": "*/5 * * * *",
        },
    })
    assert pred.temporal_relation.kind == "every"
    assert pred.temporal_relation.cron_expression == "*/5 * * * *"


def test_rich_trigger_with_event_selector_override():
    """When event_selector is supplied, it replaces the default
    event_type-eq selector — predicate authors needing AND/OR
    composition can pass a richer AST."""
    selector = {
        "op": "AND", "operands": [
            {"op": "eq", "path": "event_type",
             "value": "email.message_observed"},
            {"op": "eq", "path": "payload.from_address",
             "value": "kit@anthropic.com"},
        ],
    }
    pred = compile_trigger_descriptor({
        "event_type": "email.message_observed",
        "event_selector": selector,
    })
    assert pred.event_selector == selector


def test_rich_trigger_with_dispatch_policy_override():
    pred = compile_trigger_descriptor({
        "event_type": "user.message",
        "dispatch_policy": {
            "dedup_window_seconds": 60,
            "missed_window": "skip",
            "retry_on_dispatch_failure": 5,
        },
    })
    assert pred.dispatch_policy.dedup_window_seconds == 60
    assert pred.dispatch_policy.missed_window == "skip"
    assert pred.dispatch_policy.retry_on_dispatch_failure == 5


def test_before_temporal_relation_with_minutes():
    pred = compile_trigger_descriptor({
        "event_type": "calendar.event_observed",
        "temporal_relation": {"kind": "before", "minutes": 30},
    })
    assert pred.temporal_relation.kind == "before"
    assert pred.temporal_relation.minutes == 30


def test_after_temporal_relation_with_minutes():
    pred = compile_trigger_descriptor({
        "event_type": "calendar.event_observed",
        "temporal_relation": {"kind": "after", "minutes": 15},
    })
    assert pred.temporal_relation.kind == "after"
    assert pred.temporal_relation.minutes == 15


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_missing_event_type_raises():
    with pytest.raises(PredicateValidationError, match="event_type"):
        compile_trigger_descriptor({})


def test_non_dict_trigger_raises():
    with pytest.raises(PredicateValidationError, match="must be a dict"):
        compile_trigger_descriptor("not-a-dict")  # type: ignore[arg-type]


def test_invalid_temporal_kind_raises():
    with pytest.raises(TemporalRelationError):
        compile_trigger_descriptor({
            "event_type": "x",
            "temporal_relation": {"kind": "bogus"},
        })


def test_every_without_cron_raises():
    with pytest.raises(TemporalRelationError, match="cron_expression"):
        compile_trigger_descriptor({
            "event_type": "x",
            "temporal_relation": {"kind": "every"},
        })


def test_before_without_minutes_raises():
    with pytest.raises(TemporalRelationError, match="minutes"):
        compile_trigger_descriptor({
            "event_type": "x",
            "temporal_relation": {"kind": "before"},
        })


def test_dispatch_policy_invalid_missed_window_raises():
    with pytest.raises(DispatchPolicyError):
        compile_trigger_descriptor({
            "event_type": "x",
            "dispatch_policy": {"missed_window": "bogus"},
        })


def test_event_selector_must_be_dict():
    with pytest.raises(PredicateValidationError, match="event_selector"):
        compile_trigger_descriptor({
            "event_type": "x",
            "event_selector": "not-a-dict",
        })


# ---------------------------------------------------------------------------
# Workflow-level compilation
# ---------------------------------------------------------------------------


def test_compile_descriptor_triggers_returns_compiled_per_entry():
    compiled = compile_descriptor_triggers(
        workflow_id="wf_abc",
        descriptor={
            "triggers": [
                {"event_type": "user.message"},
                {
                    "event_type": "schedule.tick",
                    "temporal_relation": {
                        "kind": "every", "cron_expression": "0 * * * *",
                    },
                },
            ],
        },
    )
    assert len(compiled) == 2
    assert all(isinstance(t, CompiledTrigger) for t in compiled)
    assert all(t.workflow_id == "wf_abc" for t in compiled)
    # Distinct trigger ids — different predicates.
    assert compiled[0].trigger_id != compiled[1].trigger_id


def test_compile_descriptor_triggers_is_deterministic():
    """Same descriptor → same trigger_ids. Re-registration of an
    unchanged workflow doesn't churn ids."""
    descriptor = {
        "triggers": [
            {"event_type": "user.message"},
            {"event_type": "schedule.tick"},
        ],
    }
    a = compile_descriptor_triggers(
        workflow_id="wf_x", descriptor=descriptor,
    )
    b = compile_descriptor_triggers(
        workflow_id="wf_x", descriptor=descriptor,
    )
    assert [t.trigger_id for t in a] == [t.trigger_id for t in b]


def test_compile_descriptor_triggers_distinct_ids_per_workflow():
    """Different workflow_ids produce different trigger_ids even
    for the same descriptor.triggers shape."""
    descriptor = {"triggers": [{"event_type": "x"}]}
    a = compile_descriptor_triggers(
        workflow_id="wf_a", descriptor=descriptor,
    )
    b = compile_descriptor_triggers(
        workflow_id="wf_b", descriptor=descriptor,
    )
    assert a[0].trigger_id != b[0].trigger_id


def test_compile_descriptor_triggers_index_position_matters():
    """Index position is part of the fingerprint — same predicate
    in slot 0 vs slot 1 produces different ids."""
    descriptor = {
        "triggers": [
            {"event_type": "x"},
            {"event_type": "x"},
        ],
    }
    compiled = compile_descriptor_triggers(
        workflow_id="wf", descriptor=descriptor,
    )
    assert compiled[0].trigger_id != compiled[1].trigger_id


def test_empty_triggers_list_raises():
    with pytest.raises(PredicateValidationError, match="non-empty"):
        compile_descriptor_triggers(
            workflow_id="wf", descriptor={"triggers": []},
        )


def test_missing_triggers_field_raises():
    with pytest.raises(PredicateValidationError, match="must be a list"):
        compile_descriptor_triggers(
            workflow_id="wf", descriptor={},
        )


def test_per_entry_error_carries_index():
    """A bad entry mid-descriptor raises with index context so
    operators can locate the offending entry without spelunking."""
    with pytest.raises(
        TemporalRelationError, match=r"descriptor\.triggers\[1\]",
    ):
        compile_descriptor_triggers(
            workflow_id="wf",
            descriptor={
                "triggers": [
                    {"event_type": "ok"},
                    {
                        "event_type": "bad",
                        "temporal_relation": {"kind": "bogus"},
                    },
                ],
            },
        )


def test_workflow_id_required():
    with pytest.raises(PredicateValidationError, match="workflow_id"):
        compile_descriptor_triggers(
            workflow_id="", descriptor={"triggers": [{"event_type": "x"}]},
        )


# ---------------------------------------------------------------------------
# End-to-end: compile → register → fire
# ---------------------------------------------------------------------------


class _StubWLP:
    def __init__(self) -> None:
        self.executions: dict[str, str] = {}
        self.dispatch_calls: list[dict] = []

    async def execute_workflow(
        self,
        *,
        fire_id: str,
        workflow_id: str,
        instance_id: str,
        trigger_event_payload: Any = None,
        member_id: str = "",
        **kwargs: Any,
    ) -> str:
        self.dispatch_calls.append({
            "fire_id": fire_id, "workflow_id": workflow_id,
        })
        if fire_id in self.executions:
            return self.executions[fire_id]
        execution_id = f"exec_{uuid.uuid4().hex[:8]}"
        self.executions[fire_id] = execution_id
        return execution_id

    async def find_execution_by_fire_id(self, fire_id: str) -> str | None:
        return self.executions.get(fire_id)


@pytest.fixture
async def event_stream_started(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path), flush_interval_s=0.05)
    yield
    await event_stream.stop_writer()
    await event_stream._reset_for_tests()


async def test_compile_then_register_fires_on_matching_event(
    tmp_path, event_stream_started,
):
    """End-to-end: a CRB descriptor with a single trigger gets
    compiled, registered, and fires through the runtime when a
    matching event flushes."""
    wlp = _StubWLP()
    runtime = TriggerEvaluationRuntime()
    await runtime.start(
        data_dir=str(tmp_path),
        heartbeat_seconds=1,
        wlp_dispatch=wlp.execute_workflow,
        wlp_lookup_by_fire_id=wlp.find_execution_by_fire_id,
    )
    adapter = InternalEventAdapter(runtime)
    await adapter.start()

    try:
        descriptor = {
            "triggers": [{"event_type": "user.message"}],
        }
        compiled = compile_descriptor_triggers(
            workflow_id="wf_crb_demo", descriptor=descriptor,
        )
        for ct in compiled:
            await runtime.register(
                trigger_id=ct.trigger_id,
                instance_id="inst1",
                workflow_id=ct.workflow_id,
                predicate=ct.predicate,
            )

        await event_stream.emit(
            instance_id="inst1",
            event_type="user.message",
            payload={"text": "hi"},
        )
        await event_stream.flush_now()

        assert len(wlp.dispatch_calls) == 1
        assert wlp.dispatch_calls[0]["workflow_id"] == "wf_crb_demo"
    finally:
        await adapter.stop()
        await runtime.stop()


async def test_compile_then_register_multiple_triggers_independent(
    tmp_path, event_stream_started,
):
    """A workflow with two triggers — different event_types — fires
    once per matching event independently."""
    wlp = _StubWLP()
    runtime = TriggerEvaluationRuntime()
    await runtime.start(
        data_dir=str(tmp_path),
        heartbeat_seconds=1,
        wlp_dispatch=wlp.execute_workflow,
        wlp_lookup_by_fire_id=wlp.find_execution_by_fire_id,
    )
    adapter = InternalEventAdapter(runtime)
    await adapter.start()

    try:
        descriptor = {
            "triggers": [
                {"event_type": "user.message"},
                {"event_type": "page.edit"},
            ],
        }
        compiled = compile_descriptor_triggers(
            workflow_id="wf_two", descriptor=descriptor,
        )
        for ct in compiled:
            await runtime.register(
                trigger_id=ct.trigger_id,
                instance_id="inst1",
                workflow_id=ct.workflow_id,
                predicate=ct.predicate,
            )

        await event_stream.emit(
            instance_id="inst1",
            event_type="user.message",
            payload={},
        )
        await event_stream.emit(
            instance_id="inst1",
            event_type="page.edit",
            payload={},
        )
        await event_stream.flush_now()

        # Two fires, both for the same workflow_id, but distinct
        # fire_ids (different trigger_ids).
        assert len(wlp.dispatch_calls) == 2
        fire_ids = {c["fire_id"] for c in wlp.dispatch_calls}
        assert len(fire_ids) == 2
    finally:
        await adapter.stop()
        await runtime.stop()


# ---------------------------------------------------------------------------
# derive_trigger_id — direct unit test
# ---------------------------------------------------------------------------


def test_derive_trigger_id_requires_workflow_id():
    with pytest.raises(PredicateValidationError, match="workflow_id"):
        derive_trigger_id(
            workflow_id="",
            index=0,
            predicate=TriggerPredicate(
                event_selector={
                    "op": "eq", "path": "event_type", "value": "x",
                },
                temporal_relation=TemporalRelation(kind="on"),
                dispatch_policy=DispatchPolicy(),
            ),
        )


def test_derive_trigger_id_stable_format():
    tid = derive_trigger_id(
        workflow_id="wf",
        index=0,
        predicate=TriggerPredicate(
            event_selector={"op": "eq", "path": "event_type", "value": "x"},
            temporal_relation=TemporalRelation(kind="on"),
            dispatch_policy=DispatchPolicy(),
        ),
    )
    assert tid.startswith("trig_")
    # 5-char prefix + 16 hex chars.
    assert len(tid) == 5 + 16
