"""SubstrateTools — production-path facade over Kernos substrate.

Future-composition invariant: STS exposes stable refs, capability summaries,
validation results, and approval-bound registration WITHOUT depending directly
on Canvas, domains, tools, agents, or unknown future systems. Canvas, domain
briefs, provider config, and future surfaces plug in as context/capability
providers behind neutral query APIs. STS is a small deterministic facade over
discoverable substrate; not a growing switchboard that learns every surface
by name. Reviewers of follow-on specs (Drafter cohort, CRB main, Canvas
integration, etc.) should reject changes that introduce direct subsystem
dependencies into STS.

C1 surface: read/query methods only — :meth:`list_known_providers`,
:meth:`list_agents`, :meth:`list_workflows`, :meth:`list_drafts`,
:meth:`query_context_brief`. C2 adds the approval-bound
:meth:`register_workflow` registration gate.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from kernos.kernel.substrate_tools.query.context_brief import (
    ContextBrief,
    ContextBriefRegistry,
    ContextRef,
)
from kernos.kernel.substrate_tools.query.list_agents import (
    list_agents as _list_agents,
)
from kernos.kernel.substrate_tools.query.list_drafts import (
    list_drafts as _list_drafts,
)
from kernos.kernel.substrate_tools.query.list_providers import (
    ProviderRecord,
    ProviderRegistry,
)
from kernos.kernel.substrate_tools.query.list_workflows import (
    list_workflows as _list_workflows,
)
from kernos.kernel.substrate_tools.registration.register import (
    register_workflow as _register_workflow,
)
from kernos.kernel.substrate_tools.registration.validation import DryRunResult

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.agents.registry import AgentRecord, AgentRegistry
    from kernos.kernel.drafts.registry import DraftRegistry, WorkflowDraft
    from kernos.kernel.triggers.runtime import TriggerEvaluationRuntime
    from kernos.kernel.workflows.workflow_registry import (
        Workflow,
        WorkflowRegistry,
    )


class SubstrateTools:
    """Cohort-facing facade over WDP/DAR/WLP/provider config.

    Construction wires the four substrate registries and the provider /
    context-brief registries. Engine bring-up:

    1. Construct DAR ``AgentRegistry``, WLP ``WorkflowRegistry``, WDP
       ``DraftRegistry``, all started as today.
    2. Construct STS ``ProviderRegistry`` and register one provider type
       per supported source (v1: ``"agent_inbox"`` aggregating DAR
       agents).
    3. Construct STS ``ContextBriefRegistry`` and register one resolver
       per supported ref type (v1: ``"space"``, ``"domain"``).
    4. Construct ``SubstrateTools(...)`` and hand it to cohorts.

    All query surfaces are deterministic, instance-scoped, and contain
    no LLM calls. ``register_workflow`` lands in C2 with the
    approval-binding gate.
    """

    def __init__(
        self,
        *,
        agent_registry: "AgentRegistry",
        workflow_registry: "WorkflowRegistry",
        draft_registry: "DraftRegistry",
        provider_registry: ProviderRegistry,
        context_brief_registry: ContextBriefRegistry,
        runtime: "TriggerEvaluationRuntime | None" = None,
    ) -> None:
        self._agent_registry = agent_registry
        self._workflow_registry = workflow_registry
        self._draft_registry = draft_registry
        self._provider_registry = provider_registry
        self._context_brief_registry = context_brief_registry
        # WTC v1 C5b: optional unified TriggerEvaluationRuntime.
        # When wired, register_workflow registers descriptor.triggers
        # atomically with the workflow. When None, trigger
        # registration is skipped — legacy callers continue working
        # without modification.
        self._runtime = runtime

    # === Query surfaces ===

    async def list_known_providers(
        self, *, instance_id: str,
    ) -> list[ProviderRecord]:
        return await self._provider_registry.list_all(instance_id=instance_id)

    async def list_agents(
        self, *, instance_id: str, status_filter: str | None = None,
    ) -> "list[AgentRecord]":
        return await _list_agents(
            self._agent_registry,
            instance_id=instance_id,
            status_filter=status_filter,
        )

    async def list_workflows(
        self,
        *,
        instance_id: str,
        status_filter: str | None = None,
        home_space_id: str | None = None,
    ) -> "list[Workflow]":
        return await _list_workflows(
            self._workflow_registry,
            instance_id=instance_id,
            status_filter=status_filter,
            home_space_id=home_space_id,
        )

    async def list_drafts(
        self,
        *,
        instance_id: str,
        status_filter: str | None = None,
        home_space_id: str | None = None,
        include_terminal: bool = False,
    ) -> "list[WorkflowDraft]":
        return await _list_drafts(
            self._draft_registry,
            instance_id=instance_id,
            status_filter=status_filter,
            home_space_id=home_space_id,
            include_terminal=include_terminal,
        )

    async def query_context_brief(
        self, *, instance_id: str, ref: ContextRef,
    ) -> ContextBrief | None:
        return await self._context_brief_registry.resolve(
            instance_id=instance_id, ref=ref,
        )

    async def find_workflow_by_approval_event_id(
        self,
        *,
        instance_id: str,
        approval_event_id: str,
    ) -> "Workflow | None":
        """Read-only lookup by ``(instance_id, approval_event_id)``.

        Used by CRB's crash-recovery sweep (CRB main spec) to determine
        whether STS has already registered a workflow against a given
        approval event, and to recover gracefully from
        :class:`ApprovalAlreadyConsumed` race conditions where a
        concurrent path beat us to registration.

        Returns ``None`` if no row matches — both for "approval not
        yet consumed" and for cross-instance queries.
        """
        return await self._workflow_registry.find_workflow_by_approval_event_id(
            instance_id=instance_id,
            approval_event_id=approval_event_id,
        )

    # === Registration gate ===

    async def register_workflow(
        self,
        *,
        instance_id: str,
        descriptor: dict,
        dry_run: bool = False,
        approval_event_id: str | None = None,
    ) -> "Workflow | DryRunResult":
        """Approval-bound workflow registration. See
        :func:`kernos.kernel.substrate_tools.registration.register.register_workflow`
        for the full 9-step validation flow.

        ``dry_run=True``: validates descriptor and returns
        :class:`DryRunResult`. No persistence, no event emission, no
        mutation. ``approval_event_id`` is ignored. Used by the
        Compiler at proposal time.

        ``dry_run=False``: REQUIRES ``approval_event_id``. Resolves the
        approval, validates envelope source authority + provenance +
        instance match + (for modifications) target binding, re-runs
        full descriptor validation, verifies hash match, then atomically
        persists the workflow + consumes the approval via the partial
        UNIQUE constraint on ``(instance_id, approval_event_id)``.
        """
        return await _register_workflow(
            instance_id=instance_id,
            descriptor=descriptor,
            workflow_registry=self._workflow_registry,
            agent_registry=self._agent_registry,
            runtime=self._runtime,
            dry_run=dry_run,
            approval_event_id=approval_event_id,
        )


__all__ = ["SubstrateTools"]
