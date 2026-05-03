"""Tool runtime context — invocation-scoped, never registration-scoped.

Per Section 7 of the the design review-revised spec: when a tool is invoked, it
receives a runtime context derived from the invoking member's
identity. Tools cannot pin to registration-time identity. The context
exposes:

  - member_id: the invoking member.
  - data_dir: the per-member tool data directory. Layout follows the
    AppData pattern: <member_data_dir>/tools/<tool_id>/.
  - credentials: the credential accessor scoped to the invoking
    member and the tool's declared service_id.
  - space: the active space context for the invocation.

This module defines the context shape; the dispatcher in C5 builds an
instance and passes it to tool implementations. Validation in
register_tool ensures tool authors don't accidentally pin to
registration-time identity, and runtime enforcement in C5 verifies
data_dir sandbox containment at every invocation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kernos.kernel.credentials_member import (
    MemberCredentialNotFound,
    MemberCredentialStore,
    StoredCredential,
)

logger = logging.getLogger(__name__)


_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _safe_name(value: str) -> str:
    """Filesystem-safe name. Mirrors the convention in credentials_member."""
    if not value or value.strip() != value:
        raise ValueError(f"Invalid identifier: {value!r}")
    if _VALID_NAME_RE.match(value):
        return value
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    if not cleaned or not _VALID_NAME_RE.match(cleaned):
        raise ValueError(f"Identifier {value!r} cannot be made safe")
    return cleaned


# ---------------------------------------------------------------------------
# Per-tool credential accessor
# ---------------------------------------------------------------------------


class ToolCredentialAccessor:
    """Member-and-service-scoped credential view handed to a tool.

    The accessor binds at invocation time to:
      - The invoking member's identifier
      - The tool's declared service_id (if any)
      - The credential store backing this install

    A tool calling `accessor.get()` receives the StoredCredential for
    its own member-and-service pair only. Tools without a service_id
    have an accessor whose `get()` always raises — they did not opt
    into the credentials surface.
    """

    def __init__(
        self,
        *,
        store: MemberCredentialStore,
        member_id: str,
        service_id: str,
    ) -> None:
        self._store = store
        self._member_id = member_id
        self._service_id = service_id

    @property
    def service_id(self) -> str:
        return self._service_id

    @property
    def member_id(self) -> str:
        return self._member_id

    @property
    def has_credential(self) -> bool:
        """True if a credential exists for this member-and-service pair."""
        if not self._service_id:
            return False
        return self._store.has(member_id=self._member_id, service_id=self._service_id)

    def get(self) -> StoredCredential:
        """Return the credential for the bound member-and-service pair.

        Raises ToolCredentialUnavailable if the tool was not declared
        as service-bound, or MemberCredentialNotFound if the member
        has not onboarded this service yet.
        """
        if not self._service_id:
            raise ToolCredentialUnavailable(
                "this tool is not service-bound (no service_id declared); "
                "credentials are not available to it"
            )
        return self._store.get(
            member_id=self._member_id, service_id=self._service_id,
        )


class ToolCredentialUnavailable(RuntimeError):
    """Raised when a tool requests credentials but isn't service-bound."""


# ---------------------------------------------------------------------------
# Runtime context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolRuntimeContext:
    """Invocation-scoped context handed to a workshop tool's implementation.

    Constructed at invocation time by the dispatcher (C5). The fields
    are read-only; tools that mutate them at runtime would be picked
    up by the runtime enforcement check on data_dir containment.

    Fields:
        member_id: invoking member's identifier.
        instance_id: invoking install's identifier (rarely needed by
            tools but available for tools that need to disambiguate
            across instances).
        space_id: active space at invocation time. May be empty for
            tools invoked outside a space.
        tool_id: the tool's name, used as the AppData-style sub-folder.
        data_dir: per-tool, per-member data directory. The tool writes
            its persistent state here. Layout:
                <install data dir>/<instance>/members/<member>/tools/<tool_id>/
        credentials: ToolCredentialAccessor scoped to this member-and-
            service pair (or a no-credentials accessor if the tool is
            not service-bound).
        agent_name: optional. The agent invoking the tool, when known
            (used for log lines; not load-bearing).
    """

    member_id: str
    instance_id: str
    space_id: str
    tool_id: str
    data_dir: Path
    credentials: ToolCredentialAccessor
    agent_name: str = ""

    @property
    def is_service_bound(self) -> bool:
        return bool(self.credentials.service_id)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def resolve_tool_data_dir(
    *,
    install_data_dir: str | Path,
    instance_id: str,
    member_id: str,
    tool_id: str,
) -> Path:
    """Resolve the per-member tool data directory.

    Layout: <install>/<instance>/members/<member>/tools/<tool_id>/.
    Created on demand. The tool writes its state files here; the
    dispatcher's runtime enforcement (C5) verifies that any path the
    tool actually opens lives within this prefix.
    """
    install = Path(install_data_dir)
    return (
        install
        / _safe_name(instance_id)
        / "members"
        / _safe_name(member_id)
        / "tools"
        / _safe_name(tool_id)
    )


def build_runtime_context(
    *,
    install_data_dir: str | Path,
    credential_store: MemberCredentialStore,
    instance_id: str,
    member_id: str,
    space_id: str,
    tool_id: str,
    service_id: str = "",
    agent_name: str = "",
) -> ToolRuntimeContext:
    """Construct an invocation-scoped runtime context.

    Ensures the per-member tool data directory exists. Creates a
    credential accessor scoped to (member_id, service_id) when the
    tool is service-bound; otherwise creates a no-credentials accessor.
    """
    data_dir = resolve_tool_data_dir(
        install_data_dir=install_data_dir,
        instance_id=instance_id,
        member_id=member_id,
        tool_id=tool_id,
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    accessor = ToolCredentialAccessor(
        store=credential_store,
        member_id=member_id,
        service_id=service_id,
    )
    return ToolRuntimeContext(
        member_id=member_id,
        instance_id=instance_id,
        space_id=space_id,
        tool_id=tool_id,
        data_dir=data_dir,
        credentials=accessor,
        agent_name=agent_name,
    )


# ---------------------------------------------------------------------------
# Sandbox containment check
# ---------------------------------------------------------------------------


def is_within_sandbox(target: Path | str, sandbox: Path) -> bool:
    """True if `target` resolves under `sandbox`.

    Used by C5's runtime enforcement to verify that the paths a tool
    opens live within its per-member data directory. Symlinks are
    resolved before comparison.
    """
    try:
        target_resolved = Path(target).resolve()
        sandbox_resolved = Path(sandbox).resolve()
    except (OSError, RuntimeError):
        return False
    try:
        target_resolved.relative_to(sandbox_resolved)
        return True
    except ValueError:
        return False


__all__ = [
    "MemberCredentialNotFound",
    "ToolCredentialAccessor",
    "ToolCredentialUnavailable",
    "ToolRuntimeContext",
    "build_runtime_context",
    "is_within_sandbox",
    "resolve_tool_data_dir",
]
