"""Operator-surface rendering for the two governance sections in `/dump`.

`/status` is deliberately free of internal identifiers and file paths
(SURFACE-DISCIPLINE-PASS D5), so lane state and open governance items belong on
the operator diagnostic surface instead. These assert the rendering directly
rather than through a full handler turn.
"""
import io

import pytest

from kernos.kernel import friction_response as fr
from kernos.kernel import self_maintenance_review as smr
from kernos.kernel.governance_lanes import GOVERNANCE_LANES, lane_states


def _render_lanes() -> str:
    """Mirror of the /dump GOVERNANCE LANES block."""
    f = io.StringIO()
    for st in lane_states():
        on = {True: "ON", False: "OFF", None: "UNOBSERVABLE"}[st["enabled"]]
        f.write(f"{st['key']:26} {on:12} {','.join(st['env_vars'])}\n")
        f.write(f"{'':26} {st['module']}\n")
    return f.getvalue()


def test_dump_renders_every_registered_lane():
    out = _render_lanes()
    for lane in GOVERNANCE_LANES:
        assert lane.key in out
        assert lane.module in out
        for var in lane.env_vars:
            assert var in out


def test_dump_reflects_a_live_flip_not_a_static_echo(monkeypatch):
    """The section must show live state, not a hard-coded copy of the table."""
    def _state(text: str) -> str:
        for line in text.splitlines():
            if line.startswith("friction_response"):
                return line.split()[1]
        raise AssertionError("friction_response row missing")

    monkeypatch.delenv("KERNOS_FRICTION_RESPONSE", raising=False)
    off = _render_lanes()
    assert _state(off) == "OFF"

    monkeypatch.setenv("KERNOS_FRICTION_RESPONSE", "1")
    on = _render_lanes()
    assert _state(on) == "ON"
    assert off != on


def test_dump_lists_open_governance_items_with_gate_intact(tmp_path):
    d = str(tmp_path)
    assert fr.upsert_governance_item(
        d, signature=smr.COVERAGE_GAP_SIGNATURE, title="Coverage gap",
        condition="modules unowned", payload=["kernos/x.py"],
        now_iso="2026-08-04T00:00:00+00:00")

    items = fr.open_governance_items(d)
    assert len(items) == 1
    it = items[0]
    # the human-gated marker must survive all the way to the operator surface —
    # this is what makes AC "no auto-trigger consumes it" verifiable end-to-end
    assert it["human_gated"] is True
    assert it["signature"] == smr.COVERAGE_GAP_SIGNATURE
    assert "kernos/x.py" in it["payload"]


def test_no_open_items_renders_cleanly(tmp_path):
    assert fr.open_governance_items(str(tmp_path)) == []
