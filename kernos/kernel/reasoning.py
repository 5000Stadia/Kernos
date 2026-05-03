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
    REQUEST_TOOL, READ_DOC_TOOL, REMEMBER_DETAILS_TOOL,
    MANAGE_CAPABILITIES_TOOL, READ_SOURCE_TOOL,
    READ_SOUL_TOOL, UPDATE_SOUL_TOOL, SOUL_UPDATABLE_FIELDS,
    read_doc as _read_doc, read_source as _read_source,
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
    # Consumed by _call_chain via resolve_effective_chain.
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
        turn_runner: Any = None,  # TurnRunner | None — Any avoids circular import
        trace_sink: list[dict] | None = None,  # IWL C2: shared tool-trace seam
        turn_runner_provider: Any = None,  # IWL C6: per-turn factory; takes (request, event_emitter) -> (TurnRunner, ProductionResponseDelivery)
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
        # Hybrid token counting: real input_tokens from last principal reasoning call per-instance
        self._last_real_input_tokens: dict[str, int] = {}  # instance_id → tokens
        # Pre-flight chain-skip support — lazily-loaded model registry
        # cards keyed by model name. Loaded once per ReasoningService
        # lifetime; refreshes happen out-of-process via `python -m
        # kernos.models`. Empty dict marks "tried and failed/empty".
        self._catalog_cards: dict[str, Any] | None = None
        # Track unknown-model warnings so we log each at most once per
        # ReasoningService process to avoid log spam.
        self._unknown_model_warned: set[str] = set()
        # PDI C2: optional TurnRunner for the decoupled-cognition path.
        # When None and the feature flag is set, .reason() raises a
        # clear error rather than producing a half-formed turn. Default
        # path (flag off) is the legacy reasoning loop, unchanged.
        self._turn_runner = turn_runner
        # IWL C6: per-turn factory. When set, takes precedence over
        # static turn_runner — produces a fresh TurnRunner +
        # ProductionResponseDelivery per call so the synthetic
        # reasoning.* events fire correctly and per-turn telemetry
        # binding works. Production wiring uses this; tests may use
        # either turn_runner (static) or turn_runner_provider
        # (per-turn) depending on which seam they exercise.
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
            self._gate = DispatchGate(
                reasoning_service=self,
                registry=getattr(self, '_registry', None),
                state=getattr(self, '_state', None),
                events=getattr(self, '_events', None),
                mcp=getattr(self, '_mcp', None),
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

    async def _call_chain(
        self,
        chain_name: str,
        system: str | list[dict],
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        request_model: str | None = None,
        request: "ReasoningRequest | None" = None,
    ) -> ProviderResponse:
        """Try each entry in the named chain until one succeeds.

        For the "primary" chain, the first entry uses request_model (from
        ReasoningRequest) rather than the chain's configured model — this
        preserves the current handler → reasoning model selection.

        MODEL-AND-STATUS-V1: when ``request.model_override`` is set, the
        shared resolver in ``model_routing`` rewrites the chain by
        switching to a different chain (when ``chain_name`` is set on
        the override) and/or prepending a head entry with later
        duplicates de-duped. The override is "preferred first attempt,"
        not a hard pin — natural fallback applies on override-head
        failure (Codex pre-spec refinement #1). Stale overrides
        (chain or entry no longer in current chains) are skipped at
        resolve time so the dispatch sees a clean entry list.

        Catches only ReasoningProviderError | ReasoningConnectionError to
        match existing behavior and avoid masking programming errors.
        """
        from kernos.kernel.model_routing import resolve_effective_chain

        eff = resolve_effective_chain(
            chains=self._chains,
            requested_chain=chain_name,
            override=(request.model_override if request is not None else None),
        )
        # Use the resolver's chain choice. When a stale chain_name was
        # passed via the override, eff.chain_name reflects the
        # fallback so logging stays accurate.
        chain_name = eff.chain_name
        entries = list(eff.entries)
        if not entries:
            entries = list(
                self._chains.get(chain_name, self._chains.get("primary", []))
            )

        # Codex post-impl fold: when the override carries a (provider,
        # model) head spec, the user has explicitly chosen that exact
        # entry — the request_model substitution below MUST NOT clobber
        # entry 0's model with the principal's default main_model. This
        # flag is consulted in the loop. Chain-only switches do NOT set
        # the flag because the chain's natural head is what entry 0
        # carries and request_model substitution remains the right
        # behaviour.
        head_was_overridden = bool(
            request is not None
            and request.model_override is not None
            and request.model_override.get("override_provider")
            and request.model_override.get("override_model")
            and not eff.stale_head_spec
        )
        last_exc: Exception | None = None
        # LLM-SETUP-AND-FALLBACK: accumulate per-entry failure detail so the
        # LLMChainExhausted exception can carry it for the pre-rendered
        # failure message and diagnostic tools.
        attempts: list[tuple[str, str, str]] = []

        # Pre-flight payload estimate — the chain dispatcher uses this
        # to skip entries whose effective context window cannot fit the
        # request, before any model is called. Estimator is biased high;
        # combined with a per-call safety margin, the decisions tolerate
        # the heuristic's inaccuracy without false-positively passing.
        est_tokens = estimate_tokens(system=system, messages=messages, tools=tools)
        catalog = self._get_catalog_cards()
        safety_margin = self._context_safety_margin()
        called_count = 0
        skipped_count = 0
        largest_ceiling: int | None = None

        for i, entry in enumerate(entries):
            # When the user has set an explicit head override, entry 0
            # IS the override and its model must not be replaced by
            # request_model (Codex post-impl fold).
            if i == 0 and head_was_overridden:
                model = entry.model
            else:
                model = request_model if (i == 0 and request_model) else entry.model
            pname = getattr(entry.provider, "provider_name", "unknown")

            # Pre-flight context-window skip. Tolerant on unknown models:
            # if the catalog has no card, fall through and route normally
            # so existing behaviour is preserved for anything not in the
            # registry. The first-time-unknown warning logs once per
            # process at info level.
            card = catalog.get(model) if catalog else None
            if card is not None and card.effective_max_input_tokens:
                ceiling = card.effective_max_input_tokens
                if largest_ceiling is None or ceiling > largest_ceiling:
                    largest_ceiling = ceiling
                threshold = int(ceiling * (1.0 - safety_margin))
                if est_tokens > threshold:
                    skipped_count += 1
                    skip_reason = (
                        f"skipped: payload {est_tokens} tokens exceeds "
                        f"threshold {threshold} (ceiling {ceiling}, "
                        f"margin {safety_margin:.0%})"
                    )
                    if request:
                        self._trace(
                            request, "info", "reasoning", "CHAIN_SKIP",
                            f"chain={chain_name} entry={pname} model={model} "
                            f"estimated_tokens={est_tokens} threshold={threshold}",
                        )
                    logger.info(
                        "CHAIN[%s]: skip %s/%s — %s",
                        chain_name, pname, model, skip_reason,
                    )
                    attempts.append((pname, model, skip_reason))
                    continue
            elif card is None and model:
                self._warn_unknown_model_once(model)

            # Thread trace to provider for internal event capture
            if hasattr(entry.provider, "_trace"):
                entry.provider._trace = getattr(request, "trace", None) if request else None

            try:
                called_count += 1
                response = await entry.provider.complete(
                    model=model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    conversation_id=request.conversation_id if request else "",
                )
                if i > 0 and request:
                    # Partial fallback succeeded — silent per the
                    # LLM-SETUP-AND-FALLBACK contract. Log a FALLBACK_USED
                    # diagnostic event only; never surface to the agent or
                    # user, never fire a whisper.
                    self._trace(request, "info", "reasoning", "FALLBACK_USED",
                        f"chain={chain_name} via {pname}/{model} (skipped {i} entries)")
                    logger.info("CHAIN[%s]: success via %s/%s", chain_name, pname, model)
                return response
            except (ReasoningProviderError, ReasoningConnectionError) as exc:
                if request:
                    self._trace(request, "warning", "reasoning", "CHAIN_FALLBACK",
                        f"chain={chain_name} {pname}/{model} failed: {str(exc)[:150]}")
                logger.warning("CHAIN[%s]: %s/%s failed: %s", chain_name, pname, model, exc)
                last_exc = exc
                attempts.append((pname, model, str(exc)))
                continue

        # If every entry was skipped because the payload could not fit,
        # raise the distinct ChainPayloadTooLarge so the handler can
        # surface a clear "trim or compact" message rather than report
        # a transient-error retry chain. The estimated-vs-ceiling
        # numbers are part of the exception so diagnostic surfaces can
        # render them.
        if called_count == 0 and skipped_count > 0:
            if request:
                self._trace(
                    request, "error", "reasoning", "CHAIN_PAYLOAD_TOO_LARGE",
                    f"chain={chain_name} estimated_tokens={est_tokens} "
                    f"largest_ceiling={largest_ceiling} skipped={skipped_count}",
                )
            logger.error(
                "CHAIN[%s]: payload too large for any entry "
                "(estimated=%d, largest_ceiling=%s)",
                chain_name, est_tokens, largest_ceiling,
            )
            raise ChainPayloadTooLarge(
                chain_name=chain_name,
                estimated_tokens=est_tokens,
                largest_ceiling=largest_ceiling,
                attempts=attempts,
            )

        # All entries exhausted — raise the specific chain-exhaustion
        # exception the handler catches to deliver a pre-rendered failure
        # message (instead of an LLM reply) for this turn.
        if request:
            self._trace(request, "error", "reasoning", "CHAIN_EXHAUSTED",
                f"chain={chain_name} all {len(entries)} entries exhausted")
        logger.error("CHAIN[%s]: all %d providers failed", chain_name, len(entries))
        raise LLMChainExhausted(chain_name=chain_name, attempts=attempts)

    def get_loaded_tools(self, space_id: str) -> set[str]:
        """Get the set of MCP tool names currently loaded for a space."""
        return self._loaded_tools.get(space_id, set())

    def load_tool(self, space_id: str, tool_name: str) -> None:
        """Add a tool to the loaded set for a space."""
        if space_id not in self._loaded_tools:
            self._loaded_tools[space_id] = set()
        self._loaded_tools[space_id].add(tool_name)

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

    def clear_loaded_tools(self, space_id: str) -> None:
        """Clear loaded tools for a space (session boundary)."""
        count = len(self._loaded_tools.pop(space_id, set()))
        if count:
            logger.info("TOOL_UNLOAD: space=%s cleared=%d", space_id, count)

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
    _KERNEL_TOOLS = {"remember", "remember_details", "write_file", "read_file", "list_files", "delete_file", "dismiss_whisper", "read_source", "read_doc", "read_soul", "update_soul", "manage_covenants", "manage_capabilities", "manage_channels", "send_to_channel", "manage_schedule", "inspect_state", "request_tool", "execute_code", "manage_workspace", "register_tool", "manage_plan", "read_runtime_trace", "diagnose_issue", "propose_fix", "submit_spec", "manage_members", "send_relational_message", "resolve_relational_message", "set_chain_model", "diagnose_llm_chain", "diagnose_messenger", "canvas_list", "canvas_create", "page_read", "page_write", "page_list", "page_search", "canvas_preference_extract", "canvas_preference_confirm", "consult", "request_space_action"}

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
        "write_file":                  frozenset({"loop", "confirmed"}),
        "read_file":                   frozenset({"loop", "confirmed"}),
        "list_files":                  frozenset({"loop", "confirmed"}),
        "delete_file":                 frozenset({"loop", "confirmed"}),
        "execute_code":                frozenset({"loop", "confirmed"}),
        "consult":                     frozenset({"loop", "confirmed"}),
        "request_space_action":        frozenset({"loop", "confirmed"}),
        "manage_workspace":            frozenset({"loop", "confirmed"}),
        "register_tool":               frozenset({"loop", "confirmed"}),
        "manage_plan":                 frozenset({"loop", "confirmed"}),
        "read_runtime_trace":          frozenset({"loop", "confirmed"}),
        "diagnose_issue":              frozenset({"loop", "confirmed"}),
        "propose_fix":                 frozenset({"loop", "confirmed"}),
        "submit_spec":                 frozenset({"loop", "confirmed"}),
        "manage_members":              frozenset({"loop", "confirmed"}),
        "send_relational_message":     frozenset({"loop", "confirmed"}),
        "resolve_relational_message":  frozenset({"loop", "confirmed"}),
        "remember":                    frozenset({"loop", "confirmed"}),
        "dismiss_whisper":             frozenset({"loop", "confirmed"}),
        "read_source":                 frozenset({"loop", "confirmed"}),
        "read_doc":                    frozenset({"loop", "confirmed"}),
        "read_soul":                   frozenset({"loop", "confirmed"}),
        "update_soul":                 frozenset({"loop", "confirmed"}),
        "manage_covenants":            frozenset({"loop", "confirmed"}),
        "manage_capabilities":         frozenset({"loop", "confirmed"}),
        "manage_channels":             frozenset({"loop", "confirmed"}),
        "send_to_channel":             frozenset({"loop", "confirmed"}),
        "manage_schedule":             frozenset({"loop", "confirmed"}),
        "request_tool":                frozenset({"loop", "confirmed"}),
        # Loop only — read-only or chain-management; never produce
        # confirmable PendingActions.
        "remember_details":            frozenset({"loop"}),
        "inspect_state":               frozenset({"loop"}),
        "set_chain_model":             frozenset({"loop"}),
        "diagnose_llm_chain":          frozenset({"loop"}),
        "diagnose_messenger":          frozenset({"loop"}),
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
                )
                from kernos.kernel.external_agents.errors import (
                    ExternalAgentError as _ExtError,
                )
                try:
                    _svc = await _ext_get_service()
                    _consult_result = await _svc.orchestrator.consult(
                        instance_id=request.instance_id,
                        member_id=getattr(request, "member_id", "")
                                  or request.instance_id,
                        harness=tool_input.get("harness", ""),
                        question=tool_input.get("question", ""),
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
                    _register_msg = await self._workspace.register_tool(
                        request.instance_id, request.active_space_id, _desc_file,
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
            elif tool_name == "read_doc":
                return _read_doc(tool_input.get("path", ""))
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
            elif tool_name in ("canvas_list", "canvas_create", "page_read",
                                "page_write", "page_list", "page_search",
                                "canvas_preference_extract",
                                "canvas_preference_confirm"):
                return await self._handle_canvas_tool(tool_name, tool_input, request)
            else:
                return f"Kernel tool '{tool_name}' not handled."
        else:
            return await self._mcp.call_tool(tool_name, tool_input)

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

    async def _execute_single_tool(
        self,
        block: ContentBlock,
        tool_input: dict,
        request: ReasoningRequest,
        tools: list[dict],
        gate_cache: dict,
        rr_event: Any,
        iterations: int,
        agent_reasoning: str,
    ) -> dict:
        """Execute a single tool call: gate -> execute -> budget -> return tool_result dict.

        Returns a dict with keys: type, tool_use_id, content.
        Side effects: may modify gate_cache, _pending_actions, _conflict_raised_this_turn.
        """
        approval_token_id = tool_input.pop("_approval_token", None)

        logger.info(
            "AGENT_ACTION: tool=%s input=%s",
            block.name, json.dumps(tool_input)[:200],
        )

        # Dispatch Gate: classify and check write tools before execution
        tool_effect = self._get_gate().classify_tool_effect(
            block.name, request.active_space, tool_input)
        if tool_effect in ("soft_write", "hard_write", "unknown"):
            if block.name in gate_cache and gate_cache[block.name].allowed:
                gate_result = gate_cache.pop(block.name)
                logger.info(
                    "GATE_CACHED: tool=%s (approved on stub call)", block.name,
                )
            else:
                logger.info(
                    "GATE_INPUT: tool=%s effect=%s reasoning=%s",
                    block.name, tool_effect, agent_reasoning[:80],
                )
                gate_result = await self._get_gate().evaluate(
                    block.name, tool_input, tool_effect,
                    request.input_text, request.instance_id,
                    request.active_space_id,
                    messages=request.messages,
                    approval_token_id=approval_token_id,
                    agent_reasoning=agent_reasoning,
                    is_reactive=request.is_reactive,
                )

            try:
                await emit_event(
                    self._events,
                    EventType.DISPATCH_GATE,
                    request.instance_id,
                    "dispatch_interceptor",
                    payload={
                        "tool_name": block.name,
                        "effect": tool_effect,
                        "allowed": gate_result.allowed,
                        "reason": gate_result.reason,
                        "method": gate_result.method,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit dispatch.gate: %s", exc)

            # EVENT-STREAM-TO-SQLITE: unified-timeline emission. Coexists
            # with the older emit_event above during the transition.
            try:
                from kernos.kernel import event_stream
                _turn_id = getattr(getattr(request, "trace", None), "turn_id", None)
                await event_stream.emit(
                    request.instance_id, "gate.verdict",
                    {
                        "tool": block.name,
                        "effect": tool_effect,
                        "verdict": gate_result.reason,
                        "allowed": gate_result.allowed,
                        "method": gate_result.method,
                    },
                    member_id=request.member_id or None,
                    space_id=request.active_space_id or None,
                    correlation_id=_turn_id,
                )
            except Exception as exc:
                logger.warning("Failed to emit gate.verdict: %s", exc)

            self._trace(request, "info" if gate_result.allowed else "warning",
                "gate", "GATE",
                f"tool={block.name} effect={tool_effect} allowed={gate_result.allowed} "
                f"reason={gate_result.reason} method={gate_result.method}",
                phase="reason")
            logger.info(
                "GATE: tool=%s effect=%s allowed=%s reason=%s method=%s",
                block.name, tool_effect, gate_result.allowed,
                gate_result.reason, gate_result.method,
            )

            if not gate_result.allowed:
                self._get_gate().issue_approval_token(block.name, tool_input)
                instance_id = request.instance_id
                if instance_id not in self._pending_actions:
                    self._pending_actions[instance_id] = []
                pending_idx = len(self._pending_actions[instance_id])
                self._pending_actions[instance_id].append(PendingAction(
                    tool_name=block.name,
                    tool_input=dict(tool_input),
                    proposed_action=gate_result.proposed_action,
                    conflicting_rule=gate_result.conflicting_rule,
                    gate_reason=gate_result.reason,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
                ))
                self._conflict_raised_this_turn = True
                if gate_result.reason == "covenant_conflict":
                    system_msg = (
                        f"[SYSTEM] Action blocked — conflict with standing rule. "
                        f"Proposed: {gate_result.proposed_action}. "
                        f"Conflicting rule: {gate_result.conflicting_rule}. "
                        f"Pending action index: {pending_idx}. "
                        f"Ask the user to confirm. If they confirm, include "
                        f"[CONFIRM:{pending_idx}] in your response. "
                        f"Also offer three options: "
                        f"1. Respect the rule (don't do it). "
                        f"2. Override this time (confirm the action). "
                        f"3. Update the rule permanently."
                    )
                elif gate_result.reason == "clarify":
                    system_msg = (
                        f"[SYSTEM] Action paused — the request is ambiguous. "
                        f"Proposed: {gate_result.proposed_action}. "
                        f"Pending action index: {pending_idx}. "
                        f"Ask the user to clarify what they meant. "
                        f"Once clear, include [CONFIRM:{pending_idx}] in your response."
                    )
                else:
                    system_msg = (
                        f"[SYSTEM] Action paused — confirming intent. "
                        f"Proposed: {gate_result.proposed_action}. "
                        f"Pending action index: {pending_idx}. "
                        f"Ask the user if they want to proceed. If they confirm, "
                        f"include [CONFIRM:{pending_idx}] in your response."
                    )
                return {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": system_msg,
                }

        # Emit tool.called
        try:
            await emit_event(
                self._events,
                EventType.TOOL_CALLED,
                request.instance_id,
                "reasoning_service",
                payload={
                    "tool_name": block.name,
                    "tool_input": tool_input,
                    "conversation_id": request.conversation_id,
                    "reasoning_event_id": rr_event.id if rr_event else None,
                },
            )
        except Exception as exc:
            logger.warning("Failed to emit tool.called: %s", exc)

        # EVENT-STREAM-TO-SQLITE: unified-timeline emission.
        try:
            from kernos.kernel import event_stream
            _turn_id = getattr(getattr(request, "trace", None), "turn_id", None)
            await event_stream.emit(
                request.instance_id, "tool.called",
                {
                    "name": block.name,
                    "args_keys": sorted((tool_input or {}).keys()),
                },
                member_id=request.member_id or None,
                space_id=request.active_space_id or None,
                correlation_id=_turn_id,
            )
        except Exception as exc:
            logger.debug("Failed to emit tool.called (unified): %s", exc)

        await self._audit.log(
            request.instance_id,
            {
                "type": "tool_call",
                "timestamp": utc_now(),
                "instance_id": request.instance_id,
                "conversation_id": request.conversation_id,
                "tool_name": block.name,
                "tool_input": tool_input,
            },
        )

        t_tool = time.monotonic()
        _is_mcp_tool = False
        result = ""

        if block.name in self._KERNEL_TOOLS:
            logger.info(
                "KERNEL_TOOL name=%s space=%s",
                block.name, request.active_space_id,
            )
            tool_args = tool_input
            if block.name == "remember":
                if self._retrieval:
                    try:
                        _idb = (
                            getattr(self._handler, '_instance_db', None)
                            if hasattr(self, '_handler') and self._handler
                            else None
                        )
                        result = await self._retrieval.search(
                            request.instance_id,
                            tool_args.get("query", ""),
                            request.active_space_id,
                            requesting_member_id=getattr(request, "member_id", ""),
                            instance_db=_idb,
                        )
                    except Exception as exc:
                        logger.warning("Kernel tool 'remember' failed: %s", exc)
                        result = "Memory search failed — try asking in a different way."
                else:
                    result = "Memory search is not available right now."
            elif block.name == "remember_details":
                result = await self._handle_remember_details(
                    request.instance_id,
                    request.active_space_id,
                    tool_args,
                )
            elif block.name == "write_file":
                if self._files:
                    try:
                        result = await self._files.write_file(
                            request.instance_id,
                            request.active_space_id,
                            tool_args.get("name", ""),
                            tool_args.get("content", ""),
                            tool_args.get("description", ""),
                            target_space_id=tool_args.get("target_space_id"),
                        )
                    except Exception as exc:
                        logger.warning("Kernel tool 'write_file' failed: %s", exc)
                        result = "File write failed — try again."
                else:
                    result = "File system is not available right now."
            elif block.name == "read_file":
                if self._files:
                    try:
                        result = await self._files.read_file(
                            request.instance_id,
                            request.active_space_id,
                            tool_args.get("name", ""),
                        )
                    except Exception as exc:
                        logger.warning("Kernel tool 'read_file' failed: %s", exc)
                        result = "File read failed — try again."
                else:
                    result = "File system is not available right now."
            elif block.name == "list_files":
                if self._files:
                    try:
                        result = await self._files.list_files(
                            request.instance_id,
                            request.active_space_id,
                        )
                    except Exception as exc:
                        logger.warning("Kernel tool 'list_files' failed: %s", exc)
                        result = "File listing failed — try again."
                else:
                    result = "File system is not available right now."
            elif block.name == "delete_file":
                if self._files:
                    try:
                        result = await self._files.delete_file(
                            request.instance_id,
                            request.active_space_id,
                            tool_args.get("name", ""),
                        )
                    except Exception as exc:
                        logger.warning("Kernel tool 'delete_file' failed: %s", exc)
                        result = "File deletion failed — try again."
                else:
                    result = "File system is not available right now."
            elif block.name == "execute_code":
                try:
                    import json as _json
                    from kernos.kernel.code_exec import execute_code as _exec_code
                    _data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
                    _exec_result = await _exec_code(
                        instance_id=request.instance_id,
                        space_id=request.active_space_id,
                        code=tool_args.get("code", ""),
                        timeout_seconds=tool_args.get("timeout_seconds", 30),
                        write_file_name=tool_args.get("write_file"),
                        data_dir=_data_dir,
                        backend=tool_args.get("backend"),
                    )
                    result = _json.dumps(_exec_result)
                except Exception as exc:
                    logger.warning("Kernel tool 'execute_code' failed: %s", exc)
                    result = f"Code execution failed: {exc}"
            elif block.name == "consult":
                try:
                    import json as _json
                    from kernos.kernel.external_agents.tool import (
                        get_service as _ext_get_service,
                    )
                    from kernos.kernel.external_agents.errors import (
                        ExternalAgentError as _ExtError,
                    )
                    _svc = await _ext_get_service()
                    _cresult = await _svc.orchestrator.consult(
                        instance_id=request.instance_id,
                        member_id=getattr(request, "member_id", "")
                                  or request.instance_id,
                        harness=tool_args.get("harness", ""),
                        question=tool_args.get("question", ""),
                        context=tool_args.get("context", ""),
                        session_id_raw=tool_args.get("session_id", ""),
                        workspace_dir=tool_args.get("workspace_dir") or None,
                        timeout_seconds=tool_args.get("timeout_seconds"),
                    )
                    result = _json.dumps({
                        "response": _cresult.response,
                        "harness": _cresult.harness,
                        "session_id": _cresult.session_id,
                        "truncated": _cresult.truncated,
                        "metadata": _cresult.metadata,
                    })
                except _ExtError as exc:
                    logger.info(
                        "consult tool returned typed error: %s: %s",
                        type(exc).__name__, exc,
                    )
                    result = _json.dumps({
                        "error": type(exc).__name__,
                        "message": str(exc),
                    })
                except Exception as exc:
                    logger.warning("Kernel tool 'consult' failed: %s", exc)
                    result = f"Consult failed: {exc}"
            elif block.name == "request_space_action":
                try:
                    result = await self._dispatch_cross_space_request(
                        tool_args, request,
                    )
                except Exception as exc:
                    logger.warning(
                        "Kernel tool 'request_space_action' failed: %s", exc,
                    )
                    result = f"Cross-space request failed: {exc}"
            elif block.name == "manage_workspace":
                if hasattr(self, '_workspace') and self._workspace:
                    try:
                        action = tool_args.get("action", "list")
                        if action == "list":
                            result = await self._workspace.list_artifacts(request.instance_id, request.active_space_id)
                        elif action == "add":
                            msg, _ = await self._workspace.add_artifact(request.instance_id, request.active_space_id, tool_args.get("artifact", {}))
                            result = msg
                        elif action == "update":
                            result = await self._workspace.update_artifact(request.instance_id, request.active_space_id, tool_args.get("artifact_id", ""), tool_args.get("artifact", {}))
                        elif action == "archive":
                            result = await self._workspace.archive_artifact(request.instance_id, request.active_space_id, tool_args.get("artifact_id", ""))
                        else:
                            result = f"Unknown action: {action}"
                    except Exception as exc:
                        logger.warning("Kernel tool 'manage_workspace' failed: %s", exc)
                        result = f"Workspace operation failed: {exc}"
                else:
                    result = "Workspace manager is not available."
            elif block.name == "register_tool":
                if hasattr(self, '_workspace') and self._workspace:
                    try:
                        _desc_file = tool_args.get("descriptor_file", "") or tool_args
                        result = await self._workspace.register_tool(request.instance_id, request.active_space_id, _desc_file)
                    except Exception as exc:
                        logger.warning("Kernel tool 'register_tool' failed: %s", exc)
                        result = f"Registration failed: {exc}"
                else:
                    result = "Workspace manager is not available."
            elif block.name == "manage_plan":
                if hasattr(self, '_handler') and self._handler:
                    try:
                        result = await self._handler._handle_manage_plan(
                            request.instance_id, request.active_space_id, tool_args)
                    except Exception as exc:
                        logger.warning("Kernel tool 'manage_plan' failed: %s", exc)
                        result = f"Plan operation failed: {exc}"
                else:
                    result = "Self-directed execution is not available."
            elif block.name == "read_runtime_trace":
                if hasattr(self, '_handler') and self._handler:
                    _turns = tool_args.get("turns", 10)
                    _filter = tool_args.get("filter", None)
                    _turn_id = tool_args.get("turn_id", None)
                    events = await self._handler._runtime_trace.read(
                        request.instance_id, turns=_turns,
                        filter_level=_filter, turn_id=_turn_id)
                    if not events:
                        result = "No trace events found."
                    else:
                        lines = []
                        for e in events:
                            lines.append(
                                f"[{e.get('timestamp', '?')[:19]}] {e.get('level', '?').upper()} "
                                f"{e.get('source', '?')}:{e.get('event', '?')} — {e.get('detail', '')[:200]}"
                            )
                        result = f"Runtime trace ({len(events)} events):\n" + "\n".join(lines)
                else:
                    result = "Runtime trace is not available."
            elif block.name in ("diagnose_issue", "propose_fix", "submit_spec"):
                from kernos.kernel.diagnostics import handle_diagnose_issue, handle_propose_fix, handle_submit_spec
                _rt = getattr(self._handler, '_runtime_trace', None) if self._handler else None
                try:
                    if block.name == "diagnose_issue":
                        result = await handle_diagnose_issue(
                            request.instance_id, request.active_space_id, tool_args, _rt, self)
                    elif block.name == "propose_fix":
                        result = await handle_propose_fix(request.instance_id, tool_args, _rt)
                    else:
                        result = await handle_submit_spec(request.instance_id, tool_args, self._handler)
                except Exception as exc:
                    logger.warning("Kernel tool '%s' failed: %s", block.name, exc)
                    result = f"Diagnostic tool failed: {exc}"
            elif block.name == "manage_members":
                if hasattr(self, '_handler') and self._handler:
                    try:
                        result = await self._handler._handle_manage_members(
                            request.instance_id, tool_args, requesting_member_id=request.member_id)
                    except Exception as exc:
                        logger.warning("Kernel tool 'manage_members' failed: %s", exc)
                        result = f"Member management failed: {exc}"
                else:
                    result = "Member management is not available."
            elif block.name == "send_relational_message":
                if hasattr(self, '_handler') and self._handler:
                    try:
                        result = await self._handler._handle_send_relational_message(
                            request.instance_id, tool_args,
                            origin_member_id=request.member_id,
                        )
                    except Exception as exc:
                        logger.warning("Kernel tool 'send_relational_message' failed: %s", exc)
                        result = f"Relational send failed: {exc}"
                else:
                    result = "Relational messaging is not available."
            elif block.name == "resolve_relational_message":
                if hasattr(self, '_handler') and self._handler:
                    try:
                        result = await self._handler._handle_resolve_relational_message(
                            request.instance_id, tool_args,
                            requesting_member_id=request.member_id,
                        )
                    except Exception as exc:
                        logger.warning("Kernel tool 'resolve_relational_message' failed: %s", exc)
                        result = f"Relational resolve failed: {exc}"
                else:
                    result = "Relational messaging is not available."
            elif block.name == "dismiss_whisper":
                try:
                    result = await self._handle_dismiss_whisper(
                        request.instance_id,
                        tool_args.get("whisper_id", ""),
                        tool_args.get("reason", "user_dismissed"),
                    )
                except Exception as exc:
                    logger.warning("Kernel tool 'dismiss_whisper' failed: %s", exc)
                    result = "Failed to dismiss whisper — try again."
            elif block.name == "read_source":
                result = _read_source(
                    tool_args.get("path", ""),
                    tool_args.get("section", ""),
                )
            elif block.name == "read_doc":
                result = _read_doc(tool_args.get("path", ""))
            elif block.name == "read_soul":
                if self._state:
                    soul = await self._state.get_soul(request.instance_id)
                    if soul:
                        from dataclasses import asdict
                        result = json.dumps(asdict(soul), indent=2)
                    else:
                        result = "No soul found for this instance."
                else:
                    result = "State store is not available."
            elif block.name == "update_soul":
                if self._state:
                    field_name = tool_args.get("field", "")
                    value = tool_args.get("value", "")
                    if field_name not in _SOUL_UPDATABLE_FIELDS:
                        result = (
                            f"Cannot update '{field_name}'. Only these fields can be updated: "
                            f"{', '.join(sorted(_SOUL_UPDATABLE_FIELDS))}."
                        )
                    else:
                        soul = await self._state.get_soul(request.instance_id)
                        if not soul:
                            result = "No soul found for this instance."
                        else:
                            setattr(soul, field_name, value)
                            await self._state.save_soul(soul, source="update_soul", trigger=f"{field_name}={value}")
                            result = f"Updated {field_name} to: {value}"
                else:
                    result = "State store is not available."
            elif block.name == "manage_covenants":
                try:
                    from kernos.kernel.covenant_manager import handle_manage_covenants
                    cov_action = tool_args.get("action", "list")
                    result = await handle_manage_covenants(
                        self._state,
                        request.instance_id,
                        action=cov_action,
                        rule_id=tool_args.get("rule_id", ""),
                        new_description=tool_args.get("new_description", ""),
                        show_all=tool_args.get("show_all", False),
                    )
                    if cov_action == "update" and "Updated" in result:
                        from kernos.kernel.covenant_manager import validate_covenant_set
                        id_match = re.search(r"new ID: (rule_\w+)", result)
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
                except Exception as exc:
                    logger.warning("Kernel tool 'manage_covenants' failed: %s", exc)
                    result = "Failed to manage covenants — try again."
            elif block.name == "manage_capabilities":
                try:
                    result = await self._handle_manage_capabilities(
                        request.instance_id,
                        tool_args.get("action", "list"),
                        tool_args.get("capability", ""),
                    )
                except Exception as exc:
                    logger.warning("Kernel tool 'manage_capabilities' failed: %s", exc)
                    result = "Failed to manage tools — try again."
            elif block.name == "manage_channels":
                from kernos.kernel.channels import handle_manage_channels
                if self._channel_registry:
                    result = handle_manage_channels(
                        self._channel_registry,
                        tool_args.get("action", "list"),
                        tool_args.get("channel", ""),
                    )
                else:
                    result = "Channel registry is not available."
            elif block.name == "send_to_channel":
                from kernos.kernel.channels import resolve_channel_alias
                _ch_input = tool_args.get("channel", "")
                _ch_msg = tool_args.get("message", "")
                if not _ch_input or not _ch_msg:
                    result = "Error: both 'channel' and 'message' are required."
                elif not self._channel_registry:
                    result = "Channel registry is not available."
                else:
                    _resolved = resolve_channel_alias(_ch_input)
                    _ch_info = self._channel_registry.get(_resolved)
                    if not _ch_info:
                        _avail = [c.name for c in self._channel_registry.get_connected()]
                        result = (
                            f"Channel '{_resolved}' (from '{_ch_input}') is not registered. "
                            f"Available channels: {', '.join(_avail) or 'none'}"
                        )
                    elif _ch_info.status != "connected":
                        result = f"Channel '{_resolved}' exists but is not connected (status: {_ch_info.status})."
                    elif not _ch_info.can_send_outbound:
                        result = f"Channel '{_resolved}' is connected but cannot send outbound messages."
                    elif not self._handler:
                        result = "Handler not available for outbound delivery."
                    else:
                        try:
                            from kernos.kernel.scheduler import resolve_owner_member_id as _resolve_mid
                            _member_id = _resolve_mid(request.instance_id)
                            await self._handler.send_outbound(
                                request.instance_id, _member_id, _resolved, _ch_msg,
                            )
                            logger.info(
                                "CROSS_CHANNEL_SEND: channel=%s resolved_from=%s len=%d",
                                _resolved, _ch_input, len(_ch_msg),
                            )
                            result = f"Message sent to {_ch_info.display_name}."
                        except Exception as exc:
                            result = f"Failed to send to {_resolved}: {exc}"
            elif block.name == "manage_schedule":
                from kernos.kernel.scheduler import handle_manage_schedule
                if self._trigger_store:
                    result = await handle_manage_schedule(
                        self._trigger_store,
                        request.instance_id,
                        member_id=request.active_space_id,
                        space_id=request.active_space_id,
                        action=tool_args.get("action", "list"),
                        trigger_id=tool_args.get("trigger_id", ""),
                        description=tool_args.get("description", ""),
                        reasoning_service=self,
                        conversation_id=request.conversation_id,
                        user_timezone=request.user_timezone,
                    )
                else:
                    result = "Scheduler is not available."
            elif block.name == "inspect_state":
                try:
                    from kernos.kernel.introspection import build_user_truth_view
                    result = await build_user_truth_view(
                        request.instance_id,
                        self._state,
                        self._trigger_store,
                        self._registry,
                    )
                except Exception as exc:
                    logger.warning("Kernel tool 'inspect_state' failed: %s", exc)
                    result = "State inspection failed — try again."
            elif block.name == "set_chain_model":
                # Admin-only: restrict to system space. The agent's Gate
                # already confines sensitive tools by space; here we apply a
                # lightweight space-check at dispatch time since Gate's admin
                # intent routing is a frozen primitive.
                space_type = ""
                if request.active_space is not None:
                    space_type = getattr(request.active_space, "space_type", "") or ""
                if space_type != "system":
                    result = (
                        "set_chain_model is admin-only and only available "
                        "in the System space."
                    )
                else:
                    from kernos.setup.admin_tools import set_chain_model as _set_chain_model
                    try:
                        admin_res = _set_chain_model(
                            chain=tool_args.get("chain", ""),
                            provider_id=tool_args.get("provider_id", ""),
                            model_id=tool_args.get("model_id", ""),
                        )
                        result = admin_res.get("message") or admin_res.get("error") or "set_chain_model returned no result."
                    except Exception as exc:
                        logger.warning("Kernel tool 'set_chain_model' failed: %s", exc)
                        result = f"set_chain_model failed: {exc}"
            elif block.name == "diagnose_llm_chain":
                space_type = ""
                if request.active_space is not None:
                    space_type = getattr(request.active_space, "space_type", "") or ""
                if space_type != "system":
                    result = (
                        "diagnose_llm_chain is admin-only and only available "
                        "in the System space."
                    )
                else:
                    from kernos.setup.admin_tools import diagnose_llm_chain as _diagnose_llm_chain
                    try:
                        import json as _json
                        admin_res = _diagnose_llm_chain(
                            include_fallback_events=bool(tool_args.get("include_fallback_events", False)),
                            instance_id=request.instance_id,
                        )
                        result = _json.dumps(admin_res, indent=2, default=str)
                    except Exception as exc:
                        logger.warning("Kernel tool 'diagnose_llm_chain' failed: %s", exc)
                        result = f"diagnose_llm_chain failed: {exc}"
            elif block.name == "diagnose_messenger":
                space_type = ""
                if request.active_space is not None:
                    space_type = getattr(request.active_space, "space_type", "") or ""
                if space_type != "system":
                    result = (
                        "diagnose_messenger is admin-only and only available "
                        "in the System space."
                    )
                else:
                    from kernos.cohorts.admin import diagnose_messenger as _diagnose_messenger
                    try:
                        import json as _json
                        idb = getattr(self._handler, "_instance_db", None) if hasattr(self, "_handler") else None
                        admin_res = await _diagnose_messenger(
                            instance_id=request.instance_id,
                            member_a_id=tool_args.get("member_a_id", ""),
                            member_b_id=tool_args.get("member_b_id", ""),
                            state=self._state,
                            instance_db=idb,
                        )
                        result = _json.dumps(admin_res, indent=2, default=str)
                    except Exception as exc:
                        logger.warning("Kernel tool 'diagnose_messenger' failed: %s", exc)
                        result = f"diagnose_messenger failed: {exc}"
            elif block.name == "request_tool":
                result = await self._handle_request_tool(
                    request.instance_id,
                    request.active_space_id,
                    tool_args.get("capability_name", "unknown"),
                    tool_args.get("description", ""),
                )
            else:
                result = f"Kernel tool '{block.name}' not handled."
        else:
            # Lazy tool loading: check if this tool is a stub
            _tool_entry = None
            for _t in tools:
                if _t.get("name") == block.name:
                    _tool_entry = _t
                    break

            if _tool_entry and self._is_stub_schema(_tool_entry) and self._registry:
                full_schema = self._registry.get_tool_schema(block.name)
                if full_schema:
                    for _i, _t in enumerate(tools):
                        if _t.get("name") == block.name:
                            tools[_i] = full_schema
                            break
                    self.load_tool(request.active_space_id, block.name)
                    try:
                        if tool_effect in ("soft_write", "hard_write", "unknown"):
                            gate_cache[block.name] = gate_result  # noqa: F821
                    except NameError:
                        pass
                    logger.info(
                        "TOOL_LOAD: tool=%s space=%s (stub -> full schema, re-running)",
                        block.name, request.active_space_id,
                    )
                    return {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": (
                            f"[SYSTEM] The tool {block.name} is now fully loaded. "
                            "Please retry your call with the correct parameters."
                        ),
                    }

            if not _tool_entry and self._registry:
                schema = self._registry.get_tool_schema(block.name)
                if schema:
                    self.load_tool(request.active_space_id, block.name)
                    tools.append(schema)
                    logger.info(
                        "TOOL_LOAD: tool=%s space=%s (not in list, schema loaded)",
                        block.name, request.active_space_id,
                    )
                elif hasattr(self, '_workspace') and self._workspace and self._workspace._catalog and self._workspace._catalog.has_workspace_tool(block.name):
                    # Workspace tool — dispatch via workspace manager
                    _data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
                    result = await self._workspace.execute_workspace_tool(
                        request.instance_id, block.name, tool_input, _data_dir,
                        member_id=request.member_id,
                    )
                    logger.info("TOOL_DISPATCH: name=%s type=workspace", block.name)
                    tool_duration_ms = int((time.monotonic() - t_tool) * 1000)
                    logger.info("AGENT_RESULT: tool=%s success=%s preview=%s",
                        block.name, "error" not in result.lower()[:50], result[:100])
                    return {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                else:
                    result = f"Tool '{block.name}' is not available."
                    tool_duration_ms = int((time.monotonic() - t_tool) * 1000)
                    logger.info(
                        "AGENT_RESULT: tool=%s success=%s preview=%s",
                        block.name, False, result[:100],
                    )
                    return {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
            # Check workspace tools BEFORE MCP — workspace tools aren't in MCP
            if (hasattr(self, '_workspace') and self._workspace
                    and self._workspace._catalog
                    and self._workspace._catalog.has_workspace_tool(block.name)):
                _data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
                result = await self._workspace.execute_workspace_tool(
                    request.instance_id, block.name, tool_input, _data_dir,
                    member_id=request.member_id,
                )
                logger.info("TOOL_DISPATCH: name=%s type=workspace", block.name)
            else:
                result = await self._mcp.call_tool(block.name, tool_input)
                _is_mcp_tool = True

        tool_duration_ms = int((time.monotonic() - t_tool) * 1000)
        is_error = result.startswith("Tool error:") or result.startswith(
            "Calendar tool error:")

        logger.info(
            "AGENT_RESULT: tool=%s success=%s preview=%s",
            block.name, not is_error, result[:100],
        )
        if is_error:
            self._trace(request, "warning", "reasoning", "TOOL_FAILED",
                f"tool={block.name} result={result[:200]}", phase="reason")

        # Emit tool.result
        try:
            await emit_event(
                self._events,
                EventType.TOOL_RESULT,
                request.instance_id,
                "reasoning_service",
                payload={
                    "tool_name": block.name,
                    "success": not is_error,
                    "result_length": len(result),
                    "duration_ms": tool_duration_ms,
                    "conversation_id": request.conversation_id,
                    "error": result if is_error else None,
                },
            )
        except Exception as exc:
            logger.warning("Failed to emit tool.result: %s", exc)

        await self._audit.log(
            request.instance_id,
            {
                "type": "tool_result",
                "timestamp": utc_now(),
                "instance_id": request.instance_id,
                "conversation_id": request.conversation_id,
                "tool_name": block.name,
                "tool_output": str(result)[:2000],
            },
        )

        # Tool result budgeting: persist oversized MCP results
        injected = result
        if (
            _is_mcp_tool
            and not is_error
            and len(result) > TOOL_RESULT_CHAR_BUDGET
            and self._files
            and request.active_space_id
        ):
            ts = re.sub(r"[^0-9T-]", "", utc_now()[:19])
            slug = uuid.uuid4().hex[:6]
            safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", block.name)
            filename = f"tr_{safe_name}_{ts}_{slug}.txt"
            try:
                write_msg = await self._files.write_file(
                    request.instance_id,
                    request.active_space_id,
                    filename,
                    result,
                    f"Persisted tool result from {block.name} ({len(result)} chars)",
                )
                if write_msg.startswith("Error:"):
                    raise RuntimeError(write_msg)
                preview_chars = TOOL_RESULT_CHAR_BUDGET - 200
                raw_preview = result[:preview_chars]
                cleaned = re.sub(r"\n{3,}", "\n\n", raw_preview).rstrip()
                injected = (
                    f"[Tool result from {block.name} — {len(result)} chars, persisted]\n"
                    f"{cleaned}\n"
                    f"...\n"
                    f"[Full result saved as {filename}. "
                    f"Use read_file to access the full content.]"
                )
                logger.info(
                    "RESULT_BUDGETED: tool=%s original=%d preview=%d path=%s",
                    block.name, len(result), len(injected), filename,
                )
            except Exception as exc:
                logger.warning(
                    "Result budgeting failed, injecting raw: tool=%s err=%s",
                    block.name, exc,
                )

        # Accumulate trace for friction observer + tool receipts
        self._turn_tool_trace.append({
            "name": block.name,
            "input": tool_input,
            "success": not is_error,
            "result_preview": result[:200] if isinstance(result, str) else str(result)[:200],
        })

        # EVENT-STREAM-TO-SQLITE: one emission per tool invocation
        # (tool.returned on success, tool.failed on error). The inputs'
        # shape — not the inputs themselves — is captured so the payload
        # stays small and doesn't leak sensitive arguments.
        try:
            from kernos.kernel import event_stream
            _turn_id = getattr(getattr(request, "trace", None), "turn_id", None)
            _event_type = "tool.failed" if is_error else "tool.returned"
            await event_stream.emit(
                request.instance_id, _event_type,
                {
                    "name": block.name,
                    "args_keys": sorted((tool_input or {}).keys()),
                    "result_preview_len": len(str(result)[:200]) if result else 0,
                },
                member_id=request.member_id or None,
                space_id=request.active_space_id or None,
                correlation_id=_turn_id,
            )
        except Exception as exc:
            logger.debug("Failed to emit tool event: %s", exc)

        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": injected,
        }

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

    async def _run_via_turn_runner(
        self, request: ReasoningRequest
    ) -> ReasoningResult:
        """Route a ReasoningRequest through the TurnRunner.

        Pure adapter: translate ReasoningRequest into TurnRunnerInputs,
        invoke run_turn, and shape the response back into a
        ReasoningResult so the call site stays unchanged.

        The translation is deliberately conservative in C2 — only
        fields the TurnRunner needs are passed through. INTEGRATION-
        WIRE-LIVE will tighten the seam if needed once the full
        cohort/integration/enactment dependencies are wired into the
        production construction site.
        """
        from kernos.kernel.turn_runner import TurnRunnerInputs

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
            cognitive_context=getattr(request, "cognitive_context", None),
        )
        outcome = await self._turn_runner.run_turn(inputs)
        # The translation back to ReasoningResult is the response-
        # delivery hook's job in production; skeleton C2 returns
        # whatever the hook produced. Tests can inspect the outcome
        # directly via the TurnRunner; ReasoningService callers see
        # a ReasoningResult only when delivery is wired.
        if isinstance(outcome, ReasoningResult):
            return outcome
        # Conservative skeleton fallback — the response_delivery hook
        # has not been wired yet (or it returned a non-Result shape).
        # Surface a minimal Result so the call site doesn't crash on
        # the type contract; the real shape lands with INTEGRATION-
        # WIRE-LIVE when delivery is wired end-to-end.
        return ReasoningResult(
            text="",
            model=request.model,
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            duration_ms=0,
            tool_iterations=0,
        )

    async def reason(self, request: ReasoningRequest) -> ReasoningResult:
        """Run a full reasoning turn, including tool-use loop.

        Raises ReasoningError subtypes on provider failure. Does NOT catch them.

        PDI C2: when KERNOS_USE_DECOUPLED_TURN_RUNNER is set AND a
        TurnRunner has been wired, this façade routes to the new
        path. Otherwise the legacy loop runs unchanged. The signature
        and return shape are identical across both paths so existing
        callers (the message handler, scheduler, engine) need no
        modification.
        """
        from kernos.kernel.turn_runner import (
            TurnRunner,
            TurnRunnerNotWired,
            use_decoupled_turn_runner,
        )
        if use_decoupled_turn_runner():
            if self._turn_runner_provider is not None:
                return await self._run_via_turn_runner_provider(request)
            if self._turn_runner is None:
                raise TurnRunnerNotWired(
                    "KERNOS_USE_DECOUPLED_TURN_RUNNER is set but no "
                    "TurnRunner was provided to ReasoningService. "
                    "Either unset the flag or wire the TurnRunner "
                    "(see kernos/kernel/turn_runner.py). The decoupled "
                    "path lands incrementally across PDI C2-C7; full "
                    "wiring arrives with INTEGRATION-WIRE-LIVE."
                )
            return await self._run_via_turn_runner(request)
        t_global = time.monotonic()
        messages = list(request.messages)
        tools = request.tools
        total_input_tokens = 0
        total_output_tokens = 0

        # --- Initial reasoning.request ---
        try:
            await emit_event(
                self._events,
                EventType.REASONING_REQUEST,
                request.instance_id,
                "reasoning_service",
                payload={
                    "model": request.model,
                    "provider": getattr(self._provider, "provider_name", "unknown"),
                    "conversation_id": request.conversation_id,
                    "message_count": len(messages),
                    "tool_count": len(tools),
                    "system_prompt_length": len(request.system_prompt),
                    "trigger": request.trigger,
                },
            )
        except Exception as exc:
            logger.warning("Failed to emit reasoning.request: %s", exc)

        # Token estimation: hybrid (real baseline + delta) when available, char-based fallback.
        _tool_chars = sum(len(json.dumps(t)) for t in tools)
        _ctx_chars = len(request.system_prompt) + sum(
            len(m.get("content", "") if isinstance(m.get("content"), str)
                else json.dumps(m.get("content", "")))
            for m in messages
        )
        _char_est = (_ctx_chars + _tool_chars) // 4
        _last_real = self._last_real_input_tokens.get(request.instance_id, 0)
        if _last_real > 0:
            # Hybrid: real baseline + estimated delta from new content
            # The last real count covers the full context window at that point.
            # Delta is new user message + any changed context (estimated).
            _new_content_chars = len(request.input_text or "")
            _delta_est = _new_content_chars // 4
            _ctx_tokens_est = _last_real + _delta_est
        else:
            _ctx_tokens_est = _char_est

        _tool_sizes = [(t.get("name", "?"), len(json.dumps(t))) for t in tools]
        _tool_sizes.sort(key=lambda x: x[1], reverse=True)
        _tool_tokens = sum(chars // 4 for _, chars in _tool_sizes)
        _top3 = ", ".join(f"{name}={chars//4}tok" for name, chars in _tool_sizes[:3])
        logger.info(
            "REASON_START: tool_count=%d tool_tokens=%d max_tokens=%d msg_count=%d "
            "ctx_tokens_est=%d (hybrid=%d char=%d real_baseline=%d) top_tools=[%s]",
            len(tools), _tool_tokens, request.max_tokens, len(messages), _ctx_tokens_est,
            _ctx_tokens_est, _char_est, _last_real, _top3,
        )
        if logger.isEnabledFor(logging.DEBUG):
            for t in tools:
                _t_json = json.dumps(t)
                logger.debug(
                    "TOOL_SIZE: name=%s tokens_est=%d chars=%d",
                    t.get("name", "unknown"),
                    len(_t_json) // 4,
                    len(_t_json),
                )

        logger.info(
            "LLM_REQUEST: messages=%d tools=%d max_tokens=%d",
            len(messages), len(tools), request.max_tokens,
        )
        t0 = time.monotonic()
        # Build cache-boundary system prompt if static/dynamic split is available
        if request.system_prompt_static:
            _system: str | list[dict] = [
                {"type": "text", "text": request.system_prompt_static, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": request.system_prompt_dynamic},
            ]
        else:
            _system = request.system_prompt
        response = await self._call_chain(
            "primary", _system, messages, tools, request.max_tokens,
            request_model=request.model, request=request,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        total_input_tokens += response.input_tokens
        total_output_tokens += response.output_tokens

        # Store real input_tokens for hybrid estimation on next turn
        # This is the initial API call (full context window), not tool-loop iterations
        if response.input_tokens > 0:
            self._last_real_input_tokens[request.instance_id] = response.input_tokens

        logger.info(
            "LLM_RESPONSE: stop_reason=%s tokens_in=%d tokens_out=%d content_types=%s",
            response.stop_reason,
            response.input_tokens, response.output_tokens,
            [b.type for b in response.content],
        )
        if response.cache_creation_input_tokens or response.cache_read_input_tokens:
            logger.info(
                "CACHE: write=%d read=%d",
                response.cache_creation_input_tokens,
                response.cache_read_input_tokens,
            )
        for _b in response.content:
            if _b.type == "text":
                logger.info(
                    "LLM_BLOCK: type=text len=%d preview=%r",
                    len(_b.text or ""), (_b.text or "")[:300],
                )
            elif _b.type == "tool_use":
                logger.info(
                    "LLM_BLOCK: type=tool_use name=%s input=%r",
                    _b.name, str(_b.input)[:300],
                )

        rr_event = None
        try:
            rr_event = await emit_event(
                self._events,
                EventType.REASONING_RESPONSE,
                request.instance_id,
                "reasoning_service",
                payload={
                    "model": request.model,
                    "provider": getattr(self._provider, "provider_name", "unknown"),
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "estimated_cost_usd": estimate_cost(
                        request.model, response.input_tokens, response.output_tokens
                    ),
                    "stop_reason": response.stop_reason,
                    "duration_ms": duration_ms,
                    "conversation_id": request.conversation_id,
                },
            )
        except Exception as exc:
            logger.warning("Failed to emit reasoning.response: %s", exc)

        # Log what the model actually returned — before entering the tool-use loop.
        # This is the key diagnostic: did the model produce tool_use blocks or just text?
        _content_types = [block.type for block in response.content]
        _text_preview = ""
        for _b in response.content:
            if _b.type == "text" and _b.text:
                _text_preview = _b.text[:200]
                break
        logger.info(
            "REASON_RESPONSE: stop=%s content_types=%s text_preview=%r",
            response.stop_reason,
            _content_types,
            _text_preview,
        )

        # --- Tool-use loop ---
        iterations = 0
        _is_plan_step = (request.input_text or "").startswith("[PLAN STEP")
        _max_iters = self.MAX_TOOL_ITERATIONS_PLAN if _is_plan_step else self.MAX_TOOL_ITERATIONS
        _gate_cache: dict[str, Any] = {}  # tool_name → GateResult (for lazy-load re-runs)
        while (
            response.stop_reason == "tool_use"
            and iterations < _max_iters
        ):
            iterations += 1
            tool_results: list[dict] = []

            # Build a per-tool-call index of agent reasoning.
            _last_text = "No explicit reasoning provided."
            _tool_reasoning: dict[str, str] = {}
            for _b in response.content:
                if _b.type == "text" and _b.text:
                    _last_text = _b.text.strip() or "No explicit reasoning provided."
                elif _b.type == "tool_use" and _b.id:
                    _tool_reasoning[_b.id] = _last_text
                    _last_text = "No explicit reasoning provided."

            # Collect all tool_use blocks with their original index
            indexed_blocks: list[tuple[int, ContentBlock]] = []
            for i, block in enumerate(response.content):
                if block.type == "tool_use":
                    indexed_blocks.append((i, block))

            # Pre-pass: handle stubs (modifies shared tools list — must be sequential)
            stub_results: dict[int, dict] = {}
            non_stub_blocks: list[tuple[int, ContentBlock]] = []
            for idx, block in indexed_blocks:
                if block.name not in self._KERNEL_TOOLS:
                    _stub_entry = None
                    for _t in tools:
                        if _t.get("name") == block.name:
                            _stub_entry = _t
                            break
                    if _stub_entry and self._is_stub_schema(_stub_entry) and self._registry:
                        full_schema = self._registry.get_tool_schema(block.name)
                        if full_schema:
                            for _i, _t in enumerate(tools):
                                if _t.get("name") == block.name:
                                    tools[_i] = full_schema
                                    break
                            self.load_tool(request.active_space_id, block.name)
                            logger.info(
                                "TOOL_LOAD: tool=%s space=%s (stub -> full schema, re-running)",
                                block.name, request.active_space_id,
                            )
                            # Build a hint about required parameters from the full schema
                            _required = full_schema.get("input_schema", {}).get("required", [])
                            _props = full_schema.get("input_schema", {}).get("properties", {})
                            _param_hint = ""
                            if _required:
                                _param_hint = f" Required parameters: {', '.join(_required)}."
                            elif _props:
                                _param_hint = f" Available parameters: {', '.join(list(_props.keys())[:8])}."
                            stub_results[idx] = {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": (
                                    f"[SYSTEM] The tool {block.name} is now fully loaded with its complete schema. "
                                    f"Your previous call had empty parameters because the schema wasn't loaded yet."
                                    f"{_param_hint} "
                                    f"Please call {block.name} again with the correct parameters for the user's request."
                                ),
                            }
                            continue
                non_stub_blocks.append((idx, block))

            # Classify non-stub blocks into concurrent-safe vs sequential
            concurrent: list[tuple[int, ContentBlock]] = []
            sequential: list[tuple[int, ContentBlock]] = []
            for idx, block in non_stub_blocks:
                if self._is_concurrent_safe(block.name):
                    concurrent.append((idx, block))
                else:
                    sequential.append((idx, block))

            results_by_index: dict[int, dict] = dict(stub_results)

            # Log concurrency decision when multiple tools present
            total_tools = len(indexed_blocks)
            if total_tools > 1:
                logger.info(
                    "TOOL_CONCURRENT: parallel=%d sequential=%d stubs=%d total=%d",
                    len(concurrent), len(sequential), len(stub_results), total_tools,
                )

            # Execute concurrent-safe (read) tools in parallel
            if concurrent:
                async def _run_concurrent(
                    idx: int, block: ContentBlock,
                ) -> tuple[int, dict]:
                    tool_input = dict(block.input or {})
                    agent_reasoning = _tool_reasoning.get(
                        block.id or "", "No explicit reasoning provided.")
                    logger.info(
                        "TOOL_LOOP iter=%d tool=%s kernel=%s",
                        iterations, block.name, block.name in self._KERNEL_TOOLS,
                    )
                    try:
                        tr = await self._execute_single_tool(
                            block, tool_input, request, tools,
                            _gate_cache, rr_event, iterations, agent_reasoning,
                        )
                    except Exception as exc:
                        logger.warning("Concurrent tool error: tool=%s err=%s", block.name, exc)
                        tr = {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Tool error: {exc}",
                        }
                    return idx, tr

                gather_results = await asyncio.gather(
                    *[_run_concurrent(idx, block) for idx, block in concurrent],
                    return_exceptions=True,
                )
                for item in gather_results:
                    if isinstance(item, Exception):
                        logger.warning("Concurrent gather exception: %s", item)
                        continue
                    c_idx, c_result = item
                    results_by_index[c_idx] = c_result

            # Execute sequential (write/unknown) tools one at a time
            for idx, block in sequential:
                tool_input = dict(block.input or {})
                agent_reasoning = _tool_reasoning.get(
                    block.id or "", "No explicit reasoning provided.")
                logger.info(
                    "TOOL_LOOP iter=%d tool=%s kernel=%s",
                    iterations, block.name, block.name in self._KERNEL_TOOLS,
                )
                tr = await self._execute_single_tool(
                    block, tool_input, request, tools,
                    _gate_cache, rr_event, iterations, agent_reasoning,
                )
                results_by_index[idx] = tr

            # Emit tool_results in original block order
            tool_results = [results_by_index[idx] for idx, _ in indexed_blocks]

            messages.append(
                {
                    "role": "assistant",
                    "content": [_block_to_api_dict(b) for b in response.content],
                }
            )
            messages.append({"role": "user", "content": tool_results})

            # Emit reasoning.request for continuation
            try:
                await emit_event(
                    self._events,
                    EventType.REASONING_REQUEST,
                    request.instance_id,
                    "reasoning_service",
                    payload={
                        "model": request.model,
                        "provider": getattr(self._provider, "provider_name", "unknown"),
                        "conversation_id": request.conversation_id,
                        "message_count": len(messages),
                        "tool_count": len(tools),
                        "system_prompt_length": len(request.system_prompt),
                        "trigger": "tool_continuation",
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit reasoning.request: %s", exc)

            logger.info(
                "LLM_REQUEST: messages=%d tools=%d max_tokens=%d",
                len(messages), len(tools), request.max_tokens,
            )
            t0 = time.monotonic()
            response = await self._call_chain(
                "primary", _system, messages, tools, request.max_tokens,
                request_model=request.model, request=request,
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            total_input_tokens += response.input_tokens
            total_output_tokens += response.output_tokens

            logger.info(
                "LLM_RESPONSE: stop_reason=%s content_types=%s",
                response.stop_reason,
                [b.type for b in response.content],
            )
            if response.cache_creation_input_tokens or response.cache_read_input_tokens:
                logger.info(
                    "CACHE: write=%d read=%d",
                    response.cache_creation_input_tokens,
                    response.cache_read_input_tokens,
                )
            for _b in response.content:
                if _b.type == "text":
                    logger.info(
                        "LLM_BLOCK: type=text len=%d preview=%r",
                        len(_b.text or ""), (_b.text or "")[:300],
                    )
                elif _b.type == "tool_use":
                    logger.info(
                        "LLM_BLOCK: type=tool_use name=%s input=%r",
                        _b.name, str(_b.input)[:300],
                    )

            rr_event = None
            try:
                rr_event = await emit_event(
                    self._events,
                    EventType.REASONING_RESPONSE,
                    request.instance_id,
                    "reasoning_service",
                    payload={
                        "model": request.model,
                        "provider": getattr(self._provider, "provider_name", "unknown"),
                        "input_tokens": response.input_tokens,
                        "output_tokens": response.output_tokens,
                        "estimated_cost_usd": estimate_cost(
                            request.model, response.input_tokens, response.output_tokens
                        ),
                        "stop_reason": response.stop_reason,
                        "duration_ms": duration_ms,
                        "conversation_id": request.conversation_id,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit reasoning.response: %s", exc)

        # --- Build result ---
        total_duration_ms = int((time.monotonic() - t_global) * 1000)
        estimated_cost = estimate_cost(
            request.model, total_input_tokens, total_output_tokens
        )

        logger.info(
            "TOOL_LOOP exit: iterations=%d stop=%s has_text=%s",
            iterations, response.stop_reason,
            bool([b for b in response.content if b.type == "text"]),
        )

        if response.stop_reason == "max_tokens":
            logger.warning(
                "RESPONSE_TRUNCATED: max_tokens=%d reached on iter=%d. "
                "Tool calls may have been cut off. Consider raising max_tokens.",
                request.max_tokens, iterations,
            )

        if iterations >= _max_iters:
            logger.warning("TOOL_LOOP EXHAUSTED after %d iterations (limit=%d plan=%s)",
                iterations, _max_iters, _is_plan_step)
            return ReasoningResult(
                text="I'm having trouble completing that request. Try asking in a simpler way.",
                model=request.model,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                estimated_cost_usd=estimated_cost,
                duration_ms=total_duration_ms,
                tool_iterations=iterations,
            )

        text_parts = [b.text for b in response.content if b.type == "text"]
        response_text = (
            "".join(text_parts)
            if text_parts
            else "I processed your request but don't have a text response. Try rephrasing?"
        )

        # Hallucination detector: observe-only mode.
        # Detects when the agent claims tool use in text without actually calling a tool.
        # Logs detection + Haiku analysis for monitoring. Does NOT intervene or block.
        if iterations == 0 and response.stop_reason == "end_turn":
            _TOOL_CLAIM_PHRASES = (
                # Agent claims it PERFORMED an action (first person + past tense)
                # These indicate the agent believes it did something, not that it's
                # discussing a tool conceptually. Mentioning a tool by name without
                # claiming action is NOT a hallucination signal.
                "i created", "i deleted", "i wrote", "i removed",
                "i've created", "i've deleted", "i've written", "i've removed",
                "i scheduled", "i've scheduled", "i set a reminder",
                "i've set a reminder",
                "i sent", "i've sent",
                # Completion claims at start of response
                "done —", "done.", "✅",
                # Schedule-specific: agent describes a COMPLETED future action
                "reminder set", "event created",
                "heads up at", "alert at", "notification at",
                "locked in", "lands at", "queued for",
                "on its way", "will arrive at", "dropping at",
            )
            rt_lower = response_text.lower()

            # Pattern-based detection: if the user's message implied an action request
            # and the agent responded with text-only (no tool call), check via Haiku.
            # This catches novel phrasing that the phrase list misses.
            _phrase_match = any(phrase in rt_lower for phrase in _TOOL_CLAIM_PHRASES)
            if not _phrase_match and request.input_text:
                _ACTION_REQUEST_SIGNALS = (
                    "remind", "schedule", "set a", "create a", "send a",
                    "send me", "delete", "remove", "write a", "save",
                    "remember", "tell me to", "notify me",
                    "in 2 min", "in 5 min", "in 10 min", "in an hour",
                    "in 1 hour", "in 30 min", "in 15 min",
                    "tomorrow at", "every morning", "every day",
                )
                _input_lower = request.input_text.lower()
                _user_wants_action = any(
                    sig in _input_lower for sig in _ACTION_REQUEST_SIGNALS
                )
                if _user_wants_action and len(response_text) < 200:
                    _has_time = bool(re.search(r'\d{1,2}:\d{2}', response_text))
                    if _has_time:
                        _phrase_match = True
                        logger.info(
                            "HALLUCINATION_PATTERN: short response with time to action "
                            "request, no tool call. input=%r response=%r",
                            request.input_text[:100], response_text[:100],
                        )

            if _phrase_match:
                original_preview = response_text[:200]
                logger.warning(
                    "HALLUCINATION_CHECK: Agent claims tool use but iterations=0 "
                    "(stop=%s, tool_count=%d). Hands-off mode — response passed through. "
                    "Original: %s",
                    response.stop_reason, len(tools), original_preview,
                )

                # Analyze why this hallucination occurred (cheap Haiku call for diagnostics)
                try:
                    _user_msg = request.input_text[:200] if request.input_text else "(unknown)"
                    _expected_tool = "unknown"
                    for _phrase, _tool in (
                        ("schedul", "manage_schedule"), ("remind", "manage_schedule"),
                        ("calendar", "create-event"), ("event", "create-event"),
                        ("email", "send-email"), ("file", "write_file"),
                        ("remember", "remember"),
                    ):
                        if _phrase in rt_lower:
                            _expected_tool = _tool
                            break
                    _prior_calls = sum(
                        1 for m in messages
                        if m.get("role") == "assistant"
                        and isinstance(m.get("content"), list)
                        and any(
                            b.get("type") == "tool_use" and b.get("name") == _expected_tool
                            for b in m["content"]
                            if isinstance(b, dict)
                        )
                    )
                    _analysis = await self.complete_simple(
                        system_prompt=(
                            "You are analyzing why an LLM agent generated text "
                            "instead of calling a tool. Be specific about what "
                            "in the context most likely caused this."
                        ),
                        user_content=(
                            f"The agent was asked: {_user_msg}\n"
                            f"Available tool: {_expected_tool}\n"
                            f"Instead of calling the tool, it generated: "
                            f"'{original_preview}'\n"
                            f"Conversation had {len(messages)} messages. "
                            f"Prior successful calls to this tool in session: "
                            f"{_prior_calls}\n"
                            f"Analyze the most likely cause in 1-2 sentences."
                        ),
                        max_tokens=256,
                        prefer_cheap=True,
                    )
                    logger.info(
                        "HALLUCINATION_ANALYSIS: tool=%s prior_calls=%d analysis=%s",
                        _expected_tool, _prior_calls, _analysis[:200],
                    )
                except Exception as _exc:
                    logger.warning("HALLUCINATION_ANALYSIS: failed: %s", _exc)

                # COACHING LOOP: Currently disabled (hands-off mode).
                # The system prompt cleanup (ACTIONS REQUIRE TOOL CALLS,
                # bootstrap graduation, covenant pruning) was the real fix.
                # Re-enable if hallucination rates increase after prompt
                # changes stabilize.
                # See: Session 2026-03-22, hallucination audit findings.

        return ReasoningResult(
            text=response_text,
            model=request.model,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            estimated_cost_usd=estimated_cost,
            duration_ms=total_duration_ms,
            tool_iterations=iterations,
        )
