"""Portable workflow descriptor parser.

Loads ``.workflow.yaml`` / ``.workflow.json`` / ``.workflow.md`` files
into the dataclass-shaped :class:`Workflow` form. The descriptor schema
matches the dataclass shape exactly; field-level validation errors
are raised as :class:`DescriptorError` with a clear message naming the
offending field path.

Markdown variant: a ``.workflow.md`` file has YAML frontmatter delimited
by ``---`` lines; the frontmatter parses as the structured form, and
the markdown body becomes the ``description`` field.

Sharing constraint: instance-specific field paths must be either
parameterised (``{installer.<name>}`` placeholder) or guarded by
``instance_local: true`` at the top level. The allowlist of
instance-specific paths matches the spec's narrow-fix list.

Predicate handling: this loader accepts predicates in the canonical
AST form only. Expression-string DSL compilation is deferred to C6's
trigger_compiler module; until that ships, a string-shaped predicate
raises a clear :class:`DescriptorError` pointing operators at the
canonical AST shape.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from kernos.kernel.workflows.workflow_registry import (
    ActionDescriptor,
    ApprovalGate,
    Bounds,
    ContinuationRules,
    TriggerDescriptor,
    Verifier,
    Workflow,
)


class DescriptorError(ValueError):
    """Raised when a workflow descriptor file fails parsing or
    field-level validation."""


# ---------------------------------------------------------------------------
# Sharing constraint
# ---------------------------------------------------------------------------


# Field paths inside a parsed descriptor that, if they reference a
# concrete value, MUST be either parameterised or guarded by
# instance_local: true. Match-paths use a dotted notation with ``[]``
# for list indices replaced by ``[*]``.
INSTANCE_SPECIFIC_PATHS = (
    "instance_id",
    "trigger.actor_filter",
    "trigger.correlation_filter",
    "approval_gates[*].approval_event_predicate",
    "action_sequence[*].parameters.member_id",
    "action_sequence[*].parameters.space_id",
    "action_sequence[*].parameters.canvas_id",
    "action_sequence[*].parameters.agent_id",
    "action_sequence[*].parameters.channel_id",
    "action_sequence[*].parameters.service_id",
)

_INSTALLER_PLACEHOLDER_RE = re.compile(r"^\s*\{installer\.[A-Za-z_][\w]*\}\s*$")


def _is_parameterised(value: Any) -> bool:
    """Return True if ``value`` is a string that's a single
    ``{installer.<name>}`` placeholder (and only that)."""
    return isinstance(value, str) and bool(_INSTALLER_PLACEHOLDER_RE.match(value))


def _walk_for_sharing_violations(
    body: dict, instance_local: bool,
) -> list[str]:
    """Walk the body looking for instance-specific values that would
    make the workflow unshareable. Returns a list of offending field
    paths; an empty list means the workflow is shareable.

    When ``instance_local`` is True the workflow is explicitly opted
    out of sharing and no checks run."""
    if instance_local:
        return []
    violations: list[str] = []

    def _check(value: Any, path: str) -> None:
        if value in (None, "", [], {}):
            return
        if _is_parameterised(value):
            return
        violations.append(path)

    # Top-level ``instance_id`` is set per installation (the installer
    # provides their own instance), so it is intentionally NOT part of
    # the sharing-constraint surface. The sharing concern is for
    # references INSIDE predicates and action parameters.

    trigger = body.get("trigger") or {}
    _check(trigger.get("actor_filter"), "trigger.actor_filter")
    _check(trigger.get("correlation_filter"), "trigger.correlation_filter")

    # approval_gates predicates: only flag if they reference instance-specific
    # paths in their leaf nodes — covered by the predicate AST visitor below.
    for idx, gate in enumerate(body.get("approval_gates") or []):
        pred = gate.get("approval_event_predicate")
        if pred is not None:
            for offending in _predicate_instance_specific_leaves(pred):
                violations.append(
                    f"approval_gates[{idx}].approval_event_predicate.{offending}"
                )

    for idx, action in enumerate(body.get("action_sequence") or []):
        params = action.get("parameters") or {}
        for key in ("member_id", "space_id", "canvas_id", "agent_id",
                    "channel_id", "service_id"):
            if key in params:
                _check(params[key], f"action_sequence[{idx}].parameters.{key}")

    return violations


def _predicate_instance_specific_leaves(ast: Any) -> list[str]:
    """Return a list of leaf-locations within a predicate AST that
    reference instance-specific concrete values. Predicates that
    reference such values must be parameterised; flagging here lets
    the caller assemble a violation path."""
    out: list[str] = []
    if not isinstance(ast, dict):
        return out
    op = ast.get("op")
    if op in {"AND", "OR"}:
        for idx, child in enumerate(ast.get("operands") or []):
            for sub in _predicate_instance_specific_leaves(child):
                out.append(f"operands[{idx}].{sub}")
        return out
    if op == "NOT":
        operand = ast.get("operand")
        if operand is not None:
            for sub in _predicate_instance_specific_leaves(operand):
                out.append(f"operand.{sub}")
        return out
    # Leaf operators: actor_eq / correlation_eq target instance-specific
    # surfaces; eq with path "member_id" / "instance_id" / "space_id" too.
    if op == "actor_eq" and not _is_parameterised(ast.get("value")):
        out.append("value")
    elif op == "correlation_eq" and not _is_parameterised(ast.get("value")):
        out.append("value")
    elif op == "eq":
        path = ast.get("path", "")
        if path in {"instance_id", "member_id", "space_id"}:
            if not _is_parameterised(ast.get("value")):
                out.append("value")
    return out


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


_FRONTMATTER_DELIM = "---"


def parse_descriptor(file_path: str | Path) -> Workflow:
    """Top-level entry point. Routes to the appropriate loader based
    on file extension. Raises :class:`DescriptorError` on any
    failure."""
    path = Path(file_path)
    if not path.exists():
        raise DescriptorError(f"descriptor file not found: {path}")
    suffix = "".join(path.suffixes).lower()
    if suffix.endswith(".yaml") or suffix.endswith(".yml"):
        body, narrative = _load_yaml(path), ""
    elif suffix.endswith(".json"):
        body, narrative = _load_json(path), ""
    elif suffix.endswith(".md"):
        body, narrative = _load_markdown(path)
    else:
        raise DescriptorError(
            f"unrecognised descriptor extension {suffix!r}; expected "
            f".workflow.yaml / .workflow.json / .workflow.md"
        )
    return _build_workflow(body, narrative_description=narrative)


def _load_yaml(path: Path) -> dict:
    try:
        with path.open("r") as fp:
            data = yaml.safe_load(fp)
    except yaml.YAMLError as exc:
        raise DescriptorError(f"YAML parse error in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DescriptorError(f"YAML descriptor must be a mapping, got {type(data).__name__}")
    return data


def _load_json(path: Path) -> dict:
    try:
        with path.open("r") as fp:
            data = json.load(fp)
    except json.JSONDecodeError as exc:
        raise DescriptorError(f"JSON parse error in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DescriptorError(f"JSON descriptor must be a mapping, got {type(data).__name__}")
    return data


def _load_markdown(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        raise DescriptorError(
            f"markdown descriptor {path} must start with YAML frontmatter "
            f"delimited by '---' lines"
        )
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == _FRONTMATTER_DELIM:
            end_idx = idx
            break
    if end_idx is None:
        raise DescriptorError(
            f"markdown descriptor {path} missing closing '---' frontmatter "
            f"delimiter"
        )
    fm_text = "\n".join(lines[1:end_idx])
    body_text = "\n".join(lines[end_idx + 1:]).strip()
    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise DescriptorError(f"YAML frontmatter parse error in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DescriptorError(
            f"markdown frontmatter in {path} must be a mapping, got "
            f"{type(data).__name__}"
        )
    return data, body_text


# ---------------------------------------------------------------------------
# Build Workflow from parsed body
# ---------------------------------------------------------------------------


def _require(body: dict, key: str, ctx: str = "descriptor") -> Any:
    if key not in body:
        raise DescriptorError(f"{ctx} missing required field {key!r}")
    return body[key]


def _build_bounds(body: dict) -> Bounds:
    raw = body.get("bounds")
    if not isinstance(raw, dict):
        raise DescriptorError("bounds must be a mapping")
    return Bounds(
        iteration_count=raw.get("iteration_count"),
        wall_time_seconds=raw.get("wall_time_seconds"),
        cost_usd=raw.get("cost_usd"),
        composite=raw.get("composite"),
    )


def _build_verifier(body: dict) -> Verifier:
    raw = body.get("verifier")
    if not isinstance(raw, dict):
        raise DescriptorError("verifier must be a mapping")
    return Verifier(
        flavor=_require(raw, "flavor", "verifier"),
        check=_require(raw, "check", "verifier"),
    )


def _build_predicate(raw: Any, *, ctx: str) -> dict:
    """Compile a descriptor predicate to canonical AST.

    Accepts both canonical AST (dict) and expression-string DSL
    (str). The DSL form compiles via :mod:`trigger_compiler`'s
    deterministic parser. English-form predicates are NOT auto-
    compiled here — operators wanting English compilation should
    either pre-compile via ``trigger_compiler.compile_predicate_source``
    with their LLM bound, or author the predicate as DSL / AST."""
    if isinstance(raw, str):
        from kernos.kernel.workflows.trigger_compiler import (
            CompilerError,
            compile_dsl,
        )
        try:
            return compile_dsl(raw)
        except CompilerError as exc:
            raise DescriptorError(
                f"{ctx} DSL compile error: {exc}"
            ) from exc
    if not isinstance(raw, dict):
        raise DescriptorError(f"{ctx} must be a mapping (canonical AST)")
    return raw


def _build_action(idx: int, raw: dict, *, ctx: str = "action_sequence") -> ActionDescriptor:
    if not isinstance(raw, dict):
        raise DescriptorError(f"{ctx}[{idx}] must be a mapping")
    cont_raw = raw.get("continuation_rules") or {}
    return ActionDescriptor(
        action_type=_require(raw, "action_type", f"{ctx}[{idx}]"),
        parameters=raw.get("parameters") or {},
        per_action_expectation=raw.get("per_action_expectation", ""),
        continuation_rules=ContinuationRules(
            on_failure=cont_raw.get("on_failure", "abort"),
            max_retries=cont_raw.get("max_retries", 0),
        ),
        gate_ref=raw.get("gate_ref"),
        resume_safe=raw.get("resume_safe", False),
        # WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 0: optional
        # human-readable step ID for reference targets. step_index is
        # ASSIGNED at registration time by validate_workflow, but
        # workflow loading from JSON descriptors must preserve the
        # assigned value so engine lookups via _build_action_by_index
        # find the action by global ordinal.
        id=raw.get("id", ""),
        step_index=int(raw.get("step_index", -1)),
    )


def _build_gate(idx: int, raw: dict) -> ApprovalGate:
    if not isinstance(raw, dict):
        raise DescriptorError(f"approval_gates[{idx}] must be a mapping")
    return ApprovalGate(
        gate_name=_require(raw, "gate_name", f"approval_gates[{idx}]"),
        pause_reason=raw.get("pause_reason", ""),
        approval_event_type=_require(
            raw, "approval_event_type", f"approval_gates[{idx}]",
        ),
        approval_event_predicate=_build_predicate(
            _require(raw, "approval_event_predicate", f"approval_gates[{idx}]"),
            ctx=f"approval_gates[{idx}].approval_event_predicate",
        ),
        timeout_seconds=_require(raw, "timeout_seconds", f"approval_gates[{idx}]"),
        bound_behavior_on_timeout=_require(
            raw, "bound_behavior_on_timeout", f"approval_gates[{idx}]",
        ),
        default_value=raw.get("default_value"),
    )


def _build_trigger(raw: dict) -> TriggerDescriptor:
    if not isinstance(raw, dict):
        raise DescriptorError("trigger must be a mapping")
    return TriggerDescriptor(
        event_type=_require(raw, "event_type", "trigger"),
        predicate=_build_predicate(
            _require(raw, "predicate", "trigger"),
            ctx="trigger.predicate",
        ),
        predicate_source=raw.get("predicate_source", ""),
        actor_filter=raw.get("actor_filter"),
        correlation_filter=raw.get("correlation_filter"),
        idempotency_key_template=raw.get("idempotency_key_template"),
        description=raw.get("description", ""),
    )


def _build_workflow(body: dict, *, narrative_description: str = "") -> Workflow:
    instance_local = bool(body.get("instance_local", False))
    violations = _walk_for_sharing_violations(body, instance_local)
    if violations:
        raise DescriptorError(
            "descriptor references instance-specific values that are neither "
            "parameterised with {installer.<name>} nor guarded by "
            "instance_local: true: " + ", ".join(violations)
        )
    bounds = _build_bounds(body)
    verifier = _build_verifier(body)
    actions_raw = body.get("action_sequence") or []
    if not isinstance(actions_raw, list):
        raise DescriptorError("action_sequence must be a list")
    action_sequence = [_build_action(i, a) for i, a in enumerate(actions_raw)]
    gates_raw = body.get("approval_gates") or []
    if not isinstance(gates_raw, list):
        raise DescriptorError("approval_gates must be a list")
    approval_gates = [_build_gate(i, g) for i, g in enumerate(gates_raw)]
    # SELF-IMPROVEMENT-WORKFLOW-V1 12th amendment (Spec 6 v6.2): accept either
    # singular ``trigger:`` (Spec 4 legacy) or plural ``triggers:`` (production
    # WTC path that compiles via ``compile_descriptor_triggers``). Reject mixing
    # for descriptor-ambiguity defense. When plural is present, ``Workflow.trigger``
    # stays ``None``; the descriptor.triggers list lives in the descriptor JSON
    # blob and is re-compiled by the production-wiring helper at every bring-up
    # (v6.2 V6.2.2 architect call: helper re-execution is the canonical re-hydration
    # path; Workflow dataclass intentionally does not roundtrip plural triggers).
    trigger_raw = body.get("trigger")
    triggers_raw = body.get("triggers")
    if trigger_raw is not None and triggers_raw is not None:
        raise DescriptorError(
            "descriptor specifies both 'trigger' (singular) and 'triggers' "
            "(plural); specify exactly one. Plural is the production WTC shape; "
            "singular is the legacy Spec 4 shape."
        )
    if triggers_raw is not None and not isinstance(triggers_raw, list):
        raise DescriptorError("triggers must be a list")
    trigger = _build_trigger(trigger_raw) if trigger_raw else None
    description = body.get("description", "") or narrative_description
    # WORKFLOW-ORCHESTRATION-PRIMITIVES-V1 Decision 7: terminal_branches
    # is an optional top-level descriptor block; each entry is a named
    # action sub-sequence reachable only via the ``branch`` verb.
    terminal_raw = body.get("terminal_branches") or {}
    if not isinstance(terminal_raw, dict):
        raise DescriptorError("terminal_branches must be a mapping")
    terminal_branches: dict[str, list[ActionDescriptor]] = {}
    for branch_name, branch_actions_raw in terminal_raw.items():
        if not isinstance(branch_actions_raw, list):
            raise DescriptorError(
                f"terminal_branches[{branch_name!r}] must be a list"
            )
        terminal_branches[branch_name] = [
            _build_action(
                i, a, ctx=f"terminal_branches[{branch_name!r}]",
            )
            for i, a in enumerate(branch_actions_raw)
        ]
    return Workflow(
        workflow_id=body.get("workflow_id", "") or str(uuid.uuid4()),
        instance_id=_require(body, "instance_id"),
        name=_require(body, "name"),
        description=description,
        owner=body.get("owner", ""),
        version=str(_require(body, "version")),
        bounds=bounds,
        verifier=verifier,
        action_sequence=action_sequence,
        approval_gates=approval_gates,
        trigger=trigger,
        metadata=body.get("metadata") or {},
        instance_local=instance_local,
        status=body.get("status", "active"),
        terminal_branches=terminal_branches,
    )


__all__ = [
    "DescriptorError",
    "INSTANCE_SPECIFIC_PATHS",
    "parse_descriptor",
]
