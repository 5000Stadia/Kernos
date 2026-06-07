"""Focused regression pin for AUTO-WAKE-V1 inject_consult_completion_wake.

The wake-side test in test_workflow_autonomy_emitters.py verifies the
emitter's callback wiring + payload shape, but doesn't run the
HANDLER side of the chain. That gap let a method-name typo
(``self.handle`` vs ``self.process``) ship in c750a85: emitter
fired the wake_callback, .waked sentinel landed, but the synthetic
message AttributeError'd silently inside asyncio.create_task.

These tests pin the handler-side invariants without needing the
full handler fixture:
  * ``MessageHandler.inject_consult_completion_wake`` exists.
  * It calls ``self.process`` (the real method name) — NOT
    ``self.handle`` (which never existed).
  * The synthetic NormalizedMessage carries the right substrate
    shape (platform=system, member_id pre-resolved, execution_envelope
    source=consult_completion_wake).
  * Skipped cleanly with a warning when required fields are missing.

The handler is monkey-patched with a capture function instead of
running the full pipeline — the pin is the invocation contract,
not pipeline behavior (pipeline coverage is downstream).
"""
from __future__ import annotations

import asyncio
import pytest

from kernos.messages.handler import MessageHandler
from kernos.messages.models import NormalizedMessage


class _CaptureHandler:
    """Minimal stand-in that captures process() calls. Bound to
    MessageHandler.inject_consult_completion_wake via __get__ /
    direct method invocation so we test the real method body."""

    def __init__(self):
        self.process_calls: list[NormalizedMessage] = []
        self.process_exc: Exception | None = None

    async def process(self, message: NormalizedMessage) -> str:
        self.process_calls.append(message)
        return "captured"

    # Bind the real handler method so we test the actual code,
    # not a reimplementation.
    inject_consult_completion_wake = (
        MessageHandler.inject_consult_completion_wake
    )


class TestInjectConsultCompletionWakeContract:
    async def test_invokes_process_not_handle(self):
        """The bug that motivated this file: handler method is
        ``process``, not ``handle``. If anyone renames it back to
        ``handle`` or someone refactors process(), this test
        catches it before deploying a silent-failure shape."""
        # MessageHandler must expose a `process` method
        assert hasattr(MessageHandler, "process"), (
            "MessageHandler.process is the public turn entry point — "
            "inject_consult_completion_wake calls it"
        )
        # And NOT 'handle' (the typo'd name that silently shipped
        # in c750a85)
        assert not hasattr(MessageHandler, "handle"), (
            "MessageHandler.handle existed as a typo in "
            "inject_consult_completion_wake; if it now exists as a "
            "real method, update the wake injector"
        )

    async def test_wake_calls_process_with_synthetic_message(self):
        capture = _CaptureHandler()
        payload = {
            "request_id": "req_test_wake",
            "instance_id": "test_inst",
            "originating_member_id": "mem_test",
            "originating_space": "space_test_abc",
            "target": "codex",
            "investigation_outcome": "completed",
            "summary": "test summary text",
        }
        await capture.inject_consult_completion_wake(payload)
        # Give the create_task a moment to run
        for _ in range(10):
            await asyncio.sleep(0.01)
            if capture.process_calls:
                break

        assert len(capture.process_calls) == 1, (
            f"expected 1 process() call, got {len(capture.process_calls)}"
        )
        msg = capture.process_calls[0]
        # Substrate shape pins
        assert msg.platform == "system"
        assert msg.sender == "kernos-system"
        assert msg.instance_id == "test_inst"
        assert msg.member_id == "mem_test"
        assert msg.conversation_id == "space_test_abc"
        assert "test summary text" in msg.content
        assert "[system: external consult response arrived]" in msg.content
        # Context carries the wake source marker
        env = msg.context.get("execution_envelope", {})
        assert env.get("source") == "consult_completion_wake"
        assert env.get("request_id") == "req_test_wake"
        assert env.get("target") == "codex"

    async def test_wake_skipped_when_space_missing(self):
        capture = _CaptureHandler()
        payload = {
            "request_id": "req_x",
            "instance_id": "inst_x",
            "originating_member_id": "mem_x",
            "originating_space": "",  # MISSING
            "target": "codex",
            "investigation_outcome": "completed",
            "summary": "",
        }
        await capture.inject_consult_completion_wake(payload)
        await asyncio.sleep(0.05)
        # No process call because we short-circuited on missing space
        assert capture.process_calls == []

    async def test_wake_skipped_when_instance_missing(self):
        capture = _CaptureHandler()
        payload = {
            "request_id": "req_x",
            "instance_id": "",  # MISSING
            "originating_member_id": "mem_x",
            "originating_space": "space_x",
            "target": "codex",
            "investigation_outcome": "completed",
            "summary": "",
        }
        await capture.inject_consult_completion_wake(payload)
        await asyncio.sleep(0.05)
        assert capture.process_calls == []

    async def test_wake_surfaces_process_exceptions_via_logger(self, caplog):
        """Bug-class pin: if process() raises inside the fire-and-
        forget task, the exception must hit the logger — not get
        swallowed by asyncio.create_task. That's how the
        AttributeError typo escaped detection in c750a85."""
        capture = _CaptureHandler()

        async def boom_process(message):
            raise RuntimeError("simulated process failure")

        capture.process = boom_process
        payload = {
            "request_id": "req_boom",
            "instance_id": "inst_boom",
            "originating_member_id": "mem_boom",
            "originating_space": "space_boom",
            "target": "codex",
            "investigation_outcome": "completed",
            "summary": "ok",
        }
        import logging
        with caplog.at_level(logging.ERROR):
            await capture.inject_consult_completion_wake(payload)
            # Yield a few times so the create_task can run + log
            for _ in range(20):
                await asyncio.sleep(0.01)
                if any(
                    "CONSULT_WAKE_TURN_CRASHED" in r.message
                    for r in caplog.records
                ):
                    break

        crash_logs = [
            r for r in caplog.records
            if "CONSULT_WAKE_TURN_CRASHED" in r.message
        ]
        assert crash_logs, (
            "process() exception must be logged via "
            "CONSULT_WAKE_TURN_CRASHED, not silently swallowed"
        )


class TestSystemPlatformBypass:
    """Pins the _check_early_return bypass for platform=system.

    Without this bypass, _resolve_incoming treats system-injected
    messages as unknown-sender invasion attempts → records a
    sender_failure → returns the 'private Kernos' static response
    → short-circuits the entire pipeline. The wake never reaches
    the turn.
    """

    def test_check_early_return_recognizes_platform_system(self):
        """Static-analysis pin: the bypass branch exists in
        _check_early_return for platform=system before the
        unknown-sender resolution path runs."""
        import inspect
        src = inspect.getsource(MessageHandler._check_early_return)
        # The bypass must be present
        assert 'message.platform == "system"' in src, (
            "_check_early_return must short-circuit _resolve_incoming "
            "for platform=system messages — without this, "
            "wake-injected messages get blocked by the unknown-sender "
            "abuse-prevention path"
        )
        # And it must be BEFORE the _resolve_incoming call
        bypass_pos = src.find('message.platform == "system"')
        resolve_pos = src.find('_resolve_incoming(')
        assert 0 < bypass_pos < resolve_pos, (
            "platform=system bypass must come BEFORE _resolve_incoming "
            "or it has no effect"
        )


class TestInternalPlatformBypass:
    """Pins the self-directed bypass for platform=internal.

    Self-directed plan steps (_execute_self_directed_step) send a
    synthetic NormalizedMessage with platform="internal",
    sender="self_directed". Without this bypass, _resolve_incoming
    treats it as an unknown external sender: it isn't a known member,
    so record_sender_failure escalates it into a self-block, and every
    subsequent plan step short-circuits via check_sender_blocked with
    the 'private Kernos' static response (the uniform response_len=22
    stub observed on a live 17-step self-test run where the spine
    auto-advanced but no step actually executed). Same failure mode and
    remedy as the platform=system wake bypass above.
    """

    def test_check_early_return_recognizes_platform_internal(self):
        import inspect
        src = inspect.getsource(MessageHandler._check_early_return)
        assert 'message.platform == "internal"' in src, (
            "_check_early_return must short-circuit _resolve_incoming "
            "for platform=internal messages — without this, self-directed "
            "plan steps get blocked by the unknown-sender abuse path and "
            "every step returns the 'private Kernos' stub"
        )
        bypass_pos = src.find('message.platform == "internal"')
        resolve_pos = src.find('_resolve_incoming(')
        assert 0 < bypass_pos < resolve_pos, (
            "platform=internal bypass must come BEFORE _resolve_incoming "
            "or it has no effect"
        )

    def test_plan_management_hallucination_aliases(self):
        # 2026-06-07 live dump: model emitted `plan_management` which was
        # 'not registered' before falling back to manage_plan.
        from kernos.kernel.tool_aliases import canonicalize_tool_name
        assert canonicalize_tool_name("plan_management") == ("manage_plan", True)
