"""SELF-IMPROVEMENT-CLOSURE-V1 Phase D — seed invariant + probe
handler tests.

Coverage:
* AC7 — Tool Availability Honesty probe runs against current
  substrate. Healthy substrate (every catalog entry classifiable
  and reachable): probe returns ``passed``. Substrate with a
  catalog entry added without gate classification: probe returns
  ``failed`` with evidence naming the divergent tool.
* AC11 — Failed probe emits ``closure.probe_failed`` event with
  the prescribed payload shape (pattern_id, invariant_id,
  closure_id, active_epoch, evidence); pattern's active_epoch is
  NOT bumped (covered by AC6 test; re-asserted in cardinality
  pin here).
* Seed invariant idempotency.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kernos.kernel.closure_store import (
    ClosureStore,
    TOOL_AVAILABILITY_HONESTY_INVARIANT_ID,
    TOOL_AVAILABILITY_HONESTY_STATEMENT,
    build_tool_availability_honesty_probe,
    clear_probe_runners,
    record_closure_attempt,
    register_probe_runner,
    run_closure_probe,
    seed_v1_invariants,
)
from kernos.kernel.friction_patterns import FrictionPatternStore


@pytest.fixture(autouse=True)
def _reset_probe_runners():
    clear_probe_runners()
    yield
    clear_probe_runners()


@pytest.fixture
async def fp_store(tmp_path: Path) -> FrictionPatternStore:
    store = FrictionPatternStore()
    await store.start(str(tmp_path))
    yield store
    await store.stop()


@pytest.fixture
async def closure_store(tmp_path: Path, fp_store) -> ClosureStore:
    store = ClosureStore()
    await store.start(str(tmp_path))
    yield store
    await store.close()


# ---------------------------------------------------------------------
# Seed invariant
# ---------------------------------------------------------------------


async def test_seed_v1_invariants_creates_tool_availability_honesty(
    closure_store,
):
    seeded = await seed_v1_invariants(
        closure_store, instance_id="i1",
    )
    assert seeded == {TOOL_AVAILABILITY_HONESTY_INVARIANT_ID: True}
    stored = await closure_store.get_invariant(
        instance_id="i1",
        invariant_id=TOOL_AVAILABILITY_HONESTY_INVARIANT_ID,
    )
    assert stored is not None
    assert stored["statement"] == TOOL_AVAILABILITY_HONESTY_STATEMENT
    assert stored["owner"] == "architect"
    assert stored["status"] == "active"


async def test_seed_v1_invariants_idempotent(closure_store):
    """Second call updates rather than failing on PK violation."""
    seeded_first = await seed_v1_invariants(
        closure_store, instance_id="i1",
    )
    seeded_second = await seed_v1_invariants(
        closure_store, instance_id="i1",
    )
    assert seeded_first[TOOL_AVAILABILITY_HONESTY_INVARIANT_ID] is True
    assert (
        seeded_second[TOOL_AVAILABILITY_HONESTY_INVARIANT_ID] is False
    )


async def test_seed_v1_pattern_invariant_links_idempotent(
    closure_store, fp_store,
):
    """Seed v1 link rows: needs both pattern + invariant to exist
    first. Once both exist, link insert succeeds; second call
    returns False (already exists). FK violations on missing
    sides are skipped silently."""
    from kernos.kernel.closure_store import (
        seed_v1_pattern_invariant_links,
    )
    from datetime import datetime, timezone
    # First call with neither pattern nor invariant present →
    # silent skip on the FK violation.
    out_empty = await seed_v1_pattern_invariant_links(
        closure_store, instance_id="i1",
    )
    assert all(v is False for v in out_empty.values())

    # Seed pattern + invariant.
    now = datetime.now(timezone.utc).isoformat()
    await fp_store._db.execute(
        """
        INSERT INTO friction_pattern (
            instance_id, pattern_id, description, signal_type_keys,
            lifecycle_state, occurrence_count, first_observed_at,
            last_observed_at, created_at, active_epoch,
            reactivation_threshold
        ) VALUES ('i1', 'capability-catalog-dispatch-divergence',
                  'seed', '[]', 'active', 0, ?, ?, ?, 0, 3)
        """,
        (now, now, now),
    )
    await seed_v1_invariants(closure_store, instance_id="i1")

    out_first = await seed_v1_pattern_invariant_links(
        closure_store, instance_id="i1",
    )
    # First successful seed → newly_created=True for the one link.
    assert any(v is True for v in out_first.values())

    # Second call → all False (already linked).
    out_second = await seed_v1_pattern_invariant_links(
        closure_store, instance_id="i1",
    )
    assert all(v is False for v in out_second.values())


async def test_seed_v1_invariants_per_instance(closure_store):
    """Same invariant gets seeded per-instance."""
    await seed_v1_invariants(closure_store, instance_id="i1")
    await seed_v1_invariants(closure_store, instance_id="i2")
    r1 = await closure_store.get_invariant(
        instance_id="i1",
        invariant_id=TOOL_AVAILABILITY_HONESTY_INVARIANT_ID,
    )
    r2 = await closure_store.get_invariant(
        instance_id="i2",
        invariant_id=TOOL_AVAILABILITY_HONESTY_INVARIANT_ID,
    )
    assert r1 is not None and r2 is not None


# ---------------------------------------------------------------------
# AC7 — Tool Availability Honesty probe
# ---------------------------------------------------------------------


def _fake_catalog_entry(name: str, source: str):
    """Minimal catalog-entry stub with name + source attributes."""
    entry = MagicMock()
    entry.name = name
    entry.source = source
    return entry


def test_ac7_probe_passes_when_all_entries_classifiable_and_dispatchable():
    """Healthy substrate: every catalog entry is in the
    dispatchable set AND classify returns non-unknown."""
    catalog = MagicMock()
    catalog.get_all.return_value = [
        _fake_catalog_entry("write_file", "kernel"),
        _fake_catalog_entry("read_file", "kernel"),
        _fake_catalog_entry("brave_web_search", "mcp"),
    ]
    gate = MagicMock()
    gate.classify_tool_effect.return_value = "soft_write"
    runner = build_tool_availability_honesty_probe(
        tool_catalog=catalog,
        dispatch_gate=gate,
        get_dispatchable_kernel_tools=lambda: {
            "write_file", "read_file",
        },
    )
    passed, evidence = runner({}, {})
    assert passed is True
    assert evidence["divergent_count"] == 0
    assert evidence["checked_count"] == 3


def test_ac7_probe_fails_when_gate_classifies_as_unknown():
    catalog = MagicMock()
    catalog.get_all.return_value = [
        _fake_catalog_entry("known_tool", "kernel"),
        _fake_catalog_entry("orphan_tool", "kernel"),
    ]
    gate = MagicMock()

    def _classify(name, *args, **kwargs):
        return "unknown" if name == "orphan_tool" else "read"
    gate.classify_tool_effect.side_effect = _classify

    runner = build_tool_availability_honesty_probe(
        tool_catalog=catalog,
        dispatch_gate=gate,
        get_dispatchable_kernel_tools=lambda: {
            "known_tool", "orphan_tool",
        },
    )
    passed, evidence = runner({}, {})
    assert passed is False
    assert evidence["divergent_count"] == 1
    assert evidence["divergent_tools"][0]["name"] == "orphan_tool"
    assert "gate_classify_unknown" in (
        evidence["divergent_tools"][0]["reason"]
    )


def test_ac7_probe_fails_when_kernel_tool_not_in_dispatchable_set():
    """Catalog claims a kernel tool but the dispatchability
    registry doesn't include it — that's the registry drift the
    invariant catches."""
    catalog = MagicMock()
    catalog.get_all.return_value = [
        _fake_catalog_entry("real_tool", "kernel"),
        _fake_catalog_entry("registered_but_unhandled", "kernel"),
    ]
    gate = MagicMock()
    gate.classify_tool_effect.return_value = "soft_write"
    runner = build_tool_availability_honesty_probe(
        tool_catalog=catalog,
        dispatch_gate=gate,
        get_dispatchable_kernel_tools=lambda: {"real_tool"},
    )
    passed, evidence = runner({}, {})
    assert passed is False
    assert evidence["divergent_count"] == 1
    div = evidence["divergent_tools"][0]
    assert div["name"] == "registered_but_unhandled"
    assert "not_in_dispatchable_kernel_tools" in div["reason"]


def test_ac7_probe_treats_mcp_source_as_dispatchable():
    """MCP-source entries bypass the kernel-dispatchable set check
    (they route through call_tool on the MCP server, not the
    kernel-tool branch)."""
    catalog = MagicMock()
    catalog.get_all.return_value = [
        _fake_catalog_entry("brave_web_search", "mcp"),
        _fake_catalog_entry("calendar_create", "mcp:google-calendar"),
    ]
    gate = MagicMock()
    gate.classify_tool_effect.return_value = "read"
    runner = build_tool_availability_honesty_probe(
        tool_catalog=catalog,
        dispatch_gate=gate,
        # MCP tools are NOT in kernel-dispatchable; the probe must
        # accept them anyway.
        get_dispatchable_kernel_tools=lambda: set(),
    )
    passed, evidence = runner({}, {})
    assert passed is True
    assert evidence["divergent_count"] == 0


def test_ac7_probe_evidence_includes_all_divergents():
    """Multiple divergences → all listed in evidence (not
    truncated)."""
    catalog = MagicMock()
    catalog.get_all.return_value = [
        _fake_catalog_entry("good_tool", "kernel"),
        _fake_catalog_entry("unknown_class", "kernel"),
        _fake_catalog_entry("missing_handler", "kernel"),
    ]
    gate = MagicMock()

    def _classify(name, *args, **kwargs):
        return "unknown" if name == "unknown_class" else "read"
    gate.classify_tool_effect.side_effect = _classify

    runner = build_tool_availability_honesty_probe(
        tool_catalog=catalog,
        dispatch_gate=gate,
        get_dispatchable_kernel_tools=lambda: {"good_tool"},
    )
    passed, evidence = runner({}, {})
    assert passed is False
    names = {d["name"] for d in evidence["divergent_tools"]}
    assert names == {"unknown_class", "missing_handler"}


def test_ac7_probe_survives_catalog_get_all_exception():
    """If the catalog throws, probe surfaces the error in evidence
    rather than crashing the workflow."""
    catalog = MagicMock()
    catalog.get_all.side_effect = RuntimeError("catalog corrupted")
    gate = MagicMock()
    runner = build_tool_availability_honesty_probe(
        tool_catalog=catalog,
        dispatch_gate=gate,
        get_dispatchable_kernel_tools=lambda: set(),
    )
    passed, evidence = runner({}, {})
    assert passed is False
    assert "catalog_get_all_raised" in evidence["error"]


# ---------------------------------------------------------------------
# AC11 — closure.probe_failed event payload shape
# ---------------------------------------------------------------------


async def _seed_link(closure_store, fp_store, *, instance_id="i1",
                     pattern_id="p1",
                     invariant_id=TOOL_AVAILABILITY_HONESTY_INVARIANT_ID):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    await fp_store._db.execute(
        """
        INSERT INTO friction_pattern (
            instance_id, pattern_id, description, signal_type_keys,
            lifecycle_state, occurrence_count, first_observed_at,
            last_observed_at, created_at, active_epoch,
            reactivation_threshold
        ) VALUES (?, ?, ?, '[]', 'active', 0, ?, ?, ?, 5, 3)
        """,
        (instance_id, pattern_id, "seed for AC11", now, now, now),
    )
    await seed_v1_invariants(closure_store, instance_id=instance_id)
    await closure_store.insert_link(
        instance_id=instance_id,
        pattern_id=pattern_id,
        invariant_id=invariant_id,
    )


async def test_ac11_closure_probe_failed_event_payload_shape(
    closure_store, fp_store,
):
    await _seed_link(closure_store, fp_store)

    async def _failing(payload, ctx):
        return False, {"divergent_tools": [{"name": "x", "reason": "y"}]}
    register_probe_runner("deterministic_introspection", _failing)

    rec = await record_closure_attempt(
        store=closure_store,
        instance_id="i1",
        pattern_id="p1",
        invariant_id=TOOL_AVAILABILITY_HONESTY_INVARIANT_ID,
        active_epoch=5,
        route="code_change_via_cc",
        route_payload={},
        probe_kind="deterministic_introspection",
        probe_payload={},
        probe_payload_version=1,
    )

    emit_calls: list[dict] = []

    def _emit(*, instance_id, event_type, payload):
        emit_calls.append({
            "instance_id": instance_id,
            "event_type": event_type,
            "payload": payload,
        })

    await run_closure_probe(
        store=closure_store,
        instance_id="i1",
        closure_id=rec["closure_id"],
        event_emit_fn=_emit,
    )

    assert len(emit_calls) == 1
    call = emit_calls[0]
    assert call["instance_id"] == "i1"
    assert call["event_type"] == "closure.probe_failed"
    p = call["payload"]
    # AC11 prescribed shape:
    assert set(p.keys()) >= {
        "pattern_id", "invariant_id", "closure_id",
        "active_epoch", "evidence",
    }
    assert p["pattern_id"] == "p1"
    assert p["invariant_id"] == TOOL_AVAILABILITY_HONESTY_INVARIANT_ID
    assert p["closure_id"] == rec["closure_id"]
    assert p["active_epoch"] == 5
    assert p["evidence"] == {
        "divergent_tools": [{"name": "x", "reason": "y"}],
    }


async def test_ac11_active_epoch_not_bumped_on_failed_probe(
    closure_store, fp_store,
):
    """Failed probe must NOT bump the pattern's active_epoch — the
    failure is a remediation gap within the same episode, not a
    new activation."""
    await _seed_link(closure_store, fp_store)

    async def _failing(payload, ctx):
        return False, {"x": 1}
    register_probe_runner("deterministic_introspection", _failing)

    rec = await record_closure_attempt(
        store=closure_store,
        instance_id="i1",
        pattern_id="p1",
        invariant_id=TOOL_AVAILABILITY_HONESTY_INVARIANT_ID,
        active_epoch=5,
        route="code_change_via_cc",
        route_payload={},
        probe_kind="deterministic_introspection",
        probe_payload={},
        probe_payload_version=1,
    )
    await run_closure_probe(
        store=closure_store,
        instance_id="i1",
        closure_id=rec["closure_id"],
    )
    async with fp_store._db.execute(
        "SELECT active_epoch FROM friction_pattern "
        "WHERE instance_id='i1' AND pattern_id='p1'"
    ) as cur:
        row = await cur.fetchone()
    assert row["active_epoch"] == 5
