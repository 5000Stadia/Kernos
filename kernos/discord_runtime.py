"""DiscordRuntime — the Discord-only server surface (CLI-FIRST-CORE-V1 A3).

This module grows to own the whole Discord chassis (client + tree,
command handlers, gateway watchdog, deferred-delivery flusher,
presence, tree sync). Unit 2 lands the **async 429 smart-backoff** —
the ``await client.start()`` equivalent of server.py's synchronous
``_run_with_429_smart_backoff`` — with the same env-overridable
schedule and the same operator text, plus explicit close/cancellation
semantics for the supervised main (spec A2/A3).

No module in the kernel imports this; it sits at the server layer and
is imported only when a Discord token is configured.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    import discord

logger = logging.getLogger(__name__)

# Canonical home of the schedule (server.py's copy retires in the A3
# extraction). Env-overridable, same knobs as before.
DISCORD_429_BACKOFF_SCHEDULE: list[int] = [
    int(os.getenv("KERNOS_DISCORD_429_BACKOFF_1_SEC", "60")),     # 1 minute
    int(os.getenv("KERNOS_DISCORD_429_BACKOFF_2_SEC", "300")),    # 5 minutes
    int(os.getenv("KERNOS_DISCORD_429_BACKOFF_3_SEC", "1800")),   # 30 minutes
    int(os.getenv("KERNOS_DISCORD_429_BACKOFF_4_SEC", "3600")),   # 1 hour
    int(os.getenv("KERNOS_DISCORD_429_BACKOFF_5_SEC", "14400")),  # 4 hours
]


def format_429_wait_duration(seconds: int) -> str:
    """Render a wait duration as plain English for operator surfacing."""
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    if seconds < 3600:
        m = seconds // 60
        return f"{m} minute{'s' if m != 1 else ''}"
    h = seconds / 3600
    if h == int(h):
        ih = int(h)
        return f"{ih} hour{'s' if ih != 1 else ''}"
    return f"{h:.1f} hours"


def _give_up_operator_text(attempts: int) -> str:
    return (
        "\n" + "=" * 64 + "\n"
        f"DISCORD RATE LIMIT — backoff schedule exhausted "
        f"({attempts} retries)\n"
        + "=" * 64 + "\n"
        "The token is likely Cloudflare-flagged. The flag\n"
        "typically persists 4-24+ hours after the abuse stops.\n"
        "Either wait longer, OR rotate the bot token:\n"
        "  1. https://discord.com/developers/applications\n"
        "  2. Open the application this bot belongs to\n"
        "  3. Bot tab -> Reset Token\n"
        "  4. Update DISCORD_BOT_TOKEN in .env\n"
        "  5. Re-run start.sh\n"
    )


async def _close_quietly(client: "discord.Client") -> None:
    """Close the client, swallowing close-path errors (shutdown lane)."""
    try:
        await client.close()
    except Exception as exc:  # noqa: BLE001 - close is best-effort
        logger.debug("Discord client close during backoff raised: %s", exc)


async def run_discord_with_429_smart_backoff(
    client: "discord.Client",
    token: str,
    *,
    schedule: list[int] | None = None,
    sleep=asyncio.sleep,
) -> None:
    """Async equivalent of the sync 429 smart-backoff runner (spec A3).

    Semantics mirrored from ``_run_with_429_smart_backoff``:
    - 429 HTTPException escaping discord.py's internal retry → close the
      client, wait the scheduled duration (same log line, human wait,
      resume timestamp, code), re-attempt ``client.start``.
    - Non-429 HTTPException (PrivilegedIntentsRequired, LoginFailure,
      other statuses) → close and re-raise so the supervisor's friendly
      remediation handlers fire.
    - Schedule exhausted → same give-up log + operator text, re-raise.

    New, supervised-lifecycle semantics (not in the sync version):
    - ``asyncio.CancelledError`` (supervisor shutdown) → close the
      client, propagate. Cancellation during the backoff sleep also
      closes before propagating.
    - Graceful return when ``client.start`` returns (shutdown).

    ``schedule``/``sleep`` are injectable for tests; defaults are the
    canonical schedule and ``asyncio.sleep``.
    """
    import discord  # deferred: only a Discord-configured process arrives here

    _schedule = DISCORD_429_BACKOFF_SCHEDULE if schedule is None else schedule
    schedule_len = len(_schedule)
    attempt = 0
    while True:
        try:
            await client.start(token)
            return  # graceful shutdown
        except asyncio.CancelledError:
            await _close_quietly(client)
            raise
        except discord.HTTPException as exc:
            if exc.status != 429:
                await _close_quietly(client)
                raise
            await _close_quietly(client)
            if attempt >= schedule_len:
                logger.error(
                    "DISCORD_429_GIVE_UP: %d retries exhausted. "
                    "Token likely Cloudflare-flagged. Wait several "
                    "hours OR rotate the bot token in the Discord "
                    "Developer Portal (Bot -> Reset Token). "
                    "status=%d code=%s",
                    attempt, exc.status, getattr(exc, "code", "?"),
                )
                print(
                    _give_up_operator_text(attempt),
                    file=sys.stderr, flush=True,
                )
                raise
            wait = _schedule[attempt]
            attempt += 1
            code = getattr(exc, "code", None)
            human = format_429_wait_duration(wait)
            retry_at = datetime.now() + timedelta(seconds=wait)
            logger.warning(
                "DISCORD_AUTH_429: backing off %s (attempt %d/%d, "
                "resume %s, code=%s)",
                human, attempt, schedule_len,
                retry_at.strftime("%H:%M:%S"), code,
            )
            try:
                await sleep(wait)
            except asyncio.CancelledError:
                await _close_quietly(client)
                raise
