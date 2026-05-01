"""AUTO-UPDATE-BEHAVIOR-V1 + AUTO-UPDATE-INFORMING-V1 tests.

Covers:
  1. KERNOS_AUTO_UPDATE_TIME parsing + fallback (V1).
  2. Scheduled-update loop disable + sleep contract (V1).
  3. format_update_event_text shape — substrate event description
     for the agent's situation context (INFORMING-V1).
  4. The default "tell me about updates" covenant ships in the
     starter rule set (INFORMING-V1).

KERNOS_AUTO_UPDATE_VERBOSE is gone — its tests were removed when
the verbose ephemeral path was retired.

Does NOT exercise the real subprocess pull — those are covered by
the existing self_update test suite.
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
# KERNOS_AUTO_UPDATE_VERBOSE master toggle (AUTO-UPDATE-INFORMING-V1
# revision: env var stays as the operator-level opt-out; new
# semantics gate the post-update whisper instead of a parallel
# ephemeral path)
# ---------------------------------------------------------------------------


class TestVerboseFlag:
    def test_on_when_unset(self, monkeypatch):
        # New semantics: default is ON. Operators get update
        # notifications by default; opt out by setting off.
        monkeypatch.delenv("KERNOS_AUTO_UPDATE_VERBOSE", raising=False)
        assert self_update._verbose_enabled() is True

    def test_on_when_empty(self, monkeypatch):
        monkeypatch.setenv("KERNOS_AUTO_UPDATE_VERBOSE", "")
        assert self_update._verbose_enabled() is True

    def test_on_when_on(self, monkeypatch):
        monkeypatch.setenv("KERNOS_AUTO_UPDATE_VERBOSE", "on")
        assert self_update._verbose_enabled() is True

    def test_off_when_off(self, monkeypatch):
        monkeypatch.setenv("KERNOS_AUTO_UPDATE_VERBOSE", "off")
        assert self_update._verbose_enabled() is False

    def test_off_on_anything_else(self, monkeypatch):
        # Anything other than "on" turns it off — conservative.
        for value in ("true", "1", "yes", "garbage"):
            monkeypatch.setenv("KERNOS_AUTO_UPDATE_VERBOSE", value)
            assert self_update._verbose_enabled() is False, value


# ---------------------------------------------------------------------------
# Substrate-event format (AUTO-UPDATE-INFORMING-V1)
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


class TestFormatUpdateEventText:
    def test_includes_substrate_event_marker(self):
        text = self_update.format_update_event_text(_LOG_TEMPLATE)
        assert "[SUBSTRATE_EVENT: kernos_self_updated]" in text

    def test_includes_head_commit_hash(self):
        text = self_update.format_update_event_text(_LOG_TEMPLATE)
        assert "3b053f3" in text

    def test_caps_at_five_commit_subjects(self):
        text = self_update.format_update_event_text(_LOG_TEMPLATE)
        # Cap at 5 most recent commit subjects. Each commit appears
        # on its own bulleted line; count those.
        bullet_lines = [l for l in text.splitlines() if l.startswith("  - ")]
        assert len(bullet_lines) <= 5, (
            f"more than 5 commit lines in {text!r}"
        )

    def test_reports_total_commit_count(self):
        text = self_update.format_update_event_text(_LOG_TEMPLATE)
        # Log has 7 commits.
        assert "7 commit" in text

    def test_marks_truncation_when_capped(self):
        text = self_update.format_update_event_text(_LOG_TEMPLATE)
        # 7 total > 5 cap → text should mention "showing N most recent"
        assert "showing" in text

    def test_no_truncation_marker_when_under_cap(self):
        small_log = (
            "# Auto-update\n\n## Commits pulled\n\n```\n"
            "abc1234 first\nbcd5678 second\n```\n"
        )
        text = self_update.format_update_event_text(small_log)
        assert "showing" not in text

    def test_empty_log_still_produces_event_marker(self):
        text = self_update.format_update_event_text("")
        # Even with no commits, the agent should see the event marker
        # so its covenant logic can still recognize the event.
        assert "[SUBSTRATE_EVENT: kernos_self_updated]" in text

    def test_does_not_pre_phrase_in_first_person(self):
        # Spec invariant: substrate does NOT put words in the agent's
        # mouth. The old _format_whisper_summary started with "I just
        # auto-updated."; the refit must NOT do that — the agent
        # phrases in its own voice.
        text = self_update.format_update_event_text(_LOG_TEMPLATE)
        assert "I just auto-updated" not in text

    def test_singular_commit_grammar(self):
        single_log = (
            "# Auto-update\n\n## Commits pulled\n\n```\n"
            "abc1234 single commit\n```\n"
        )
        text = self_update.format_update_event_text(single_log)
        assert "1 commit " in text
        assert "1 commits " not in text


# ---------------------------------------------------------------------------
# queue_pending_whisper gating by KERNOS_AUTO_UPDATE_VERBOSE
# ---------------------------------------------------------------------------


class TestQueuePendingWhisperVerboseGate:
    """The whisper queueing path is the master entry for
    update-notification awareness. When verbose=off, no whisper —
    agent never knows the update happened."""

    async def test_verbose_off_skips_queue(self, tmp_path, monkeypatch):
        from kernos.setup.self_update import (
            LOG_FILENAME, MARKER_FILENAME, queue_pending_whisper,
        )
        monkeypatch.setenv("KERNOS_AUTO_UPDATE_VERBOSE", "off")
        (tmp_path / LOG_FILENAME).write_text(
            "## Commits pulled\n\n```\nabc feat: thing\n```\n"
        )
        (tmp_path / MARKER_FILENAME).write_text("2026-05-01T00:00:00Z")

        save_calls = []

        class _State:
            async def save_whisper(self, instance_id, whisper):
                save_calls.append((instance_id, whisper))

        queued = await queue_pending_whisper(
            state=_State(), instance_id="inst1", data_dir=str(tmp_path),
        )
        assert queued is False
        assert save_calls == []
        # Marker is consumed even when verbose=off so subsequent
        # restarts don't try to queue either.
        assert not (tmp_path / MARKER_FILENAME).exists()

    async def test_verbose_on_queues_normally(self, tmp_path, monkeypatch):
        from kernos.setup.self_update import (
            LOG_FILENAME, MARKER_FILENAME, queue_pending_whisper,
        )
        monkeypatch.setenv("KERNOS_AUTO_UPDATE_VERBOSE", "on")
        (tmp_path / LOG_FILENAME).write_text(
            "## Commits pulled\n\n```\nabc feat: thing\n```\n"
        )
        (tmp_path / MARKER_FILENAME).write_text("2026-05-01T00:00:00Z")

        save_calls = []

        class _State:
            async def save_whisper(self, instance_id, whisper):
                save_calls.append((instance_id, whisper))

        queued = await queue_pending_whisper(
            state=_State(), instance_id="inst1", data_dir=str(tmp_path),
        )
        assert queued is True
        assert len(save_calls) == 1
        # Whisper carries the substrate-event marker.
        _, whisper = save_calls[0]
        assert "[SUBSTRATE_EVENT: kernos_self_updated]" in whisper.insight_text


# ---------------------------------------------------------------------------
# Default covenant for update notifications (AUTO-UPDATE-INFORMING-V1)
# ---------------------------------------------------------------------------


class TestDefaultUpdateCovenant:
    def test_covenant_ships_in_default_rules(self):
        from kernos.kernel.state import default_covenant_rules

        rules = default_covenant_rules("inst1", "2026-05-01T00:00:00Z")
        # The update covenant should be present, identifiable by
        # description content (not by ID — IDs are random per call).
        update_rules = [
            r for r in rules
            if "update" in r.description.lower()
            and "kernos" in r.description.lower()
        ]
        assert len(update_rules) == 1, (
            "expected exactly one default covenant about Kernos "
            f"updates; got {len(update_rules)}: "
            f"{[r.description[:80] for r in update_rules]}"
        )
        rule = update_rules[0]
        assert rule.source == "default"
        assert rule.active is True
        assert rule.rule_type == "preference"

    def test_covenant_text_invites_user_revision(self):
        # The covenant should explicitly tell the agent (and through
        # the agent's reading, the user) that the rule is editable.
        from kernos.kernel.state import default_covenant_rules

        rules = default_covenant_rules("inst1", "2026-05-01T00:00:00Z")
        update_rule = next(
            r for r in rules
            if "update" in r.description.lower()
            and "kernos" in r.description.lower()
        )
        # Look for hints that the agent should treat user requests
        # to change the rule as actionable.
        text = update_rule.description.lower()
        assert "archive" in text or "revise" in text or "edit" in text or "stop" in text


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
