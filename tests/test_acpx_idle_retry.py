"""ACPX idle-stall watchdog and retry behavior."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable

import pytest

from kernos.kernel.external_agents import acpx_adapter
from kernos.kernel.external_agents.errors import (
    ConsultationFailed,
    ConsultationStalled,
)


def _event_line(event: dict) -> bytes:
    return (json.dumps(event) + "\n").encode("utf-8")


TOOL_CALL_EVENT = {
    "jsonrpc": "2.0",
    "method": "session/update",
    "params": {
        "update": {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-1",
        },
    },
}


SUCCESS_EVENTS = [
    {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "done"},
            },
        },
    },
    {"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "end_turn"}},
]


AUTH_ERROR_EVENT = {
    "jsonrpc": "2.0",
    "id": None,
    "error": {
        "code": -32603,
        "message": "Internal error: Credit balance is too low",
        "data": {"errorKind": "billing_error"},
    },
}


class _FakeProc:
    _next_pid = 50_000

    def __init__(self, mode: str) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.mode = mode
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.killed = False
        self._stdout_eof = False
        self._stderr_eof = False
        self._done = asyncio.Event()
        self._prime_streams()

    def _feed_stdout(self, events: Iterable[dict]) -> None:
        for event in events:
            self.stdout.feed_data(_event_line(event))

    def _prime_streams(self) -> None:
        if self.mode == "stall":
            self._feed_stdout([TOOL_CALL_EVENT])
            return
        if self.mode == "success":
            self._feed_stdout(SUCCESS_EVENTS)
            self._feed_eof()
            asyncio.create_task(self._finish_later(0))
            return
        if self.mode == "auth":
            self._feed_stdout([AUTH_ERROR_EVENT])
            self._feed_eof()
            asyncio.create_task(self._finish_later(1))
            return
        raise AssertionError(f"unknown fake ACPX mode: {self.mode}")

    def _feed_eof(self) -> None:
        if not self._stdout_eof:
            self.stdout.feed_eof()
            self._stdout_eof = True
        if not self._stderr_eof:
            self.stderr.feed_eof()
            self._stderr_eof = True

    async def _finish_later(self, rc: int) -> None:
        await asyncio.sleep(0.02)
        if self.returncode is None:
            self.returncode = rc
            self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9
            self._feed_eof()
            self._done.set()


def _install_fake_acpx(
    monkeypatch: pytest.MonkeyPatch,
    modes: list[str],
    tmp_path,
) -> list[_FakeProc]:
    procs: list[_FakeProc] = []

    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(acpx_adapter, "_acpx_binary", lambda: "acpx")
    monkeypatch.setattr(acpx_adapter.shutil, "which", lambda _binary: "/bin/acpx")
    monkeypatch.setattr(acpx_adapter, "_collect_descendants", lambda _pid: [])
    monkeypatch.setattr(acpx_adapter, "_kill_tree", lambda _pid: 0)

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        if not modes:
            raise AssertionError("unexpected extra ACPX subprocess")
        proc = _FakeProc(modes.pop(0))
        procs.append(proc)
        return proc

    monkeypatch.setattr(
        acpx_adapter.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    return procs


def test_last_event_kind_prefers_session_update_tool_call() -> None:
    assert acpx_adapter._extract_event_kind(TOOL_CALL_EVENT) == "tool_call"


@pytest.mark.asyncio
async def test_idle_watchdog_raises_stalled_before_total_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("KERNOS_ACPX_IDLE_ABORT_SEC", "0.05")
    monkeypatch.setenv("KERNOS_ACPX_CONSULT_RETRIES", "0")
    procs = _install_fake_acpx(monkeypatch, ["stall"], tmp_path)

    started = time.monotonic()
    with pytest.raises(ConsultationStalled) as caught:
        await acpx_adapter.dispatch(
            target="claude_code",
            prompt="work",
            workspace_dir=str(tmp_path),
            timeout_seconds=30,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert caught.value.last_event_kind == "tool_call"
    assert caught.value.event_count == 1
    assert procs[0].killed is True


@pytest.mark.asyncio
async def test_retry_after_stall_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("KERNOS_ACPX_IDLE_ABORT_SEC", "0.05")
    monkeypatch.setenv("KERNOS_ACPX_CONSULT_RETRIES", "1")
    caplog.set_level(logging.WARNING)
    procs = _install_fake_acpx(monkeypatch, ["stall", "success"], tmp_path)

    result = await acpx_adapter.dispatch(
        target="claude_code",
        prompt="work",
        workspace_dir=str(tmp_path),
        timeout_seconds=30,
    )

    assert result.response == "done"
    assert len(procs) == 2
    assert procs[0].killed is True
    assert any(
        "ACPX_RETRY target=claude_code attempt=2/2 "
        "reason=idle_stall last_kind=tool_call" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_no_retry_on_deterministic_billing_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("KERNOS_ACPX_IDLE_ABORT_SEC", "1")
    monkeypatch.setenv("KERNOS_ACPX_CONSULT_RETRIES", "2")
    caplog.set_level(logging.WARNING)
    procs = _install_fake_acpx(monkeypatch, ["auth", "success"], tmp_path)

    with pytest.raises(ConsultationFailed, match="billing_error"):
        await acpx_adapter.dispatch(
            target="claude_code",
            prompt="work",
            workspace_dir=str(tmp_path),
            timeout_seconds=30,
        )

    assert len(procs) == 1
    assert not any("ACPX_RETRY" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_stall_retry_exhaustion_reports_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("KERNOS_ACPX_IDLE_ABORT_SEC", "0.05")
    monkeypatch.setenv("KERNOS_ACPX_CONSULT_RETRIES", "2")
    procs = _install_fake_acpx(
        monkeypatch, ["stall", "stall", "stall"], tmp_path,
    )

    with pytest.raises(ConsultationStalled) as caught:
        await acpx_adapter.dispatch(
            target="claude_code",
            prompt="work",
            workspace_dir=str(tmp_path),
            timeout_seconds=30,
        )

    assert len(procs) == 3
    assert caught.value.attempts == 3
    assert caught.value.last_event_kind == "tool_call"
    assert "attempts=3" in str(caught.value)
