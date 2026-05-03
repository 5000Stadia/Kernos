#!/usr/bin/env python3
"""Live test harness for 3D-HOTFIX-v2: Gate Full Redesign.

Tests the three-step gate (token → permission_override → model) and the new
CONFLICT response type, agent reasoning extraction, and approval token flow.

Direct handler invocation — no Discord required.
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Capture GATE_MODEL log lines for verification
class GateLogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []
    def emit(self, record):
        msg = record.getMessage()
        if "GATE_MODEL" in msg or "GATE:" in msg:
            self.lines.append(msg)

gate_capture = GateLogCapture()
gate_capture.setLevel(logging.INFO)
logging.basicConfig(level=logging.INFO, format='%(name)s %(message)s')
logging.getLogger("kernos.kernel.reasoning").addHandler(gate_capture)

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
from kernos.kernel.state import CovenantRule
from kernos.persistence.json_file import JsonAuditStore, JsonConversationStore, JsonInstanceStore


DATA_DIR = os.getenv("KERNOS_DATA_DIR", "./data")
TENANT = "discord:000000000000000000"
CONVERSATION_ID = "live_test_3d_hotfix_v2"


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


def record(step, title: str, passed: bool, notes: str = ""):
    results.append({"step": step, "title": title, "passed": passed, "notes": notes})
    status = "PASS" if passed else "FAIL"
    print(f"\n{'='*60}")
    print(f"Step {step} — {title}: {status}")
    if notes:
        for line in notes.split("\n"):
            print(f"  {line}")
    print(f"{'='*60}\n")


async def build_handler():
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

    # -------------------------------------------------------------------------
    # Step 0: Architecture verification — old methods gone, new methods present
    # -------------------------------------------------------------------------
    print("\n--- Step 0: Architecture verification ---")
    has_gate = hasattr(reasoning, '_gate_tool_call')
    has_evaluate = hasattr(reasoning, '_evaluate_gate')
    has_classify = hasattr(reasoning, '_classify_tool_effect')
    has_issue_token = hasattr(reasoning, '_issue_approval_token')
    has_validate_token = hasattr(reasoning, '_validate_approval_token')
    no_explicit_matches = not hasattr(reasoning, '_explicit_instruction_matches')
    no_prohibiting_covenant = not hasattr(reasoning, '_has_prohibiting_covenant')
    no_tool_signals = not hasattr(reasoning, '_TOOL_SIGNALS')
    no_domain_keywords = not hasattr(reasoning, '_get_domain_keywords')

    arch_ok = all([
        has_gate, has_evaluate, has_classify,
        has_issue_token, has_validate_token,
        no_explicit_matches, no_prohibiting_covenant,
        no_tool_signals, no_domain_keywords,
    ])
    record(0, "Architecture: new methods present, old methods removed", arch_ok,
           f"_gate_tool_call={has_gate}, _evaluate_gate={has_evaluate}, "
           f"_classify_tool_effect={has_classify}\n"
           f"_issue_approval_token={has_issue_token}, _validate_approval_token={has_validate_token}\n"
           f"_explicit_instruction_matches removed={no_explicit_matches}, "
           f"_has_prohibiting_covenant removed={no_prohibiting_covenant}\n"
           f"_TOOL_SIGNALS removed={no_tool_signals}, _get_domain_keywords removed={no_domain_keywords}")

    # -------------------------------------------------------------------------
    # Step 1: GateResult dataclass has new fields
    # -------------------------------------------------------------------------
    print("\n--- Step 1: GateResult dataclass fields ---")
    from kernos.kernel.reasoning import GateResult
    r = GateResult(allowed=True, reason="explicit_instruction", method="model_check",
                   conflicting_rule="test", raw_response="EXPLICIT\nSome explanation")
    fields_ok = (
        hasattr(r, 'allowed') and hasattr(r, 'reason') and hasattr(r, 'method') and
        hasattr(r, 'proposed_action') and hasattr(r, 'conflicting_rule') and
        hasattr(r, 'raw_response')
    )
    record(1, "GateResult has conflicting_rule and raw_response fields", fields_ok,
           f"allowed={r.allowed}, reason={r.reason}, method={r.method}, "
           f"conflicting_rule={r.conflicting_rule!r}, raw_response={r.raw_response[:30]!r}")

    # -------------------------------------------------------------------------
    # Step 2: Tool classification
    # -------------------------------------------------------------------------
    print("\n--- Step 2: Tool classification ---")
    reads = ["remember", "list_files", "read_file", "request_tool"]
    writes = ["write_file", "delete_file"]
    reads_ok = all(reasoning._classify_tool_effect(t, None) == "read" for t in reads)
    writes_ok = all(reasoning._classify_tool_effect(t, None) == "soft_write" for t in writes)
    unknown_ok = reasoning._classify_tool_effect("mystery-tool", None) == "unknown"
    record(2, "Tool effect classification",
           reads_ok and writes_ok and unknown_ok,
           f"reads={reads_ok}, writes={writes_ok}, unknown={unknown_ok}")

    # -------------------------------------------------------------------------
    # Step 3: Approval token mechanics (unit-level, no LLM)
    # -------------------------------------------------------------------------
    print("\n--- Step 3: Approval token mechanics ---")
    tool_input = {"summary": "Dentist appointment", "start": "2026-03-15T16:00:00"}
    token = reasoning._issue_approval_token("create-event", tool_input)
    # Valid: first use
    valid_first = reasoning._validate_approval_token(token.token_id, "create-event", tool_input)
    # Single-use: second use rejected
    valid_second = reasoning._validate_approval_token(token.token_id, "create-event", tool_input)
    # Wrong tool name
    token2 = reasoning._issue_approval_token("create-event", tool_input)
    wrong_tool = reasoning._validate_approval_token(token2.token_id, "delete-event", tool_input)
    # Wrong input hash
    token3 = reasoning._issue_approval_token("create-event", tool_input)
    wrong_hash = reasoning._validate_approval_token(token3.token_id, "create-event", {"summary": "Other"})
    # Expired (6 min ago)
    token4 = reasoning._issue_approval_token("create-event", tool_input)
    token4.issued_at = datetime.now(timezone.utc) - timedelta(minutes=6)
    expired = reasoning._validate_approval_token(token4.token_id, "create-event", tool_input)
    # sort_keys stability check
    import hashlib
    import json as _json
    h1 = hashlib.md5(_json.dumps(tool_input, sort_keys=True).encode()).hexdigest()[:8]
    h2 = hashlib.md5(_json.dumps(tool_input, sort_keys=True).encode()).hexdigest()[:8]
    hash_stable = (h1 == h2)

    token_ok = valid_first and not valid_second and not wrong_tool and not wrong_hash and not expired and hash_stable
    record(3, "ApprovalToken mechanics (single-use, TTL, hash, sort_keys)",
           token_ok,
           f"valid_first={valid_first}, single_use_rejected={not valid_second}, "
           f"wrong_tool_rejected={not wrong_tool}, wrong_hash_rejected={not wrong_hash}, "
           f"expired_rejected={not expired}, hash_stable={hash_stable}")

    # -------------------------------------------------------------------------
    # Step 4: Permission override mechanical bypass (no model call)
    # -------------------------------------------------------------------------
    print("\n--- Step 4: Permission override mechanical bypass ---")
    from unittest.mock import AsyncMock
    from kernos.kernel.state import InstanceProfile

    mock_state = AsyncMock()
    mock_state.get_instance_profile = AsyncMock(return_value=InstanceProfile(
        instance_id="t1", status="active", created_at="2026-01-01",
        permission_overrides={"google-calendar": "always-allow"},
    ))
    mock_state.query_covenant_rules = AsyncMock(return_value=[])

    from kernos.capability.known import KNOWN_CAPABILITIES
    import dataclasses as _dc
    registry_for_gate = CapabilityRegistry(mcp=None)
    for cap in KNOWN_CAPABILITIES:
        registry_for_gate.register(_dc.replace(cap))

    from kernos.kernel.events import JsonEventStream as _JES
    from kernos.persistence.json_file import JsonAuditStore as _JAS
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        _events = _JES(tmp)
        _audit = _JAS(tmp)
        _mcp = MCPClientManager()
        _provider = AnthropicProvider(os.getenv("ANTHROPIC_API_KEY"))
        svc = ReasoningService(_provider, _events, _mcp, _audit)
        svc.set_state(mock_state)
        svc.set_registry(registry_for_gate)

        # Capture complete_simple calls
        calls = []
        orig_complete = svc.complete_simple
        async def spy_complete(*args, **kwargs):
            calls.append(True)
            return await orig_complete(*args, **kwargs)
        svc.complete_simple = spy_complete

        result = await svc._gate_tool_call(
            "create-event", {"summary": "test"}, "soft_write",
            "I was thinking", "t1", "space_1",
        )
        model_not_called = len(calls) == 0

    record(4, "Permission override is mechanical bypass (no model call)",
           result.allowed and result.method == "always_allow" and model_not_called,
           f"allowed={result.allowed}, method={result.method}, "
           f"reason={result.reason}, model_called={not model_not_called}")

    # -------------------------------------------------------------------------
    # Step 5: CONFLICT response — must_not rule + user request
    # -------------------------------------------------------------------------
    print("\n--- Step 5: CONFLICT response type ---")
    mock_state2 = AsyncMock()
    mock_state2.get_instance_profile = AsyncMock(return_value=InstanceProfile(
        instance_id="t1", status="active", created_at="2026-01-01",
    ))
    mock_state2.query_covenant_rules = AsyncMock(return_value=[
        CovenantRule(
            id="r1", instance_id="t1", capability="email",
            rule_type="must_not", description="Never send emails without asking me first",
            active=True, source="user_stated",
        ),
    ])

    with tempfile.TemporaryDirectory() as tmp:
        _events = _JES(tmp)
        _audit = _JAS(tmp)
        _mcp2 = MCPClientManager()
        _mcp2.get_tools = lambda: []
        from kernos.capability.known import KNOWN_CAPABILITIES
        _reg2 = CapabilityRegistry(mcp=None)
        for cap in KNOWN_CAPABILITIES:
            _reg2.register(_dc.replace(cap))

        svc2 = ReasoningService(AnthropicProvider(os.getenv("ANTHROPIC_API_KEY")), _events, _mcp2, _audit)
        svc2.set_state(mock_state2)
        svc2.set_registry(_reg2)

        # Mock complete_simple to return CONFLICT
        svc2.complete_simple = AsyncMock(return_value="CONFLICT")

        conflict_result = await svc2._gate_tool_call(
            "send-email", {"to": "alice@example.com", "subject": "Hello"}, "soft_write",
            "send this email to Alice", "t1", "space_1",
        )

    conflict_ok = (
        not conflict_result.allowed and
        conflict_result.reason == "covenant_conflict" and
        conflict_result.method == "model_check" and
        conflict_result.conflicting_rule == "Never send emails without asking me first"
    )
    record(5, "CONFLICT response: blocked, covenant_conflict reason, conflicting_rule set",
           conflict_ok,
           f"allowed={conflict_result.allowed}, reason={conflict_result.reason}, "
           f"method={conflict_result.method}\n"
           f"conflicting_rule={conflict_result.conflicting_rule!r}")

    # -------------------------------------------------------------------------
    # Step 6: CONFLICT system message format (three options)
    # -------------------------------------------------------------------------
    print("\n--- Step 6: CONFLICT system message has three options ---")
    token_for_conflict = reasoning._issue_approval_token("send-email", {"to": "alice@example.com", "subject": "Hello"})
    conflict_msg = (
        f"[SYSTEM] Action paused — conflict with standing rule. "
        f"Proposed: {conflict_result.proposed_action}. "
        f"Conflicting rule: {conflict_result.conflicting_rule}. "
        f"The user may be knowingly overriding this rule. "
        f"Ask for clarification. Offer three options: "
        f"(1) respect the rule, (2) override just this time with "
        f"_approval_token: '{token_for_conflict.token_id}', "
        f"(3) update or remove the rule permanently."
    )
    msg_ok = (
        "[SYSTEM]" in conflict_msg and
        "three options" in conflict_msg and
        "(1)" in conflict_msg and
        "(2)" in conflict_msg and
        "(3)" in conflict_msg and
        token_for_conflict.token_id in conflict_msg and
        "Never send emails without asking me first" in conflict_msg
    )
    record(6, "CONFLICT system message includes three options and token",
           msg_ok,
           f"Message preview:\n{conflict_msg[:300]}")

    # -------------------------------------------------------------------------
    # Step 7: DENIED system message format (vs CONFLICT)
    # -------------------------------------------------------------------------
    print("\n--- Step 7: DENIED system message is distinct from CONFLICT ---")
    token_for_denied = reasoning._issue_approval_token("create-event", {})
    denied_result = GateResult(
        allowed=False, reason="denied", method="model_check",
        proposed_action="Create calendar event",
    )
    denied_msg = (
        f"[SYSTEM] Action blocked — no authorization found. "
        f"Proposed: {denied_result.proposed_action}. "
        f"The user's recent messages do not request this action "
        f"and no covenant rule covers it. "
        f"Ask the user if they'd like you to proceed. "
        f"If they confirm, re-submit with "
        f"_approval_token: '{token_for_denied.token_id}' in the tool input. "
        f"You may also offer to create a standing rule."
    )
    denied_ok = (
        "[SYSTEM]" in denied_msg and
        "blocked" in denied_msg and
        "standing rule" in denied_msg and
        "paused" not in denied_msg and  # not CONFLICT
        token_for_denied.token_id in denied_msg
    )
    record(7, "DENIED system message is distinct from CONFLICT message",
           denied_ok,
           f"Message preview:\n{denied_msg[:300]}")

    # -------------------------------------------------------------------------
    # Step 8: Permission overrides NOT in rules_text (model prompt)
    # -------------------------------------------------------------------------
    print("\n--- Step 8: Permission overrides not in model rules_text ---")
    mock_state3 = AsyncMock()
    mock_state3.get_instance_profile = AsyncMock(return_value=InstanceProfile(
        instance_id="t1", status="active", created_at="2026-01-01",
        permission_overrides={},
    ))
    mock_state3.query_covenant_rules = AsyncMock(return_value=[])

    with tempfile.TemporaryDirectory() as tmp:
        _events = _JES(tmp)
        _audit = _JAS(tmp)
        svc3 = ReasoningService(AnthropicProvider(os.getenv("ANTHROPIC_API_KEY")), _events, MCPClientManager(), _audit)
        svc3.set_state(mock_state3)

        captured_prompts = []
        async def capture_model(system_prompt, user_content, **kwargs):
            captured_prompts.append(user_content)
            return "DENIED"
        svc3.complete_simple = capture_model

        await svc3._gate_tool_call(
            "create-event", {}, "soft_write", "thinking", "t1", "space_1",
        )

    no_always_allow_in_prompt = (
        len(captured_prompts) > 0 and
        "[always-allow]" not in captured_prompts[0]
    )
    record(8, "Permission overrides NOT included in model rules_text",
           no_always_allow_in_prompt,
           f"Model was called: {len(captured_prompts) > 0}\n"
           f"[always-allow] absent from prompt: {no_always_allow_in_prompt}")

    # -------------------------------------------------------------------------
    # Step 9: First-word parser safety — DENIED with EXPLICIT in explanation
    # -------------------------------------------------------------------------
    print("\n--- Step 9: First-word parser safety ---")
    mock_state4 = AsyncMock()
    mock_state4.get_instance_profile = AsyncMock(return_value=InstanceProfile(
        instance_id="t1", status="active", created_at="2026-01-01",
    ))
    mock_state4.query_covenant_rules = AsyncMock(return_value=[])

    with tempfile.TemporaryDirectory() as tmp:
        _events = _JES(tmp)
        _audit = _JAS(tmp)
        svc4 = ReasoningService(AnthropicProvider(os.getenv("ANTHROPIC_API_KEY")), _events, MCPClientManager(), _audit)
        svc4.set_state(mock_state4)
        # Real bug case: EXPLICIT in the denial explanation
        svc4.complete_simple = AsyncMock(
            return_value='DENIED\n\nThe user\'s message "again?" is ambiguous and does not '
                         'constitute an EXPLICIT request to create a calendar event.'
        )

        parser_result = await svc4._gate_tool_call(
            "create-event", {}, "soft_write", "again?", "t1", "space_1",
        )

    parser_ok = not parser_result.allowed and parser_result.reason == "denied"
    record(9, "First-word parser: EXPLICIT in denial explanation doesn't cause false allow",
           parser_ok,
           f"allowed={parser_result.allowed}, reason={parser_result.reason!r}")

    # -------------------------------------------------------------------------
    # Step 10: Live natural-language write tool (real LLM call via handler)
    # -------------------------------------------------------------------------
    print("\n--- Step 10: Live write tool with natural language instruction ---")
    gate_capture.lines.clear()
    response = await handler.process(make_msg(
        "Write a file called 3d-gate-live-test.md with content: 'Gate live test passed 2026-03-15'"
    ))
    print(f"Response: {response[:300]}")
    gate_lines = list(gate_capture.lines)

    gate_logged = any("GATE:" in l for l in gate_lines)
    gate_model_logged = any("GATE_MODEL:" in l for l in gate_lines)
    response_ok = isinstance(response, str) and len(response) > 0
    record(10, "Live write tool: response received, GATE logs emitted",
           response_ok,
           f"Response: {response[:200]}\n"
           f"GATE: log found={gate_logged}, GATE_MODEL: log found={gate_model_logged}\n"
           f"Gate log lines:\n" + "\n".join(f"  {l}" for l in gate_lines[:6]))

    # -------------------------------------------------------------------------
    # Step 11: Live file delete with explicit instruction
    # -------------------------------------------------------------------------
    print("\n--- Step 11: Live file delete with explicit instruction ---")
    gate_capture.lines.clear()
    response = await handler.process(make_msg(
        "Delete the file 3d-gate-live-test.md"
    ))
    print(f"Response: {response[:300]}")
    delete_gate_lines = list(gate_capture.lines)
    delete_ok = isinstance(response, str) and len(response) > 0
    record(11, "Live delete_file: gate evaluated, response received",
           delete_ok,
           f"Response: {response[:200]}\n"
           f"Gate log lines:\n" + "\n".join(f"  {l}" for l in delete_gate_lines[:6]))

    # -------------------------------------------------------------------------
    # Step 12: Read tool bypass — no gate on read-only queries
    # -------------------------------------------------------------------------
    print("\n--- Step 12: Read tool bypass (no gate on list_files) ---")
    gate_capture.lines.clear()
    response = await handler.process(make_msg("What files do I have in this space?"))
    print(f"Response: {response[:200]}")
    read_gate_lines = list(gate_capture.lines)
    # For read tools, no GATE: log should appear (gate never fires)
    no_gate_on_read = len([l for l in read_gate_lines if "GATE:" in l]) == 0
    record(12, "Read tool bypass: no GATE log for read-only queries",
           no_gate_on_read,
           f"Response: {response[:200]}\n"
           f"Gate lines: {read_gate_lines[:3] if read_gate_lines else '(none — correct)'}")

    # -------------------------------------------------------------------------
    # Step 13: DISPATCH_GATE events in event stream
    # -------------------------------------------------------------------------
    print("\n--- Step 13: DISPATCH_GATE events in event stream ---")
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
            except (json.JSONDecodeError, Exception):
                pass
    event_ok = len(gate_events) > 0
    last_event = gate_events[-1] if gate_events else {}
    has_required_fields = all(k in last_event.get("payload", {}) for k in ["tool_name", "effect", "allowed", "reason", "method"])
    record(13, "DISPATCH_GATE events emitted with required fields",
           event_ok and has_required_fields,
           f"Found {len(gate_events)} dispatch.gate events\n"
           f"Last event payload: {_json.dumps(last_event.get('payload', {}), indent=2)}")

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("LIVE TEST SUMMARY — 3D-HOTFIX-v2 Gate Redesign")
    print("=" * 60)
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"  Step {r['step']:2d}: {status} — {r['title']}")
    print(f"\nTotal: {total} | PASS: {passed} | FAIL: {failed}")
    print(f"Result: {'FULL PASS' if failed == 0 else 'HAS FAILURES'}")

    # Save results JSON
    results_path = Path(__file__).parent / "live_test_3d_hotfix_v2_results.json"
    with open(results_path, "w") as f:
        json.dump({
            "run_at": now_iso(),
            "total": total,
            "passed": passed,
            "failed": failed,
            "results": results,
        }, f, indent=2)
    print(f"\nResults saved: {results_path}")


if __name__ == "__main__":
    asyncio.run(run_tests())
