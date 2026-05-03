"""External-service registration for the workshop primitive.

A service descriptor declares an external service (Notion, GitHub,
Slack, etc.) that workshop tools can bind to via service_id. Each
service names its auth type, the operator scopes the service exposes,
the audit category for invocations, and the channels through which
the auth onboarding flow is allowed to run.

The auth-type-by-channel matrix is machine-readable and load-bearing:
the onboarding flow refuses incompatible channel-and-auth combos with
a clear pointer to the alternative. API token paste lands only on
CLI; OAuth device-code flows on every adapter Kernos supports.
cookie_upload is intentionally absent from the auth-type enum until
the BROWSER-COOKIE-IMPORT spec lands and gives it real implementation
substrate; declaring an enum value with no implementation is a
registration footgun we are deliberately not shipping.

Service descriptors live as JSON files under kernos/kernel/services/
(stock services) or under a member's workspace (workshop-built
services). The ServiceRegistry loads stock services at construction
and accepts dynamic registrations for workshop-built ones.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums + matrices
# ---------------------------------------------------------------------------


class AuthType(str, Enum):
    """Auth onboarding mechanism for a service.

    Per the the design review-revised spec: cookie_upload is not in this enum until
    BROWSER-COOKIE-IMPORT ships. Adding it before then would let
    services register with an auth type that has no implementation.
    """

    API_TOKEN = "api_token"
    OAUTH_DEVICE_CODE = "oauth_device_code"


class ChannelType(str, Enum):
    """Channels Kernos can drive auth onboarding through.

    Matches the platform field used elsewhere in the system
    (NormalizedMessage.platform, instance_db.platform_config).
    """

    CLI = "cli"
    DISCORD = "discord"
    SMS = "sms"
    TELEGRAM = "telegram"


# The auth-type-by-channel-type matrix.
#
# Per the the design review-revised spec: each auth type pairs with the channels
# where its onboarding flow can run safely. The onboarding flow
# refuses anything outside this matrix with a clear message and a
# pointer to the alternative.
#
# The reasoning by row:
#  - api_token: paste a long-lived secret. CLI is the only channel
#    where the token is not stored server-side by an external service
#    we do not control. Discord, SMS, and Telegram all retain message
#    history outside Kernos's threat boundary.
#  - oauth_device_code: the user confirms on their own device using
#    a short-lived code we surface via the adapter. The code itself
#    is not a long-lived secret, so adapter-history retention is
#    acceptable. Works on every adapter.
AUTH_CHANNEL_MATRIX: dict[AuthType, frozenset[ChannelType]] = {
    AuthType.API_TOKEN: frozenset({ChannelType.CLI}),
    AuthType.OAUTH_DEVICE_CODE: frozenset({
        ChannelType.CLI,
        ChannelType.DISCORD,
        ChannelType.SMS,
        ChannelType.TELEGRAM,
    }),
}


def is_auth_channel_compatible(auth: AuthType, channel: ChannelType) -> bool:
    """True if onboarding `auth` is allowed via `channel`."""
    return channel in AUTH_CHANNEL_MATRIX.get(auth, frozenset())


def channel_alternatives_for(auth: AuthType) -> list[ChannelType]:
    """Channels the operator could use instead. Sorted, deterministic."""
    return sorted(AUTH_CHANNEL_MATRIX.get(auth, frozenset()), key=lambda c: c.value)


class IncompatibleAuthChannelError(RuntimeError):
    """Raised when onboarding tries to run an auth flow on an unsafe channel.

    Carries enough information for callers to surface the alternative
    to the operator without further lookups.
    """

    def __init__(self, auth: AuthType, channel: ChannelType) -> None:
        alts = channel_alternatives_for(auth)
        alts_str = ", ".join(c.value for c in alts) if alts else "(none configured)"
        super().__init__(
            f"Auth type '{auth.value}' is not allowed on channel "
            f"'{channel.value}'. Use one of: {alts_str}."
        )
        self.auth = auth
        self.channel = channel
        self.alternatives = alts


def assert_auth_channel_compatible(auth: AuthType, channel: ChannelType) -> None:
    """Raise IncompatibleAuthChannelError if the pair is unsafe."""
    if not is_auth_channel_compatible(auth, channel):
        raise IncompatibleAuthChannelError(auth, channel)


# ---------------------------------------------------------------------------
# Service descriptor
# ---------------------------------------------------------------------------


_SERVICE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class PkceMode(str, Enum):
    """PKCE handling for OAuth device-code services (Q5).

    Providers vary on PKCE acceptance for device flow:
    - REQUIRED: provider rejects requests without code_verifier
      (Google APIs).
    - OPTIONAL: provider accepts code_verifier when present but does
      not require it (Slack).
    - OMIT: provider rejects code_verifier (legacy implementations).

    The flag lives per-service in the descriptor's oauth section so
    the connector author declares the right shape per provider rather
    than the subsystem hardcoding a one-size choice.
    """

    REQUIRED = "required"
    OPTIONAL = "optional"
    OMIT = "omit"


@dataclass(frozen=True)
class OAuthDeviceCodeConfig:
    """Per-service OAuth device-code endpoints + client config (Q1).

    Belongs in the service descriptor when auth_type is
    oauth_device_code. The auth-by-channel matrix already governs
    which adapters can run device-code onboarding; this struct is the
    service-specific endpoint and client information the device-code
    client needs at runtime.

    Fields:
        device_authorization_uri: provider endpoint that issues
            device_code + user_code (RFC 8628 §3.1).
        token_uri: provider endpoint that exchanges device_code for
            access_token (RFC 8628 §3.4 + RFC 6749 §6 for refresh).
        client_id: literal client_id when the connector is an
            OSS-public client (Slack, GitHub). Empty when the
            operator supplies it via env var (closed-app cases like
            Google APIs); see client_id_env.
        client_id_env: name of the env var holding the client_id.
            Empty when client_id is set literally. Exactly one of
            client_id and client_id_env is populated; both empty is a
            validation error.
        client_secret: optional literal client_secret. Some providers
            (Google, GitHub via PAT-based OAuth apps) require a
            client_secret in the token-exchange step alongside
            client_id; pure RFC 8628 implementations (Slack) do not.
            Empty when not needed or supplied via env var.
        client_secret_env: optional env var name holding the
            client_secret. At most one of client_secret and
            client_secret_env may be set; both empty means the
            service uses client-id-only auth.
        pkce: PkceMode per Q5.

    The client_secret pair was added to handle the Google divergence
    surfaced during STOCK-INTEGRATIONS-DRIVE: Google's device-flow
    token-exchange step requires client_secret even though RFC 8628
    does not. Including it here keeps the subsystem provider-agnostic
    (the per-service descriptor decides whether to send secret).
    """

    device_authorization_uri: str
    token_uri: str
    client_id: str = ""
    client_id_env: str = ""
    client_secret: str = ""
    client_secret_env: str = ""
    pkce: PkceMode = PkceMode.OPTIONAL


@dataclass(frozen=True)
class ServiceDescriptor:
    """One external service Kernos can bind to.

    Fields:
        service_id: stable machine identifier (e.g. "notion", "github").
        display_name: human label.
        auth_type: which onboarding mechanism this service uses.
        operations: the operation vocabulary this service exposes (e.g.
            ["read_pages", "write_pages", "delete_pages"]). Tool
            descriptors declare their authority as a subset of this list.
        audit_category: the audit log's operator-readable category for
            invocations against this service. Free-form string; convention
            mirrors the service_id ("notion" → "notion") but is separate
            so operators can rename without churning the service id.
        required_scopes: scope strings the auth flow asks for at onboarding
            time (e.g. OAuth scopes). Free-form per-service.
        notes: free-form text that surfaces in inspect_state.
        oauth: device-code endpoints + client config when auth_type is
            oauth_device_code. None for api_token services. Validated
            at parse time: required when auth_type is oauth_device_code,
            rejected when auth_type is api_token.
    """

    service_id: str
    display_name: str
    auth_type: AuthType
    operations: tuple[str, ...] = ()
    audit_category: str = ""
    required_scopes: tuple[str, ...] = ()
    notes: str = ""
    oauth: OAuthDeviceCodeConfig | None = None

    def supports_operation(self, operation: str) -> bool:
        """True if this service declares the named operation."""
        return operation in self.operations

    def supported_channels(self) -> list[ChannelType]:
        """Channels through which auth onboarding for this service can run."""
        return channel_alternatives_for(self.auth_type)

    def resolve_client_id(self) -> str:
        """Return the OAuth client_id, reading the env var when applicable.

        Raises ServiceDescriptorError when the descriptor expects an
        env var (client_id_env) that isn't set. Callers needing to
        distinguish "not configured yet" from a literal empty value
        should call this directly rather than reading oauth.client_id.
        """
        if self.oauth is None:
            raise ServiceDescriptorError(
                f"Service {self.service_id!r} has no oauth config; "
                f"resolve_client_id is only valid for oauth_device_code "
                f"services."
            )
        if self.oauth.client_id:
            return self.oauth.client_id
        if self.oauth.client_id_env:
            value = os.environ.get(self.oauth.client_id_env, "").strip()
            if not value:
                raise ServiceDescriptorError(
                    f"Service {self.service_id!r} expects the OAuth "
                    f"client_id in env var {self.oauth.client_id_env!r} "
                    f"but it is not set or is empty. Set the env var "
                    f"and retry."
                )
            return value
        # parse_service_descriptor validates that exactly one is set;
        # reaching here means the descriptor was constructed bypassing
        # the parser, which is a programmer error.
        raise ServiceDescriptorError(
            f"Service {self.service_id!r}'s oauth config has no "
            f"client_id and no client_id_env. Programmer error: do not "
            f"construct OAuthDeviceCodeConfig with both empty."
        )

    def resolve_client_secret(self) -> str:
        """Return the OAuth client_secret, or empty string when none is configured.

        Unlike client_id, client_secret is genuinely optional — many
        providers (Slack) don't need one for device flow. Empty
        return means the device-code subsystem will not send a
        client_secret in token-exchange requests; non-empty return
        is included in those requests verbatim.

        Raises ServiceDescriptorError only when client_secret_env is
        named but the env var is missing — distinguishes "not
        configured" from "configured but unresolvable."
        """
        if self.oauth is None:
            raise ServiceDescriptorError(
                f"Service {self.service_id!r} has no oauth config; "
                f"resolve_client_secret is only valid for "
                f"oauth_device_code services."
            )
        if self.oauth.client_secret:
            return self.oauth.client_secret
        if self.oauth.client_secret_env:
            value = os.environ.get(self.oauth.client_secret_env, "").strip()
            if not value:
                raise ServiceDescriptorError(
                    f"Service {self.service_id!r} expects the OAuth "
                    f"client_secret in env var "
                    f"{self.oauth.client_secret_env!r} but it is not "
                    f"set or is empty. Set the env var and retry."
                )
            return value
        return ""


class ServiceDescriptorError(ValueError):
    """Raised when a service descriptor file or dict fails validation."""


def _validate_service_id(value: str) -> str:
    if not isinstance(value, str) or not _SERVICE_ID_RE.match(value):
        raise ServiceDescriptorError(
            f"service_id {value!r} must be lowercase alphanumeric with "
            f"underscores/hyphens (matching {_SERVICE_ID_RE.pattern})"
        )
    return value


def _validate_operations(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ServiceDescriptorError(
            f"operations must be a list of operation names; got {type(value).__name__}"
        )
    out: list[str] = []
    for op in value:
        if not isinstance(op, str) or not _OPERATION_RE.match(op):
            raise ServiceDescriptorError(
                f"operation name {op!r} must be lowercase alphanumeric "
                f"with underscores (matching {_OPERATION_RE.pattern})"
            )
        out.append(op)
    return tuple(out)


def parse_service_descriptor(data: dict[str, Any]) -> ServiceDescriptor:
    """Validate and construct a ServiceDescriptor from a dict.

    Raises ServiceDescriptorError on any validation failure.
    """
    if not isinstance(data, dict):
        raise ServiceDescriptorError(
            f"service descriptor must be a dict; got {type(data).__name__}"
        )

    service_id = _validate_service_id(data.get("service_id", ""))
    display_name = data.get("display_name", "").strip()
    if not display_name:
        raise ServiceDescriptorError("display_name must be a non-empty string")

    auth_raw = data.get("auth_type", "")
    try:
        auth_type = AuthType(auth_raw)
    except ValueError as exc:
        valid = ", ".join(a.value for a in AuthType)
        raise ServiceDescriptorError(
            f"auth_type {auth_raw!r} is not one of: {valid}. "
            f"(cookie_upload is reserved for a future spec and not yet supported.)"
        ) from exc

    operations = _validate_operations(data.get("operations"))
    audit_category = data.get("audit_category", "") or service_id
    required_scopes = tuple(data.get("required_scopes") or ())
    notes = data.get("notes", "") or ""

    oauth_section = data.get("oauth")
    oauth = _parse_oauth_section(oauth_section, service_id, auth_type)

    return ServiceDescriptor(
        service_id=service_id,
        display_name=display_name,
        auth_type=auth_type,
        operations=operations,
        audit_category=audit_category,
        required_scopes=required_scopes,
        notes=notes,
        oauth=oauth,
    )


def _parse_oauth_section(
    raw: Any, service_id: str, auth_type: AuthType,
) -> OAuthDeviceCodeConfig | None:
    """Parse + validate the oauth section.

    Required when auth_type is oauth_device_code; rejected when
    auth_type is something else.
    """
    if auth_type != AuthType.OAUTH_DEVICE_CODE:
        if raw is not None:
            raise ServiceDescriptorError(
                f"Service {service_id!r} declares an oauth section but "
                f"auth_type is {auth_type.value!r}. The oauth section is "
                f"only valid for oauth_device_code services."
            )
        return None

    if raw is None:
        raise ServiceDescriptorError(
            f"Service {service_id!r} has auth_type oauth_device_code "
            f"but no oauth section. Required fields: "
            f"device_authorization_uri, token_uri, and one of "
            f"client_id or client_id_env."
        )
    if not isinstance(raw, dict):
        raise ServiceDescriptorError(
            f"Service {service_id!r}'s oauth section must be a dict; "
            f"got {type(raw).__name__}."
        )

    device_uri = (raw.get("device_authorization_uri") or "").strip()
    token_uri = (raw.get("token_uri") or "").strip()
    if not device_uri:
        raise ServiceDescriptorError(
            f"Service {service_id!r}'s oauth section is missing "
            f"device_authorization_uri."
        )
    if not token_uri:
        raise ServiceDescriptorError(
            f"Service {service_id!r}'s oauth section is missing token_uri."
        )

    client_id = (raw.get("client_id") or "").strip()
    client_id_env = (raw.get("client_id_env") or "").strip()
    if client_id and client_id_env:
        raise ServiceDescriptorError(
            f"Service {service_id!r}'s oauth section has both client_id "
            f"and client_id_env set. Choose one: literal client_id for "
            f"OSS-public clients, client_id_env naming an env var for "
            f"closed-app clients (e.g. Google APIs)."
        )
    if not client_id and not client_id_env:
        raise ServiceDescriptorError(
            f"Service {service_id!r}'s oauth section needs exactly one "
            f"of client_id (literal) or client_id_env (env var name)."
        )

    client_secret = (raw.get("client_secret") or "").strip()
    client_secret_env = (raw.get("client_secret_env") or "").strip()
    if client_secret and client_secret_env:
        raise ServiceDescriptorError(
            f"Service {service_id!r}'s oauth section has both "
            f"client_secret and client_secret_env set. Choose one. "
            f"Most operators use client_secret_env so the secret is "
            f"sourced from the host's environment rather than committed "
            f"to the descriptor."
        )

    pkce_raw = raw.get("pkce", "optional")
    try:
        pkce = PkceMode(pkce_raw)
    except ValueError as exc:
        valid = ", ".join(m.value for m in PkceMode)
        raise ServiceDescriptorError(
            f"Service {service_id!r}'s oauth section has unknown pkce "
            f"value {pkce_raw!r}. Must be one of: {valid}."
        ) from exc

    return OAuthDeviceCodeConfig(
        device_authorization_uri=device_uri,
        token_uri=token_uri,
        client_id=client_id,
        client_id_env=client_id_env,
        client_secret=client_secret,
        client_secret_env=client_secret_env,
        pkce=pkce,
    )


def load_service_descriptor_file(path: Path) -> ServiceDescriptor:
    """Load and parse a service descriptor JSON file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ServiceDescriptorError(f"Invalid JSON in {path}: {exc}") from exc
    return parse_service_descriptor(raw)


# ---------------------------------------------------------------------------
# ServiceRegistry
# ---------------------------------------------------------------------------


class DuplicateServiceError(RuntimeError):
    """Raised when a service_id is registered twice."""


class ServiceRegistry:
    """Catalog of registered external services.

    Stock services arrive via load_stock_dir at construction or later;
    workshop-built services register dynamically. Lookups are case-
    sensitive on service_id.
    """

    def __init__(self) -> None:
        self._services: dict[str, ServiceDescriptor] = {}

    # --- registration ---

    def register(self, descriptor: ServiceDescriptor) -> ServiceDescriptor:
        """Register a service. Raises DuplicateServiceError on collision."""
        if descriptor.service_id in self._services:
            raise DuplicateServiceError(
                f"Service {descriptor.service_id!r} is already registered. "
                f"Unregister first if you want to replace it."
            )
        self._services[descriptor.service_id] = descriptor
        logger.info(
            "SERVICE_REGISTER: id=%s display=%s auth=%s operations=%d",
            descriptor.service_id,
            descriptor.display_name,
            descriptor.auth_type.value,
            len(descriptor.operations),
        )
        return descriptor

    def unregister(self, service_id: str) -> bool:
        """Remove a service from the registry. Returns True if removed."""
        return self._services.pop(service_id, None) is not None

    def load_stock_dir(self, dir_path: Path) -> int:
        """Load all *.service.json files from a directory.

        Returns the count loaded. Files that fail to parse are skipped
        with a logged warning; the registry continues with the rest.
        """
        if not dir_path.exists() or not dir_path.is_dir():
            return 0
        loaded = 0
        for path in sorted(dir_path.iterdir()):
            if not path.name.endswith(".service.json"):
                continue
            try:
                descriptor = load_service_descriptor_file(path)
                self.register(descriptor)
                loaded += 1
            except (ServiceDescriptorError, DuplicateServiceError) as exc:
                logger.warning(
                    "SERVICE_LOAD_FAILED: path=%s reason=%s", path, exc,
                )
        return loaded

    # --- read path ---

    def get(self, service_id: str) -> ServiceDescriptor | None:
        """Return the descriptor for service_id, or None if absent."""
        return self._services.get(service_id)

    def has(self, service_id: str) -> bool:
        return service_id in self._services

    def list_services(self) -> list[ServiceDescriptor]:
        """Return all registered services, sorted by service_id."""
        return [self._services[sid] for sid in sorted(self._services)]

    def supports_operation(self, service_id: str, operation: str) -> bool:
        """True if the named service is registered and declares the op."""
        descriptor = self._services.get(service_id)
        return bool(descriptor and descriptor.supports_operation(operation))
