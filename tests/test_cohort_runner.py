"""Tests for the cohort fan-out runner.

Covers Sections 4, 5, 5a, 7, 10 of the COHORT-FAN-OUT-RUNNER spec
plus the design review edits #3 (asyncio.wait), #6 (global wall-clock cap), and
the synthetic-output shape from the design review edit #4.

Live-test scenarios 1-16 from the spec are exercised here. Scenarios
17-18 (integration with V1 IntegrationRunner) live in
test_cohort_runner_integration.py.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from kernos.kernel.cohorts import (
    CohortContext,
    CohortDescriptor,
    CohortFanOutConfig,
    CohortFanOutResult,
    CohortFanOutRunner,
    CohortRegistry,
    ContextSpaceRef,
    Turn,
)
from kernos.kernel.integration.briefing import (
    CohortOutput,
    Outcome,
    Public,
    Restricted,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ctx(**overrides) -> CohortContext:
    base = dict(
        member_id="m-1",
        user_message="hello",
        conversation_thread=(Turn("user", "hello"),),
        active_spaces=(ContextSpaceRef("default"),),
        turn_id="turn-1",
        instance_id="inst-1",
        produced_at="2026-04-26T00:00:00+00:00",
    )
    base.update(overrides)
    return CohortContext(**base)


def _ok_output(cohort_id: str = "memory", payload: dict | None = None) -> CohortOutput:
    return CohortOutput(
        cohort_id=cohort_id,
        cohort_run_id="ignored-runner-mints",
        output=payload or {"hits": []},
    )


def _make_runner(
    registry: CohortRegistry,
    *,
    config: CohortFanOutConfig | None = None,
) -> tuple[CohortFanOutRunner, list[dict]]:
    sink: list[dict] = []

    async def emit(entry: dict) -> None:
        sink.append(entry)

    runner = CohortFanOutRunner(
        registry=registry,
        audit_emitter=emit,
        config=config or CohortFanOutConfig(global_timeout_seconds=2.0),
    )
    return runner, sink


# ---------------------------------------------------------------------------
# Scenario 1: single async cohort, clean run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_async_cohort_clean_run():
    async def run(ctx):
        return _ok_output("memory", {"hits": ["alice"]})

    r = CohortRegistry()
    r.register(CohortDescriptor(cohort_id="memory", run=run, timeout_ms=500))

    runner, audit = _make_runner(r)
    result = await runner.run(_ctx())

    assert isinstance(result, CohortFanOutResult)
    assert len(result.outputs) == 1
    out = result.outputs[0]
    assert out.cohort_id == "memory"
    assert out.outcome is Outcome.SUCCESS
    assert out.is_synthetic is False
    assert out.output == {"hits": ["alice"]}
    assert len(audit) == 1
    assert audit[0]["audit_category"] == "cohort.fan_out"


# ---------------------------------------------------------------------------
# Scenario 2: multiple cohorts, all clean, registration order preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_cohorts_all_clean_in_registration_order():
    async def run_a(ctx):
        await asyncio.sleep(0.02)
        return _ok_output("a", {"x": 1})

    async def run_b(ctx):
        return _ok_output("b", {"y": 2})

    async def run_c(ctx):
        await asyncio.sleep(0.01)
        return _ok_output("c", {"z": 3})

    r = CohortRegistry()
    r.register(CohortDescriptor(cohort_id="a", run=run_a, timeout_ms=500))
    r.register(CohortDescriptor(cohort_id="b", run=run_b, timeout_ms=500))
    r.register(CohortDescriptor(cohort_id="c", run=run_c, timeout_ms=500))

    runner, _ = _make_runner(r)
    result = await runner.run(_ctx())

    assert [o.cohort_id for o in result.outputs] == ["a", "b", "c"]
    assert all(o.outcome is Outcome.SUCCESS for o in result.outputs)


# ---------------------------------------------------------------------------
# Scenario 4: per-cohort timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_cohort_timeout_isolates_to_that_cohort():
    async def run_slow(ctx):
        await asyncio.sleep(1.0)  # > timeout_ms below
        return _ok_output("slow")

    async def run_fast(ctx):
        return _ok_output("fast", {"ok": True})

    r = CohortRegistry()
    r.register(CohortDescriptor(cohort_id="slow", run=run_slow, timeout_ms=50))
    r.register(CohortDescriptor(cohort_id="fast", run=run_fast, timeout_ms=500))

    runner, audit = _make_runner(r)
    result = await runner.run(_ctx())

    by_id = {o.cohort_id: o for o in result.outputs}
    assert by_id["slow"].outcome is Outcome.TIMEOUT_PER_COHORT
    assert by_id["slow"].output == {}
    assert "50ms" in by_id["slow"].error_summary
    assert by_id["fast"].outcome is Outcome.SUCCESS
    assert by_id["fast"].output == {"ok": True}


# ---------------------------------------------------------------------------
# Scenario 5: cohort exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cohort_exception_caught_with_redacted_summary():
    async def run_boom(ctx):
        raise RuntimeError("auth failed for sk-abcdef0123456789xyzfoobar")

    async def run_ok(ctx):
        return _ok_output("ok", {"ok": True})

    r = CohortRegistry()
    r.register(CohortDescriptor(cohort_id="boom", run=run_boom, timeout_ms=500))
    r.register(CohortDescriptor(cohort_id="ok", run=run_ok, timeout_ms=500))

    runner, _ = _make_runner(r)
    result = await runner.run(_ctx())

    by_id = {o.cohort_id: o for o in result.outputs}
    assert by_id["boom"].outcome is Outcome.ERROR
    assert by_id["boom"].output == {}
    # Sanity: secret pattern stripped
    assert "sk-abcdef" not in by_id["boom"].error_summary
    assert "RuntimeError" in by_id["boom"].error_summary
    # Other cohort completely unaffected
    assert by_id["ok"].outcome is Outcome.SUCCESS


# ---------------------------------------------------------------------------
# Scenario 6: failure isolation (cooperative)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_isolation_three_paths():
    async def run_timeout(ctx):
        await asyncio.sleep(1.0)
        return _ok_output("timeout")

    async def run_error(ctx):
        raise ValueError("boom")

    async def run_ok(ctx):
        return _ok_output("ok", {"value": 42})

    r = CohortRegistry()
    r.register(CohortDescriptor(cohort_id="t_out", run=run_timeout, timeout_ms=50))
    r.register(CohortDescriptor(cohort_id="err", run=run_error, timeout_ms=500))
    r.register(CohortDescriptor(cohort_id="ok", run=run_ok, timeout_ms=500))

    runner, _ = _make_runner(r)
    result = await runner.run(_ctx())

    by_id = {o.cohort_id: o for o in result.outputs}
    assert by_id["t_out"].outcome is Outcome.TIMEOUT_PER_COHORT
    assert by_id["err"].outcome is Outcome.ERROR
    assert by_id["ok"].outcome is Outcome.SUCCESS
    assert by_id["ok"].output == {"value": 42}


# ---------------------------------------------------------------------------
# Scenario 7: global wall-clock exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_wall_clock_cap_cancels_pending():
    async def run_slow(ctx):
        await asyncio.sleep(2.0)
        return _ok_output("slow")

    async def run_ok(ctx):
        await asyncio.sleep(0.05)
        return _ok_output("ok")

    r = CohortRegistry()
    r.register(CohortDescriptor(cohort_id="slow", run=run_slow, timeout_ms=5000))
    r.register(CohortDescriptor(cohort_id="ok", run=run_ok, timeout_ms=5000))

    runner, _ = _make_runner(
        r, config=CohortFanOutConfig(global_timeout_seconds=0.2)
    )
    result = await runner.run(_ctx())

    assert result.global_timeout_engaged is True
    by_id = {o.cohort_id: o for o in result.outputs}
    assert by_id["slow"].outcome is Outcome.TIMEOUT_GLOBAL
    assert by_id["ok"].outcome is Outcome.SUCCESS


# ---------------------------------------------------------------------------
# Scenario 8: required cohort failure (non-safety)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_required_cohort_failure_lands_in_required_failures_list():
    async def run_required_fail(ctx):
        raise RuntimeError("memory backend down")

    async def run_optional_fail(ctx):
        raise RuntimeError("weather api down")

    async def run_ok(ctx):
        return _ok_output("ok")

    r = CohortRegistry()
    r.register(
        CohortDescriptor(
            cohort_id="memory",
            run=run_required_fail,
            timeout_ms=500,
            required=True,
        )
    )
    r.register(
        CohortDescriptor(
            cohort_id="weather",
            run=run_optional_fail,
            timeout_ms=500,
            required=False,
        )
    )
    r.register(CohortDescriptor(cohort_id="ok", run=run_ok, timeout_ms=500))

    runner, _ = _make_runner(r)
    result = await runner.run(_ctx())

    assert result.required_cohort_failures == ("memory",)
    assert result.required_safety_cohort_failures == ()
    assert result.degraded is True
    assert result.safety_degraded is False


# ---------------------------------------------------------------------------
# Scenario 9: required safety cohort failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_required_safety_cohort_failure_in_both_lists():
    async def run_safety_fail(ctx):
        raise RuntimeError("covenant lookup failed")

    r = CohortRegistry()
    r.register(
        CohortDescriptor(
            cohort_id="covenant",
            run=run_safety_fail,
            timeout_ms=500,
            required=True,
            safety_class=True,
            default_visibility=Restricted(reason="covenant"),
        )
    )

    runner, _ = _make_runner(r)
    result = await runner.run(_ctx())

    assert result.required_cohort_failures == ("covenant",)
    assert result.required_safety_cohort_failures == ("covenant",)
    assert result.degraded is True
    assert result.safety_degraded is True


# ---------------------------------------------------------------------------
# Scenario 10: redaction policy on error_summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_summary_strips_each_redaction_pattern():
    async def run_bad(ctx):
        raise RuntimeError(
            "Authorization: Bearer xoxb-123-secret-foo with key "
            "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA at "
            "/home/u/.config/kernos/credentials/notion.json"
        )

    r = CohortRegistry()
    r.register(CohortDescriptor(cohort_id="bad", run=run_bad, timeout_ms=500))

    runner, _ = _make_runner(r)
    result = await runner.run(_ctx())
    summary = result.outputs[0].error_summary

    assert "xoxb-" not in summary
    assert "ghp_" not in summary
    assert "Authorization" not in summary
    assert "credentials/notion.json" not in summary


# ---------------------------------------------------------------------------
# Scenario 11: no stack traces in CohortOutput
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_stack_traces_in_cohort_output():
    async def run_with_frames(ctx):
        def _inner():
            raise RuntimeError("inner blew up")
        _inner()

    r = CohortRegistry()
    r.register(
        CohortDescriptor(cohort_id="framed", run=run_with_frames, timeout_ms=500)
    )

    runner, _ = _make_runner(r)
    result = await runner.run(_ctx())
    out = result.outputs[0]

    serialised_str = str(out.to_dict())
    assert "Traceback" not in serialised_str
    assert "  File \"" not in serialised_str
    assert "inner blew up" in out.error_summary


# ---------------------------------------------------------------------------
# Scenario 12: audit log capture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_captures_per_cohort_outcome_and_timing():
    async def run_ok(ctx):
        return _ok_output("ok")

    async def run_err(ctx):
        raise ValueError("nope")

    r = CohortRegistry()
    r.register(CohortDescriptor(cohort_id="ok", run=run_ok, timeout_ms=500))
    r.register(CohortDescriptor(cohort_id="err", run=run_err, timeout_ms=500))

    runner, audit = _make_runner(r)
    await runner.run(_ctx())

    assert len(audit) == 1
    entry = audit[0]
    assert entry["audit_category"] == "cohort.fan_out"
    assert entry["registered_cohort_ids"] == ["ok", "err"]
    outcomes = {o["cohort_id"]: o for o in entry["outcomes"]}
    assert outcomes["ok"]["outcome"] == "success"
    assert outcomes["err"]["outcome"] == "error"
    assert "duration_ms" in outcomes["ok"]
    assert outcomes["err"]["error_summary"]


# ---------------------------------------------------------------------------
# Scenario 13: outputs preserve registration order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outputs_preserve_registration_order_regardless_of_completion():
    async def slow(ctx):
        await asyncio.sleep(0.05)
        return _ok_output("slow")

    async def fast(ctx):
        return _ok_output("fast")

    async def medium(ctx):
        await asyncio.sleep(0.025)
        return _ok_output("medium")

    r = CohortRegistry()
    r.register(CohortDescriptor(cohort_id="slow", run=slow, timeout_ms=500))
    r.register(CohortDescriptor(cohort_id="fast", run=fast, timeout_ms=500))
    r.register(CohortDescriptor(cohort_id="medium", run=medium, timeout_ms=500))

    runner, _ = _make_runner(r)
    result = await runner.run(_ctx())

    assert [o.cohort_id for o in result.outputs] == ["slow", "fast", "medium"]


# ---------------------------------------------------------------------------
# Scenario 14: cohort_run_id deterministic + minted by runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cohort_run_id_deterministic_format():
    async def run_with_bogus_id(ctx):
        return CohortOutput(
            cohort_id="memory",
            cohort_run_id="cohort-tries-to-mint-its-own",
            output={},
        )

    r = CohortRegistry()
    r.register(
        CohortDescriptor(cohort_id="memory", run=run_with_bogus_id, timeout_ms=500)
    )

    runner, _ = _make_runner(r)
    result = await runner.run(_ctx(turn_id="turn-42"))

    out = result.outputs[0]
    assert out.cohort_run_id == "turn-42:memory:0"
    # Format matches {turn_id}:{cohort_id}:{sequence}
    assert re.match(r"^[^:]+:[^:]+:\d+$", out.cohort_run_id)
    # The runner overrode the cohort's attempt to mint its own id.
    assert "cohort-tries-to-mint-its-own" not in out.cohort_run_id


# ---------------------------------------------------------------------------
# Scenario 15: synthetic outputs use empty payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthetic_failure_uses_empty_output_payload():
    async def run_err(ctx):
        raise RuntimeError("nope")

    async def run_to(ctx):
        await asyncio.sleep(1.0)
        return _ok_output()

    r = CohortRegistry()
    r.register(CohortDescriptor(cohort_id="err", run=run_err, timeout_ms=500))
    r.register(CohortDescriptor(cohort_id="t_out", run=run_to, timeout_ms=50))

    runner, _ = _make_runner(r)
    result = await runner.run(_ctx())

    by_id = {o.cohort_id: o for o in result.outputs}
    # Per Section 5: outcome carries the signal; output stays empty.
    assert by_id["err"].output == {}
    assert by_id["t_out"].output == {}


# ---------------------------------------------------------------------------
# Scenario 8 (overall): outputs length always equals registered count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outputs_length_invariant():
    async def run_ok(ctx):
        return _ok_output("ok")

    async def run_err(ctx):
        raise RuntimeError("nope")

    async def run_to(ctx):
        await asyncio.sleep(1.0)
        return _ok_output("to")

    r = CohortRegistry()
    r.register(CohortDescriptor(cohort_id="ok", run=run_ok, timeout_ms=500))
    r.register(CohortDescriptor(cohort_id="err", run=run_err, timeout_ms=500))
    r.register(CohortDescriptor(cohort_id="to", run=run_to, timeout_ms=50))

    runner, _ = _make_runner(r)
    result = await runner.run(_ctx())

    assert len(result.outputs) == len(r)


# ---------------------------------------------------------------------------
# Scenario 16: opt-in callable verification (grep)
# ---------------------------------------------------------------------------


def test_no_production_caller_invokes_runner():
    """Acceptance criterion #13: runner is opt-in callable; nothing
    in handler.py or reasoning.py invokes it."""
    import pathlib
    repo = pathlib.Path(__file__).resolve().parents[1]
    for path in (
        repo / "kernos" / "messages" / "handler.py",
        repo / "kernos" / "kernel" / "reasoning.py",
    ):
        text = path.read_text()
        assert "CohortFanOutRunner" not in text
        assert "kernos.kernel.cohorts" not in text


# ---------------------------------------------------------------------------
# Empty registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_registry_returns_empty_result_and_audits():
    r = CohortRegistry()
    runner, audit = _make_runner(r)
    result = await runner.run(_ctx())
    assert result.outputs == ()
    assert len(audit) == 1


# ---------------------------------------------------------------------------
# Cohort returns wrong type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cohort_returning_wrong_type_synthesised_as_error():
    async def run_bad(ctx):
        return {"not": "a CohortOutput"}  # type: ignore[return-value]

    r = CohortRegistry()
    r.register(CohortDescriptor(cohort_id="bad", run=run_bad, timeout_ms=500))

    runner, _ = _make_runner(r)
    result = await runner.run(_ctx())
    out = result.outputs[0]
    assert out.outcome is Outcome.ERROR
    assert "expected CohortOutput" in out.error_summary


# ---------------------------------------------------------------------------
# default_visibility applied on synthetic outputs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthetic_output_inherits_default_visibility():
    async def run_err(ctx):
        raise RuntimeError("boom")

    r = CohortRegistry()
    r.register(
        CohortDescriptor(
            cohort_id="covenant",
            run=run_err,
            timeout_ms=500,
            default_visibility=Restricted(reason="covenant"),
        )
    )

    runner, _ = _make_runner(r)
    result = await runner.run(_ctx())
    out = result.outputs[0]
    assert isinstance(out.visibility, Restricted)
    assert out.visibility.reason == "covenant"
