"""Development stdin/stdout REPL for Kernos.

A self-contained CLI that boots Kernos with the same production
wiring as ``server.py`` (decoupled cognitive substrate path,
ReasoningService + per-turn TurnRunner factory, MessageHandler)
but skips the Discord / SMS / Telegram adapter registration. Lets
the operator talk to a dev Kernos instance directly without
setting up second platform credentials, AND gives CC a
programmatic boot path for smoke tests against the real boot
(not the unit-test ``_make_handler`` mock).

Boot symmetry with ``server.py``: this module mirrors
``on_ready``'s wiring using the same helper classes. The single
intentional divergence is the platform layer — ``server.py`` runs
the Discord ``client.run()`` event loop; ``repl.py`` runs a
stdin loop calling ``handler.process()`` directly. The cognitive-
substrate invariant (CCV1) is preserved end-to-end because the
``ReasoningService`` + ``MessageHandler`` constructed here is the
same shape production runs.

Two consumers:

* **Operator REPL** — ``python -m kernos.repl`` reads stdin, sends
  each line as a normalized message, prints the response. Used
  for the CCV1 C6 soak runbook scenarios.
* **CC smoke test** — ``build_dev_handler()`` is a public seam.
  ``tests/test_repl_boot_smoke.py`` calls it with a mock provider
  to verify the boot succeeds and the substrate reaches the
  model-call seam — catches boot-time issues the unit-test
  ``_make_handler`` mock doesn't (because that mock bypasses
  ``build_dev_handler``).

What this REPL deliberately skips vs. ``server.py``:

* External MCP servers (google-calendar, brave-search, web-browser).
  The kernel registry's built-in capabilities still load; external
  services are skipped because soak doesn't need them and they
  pull in optional API keys.
* Auto-update, post-update whisper queueing, canvas-seeding,
  WTC C5c-bringup substrate. These are operational lifecycle
  concerns; soak verification is about model-call substrate
  fidelity, which is upstream of all of them.
* Discord / SMS / Telegram adapters. The whole point of REPL.

What it preserves verbatim from ``server.py``:

* JsonEventStream / SqliteStateStore (or JsonStateStore via
  KERNOS_STORE_BACKEND=json) / InstanceDB / event-stream writer.
* JsonConversationStore / JsonInstanceStore / JsonAuditStore.
* CapabilityRegistry seeded with KNOWN_CAPABILITIES.
* ``build_chains_from_env`` for the LLM provider chain.
* The full per-turn TurnRunner factory closure
  (Planner / StepDispatcher / DivergenceReasoner /
  PresenceRenderer / IntegrationService / EnactmentService /
  ProductionResponseDelivery) with telemetry wrapping.
* MessageHandler with the same constructor args, including
  ``_instance_db`` wiring for member resolution.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("kernos.repl")


@dataclass(frozen=True)
class ReplIdentity:
    """Selected member identity for a REPL session.

    Codex C5-review CONCERN fold: the prior ``select_member`` returned
    only ``(sender, display)`` which dropped member_id, the platform
    binding, and whether a new repl channel was registered. The
    future ``kernos`` CLI needs all four to do its job
    (``kernos repl --member <id> --space <space>``,
    ``kernos members add ...``, etc.). This dataclass is the public
    identity surface.

    * ``member_id`` — the stable mem_xxx id for the selected member.
    * ``display_name`` — the member's display name (banner / prompt).
    * ``platform`` — the platform string the REPL builds messages
      with. Always ``"repl"`` today; preserved on the dataclass so
      the future CLI can extend without changing the message-build
      shape.
    * ``channel_id`` — the repl-platform channel id for this member.
      Pre-registered in instance_db so abuse-prevention and member
      resolution recognize the sender on every turn.
    """

    member_id: str
    display_name: str
    platform: str
    channel_id: str


async def build_dev_handler(
    *,
    data_dir: str | None = None,
    instance_id: str | None = None,
    decoupled: bool = True,
    sender: str | None = None,
    sender_display_name: str = "operator",
) -> Any:
    """Boot Kernos for REPL / smoke-test use. Returns a wired
    ``MessageHandler`` ready to call ``handler.process(message)``.

    Args:
      data_dir: persistence root. Defaults to ``KERNOS_DATA_DIR`` env
        or ``./data-dev`` so REPL state is isolated from production.
      instance_id: identifier for this instance (used for state
        keying). Defaults to ``KERNOS_INSTANCE_ID`` env or
        ``repl:dev``.
      decoupled: when True (default), boots with
        ``KERNOS_USE_DECOUPLED_TURN_RUNNER=1`` semantics — the path
        CCV1 closes substrate fidelity on. The flag is sticky for the
        process; use ``decoupled=False`` to exercise the legacy path.
      sender: the platform sender id for the REPL user. Pre-registered
        as the instance's owner so the abuse-prevention guard doesn't
        block the very first message. Defaults to
        ``KERNOS_REPL_SENDER`` env or ``"operator"``.
      sender_display_name: the display name to associate with the
        registered owner. Defaults to ``"operator"``.

    Returns: a ``MessageHandler`` instance with full reasoning, state,
    capability registry, and substrate wiring. Adapter registration is
    deliberately omitted — the caller drives the message flow directly.
    """
    # Imports are local to keep the module's import-time surface light
    # (the contract tests don't pay for the production-wiring imports
    # unless they actually call build_dev_handler).
    from kernos.capability.client import MCPClientManager
    from kernos.capability.known import KNOWN_CAPABILITIES
    from kernos.capability.registry import (
        CapabilityRegistry,
        CapabilityStatus,
    )
    from kernos.kernel.cohorts import (
        CohortFanOutConfig,
        CohortFanOutRunner,
        CohortRegistry,
        register_covenant_cohort,
    )
    from kernos.kernel.enactment import (
        DivergenceReasoner,
        EnactmentService,
        Planner,
        PresenceRenderer,
        StaticToolCatalog,
        StepDispatcher,
        ToolExecutionResult,
    )
    from kernos.kernel.enactment.dispatcher import (
        ToolExecutionInputs,
    )
    from kernos.kernel.engine import TaskEngine
    from kernos.kernel.events import JsonEventStream, emit_event
    from kernos.kernel.event_types import EventType
    from kernos.kernel.instance_db import InstanceDB
    from kernos.kernel.integration.service import IntegrationService
    from kernos.kernel.reasoning import ReasoningService
    from kernos.kernel.response_delivery import (
        AggregatedTelemetry,
        ProductionResponseDelivery,
        wrap_chain_caller_with_telemetry,
    )
    from kernos.kernel.turn_runner import TurnRunner
    from kernos.messages.handler import MessageHandler
    from kernos.persistence.json_file import (
        JsonAuditStore,
        JsonConversationStore,
        JsonInstanceStore,
    )
    from kernos.providers.chains import build_chains_from_env

    # Decoupled flag — set explicitly in BOTH directions. Codex
    # C5-review BLOCKER fold: the prior implementation only set the
    # env var when ``decoupled=True`` and never cleared it when
    # ``decoupled=False``, so a legacy-oracle soak run that
    # inherited ``KERNOS_USE_DECOUPLED_TURN_RUNNER=1`` from .env
    # would silently still take the decoupled path. The C6 runbook
    # requires both paths runnable; force the env to match the arg.
    os.environ["KERNOS_USE_DECOUPLED_TURN_RUNNER"] = (
        "1" if decoupled else "0"
    )

    _data_dir = data_dir or os.getenv("KERNOS_DATA_DIR", "./data-dev")
    _instance_id = instance_id or os.getenv("KERNOS_INSTANCE_ID", "repl:dev")
    Path(_data_dir).mkdir(parents=True, exist_ok=True)

    # Mirror server.py:on_ready — same construction order so the
    # wiring shape matches production exactly. See
    # docstring "What it preserves verbatim from server.py."
    events = JsonEventStream(_data_dir)

    store_backend = os.getenv("KERNOS_STORE_BACKEND", "sqlite")
    if store_backend == "json":
        from kernos.kernel.state_json import JsonStateStore
        state = JsonStateStore(_data_dir)
        logger.info("repl: state backend = JSON files")
    else:
        from kernos.kernel.state_sqlite import SqliteStateStore
        state = SqliteStateStore(_data_dir)
        logger.info("repl: state backend = SQLite (WAL)")

    instance_db = InstanceDB(_data_dir)
    try:
        await instance_db.connect()
    except Exception as exc:
        logger.warning("repl: instance_db init failed (non-fatal): %s", exc)

    # Bootstrap sender registration — only when the sender's
    # channel isn't already bound. This is the right semantic for
    # both Codex's CONCERN (don't over-bind operator-chosen senders
    # to the owner role) and the practical REPL need (the
    # abuse-prevention guard requires the sender to resolve to
    # SOMEBODY).
    #
    # Three cases:
    #
    # 1. Sender already registered (for any member): no-op. The
    #    operator picked an existing identity (e.g.,
    #    ``KERNOS_REPL_SENDER=mem_xyz`` for a non-owner soak run).
    #    ``select_member`` will resolve it to the right member;
    #    no over-binding to owner occurs.
    # 2. Sender NOT registered, instance has no owner yet (truly
    #    fresh): ``ensure_owner`` creates the owner with this
    #    sender as their repl channel. Standard fresh-operator
    #    bootstrap.
    # 3. Sender NOT registered, instance already has an owner
    #    (e.g., a prod-derived data dir that hasn't seen REPL
    #    use): ``ensure_owner`` registers the sender as an
    #    additional repl channel for the existing owner. The
    #    operator implicitly chose to act as the instance owner
    #    by not picking a specific channel — this is the right
    #    default for REPL.
    _bootstrap_sender = sender or os.getenv("KERNOS_REPL_SENDER", "operator")
    try:
        existing_for_channel = await instance_db.get_member_by_channel(
            "repl", _bootstrap_sender,
        )
    except Exception:
        existing_for_channel = None
    if existing_for_channel is None:
        try:
            await instance_db.ensure_owner(
                member_id="",  # ensure_owner derives stable id
                display_name=sender_display_name,
                instance_id=_instance_id,
                platform="repl",
                channel_id=_bootstrap_sender,
            )
        except Exception as exc:
            logger.warning("repl: ensure_owner failed (non-fatal): %s", exc)

    try:
        from kernos.kernel import event_stream as _evstream_mod
        await _evstream_mod.start_writer(_data_dir)
    except Exception as exc:
        logger.warning("repl: event_stream writer start failed: %s", exc)

    try:
        await emit_event(events, EventType.SYSTEM_STARTED, "system", "repl", payload={})
    except Exception:
        pass

    # MCP manager — but we do NOT register external servers (calendar
    # / brave / web-browser). Soak doesn't need them and they pull
    # API keys we may not have in dev. The kernel registry still
    # functions; built-in tools (remember, write_file, etc.) work.
    mcp_manager = MCPClientManager(events=events)
    await mcp_manager.connect_all()  # connects nothing; safe no-op

    conversations = JsonConversationStore(_data_dir)
    tenants = JsonInstanceStore(_data_dir)
    audit = JsonAuditStore(_data_dir)

    registry = CapabilityRegistry(mcp=mcp_manager)
    for cap in KNOWN_CAPABILITIES:
        registry.register(dataclasses.replace(cap))
    # No connected-server promotion — we registered no external servers.
    logger.info("repl: capability registry ready (kernel tools only)")

    chains, _primary_provider = build_chains_from_env()

    # Per-turn TurnRunner factory — verbatim mirror of server.py's
    # _build_per_turn_runner closure.
    reasoning_trace_sink: list[dict] = []

    cohort_registry = CohortRegistry()
    try:
        register_covenant_cohort(cohort_registry, state)
    except Exception:
        logger.exception("repl: covenant cohort registration failed")

    async def _cohort_audit_emitter(entry: dict) -> None:
        try:
            if audit is None or not hasattr(audit, "log"):
                return
            iid = entry.get("instance_id", "") or ""
            await audit.log(iid, entry)
        except Exception:
            pass

    cohort_runner = CohortFanOutRunner(
        registry=cohort_registry,
        audit_emitter=_cohort_audit_emitter,
        config=CohortFanOutConfig(),
    )

    primary_chain = chains.get("primary", [])

    async def _shared_chain_caller(
        system, messages, tools, max_tokens, *, conversation_id="",
    ):
        # ============================================================
        # WIRE-SHAPE PLUMBING SEAM — mirrors server.py's chain caller.
        # ============================================================
        # See server.py's _shared_chain_caller for the full contract.
        # The dev REPL boot path MUST forward conversation_id identically
        # to the production server, otherwise tests/test_repl_boot_smoke.py
        # detects substrate divergence between dev REPL and production.
        # Pin tests:
        #   tests/test_thin_path_codex_wire_shape_plumbing.py
        # See kernos/providers/codex_provider.py class docstring
        # "WIRE SHAPE INVARIANTS" for the full Codex wire contract.
        if not primary_chain:
            raise RuntimeError(
                "primary chain not configured — REPL needs at least one "
                "configured provider (set KERNOS_PRIMARY_MODEL etc.)"
            )
        entry = primary_chain[0]
        return await entry.provider.complete(
            model=entry.model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            conversation_id=conversation_id,
        )

    class _UnwiredDescriptorLookup:
        def descriptor_for(self, tool_id):
            raise NotImplementedError(
                f"workshop tool descriptor lookup not wired in REPL; "
                f"tool={tool_id!r}. Thin-path turns succeed; full-"
                f"machinery dispatch awaits the workshop binding."
            )

    class _UnwiredExecutor:
        async def execute(self, inputs: ToolExecutionInputs) -> ToolExecutionResult:
            raise RuntimeError(
                f"production tool executor not wired in REPL; "
                f"tool={inputs.tool_id!r}."
            )

    planner_tool_catalog = StaticToolCatalog()
    shared_executor = _UnwiredExecutor()
    shared_descriptor_lookup = _UnwiredDescriptorLookup()

    async def _integration_dispatcher(tool_id, args, inputs):
        return {}

    async def _integration_audit_emitter(entry: dict) -> None:
        try:
            if audit is None or not hasattr(audit, "log"):
                return
            iid = entry.get("instance_id", "") or ""
            await audit.log(iid, entry)
        except Exception:
            pass

    async def _dispatcher_event_emitter(payload: dict) -> None:
        try:
            from kernos.kernel.events import emit_event as _emit
            from kernos.kernel.event_types import EventType as _ET
            event_type = (
                _ET.TOOL_CALLED if payload.get("type") == "tool.called"
                else _ET.TOOL_RESULT
            )
            await _emit(
                events, event_type, payload.get("instance_id", ""),
                "step_dispatcher", payload=payload,
            )
        except Exception:
            pass

    async def _dispatcher_audit_emitter(entry: dict) -> None:
        try:
            if audit is None or not hasattr(audit, "log"):
                return
            iid = entry.get("instance_id", "") or ""
            await audit.log(iid, entry)
        except Exception:
            pass

    def _build_per_turn_runner(request, event_emitter):
        telemetry = AggregatedTelemetry()
        wrapped_chain = wrap_chain_caller_with_telemetry(
            _shared_chain_caller, telemetry,
        )
        planner = Planner(chain_caller=wrapped_chain, tool_catalog=planner_tool_catalog)
        dispatcher = StepDispatcher(
            executor=shared_executor,
            descriptor_lookup=shared_descriptor_lookup,
            trace_sink=reasoning_trace_sink,
            event_emitter=_dispatcher_event_emitter,
            audit_emitter=_dispatcher_audit_emitter,
            on_dispatch_complete=telemetry.add_tool_iteration,
        )
        reasoner = DivergenceReasoner(chain_caller=wrapped_chain)
        presence = PresenceRenderer(chain_caller=wrapped_chain)
        integration = IntegrationService(
            chain_caller=wrapped_chain,
            read_only_dispatcher=_integration_dispatcher,
            audit_emitter=_integration_audit_emitter,
        )
        enactment = EnactmentService(
            presence_renderer=presence,
            planner=planner,
            step_dispatcher=dispatcher,
            divergence_reasoner=reasoner,
        )
        delivery = ProductionResponseDelivery(
            request=request,
            telemetry=telemetry,
            event_emitter=event_emitter,
        )
        runner = TurnRunner(
            cohort_runner=cohort_runner,
            integration_service=integration,
            enactment_service=enactment,
            response_delivery=delivery,
        )
        return runner, delivery

    reasoning = ReasoningService(
        events=events,
        mcp=mcp_manager,
        audit=audit,
        chains=chains,
        trace_sink=reasoning_trace_sink,
        turn_runner_provider=_build_per_turn_runner,
    )
    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(
        mcp_manager, conversations, tenants, audit, events, state,
        reasoning, registry, engine,
        secrets_dir=os.getenv("KERNOS_SECRETS_DIR", "./secrets-dev"),
    )
    handler._instance_db = instance_db
    handler.register_mcp_tools_in_catalog()
    logger.info("repl: handler ready (instance_id=%s, data_dir=%s)", _instance_id, _data_dir)
    return handler


async def shutdown_dev_handler(handler: Any) -> None:
    """Tear down the global side effects ``build_dev_handler``
    started: event-stream writer task, awareness evaluators, MCP
    clients, instance_db connection, state-store connections.
    Idempotent and best-effort — each subsystem is wrapped in
    try/except.

    Call this from REPL ``finally:`` blocks and from smoke-test
    fixtures so subsequent tests / processes don't see leaked
    background tasks.

    Codex C5-review BLOCKER fold: ``MessageHandler`` stores live
    awareness evaluators in ``handler._evaluators`` (a dict keyed by
    instance_id), not the singular ``_evaluator``. The original
    shutdown checked the wrong attribute, leaving the periodic task
    leaked. This version iterates the dict + clears it.
    """
    # Awareness evaluators (started lazily by handler when it begins
    # background polling per-instance — see provision phase's
    # _maybe_start_evaluator). Stop first so they don't try to write
    # during teardown.
    try:
        evaluators = getattr(handler, "_evaluators", None) or {}
        for evaluator in list(evaluators.values()):
            try:
                await evaluator.stop()
            except Exception:
                pass
        if isinstance(evaluators, dict):
            evaluators.clear()
    except Exception:
        pass
    # MCP clients.
    try:
        if getattr(handler, "mcp", None) is not None:
            await handler.mcp.disconnect_all()
    except Exception:
        pass
    # Event-stream writer (the background SQLite-flush task).
    try:
        from kernos.kernel import event_stream as _evstream_mod
        _stop = getattr(_evstream_mod, "stop_writer", None)
        if _stop is not None:
            await _stop()
    except Exception:
        pass
    # Instance DB connection.
    try:
        idb = getattr(handler, "_instance_db", None)
        if idb is not None and hasattr(idb, "close"):
            await idb.close()
    except Exception:
        pass
    # State store connections (SQLite has a connection pool;
    # close_all releases handles so subsequent boots don't hit
    # "database is locked").
    try:
        state = getattr(handler, "state", None)
        if state is not None and hasattr(state, "close_all"):
            await state.close_all()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# REPL loop — the operator-facing interactive surface
# ---------------------------------------------------------------------------


def _build_message(
    content: str, *, instance_id: str, identity: ReplIdentity,
) -> Any:
    """Construct a ``NormalizedMessage`` from a ``ReplIdentity``.

    Codex C5-review BLOCKER fold: ``platform`` and ``sender`` are
    paired together via the identity dataclass so they cannot
    drift. The prior implementation hardcoded ``platform="repl"``
    while accepting any sender id — including discord/sms ids that
    didn't match the platform — which broke
    ``handler._resolve_member`` and tripped the abuse path.
    """
    from kernos.messages.models import AuthLevel, NormalizedMessage
    return NormalizedMessage(
        content=content,
        sender=identity.channel_id,
        sender_auth_level=AuthLevel.owner_verified,
        platform=identity.platform,
        platform_capabilities=["text"],
        conversation_id=identity.channel_id,
        timestamp=datetime.now(timezone.utc),
        instance_id=instance_id,
    )


async def _read_line(prompt: str = "> ") -> str:
    """Async stdin readline — uses asyncio.to_thread so we don't
    block the event loop on user typing."""
    print(prompt, end="", flush=True)
    return await asyncio.to_thread(sys.stdin.readline)


async def select_member(
    handler: Any,
    *,
    explicit_sender: str | None = None,
) -> ReplIdentity:
    """Multi-user member selection. Returns a :class:`ReplIdentity`
    pinning the chosen member + their repl-platform channel id so
    REPL message construction can pass a sender that
    ``handler._resolve_member`` actually recognizes.

    Selection order:

    1. If ``explicit_sender`` is non-empty, use it verbatim AS the
       repl channel id. Caller passed a specific identifier they
       know is registered (default ``"operator"`` from .env, an
       explicit override, or CC's piped-input usage).
    2. If exactly one member exists on the instance, auto-select it.
    3. Otherwise, prompt for a selection. Codex C5-review fold:
       refuse to prompt when stdin is non-TTY (e.g., piped input)
       since the prompt would consume the first piped line as the
       member-selection answer. In that case raise with a clear
       message instructing the operator to set ``KERNOS_REPL_SENDER``.

    For the selected member, the function ensures a ``repl``-platform
    channel exists in instance_db (registers one if absent) so that
    ``_resolve_member(platform="repl", sender=channel_id)`` resolves
    the right member every turn. Without this, ``_first_channel_id``
    might return a discord/sms id that doesn't match the REPL's
    hardcoded ``platform="repl"``, and turns would hit the abuse
    path. (Codex Q3 BLOCKER fold.)
    """
    instance_db = getattr(handler, "_instance_db", None)

    # Helper: resolve / register a repl channel for a given member.
    # Returns the channel_id the REPL should use as sender.
    async def _ensure_repl_channel(member: dict) -> str:
        channels = member.get("channels", []) or []
        for ch in channels:
            if ch.get("platform") == "repl" and ch.get("channel_id"):
                return ch["channel_id"]
        # No repl channel yet — register a deterministic one keyed
        # to the member id so multiple REPL sessions for the same
        # member always resolve the same way.
        mid = member.get("member_id", "")
        new_channel = f"repl:{mid}" if mid else "repl"
        if instance_db is not None and mid:
            try:
                await instance_db.register_channel(
                    member_id=mid,
                    platform="repl",
                    channel_id=new_channel,
                )
            except Exception as exc:
                logger.warning(
                    "repl: register_channel failed for %s — falling back "
                    "to ad-hoc sender. Error: %s",
                    mid, exc,
                )
        return new_channel

    if explicit_sender:
        # The caller asserts this sender is a valid repl channel.
        # The match path: caller is using KERNOS_REPL_SENDER from
        # .env / shell (likely ``operator`` from the fresh-instance
        # ensure_owner path). Look up the member by repl channel
        # if possible; fall back to using the sender verbatim.
        if instance_db is not None:
            try:
                member = await instance_db.get_member_by_channel(
                    "repl", explicit_sender,
                )
            except Exception:
                member = None
            if member is not None:
                return ReplIdentity(
                    member_id=member.get("member_id", ""),
                    display_name=(
                        member.get("display_name", "") or explicit_sender
                    ),
                    platform="repl",
                    channel_id=explicit_sender,
                )
        return ReplIdentity(
            member_id="", display_name=explicit_sender,
            platform="repl", channel_id=explicit_sender,
        )

    if instance_db is None:
        return ReplIdentity(
            member_id="", display_name="operator",
            platform="repl", channel_id="operator",
        )

    try:
        members = await instance_db.list_members()
    except Exception:
        members = []

    if not members:
        return ReplIdentity(
            member_id="", display_name="operator",
            platform="repl", channel_id="operator",
        )

    if len(members) == 1:
        m = members[0]
        channel_id = await _ensure_repl_channel(m)
        display = m.get("display_name", "") or m.get("member_id", "operator")
        print(f"REPL: auto-selected sole member: {display} ({channel_id})")
        return ReplIdentity(
            member_id=m.get("member_id", ""),
            display_name=display,
            platform="repl",
            channel_id=channel_id,
        )

    # Multi-member: prompt — but only if stdin is interactive.
    # Codex C5-review fold: piped use without ``KERNOS_REPL_SENDER``
    # would otherwise consume the first piped line as the
    # member-selection answer. Refuse to prompt and surface a clear
    # error instead.
    if not sys.stdin.isatty():
        raise RuntimeError(
            "kernos.repl: multiple members found and stdin is not a "
            "TTY. Set KERNOS_REPL_SENDER to a member's repl channel "
            "id (or use --sender on the future kernos CLI) to "
            "bypass interactive selection. Available members: "
            + ", ".join(
                f"{m.get('display_name','?')} ({m.get('member_id','?')})"
                for m in members
            )
        )

    print("Members on this instance:")
    for i, m in enumerate(members, start=1):
        display = m.get("display_name", "") or m.get("member_id", "")
        mid = m.get("member_id", "")
        chs = m.get("channels", []) or []
        ch_strs = [f"{c.get('platform','')}:{c.get('channel_id','')}" for c in chs]
        ch_label = ", ".join(ch_strs) if ch_strs else "no channels"
        print(f"  [{i}] {display}  ({mid})  — {ch_label}")
    print()

    while True:
        choice_line = await _read_line("Pick member [1] or member_id: ")
        choice = (choice_line or "").strip()
        if not choice:
            choice = "1"
        # Numeric index
        m = None
        try:
            idx = int(choice)
            if 1 <= idx <= len(members):
                m = members[idx - 1]
        except ValueError:
            pass
        if m is None:
            # member_id / display_name substring match
            matches = [
                cand for cand in members
                if choice in (cand.get("member_id", "") or "")
                or choice.lower() in (cand.get("display_name", "") or "").lower()
            ]
            if len(matches) == 1:
                m = matches[0]
        if m is None:
            print("No unique match. Try again, or Ctrl-C to bail.")
            continue
        channel_id = await _ensure_repl_channel(m)
        display = m.get("display_name", "") or m.get("member_id", "")
        return ReplIdentity(
            member_id=m.get("member_id", ""),
            display_name=display,
            platform="repl",
            channel_id=channel_id,
        )


async def repl_loop(
    handler: Any,
    *,
    instance_id: str,
    identity: ReplIdentity,
) -> None:
    """Read-eval-print loop. Each line of stdin becomes a turn.

    Special commands:
      /quit, /exit — leave the REPL.
      (any other ``/...`` line is passed to the handler unchanged so
      slash commands like /wipe, /dump can be exercised.)
    """
    print("Kernos REPL (Ctrl-D or /quit to exit)")
    print(f"  instance_id = {instance_id}")
    print(
        f"  member      = {identity.display_name} "
        f"({identity.member_id or 'unbound'})"
    )
    print(f"  channel     = {identity.platform}:{identity.channel_id}")
    print()
    while True:
        try:
            line = await _read_line()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            print()
            break
        text = line.rstrip("\n")
        if text.strip() in ("/quit", "/exit"):
            break
        if not text.strip():
            continue
        try:
            message = _build_message(
                text, instance_id=instance_id, identity=identity,
            )
            response = await handler.process(message)
        except Exception as exc:
            print(f"[error] {exc}")
            logger.exception("repl: handler.process raised")
            continue
        if isinstance(response, str):
            print(response)
        else:
            print(repr(response))
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> int:
    """Entry point — boot Kernos, prompt for member selection if
    multi-member, run the REPL loop.

    Multi-user note: when a Kernos instance has multiple members,
    the operator picks which member's space to act as for the
    session. ``KERNOS_REPL_SENDER`` env or ``--sender`` (future CLI
    arg) bypasses the prompt. Until the full ``kernos`` CLI lands,
    this function is the closest thing to ``kernos repl``.
    """
    # Load .env so API keys (ANTHROPIC_API_KEY etc.) are available
    # to the provider chain. cli.sh / dev-repl.sh's _load_kernos_env
    # only loads KERNOS_* prefixed values; the LLM credentials live
    # in the rest of the file. server.py + chat.py + cli.py all
    # call load_dotenv at module-import time; the REPL does the
    # same here.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    logging.basicConfig(
        level=os.getenv("KERNOS_LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    instance_id = os.getenv("KERNOS_INSTANCE_ID", "repl:dev")
    explicit_sender = os.getenv("KERNOS_REPL_SENDER", "")
    handler = await build_dev_handler(
        instance_id=instance_id,
        sender=explicit_sender or "operator",
    )
    identity = await select_member(
        handler, explicit_sender=explicit_sender or None,
    )
    try:
        await repl_loop(handler, instance_id=instance_id, identity=identity)
    finally:
        await shutdown_dev_handler(handler)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
