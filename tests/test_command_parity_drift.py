"""CLEANUP-BATCH-V1 item 10: command parity drift test.

Pins the *current* mapping between Discord-native slash commands and
the universal text-command dispatcher. The test is a baseline guard,
not a policy enforcer — it accepts today's state and fails when a
future change adds, renames, or drops a command on one surface
without updating the other.

Discord-only commands (no universal-dispatcher equivalent expected)
live in an explicit allow-list inside this file. To add a new
Discord-only command, add its name to ``DISCORD_ONLY_COMMANDS``.

To add a command on both surfaces, register it in both
``kernos/server.py`` (``@tree.command(name=...)``) and the universal
text-command dispatcher in ``kernos/messages/handler.py`` around the
``_cmd_lower == "/<name>"`` chain. The test asserts the two surfaces
agree.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Allow-list of Discord-only commands
# ---------------------------------------------------------------------------

# Commands intentionally exposed only as Discord app commands. Adding
# a new entry here is a deliberate decision; reviewers should ask
# whether the command should also be reachable from SMS / Telegram /
# CLI.
DISCORD_ONLY_COMMANDS: frozenset[str] = frozenset({
    "debug",  # Discord ephemeral diagnostic surface; raw IDs unsafe in SMS
})


# ---------------------------------------------------------------------------
# Discoverers
# ---------------------------------------------------------------------------

def _discord_slash_commands() -> set[str]:
    """Parse ``@tree.command(name=...)`` decorators out of server.py.

    Static parsing — no Discord imports — so the test runs without a
    live discord environment."""
    text = (REPO_ROOT / "kernos" / "server.py").read_text(encoding="utf-8")
    pattern = re.compile(r"@tree\.command\(\s*name=\"([^\"]+)\"")
    return set(pattern.findall(text))


def _universal_text_commands() -> set[str]:
    """Parse ``elif _cmd_lower == '/<name>'`` and
    ``elif _cmd_lower.startswith('/<name>')`` lines out of handler.py.

    The dispatcher uses the same conventions across the chain so a
    regex over the source captures every exact-match and prefix
    command without spinning up a handler."""
    text = (REPO_ROOT / "kernos" / "messages" / "handler.py").read_text(
        encoding="utf-8",
    )
    out: set[str] = set()
    for match in re.finditer(
        r"_cmd_lower\s*==\s*\"(/[a-z]+)\"", text,
    ):
        out.add(match.group(1).lstrip("/"))
    for match in re.finditer(
        r"_cmd_lower\.startswith\(\s*\"(/[a-z]+)\"", text,
    ):
        out.add(match.group(1).lstrip("/"))
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDiscoverersWork:
    """The regexes are load-bearing — sanity check they actually
    return something. If either set is empty the parity test below
    is meaningless."""

    def test_discord_slash_commands_discovered(self):
        cmds = _discord_slash_commands()
        assert len(cmds) >= 2, (
            f"Discord slash command discovery returned {cmds!r}; "
            "regex against server.py probably broke"
        )

    def test_universal_text_commands_discovered(self):
        cmds = _universal_text_commands()
        assert len(cmds) >= 2, (
            f"Universal text command discovery returned {cmds!r}; "
            "regex against handler.py probably broke"
        )


class TestParityBaseline:
    """The actual parity guard: every Discord slash command except
    those explicitly Discord-only must also exist in the universal
    text dispatcher.

    Failure means one of three things:
    1. A new Discord slash command was added without adding the
       universal text equivalent.
    2. A universal text command was removed but the Discord slash
       command is still there.
    3. The command should be Discord-only — add it to
       ``DISCORD_ONLY_COMMANDS``.
    """

    def test_discord_commands_have_universal_equivalents(self):
        discord_cmds = _discord_slash_commands()
        universal_cmds = _universal_text_commands()
        expected_universal = discord_cmds - DISCORD_ONLY_COMMANDS

        missing = expected_universal - universal_cmds
        assert not missing, (
            f"Discord slash command(s) {sorted(missing)} have no "
            "universal text-command equivalent. Either add the "
            "matching `elif _cmd_lower == \"/<name>\"` branch in "
            "kernos/messages/handler.py, or add the name to "
            "DISCORD_ONLY_COMMANDS in this test."
        )

    def test_allow_list_entries_are_actually_discord_native(self):
        """Sanity: anything in DISCORD_ONLY_COMMANDS should be a
        registered Discord slash command. Otherwise the allow-list
        is hiding a typo / stale entry."""
        discord_cmds = _discord_slash_commands()
        stale = DISCORD_ONLY_COMMANDS - discord_cmds
        assert not stale, (
            f"DISCORD_ONLY_COMMANDS contains {sorted(stale)} that "
            "are not registered Discord slash commands. Remove the "
            "stale entries."
        )
