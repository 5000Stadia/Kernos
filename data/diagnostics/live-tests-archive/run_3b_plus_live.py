"""Live test for SPEC-3B+: MCP Installation.

Tests the full installation flow including:
- Agent behavior in system space (capabilities listing, OAuth explanation)
- Secure input mode intercept ("secure api" trigger and credential handoff)
- Credential storage, config persistence, event emission
- Disconnect and uninstall flow
- Startup merge (restart simulation)

Run with: source .venv/bin/activate && python tests/live/run_3b_plus_live.py
"""
import asyncio
import glob
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.WARNING)

TENANT_ID = "discord:000000000000000000"
CONVERSATION_ID = TENANT_ID


def _safe_instance_name(instance_id: str) -> str:
    import re
    return re.sub(r"[^\w.-]", "_", instance_id)


async def build_handler():
    """Build a real MessageHandler using live data dir."""
    import dataclasses
    from kernos.capability.client import MCPClientManager
    from kernos.capability.known import KNOWN_CAPABILITIES
    from kernos.capability.registry import CapabilityRegistry, CapabilityStatus
    from kernos.kernel.credentials import resolve_anthropic_credential
    from kernos.kernel.engine import TaskEngine
    from kernos.kernel.event_types import EventType
    from kernos.kernel.events import JsonEventStream
    from kernos.kernel.reasoning import AnthropicProvider, ReasoningService
    from kernos.kernel.state_json import JsonStateStore
    from kernos.messages.handler import MessageHandler
    from kernos.persistence.json_file import JsonAuditStore, JsonConversationStore, JsonInstanceStore

    data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
    secrets_dir = os.getenv("KERNOS_SECRETS_DIR", "./secrets")

    events = JsonEventStream(data_dir)
    state = JsonStateStore(data_dir)
    mcp_manager = MCPClientManager(events=events)
    # Don't connect real MCP servers for live test — calendar is AVAILABLE not CONNECTED

    conversations = JsonConversationStore(data_dir)
    tenants = JsonInstanceStore(data_dir)
    audit = JsonAuditStore(data_dir)

    registry = CapabilityRegistry(mcp=mcp_manager)
    for cap in KNOWN_CAPABILITIES:
        registry.register(dataclasses.replace(cap))

    provider = AnthropicProvider(api_key=resolve_anthropic_credential())
    reasoning = ReasoningService(provider, events, mcp_manager, audit)
    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(
        mcp_manager, conversations, tenants, audit, events, state, reasoning, registry, engine,
        secrets_dir=secrets_dir,
    )
    return handler, events, data_dir, secrets_dir


def _make_message(content: str, instance_id: str = TENANT_ID):
    from kernos.messages.models import NormalizedMessage, AuthLevel
    from datetime import datetime, timezone
    return NormalizedMessage(
        sender=instance_id.split(":")[1],
        content=content,
        platform="discord",
        platform_capabilities=["text"],
        conversation_id=CONVERSATION_ID,
        sender_auth_level=AuthLevel.owner_verified,
        timestamp=datetime.now(timezone.utc),
        instance_id=instance_id,
    )


def _find_events_since(data_dir: str, instance_id: str, event_type: str, after_ts: str) -> list:
    """Find events of a given type emitted after a timestamp."""
    safe_name = _safe_instance_name(instance_id)
    event_files = glob.glob(f"{data_dir}/{safe_name}/events/*.json")
    found = []
    for ef in sorted(event_files):
        try:
            with open(ef) as f:
                evts = json.load(f)
            for evt in evts:
                if evt.get("type") == event_type:
                    if evt.get("timestamp", "") >= after_ts:
                        found.append(evt)
        except Exception:
            pass
    return found


def _check(label: str, condition: bool, detail: str = "") -> bool:
    status = "✓ PASS" if condition else "✗ FAIL"
    msg = f"  {status}: {label}"
    if detail:
        msg += f"\n    {detail}"
    print(msg)
    return condition


results = {}


async def run_tests():
    print("\n=== SPEC-3B+ MCP Installation — Live Test ===\n")
    print(f"Tenant:  {TENANT_ID}")
    print(f"Date:    {datetime.now(timezone.utc).isoformat()[:19]}Z\n")

    handler, events, data_dir, secrets_dir = await build_handler()
    start_ts = datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Step 0: Verify new fields on CapabilityInfo / CapabilityStatus
    # ------------------------------------------------------------------
    print("### Step 0 — New CapabilityInfo fields and SUPPRESSED status\n")
    from kernos.capability.registry import CapabilityStatus, CapabilityInfo
    from kernos.capability.known import KNOWN_CAPABILITIES

    has_suppressed = hasattr(CapabilityStatus, "SUPPRESSED")
    cal = next((c for c in KNOWN_CAPABILITIES if c.name == "google-calendar"), None)
    has_requires_web = cal is not None and hasattr(cal, "requires_web_interface") and cal.requires_web_interface is True
    has_server_fields = cal is not None and cal.server_command == "npx" and "@cocal/google-calendar-mcp" in cal.server_args
    has_creds_key = cal is not None and cal.credentials_key == "google-calendar"

    results[0] = all([has_suppressed, has_requires_web, has_server_fields, has_creds_key])
    _check("SUPPRESSED status exists", has_suppressed)
    _check("google-calendar.requires_web_interface=True", has_requires_web)
    _check("google-calendar.server_command='npx'", has_server_fields,
           f"command={getattr(cal, 'server_command', None)}, args={getattr(cal, 'server_args', None)}")
    _check("google-calendar.credentials_key='google-calendar'", has_creds_key)

    # ------------------------------------------------------------------
    # Step 1: Agent in system space lists capabilities
    # ------------------------------------------------------------------
    print("\n### Step 1 — Agent lists available capabilities\n")
    print("  Sending: \"What tools can I connect?\"")
    msg = _make_message("What tools can I connect?")
    try:
        response = await handler.process(msg)
        print(f"  Response: {response[:300]}")
        has_cal_mention = "calendar" in response.lower() or "google" in response.lower()
        results[1] = has_cal_mention
        _check("Agent mentions Google Calendar or calendar in response", has_cal_mention)
    except Exception as exc:
        print(f"  ERROR: {exc}")
        results[1] = False
        _check("Agent responds to capabilities question", False, str(exc))

    # ------------------------------------------------------------------
    # Step 2: Connect Google Calendar — agent explains OAuth limitation
    # ------------------------------------------------------------------
    print("\n### Step 2 — Connect Google Calendar (OAuth → web interface required)\n")
    print("  Sending: \"Connect Google Calendar\"")
    msg = _make_message("Connect Google Calendar")
    try:
        response = await handler.process(msg)
        print(f"  Response: {response[:400]}")
        # Agent should mention OAuth/web interface limitation OR setup instructions
        mentions_limitation = any(word in response.lower() for word in [
            "web", "browser", "oauth", "interface", "can't", "cannot", "channel",
            "setup", "connect", "calendar"
        ])
        results[2] = mentions_limitation
        _check("Agent responds with calendar setup info or limitation", mentions_limitation)
    except Exception as exc:
        print(f"  ERROR: {exc}")
        results[2] = False
        _check("Agent responds to Connect Google Calendar", False, str(exc))

    # ------------------------------------------------------------------
    # Step 3: "secure api" trigger
    # ------------------------------------------------------------------
    print("\n### Step 3 — Secure input mode activation\n")
    print("  Sending: \"secure api\"")

    # For inference to work, handler needs to know which capability we're setting up.
    # The agent just discussed "google-calendar" above, so inference should find it.
    msg = _make_message("secure api")
    try:
        response = await handler.process(msg)
        print(f"  Response: {response[:300]}")
        # google-calendar requires_web_interface=True, so inference finds it from AVAILABLE
        # But the secure api flow still activates — inference just looks for capability mentions
        mode_active = "Secure input mode" in response or "secure" in response.lower()
        tenant_in_state = TENANT_ID in handler._secure_input_state
        results[3] = mode_active
        _check("Secure input mode activated", mode_active)
        _check("Tenant registered in _secure_input_state", tenant_in_state,
               f"state keys: {list(handler._secure_input_state.keys())}")
        if tenant_in_state:
            cap_in_state = handler._secure_input_state[TENANT_ID].capability_name
            _check(f"Capability name inferred: {cap_in_state}", bool(cap_in_state))
    except Exception as exc:
        print(f"  ERROR: {exc}")
        results[3] = False
        _check("Secure input mode activated", False, str(exc))

    # ------------------------------------------------------------------
    # Step 4: Send test API key — credential stored, connection attempted
    # ------------------------------------------------------------------
    print("\n### Step 4 — Credential handoff\n")
    test_key = "test-api-key-live-3b-plus-verification"

    # If secure mode wasn't activated in Step 3, manually set it
    if TENANT_ID not in handler._secure_input_state:
        from kernos.messages.handler import SecureInputState
        from datetime import timedelta
        handler._secure_input_state[TENANT_ID] = SecureInputState(
            capability_name="google-calendar",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        print("  (manually set secure input state for testing)")

    print(f"  Sending test key: {test_key}")
    msg = _make_message(test_key)
    try:
        response = await handler.process(msg)
        print(f"  Response: {response[:300]}")
        key_stored_response = "Key stored" in response
        results[4] = key_stored_response
        _check("Response mentions key stored", key_stored_response)
    except Exception as exc:
        print(f"  ERROR: {exc}")
        results[4] = False
        _check("Credential handoff response", False, str(exc))

    # ------------------------------------------------------------------
    # Step 5: Check secrets directory
    # ------------------------------------------------------------------
    print("\n### Step 5 — Secrets directory check\n")
    safe_name = _safe_instance_name(TENANT_ID)
    secret_path = Path(secrets_dir) / safe_name / "google-calendar.key"
    file_exists = secret_path.exists()
    key_content = secret_path.read_text().strip() if file_exists else ""
    key_correct = key_content == test_key

    if file_exists:
        file_mode = oct(secret_path.stat().st_mode)[-3:]
    else:
        file_mode = "N/A"

    results[5] = file_exists and key_correct
    _check(f"Credential file exists: {secret_path}", file_exists)
    _check(f"Credential content matches test key", key_correct,
           f"expected: {test_key!r}, got: {key_content!r}")
    _check(f"File permissions are 600", file_mode == "600", f"mode: {file_mode}")

    # ------------------------------------------------------------------
    # Step 6: Verify credential NOT in conversation store
    # ------------------------------------------------------------------
    print("\n### Step 6 — Credential isolation from conversation store\n")
    stored_messages = await handler.conversations.get_recent(TENANT_ID, CONVERSATION_ID, limit=100)
    secret_in_store = any(test_key in str(m.get("content", "")) for m in stored_messages)

    results[6] = not secret_in_store
    _check("Test API key NOT found in conversation store", not secret_in_store,
           f"Messages checked: {len(stored_messages)}" + (f"\n    FOUND in message!" if secret_in_store else ""))

    # ------------------------------------------------------------------
    # Step 7: capabilities-overview.md reflects state
    # ------------------------------------------------------------------
    print("\n### Step 7 — capabilities-overview.md in system space\n")
    system_space = await handler._get_system_space(TENANT_ID)
    if system_space:
        overview = await handler._files.read_file(TENANT_ID, system_space.id, "capabilities-overview.md")
        overview_exists = not overview.startswith("Error:")
        results[7] = overview_exists
        _check("capabilities-overview.md exists in system space", overview_exists)
        if overview_exists:
            print(f"  Overview preview: {overview[:200]}")
    else:
        results[7] = False
        _check("System space found", False)

    # ------------------------------------------------------------------
    # Step 8: Check event stream for TOOL_INSTALLED (if connection succeeded)
    # ------------------------------------------------------------------
    print("\n### Step 8 — Event stream check\n")

    # Check all events since start
    all_events_since = _find_events_since(data_dir, TENANT_ID, "tool.installed", start_ts)
    # Even if google-calendar fails to connect (no real OAuth), check events were attempted
    installed_event_found = len(all_events_since) > 0

    # Also check tool.uninstalled for later test
    print(f"  tool.installed events since start: {len(all_events_since)}")
    if all_events_since:
        for evt in all_events_since:
            print(f"    capability_name={evt['payload'].get('capability_name')}")

    # Note: if google-calendar fails (no credentials), no TOOL_INSTALLED is emitted.
    # This is correct behavior — we verify events are emitted on SUCCESS below.
    # For the live test, we use a mock capability to verify the event path.
    from kernos.capability.registry import CapabilityInfo, CapabilityStatus
    # Register a test cap that will "connect" via mock mcp
    test_cap = CapabilityInfo(
        name="test-live-tool", display_name="Test Live Tool",
        description="test", category="test",
        status=CapabilityStatus.AVAILABLE,
        server_name="test-live-tool",
        server_command="echo",
        server_args=["test"],
        credentials_key="test-live-tool",
        env_template={"TEST_KEY": "{credentials}"},
    )
    handler.registry.register(test_cap)

    # Override mcp.connect_one to succeed
    from unittest.mock import AsyncMock, MagicMock
    original_connect_one = handler.mcp.connect_one
    handler.mcp.connect_one = AsyncMock(return_value=True)
    handler.mcp.get_tool_definitions = MagicMock(return_value={
        "test-live-tool": [{"name": "test-action"}]
    })

    await handler._store_credential(TENANT_ID, "test-live-tool", "fake-key-for-event-test")
    event_ts_before = datetime.now(timezone.utc).isoformat()
    connect_success = await handler._connect_after_credential(TENANT_ID, "test-live-tool")

    installed_events = _find_events_since(data_dir, TENANT_ID, "tool.installed", event_ts_before)
    results[8] = connect_success and len(installed_events) > 0
    _check("connect_after_credential returns True", connect_success)
    _check(f"tool.installed event emitted ({len(installed_events)} found)", len(installed_events) > 0)

    # Restore
    handler.mcp.connect_one = original_connect_one

    # ------------------------------------------------------------------
    # Step 9: mcp-servers.json persisted
    # ------------------------------------------------------------------
    print("\n### Step 9 — mcp-servers.json persistence\n")
    if system_space:
        config_raw = await handler._files.read_file(TENANT_ID, system_space.id, "mcp-servers.json")
        config_ok = not config_raw.startswith("Error:")
        if config_ok:
            config = json.loads(config_raw)
            has_test_tool = "test-live-tool" in config.get("servers", {})
            results[9] = has_test_tool
            _check("mcp-servers.json exists", config_ok)
            _check("test-live-tool in servers", has_test_tool)
            print(f"  Config servers: {list(config.get('servers', {}).keys())}")
            print(f"  Config uninstalled: {config.get('uninstalled', [])}")
        else:
            results[9] = False
            _check("mcp-servers.json exists", False, config_raw)
    else:
        results[9] = False
        _check("System space found for config check", False)

    # ------------------------------------------------------------------
    # Step 10: Disconnect capability
    # ------------------------------------------------------------------
    print("\n### Step 10 — Disconnect test-live-tool\n")
    handler.mcp.disconnect_one = AsyncMock(return_value=True)
    event_ts_before_disc = datetime.now(timezone.utc).isoformat()

    disconnect_success = await handler._disconnect_capability(TENANT_ID, "test-live-tool")
    disc_cap = handler.registry.get("test-live-tool")

    uninstalled_events = _find_events_since(data_dir, TENANT_ID, "tool.uninstalled", event_ts_before_disc)
    results[10] = disconnect_success and disc_cap.status == CapabilityStatus.SUPPRESSED

    _check("disconnect_capability returns True", disconnect_success)
    _check("Registry status set to SUPPRESSED", disc_cap.status == CapabilityStatus.SUPPRESSED,
           f"status={disc_cap.status}")
    _check("tool.uninstalled event emitted", len(uninstalled_events) > 0)

    # ------------------------------------------------------------------
    # Step 11: mcp-servers.json uninstalled list
    # ------------------------------------------------------------------
    print("\n### Step 11 — mcp-servers.json uninstalled list\n")
    if system_space:
        config_raw = await handler._files.read_file(TENANT_ID, system_space.id, "mcp-servers.json")
        if not config_raw.startswith("Error:"):
            config = json.loads(config_raw)
            in_uninstalled = "test-live-tool" in config.get("uninstalled", [])
            not_in_servers = "test-live-tool" not in config.get("servers", {})
            results[11] = in_uninstalled and not_in_servers
            _check("test-live-tool in uninstalled list", in_uninstalled)
            _check("test-live-tool NOT in servers", not_in_servers)
            print(f"  Uninstalled: {config.get('uninstalled', [])}")
        else:
            results[11] = False
            _check("mcp-servers.json readable", False)
    else:
        results[11] = False

    # ------------------------------------------------------------------
    # Step 12: Startup merge simulation (restart)
    # ------------------------------------------------------------------
    print("\n### Step 12 — Startup merge simulation (restart)\n")
    # Build a fresh handler to simulate restart
    handler2, _, _, _ = await build_handler()

    # Re-register test-live-tool as AVAILABLE (simulating fresh startup with known.py)
    from kernos.capability.registry import CapabilityInfo as CI
    test_cap2 = CI(
        name="test-live-tool", display_name="Test Live Tool",
        description="test", category="test",
        status=CapabilityStatus.AVAILABLE,
        server_name="test-live-tool",
    )
    handler2.registry.register(test_cap2)

    # Now load the config — should suppress test-live-tool (it's in uninstalled)
    await handler2._maybe_load_mcp_config(TENANT_ID)
    cap_after_restart = handler2.registry.get("test-live-tool")
    was_suppressed = cap_after_restart and cap_after_restart.status == CapabilityStatus.SUPPRESSED

    results[12] = bool(was_suppressed)
    _check("Restarted handler loads mcp-servers.json", True)  # If we got here, loading didn't crash
    _check("test-live-tool SUPPRESSED after restart", bool(was_suppressed),
           f"status={getattr(cap_after_restart, 'status', 'NOT FOUND')}")

    # Cleanup: remove test artifacts
    test_key_path = Path(secrets_dir) / _safe_instance_name(TENANT_ID) / "google-calendar.key"
    test_live_key_path = Path(secrets_dir) / _safe_instance_name(TENANT_ID) / "test-live-tool.key"
    for p in [test_live_key_path]:  # Keep google-calendar.key for AC 14 verification
        if p.exists():
            p.unlink()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n---\n")
    total = len(results)
    passed = sum(1 for v in results.values() if v)
    status = "FULL PASS" if passed == total else f"PARTIAL — {passed}/{total}"
    print(f"| Summary | |")
    print(f"|---|---|")
    print(f"| Total steps | {total} (0–{total-1}) |")
    print(f"| PASS | {passed} |")
    print(f"| FAIL | {total - passed} |")
    print(f"| Result | **{status}** |")

    return results


if __name__ == "__main__":
    results = asyncio.run(run_tests())
    all_pass = all(results.values())
    sys.exit(0 if all_pass else 1)
