"""Audit-log integration for workshop tool invocations.

Per the the design review-revised spec:

- Service-bound and internal tool invocations both flow into the
  existing audit log (no new log surface). The shape adds two
  category fields:
  - audit_category: operator-readable, free-form, defaults to the
    tool name for standalone tools and to the service's
    audit_category for service-bound tools. Surfaced in operator
    filters so a human can grep by service or tool family.
  - normalized_category: a fixed-vocabulary token that downstream
    processors key off without parsing operator-readable strings.
    Two values: tool.invocation.internal and
    tool.invocation.external_service.

- Payload digest: SHA-256 of canonicalised JSON. Canonicalisation
  follows RFC 8785 (JSON Canonicalisation Scheme, JCS) closely
  enough for our purposes: keys sorted lexicographically, no
  insignificant whitespace, UTF-8. The full RFC 8785 spec includes
  ECMAScript-style number formatting for edge cases (very large
  numbers, NaN, Infinity); audit payloads do not contain those, so
  Python's default number serialisation matches the canonical form
  in practice. The digest is sufficient for after-the-fact integrity
  checks without retaining sensitive request bodies.

The raw payload is not stored. Only the digest. Tokens, refresh
tokens, and credential values inside payloads cannot leak through
the audit log.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalized category vocabulary (the design review edit 4)
# ---------------------------------------------------------------------------


# Fixed vocabulary for the normalized_category field. Downstream
# processors filter by this without having to parse operator-readable
# audit_category strings. New values may be added but existing values
# are stable.
NORMALIZED_TOOL_INVOCATION_INTERNAL = "tool.invocation.internal"
NORMALIZED_TOOL_INVOCATION_EXTERNAL_SERVICE = "tool.invocation.external_service"


def normalized_category_for(*, service_id: str) -> str:
    """Return the normalized audit category for a tool invocation.

    A tool with a non-empty service_id is external-service-bound;
    otherwise it is internal. The boundary is mechanical, not
    interpretive — the descriptor's service_id field decides.
    """
    return (
        NORMALIZED_TOOL_INVOCATION_EXTERNAL_SERVICE
        if service_id
        else NORMALIZED_TOOL_INVOCATION_INTERNAL
    )


# ---------------------------------------------------------------------------
# Canonicalised digest (RFC 8785 / JCS approximation)
# ---------------------------------------------------------------------------


def canonicalize_json(payload: Any) -> bytes:
    """Return the canonical JSON encoding of `payload` as UTF-8 bytes.

    Approximates RFC 8785 (JCS):
    - Object keys sorted lexicographically.
    - No insignificant whitespace.
    - UTF-8 output.
    - ensure_ascii=False so non-ASCII strings encode as themselves
      rather than \\u escapes (matches JCS behaviour).

    Payloads containing NaN, Infinity, very-large-integer, or
    not-JSON-serialisable types fall outside this approximation;
    callers should pass dict-of-strings/numbers/lists payloads. Audit
    payloads in the workshop contract do not include those edge
    cases.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def payload_digest(payload: Any) -> str:
    """Return the hex SHA-256 digest of the canonicalised payload."""
    return hashlib.sha256(canonicalize_json(payload)).hexdigest()


# ---------------------------------------------------------------------------
# Audit entry shape for tool invocations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolInvocationAuditEntry:
    """One audit entry produced by a workshop tool invocation.

    Persists into the existing audit log. The shape adds the two
    category fields the the design review-revised spec requires; everything else
    matches the existing tool_call audit shape so operator-level
    filtering and replay continue to work.

    Fields:
        type: always "tool_call" so the existing log writer does not
            need a new path.
        timestamp: ISO-8601 string (UTC) recorded by the writer.
        instance_id: invoking install identifier.
        member_id: invoking member identifier.
        space_id: active space at invocation time.
        tool_name: descriptor name.
        operation: which named operation was invoked, if the tool
            declared per-operation classifications. Empty when the
            tool dispatches a single kind of work.
        service_id: empty for internal tools; the bound service id
            for external-service tools.
        authority: the authority subset the invocation actually used
            (i.e., the operations the tool's authority list grants).
        audit_category: operator-readable category (free-form).
        normalized_category: fixed vocabulary; one of the
            NORMALIZED_* constants above.
        payload_digest: hex SHA-256 of the canonical JSON of the
            tool's input payload. Raw payload is never stored.
        success: True when the tool returned without raising.
        error: sanitised error message when success is False; empty
            otherwise. Service errors that include token-shaped values
            are scrubbed by the caller before this is constructed.
    """

    type: str = "tool_call"
    timestamp: str = ""
    instance_id: str = ""
    member_id: str = ""
    space_id: str = ""
    tool_name: str = ""
    operation: str = ""
    service_id: str = ""
    authority: tuple[str, ...] = ()
    audit_category: str = ""
    normalized_category: str = NORMALIZED_TOOL_INVOCATION_INTERNAL
    payload_digest: str = ""
    success: bool = True
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict for the audit log writer."""
        d = asdict(self)
        # Tuples serialize as lists; the existing audit log expects
        # JSON-native types.
        d["authority"] = list(self.authority)
        return d


def build_audit_entry(
    *,
    timestamp: str,
    instance_id: str,
    member_id: str,
    space_id: str,
    tool_name: str,
    operation: str,
    service_id: str,
    authority: tuple[str, ...] | list[str],
    audit_category: str,
    payload: Any,
    success: bool,
    error: str = "",
) -> ToolInvocationAuditEntry:
    """Construct an audit entry, computing the payload digest and the
    normalized category from inputs.
    """
    return ToolInvocationAuditEntry(
        timestamp=timestamp,
        instance_id=instance_id,
        member_id=member_id,
        space_id=space_id,
        tool_name=tool_name,
        operation=operation,
        service_id=service_id,
        authority=tuple(authority),
        audit_category=audit_category,
        normalized_category=normalized_category_for(service_id=service_id),
        payload_digest=payload_digest(payload),
        success=success,
        error=error,
    )
