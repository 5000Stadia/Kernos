"""Tests for KERNOS_AUTO_UPDATE startup self-update.

Spec reference: SPEC-KERNOS-AUTO-UPDATE expected behaviors 1-11.

Every test mocks ``subprocess.run`` and ``os.execv`` so we never actually
run git, pip, or replace the test process.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from kernos.setup import self_update
from kernos.setup.self_update import (
    LOG_FILENAME,
    MARKER_FILENAME,
    _format_whisper_summary,
    enforce_or_continue,
    queue_pending_whisper,
)


def _completed(
    *, returncode: int = 0, stdout: str = "", stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "KERNOS_AUTO_UPDATE",
        "KERNOS_UPDATE_BRANCH",
        "KERNOS_DATA_DIR",
        "KERNOS_AUTO_UPDATE_IGNORE_DIRTY",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Point ``_kernos_source_dir`` at a fake repo with a .git directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setattr(self_update, "_kernos_source_dir", lambda: repo)
    return repo


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("KERNOS_DATA_DIR", str(d))
    return d


# ---------------------------------------------------------------------------
# Pre-update precondition tests (no subprocess mocks needed)
# ---------------------------------------------------------------------------


class TestNotGitCheckout:
    """Expected behavior #4."""

    def test_silent_skip_when_no_dot_git(self, tmp_path, monkeypatch, data_dir):
        not_repo = tmp_path / "not_a_repo"
        not_repo.mkdir()
        monkeypatch.setattr(self_update, "_kernos_source_dir", lambda: not_repo)

        # Fails if subprocess.run gets called — it shouldn't.
        called = {"git": 0}

        def _fail(*a, **kw):
            called["git"] += 1
            raise AssertionError("subprocess.run should not be called")

        monkeypatch.setattr(self_update.subprocess, "run", _fail)
        execv_mock = MagicMock()
        enforce_or_continue(_execv=execv_mock, _argv=["server.py"])
        assert called["git"] == 0
        execv_mock.assert_not_called()


class TestAutoUpdateDisabled:
    """Expected behavior #3."""

    def test_disabled_skips_all(self, fake_repo, data_dir, monkeypatch):
        monkeypatch.setenv("KERNOS_AUTO_UPDATE", "off")

        def _fail(*a, **kw):
            raise AssertionError("subprocess.run should not be called")

        monkeypatch.setattr(self_update.subprocess, "run", _fail)
        execv_mock = MagicMock()
        enforce_or_continue(_execv=execv_mock, _argv=["server.py"])
        execv_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Tests that exercise the subprocess sequence
# ---------------------------------------------------------------------------


def _install_run_chain(monkeypatch, responses: list):
    """Install a subprocess.run mock that returns the given responses in order."""
    q = list(responses)

    def _run(args, **kwargs):
        if not q:
            raise AssertionError(f"Unexpected subprocess.run call: {args}")
        return q.pop(0)

    monkeypatch.setattr(self_update.subprocess, "run", _run)
    return q


class TestDirtyTree:
    """Expected behavior #5."""

    def test_dirty_tree_skips_update(self, fake_repo, data_dir, monkeypatch):
        _install_run_chain(monkeypatch, [
            _completed(stdout=" M some_file.py\n"),  # git status --porcelain
        ])
        execv_mock = MagicMock()
        enforce_or_continue(_execv=execv_mock, _argv=["server.py"])
        execv_mock.assert_not_called()

    def test_ignore_dirty_proceeds_through_pull(
        self, fake_repo, data_dir, monkeypatch,
    ):
        """KERNOS_AUTO_UPDATE_IGNORE_DIRTY=on bypasses the dirty check
        and proceeds through fetch + pull + reinstall + execv."""
        monkeypatch.setenv("KERNOS_AUTO_UPDATE_IGNORE_DIRTY", "on")
        _install_run_chain(monkeypatch, [
            _completed(stdout=" D data/diagnostics/foo.md\n"),    # dirty status
            _completed(),                                          # fetch
            _completed(stdout="aaaaaaaaaaaa\n"),                   # local head
            _completed(stdout="bbbbbbbbbbbb\n"),                   # remote head
            _completed(returncode=0),                              # ancestor check
            _completed(stdout="Fast-forward\n"),                   # pull
            _completed(returncode=0),                              # pip install
            _completed(stdout="bbbbbbbbbbbb cleanup\n"),           # log range
        ])
        execv_mock = MagicMock()
        enforce_or_continue(_execv=execv_mock, _argv=["server.py"])
        execv_mock.assert_called_once()
        assert (data_dir / LOG_FILENAME).is_file()

    def test_ignore_dirty_off_still_skips(
        self, fake_repo, data_dir, monkeypatch,
    ):
        """Override is opt-in: anything other than 'on' keeps the guard."""
        monkeypatch.setenv("KERNOS_AUTO_UPDATE_IGNORE_DIRTY", "off")
        _install_run_chain(monkeypatch, [
            _completed(stdout=" M some_file.py\n"),
        ])
        execv_mock = MagicMock()
        enforce_or_continue(_execv=execv_mock, _argv=["server.py"])
        execv_mock.assert_not_called()


class TestCurrentHead:
    """Expected behavior #1."""

    def test_current_head_no_action(self, fake_repo, data_dir, monkeypatch):
        _install_run_chain(monkeypatch, [
            _completed(stdout=""),                           # status --porcelain
            _completed(),                                    # fetch
            _completed(stdout="aaaaaaaaaaaa\n"),             # rev-parse HEAD
            _completed(stdout="aaaaaaaaaaaa\n"),             # rev-parse origin/main
        ])
        execv_mock = MagicMock()
        enforce_or_continue(_execv=execv_mock, _argv=["server.py"])
        execv_mock.assert_not_called()


class TestBehindHeadHappyPath:
    """Expected behaviors #2, #9, #10 — pull + reinstall + execv + log written."""

    def test_behind_head_triggers_pull_reinstall_execv(
        self, fake_repo, data_dir, monkeypatch,
    ):
        _install_run_chain(monkeypatch, [
            _completed(stdout=""),                                 # status
            _completed(),                                          # fetch
            _completed(stdout="aaaaaaaaaaaa\n"),                   # local head
            _completed(stdout="bbbbbbbbbbbb\n"),                   # remote head
            _completed(returncode=0),                              # merge-base ancestor
            _completed(stdout="Fast-forward\n"),                   # pull
            _completed(returncode=0),                              # pip install
            _completed(stdout="bbbbbbbbbbbb feat: thing\n"
                              "ccccccccccc fix: other\n"),         # log
        ])
        execv_mock = MagicMock()
        enforce_or_continue(_execv=execv_mock, _argv=["server.py", "--flag"])

        execv_mock.assert_called_once()
        exec_args = execv_mock.call_args[0]
        assert exec_args[1][1:] == ["server.py", "--flag"]

        # Log + marker written
        log_path = data_dir / LOG_FILENAME
        marker_path = data_dir / MARKER_FILENAME
        assert log_path.is_file()
        assert marker_path.is_file()
        log_text = log_path.read_text()
        assert "feat: thing" in log_text
        assert "Previous HEAD: `aaaaaaaaaaaa`" in log_text
        assert "Branch: `main`" in log_text


class TestDivergedHistory:
    """Expected behavior #7."""

    def test_diverged_skips_update(self, fake_repo, data_dir, monkeypatch):
        _install_run_chain(monkeypatch, [
            _completed(stdout=""),                          # status
            _completed(),                                   # fetch
            _completed(stdout="aaaaaaaaaaaa\n"),            # local
            _completed(stdout="bbbbbbbbbbbb\n"),            # remote
            _completed(returncode=1),                       # merge-base: NOT ancestor
        ])
        execv_mock = MagicMock()
        enforce_or_continue(_execv=execv_mock, _argv=["server.py"])
        execv_mock.assert_not_called()
        assert not (data_dir / LOG_FILENAME).exists()


class TestFetchFailure:
    """Expected behavior #6."""

    def test_fetch_failure_skips_update(self, fake_repo, data_dir, monkeypatch):
        _install_run_chain(monkeypatch, [
            _completed(stdout=""),                          # status
            _completed(returncode=1, stderr="connection refused"),  # fetch
        ])
        execv_mock = MagicMock()
        enforce_or_continue(_execv=execv_mock, _argv=["server.py"])
        execv_mock.assert_not_called()

    def test_fetch_timeout_does_not_crash_bringup(
        self, fake_repo, data_dir, monkeypatch,
    ):
        """2026-05-27 04:48 incident regression pin: a 60s git-fetch
        timeout uncaught used to propagate through enforce_or_continue
        and kill bring-up. Now _run_git catches TimeoutExpired and
        returns a synthetic returncode=124 — _fetch sees failure,
        logs FETCH_FAILED, returns from enforce_or_continue without
        raising. Bot continues with existing code."""
        call_count = {"n": 0}

        def _fake_run(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # status — succeeds.
                return _completed(stdout="")
            if call_count["n"] == 2:
                # fetch — raises TimeoutExpired (the bug scenario).
                raise subprocess.TimeoutExpired(
                    cmd=["git", "fetch", "origin", "main", "--quiet"],
                    timeout=60,
                )
            raise AssertionError(
                f"unexpected subprocess.run call #{call_count['n']}: "
                f"args={args} kwargs={kwargs}"
            )

        monkeypatch.setattr(subprocess, "run", _fake_run)
        execv_mock = MagicMock()
        # MUST NOT raise — bug was that TimeoutExpired propagated.
        enforce_or_continue(_execv=execv_mock, _argv=["server.py"])
        execv_mock.assert_not_called()
        # Confirm we got past status (call 1) and tried fetch (call 2),
        # then enforced returned cleanly without calling rev-parse (3).
        assert call_count["n"] == 2


class TestPullFailure:
    def test_pull_failure_skips_execv(self, fake_repo, data_dir, monkeypatch):
        _install_run_chain(monkeypatch, [
            _completed(stdout=""),                          # status
            _completed(),                                   # fetch
            _completed(stdout="aaaaaaaaaaaa\n"),            # local
            _completed(stdout="bbbbbbbbbbbb\n"),            # remote
            _completed(returncode=0),                       # merge-base
            _completed(
                returncode=1, stderr="Not possible to fast-forward\n",
            ),                                              # pull
        ])
        execv_mock = MagicMock()
        enforce_or_continue(_execv=execv_mock, _argv=["server.py"])
        execv_mock.assert_not_called()
        assert not (data_dir / LOG_FILENAME).exists()


class TestReinstallFailure:
    """Expected behavior #8 — reinstall failure is loud but doesn't abort restart."""

    def test_reinstall_failure_still_restarts(self, fake_repo, data_dir, monkeypatch):
        _install_run_chain(monkeypatch, [
            _completed(stdout=""),                          # status
            _completed(),                                   # fetch
            _completed(stdout="aaaaaaaaaaaa\n"),            # local
            _completed(stdout="bbbbbbbbbbbb\n"),            # remote
            _completed(returncode=0),                       # merge-base
            _completed(stdout="Fast-forward\n"),            # pull
            _completed(
                returncode=1, stderr="dependency conflict\n",
            ),                                              # pip install (fails)
            _completed(stdout="bbbbbbbbbbbb feat: x\n"),    # log
        ])
        execv_mock = MagicMock()
        enforce_or_continue(_execv=execv_mock, _argv=["server.py"])
        execv_mock.assert_called_once()


class TestPullOnlyDirtyOverride:
    """Cron-path mirror of the IGNORE_DIRTY override."""

    def test_cron_dirty_skips_by_default(self, fake_repo, data_dir, monkeypatch):
        _install_run_chain(monkeypatch, [
            _completed(stdout=" M some_file.py\n"),  # status
        ])
        assert self_update._pull_only(data_dir=str(data_dir)) is False

    def test_cron_ignore_dirty_proceeds(self, fake_repo, data_dir, monkeypatch):
        monkeypatch.setenv("KERNOS_AUTO_UPDATE_IGNORE_DIRTY", "on")
        _install_run_chain(monkeypatch, [
            _completed(stdout=" D data/diagnostics/foo.md\n"),
            _completed(),                                          # fetch
            _completed(stdout="aaaaaaaaaaaa\n"),                   # local
            _completed(stdout="bbbbbbbbbbbb\n"),                   # remote
            _completed(returncode=0),                              # ancestor
            _completed(stdout="Fast-forward\n"),                   # pull
            _completed(returncode=0),                              # pip install
            _completed(stdout="bbbbbbbbbbbb cleanup\n"),           # log
        ])
        assert self_update._pull_only(data_dir=str(data_dir)) is True
        assert (data_dir / LOG_FILENAME).is_file()


class TestBranchOverride:
    """Expected behavior #11."""

    def test_branch_override_used_in_fetch_and_pull(
        self, fake_repo, data_dir, monkeypatch,
    ):
        monkeypatch.setenv("KERNOS_UPDATE_BRANCH", "dogfood")
        captured: list = []

        def _run(args, **kwargs):
            captured.append(list(args))
            # Walk through the expected sequence
            if args[0] == "git" and args[1] == "status":
                return _completed(stdout="")
            if args[0] == "git" and args[1] == "fetch":
                return _completed()
            if args[0] == "git" and args[1] == "rev-parse":
                if args[2] == "HEAD":
                    return _completed(stdout="aaaaaaaaaaaa\n")
                return _completed(stdout="bbbbbbbbbbbb\n")
            if args[0] == "git" and args[1] == "merge-base":
                return _completed(returncode=0)
            if args[0] == "git" and args[1] == "pull":
                return _completed(stdout="Fast-forward\n")
            if args[0] == "git" and args[1] == "log":
                return _completed(stdout="bbbbbbbbbbbb feat: x\n")
            return _completed(returncode=0)

        monkeypatch.setattr(self_update.subprocess, "run", _run)
        execv_mock = MagicMock()
        enforce_or_continue(_execv=execv_mock, _argv=["server.py"])

        # Fetch call used the dogfood branch
        fetch_calls = [a for a in captured if len(a) >= 2 and a[1] == "fetch"]
        assert fetch_calls
        assert "dogfood" in fetch_calls[0]

        # Pull call used the dogfood branch
        pull_calls = [a for a in captured if len(a) >= 2 and a[1] == "pull"]
        assert pull_calls
        assert "dogfood" in pull_calls[0]

        # rev-parse origin/dogfood was looked up
        revparse_remote_calls = [
            a for a in captured
            if len(a) >= 3 and a[1] == "rev-parse" and a[2].startswith("origin/")
        ]
        assert revparse_remote_calls
        assert revparse_remote_calls[0][2] == "origin/dogfood"

        # Log referenced the correct branch
        log_text = (data_dir / LOG_FILENAME).read_text()
        assert "Branch: `dogfood`" in log_text


# ---------------------------------------------------------------------------
# Whisper summary helper + queueing
# ---------------------------------------------------------------------------


class TestWhisperSummary:
    """AUTO-UPDATE-INFORMING-V1: the summary helper now produces
    a substrate-event description (raw data + marker), not a
    pre-phrased message. The agent reads this alongside its
    covenants and produces user-facing phrasing in its own voice."""

    def test_summary_includes_top_commits(self):
        log = (
            "# Auto-update applied at 2026-04-23T15:00:00Z\n"
            "Branch: `main`\n\n"
            "## Commits pulled\n\n"
            "```\n"
            "abc1234 feat: first thing\n"
            "def5678 fix: second thing\n"
            "ghi9012 docs: third thing\n"
            "jkl3456 chore: fourth thing\n"
            "mno7890 chore: fifth thing\n"
            "pqr1234 chore: sixth (beyond cap)\n"
            "```\n"
        )
        text = _format_whisper_summary(log)
        # Cap at 5 — first five appear, sixth does not.
        assert "first thing" in text
        assert "fifth thing" in text
        assert "sixth" not in text
        assert LOG_FILENAME in text

    def test_summary_carries_substrate_event_marker(self):
        log = (
            "# Auto-update\n\n## Commits pulled\n\n```\n"
            "abc1234 feat: thing\n```\n"
        )
        text = _format_whisper_summary(log)
        assert "[SUBSTRATE_EVENT: kernos_self_updated]" in text

    def test_summary_handles_empty_commit_range(self):
        log = (
            "# Auto-update applied at 2026-04-23T15:00:00Z\n"
            "## Commits pulled\n\n"
            "```\n"
            "(commit range empty)\n"
            "```\n"
        )
        text = _format_whisper_summary(log)
        # "(commit range empty)" is itself a single "commit line" in
        # the parser; the substrate-event marker is what matters.
        assert "[SUBSTRATE_EVENT: kernos_self_updated]" in text


class TestQueuePendingWhisper:
    """Expected behavior #10 — whisper queued after restart when log is pending."""

    async def test_no_marker_no_whisper(self, tmp_path):
        state = MagicMock()
        state.save_whisper = MagicMock()
        queued = await queue_pending_whisper(
            state=state, instance_id="inst_x", data_dir=str(tmp_path),
        )
        assert queued is False
        state.save_whisper.assert_not_called()

    async def test_marker_plus_log_queues_whisper(self, tmp_path):
        (tmp_path / LOG_FILENAME).write_text(
            "## Commits pulled\n\n```\nabc feat: thing\n```\n"
        )
        (tmp_path / MARKER_FILENAME).write_text("2026-04-23T15:00:00Z")

        state = MagicMock()

        async def _save(instance_id, whisper):
            _save.called_with = (instance_id, whisper)
        _save.called_with = None
        state.save_whisper = _save

        queued = await queue_pending_whisper(
            state=state, instance_id="inst_x", data_dir=str(tmp_path),
        )
        assert queued is True
        assert _save.called_with is not None
        instance_id, whisper = _save.called_with
        assert instance_id == "inst_x"
        # The whisper carries a substrate-event description (not a
        # pre-phrased "I just auto-updated" message). The agent
        # reads it alongside covenants and phrases the surfacing.
        assert "[SUBSTRATE_EVENT: kernos_self_updated]" in whisper.insight_text
        assert "I just auto-updated" not in whisper.insight_text
        assert whisper.foresight_signal == "auto_update:applied"
        assert whisper.owner_member_id == ""
        # Marker cleared
        assert not (tmp_path / MARKER_FILENAME).exists()
        # Log remains (durable artifact)
        assert (tmp_path / LOG_FILENAME).exists()
