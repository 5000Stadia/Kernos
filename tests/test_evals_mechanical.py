"""Tests for EVAL-MECHANICAL-RUBRICS primitives, parser, and dispatch."""
from __future__ import annotations

from pathlib import Path

import pytest

from kernos.evals.mechanical import (
    KNOWN_CHECKS, PROJECTOR_SCHEMAS, evaluate_mechanical,
    observation_absent, observation_field_equals, observation_has,
    reply_contains, reply_does_not_contain, tool_called, tool_not_called,
    trace_event_fired, validate_mechanical_rubric,
)
from kernos.evals.rubrics import _evaluate_mechanical
from kernos.evals.scenario import parse_scenario
from kernos.evals.types import Rubric, Scenario, ScenarioResult, TurnResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(
    replies: list[str] | None = None,
    observations: dict | None = None,
    tool_calls: list[dict] | None = None,
    trace_events: list[dict] | None = None,
) -> ScenarioResult:
    scenario = Scenario(name="test", file_path=Path("test.md"))
    turn_results = []
    for i, r in enumerate(replies or [], start=1):
        turn_results.append(TurnResult(
            turn_index=i, sender_display=f"t{i}", content="", reply=r,
        ))
    return ScenarioResult(
        scenario=scenario,
        started_at="2026-04-21T00:00:00Z",
        turn_results=turn_results,
        observations=observations or {},
        tool_calls=tool_calls or [],
        trace_events=trace_events or [],
    )


# ---------------------------------------------------------------------------
# reply_contains / reply_does_not_contain
# ---------------------------------------------------------------------------


def test_reply_contains_matches_pattern():
    r = _make_result(replies=["hi harold", "goodbye"])
    v = reply_contains(r, "any", r"harold")
    assert v.passed
    assert "harold" in v.reason.lower()


def test_reply_contains_specific_turn():
    r = _make_result(replies=["nope", "yes match here"])
    assert reply_contains(r, 2, "match").passed
    assert not reply_contains(r, 1, "match").passed


def test_reply_contains_no_match():
    r = _make_result(replies=["hello world"])
    v = reply_contains(r, "any", "goodbye")
    assert not v.passed
    assert "no match" in v.reason


def test_reply_does_not_contain_absent():
    r = _make_result(replies=["all clear"])
    v = reply_does_not_contain(r, "any", r"mem_[a-f0-9]+")
    assert v.passed


def test_reply_does_not_contain_caught():
    r = _make_result(replies=["leaking mem_abc123def456 here"])
    v = reply_does_not_contain(r, "any", r"mem_[a-f0-9]+")
    assert not v.passed
    assert "mem_abc123def456" in v.reason


def test_reply_does_not_contain_vacuous_no_replies():
    r = _make_result(replies=[])
    v = reply_does_not_contain(r, "any", "anything")
    assert v.passed
    assert "vacuous" in v.reason


def test_reply_contains_empty_pattern_fails_defensively():
    r = _make_result(replies=["something"])
    v = reply_contains(r, "any", "")
    assert not v.passed


# ---------------------------------------------------------------------------
# observation_has / observation_field_equals / observation_absent
# ---------------------------------------------------------------------------


def test_observation_has_finds_match():
    r = _make_result(observations={
        "relationships:emma": [
            {"declarer": "emma", "other": "owner", "permission": "full-access"},
            {"declarer": "owner", "other": "emma", "permission": "by-permission"},
        ],
    })
    v = observation_has(r, "relationships:emma", {"declarer": "emma", "permission": "full-access"})
    assert v.passed


def test_observation_has_no_match():
    r = _make_result(observations={
        "relationships:emma": [
            {"declarer": "owner", "other": "emma", "permission": "by-permission"},
        ],
    })
    v = observation_has(r, "relationships:emma", {"permission": "full-access"})
    assert not v.passed
    assert "no entry" in v.reason


def test_observation_has_missing_observation():
    r = _make_result(observations={})
    v = observation_has(r, "relationships:emma", {})
    assert not v.passed
    assert "no observation" in v.reason


def test_observation_field_equals_on_dict():
    r = _make_result(observations={
        "member_profile:owner": {"display_name": "Harold", "hatched": True},
    })
    assert observation_field_equals(r, "member_profile:owner", "display_name", "Harold").passed
    assert not observation_field_equals(r, "member_profile:owner", "display_name", "Other").passed


def test_observation_field_equals_on_list_any_entry():
    r = _make_result(observations={
        "outbound": [{"channel": "discord", "message": "hi"}],
    })
    assert observation_field_equals(r, "outbound", "channel", "discord").passed


def test_observation_field_equals_matches_later_list_entry():
    """Codex review F1: an any-entry match — not just first — so scenarios
    that look for a match anywhere in a projector list don't silently fail
    when the match sits past position 0."""
    r = _make_result(observations={
        "relationships:owner": [
            {"declarer": "owner", "other": "emma", "permission": "by-permission"},
            {"declarer": "owner", "other": "emma", "permission": "full-access"},
        ],
    })
    v = observation_field_equals(r, "relationships:owner", "permission", "full-access")
    assert v.passed
    assert "[1]" in v.reason  # Cites the second entry


def test_observation_field_equals_list_no_match_reports_all_seen():
    r = _make_result(observations={
        "relationships:owner": [
            {"declarer": "owner", "permission": "by-permission"},
            {"declarer": "owner", "permission": "no-access"},
        ],
    })
    v = observation_field_equals(r, "relationships:owner", "permission", "full-access")
    assert not v.passed
    # The report should cite what values WERE seen so diagnosis is quick.
    assert "by-permission" in v.reason and "no-access" in v.reason


def test_observation_absent_missing():
    r = _make_result(observations={})
    assert observation_absent(r, "knowledge").passed


def test_observation_absent_empty():
    r = _make_result(observations={"knowledge": []})
    assert observation_absent(r, "knowledge").passed


def test_observation_absent_populated_fails():
    r = _make_result(observations={"knowledge": [{"id": 1}]})
    v = observation_absent(r, "knowledge")
    assert not v.passed


# ---------------------------------------------------------------------------
# trace_event_fired
# ---------------------------------------------------------------------------


def test_trace_event_fired_present():
    r = _make_result(trace_events=[
        {"event": "SURFACE_LEAK_DETECTED", "turn_index": 1, "detail": "x"},
    ])
    assert trace_event_fired(r, "SURFACE_LEAK_DETECTED").passed


def test_trace_event_fired_absent():
    r = _make_result(trace_events=[])
    v = trace_event_fired(r, "SURFACE_LEAK_DETECTED")
    assert not v.passed
    assert "not observed" in v.reason


# ---------------------------------------------------------------------------
# tool_called / tool_not_called
# ---------------------------------------------------------------------------


def test_tool_called_present():
    r = _make_result(tool_calls=[{"name": "manage_members", "turn_index": 2}])
    assert tool_called(r, "manage_members").passed


def test_tool_called_absent():
    r = _make_result(tool_calls=[{"name": "other", "turn_index": 1}])
    v = tool_called(r, "manage_members")
    assert not v.passed
    assert "other" in v.reason


def test_tool_not_called_passes_when_absent():
    r = _make_result(tool_calls=[])
    assert tool_not_called(r, "send_to_channel").passed


def test_tool_not_called_fails_when_present():
    r = _make_result(tool_calls=[{"name": "send_to_channel", "turn_index": 1}])
    v = tool_not_called(r, "send_to_channel")
    assert not v.passed


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_evaluate_mechanical_routes_known_check():
    r = _make_result(replies=["hi"])
    v = evaluate_mechanical("reply_contains", {"turn": "any", "pattern": "hi"}, r)
    assert v.passed


def test_evaluate_mechanical_unknown_check_fails_loudly():
    r = _make_result()
    v = evaluate_mechanical("make_up_a_check", {}, r)
    assert not v.passed
    assert "unknown" in v.reason


def test_dispatch_covers_every_known_check():
    for name in KNOWN_CHECKS:
        v = evaluate_mechanical(name, {}, _make_result())
        # Every known check either passes vacuously or produces a legible
        # fail reason — what must NOT happen is a crash or the "unknown
        # mechanical check" sentinel leaking through.
        assert "unknown mechanical check" not in v.reason, (
            f"dispatch forgot {name!r}"
        )


def test_evaluate_mechanical_wrapper_yields_rubric_verdict():
    r = _make_result(replies=["hi"])
    rubric = Rubric(
        question="reply contains hi", kind="mechanical",
        check="reply_contains", params={"turn": "any", "pattern": "hi"},
    )
    verdict = _evaluate_mechanical(rubric, r)
    assert verdict.passed
    assert "[mechanical:reply_contains]" in verdict.reasoning


# ---------------------------------------------------------------------------
# Parse-time validation
# ---------------------------------------------------------------------------


def test_validate_unknown_check():
    err = validate_mechanical_rubric("nope", {})
    assert err and "unknown" in err


def test_validate_reply_contains_missing_pattern():
    err = validate_mechanical_rubric("reply_contains", {"turn": "any"})
    assert "pattern" in err


def test_validate_observation_has_unknown_kind():
    err = validate_mechanical_rubric(
        "observation_has",
        {"observation": "made_up_projector:thing", "where": {}},
    )
    assert "not a known projector output" in err


def test_validate_observation_has_bad_where_key():
    err = validate_mechanical_rubric(
        "observation_has",
        {"observation": "relationships:emma", "where": {"bogus_field": "x"}},
    )
    assert "bogus_field" in err


def test_validate_observation_field_equals_bad_field():
    err = validate_mechanical_rubric(
        "observation_field_equals",
        {"observation": "knowledge", "field": "nope", "value": 1},
    )
    assert "nope" in err


def test_validate_trace_event_missing_name():
    err = validate_mechanical_rubric("trace_event_fired", {})
    assert "event_name" in err


def test_validate_tool_called_missing_name():
    err = validate_mechanical_rubric("tool_called", {})
    assert "tool_name" in err


def test_validate_happy_path_mechanical():
    assert validate_mechanical_rubric(
        "reply_does_not_contain",
        {"turn": "any", "pattern": r"mem_[a-f0-9]+"},
    ) == ""
    assert validate_mechanical_rubric(
        "observation_has",
        {"observation": "relationships:emma", "where": {"declarer": "emma", "permission": "full-access"}},
    ) == ""


# ---------------------------------------------------------------------------
# Parser round-trip
# ---------------------------------------------------------------------------


def test_parser_reads_semantic_rubric_default():
    text = """# Test
## Rubrics
- The agent declined politely.
"""
    scenario = parse_scenario(text)
    assert len(scenario.rubrics) == 1
    rb = scenario.rubrics[0]
    assert rb.kind == "semantic"
    assert rb.check == ""
    assert "declined" in rb.question


def test_parser_reads_mechanical_reply_does_not_contain():
    text = """# Test
## Rubrics
- kind: mechanical
  check: reply_does_not_contain
  turn: any
  pattern: 'mem_[a-f0-9]+'
"""
    scenario = parse_scenario(text)
    assert len(scenario.rubrics) == 1
    rb = scenario.rubrics[0]
    assert rb.kind == "mechanical"
    assert rb.check == "reply_does_not_contain"
    assert rb.params["pattern"] == "mem_[a-f0-9]+"
    assert rb.params["turn"] == "any"


def test_parser_reads_mechanical_observation_has_with_where():
    text = """# Test
## Rubrics
- kind: mechanical
  check: observation_has
  observation: relationships:emma
  where:
    declarer: emma
    permission: full-access
"""
    scenario = parse_scenario(text)
    rb = scenario.rubrics[0]
    assert rb.kind == "mechanical"
    assert rb.check == "observation_has"
    assert rb.params["observation"] == "relationships:emma"
    assert rb.params["where"] == {"declarer": "emma", "permission": "full-access"}


def test_parser_rejects_mechanical_with_unknown_projector_kind():
    text = """# Test
## Rubrics
- kind: mechanical
  check: observation_has
  observation: fake_projector:xyz
  where:
    foo: bar
"""
    with pytest.raises(ValueError, match="not a known projector output"):
        parse_scenario(text)


def test_parser_mixed_semantic_and_mechanical():
    text = """# Test
## Rubrics
- kind: mechanical
  check: reply_does_not_contain
  turn: any
  pattern: 'mem_[a-f0-9]+'
- The agent's reply is warm and consistent.
- kind: mechanical
  check: trace_event_fired
  event_name: SURFACE_LEAK_DETECTED
"""
    scenario = parse_scenario(text)
    assert len(scenario.rubrics) == 3
    assert scenario.rubrics[0].kind == "mechanical"
    assert scenario.rubrics[1].kind == "semantic"
    assert scenario.rubrics[2].kind == "mechanical"
    assert scenario.rubrics[2].check == "trace_event_fired"


# ---------------------------------------------------------------------------
# Log-format contract (Codex review F2)
#
# _EvalLogCapture matches `TOOL_DISPATCH: name=X` and
# `AGENT_RESULT: tool=X success=Y` via regex against log messages emitted by
# kernos.kernel.reasoning. If the kernel ever rewrites those format strings,
# mechanical `tool_called`/`tool_not_called` rubrics would silently drift.
# These tests assert the exact literal text in `reasoning.py` so a format
# change triggers a test failure instead of a quiet eval regression.
# ---------------------------------------------------------------------------




def test_log_format_surface_leak_detected_string_is_stable():
    from pathlib import Path
    src = Path("kernos/messages/handler.py").read_text(encoding="utf-8")
    assert 'SURFACE_LEAK_DETECTED' in src, (
        "handler.py SURFACE_LEAK_DETECTED marker removed — "
        "_EvalLogCapture._TRACE_EVENT_NAMES must be updated in the same "
        "commit, or trace_event_fired rubrics become silent no-ops."
    )


def test_projector_schema_registry_lists_all_runner_kinds():
    # Guardrail: if the runner adds a new observation kind, this list must
    # be updated in the same commit or mechanical rubrics against it will
    # fail loudly at parse time.
    expected = {
        "member_profile", "knowledge", "relational_messages",
        "relationships", "covenants", "outbound", "conversation_log",
    }
    assert expected == set(PROJECTOR_SCHEMAS.keys())


# ---------------------------------------------------------------------------
# test_log_format_*_is_stable tests removed by CCV1 C7 strike (2026-05-03).
# These pinned legacy-loop log-line formats (TOOL_DISPATCH: name=%s,
# AGENT_RESULT: %s) that were inside _execute_single_tool. The strike
# removed that wrapper; live dispatchers now emit TOOL_CALLED /
# TOOL_RESULT events instead. The eval mechanical rubric's regex
# patterns migrated to the new event-stream-based capture (see
# _EvalLogCapture). Tests retire with the legacy log-line shape per
# the architect's "retired by architectural transition" subprinciple.
# ---------------------------------------------------------------------------


