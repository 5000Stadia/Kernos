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


# ---------------------------------------------------------------------
# Persistent file capture (LOG-PERSIST-V1, 2026-05-19)
# ---------------------------------------------------------------------
#
# The ring buffer is in-memory only. When the bot crashes or gets
# manually restarted (e.g. to recover from a stuck gateway), the
# buffer is wiped and post-hoc RCA becomes impossible. Codex
# investigation of the 2026-05-19 14:24 silent-gateway failure
# pointed at "asyncio loop blocked starving discord.py heartbeat"
# as the likely root cause — discord.py logs `Heartbeat blocked`
# warnings when this happens, but we couldn't see them because
# logs only existed in memory and got nuked by the recovery
# restart.
#
# This handler writes ALL log records to a rotating file under
# data/<instance>/diagnostics/server.log. Survives restarts. Lets
# the next investigation actually have evidence.

_file_handler_singleton: logging.Handler | None = None


def install_log_file_handler(
    *, data_dir: str, max_bytes: int | None = None, backup_count: int | None = None,
) -> logging.Handler | None:
    """Attach a rotating file handler to the root logger.

    Default cap: 10 MB per file, 5 backups = 50 MB ceiling.
    Tunable via ``KERNOS_LOG_FILE_MAX_BYTES`` / ``KERNOS_LOG_FILE_BACKUP_COUNT``.

    Skipped (returns None) when ``data_dir`` is empty or unwritable —
    the bot must continue running even if log persistence isn't
    possible. Idempotent.
    """
    from logging.handlers import RotatingFileHandler
    from pathlib import Path
    global _file_handler_singleton
    if _file_handler_singleton is not None:
        return _file_handler_singleton
    if not data_dir:
        return None
    if max_bytes is None:
        try:
            max_bytes = int(
                os.getenv("KERNOS_LOG_FILE_MAX_BYTES", str(10 * 1024 * 1024))
            )
        except ValueError:
            max_bytes = 10 * 1024 * 1024
    if backup_count is None:
        try:
            backup_count = int(
                os.getenv("KERNOS_LOG_FILE_BACKUP_COUNT", "5")
            )
        except ValueError:
            backup_count = 5

    # Resolve the diagnostics dir under the instance root, falling
    # back to data_dir / diagnostics for non-multi-tenant configs.
    instance_id = os.getenv("KERNOS_INSTANCE_ID", "")
    if instance_id:
        from kernos.utils import _safe_name
        log_root = Path(data_dir) / _safe_name(instance_id) / "diagnostics"
    else:
        log_root = Path(data_dir) / "diagnostics"
    try:
        log_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    log_path = log_root / "server.log"

    try:
        handler = RotatingFileHandler(
            str(log_path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    except OSError:
        return None

    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logging.root.addHandler(handler)

    # Critical: ensure discord.py's own loggers actually emit at INFO
    # so we capture `Heartbeat blocked` and reconnect-cycle messages.
    # discord.py defaults to WARNING for most of its loggers; bumping
    # discord.gateway to INFO without going to DEBUG (which is very
    # chatty) gives us the failure-mode signals without firehose.
    logging.getLogger("discord.gateway").setLevel(logging.INFO)
    logging.getLogger("discord.client").setLevel(logging.INFO)
    logging.getLogger("discord.http").setLevel(logging.WARNING)  # noisy at INFO

    _file_handler_singleton = handler
    return handler


def get_log_file_path() -> str | None:
    """Return the absolute path of the active log file, or None when
    the file handler isn't installed."""
    if _file_handler_singleton is None:
        return None
    base = getattr(_file_handler_singleton, "baseFilename", None)
    return base if isinstance(base, str) else None


def ensure_log_file_handler_attached() -> bool:
    """Defensive re-install check (2026-05-20).

    Live observation: on the live bot the RotatingFileHandler stopped
    writing at 18:46:55 PT even though the ring buffer kept capturing
    records — so the handler was somehow detached from
    ``logging.root.handlers`` while the in-memory ring buffer wasn't.
    Cause unconfirmed; likely candidates include discord.py's
    ``setup_logging`` interaction or a third-party handler-list
    manipulation we haven't tracked down.

    This check is meant to be called periodically (from
    GatewayHealthObserver's tick): if the singleton handler exists
    but isn't currently in ``logging.root.handlers``, re-attach it.
    Returns True if a re-attach was needed (caller can log loud),
    False if everything was already correct.
    """
    if _file_handler_singleton is None:
        return False
    if _file_handler_singleton in logging.root.handlers:
        return False
    logging.root.addHandler(_file_handler_singleton)
    return True
