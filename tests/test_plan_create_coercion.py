"""Generalized manage_plan create (2026-06-07): the model rarely builds the full
nested phases->steps->id/title shape, so plan creation failed ("'phases' is
required") and the self-directed-execution capability didn't deliver. create now
accepts the flat step list the model naturally emits and auto-builds the
scaffolding. This helps EVERY multi-step task, not the self-test.
"""
from kernos.messages.handler import _coerce_plan_phases
from kernos.kernel.tool_aliases import canonicalize_tool_name


def test_flat_steps_strings_build_one_phase():
    phases = _coerce_plan_phases({"steps": ["read the file", "summarize", "write result"]})
    assert len(phases) == 1
    steps = phases[0]["steps"]
    assert [s["title"] for s in steps] == ["read the file", "summarize", "write result"]
    # ids + status auto-filled
    assert all(s["id"] and s["status"] == "pending" for s in steps)
    assert phases[0]["id"] and phases[0]["title"]


def test_tasks_and_items_synonyms():
    assert _coerce_plan_phases({"tasks": ["a"]})[0]["steps"][0]["title"] == "a"
    assert _coerce_plan_phases({"items": ["b"]})[0]["steps"][0]["title"] == "b"


def test_step_dicts_with_description():
    phases = _coerce_plan_phases({"steps": [{"description": "do X"}, {"title": "do Y"}]})
    assert [s["title"] for s in phases[0]["steps"]] == ["do X", "do Y"]


def test_canonical_phases_normalized_fills_missing_ids():
    phases = _coerce_plan_phases({"phases": [
        {"title": "Phase A", "steps": [{"title": "s1"}, "s2"]},
    ]})
    assert phases[0]["id"]                       # auto id
    assert all(s["id"] for s in phases[0]["steps"])
    assert phases[0]["steps"][1]["title"] == "s2"   # bare string step ok


def test_nothing_to_plan_returns_empty():
    assert _coerce_plan_phases({"title": "x"}) == []
    assert _coerce_plan_phases({}) == []


def test_invented_plan_names_alias_to_manage_plan():
    for name in ("self_directed_plan", "self_directed_execution", "create_plan"):
        assert canonicalize_tool_name(name) == ("manage_plan", True)
