"""Proactive Awareness — background kernel process for surfacing signals.

The AwarenessEvaluator runs on a periodic timer, checks the knowledge store
for time-anchored signals worth surfacing, and queues structured Whisper
objects. The handler injects pending whispers at session start.

The evaluator OBSERVES only — it never acts on the world.
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event
from kernos.kernel.state import KnowledgeEntry, StateStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------


def generate_whisper_id() -> str:
    """Generate a unique, time-sortable whisper ID."""
    ts_us = time.time_ns() // 1_000
    rand = uuid.uuid4().hex[:4]
    return f"wsp_{ts_us}_{rand}"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Whisper:
    """A structured insight the evaluator wants the agent to surface."""

    whisper_id: str              # Unique ID: "wsp_{timestamp}_{rand4}"
    insight_text: str            # Natural language framing for the agent
    delivery_class: str          # "ambient", "stage", or "interrupt"
    source_space_id: str         # Context space where the signal originated
    target_space_id: str         # Where to deliver (usually source; cross-domain = active space)
    supporting_evidence: list[str]  # Underlying data for follow-up questions
    reasoning_trace: str         # Why this was surfaced (agent draws on when user asks)
    knowledge_entry_id: str      # The KnowledgeEntry that triggered this whisper
    foresight_signal: str        # Raw signal from the knowledge entry (stable — used for suppression matching)
    created_at: str              # ISO 8601 UTC
    surfaced_at: str = ""        # When the agent actually received it (empty = pending)
    notify_via: str = ""         # Channel preference. Empty = most recently used.
    owner_member_id: str = ""    # Member whose activity produced this whisper.
                                 # Disclosure gate filters whispers during another
                                 # member's turn. Empty = instance-wide (legacy /
                                 # system whispers; visible to everyone).


@dataclass
class SuppressionEntry:
    """Tracks what has been surfaced to prevent nagging."""

    whisper_id: str
    knowledge_entry_id: str      # What triggered the whisper
    foresight_signal: str        # RAW signal from KnowledgeEntry (not formatted insight_text)
    created_at: str              # When the whisper was first created
    resolution_state: str        # "surfaced" | "dismissed" | "acted_on" | "resolved"
    resolved_by: str = ""        # "user_dismissed" | "already_handled" | "entry_expired"
                                 # Note: "knowledge_updated" DELETES the entry (see Component 6)
    resolved_at: str = ""        # When resolution happened


# ---------------------------------------------------------------------------
# Tool definition for dismiss_whisper
# ---------------------------------------------------------------------------

DISMISS_WHISPER_TOOL = {
    "name": "dismiss_whisper",
    "description": (
        "Dismiss a proactive insight so it won't be surfaced again. "
        "Use when the user explicitly says they don't want to hear "
        "about this topic or have already handled it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "whisper_id": {
                "type": "string",
                "description": "The whisper ID to dismiss (from the proactive awareness block)",
            },
            "reason": {
                "type": "string",
                "enum": ["user_dismissed", "already_handled"],
                "description": "Why this whisper is being dismissed",
            },
        },
        "required": ["whisper_id"],
    },
}


def _hours_until_foresight(whisper: "Whisper") -> float | None:
    """Extract hours remaining from a whisper's supporting evidence.

    Returns None if foresight_expires cannot be determined.
    """
    for ev in whisper.supporting_evidence:
        if ev.startswith("Expires: "):
            try:
                expires_dt = datetime.fromisoformat(ev[9:])
                now = datetime.now(timezone.utc)
                return (expires_dt - now).total_seconds() / 3600
            except (ValueError, TypeError):
                pass
    return None


# ---------------------------------------------------------------------------
# AwarenessEvaluator
# ---------------------------------------------------------------------------


class AwarenessEvaluator:
    """Background kernel process that checks for signals worth surfacing.

    Runs on a periodic timer. Produces Whisper objects.
    Does NOT call LLMs (MVP time pass). Does NOT act on the world.
    """

    def __init__(
        self,
        state: StateStore,
        events: EventStream,
        interval_seconds: int = 1800,  # 30 minutes default for awareness pass
        trigger_interval_seconds: int = 15,  # 15 seconds for trigger evaluation
        trigger_store=None,  # TriggerStore — set for scheduler support
        handler=None,  # MessageHandler — set for scheduler outbound delivery
        runtime=None,  # WTC v1: TriggerEvaluationRuntime — additive heartbeat
    ) -> None:
        self._state = state
        self._events = events
        self._interval = interval_seconds
        self._trigger_interval = trigger_interval_seconds
        self._trigger_store = trigger_store
        self._handler = handler
        # WTC v1 C5c: optional unified runtime. When wired, the
        # awareness loop drives runtime.evaluate_now() on the same
        # cadence as the legacy trigger evaluation. Additive — the
        # legacy path continues to run for backward compat. The
        # full Pattern 05 strike lands in C7.
        self._runtime = runtime
        self._running = False
        self._task: asyncio.Task | None = None
        self._awareness_tick_count = 0
        self._stale_scan_done: set[str] = set()
        # Proactive outbound budget
        self.PROACTIVE_BUDGET_WINDOW_S = 15  # 15-second window
        self.PROACTIVE_BUDGET_MAX = 2        # Max 2 proactive messages per window
        self._proactive_timestamps: list[float] = []

    async def start(self, instance_id: str) -> None:
        """Start the periodic evaluator for an instance."""
        # WTC v1 C5c: run the unified runtime's recovery sweep once
        # before the heartbeat loop begins. Closes the Kit must-fix
        # seam (AC6 scenarios) — pending outbox rows past their
        # claim lease are reconciled or reclaimed before any new
        # fires are evaluated.
        if self._runtime is not None:
            try:
                recovered = await self._runtime.recover()
                if recovered:
                    logger.info(
                        "WTC_TRIGGER_RECOVER: instance=%s reconciled=%d",
                        instance_id, recovered,
                    )
            except Exception as e:
                logger.error(
                    "WTC trigger recovery sweep error: %s", e,
                )
        self._running = True
        self._task = asyncio.create_task(self._run_loop(instance_id))
        logger.info("AWARENESS: evaluator started for instance=%s awareness=%ds triggers=%ds",
                     instance_id, self._interval, self._trigger_interval)

    async def stop(self) -> None:
        """Stop the evaluator."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("AWARENESS: evaluator stopped")

    def _check_proactive_budget(self, action_type: str = "unknown") -> bool:
        """Check if a proactive message is within budget.

        Returns True if the message can be sent.
        Reactive messages (user-initiated) bypass this entirely.
        """
        now = time.monotonic()
        self._proactive_timestamps = [
            t for t in self._proactive_timestamps
            if now - t < self.PROACTIVE_BUDGET_WINDOW_S
        ]
        if len(self._proactive_timestamps) >= self.PROACTIVE_BUDGET_MAX:
            logger.info(
                "PROACTIVE_BUDGET: blocked type=%s — %d messages in last %ds",
                action_type,
                len(self._proactive_timestamps),
                self.PROACTIVE_BUDGET_WINDOW_S,
            )
            return False
        self._proactive_timestamps.append(now)
        return True

    async def _run_loop(self, instance_id: str) -> None:
        """Main loop — tick every trigger_interval seconds.

        Awareness pass runs every N ticks (where N = awareness_interval / trigger_interval).
        Fast-path interrupt check runs every 300s / trigger_interval ticks.
        Trigger evaluation runs every tick.
        """
        from kernos.kernel.scheduler import EVENT_POLL_INTERVAL_SECONDS
        awareness_every_n = max(1, self._interval // self._trigger_interval)
        interrupt_check_every_n = max(1, 300 // self._trigger_interval)  # ~5 minutes
        event_every_n = max(1, EVENT_POLL_INTERVAL_SECONDS // self._trigger_interval)
        plan_sweep_every_n = max(1, 600 // self._trigger_interval)  # ~10 minutes
        console_clear_every_n = max(1, 21600 // self._trigger_interval)  # ~6 hours
        token_refresh_every_n = max(1, 1800 // self._trigger_interval)  # ~30 minutes
        _interrupt_tick_count = 0
        _event_tick_count = 0
        _plan_sweep_tick_count = 0
        _console_clear_tick_count = 0
        _token_refresh_tick_count = 0

        while self._running:
            self._awareness_tick_count += 1
            _interrupt_tick_count += 1

            # Phase 0: Boot scan — retire stale triggers (first pass only)
            if instance_id not in self._stale_scan_done:
                self._stale_scan_done.add(instance_id)
                if self._trigger_store and self._handler:
                    try:
                        from kernos.kernel.scheduler import retire_stale_triggers
                        await retire_stale_triggers(
                            self._trigger_store, instance_id,
                            self._handler.registry,
                            self._handler,
                        )
                    except Exception as e:
                        logger.warning("Stale trigger scan error: %s", e)

            # Phase 1: Awareness pass (runs every Nth tick)
            if self._awareness_tick_count >= awareness_every_n:
                self._awareness_tick_count = 0
                try:
                    await self._evaluate(instance_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("AwarenessEvaluator error: %s", e)

            # Phase 1b: Fast-path interrupt check (every ~5 minutes)
            # Re-evaluates pending whispers for interrupt promotion + delivers interrupts.
            if _interrupt_tick_count >= interrupt_check_every_n:
                _interrupt_tick_count = 0
                try:
                    await self._interrupt_check(instance_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("Interrupt check error: %s", e)

            # Phase 2: Trigger evaluation (runs every tick)
            if self._trigger_store and self._handler:
                try:
                    from kernos.kernel.scheduler import evaluate_triggers
                    fired = await evaluate_triggers(
                        self._trigger_store, instance_id, self._handler,
                        proactive_budget_check=self._check_proactive_budget,
                    )
                    if fired:
                        logger.info("TRIGGER_EVAL: instance=%s fired=%d", instance_id, fired)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("Trigger evaluation error: %s", e)

            # Phase 2b (WTC v1 C5c): unified TriggerEvaluationRuntime
            # heartbeat. Drives every(cron) walks + drains pending
            # before/after fires. Additive to Phase 2 — the legacy
            # path is untouched. CRB-registered workflows (via STS
            # C5b) flow through this path; manage_schedule's legacy
            # path still uses Phase 2.
            if self._runtime is not None:
                try:
                    runtime_fired = await self._runtime.evaluate_now()
                    if runtime_fired:
                        logger.info(
                            "WTC_TRIGGER_EVAL: instance=%s fired=%d",
                            instance_id, runtime_fired,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(
                        "WTC trigger evaluation error: %s", e,
                    )

            # Phase 3: Event trigger evaluation (adaptive cadence)
            _event_tick_count += 1
            if _event_tick_count >= event_every_n:
                _event_tick_count = 0
                if self._trigger_store and self._handler:
                    mcp_client = getattr(self._handler, 'mcp', None)
                    if mcp_client:
                        try:
                            from kernos.kernel.scheduler import evaluate_event_triggers
                            # Get user timezone from soul for correct time formatting
                            _user_tz = ""
                            try:
                                _soul = await self._state.get_soul(instance_id)
                                if _soul:
                                    _user_tz = getattr(_soul, "timezone", "")
                            except Exception:
                                pass
                            event_fired, next_poll = await evaluate_event_triggers(
                                self._trigger_store, instance_id,
                                self._handler, mcp_client,
                                user_timezone=_user_tz,
                                proactive_budget_check=self._check_proactive_budget,
                            )
                            # Adaptive cadence: adjust event_every_n based on proximity
                            event_every_n = max(1, next_poll // self._trigger_interval)
                            if event_fired:
                                logger.info(
                                    "EVENT_TRIGGERS: instance=%s fired=%d next_poll=%ds",
                                    instance_id, event_fired, next_poll,
                                )
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.warning("EVENT_TRIGGERS_FAILED: %s", e)

            # Phase 4: Plan sweep — re-enqueue stale active plans
            _plan_sweep_tick_count += 1
            if _plan_sweep_tick_count >= plan_sweep_every_n:
                _plan_sweep_tick_count = 0
                if self._handler:
                    try:
                        await self._sweep_stale_plans(instance_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning("PLAN_SWEEP: error: %s", e)

            # Phase 5: Proactive token refresh (~30 minutes)
            # Keeps Codex OAuth tokens fresh even during idle periods.
            # Without this, tokens expire during inactivity and the refresh
            # token can be rotated server-side, leaving us locked out.
            _token_refresh_tick_count += 1
            if _token_refresh_tick_count >= token_refresh_every_n:
                _token_refresh_tick_count = 0
                if self._handler:
                    try:
                        _provider = getattr(self._handler.reasoning, '_provider', None)
                        if _provider and hasattr(_provider, '_ensure_valid_token'):
                            await _provider._ensure_valid_token()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning("TOKEN_REFRESH: proactive refresh failed: %s", e)

            # Phase 6: Periodic console clear (~6 hours)
            _console_clear_tick_count += 1
            if _console_clear_tick_count >= console_clear_every_n:
                _console_clear_tick_count = 0
                print("\033[2J\033[H", end="", flush=True)
                logger.info("CONSOLE_CLEAR: periodic (6h)")

            await asyncio.sleep(self._trigger_interval)

    async def _sweep_stale_plans(self, instance_id: str) -> None:
        """Check for active plans with in-progress steps that aren't being executed.

        If a step has been in_progress for more than 10 minutes with no active
        asyncio task running it, re-enqueue it. This catches plans that stalled
        due to errors, crashes, or slow-poll exhaustion.
        """
        import os
        from kernos.kernel.execution import scan_active_plans, build_envelope_from_plan

        data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
        active_plans = scan_active_plans(data_dir)
        if not active_plans:
            return

        for plan_instance, space_id, plan in active_plans:
            if plan_instance != instance_id:
                continue
            plan_id = plan.get("plan_id", "?")

            # Find in-progress steps
            for phase in plan.get("phases", []):
                for step in phase.get("steps", []):
                    if step.get("status") != "in_progress":
                        continue

                    # Check if this plan already has an active task
                    # (tracked by _plan_progress_msgs — if there's a message, it's running)
                    if hasattr(self._handler, '_plan_progress_msgs') and plan_id in self._handler._plan_progress_msgs:
                        continue  # Still actively executing

                    step_id = step["id"]
                    step_desc = step.get("title", "")
                    envelope = build_envelope_from_plan(plan, step_id, step_desc)
                    _remaining = [
                        s for p in plan.get("phases", [])
                        for s in p.get("steps", [])
                        if s.get("status") == "pending"
                    ]
                    envelope.is_final_step = len(_remaining) == 0

                    logger.info("PLAN_SWEEP: re-enqueuing stale step plan=%s step=%s",
                        plan_id, step_id)
                    asyncio.create_task(
                        self._handler._execute_self_directed_step(instance_id, space_id, envelope))
                    break  # One step per plan per sweep

    async def _evaluate(self, instance_id: str) -> None:
        """Run all evaluation passes for an instance."""
        whispers = await self.run_time_pass(instance_id)

        # Capability gap detection
        try:
            gap_whispers = await self.run_capability_gap_pass(instance_id)
            whispers.extend(gap_whispers)
        except Exception as exc:
            logger.warning("AWARENESS: capability_gap pass failed: %s", exc)

        for whisper in whispers:
            # Check suppression — don't re-surface
            if await self._is_suppressed(instance_id, whisper):
                logger.info("AWARENESS: suppressed whisper=%s signal=%r",
                            whisper.whisper_id, whisper.insight_text[:80])
                continue

            # Interrupt whispers: push immediately via outbound if handler is available
            if whisper.delivery_class == "interrupt" and self._handler:
                pushed = await self._push_interrupt(instance_id, whisper)
                if pushed:
                    continue  # Delivered — don't queue for session-start injection

            # Save to pending queue (ambient, stage, or failed interrupt)
            await self._state.save_whisper(instance_id, whisper)

            # Emit audit event
            try:
                await emit_event(
                    self._events,
                    EventType.PROACTIVE_INSIGHT,
                    instance_id,
                    "awareness_evaluator",
                    payload={
                        "whisper_id": whisper.whisper_id,
                        "insight_text": whisper.insight_text,
                        "delivery_class": whisper.delivery_class,
                        "source_space_id": whisper.source_space_id,
                        "knowledge_entry_id": whisper.knowledge_entry_id,
                        "reasoning_trace": whisper.reasoning_trace,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit proactive.insight: %s", exc)

            logger.info("AWARENESS: queued whisper=%s class=%s signal=%r",
                        whisper.whisper_id, whisper.delivery_class,
                        whisper.insight_text[:80])

        # Enforce queue bound — max 10 pending whispers per-instance
        await self._enforce_queue_bound(instance_id, max_whispers=10)

        # Periodic cleanup of old suppressions
        await self._cleanup_old_suppressions(instance_id)

    async def run_time_pass(self, instance_id: str) -> list[Whisper]:
        """Check for time-anchored signals worth surfacing.

        Queries knowledge entries where foresight_expires falls within
        the next 48 hours. Packages each as a whisper.
        """
        now = datetime.now(timezone.utc)
        window_end = now + timedelta(hours=48)

        entries = await self._state.query_knowledge_by_foresight(
            instance_id,
            expires_before=window_end.isoformat(),
            expires_after=now.isoformat(),
        )

        whispers = []
        for entry in entries:
            # Calculate urgency from time remaining
            try:
                expires_dt = datetime.fromisoformat(entry.foresight_expires)
            except (ValueError, TypeError):
                continue
            hours_remaining = (expires_dt - now).total_seconds() / 3600

            if hours_remaining < 2:
                delivery_class = "interrupt"
            elif hours_remaining < 12:
                delivery_class = "stage"
            else:
                delivery_class = "ambient"

            whisper = Whisper(
                whisper_id=generate_whisper_id(),
                insight_text=self._format_time_insight(entry, hours_remaining),
                delivery_class=delivery_class,
                source_space_id=entry.context_space or "",
                target_space_id=entry.context_space or "",  # Same space for time pass
                supporting_evidence=[
                    f"Knowledge entry: {entry.id}",
                    f"Foresight signal: {entry.foresight_signal}",
                    f"Expires: {entry.foresight_expires}",
                    f"Hours remaining: {hours_remaining:.1f}",
                ],
                reasoning_trace=(
                    f"Time pass detected: '{entry.foresight_signal}' "
                    f"expires in {hours_remaining:.1f} hours. "
                    f"Source: knowledge entry {entry.id} in {entry.context_space}."
                ),
                knowledge_entry_id=entry.id,
                foresight_signal=entry.foresight_signal,
                created_at=now.isoformat(),
            )
            whispers.append(whisper)

        logger.info("AWARENESS: time_pass entries_checked=%d whispers_produced=%d",
                     len(entries), len(whispers))

        return whispers

    async def run_capability_gap_pass(self, instance_id: str) -> list[Whisper]:
        """Detect workaround patterns that suggest a missing tool.

        Reads recent conversation logs looking for patterns where the agent
        used plain files, manual formatting, or repeated multi-step processes
        that could be a registered workspace tool. One cheap LLM call.
        """
        # Read recent conversation from the active space
        try:
            from kernos.kernel.state import InstanceProfile
            profile = await self._state.get_instance_profile(instance_id)
            if not profile or not profile.last_active_space_id:
                return []
            space_id = profile.last_active_space_id
        except Exception:
            return []

        # Get recent conversation log
        try:
            if not self._handler:
                return []
            log_text = await self._handler.conv_logger.read_current_log_text(instance_id, space_id)
            if isinstance(log_text, tuple):
                log_text = log_text[0]
            if not log_text or len(log_text) < 200:
                return []
        except Exception:
            return []

        # One cheap LLM call — check for capability gaps
        try:
            reasoning = self._handler.reasoning if self._handler else None
            if not reasoning:
                return []
            result = await reasoning.complete_simple(
                system_prompt=(
                    "You detect capability gaps — places where the user could benefit from "
                    "a built tool instead of manual workarounds. Look for:\n"
                    "- Data tracked in plain text files instead of a structured tool\n"
                    "- Repeated multi-step processes that could be automated\n"
                    "- Manual formatting that a tool could handle\n"
                    "- The agent saying 'I can't do that' when it could build something\n\n"
                    "If you find a gap, describe what tool could be built and why.\n"
                    "If no gaps found, reply with just: NONE"
                ),
                user_content=f"Recent conversation:\n{log_text[-3000:]}",
                max_tokens=200,
                prefer_cheap=True,
            )

            if not result or "NONE" in result.upper()[:20]:
                return []

            now = datetime.now(timezone.utc)
            whisper = Whisper(
                whisper_id=generate_whisper_id(),
                insight_text=result.strip(),
                delivery_class="ambient",
                source_space_id=space_id,
                target_space_id=space_id,
                supporting_evidence=["capability_gap_detection"],
                reasoning_trace="Awareness evaluator detected a potential capability gap from recent conversation patterns.",
                knowledge_entry_id="",
                foresight_signal="capability_gap",
                created_at=now.isoformat(),
            )
            logger.info("AWARENESS: capability_gap detected in space=%s", space_id)
            return [whisper]
        except Exception as exc:
            logger.warning("AWARENESS: capability_gap LLM failed: %s", exc)
            return []

    def _format_time_insight(self, entry: KnowledgeEntry, hours: float) -> str:
        """Format a foresight signal into natural insight text for the agent."""
        if hours < 2:
            urgency = "very soon"
        elif hours < 6:
            urgency = "in the next few hours"
        elif hours < 24:
            urgency = "today"
        else:
            urgency = "tomorrow"

        return (
            f"Upcoming: {entry.foresight_signal}. "
            f"This is relevant {urgency} (expires in ~{hours:.0f} hours). "
            f"Related knowledge: {entry.content[:200]}"
        )

    async def _enforce_queue_bound(self, instance_id: str, max_whispers: int = 10) -> None:
        """Trim the whisper queue to max_whispers.

        Priority: stage before ambient. Within same class, newest first.
        Excess whispers are silently dropped (not suppressed — they just
        didn't make the cut).
        """
        pending = await self._state.get_pending_whispers(instance_id)
        if len(pending) <= max_whispers:
            return

        # Sort: stage first, then by created_at descending (newest first)
        pending.sort(key=lambda w: (
            0 if w.delivery_class == "stage" else 1,
            w.created_at,
        ))
        # Within same delivery class, we want newest first, so reverse created_at
        # Re-sort with proper key: stage before ambient, then newest first
        pending.sort(key=lambda w: (
            0 if w.delivery_class == "stage" else 1,
            # Negate time: invert ISO string is complex, just use a tuple sort
        ))
        # Simpler approach: separate stage and ambient, sort each by newest
        stage = [w for w in pending if w.delivery_class == "stage"]
        ambient = [w for w in pending if w.delivery_class != "stage"]
        stage.sort(key=lambda w: w.created_at, reverse=True)  # newest first
        ambient.sort(key=lambda w: w.created_at, reverse=True)  # newest first
        prioritized = stage + ambient

        # Keep the top max_whispers, delete the rest
        keep = set(w.whisper_id for w in prioritized[:max_whispers])
        for w in prioritized:
            if w.whisper_id not in keep:
                await self._state.delete_whisper(instance_id, w.whisper_id)
                logger.info("AWARENESS: trimmed whisper=%s (queue bound %d)",
                            w.whisper_id, max_whispers)

    async def _is_suppressed(self, instance_id: str, whisper: Whisper) -> bool:
        """Check if this whisper has already been surfaced or dismissed.

        Suppression is keyed to knowledge_entry_id, not insight text.
        The insight text changes every cycle (countdown updates). The
        knowledge entry ID is stable — if we already surfaced a whisper
        for this entry and nothing changed, suppress.
        """
        suppressions = await self._state.get_suppressions(
            instance_id,
            knowledge_entry_id=whisper.knowledge_entry_id,
        )

        for s in suppressions:
            if s.resolution_state in ("surfaced", "dismissed", "acted_on"):
                return True
            if s.resolution_state == "resolved" and s.resolved_by == "entry_expired":
                return True

        return False

    async def _push_interrupt(self, instance_id: str, whisper: Whisper) -> bool:
        """Push an interrupt whisper via outbound messaging.

        Returns True if successfully delivered (or suppressed because user is active).
        Returns False if outbound failed (whisper should be queued for session-start).
        """
        if not self._handler:
            return False

        # Check: is user currently active? If so, let stage handle it.
        try:
            spaces = await self._state.list_context_spaces(instance_id)
            now = datetime.now(timezone.utc)
            user_active = False
            for space in spaces:
                if space.last_active_at:
                    try:
                        last_active = datetime.fromisoformat(space.last_active_at)
                        if (now - last_active).total_seconds() < 300:  # 5 minutes
                            user_active = True
                            break
                    except (ValueError, TypeError):
                        continue

            if user_active:
                # User is active — downgrade to stage, session-start will catch it
                whisper.delivery_class = "stage"
                logger.info(
                    "WHISPER_SUPPRESS_ACTIVE: id=%s signal=%r downgraded to stage (user active)",
                    whisper.whisper_id, whisper.foresight_signal[:80],
                )
                return False  # Queue for session-start injection
        except Exception as exc:
            logger.warning("Interrupt active check failed: %s", exc)

        # Proactive budget check
        if not self._check_proactive_budget("whisper"):
            return False  # Deferred to next tick

        # Push via outbound
        success = await self._handler.send_outbound(
            instance_id=instance_id,
            member_id="",  # V1: owner
            channel_name=whisper.notify_via or None,
            message=whisper.insight_text,
        )

        if success:
            # Store in conversation history
            await self._store_whisper_message(instance_id, whisper)

            # Write to per-space conversation log
            if self._handler and hasattr(self._handler, "conv_logger"):
                space_id = whisper.target_space_id or whisper.source_space_id or ""
                if space_id:
                    await self._handler.conv_logger.append(
                        instance_id=instance_id,
                        space_id=space_id,
                        speaker="assistant",
                        channel="whisper",
                        content=whisper.insight_text,
                    )

            # Mark surfaced + create suppression
            await self._state.mark_whisper_surfaced(instance_id, whisper.whisper_id)
            suppression = SuppressionEntry(
                whisper_id=whisper.whisper_id,
                knowledge_entry_id=whisper.knowledge_entry_id,
                foresight_signal=whisper.foresight_signal,
                created_at=whisper.created_at,
                resolution_state="surfaced",
            )
            await self._state.save_suppression(instance_id, suppression)

            logger.info(
                "WHISPER_PUSH: id=%s class=interrupt channel=%s signal=%r space=%s",
                whisper.whisper_id, whisper.notify_via or "default",
                whisper.foresight_signal[:80], whisper.target_space_id,
            )
            return True
        else:
            # Outbound failed — keep as pending for retry or session-start
            logger.warning(
                "WHISPER_PUSH_FAILED: id=%s signal=%r, keeping as pending",
                whisper.whisper_id, whisper.foresight_signal[:80],
            )
            return False

    async def _interrupt_check(self, instance_id: str) -> None:
        """Fast-path interrupt check — promote pending whispers to interrupt if threshold crossed.

        Runs every ~5 minutes. Lightweight: reads pending whispers, checks timestamps,
        promotes if needed. No LLM call.
        """
        pending = await self._state.get_pending_whispers(instance_id)
        promoted = 0

        for whisper in pending:
            if whisper.delivery_class == "interrupt":
                # Already interrupt but not yet pushed — try again
                if self._handler:
                    pushed = await self._push_interrupt(instance_id, whisper)
                    if pushed:
                        # Remove from pending since it was delivered
                        await self._state.delete_whisper(instance_id, whisper.whisper_id)
                        promoted += 1
                continue

            # Check if this whisper should be promoted to interrupt
            # Parse foresight_expires from supporting evidence
            hours_remaining = _hours_until_foresight(whisper)
            if hours_remaining is not None and hours_remaining < 2:
                logger.info(
                    "INTERRUPT_PROMOTE: id=%s hours=%.1f signal=%r",
                    whisper.whisper_id, hours_remaining, whisper.foresight_signal[:80],
                )
                whisper.delivery_class = "interrupt"
                if self._handler:
                    pushed = await self._push_interrupt(instance_id, whisper)
                    if pushed:
                        await self._state.delete_whisper(instance_id, whisper.whisper_id)
                        promoted += 1
                    else:
                        # Save updated delivery_class even if push failed
                        await self._state.save_whisper(instance_id, whisper)
                else:
                    await self._state.save_whisper(instance_id, whisper)
                promoted += 1

        if promoted:
            logger.info("INTERRUPT_CHECK: instance=%s promoted=%d", instance_id, promoted)

    async def _store_whisper_message(self, instance_id: str, whisper: Whisper) -> None:
        """Store pushed whisper in conversation history so the agent sees it."""
        if not self._handler or not hasattr(self._handler, "conversations"):
            return

        # Find the most recent conversation for this instance
        conversation_id = ""
        try:
            conversations = await self._state.list_conversations(instance_id, active_only=True, limit=1)
            if conversations:
                conversation_id = conversations[0].conversation_id
        except Exception:
            pass

        if not conversation_id:
            logger.warning(
                "WHISPER_HISTORY: no conversation found for instance=%s, skipping",
                instance_id,
            )
            return

        try:
            space_id = whisper.target_space_id or whisper.source_space_id or ""
            entry = {
                "role": "assistant",
                "content": f"[WHISPER] {whisper.insight_text}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "platform": "awareness",
                "instance_id": instance_id,
                "conversation_id": conversation_id,
                "space_tags": [space_id] if space_id else None,
            }
            await self._handler.conversations.append(
                instance_id, conversation_id, entry,
            )
            logger.info(
                "WHISPER_HISTORY: stored [WHISPER] message for whisper=%s in space=%s",
                whisper.whisper_id, space_id or "general",
            )
        except Exception as exc:
            logger.warning(
                "WHISPER_HISTORY: failed to store message for whisper=%s: %s",
                whisper.whisper_id, exc,
            )

    async def _cleanup_old_suppressions(self, instance_id: str) -> None:
        """Remove suppression entries older than 7 days."""
        suppressions = await self._state.get_suppressions(instance_id)
        now = datetime.now(timezone.utc)
        removed = 0
        for s in suppressions:
            try:
                created = datetime.fromisoformat(s.created_at)
                if (now - created).total_seconds() > 7 * 86400:
                    await self._state.delete_suppression(instance_id, s.whisper_id)
                    removed += 1
            except (ValueError, TypeError):
                continue
        if removed:
            logger.info("AWARENESS: cleanup suppressions_removed=%d", removed)
