"""Agentic Workspace — manifest, artifact lifecycle, and tool registration.

Every space can be a workspace. When the agent builds something (a tool,
a script, a project), it's tracked here as an artifact following the design review's
four-layer model: Artifact → Descriptor → Surface → Store.

The workspace_manifest.json in each space's directory is the source of truth.
Descriptors (.tool.json files) are the canonical tool definitions.
The catalog reads from descriptors. One source, no divergence.
"""
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from kernos.utils import utc_now, _safe_name
from kernos.kernel.credentials_member import MemberCredentialStore
from kernos.kernel.services import ServiceRegistry
from kernos.kernel.tool_audit import build_audit_entry
from kernos.kernel.tool_descriptor import (
    ToolDescriptor,
    ToolDescriptorError,
    parse_tool_descriptor,
)
from kernos.kernel.tool_runtime import build_runtime_context
from kernos.kernel.tool_runtime_enforcement import (
    EnforcementInputs,
    RuntimeEnforcementError,
    ServiceDisabledError,
    compute_registration_hash,
    enforce_invocation,
)
from kernos.kernel.tool_validation import validate_tool_file
from kernos.setup.service_state import ServiceStateStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Artifact:
    """A workspace-built capability following the four-layer model."""

    id: str                        # "artifact_{uuid8}"
    name: str                      # human-readable, matches catalog entry
    type: str                      # "data_tool" | "script" | "project"
    description: str               # one-line (used in catalog)
    files: dict[str, str]          # layer → filename: artifact, descriptor, implementation, store
    catalog_entry: str = ""        # tool name in ToolCatalog (empty = not registered)
    created_at: str = ""
    last_modified: str = ""
    version: int = 1
    status: str = "active"         # "active" | "archived"
    home_space: str = ""           # space where this artifact's data lives
    stateful: bool = True          # whether the tool needs its home space for execution


@dataclass
class WorkspaceManifest:
    """Per-space manifest tracking all built artifacts."""

    version: int = 1
    instance_id: str = ""
    space_id: str = ""
    artifacts: list[Artifact] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

MANAGE_WORKSPACE_TOOL = {
    "name": "manage_workspace",
    "description": (
        "Manage workspace artifacts. List what's been built in this space, "
        "add new artifacts to the manifest after building them with execute_code, "
        "update versions after modifications, or archive artifacts. "
        "Tracks both TOOLS (callable capabilities registered in the catalog) "
        "and PROJECTS (bodies of work like books, websites, business plans — "
        "structured files that persist across sessions, not registered as tools)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "add", "update", "archive"],
                "description": "Operation to perform",
            },
            "artifact": {
                "type": "object",
                "description": "Artifact data (for add/update)",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ["data_tool", "script", "project"]},
                    "description": {"type": "string"},
                    "files": {"type": "object"},
                    "catalog_entry": {"type": "string"},
                    "stateful": {"type": "boolean"},
                },
            },
            "artifact_id": {
                "type": "string",
                "description": "Artifact ID (for update/archive)",
            },
        },
        "required": ["action"],
    },
}

REGISTER_TOOL_TOOL = {
    "name": "register_tool",
    "description": (
        "Register a workspace-built tool in the universal catalog. "
        "The tool must have a .tool.json descriptor file in the current "
        "space's directory. After registration, the tool is callable "
        "from any space via intent-based surfacing. "
        "The descriptor defines name, description, input_schema, and implementation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "descriptor_file": {
                "type": "string",
                "description": "Filename of the .tool.json descriptor in the current space's directory.",
            },
        },
        "required": ["descriptor_file"],
    },
}


# ---------------------------------------------------------------------------
# WorkspaceManager
# ---------------------------------------------------------------------------

class WorkspaceManager:
    """Manages workspace manifests, artifact lifecycle, and tool registration.

    One instance per handler. Manifests are lazy-loaded on space entry —
    no boot-time scan, no cost for unvisited spaces.
    """

    def __init__(
        self,
        data_dir: str,
        catalog: Any = None,
        service_registry: ServiceRegistry | None = None,
        audit_store: Any = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._catalog = catalog  # ToolCatalog reference
        self._loaded_manifests: dict[str, WorkspaceManifest] = {}  # "tenant:space" → manifest
        self._services = service_registry
        self._audit = audit_store
        # Per-instance credential stores cached on first use. Each
        # store is bound to one (data_dir, instance_id) pair.
        self._credential_stores: dict[str, MemberCredentialStore] = {}
        # Install-level service state store (shared across instances).
        # Lazy: instantiated on first access so tests/legacy callers
        # that never touch service-bound dispatch don't pay the cost.
        self._service_state_store: ServiceStateStore | None = None

    def set_catalog(self, catalog: Any) -> None:
        """Set the ToolCatalog reference (called after construction)."""
        self._catalog = catalog

    def set_service_registry(self, registry: ServiceRegistry) -> None:
        """Set the ServiceRegistry (called after construction).

        When set, register_tool consults it to validate service_id and
        authority for service-bound tool descriptors per the workshop
        external-service primitive.
        """
        self._services = registry

    def set_audit_store(self, audit_store: Any) -> None:
        """Set the AuditStore (called after construction).

        When set, service-bound tool invocations emit audit entries
        with the workshop primitive's payload-digest + normalized-
        category shape.
        """
        self._audit = audit_store

    def _credential_store_for(self, instance_id: str) -> MemberCredentialStore:
        """Return (or construct) the per-instance credential store."""
        if instance_id not in self._credential_stores:
            self._credential_stores[instance_id] = MemberCredentialStore(
                self._data_dir, instance_id,
            )
        return self._credential_stores[instance_id]

    def service_state_store(self) -> ServiceStateStore:
        """Return (or construct) the install-level service state store.

        Install-scoped: one store regardless of instance. Lazy-built
        so legacy code paths that don't touch service-bound dispatch
        don't trigger filesystem access. Public so the surfacing
        layer can read disabled state too.
        """
        if self._service_state_store is None:
            self._service_state_store = ServiceStateStore(self._data_dir)
        return self._service_state_store

    # --- Path helpers ---

    def _space_dir(self, instance_id: str, space_id: str) -> Path:
        return self._data_dir / _safe_name(instance_id) / "spaces" / space_id / "files"

    def _manifest_path(self, instance_id: str, space_id: str) -> Path:
        return self._space_dir(instance_id, space_id) / "workspace_manifest.json"

    # --- Manifest I/O ---

    async def load_manifest(self, instance_id: str, space_id: str) -> WorkspaceManifest:
        """Load or create a workspace manifest. Caches in memory."""
        key = f"{instance_id}:{space_id}"
        if key in self._loaded_manifests:
            return self._loaded_manifests[key]

        path = self._manifest_path(instance_id, space_id)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                artifacts = [
                    Artifact(**{k: v for k, v in a.items() if k in Artifact.__dataclass_fields__})
                    for a in raw.get("artifacts", [])
                ]
                manifest = WorkspaceManifest(
                    version=raw.get("version", 1),
                    instance_id=instance_id,
                    space_id=space_id,
                    artifacts=artifacts,
                )
                logger.info("WORKSPACE_MANIFEST: space=%s loaded artifacts=%d active=%d archived=%d",
                    space_id, len(artifacts),
                    sum(1 for a in artifacts if a.status == "active"),
                    sum(1 for a in artifacts if a.status == "archived"))
            except Exception as exc:
                logger.warning("WORKSPACE_MANIFEST: corrupt manifest in %s: %s", space_id, exc)
                manifest = WorkspaceManifest(instance_id=instance_id, space_id=space_id)
        else:
            manifest = WorkspaceManifest(instance_id=instance_id, space_id=space_id)

        self._loaded_manifests[key] = manifest
        return manifest

    async def save_manifest(self, instance_id: str, space_id: str, manifest: WorkspaceManifest) -> None:
        """Persist manifest to disk."""
        path = self._manifest_path(instance_id, space_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": manifest.version,
            "instance_id": manifest.instance_id,
            "space_id": manifest.space_id,
            "artifacts": [asdict(a) for a in manifest.artifacts],
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- Artifact CRUD ---

    async def list_artifacts(self, instance_id: str, space_id: str) -> str:
        """List all active artifacts in the workspace. Returns formatted text."""
        manifest = await self.load_manifest(instance_id, space_id)
        active = [a for a in manifest.artifacts if a.status == "active"]
        if not active:
            return "No artifacts built in this space yet. Use execute_code to build something."

        tools = [a for a in active if a.type in ("data_tool", "script")]
        projects = [a for a in active if a.type == "project"]

        lines = [f"**Workspace** ({len(active)} artifacts)\n"]
        if tools:
            lines.append("**Tools:**")
            for a in tools:
                registered = f" [catalog: {a.catalog_entry}]" if a.catalog_entry else " [not yet registered]"
                lines.append(
                    f"- **{a.name}** ({a.type}, v{a.version}){registered}\n"
                    f"  {a.description}\n"
                    f"  Files: {', '.join(f'{k}={v}' for k, v in a.files.items() if v)}"
                )
        if projects:
            lines.append("\n**Projects:**")
            for a in projects:
                lines.append(
                    f"- **{a.name}** (v{a.version})\n"
                    f"  {a.description}\n"
                    f"  Files: {', '.join(f'{k}={v}' for k, v in a.files.items() if v)}"
                )
        return "\n".join(lines)

    async def add_artifact(
        self, instance_id: str, space_id: str, artifact_data: dict,
    ) -> tuple[str, Artifact]:
        """Add a new artifact to the manifest. Returns (message, artifact)."""
        manifest = await self.load_manifest(instance_id, space_id)
        now = utc_now()

        artifact = Artifact(
            id=f"artifact_{uuid.uuid4().hex[:8]}",
            name=artifact_data.get("name", "untitled"),
            type=artifact_data.get("type", "script"),
            description=artifact_data.get("description", ""),
            files=artifact_data.get("files", {}),
            catalog_entry=artifact_data.get("catalog_entry", ""),
            created_at=now,
            last_modified=now,
            version=1,
            status="active",
            home_space=space_id,
            stateful=artifact_data.get("stateful", True),
        )

        manifest.artifacts.append(artifact)
        await self.save_manifest(instance_id, space_id, manifest)

        logger.info("WORKSPACE_ADD: space=%s artifact=%s type=%s version=%d",
            space_id, artifact.name, artifact.type, artifact.version)

        return f"Added artifact '{artifact.name}' ({artifact.id}) to workspace.", artifact

    async def update_artifact(
        self, instance_id: str, space_id: str, artifact_id: str, updates: dict,
    ) -> str:
        """Update an existing artifact. Increments version."""
        manifest = await self.load_manifest(instance_id, space_id)
        target = next((a for a in manifest.artifacts if a.id == artifact_id), None)
        if not target:
            return f"Artifact '{artifact_id}' not found."
        if target.status != "active":
            return f"Artifact '{artifact_id}' is archived."

        # Apply updates
        for key in ("name", "description", "type", "files", "catalog_entry", "stateful"):
            if key in updates:
                setattr(target, key, updates[key])

        target.version += 1
        target.last_modified = utc_now()
        await self.save_manifest(instance_id, space_id, manifest)

        logger.info("WORKSPACE_UPDATE: space=%s artifact=%s version=%d",
            space_id, target.name, target.version)
        return f"Updated '{target.name}' to version {target.version}."

    async def archive_artifact(
        self, instance_id: str, space_id: str, artifact_id: str,
    ) -> str:
        """Archive an artifact. Removes from catalog but preserves files."""
        manifest = await self.load_manifest(instance_id, space_id)
        target = next((a for a in manifest.artifacts if a.id == artifact_id), None)
        if not target:
            return f"Artifact '{artifact_id}' not found."

        target.status = "archived"
        target.last_modified = utc_now()

        # Remove from catalog if registered
        if target.catalog_entry and self._catalog:
            self._catalog.unregister(target.catalog_entry)

        await self.save_manifest(instance_id, space_id, manifest)
        logger.info("WORKSPACE_ARCHIVE: space=%s artifact=%s", space_id, target.name)
        return f"Archived '{target.name}'. Files preserved on disk."

    # --- Tool Registration ---

    # TOOL-REGISTRATION-AUTHORIZATION-V1 (2026-05-22): tools that
    # declare these classifications enter a pending-approval state
    # rather than auto-registering. The operator approves via the
    # existing /approve flow; on approve, the registration activates
    # through the receipts callback.
    _GATED_CLASSIFICATIONS: frozenset[str] = frozenset({
        "hard_write", "external_agent_read",
    })
    _RECEIPT_KIND_TOOL_REGISTRATION: str = "tool_registration"

    async def register_tool(
        self,
        instance_id: str,
        space_id: str,
        descriptor_file: str | dict,
        *,
        force: bool = False,
        member_id: str = "",
        data_dir: str = "",
        event_stream: Any = None,
    ) -> str:
        """Validate a descriptor and register the tool in the catalog.

        The descriptor (.tool.json) is the source of truth. The catalog
        reads from it. The manifest tracks it.

        Per WORKSHOP-EXTERNAL-SERVICE-PRIMITIVE, descriptors may declare
        extension fields (service_id, authority, gate_classification,
        per-operation classifications, audit_category, domain_hints,
        aggregation). Those are parsed and validated against the
        ServiceRegistry when the WorkspaceManager has one. The
        implementation source is scanned for unsafe authoring patterns;
        findings reject registration unless force=True. A registration
        hash of (descriptor || impl) is stored on the catalog entry so
        runtime enforcement can detect post-registration edits.
        """
        # Guard: LLM may send a dict instead of a string
        if isinstance(descriptor_file, dict):
            descriptor_file = descriptor_file.get("descriptor_file", descriptor_file.get("name", str(descriptor_file)))
        descriptor_file = str(descriptor_file).strip()
        if not descriptor_file:
            return "Error: descriptor_file must be a filename string."

        space_dir = self._space_dir(instance_id, space_id)

        # 1. Validate descriptor filename (no path traversal)
        if "/" in descriptor_file or "\\" in descriptor_file or ".." in descriptor_file:
            return "Descriptor filename must not contain path separators or '..'."

        # 2. Load descriptor. The model sometimes builds the tool inside a
        # subdirectory (e.g. flip_coin_tool/flip_coin.tool.json) instead of the
        # files root. Be forgiving: find it by basename anywhere under the space
        # dir. Bounded to space_dir — path separators in descriptor_file are
        # rejected above, so this never escapes the space. (v1 self-test:
        # register_tool descriptor placement.)
        desc_path = space_dir / descriptor_file
        if not desc_path.exists():
            _matches = sorted(space_dir.rglob(descriptor_file))
            if _matches:
                desc_path = _matches[0]
                logger.info(
                    "TOOL_REGISTER_DESC_NESTED: found %s at %s",
                    descriptor_file, desc_path,
                )
            else:
                return f"Descriptor file '{descriptor_file}' not found in space directory."

        try:
            descriptor = json.loads(desc_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return f"Invalid JSON in descriptor: {exc}"

        # 3. Validate required fields
        required = ["name", "description", "input_schema", "implementation"]
        missing = [f for f in required if f not in descriptor]
        if missing:
            return f"Descriptor missing required fields: {', '.join(missing)}"

        name = descriptor["name"]
        impl = descriptor["implementation"]

        # 3b. Implementation must be a string filename, not an object
        if isinstance(impl, dict):
            # Common mistake: agent sends {"type": "python", "entrypoint": "file.py"}
            impl = impl.get("entrypoint", impl.get("file", impl.get("name", "")))
            if not impl or not isinstance(impl, str):
                return (
                    "The 'implementation' field must be a string filename (e.g. \"my_tool.py\"), "
                    "not an object. The Python file must export execute(input_data) → dict."
                )

        # 4. Validate name (snake_case, no special chars)
        if not name or not re.match(r'^[a-z][a-z0-9_]*$', name):
            return f"Tool name '{name}' must be snake_case (lowercase letters, digits, underscores)."

        # 5. Validate implementation filename (no traversal, must be .py)
        if "/" in impl or "\\" in impl or ".." in impl:
            return "Implementation filename must not contain path separators or '..'."
        if not impl.endswith(".py"):
            return f"Implementation '{impl}' must be a .py file."

        # 6. Check implementation exists and is a file. Resolve it co-located
        # with the descriptor first (the model usually keeps both together),
        # then the files root, then anywhere under the space dir.
        impl_path = desc_path.parent / impl
        if not impl_path.is_file():
            _root_impl = space_dir / impl
            if _root_impl.is_file():
                impl_path = _root_impl
            else:
                _impl_matches = sorted(space_dir.rglob(impl))
                impl_path = _impl_matches[0] if _impl_matches else (space_dir / impl)
        if not impl_path.is_file():
            return f"Implementation file '{impl}' not found."

        # 6b. Normalize to the files ROOT. The catalog stores only basenames and
        # the runtime schema/impl loaders read from the root, so a descriptor/
        # impl the model built in a subdirectory must be copied up or the tool
        # would register but fail to load on first call. (v1 self-test.)
        import shutil as _shutil
        if desc_path.parent != space_dir:
            _root_desc = space_dir / descriptor_file
            try:
                _shutil.copyfile(desc_path, _root_desc)
                desc_path = _root_desc
            except OSError as _exc:
                return f"Could not place descriptor '{descriptor_file}' at the space root: {_exc}"
        if impl_path.parent != space_dir:
            _root_impl2 = space_dir / impl
            try:
                _shutil.copyfile(impl_path, _root_impl2)
                impl_path = _root_impl2
            except OSError as _exc:
                return f"Could not place implementation '{impl}' at the space root: {_exc}"

        # 7. Check name uniqueness in catalog
        existing = self._catalog.get(name) if self._catalog else None
        if existing and existing.source != "workspace":
            return f"Name '{name}' conflicts with an existing {existing.source} tool."

        # 6. Validate input_schema
        schema = descriptor.get("input_schema", {})
        if not isinstance(schema, dict) or "type" not in schema:
            return "input_schema must be a valid JSON Schema object with a 'type' field."

        # 6b. Parse the extended descriptor (workshop external-service
        # primitive). Pre-existing tools without extension fields parse
        # cleanly with all extensions defaulted; tools that declare
        # service_id are cross-validated against the ServiceRegistry
        # (when one is wired in).
        try:
            service_lookup = self._services.get if self._services else None
            extended_descriptor = parse_tool_descriptor(
                descriptor, service_lookup=service_lookup,
            )
        except ToolDescriptorError as exc:
            return f"Tool descriptor validation failed: {exc}"

        # 6c. Authoring-pattern validation. Heuristic regex pass over
        # the implementation source; findings reject registration unless
        # force=True. Force-registered tools surface only to the author
        # at invocation time; runtime enforcement still applies to them.
        validation = validate_tool_file(impl_path)
        if not validation.is_clean and not force:
            return (
                "Authoring-pattern validation rejected the tool. "
                "Either fix the findings or pass force=True (which "
                "limits the tool's surfacing to its author). "
                f"Findings:\n{validation.render()}"
            )
        if force and not validation.is_clean:
            logger.warning(
                "TOOL_REGISTER_FORCE: name=%s space=%s findings=%d "
                "(force-registered; tool surfaces only to its author)",
                name, space_id, len(validation.findings),
            )

        # 6d. Compute registration hash of (descriptor || impl) for the
        # runtime enforcement check.
        try:
            registration_hash = compute_registration_hash(
                desc_path.read_bytes(),
                impl_path.read_bytes(),
            )
        except OSError as exc:
            return f"Failed to read tool source for registration hash: {exc}"

        # 6e. TOOL-REGISTRATION-AUTHORIZATION-V1 (2026-05-22): if the
        # descriptor declares a gated classification (hard_write or
        # external_agent_read), defer activation pending operator
        # approval. The receipts substrate provides durability +
        # idempotency. v1 documented behavior: unknown / unset
        # classification auto-approves (existing behavior); tighten
        # in a future spec after the catalog audit lands.
        _classification = (
            getattr(extended_descriptor, "gate_classification", "") or ""
        )
        if (
            _classification in self._GATED_CLASSIFICATIONS
            and data_dir  # gate only when caller supplies receipts-substrate context
        ):
            gate_result = await self._gate_registration_via_receipt(
                instance_id=instance_id,
                space_id=space_id,
                name=name,
                description=descriptor["description"],
                descriptor_file=descriptor_file,
                impl=impl,
                classification=_classification,
                registration_hash=registration_hash,
                force=force,
                member_id=member_id or "owner",
                data_dir=data_dir,
                event_stream=event_stream,
            )
            if gate_result is not None:
                return gate_result

        # 7-8. Activate (catalog + manifest). Extracted into a helper
        # so the receipts approval callback can re-use the same code.
        return await self._activate_registration(
            instance_id=instance_id,
            space_id=space_id,
            name=name,
            descriptor=descriptor,
            descriptor_file=descriptor_file,
            impl=impl,
            registration_hash=registration_hash,
            extended_descriptor=extended_descriptor,
            force=force,
            validation_is_clean=validation.is_clean,
        )

    async def _gate_registration_via_receipt(
        self, *,
        instance_id: str,
        space_id: str,
        name: str,
        description: str,
        descriptor_file: str,
        impl: str,
        classification: str,
        registration_hash: str,
        force: bool,
        member_id: str,
        data_dir: str,
        event_stream: Any,
    ) -> str | None:
        """TOOL-REGISTRATION-AUTHORIZATION-V1 (2026-05-22).

        Returns the agent-facing message string when activation
        should be deferred (pending receipt issued / existing
        pending found / prior rejection surfaced). Returns None to
        signal "proceed with normal activation" (e.g., a prior
        approved receipt for the same hash + the activation has
        not run yet — caller continues to step 7).
        """
        from kernos.kernel import approval_receipts as _approvals
        from kernos.kernel.event_types import EventType

        # Idempotency: same hash already pending → return same request_id.
        existing_pending = await _approvals.find_pending_by_binding_field(
            data_dir=data_dir, instance_id=instance_id,
            kind=self._RECEIPT_KIND_TOOL_REGISTRATION,
            field="registration_hash", value=registration_hash,
        )
        if existing_pending is not None:
            return (
                f"Tool registration for '{name}' is already pending "
                f"owner approval. Request ID: "
                f"{existing_pending['approval_id']}. Operator will "
                f"see the request via /approve."
            )

        # Recent terminal-state lookup: if the prior identical hash
        # was rejected, surface the rejection reason rather than
        # silently re-issuing a fresh receipt.
        recent_terminal = await _approvals.find_recent_terminal_by_binding_field(
            data_dir=data_dir, instance_id=instance_id,
            kind=self._RECEIPT_KIND_TOOL_REGISTRATION,
            field="registration_hash", value=registration_hash,
        )
        if recent_terminal is not None and recent_terminal.get("state") == "rejected":
            reason = (recent_terminal.get("state_reason") or "").strip()
            return (
                f"Tool registration for '{name}' (same descriptor + "
                f"impl) was previously rejected by the operator"
                + (f": {reason}." if reason else ".")
                + " Modify the descriptor / impl to retry."
            )
        # An approved-but-unactivated row would be unusual (activation
        # callback runs synchronously with approve); if it happened
        # (operator pid-killed mid-callback), fall through to
        # re-activate. The catalog.register call is idempotent on the
        # name (returns existing entry).
        if recent_terminal is not None and recent_terminal.get("state") == "approved":
            return None  # signal: proceed with normal activation

        # Issue a new pending receipt.
        binding_payload = {
            "kind": self._RECEIPT_KIND_TOOL_REGISTRATION,
            "instance_id": instance_id,
            "space_id": space_id,
            "name": name,
            "description": description,
            "descriptor_file": descriptor_file,
            "impl": impl,
            "classification": classification,
            "registration_hash": registration_hash,
            "force": force,
        }
        summary = (
            f"Tool registration: {name!r} ({classification}). "
            f"From space {space_id}. /approve to activate."
        )
        try:
            approval_id = await _approvals.request_approval(
                data_dir=data_dir,
                instance_id=instance_id,
                kind=self._RECEIPT_KIND_TOOL_REGISTRATION,
                requested_for_actor=member_id,
                operator_actor_id=member_id,
                request_summary=summary,
                binding_payload=binding_payload,
                event_stream=event_stream,
            )
        except Exception as exc:
            logger.warning(
                "TOOL_REGISTER_GATE_FAILED name=%s exc=%s",
                name, exc,
            )
            return (
                f"Tool registration for '{name}' could not be gated: "
                f"{exc}. Tool was NOT registered."
            )

        if event_stream is not None:
            try:
                await event_stream.emit(
                    instance_id, EventType.TOOL_REGISTRATION_PENDING.value,
                    {
                        "name": name,
                        "classification": classification,
                        "request_id": approval_id,
                        "registration_hash": registration_hash,
                        "space_id": space_id,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "TOOL_REGISTRATION_PENDING emit failed: %s", exc,
                )

        logger.info(
            "TOOL_REGISTRATION_PENDING name=%s classification=%s "
            "request_id=%s hash=%s space=%s",
            name, classification, approval_id,
            registration_hash[:12], space_id,
        )
        return (
            f"Tool registration for '{name}' ({classification}) is "
            f"pending owner approval. Request ID: {approval_id}. The "
            f"owner sees a notification; the tool surfaces on next "
            f"assemble after `/approve {approval_id} CONFIRM`."
        )

    async def _activate_registration(
        self, *,
        instance_id: str,
        space_id: str,
        name: str,
        descriptor: dict,
        descriptor_file: str,
        impl: str,
        registration_hash: str,
        extended_descriptor: Any,
        force: bool,
        validation_is_clean: bool,
    ) -> str:
        """Steps 7-8 of the original register_tool flow, extracted so
        the receipts approval callback can re-use it after operator
        confirmation."""
        if self._catalog:
            self._catalog.register(
                name=name,
                description=descriptor["description"],
                source="workspace",
            )
            entry = self._catalog.get(name)
            if entry:
                entry.home_space = space_id
                entry.implementation = impl
                entry.stateful = descriptor.get("stateful", True)
                entry.descriptor_file = descriptor_file
                entry.service_id = extended_descriptor.service_id
                entry.registration_hash = registration_hash
                entry.force_registered = bool(force and not validation_is_clean)
        else:
            entry = None

        logger.info(
            "TOOL_REGISTER: name=%s space=%s source=workspace "
            "service=%s hash=%s force=%s",
            name, space_id,
            extended_descriptor.service_id or "(internal)",
            registration_hash[:12],
            entry.force_registered if entry else False,
        )

        manifest = await self.load_manifest(instance_id, space_id)
        existing_artifact = next(
            (a for a in manifest.artifacts if a.catalog_entry == name and a.status == "active"),
            None,
        )
        if not existing_artifact:
            await self.add_artifact(instance_id, space_id, {
                "name": name,
                "type": descriptor.get("type", "data_tool"),
                "description": descriptor["description"],
                "files": {
                    "descriptor": descriptor_file,
                    "implementation": impl,
                    "store": descriptor.get("store", ""),
                },
                "catalog_entry": name,
                "stateful": descriptor.get("stateful", True),
            })
        return (
            f"Registered tool '{name}'. It's now available across "
            f"all spaces via the universal catalog."
        )

    async def activate_pending_registration(
        self, *,
        approval_id: str,
        binding_payload: dict,
        event_stream: Any = None,
    ) -> str:
        """TOOL-REGISTRATION-AUTHORIZATION-V1 (2026-05-22).

        Called by the /approve slash command after an operator
        approves a tool_registration receipt. Reads the binding
        payload's descriptor + impl from disk (validates they still
        exist + hash unchanged), then activates via
        :meth:`_activate_registration`.

        Returns the operator-facing message describing the outcome.
        Failures land here (descriptor deleted post-approval, hash
        edited) and surface a clear message; the receipt stays
        approved (idempotency).
        """
        from kernos.kernel.event_types import EventType

        name = binding_payload.get("name", "")
        instance_id = binding_payload.get("instance_id", "")
        space_id = binding_payload.get("space_id", "")
        descriptor_file = binding_payload.get("descriptor_file", "")
        impl = binding_payload.get("impl", "")
        recorded_hash = binding_payload.get("registration_hash", "")
        classification = binding_payload.get("classification", "")
        force = bool(binding_payload.get("force", False))

        space_dir = self._space_dir(instance_id, space_id)
        desc_path = space_dir / descriptor_file
        impl_path = space_dir / impl

        if not desc_path.exists() or not impl_path.is_file():
            self._write_activation_friction_report(
                approval_id=approval_id, name=name,
                reason="descriptor or implementation missing on disk",
                binding_payload=binding_payload,
            )
            return (
                f"Tool '{name}' was approved but the descriptor or "
                f"implementation file is no longer on disk. Activation "
                f"aborted. Agent must re-create the files + re-call "
                f"register_tool (which will issue a fresh approval)."
            )

        # Verify the hash hasn't drifted between issue and approval.
        try:
            current_hash = compute_registration_hash(
                desc_path.read_bytes(),
                impl_path.read_bytes(),
            )
        except OSError as exc:
            self._write_activation_friction_report(
                approval_id=approval_id, name=name,
                reason=f"failed to read sources: {exc}",
                binding_payload=binding_payload,
            )
            return (
                f"Tool '{name}' was approved but its source files "
                f"could not be read: {exc}. Activation aborted."
            )
        if current_hash != recorded_hash:
            self._write_activation_friction_report(
                approval_id=approval_id, name=name,
                reason=(
                    f"descriptor or implementation was edited after "
                    f"approval (hash {recorded_hash[:12]} → "
                    f"{current_hash[:12]})"
                ),
                binding_payload=binding_payload,
            )
            return (
                f"Tool '{name}' was approved but the descriptor or "
                f"implementation was edited since approval. The "
                f"approved hash {recorded_hash[:12]} no longer "
                f"matches. Activation aborted — agent must re-call "
                f"register_tool to issue a fresh approval for the "
                f"current source."
            )

        try:
            descriptor = json.loads(desc_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self._write_activation_friction_report(
                approval_id=approval_id, name=name,
                reason=f"descriptor JSON parse failed: {exc}",
                binding_payload=binding_payload,
            )
            return (
                f"Tool '{name}' was approved but its descriptor is "
                f"no longer valid JSON: {exc}. Activation aborted."
            )

        service_lookup = self._services.get if self._services else None
        try:
            extended_descriptor = parse_tool_descriptor(
                descriptor, service_lookup=service_lookup,
            )
        except ToolDescriptorError as exc:
            self._write_activation_friction_report(
                approval_id=approval_id, name=name,
                reason=f"descriptor re-parse failed: {exc}",
                binding_payload=binding_payload,
            )
            return (
                f"Tool '{name}' was approved but its descriptor "
                f"re-validation failed: {exc}. Activation aborted."
            )

        # Re-run authoring-pattern validation in case the impl was
        # edited (we already caught hash drift above, but the
        # validation gives us the is_clean flag).
        validation = validate_tool_file(impl_path)

        activation_msg = await self._activate_registration(
            instance_id=instance_id,
            space_id=space_id,
            name=name,
            descriptor=descriptor,
            descriptor_file=descriptor_file,
            impl=impl,
            registration_hash=recorded_hash,
            extended_descriptor=extended_descriptor,
            force=force,
            validation_is_clean=validation.is_clean,
        )

        if event_stream is not None:
            try:
                await event_stream.emit(
                    instance_id,
                    EventType.TOOL_REGISTRATION_APPROVED.value,
                    {
                        "name": name,
                        "classification": classification,
                        "request_id": approval_id,
                        "space_id": space_id,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "TOOL_REGISTRATION_APPROVED emit failed: %s", exc,
                )
        logger.info(
            "TOOL_REGISTRATION_APPROVED name=%s classification=%s "
            "request_id=%s",
            name, classification, approval_id,
        )
        return activation_msg

    def _write_activation_friction_report(
        self, *, approval_id: str, name: str, reason: str,
        binding_payload: dict,
    ) -> None:
        """Drop a friction report when a tool_registration approval
        callback fails post-approval. Best-effort; never raises."""
        try:
            from datetime import datetime, timezone
            data_dir = os.environ.get("KERNOS_DATA_DIR", "./data")
            friction_dir = Path(data_dir) / "diagnostics" / "friction"
            friction_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            unique = uuid.uuid4().hex[:8]
            filepath = (
                friction_dir
                / f"FRICTION_{ts}_{unique}_TOOL_REGISTRATION_ACTIVATION.md"
            )
            lines = [
                "# Friction Report: TOOL_REGISTRATION_ACTIVATION",
                f"Generated: {datetime.now(timezone.utc).isoformat()}",
                "",
                "## Description",
                f"Tool '{name}' was approved (approval_id={approval_id}) "
                f"but activation failed: {reason}.",
                "",
                "## Binding payload",
                "```json",
                json.dumps(binding_payload, indent=2),
                "```",
                "",
                "## Recommendation",
                "Receipt stays approved (terminal). Agent must "
                "re-create the file(s) and call register_tool again "
                "to issue a fresh approval.",
            ]
            filepath.write_text("\n".join(lines), encoding="utf-8")
            logger.info(
                "TOOL_REGISTRATION_ACTIVATION_FRICTION path=%s reason=%s",
                filepath, reason,
            )
        except Exception as exc:
            logger.warning(
                "TOOL_REGISTRATION_ACTIVATION_FRICTION_WRITE_FAILED "
                "exc=%s", exc,
            )

    # --- Workspace Tool Execution ---

    async def execute_workspace_tool(
        self,
        instance_id: str,
        tool_name: str,
        tool_input: dict,
        data_dir: str,
        *,
        member_id: str = "",
        audit_entry_id: str = "",
    ) -> str:
        """Execute a workspace-built tool by calling its implementation.

        When the catalog entry has a service_id (workshop external-
        service primitive), execution routes through
        _execute_service_bound_tool, which runs the four runtime checks,
        builds the per-member runtime context, calls the tool's
        execute(input_data, context), and emits an audit entry.

        Tools without service_id continue through the existing
        subprocess path; this preserves back-compat for the workshop's
        original self-contained tool model.
        """
        if not self._catalog:
            return json.dumps({"error": "Catalog not available"})

        entry = self._catalog.get(tool_name)
        if not entry or entry.source != "workspace":
            return json.dumps({"error": f"Unknown workspace tool: {tool_name}"})

        # Route service-bound tools (workspace or stock) through the
        # primitive's dispatch path. Tools without service_id stay on
        # the existing subprocess flow which requires home_space +
        # implementation per the workspace model.
        if getattr(entry, "service_id", ""):
            if not member_id:
                return json.dumps({
                    "error": (
                        f"Service-bound tool '{tool_name}' requires "
                        f"member_id at invocation time. The dispatcher "
                        f"must thread the invoking member's identity."
                    ),
                })
            return await self._execute_service_bound_tool(
                instance_id=instance_id,
                tool_name=tool_name,
                tool_input=tool_input,
                member_id=member_id,
                entry=entry,
                audit_entry_id=audit_entry_id,
            )

        home_space = getattr(entry, "home_space", "")
        implementation = getattr(entry, "implementation", "")
        if not home_space or not implementation:
            return json.dumps({"error": f"Tool '{tool_name}' missing home_space or implementation"})

        # Validate implementation filename
        if "/" in implementation or "\\" in implementation or ".." in implementation:
            return json.dumps({"error": "Implementation path contains traversal sequences"})
        if not implementation.endswith(".py"):
            return json.dumps({"error": "Implementation must be a .py file"})

        # Write input data to a unique temp file (avoids collision on concurrent calls)
        import tempfile as _tf
        space_dir = self._space_dir(instance_id, home_space)
        space_dir.mkdir(parents=True, exist_ok=True)
        fd, input_path = _tf.mkstemp(suffix=".json", prefix="_tool_input_", dir=str(space_dir))
        input_filename = os.path.basename(input_path)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(tool_input, f)
        except Exception:
            os.close(fd)
            raise

        module_name = implementation.replace(".py", "")
        exec_code = (
            "import json, sys, os\n"
            "sys.path.insert(0, '.')\n"
            f"from {module_name} import execute\n"
            f"with open('{input_filename}') as f:\n"
            "    input_data = json.load(f)\n"
            f"os.unlink('{input_filename}')\n"
            "result = execute(input_data)\n"
            "print(json.dumps(result))\n"
        )

        from kernos.kernel.code_exec import execute_code
        result = await execute_code(
            instance_id=instance_id,
            space_id=home_space,
            code=exec_code,
            timeout_seconds=30,
            data_dir=data_dir,
        )

        logger.info("TOOL_DISPATCH: name=%s type=workspace home=%s success=%s",
            tool_name, home_space, result.get("success"))

        if result.get("success"):
            stdout = result.get("stdout", "").strip()
            try:
                return json.dumps(json.loads(stdout))
            except json.JSONDecodeError:
                return json.dumps({"output": stdout}) if stdout else json.dumps({"status": "completed"})
        else:
            error = result.get("stderr", "") or result.get("error", "Execution failed")
            return json.dumps({"error": error[:500]})

    # --- Stock-connector tool registration ---

    def register_stock_tools(self, stock_root: Path | str) -> int:
        """Auto-register tools that ship in source under stock_root.

        Walks `stock_root/*/` for any `.tool.json` files. Each pair of
        (descriptor, implementation) registers into the catalog the
        same way workshop tools do — service_id cross-validation,
        authoring-pattern check, registration hash. The catalog entry's
        stock_dir is set to the file's directory so the dispatcher
        resolves the source paths at invocation time.

        Returns the count of tools registered.

        Stock-tool authoring-pattern findings cause the loader to log
        a warning and skip that tool rather than raising; a broken
        stock tool should not take down boot.
        """
        root = Path(stock_root)
        if not root.exists() or not root.is_dir():
            return 0
        loaded = 0
        for descriptor_path in sorted(root.glob("*/*.tool.json")):
            impl_dir = descriptor_path.parent
            try:
                self._register_stock_tool(descriptor_path, impl_dir)
                loaded += 1
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "STOCK_TOOL_LOAD_FAILED: path=%s reason=%s",
                    descriptor_path, exc,
                )
        logger.info("STOCK_TOOLS_REGISTERED: count=%d root=%s", loaded, root)
        return loaded

    def _register_stock_tool(self, descriptor_path: Path, impl_dir: Path) -> None:
        """Validate + register one stock tool. Mirrors register_tool's
        path but resolves files from impl_dir rather than a workspace.
        """
        if self._catalog is None:
            return  # nothing to register against
        try:
            descriptor_dict = json.loads(descriptor_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in {descriptor_path}: {exc}") from exc

        service_lookup = self._services.get if self._services else None
        descriptor = parse_tool_descriptor(
            descriptor_dict, service_lookup=service_lookup,
        )

        impl_filename = descriptor.implementation
        impl_path = impl_dir / impl_filename
        if not impl_path.is_file():
            raise FileNotFoundError(
                f"stock tool implementation not found at {impl_path}"
            )

        # Authoring-pattern validation. Stock tools are committed
        # source under our control, so findings here are programmer
        # errors and should fail the load rather than be force-bypassed
        # silently. Surface clearly.
        validation = validate_tool_file(impl_path)
        if not validation.is_clean:
            raise RuntimeError(
                f"stock tool {descriptor.name!r} authoring-pattern findings "
                f"({len(validation.findings)} issues); fix the implementation. "
                f"First finding: {validation.findings[0].code} at line "
                f"{validation.findings[0].line}"
            )

        registration_hash = compute_registration_hash(
            descriptor_path.read_bytes(), impl_path.read_bytes(),
        )

        # Refuse silent overrides of an existing workspace tool by name.
        existing = self._catalog.get(descriptor.name)
        if existing and existing.source != "workspace":
            return  # already registered (e.g. earlier stock load)

        self._catalog.register(
            name=descriptor.name,
            description=descriptor.description,
            source="workspace",
        )
        entry = self._catalog.get(descriptor.name)
        if entry is None:
            return
        entry.home_space = ""  # stock tools have no per-(instance, space) home
        entry.implementation = impl_filename
        entry.descriptor_file = descriptor_path.name
        entry.service_id = descriptor.service_id
        entry.registration_hash = registration_hash
        entry.force_registered = False
        entry.stock_dir = str(impl_dir)
        entry.stateful = bool(descriptor.stateful)
        logger.info(
            "STOCK_TOOL_REGISTER: name=%s service=%s dir=%s",
            descriptor.name, descriptor.service_id or "(internal)", impl_dir,
        )

    # --- Service-bound tool dispatch (workshop external-service primitive) ---

    async def _execute_service_bound_tool(
        self,
        *,
        instance_id: str,
        tool_name: str,
        tool_input: dict,
        member_id: str,
        entry: Any,
        audit_entry_id: str = "",
    ) -> str:
        """Service-bound tool execution path per WORKSHOP-EXTERNAL-SERVICE-PRIMITIVE.

        Six steps in order:
        1. Re-parse the descriptor from disk (so any registered hash
           still matches the current bytes; runtime enforcement
           verifies this in step 2).
        2. Run the four invocation-time checks (hash, operation
           authority, credential scope, sandbox readiness) via
           enforce_invocation. Failures raise specific subclasses of
           RuntimeEnforcementError; the dispatcher catches and surfaces
           clean errors.
        3. Build the runtime context (per-member data_dir, scoped
           credentials, member_id) with the invoking member's identity.
        4. Import the implementation module and invoke
           execute(input_data, context).
        5. Build an audit entry with the workshop primitive's payload
           digest + normalized category and write it to the audit log.
        6. Return the result as JSON.
        """
        import importlib
        import sys
        from datetime import datetime, timezone

        # Stock connectors set entry.stock_dir to the absolute source
        # directory; workspace tools resolve paths from the per-
        # (instance, space) workspace dir.
        stock_dir = getattr(entry, "stock_dir", "") or ""
        if stock_dir:
            space_dir = Path(stock_dir)
        else:
            space_dir = self._space_dir(instance_id, entry.home_space)
        desc_path = space_dir / entry.descriptor_file
        impl_path = space_dir / entry.implementation

        # Step 1: re-parse descriptor (services + extended fields).
        try:
            descriptor_dict = json.loads(desc_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return json.dumps({
                "error": f"Failed to load descriptor: {exc}",
            })
        try:
            service_lookup = self._services.get if self._services else None
            descriptor = parse_tool_descriptor(
                descriptor_dict, service_lookup=service_lookup,
            )
        except ToolDescriptorError as exc:
            return json.dumps({
                "error": f"Descriptor failed re-validation at invocation: {exc}",
            })

        # Step 2: runtime enforcement. The operation is taken from the
        # tool input under the conventional `__operation__` field; if
        # absent, the tool's authority is consulted: a single-authority
        # tool defaults to that operation.
        operation = str(tool_input.get("__operation__", "")).strip()
        if not operation and len(descriptor.authority) == 1:
            operation = descriptor.authority[0]
        # Strip the convention key so it doesn't leak into the tool
        # input or the audit-log digest.
        clean_input = {k: v for k, v in tool_input.items() if k != "__operation__"}

        credential_store = self._credential_store_for(instance_id)
        service_state_store = self.service_state_store()
        try:
            enforce_invocation(EnforcementInputs(
                descriptor=descriptor,
                operation=operation,
                descriptor_path=desc_path,
                implementation_path=impl_path,
                registered_hash=getattr(entry, "registration_hash", ""),
                member_id=member_id,
                credential_store=credential_store,
                service_registry=self._services,
                service_state_store=service_state_store,
            ))
        except ServiceDisabledError as exc:
            # INSTALL-FOR-STOCK-CONNECTORS: dedicated audit category
            # for disabled-service refusals so operators can grep the
            # log for "what got refused because a service was off."
            await self._emit_disabled_service_audit(
                instance_id=instance_id,
                member_id=member_id,
                space_id=entry.home_space,
                tool_name=descriptor.name,
                service_id=descriptor.service_id,
                operation=operation,
            )
            return json.dumps({"error": str(exc)})
        except RuntimeEnforcementError as exc:
            await self._emit_audit(
                instance_id=instance_id,
                member_id=member_id,
                space_id=entry.home_space,
                descriptor=descriptor,
                operation=operation,
                payload=clean_input,
                success=False,
                error=str(exc)[:300],
                audit_entry_id=audit_entry_id,
            )
            return json.dumps({"error": str(exc)})

        # Step 3: build runtime context.
        context = build_runtime_context(
            install_data_dir=self._data_dir,
            credential_store=credential_store,
            instance_id=instance_id,
            member_id=member_id,
            space_id=entry.home_space,
            tool_id=tool_name,
            service_id=descriptor.service_id,
        )

        # Step 4: import the module and invoke execute(input, context).
        # The space directory is added to sys.path for the import; the
        # path entry is removed in the finally block to avoid bleeding
        # imports across tool invocations.
        path_entry = str(space_dir)
        result_payload: Any
        success = False
        error_text = ""
        sys.path.insert(0, path_entry)
        try:
            module_name = entry.implementation.removesuffix(".py")
            try:
                if module_name in sys.modules:
                    module = importlib.reload(sys.modules[module_name])
                else:
                    module = importlib.import_module(module_name)
            except Exception as exc:
                raise RuntimeError(f"failed to import {module_name}: {exc}") from exc
            execute_fn = getattr(module, "execute", None)
            if execute_fn is None:
                raise RuntimeError(
                    f"tool module {module_name!r} does not define execute(input_data, context)"
                )
            result_payload = execute_fn(clean_input, context)
            success = True
        except Exception as exc:
            result_payload = {"error": str(exc)[:300]}
            error_text = str(exc)[:300]
        finally:
            try:
                sys.path.remove(path_entry)
            except ValueError:
                pass

        # Step 5: emit audit entry (skipped when canonical entry
        # already exists upstream — TOOL-AUDIT-NORMALIZATION-V1).
        await self._emit_audit(
            instance_id=instance_id,
            member_id=member_id,
            space_id=entry.home_space,
            descriptor=descriptor,
            operation=operation,
            payload=clean_input,
            success=success,
            error=error_text,
            audit_entry_id=audit_entry_id,
        )

        # Step 6: return JSON.
        try:
            return json.dumps(result_payload)
        except (TypeError, ValueError):
            return json.dumps({"error": "tool returned non-serialisable result"})

    async def _emit_audit(
        self,
        *,
        instance_id: str,
        member_id: str,
        space_id: str,
        descriptor: ToolDescriptor,
        operation: str,
        payload: dict,
        success: bool,
        error: str = "",
        audit_entry_id: str = "",
    ) -> None:
        """Write a workshop-primitive-shaped audit entry. Best effort —
        log a warning on failure rather than blocking the tool result.

        TOOL-AUDIT-NORMALIZATION-V1 (2026-05-22): when
        ``audit_entry_id`` is set, the canonical entry is being
        constructed upstream by the live dispatcher; this path
        skips its own emission to avoid double-audit.
        """
        if audit_entry_id:
            # Canonical entry already in flight upstream; suppress.
            return
        if self._audit is None:
            return
        try:
            from datetime import datetime, timezone
            audit_entry = build_audit_entry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                instance_id=instance_id,
                member_id=member_id,
                space_id=space_id,
                tool_name=descriptor.name,
                operation=operation,
                service_id=descriptor.service_id,
                authority=descriptor.authority,
                audit_category=descriptor.audit_category,
                payload=payload,
                success=success,
                error=error,
            )
            await self._audit.log(instance_id, audit_entry.to_dict())
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "WORKSHOP_AUDIT_WRITE_FAILED: tool=%s err=%s",
                descriptor.name, exc,
            )

    async def _emit_disabled_service_audit(
        self,
        *,
        instance_id: str,
        member_id: str,
        space_id: str,
        tool_name: str,
        service_id: str,
        operation: str,
    ) -> None:
        """Emit `install.dispatch_refused_disabled_service` audit entry.

        Per INSTALL-FOR-STOCK-CONNECTORS spec Section 11. Distinct
        from the workshop's per-invocation audit shape — this fires
        before the tool's authority and credential machinery, so the
        category is install-flavored rather than tool-flavored.
        Best-effort: never blocks the dispatch refusal path.
        """
        if self._audit is None:
            return
        try:
            from datetime import datetime, timezone
            entry = {
                "type": "install.dispatch_refused_disabled_service",
                "audit_category": "install.dispatch_refused_disabled_service",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "instance_id": instance_id,
                "member_id": member_id,
                "space_id": space_id,
                "tool_name": tool_name,
                "service_id": service_id,
                "operation": operation,
            }
            await self._audit.log(instance_id, entry)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "INSTALL_AUDIT_WRITE_FAILED: tool=%s service=%s err=%s",
                tool_name, service_id, exc,
            )

    # --- Lazy Registration on Space Entry ---

    async def ensure_registered(self, instance_id: str, space_id: str) -> None:
        """On space entry, ensure all active artifacts with catalog entries are registered.

        This is the lazy-load mechanism — manifests load and register tools
        on first visit, not at boot. No cost for unvisited spaces.
        """
        manifest = await self.load_manifest(instance_id, space_id)
        for artifact in manifest.artifacts:
            if artifact.status != "active" or not artifact.catalog_entry:
                continue
            if self._catalog and not self._catalog.get(artifact.catalog_entry):
                # Not yet in catalog — load descriptor and register
                desc_file = artifact.files.get("descriptor", "")
                if desc_file:
                    desc_path = self._space_dir(instance_id, space_id) / desc_file
                    if desc_path.exists():
                        try:
                            descriptor = json.loads(desc_path.read_text(encoding="utf-8"))
                            self._catalog.register(
                                name=artifact.catalog_entry,
                                description=descriptor.get("description", artifact.description),
                                source="workspace",
                            )
                            entry = self._catalog.get(artifact.catalog_entry)
                            if entry:
                                entry.home_space = artifact.home_space or space_id
                                entry.implementation = descriptor.get("implementation", "")
                                entry.stateful = descriptor.get("stateful", artifact.stateful)
                            logger.info("WORKSPACE_REGISTER: artifact=%s catalog_entry=%s source=workspace",
                                artifact.name, artifact.catalog_entry)
                        except Exception as exc:
                            logger.warning("WORKSPACE_REGISTER: failed for %s: %s", artifact.name, exc)
