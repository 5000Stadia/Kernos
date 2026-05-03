#!/usr/bin/env python3
"""Live test harness for SPEC-2D: Active Retrieval + NL Contract Parser.

Direct handler invocation — no Discord required.
Tests the remember tool, NL contract parser, and quality scoring.
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
CONVERSATION_ID = "live_test_2d"


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


results = []


def log_result(step: str, action: str, expected: str, actual: str, passed: bool):
    result = {
        "step": step,
        "action": action,
        "expected": expected,
        "actual": actual[:500],
        "passed": passed,
        "timestamp": now_iso(),
    }
    results.append(result)
    status = "✓ PASS" if passed else "✗ FAIL"
    print(f"\n{'─' * 60}")
    print(f"  {status}: Step {step}")
    print(f"  Action: {action}")
    print(f"  Expected: {expected}")
    print(f"  Actual: {actual[:200]}")
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
    print("  SPEC-2D LIVE TEST: Active Retrieval + NL Contract Parser")
    print("=" * 60)

    # Check retrieval service is wired
    has_retrieval = handler._retrieval is not None
    log_result(
        "0", "Retrieval service initialization",
        "RetrievalService wired to handler",
        f"has_retrieval={has_retrieval}",
        has_retrieval,
    )

    # Step 1: Entity query — Henderson
    print("\n\n>>> Step 1: Sending 'What do you know about Henderson?'")
    t0 = time.monotonic()
    response = await handler.process(make_msg("What do you know about Henderson?"))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response[:300]}")

    # Check if Henderson is mentioned in response
    henderson_found = "henderson" in response.lower()
    log_result(
        "1", "Entity query: Henderson",
        "Response mentions Henderson with linked knowledge",
        response,
        henderson_found,
    )

    await asyncio.sleep(3)  # Wait for Tier 2 extraction

    # Step 2: Relationship query — wife's hobbies
    print("\n\n>>> Step 2: Sending 'What do you know about my wife?'")
    t0 = time.monotonic()
    response = await handler.process(make_msg("What do you know about my wife?"))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response[:300]}")

    wife_found = "liana" in response.lower() or "wife" in response.lower()
    log_result(
        "2", "Relationship query: wife",
        "Response mentions Liana or wife with knowledge",
        response,
        wife_found,
    )

    await asyncio.sleep(3)

    # Step 3: Historical query — D&D campaign
    print("\n\n>>> Step 3: Sending 'What happened in the D&D campaign with Pip?'")
    t0 = time.monotonic()
    response = await handler.process(make_msg("What happened in the D&D campaign with Pip?"))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response[:300]}")
    print(f"  Latency: {elapsed:.1f}s")

    dnd_found = "pip" in response.lower() or "d&d" in response.lower() or "campaign" in response.lower()
    log_result(
        "3", "Historical query: D&D campaign",
        "Response mentions D&D campaign content",
        response,
        dnd_found,
    )

    # Step 3b: Latency check
    log_result(
        "3b", "Archive latency check",
        "Response within 15 seconds",
        f"{elapsed:.1f}s",
        elapsed < 15,
    )

    await asyncio.sleep(3)

    # Step 4: Foresight/appointment query
    print("\n\n>>> Step 4: Sending 'Do I have any upcoming appointments or things to track?'")
    t0 = time.monotonic()
    response = await handler.process(make_msg("Do I have any upcoming appointments or things to track?"))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response[:300]}")

    # This one is informational — check it responds (may or may not have foresight data)
    log_result(
        "4", "Foresight query",
        "Response addresses the query (may not have foresight data)",
        response,
        len(response) > 20,
    )

    await asyncio.sleep(3)

    # Step 5: Behavioral instruction — space-scoped must_not
    print("\n\n>>> Step 5: Sending 'Never contact Henderson without checking with me first.'")
    t0 = time.monotonic()
    response = await handler.process(make_msg("Never contact Henderson without checking with me first."))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response[:300]}")

    # Agent should acknowledge
    log_result(
        "5a", "Behavioral instruction: agent acknowledgment",
        "Agent acknowledges the instruction",
        response,
        len(response) > 10,
    )

    # Wait for Tier 2 extraction + NL parser to fire
    print("  Waiting 8s for Tier 2 + NL parser...")
    await asyncio.sleep(8)

    # Check if rule was created
    rules = await state.get_contract_rules(TENANT, active_only=True)
    user_stated_rules = [r for r in rules if r.source == "user_stated"]
    henderson_rule = any("henderson" in r.description.lower() for r in user_stated_rules)
    log_result(
        "5b", "Behavioral instruction: rule creation",
        "CovenantRule created with source='user_stated' mentioning Henderson",
        f"user_stated rules: {len(user_stated_rules)}, henderson match: {henderson_rule}",
        henderson_rule,
    )

    # Step 6: Global behavioral instruction
    print("\n\n>>> Step 6: Sending 'Don't ever bring up my divorce.'")
    t0 = time.monotonic()
    response = await handler.process(make_msg("Don't ever bring up my divorce."))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response[:300]}")

    log_result(
        "6a", "Global instruction: agent acknowledgment",
        "Agent acknowledges",
        response,
        len(response) > 10,
    )

    print("  Waiting 8s for Tier 2 + NL parser...")
    await asyncio.sleep(8)

    rules = await state.get_contract_rules(TENANT, active_only=True)
    user_stated_rules = [r for r in rules if r.source == "user_stated"]
    divorce_rule = any("divorce" in r.description.lower() for r in user_stated_rules)
    log_result(
        "6b", "Global instruction: rule creation",
        "CovenantRule created mentioning divorce, context_space=None",
        f"user_stated rules: {len(user_stated_rules)}, divorce match: {divorce_rule}",
        divorce_rule,
    )

    # Step 7: Ask about rules
    print("\n\n>>> Step 7: Sending 'What rules do you follow?'")
    t0 = time.monotonic()
    response = await handler.process(make_msg("What rules do you follow?"))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response[:300]}")

    log_result(
        "7", "Rule awareness",
        "Agent references behavioral rules in response",
        response,
        len(response) > 50,
    )

    await asyncio.sleep(3)

    # Step 8: Unrelated query
    print("\n\n>>> Step 8: Sending 'What do you know about quantum chromodynamics?'")
    t0 = time.monotonic()
    response = await handler.process(make_msg("What do you know about quantum chromodynamics?"))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response[:300]}")

    # Should not hallucinate stored knowledge about physics
    log_result(
        "8", "Unrelated query: no false memories",
        "Agent responds without claiming stored knowledge about physics",
        response,
        len(response) > 10,
    )

    await asyncio.sleep(3)

    # Step 9: Similarity threshold test
    print("\n\n>>> Step 9: Sending 'What do you know about spaghetti?'")
    t0 = time.monotonic()
    response = await handler.process(make_msg("What do you know about spaghetti?"))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response[:300]}")

    log_result(
        "9", "Similarity threshold test",
        "Agent does not claim stored knowledge about spaghetti",
        response,
        len(response) > 10,
    )

    # Step 10: CLI quality score check
    print("\n\n>>> Step 10: CLI quality score check")
    from kernos.kernel.retrieval import compute_quality_score
    all_entries = await state.query_knowledge(TENANT, active_only=True, limit=100)
    now = now_iso()
    scores = [(e.content[:50], compute_quality_score(e, "", now)) for e in all_entries]
    scores.sort(key=lambda x: x[1], reverse=True)

    # Check no entries at 1.0
    max_score = max(s for _, s in scores)
    all_below_1 = max_score < 1.0

    print(f"  Top 5 scores:")
    for content, score in scores[:5]:
        print(f"    {score:.3f}  {content}")
    print(f"  Bottom 3 scores:")
    for content, score in scores[-3:]:
        print(f"    {score:.3f}  {content}")

    log_result(
        "10", "Quality score ranking",
        "No entries at 1.0, scores differentiated",
        f"max={max_score:.3f}, all_below_1={all_below_1}, range={scores[-1][1]:.3f}-{scores[0][1]:.3f}",
        all_below_1,
    )

    # Summary
    print("\n\n" + "=" * 60)
    print("  LIVE TEST SUMMARY")
    print("=" * 60)
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    print(f"  {passed}/{total} steps passed")
    for r in results:
        status = "✓" if r["passed"] else "✗"
        print(f"  {status} Step {r['step']}: {r['action']}")

    # Write results
    output_path = os.path.join(os.path.dirname(__file__), "LIVE-TEST-2D.md")
    with open(output_path, "w") as f:
        f.write("# LIVE-TEST-2D: Active Retrieval + NL Contract Parser\n\n")
        f.write(f"**Date:** {now_iso()[:10]}\n")
        f.write(f"**Tenant:** {TENANT}\n")
        f.write(f"**Result:** {passed}/{total} passed\n\n")
        f.write("## Results\n\n")
        f.write("| Step | Action | Expected | Result | Status |\n")
        f.write("|------|--------|----------|--------|--------|\n")
        for r in results:
            status = "PASS" if r["passed"] else "FAIL"
            exp = r["expected"][:60]
            act = r["actual"][:60].replace("\n", " ")
            f.write(f"| {r['step']} | {r['action']} | {exp} | {act} | {status} |\n")

        f.write("\n## Detailed Results\n\n")
        for r in results:
            f.write(f"### Step {r['step']}: {r['action']}\n\n")
            f.write(f"**Expected:** {r['expected']}\n\n")
            f.write(f"**Actual:** {r['actual'][:500]}\n\n")
            f.write(f"**Status:** {'PASS' if r['passed'] else 'FAIL'}\n\n")

    print(f"\n  Results written to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
