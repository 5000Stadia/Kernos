"""SELF-MAINTENANCE-REVIEW-V2 — signal-promoted selection + on-demand targeting.

Acceptance criteria from specs/SELF-MAINTENANCE-REVIEW-V2.md.
"""
import json

from kernos.kernel import self_maintenance_review as smr

NOW = "2026-06-05T00:00:00+00:00"
RECENT = "2026-06-04T12:00:00+00:00"      # ~half a day old
OLD = "2026-05-01T00:00:00+00:00"          # ~35 days old (past the 10d floor)
HEALTHY = ('```json\n'
           '{"overall_health":"healthy","corrective_findings":[],'
           '"evolution_idea":null,"serves_the_whole":true}\n```')


# --- selection ------------------------------------------------------------

def test_signal_promotes_within_floor():
    S = smr.REVIEW_SLICES
    last = {s.name: RECENT for s in S}
    sig = {s.name: 0 for s in S}
    sig["dispatch-gate"] = 2
    assert smr.select_slice(S, {"last_reviewed": last}, sig, NOW).name == "dispatch-gate"


def test_no_signal_picks_least_recently_reviewed():
    S = smr.REVIEW_SLICES
    last = {s.name: RECENT for s in S}
    last[S[5].name] = "2026-06-01T00:00:00+00:00"   # 4d old, < floor, stalest
    sig = {s.name: 0 for s in S}
    assert smr.select_slice(S, {"last_reviewed": last}, sig, NOW).name == S[5].name


def test_coverage_floor_beats_fresh_signal():
    S = smr.REVIEW_SLICES
    last = {s.name: RECENT for s in S}
    last["workflows"] = OLD                          # past the 10d floor
    sig = {s.name: 0 for s in S}
    sig["reasoning"] = 5                             # max signal elsewhere
    assert smr.select_slice(S, {"last_reviewed": last}, sig, NOW).name == "workflows"


def test_empty_state_sweeps_via_floor():
    S = smr.REVIEW_SLICES
    pick = smr.select_slice(S, {"last_reviewed": {}}, {s.name: 0 for s in S}, NOW)
    assert pick.name == S[0].name                    # all never-reviewed → index 0


# --- prefix-safe churn mapping -------------------------------------------

def test_path_matches_is_prefix_safe():
    assert smr._path_matches("kernos/kernel/workflows/x.py", "kernos/kernel/workflows/")
    assert smr._path_matches("kernos/kernel/gate.py", "kernos/kernel/gate.py")
    assert not smr._path_matches("kernos/kernel/gateway_other.py", "kernos/kernel/gate.py")
    assert not smr._path_matches("kernos/kernel/gatekeeper.py", "kernos/kernel/gate")


# --- targeting + allowlist -----------------------------------------------

def test_resolve_target_case_and_sep_insensitive():
    assert smr.resolve_target("Dispatch Gate").name == "dispatch-gate"
    assert smr.resolve_target("dispatch_gate").name == "dispatch-gate"
    assert smr.resolve_target("nope") is None


def test_instance_allowlist(monkeypatch):
    monkeypatch.delenv("KERNOS_SMR_INSTANCE_ALLOWLIST", raising=False)
    assert smr.instance_allowed("a")
    monkeypatch.setenv("KERNOS_SMR_INSTANCE_ALLOWLIST", "a, b")
    assert smr.instance_allowed("a")
    assert not smr.instance_allowed("c")


def test_signal_collection_degrades_with_no_git_or_friction(tmp_path):
    scores = smr.collect_signal_scores(
        smr.REVIEW_SLICES, str(tmp_path), str(tmp_path), 7, 5, NOW)
    assert set(scores.values()) == {0}               # no raise, all zero


# --- maybe_run_daily integration -----------------------------------------

async def test_unknown_target_runs_nothing(tmp_path):
    calls = []

    async def consult(prompt, slice_):
        calls.append(slice_.name)
        return HEALTHY

    res = await smr.maybe_run_daily(
        data_dir=str(tmp_path), now_iso=NOW, consult_fn=consult,
        whisper_fn=None, force=True, target="nonsense", repo_root=str(tmp_path))

    assert res["outcome"] == "unknown_target"
    assert calls == []                               # no consult
    assert not (tmp_path / "self_maintenance_review.json").exists()  # no state write


async def test_targeted_review_stamps_only_that_slice(tmp_path):
    async def consult(prompt, slice_):
        return HEALTHY

    res = await smr.maybe_run_daily(
        data_dir=str(tmp_path), now_iso=NOW, consult_fn=consult,
        whisper_fn=None, force=True, target="dispatch-gate", repo_root=str(tmp_path))

    assert res["slice"] == "dispatch-gate"
    st = json.loads((tmp_path / "self_maintenance_review.json").read_text())
    assert list(st["last_reviewed"].keys()) == ["dispatch-gate"]


async def test_parse_error_does_not_stamp_last_reviewed(tmp_path):
    async def consult(prompt, slice_):
        return "not a json verdict"

    res = await smr.maybe_run_daily(
        data_dir=str(tmp_path), now_iso=NOW, consult_fn=consult,
        whisper_fn=None, force=True, target="reasoning", repo_root=str(tmp_path))

    assert res["outcome"] == "parse_error"
    st = json.loads((tmp_path / "self_maintenance_review.json").read_text())
    assert st.get("last_reviewed", {}) == {}         # failed read stays eligible


def test_functional_map_covers_every_module_single_owner():
    mods = smr.list_modules(".")
    assert mods, "expected to find kernos modules"
    owner = smr.assign_owners(smr.REVIEW_SLICES, mods)
    assert all(owner.get(m) for m in mods)                 # nothing unassigned
    assert smr.unassigned_modules(smr.REVIEW_SLICES, ".") == []
    names = {s.name for s in smr.REVIEW_SLICES}
    assert set(owner.values()) <= names                    # owners are real elements


def test_single_owner_specificity_beats_prefix():
    owner = smr.assign_owners(smr.REVIEW_SLICES, smr.list_modules("."))
    # exact-file ownership beats a broader dir prefix in another element
    assert owner.get("kernos/kernel/tools/operation_resolver.py") == "dispatch-gate"
    assert owner.get("kernos/kernel/workflows/self_improvement_helper.py") == "improvement-loop"


def test_shape_fingerprint_is_stable_and_nonempty():
    fp = smr.shape_fingerprint(".")
    assert fp and fp == smr.shape_fingerprint(".")


async def test_coverage_scan_records_fingerprint_no_gap_on_full_map(tmp_path):
    async def consult(prompt, slice_):
        return HEALTHY

    await smr.maybe_run_daily(
        data_dir=str(tmp_path), now_iso=NOW, consult_fn=consult,
        whisper_fn=None, force=True, repo_root=".")

    st = json.loads((tmp_path / "self_maintenance_review.json").read_text())
    assert st["shape_fingerprint"]                          # recorded every scan
    assert st.get("gap_surfaced_fingerprint", "") == ""     # full map → no gap surfaced


async def test_coverage_gap_retries_on_failed_surface(tmp_path, monkeypatch):
    # Trim the map so evals modules are unassigned → a real gap. First surface
    # FAILS; the gap must re-surface next scan (Codex must-fix: gate on
    # gap_surfaced_fingerprint, not on 'changed since last scan').
    trimmed = tuple(s for s in smr.REVIEW_SLICES if s.name != "evals-soak")
    monkeypatch.setattr(smr, "REVIEW_SLICES", trimmed)
    seen = {"gap_calls": 0, "fail_next": True}

    async def whisper(text, report):
        if report.get("kind") == "coverage_gap":
            seen["gap_calls"] += 1
            if seen["fail_next"]:
                seen["fail_next"] = False
                raise RuntimeError("surface boom")

    async def consult(prompt, slice_):
        return HEALTHY

    await smr.maybe_run_daily(data_dir=str(tmp_path), now_iso=NOW,
                              consult_fn=consult, whisper_fn=whisper,
                              force=True, repo_root=".")
    st = json.loads((tmp_path / "self_maintenance_review.json").read_text())
    assert seen["gap_calls"] == 1
    assert st.get("gap_surfaced_fingerprint", "") == ""   # failed → not marked

    await smr.maybe_run_daily(data_dir=str(tmp_path), now_iso=NOW,
                              consult_fn=consult, whisper_fn=whisper,
                              force=True, repo_root=".")
    assert seen["gap_calls"] == 2                          # RETRIED
    st2 = json.loads((tmp_path / "self_maintenance_review.json").read_text())
    assert st2["gap_surfaced_fingerprint"] == st2["shape_fingerprint"]


async def test_v1_state_migrates_and_runs(tmp_path):
    (tmp_path / "self_maintenance_review.json").write_text(
        json.dumps({"cursor": 3, "last_run_iso": "x", "seen": {}}))
    assert smr.load_state(str(tmp_path))["last_reviewed"] == {}

    async def consult(prompt, slice_):
        return HEALTHY

    res = await smr.maybe_run_daily(
        data_dir=str(tmp_path), now_iso=NOW, consult_fn=consult,
        whisper_fn=None, force=True, repo_root=str(tmp_path))
    assert res["outcome"] in ("reviewed_quiet", "reviewed_surfaced")
