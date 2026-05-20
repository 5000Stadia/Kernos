import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

import sys
from typing import Any

import discord
from discord import app_commands
from dotenv import load_dotenv
from mcp import StdioServerParameters

import dataclasses

from kernos.messages.adapters.discord_bot import DiscordAdapter
from kernos.messages.handler import MessageHandler
from kernos.capability.client import AuthCommand, MCPClientManager
from kernos.capability.known import KNOWN_CAPABILITIES
from kernos.capability.registry import CapabilityRegistry, CapabilityStatus
from kernos.kernel.event_types import EventType
from kernos.kernel.events import JsonEventStream, emit_event
from kernos.kernel.engine import TaskEngine
from kernos.kernel.reasoning import ReasoningRequest, ReasoningService
from kernos.kernel.state_json import JsonStateStore
from kernos.persistence.json_file import JsonAuditStore, JsonConversationStore, JsonInstanceStore

load_dotenv()


class _ColorFormatter(logging.Formatter):
    """Console formatter that color-codes log lines by event type."""

    # ANSI color codes
    _COLORS = {
        "ROUTE": "\033[36m",        # cyan — routing decisions
        "SPACE_SWITCH": "\033[35m",  # magenta — space changes
        "TOOL_": "\033[33m",         # yellow — tool surfacing/budget/promotion
        "REASON_": "\033[32m",       # green — reasoning/LLM
        "LLM_": "\033[32m",          # green — LLM calls
        "CODEX_": "\033[32m",        # green — provider
        "COMPACTION": "\033[34m",    # blue — compaction
        "FACT_HARVEST": "\033[34m",  # blue — fact harvest
        "DOMAIN_": "\033[35m",       # magenta — domain creation/migration
        "GATE": "\033[91m",          # bright red — gate decisions
        "PLAN_": "\033[95m",         # bright magenta — plan execution
        "CODE_EXEC": "\033[93m",     # bright yellow — code execution
        "WORKSPACE": "\033[93m",     # bright yellow — workspace
        "CROSS_DOMAIN": "\033[96m",  # bright cyan — cross-domain signals
        "AWARENESS": "\033[96m",     # bright cyan — awareness
        "WARNING": "\033[91m",       # bright red — warnings
        "ERROR": "\033[91m",         # bright red — errors
        "FRICTION": "\033[91m",      # bright red — friction
        "MESSAGE_ANALYSIS": "\033[36m",  # cyan — message analyzer
        "PHASE_TIMING": "\033[90m",  # gray — timing (low priority)
        "TURN_TIMING": "\033[90m",   # gray — timing
    }
    _RESET = "\033[0m"

    def format(self, record):
        msg = super().format(record)
        # Check for event prefixes in the message
        for prefix, color in self._COLORS.items():
            if prefix in record.getMessage():
                return f"{color}{msg}{self._RESET}"
        # Color by level
        if record.levelno >= logging.ERROR:
            return f"\033[91m{msg}{self._RESET}"
        if record.levelno >= logging.WARNING:
            return f"\033[93m{msg}{self._RESET}"
        return msg


_handler = logging.StreamHandler()
_handler.setFormatter(_ColorFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"))
logging.root.addHandler(_handler)
logging.root.setLevel(logging.INFO)
# Prevent duplicate output from basicConfig
for h in logging.root.handlers:
    if h is not _handler:
        logging.root.removeHandler(h)

# Install in-memory ring buffer of recent log records so /dump can include
# a RECENT LOG section. Lets operators see substrate AND runtime evidence
# (CODEX_REQUEST tools=N, TOOL_SURFACING, etc.) in the same artifact.
# See kernos/kernel/log_buffer.py for capacity + env override.
from kernos.kernel.log_buffer import (
    install_log_file_handler,
    install_log_ring_buffer,
)
install_log_ring_buffer()
# LOG-PERSIST-V1 (2026-05-19): also write all logs to a rotating
# file under data/<instance>/diagnostics/server.log so post-crash
# RCA actually has evidence. The in-memory ring buffer is wiped
# on restart; the file survives. Discord's `Heartbeat blocked`
# warnings — the smoking gun for the 2026-05-19 silent-gateway
# failure mode — land here. Best-effort: if data_dir is unwritable
# the bot continues without file logging.
install_log_file_handler(
    data_dir=os.getenv("KERNOS_DATA_DIR", "./data"),
)

logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
# DISCORD-429-SHORT-CIRCUIT (2026-05-08): max_ratelimit_timeout caps
# discord.py's internal retry-on-429 sleep. When discord.py would
# sleep longer than this value before retrying, it raises RateLimited
# immediately — bypassing the 5x retry loop that compounds the abuse
# flag on every 429 response. discord.py CLAMPS this to a 30-second
# minimum when set via the Client constructor (HTTPClient.__init__:544
# does ``max(30.0, max_ratelimit_timeout)``). We need shorter, so
# bypass the clamp via direct assignment after construction. The
# request loop reads ``self.max_ratelimit_timeout`` (public attribute)
# without re-clamping, so our value sticks.
_DISCORD_MAX_RETRY_SLEEP = float(
    os.getenv("KERNOS_DISCORD_MAX_RETRY_SLEEP_SEC", "0.5")
)
client.http.max_ratelimit_timeout = _DISCORD_MAX_RETRY_SLEEP
tree = app_commands.CommandTree(client)
adapter = DiscordAdapter()

OWNER_USER_ID = int(os.getenv("DISCORD_OWNER_ID", "0"))
_PENDING_CONFIRMATION_PATH = Path("/tmp/kernos_pending_confirmation.json")


def _write_pending_confirmation(channel_id: int, message: str, delete_message_id: int = 0) -> None:
    """Write a pending confirmation file for the new process to pick up."""
    data = {"channel_id": channel_id, "message": message}
    if delete_message_id:
        data["delete_message_id"] = delete_message_id
    _PENDING_CONFIRMATION_PATH.write_text(json.dumps(data))


@tree.command(name="restart", description="Restart the Kernos bot")
async def restart_command(interaction: discord.Interaction) -> None:
    if interaction.user.id != OWNER_USER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    logger.info("Restart requested by %s", interaction.user)
    await interaction.response.send_message("Restarting...", ephemeral=True)
    restart_msg = await interaction.channel.send("⏳")
    _write_pending_confirmation(interaction.channel_id, "Ready.", delete_message_id=restart_msg.id)
    os.execv(sys.executable, [sys.executable] + sys.argv)


# Owner-only direct broker dispatch (DIRECT-BROKER-V1, 2026-05-20):
# Bypasses the LLM-driven `consult` tool path entirely. Lets the
# operator verify the CC/Codex broker chain end-to-end without
# depending on the agent's tool-calling judgment. Use:
#   /codex Reply with exactly: hello
#   /cc How many files are in the kernos/ directory?
async def _direct_broker_dispatch(
    interaction: "discord.Interaction",
    *,
    target: str,
    prompt: str,
    timeout_seconds: int = 180,
) -> None:
    if interaction.user.id != OWNER_USER_ID:
        await interaction.response.send_message(
            "Not authorized.", ephemeral=True,
        )
        return
    if not prompt or not prompt.strip():
        await interaction.response.send_message(
            f"Pass a non-empty prompt: `/{target.split('_')[0]} <prompt>`",
            ephemeral=True,
        )
        return
    # Defer publicly so the response is visible to the channel
    await interaction.response.defer(ephemeral=False)
    from kernos.kernel.external_agents.acpx_adapter import dispatch
    from kernos.kernel.external_agents.errors import (
        ConsultationFailed, ConsultationTimeout, HarnessUnavailable,
    )
    workspace_dir = os.getenv("KERNOS_DATA_DIR", "./data")
    # Use repo root for the broker's --cwd so it can read code if asked
    repo_root = str(Path(__file__).resolve().parent.parent)
    try:
        result = await dispatch(
            target=target,
            prompt=prompt,
            workspace_dir=repo_root,
            timeout_seconds=timeout_seconds,
        )
    except (ConsultationFailed, ConsultationTimeout, HarnessUnavailable) as exc:
        await interaction.followup.send(
            f"**/{target.split('_')[0]}** failed:\n```\n{exc}\n```"[:1900],
        )
        return
    except Exception as exc:
        await interaction.followup.send(
            f"**/{target.split('_')[0]}** unexpected error:\n"
            f"```\n{type(exc).__name__}: {exc}\n```"[:1900],
        )
        return
    response_text = result.response or "(empty response)"
    label = "Codex" if target == "codex" else "Claude Code"
    body = (
        f"**{label}** "
        f"(stop_reason={result.metadata.get('acpx_stop_reason', '?')}):\n"
        f"{response_text}"
    )
    # Chunk to fit Discord's 2000-char limit (subtracting header overhead)
    if len(body) <= 1900:
        await interaction.followup.send(body)
        return
    # Multi-chunk send
    chunks = _chunk_response(body)
    first = True
    for chunk in chunks:
        if first:
            await interaction.followup.send(chunk)
            first = False
        else:
            await interaction.channel.send(chunk)


@tree.command(
    name="codex",
    description=(
        "Dispatch a prompt directly to Codex via ACPX. "
        "Bypasses the agent's consult tool — for operator-driven broker testing."
    ),
)
@app_commands.describe(prompt="The full prompt to send to Codex.")
async def codex_command(
    interaction: discord.Interaction, prompt: str,
) -> None:
    await _direct_broker_dispatch(
        interaction, target="codex", prompt=prompt,
    )


@tree.command(
    name="cc",
    description=(
        "Dispatch a prompt directly to Claude Code via ACPX. "
        "Bypasses the agent's consult tool — for operator-driven broker testing."
    ),
)
@app_commands.describe(prompt="The full prompt to send to Claude Code.")
async def cc_command(
    interaction: discord.Interaction, prompt: str,
) -> None:
    await _direct_broker_dispatch(
        interaction, target="claude_code", prompt=prompt,
    )


@tree.command(name="debug", description="Show diagnostic data: friction, trace, specs")
@app_commands.describe(category="What to show: friction, trace, specs")
async def debug_command(interaction: discord.Interaction, category: str = "trace") -> None:
    if interaction.user.id != OWNER_USER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
    instance_id = os.getenv("KERNOS_INSTANCE_ID", "")

    if category == "friction":
        friction_dir = Path(data_dir) / "diagnostics" / "friction"
        if not friction_dir.exists():
            await interaction.followup.send("No friction reports.", ephemeral=True)
            return
        reports = sorted(friction_dir.glob("FRICTION_*.md"), reverse=True)[:5]
        if not reports:
            await interaction.followup.send("No friction reports.", ephemeral=True)
            return
        lines = []
        for rpt in reports:
            content = rpt.read_text(encoding="utf-8")[:300]
            lines.append(f"**{rpt.stem}**\n```\n{content}\n```")
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)

    elif category == "trace":
        if handler and hasattr(handler, '_runtime_trace'):
            import asyncio
            events = await handler._runtime_trace.read(instance_id, turns=10)
            if not events:
                await interaction.followup.send("No trace events.", ephemeral=True)
                return
            lines = []
            for e in events[-30:]:
                lines.append(
                    f"`[{e.get('level', '?')[0].upper()}]` "
                    f"{e.get('source', '?')}:**{e.get('event', '?')}** — {e.get('detail', '')[:100]}"
                )
            await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)
        else:
            await interaction.followup.send("Runtime trace not available.", ephemeral=True)

    elif category == "specs":
        from kernos.utils import _safe_name
        specs_base = Path(data_dir) / _safe_name(instance_id) / "specs"
        lines = []
        for stage in ("proposed", "submitted", "implemented"):
            stage_dir = specs_base / stage
            if stage_dir.exists():
                specs = list(stage_dir.glob("*.md"))
                if specs:
                    lines.append(f"**{stage.upper()}:** {len(specs)} specs")
                    for s in specs[:3]:
                        first_line = s.read_text(encoding="utf-8").split("\n")[0][:80]
                        lines.append(f"  - {s.stem}: {first_line}")
        if not lines:
            await interaction.followup.send("No specs.", ephemeral=True)
            return
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)

    else:
        await interaction.followup.send(
            "Usage: `/debug friction` | `/debug trace` | `/debug specs`",
            ephemeral=True,
        )


@tree.command(name="wipe", description="Wipe all data and start fresh (factory reset)")
async def wipe_command(interaction: discord.Interaction) -> None:
    global handler

    if interaction.user.id != OWNER_USER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    data_dir = Path(os.getenv("KERNOS_DATA_DIR", "./data"))

    print("\033[2J\033[H", end="", flush=True)  # Clear console — fresh start
    await interaction.response.send_message("Wiping...", ephemeral=True)
    await client.change_presence(activity=discord.Activity(
        type=discord.ActivityType.playing, name="factory reset..."))
    wipe_msg = await interaction.channel.send("⏳")
    _write_pending_confirmation(interaction.channel_id, "Ready.", delete_message_id=wipe_msg.id)

    # 1. Null the handler so on_message rejects new messages during wipe.
    current_handler = handler
    handler = None

    # 2. Stop the awareness evaluator — it writes to data/ on a timer.
    if current_handler and getattr(current_handler, "_evaluator", None):
        try:
            await current_handler._evaluator.stop()
            logger.info("Wipe: awareness evaluator stopped")
        except Exception as exc:
            logger.warning("Wipe: failed to stop evaluator: %s", exc)

    # 3. Disconnect MCP servers — release file handles and child processes.
    if current_handler and current_handler.mcp:
        try:
            await current_handler.mcp.disconnect_all()
            logger.info("Wipe: MCP servers disconnected")
        except Exception as exc:
            logger.warning("Wipe: failed to disconnect MCP: %s", exc)

    # 4. Delete everything inside data/ — produces a truly blank state.
    #    .env and secrets/ live outside data/ so they are never touched.
    #    All tenant data (conversations, state, events, spaces, awareness,
    #    compaction, audit, archive) lives under data/{instance_id}/.
    if data_dir.exists():
        shutil.rmtree(data_dir)
        logger.info("Wipe: removed %s", data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Wipe: recreated empty %s", data_dir)

    # 5. Restart the process — all in-memory state is discarded.
    os.execv(sys.executable, [sys.executable] + sys.argv)


# None until on_ready completes MCP setup.
handler: MessageHandler | None = None


# ---------------------------------------------------------------------------
# Discord gateway watchdog (DISCORD-GATEWAY-WATCHDOG-V1, 2026-05-19)
# ---------------------------------------------------------------------------
#
# Live failure mode (2026-05-19 14:24): Discord gateway WebSocket
# closed server-side (FIN from 65.8.53.8), bot accumulated CLOSE_WAIT
# sockets with 25 bytes unread each, discord.py's auto-reconnect did
# NOT recover the session, asyncio event loop stayed idle in ep_poll
# for 20+ min, no incoming messages reached on_message. Bot had to
# be manually restarted to recover.
#
# Defense-in-depth: watchdog observes ``client.latency`` (heartbeat
# RTT). When the heartbeat is broken (latency is inf / NaN / >> 60s)
# for N consecutive checks, force a clean restart via os.execv. This
# matches what /restart does and what the network actually needs:
# fresh gateway IDENTIFY.
#
# Why latency, not "no incoming events": users may legitimately be
# quiet for hours. We must NOT restart for normal idle. Heartbeat is
# the gateway-level health signal that's independent of user activity.

_DISCORD_WATCHDOG_INTERVAL_SEC: int = int(
    os.getenv("KERNOS_DISCORD_WATCHDOG_INTERVAL_SEC", "120"),
)
_DISCORD_WATCHDOG_LATENCY_THRESHOLD_SEC: float = float(
    os.getenv("KERNOS_DISCORD_WATCHDOG_LATENCY_THRESHOLD_SEC", "60"),
)
_DISCORD_WATCHDOG_STRIKES_TO_RESTART: int = int(
    os.getenv("KERNOS_DISCORD_WATCHDOG_STRIKES_TO_RESTART", "3"),
)
_DISCORD_WATCHDOG_DISABLE: bool = (
    os.getenv("KERNOS_DISCORD_WATCHDOG_DISABLE", "").strip() == "1"
)

# Mutable state. _last_inbound_event_ts is updated by on_message and
# the gateway lifecycle events (on_ready, on_resumed) so the watchdog
# can correlate "heartbeat broken" with "no recent activity" before
# choosing to restart.
_last_inbound_event_ts: float = 0.0
_gateway_unhealthy_strikes: int = 0


def _mark_inbound_event() -> None:
    """Bump ``_last_inbound_event_ts``. Called from on_message,
    on_ready, on_resumed."""
    global _last_inbound_event_ts
    _last_inbound_event_ts = _time_module.time()


def _is_gateway_heartbeat_unhealthy() -> tuple[bool, str]:
    """Return ``(unhealthy, reason)``. The heartbeat is unhealthy
    when ``client.latency`` is non-finite or grossly exceeds the
    expected Discord gateway heartbeat interval (~41.25s)."""
    import math
    try:
        latency = client.latency  # seconds, float
    except Exception as exc:
        return True, f"client.latency raised: {exc}"
    if latency is None:
        return True, "client.latency is None"
    try:
        finite = math.isfinite(latency)
    except (TypeError, ValueError):
        return True, f"client.latency is non-numeric: {latency!r}"
    if not finite:
        return True, f"client.latency is non-finite: {latency}"
    if latency <= 0:
        return True, f"client.latency is non-positive: {latency}"
    if latency > _DISCORD_WATCHDOG_LATENCY_THRESHOLD_SEC:
        return True, (
            f"client.latency={latency:.1f}s exceeds threshold "
            f"{_DISCORD_WATCHDOG_LATENCY_THRESHOLD_SEC}s"
        )
    return False, f"latency={latency:.3f}s OK"


def _watchdog_tick() -> str:
    """One observation of the gateway. Updates ``_gateway_unhealthy_strikes``
    and triggers ``os.execv`` (via the module attribute so tests can
    monkey-patch) when the strike count reaches the configured
    threshold. Returns a short status code:

      * ``"ok"`` — gateway healthy this tick
      * ``"recovered"`` — gateway healthy after prior strikes
      * ``"strike"`` — gateway unhealthy, strikes incremented
      * ``"restart"`` — strikes hit threshold, execv called

    Pure of timing concerns (the loop owns sleep). Easier to test
    than driving the full loop through asyncio.
    """
    global _gateway_unhealthy_strikes
    unhealthy, reason = _is_gateway_heartbeat_unhealthy()
    if not unhealthy:
        if _gateway_unhealthy_strikes > 0:
            logger.info(
                "DISCORD_GATEWAY_WATCHDOG_RECOVERED "
                "strikes_cleared=%d reason=%s",
                _gateway_unhealthy_strikes, reason,
            )
            _gateway_unhealthy_strikes = 0
            return "recovered"
        _gateway_unhealthy_strikes = 0
        return "ok"
    _gateway_unhealthy_strikes += 1
    logger.warning(
        "DISCORD_GATEWAY_WATCHDOG_STRIKE strike=%d/%d reason=%s",
        _gateway_unhealthy_strikes,
        _DISCORD_WATCHDOG_STRIKES_TO_RESTART,
        reason,
    )
    if _gateway_unhealthy_strikes >= _DISCORD_WATCHDOG_STRIKES_TO_RESTART:
        idle_sec = (
            _time_module.time() - _last_inbound_event_ts
            if _last_inbound_event_ts else -1.0
        )
        logger.error(
            "DISCORD_GATEWAY_WATCHDOG_FORCE_RESTART "
            "strikes=%d reason=%s idle_sec=%.0f — restarting "
            "process via execv",
            _gateway_unhealthy_strikes, reason, idle_sec,
        )
        # Same restart path as /restart command — replaces the
        # process, re-establishes a fresh Discord IDENTIFY,
        # clears any stuck WebSocket state.
        os.execv(sys.executable, [sys.executable] + sys.argv)
        return "restart"
    return "strike"


async def _discord_gateway_watchdog_loop() -> None:
    """Periodically check the Discord gateway heartbeat. After
    ``_DISCORD_WATCHDOG_STRIKES_TO_RESTART`` consecutive bad ticks,
    log a loud warning and ``os.execv`` to restart cleanly.

    Disabled when ``KERNOS_DISCORD_WATCHDOG_DISABLE=1`` — escape hatch
    for diagnostic sessions where you want to inspect a stuck bot
    instead of having it auto-recover.
    """
    if _DISCORD_WATCHDOG_DISABLE:
        logger.info("DISCORD_GATEWAY_WATCHDOG: disabled via env")
        return
    logger.info(
        "DISCORD_GATEWAY_WATCHDOG_STARTED interval_s=%d "
        "latency_threshold_s=%.1f strikes_to_restart=%d",
        _DISCORD_WATCHDOG_INTERVAL_SEC,
        _DISCORD_WATCHDOG_LATENCY_THRESHOLD_SEC,
        _DISCORD_WATCHDOG_STRIKES_TO_RESTART,
    )
    while True:
        try:
            await asyncio.sleep(_DISCORD_WATCHDOG_INTERVAL_SEC)
            _watchdog_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "DISCORD_GATEWAY_WATCHDOG_LOOP_ERROR: %s",
                exc, exc_info=True,
            )


# ---------------------------------------------------------------------------
# Zombie-reaper background loop (ZOMBIE-CHILD-PROCESS-REAP-V1)
# ---------------------------------------------------------------------------

_ZOMBIE_REAPER_INTERVAL_SEC: int = int(
    os.getenv("KERNOS_ZOMBIE_REAPER_INTERVAL_SEC", "60")
)


async def _zombie_reaper_loop() -> None:
    """Periodically reap any zombie child processes.

    Belt-and-suspenders defense against subprocess-lifecycle leaks.
    The primary fix is per-spawn-site discipline (always await
    proc.wait() after proc.terminate()), but a periodic
    waitpid(-1, WNOHANG) loop catches anything that path misses
    without depending on every spawn site doing the right thing.

    Loop discipline:
      - sleep first so on_ready isn't blocked
      - reap in a tight inner while-loop until WNOHANG returns 0
        (handles bursts of multiple zombies cleanly)
      - log every reap so operational verification is easy
      - any exception in the loop is logged and the loop continues
        (don't let a transient OS hiccup kill the reaper)

    Interval tunable via ``KERNOS_ZOMBIE_REAPER_INTERVAL_SEC`` env
    (default 60s). Well below the observed leak rate (~18/day).
    """
    import asyncio
    while True:
        try:
            await asyncio.sleep(_ZOMBIE_REAPER_INTERVAL_SEC)
            reaped = 0
            # Loop because a burst of MCP reconnects can leave
            # multiple zombies queued; one waitpid call reaps one.
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    # No children at all (unusual but possible
                    # mid-startup before any subprocess spawn).
                    break
                if pid == 0:
                    # No more reapable children right now.
                    break
                reaped += 1
                logger.info(
                    "ZOMBIE_REAPED: pid=%d status=%d", pid, status,
                )
            if reaped:
                logger.info(
                    "ZOMBIE_REAPER_CYCLE: reaped=%d", reaped,
                )
        except Exception as exc:
            # Don't let any unexpected exception kill the reaper.
            # Log and continue.
            logger.warning(
                "ZOMBIE_REAPER_LOOP_ERROR: %s", exc, exc_info=True,
            )


@client.event
async def on_ready():
    global handler
    logger.info("Starting Kernos server")
    instance_id = os.getenv("KERNOS_INSTANCE_ID", "")
    if instance_id:
        logger.info("INSTANCE: id=%s (from KERNOS_INSTANCE_ID)", instance_id)
    else:
        logger.info("INSTANCE: id derived per-adapter (set KERNOS_INSTANCE_ID for cross-channel identity)")
    logger.info("Discord adapter connected as %s", client.user)

    data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
    events = JsonEventStream(data_dir)
    store_backend = os.getenv("KERNOS_STORE_BACKEND", "sqlite")
    if store_backend == "json":
        state = JsonStateStore(data_dir)
        logger.info("State backend: JSON files")
    else:
        from kernos.kernel.state_sqlite import SqliteStateStore
        state = SqliteStateStore(data_dir)
        logger.info("State backend: SQLite (WAL mode)")

    # Initialize instance database (shared across all instances)
    from kernos.kernel.instance_db import InstanceDB
    instance_db = InstanceDB(data_dir)
    try:
        await instance_db.connect()
        # Register the owner as a member
        _owner_discord = os.getenv("DISCORD_OWNER_ID", "")
        _instance_id = os.getenv("KERNOS_INSTANCE_ID", "")
        if _owner_discord and _instance_id:
            await instance_db.ensure_owner(
                member_id="",  # Ignored — ensure_owner finds or creates stable mem_ ID
                display_name="owner",
                instance_id=_instance_id,
                platform="discord",
                channel_id=_owner_discord,
            )
    except Exception as exc:
        logger.warning("Instance DB init failed (non-fatal): %s", exc)

    # EVENT-STREAM-TO-SQLITE: start the background writer. Fire-and-forget
    # emissions from six instrumented subsystems batch-flush to
    # data/instance.db every 2s or when the queue hits 100 events.
    try:
        from kernos.kernel import event_stream
        await event_stream.start_writer(data_dir)
        logger.info("EVENT_STREAM: writer started (data_dir=%s)", data_dir)
    except Exception as exc:
        logger.warning("EVENT_STREAM_START_FAILED: %s", exc)

    try:
        await emit_event(
            events, EventType.SYSTEM_STARTED, "system", "server", payload={}
        )
    except Exception as exc:
        logger.warning("Failed to emit system.started: %s", exc)

    # Post-update whisper: if the previous startup applied an auto-update,
    # a pending-marker + commit-range log sit in {data_dir}. Convert to a
    # queued Whisper carrying the substrate event for the first member
    # turn after restart. The agent reads the event alongside its
    # covenants (a default "tell me about updates" preference ships
    # with the instance) and decides what to surface in its own voice.
    # AUTO-UPDATE-INFORMING-V1.
    try:
        from kernos.setup.self_update import queue_pending_whisper
        if _instance_id:
            await queue_pending_whisper(
                state=state, instance_id=_instance_id, data_dir=data_dir,
            )
    except Exception as exc:
        logger.warning("AUTO_UPDATE_WHISPER_QUEUE_FAILED: %s", exc)

    mcp_manager = MCPClientManager(events=events)

    credentials_path = os.getenv("GOOGLE_OAUTH_CREDENTIALS_PATH", "")
    if credentials_path:
        mcp_manager.register_server(
            "google-calendar",
            StdioServerParameters(
                command="npx",
                args=["@cocal/google-calendar-mcp"],
                env={"GOOGLE_OAUTH_CREDENTIALS": credentials_path},
            ),
        )
        mcp_manager.register_auth_command(
            "google-calendar",
            AuthCommand(
                command="npx",
                args=["@cocal/google-calendar-mcp", "auth", "normal"],
                env={"GOOGLE_OAUTH_CREDENTIALS": credentials_path},
                probe_tool="get-current-time",
            ),
        )
    else:
        logger.warning(
            "GOOGLE_OAUTH_CREDENTIALS_PATH not set — calendar tools unavailable"
        )

    brave_api_key = os.getenv("BRAVE_API_KEY", "")
    if brave_api_key:
        mcp_manager.register_server(
            "brave-search",
            StdioServerParameters(
                command="npx",
                args=["-y", "@modelcontextprotocol/server-brave-search"],
                env={"BRAVE_API_KEY": brave_api_key},
            ),
        )
    else:
        logger.warning("BRAVE_API_KEY not set — web search tools unavailable")

    mcp_manager.register_server(
        "web-browser",
        StdioServerParameters(
            command=sys.executable,
            args=["-m", "kernos.browser"],
        ),
    )

    await mcp_manager.connect_all()

    conversations = JsonConversationStore(data_dir)
    tenants = JsonInstanceStore(data_dir)
    audit = JsonAuditStore(data_dir)

    # Build capability registry from known catalog, promote connected servers
    registry = CapabilityRegistry(mcp=mcp_manager)
    for cap in KNOWN_CAPABILITIES:
        registry.register(dataclasses.replace(cap))
    for server_name, tools in mcp_manager.get_tool_definitions().items():
        cap = registry.get(server_name) or registry.get_by_server_name(server_name)
        if cap:
            cap.status = CapabilityStatus.CONNECTED
            cap.tools = [t["name"] for t in tools]
    connected = [c.name for c in registry.get_connected()]
    logger.info("Capability registry ready — connected: %s", connected or "none")

    from kernos.providers.chains import build_chains_from_env
    chains, _primary_provider = build_chains_from_env()

    # IWL C5: shared trace sink — StepDispatcher writes here on the
    # new path; ReasoningService.drain_tool_trace() reads from this
    # same list. The handler owns the drain (drain-ordering invariant).
    reasoning_trace_sink: list[dict] = []
    # RESPONSE-FIDELITY-V1 Batch 1.3 hardening (2026-05-08): shared
    # ActionStateRecord sink — note_this (and Batch 2+ migrated
    # surfaces) append here via ReasoningService; the integration
    # runner peeks at finalize, the handler drains at turn end.
    reasoning_action_record_sink: list = []

    # IWL C5/C6: production wiring for the decoupled-cognition path.
    # Constructs a per-turn turn_runner_provider closure that:
    #   - builds a fresh AggregatedTelemetry per turn,
    #   - wraps the shared chain caller with telemetry per turn so
    #     token aggregation accumulates correctly,
    #   - constructs Planner / DivergenceReasoner / PresenceRenderer
    #     with the wrapped chain,
    #   - constructs StepDispatcher with shared trace sink + event +
    #     audit emitters + an on_dispatch_complete callback that
    #     increments the per-turn telemetry's tool_iterations,
    #   - constructs IntegrationService with the wrapped chain,
    #   - constructs EnactmentService with the four hooks,
    #   - constructs ProductionResponseDelivery bound to (request,
    #     telemetry, event_emitter),
    #   - returns a TurnRunner with PDI-shipped constructor shape.
    #
    # ReasoningService._run_via_turn_runner_provider invokes the
    # provider per turn; the synthetic reasoning.* events fire
    # exactly once per turn (no-double-count invariant).
    from kernos.kernel.cohorts import (
        CohortFanOutConfig,
        CohortFanOutRunner,
        CohortRegistry,
        register_covenant_cohort,
    )
    from kernos.kernel.enactment import (
        DivergenceReasoner,
        EnactmentService,
        Planner,
        PresenceRenderer,
        StaticToolCatalog,
        StepDispatcher,
        ToolExecutionResult,
    )
    from kernos.kernel.enactment.dispatcher import (
        ToolDescriptorLookup,
        ToolExecutor,
        ToolExecutionInputs,
    )
    from kernos.kernel.integration.service import IntegrationService
    from kernos.kernel.response_delivery import (
        AggregatedTelemetry,
        ProductionResponseDelivery,
        wrap_chain_caller_with_telemetry,
    )
    from kernos.kernel.turn_runner import TurnRunner

    # Cohort registration — v1 covenant only (see arch doc note).
    cohort_registry = CohortRegistry()
    try:
        register_covenant_cohort(cohort_registry, state)
    except Exception:
        logger.exception("IWL_COVENANT_COHORT_REGISTRATION_FAILED")

    async def _cohort_audit_emitter(entry: dict) -> None:
        """Bridge cohort fan-out audit entries into the existing
        audit store with the correct two-arg async signature."""
        try:
            if audit is None or not hasattr(audit, "log"):
                return
            instance_id = entry.get("instance_id", "") or ""
            await audit.log(instance_id, entry)
        except Exception:
            logger.exception("IWL_COHORT_AUDIT_EMIT_FAILED")

    cohort_runner = CohortFanOutRunner(
        registry=cohort_registry,
        audit_emitter=_cohort_audit_emitter,
        config=CohortFanOutConfig(),
    )

    # Hook chain callers — v1 same-model default (the design review edit, locked).
    # All hooks share this base chain caller; per-turn telemetry
    # wrapping happens in the provider closure below.
    primary_chain = chains.get("primary", [])

    async def _shared_chain_caller(
        system, messages, tools, max_tokens, *, conversation_id="",
    ):
        # ============================================================
        # WIRE-SHAPE PLUMBING SEAM — do NOT drop conversation_id.
        # ============================================================
        # conversation_id flows from briefing.turn_id → PresenceRenderer
        # → response_delivery._wrapped → here → provider.complete. It
        # populates the Codex provider's prompt_cache_key + session
        # correlation headers. Without it, the consumer backend's KV
        # cache misses on every turn and >40KB calls mid-stream-fail
        # with server_error. Pin tests:
        #   tests/test_thin_path_codex_wire_shape_plumbing.py
        # See kernos/providers/codex_provider.py class docstring
        # "WIRE SHAPE INVARIANTS" for the full contract.
        # If you're refactoring this seam, the conversation_id kwarg
        # MUST be accepted AND forwarded. Anthropic + Ollama providers
        # accept and ignore it — passing it never breaks them.
        if not primary_chain:
            raise RuntimeError(
                "primary chain not configured; new path requires a "
                "configured provider"
            )
        entry = primary_chain[0]
        return await entry.provider.complete(
            model=entry.model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            conversation_id=conversation_id,
        )

    # Design Review-lean (a) loud-failure surface for v1: until the
    # workshop-registry binding lands, full-machinery tool dispatch
    # raises STRUCTURALLY at the descriptor-lookup layer rather than
    # returning None (which would silently produce a graceful
    # "tool-not-registered" StepDispatchResult — indistinguishable
    # from a misconfigured tool catalog during soak). The loud
    # failure makes the deferred binding observable.
    class _UnwiredDescriptorLookup:
        """v1 placeholder for the workshop-registry binding. Raises
        loudly when consulted so soak operators see clearly that
        the binding is not yet wired (vs. a graceful tool-not-
        registered response that would mask the deferred work)."""

        def descriptor_for(self, tool_id):
            raise NotImplementedError(
                f"workshop tool descriptor lookup is not wired in v1. "
                f"tool={tool_id!r}. Thin-path turns succeed; "
                f"full-machinery dispatch awaits "
                f"INTEGRATION-WIRE-LIVE-WORKSHOP-BINDING follow-up."
            )

    class _UnwiredExecutor:
        async def execute(self, inputs: ToolExecutionInputs) -> ToolExecutionResult:
            raise RuntimeError(
                f"production tool executor not wired; tool={inputs.tool_id!r}. "
                f"INTEGRATION-WIRE-LIVE-WORKSHOP-BINDING follow-up."
            )

    # Tool catalog + executor + descriptor lookup are shared across
    # turns (the workshop binding will replace these with the real
    # surface). The hooks themselves are constructed PER TURN inside
    # the provider closure so their chain callers can be wrapped
    # with the per-turn telemetry.
    planner_tool_catalog = StaticToolCatalog()
    shared_executor = _UnwiredExecutor()
    shared_descriptor_lookup = _UnwiredDescriptorLookup()

    async def _integration_dispatcher(tool_id, args, inputs):
        return {}

    async def _integration_audit_emitter(entry: dict) -> None:
        """Bridge integration's audit entries into the existing audit
        store. AuditStore.log is async with signature
        (instance_id, entry) — threading instance_id from the entry's
        turn-context fields when present."""
        try:
            if audit is None or not hasattr(audit, "log"):
                return
            instance_id = entry.get("instance_id", "") or ""
            await audit.log(instance_id, entry)
        except Exception:
            logger.exception("IWL_INTEGRATION_AUDIT_EMIT_FAILED")

    async def _dispatcher_event_emitter(payload: dict) -> None:
        """Bridge dispatcher's tool.called / tool.result emissions
        into the existing event stream so legacy consumers see them
        with the right shape on the new path. Also logs at INFO so
        the in-process log ring buffer (used by /dump's RECENT LOG
        section) captures the events alongside the on-disk event
        stream. Without the log line, tool dispatch was invisible
        in /dump output even though it was firing correctly."""
        # /dump-visibility log line: terse, structured, parseable.
        # Mirrors the legacy CODEX_REQUEST line shape so operators
        # can scan the buffer for tool activity.
        try:
            _t = payload.get("type", "?")
            # Two emitters land here with different field names:
            # StepDispatcher uses tool_name, LiveIntegrationDispatcher
            # uses tool_id. Accept either so the log shows the real name.
            _tool = payload.get("tool_name") or payload.get("tool_id") or "?"
            _seam = payload.get("seam", "")
            _err = payload.get("is_error", False)
            if _t == "tool.called":
                logger.info(
                    "TOOL_CALLED: tool=%s seam=%s classification=%s",
                    _tool, _seam, payload.get("classification", ""),
                )
            else:
                logger.info(
                    "TOOL_RESULT: tool=%s seam=%s is_error=%s",
                    _tool, _seam, _err,
                )
        except Exception:
            pass
        if events is None:
            return
        try:
            from kernos.kernel.events import emit_event
            from kernos.kernel.event_types import EventType
            event_type = (
                EventType.TOOL_CALLED
                if payload.get("type") == "tool.called"
                else EventType.TOOL_RESULT
            )
            await emit_event(
                events,
                event_type,
                payload.get("instance_id", ""),
                "step_dispatcher",
                payload=payload,
            )
        except Exception:
            logger.warning(
                "DISPATCHER_EVENT_EMIT_FAILED type=%s",
                payload.get("type", "?"),
            )

    async def _dispatcher_audit_emitter(entry: dict) -> None:
        """Bridge dispatcher's audit entries into the existing audit
        store. AuditStore.log is async with signature
        (instance_id, entry); references-not-dumps already enforced
        at the entry construction site. Also logs at INFO for /dump
        ring-buffer visibility."""
        try:
            _t = entry.get("type", "?")
            _tool = entry.get("tool_id", "?")
            logger.info("DISPATCHER_AUDIT: type=%s tool=%s", _t, _tool)
        except Exception:
            pass
        try:
            if audit is None or not hasattr(audit, "log"):
                return
            instance_id = entry.get("instance_id", "") or ""
            await audit.log(instance_id, entry)
        except Exception:
            logger.exception("DISPATCHER_AUDIT_EMIT_FAILED")

    # REASONING-SERVICE-CONSTRUCTION-PARITY-V1: the per-turn factory
    # closure is now built by the shared helper. Each callsite that
    # constructs ReasoningService populates a ThinPathContext, passes
    # the closure to ReasoningService(turn_runner_provider=...), then
    # calls wire_live_thin_path() after handler construction. The
    # context's late-binding semantics preserve the rebinding pattern
    # the inline closure relied on: live components assigned post-
    # handler propagate to subsequent per-turn invocations.
    from kernos.kernel.turn_runner_provider import (
        ThinPathContext,
        build_turn_runner_provider,
    )

    _thin_path_ctx = ThinPathContext(
        chains=chains,
        chain_caller=_shared_chain_caller,
        cohort_runner=cohort_runner,
        dispatcher_event_emitter=_dispatcher_event_emitter,
        dispatcher_audit_emitter=_dispatcher_audit_emitter,
        integration_audit_emitter=_integration_audit_emitter,
        trace_sink=reasoning_trace_sink,
        action_record_sink=reasoning_action_record_sink,
        executor=shared_executor,
        descriptor_lookup=shared_descriptor_lookup,
        integration_dispatcher=_integration_dispatcher,
        planner_tool_catalog=planner_tool_catalog,
    )

    reasoning = ReasoningService(
        events=events,
        mcp=mcp_manager,
        audit=audit,
        chains=chains,
        trace_sink=reasoning_trace_sink,
        action_record_sink=reasoning_action_record_sink,
        turn_runner_provider=build_turn_runner_provider(_thin_path_ctx),
    )
    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(mcp_manager, conversations, tenants, audit, events, state, reasoning, registry, engine, secrets_dir=os.getenv("KERNOS_SECRETS_DIR", "./secrets"))
    handler._instance_db = instance_db  # Wire instance DB for member resolution
    handler.register_mcp_tools_in_catalog()

    # ====================================================================
    # INTEGRATION-CAPABILITY-FIRST-V1 Batch 2 — live workshop binding
    # ====================================================================
    # The C5c-bringup cutover stubbed the workshop binding seams with
    # _UnwiredDescriptorLookup / _UnwiredExecutor / empty
    # _integration_dispatcher / empty StaticToolCatalog so the thin
    # path could ship without full-machinery dispatch yet. Batch 2
    # replaces those stubs with production-wired versions reading
    # from the live tool catalog and routing through reasoning's
    # execute_tool. Late-bind into the closure: the per-turn factory
    # reads `shared_executor` etc. from the enclosing scope at call
    # time, so rebinding here picks up correctly when turns fire.
    #
    # Per the design review's Fold 3 ("Gate at dispatch, hint at
    # surfacing"): every live dispatch path classifies with the
    # actual call arguments before executing — surfacing-time hints
    # are not authoritative. See kernos/kernel/integration/live_wiring.py
    # for the canonical implementations.
    # REASONING-SERVICE-CONSTRUCTION-PARITY-V1: live thin-path wiring
    # is now driven by the shared helper. Mutates _thin_path_ctx with
    # production LiveExecutor / LiveDescriptorLookup /
    # LiveIntegrationDispatcher / LivePlannerCatalog. The closure
    # built earlier reads ctx fields per turn, so subsequent turns
    # pick up the live components automatically.
    from kernos.kernel.turn_runner_provider import wire_live_thin_path
    wire_live_thin_path(
        _thin_path_ctx,
        reasoning=reasoning,
        handler=handler,
    )

    logger.info("MessageHandler ready (data_dir=%s)", data_dir)

    # WTC v1 C5c-bringup: instantiate the WLP / runtime / STS substrate
    # so it actually runs in production rather than only existing as
    # shipped-but-unwired code. Failure is fail-loud-but-non-blocking:
    # the legacy Pattern 05 path stays active even if substrate
    # bring-up errors, so a startup regression in the new substrate
    # doesn't take down the bot.
    try:
        from kernos.kernel.agents.registry import AgentRegistry
        # AgentRegistry is constructed lazily by handler today; surface
        # it explicitly for the substrate's STS facade. If handler
        # already has one, reuse; otherwise construct fresh.
        _agent_registry = getattr(handler, "_agent_registry", None)
        if _agent_registry is None:
            from kernos.kernel.agents.providers import (
                ProviderRegistry as DARProviderRegistry,
            )
            _dar_pr = DARProviderRegistry()
            _agent_registry = AgentRegistry(provider_registry=_dar_pr)
            await _agent_registry.start(data_dir)
            handler._agent_registry = _agent_registry
        from kernos.setup.bring_up_substrate import bring_up_substrate
        _substrate = await bring_up_substrate(
            data_dir=data_dir,
            handler=handler,
            agent_registry=_agent_registry,
        )
        handler._wlp_substrate = _substrate
        handler._wlp_runtime = _substrate.runtime
        logger.info(
            "WTC_C5C_BRINGUP_OK: substrate live (runtime=%s, "
            "engine started, %d action verbs registered, "
            "STS facade ready)",
            _substrate.runtime.claim_owner,
            len(_substrate.action_library._verbs),
        )
    except Exception as exc:
        logger.warning(
            "WTC_C5C_BRINGUP_FAILED: %s — legacy Pattern 05 path "
            "remains authoritative; bot continues without unified "
            "runtime. This is non-blocking but should be investigated.",
            exc,
        )

    # SYSTEM-REFERENCE-CANVAS-SEED: idempotent first-boot seeding of
    # System Reference + Our Procedures canvases. Safe to call on every
    # boot; skips canvases that already exist. Per-member My Tools seed
    # runs at bootstrap-graduation time, not here.
    try:
        _seed_instance_id = os.getenv("KERNOS_INSTANCE_ID", "")
        if _seed_instance_id:
            from kernos.setup.seed_canvases import seed_canvases_on_first_boot
            from kernos.kernel.scheduler import resolve_owner_member_id
            _canvas_svc = handler._get_canvas_service()
            if _canvas_svc is not None:
                _seed_result = await seed_canvases_on_first_boot(
                    _seed_instance_id,
                    canvas_service=_canvas_svc,
                    instance_db=instance_db,
                    operator_member_id=resolve_owner_member_id(_seed_instance_id),
                    tool_catalog=handler._tool_catalog,
                )
                logger.info(
                    "CANVAS_SEED_BOOT: instance=%s seeded=%s skipped=%s pages=%d warnings=%d",
                    _seed_instance_id, _seed_result.seeded_canvases,
                    _seed_result.skipped_canvases, _seed_result.pages_written,
                    len(_seed_result.warnings),
                )
                for _w in _seed_result.warnings:
                    logger.warning("CANVAS_SEED_WARNING: %s", _w)
        else:
            logger.info(
                "CANVAS_SEED_BOOT: KERNOS_INSTANCE_ID unset — seeding deferred "
                "to per-adapter instance resolution (not implemented in v1)."
            )
    except Exception as exc:
        logger.warning("CANVAS_SEED_BOOT_FAILED: %s", exc)

    # Register adapters and channels for outbound messaging
    adapter.set_client(client)
    handler.register_adapter("discord", adapter)
    handler.register_channel(
        name="discord", display_name="Discord", platform="discord",
        can_send_outbound=True, channel_target="",  # Updated per-message
    )
    # Persist Discord bot identity for invite instructions
    if client.user:
        await instance_db.set_platform_config("discord", {
            "bot_name": client.user.display_name or str(client.user),
            "bot_id": str(client.user.id),
        })

    # Register SMS channel if Twilio credentials are configured
    twilio_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_phone = os.getenv("TWILIO_PHONE_NUMBER", "")
    if twilio_sid and twilio_token and twilio_phone:
        from kernos.messages.adapters.twilio_sms import TwilioSMSAdapter
        sms_adapter = TwilioSMSAdapter()
        handler.register_adapter("sms", sms_adapter)
        owner_phone = os.getenv("OWNER_PHONE_NUMBER", "")
        handler.register_channel(
            name="sms", display_name="Twilio SMS", platform="sms",
            can_send_outbound=True, channel_target=owner_phone,
        )

        # Start SMS polling for inbound messages (no webhook needed)
        from kernos.sms_poller import SMSPoller
        sms_poller = SMSPoller(
            adapter=sms_adapter, handler=handler,
            account_sid=twilio_sid, auth_token=twilio_token,
            twilio_number=twilio_phone,
            interval=float(os.getenv("KERNOS_SMS_POLL_INTERVAL", "30")),
        )
        await sms_poller.start()
        # Persist SMS identity for invite instructions
        await instance_db.set_platform_config("sms", {"phone_number": twilio_phone})
        logger.info(
            "SMS channel registered — polling interval=%ss, outbound to %s",
            sms_poller._interval, owner_phone,
        )

    # Register Telegram channel if bot token is configured
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if telegram_token:
        from kernos.messages.adapters.telegram_bot import TelegramAdapter
        tg_adapter = TelegramAdapter()
        handler.register_adapter("telegram", tg_adapter)
        handler.register_channel(
            name="telegram", display_name="Telegram", platform="telegram",
            can_send_outbound=True, channel_target="",
        )
        from kernos.telegram_poller import TelegramPoller
        tg_poller = TelegramPoller(
            adapter=tg_adapter, handler=handler,
            bot_token=telegram_token,
        )
        # Discover and persist Telegram bot identity for invite instructions
        tg_identity = await tg_poller.discover_identity()
        if tg_identity:
            await instance_db.set_platform_config("telegram", tg_identity)
        await tg_poller.start()
        logger.info("Telegram channel registered — long polling active")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram adapter unavailable")

    # CLI is always registered but can't push
    handler.register_channel(
        name="cli", display_name="CLI Terminal", platform="cli",
        can_send_outbound=False,
    )

    # AUTO-UPDATE-BEHAVIOR-V1: launch the daily scheduled background
    # pull. Pulls origin/{branch} at KERNOS_AUTO_UPDATE_TIME local
    # time; new code applies on the next natural restart, NOT mid-
    # flight. No-op when KERNOS_AUTO_UPDATE=off.
    #
    # AUTO-UPDATE-INFORMING-V1: the verbose-mode ephemeral path is
    # gone. The post-update whisper above carries the substrate event
    # to the agent's situation context, where the agent's covenants
    # (including a default "tell me about updates" preference) decide
    # whether and how to surface in the agent's own voice.
    try:
        import asyncio as _au_asyncio
        from kernos.setup.self_update import scheduled_update_loop
        _au_asyncio.create_task(scheduled_update_loop(data_dir=data_dir))
    except Exception as exc:
        logger.warning("AUTO_UPDATE_CRON_LAUNCH_FAILED: %s", exc)

    # ZOMBIE-CHILD-PROCESS-REAP-V1 (2026-05-17): belt-and-suspenders
    # SIGCHLD reaper. MCP server reconnect cycles + auth-flow timeouts
    # were leaving ~18 defunct child processes per day attached to the
    # bot (observed: 36 zombies on a 2-day-old process). Per-spawn-
    # site cleanup discipline is the primary fix (see capability/
    # client.py:412 + the AsyncExitStack pattern), but a periodic
    # waitpid(WNOHANG) loop catches anything that path misses without
    # depending on every spawn site doing the right thing.
    #
    # 60-second interval is well below the leak rate; each call costs
    # one syscall + at most one process-table walk. Fail-safe: any
    # exception in the loop is logged and the loop continues.
    try:
        _au_asyncio.create_task(_zombie_reaper_loop())
    except Exception as exc:
        logger.warning("ZOMBIE_REAPER_LAUNCH_FAILED: %s", exc)

    # DISCORD-GATEWAY-WATCHDOG-V1 (2026-05-19): observe gateway
    # heartbeat health and force-restart when broken. See the
    # watchdog block above for full rationale; bug shape was a
    # silent gateway death that left the bot online but deaf
    # for 20+ min until manual /restart.
    _mark_inbound_event()  # bootstrap timestamp
    try:
        _au_asyncio.create_task(_discord_gateway_watchdog_loop())
    except Exception as exc:
        logger.warning("DISCORD_GATEWAY_WATCHDOG_LAUNCH_FAILED: %s", exc)

    # ACPX-INTEGRATION-V1 (2026-05-18): probe + launch the bridge
    # watchers. Outbound watcher closes the ask_coding_session
    # operator-relay gap (Kernos's tool surface dispatching out to
    # CC/Codex/Gemini through ACPX). Inbound watcher gives external
    # CLI clients (CC sessions, Codex via acpx, scripts) a read-only
    # channel to ask Kernos to introspect itself.
    #
    # The ACPX probe is informational, not blocking — if ACPX is
    # missing, the watchers still launch but each outbound dispatch
    # writes an unable_to_investigate response with a clear pointer
    # to the install command. Fail-loud, fail-recoverable.
    try:
        from kernos.kernel.external_agents.acpx_adapter import (
            EXPECTED_ACPX_VERSION as _ACPX_EXPECTED,
            is_acpx_available as _acpx_check,
        )
        _acpx_ok, _acpx_detail = _acpx_check()
        if _acpx_ok:
            logger.info(
                "AGENT_PROTOCOL_AVAILABLE: acpx=%s (expected=%s)",
                _acpx_detail, _ACPX_EXPECTED,
            )
        else:
            _auto = os.getenv("KERNOS_ACPX_AUTO_INSTALL", "").strip().lower()
            if _auto in ("1", "true", "on", "yes"):
                logger.warning(
                    "AGENT_PROTOCOL_MISSING: %s — attempting auto-install "
                    "(KERNOS_ACPX_AUTO_INSTALL set)", _acpx_detail,
                )
                import subprocess as _subp
                try:
                    _subp.run(
                        ["npm", "install", "-g",
                         f"acpx@{_ACPX_EXPECTED}"],
                        check=True, capture_output=True, timeout=120,
                    )
                    logger.info(
                        "AGENT_PROTOCOL_AUTO_INSTALLED: acpx@%s",
                        _ACPX_EXPECTED,
                    )
                except Exception as _ie:
                    logger.warning(
                        "AGENT_PROTOCOL_AUTO_INSTALL_FAILED: %s — "
                        "broker dispatch will return "
                        "unable_to_investigate until acpx is "
                        "installed manually: `npm install -g acpx@%s`",
                        _ie, _ACPX_EXPECTED,
                    )
            else:
                logger.warning(
                    "AGENT_PROTOCOL_MISSING: %s — install via "
                    "`npm install -g acpx@%s` or set "
                    "KERNOS_ACPX_AUTO_INSTALL=1 for auto-install at "
                    "next startup. Broker dispatch will return "
                    "unable_to_investigate until acpx is installed.",
                    _acpx_detail, _ACPX_EXPECTED,
                )
        # Launch the watchers regardless — they handle the no-ACPX
        # case gracefully (per-request unable_to_investigate response).
        from kernos.kernel.external_agents.bridge_watcher import (
            inbound_watcher_loop as _inbound_watcher,
            outbound_watcher_loop as _outbound_watcher,
        )
        _watcher_instance_id = (
            os.getenv("KERNOS_INSTANCE_ID", "") or "default"
        )
        _au_asyncio.create_task(_outbound_watcher(
            data_dir=data_dir,
            instance_id=_watcher_instance_id,
        ))
        _au_asyncio.create_task(_inbound_watcher(
            data_dir=data_dir,
            instance_id=_watcher_instance_id,
        ))
    except Exception as exc:
        logger.warning("BRIDGE_WATCHER_LAUNCH_FAILED: %s", exc)

    # Send pending confirmation from a prior /restart or /wipe
    if _PENDING_CONFIRMATION_PATH.is_file():
        try:
            pending = json.loads(_PENDING_CONFIRMATION_PATH.read_text())
            channel = await client.fetch_channel(pending["channel_id"])

            # Delete the pre-restart placeholder (⏳)
            _del_id = pending.get("delete_message_id")
            if _del_id:
                try:
                    old_msg = await channel.fetch_message(int(_del_id))
                    await old_msg.delete()
                except Exception:
                    pass

            # Send "Ready." and auto-delete after 5 seconds
            conf_msg = await channel.send(pending["message"])
            logger.info("Sent pending confirmation to channel %s", pending["channel_id"])
            _PENDING_CONFIRMATION_PATH.unlink()

            async def _delete_after(msg, delay=5):
                import asyncio as _aio
                await _aio.sleep(delay)
                try:
                    await msg.delete()
                except Exception:
                    pass
            import asyncio as _aio
            _aio.create_task(_delete_after(conf_msg))
        except Exception as exc:
            logger.warning("Failed to send pending confirmation: %s", exc)

    # AwarenessEvaluator starts lazily per-instance on first message
    # (handler._maybe_start_evaluator). No startup guessing needed.

    # Recover any plans interrupted by crash/restart
    try:
        await handler.recover_active_plans()
    except Exception as exc:
        logger.warning("Failed to recover active plans: %s", exc)

    # DISCORD-429-SMART-BACKOFF (2026-05-08, hardening): tree.sync()
    # POSTs to /applications/{id}/commands which can 429 under
    # Cloudflare-flagged tokens or 503 during Discord-side
    # unavailability. Either failure used to be logged by discord.py's
    # event handler ("Ignoring exception in on_ready") but the slash
    # commands ended up unsynced for the session. Wrap so we log
    # specifically + retry on 503; slash-command sync isn't blocking
    # for normal message flow, so partial success is acceptable.
    try:
        await tree.sync()
        logger.info("Slash commands synced")
    except discord.HTTPException as exc:
        if exc.status == 429:
            code = getattr(exc, "code", None)
            label = (
                "Cloudflare-level / abuse flag (40062)"
                if code == 40062
                else f"code={code}"
            )
            logger.warning(
                "DISCORD_TREE_SYNC_RATE_LIMITED: %s — slash commands "
                "not synced for this session. Existing slash commands "
                "from prior sessions still work; only NEW or CHANGED "
                "command definitions won't be visible until the next "
                "successful sync.",
                label,
            )
        elif exc.status >= 500:
            logger.warning(
                "DISCORD_TREE_SYNC_SERVER_ERROR: status=%d — Discord "
                "API transient unavailability. Slash commands not "
                "synced this session; retry on next boot.",
                exc.status,
            )
        else:
            logger.warning(
                "DISCORD_TREE_SYNC_FAILED: status=%d code=%s text=%s",
                exc.status, getattr(exc, "code", "?"), exc,
            )





DISCORD_MAX_LENGTH = 2000

# DISCORD-INTERCHUNK-DELAY (2026-05-18): pause between consecutive
# chunks of a single multi-chunk response. Discord's per-route rate
# limit is ~5 messages / 5 seconds per channel; sending 3+ chunks
# back-to-back (the "shotgun" pattern) reliably triggers 429s,
# which then activate the cool-off and produce the silent-
# truncation UX gap. A 1.2s pause between chunks keeps a 4-chunk
# response under the limit + leaves headroom for the typing-
# indicator + other concurrent sends. Env-tunable for operators
# who want to tighten or loosen. Set to 0 to disable.
DISCORD_INTERCHUNK_DELAY_SEC: float = float(
    os.getenv("KERNOS_DISCORD_INTERCHUNK_DELAY_SEC", "1.2")
)


def _chunk_response(text: str) -> list[str]:
    """Split text into chunks that fit Discord's 2000-char limit.

    Collapses triple+ newlines to double (prevents excessive spacing in Discord).
    Splits on newlines where possible; falls back to hard cuts.
    """
    import re
    text = re.sub(r'\n{3,}', '\n\n', text)
    if len(text) <= DISCORD_MAX_LENGTH:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= DISCORD_MAX_LENGTH:
            chunks.append(text)
            break

        # Find the last newline within the limit
        cut = text.rfind("\n", 0, DISCORD_MAX_LENGTH)
        if cut <= 0:
            # No newline found — hard cut
            cut = DISCORD_MAX_LENGTH

        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")

    return chunks


_TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".json", ".csv", ".yaml", ".yml",
    ".toml", ".html", ".css", ".js", ".ts", ".sh", ".xml",
}


# Guard against duplicate Discord gateway deliveries.
# Discord can re-deliver events on gateway reconnect or missed ACKs.
_seen_message_ids: set[int] = set()
_SEEN_MAX = 200


# ---------------------------------------------------------------------------
# Per-call Discord 429 graceful-degradation helpers
# ---------------------------------------------------------------------------
#
# DISCORD-429-SMART-BACKOFF, post-shipping hardening (2026-05-08):
# the smart-backoff wrapper around client.run handles AUTH-side 429s,
# but Cloudflare-flagged tokens 429 on EVERY API call — including
# typing indicators and message sends. discord.py's internal retry
# loop hammers the endpoint 5 times at the Retry-After cadence
# (typically 3s) then raises HTTPException.
#
# Two layers of defense:
#
# (1) Per-call helpers (_begin_typing_safely, _send_safely) catch
#     HTTPException(429) escaping discord.py's internal retry and
#     degrade gracefully — typing is cosmetic, send returns False.
#
# (2) Global cool-off (_discord_pause_until) prevents COMPOUNDING:
#     once a 40062 is observed, ALL subsequent typing/send/reaction
#     calls in the bot skip immediately for an escalating duration.
#     This stops discord.py's internal 5x retry from firing on every
#     subsequent user message — that retry loop is what was extending
#     the abuse flag (~10 hits per inbound user message before the
#     cool-off shipped).
#
# Cool-off escalates per consecutive 429-streak:
#   1st 429 in session: 5 min pause
#   2nd: 30 min
#   3rd: 2 hours
#   4th+: 6 hours (stays here)
# A successful API call resets the streak to 0.
#
# Substrate (memory, conv-log, LLM calls) keeps working during pause —
# only Discord delivery is suppressed.

import time as _time_module

_DISCORD_PAUSE_SCHEDULE_SEC: list[int] = [
    # DISCORD-COOL-OFF-SOFTENING (2026-05-17): softened the escalation
    # schedule. Prior shape was 5/30/120/360 min, which meant a single
    # 429 hiccup → 5 min, but two in a session → 30 min (a long UX
    # blackhole). New shape: 5/10/30/120 — second-strike is 10 min
    # not 30, third-strike is 30 not 120, ceiling stays at 2hr. Single
    # hiccups stay short; sustained 429-streaks still escalate.
    int(os.getenv("KERNOS_DISCORD_PAUSE_1_SEC", "300")),    # 5 minutes
    int(os.getenv("KERNOS_DISCORD_PAUSE_2_SEC", "600")),    # 10 minutes (was 30)
    int(os.getenv("KERNOS_DISCORD_PAUSE_3_SEC", "1800")),   # 30 minutes (was 120)
    int(os.getenv("KERNOS_DISCORD_PAUSE_4_SEC", "7200")),   # 2 hours (was 6)
]

# Module-level cool-off state. _discord_pause_until is a Unix timestamp
# (0 = not paused). _discord_429_streak counts consecutive 429
# observations to escalate the schedule.
_discord_pause_until: float = 0.0
_discord_429_streak: int = 0


def _is_discord_paused() -> bool:
    """True when the bot should skip all Discord API calls."""
    return _time_module.time() < _discord_pause_until


def _seconds_until_resume() -> int:
    return max(0, int(_discord_pause_until - _time_module.time()))


def _register_discord_429(reason: str) -> None:
    """Activate the global cool-off after observing a 429.

    Escalates duration by streak count. Logs ONE LINE to console
    (per founder feedback 2026-05-08); the verbose multi-line block
    was noise once the mechanism was understood.

    The Discord-side notification (one-line message to the active
    channel) is best-effort and fired by the on_message helper that
    has the channel reference; this function only handles the state
    + logging.
    """
    from datetime import datetime, timedelta
    global _discord_pause_until, _discord_429_streak

    idx = min(_discord_429_streak, len(_DISCORD_PAUSE_SCHEDULE_SEC) - 1)
    duration = _DISCORD_PAUSE_SCHEDULE_SEC[idx]
    _discord_pause_until = _time_module.time() + duration
    _discord_429_streak += 1

    human = _format_429_wait_duration(duration)
    retry_at = datetime.now() + timedelta(seconds=duration)
    logger.warning(
        "DISCORD_COOL_OFF: paused %s (streak=%d, resume %s) — %s",
        human, _discord_429_streak,
        retry_at.strftime("%H:%M:%S"), reason,
    )


def _format_pause_user_notice() -> str:
    """One-line message surfaced to the active Discord channel when
    the cool-off activates. Tells the user the bot is rate-limited,
    when it'll be back, and that their input was processed (so they
    don't re-send and compound the issue)."""
    human = _format_429_wait_duration(_seconds_until_resume())
    return (
        f"⚠️ Discord is rate-limiting me — pausing replies for "
        f"~{human}. Your message was received and processed; "
        f"only the reply delivery is delayed."
    )


def _register_discord_call_succeeded() -> None:
    """Reset the 429 streak after a successful Discord API call."""
    global _discord_429_streak
    if _discord_429_streak > 0:
        logger.info(
            "DISCORD_PAUSE_STREAK_RESET: %d → 0 (successful call)",
            _discord_429_streak,
        )
        _discord_429_streak = 0


async def _begin_typing_safely(channel) -> Any:
    """Open a typing indicator best-effort.

    Returns the entered context manager on success; None when
    rate-limited (cool-off active or 429 observed). Other exceptions
    re-raise.

    Catches both ``discord.HTTPException`` (status=429 escaping
    discord.py's retry loop) and ``discord.RateLimited`` (raised
    immediately when retry_after exceeds max_ratelimit_timeout —
    the short-circuit that prevents the 5x compounding burst).
    """
    if _is_discord_paused():
        return None
    try:
        ctx = channel.typing()
        await ctx.__aenter__()
        _register_discord_call_succeeded()
        return ctx
    except discord.RateLimited:
        _register_discord_429("typing 429")
        return None
    except discord.HTTPException as exc:
        if exc.status == 429:
            _register_discord_429("typing 429")
            return None
        raise


async def _send_safely(channel, content: str) -> bool:
    """Send a message with 429 graceful degradation.

    Returns True on success, False on rate-limit failure. Catches
    both HTTPException(429) and RateLimited (the short-circuit
    raised by discord.py when retry_after exceeds the configured
    max_ratelimit_timeout, preventing the 5x retry compound).

    On rate limit, response content is NOT logged in full — keeps
    the console one-liner. Conv-log on disk already has it.
    """
    if _is_discord_paused():
        return False
    try:
        await channel.send(content)
        _register_discord_call_succeeded()
        return True
    except discord.RateLimited:
        _register_discord_429("send 429")
        return False
    except discord.HTTPException as exc:
        if exc.status == 429:
            _register_discord_429("send 429")
            return False
        raise


async def _send_pause_notice_to_channel(channel) -> None:
    """Best-effort: send the one-line cool-off notice to the user's
    Discord channel. If the send itself 429s (likely, since we're
    rate-limited), just shrug — the console log already captured it.

    Called once per cool-off activation by on_message. The streak
    counter prevents the notice from spamming on repeated turns
    during the same cool-off period (we only attempt when streak
    just incremented past 0)."""
    notice = _format_pause_user_notice()
    try:
        await channel.send(notice)
    except (discord.RateLimited, discord.HTTPException):
        # Already rate-limited; don't spam retries. The console log
        # has the cool-off duration; the user will figure it out from
        # the bot going silent.
        pass


# GATEWAY-HEALTH-OBSERVER-V1 (2026-05-19): MESSAGE_CREATE counter
# shared between the on_socket_event_type handler below and the
# GatewayHealthObserver. Bounded deque, window-filtered at read
# time. The observer reads this to detect the "Discord sent
# MESSAGE_CREATE but on_message didn't fire" pattern (Codex's
# 2026-05-19 diagnostic split).
from kernos.kernel.gateway_health import _MessageCreateCounter as _MCC
_message_create_counter = _MCC(
    window_sec=int(os.getenv("KERNOS_GATEWAY_DEAF_WINDOW_SEC", "600")),
)


@client.event
async def on_socket_event_type(event_type: str):
    """Fired by discord.py BEFORE parser dispatch. We count
    MESSAGE_CREATE events here as a gateway-level ground truth:
    if these fire but on_message doesn't, the parser/dispatcher
    layer is broken (Codex 2026-05-19 RCA)."""
    if event_type == "MESSAGE_CREATE":
        _message_create_counter.record(_time_module.time())


@client.event
async def on_disconnect():
    """Discord gateway WebSocket disconnected. Logs so silent
    gateway death is visible in events; discord.py's reconnect
    machinery handles the actual reconnect."""
    logger.warning("DISCORD_GATEWAY_DISCONNECT")


@client.event
async def on_resumed():
    """Discord gateway session resumed (after a transient drop).
    Treat as inbound activity so the watchdog's idle counter
    resets — the gateway is demonstrably alive."""
    logger.info("DISCORD_GATEWAY_RESUMED")
    _mark_inbound_event()


@client.event
async def on_message(message):
    # Watchdog: any incoming message proves the gateway is routing.
    # Bump the timestamp regardless of whether we'll respond — even
    # bot-own messages and other-bot messages indicate gateway health.
    _mark_inbound_event()

    # Deduplicate gateway re-deliveries
    if message.id in _seen_message_ids:
        return
    _seen_message_ids.add(message.id)
    if len(_seen_message_ids) > _SEEN_MAX:
        # Discard oldest half to bound memory
        to_remove = sorted(_seen_message_ids)[:_SEEN_MAX // 2]
        _seen_message_ids.difference_update(to_remove)

    # Don't respond to ourselves
    if message.author == client.user:
        return
    # Don't respond to other bots
    if message.author.bot:
        return

    if handler is None:
        await message.channel.send("Still starting up — try again in a moment.")
        return

    normalized = adapter.inbound(message)

    # Process Discord attachments: download text files into context for the handler
    if message.attachments:
        text_attachments = []
        binary_rejections = []
        for att in message.attachments:
            ext = Path(att.filename).suffix.lower()
            if ext in _TEXT_EXTENSIONS:
                try:
                    raw = await att.read()
                    content = raw.decode("utf-8")
                    text_attachments.append({"filename": att.filename, "content": content})
                except Exception as exc:
                    logger.warning("Failed to read attachment %s: %s", att.filename, exc)
                    binary_rejections.append(att.filename)
            else:
                binary_rejections.append(att.filename)

        if text_attachments:
            if normalized.context is None:
                normalized.context = {}
            normalized.context["attachments"] = text_attachments

        if binary_rejections:
            rejection_note = (
                "I can only handle text files right now — "
                f"{', '.join(binary_rejections)} cannot be processed (binary or unreadable)."
            )
            await message.channel.send(rejection_note)
            # Pass rejection info to handler so agent knows it can't reference these files
            if normalized.context is None:
                normalized.context = {}
            normalized.context["rejected_files"] = binary_rejections
            if not text_attachments and not message.content:
                return

    # Typing animation during processing — cosmetic. DISCORD-429-SMART-
    # BACKOFF (2026-05-08, hardened post-shipping after kernos-main field
    # session): if the token is Cloudflare-flagged (error 40062), the
    # typing endpoint 429s immediately. The previous shape wrapped
    # handler.process inside the typing context manager, so a typing
    # failure killed the entire turn before reasoning ran. Now: open
    # the typing indicator best-effort; on failure, log + proceed
    # without it. handler.process always runs.
    streak_before = _discord_429_streak
    typing_ctx = await _begin_typing_safely(message.channel)
    # If typing just activated the cool-off (streak incremented from
    # 0 to 1), surface a one-line notice to the user's channel so they
    # know the bot is paused. Only on the FIRST activation per streak
    # — subsequent messages during the same cool-off go silent (no
    # spam). Best-effort send; if it 429s too, console log already
    # captured the state.
    if streak_before == 0 and _discord_429_streak >= 1:
        await _send_pause_notice_to_channel(message.channel)
    try:
        try:
            response_text = await handler.process(normalized)
        except Exception as exc:
            logger.error("Handler error: %s", exc, exc_info=True)
            await _send_safely(
                message.channel,
                "Something went wrong — try again in a moment.",
            )
            try:
                await message.add_reaction("⚠️")
            except Exception:
                pass
            return
    finally:
        if typing_ctx is not None:
            try:
                await typing_ctx.__aexit__(None, None, None)
            except Exception:
                pass

    if not response_text:  # Merged message — response comes from primary turn
        return

    _all_chunks = _chunk_response(response_text)
    _total_chunks = len(_all_chunks)
    _delivered = 0
    _chunk_streak_before = _discord_429_streak
    import asyncio as _chunk_asyncio
    for _chunk_idx, chunk in enumerate(_all_chunks):
        # DISCORD-INTERCHUNK-DELAY: pause between chunks (not before
        # the first, not after the last) so consecutive sends don't
        # blow through Discord's per-route rate limit. Skipping when
        # only one chunk exists keeps single-message latency at
        # baseline.
        if _chunk_idx > 0 and DISCORD_INTERCHUNK_DELAY_SEC > 0:
            await _chunk_asyncio.sleep(DISCORD_INTERCHUNK_DELAY_SEC)
        sent = await _send_safely(message.channel, chunk)
        if not sent:
            # Could not deliver to Discord (e.g., Cloudflare-flagged token
            # or 429 cool-off). Response was persisted to conv-log
            # earlier in the persist phase, so the substrate state is
            # intact — only the user-visible delivery failed. Stop
            # chunking; further sends will also fail.
            #
            # DISCORD-CHUNK-TRUNCATION-INDICATOR (refined 2026-05-17):
            # handle three cases:
            #   (a) cool-off was already active before this send AND no
            #       chunks delivered → user has no signal at all. Notice
            #       send would also be paused; rely on the pause-notice
            #       fired at on_message::typing_ctx entry instead.
            #   (b) cool-off just ACTIVATED on a chunk send (streak
            #       incremented from 0→1 mid-loop) AND no chunks
            #       delivered → mirror the typing-indicator path's
            #       pause-notice so the user knows BEFORE the silence
            #       starts. _send_pause_notice_to_channel is best-
            #       effort (handles its own 429 fallback).
            #   (c) some chunks delivered, then 429 mid-message → the
            #       original truncation-indicator path: try one final
            #       short indicator send so the user sees that more
            #       was supposed to come.
            if (
                _chunk_streak_before == 0
                and _discord_429_streak >= 1
                and _delivered == 0
            ):
                # case (b): chunk send just triggered the cool-off
                await _send_pause_notice_to_channel(message.channel)
            elif _delivered > 0:
                # case (c): partial delivery, drop indicator
                _remaining = _total_chunks - _delivered
                if _remaining > 0:
                    _truncation_notice = (
                        f"⚠️ Remainder dropped — Discord rate limit hit "
                        f"after chunk {_delivered}/{_total_chunks}. Full "
                        f"response is in the conv-log on disk."
                    )
                    await _send_safely(
                        message.channel, _truncation_notice,
                    )
            # case (a) is implicit: no notice attempt because cool-off
            # was already active before this turn; pause-notice was
            # surfaced when cool-off first activated.
            break
        _delivered += 1


# ---------------------------------------------------------------------------
# Discord rate-limit smart-backoff wrapper
# ---------------------------------------------------------------------------
#
# DISCORD-429-SMART-BACKOFF (2026-05-08): discord.py's internal 429 handling
# honors the Retry-After header (typically 3 seconds for per-route limits).
# For Cloudflare-level rate limits / abuse-flag 429s (Discord error code
# 40062), the Retry-After is unreliable — the flag persists for
# minutes-to-hours regardless of what the header says. Repeated 3-second
# retries compound the abuse flag and extend the ban duration.
#
# This wrapper catches HTTPException(429) escaping discord.py's internal
# retry loop, applies an exponential backoff schedule that reflects observed
# abuse-flag recovery times, surfaces a specific duration to the operator
# (not generic "try again later"), and auto-resumes — no manual restart
# needed.
#
# Schedule is env-overridable via KERNOS_DISCORD_429_BACKOFF_*_SEC. After the
# final attempt, the wrapper exits loud with rotation instructions.
_DISCORD_429_BACKOFF_SCHEDULE: list[int] = [
    int(os.getenv("KERNOS_DISCORD_429_BACKOFF_1_SEC", "60")),     # 1 minute
    int(os.getenv("KERNOS_DISCORD_429_BACKOFF_2_SEC", "300")),    # 5 minutes
    int(os.getenv("KERNOS_DISCORD_429_BACKOFF_3_SEC", "1800")),   # 30 minutes
    int(os.getenv("KERNOS_DISCORD_429_BACKOFF_4_SEC", "3600")),   # 1 hour
    int(os.getenv("KERNOS_DISCORD_429_BACKOFF_5_SEC", "14400")),  # 4 hours
]


def _format_429_wait_duration(seconds: int) -> str:
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


def _run_with_429_smart_backoff(client: "discord.Client", token: str) -> None:
    """Run the Discord client with smart backoff on 429 rate-limit errors.

    Catches HTTPException(429) escaping discord.py's internal retry,
    applies the configured exponential schedule, logs the specific wait
    duration + reason + auto-retry timestamp, and re-invokes ``client.run``.
    Other HTTPException variants (PrivilegedIntentsRequired, LoginFailure,
    non-429 status codes) re-raise so the existing friendly remediation
    handlers in ``__main__`` can produce their messages.
    """
    import time as _time
    from datetime import datetime, timedelta

    attempt = 0
    schedule_len = len(_DISCORD_429_BACKOFF_SCHEDULE)
    while True:
        try:
            client.run(token)
            return  # graceful shutdown
        except discord.HTTPException as exc:
            if exc.status != 429:
                raise
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
                    "\n" + "=" * 64 + "\n"
                    f"DISCORD RATE LIMIT — backoff schedule exhausted "
                    f"({attempt} retries)\n"
                    + "=" * 64 + "\n"
                    "The token is likely Cloudflare-flagged. The flag\n"
                    "typically persists 4-24+ hours after the abuse stops.\n"
                    "Either wait longer, OR rotate the bot token:\n"
                    "  1. https://discord.com/developers/applications\n"
                    "  2. Open the application this bot belongs to\n"
                    "  3. Bot tab -> Reset Token\n"
                    "  4. Update DISCORD_BOT_TOKEN in .env\n"
                    "  5. Re-run start.sh\n",
                    file=sys.stderr, flush=True,
                )
                raise
            wait = _DISCORD_429_BACKOFF_SCHEDULE[attempt]
            attempt += 1
            code = getattr(exc, "code", None)
            human = _format_429_wait_duration(wait)
            retry_at = datetime.now() + timedelta(seconds=wait)
            logger.warning(
                "DISCORD_AUTH_429: backing off %s (attempt %d/%d, "
                "resume %s, code=%s)",
                human, attempt, schedule_len,
                retry_at.strftime("%H:%M:%S"), code,
            )
            _time.sleep(wait)


if __name__ == "__main__":
    # Startup binary health check — binary config read, no network, no LLM.
    # Exit cleanly with code 1 if any named chain has no providers configured.
    from kernos.setup.health_check import enforce_or_exit
    enforce_or_exit()

    # Workspace scope + builder toggle validation. Exit 1 on unknown values;
    # log effective configuration and any scoped/unscoped pairing warnings.
    from kernos.setup.workspace_config import enforce_or_exit as _enforce_workspace_config
    _enforce_workspace_config()

    # Startup auto-update: pull origin/{KERNOS_UPDATE_BRANCH}, reinstall deps,
    # execv restart if behind. Graceful fallback on every failure mode
    # (not a git checkout, dirty tree, network failure, diverged history).
    # This is the earliest point where config is validated but no external
    # side effects have happened — safe to replace the process.
    from kernos.setup.self_update import enforce_or_continue as _self_update
    _self_update()

    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print(
            "\n" + "=" * 60 + "\n"
            "DISCORD CONFIG ERROR: DISCORD_BOT_TOKEN is not set\n"
            + "=" * 60 + "\n"
            "Add your bot token to .env in this directory:\n"
            "  DISCORD_BOT_TOKEN=<your_token_from_discord_dev_portal>\n\n"
            "Get a token at https://discord.com/developers/applications\n"
            "(create or open an application -> Bot -> Reset Token).\n",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        # DISCORD-429-SMART-BACKOFF: wraps client.run with exponential
        # backoff retry on 429 errors. discord.py's internal Retry-After
        # handling (typically 3 seconds) is unreliable for Cloudflare-level
        # rate limits — the flag persists for minutes-to-hours regardless
        # of what the header says, and short retries compound the abuse.
        # On 429 the wrapper sleeps per the configured schedule and
        # auto-resumes; on non-429 errors re-raises so the friendly
        # remediation handlers below produce their messages.
        _run_with_429_smart_backoff(client, token)
    except discord.errors.PrivilegedIntentsRequired:
        # Friendly remediation for the most common first-run misconfig:
        # the application this token belongs to doesn't have MESSAGE
        # CONTENT INTENT enabled in the Discord developer portal.
        # Kernos requests it (server.py top: intents.message_content =
        # True), so without the toggle Discord refuses the gateway
        # connection entirely. Caught here so the operator sees the
        # exact fix instead of a 30-line discord.py traceback.
        print(
            "\n" + "=" * 60 + "\n"
            "DISCORD CONFIG ERROR: MESSAGE CONTENT INTENT is not enabled\n"
            + "=" * 60 + "\n"
            "Discord refused the bot connection because the application\n"
            "this token belongs to does not have MESSAGE CONTENT INTENT\n"
            "enabled. Kernos needs it to read message bodies.\n\n"
            "Fix (one-time, ~30 seconds):\n"
            "  1. https://discord.com/developers/applications\n"
            "  2. Open the application this bot's token belongs to\n"
            "  3. Bot tab (left sidebar)\n"
            "  4. Scroll to 'Privileged Gateway Intents'\n"
            "  5. Toggle ON: MESSAGE CONTENT INTENT\n"
            "     (leave Server Members + Presence OFF — not needed)\n"
            "  6. Click 'Save Changes' at the bottom of the page\n"
            "  7. Re-run start.sh\n\n"
            "If you already toggled it on: confirm Save Changes was\n"
            "clicked, and that the token in .env belongs to the SAME\n"
            "application you toggled (mismatched dev vs. prod tokens\n"
            "are the most common cause of this surviving step 6).\n",
            file=sys.stderr,
        )
        sys.exit(2)
    except discord.errors.LoginFailure as exc:
        # Bad token — also friendly-fail rather than dump a stack.
        print(
            "\n" + "=" * 60 + "\n"
            "DISCORD CONFIG ERROR: bot token rejected\n"
            + "=" * 60 + "\n"
            "Discord rejected DISCORD_BOT_TOKEN. Common causes:\n"
            "  - Token was reset in the Dev Portal -> copy the new one\n"
            "    into .env (and update any other places it's stored)\n"
            "  - Wrong token (mixed up dev vs. prod application)\n"
            "  - Trailing whitespace or truncation when pasting\n\n"
            f"Underlying error: {exc}\n",
            file=sys.stderr,
        )
        sys.exit(2)
