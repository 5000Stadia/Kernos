"""DISCORD-RATE-LIMIT-DEFERRED-DELIVERY (2026-05-23) acceptance tests.

The bug: when Kernos's Discord adapter hit a 429 cool-off and the
user sent another message during the pause, the reply was
generated (visible in conv-log on disk) but Discord delivery
silently dropped. The user saw the pause notice and then never
got the actual reply.

The fix: dropped chunks queue in ``_dropped_deliveries``; a
background flusher polls; when ``_is_discord_paused()`` clears,
the flusher drains the queue with a brief "↩️ Cool-off ended"
header.

These tests pin the queue mechanics + the flush behavior using
stub channels (no actual Discord). The async send path is
mocked via a MagicMock channel.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------
# Helpers: import the module + reset module state between tests.
# ---------------------------------------------------------------------


@pytest.fixture
def server_mod():
    """Import server module + reset module state so tests are isolated."""
    from kernos import server
    # Reset state
    server._dropped_deliveries.clear()
    server._discord_pause_until = 0.0
    server._discord_429_streak = 0
    yield server
    # Cleanup
    server._dropped_deliveries.clear()
    server._discord_pause_until = 0.0
    server._discord_429_streak = 0


def _make_channel(channel_id: int = 12345) -> MagicMock:
    ch = MagicMock()
    ch.id = channel_id
    ch.send = AsyncMock()
    return ch


# ---------------------------------------------------------------------
# Queue mechanics
# ---------------------------------------------------------------------


class TestRegisterDroppedDelivery:
    def test_queues_by_channel_id(self, server_mod):
        ch = _make_channel(channel_id=7)
        server_mod._register_dropped_delivery(ch, "hello world")
        assert 7 in server_mod._dropped_deliveries
        assert len(server_mod._dropped_deliveries[7]) == 1
        stored_channel, chunk, ts = server_mod._dropped_deliveries[7][0]
        assert stored_channel is ch
        assert chunk == "hello world"
        assert isinstance(ts, float)

    def test_appends_multiple_chunks_in_order(self, server_mod):
        ch = _make_channel(channel_id=42)
        for chunk in ("first", "second", "third"):
            server_mod._register_dropped_delivery(ch, chunk)
        chunks = [c for _, c, _ in server_mod._dropped_deliveries[42]]
        assert chunks == ["first", "second", "third"]

    def test_separate_channels_separate_queues(self, server_mod):
        a = _make_channel(channel_id=1)
        b = _make_channel(channel_id=2)
        server_mod._register_dropped_delivery(a, "for a")
        server_mod._register_dropped_delivery(b, "for b")
        assert 1 in server_mod._dropped_deliveries
        assert 2 in server_mod._dropped_deliveries
        assert len(server_mod._dropped_deliveries[1]) == 1
        assert len(server_mod._dropped_deliveries[2]) == 1

    def test_empty_chunk_ignored(self, server_mod):
        ch = _make_channel()
        server_mod._register_dropped_delivery(ch, "")
        assert ch.id not in server_mod._dropped_deliveries

    def test_none_channel_ignored(self, server_mod):
        server_mod._register_dropped_delivery(None, "x")
        assert not server_mod._dropped_deliveries

    def test_channel_without_id_ignored(self, server_mod):
        ch = MagicMock()
        del ch.id
        # Defensive: channel without .id attr → skipped, no crash
        server_mod._register_dropped_delivery(ch, "x")
        assert not server_mod._dropped_deliveries


# ---------------------------------------------------------------------
# _send_safely registers dropped chunks during cool-off
# ---------------------------------------------------------------------


class TestSendSafelyQueuesOnPause:
    @pytest.mark.asyncio
    async def test_active_pause_queues_chunk(self, server_mod):
        # Activate pause
        server_mod._discord_pause_until = time.time() + 60
        ch = _make_channel(channel_id=99)
        ok = await server_mod._send_safely(ch, "dropped message")
        assert ok is False
        # ch.send was NOT called (paused before attempt)
        ch.send.assert_not_called()
        # The chunk is in the queue
        assert 99 in server_mod._dropped_deliveries
        assert (
            server_mod._dropped_deliveries[99][0][1] == "dropped message"
        )


# ---------------------------------------------------------------------
# Flusher behavior
# ---------------------------------------------------------------------


class TestFlushDroppedDeliveriesOnce:
    @pytest.mark.asyncio
    async def test_flush_no_op_when_paused(self, server_mod):
        server_mod._discord_pause_until = time.time() + 60
        ch = _make_channel(channel_id=5)
        server_mod._register_dropped_delivery(ch, "x")
        delivered = await server_mod._flush_dropped_deliveries_once()
        assert delivered == 0
        # Still queued
        assert 5 in server_mod._dropped_deliveries

    @pytest.mark.asyncio
    async def test_flush_no_op_when_empty(self, server_mod):
        # Cool-off cleared, nothing queued
        server_mod._discord_pause_until = 0.0
        delivered = await server_mod._flush_dropped_deliveries_once()
        assert delivered == 0

    @pytest.mark.asyncio
    async def test_flush_delivers_queued_chunks(self, server_mod):
        ch = _make_channel(channel_id=11)
        server_mod._register_dropped_delivery(ch, "first")
        server_mod._register_dropped_delivery(ch, "second")
        # Cool-off cleared
        server_mod._discord_pause_until = 0.0
        # Pin DISCORD_INTERCHUNK_DELAY_SEC to 0 so the test runs fast
        # (the actual constant might add a sleep otherwise).
        delivered = await server_mod._flush_dropped_deliveries_once()
        assert delivered == 2
        # The resume note rides on the FIRST chunk (one send), so there are
        # exactly N sends, NOT N+1. No standalone header that could succeed
        # while the payload 429s (the live-observed infinite-loop bug).
        assert ch.send.await_count == 2
        first_send = ch.send.await_args_list[0].args[0]
        assert "Cool-off ended" in first_send
        assert "2 message(s)" in first_send
        assert "first" in first_send                  # content carried in the same send
        assert ch.send.await_args_list[1].args[0] == "second"
        # Queue is empty now
        assert not server_mod._dropped_deliveries

    @pytest.mark.asyncio
    async def test_flush_handles_multiple_channels(self, server_mod):
        a = _make_channel(channel_id=1)
        b = _make_channel(channel_id=2)
        server_mod._register_dropped_delivery(a, "for a")
        server_mod._register_dropped_delivery(b, "for b1")
        server_mod._register_dropped_delivery(b, "for b2")
        server_mod._discord_pause_until = 0.0
        delivered = await server_mod._flush_dropped_deliveries_once()
        assert delivered == 3
        # Resume note rides on each channel's first chunk → one send per chunk.
        assert a.send.await_count == 1  # note+chunk in one send
        assert b.send.await_count == 2  # note+chunk1, then chunk2

    @pytest.mark.asyncio
    async def test_flush_requeues_on_persistent_429(
        self, server_mod, monkeypatch,
    ):
        """If a chunk's send still 429s during flush (transient),
        the chunk is re-queued for the next pass — not lost."""
        import discord
        ch = _make_channel(channel_id=88)
        # Make ch.send raise RateLimited
        ch.send = AsyncMock(side_effect=discord.RateLimited(
            retry_after=5,
        ))
        server_mod._register_dropped_delivery(ch, "chunk-1")
        server_mod._discord_pause_until = 0.0
        delivered = await server_mod._flush_dropped_deliveries_once()
        assert delivered == 0
        # Re-queued
        assert 88 in server_mod._dropped_deliveries
        assert (
            server_mod._dropped_deliveries[88][0][1] == "chunk-1"
        )

    @pytest.mark.asyncio
    async def test_flush_header_includes_wait_duration(self, server_mod):
        ch = _make_channel(channel_id=33)
        # Manually backdate the queued time so wait calc is non-zero
        server_mod._dropped_deliveries[33] = [
            (ch, "x", time.time() - 42),
        ]
        server_mod._discord_pause_until = 0.0
        await server_mod._flush_dropped_deliveries_once()
        first_send = ch.send.await_args_list[0].args[0]
        # Wait duration appears in the note (now riding on the first chunk).
        assert "~42s" in first_send or "~41s" in first_send or "~43s" in first_send
        assert "x" in first_send  # content delivered in the same send

    @pytest.mark.asyncio
    async def test_flush_does_not_spam_header_when_payload_429s(
        self, server_mod, monkeypatch,
    ):
        """The infinite-loop regression (live-observed 2026-06-08).

        Old shape: a standalone 'Cool-off ended' header was sent, succeeded,
        reset the 429 streak — then the back-to-back payload chunk 429'd and
        re-armed the cool-off. The user got the header every cycle and never
        the content. With the note merged into the payload, a 429 means NOTHING
        was sent (no header spam) and the streak escalates instead of resetting.
        """
        import discord
        ch = _make_channel(channel_id=99)
        ch.send = AsyncMock(side_effect=discord.RateLimited(retry_after=5))
        server_mod._register_dropped_delivery(ch, "the actual reply")
        server_mod._discord_pause_until = 0.0
        server_mod._discord_429_streak = 0

        delivered = await server_mod._flush_dropped_deliveries_once()

        assert delivered == 0
        # Exactly one send attempt (the merged note+chunk) — no separate header
        # that could land on its own.
        assert ch.send.await_count == 1
        # The failed send escalated the backoff rather than resetting it, so a
        # persistently throttled channel backs off instead of looping at 5 min.
        assert server_mod._discord_429_streak == 1
        # Content re-queued, not lost.
        assert server_mod._dropped_deliveries[99][0][1] == "the actual reply"

    @pytest.mark.asyncio
    async def test_flush_ages_out_stuck_queue(self, server_mod):
        """A queue stuck past the max age gets one terminal note, then dropped —
        not retried forever."""
        ch = _make_channel(channel_id=77)
        old_ts = time.time() - (server_mod._DEFERRED_DELIVERY_MAX_AGE_SEC + 60)
        server_mod._dropped_deliveries[77] = [
            (ch, "stale reply", old_ts),
        ]
        server_mod._discord_pause_until = 0.0
        delivered = await server_mod._flush_dropped_deliveries_once()
        assert delivered == 0
        assert ch.send.await_count == 1
        note = ch.send.await_args_list[0].args[0]
        assert "conv-log" in note
        # Dropped — no longer retried.
        assert not server_mod._dropped_deliveries
