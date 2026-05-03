"""Tests for the GardenerService snapshot read surface (C1 of CAG).

Covers Section 7 of the COHORT-ADAPT-GARDENER spec + the design review edit #4:

  - ProposalCoalescer.snapshot_pending: non-mutating tuple copy
  - EvolutionRecord populated by consult_evolution / consult_section
  - GardenerService.current_observation_snapshot: frozen snapshot;
    no LLM, no mutation, no event emit
  - observation_age_seconds correctness

The cohort adapter (C2) reads via this surface only — never via
direct coalescer access or consultation invocations.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.cohorts.gardener import (
    EvolutionContext,
    GardenerDecision,
    SectionContext,
)
from kernos.kernel.gardener import (
    EvolutionRecord,
    GardenerSnapshot,
    GardenerService,
    PendingProposal,
    ProposalCoalescer,
)


# ---------------------------------------------------------------------------
# ProposalCoalescer.snapshot_pending
# ---------------------------------------------------------------------------


def _proposal(canvas_id: str = "c1", **kwargs) -> PendingProposal:
    base = dict(
        canvas_id=canvas_id,
        action="propose_split",
        confidence="high",
        rationale="canvas growing past size threshold",
        affected_pages=["index.md"],
        captured_at=datetime.now(timezone.utc),
        payload={},
    )
    base.update(kwargs)
    return PendingProposal(**base)


def test_snapshot_pending_returns_empty_tuple_when_buffer_empty():
    c = ProposalCoalescer()
    assert c.snapshot_pending("c1") == ()


def test_snapshot_pending_returns_buffered_copy():
    c = ProposalCoalescer()
    p1 = _proposal("c1")
    p2 = _proposal("c1", action="flag_stale")
    c.add(p1)
    c.add(p2)
    snap = c.snapshot_pending("c1")
    assert snap == (p1, p2)
    assert isinstance(snap, tuple)


def test_snapshot_pending_isolates_canvases():
    c = ProposalCoalescer()
    c.add(_proposal("c1"))
    c.add(_proposal("c2", action="propose_merge"))
    assert len(c.snapshot_pending("c1")) == 1
    assert len(c.snapshot_pending("c2")) == 1
    assert c.snapshot_pending("c1")[0].action == "propose_split"
    assert c.snapshot_pending("c2")[0].action == "propose_merge"


def test_snapshot_pending_does_not_mutate_buffer():
    """Snapshot followed by drain returns the same proposals — proof
    that snapshot did not consume them."""
    c = ProposalCoalescer()
    c.add(_proposal("c1"))
    c.add(_proposal("c1", action="flag_stale"))
    before_count = c.buffered_count("c1")
    snap1 = c.snapshot_pending("c1")
    snap2 = c.snapshot_pending("c1")
    assert snap1 == snap2  # idempotent
    assert c.buffered_count("c1") == before_count
    drained = c.drain("c1")
    # Drain returns the same proposals.
    assert len(drained) == 2


# ---------------------------------------------------------------------------
# GardenerService observation ledger + record
# ---------------------------------------------------------------------------


def _service() -> GardenerService:
    return GardenerService(
        canvas_service=MagicMock(),
        instance_db=MagicMock(),
        reasoning_service=MagicMock(),
    )


def test_record_evolution_populates_ledger_in_order():
    svc = _service()
    d1 = GardenerDecision(action="propose_split", confidence="high", pattern="growth")
    d2 = GardenerDecision(action="flag_stale", confidence="medium", pattern="growth")
    svc._record_evolution("canvas-1", d1, consultation="evolution")
    svc._record_evolution("canvas-1", d2, consultation="section")
    snap = svc.current_observation_snapshot(
        instance_id="i1", member_id="m1", canvas_id="canvas-1",
    )
    assert len(snap.recent_evolution) == 2
    assert snap.recent_evolution[0].action == "propose_split"
    assert snap.recent_evolution[1].action == "flag_stale"
    assert snap.recent_evolution[0].consultation == "evolution"
    assert snap.recent_evolution[1].consultation == "section"


def test_record_evolution_caps_at_max():
    svc = _service()
    svc._evolution_ledger_max = 3  # tighten for the test
    for i in range(5):
        svc._record_evolution(
            "c1",
            GardenerDecision(action=f"a{i}", confidence="high", pattern=""),
            consultation="evolution",
        )
    snap = svc.current_observation_snapshot(
        instance_id="i", member_id="m", canvas_id="c1",
    )
    # The ledger holds at most _evolution_ledger_max records.
    assert len(snap.recent_evolution) == 3
    # FIFO eviction — the three most recent survive.
    assert [r.action for r in snap.recent_evolution] == ["a2", "a3", "a4"]


def test_record_evolution_isolates_per_canvas():
    svc = _service()
    svc._record_evolution(
        "c1", GardenerDecision(action="x", confidence="high", pattern=""),
        consultation="evolution",
    )
    svc._record_evolution(
        "c2", GardenerDecision(action="y", confidence="high", pattern=""),
        consultation="evolution",
    )
    snap1 = svc.current_observation_snapshot(
        instance_id="i", member_id="m", canvas_id="c1",
    )
    snap2 = svc.current_observation_snapshot(
        instance_id="i", member_id="m", canvas_id="c2",
    )
    assert [r.action for r in snap1.recent_evolution] == ["x"]
    assert [r.action for r in snap2.recent_evolution] == ["y"]


def test_record_evolution_ignores_blank_canvas_id_or_none_decision():
    svc = _service()
    svc._record_evolution("", GardenerDecision(action="x", confidence="high"), consultation="evolution")
    svc._record_evolution("c1", None, consultation="evolution")  # type: ignore[arg-type]
    snap = svc.current_observation_snapshot(
        instance_id="i", member_id="m", canvas_id="c1",
    )
    assert snap.recent_evolution == ()


# ---------------------------------------------------------------------------
# current_observation_snapshot — non-mutating contract
# ---------------------------------------------------------------------------


def test_snapshot_is_frozen_dataclass():
    svc = _service()
    snap = svc.current_observation_snapshot(
        instance_id="i", member_id="m", canvas_id="c1",
    )
    assert isinstance(snap, GardenerSnapshot)
    with pytest.raises((AttributeError, Exception)):
        snap.canvas_id = "tampered"  # type: ignore[misc]


def test_snapshot_does_not_drain_proposals():
    svc = _service()
    svc.coalescer.add(_proposal("c1"))
    svc.coalescer.add(_proposal("c1", action="flag_stale"))

    before_count = svc.coalescer.buffered_count("c1")
    svc.current_observation_snapshot(
        instance_id="i", member_id="m", canvas_id="c1",
    )
    after_count = svc.coalescer.buffered_count("c1")
    assert before_count == after_count == 2

    # Calling snapshot twice in succession yields identical state.
    snap1 = svc.current_observation_snapshot(
        instance_id="i", member_id="m", canvas_id="c1",
    )
    snap2 = svc.current_observation_snapshot(
        instance_id="i", member_id="m", canvas_id="c1",
    )
    assert snap1.pending_proposals == snap2.pending_proposals
    assert svc.coalescer.buffered_count("c1") == 2


def test_snapshot_includes_pending_and_evolution_combined():
    svc = _service()
    svc.coalescer.add(_proposal("c1"))
    svc._record_evolution(
        "c1",
        GardenerDecision(action="propose_split", confidence="high"),
        consultation="evolution",
    )
    snap = svc.current_observation_snapshot(
        instance_id="i", member_id="m", canvas_id="c1",
    )
    assert len(snap.pending_proposals) == 1
    assert len(snap.recent_evolution) == 1
    assert snap.canvas_id == "c1"


def test_snapshot_observation_age_uses_newest_across_both_lists():
    svc = _service()
    old = datetime.now(timezone.utc) - timedelta(seconds=300)
    recent = datetime.now(timezone.utc) - timedelta(seconds=2)
    svc.coalescer.add(_proposal("c1", captured_at=old))
    # Manually inject a recent EvolutionRecord (bypasses _record_evolution
    # so we control occurred_at exactly).
    import collections as _coll
    svc._evolution_ledger["c1"] = _coll.deque(maxlen=10)
    svc._evolution_ledger["c1"].append(
        EvolutionRecord(
            decision_id="x",
            action="a",
            confidence="high",
            pattern="",
            affected_pages=(),
            occurred_at=recent,
            consultation="evolution",
        )
    )
    snap = svc.current_observation_snapshot(
        instance_id="i", member_id="m", canvas_id="c1",
    )
    # Newest is `recent` → age in seconds is small (≤ a few)
    assert snap.observation_age_seconds is not None
    assert 0 <= snap.observation_age_seconds <= 5


def test_snapshot_observation_age_none_when_both_empty():
    svc = _service()
    snap = svc.current_observation_snapshot(
        instance_id="i", member_id="m", canvas_id="c1",
    )
    assert snap.observation_age_seconds is None


def test_snapshot_returns_empty_lists_for_unknown_canvas():
    svc = _service()
    snap = svc.current_observation_snapshot(
        instance_id="i", member_id="m", canvas_id="ghost",
    )
    assert snap.pending_proposals == ()
    assert snap.recent_evolution == ()


# ---------------------------------------------------------------------------
# Snapshot doesn't invoke the reasoning service
# ---------------------------------------------------------------------------


def test_snapshot_makes_no_llm_call():
    """Acceptance criterion 3: the run function performs no model calls.
    The underlying snapshot read is the surface — verify it doesn't
    touch the reasoning service."""
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(
        side_effect=AssertionError("snapshot must not call LLM"),
    )
    reasoning.reason = AsyncMock(
        side_effect=AssertionError("snapshot must not call LLM"),
    )
    svc = GardenerService(
        canvas_service=MagicMock(),
        instance_db=MagicMock(),
        reasoning_service=reasoning,
    )
    svc.coalescer.add(_proposal("c1"))
    svc._record_evolution(
        "c1",
        GardenerDecision(action="x", confidence="high"),
        consultation="evolution",
    )
    # Should complete without invoking any LLM method.
    snap = svc.current_observation_snapshot(
        instance_id="i", member_id="m", canvas_id="c1",
    )
    assert snap.canvas_id == "c1"
    reasoning.complete_simple.assert_not_called()
    reasoning.reason.assert_not_called()


# ---------------------------------------------------------------------------
# consult_evolution + consult_section populate the ledger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consult_evolution_records_to_ledger(monkeypatch):
    """When consult_evolution returns a non-None decision, an
    EvolutionRecord lands in the ledger keyed to the context's
    canvas_id."""
    svc = _service()

    async def fake_judge_evolution(ctx, *, reasoning_service):
        return GardenerDecision(
            action="propose_split",
            confidence="high",
            rationale="growing fast",
            pattern="growth",
            affected_pages=["index.md"],
        )

    # Patch the imported judge_evolution at the GardenerService module
    # level.
    monkeypatch.setattr(
        "kernos.kernel.gardener.judge_evolution", fake_judge_evolution,
    )

    # Stub out _ensure_patterns_loaded to avoid touching CanvasService.
    async def _noop(_iid):
        return None

    monkeypatch.setattr(svc, "_ensure_patterns_loaded", _noop)

    ctx = EvolutionContext(
        instance_id="inst",
        canvas_id="canvas-evo",
        canvas_pattern="unmatched",
        event_type="canvas.page.created",
        page_path="x.md",
        page_summary="...",
        canvas_pages_index=[],
        cross_pattern_heuristics="",
    )
    decision = await svc.consult_evolution(ctx)
    assert decision is not None
    snap = svc.current_observation_snapshot(
        instance_id="inst", member_id="m", canvas_id="canvas-evo",
    )
    assert len(snap.recent_evolution) == 1
    assert snap.recent_evolution[0].action == "propose_split"
    assert snap.recent_evolution[0].consultation == "evolution"


@pytest.mark.asyncio
async def test_consult_evolution_returning_none_does_not_record(monkeypatch):
    svc = _service()

    async def fake_judge_evolution(ctx, *, reasoning_service):
        return None

    async def _noop(_iid):
        return None

    monkeypatch.setattr(
        "kernos.kernel.gardener.judge_evolution", fake_judge_evolution,
    )
    monkeypatch.setattr(svc, "_ensure_patterns_loaded", _noop)

    ctx = EvolutionContext(
        instance_id="inst",
        canvas_id="canvas-evo",
        canvas_pattern="unmatched",
        event_type="canvas.page.created",
        page_path="x.md",
        page_summary="",
        canvas_pages_index=[],
        cross_pattern_heuristics="",
    )
    decision = await svc.consult_evolution(ctx)
    assert decision is None
    snap = svc.current_observation_snapshot(
        instance_id="inst", member_id="m", canvas_id="canvas-evo",
    )
    assert snap.recent_evolution == ()


@pytest.mark.asyncio
async def test_consult_section_records_to_ledger(monkeypatch):
    svc = _service()

    async def fake_judge_section(ctx, *, reasoning_service):
        return GardenerDecision(
            action="restructure_section",
            confidence="medium",
            rationale="section drifted",
            pattern="growth",
            affected_pages=["doc.md"],
        )

    monkeypatch.setattr(
        "kernos.kernel.gardener.judge_section_management",
        fake_judge_section,
    )

    ctx = SectionContext(
        instance_id="inst",
        canvas_id="canvas-sec",
        page_path="doc.md",
        section_slug="overview",
        section_heading="Overview",
        section_body="...",
        current_marker_summary="",
        current_marker_tokens=0,
    )
    decision = await svc.consult_section(ctx)
    assert decision is not None
    snap = svc.current_observation_snapshot(
        instance_id="inst", member_id="m", canvas_id="canvas-sec",
    )
    assert len(snap.recent_evolution) == 1
    assert snap.recent_evolution[0].consultation == "section"
    assert snap.recent_evolution[0].confidence == "medium"
