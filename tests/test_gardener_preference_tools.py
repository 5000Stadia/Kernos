"""CANVAS-GARDENER-PREFERENCE-CAPTURE Commit 3 — tools + dispatch TTL.

Covers:
  - extract_intent_hook_names against real pattern library files
  - canvas_preference_extract tool dispatch (no-match, non-wired, surface)
  - canvas_preference_confirm tool dispatch (confirm, discard, unknown name)
  - Gate classifications for both preference tools
  - Gardener dispatch calls drop_expired_pending_preferences on events
  - Auto-apply consent modes do NOT apply to preference capture
"""
from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from kernos.cohorts.gardener import (
    PreferenceExtractionResult,
    extract_intent_hook_names,
)
from kernos.kernel.canvas import CanvasService, canvas_dir
from kernos.kernel.gate import DispatchGate
from kernos.kernel.gardener import GardenerService
from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.reasoning import ReasoningService


INSTANCE = "inst_preftools"
OPERATOR = "member:inst_preftools:owner"
LIBRARY = Path(__file__).resolve().parents[1] / "docs" / "workflow-patterns"


class _StubProvider:
    """Minimal provider stub — never actually called."""


class _StubReasoning:
    """Preset payload for consult_preference_extraction.

    Set ``payload`` (dict) before the tool call; next complete_simple
    returns the JSON-encoded payload. ``raises`` preempts and raises.
    """
    def __init__(self) -> None:
        self.payload: dict | None = None
        self.raises: Exception | None = None
        self.calls: list[dict] = []

    async def complete_simple(self, *, system_prompt, user_content, chain,
                               output_schema=None, max_tokens=512):
        self.calls.append({"chain": chain})
        if self.raises:
            raise self.raises
        if self.payload is None:
            return json.dumps({"matched": False, "confidence": "low"})
        return json.dumps(self.payload)


# ---- extract_intent_hook_names against the real pattern library ---------


def test_extract_intent_hooks_on_pattern_01():
    body = (LIBRARY / "01-software-development.md").read_text(encoding="utf-8")
    hooks = extract_intent_hook_names(body)
    for expected in ("manifest-routing", "scope-enforcement",
                      "supersession-required", "ledger-routing"):
        assert expected in hooks


def test_extract_intent_hooks_on_pattern_02():
    body = (LIBRARY / "02-long-form-campaign.md").read_text(encoding="utf-8")
    hooks = extract_intent_hook_names(body)
    for expected in ("canon-routing", "pin-characters"):
        assert expected in hooks


def test_extract_intent_hooks_missing_section_returns_empty():
    assert extract_intent_hook_names("") == []
    assert extract_intent_hook_names("no Member intent hooks section here") == []


# ---- Gate classifications ------------------------------------------------


def test_gate_classifies_both_preference_tools_as_soft_write():
    gate = DispatchGate(
        reasoning_service=None, registry=None, state=None,
        events=None, mcp=None,
    )
    assert gate.classify_tool_effect("canvas_preference_extract", None, {}) == "soft_write"
    assert gate.classify_tool_effect("canvas_preference_confirm", None, {}) == "soft_write"


# ---- Reasoning-handler dispatch for the two tools ------------------------


@pytest.fixture
async def env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member(OPERATOR, "Op", "owner", "")
    stub_reasoning = _StubReasoning()
    svc = CanvasService(instance_db=idb, data_dir=str(tmp_path))
    gardener = GardenerService(
        canvas_service=svc, instance_db=idb, reasoning_service=stub_reasoning,
    )
    # Preload Pattern 01 body so intent-hook extraction has vocabulary.
    gardener.patterns.put(
        "software-development",
        (LIBRARY / "01-software-development.md").read_text(encoding="utf-8"),
        {"pattern": "software-development"},
    )
    gardener.patterns.mark_loaded()

    handler = types.SimpleNamespace()
    handler._instance_db = idb

    def _get_canvas():
        return svc

    def _get_gardener():
        return gardener

    handler._get_canvas_service = _get_canvas
    handler._get_gardener_service = _get_gardener
    handler._get_relational_dispatcher = lambda: None

    reasoning = ReasoningService(provider=_StubProvider(), events=None, mcp=None)
    reasoning._handler = handler
    reasoning._canvas = svc

    yield reasoning, svc, idb, stub_reasoning, tmp_path
    await idb.close()


def _request(instance_id=INSTANCE, member_id=OPERATOR):
    return types.SimpleNamespace(
        instance_id=instance_id, member_id=member_id,
        active_space_id="default", conversation_id="",
        user_timezone="UTC", trace=None,
    )


async def _make_sd_canvas(svc):
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="Test", scope="personal",
    )
    await svc.set_canvas_pattern(
        instance_id=INSTANCE, canvas_id=c.canvas_id, pattern="software-development",
    )
    return c.canvas_id


async def test_extract_tool_no_op_when_canvas_has_no_pattern(env):
    reasoning, svc, _, _, _ = env
    c = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="Unpatterned", scope="personal",
    )
    out = await reasoning._handle_canvas_tool(
        "canvas_preference_extract",
        {"canvas_id": c.canvas_id, "utterance": "something"},
        _request(),
    )
    data = json.loads(out)
    assert data["ok"] is True
    assert data["matched"] is False
    assert "no declared pattern" in data["reason"]


async def test_extract_tool_unmatched_lightweight_returns_no_op(env):
    reasoning, svc, _, stub, _ = env
    canvas_id = await _make_sd_canvas(svc)
    stub.payload = {"matched": False, "confidence": "low"}
    out = await reasoning._handle_canvas_tool(
        "canvas_preference_extract",
        {"canvas_id": canvas_id, "utterance": "just chatting"},
        _request(),
    )
    data = json.loads(out)
    assert data["ok"] is True
    assert data["matched"] is False
    # No pending preferences added.
    pending = await svc.get_pending_preferences(
        instance_id=INSTANCE, canvas_id=canvas_id,
    )
    assert pending == []


async def test_extract_tool_non_wired_effect_no_op(env):
    """the design review revision #2 — LLM returns effect_kind=other → silent no-op."""
    reasoning, svc, _, stub, _ = env
    canvas_id = await _make_sd_canvas(svc)
    stub.payload = {
        "matched": True,
        "preference_name": "kid-exclusion",
        "preference_value": ["medical"],
        "confidence": "high",
        "effect_kind": "other",  # scope-modifier → not wired in v1
    }
    out = await reasoning._handle_canvas_tool(
        "canvas_preference_extract",
        {"canvas_id": canvas_id, "utterance": "Keep medical out of this canvas"},
        _request(),
    )
    data = json.loads(out)
    assert data["ok"] is True
    assert data["matched"] is False  # forced false by parser
    pending = await svc.get_pending_preferences(
        instance_id=INSTANCE, canvas_id=canvas_id,
    )
    assert pending == []


async def test_extract_tool_suppression_match_surfaces_to_pending(env):
    reasoning, svc, _, stub, _ = env
    canvas_id = await _make_sd_canvas(svc)
    stub.payload = {
        "matched": True,
        "preference_name": "manifest-routing",
        "preference_value": "operator-on-change",
        "confidence": "high",
        "effect_kind": "suppression",
        "evidence": "manifest updates route to operator surface",
    }
    out = await reasoning._handle_canvas_tool(
        "canvas_preference_extract",
        {"canvas_id": canvas_id, "utterance": "Track what's shipped"},
        _request(),
    )
    data = json.loads(out)
    assert data["ok"] is True
    assert data["needs_confirmation"] is True
    assert data["preference_name"] == "manifest-routing"
    # Pending preference actually landed on disk.
    pending = await svc.get_pending_preferences(
        instance_id=INSTANCE, canvas_id=canvas_id,
    )
    assert len(pending) == 1
    assert pending[0]["name"] == "manifest-routing"


async def test_confirm_tool_confirms_pending(env):
    reasoning, svc, _, _, _ = env
    canvas_id = await _make_sd_canvas(svc)
    await svc.add_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        preference={
            "name": "manifest-routing",
            "value": "operator-on-change",
            "effect_kind": "suppression",
            "confidence": "high",
        },
    )
    out = await reasoning._handle_canvas_tool(
        "canvas_preference_confirm",
        {"canvas_id": canvas_id, "preference_name": "manifest-routing",
         "action": "confirm"},
        _request(),
    )
    data = json.loads(out)
    assert data["ok"] is True
    assert data["resolved"]["action"] == "confirm"
    prefs = await svc.get_preferences(
        instance_id=INSTANCE, canvas_id=canvas_id,
    )
    assert prefs["manifest-routing"] == "operator-on-change"


async def test_confirm_tool_discards_pending(env):
    reasoning, svc, _, _, _ = env
    canvas_id = await _make_sd_canvas(svc)
    await svc.add_pending_preference(
        instance_id=INSTANCE, canvas_id=canvas_id,
        preference={"name": "x", "value": "y", "effect_kind": "suppression",
                    "confidence": "high", "evidence": "test"},
    )
    out = await reasoning._handle_canvas_tool(
        "canvas_preference_confirm",
        {"canvas_id": canvas_id, "preference_name": "x", "action": "discard"},
        _request(),
    )
    data = json.loads(out)
    assert data["ok"] is True
    assert data["resolved"]["action"] == "discard"
    assert "x" not in await svc.get_preferences(
        instance_id=INSTANCE, canvas_id=canvas_id,
    )


async def test_confirm_tool_unknown_pending_errors(env):
    reasoning, svc, _, _, _ = env
    canvas_id = await _make_sd_canvas(svc)
    out = await reasoning._handle_canvas_tool(
        "canvas_preference_confirm",
        {"canvas_id": canvas_id, "preference_name": "never-pended",
         "action": "confirm"},
        _request(),
    )
    data = json.loads(out)
    assert data["ok"] is False
    assert "no pending preference" in data["error"].lower()


async def test_extract_tool_requires_utterance(env):
    reasoning, svc, _, _, _ = env
    canvas_id = await _make_sd_canvas(svc)
    out = await reasoning._handle_canvas_tool(
        "canvas_preference_extract",
        {"canvas_id": canvas_id, "utterance": ""},
        _request(),
    )
    data = json.loads(out)
    assert data["ok"] is False
    assert "utterance is required" in data["error"]


async def test_extract_tool_denies_non_member_access(env):
    reasoning, svc, idb, _, _ = env
    await idb.create_member("bob", "Bob", "member", "")
    canvas_id = await _make_sd_canvas(svc)
    out = await reasoning._handle_canvas_tool(
        "canvas_preference_extract",
        {"canvas_id": canvas_id, "utterance": "hi"},
        _request(member_id="bob"),
    )
    data = json.loads(out)
    assert data["ok"] is False
    assert data["error"] == "canvas_not_accessible"


# ---- Gardener dispatch TTL cleanup ---------------------------------------


async def test_gardener_dispatch_drops_expired_pending_prefs(env):
    """Every canvas-event dispatch calls drop_expired_pending_preferences —
    so pending prefs auto-expire at 24h even when the member never
    responds. Test writes a preference with a stale surfaced_at stamp,
    then fires an event on the canvas, then confirms the stale entry is gone."""
    reasoning, svc, idb, stub, tmp_path = env
    canvas_id = await _make_sd_canvas(svc)
    # Manually write a pending with an old timestamp.
    yaml_path = canvas_dir(str(tmp_path), INSTANCE, canvas_id) / "canvas.yaml"
    data = yaml.safe_load(yaml_path.read_text())
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    data.setdefault("pending_preferences", []).append({
        "name": "stale", "value": "v", "effect_kind": "suppression",
        "confidence": "high", "surfaced_at": old_ts,
    })
    yaml_path.write_text(yaml.safe_dump(data))

    # Fire a canvas.page.created event through the Gardener.
    # (page_write doesn't emit via the handler in this test — we call
    # _dispatch directly with the event shape the Gardener expects.)
    gardener = reasoning._handler._get_gardener_service()
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=canvas_id, page_slug="anything.md",
        body="body", writer_member_id=OPERATOR,
    )
    await gardener._dispatch(
        INSTANCE, "canvas.page.created",
        {"canvas_id": canvas_id, "page_path": "anything.md"},
    )

    remaining = await svc.get_pending_preferences(
        instance_id=INSTANCE, canvas_id=canvas_id,
    )
    assert not any(p["name"] == "stale" for p in remaining)
