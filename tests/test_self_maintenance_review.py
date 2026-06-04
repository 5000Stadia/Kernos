"""SELF-MAINTENANCE-REVIEW-V1 — daily self-stewardship review.

Pins: default-off kill switch, rotating-slice cursor, two-lens prompt,
structured parse + discipline (<=1 evolution idea), honest-when-healthy,
dedup TTL, and the idle-aware once/24h orchestration.
"""
from __future__ import annotations

import pytest

from kernos.kernel import self_maintenance_review as smr


# --- kill switch -----------------------------------------------------------


def test_default_off(monkeypatch):
    monkeypatch.delenv("KERNOS_SELF_MAINTENANCE_REVIEW", raising=False)
    assert smr.is_enabled() is False


def test_enabled_when_set(monkeypatch):
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "1")
    assert smr.is_enabled() is True
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "off")
    assert smr.is_enabled() is False


# --- rotating slices -------------------------------------------------------


def test_cursor_rotates_through_all_slices():
    n = len(smr.REVIEW_SLICES)
    seen = {smr.slice_for_cursor(i).name for i in range(n)}
    assert len(seen) == n               # every slice reachable
    assert smr.slice_for_cursor(n).name == smr.slice_for_cursor(0).name  # wraps


def test_prompt_carries_both_lenses_and_discipline():
    p = smr.build_review_prompt(smr.REVIEW_SLICES[0]).lower()
    assert "corrective" in p and "generative" in p
    assert "overarching intention of the whole" in p
    assert "at most one" in p and "out-of-hand mutation" in p


# --- parse + discipline ----------------------------------------------------


def test_parse_extracts_structured_block():
    text = (
        "I reviewed the slice.\n\n```json\n"
        '{"overall_health": "healthy", "corrective_findings": [],'
        ' "evolution_idea": null, "serves_the_whole": true,'
        ' "serves_the_whole_why": "core path", "suggested_direction": "none"}'
        "\n```"
    )
    r = smr.parse_review(text, "reasoning")
    assert r["overall_health"] == "healthy"
    assert r["serves_the_whole"] is True
    assert r["evolution_idea"] is None


def test_parse_enforces_single_evolution_idea():
    text = (
        "```json\n"
        '{"overall_health": "minor_concerns", "corrective_findings": ["x"],'
        ' "evolution_idea": ["idea one", "idea two"], "serves_the_whole": true}'
        "\n```"
    )
    r = smr.parse_review(text, "awareness")
    assert r["evolution_idea"] == "idea one"   # discipline: <=1


def test_parse_falls_back_on_malformed_block():
    r = smr.parse_review("no json here, just prose musings", "workflows")
    assert r["slice"] == "workflows"
    assert r["raw"]


# --- honest-when-healthy ---------------------------------------------------


def test_healthy_quiet_report_has_nothing_to_say():
    r = smr.parse_review(
        '```json\n{"overall_health":"healthy","corrective_findings":[],'
        '"evolution_idea":null,"serves_the_whole":true}\n```', "state-store",
    )
    assert smr.has_anything_to_say(r) is False


def test_report_with_evolution_idea_has_something_to_say():
    r = smr.parse_review(
        '```json\n{"overall_health":"healthy","corrective_findings":[],'
        '"evolution_idea":"cache the registry lookup","serves_the_whole":true}'
        '\n```', "reasoning",
    )
    assert smr.has_anything_to_say(r) is True


# --- dedup -----------------------------------------------------------------


def test_filter_seen_suppresses_repeats_within_ttl():
    state = {"cursor": 0, "last_run_iso": "", "seen": {}}
    rep = {"slice": "awareness", "corrective_findings": ["dup finding"],
           "evolution_idea": "evolve x"}
    first = smr.filter_seen(rep, state, "2026-06-04T00:00:00+00:00")
    assert first["corrective_findings"] == ["dup finding"]
    assert first["evolution_idea"] == "evolve x"
    # same report next day -> both suppressed
    second = smr.filter_seen(rep, state, "2026-06-05T00:00:00+00:00")
    assert second["corrective_findings"] == []
    assert second["evolution_idea"] is None


def test_filter_seen_reraises_after_ttl_expiry():
    state = {"cursor": 0, "last_run_iso": "", "seen": {}}
    rep = {"slice": "awareness", "corrective_findings": ["aged finding"],
           "evolution_idea": None}
    smr.filter_seen(rep, state, "2026-06-04T00:00:00+00:00")
    # 30 days later (> 14d TTL) -> surfaces again
    later = smr.filter_seen(rep, state, "2026-07-04T00:00:00+00:00")
    assert later["corrective_findings"] == ["aged finding"]


# --- orchestration ---------------------------------------------------------


def _consult_returning(payload: str):
    async def _c(_prompt):
        return payload
    return _c


@pytest.mark.asyncio
async def test_maybe_run_inert_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("KERNOS_SELF_MAINTENANCE_REVIEW", raising=False)
    res = await smr.maybe_run_daily(
        data_dir=str(tmp_path), now_iso="2026-06-04T00:00:00+00:00",
        consult_fn=_consult_returning("x"),
    )
    assert res["outcome"] == "disabled"


@pytest.mark.asyncio
async def test_maybe_run_defers_when_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "1")
    res = await smr.maybe_run_daily(
        data_dir=str(tmp_path), now_iso="2026-06-04T00:00:00+00:00",
        consult_fn=_consult_returning("x"), busy=True,
    )
    assert res["outcome"] == "busy"


@pytest.mark.asyncio
async def test_maybe_run_surfaces_and_advances_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "1")
    d = str(tmp_path)
    payload = (
        "reviewed\n```json\n"
        '{"overall_health":"minor_concerns","corrective_findings":["tighten X"],'
        '"evolution_idea":"extract a helper","serves_the_whole":true,'
        '"serves_the_whole_why":"central","suggested_direction":"consider"}\n```'
    )
    whispers = []
    async def _whisper(text, report): whispers.append((text, report))

    res = await smr.maybe_run_daily(
        data_dir=d, now_iso="2026-06-04T00:00:00+00:00",
        consult_fn=_consult_returning(payload), whisper_fn=_whisper,
    )
    assert res["outcome"] == "reviewed_surfaced"
    assert len(whispers) == 1
    assert "evolution" in whispers[0][0].lower()
    assert smr.load_state(d)["cursor"] == 1   # advanced


@pytest.mark.asyncio
async def test_maybe_run_not_due_second_time_same_day(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "1")
    d = str(tmp_path)
    healthy = (
        '```json\n{"overall_health":"healthy","corrective_findings":[],'
        '"evolution_idea":null,"serves_the_whole":true}\n```'
    )
    r1 = await smr.maybe_run_daily(
        data_dir=d, now_iso="2026-06-04T00:00:00+00:00",
        consult_fn=_consult_returning(healthy),
    )
    assert r1["outcome"] == "reviewed_quiet"   # healthy => nothing surfaced
    # an hour later -> not due (< 20h)
    r2 = await smr.maybe_run_daily(
        data_dir=d, now_iso="2026-06-04T01:00:00+00:00",
        consult_fn=_consult_returning(healthy),
    )
    assert r2["outcome"] == "not_due"
    # next day -> due again, cursor advanced to slice 2
    r3 = await smr.maybe_run_daily(
        data_dir=d, now_iso="2026-06-05T00:00:00+00:00",
        consult_fn=_consult_returning(healthy),
    )
    assert r3["outcome"] == "reviewed_quiet"
    assert smr.load_state(d)["cursor"] == 2
