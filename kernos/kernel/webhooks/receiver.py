"""Webhook receiver — HTTP POST → event_stream.emit.

WORKFLOW-LOOP-PRIMITIVE C6. External sources push events into the
Kernos timeline by POSTing to ``/webhooks/{source_id}``. Each
registered source declares its authentication scheme (HMAC
signature with shared secret, or bearer token) and a payload
schema (a callable that validates the JSON body). Validated
payloads translate to a single ``event_stream.emit`` with
``event_type="external.webhook"`` and a payload that carries the
source identifier alongside the validated body.

Route registration: this module exposes ``register_routes(app)``
which mounts a ``POST /webhooks/{source_id}`` route on a passed-in
FastAPI application. Kernos's live runtime is Discord-via-``server.py``
(no FastAPI surface), so today only the test suite calls
``register_routes`` against a stand-alone FastAPI instance. The
function is kept as ready-but-unwired infrastructure for any future
HTTP-fronted deployment.

v1: HTTP POST with JSON. Future expansion (other content types,
streaming bodies, signed query params) lands in a separate spec.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request

from kernos.kernel import event_stream

logger = logging.getLogger(__name__)


# A schema validator takes the parsed JSON body and either returns
# the body (possibly normalised) or raises a ``ValueError`` to
# reject. Implementations decide how strict to be; v1 ships a
# pass-through default for sources that don't need schema checks.
WebhookSchema = Callable[[dict], dict]


@dataclass
class WebhookSourceConfig:
    """Per-source registration record.

    Pick exactly one of (hmac_secret, bearer_token) — sources are
    rejected at registration if both are set or neither is set.
    """

    source_id: str
    instance_id: str
    hmac_secret: bytes | None = None
    hmac_header: str = "X-Webhook-Signature"
    bearer_token: str | None = None
    bearer_header: str = "Authorization"
    schema: WebhookSchema | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ValueError("source_id is required")
        if not self.instance_id:
            raise ValueError("instance_id is required")
        if (self.hmac_secret is None) == (self.bearer_token is None):
            raise ValueError(
                "exactly one of hmac_secret / bearer_token must be set"
            )
        # Reject empty credentials — an empty bearer_token would
        # accept ``Authorization: Bearer `` (trailing space), and an
        # empty hmac_secret produces a deterministic predictable
        # signature.
        if self.bearer_token is not None and not self.bearer_token.strip():
            raise ValueError("bearer_token must not be empty")
        if self.hmac_secret is not None and len(self.hmac_secret) == 0:
            raise ValueError("hmac_secret must not be empty")


class WebhookRegistry:
    """Holds the registered sources. Operators register a source at
    install time; the receiver consults the registry per request."""

    def __init__(self) -> None:
        self._sources: dict[str, WebhookSourceConfig] = {}

    def register(self, config: WebhookSourceConfig) -> None:
        if config.source_id in self._sources:
            raise ValueError(
                f"webhook source {config.source_id!r} already registered"
            )
        self._sources[config.source_id] = config

    def unregister(self, source_id: str) -> bool:
        return self._sources.pop(source_id, None) is not None

    def get(self, source_id: str) -> WebhookSourceConfig | None:
        return self._sources.get(source_id)

    def has(self, source_id: str) -> bool:
        return source_id in self._sources

    def list_sources(self) -> tuple[WebhookSourceConfig, ...]:
        return tuple(self._sources.values())


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def _verify_hmac(
    secret: bytes, header_value: str | None, body: bytes,
) -> bool:
    if not header_value:
        return False
    # Header may be "sha256=<hex>" or just "<hex>"; accept both.
    sig = header_value.split("=", 1)[1] if "=" in header_value else header_value
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def _verify_bearer(token: str, header_value: str | None) -> bool:
    if not header_value:
        return False
    parts = header_value.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1], token)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_routes(
    app: FastAPI,
    registry: WebhookRegistry,
    *,
    path_prefix: str = "/webhooks",
) -> None:
    """Mount the webhook routes on ``app``. Idempotent for the same
    (app, prefix) pair."""

    @app.post(f"{path_prefix}/{{source_id}}")
    async def receive_webhook(source_id: str, request: Request):
        config = registry.get(source_id)
        if config is None:
            raise HTTPException(
                status_code=404,
                detail=f"unknown webhook source {source_id!r}",
            )
        body = await request.body()
        # Auth.
        if config.hmac_secret is not None:
            header_value = request.headers.get(config.hmac_header)
            if not _verify_hmac(config.hmac_secret, header_value, body):
                raise HTTPException(status_code=401, detail="hmac_invalid")
        elif config.bearer_token is not None:
            header_value = request.headers.get(config.bearer_header)
            if not _verify_bearer(config.bearer_token, header_value):
                raise HTTPException(status_code=401, detail="bearer_invalid")
        # Decode JSON.
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="invalid_json")
        if not isinstance(parsed, dict):
            raise HTTPException(
                status_code=400, detail="payload_must_be_json_object",
            )
        # Schema validation.
        if config.schema is not None:
            try:
                parsed = config.schema(parsed)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail=f"schema_invalid:{exc}",
                )
        # Translate to event_stream emit.
        await event_stream.emit(
            config.instance_id,
            "external.webhook",
            {"source_id": config.source_id, "body": parsed},
        )
        return {"status": "accepted"}


__all__ = [
    "WebhookRegistry",
    "WebhookSchema",
    "WebhookSourceConfig",
    "register_routes",
]
