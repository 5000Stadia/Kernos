"""Self-administration tools — agent-callable equivalents of the
``/dump`` and ``/restart`` Discord slash commands.

Per memory ``feedback_system_space_design.md`` — "slash commands
should also be accessible as tools in System space". This module
mirrors the two most useful diagnostic / recovery slash commands
into the kernel-tool surface so the agent can:

  * ``dump_context`` — write its fully-assembled turn context to a
    diagnostic file for self-introspection or hand-off to a coding
    agent for review. Read-only; no destructive risk.
  * ``restart_self`` — replace the bot process via os.execv. Same
    code path as the owner running /restart. Equivalent to a clean
    reboot; loses in-flight async tasks (including the response
    that called this tool — by design). Confirmation required.

Both tools are System-space-gated at dispatch time (defense in
depth on top of the surfacing-layer gate). Conservative default:
the agent has to be operating in the System space to reach them.

The dump helper here writes a strict subset of what
``MessageHandler._handle_dump`` writes — specifically, it omits
``RECENT CONVERSATION`` (which needs the handler's ``conv_logger``)
and the per-instance ``last_real_input_tokens`` summary line.
Everything load-bearing (system prompt, messages, tools, log
buffer, last outgoing payload, summary) IS included; the agent
just gets a tag in the file noting these omissions and pointing
to /dump for the full version.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from kernos.utils import utc_now

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------


DUMP_CONTEXT_TOOL: dict = {
    "name": "dump_context",
    "description": (
        "Self-introspect: write your fully-assembled turn context "
        "(system prompt, conversation messages, surfaced tool "
        "schemas, recent log buffer, token summary) to a "
        "diagnostic file at data/diagnostics/context_<timestamp>.txt. "
        "Returns a short summary (path, file size, section "
        "breakdown, token estimates) by default — useful for "
        "quick self-checks without bloating the next turn's "
        "context.\n\n"
        "Pass include_content=true to inline the full file "
        "contents in the tool result so you can actually inspect "
        "what was dumped (the dump file lives outside any "
        "Kernos space so read_file cannot reach it). Use this "
        "when you need to grep your own substrate, audit a "
        "tool-surface decision, or feed the dump to a coding "
        "agent via consult/ask_coding_session. Caveat: dumps "
        "are typically 50-200 KB; only inline when you actually "
        "need the content.\n\n"
        "Equivalent to the owner running /dump. Read-only; no "
        "confirmation needed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "Optional: why you're dumping. Logged for "
                    "operator visibility."
                ),
            },
            "include_content": {
                "type": "boolean",
                "description": (
                    "Default false: returns just the summary + "
                    "file path. Pass true to inline the full "
                    "dump file content in the tool result so "
                    "you can inspect it directly."
                ),
            },
        },
        "additionalProperties": False,
    },
}


RESTART_SELF_TOOL: dict = {
    "name": "restart_self",
    "description": (
        "PROCESS-TERMINATING. Replaces the bot process via "
        "os.execv — clean restart, same code path as the owner "
        "running /restart. Use when the bot is in a stuck state "
        "a fresh boot will fix (broken connection, accumulated "
        "zombie tasks, post-update reload, or you've explicitly "
        "been asked to).\n\n"
        "HARD TURN-BOUNDARY: this call terminates the current "
        "process. Everything you planned to do AFTER it in the "
        "same response — additional tool calls, text content, "
        "follow-up verifications — IS DROPPED. The new process "
        "boots fresh with no continuation memory of this turn's "
        "in-flight instructions.\n\n"
        "If the user gave you a multi-step instruction that "
        "includes restart_self plus subsequent work (e.g. "
        "'restart then run these three tests'), you CANNOT "
        "execute the subsequent work in this turn. Surface a "
        "brief 'restarting now — please re-prompt me for the "
        "follow-up steps after I'm back' message to the user "
        "BEFORE calling this, then let restart_self be your "
        "FINAL action. The user re-engages you in a new turn "
        "post-restart to do the subsequent steps.\n\n"
        "Confirmation pattern: first call with confirm=false "
        "(or no confirm) returns a proposed-action string for "
        "you to surface to the user. Second call with "
        "confirm=true actually restarts. The two-step "
        "confirmation naturally enforces the turn-boundary — "
        "if you've already surfaced the proposal, the "
        "confirm=true call should be your only action that "
        "turn."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "Why a restart is needed. Logged loudly "
                    "before execv so the operator has a paper "
                    "trail."
                ),
            },
            "confirm": {
                "type": "boolean",
                "description": (
                    "Must be true to actually restart. Default "
                    "false returns the proposed action without "
                    "restarting."
                ),
            },
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------
# dump_context implementation
# ---------------------------------------------------------------------


def write_context_dump(
    *,
    system_prompt: str,
    messages: list[dict],
    tools: list[dict],
    instance_id: str,
    data_dir: str | None = None,
    system_prompt_static: str = "",
    system_prompt_dynamic: str = "",
    omit_conversation_note: bool = True,
) -> Path:
    """Write a diagnostic dump of the supplied substrate fields.

    Shared helper called by both ``MessageHandler._handle_dump`` (which
    has the full TurnContext including conv_logger) and the tool-
    dispatch path (which has only the ReasoningRequest fields). The
    tool path sets ``omit_conversation_note=True`` so the file
    includes a "(RECENT CONVERSATION omitted — tool-dispatched
    dump)" tag instead of silently dropping the section.

    Returns the Path to the written file. Caller decides what to
    return to the user / agent.
    """
    data_dir = data_dir or os.getenv("KERNOS_DATA_DIR", "./data")
    ts = utc_now()[:19].replace(":", "-")
    # Match the slash-command layout: data/diagnostics/ when no
    # instance, otherwise data/<instance>/diagnostics. Slash /dump
    # uses the plain data/diagnostics path; for parity the tool
    # writes there too.
    dump_path = Path(data_dir) / "diagnostics" / f"context_{ts}.txt"
    dump_path.parent.mkdir(parents=True, exist_ok=True)

    with open(dump_path, "w", encoding="utf-8") as f:
        f.write("=== SYSTEM PROMPT ===\n\n")
        f.write(system_prompt or "")
        f.write("\n\n=== MESSAGES ===\n\n")
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, str):
                f.write(f"[{role}]\n{content}\n\n")
            elif isinstance(content, list):
                f.write(f"[{role}] <{len(content)} content blocks>\n\n")
            else:
                f.write(f"[{role}] <non-text content>\n\n")
        f.write("\n=== TOOLS ===\n\n")
        for tool in tools:
            f.write(f"{json.dumps(tool, indent=2)}\n\n")

        if omit_conversation_note:
            f.write("\n=== RECENT CONVERSATION ===\n")
            f.write(
                "(omitted — tool-dispatched dump_context. Run /dump "
                "for the full version including conversation tail.)\n"
            )

        # Recent log buffer (in-memory) — same content as slash /dump
        f.write("\n=== RECENT LOG ===\n")
        f.write(
            "(tail of in-process log ring buffer — same lines that "
            "scroll past on stdout)\n\n"
        )
        try:
            from kernos.kernel.log_buffer import get_recent_log_lines
            tail = int(os.getenv("KERNOS_DUMP_LOG_TAIL_LINES", "150"))
            lines = get_recent_log_lines(last_n=tail)
            if lines:
                for line in lines:
                    f.write(line)
                    f.write("\n")
            else:
                f.write("(log ring buffer not installed)\n")
        except Exception as exc:
            f.write(f"(log ring buffer read failed: {exc})\n")

        # Last outgoing LLM payload (optional, env-gated)
        f.write("\n=== LAST OUTGOING PAYLOAD ===\n")
        f.write(
            "(exact JSON body shipped to the LLM on the most recent "
            "call — enable via KERNOS_CODEX_LAST_PAYLOAD=1)\n\n"
        )
        try:
            payload_path = os.getenv(
                "KERNOS_CODEX_LAST_PAYLOAD_PATH",
                os.path.join(data_dir, "diagnostics", "codex_last_payload.json"),
            )
            if Path(payload_path).exists():
                payload_text = Path(payload_path).read_text(encoding="utf-8")
                f.write(f"(source: {payload_path}, {len(payload_text)} chars)\n\n")
                f.write(payload_text)
                if not payload_text.endswith("\n"):
                    f.write("\n")
            else:
                f.write(
                    f"(no last-payload file at {payload_path}; "
                    f"set KERNOS_CODEX_LAST_PAYLOAD=1 to enable)\n"
                )
        except Exception as exc:
            f.write(f"(last-payload read failed: {exc})\n")

        f.write("\n=== SUMMARY ===\n")
        sys_chars = len(system_prompt or "")
        msg_chars = sum(len(str(m.get("content", ""))) for m in messages)
        tool_chars = sum(len(json.dumps(t)) for t in tools)
        char_est = (sys_chars + msg_chars + tool_chars) // 4
        f.write(f"System prompt: ~{sys_chars // 4} tokens ({sys_chars} chars)\n")
        if system_prompt_static:
            stat_chars = len(system_prompt_static)
            f.write(
                f"  Static (cached): ~{stat_chars // 4} tokens "
                f"({stat_chars} chars)\n"
            )
        if system_prompt_dynamic:
            dyn_chars = len(system_prompt_dynamic)
            f.write(
                f"  Dynamic (fresh):  ~{dyn_chars // 4} tokens "
                f"({dyn_chars} chars)\n"
            )
        f.write(f"Messages: {len(messages)} entries, ~{msg_chars // 4} tokens\n")
        f.write(f"Tools: {len(tools)} schemas, ~{tool_chars // 4} tokens\n")
        f.write(f"Char-based estimate: ~{char_est} tokens\n")
        f.write(f"Instance: {instance_id}\n")

    logger.info(
        "DUMP_CONTEXT_TOOL: instance=%s dump_path=%s",
        instance_id, dump_path,
    )
    return dump_path


def handle_dump_context_tool(
    *,
    request: Any,  # ReasoningRequest; typed as Any to avoid import cycle
    reason: str = "",
    include_content: bool = False,
) -> str:
    """Dispatch handler for the ``dump_context`` tool.

    Default: returns a short summary (file path, file size,
    section breakdown, token estimates). Useful for quick self-
    checks without bloating the next turn with a multi-KB inline
    payload.

    ``include_content=True``: returns the FULL file contents
    inline. The dump file lives outside any Kernos space so
    ``read_file`` cannot reach it — this is the agent's only
    path to actually inspect its own dump (vs. just knowing it
    exists). Use deliberately; dumps are typically 50-200 KB.
    """
    if reason:
        logger.info(
            "DUMP_CONTEXT_TOOL_REASON: instance=%s reason=%r",
            request.instance_id, reason,
        )
    system_prompt = getattr(request, "system_prompt", "") or ""
    messages = list(getattr(request, "messages", []) or [])
    tools = list(getattr(request, "tools", []) or [])
    system_prompt_static = getattr(request, "system_prompt_static", "") or ""
    system_prompt_dynamic = getattr(request, "system_prompt_dynamic", "") or ""
    # 2026-05-23 dump_context accounting fix: when the tool is
    # dispatched via the live-dispatch path, the ReasoningRequest
    # is constructed by _live_request_factory with empty
    # system_prompt/messages/tools (those aren't needed for normal
    # tool dispatch). For dump_context we DO need them or the
    # summary line reports 0 tokens. Fall back to the reasoning
    # service's cached last-payload (set in reason()) when the
    # passed-in request is empty.
    if not (system_prompt or messages or tools):
        from kernos.kernel.reasoning import (
            get_active_reasoning_service,
        )
        reasoning_svc = get_active_reasoning_service()
        if reasoning_svc is not None:
            instance_id = getattr(request, "instance_id", "") or ""
            cached = reasoning_svc.get_last_reasoning_payload(instance_id)
            if cached:
                system_prompt = system_prompt or cached.get("system_prompt", "")
                messages = messages or cached.get("messages", []) or []
                tools = tools or cached.get("tools", []) or []
                system_prompt_static = (
                    system_prompt_static
                    or cached.get("system_prompt_static", "")
                )
                system_prompt_dynamic = (
                    system_prompt_dynamic
                    or cached.get("system_prompt_dynamic", "")
                )
    dump_path = write_context_dump(
        system_prompt=system_prompt,
        messages=messages,
        tools=tools,
        instance_id=getattr(request, "instance_id", "") or "",
        system_prompt_static=system_prompt_static,
        system_prompt_dynamic=system_prompt_dynamic,
        omit_conversation_note=True,
    )

    if include_content:
        try:
            content = dump_path.read_text(encoding="utf-8")
        except OSError as exc:
            return (
                f"Context dumped to {dump_path}, but reading it "
                f"back failed: {exc}. Use the file directly."
            )
        size_kb = len(content) / 1024
        return (
            f"=== dump_context (full content, {size_kb:.1f} KB) ===\n"
            f"Source: {dump_path}\n"
            f"---\n"
            f"{content}"
        )

    # Default: summary only — useful at-a-glance info without
    # bloating the next turn's input.
    try:
        size_bytes = dump_path.stat().st_size
    except OSError:
        size_bytes = 0
    sys_chars = len(system_prompt)
    msg_chars = sum(len(str(m.get("content", ""))) for m in messages)
    tool_chars = sum(len(json.dumps(t)) for t in tools)
    return (
        f"Context dumped to {dump_path}\n"
        f"  file size: {size_bytes / 1024:.1f} KB\n"
        f"  system prompt: ~{sys_chars // 4} tokens ({sys_chars} chars)\n"
        f"  messages: {len(messages)} entries, ~{msg_chars // 4} tokens\n"
        f"  tools: {len(tools)} schemas, ~{tool_chars // 4} tokens\n"
        f"\n"
        f"Sections in the dump: SYSTEM PROMPT, MESSAGES, TOOLS, "
        f"RECENT LOG, LAST OUTGOING PAYLOAD, SUMMARY.\n"
        f"Pass include_content=true to inline the full file in "
        f"a follow-up call so you can read it directly."
    )


# ---------------------------------------------------------------------
# restart_self implementation
# ---------------------------------------------------------------------


def handle_restart_self_tool(
    *,
    reason: str,
    confirm: bool = False,
    instance_id: str = "",
) -> str:
    """Dispatch handler for the ``restart_self`` tool.

    Two-call confirmation pattern:
      * First call with confirm=False (or missing) returns a
        proposed-action string for the agent to surface to the user.
      * Second call with confirm=True logs the reason loudly and
        execs the process — this call does NOT return (process is
        replaced).

    Loss-of-state warning: this kills in-flight async tasks
    including the calling turn. The agent should surface a brief
    "restarting now, back in a few seconds" message to the user
    BEFORE the second call.
    """
    if not reason or not isinstance(reason, str):
        return (
            "restart_self requires a reason (string). Pass "
            "reason='why you want to restart' and confirm=true."
        )
    if not confirm:
        return (
            f"Proposed restart_self (reason: {reason!r}). "
            f"This will execv the process — in-flight tasks die "
            f"including this turn. Surface the intent to the user "
            f"first, then call again with confirm=true."
        )

    logger.warning(
        "RESTART_SELF_TOOL_FIRED: instance=%s reason=%r — "
        "executing os.execv now (no further log lines from this "
        "process expected)",
        instance_id, reason,
    )
    # Best-effort flush so the log line above lands on disk before
    # we replace the process.
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass
    # Same code path as /restart slash command.
    os.execv(sys.executable, [sys.executable] + sys.argv)
    # Unreachable — execv replaces the process.
    return "restart_self: execv did not replace the process (this should not be visible)."


__all__ = [
    "DUMP_CONTEXT_TOOL",
    "RESTART_SELF_TOOL",
    "write_context_dump",
    "handle_dump_context_tool",
    "handle_restart_self_tool",
]
