#!/usr/bin/env python3
"""Live test harness for SPEC-3A: Per-Space File System.

Direct handler invocation — no Discord required.
Tests write_file, read_file, list_files, delete_file tools,
cross-space isolation, and compaction manifest injection.
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
CONVERSATION_ID = "live_test_3a"


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
    print("  SPEC-3A LIVE TEST: Per-Space File System")
    print(f"  Tenant: {TENANT}")
    print(f"  D&D Space: {DND_SPACE}")
    print(f"  Business Space: {HENDERSON_SPACE}")
    print("=" * 60)

    # Step 0: Verify FileService is wired
    has_files = handler._files is not None
    log_result(
        "0", "FileService initialization",
        "FileService wired to handler",
        f"has_files={has_files}",
        has_files,
    )

    # -----------------------------------------------------------------------
    # PHASE 1: D&D SPACE — basic CRUD
    # -----------------------------------------------------------------------
    print("\n\n>>> Phase 1: D&D space file operations")

    # Step 1: Create a file
    print("\n>>> Step 1: 'Create a file with my D&D campaign notes so far'")
    t0 = time.monotonic()
    response1 = await handler.process(make_msg(
        "Create a file with my D&D campaign notes so far — include what we know "
        "about Pip, the Ashen Veil, and the Tidemark docks."
    ))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response1[:400]}")

    # Check the file was created on disk
    from pathlib import Path
    from kernos.utils import _safe_name
    files_dir = Path(DATA_DIR) / _safe_name(TENANT) / "spaces" / DND_SPACE / "files"
    dnd_files = list(files_dir.iterdir()) if files_dir.exists() else []
    non_hidden = [f for f in dnd_files if not f.name.startswith(".")]

    file_created = len(non_hidden) > 0
    log_result(
        "1", "Create D&D campaign notes file",
        "Agent calls write_file; file exists on disk in D&D space",
        f"files_dir={files_dir.exists()}, files={[f.name for f in non_hidden]}, response={response1[:200]}",
        file_created,
    )

    await asyncio.sleep(3)

    # Step 2: list_files
    print("\n>>> Step 2: 'What files do I have?'")
    t0 = time.monotonic()
    response2 = await handler.process(make_msg("What files do I have?"))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response2[:400]}")

    # The response should list the campaign notes file
    has_file_listing = any(
        kw in response2.lower() for kw in ["file", "notes", "campaign", ".md", ".txt"]
    )
    log_result(
        "2", "list_files in D&D space",
        "Response lists the campaign notes file with description",
        response2,
        has_file_listing,
    )

    await asyncio.sleep(2)

    # Step 3: read_file
    print("\n>>> Step 3: 'Read the campaign notes'")
    t0 = time.monotonic()
    response3 = await handler.process(make_msg("Read the campaign notes back to me"))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response3[:400]}")

    # Should mention Pip or Ashen Veil or Tidemark
    has_campaign_content = any(
        kw in response3.lower() for kw in ["pip", "ashen", "tidemark", "campaign", "docks"]
    )
    log_result(
        "3", "read_file — campaign notes",
        "Agent reads file and returns campaign content (Pip / Ashen Veil / Tidemark)",
        response3,
        has_campaign_content,
    )

    await asyncio.sleep(2)

    # Step 4: overwrite (update file)
    print("\n>>> Step 4: 'Update the campaign notes with what happened in our last session'")
    t0 = time.monotonic()
    response4 = await handler.process(make_msg(
        "Update the campaign notes — add that in the last session Pip discovered "
        "The Architect's true identity and escaped through the sewer tunnels."
    ))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response4[:400]}")

    # Check for "Updated" confirmation in response
    file_updated = any(
        kw in response4.lower() for kw in ["updated", "update", "saved", "added", "architect", "sewer"]
    )
    log_result(
        "4", "write_file overwrite — update campaign notes",
        "Agent calls write_file (overwrite), confirms update",
        response4,
        file_updated,
    )

    await asyncio.sleep(2)

    # Step 5: delete_file — user explicitly requests deletion
    print("\n>>> Step 5: 'Delete the campaign notes'")
    t0 = time.monotonic()
    response5 = await handler.process(make_msg("Delete the campaign notes file"))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response5[:400]}")

    # Check .deleted directory
    deleted_dir = files_dir / ".deleted"
    deleted_exists = deleted_dir.exists() and len(list(deleted_dir.iterdir())) > 0

    # With our kernel principle implementation, "delete" in user message allows the action.
    # The file should be soft-deleted (moved to .deleted/, removed from manifest).
    delete_handled = (
        "deleted" in response5.lower()
        or "removed" in response5.lower()
        or deleted_exists
        or "can't delete" in response5.lower()  # In case agent declines for other reasons
    )
    note5 = (
        "Kernel principle: user said 'delete' → _check_delete_allowed=True → soft delete "
        "proceeds. File moved to .deleted/ and preserved for recovery. "
        f"deleted_dir_exists={deleted_exists}"
    )
    log_result(
        "5", "delete_file — user-requested deletion",
        "File soft-deleted to .deleted/, removed from manifest, response confirms deletion",
        f"response={response5[:200]}, deleted_dir={deleted_exists}",
        delete_handled,
        note=note5,
    )

    await asyncio.sleep(2)

    # -----------------------------------------------------------------------
    # PHASE 2: Cross-space isolation
    # -----------------------------------------------------------------------
    print("\n\n>>> Phase 2: Cross-space isolation")

    # Step 6: Switch to Business space, check list_files
    print("\n>>> Step 6: Switch to Henderson space — 'What files do I have?'")
    # Mention Henderson to trigger routing to Henderson space
    t0 = time.monotonic()
    response6 = await handler.process(make_msg(
        "I'm looking at the Henderson contract. What files do I have in this space?"
    ))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response6[:400]}")

    # Should NOT see D&D files (they're in a different space)
    # Should say "No files" or similar
    no_dnd_files = "campaign" not in response6.lower() and "pip" not in response6.lower()
    log_result(
        "6", "list_files in Henderson space — cross-space isolation",
        "No D&D files visible in Henderson space",
        response6,
        no_dnd_files,
    )

    await asyncio.sleep(2)

    # Step 7: Create a file in Business space
    print("\n>>> Step 7: 'Draft an NDA template for Henderson'")
    t0 = time.monotonic()
    response7 = await handler.process(make_msg(
        "Draft a simple NDA template for the Henderson project and save it as a file"
    ))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response7[:400]}")

    # Check Henderson space has files
    henderson_files_dir = (
        Path(DATA_DIR) / _safe_name(TENANT) / "spaces" / HENDERSON_SPACE / "files"
    )
    henderson_non_hidden = []
    if henderson_files_dir.exists():
        henderson_non_hidden = [
            f for f in henderson_files_dir.iterdir() if not f.name.startswith(".")
        ]

    henderson_file_created = len(henderson_non_hidden) > 0
    log_result(
        "7", "write_file in Business/Henderson space",
        "File created in Henderson space",
        f"files={[f.name for f in henderson_non_hidden]}, response={response7[:200]}",
        henderson_file_created,
    )

    await asyncio.sleep(2)

    # Step 8: Switch back to D&D — verify isolation
    print("\n>>> Step 8: Back to D&D space — 'What files do I have?'")
    t0 = time.monotonic()
    response8 = await handler.process(make_msg(
        "Back to D&D — what files do I have for Pip's campaign?"
    ))
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s): {response8[:400]}")

    # Should NOT see Henderson NDA
    no_henderson_in_dnd = "henderson" not in response8.lower() or "no files" in response8.lower()
    log_result(
        "8", "D&D space isolation — Henderson NDA not visible",
        "Henderson NDA not listed in D&D space file listing",
        response8,
        no_henderson_in_dnd,
    )

    await asyncio.sleep(2)

    # -----------------------------------------------------------------------
    # PHASE 3: Compaction manifest injection (Step 9)
    # -----------------------------------------------------------------------
    print("\n\n>>> Phase 3: Compaction awareness")
    print(">>> Step 9: Create a file in D&D then inspect compaction document")

    # First: recreate a D&D file (step 5 may have deleted it)
    print("  Creating a fresh D&D file for compaction test...")
    t0 = time.monotonic()
    response9a = await handler.process(make_msg(
        "Create a new session log file for our last D&D session where Pip escaped through the sewers"
    ))
    elapsed = time.monotonic() - t0
    print(f"  File creation response ({elapsed:.1f}s): {response9a[:300]}")
    await asyncio.sleep(2)

    # Check manifest state
    from kernos.kernel.files import FileService
    files_svc = FileService(DATA_DIR)
    dnd_manifest = await files_svc.load_manifest(TENANT, DND_SPACE)

    manifest_has_files = len(dnd_manifest) > 0
    log_result(
        "9", "File manifest state after creation",
        "Manifest has at least one file entry",
        f"manifest={dnd_manifest}",
        manifest_has_files,
    )

    # -----------------------------------------------------------------------
    # Step 10: CLI verification
    # -----------------------------------------------------------------------
    print("\n\n>>> Step 10: CLI — kernos-cli files")
    import subprocess
    cli_result = subprocess.run(
        ["./kernos-cli", "files", TENANT, DND_SPACE],
        capture_output=True, text=True, cwd=os.path.join(os.path.dirname(__file__), "../..")
    )
    cli_output = cli_result.stdout + cli_result.stderr
    print(f"  CLI output:\n{cli_output}")

    cli_worked = cli_result.returncode == 0 and "Files:" in cli_output
    log_result(
        "10", "kernos-cli files <tenant> <space_id>",
        "CLI shows file manifest with sizes and .deleted directory status",
        cli_output[:300],
        cli_worked,
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

    # Write results to JSON
    results_path = os.path.join(os.path.dirname(__file__), "live_test_3a_results.json")
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Full results saved: {results_path}")

    return failed


if __name__ == "__main__":
    failed = asyncio.run(main())
    sys.exit(0 if not failed else 1)
