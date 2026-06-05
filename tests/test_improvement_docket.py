"""SELF-MAINTENANCE-REVIEW-V3 Part B — the improvement docket.

Opportunity-class friction notes: captured by the pure observer, skipped by the
reactive Shape B loop and the escalation path, worked by the daily Shape A review.
"""
from pathlib import Path

from kernos.kernel import self_maintenance_review as smr
from kernos.kernel import friction_response as fr
from kernos.kernel.friction import FrictionObserver, FrictionSignal

NOW = "2026-06-05T00:00:00+00:00"

OPP = ("# Friction Report: BETTER_METHOD_ON_RETRY\nGenerated: t\n"
       "Class: opportunity\n\n## Description\n`a` failed, then `b` succeeded — "
       "make b the default.\n\n## Recommendation: SIMPLIFY\n")
ERR = "# Friction Report: EMPTY_RESPONSE\nGenerated: t\n\n## Description\nbad\n"


def _write(d, name, body):
    fdir = Path(d) / "diagnostics" / "friction"
    fdir.mkdir(parents=True, exist_ok=True)
    (fdir / name).write_text(body)


# --- detectors ------------------------------------------------------------

def test_better_method_on_retry_requires_failure_then_different_success():
    o = FrictionObserver(reasoning=None, enabled=True)
    hit = o._check_better_method_on_retry(
        [{"name": "search_web", "success": False},
         {"name": "browse_page", "success": True}], {"user_message": "find it"})
    assert hit and hit.report_class == "opportunity"
    # same-tool retry and all-success do NOT fire
    assert o._check_better_method_on_retry(
        [{"name": "x", "success": False}, {"name": "x", "success": True}], {}) is None
    assert o._check_better_method_on_retry(
        [{"name": "x", "success": True}, {"name": "y", "success": True}], {}) is None


def test_deferred_capability_is_high_precision():
    o = FrictionObserver(reasoning=None, enabled=True)
    assert o._check_deferred_capability(
        "down the road I'll need a tool to track invoices", {}) is not None
    # ordinary future-tense chatter does not fire
    assert o._check_deferred_capability("I'll see you tomorrow", {}) is None
    assert o._check_deferred_capability("eventually it gets dark", {}) is None


# --- class parser + Shape B skip -----------------------------------------

def test_report_class_backcompat():
    assert fr.report_class(ERR) == "error"           # class-less = error
    assert fr.report_class(OPP) == "opportunity"


def test_shape_b_skips_opportunity(tmp_path):
    _write(str(tmp_path), "FRICTION_1_ERR.md", ERR)
    _write(str(tmp_path), "FRICTION_2_OPP.md", OPP)
    sigs = fr.list_open_signatures(str(tmp_path))
    assert len(sigs) == 1                             # only the error report is open
    assert all("opportunity" not in g.get("sample_body", "").lower() for g in sigs)


async def test_observer_opportunity_skips_escalation(tmp_path):
    calls = []

    class _PS:  # presence of a pattern_store is what would trigger escalation
        pass

    o = FrictionObserver(reasoning=None, data_dir=str(tmp_path),
                         enabled=True, pattern_store=_PS())

    async def _spy(*a, **k):
        calls.append(1)
    o._classify_and_record = _spy

    await o._write_report(
        FrictionSignal("BETTER_METHOD_ON_RETRY", "d", [], {"space": ""},
                       report_class="opportunity"), "inst")
    assert calls == []                               # opportunity skips escalation

    await o._write_report(
        FrictionSignal("EMPTY_RESPONSE", "d", [], {"space": ""}), "inst")
    assert calls == [1]                              # error-class still escalates


# --- Shape A consume ------------------------------------------------------

def test_daily_review_folds_opportunities(tmp_path):
    _write(str(tmp_path), "FRICTION_2_OPP.md", OPP)
    opps = smr.open_opportunities(str(tmp_path), NOW)
    assert opps and "succeeded" in opps[0]["desc"]
    healthy = {"slice": "x", "overall_health": "healthy",
               "corrective_findings": [], "evolution_idea": None,
               "opportunities": opps}
    assert smr.has_anything_to_say(healthy)          # healthy slice still surfaces
    assert "improvement opportunities" in smr.to_whisper_text(healthy).lower()
