"""CANVAS-GARDENER-PREFERENCE-CAPTURE Commit 2 — extraction consultation.

Covers:
  - detect_explicit_phrases pure helper
  - _parse_preference_extraction subject-matter validation (the design review rev #2)
  - _parse_preference_extraction novel-preference confidence downgrade
  - judge_preference_extraction end-to-end with a stubbed reasoning service
  - GardenerService.consult_preference_extraction integration
"""
from __future__ import annotations

import json

import pytest

from kernos.cohorts.gardener import (
    EXPLICIT_PREFERENCE_PHRASES,
    WIRED_EFFECT_KINDS,
    PreferenceExtractionContext,
    PreferenceExtractionExhausted,
    PreferenceExtractionResult,
    _parse_preference_extraction,
    detect_explicit_phrases,
    judge_preference_extraction,
)
from kernos.cohorts.gardener_prompts import build_preference_extraction_prompt
from kernos.kernel.canvas import CanvasService
from kernos.kernel.gardener import GardenerService
from kernos.kernel.instance_db import InstanceDB


INSTANCE = "inst_prefextr"
OPERATOR = "member:inst_prefextr:owner"


# ---- detect_explicit_phrases ---------------------------------------------


def test_explicit_phrases_trip_detection():
    for phrase in ("remember that I hate RSVPs",
                   "from now on, skip that",
                   "Always ping me on archive",
                   "I never want that surfaced",
                   "don't let me ship without review",
                   "keep medical private",
                   "this canvas is for planning only"):
        assert detect_explicit_phrases(phrase), phrase


def test_explicit_phrases_case_insensitive():
    assert detect_explicit_phrases("REMEMBER THAT I dislike rsvps")
    assert detect_explicit_phrases("This Canvas Is the household planner")


def test_neutral_utterance_is_not_explicit():
    assert not detect_explicit_phrases("just saying hi")
    assert not detect_explicit_phrases("")
    assert not detect_explicit_phrases("what did we decide about Frank?")


def test_explicit_phrase_list_is_stable():
    # the design review revision #1 relies on this vocabulary; guard it against silent edits.
    assert "remember that" in EXPLICIT_PREFERENCE_PHRASES
    assert "from now on" in EXPLICIT_PREFERENCE_PHRASES
    assert "always" in EXPLICIT_PREFERENCE_PHRASES
    assert "never" in EXPLICIT_PREFERENCE_PHRASES


# ---- _parse_preference_extraction parsing + validation -------------------


def _ctx(**overrides) -> PreferenceExtractionContext:
    base = dict(
        instance_id="i1", canvas_id="c1",
        canvas_pattern="long-form-campaign",
        utterance="test",
        known_intent_hook_names=["rsvp-routing", "staleness-days"],
        current_preferences={},
        declined_preference_names=[],
    )
    base.update(overrides)
    return PreferenceExtractionContext(**base)


def test_parse_unmatched_passthrough():
    raw = json.dumps({"matched": False, "confidence": "low"})
    result = _parse_preference_extraction(raw, _ctx())
    assert not result.matched


def test_parse_malformed_json_returns_unmatched():
    result = _parse_preference_extraction("not-json", _ctx())
    assert not result.matched


def test_parse_empty_string_returns_unmatched():
    result = _parse_preference_extraction("", _ctx())
    assert not result.matched


def test_parse_suppression_match_surfaces():
    raw = json.dumps({
        "matched": True,
        "preference_name": "rsvp-routing",
        "preference_value": "threshold-only",
        "evidence": "don't spam me",
        "confidence": "high",
        "supersedes": None,
        "effect_kind": "suppression",
    })
    result = _parse_preference_extraction(raw, _ctx())
    assert result.matched
    assert result.effect_kind == "suppression"
    assert result.confidence == "high"
    assert result.should_surface


def test_parse_threshold_match_surfaces():
    raw = json.dumps({
        "matched": True,
        "preference_name": "staleness-days",
        "preference_value": 180,
        "evidence": "staleness for this one is 180 days",
        "confidence": "high",
        "effect_kind": "threshold",
    })
    result = _parse_preference_extraction(raw, _ctx())
    assert result.matched
    assert result.effect_kind == "threshold"
    assert result.preference_value == 180
    assert result.should_surface


# the design review revision #2: non-wired effect_kind forces matched=false.


def test_parse_other_effect_kind_forces_unmatched():
    """the design review revision #2 — preferences whose effect isn't wired (suppression
    or threshold in v1) must silently no-op. No confirmation whisper fires."""
    raw = json.dumps({
        "matched": True,
        "preference_name": "kid-exclusion",
        "preference_value": ["medical"],
        "confidence": "high",
        "effect_kind": "other",   # scope modifier — not wired in v1
    })
    result = _parse_preference_extraction(raw, _ctx())
    assert not result.matched
    assert not result.should_surface


def test_parse_unknown_effect_kind_rejected():
    raw = json.dumps({
        "matched": True,
        "preference_name": "x",
        "confidence": "high",
        "effect_kind": "routing-override",  # unknown to v1
    })
    result = _parse_preference_extraction(raw, _ctx())
    assert not result.matched


def test_parse_missing_effect_kind_rejected():
    raw = json.dumps({
        "matched": True,
        "preference_name": "x",
        "confidence": "high",
    })
    result = _parse_preference_extraction(raw, _ctx())
    assert not result.matched


# Novel-preference downgrade


def test_novel_preference_downgrades_confidence():
    """A preference name not in the pattern's intent-hook vocabulary drops one tier."""
    raw = json.dumps({
        "matched": True,
        "preference_name": "novel-custom-key",   # not in known_intent_hook_names
        "preference_value": True,
        "confidence": "high",
        "effect_kind": "suppression",
    })
    result = _parse_preference_extraction(
        raw, _ctx(known_intent_hook_names=["rsvp-routing"]),
    )
    assert result.matched
    # High downgrades to medium when novel → no longer surfaces.
    assert result.confidence == "medium"
    assert not result.should_surface


def test_known_preference_keeps_confidence():
    raw = json.dumps({
        "matched": True,
        "preference_name": "rsvp-routing",
        "preference_value": "threshold-only",
        "confidence": "high",
        "effect_kind": "suppression",
    })
    result = _parse_preference_extraction(
        raw, _ctx(known_intent_hook_names=["rsvp-routing"]),
    )
    assert result.confidence == "high"
    assert result.should_surface


def test_medium_confidence_does_not_surface_even_when_known():
    raw = json.dumps({
        "matched": True,
        "preference_name": "rsvp-routing",
        "confidence": "medium",
        "effect_kind": "suppression",
    })
    result = _parse_preference_extraction(
        raw, _ctx(known_intent_hook_names=["rsvp-routing"]),
    )
    assert result.matched
    # Only high confidence surfaces.
    assert not result.should_surface


def test_invalid_confidence_defaults_to_low():
    raw = json.dumps({
        "matched": True,
        "preference_name": "x",
        "confidence": "certain",  # not a valid enum
        "effect_kind": "suppression",
    })
    result = _parse_preference_extraction(raw, _ctx())
    assert result.confidence == "low"


def test_supersession_passes_through():
    raw = json.dumps({
        "matched": True,
        "preference_name": "rsvp-routing",
        "preference_value": "every-rsvp",
        "confidence": "high",
        "supersedes": "rsvp-routing",
        "effect_kind": "suppression",
    })
    result = _parse_preference_extraction(
        raw, _ctx(known_intent_hook_names=["rsvp-routing"]),
    )
    assert result.supersedes == "rsvp-routing"


# ---- Prompt builder -------------------------------------------------------


def test_prompt_builder_mentions_utterance_and_vocabulary():
    ctx = _ctx(utterance="don't ping me on every RSVP")
    system, user = build_preference_extraction_prompt(ctx)
    assert "suppression" in system
    assert "threshold" in system
    assert "don't ping me on every RSVP" in user
    assert "rsvp-routing" in user   # from known_intent_hook_names


def test_prompt_builder_handles_empty_vocabulary():
    ctx = _ctx(known_intent_hook_names=[])
    system, user = build_preference_extraction_prompt(ctx)
    assert "no intent-hook vocabulary known" in user


# ---- judge_preference_extraction end-to-end ------------------------------


class _StubReasoning:
    """Returns a pre-canned JSON response for each complete_simple call."""
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    async def complete_simple(self, *, system_prompt, user_content, chain,
                               output_schema=None, max_tokens=512):
        self.calls.append({"chain": chain, "max_tokens": max_tokens})
        return json.dumps(self.payload)


async def test_judge_routes_through_lightweight_chain():
    stub = _StubReasoning({
        "matched": True,
        "preference_name": "rsvp-routing",
        "preference_value": "threshold-only",
        "confidence": "high",
        "effect_kind": "suppression",
    })
    result = await judge_preference_extraction(
        _ctx(known_intent_hook_names=["rsvp-routing"]),
        reasoning_service=stub,
    )
    assert len(stub.calls) == 1
    assert stub.calls[0]["chain"] == "lightweight"
    assert result.should_surface


async def test_judge_surfaces_exhaustion_as_domain_exception():
    class _Fail:
        async def complete_simple(self, **kw):
            raise RuntimeError("all providers failed")
    with pytest.raises(PreferenceExtractionExhausted):
        await judge_preference_extraction(_ctx(), reasoning_service=_Fail())


# ---- GardenerService integration -----------------------------------------


@pytest.fixture
async def gardener_env(tmp_path):
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member(OPERATOR, "Op", "owner", "")
    svc = CanvasService(instance_db=idb, data_dir=str(tmp_path))
    stub = _StubReasoning({})  # per-test override via .payload
    gardener = GardenerService(
        canvas_service=svc, instance_db=idb, reasoning_service=stub,
    )
    yield gardener, svc, stub, idb
    await idb.close()


async def test_service_consult_returns_extraction_result(gardener_env):
    gardener, svc, stub, _ = gardener_env
    stub.payload = {
        "matched": True,
        "preference_name": "rsvp-routing",
        "preference_value": "threshold-only",
        "confidence": "high",
        "effect_kind": "suppression",
    }
    result = await gardener.consult_preference_extraction(
        _ctx(known_intent_hook_names=["rsvp-routing"]),
    )
    assert isinstance(result, PreferenceExtractionResult)
    assert result.should_surface


async def test_service_swallows_exhaustion_as_no_op(gardener_env):
    gardener, _, _, _ = gardener_env

    class _Fail:
        async def complete_simple(self, **kw):
            raise RuntimeError("dead")
    gardener._reasoning = _Fail()
    result = await gardener.consult_preference_extraction(_ctx())
    assert not result.matched
    assert not result.should_surface


async def test_wired_effect_kinds_enumerated():
    # Sanity: v1 is explicit about what's wired.
    assert WIRED_EFFECT_KINDS == {"suppression", "threshold"}
