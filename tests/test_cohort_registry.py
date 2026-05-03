"""Tests for the cohort descriptor + registry.

Covers Section 1, 2, 3 of the COHORT-FAN-OUT-RUNNER spec:
- CohortDescriptor / CohortContext / ContextSpaceRef / Turn validation
- Registry register / has / get / list_cohorts
- Sync-callable rejection (the design review edit #1, scenario 3)
- ExecutionMode validation (the design review edit #2: only ASYNC accepted; THREAD
  reserved with clear error)
- Duplicate-id rejection
- cohort_id snake_case validation
"""

from __future__ import annotations

import asyncio

import pytest

from kernos.kernel.cohorts import (
    CohortContext,
    CohortDescriptor,
    CohortDescriptorError,
    CohortFanOutResult,
    CohortRegistry,
    ContextSpaceRef,
    ExecutionMode,
    Turn,
)
from kernos.kernel.integration.briefing import (
    CohortOutput,
    Public,
    Restricted,
)


# ---------------------------------------------------------------------------
# Turn / ContextSpaceRef
# ---------------------------------------------------------------------------


def test_turn_to_api_dict_round_trip():
    t = Turn(role="user", content="hi")
    assert t.to_api_dict() == {"role": "user", "content": "hi"}


def test_turn_rejects_empty_role():
    with pytest.raises(CohortDescriptorError, match="role"):
        Turn(role="", content="hi")


def test_context_space_ref_default_domain_is_empty():
    s = ContextSpaceRef(space_id="default")
    assert s.domain == ""


def test_context_space_ref_rejects_empty_id():
    with pytest.raises(CohortDescriptorError, match="space_id"):
        ContextSpaceRef(space_id="")


# ---------------------------------------------------------------------------
# CohortContext
# ---------------------------------------------------------------------------


def _ctx(**overrides) -> CohortContext:
    base = dict(
        member_id="m-1",
        user_message="hi",
        conversation_thread=(),
        active_spaces=(),
        turn_id="turn-1",
        instance_id="inst-1",
        produced_at="2026-04-26T00:00:00+00:00",
    )
    base.update(overrides)
    return CohortContext(**base)


def test_cohort_context_happy_path():
    c = _ctx(
        conversation_thread=(Turn("user", "hi"), Turn("assistant", "hello")),
        active_spaces=(ContextSpaceRef("default"),),
    )
    assert c.member_id == "m-1"
    assert c.turn_id == "turn-1"
    assert len(c.conversation_thread) == 2


def test_cohort_context_rejects_empty_member_id():
    with pytest.raises(CohortDescriptorError, match="member_id"):
        _ctx(member_id="")


def test_cohort_context_rejects_empty_turn_id():
    with pytest.raises(CohortDescriptorError, match="turn_id"):
        _ctx(turn_id="")


def test_cohort_context_rejects_non_turn_in_thread():
    with pytest.raises(CohortDescriptorError, match="conversation_thread"):
        _ctx(conversation_thread=({"role": "user", "content": "hi"},))  # type: ignore[arg-type]


def test_cohort_context_rejects_non_space_ref_in_active_spaces():
    with pytest.raises(CohortDescriptorError, match="active_spaces"):
        _ctx(active_spaces=({"space_id": "default"},))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CohortFanOutResult
# ---------------------------------------------------------------------------


def _output(cohort_id: str = "x") -> CohortOutput:
    return CohortOutput(
        cohort_id=cohort_id,
        cohort_run_id=f"turn-1:{cohort_id}:0",
        output={},
    )


def test_fan_out_result_degraded_property():
    r = CohortFanOutResult(
        outputs=(_output(),),
        fan_out_started_at="2026-04-26T00:00:00+00:00",
        fan_out_completed_at="2026-04-26T00:00:01+00:00",
        required_cohort_failures=("memory",),
    )
    assert r.degraded is True
    assert r.safety_degraded is False


def test_fan_out_result_safety_degraded_property():
    r = CohortFanOutResult(
        outputs=(_output(),),
        fan_out_started_at="2026-04-26T00:00:00+00:00",
        fan_out_completed_at="2026-04-26T00:00:01+00:00",
        required_cohort_failures=("covenant",),
        required_safety_cohort_failures=("covenant",),
    )
    assert r.degraded is True
    assert r.safety_degraded is True


def test_fan_out_result_to_dict_round_trip_shape():
    r = CohortFanOutResult(
        outputs=(_output("memory"), _output("weather")),
        fan_out_started_at="2026-04-26T00:00:00+00:00",
        fan_out_completed_at="2026-04-26T00:00:01+00:00",
        global_timeout_engaged=True,
        required_cohort_failures=("memory",),
        required_safety_cohort_failures=(),
    )
    d = r.to_dict()
    assert len(d["outputs"]) == 2
    assert d["global_timeout_engaged"] is True
    assert d["required_cohort_failures"] == ["memory"]


def test_fan_out_result_rejects_non_cohort_output():
    with pytest.raises(CohortDescriptorError, match="outputs"):
        CohortFanOutResult(
            outputs=("not-an-output",),  # type: ignore[arg-type]
            fan_out_started_at="t0",
            fan_out_completed_at="t1",
        )


# ---------------------------------------------------------------------------
# Registry — happy path
# ---------------------------------------------------------------------------


async def _async_run(ctx: CohortContext) -> CohortOutput:
    return CohortOutput(
        cohort_id="memory",
        cohort_run_id="turn-1:memory:0",
        output={"hits": []},
    )


def test_registry_register_async_cohort():
    r = CohortRegistry()
    desc = CohortDescriptor(
        cohort_id="memory",
        run=_async_run,
        timeout_ms=500,
    )
    r.register(desc)
    assert r.has("memory")
    assert len(r) == 1
    assert r.get("memory") is desc


def test_registry_list_preserves_registration_order():
    r = CohortRegistry()

    async def run_a(ctx):  # noqa: D401
        return _output("a")

    async def run_b(ctx):
        return _output("b")

    async def run_c(ctx):
        return _output("c")

    r.register(CohortDescriptor(cohort_id="a", run=run_a))
    r.register(CohortDescriptor(cohort_id="b", run=run_b))
    r.register(CohortDescriptor(cohort_id="c", run=run_c))
    ids = [d.cohort_id for d in r.list_cohorts()]
    assert ids == ["a", "b", "c"]


def test_registry_get_unknown_raises():
    r = CohortRegistry()
    with pytest.raises(CohortDescriptorError, match="not registered"):
        r.get("ghost")


# ---------------------------------------------------------------------------
# Registry — the design review edit #1: sync rejection
# ---------------------------------------------------------------------------


def test_registry_rejects_sync_callable():
    r = CohortRegistry()

    def sync_run(ctx):  # plain def, NOT async
        return _output()

    desc = CohortDescriptor(cohort_id="bad", run=sync_run)
    with pytest.raises(CohortDescriptorError) as excinfo:
        r.register(desc)
    msg = str(excinfo.value)
    assert "bad" in msg
    assert "async" in msg.lower()
    assert "loop.run_in_executor" in msg
    assert not r.has("bad")


def test_registry_rejects_lambda():
    r = CohortRegistry()
    desc = CohortDescriptor(
        cohort_id="lambda_cohort",
        run=lambda ctx: _output(),  # type: ignore[arg-type]
    )
    with pytest.raises(CohortDescriptorError, match="async"):
        r.register(desc)


def test_registry_rejects_non_callable():
    r = CohortRegistry()
    desc = CohortDescriptor(cohort_id="x", run="not a callable")  # type: ignore[arg-type]
    with pytest.raises(CohortDescriptorError, match="callable"):
        r.register(desc)


# ---------------------------------------------------------------------------
# Registry — the design review edit #2: ExecutionMode validation
# ---------------------------------------------------------------------------


def test_registry_rejects_thread_execution_mode_with_future_spec_pointer():
    r = CohortRegistry()
    desc = CohortDescriptor(
        cohort_id="threaded",
        run=_async_run,
        execution_mode=ExecutionMode.THREAD,
    )
    with pytest.raises(CohortDescriptorError) as excinfo:
        r.register(desc)
    msg = str(excinfo.value)
    assert "thread" in msg.lower()
    assert "future spec" in msg.lower() or "future" in msg.lower()
    assert "loop.run_in_executor" in msg


def test_registry_accepts_async_execution_mode_explicitly():
    r = CohortRegistry()
    desc = CohortDescriptor(
        cohort_id="explicit_async",
        run=_async_run,
        execution_mode=ExecutionMode.ASYNC,
    )
    r.register(desc)
    assert r.has("explicit_async")


# ---------------------------------------------------------------------------
# Registry — uniqueness + id format
# ---------------------------------------------------------------------------


def test_registry_rejects_duplicate_cohort_id():
    r = CohortRegistry()
    r.register(CohortDescriptor(cohort_id="memory", run=_async_run))
    with pytest.raises(CohortDescriptorError, match="already registered"):
        r.register(CohortDescriptor(cohort_id="memory", run=_async_run))


def test_registry_rejects_invalid_cohort_id_format():
    r = CohortRegistry()
    for bad in ["Memory", "memory-cohort", "1memory", ""]:
        with pytest.raises(CohortDescriptorError, match="snake_case"):
            r.register(
                CohortDescriptor(cohort_id=bad, run=_async_run)
            )


def test_registry_rejects_zero_or_negative_timeout():
    r = CohortRegistry()
    with pytest.raises(CohortDescriptorError, match="timeout_ms"):
        r.register(
            CohortDescriptor(cohort_id="x", run=_async_run, timeout_ms=0)
        )
    with pytest.raises(CohortDescriptorError, match="timeout_ms"):
        r.register(
            CohortDescriptor(cohort_id="y", run=_async_run, timeout_ms=-100)
        )


# ---------------------------------------------------------------------------
# Registry — required + safety_class flags propagate correctly
# ---------------------------------------------------------------------------


def test_registry_preserves_required_and_safety_class():
    r = CohortRegistry()
    r.register(
        CohortDescriptor(
            cohort_id="covenant",
            run=_async_run,
            required=True,
            safety_class=True,
            default_visibility=Restricted(reason="covenant"),
        )
    )
    desc = r.get("covenant")
    assert desc.required is True
    assert desc.safety_class is True
    assert isinstance(desc.default_visibility, Restricted)


def test_registry_default_required_is_false_default_safety_class_is_false():
    r = CohortRegistry()
    r.register(CohortDescriptor(cohort_id="weather", run=_async_run))
    desc = r.get("weather")
    assert desc.required is False
    assert desc.safety_class is False
    assert isinstance(desc.default_visibility, Public)
