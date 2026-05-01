"""External-agent consultation primitive.

Composes external coding-agent CLIs (Claude Code, Codex, Gemini,
Aider) behind a uniform :class:`Harness` protocol with two modes:

* **Consult** — Q&A. Kernos's primary agent reaches out for review,
  second opinion, exploratory thinking. Agent-facing surface is the
  ``consult`` tool (lands in C4).
* **Build** — task execution. The existing
  :mod:`kernos.kernel.builders` flow continues using this same
  primitive, with the ``builders/`` package preserved as a
  compatibility facade so existing callers + the
  ``KERNOS_BUILDER`` env var keep working unchanged.

Spec: ``specs/EXTERNAL-AGENT-CONSULTATION-V1.md``.
"""
from __future__ import annotations

from kernos.kernel.external_agents.consultation_log import (
    ConsultationLog,
    ConsultationRecord,
    ConsultationStatus,
)
from kernos.kernel.external_agents.errors import (
    ConsultationFailed,
    ConsultationTimeout,
    DepthExceeded,
    ExternalAgentError,
    HarnessRegistrationError,
    HarnessUnavailable,
    ReentrancyBlocked,
    WorkspaceNotAllowed,
)
from kernos.kernel.external_agents.harness import (
    BuildResult,
    ConsultResult,
    Harness,
    HarnessHealth,
)
from kernos.kernel.external_agents.orchestrator import (
    ConsultationOrchestrator,
    WorkspacePolicy,
)
from kernos.kernel.external_agents.reentrancy import (
    CallingContext,
    current_calling_context,
    current_consult_depth,
    enter_consult,
    exit_consult,
    reset_calling_context,
    set_calling_context,
)
from kernos.kernel.external_agents.registry import HarnessRegistry
from kernos.kernel.external_agents.subprocess_substrate import (
    DEFAULT_RESPONSE_CAP_BYTES,
    SubprocessResult,
    response_truncate,
    run_subprocess,
    sanitize_session_id,
)


__all__ = [
    "BuildResult",
    "CallingContext",
    "ConsultResult",
    "ConsultationFailed",
    "ConsultationLog",
    "ConsultationOrchestrator",
    "ConsultationRecord",
    "ConsultationStatus",
    "ConsultationTimeout",
    "DEFAULT_RESPONSE_CAP_BYTES",
    "DepthExceeded",
    "ExternalAgentError",
    "Harness",
    "HarnessHealth",
    "HarnessRegistrationError",
    "HarnessRegistry",
    "HarnessUnavailable",
    "ReentrancyBlocked",
    "SubprocessResult",
    "WorkspaceNotAllowed",
    "WorkspacePolicy",
    "current_calling_context",
    "current_consult_depth",
    "enter_consult",
    "exit_consult",
    "reset_calling_context",
    "response_truncate",
    "run_subprocess",
    "sanitize_session_id",
    "set_calling_context",
]
