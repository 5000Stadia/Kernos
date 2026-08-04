"""Operator-surface rendering for the governance sections in `/dump`.

`/status` is deliberately free of internal identifiers and file paths
(SURFACE-DISCIPLINE-PASS D5), so lane state, the open-governance queue, and the
classification quarantine belong on the operator diagnostic surface.

These call the PRODUCTION renderer (`_render_governance_dump_sections`). An
earlier version of this file reimplemented the rendering in a private helper,
which meant deleting the real section would have left every test green.
"""
import os

import pytest

from kernos.kernel import friction_response as fr
from kernos.kernel import self_maintenance_review as smr
from kernos.kernel.governance_lanes import GOVERNANCE_LANES
from kernos.messages.handler import MessageHandler


def _render(monkeypatch, data_dir: str) -> str:
    monkeypatch.setenv("KERNOS_DATA_DIR", data_dir)
    # Unbound call: the renderer touches no instance state, which is precisely
    # why it is safe to exercise without standing up a whole handler.
    return MessageHandler._render_governance_dump_sections(None)


def test_renders_every_lane_with_key_title_env_and_module(tmp_path, monkeypatch):
    out = _render(monkeypatch, str(tmp_path))
    assert "=== GOVERNANCE LANES ===" in out
    for lane in GOVERNANCE_LANES:
        assert lane.key in out
        assert lane.title in out, "Part F requires the human title, not just the key"
        assert lane.module in out
        for var in lane.env_vars:
            assert var in out


def test_reflects_a_live_flip_not_a_static_echo(tmp_path, monkeypatch):
    def state(text: str) -> str:
        for line in text.splitlines():
            if line.startswith("friction_response"):
                return line.split()[1]
        raise AssertionError("friction_response row missing")

    monkeypatch.delenv("KERNOS_FRICTION_RESPONSE", raising=False)
    off = _render(monkeypatch, str(tmp_path))
    assert state(off) == "OFF"

    monkeypatch.setenv("KERNOS_FRICTION_RESPONSE", "1")
    on = _render(monkeypatch, str(tmp_path))
    assert state(on) == "ON"


def test_open_governance_queue_renders_with_gate_intact(tmp_path, monkeypatch):
    d = str(tmp_path)
    assert fr.upsert_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE, title="Coverage gap",
        condition="modules unowned", payload=["kernos/x.py"],
        now_iso="2026-08-04T00:00:00+00:00")

    out = _render(monkeypatch, d)
    assert "=== OPEN GOVERNANCE ITEMS ===" in out
    assert smr.COVERAGE_GAP_SIGNATURE in out
    assert "human-gated=True" in out, "the gate must survive to the operator surface"
    assert "kernos/x.py" in out


def test_empty_queue_renders_cleanly(tmp_path, monkeypatch):
    out = _render(monkeypatch, str(tmp_path))
    assert "(none open)" in out


def test_quarantined_unknown_class_is_visible_not_only_logged(tmp_path, monkeypatch):
    """AC4 — a fail-closed report is excluded from every automated lane, so if
    it is not listed here it has silently disappeared."""
    d = str(tmp_path)
    fdir = tmp_path / "diagnostics" / "friction"
    fdir.mkdir(parents=True)
    (fdir / "FRICTION_20260804_120000_TYPO_cccccccc.md").write_text(
        "Class: governnace\n\nsomething went wrong\n")

    out = _render(monkeypatch, d)
    assert "=== QUARANTINED REPORTS" in out
    assert "FRICTION_20260804_120000_TYPO_cccccccc.md" in out
    assert "governnace" in out, "the misdeclared class must be shown for triage"


def test_no_quarantine_renders_cleanly(tmp_path, monkeypatch):
    out = _render(monkeypatch, str(tmp_path))
    assert "(none)" in out
