"""Extended tool descriptor for the workshop external-service primitive.

Layered on top of the existing workshop descriptor (name, description,
input_schema, implementation). The original required fields are
preserved; the new fields are additive so existing tools continue
parsing without churn. Existing tools migrate to explicit
gate_classification: read in C6.

New fields per the the design review-revised spec:

- service_id: optional. Links the tool to a registered service so the
  runtime-context credential accessor knows which service's token to
  hand out. Tools without service_id do not receive credentials.

- authority: list of operation names the tool is allowed to invoke
  against its service. Validated as a subset of the service's
  declared operations at registration. Re-validated at invocation
  time by C5's runtime enforcement.

- gate_classification: tool-level shorthand for the dispatch-gate
  routing token. Values: read, soft_write, hard_write, delete.
  Used when the tool dispatches a single kind of work.

- operations: per-operation classification (the design review edit 1). When a tool
  exposes multiple operations with different gate semantics (e.g.,
  read_pages is read; write_pages is hard_write), each operation is
  classified independently. Per-operation overrides the tool-level
  shorthand at gate-routing time.

- audit_category: free-form operator-readable label. Defaults to the
  service's audit_category when service_id is set, else to the tool
  name.

- domain_hints: optional list of strings for relevance-based
  surfacing. Service-bound tools do not need them — surfacing keys on
  credential presence for those.

- aggregation: per_member (default) or cross_member. The cross_member
  value is reserved-but-rejected at registration in v1 per the design review's
  call. Declaring it produces a clear error pointing at the v2
  follow-on. The reserved enum value lives here so the validation
  surface is in place when the follow-on lands.

Default for missing classification:
- A tool with no gate_classification and no per-operation overrides
  defaults to soft_write at the tool level. This is the fail-closed
  default per design review's revision 1; previously this was read.
- A tool that *only* has per-operation overrides without a tool-level
  classification picks each operation's classification independently;
  unclassified operations under such a tool default to soft_write.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class GateClassification(str, Enum):
    """Routing tokens recognised by the dispatch gate.

    Per the design review's response on question 1: read / soft_write / hard_write /
    delete are the v1 categories. A separate destructive_irreversible
    category was considered and not added in v1 — the design review's view was that
    delete plus runtime confirmation suffices for the v1 surface.

    EXTERNAL-AGENT-CONSULTATION v1 (additive, AC5 + Codex spec-review
    fold #2): ``external_agent_read`` distinguishes consultation +
    non-native code_exec from plain ``read``. Kernos doesn't write
    anything in either case, but the operation discloses sensitive
    repo / state context to an external process AND triggers a paid,
    stateful subprocess. The dispatch gate routes
    ``external_agent_read`` through a stricter authority gate while
    deriving ``read_only`` safety for integration's catalog (same as
    READ).
    """

    READ = "read"
    SOFT_WRITE = "soft_write"
    HARD_WRITE = "hard_write"
    DELETE = "delete"
    EXTERNAL_AGENT_READ = "external_agent_read"


class Aggregation(str, Enum):
    """Whether a tool reads data across members.

    per_member (default) is the AppData-style isolation. cross_member
    is reserved for v2 per the design review's call — registration with this value
    produces a clear error pointing at the future spec rather than
    silently accepting it or feature-flagging it.
    """

    PER_MEMBER = "per_member"
    CROSS_MEMBER = "cross_member"  # reserved-but-rejected in v1


class OperationSafety(str, Enum):
    """Coarse safety taxonomy used by integration's catalog filter and
    EnactmentService's branch decision (PDI extension).

    Distinct from GateClassification (which drives the dispatch gate's
    confirmation routing) — they correlate but serve different jobs:

      - GateClassification: how the dispatch gate routes (read,
        soft_write, hard_write, delete) for confirmation/blast-radius
      - OperationSafety: who sees the operation
        (integration sees read_only; enactment full machinery sees
        mutating and sensitive_action)

    Default derivation when an OperationClassification declares only a
    GateClassification (no explicit safety override):

      READ                       → read_only
      SOFT_WRITE | HARD_WRITE    → mutating
      DELETE                     → sensitive_action

    Tools whose safety class differs from the gate-classification
    default (e.g. a SOFT_WRITE operation that should still gate as
    sensitive_action because of cross-member implications) declare
    `safety` on the per-operation classification explicitly.
    """

    READ_ONLY = "read_only"
    MUTATING = "mutating"
    SENSITIVE_ACTION = "sensitive_action"


# The conservative fallback when operation classification is
# ambiguous. Per the PDI spec: an ambiguous operation is NEVER
# surfaced to integration's catalog; full machinery treats it as
# sensitive_action.
DEFAULT_AMBIGUOUS_SAFETY = OperationSafety.SENSITIVE_ACTION


# Default mapping from GateClassification to OperationSafety. Only
# applied when an OperationClassification does not declare an explicit
# `safety` override. Centralised here so the derivation rule lives in
# one place.
SAFETY_FOR_GATE: dict[GateClassification, OperationSafety] = {
    GateClassification.READ: OperationSafety.READ_ONLY,
    GateClassification.SOFT_WRITE: OperationSafety.MUTATING,
    GateClassification.HARD_WRITE: OperationSafety.MUTATING,
    GateClassification.DELETE: OperationSafety.SENSITIVE_ACTION,
    # external_agent_read derives read_only safety for catalog
    # filtering purposes (it doesn't mutate Kernos state) but the
    # dispatch gate routes it through a stricter authority pre-check
    # because of context disclosure + paid-subprocess implications.
    GateClassification.EXTERNAL_AGENT_READ: OperationSafety.READ_ONLY,
}


# Default tool-level classification when nothing is declared.
# Per design review's revision 1: missing classification fails closed,
# not open. soft_write is the fail-closed shape.
DEFAULT_GATE_CLASSIFICATION = GateClassification.SOFT_WRITE


# ---------------------------------------------------------------------------
# Operation-level classification
# ---------------------------------------------------------------------------


_OPERATION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class OperationClassification:
    """Per-operation gate classification (the design review edit 1, extended in PDI).

    A tool that exposes multiple operations classifies each
    independently. read_pages is read; write_pages is hard_write;
    delete_pages is delete. The tool-level gate_classification field
    remains as shorthand for single-operation tools; per-operation
    overrides at gate-routing time when both are present.

    PDI extension fields (additive, fully back-compat):
      - `safety`: OperationSafety override. Defaults to None, in
        which case the safety class is derived from `classification`
        via SAFETY_FOR_GATE. Use the override only when the gate
        routing's confirmation policy and the catalog-filter safety
        class need to disagree.
      - `timeout_ms`: per-operation execution-budget hint. 0 means
        "use the tool's default". Consumed by enactment full
        machinery's per-step timeout enforcement.
    """

    operation: str
    classification: GateClassification
    safety: OperationSafety | None = None
    timeout_ms: int = 0

    @property
    def effective_safety(self) -> OperationSafety:
        """Return the OperationSafety after applying the
        gate-classification default when no override is set."""
        if self.safety is not None:
            return self.safety
        return SAFETY_FOR_GATE[self.classification]


def _validate_operation_name(name: str) -> str:
    if not isinstance(name, str) or not _OPERATION_NAME_RE.match(name):
        raise ToolDescriptorError(
            f"operation name {name!r} must be lowercase alphanumeric "
            f"with underscores (matching {_OPERATION_NAME_RE.pattern})"
        )
    return name


# ---------------------------------------------------------------------------
# Tool descriptor
# ---------------------------------------------------------------------------


_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class ToolDescriptor:
    """Workshop tool descriptor with the primitive's extension fields.

    The first four fields preserve the existing workshop contract.
    The remaining fields land in this batch and are all optional so
    pre-existing descriptors parse without modification (with the
    soft_write fail-closed default applying to anything not classified
    explicitly).
    """

    # Original workshop fields
    name: str
    description: str
    input_schema: dict[str, Any]
    implementation: str

    # Extension fields landing in this batch
    service_id: str = ""
    authority: tuple[str, ...] = ()
    gate_classification: GateClassification | None = None
    operations: tuple[OperationClassification, ...] = ()
    audit_category: str = ""
    domain_hints: tuple[str, ...] = ()
    aggregation: Aggregation = Aggregation.PER_MEMBER

    # PDI extension. Optional callable that derives the operation
    # name from a call's args dict at dispatch time. Used by tools
    # whose read/write classification depends on argument values
    # (manage_covenants is the canonical example). Set programmatically
    # by tool registration code; not parsed from JSON descriptors.
    operation_resolver: Callable[[Mapping[str, Any]], str] | None = None

    # Pre-existing optional workshop fields (preserved for back-compat)
    type: str = ""
    stateful: bool = True
    store: str = ""

    # --- read helpers ---

    def classification_for(self, operation: str | None) -> GateClassification:
        """Return the gate classification that applies to the named operation.

        Resolution order, per the design review edit 1:
          1. Per-operation classification (if the tool declared one for
             this operation).
          2. Tool-level gate_classification (shorthand).
          3. The fail-closed default (soft_write).
        """
        if operation:
            for op in self.operations:
                if op.operation == operation:
                    return op.classification
        if self.gate_classification is not None:
            return self.gate_classification
        return DEFAULT_GATE_CLASSIFICATION

    def operation_for(self, name: str) -> OperationClassification | None:
        """Return the OperationClassification for `name`, or None."""
        for op in self.operations:
            if op.operation == name:
                return op
        return None

    def operations_map(self) -> dict[str, OperationClassification]:
        """Return a name-keyed view of declared per-operation
        classifications. Convenience for callers that want dict-style
        access (the canonical storage is the immutable tuple)."""
        return {op.operation: op for op in self.operations}

    def safety_for(self, operation: str | None) -> OperationSafety:
        """Return the OperationSafety for `operation`.

        Resolution order:
          1. Per-operation `safety` override (if declared).
          2. Per-operation derivation from `classification` via
             SAFETY_FOR_GATE.
          3. Tool-level gate_classification (shorthand) → safety via
             SAFETY_FOR_GATE.
          4. Default sensitive_action (conservative fallback for
             unclassified tools).

        Used by integration's catalog filter (only read_only is
        surfaced) and by EnactmentService's full-machinery routing
        (mutating + sensitive_action are dispatch operations).
        """
        if operation:
            op = self.operation_for(operation)
            if op is not None:
                return op.effective_safety
        if self.gate_classification is not None:
            return SAFETY_FOR_GATE[self.gate_classification]
        # No tool-level shorthand and no per-op match — conservative
        # fallback rather than re-routing through DEFAULT_GATE_CLASSIFICATION.
        return DEFAULT_AMBIGUOUS_SAFETY

    @property
    def is_service_bound(self) -> bool:
        return bool(self.service_id)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ToolDescriptorError(ValueError):
    """Raised when a tool descriptor fails validation.

    Validation errors carry a clear-text message that names the
    offending field and (when relevant) the AppData analogy concretely.
    """


class CrossMemberAggregationReservedError(ToolDescriptorError):
    """Raised when a descriptor declares aggregation: cross_member.

    Per the design review's call: cross_member aggregation is a v2 surface. The
    enum value is reserved here so the validation footprint exists
    when the follow-on spec lands; using it in v1 fails with a clear
    pointer to the future spec rather than silent acceptance or a
    feature flag.
    """


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_tool_descriptor(
    data: dict[str, Any],
    *,
    service_lookup: Any | None = None,
    operation_resolver: Callable[[Mapping[str, Any]], str] | None = None,
) -> ToolDescriptor:
    """Validate and construct a ToolDescriptor from a dict.

    `service_lookup` is an optional callable taking a service_id and
    returning the ServiceDescriptor (or None if unknown). When
    provided and the descriptor declares service_id, the parser
    cross-validates that:
      - the service is registered, and
      - every authority entry is in the service's declared operations,
      - every per-operation classification names a real operation.
    Without service_lookup, those cross-checks are deferred to caller.

    Raises ToolDescriptorError (or its CrossMemberAggregationReservedError
    subclass) on validation failure.
    """
    if not isinstance(data, dict):
        raise ToolDescriptorError(
            f"descriptor must be a dict; got {type(data).__name__}"
        )

    name = _validate_name(data.get("name", ""))
    description = _validate_description(data.get("description", ""))
    input_schema = _validate_input_schema(data.get("input_schema", None))
    implementation = _validate_implementation(data.get("implementation", ""))

    service_id = (data.get("service_id") or "").strip()
    authority = _validate_authority(data.get("authority"))
    gate_classification = _parse_gate_classification(data.get("gate_classification"))
    operations = _parse_operations(data.get("operations"))
    audit_category = (data.get("audit_category") or "").strip()
    domain_hints = _parse_domain_hints(data.get("domain_hints"))
    aggregation = _parse_aggregation(data.get("aggregation"))

    # Cross-check service_id and authority against the registry when
    # the caller supplied a lookup.
    if service_id and service_lookup is not None:
        service = service_lookup(service_id)
        if service is None:
            raise ToolDescriptorError(
                f"service_id {service_id!r} is not registered. "
                f"Register the service descriptor first."
            )
        for op in authority:
            if not service.supports_operation(op):
                raise ToolDescriptorError(
                    f"authority operation {op!r} is not in the declared "
                    f"operations of service {service_id!r}. The service's "
                    f"operations are: {', '.join(service.operations) or '(none)'}."
                )
        for op_class in operations:
            if not service.supports_operation(op_class.operation):
                raise ToolDescriptorError(
                    f"per-operation classification names operation "
                    f"{op_class.operation!r} which is not in the declared "
                    f"operations of service {service_id!r}."
                )

    # Per-operation classification names must be a subset of authority
    # when both are declared. Classifying an op the tool doesn't
    # declare authority for is a registration smell.
    if operations and authority:
        for op_class in operations:
            if op_class.operation not in authority:
                raise ToolDescriptorError(
                    f"operation {op_class.operation!r} appears in per-"
                    f"operation classification but not in authority. "
                    f"Either add it to authority or remove the classification."
                )

    if audit_category == "":
        # Default mirrors the spec: service-bound tools inherit the
        # service's audit_category; standalone tools fall back to the
        # tool name. The service value is filled in later when the
        # service registry is consulted; for now we leave it blank
        # and let the runtime resolve it.
        audit_category = "" if service_id else name

    if operation_resolver is not None and not callable(operation_resolver):
        raise ToolDescriptorError(
            "operation_resolver must be callable (mapping[str, Any] -> str) "
            "or None"
        )

    return ToolDescriptor(
        name=name,
        description=description,
        input_schema=input_schema,
        implementation=implementation,
        service_id=service_id,
        authority=authority,
        gate_classification=gate_classification,
        operations=operations,
        audit_category=audit_category,
        domain_hints=domain_hints,
        aggregation=aggregation,
        operation_resolver=operation_resolver,
        type=(data.get("type") or "").strip(),
        stateful=bool(data.get("stateful", True)),
        store=(data.get("store") or "").strip(),
    )


# ---------------------------------------------------------------------------
# Field validators
# ---------------------------------------------------------------------------


def _validate_name(value: Any) -> str:
    if not isinstance(value, str) or not _TOOL_NAME_RE.match(value):
        raise ToolDescriptorError(
            f"tool name {value!r} must be snake_case (lowercase letters, "
            f"digits, underscores; starting with a letter)"
        )
    return value


def _validate_description(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolDescriptorError("description must be a non-empty string")
    return value.strip()


def _validate_input_schema(value: Any) -> dict[str, Any]:
    # Forgiving: models routinely omit input_schema or send a dict without a
    # top-level "type". A tool that takes no params is perfectly valid — default
    # to a permissive object schema rather than failing the whole registration
    # over a missing field (v1 self-test Test 7: agent-built tools bounced here).
    if isinstance(value, dict):
        if value.get("type") == "object":
            return dict(value)
        # A tool's parameter schema is ALWAYS an object wrapper. Coerce a missing
        # OR non-object top-level type (e.g. {"type": "string"}) to object,
        # preserving any properties/required — otherwise the provider gets an
        # invalid tool-parameters schema and the next model request fails.
        coerced = dict(value)
        coerced["type"] = "object"
        return coerced
    return {"type": "object"}


def _extract_impl_filename_from_dict(d: dict[str, Any]) -> str:
    """Pull a *.py filename out of an object-shaped implementation field.

    The model sometimes wraps the implementation as
    ``{"file": "tool.py", "language": "python"}`` instead of a bare string.
    Prefer common filename keys, then any string value ending in .py. The
    extracted value still goes through the strict checks in
    ``_validate_implementation`` (so path traversal / non-.py are rejected).
    """
    for key in ("file", "filename", "path", "module", "name", "entrypoint", "entry"):
        v = d.get(key)
        if isinstance(v, str) and v.strip().endswith(".py"):
            return v.strip()
    for v in d.values():
        if isinstance(v, str) and v.strip().endswith(".py"):
            return v.strip()
    return ""


def _validate_implementation(value: Any) -> str:
    # Forgiving: accept an object-shaped implementation by extracting the .py
    # filename from it, then apply the SAME strict checks below (a dict carrying
    # "../escape.py" or a non-.py name is still rejected).
    if isinstance(value, dict):
        value = _extract_impl_filename_from_dict(value)
    if not isinstance(value, str) or not value.strip():
        raise ToolDescriptorError(
            "implementation must be a string filename (e.g. \"my_tool.py\")"
        )
    impl = value.strip()
    if "/" in impl or "\\" in impl or ".." in impl:
        raise ToolDescriptorError(
            "implementation filename must not contain path separators or '..'"
        )
    if not impl.endswith(".py"):
        raise ToolDescriptorError(
            f"implementation {impl!r} must be a .py file"
        )
    return impl


def _validate_authority(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ToolDescriptorError(
            f"authority must be a list of operation names; got "
            f"{type(value).__name__}"
        )
    return tuple(_validate_operation_name(op) for op in value)


def _parse_gate_classification(value: Any) -> GateClassification | None:
    if value is None or value == "":
        return None
    try:
        return GateClassification(value)
    except ValueError as exc:
        valid = ", ".join(c.value for c in GateClassification)
        raise ToolDescriptorError(
            f"gate_classification {value!r} is not one of: {valid}"
        ) from exc


def _parse_operations(value: Any) -> tuple[OperationClassification, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ToolDescriptorError(
            f"operations (per-operation classifications) must be a list; "
            f"got {type(value).__name__}"
        )
    out: list[OperationClassification] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise ToolDescriptorError(
                f"per-operation classification entry must be a dict with "
                f"'operation' and 'classification' keys; got "
                f"{type(entry).__name__}"
            )
        op = _validate_operation_name(entry.get("operation", ""))
        if op in seen:
            raise ToolDescriptorError(
                f"operation {op!r} is classified more than once"
            )
        seen.add(op)
        cls = _parse_gate_classification(entry.get("classification"))
        if cls is None:
            raise ToolDescriptorError(
                f"per-operation classification for {op!r} must declare "
                f"a non-empty classification"
            )
        safety = _parse_operation_safety(entry.get("safety"))
        timeout_ms = _parse_timeout_ms(entry.get("timeout_ms"))
        out.append(
            OperationClassification(
                operation=op,
                classification=cls,
                safety=safety,
                timeout_ms=timeout_ms,
            )
        )
    return tuple(out)


def _parse_operation_safety(value: Any) -> OperationSafety | None:
    """Parse an optional `safety` override from descriptor JSON.

    Absent or empty → None (caller will derive safety from
    GateClassification at lookup time). Unknown value → clear error
    listing the valid set so authoring mistakes surface fast.
    """
    if value is None or value == "":
        return None
    try:
        return OperationSafety(value)
    except ValueError as exc:
        valid = ", ".join(s.value for s in OperationSafety)
        raise ToolDescriptorError(
            f"operation safety {value!r} is not one of: {valid}"
        ) from exc


def _parse_timeout_ms(value: Any) -> int:
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ToolDescriptorError(
            f"operation timeout_ms must be a non-negative integer; got "
            f"{value!r}"
        )
    return value


def _parse_domain_hints(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ToolDescriptorError(
            f"domain_hints must be a list of strings; got "
            f"{type(value).__name__}"
        )
    out = []
    for hint in value:
        if not isinstance(hint, str) or not hint.strip():
            raise ToolDescriptorError(
                "every domain_hint must be a non-empty string"
            )
        out.append(hint.strip())
    return tuple(out)


def _parse_aggregation(value: Any) -> Aggregation:
    if value is None or value == "":
        return Aggregation.PER_MEMBER
    try:
        agg = Aggregation(value)
    except ValueError as exc:
        valid = ", ".join(a.value for a in Aggregation)
        raise ToolDescriptorError(
            f"aggregation {value!r} is not one of: {valid}"
        ) from exc
    if agg is Aggregation.CROSS_MEMBER:
        raise CrossMemberAggregationReservedError(
            "aggregation 'cross_member' is reserved for a future spec "
            "(WORKSHOP-CROSS-MEMBER-AGGREGATION). v1 of the workshop "
            "primitive does not accept cross-member aggregation. The "
            "enum value exists in the validation surface so it stays "
            "machine-rejectable until the follow-on lands. Default "
            "'per_member' applies otherwise."
        )
    return agg
