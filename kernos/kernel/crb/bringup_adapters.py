"""Production adapters bridging substrate dependencies to CRB ports.

WTC v1 C5c-bringup-crb. The CRBApprovalFlow + CRBProposalAuthor were
shipped against typed Protocols (``DraftReadPort``,
``STSRegistrationPort``, ``LLMClient``) so tests inject stubs.
Production wiring needs concrete adapters that:

* Restrict the substrate surface to the port's intended capability
  (CRB never gets the full DraftRegistry / SubstrateTools).
* Bridge call-shape differences (e.g. ProposalAuthor's ``LLMClient``
  contract vs ReasoningService's ``complete_simple`` signature).

Out of scope: wiring these into ``bring_up_substrate`` is a
follow-up step in the same commit; this module only defines the
adapters."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.drafts.registry import DraftRegistry, WorkflowDraft
    from kernos.kernel.reasoning import ReasoningService
    from kernos.kernel.substrate_tools.facade import SubstrateTools
    from kernos.kernel.workflows.workflow_registry import Workflow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLMClient adapter (CRBProposalAuthor)
# ---------------------------------------------------------------------------


# Pinned at construction. CRBProposalAuthor refuses any client whose
# ``temperature`` exceeds ``MAX_TEMPERATURE = 0.3`` (AC #6). The
# lightweight chain in production is configured for low-temperature
# stateless completions, so 0.2 is the contract this adapter pins.
_PROPOSAL_AUTHOR_TEMPERATURE = 0.2


class ReasoningLLMAdapter:
    """``CRBProposalAuthor.LLMClient`` over :class:`ReasoningService`.

    The author's contract is a stateless ``complete(prompt) -> str``
    plus a ``temperature`` declaration. ReasoningService's
    ``complete_simple`` is the kernel-side stateless surface;
    everything else (system prompt scaffold, token caps, structured
    output) lives in the author's templates.
    """

    def __init__(
        self,
        *,
        reasoning: "ReasoningService",
        max_tokens: int = 1024,
    ) -> None:
        self._reasoning = reasoning
        self._max_tokens = max_tokens

    @property
    def temperature(self) -> float:
        return _PROPOSAL_AUTHOR_TEMPERATURE

    async def complete(self, prompt: str) -> str:
        # The author embeds its templated scaffold in ``prompt``;
        # there's no separate system message at this layer. Empty
        # system_prompt is intentional — the author owns wording.
        return await self._reasoning.complete_simple(
            system_prompt="",
            user_content=prompt,
            max_tokens=self._max_tokens,
        )


# ---------------------------------------------------------------------------
# DraftReadPort adapter
# ---------------------------------------------------------------------------


class DraftRegistryReadAdapter:
    """``DraftReadPort`` facade over :class:`DraftRegistry`.

    DraftRegistry exposes write operations CRB must not call;
    structurally narrowing to ``get_draft`` enforces the read-only
    invariant the port documents.
    """

    def __init__(self, *, draft_registry: "DraftRegistry") -> None:
        self._registry = draft_registry

    async def get_draft(
        self,
        *,
        instance_id: str,
        draft_id: str,
    ) -> "WorkflowDraft | None":
        return await self._registry.get_draft(
            instance_id=instance_id,
            draft_id=draft_id,
        )


# ---------------------------------------------------------------------------
# STSRegistrationPort adapter
# ---------------------------------------------------------------------------


class SubstrateToolsSTSAdapter:
    """``STSRegistrationPort`` facade over :class:`SubstrateTools`.

    Forwards to the production approval-bound surface
    (``SubstrateTools.register_workflow``) without exposing the
    ``dry_run`` parameter — CRB is always production registration,
    never dry-run, so the port narrows the call shape.
    """

    def __init__(self, *, substrate_tools: "SubstrateTools") -> None:
        self._tools = substrate_tools

    async def register_workflow(
        self,
        *,
        instance_id: str,
        descriptor: dict,
        approval_event_id: str,
    ) -> "Workflow":
        result = await self._tools.register_workflow(
            instance_id=instance_id,
            descriptor=descriptor,
            approval_event_id=approval_event_id,
        )
        # SubstrateTools returns Workflow | DryRunResult; we never set
        # dry_run, so the result is always a Workflow. The cast is
        # implicit — type narrowing belongs to the caller's return
        # annotation.
        return result  # type: ignore[return-value]

    async def find_workflow_by_approval_event_id(
        self,
        *,
        instance_id: str,
        approval_event_id: str,
    ) -> "Workflow | None":
        return await self._tools.find_workflow_by_approval_event_id(
            instance_id=instance_id,
            approval_event_id=approval_event_id,
        )


__all__ = [
    "DraftRegistryReadAdapter",
    "ReasoningLLMAdapter",
    "SubstrateToolsSTSAdapter",
]
