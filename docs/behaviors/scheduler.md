# Time-Triggered Scheduler

Kernos can execute actions at a specified future time without the user being present. "Remind me to invoice Henderson on Friday at 9am" — the user sets it, walks away, Kernos does it when the time comes.

## How It Works

The scheduler stores **triggers** — persistent records that fire actions at specified times. The awareness evaluator's tick loop evaluates triggers every 60 seconds.

## Trigger Types

**Notify** (always authorized): Sends a message to the user via outbound messaging. No gate confirmation needed — reminders are always OK.

**Tool call** (covenant-authorized): Executes a tool (create calendar event, send email, etc.). Pre-authorized at creation time via the dispatch gate.

## manage_schedule Tool

| Action | Effect | What it does |
|--------|--------|-------------|
| list | read | Show all scheduled actions with status |
| create | soft_write | Schedule a new action (NL description → cheap-model extraction) |
| update | soft_write | Change a trigger's timing or content |
| pause | soft_write | Temporarily stop a trigger from firing |
| resume | soft_write | Re-activate a paused trigger |
| remove | soft_write | Delete a trigger |

Create and update accept a natural language description. The handler uses a cheap-chain model call to extract structured parameters (action_type, when, message, recurrence, delivery_class). The dispatch gate classifies using the **normalized** action (see Forgiving Dispatch below), so an inferred create is correctly gated as a write.

Examples:
- `manage_schedule(action="create", description="Remind me to invoice Henderson on Friday at 9am")`
- `manage_schedule(action="create", description="Every morning at 8am tell me what is on my calendar")`
- `manage_schedule(action="create", description="In 2 hours send me a message saying time to stretch")`

## Forgiving Dispatch (TOOL-ARG-REPAIR-V1)

Models fumble this tool's shape in predictable ways; `normalize_schedule_input` repairs them so the **first** call succeeds:

- **Action synonyms** map to the canonical enum (`create_reminder`/`set_reminder`/`add` → `create`; `cancel`/`delete` → `remove`; `show` → `list`). A `type` value is honored only when it names a real action.
- **Message text** is resolved from any of the field names models actually emit (`description`, `message`, `text`, `reminder_text`, `title`, `note`, …).
- **Time-bearing fields** the model parks separately (`when`, `due_at` via value scanning, `scheduled_time`, `time_offset`, `recurrence`, …) are folded into the extraction description so the extractor sees the WHEN regardless of which key carried it. Timezone fields are supplemental only — a bare timezone never implies a schedule.
- **Create-intent is inferred** when schedule text is present with no action (the `create_reminder` tool-name alias carries no action).
- **Hard boundary:** action text with *no* time signal anywhere returns a clean ask ("couldn't determine when") rather than guessing — see Typed Failures below.

## Time Handling

The extraction model produces `when` as a **local wall-clock** time; the scheduler converts it to **UTC at the extraction seam** and stores tz-aware UTC throughout. (The pre-fix behavior — storing naive local time that the evaluator then read as UTC — made reminders fire hours early and instantly self-complete; live-diagnosed in the v1 self-test.) Resolution order for the local zone: the member's IANA timezone from their profile; a non-IANA or missing profile zone falls back to the server's local zone, DST-aware for the *target* date.

**Recurring triggers** use cron expressions (via `croniter`) and carry the user's IANA zone on the trigger itself (`Trigger.timezone`, captured at creation). Every evaluation — first fire and all reschedules — anchors the cron in that zone and converts the result back to UTC for storage, so "every morning at 8am" means 8am *local*, across DST transitions. A bare recurrence with no `when` is valid (next fire derives from the cron).

User-facing receipts (`create` confirmation and `list`) render fire times back in the user's local zone, so the displayed wall-clock always matches when the trigger actually fires.

## Failure Handling

- **Validation rejections are typed.** Pre-write validation failures (missing description, unparseable time, unknown action) return a `ToolFailure` — a `str` subclass carrying `code="schedule_underspecified"` and `pre_side_effect=True` — so dispatch boundaries record `is_error=True` and autonomous plans don't advance over them, while the agent still sees the same plain-English message.
- **Outbound channel unavailable:** Result held in `pending_delivery` on the trigger. Delivered inline on next user message.
- **Tool call error:** Failures are classified **structural** (trigger can never succeed — tool not found, not registered) vs **transient** (dependency temporarily broken). Transient failures retry with a consecutive-failure counter (`transient_failure_count`); structural failures retire the trigger. The user is notified with error details.
- **MCP disconnected:** Treated as transient; trigger marked `degraded` while the dependency is down.

## Lifecycle

| Status | Meaning |
|--------|---------|
| active | Trigger will fire at next_fire_at |
| paused | Trigger exists but won't fire until resumed |
| completed | One-shot trigger that has fired |
| failed | Trigger that encountered a structural error |
| retired | Permanently stood down after repeated/structural failure |
| replaced | Superseded by a newer standing trigger (`replaced_by` links forward) |

Supporting fields: `fire_count`, `last_fired_at`, `failure_class` (structural/transient), `transient_failure_count`, `degraded`, `retired_at`, `pending_delivery`. Nothing is deleted — lifecycle states preserve history (shadow-archive convention).

## Storage

`data/{instance_id}/state/triggers.json` — atomic writes (tempfile + os.replace).

## Code Locations

| Component | Path |
|-----------|------|
| Trigger, TriggerStore | `kernos/kernel/scheduler.py` |
| normalize_schedule_input (forgiving dispatch) | `kernos/kernel/scheduler.py` |
| compute_next_fire (tz-aware cron) | `kernos/kernel/scheduler.py` |
| MANAGE_SCHEDULE_TOOL, handle_manage_schedule | `kernos/kernel/scheduler.py` |
| evaluate_triggers | `kernos/kernel/scheduler.py` |
| ToolFailure | `kernos/kernel/tool_failure.py` |
| Tick loop integration | `kernos/kernel/awareness.py` (Phase 2) |
