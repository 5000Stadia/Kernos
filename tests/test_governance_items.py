"""SELF-REVIEW-SURFACING-INTEGRITY-V1 — durable human-gated governance items.

The invariant under test: a human-gated finding must not be routed to a
one-shot ephemeral whisper, because that is indistinguishable from a finding
that was never raised. These assert the durable queue behind the human gate —
open, upsert, enumerate, close-on-condition, shadow-archive, reopen — plus the
fail-closed classification boundary.
"""
import json

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


def _archives(tmp_path) -> list:
    return list((tmp_path / "diagnostics" / "friction_resolved").glob(
        "GOVERNANCE_*.md"))


def _manifest_rows(tmp_path) -> list:
    p = tmp_path / "diagnostics" / "friction_resolved" / "_manifest.jsonl"
    if not p.is_file():
        return []
    return [ln for ln in p.read_text().splitlines() if ln.strip()]


def test_archive_write_failure_records_no_closure(tmp_path, monkeypatch):
    """A manifest row must never outlive a failed archive write.

    Manifest-first would declare the item closed while it is still open, then
    write a SECOND row on a successful retry — one archive, two closures.
    """
    import shutil as _sh
    d = str(tmp_path)
    _upsert(d, ["a.py"])

    monkeypatch.setattr(_sh, "copy2", lambda *a, **k: (_ for _ in ()).throw(
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


def _break_manifest(monkeypatch):
    """Audit commit fails outright — nothing lands."""
    monkeypatch.setattr(fr, "_write_manifest", lambda *a, **k: False)


def test_manifest_failure_leaves_item_open_and_unaudited(tmp_path, monkeypatch):
    """An append that raises is completion-AMBIGUOUS, so the archive stays.

    Deleting it would leave a possibly-valid audit row pointing at nothing,
    and the next retry — seeing RECORDED — would retire the authoritative
    source and destroy the last copy. What must hold is that the item stays
    OPEN and no closure is recorded, then a retry reconciles to exactly one.
    """
    d = str(tmp_path)
    _upsert(d, ["a.py"])
    _break_manifest(monkeypatch)

    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T00:00:00+00:00", resolving_condition="cleared") is False
    monkeypatch.undo()

    assert len(fr.open_governance_items(d)) == 1, "item must stay open"
    assert _manifest_rows(tmp_path) == [], "no closure may be recorded"

    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T01:00:00+00:00", resolving_condition="cleared")
    assert fr.open_governance_items(d) == []
    assert len(_archives(tmp_path)) == 1
    assert len(_manifest_rows(tmp_path)) == 1


def test_cleanup_failure_still_cannot_strand_the_finding(tmp_path, monkeypatch):
    """The branch that used to lose the item entirely.

    Two-stage failure: the archive is written, the audit fails, AND the
    cleanup of the un-audited copy also fails. Under a move-then-undo design
    the source was already gone, so the item vanished from every queue — no
    reader scans friction_resolved, so nothing could ever retry it.

    Copy-then-commit-then-unlink makes this branch harmless: the source is
    untouched until both effects are durable, so the worst case is a stray
    archive file, never a lost human-gated finding.
    """
    d = str(tmp_path)
    _upsert(d, ["a.py"])
    _break_manifest(monkeypatch)
    monkeypatch.setattr(fr.Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(
        OSError("cannot remove")))

    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T00:00:00+00:00", resolving_condition="cleared") is False
    monkeypatch.undo()

    # THE invariant: the finding is still queued and still retryable.
    assert len(fr.open_governance_items(d)) == 1
    assert _manifest_rows(tmp_path) == []

    # a later healthy scan RESUMES the same transaction: it reconciles the
    # orphaned archive instead of writing a second one.
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T01:00:00+00:00", resolving_condition="cleared")
    assert fr.open_governance_items(d) == []
    assert len(_manifest_rows(tmp_path)) == 1
    assert len(_archives(tmp_path)) == 1, (
        "retry must reuse the orphaned archive, not create a second one")


def test_source_unlink_failure_leaves_item_retryable_not_lost(tmp_path, monkeypatch):
    """Archive + audit committed but the source could not be retired: the item
    is still listed open, so the next scan re-closes it. Never stranded."""
    d = str(tmp_path)
    _upsert(d, ["a.py"])
    monkeypatch.setattr(fr.Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(
        OSError("cannot remove")))

    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T00:00:00+00:00", resolving_condition="cleared") is False
    monkeypatch.undo()

    assert len(fr.open_governance_items(d)) == 1     # visible, retryable
    assert len(_manifest_rows(tmp_path)) == 1        # audit is durable
    assert len(_archives(tmp_path)) == 1

    # The next healthy scan must RESUME at phase three — retire the source
    # only. Restarting would append a second closure row and a second archive
    # for a single opening, which is what made the previous design non-idempotent.
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T02:00:00+00:00", resolving_condition="cleared")
    assert fr.open_governance_items(d) == []
    assert len(_manifest_rows(tmp_path)) == 1, "one opening → exactly one closure row"
    assert len(_archives(tmp_path)) == 1, "one opening → exactly one archive"


def _read_item(tmp_path) -> str:
    p = (tmp_path / "diagnostics" / "friction"
         / fr.governance_filename(smr.COVERAGE_GAP_SIGNATURE))
    return p.read_text() if p.is_file() else ""


def _archive_text(tmp_path) -> str:
    return "\n".join(p.read_text() for p in _archives(tmp_path))


def test_unreadable_manifest_fails_closed_and_changes_nothing(tmp_path, monkeypatch):
    """A read error is not evidence the audit is missing.

    Collapsing UNREADABLE into "absent" let a retry append a second row for one
    transaction. Since the open source is still authoritative, the safe move is
    to abort and retry later.
    """
    d = str(tmp_path)
    _upsert(d, ["a.py"])
    # phase two committed, source retirement failed
    monkeypatch.setattr(fr.Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(
        OSError("cannot remove")))
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T00:00:00+00:00", resolving_condition="cleared") is False
    monkeypatch.undo()
    before = (len(_archives(tmp_path)), len(_manifest_rows(tmp_path)),
              len(fr.open_governance_items(d)))
    assert before == (1, 1, 1)

    # now make ONLY the manifest read fail; append stays healthy
    real_read = fr.Path.read_text

    def _boom(self, *a, **k):
        if self.name == "_manifest.jsonl":
            raise OSError("unreadable")
        return real_read(self, *a, **k)

    monkeypatch.setattr(fr.Path, "read_text", _boom)
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T01:00:00+00:00", resolving_condition="cleared") is False
    monkeypatch.undo()

    assert (len(_archives(tmp_path)), len(_manifest_rows(tmp_path)),
            len(fr.open_governance_items(d))) == before, \
        "an unreadable audit must change nothing at all"


def test_partial_copy_is_never_blessed_by_a_later_retry(tmp_path, monkeypatch):
    """A crashed copy must not leave a target a retry will trust."""
    import shutil as _sh
    d = str(tmp_path)
    _upsert(d, ["a.py"])
    real_copy = _sh.copy2

    def _partial(s, t, *a, **k):
        fr.Path(t).write_text("PARTIAL")
        raise OSError("crashed mid-copy")

    monkeypatch.setattr(_sh, "copy2", _partial)
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T00:00:00+00:00", resolving_condition="cleared") is False
    monkeypatch.undo()

    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T01:00:00+00:00", resolving_condition="cleared")
    assert len(_archives(tmp_path)) == 1
    text = _archive_text(tmp_path)
    assert "PARTIAL" not in text, "retry blessed a truncated archive"
    assert "a.py" in text, "archive must match the authoritative source"


def test_orphan_archive_is_refreshed_to_match_final_payload(tmp_path, monkeypatch):
    """An unaudited orphan must not outlive an update to the open item."""
    d = str(tmp_path)
    _upsert(d, ["a.py"])
    _break_manifest(monkeypatch)
    monkeypatch.setattr(fr.Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(
        OSError("cannot remove")))
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T00:00:00+00:00", resolving_condition="cleared") is False
    monkeypatch.undo()
    assert len(_archives(tmp_path)) == 1          # unaudited orphan holding a.py

    _upsert(d, ["b.py"], "2026-08-04T00:30:00+00:00")   # payload superseded
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T01:00:00+00:00", resolving_condition="cleared")

    assert len(_archives(tmp_path)) == 1
    assert len(_manifest_rows(tmp_path)) == 1
    text = _archive_text(tmp_path)
    assert "b.py" in text, "archive content must match the recorded final_payload"
    assert "a.py" not in text
    assert "b.py" in _manifest_rows(tmp_path)[0]


def test_recurrence_during_failed_retirement_gets_its_own_occurrence(tmp_path, monkeypatch):
    """A real recurrence must never be absorbed into a committed transaction."""
    d = str(tmp_path)
    _upsert(d, ["a.py"])
    monkeypatch.setattr(fr.Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(
        OSError("cannot remove")))
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T00:00:00+00:00", resolving_condition="cleared") is False
    monkeypatch.undo()
    first = fr.open_governance_items(d)[0]["occurrence"]

    # the condition recurs before the stale source is retired
    _upsert(d, ["b.py"], "2026-08-04T02:00:00+00:00")
    second = fr.open_governance_items(d)[0]["occurrence"]
    assert second and second != first, "recurrence must start a NEW occurrence"

    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T03:00:00+00:00", resolving_condition="cleared again")

    assert len(_archives(tmp_path)) == 2, "two occurrences → two archives"
    rows = _manifest_rows(tmp_path)
    assert len(rows) == 2, "two occurrences → two closure rows"
    assert any("a.py" in r for r in rows) and any("b.py" in r for r in rows)
    assert {first, second} == {json.loads(r)["governance_txn"] for r in rows}


def test_upsert_defers_when_audit_state_is_unreadable(tmp_path, monkeypatch):
    """P0: UNKNOWN must not be collapsed to ABSENT during upsert.

    Committed audit + failed retirement + unreadable manifest at recurrence:
    treating UNKNOWN as not-audited preserved the CLOSED occurrence id while
    overwriting the payload, so once the manifest was readable the close saw
    RECORDED and only unlinked the source — the recurrence vanished with no
    archive and no audit.
    """
    d = str(tmp_path)
    _upsert(d, ["a.py"])
    monkeypatch.setattr(fr.Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(
        OSError("cannot remove")))
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T00:00:00+00:00", resolving_condition="cleared") is False
    monkeypatch.undo()
    before = _read_item(tmp_path)

    real_read = fr.Path.read_text

    def _boom(self, *a, **k):
        if self.name == "_manifest.jsonl":
            raise OSError("unreadable")
        return real_read(self, *a, **k)

    monkeypatch.setattr(fr.Path, "read_text", _boom)
    assert _upsert(d, ["b.py"], "2026-08-04T02:00:00+00:00") is False
    monkeypatch.undo()

    assert _read_item(tmp_path) == before, "source must not be mutated"
    assert "b.py" not in before

    # once readable, the recurrence gets its OWN occurrence and full history
    assert _upsert(d, ["b.py"], "2026-08-04T03:00:00+00:00")
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T04:00:00+00:00", resolving_condition="cleared again")
    rows = _manifest_rows(tmp_path)
    assert len(rows) == 2 and len(_archives(tmp_path)) == 2
    assert any("b.py" in r for r in rows), "the recurrence must have its own audit"


def test_ambiguous_append_never_destroys_the_last_copy(tmp_path, monkeypatch):
    """P0: a row that lands on disk and THEN raises must not orphan the audit.

    Removing the archive in that branch left a valid row pointing at nothing;
    the retry saw RECORDED, skipped the refresh, and unlinked the source —
    ending at one audit row, zero archives, zero open items.
    """
    d = str(tmp_path)
    _upsert(d, ["a.py"])

    real_write = fr._write_manifest

    def _commits_then_reports_failure(path, rows):
        real_write(path, rows)      # the row IS durable on disk
        return False                # ...but we are told it failed

    monkeypatch.setattr(fr, "_write_manifest", _commits_then_reports_failure)
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T00:00:00+00:00", resolving_condition="cleared") is False
    monkeypatch.undo()

    # the row DID land; the archive must still be there beside it
    assert len(_manifest_rows(tmp_path)) == 1
    assert len(_archives(tmp_path)) == 1, "archive must survive an ambiguous append"

    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T01:00:00+00:00", resolving_condition="cleared")
    assert fr.open_governance_items(d) == []
    assert len(_archives(tmp_path)) == 1, "the last copy must never be destroyed"
    assert len(_manifest_rows(tmp_path)) == 1


def test_recorded_transaction_rebuilds_the_archive_its_row_declares(tmp_path):
    """A manifest row alone is not proof the archive survived — and the row's
    `archive` field is authoritative.

    Rebuilding a *canonical* name while the committed row points elsewhere
    would "recover" into a permanently broken audit link, so recovery must
    restore the declared filename and leave the reference resolvable.
    """
    d = str(tmp_path)
    _upsert(d, ["a.py"])
    dest = tmp_path / "diagnostics" / "friction_resolved"
    dest.mkdir(parents=True, exist_ok=True)
    occ = fr.open_governance_items(d)[0]["occurrence"]
    declared = "GOVERNANCE_custom_name_for_this_txn.md"
    fr._write_manifest(dest / "_manifest.jsonl", [{
        "governance_txn": occ,
        "governance_signature": smr.COVERAGE_GAP_SIGNATURE,
        "archive": declared,
    }])

    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T01:00:00+00:00", resolving_condition="cleared")

    rows = [json.loads(r) for r in _manifest_rows(tmp_path)]
    assert len(rows) == 1, "no duplicate row"
    ref = rows[0]["archive"]
    assert (dest / ref).is_file(), "the row's archive reference must resolve"
    assert ref == declared, "recovery must honour the declared name"
    assert "a.py" in (dest / ref).read_text()
    assert [p.name for p in _archives(tmp_path)] == [declared], \
        "exactly the declared archive — no stray canonical duplicate"


def test_torn_manifest_tail_is_repaired_not_stranded_forever(tmp_path, monkeypatch):
    """A partial audit write must not make closure permanently impossible.

    Under append-only + fail-closed, a half-written JSON row parsed as UNKNOWN
    on every future scan and no state transition could ever repair it — the
    item stayed visible but could never close without manual surgery.
    """
    d = str(tmp_path)
    _upsert(d, ["a.py"])
    dest = tmp_path / "diagnostics" / "friction_resolved"
    dest.mkdir(parents=True, exist_ok=True)
    # a torn tail: the front of a row, cut mid-write
    (dest / "_manifest.jsonl").write_text('{"governance_txn": "abc", "arch')

    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T01:00:00+00:00", resolving_condition="cleared"), \
        "a torn tail must be repairable, not a permanent block"

    rows = _manifest_rows(tmp_path)
    assert len(rows) == 1
    json.loads(rows[0])                       # the surviving row is valid
    assert fr.open_governance_items(d) == []


def test_corrupt_interior_row_fails_closed(tmp_path):
    """The counterweight: corruption of COMMITTED history is not repairable
    and must never be silently reinterpreted as 'no audit exists'."""
    d = str(tmp_path)
    _upsert(d, ["a.py"])
    dest = tmp_path / "diagnostics" / "friction_resolved"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "_manifest.jsonl").write_text(
        'not-json-at-all\n{"governance_txn":"zzz"}\n')

    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T01:00:00+00:00", resolving_condition="cleared") is False
    assert len(fr.open_governance_items(d)) == 1, "item stays open, nothing destroyed"


def test_legacy_recurrence_before_first_close_is_not_absorbed(tmp_path):
    """P0: recurrence over a LEGACY in-flight closure.

    With no Occurrence field, skipping the audit lookup made upsert overwrite
    payload A with B under the SAME legacy txn; close then saw RECORDED and
    merely retired the source, so B had no archive and no audit.
    """
    import hashlib as _h
    d = str(tmp_path)
    opened = "2026-08-01T00:00:00+00:00"
    _upsert(d, ["a.py"], opened)
    path = (tmp_path / "diagnostics" / "friction"
            / fr.governance_filename(smr.COVERAGE_GAP_SIGNATURE))
    path.write_text("\n".join(
        ln for ln in path.read_text().splitlines()
        if not ln.startswith("Occurrence:")) + "\n")

    legacy = _h.sha256(
        f"{smr.COVERAGE_GAP_SIGNATURE}|{opened}".encode()).hexdigest()[:16]
    dest = tmp_path / "diagnostics" / "friction_resolved"
    dest.mkdir(parents=True, exist_ok=True)
    stem = fr.governance_filename(smr.COVERAGE_GAP_SIGNATURE)[:-3]
    (dest / f"{stem}_closed_{legacy}.md").write_text(path.read_text())
    fr._write_manifest(dest / "_manifest.jsonl", [{
        "governance_txn": legacy,
        "governance_signature": smr.COVERAGE_GAP_SIGNATURE,
        "archive": f"{stem}_closed_{legacy}.md",
        "final_payload": ["a.py"],
    }])

    # the condition recurs BEFORE the first post-upgrade close
    assert _upsert(d, ["b.py"], "2026-08-02T00:00:00+00:00")
    occ = fr.open_governance_items(d)[0]["occurrence"]
    assert occ and occ != legacy, "recurrence must not inherit the closed txn"

    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-03T00:00:00+00:00", resolving_condition="cleared again")

    rows = [json.loads(r) for r in _manifest_rows(tmp_path)]
    assert len(rows) == 2, "the recurrence must get its own audit"
    assert {r["governance_txn"] for r in rows} == {legacy, occ}
    assert any("b.py" in str(r.get("final_payload")) for r in rows)
    assert len(_archives(tmp_path)) == 2


def test_legacy_in_flight_item_from_parent_commit_does_not_duplicate(tmp_path):
    """Upgrade state: an item written before occurrence ids were persisted.

    The parent derived the id as sha256(signature|opened_iso). If the new
    fallback appends an empty segment it mints a DIFFERENT id and the close
    writes a second archive and row for the same opening.
    """
    import hashlib as _h
    d = str(tmp_path)
    _upsert(d, ["a.py"], "2026-08-01T00:00:00+00:00")
    path = (tmp_path / "diagnostics" / "friction"
            / fr.governance_filename(smr.COVERAGE_GAP_SIGNATURE))
    # strip the Occurrence line → exactly the parent commit's on-disk format
    path.write_text("\n".join(
        ln for ln in path.read_text().splitlines()
        if not ln.startswith("Occurrence:")) + "\n")
    assert "Occurrence:" not in path.read_text()

    legacy = _h.sha256(
        f"{smr.COVERAGE_GAP_SIGNATURE}|2026-08-01T00:00:00+00:00".encode()
    ).hexdigest()[:16]
    assert fr._new_occurrence_id(
        smr.COVERAGE_GAP_SIGNATURE, "2026-08-01T00:00:00+00:00", "") == legacy

    # legacy archive + audit already committed, retirement pending
    dest = tmp_path / "diagnostics" / "friction_resolved"
    dest.mkdir(parents=True, exist_ok=True)
    stem = fr.governance_filename(smr.COVERAGE_GAP_SIGNATURE)[:-3]
    (dest / f"{stem}_closed_{legacy}.md").write_text(path.read_text())
    (dest / "_manifest.jsonl").write_text(json.dumps({
        "governance_txn": legacy,
        "governance_signature": smr.COVERAGE_GAP_SIGNATURE}) + "\n")

    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T00:00:00+00:00", resolving_condition="cleared")
    assert len(_archives(tmp_path)) == 1, "legacy in-flight item must not duplicate"
    assert len(_manifest_rows(tmp_path)) == 1
    assert fr.open_governance_items(d) == []


def test_repeated_close_of_an_already_closed_item_is_a_noop(tmp_path):
    """Idempotency at the simplest level: closing twice changes nothing."""
    d = str(tmp_path)
    _upsert(d, ["a.py"])
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T00:00:00+00:00", resolving_condition="cleared")
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-04T03:00:00+00:00", resolving_condition="cleared") is False
    assert len(_manifest_rows(tmp_path)) == 1
    assert len(_archives(tmp_path)) == 1


def test_reopening_after_close_gets_its_own_transaction(tmp_path):
    """A genuine recurrence is a NEW occurrence: its own archive and row."""
    d = str(tmp_path)
    _upsert(d, ["a.py"], "2026-08-01T00:00:00+00:00")
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-08-02T00:00:00+00:00", resolving_condition="cleared")

    _upsert(d, ["b.py"], "2026-09-01T00:00:00+00:00")   # recurrence
    assert fr.close_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE,
        now_iso="2026-09-02T00:00:00+00:00", resolving_condition="cleared again")

    assert len(_manifest_rows(tmp_path)) == 2
    assert len(_archives(tmp_path)) == 2, "distinct occurrences must not collide"


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
