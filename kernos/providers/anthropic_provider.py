"""Anthropic SDK provider implementation."""
import os
from typing import Any

import anthropic

from kernos.kernel.exceptions import (
    ReasoningConnectionError,
    ReasoningProviderError,
    ReasoningRateLimitError,
    ReasoningTimeoutError,
)
from kernos.providers.base import ContentBlock, Provider, ProviderResponse

_MAIN_MODEL = "claude-sonnet-4-6"
_LIGHTWEIGHT_MODEL = "claude-haiku-4-5-20251001"


class AnthropicProvider(Provider):
    """Wraps the Anthropic SDK. Maps SDK exceptions to KERNOS exceptions."""

    provider_name = "anthropic"
    main_model = _MAIN_MODEL
    #: Two-tier chain: primary (main_model) + lightweight. Legacy
    #: ``simple_model`` / ``cheap_model`` attributes retained as aliases
    #: for any external reader that still reaches for them; new code
    #: uses ``lightweight_model``.
    lightweight_model = _LIGHTWEIGHT_MODEL
    simple_model = _MAIN_MODEL      # legacy alias — simple used to default to main
    cheap_model = _LIGHTWEIGHT_MODEL  # legacy alias

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

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
        # conversation_id unused: Anthropic's prompt caching is keyed on the
        # static system-prompt prefix, not a session token.
        del conversation_id
        # Apply prompt caching with cache boundary support.
        # If system is a list of dicts (static + dynamic split), apply
        # cache_control only to the static prefix for real cache hits.
        # If system is a plain string, cache the whole thing (legacy path).
        if isinstance(system, list):
            cached_system = []
            for i, block in enumerate(system):
                entry: dict = {"type": "text", "text": block.get("text", "")}
                if block.get("cache_control"):
                    entry["cache_control"] = block["cache_control"]
                cached_system.append(entry)
        else:
            cached_system = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        cached_tools = list(tools) if tools else []
        if cached_tools:
            cached_tools[-1] = {
                **cached_tools[-1],
                "cache_control": {"type": "ephemeral"},
            }

        create_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": cached_system,
            "messages": messages,
        }
        if cached_tools:
            create_kwargs["tools"] = cached_tools
            if tool_choice == "required":
                create_kwargs["tool_choice"] = {"type": "any"}
        if output_schema:
            create_kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": output_schema}
            }

        try:
            # Use streaming to avoid SDK timeout for large max_tokens values
            async with self._client.messages.stream(**create_kwargs) as stream:
                response = await stream.get_final_message()
        except anthropic.APITimeoutError as exc:
            raise ReasoningTimeoutError(str(exc)) from exc
        except anthropic.APIConnectionError as exc:
            raise ReasoningConnectionError(str(exc)) from exc
        except anthropic.RateLimitError as exc:
            raise ReasoningRateLimitError(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            raise ReasoningProviderError(f"API status {exc.status_code}: {exc}") from exc
        except Exception as exc:
            raise ReasoningProviderError(str(exc)) from exc

        content = [
            ContentBlock(
                type=block.type,
                text=getattr(block, "text", None),
                name=getattr(block, "name", None),
                id=getattr(block, "id", None),
                input=getattr(block, "input", None),
            )
            for block in response.content
        ]

        cache_write = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0

        return ProviderResponse(
            content=content,
            stop_reason=response.stop_reason,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_creation_input_tokens=cache_write,
            cache_read_input_tokens=cache_read,
        )
