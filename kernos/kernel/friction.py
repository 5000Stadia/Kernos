"""Friction Observer — detects system friction and writes diagnostic reports.

Post-turn cohort agent. Reads turn trace data, detects friction patterns,
writes self-contained bug reports to data/diagnostics/friction/.

Biased toward subtraction: REMOVE > STRUCTURAL_ENFORCE > SIMPLIFY > ADD.

FRICTION-PATTERN-STABLE-IDS-V1 integration: an optional
``pattern_store`` injection in :class:`FrictionObserver` enables the
auto-classifier hook. When wired, each friction signal is matched
against the catalog's patterns; the hook dispatches to
``record_occurrence`` or ``record_recurrence`` based on the matched
pattern's ``lifecycle_state``. Fail-open: classifier errors log and
continue; never block the report write.
"""
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Preference-shaped language patterns (signal 7)
# These must be specific enough to avoid matching roleplay/narrative text.
# "Never" alone is too broad — requires a behavioral verb after it.
_PREF_PATTERNS = [
    re.compile(r"\b(from now on|every time|whenever|each time)\b", re.I),
    re.compile(r"\balways\b.{0,20}\b(do|use|send|notify|remind|check|include|keep)\b", re.I),
    re.compile(r"\bnever\b.{0,20}\b(do|use|send|notify|ask|confirm|check|include)\b", re.I),
    re.compile(r"\b(remind me|notify me|let me know|alert me)\b.{0,40}\b(before|when|if|every)\b", re.I),
    re.compile(r"\b(don'?t|do not|stop)\b.{0,30}\b(ask|confirm|check|send|notify)\b", re.I),
]


@dataclass
class FrictionSignal:
    """A detected friction event."""
    signal_type: str          # e.g. TOOL_REQUEST_FOR_SURFACED_TOOL
    description: str          # Human-readable short description
    evidence: list[str]       # Log-like evidence lines
    context: dict             # Snapshot: user message, space, tools, etc.
    heuristic: bool = False   # True for low-confidence signals (2, 4)


class FrictionObserver:
    """Detects system friction from turn trace data.

    Called post-turn with the TurnContext and tool trace. Produces
    FrictionSignal objects and writes reports to disk.
    """

    def __init__(
        self,
        reasoning: Any = None,
        data_dir: str = "./data",
        enabled: bool = True,
        pattern_store: Any = None,
        emit_event: Callable[[str, dict], Awaitable[None]] | None = None,
    ) -> None:
        self._reasoning = reasoning
        self._data_dir = data_dir
        self._enabled = enabled
        # FRICTION-PATTERN-STABLE-IDS-V1: optional catalog wiring. When
        # None, behavior is identical to pre-spec (markdown report only).
        self._pattern_store = pattern_store
        self._emit_event = emit_event

    async def observe(
        self,
        *,
        instance_id: str,
        user_message: str,
        response_text: str,
        tool_trace: list[dict],
        surfaced_tool_names: set[str],
        active_space_id: str,
        merged_count: int,
        is_reactive: bool,
        pref_detected: bool,
        provider_errors: list[str] | None = None,
        has_now_block_time: bool = False,
        member_id: str = "",
    ) -> list[FrictionSignal]:
        """Run all signal detectors and write reports for any friction found.

        Returns the list of detected signals (empty if no friction).

        ``member_id`` (Codex post-impl Finding M5) carries the
        originating member through into the per-signal context so the
        FRICTION-PATTERN-STABLE-IDS-V1 classifier hook can record it on
        occurrence rows for provenance. Optional for backward-compat
        with callers that don't have a member; defaults to empty string.
        """
        if not self._enabled:
            return []

        signals: list[FrictionSignal] = []

        ctx_snapshot = {
            "user_message": user_message[:500],
            "space": active_space_id,
            "member_id": member_id,
            "surfaced_tool_count": len(surfaced_tool_names),
            "tool_calls": [t["name"] for t in tool_trace],
            "merged_count": merged_count,
            "is_reactive": is_reactive,
        }

        # Signal 9: EMPTY_RESPONSE — agent returned nothing to a non-empty user message
        if user_message.strip() and (not response_text or not response_text.strip()):
            signals.append(FrictionSignal(
                signal_type="EMPTY_RESPONSE",
                description="Agent returned an empty response to a non-empty user message",
                evidence=[
                    f"User message: {user_message[:200]}",
                    f"Response length: {len(response_text or '')} chars",
                    f"Tool calls: {[t['name'] for t in tool_trace] or 'none'}",
                ],
                context=ctx_snapshot,
            ))

        # Signal 1: TOOL_REQUEST_FOR_SURFACED_TOOL
        sig = self._check_request_for_surfaced(tool_trace, surfaced_tool_names, ctx_snapshot)
        if sig:
            signals.append(sig)

        # Signal 3: GATE_CONFIRM_ON_REACTIVE
        sig = self._check_gate_confirm_reactive(tool_trace, is_reactive, ctx_snapshot)
        if sig:
            signals.append(sig)

        # Signal 5: SCHEMA_ERROR_ON_PROVIDER
        sig = self._check_schema_error(tool_trace, ctx_snapshot)
        if sig:
            signals.append(sig)

        # Signal 6: MERGED_MESSAGES_DROPPED
        sig = self._check_merged_dropped(merged_count, response_text, user_message, ctx_snapshot)
        if sig:
            signals.append(sig)

        # Signal 7: PREFERENCE_STATED_BUT_NOT_CAPTURED
        sig = self._check_pref_missed(user_message, pref_detected, ctx_snapshot)
        if sig:
            signals.append(sig)

        # Signal 2: STALE_DATA_IN_RESPONSE (heuristic)
        sig = self._check_stale_data(tool_trace, response_text, user_message, ctx_snapshot, has_now_block_time)
        if sig:
            signals.append(sig)

        # Signal 4: TOOL_AVAILABLE_BUT_NOT_USED (heuristic)
        sig = self._check_tool_not_used(
            tool_trace, surfaced_tool_names, user_message, response_text, ctx_snapshot
        )
        if sig:
            signals.append(sig)

        # Signal 8: PROVIDER_ERROR_REPEATED
        sig = self._check_provider_errors(provider_errors or [], ctx_snapshot)
        if sig:
            signals.append(sig)

        # Write reports + emit to unified timeline
        for signal in signals:
            logger.warning(
                "FRICTION: type=%s desc=%s",
                signal.signal_type, signal.description[:120],
            )
            try:
                await self._write_report(signal, instance_id)
            except Exception as exc:
                logger.warning("FRICTION: failed to write report: %s", exc)
            # EVENT-STREAM-TO-SQLITE: friction observation emission.
            try:
                from kernos.kernel import event_stream
                await event_stream.emit(
                    instance_id, "friction.observed",
                    {
                        "signal_type": signal.signal_type,
                        "description": signal.description[:200],
                        "heuristic": signal.heuristic,
                    },
                    space_id=active_space_id or None,
                )
            except Exception as exc:
                logger.debug("Failed to emit friction.observed: %s", exc)

        return signals

    # ------------------------------------------------------------------
    # Signal detectors
    # ------------------------------------------------------------------

    def _check_request_for_surfaced(
        self, tool_trace: list[dict], surfaced: set[str], ctx: dict,
    ) -> FrictionSignal | None:
        """Signal 1: Agent called request_tool when the target tool was already surfaced."""
        for call in tool_trace:
            if call["name"] != "request_tool":
                continue
            # Check if any surfaced tool matches what was requested
            cap_name = call.get("input", {}).get("capability_name", "")
            desc = call.get("input", {}).get("description", "").lower()

            # Check if the capability's tools are in the surfaced set
            matching_surfaced = []
            for tool_name in surfaced:
                if cap_name and cap_name != "unknown" and cap_name in tool_name:
                    matching_surfaced.append(tool_name)
                elif any(kw in tool_name.lower() for kw in desc.split()[:5] if len(kw) > 3):
                    matching_surfaced.append(tool_name)

            # Also check common tool categories
            _cal_tools = {"create-event", "list-events", "search-events", "get-event"}
            _search_tools = {"brave_web_search", "brave_local_search"}
            if any(kw in desc for kw in ["calendar", "event", "appointment", "schedule"]):
                matching_surfaced.extend(t for t in _cal_tools if t in surfaced)
            if any(kw in desc for kw in ["search", "web", "find", "look up"]):
                matching_surfaced.extend(t for t in _search_tools if t in surfaced)

            if matching_surfaced:
                return FrictionSignal(
                    signal_type="TOOL_REQUEST_FOR_SURFACED_TOOL",
                    description=(
                        f"Agent called request_tool (capability='{cap_name}') but "
                        f"matching tools already surfaced: {sorted(set(matching_surfaced))}"
                    ),
                    evidence=[
                        f"request_tool input: capability_name='{cap_name}', description='{desc[:100]}'",
                        f"Surfaced tools ({len(surfaced)}): {sorted(surfaced)[:10]}...",
                        f"Matching surfaced: {sorted(set(matching_surfaced))}",
                    ],
                    context=ctx,
                )
        return None

    def _check_gate_confirm_reactive(
        self, tool_trace: list[dict], is_reactive: bool, ctx: dict,
    ) -> FrictionSignal | None:
        """Signal 3: Gate returned CONFIRM for reactive soft_write — shouldn't happen."""
        # This is detected from gate events, not tool trace directly.
        # The gate bypass should prevent this entirely. If we see gate blocks
        # on reactive soft_write, the bypass isn't working.
        # For V1: detect if request_tool was called because a tool was blocked
        # (this is a proxy — the actual gate trace would need event stream reading)
        return None  # V1: rely on GATE log lines; full integration in V2

    def _check_schema_error(
        self, tool_trace: list[dict], ctx: dict,
    ) -> FrictionSignal | None:
        """Signal 5: Structured output schema error from provider."""
        # This fires from PREF_DETECT logs, not tool_trace.
        # V1: handled by checking pref_detected flag externally.
        return None  # Detected at the source (preference_parser logs)

    def _check_merged_dropped(
        self, merged_count: int, response: str, user_message: str, ctx: dict,
    ) -> FrictionSignal | None:
        """Signal 6: Agent addressed fewer topics than merged messages."""
        if merged_count <= 1:
            return None

        # Empty response is a separate signal (EMPTY_RESPONSE handles it)
        if not response or not response.strip():
            return None

        # Count substantial content units (paragraphs or list items)
        response_paragraphs = [p.strip() for p in response.split("\n\n") if len(p.strip()) > 20]
        response_lines = [l.strip() for l in response.split("\n") if len(l.strip()) > 10]
        content_units = max(len(response_paragraphs), len(response_lines))
        if content_units >= merged_count:
            return None  # Addressed enough topics

        # Very short response relative to merge count
        words = len(response.split())
        if words < merged_count * 8:
            return FrictionSignal(
                signal_type="MERGED_MESSAGES_DROPPED",
                description=(
                    f"Turn had {merged_count} merged messages but response "
                    f"only has {content_units} content units / {words} words"
                ),
                evidence=[
                    f"Merged count: {merged_count}",
                    f"Content units: {content_units}",
                    f"Response words: {words}",
                ],
                context=ctx,
            )
        return None

    def _check_pref_missed(
        self, user_message: str, pref_detected: bool, ctx: dict,
    ) -> FrictionSignal | None:
        """Signal 7: User said something preference-shaped but no detection fired."""
        if pref_detected:
            return None  # Detection pipeline ran

        if any(p.search(user_message) for p in _PREF_PATTERNS):
            return FrictionSignal(
                signal_type="PREFERENCE_STATED_BUT_NOT_CAPTURED",
                description=(
                    f"User message contains preference-shaped language but "
                    f"no PREF_DETECT event fired"
                ),
                evidence=[
                    f"User message: {user_message[:200]}",
                    f"Matched pattern: preference-shaped language detected",
                    f"pref_detected: {pref_detected}",
                ],
                context=ctx,
            )
        return None

    def _check_stale_data(
        self, tool_trace: list[dict], response: str, user_message: str, ctx: dict,
        has_now_block_time: bool = False,
    ) -> FrictionSignal | None:
        """Signal 2: Agent answered with data from context instead of calling a tool. Heuristic."""
        msg_lower = user_message.lower()

        # Time queries without ANY authoritative time source
        # The NOW block provides current time in the system prompt — reading it is correct.
        # Only flag when there's no NOW block time AND no get-current-time call.
        time_keywords = ["what time", "current time", "time is it", "what's the time"]
        if any(kw in msg_lower for kw in time_keywords):
            tool_names = {t["name"] for t in tool_trace}
            if "get-current-time" not in tool_names and not has_now_block_time:
                return FrictionSignal(
                    signal_type="STALE_DATA_IN_RESPONSE",
                    description="Agent answered a time query without calling get-current-time",
                    evidence=[
                        f"User asked about time: {user_message[:100]}",
                        f"Tools called: {sorted(tool_names) or 'none'}",
                        f"get-current-time was NOT called",
                    ],
                    context=ctx,
                    heuristic=True,
                )
        return None

    def _check_tool_not_used(
        self, tool_trace: list[dict], surfaced: set[str],
        user_message: str, response: str, ctx: dict,
    ) -> FrictionSignal | None:
        """Signal 4: Agent had a tool for the query but didn't use it. Heuristic."""
        msg_lower = user_message.lower()
        tool_names = {t["name"] for t in tool_trace}

        # Preference/state queries without inspect_state
        # Note: remember now delegates to inspect_state for preference-shaped queries
        # internally, so calling remember IS using structured state.
        state_keywords = ["what preferences", "what settings", "what's set up",
                         "what do i have set", "my notifications", "my triggers"]
        if any(kw in msg_lower for kw in state_keywords):
            if "inspect_state" in tool_names or "remember" in tool_names:
                return None  # Agent used a tool that provides state data
            if "inspect_state" in surfaced:
                return FrictionSignal(
                    signal_type="TOOL_AVAILABLE_BUT_NOT_USED",
                    description="Agent answered a state query without calling inspect_state or remember",
                    evidence=[
                        f"User asked about state: {user_message[:100]}",
                        f"inspect_state: surfaced but NOT called",
                        f"Tools called: {sorted(tool_names) or 'none'}",
                    ],
                    context=ctx,
                    heuristic=True,
                )

        # Schedule/trigger queries — manage_schedule is for triggers, NOT calendar.
        # Calendar tools (list-events, search-events) are the correct tools for
        # "What's on my schedule?" / "What's on my calendar?".
        # Only flag manage_schedule for explicit trigger/reminder queries.
        trigger_keywords = ["what triggers", "my triggers", "what automations", "my automations",
                           "what reminders", "my reminders"]
        if any(kw in msg_lower for kw in trigger_keywords):
            _calendar_reads = {"list-events", "search-events", "get-event", "get-freebusy"}
            if tool_names & _calendar_reads or "manage_schedule" in tool_names:
                return None
            if "manage_schedule" in surfaced:
                return FrictionSignal(
                    signal_type="TOOL_AVAILABLE_BUT_NOT_USED",
                    description="Agent answered a trigger query without calling manage_schedule",
                    evidence=[
                        f"User asked about triggers: {user_message[:100]}",
                        f"manage_schedule: surfaced but NOT called",
                        f"Tools called: {sorted(tool_names) or 'none'}",
                    ],
                    context=ctx,
                    heuristic=True,
                )
        return None

    def _check_provider_errors(
        self, errors: list[str], ctx: dict,
    ) -> FrictionSignal | None:
        """Signal 8: Same provider error occurs 2+ times — infrastructure bug."""
        if len(errors) < 2:
            return None

        # Count error frequencies
        from collections import Counter
        counts = Counter(errors)
        repeated = {msg: count for msg, count in counts.items() if count >= 2}
        if not repeated:
            return None

        top_msg, top_count = max(repeated.items(), key=lambda x: x[1])
        return FrictionSignal(
            signal_type="PROVIDER_ERROR_REPEATED",
            description=(
                f"Provider error occurred {top_count}x in this session: "
                f"{top_msg[:100]}"
            ),
            evidence=[
                f"Error message: {top_msg[:200]}",
                f"Occurrences: {top_count}",
                f"Total errors this session: {len(errors)}",
                f"Distinct errors: {len(counts)}",
                "Check provider code for error parsing — if message is always "
                "'unknown', the parsing code may be reading the wrong field",
            ],
            context=ctx,
        )

    # ------------------------------------------------------------------
    # Report writing
    # ------------------------------------------------------------------

    async def _write_report(self, signal: FrictionSignal, instance_id: str) -> None:
        """Write a friction report file with LLM-generated description."""
        # FRICTION-PATTERN-STABLE-IDS-V1 round-1 Finding 8: UUID8 suffix
        # for collision resistance. Second-granularity timestamps alone
        # are insufficient when friction cascades fire multiple signals
        # in the same second; the catalog uses report_path as an
        # evidence key so filename collisions corrupt the catalog.
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_type = signal.signal_type.replace(" ", "_")[:40]
        uuid8 = uuid.uuid4().hex[:8]
        filename = f"FRICTION_{ts}_{safe_type}_{uuid8}.md"

        friction_dir = os.path.join(self._data_dir, "diagnostics", "friction")
        os.makedirs(friction_dir, exist_ok=True)
        filepath = os.path.join(friction_dir, filename)

        # Generate LLM description if reasoning service available
        llm_description = ""
        recommendation = "SIMPLIFY"
        if self._reasoning:
            try:
                llm_description, recommendation = await self._generate_description(signal)
            except Exception as exc:
                logger.warning("FRICTION: LLM description failed: %s", exc)
                llm_description = signal.description
                recommendation = self._default_recommendation(signal.signal_type)
        else:
            llm_description = signal.description
            recommendation = self._default_recommendation(signal.signal_type)

        heuristic_note = ""
        if signal.heuristic:
            heuristic_note = "\n**Confidence:** LOW (heuristic — may be a false positive)\n"

        report = (
            f"# Friction Report: {signal.signal_type}\n"
            f"Generated: {datetime.now(timezone.utc).isoformat()}\n"
            f"{heuristic_note}\n"
            f"## Description\n{llm_description}\n\n"
            f"## Recommendation: {recommendation}\n"
            f"{signal.description}\n\n"
            f"## Evidence\n"
        )
        for line in signal.evidence:
            report += f"- {line}\n"
        report += (
            f"\n## Context\n"
            f"User message: {signal.context.get('user_message', 'N/A')}\n"
            f"Space: {signal.context.get('space', 'N/A')}\n"
            f"Tools surfaced: {signal.context.get('surfaced_tool_count', 'N/A')}\n"
            f"Tool calls: {signal.context.get('tool_calls', [])}\n"
            f"Merged count: {signal.context.get('merged_count', 0)}\n"
            f"Reactive: {signal.context.get('is_reactive', True)}\n"
        )

        with open(filepath, "w") as f:
            f.write(report)

        logger.info("FRICTION_REPORT: written %s", filepath)

        # FRICTION-PATTERN-STABLE-IDS-V1 classifier hook. Fail-open per
        # round-2 Blocker 2: dispatch by lifecycle_state with the proper
        # classified_by vocabulary; classifier failure logs and continues,
        # never blocks the report write.
        if self._pattern_store is not None:
            try:
                await self._classify_and_record(signal, instance_id, filepath)
            except Exception as exc:
                logger.warning("FRICTION_CLASSIFY: failed: %s", exc)

    async def _classify_and_record(
        self, signal: FrictionSignal, instance_id: str, filepath: str,
    ) -> None:
        """Match the signal against catalog patterns and record via the
        right method based on lifecycle_state. Imported lazily to keep
        friction.py importable in environments where the catalog hasn't
        been wired (e.g., legacy tests).
        """
        from kernos.kernel.friction_patterns import (
            LIFECYCLE_ACTIVE,
            LIFECYCLE_REACTIVATED,
            LIFECYCLE_RESOLVED,
            classified_by_for_match_path,
            classify_signal,
        )
        from kernos.utils import utc_now

        # Lazy schema-ensure. The store's start() is idempotent so
        # repeated calls are no-ops after the first; this keeps the
        # handler's synchronous __init__ clean (can't await there).
        # Codex post-impl Finding H1: production wiring must reach the
        # store, not just construct it.
        await self._pattern_store.ensure_schema(self._data_dir)

        candidates = await self._pattern_store.list_patterns(instance_id)
        result = classify_signal(
            signal_type=signal.signal_type,
            signal_description=signal.description,
            candidates=candidates,
        )
        if result is None:
            await self._emit_pattern_unclassified(signal, filepath, instance_id)
            return

        pattern, score, match_path = result
        classified_by = classified_by_for_match_path(match_path)
        observed_at = utc_now()
        space_id = signal.context.get("space", "") or ""
        member_id = signal.context.get("member_id", "") or ""

        if pattern.lifecycle_state in (LIFECYCLE_ACTIVE, LIFECYCLE_REACTIVATED):
            await self._pattern_store.record_occurrence(
                instance_id=instance_id,
                pattern_id=pattern.pattern_id,
                observed_at=observed_at,
                report_path=filepath,
                classifier_score=score,
                classified_by=classified_by,
                space_id=space_id,
                member_id=member_id,
                # SELF-CONTROLLED-LOOP-LIVENESS-V1: enable active-
                # frequency threshold-crossing emission so the
                # self-improvement autonomy loop has a path to fire
                # on active-pattern accumulation (not just
                # resolved→reactivated transitions).
                emit_event=self._emit_event,
            )
        elif pattern.lifecycle_state == LIFECYCLE_RESOLVED:
            await self._pattern_store.record_recurrence(
                instance_id=instance_id,
                pattern_id=pattern.pattern_id,
                observed_at=observed_at,
                report_path=filepath,
                classifier_score=score,
                classified_by=classified_by,
                space_id=space_id,
                member_id=member_id,
                emit_event=self._emit_event,
            )
        else:
            # Archived — preserve audit trail via unclassified event.
            await self._emit_pattern_unclassified(signal, filepath, instance_id)

    async def _emit_pattern_unclassified(
        self, signal: FrictionSignal, filepath: str, instance_id: str,
    ) -> None:
        """Surface unclassified friction so a higher-tier observer can
        hand-classify later. Uses the event_stream emit hook if wired,
        otherwise the kernel.event_stream module fallback."""
        payload = {
            "signal_type": signal.signal_type,
            "description": signal.description[:200],
            "report_path": filepath,
        }
        try:
            if self._emit_event is not None:
                await self._emit_event(
                    "friction.pattern_unclassified", payload,
                )
                return
            from kernos.kernel import event_stream
            await event_stream.emit(
                instance_id, "friction.pattern_unclassified", payload,
            )
        except Exception as exc:
            logger.debug(
                "FRICTION_PATTERNS: pattern_unclassified emit failed: %s", exc,
            )

    async def _generate_description(self, signal: FrictionSignal) -> tuple[str, str]:
        """Use a cheap LLM call to generate a human-readable friction description."""
        prompt = (
            "You are a system diagnostics observer. A friction event was detected "
            "in an AI assistant system. Write a clear, concise description of what "
            "went wrong, why it matters, and what the likely fix is.\n\n"
            "Your recommendation MUST be one of:\n"
            "- REMOVE — delete the thing causing friction\n"
            "- STRUCTURAL_ENFORCE — enforce in code, not prompt\n"
            "- SIMPLIFY — make the existing mechanism cleaner\n"
            "- ADD — add something new (last resort)\n\n"
            "Format: first line is the recommendation word, rest is description."
        )
        evidence_text = "\n".join(f"- {e}" for e in signal.evidence)
        user_content = (
            f"Signal: {signal.signal_type}\n"
            f"Summary: {signal.description}\n"
            f"Evidence:\n{evidence_text}\n"
            f"Context: {json.dumps(signal.context, default=str)[:500]}"
        )

        raw = await self._reasoning.complete_simple(
            system_prompt=prompt,
            user_content=user_content,
            max_tokens=300,
            prefer_cheap=True,
        )

        lines = raw.strip().split("\n", 1)
        first = lines[0].strip().upper()
        desc = lines[1].strip() if len(lines) > 1 else signal.description

        valid_recs = {"REMOVE", "STRUCTURAL_ENFORCE", "SIMPLIFY", "ADD"}
        recommendation = first if first in valid_recs else self._default_recommendation(signal.signal_type)

        return desc, recommendation

    @staticmethod
    def _default_recommendation(signal_type: str) -> str:
        """Default recommendation based on signal type."""
        defaults = {
            "TOOL_REQUEST_FOR_SURFACED_TOOL": "SIMPLIFY",
            "STALE_DATA_IN_RESPONSE": "SIMPLIFY",
            "GATE_CONFIRM_ON_REACTIVE": "STRUCTURAL_ENFORCE",
            "TOOL_AVAILABLE_BUT_NOT_USED": "SIMPLIFY",
            "SCHEMA_ERROR_ON_PROVIDER": "STRUCTURAL_ENFORCE",
            "MERGED_MESSAGES_DROPPED": "SIMPLIFY",
            "PREFERENCE_STATED_BUT_NOT_CAPTURED": "SIMPLIFY",
            "PROVIDER_ERROR_REPEATED": "STRUCTURAL_ENFORCE",
            "EMPTY_RESPONSE": "STRUCTURAL_ENFORCE",
        }
        return defaults.get(signal_type, "SIMPLIFY")
