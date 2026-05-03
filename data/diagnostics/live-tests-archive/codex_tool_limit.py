#!/usr/bin/env python3
"""Binary search for Codex API tool limit.

Reproduces the failing request shape and systematically isolates
whether the issue is tool count, specific tool schemas, or payload size.
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

# Manual .env loading (dotenv has issues in scripts)
env_path = os.path.join(os.path.dirname(__file__), "../../.env")
if os.path.exists(env_path):
    for line in open(env_path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import logging
logging.basicConfig(level=logging.WARNING, format="%(message)s")

from kernos.kernel.credentials import resolve_openai_codex_credential
from kernos.providers.codex_provider import OpenAICodexProvider

# Collect ALL kernel tool schemas
from kernos.kernel.files import FILE_TOOLS
from kernos.kernel.reasoning import REQUEST_TOOL, READ_DOC_TOOL, REMEMBER_DETAILS_TOOL, MANAGE_CAPABILITIES_TOOL
from kernos.kernel.awareness import DISMISS_WHISPER_TOOL
from kernos.kernel.reasoning import READ_SOURCE_TOOL, READ_SOUL_TOOL, UPDATE_SOUL_TOOL
from kernos.kernel.covenant_manager import MANAGE_COVENANTS_TOOL
from kernos.kernel.channels import MANAGE_CHANNELS_TOOL, SEND_TO_CHANNEL_TOOL
from kernos.kernel.scheduler import MANAGE_SCHEDULE_TOOL
from kernos.kernel.tools import INSPECT_STATE_TOOL
from kernos.kernel.code_exec import EXECUTE_CODE_TOOL
from kernos.kernel.workspace import MANAGE_WORKSPACE_TOOL, REGISTER_TOOL_TOOL

ALL_KERNEL = FILE_TOOLS + [REQUEST_TOOL, READ_DOC_TOOL, DISMISS_WHISPER_TOOL,
    MANAGE_CAPABILITIES_TOOL, REMEMBER_DETAILS_TOOL,
    READ_SOURCE_TOOL, READ_SOUL_TOOL, UPDATE_SOUL_TOOL,
    MANAGE_COVENANTS_TOOL, MANAGE_CHANNELS_TOOL,
    SEND_TO_CHANNEL_TOOL, MANAGE_SCHEDULE_TOOL,
    INSPECT_STATE_TOOL, EXECUTE_CODE_TOOL,
    MANAGE_WORKSPACE_TOOL, REGISTER_TOOL_TOOL]

# Simulated MCP calendar tools (realistic schema sizes)
def _mcp_tool(name, desc, props, required):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": required}}

MCP_CALENDAR = [
    _mcp_tool("list-events", "List calendar events", {"account":{"type":"string"},"calendarId":{"type":"string"},"timeMin":{"type":"string"},"timeMax":{"type":"string"},"maxResults":{"type":"integer"},"orderBy":{"type":"string"},"singleEvents":{"type":"boolean"},"q":{"type":"string"}}, ["account","calendarId"]),
    _mcp_tool("search-events", "Search calendar events", {"account":{"type":"string"},"calendarId":{"type":"string"},"q":{"type":"string"},"timeMin":{"type":"string"},"timeMax":{"type":"string"},"maxResults":{"type":"integer"}}, ["account","calendarId","q"]),
    _mcp_tool("create-event", "Create calendar event", {"account":{"type":"string"},"calendarId":{"type":"string"},"summary":{"type":"string"},"description":{"type":"string"},"start":{"type":"string"},"end":{"type":"string"},"timeZone":{"type":"string"},"location":{"type":"string"},"sendUpdates":{"type":"string","enum":["all","externalOnly","none"]}}, ["account","calendarId","summary","start","end","timeZone"]),
    _mcp_tool("create-events", "Create multiple events", {"account":{"type":"string"},"calendarId":{"type":"string"},"events":{"type":"array","items":{"type":"object"}}}, ["account","calendarId","events"]),
    _mcp_tool("update-event", "Update calendar event", {"account":{"type":"string"},"calendarId":{"type":"string"},"eventId":{"type":"string"},"summary":{"type":"string"},"description":{"type":"string"},"start":{"type":"string"},"end":{"type":"string"},"timeZone":{"type":"string"},"location":{"type":"string"},"sendUpdates":{"type":"string","enum":["all","externalOnly","none"]},"status":{"type":"string","enum":["confirmed","tentative","cancelled"]}}, ["account","calendarId","eventId"]),
    _mcp_tool("delete-event", "Delete calendar event", {"account":{"type":"string"},"calendarId":{"type":"string"},"eventId":{"type":"string"},"sendUpdates":{"type":"string","enum":["all","externalOnly","none"]}}, ["account","calendarId","eventId"]),
    _mcp_tool("get-event", "Get event details", {"account":{"type":"string"},"calendarId":{"type":"string"},"eventId":{"type":"string"}}, ["account","calendarId","eventId"]),
    _mcp_tool("get-freebusy", "Check free/busy", {"account":{"type":"string"},"timeMin":{"type":"string"},"timeMax":{"type":"string"}}, ["account","timeMin","timeMax"]),
    _mcp_tool("list-calendars", "List calendars", {"account":{"type":"string"}}, ["account"]),
    _mcp_tool("get-current-time", "Get current time", {"account":{"type":"string"},"timeZone":{"type":"string"}}, ["account"]),
    _mcp_tool("respond-to-event", "RSVP to event", {"account":{"type":"string"},"calendarId":{"type":"string"},"eventId":{"type":"string"},"response":{"type":"string","enum":["accepted","declined","tentative"]}}, ["account","calendarId","eventId","response"]),
]

MCP_SEARCH = [
    _mcp_tool("brave_web_search", "Search the web", {"query":{"type":"string"},"count":{"type":"integer"}}, ["query"]),
    _mcp_tool("brave_local_search", "Search local businesses", {"query":{"type":"string"},"count":{"type":"integer"}}, ["query"]),
]

ALL_TOOLS = ALL_KERNEL + MCP_CALENDAR + MCP_SEARCH

# Build realistic context (~15K tokens)
SYSTEM = "You are a helpful personal assistant. " * 40
MESSAGES = []
for i in range(12):
    MESSAGES.append({"role": "user", "content": f"User message {i}: " + "conversation context " * 60})
    MESSAGES.append({"role": "assistant", "content": f"Assistant response {i}: " + "helpful details " * 60})
MESSAGES.append({"role": "user", "content": "Register the invoice tracker tool please."})


async def test_tools(provider, tool_set, label):
    translated = OpenAICodexProvider._translate_tools(tool_set)
    payload_bytes = len(json.dumps({
        "model": provider.main_model,
        "instructions": SYSTEM,
        "input": OpenAICodexProvider._translate_input(MESSAGES),
        "max_output_tokens": 64000,
        "tools": translated,
    }))
    try:
        t0 = time.time()
        resp = await provider.complete(
            model=provider.main_model,
            system=SYSTEM, messages=MESSAGES,
            tools=tool_set, max_tokens=64000,
        )
        dur = time.time() - t0
        text = resp.content[0].text[:30] if resp.content else ""
        print(f"  {label:30s} → SUCCESS  {len(tool_set):2d} tools  {payload_bytes//1024:3d}KB  {dur:.1f}s  {repr(text)}")
        return True
    except Exception as exc:
        ename = type(exc).__name__
        print(f"  {label:30s} → FAILED   {len(tool_set):2d} tools  {payload_bytes//1024:3d}KB  {ename}")
        return False


async def main():
    provider = OpenAICodexProvider(credential=resolve_openai_codex_credential())

    print(f"Tools inventory: {len(ALL_TOOLS)} total ({len(ALL_KERNEL)} kernel + {len(MCP_CALENDAR)} calendar + {len(MCP_SEARCH)} search)")
    print(f"Message context: {len(MESSAGES)} messages")
    print()

    # Phase 1: Test the full set and subsets
    print("=== PHASE 1: Full set vs halves ===")
    full_ok = await test_tools(provider, ALL_TOOLS, "ALL tools")

    first_half = ALL_TOOLS[:len(ALL_TOOLS)//2]
    second_half = ALL_TOOLS[len(ALL_TOOLS)//2:]
    await test_tools(provider, first_half, "First half")
    await test_tools(provider, second_half, "Second half")

    # Phase 2: Kernel only vs MCP only
    print("\n=== PHASE 2: Kernel vs MCP ===")
    await test_tools(provider, ALL_KERNEL, "Kernel only (20)")
    await test_tools(provider, MCP_CALENDAR, "Calendar only (11)")
    await test_tools(provider, MCP_SEARCH, "Search only (2)")
    await test_tools(provider, ALL_KERNEL + MCP_SEARCH, "Kernel+Search (22)")
    await test_tools(provider, ALL_KERNEL + MCP_CALENDAR, "Kernel+Calendar (31)")

    # Phase 3: Find the exact breaking point
    print("\n=== PHASE 3: Incrementing from kernel baseline ===")
    for i in range(len(MCP_CALENDAR) + 1):
        subset = ALL_KERNEL + MCP_CALENDAR[:i] + MCP_SEARCH
        ok = await test_tools(provider, subset, f"Kernel + {i} calendar + search")
        if not ok:
            # Found it — test without search to isolate
            print(f"\n  Breaking point: adding calendar tool #{i} ({MCP_CALENDAR[i-1]['name'] if i > 0 else 'none'})")
            if i > 0:
                # Test: is it this specific tool or just the count?
                alt = ALL_KERNEL + MCP_CALENDAR[i:i+1] + MCP_SEARCH  # just the failing tool + kernel
                await test_tools(provider, alt, f"Kernel + ONLY {MCP_CALENDAR[i-1]['name']} + search")
            break

    # Phase 4: Payload size test (same tools, smaller context)
    print("\n=== PHASE 4: Full tools, smaller context ===")
    small_msgs = MESSAGES[-5:]  # just last 5 messages
    try:
        t0 = time.time()
        resp = await provider.complete(
            model=provider.main_model,
            system=SYSTEM, messages=small_msgs,
            tools=ALL_TOOLS, max_tokens=64000,
        )
        dur = time.time() - t0
        print(f"  Full tools + small context → SUCCESS ({dur:.1f}s)")
    except Exception as exc:
        print(f"  Full tools + small context → FAILED ({type(exc).__name__})")


if __name__ == "__main__":
    asyncio.run(main())
