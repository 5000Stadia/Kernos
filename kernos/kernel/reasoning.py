"""Reasoning Service — the kernel's LLM abstraction layer.

The handler calls ``ReasoningService.reason()`` instead of importing any provider SDK.
ReasoningService owns the full tool-use loop, event emission, and audit logging.
"""
from kernos.utils import utc_now
import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event, estimate_cost
from kernos.kernel.exceptions import (
    ChainPayloadTooLarge,
    LLMChainExhausted,
    ReasoningConnectionError,
    ReasoningProviderError,
    ReasoningRateLimitError,
    ReasoningTimeoutError,
)
from kernos.kernel.token_estimator import estimate_tokens

logger = logging.getLogger(__name__)

_PROVIDER = "anthropic"
_SIMPLE_MODEL = "claude-sonnet-4-6"  # Used by complete_simple()
_CHEAP_MODEL = "claude-haiku-4-5-20251001"  # Used by complete_simple() when prefer_cheap=True

_OPENAI_SIMPLE_MODEL = "gpt-4o"      # Used by complete_simple() for OpenAI
_OPENAI_CHEAP_MODEL = "gpt-4o-mini"  # Used by complete_simple(prefer_cheap=True) for OpenAI

# Tool result budgeting — Stage 1 of Tool Execution Mediation.
# MCP results exceeding this threshold are persisted to the space file store
# and replaced with a bounded preview + file reference.
TOOL_RESULT_CHAR_BUDGET = 4000  # ~1000 tokens


# Tool schemas extracted to kernos/kernel/tools/schemas.py
from kernos.kernel.tools import (
    REQUEST_TOOL, REMEMBER_DETAILS_TOOL,
    MANAGE_CAPABILITIES_TOOL, READ_SOURCE_TOOL,
    READ_SOUL_TOOL, UPDATE_SOUL_TOOL, SOUL_UPDATABLE_FIELDS,
    read_source as _read_source,
    SOUL_UPDATABLE_FIELDS as _SOUL_UPDATABLE_FIELDS,
)


# ---------------------------------------------------------------------------
# KERNOS-native content types — no provider types leak past this module
# ---------------------------------------------------------------------------


# Provider types re-exported for backward compatibility
from kernos.providers.base import ChainConfig, ChainEntry, ContentBlock, Provider, ProviderResponse


from kernos.providers.anthropic_provider import AnthropicProvider  # re-export
from kernos.providers.codex_provider import OpenAICodexProvider  # re-export
from kernos.kernel.gate import DispatchGate, GateResult, ApprovalToken  # re-export


# OpenAICodexProvider extracted to kernos/providers/codex_provider.py
# ---------------------------------------------------------------------------
# Request / Result types
# ---------------------------------------------------------------------------


# 2026-05-23 dump_context accounting fix: single-process active-
# ReasoningService registry. ReasoningService.__init__ self-
# registers; static helpers (handle_dump_context_tool) reach the
# active instance via get_active_reasoning_service() to read the
# cached last-payload without import-cycle pressure.
_ACTIVE_REASONING_SERVICE: Any = None


def _set_active_reasoning_service(svc: Any) -> None:
    global _ACTIVE_REASONING_SERVICE
    _ACTIVE_REASONING_SERVICE = svc


def get_active_reasoning_service() -> Any:
    """Return the most-recently-constructed ReasoningService, or None.
    Used by static helpers that need access to per-instance reasoning
    caches without a direct reference."""
    return _ACTIVE_REASONING_SERVICE


@dataclass
class ReasoningRequest:
    """Everything the ReasoningService needs to run a reasoning turn."""

    instance_id: str
    conversation_id: str
    system_prompt: str
    messages: list[dict]
    tools: list[dict]
    model: str
    trigger: str
    max_tokens: int = 64000  # Sonnet/Opus output limit — let the model decide when to stop
    active_space_id: str = ""  # For kernel tool routing (e.g., remember)
    member_id: str = ""        # Current member — for per-member tool writes
    input_text: str = ""       # Current user message — used by dispatch gate
    active_space: Any = None   # ContextSpace | None — for gate tool effect classification
    user_timezone: str = ""    # IANA timezone from soul — for scheduler extraction
    is_reactive: bool = True   # True when responding to a user message; False for scheduler/background
    system_prompt_static: str = ""   # Cacheable prefix (RULES + ACTIONS)
    system_prompt_dynamic: str = ""  # Fresh per turn (NOW + STATE + RESULTS + MEMORY)
    trace: Any = None  # TurnEventCollector — for runtime trace instrumentation
    # MODEL-AND-STATUS-V1: per-(member, space) chain switch + head
    # override loaded from instance.db.model_overrides at dispatch
    # construction time. None when no override is set. Shape mirrors
    # InstanceDB.get_model_override return value:
    #   {"chain_name": str | None, "override_provider": str | None,
    #    "override_model": str | None, "set_at": str}
    # Consumed by build_resilient_chain_caller (CHAIN-CALLER-
    # PARITY-V1) via resolve_effective_chain.
    model_override: dict | None = None
    # COGNITIVE-CONTEXT-V1 C3a: typed cognitive substrate constructed
    # by the assemble phase. The decoupled path threads this through
    # _run_via_turn_runner_provider -> TurnRunnerInputs -> Integration
    # -> Briefing -> PresenceRenderer. Legacy path keeps consuming
    # system_prompt_static / system_prompt_dynamic strings (dual
    # carry per Codex C3a-design Q4 — legacy as oracle, packet as
    # canonical for decoupled). Optional + default-None preserves
    # backward compat for non-handler ReasoningRequest construction
    # sites (scheduler, plan execution, test fixtures).
    cognitive_context: Any = None
    # TOOL-AUDIT-NORMALIZATION-V1 (2026-05-22): when the live
    # dispatcher constructs a canonical audit entry, it passes
    # the entry_id through here so downstream paths (workspace
    # service-bound dispatch, future audit emitters) can detect
    # "canonical entry already exists, suppress my own emission."
    # Empty string = no canonical entry (e.g., direct kernel-tool
    # paths that don't flow through the live dispatcher).
    audit_entry_id: str = ""


# GateResult and ApprovalToken extracted to kernos/kernel/gate.py (re-exported above)

@dataclass
class PendingAction:
    """A tool call blocked by the dispatch gate, awaiting user confirmation.

    Stored on the ReasoningService keyed by instance_id. The handler executes
    confirmed actions after the agent signals [CONFIRM:N] in its response.
    """

    tool_name: str
    tool_input: dict
    proposed_action: str      # Human-readable description
    conflicting_rule: str     # Populated for CONFLICT; empty for DENIED
    gate_reason: str          # "covenant_conflict" or "denied"
    expires_at: datetime      # 5 minutes from creation (UTC)


@dataclass
class ReasoningResult:
    """The outcome of a reasoning turn."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    duration_ms: int
    tool_iterations: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _block_to_api_dict(block: ContentBlock) -> dict:
    """Convert a ContentBlock to an Anthropic API-compatible dict for continuation messages."""
    if block.type == "text":
        return {"type": "text", "text": block.text or ""}
    if block.type == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id or "",
            "name": block.name or "",
            "input": block.input or {},
        }
    return {"type": block.type}


# ---------------------------------------------------------------------------
# ReasoningService
# ---------------------------------------------------------------------------


def _build_chains_from_legacy(
    provider: Provider,
    fallback_providers: list[Provider] | None = None,
    fallback_provider: Provider | None = None,
) -> ChainConfig:
    """Synthesize a ChainConfig from old-style provider + fallback args.

    Used by tests and legacy call sites that construct ReasoningService with
    positional provider arguments instead of the new chains kwarg.
    """
    fallbacks = list(fallback_providers or [])
    if fallback_provider and fallback_provider not in fallbacks:
        fallbacks.append(fallback_provider)

    all_providers = [provider] + fallbacks
    # Two-tier chain model: primary + lightweight. Legacy code that
    # passed positional provider args picks up both tiers here. Providers
    # still expose the old ``simple_model`` / ``cheap_model`` attributes
    # as aliases (see AnthropicProvider + ollama/codex providers).
    return {
        "primary": [ChainEntry(provider=p, model=getattr(p, "main_model", "unknown")) for p in all_providers],
        "lightweight": [ChainEntry(provider=p, model=getattr(p, "lightweight_model", getattr(p, "cheap_model", _CHEAP_MODEL))) for p in all_providers],
    }


class ReasoningService:
    """Owns the full tool-use reasoning loop. Provider-agnostic.

    Emits reasoning.request, reasoning.response, tool.called, tool.result events.
    Logs tool calls and results to the audit store.
    Raises ReasoningError subtypes on provider failure — does NOT catch them.

    CONSTRUCTION CONTRACT (REASONING-SERVICE-CONSTRUCTION-PARITY-V1,
    2026-05-03): every entry point that constructs ReasoningService
    MUST wire ``turn_runner_provider`` via the shared helper at
    ``kernos.kernel.turn_runner_provider``:

        from kernos.kernel.turn_runner_provider import (
            build_turn_runner_provider,
            setup_default_thin_path_context,
            wire_live_thin_path,
        )

        ctx = setup_default_thin_path_context(...)
        reasoning = ReasoningService(
            ...,
            turn_runner_provider=build_turn_runner_provider(ctx),
        )
        handler = MessageHandler(..., reasoning=reasoning, ...)
        wire_live_thin_path(ctx, reasoning=reasoning, handler=handler)

    Do NOT copy-paste the closure pattern from server.py / repl.py
    into a new launcher. The shared helper is the canonical
    construction surface; copy-paste IS the failure mode this
    contract exists to prevent. Per Kit's framing 2026-05-03 this is
    the fourth instance of "canonical source + derived consumers +
    parity pins" in recent architecture work (after CCV1 field-
    provenance, gate-at-dispatch, and KERNEL-TOOL-REGISTRY-V1).

    The pin test at
    ``tests/test_reasoning_service_construction_parity.py`` walks
    every callsite that constructs ReasoningService and asserts
    each either uses the shared helper or appears in a documented
    exclusion list with rationale. Adding a new launcher without
    the shared helper fails CI.
    """

    MAX_TOOL_ITERATIONS = 10
    MAX_TOOL_ITERATIONS_PLAN = 25  # Self-directed plan steps need more room for research

    def __init__(
        self,
        provider: Provider | None = None,
        events: EventStream | None = None,
        mcp: Any = None,    # MCPClientManager — Any avoids circular import with capability layer
        audit: Any = None,  # AuditStore
        fallback_providers: list[Provider] | None = None,
        # Legacy single fallback — converted to list internally
        fallback_provider: Provider | None = None,
        *,
        chains: ChainConfig | None = None,
        trace_sink: list[dict] | None = None,  # IWL C2: shared tool-trace seam
        turn_runner_provider: Any = None,  # IWL C6: per-turn factory; takes (request, event_emitter) -> (TurnRunner, ProductionResponseDelivery)
        action_record_sink: list | None = None,  # RESPONSE-FIDELITY-V1 Batch 1.3: shared ActionStateRecord seam (mirrors trace_sink)
    ) -> None:
        if chains is not None:
            self._chains = chains
            self._provider = chains["primary"][0].provider
        else:
            assert provider is not None, "Either provider or chains must be supplied"
            self._provider = provider
            self._chains = _build_chains_from_legacy(provider, fallback_providers, fallback_provider)
        self._events = events
        self._mcp = mcp
        self._audit = audit
        self._retrieval = None  # Set by handler after construction (avoids circular import)
        self._files = None      # Set by handler after construction
        self._registry = None   # Set by handler after construction
        self._state = None      # Set by handler after construction
        self._channel_registry = None  # Set by handler after construction
        self._trigger_store = None     # Set by handler after construction
        self._handler = None           # Set by handler after construction (for schedule tool)
        self._canvas = None            # Set by handler after construction (CanvasService)
        self._gate: DispatchGate | None = None  # Created lazily after registry/state are set
        self._pending_actions: dict[str, list[PendingAction]] = {}  # instance_id → list
        self._conflict_raised_this_turn: bool = False  # Set when gate blocks; cleared at turn start
        self._tools_changed: bool = False  # Set by manage_capabilities; handler checks post-reasoning
        # Lazy tool loading: tracks which MCP tools have been loaded per-space session
        self._loaded_tools: dict[str, set[str]] = {}  # space_id → set of tool names
        # Turn-level tool call trace — accumulated during reasoning, read+cleared by handler.
        # IWL C2: when `trace_sink` is provided at construction, the new
        # path's StepDispatcher writes into the same backing list so
        # `drain_tool_trace()` returns entries from BOTH paths uniformly.
        # Default: a fresh internal list (back-compat with legacy-only
        # callers).
        self._turn_tool_trace: list[dict] = (
            trace_sink if trace_sink is not None else []
        )
        # RESPONSE-FIDELITY-V1 Batch 1.2 (2026-05-08): per-turn collector
        # for ActionStateRecords populated by tool handlers (currently
        # only note_this; existing surfaces migrate in Batch 2 onward).
        # When ``action_record_sink`` is injected at construction
        # (production wiring; mirrors trace_sink pattern), the runner's
        # peek-callable reads from the same backing list so records
        # land on Briefing.audit_trace.action_state_records without
        # preventing the handler-level drain that feeds the conv-log
        # "Action state this turn" block.
        self._turn_action_records: list = (
            action_record_sink if action_record_sink is not None else []
        )
        # Hybrid token counting: real input_tokens from last principal reasoning call per-instance
        self._last_real_input_tokens: dict[str, int] = {}  # instance_id → tokens
        # 2026-05-23 dump_context accounting fix: cache the most-recent
        # reasoning payload per instance so tool-dispatched dump_context
        # (which receives a minimal ReasoningRequest from the live-
        # dispatch _request_factory with empty system_prompt/messages/
        # tools) can render an accurate token + char summary. Each
        # entry replaces the prior — bounded by instance count, not
        # turn count.
        self._last_reasoning_payload: dict[str, dict] = {}
        # Self-register as the active reasoning service so
        # static helpers (e.g. handle_dump_context_tool) can
        # reach this instance without import-cycle gymnastics.
        # Single-process Kernos = one ReasoningService; last
        # constructed wins.
        _set_active_reasoning_service(self)
        # Pre-flight chain-skip support — lazily-loaded model registry
        # cards keyed by model name. Loaded once per ReasoningService
        # lifetime; refreshes happen out-of-process via `python -m
        # kernos.models`. Empty dict marks "tried and failed/empty".
        self._catalog_cards: dict[str, Any] | None = None
        # Track unknown-model warnings so we log each at most once per
        # ReasoningService process to avoid log spam.
        self._unknown_model_warned: set[str] = set()
        # IWL C6: per-turn factory. Builds a fresh TurnRunner +
        # ProductionResponseDelivery per call so the synthetic
        # reasoning.* events fire correctly and per-turn telemetry
        # binding works. Required post-CCV1-C7-strike (2026-05-03);
        # see CONSTRUCTION CONTRACT block in this class's docstring
        # and kernos.kernel.turn_runner_provider.
        self._turn_runner_provider = turn_runner_provider

    @staticmethod
    def _trace(request: "ReasoningRequest", level: str, source: str, event: str, detail: str, **kw: Any) -> None:
        """Record a trace event if collector is available."""
        if request and getattr(request, 'trace', None):
            request.trace.record(level, source, event, detail, **kw)

    def _get_catalog_cards(self) -> dict[str, Any]:
        """Return the lazily-loaded model registry cards, keyed by name.

        Returns an empty dict if the registry could not be loaded or is
        empty. Catalog load failures are non-fatal: chain dispatch
        falls back to the existing tolerant behaviour for any model
        without a card.
        """
        if self._catalog_cards is not None:
            return self._catalog_cards
        try:
            from kernos.models import load_catalog
            result = load_catalog()
            self._catalog_cards = dict(result.cards)
            for w in result.warnings:
                logger.info("MODEL_CATALOG_WARNING: %s", w)
        except Exception as exc:
            logger.warning("MODEL_CATALOG_LOAD_FAILED: %s", exc)
            self._catalog_cards = {}
        return self._catalog_cards

    @staticmethod
    def _context_safety_margin() -> float:
        """Per-call safety margin applied to each entry's effective ceiling.

        Default ten percent. Set KERNOS_CONTEXT_SAFETY_MARGIN to a
        float between 0 and 1 to override. Values outside that range
        are ignored and the default is used.
        """
        import os
        raw = os.environ.get("KERNOS_CONTEXT_SAFETY_MARGIN", "")
        try:
            value = float(raw)
        except ValueError:
            return 0.10
        if 0.0 <= value < 1.0:
            return value
        return 0.10

    def _warn_unknown_model_once(self, model: str) -> None:
        """Log once-per-process for a configured model with no catalog card."""
        if model in self._unknown_model_warned:
            return
        self._unknown_model_warned.add(model)
        logger.info(
            "MODEL_NOT_IN_CATALOG: %s — pre-flight context-window skip "
            "is disabled for this model. Add an entry to the overlay "
            "file at data/models/overlay.yaml to enable it.",
            model,
        )

    def _get_gate(self) -> DispatchGate:
        """Lazy gate creation — registry/state set after construction."""
        if not hasattr(self, '_gate') or self._gate is None:
            # LIVE-DISPATCH-UNBLOCKER-V1 Phase D (2026-05-22):
            # gate reads ToolCatalog metadata for amortization
            # tool_hash + future diagnostic surfacing. Pulled from
            # the handler when present (the handler holds the
            # canonical catalog instance).
            catalog = None
            handler = getattr(self, "_handler", None)
            if handler is not None:
                catalog = getattr(handler, "_tool_catalog", None)
            self._gate = DispatchGate(
                reasoning_service=self,
                registry=getattr(self, '_registry', None),
                state=getattr(self, '_state', None),
                events=getattr(self, '_events', None),
                mcp=getattr(self, '_mcp', None),
                catalog=catalog,
            )
        return self._gate

    def cleanup_expired_authorizations(self, instance_id: str) -> None:
        """Remove expired PendingActions and used/expired ApprovalTokens."""
        now = datetime.now(timezone.utc)

        if instance_id in self._pending_actions:
            self._pending_actions[instance_id] = [
                a for a in self._pending_actions[instance_id]
                if now < a.expires_at
            ]
            if not self._pending_actions[instance_id]:
                del self._pending_actions[instance_id]

        self._get_gate().cleanup_expired_tokens()

    @staticmethod
    def _is_stub_schema(tool_entry: dict) -> bool:
        """Check if a tool entry has a stub schema (open input, no properties)."""
        schema = tool_entry.get("input_schema", {})
        return schema.get("additionalProperties") is True and not schema.get("properties")

    def set_retrieval(self, retrieval: Any) -> None:
        """Wire up the retrieval service for kernel tool routing."""
        self._retrieval = retrieval

    def set_files(self, files: Any) -> None:
        """Wire up the file service for kernel tool routing."""
        self._files = files

    def set_registry(self, registry: Any) -> None:
        """Wire up the capability registry for request_tool routing."""
        self._registry = registry

    def set_workspace(self, workspace: Any) -> None:
        """Wire up the workspace manager for manage_workspace/register_tool."""
        self._workspace = workspace

    def set_state(self, state: Any) -> None:
        """Wire up the state store for request_tool activation."""
        self._state = state

    def set_channel_registry(self, registry: Any) -> None:
        """Wire up the channel registry for send_to_channel."""
        self._channel_registry = registry

    def set_trigger_store(self, store: Any) -> None:
        """Wire up the trigger store for manage_schedule."""
        self._trigger_store = store

    def set_handler(self, handler: Any) -> None:
        """Wire up the handler (implements HandlerProtocol)."""
        self._handler = handler

    def set_canvas(self, canvas: Any) -> None:
        """Wire up the CanvasService for canvas_* / page_* tool routing."""
        self._canvas = canvas

    # --- Public state accessors (replace private attribute access from handler) ---

    def get_pending_actions(self, instance_id: str) -> list[PendingAction] | None:
        """Return a copy of pending actions for an instance, or None."""
        actions = self._pending_actions.get(instance_id)
        if actions is None:
            return None
        return list(actions)  # copy — caller cannot mutate internal list

    def clear_pending_actions(self, instance_id: str) -> None:
        """Remove all pending actions for an instance."""
        self._pending_actions.pop(instance_id, None)

    def get_conflict_raised(self) -> bool:
        """Whether a gate conflict was raised this turn."""
        return self._conflict_raised_this_turn

    def reset_conflict_raised(self) -> None:
        """Reset the per-turn conflict flag and gate denial counters."""
        self._conflict_raised_this_turn = False
        if hasattr(self, '_gate') and self._gate:
            self._gate.reset_denial_counts()

    def get_tools_changed(self) -> bool:
        """Whether manage_capabilities changed tool state this turn."""
        return self._tools_changed

    def reset_tools_changed(self) -> None:
        """Reset the tools-changed flag."""
        self._tools_changed = False

    @property
    def main_model(self) -> str:
        """The primary model name from the provider."""
        entries = self._chains.get("primary", [])
        return entries[0].model if entries else "unknown"

    def get_loaded_tools(self, space_id: str) -> set[str]:
        """Get the set of MCP tool names currently loaded for a space."""
        return self._loaded_tools.get(space_id, set())

    def load_tool(self, space_id: str, tool_name: str) -> None:
        """Add a tool to the loaded set for a space."""
        if space_id not in self._loaded_tools:
            self._loaded_tools[space_id] = set()
        self._loaded_tools[space_id].add(tool_name)

    def get_last_reasoning_payload(self, instance_id: str) -> dict:
        """Return the most-recent reasoning payload for this instance
        (system_prompt, messages, tools, system_prompt_static,
        system_prompt_dynamic). Empty dict if no reason() call has
        fired yet for this instance.

        Consumers: ``handle_dump_context_tool`` falls back to this
        when the dispatch-time ReasoningRequest carries empty payload
        fields (live-dispatch factory provides a minimal request)."""
        return self._last_reasoning_payload.get(instance_id, {})

    def get_last_real_input_tokens(self, instance_id: str) -> int:
        """Return the real input_tokens from the last principal reasoning call, or 0."""
        return self._last_real_input_tokens.get(instance_id, 0)

    def drain_tool_trace(self) -> list[dict]:
        """Return and clear the accumulated tool call trace for the current turn.

        IWL drain-ordering invariant (the design review final-signoff note): the
        handler is the single owner of this drain. response_delivery /
        StepDispatcher append into the trace store but never call
        this method. After `reason()` returns, the handler calls
        drain_tool_trace() once to consume the turn's entries.

        IWL trace-sink note: when `trace_sink` was injected at
        construction, the underlying list is shared with the new
        path's StepDispatcher; drain returns entries from whichever
        path produced them. The list is cleared in-place to preserve
        the shared reference for subsequent turns.
        """
        trace = list(self._turn_tool_trace)
        self._turn_tool_trace.clear()
        return trace

    def drain_action_records(self) -> list:
        """Return and clear the accumulated ActionStateRecords for
        the current turn.

        RESPONSE-FIDELITY-V1 Batch 1.2 (2026-05-08): tool handlers
        (currently only note_this; existing surfaces migrate in
        Batch 2 onward) append to the per-turn list. Drain ordering
        (hardened 2026-05-08): the integration runner PEEKS at
        finalize time (copy without clearing) so records reach
        Briefing.audit_trace.action_state_records; the handler then
        DRAINS (this method, clear-on-read) at turn end to populate
        TurnContext.action_state_records for the conv-log "Action
        state this turn" block. Two readers, one shared list — same
        pattern as trace_sink.
        """
        records = list(self._turn_action_records)
        self._turn_action_records.clear()
        return records

    def clear_loaded_tools(self, space_id: str) -> None:
        """Clear loaded tools for a space (session boundary)."""
        count = len(self._loaded_tools.pop(space_id, set()))
        if count:
            logger.info("TOOL_UNLOAD: space=%s cleared=%d", space_id, count)

    def _assert_admin_space(
        self, request: "ReasoningRequest", tool_name: str,
    ) -> str | None:
        """Lightweight admin-space gate.

        Several admin-only tools (set_chain_model, diagnose_llm_chain,
        diagnose_messenger) restrict their dispatch to the System
        space. The agent's DispatchGate already confines sensitive
        tools by space at the surfacing layer; this is a defense-in-
        depth check at dispatch time.

        Returns the user-facing rejection string when the active
        space is not "system"; returns None when the call is allowed
        to proceed.

        CONFIRMED-PATH-DISPATCH-PARITY-V1 (2026-05-03): extracted
        from inline duplicates in the legacy reason() loop. Single
        source of truth for the policy text per Kit's framing
        (canonical-source-plus-derived-consumers).
        """
        space_type = ""
        if getattr(request, "active_space", None) is not None:
            space_type = getattr(request.active_space, "space_type", "") or ""
        if space_type != "system":
            return (
                f"{tool_name} is admin-only and only available "
                f"in the System space."
            )
        return None

    async def complete_simple(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 1024,
        prefer_cheap: bool = False,
        output_schema: dict | None = None,
        chain: str | None = None,
    ) -> str:
        """Single stateless completion. No tools, no history, no task events.

        Used by kernel infrastructure (extraction, consolidation) not by agents.
        Returns raw text response. prefer_cheap uses Haiku-class model for cost efficiency.

        chain: explicit chain name override ("primary", "simple", "cheap").
        When omitted, prefer_cheap selects "cheap" or "simple".

        When output_schema is provided, uses Anthropic's native structured outputs
        (constrained decoding). Schema compliance is guaranteed by the API — no
        json.loads() retry logic needed. Returns "{}" on truncation or refusal.
        """
        # Two-chain model: "primary" + "lightweight". The legacy
        # three-chain names ("simple" / "cheap") map to "lightweight"
        # with a deprecation log so external callers keep working. The
        # old ``prefer_cheap`` parameter is now a no-op selector into
        # the same lightweight chain — kept for back-compat.
        _LEGACY_ALIASES = {"cheap": "lightweight", "simple": "lightweight"}
        if chain is None:
            chain_name = "lightweight"
        elif chain in _LEGACY_ALIASES:
            chain_name = _LEGACY_ALIASES[chain]
            logger.debug(
                "complete_simple: legacy chain name %r remapped to %r "
                "(consolidate to 'lightweight' at the call site)",
                chain, chain_name,
            )
        else:
            chain_name = chain
        if prefer_cheap and chain is None:
            # prefer_cheap=True historically selected "cheap"; that chain
            # is now "lightweight" (the default), so this is a no-op.
            pass
        entries = self._chains.get(chain_name, self._chains.get("primary", []))
        messages = [{"role": "user", "content": user_content}]

        # Walk the chain until one provider succeeds
        last_exc: Exception | None = None
        response = None
        for entry in entries:
            pname = getattr(entry.provider, "provider_name", type(entry.provider).__name__)
            try:
                response = await entry.provider.complete(
                    model=entry.model,
                    system=system_prompt,
                    messages=messages,
                    tools=[],
                    max_tokens=max_tokens,
                    output_schema=output_schema,
                )
                break  # Success
            except Exception as exc:
                logger.warning("complete_simple[%s]: %s/%s failed: %s", chain_name, pname, entry.model, exc)
                last_exc = exc
                continue

        if response is None:
            raise last_exc or RuntimeError(f"complete_simple: all providers in chain '{chain_name}' failed")

        # Log token usage on every simple completion
        logger.info(
            "SIMPLE_RESPONSE: tokens_in=%d tokens_out=%d truncated=%s",
            response.input_tokens, response.output_tokens,
            response.stop_reason == "max_tokens",
        )
        if response.stop_reason == "max_tokens":
            text_preview = "".join(b.text for b in response.content if b.type == "text")
            logger.warning(
                "complete_simple: response truncated (max_tokens=%d) preview=%s",
                max_tokens, text_preview[:200],
            )
            if output_schema:
                return "{}"
            # Plain-text call: return whatever was generated (partial is better than "{}")
        if response.stop_reason == "refusal":
            logger.warning("complete_simple: response refused by model")
            return "{}"
        text_parts = [b.text for b in response.content if b.type == "text"]
        return "".join(text_parts)

    # Kernel tools: intercepted before MCP, never passed through to external servers
    _KERNEL_TOOLS = {"remember", "remember_details", "write_file", "read_file", "list_files", "delete_file", "dismiss_whisper", "read_source", "read_soul", "update_soul", "manage_covenants", "manage_capabilities", "manage_channels", "send_to_channel", "manage_schedule", "inspect_state", "request_tool", "execute_code", "manage_workspace", "register_tool", "manage_plan", "read_runtime_trace", "diagnose_issue", "propose_fix", "submit_spec", "manage_members", "send_relational_message", "resolve_relational_message", "set_chain_model", "diagnose_llm_chain", "diagnose_messenger", "canvas_list", "canvas_create", "page_read", "page_write", "page_list", "page_search", "canvas_preference_extract", "canvas_preference_confirm", "consult", "request_space_action", "request_reference", "store_reference", "create_reference_collection", "move_reference_to_canvas", "mark_reference_superseded", "quarantine_reference", "restore_reference_from_quarantine", "note_this", "ask_coding_session", "read_coding_session_response", "dump_context", "restart_self", "inspect_tools", "git_fetch", "git_rev_parse", "git_status", "git_diff_for_review", "git_commit", "git_push", "run_self_test_suite", "improve_kernos", "record_closure_attempt", "run_closure_probe", "lookup_pattern_invariants", "record_fix_authorization", "classify_proposed_fix", "validate_investigation_response", "maybe_run_closure_for_fix", "surface_to_user"}

    # SELF-IMPROVEMENT-CLOSURE-V1 (AC17): explicit dispatchability
    # registry. Every name in this set MUST have a concrete branch
    # in execute_tool that does NOT return the
    # "Kernel tool '<name>' not handled." sentinel. Adding a name
    # here without a handler breaks AC17's test
    # (test_dispatchability_registry_honest). The substrate-parity
    # probe (Tool Availability Honesty) reads this set via the
    # public get_dispatchable_kernel_tools() helper rather than
    # equating _KERNEL_TOOLS membership with dispatchability —
    # because execute_tool returns the sentinel for any name in
    # _KERNEL_TOOLS that lacks a real handler branch, which is
    # exactly the catalog-vs-dispatch divergence the invariant
    # catches.
    _DISPATCHABLE_KERNEL_TOOLS: frozenset[str] = frozenset({
        # Workspace files
        "write_file", "read_file", "list_files", "delete_file",
        # Memory + identity
        "remember", "remember_details", "dismiss_whisper",
        "read_source", "read_soul", "update_soul",
        "note_this", "inspect_state",
        # Covenants / capabilities / channels
        "manage_covenants", "manage_capabilities",
        "manage_channels", "send_to_channel", "manage_schedule",
        # Planning + diagnostics
        "request_tool", "execute_code", "manage_workspace",
        "register_tool", "manage_plan", "read_runtime_trace",
        "diagnose_issue", "propose_fix", "submit_spec",
        "inspect_tools",
        # Members + relational messaging
        "manage_members", "send_relational_message",
        "resolve_relational_message",
        # Model + chain diagnostics
        "set_chain_model", "diagnose_llm_chain", "diagnose_messenger",
        # Canvas + reference
        "canvas_list", "canvas_create", "page_read", "page_write",
        "page_list", "page_search", "canvas_preference_extract",
        "canvas_preference_confirm",
        "request_reference", "store_reference",
        "create_reference_collection", "move_reference_to_canvas",
        "mark_reference_superseded", "quarantine_reference",
        "restore_reference_from_quarantine",
        # External agents + cross-space
        "consult", "request_space_action",
        "ask_coding_session", "read_coding_session_response",
        # Self-admin
        "dump_context", "restart_self",
        # Git operations + self-test + autonomous improvement
        "git_fetch", "git_rev_parse", "git_status",
        "git_diff_for_review", "git_commit", "git_push",
        "run_self_test_suite", "improve_kernos",
        # SELF-IMPROVEMENT-CLOSURE-V1
        "record_closure_attempt", "run_closure_probe",
        "lookup_pattern_invariants",
        # USER-INITIATED-IMPROVEMENT-TRIGGER-V1
        "record_fix_authorization", "classify_proposed_fix",
        "validate_investigation_response",
        "maybe_run_closure_for_fix", "surface_to_user",
    })

    def get_dispatchable_kernel_tools(self) -> set[str]:
        """Return the set of tool names with confirmed dispatch
        paths through ``execute_tool``. Public surface for
        substrate-parity probes (SELF-IMPROVEMENT-CLOSURE-V1).

        Contract: every returned name has a concrete handler branch
        in ``execute_tool`` that does NOT return the
        ``"Kernel tool '<name>' not handled."`` sentinel string.
        The returned set is a subset of ``_KERNEL_TOOLS`` by
        construction; names in ``_KERNEL_TOOLS`` but NOT in this
        set represent registration drift and are exactly what the
        Tool Availability Honesty invariant probe detects.
        """
        return set(self._DISPATCHABLE_KERNEL_TOOLS)

    # CLEANUP-BATCH-V1 item 11: kernel-tool dispatch path registry.
    #
    # Each tool name in `_KERNEL_TOOLS` declares which dispatch paths
    # carry it. Three valid path tokens:
    #
    #   "loop"      — the main reason() tool loop (Chain 2). Wraps
    #                 handler in try/except with friendly fallback
    #                 strings. Used by the agent's regular tool calls.
    #   "confirmed" — execute_tool() (Chain 1). Used when the gate
    #                 surfaces a PendingAction and the user confirms;
    #                 also called from scheduler triggers. Returns
    #                 strings (no try/except wrapping); callers route
    #                 the result through classify_trigger_failure.
    #   "helper"    — dispatched through a helper method
    #                 (_handle_canvas_tool) rather than a direct elif
    #                 branch. Canvas tools follow this path because
    #                 their dispatch is shared between contexts.
    #
    # Why a registry instead of a single dispatch table: the two elif
    # chains have legitimately different error semantics (loop wraps,
    # confirmed raises). Unifying them at the handler layer would
    # silently change behavior on one path. The registry documents the
    # intentional divergence and a structural test
    # (tests/test_kernel_tool_dispatch_paths.py) pins both chains to
    # this declaration so adding or moving a tool to one chain without
    # updating its paths fails CI. Full handler extraction is parked
    # as a follow-on spec with explicit error-semantic decisions.
    #
    # Five tools intentionally declared "loop"-only: read-only or
    # chain-management surfaces that never produce confirmable
    # PendingActions. They appear only in Chain 2.
    _KERNEL_TOOL_PATHS: dict[str, frozenset[str]] = {
        # Loop + confirmed (general kernel tools)
        "write_file":                  frozenset({"confirmed"}),
        "read_file":                   frozenset({"confirmed"}),
        "list_files":                  frozenset({"confirmed"}),
        "delete_file":                 frozenset({"confirmed"}),
        "execute_code":                frozenset({"confirmed"}),
        "consult":                     frozenset({"confirmed"}),
        "request_space_action":        frozenset({"confirmed"}),
        "manage_workspace":            frozenset({"confirmed"}),
        "register_tool":               frozenset({"confirmed"}),
        "manage_plan":                 frozenset({"confirmed"}),
        "read_runtime_trace":          frozenset({"confirmed"}),
        "diagnose_issue":              frozenset({"confirmed"}),
        "propose_fix":                 frozenset({"confirmed"}),
        "submit_spec":                 frozenset({"confirmed"}),
        "manage_members":              frozenset({"confirmed"}),
        "send_relational_message":     frozenset({"confirmed"}),
        "resolve_relational_message":  frozenset({"confirmed"}),
        "remember":                    frozenset({"confirmed"}),
        "dismiss_whisper":             frozenset({"confirmed"}),
        "read_source":                 frozenset({"confirmed"}),
        "read_soul":                   frozenset({"confirmed"}),
        "update_soul":                 frozenset({"confirmed"}),
        "manage_covenants":            frozenset({"confirmed"}),
        "manage_capabilities":         frozenset({"confirmed"}),
        "manage_channels":             frozenset({"confirmed"}),
        "send_to_channel":             frozenset({"confirmed"}),
        "manage_schedule":             frozenset({"confirmed"}),
        "request_tool":                frozenset({"confirmed"}),
        # Previously loop-only — read-only or chain-management; never
        # produced confirmable PendingActions on the legacy path.
        # CONFIRMED-PATH-DISPATCH-PARITY-V1 (2026-05-03) wired them
        # into execute_tool's confirmed elif chain so the legacy
        # strike can remove "loop" from these entries cleanly. The
        # three admin-gated tools (set_chain_model,
        # diagnose_llm_chain, diagnose_messenger) share the
        # _assert_admin_space helper at the dispatch site.
        "remember_details":            frozenset({"confirmed"}),
        "inspect_state":               frozenset({"confirmed"}),
        "set_chain_model":             frozenset({"confirmed"}),
        "diagnose_llm_chain":          frozenset({"confirmed"}),
        "diagnose_messenger":          frozenset({"confirmed"}),
        # Helper-routed (canvas tools share dispatch through
        # _handle_canvas_tool from execute_tool's confirmed path).
        "canvas_list":                 frozenset({"confirmed", "helper"}),
        "canvas_create":               frozenset({"confirmed", "helper"}),
        "page_read":                   frozenset({"confirmed", "helper"}),
        "page_write":                  frozenset({"confirmed", "helper"}),
        "page_list":                   frozenset({"confirmed", "helper"}),
        "page_search":                 frozenset({"confirmed", "helper"}),
        "canvas_preference_extract":   frozenset({"confirmed", "helper"}),
        "canvas_preference_confirm":   frozenset({"confirmed", "helper"}),
        # REFERENCE-PRIMITIVE-V1 — seven tools dispatched on the
        # confirmed path. request_reference is read-classified
        # (read-only catalog navigation + injection); the other six
        # are soft_write (file writes, catalog mutations — all
        # reversible via tombstone / restore).
        "request_reference":                  frozenset({"confirmed"}),
        "store_reference":                    frozenset({"confirmed"}),
        "create_reference_collection":        frozenset({"confirmed"}),
        "move_reference_to_canvas":           frozenset({"confirmed"}),
        "mark_reference_superseded":          frozenset({"confirmed"}),
        "quarantine_reference":               frozenset({"confirmed"}),
        "restore_reference_from_quarantine":  frozenset({"confirmed"}),
        # RESPONSE-FIDELITY-V1 Batch 1.2 — synchronous receipt-backed
        # memory path. Dispatched in the confirmed elif chain at
        # ``execute_tool``; appears here so the registry-parity pins
        # in ``test_kernel_tool_dispatch_paths.py`` /
        # ``test_kernel_tool_registry_parity.py`` agree with dispatch.
        "note_this":                          frozenset({"confirmed"}),
        # CODING-SESSION-BRIDGE-V1: file-based bridge to already-running
        # coding sessions. ask=soft_write, read=read; both dispatched
        # in the confirmed elif chain.
        "ask_coding_session":                 frozenset({"confirmed"}),
        "read_coding_session_response":       frozenset({"confirmed"}),
        # SELF-ADMIN-TOOLS-V1 (2026-05-19): /dump + /restart
        # agent-callable equivalents. System-space-gated at dispatch.
        "dump_context":                       frozenset({"confirmed"}),
        "restart_self":                       frozenset({"confirmed"}),
        # TOOL-INTROSPECTION-V1 (2026-05-22): natural-prose
        # catalog reader. Pure read; confirmed path only.
        "inspect_tools":                      frozenset({"confirmed"}),
        # GIT-OPERATIONS-PRIMITIVES-V1 (2026-05-22): git kernel
        # tools for the autonomous-improvement loop. All
        # workspace-guarded.
        "git_fetch":                          frozenset({"confirmed"}),
        "git_rev_parse":                      frozenset({"confirmed"}),
        "git_status":                         frozenset({"confirmed"}),
        "git_diff_for_review":                frozenset({"confirmed"}),
        "git_commit":                         frozenset({"confirmed"}),
        "git_push":                           frozenset({"confirmed"}),
        # SELF-TEST-GATE-V1 (2026-05-22): pytest runner for
        # autonomous loop. workspace-guarded.
        "run_self_test_suite":                frozenset({"confirmed"}),
        # IMPROVEMENT-LOOP-WORKFLOW-V1 (2026-05-22): autonomous
        # improvement orchestrator entry point.
        "improve_kernos":                     frozenset({"confirmed"}),
        # SELF-IMPROVEMENT-CLOSURE-V1 (2026-05-26): closure-
        # machinery tools. Invoked from the self_improvement
        # workflow's closure path; not surfaced to the agent's
        # "loop" path (purely substrate-internal).
        "lookup_pattern_invariants":          frozenset({"confirmed"}),
        "record_closure_attempt":             frozenset({"confirmed"}),
        "run_closure_probe":                  frozenset({"confirmed"}),
        # USER-INITIATED-IMPROVEMENT-TRIGGER-V1 (2026-05-27):
        # fix-authorization workflow tools. Invoked from the
        # user_initiated_improvement workflow; substrate-
        # internal (not surfaced to agent loop path).
        "record_fix_authorization":           frozenset({"confirmed"}),
        "classify_proposed_fix":              frozenset({"confirmed"}),
        "validate_investigation_response":    frozenset({"confirmed"}),
        "maybe_run_closure_for_fix":          frozenset({"confirmed"}),
        "surface_to_user":                    frozenset({"confirmed"}),
    }

    # ---------------------------------------------------------------------------
    # Dispatch Gate (3D-HOTFIX)
    # ---------------------------------------------------------------------------

    # Gate methods extracted to kernos/kernel/gate.py — accessed via self._get_gate()
    # Delegation methods for backward compatibility (tests call these directly)
    def _classify_tool_effect(self, tool_name: str, active_space: Any, tool_input: dict | None = None) -> str:
        return self._get_gate().classify_tool_effect(tool_name, active_space, tool_input)

    def _describe_action(self, tool_name: str, tool_input: dict) -> str:
        return self._get_gate()._describe_action(tool_name, tool_input)

    def _get_capability_for_tool(self, tool_name: str) -> str | None:
        return self._get_gate()._get_capability_for_tool(tool_name)

    def _get_tool_description(self, tool_name: str) -> str:
        return self._get_gate()._get_tool_description(tool_name)

    async def _gate_tool_call(self, *args, **kwargs) -> GateResult:
        return await self._get_gate().evaluate(*args, **kwargs)

    async def _evaluate_gate(self, *args, **kwargs) -> GateResult:
        return await self._get_gate()._evaluate_model(*args, **kwargs)

    def _issue_approval_token(self, tool_name: str, tool_input: dict) -> ApprovalToken:
        return self._get_gate().issue_approval_token(tool_name, tool_input)

    def _validate_approval_token(self, token_id: str, tool_name: str, tool_input: dict) -> bool:
        return self._get_gate().validate_approval_token(token_id, tool_name, tool_input)

    @property
    def _approval_tokens(self) -> dict:
        """Backward compat — tokens now live on the gate."""
        return self._get_gate()._approval_tokens

    async def execute_tool(
        self, tool_name: str, tool_input: dict, request: "ReasoningRequest"
    ) -> str:
        """Execute a tool call directly (used for confirmed pending actions).

        Handles both kernel tools and MCP tools. Mirrors the routing in reason().
        """
        # SELF-CONTROLLED-LOOP-LIVENESS-V1 (2026-05-21): canonicalize
        # known model-hallucinated tool names before routing. Keeps
        # critical kernel primitives (manage_plan) reachable even when
        # the model picks the wrong name. See kernos/kernel/tool_aliases.py.
        from kernos.kernel.tool_aliases import (
            canonicalize_tool_name,
            emit_alias_repair_receipt,
        )
        _canonical_name, _was_repaired = canonicalize_tool_name(tool_name)
        if _was_repaired:
            logger.info(
                "TOOL_ALIAS_REPAIR alias=%s canonical=%s context=dispatch",
                tool_name, _canonical_name,
            )
            await emit_alias_repair_receipt(
                self._events,
                instance_id=request.instance_id,
                requested=tool_name,
                canonical=_canonical_name,
                context="dispatch",
            )
            tool_name = _canonical_name
        if tool_name in self._KERNEL_TOOLS:
            if tool_name == "write_file":
                if self._files:
                    return await self._files.write_file(
                        request.instance_id,
                        request.active_space_id,
                        tool_input.get("name", ""),
                        tool_input.get("content", ""),
                        tool_input.get("description", ""),
                        target_space_id=tool_input.get("target_space_id"),
                    )
                return "File system is not available."
            elif tool_name == "read_file":
                if self._files:
                    return await self._files.read_file(
                        request.instance_id,
                        request.active_space_id,
                        tool_input.get("name", ""),
                    )
                return "File system is not available."
            elif tool_name == "list_files":
                if self._files:
                    return await self._files.list_files(
                        request.instance_id,
                        request.active_space_id,
                    )
                return "File system is not available."
            elif tool_name == "delete_file":
                if self._files:
                    return await self._files.delete_file(
                        request.instance_id,
                        request.active_space_id,
                        tool_input.get("name", ""),
                    )
                return "File system is not available."
            elif tool_name == "execute_code":
                import json as _json
                from kernos.kernel.code_exec import execute_code as _exec_code
                data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
                result = await _exec_code(
                    instance_id=request.instance_id,
                    space_id=request.active_space_id,
                    code=tool_input.get("code", ""),
                    timeout_seconds=tool_input.get("timeout_seconds", 30),
                    write_file_name=tool_input.get("write_file"),
                    data_dir=data_dir,
                    backend=tool_input.get("backend"),
                )
                return _json.dumps(result)
            elif tool_name == "consult":
                import json as _json
                from kernos.kernel.external_agents.tool import (
                    get_service as _ext_get_service,
                    validate_consult_input as _validate_consult_input,
                )
                from kernos.kernel.external_agents.errors import (
                    ExternalAgentError as _ExtError,
                )
                # Handler-side validation: schema enforces non-empty
                # harness + question via minLength: 1 + enum, but
                # some models bypass JSON schema validation silently.
                # validate_consult_input is the pure helper — same
                # logic, testable without the full handler.
                _validated = _validate_consult_input(tool_input)
                if isinstance(_validated, dict):
                    return _json.dumps(_validated)
                _harness, _question = _validated
                try:
                    _svc = await _ext_get_service()
                    _consult_result = await _svc.orchestrator.consult(
                        instance_id=request.instance_id,
                        member_id=getattr(request, "member_id", "")
                                  or request.instance_id,
                        harness=_harness,
                        question=_question,
                        context=tool_input.get("context", ""),
                        session_id_raw=tool_input.get("session_id", ""),
                        workspace_dir=tool_input.get("workspace_dir") or None,
                        timeout_seconds=tool_input.get("timeout_seconds"),
                    )
                    return _json.dumps({
                        "response": _consult_result.response,
                        "harness": _consult_result.harness,
                        "session_id": _consult_result.session_id,
                        "truncated": _consult_result.truncated,
                        "metadata": _consult_result.metadata,
                    })
                except _ExtError as exc:
                    return _json.dumps({
                        "error": type(exc).__name__,
                        "message": str(exc),
                    })
            elif tool_name == "request_space_action":
                return await self._dispatch_cross_space_request(
                    tool_input, request,
                )
            elif tool_name == "manage_workspace":
                if self._workspace:
                    action = tool_input.get("action", "list")
                    if action == "list":
                        return await self._workspace.list_artifacts(request.instance_id, request.active_space_id)
                    elif action == "add":
                        msg, _ = await self._workspace.add_artifact(request.instance_id, request.active_space_id, tool_input.get("artifact", {}))
                        return msg
                    elif action == "update":
                        return await self._workspace.update_artifact(request.instance_id, request.active_space_id, tool_input.get("artifact_id", ""), tool_input.get("artifact", {}))
                    elif action == "archive":
                        return await self._workspace.archive_artifact(request.instance_id, request.active_space_id, tool_input.get("artifact_id", ""))
                    return f"Unknown action: {action}"
                return "Workspace manager is not available."
            elif tool_name == "register_tool":
                if self._workspace:
                    _desc_file = tool_input.get("descriptor_file", "") or tool_input
                    # TOOL-REGISTRATION-AUTHORIZATION-V1 (2026-05-22):
                    # thread receipts-substrate context so workspace
                    # can gate hard_write / external_agent_read tools.
                    _register_msg = await self._workspace.register_tool(
                        request.instance_id, request.active_space_id, _desc_file,
                        member_id=request.member_id or "",
                        data_dir=os.environ.get("KERNOS_DATA_DIR", "./data"),
                        event_stream=self._events,
                    )
                    # SYSTEM-REFERENCE-CANVAS-SEED Pillar 2: append a page to
                    # the member's My Tools canvas. Best-effort — never
                    # breaks registration. Only runs when the registration
                    # actually succeeded.
                    if "Registered tool" in _register_msg:
                        await self._populate_my_tools_page(
                            request=request, descriptor_file=_desc_file,
                        )
                    return _register_msg
                return "Workspace manager is not available."
            elif tool_name == "manage_plan":
                if hasattr(self, '_handler') and self._handler:
                    return await self._handler._handle_manage_plan(
                        request.instance_id, request.active_space_id, tool_input)
                return "Self-directed execution is not available."
            elif tool_name == "read_runtime_trace":
                if hasattr(self, '_handler') and self._handler:
                    _turns = tool_input.get("turns", 10)
                    _filter = tool_input.get("filter", None)
                    _turn_id = tool_input.get("turn_id", None)
                    events = await self._handler._runtime_trace.read(
                        request.instance_id, turns=_turns,
                        filter_level=_filter, turn_id=_turn_id)
                    if not events:
                        return "No trace events found."
                    lines = []
                    for e in events:
                        lines.append(
                            f"[{e.get('timestamp', '?')[:19]}] {e.get('level', '?').upper()} "
                            f"{e.get('source', '?')}:{e.get('event', '?')} — {e.get('detail', '')[:200]}"
                        )
                    return f"Runtime trace ({len(events)} events):\n" + "\n".join(lines)
                return "Runtime trace is not available."
            elif tool_name in ("diagnose_issue", "propose_fix", "submit_spec"):
                from kernos.kernel.diagnostics import handle_diagnose_issue, handle_propose_fix, handle_submit_spec
                _rt = getattr(self._handler, '_runtime_trace', None) if self._handler else None
                if tool_name == "diagnose_issue":
                    return await handle_diagnose_issue(
                        request.instance_id, request.active_space_id, tool_input, _rt, self)
                elif tool_name == "propose_fix":
                    return await handle_propose_fix(request.instance_id, tool_input, _rt)
                else:
                    return await handle_submit_spec(request.instance_id, tool_input, self._handler)
            elif tool_name == "manage_members":
                if hasattr(self, '_handler') and self._handler:
                    return await self._handler._handle_manage_members(request.instance_id, tool_input, requesting_member_id=request.member_id)
                return "Member management is not available."
            elif tool_name == "send_relational_message":
                if hasattr(self, '_handler') and self._handler:
                    return await self._handler._handle_send_relational_message(
                        request.instance_id, tool_input,
                        origin_member_id=request.member_id,
                    )
                return "Relational messaging is not available."
            elif tool_name == "resolve_relational_message":
                if hasattr(self, '_handler') and self._handler:
                    return await self._handler._handle_resolve_relational_message(
                        request.instance_id, tool_input,
                        requesting_member_id=request.member_id,
                    )
                return "Relational messaging is not available."
            elif tool_name == "note_this":
                # RESPONSE-FIDELITY-V1 Batch 1.2 (2026-05-08): synchronous
                # receipt-backed memory path. Resolves G.1 ("I'll remember"
                # without a substrate write). Appends an ActionStateRecord
                # to the per-turn collector for the integration runner
                # to fold into the briefing's audit trace at finalize time.
                from kernos.kernel.note_this import handle_note_this
                if self._state is None:
                    return "Memory state store is not available."
                kind_arg = str(tool_input.get("kind", ""))
                summary, record = await handle_note_this(
                    state=self._state,
                    instance_id=request.instance_id,
                    member_id=getattr(request, "member_id", "") or "",
                    active_space_id=request.active_space_id,
                    turn_id=getattr(request, "conversation_id", "") or "",
                    kind=kind_arg,
                    content=str(tool_input.get("content", "")),
                    subject=str(tool_input.get("subject", "")),
                    category=str(tool_input.get("category", "")),
                )
                self._turn_action_records.append(record)
                # Codex review fold (2026-05-08): when kind=rule and the
                # write succeeded, fire validate_covenant_set async (same
                # pattern as manage_covenants update). Surfaces conflicts,
                # merges, and rewrites the LLM analysis catches; without
                # this, note_this(rule) silently bypassed the validation
                # discipline manage_covenants honors.
                if (
                    kind_arg == "rule"
                    and record.execution_state == "completed"
                    and record.affected_objects
                    and record.affected_objects[0].startswith("rule_")
                ):
                    import asyncio as _asyncio
                    from kernos.kernel.covenant_manager import (
                        validate_covenant_set,
                    )
                    _asyncio.create_task(
                        validate_covenant_set(
                            state=self._state,
                            events=self._events,
                            reasoning_service=self,
                            instance_id=request.instance_id,
                            new_rule_id=record.affected_objects[0],
                        )
                    )
                return summary
            elif tool_name in ("ask_coding_session", "read_coding_session_response"):
                # CODING-SESSION-BRIDGE-V1: file-bridge tools talking to
                # already-running coding sessions. Each returns
                # (summary, ActionStateRecord); the record is appended
                # to the per-turn collector for the integration runner
                # to fold into AuditTrace (same shape as note_this).
                from kernos.kernel.coding_session_bridge import (
                    handle_ask_coding_session,
                    handle_read_coding_session_response,
                )
                import os as _os_local
                data_dir = _os_local.getenv("KERNOS_DATA_DIR", "./data")
                if tool_name == "ask_coding_session":
                    summary, record = await handle_ask_coding_session(
                        instance_id=request.instance_id,
                        member_id=getattr(request, "member_id", "") or "",
                        active_space_id=request.active_space_id,
                        data_dir=data_dir,
                        target=str(tool_input.get("target", "")),
                        question=str(tool_input.get("question", "")),
                        context=tool_input.get("context") or {},
                    )
                else:
                    summary, record = await handle_read_coding_session_response(
                        instance_id=request.instance_id,
                        data_dir=data_dir,
                        request_id=str(tool_input.get("request_id", "")),
                    )
                self._turn_action_records.append(record)
                return summary
            elif tool_name == "remember":
                if self._retrieval:
                    _idb = (
                        getattr(self._handler, '_instance_db', None)
                        if hasattr(self, '_handler') and self._handler
                        else None
                    )
                    return await self._retrieval.search(
                        request.instance_id,
                        tool_input.get("query", ""),
                        request.active_space_id,
                        requesting_member_id=getattr(request, "member_id", ""),
                        instance_db=_idb,
                    )
                return "Memory search is not available."
            elif tool_name == "dismiss_whisper":
                return await self._handle_dismiss_whisper(
                    request.instance_id,
                    tool_input.get("whisper_id", ""),
                    tool_input.get("reason", "user_dismissed"),
                )
            elif tool_name == "read_source":
                return _read_source(
                    tool_input.get("path", ""),
                    tool_input.get("section", ""),
                )
            elif tool_name == "read_soul":
                # Per-member: return member profile (the real identity state)
                member_id = getattr(request, "member_id", "")
                if member_id and hasattr(self, "_handler") and self._handler:
                    idb = getattr(self._handler, "_instance_db", None)
                    if idb:
                        profile = await idb.get_member_profile(member_id)
                        if profile:
                            return json.dumps(profile, indent=2, default=str)
                # Fallback: instance soul
                if self._state:
                    soul = await self._state.get_soul(request.instance_id)
                    if soul:
                        from dataclasses import asdict
                        return json.dumps(asdict(soul), indent=2)
                    return "No soul found for this instance."
                return "State store is not available."
            elif tool_name == "update_soul":
                if self._state:
                    field = tool_input.get("field", "")
                    value = tool_input.get("value", "")
                    if field not in _SOUL_UPDATABLE_FIELDS:
                        return (
                            f"Cannot update '{field}'. Only these fields can be updated: "
                            f"{', '.join(sorted(_SOUL_UPDATABLE_FIELDS))}."
                        )
                    # Per-member soul fields → write to member_profiles
                    _MEMBER_SOUL_FIELDS = {"agent_name", "emoji", "personality_notes", "communication_style"}
                    member_id = getattr(request, "member_id", "") if hasattr(request, "member_id") else ""
                    if field in _MEMBER_SOUL_FIELDS and member_id and hasattr(self, "_handler") and self._handler:
                        idb = getattr(self._handler, "_instance_db", None)
                        if idb:
                            await idb.upsert_member_profile(member_id, {field: value})
                            return f"Updated {field} to: {value}"
                    # Legacy fallback: write to instance soul
                    soul = await self._state.get_soul(request.instance_id)
                    if not soul:
                        return "No soul found for this instance."
                    setattr(soul, field, value)
                    await self._state.save_soul(soul, source="update_soul", trigger=f"{field}={value}")
                    return f"Updated {field} to: {value}"
                return "State store is not available."
            elif tool_name == "manage_covenants":
                from kernos.kernel.covenant_manager import handle_manage_covenants
                cov_action = tool_input.get("action", "list")
                cov_result = await handle_manage_covenants(
                    self._state,
                    request.instance_id,
                    action=cov_action,
                    rule_id=tool_input.get("rule_id", ""),
                    new_description=tool_input.get("new_description", ""),
                    show_all=tool_input.get("show_all", False),
                )
                if cov_action == "update" and "Updated" in cov_result:
                    import asyncio
                    from kernos.kernel.covenant_manager import validate_covenant_set
                    id_match = re.search(r"new ID: (rule_\w+)", cov_result)
                    new_id = id_match.group(1) if id_match else ""
                    if new_id:
                        asyncio.create_task(
                            validate_covenant_set(
                                state=self._state,
                                events=self._events,
                                reasoning_service=self,
                                instance_id=request.instance_id,
                                new_rule_id=new_id,
                            )
                        )
                return cov_result
            elif tool_name == "manage_capabilities":
                return await self._handle_manage_capabilities(
                    request.instance_id,
                    tool_input.get("action", "list"),
                    tool_input.get("capability", ""),
                )
            elif tool_name == "manage_channels":
                from kernos.kernel.channels import handle_manage_channels
                if self._channel_registry:
                    return handle_manage_channels(
                        self._channel_registry,
                        tool_input.get("action", "list"),
                        tool_input.get("channel", ""),
                    )
                return "Channel registry is not available."
            elif tool_name == "send_to_channel":
                from kernos.kernel.channels import resolve_channel_alias
                from kernos.kernel.scheduler import resolve_owner_member_id
                channel_input = tool_input.get("channel", "")
                message_text = tool_input.get("message", "")
                if not channel_input or not message_text:
                    return "Error: both 'channel' and 'message' are required."
                resolved = resolve_channel_alias(channel_input)
                if not self._channel_registry:
                    return "Channel registry is not available."
                ch_info = self._channel_registry.get(resolved)
                if not ch_info:
                    available = [c.name for c in self._channel_registry.get_connected()]
                    return (
                        f"Channel '{resolved}' (from '{channel_input}') is not registered. "
                        f"Available channels: {', '.join(available) or 'none'}"
                    )
                if ch_info.status != "connected":
                    return f"Channel '{resolved}' exists but is not connected (status: {ch_info.status})."
                if not ch_info.can_send_outbound:
                    return f"Channel '{resolved}' is connected but cannot send outbound messages."
                if not self._handler:
                    return "Handler not available for outbound delivery."
                try:
                    member_id = resolve_owner_member_id(request.instance_id)
                    await self._handler.send_outbound(
                        request.instance_id, member_id, resolved, message_text,
                    )
                    logger.info(
                        "CROSS_CHANNEL_SEND: channel=%s resolved_from=%s len=%d",
                        resolved, channel_input, len(message_text),
                    )
                    return f"Message sent to {ch_info.display_name}."
                except Exception as exc:
                    return f"Failed to send to {resolved}: {exc}"
            elif tool_name == "manage_schedule":
                from kernos.kernel.scheduler import handle_manage_schedule
                if self._trigger_store:
                    return await handle_manage_schedule(
                        self._trigger_store,
                        request.instance_id,
                        member_id=request.active_space_id,
                        space_id=request.active_space_id,
                        action=tool_input.get("action", "list"),
                        trigger_id=tool_input.get("trigger_id", ""),
                        description=tool_input.get("description", ""),
                        reasoning_service=self,
                        conversation_id=request.conversation_id,
                        user_timezone=request.user_timezone,
                    )
                return "Scheduler is not available."
            elif tool_name == "request_tool":
                return await self._handle_request_tool(
                    request.instance_id,
                    request.active_space_id,
                    tool_input.get("capability_name", "unknown"),
                    tool_input.get("description", ""),
                )
            # CONFIRMED-PATH-DISPATCH-PARITY-V1 (2026-05-03): the
            # following five tools were dispatched only inside the
            # legacy reason() loop's elif chain. The legacy strike
            # removes that loop; their dispatch parity moves here so
            # the thin path retains reachability. Three of the five
            # carry admin-only space gating — see _assert_admin_space.
            elif tool_name == "remember_details":
                return await self._handle_remember_details(
                    request.instance_id,
                    request.active_space_id,
                    tool_input,
                )
            elif tool_name == "inspect_state":
                from kernos.kernel.introspection import build_user_truth_view
                return await build_user_truth_view(
                    request.instance_id,
                    self._state,
                    self._trigger_store,
                    self._registry,
                )
            elif tool_name == "dump_context":
                # SELF-ADMIN-TOOLS-V1: agent-callable equivalent of
                # the /dump slash command. Read-only introspection;
                # available in every space (founder decision
                # 2026-05-19: the agent benefits from being able to
                # self-introspect from any context, and there's no
                # destructive risk).
                from kernos.kernel.self_admin_tools import (
                    handle_dump_context_tool,
                )
                return handle_dump_context_tool(
                    request=request,
                    reason=tool_input.get("reason", "") or "",
                    include_content=bool(
                        tool_input.get("include_content", False)
                    ),
                )
            elif tool_name == "restart_self":
                # SELF-ADMIN-TOOLS-V1: agent-callable equivalent of
                # /restart. Available in every space (founder
                # decision 2026-05-19: the agent must be able to
                # self-recover from any state, including when
                # stuck outside the System space). Safety stays at
                # the handler level (two-call confirm=true required)
                # + the gate's hard_write classification, layered.
                from kernos.kernel.self_admin_tools import (
                    handle_restart_self_tool,
                )
                return handle_restart_self_tool(
                    reason=tool_input.get("reason", "") or "",
                    confirm=bool(tool_input.get("confirm", False)),
                    instance_id=request.instance_id,
                )
            elif tool_name == "inspect_tools":
                # TOOL-INTROSPECTION-V1 (2026-05-22): natural-prose
                # catalog reader. Reads catalog from handler;
                # composes the sentence substrate-side so the agent
                # gets English, not structured data.
                from kernos.kernel.tool_introspection import (
                    handle_inspect_tools,
                )
                _catalog = None
                handler = getattr(self, "_handler", None)
                if handler is not None:
                    _catalog = getattr(handler, "_tool_catalog", None)
                return handle_inspect_tools(
                    catalog=_catalog,
                    focus=str(tool_input.get("focus", "") or ""),
                    capability=str(tool_input.get("capability", "") or ""),
                )
            elif tool_name in (
                "git_fetch", "git_rev_parse", "git_status",
                "git_diff_for_review", "git_commit", "git_push",
            ):
                # GIT-OPERATIONS-PRIMITIVES-V1 (2026-05-22):
                # all 6 git tools dispatch through their named
                # handlers in kernos.kernel.git_operations.
                # workspace-guarded; commit + push are receipt-bound.
                from kernos.kernel import git_operations as _git_ops
                _handlers = {
                    "git_fetch": _git_ops.handle_git_fetch,
                    "git_rev_parse": _git_ops.handle_git_rev_parse,
                    "git_status": _git_ops.handle_git_status,
                    "git_diff_for_review": _git_ops.handle_git_diff_for_review,
                    "git_commit": _git_ops.handle_git_commit,
                    "git_push": _git_ops.handle_git_push,
                }
                return await _handlers[tool_name](
                    tool_input=tool_input,
                    instance_id=request.instance_id,
                    data_dir=os.environ.get("KERNOS_DATA_DIR", "./data"),
                )
            elif tool_name == "run_self_test_suite":
                # SELF-TEST-GATE-V1 (2026-05-22): curated smoke
                # test runner for the autonomous-improvement loop.
                from kernos.kernel.self_test_gate import (
                    handle_run_self_test_suite,
                )
                return await handle_run_self_test_suite(
                    tool_input=tool_input,
                    instance_id=request.instance_id,
                    data_dir=os.environ.get("KERNOS_DATA_DIR", "./data"),
                )
            elif tool_name == "improve_kernos":
                # IMPROVEMENT-LOOP-WORKFLOW-V1 (2026-05-22):
                # autonomous-improvement orchestrator entry point.
                from kernos.kernel.improvement_loop_workflow import (
                    handle_improve_kernos,
                )
                return await handle_improve_kernos(
                    handler=getattr(self, "_handler", None),
                    tool_input=tool_input,
                    instance_id=request.instance_id,
                    data_dir=os.environ.get("KERNOS_DATA_DIR", "./data"),
                )
            elif tool_name == "set_chain_model":
                _gate_msg = self._assert_admin_space(request, "set_chain_model")
                if _gate_msg is not None:
                    return _gate_msg
                from kernos.setup.admin_tools import (
                    set_chain_model as _set_chain_model,
                )
                admin_res = _set_chain_model(
                    chain=tool_input.get("chain", ""),
                    provider_id=tool_input.get("provider_id", ""),
                    model_id=tool_input.get("model_id", ""),
                )
                return (
                    admin_res.get("message")
                    or admin_res.get("error")
                    or "set_chain_model returned no result."
                )
            elif tool_name == "diagnose_llm_chain":
                _gate_msg = self._assert_admin_space(request, "diagnose_llm_chain")
                if _gate_msg is not None:
                    return _gate_msg
                import json as _json
                from kernos.setup.admin_tools import (
                    diagnose_llm_chain as _diagnose_llm_chain,
                )
                admin_res = _diagnose_llm_chain(
                    include_fallback_events=bool(
                        tool_input.get("include_fallback_events", False),
                    ),
                    instance_id=request.instance_id,
                )
                return _json.dumps(admin_res, indent=2, default=str)
            elif tool_name == "diagnose_messenger":
                _gate_msg = self._assert_admin_space(request, "diagnose_messenger")
                if _gate_msg is not None:
                    return _gate_msg
                import json as _json
                from kernos.cohorts.admin import (
                    diagnose_messenger as _diagnose_messenger,
                )
                idb = (
                    getattr(self._handler, "_instance_db", None)
                    if hasattr(self, "_handler") else None
                )
                admin_res = await _diagnose_messenger(
                    instance_id=request.instance_id,
                    member_a_id=tool_input.get("member_a_id", ""),
                    member_b_id=tool_input.get("member_b_id", ""),
                    state=self._state,
                    instance_db=idb,
                )
                return _json.dumps(admin_res, indent=2, default=str)
            elif tool_name in ("canvas_list", "canvas_create", "page_read",
                                "page_write", "page_list", "page_search",
                                "canvas_preference_extract",
                                "canvas_preference_confirm"):
                return await self._handle_canvas_tool(tool_name, tool_input, request)
            elif tool_name in (
                "request_reference", "store_reference",
                "create_reference_collection",
                "move_reference_to_canvas",
                "mark_reference_superseded",
                "quarantine_reference",
                "restore_reference_from_quarantine",
            ):
                return await self._handle_reference_tool(
                    tool_name, tool_input, request,
                )
            elif tool_name in (
                "record_closure_attempt",
                "run_closure_probe",
                "lookup_pattern_invariants",
            ):
                # SELF-IMPROVEMENT-CLOSURE-V1: dispatch the three
                # closure-machinery tools through their named
                # helpers in kernos.kernel.closure_store. The
                # ClosureStore instance is resolved from the
                # handler (set at bring-up).
                return await self._handle_closure_tool(
                    tool_name, tool_input, request,
                )
            elif tool_name in (
                "record_fix_authorization",
                "classify_proposed_fix",
                "validate_investigation_response",
                "maybe_run_closure_for_fix",
                "surface_to_user",
            ):
                # USER-INITIATED-IMPROVEMENT-TRIGGER-V1: dispatch
                # the five fix-authorization workflow tools.
                return await self._handle_fix_authorization_tool(
                    tool_name, tool_input, request,
                )
            else:
                return f"Kernel tool '{tool_name}' not handled."
        else:
            return await self._mcp.call_tool(tool_name, tool_input)

    async def _handle_fix_authorization_tool(
        self,
        tool_name: str,
        tool_input: dict,
        request: "ReasoningRequest",
    ) -> str:
        """USER-INITIATED-IMPROVEMENT-TRIGGER-V1 dispatch helper
        for the five fix-authorization workflow tools."""
        import json as _json
        from kernos.kernel.fix_authorization import (
            FixAuthorizationError,
            InvestigationResponseMalformed,
            classify_proposed_fix,
            maybe_run_closure_for_fix,
            record_fix_authorization,
            validate_investigation_response,
        )

        handler = getattr(self, "_handler", None)

        try:
            if tool_name == "classify_proposed_fix":
                # Pure function; no substrate dependency.
                result = await classify_proposed_fix(
                    instance_id=request.instance_id,
                    proposed_fix_summary=tool_input.get(
                        "proposed_fix_summary", "",
                    ),
                    proposed_fix_diff=tool_input.get(
                        "proposed_fix_diff", "",
                    ),
                    touches_paths=tool_input.get(
                        "touches_paths", [],
                    ) or [],
                    external_action=tool_input.get(
                        "external_action", "",
                    ),
                )
                return _json.dumps(result, sort_keys=True)

            if tool_name == "validate_investigation_response":
                # Validation; raises InvestigationResponseMalformed
                # on bad shape — workflow's on_failure: abort
                # fires when this raises.
                try:
                    result = validate_investigation_response(
                        investigation_outcome=tool_input.get(
                            "investigation_outcome", "",
                        ),
                        failure_mode=tool_input.get(
                            "failure_mode", "",
                        ),
                        proposed_fix_summary=tool_input.get(
                            "proposed_fix_summary", "",
                        ),
                        proposed_fix_diff=tool_input.get(
                            "proposed_fix_diff", "",
                        ),
                        external_action=tool_input.get(
                            "external_action", "",
                        ),
                        touches_paths=tool_input.get(
                            "touches_paths", [],
                        ),
                        summary=tool_input.get("summary", ""),
                    )
                except InvestigationResponseMalformed:
                    raise
                return _json.dumps(result, sort_keys=True)

            if tool_name == "record_fix_authorization":
                fa_store = (
                    getattr(handler, "_fix_authorization_store", None)
                    if handler is not None else None
                )
                if fa_store is None:
                    return (
                        "Fix-authorization substrate is not "
                        "available — fix_authorization_store "
                        "hasn't been wired in this process."
                    )
                result = await record_fix_authorization(
                    store=fa_store,
                    instance_id=request.instance_id,
                    request_id=tool_input.get("request_id", ""),
                    requester_member_id=tool_input.get(
                        "requester_member_id", "",
                    ),
                    source_space_id=tool_input.get(
                        "source_space_id", "",
                    ),
                    target_hint=tool_input.get("target_hint", ""),
                    request_text=tool_input.get("request_text", ""),
                    trigger_surface=tool_input.get(
                        "trigger_surface", "slash:/fix",
                    ),
                )
                return _json.dumps(result, sort_keys=True)

            if tool_name == "maybe_run_closure_for_fix":
                # Composes closure-v1 primitives. Needs both
                # closure_store (from handler) AND optional
                # callbacks (resolved lazily).
                closure_store = (
                    getattr(handler, "_closure_store", None)
                    if handler is not None else None
                )
                fp_store = (
                    getattr(handler, "_friction_pattern_store", None)
                    if handler is not None else None
                )

                def _transition(**kwargs):
                    if fp_store is None:
                        return None
                    return fp_store.transition_pattern_lifecycle(
                        **kwargs,
                    )

                events = (
                    getattr(handler, "_events", None)
                    or getattr(handler, "_event_stream", None)
                ) if handler is not None else None

                async def _emit(*, instance_id, event_type, payload):
                    if events is None:
                        return
                    await events.emit(
                        instance_id, event_type, payload,
                        space_id="",
                    )

                result = await maybe_run_closure_for_fix(
                    instance_id=request.instance_id,
                    related_pattern_id=tool_input.get(
                        "related_pattern_id", "",
                    ),
                    active_epoch=int(
                        tool_input.get("active_epoch", 0),
                    ),
                    closure_store=closure_store,
                    pattern_transition_fn=(
                        _transition if fp_store is not None
                        else None
                    ),
                    event_emit_fn=(
                        _emit if events is not None else None
                    ),
                )
                return _json.dumps(result, sort_keys=True)

            if tool_name == "surface_to_user":
                # v1: structured diagnostic write. The full
                # routing-through-the-agent-response-path is
                # deferred; v1 ships persistent diagnostic
                # records so the user-facing surfacing can be
                # observed and the operator can manually
                # forward via channel post if needed. Wiring
                # to the live channel send is a Phase D follow-
                # on once the workflow is shipped + soaked.
                import os as _os
                from pathlib import Path as _Path
                data_dir = _os.environ.get(
                    "KERNOS_DATA_DIR", "./data",
                )
                space_id = tool_input.get("space_id", "")
                message_kind = tool_input.get("message_kind", "")
                _surface_dir = (
                    _Path(data_dir)
                    / f"discord_{request.instance_id.split(':')[-1] if ':' in request.instance_id else request.instance_id}"
                    / "diagnostics"
                    / "fix_authorizations"
                )
                _surface_dir.mkdir(parents=True, exist_ok=True)
                _surface_file = _surface_dir / (
                    f"surface_{message_kind}_"
                    f"{tool_input.get('metadata', {}).get('request_id', 'unknown')}.json"
                )
                _payload = {
                    "instance_id": request.instance_id,
                    "space_id": space_id,
                    "member_id": tool_input.get("member_id", ""),
                    "message_kind": message_kind,
                    "body": tool_input.get("body", ""),
                    "metadata": tool_input.get("metadata", {}),
                    "surfaced_at": __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc,
                    ).isoformat(),
                }
                _surface_file.write_text(
                    _json.dumps(_payload, sort_keys=True, indent=2),
                )
                return _json.dumps({
                    "surfaced_at": _payload["surfaced_at"],
                    "diagnostic_path": str(_surface_file),
                }, sort_keys=True)

        except FixAuthorizationError as exc:
            # InvestigationResponseMalformed is a subclass of
            # FixAuthorizationError but we WANT it to propagate
            # (handled inside the if-branch via explicit raise).
            # All other FixAuthorizationError subclasses return
            # a friendly string here.
            if isinstance(exc, InvestigationResponseMalformed):
                raise
            return (
                f"fix_authorization_error: "
                f"{type(exc).__name__}: {exc}"
            )

        return f"Kernel tool '{tool_name}' not handled."

    async def _handle_closure_tool(
        self,
        tool_name: str,
        tool_input: dict,
        request: "ReasoningRequest",
    ) -> str:
        """SELF-IMPROVEMENT-CLOSURE-V1 dispatch helper for the three
        closure-machinery tools (record_closure_attempt,
        run_closure_probe, lookup_pattern_invariants).

        Resolves :class:`ClosureStore` from ``handler._closure_store``
        (set at bring-up). Returns a friendly string when the
        substrate isn't bound — surfaces the gap rather than
        crashing the turn.
        """
        import json as _json
        from kernos.kernel.closure_store import (
            ClosureStoreError,
            lookup_pattern_invariants,
            record_closure_attempt,
            run_closure_probe,
        )

        store = None
        handler = getattr(self, "_handler", None)
        if handler is not None:
            store = getattr(handler, "_closure_store", None)
        if store is None:
            return (
                "Closure substrate is not available — "
                "closure_store hasn't been wired in this process."
            )

        try:
            if tool_name == "lookup_pattern_invariants":
                result = await lookup_pattern_invariants(
                    store=store,
                    instance_id=request.instance_id,
                    pattern_id=tool_input.get("pattern_id", ""),
                )
                return _json.dumps(result, sort_keys=True)

            if tool_name == "record_closure_attempt":
                result = await record_closure_attempt(
                    store=store,
                    instance_id=request.instance_id,
                    pattern_id=tool_input.get("pattern_id", ""),
                    invariant_id=tool_input.get("invariant_id", ""),
                    active_epoch=int(
                        tool_input.get("active_epoch", 0),
                    ),
                    route=tool_input.get("route", "code_change_via_cc"),
                    route_payload=tool_input.get("route_payload") or {},
                    probe_kind=tool_input.get(
                        "probe_kind", "deterministic_introspection",
                    ),
                    probe_payload=tool_input.get("probe_payload") or {},
                    probe_payload_version=int(
                        tool_input.get("probe_payload_version", 1),
                    ),
                )
                return _json.dumps(result, sort_keys=True)

            if tool_name == "run_closure_probe":
                # Lazily resolve the pattern-transition + event-emit
                # callbacks so the handler can supply them without
                # the closure_store knowing about friction patterns
                # or the event stream.
                fp_store = getattr(
                    handler, "_friction_pattern_store", None,
                )

                def _transition(**kwargs):
                    if fp_store is None:
                        return None
                    return fp_store.transition_pattern_lifecycle(
                        **kwargs,
                    )

                events = getattr(handler, "_events", None) or getattr(
                    handler, "_event_stream", None,
                )

                async def _emit(*, instance_id, event_type, payload):
                    if events is None:
                        return
                    await events.emit(
                        instance_id, event_type, payload, space_id="",
                    )

                result = await run_closure_probe(
                    store=store,
                    instance_id=request.instance_id,
                    closure_id=tool_input.get("closure_id", ""),
                    pattern_transition_fn=(
                        _transition if fp_store is not None else None
                    ),
                    event_emit_fn=(
                        _emit if events is not None else None
                    ),
                )
                return _json.dumps(result, sort_keys=True)

        except ClosureStoreError as exc:
            return f"closure error: {type(exc).__name__}: {exc}"

        return f"Kernel tool '{tool_name}' not handled."

    async def _handle_reference_tool(
        self,
        tool_name: str,
        tool_input: dict,
        request: "ReasoningRequest",
    ) -> str:
        """Dispatch helper for the seven REFERENCE-PRIMITIVE-V1 tools.

        Resolves :class:`ReferenceService` from
        ``handler._wlp_substrate.reference_service`` (production) or
        ``handler._reference_service`` (test convenience). Returns a
        friendly string when the substrate isn't bound — surfaces
        the gap rather than crashing the turn."""
        import json as _json
        from kernos.kernel.reference.tools import ReferenceServiceContext

        service = None
        handler = getattr(self, "_handler", None)
        if handler is not None:
            substrate = getattr(handler, "_wlp_substrate", None)
            if substrate is not None:
                service = getattr(substrate, "reference_service", None)
            if service is None:
                service = getattr(handler, "_reference_service", None)
        if service is None:
            return (
                "Reference primitive substrate is not available — "
                "the catalog hasn't been wired in this process."
            )
        ctx = ReferenceServiceContext(
            instance_id=request.instance_id,
            domain_id=request.active_space_id,
            member_id=getattr(request, "member_id", "") or "",
        )
        try:
            if tool_name == "request_reference":
                # Defaulted on the service side; passing the agent
                # value through when present so callers can opt into
                # higher fan-out for multi-topic briefs.
                _max_targets = tool_input.get("max_targets")
                _kwargs: dict[str, Any] = {
                    "ctx": ctx,
                    "brief_request": tool_input.get("brief_request", ""),
                }
                if isinstance(_max_targets, int):
                    _kwargs["max_targets"] = _max_targets
                result = await service.handle_request_reference(**_kwargs)
            elif tool_name == "store_reference":
                result = await service.handle_store_reference(
                    ctx=ctx,
                    content=tool_input.get("content", ""),
                    collection=tool_input.get("collection", ""),
                    filename=tool_input.get("filename", ""),
                    trust_tier=tool_input.get(
                        "trust_tier", "agent_authored",
                    ),
                    metadata=tool_input.get("metadata") or {},
                )
            elif tool_name == "create_reference_collection":
                result = await service.handle_create_reference_collection(
                    ctx=ctx,
                    name=tool_input.get("name", ""),
                    purpose=tool_input.get("purpose", ""),
                    trust_tier=tool_input.get(
                        "trust_tier", "agent_authored",
                    ),
                    refresh_policy=tool_input.get(
                        "refresh_policy", "snapshot",
                    ),
                    provenance=tool_input.get("provenance") or {},
                )
            elif tool_name == "move_reference_to_canvas":
                result = await service.handle_move_reference_to_canvas(
                    ctx=ctx,
                    entry_id=tool_input.get("entry_id", ""),
                    target_canvas=tool_input.get("target_canvas", ""),
                )
            elif tool_name == "mark_reference_superseded":
                result = await service.handle_mark_reference_superseded(
                    ctx=ctx,
                    old_entry_id=tool_input.get("old_entry_id", ""),
                    new_entry_id=tool_input.get("new_entry_id", ""),
                    reason=tool_input.get("reason", ""),
                )
            elif tool_name == "quarantine_reference":
                result = await service.handle_quarantine_reference(
                    ctx=ctx,
                    entry_id=tool_input.get("entry_id", ""),
                    reason=tool_input.get("reason", ""),
                )
            elif tool_name == "restore_reference_from_quarantine":
                result = (
                    await service.handle_restore_reference_from_quarantine(
                        ctx=ctx,
                        entry_id=tool_input.get("entry_id", ""),
                    )
                )
            else:
                return f"Reference tool {tool_name!r} not handled."
            return _json.dumps(result, indent=2, default=str)
        except Exception as exc:
            logger.exception(
                "REFERENCE_TOOL_DISPATCH_FAILED tool=%s exc=%s",
                tool_name, exc,
            )
            return (
                f"Reference tool {tool_name} failed: "
                f"{type(exc).__name__}: {exc}"
            )

    def _is_concurrent_safe(self, tool_name: str) -> bool:
        """A tool is concurrent-safe ONLY if explicitly classified as 'read'.

        Unknown, soft_write, hard_write all stay sequential.
        Conservative: if classification fails, return False.
        """
        try:
            effect = self._get_gate().classify_tool_effect(tool_name, None, None)
            return effect == "read"
        except Exception:
            return False

    async def _dispatch_cross_space_request(
        self, tool_input: dict, request: "ReasoningRequest",
    ) -> str:
        """Shared dispatch for the ``request_space_action`` tool.
        Both the legacy and decoupled tool-loop paths route through
        this helper. Returns a JSON string of the receipt
        (suitable as a tool_result content)."""
        import json as _json
        from kernos.kernel.cross_space.tool import (
            build_request_from_tool_input,
            get_service as _cs_get_service,
        )
        from kernos.kernel.external_agents.errors import (
            ExternalAgentError as _ExtError,
        )
        try:
            svc = await _cs_get_service(
                state=self._state,
                events=self._events,
                audit=self._audit,
                gate=self._get_gate(),
                space_locks=getattr(self._handler, "_space_locks", None),
            )
            req = build_request_from_tool_input(
                tool_input=tool_input,
                instance_id=request.instance_id,
                origin_space_id=request.active_space_id,
                initiating_member_id=getattr(request, "member_id", "")
                                     or request.instance_id,
                source_turn_id=getattr(request, "conversation_id", "") or "",
            )
            receipt = await svc.dispatch(req)
            return _json.dumps(receipt.to_tool_result())
        except _ExtError as exc:
            logger.info(
                "request_space_action returned typed error: %s: %s",
                type(exc).__name__, exc,
            )
            return _json.dumps({
                "error": type(exc).__name__,
                "message": str(exc),
            })

    async def _handle_request_tool(
        self,
        instance_id: str,
        space_id: str,
        capability_name: str,
        description: str,
    ) -> str:
        """Handle a request_tool call.

        1. If capability_name matches an installed capability: activate silently
        2. If capability_name is 'unknown': fuzzy match against registry using description
        3. If not installed: direct user to system space
        """
        from kernos.capability.registry import CapabilityStatus

        if not self._registry:
            return "Tool registry is not available right now."

        # Exact match (when capability_name is known)
        if capability_name and capability_name != "unknown":
            cap = self._registry.get(capability_name)
            if cap and cap.status == CapabilityStatus.CONNECTED:
                await self._activate_tool_for_space(instance_id, space_id, capability_name)
                tools = cap.tools
                return (
                    f"Activated '{cap.name}' for this space. "
                    f"Available tools: {', '.join(tools)}. "
                    f"These will be available in this space going forward."
                )

        # Fuzzy match — check if any capability name or tool name appears in description
        desc_lower = description.lower()
        # Sort: universal first (prefer broadly useful tools)
        candidates = sorted(
            [c for c in self._registry.get_all() if c.status == CapabilityStatus.CONNECTED],
            key=lambda c: (not c.universal, c.name),
        )
        best_match = None
        for cap in candidates:
            if (cap.name.lower() in desc_lower or
                    any(tool.lower() in desc_lower for tool in cap.tools)):
                best_match = cap
                break

        if best_match:
            await self._activate_tool_for_space(instance_id, space_id, best_match.name)
            tools = best_match.tools
            return (
                f"Found and activated '{best_match.name}' for this space. "
                f"Available tools: {', '.join(tools)}. "
                f"These will be available in this space going forward."
            )

        # Not installed
        return (
            f"I don't have a tool matching '{capability_name}' installed. "
            f"To get new tools set up, go to the System space for installation. "
            f"Want me to help you find the right tool there?"
        )

    async def _activate_tool_for_space(
        self, instance_id: str, space_id: str, capability_name: str
    ) -> None:
        """Add a capability to a space's active_tools list and persist."""
        if not self._state:
            return
        space = await self._state.get_context_space(instance_id, space_id)
        if space and capability_name not in space.active_tools:
            space.active_tools.append(capability_name)
            await self._state.update_context_space(
                instance_id, space_id, {"active_tools": space.active_tools}
            )

    async def _handle_manage_capabilities(
        self, instance_id: str, action: str, capability: str
    ) -> str:
        """Handle the manage_capabilities kernel tool."""
        from kernos.capability.registry import CapabilityStatus

        if not self._registry:
            return "Tool registry is not available right now."

        if action == "list":
            caps = self._registry.get_all()
            if not caps:
                return "No capabilities registered."
            lines = ["Capabilities:"]
            for cap in sorted(caps, key=lambda c: c.name):
                lines.append(
                    f"- {cap.name} ({cap.display_name}): "
                    f"status={cap.status.value}, source={cap.source}"
                )
                # Show individual tool names for connected capabilities
                if cap.tool_effects:
                    tool_names = ", ".join(sorted(cap.tool_effects.keys()))
                    lines.append(f"    Tools: {tool_names}")
            return "\n".join(lines)

        if action == "enable":
            if not capability:
                return "Error: 'capability' is required for enable."
            cap = self._registry.get(capability)
            if not cap:
                return f"Error: Capability '{capability}' not found."
            if cap.status == CapabilityStatus.CONNECTED:
                return f"'{capability}' is already enabled."
            if cap.status != CapabilityStatus.DISABLED:
                return (
                    f"Cannot enable '{capability}' — current status is "
                    f"'{cap.status.value}'. Only disabled capabilities can be enabled."
                )
            self._registry.enable(capability)
            self._tools_changed = True
            return f"Enabled '{capability}'. Its tools are now visible."

        if action == "disable":
            if not capability:
                return "Error: 'capability' is required for disable."
            cap = self._registry.get(capability)
            if not cap:
                return f"Error: Capability '{capability}' not found."
            if cap.status == CapabilityStatus.DISABLED:
                return f"'{capability}' is already disabled."
            if cap.status != CapabilityStatus.CONNECTED:
                return (
                    f"Cannot disable '{capability}' — current status is "
                    f"'{cap.status.value}'. Only connected capabilities can be disabled."
                )
            self._registry.disable(capability)
            self._tools_changed = True
            return (
                f"Disabled '{capability}'. Its tools are now hidden from the tool list. "
                f"The server is still running — re-enable will be instant."
            )

        if action == "install":
            if not capability:
                return "Error: 'capability' is required for install."
            # Route through request_tool for existing flow
            return await self._handle_request_tool(
                instance_id, "", capability, f"Install {capability}"
            )

        if action == "remove":
            if not capability:
                return "Error: 'capability' is required for remove."
            cap = self._registry.get(capability)
            if not cap:
                return f"Error: Capability '{capability}' not found."
            if cap.source == "default":
                return (
                    f"Cannot remove '{capability}' — it's a pre-installed default. "
                    f"Use disable instead to hide it from the tool list."
                )
            # User-installed: disconnect and suppress
            if self._mcp and cap.status in (
                CapabilityStatus.CONNECTED, CapabilityStatus.DISABLED
            ):
                await self._mcp.disconnect_one(cap.server_name or capability)
            cap.status = CapabilityStatus.SUPPRESSED
            cap.tools = []
            self._tools_changed = True
            return f"Removed '{capability}'. It has been uninstalled."

        return f"Unknown action: '{action}'. Use list, enable, disable, install, or remove."

    async def _handle_canvas_tool(
        self, tool_name: str, tool_input: dict, request: "ReasoningRequest",
    ) -> str:
        """Dispatch canvas_*/page_* tool calls to CanvasService.

        Pillar 3 of CANVAS-V1. Consent-on-cross-member-writes lives here
        (at the tool layer, above the dispatch gate): page_write to a
        cross-member non-log page without ``confirmed=true`` short-circuits
        and tells the agent to re-ask the user.
        """
        # Lazy-resolve the canvas service via the handler. Keeps the
        # wire-up simple: server.py/bootstrap attach _instance_db to the
        # handler post-init, and the first canvas tool call constructs
        # the service on demand.
        canvas = self._canvas
        if canvas is None and self._handler and hasattr(self._handler, "_get_canvas_service"):
            canvas = self._handler._get_canvas_service()
            if canvas is not None:
                self._canvas = canvas
        if not canvas:
            return "Canvas service is not available."

        import json as _json

        instance_id = request.instance_id
        member_id = getattr(request, "member_id", "") or ""

        async def _assert_access(canvas_id: str) -> str | None:
            idb = getattr(self._handler, "_instance_db", None) if self._handler else None
            if not idb:
                return "Instance database is not available."
            ok = await idb.member_has_canvas_access(
                canvas_id=canvas_id, member_id=member_id,
            )
            if not ok:
                return _json.dumps({
                    "ok": False,
                    "error": "canvas_not_accessible",
                    "detail": f"Canvas {canvas_id!r} does not exist or is not accessible.",
                })
            return None

        if tool_name == "canvas_list":
            include_archived = bool(tool_input.get("include_archived", False))
            canvases = await self._canvas.list_for_member(
                member_id=member_id, include_archived=include_archived,
            )
            return _json.dumps({"ok": True, "canvases": canvases}, default=str)

        if tool_name == "canvas_create":
            result = await self._canvas.create(
                instance_id=instance_id,
                creator_member_id=member_id,
                name=tool_input.get("name", ""),
                scope=tool_input.get("scope", ""),
                members=tool_input.get("members") or [],
                description=tool_input.get("description", ""),
                default_page_type=tool_input.get("default_page_type", "note"),
                pinned_to_spaces=tool_input.get("pinned_to_spaces") or [],
            )
            if result.ok:
                await self._dispatch_canvas_offer(
                    request=request,
                    creator_member_id=member_id,
                    canvas_id=result.canvas_id,
                    canvas_name=result.extra.get("name", ""),
                    scope=result.extra.get("scope", ""),
                    notify=result.extra.get("notify") or [],
                )
                # SECTION-MARKERS + GARDENER Pillar 3: kick off initial-shape
                # application asynchronously so canvas_create returns
                # immediately and the member's agent can keep moving while
                # the Gardener picks a pattern + instantiates pages.
                intent = tool_input.get("intent") or ""
                explicit_pattern = tool_input.get("pattern") or ""
                if intent or explicit_pattern:
                    await self._schedule_gardener_initial_shape(
                        request=request,
                        canvas_id=result.canvas_id,
                        canvas_name=result.extra.get("name", ""),
                        scope=result.extra.get("scope", ""),
                        creator_member_id=member_id,
                        intent=intent,
                        explicit_pattern=explicit_pattern,
                    )
            return _json.dumps(result.to_dict(), default=str)

        if tool_name == "page_read":
            canvas_id = tool_input.get("canvas_id", "")
            err = await _assert_access(canvas_id)
            if err:
                return err
            result = await self._canvas.page_read(
                instance_id=instance_id,
                canvas_id=canvas_id,
                page_slug=tool_input.get("page_path", ""),
            )
            return _json.dumps(result.to_dict(), default=str)

        if tool_name == "page_list":
            canvas_id = tool_input.get("canvas_id", "")
            err = await _assert_access(canvas_id)
            if err:
                return err
            pages = await self._canvas.page_list(
                instance_id=instance_id, canvas_id=canvas_id,
            )
            return _json.dumps({"ok": True, "canvas_id": canvas_id, "pages": pages}, default=str)

        if tool_name == "page_search":
            query = tool_input.get("query", "")
            canvas_id = tool_input.get("canvas_id") or ""
            limit = int(tool_input.get("limit", 20) or 20)
            if canvas_id:
                err = await _assert_access(canvas_id)
                if err:
                    return err
                canvas_ids = [canvas_id]
            else:
                canvases = await self._canvas.list_for_member(member_id=member_id)
                canvas_ids = [c["canvas_id"] for c in canvases if c.get("canvas_id")]
            hits = await self._canvas.page_search(
                instance_id=instance_id,
                canvas_ids=canvas_ids,
                query=query,
                limit=limit,
            )
            return _json.dumps({"ok": True, "hits": hits}, default=str)

        if tool_name == "page_write":
            canvas_id = tool_input.get("canvas_id", "")
            page_slug = tool_input.get("page_path", "")
            err = await _assert_access(canvas_id)
            if err:
                return err

            # Consent gate for cross-member shared writes.
            # Scope: team or specific canvases with >1 member AND non-log page
            # writes require explicit confirmed=true. Personal canvases and
            # log pages skip this — personal is solo, logs are append-only.
            idb = getattr(self._handler, "_instance_db", None) if self._handler else None
            canvas_row = await idb.get_canvas(canvas_id) if idb else None
            canvas_scope = (canvas_row or {}).get("scope", "") if canvas_row else ""
            page_type = (tool_input.get("page_type") or "note").lower()
            confirmed = bool(tool_input.get("confirmed", False))

            is_cross_member = canvas_scope in ("team", "specific")
            requires_consent = (
                is_cross_member
                and page_type != "log"
                and not confirmed
            )
            if requires_consent:
                members = await idb.list_canvas_members(canvas_id) if idb else []
                other_members = [m for m in members if m and m != member_id]
                return _json.dumps({
                    "ok": False,
                    "requires_confirmation": True,
                    "canvas_id": canvas_id,
                    "page_path": page_slug,
                    "scope": canvas_scope,
                    "other_members": other_members,
                    "proposed_summary": (tool_input.get("body") or "")[:200],
                    "detail": (
                        "This is a shared canvas with other members. Surface the "
                        "proposed write to the user and re-call page_write with "
                        "confirmed=true after they approve."
                    ),
                })

            result = await self._canvas.page_write(
                instance_id=instance_id,
                canvas_id=canvas_id,
                page_slug=page_slug,
                body=tool_input.get("body", ""),
                writer_member_id=member_id,
                title=tool_input.get("title"),
                page_type=tool_input.get("page_type"),
                state=tool_input.get("state"),
            )
            if result.ok:
                await self._notify_canvas_watchers(
                    request=request,
                    writer_member_id=member_id,
                    canvas_id=canvas_id,
                    page_path=page_slug,
                    watchers=result.extra.get("watchers") or [],
                    state_changed=bool(result.extra.get("state_changed")),
                    new_state=result.extra.get("state", ""),
                    prev_state=result.extra.get("prev_state", ""),
                )
                await self._fire_canvas_routes(
                    request=request,
                    writer_member_id=member_id,
                    canvas_id=canvas_id,
                    page_path=page_slug,
                    state_changed=bool(result.extra.get("state_changed")),
                    new_state=result.extra.get("state", ""),
                    prev_state=result.extra.get("prev_state", ""),
                    route_targets=result.extra.get("route_targets") or [],
                    consult_operator=bool(result.extra.get("consult_operator")),
                )
            return _json.dumps(result.to_dict(), default=str)

        if tool_name == "canvas_preference_extract":
            canvas_id = tool_input.get("canvas_id", "")
            utterance = (tool_input.get("utterance") or "").strip()
            err = await _assert_access(canvas_id)
            if err:
                return err
            if not utterance:
                return _json.dumps({
                    "ok": False,
                    "error": "utterance is required (member's verbatim words)",
                })
            return await self._handle_canvas_preference_extract(
                request=request, canvas=canvas, canvas_id=canvas_id,
                utterance=utterance,
            )

        if tool_name == "canvas_preference_confirm":
            canvas_id = tool_input.get("canvas_id", "")
            err = await _assert_access(canvas_id)
            if err:
                return err
            name = (tool_input.get("preference_name") or "").strip()
            action = (tool_input.get("action") or "").strip().lower()
            if not name or action not in ("confirm", "discard"):
                return _json.dumps({
                    "ok": False,
                    "error": "preference_name required; action must be 'confirm' or 'discard'.",
                })
            resolved = await canvas.resolve_pending_preference(
                instance_id=instance_id, canvas_id=canvas_id,
                name=name, action=action,
            )
            if resolved is None:
                return _json.dumps({
                    "ok": False,
                    "error": f"no pending preference named {name!r} (may have expired or been resolved already)",
                })
            return _json.dumps({"ok": True, "resolved": resolved})

        return f"Canvas tool {tool_name!r} not dispatched."

    async def _handle_canvas_preference_extract(
        self, *, request: "ReasoningRequest", canvas: Any, canvas_id: str,
        utterance: str,
    ) -> str:
        """Run the Gardener's preference-extraction consultation.

        Pref-Capture Commit 3 tool path. Reads the canvas's pattern,
        pulls the pattern body from the Gardener's PatternCache to
        harvest intent-hook vocabulary, runs ``consult_preference_extraction``,
        and — if the result surfaces (high-confidence + wired effect_kind) —
        writes the preference to ``pending_preferences`` for explicit
        confirmation via ``canvas_preference_confirm``.

        Returns a JSON dict the agent uses to decide whether to surface
        the pending preference to the member.
        """
        import json as _json
        from kernos.cohorts.gardener import (
            PreferenceExtractionContext, extract_intent_hook_names,
        )

        instance_id = request.instance_id

        # Canvas must exist and carry a declared pattern.
        try:
            defaults = await canvas._canvas_defaults(instance_id, canvas_id)
        except Exception:
            defaults = {}
        pattern_name = (defaults.get("pattern") or "").strip()
        if not pattern_name or pattern_name == "unmatched":
            return _json.dumps({
                "ok": True, "matched": False,
                "reason": "canvas has no declared pattern; no intent-hook vocabulary available",
            })

        # Resolve pattern body via the Gardener's PatternCache so we can
        # harvest intent-hook vocabulary. Gardener is the pattern-content
        # authority; this keeps canvas.py out of library-layer concerns.
        gardener = None
        if self._handler and hasattr(self._handler, "_get_gardener_service"):
            gardener = self._handler._get_gardener_service()
        if gardener is None:
            return _json.dumps({
                "ok": False,
                "error": "Gardener service is not available",
            })
        await gardener._ensure_patterns_loaded(instance_id)
        cached = gardener.patterns.get(pattern_name)
        if cached is None:
            return _json.dumps({
                "ok": True, "matched": False,
                "reason": f"pattern {pattern_name!r} not in library — nothing to extract against",
            })

        intent_hooks = extract_intent_hook_names(cached.body)

        # Preferences context — confirmed + declined.
        try:
            confirmed_prefs = await canvas.get_preferences(
                instance_id=instance_id, canvas_id=canvas_id,
            )
        except Exception:
            confirmed_prefs = {}
        declined_raw = defaults.get("declined_preferences") or []
        declined_names = [
            d.get("name", "") for d in declined_raw if isinstance(d, dict)
        ]

        ctx = PreferenceExtractionContext(
            instance_id=instance_id,
            canvas_id=canvas_id,
            canvas_pattern=pattern_name,
            utterance=utterance,
            known_intent_hook_names=intent_hooks,
            current_preferences=dict(confirmed_prefs),
            declined_preference_names=declined_names,
        )
        result = await gardener.consult_preference_extraction(ctx)

        # Low/medium confidence or unwired effect kinds silently no-op —
        # member never sees a confirmation for a preference that won't do
        # anything (the design review revision #2).
        if not result.should_surface:
            return _json.dumps({
                "ok": True,
                "matched": result.matched,
                "confidence": result.confidence,
                "effect_kind": result.effect_kind,
                "reason": (
                    "extraction no-op: either unmatched, low confidence, "
                    "or effect kind isn't wired in v1"
                ),
            })

        # High-confidence + wired effect → move to pending_preferences.
        pending_entry = {
            "name": result.preference_name,
            "value": result.preference_value,
            "effect_kind": result.effect_kind,
            "evidence": result.evidence,
            "confidence": result.confidence,
        }
        if result.supersedes:
            pending_entry["supersedes"] = result.supersedes
        await canvas.add_pending_preference(
            instance_id=instance_id, canvas_id=canvas_id,
            preference=pending_entry,
        )
        return _json.dumps({
            "ok": True,
            "matched": True,
            "needs_confirmation": True,
            "preference_name": result.preference_name,
            "preference_value": result.preference_value,
            "effect_kind": result.effect_kind,
            "evidence": result.evidence,
            "supersedes": result.supersedes,
            "confirmation_tool": "canvas_preference_confirm",
            "note": (
                "Preference is in pending_preferences awaiting explicit "
                "member confirmation. Auto-apply consent modes do NOT "
                "extend to preference capture. Expires in 24h if not resolved."
            ),
        })

    async def _dispatch_canvas_offer(
        self, *, request: "ReasoningRequest",
        creator_member_id: str, canvas_id: str, canvas_name: str,
        scope: str, notify: list[str],
    ) -> None:
        """Send ``canvas_offer`` relational messages to target members.

        Best-effort: each addressee is an independent send; one failure does
        not block the others. The ``__team__`` sentinel expands to all
        instance members except the creator.
        """
        if not notify:
            return
        dispatcher = None
        if self._handler and hasattr(self._handler, "_get_relational_dispatcher"):
            dispatcher = self._handler._get_relational_dispatcher()
        if not dispatcher:
            return

        idb = getattr(self._handler, "_instance_db", None) if self._handler else None
        resolved: list[str] = []
        for m in notify:
            if m == "__team__" and idb is not None:
                try:
                    members = await idb.list_members()
                    for row in members:
                        mid = row.get("member_id") if isinstance(row, dict) else None
                        if mid and mid != creator_member_id and mid not in resolved:
                            resolved.append(mid)
                except Exception as exc:
                    logger.debug("CANVAS_TEAM_RESOLVE_FAILED: %s", exc)
            elif m and m != creator_member_id and m not in resolved:
                resolved.append(m)

        identity = ""
        if idb is not None:
            try:
                prof = await idb.get_member_profile(creator_member_id)
                if prof:
                    identity = prof.get("agent_name") or prof.get("display_name") or ""
            except Exception:
                pass

        content = (
            f"A new {scope} canvas '{canvas_name}' was created "
            f"and you have access. (canvas_id: {canvas_id})"
        )
        for addressee in resolved:
            try:
                await dispatcher.send(
                    instance_id=request.instance_id,
                    origin_member_id=creator_member_id,
                    origin_agent_identity=identity,
                    addressee=addressee,
                    intent="inform",
                    content=content,
                    urgency="normal",
                    envelope_type="canvas_offer",
                    canvas_id=canvas_id,
                )
            except Exception as exc:
                logger.debug("CANVAS_OFFER_SEND_FAILED: to=%s %s", addressee, exc)

    async def _notify_canvas_watchers(
        self, *, request: "ReasoningRequest",
        writer_member_id: str, canvas_id: str, page_path: str,
        watchers: list[str], state_changed: bool, new_state: str,
        prev_state: str,
    ) -> None:
        """Send whisper-style notifications to page watchers on state change.

        v1 semantics (spec Pillar 4): watcher whispers fire only on
        ``state_changed`` and are coalesced by (canvas_id, page_path,
        watcher_member_id) within a 10-minute window. Plain body edits do
        not wake a watcher — state changes do.

        Coalescing is in-process (per reasoning service). If the process
        restarts the window resets — acceptable for v1; persistent
        coalescing would require a new table and isn't in scope.
        """
        if not watchers or not state_changed:
            return
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        window = timedelta(minutes=10)
        if not hasattr(self, "_canvas_watcher_last"):
            self._canvas_watcher_last: dict[tuple[str, str, str], datetime] = {}

        dispatcher = None
        if self._handler and hasattr(self._handler, "_get_relational_dispatcher"):
            dispatcher = self._handler._get_relational_dispatcher()
        idb = getattr(self._handler, "_instance_db", None) if self._handler else None
        identity = ""
        if idb is not None:
            try:
                prof = await idb.get_member_profile(writer_member_id)
                if prof:
                    identity = prof.get("agent_name") or prof.get("display_name") or ""
            except Exception:
                pass

        for watcher in watchers:
            if not watcher or watcher == writer_member_id:
                continue
            key = (canvas_id, page_path, watcher)
            last = self._canvas_watcher_last.get(key)
            if last and (now - last) < window:
                continue
            self._canvas_watcher_last[key] = now
            if not dispatcher:
                continue
            content = (
                f"Canvas page '{page_path}' state changed from "
                f"{prev_state or '(none)'} → {new_state or '(none)'}."
            )
            try:
                await dispatcher.send(
                    instance_id=request.instance_id,
                    origin_member_id=writer_member_id,
                    origin_agent_identity=identity,
                    addressee=watcher,
                    intent="inform",
                    content=content,
                    urgency="normal",
                    envelope_type="canvas_watch",
                    canvas_id=canvas_id,
                )
            except Exception as exc:
                logger.debug("CANVAS_WATCH_SEND_FAILED: to=%s %s", watcher, exc)

    async def _populate_my_tools_page(
        self, *, request: "ReasoningRequest", descriptor_file: str,
    ) -> None:
        """Observer on successful register_tool → write page to My Tools.

        Reads the descriptor file the workspace just validated (same path
        convention as WorkspaceManager.register_tool) and appends a
        structured page to the member's My Tools canvas. Silent on every
        failure path — tool registration has already succeeded and must
        not be reversed by a canvas-write hiccup.
        """
        if not self._handler or not hasattr(self._handler, "_instance_db"):
            return
        try:
            from pathlib import Path
            import json as _json
            from kernos.utils import _safe_name
            from kernos.setup.seed_canvases import append_my_tools_page

            canvas_svc = None
            if hasattr(self._handler, "_get_canvas_service"):
                canvas_svc = self._handler._get_canvas_service()
            if canvas_svc is None:
                return

            data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
            space_dir = (
                Path(data_dir) / _safe_name(request.instance_id)
                / "spaces" / request.active_space_id
            )
            desc_path = space_dir / descriptor_file
            if not desc_path.is_file():
                return
            descriptor = _json.loads(desc_path.read_text(encoding="utf-8"))
            tool_name = descriptor.get("name", "")
            if not tool_name:
                return

            member_id = getattr(request, "member_id", "") or ""
            if not member_id:
                return

            await append_my_tools_page(
                instance_id=request.instance_id,
                member_id=member_id,
                tool_name=tool_name,
                descriptor=descriptor,
                canvas_service=canvas_svc,
                instance_db=self._handler._instance_db,
            )
        except Exception as exc:
            logger.debug("MY_TOOLS_PAGE_POPULATE_FAILED: %s", exc)

    async def _schedule_gardener_initial_shape(
        self,
        *,
        request: "ReasoningRequest",
        canvas_id: str,
        canvas_name: str,
        scope: str,
        creator_member_id: str,
        intent: str,
        explicit_pattern: str,
    ) -> None:
        """Schedule Gardener initial-shape application in the background.

        Spec Pillar 3: canvas_create returns immediately; the Gardener
        picks a pattern and instantiates its declared pages asynchronously.
        Swallows all errors — pattern application is a best-effort
        enrichment of a canvas that already exists.
        """
        gardener = None
        if self._handler and hasattr(self._handler, "_get_gardener_service"):
            gardener = self._handler._get_gardener_service()
        if gardener is None:
            return

        import asyncio as _asyncio

        async def _run():
            try:
                await gardener.apply_initial_shape(
                    instance_id=request.instance_id,
                    canvas_id=canvas_id,
                    canvas_name=canvas_name,
                    scope=scope,
                    creator_member_id=creator_member_id,
                    intent=intent,
                    explicit_pattern=explicit_pattern,
                )
            except Exception as exc:
                logger.debug("GARDENER_APPLY_INITIAL_SHAPE_FAILED: %s", exc)

        _asyncio.create_task(_run(), name=f"gardener_initial_shape_{canvas_id}")

    async def _fire_canvas_routes(
        self, *, request: "ReasoningRequest",
        writer_member_id: str, canvas_id: str, page_path: str,
        state_changed: bool, new_state: str, prev_state: str,
        route_targets: list[str], consult_operator: bool,
    ) -> None:
        """Fire routes-lite on a state-changed page_write.

        Targets:
          - ``operator`` — the canvas owner (the member who created the canvas)
          - ``member:<id>`` — a specific member
          - ``space:<id>`` — NOT SUPPORTED in v1; logged as
            ``route_target_not_supported_in_v1`` and skipped.

        Operator precedence: if ``consult_operator`` resolved true via the
        consult_operator_at inheritance chain (instance → canvas → page,
        replacing), the operator is added to the target set regardless of
        whether the page's ``routes`` declared them. This is the
        "non-bypassable operator precedence" in the spec.
        """
        if not state_changed:
            return
        from kernos.kernel.canvas import classify_route_target

        dispatcher = None
        if self._handler and hasattr(self._handler, "_get_relational_dispatcher"):
            dispatcher = self._handler._get_relational_dispatcher()
        idb = getattr(self._handler, "_instance_db", None) if self._handler else None

        resolved_targets: list[tuple[str, str]] = []
        for t in route_targets:
            kind, arg = classify_route_target(t)
            if kind == "space":
                logger.info(
                    "CANVAS_ROUTE_TARGET_NOT_SUPPORTED_IN_V1: canvas=%s page=%s target=%s",
                    canvas_id, page_path, t,
                )
                continue
            if kind == "unknown":
                logger.debug("CANVAS_ROUTE_TARGET_UNKNOWN: %r", t)
                continue
            resolved_targets.append((kind, arg))

        # Non-bypassable operator precedence (consult_operator_at).
        if consult_operator and not any(k == "operator" for k, _ in resolved_targets):
            resolved_targets.append(("operator", ""))

        if not resolved_targets:
            return

        # Resolve operator → canvas owner
        owner_member_id = ""
        if idb is not None:
            try:
                row = await idb.get_canvas(canvas_id)
                owner_member_id = (row or {}).get("owner_member_id", "") or ""
            except Exception:
                pass

        identity = ""
        if idb is not None:
            try:
                prof = await idb.get_member_profile(writer_member_id)
                if prof:
                    identity = prof.get("agent_name") or prof.get("display_name") or ""
            except Exception:
                pass

        # Dedup + final addressee list
        addressees: list[str] = []
        for kind, arg in resolved_targets:
            if kind == "operator":
                if owner_member_id and owner_member_id not in addressees:
                    addressees.append(owner_member_id)
            elif kind == "member":
                if arg and arg not in addressees:
                    addressees.append(arg)

        content = (
            f"Canvas page '{page_path}' state changed "
            f"{prev_state or '(none)'} → {new_state or '(none)'}."
        )
        for addressee in addressees:
            if addressee == writer_member_id:
                continue
            if not dispatcher:
                break
            try:
                await dispatcher.send(
                    instance_id=request.instance_id,
                    origin_member_id=writer_member_id,
                    origin_agent_identity=identity,
                    addressee=addressee,
                    intent="inform",
                    content=content,
                    urgency="normal",
                    envelope_type="route_fire",
                    canvas_id=canvas_id,
                )
            except Exception as exc:
                logger.debug("CANVAS_ROUTE_FIRE_FAILED: to=%s %s", addressee, exc)

    async def _handle_dismiss_whisper(
        self, instance_id: str, whisper_id: str, reason: str = "user_dismissed"
    ) -> str:
        """Dismiss a whisper — update suppression to prevent re-surfacing."""
        if not self._state:
            return "State store is not available."
        suppressions = await self._state.get_suppressions(
            instance_id, whisper_id=whisper_id
        )
        if suppressions:
            s = suppressions[0]
            s.resolution_state = "dismissed"
            s.resolved_by = reason
            s.resolved_at = datetime.now(timezone.utc).isoformat()
            await self._state.save_suppression(instance_id, s)

            # If this was a behavioral pattern whisper, mark pattern as declined
            if s.foresight_signal.startswith("behavioral_pattern:"):
                try:
                    _bp_id = s.foresight_signal.split(":", 1)[1]
                    data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
                    from kernos.kernel.behavioral_patterns import load_patterns, save_patterns
                    patterns = load_patterns(data_dir, instance_id)
                    for p in patterns:
                        if p.pattern_id == _bp_id:
                            p.proposal_declined = True
                            p.proposal_surfaced = False  # Allow re-proposal after reset
                            p.threshold_met = False
                            save_patterns(data_dir, instance_id, patterns)
                            logger.info("BEHAVIORAL_RESOLVED: fingerprint=%s action=declined", p.fingerprint[:40])
                            break
                except Exception as exc:
                    logger.debug("BEHAVIORAL_PATTERN: decline handling failed: %s", exc)

            return f"Dismissed whisper {whisper_id}. Won't bring this up again."
        return f"Whisper {whisper_id} not found in suppression registry."

    async def _handle_remember_details(
        self, instance_id: str, space_id: str, input_data: dict,
    ) -> str:
        """Retrieve conversation text from a specific archived log file.

        Read-only. No state mutation.
        """
        source_ref = input_data.get("source_ref", "")
        query = input_data.get("query", "")

        if not source_ref:
            return (
                "No source reference provided. Call remember() first to find "
                "a Ledger entry with a source log reference (e.g., 'source: log_003'), "
                "then pass that reference here."
            )

        log_number = self._parse_log_ref(source_ref)
        if log_number is None:
            return (
                f"Could not parse '{source_ref}' as a log reference. "
                f"Expected format: 'log_003' or '3'. "
                f"Call remember() first to find the correct source reference."
            )

        # Read via HandlerProtocol.read_log_text
        if not self._handler or not hasattr(self._handler, "read_log_text"):
            return "Conversation logger is not available."

        log_text = await self._handler.read_log_text(
            instance_id, space_id, log_number,
        )

        if log_text is None:
            logger.info("DEEP_RECALL: space=%s log=%03d not_found", space_id, log_number)
            return f"Log file log_{log_number:03d} not found for this space."

        # If a query is provided, extract relevant section
        if query:
            relevant = self._extract_relevant_section(log_text, query)
            if relevant:
                logger.info(
                    "DEEP_RECALL: space=%s log=%03d query=%s chars=%d",
                    space_id, log_number, query[:50], len(relevant),
                )
                return (
                    f"From log_{log_number:03d} — section matching '{query}':"
                    f"\n\n{relevant}"
                )
            else:
                return (
                    f"Log_{log_number:03d} exists but no section matches '{query}'. "
                    f"Try a different search term, or omit the query to see the full log."
                )

        # No query — return bounded log content
        max_chars = 8000  # ~2000 tokens
        if len(log_text) <= max_chars:
            logger.info(
                "DEEP_RECALL: space=%s log=%03d full chars=%d",
                space_id, log_number, len(log_text),
            )
            return f"From log_{log_number:03d} (full log):\n\n{log_text}"

        # Log too large — head + tail with gap notice
        chunk_size = max_chars // 2
        head = log_text[:chunk_size]
        tail = log_text[-chunk_size:]
        logger.info(
            "DEEP_RECALL: space=%s log=%03d bounded chars=%d (total=%d)",
            space_id, log_number, max_chars, len(log_text),
        )
        return (
            f"From log_{log_number:03d} ({len(log_text)} chars total, "
            f"showing first and last sections):\n\n"
            f"--- START ---\n{head}\n\n"
            f"--- GAP ({len(log_text) - max_chars} chars omitted) ---\n\n"
            f"--- END ---\n{tail}\n\n"
            f"To see a specific section, retry with a query keyword."
        )

    @staticmethod
    def _parse_log_ref(ref: str) -> int | None:
        """Parse a log reference string into a log number.

        Accepts: "log_003", "log_3", "3", "log003"
        """
        import re as _re
        match = _re.match(r'log_?(\d+)', ref.strip().lower())
        if match:
            return int(match.group(1))
        try:
            return int(ref.strip())
        except ValueError:
            return None

    @staticmethod
    def _extract_relevant_section(
        log_text: str, query: str, context_lines: int = 10,
    ) -> str:
        """Extract lines from a log relevant to a query.

        Simple keyword matching with surrounding context lines.
        """
        lines = log_text.split("\n")
        query_lower = query.lower()

        matching_indices = [
            i for i, line in enumerate(lines) if query_lower in line.lower()
        ]

        if not matching_indices:
            return ""

        included: set[int] = set()
        for idx in matching_indices:
            start = max(0, idx - context_lines)
            end = min(len(lines), idx + context_lines + 1)
            for i in range(start, end):
                included.add(i)

        return "\n".join(lines[i] for i in sorted(included))

    async def _run_via_turn_runner_provider(
        self, request: ReasoningRequest
    ) -> ReasoningResult:
        """Route a request through the per-turn TurnRunner factory (IWL C6).

        The provider produces (TurnRunner, ProductionResponseDelivery)
        bound to (request, event_emitter) so:
          - emit_request_event() fires ONCE at turn start.
          - ProductionResponseDelivery.__call__ emits the SINGLE
            reasoning.response at turn end.
          - Per-turn AggregatedTelemetry wraps the hooks' chain
            callers so token aggregation accumulates correctly.

        This replaces the static turn_runner path for production
        wiring. Tests that don't exercise the per-turn binding
        continue to use the static turn_runner seam.
        """
        from kernos.kernel.turn_runner import TurnRunnerInputs

        async def _event_emitter(payload: dict) -> None:
            """Bridge synthetic reasoning.* events into the existing
            event stream so legacy consumers see them with the right
            shape."""
            if self._events is None:
                return
            try:
                from kernos.kernel.events import emit_event
                from kernos.kernel.event_types import EventType
                # The synthetic emission uses the existing event
                # surface; payload['type'] determines the EventType.
                await emit_event(
                    self._events,
                    EventType.REASONING_RESPONSE
                    if payload.get("type") == "reasoning.response"
                    else EventType.REASONING_REQUEST,
                    payload.get("instance_id", ""),
                    "reasoning_service.turn_runner",
                    payload=payload,
                )
            except Exception:
                logger.warning(
                    "TURN_RUNNER_EVENT_EMIT_FAILED type=%s",
                    payload.get("type", "?"),
                    exc_info=True,
                )

        provider = self._turn_runner_provider
        turn_runner, delivery = provider(request, _event_emitter)

        # Synthetic reasoning.request emitted ONCE at turn start.
        await delivery.emit_request_event()

        # INTEGRATION-CAPABILITY-FIRST-V1 (Batch 1, piece A): build
        # SurfacedTool tuple from cognitive_context.tool_surface so
        # IntegrationInputs.surfaced_tools is populated. Without this,
        # the integration LLM sees zero tools for the turn and defaults
        # to render-only ActionKinds (RESPOND_ONLY / CONSTRAINED_RESPONSE
        # / PROPOSE_TOOL) — agent cannot select tool execution even
        # when the user explicitly asks. See
        # specs/INTEGRATION-CAPABILITY-FIRST-V1.md §"Batch 1, piece A".
        _cognitive_context = getattr(request, "cognitive_context", None)
        _surfaced_tools: tuple = ()
        if _cognitive_context is not None:
            _tool_surface = getattr(_cognitive_context, "tool_surface", None)
            if _tool_surface is not None:
                try:
                    from kernos.kernel.integration.surfaced_tools import (
                        build_surfaced_tools,
                    )
                    _tool_dicts = _tool_surface.all_tools()
                    _surfaced_tools = build_surfaced_tools(
                        _tool_dicts,
                        gate=self._get_gate(),
                        active_space=None,
                        rationale="cognitive_context.tool_surface",
                    )
                except Exception as exc:
                    logger.warning(
                        "SURFACED_TOOLS_BUILD_FAILED: err=%s; "
                        "integration will see empty surface (degraded)",
                        exc,
                    )
        inputs = TurnRunnerInputs.from_api_messages(
            instance_id=request.instance_id,
            member_id=request.member_id,
            space_id=request.active_space_id,
            turn_id=request.conversation_id,
            user_message=request.input_text,
            api_messages=tuple(request.messages),
            active_space_ids=(
                (request.active_space_id,) if request.active_space_id else ()
            ),
            surfaced_tools=_surfaced_tools,
            # COGNITIVE-CONTEXT-V1 C3a: thread the typed packet from
            # ReasoningRequest into TurnRunnerInputs so the runner
            # can pass it through to IntegrationInputs and onwards
            # to Briefing.cognitive_context for the renderer.
            cognitive_context=_cognitive_context,
        )
        outcome = await turn_runner.run_turn(inputs)
        # When the response_delivery hook is wired correctly, the
        # outcome IS the ReasoningResult (TurnRunner.deliver invoked
        # delivery.__call__ which produced the result + emitted the
        # synthetic reasoning.response).
        if isinstance(outcome, ReasoningResult):
            return outcome
        # Defensive — provider returned a delivery hook that didn't
        # produce a ReasoningResult. Surface clearly rather than
        # producing degenerate output.
        raise RuntimeError(
            "turn_runner_provider returned a delivery that did not "
            "produce a ReasoningResult. Check the provider's "
            "response_delivery wiring."
        )

    async def reason(self, request: ReasoningRequest) -> ReasoningResult:
        """Run a full reasoning turn through the decoupled turn runner."""  # noqa: D401
        # 2026-05-23 dump_context accounting fix: cache the payload
        # so tool-dispatched dump_context can render accurate token
        # counts. See get_last_reasoning_payload().
        if request.instance_id:
            self._last_reasoning_payload[request.instance_id] = {
                "system_prompt": request.system_prompt or "",
                "messages": list(request.messages or []),
                "tools": list(request.tools or []),
                "system_prompt_static": request.system_prompt_static or "",
                "system_prompt_dynamic": request.system_prompt_dynamic or "",
            }
        return await self._reason_impl(request)

    async def _reason_impl(self, request: ReasoningRequest) -> ReasoningResult:
        """Run a full reasoning turn through the decoupled turn runner.

        The signature and return shape are unchanged from the pre-CCV1
        legacy loop, so existing callers (the message handler, scheduler,
        engine) need no modification.

        CCV1 C7 strike (2026-05-03): the legacy reasoning loop has been
        removed. ``turn_runner_provider`` is required (wired via
        ``kernos.kernel.turn_runner_provider`` per
        REASONING-SERVICE-CONSTRUCTION-PARITY-V1). Constructing
        ReasoningService without it raises
        :class:`TurnRunnerNotWired` on first reason() call.
        """
        from kernos.kernel.turn_runner import TurnRunnerNotWired
        if self._turn_runner_provider is None:
            raise TurnRunnerNotWired(
                "ReasoningService.reason() requires turn_runner_provider "
                "to be wired. Use the shared helper at "
                "kernos.kernel.turn_runner_provider per "
                "REASONING-SERVICE-CONSTRUCTION-PARITY-V1; see the "
                "ReasoningService class docstring CONSTRUCTION CONTRACT "
                "block."
            )
        return await self._run_via_turn_runner_provider(request)
