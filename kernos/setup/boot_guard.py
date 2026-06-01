"""Boot-verification + auto-rollback guard for self-modifying deploys.

The improve_kernos loop and the auto-update poller can land new commits and
restart onto them. A change that breaks *startup* (a syntax/import error, a
boot-path wiring bug) would otherwise brick the bot: the in-process recovery
loop can't run if the process can't boot. This module is the backstop.

Algorithm (flag-driven; systemd ``Restart=on-failure`` is the relauncher,
so no separate watcher process is needed):

1. When an update is applied, :func:`mark_update_pending` drops
   ``.update_pending = <new head>``. The new head is now "on probation."
2. ``start.sh`` runs ``python -m kernos.setup.boot_guard pre-launch`` BEFORE
   launching the server. :func:`pre_launch` counts boot attempts for the
   pending head; once it has failed to ready ``MAX_ATTEMPTS`` times it
   ``git reset --hard`` to ``.last_known_good`` and drops ``.rollback_notice``.
   systemd's next relaunch then boots the good code.
3. On a clean boot, ``on_ready`` calls :func:`mark_boot_ok`: it promotes
   ``.last_known_good = <current head>`` and clears the pending + attempt
   flags. Probation passed — and ``.last_known_good`` is ONLY ever a head
   that actually readied.
4. A readiness deadline (in-process, scheduled at startup) calls
   :func:`rollback_now` if ``on_ready`` never fires within the window — the
   "boots but hangs / won't connect" case.
5. :func:`consume_rollback_notice` is read on the next clean boot so the
   agent can tell the user the update failed and is parked on GitHub.

SAFETY: every entry point is a no-op unless ``.update_pending`` is set, so
NON-update boots are never touched. stdlib-only so a bad commit elsewhere
can't break the guard itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_MAX_BOOT_ATTEMPTS = int(os.getenv("KERNOS_BOOT_GUARD_MAX_ATTEMPTS", "2"))

PENDING = ".update_pending"
LAST_GOOD = ".last_known_good"
ATTEMPTS = ".boot_attempts"
NOTICE = ".rollback_notice"
BOOT_OK = ".boot_ok"


def _data_dir() -> Path:
    return Path(os.getenv("KERNOS_DATA_DIR", "./data"))


def _repo_dir() -> Path:
    # kernos/setup/boot_guard.py -> repo root is two parents up.
    return Path(__file__).resolve().parents[2]


def _f(name: str) -> Path:
    return _data_dir() / name


def _read(name: str) -> str:
    try:
        return _f(name).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _write(name: str, value: object) -> None:
    try:
        p = _f(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(value), encoding="utf-8")
    except Exception:
        pass


def _rm(name: str) -> None:
    try:
        _f(name).unlink()
    except Exception:
        pass


def _git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(_repo_dir()),
        capture_output=True, text=True, timeout=timeout,
    )


def _head() -> str:
    try:
        r = _git("rev-parse", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def mark_update_pending(new_head: str) -> None:
    """Called right before an update execv. Marks ``new_head`` on probation
    and resets the attempt counter. ``.last_known_good`` is left as-is — it
    is only ever set by :func:`mark_boot_ok` (a head that truly readied)."""
    if not new_head:
        return
    _write(PENDING, new_head)
    _rm(ATTEMPTS)


def pre_launch() -> bool:
    """Run by ``start.sh`` before launching the server. Returns True if a
    rollback was performed. No-op unless an update is on probation."""
    pending = _read(PENDING)
    if not pending:
        return False
    head = _head()
    if not head:
        return False
    if pending != head:
        # HEAD already moved off the probationary commit by other means.
        _rm(PENDING)
        _rm(ATTEMPTS)
        return False
    last_good = _read(LAST_GOOD)
    attempts = 0
    try:
        attempts = int(_read(ATTEMPTS) or "0")
    except ValueError:
        attempts = 0
    if attempts >= _MAX_BOOT_ATTEMPTS and last_good and last_good != head:
        return rollback_now(reason="crash_loop", failed_head=head)
    _write(ATTEMPTS, attempts + 1)
    return False


def rollback_now(*, reason: str, failed_head: str = "") -> bool:
    """``git reset --hard`` to ``.last_known_good`` and drop a notice for the
    agent. Returns True on a successful reset. No-op if there's no distinct
    good head to fall back to."""
    last_good = _read(LAST_GOOD)
    failed = failed_head or _head()
    if not last_good or last_good == failed:
        return False
    ok = False
    err = ""
    try:
        r = _git("reset", "--hard", last_good)
        ok = r.returncode == 0
        err = "" if ok else r.stderr.strip()[:500]
    except Exception as exc:  # pragma: no cover - defensive
        err = str(exc)[:500]
    _write(NOTICE, json.dumps({
        "failed_head": failed,
        "rolled_back_to": last_good,
        "reason": reason,
        "git_ok": ok,
        "git_err": err,
        "ts": int(time.time()),
    }))
    _rm(PENDING)
    _rm(ATTEMPTS)
    return ok


def mark_boot_ok(head: str = "") -> None:
    """Called from ``on_ready`` on a clean boot. Promotes the current head to
    last-known-good and clears probation."""
    head = head or _head()
    if head:
        _write(LAST_GOOD, head)
        _write(BOOT_OK, head)
    _rm(PENDING)
    _rm(ATTEMPTS)


def consume_rollback_notice() -> dict | None:
    """Read + clear the rollback notice (if any) for surfacing to the user."""
    raw = _read(NOTICE)
    if not raw:
        return None
    _rm(NOTICE)
    try:
        return json.loads(raw)
    except Exception:
        return None


def update_in_probation() -> str:
    """Return the head currently on probation (empty if none)."""
    pending = _read(PENDING)
    return pending if pending and pending == _head() else ""


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "pre-launch":
        try:
            rolled = pre_launch()
            if rolled:
                print("boot_guard: rolled back failed update", file=sys.stderr)
        except Exception as exc:  # never block launch
            print(f"boot_guard pre-launch error (continuing): {exc}",
                  file=sys.stderr)
    sys.exit(0)
