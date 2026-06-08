"""Binding-failure diagnostics for live dispatch.

LIVE-DISPATCH-UNBLOCKER-V1 Phase C (2026-05-22).

Two-layer surface per [[agent-facing-natural-simplicity]]:

  - **Substrate layer**: structured ``BindingFailureDiagnostic``
    dataclass — full attribution (status enum, source, hash,
    reason). Emitted to the event stream (``tool.binding_failure``)
    + included in friction reports + visible via ``/dump``.
    Operators read this.

  - **Agent layer**: ``compose_agent_prose(diagnostic)`` produces
    a short natural-English sentence per failure mode. The
    dispatcher returns this prose to the agent in its tool-error
    output. The agent doesn't touch the structured form.

Both audiences get what they need at the right level of richness.

Per Kernos's design input ([[kernos-dispatch-gate-design-input]]):
"Failed bindings should be diagnosable as first-class receipts,
not vibes. We couldn't tell whether the issue was tool-not-
registered vs. registered-but-evicted vs. blocked-by-gate." The
structured form gives operators that attribution; the agent gets
prose it can naturally relay or work around.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


BindingFailureStatus = Literal[
    "not_registered",
    "registered_but_inactive",
    "registered_but_evicted",
    "blocked_by_gate_classification",
    "blocked_by_service_disable",
    "blocked_by_covenant",
    "renderer_produced_invalid_action",
]


@dataclass(frozen=True)
class BindingFailureDiagnostic:
    """Structured attribution for a failed tool binding.

    Operators consume this via the event stream + friction
    reports + /dump. The agent does NOT consume it directly —
    use ``compose_agent_prose()`` for the agent-facing surface.
    """

    tool_id: str
    status: BindingFailureStatus
    expected_source: str = "unknown"       # "kernel"|"workspace"|"mcp_capability"|"stock"|"unknown"
    gate_class: str = ""                   # "read"|"soft_write"|"hard_write"|"unknown"
    last_registration_hash: str = ""
    reason_omitted: str = ""               # free-form attribution detail
    extra: dict[str, Any] = field(default_factory=dict)  # tool-specific extras

    def to_payload(self) -> dict[str, Any]:
        """JSON-serializable dict for event emission + friction reports.

        ``extra`` is spread FIRST so the canonical fields below always win a key
        collision — a tool-specific extra must never clobber tool_id/status/etc.
        (v1 self-test Test 14: the dispatch-gate self-review flagged that
        flattening extra last let it overwrite the canonical attribution.)
        """
        return {
            **(self.extra or {}),
            "tool_id": self.tool_id,
            "status": self.status,
            "expected_source": self.expected_source,
            "gate_class": self.gate_class,
            "last_registration_hash": self.last_registration_hash,
            "reason_omitted": self.reason_omitted,
        }


def build_diagnostic(
    *,
    tool_id: str,
    catalog: Any = None,
    registry: Any = None,
    explicit_status: BindingFailureStatus | None = None,
    classification: str = "",
    extra: dict[str, Any] | None = None,
) -> BindingFailureDiagnostic:
    """Inspect catalog + registry to attribute the failure.

    Most-specific cause wins:
      1. If caller passes ``explicit_status``, use it.
      2. Tool absent from catalog → ``not_registered``.
      3. Tool in catalog AND classification known → ``blocked_by_gate_classification``
         (caller decided the call is blocked; substrate confirms the
         tool exists + has a class).
      4. Tool in catalog but no metadata available → ``registered_but_inactive``
         (catalog hit but missing source/hash details).

    Defensive: any catalog/registry error falls through to
    ``unknown`` source rather than raising.
    """
    if explicit_status is not None:
        return _from_explicit(
            tool_id=tool_id, status=explicit_status,
            catalog=catalog, registry=registry,
            classification=classification, extra=extra,
        )

    meta = _safe_get_metadata(catalog, tool_id)
    if meta is None:
        # Not in the catalog at all.
        cap_owner = _safe_capability_owner(registry, tool_id)
        if cap_owner:
            # The capability registry knows about it, but the catalog doesn't
            # — registered MCP capability whose tool just hasn't been catalogued
            # (or workspace tool's catalog entry was never created).
            return BindingFailureDiagnostic(
                tool_id=tool_id,
                status="registered_but_inactive",
                expected_source="mcp_capability",
                gate_class=classification,
                reason_omitted=f"present in capability {cap_owner!r} but not in catalog",
                extra=extra or {},
            )
        return BindingFailureDiagnostic(
            tool_id=tool_id,
            status="not_registered",
            expected_source="unknown",
            gate_class=classification,
            reason_omitted="no catalog entry; no capability owner",
            extra=extra or {},
        )
    return BindingFailureDiagnostic(
        tool_id=tool_id,
        status="registered_but_inactive",
        expected_source=meta.get("source") or "unknown",
        gate_class=classification,
        last_registration_hash=meta.get("registration_hash") or "",
        reason_omitted="catalog entry present but dispatch couldn't bind",
        extra=extra or {},
    )


def _from_explicit(
    *,
    tool_id: str,
    status: BindingFailureStatus,
    catalog: Any,
    registry: Any,
    classification: str,
    extra: dict[str, Any] | None,
) -> BindingFailureDiagnostic:
    """Fill in source/hash/etc from substrate when the caller has
    already attributed the failure status."""
    meta = _safe_get_metadata(catalog, tool_id)
    expected_source = "unknown"
    last_hash = ""
    if meta is not None:
        expected_source = meta.get("source") or "unknown"
        last_hash = meta.get("registration_hash") or ""
    elif _safe_capability_owner(registry, tool_id):
        expected_source = "mcp_capability"
    return BindingFailureDiagnostic(
        tool_id=tool_id,
        status=status,
        expected_source=expected_source,
        gate_class=classification,
        last_registration_hash=last_hash,
        reason_omitted="",
        extra=extra or {},
    )


def _safe_get_metadata(catalog: Any, tool_id: str) -> dict | None:
    if catalog is None:
        return None
    try:
        get_meta = getattr(catalog, "get_metadata", None)
        if callable(get_meta):
            return get_meta(tool_id)
    except Exception:
        pass
    return None


def _safe_capability_owner(registry: Any, tool_id: str) -> str:
    if registry is None:
        return ""
    try:
        for cap in registry.get_all():
            if tool_id in (cap.tools or []) or tool_id in (cap.tool_effects or {}):
                return cap.name
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------
# Agent-facing prose composer ([[agent-facing-natural-simplicity]])
# ---------------------------------------------------------------------


def compose_agent_prose(diagnostic: BindingFailureDiagnostic) -> str:
    """Compose the natural-English sentence the agent receives in
    its tool error output.

    Per [[agent-facing-natural-simplicity]]: agent reads prose,
    not status codes or JSON. The structured diagnostic stays
    in the event stream / friction reports for operator
    inspection.
    """
    s = diagnostic.status
    tool = diagnostic.tool_id

    if s == "not_registered":
        return (
            f"`{tool}` isn't registered as a tool here. Try a "
            f"different approach — or, if this is a capability "
            f"you want to add, use `register_tool` to build it "
            f"or `request_tool` to activate one from a connected "
            f"service."
        )
    if s == "registered_but_inactive":
        return (
            f"`{tool}` exists in the catalog but isn't currently "
            f"active. The capability may need to be reconnected, "
            f"or the tool wasn't loaded into this turn's set."
        )
    if s == "registered_but_evicted":
        return (
            f"`{tool}` was registered but didn't make this turn's "
            f"active tool set. Restate what you need more "
            f"explicitly and I can try again."
        )
    if s == "blocked_by_gate_classification":
        return (
            f"I can't act on `{tool}` — it isn't classified for "
            f"safe dispatch (its effect on the world isn't "
            f"declared)."
        )
    if s == "blocked_by_service_disable":
        return (
            f"`{tool}` is connected to a service that's currently "
            f"disabled. Ask the operator to enable it if you need "
            f"this capability."
        )
    if s == "blocked_by_covenant":
        rule = diagnostic.extra.get("rule_text", "") if diagnostic.extra else ""
        if rule:
            return f"A standing rule prevents using `{tool}`: {rule}"
        return f"A standing rule prevents using `{tool}`."
    if s == "renderer_produced_invalid_action":
        return (
            f"I called `{tool}` but that doesn't exist as a tool "
            f"— let me try a different approach."
        )
    return f"Couldn't bind `{tool}` for dispatch."
