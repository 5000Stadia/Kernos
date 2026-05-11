"""CODING-SESSION-BRIDGE-V1 — file-based bridge to already-running coding sessions.

Architect-ratified spec at Notion ``35cffafef4db8152b3dad07092eaf142``.

Two tools, file-based bridge directory under
``data/<instance>/coding_session_bridge/``:

* ``ask_coding_session(target, question, context)`` writes a structured
  request to ``requests/{request_id}.json``. Returns
  ``(summary, ActionStateRecord)`` with ``execution_state="attempted"``
  (consultation cycle is in flight; response not yet available).
* ``read_coding_session_response(request_id)`` checks for a response
  file at ``responses/{request_id}.json``. Returns:

    - ``completed`` with structured findings if present;
    - ``attempted`` if absent and within the timeout window
      (Kernos can poll or proceed);
    - ``failed`` if the bridge times out (default 1 hour from
      the request's ``timestamp``).

Distinct operational shape from the existing ``consult`` tool
(``kernos/kernel/external_agents/tool.py``), which spawns fresh CLI
subprocesses. This one talks to an already-running session via the
file bridge.

Event emission on response detection (per architect's event-emission
revision, Notion ``35cffafef4db8101bebffe32e8b43e74``):
``coding_consult.response_received`` fires once per response arrival,
gated by a sentinel file ``responses/{request_id}.emitted`` written
via ``os.rename`` for atomicity. ``correlation_id`` equals
``request_id`` literally (no prefix) so workflow gates filtering on
``payload.request_id`` work without unwrapping.

Per-tool ActionStateRecord composition follows RESPONSE-FIDELITY-V1
Batch 1.3 — handlers return ``tuple[str, ActionStateRecord]`` and the
caller (``reasoning.execute_tool``) appends the record to
``self._turn_action_records``. Mirrors the ``note_this`` shape.

Path scope is enforced by tool implementation — the request_id is
slugified through ``_safe_request_id`` so out-of-scope writes via
path traversal in the request_id parameter are blocked at the
substrate boundary. The spec's scope discipline rides at this layer;
no substrate-level filesystem gate primitive exists yet (that's a
follow-up if/when needed).
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from kernos.kernel.integration import ActionStateRecord
from kernos.utils import _safe_name, utc_now

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


VALID_TARGETS = frozenset({"claude_code", "codex"})
"""Supported coding sessions. Aligns with the spec's target enum."""

VALID_INVESTIGATION_OUTCOMES = frozenset({
    "completed", "partial", "unable_to_investigate",
})

# Request-id allowable shape: UUID-like (hex + hyphens) plus underscores.
# Anything else is rejected by _safe_request_id so path traversal via
# the request_id parameter cannot escape the bridge directory.
_REQUEST_ID_RX = re.compile(r"^[A-Za-z0-9_\-]+$")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "CODING_SESSION_BRIDGE: invalid %s=%r, using default %d",
            name, raw, default,
        )
        return default


def _timeout_seconds() -> int:
    """Bridge timeout (default 1 hour); env-tunable.

    Read on every call so tests can monkey-patch the env var without
    re-importing the module.
    """
    return _env_int("KERNOS_CODING_SESSION_BRIDGE_TIMEOUT_SECONDS", 3600)


async def _is_request_past_timeout(
    instance_id: str, request_id: str, data_dir: str,
) -> bool:
    """Read the request's ``timestamp`` field and return True if it's
    older than ``_timeout_seconds()``. Used by both the response-file
    polling path and the JSONDecodeError partial-write path
    (Codex post-impl H1).
    """
    request_path = _requests_dir(data_dir, instance_id) / f"{request_id}.json"
    if not request_path.exists():
        return False
    try:
        with open(request_path, "r", encoding="utf-8") as f:
            request_data = json.load(f)
        submitted_at = request_data.get("timestamp", "")
    except (OSError, json.JSONDecodeError):
        return False
    if not submitted_at:
        return False
    from datetime import datetime, timezone
    try:
        submitted_dt = datetime.fromisoformat(submitted_at)
        now_dt = datetime.now(timezone.utc)
        elapsed = (now_dt - submitted_dt).total_seconds()
        return elapsed > _timeout_seconds()
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Path helpers (tool-internal scope enforcement)
# ---------------------------------------------------------------------------


def _bridge_dir(data_dir: str, instance_id: str) -> Path:
    """Resolve the bridge directory for an instance. Path is constructed
    from sanitized components only — caller-provided request_id is
    validated separately via ``_safe_request_id``."""
    return Path(data_dir) / _safe_name(instance_id) / "coding_session_bridge"


def _requests_dir(data_dir: str, instance_id: str) -> Path:
    return _bridge_dir(data_dir, instance_id) / "requests"


def _responses_dir(data_dir: str, instance_id: str) -> Path:
    return _bridge_dir(data_dir, instance_id) / "responses"


def _safe_request_id(request_id: str) -> str:
    """Validate request_id has no path-traversal characters. Raises
    ``ValueError`` if the input is unsafe; otherwise returns it
    unchanged. The check is the load-bearing path-scope guard."""
    if not request_id:
        raise ValueError("request_id is required")
    if not _REQUEST_ID_RX.match(request_id):
        raise ValueError(
            f"request_id {request_id!r} contains disallowed characters; "
            f"must match {_REQUEST_ID_RX.pattern}"
        )
    if ".." in request_id:
        # Defense-in-depth even though the regex excludes it; explicit
        # check makes the intent obvious in audit.
        raise ValueError(f"request_id {request_id!r} contains '..'")
    return request_id


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------


ASK_CODING_SESSION_TOOL: dict = {
    "name": "ask_coding_session",
    "description": (
        "Ask an already-running coding session (Claude Code or Codex) "
        "to investigate substrate behavior, audit code claims, or "
        "verify reasoning. Writes a structured request to a bridge "
        "directory the operator (or v2 watcher) relays to the "
        "session. Returns a request_id; use "
        "read_coding_session_response with that id to retrieve the "
        "answer.\n\n"
        "Use when: you want to verify a claim about current code; "
        "you're confused about substrate behavior and would benefit "
        "from an investigation pass; you want a second opinion from "
        "a coding tool with substrate access.\n\n"
        "Distinct from the `consult` tool (which spawns a fresh CLI "
        "subprocess). This one talks to an ALREADY-RUNNING session "
        "via the file bridge. Operator (or future watcher) is in "
        "the loop on the relay; expect async response."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "enum": sorted(VALID_TARGETS),
                "description": "Which coding session to ask.",
            },
            "question": {
                "type": "string",
                "description": (
                    "Prose description of what you want investigated. "
                    "Be specific; the coding session will use this to "
                    "scope its investigation."
                ),
            },
            "context": {
                "type": "object",
                "description": (
                    "Optional structured context to help the coding "
                    "session find what's relevant."
                ),
                "properties": {
                    "suspected_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "File paths you suspect are relevant."
                        ),
                    },
                    "related_conversation": {
                        "type": "string",
                        "description": (
                            "Conversation excerpts that frame the "
                            "question."
                        ),
                    },
                    "prior_decisions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Architectural decisions you're "
                            "reasoning over."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
        "required": ["target", "question"],
        "additionalProperties": False,
    },
}


READ_CODING_SESSION_RESPONSE_TOOL: dict = {
    "name": "read_coding_session_response",
    "description": (
        "Retrieve a coding session's response to an earlier "
        "ask_coding_session request. Returns:\n"
        "  - 'attempted' if the response hasn't arrived yet "
        "(consultation cycle in flight; safe to poll later);\n"
        "  - 'completed' with structured findings if the response "
        "has arrived;\n"
        "  - 'failed' if the bridge timed out (default 1 hour "
        "from request submission)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "request_id": {
                "type": "string",
                "description": (
                    "The request_id returned by ask_coding_session."
                ),
            },
        },
        "required": ["request_id"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Event emission (idempotent via sentinel file)
# ---------------------------------------------------------------------------


async def _emit_response_received_once(
    *,
    instance_id: str,
    request_id: str,
    response_payload: dict,
    data_dir: str,
    emit_event: Callable[..., Awaitable[None]] | None,
) -> None:
    """Fire ``coding_consult.response_received`` exactly once per
    response arrival. Idempotency via sentinel file
    ``responses/{request_id}.emitted`` claimed atomically before
    emit via ``O_CREAT | O_EXCL`` so two concurrent readers cannot
    both pass the check and double-emit (Codex post-impl M2).

    Claim-before-emit ordering means if the emit itself fails after
    a successful claim, the sentinel remains in place and the event
    will NOT re-fire on subsequent reads. The trade-off prefers
    exactly-once over at-least-once: the architect's spec contract
    is "fires once per response arrival." A failed emit logs loud;
    manual intervention or audit-log replay covers the failure case.

    ``emit_event`` callable signature is
    ``async (event_type, payload, *, correlation_id) -> None`` so
    injected emitters can use the correlation_id metadata if they
    want; the spec contract is that ``correlation_id == request_id``
    literally regardless of which emission path runs (Codex M3).
    Implementations that only accept ``(event_type, payload)`` can
    drop ``correlation_id`` via ``**kwargs``-style swallowing; the
    request_id remains in the payload for those consumers.
    """
    responses_dir = _responses_dir(data_dir, instance_id)
    responses_dir.mkdir(parents=True, exist_ok=True)
    sentinel_path = responses_dir / f"{request_id}.emitted"

    # Atomic claim: O_CREAT | O_EXCL refuses if the file already
    # exists. Two concurrent emitters race here; only one wins the
    # OS-level claim. The losers see FileExistsError and bail
    # without emitting.
    try:
        fd = os.open(
            str(sentinel_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o644,
        )
    except FileExistsError:
        return  # another emit already won the claim
    except OSError as exc:
        logger.warning(
            "CODING_SESSION_BRIDGE: sentinel claim failed (event may "
            "re-fire on next read): %s", exc,
        )
        return
    try:
        # Write a marker timestamp into the sentinel for debugging.
        # The presence of the file is the load-bearing semantic, not
        # the contents; a zero-byte sentinel would also work.
        os.write(fd, utc_now().encode("utf-8"))
    finally:
        os.close(fd)

    # Build payload — only after the claim succeeds.
    request_path = _requests_dir(data_dir, instance_id) / f"{request_id}.json"
    originating_member_id = ""
    originating_kernos_instance = instance_id
    target = response_payload.get("target", "")
    try:
        with open(request_path, "r", encoding="utf-8") as f:
            request_data = json.load(f)
        originating_member_id = request_data.get("originating_member_id", "") or ""
        originating_kernos_instance = (
            request_data.get("originating_kernos_instance", instance_id)
            or instance_id
        )
        if not target:
            target = request_data.get("target", "") or ""
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    payload = {
        "request_id": request_id,
        "originating_kernos_instance": originating_kernos_instance,
        "originating_member_id": originating_member_id,
        "target": target,
        "investigation_outcome": response_payload.get(
            "investigation_outcome", "",
        ),
    }

    if emit_event is not None:
        try:
            # Codex post-impl M3: pass correlation_id explicitly so
            # the injected callable can satisfy the full event
            # contract. Callables that only accept (event_type,
            # payload) should declare **kwargs (or use a wrapper
            # that does) to swallow the extra kwarg.
            await emit_event(
                "coding_consult.response_received",
                payload,
                correlation_id=request_id,
            )
            return
        except TypeError:
            # Callable doesn't accept correlation_id kwarg; retry the
            # minimal signature so existing emit_event consumers
            # (FRICTION-PATTERN-style callables) keep working.
            try:
                await emit_event(
                    "coding_consult.response_received", payload,
                )
                return
            except Exception as exc:
                logger.warning(
                    "CODING_SESSION_BRIDGE: emit via callable "
                    "(minimal signature) failed: %s", exc,
                )
        except Exception as exc:
            logger.warning(
                "CODING_SESSION_BRIDGE: emit via callable failed: %s",
                exc,
            )

    # Fallback: module-level event_stream emit always carries the
    # correlation_id metadata. payload.request_id is the load-bearing
    # workflow-gate filter key regardless of which path emits.
    try:
        from kernos.kernel import event_stream
        await event_stream.emit(
            instance_id,
            "coding_consult.response_received",
            payload,
            correlation_id=request_id,
        )
    except Exception as exc:
        logger.debug(
            "CODING_SESSION_BRIDGE: emit via event_stream failed: %s",
            exc,
        )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _new_action_id() -> str:
    return f"act_{uuid.uuid4().hex[:12]}"


async def handle_ask_coding_session(
    *,
    instance_id: str,
    member_id: str,
    active_space_id: str,
    data_dir: str,
    target: str,
    question: str,
    context: dict | None = None,
) -> tuple[str, ActionStateRecord]:
    """Write a request file. Returns ``(summary, ActionStateRecord)``.

    ActionStateRecord has ``execution_state="attempted"`` per spec
    (consultation cycle in flight; response not yet available).
    """
    action_id = _new_action_id()

    if target not in VALID_TARGETS:
        return (
            f"Error: target {target!r} not in {sorted(VALID_TARGETS)}.",
            ActionStateRecord(
                action_id=action_id,
                surface="coding_session_bridge",
                operation="ask_coding_session",
                operation_class="mutate",
                authorization_state="not_required",
                execution_state="failed",
                user_visible_summary=(
                    f"ask_coding_session validation failed: invalid "
                    f"target {target!r}"
                ),
                risk_level="low",
            ),
        )

    if not question or not question.strip():
        return (
            "Error: question is required.",
            ActionStateRecord(
                action_id=action_id,
                surface="coding_session_bridge",
                operation="ask_coding_session",
                operation_class="mutate",
                authorization_state="not_required",
                execution_state="failed",
                user_visible_summary=(
                    "ask_coding_session validation failed: empty question"
                ),
                risk_level="low",
            ),
        )

    request_id = uuid.uuid4().hex
    requests_dir = _requests_dir(data_dir, instance_id)
    try:
        requests_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return (
            f"Error: could not create bridge directory: {exc}",
            ActionStateRecord(
                action_id=action_id,
                surface="coding_session_bridge",
                operation="ask_coding_session",
                operation_class="mutate",
                authorization_state="not_required",
                execution_state="failed",
                user_visible_summary=(
                    f"ask_coding_session bridge directory create failed: {exc}"
                ),
                risk_level="low",
            ),
        )

    request_path = requests_dir / f"{request_id}.json"
    request_body = {
        "request_id": request_id,
        "timestamp": utc_now(),
        "target": target,
        "originating_kernos_instance": instance_id,
        "originating_space": active_space_id,
        "originating_member_id": member_id,
        "question": question,
        "context": context or {},
    }
    try:
        # Atomic write via tempfile + rename so partial files never
        # surface to a relayer that's watching.
        tmp = request_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(request_body, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.rename(str(tmp), str(request_path))
    except OSError as exc:
        return (
            f"Error: could not write request file: {exc}",
            ActionStateRecord(
                action_id=action_id,
                surface="coding_session_bridge",
                operation="ask_coding_session",
                operation_class="mutate",
                authorization_state="not_required",
                execution_state="failed",
                user_visible_summary=(
                    f"ask_coding_session request write failed: {exc}"
                ),
                risk_level="low",
            ),
        )

    summary = (
        f"Request submitted to {target} session. "
        f"request_id={request_id}. "
        f"Use read_coding_session_response(request_id={request_id!r}) "
        f"to retrieve the answer once the operator (or watcher) has "
        f"relayed it to the session and the response is written back."
    )
    record = ActionStateRecord(
        action_id=action_id,
        surface="coding_session_bridge",
        operation="ask_coding_session",
        operation_class="mutate",
        authorization_state="not_required",
        execution_state="attempted",
        receipt_refs=(request_id,),
        user_visible_summary=summary,
        risk_level="low",
    )
    return summary, record


async def handle_read_coding_session_response(
    *,
    instance_id: str,
    data_dir: str,
    request_id: str,
    emit_event: Callable[[str, dict], Awaitable[None]] | None = None,
) -> tuple[str, ActionStateRecord]:
    """Read a response if present. Returns ``(summary, ActionStateRecord)``.

    Outcomes:
      * Response file exists → execution_state="completed", summary
        carries the findings + caveats; emits
        ``coding_consult.response_received`` event (idempotent via
        sentinel file).
      * No response file, request file exists, within timeout →
        execution_state="attempted" (Kernos polls again later or
        proceeds with other work).
      * No response file, request file exists, past timeout →
        execution_state="failed" with timeout reason.
      * No request file at all → execution_state="failed" (unknown
        request_id).
    """
    action_id = _new_action_id()

    try:
        request_id = _safe_request_id(request_id)
    except ValueError as exc:
        return (
            f"Error: {exc}",
            ActionStateRecord(
                action_id=action_id,
                surface="coding_session_bridge",
                operation="read_coding_session_response",
                operation_class="read",
                authorization_state="not_required",
                execution_state="failed",
                user_visible_summary=(
                    f"read_coding_session_response validation failed: {exc}"
                ),
                risk_level="low",
            ),
        )

    request_path = _requests_dir(data_dir, instance_id) / f"{request_id}.json"
    response_path = _responses_dir(data_dir, instance_id) / f"{request_id}.json"

    if response_path.exists():
        # Codex post-impl H1: response files are written by external
        # tooling (operator / CC session) and not under our atomic
        # control. A polling read that catches a partial JSON write
        # must NOT permanently fail the request — the writer may still
        # be in progress. JSONDecodeError within the timeout window is
        # treated as ``attempted`` (try again later); only past timeout
        # OR with no matching request does it become ``failed``.
        try:
            with open(response_path, "r", encoding="utf-8") as f:
                response_data = json.load(f)
        except OSError as exc:
            return (
                f"Error: response file present but unreadable: {exc}",
                ActionStateRecord(
                    action_id=action_id,
                    surface="coding_session_bridge",
                    operation="read_coding_session_response",
                    operation_class="read",
                    authorization_state="not_required",
                    execution_state="failed",
                    receipt_refs=(request_id,),
                    user_visible_summary=(
                        f"read_coding_session_response: response file "
                        f"unreadable for {request_id}: {exc}"
                    ),
                    risk_level="low",
                ),
            )
        except json.JSONDecodeError as exc:
            # Within timeout window → response write in progress; tell
            # the caller to poll again. Past timeout → real failure.
            timed_out = await _is_request_past_timeout(
                instance_id, request_id, data_dir,
            )
            if timed_out:
                return (
                    f"Bridge response malformed and request past timeout: {exc}",
                    ActionStateRecord(
                        action_id=action_id,
                        surface="coding_session_bridge",
                        operation="read_coding_session_response",
                        operation_class="read",
                        authorization_state="not_required",
                        execution_state="failed",
                        receipt_refs=(request_id,),
                        user_visible_summary=(
                            f"read_coding_session_response: response "
                            f"malformed and timeout exceeded for "
                            f"{request_id}: {exc}"
                        ),
                        risk_level="low",
                    ),
                )
            return (
                f"Response file present but JSON not yet parseable "
                f"(write in progress?). Poll again shortly.",
                ActionStateRecord(
                    action_id=action_id,
                    surface="coding_session_bridge",
                    operation="read_coding_session_response",
                    operation_class="read",
                    authorization_state="not_required",
                    execution_state="attempted",
                    receipt_refs=(request_id,),
                    user_visible_summary=(
                        f"read_coding_session_response: response "
                        f"file present but partial for {request_id}; "
                        f"poll again"
                    ),
                    risk_level="low",
                ),
            )

        # Codex post-impl M4: response files are written by external
        # tooling; verify the body's request_id matches the one we
        # asked for so a misplaced / stale response file can't
        # complete the wrong consultation. Also normalize
        # investigation_outcome against the documented enum.
        body_request_id = response_data.get("request_id", "")
        if body_request_id and body_request_id != request_id:
            return (
                f"Response body request_id {body_request_id!r} does not "
                f"match expected {request_id!r}; refusing to complete "
                f"the wrong consultation.",
                ActionStateRecord(
                    action_id=action_id,
                    surface="coding_session_bridge",
                    operation="read_coding_session_response",
                    operation_class="read",
                    authorization_state="not_required",
                    execution_state="failed",
                    receipt_refs=(request_id,),
                    user_visible_summary=(
                        f"read_coding_session_response: body request_id "
                        f"mismatch for {request_id} (got "
                        f"{body_request_id!r})"
                    ),
                    risk_level="low",
                ),
            )

        outcome_raw = str(response_data.get("investigation_outcome", "completed"))
        if outcome_raw not in VALID_INVESTIGATION_OUTCOMES:
            logger.warning(
                "CODING_SESSION_BRIDGE: response %s has unknown "
                "investigation_outcome=%r; treating as 'unable_to_investigate'",
                request_id, outcome_raw,
            )
            response_data["investigation_outcome"] = "unable_to_investigate"

        # Idempotent event emission.
        await _emit_response_received_once(
            instance_id=instance_id,
            request_id=request_id,
            response_payload=response_data,
            data_dir=data_dir,
            emit_event=emit_event,
        )

        findings = str(response_data.get("findings", ""))
        outcome = str(response_data.get("investigation_outcome", "completed"))
        caveats = str(response_data.get("caveats", ""))
        source_refs = response_data.get("source_references", []) or []

        summary_parts = [
            f"Response received from {response_data.get('target', 'session')} "
            f"(request_id={request_id}, outcome={outcome}).",
            "",
            "Findings:",
            findings or "(no findings text)",
        ]
        if source_refs:
            summary_parts.append("")
            summary_parts.append("Source references:")
            for ref in source_refs:
                if isinstance(ref, dict):
                    path = ref.get("path", "")
                    line_range = ref.get("line_range", "")
                    relevance = ref.get("relevance", "")
                    summary_parts.append(
                        f"  - {path}:{line_range} — {relevance}"
                    )
        if caveats:
            summary_parts.append("")
            summary_parts.append(f"Caveats: {caveats}")

        summary = "\n".join(summary_parts)
        return summary, ActionStateRecord(
            action_id=action_id,
            surface="coding_session_bridge",
            operation="read_coding_session_response",
            operation_class="read",
            authorization_state="not_required",
            execution_state="completed",
            receipt_refs=(request_id,),
            user_visible_summary=(
                f"read_coding_session_response: response received for "
                f"{request_id} (outcome={outcome})"
            ),
            risk_level="low",
        )

    # No response yet. Check the request for timeout vs in-flight.
    if not request_path.exists():
        return (
            f"Error: no request found for {request_id}.",
            ActionStateRecord(
                action_id=action_id,
                surface="coding_session_bridge",
                operation="read_coding_session_response",
                operation_class="read",
                authorization_state="not_required",
                execution_state="failed",
                user_visible_summary=(
                    f"read_coding_session_response: unknown request_id "
                    f"{request_id}"
                ),
                risk_level="low",
            ),
        )

    # Timeout check via shared helper (reused by JSONDecodeError path).
    timed_out = await _is_request_past_timeout(
        instance_id, request_id, data_dir,
    )

    if timed_out:
        return (
            f"Bridge timeout: no response received for {request_id} "
            f"within {_timeout_seconds()}s.",
            ActionStateRecord(
                action_id=action_id,
                surface="coding_session_bridge",
                operation="read_coding_session_response",
                operation_class="read",
                authorization_state="not_required",
                execution_state="failed",
                receipt_refs=(request_id,),
                user_visible_summary=(
                    f"read_coding_session_response: timeout for {request_id}"
                ),
                risk_level="low",
            ),
        )

    summary = (
        f"Response not yet received for {request_id}. "
        f"Consultation cycle is in flight; poll again later or proceed "
        f"with other work."
    )
    return summary, ActionStateRecord(
        action_id=action_id,
        surface="coding_session_bridge",
        operation="read_coding_session_response",
        operation_class="read",
        authorization_state="not_required",
        execution_state="attempted",
        receipt_refs=(request_id,),
        user_visible_summary=(
            f"read_coding_session_response: response pending for {request_id}"
        ),
        risk_level="low",
    )


__all__ = [
    "ASK_CODING_SESSION_TOOL",
    "READ_CODING_SESSION_RESPONSE_TOOL",
    "VALID_TARGETS",
    "VALID_INVESTIGATION_OUTCOMES",
    "handle_ask_coding_session",
    "handle_read_coding_session_response",
]
