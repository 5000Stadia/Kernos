"""v1 self-test Test 6 (2026-06-08): the reminder test FAILED because the model
reached for invalid actions ('create_reminder'), a dotted tool name
('reminder.create'), and put the schedule text in fields other than
'description'. Forgiving dispatch + aliases fix the generalized scheduling call.
"""
from kernos.kernel.scheduler import normalize_schedule_input
from kernos.kernel.tool_aliases import canonicalize_tool_name


def test_action_synonyms_map_to_create():
    for a in ("create_reminder", "set_reminder", "add_reminder", "add",
              "new", "set", "schedule", "remind", "reminder"):
        action, _ = normalize_schedule_input({"action": a, "description": "x"})
        assert action == "create", f"{a!r} -> {action!r}"


def test_action_synonyms_map_remove_and_list():
    assert normalize_schedule_input({"action": "cancel"})[0] == "remove"
    assert normalize_schedule_input({"action": "delete"})[0] == "remove"
    assert normalize_schedule_input({"action": "show"})[0] == "list"


def test_canonical_actions_unchanged():
    for a in ("create", "list", "update", "pause", "resume", "remove"):
        assert normalize_schedule_input({"action": a})[0] == a


def test_text_resolved_from_alternate_fields():
    # description preferred, but fall back through message/text/reminder/...
    assert normalize_schedule_input(
        {"action": "create", "message": "v1 self-test reminder"}
    )[1] == "v1 self-test reminder"
    assert normalize_schedule_input(
        {"action": "create", "reminder": "stretch"}
    )[1] == "stretch"
    assert normalize_schedule_input(
        {"action": "create", "description": "win", "message": "lose"}
    )[1] == "win"  # description wins when present


def test_missing_action_defaults_list():
    assert normalize_schedule_input({})[0] == "list"


def test_reminder_tool_name_hallucinations_alias():
    for name in ("reminder.create", "create_reminder", "set_reminder",
                 "add_reminder", "schedule.create", "reminders.create"):
        assert canonicalize_tool_name(name) == ("manage_schedule", True), name


def test_no_action_with_text_infers_create():
    # create_reminder tool alias rewrites name to manage_schedule but carries
    # no action — text present means create-intent, not list (Codex review).
    action, desc = normalize_schedule_input({"description": "remind me in 1h"})
    assert action == "create" and desc == "remind me in 1h"
    action2, _ = normalize_schedule_input({"message": "stretch in 2h"})
    assert action2 == "create"


def test_no_action_no_text_defaults_list():
    assert normalize_schedule_input({})[0] == "list"
    assert normalize_schedule_input({"action": ""})[0] == "list"
