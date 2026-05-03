"""Kernos FastAPI server — webhook-based SMS inbound.

This is the cloud deployment path for receiving SMS via Twilio webhooks.
For local/development use, SMS inbound uses polling via SMSPoller in server.py.
Run this when deploying to a server with a public URL.

    uvicorn kernos.app:app --host 0.0.0.0 --port 8000
"""
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mcp import StdioServerParameters

load_dotenv()

import dataclasses

from kernos.messages.adapters.twilio_sms import TwilioSMSAdapter
from kernos.kernel.credentials import resolve_anthropic_credential
from kernos.messages.handler import MessageHandler
from kernos.capability.client import AuthCommand, MCPClientManager
from kernos.capability.known import KNOWN_CAPABILITIES
from kernos.capability.registry import CapabilityRegistry, CapabilityStatus
from kernos.kernel.event_types import EventType
from kernos.kernel.events import JsonEventStream, emit_event
from kernos.kernel.engine import TaskEngine
from kernos.kernel.reasoning import AnthropicProvider, ReasoningService
from kernos.kernel.state_json import JsonStateStore
from kernos.persistence.json_file import JsonAuditStore, JsonConversationStore, JsonInstanceStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_twilio_adapter = TwilioSMSAdapter()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: connect MCP servers, init stores, emit system.started. Shutdown: inverse."""
    data_dir = os.getenv("KERNOS_DATA_DIR", "./data")

    events = JsonEventStream(data_dir)
    state = JsonStateStore(data_dir)

    try:
        await emit_event(
            events, EventType.SYSTEM_STARTED, "system", "app", payload={}
        )
    except Exception as exc:
        logger.warning("Failed to emit system.started: %s", exc)

    mcp_manager = MCPClientManager(events=events)

    credentials_path = os.getenv("GOOGLE_OAUTH_CREDENTIALS_PATH", "")
    if credentials_path:
        mcp_manager.register_server(
            "google-calendar",
            StdioServerParameters(
                command="npx",
                args=["@cocal/google-calendar-mcp"],
                env={"GOOGLE_OAUTH_CREDENTIALS": credentials_path},
            ),
        )
        mcp_manager.register_auth_command(
            "google-calendar",
            AuthCommand(
                command="npx",
                args=["@cocal/google-calendar-mcp", "auth", "normal"],
                env={"GOOGLE_OAUTH_CREDENTIALS": credentials_path},
                probe_tool="get-current-time",
            ),
        )
    else:
        logger.warning(
            "GOOGLE_OAUTH_CREDENTIALS_PATH not set — calendar tools unavailable"
        )

    await mcp_manager.connect_all()

    # Build capability registry from known catalog, promote connected servers
    registry = CapabilityRegistry(mcp=mcp_manager)
    for cap in KNOWN_CAPABILITIES:
        registry.register(dataclasses.replace(cap))
    for server_name, tools in mcp_manager.get_tool_definitions().items():
        cap = registry.get(server_name) or registry.get_by_server_name(server_name)
        if cap:
            cap.status = CapabilityStatus.CONNECTED
            cap.tools = [t["name"] for t in tools]
    connected = [c.name for c in registry.get_connected()]
    logger.info("Capability registry ready — connected: %s", connected or "none")

    conversations = JsonConversationStore(data_dir)
    tenants = JsonInstanceStore(data_dir)
    audit = JsonAuditStore(data_dir)

    # REASONING-SERVICE-CONSTRUCTION-PARITY-V1: every callsite that
    # constructs ReasoningService wires turn_runner_provider via the
    # shared helper. Pre-strike requirement: post-flip default-thin
    # crashes here without the wiring (no fallback once legacy is
    # stricken).
    from kernos.providers.chains import build_chains_from_env
    from kernos.kernel.turn_runner_provider import (
        build_turn_runner_provider,
        setup_default_thin_path_context,
        wire_live_thin_path,
    )

    chains, _primary_provider = build_chains_from_env()
    _thin_path_ctx = setup_default_thin_path_context(
        chains=chains, state=state, events=events, audit=audit,
    )
    reasoning = ReasoningService(
        events=events,
        mcp=mcp_manager,
        audit=audit,
        chains=chains,
        trace_sink=_thin_path_ctx.trace_sink,
        turn_runner_provider=build_turn_runner_provider(_thin_path_ctx),
    )
    engine = TaskEngine(reasoning=reasoning, events=events)
    app.state.handler = MessageHandler(
        mcp_manager, conversations, tenants, audit, events, state, reasoning, registry, engine,
        secrets_dir=os.getenv("KERNOS_SECRETS_DIR", "./secrets"),
    )
    wire_live_thin_path(
        _thin_path_ctx,
        reasoning=reasoning,
        handler=app.state.handler,
    )
    logger.info("MessageHandler ready (data_dir=%s)", data_dir)

    yield

    try:
        await emit_event(
            events, EventType.SYSTEM_STOPPED, "system", "app", payload={}
        )
    except Exception as exc:
        logger.warning("Failed to emit system.stopped: %s", exc)

    await app.state.handler.shutdown_runners()
    await mcp_manager.disconnect_all()


app = FastAPI(title="Kernos", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": "0.1.0"})


@app.post("/sms/inbound")
async def sms_inbound(request: Request) -> Response:
    """
    Twilio SMS webhook endpoint.

    Wiring: Twilio adapter (inbound) → handler → Twilio adapter (outbound)
    Validates Twilio signature before processing.
    """
    form_data = await request.form()
    raw = dict(form_data)

    # Validate Twilio request signature
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if auth_token:
        try:
            from twilio.request_validator import RequestValidator
            validator = RequestValidator(auth_token)
            signature = request.headers.get("X-Twilio-Signature", "")
            url = str(request.url)
            if not validator.validate(url, raw, signature):
                logger.warning("SMS_WEBHOOK: rejected invalid Twilio signature from %s", raw.get("From", "unknown"))
                return Response("Forbidden", status_code=403)
        except ImportError:
            logger.warning("SMS_WEBHOOK: twilio library not installed, skipping signature validation")
    else:
        logger.warning("SMS_WEBHOOK: TWILIO_AUTH_TOKEN not set, skipping signature validation")

    logger.info("Inbound SMS from=%s body=%r", raw.get("From"), raw.get("Body"))

    try:
        handler: MessageHandler = request.app.state.handler
        message = _twilio_adapter.inbound(raw)
        response_text = await handler.process(message)
        if not response_text:  # Merged message — no reply needed
            return Response(content="<Response/>", media_type="application/xml")
        twiml = _twilio_adapter.outbound(response_text, message)
        logger.info("Response to=%s twiml=%r", message.sender, twiml)
        return Response(content=twiml, media_type="application/xml")
    except Exception as exc:
        logger.error("Unhandled error in sms_inbound: %s", exc, exc_info=True)
        error_twiml = (
            "<Response><Message>Something went wrong. Please try again.</Message></Response>"
        )
        return Response(content=error_twiml, media_type="application/xml")
