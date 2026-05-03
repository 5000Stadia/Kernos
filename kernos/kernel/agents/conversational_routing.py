"""Thin conversational-routing consumer of AgentRegistry.resolve_natural.

DOMAIN-AGENT-REGISTRY C4. The message handler / cohort layer
decides whether a user message contains a routing intent (e.g. "ask
the spec agent to draft this", "send to my code reviewer"); the
registry only resolves once asked. This module is the small
adapter between "we have a phrase + scope" and "here's a routing
decision."

Per the design review edit v1 → v2: the registry does NOT own intent detection.
This module mirrors that boundary — it doesn't decide WHEN to
route, only what to do once the handler has decided to ask.

Three decision shapes the handler can act on:

  - DispatchTo(record): handler routes the relevant content to
    this agent's inbox via the same RouteToAgentAction path
    workflows use.
  - AskClarification(candidates): handler responds with a
    clarification question naming each candidate by display_name
    and domain_summary.
  - Unknown(known_agents): handler responds with "I don't
    recognize that agent — here are the agents I know about" with
    the active records to choose from.
"""
from __future__ import annotations

from dataclasses import dataclass

from kernos.kernel.agents.registry import (
    AgentRecord,
    AgentRegistry,
    Ambiguity,
    Match,
    NotFound,
)


@dataclass(frozen=True)
class DispatchTo:
    """The handler should route the relevant content to this
    agent's inbox."""
    record: AgentRecord


@dataclass(frozen=True)
class AskClarification:
    """The handler should ask the user which candidate they
    meant. ``candidates`` carries enough info (display_name,
    domain_summary) for the handler to produce a friendly
    clarification question."""
    candidates: tuple[AgentRecord, ...]


@dataclass(frozen=True)
class Unknown:
    """No match. Handler may respond with 'agents I know'
    listing using ``known_agents``. May be empty if the instance
    has no active agents at all."""
    known_agents: tuple[AgentRecord, ...]


RoutingDecision = "DispatchTo | AskClarification | Unknown"


async def route_phrase_to_agent(
    registry: AgentRegistry,
    phrase: str,
    instance_id: str,
    *,
    space_id: str | None = None,
    domain_label: str | None = None,
    allow_llm_fallback: bool = True,
) -> "DispatchTo | AskClarification | Unknown":
    """Resolve ``phrase`` against the registry and translate the
    resolver result into a routing decision.

    The handler is responsible for deciding WHEN to call this
    (intent detection lives upstream). This function maps the
    registry's three result shapes onto a handler-shaped decision
    surface.

    The optional ``space_id`` / ``domain_label`` kwargs scope the
    default-agent fallback (see registry.resolve_natural). The
    handler typically passes the user's current context space
    and any domain hint detected from the phrase.
    """
    result = await registry.resolve_natural(
        phrase,
        instance_id,
        space_id=space_id,
        domain_label=domain_label,
        allow_llm_fallback=allow_llm_fallback,
    )
    if isinstance(result, Match):
        return DispatchTo(record=result.record)
    if isinstance(result, Ambiguity):
        return AskClarification(candidates=result.candidates)
    if isinstance(result, NotFound):
        # Surface the active-records list so the handler can
        # render "agents I know about" for the user.
        active = await registry.list_agents(instance_id, status="active")
        return Unknown(known_agents=tuple(active))
    raise TypeError(f"unexpected resolver result: {result!r}")


__all__ = [
    "AskClarification",
    "DispatchTo",
    "RoutingDecision",
    "Unknown",
    "route_phrase_to_agent",
]
