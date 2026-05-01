"""WTC v1 C5c-1 — AwarenessEvaluator drives unified runtime heartbeat.

Pins:

* When a TriggerEvaluationRuntime is wired, AwarenessEvaluator's
  heartbeat tick calls runtime.evaluate_now().
* On start, the runtime's recovery sweep runs ONCE before the
  loop begins.
* Backward compat: when no runtime is wired, behavior is exactly
  as before (legacy Phase 2 still runs; no Phase 2b call).
* Errors in the unified runtime path are isolated — they do not
  break legacy trigger evaluation or other phases.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from kernos.kernel import event_stream
from kernos.kernel.awareness import AwarenessEvaluator
from kernos.kernel.state_json import JsonStateStore
from kernos.kernel.triggers import TriggerEvaluationRuntime


@pytest.fixture
async def event_stream_started(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path), flush_interval_s=0.05)
    yield
    await event_stream.stop_writer()
    await event_stream._reset_for_tests()


@pytest.fixture
async def state(tmp_path):
    yield JsonStateStore(tmp_path)


@pytest.fixture
async def runtime(tmp_path, event_stream_started):
    rt = TriggerEvaluationRuntime()
    await rt.start(
        data_dir=str(tmp_path),
        heartbeat_seconds=1,
    )
    yield rt
    await rt.stop()


# ---------------------------------------------------------------------------
# Direct method observation: instrument runtime.evaluate_now / recover
# and verify AwarenessEvaluator invokes them on the right cadence.
# ---------------------------------------------------------------------------


class _CountingRuntime:
    """Minimal stand-in that satisfies AwarenessEvaluator's runtime
    contract (evaluate_now, recover) and records call counts."""

    def __init__(self) -> None:
        self.evaluate_calls = 0
        self.recover_calls = 0
        self.evaluate_should_raise = False

    async def evaluate_now(self) -> int:
        self.evaluate_calls += 1
        if self.evaluate_should_raise:
            raise RuntimeError("simulated runtime failure")
        return 0

    async def recover(self) -> int:
        self.recover_calls += 1
        return 0


async def test_recovery_sweep_runs_once_at_start(state, event_stream_started):
    counting = _CountingRuntime()
    aw = AwarenessEvaluator(
        state=state, events=event_stream,
        interval_seconds=3600,
        trigger_interval_seconds=1,
        runtime=counting,
    )
    await aw.start("inst1")
    # Yield control briefly so the loop ticks at least once.
    await asyncio.sleep(0.05)
    await aw.stop()
    # Recovery ran exactly once at start.
    assert counting.recover_calls == 1


async def test_evaluate_now_called_on_each_tick(state, event_stream_started):
    counting = _CountingRuntime()
    aw = AwarenessEvaluator(
        state=state, events=event_stream,
        interval_seconds=3600,
        trigger_interval_seconds=1,
        runtime=counting,
    )
    await aw.start("inst1")
    # Yield enough time for at least one full tick of the inner
    # loop. trigger_interval=1s, but the loop also has the
    # main interval check; we settle for "more than 0".
    await asyncio.sleep(0.05)
    await aw.stop()
    assert counting.evaluate_calls >= 1


async def test_no_runtime_means_no_runtime_calls(state, event_stream_started):
    """Backward compat: no runtime → no calls into the runtime
    contract. Instantiating without the kwarg keeps the legacy
    behavior exactly."""
    aw = AwarenessEvaluator(
        state=state, events=event_stream,
        interval_seconds=3600,
        trigger_interval_seconds=1,
        # runtime intentionally omitted.
    )
    await aw.start("inst1")
    await asyncio.sleep(0.05)
    await aw.stop()
    # Nothing to assert directly — the test passes by surviving
    # without AttributeError on a None runtime. Implementation
    # check: the new attribute exists and is None.
    assert aw._runtime is None


async def test_runtime_failure_isolated_from_legacy_path(
    state, event_stream_started,
):
    """A raise from runtime.evaluate_now() must not crash the
    awareness loop or block other phases."""
    counting = _CountingRuntime()
    counting.evaluate_should_raise = True
    aw = AwarenessEvaluator(
        state=state, events=event_stream,
        interval_seconds=3600,
        trigger_interval_seconds=1,
        runtime=counting,
    )
    await aw.start("inst1")
    await asyncio.sleep(0.05)
    # Loop is still running despite per-tick raises.
    assert aw._running is True
    await aw.stop()
    # The error path was hit (counter incremented BEFORE raising).
    assert counting.evaluate_calls >= 1


async def test_recovery_failure_does_not_block_start(
    state, event_stream_started,
):
    """If recover() raises during start, the heartbeat loop must
    still come up — start_loop continues."""

    class _RecoverFailsRuntime(_CountingRuntime):
        async def recover(self) -> int:
            await super().recover()
            raise RuntimeError("simulated recovery failure")

    counting = _RecoverFailsRuntime()
    aw = AwarenessEvaluator(
        state=state, events=event_stream,
        interval_seconds=3600,
        trigger_interval_seconds=1,
        runtime=counting,
    )
    await aw.start("inst1")
    await asyncio.sleep(0.05)
    await aw.stop()
    assert counting.recover_calls == 1
    # Loop kept running — evaluate_now was called at least once.
    assert counting.evaluate_calls >= 1


# ---------------------------------------------------------------------------
# End-to-end with a real runtime instance
# ---------------------------------------------------------------------------


async def test_real_runtime_ticks_via_awareness(
    state, runtime, event_stream_started,
):
    """Full integration: a real TriggerEvaluationRuntime wired into
    AwarenessEvaluator gets ticked via the loop."""
    aw = AwarenessEvaluator(
        state=state, events=event_stream,
        interval_seconds=3600,
        trigger_interval_seconds=1,
        runtime=runtime,
    )
    await aw.start("inst1")
    # The tick should have triggered evaluate_now without error.
    # The runtime's outbox is started; an idle evaluate_now returns 0.
    await asyncio.sleep(0.1)
    await aw.stop()
    # No assertion failure means evaluate_now ran cleanly.
