"""SELF-MAINTENANCE-REVIEW-V1 — daily self-stewardship review.

Pins: default-off kill switch, rotating-slice cursor, two-lens prompt,
structured parse + discipline (<=1 evolution idea), honest-when-healthy,
dedup TTL, and the idle-aware once/24h orchestration.
"""
from __future__ import annotations

import pytest

from kernos.kernel import self_maintenance_review as smr


# --- kill switch -----------------------------------------------------------


def test_default_on(monkeypatch):
    # SELF-MAINTENANCE-REVIEW-V3: ships DEFAULT-ON (reflection-only, idle-aware).
    monkeypatch.delenv("KERNOS_SELF_MAINTENANCE_REVIEW", raising=False)
    assert smr.is_enabled() is True


def test_explicit_off_disables(monkeypatch):
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "1")
    assert smr.is_enabled() is True
    for off in ("0", "false", "off", "no"):
        monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", off)
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
    seen = {}
    rep = {"slice": "awareness", "corrective_findings": ["dup finding"],
           "evolution_idea": "evolve x", "serves_the_whole": True}
    first, fresh = smr.filter_seen(rep, seen, "2026-06-04T00:00:00+00:00")
    assert first["corrective_findings"] == ["dup finding"]
    assert first["evolution_idea"] == "evolve x"
    seen.update(fresh)   # caller commits after surfacing
    # same report next day -> both suppressed
    second, _ = smr.filter_seen(rep, seen, "2026-06-05T00:00:00+00:00")
    assert second["corrective_findings"] == []
    assert second["evolution_idea"] is None


def test_filter_seen_reraises_after_ttl_expiry():
    seen = {}
    rep = {"slice": "awareness", "corrective_findings": ["aged finding"]}
    _, fresh = smr.filter_seen(rep, seen, "2026-06-04T00:00:00+00:00")
    seen.update(fresh)
    seen = smr.prune_seen(seen, "2026-07-04T00:00:00+00:00")  # >14d -> expired
    later, _ = smr.filter_seen(rep, seen, "2026-07-04T00:00:00+00:00")
    assert later["corrective_findings"] == ["aged finding"]


def test_filter_seen_does_not_mutate_caller_seen():
    seen = {}
    rep = {"slice": "x", "corrective_findings": ["f"]}
    smr.filter_seen(rep, seen, "2026-06-04T00:00:00+00:00")
    assert seen == {}   # Codex #2: caller commits, filter_seen must not mutate


# --- Codex code-review fixes ----------------------------------------------


def test_evolution_idea_dropped_unless_serves_the_whole():
    # serves_the_whole False -> the idea is NOT raised (discipline #4)
    r = smr.parse_review(
        '```json\n{"overall_health":"minor_concerns","corrective_findings":[],'
        '"evolution_idea":"rip it out","serves_the_whole":false}\n```', "gate",
    )
    assert r["evolution_idea"] is None
    assert smr.has_anything_to_say(r) is False  # nothing fresh/serving


@pytest.mark.asyncio
async def test_failed_whisper_does_not_bury_finding(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "1")
    d = str(tmp_path)
    payload = (
        '```json\n{"overall_health":"minor_concerns",'
        '"corrective_findings":["real concern"],"evolution_idea":null,'
        '"serves_the_whole":true}\n```'
    )
    async def _consult(_p, _s=None): return payload
    async def _boom(_t, _r): raise RuntimeError("whisper down")

    r1 = await smr.maybe_run_daily(
        data_dir=d, now_iso="2026-06-04T00:00:00+00:00",
        consult_fn=_consult, whisper_fn=_boom,
    )
    assert r1["outcome"] == "reviewed_quiet"        # surface failed
    assert smr.load_state(d)["seen"] == {}          # NOT buried
    # next cycle, whisper works -> the concern still surfaces
    seen_whispers = []
    async def _ok(t, rr): seen_whispers.append(t)
    # same slice again: force cursor back + clear the daily gate
    st = smr.load_state(d); st["cursor"] -= 1; st["last_run_iso"] = ""
    smr.save_state(d, st)
    r2 = await smr.maybe_run_daily(
        data_dir=d, now_iso="2026-06-05T00:00:00+00:00",
        consult_fn=_consult, whisper_fn=_ok,
    )
    assert r2["outcome"] == "reviewed_surfaced"
    assert seen_whispers and "real concern" in seen_whispers[0]


@pytest.mark.asyncio
async def test_parse_failure_does_not_advance_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "1")
    d = str(tmp_path)
    async def _consult(_p, _s=None): return "I have opinions but no json block."
    r = await smr.maybe_run_daily(
        data_dir=d, now_iso="2026-06-04T00:00:00+00:00", consult_fn=_consult,
    )
    assert r["outcome"] == "parse_error"
    assert smr.load_state(d)["cursor"] == 0   # NOT counted as a clean review


@pytest.mark.asyncio
async def test_review_writes_audit_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "1")
    d = str(tmp_path)
    payload = (
        '```json\n{"overall_health":"healthy","corrective_findings":[],'
        '"evolution_idea":null,"serves_the_whole":true}\n```'
    )
    async def _consult(_p, _s=None): return payload
    await smr.maybe_run_daily(
        data_dir=d, now_iso="2026-06-04T00:00:00+00:00", consult_fn=_consult,
    )
    import json as _json
    receipts = (tmp_path / "self_maintenance_receipts.jsonl").read_text().splitlines()
    assert len(receipts) == 1
    rec = _json.loads(receipts[0])
    assert rec["outcome"] == "reviewed_quiet" and rec["overall_health"] == "healthy"


# --- orchestration ---------------------------------------------------------


def _consult_returning(payload: str):
    async def _c(_prompt, _slice=None):
        return payload
    return _c


@pytest.mark.asyncio
async def test_maybe_run_inert_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "0")  # explicit off
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
async def test_maybe_run_surfaces_and_records_coverage(tmp_path, monkeypatch):
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
        repo_root=d,   # no churn signal → deterministic floor pick
    )
    assert res["outcome"] == "reviewed_surfaced"
    assert len(whispers) == 1
    assert "evolution" in whispers[0][0].lower()
    # V2: per-slice coverage recorded for exactly the reviewed slice
    st = smr.load_state(d)
    assert len(st["last_reviewed"]) == 1
    assert res["report"]["slice"] in st["last_reviewed"]


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
        consult_fn=_consult_returning(healthy), repo_root=d,
    )
    assert r1["outcome"] == "reviewed_quiet"   # healthy => nothing surfaced
    # an hour later -> not due (< 20h)
    r2 = await smr.maybe_run_daily(
        data_dir=d, now_iso="2026-06-04T01:00:00+00:00",
        consult_fn=_consult_returning(healthy), repo_root=d,
    )
    assert r2["outcome"] == "not_due"
    # next day -> due again, the floor moves to a fresh (distinct) slice
    r3 = await smr.maybe_run_daily(
        data_dir=d, now_iso="2026-06-05T00:00:00+00:00",
        consult_fn=_consult_returning(healthy), repo_root=d,
    )
    assert r3["outcome"] == "reviewed_quiet"
    assert len(smr.load_state(d)["last_reviewed"]) == 2   # two distinct slices covered


# --- the methodology reviews itself (constitutional, human-gated) ----------


def test_methodology_reviews_itself():
    names = {s.name for s in smr.REVIEW_SLICES}
    # nothing is exempt: the maintenance methodology + self-healing + the
    # governing intention are all in scope.
    assert "self-maintenance-methodology" in names
    assert "self-healing" in names
    assert "governing-intention" in names
    meta = next(s for s in smr.REVIEW_SLICES
                if s.name == "self-maintenance-methodology")
    assert "kernos/kernel/self_maintenance_review.py" in meta.paths
    assert meta.constitutional is True


def test_constitutional_slices_are_human_gated_in_prompt():
    meta = next(s for s in smr.REVIEW_SLICES if s.constitutional)
    p = smr.build_review_prompt(meta).lower()
    assert "constitutional" in p and "human-gated" in p
    assert "not be self-applied" in p or "must not be self-applied" in p


def test_constitutional_whisper_routes_to_founder_not_self_apply():
    report = {
        "slice": "self-maintenance-methodology", "overall_health": "healthy",
        "corrective_findings": [], "evolution_idea": "tighten the dedup window",
        "serves_the_whole": True, "constitutional": True,
    }
    text = smr.to_whisper_text(report).lower()
    assert "constitutional" in text
    assert "founder" in text and "human-gated" in text


@pytest.mark.asyncio
async def test_constitutional_flag_survives_to_report(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "1")
    d = str(tmp_path)
    # V2: target the first constitutional slice directly
    meta = next(s for s in smr.REVIEW_SLICES if s.constitutional)
    payload = (
        '```json\n{"overall_health":"healthy","corrective_findings":[],'
        '"evolution_idea":"a minor tweak","serves_the_whole":true}\n```'
    )
    captured = []
    async def _whisper(text, report): captured.append(report)
    res = await smr.maybe_run_daily(
        data_dir=d, now_iso="2026-06-04T00:00:00+00:00",
        consult_fn=_consult_returning(payload), whisper_fn=_whisper,
        target=meta.name, repo_root=d,
    )
    assert res["report"]["constitutional"] is True
    assert captured and captured[0]["constitutional"] is True


# --- live source loader (read budget) --------------------------------------


def test_load_bounded_source_reads_files_and_caps(tmp_path):
    (tmp_path / "kernos").mkdir()
    (tmp_path / "kernos" / "a.py").write_text("\n".join(f"line{i}" for i in range(500)))
    sl = smr.ReviewSlice("t", "intent", ("kernos/a.py",))
    out = smr.load_bounded_source(sl, str(tmp_path), max_lines_per_file=50)
    assert "kernos/a.py" in out
    assert "line0" in out and "line49" in out
    assert "line60" not in out          # capped at 50 lines/file
    assert "…" in out                   # truncation marker


def test_load_bounded_source_hard_char_caps(tmp_path):
    (tmp_path / "big.py").write_text("X" * 100000)        # one huge line
    sl = smr.ReviewSlice("t", "i", ("big.py",))
    out = smr.load_bounded_source(sl, str(tmp_path), max_line_chars=80,
                                  max_total_chars=2000)
    assert len(out) <= 2000                               # HARD ceiling
    assert "XXXX" in out                                  # something was read
    # even an absurdly tiny budget is a true ceiling
    tiny = smr.load_bounded_source(sl, str(tmp_path), max_total_chars=10)
    assert len(tiny) <= 10


def test_load_bounded_source_skips_paths_outside_root(tmp_path):
    root = tmp_path / "repo"; root.mkdir()
    (root / "inside.py").write_text("safe\n")
    (tmp_path / "secret.py").write_text("ESCAPED\n")
    sl = smr.ReviewSlice("t", "i", ("inside.py", "../secret.py"))
    out = smr.load_bounded_source(sl, str(root))
    assert "safe" in out
    assert "ESCAPED" not in out          # traversal path skipped


def test_load_bounded_source_handles_directory_and_missing(tmp_path):
    d = tmp_path / "pkg"; d.mkdir()
    (d / "one.py").write_text("alpha\nbeta\n")
    (d / "two.py").write_text("gamma\n")
    sl = smr.ReviewSlice("t", "i", ("pkg/", "does/not/exist.py"))
    out = smr.load_bounded_source(sl, str(tmp_path), max_files_per_dir=4)
    assert "alpha" in out and "gamma" in out   # dir expanded
    # missing path simply contributes nothing; no crash
    assert "one.py" in out


# --- on-demand force (operator-initiated /selfreview) ----------------------


@pytest.mark.asyncio
async def test_force_runs_even_when_disabled_and_not_due(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "0")  # explicit OFF
    d = str(tmp_path)
    healthy = (
        '```json\n{"overall_health":"healthy","corrective_findings":[],'
        '"evolution_idea":null,"serves_the_whole":true}\n```'
    )
    async def _c(_p, _s=None): return healthy
    # forced run works despite the kill switch being off + not-due + busy
    r1 = await smr.maybe_run_daily(
        data_dir=d, now_iso="2026-06-04T00:00:00+00:00",
        consult_fn=_c, busy=True, force=True, repo_root=d,
    )
    assert r1["outcome"] == "reviewed_quiet"
    assert len(smr.load_state(d)["last_reviewed"]) == 1
    # immediately again (would be not_due) — force still runs, next slice
    r2 = await smr.maybe_run_daily(
        data_dir=d, now_iso="2026-06-04T00:05:00+00:00",
        consult_fn=_c, force=True, repo_root=d,
    )
    assert r2["outcome"] == "reviewed_quiet"
    assert len(smr.load_state(d)["last_reviewed"]) == 2


@pytest.mark.asyncio
async def test_no_force_still_gated_by_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "0")  # explicit OFF
    async def _c(_p, _s=None): return "x"
    r = await smr.maybe_run_daily(
        data_dir=str(tmp_path), now_iso="2026-06-04T00:00:00+00:00",
        consult_fn=_c,
    )
    assert r["outcome"] == "disabled"   # autonomous path stays gated
