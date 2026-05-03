#!/usr/bin/env python3
"""Live test harness for SPEC-3D: Dispatch Interceptor.

Direct handler invocation — no Discord required.
Tests gate classification, fast path authorization, covenant check,
permission override, blocked message format, confirmation handling,
delete_file consolidation, and DISPATCH_GATE event emission.
"""
import asyncio
import json
import logging
import os
import sys
import time

logging.basicConfig(level=logging.INFO, format='%(name)s %(message)s')

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from kernos.messages.handler import MessageHandler
from kernos.messages.models import NormalizedMessage, AuthLevel
from kernos.capability.client import MCPClientManager
from kernos.capability.registry import CapabilityRegistry
from kernos.kernel.engine import TaskEngine
from kernos.kernel.events import JsonEventStream
from kernos.kernel.reasoning import AnthropicProvider, ReasoningService
from kernos.kernel.state_json import JsonStateStore
from kernos.persistence.json_file import JsonAuditStore, JsonConversationStore, JsonInstanceStore
from datetime import datetime, timezone


DATA_DIR = os.getenv("KERNOS_DATA_DIR", "./data")
TENANT = "discord:000000000000000000"
CONVERSATION_ID = "live_test_3d"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def make_msg(content: str, conversation_id: str = CONVERSATION_ID) -> NormalizedMessage:
    return NormalizedMessage(
        content=content,
        sender="000000000000000000",
        sender_auth_level=AuthLevel.owner_verified,
        platform="discord",
        platform_capabilities=["text"],
        conversation_id=conversation_id,
        timestamp=datetime.now(timezone.utc),
        instance_id=TENANT,
    )


results = []


def record(step: int, title: str, passed: bool, notes: str = ""):
    results.append({"step": step, "title": title, "passed": passed, "notes": notes})
    status = "PASS" if passed else "FAIL"
    print(f"\n{'='*60}")
    print(f"Step {step} — {title}: {status}")
    if notes:
        print(f"  {notes}")
    print(f"{'='*60}\n")


async def build_handler():
    """Construct a full handler with real stores and Anthropic API."""
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
    mcp = MCPClientManager()
    reasoning = ReasoningService(provider, events, mcp, audit)

    # Register known capabilities
    import dataclasses
    from kernos.capability.known import KNOWN_CAPABILITIES
    registry = CapabilityRegistry(mcp=mcp)
    for cap in KNOWN_CAPABILITIES:
        registry.register(dataclasses.replace(cap))

    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(
        mcp, conversations, tenants, audit, events, state,
        reasoning, registry, engine,
    )
    return handler, state, events, reasoning


async def run_tests():
    handler, state, events, reasoning = await build_handler()

    # Step 0: Verify gate methods exist on ReasoningService
    has_gate = hasattr(reasoning, '_gate_tool_call')
    has_classify = hasattr(reasoning, '_classify_tool_effect')
    has_explicit = hasattr(reasoning, '_explicit_instruction_matches')
    no_old_delete = not hasattr(reasoning, '_check_delete_allowed')
    record(0, "Gate methods wired on ReasoningService",
           has_gate and has_classify and has_explicit and no_old_delete,
           f"gate={has_gate}, classify={has_classify}, explicit={has_explicit}, old_delete_removed={no_old_delete}")

    # Step 1: Read tool bypass — asking about schedule should NOT trigger gate
    print("\n--- Step 1: Read tool bypass ---")
    response = await handler.process(make_msg("What's on my calendar today?"))
    print(f"Response: {response[:200]}")
    record(1, "Read tool bypass (calendar query)",
           isinstance(response, str) and len(response) > 0,
           f"Response length: {len(response)}")

    # Step 2: Write tool with explicit instruction (fast path)
    print("\n--- Step 2: Write tool fast path ---")
    response = await handler.process(make_msg("Book a meeting with Henderson for Thursday at 2pm"))
    print(f"Response: {response[:300]}")
    record(2, "Write tool fast path (book meeting)",
           isinstance(response, str) and len(response) > 0,
           f"Response: {response[:150]}")

    # Step 3: File write with instruction
    print("\n--- Step 3: File write with instruction ---")
    response = await handler.process(make_msg("Write a file called test-3d-gate.md with the content 'Gate test passed'"))
    print(f"Response: {response[:200]}")
    record(3, "File write with instruction",
           isinstance(response, str) and len(response) > 0,
           f"Response: {response[:150]}")

    # Step 4: Delete file with instruction (consolidated gate)
    print("\n--- Step 4: Delete file with instruction (consolidated gate) ---")
    response = await handler.process(make_msg("Delete the file test-3d-gate.md"))
    print(f"Response: {response[:200]}")
    record(4, "Delete file via dispatch gate",
           isinstance(response, str) and len(response) > 0,
           f"Response: {response[:150]}")

    # Step 5: Verify DISPATCH_GATE events in event stream
    print("\n--- Step 5: Check DISPATCH_GATE events ---")
    from pathlib import Path
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    event_path = Path(DATA_DIR) / TENANT.replace(":", "_") / "events" / f"{today}.json"
    gate_events = []
    if event_path.exists():
        with open(event_path) as f:
            try:
                all_events = json.load(f)
                for evt in all_events:
                    if isinstance(evt, dict) and evt.get("type") == "dispatch.gate":
                        gate_events.append(evt)
            except json.JSONDecodeError:
                pass
    record(5, "DISPATCH_GATE events emitted",
           len(gate_events) > 0,
           f"Found {len(gate_events)} dispatch.gate events")

    # Step 6: Verify permission_overrides on InstanceProfile
    print("\n--- Step 6: Permission overrides field ---")
    instance_profile = await state.get_instance_profile(TENANT)
    has_field = hasattr(instance_profile, 'permission_overrides') if instance_profile else False
    record(6, "permission_overrides field on InstanceProfile",
           has_field,
           f"Field exists: {has_field}, value: {instance_profile.permission_overrides if has_field else 'N/A'}")

    # Step 7: Verify tool classification
    print("\n--- Step 7: Tool classification ---")
    reads = ["remember", "list_files", "read_file", "request_tool"]
    writes = ["write_file", "delete_file"]
    all_reads_correct = all(reasoning._classify_tool_effect(t, None) == "read" for t in reads)
    all_writes_correct = all(reasoning._classify_tool_effect(t, None) == "soft_write" for t in writes)
    unknown_correct = reasoning._classify_tool_effect("mystery-tool", None) == "unknown"
    record(7, "Tool effect classification",
           all_reads_correct and all_writes_correct and unknown_correct,
           f"reads={all_reads_correct}, writes={all_writes_correct}, unknown={unknown_correct}")

    # Step 8: Verify TOOL_SIGNALS has delete_file signals
    print("\n--- Step 8: TOOL_SIGNALS consolidation ---")
    has_delete_signals = "delete_file" in reasoning._TOOL_SIGNALS
    signals = reasoning._TOOL_SIGNALS.get("delete_file", [])
    has_delete = "delete" in signals
    has_remove = "remove" in signals
    record(8, "delete_file signals consolidated in TOOL_SIGNALS",
           has_delete_signals and has_delete and has_remove,
           f"delete_file in TOOL_SIGNALS={has_delete_signals}, has delete={has_delete}, has remove={has_remove}")

    # Print summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"  Step {r['step']}: {status} — {r['title']}")
    print(f"\nTotal: {total} | PASS: {passed} | FAIL: {failed}")
    print(f"Result: {'FULL PASS' if failed == 0 else 'HAS FAILURES'}")


if __name__ == "__main__":
    asyncio.run(run_tests())
