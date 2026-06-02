"""Provider ABC + shared data types."""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ContentBlock:
    """A single content block from a provider response. Provider-agnostic."""

    type: str
    text: str | None = None
    name: str | None = None
    id: str | None = None
    input: dict | None = None


@dataclass
class ProviderResponse:
    """Provider response in KERNOS-native format."""

    content: list[ContentBlock]
    stop_reason: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class ChainEntry:
    """A single entry in a provider chain: which provider to call, with which model."""

    provider: "Provider"
    model: str


# Chain name → ordered list of entries to try. Standard keys: "primary",
# "lightweight". Legacy names "simple" / "cheap" are accepted as deprecation-
# aliased inputs by ReasoningService.complete_simple; both resolve to
# "lightweight" at dispatch time.
ChainConfig = dict[str, list[ChainEntry]]


class Provider(ABC):
    """Abstract LLM provider. Each implementation wraps a specific SDK."""

    @abstractmethod
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
        """Send a completion request and return a KERNOS-native response.

        system can be a plain string or a list of dicts with 'text' and optional
        'cache_control' keys.  When a list, the first entry is the stable/cacheable
        prefix, subsequent entries are dynamic per-turn content.  Providers that
        support prompt caching should apply cache_control to the static prefix.
        Providers that don't can concatenate all entries.

        conversation_id, when provided, lets providers correlate calls in the
        same conversation for backend prompt-cache hits and session routing.
        Providers that don't have a use for it can ignore it.

        tool_choice defaults to "auto" to preserve provider behavior. Callers
        that require a tool response, such as synthetic finalizer loops, pass
        "required"; providers translate that into their native shape where
        supported.
        """
        ...
