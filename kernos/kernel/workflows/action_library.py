"""Workflow action library — bounded set of verbs.

WORKFLOW-LOOP-PRIMITIVE C4. Each verb wraps an existing Kernos
surface; no verb invents new world-effect machinery. The verbs split
into two classes per the spec's verb-split invariant:

**World-effect verbs (action-loop instances).** These actually change
state in the world. Each has an ``execute`` side-effect path and a
``verify`` intent-satisfaction check. Covenant-gated: a configured
covenant_gate callable is consulted before execute; denied gates
short-circuit to ``ActionResult(success=False)``.

  * ``notify_user`` — wraps presence/adapter delivery
  * ``write_canvas`` — wraps canvas write surface
  * ``route_to_agent`` — writes to the configured AgentInbox
    provider; FAILS LOUDLY when no provider is bound
  * ``call_tool`` — wraps the existing tool dispatch primitive;
    verifier is the wrapped tool's own declared verifier
  * ``post_to_service`` — wraps the workshop service registry
  * ``request_approval`` — creates a durable operator approval receipt

**Direct-effect verbs (structural assertions, NOT action-loop
instances).** These mutate internal state only and have a structural
assertion in lieu of an LLM-judged verifier — per the
ACTION-LOOP-PRIMITIVE Anti-Goal of not adding LLM verification to
deterministic operations.

  * ``mark_state`` — versioned internal-state mutation
  * ``append_to_ledger`` — append-only ledger entry

Provider independence: this module MUST NOT reference any specific
inbox backend (URLs, tool names, vendor-specific APIs). The
``route_to_agent`` verb goes through the AgentInbox Protocol; only
the concrete inbox implementations in ``agent_inbox.py`` may carry
backend-specific names. Structural test scans this file for backend
URL fragments and tool-namespace patterns.

Bounded set in v1. New verbs require a separate spec extending the
library — preserves covenant gating, keeps the action surface
auditable.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

from kernos.kernel.agents.providers import (
    ProviderKeyUnknown,
    ProviderRegistry,
)
from kernos.kernel.agents.registry import (
    AgentInboxProviderUnavailable,
    AgentNotRegistered,
    AgentPaused,
    AgentRegistry,
    AgentRetired,
)
from kernos.kernel.workflows.agent_inbox import (
    AgentInbox,
    AgentInboxUnavailable,
)

logger = logging.getLogger(__name__)


# A covenant gate decides whether a world-effect verb may execute.
# Returns True to permit, False to deny. The engine in C5 injects a
# real evaluator that consults the covenant cohort with the
# synthetic safety context. Tests inject stubs.
CovenantGate = Callable[[Any, str, dict], Awaitable[bool] | bool]


@dataclass
class ActionResult:
    """Uniform return shape for verb execution. Verifier reads
    ``success`` and (for world-effect verbs) cross-checks the
    receipt against the wrapped surface to confirm
    intent-satisfaction."""

    success: bool
    value: Any = None
    error: str | None = None
    receipt: dict = field(default_factory=dict)


class Action(Protocol):
    """Each action verb satisfies this Protocol."""

    action_type: str

    async def execute(self, context: Any, params: dict) -> ActionResult: ...

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_covenant(
    gate: CovenantGate | None, context: Any, action_type: str, params: dict,
) -> bool:
    """Resolve the covenant gate. ``None`` means permit. Async + sync
    callables both supported."""
    if gate is None:
        return True
    out = gate(context, action_type, params)
    if asyncio.iscoroutine(out):
        return await out  # type: ignore[no-any-return]
    return bool(out)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# World-effect verbs
# ---------------------------------------------------------------------------


class NotifyUserAction:
    """Deliver a message to a channel via the presence/adapter
    surface. The wrapped delivery callable must return a truthy
    receipt that the verifier checks for ``persisted`` semantics."""

    action_type = "notify_user"

    def __init__(
        self,
        deliver_fn: Callable[..., Awaitable[Any]],
        *,
        covenant_gate: CovenantGate | None = None,
    ) -> None:
        self._deliver = deliver_fn
        self._covenant_gate = covenant_gate

    async def execute(self, context: Any, params: dict) -> ActionResult:
        if not await _resolve_covenant(
            self._covenant_gate, context, self.action_type, params,
        ):
            return ActionResult(success=False, error="covenant_denied")
        try:
            receipt = await self._deliver(
                channel=params["channel"],
                message=params["message"],
                urgency=params.get("urgency", "normal"),
                instance_id=getattr(context, "instance_id", ""),
                member_id=getattr(context, "member_id", ""),
            )
        except KeyError as exc:
            return ActionResult(success=False, error=f"missing_param:{exc.args[0]}")
        except Exception as exc:
            return ActionResult(success=False, error=f"deliver_failed:{exc}")
        return ActionResult(
            success=True,
            value=receipt,
            receipt={"delivered_at": _now()},
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        # Read-after-write: the wrapped delivery surface returned a
        # receipt; verify the receipt is truthy and that the action
        # marked itself successful. The full read-back-from-channel
        # check is integration-test territory; the unit verifier
        # confirms we didn't silently fail.
        return result.success and bool(result.value)


class WriteCanvasAction:
    """Wraps the existing canvas write surface. ``append`` mode is
    reversible; ``replace`` mode is irreversible (per
    action_classification)."""

    action_type = "write_canvas"

    def __init__(
        self,
        canvas_write_fn: Callable[..., Awaitable[Any]],
        canvas_read_fn: Callable[..., Awaitable[str]],
        *,
        covenant_gate: CovenantGate | None = None,
    ) -> None:
        self._write = canvas_write_fn
        self._read = canvas_read_fn
        self._covenant_gate = covenant_gate

    async def execute(self, context: Any, params: dict) -> ActionResult:
        if not await _resolve_covenant(
            self._covenant_gate, context, self.action_type, params,
        ):
            return ActionResult(success=False, error="covenant_denied")
        canvas_id = params["canvas_id"]
        content = params["content"]
        mode = params.get("append_or_replace", "append")
        try:
            await self._write(
                canvas_id=canvas_id,
                content=content,
                mode=mode,
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception as exc:
            return ActionResult(success=False, error=f"canvas_write_failed:{exc}")
        return ActionResult(
            success=True,
            receipt={"canvas_id": canvas_id, "mode": mode, "wrote_at": _now()},
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        if not result.success:
            return False
        try:
            current = await self._read(
                canvas_id=params["canvas_id"],
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception:
            return False
        # Read-after-write check: replace → exact content; append →
        # content visible somewhere in the current state.
        mode = params.get("append_or_replace", "append")
        if mode == "replace":
            return current == params["content"]
        return params["content"] in current


class RouteToAgentAction:
    """Posts a payload to an AgentInbox resolved through the
    AgentRegistry.

    DOMAIN-AGENT-REGISTRY refactor: the verb takes an
    ``AgentRegistry`` at construction (instead of a single bound
    inbox). At dispatch, ``execute()`` calls
    ``registry.get_by_id(agent_id, instance_id)`` to resolve the
    record, looks up the agent's ``provider_key`` in the
    registry's bound ProviderRegistry, constructs the concrete
    inbox via the factory, and posts the payload. Typed errors
    flow up per AC #10:

      - agent_id not in registry → ``AgentNotRegistered``
      - record.status == "paused" → ``AgentPaused``
      - record.status == "retired" → ``AgentRetired``
      - provider_key not in ProviderRegistry →
        ``AgentInboxProviderUnavailable``
      - registry not bound at construction → ``AgentInboxUnavailable``
        (legacy WLP error preserved for the "engine not configured
        with the registry" case)

    Verifier reads the receipt SNAPSHOT (provider_key,
    provider_config_ref, persisted_id), reconstructs the same
    inbox via the ProviderRegistry factory, and re-reads to
    confirm the post landed. Does NOT consult the registry —
    immune to mid-flight registry mutations and lifecycle
    transitions (AC #11).

    Legacy ``inbox=`` constructor parameter is preserved for the
    pre-DAR code path (a few unit tests still use it directly);
    new code passes ``registry=``. Mutually exclusive — passing
    both raises ValueError.
    """

    action_type = "route_to_agent"

    def __init__(
        self,
        inbox: AgentInbox | None = None,
        *,
        registry: AgentRegistry | None = None,
        covenant_gate: CovenantGate | None = None,
    ) -> None:
        if inbox is not None and registry is not None:
            raise ValueError(
                "RouteToAgentAction takes either inbox= (legacy) or "
                "registry= (DAR) — not both"
            )
        self._inbox = inbox
        self._registry = registry
        self._covenant_gate = covenant_gate

    async def execute(self, context: Any, params: dict) -> ActionResult:
        # Legacy path: single-inbox constructor.
        if self._inbox is not None:
            return await self._execute_legacy_inbox(context, params)
        # DAR path: registry-resolved inbox.
        if self._registry is None:
            raise AgentInboxUnavailable(
                "route_to_agent invoked but neither inbox= nor "
                "registry= was bound at construction"
            )
        if not await _resolve_covenant(
            self._covenant_gate, context, self.action_type, params,
        ):
            return ActionResult(success=False, error="covenant_denied")
        instance_id = getattr(context, "instance_id", "")
        agent_id = params["agent_id"]
        record = await self._registry.get_by_id(agent_id, instance_id)
        if record is None:
            raise AgentNotRegistered(agent_id, instance_id)
        if record.status == "paused":
            raise AgentPaused(agent_id)
        if record.status == "retired":
            raise AgentRetired(agent_id)
        provider_registry = self._registry.provider_registry
        if provider_registry is None:
            raise AgentInboxProviderUnavailable(agent_id, record.provider_key)
        try:
            inbox = provider_registry.construct(
                record.provider_key, record.provider_config_ref,
            )
        except ProviderKeyUnknown as exc:
            raise AgentInboxProviderUnavailable(
                agent_id, record.provider_key,
            ) from exc
        try:
            receipt = await inbox.post(
                agent_id=agent_id,
                payload=params["payload"],
                instance_id=instance_id,
            )
        except Exception as exc:
            return ActionResult(
                success=False, error=f"inbox_post_failed:{exc}",
            )
        # AC #11: snapshot the resolved provider data into the
        # receipt so verify() can reconstruct the inbox without
        # touching the registry.
        return ActionResult(
            success=True,
            value=receipt,
            receipt={
                "agent_id": agent_id,
                "provider_key": record.provider_key,
                "provider_config_ref": record.provider_config_ref,
                "persisted_id": receipt.persisted_id,
            },
        )

    async def _execute_legacy_inbox(
        self, context: Any, params: dict,
    ) -> ActionResult:
        """Pre-DAR code path. Kept so existing WLP tests that bind
        a single inbox at construction continue to work without
        churn. New code uses registry= instead."""
        if self._inbox is None:
            raise AgentInboxUnavailable(
                "route_to_agent invoked but no AgentInbox provider is "
                "configured. Bind a concrete inbox provider at action "
                "library construction time."
            )
        if not await _resolve_covenant(
            self._covenant_gate, context, self.action_type, params,
        ):
            return ActionResult(success=False, error="covenant_denied")
        try:
            receipt = await self._inbox.post(
                agent_id=params["agent_id"],
                payload=params["payload"],
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception as exc:
            return ActionResult(success=False, error=f"inbox_post_failed:{exc}")
        return ActionResult(
            success=True,
            value=receipt,
            receipt={"persisted_id": receipt.persisted_id},
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        if not result.success:
            return False
        # Legacy path preserved.
        if self._inbox is not None:
            return await self._verify_legacy_inbox(context, params, result)
        # DAR path: receipt-snapshot insulated verify (AC #11).
        if self._registry is None:
            return False
        provider_registry = self._registry.provider_registry
        if provider_registry is None:
            return False
        provider_key = result.receipt.get("provider_key")
        provider_config_ref = result.receipt.get("provider_config_ref", "")
        persisted_id = result.receipt.get("persisted_id")
        agent_id = result.receipt.get("agent_id") or params.get("agent_id")
        if not provider_key or not persisted_id or not agent_id:
            return False
        try:
            inbox = provider_registry.construct(
                provider_key, provider_config_ref,
            )
        except ProviderKeyUnknown:
            return False
        try:
            items = await inbox.read(
                agent_id=agent_id,
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception:
            return False
        return any(i.persisted_id == persisted_id for i in items)

    async def _verify_legacy_inbox(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        if self._inbox is None:
            return False
        try:
            items = await self._inbox.read(
                agent_id=params["agent_id"],
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception:
            return False
        target_id = result.receipt.get("persisted_id")
        return any(i.persisted_id == target_id for i in items)


class CallToolAction:
    """Wraps the existing tool dispatch primitive. The verifier
    delegates to the tool's own declared verifier — this verb does
    NOT redefine tool verification."""

    action_type = "call_tool"

    def __init__(
        self,
        tool_dispatch_fn: Callable[..., Awaitable[Any]],
        *,
        tool_verifier_fn: Callable[..., Awaitable[bool]] | None = None,
        covenant_gate: CovenantGate | None = None,
    ) -> None:
        self._dispatch = tool_dispatch_fn
        self._tool_verifier = tool_verifier_fn
        self._covenant_gate = covenant_gate

    async def execute(self, context: Any, params: dict) -> ActionResult:
        if not await _resolve_covenant(
            self._covenant_gate, context, self.action_type, params,
        ):
            return ActionResult(success=False, error="covenant_denied")
        try:
            result_value = await self._dispatch(
                tool_id=params["tool_id"],
                args=params.get("args") or {},
                instance_id=getattr(context, "instance_id", ""),
                member_id=getattr(context, "member_id", ""),
            )
        except Exception as exc:
            return ActionResult(success=False, error=f"tool_dispatch_failed:{exc}")
        return ActionResult(
            success=True,
            value=result_value,
            receipt={"tool_id": params["tool_id"], "called_at": _now()},
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        if not result.success:
            return False
        if self._tool_verifier is None:
            # No tool-specific verifier configured — fall back to the
            # success bit. The wrapped surface is responsible for
            # raising rather than returning falsely-successful values.
            return True
        return await self._tool_verifier(
            tool_id=params["tool_id"], args=params.get("args") or {},
            value=result.value, context=context,
        )


class PostToServiceAction:
    """Wraps the workshop service registry. Each service declares its
    own verifier; this verb's verify() delegates."""

    action_type = "post_to_service"

    def __init__(
        self,
        service_post_fn: Callable[..., Awaitable[Any]],
        *,
        service_verifier_fn: Callable[..., Awaitable[bool]] | None = None,
        covenant_gate: CovenantGate | None = None,
    ) -> None:
        self._post = service_post_fn
        self._service_verifier = service_verifier_fn
        self._covenant_gate = covenant_gate

    async def execute(self, context: Any, params: dict) -> ActionResult:
        if not await _resolve_covenant(
            self._covenant_gate, context, self.action_type, params,
        ):
            return ActionResult(success=False, error="covenant_denied")
        try:
            value = await self._post(
                service_id=params["service_id"],
                payload=params.get("payload") or {},
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception as exc:
            return ActionResult(success=False, error=f"service_post_failed:{exc}")
        return ActionResult(
            success=True,
            value=value,
            receipt={"service_id": params["service_id"], "posted_at": _now()},
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        if not result.success:
            return False
        if self._service_verifier is None:
            return True
        return await self._service_verifier(
            service_id=params["service_id"],
            payload=params.get("payload") or {},
            value=result.value, context=context,
        )


# ---------------------------------------------------------------------------
# Direct-effect verbs (structural assertions only)
# ---------------------------------------------------------------------------


class MarkStateAction:
    """Internal state mutation, scoped to instance/member/space/workflow.

    NOT an action-loop. Per ACTION-LOOP-PRIMITIVE Anti-Goal:
    "do not add LLM verification to deterministic operations." The
    structural assertion is "post-mutation read returns the new
    value" — checked by ``verify`` reading the same key back.

    Mutations are versioned per the standing no-destructive-deletes
    principle: each call appends a new entry rather than overwriting.
    The state_store is responsible for the versioning shape.
    """

    action_type = "mark_state"

    def __init__(
        self,
        state_store_set: Callable[..., Awaitable[Any]],
        state_store_get: Callable[..., Awaitable[Any]],
    ) -> None:
        self._set = state_store_set
        self._get = state_store_get

    async def execute(self, context: Any, params: dict) -> ActionResult:
        try:
            await self._set(
                key=params["key"],
                value=params["value"],
                scope=params.get("scope", "instance"),
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception as exc:
            return ActionResult(success=False, error=f"state_set_failed:{exc}")
        return ActionResult(
            success=True,
            receipt={
                "key": params["key"], "scope": params.get("scope", "instance"),
                "set_at": _now(),
            },
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        if not result.success:
            return False
        try:
            current = await self._get(
                key=params["key"],
                scope=params.get("scope", "instance"),
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception:
            return False
        return current == params["value"]


class AppendToLedgerAction:
    """Append a synopsis entry to a workflow's ledger. NOT an
    action-loop — structural assertion is "ledger's last entry
    matches the appended record."

    The ledger surface itself is owned by C5's WorkflowExecution
    layer; this verb only carries the call. Tests inject a stub
    ledger.
    """

    action_type = "append_to_ledger"

    def __init__(
        self,
        ledger_append_fn: Callable[..., Awaitable[Any]],
        ledger_read_last_fn: Callable[..., Awaitable[dict | None]],
    ) -> None:
        self._append = ledger_append_fn
        self._read_last = ledger_read_last_fn

    async def execute(self, context: Any, params: dict) -> ActionResult:
        try:
            await self._append(
                workflow_id=params["workflow_id"],
                entry=params["entry"],
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception as exc:
            return ActionResult(success=False, error=f"ledger_append_failed:{exc}")
        return ActionResult(
            success=True,
            receipt={
                "workflow_id": params["workflow_id"],
                "appended_at": _now(),
            },
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        if not result.success:
            return False
        try:
            last = await self._read_last(
                workflow_id=params["workflow_id"],
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception:
            return False
        if last is None:
            return False
        # Codex doc-batch review: production ledger writers
        # (e.g. WorkflowLedger) inject a `logged_at` timestamp into
        # every appended entry, so a raw equality check against the
        # caller's original entry would always fail in production.
        # Verify by checking that every key/value the caller wrote is
        # present in the read-back record — extra writer-injected
        # fields (logged_at, future audit metadata) don't fail the
        # check.
        if not isinstance(last, dict) or not isinstance(params["entry"], dict):
            return last == params["entry"]
        return all(last.get(k) == v for k, v in params["entry"].items())


class RequestApprovalAction:
    """Create an approval receipt bound to the current workflow gate.

    The action does not pause the workflow by itself. The surrounding
    action descriptor's ``gate_ref`` drives the existing engine gate
    flow; this verb only creates the durable receipt carrying the
    execution/gate binding.
    """

    action_type = "request_approval"

    def __init__(
        self,
        request_approval_fn: Callable[..., Awaitable[str]],
        *,
        covenant_gate: CovenantGate | None = None,
    ) -> None:
        self._request_approval = request_approval_fn
        self._covenant_gate = covenant_gate

    async def execute(self, context: Any, params: dict) -> ActionResult:
        if not await _resolve_covenant(
            self._covenant_gate, context, self.action_type, params,
        ):
            return ActionResult(success=False, error="covenant_denied")

        binding_payload = params.get("binding_payload")
        if binding_payload is None:
            binding_payload = {}
        if not isinstance(binding_payload, dict):
            return ActionResult(
                success=False,
                error="invalid_binding_payload:not_a_mapping",
            )
        try:
            json.dumps(binding_payload)
        except (TypeError, ValueError) as exc:
            return ActionResult(
                success=False,
                error=f"invalid_binding_payload:{exc}",
            )

        try:
            kind = params["kind"]
            operator_actor_id = params["operator_actor_id"]
            request_summary = params["request_summary"]
        except KeyError as exc:
            return ActionResult(
                success=False, error=f"missing_param:{exc.args[0]}",
            )
        for field_name, value in (
            ("kind", kind),
            ("operator_actor_id", operator_actor_id),
            ("request_summary", request_summary),
        ):
            if not isinstance(value, str) or not value:
                return ActionResult(
                    success=False, error=f"invalid_param:{field_name}",
                )

        workflow_execution_id = params.get("_workflow_execution_id")
        gate_nonce = params.get("_gate_nonce")
        if not (workflow_execution_id and gate_nonce):
            return ActionResult(
                success=False, error="missing_workflow_binding",
            )

        try:
            approval_id = await self._request_approval(
                instance_id=getattr(context, "instance_id", ""),
                kind=kind,
                requested_for_actor=params.get(
                    "requested_for_actor",
                    getattr(context, "member_id", ""),
                ),
                operator_actor_id=operator_actor_id,
                request_summary=request_summary,
                binding_payload=binding_payload,
                workflow_execution_id=workflow_execution_id,
                gate_nonce=gate_nonce,
                ttl_seconds=params.get("ttl_seconds", 86400),
                single_use=params.get("single_use", True),
            )
        except Exception as exc:
            return ActionResult(
                success=False, error=f"approval_request_failed:{exc}",
            )
        return ActionResult(
            success=True,
            value={"approval_id": approval_id},
            receipt={
                "approval_id": approval_id,
                "kind": kind,
                "requested_at": _now(),
            },
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        return bool(
            result.success
            and result.value
            and result.value.get("approval_id")
        )

# ---------------------------------------------------------------------------
# Control-flow verbs (WORKFLOW-ORCHESTRATION-PRIMITIVES-V1)
# ---------------------------------------------------------------------------


class BranchAction:
    """Conditional control flow at workflow level.

    WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 5 (revised v2 per
    Codex round-1 Blocker 2 + High 6). Goto semantics: routes to
    one of two named step IDs (branch_on_true / branch_on_false).
    The engine continues linearly from the chosen target — to
    produce skip-downstream semantics, pair branch with
    terminal_branches so one outcome routes into a terminal branch
    that runs to its end without falling back to main sequence.

    Codex round-1 High 6: condition MUST be a native bool. The
    engine's reference resolver preserves native types via the
    sole-reference shortcut, so `{step.X.output.approved}` where
    `approved` is True resolves to Python True (not the string
    "True"). Non-bool condition values surface as
    ``branch_condition_not_bool`` failure and route through
    continuation_rules.
    """

    action_type = "branch"

    def __init__(self) -> None:
        # Branch is pure control-flow; no external deps.
        pass

    async def execute(self, context: Any, params: dict) -> ActionResult:
        condition_value = params.get("condition")
        if not isinstance(condition_value, bool):
            return ActionResult(
                success=False,
                error=(
                    f"branch_condition_not_bool:"
                    f"got {type(condition_value).__name__}={condition_value!r}"
                ),
            )
        target_step_id = (
            params["branch_on_true"] if condition_value
            else params["branch_on_false"]
        )
        return ActionResult(
            success=True,
            value={
                "condition_resolved_to": condition_value,
                "target_step_id": target_step_id,
            },
            receipt={
                "branched_to": target_step_id,
                "condition_value": condition_value,
            },
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        # Branch verb has no world-effect to verify. The strict-bool
        # check inside execute() means a False success result is a
        # real failure that routes through continuation_rules.
        return result.success


# ---------------------------------------------------------------------------
# Library registry
# ---------------------------------------------------------------------------


class ActionLibrary:
    """Registry mapping action_type → Action instance. The execution
    engine looks up verbs by type and dispatches."""

    def __init__(self) -> None:
        self._verbs: dict[str, Action] = {}

    def register(self, action: Action) -> None:
        if action.action_type in self._verbs:
            raise ValueError(
                f"action_type {action.action_type!r} already registered"
            )
        self._verbs[action.action_type] = action

    def get(self, action_type: str) -> Action:
        if action_type not in self._verbs:
            raise KeyError(f"action_type {action_type!r} not registered")
        return self._verbs[action_type]

    def has(self, action_type: str) -> bool:
        return action_type in self._verbs

    def registered_types(self) -> tuple[str, ...]:
        return tuple(self._verbs.keys())


__all__ = [
    "Action",
    "ActionLibrary",
    "ActionResult",
    "AppendToLedgerAction",
    "BranchAction",
    "CallToolAction",
    "CovenantGate",
    "MarkStateAction",
    "NotifyUserAction",
    "PostToServiceAction",
    "RequestApprovalAction",
    "RouteToAgentAction",
    "WriteCanvasAction",
]
