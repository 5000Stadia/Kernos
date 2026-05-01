"""Error class hierarchy for the external_agents module.

Top-level :class:`ExternalAgentError` so callers can ``except`` the
whole module's failure surface in one line. Specific subclasses
distinguish failure modes that matter to recovery / observability.
"""
from __future__ import annotations


class ExternalAgentError(Exception):
    """Base for the external_agents module."""


class HarnessUnavailable(ExternalAgentError):
    """CLI binary not on PATH, auth missing, or harness does not
    implement the requested mode (e.g. aider asked for consult)."""


class ConsultationTimeout(ExternalAgentError):
    """Subprocess exceeded ``timeout_seconds``; killed."""


class ConsultationFailed(ExternalAgentError):
    """Subprocess exited non-zero; stderr captured in ``args[1]``
    when available."""


class WorkspaceNotAllowed(ExternalAgentError):
    """Resolved ``workspace_dir`` not under the configured allowlist."""


class ReentrancyBlocked(ExternalAgentError):
    """Consultation attempted from a calling context where the v1
    reentrancy policy disallows it (CRB dispatch, trigger
    evaluation, WLP execution, compaction, recovery sweep)."""


class DepthExceeded(ExternalAgentError):
    """Consult-within-consult exceeded the configured depth limit."""


class HarnessRegistrationError(ExternalAgentError):
    """Registry could not register or construct a harness — typically
    a programmer error (bad options, name collision)."""


__all__ = [
    "ConsultationFailed",
    "ConsultationTimeout",
    "DepthExceeded",
    "ExternalAgentError",
    "HarnessRegistrationError",
    "HarnessUnavailable",
    "ReentrancyBlocked",
    "WorkspaceNotAllowed",
]
