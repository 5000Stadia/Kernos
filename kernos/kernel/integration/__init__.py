"""Integration layer.

Sits between cohorts and presence in the four-layer cognition
architecture (cohorts → integration → presence → expression-future).
This package ships the foundational primitive: the briefing
schema and the integration runner. Subsequent specs build the
per-turn cohort fan-out runner, the cohort adapters, and
presence decoupling against the contract this package defines.

The runner is opt-in callable (spec acceptance criterion #13) —
nothing in the existing reasoning loop calls it yet.

The briefing is the architecture's safety surface: redaction at
the briefing boundary keeps Restricted CohortOutput content from
leaking into presence even though integration may use it to
shape the decision.
"""

from kernos.kernel.integration.briefing import (
    ActionKind,
    AuditTrace,
    Briefing,
    BriefingValidationError,
    BudgetState,
    CohortOutput,
    ConstrainedResponse,
    ContextItem,
    DecidedAction,
    Defer,
    ExecuteTool,
    FilteredItem,
    Outcome,
    Pivot,
    ProposeTool,
    Public,
    RespondOnly,
    Restricted,
    Visibility,
    VisibilityKind,
    decided_action_from_dict,
    iteration_cap_briefing,
    minimal_fail_soft_briefing,
    system_error_briefing,
    now_iso,
    visibility_from_dict,
)
from kernos.kernel.integration.runner import (
    AuditEmitter,
    ChainCaller,
    IntegrationAttemptFailed,
    IntegrationConfig,
    IntegrationInputs,
    IntegrationRunner,
    ReadOnlyToolDispatcher,
    ReadOnlyToolViolation,
    SurfacedTool,
    SURFACING_RATIONALE_CONTEXT_SPACE_PIN,
    SURFACING_RATIONALE_CREDENTIAL,
    SURFACING_RATIONALE_GATE_CLASS,
    SURFACING_RATIONALE_PINNED,
    SURFACING_RATIONALE_RELEVANCE,
)
from kernos.kernel.integration.template import (
    FINALIZE_TOOL_NAME,
    FINALIZE_TOOL_SCHEMA,
    build_system_prompt,
)

__all__ = [
    "ActionKind",
    "AuditEmitter",
    "AuditTrace",
    "Briefing",
    "BriefingValidationError",
    "BudgetState",
    "ChainCaller",
    "CohortOutput",
    "ConstrainedResponse",
    "ContextItem",
    "DecidedAction",
    "Defer",
    "ExecuteTool",
    "FilteredItem",
    "FINALIZE_TOOL_NAME",
    "FINALIZE_TOOL_SCHEMA",
    "IntegrationAttemptFailed",
    "IntegrationConfig",
    "IntegrationInputs",
    "IntegrationRunner",
    "Outcome",
    "Pivot",
    "ProposeTool",
    "Public",
    "ReadOnlyToolDispatcher",
    "ReadOnlyToolViolation",
    "RespondOnly",
    "Restricted",
    "SurfacedTool",
    "SURFACING_RATIONALE_CONTEXT_SPACE_PIN",
    "SURFACING_RATIONALE_CREDENTIAL",
    "SURFACING_RATIONALE_GATE_CLASS",
    "SURFACING_RATIONALE_PINNED",
    "SURFACING_RATIONALE_RELEVANCE",
    "Visibility",
    "VisibilityKind",
    "build_system_prompt",
    "decided_action_from_dict",
    "iteration_cap_briefing",
    "minimal_fail_soft_briefing",
    "system_error_briefing",
    "now_iso",
    "visibility_from_dict",
]
