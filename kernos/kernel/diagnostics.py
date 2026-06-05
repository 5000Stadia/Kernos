"""Diagnostic Tools — Improvement Loop Tier 2 Pass 2.

diagnose_issue: Gathers evidence from runtime trace + source + friction reports,
  asks the LLM to synthesize a diagnosis.
propose_fix: Writes structured spec to data/{tenant}/specs/proposed/.
submit_spec: Moves proposed → submitted, optionally notifies user.

Protected boundaries prevent proposals targeting security-critical code.
"""
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kernos.utils import utc_now, _safe_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protected boundaries — propose_fix REFUSES to target these
# ---------------------------------------------------------------------------

PROTECTED_PATTERNS = [
    "kernos/kernel/gate.py",
    "kernos/kernel/credentials.py",
    "**/auth*",
    "**/security*",
    "PROTECTED_PATTERNS",  # Meta-protection
]

def _is_protected(location: str) -> bool:
    """Check if a file/function location targets protected code."""
    loc = location.lower()
    for pattern in PROTECTED_PATTERNS:
        p = pattern.lower().replace("**", "").replace("*", "")
        if p in loc:
            return True
    return False


# ---------------------------------------------------------------------------
# Spec directory management
# ---------------------------------------------------------------------------

def _specs_dir(data_dir: str, instance_id: str, stage: str) -> Path:
    """Get the specs directory for an instance and stage."""
    path = Path(data_dir) / _safe_name(instance_id) / "specs" / stage
    path.mkdir(parents=True, exist_ok=True)
    return path


def _generate_spec_id() -> str:
    return f"spec_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

DIAGNOSE_ISSUE_TOOL = {
    "name": "diagnose_issue",
    "description": (
        "Diagnose a system issue by gathering evidence from runtime trace, "
        "source code, and friction reports. Returns a structured diagnosis."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "What you observed — the symptom to investigate",
            },
            "turn_id": {
                "type": "string",
                "description": "Specific turn ID to investigate (from runtime trace)",
            },
            "code_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Source files to read for context (e.g., ['kernos/capability/client.py'])",
            },
        },
        "required": ["description"],
    },
}

PROPOSE_FIX_TOOL = {
    "name": "propose_fix",
    "description": (
        "Write a structured fix spec for a diagnosed issue. "
        "The spec is saved for review. Protected code (gate, auth, credentials) is rejected."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "diagnosis": {
                "type": "string",
                "description": "Summary of the diagnosed issue",
            },
            "location": {
                "type": "string",
                "description": "File + function/class to change (e.g., 'kernos/capability/client.py:call_tool')",
            },
            "description": {
                "type": "string",
                "description": "What to change and why",
            },
            "fix_type": {
                "type": "string",
                "enum": ["bug_fix", "optimization", "refactor", "feature"],
            },
            "risk": {
                "type": "string",
                "enum": ["low", "medium", "high"],
            },
            "test_requirements": {
                "type": "string",
                "description": "What tests should verify the fix",
            },
            "affected_components": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Other files/modules affected by this change",
            },
            "who_benefits": {
                "type": "string",
                "description": "REQUIRED — how does this help the user?",
            },
        },
        "required": ["diagnosis", "location", "description", "fix_type", "risk",
                      "test_requirements", "affected_components", "who_benefits"],
    },
}

SUBMIT_SPEC_TOOL = {
    "name": "submit_spec",
    "description": (
        "Submit a proposed fix spec for implementation. "
        "Moves from proposed/ to submitted/. Optionally notifies the user."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "spec_id": {
                "type": "string",
                "description": "ID of the spec to submit",
            },
            "notify_user": {
                "type": "boolean",
                "description": "Whether to notify the user about this spec (default true)",
            },
        },
        "required": ["spec_id"],
    },
}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def handle_diagnose_issue(
    instance_id: str,
    space_id: str,
    tool_input: dict,
    runtime_trace: Any,
    reasoning: Any,
) -> str:
    """Gather evidence and return a structured diagnosis."""
    description = tool_input.get("description", "")
    turn_id = tool_input.get("turn_id")
    code_paths = tool_input.get("code_paths", [])

    if not description:
        return "Error: description is required."

    evidence_parts: list[str] = []

    # 1. Runtime trace events
    if runtime_trace:
        if turn_id:
            events = await runtime_trace.read(instance_id, turn_id=turn_id)
        else:
            events = await runtime_trace.read(instance_id, turns=5, filter_level="error")
            if not events:
                events = await runtime_trace.read(instance_id, turns=5, filter_level="warning")
        if events:
            trace_lines = []
            for e in events[:20]:
                trace_lines.append(
                    f"  [{e.get('level', '?')}] {e.get('source', '?')}:{e.get('event', '?')} — {e.get('detail', '')[:150]}"
                )
            evidence_parts.append("Runtime trace:\n" + "\n".join(trace_lines))

    # 2. Source code (if paths specified)
    if code_paths and reasoning and hasattr(reasoning, '_read_source_impl'):
        for path in code_paths[:3]:
            try:
                from kernos.kernel.tools import read_source as _read_source_fn
                content = _read_source_fn(path, max_lines=100)
                evidence_parts.append(f"Source ({path}):\n{content[:2000]}")
            except Exception:
                evidence_parts.append(f"Source ({path}): could not read")

    # 3. Recent friction reports
    data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
    friction_dir = Path(data_dir) / "diagnostics" / "friction"
    if friction_dir.exists():
        reports = sorted(friction_dir.glob("*.md"), key=lambda _p: _p.stat().st_mtime, reverse=True)[:3]  # both naming conventions, by recency
        for rpt in reports:
            try:
                content = rpt.read_text(encoding="utf-8")[:500]
                evidence_parts.append(f"Friction report ({rpt.name}):\n{content}")
            except Exception:
                pass

    evidence = "\n\n".join(evidence_parts) if evidence_parts else "(no evidence gathered)"

    # 4. LLM synthesis
    if reasoning:
        try:
            diagnosis = await reasoning.complete_simple(
                system_prompt=(
                    "You are diagnosing a system issue. Given the symptom description and evidence, "
                    "produce a structured diagnosis:\n"
                    "- Symptom: what happened\n"
                    "- Root cause: why it happened\n"
                    "- Affected code: file + function\n"
                    "- Classification: code_bug | config_issue | provider_issue | design_gap\n"
                    "- Recommended action\n\n"
                    "Be specific and cite evidence."
                ),
                user_content=f"Symptom: {description}\n\nEvidence:\n{evidence}",
                max_tokens=512,
                prefer_cheap=True,
            )
            logger.info("DIAGNOSE: desc=%r classification=llm_synthesized", description[:60])
            return diagnosis
        except Exception as exc:
            logger.warning("DIAGNOSE: LLM synthesis failed: %s", exc)

    # Fallback: return raw evidence
    logger.info("DIAGNOSE: desc=%r classification=evidence_only", description[:60])
    return f"Diagnosis for: {description}\n\nEvidence gathered:\n{evidence}"


async def handle_propose_fix(
    instance_id: str,
    tool_input: dict,
    runtime_trace: Any = None,
) -> str:
    """Write a structured fix spec to specs/proposed/."""
    location = tool_input.get("location", "")
    who_benefits = tool_input.get("who_benefits", "")

    if not who_benefits:
        return "Error: who_benefits is required. Every fix must explain how it helps the user."

    # Protected boundary check
    if _is_protected(location):
        logger.info("PROPOSE_BLOCKED: location=%s reason=protected_boundary", location)
        return (
            f"Error: '{location}' is in a protected boundary. "
            f"Gate logic, authentication, credentials, and security code cannot be modified "
            f"through the improvement loop. Report this to the developer instead."
        )

    data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
    spec_id = _generate_spec_id()
    spec_dir = _specs_dir(data_dir, instance_id, "proposed")

    # Gather trace evidence if available
    trace_evidence = ""
    if runtime_trace:
        events = await runtime_trace.read(instance_id, turns=3, filter_level="error")
        if events:
            trace_evidence = "\n".join(
                f"- [{e.get('event', '?')}] {e.get('detail', '')[:100]}"
                for e in events[:5]
            )

    # Write spec
    spec_content = f"""# FIX SPEC: {tool_input.get('description', 'Untitled')[:80]}

**Spec ID:** {spec_id}
**Generated:** {utc_now()}
**Classification:** {tool_input.get('fix_type', 'bug_fix')}
**Risk:** {tool_input.get('risk', 'low')}
**Who benefits:** {who_benefits}

## Diagnosis

{tool_input.get('diagnosis', '(no diagnosis provided)')}

## Proposed Change

**Location:** {location}
**Description:** {tool_input.get('description', '')}

## Test Requirements

{tool_input.get('test_requirements', '(none specified)')}

## Affected Components

{chr(10).join('- ' + c for c in tool_input.get('affected_components', []))}

## Runtime Evidence

{trace_evidence or '(no trace evidence)'}
"""

    spec_path = spec_dir / f"{spec_id}.md"
    spec_path.write_text(spec_content, encoding="utf-8")

    logger.info("PROPOSE_FIX: location=%s type=%s risk=%s spec_id=%s",
        location, tool_input.get('fix_type'), tool_input.get('risk'), spec_id)

    return f"Fix spec written: {spec_id}\nLocation: {location}\nReview in specs/proposed/{spec_id}.md"


async def handle_submit_spec(
    instance_id: str,
    tool_input: dict,
    handler: Any = None,
) -> str:
    """Move a spec from proposed/ to submitted/."""
    spec_id = tool_input.get("spec_id", "")
    notify = tool_input.get("notify_user", True)

    if not spec_id:
        return "Error: spec_id is required."

    data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
    proposed_dir = _specs_dir(data_dir, instance_id, "proposed")
    submitted_dir = _specs_dir(data_dir, instance_id, "submitted")

    spec_path = proposed_dir / f"{spec_id}.md"
    if not spec_path.exists():
        return f"Error: spec {spec_id} not found in proposed/."

    # Move to submitted
    dest = submitted_dir / f"{spec_id}.md"
    spec_path.rename(dest)

    logger.info("SUBMIT_SPEC: spec_id=%s notify=%s", spec_id, notify)

    # Notify user via whisper
    if notify and handler:
        try:
            content = dest.read_text(encoding="utf-8")
            # Extract title from first heading
            title = "Fix spec"
            for line in content.split("\n"):
                if line.startswith("# FIX SPEC:"):
                    title = line[len("# FIX SPEC:"):].strip()
                    break

            from kernos.kernel.awareness import Whisper, generate_whisper_id
            whisper = Whisper(
                whisper_id=generate_whisper_id(),
                insight_text=f"I wrote a fix spec: {title}. Want to review it?",
                delivery_class="stage",
                source_space_id="",
                target_space_id="",
                supporting_evidence=[f"spec_id: {spec_id}"],
                reasoning_trace=f"Agent-generated fix spec submitted for review.",
                knowledge_entry_id="",
                foresight_signal=f"fix_spec:{spec_id}",
                created_at=utc_now(),
            )
            await handler.state.save_whisper(instance_id, whisper)
        except Exception as exc:
            logger.warning("SUBMIT_SPEC: whisper failed: %s", exc)

    return f"Spec {spec_id} submitted for implementation."
