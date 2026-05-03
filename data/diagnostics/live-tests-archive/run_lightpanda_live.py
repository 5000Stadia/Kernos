#!/usr/bin/env python3
"""Live test harness for Lightpanda web browser MCP integration.

Direct handler invocation — no Discord required.
Tests: MCP connection, tool discovery, web browsing, gate behavior.
"""
import asyncio
import json
import logging
import os
import sys
import dataclasses
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(name)s %(message)s')

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from kernos.messages.handler import MessageHandler
from kernos.messages.models import NormalizedMessage, AuthLevel
from kernos.capability.client import MCPClientManager
from kernos.capability.known import KNOWN_CAPABILITIES
from kernos.capability.registry import CapabilityRegistry, CapabilityStatus
from kernos.kernel.engine import TaskEngine
from kernos.kernel.events import JsonEventStream
from kernos.kernel.reasoning import AnthropicProvider, ReasoningService
from kernos.kernel.state_json import JsonStateStore
from kernos.persistence.json_file import JsonAuditStore, JsonConversationStore, JsonInstanceStore
from mcp import StdioServerParameters

DATA_DIR = os.getenv("KERNOS_DATA_DIR", "./data")
TENANT = "discord:000000000000000000"
CONVERSATION_ID = "live_test_lightpanda"

results = []


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def make_msg(content: str) -> NormalizedMessage:
    return NormalizedMessage(
        content=content,
        sender="000000000000000000",
        sender_auth_level=AuthLevel.owner_verified,
        platform="discord",
        platform_capabilities=["text"],
        conversation_id=CONVERSATION_ID,
        timestamp=datetime.now(timezone.utc),
        instance_id=TENANT,
    )


def record(step: int, title: str, passed: bool, notes: str = ""):
    results.append({"step": step, "title": title, "passed": passed, "notes": notes})
    status = "PASS" if passed else "FAIL"
    print(f"\n{'='*60}")
    print(f"Step {step} — {title}: {status}")
    if notes:
        print(f"  {notes}")
    print(f"{'='*60}\n")


async def build_handler():
    """Construct handler with Lightpanda MCP connected."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    conversations = JsonConversationStore(DATA_DIR)
    tenants = JsonInstanceStore(DATA_DIR)
    audit = JsonAuditStore(DATA_DIR)
    events = JsonEventStream(DATA_DIR)
    state = JsonStateStore(DATA_DIR)
    provider = AnthropicProvider(api_key)
    mcp = MCPClientManager(events=events)

    # Register Lightpanda
    lightpanda_path = os.getenv("LIGHTPANDA_PATH", os.path.expanduser("~/bin/lightpanda"))
    if not Path(lightpanda_path).is_file():
        print(f"ERROR: Lightpanda binary not found at {lightpanda_path}")
        sys.exit(1)

    mcp.register_server(
        "lightpanda",
        StdioServerParameters(command=lightpanda_path, args=["mcp"]),
    )

    await mcp.connect_all()

    reasoning = ReasoningService(provider, events, mcp, audit)
    registry = CapabilityRegistry(mcp=mcp)
    for cap in KNOWN_CAPABILITIES:
        registry.register(dataclasses.replace(cap))
    # Promote connected servers
    for server_name, tools in mcp.get_tool_definitions().items():
        cap = registry.get(server_name) or registry.get_by_server_name(server_name)
        if cap:
            cap.status = CapabilityStatus.CONNECTED
            cap.tools = [t["name"] for t in tools]

    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(
        mcp, conversations, tenants, audit, events, state,
        reasoning, registry, engine,
    )
    return handler, state, events, reasoning, registry, mcp


async def run_tests():
    handler, state, events, reasoning, registry, mcp = await build_handler()

    # ==================================================================
    # Step 0: Lightpanda MCP connected
    # ==================================================================
    cap = registry.get("web-browser") or registry.get_by_server_name("lightpanda")
    is_connected = cap and cap.status == CapabilityStatus.CONNECTED
    tool_names = cap.tools if cap else []
    record(0, "Lightpanda MCP connected",
           is_connected and len(tool_names) > 0,
           f"status={cap.status if cap else 'NOT_FOUND'}, tools={tool_names}")

    # ==================================================================
    # Step 1: Expected tools discovered
    # ==================================================================
    expected = {"goto", "markdown", "links", "evaluate", "semantic_tree",
                "interactiveElements", "structuredData"}
    found = set(tool_names)
    missing = expected - found
    record(1, "All 7 expected tools discovered",
           len(missing) == 0,
           f"found={sorted(found)}, missing={sorted(missing)}")

    # ==================================================================
    # Step 2: Tool effect classification
    # ==================================================================
    web_cap = registry.get("web-browser") or registry.get("lightpanda")
    effects = web_cap.tool_effects if web_cap else {}
    reads_correct = all(effects.get(t) == "read" for t in
                        ["goto", "markdown", "semantic_tree", "interactiveElements",
                         "structuredData", "links"])
    evaluate_gated = effects.get("evaluate") == "soft_write"
    record(2, "Tool effect classifications correct",
           reads_correct and evaluate_gated,
           f"reads_correct={reads_correct}, evaluate_gated={evaluate_gated}")

    # ==================================================================
    # Step 3: Browse a webpage — "What's on Hacker News?"
    # ==================================================================
    print("\n--- Step 3: Sending web browsing request ---")
    response = await handler.process(make_msg(
        "What are the top 5 stories on Hacker News right now? Go to https://news.ycombinator.com and check."
    ))
    print(f"Response ({len(response)} chars): {response[:500]}")
    # Should contain some HN content
    has_content = len(response) > 50
    record(3, "Web browsing produces content",
           has_content,
           f"Response length: {len(response)}, preview: {response[:150]}")

    # ==================================================================
    # Step 4: Check tool.called events for goto/markdown
    # ==================================================================
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    from kernos.utils import _safe_name
    event_path = Path(DATA_DIR) / _safe_name(TENANT) / "events" / f"{today}.json"
    browser_tool_events = []
    if event_path.exists():
        with open(event_path) as f:
            try:
                all_events = json.load(f)
                for evt in all_events:
                    if (isinstance(evt, dict) and
                        evt.get("type") == "tool.called" and
                        evt.get("payload", {}).get("tool_name") in expected):
                        browser_tool_events.append(evt["payload"]["tool_name"])
            except json.JSONDecodeError:
                pass
    tools_used = set(browser_tool_events)
    record(4, "Browser tool.called events emitted",
           len(browser_tool_events) > 0,
           f"browser tools used: {sorted(tools_used)}")

    # ==================================================================
    # Step 5: Check dispatch.gate events — reads should be allowed
    # ==================================================================
    gate_events = []
    if event_path.exists():
        with open(event_path) as f:
            try:
                all_events = json.load(f)
                for evt in all_events:
                    if (isinstance(evt, dict) and
                        evt.get("type") == "dispatch.gate" and
                        evt.get("payload", {}).get("tool_name") in expected):
                        gate_events.append(evt["payload"])
            except json.JSONDecodeError:
                pass
    # Read tools should have allowed=True
    read_gates = [g for g in gate_events if g.get("effect") == "read"]
    all_reads_allowed = all(g.get("allowed", False) for g in read_gates)
    record(5, "Read tools bypass dispatch gate",
           all_reads_allowed or len(read_gates) == 0,
           f"read gate events: {len(read_gates)}, all allowed: {all_reads_allowed}")

    # Cleanup
    await mcp.disconnect_all()

    # ==================================================================
    # Summary
    # ==================================================================
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"  Step {r['step']}: {status} — {r['title']}")
        if r["notes"]:
            print(f"    {r['notes']}")
    print(f"\nTotal: {total} | PASS: {passed} | FAIL: {failed}")
    print(f"Result: {'FULL PASS' if failed == 0 else 'HAS FAILURES'}")


if __name__ == "__main__":
    asyncio.run(run_tests())
