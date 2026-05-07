"""Startup-time self-update.

Kernos runs continuously on operator hardware and is under frequent
development. Without a self-update path, operators run stale Kernos
until they remember to SSH in and ``git pull``. This module closes that
gap: on startup, fetch ``origin/{branch}``, compare to local HEAD, pull
if behind, reinstall dependencies, and restart the process via
``os.execv``.

Graceful degradation is the whole point of the design. Every failure
mode — not a git checkout, dirty working tree, network error, diverged
history, reinstall failure — produces a log line and continues startup
with the current code. Auto-update never blocks startup and never
leaves the process in a limbo state.

Entry point: :func:`enforce_or_continue`. May ``os.execv`` and never
return; may return normally with no side effects.

Post-update whisper: when an update is applied, the commit range is
written to ``{data_dir}/.auto_update_log.md`` before ``execv``. On the
fresh process, :func:`queue_pending_whisper` — called from ``on_ready``
after state is ready — converts the file into a queued Whisper for the
owner member so the first turn after restart summarizes what changed.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


LOG_FILENAME = ".auto_update_log.md"
MARKER_FILENAME = ".auto_update_pending"

#: AUTO_UPDATE log line prefix family. Consistent so operators can grep
#: for a single token and see the full update trajectory.
_LOG_PREFIX = "AUTO_UPDATE"


def _kernos_source_dir() -> Path:
    """Return the Kernos source root (repo root).

    ``kernos/setup/self_update.py`` → repo root is two directories up.
    """
    return Path(__file__).resolve().parent.parent.parent


def _effective_branch() -> str:
    return (os.getenv("KERNOS_UPDATE_BRANCH", "") or "main").strip() or "main"


def _auto_update_enabled() -> bool:
    val = (os.getenv("KERNOS_AUTO_UPDATE", "") or "on").strip().lower()
    return val == "on"


def _ignore_dirty_enabled() -> bool:
    """``KERNOS_AUTO_UPDATE_IGNORE_DIRTY=on`` bypasses the working-tree
    cleanliness check.

    The dirty check exists as a belt-and-suspenders guard, but
    ``git pull --ff-only`` already aborts safely if local changes
    would conflict. The check turns out to be over-cautious in two
    common cases:

    1. Tracked files physically removed from disk (``D`` in
       ``git status``) — the pull would resolve them.
    2. Local clones used for ad-hoc inspection where uncommitted
       changes exist but the operator still wants the update.

    Off by default. When on, the dirty status is logged at INFO and
    the update sequence continues; ``--ff-only`` provides the real
    safety boundary.
    """
    val = (os.getenv("KERNOS_AUTO_UPDATE_IGNORE_DIRTY", "") or "off").strip().lower()
    return val == "on"


def _verbose_enabled() -> bool:
    """``KERNOS_AUTO_UPDATE_VERBOSE`` is the operator-level master
    toggle for update notifications. Default ``on`` — the substrate
    queues a post-update whisper carrying the substrate-event data,
    and the agent's covenants decide what to surface in the agent's
    voice.

    AUTO-UPDATE-INFORMING-V1 revision: the env var was originally
    proposed for removal, but it has a real role as the
    pre-conversation operator opt-out. ``off`` means no whisper is
    queued at all — the agent never knows the update happened. The
    covenant layer, by contrast, governs *what* the agent says
    about updates that DO reach it (granularity and conditionality).

    Set ``off`` if you don't want update notifications at all.
    Leave ``on`` (default) and edit the agent's covenant in
    conversation to tune phrasing or scope.
    """
    val = (os.getenv("KERNOS_AUTO_UPDATE_VERBOSE", "") or "on").strip().lower()
    return val == "on"


_DEFAULT_UPDATE_TIME = (3, 0)


def _parse_update_time() -> tuple[int, int]:
    """Parse ``KERNOS_AUTO_UPDATE_TIME`` (``HH:MM`` 24-hour, server
    local clock) into a (hour, minute) tuple. Falls back to
    ``03:00`` and logs a warning on malformed input."""
    raw = (os.getenv("KERNOS_AUTO_UPDATE_TIME", "") or "").strip()
    if not raw:
        return _DEFAULT_UPDATE_TIME
    try:
        h_str, m_str = raw.split(":", 1)
        hour = int(h_str)
        minute = int(m_str)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"out of range: {raw!r}")
    except (ValueError, AttributeError):
        logger.warning(
            "%s_TIME_PARSE_FAILED: KERNOS_AUTO_UPDATE_TIME=%r is not "
            "valid HH:MM (24-hour) — falling back to default %02d:%02d",
            _LOG_PREFIX, raw,
            _DEFAULT_UPDATE_TIME[0], _DEFAULT_UPDATE_TIME[1],
        )
        return _DEFAULT_UPDATE_TIME
    return (hour, minute)


def _run_git(
    args: list[str], *, cwd: Path, timeout: int = 60,
) -> subprocess.CompletedProcess:
    """Run a git subprocess with captured output."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_pip_install(cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run ``pip install -e .`` for the dependency refresh step."""
    return subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@dataclass
class UpdateContext:
    """State threaded through the update sequence for logging + tests."""

    source_dir: Path
    branch: str
    enabled: bool
    #: The HEAD OID before any pull; captured so the commit-range log can
    #: render ``HEAD@{1}..HEAD`` reliably after the pull.
    pre_pull_head: str = ""


# ---------------------------------------------------------------------------
# Precondition checks
# ---------------------------------------------------------------------------


def _is_git_checkout(source_dir: Path) -> bool:
    return (source_dir / ".git").exists()


def _working_tree_clean(source_dir: Path) -> tuple[bool, str]:
    """Return (clean, status_output). Clean = no changes + no untracked files."""
    result = _run_git(["status", "--porcelain"], cwd=source_dir)
    if result.returncode != 0:
        return (False, result.stderr.strip() or "git status failed")
    return (not bool(result.stdout.strip()), result.stdout.strip())


# ---------------------------------------------------------------------------
# Sequence steps
# ---------------------------------------------------------------------------


def _fetch(source_dir: Path, branch: str) -> tuple[bool, str]:
    result = _run_git(["fetch", "origin", branch, "--quiet"], cwd=source_dir)
    if result.returncode != 0:
        return (False, result.stderr.strip() or "git fetch failed")
    return (True, "")


def _local_head(source_dir: Path) -> str:
    result = _run_git(["rev-parse", "HEAD"], cwd=source_dir)
    return result.stdout.strip() if result.returncode == 0 else ""


def _remote_head(source_dir: Path, branch: str) -> str:
    result = _run_git(
        ["rev-parse", f"origin/{branch}"], cwd=source_dir,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _is_ancestor(source_dir: Path, a: str, b: str) -> bool:
    """Return True if ``a`` is an ancestor of ``b`` (or equal).

    Used to detect "remote is strictly ahead of local" (local is ancestor
    of remote, and they're not equal).
    """
    result = _run_git(
        ["merge-base", "--is-ancestor", a, b], cwd=source_dir,
    )
    return result.returncode == 0


def _pull(source_dir: Path, branch: str) -> tuple[bool, str]:
    result = _run_git(
        ["pull", "--ff-only", "origin", branch], cwd=source_dir,
    )
    if result.returncode != 0:
        reason = (result.stderr + result.stdout).strip() or "git pull failed"
        return (False, reason)
    return (True, result.stdout.strip())


def _reinstall(source_dir: Path) -> tuple[bool, str]:
    result = _run_pip_install(source_dir)
    if result.returncode != 0:
        reason = (result.stderr + result.stdout).strip()[:500] or "pip install failed"
        return (False, reason)
    return (True, "")


def run_post_update_hooks(data_dir: str | Path) -> tuple[int, int, int]:
    """Run the shared install-hook runner in post_update phase.

    Per INSTALL-FOR-STOCK-CONNECTORS Section 7: self_update.py runs
    the SAME hook runner that `kernos setup` runs. New substrate
    pieces declare hooks; the updater honors them after pip
    install. Returns (succeeded, failed, skipped_check) counts so
    the surrounding update-log can summarize.

    Best-effort: hook failures are loud but non-fatal. The update
    completes regardless; failed hooks persist in the
    hook_status store and surface via `kernos services info`
    install_health.
    """
    try:
        from kernos.setup.install_hooks import (
            HookPhase,
            HookRunner,
            HookStatusStore,
            build_default_registry,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(f"{_LOG_PREFIX} install-hook import failed: {exc}")
        return (0, 0, 0)

    try:
        registry = build_default_registry()
        status_store = HookStatusStore(data_dir)
        runner = HookRunner(registry=registry, status_store=status_store)
        report = runner.run(
            phase=HookPhase.POST_UPDATE,
            invoked_by="self_update",
            data_dir=data_dir,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(f"{_LOG_PREFIX} install-hook runner raised: {exc}")
        return (0, 0, 0)

    return (
        len(report.succeeded),
        len(report.failed),
        len(report.skipped_check),
    )


def _commit_range_log(source_dir: Path, pre_pull_head: str) -> str:
    """Return the ``git log pre_pull_head..HEAD --oneline`` output."""
    if not pre_pull_head:
        return ""
    result = _run_git(
        ["log", f"{pre_pull_head}..HEAD", "--oneline"],
        cwd=source_dir,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _write_update_log(
    data_dir: str, pre_pull_head: str, branch: str, commits: str,
) -> None:
    """Persist the commit-range summary so the fresh process can surface it."""
    data_path = Path(data_dir)
    data_path.mkdir(parents=True, exist_ok=True)
    log_path = data_path / LOG_FILENAME
    marker_path = data_path / MARKER_FILENAME
    from kernos.utils import utc_now

    lines = [
        f"# Auto-update applied at {utc_now()}",
        f"Branch: `{branch}`",
        f"Previous HEAD: `{pre_pull_head[:12]}`",
        "",
        "## Commits pulled",
        "",
        "```",
        commits or "(commit range empty)",
        "```",
    ]
    log_path.write_text("\n".join(lines), encoding="utf-8")
    marker_path.write_text(utc_now(), encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def enforce_or_continue(
    *,
    data_dir: str | None = None,
    _execv: callable | None = None,
    _argv: list[str] | None = None,
) -> None:
    """Run the startup update sequence. May not return (via ``os.execv``).

    :param data_dir: override for ``KERNOS_DATA_DIR``; tests pass tmp paths.
    :param _execv: test hook to replace ``os.execv`` with a mock.
    :param _argv: test hook to replace ``sys.argv`` with a fixed list.
    """
    source_dir = _kernos_source_dir()
    branch = _effective_branch()
    enabled = _auto_update_enabled()

    if not enabled:
        logger.info(
            "%s_DISABLED: KERNOS_AUTO_UPDATE=off — skipping update check",
            _LOG_PREFIX,
        )
        return

    if not _is_git_checkout(source_dir):
        logger.debug(
            "%s_NOT_GIT: %s is not a git checkout — skipping",
            _LOG_PREFIX, source_dir,
        )
        return

    clean, status = _working_tree_clean(source_dir)
    if not clean:
        if _ignore_dirty_enabled():
            logger.info(
                "%s_DIRTY_OVERRIDE: KERNOS_AUTO_UPDATE_IGNORE_DIRTY=on — "
                "proceeding despite uncommitted changes (--ff-only will "
                "still abort on real conflicts):\n%s",
                _LOG_PREFIX, status[:500],
            )
        else:
            logger.warning(
                "%s_DIRTY: working tree has uncommitted changes or untracked "
                "files — skipping update (set KERNOS_AUTO_UPDATE_IGNORE_DIRTY=on "
                "to override):\n%s",
                _LOG_PREFIX, status[:500],
            )
            return

    ok, reason = _fetch(source_dir, branch)
    if not ok:
        logger.warning(
            "%s_FETCH_FAILED: %s — proceeding with current code",
            _LOG_PREFIX, reason[:500],
        )
        return

    local = _local_head(source_dir)
    remote = _remote_head(source_dir, branch)
    if not local or not remote:
        logger.warning(
            "%s_REV_LOOKUP_FAILED: local=%r remote=%r — skipping update",
            _LOG_PREFIX, local, remote,
        )
        return

    if local == remote:
        logger.info(
            "%s_CURRENT: local and origin/%s both at %s — no update available",
            _LOG_PREFIX, branch, local[:12],
        )
        return

    if not _is_ancestor(source_dir, local, remote):
        logger.error(
            "%s_DIVERGED: local HEAD %s is not an ancestor of origin/%s %s "
            "— history has diverged, skipping update",
            _LOG_PREFIX, local[:12], branch, remote[:12],
        )
        return

    pre_pull_head = local
    logger.info(
        "%s_PULLING: local=%s → remote=%s on origin/%s",
        _LOG_PREFIX, local[:12], remote[:12], branch,
    )
    ok, reason = _pull(source_dir, branch)
    if not ok:
        logger.error(
            "%s_PULL_FAILED: %s — proceeding with current code",
            _LOG_PREFIX, reason[:500],
        )
        return

    logger.info("%s_REINSTALLING: pip install -e .", _LOG_PREFIX)
    ok, reason = _reinstall(source_dir)
    if not ok:
        # Loud: reinstall failure likely causes downstream breakage. We
        # still proceed to restart because the new code is already in
        # place; reinstall might succeed on the next startup after the
        # operator intervenes.
        logger.error(
            "%s_REINSTALL_FAILED: %s — continuing startup but dependency "
            "state may be inconsistent",
            _LOG_PREFIX, reason,
        )

    resolved_data_dir = data_dir or os.getenv("KERNOS_DATA_DIR", "./data")

    # INSTALL-FOR-STOCK-CONNECTORS Section 7 (the design review edit #2): run the
    # shared install-hook runner after pip install so substrate that
    # needs install-time work (e.g. browser binaries, directory
    # permissions) gets handled. Fresh installs invoke the same
    # runner from `kernos setup`; updates invoke it here.
    try:
        succeeded, failed, skipped = run_post_update_hooks(resolved_data_dir)
        logger.info(
            "%s_HOOKS: succeeded=%d failed=%d skipped=%d",
            _LOG_PREFIX, succeeded, failed, skipped,
        )
        if failed:
            logger.warning(
                "%s_HOOKS_FAILED: %d hook(s) failed — see "
                "data/install/hook_status.json or `kernos services info`",
                _LOG_PREFIX, failed,
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "%s_HOOKS_RAISED: %s — update still applied",
            _LOG_PREFIX, exc,
        )
    try:
        commits = _commit_range_log(source_dir, pre_pull_head)
        _write_update_log(resolved_data_dir, pre_pull_head, branch, commits)
    except Exception as exc:
        logger.warning(
            "%s_LOG_WRITE_FAILED: %s — update still applied",
            _LOG_PREFIX, exc,
        )

    logger.info(
        "%s_RESTARTING: execv(%s, %s)",
        _LOG_PREFIX, sys.executable, _argv or sys.argv,
    )
    execv = _execv or os.execv
    execv(sys.executable, [sys.executable, *(_argv or sys.argv)])
    # Unreachable in real execution; tests with a mock execv fall through.


# ---------------------------------------------------------------------------
# Scheduled background pull (no execv — applies on next natural restart)
# ---------------------------------------------------------------------------


def _pull_only(*, data_dir: str | None = None) -> bool:
    """Run the same fetch + ancestry check + ff-only pull + reinstall
    sequence as :func:`enforce_or_continue`, but stop short of
    ``os.execv``. New code lands on disk; the running process keeps
    its old imports until next natural restart.

    Returns True if a pull applied (and the update log was written),
    False otherwise. Every failure mode is logged and absorbed —
    the scheduler retries on its next tick.

    AUTO-UPDATE-BEHAVIOR-V1: used by the daily cron task. Distinct
    from ``enforce_or_continue`` because the cron must NOT restart
    the process — that's the structural difference the spec calls
    for.
    """
    source_dir = _kernos_source_dir()
    branch = _effective_branch()

    if not _is_git_checkout(source_dir):
        logger.debug(
            "%s_CRON_NOT_GIT: %s is not a git checkout — skipping",
            _LOG_PREFIX, source_dir,
        )
        return False

    clean, status = _working_tree_clean(source_dir)
    if not clean:
        if _ignore_dirty_enabled():
            logger.info(
                "%s_CRON_DIRTY_OVERRIDE: KERNOS_AUTO_UPDATE_IGNORE_DIRTY=on — "
                "proceeding despite uncommitted changes:\n%s",
                _LOG_PREFIX, status[:500],
            )
        else:
            logger.warning(
                "%s_CRON_DIRTY: working tree has uncommitted changes — "
                "skipping scheduled pull (set "
                "KERNOS_AUTO_UPDATE_IGNORE_DIRTY=on to override):\n%s",
                _LOG_PREFIX, status[:500],
            )
            return False

    ok, reason = _fetch(source_dir, branch)
    if not ok:
        logger.warning(
            "%s_CRON_FETCH_FAILED: %s — skipping this window",
            _LOG_PREFIX, reason[:500],
        )
        return False

    local = _local_head(source_dir)
    remote = _remote_head(source_dir, branch)
    if not local or not remote:
        logger.warning(
            "%s_CRON_REV_LOOKUP_FAILED: local=%r remote=%r",
            _LOG_PREFIX, local, remote,
        )
        return False

    if local == remote:
        logger.info(
            "%s_CRON_CURRENT: local and origin/%s both at %s",
            _LOG_PREFIX, branch, local[:12],
        )
        return False

    if not _is_ancestor(source_dir, local, remote):
        logger.warning(
            "%s_CRON_DIVERGED: local %s not an ancestor of origin/%s "
            "%s — skipping",
            _LOG_PREFIX, local[:12], branch, remote[:12],
        )
        return False

    pre_pull_head = local
    logger.info(
        "%s_CRON_PULLING: %s → %s on origin/%s",
        _LOG_PREFIX, local[:12], remote[:12], branch,
    )
    ok, reason = _pull(source_dir, branch)
    if not ok:
        logger.warning(
            "%s_CRON_PULL_FAILED: %s",
            _LOG_PREFIX, reason[:500],
        )
        return False

    ok, reason = _reinstall(source_dir)
    if not ok:
        logger.warning(
            "%s_CRON_REINSTALL_FAILED: %s — pull landed but deps may be "
            "inconsistent until next natural restart",
            _LOG_PREFIX, reason,
        )

    resolved_data_dir = data_dir or os.getenv("KERNOS_DATA_DIR", "./data")
    try:
        commits = _commit_range_log(source_dir, pre_pull_head)
        _write_update_log(resolved_data_dir, pre_pull_head, branch, commits)
    except Exception as exc:
        logger.warning(
            "%s_CRON_LOG_WRITE_FAILED: %s — pull still applied",
            _LOG_PREFIX, exc,
        )
    logger.info(
        "%s_CRON_APPLIED: %s → %s. New code applies on next restart.",
        _LOG_PREFIX, pre_pull_head[:12], remote[:12],
    )
    return True


def _seconds_until_next(hour: int, minute: int) -> float:
    """Compute seconds from now until the next occurrence of
    ``hour:minute`` in server local time. If we're already past
    today's slot, returns the offset to tomorrow's."""
    from datetime import datetime, timedelta

    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def scheduled_update_loop(
    *,
    data_dir: str | None = None,
    _pull: callable | None = None,
    _sleep: callable | None = None,
) -> None:
    """Daily background pull at ``KERNOS_AUTO_UPDATE_TIME``. Runs as
    an asyncio task launched at server startup; loops indefinitely
    until cancelled.

    Disabled when ``KERNOS_AUTO_UPDATE=off`` — the loop logs once
    and returns. Distinct from :func:`enforce_or_continue` (cold
    start / pre-bind) — this is the long-running daily window.

    Test hooks: ``_pull`` overrides :func:`_pull_only`; ``_sleep``
    overrides :func:`asyncio.sleep`.
    """
    import asyncio

    if not _auto_update_enabled():
        logger.info(
            "%s_CRON_DISABLED: KERNOS_AUTO_UPDATE=off — scheduled "
            "loop not started",
            _LOG_PREFIX,
        )
        return

    pull_fn = _pull or _pull_only
    sleep_fn = _sleep or asyncio.sleep
    hour, minute = _parse_update_time()

    logger.info(
        "%s_CRON_SCHEDULED: daily pull at %02d:%02d (server local)",
        _LOG_PREFIX, hour, minute,
    )

    while True:
        seconds = _seconds_until_next(hour, minute)
        try:
            await sleep_fn(seconds)
        except asyncio.CancelledError:
            logger.info("%s_CRON_CANCELLED: scheduled loop stopped", _LOG_PREFIX)
            raise
        try:
            pull_fn(data_dir=data_dir)
        except Exception as exc:
            logger.warning(
                "%s_CRON_RAISED: pull task raised %s — continuing loop",
                _LOG_PREFIX, exc,
            )


# ---------------------------------------------------------------------------
# Post-restart whisper queueing — substrate event delivery to the agent
# ---------------------------------------------------------------------------


_UPDATE_EVENT_COMMIT_CAP = 5


def format_update_event_text(log_text: str) -> str:
    """Render the auto-update log as a structured substrate-event
    description for the agent's situation context.

    AUTO-UPDATE-INFORMING-V1: the substrate does NOT pre-phrase a
    user-facing message. The agent reads this event description
    plus its covenants (which include a default "tell me about
    updates" rule) and produces the user-facing surfacing in its
    own voice.

    The text is event-shaped (data + marker), not response-shaped
    (no "I just updated" first-person framing). The marker
    ``[SUBSTRATE_EVENT: kernos_self_updated]`` lets the agent
    recognize this as an event to optionally surface, not a
    pre-rendered message to deliver verbatim.
    """
    in_code = False
    commits: list[tuple[str, str]] = []  # (short_hash, subject)
    for line in log_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code and stripped:
            parts = stripped.split(maxsplit=1)
            if len(parts) >= 2:
                commits.append((parts[0], parts[1]))
            elif parts:
                commits.append((parts[0], ""))

    head = commits[0] if commits else None
    capped = commits[:_UPDATE_EVENT_COMMIT_CAP]
    commit_lines = "\n".join(
        f"  - {h} {s}" if s else f"  - {h}"
        for h, s in capped
    ) or "  (commit range empty)"
    head_hash = head[0] if head else "(unknown)"
    total = len(commits)
    return (
        "[SUBSTRATE_EVENT: kernos_self_updated]\n"
        f"Kernos pulled new code from origin and applied it. "
        f"Now at commit {head_hash}.\n"
        f"{total} commit{'s' if total != 1 else ''} since previous head"
        + (f" (showing {len(capped)} most recent):" if total > len(capped) else ":")
        + f"\n{commit_lines}\n"
        f"Full log persisted at `{LOG_FILENAME}` in the data directory."
    )


# Backwards-compatibility alias for tests that referenced the old name.
_format_whisper_summary = format_update_event_text


async def queue_pending_whisper(
    *, state, instance_id: str, data_dir: str,
) -> bool:
    """If an auto-update completed on the previous startup, queue a Whisper.

    Called from ``server.on_ready`` after state + instance_db are ready
    but before the handler starts receiving turns. Returns True if a
    whisper was queued, False otherwise.

    AUTO-UPDATE-INFORMING-V1: gated by ``KERNOS_AUTO_UPDATE_VERBOSE``.
    When verbose is ``off``, no whisper is queued — the operator opted
    out at the substrate level and the agent never sees the event.
    When ``on`` (default), the whisper is queued carrying the
    substrate-event data; the agent's covenants govern what it
    surfaces in its own voice.

    The log file is left in place as a persistent record. Only the
    pending marker gets removed — the whisper is a one-time surface, the
    log is durable diagnostic artifact.
    """
    log_path = Path(data_dir) / LOG_FILENAME
    marker_path = Path(data_dir) / MARKER_FILENAME
    if not marker_path.exists() or not log_path.exists():
        return False

    if not _verbose_enabled():
        # Operator-level opt-out. Clear the marker so we don't queue
        # on subsequent restarts either, but leave the log file as
        # the durable diagnostic artifact.
        logger.info(
            "%s_WHISPER_SKIP: KERNOS_AUTO_UPDATE_VERBOSE=off — "
            "skipping post-update whisper queue",
            _LOG_PREFIX,
        )
        try:
            marker_path.unlink()
        except Exception:
            pass
        return False

    try:
        log_text = log_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("%s_LOG_READ_FAILED: %s", _LOG_PREFIX, exc)
        try:
            marker_path.unlink()
        except Exception:
            pass
        return False

    event_text = format_update_event_text(log_text)
    from kernos.kernel.awareness import Whisper, generate_whisper_id
    from kernos.utils import utc_now

    # AUTO-UPDATE-INFORMING-V1: the whisper carries the substrate
    # event for the agent's situation context. The agent reads this
    # alongside its covenants (which include a default "tell me
    # about updates" preference) and produces user-facing
    # phrasing in its own voice. Substrate does not pre-phrase.
    whisper = Whisper(
        whisper_id=generate_whisper_id(),
        insight_text=event_text,
        delivery_class="ambient",
        source_space_id="",
        target_space_id="",
        supporting_evidence=[],
        reasoning_trace=(
            "Substrate event: kernos_self_updated. "
            "The user's covenants determine whether and how to surface "
            f"this in conversation. Full log at {LOG_FILENAME}."
        ),
        knowledge_entry_id="",
        foresight_signal="auto_update:applied",
        created_at=utc_now(),
        owner_member_id="",  # instance-wide; visible to whoever takes the next turn
    )
    try:
        await state.save_whisper(instance_id, whisper)
        logger.info(
            "%s_WHISPER_QUEUED: instance=%s whisper=%s",
            _LOG_PREFIX, instance_id, whisper.whisper_id,
        )
    except Exception as exc:
        logger.warning("%s_WHISPER_SAVE_FAILED: %s", _LOG_PREFIX, exc)
        return False
    finally:
        try:
            marker_path.unlink()
        except Exception:
            pass
    return True
