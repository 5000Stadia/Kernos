"""OpenAI Codex OAuth provider — ChatGPT Codex Responses API.

Explicit opt-in only (``KERNOS_LLM_PROVIDER=openai-codex``); the shipped
default is the API-key Anthropic provider. This path authenticates with a
personal ChatGPT-subscription OAuth token — OpenAI's terms steer
programmatic access to the API-key platform, so this provider exists for
personal-use configurations, not as a recommended default.
"""
import asyncio
import json
import logging
import os
import time
from typing import Any

from kernos.kernel.exceptions import (
    ReasoningConnectionError,
    ReasoningProviderError,
    ReasoningRateLimitError,
    ReasoningTransientError,
)
from kernos.providers.base import ContentBlock, Provider, ProviderResponse

logger = logging.getLogger(__name__)

_OPENAI_SIMPLE_MODEL = "gpt-4o"
_OPENAI_CHEAP_MODEL = "gpt-4o-mini"


def _force_strict_object_schema(schema: Any) -> Any:
    """Recursively ensure every ``type: "object"`` level in a JSON
    schema declares ``additionalProperties: false``.

    OpenAI Codex's responses endpoint tightened JSON-schema
    validation 2026-05-22: previously-accepted schemas now fail with
    "In context=(<path>), 'additionalProperties' is required to be
    supplied and to be false."

    Normalizing here at the provider boundary keeps every caller
    source-compatible. Handles the three nesting paths the API
    actually walks:

      * Object schemas (recurse into ``properties.*``).
      * Array schemas (recurse into ``items``, ``prefixItems``).
      * Composition: ``oneOf`` / ``anyOf`` / ``allOf`` (recurse into
        each branch).

    Returns a NEW dict; the input is not mutated so callers can
    keep using their module-level schema constants.
    """
    if isinstance(schema, list):
        return [_force_strict_object_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    schema_type = out.get("type")
    if schema_type == "object":
        # Force additionalProperties: false at this level regardless
        # of what (if anything) the caller declared. The API requires
        # false, not just present.
        out["additionalProperties"] = False
        if "properties" in out and isinstance(out["properties"], dict):
            out["properties"] = {
                k: _force_strict_object_schema(v)
                for k, v in out["properties"].items()
            }
    if schema_type == "array":
        if "items" in out:
            out["items"] = _force_strict_object_schema(out["items"])
        if "prefixItems" in out:
            out["prefixItems"] = [
                _force_strict_object_schema(item)
                for item in out["prefixItems"]
            ]
    for composition_key in ("oneOf", "anyOf", "allOf"):
        if composition_key in out and isinstance(out[composition_key], list):
            out[composition_key] = [
                _force_strict_object_schema(branch)
                for branch in out[composition_key]
            ]
    # $defs / definitions can also carry object schemas
    for defs_key in ("$defs", "definitions"):
        if defs_key in out and isinstance(out[defs_key], dict):
            out[defs_key] = {
                k: _force_strict_object_schema(v)
                for k, v in out[defs_key].items()
            }
    return out


class OpenAICodexProvider(Provider):
    """ChatGPT Codex OAuth provider — uses chatgpt.com/backend-api/codex/responses.

    NOT the standard OpenAI API. This talks to OpenAI's *consumer* backend
    (the same endpoint OpenClaw / pi-ai use). The consumer backend is more
    sensitive to body shape than api.openai.com — small differences in
    field presence or value flip whether large payloads stream cleanly or
    mid-stream-fail with ``server_error`` SSE events.

    ============================================================================
    WIRE SHAPE INVARIANTS — DO NOT REMOVE WITHOUT REPLACEMENT
    ============================================================================

    Every field listed here is load-bearing. Each one was added in response to
    a specific failure mode observed in production. The original "minimal
    body" shape worked for small payloads but failed reliably above ~40KB
    with real tool schemas. The current shape mirrors OpenClaw's installed
    Codex transport (``@mariozechner/pi-ai openai-codex-responses``) which
    has been stable in production for the same backend.

    If you find yourself wanting to "clean up" any of the fields below, run
    ``scripts/diagnostics/codex_replay_mutations.py`` against a captured >40KB body
    first — strip the field, see the failure rate change. The mutation
    matrix is the contract test for this transport.

    REQUIRED BODY FIELDS:

    * ``model`` — str. e.g. "gpt-5.5". Determines reasoning capability path.
    * ``instructions`` — str. System prompt as a single block (NOT a system
      message in ``input``). The consumer backend rejects system roles in
      ``input`` for codex/responses.
    * ``input`` — list of message items in Responses-API shape. Each item
      is ``{role, content: [{type: "input_text", text: ...}]}`` for user,
      or ``{role: "developer", content: ...}`` for the developer-role
      override (overflow-handling).
    * ``store: false`` — REQUIRED. Tells backend not to persist conversation
      state. Omitting this triggers extra backend persistence work that
      tips large payloads into mid-stream timeouts. OpenClaw sets this
      via ``storeMode: "disable"``.
    * ``stream: true`` — REQUIRED. The endpoint is SSE-only.
    * ``tool_choice`` — REQUIRED when tools are present. Defaults to
      ``"auto"`` for normal calls; finalizer loops may pass ``"required"``.
      OpenClaw sends this field on every tool-bearing call.
    * ``parallel_tool_calls: true`` — REQUIRED. Allows backend to plan
      multi-tool turns. OpenClaw sends this.
    * ``include: ["reasoning.encrypted_content"]`` — REQUIRED for gpt-5.x
      reasoning models. Without it, the backend silently drops reasoning
      state across turns, breaking continuity.
    * ``prompt_cache_key`` — REQUIRED on every call where the caller has
      a conversation identity. Set to a stable per-conversation string
      (Discord channel id, REPL session id, etc.). Without it, the
      consumer backend's KV cache misses on every turn and large payloads
      recompute attention from scratch — reliably causing mid-stream
      ``server_error`` once payload + tool schemas cross ~40KB.
    * ``reasoning: {effort, summary}`` — REQUIRED for gpt-5.x. Omitting
      lets the backend pick effort, which on big payloads drifts toward
      "high" and blows the per-request time budget. ``effort`` overridable
      via ``OPENAI_CODEX_REASONING_EFFORT`` (default "medium"). OpenClaw
      defaults to "high" — both are stable; "medium" is the conservative
      latency choice.
    * ``text: {verbosity: "medium"}`` — for non-schema responses. When an
      output_schema is set, ``text.format`` carries the JSON-schema spec
      instead. Either ``text.verbosity`` or ``text.format`` must be set;
      the field cannot be empty/missing on the consumer backend.

    REQUIRED TOOL SHAPE — see ``_translate_tools`` for the load-bearing
    detail. **Every tool MUST have ``strict: None`` as a top-level key**.
    Missing-vs-null is the trigger for the ~40KB failure mode confirmed
    by 2026-05-02 mutation matrix replay (0/3 fail flipped to 5/5 pass
    with strict:None alone). OpenClaw sets the same: ``{strict: null}``.

    REQUIRED HEADERS — see ``_headers``. Auth (Bearer + chatgpt-account-id),
    transport identity (originator: pi, OS-aware User-Agent), and per-call
    routing (session_id + x-client-request-id when conversation_id is
    provided) are all load-bearing.

    ============================================================================
    PLUMBING INVARIANTS — conversation_id must reach the provider
    ============================================================================

    Every caller of ``complete()`` MUST forward ``conversation_id``. The
    chain in production is:

        PresenceRenderer.render(briefing)
        → self._chain_caller(..., conversation_id=briefing.turn_id)
        → response_delivery._wrapped(..., conversation_id=...)
        → _shared_chain_caller(..., conversation_id=...)  [server.py + repl.py]
        → entry.provider.complete(..., conversation_id=...)
        → body["prompt_cache_key"] = conversation_id
          headers["session_id"] = conversation_id
          headers["x-client-request-id"] = conversation_id

    If any seam drops conversation_id, the ~40KB failure mode returns.
    Pin tests at ``tests/test_thin_path_codex_wire_shape_plumbing.py``
    enforce the plumbing; do not delete them.

    ============================================================================
    INVESTIGATION TOOLING
    ============================================================================

    If a future failure looks wire-shape-related:

    1. Set ``KERNOS_CODEX_CAPTURE_BODY=1`` in env. Triggers JSON dump of
       any >40KB body to ``/tmp/codex_bodies/`` (path overridable via
       ``KERNOS_CODEX_CAPTURE_DIR``, threshold via
       ``KERNOS_CODEX_CAPTURE_THRESHOLD_KB``).
    2. Trigger the failing turn.
    3. Run ``python scripts/diagnostics/codex_replay_mutations.py <body.json>``. The
       mutation matrix tries strip-field, value-swap, and shape mutations
       and reports which one flips success rate. That's your trigger.

    Do NOT diagnose by guessing. The matrix is fast (≤5min) and definitive.
    """

    provider_name = "openai-codex"

    def __init__(
        self,
        credential: "OpenAICodexCredential",
        model: str = "",
    ) -> None:
        self._credential = credential
        self.main_model = model or os.getenv("OPENAI_CODEX_MODEL", "gpt-5.5")
        # Two-tier: primary (main_model) + lightweight. Env var resolution
        # prefers OPENAI_CODEX_LIGHTWEIGHT_MODEL; falls back to the legacy
        # OPENAI_CODEX_CHEAP_MODEL so existing .env files keep working;
        # default gpt-5.4-mini because no gpt-5.5-{mini,nano} exists in
        # the Codex ChatGPT catalog (OpenAI's 5.x mini tier tops out at
        # gpt-5.4-mini per published model list).
        self.lightweight_model = (
            os.getenv("OPENAI_CODEX_LIGHTWEIGHT_MODEL")
            or os.getenv("OPENAI_CODEX_CHEAP_MODEL")
            or "gpt-5.4-mini"
        )
        # Legacy aliases retained for external readers.
        self.simple_model = os.getenv("OPENAI_CODEX_SIMPLE_MODEL", self.main_model)
        self.cheap_model = self.lightweight_model
        self._base_url = os.getenv(
            "OPENAI_CODEX_BASE_URL", "https://chatgpt.com/backend-api"
        )
        self._http: Any = None
        self._trace: Any = None  # TurnEventCollector — set per-turn by reasoning

    async def _ensure_http(self) -> Any:
        if self._http is None:
            import httpx
            # NOTE (2026-06-02): the keepalive-limits variant of this client
            # was REVERTED — it correlated in time with a deterministic
            # `no_tool_use` synthesis failure (worked before, broke after).
            # Mechanism unclear, but the timing made it the prime suspect, so
            # we restore the original unbounded client to empirically test
            # that. If the failure persists after revert, it's the pre-existing
            # synthesis bug (FORCE-SYNTHESIS-TOOL-CHOICE-V1), not this client.
            self._http = httpx.AsyncClient(timeout=120.0)
        return self._http

    async def _ensure_valid_token(self) -> None:
        """Refresh the access token if expired or within 5 minutes of expiry."""
        now_ms = int(time.time() * 1000)
        if self._credential["expires"] and self._credential["expires"] > now_ms + 300_000:
            return
        from kernos.kernel.credentials import refresh_openai_codex_credential
        logger.info("CODEX_REFRESH: token expired or near expiry, refreshing")
        self._credential = await refresh_openai_codex_credential(self._credential)

    def _headers(self, *, session_id: str = "") -> dict[str, str]:
        """Build request headers matching OpenClaw's Codex wire contract.

        Every header below is load-bearing — the consumer backend
        rejects calls or routes them to a different path if any are
        missing. Do NOT remove or rename without replacement.

        When session_id is provided, also sets the session_id and
        x-client-request-id headers so the backend can correlate calls
        in the same conversation for routing and prompt-cache hits.
        """
        ua = self._user_agent()
        headers = {
            # OAuth bearer from ChatGPT credential. NOT an OPENAI_API_KEY —
            # the consumer codex backend rejects API keys on this endpoint.
            "Authorization": f"Bearer {self._credential['access']}",
            # Per-account routing. Required by chatgpt.com backend.
            "chatgpt-account-id": self._credential["accountId"],
            # Transport identity tag — backend treats "pi" as a known
            # transport. Other values get rate-limited or rejected.
            "originator": "pi",
            # OS-aware UA matching OpenClaw shape exactly. The bare string
            # "pi (python)" gets reduced quotas on the consumer backend;
            # the OS-detail form mirrors what's been measured stable.
            "User-Agent": ua,
            "Content-Type": "application/json",
            # Required for Responses API on the consumer backend.
            "OpenAI-Beta": "responses=experimental",
            # Default; the actual call sets this to "text/event-stream"
            # before sending (see complete()). Stream is REQUIRED.
            "accept": "application/json",
        }
        if session_id:
            # Session correlation headers — set on every call where the
            # caller has a conversation identity. Without these, the
            # consumer backend's prompt-cache misses on every turn and
            # large payloads tip into mid-stream timeouts. See class
            # docstring "WIRE SHAPE INVARIANTS" for the full failure mode.
            headers["session_id"] = session_id
            headers["x-client-request-id"] = session_id
        return headers

    @staticmethod
    def _user_agent() -> str:
        """OS-aware User-Agent matching OpenClaw's shape: 'pi (<system> <release>; <machine>)'."""
        try:
            import platform
            system = platform.system().lower() or "unknown"
            release = platform.release() or ""
            machine = platform.machine() or ""
            details = "; ".join(p for p in (release, machine) if p)
            return f"pi ({system} {details})".strip()
        except Exception:
            return "pi (python)"

    @staticmethod
    def _translate_tools(tools: list[dict], skin: dict | None = None) -> list[dict]:
        """Convert Anthropic-format tool defs to OpenAI Responses API function format.

        ``strict: None`` is set explicitly on every tool because the Codex
        consumer backend treats a missing ``strict`` key differently from
        ``strict: null`` on payloads with real (non-trivial) tool schemas:
        without the explicit null, calls reliably mid-stream-fail with
        ``server_error`` once payload crosses ~40KB. The mutation matrix at
        ``scripts/diagnostics/codex_replay_mutations.py`` 2026-05-02 confirmed this is
        the trigger (0/3 fail rate flipped to 3/3 pass with this single
        change). OpenClaw's installed Codex transport uses the same
        explicit-null pattern (``convertResponsesTools(tools, {strict: null})``).
        """
        # TOOL-ARG-REPAIR-V1 guidance: lead every description with a compact
        # SIGNATURE (+ EXAMPLE for high-fumble tools) so the call pattern is
        # the FIRST thing the model reads, not buried in prose. Generated from
        # the same schema we send — no second source of truth. Best-effort:
        # presentation must never break dispatch.
        try:
            from kernos.kernel.tool_signatures import signature_prefix
        except Exception:  # pragma: no cover
            signature_prefix = None  # type: ignore[assignment]
        result = []
        for t in tools:
            schema = t.get("input_schema", {"type": "object", "properties": {}})
            # SEMANTIC-ACTION-ENVELOPE-V1: present kernel tools to the model
            # under their area__tool wire name (skin map); MCP/workshop names
            # pass through unchanged. Internal surfaces stay flat.
            _wire_name = (skin or {}).get(t["name"], t["name"])
            _description = t.get("description", "")
            if signature_prefix is not None:
                try:
                    _description = signature_prefix(t, _wire_name) + _description
                except Exception:
                    pass
            result.append({
                # Tool envelope. The consumer backend expects exactly these
                # five keys per tool — type, name, description, parameters,
                # strict. Adding extra keys is silently ignored; missing
                # `strict` is the production failure trigger.
                "type": "function",
                "name": _wire_name,
                "description": _description,
                "parameters": schema,
                # CRITICAL: `strict` MUST be present, MUST be exactly None
                # (renders as JSON `null`). This is enforced by the pin
                # test test_body_tools_carry_explicit_strict_null. If you
                # find yourself wanting to drop this line because "the key
                # is set to null, that's the same as missing it" — it
                # isn't, and the symptom is mid-stream server_error on
                # payloads above ~40KB. Run scripts/diagnostics/codex_replay_mutations.py
                # to convince yourself before changing.
                "strict": None,
            })
        return result

    @staticmethod
    def _translate_input(messages: list[dict], skin: dict | None = None) -> list[dict]:
        """Convert Anthropic-format messages to OpenAI Responses API input items."""
        items: list[dict] = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if isinstance(content, str):
                if role == "assistant":
                    items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    })
                else:
                    items.append({
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": content}],
                    })
                continue

            if isinstance(content, list):
                tool_calls = []
                text_parts = []
                tool_results = []

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "tool_use":
                        tool_calls.append(block)
                    elif btype == "tool_result":
                        tool_results.append(block)
                    elif btype == "text":
                        text_parts.append(block.get("text", ""))

                if tool_calls:
                    if text_parts:
                        text = "".join(text_parts)
                        items.append({
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        })
                    for tc in tool_calls:
                        # Re-skin prior tool_use names so the function-call
                        # history the model sees matches the namespaced tool
                        # list it was offered (SEMANTIC-ACTION-ENVELOPE-V1).
                        _tc_name = tc.get("name", "")
                        _tc_name = (skin or {}).get(_tc_name, _tc_name)
                        items.append({
                            "type": "function_call",
                            "call_id": tc.get("id", ""),
                            "name": _tc_name,
                            "arguments": json.dumps(tc.get("input", {})),
                        })
                elif tool_results:
                    for tr in tool_results:
                        items.append({
                            "type": "function_call_output",
                            "call_id": tr.get("tool_use_id", ""),
                            "output": tr.get("content", ""),
                        })
                elif text_parts:
                    items.append({
                        "type": "message",
                        "role": role if role != "assistant" else "user",
                        "content": [{"type": "input_text", "text": "".join(text_parts)}],
                    })

        return items

    @staticmethod
    def _parse_response(data: dict, unskin: dict | None = None) -> ProviderResponse:
        """Parse OpenAI Responses API response into Kernos-native format."""
        _unskin = unskin or {}
        status = data.get("status", "completed")
        output_items = data.get("output", [])

        if status == "incomplete":
            stop_reason = "max_tokens"
        elif any(item.get("type") == "function_call" for item in output_items):
            stop_reason = "tool_use"
        else:
            stop_reason = "end_turn"

        content_blocks: list[ContentBlock] = []


        for item in output_items:
            item_type = item.get("type", "")

            if item_type == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        content_blocks.append(
                            ContentBlock(type="text", text=part.get("text", ""))
                        )

            elif item_type == "output_text":
                # Direct output_text item (structured output / text format)
                content_blocks.append(
                    ContentBlock(type="text", text=item.get("text", ""))
                )

            elif item_type == "function_call":
                try:
                    args = json.loads(item.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                call_name = item.get("name", "")

                # Unpack synthetic multi_tool_use.parallel into individual calls
                if call_name == "multi_tool_use.parallel":
                    for sub_call in args.get("tool_uses", []):
                        sub_args = sub_call.get("parameters", {})
                        if isinstance(sub_args, str):
                            try:
                                sub_args = json.loads(sub_args)
                            except json.JSONDecodeError:
                                sub_args = {}
                        # Unskin the namespaced wire name back to the flat tool
                        # id before it enters substrate (SEMANTIC-ACTION-
                        # ENVELOPE-V1: internal surfaces are always flat).
                        _rname = sub_call.get("recipient_name", "")
                        _rname = _unskin.get(_rname, _rname)
                        content_blocks.append(ContentBlock(
                            type="tool_use",
                            id=_rname + "_" + item.get("call_id", ""),
                            name=_rname,
                            input=sub_args,
                        ))
                    logger.info("CODEX_PARALLEL_UNPACK: unpacked %d tool calls from multi_tool_use.parallel",
                        len(args.get("tool_uses", [])))
                else:
                    content_blocks.append(ContentBlock(
                        type="tool_use",
                        id=item.get("call_id", item.get("id", "")),
                        name=_unskin.get(call_name, call_name),
                        input=args,
                    ))

        if not content_blocks:
            content_blocks.append(ContentBlock(type="text", text=""))

        usage = data.get("usage", {})
        return ProviderResponse(
            content=content_blocks,
            stop_reason=stop_reason,
            input_tokens=usage.get("input_tokens", usage.get("prompt_tokens", 0)),
            output_tokens=usage.get("output_tokens", usage.get("completion_tokens", 0)),
        )

    def _resolve_url(self) -> str:
        """Build the Codex responses endpoint URL."""
        base = self._base_url.rstrip("/")
        if base.endswith("/codex/responses"):
            return base
        if base.endswith("/codex"):
            return f"{base}/responses"
        return f"{base}/codex/responses"

    async def complete(
        self,
        model: str,
        system: str | list[dict],
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        output_schema: dict | None = None,
        conversation_id: str = "",
        tool_choice: str = "auto",
    ) -> ProviderResponse:
        await self._ensure_valid_token()
        http = await self._ensure_http()

        # SEMANTIC-ACTION-ENVELOPE-V1 (option A): build the request-local
        # tool-name skin maps once from the flat `tools` list. Outbound,
        # kernel tools are presented as area__tool; inbound function calls are
        # unskinned back to flat before they enter substrate.
        from kernos.kernel.tool_namespace import build_skin_maps
        _skin, _unskin = build_skin_maps(tools)

        # Codex API has a ~32KB limit on the instructions field.
        # Strategy: use the static/dynamic split from Anthropic's cache boundary.
        # Static (RULES + ACTIONS) → instructions field (stable, fits in 30KB).
        # Dynamic (NOW + STATE + RESULTS + MEMORY) → developer message in input
        # (no size limit beyond the model's context window).
        # This is intentional architecture, not just overflow handling.
        _INSTRUCTIONS_LIMIT = 30000

        if isinstance(system, list) and len(system) >= 2:
            # Cache-boundary format: [static, dynamic]
            instructions_str = system[0].get("text", "") if isinstance(system[0], dict) else str(system[0])
            dynamic_str = system[1].get("text", "") if isinstance(system[1], dict) else str(system[1])
            # If static alone exceeds limit, trim it too
            if len(instructions_str) > _INSTRUCTIONS_LIMIT:
                cut = instructions_str.rfind("\n", 0, _INSTRUCTIONS_LIMIT)
                if cut <= 0:
                    cut = _INSTRUCTIONS_LIMIT
                dynamic_str = instructions_str[cut:] + "\n\n" + dynamic_str
                instructions_str = instructions_str[:cut]
        elif isinstance(system, list):
            instructions_str = "\n\n".join(b.get("text", "") for b in system if b.get("text"))
            dynamic_str = ""
        else:
            instructions_str = system
            dynamic_str = ""

        # If no split was available and instructions exceed limit, split on newline
        if not dynamic_str and len(instructions_str) > _INSTRUCTIONS_LIMIT:
            cut = instructions_str.rfind("\n", 0, _INSTRUCTIONS_LIMIT)
            if cut <= 0:
                cut = _INSTRUCTIONS_LIMIT
            dynamic_str = instructions_str[cut:]
            instructions_str = instructions_str[:cut]

        translated_input = self._translate_input(messages, _skin)
        if dynamic_str:
            translated_input.insert(0, {"role": "developer", "content": dynamic_str})
            logger.info("CODEX_SPLIT: instructions=%dKB developer_msg=%dKB input_items=%d",
                len(instructions_str) // 1024, len(dynamic_str) // 1024,
                len(translated_input))

        # ====================================================================
        # CODEX REQUEST BODY — every field below is load-bearing.
        # See the OpenAICodexProvider class docstring "WIRE SHAPE INVARIANTS"
        # section for the full failure mode that maps to each field. Stripping
        # any of these reproduces a known production failure on the consumer
        # backend. Run scripts/diagnostics/codex_replay_mutations.py before changing.
        # ====================================================================
        body: dict[str, Any] = {
            # Model id, e.g. "gpt-5.5". Determines reasoning capability path.
            "model": model,
            # System prompt as a single block (NOT a system role in input).
            # The consumer backend rejects {role: "system"} items in input.
            "instructions": instructions_str,
            # User/developer/assistant messages in Responses-API shape.
            "input": translated_input,
            # REQUIRED. Tells backend NOT to persist conversation state.
            # Without this (or with True), persistence overhead tips
            # large payloads into mid-stream timeouts. OpenClaw sets the
            # equivalent via storeMode: "disable".
            "store": False,
            # REQUIRED. The codex/responses endpoint is SSE-only.
            "stream": True,
            # REQUIRED when tools present. OpenClaw sends this on every
            # tool-bearing call. Removing it changes backend tool-routing
            # behavior unpredictably. Defaults to "auto"; integration
            # synthesis may force "required" for synthetic finalizers.
            "tool_choice": tool_choice,
            # REQUIRED. Allows backend to plan multi-tool turns. OpenClaw
            # sends this. Verified present in their stable wire shape.
            "parallel_tool_calls": True,
            # REQUIRED for gpt-5.x reasoning models. Without it, the
            # backend silently drops reasoning state across turns,
            # breaking continuity. Do NOT remove even if you "don't use
            # reasoning" — the backend needs the field to be present.
            "include": ["reasoning.encrypted_content"],
        }
        # prompt_cache_key — REQUIRED on every call where the caller has a
        # conversation identity. Discord channel id / REPL session / etc.
        # Without it, consumer backend's KV cache misses on every turn,
        # large payloads recompute attention from scratch, and >40KB calls
        # mid-stream-fail with `server_error` (e50fb32 fix). Plumbed via
        # PresenceRenderer → response_delivery → _shared_chain_caller →
        # provider.complete(conversation_id=...). Pin tests at
        # tests/test_thin_path_codex_wire_shape_plumbing.py enforce this.
        if conversation_id:
            body["prompt_cache_key"] = conversation_id
        # reasoning — REQUIRED for gpt-5.x. Omitting lets backend pick
        # effort which on big payloads drifts toward "high" and blows the
        # per-request time budget. OPENAI_CODEX_REASONING_EFFORT overrides.
        # OpenClaw defaults to "high"; "medium" is conservative latency.
        if model.startswith("gpt-5"):
            body["reasoning"] = {
                "effort": os.getenv("OPENAI_CODEX_REASONING_EFFORT", "medium"),
                "summary": "auto",
            }
        # tools — translated via _translate_tools which sets `strict: None`
        # on every tool. The strict-key-presence requirement is THE trigger
        # for the >40KB tool-heavy failure mode confirmed 2026-05-02. See
        # _translate_tools docstring for the full story. Do NOT bypass.
        if tools:
            body["tools"] = self._translate_tools(tools, _skin)
        # text — REQUIRED. Either {format: ...} for schema-constrained
        # output, or {verbosity: ...} for free-form. The field cannot be
        # missing; the consumer backend treats absent text as a malformed
        # request on the codex/responses endpoint.
        if output_schema:
            # 2026-05-22: OpenAI Codex API tightened JSON-schema
            # validation — every ``type: object`` level must declare
            # ``additionalProperties: false`` explicitly. Production
            # was hitting:
            #   "In context=(), 'additionalProperties' is required to
            #    be supplied and to be false."
            # on schemas authored before the API change. Substrate-side
            # normalization at the provider boundary keeps every caller
            # source-compatible without per-schema edits across the
            # codebase. Recurses through nested objects + properties
            # + items.
            normalized_schema = _force_strict_object_schema(output_schema)
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "output",
                    "schema": normalized_schema,
                }
            }
        else:
            # Verbosity only meaningful for free-form text. Schema calls
            # already constrain output; setting verbosity AND format is
            # mutually exclusive on this endpoint.
            body["text"] = {"verbosity": "medium"}

        # Log actual request payload size for debugging API limits.
        # Wire-shape diagnostic: log whether prompt_cache_key (gated on
        # conversation_id) is present in the body. Empty key means the
        # caller didn't pass conversation_id and the backend will recompute
        # attention from scratch — the e50fb32 failure mode.
        _payload_bytes = len(json.dumps(body))
        _tool_count = len(body.get("tools", []))
        _tool_bytes = len(json.dumps(body.get("tools", []))) if body.get("tools") else 0
        _cache_key = body.get("prompt_cache_key", "")
        logger.info(
            "CODEX_REQUEST: payload=%dKB tools=%d tool_schemas=%dKB "
            "input_items=%d cache_key=%s",
            _payload_bytes // 1024, _tool_count, _tool_bytes // 1024,
            len(body.get("input", [])), _cache_key or "MISSING",
        )
        # Capture-on-large hook: when KERNOS_CODEX_CAPTURE_BODY=1 and
        # payload exceeds threshold, dump the exact body to disk so the
        # tipping-point probe can replay it verbatim. Investigative-only;
        # off by default. Accumulates one file per call (timestamped),
        # paired with scripts/diagnostics/codex_replay_mutations.py for A/B work.
        if (
            os.getenv("KERNOS_CODEX_CAPTURE_BODY", "0") == "1"
            and _payload_bytes >= int(os.getenv("KERNOS_CODEX_CAPTURE_THRESHOLD_KB", "40")) * 1024
        ):
            import tempfile
            import time as _time
            cap_dir = os.getenv("KERNOS_CODEX_CAPTURE_DIR", "/tmp/codex_bodies")
            os.makedirs(cap_dir, exist_ok=True)
            cap_path = os.path.join(
                cap_dir, f"body_{int(_time.time())}_{_payload_bytes // 1024}KB.json",
            )
            try:
                with open(cap_path, "w") as _f:
                    json.dump(body, _f)
                logger.info("CODEX_BODY_CAPTURED: path=%s", cap_path)
            except Exception:
                logger.exception("CODEX_BODY_CAPTURE_FAILED")

        # Last-payload hook: when KERNOS_CODEX_LAST_PAYLOAD=1, write the
        # EXACT body shipped to the model to a fixed "last payload" file
        # (replaced each call). Pairs with /dump's LAST OUTGOING PAYLOAD
        # section to give operators receipts for what the model literally
        # received — settles "did tool X reach the model" questions.
        #
        # Default filter: only capture MAIN reasoning calls (those that
        # carry a conversation_id). Utility calls — fact harvest,
        # MESSAGE_ANALYSIS classification, tool-surfacing decisions —
        # fire AFTER the main call in a turn and would otherwise
        # overwrite the receipt with something irrelevant (gpt-5.4-mini
        # / no tools / no cache_key body). Filtering on conversation_id
        # keeps the file aligned with the turn the operator cares about.
        # Set KERNOS_CODEX_LAST_PAYLOAD_ALL=1 to capture every call
        # regardless (useful when investigating utility-call shape).
        #
        # Distinct from KERNOS_CODEX_CAPTURE_BODY: that one is threshold-
        # gated, accumulates timestamped files, and pairs with
        # scripts/diagnostics/codex_replay_mutations.py for A/B work. This one
        # replaces a single file and pairs with /dump for receipts.
        if os.getenv("KERNOS_CODEX_LAST_PAYLOAD", "0") == "1":
            _capture_all = os.getenv("KERNOS_CODEX_LAST_PAYLOAD_ALL", "0") == "1"
            _is_main_call = bool(conversation_id)
            if _capture_all or _is_main_call:
                _data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
                _last_path = os.getenv(
                    "KERNOS_CODEX_LAST_PAYLOAD_PATH",
                    os.path.join(_data_dir, "diagnostics", "codex_last_payload.json"),
                )
                try:
                    os.makedirs(os.path.dirname(_last_path), exist_ok=True)
                    with open(_last_path, "w") as _f:
                        json.dump(body, _f, indent=2)
                except Exception:
                    logger.exception("CODEX_LAST_PAYLOAD_WRITE_FAILED")

        url = self._resolve_url()
        headers = self._headers(session_id=conversation_id)
        headers["accept"] = "text/event-stream"

        _max_retries = int(os.environ.get("KERNOS_CODEX_MAX_RETRIES", "3"))
        last_exc: Exception | None = None
        for attempt in range(_max_retries):
            try:
                async with http.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code == 401:
                        await resp.aread()
                        raise ReasoningProviderError(
                            f"Codex auth failed (401): {resp.text[:300]}"
                        )
                    if resp.status_code == 429:
                        await resp.aread()
                        raise ReasoningRateLimitError(
                            f"Codex rate limited (429): {resp.text[:300]}"
                        )
                    if resp.status_code >= 400:
                        await resp.aread()
                        raise ReasoningProviderError(
                            f"Codex API error ({resp.status_code}): {resp.text[:300]}"
                        )
                    data = await self._collect_sse_response(resp)
                return self._parse_response(data, _unskin)
            except (ReasoningRateLimitError, ReasoningProviderError):
                raise  # 4xx / known errors — don't retry
            except ReasoningTransientError as exc:
                last_exc = exc
                if self._trace:
                    self._trace.record("warning", "codex_provider", "CODEX_STREAM_ERROR",
                        str(exc)[:300], phase="reason")
                if attempt < _max_retries - 1:
                    _delay = min(1.5 * (1.5 ** attempt), 15.0)
                    logger.warning("REASON_RETRY: attempt=%d/%d delay=%.1fs transient=%s",
                        attempt + 2, _max_retries, _delay, str(exc)[:80])
                    if self._trace:
                        self._trace.record("warning", "codex_provider", "REASON_RETRY",
                            f"attempt={attempt + 2}/{_max_retries} delay={_delay:.1f}s", phase="reason")
                    await asyncio.sleep(_delay)
                    continue
                raise ReasoningProviderError(f"Codex transient error after {_max_retries} attempts: {exc}") from exc
            except Exception as exc:
                last_exc = exc
                if self._trace:
                    self._trace.record("error", "codex_provider", "CODEX_ERROR",
                        str(exc)[:300], phase="reason")
                if attempt < _max_retries - 1:
                    _delay = min(1.5 * (1.5 ** attempt), 15.0)
                    logger.warning("REASON_RETRY: attempt=%d/%d delay=%.1fs error=%s",
                        attempt + 2, _max_retries, _delay, str(exc)[:80])
                    await asyncio.sleep(_delay)
                    continue
                raise ReasoningConnectionError(f"Codex request failed after {_max_retries} attempts: {exc}") from exc

        raise ReasoningConnectionError(f"Codex request failed: {last_exc}") from last_exc

    @staticmethod
    async def _collect_sse_response(resp: Any) -> dict:
        """Read an SSE stream and return the final response object.

        Accumulates text from delta events during streaming, then merges
        into the final response if the completed event has empty output.
        """
        final_response: dict = {}
        buffer = ""

        # Accumulate streamed content: {output_index: {type, text, ...}}
        _streamed_items: dict[int, dict] = {}
        _streamed_text: dict[int, list[str]] = {}  # output_index → text chunks
        _streamed_fn_args: dict[str, list[str]] = {}  # call_id → argument chunks

        async for chunk in resp.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                idx = buffer.index("\n\n")
                block = buffer[:idx]
                buffer = buffer[idx + 2:]

                data_lines = [
                    line[5:].strip() for line in block.split("\n")
                    if line.startswith("data:")
                ]
                if not data_lines:
                    continue
                data_str = "\n".join(data_lines).strip()
                if not data_str or data_str == "[DONE]":
                    continue

                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")

                if event_type in ("response.completed", "response.done"):
                    final_response = event.get("response", event)

                # Accumulate output items and text deltas
                elif event_type == "response.output_item.added":
                    oi = event.get("output_index", 0)
                    item = event.get("item", {})
                    _streamed_items[oi] = item
                elif event_type == "response.output_text.delta":
                    oi = event.get("output_index", 0)
                    delta = event.get("delta", "")
                    if oi not in _streamed_text:
                        _streamed_text[oi] = []
                    _streamed_text[oi].append(delta)
                elif event_type == "response.output_item.done":
                    # Completed output item — may have full arguments
                    oi = event.get("output_index", 0)
                    item = event.get("item", {})
                    if item:
                        _streamed_items[oi] = item  # Overwrite with completed version
                elif event_type == "response.function_call_arguments.delta":
                    # Key by output_index (reliable) AND call_id/item_id (fallback)
                    oi = event.get("output_index", -1)
                    call_id = event.get("call_id", event.get("item_id", ""))
                    delta = event.get("delta", "")
                    # Use output_index as primary key for reconstruction
                    key = f"oi:{oi}" if oi >= 0 else call_id
                    if key not in _streamed_fn_args:
                        _streamed_fn_args[key] = []
                    _streamed_fn_args[key].append(delta)
                    # Also store by call_id for backwards compat
                    if call_id and call_id != key:
                        if call_id not in _streamed_fn_args:
                            _streamed_fn_args[call_id] = []
                        _streamed_fn_args[call_id].append(delta)

                elif event_type == "response.failed":
                    msg = ""
                    if "response" in event:
                        err = event["response"].get("error", {})
                        msg = err.get("message", "")
                    raise ReasoningProviderError(
                        f"Codex response failed: {msg or event_type}"
                    )
                elif event_type == "error":
                    err = event.get("error", {})
                    msg = err.get("message", event.get("message", event.get("code", "unknown")))
                    error_type = err.get("type", "")
                    logger.warning("CODEX_STREAM_ERROR: event=%s", json.dumps(event)[:500])
                    if error_type == "server_error":
                        raise ReasoningTransientError(f"Codex server error: {msg}")
                    raise ReasoningProviderError(f"Codex stream error: {msg}")

        # If final_response.output is empty but we accumulated streamed content,
        # reconstruct the output from deltas
        if final_response:
            output = final_response.get("output", [])
            if not output and (_streamed_text or _streamed_items or _streamed_fn_args):
                # Collect all output indices we know about
                all_indices = set()
                all_indices.update(_streamed_items.keys())
                all_indices.update(_streamed_text.keys())
                # Also extract indices from fn_args keys like "oi:0"
                for key in _streamed_fn_args:
                    if key.startswith("oi:"):
                        try:
                            all_indices.add(int(key[3:]))
                        except ValueError:
                            pass

                reconstructed = []
                for oi in sorted(all_indices):
                    item = dict(_streamed_items.get(oi, {}))
                    if oi in _streamed_text:
                        full_text = "".join(_streamed_text[oi])
                        if item.get("type") == "message":
                            item["content"] = [{"type": "output_text", "text": full_text}]
                        else:
                            item.setdefault("type", "output_text")
                            item["text"] = full_text
                    # Reconstruct function call arguments — try output_index first, then call_id
                    oi_key = f"oi:{oi}"
                    call_id = item.get("call_id", item.get("id", ""))
                    fn_args = _streamed_fn_args.get(oi_key) or _streamed_fn_args.get(call_id)
                    if fn_args:
                        item["arguments"] = "".join(fn_args)
                    reconstructed.append(item)
                final_response["output"] = reconstructed

        if not final_response:
            raise ReasoningProviderError("Codex stream ended without response.completed")

        return final_response
