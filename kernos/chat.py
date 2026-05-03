#!/usr/bin/env python3
"""KERNOS CLI Chat — talk to Kernos from the command line.

Uses the same handler, state, spaces, and knowledge as any other adapter.
Same tenant, different door. Or create a fresh tenant for testing.

Usage:
  source .venv/bin/activate
  python -m kernos.chat                     # interactive tenant picker
  python -m kernos.chat --tenant "discord:000000000000000000"
  python -m kernos.chat --new "test:onboarding"   # fresh tenant
  python -m kernos.chat -q                  # quiet (suppress logs)
"""
import asyncio
import dataclasses
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _setup_logging(quiet: bool) -> None:
    level = logging.WARNING if quiet else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def build_handler():
    """Construct the full handler stack — same as server.py."""
    from mcp import StdioServerParameters

    from kernos.capability.client import AuthCommand, MCPClientManager
    from kernos.capability.known import KNOWN_CAPABILITIES
    from kernos.capability.registry import CapabilityRegistry, CapabilityStatus
    from kernos.kernel.credentials import resolve_anthropic_credential
    from kernos.kernel.engine import TaskEngine
    from kernos.kernel.event_types import EventType
    from kernos.kernel.events import JsonEventStream, emit_event
    from kernos.kernel.reasoning import AnthropicProvider, ReasoningService
    from kernos.kernel.state_json import JsonStateStore
    from kernos.messages.handler import MessageHandler
    from kernos.persistence.json_file import (
        JsonAuditStore,
        JsonConversationStore,
        JsonInstanceStore,
    )

    data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
    events = JsonEventStream(data_dir)
    state = JsonStateStore(data_dir)

    try:
        await emit_event(events, EventType.SYSTEM_STARTED, "system", "cli_chat", payload={})
    except Exception:
        pass

    mcp_manager = MCPClientManager(events=events)

    # Register MCP servers (same as server.py)
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

    import sys

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

    registry = CapabilityRegistry(mcp=mcp_manager)
    for cap in KNOWN_CAPABILITIES:
        registry.register(dataclasses.replace(cap))
    for server_name, tools in mcp_manager.get_tool_definitions().items():
        cap = registry.get(server_name) or registry.get_by_server_name(server_name)
        if cap:
            cap.status = CapabilityStatus.CONNECTED
            cap.tools = [t["name"] for t in tools]

    # REASONING-SERVICE-CONSTRUCTION-PARITY-V1: see app.py / server.py
    # for full rationale. Same wiring pattern; chat.py inherits the
    # default thin-path context.
    from kernos.providers.chains import build_chains_from_env
    from kernos.kernel.turn_runner_provider import (
        build_turn_runner_provider,
        setup_default_thin_path_context,
        wire_live_thin_path,
    )

    chains, _primary_provider = build_chains_from_env()
    _thin_path_ctx = setup_default_thin_path_context(
        chains=chains, state=state, events=events, audit=audit,
    )
    reasoning = ReasoningService(
        events=events,
        mcp=mcp_manager,
        audit=audit,
        chains=chains,
        trace_sink=_thin_path_ctx.trace_sink,
        turn_runner_provider=build_turn_runner_provider(_thin_path_ctx),
    )
    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(
        mcp_manager, conversations, tenants, audit, events, state,
        reasoning, registry, engine,
        secrets_dir=os.getenv("KERNOS_SECRETS_DIR", "./secrets"),
    )
    wire_live_thin_path(
        _thin_path_ctx,
        reasoning=reasoning,
        handler=handler,
    )

    return handler, mcp_manager


# ---------------------------------------------------------------------------
# Tenant discovery
# ---------------------------------------------------------------------------


def _list_tenants() -> list[dict]:
    """List all instances with metadata."""
    import json

    data_dir = Path(os.getenv("KERNOS_DATA_DIR", "./data"))
    if not data_dir.exists():
        return []

    tenants = []
    for d in sorted(data_dir.iterdir()):
        if not d.is_dir():
            continue
        profile_path = d / "state" / "profile.json"
        soul_path = d / "state" / "soul.json"

        if not profile_path.exists():
            continue

        # Reconstruct instance_id from dir name
        instance_id = d.name.replace("_", ":", 1)

        info: dict = {"instance_id": instance_id, "dir": d.name}

        try:
            profile = json.loads(profile_path.read_text())
            info["status"] = profile.get("status", "?")
            info["platforms"] = list(profile.get("platforms", {}).keys())
        except Exception:
            info["status"] = "?"
            info["platforms"] = []

        if soul_path.exists():
            try:
                soul = json.loads(soul_path.read_text())
                info["user_name"] = soul.get("user_name", "")
                info["agent_name"] = soul.get("agent_name", "")
                info["interactions"] = soul.get("interaction_count", 0)
            except Exception:
                pass

        tenants.append(info)

    return tenants


def _pickinstance_interactive() -> str:
    """Show available tenants and let the user pick one, or create new."""
    tenants = _list_tenants()

    print("\n╔══════════════════════════════════════════════════╗")
    print("║            Kernos CLI Chat — Select Tenant       ║")
    print("╚══════════════════════════════════════════════════╝\n")

    if tenants:
        print("  Existing tenants:\n")
        for i, t in enumerate(tenants, 1):
            user = t.get("user_name", "")
            agent = t.get("agent_name", "")
            interactions = t.get("interactions", 0)
            platforms = ", ".join(t.get("platforms", []))

            label = t["instance_id"]
            details = []
            if user:
                details.append(f"user: {user}")
            if agent:
                details.append(f"agent: {agent}")
            if interactions:
                details.append(f"{interactions} interactions")
            if platforms:
                details.append(platforms)

            detail_str = f"  ({', '.join(details)})" if details else ""
            print(f"    {i}. {label}{detail_str}")

        print(f"\n    N. Create new tenant")
        print()

        while True:
            choice = input("  Choose [1-{}, N]: ".format(len(tenants))).strip()

            if choice.upper() == "N":
                return _createinstance_interactive()

            try:
                idx = int(choice) - 1
                if 0 <= idx < len(tenants):
                    return tenants[idx]["instance_id"]
            except ValueError:
                pass

            # Also accept raw instance_id
            if ":" in choice:
                return choice

            print("  Invalid choice. Try again.")
    else:
        print("  No existing tenants found.\n")
        return _createinstance_interactive()


def _createinstance_interactive() -> str:
    """Prompt for a new tenant ID."""
    print("\n  Create a new tenant. This starts a fresh Kernos instance")
    print("  with no history, no knowledge, no spaces — clean onboarding.\n")
    print("  Tenant ID format: <platform>:<identifier>")
    print("  Examples: cli:testing, cli:onboarding, test:demo\n")

    while True:
        tid = input("  Tenant ID: ").strip()
        if not tid:
            print("  Cannot be empty.")
            continue
        if ":" not in tid:
            tid = f"cli:{tid}"
            print(f"  → Using: {tid}")
        return tid


# ---------------------------------------------------------------------------
# Chat loop
# ---------------------------------------------------------------------------


async def chat_loop(instance_id: str, handler, mcp_manager) -> None:
    from kernos.messages.models import AuthLevel, NormalizedMessage

    conversation_id = f"cli_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    # Split instance_id into platform + sender so derive_instance_id reconstructs
    # the correct instance_id (e.g., "discord:364303..." → platform="discord", sender="364303...")
    if ":" in instance_id:
        platform, sender = instance_id.split(":", 1)
    else:
        platform, sender = "cli", instance_id

    print(f"\n{'─' * 50}")
    print(f"  Kernos CLI Chat")
    print(f"  Tenant:  {instance_id}")
    print(f"  Session: {conversation_id}")
    print(f"{'─' * 50}")
    print(f"  Type 'quit' or 'exit' to end. Ctrl+C also works.\n")

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit"):
            break

        msg = NormalizedMessage(
            content=user_input,
            sender=sender,
            sender_auth_level=AuthLevel.owner_verified,
            platform=platform,
            platform_capabilities=["text"],
            conversation_id=conversation_id,
            timestamp=datetime.now(timezone.utc),
            instance_id=instance_id,
        )

        try:
            response = await handler.process(msg)
            print(f"\nkernos> {response}\n")
        except Exception as exc:
            print(f"\n[error] {exc}\n")

    # Cleanup
    print("Disconnecting...")
    try:
        await mcp_manager.disconnect_all()
    except Exception:
        pass
    print("Goodbye.")


# ---------------------------------------------------------------------------
# Script mode — send messages from a file, one per line
# ---------------------------------------------------------------------------


async def script_loop(
    instance_id: str, handler, mcp_manager, script_path: str,
) -> None:
    """Send each line from a file as a message, print responses."""
    from kernos.messages.models import AuthLevel, NormalizedMessage

    path = Path(script_path)
    if not path.exists():
        print(f"Error: Script file not found: {script_path}")
        return

    lines = [
        line.strip() for line in path.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        print("Error: Script file is empty (no non-blank, non-comment lines).")
        return

    conversation_id = f"cli_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    if ":" in instance_id:
        platform, sender = instance_id.split(":", 1)
    else:
        platform, sender = "cli", instance_id

    print(f"\n{'─' * 50}")
    print(f"  Kernos Script Mode")
    print(f"  Tenant:  {instance_id}")
    print(f"  Script:  {script_path} ({len(lines)} messages)")
    print(f"  Session: {conversation_id}")
    print(f"{'─' * 50}\n")

    for i, user_input in enumerate(lines, 1):
        print(f"[{i}/{len(lines)}] you> {user_input}")

        msg = NormalizedMessage(
            content=user_input,
            sender=sender,
            sender_auth_level=AuthLevel.owner_verified,
            platform=platform,
            platform_capabilities=["text"],
            conversation_id=conversation_id,
            timestamp=datetime.now(timezone.utc),
            instance_id=instance_id,
        )

        try:
            response = await handler.process(msg)
            print(f"\nkernos> {response}\n")
        except Exception as exc:
            print(f"\n[error] {exc}\n")

    # Cleanup
    print(f"{'─' * 50}")
    print(f"  Script complete. {len(lines)} messages sent.")
    print(f"{'─' * 50}")
    try:
        await mcp_manager.disconnect_all()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="kernos.chat",
        description="Talk to Kernos from the command line.",
    )
    parser.add_argument(
        "--tenant", "-t",
        help="Connect to a specific tenant ID",
    )
    parser.add_argument(
        "--new", "-n",
        metavar="TENANT_ID",
        help="Create and connect to a new tenant (e.g., 'cli:testing')",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress background logs (only show chat)",
    )
    parser.add_argument(
        "--script", "-s",
        metavar="FILE",
        help="Send messages from a file (one per line), print responses, then exit",
    )
    args = parser.parse_args()

    _setup_logging(args.quiet)

    # Determine tenant
    instance_id = os.getenv("KERNOS_INSTANCE_ID", "")
    if args.new:
        instance_id = args.new if ":" in args.new else f"cli:{args.new}"
    elif args.tenant:
        instance_id = args.tenant
    elif instance_id:
        instance_id = instance_id
    else:
        instance_id = _pickinstance_interactive()

    if not instance_id:
        print("No tenant selected.")
        sys.exit(1)

    async def run():
        handler, mcp = await build_handler()
        if args.script:
            await script_loop(instance_id, handler, mcp, args.script)
        else:
            await chat_loop(instance_id, handler, mcp)

    asyncio.run(run())


if __name__ == "__main__":
    # Startup binary health check — binary config read, no network, no LLM.
    from kernos.setup.health_check import enforce_or_exit
    enforce_or_exit()

    main()
