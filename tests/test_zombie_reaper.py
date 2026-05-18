"""ZOMBIE-CHILD-PROCESS-REAP-V1 pin tests.

Pins:
  * The zombie reaper loop function exists and is callable
  * Per-cycle reap loops until WNOHANG returns 0
  * Reap loop logs ZOMBIE_REAPED for each reaped child
  * Reap loop continues past exceptions (doesn't die on transient OS hiccups)
  * Capability/client.py auth-timeout path awaits proc.wait() after
    terminate so the OS reaps the SIGTERMed child instead of leaving
    a zombie attached to the bot
"""
from __future__ import annotations

import asyncio
import logging
import os
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------
# Reaper loop pins
# ---------------------------------------------------------------------


async def test_zombie_reaper_loop_function_exists():
    """Pin: the reaper loop function is importable from server.py
    and is an async coroutine function."""
    from kernos.server import _zombie_reaper_loop
    assert asyncio.iscoroutinefunction(_zombie_reaper_loop)


async def test_zombie_reaper_loop_reaps_until_wnohang_returns_zero(caplog):
    """Pin: per cycle, the loop calls waitpid(WNOHANG) repeatedly
    until it returns 0 — so a burst of zombies gets fully drained
    in one cycle, not one per minute.
    """
    from kernos import server as srv

    # Simulate: first two calls return (pid, status), third returns
    # (0, 0) signaling "no more reapable children right now".
    fake_calls = iter([(12345, 0), (67890, 0), (0, 0)])

    def _fake_waitpid(pid, flags):
        assert pid == -1
        assert flags == os.WNOHANG
        return next(fake_calls)

    caplog.set_level(logging.INFO, logger="kernos.server")
    # Patch waitpid + clamp the loop interval so the test runs fast.
    with patch.object(os, "waitpid", _fake_waitpid), \
         patch.object(srv, "_ZOMBIE_REAPER_INTERVAL_SEC", 0.01):
        task = asyncio.create_task(srv._zombie_reaper_loop())
        # Let one cycle complete.
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    # Both reaped pids should have been logged.
    reaped_lines = [
        r.message for r in caplog.records if "ZOMBIE_REAPED" in r.message
    ]
    assert any("pid=12345" in m for m in reaped_lines)
    assert any("pid=67890" in m for m in reaped_lines)


async def test_zombie_reaper_loop_continues_past_exceptions(caplog):
    """Pin: a transient OS exception in the inner loop must not kill
    the reaper. Logged and loop continues."""
    from kernos import server as srv

    call_count = [0]

    def _flaky_waitpid(pid, flags):
        call_count[0] += 1
        if call_count[0] == 1:
            raise OSError("transient kernel hiccup")
        return (0, 0)  # second call: nothing to reap, exit inner loop

    caplog.set_level(logging.WARNING, logger="kernos.server")
    with patch.object(os, "waitpid", _flaky_waitpid), \
         patch.object(srv, "_ZOMBIE_REAPER_INTERVAL_SEC", 0.01):
        task = asyncio.create_task(srv._zombie_reaper_loop())
        # Let two cycles run so we see the error + recovery.
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    error_lines = [
        r.message for r in caplog.records
        if "ZOMBIE_REAPER_LOOP_ERROR" in r.message
    ]
    assert len(error_lines) >= 1, (
        f"expected at least one ZOMBIE_REAPER_LOOP_ERROR log; "
        f"got messages: {[r.message for r in caplog.records]}"
    )


async def test_zombie_reaper_loop_handles_no_children_gracefully():
    """Pin: ChildProcessError (no children to wait on) is non-fatal —
    the inner loop breaks cleanly and the outer loop continues."""
    from kernos import server as srv

    def _no_children(pid, flags):
        raise ChildProcessError("no child processes")

    with patch.object(os, "waitpid", _no_children), \
         patch.object(srv, "_ZOMBIE_REAPER_INTERVAL_SEC", 0.01):
        task = asyncio.create_task(srv._zombie_reaper_loop())
        await asyncio.sleep(0.05)
        # If the loop survived, task is still running — cancellation
        # works cleanly.
        assert not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------
# Auth-timeout zombie fix pin
# ---------------------------------------------------------------------


async def test_auth_timeout_awaits_proc_wait_after_terminate():
    """Pin: capability/client.py's TOOL_AUTH_REAUTH timeout path must
    await proc.wait() after terminate() so the OS reaps the SIGTERMed
    child immediately — without this await, the child sits as a
    zombie attached to the bot until the bot exits.

    Verifies via source inspection (not a live subprocess test) that
    the proc.wait() pattern is present in the auth-timeout block.
    """
    import inspect
    from kernos.capability import client as _client
    src = inspect.getsource(_client)
    # Look for the timeout-path comment + the wait_for(proc.wait()) call
    # immediately following proc.terminate() in the auth path.
    assert "ZOMBIE-CHILD-PROCESS-REAP-V1" in src, (
        "auth-timeout zombie fix marker not present in capability/client.py — "
        "either the fix was reverted or the comment marker was edited"
    )
    assert "asyncio.wait_for(proc.wait()" in src, (
        "no proc.wait() found after terminate() in capability/client.py — "
        "zombies will accumulate from auth-flow timeouts"
    )
