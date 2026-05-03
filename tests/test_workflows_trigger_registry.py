"""Tests for the trigger registry.

WORKFLOW-LOOP-PRIMITIVE C2. The registry attaches a single post-flush
hook to event_stream and dispatches matching triggers to registered
listeners. These tests pin: registration round-trip, multi-tenancy,
post-flush integration, prefilters, idempotency suppression,
restart-resume, listener isolation, and emit-fast-path-unaffected.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from kernos.kernel import event_stream
from kernos.kernel.event_stream import _registered_post_flush_hooks
from kernos.kernel.workflows.predicates import PredicateError
from kernos.kernel.workflows.trigger_registry import (
    Trigger,
    TriggerRegistry,
    _reset_for_tests,
)


@pytest.fixture
async def registry(tmp_path):
    """Fresh event_stream + registry per test."""
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    reg = TriggerRegistry()
    await reg.start(str(tmp_path))
    yield reg
    await _reset_for_tests(reg)
    await event_stream._reset_for_tests()


def _eq_predicate(field: str, value) -> dict:
    return {"op": "eq", "path": field, "value": value}


def _make_trigger(**overrides) -> Trigger:
    base = dict(
        trigger_id="",
        workflow_id="wf-1",
        instance_id="inst_a",
        event_type="cc.batch.report",
        predicate=_eq_predicate("payload.kind", "report"),
        owner="owner",
    )
    base.update(overrides)
    return Trigger(**base)


class TestLifecycle:
    async def test_start_attaches_hook(self, registry):
        # The registry's post-flush hook is registered.
        assert any(
            getattr(h, "__qualname__", "").endswith("_on_post_flush")
            for h in _registered_post_flush_hooks()
        )

    async def test_stop_detaches_hook(self, tmp_path):
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        try:
            reg = TriggerRegistry()
            await reg.start(str(tmp_path))
            await reg.stop()
            assert all(
                not getattr(h, "__qualname__", "").endswith("_on_post_flush")
                for h in _registered_post_flush_hooks()
            )
        finally:
            await event_stream._reset_for_tests()

    async def test_start_idempotent(self, registry):
        # Calling start twice is a no-op (cache + DB unchanged).
        await registry.start("/tmp/should-not-be-used")
        # Hook still registered exactly once for this registry.
        hits = [
            h for h in _registered_post_flush_hooks()
            if getattr(h, "__qualname__", "").endswith("_on_post_flush")
        ]
        assert len(hits) == 1


class TestRegistration:
    async def test_register_round_trip(self, registry):
        trig = await registry.register_trigger(_make_trigger())
        assert trig.trigger_id  # filled in
        assert trig.created_at  # filled in
        loaded = await registry.get_trigger(trig.trigger_id)
        assert loaded is not None
        assert loaded.workflow_id == "wf-1"
        assert loaded.predicate["op"] == "eq"

    async def test_register_invalid_predicate_rejected(self, registry):
        bad = _make_trigger(predicate={"op": "wat"})
        with pytest.raises(PredicateError):
            await registry.register_trigger(bad)
        # No partial state in DB.
        rows = await registry.list_triggers("inst_a")
        assert rows == []

    async def test_list_triggers_filters_by_status(self, registry):
        a = await registry.register_trigger(_make_trigger(workflow_id="wf-a"))
        b = await registry.register_trigger(_make_trigger(workflow_id="wf-b"))
        await registry.update_status(a.trigger_id, "paused")
        active = await registry.list_triggers("inst_a", status="active")
        paused = await registry.list_triggers("inst_a", status="paused")
        assert {t.trigger_id for t in active} == {b.trigger_id}
        assert {t.trigger_id for t in paused} == {a.trigger_id}

    async def test_update_status_invalid_rejected(self, registry):
        trig = await registry.register_trigger(_make_trigger())
        with pytest.raises(ValueError):
            await registry.update_status(trig.trigger_id, "wat")

    async def test_register_rolls_back_on_cache_failure(self, registry):
        """If the in-memory cache update raises after a successful
        DB INSERT, the persisted row must be rolled back so on-disk
        state cannot disagree with in-memory state. Forces the failure
        by monkeypatching ``_cache_insert`` to raise."""
        original = registry._cache_insert
        registry._cache_insert = lambda t: (_ for _ in ()).throw(  # type: ignore[assignment]
            RuntimeError("cache busted"),
        )
        try:
            with pytest.raises(RuntimeError, match="cache busted"):
                await registry.register_trigger(_make_trigger())
        finally:
            registry._cache_insert = original  # type: ignore[assignment]
        # Persisted row was rolled back.
        rows = await registry.list_triggers("inst_a")
        assert rows == []


class TestPostFlushDispatch:
    async def test_match_fires_listener(self, registry):
        captured: list[tuple] = []

        async def listener(trigger, event):
            captured.append((trigger.trigger_id, event.event_id))

        registry.add_match_listener(listener)
        trig = await registry.register_trigger(_make_trigger())

        await event_stream.emit(
            "inst_a", "cc.batch.report", {"kind": "report"}, member_id="mem_a",
        )
        await event_stream.flush_now()
        # Yield once so any scheduled hook task can complete.
        await asyncio.sleep(0)

        assert len(captured) == 1
        assert captured[0][0] == trig.trigger_id

    async def test_predicate_miss_does_not_fire(self, registry):
        captured: list = []

        async def listener(trigger, event):
            captured.append(event)

        registry.add_match_listener(listener)
        await registry.register_trigger(_make_trigger())
        # Wrong payload — predicate misses.
        await event_stream.emit(
            "inst_a", "cc.batch.report", {"kind": "other"}, member_id="mem_a",
        )
        await event_stream.flush_now()
        assert captured == []

    async def test_paused_trigger_does_not_fire(self, registry):
        captured: list = []
        registry.add_match_listener(lambda t, e: captured.append(e))
        trig = await registry.register_trigger(_make_trigger())
        await registry.update_status(trig.trigger_id, "paused")
        await event_stream.emit(
            "inst_a", "cc.batch.report", {"kind": "report"}, member_id="mem_a",
        )
        await event_stream.flush_now()
        assert captured == []

    async def test_resumed_trigger_fires_again(self, registry):
        captured: list = []
        registry.add_match_listener(lambda t, e: captured.append(e))
        trig = await registry.register_trigger(_make_trigger())
        await registry.update_status(trig.trigger_id, "paused")
        await event_stream.emit("inst_a", "cc.batch.report", {"kind": "report"})
        await event_stream.flush_now()
        assert captured == []
        await registry.update_status(trig.trigger_id, "active")
        await event_stream.emit("inst_a", "cc.batch.report", {"kind": "report"})
        await event_stream.flush_now()
        assert len(captured) == 1


class TestMultiTenancy:
    async def test_trigger_in_one_instance_does_not_fire_in_another(self, registry):
        captured: list = []
        registry.add_match_listener(lambda t, e: captured.append(e))
        # Trigger on inst_a only.
        await registry.register_trigger(_make_trigger(instance_id="inst_a"))
        # Event in inst_b — must not fire.
        await event_stream.emit(
            "inst_b", "cc.batch.report", {"kind": "report"}, member_id="mem_b",
        )
        await event_stream.flush_now()
        assert captured == []
        # Event in inst_a — fires.
        await event_stream.emit(
            "inst_a", "cc.batch.report", {"kind": "report"}, member_id="mem_a",
        )
        await event_stream.flush_now()
        assert len(captured) == 1


class TestPrefilters:
    async def test_actor_filter(self, registry):
        captured: list = []
        registry.add_match_listener(lambda t, e: captured.append(e))
        await registry.register_trigger(_make_trigger(actor_filter="mem_a"))
        # Event from a different actor doesn't fire.
        await event_stream.emit(
            "inst_a", "cc.batch.report", {"kind": "report"}, member_id="mem_b",
        )
        await event_stream.flush_now()
        assert captured == []
        # Event from the right actor fires.
        await event_stream.emit(
            "inst_a", "cc.batch.report", {"kind": "report"}, member_id="mem_a",
        )
        await event_stream.flush_now()
        assert len(captured) == 1

    async def test_correlation_filter(self, registry):
        captured: list = []
        registry.add_match_listener(lambda t, e: captured.append(e))
        await registry.register_trigger(_make_trigger(correlation_filter="cor-X"))
        await event_stream.emit(
            "inst_a", "cc.batch.report", {"kind": "report"},
            member_id="mem_a", correlation_id="cor-Y",
        )
        await event_stream.flush_now()
        assert captured == []
        await event_stream.emit(
            "inst_a", "cc.batch.report", {"kind": "report"},
            member_id="mem_a", correlation_id="cor-X",
        )
        await event_stream.flush_now()
        assert len(captured) == 1


class TestWildcardEventType:
    async def test_wildcard_matches_any_event_type(self, registry):
        captured: list = []
        registry.add_match_listener(lambda t, e: captured.append(e.event_type))
        await registry.register_trigger(_make_trigger(
            event_type="*",
            predicate={"op": "event_type_starts_with", "prefix": "tool."},
        ))
        await event_stream.emit("inst_a", "tool.called", {})
        await event_stream.emit("inst_a", "cc.batch.report", {})
        await event_stream.emit("inst_a", "tool.returned", {})
        await event_stream.flush_now()
        assert captured == ["tool.called", "tool.returned"]


class TestIdempotency:
    async def test_duplicate_fire_suppressed(self, registry):
        captured: list = []
        registry.add_match_listener(lambda t, e: captured.append(e.event_id))
        await registry.register_trigger(_make_trigger(
            idempotency_key_template="{payload[task_id]}",
        ))
        # Two events with the SAME task_id — only the first fires.
        await event_stream.emit(
            "inst_a", "cc.batch.report",
            {"kind": "report", "task_id": "T-42"},
        )
        await event_stream.flush_now()
        await event_stream.emit(
            "inst_a", "cc.batch.report",
            {"kind": "report", "task_id": "T-42"},
        )
        await event_stream.flush_now()
        assert len(captured) == 1

    async def test_different_keys_both_fire(self, registry):
        captured: list = []
        registry.add_match_listener(lambda t, e: captured.append(e.event_id))
        await registry.register_trigger(_make_trigger(
            idempotency_key_template="{payload[task_id]}",
        ))
        await event_stream.emit(
            "inst_a", "cc.batch.report",
            {"kind": "report", "task_id": "T-1"},
        )
        await event_stream.emit(
            "inst_a", "cc.batch.report",
            {"kind": "report", "task_id": "T-2"},
        )
        await event_stream.flush_now()
        assert len(captured) == 2

    async def test_template_missing_field_skips(self, registry):
        """A template that references a missing payload field renders
        to None and the trigger is treated as a no-fire (rather than
        falsely firing under some default key)."""
        captured: list = []
        registry.add_match_listener(lambda t, e: captured.append(e))
        await registry.register_trigger(_make_trigger(
            idempotency_key_template="{payload[absent_field]}",
        ))
        await event_stream.emit("inst_a", "cc.batch.report", {"kind": "report"})
        await event_stream.flush_now()
        assert captured == []

    async def test_template_attribute_traversal_rejected(self, registry):
        """Templates must not be able to read arbitrary Python
        attributes via the ``.attr`` format spec. ``{payload.__class__}``
        would otherwise leak ``<class 'dict'>`` into the rendered key
        and let an operator probe Python internals at registration
        time. The custom formatter raises ValueError on attribute
        access, which is treated as a missing-field render → no fire.
        """
        captured: list = []
        registry.add_match_listener(lambda t, e: captured.append(e))
        await registry.register_trigger(_make_trigger(
            idempotency_key_template="{payload.__class__}",
        ))
        await event_stream.emit("inst_a", "cc.batch.report", {"kind": "report"})
        await event_stream.flush_now()
        assert captured == []


class TestRestartResume:
    async def test_active_triggers_reload_after_restart(self, tmp_path):
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        try:
            reg1 = TriggerRegistry()
            await reg1.start(str(tmp_path))
            await reg1.register_trigger(_make_trigger(workflow_id="wf-1"))
            await reg1.register_trigger(_make_trigger(workflow_id="wf-paused"))
            paused = (await reg1.list_triggers("inst_a"))[1]
            await reg1.update_status(paused.trigger_id, "paused")
            await reg1.stop()

            # Fresh registry on the same DB.
            reg2 = TriggerRegistry()
            await reg2.start(str(tmp_path))
            captured: list = []
            reg2.add_match_listener(lambda t, e: captured.append(t.workflow_id))
            await event_stream.emit(
                "inst_a", "cc.batch.report", {"kind": "report"},
            )
            await event_stream.flush_now()
            # Only the active one fires; paused stays paused across restart.
            assert captured == ["wf-1"]
            await reg2.stop()
        finally:
            await event_stream._reset_for_tests()


class TestListenerIsolation:
    async def test_failing_listener_does_not_block_others(self, registry):
        seen_a: list = []
        seen_b: list = []

        async def boom(trigger, event):
            raise RuntimeError("boom")

        async def cap_a(trigger, event):
            seen_a.append(event.event_id)

        async def cap_b(trigger, event):
            seen_b.append(event.event_id)

        registry.add_match_listener(cap_a)
        registry.add_match_listener(boom)
        registry.add_match_listener(cap_b)
        await registry.register_trigger(_make_trigger())
        await event_stream.emit(
            "inst_a", "cc.batch.report", {"kind": "report"}, member_id="mem_a",
        )
        await event_stream.flush_now()
        assert len(seen_a) == 1
        assert len(seen_b) == 1

    async def test_remove_listener(self, registry):
        captured: list = []

        async def cap(t, e):
            captured.append(e)

        registry.add_match_listener(cap)
        assert registry.remove_match_listener(cap) is True
        await registry.register_trigger(_make_trigger())
        await event_stream.emit("inst_a", "cc.batch.report", {"kind": "report"})
        await event_stream.flush_now()
        assert captured == []

    async def test_remove_unknown_listener_returns_false(self, registry):
        async def never(t, e): ...

        assert registry.remove_match_listener(never) is False


class TestFastPathUnaffected:
    async def test_emit_latency_unchanged_with_registry_running(self, registry):
        """Acceptance criterion 4: predicate evaluation is on the
        post-flush hook, not inline with emit. emit latency stays
        within noise even with triggers + listeners attached."""
        captured: list = []
        registry.add_match_listener(lambda t, e: captured.append(e.event_id))
        # Register multiple triggers — emit latency must not scale with this.
        for i in range(20):
            await registry.register_trigger(_make_trigger(
                workflow_id=f"wf-{i}",
                predicate={"op": "eq", "path": "payload.kind", "value": f"k-{i}"},
            ))
        t0 = time.monotonic()
        for i in range(50):
            await event_stream.emit(
                "inst_a", "cc.batch.report", {"kind": "k-1"},
            )
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert elapsed_ms < 100, (
            f"50 emits took {elapsed_ms:.1f}ms — registry leaked into fast path"
        )
