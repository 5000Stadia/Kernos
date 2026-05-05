"""Production adapters bridging substrate dependencies to reference
primitive ports. REFERENCE-PRIMITIVE-V1 C-bringup.

Mirrors :mod:`kernos.kernel.crb.bringup_adapters` shape — the
cataloging cohort + the request_reference cohort navigator each
need a stateless ``complete(prompt) -> str`` LLM client. Production
wiring routes both through :class:`ReasoningService.complete_simple`
with ``prefer_cheap=True`` so the cheap chain is used.

The adapter narrows the substrate surface to the port's intended
capability (the cataloging cohort never gets the full
ReasoningService; only the cheap-tier completion entry point)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.reasoning import ReasoningService

logger = logging.getLogger(__name__)


# Cheap-tier temperature pin: low for deterministic one-line
# summaries / catalog navigation. The cheap chain is configured
# upstream; this adapter just declares its temperature contract for
# the LLM client interface.
_REFERENCE_TEMPERATURE = 0.1


class ReferenceCheapLLMAdapter:
    """Cheap-tier ``LLMClient`` over :class:`ReasoningService`.

    Used by both the cataloging cohort (one-line section
    descriptions, collection purpose summaries) and the
    request_reference cohort (catalog navigation). The contract is
    the same for both consumers."""

    def __init__(
        self,
        *,
        reasoning: "ReasoningService",
        max_tokens: int = 256,
    ) -> None:
        self._reasoning = reasoning
        self._max_tokens = max_tokens

    @property
    def temperature(self) -> float:
        return _REFERENCE_TEMPERATURE

    async def complete(self, prompt: str) -> str:
        return await self._reasoning.complete_simple(
            system_prompt="",
            user_content=prompt,
            max_tokens=self._max_tokens,
            prefer_cheap=True,
        )


__all__ = ["ReferenceCheapLLMAdapter"]
