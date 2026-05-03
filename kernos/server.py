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
from kernos.kernel.log_buffer import install_log_ring_buffer
install_log_ring_buffer()

logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
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
            _tool = payload.get("tool_id", "?")
            _seam = payload.get("seam", "")
            _err = payload.get("is_error", False)
            if _t == "tool.called":
                logger.info(
                    "TOOL_CALLED: tool=%s seam=%s classification=%s",
                    _tool, _seam, payload.get("classification", "?"),
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

    def _build_per_turn_runner(request, event_emitter):
        """Per-turn factory: builds a fully-wired TurnRunner +
        ProductionResponseDelivery for this request.

        Returns (TurnRunner, ProductionResponseDelivery) — the
        ReasoningService routing layer fires emit_request_event()
        on the delivery before invoking run_turn().

        Per-turn binding is the load-bearing architectural shape:
        AggregatedTelemetry is fresh per turn so token aggregation
        + tool_iterations accumulate correctly; ProductionResponseDelivery
        captures the request so synthetic events carry the right
        identifiers; chain wrappers bind the same telemetry across
        all four hooks so cost-tracking aggregates ONCE.
        """
        telemetry = AggregatedTelemetry()
        wrapped_chain = wrap_chain_caller_with_telemetry(
            _shared_chain_caller, telemetry
        )

        per_turn_planner = Planner(
            chain_caller=wrapped_chain,
            tool_catalog=planner_tool_catalog,
        )
        per_turn_dispatcher = StepDispatcher(
            executor=shared_executor,
            descriptor_lookup=shared_descriptor_lookup,
            trace_sink=reasoning_trace_sink,
            event_emitter=_dispatcher_event_emitter,
            audit_emitter=_dispatcher_audit_emitter,
            on_dispatch_complete=telemetry.add_tool_iteration,
        )
        per_turn_reasoner = DivergenceReasoner(chain_caller=wrapped_chain)
        # INTEGRATION-CAPABILITY-FIRST-V1 Batch 2 Fold 1: bridge the
        # renderer's keyword-style tool-dispatcher contract to the
        # integration runner's positional (tool_id, args, inputs)
        # contract via the adapter shim. Both seams stay intact.
        # CC-scope follow-up to Batch 2 Codex review: thread the
        # per-turn request's identifiers through the inputs_factory
        # so the dispatcher's request_factory has real
        # instance_id/member_id/space_id to populate downstream
        # ReasoningRequest with. Pre-fix the adapter passed
        # inputs=None and the request_factory built with empty
        # identifiers everywhere on inline tool calls.
        from kernos.kernel.integration.live_wiring import (
            build_renderer_to_integration_adapter,
        )

        @dataclasses.dataclass(frozen=True)
        class _RendererTurnInputs:
            instance_id: str
            member_id: str
            space_id: str
            turn_id: str

        def _renderer_inputs_factory(conversation_id: str) -> Any:
            return _RendererTurnInputs(
                instance_id=getattr(request, "instance_id", "") or "",
                member_id=getattr(request, "member_id", "") or "",
                space_id=getattr(request, "active_space_id", "") or "",
                turn_id=conversation_id or getattr(request, "conversation_id", "") or "",
            )

        per_turn_presence = PresenceRenderer(
            chain_caller=wrapped_chain,
            tool_dispatcher=build_renderer_to_integration_adapter(
                integration_dispatcher=_integration_dispatcher,
                inputs_factory=_renderer_inputs_factory,
            ),
        )

        per_turn_integration = IntegrationService(
            chain_caller=wrapped_chain,
            read_only_dispatcher=_integration_dispatcher,
            audit_emitter=_integration_audit_emitter,
        )
        per_turn_enactment = EnactmentService(
            presence_renderer=per_turn_presence,
            planner=per_turn_planner,
            step_dispatcher=per_turn_dispatcher,
            divergence_reasoner=per_turn_reasoner,
        )

        delivery = ProductionResponseDelivery(
            request=request,
            telemetry=telemetry,
            event_emitter=event_emitter,
        )

        per_turn_turn_runner = TurnRunner(
            cohort_runner=cohort_runner,
            integration_service=per_turn_integration,
            enactment_service=per_turn_enactment,
            response_delivery=delivery,
        )
        return per_turn_turn_runner, delivery

    reasoning = ReasoningService(
        events=events,
        mcp=mcp_manager,
        audit=audit,
        chains=chains,
        trace_sink=reasoning_trace_sink,
        turn_runner_provider=_build_per_turn_runner,
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
    from kernos.kernel.integration.live_wiring import (
        LiveDescriptorLookup,
        LiveExecutor,
        LiveIntegrationDispatcher,
        LivePlannerCatalog,
    )

    async def _resolve_user_timezone(instance_id: str, member_id: str) -> str:
        """Best-effort: pull user_timezone from the soul/member profile
        for manage_schedule-style tools that consult it. Empty string
        on miss (legacy execute_tool also accepts that). CC-scope item
        from Codex Batch 2 review."""
        if not instance_id or not state:
            return ""
        try:
            profile = await state.get_member_profile(instance_id, member_id) if member_id else None
            if profile:
                return getattr(profile, "timezone", "") or ""
        except Exception:
            pass
        return ""

    def _live_request_factory(*args) -> Any:
        """Build a minimal ReasoningRequest-shaped object for
        execute_tool routing. Either receives ToolExecutionInputs
        (executor path) or (tool_id, args, inputs) positional
        (integration dispatcher path) — both supply the fields
        execute_tool consults (instance_id, active_space_id,
        member_id, user_timezone)."""
        if len(args) == 1:
            inputs = args[0]
            instance_id = getattr(inputs, "instance_id", "") or ""
            member_id = getattr(inputs, "member_id", "") or ""
            return ReasoningRequest(
                instance_id=instance_id,
                conversation_id=getattr(inputs, "turn_id", "") or "",
                system_prompt="",
                messages=[],
                tools=[],
                model="",
                trigger="thin-path-executor",
                active_space_id=getattr(inputs, "space_id", "") or "",
                member_id=member_id,
            )
        _, _, dispatch_inputs = args
        instance_id = getattr(dispatch_inputs, "instance_id", "") or ""
        member_id = getattr(dispatch_inputs, "member_id", "") or ""
        return ReasoningRequest(
            instance_id=instance_id,
            conversation_id=getattr(dispatch_inputs, "turn_id", "") or "",
            system_prompt="",
            messages=[],
            tools=[],
            model="",
            trigger="thin-path-integration-dispatcher",
            active_space_id=getattr(dispatch_inputs, "space_id", "") or "",
            member_id=member_id,
        )

    shared_descriptor_lookup = LiveDescriptorLookup(
        tool_catalog=handler._tool_catalog,
    )
    shared_executor = LiveExecutor(
        execute_tool=reasoning.execute_tool,
        gate=reasoning._get_gate(),
        request_factory=lambda inputs: _live_request_factory(inputs),
    )
    _integration_dispatcher = LiveIntegrationDispatcher(
        execute_tool=reasoning.execute_tool,
        gate=reasoning._get_gate(),
        request_factory=lambda tid, args, inp: _live_request_factory(tid, args, inp),
        # Fold 8: emit tool.called/tool.result + audit on every dispatch
        # so equivalence soak can compare audit/event trails between
        # legacy and thin paths. Reuses the existing dispatcher emitters
        # wired further up so events flow through the canonical
        # EventStream + AuditStore.
        event_emitter=_dispatcher_event_emitter,
        audit_emitter=_dispatcher_audit_emitter,
    )
    planner_tool_catalog = LivePlannerCatalog(
        tool_catalog=handler._tool_catalog,
    )
    logger.info(
        "INTEGRATION_CAPABILITY_FIRST_V1_BATCH2: live workshop binding "
        "wired (descriptor_lookup + executor + integration_dispatcher + "
        "planner_catalog all live; gate-at-dispatch enforcement active)",
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

    await tree.sync()
    logger.info("Slash commands synced")





DISCORD_MAX_LENGTH = 2000


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


@client.event
async def on_message(message):
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

    # Typing animation during processing (no placeholder message)
    try:
        async with message.channel.typing():
            response_text = await handler.process(normalized)
    except Exception as exc:
        logger.error("Handler error: %s", exc, exc_info=True)
        await message.channel.send("Something went wrong — try again in a moment.")
        try:
            await message.add_reaction("⚠️")
        except Exception:
            pass
        return

    if not response_text:  # Merged message — response comes from primary turn
        return

    for chunk in _chunk_response(response_text):
        await message.channel.send(chunk)


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
        client.run(token)
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
