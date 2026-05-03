#!/usr/bin/env python3
"""Phase 2 continuation: second D&D compaction + Daily first compaction."""
import asyncio
import json
import os
import re
import sys
import time

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
CONVERSATION_ID = "live_test_2c"
DND_SPACE = "space_fbdace10"
DAILY_SPACE = "space_5b632b42"


def make_msg(content):
    return NormalizedMessage(
        content=content, sender="000000000000000000",
        sender_auth_level=AuthLevel.owner_verified, platform="discord",
        platform_capabilities=["text"], conversation_id=CONVERSATION_ID,
        timestamp=datetime.now(timezone.utc), instance_id=TENANT,
    )


async def setup():
    events = JsonEventStream(DATA_DIR)
    state = JsonStateStore(DATA_DIR)
    provider = AnthropicProvider(os.getenv("ANTHROPIC_API_KEY", ""))
    mcp = MCPClientManager(events=events)
    audit = JsonAuditStore(DATA_DIR)
    reasoning = ReasoningService(provider, events, mcp, audit)
    registry = CapabilityRegistry()
    engine = TaskEngine(reasoning, events)
    conversations = JsonConversationStore(DATA_DIR)
    tenants = JsonInstanceStore(DATA_DIR)
    handler = MessageHandler(
        mcp=mcp, conversations=conversations, tenants=tenants,
        audit=audit, events=events, state=state,
        reasoning=reasoning, registry=registry, engine=engine,
    )
    return handler


async def get_cs(space_id):
    from kernos.kernel.compaction import CompactionService
    from kernos.kernel.tokens import EstimateTokenAdapter
    service = CompactionService(
        state=JsonStateStore(DATA_DIR), reasoning=None,
        token_adapter=EstimateTokenAdapter(), data_dir=DATA_DIR,
    )
    return await service.load_state(TENANT, space_id)


async def get_doc(space_id):
    from kernos.kernel.compaction import CompactionService
    from kernos.kernel.tokens import EstimateTokenAdapter
    service = CompactionService(
        state=JsonStateStore(DATA_DIR), reasoning=None,
        token_adapter=EstimateTokenAdapter(), data_dir=DATA_DIR,
    )
    return await service.load_document(TENANT, space_id)


def print_section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_cs(cs, label=""):
    if cs is None:
        print(f"  {label}: [no state]")
        return
    print(f"  {label}")
    print(f"    compaction: {cs.compaction_number} (global: {cs.global_compaction_number})")
    print(f"    tokens: {cs.cumulative_new_tokens} / ceiling: {cs.message_ceiling}")
    print(f"    history: {cs.history_tokens}  archives: {cs.archive_count}")
    if cs.last_compaction_at:
        print(f"    last: {cs.last_compaction_at[:19]}")


async def send(handler, content, n):
    print(f"\n  [{n}] User: {content[:80]}{'...' if len(content) > 80 else ''}")
    t0 = time.monotonic()
    resp = await handler.process(make_msg(content))
    print(f"       Agent: {resp[:120]}{'...' if len(resp) > 120 else ''}")
    print(f"       ({time.monotonic()-t0:.1f}s)")
    return resp


async def main():
    handler = await setup()

    # Save Ledger #1 from current doc for byte comparison
    doc_before = await get_doc(DND_SPACE)
    ledger1_before = ""
    if doc_before:
        m = re.search(r'(## Compaction #1.*?)(?=## Compaction #\d|# Living State)', doc_before, re.DOTALL)
        if m:
            ledger1_before = m.group(1)
            print(f"  Captured Ledger #1 for byte comparison: {len(ledger1_before)} chars")

    # ================================================================
    # D&D: Trigger second compaction
    # ================================================================
    print_section("D&D: Trigger second compaction (ceiling re-lowered to 3000)")

    cs = await get_cs(DND_SPACE)
    print_cs(cs, "D&D before")

    dnd_msgs = [
        "OK let's deal with this guard. Pip attempts to talk his way past — he says he's a city inspector checking for contraband.",
        "That didn't work. Pip throws a smoke bomb and dashes past the guard. Acrobatics check: 18.",
    ]

    for i, msg in enumerate(dnd_msgs, 1):
        await send(handler, msg, i)
        cs = await get_cs(DND_SPACE)
        print_cs(cs, f"  after msg {i}")

    doc_after = await get_doc(DND_SPACE)
    print_section("D&D SECOND COMPACTION RESULTS")
    cs = await get_cs(DND_SPACE)
    print_cs(cs, "D&D final")

    if doc_after:
        # Check Ledger #1 byte-identical
        m = re.search(r'(## Compaction #1.*?)(?=## Compaction #\d|# Living State)', doc_after, re.DOTALL)
        if m:
            ledger1_after = m.group(1)
            if ledger1_before and ledger1_after == ledger1_before:
                print("\n  ✅ LEDGER #1 IS BYTE-IDENTICAL after second compaction")
            elif ledger1_before:
                print("\n  ❌ LEDGER #1 CHANGED after second compaction")
                # Show diff
                for i, (a, b) in enumerate(zip(ledger1_before, ledger1_after)):
                    if a != b:
                        print(f"     First difference at char {i}: '{a}' vs '{b}'")
                        print(f"     Context before: ...{ledger1_before[max(0,i-20):i]}|{ledger1_before[i:i+20]}...")
                        print(f"     Context after:  ...{ledger1_after[max(0,i-20):i]}|{ledger1_after[i:i+20]}...")
                        break
            else:
                print("  ℹ No Ledger #1 from before to compare")

        has_c2 = "Compaction #2" in doc_after
        print(f"  {'✅' if has_c2 else '❌'} Compaction #2 present: {has_c2}")

        print(f"\n  --- Full document ({len(doc_after)} chars) ---")
        print(doc_after)

    # ================================================================
    # Daily: Trigger first compaction
    # ================================================================
    print_section("Daily: Trigger first compaction (ceiling lowered to 800)")

    cs_d = await get_cs(DAILY_SPACE)
    print_cs(cs_d, "Daily before")

    daily_msgs = [
        "Can you summarize what I need to do this week? The dentist, groceries, Liana's birthday gift, and calling Greg.",
        "Also I realized I need to respond to the Henderson contract email by Friday. Add that to the list.",
    ]

    for i, msg in enumerate(daily_msgs, 1):
        await send(handler, msg, i)
        cs_d = await get_cs(DAILY_SPACE)
        print_cs(cs_d, f"  after msg {i}")

    doc_daily = await get_doc(DAILY_SPACE)
    print_section("DAILY COMPACTION RESULTS")
    cs_d = await get_cs(DAILY_SPACE)
    print_cs(cs_d, "Daily final")

    if doc_daily:
        print(f"\n  --- Full document ({len(doc_daily)} chars) ---")
        print(doc_daily)
    else:
        print("  Daily still no document")

    # ================================================================
    # Editorial comparison
    # ================================================================
    print_section("EDITORIAL CHARACTER COMPARISON")
    doc_dnd = await get_doc(DND_SPACE)
    if doc_dnd and doc_daily:
        # Extract first ledger entry from each
        dnd_entry = ""
        m = re.search(r'(## Compaction #1[^\n]*\n.*?)(?=## Compaction #\d|# Living State)', doc_dnd, re.DOTALL)
        if m:
            dnd_entry = m.group(1).strip()[:600]

        daily_entry = ""
        m = re.search(r'(## Compaction #1[^\n]*\n.*?)(?=## Compaction #\d|# Living State)', doc_daily, re.DOTALL)
        if m:
            daily_entry = m.group(1).strip()[:600]

        print("  D&D Ledger #1 (first 600 chars):")
        print(f"  {dnd_entry}")
        print()
        print("  Daily Ledger #1 (first 600 chars):")
        print(f"  {daily_entry}")
    else:
        which = "D&D" if not doc_dnd else "Daily"
        print(f"  {which} document not yet available for comparison")

    # ================================================================
    # Final state dump
    # ================================================================
    print_section("FINAL STATE DUMP")
    for sid, name in [(DND_SPACE, "D&D"), (DAILY_SPACE, "Daily")]:
        cs = await get_cs(sid)
        print_cs(cs, f"\n  {name} ({sid})")

    print("\n\n  ✅ Phase 2 live test complete.")


if __name__ == "__main__":
    asyncio.run(main())
