"""RECURSIVE-SELF-HEAL-V1 safety foundation — the guards Codex's review
(§9) flagged as load-bearing: durable runaway bound, classifier negative
guards, constitutional boundary, default-off kill switch."""
from __future__ import annotations

import pytest

from kernos.kernel import recursive_self_heal as rsh


# ---------------------------------------------------------------------------
# Kill switch (§9.5) — inert by default
# ---------------------------------------------------------------------------


def test_default_off(monkeypatch):
    monkeypatch.delenv("KERNOS_RECURSIVE_SELF_HEAL", raising=False)
    assert rsh.is_enabled() is False


def test_enabled_when_set(monkeypatch):
    monkeypatch.setenv("KERNOS_RECURSIVE_SELF_HEAL", "1")
    assert rsh.is_enabled() is True
    monkeypatch.setenv("KERNOS_RECURSIVE_SELF_HEAL", "off")
    assert rsh.is_enabled() is False


# ---------------------------------------------------------------------------
# Classifier (§9.1) — positive AND negative guard; default task_failure
# ---------------------------------------------------------------------------


def test_machinery_requires_positive_and_negative_guard():
    # positive symptom (no_diff) AND objectively dirty worktree -> machinery
    diag = {"reason": "false_green", "worktree_objectively_dirty": True}
    assert rsh.classify_failure(diag) == "worktree_dirty_state_invariant"


def test_false_green_with_pristine_worktree_is_task_failure():
    # positive symptom but NOT objectively dirty -> the agent really wrote
    # nothing -> TASK failure, must NOT recurse.
    diag = {"reason": "false_green", "worktree_objectively_dirty": False}
    assert rsh.classify_failure(diag) == rsh.TASK_FAILURE


def test_unrelated_failure_is_task_failure():
    assert rsh.classify_failure({"reason": "agent timed out"}) == rsh.TASK_FAILURE
    assert rsh.classify_failure({}) == rsh.TASK_FAILURE
    assert rsh.classify_failure(None) == rsh.TASK_FAILURE


# ---------------------------------------------------------------------------
# Constitutional boundary (§9.4)
# ---------------------------------------------------------------------------


def test_constitutional_paths_detected():
    hits = rsh.touches_constitutional_path([
        "docs/LOOP-TEST.md",
        "kernos/kernel/gate.py",                  # guardrail
        "kernos/kernel/external_agents/acpx_adapter.py",  # prefix
        "kernos/kernel/kernel_tool_registry.py",  # substring
    ])
    assert "kernos/kernel/gate.py" in hits
    assert "kernos/kernel/external_agents/acpx_adapter.py" in hits
    assert any("kernel_tool_registry" in h for h in hits)
    assert "docs/LOOP-TEST.md" not in hits


def test_benign_change_is_not_constitutional():
    assert rsh.touches_constitutional_path(["docs/LOOP-TEST.md"]) == []


# ---------------------------------------------------------------------------
# Durable runaway bound (§9.3) — the heart of the safety case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reserve_then_dedup_and_one_child_per_parent(tmp_path):
    d = str(tmp_path)
    await rsh.ensure_schema(d)
    fp = rsh.failure_fingerprint("worktree_dirty_state_invariant", {"reason": "no_diff"})

    ok, _ = await rsh.reserve_child_repair(
        data_dir=d, parent_attempt_id="att_parent", child_attempt_id="att_child1",
        signature_id="worktree_dirty_state_invariant", failure_fingerprint=fp,
        edge_id="e1", now_iso="2026-06-04T00:00:00+00:00",
    )
    assert ok is True

    # one child per parent: a second child for the same parent is rejected
    ok2, reason2 = await rsh.reserve_child_repair(
        data_dir=d, parent_attempt_id="att_parent", child_attempt_id="att_child2",
        signature_id="worktree_dirty_state_invariant", failure_fingerprint=fp,
        edge_id="e2", now_iso="2026-06-04T00:00:01+00:00",
    )
    assert ok2 is False

    # a DIFFERENT root (independent improvement run) with the same signature
    # IS allowed — de-dup is correctly per-root, not global.
    ok3, _ = await rsh.reserve_child_repair(
        data_dir=d, parent_attempt_id="att_other", child_attempt_id="att_child3",
        signature_id="worktree_dirty_state_invariant", failure_fingerprint=fp,
        edge_id="e3", now_iso="2026-06-04T00:00:02+00:00",
    )
    assert ok3 is True


@pytest.mark.asyncio
async def test_depth_bound_blocks_child_of_child(tmp_path):
    d = str(tmp_path)
    await rsh.ensure_schema(d)
    fp1 = rsh.failure_fingerprint("worktree_dirty_state_invariant", {"reason": "no_diff", "x": 1})
    # root -> child1 (depth 1) : allowed
    ok, _ = await rsh.reserve_child_repair(
        data_dir=d, parent_attempt_id="root", child_attempt_id="child1",
        signature_id="worktree_dirty_state_invariant", failure_fingerprint=fp1,
        edge_id="e1", now_iso="t0",
    )
    assert ok is True
    # child1 -> child2 (depth 2) : BLOCKED (max depth 1), even with a fresh
    # signature/fingerprint — depth is global/root-anchored.
    fp2 = rsh.failure_fingerprint("worktree_dirty_state_invariant", {"reason": "no_diff", "x": 2})
    ok2, reason2 = await rsh.reserve_child_repair(
        data_dir=d, parent_attempt_id="child1", child_attempt_id="child3",
        signature_id="worktree_dirty_state_invariant", failure_fingerprint=fp2,
        edge_id="e2", now_iso="t1",
    )
    assert ok2 is False
    assert "depth" in reason2


@pytest.mark.asyncio
async def test_reservation_durable_across_reopen(tmp_path):
    """The bound must survive a restart — a fresh connection still sees the
    reservation (in-memory counters would launder depth). Same parent that
    already has a child stays blocked after a 're-open'."""
    d = str(tmp_path)
    await rsh.ensure_schema(d)
    fp = rsh.failure_fingerprint("worktree_dirty_state_invariant", {"reason": "no_diff"})
    ok, _ = await rsh.reserve_child_repair(
        data_dir=d, parent_attempt_id="p", child_attempt_id="c",
        signature_id="worktree_dirty_state_invariant", failure_fingerprint=fp,
        edge_id="e1", now_iso="t0",
    )
    assert ok is True
    # simulate restart: re-ensure schema (idempotent), re-attempt — the same
    # parent's reservation persisted, so a second child is still blocked.
    await rsh.ensure_schema(d)
    ok2, _ = await rsh.reserve_child_repair(
        data_dir=d, parent_attempt_id="p", child_attempt_id="c2",
        signature_id="worktree_dirty_state_invariant", failure_fingerprint=fp,
        edge_id="e2", now_iso="t1",
    )
    assert ok2 is False  # one-child-per-parent survived the "restart"
