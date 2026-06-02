"""Ollama provider — local or cloud models via Ollama's native API.

Local: http://localhost:11434/api/chat (no auth needed)
Cloud: https://ollama.com/api/chat (Bearer token auth via OLLAMA_API_KEY)

Supports tool/function calling with models that have it (Gemma 4, etc.).
"""
import asyncio
import json
import logging
import os
from typing import Any

from kernos.kernel.exceptions import (
    ReasoningConnectionError,
    ReasoningProviderError,
    ReasoningTransientError,
)
from kernos.providers.base import ContentBlock, Provider, ProviderResponse

logger = logging.getLogger(__name__)


class OllamaProvider(Provider):
    """Ollama provider via native /api/chat endpoint (local or cloud)."""

    provider_name = "ollama"

    def __init__(
        self,
        base_url: str = "",
        model: str = "",
        api_key: str = "",
    ) -> None:
        self._base_url = (
            base_url
            or os.getenv("OLLAMA_BASE_URL", "https://ollama.com")
        ).rstrip("/")
        self._api_key = api_key or os.getenv("OLLAMA_API_KEY", "")
        self.main_model = model or os.getenv("OLLAMA_MODEL", "gemma4:31b-cloud")
        # Two-tier: primary (main_model) + lightweight. Defaults to the same
        # model as primary — most local-Ollama users want one model
        # handling both tiers. OLLAMA_LIGHTWEIGHT_MODEL / legacy
        # OLLAMA_CHEAP_MODEL override when a distinct small model is configured.
        self.lightweight_model = (
            os.getenv("OLLAMA_LIGHTWEIGHT_MODEL")
            or os.getenv("OLLAMA_CHEAP_MODEL")
            or self.main_model
        )
        self.simple_model = os.getenv("OLLAMA_SIMPLE_MODEL", self.main_model)
        self.cheap_model = self.lightweight_model
        self._http: Any = None
        self.on_retry_notify: Any = None

    async def _ensure_http(self) -> Any:
        if self._http is None:
            import httpx
            self._http = httpx.AsyncClient(timeout=300.0)
        return self._http

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _build_messages(
        self, system: str | list[dict], messages: list[dict],
    ) -> list[dict]:
        """Build Ollama-format messages array with system prompt."""
        result: list[dict] = []

        # System prompt
        if isinstance(system, list):
            system_text = "\n\n".join(
                b.get("text", "") for b in system if b.get("text")
            )
        else:
            system_text = system
        if system_text:
            result.append({"role": "system", "content": system_text})

        # Conversation messages
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if isinstance(content, str):
                result.append({"role": role, "content": content})
                continue

            if isinstance(content, list):
                # Handle Anthropic-format content blocks
                text_parts = []
                tool_calls = []
                tool_results = []

                for block in content:
                    if isinstance(block, str):
                        text_parts.append(block)
                    elif isinstance(block, dict):
                        btype = block.get("type", "")
                        if btype == "text":
                            text_parts.append(block.get("text", ""))
                        elif btype == "tool_use":
                            tool_calls.append({
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": block.get("input", {}),
                                },
                            })
                        elif btype == "tool_result":
                            tool_results.append({
                                "role": "tool",
                                "content": block.get("content", ""),
                            })

                if text_parts or tool_calls:
                    entry: dict[str, Any] = {"role": role}
                    if text_parts:
                        entry["content"] = "\n".join(text_parts)
                    if tool_calls:
                        entry["tool_calls"] = tool_calls
                        if "content" not in entry:
                            entry["content"] = ""
                    result.append(entry)

                for tr in tool_results:
                    result.append(tr)

        return result

    @staticmethod
    def _build_tools(tools: list[dict]) -> list[dict]:
        """Convert Anthropic-format tools to Ollama function format."""
        result = []
        for t in tools:
            result.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            })
        return result

    def _parse_response(self, data: dict) -> ProviderResponse:
        """Parse Ollama /api/chat response into ProviderResponse."""
        message = data.get("message", {})

        content_blocks: list[ContentBlock] = []
        stop_reason = "end_turn"

        # Text content
        text = message.get("content", "")
        if text:
            content_blocks.append(ContentBlock(type="text", text=text))

        # Tool calls
        for tc in message.get("tool_calls", []):
            func = tc.get("function", {})
            args = func.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    args = {}
            content_blocks.append(ContentBlock(
                type="tool_use",
                name=func.get("name", ""),
                id=f"call_{func.get('name', '')}_{id(tc)}",
                input=args,
            ))
            stop_reason = "tool_use"

        if not content_blocks:
            content_blocks.append(ContentBlock(type="text", text=""))

        # Ollama returns token counts in different fields depending on version
        prompt_tokens = data.get("prompt_eval_count", 0)
        completion_tokens = data.get("eval_count", 0)

        return ProviderResponse(
            content=content_blocks,
            stop_reason=stop_reason,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
        )

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
        del conversation_id  # Ollama's API has no equivalent session/cache key.
        del tool_choice  # Ollama does not expose a compatible tool-choice knob.
        http = await self._ensure_http()

        ollama_messages = self._build_messages(system, messages)

        body: dict[str, Any] = {
            "model": model,
            "messages": ollama_messages,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
            },
        }

        if tools:
            body["tools"] = self._build_tools(tools)

        if output_schema:
            body["format"] = output_schema

        url = f"{self._base_url}/api/chat"

        _payload_bytes = len(json.dumps(body))
        logger.info("OLLAMA_REQUEST: url=%s model=%s payload=%dKB tools=%d messages=%d",
            url, model, _payload_bytes // 1024, len(tools), len(ollama_messages))

        _max_retries = int(os.getenv("KERNOS_OLLAMA_MAX_RETRIES", "5"))
        last_exc: Exception | None = None

        for attempt in range(_max_retries):
            try:
                resp = await http.post(url, json=body, headers=self._headers())

                if resp.status_code >= 500:
                    raise ReasoningTransientError(
                        f"Ollama server error ({resp.status_code}): {resp.text[:300]}"
                    )
                if resp.status_code >= 400:
                    raise ReasoningProviderError(
                        f"Ollama API error ({resp.status_code}): {resp.text[:300]}"
                    )

                data = resp.json()
                return self._parse_response(data)

            except ReasoningProviderError:
                raise
            except ReasoningTransientError as exc:
                last_exc = exc
                if attempt < _max_retries - 1:
                    _delay = min(2.0 * (2 ** attempt), 30.0)
                    logger.warning("OLLAMA_RETRY: attempt=%d/%d delay=%.1fs error=%s",
                        attempt + 2, _max_retries, _delay, str(exc)[:80])
                    await asyncio.sleep(_delay)
                    continue
                raise ReasoningProviderError(
                    f"Ollama error after {_max_retries} attempts: {exc}"
                ) from exc
            except Exception as exc:
                last_exc = exc
                if attempt < _max_retries - 1:
                    _delay = min(2.0 * (2 ** attempt), 30.0)
                    logger.warning("OLLAMA_RETRY: attempt=%d/%d delay=%.1fs error=%s",
                        attempt + 2, _max_retries, _delay, str(exc)[:80])
                    await asyncio.sleep(_delay)
                    continue
                raise ReasoningConnectionError(
                    f"Ollama request failed after {_max_retries} attempts: {exc}"
                ) from exc

        raise ReasoningConnectionError(f"Ollama request failed: {last_exc}") from last_exc
