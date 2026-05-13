"""Workflow step reference resolver.

WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 3. Resolves template
references in action parameters AND predicate ``value:`` fields
against the per-execution resolution context. Four namespaces with
prefix dispatch:

    {workflow.<key>}              fixed placeholders (Spec 3 baseline)
    {idea_payload.<path>}         workflow trigger event payload
    {step.<step_id>.<scope>.<path>}  step output / receipt / error / success
    {gate.<gate_name>.output.<path>} gate-release event payload

Resolution failure modes (Decision 3 v2):

- Static failures (registration-time): branch targets unknown,
  parameter references unknown step IDs. Caught by validators.
- Dynamic failures in action-parameter context: referenced step
  output missing OR path doesn't traverse → ``RefResolutionError``
  raised → workflow aborts via per-outcome aborting-failure matrix.
- Dynamic failures in predicate-evaluation context: same conditions
  → predicate returns False (no match) — composes with the
  request-and-wait pattern where a predicate may reference a future
  step's output until that step completes.

Type preservation (Decision 3 v2):

- Sole-reference shortcut: if the whole string IS a single
  reference (e.g., ``'{step.X.output.flag}'``), return the resolved
  native value (bool stays bool, dict stays dict).
- Mixed-string substitution: ``'prefix-{step.X.output.id}-suffix'``
  stringifies each reference and concatenates.

Identifier grammar (Decision 0 v2 / Codex Medium 9): step IDs,
gate names, terminal branch names match
``[A-Za-z][A-Za-z0-9_-]*``. Enforced by ``validate_identifier``
helper called from workflow registration.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kernos.kernel.workflows.execution_engine import WorkflowExecution


# Reference token format: {namespace.path.with.dots}
_REFERENCE_PATTERN = re.compile(r"\{([^{}]+)\}")


# Identifier grammar — Decision 0 v2 / Codex Medium 9.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


# Recognised single placeholders in the ``{workflow.X}`` namespace
# (matches the existing _interpolate_params discipline from
# execution_engine.py).
_WORKFLOW_KEYS = frozenset({
    "execution_id", "gate_nonce", "correlation_id",
    "workflow_id", "instance_id",
})


# Recognised scopes for ``{step.<id>.<scope>.<path>}``. Maps to
# fields in the captured envelope from step_outputs.py.
_STEP_SCOPES = frozenset({"output", "receipt", "error", "success", "value"})


class RefResolutionError(ValueError):
    """Raised when a template reference can't be resolved."""


class IdentifierGrammarError(ValueError):
    """Raised when a step ID / gate name / terminal branch name
    violates the grammar regex."""


@dataclass
class ResolutionContext:
    """Per-execution state the resolver needs.

    Constructed by the engine before each step's parameter
    resolution AND before each predicate evaluation. The engine
    loads ``step_outputs`` + ``gate_outputs`` from the
    workflow_step_outputs table via load_workflow_outputs.

    ``pending_gate_nonce`` carries the engine-minted nonce for the
    current gated step BEFORE the gate-await begins. This shadows
    ``execution.gate_nonce`` (which is set on the execution row only
    AFTER the action executes successfully) so action parameters
    that reference ``{workflow.gate_nonce}`` resolve to the minted
    nonce at action-dispatch time. Mirrors the original
    ``_interpolate_params`` discipline.
    """
    execution: "WorkflowExecution"
    trigger_payload: dict = field(default_factory=dict)
    step_outputs: dict[str, dict] = field(default_factory=dict)
    gate_outputs: dict[str, dict] = field(default_factory=dict)
    pending_gate_nonce: str = ""
    # Resolution context mode: 'parameter' raises RefResolutionError
    # on dynamic failures; 'predicate' returns _NOT_FOUND so the
    # caller can route to "predicate didn't match" rather than abort
    # the workflow.
    mode: str = "parameter"


_NOT_FOUND = object()


def validate_identifier(value: str, *, ctx: str = "identifier") -> None:
    """Raise IdentifierGrammarError if ``value`` doesn't match the
    grammar regex. Used at workflow registration time.
    """
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise IdentifierGrammarError(
            f"{ctx} {value!r} must match {_IDENTIFIER_RE.pattern}"
        )


def resolve_references_in_value(
    value: Any, ctx: ResolutionContext,
) -> Any:
    """Recursively walk a parameter value and substitute references.

    Type preservation: when the entire value IS a single reference
    (e.g., ``'{step.X.output.flag}'``), the resolved value's native
    type is returned. Mixed strings stringify references.
    """
    if isinstance(value, str):
        return _resolve_string(value, ctx)
    if isinstance(value, dict):
        return {k: resolve_references_in_value(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_references_in_value(item, ctx) for item in value]
    if isinstance(value, tuple):
        return tuple(resolve_references_in_value(item, ctx) for item in value)
    return value


def _resolve_string(template: str, ctx: ResolutionContext) -> Any:
    """Substitute references in a string template.

    Sole-reference shortcut: if the entire template IS one
    reference, return the resolved native value (preserves type).
    Otherwise: substitute each reference's resolved value as str().
    """
    matches = list(_REFERENCE_PATTERN.finditer(template))
    if not matches:
        return template

    # Sole-reference shortcut.
    if (
        len(matches) == 1
        and matches[0].start() == 0
        and matches[0].end() == len(template)
    ):
        resolved = _resolve_one(matches[0].group(1), ctx)
        if resolved is _NOT_FOUND:
            if ctx.mode == "predicate":
                # Caller (predicate evaluator) reads _NOT_FOUND as
                # "no match this evaluation"; gate stays paused.
                return _NOT_FOUND
            raise RefResolutionError(
                f"reference {matches[0].group(1)!r} unresolved"
            )
        return resolved

    # Mixed: stringify each.
    out_parts: list[str] = []
    cursor = 0
    for match in matches:
        out_parts.append(template[cursor:match.start()])
        resolved = _resolve_one(match.group(1), ctx)
        if resolved is _NOT_FOUND:
            if ctx.mode == "predicate":
                return _NOT_FOUND
            raise RefResolutionError(
                f"reference {match.group(1)!r} unresolved"
            )
        out_parts.append(str(resolved))
        cursor = match.end()
    out_parts.append(template[cursor:])
    return "".join(out_parts)


def _resolve_one(reference: str, ctx: ResolutionContext) -> Any:
    """Resolve a single reference path. Returns _NOT_FOUND if the
    reference can't resolve (caller decides whether to raise per
    ctx.mode)."""
    segments = reference.split(".")
    if not segments or not segments[0]:
        return _NOT_FOUND
    head = segments[0]
    if head == "workflow":
        return _resolve_workflow(segments[1:], ctx)
    if head == "idea_payload":
        return _resolve_path(ctx.trigger_payload, segments[1:])
    if head == "step":
        return _resolve_step(segments[1:], ctx)
    if head == "gate":
        return _resolve_gate(segments[1:], ctx)
    return _NOT_FOUND


def _resolve_workflow(rest: list[str], ctx: ResolutionContext) -> Any:
    if not rest:
        return _NOT_FOUND
    key = rest[0]
    if key not in _WORKFLOW_KEYS:
        return _NOT_FOUND
    if key == "gate_nonce" and ctx.pending_gate_nonce:
        # The minted nonce takes precedence at parameter-resolution
        # time; this is the value the gated action's payload needs to
        # carry so the post-flush match logic recognizes it.
        return ctx.pending_gate_nonce
    return getattr(ctx.execution, key, _NOT_FOUND)


def _resolve_step(rest: list[str], ctx: ResolutionContext) -> Any:
    # Expected: <step_id>.<scope>[.<path>...]
    if len(rest) < 2:
        return _NOT_FOUND
    step_id = rest[0]
    scope = rest[1]
    if step_id not in ctx.step_outputs:
        return _NOT_FOUND
    envelope = ctx.step_outputs[step_id]
    if scope not in _STEP_SCOPES:
        return _NOT_FOUND
    # Map scope to envelope field.
    if scope == "output":
        base = envelope.get("value")
    elif scope == "receipt":
        base = envelope.get("receipt")
    elif scope == "error":
        base = envelope.get("error")
    elif scope == "success":
        base = envelope.get("success")
    elif scope == "value":
        base = envelope.get("value")
    else:
        return _NOT_FOUND
    if len(rest) == 2:
        return base
    return _resolve_path(base, rest[2:])


def _resolve_gate(rest: list[str], ctx: ResolutionContext) -> Any:
    # Expected: <gate_name>.output[.<path>...]
    if len(rest) < 2:
        return _NOT_FOUND
    gate_name = rest[0]
    scope = rest[1]
    if scope != "output":
        # Only "output" supported for gates in v1.
        return _NOT_FOUND
    if gate_name not in ctx.gate_outputs:
        return _NOT_FOUND
    envelope = ctx.gate_outputs[gate_name]
    base = envelope.get("value")  # contains {"payload": event_payload}
    if len(rest) == 2:
        return base
    return _resolve_path(base, rest[2:])


def _resolve_path(obj: Any, path: list[str]) -> Any:
    """Walk a dotted path against a dict. Returns _NOT_FOUND on miss."""
    cur = obj
    for segment in path:
        if isinstance(cur, dict) and segment in cur:
            cur = cur[segment]
        else:
            return _NOT_FOUND
    return cur


def extract_references(template: str) -> list[str]:
    """Return all reference tokens in a template string. Used by
    workflow validators for static reference-target checking.
    """
    if not isinstance(template, str):
        return []
    return [m.group(1) for m in _REFERENCE_PATTERN.finditer(template)]


def extract_references_in_value(value: Any) -> list[str]:
    """Recursively extract reference tokens from a parameter value.
    Used at registration time to identify all step IDs / gate names
    a workflow descriptor references.
    """
    refs: list[str] = []
    if isinstance(value, str):
        refs.extend(extract_references(value))
    elif isinstance(value, dict):
        for v in value.values():
            refs.extend(extract_references_in_value(v))
    elif isinstance(value, (list, tuple)):
        for item in value:
            refs.extend(extract_references_in_value(item))
    return refs


def parse_reference_head(reference: str) -> tuple[str, str]:
    """Return (namespace, target) for static analysis. Target is the
    second segment (step_id, gate_name, or workflow key). Used by
    validate_workflow to check that all referenced step IDs / gate
    names exist.
    """
    segments = reference.split(".")
    if len(segments) < 2:
        return "", ""
    return segments[0], segments[1]


__all__ = [
    "IdentifierGrammarError",
    "RefResolutionError",
    "ResolutionContext",
    "extract_references",
    "extract_references_in_value",
    "parse_reference_head",
    "resolve_references_in_value",
    "validate_identifier",
]
