"""Cross-space request dispatch — kernel-side validation, covenant
evaluation, and per-action-kind execution.

Flow per :func:`dispatch_request`:

1. Reentrancy guard (:func:`enter_cross_space`).
2. Validate envelope: action_kind in whitelist, target exists,
   member-set match, instance-bound.
3. Idempotency check: if request_id seen before in target's audit,
   return the original receipt.
4. **Same-space short-circuit** — when target_space_id ==
   origin_space_id, run inside the existing turn's lock; do NOT
   acquire a new per-space lock. Audit log entry is suppressed
   (it's a normal local action).
5. Acquire target's per-space lock (Q1 constraint). Bounded
   timeout (30s default) — on timeout return ``failed`` with
   ``refusal_reason='timeout_waiting_for_target'``.
6. Inside the lock as one ordered unit: covenant evaluation →
   mutation → event/audit emission. Receipt returns AFTER lock
   release.

Covenant evaluation (Q2 constraint): LLM-based via
``DispatchGate._evaluate_model``. ``propose_covenant`` action_kind
bypasses the gate (proposals are always proposed-not-applied; the
proposal entity itself records the request capsule and audit).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from kernos.kernel.cross_space.envelopes import (
    ALLOWED_ACTION_KINDS,
    CrossSpaceReceipt,
    CrossSpaceReceiptRef,
    CrossSpaceRequest,
    ReceiptStatus,
    new_request_id,
)
from kernos.kernel.cross_space.reentrancy import (
    enter_cross_space,
    exit_cross_space,
)
from kernos.utils import utc_now

logger = logging.getLogger(__name__)


# Default bounded timeout for waiting on the target-space lock.
# Per Q1: a longer-running target turn shouldn't block origin
# indefinitely; surface as a structured failure instead.
DEFAULT_TARGET_LOCK_TIMEOUT_SECONDS: float = 30.0


# ---------------------------------------------------------------------------
# Action-kind dispatch table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ActionKindEntry:
    """Validator + executor pair per action_kind.

    Validator: pure function, returns (ok, error_string). Runs
    BEFORE covenant evaluation. Catches structural errors in
    work_order shape.

    Executor: async function, returns
    ``tuple[ReceiptStatus, list[CrossSpaceReceiptRef], str]`` —
    (status, created_refs, user_visible_summary). Performs the
    actual mutation through existing state APIs with provenance
    fields populated. Runs INSIDE the target-space lock.

    bypass_target_covenants: when True, the kernel does NOT invoke
    target covenant evaluation for this action_kind. Per Q2 safety
    valve, only ``propose_covenant`` sets this — proposals are
    always proposed-not-applied and the proposal entity carries
    the request capsule and audit.
    """

    validator: Callable[[CrossSpaceRequest], tuple[bool, str]]
    executor: Callable[
        [CrossSpaceRequest, "DispatchEngine"],
        Awaitable[tuple[ReceiptStatus, list[CrossSpaceReceiptRef], str]],
    ]
    bypass_target_covenants: bool = False


# Populated in executors.py at import time. Kept here so callers
# can introspect (e.g. tests, /capabilities-style surfaces) without
# pulling the full executor surface.
ACTION_KIND_DISPATCH: dict[str, _ActionKindEntry] = {}


def register_action_kind(
    kind: str,
    *,
    validator: Callable[[CrossSpaceRequest], tuple[bool, str]],
    executor: Callable[
        [CrossSpaceRequest, "DispatchEngine"],
        Awaitable[tuple[ReceiptStatus, list[CrossSpaceReceiptRef], str]],
    ],
    bypass_target_covenants: bool = False,
) -> None:
    if kind not in ALLOWED_ACTION_KINDS:
        raise ValueError(
            f"action_kind {kind!r} is not in the v1 whitelist; "
            f"allowed: {sorted(ALLOWED_ACTION_KINDS)}"
        )
    if kind in ACTION_KIND_DISPATCH:
        raise ValueError(f"action_kind {kind!r} already registered")
    ACTION_KIND_DISPATCH[kind] = _ActionKindEntry(
        validator=validator,
        executor=executor,
        bypass_target_covenants=bypass_target_covenants,
    )


# ---------------------------------------------------------------------------
# Dispatch engine
# ---------------------------------------------------------------------------


class DispatchEngine:
    """Bundles the substrate dependencies that executors need.

    Held by the kernel/handler at construction time and passed to
    each executor invocation. Keeps the dispatch module from having
    to thread state/events/audit through every function signature
    or import them at module top.
    """

    def __init__(
        self,
        *,
        state: Any,
        events: Any,
        audit: Any,
        gate: Any | None = None,
        space_locks: dict[tuple[str, str], asyncio.Lock] | None = None,
    ) -> None:
        self.state = state
        self.events = events
        self.audit = audit
        self.gate = gate
        self._space_locks = (
            space_locks if space_locks is not None else {}
        )

    def get_target_lock(self, instance_id: str, space_id: str) -> asyncio.Lock:
        key = (instance_id, space_id)
        if key not in self._space_locks:
            self._space_locks[key] = asyncio.Lock()
        return self._space_locks[key]


# ---------------------------------------------------------------------------
# Pre-flight validation
# ---------------------------------------------------------------------------


def _validate_envelope(
    req: CrossSpaceRequest,
) -> tuple[bool, str]:
    """Structural envelope checks — run before lock acquisition."""
    if req.action_kind not in ALLOWED_ACTION_KINDS:
        return (False, (
            f"action_kind {req.action_kind!r} not in v1 whitelist "
            f"({sorted(ALLOWED_ACTION_KINDS)})"
        ))
    if req.action_kind not in ACTION_KIND_DISPATCH:
        return (False, (
            f"action_kind {req.action_kind!r} has no dispatch entry "
            "registered (substrate bug)"
        ))
    if not req.request_id:
        return (False, "request_id is required")
    if not req.target_space_id:
        return (False, "target_space_id is required")
    if not req.origin_space_id:
        return (False, "origin_space_id is required")
    if not req.instance_id:
        return (False, "instance_id is required")
    if not isinstance(req.work_order, dict):
        return (False, "work_order must be a dict")
    return (True, "")


async def _validate_target_member_match(
    engine: DispatchEngine, req: CrossSpaceRequest,
) -> tuple[bool, str]:
    """Same member-set check. ContextSpace has a single member_id;
    origin and target must share the same owner (or both be
    instance-level).

    v1 deliberately rejects cross-member targets — those go through
    the existing Messenger / relational-dispatch path."""
    origin = await engine.state.get_context_space(
        req.instance_id, req.origin_space_id,
    )
    target = await engine.state.get_context_space(
        req.instance_id, req.target_space_id,
    )
    if target is None:
        return (False, f"target_space {req.target_space_id!r} does not exist")
    if origin is None:
        return (False, f"origin_space {req.origin_space_id!r} does not exist")
    o_member = (origin.member_id or "").strip()
    t_member = (target.member_id or "").strip()
    if o_member != t_member:
        return (False, (
            "cross-member targets are not supported in v1; use "
            "send_relational_message for member-to-member communication"
        ))
    return (True, "")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

_CROSS_SPACE_EVENT_TYPE: str = "cross_space.action"


async def _find_idempotent_receipt(
    engine: DispatchEngine, req: CrossSpaceRequest,
) -> CrossSpaceReceipt | None:
    """Look for a prior cross_space_action event for this
    request_id. Returns the original receipt if found, None
    otherwise."""
    if not engine.events or not hasattr(engine.events, "query"):
        return None
    try:
        # The events module's query surface — scoped to this
        # instance_id. The event payload was stamped with
        # request_id; we recover the receipt fields from the
        # payload.
        events = await engine.events.query(
            req.instance_id,
            event_types=[_CROSS_SPACE_EVENT_TYPE],
            limit=200,
        )
    except Exception as exc:
        logger.debug("CROSS_SPACE_IDEMPOTENT_LOOKUP_FAILED: %s", exc)
        return None
    for evt in events:
        payload = getattr(evt, "payload", {}) or {}
        if payload.get("request_id") != req.request_id:
            continue
        receipt = payload.get("receipt") or {}
        if not isinstance(receipt, dict):
            continue
        try:
            refs = tuple(
                CrossSpaceReceiptRef(type=r["type"], id=r["id"])
                for r in receipt.get("created_refs", [])
                if isinstance(r, dict) and "type" in r and "id" in r
            )
            return CrossSpaceReceipt(
                request_id=receipt.get("request_id", req.request_id),
                status=receipt.get("status", "completed"),
                target_space_id=receipt.get(
                    "target_space_id", req.target_space_id,
                ),
                timestamp=receipt.get("timestamp", ""),
                created_refs=refs,
                target_audit_ref=receipt.get("target_audit_ref", ""),
                provenance=dict(receipt.get("provenance", {})),
                user_visible_summary=receipt.get("user_visible_summary", ""),
                refusal_reason=receipt.get("refusal_reason", ""),
            )
        except Exception as exc:
            logger.debug("CROSS_SPACE_IDEMPOTENT_RECONSTRUCT_FAILED: %s", exc)
            return None
    return None


# ---------------------------------------------------------------------------
# Covenant evaluation hook (Q2)
# ---------------------------------------------------------------------------


async def _evaluate_target_covenants(
    engine: DispatchEngine, req: CrossSpaceRequest,
) -> tuple[ReceiptStatus | None, str]:
    """Run target-space covenants against the proposed action.

    Returns (None, "") when covenants pass — caller proceeds to
    execute. Returns (status, reason) when covenants block or
    transform — caller returns the receipt without executing.

    Q2 safety valve: action kinds with bypass_target_covenants=True
    skip evaluation entirely (proposals are always proposed-not-
    applied; the proposal entity carries the request capsule and
    audit).
    """
    entry = ACTION_KIND_DISPATCH.get(req.action_kind)
    if entry is None or entry.bypass_target_covenants:
        return (None, "")

    if engine.gate is None:
        # Gate not wired (typical in test contexts) — fail open.
        # Production wiring threads the DispatchGate through.
        logger.debug(
            "CROSS_SPACE_NO_GATE: skipping target covenant eval for "
            "request_id=%s action_kind=%s", req.request_id, req.action_kind,
        )
        return (None, "")

    try:
        # We synthesize a tool-shape payload for the gate's
        # LLM-evaluator: action_kind serves as the "tool name", the
        # work_order as "tool input". The gate queries covenants
        # scoped to target_space_id.
        from kernos.kernel.gate import GateResult  # noqa: F401 — runtime import only
        gate_result = await engine.gate.evaluate_cross_space(
            instance_id=req.instance_id,
            target_space_id=req.target_space_id,
            action_kind=req.action_kind,
            work_order=req.work_order,
            initiating_member_id=req.initiating_member_id,
        )
    except AttributeError:
        # The gate doesn't expose evaluate_cross_space yet — fail
        # open with a log entry. Production wiring lands the method
        # alongside this dispatch.
        logger.debug(
            "CROSS_SPACE_GATE_NO_METHOD: gate lacks "
            "evaluate_cross_space; skipping covenant eval"
        )
        return (None, "")
    except Exception as exc:
        logger.warning(
            "CROSS_SPACE_GATE_RAISED: %s — failing closed for safety", exc,
        )
        return ("refused", f"covenant evaluation raised: {exc}")

    decision = getattr(gate_result, "decision", "approved")
    if decision == "approved":
        return (None, "")
    if decision in ("covenant_conflict", "blocked"):
        reason = getattr(gate_result, "reason", "blocked by target covenant")
        return ("refused", reason)
    if decision in ("confirm", "needs_confirmation"):
        reason = getattr(gate_result, "reason", "target covenant requires confirmation")
        return ("needs_confirmation", reason)
    if decision in ("propose", "transform_to_proposal"):
        reason = getattr(gate_result, "reason", "target covenant requires proposal")
        return ("proposed", reason)
    # Unknown decision — fail closed.
    return ("refused", f"unrecognized covenant decision: {decision}")


# ---------------------------------------------------------------------------
# Audit + event emission
# ---------------------------------------------------------------------------


async def _emit_audit_and_event(
    engine: DispatchEngine,
    req: CrossSpaceRequest,
    receipt: CrossSpaceReceipt,
    *,
    suppress_audit: bool = False,
) -> str:
    """Emit the cross_space_action event into target's event stream
    (always) and write the audit entry (unless suppressed for
    same-space short-circuit). Returns the audit ref string for
    inclusion in the receipt."""
    audit_ref = ""

    # Target event stream — surfaces in target's awareness on next
    # entry (the assemble.py preamble queries this event_type).
    if engine.events is not None:
        try:
            from kernos.kernel.events import emit_event
            from kernos.kernel.event_types import EventType

            payload = {
                "request_id": req.request_id,
                "origin_space_id": req.origin_space_id,
                "target_space_id": req.target_space_id,
                "action_kind": req.action_kind,
                "initiating_member_id": req.initiating_member_id,
                "source_turn_id": req.source_turn_id,
                "work_order": req.work_order,
                "receipt": receipt.to_tool_result(),
            }
            await emit_event(
                engine.events,
                EventType.CROSS_SPACE_ACTION,
                req.instance_id,
                "cross_space",
                payload=payload,
            )
        except Exception as exc:
            logger.warning(
                "CROSS_SPACE_EVENT_EMIT_FAILED: request_id=%s %s",
                req.request_id, exc,
            )

    # Audit log — suppressed for same-space short-circuit (it's a
    # normal local action). Otherwise: always logged with full
    # request capsule.
    if not suppress_audit and engine.audit is not None:
        try:
            entry = {
                "type": _CROSS_SPACE_EVENT_TYPE,
                "request_id": req.request_id,
                "origin_space_id": req.origin_space_id,
                "target_space_id": req.target_space_id,
                "action_kind": req.action_kind,
                "status": receipt.status,
                "initiating_member_id": req.initiating_member_id,
                "source_turn_id": req.source_turn_id,
                "timestamp": receipt.timestamp,
                "created_refs": [
                    {"type": r.type, "id": r.id}
                    for r in receipt.created_refs
                ],
                "refusal_reason": receipt.refusal_reason,
            }
            await engine.audit.log(req.instance_id, entry)
            audit_ref = f"audit:{req.request_id}"
        except Exception as exc:
            logger.warning(
                "CROSS_SPACE_AUDIT_LOG_FAILED: request_id=%s %s",
                req.request_id, exc,
            )

    return audit_ref


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


async def dispatch_request(
    req: CrossSpaceRequest,
    engine: DispatchEngine,
    *,
    target_lock_timeout_seconds: float = DEFAULT_TARGET_LOCK_TIMEOUT_SECONDS,
) -> CrossSpaceReceipt:
    """Kernel entry point. Runs the full validate → idempotency →
    covenant → execute → audit pipeline and returns a receipt.

    Same-space requests short-circuit (no new lock acquired) per
    Q1 constraint. Cross-space requests acquire the target's
    per-space lock with bounded timeout; on timeout return ``failed``
    with ``refusal_reason='timeout_waiting_for_target'``.

    The reentrancy guard is acquired BEFORE validation so recursion
    rejection happens early.
    """
    token = enter_cross_space()  # may raise ReentrancyBlocked / DepthExceeded
    try:
        # ---- 1. Envelope structural validation ----
        ok, reason = _validate_envelope(req)
        if not ok:
            return _refused(req, reason)

        # ---- 2. Same-space short-circuit ----
        # Detected BEFORE lock acquisition (Q1 constraint).
        if req.target_space_id == req.origin_space_id:
            return await _dispatch_same_space(req, engine)

        # ---- 3. Membership match (cross-space only) ----
        ok, reason = await _validate_target_member_match(engine, req)
        if not ok:
            return _refused(req, reason)

        # ---- 4. Idempotency check ----
        prior = await _find_idempotent_receipt(engine, req)
        if prior is not None:
            logger.info(
                "CROSS_SPACE_IDEMPOTENT_HIT: request_id=%s — returning prior receipt",
                req.request_id,
            )
            return prior

        # ---- 5. Acquire target lock with bounded timeout (Q1) ----
        target_lock = engine.get_target_lock(
            req.instance_id, req.target_space_id,
        )
        try:
            await asyncio.wait_for(
                target_lock.acquire(),
                timeout=target_lock_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return _failed(
                req, "timeout_waiting_for_target",
                summary=(
                    f"target space {req.target_space_id} was busy for "
                    f"{target_lock_timeout_seconds:.0f}s; request was not applied"
                ),
            )

        try:
            # ---- 6. Inside the lock: covenants → mutate → emit ----
            return await _execute_under_lock(req, engine)
        finally:
            target_lock.release()

    finally:
        exit_cross_space(token)


async def _execute_under_lock(
    req: CrossSpaceRequest, engine: DispatchEngine,
) -> CrossSpaceReceipt:
    """Inside the target-space lock: covenant evaluation, mutation,
    audit/event emission, receipt construction. Per Q1: this whole
    block is one ordered unit; receipt returns after lock release
    (caller does the release in dispatch_request)."""

    # Covenant evaluation (Q2) — gate-bypass kinds skip this.
    cov_status, cov_reason = await _evaluate_target_covenants(engine, req)
    if cov_status is not None:
        # Covenant blocked / transformed: build the receipt without
        # invoking the executor.
        receipt = CrossSpaceReceipt(
            request_id=req.request_id,
            status=cov_status,
            target_space_id=req.target_space_id,
            timestamp=utc_now(),
            created_refs=(),
            target_audit_ref="",
            provenance=_provenance_dict(req),
            user_visible_summary=cov_reason,
            refusal_reason=cov_reason if cov_status == "refused" else "",
        )
        audit_ref = await _emit_audit_and_event(engine, req, receipt)
        return _with_audit_ref(receipt, audit_ref)

    # Per-action validator (work_order shape).
    entry = ACTION_KIND_DISPATCH[req.action_kind]
    ok, reason = entry.validator(req)
    if not ok:
        receipt = _refused(req, reason)
        audit_ref = await _emit_audit_and_event(engine, req, receipt)
        return _with_audit_ref(receipt, audit_ref)

    # Execute the mutation.
    try:
        status, refs, summary = await entry.executor(req, engine)
    except Exception as exc:
        logger.warning(
            "CROSS_SPACE_EXECUTOR_RAISED: action_kind=%s request_id=%s %s",
            req.action_kind, req.request_id, exc,
        )
        receipt = _failed(req, f"executor raised: {exc}")
        audit_ref = await _emit_audit_and_event(engine, req, receipt)
        return _with_audit_ref(receipt, audit_ref)

    receipt = CrossSpaceReceipt(
        request_id=req.request_id,
        status=status,
        target_space_id=req.target_space_id,
        timestamp=utc_now(),
        created_refs=tuple(refs),
        target_audit_ref="",
        provenance=_provenance_dict(req),
        user_visible_summary=summary,
        refusal_reason="",
    )
    audit_ref = await _emit_audit_and_event(engine, req, receipt)
    return _with_audit_ref(receipt, audit_ref)


async def _dispatch_same_space(
    req: CrossSpaceRequest, engine: DispatchEngine,
) -> CrossSpaceReceipt:
    """Same-space short-circuit: dispatch within the existing turn
    lock (which the message handler is already holding). Audit-log
    suppressed (it's a normal local action). Receipt is shaped
    consistently with cross-space so callers can use
    ``request_space_action`` uniformly."""
    entry = ACTION_KIND_DISPATCH.get(req.action_kind)
    if entry is None:
        return _refused(req, f"unknown action_kind: {req.action_kind!r}")

    ok, reason = entry.validator(req)
    if not ok:
        # Even on validation refusal we don't audit — same-space.
        return _refused(req, reason)

    try:
        status, refs, summary = await entry.executor(req, engine)
    except Exception as exc:
        logger.warning(
            "CROSS_SPACE_SAME_SPACE_EXECUTOR_RAISED: %s", exc,
        )
        return _failed(req, f"executor raised: {exc}")

    receipt = CrossSpaceReceipt(
        request_id=req.request_id,
        status=status,
        target_space_id=req.target_space_id,
        timestamp=utc_now(),
        created_refs=tuple(refs),
        target_audit_ref="",
        provenance=_provenance_dict(req),
        user_visible_summary=summary,
        refusal_reason="",
    )
    # Suppress audit + event for same-space short-circuit.
    return receipt


# ---------------------------------------------------------------------------
# Receipt helpers
# ---------------------------------------------------------------------------


def _provenance_dict(req: CrossSpaceRequest) -> dict[str, Any]:
    return {
        "origin_space_id": req.origin_space_id,
        "source_turn_id": req.source_turn_id,
        "initiating_member_id": req.initiating_member_id,
        "action_kind": req.action_kind,
        "request_id": req.request_id,
    }


def _refused(req: CrossSpaceRequest, reason: str) -> CrossSpaceReceipt:
    return CrossSpaceReceipt(
        request_id=req.request_id,
        status="refused",
        target_space_id=req.target_space_id,
        timestamp=utc_now(),
        created_refs=(),
        target_audit_ref="",
        provenance=_provenance_dict(req),
        user_visible_summary=f"refused: {reason}",
        refusal_reason=reason,
    )


def _failed(
    req: CrossSpaceRequest,
    refusal_reason: str,
    *,
    summary: str = "",
) -> CrossSpaceReceipt:
    return CrossSpaceReceipt(
        request_id=req.request_id,
        status="failed",
        target_space_id=req.target_space_id,
        timestamp=utc_now(),
        created_refs=(),
        target_audit_ref="",
        provenance=_provenance_dict(req),
        user_visible_summary=summary or f"failed: {refusal_reason}",
        refusal_reason=refusal_reason,
    )


def _with_audit_ref(
    receipt: CrossSpaceReceipt, audit_ref: str,
) -> CrossSpaceReceipt:
    """Attach a final audit_ref to the receipt. CrossSpaceReceipt
    is frozen, so build a new one with the audit_ref filled in."""
    if not audit_ref:
        return receipt
    return CrossSpaceReceipt(
        request_id=receipt.request_id,
        status=receipt.status,
        target_space_id=receipt.target_space_id,
        timestamp=receipt.timestamp,
        created_refs=receipt.created_refs,
        target_audit_ref=audit_ref,
        provenance=receipt.provenance,
        user_visible_summary=receipt.user_visible_summary,
        refusal_reason=receipt.refusal_reason,
    )


__all__ = [
    "ACTION_KIND_DISPATCH",
    "ALLOWED_ACTION_KINDS",
    "CrossSpaceReceipt",
    "CrossSpaceReceiptRef",
    "CrossSpaceRequest",
    "DEFAULT_TARGET_LOCK_TIMEOUT_SECONDS",
    "DispatchEngine",
    "dispatch_request",
    "new_request_id",
    "register_action_kind",
]
