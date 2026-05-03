"""In-memory ring buffer of recent log records for /dump introspection.

Bot console output normally goes to stdout/stderr only — it streams past
the operator and isn't captured anywhere a slash command can read it.
That makes after-the-fact diagnosis of "what did the bot actually do?"
require terminal scrollback, which is fragile (lost on restart, hard to
share, breaks on long sessions).

This module installs a logging handler that retains the last N formatted
records in a deque, accessible via ``get_recent_log_lines()``. The /dump
slash command appends those lines as a ``=== RECENT LOG ===`` section so
substrate inspection and runtime evidence land in the same artifact.

Behavior:
- Default capacity 200 records (~50KB at typical line lengths).
- Capacity overridable via ``KERNOS_LOG_BUFFER_LINES`` env.
- Idempotent install — calling install twice doesn't duplicate handlers.
- Best-effort: if formatting raises, the failing record is silently
  dropped rather than crashing the logger.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from typing import Iterable

_DEFAULT_CAPACITY = 200


class LogRingBuffer(logging.Handler):
    """Fixed-capacity in-memory buffer of formatted log records.

    Thread-safe via the underlying deque + a small lock for the snapshot
    accessor. Designed to live for the lifetime of the process; never
    cleared automatically.
    """

    def __init__(self, capacity: int = _DEFAULT_CAPACITY) -> None:
        super().__init__()
        self._buffer: deque[str] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        # Same format the StreamHandler uses (server.py + repl.py) so the
        # /dump section reads identically to the live console output.
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            line = self.format(record)
        except Exception:
            return
        with self._lock:
            self._buffer.append(line)

    def snapshot(self, last_n: int | None = None) -> list[str]:
        """Return a copy of the most recent log lines, oldest-first.

        ``last_n`` caps the count when set (e.g. /dump only wants the
        most recent ~150 lines, not the entire 200-record buffer).
        """
        with self._lock:
            if last_n is None or last_n >= len(self._buffer):
                return list(self._buffer)
            # Take the tail.
            return list(self._buffer)[-last_n:]


# Module-level singleton — set by install_log_ring_buffer(), read by
# get_recent_log_lines(). One per process.
_singleton: LogRingBuffer | None = None


def install_log_ring_buffer(capacity: int | None = None) -> LogRingBuffer:
    """Attach the ring buffer to the root logger. Idempotent.

    Should be called once at boot, after the StreamHandler is set up
    (matching format), so live console output and /dump's RECENT LOG
    section are formatted identically.
    """
    global _singleton
    if _singleton is not None:
        return _singleton
    if capacity is None:
        try:
            capacity = int(os.getenv("KERNOS_LOG_BUFFER_LINES", str(_DEFAULT_CAPACITY)))
        except ValueError:
            capacity = _DEFAULT_CAPACITY
    handler = LogRingBuffer(capacity=capacity)
    handler.setLevel(logging.INFO)
    logging.root.addHandler(handler)
    _singleton = handler
    return handler


def get_recent_log_lines(last_n: int | None = None) -> list[str]:
    """Return recent log lines for inclusion in /dump or status output.

    Empty list when the ring buffer hasn't been installed (e.g. running
    inside a test that never calls install_log_ring_buffer()).
    """
    if _singleton is None:
        return []
    return _singleton.snapshot(last_n=last_n)
