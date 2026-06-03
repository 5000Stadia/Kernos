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


class ConsultationStalled(ConsultationTimeout):
    """Subprocess stopped emitting ACP events before the total timeout."""

    def __init__(
        self,
        *args: object,
        last_event_kind: str = "",
        silence_seconds: float = 0.0,
        event_count: int = 0,
        attempts: int = 1,
        last_reason: str = "idle_stall",
    ) -> None:
        super().__init__(*args)
        self.last_event_kind = last_event_kind
        self.silence_seconds = silence_seconds
        self.event_count = event_count
        self.attempts = attempts
        self.last_reason = last_reason


class ConsultationFailed(ExternalAgentError):
    """Subprocess exited non-zero; stderr captured in ``args[1]``
    when available. ``exit_status`` carries the subprocess return
    code so audit rows distinguish exit codes (AC16). ``diagnostics``
    (AGENT-CONSULT-CHANNEL-V1 Stage 1c) carries the structured
    stream context (event_count, last_event_kind, stop_reason,
    stderr_tail, …) so EVERY failure — not just stalls — is legible
    up the ladder."""

    def __init__(
        self, *args: object, exit_status: int = 0,
        diagnostics: dict | None = None,
    ) -> None:
        super().__init__(*args)
        self.exit_status = exit_status
        self.diagnostics = dict(diagnostics or {})


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


def consultation_diagnostics(exc: BaseException) -> dict:
    """AGENT-CONSULT-CHANNEL-V1 Stage 1b: extract a legible, structured
    diagnostic dict from any consult exception so the failure can be
    attributed up the ladder instead of flattened to ``str(exc)[:300]``.
    Safe on any exception (returns at least exc_type + message)."""
    diag: dict = {
        "exc_type": type(exc).__name__,
        "message": str(exc)[:300],
    }
    for field_name in (
        "last_event_kind", "silence_seconds", "event_count",
        "attempts", "last_reason", "exit_status",
    ):
        if hasattr(exc, field_name):
            diag[field_name] = getattr(exc, field_name)
    # Merge any structured diagnostics dict the raise site attached
    # (covers non-stall failures: timeout, non-zero rc, no-response).
    extra = getattr(exc, "diagnostics", None)
    if isinstance(extra, dict):
        for k, v in extra.items():
            diag.setdefault(k, v)
    return diag


__all__ = [
    "ConsultationFailed",
    "ConsultationStalled",
    "ConsultationTimeout",
    "DepthExceeded",
    "ExternalAgentError",
    "HarnessRegistrationError",
    "HarnessUnavailable",
    "ReentrancyBlocked",
    "WorkspaceNotAllowed",
    "consultation_diagnostics",
]
