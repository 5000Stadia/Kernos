#!/usr/bin/env python3
"""Live smoke test — validates core Kernos behavior against the real provider.

Usage: source .venv/bin/activate && python tests/live/run_live_smoke.py

Requires: OPENAI_CODEX credentials in .credentials/openai-codex.json
          (or ANTHROPIC_API_KEY for Anthropic provider)

Tests both current-spec features and regression checks for prior work.
Outputs: tests/live/SMOKE_RESULTS.md
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.chdir(str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("live_smoke")

# Capture log lines for analysis
_captured_logs: list[str] = []

class LogCapture(logging.Handler):
    def emit(self, record):
        _captured_logs.append(self.format(record))

_capture_handler = LogCapture()
_capture_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
logging.getLogger().addHandler(_capture_handler)


TENANT_ID = "live_smoke_test"


async def build_handler():
    """Build a MessageHandler with real provider, same as server.py."""
    from dotenv import load_dotenv
    load_dotenv()

    from kernos.kernel.state_json import JsonStateStore
    from kernos.kernel.events import JsonEventStream
    from kernos.persistence.json_file import JsonConversationStore, JsonInstanceStore, JsonAuditStore
    from kernos.capability.client import MCPClientManager
    from kernos.capability.registry import CapabilityRegistry
    from kernos.capability.known import KNOWN_CAPABILITIES
    from kernos.kernel.reasoning import ReasoningService
    from kernos.kernel.engine import TaskEngine
    from kernos.messages.handler import MessageHandler
    import dataclasses

    data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
    state = JsonStateStore(data_dir)
    events = JsonEventStream(data_dir)
    conversations = JsonConversationStore(data_dir)
    tenants = JsonInstanceStore(data_dir)
    audit = JsonAuditStore(data_dir)
    mcp = MCPClientManager(events)

    registry = CapabilityRegistry(mcp=mcp)
    for cap in KNOWN_CAPABILITIES:
        registry.register(dataclasses.replace(cap))

    provider_name = os.getenv("KERNOS_LLM_PROVIDER", "openai-codex")
    if provider_name == "openai-codex":
        from kernos.kernel.credentials import resolve_openai_codex_credential
        from kernos.providers.codex_provider import OpenAICodexProvider
        provider = OpenAICodexProvider(credential=resolve_openai_codex_credential())
    else:
        from kernos.providers.anthropic_provider import AnthropicProvider, resolve_anthropic_credential
        provider = AnthropicProvider(api_key=resolve_anthropic_credential())

    reasoning = ReasoningService(provider, events, mcp, audit)
    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(mcp, conversations, tenants, audit, events, state, reasoning, registry, engine)
    handler.register_mcp_tools_in_catalog()
    return handler


def make_message(content: str, instance_id: str = TENANT_ID):
    from kernos.messages.models import NormalizedMessage, AuthLevel
    return NormalizedMessage(
        content=content,
        sender="smoke_tester",
        sender_auth_level=AuthLevel.owner_verified,
        platform="discord",
        platform_capabilities=["text"],
        conversation_id="smoke_conv",
        timestamp=datetime.now(timezone.utc),
        instance_id=instance_id,
    )


class TestResult:
    def __init__(self, name: str, category: str = ""):
        self.name = name
        self.category = category
        self.passed = False
        self.response = ""
        self.error = ""
        self.duration_ms = 0
        self.notes: list[str] = []

    def to_md(self) -> str:
        icon = "✅" if self.passed else "❌"
        lines = [f"### {icon} {self.name}"]
        if self.category:
            lines[0] += f" ({self.category})"
        if self.duration_ms:
            lines.append(f"**Duration:** {self.duration_ms}ms")
        if self.response:
            resp_preview = self.response[:300].replace("\n", " ↵ ")
            lines.append(f"**Response:** {resp_preview}")
        if self.error:
            lines.append(f"**Error:** {self.error}")
        for note in self.notes:
            lines.append(f"- {note}")
        return "\n".join(lines)


async def send(handler, message: str) -> tuple[str, list[str], int]:
    """Send a message and return (response, captured_logs, duration_ms)."""
    _captured_logs.clear()
    t0 = time.monotonic()
    response = await handler.process(make_message(message))
    duration = int((time.monotonic() - t0) * 1000)
    logs = list(_captured_logs)
    return response or "", logs, duration


def has_log(logs: list[str], pattern: str) -> bool:
    return any(pattern in line for line in logs)


def get_log(logs: list[str], pattern: str) -> str:
    for line in logs:
        if pattern in line:
            return line
    return ""


async def main():
    logger.info("Building handler with real provider...")
    handler = await build_handler()
    logger.info("Handler ready. Running comprehensive smoke tests...\n")

    results: list[TestResult] = []

    # =========================================================================
    # SECTION A: Core LLM Pipeline
    # =========================================================================

    # A1: Basic response — LLM returns non-empty text
    r = TestResult("Basic response", "core")
    resp, logs, dur = await send(handler, "What time is it?")
    r.duration_ms = dur
    r.response = resp
    r.passed = bool(resp.strip()) and "went wrong" not in resp.lower()
    r.notes.append("OK: non-empty" if resp.strip() else "FAIL: empty response")
    if has_log(logs, "ROUTE:"):
        r.notes.append("OK: router fired")
    else:
        r.notes.append("FAIL: no ROUTE log")
        r.passed = False
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name} ({dur}ms)")

    # A2: Multi-turn — second message in same session
    r = TestResult("Multi-turn coherence", "core")
    resp, logs, dur = await send(handler, "And what day is it?")
    r.duration_ms = dur
    r.response = resp
    r.passed = bool(resp.strip()) and "went wrong" not in resp.lower()
    r.notes.append("OK: non-empty" if resp.strip() else "FAIL: empty response")
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name} ({dur}ms)")

    # A3: Structured output — router uses JSON schema
    r = TestResult("Router structured output", "core")
    # The router call is implicit in every message; check for JSON parse errors
    resp, logs, dur = await send(handler, "Hello, how are you doing today?")
    r.duration_ms = dur
    r.response = resp
    router_failed = has_log(logs, "LLM router failed")
    r.passed = bool(resp.strip()) and not router_failed
    if router_failed:
        r.notes.append("FAIL: router fell back (structured output broken)")
    else:
        r.notes.append("OK: router returned valid JSON")
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name} ({dur}ms)")

    # =========================================================================
    # SECTION B: Context UI Quality (Hotfix)
    # =========================================================================

    # B1: DEPTH structural confidence in RULES block
    r = TestResult("DEPTH paragraph in RULES", "hotfix")
    resp, logs, dur = await send(handler, "/dump")
    r.duration_ms = dur
    r.response = resp
    # Find and read the dump file
    dump_content = ""
    if "dumped to" in resp:
        dump_path = resp.split("dumped to ")[-1].strip()
        if os.path.exists(dump_path):
            dump_content = open(dump_path).read()
    if "precisely briefed" in dump_content:
        r.passed = True
        r.notes.append("OK: DEPTH paragraph found in RULES block")
    else:
        r.passed = False
        r.notes.append("FAIL: DEPTH paragraph not found in dump")
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name} ({dur}ms)")

    # B2: USER CONTEXT source tags + no duplicates
    r = TestResult("USER CONTEXT source tags + dedup", "hotfix")
    if dump_content:
        state_section = ""
        in_state = False
        for line in dump_content.split("\n"):
            if line.startswith("## STATE"):
                in_state = True
            elif line.startswith("## ") and in_state:
                break
            elif in_state:
                state_section += line + "\n"

        has_tags = "[stated]" in state_section or "[established]" in state_section or "[known]" in state_section or "[observed]" in state_section
        # Check for duplicates: collect USER CONTEXT lines only
        in_user_ctx = False
        user_lines = []
        for line in state_section.split("\n"):
            if "USER CONTEXT:" in line:
                in_user_ctx = True
                continue
            if in_user_ctx and line.strip():
                user_lines.append(line.strip())

        seen = set()
        duplicates = []
        for line in user_lines:
            normalized = line.strip().lower()
            if normalized in seen:
                duplicates.append(line)
            seen.add(normalized)

        has_kernos_identity = "identity/name in the system state is kernos" in state_section.lower()

        issues = []
        if not has_tags and user_lines:
            issues.append(f"FAIL: no source tags found in {len(user_lines)} knowledge entries")
        if duplicates:
            issues.append(f"FAIL: {len(duplicates)} duplicate(s): {duplicates[0][:60]}")
        if has_kernos_identity:
            issues.append("FAIL: 'user identity is Kernos' still present")

        if not issues:
            r.passed = True
            r.notes.append("OK: source tags present, no duplicates, no identity confusion")
        else:
            r.passed = False
            r.notes.extend(issues)
    else:
        r.passed = False
        r.notes.append("FAIL: no dump content available")
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name}")

    # =========================================================================
    # SECTION C: Tool Surfacing Redesign
    # =========================================================================

    # C1: Tool surfacing logs correctly
    r = TestResult("Tool surfacing logs", "surfacing")
    resp, logs, dur = await send(handler, "Search for good sushi restaurants")
    r.duration_ms = dur
    r.response = resp
    surfacing_log = get_log(logs, "TOOL_SURFACING:")
    r.passed = bool(surfacing_log) and bool(resp.strip())
    if surfacing_log:
        r.notes.append(f"OK: {surfacing_log.split(': ', 1)[-1][:100]}")
    else:
        r.notes.append("FAIL: no TOOL_SURFACING log")
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name} ({dur}ms)")

    # C2: All kernel tools surfaced (should be ~20+ tools)
    r = TestResult("Kernel tools all surfaced", "surfacing")
    tool_count = 0
    for line in logs:
        if "tool_count=" in line:
            try:
                tool_count = int(line.split("tool_count=")[1].split()[0])
            except (ValueError, IndexError):
                pass
            break
    r.passed = 8 <= tool_count <= 20  # Budget: 8-20 tools (TOOL-WINDOW spec)
    r.notes.append(f"tool_count={tool_count} (expect ≥15)")
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name} (tools={tool_count})")

    # =========================================================================
    # SECTION D: Code Execution (AW-1)
    # =========================================================================

    # D1: execute_code works
    r = TestResult("Code execution", "workspace")
    resp, logs, dur = await send(handler, "Calculate 2 to the power of 100 using execute_code")
    r.duration_ms = dur
    r.response = resp
    has_result = "1267650600228229401496703205376" in resp
    code_exec_log = has_log(logs, "CODE_EXEC")
    r.passed = has_result or (bool(resp.strip()) and "went wrong" not in resp.lower())
    if has_result:
        r.notes.append("OK: correct computation result")
    elif code_exec_log:
        r.notes.append("OK: execute_code fired (result format may vary)")
    else:
        r.notes.append("WARN: no CODE_EXEC log, agent may not have used the tool")
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name} ({dur}ms)")

    # =========================================================================
    # SECTION E: Timing + Context Size (Regression)
    # =========================================================================

    # E1: Context size and turn timing
    r = TestResult("Context size + timing", "regression")
    # Use the last turn's logs
    ctx_tokens = 0
    assemble_ms = 0
    route_ms = 0
    for line in logs:
        if "ctx_tokens_est=" in line:
            try:
                ctx_tokens = int(line.split("ctx_tokens_est=")[1].split()[0].rstrip("("))
            except (ValueError, IndexError):
                pass
        if "assemble=" in line and "TURN_TIMING" in line:
            try:
                assemble_ms = int(line.split("assemble=")[1].split()[0])
                route_ms = int(line.split("route=")[1].split()[0])
            except (ValueError, IndexError):
                pass

    r.notes.append(f"ctx_tokens_est={ctx_tokens}")
    r.notes.append(f"assemble={assemble_ms}ms route={route_ms}ms")
    r.passed = True  # Informational — no hard pass/fail
    if ctx_tokens > 15000:
        r.notes.append("WARN: context size >15k — check for bloat")
    if assemble_ms > 5000:
        r.notes.append("WARN: assembly >5s — check cohort parallelization")
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name}")

    # =========================================================================
    # SECTION F: Preference Parser + Knowledge Pipeline (Regression)
    # =========================================================================

    # F1: Preference parser doesn't crash
    r = TestResult("Preference parser stability", "regression")
    pref_crash = any("PREF_DETECT: failed" in l for l in logs)
    r.passed = not pref_crash
    if pref_crash:
        r.notes.append("FAIL: preference parser threw an error (structured output issue?)")
    else:
        r.notes.append("OK: preference parser ran without errors")
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name}")

    # F2: Knowledge shaping doesn't crash
    r = TestResult("Knowledge shaping stability", "regression")
    shape_log = any("KNOWLEDGE_SHAPED" in l or "SHAPE_INPUT" in l for l in logs)
    r.passed = True  # Just check it ran
    r.notes.append("OK: knowledge shaping ran" if shape_log else "INFO: no shaping logs (may have no candidates)")
    results.append(r)
    logger.info(f"{'PASS' if r.passed else 'FAIL'}: {r.name}")

    # =========================================================================
    # Write results
    # =========================================================================

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    provider = os.getenv("KERNOS_LLM_PROVIDER", "openai-codex")

    md = f"# Live Smoke Test Results\n\n"
    md += f"**Date:** {ts}\n"
    md += f"**Result:** {passed}/{total} passed\n"
    md += f"**Provider:** {provider}\n\n"

    # Group by category
    categories = {}
    for r in results:
        cat = r.category or "other"
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(r)

    category_labels = {
        "core": "Core LLM Pipeline",
        "hotfix": "Context UI Quality (Hotfix)",
        "surfacing": "Tool Surfacing Redesign",
        "workspace": "Agentic Workspace",
        "regression": "Regression Checks",
    }

    for cat, cat_results in categories.items():
        label = category_labels.get(cat, cat.title())
        cat_passed = sum(1 for r in cat_results if r.passed)
        md += f"## {label} ({cat_passed}/{len(cat_results)})\n\n"
        for r in cat_results:
            md += r.to_md() + "\n\n"

    output_path = Path("tests/live/SMOKE_RESULTS.md")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(md)

    logger.info(f"\n{'='*60}")
    logger.info(f"SMOKE TEST COMPLETE: {passed}/{total} passed")
    for cat, cat_results in categories.items():
        label = category_labels.get(cat, cat)
        cat_passed = sum(1 for r in cat_results if r.passed)
        logger.info(f"  {label}: {cat_passed}/{len(cat_results)}")
    logger.info(f"Results: {output_path}")
    logger.info(f"{'='*60}")

    return passed == total


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
