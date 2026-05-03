"""Isolated handler bootstrap for eval scenarios.

Constructs a real MessageHandler pointed at a temporary data directory with:
- Real state store, instance_db, conversations, events, audit
- Real reasoning service with real provider chains (from env)
- Empty MCP manager (no external tool connections)
- No awareness evaluator (background timers are noise in evals)
- No adapters (messages are injected directly, outbound is captured)

Each BootstrappedInstance owns its temp directory and must be closed to clean up.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from kernos.capability.client import MCPClientManager
from kernos.capability.registry import CapabilityRegistry
from kernos.evals.types import MemberSpec, Setup
from kernos.kernel.engine import TaskEngine
from kernos.kernel.events import JsonEventStream
from kernos.kernel.instance_db import InstanceDB
from kernos.kernel.reasoning import ReasoningService
from kernos.kernel.state_json import JsonStateStore
from kernos.messages.handler import MessageHandler
from kernos.messages.models import AuthLevel, NormalizedMessage
from kernos.persistence.json_file import (
    JsonAuditStore, JsonConversationStore, JsonInstanceStore,
)
from kernos.providers.chains import build_chains_from_env
from kernos.utils import utc_now

logger = logging.getLogger(__name__)


@dataclass
class OutboundRecord:
    """A captured send_outbound call — whispers, scheduled messages, etc."""
    instance_id: str
    member_id: str
    channel_name: str
    message: str
    timestamp: str


class RecordingAdapter:
    """Minimal adapter that captures outbound calls instead of sending them.

    Registered per platform so send_outbound doesn't silently drop. Matches the
    BaseAdapter send_outbound signature but records to a list.
    """

    def __init__(self, platform: str, outbound: list[OutboundRecord]) -> None:
        self.platform = platform
        self._outbound = outbound
        self.can_send_outbound = True

    async def send_outbound(
        self, instance_id: str, channel_target: str, message: str,
    ) -> int:
        # The handler calls this via send_to_channel / whisper flow.
        self._outbound.append(OutboundRecord(
            instance_id=instance_id,
            member_id="",  # not available here; captured by handler-level hooks if needed
            channel_name=self.platform,
            message=message,
            timestamp=utc_now(),
        ))
        return 1  # pretend success


@dataclass
class BootstrappedInstance:
    """A running handler with its isolated data directory.

    Use as async context manager or call `close()` to clean up.
    """
    data_dir: Path
    handler: MessageHandler
    instance_db: InstanceDB
    state: JsonStateStore
    events: JsonEventStream
    reasoning: ReasoningService
    mcp: MCPClientManager
    member_id_map: dict[str, str] = field(default_factory=dict)  # scenario_id → real member_id
    outbound: list[OutboundRecord] = field(default_factory=list)
    _original_env: dict[str, str] = field(default_factory=dict)

    async def close(self) -> None:
        """Stop runners, close DB connections, delete temp data dir, restore env."""
        try:
            await self.handler.shutdown_runners()
        except Exception as exc:
            logger.warning("eval_bootstrap: shutdown_runners failed: %s", exc)
        try:
            await self.instance_db.close()
        except Exception as exc:
            logger.warning("eval_bootstrap: instance_db close failed: %s", exc)
        try:
            from kernos.kernel.state_sqlite import SqliteStateStore  # noqa
            if hasattr(self.state, "close_all"):
                await self.state.close_all()
        except Exception:
            pass
        # Restore env
        for k, v in self._original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # Remove temp dir
        try:
            if self.data_dir.exists():
                shutil.rmtree(self.data_dir)
        except Exception as exc:
            logger.warning("eval_bootstrap: temp dir cleanup failed: %s", exc)


async def bootstrap_instance(
    setup: Setup,
    instance_id: str = "eval_instance",
    compaction_threshold: int | None = None,
) -> BootstrappedInstance:
    """Stand up a real MessageHandler in an isolated temp data directory.

    Args:
        setup: What state to prepare (fresh_instance, members).
        instance_id: Identifier for this eval instance (defaults to "eval_instance").
        compaction_threshold: If set, overrides KERNOS_COMPACTION_THRESHOLD for this run
            so scenarios can force compaction to fire on a few messages.
    """
    # Temp dir for all eval state
    temp_dir = Path(tempfile.mkdtemp(prefix="kernos_eval_"))
    data_dir = temp_dir / "data"
    secrets_dir = temp_dir / "secrets"
    data_dir.mkdir(parents=True, exist_ok=True)
    secrets_dir.mkdir(parents=True, exist_ok=True)

    # Capture and override env
    env_keys = [
        "KERNOS_DATA_DIR", "KERNOS_SECRETS_DIR", "KERNOS_STORE_BACKEND",
        "KERNOS_COMPACTION_THRESHOLD", "KERNOS_INSTANCE_ID",
    ]
    original_env: dict[str, str] = {k: os.environ.get(k) for k in env_keys}  # type: ignore

    os.environ["KERNOS_DATA_DIR"] = str(data_dir)
    os.environ["KERNOS_SECRETS_DIR"] = str(secrets_dir)
    os.environ["KERNOS_STORE_BACKEND"] = "json"  # Simpler for eval isolation
    os.environ["KERNOS_INSTANCE_ID"] = instance_id
    if compaction_threshold is not None:
        os.environ["KERNOS_COMPACTION_THRESHOLD"] = str(compaction_threshold)

    # Core stores
    events = JsonEventStream(str(data_dir))
    state = JsonStateStore(str(data_dir))
    conversations = JsonConversationStore(str(data_dir))
    tenants = JsonInstanceStore(str(data_dir))
    audit = JsonAuditStore(str(data_dir))

    # Instance DB
    instance_db = InstanceDB(str(data_dir))
    await instance_db.connect()

    # MCP — empty. No external tool connections.
    mcp = MCPClientManager(events=events)

    # Capability registry — empty (no MCP capabilities registered).
    registry = CapabilityRegistry(mcp=mcp)

    # Provider chains — real. Reads KERNOS_LLM_PROVIDER / KERNOS_LLM_FALLBACK from env.
    chains, _ = build_chains_from_env()

    # REASONING-SERVICE-CONSTRUCTION-PARITY-V1: shared turn-runner-
    # provider wiring. Eval scenarios go through the same cognition
    # path as production — different door, same handler.
    from kernos.kernel.turn_runner_provider import (
        build_turn_runner_provider,
        setup_default_thin_path_context,
        wire_live_thin_path,
    )

    _thin_path_ctx = setup_default_thin_path_context(
        chains=chains, state=state, events=events, audit=audit,
    )
    reasoning = ReasoningService(
        events=events, mcp=mcp, audit=audit, chains=chains,
        trace_sink=_thin_path_ctx.trace_sink,
        turn_runner_provider=build_turn_runner_provider(_thin_path_ctx),
    )
    engine = TaskEngine(reasoning=reasoning, events=events)

    handler = MessageHandler(
        mcp=mcp, conversations=conversations, tenants=tenants,
        audit=audit, events=events, state=state, reasoning=reasoning,
        registry=registry, engine=engine, secrets_dir=str(secrets_dir),
    )
    handler._instance_db = instance_db  # same post-init pattern as server.py
    wire_live_thin_path(
        _thin_path_ctx,
        reasoning=reasoning,
        handler=handler,
    )

    bi = BootstrappedInstance(
        data_dir=data_dir,
        handler=handler,
        instance_db=instance_db,
        state=state,
        events=events,
        reasoning=reasoning,
        mcp=mcp,
        _original_env=original_env,
    )

    # Register recording adapters for any platform referenced in setup
    platforms_used = {m.platform for m in setup.members if m.platform}
    for platform in platforms_used:
        adapter = RecordingAdapter(platform, bi.outbound)
        handler.register_adapter(platform, adapter)
        handler.register_channel(
            name=platform, display_name=platform.title(),
            platform=platform, can_send_outbound=True, channel_target="",
        )

    # Create members declared in setup
    await _provision_members(bi, setup, instance_id)

    return bi


async def _provision_members(
    bi: BootstrappedInstance, setup: Setup, instance_id: str,
) -> None:
    """Create the members declared in the scenario's Setup section."""
    for m in setup.members:
        if m.role == "owner":
            stable_id = await bi.instance_db.ensure_owner(
                member_id="",
                display_name=m.display_name or "owner",
                instance_id=instance_id,
                platform=m.platform,
                channel_id=m.channel_id,
            )
            bi.member_id_map[m.id] = stable_id
        else:
            # Use the scenario id as the real member_id — predictable for tests.
            await bi.instance_db.create_member(m.id, m.display_name, m.role, "")
            await bi.instance_db.register_channel(m.id, m.platform, m.channel_id)
            await bi.instance_db.upsert_member_profile(m.id, {
                "display_name": m.display_name,
            })
            bi.member_id_map[m.id] = m.id


def build_message(
    bi: BootstrappedInstance,
    sender: str,
    platform: str,
    content: str,
    instance_id: str = "eval_instance",
) -> NormalizedMessage:
    """Build a NormalizedMessage for injection, resolving the scenario sender id."""
    # Find the channel_id for this scenario member on this platform
    channel_id = ""
    for scenario_id, real_id in bi.member_id_map.items():
        if scenario_id != sender:
            continue
        # Find setup member to get channel_id
        for m in _iter_members(bi):
            if m.id == scenario_id and m.platform == platform:
                channel_id = m.channel_id
                break
        break

    # If sender is "new_user" or unknown, use content as raw channel_id
    if not channel_id:
        channel_id = f"unknown_{sender}"

    return NormalizedMessage(
        content=content,
        sender=channel_id,
        sender_auth_level=AuthLevel.owner_verified,
        platform=platform,
        platform_capabilities=["text"],
        conversation_id=channel_id,
        timestamp=datetime.now(timezone.utc),
        instance_id=instance_id,
    )


def _iter_members(bi: BootstrappedInstance):
    """Iterator used during message building — re-reads scenario members.

    Stored MemberSpecs are needed for channel_id lookup; we stash them on the
    bootstrapped instance via a hidden attr so build_message can find them.
    """
    return getattr(bi, "_setup_members", [])


def attach_setup_members(bi: BootstrappedInstance, members: list[MemberSpec]) -> None:
    """Attach the raw MemberSpecs so build_message can resolve channel_ids."""
    bi._setup_members = members  # type: ignore[attr-defined]
