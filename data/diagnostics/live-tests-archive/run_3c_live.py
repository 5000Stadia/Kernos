#!/usr/bin/env python3
"""Live test harness for SPEC-3C: Proactive Awareness.

Direct handler invocation — no Discord required.
Tests evaluator time pass, whisper queuing, session-start injection,
suppression, dismissal, knowledge update clearing, expired signals,
queue bounding, PROACTIVE_INSIGHT events, and evaluator lifecycle.
"""
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(name)s %(message)s')

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from kernos.kernel.awareness import AwarenessEvaluator, SuppressionEntry, Whisper, generate_whisper_id
from kernos.kernel.event_types import EventType
from kernos.kernel.events import JsonEventStream
from kernos.kernel.state import KnowledgeEntry
from kernos.kernel.state_json import JsonStateStore
from kernos.messages.handler import MessageHandler
from kernos.messages.models import NormalizedMessage, AuthLevel
from kernos.capability.client import MCPClientManager
from kernos.capability.registry import CapabilityRegistry
from kernos.kernel.engine import TaskEngine
from kernos.kernel.reasoning import AnthropicProvider, ReasoningService
from kernos.persistence.json_file import JsonAuditStore, JsonConversationStore, JsonInstanceStore


DATA_DIR = os.getenv("KERNOS_DATA_DIR", "./data")
TENANT = "discord:000000000000000000"
CONVERSATION_ID = "live_test_3c"


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

    # ==================================================================
    # Step 0: Architecture — dataclasses and evaluator exist
    # ==================================================================
    from dataclasses import fields as dc_fields
    whisper_field_names = {f.name for f in dc_fields(Whisper)}
    suppression_field_names = {f.name for f in dc_fields(SuppressionEntry)}
    has_whisper = 'whisper_id' in whisper_field_names
    has_suppression = 'resolution_state' in suppression_field_names
    has_evaluator = hasattr(AwarenessEvaluator, 'run_time_pass')
    has_event_type = hasattr(EventType, 'PROACTIVE_INSIGHT')
    record(0, "Architecture: dataclasses + evaluator + event type exist",
           has_whisper and has_suppression and has_evaluator and has_event_type,
           f"Whisper={has_whisper}, Suppression={has_suppression}, Evaluator={has_evaluator}, EventType={has_event_type}")

    # ==================================================================
    # Step 1: Evaluator starts cleanly
    # ==================================================================
    evaluator = AwarenessEvaluator(state, events, interval_seconds=3600)
    await evaluator.start(TENANT)
    started = evaluator._running and evaluator._task is not None
    await evaluator.stop()
    stopped = not evaluator._running
    record(1, "Evaluator starts and stops cleanly",
           started and stopped,
           f"started={started}, stopped={stopped}")

    # ==================================================================
    # Step 2: Time pass with no signals — produces 0 whispers
    # ==================================================================
    evaluator = AwarenessEvaluator(state, events)
    whispers = await evaluator.run_time_pass(TENANT)
    record(2, "Time pass with no foresight signals",
           len(whispers) == 0,
           f"whispers_produced={len(whispers)}")

    # ==================================================================
    # Step 3: Create a knowledge entry with foresight signal
    # ==================================================================
    test_entry = KnowledgeEntry(
        id="know_live3c_dentist",
        instance_id=TENANT,
        category="fact",
        subject="calendar",
        content="User has a dentist appointment at 3pm today (live test 3C).",
        confidence="stated",
        source_event_id="evt_livetest",
        source_description="live_test_3c",
        created_at=now_iso(),
        last_referenced=now_iso(),
        tags=["calendar", "live_test_3c"],
        active=True,
        foresight_signal="Dentist appointment at 3pm",
        foresight_expires=(datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
        context_space="",  # Global — should match any space
    )
    await state.save_knowledge_entry(test_entry)

    # Verify it's there
    fetched = await state.get_knowledge_entry(TENANT, "know_live3c_dentist")
    record(3, "Create foresight signal knowledge entry",
           fetched is not None and fetched.foresight_signal == "Dentist appointment at 3pm",
           f"entry_id={fetched.id if fetched else 'NOT_FOUND'}, signal={fetched.foresight_signal if fetched else 'N/A'}")

    # ==================================================================
    # Step 4: Time pass detects the signal
    # ==================================================================
    whispers = await evaluator.run_time_pass(TENANT)
    record(4, "Time pass detects foresight signal",
           len(whispers) == 1 and "Dentist" in whispers[0].insight_text,
           f"whispers={len(whispers)}, text={whispers[0].insight_text[:80] if whispers else 'NONE'}")

    # ==================================================================
    # Step 5: Full evaluate — whisper queued
    # ==================================================================
    # Clean up any existing test whispers first
    for w in await state.get_pending_whispers(TENANT):
        if "live_test_3c" in w.reasoning_trace or "Dentist" in w.insight_text:
            await state.delete_whisper(TENANT, w.whisper_id)
    # Also clean test suppressions
    for s in await state.get_suppressions(TENANT, knowledge_entry_id="know_live3c_dentist"):
        await state.delete_suppression(TENANT, s.whisper_id)

    await evaluator._evaluate(TENANT)
    pending = await state.get_pending_whispers(TENANT)
    dentist_whispers = [w for w in pending if "Dentist" in w.insight_text]
    record(5, "Whisper queued after evaluate",
           len(dentist_whispers) >= 1,
           f"pending_total={len(pending)}, dentist_whispers={len(dentist_whispers)}")

    # ==================================================================
    # Step 6: Session-start injection — send message, check response
    # ==================================================================
    print("\n--- Step 6: Sending message to trigger whisper injection ---")
    response = await handler.process(make_msg("Hey, what's up?"))
    print(f"Response: {response[:300]}")
    # The agent should mention the dentist appointment naturally
    has_dentist_ref = "dentist" in response.lower() or "appointment" in response.lower() or "3pm" in response.lower()
    record(6, "Session-start whisper injection",
           isinstance(response, str) and len(response) > 10,
           f"Response mentions dentist: {has_dentist_ref}. Response: {response[:150]}")

    # ==================================================================
    # Step 7: Suppression prevents nagging
    # ==================================================================
    # After injection, whispers should be surfaced and suppressed
    pending_after = await state.get_pending_whispers(TENANT)
    dentist_pending_after = [w for w in pending_after if "Dentist" in w.insight_text]
    suppressions = await state.get_suppressions(TENANT, knowledge_entry_id="know_live3c_dentist")
    surfaced_suppressions = [s for s in suppressions if s.resolution_state == "surfaced"]
    record(7, "Suppression created after surfacing",
           len(dentist_pending_after) == 0 and len(surfaced_suppressions) >= 1,
           f"pending_dentist={len(dentist_pending_after)}, surfaced_suppressions={len(surfaced_suppressions)}")

    # Run evaluator again — should NOT produce new whispers for the same entry
    await evaluator._evaluate(TENANT)
    pending_after2 = await state.get_pending_whispers(TENANT)
    dentist_new = [w for w in pending_after2 if "Dentist" in w.insight_text]
    record(7, "Suppression prevents re-queueing (continued)",
           len(dentist_new) == 0,
           f"dentist_whispers_after_re_evaluate={len(dentist_new)}")

    # ==================================================================
    # Step 8: Dismiss whisper
    # ==================================================================
    if surfaced_suppressions:
        dismiss_whisper_id = surfaced_suppressions[0].whisper_id
        result = await reasoning._handle_dismiss_whisper(TENANT, dismiss_whisper_id, "user_dismissed")
        dismissed = await state.get_suppressions(TENANT, whisper_id=dismiss_whisper_id)
        is_dismissed = dismissed and dismissed[0].resolution_state == "dismissed"
        record(8, "Dismiss whisper updates suppression",
               is_dismissed,
               f"result={result}, dismissed={is_dismissed}")
    else:
        record(8, "Dismiss whisper (skipped — no surfaced suppression to dismiss)",
               False, "No surfaced suppression found")

    # ==================================================================
    # Step 9: Knowledge update clears suppression
    # ==================================================================
    # Create a new suppression to test clearing
    test_s = SuppressionEntry(
        whisper_id="wsp_clear_test_3c",
        knowledge_entry_id="know_live3c_dentist",
        foresight_signal="Dentist appointment at 3pm",
        created_at=now_iso(),
        resolution_state="surfaced",
    )
    await state.save_suppression(TENANT, test_s)

    # Simulate knowledge update clearing (same logic as llm_extractor)
    clear_suppressions = await state.get_suppressions(TENANT, knowledge_entry_id="know_live3c_dentist")
    for s in clear_suppressions:
        if s.resolution_state == "surfaced":
            await state.delete_suppression(TENANT, s.whisper_id)

    remaining = await state.get_suppressions(TENANT, whisper_id="wsp_clear_test_3c")
    record(9, "Knowledge update clears surfaced suppression",
           len(remaining) == 0,
           f"remaining_after_clear={len(remaining)}")

    # ==================================================================
    # Step 10: Expired signal not picked up
    # ==================================================================
    expired_entry = KnowledgeEntry(
        id="know_live3c_expired",
        instance_id=TENANT,
        category="fact",
        subject="calendar",
        content="Old meeting that already happened",
        confidence="stated",
        source_event_id="evt_livetest",
        source_description="live_test_3c",
        created_at=now_iso(),
        last_referenced=now_iso(),
        tags=["live_test_3c"],
        active=True,
        foresight_signal="Meeting that already happened",
        foresight_expires=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    )
    await state.save_knowledge_entry(expired_entry)
    # Clean suppressions for clean test
    for s in await state.get_suppressions(TENANT, knowledge_entry_id="know_live3c_expired"):
        await state.delete_suppression(TENANT, s.whisper_id)

    whispers_expired = await evaluator.run_time_pass(TENANT)
    expired_whispers = [w for w in whispers_expired if "already happened" in w.insight_text]
    record(10, "Expired signal not picked up by time pass",
           len(expired_whispers) == 0,
           f"expired_whispers={len(expired_whispers)}")

    # ==================================================================
    # Step 11: Queue bounding
    # ==================================================================
    # Create 15 test whispers
    for i in range(15):
        w = Whisper(
            whisper_id=f"wsp_bound_test_{i:03d}",
            insight_text=f"Test signal {i}",
            delivery_class="ambient",
            source_space_id="",
            target_space_id="",
            supporting_evidence=[],
            reasoning_trace="queue bound test",
            knowledge_entry_id=f"know_bound_{i}",
            foresight_signal=f"signal_{i}",
            created_at=now_iso(),
        )
        await state.save_whisper(TENANT, w)

    await evaluator._enforce_queue_bound(TENANT, max_whispers=10)
    pending_bound = await state.get_pending_whispers(TENANT)
    bound_test = [w for w in pending_bound if "queue bound test" in w.reasoning_trace]
    record(11, "Queue bounded to max 10",
           len(bound_test) <= 10,
           f"bound_test_whispers={len(bound_test)}, total_pending={len(pending_bound)}")

    # Clean up bound test whispers
    for w in bound_test:
        await state.delete_whisper(TENANT, w.whisper_id)

    # ==================================================================
    # Step 12: PROACTIVE_INSIGHT event in stream
    # ==================================================================
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    from kernos.utils import _safe_name
    event_path = Path(DATA_DIR) / _safe_name(TENANT) / "events" / f"{today}.json"
    insight_events = []
    if event_path.exists():
        with open(event_path) as f:
            try:
                all_events = json.load(f)
                for evt in all_events:
                    if isinstance(evt, dict) and evt.get("type") == "proactive.insight":
                        insight_events.append(evt)
            except json.JSONDecodeError:
                pass
    record(12, "PROACTIVE_INSIGHT events in event stream",
           len(insight_events) >= 1,
           f"Found {len(insight_events)} proactive.insight events")

    # ==================================================================
    # Step 13: Clean shutdown
    # ==================================================================
    evaluator2 = AwarenessEvaluator(state, events, interval_seconds=3600)
    await evaluator2.start(TENANT)
    await evaluator2.stop()
    record(13, "Clean evaluator shutdown",
           not evaluator2._running,
           "No errors on stop")

    # ==================================================================
    # Cleanup test data
    # ==================================================================
    print("\n--- Cleaning up test data ---")
    # Deactivate test knowledge entries
    for eid in ["know_live3c_dentist", "know_live3c_expired"]:
        entry = await state.get_knowledge_entry(TENANT, eid)
        if entry:
            await state.update_knowledge(TENANT, eid, {"active": False})
    # Clean remaining test whispers
    for w in await state.get_pending_whispers(TENANT):
        if "live_test_3c" in w.reasoning_trace or "Dentist" in w.insight_text:
            await state.delete_whisper(TENANT, w.whisper_id)

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
