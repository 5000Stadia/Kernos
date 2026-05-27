"""Dispatch Gate — loss-cost evaluator for tool call authorization.

Classifies tool effects, evaluates loss cost via lightweight LLM call,
manages approval tokens for confirmed actions.
"""
import hashlib
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event

logger = logging.getLogger(__name__)


# ============================================================
# POSTURE-EVALUATION-MODES-V1 (2026-05-22)
# ============================================================


@dataclass(frozen=True)
class GateModePolicy:
    """Per-mode tuning of DispatchGate.evaluate's behavior.

    Read at construction time. The policy is consulted at three
    branch points: reactive-soft_write bypass eligibility, the
    model system-prompt preamble, and the fallback for ambiguous
    model responses (CONFIRM or unparseable).
    """

    name: str  # "permissive" | "balanced" | "strict"

    # Bypass-rule overrides
    reactive_soft_write_auto_proceed: bool

    # Reserved for future spec: explicit "user named this exact
    # action" signal would unlock auto-bypass for hard_write
    # under permissive mode. v1 holds False across ALL modes
    # per POSTURE-V1 D3 Codex round 2 finding 1.
    reactive_hard_write_auto_proceed: bool

    # System-prompt preamble injected at the top of
    # _evaluate_model's system_prompt. Biases the model without
    # overriding APPROVE/CONFIRM/CONFLICT decision criteria.
    prompt_preamble: str

    # What to do when the model returns CONFIRM or an
    # unparseable response (the "ambiguous" branch).
    # CLARIFY is NEVER subject to this — it always blocks
    # with reason="clarify".
    ambiguous_fallback: Literal["proceed", "confirm", "refuse"]


_POLICY_PERMISSIVE = GateModePolicy(
    name="permissive",
    reactive_soft_write_auto_proceed=True,
    reactive_hard_write_auto_proceed=False,
    prompt_preamble=(
        "POSTURE: permissive. Default to APPROVE unless a covenant "
        "clearly blocks the action. If the user's intent is plausibly "
        "served by this action, approve. Reserve CONFIRM for actions "
        "that clearly exceed the user's request.\n\n"
    ),
    ambiguous_fallback="proceed",
)

_POLICY_BALANCED = GateModePolicy(
    name="balanced",
    reactive_soft_write_auto_proceed=True,
    reactive_hard_write_auto_proceed=False,
    prompt_preamble=(
        "POSTURE: balanced. Default to APPROVE for reactive actions "
        "matching the user's request. Use CONFIRM when the action "
        "exceeds the request or affects parties beyond the user.\n\n"
    ),
    ambiguous_fallback="confirm",
)

_POLICY_STRICT = GateModePolicy(
    name="strict",
    reactive_soft_write_auto_proceed=False,
    reactive_hard_write_auto_proceed=False,
    prompt_preamble=(
        "POSTURE: strict. Default to CONFIRM unless the action is "
        "read-only or the user named this exact action verbatim in "
        "their current message. Bias toward asking.\n\n"
    ),
    ambiguous_fallback="refuse",
)

_GATE_MODES: dict[str, GateModePolicy] = {
    "permissive": _POLICY_PERMISSIVE,
    "balanced": _POLICY_BALANCED,
    "strict": _POLICY_STRICT,
}


def get_mode_policy_by_name(name: str) -> GateModePolicy | None:
    """POSTURE-CONFIGURATION-V1 (2026-05-22): look up a policy
    by its mode name. Used by ``/posture mode`` to translate
    operator input + the bring-up hook that applies persisted
    mode at startup.

    Returns ``None`` if ``name`` isn't a recognized mode.
    Whitespace + case are normalized.
    """
    key = (name or "").strip().lower()
    return _GATE_MODES.get(key)


def _resolve_gate_mode_policy() -> GateModePolicy:
    """Read + normalize ``KERNOS_GATE_MODE``.

    Unset → ``permissive`` (default, behavior-neutral).
    Known value → that mode.
    Unknown value → ``strict`` + ERROR log (fail-loud + fall-safe).
    Silent permissive on a typo would silently loosen the gate.
    """
    raw = os.environ.get("KERNOS_GATE_MODE", "").strip().lower()
    if not raw:
        return _POLICY_PERMISSIVE
    if raw in _GATE_MODES:
        return _GATE_MODES[raw]
    logger.error(
        "KERNOS_GATE_MODE=%r unknown; falling back to 'strict' "
        "(fail-loud + fall-safe). Set KERNOS_GATE_MODE to "
        "permissive|balanced|strict.",
        raw,
    )
    return _POLICY_STRICT


def _action_keywords(tool_name: str, tool_input: dict) -> list[str]:
    """Extract keywords from a tool call for must_not rule relevance matching."""
    keywords = [tool_name.replace("-", " "), tool_name.replace("_", " ")]
    # Add action-specific keywords
    action = (tool_input or {}).get("action", "")
    if action:
        keywords.append(action)
    summary = (tool_input or {}).get("summary", "")
    if summary:
        keywords.extend(summary.lower().split()[:3])
    return keywords


@dataclass
class GateResult:
    """The outcome of a dispatch gate check."""

    allowed: bool
    reason: str    # "approved", "covenant_conflict", "confirm", "clarify", "token_approved"
    method: str    # "token", "model_check", "always_allow"
    proposed_action: str = ""    # Human-readable description of what was blocked
    conflicting_rule: str = ""   # For CONFLICT — which rule conflicts
    raw_response: str = ""       # Full model response for logging


@dataclass
class _CrossSpaceGateDecision:
    """CROSS_SPACE_REQUESTS_V1 (Q2): result shape returned by
    :meth:`DispatchGate.evaluate_cross_space`. Kept narrow — the
    dispatch flow only needs the decision token + reason."""

    decision: str   # "approved" | "covenant_conflict" | "needs_confirmation"
    reason: str = ""


@dataclass
class ApprovalToken:
    """Single-use token issued when the dispatch gate blocks an action."""

    token_id: str          # uuid hex[:12]
    tool_name: str
    tool_input_hash: str   # md5 hex[:8] of tool_input
    issued_at: datetime
    used: bool = False


class DispatchGate:
    """Loss-cost evaluator for tool call authorization.

    Three-step check:
    1. Approval token bypass (user confirmed this specific action)
    2. Permission override fast path (capability set to always-allow)
    3. Lightweight model call evaluating loss cost
    """

    def __init__(
        self,
        reasoning_service: Any,  # For complete_simple calls
        registry: Any,           # CapabilityRegistry for tool_effects
        state: Any,              # StateStore for covenant queries
        events: EventStream,
        mcp: Any = None,         # MCPClientManager for tool descriptions
        catalog: Any = None,     # ToolCatalog for metadata reads (Phase D)
    ) -> None:
        self._reasoning = reasoning_service
        self._registry = registry
        self._state = state
        self._events = events
        self._mcp = mcp
        # LIVE-DISPATCH-UNBLOCKER-V1 Phase D: catalog reference for
        # metadata reads (amortization tool_hash, future diagnostic
        # surface, future TOOL-INTROSPECTION-V1 consumer).
        self._catalog = catalog
        self._approval_tokens: dict[str, ApprovalToken] = {}
        # Per-turn denial tracking: {tool_name: consecutive_block_count}
        self._denial_counts: dict[str, int] = {}
        self._denial_limit = int(os.environ.get("KERNOS_GATE_DENIAL_LIMIT", "3"))
        # POSTURE-EVALUATION-MODES-V1 (2026-05-22): mode policy
        # consulted at reactive-soft_write bypass, _evaluate_model
        # preamble, and the ambiguous-branch fallback.
        self._mode_policy: GateModePolicy = _resolve_gate_mode_policy()
        # LIVE-DISPATCH-UNBLOCKER-V1 Phase B (2026-05-22): scoped
        # amortization layer per [[kernos-dispatch-gate-design-input]].
        # Gate.evaluate always runs evaluation; this cache collapses
        # the user-visible CONFIRM cost on stable bindings. In-memory
        # LRU; per-binding TTL. hard_write NEVER amortizes (every call
        # gets full eval per its semantics). Cache wipes on mode swap;
        # covenant-mutation invalidation is a documented follow-up.
        self._amortization_cache: OrderedDict[tuple, float] = OrderedDict()
        self._amortization_ttl_s = float(os.environ.get(
            "KERNOS_GATE_AMORTIZATION_TTL_SEC", "3600",
        ))
        self._amortization_max_entries = int(os.environ.get(
            "KERNOS_GATE_AMORTIZATION_MAX_ENTRIES", "256",
        ))
        logger.info(
            "GATE_MODE_RESOLVED mode=%s ambiguous_fallback=%s "
            "reactive_soft_write_bypass=%s",
            self._mode_policy.name,
            self._mode_policy.ambiguous_fallback,
            self._mode_policy.reactive_soft_write_auto_proceed,
        )

    def set_mode_policy(self, policy: GateModePolicy) -> None:
        """Swap the active gate mode policy. Used by
        ``/posture mode`` slash command (POSTURE-CONFIGURATION-V1)."""
        self._mode_policy = policy
        # LIVE-DISPATCH-UNBLOCKER-V1 Phase B: mode swap invalidates
        # amortization cache — a mode change is a posture shift that
        # could change the gate's decisions; stale cache entries
        # could let a more-permissive decision survive into a
        # more-strict posture window.
        self.clear_amortization_cache()
        logger.info(
            "GATE_MODE_SWAPPED mode=%s ambiguous_fallback=%s "
            "amortization_cache=cleared",
            policy.name, policy.ambiguous_fallback,
        )

    def clear_amortization_cache(self) -> None:
        """Wipe the amortization cache. Called by set_mode_policy +
        external invalidators (slash commands, tests, future
        covenant-mutation hook).

        LIVE-DISPATCH-UNBLOCKER-V1 Phase B (2026-05-22).
        """
        prior_count = len(self._amortization_cache)
        self._amortization_cache.clear()
        if prior_count:
            logger.info(
                "GATE_AMORTIZATION_CLEARED entries=%d", prior_count,
            )

    def _amortization_signature(
        self, *, instance_id: str, member_id: str, tool_name: str,
        effect: str, scope_key: str,
    ) -> tuple:
        """Build the stable-binding signature per
        [[kernos-dispatch-gate-design-input]]. Includes
        registration_hash for workspace tools when the catalog
        carries it (Phase D's get_metadata API surfaces this);
        kernel/MCP tools use tool_name as the stability marker
        (their identity IS stable across calls)."""
        tool_hash = tool_name  # default for kernel + MCP
        if self._catalog is not None:
            try:
                get_meta = getattr(self._catalog, "get_metadata", None)
                if callable(get_meta):
                    meta = get_meta(tool_name) or {}
                    h = (meta.get("registration_hash") or "").strip()
                    if h:
                        tool_hash = h
            except Exception:
                pass
        return (
            instance_id, member_id, tool_name, tool_hash, effect,
            scope_key or "global",
        )

    def _check_amortization(self, sig: tuple) -> bool:
        """Returns True if the signature has a fresh cache entry.
        Side-effect: evicts the entry if expired."""
        now = time.monotonic()
        expires_at = self._amortization_cache.get(sig)
        if expires_at is None:
            return False
        if expires_at < now:
            self._amortization_cache.pop(sig, None)
            return False
        # LRU touch
        self._amortization_cache.move_to_end(sig)
        return True

    def _record_amortization(self, sig: tuple) -> None:
        """Insert into the cache, evicting LRU if over capacity.
        Caller is responsible for skipping hard_write (which
        never amortizes per spec)."""
        expires_at = time.monotonic() + self._amortization_ttl_s
        self._amortization_cache[sig] = expires_at
        self._amortization_cache.move_to_end(sig)
        while len(self._amortization_cache) > self._amortization_max_entries:
            self._amortization_cache.popitem(last=False)

    def classify_tool_effect(
        self, tool_name: str, active_space: Any, tool_input: dict[str, Any] | None = None,
    ) -> str:
        """Classify a tool call's effect level.

        Returns: "read", "soft_write", "hard_write", or "unknown"
        """
        # SELF-CONTROLLED-LOOP-LIVENESS-V1 (2026-05-21): canonicalize
        # known model-hallucinated tool names before classification.
        # Live dispatch classifies BEFORE calling execute_tool; without
        # repair here the gate returns "unknown" and the live
        # dispatcher refuses before reasoning ever sees the call.
        # See kernos/kernel/tool_aliases.py.
        from kernos.kernel.tool_aliases import canonicalize_tool_name
        _canonical_name, _was_repaired = canonicalize_tool_name(tool_name)
        if _was_repaired:
            # TOOL-ALIAS-RECEIPT-V1 (2026-05-23): log here for trace
            # parity. The first-class TOOL_ALIAS_REPAIRED event is
            # emitted at the dispatch ingress in reasoning.execute_tool
            # because classify_tool_effect is sync and the receipt
            # path is async; classify-only repairs (e.g., concurrency-
            # safe checks) leave a log line but no event.
            import logging as _gate_logging
            _gate_logging.getLogger(__name__).info(
                "TOOL_ALIAS_REPAIR alias=%s canonical=%s context=classify",
                tool_name, _canonical_name,
            )
            tool_name = _canonical_name
        _KERNEL_READS = {
            "remember", "remember_details", "list_files", "read_file",
            "dismiss_whisper", "read_source", "read_soul",
            "request_tool", "inspect_state",
            "list_parcels", "inspect_parcel",
            # CANVAS-V1
            "canvas_list", "page_read", "page_list", "page_search",
            # REFERENCE-PRIMITIVE-V1 (read_doc retired here; canonical
            # docs reach via request_reference)
            "request_reference",
            # BROKER-ROLE classifications (2026-05-17): tools that
            # were surfaced but blocked at the gate because they
            # weren't in either _KERNEL_READS or _KERNEL_WRITES.
            # Without these, dispatch returns "unknown" and the
            # broker role can't actually fire.
            "read_coding_session_response",  # polls bridge response dir
            "diagnose_issue",                # reads trace + source + friction reports
            # SELF-ADMIN-TOOLS-V1 (2026-05-19): dump_context is pure
            # read-only introspection (writes a diagnostic file but
            # doesn't mutate substrate state — the file is an
            # artifact, like inspect_state's return value).
            "dump_context",
            # TOOL-INTROSPECTION-V1 (2026-05-22): pure read of the
            # catalog metadata. No state mutation. Composes prose
            # from get_metadata; agent reads the sentence.
            "inspect_tools",
            # GIT-OPERATIONS-PRIMITIVES-V1 (2026-05-22): read git
            # tools for the autonomous-improvement loop. All
            # workspace-guarded — refuse paths outside
            # data/<instance>/improvement_workspace/.
            "git_fetch",
            "git_rev_parse",
            "git_status",
            "git_diff_for_review",
            # SELF-TEST-GATE-V1 (2026-05-22): pytest runner, no
            # source mutation. workspace-guarded.
            "run_self_test_suite",
            # SELF-IMPROVEMENT-CLOSURE-V1 (AC9): read-only lookup
            # over friction_pattern_invariant. Pure DB read.
            "lookup_pattern_invariants",
            # NOTE: manage_channels was here pre-INTEGRATION-CAPABILITY-FIRST-V1
            # Batch 2 follow-up. It has action-dependent semantics
            # (list=read, enable/disable=soft_write); the kernel-reads
            # membership check fired before the action-dependent branch
            # below, so enable/disable were silently classified as
            # read at dispatch time. Per Fold 5 (architect verdict
            # 2026-05-03): moved into the action-dependent branch
            # below where the actual semantics live.
            # NOTE: manage_schedule had a hardcoded "read" return at
            # the per-tool branch below for the same reason; replaced
            # with action-aware classification below.
        }
        _KERNEL_WRITES = {
            "write_file", "delete_file", "manage_covenants",
            "update_soul", "manage_capabilities", "send_to_channel",
            "execute_code",
            "pack_parcel",
            # CANVAS-V1: page_write is soft_write (reversible — prior
            # versions retained as .v{N}.md). canvas_create is hard_write
            # (creates a new shared-state primitive — classified separately
            # below so the model-check path applies).
            "page_write",
            # CANVAS-GARDENER-PREFERENCE-CAPTURE: both preference tools
            # mutate canvas.yaml (pending_preferences + confirmed preferences
            # lists). Reversible — the confirm/discard action is explicit.
            "canvas_preference_extract",
            "canvas_preference_confirm",
            # REFERENCE-PRIMITIVE-V1: store_reference + create_collection
            # write user-data files; the four recovery primitives mutate
            # catalog state. All reversible (tombstone is reversible via
            # restore; supersede/move-to-canvas track provenance).
            "store_reference",
            "create_reference_collection",
            "move_reference_to_canvas",
            "mark_reference_superseded",
            "quarantine_reference",
            "restore_reference_from_quarantine",
            # RESPONSE-FIDELITY-V1 Batch 1.2 (2026-05-08): note_this
            # writes to KnowledgeEntry / Preference / CovenantRule
            # depending on kind. All three are reversible (entries
            # superseded; covenants tombstoned), so soft_write at the
            # gate. Without this entry the gate would classify as
            # unknown and both live execution seams would refuse the
            # call.
            "note_this",
            # BROKER-ROLE classifications (2026-05-17):
            # consult spawns an external CLI subprocess; from Kernos's
            # POV the side-effects (any code changes the subprocess
            # makes via its own tools) are external to the substrate
            # and not directly tracked here, but the call itself is
            # a paid mutation of broker state. soft_write so the gate
            # surfaces the dispatch through the standard write path
            # rather than blocking it as unknown.
            "consult",
            # ask_coding_session writes a structured request file to
            # the coding_session_bridge/requests/ directory; reversible
            # (request file can be deleted; the relayed work may
            # produce downstream code changes but those are tracked
            # downstream, not at this surface).
            "ask_coding_session",
            # 2026-05-22: request_space_action was registered as a
            # kernel tool but missing from the gate classification
            # table — live-integration dispatcher refused it as
            # 'unknown'. It writes a bounded typed mutation request
            # to another space; reversible (the target space's runner
            # can reject) → soft_write.
            "request_space_action",
            # SELF-IMPROVEMENT-CLOSURE-V1 (AC9): closure-machinery
            # tools. record_closure_attempt performs a bounded
            # SQLite insert (idempotent on episode). run_closure_probe
            # also soft_write — the PROBE HANDLER is read-only, but
            # the wrapper updates closure_attempt outcome + may
            # transition the friction pattern lifecycle to 'resolved'
            # and emit a substrate event. Those are soft writes.
            # lookup_pattern_invariants is read-only (in _KERNEL_READS).
            "record_closure_attempt",
            "run_closure_probe",
        }

        if tool_name in _KERNEL_READS:
            return "read"
        if tool_name == "manage_covenants":
            action = (tool_input or {}).get("action", "list")
            return "read" if action == "list" else "soft_write"
        if tool_name == "manage_capabilities":
            action = (tool_input or {}).get("action", "list")
            return "read" if action == "list" else "soft_write"
        if tool_name == "manage_channels":
            action = (tool_input or {}).get("action", "list")
            return "read" if action == "list" else "soft_write"
        if tool_name == "manage_members":
            action = (tool_input or {}).get("action", "list")
            return "read" if action == "list" else "soft_write"
        if tool_name == "manage_plan":
            action = (tool_input or {}).get("action", "status")
            return "read" if action == "status" else "soft_write"
        if tool_name == "respond_to_parcel":
            # accept triggers a permanent cross-member file delivery →
            # hard_write. decline is reversible / informational → soft_write.
            # POSTURE-V1 D2 audit (2026-05-22): RETAINED — accept is a
            # cross-member commitment with no substrate-side undo.
            action = (tool_input or {}).get("action", "")
            return "hard_write" if action == "accept" else "soft_write"
        if tool_name == "canvas_create":
            # POSTURE-GATE-CLASSIFICATION-V1 (2026-05-22): scope-aware.
            # personal = owner-only state, tombstone-able via
            # canvas.delete() → soft_write. specific/team = cross-member
            # notification fires at create-time (reasoning.py emits
            # NOTIFY events to declared members) + shared state →
            # hard_write. Unknown/missing scope defaults to hard_write
            # as the conservative fallback so a schema-bypass caller
            # never silently demotes.
            scope = (tool_input or {}).get("scope", "")
            if scope == "personal":
                return "soft_write"
            return "hard_write"
        if tool_name == "improve_kernos":
            # IMPROVEMENT-LOOP-WORKFLOW-V1 (2026-05-22): starts an
            # autonomous attempt against Kernos's own source.
            # hard_write: the attempt ultimately commits + pushes
            # + restarts the process. Receipt-bound at the COMMIT
            # gate (Step 5), not at attempt-start; this
            # classification ensures the substrate gates the
            # invocation through the full evaluate path.
            return "hard_write"
        if tool_name in ("git_commit", "git_push"):
            # GIT-OPERATIONS-PRIMITIVES-V1 (2026-05-22): both
            # operations are hard_write — git_commit creates a
            # commit in the worktree (visible via git log), and
            # git_push pushes to origin/main (visible to the
            # world). Receipt-bound — verified in the handler
            # against an operator-approved authorization.
            return "hard_write"
        if tool_name == "restart_self":
            # SELF-ADMIN-TOOLS-V1 (2026-05-19): execv replaces the
            # process — in-flight tasks die, including the calling
            # turn. Reversible only in the sense that the bot comes
            # back, but the calling turn's response is permanently
            # lost. hard_write so the gate evaluates the move with
            # the model + space-context, AND restart_self has its
            # own two-call confirm=true safeguard inside its handler.
            # Defense in depth: both layers gate.
            # POSTURE-V1 D2 audit (2026-05-22): RETAINED — process
            # death cannot be undone by the substrate; calling turn's
            # response is permanently lost.
            return "hard_write"
        if tool_name == "manage_schedule":
            # INTEGRATION-CAPABILITY-FIRST-V1 Batch 2 Fold 5: was
            # hardcoded "read" pre-fold despite enum supporting
            # create/update/pause/resume/remove which mutate trigger
            # state. Now action-aware: list = read, anything else
            # = soft_write (reversible — pause/resume/remove can
            # be undone via list-then-create-or-resume; create can
            # be remove'd; update produces a soft history).
            action = (tool_input or {}).get("action", "list")
            return "read" if action == "list" else "soft_write"
        if tool_name == "manage_workspace":
            action = (tool_input or {}).get("action", "list")
            return "read" if action == "list" else "soft_write"
        if tool_name == "register_tool":
            return "soft_write"
        if tool_name in _KERNEL_WRITES:
            return "soft_write"

        if not self._registry:
            return "unknown"

        for cap in self._registry.get_all():
            if tool_name in (cap.tool_effects or {}):
                return cap.tool_effects[tool_name]
            if tool_name in (cap.tools or []) and tool_name not in (cap.tool_effects or {}):
                return "unknown"

        return "unknown"

    def _get_capability_for_tool(self, tool_name: str) -> str | None:
        """Return the capability name that owns this tool, or None."""
        if not self._registry:
            return None
        for cap in self._registry.get_all():
            if tool_name in (cap.tools or []):
                return cap.name
            if tool_name in (cap.tool_effects or {}):
                return cap.name
        return None

    def _get_tool_description(self, tool_name: str) -> str:
        """Return the tool's description from the MCP manifest."""
        if self._mcp:
            try:
                for tool in self._mcp.get_tools():
                    if tool.get("name") == tool_name:
                        return tool.get("description", "")
            except Exception:
                pass
        return ""

    def _describe_action(self, tool_name: str, tool_input: dict) -> str:
        """Generate a human-readable description of a proposed tool call."""
        if tool_name == "create-event":
            return f"Create calendar event: '{tool_input.get('summary', 'an event')}' at {tool_input.get('start', 'unspecified time')}"
        if tool_name == "update-event":
            return f"Update calendar event: '{tool_input.get('summary', 'an event')}'"
        if tool_name == "delete-event":
            return f"Delete calendar event: '{tool_input.get('summary', 'an event')}'"
        if tool_name == "send-email":
            return f"Send email to {tool_input.get('to', 'someone')}: '{tool_input.get('subject', 'no subject')}'"
        if tool_name == "delete-email":
            return f"Delete email: {tool_input.get('id', 'a message')}"
        if tool_name == "delete_file":
            return f"Delete file: {tool_input.get('name', 'a file')}"
        if tool_name == "write_file":
            return f"Write/update file: {tool_input.get('name', 'a file')}"
        return f"Execute {tool_name} with {json.dumps(tool_input)[:200]}"

    async def evaluate(
        self,
        tool_name: str,
        tool_input: dict,
        effect: str,
        user_message: str,
        instance_id: str,
        active_space_id: str,
        messages: list[dict] | None = None,
        approval_token_id: str | None = None,
        agent_reasoning: str = "",
        is_reactive: bool = True,
        member_id: str = "",
    ) -> GateResult:
        """Full gate evaluation: token → denial limit → override → reactive bypass → model check.

        MESSENGER-IS-THE-VOICE exclusion: ``send_relational_message`` is
        unconditionally delegated to the Messenger cohort (Layer 3 welfare
        judgment). The dispatch gate does NOT intervene on cross-member
        relational exchanges. This is safe only because the Messenger hook
        in ``RelationalDispatcher.send`` fires on every RM-permitted
        exchange, after the permission matrix has authorized it. Any code
        change that makes Messenger's firing conditional on anything (even
        a feature flag) turns this exclusion into a privacy regression —
        the two invariants travel together.

        Exclude by tool-call name (not by intent, not by capability). All
        three intents — ``ask_question``, ``request_action``, ``inform`` —
        route through ``send_relational_message``, so one exclusion covers
        the full cross-member surface.
        """
        if tool_name == "send_relational_message":
            self._denial_counts.pop(tool_name, None)
            return GateResult(
                allowed=True,
                reason="messenger_delegated",
                method="messenger_handoff",
            )

        # Step 0: Denial limit — stop runaway retry loops.
        # DOCS-AUDIT-RECOVERY #6: surface an agent-readable message
        # in proposed_action so the agent sees WHY the call failed and
        # can pivot rather than retry. Without this, the agent gets a
        # generic block and may loop on the same approach.
        if self._denial_counts.get(tool_name, 0) >= self._denial_limit:
            logger.warning(
                "GATE_DENIAL_LIMIT: tool=%s attempts=%d action=deny",
                tool_name, self._denial_counts[tool_name],
            )
            describe = self._describe_action(tool_name, tool_input)
            limit_message = (
                f"You've hit the per-tool denial limit "
                f"({self._denial_limit} consecutive denials) for "
                f"{tool_name!r} this turn. Pick a different approach, "
                f"surface the friction to the user, or wait for the next "
                f"turn (counters reset). Proposed action: {describe}"
            )
            return GateResult(
                allowed=False,
                reason="denial_limit",
                method="denial_tracking",
                proposed_action=limit_message,
            )

        # Step 1: Approval token
        if approval_token_id and self.validate_approval_token(
            approval_token_id, tool_name, tool_input
        ):
            self._denial_counts.pop(tool_name, None)  # Reset on approval
            logger.info("GATE: token_validated tool=%s token=%s", tool_name, approval_token_id)
            return GateResult(allowed=True, reason="token_approved", method="token")

        # Step 2: Permission override
        cap_name = self._get_capability_for_tool(tool_name)
        if cap_name and self._state:
            try:
                tenant = await self._state.get_instance_profile(instance_id)
                if tenant and tenant.permission_overrides.get(cap_name) == "always-allow":
                    self._denial_counts.pop(tool_name, None)
                    logger.info("GATE: permission_override tool=%s cap=%s", tool_name, cap_name)
                    return GateResult(allowed=True, reason="permission_override", method="always_allow")
            except Exception as exc:
                logger.warning("Gate: permission override check failed: %s", exc)

        # Step 3: Reactive soft_write bypass
        # When the agent acts in response to user interaction and the action is
        # reversible (soft_write), skip the gate model.  The user established
        # intent through conversation — don't second-guess it.
        # must_not covenants still block; hard_write/unknown still go to model.
        # POSTURE-EVALUATION-MODES-V1 (2026-05-22): strict mode disables
        # this bypass — every soft_write goes through model evaluation.
        if (
            is_reactive
            and effect == "soft_write"
            and self._mode_policy.reactive_soft_write_auto_proceed
        ):
            # Reactive soft_write: user requested this action. Only fall through
            # to the gate model if a must_not rule MENTIONS this tool or capability.
            has_relevant_blocking_rule = False
            if self._state:
                try:
                    rules = await self._state.query_covenant_rules(
                        instance_id, context_space_scope=[active_space_id, None], active_only=True,
                    )
                    cap_name = self._get_capability_for_tool(tool_name) or ""
                    for r in rules:
                        if r.rule_type != "must_not":
                            continue
                        desc_lower = r.description.lower()
                        # Only relevant if the rule mentions this tool, capability, or action
                        if (tool_name in desc_lower
                                or (cap_name and cap_name in desc_lower)
                                or any(kw in desc_lower for kw in _action_keywords(tool_name, tool_input))):
                            has_relevant_blocking_rule = True
                            break
                except Exception:
                    pass
            if not has_relevant_blocking_rule:
                self._denial_counts.pop(tool_name, None)
                logger.info(
                    "GATE: reactive_soft_write tool=%s — user-initiated, skipping gate model",
                    tool_name,
                )
                return GateResult(allowed=True, reason="approved", method="reactive_soft_write")
            # has relevant must_not rules — fall through to model to check

        # Step 3.5: Scoped amortization (LIVE-DISPATCH-UNBLOCKER-V1
        # Phase B, per [[kernos-dispatch-gate-design-input]]).
        # hard_write NEVER amortizes — every call gets full eval
        # per its semantics (operator confirmation IS the per-call
        # event). read / soft_write / external_agent_read may
        # amortize on a stable binding.
        amortizable = effect in ("read", "soft_write", "external_agent_read")
        amortization_sig: tuple | None = None
        if amortizable:
            amortization_sig = self._amortization_signature(
                instance_id=instance_id, member_id=member_id,
                tool_name=tool_name, effect=effect,
                scope_key=active_space_id or "global",
            )
            if self._check_amortization(amortization_sig):
                self._denial_counts.pop(tool_name, None)
                logger.info(
                    "GATE: amortized tool=%s effect=%s scope=%s — "
                    "collapsed via stable binding cache",
                    tool_name, effect, active_space_id or "global",
                )
                return GateResult(
                    allowed=True,
                    reason="amortized",
                    method="amortization",
                )

        # Step 4: Model evaluation
        result = await self._evaluate_model(
            tool_name, tool_input, effect, messages, agent_reasoning,
            instance_id, active_space_id, user_message=user_message,
        )
        # Track denials / reset on approve
        if result.allowed:
            self._denial_counts.pop(tool_name, None)
            # LIVE-DISPATCH-UNBLOCKER-V1 Phase B: record into
            # amortization cache so the next call within TTL
            # short-circuits the model. Only record when the
            # model produced the approval — token / override /
            # reactive-soft_write paths already short-circuit
            # before this point and don't need cache entries
            # (their fast paths are already O(1)).
            if amortization_sig is not None:
                self._record_amortization(amortization_sig)
        else:
            self._denial_counts[tool_name] = self._denial_counts.get(tool_name, 0) + 1
        return result

    async def _evaluate_model(
        self,
        tool_name: str,
        tool_input: dict,
        effect: str,
        messages: list[dict] | None,
        agent_reasoning: str,
        instance_id: str,
        active_space_id: str,
        user_message: str = "",
    ) -> GateResult:
        """Lightweight model evaluation for loss-cost assessment."""
        # Build recent_messages_text
        recent_messages_text = "No recent messages."
        if messages:
            user_msgs = [m for m in messages if m.get("role") == "user"][-5:]
            if user_msgs:
                lines = []
                for m in user_msgs:
                    content = m.get("content", "")
                    if isinstance(content, str):
                        lines.append(f'- "{content[:300]}"')
                    elif isinstance(content, list):
                        text = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                        if text:
                            lines.append(f'- "{text[:300]}"')
                if lines:
                    recent_messages_text = "\n".join(lines)

        # Build rules_text
        rules_text = "No standing covenant rules."
        rules_count = 0
        must_not_rules: list[str] = []
        if self._state:
            try:
                rules = await self._state.query_covenant_rules(
                    instance_id, context_space_scope=[active_space_id, None], active_only=True,
                )
                rule_lines = []
                for r in rules:
                    rule_lines.append(
                        f"- [{r.rule_type}] {r.description} (scope: {r.context_space or 'global'})"
                    )
                    if r.rule_type == "must_not":
                        must_not_rules.append(r.description)
                if rule_lines:
                    rules_count = len(rule_lines)
                    rules_text = "\n".join(rule_lines)
            except Exception as exc:
                logger.warning("Gate: covenant query failed: %s", exc)

        action_desc = self._describe_action(tool_name, tool_input)
        tool_description = self._get_tool_description(tool_name)

        # POSTURE-EVALUATION-MODES-V1 (2026-05-22): mode preamble
        # injected at the top of the system_prompt to bias the
        # model toward the configured posture.
        system_prompt = (
            self._mode_policy.prompt_preamble
            + "You are a safety gate for an AI assistant's actions.\n\n"
            "FIRST, determine: is this action a direct fulfillment of the user's "
            "current request? The user's request IS the authorization — do not "
            "re-confirm what the user already asked for.\n\n"
            "Answer with ONE of these:\n\n"
            "APPROVE — The action directly fulfills what the user asked for in their "
            "current message. The user said 'set an appointment' and the agent is "
            "creating the appointment. Or: the action is low-cost and easily reversible.\n"
            "CONFIRM — The action was NOT requested by the user (agent is acting "
            "proactively), OR goes beyond what the user asked for, OR affects someone "
            "other than the user (sending messages to third parties), OR could cause "
            "significant irreversible data loss.\n"
            "CONFLICT: <exact rule text> — A standing must_not covenant rule blocks "
            "this action. Copy the exact rule text after the colon.\n"
            "CLARIFY — The user's request is ambiguous — it could mean multiple things "
            "with meaningfully different outcomes.\n\n"
            "Key principle: reactive actions that serve the user's request → APPROVE. "
            "Proactive actions the user didn't ask for → evaluate normally.\n\n"
            "Rules:\n"
            "- If the user explicitly addresses a restriction (\"no need to review, "
            "just send it\"), that is an override — return APPROVE, not CONFLICT.\n"
            "- If a must_not rule genuinely applies and the user did NOT address it, "
            "return CONFLICT: <that rule's exact text>.\n"
            "- When in doubt between APPROVE and CONFIRM, choose CONFIRM.\n\n"
            "For CONFLICT, use format: CONFLICT: <rule text>\n"
            "For all others, answer with ONLY the one word."
        )
        current_request = ""
        if user_message:
            current_request = f"Current user request:\n\"{user_message[:500]}\"\n\n"

        user_content = (
            f"{current_request}"
            f"Recent user messages (oldest to newest):\n{recent_messages_text}\n\n"
            f"Agent's reasoning for this action:\n{agent_reasoning}\n\n"
            f"Proposed action: {tool_name}\n"
            f"Tool description: {tool_description}\n"
            f"Action details: {action_desc}\n\n"
            f"Active covenant rules:\n{rules_text}"
        )

        raw = ""
        logger.info("GATE_MODEL: max_tokens=512, has_schema=False, rules=%d", rules_count)
        try:
            raw = await self._reasoning.complete_simple(
                system_prompt=system_prompt,
                user_content=user_content,
                max_tokens=512,
                prefer_cheap=True,
            )
        except Exception as exc:
            logger.warning("Gate: model evaluation failed: %s", exc)
        logger.info("GATE_MODEL: raw_response=%r", raw[:300])

        stripped = raw.strip()
        first_word = stripped.split()[0].upper() if stripped else ""
        if first_word in ("APPROVE", "EXPLICIT", "AUTHORIZED"):
            return GateResult(allowed=True, reason="approved", method="model_check", raw_response=raw)
        if first_word.startswith("CONFLICT"):
            conflicting_rule = ""
            if ":" in stripped:
                conflicting_rule = stripped.split(":", 1)[1].strip()
            if not conflicting_rule:
                conflicting_rule = must_not_rules[0] if must_not_rules else ""
            return GateResult(
                allowed=False, reason="covenant_conflict", method="model_check",
                proposed_action=action_desc, conflicting_rule=conflicting_rule, raw_response=raw,
            )
        if first_word == "CLARIFY":
            # CLARIFY is the model's explicit "genuinely ambiguous"
            # signal — NEVER subject to ambiguous_fallback. Always
            # blocks with reason="clarify" regardless of mode.
            return GateResult(
                allowed=False, reason="clarify", method="model_check",
                proposed_action=action_desc, raw_response=raw,
            )
        # POSTURE-EVALUATION-MODES-V1 (2026-05-22): ambiguous
        # branch (model said CONFIRM or returned junk) — fallback
        # depends on the configured mode.
        if self._mode_policy.ambiguous_fallback == "proceed":
            return GateResult(
                allowed=True, reason="approved_by_mode",
                method=f"mode_{self._mode_policy.name}",
                raw_response=raw,
            )
        if self._mode_policy.ambiguous_fallback == "refuse":
            return GateResult(
                allowed=False, reason="refused_by_mode",
                method=f"mode_{self._mode_policy.name}",
                proposed_action=action_desc, raw_response=raw,
            )
        # ambiguous_fallback == "confirm" — preserve prior behavior.
        return GateResult(
            allowed=False, reason="confirm", method="model_check",
            proposed_action=action_desc, raw_response=raw,
        )

    async def evaluate_cross_space(
        self,
        *,
        instance_id: str,
        target_space_id: str,
        action_kind: str,
        work_order: dict,
        initiating_member_id: str = "",
    ) -> "_CrossSpaceGateDecision":
        """Evaluate target-space covenants for a cross-space request.

        CROSS_SPACE_REQUESTS_V1 (Q2): wraps the LLM-based covenant
        evaluator with target_space_id scope, translating
        GateResult into dispatch tokens (approved / covenant_conflict
        / needs_confirmation). The dispatch flow consumes the
        ``decision`` + ``reason`` fields.

        Action kinds with the bypass-target-covenants safety valve
        (``propose_covenant``) skip this method entirely; the
        dispatch module checks the registry flag before calling
        here.
        """
        gate_result = await self._evaluate_model(
            tool_name=f"cross_space:{action_kind}",
            tool_input=dict(work_order or {}),
            effect="hard_write",
            messages=None,
            agent_reasoning=(
                f"Cross-space request from member {initiating_member_id} "
                f"targeting {target_space_id} with action_kind="
                f"{action_kind}."
            ),
            instance_id=instance_id,
            active_space_id=target_space_id,  # scope to TARGET, not origin
            user_message="",
        )

        if gate_result.allowed:
            return _CrossSpaceGateDecision(
                decision="approved",
                reason=gate_result.reason or "approved",
            )
        # Map blocked/confirm/clarify → dispatch tokens.
        if gate_result.reason == "covenant_conflict":
            return _CrossSpaceGateDecision(
                decision="covenant_conflict",
                reason=(
                    gate_result.conflicting_rule
                    or "blocked by target covenant"
                ),
            )
        if gate_result.reason in ("confirm", "clarify"):
            return _CrossSpaceGateDecision(
                decision="needs_confirmation",
                reason=(
                    gate_result.proposed_action
                    or "target covenant requires confirmation"
                ),
            )
        # Unknown shape — fail closed.
        return _CrossSpaceGateDecision(
            decision="covenant_conflict",
            reason=f"unrecognized gate result reason: {gate_result.reason!r}",
        )

    def reset_denial_counts(self) -> None:
        """Reset per-turn denial counters. Call at the start of each turn."""
        self._denial_counts.clear()

    def issue_approval_token(self, tool_name: str, tool_input: dict) -> ApprovalToken:
        """Issue a single-use approval token for a blocked tool call."""
        token_id = uuid.uuid4().hex[:12]
        input_hash = hashlib.md5(
            json.dumps(tool_input, sort_keys=True).encode()
        ).hexdigest()[:8]
        token = ApprovalToken(
            token_id=token_id,
            tool_name=tool_name,
            tool_input_hash=input_hash,
            issued_at=datetime.now(timezone.utc),
        )
        self._approval_tokens[token_id] = token
        return token

    def validate_approval_token(
        self, token_id: str, tool_name: str, tool_input: dict,
    ) -> bool:
        """Validate an approval token. Marks it used on success."""
        token = self._approval_tokens.get(token_id)
        if not token:
            return False
        if token.used:
            return False
        if token.tool_name != tool_name:
            return False
        age_seconds = (datetime.now(timezone.utc) - token.issued_at).total_seconds()
        if age_seconds > 300:
            return False
        input_hash = hashlib.md5(
            json.dumps(tool_input, sort_keys=True).encode()
        ).hexdigest()[:8]
        if input_hash != token.tool_input_hash:
            return False
        token.used = True
        return True

    def cleanup_expired_tokens(self) -> None:
        """Remove expired or used approval tokens."""
        now = datetime.now(timezone.utc)
        expired = [
            tid for tid, token in self._approval_tokens.items()
            if token.used or (now - token.issued_at).total_seconds() > 300
        ]
        for tid in expired:
            del self._approval_tokens[tid]
