"""Runtime enforcement for workshop tool invocations (the design review edit 2).

Layered on top of the registration-time authoring-pattern validator
(C3). The four checks defined here run at invocation time, before
the tool's implementation receives control:

1. Hash check: descriptor JSON + implementation source unchanged
   since registration. Catches post-registration edits to either
   file. The registration hash is recorded at register_tool time
   alongside the catalog entry.

2. Operation authority re-check: the invoked operation must be in
   the tool's declared authority list. Service-bound tools also
   re-validate against the service's declared operations, so a
   service descriptor edit that removes an operation invalidates
   any tool that still depends on it.

3. Credential scope re-check: service-bound tools must have a
   non-expired credential for the invoking member. The credential
   accessor is re-bound to the runtime context at every invocation;
   this is a pre-flight check that surfaces "credential missing or
   expired" cleanly rather than letting the tool's HTTP call fail
   later.

4. Data dir sandbox enforcement: provided as a verification helper
   the dispatcher and post-invocation review can call. Full
   sandbox enforcement at runtime requires subprocess isolation
   (a future spec); the in-process check is "best effort where
   feasible" per the spec language. Force-registered tools are
   subject to the same enforcement as ordinary tools — force
   bypasses authoring-pattern validation only, never runtime
   isolation (the design review edit 5).

All checks raise `RuntimeEnforcementError` (or a subclass) on
failure. The dispatcher catches and renders a clean error to the
agent / member rather than letting the tool run.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from kernos.kernel.credentials_member import (
    MemberCredentialNotFound,
    MemberCredentialStore,
)
from kernos.kernel.services import AuthType, ServiceRegistry
from kernos.kernel.tool_descriptor import ToolDescriptor
from kernos.kernel.tool_runtime import (
    ToolRuntimeContext,
    is_within_sandbox,
)
from kernos.setup.service_state import ServiceStateStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RuntimeEnforcementError(RuntimeError):
    """Base class for runtime enforcement failures.

    Subclasses correspond to the four checks so dispatcher code can
    branch (e.g., re-onboard a credential vs. surface a hash mismatch).
    """


class HashMismatchError(RuntimeEnforcementError):
    """Descriptor or implementation source changed since registration."""


class AuthorityViolationError(RuntimeEnforcementError):
    """Invoked operation not in the tool's declared authority."""


class CredentialUnavailableError(RuntimeEnforcementError):
    """Service-bound tool has no credential or it is expired."""


class SandboxViolationError(RuntimeEnforcementError):
    """Path operation reached outside the per-member tool data directory."""


class ServiceDisabledError(RuntimeEnforcementError):
    """Service-bound tool invocation refused because the service is disabled.

    Per the INSTALL-FOR-STOCK-CONNECTORS spec Section 2: dispatch
    is the security boundary for the disabled flag. Surfacing
    reduces visibility but is a UX filter; a leaked tool ID could
    invoke a disabled service if dispatch didn't enforce. This
    error closes that window.
    """


# ---------------------------------------------------------------------------
# Registration hash
# ---------------------------------------------------------------------------


def compute_registration_hash(
    descriptor_json: str | bytes,
    implementation_source: str | bytes,
) -> str:
    """Hex SHA-256 of (descriptor JSON || implementation source).

    Stable across registrations of the same content; changes if either
    the descriptor or the implementation is edited. Stored at
    registration time and re-computed at every invocation by Check 1.
    """
    h = hashlib.sha256()
    if isinstance(descriptor_json, str):
        descriptor_json = descriptor_json.encode("utf-8")
    if isinstance(implementation_source, str):
        implementation_source = implementation_source.encode("utf-8")
    h.update(descriptor_json)
    # A separator that cannot appear in JSON output prevents
    # collisions between (descriptor, impl) and a single concatenated
    # blob with the same byte sequence.
    h.update(b"\x00\x00")
    h.update(implementation_source)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Check 1: hash unchanged since registration
# ---------------------------------------------------------------------------


def check_hash_unchanged(
    *,
    descriptor_path: Path,
    implementation_path: Path,
    registered_hash: str,
) -> None:
    """Raise HashMismatchError if the on-disk content has changed.

    Reads both files fresh on every call. The dispatcher supplies the
    paths and the registered hash from the catalog entry.
    """
    try:
        descriptor_bytes = descriptor_path.read_bytes()
        impl_bytes = implementation_path.read_bytes()
    except OSError as exc:
        raise HashMismatchError(
            f"Tool's source files cannot be read at invocation time: {exc}. "
            f"Re-register the tool or restore the files."
        ) from exc

    current = compute_registration_hash(descriptor_bytes, impl_bytes)
    if current != registered_hash:
        raise HashMismatchError(
            "Tool's descriptor or implementation has been edited since "
            "registration. Re-register the tool to update its registered "
            "hash, or restore the files to their registered state. "
            f"(registered={registered_hash[:12]}..., "
            f"current={current[:12]}...)"
        )


# ---------------------------------------------------------------------------
# Check 2: operation in authority
# ---------------------------------------------------------------------------


def check_operation_authority(
    *,
    descriptor: ToolDescriptor,
    operation: str,
    service_registry: ServiceRegistry | None = None,
) -> None:
    """Raise AuthorityViolationError if the invoked operation is not allowed.

    For tools that declare authority, the operation must be in the
    list. Service-bound tools additionally cross-check that the
    operation is still in the service's declared operations (catches
    descriptor-edit drift between tool and service).

    A blank operation is treated as "the tool dispatches a single
    kind of work and the caller did not name a specific operation."
    That path is allowed when the descriptor's authority list is
    empty (no per-operation authority declared).
    """
    if not operation:
        if descriptor.authority:
            raise AuthorityViolationError(
                f"Tool {descriptor.name!r} declares per-operation "
                f"authority but the invocation did not name an "
                f"operation. Authority list: "
                f"{', '.join(descriptor.authority)}."
            )
        return  # No authority list, no operation: pass.

    if descriptor.authority and operation not in descriptor.authority:
        raise AuthorityViolationError(
            f"Operation {operation!r} is not in tool {descriptor.name!r}'s "
            f"declared authority. Authority list: "
            f"{', '.join(descriptor.authority)}."
        )

    if descriptor.is_service_bound and service_registry is not None:
        service = service_registry.get(descriptor.service_id)
        if service is None:
            raise AuthorityViolationError(
                f"Tool {descriptor.name!r} is bound to service "
                f"{descriptor.service_id!r}, which is no longer registered."
            )
        if operation and not service.supports_operation(operation):
            raise AuthorityViolationError(
                f"Operation {operation!r} is no longer in the operations "
                f"declared by service {descriptor.service_id!r}. The "
                f"service's current operations: "
                f"{', '.join(service.operations) or '(none)'}."
            )


# ---------------------------------------------------------------------------
# Check 3: credential scope
# ---------------------------------------------------------------------------


def check_credential_scope(
    *,
    descriptor: ToolDescriptor,
    member_id: str,
    credential_store: MemberCredentialStore,
    service_registry: ServiceRegistry | None = None,
) -> None:
    """Raise CredentialUnavailableError when a service-bound tool's
    credential is missing or expired.

    Tools that are not service-bound pass this check trivially.

    Auto-refresh (OAUTH-DEVICE-CODE-SUBSYSTEM Q4 follow-on): when an
    expired credential carries a refresh_token AND the service is an
    oauth_device_code service AND a service_registry is supplied,
    the runtime attempts an in-line refresh before raising. Success
    rotates the credential in place and the check returns cleanly.
    Refresh-endpoint failures (revoked grant, network error) fall
    through to the original "expired" error path so the user is
    asked to re-onboard.

    api_token services and credentials without a refresh_token skip
    the refresh attempt and raise immediately, matching the prior
    behaviour.
    """
    if not descriptor.is_service_bound:
        return
    try:
        credential = credential_store.get(
            member_id=member_id, service_id=descriptor.service_id,
        )
    except MemberCredentialNotFound as exc:
        raise CredentialUnavailableError(
            f"No credential for member={member_id} service="
            f"{descriptor.service_id!r}. Run the auth onboarding flow "
            f"on a compatible channel first."
        ) from exc
    if credential.is_expired:
        if _try_auto_refresh(
            descriptor=descriptor,
            credential=credential,
            member_id=member_id,
            credential_store=credential_store,
            service_registry=service_registry,
        ):
            return
        raise CredentialUnavailableError(
            f"Credential for service {descriptor.service_id!r} has "
            f"expired (expires_at={credential.expires_at}). Re-run "
            f"the auth onboarding flow or rotate the credential."
        )


def _try_auto_refresh(
    *,
    descriptor: ToolDescriptor,
    credential,
    member_id: str,
    credential_store: MemberCredentialStore,
    service_registry: ServiceRegistry | None,
) -> bool:
    """Attempt OAuth refresh for an expired credential. Returns True
    when the refresh succeeded (credential rotated in store), False
    when refresh is not applicable or failed.

    Applicable iff: registry supplied, descriptor maps to an oauth_
    device_code service, refresh_token present on the credential.
    """
    if service_registry is None:
        return False
    if not credential.refresh_token:
        return False
    service = service_registry.get(descriptor.service_id)
    if service is None or service.auth_type != AuthType.OAUTH_DEVICE_CODE:
        return False
    # Local import keeps the heavy oauth surface lazy at module load.
    from kernos.kernel.oauth_device_code import (
        DeviceCodeNetworkError,
        TokenEndpointError,
        refresh_credential,
    )
    try:
        rotated = refresh_credential(
            service=service, member_id=member_id,
            store=credential_store,
        )
    except (TokenEndpointError, DeviceCodeNetworkError) as exc:
        logger.warning(
            "CREDENTIAL_REFRESH_FAILED: member=%s service=%s err=%s",
            member_id, descriptor.service_id, exc,
        )
        return False
    # Codex pre-push fold: refresh_credential maps a missing or
    # zero ``expires_in`` to ``expires_at=None``, and
    # ``MemberCredentialStore.rotate`` treats ``expires_at=None`` as
    # "preserve the prior value" (credentials_member.py:320). The
    # access token rotates but the original expired timestamp can
    # survive — the gate would incorrectly accept a still-expired
    # credential. Re-check is_expired on the rotated record; only
    # signal success when the rotation actually delivered a valid
    # window.
    if rotated.is_expired:
        logger.warning(
            "CREDENTIAL_REFRESH_NO_FRESH_EXPIRY: member=%s service=%s "
            "rotated record is still expired (expires_at=%s) — token "
            "endpoint response likely omitted expires_in",
            member_id, descriptor.service_id, rotated.expires_at,
        )
        return False
    logger.info(
        "CREDENTIAL_REFRESH_OK: member=%s service=%s",
        member_id, descriptor.service_id,
    )
    return True


# ---------------------------------------------------------------------------
# Check 4: sandbox enforcement
# ---------------------------------------------------------------------------


def check_service_enabled(
    *,
    descriptor: ToolDescriptor,
    service_state_store: "ServiceStateStore | None",
) -> None:
    """Raise ServiceDisabledError when a service-bound tool's service
    is disabled in the install state.

    Tools that are not service-bound pass trivially. When no
    service_state_store is provided (legacy callers, tests),
    enforcement is skipped — any caller that wants the disabled
    flag honored must pass the store.
    """
    if not descriptor.is_service_bound or service_state_store is None:
        return
    if not service_state_store.is_enabled(descriptor.service_id):
        raise ServiceDisabledError(
            f"service {descriptor.service_id!r} is disabled for this "
            f"install. Enable it with `kernos services enable "
            f"{descriptor.service_id}` or run `kernos setup` to "
            f"reconfigure."
        )


def check_sandbox_path(*, target: Path | str, context: ToolRuntimeContext) -> None:
    """Raise SandboxViolationError if `target` falls outside the
    invocation's data_dir.

    Best-effort in-process check. Full subprocess-level isolation is a
    future spec; the in-process variant is "where feasible" per the
    spec language. Helpers using this check verify paths the tool
    returns or paths the dispatcher inspects after the call; tools
    written against context.data_dir pass naturally, and the
    authoring-pattern validator (C3) catches the most common bypass
    patterns at registration time so this check is a backstop, not
    the primary defence.
    """
    if not is_within_sandbox(target, context.data_dir):
        raise SandboxViolationError(
            f"Path {target!s} resolves outside the tool's per-member "
            f"data directory ({context.data_dir}). Tools must read and "
            f"write under context.data_dir; reaching outside is the "
            f"equivalent of an app writing to System32 instead of "
            f"AppData."
        )


# ---------------------------------------------------------------------------
# Composed enforcement entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnforcementInputs:
    """Bundle of everything enforce_invocation needs.

    Constructed by the dispatcher at invocation time. Keeping the
    function signature small means the dispatcher does not have to
    branch on which checks apply.
    """

    descriptor: ToolDescriptor
    operation: str
    descriptor_path: Path
    implementation_path: Path
    registered_hash: str
    member_id: str
    credential_store: MemberCredentialStore
    service_registry: ServiceRegistry | None = None
    service_state_store: ServiceStateStore | None = None


def enforce_invocation(inputs: EnforcementInputs) -> None:
    """Run all four checks. First failure raises; subsequent are not run.

    This is the entry point the dispatcher calls before handing
    control to the tool's implementation. Force-registered tools go
    through this same path — the design review edit 5: force bypasses authoring-
    pattern validation only, not runtime isolation.
    """
    check_hash_unchanged(
        descriptor_path=inputs.descriptor_path,
        implementation_path=inputs.implementation_path,
        registered_hash=inputs.registered_hash,
    )
    check_operation_authority(
        descriptor=inputs.descriptor,
        operation=inputs.operation,
        service_registry=inputs.service_registry,
    )
    # Check 5 (INSTALL-FOR-STOCK-CONNECTORS): refuse service-bound
    # invocation when the service is disabled. Runs before the
    # credential check because a disabled service shouldn't even
    # query the credential store. When no service_state_store is
    # supplied, this check is skipped — but production callers
    # (workspace dispatch) always pass the store.
    check_service_enabled(
        descriptor=inputs.descriptor,
        service_state_store=inputs.service_state_store,
    )
    check_credential_scope(
        descriptor=inputs.descriptor,
        member_id=inputs.member_id,
        credential_store=inputs.credential_store,
        service_registry=inputs.service_registry,
    )
    # Sandbox check is per-path; the dispatcher calls
    # check_sandbox_path on any user-supplied path inputs and on
    # paths the tool returns. enforce_invocation does not iterate
    # over arbitrary user input here — that's the dispatcher's job
    # since it knows which input fields are paths.


__all__ = [
    "AuthorityViolationError",
    "CredentialUnavailableError",
    "EnforcementInputs",
    "HashMismatchError",
    "RuntimeEnforcementError",
    "SandboxViolationError",
    "ServiceDisabledError",
    "check_credential_scope",
    "check_hash_unchanged",
    "check_operation_authority",
    "check_sandbox_path",
    "check_service_enabled",
    "compute_registration_hash",
    "enforce_invocation",
]
