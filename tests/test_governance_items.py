"""SELF-REVIEW-SURFACING-INTEGRITY-V1 — durable human-gated governance items.

The invariant under test: a human-gated finding must not be routed to a
one-shot ephemeral whisper, because that is indistinguishable from a finding
that was never raised. These assert the durable queue behind the human gate —
open, upsert, enumerate, close-on-condition, shadow-archive, reopen — plus the
fail-closed classification boundary.
"""
import pytest

from kernos.kernel import friction_response as fr
from kernos.kernel import self_maintenance_review as smr


# --- classification boundary: fails CLOSED -----------------------------------

def test_report_class_matrix():
    assert fr.report_class("no header here") == "error"          # legacy
    assert fr.report_class("Class: error") == "error"
    assert fr.report_class("Class: opportunity") == "opportunity"
    assert fr.report_class("Class: governance") == "governance"


def test_typod_governance_class_is_quarantined_not_escalated():
    """A typo must never BROADEN a human-gated item into an automated lane.

    `error` is not a neutral bucket — it enters reactive Shape B and can reach
    gated automation. So an explicit-but-unrecognized class quarantines.
    """
    assert fr.report_class("Class: governnace") == "unknown"
    assert fr.report_class("Class: whatever") == "unknown"
    # and quarantine means excluded from Shape B, like governance
    assert "unknown" in fr.SHAPE_B_EXCLUDED_CLASSES
    assert "governance" in fr.SHAPE_B_EXCLUDED_CLASSES
    # legacy class-less reports are still ordinary Shape B work
    assert "error" not in fr.SHAPE_B_EXCLUDED_CLASSES


def test_governance_and_unknown_skipped_by_shape_b_inventory(tmp_path):
    d = str(tmp_path)
    fdir = tmp_path / "diagnostics" / "friction"
    fdir.mkdir(parents=True)
    (fdir / "FRICTION_20260804_120000_REAL_ERROR_aaaaaaaa.md").write_text("boom")
    (fdir / "FRICTION_20260804_120001_GOV_bbbbbbbb.md").write_text("Class: governance\n")
    (fdir / "FRICTION_20260804_120002_TYPO_cccccccc.md").write_text("Class: governnace\n")
    sigs = fr.list_open_signatures(d)
    bodies = " ".join(g["sample_body"] for g in sigs)
    assert "boom" in bodies              # the real error is Shape B work
    assert "governance" not in bodies    # human-gated: never reactive
    assert "governnace" not in bodies    # quarantined: never reactive


# --- filename safety ---------------------------------------------------------

def test_filename_never_contains_raw_signature():
    name = fr.governance_filename("self-review:coverage-gap")
    assert ":" not in name and "/" not in name
    assert name.endswith(".md")
    # stable across calls — upsert depends on it
    assert name == fr.governance_filename("self-review:coverage-gap")
    assert name != fr.governance_filename("self-review:other-condition")


# --- durable lifecycle -------------------------------------------------------

def _upsert(d, payload, now="2026-08-04T00:00:00+00:00"):
    return fr.upsert_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE, title="Coverage gap",
        condition="modules unowned", payload=payload, now_iso=now,
    )


def test_upsert_is_idempotent_and_preserves_opened_stamp(tmp_path):
    d = str(tmp_path)
    assert _upsert(d, ["a.py", "b.py"], "2026-08-01T00:00:00+00:00")
    assert _upsert(d, ["a.py"], "2026-08-04T00:00:00+00:00")   # partial repair
    items = fr.open_governance_items(d)
    assert len(items) == 1, "partial repair must UPDATE, not strand + reopen"
    assert items[0]["payload"] == ["a.py"]
    assert items[0]["opened_iso"] == "2026-08-01T00:00:00+00:00"  # preserved
    assert items[0]["last_seen_iso"] == "2026-08-04T00:00:00+00:00"
    assert items[0]["human_gated"] is True


def test_open_items_have_no_ttl(tmp_path):
    """Unlike the opportunity docket (30-day window), governance items persist."""
    import os, time
    d = str(tmp_path)
    _upsert(d, ["a.py"])
    p = tmp_path / "diagnostics" / "friction" / fr.governance_filename(
        smr.COVERAGE_GAP_SIGNATURE)
    old = time.time() - 400 * 86400          # >1 year stale
    os.utime(p, (old, old))
    assert len(fr.open_governance_items(d)) == 1


def test_close_shadow_archives_and_reopens_without_collision(tmp_path):
    d = str(tmp_path)
    _upsert(d, ["a.py"])
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T00:00:00+00:00",
        resolving_condition="unassigned_modules is empty")
    assert fr.open_governance_items(d) == []

    resolved = tmp_path / "diagnostics" / "friction_resolved"
    archived = list(resolved.glob("GOVERNANCE_*.md"))
    assert len(archived) == 1, "closure must shadow-archive, never hard-delete"
    manifest = (resolved / "_manifest.jsonl").read_text()
    assert smr.COVERAGE_GAP_SIGNATURE in manifest
    assert "a.py" in manifest                      # final payload preserved
    assert "resolving_condition" in manifest       # closure audit preserved

    # recurrence reopens cleanly, no collision with the archived name
    _upsert(d, ["c.py"], "2026-09-01T00:00:00+00:00")
    assert len(fr.open_governance_items(d)) == 1
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-09-02T00:00:00+00:00", resolving_condition="cleared again")
    assert len(list(resolved.glob("GOVERNANCE_*.md"))) == 2


def _manifest_rows(tmp_path) -> list:
    p = tmp_path / "diagnostics" / "friction_resolved" / "_manifest.jsonl"
    if not p.is_file():
        return []
    return [ln for ln in p.read_text().splitlines() if ln.strip()]


def test_move_failure_records_no_closure(tmp_path, monkeypatch):
    """A manifest row must never outlive a failed archive move.

    Manifest-first would declare the item closed while it is still open, then
    write a SECOND row on a successful retry — one archive, two closures.
    """
    import shutil as _sh
    d = str(tmp_path)
    _upsert(d, ["a.py"])

    monkeypatch.setattr(_sh, "move", lambda *a, **k: (_ for _ in ()).throw(
        OSError("disk gone")))
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T00:00:00+00:00", resolving_condition="cleared") is False

    assert len(fr.open_governance_items(d)) == 1, "item must stay open"
    assert _manifest_rows(tmp_path) == [], "no closure may be recorded"

    # a later successful retry produces exactly ONE closure, not two
    monkeypatch.undo()
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T00:00:09+00:00", resolving_condition="cleared")
    assert len(_manifest_rows(tmp_path)) == 1
    assert fr.open_governance_items(d) == []


def test_manifest_failure_rolls_back_the_archive(tmp_path, monkeypatch):
    """The other half: an archived file must never exist without its audit."""
    d = str(tmp_path)
    _upsert(d, ["a.py"])

    real_open = fr.Path.open

    def _boom(self, *a, **k):
        if self.name == "_manifest.jsonl":
            raise OSError("audit device full")
        return real_open(self, *a, **k)

    monkeypatch.setattr(fr.Path, "open", _boom)
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T00:00:00+00:00", resolving_condition="cleared") is False
    monkeypatch.undo()

    # compensated: the item is back, and no orphan archive was left behind
    assert len(fr.open_governance_items(d)) == 1
    resolved = tmp_path / "diagnostics" / "friction_resolved"
    assert list(resolved.glob("GOVERNANCE_*.md")) == []


def test_enumeration_does_not_whisper(tmp_path):
    """Reading the recovery queue is not surfacing it."""
    d = str(tmp_path)
    _upsert(d, ["a.py"])
    before = (tmp_path / "diagnostics" / "friction" / fr.governance_filename(
        smr.COVERAGE_GAP_SIGNATURE)).read_text()
    for _ in range(3):
        fr.open_governance_items(d)
    after = (tmp_path / "diagnostics" / "friction" / fr.governance_filename(
        smr.COVERAGE_GAP_SIGNATURE)).read_text()
    assert before == after      # enumeration is side-effect free


# --- the transition that rev 2 would have got wrong --------------------------

@pytest.mark.asyncio
async def test_map_repair_closes_the_item_despite_unchanged_shape(
        tmp_path, monkeypatch):
    """AC6 — the load-bearing test.

    `shape_fingerprint()` hashes the set of module PATHS. Repairing the map
    edits REVIEW_SLICES ownership and changes NO path, so the fingerprint is
    unchanged by exactly the repair that resolves the gap. If lifecycle
    evaluation sat behind that fingerprint the item could never close.
    """
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "1")
    d = str(tmp_path)
    payload = ('```json\n{"overall_health":"healthy","corrective_findings":[],'
               '"evolution_idea":null,"serves_the_whole":true}\n```')

    async def _consult(_p, _s=None): return payload
    async def _ok(_t, _r): pass

    # 1. a gap exists → item opens
    monkeypatch.setattr(smr, "unassigned_modules", lambda *a, **k: ["kernos/x.py"])
    await smr.maybe_run_daily(data_dir=d, now_iso="2026-08-01T00:00:00+00:00",
                              consult_fn=_consult, whisper_fn=_ok)
    items = fr.open_governance_items(d)
    assert len(items) == 1 and items[0]["payload"] == ["kernos/x.py"]
    fp_before = smr.load_state(d)["shape_fingerprint"]

    # 2. the map is repaired — ownership changes, module paths do NOT
    monkeypatch.setattr(smr, "unassigned_modules", lambda *a, **k: [])
    st = smr.load_state(d); st["last_run_iso"] = ""; smr.save_state(d, st)
    await smr.maybe_run_daily(data_dir=d, now_iso="2026-08-02T00:00:00+00:00",
                              consult_fn=_consult, whisper_fn=_ok)

    assert smr.load_state(d)["shape_fingerprint"] == fp_before, \
        "precondition: the repair must NOT change the shape fingerprint"
    assert fr.open_governance_items(d) == [], \
        "item must close on the live condition, not on the shape fingerprint"


@pytest.mark.asyncio
async def test_failed_durable_write_stays_retryable(tmp_path, monkeypatch):
    """AC9 — a landed whisper must never imply the finding was recorded."""
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "1")
    d = str(tmp_path)
    payload = ('```json\n{"overall_health":"healthy","corrective_findings":[],'
               '"evolution_idea":null,"serves_the_whole":true}\n```')

    async def _consult(_p, _s=None): return payload
    async def _ok(_t, _r): pass

    monkeypatch.setattr(smr, "unassigned_modules", lambda *a, **k: ["kernos/x.py"])
    monkeypatch.setattr(fr, "upsert_governance_item", lambda *a, **k: False)
    await smr.maybe_run_daily(data_dir=d, now_iso="2026-08-01T00:00:00+00:00",
                              consult_fn=_consult, whisper_fn=_ok)
    # the whisper succeeded, but persistence did NOT — so persistence state is
    # empty and the item remains eligible for retry on the next scan.
    assert smr.load_state(d)["governance_persisted_fingerprint"] == ""
