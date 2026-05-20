"""Tests for the /codex + /cc operator-driven slash commands
(DIRECT-BROKER-V1, 2026-05-20).

Why these exist: founder needs a path to verify the CC/Codex
broker chain end-to-end without depending on the agent's tool-
calling judgment. The agent has fumbled `consult` calls multiple
times in live testing (empty harness, invented tool names like
`external_agent_consult.cc`). Slash commands bypass the agent
entirely — the prompt goes from Discord directly through
`acpx_adapter.dispatch` and the response comes straight back.

These tests don't try to spin up a Discord interaction (heavy);
instead they pin the surface-level invariants that matter:
* The slash commands are registered with the discord tree
* They route through `_direct_broker_dispatch` with the right target
* `_direct_broker_dispatch` enforces owner gating + empty-prompt
  validation BEFORE deferring (so unauthorized users don't
  consume a defer)
"""
from __future__ import annotations

import asyncio
import inspect

import pytest


class TestSlashCommandsRegistered:
    def test_codex_command_exists(self):
        import kernos.server as server
        assert hasattr(server, "codex_command")
        # discord.py wraps slash commands in Command/AppCommand objects
        cmd = server.codex_command
        # The function-equivalent should be callable (either directly
        # or via .callback)
        assert callable(cmd) or callable(getattr(cmd, "callback", None))

    def test_cc_command_exists(self):
        import kernos.server as server
        assert hasattr(server, "cc_command")
        cmd = server.cc_command
        assert callable(cmd) or callable(getattr(cmd, "callback", None))

    def test_direct_broker_dispatch_exists(self):
        import kernos.server as server
        assert hasattr(server, "_direct_broker_dispatch")
        # Verify signature exposes the right kwargs
        sig = inspect.signature(server._direct_broker_dispatch)
        params = sig.parameters
        assert "interaction" in params
        assert "target" in params
        assert "prompt" in params


class TestOwnerGateAndValidation:
    """Pin the safety semantics of _direct_broker_dispatch without
    actually spinning up Discord."""

    async def test_unauthorized_user_blocked(self):
        import kernos.server as server

        class _FakeInteraction:
            class user:
                id = 999_999_999  # not the owner

            class response:
                _sent: list[tuple[str, dict]] = []

                @classmethod
                async def send_message(cls, msg, **kw):
                    cls._sent.append((msg, kw))

                @classmethod
                async def defer(cls, **kw):
                    raise AssertionError(
                        "defer should NOT be called for unauthorized user"
                    )

        await server._direct_broker_dispatch(
            _FakeInteraction(), target="codex", prompt="anything",
        )
        sent = _FakeInteraction.response._sent
        assert len(sent) == 1
        assert "Not authorized" in sent[0][0]
        assert sent[0][1].get("ephemeral") is True

    async def test_empty_prompt_rejected_before_dispatch(self):
        import kernos.server as server

        class _FakeInteraction:
            class user:
                id = server.OWNER_USER_ID  # authorized

            class response:
                _sent: list[tuple[str, dict]] = []

                @classmethod
                async def send_message(cls, msg, **kw):
                    cls._sent.append((msg, kw))

                @classmethod
                async def defer(cls, **kw):
                    raise AssertionError(
                        "defer should NOT be called for empty prompt"
                    )

        await server._direct_broker_dispatch(
            _FakeInteraction(), target="codex", prompt="   ",
        )
        sent = _FakeInteraction.response._sent
        assert len(sent) == 1
        assert "non-empty prompt" in sent[0][0]

    async def test_successful_dispatch_returns_response(self, monkeypatch):
        """Mock acpx_adapter.dispatch and verify
        _direct_broker_dispatch sends the response via followup."""
        import kernos.server as server
        from kernos.kernel.external_agents.harness import ConsultResult

        sent: list = []

        class _FakeInteraction:
            class user:
                id = server.OWNER_USER_ID

            class response:
                @classmethod
                async def defer(cls, **kw):
                    pass

            class followup:
                @classmethod
                async def send(cls, msg, **kw):
                    sent.append(("followup", msg))

            class channel:
                @classmethod
                async def send(cls, msg, **kw):
                    sent.append(("channel", msg))

        async def fake_dispatch(**kwargs):
            assert kwargs["target"] == "codex"
            assert kwargs["prompt"] == "test prompt"
            return ConsultResult(
                response="fake response from codex",
                harness="codex",
                session_id="",
                native_session_ref="",
                metadata={"acpx_stop_reason": "end_turn"},
                truncated=False,
            )

        # Patch the dispatch the slash command imports inline
        import kernos.kernel.external_agents.acpx_adapter as adapter
        monkeypatch.setattr(adapter, "dispatch", fake_dispatch)

        await server._direct_broker_dispatch(
            _FakeInteraction(), target="codex", prompt="test prompt",
        )
        assert len(sent) == 1
        body = sent[0][1]
        assert "Codex" in body
        assert "fake response from codex" in body
        assert "end_turn" in body

    async def test_failed_dispatch_surfaces_error(self, monkeypatch):
        import kernos.server as server
        from kernos.kernel.external_agents.errors import ConsultationFailed

        sent: list = []

        class _FakeInteraction:
            class user:
                id = server.OWNER_USER_ID

            class response:
                @classmethod
                async def defer(cls, **kw):
                    pass

            class followup:
                @classmethod
                async def send(cls, msg, **kw):
                    sent.append(msg)

        async def fake_dispatch(**kwargs):
            raise ConsultationFailed("synthetic dispatch failure")

        import kernos.kernel.external_agents.acpx_adapter as adapter
        monkeypatch.setattr(adapter, "dispatch", fake_dispatch)

        await server._direct_broker_dispatch(
            _FakeInteraction(), target="codex", prompt="test",
        )
        assert len(sent) == 1
        assert "failed" in sent[0]
        assert "synthetic dispatch failure" in sent[0]
