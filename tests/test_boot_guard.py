"""Tests for the boot-verification + auto-rollback guard.

Uses a real temp git repo so the actual ``git reset --hard`` path runs.
"""
import json
import subprocess
from pathlib import Path

import pytest

from kernos.setup import boot_guard


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A temp git repo with two commits + a data dir, wired into boot_guard."""
    rd = tmp_path / "repo"
    rd.mkdir()
    dd = tmp_path / "data"
    dd.mkdir()

    def _g(*args):
        return subprocess.run(["git", *args], cwd=str(rd),
                              capture_output=True, text=True)

    _g("init", "-q")
    _g("config", "user.email", "t@t.t")
    _g("config", "user.name", "t")
    (rd / "f.txt").write_text("v1")
    _g("add", "."); _g("commit", "-q", "-m", "c1")
    head1 = _g("rev-parse", "HEAD").stdout.strip()
    (rd / "f.txt").write_text("v2")
    _g("add", "."); _g("commit", "-q", "-m", "c2")
    head2 = _g("rev-parse", "HEAD").stdout.strip()

    monkeypatch.setattr(boot_guard, "_repo_dir", lambda: rd)
    monkeypatch.setattr(boot_guard, "_data_dir", lambda: dd)
    monkeypatch.setattr(boot_guard, "_MAX_BOOT_ATTEMPTS", 2)
    return {"rd": rd, "dd": dd, "head1": head1, "head2": head2, "g": _g}


def test_pre_launch_noop_without_pending(repo):
    # Normal boot: no probation flag -> never touches anything.
    assert boot_guard.pre_launch() is False
    assert not (repo["dd"] / boot_guard.ATTEMPTS).exists()
    assert repo["g"]("rev-parse", "HEAD").stdout.strip() == repo["head2"]


def test_pre_launch_increments_then_rolls_back(repo):
    # head2 is on probation; head1 is the last known good.
    boot_guard._write(boot_guard.LAST_GOOD, repo["head1"])
    boot_guard.mark_update_pending(repo["head2"])

    # Attempt 1: under the cap -> increments, no rollback, HEAD unchanged.
    assert boot_guard.pre_launch() is False
    assert boot_guard._read(boot_guard.ATTEMPTS) == "1"
    assert repo["g"]("rev-parse", "HEAD").stdout.strip() == repo["head2"]

    # Attempt 2: under the cap (>= happens at 2) -> increments to 2.
    assert boot_guard.pre_launch() is False
    assert boot_guard._read(boot_guard.ATTEMPTS) == "2"

    # Attempt 3: attempts(2) >= MAX(2) -> ROLLBACK to head1.
    assert boot_guard.pre_launch() is True
    assert repo["g"]("rev-parse", "HEAD").stdout.strip() == repo["head1"]
    # Probation cleared; notice written.
    assert not (repo["dd"] / boot_guard.PENDING).exists()
    notice = json.loads(boot_guard._read(boot_guard.NOTICE))
    assert notice["failed_head"] == repo["head2"]
    assert notice["rolled_back_to"] == repo["head1"]
    assert notice["reason"] == "crash_loop"
    assert notice["git_ok"] is True


def test_mark_boot_ok_promotes_and_clears(repo):
    boot_guard.mark_update_pending(repo["head2"])
    boot_guard._write(boot_guard.ATTEMPTS, "1")
    boot_guard.mark_boot_ok(repo["head2"])
    assert boot_guard._read(boot_guard.LAST_GOOD) == repo["head2"]
    assert not (repo["dd"] / boot_guard.PENDING).exists()
    assert not (repo["dd"] / boot_guard.ATTEMPTS).exists()


def test_rollback_now_noop_without_distinct_good(repo):
    # No last_known_good recorded -> nothing to fall back to.
    assert boot_guard.rollback_now(reason="readiness_timeout") is False
    # last_good == current head -> also a no-op.
    boot_guard._write(boot_guard.LAST_GOOD, repo["head2"])
    assert boot_guard.rollback_now(reason="x", failed_head=repo["head2"]) is False


def test_readiness_timeout_rollback(repo):
    boot_guard._write(boot_guard.LAST_GOOD, repo["head1"])
    boot_guard.mark_update_pending(repo["head2"])
    assert boot_guard.rollback_now(reason="readiness_timeout") is True
    assert repo["g"]("rev-parse", "HEAD").stdout.strip() == repo["head1"]
    assert boot_guard.consume_rollback_notice()["reason"] == "readiness_timeout"
    # Consumed -> gone on second read.
    assert boot_guard.consume_rollback_notice() is None


def test_pending_for_different_head_is_cleared(repo):
    # Pending points at a head we're no longer on -> clear, don't roll back.
    boot_guard.mark_update_pending("deadbeef" * 5)
    assert boot_guard.pre_launch() is False
    assert not (repo["dd"] / boot_guard.PENDING).exists()
