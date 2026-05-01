"""AUTO-UPDATE-BEHAVIOR-V1 tests.

Covers the three new pieces:
  1. KERNOS_AUTO_UPDATE_TIME parsing + fallback.
  2. KERNOS_AUTO_UPDATE_VERBOSE gating + ephemeral message format.
  3. Scheduled-update loop disable + sleep contract.

Does NOT exercise the real subprocess pull — those are covered by
the existing self_update test suite. This file pins the new env
vars + the announcement-formatting + the scheduled-loop behavior.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.setup import self_update


# ---------------------------------------------------------------------------
# KERNOS_AUTO_UPDATE_TIME parser
# ---------------------------------------------------------------------------


class TestParseUpdateTime:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("KERNOS_AUTO_UPDATE_TIME", raising=False)
        assert self_update._parse_update_time() == (3, 0)

    def test_default_when_empty(self, monkeypatch):
        monkeypatch.setenv("KERNOS_AUTO_UPDATE_TIME", "")
        assert self_update._parse_update_time() == (3, 0)

    def test_valid_morning(self, monkeypatch):
        monkeypatch.setenv("KERNOS_AUTO_UPDATE_TIME", "04:00")
        assert self_update._parse_update_time() == (4, 0)

    def test_valid_evening(self, monkeypatch):
        monkeypatch.setenv("KERNOS_AUTO_UPDATE_TIME", "23:45")
        assert self_update._parse_update_time() == (23, 45)

    def test_fallback_on_garbage(self, monkeypatch):
        monkeypatch.setenv("KERNOS_AUTO_UPDATE_TIME", "garbage")
        assert self_update._parse_update_time() == (3, 0)

    def test_fallback_on_out_of_range(self, monkeypatch):
        monkeypatch.setenv("KERNOS_AUTO_UPDATE_TIME", "25:00")
        assert self_update._parse_update_time() == (3, 0)

    def test_fallback_on_negative(self, monkeypatch):
        monkeypatch.setenv("KERNOS_AUTO_UPDATE_TIME", "-1:30")
        assert self_update._parse_update_time() == (3, 0)


# ---------------------------------------------------------------------------
# KERNOS_AUTO_UPDATE_VERBOSE flag
# ---------------------------------------------------------------------------


class TestVerboseFlag:
    def test_off_when_unset(self, monkeypatch):
        monkeypatch.delenv("KERNOS_AUTO_UPDATE_VERBOSE", raising=False)
        assert self_update._verbose_enabled() is False

    def test_on_when_on(self, monkeypatch):
        monkeypatch.setenv("KERNOS_AUTO_UPDATE_VERBOSE", "on")
        assert self_update._verbose_enabled() is True

    def test_off_on_anything_else(self, monkeypatch):
        for value in ("off", "true", "1", "yes", "garbage"):
            monkeypatch.setenv("KERNOS_AUTO_UPDATE_VERBOSE", value)
            assert self_update._verbose_enabled() is False, value


# ---------------------------------------------------------------------------
# Verbose announcement format
# ---------------------------------------------------------------------------


_LOG_TEMPLATE = """# Auto-update applied at 2026-04-30T22:00:00+00:00
Branch: `main`
Previous HEAD: `abc123def456`

## Commits pulled

```
3b053f3 CLEANUP-BATCH-V1 Pass 5: kernel-tool dispatch path registry
e778085 CLEANUP-BATCH-V1 Pass 6: NOW-block guard
f331d66 CLEANUP-BATCH-V1 Pass 4: operational hardening
294e5f6 CLEANUP-BATCH-V1 Pass 3: DECISIONS.md refresh
3f302d5 CLEANUP-BATCH-V1 Pass 2: capability matrix + /capabilities
e77c384 CLEANUP-BATCH-V1 Pass 1: text/docs
d8f0b28 CLEANUP-BATCH-V1 close
```
"""


class TestFormatVerboseAnnouncement:
    def test_includes_head_commit_hash(self):
        msg = self_update.format_verbose_announcement(_LOG_TEMPLATE)
        assert "3b053f3" in msg

    def test_caps_at_five_commit_subjects(self):
        msg = self_update.format_verbose_announcement(_LOG_TEMPLATE)
        # Spec: cap at 5 most recent commit subjects.
        # Commit subjects with quotes — count quoted strings.
        quoted_count = msg.count('"')
        # Each subject contributes 2 quotes (open + close).
        assert quoted_count <= 10, (
            f"more than 5 commit subjects in {msg!r}"
        )

    def test_total_count_reflects_all_commits(self):
        msg = self_update.format_verbose_announcement(_LOG_TEMPLATE)
        # Log has 7 commits; "7 changes pulled" should appear.
        assert "7 change" in msg

    def test_empty_log_returns_graceful_fallback(self):
        msg = self_update.format_verbose_announcement("")
        assert "Updated" in msg
        assert "unavailable" in msg

    def test_log_with_no_commit_block_falls_back(self):
        msg = self_update.format_verbose_announcement(
            "# Auto-update header only\nBranch: main\n"
        )
        assert "Updated" in msg

    def test_singular_change_grammar(self):
        single_log = (
            "# Auto-update\n\n## Commits pulled\n\n```\n"
            "abc1234 single commit\n```\n"
        )
        msg = self_update.format_verbose_announcement(single_log)
        assert "1 change " in msg
        assert "1 changes " not in msg


# ---------------------------------------------------------------------------
# Scheduled update loop
# ---------------------------------------------------------------------------


class TestScheduledUpdateLoop:
    async def test_loop_disabled_when_auto_update_off(self, monkeypatch):
        monkeypatch.setenv("KERNOS_AUTO_UPDATE", "off")
        pull_calls: list[dict] = []

        def _fake_pull(**kwargs):
            pull_calls.append(kwargs)
            return False

        # Loop should return immediately; no pull attempted, no sleep.
        sleep_calls: list[float] = []

        async def _fake_sleep(seconds):
            sleep_calls.append(seconds)

        await self_update.scheduled_update_loop(
            data_dir=None, _pull=_fake_pull, _sleep=_fake_sleep,
        )
        assert pull_calls == []
        assert sleep_calls == []

    async def test_loop_sleeps_then_pulls(self, monkeypatch):
        """One full iteration: sleep, pull, sleep again — interrupt
        on the second sleep to exit cleanly."""
        monkeypatch.setenv("KERNOS_AUTO_UPDATE", "on")
        monkeypatch.setenv("KERNOS_AUTO_UPDATE_TIME", "04:00")

        pull_calls: list[dict] = []

        def _fake_pull(**kwargs):
            pull_calls.append(kwargs)
            return True

        sleep_count = [0]

        async def _fake_sleep(seconds):
            sleep_count[0] += 1
            assert seconds > 0  # always positive — next-target offset
            if sleep_count[0] >= 2:
                raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await self_update.scheduled_update_loop(
                data_dir="/tmp/test", _pull=_fake_pull, _sleep=_fake_sleep,
            )
        # One pull executed between the two sleeps.
        assert len(pull_calls) == 1
        assert pull_calls[0] == {"data_dir": "/tmp/test"}

    async def test_loop_continues_after_pull_raises(self, monkeypatch):
        """If _pull raises, the loop logs and continues — doesn't
        crash the task."""
        monkeypatch.setenv("KERNOS_AUTO_UPDATE", "on")

        pull_calls = [0]

        def _fake_pull(**kwargs):
            pull_calls[0] += 1
            raise RuntimeError("simulated pull failure")

        sleep_count = [0]

        async def _fake_sleep(seconds):
            sleep_count[0] += 1
            if sleep_count[0] >= 3:
                raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await self_update.scheduled_update_loop(
                data_dir=None, _pull=_fake_pull, _sleep=_fake_sleep,
            )
        # Loop survived two pull failures (3 sleeps means 2 pulls).
        assert pull_calls[0] == 2


# ---------------------------------------------------------------------------
# _seconds_until_next math
# ---------------------------------------------------------------------------


class TestSecondsUntilNext:
    def test_returns_positive_value(self):
        # No matter the wall clock, the result should be > 0 and <= 86400.
        for hour in (0, 6, 12, 23):
            for minute in (0, 15, 59):
                seconds = self_update._seconds_until_next(hour, minute)
                assert 0 < seconds <= 86400, (
                    f"out of bounds at {hour:02d}:{minute:02d}: {seconds}"
                )
