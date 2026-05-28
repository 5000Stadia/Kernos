"""Runtime Trace — structured event capture for diagnostic visibility.

Per-tenant JSONL ring buffer capturing provider errors, tool failures,
gate decisions, timing, plan lifecycle, and friction signals. The agent
reads this via the read_runtime_trace kernel tool.

Improvement Loop Tier 2, Pass 1.
"""
import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field, asdict
from functools import partial
from pathlib import Path
from typing import Any

from kernos.utils import utc_now, _safe_name

logger = logging.getLogger(__name__)

MAX_TURNS = 200  # Ring buffer capacity


async def _run_sync_io(fn, *args, **kwargs):
    return await asyncio.get_running_loop().run_in_executor(
        None, partial(fn, *args, **kwargs),
    )


@dataclass
class TraceEvent:
    """A single structured event captured during a turn."""
    turn_id: str
    timestamp: str
    level: str  # "info" | "warning" | "error"
    source: str  # module that emitted the event
    event: str  # event name (e.g., "CODEX_STREAM_ERROR")
    detail: str  # human-readable detail
    phase: str = ""  # pipeline phase if applicable
    duration_ms: int | None = None


def generate_turn_id() -> str:
    return f"turn_{uuid.uuid4().hex[:8]}"


def _append_events_sync(path: Path, events: list["TraceEvent"]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(asdict(evt), ensure_ascii=False) + "\n")


class TurnEventCollector:
    """Collects structured events during a single turn."""

    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self.events: list[TraceEvent] = []

    def record(
        self,
        level: str,
        source: str,
        event: str,
        detail: str,
        phase: str = "",
        duration_ms: int | None = None,
    ) -> None:
        self.events.append(TraceEvent(
            turn_id=self.turn_id,
            timestamp=utc_now(),
            level=level,
            source=source,
            event=event,
            detail=detail[:500],
            phase=phase,
            duration_ms=duration_ms,
        ))


class RuntimeTrace:
    """Per-tenant structured event log with ring buffer."""

    def __init__(self, data_dir: str) -> None:
        self._data_dir = data_dir
        self._trace_locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, path: Path) -> asyncio.Lock:
        """Per-trace-path lock for serializing append/read/rotate writes."""
        key = os.fspath(path)
        if key not in self._trace_locks:
            self._trace_locks[key] = asyncio.Lock()
        return self._trace_locks[key]

    def _trace_path(self, instance_id: str) -> Path:
        return (
            Path(self._data_dir)
            / _safe_name(instance_id)
            / "diagnostics"
            / "runtime_trace.jsonl"
        )

    async def append_turn(self, instance_id: str, events: list[TraceEvent]) -> None:
        """Append a turn's events to the trace file. Rotates if needed."""
        if not events:
            return

        path = self._trace_path(instance_id)

        async with self._get_lock(path):
            await _run_sync_io(path.parent.mkdir, parents=True, exist_ok=True)
            await _run_sync_io(_append_events_sync, path, events)

            # Check rotation
            try:
                text = await _run_sync_io(path.read_text, encoding="utf-8")
                lines = text.strip().split("\n")
                turn_ids = set()
                for line in lines:
                    try:
                        d = json.loads(line)
                        turn_ids.add(d.get("turn_id", ""))
                    except json.JSONDecodeError:
                        continue
                if len(turn_ids) > MAX_TURNS:
                    await self._rotate_async(path, lines, turn_ids)
            except Exception as exc:
                logger.debug("TRACE_ROTATE: check failed: %s", exc)

        logger.info("TRACE_APPEND: turn=%s events=%d", events[0].turn_id, len(events))

    async def _rotate_async(self, path: Path, lines: list[str], turn_ids: set[str]) -> None:
        await _run_sync_io(self._rotate, path, lines, turn_ids)

    def _rotate(self, path: Path, lines: list[str], turn_ids: set[str]) -> None:
        """Remove oldest turns to stay within MAX_TURNS."""
        # Parse all events with turn_ids
        events_by_turn: dict[str, list[str]] = {}
        for line in lines:
            try:
                d = json.loads(line)
                tid = d.get("turn_id", "")
                events_by_turn.setdefault(tid, []).append(line)
            except json.JSONDecodeError:
                continue

        # Sort turn_ids by first event timestamp
        def _first_ts(tid: str) -> str:
            evts = events_by_turn.get(tid, [])
            if evts:
                try:
                    return json.loads(evts[0]).get("timestamp", "")
                except json.JSONDecodeError:
                    pass
            return ""

        sorted_turns = sorted(events_by_turn.keys(), key=_first_ts)
        to_remove = len(sorted_turns) - MAX_TURNS
        if to_remove <= 0:
            return

        keep_turns = set(sorted_turns[to_remove:])
        kept_lines = [
            line for line in lines
            if _get_turn_id(line) in keep_turns
        ]
        path.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
        logger.info("TRACE_ROTATE: removed=%d remaining=%d", to_remove, len(keep_turns))

    async def read(
        self,
        instance_id: str,
        turns: int = 10,
        since: str | None = None,
        filter_level: str | None = None,
        turn_id: str | None = None,
    ) -> list[dict]:
        """Read trace events with optional filtering.

        Args:
            turns: Last N turns (max 50)
            since: ISO timestamp — only events after this
            filter_level: "error"|"warning"|"gate"|"timing"|"friction"|"provider"|"tool"
            turn_id: Specific turn ID
        """
        path = self._trace_path(instance_id)
        if not path.exists():
            return []

        turns = min(turns, 50)

        try:
            lines = path.read_text(encoding="utf-8").strip().split("\n")
        except OSError:
            return []

        events: list[dict] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Filter by turn_id
            if turn_id and d.get("turn_id") != turn_id:
                continue

            # Filter by since
            if since and d.get("timestamp", "") < since:
                continue

            # Filter by level/category
            if filter_level:
                fl = filter_level.lower()
                if fl in ("error", "warning", "info"):
                    if d.get("level") != fl:
                        continue
                elif fl == "gate":
                    if "GATE" not in d.get("event", ""):
                        continue
                elif fl == "timing":
                    if "TIMING" not in d.get("event", ""):
                        continue
                elif fl == "friction":
                    if not any(k in d.get("event", "") for k in ("FRICTION", "BEHAVIORAL")):
                        continue
                elif fl == "provider":
                    if not any(k in d.get("event", "") for k in ("CODEX", "OLLAMA", "FALLBACK", "RETRY")):
                        continue
                elif fl == "tool":
                    if not any(k in d.get("event", "") for k in ("TOOL_FAILED", "TOOL_ERROR")):
                        continue

            events.append(d)

        # Limit to last N turns
        if not turn_id:
            seen_turns: list[str] = []
            for e in reversed(events):
                tid = e.get("turn_id", "")
                if tid not in seen_turns:
                    seen_turns.append(tid)
                if len(seen_turns) > turns:
                    break
            keep_turns = set(seen_turns[:turns])
            events = [e for e in events if e.get("turn_id") in keep_turns]

        return events


def _get_turn_id(line: str) -> str:
    try:
        return json.loads(line).get("turn_id", "")
    except (json.JSONDecodeError, AttributeError):
        return ""


# ---------------------------------------------------------------------------
# Kernel tool schema
# ---------------------------------------------------------------------------

READ_RUNTIME_TRACE_TOOL = {
    "name": "read_runtime_trace",
    "description": (
        "Read the runtime trace log — structured events from recent turns. "
        "Shows provider errors, tool failures, gate decisions, timing, and plan events. "
        "Use this to diagnose what happened during a specific turn or recent turns."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "turns": {
                "type": "integer",
                "description": "Number of recent turns to show (default 10, max 50)",
            },
            "filter": {
                "type": "string",
                "description": "Filter by category: error, warning, gate, timing, friction, provider, tool",
            },
            "turn_id": {
                "type": "string",
                "description": "Show events for a specific turn ID",
            },
        },
        "required": [],
    },
}
