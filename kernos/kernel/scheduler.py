"""Scheduler — time and event triggers, manage_schedule tool.

Triggers are persistent records that fire actions at specified times
or in response to external events (calendar, etc.).
- Notify: send a message to the user (always authorized)
- Tool call: execute a tool with covenant pre-authorization
- Event: poll external sources and fire on matching events
"""
import json
import logging
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kernos.kernel.state import StateStore
from kernos.kernel.tool_failure import ToolFailure
from kernos.utils import utc_now

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Trigger:
    """A persistent scheduled action."""

    trigger_id: str
    instance_id: str
    member_id: str = ""
    space_id: str = ""
    conversation_id: str = ""             # Platform conversation (e.g. Discord channel ID)

    # Condition
    condition_type: str = "time"         # "time" for 3E-B
    condition: str = ""                  # ISO datetime or cron expression
    next_fire_at: str = ""               # ISO datetime — precomputed next fire time
    recurrence: str = ""                 # Cron expression for recurring. Empty = one-shot.

    # Action
    action_type: str = "notify"          # "notify" or "tool_call"
    action_description: str = ""         # Human-readable
    action_params: dict = field(default_factory=dict)  # {message, tool_name, tool_args, ...}

    # Delivery
    notify_via: str = ""                 # Channel name. Empty = default.
    delivery_class: str = "stage"        # "ambient" | "stage" | "interrupt"

    # Authorization
    authorization_covenant_id: str = ""  # Covenant rule ID authorizing this action

    # Lifecycle
    status: str = "active"              # "active", "paused", "completed", "failed", "retired"
    created_at: str = ""
    last_fired_at: str = ""
    fire_count: int = 0
    failure_reason: str = ""
    pending_delivery: str = ""           # Held result if outbound failed

    # Failure classification
    failure_class: str = ""              # "structural" or "transient" or ""
    transient_failure_count: int = 0     # Consecutive transient failures
    last_failure_at: str = ""            # ISO timestamp of last failure
    degraded: bool = False               # True if active but dependency broken
    retired_at: str = ""                 # ISO timestamp if retired

    # Event-specific fields (used when condition_type == "event")
    event_source: str = ""               # "calendar" (future: "gmail", "webhook")
    event_filter: str = ""               # Keyword filter on event TITLE ONLY
    event_lead_minutes: int = 30         # How far before the event to fire
    event_matched_ids: list[str] = field(default_factory=list)  # Duplicate suppression
    event_daily_fire_count: int = 0      # Anti-spam: fires today (standing triggers only)
    event_daily_fire_date: str = ""      # ISO date of last fire count reset

    # Replacement chain
    replaced_by: str = ""               # trigger_id that superseded this one

    # Preference linkage (Phase 6A)
    source_preference_id: str = ""      # Preference ID that generated this trigger, or ""

    # Provenance
    source: str = ""  # "explicit_schedule" | "compaction_commitment" | "" (legacy)

    # Audit
    created_by_tool_call: str = ""

    # IANA timezone captured at creation — recurring cron is local-time intent
    # ("every morning at 8am"), so reschedules evaluate the cron in this zone.
    timezone: str = ""


def _trigger_id() -> str:
    return f"trig_{uuid.uuid4().hex[:8]}"




TRANSIENT_FAILURE_NOTIFY_THRESHOLD = 10


def classify_trigger_failure(error: str | Exception) -> str:
    """Classify a trigger failure as 'structural' or 'transient'.

    Structural: trigger itself is invalid, will never succeed.
    Transient: dependency is temporarily broken, may recover.

    Conservative default: transient. Better to retry a broken
    trigger than retire a valid one.
    """
    err_str = str(error).lower()

    structural_patterns = [
        "not found",
        "not handled",
        "no longer exists",
        "unknown tool",
        "not available",
        "not registered",
        "permanently unavailable",
    ]

    for pattern in structural_patterns:
        if pattern in err_str:
            return "structural"

    return "transient"


def resolve_owner_member_id(instance_id: str) -> str:
    """Canonical owner member ID for an instance.

    Centralized resolver — do not construct member IDs by
    splitting instance_id strings elsewhere.
    """
    return f"member:{instance_id}:owner"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TriggerStore:
    """JSON-on-disk persistence for triggers."""

    def __init__(self, data_dir: str | Path) -> None:
        from kernos.utils import _safe_name
        self._data_dir = Path(data_dir)
        self._safe_name = _safe_name

    def _path(self, instance_id: str) -> Path:
        return self._data_dir / self._safe_name(instance_id) / "state" / "triggers.json"

    def _read(self, instance_id: str) -> list[dict]:
        path = self._path(instance_id)
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, instance_id: str, data: list[dict]) -> None:
        import tempfile
        path = self._path(instance_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            import os
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, str(path))
        except Exception:
            import os
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    async def save(self, trigger: Trigger) -> None:
        raw = self._read(trigger.instance_id)
        for i, d in enumerate(raw):
            if d.get("trigger_id") == trigger.trigger_id:
                raw[i] = asdict(trigger)
                self._write(trigger.instance_id, raw)
                return
        raw.append(asdict(trigger))
        self._write(trigger.instance_id, raw)

    async def get(self, instance_id: str, trigger_id: str) -> Trigger | None:
        for d in self._read(instance_id):
            if d.get("trigger_id") == trigger_id:
                return Trigger(**d)
        return None

    async def list_active(self, instance_id: str) -> list[Trigger]:
        return [Trigger(**d) for d in self._read(instance_id) if d.get("status") == "active"]

    async def list_all(self, instance_id: str) -> list[Trigger]:
        return [Trigger(**d) for d in self._read(instance_id)]

    async def get_due(self, instance_id: str, now_iso: str) -> list[Trigger]:
        """Get active triggers where next_fire_at <= now.

        Defensive: skip one-shot triggers that already fired (fire_count > 0, no recurrence).
        """
        results = []
        for d in self._read(instance_id):
            if d.get("status") != "active":
                continue
            # Defensive: skip one-shot triggers that already fired but weren't marked completed
            if d.get("fire_count", 0) > 0 and not d.get("recurrence"):
                continue
            nfa = d.get("next_fire_at", "")
            if not nfa:
                continue
            try:
                # Handle both "2026-03-22 12:03" and "2026-03-22T12:03:00" formats
                nfa_dt = datetime.fromisoformat(nfa.replace(" ", "T"))
                now_dt = datetime.fromisoformat(now_iso.replace(" ", "T"))
                # Tz-normalize before comparing: an agent-supplied `when` often
                # omits a timezone (naive), while `now` is UTC-aware. Comparing
                # the two raised "can't compare offset-naive and offset-aware
                # datetimes" EVERY tick, killing the whole trigger-eval pass so
                # no scheduled reminder could fire (v1 self-test bug #8). Treat
                # a naive datetime as UTC.
                if nfa_dt.tzinfo is None:
                    nfa_dt = nfa_dt.replace(tzinfo=timezone.utc)
                if now_dt.tzinfo is None:
                    now_dt = now_dt.replace(tzinfo=timezone.utc)
                if nfa_dt <= now_dt:
                    results.append(Trigger(**d))
            except (ValueError, TypeError):
                # A single malformed/odd trigger must never abort the whole
                # evaluation pass for all other triggers.
                continue
        return results

    async def get_by_condition_type(
        self, instance_id: str, condition_type: str, status: str = "active"
    ) -> list[Trigger]:
        """Get all triggers of a specific condition type."""
        all_triggers = await self.list_all(instance_id)
        return [
            t for t in all_triggers
            if t.condition_type == condition_type and t.status == status
        ]

    async def remove(self, instance_id: str, trigger_id: str) -> bool:
        raw = self._read(instance_id)
        new = [d for d in raw if d.get("trigger_id") != trigger_id]
        if len(new) == len(raw):
            return False
        self._write(instance_id, new)
        return True


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def compute_next_fire(recurrence: str, after_iso: str, tz_name: str = "") -> str:
    """Compute the next fire time from a cron expression after a given time.

    A cron recurrence expresses LOCAL wall-clock intent — "every morning at 8am"
    means 8am in the user's timezone, not 8am UTC. When ``tz_name`` is given,
    anchor the cron in that zone (croniter is DST-aware on a tz-aware base) and
    convert the result back to UTC for storage. Without a tz the cron is
    evaluated against the (UTC) ``after`` as before — behavior unchanged.

    Returns a UTC ISO datetime string, or "" if computation fails.
    """
    try:
        from croniter import croniter
        after = datetime.fromisoformat(after_iso)
        zone = None
        if tz_name:
            try:
                from zoneinfo import ZoneInfo
                zone = ZoneInfo(tz_name)
            except Exception:
                zone = None
        if zone is not None:
            if after.tzinfo is None:
                after = after.replace(tzinfo=timezone.utc)
            after = after.astimezone(zone)        # anchor cron in local zone
        nxt = croniter(recurrence, after).get_next(datetime)
        if zone is not None:
            nxt = nxt.astimezone(timezone.utc)    # store UTC
        return nxt.isoformat()
    except Exception as exc:
        logger.warning("TRIGGER: cron computation failed for %r: %s", recurrence, exc)
        return ""


# ---------------------------------------------------------------------------
# manage_schedule tool definition
# ---------------------------------------------------------------------------

_SCHEDULE_ACTION_SYNONYMS = {
    "create_reminder": "create", "add_reminder": "create",
    "set_reminder": "create", "add": "create", "new": "create",
    "set": "create", "schedule": "create", "remind": "create",
    "reminder": "create",
    "delete": "remove", "cancel": "remove", "clear": "remove",
    "show": "list", "get": "list", "ls": "list",
}
_SCHEDULE_TEXT_FIELDS = (
    "description", "message", "text", "reminder", "reminder_text", "content",
    "what", "task", "title", "note", "body", "summary", "label",
)
# The model frequently splits the WHEN into its own field while putting only the
# message in a text field above (live-observed shapes: ["text","scheduled_time",
# "timezone","when"], ["reminder_text","time_offset"], ...). The extraction model
# needs the time IN the description, so we fold any of these back in — otherwise a
# "remind me in 2 hours" lands as a time-less description and extraction returns
# empty `when` ("I couldn't determine when to schedule that").
_SCHEDULE_TIME_FIELDS = (
    "natural_language_request", "when", "scheduled_time", "schedule_time",
    "time", "datetime", "date", "time_offset", "offset", "schedule",
    "recurrence", "cron",
)
# Supplemental only — a timezone alone is ambient metadata, NOT a reason to
# schedule. Folded into the description only when real text/time content exists,
# so a call carrying just a timezone still defaults to `list` (Codex P2).
_SCHEDULE_TZ_FIELDS = ("timezone", "tz")


def normalize_schedule_input(tool_input: dict) -> tuple[str, str]:
    """Return (canonical_action, description) for a manage_schedule call.

    Forgiving dispatch (v1 self-test Test 6, 2026-06-08): the model reached for
    invalid actions ('create_reminder') and put the reminder text in fields
    other than 'description', so a legitimate "remind me…" bounced. Map common
    action synonyms to the canonical enum and resolve the schedule text from any
    field the model naturally emits. Helps EVERY scheduling call.
    """
    si = tool_input or {}
    _VALID_ACTIONS = {"list", "create", "update", "pause", "resume", "remove"}
    raw_action = str(si.get("action") or "").strip().lower()
    action = _SCHEDULE_ACTION_SYNONYMS.get(raw_action, raw_action)
    if action not in _VALID_ACTIONS:
        # The model sometimes uses `type` for the action ("type":"reminder"),
        # but more often for the trigger KIND ("notify"/"time"). Only honor it
        # when it resolves to a real action; otherwise fall through to inference.
        raw_type = str(si.get("type") or "").strip().lower()
        mapped_type = _SCHEDULE_ACTION_SYNONYMS.get(raw_type, raw_type)
        if mapped_type in _VALID_ACTIONS:
            action = mapped_type
        else:
            action = ""
    desc = ""
    for f in _SCHEDULE_TEXT_FIELDS:
        v = si.get(f)
        if v:
            desc = str(v)
            break
    # Fold any time/schedule hint the model parked in a separate field back into
    # the description (skip ones already echoed in the text). Without this the
    # extraction model never sees the WHEN and bounces the create.
    hints = []
    for f in _SCHEDULE_TIME_FIELDS:
        v = si.get(f)
        if v is None:
            continue
        sv = str(v).strip()
        if sv and sv.lower() not in desc.lower():
            hints.append(sv)
    # Timezone is supplemental — attach it only when we already have substance
    # (real text or a real time hint), never let it alone imply a schedule.
    if desc or hints:
        for f in _SCHEDULE_TZ_FIELDS:
            v = si.get(f)
            if v is None:
                continue
            sv = str(v).strip()
            if sv and sv.lower() not in desc.lower():
                hints.append(sv)
    if hints:
        joined = " ".join(hints)
        desc = f"{desc} ({joined})".strip() if desc else joined
    if not action:
        # No action supplied. The create_reminder/reminder.create tool aliases
        # rewrite the NAME to manage_schedule but carry no action, so without
        # this a "remind me…" with reminder text would default to 'list' and be
        # ignored (Codex review). Infer create-intent when there's schedule
        # text; otherwise list.
        action = "create" if desc.strip() else "list"
    return action, desc


MANAGE_SCHEDULE_TOOL = {
    "name": "manage_schedule",
    "description": (
        "Manage scheduled actions — both time-based reminders AND event-based monitoring. "
        "Use 'create' with a natural language description. "
        "Time-based: 'Remind me to send the estimate on Friday at 9am', "
        "'Every morning at 8am tell me what is on my calendar today', "
        "'In 2 hours send me a message saying time to stretch'. "
        "Event-based monitoring: 'Let me know 30 minutes before any calendar event', "
        "'Remind me about the dentist appointment', 'Alert me 15 minutes before meetings'. "
        "Currently supported event sources: calendar. "
        "Use 'list' to see all scheduled items. "
        "Use 'pause', 'resume', or 'remove' to manage existing schedules."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "create", "update", "pause", "resume", "remove"],
                "description": "The action to perform.",
            },
            "trigger_id": {
                "type": "string",
                "description": "Trigger ID (required for update/pause/resume/remove).",
            },
            "description": {
                "type": "string",
                "description": (
                    "What to schedule, in natural language. Include the time and what "
                    "should happen. Examples: 'Remind me to follow up with the contractor on "
                    "Friday at 9am', 'Every morning at 8am tell me what is on my calendar today', "
                    "'In 2 hours send me a message saying time to stretch'"
                ),
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


# Structured output schema for Haiku extraction of schedule parameters
_SCHEDULE_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action_type": {
            "type": "string",
            "enum": ["notify", "tool_call"],
            "description": "notify = send a message, tool_call = execute a tool",
        },
        "when": {
            "type": "string",
            "description": "ISO 8601 datetime with T separator and seconds (e.g. 2026-03-22T09:00:00)",
        },
        "message": {
            "type": "string",
            "description": "The notification message text (for notify type)",
        },
        "recurrence": {
            "type": "string",
            "description": (
                "For time triggers: cron expression for recurring, or empty for one-shot. "
                "For event triggers: 'standing' if ongoing monitoring, or empty for one-shot."
            ),
        },
        "delivery_class": {
            "type": "string",
            "enum": ["ambient", "stage", "interrupt"],
            "description": "Urgency: ambient (low), stage (normal), interrupt (push now)",
        },
        "notify_via": {
            "type": "string",
            "description": "Channel name (discord, sms) or empty for default",
        },
        "tool_name": {
            "type": "string",
            "description": "Tool to call (for tool_call type only)",
        },
        "tool_args": {
            "type": "string",
            "description": "JSON string of tool arguments (for tool_call type only)",
        },
        "condition_type": {
            "type": "string",
            "enum": ["time", "event"],
            "description": (
                "Use 'time' for reminders at specific times/dates. "
                "Use 'event' for calendar-based triggers like "
                "'let me know before meetings' or 'remind me about my dentist'."
            ),
        },
        "event_source": {
            "type": "string",
            "enum": ["calendar", ""],
            "description": "Event source. 'calendar' for event triggers, empty string for time triggers.",
        },
        "event_filter": {
            "type": "string",
            "description": "Keyword to match event titles. Empty = all events. Empty for time triggers.",
        },
        "event_lead_minutes": {
            "type": "integer",
            "description": "Minutes before the event to fire (default 30). 0 for time triggers.",
        },
    },
    "required": ["action_type", "when", "message", "recurrence",
                 "delivery_class", "notify_via", "tool_name", "tool_args",
                 "condition_type", "event_source", "event_filter",
                 "event_lead_minutes"],
    "additionalProperties": False,
}


async def _extract_schedule_params(
    reasoning_service, description: str, user_timezone: str = "",
) -> dict | str:
    """Use Haiku to extract structured schedule params from NL description.

    Returns a dict on success, or an error string on failure.
    """
    from kernos.utils import utc_now_dt, to_user_local, format_user_datetime
    now_utc = utc_now_dt()
    now_local = to_user_local(now_utc, user_timezone)
    tz_display = user_timezone or "system local"
    local_time = now_local.strftime('%A, %B %d, %Y %I:%M %p')
    utc_time = now_utc.strftime('%Y-%m-%d %H:%M')

    try:
        result = await reasoning_service.complete_simple(
            system_prompt=(
                "You are extracting schedule data from a natural language description. "
                f"Current time: {local_time} ({tz_display}) / {utc_time} UTC\n\n"
                "Extract:\n"
                "- action_type: 'notify' (send a message) or 'tool_call' (execute a tool)\n"
                "- when: ISO 8601 datetime with T separator and seconds in LOCAL time "
                "(e.g., 2026-03-22T09:00:00, NOT 2026-03-22 09:00). No timezone offset. "
                "Convert relative times ('in 2 hours', 'tomorrow 9am') to absolute datetimes.\n"
                "- message: The notification message text\n"
                "- recurrence: empty string for one-shot, or cron expression for recurring "
                "(e.g., '0 8 * * *' for daily 8am, '0 8 * * 1' for Monday 8am)\n"
                "- delivery_class: 'stage' (default normal), 'ambient' (low), 'interrupt' (urgent)\n"
                "- notify_via: empty string for default channel, or 'discord' or 'sms'\n"
                "- tool_name: empty for notify, tool name for tool_call\n"
                "- tool_args: empty for notify, JSON string of args for tool_call\n"
                "- condition_type: 'time' for reminders at specific times, 'event' for "
                "calendar-based triggers\n"
                "- event_source: 'calendar' for event triggers, empty for time triggers\n"
                "- event_filter: keyword to match event titles (empty = all events), "
                "empty for time triggers\n"
                "- event_lead_minutes: minutes before event to fire (default 30), "
                "0 for time triggers\n\n"
                "For calendar-based requests like 'let me know before meetings', "
                "'remind me about the dentist', or 'alert me 15 minutes before events':\n"
                "  - condition_type: 'event'\n"
                "  - event_source: 'calendar'\n"
                "  - event_filter: keyword if specific ('dentist', 'inspection'), "
                "empty string if all events\n"
                "  - event_lead_minutes: requested lead time (default 30)\n"
                "  - recurrence: 'standing' if ongoing ('before any meeting'), "
                "empty if one-shot ('about the dentist appointment')\n"
                "  - when: empty string (event triggers poll, not fire at a time)\n\n"
                "Respond with ONLY a JSON object."
            ),
            user_content=f"Description: {description}",
            output_schema=_SCHEDULE_EXTRACTION_SCHEMA,
            max_tokens=512,
            prefer_cheap=True,
        )
        import json
        parsed = json.loads(result)

        # Event triggers poll (no 'when'); recurring triggers derive next_fire
        # from the cron (no 'when' needed either).
        is_event = parsed.get("condition_type") == "event"
        is_recurring = bool(parsed.get("recurrence"))
        if not parsed.get("when") and not is_event and not is_recurring:
            return "I couldn't determine when to schedule that. Can you be more specific about the time?"

        # Normalize 'when' to ISO 8601 with T separator — Haiku sometimes produces
        # "2026-03-22 12:03" (space separator) which breaks string comparison with
        # utc_now() output that uses T separator.
        #
        # CRITICAL: the extraction prompt asks for `when` in LOCAL time with no
        # offset (a naive wall-clock string like "2026-06-08T11:58:00"). But the
        # trigger evaluator (`get_due`) treats a naive `next_fire_at` as UTC. So a
        # reminder for "11:58 AM Pacific" (~1h out) was stored naive, re-read as
        # 11:58 UTC (~7h in the PAST), and fired+completed instantly — vanishing
        # from `list` the moment it was created (v1 self-test Test 6). Convert the
        # local wall-clock to UTC here, at the single extraction seam, so the
        # whole downstream pipeline stores and compares one consistent zone.
        raw_when = parsed.get("when", "")
        if raw_when:
            try:
                when_dt = datetime.fromisoformat(raw_when.replace(" ", "T"))
                if when_dt.tzinfo is None:
                    # Localize the naive local wall-clock → UTC. Prefer the user's
                    # IANA zone, but FALL BACK to the server's local zone when the
                    # profile timezone is missing OR invalid (the lazy tz-discovery
                    # can store a non-IANA abbreviation like "PDT", which ZoneInfo
                    # rejects). Without a working fallback the time stays naive and
                    # get_due misreads it as UTC → the reminder fires instantly
                    # (live-observed 2026-06-08: stored 17:01 'local' read as 17:01
                    # UTC, ~7h in the past).
                    zone = None
                    if user_timezone:
                        try:
                            from zoneinfo import ZoneInfo
                            zone = ZoneInfo(user_timezone)
                        except Exception:
                            zone = None
                    if zone is not None:
                        # ZoneInfo applies the correct DST offset for the target date.
                        when_dt = when_dt.replace(tzinfo=zone)
                    else:
                        # System local, DST-aware for the TARGET date: astimezone()
                        # on a naive datetime treats it as local and uses that date's
                        # offset (a fixed current-offset tzinfo would mis-convert a
                        # reminder scheduled across a DST boundary — Codex P2).
                        when_dt = when_dt.astimezone()
                parsed["when"] = when_dt.astimezone(timezone.utc).isoformat()
            except ValueError:
                if not is_event:
                    return "I couldn't parse that time. Can you be more specific?"
                parsed["when"] = ""

        return parsed

    except Exception as exc:
        logger.warning("TRIGGER: schedule extraction failed: %s", exc)
        return "I couldn't parse that schedule request. Can you be more specific about the time?"


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------


async def handle_manage_schedule(
    trigger_store: TriggerStore,
    instance_id: str,
    member_id: str,
    space_id: str,
    action: str,
    trigger_id: str = "",
    description: str = "",
    reasoning_service=None,
    conversation_id: str = "",
    user_timezone: str = "",
    **kwargs,  # Accept extra fields for backward compat
) -> str:
    """Handle the manage_schedule kernel tool.

    For create/update: description is parsed via Haiku into structured params.
    """
    if action == "list":
        include_inactive = "retired" in description.lower() or "inactive" in description.lower() or "all" in description.lower()
        return await _list_triggers(trigger_store, instance_id, include_inactive=include_inactive, user_timezone=user_timezone)

    # TOOL-ARG-REPAIR-V1 Phase 0: validation rejections return a typed
    # ToolFailure (still the same string the agent sees) so the dispatch
    # boundaries record is_error=True and the plan-spine does not advance
    # over them. All of these fire BEFORE any trigger is written →
    # pre_side_effect=True (safe for a future bounded auto-retry).
    from kernos.kernel.tool_failure import ToolFailure

    def _invalid(msg: str) -> ToolFailure:
        return ToolFailure(
            msg, code="schedule_underspecified", pre_side_effect=True,
        )

    if action == "create":
        if not description:
            return _invalid("Error: 'description' is required — describe what to schedule and when.")
        if not reasoning_service:
            return _invalid("Error: Reasoning service not available for schedule extraction.")
        extracted = await _extract_schedule_params(reasoning_service, description, user_timezone)
        if isinstance(extracted, str):
            return _invalid(extracted)  # Extraction error message
        logger.info("EXTRACTION_RESULT: %s", json.dumps(extracted, default=str))

        # Normalize: event triggers are always notify actions
        condition_type = extracted.get("condition_type", "time")
        if condition_type == "event":
            extracted["action_type"] = "notify"

        return await _create_trigger(
            trigger_store, instance_id, member_id, space_id,
            description,
            extracted.get("when", ""),
            extracted.get("action_type", "notify"),
            extracted.get("message", description),
            extracted.get("tool_name", ""),
            {},  # tool_args parsed separately if needed
            extracted.get("notify_via", ""),
            extracted.get("delivery_class", "stage"),
            extracted.get("recurrence", ""),
            conversation_id=conversation_id,
            condition_type=condition_type,
            event_source=extracted.get("event_source", ""),
            event_filter=extracted.get("event_filter", ""),
            event_lead_minutes=int(extracted.get("event_lead_minutes", 30) or 30),
            user_timezone=user_timezone,
        )

    if action == "pause":
        return await _set_trigger_status(trigger_store, instance_id, trigger_id, "paused")

    if action == "resume":
        return await _set_trigger_status(trigger_store, instance_id, trigger_id, "active")

    if action == "remove":
        if not trigger_id:
            return _invalid("Error: trigger_id is required for remove.")
        removed = await trigger_store.remove(instance_id, trigger_id)
        if removed:
            logger.info("TRIGGER_REMOVE: id=%s instance=%s", trigger_id, instance_id)
            return f"Removed trigger {trigger_id}."
        return f"Error: Trigger '{trigger_id}' not found."

    if action == "update":
        if not description:
            return _invalid("Error: 'description' is required for update.")
        if not reasoning_service:
            return _invalid("Error: Reasoning service not available for schedule extraction.")
        extracted = await _extract_schedule_params(reasoning_service, description, user_timezone)
        if isinstance(extracted, str):
            return _invalid(extracted)
        return await _update_trigger(
            trigger_store, instance_id, trigger_id,
            description,
            extracted.get("when", ""),
            extracted.get("message", ""),
            extracted.get("recurrence", ""),
        )

    return _invalid(
        f"Error: Unknown action '{action}'. Use list, create, update, pause, resume, or remove."
    )


async def _list_triggers(
    store: TriggerStore, instance_id: str, include_inactive: bool = False,
    user_timezone: str = "",
) -> str:
    all_triggers = await store.list_all(instance_id)
    if include_inactive:
        triggers = all_triggers
    else:
        triggers = [t for t in all_triggers if t.status in ("active", "paused")]
    if not triggers:
        return "No scheduled actions."

    # next_fire_at / last_fired_at are stored UTC — render in the user's zone so
    # the listed wall-clock matches the create receipt (and what they asked for).
    lines = ["**Scheduled Actions:**\n"]
    for t in triggers:
        status_icon = {"active": "▶", "paused": "⏸", "completed": "✓", "failed": "✗", "retired": "⊘", "replaced": "↻"}.get(t.status, "?")
        recur = f" (recurring: {t.recurrence})" if t.recurrence else ""
        if t.condition_type == "event":
            source_info = f"source: {t.event_source}"
            filter_info = f" filter: \"{t.event_filter}\"" if t.event_filter else ""
            lead_info = f" lead: {t.event_lead_minutes}min"
            fired_info = f"fires: {t.fire_count}"
            if t.last_fired_at:
                fired_info += f" | last_fired: {_format_fire_local(t.last_fired_at, user_timezone)}"
            lines.append(
                f"  {status_icon} [{t.trigger_id}] {t.action_description}\n"
                f"    event trigger | {source_info}{filter_info}{lead_info} | "
                f"{fired_info}{recur}"
            )
        else:
            fired_info = f"fires: {t.fire_count}"
            if t.last_fired_at:
                fired_info += f" | last_fired: {_format_fire_local(t.last_fired_at, user_timezone)}"
            next_display = _format_fire_local(t.next_fire_at, user_timezone) if t.next_fire_at else "N/A"
            lines.append(
                f"  {status_icon} [{t.trigger_id}] {t.action_description}\n"
                f"    next: {next_display} | "
                f"type: {t.action_type} | {fired_info}{recur}"
            )
    return "\n".join(lines)


def _format_fire_local(fire_iso: str, user_timezone: str) -> str:
    """Render a stored (UTC) next_fire_at as the user's local wall-clock.

    Defensive: an unparseable or naive value falls back to the raw string
    truncated to seconds, so a receipt is never blocked on formatting.
    """
    try:
        dt = datetime.fromisoformat(fire_iso.replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Resolve to the user's zone; fall back to the server's local zone when
        # the profile timezone is missing OR a non-IANA string (e.g. "PDT").
        # This mirrors the extraction-side fallback so the displayed wall-clock
        # matches when the reminder actually fires.
        local = None
        if user_timezone:
            try:
                from zoneinfo import ZoneInfo
                local = dt.astimezone(ZoneInfo(user_timezone))
            except Exception:
                local = None
        if local is None:
            local = dt.astimezone()  # server local
        return local.strftime("%Y-%m-%d %H:%M %Z").strip()
    except (ValueError, TypeError):
        return fire_iso[:19]


async def _create_trigger(
    store: TriggerStore,
    instance_id: str, member_id: str, space_id: str,
    description: str, when: str, action_type: str, message: str,
    tool_name: str, tool_args: dict, notify_via: str,
    delivery_class: str, recurrence: str,
    conversation_id: str = "",
    condition_type: str = "time",
    event_source: str = "",
    event_filter: str = "",
    event_lead_minutes: int = 30,
    user_timezone: str = "",
) -> str:
    if not when and condition_type != "event" and not recurrence:
        # Recurring triggers derive next_fire from the cron, not `when`, so a
        # bare recurrence ("every morning at 8am") is valid without a `when`.
        return ToolFailure("Error: 'when' is required — provide an ISO datetime or cron expression.", code="schedule_underspecified", pre_side_effect=True)
    if not description:
        return ToolFailure("Error: 'description' is required — what should this trigger do?", code="schedule_underspecified", pre_side_effect=True)

    now = utc_now()
    tid = _trigger_id()

    # Event triggers don't use next_fire_at — they poll, not schedule.
    # "standing" recurrence means "stay active" — not a cron expression.
    if condition_type == "event":
        next_fire = ""
    elif recurrence:
        next_fire = compute_next_fire(recurrence, now, tz_name=user_timezone)
        if not next_fire:
            return ToolFailure(f"Error: Could not parse recurrence '{recurrence}' as a cron expression.", code="schedule_underspecified", pre_side_effect=True)
    else:
        next_fire = when  # Agent provides ISO datetime

    params: dict = {}
    if action_type == "notify":
        params["message"] = message or description
    elif action_type == "tool_call":
        if not tool_name:
            return ToolFailure("Error: 'tool_name' is required for tool_call action type.", code="schedule_underspecified", pre_side_effect=True)
        params["tool_name"] = tool_name
        params["tool_args"] = tool_args

    trigger = Trigger(
        trigger_id=tid,
        instance_id=instance_id,
        member_id=member_id,
        space_id=space_id,
        conversation_id=conversation_id,
        condition_type=condition_type,
        condition=when if condition_type == "time" else "",
        next_fire_at=next_fire,
        recurrence=recurrence,
        action_type=action_type,
        action_description=description,
        action_params=params,
        notify_via=notify_via,
        delivery_class=delivery_class or "stage",
        status="active",
        created_at=now,
        timezone=user_timezone,
        event_source=event_source if condition_type == "event" else "",
        event_filter=event_filter if condition_type == "event" else "",
        event_lead_minutes=event_lead_minutes if condition_type == "event" else 30,
    )

    # Fix 1: Supersede existing standing event triggers with same source+filter.
    # Ignore notify_via — latest preference wins regardless of channel.
    replaced_descriptions: list[str] = []
    if condition_type == "event" and recurrence == "standing":
        existing = await store.list_active(instance_id)
        for old in existing:
            if (old.condition_type == "event"
                    and old.event_source == event_source
                    and old.event_filter == event_filter
                    and old.recurrence == "standing"
                    and old.status == "active"):
                old.status = "replaced"
                old.replaced_by = tid
                await store.save(old)
                replaced_descriptions.append(
                    f"{old.event_lead_minutes}min (id={old.trigger_id})"
                )
                logger.info(
                    "TRIGGER_REPLACED: old=%s new=%s reason=preference_update",
                    old.trigger_id, tid,
                )

    await store.save(trigger)
    logger.info(
        "TRIGGER_CREATE: id=%s desc=%r condition=%s next=%s action=%s recurrence=%r"
        " event_source=%s event_filter=%r event_lead=%d replaced=%d",
        tid, description, condition_type,
        next_fire[:19] if next_fire else "?", action_type, recurrence,
        trigger.event_source, trigger.event_filter, trigger.event_lead_minutes,
        len(replaced_descriptions),
    )

    if condition_type == "event":
        recur_note = f" Standing: {recurrence}." if recurrence else ""
        replaced_note = ""
        if replaced_descriptions:
            replaced_note = f"\nReplaced {len(replaced_descriptions)} previous reminder(s): {', '.join(replaced_descriptions)}"
        return (
            f"Scheduled: {description}\n"
            f"Event trigger: {trigger.event_source} | "
            f"Lead: {trigger.event_lead_minutes}min | "
            f"Filter: {trigger.event_filter or '(all events)'}\n"
            f"ID: {tid}{recur_note}{replaced_note}"
        )
    recur_note = f" Recurring: {recurrence}." if recurrence else ""
    # Display the next fire in the user's local zone — `next_fire` is stored UTC
    # (extraction now converts local→UTC), so a raw print would show UTC and the
    # agent would echo the wrong wall-clock back to the user.
    next_fire_display = _format_fire_local(next_fire, user_timezone) if next_fire else when
    return (
        f"Scheduled: {description}\n"
        f"Next fire: {next_fire_display}\n"
        f"Type: {action_type} | ID: {tid}{recur_note}"
    )


async def _set_trigger_status(
    store: TriggerStore, instance_id: str, trigger_id: str, new_status: str,
) -> str:
    if not trigger_id:
        return f"Error: trigger_id is required for {new_status}."
    trigger = await store.get(instance_id, trigger_id)
    if not trigger:
        return f"Error: Trigger '{trigger_id}' not found."
    trigger.status = new_status
    await store.save(trigger)
    logger.info("TRIGGER_STATUS: id=%s status=%s", trigger_id, new_status)
    return f"Trigger {trigger_id} is now {new_status}."


async def _update_trigger(
    store: TriggerStore, instance_id: str, trigger_id: str,
    description: str, when: str, message: str, recurrence: str,
) -> str:
    if not trigger_id:
        return "Error: trigger_id is required for update."
    trigger = await store.get(instance_id, trigger_id)
    if not trigger:
        return f"Error: Trigger '{trigger_id}' not found."

    if description:
        trigger.action_description = description
    if when:
        trigger.condition = when
        if recurrence:
            trigger.recurrence = recurrence
            trigger.next_fire_at = compute_next_fire(recurrence, utc_now(), tz_name=trigger.timezone)
        else:
            trigger.next_fire_at = when
    if message and trigger.action_type == "notify":
        trigger.action_params["message"] = message
    if recurrence and not when:
        trigger.recurrence = recurrence
        trigger.next_fire_at = compute_next_fire(recurrence, utc_now(), tz_name=trigger.timezone)

    await store.save(trigger)
    logger.info("TRIGGER_UPDATE: id=%s desc=%r next=%s", trigger_id, trigger.action_description, trigger.next_fire_at[:19] if trigger.next_fire_at else "?")
    return f"Updated trigger {trigger_id}."


# ---------------------------------------------------------------------------
# Trigger evaluation — called from the tick loop
# ---------------------------------------------------------------------------


async def evaluate_triggers(
    trigger_store: TriggerStore,
    instance_id: str,
    handler,  # MessageHandler — for send_outbound and tool execution
    proactive_budget_check=None,  # Optional: () -> bool, gates outbound delivery
) -> int:
    """Evaluate and fire all due triggers. Returns count of triggers fired."""
    now = utc_now()
    due = await trigger_store.get_due(instance_id, now)
    fired = 0

    for trigger in due:
        # Proactive budget check — defer if over budget
        if proactive_budget_check and not proactive_budget_check("scheduler"):
            break  # Defer remaining triggers to next tick

        try:
            success = await _fire_trigger(trigger, handler)

            # If _fire_trigger already retired the trigger, just save and continue
            if trigger.status == "retired":
                await trigger_store.save(trigger)
                continue

            trigger.last_fired_at = now
            trigger.fire_count += 1

            if success:
                # Recovery from degraded state
                if trigger.degraded:
                    logger.info(
                        "TRIGGER_RECOVERED: id=%s was_degraded_for=%d failures desc=%r",
                        trigger.trigger_id, trigger.transient_failure_count,
                        trigger.action_description,
                    )
                    trigger.degraded = False
                    trigger.transient_failure_count = 0
                    trigger.failure_class = ""
                    # Keep failure_reason for debugging history

                if trigger.recurrence:
                    trigger.next_fire_at = compute_next_fire(trigger.recurrence, now, tz_name=trigger.timezone)
                    if not trigger.next_fire_at:
                        trigger.status = "completed"
                else:
                    trigger.status = "completed"
            else:
                # _fire_trigger may have applied transient failure state already
                if not trigger.recurrence and trigger.failure_class != "transient":
                    trigger.status = "failed"

            await trigger_store.save(trigger)
            fired += 1

            logger.info(
                "TRIGGER_FIRE: id=%s action=%s status=%s desc=%r",
                trigger.trigger_id, trigger.action_type,
                "success" if success else "failed",
                trigger.action_description,
            )
        except Exception as exc:
            fc = classify_trigger_failure(exc)
            trigger.failure_reason = str(exc)
            trigger.failure_class = fc
            trigger.last_failure_at = utc_now()
            if fc == "structural":
                trigger.status = "retired"
                trigger.retired_at = utc_now()
                logger.info(
                    "TRIGGER_RETIRED: id=%s reason=structural error=%s",
                    trigger.trigger_id, exc,
                )
                _notify_retirement(handler, trigger)
            else:
                _apply_transient_failure(trigger, handler)
            await trigger_store.save(trigger)
            logger.error(
                "TRIGGER_FIRE: id=%s action=%s EXCEPTION: %s",
                trigger.trigger_id, trigger.action_type, exc,
            )

    return fired


async def _store_scheduled_message(handler, trigger: Trigger, content: str) -> None:
    """Inject a [SCHEDULED] message into conversation history so the agent has context."""
    if not trigger.conversation_id or not hasattr(handler, "conversations"):
        return
    try:
        entry = {
            "role": "assistant",
            "content": content,
            "timestamp": utc_now(),
            "platform": "scheduler",
            "instance_id": trigger.instance_id,
            "conversation_id": trigger.conversation_id,
            "space_tags": [trigger.space_id] if trigger.space_id else None,
        }
        await handler.conversations.append(
            trigger.instance_id, trigger.conversation_id, entry,
        )
        logger.info(
            "TRIGGER_HISTORY: stored [SCHEDULED] message for trigger=%s in conv=%s",
            trigger.trigger_id, trigger.conversation_id,
        )
        # Write to per-space conversation log
        if hasattr(handler, "conv_logger") and trigger.space_id:
            # Strip [SCHEDULED] prefix for cleaner log
            log_content = content.removeprefix("[SCHEDULED] ")
            await handler.conv_logger.append(
                instance_id=trigger.instance_id,
                space_id=trigger.space_id,
                speaker="assistant",
                channel="scheduled",
                content=log_content,
                member_id=trigger.member_id,
            )
    except Exception as exc:
        logger.warning(
            "TRIGGER_HISTORY: failed to store message for trigger=%s: %s",
            trigger.trigger_id, exc,
        )


async def _write_receipt(
    handler, trigger: Trigger, outcome: str,
    event_summary: str = "", channel: str = "", error: str = "",
) -> None:
    """Write a structured [RECEIPT] entry to the conversation log."""
    if not hasattr(handler, "conv_logger") or not trigger.space_id:
        return
    try:
        parts = [
            f"[RECEIPT] trigger_fired | {trigger.trigger_id}",
            f"| {trigger.action_description}",
        ]
        if event_summary:
            parts.append(f"| event={event_summary}")
        parts.append(f"| channel={channel or 'default'}")
        parts.append(f"| outcome={outcome}")
        if error:
            parts.append(f"| error={error}")
        parts.append(f"| fire_count={trigger.fire_count}")
        parts.append(f"| timestamp={utc_now()}")
        receipt_content = " ".join(parts)

        await handler.conv_logger.append(
            instance_id=trigger.instance_id,
            space_id=trigger.space_id,
            speaker="system",
            channel="receipt",
            content=receipt_content,
            member_id=trigger.member_id,
        )
    except Exception as exc:
        logger.warning("RECEIPT_WRITE_FAILED: trigger=%s error=%s", trigger.trigger_id, exc)


async def _fire_trigger(trigger: Trigger, handler) -> bool:
    """Execute a trigger's action. Returns True on success."""
    if trigger.action_type == "notify":
        message = trigger.action_params.get("message", trigger.action_description)
        member_id = trigger.member_id or resolve_owner_member_id(trigger.instance_id)
        channel = trigger.notify_via or None
        success = await handler.send_outbound(
            trigger.instance_id, member_id, channel, message,
        )
        if success:
            # Inject into conversation history so the agent knows what it sent
            await _store_scheduled_message(
                handler, trigger, f"[SCHEDULED] {message}",
            )
            # Execution receipt
            await _write_receipt(
                handler, trigger, outcome="success",
                channel=trigger.notify_via or "default",
            )
        else:
            # Hold for delivery on next user message
            trigger.pending_delivery = message
            await _write_receipt(
                handler, trigger, outcome="failed",
                channel=trigger.notify_via or "default",
                error="outbound_delivery_failed",
            )
            logger.warning(
                "TRIGGER_DELIVERY_PENDING: id=%s reason=outbound_failed",
                trigger.trigger_id,
            )
        return success

    elif trigger.action_type == "tool_call":
        tool_name = trigger.action_params.get("tool_name", "")
        tool_args = trigger.action_params.get("tool_args", {})

        if not tool_name:
            trigger.failure_reason = "No tool_name in action_params"
            return False

        try:
            from kernos.kernel.reasoning import ReasoningRequest
            request = ReasoningRequest(
                instance_id=trigger.instance_id,
                conversation_id=f"trigger_{trigger.trigger_id}",
                system_prompt="",
                messages=[],
                tools=[],
                model="",
                trigger="scheduler",
                active_space_id=trigger.space_id,
                is_reactive=False,
            )
            result = await handler.reasoning.execute_tool(tool_name, tool_args, request)

            # Classify the result for potential tool failures
            fc = classify_trigger_failure(result)
            if fc == "structural":
                trigger.failure_reason = f"Tool permanently unavailable: {result}"
                trigger.failure_class = "structural"
                trigger.status = "retired"
                trigger.retired_at = utc_now()
                logger.info(
                    "TRIGGER_RETIRED: id=%s reason=structural tool=%s",
                    trigger.trigger_id, tool_name,
                )
                _notify_retirement(handler, trigger)
                return False

            # Deliver result to user
            member_id = trigger.member_id or resolve_owner_member_id(trigger.instance_id)
            delivery_msg = f"Scheduled action completed: {trigger.action_description}\n\nResult: {result}"
            channel = trigger.notify_via or None
            success = await handler.send_outbound(
                trigger.instance_id, member_id, channel, delivery_msg,
            )
            if success:
                await _store_scheduled_message(
                    handler, trigger, f"[SCHEDULED] {delivery_msg}",
                )
            else:
                trigger.pending_delivery = delivery_msg
                logger.warning(
                    "TRIGGER_DELIVERY_PENDING: id=%s reason=outbound_failed",
                    trigger.trigger_id,
                )
            return True  # Tool call succeeded even if delivery pending

        except Exception as exc:
            fc = classify_trigger_failure(exc)
            trigger.failure_reason = str(exc)
            trigger.failure_class = fc
            trigger.last_failure_at = utc_now()
            if fc == "structural":
                trigger.status = "retired"
                trigger.retired_at = utc_now()
                logger.info(
                    "TRIGGER_RETIRED: id=%s reason=structural error=%s",
                    trigger.trigger_id, exc,
                )
                _notify_retirement(handler, trigger)
            else:
                _apply_transient_failure(trigger, handler)
            return False

    trigger.failure_reason = f"Unknown action_type: {trigger.action_type}"
    return False


def _notify_retirement(handler, trigger: Trigger) -> None:
    """Queue a system event for trigger retirement."""
    try:
        handler.queue_system_event(
            trigger.instance_id,
            f'[SYSTEM] trigger_retired: "{trigger.action_description}" — '
            f'{trigger.failure_reason}. Recreate under event triggers if needed.',
        )
    except Exception:
        pass


def _apply_transient_failure(trigger: Trigger, handler) -> None:
    """Update trigger state for a transient failure."""
    trigger.transient_failure_count += 1
    trigger.last_failure_at = utc_now()
    trigger.failure_class = "transient"

    if not trigger.degraded:
        trigger.degraded = True
        logger.info(
            "TRIGGER_DEGRADED: id=%s reason=%s",
            trigger.trigger_id, trigger.failure_reason,
        )

    if trigger.transient_failure_count == TRANSIENT_FAILURE_NOTIFY_THRESHOLD:
        logger.info(
            "TRIGGER_DEGRADED_NOTIFY: id=%s count=%d",
            trigger.trigger_id, trigger.transient_failure_count,
        )
        try:
            handler.queue_system_event(
                trigger.instance_id,
                f'[SYSTEM] trigger_degraded: "{trigger.action_description}" — '
                f'{trigger.failure_reason}. Service may need reconnection.',
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Boot scan — retire stale legacy triggers
# ---------------------------------------------------------------------------


async def retire_stale_triggers(
    trigger_store: TriggerStore,
    instance_id: str,
    registry,  # CapabilityRegistry — public API
    handler,
) -> int:
    """Retire active triggers whose tools no longer exist."""
    all_triggers = await trigger_store.list_all(instance_id)
    retired = 0

    for trigger in all_triggers:
        if trigger.status != "active":
            continue
        if trigger.action_type == "tool_call":
            tool_name = trigger.action_params.get("tool_name", "")
            if not tool_name:
                continue
            # Check via public registry
            tool_exists = registry.get_tool_schema(tool_name) is not None
            if not tool_exists:
                trigger.status = "retired"
                trigger.failure_class = "structural"
                trigger.failure_reason = f"Tool '{tool_name}' no longer exists"
                trigger.retired_at = utc_now()
                await trigger_store.save(trigger)
                retired += 1
                # Queue system event (agent sees on next message)
                try:
                    handler.queue_system_event(
                        trigger.instance_id,
                        f'[SYSTEM] trigger_retired: "{trigger.action_description}" — '
                        f'{trigger.failure_reason}. Recreate if needed.',
                    )
                except Exception:
                    pass
                logger.info(
                    "TRIGGER_RETIRED: id=%s reason=tool_not_found tool=%s",
                    trigger.trigger_id, tool_name,
                )

    if retired:
        logger.info("STALE_TRIGGERS_RETIRED: instance=%s count=%d", instance_id, retired)
    return retired


# ---------------------------------------------------------------------------
# Calendar event type + parser (Component 1)
# ---------------------------------------------------------------------------


@dataclass
class CalendarEvent:
    """Normalized calendar event from MCP list-events response."""

    id: str
    summary: str
    start: datetime          # Parsed datetime, timezone-aware
    end: datetime | None
    location: str = ""
    is_all_day: bool = False  # True if date-only (no dateTime)


def parse_calendar_events(raw_result: str) -> list[CalendarEvent]:
    """Parse MCP list-events response into structured CalendarEvent objects.

    Returns only timed events. All-day events are SKIPPED in v1 —
    "N minutes before" has no meaning for all-day events without a
    chosen policy.
    """
    try:
        data = json.loads(raw_result)
        items = data if isinstance(data, list) else data.get("items", data.get("events", []))
    except (json.JSONDecodeError, TypeError):
        return []

    events: list[CalendarEvent] = []
    for item in items:
        start_raw = item.get("start", {})

        # Skip all-day events (explicit v1 policy)
        if "date" in start_raw and "dateTime" not in start_raw:
            continue

        dt_str = start_raw.get("dateTime")
        if not dt_str:
            continue

        try:
            start_dt = datetime.fromisoformat(dt_str)
        except (ValueError, TypeError):
            continue

        end_dt = None
        end_raw = item.get("end", {})
        if end_raw.get("dateTime"):
            try:
                end_dt = datetime.fromisoformat(end_raw["dateTime"])
            except (ValueError, TypeError):
                pass

        events.append(CalendarEvent(
            id=item.get("id", ""),
            summary=item.get("summary", "Calendar event"),
            start=start_dt,
            end=end_dt,
            location=item.get("location", ""),
            is_all_day=False,
        ))

    return events


# ---------------------------------------------------------------------------
# Event trigger evaluation (Component 4)
# ---------------------------------------------------------------------------

EVENT_POLL_INTERVAL_SECONDS = int(os.getenv("KERNOS_EVENT_POLL_INTERVAL", "60"))
EVENT_DAILY_FIRE_CAP = int(os.getenv("KERNOS_EVENT_DAILY_CAP", "15"))


async def _poll_calendar_events(mcp_client, user_timezone: str = "") -> list[CalendarEvent] | None:
    """Poll calendar once and return parsed events. Returns None on error."""
    # MCP expects LOCAL time, no timezone offset, no microseconds: '2026-01-01T00:00:00'
    from kernos.utils import utc_now_dt, to_user_local
    now_utc = utc_now_dt()
    now_local = to_user_local(now_utc, user_timezone)
    window_end_local = now_local + timedelta(hours=24)
    time_fmt = "%Y-%m-%dT%H:%M:%S"
    poll_args = {
        "account": "normal",
        "calendarId": "primary",
        "timeMin": now_local.strftime(time_fmt),
        "timeMax": window_end_local.strftime(time_fmt),
        "maxResults": 20,
    }
    logger.info("EVENT_CALL: tool=list-events args=%s", json.dumps(poll_args))
    raw_result = await mcp_client.call_tool("list-events", poll_args)

    if raw_result.startswith("Tool error:") or raw_result.startswith("Calendar tool error:") or raw_result.startswith("MCP error"):
        logger.warning("EVENT_CALENDAR_POLL_FAILED: %s", raw_result[:200])
        return None

    events = parse_calendar_events(raw_result)
    logger.info("EVENT_POLL: events_parsed=%d raw_len=%d", len(events), len(raw_result))
    return events


async def evaluate_event_triggers(
    trigger_store: TriggerStore,
    instance_id: str,
    handler,
    mcp_client,   # MCPClientManager — explicit contract
    user_timezone: str = "",
    proactive_budget_check=None,  # Optional: (str) -> bool
) -> tuple[int, int]:
    """Evaluate event-based triggers.

    Returns (count_fired, next_poll_seconds) for adaptive cadence.
    """
    event_triggers = await trigger_store.get_by_condition_type(
        instance_id, "event", status="active"
    )
    if not event_triggers:
        return 0, 15 * 60  # no triggers, 15 min ceiling

    # Group by event_source — poll each source ONCE, share across triggers
    calendar_triggers = [t for t in event_triggers if t.event_source == "calendar"]
    logger.info(
        "EVENT_TICK: instance=%s triggers_found=%d calendar=%d",
        instance_id, len(event_triggers), len(calendar_triggers),
    )

    fired = 0
    next_poll = 15 * 60  # default ceiling
    if calendar_triggers:
        # Single poll for all calendar triggers
        all_events = await _poll_calendar_events(mcp_client, user_timezone)
        if all_events is not None:
            # Pre-filter: separate past, relevant, and far-future events
            now = datetime.now(timezone.utc)
            max_lead = max(t.event_lead_minutes for t in calendar_triggers)
            past_count = 0
            relevant_events: list[CalendarEvent] = []
            for e in all_events:
                mins = (e.start - now).total_seconds() / 60
                if mins < 0:
                    past_count += 1
                elif mins <= max_lead + 5:
                    relevant_events.append(e)
                # else: far-future, skip silently
            if past_count:
                logger.info("EVENT_SKIP_PAST: skipped=%d", past_count)

            for trigger in calendar_triggers:
                try:
                    fired += await _evaluate_calendar_trigger(
                        trigger, trigger_store, handler, relevant_events,
                        user_timezone, proactive_budget_check,
                    )
                except Exception as exc:
                    logger.warning(
                        "EVENT_EVAL_FAILED: trigger=%s source=%s error=%s",
                        trigger.trigger_id, trigger.event_source, exc,
                    )

            # Adaptive cadence: compute next poll based on nearest event
            next_poll = _compute_adaptive_cadence(all_events, calendar_triggers)
        else:
            # MCP error — retry sooner
            next_poll = 60

    return fired, next_poll


def _compute_adaptive_cadence(
    events: list[CalendarEvent], triggers: list[Trigger],
) -> int:
    """Compute seconds until next poll based on nearest upcoming event."""
    now = datetime.now(timezone.utc)
    max_lead = max(t.event_lead_minutes for t in triggers) if triggers else 30

    # Find earliest future event
    future_minutes = []
    for e in events:
        mins = (e.start - now).total_seconds() / 60
        if mins > 0:
            future_minutes.append(mins)

    if not future_minutes:
        return 15 * 60  # nothing upcoming, 15 min ceiling

    earliest = min(future_minutes)

    if earliest > max_lead + 5:
        # Far away — sleep until approaching lead window
        sleep_min = earliest - max_lead - 2
        return max(30, min(int(sleep_min * 60), 5 * 60))  # 30s floor, 5min cap
    elif earliest > 2:
        return 60  # approaching lead window
    else:
        return 30  # imminent, 30s floor


async def _evaluate_calendar_trigger(
    trigger: Trigger,
    trigger_store: TriggerStore,
    handler,
    all_events: list[CalendarEvent],
    user_timezone: str = "",
    proactive_budget_check=None,
) -> int:
    """Evaluate a single calendar event trigger against pre-fetched events."""
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    # Anti-spam: daily cap applies to STANDING triggers only
    is_standing = bool(trigger.recurrence)
    if is_standing:
        if trigger.event_daily_fire_date != today_str:
            trigger.event_daily_fire_count = 0
            trigger.event_daily_fire_date = today_str

        if trigger.event_daily_fire_count >= EVENT_DAILY_FIRE_CAP:
            logger.info(
                "EVENT_CAPPED: trigger=%s fires_today=%d cap=%d",
                trigger.trigger_id, trigger.event_daily_fire_count,
                EVENT_DAILY_FIRE_CAP,
            )
            return 0

    # 1. Filter by event_filter — TITLE/SUMMARY ONLY
    events = list(all_events)
    if trigger.event_filter:
        filter_lower = trigger.event_filter.lower()
        pre_count = len(events)
        events = [e for e in events if filter_lower in e.summary.lower()]
        logger.info(
            "EVENT_FILTER: trigger=%s filter=%r before=%d after=%d",
            trigger.trigger_id, trigger.event_filter, pre_count, len(events),
        )

    # 2. Inline cleanup: prune matched IDs for past/out-of-window events
    # (Past and far-future events already filtered at caller level)
    active_event_ids = {e.id for e in events}
    trigger.event_matched_ids = [
        eid for eid in trigger.event_matched_ids
        if eid in active_event_ids
    ]

    # 4. Check lead time and fire
    fired = 0
    for event in events:
        is_matched = event.id in trigger.event_matched_ids
        minutes_until = (event.start - now).total_seconds() / 60

        if is_matched:
            # Already fired for this event — skip silently
            continue

        if minutes_until > trigger.event_lead_minutes:
            # Outside lead window — not yet time to fire
            logger.info(
                "EVENT_WAIT: trigger=%s event=%s summary=%r "
                "minutes_until=%.1f lead=%d",
                trigger.trigger_id, event.id, event.summary,
                minutes_until, trigger.event_lead_minutes,
            )
            continue

        if minutes_until <= trigger.event_lead_minutes:
            # Build notification message — grammar fix
            from kernos.utils import format_user_time
            try:
                time_str = format_user_time(event.start, user_timezone)
            except (ValueError, Exception):
                time_str = event.start.strftime("%I:%M %p")
            # Use configured lead for display (stable), not live delta (lossy).
            # Fall back to "starting now" if event has already started.
            lead = trigger.event_lead_minutes if minutes_until > 0 else 0
            if lead <= 0:
                time_note = "starting now"
            elif lead == 1:
                time_note = "in 1 minute"
            else:
                time_note = f"in {lead} minutes"
            message = f"Upcoming: {event.summary} at {time_str}"
            if event.location:
                message += f" ({event.location})"
            message += f" — {time_note}"

            # Deliver via send_outbound — canonical member ID
            channel = trigger.notify_via or None
            member_id = resolve_owner_member_id(trigger.instance_id)

            # Proactive budget check
            if proactive_budget_check and not proactive_budget_check("event_trigger"):
                continue  # Defer to next poll

            try:
                await handler.send_outbound(
                    trigger.instance_id, member_id, channel, message,
                )

                # Write to conversation log
                if hasattr(handler, "conv_logger") and trigger.space_id:
                    await handler.conv_logger.append(
                        instance_id=trigger.instance_id,
                        space_id=trigger.space_id,
                        speaker="assistant",
                        channel="scheduled",
                        content=f"[EVENT] {message}",
                        member_id=trigger.member_id,
                    )

                # Execution receipt
                await _write_receipt(
                    handler, trigger, outcome="success",
                    event_summary=event.summary,
                    channel=channel or "default",
                )

                # Recovery from degraded state
                if trigger.degraded:
                    logger.info(
                        "TRIGGER_RECOVERED: id=%s was_degraded_for=%d failures desc=%r",
                        trigger.trigger_id, trigger.transient_failure_count,
                        trigger.action_description,
                    )
                    trigger.degraded = False
                    trigger.transient_failure_count = 0
                    trigger.failure_class = ""

                # Mark as fired
                trigger.event_matched_ids.append(event.id)
                if is_standing:
                    trigger.event_daily_fire_count += 1
                trigger.last_fired_at = utc_now()
                trigger.fire_count += 1
                fired += 1

                logger.info(
                    "EVENT_FIRE: trigger=%s event=%s summary=%r minutes=%d channel=%s",
                    trigger.trigger_id, event.id, event.summary,
                    int(minutes_until), channel or "default",
                )

            except Exception as exc:
                logger.error(
                    "EVENT_FIRE_FAILED: trigger=%s event=%s error=%s",
                    trigger.trigger_id, event.id, exc,
                )
                await _write_receipt(
                    handler, trigger, outcome="failed",
                    event_summary=event.summary,
                    channel=channel or "default",
                    error=str(exc)[:100],
                )

    # 6. Handle one-shot completion
    if fired > 0 and not is_standing:
        trigger.status = "completed"

    # Save trigger state
    await trigger_store.save(trigger)

    return fired
