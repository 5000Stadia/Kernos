"""Deterministic detector identity, through the report/ledger boundary.

All reports are synthetic and description generation is mocked; no model or
real diagnostics are read. Historical keys deliberately remain unchanged.
"""
import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kernos.kernel.friction import FrictionObserver
from kernos.kernel import friction_response as fr
from tests._logical_timeline import LogicalTimeline, verify_in_domain


def _unused_tool(observer, message="what preferences", tools=None):
    return observer._check_tool_not_used(
        [], tools or {"inspect_state"}, message, "Synthetic answer",
        {"user_message": message, "space": "synthetic-space"},
    )


async def _write_reports(observer, signals, recommendations):
    observer._reasoning = object()
    observer._generate_description = AsyncMock(side_effect=[
        ("Synthetic description", rec) for rec in recommendations
    ])
    directory = Path(observer._data_dir) / "diagnostics" / "friction"
    paths = []
    for signal in signals:
        before = set(directory.glob("*.md"))
        await observer._write_report(signal, "synthetic-instance")
        paths.append((set(directory.glob("*.md")) - before).pop())
    return paths


def _signature(path):
    return fr.signature_from_report(path.name, path.read_text())[1]


@pytest.mark.asyncio
async def test_writer_identity_ignores_recommendation_and_incidental_context(tmp_path):
    observer = FrictionObserver(data_dir=str(tmp_path))
    paths = await _write_reports(observer, [
        _unused_tool(observer),
        _unused_tool(observer, "what settings"),
    ], ["SIMPLIFY", "ADD"])
    assert "## Recommendation: SIMPLIFY" in paths[0].read_text()
    assert "## Recommendation: ADD" in paths[1].read_text()
    assert _signature(paths[0]) == _signature(paths[1])
    assert len(fr.list_open_signatures(str(tmp_path))) == 1
    assert fr.list_open_signatures(str(tmp_path))[0]["count"] == 2


@pytest.mark.asyncio
async def test_same_type_distinguishes_detector_branches_and_resources(tmp_path):
    observer = FrictionObserver(data_dir=str(tmp_path))
    state, trigger = await _write_reports(observer, [
        _unused_tool(observer),
        _unused_tool(observer, "what automations", {"manage_schedule"}),
    ], ["SIMPLIFY", "SIMPLIFY"])
    assert fr.signature_of_filename(state.name)[0] == fr.signature_of_filename(trigger.name)[0]
    assert _signature(state) != _signature(trigger)

    def request_signal(tool):
        return observer._check_request_for_surfaced([
            {"name": "request_tool", "input": {"capability_name": tool}},
        ], {tool}, {})

    tools = await _write_reports(observer, [
        request_signal("alpha_tool"), request_signal("beta_tool"),
    ], ["ADD", "ADD"])
    assert _signature(tools[0]) != _signature(tools[1])


@pytest.mark.asyncio
async def test_changed_recommendation_obeys_persisted_reservation_and_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_FRICTION_RESPONSE", "1")
    observer = FrictionObserver(data_dir=str(tmp_path))
    first, second = await _write_reports(observer, [
        _unused_tool(observer), _unused_tool(observer),
    ], ["SIMPLIFY", "STRUCTURAL_ENFORCE"])
    first_sig, second_sig = _signature(first), _signature(second)
    now = "2026-10-29T12:00:00+00:00"
    assert fr.reserve_in_flight(str(tmp_path), friction_signature=first_sig,
                               friction_type="TOOL_AVAILABLE_BUT_NOT_USED", now_iso=now)
    # Reload the consumer: suppression must come from durable state.
    importlib.reload(fr)
    assert fr.should_respond(str(tmp_path), friction_signature=second_sig,
                             source="detector", now_iso="2026-10-29T12:01:00+00:00") == (
                                 False, "already_in_flight")
    fr.record_attempt(str(tmp_path), friction_signature=first_sig,
                      friction_type="TOOL_AVAILABLE_BUT_NOT_USED",
                      resolution_fingerprint="synthetic-fix", state=fr.ATTEMPTED,
                      now_iso=now)
    importlib.reload(fr)
    assert fr.should_respond(str(tmp_path), friction_signature=second_sig,
                             source="detector", now_iso="2026-10-29T12:01:00+00:00") == (
                                 False, "within_cooldown")


@pytest.mark.asyncio
async def test_recurrence_and_archive_share_detector_identity(tmp_path):
    observer = FrictionObserver(data_dir=str(tmp_path))
    old, repeated = await _write_reports(observer, [
        _unused_tool(observer), _unused_tool(observer),
    ], ["SIMPLIFY", "ADD"])
    timeline = LogicalTimeline.from_iso("2026-10-29T12:00:00+00:00")
    timeline.stamp(old, hours=-1)
    timeline.stamp(repeated, hours=1)
    signature = _signature(old)
    fr.record_attempt(str(tmp_path), friction_signature=signature,
                      friction_type="TOOL_AVAILABLE_BUT_NOT_USED",
                      resolution_fingerprint="synthetic-fix", state=fr.PENDING_VERIFICATION,
                      now_iso=timeline.iso())
    verify_in_domain(timeline, str(tmp_path), now_iso=timeline.iso(hours=7))
    assert fr.load_attempts(str(tmp_path))[-1]["state"] == fr.RECURRED_FAILED
    assert old.exists() and repeated.exists()
    [unrelated] = await _write_reports(observer, [
        _unused_tool(observer, "what automations", {"manage_schedule"}),
    ], ["SIMPLIFY"])
    assert fr.archive_resolved_signature(
        str(tmp_path), friction_signature=signature,
        now_iso=timeline.iso(hours=8), ledger_ref="synthetic-ledger-row",
    ) == 2
    assert unrelated.exists()
    assert not old.exists() and not repeated.exists()


@pytest.mark.asyncio
async def test_respond_once_diagnoses_once_when_recommendation_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_FRICTION_RESPONSE", "1")
    observer = FrictionObserver(data_dir=str(tmp_path))
    await _write_reports(observer, [_unused_tool(observer)], ["SIMPLIFY"])
    diagnose = AsyncMock(return_value={"cause": "synthetic cause", "touched": ["synthetic.py"]})
    surface = AsyncMock()
    result = await fr.respond_once(str(tmp_path), now_iso="2026-10-29T12:00:00+00:00",
                                   diagnose_fn=diagnose, surface_fn=surface)
    assert result["outcome"] == "surfaced"
    await _write_reports(observer, [_unused_tool(observer)], ["ADD"])
    result = await fr.respond_once(str(tmp_path), now_iso="2026-10-29T12:01:00+00:00",
                                   diagnose_fn=diagnose, surface_fn=surface)
    assert result["outcome"] == "nothing_eligible"
    assert diagnose.await_count == surface.await_count == 1


@pytest.mark.asyncio
async def test_unstructured_producers_keep_legacy_identity(tmp_path):
    observer = FrictionObserver(data_dir=str(tmp_path))
    error = observer._check_provider_errors(["synthetic request 123 failed"] * 2, {})
    deferred = observer._check_deferred_capability(
        "Someday I'll need a tool for synthetic invoices", {})
    paths = await _write_reports(observer, [error, deferred], ["ADD", "SIMPLIFY"])
    for path, recommendation in zip(paths, ["ADD", "SIMPLIFY"]):
        assert "Problem-Identity:" not in path.read_text()
        ftype, sig = fr.signature_from_report(path.name, path.read_text())
        assert sig == fr.friction_signature(friction_type=ftype, code=recommendation)


def _metadata_body(raw):
    return ("# Friction Report: TOOL_AVAILABLE_BUT_NOT_USED\n"
            "Generated: 2026-10-29T12:00:00+00:00\nClass: error\n"
            f"Problem-Identity: {raw}\n\n## Recommendation: ADD\n")


@pytest.mark.parametrize("raw", [
    "not-json", "[]", "null",
    '{"version": 2, "site": "state", "resource": "inspect_state"}',
    '{"version": true, "site": "state", "resource": "inspect_state"}',
    '{"version": 1, "site": "state"}',
    '{"version": 1, "site": "", "resource": "inspect_state"}',
    '{"version": 1, "site": "state", "resource": []}',
    json.dumps({"version": 1, "site": "state", "resource": "x" * 2100}),
])
def test_malformed_or_unknown_metadata_preserves_legacy_key(raw):
    name = "FRICTION_20261029_120000_TOOL_AVAILABLE_BUT_NOT_USED_aaaaaaaa.md"
    assert fr.signature_from_report(name, _metadata_body(raw))[1] == fr.friction_signature(
        friction_type="TOOL_AVAILABLE_BUT_NOT_USED", code="ADD")


def test_identity_like_text_in_evidence_is_not_writer_metadata():
    name = "FRICTION_20261029_120000_TOOL_AVAILABLE_BUT_NOT_USED_aaaaaaaa.md"
    identity = json.dumps({"version": 1, "site": "state", "resource": "inspect_state"})
    body = ("# Friction Report: TOOL_AVAILABLE_BUT_NOT_USED\n"
            "Generated: 2026-10-29T12:00:00+00:00\nClass: error\n\n"
            "## Recommendation: ADD\n\n## Evidence\n"
            + _metadata_body(identity))
    assert fr.signature_from_report(name, body)[1] == fr.friction_signature(
        friction_type="TOOL_AVAILABLE_BUT_NOT_USED", code="ADD")


def test_legacy_keys_are_unchanged_and_not_aliased():
    name = "FRICTION_20261029_120000_TOOL_AVAILABLE_BUT_NOT_USED_aaaaaaaa.md"
    assert fr.signature_from_report(name, "")[1] == fr.friction_signature(
        friction_type="TOOL_AVAILABLE_BUT_NOT_USED")
    assert fr.signature_from_report(name, "## Recommendation: SIMPLIFY\n")[1] == fr.friction_signature(
        friction_type="TOOL_AVAILABLE_BUT_NOT_USED", code="SIMPLIFY")
    identity = json.dumps({"version": 1, "site": "state", "resource": "inspect_state"})
    assert fr.signature_from_report(name, _metadata_body(identity))[1] != fr.friction_signature(
        friction_type="TOOL_AVAILABLE_BUT_NOT_USED", code="ADD")
