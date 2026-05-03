#!/usr/bin/env python3
"""Live test harness for SPEC-2C: Context Space Compaction.

Direct handler invocation — no Discord required.
Sends messages through the handler and inspects compaction state after each exchange.
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

# Ensure project root is on path
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


async def setup():
    """Create handler with real Anthropic provider."""
    events = JsonEventStream(DATA_DIR)
    state = JsonStateStore(DATA_DIR)
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    provider = AnthropicProvider(api_key)
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
    return handler, state


async def get_compaction_state(space_id: str):
    from kernos.kernel.compaction import CompactionService
    from kernos.kernel.tokens import EstimateTokenAdapter
    from kernos.kernel.state_json import JsonStateStore
    service = CompactionService(
        state=JsonStateStore(DATA_DIR), reasoning=None,
        token_adapter=EstimateTokenAdapter(), data_dir=DATA_DIR,
    )
    return await service.load_state(TENANT, space_id)


async def get_active_doc(space_id: str):
    from kernos.kernel.compaction import CompactionService
    from kernos.kernel.tokens import EstimateTokenAdapter
    from kernos.kernel.state_json import JsonStateStore
    service = CompactionService(
        state=JsonStateStore(DATA_DIR), reasoning=None,
        token_adapter=EstimateTokenAdapter(), data_dir=DATA_DIR,
    )
    return await service.load_document(TENANT, space_id)


def print_section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_compaction(cs, label=""):
    if cs is None:
        print(f"  {label}: [no compaction state]")
        return
    print(f"  {label}")
    print(f"    compaction_number: {cs.compaction_number} (global: {cs.global_compaction_number})")
    print(f"    cumulative_new_tokens: {cs.cumulative_new_tokens} / ceiling: {cs.message_ceiling}")
    print(f"    history_tokens: {cs.history_tokens}")
    print(f"    archive_count: {cs.archive_count}")
    if cs.last_compaction_at:
        print(f"    last_compaction_at: {cs.last_compaction_at[:19]}")


async def send_and_log(handler, content, step_num):
    """Send a message and return the response."""
    print(f"\n  [{step_num}] User: {content[:80]}{'...' if len(content) > 80 else ''}")
    t0 = time.monotonic()
    response = await handler.process(make_msg(content))
    dur = time.monotonic() - t0
    print(f"       Agent: {response[:120]}{'...' if len(response) > 120 else ''}")
    print(f"       ({dur:.1f}s)")
    return response


async def main():
    handler, state = await setup()

    # ================================================================
    # PHASE 1: D&D Space — First Compaction
    # ================================================================
    print_section("PHASE 1: D&D Space — Build up to first compaction")

    dnd_messages = [
        "Hey, I want to continue our D&D campaign with Pip the rogue. Where were we?",
        "Right, the Ashen Veil mystery in Tidemark. Let's have Pip explore the docks district at night.",
        "Pip tries to pick the lock on the warehouse door. What's the DC?",
        "I roll a 17 plus my +7 for thieves tools. That's a 24 total.",
        "Pip sneaks inside. What does he see? Any guards or traps?",
        "Pip uses his darkvision to scan the room. He's looking for anything connected to the Ashen Veil.",
        "Does Pip find any documents or letters? He searches the desk carefully.",
        "Interesting — so there's a letter from someone named Captain Thorne. What does it say?",
        "Pip pockets the letter and the vial of strange powder. He needs to show these to Mara at the tavern.",
        "Before leaving, Pip checks if there's a hidden compartment in the desk. Investigation check — I got a 15.",
    ]

    cs = await get_compaction_state(DND_SPACE)
    print_compaction(cs, "D&D Pre-test state")

    for i, msg in enumerate(dnd_messages, 1):
        await send_and_log(handler, msg, i)
        # Check state periodically
        if i % 3 == 0 or i == len(dnd_messages):
            cs = await get_compaction_state(DND_SPACE)
            print_compaction(cs, f"  D&D after message {i}")

    # Check if compaction fired
    cs = await get_compaction_state(DND_SPACE)
    doc = await get_active_doc(DND_SPACE)

    print_section("PHASE 1 RESULTS")
    print_compaction(cs, "D&D Final state")
    if doc:
        print(f"\n  Active document exists: {len(doc)} chars")
        print(f"  --- First 500 chars ---")
        print(doc[:500])
        print(f"  --- Last 300 chars ---")
        print(doc[-300:])
    else:
        print("  NO active document — compaction has not fired yet")

    if not cs or cs.compaction_number == 0:
        print("\n  ⚠ Compaction hasn't fired yet. Sending more messages...")
        extra = [
            "Pip finds a hidden compartment! What's inside?",
            "A map of the sewers under Tidemark — this is huge. Pip memorizes the route to the Ashen Veil hideout.",
            "Pip carefully replaces everything and sneaks out. He heads to the Rusty Anchor tavern to find Mara.",
            "Pip shows Mara the letter from Captain Thorne and the map. What does she think?",
            "Mara says we need to recruit Grimjaw the dwarf fighter for the sewer mission. Where do we find him?",
        ]
        for i, msg in enumerate(extra, len(dnd_messages) + 1):
            await send_and_log(handler, msg, i)

        cs = await get_compaction_state(DND_SPACE)
        doc = await get_active_doc(DND_SPACE)
        print_compaction(cs, "D&D after extra messages")
        if doc:
            print(f"\n  Active document: {len(doc)} chars")
            print(doc[:500])

    # ================================================================
    # PHASE 2: D&D Space — Second Compaction
    # ================================================================
    print_section("PHASE 2: D&D Space — Second compaction")

    # Save first doc for byte comparison
    first_doc = await get_active_doc(DND_SPACE)
    first_doc_ledger1 = ""
    if first_doc:
        import re
        m = re.search(r'(## Compaction #1.*?)(?=## Compaction #|# Living State)', first_doc, re.DOTALL)
        if m:
            first_doc_ledger1 = m.group(1)
            print(f"  Captured Ledger #1 for comparison ({len(first_doc_ledger1)} chars)")

    phase2_messages = [
        "Next session: Pip and Grimjaw enter the sewers following the map from the warehouse.",
        "The sewer tunnels are dark and the water is knee-deep. Pip leads with his darkvision.",
        "We encounter a group of ratfolk. Are they hostile or can we negotiate?",
        "Pip tries to persuade the ratfolk that we're here to stop the Ashen Veil, not them. Persuasion: 16.",
        "Great, the ratfolk agree to let us pass. They warn us about a trap ahead — pressure plates.",
        "Pip uses his trap-finding skills. I want to disable the pressure plates. Thieves' tools: natural 20!",
        "Beyond the trap, we find the Ashen Veil's underground laboratory. What do we see?",
        "Pip recognizes the alchemical equipment. This must be where they're making the poison. He takes samples.",
        "We need to find evidence that links Captain Thorne to this operation. Pip searches the lab records.",
        "Pip finds a ledger with payments from Thorne to someone called 'The Architect'. Who is that?",
    ]

    for i, msg in enumerate(phase2_messages, 1):
        await send_and_log(handler, msg, i)
        if i % 4 == 0 or i == len(phase2_messages):
            cs = await get_compaction_state(DND_SPACE)
            print_compaction(cs, f"  D&D Phase 2 after msg {i}")

    cs = await get_compaction_state(DND_SPACE)
    doc = await get_active_doc(DND_SPACE)

    print_section("PHASE 2 RESULTS")
    print_compaction(cs, "D&D after Phase 2")

    if doc:
        print(f"\n  Active document: {len(doc)} chars")
        # Check if Ledger #1 is preserved
        import re
        m = re.search(r'(## Compaction #1.*?)(?=## Compaction #|# Living State)', doc, re.DOTALL)
        if m:
            current_ledger1 = m.group(1)
            if first_doc_ledger1 and current_ledger1 == first_doc_ledger1:
                print("  ✅ Ledger #1 is BYTE-IDENTICAL to first compaction")
            elif first_doc_ledger1:
                print("  ❌ Ledger #1 has CHANGED since first compaction!")
                print(f"     First: {first_doc_ledger1[:200]}")
                print(f"     Now:   {current_ledger1[:200]}")
            else:
                print("  ℹ No first doc ledger to compare (compaction may not have fired in phase 1)")

        if "Compaction #2" in doc:
            print("  ✅ Compaction #2 appended")
        else:
            print("  ⚠ Compaction #2 NOT found in document")

        print(f"\n  --- Full document ---")
        print(doc)

    # ================================================================
    # PHASE 3: Historical Context Query
    # ================================================================
    print_section("PHASE 3: Historical context query")

    response = await send_and_log(
        handler,
        "What were we doing in the campaign? Remind me about the warehouse and Captain Thorne.",
        1,
    )
    print(f"\n  Full response:\n  {response}")

    # Check if response references compaction-era content
    checks = {
        "warehouse": "warehouse" in response.lower(),
        "Thorne": "thorne" in response.lower(),
        "Pip": "pip" in response.lower(),
        "Ashen Veil": "ashen" in response.lower(),
    }
    for key, found in checks.items():
        print(f"  {'✅' if found else '❌'} References '{key}': {found}")

    # ================================================================
    # PHASE 4: Daily Space
    # ================================================================
    print_section("PHASE 4: Daily Space — separate compaction tracking")

    cs_daily = await get_compaction_state(DAILY_SPACE)
    print_compaction(cs_daily, "Daily Pre-test state")

    daily_messages = [
        "Hey, I need to schedule a dentist appointment for next Tuesday at 2pm.",
        "Also remind me to buy groceries — we need milk, eggs, bread, and olive oil.",
        "What's the weather supposed to be like this weekend? I'm thinking about a hike.",
        "Oh, and I got an email from Sarah Henderson about the contract amendment. Can you help me draft a reply?",
        "The reply should say we agree to the timeline but want to negotiate the penalty clause. Keep it professional.",
        "Thanks. Also, Liana's birthday is coming up on March 25th. I need gift ideas — she's into pottery and Italian cooking.",
        "Those are great ideas. Let me think about it. What else do I have going on this week?",
        "Actually, one more thing — I need to call Greg about the weekend hiking plan. His number is in my contacts.",
    ]

    for i, msg in enumerate(daily_messages, 1):
        await send_and_log(handler, msg, i)
        if i % 3 == 0 or i == len(daily_messages):
            cs_daily = await get_compaction_state(DAILY_SPACE)
            print_compaction(cs_daily, f"  Daily after message {i}")

    cs_daily = await get_compaction_state(DAILY_SPACE)
    doc_daily = await get_active_doc(DAILY_SPACE)

    print_section("PHASE 4 RESULTS")
    print_compaction(cs_daily, "Daily Final state")

    if doc_daily:
        print(f"\n  Daily active document: {len(doc_daily)} chars")
        print(f"\n  --- Full document ---")
        print(doc_daily)
    else:
        print("  Daily has no active document yet")

    # ================================================================
    # PHASE 5: Compare editorial character
    # ================================================================
    print_section("PHASE 5: Editorial character comparison")

    doc_dnd = await get_active_doc(DND_SPACE)
    if doc_dnd and doc_daily:
        print("  D&D document excerpt (first 400 chars after Ledger header):")
        print(f"  {doc_dnd[doc_dnd.find('## Compaction'):doc_dnd.find('## Compaction')+400] if '## Compaction' in doc_dnd else doc_dnd[:400]}")
        print()
        print("  Daily document excerpt (first 400 chars after Ledger header):")
        print(f"  {doc_daily[doc_daily.find('## Compaction'):doc_daily.find('## Compaction')+400] if '## Compaction' in doc_daily else doc_daily[:400]}")
    elif doc_dnd:
        print("  Only D&D document exists. Daily hasn't compacted yet.")
    else:
        print("  Neither document exists yet.")

    # ================================================================
    # PHASE 6: Final CLI state dump
    # ================================================================
    print_section("PHASE 6: Final compaction state (CLI-equivalent)")

    for space_id, name in [(DND_SPACE, "D&D"), (DAILY_SPACE, "Daily")]:
        cs = await get_compaction_state(space_id)
        print_compaction(cs, f"\n  {name} ({space_id})")
        doc = await get_active_doc(space_id)
        if doc:
            lines = doc.splitlines()
            print(f"    document: {len(lines)} lines, {len(doc)} chars")

    print("\n\n  ✅ Live test complete.")


if __name__ == "__main__":
    asyncio.run(main())
