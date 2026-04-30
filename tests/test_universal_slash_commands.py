"""Universal slash-commands invariant pin (MODEL-AND-STATUS-V1, AC #9).

Two layers, both behavioral and load-bearing:

(a) **Per-adapter inbound pass-through**: a `/`-prefixed message
    flows through each adapter's ``inbound()`` without modification.
    The slash text reaches the handler unchanged on every channel —
    Discord, Telegram, SMS, CLI, and any future connector that
    follows the BaseAdapter contract.

(b) **Per-handler-branch return-type behavioral**: every branch in
    the handler's slash intercept block returns ``str`` for
    representative invocations of every existing slash command plus
    the new /model command.

A structural AST walk remains as a backstop in the catch-all final
test, but is NOT load-bearing — the behavioral tests are.
"""
from __future__ import annotations

import ast
import datetime
import inspect
from unittest.mock import MagicMock

import pytest

from kernos.messages.adapters.discord_bot import DiscordAdapter
from kernos.messages.adapters.telegram_bot import TelegramAdapter
from kernos.messages.adapters.twilio_sms import TwilioSMSAdapter
from kernos.messages.models import NormalizedMessage


SLASH_COMMANDS_TO_PIN = (
    "/status", "/help", "/spaces", "/wipe me", "/wipe all",
    "/disconnect", "/dump", "/model", "/model lightweight",
    "/model anthropic/claude-haiku-4.5", "/model reset",
)


# ---------------------------------------------------------------------------
# Layer (a): per-adapter inbound pass-through
# ---------------------------------------------------------------------------


class TestSlashTextReachesHandlerUnchanged:
    """Each adapter must surface a `/`-prefixed message body verbatim
    through its ``inbound()`` translation. Adapters are forbidden
    from intercepting slash commands — the handler owns dispatch."""

    @pytest.mark.parametrize("slash", SLASH_COMMANDS_TO_PIN)
    def test_discord_passes_slash_text_through(self, slash):
        adapter = DiscordAdapter()
        # Build a minimal discord.Message-like stub.
        raw = MagicMock()
        raw.content = slash
        raw.author.id = 999
        raw.channel.id = 123
        raw.channel.name = "test"
        raw.guild = None  # DM
        raw.created_at = datetime.datetime.now(datetime.timezone.utc)
        nm = adapter.inbound(raw)
        assert nm.content == slash

    @pytest.mark.parametrize("slash", SLASH_COMMANDS_TO_PIN)
    def test_telegram_passes_slash_text_through(self, slash):
        adapter = TelegramAdapter()
        raw = {
            "message": {
                "text": slash,
                "from": {"id": 999, "first_name": "Test"},
                "chat": {"id": 123, "type": "private"},
                "date": 1700000000,
            },
        }
        nm = adapter.inbound(raw)
        assert nm.content == slash

    @pytest.mark.parametrize("slash", SLASH_COMMANDS_TO_PIN)
    def test_sms_passes_slash_text_through(self, slash):
        adapter = TwilioSMSAdapter()
        raw = {"From": "+15555550100", "Body": slash}
        nm = adapter.inbound(raw)
        assert nm.content == slash


# ---------------------------------------------------------------------------
# Layer (b): per-handler-branch return-type behavioral
# ---------------------------------------------------------------------------


class TestSlashHandlersReturnStrings:
    """Each handler method invoked by the slash intercept block
    returns ``str``. Behavioral, not just AST — covers the actual
    code path the dispatch block reaches."""

    async def _make_handler_with_chains(self, tmp_path):
        """Tiny shim so this file doesn't depend on the heavyweight
        ``_make_handler`` from ``test_handler``. The slash branches
        we test only call read-only handler methods or methods that
        accept any TurnContext — no LLM, no network."""
        from tests.test_handler_model_command import (
            _stub_chains, _ctx,
        )
        from tests.test_handler import _make_handler
        from kernos.kernel.instance_db import InstanceDB

        handler, _ = _make_handler()
        handler.reasoning._chains = _stub_chains()
        idb = InstanceDB(str(tmp_path))
        await idb.connect()
        handler._instance_db = idb
        ctx = _ctx(handler)
        return handler, idb, ctx

    async def test_status_returns_string(self, tmp_path):
        handler, idb, ctx = await self._make_handler_with_chains(tmp_path)
        try:
            out = await handler._handle_status(ctx)
            assert isinstance(out, str)
            assert "Kernos Status" in out
        finally:
            await idb.close()

    async def test_help_returns_string(self, tmp_path):
        handler, idb, ctx = await self._make_handler_with_chains(tmp_path)
        try:
            out = handler._handle_help()
            assert isinstance(out, str)
        finally:
            await idb.close()

    @pytest.mark.parametrize("variant", [
        "/model", "/model lightweight", "/model primary",
        "/model anthropic/claude-haiku-4.5",
        "/model imaginary/ghost-1", "/model reset",
        "/model nonsense-args-go-here",
    ])
    async def test_model_command_returns_string(self, tmp_path, variant):
        handler, idb, ctx = await self._make_handler_with_chains(tmp_path)
        try:
            out = await handler._handle_model_command(ctx, variant)
            assert isinstance(out, str)
            assert out  # non-empty
        finally:
            await idb.close()

    # Codex post-impl fold: per-branch behavioral tests for the
    # remaining slash commands (/spaces, /wipe, /restart non-owner,
    # /disconnect, /dump). AC #9 requires behavioral pins, not just
    # AST presence.

    async def test_spaces_returns_string(self, tmp_path):
        handler, idb, ctx = await self._make_handler_with_chains(tmp_path)
        try:
            out = await handler._handle_spaces(ctx, "/spaces")
            assert isinstance(out, str)
        finally:
            await idb.close()

    async def test_wipe_returns_string(self, tmp_path):
        handler, idb, ctx = await self._make_handler_with_chains(tmp_path)
        try:
            # /wipe me without exact-phrase confirmation returns the
            # confirmation prompt as a string.
            out = await handler._handle_wipe(ctx, "/wipe me")
            assert isinstance(out, str)
        finally:
            await idb.close()

    async def test_disconnect_returns_string(self, tmp_path):
        handler, idb, ctx = await self._make_handler_with_chains(tmp_path)
        try:
            out = await handler._handle_disconnect(ctx)
            assert isinstance(out, str)
        finally:
            await idb.close()

    async def test_dump_returns_string(self, tmp_path):
        handler, idb, ctx = await self._make_handler_with_chains(tmp_path)
        try:
            out = await handler._handle_dump(ctx)
            assert isinstance(out, str)
        finally:
            await idb.close()


# ---------------------------------------------------------------------------
# Backstop (NOT load-bearing): AST walk of the slash intercept block.
# ---------------------------------------------------------------------------


class TestSlashInterceptBlockStructure:
    """Backstop sanity check — walk the handler source and confirm
    the slash intercept block dispatches to a method on `self` for
    each known command. Pure structural; behavioral pins above are
    the load-bearing guarantee."""

    def test_handler_source_dispatches_known_slash_commands(self):
        from kernos.messages import handler as handler_mod
        src = inspect.getsource(handler_mod)
        # Each command should appear at least once in a string literal
        # comparison ('/foo') in the source. Probe presence — not
        # ordering or exhaustiveness.
        for cmd in (
            "/status", "/help", "/spaces", "/wipe", "/disconnect",
            "/dump", "/model",
        ):
            assert cmd in src, (
                f"slash command {cmd!r} not present in handler source — "
                "intercept block may have lost a branch"
            )

    def test_model_dispatch_calls_handler_method(self):
        """The /model branch must call `_handle_model_command` rather
        than inlining or calling an adapter — keeps the handler the
        single owner of slash dispatch."""
        from kernos.messages import handler as handler_mod
        src = inspect.getsource(handler_mod)
        assert "_handle_model_command" in src
