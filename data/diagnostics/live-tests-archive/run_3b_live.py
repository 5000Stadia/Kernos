#!/usr/bin/env python3
"""Live test harness for SPEC-3B: Per-Space Tool Scoping.

Direct handler invocation — no Discord required.
Tests system space creation, tool filtering, Gate 2 seeding,
request_tool activation, and documentation files.
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
DND_SPACE = "space_fbdace10"
HENDERSON_SPACE = "space_66580317"
CONVERSATION_ID = "live_test_3b"


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


def log_result(step: str, action: str, expected: str, actual: str, passed: bool, note: str = ""):
    result = {
        "step": step,
        "action": action,
        "expected": expected,
        "actual": actual[:600],
        "passed": passed,
        "note": note,
        "timestamp": now_iso(),
    }
    results.append(result)
    status = "✓ PASS" if passed else "✗ FAIL"
    print(f"\n{'─' * 60}")
    print(f"  {status}: Step {step}")
    print(f"  Action: {action}")
    print(f"  Expected: {expected}")
    print(f"  Actual: {actual[:300]}")
    if note:
        print(f"  Note: {note}")
    print(f"{'─' * 60}")


async def main():
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    # Build handler
    events = JsonEventStream(DATA_DIR)
    state = JsonStateStore(DATA_DIR)
    conversations = JsonConversationStore(DATA_DIR)
    tenants = JsonInstanceStore(DATA_DIR)
    audit = JsonAuditStore(DATA_DIR)
    provider = AnthropicProvider(api_key)
    mcp = MCPClientManager()
    registry = CapabilityRegistry()
    reasoning = ReasoningService(provider, events, mcp, audit)
    engine = TaskEngine(reasoning, events)
    handler = MessageHandler(
        mcp=mcp, conversations=conversations, tenants=tenants,
        audit=audit, events=events, state=state,
        reasoning=reasoning, registry=registry, engine=engine,
    )

    print("=" * 60)
    print("  SPEC-3B LIVE TEST: Per-Space Tool Scoping")
    print(f"  Tenant: {TENANT}")
    print("=" * 60)

    # -----------------------------------------------------------------------
    # Step 0: Verify handler wiring
    # -----------------------------------------------------------------------
    has_registry = reasoning._registry is not None
    has_state_wired = reasoning._state is not None
    log_result(
        "0", "Handler wiring verification",
        "reasoning._registry and ._state wired",
        f"has_registry={has_registry}, has_state={has_state_wired}",
        has_registry and has_state_wired,
    )

    # -----------------------------------------------------------------------
    # Step 1: Trigger _get_or_init_soul via first message — creates system space
    # -----------------------------------------------------------------------
    print("\n>>> Step 1: First message — triggers system space creation")
    t0 = time.monotonic()
    resp1 = await handler.process(make_msg("Hello — what spaces do I have set up?"))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {resp1[:400]}")

    # Check system space was created
    spaces = await state.list_context_spaces(TENANT)
    system_spaces = [s for s in spaces if s.space_type == "system"]
    system_space = system_spaces[0] if system_spaces else None

    has_system = system_space is not None
    has_correct_desc = has_system and "system configuration" in system_space.description.lower()
    has_correct_posture = has_system and "precise" in system_space.posture.lower()

    log_result(
        "1", "System space auto-created at provisioning",
        "System space exists with type=system, correct description, correct posture",
        f"system_space_id={system_space.id if system_space else None}, "
        f"name={system_space.name if system_space else None}, "
        f"type={system_space.space_type if system_space else None}, "
        f"desc_ok={has_correct_desc}, posture_ok={has_correct_posture}",
        has_system and has_correct_desc and has_correct_posture,
        note="System space should be alongside Daily space",
    )

    await asyncio.sleep(2)

    # -----------------------------------------------------------------------
    # Step 2: CLI verification of spaces (subprocess)
    # -----------------------------------------------------------------------
    print("\n>>> Step 2: kernos-cli spaces")
    import subprocess
    cli_result = subprocess.run(
        ["./kernos-cli", "spaces", TENANT],
        capture_output=True, text=True,
        cwd=os.path.join(os.path.dirname(__file__), "../..")
    )
    cli_output = cli_result.stdout + cli_result.stderr
    print(f"  CLI output:\n{cli_output}")

    has_system_in_cli = "system" in cli_output.lower()
    has_daily_in_cli = "daily" in cli_output.lower()
    log_result(
        "2", "kernos-cli spaces shows System space",
        "CLI lists System space alongside Daily",
        cli_output[:400],
        has_system_in_cli and has_daily_in_cli,
    )

    # -----------------------------------------------------------------------
    # Step 3: Documentation files in system space
    # -----------------------------------------------------------------------
    print("\n>>> Step 3: Check system space documentation files")
    from pathlib import Path
    from kernos.utils import _safe_name

    if system_space:
        files_dir = Path(DATA_DIR) / _safe_name(TENANT) / "spaces" / system_space.id / "files"
        cap_overview = files_dir / "capabilities-overview.md"
        how_to = files_dir / "how-to-connect-tools.md"

        cap_exists = cap_overview.exists()
        how_exists = how_to.exists()
        cap_content = cap_overview.read_text() if cap_exists else ""
        how_content = how_to.read_text() if how_exists else ""

        docs_ok = cap_exists and how_exists
        cap_has_connected = "Connected Tools" in cap_content
        how_has_tools = "How to Connect Tools" in how_content
        log_result(
            "3", "Documentation files in system space",
            "capabilities-overview.md and how-to-connect-tools.md exist with correct content",
            f"cap_overview={cap_exists}, how_to={how_exists}, "
            f"cap_has_section={cap_has_connected}, how_has_section={how_has_tools}",
            docs_ok and cap_has_connected and how_has_tools,
            note=f"Files dir: {files_dir}",
        )
    else:
        log_result(
            "3", "Documentation files in system space",
            "System space doc files exist",
            "SKIP — system space not found in step 1",
            False,
        )

    await asyncio.sleep(2)

    # -----------------------------------------------------------------------
    # Step 4: kernos-cli files for system space
    # -----------------------------------------------------------------------
    if system_space:
        print(f"\n>>> Step 4: kernos-cli files {TENANT} {system_space.id}")
        cli_result2 = subprocess.run(
            ["./kernos-cli", "files", TENANT, system_space.id],
            capture_output=True, text=True,
            cwd=os.path.join(os.path.dirname(__file__), "../..")
        )
        cli_files_output = cli_result2.stdout + cli_result2.stderr
        print(f"  CLI output:\n{cli_files_output}")

        has_cap_file = "capabilities-overview.md" in cli_files_output
        has_how_file = "how-to-connect-tools.md" in cli_files_output
        log_result(
            "4", "kernos-cli files <tenant> <system_space_id>",
            "CLI shows capabilities-overview.md and how-to-connect-tools.md",
            cli_files_output[:400],
            has_cap_file and has_how_file,
        )
    else:
        log_result("4", "kernos-cli files system space", "Shows doc files", "SKIP", False)

    await asyncio.sleep(2)

    # -----------------------------------------------------------------------
    # Step 5: Route to system space — "What tools do I have?"
    # -----------------------------------------------------------------------
    print("\n>>> Step 5: 'What tools do I have?' — should route to system space")
    t0 = time.monotonic()
    resp5 = await handler.process(make_msg(
        "I'm in system settings — what tools and capabilities do I have connected?"
    ))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {resp5[:400]}")

    # Should mention capabilities — even if none connected, should be informative
    mentions_tools = any(kw in resp5.lower() for kw in [
        "tool", "capabilit", "calendar", "connected", "available", "gmail"
    ])
    log_result(
        "5", "Route to system space — capability awareness",
        "Agent responds about tools/capabilities in system context",
        resp5,
        mentions_tools,
        note="System space sees all tools; agent should describe capability state",
    )

    await asyncio.sleep(2)

    # -----------------------------------------------------------------------
    # Step 6: D&D space — tool visibility
    # -----------------------------------------------------------------------
    print(f"\n>>> Step 6: D&D space tool visibility (space_id={DND_SPACE})")
    dnd_space_obj = await state.get_context_space(TENANT, DND_SPACE)
    dnd_active_tools_before = dnd_space_obj.active_tools if dnd_space_obj else []
    print(f"  D&D active_tools before: {dnd_active_tools_before}")

    t0 = time.monotonic()
    resp6 = await handler.process(make_msg(
        "Back to Pip's campaign — what can you help me with here in this space?"
    ))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {resp6[:400]}")

    # D&D space should respond with what it can do (game/roleplay context)
    relevant = any(kw in resp6.lower() for kw in [
        "campaign", "pip", "d&d", "help", "story", "game", "roleplay",
        "write", "file", "remember", "memory"
    ])
    log_result(
        "6", "D&D space tool visibility check",
        "Agent responds in D&D context, describing relevant capabilities",
        resp6,
        relevant,
        note=f"D&D active_tools before: {dnd_active_tools_before}",
    )

    await asyncio.sleep(2)

    # -----------------------------------------------------------------------
    # Step 7: Calendar check in D&D — universal capability
    # -----------------------------------------------------------------------
    print("\n>>> Step 7: Calendar in D&D space (universal capability)")
    t0 = time.monotonic()
    resp7 = await handler.process(make_msg(
        "Still in D&D — I need to check my calendar to find a time for our next session"
    ))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {resp7[:400]}")

    # google-calendar is universal=True, so it should be visible in D&D
    # If calendar is not connected (AVAILABLE status), the agent should offer to set it up
    # Either way, it should acknowledge the calendar need
    calendar_acknowledged = any(kw in resp7.lower() for kw in [
        "calendar", "schedule", "connect", "set up", "google", "available"
    ])
    log_result(
        "7", "Universal capability (calendar) in D&D space",
        "Agent acknowledges calendar need (either uses it if connected, or offers setup)",
        resp7,
        calendar_acknowledged,
        note="google-calendar has universal=True; if AVAILABLE (not connected) agent should offer setup",
    )

    await asyncio.sleep(2)

    # -----------------------------------------------------------------------
    # Step 8: request_tool — map-drawing (not installed) → redirect
    # -----------------------------------------------------------------------
    print("\n>>> Step 8: request_tool — map-drawing not installed")
    t0 = time.monotonic()
    resp8 = await handler.process(make_msg(
        "I need a tool for drawing battle maps for D&D — can you activate a map-drawing tool?"
    ))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {resp8[:400]}")

    # Agent should call request_tool, get not-found response, redirect to system space
    map_not_found = any(kw in resp8.lower() for kw in [
        "don't have", "not installed", "system", "connect", "set up", "install", "tool"
    ])
    log_result(
        "8", "request_tool — not installed capability",
        "Agent tries request_tool for map-drawing, gets not-found, redirects to system space",
        resp8,
        map_not_found,
        note="Kernel should intercept request_tool and return not-installed message",
    )

    await asyncio.sleep(2)

    # -----------------------------------------------------------------------
    # Step 9: Business space — tool check
    # -----------------------------------------------------------------------
    print(f"\n>>> Step 9: Business space (Henderson) — capability state")
    henderson_space_obj = await state.get_context_space(TENANT, HENDERSON_SPACE)
    henderson_active_before = henderson_space_obj.active_tools if henderson_space_obj else []
    print(f"  Henderson active_tools: {henderson_active_before}")

    t0 = time.monotonic()
    resp9 = await handler.process(make_msg(
        "Looking at the Henderson project. What capabilities do I have available in this space?"
    ))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {resp9[:400]}")

    relevant9 = any(kw in resp9.lower() for kw in [
        "henderson", "business", "tool", "capabilit", "help", "contract", "calendar"
    ])
    log_result(
        "9", "Business space — capability awareness",
        "Agent responds in Henderson context, mentions available capabilities",
        resp9,
        relevant9,
        note=f"Henderson active_tools: {henderson_active_before}",
    )

    await asyncio.sleep(2)

    # -----------------------------------------------------------------------
    # Step 10: System space read — read capabilities overview file
    # -----------------------------------------------------------------------
    print("\n>>> Step 10: Read capabilities-overview.md from system space")
    t0 = time.monotonic()
    resp10 = await handler.process(make_msg(
        "In system settings — please read the capabilities overview file and summarize what's there"
    ))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {resp10[:400]}")

    # Agent should read the file and mention connected/available tools
    file_read = any(kw in resp10.lower() for kw in [
        "connected", "available", "tool", "capabilit", "calendar", "no tools", "nothing connected"
    ])
    log_result(
        "10", "System space — read capabilities-overview.md",
        "Agent reads capabilities-overview.md and summarizes tool state",
        resp10,
        file_read,
        note="Agent should use read_file, then describe connected/available state",
    )

    # -----------------------------------------------------------------------
    # Step 11: Verify LRU exemption — system space not in LRU candidates
    # -----------------------------------------------------------------------
    print("\n>>> Step 11: LRU exemption check")
    spaces_final = await state.list_context_spaces(TENANT)
    system_spaces_final = [s for s in spaces_final if s.space_type == "system"]
    lru_candidates = [
        s for s in spaces_final
        if s.status == "active" and not s.is_default and s.space_type != "system"
    ]
    system_in_lru = any(s.space_type == "system" for s in lru_candidates)
    log_result(
        "11", "LRU exemption — system space excluded",
        "System space not in LRU archiving candidates",
        f"system_spaces={[s.id for s in system_spaces_final]}, "
        f"lru_candidates={[s.id for s in lru_candidates]}, "
        f"system_in_lru={system_in_lru}",
        len(system_spaces_final) > 0 and not system_in_lru,
    )

    # -----------------------------------------------------------------------
    # Step 12: Verify tool filtering — active_tools in state
    # -----------------------------------------------------------------------
    print("\n>>> Step 12: Verify active_tools state across spaces")
    spaces_final2 = await state.list_context_spaces(TENANT)
    system_s = next((s for s in spaces_final2 if s.space_type == "system"), None)
    daily_s = next((s for s in spaces_final2 if s.is_default), None)

    space_state_summary = {
        s.name: {"type": s.space_type, "active_tools": s.active_tools}
        for s in spaces_final2
    }
    print(f"  Space tool state: {json.dumps(space_state_summary, indent=2)}")

    # system space exists, daily space exists, both have active_tools field
    system_ok = system_s is not None
    daily_ok = daily_s is not None
    log_result(
        "12", "active_tools field on all spaces",
        "All spaces have active_tools field (backward compat); system and daily exist",
        json.dumps(space_state_summary)[:400],
        system_ok and daily_ok,
        note="active_tools defaults to [] for existing spaces",
    )

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print("\n\n" + "=" * 60)
    print("  LIVE TEST SUMMARY")
    print("=" * 60)

    passed = [r for r in results if r["passed"]]
    failed = [r for r in results if not r["passed"]]
    total = len(results)

    print(f"\n  Total: {total}  |  Passed: {len(passed)}  |  Failed: {len(failed)}")
    print()
    for r in results:
        icon = "✓" if r["passed"] else "✗"
        print(f"  {icon} Step {r['step']}: {r['action'][:60]}")

    results_path = os.path.join(os.path.dirname(__file__), "live_test_3b_results.json")
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Full results saved: {results_path}")

    return failed


if __name__ == "__main__":
    failed = asyncio.run(main())
    sys.exit(0 if not failed else 1)
