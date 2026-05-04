"""KERNOS reasoning exception hierarchy.

The handler imports only these — never the provider SDK's own exceptions.
"""


class ReasoningError(Exception):
    """Base class for all reasoning service errors."""


class ReasoningTimeoutError(ReasoningError):
    """Provider call timed out."""


class ReasoningConnectionError(ReasoningError):
    """Could not connect to provider."""


class ReasoningRateLimitError(ReasoningError):
    """Provider rate limit exceeded."""


class ReasoningProviderError(ReasoningError):
    """API status error or unexpected provider error."""


class ReasoningTransientError(ReasoningError):
    """Transient server-side error that should be retried."""


class ChainPayloadTooLarge(ReasoningError):
    """Every chain entry's effective context window is smaller than the
    estimated payload, so no model in the chain can fit the request.

    Distinct from LLMChainExhausted: this fires before any model is
    called, and signals that the request itself is too big for the
    configured chain rather than that the upstream providers are
    unhappy. Handler should surface a clear "payload too large; trim
    or compact" message rather than a transient-error retry message.

    Attributes:
        chain_name:        The named chain that was tried.
        estimated_tokens:  The pre-flight estimate that triggered skips.
        largest_ceiling:   The largest effective_max_input_tokens
                           observed across the chain's entries (after
                           safety margin applied), or None if no entry
                           had a known ceiling.
        attempts:          Per-entry skip records:
                           (provider_name, model, "skipped: <reason>").
    """

    def __init__(
        self,
        chain_name: str,
        estimated_tokens: int,
        largest_ceiling: int | None,
        attempts: list[tuple[str, str, str]],
    ) -> None:
        self.chain_name = chain_name
        self.estimated_tokens = int(estimated_tokens)
        self.largest_ceiling = largest_ceiling
        self.attempts = list(attempts)
        ceiling_str = (
            f"{largest_ceiling}" if largest_ceiling is not None else "unknown"
        )
        super().__init__(
            f"Chain '{chain_name}' cannot fit payload: "
            f"estimated {estimated_tokens} input tokens, "
            f"largest available ceiling {ceiling_str}. "
            "Trim the payload or run a compaction pass."
        )


class LLMChainExhausted(ReasoningError):
    """Every provider in a named chain failed on this turn.

    Raised by ``build_resilient_chain_caller`` (kernos.kernel.turn_runner_provider) after exhausting the chain.
    The handler catches this exception specifically and delivers a
    pre-rendered failure message to the user via the platform adapter,
    *instead of* an LLM reply — on turns where this exception fires, the
    agent never produces an LLM response at all.

    Attributes:
        chain_name: The named chain that failed (e.g. "primary").
        attempts:   A list of ``(provider_name, model, reason)`` tuples,
                    one per entry in the chain. Useful for diagnostics
                    and for the pre-rendered message.
    """

    def __init__(
        self,
        chain_name: str,
        attempts: list[tuple[str, str, str]],
    ) -> None:
        self.chain_name = chain_name
        self.attempts = list(attempts)
        summary = ", ".join(
            f"{p}/{m} ({r[:80]})" for p, m, r in self.attempts
        ) or "no entries"
        super().__init__(
            f"Chain '{chain_name}' exhausted: {summary}"
        )
