#!/usr/bin/env python3
"""Live test: Verify compaction converts relative dates to absolute dates.

Run with low threshold to trigger compaction quickly:
    KERNOS_COMPACTION_THRESHOLD=500 python tests/live/run_compaction_dates_live.py

Expected: Ledger and Living State contain absolute dates, not "yesterday" or "last week".
"""
import asyncio
import logging
import os
import re
import sys
import time

logging.basicConfig(level=logging.INFO, format='%(name)s %(message)s')

from dotenv import load_dotenv
load_dotenv()

# Force low threshold
os.environ["KERNOS_COMPACTION_THRESHOLD"] = "500"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from datetime import datetime, timezone
from kernos.messages.handler import MessageHandler
from kernos.messages.models import NormalizedMessage, AuthLevel
from kernos.capability.client import MCPClientManager
from kernos.capability.registry import CapabilityRegistry
from kernos.kernel.engine import TaskEngine
from kernos.kernel.events import JsonEventStream
from kernos.kernel.reasoning import AnthropicProvider, ReasoningService
from kernos.kernel.state_json import JsonStateStore
from kernos.persistence.json_file import JsonAuditStore, JsonConversationStore, JsonInstanceStore

DATA_DIR = os.getenv("KERNOS_DATA_DIR", "./data")
TENANT = "discord:000000000000000000"
CONVERSATION_ID = "live_test_dates"


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
    return handler, state


async def get_active_doc(space_id: str):
    from kernos.kernel.compaction import CompactionService
    from kernos.kernel.tokens import EstimateTokenAdapter
    service = CompactionService(
        state=JsonStateStore(DATA_DIR), reasoning=None,
        token_adapter=EstimateTokenAdapter(), data_dir=DATA_DIR,
    )
    return await service.load_document(TENANT, space_id)


async def get_compaction_state(space_id: str):
    from kernos.kernel.compaction import CompactionService
    from kernos.kernel.tokens import EstimateTokenAdapter
    service = CompactionService(
        state=JsonStateStore(DATA_DIR), reasoning=None,
        token_adapter=EstimateTokenAdapter(), data_dir=DATA_DIR,
    )
    return await service.load_state(TENANT, space_id)


async def main():
    handler, state = await setup()

    # Discover the daily space
    soul = await state.get_soul(TENANT)
    daily_space = None
    for sp in (soul.spaces if soul else []):
        if sp.id.startswith("space_") and sp.definition and "daily" in sp.definition.lower():
            daily_space = sp.id
            break
    if not daily_space and soul and soul.spaces:
        daily_space = soul.spaces[0].id
    print(f"Using space: {daily_space}")

    # Messages with relative date references
    messages = [
        "I talked to Alex yesterday about the project timeline. He said the deadline moved.",
        "Last week I had a meeting with Sarah about the budget review — she approved the new numbers.",
        "Two days ago I submitted the design doc to the review committee.",
        "Earlier today I got an email from Marcus confirming the venue for next Friday's offsite.",
        "Oh and the day before yesterday I ran into Tom at the coffee shop, he's joining our team next month.",
    ]

    print("\n=== Sending messages with relative date references ===")
    for i, msg in enumerate(messages, 1):
        print(f"\n  [{i}] User: {msg}")
        t0 = time.monotonic()
        response = await handler.process(make_msg(msg))
        dur = time.monotonic() - t0
        print(f"       Agent: {response[:120]}{'...' if len(response) > 120 else ''}")
        print(f"       ({dur:.1f}s)")

    # Check compaction
    if daily_space:
        cs = await get_compaction_state(daily_space)
        doc = await get_active_doc(daily_space)

        print("\n=== COMPACTION RESULTS ===")
        if cs:
            print(f"  compaction_number: {cs.compaction_number}")
            print(f"  cumulative_new_tokens: {cs.cumulative_new_tokens}")
        else:
            print("  No compaction state yet")

        if doc:
            print(f"\n  Active document ({len(doc)} chars):")
            print(doc)

            # Verify: check for relative date terms that should have been converted
            relative_terms = ["yesterday", "last week", "two days ago", "earlier today", "day before yesterday"]
            print("\n=== DATE CONVERSION CHECK ===")
            for term in relative_terms:
                found = term.lower() in doc.lower()
                status = "FAIL - relative date found" if found else "PASS - converted to absolute"
                print(f"  '{term}': {status}")

            # Check for absolute date patterns (YYYY-MM-DD or Month Day)
            date_patterns = re.findall(r'\d{4}-\d{2}-\d{2}|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}', doc)
            if date_patterns:
                print(f"\n  Absolute dates found in document: {date_patterns}")
            else:
                print("\n  WARNING: No absolute date patterns found in document")
        else:
            print("  No active document — compaction may not have fired.")
            print("  Check that KERNOS_COMPACTION_THRESHOLD is low enough.")

    print("\n\n  Done.")


if __name__ == "__main__":
    asyncio.run(main())
