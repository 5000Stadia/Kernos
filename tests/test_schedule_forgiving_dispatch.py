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


def test_time_field_folded_into_description():
    # Live-observed shapes (2026-06-08): the model parks the message in `text`
    # and the WHEN in a separate field. The time MUST be folded into the desc so
    # extraction sees it — otherwise a time-less description bounces the create.
    action, desc = normalize_schedule_input(
        {"type": "reminder", "text": "loop fix verify", "scheduled_time": "in 5 minutes"}
    )
    assert action == "create"
    assert "loop fix verify" in desc and "in 5 minutes" in desc

    # reminder_text + time_offset
    _, desc2 = normalize_schedule_input(
        {"reminder_text": "take vitamins", "time_offset": "2 hours"}
    )
    assert "take vitamins" in desc2 and "2 hours" in desc2

    # when + timezone both folded
    _, desc3 = normalize_schedule_input(
        {"action": "create", "text": "standup", "when": "2026-06-08T20:31:00",
         "timezone": "America/Los_Angeles"}
    )
    assert "standup" in desc3 and "2026-06-08T20:31:00" in desc3 and "America/Los_Angeles" in desc3


def test_time_hint_alone_infers_create():
    # Only a time field, no text → still a create with the time as the desc.
    action, desc = normalize_schedule_input({"time_offset": "in 1 hour"})
    assert action == "create"
    assert "in 1 hour" in desc


def test_type_field_not_a_valid_action_falls_through_to_inference():
    # `type` is usually the trigger KIND, not an action. "notify" must NOT become
    # the action (would be an invalid enum) — infer create from the text instead.
    action, _ = normalize_schedule_input({"type": "notify", "text": "ping me at 5"})
    assert action == "create"
    # But `type` that DOES name an action is honored.
    assert normalize_schedule_input({"type": "list"})[0] == "list"


def test_list_action_preserved_despite_stray_time_field():
    # An explicit list must stay list even if a stray timezone rides along.
    assert normalize_schedule_input(
        {"action": "list", "timezone": "America/Los_Angeles"}
    )[0] == "list"


def test_timezone_only_metadata_defaults_to_list():
    # Ambient timezone with no action/text/real-time hint must NOT infer create
    # (Codex P2) — it defaults to list and carries no fabricated description.
    action, desc = normalize_schedule_input({"timezone": "America/Los_Angeles"})
    assert action == "list"
    assert desc == ""
    # But timezone DOES attach when there's real content to schedule.
    _, desc2 = normalize_schedule_input(
        {"text": "standup", "when": "in 1h", "timezone": "America/Los_Angeles"}
    )
    assert "standup" in desc2 and "in 1h" in desc2 and "America/Los_Angeles" in desc2


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


def test_binding_diagnostic_extra_cannot_clobber_canonical():
    # v1 self-test Test 14: the dispatch-gate self-review flagged that
    # to_payload() spread `extra` last, letting a tool-specific extra overwrite
    # canonical attribution. Canonical fields must win a key collision.
    from kernos.kernel.dispatch_diagnostics import BindingFailureDiagnostic
    d = BindingFailureDiagnostic(
        tool_id="real_tool", status="not_registered",
        extra={"tool_id": "EVIL", "status": "FAKE", "note": "kept"},
    )
    p = d.to_payload()
    assert p["tool_id"] == "real_tool"   # canonical wins
    assert p["status"] == "not_registered"
    assert p["note"] == "kept"           # non-colliding extra preserved
