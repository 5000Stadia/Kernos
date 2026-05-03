#!/usr/bin/env python3
"""Live test: verify hallucination fix for calendar tool calls.

Sends "make a calendar entry for tomorrow called Debug Test" and captures:
- REASON_RESPONSE: lines
- TOOL_LOOP iter= lines
- HALLUCINATION_CHECK / HALLUCINATION_TAGGED lines

Direct handler invocation — no Discord required.
"""
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Log capture — must happen before any kernos imports
# ---------------------------------------------------------------------------

class RelevantLogCapture(logging.Handler):
    KEYS = (
        "REASON_RESPONSE:",
        "TOOL_LOOP iter=",
        "HALLUCINATION_CHECK",
        "HALLUCINATION_TAGGED",
        "GATE:",
        "GATE_MODEL",
    )

    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        msg = self.format(record)
        if any(k in msg for k in self.KEYS):
            self.lines.append(msg)


capture = RelevantLogCapture()
capture.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
capture.setFormatter(formatter)

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
logging.getLogger("kernos.kernel.reasoning").setLevel(logging.DEBUG)
logging.getLogger("kernos.kernel.reasoning").addHandler(capture)
logging.getLogger("kernos.messages.handler").addHandler(capture)

# ---------------------------------------------------------------------------
# Load env BEFORE kernos imports so keys are available
# ---------------------------------------------------------------------------
from dotenv import load_dotenv
load_dotenv("/home/k/Kernos/.env")

sys.path.insert(0, "/home/k/Kernos")

from kernos.messages.handler import MessageHandler
from kernos.messages.models import NormalizedMessage, AuthLevel
from kernos.capability.client import MCPClientManager
from kernos.capability.registry import CapabilityRegistry
from kernos.kernel.engine import TaskEngine
from kernos.kernel.events import JsonEventStream
from kernos.kernel.reasoning import AnthropicProvider, ReasoningService
from kernos.kernel.state_json import JsonStateStore
from kernos.persistence.json_file import JsonAuditStore, JsonConversationStore, JsonInstanceStore


DATA_DIR = os.getenv("KERNOS_DATA_DIR", "/home/k/Kernos/data")
TENANT = "discord:000000000000000000"
CONVERSATION_ID = "live_test_hallucination_fix"


def make_msg(content: str) -> NormalizedMessage:
    return NormalizedMessage(
        sender="000000000000000000",
        content=content,
        platform="discord",
        platform_capabilities=["text"],
        conversation_id=CONVERSATION_ID,
        sender_auth_level=AuthLevel.owner_verified,
        timestamp=datetime.now(timezone.utc),
        instance_id=TENANT,
    )


def make_live_handler() -> MessageHandler:
    os.makedirs(DATA_DIR, exist_ok=True)
    events = JsonEventStream(DATA_DIR)
    state = JsonStateStore(DATA_DIR)
    conversations = JsonConversationStore(DATA_DIR)
    tenants = JsonInstanceStore(DATA_DIR)
    audit = JsonAuditStore(DATA_DIR)
    mcp = MCPClientManager()
    registry = CapabilityRegistry(mcp=mcp)
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    provider = AnthropicProvider(api_key)
    reasoning = ReasoningService(provider, events, mcp, audit)
    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(
        mcp=mcp,
        conversations=conversations,
        tenants=tenants,
        audit=audit,
        events=events,
        state=state,
        reasoning=reasoning,
        registry=registry,
        engine=engine,
        secrets_dir=os.path.join(DATA_DIR, "secrets"),
    )
    return handler


async def main():
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or api_key == "test-key":
        print("ERROR: No real ANTHROPIC_API_KEY found — cannot run live test.")
        sys.exit(1)

    print("=" * 64)
    print("Hallucination Fix — Live Test")
    print(f"Tenant: {TENANT}")
    print(f"Date:   {datetime.now(timezone.utc).isoformat()}")
    print("=" * 64)
    print()

    handler = make_live_handler()
    capture.lines.clear()

    print('Sending: "make a calendar entry for tomorrow called Debug Test"')
    print("-" * 64)

    try:
        response = await handler.process(make_msg(
            "make a calendar entry for tomorrow called Debug Test"
        ))
    except Exception as e:
        print(f"ERROR during handler.process: {e}")
        raise

    print()
    print("RESPONSE:")
    print(response)
    print()
    print("=" * 64)
    print("CAPTURED LOG LINES:")
    print("=" * 64)

    reason_lines = [l for l in capture.lines if "REASON_RESPONSE:" in l]
    tool_loop_lines = [l for l in capture.lines if "TOOL_LOOP iter=" in l]
    halluc_check_lines = [l for l in capture.lines if "HALLUCINATION_CHECK" in l]
    halluc_tagged_lines = [l for l in capture.lines if "HALLUCINATION_TAGGED" in l]

    print()
    print(f"REASON_RESPONSE lines ({len(reason_lines)}):")
    for l in reason_lines:
        print(f"  {l}")

    print()
    print(f"TOOL_LOOP iter= lines ({len(tool_loop_lines)}):")
    for l in tool_loop_lines:
        print(f"  {l}")

    print()
    print(f"HALLUCINATION_CHECK lines ({len(halluc_check_lines)}):")
    for l in halluc_check_lines:
        print(f"  {l}")

    print()
    print(f"HALLUCINATION_TAGGED lines ({len(halluc_tagged_lines)}):")
    for l in halluc_tagged_lines:
        print(f"  {l}")

    print()
    print("=" * 64)
    print("SUMMARY:")

    # Determine what happened
    has_tool_use_in_reason = any("tool_use" in l for l in reason_lines)
    actually_iterated = len(tool_loop_lines) > 0
    hallucination_fired = len(halluc_check_lines) > 0

    print(f"  REASON_RESPONSE saw tool_use in content_types: {has_tool_use_in_reason}")
    print(f"  Tool loop actually iterated:                    {actually_iterated}")
    print(f"  HALLUCINATION_CHECK fired:                      {hallucination_fired}")
    print(f"  HALLUCINATION_TAGGED fired:                     {len(halluc_tagged_lines) > 0}")

    if has_tool_use_in_reason and actually_iterated:
        print()
        print("PASS: Model tried to use a tool AND the loop iterated — fix working correctly.")
    elif has_tool_use_in_reason and not actually_iterated:
        print()
        print("POSSIBLE ISSUE: Model tried tool_use but loop did NOT iterate — hallucination detection path?")
    elif not has_tool_use_in_reason:
        print()
        print("NOTE: Model did not attempt tool_use (may have asked for auth or responded in text only).")

    print()
    print("All captured lines:")
    for l in capture.lines:
        print(f"  {l}")


if __name__ == "__main__":
    asyncio.run(main())
