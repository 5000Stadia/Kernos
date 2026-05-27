"""SELF-IMPROVEMENT-CLOSURE-V1 Phase A — closure_store schema +
helpers tests.

Coverage:
* AC1 — invariant insert succeeds; PK violation on duplicate.
* AC2 — link table FK violation on missing pattern OR missing
  invariant.
* AC3 — closure_attempt insert succeeds; pending unique index
  blocks second pending insert in same episode; allows re-pending
  after the first transitions to failed.
* AC4 — record_closure_attempt is idempotent on retry.
* AC8 — probe_kind allowlist hard-rejects (validation only here;
  full bypass-rejection lives with run_closure_probe).
* AC13 — record_closure_attempt rejects unlinked pairs.
* AC14 — run_closure_probe idempotent replay.

Tests use real ``data/instance.db`` in a tmp dir, no mocks —
mirrors the friction-pattern-store test style.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import aiosqlite

from kernos.kernel.closure_store import (
    ClosureStore,
    ClosureStoreError,
    InvariantNotLinkedToPattern,
    ProbeKindNotAllowed,
    READ_ONLY_PROBE_KINDS,
    ROUTE_CLASSES,
    clear_probe_runners,
    lookup_pattern_invariants,
    record_closure_attempt,
    register_probe_runner,
    run_closure_probe,
)
from kernos.kernel.friction_patterns import FrictionPatternStore


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


@pytest.fixture
async def fp_store(tmp_path: Path) -> FrictionPatternStore:
    """Friction patterns must exist for the link table's FK to
    target — so spin up a real FrictionPatternStore against the
    same instance.db."""
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


async def _seed_friction_pattern(
    fp_store: FrictionPatternStore,
    *,
    instance_id: str = "i1",
    pattern_id: str = "p1",
) -> None:
    """Insert a minimal friction_pattern row for FK satisfaction.

    Goes around ``create_pattern`` (which auto-generates the
    pattern_id from a description slug) and writes the row
    directly so tests can pin pattern_id to a deterministic
    string.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    await fp_store._db.execute(
        """
        INSERT INTO friction_pattern (
            instance_id, pattern_id, description, signal_type_keys,
            lifecycle_state, occurrence_count, first_observed_at,
            last_observed_at, created_at, active_epoch,
            reactivation_threshold
        ) VALUES (?, ?, ?, '[]', 'active', 0, ?, ?, ?, 0, 3)
        """,
        (instance_id, pattern_id, "seed for closure tests",
         now, now, now),
    )


# ---------------------------------------------------------------------
# AC1 — invariant table
# ---------------------------------------------------------------------


async def test_ac1_invariant_insert_succeeds(closure_store):
    await closure_store.insert_invariant(
        instance_id="i1",
        invariant_id="tool-availability-honesty",
        statement="If the catalog says a tool is available...",
        owner="architect",
    )
    row = await closure_store.get_invariant(
        instance_id="i1", invariant_id="tool-availability-honesty",
    )
    assert row is not None
    assert row["statement"].startswith("If the catalog")
    assert row["owner"] == "architect"
    assert row["status"] == "active"
    assert row["created_at"]
    assert row["last_edited"]


async def test_ac1_invariant_pk_violation_on_duplicate(closure_store):
    await closure_store.insert_invariant(
        instance_id="i1",
        invariant_id="dup-id",
        statement="first",
        owner="architect",
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await closure_store.insert_invariant(
            instance_id="i1",
            invariant_id="dup-id",
            statement="second attempt",
            owner="architect",
        )


async def test_ac1_invariant_rejects_invalid_owner(closure_store):
    with pytest.raises(ClosureStoreError, match="invariant owner"):
        await closure_store.insert_invariant(
            instance_id="i1",
            invariant_id="bad-owner",
            statement="x",
            owner="random-string",
        )


async def test_ac1_invariant_rejects_invalid_status(closure_store):
    with pytest.raises(ClosureStoreError, match="invariant status"):
        await closure_store.insert_invariant(
            instance_id="i1",
            invariant_id="bad-status",
            statement="x",
            owner="architect",
            status="not-a-status",
        )


async def test_ac1_invariant_scoped_per_instance(closure_store):
    """Same invariant_id can exist under different instance_ids."""
    await closure_store.insert_invariant(
        instance_id="i1", invariant_id="dup", statement="a",
        owner="architect",
    )
    await closure_store.insert_invariant(
        instance_id="i2", invariant_id="dup", statement="b",
        owner="architect",
    )
    r1 = await closure_store.get_invariant(
        instance_id="i1", invariant_id="dup",
    )
    r2 = await closure_store.get_invariant(
        instance_id="i2", invariant_id="dup",
    )
    assert r1["statement"] == "a"
    assert r2["statement"] == "b"


# ---------------------------------------------------------------------
# AC2 — link table FK enforcement
# ---------------------------------------------------------------------


async def test_ac2_link_succeeds_when_both_sides_exist(
    closure_store, fp_store,
):
    await _seed_friction_pattern(fp_store)
    await closure_store.insert_invariant(
        instance_id="i1", invariant_id="inv1",
        statement="x", owner="architect",
    )
    await closure_store.insert_link(
        instance_id="i1", pattern_id="p1", invariant_id="inv1",
    )
    assert await closure_store.link_exists(
        instance_id="i1", pattern_id="p1", invariant_id="inv1",
    )


async def test_ac2_link_fk_violation_missing_pattern(closure_store):
    """Pattern doesn't exist — link insert raises FK violation."""
    await closure_store.insert_invariant(
        instance_id="i1", invariant_id="inv1",
        statement="x", owner="architect",
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await closure_store.insert_link(
            instance_id="i1",
            pattern_id="nonexistent-pattern",
            invariant_id="inv1",
        )


async def test_ac2_link_fk_violation_missing_invariant(
    closure_store, fp_store,
):
    """Invariant doesn't exist — link insert raises FK violation."""
    await _seed_friction_pattern(fp_store)
    with pytest.raises(aiosqlite.IntegrityError):
        await closure_store.insert_link(
            instance_id="i1",
            pattern_id="p1",
            invariant_id="nonexistent-invariant",
        )


# ---------------------------------------------------------------------
# AC3 — pending uniqueness + re-pending allowed
# ---------------------------------------------------------------------


async def _seed_link(closure_store, fp_store):
    await _seed_friction_pattern(fp_store)
    await closure_store.insert_invariant(
        instance_id="i1", invariant_id="inv1",
        statement="x", owner="architect",
    )
    await closure_store.insert_link(
        instance_id="i1", pattern_id="p1", invariant_id="inv1",
    )


async def test_ac3_closure_attempt_first_pending_insert_succeeds(
    closure_store, fp_store,
):
    await _seed_link(closure_store, fp_store)
    await closure_store.insert_closure_attempt(
        instance_id="i1",
        closure_id="c1",
        pattern_id="p1",
        invariant_id="inv1",
        active_epoch=0,
        route="code_change_via_cc",
        route_payload={},
        probe_kind="deterministic_introspection",
        probe_payload={},
        probe_payload_version=1,
    )
    row = await closure_store.get_closure_attempt(
        instance_id="i1", closure_id="c1",
    )
    assert row["outcome"] == "pending"
    assert row["completed_at"] is None
    assert row["active_epoch"] == 0


async def test_ac3_second_pending_insert_blocked(
    closure_store, fp_store,
):
    """Partial unique index: two pending rows for same
    (pattern, invariant, episode) → IntegrityError."""
    await _seed_link(closure_store, fp_store)
    await closure_store.insert_closure_attempt(
        instance_id="i1",
        closure_id="c1",
        pattern_id="p1",
        invariant_id="inv1",
        active_epoch=0,
        route="code_change_via_cc",
        route_payload={},
        probe_kind="deterministic_introspection",
        probe_payload={},
        probe_payload_version=1,
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await closure_store.insert_closure_attempt(
            instance_id="i1",
            closure_id="c2",   # different closure_id
            pattern_id="p1",
            invariant_id="inv1",
            active_epoch=0,    # same episode
            route="code_change_via_cc",
            route_payload={},
            probe_kind="deterministic_introspection",
            probe_payload={},
            probe_payload_version=1,
        )


async def test_ac3_re_pending_allowed_after_failed(
    closure_store, fp_store,
):
    """Partial index excludes non-pending rows — after the first
    attempt transitions to failed, a second pending insert in the
    same episode succeeds."""
    await _seed_link(closure_store, fp_store)
    await closure_store.insert_closure_attempt(
        instance_id="i1",
        closure_id="c1",
        pattern_id="p1",
        invariant_id="inv1",
        active_epoch=0,
        route="code_change_via_cc",
        route_payload={},
        probe_kind="deterministic_introspection",
        probe_payload={},
        probe_payload_version=1,
    )
    await closure_store.update_closure_outcome(
        instance_id="i1",
        closure_id="c1",
        outcome="failed",
        evidence={"divergent_tools": ["foo"]},
    )
    # Now re-pending in the same episode should succeed.
    await closure_store.insert_closure_attempt(
        instance_id="i1",
        closure_id="c2",
        pattern_id="p1",
        invariant_id="inv1",
        active_epoch=0,
        route="code_change_via_cc",
        route_payload={},
        probe_kind="deterministic_introspection",
        probe_payload={},
        probe_payload_version=1,
    )
    row2 = await closure_store.get_closure_attempt(
        instance_id="i1", closure_id="c2",
    )
    assert row2["outcome"] == "pending"


async def test_ac3_different_episode_allowed(closure_store, fp_store):
    """Same (pattern, invariant), different active_epoch — both
    can be pending simultaneously."""
    await _seed_link(closure_store, fp_store)
    await closure_store.insert_closure_attempt(
        instance_id="i1",
        closure_id="c1",
        pattern_id="p1",
        invariant_id="inv1",
        active_epoch=0,
        route="code_change_via_cc",
        route_payload={},
        probe_kind="deterministic_introspection",
        probe_payload={},
        probe_payload_version=1,
    )
    await closure_store.insert_closure_attempt(
        instance_id="i1",
        closure_id="c2",
        pattern_id="p1",
        invariant_id="inv1",
        active_epoch=1,    # different episode
        route="code_change_via_cc",
        route_payload={},
        probe_kind="deterministic_introspection",
        probe_payload={},
        probe_payload_version=1,
    )


# ---------------------------------------------------------------------
# AC4 — record_closure_attempt idempotency
# ---------------------------------------------------------------------


async def test_ac4_record_closure_attempt_first_call_creates(
    closure_store, fp_store,
):
    await _seed_link(closure_store, fp_store)
    result = await record_closure_attempt(
        store=closure_store,
        instance_id="i1",
        pattern_id="p1",
        invariant_id="inv1",
        active_epoch=0,
        route="code_change_via_cc",
        route_payload={},
        probe_kind="deterministic_introspection",
        probe_payload={"x": 1},
        probe_payload_version=1,
    )
    assert result["newly_created"] is True
    assert result["closure_id"]


async def test_ac4_record_closure_attempt_second_call_returns_same(
    closure_store, fp_store,
):
    await _seed_link(closure_store, fp_store)
    first = await record_closure_attempt(
        store=closure_store,
        instance_id="i1",
        pattern_id="p1",
        invariant_id="inv1",
        active_epoch=0,
        route="code_change_via_cc",
        route_payload={},
        probe_kind="deterministic_introspection",
        probe_payload={},
        probe_payload_version=1,
    )
    second = await record_closure_attempt(
        store=closure_store,
        instance_id="i1",
        pattern_id="p1",
        invariant_id="inv1",
        active_epoch=0,
        route="code_change_via_cc",
        route_payload={},
        probe_kind="deterministic_introspection",
        probe_payload={},
        probe_payload_version=1,
    )
    assert second["newly_created"] is False
    assert second["closure_id"] == first["closure_id"]


# ---------------------------------------------------------------------
# AC8 — probe_kind allowlist hard-rejects in record_closure_attempt
# ---------------------------------------------------------------------


async def test_ac8_record_rejects_probe_kind_not_in_allowlist(
    closure_store, fp_store,
):
    await _seed_link(closure_store, fp_store)
    with pytest.raises(ProbeKindNotAllowed):
        await record_closure_attempt(
            store=closure_store,
            instance_id="i1",
            pattern_id="p1",
            invariant_id="inv1",
            active_epoch=0,
            route="code_change_via_cc",
            route_payload={},
            probe_kind="event_absence_window",  # deferred — not in allowlist
            probe_payload={},
            probe_payload_version=1,
        )


async def test_ac8_record_rejects_payload_bypass(closure_store, fp_store):
    """No payload-based bypass — putting bogus fields in route_payload
    does not unlock a deferred probe_kind."""
    await _seed_link(closure_store, fp_store)
    with pytest.raises(ProbeKindNotAllowed):
        await record_closure_attempt(
            store=closure_store,
            instance_id="i1",
            pattern_id="p1",
            invariant_id="inv1",
            active_epoch=0,
            route="code_change_via_cc",
            route_payload={
                "_approval_receipt": "fake-bypass-token",
                "force_kind": True,
            },
            probe_kind="manual_operator_confirmation",  # deferred
            probe_payload={},
            probe_payload_version=1,
        )


# ---------------------------------------------------------------------
# AC13 — record_closure_attempt rejects unlinked pairs
# ---------------------------------------------------------------------


async def test_ac13_rejects_when_no_link_row(closure_store, fp_store):
    """Pattern + invariant both exist but no link row →
    InvariantNotLinkedToPattern."""
    await _seed_friction_pattern(fp_store)
    await closure_store.insert_invariant(
        instance_id="i1", invariant_id="inv1",
        statement="x", owner="architect",
    )
    # No link insertion!
    with pytest.raises(InvariantNotLinkedToPattern):
        await record_closure_attempt(
            store=closure_store,
            instance_id="i1",
            pattern_id="p1",
            invariant_id="inv1",
            active_epoch=0,
            route="code_change_via_cc",
            route_payload={},
            probe_kind="deterministic_introspection",
            probe_payload={},
            probe_payload_version=1,
        )


# ---------------------------------------------------------------------
# AC14 — run_closure_probe idempotent replay
# ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_probe_runners():
    """Probe runner registry is module-level state; reset around
    each test so registrations don't leak."""
    clear_probe_runners()
    yield
    clear_probe_runners()


async def test_ac14_replay_on_passed_does_not_rerun(
    closure_store, fp_store,
):
    await _seed_link(closure_store, fp_store)
    call_count = {"n": 0}

    async def _runner(payload, ctx):
        call_count["n"] += 1
        return True, {"checked_count": 42}
    register_probe_runner("deterministic_introspection", _runner)

    rec = await record_closure_attempt(
        store=closure_store,
        instance_id="i1",
        pattern_id="p1",
        invariant_id="inv1",
        active_epoch=0,
        route="code_change_via_cc",
        route_payload={},
        probe_kind="deterministic_introspection",
        probe_payload={},
        probe_payload_version=1,
    )
    closure_id = rec["closure_id"]

    first = await run_closure_probe(
        store=closure_store,
        instance_id="i1",
        closure_id=closure_id,
    )
    assert first["outcome"] == "passed"
    assert first["replayed"] is False
    assert first["evidence"] == {"checked_count": 42}
    assert call_count["n"] == 1

    second = await run_closure_probe(
        store=closure_store,
        instance_id="i1",
        closure_id=closure_id,
    )
    assert second["outcome"] == "passed"
    assert second["replayed"] is True
    assert second["evidence"] == {"checked_count": 42}
    assert call_count["n"] == 1, "probe runner must not re-execute on replay"


async def test_ac14_replay_on_failed_does_not_re_emit(
    closure_store, fp_store,
):
    await _seed_link(closure_store, fp_store)

    async def _runner(payload, ctx):
        return False, {"divergent_tools": ["broken_tool"]}
    register_probe_runner("deterministic_introspection", _runner)

    emit_calls: list[dict] = []

    def _emit(*, instance_id, event_type, payload):
        emit_calls.append(
            {"instance_id": instance_id,
             "event_type": event_type, "payload": payload},
        )

    rec = await record_closure_attempt(
        store=closure_store,
        instance_id="i1",
        pattern_id="p1",
        invariant_id="inv1",
        active_epoch=0,
        route="code_change_via_cc",
        route_payload={},
        probe_kind="deterministic_introspection",
        probe_payload={},
        probe_payload_version=1,
    )
    closure_id = rec["closure_id"]

    first = await run_closure_probe(
        store=closure_store,
        instance_id="i1",
        closure_id=closure_id,
        event_emit_fn=_emit,
    )
    assert first["outcome"] == "failed"
    assert first["replayed"] is False
    assert len(emit_calls) == 1
    assert emit_calls[0]["event_type"] == "closure.probe_failed"
    assert emit_calls[0]["payload"]["pattern_id"] == "p1"
    assert emit_calls[0]["payload"]["closure_id"] == closure_id
    assert emit_calls[0]["payload"]["evidence"] == {
        "divergent_tools": ["broken_tool"],
    }

    second = await run_closure_probe(
        store=closure_store,
        instance_id="i1",
        closure_id=closure_id,
        event_emit_fn=_emit,
    )
    assert second["outcome"] == "failed"
    assert second["replayed"] is True
    # Crucial: did NOT re-emit.
    assert len(emit_calls) == 1


async def test_run_probe_pass_calls_pattern_transition(
    closure_store, fp_store,
):
    """On probe pass: pattern_transition_fn invoked with
    new_state='resolved'."""
    await _seed_link(closure_store, fp_store)

    async def _runner(payload, ctx):
        return True, {"ok": True}
    register_probe_runner("deterministic_introspection", _runner)

    transition_calls: list[dict] = []

    def _transition(**kwargs):
        transition_calls.append(kwargs)

    rec = await record_closure_attempt(
        store=closure_store,
        instance_id="i1",
        pattern_id="p1",
        invariant_id="inv1",
        active_epoch=0,
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
        pattern_transition_fn=_transition,
    )
    assert len(transition_calls) == 1
    assert transition_calls[0]["pattern_id"] == "p1"
    assert transition_calls[0]["new_state"] == "resolved"
    assert transition_calls[0]["resolved_by_spec"] == (
        "self_improvement_closure"
    )


async def test_run_probe_fail_does_not_call_pattern_transition(
    closure_store, fp_store,
):
    await _seed_link(closure_store, fp_store)

    async def _runner(payload, ctx):
        return False, {"divergent": ["x"]}
    register_probe_runner("deterministic_introspection", _runner)

    transition_calls: list[dict] = []

    def _transition(**kwargs):
        transition_calls.append(kwargs)

    rec = await record_closure_attempt(
        store=closure_store,
        instance_id="i1",
        pattern_id="p1",
        invariant_id="inv1",
        active_epoch=0,
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
        pattern_transition_fn=_transition,
    )
    assert transition_calls == []


async def test_run_probe_rejects_unknown_closure_id(closure_store):
    from kernos.kernel.closure_store import ClosureAttemptNotFound
    with pytest.raises(ClosureAttemptNotFound):
        await run_closure_probe(
            store=closure_store,
            instance_id="i1",
            closure_id="never-existed",
        )


# ---------------------------------------------------------------------
# lookup_pattern_invariants
# ---------------------------------------------------------------------


async def test_lookup_returns_false_when_no_invariants(
    closure_store, fp_store,
):
    await _seed_friction_pattern(fp_store)
    out = await lookup_pattern_invariants(
        store=closure_store, instance_id="i1", pattern_id="p1",
    )
    assert out == {
        "has_invariants": False,
        "primary_invariant_id": "",
        "all_invariant_ids": [],
    }


async def test_lookup_returns_primary_and_all_ids(
    closure_store, fp_store,
):
    """Multiple invariants → primary is the first by ASC ordering;
    all_invariant_ids contains the full set."""
    await _seed_friction_pattern(fp_store)
    # Insert intentionally out-of-order so we can verify the ASC
    # ordering is what produces "primary".
    for inv_id in ("z-last", "a-first", "m-middle"):
        await closure_store.insert_invariant(
            instance_id="i1", invariant_id=inv_id,
            statement=f"stmt-{inv_id}", owner="architect",
        )
        await closure_store.insert_link(
            instance_id="i1", pattern_id="p1", invariant_id=inv_id,
        )
    out = await lookup_pattern_invariants(
        store=closure_store, instance_id="i1", pattern_id="p1",
    )
    assert out["has_invariants"] is True
    assert out["primary_invariant_id"] == "a-first"
    assert out["all_invariant_ids"] == ["a-first", "m-middle", "z-last"]


# ---------------------------------------------------------------------
# Sanity: route + probe-kind enumerated sets
# ---------------------------------------------------------------------


def test_route_classes_contains_code_change_via_cc():
    assert "code_change_via_cc" in ROUTE_CLASSES


def test_read_only_probe_kinds_v1_is_deterministic_introspection_only():
    assert READ_ONLY_PROBE_KINDS == frozenset({"deterministic_introspection"})


async def test_record_rejects_unknown_route(closure_store, fp_store):
    await _seed_link(closure_store, fp_store)
    with pytest.raises(ClosureStoreError, match="route="):
        await record_closure_attempt(
            store=closure_store,
            instance_id="i1",
            pattern_id="p1",
            invariant_id="inv1",
            active_epoch=0,
            route="not-a-real-route",
            route_payload={},
            probe_kind="deterministic_introspection",
            probe_payload={},
            probe_payload_version=1,
        )
