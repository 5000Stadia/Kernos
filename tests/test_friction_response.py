"""FRICTION-RESPONSE-V1 safety core — the guards Codex's spec review (§9)
made binding: self-friction denylist + in-flight reservation, two-key
signature/fingerprint anti-loop, signature cooldown + daily budget, post-deploy
verification states, archive-by-signature."""
from __future__ import annotations

import pytest

from kernos.kernel import friction_response as fr


# --- kill switch -----------------------------------------------------------


def test_default_off(monkeypatch):
    monkeypatch.delenv("KERNOS_FRICTION_RESPONSE", raising=False)
    assert fr.is_enabled() is False


def test_enabled_when_set(monkeypatch):
    monkeypatch.setenv("KERNOS_FRICTION_RESPONSE", "1")
    assert fr.is_enabled() is True


# --- two-key identity ------------------------------------------------------


def test_signature_is_stable_and_noise_free():
    a = fr.friction_signature(friction_type="CONNECTION_POOL_LEAK",
                              resource="instance.db")
    b = fr.friction_signature(friction_type="connection_pool_leak",
                              resource="INSTANCE.DB")
    assert a == b                      # case/whitespace normalized
    c = fr.friction_signature(friction_type="ACPX_TIMEOUT")
    assert c != a                      # different problem -> different sig
    assert a.startswith("sig_")


def test_pattern_id_takes_precedence():
    a = fr.friction_signature(friction_type="X", pattern_id="POOL_LEAK_V1")
    b = fr.friction_signature(friction_type="Y", pattern_id="POOL_LEAK_V1")
    assert a == b


def test_resolution_fingerprint_ignores_commit():
    a = fr.resolution_fingerprint(cause="close conn on error",
                                  touched=["kernos/kernel/state.py"])
    b = fr.resolution_fingerprint(cause="Close  conn on error",
                                  touched=["kernos/kernel/state.py"])
    assert a == b and a.startswith("fix_")
    c = fr.resolution_fingerprint(cause="raise pool size",
                                  touched=["kernos/kernel/state.py"])
    assert c != a


def test_signature_of_filename_all_three_conventions():
    # CURRENT writer (friction.py:446): FRICTION_<date>_<time>_<TYPE>_<uuid8>
    t0, _ = fr.signature_of_filename(
        "FRICTION_20260522_160433_PREFERENCE_STATED_BUT_NOT_CAPTURED_63aaecd1.md")
    assert t0 == "PREFERENCE_STATED_BUT_NOT_CAPTURED"   # no uuid leakage
    # LEGACY FRICTION_: hash in the MIDDLE
    t1, _ = fr.signature_of_filename(
        "FRICTION_20260529_070502_faf2916a_ACPX_TIMEOUT_CLAUDE_CODE.md")
    assert t1 == "ACPX_TIMEOUT_CLAUDE_CODE"
    # timestamp-prefixed (the convention the existing globs MISS)
    t2, s2 = fr.signature_of_filename(
        "2026-06-01T07-51-41_CONNECTION_POOL_LEAK_82f2f4aa.md")
    assert t2 == "CONNECTION_POOL_LEAK"
    assert s2 == fr.friction_signature(friction_type="CONNECTION_POOL_LEAK")


# --- ledger + anti-loop ----------------------------------------------------


def test_record_and_load(tmp_path):
    d = str(tmp_path)
    fr.record_attempt(d, friction_signature="sig_a", friction_type="T",
                      resolution_fingerprint="fix_1", state=fr.RECURRED_FAILED,
                      now_iso="2026-06-05T00:00:00+00:00")
    rows = fr.load_attempts(d)
    assert len(rows) == 1 and rows[0]["state"] == fr.RECURRED_FAILED


def test_failed_fingerprints_anti_loop(tmp_path):
    d = str(tmp_path)
    fr.record_attempt(d, friction_signature="sig_a", friction_type="T",
                      resolution_fingerprint="fix_bad", state=fr.RECURRED_FAILED,
                      now_iso="2026-06-05T00:00:00+00:00")
    fr.record_attempt(d, friction_signature="sig_a", friction_type="T",
                      resolution_fingerprint="fix_ok", state=fr.RESOLVED,
                      now_iso="2026-06-05T01:00:00+00:00")
    failed = fr.failed_fingerprints(fr.load_attempts(d), "sig_a")
    assert failed == {"fix_bad"}       # only the failed one; resolved excluded


def test_reserve_in_flight_one_per_signature(tmp_path):
    d = str(tmp_path)
    assert fr.reserve_in_flight(d, friction_signature="sig_a",
                                friction_type="T",
                                now_iso="2026-06-05T00:00:00+00:00") is True
    # second reservation for the same signature is refused while open
    assert fr.reserve_in_flight(d, friction_signature="sig_a",
                                friction_type="T",
                                now_iso="2026-06-05T00:01:00+00:00") is False


# --- eligibility gate ------------------------------------------------------


def _now():
    return "2026-06-05T12:00:00+00:00"


def test_should_respond_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("KERNOS_FRICTION_RESPONSE", raising=False)
    ok, why = fr.should_respond(str(tmp_path), friction_signature="sig_a",
                                source="detector", now_iso=_now())
    assert ok is False and why == "disabled"


def test_should_respond_self_friction_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_FRICTION_RESPONSE", "1")
    ok, why = fr.should_respond(str(tmp_path), friction_signature="sig_a",
                                source="improve_kernos", now_iso=_now())
    assert ok is False and why == "self_friction_source"


def test_should_respond_eligible_then_in_flight(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_FRICTION_RESPONSE", "1")
    d = str(tmp_path)
    ok, why = fr.should_respond(d, friction_signature="sig_a",
                                source="detector", now_iso=_now())
    assert ok is True and why == "eligible"
    fr.reserve_in_flight(d, friction_signature="sig_a", friction_type="T",
                         now_iso=_now())
    ok2, why2 = fr.should_respond(d, friction_signature="sig_a",
                                  source="detector", now_iso=_now())
    assert ok2 is False and why2 == "already_in_flight"


def test_should_respond_anti_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_FRICTION_RESPONSE", "1")
    d = str(tmp_path)
    fr.record_attempt(d, friction_signature="sig_a", friction_type="T",
                      resolution_fingerprint="fix_bad", state=fr.RECURRED_FAILED,
                      now_iso="2026-06-04T00:00:00+00:00")
    ok, why = fr.should_respond(d, friction_signature="sig_a", source="detector",
                                now_iso=_now(), candidate_fingerprint="fix_bad")
    assert ok is False and why == "resolution_already_failed"


def test_should_respond_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_FRICTION_RESPONSE", "1")
    d = str(tmp_path)
    fr.record_attempt(d, friction_signature="sig_a", friction_type="T",
                      resolution_fingerprint="", state=fr.ATTEMPTED,
                      now_iso="2026-06-05T11:00:00+00:00")  # 1h ago < 6h
    ok, why = fr.should_respond(d, friction_signature="sig_a", source="detector",
                                now_iso=_now())
    assert ok is False and why == "within_cooldown"


def test_should_respond_daily_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_FRICTION_RESPONSE", "1")
    d = str(tmp_path)
    for i in range(fr.MAX_RESPONSES_PER_DAY):
        fr.record_attempt(d, friction_signature=f"sig_{i}", friction_type="T",
                          resolution_fingerprint="", state=fr.IN_FLIGHT,
                          now_iso=f"2026-06-05T0{i}:00:00+00:00")
    ok, why = fr.should_respond(d, friction_signature="sig_new",
                                source="detector", now_iso=_now())
    assert ok is False and why == "daily_budget_reached"


# --- verification states ---------------------------------------------------


def test_judge_recurred_is_failed():
    assert fr.judge_resolution(
        deployed_iso="2026-06-05T00:00:00+00:00",
        recurred_iso="2026-06-05T02:00:00+00:00",
        now_iso="2026-06-05T10:00:00+00:00", had_detector_opportunity=True,
    ) == fr.RECURRED_FAILED


def test_judge_pending_within_window():
    assert fr.judge_resolution(
        deployed_iso="2026-06-05T09:00:00+00:00",
        now_iso="2026-06-05T10:00:00+00:00", had_detector_opportunity=True,
    ) == fr.PENDING_VERIFICATION


def test_judge_unknown_when_idle():
    # window elapsed but no detector opportunity -> NOT resolved
    assert fr.judge_resolution(
        deployed_iso="2026-06-05T00:00:00+00:00",
        now_iso="2026-06-05T10:00:00+00:00", had_detector_opportunity=False,
    ) == fr.UNKNOWN_NO_OBS


def test_judge_resolved():
    assert fr.judge_resolution(
        deployed_iso="2026-06-05T00:00:00+00:00",
        now_iso="2026-06-05T10:00:00+00:00", had_detector_opportunity=True,
    ) == fr.RESOLVED


# --- archive by signature --------------------------------------------------


def test_archive_only_matching_signature(tmp_path):
    d = str(tmp_path)
    fdir = tmp_path / "diagnostics" / "friction"
    fdir.mkdir(parents=True)
    (fdir / "2026-06-01T07-51-41_CONNECTION_POOL_LEAK_82f2f4aa.md").write_text("x")
    (fdir / "2026-06-01T08-21-41_CONNECTION_POOL_LEAK_0a84419a.md").write_text("x")
    (fdir / "FRICTION_20260603_222930_c4184cbc_ACPX_TIMEOUT_CLAUDE_CODE.md").write_text("y")
    sig = fr.friction_signature(friction_type="CONNECTION_POOL_LEAK")
    n = fr.archive_resolved_signature(d, friction_signature=sig,
                                      now_iso="2026-06-05T00:00:00+00:00",
                                      ledger_ref="r1")
    assert n == 2                                   # only the pool-leak pair
    assert (fdir / "FRICTION_20260603_222930_c4184cbc_ACPX_TIMEOUT_CLAUDE_CODE.md").exists()
    resolved = tmp_path / "diagnostics" / "friction_resolved"
    assert (resolved / "_manifest.jsonl").exists()  # manifest written
    assert len(list(resolved.glob("*CONNECTION_POOL_LEAK*.md"))) == 2


# --- orchestrator + verification (seam-injected) ---------------------------


def _seed_friction(tmp_path, fname, body="x"):
    fdir = tmp_path / "diagnostics" / "friction"
    fdir.mkdir(parents=True, exist_ok=True)
    (fdir / fname).write_text(body)
    return fdir / fname


@pytest.mark.asyncio
async def test_respond_once_surfaces_top_signature(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_FRICTION_RESPONSE", "1")
    d = str(tmp_path)
    # 2 of one signature (recurring) + 1 of another
    _seed_friction(tmp_path, "2026-06-01T07-51-41_CONNECTION_POOL_LEAK_82f2f4aa.md")
    _seed_friction(tmp_path, "2026-06-01T08-21-41_CONNECTION_POOL_LEAK_0a84419a.md")
    _seed_friction(tmp_path, "2026-06-02T00-25-01_INTEGRATION_NO_TOOL_USE_cfdb47e4.md")

    surfaced = []
    async def _diag(sig, ftype, body):
        return {"cause": f"root cause of {ftype}", "touched": ["kernos/x.py"],
                "proposed_fix": "do the thing"}
    async def _surface(sig, ftype, diag):
        surfaced.append((ftype, diag["proposed_fix"]))

    res = await fr.respond_once(d, now_iso="2026-06-05T12:00:00+00:00",
                                diagnose_fn=_diag, surface_fn=_surface)
    assert res["outcome"] == "surfaced"
    assert res["type"] == "CONNECTION_POOL_LEAK"   # recurring one first
    assert surfaced and surfaced[0][0] == "CONNECTION_POOL_LEAK"
    # recorded pending; a second call is blocked by in-flight/cooldown
    res2 = await fr.respond_once(d, now_iso="2026-06-05T12:01:00+00:00",
                                 diagnose_fn=_diag, surface_fn=_surface)
    assert res2["outcome"] in ("surfaced", "nothing_eligible")
    if res2["outcome"] == "surfaced":
        assert res2["type"] != "CONNECTION_POOL_LEAK"   # moved on, not looping


@pytest.mark.asyncio
async def test_respond_once_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("KERNOS_FRICTION_RESPONSE", raising=False)
    async def _d(*a): return {}
    async def _s(*a): pass
    res = await fr.respond_once(str(tmp_path), now_iso="2026-06-05T12:00:00+00:00",
                                diagnose_fn=_d, surface_fn=_s)
    assert res["outcome"] == "disabled"


def test_verify_archives_quiet_resolved(tmp_path):
    d = str(tmp_path)
    f = _seed_friction(tmp_path, "2026-06-01T07-51-41_CONNECTION_POOL_LEAK_82f2f4aa.md")
    sig = fr.friction_signature(friction_type="CONNECTION_POOL_LEAK")
    # mark pending 25h ago (> 24h window), no new reports since
    import os as _os, time as _time
    old = _time.time() - 100 * 3600
    _os.utime(f, (old, old))
    fr.record_attempt(d, friction_signature=sig, friction_type="CONNECTION_POOL_LEAK",
                      resolution_fingerprint="fix_1", state=fr.PENDING_VERIFICATION,
                      now_iso="2026-06-04T00:00:00+00:00")
    # opportunity: a DIFFERENT-signature report lands after pending, proving
    # the detectors were live (so the quiet pool-leak is genuinely resolved)
    _seed_friction(tmp_path, "2026-06-05T11-00-00_INTEGRATION_NO_TOOL_USE_abcdef12.md")
    out = fr.verify_and_archive(d, now_iso="2026-06-05T12:00:00+00:00")
    assert sig in out["resolved"]
    # reports archived out of the active folder
    assert not list((tmp_path / "diagnostics" / "friction").glob("*CONNECTION_POOL_LEAK*"))


def test_verify_marks_recurred_failed(tmp_path):
    d = str(tmp_path)
    sig = fr.friction_signature(friction_type="CONNECTION_POOL_LEAK")
    fr.record_attempt(d, friction_signature=sig, friction_type="CONNECTION_POOL_LEAK",
                      resolution_fingerprint="fix_1", state=fr.PENDING_VERIFICATION,
                      now_iso="2026-06-04T00:00:00+00:00")
    # a NEW report lands after the pending timestamp (recurred)
    _seed_friction(tmp_path, "2026-06-05T07-51-41_CONNECTION_POOL_LEAK_99999999.md")
    out = fr.verify_and_archive(d, now_iso="2026-06-05T12:00:00+00:00")
    assert sig in out["recurred"]
    assert "fix_1" in fr.failed_fingerprints(fr.load_attempts(d), sig)


# --- Codex final-review folds ----------------------------------------------


def test_idle_with_no_opportunity_is_unknown_not_resolved(tmp_path):
    """Quiet but NO friction produced after pending ⇒ unknown, never resolved
    (Codex High-2: idle is not proof)."""
    d = str(tmp_path)
    (tmp_path / "diagnostics" / "friction").mkdir(parents=True)
    sig = fr.friction_signature(friction_type="CONNECTION_POOL_LEAK")
    fr.record_attempt(d, friction_signature=sig, friction_type="CONNECTION_POOL_LEAK",
                      resolution_fingerprint="fix_1", state=fr.PENDING_VERIFICATION,
                      now_iso="2026-06-04T00:00:00+00:00")
    out = fr.verify_and_archive(d, now_iso="2026-06-05T12:00:00+00:00")  # 36h, no activity
    assert sig not in out["resolved"]
    assert fr._latest_state(fr.load_attempts(d), sig) == fr.UNKNOWN_NO_OBS


def test_source_self_friction_denied_by_content(tmp_path, monkeypatch):
    """A report whose context implicates our own machinery is self-friction
    (Codex High-3: the denylist must actually fire)."""
    assert fr.source_of_report("...spec: Kernos, run improve_kernos ...") == "self"
    assert fr.source_of_report("plain detector evidence") == "detector"


def test_signature_from_report_folds_recommendation(tmp_path):
    """Two same-type reports with DIFFERENT recommendations get DIFFERENT
    signatures (Codex High-4: don't collapse a coarse type)."""
    name = "2026-06-02T00-25-01_INTEGRATION_NO_TOOL_USE_cfdb47e4.md"
    _t1, s1 = fr.signature_from_report(name, "## Recommendation: ENFORCE_A\nx")
    _t2, s2 = fr.signature_from_report(name, "## Recommendation: ENFORCE_B\nx")
    assert s1 != s2
    # same recommendation ⇒ same signature
    _t3, s3 = fr.signature_from_report(name, "## Recommendation: ENFORCE_A\ny")
    assert s1 == s3


# --- conversational natural-yes binding (§3A-i) ----------------------------


def test_is_affirmative_conservative():
    for yes in ["yes", "Yes!", "yeah", "go ahead", "do it", "yes please",
                "sure", "ok", "Approved.", "go for it"]:
        assert fr.is_affirmative(yes) is True, yes
    for no in ["no", "not now", "don't", "stop", "wait", "maybe later",
               "hold off", "", "can you explain what that means first",
               "yes but only the first one and not the second one please"]:
        assert fr.is_affirmative(no) is False, no


def _pending(approval_id="ap1", user="u1", space="s1", ask_msg="m1"):
    return {"approval_id": approval_id, "user_id": user, "space_id": space,
            "ask_message_id": ask_msg}


def test_yes_authorizes_single_pending_same_user_space():
    ap, why = fr.authorize_natural_yes(
        [_pending()], user_id="u1", space_id="s1", in_reply_to="m1", text="yes")
    assert ap == "ap1" and why == "authorized"


def test_yes_with_no_reply_thread_still_binds_single_pending():
    ap, why = fr.authorize_natural_yes(
        [_pending()], user_id="u1", space_id="s1", in_reply_to="", text="go ahead")
    assert ap == "ap1" and why == "authorized"


def test_multiple_pending_in_space_noops():
    ap, why = fr.authorize_natural_yes(
        [_pending("ap1", ask_msg="m1"), _pending("ap2", ask_msg="m2")],
        user_id="u1", space_id="s1", in_reply_to="", text="yes")
    assert ap is None and why == "multiple_pending"


def test_different_user_noops():
    ap, why = fr.authorize_natural_yes(
        [_pending(user="owner")], user_id="someone_else", space_id="s1",
        in_reply_to="m1", text="yes")
    assert ap is None and why == "different_user"


def test_reply_to_other_message_noops():
    ap, why = fr.authorize_natural_yes(
        [_pending(ask_msg="m1")], user_id="u1", space_id="s1",
        in_reply_to="some_other_msg", text="yes")
    assert ap is None and why == "reply_to_other"


def test_non_affirmative_noops():
    ap, why = fr.authorize_natural_yes(
        [_pending()], user_id="u1", space_id="s1", in_reply_to="m1",
        text="not yet, explain it first")
    assert ap is None and why == "not_affirmative"


def test_pending_in_other_space_not_counted():
    ap, why = fr.authorize_natural_yes(
        [_pending(space="other_space")], user_id="u1", space_id="s1",
        in_reply_to="", text="yes")
    assert ap is None and why == "no_pending"
