"""Per-space file system for KERNOS agents.

Files are persistent text artifacts (drafts, notes, configs, outputs) scoped
to a single context space. The agent creates and manages them via four
kernel-managed tools: write_file, read_file, list_files, delete_file.
"""
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from kernos.utils import utc_now

logger = logging.getLogger(__name__)




# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

FILE_TOOLS = [
    {
        "name": "write_file",
        "description": (
            "Create or update a text file. Defaults to the current space (local). "
            "If a file exists in a parent space and the change benefits ALL contexts, "
            "set target_space_id to the parent to write a universal update. "
            "If the change is specific to THIS context only, omit target_space_id "
            "to write a local override."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path/name including extension (e.g. 'henderson-sow.md', 'session-notes.txt')",
                },
                "content": {
                    "type": "string",
                    "description": "The full text content of the file",
                },
                "description": {
                    "type": "string",
                    "description": "One-sentence description of what this file is — shown in file listings",
                },
                "target_space_id": {
                    "type": "string",
                    "description": "Optional: write to a parent space instead of the current space (for universal updates). Must be an ancestor in the scope chain.",
                },
            },
            "required": ["path", "content", "description"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the contents of a file in the current context space. "
            "Use list_files first if you're not sure what files exist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path/name to read",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": "List all files in the current context space with their descriptions.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "delete_file",
        "description": (
            "Delete a file from the current context space. "
            "The file is preserved for recovery but removed from listings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path/name to delete",
                }
            },
            "required": ["path"],
        },
    },
]


# ---------------------------------------------------------------------------
# FileService
# ---------------------------------------------------------------------------


class FileService:
    """Manages per-space file operations with scope chain resolution.

    Files walk up the parent chain on reads — if a file isn't in the
    current space, check parent, then grandparent, up to root.
    Writes default to the current space (local override).
    Text-only. Binary content is rejected.
    Soft deletes: files move to .deleted/ — never permanently removed.
    """

    def __init__(self, data_dir: str, state: Any = None) -> None:
        from kernos.utils import _safe_name
        self.data_dir = Path(data_dir)
        self._safe_name = _safe_name
        self._state = state  # Optional StateStore for scope chain resolution

    def set_state(self, state: Any) -> None:
        """Set the state store for scope chain resolution."""
        self._state = state

    def _space_files_dir(self, instance_id: str, space_id: str) -> Path:
        return (
            self.data_dir
            / self._safe_name(instance_id)
            / "spaces"
            / space_id
            / "files"
        )

    def _deleted_dir(self, instance_id: str, space_id: str) -> Path:
        return self._space_files_dir(instance_id, space_id) / ".deleted"

    async def _build_scope_chain(self, instance_id: str, space_id: str) -> list[str]:
        """Walk up the parent chain for scope chain resolution."""
        if not self._state:
            return [space_id]
        chain: list[str] = []
        current = space_id
        seen: set[str] = set()
        while current and current not in seen:
            space = await self._state.get_context_space(instance_id, current)
            if not space:
                break
            chain.append(current)
            seen.add(current)
            if not space.parent_id:
                break
            current = space.parent_id
        return chain if chain else [space_id]

    def _manifest_path(self, instance_id: str, space_id: str) -> Path:
        return self._space_files_dir(instance_id, space_id) / ".manifest.json"

    async def write_file(
        self,
        instance_id: str,
        space_id: str,
        name: str,
        content: str,
        description: str,
        target_space_id: str | None = None,
    ) -> str:
        """Create or overwrite a text file. Returns confirmation message.

        If target_space_id is provided, writes to that ancestor space (universal update).
        Otherwise writes to the current space (local, may shadow parent files).
        """
        # Validate text content
        try:
            content.encode("utf-8")
        except (UnicodeEncodeError, AttributeError):
            return "Error: File system currently supports text files only. Binary support (images, PDFs) is coming in a future update."

        # Validate filename
        if not self._valid_filename(name):
            return (
                f"Error: Invalid filename '{name}'. "
                "Use alphanumeric characters, hyphens, underscores, and dots."
            )

        # Determine write target
        write_to = space_id
        if target_space_id and target_space_id != space_id:
            chain = await self._build_scope_chain(instance_id, space_id)
            if target_space_id not in chain:
                return f"Error: Can only write to ancestor spaces in the scope chain."
            write_to = target_space_id

        files_dir = self._space_files_dir(instance_id, write_to)
        files_dir.mkdir(parents=True, exist_ok=True)

        file_path = files_dir / name
        is_update = file_path.exists()
        file_path.write_text(content, encoding="utf-8")

        await self._update_manifest(instance_id, write_to, name, description)

        action = "Updated" if is_update else "Created"

        if write_to != space_id:
            logger.info("FILE_WRITE_PARENT: file=%s target=%s (universal update)", name, write_to)
            return f"{action} '{name}' ({len(content)} chars) in parent space {write_to}. Description: {description}"

        # Check if this shadows a parent file
        chain = await self._build_scope_chain(instance_id, space_id)
        for ancestor_id in chain[1:]:
            anc_path = self._space_files_dir(instance_id, ancestor_id) / name
            if anc_path.exists():
                logger.info("FILE_WRITE_OVERRIDE: file=%s shadows=%s", name, ancestor_id)
                return f"{action} '{name}' ({len(content)} chars) as local override (parent has original in {ancestor_id}). Description: {description}"
                break

        logger.info("FILE_WRITE space=%s name=%s size=%d", write_to, name, len(content))
        return f"{action} '{name}' ({len(content)} chars). Description: {description}"

    async def read_file(
        self,
        instance_id: str,
        space_id: str,
        name: str,
    ) -> str:
        """Read a file's contents. Walks scope chain if not found locally."""
        if not self._valid_filename(name):
            return f"Error: Invalid filename '{name}'."

        # Try current space first
        file_path = self._space_files_dir(instance_id, space_id) / name
        if file_path.exists():
            logger.info("FILE_READ space=%s name=%s exists=True", space_id, name)
            return file_path.read_text(encoding="utf-8")

        # Walk scope chain
        chain = await self._build_scope_chain(instance_id, space_id)
        for ancestor_id in chain[1:]:
            ancestor_path = self._space_files_dir(instance_id, ancestor_id) / name
            if ancestor_path.exists():
                logger.info("FILE_SCOPE_CHAIN: file=%s found_in=%s (ancestor)", name, ancestor_id)
                return ancestor_path.read_text(encoding="utf-8")

        logger.info("FILE_READ space=%s name=%s exists=False", space_id, name)
        return f"Error: File '{name}' not found. Use list_files to see available files."

    async def list_files(
        self,
        instance_id: str,
        space_id: str,
    ) -> str:
        """List all files with descriptions. Includes inherited files from ancestors."""
        local_manifest = await self._load_manifest(instance_id, space_id)

        # Collect inherited files from ancestor spaces
        chain = await self._build_scope_chain(instance_id, space_id)
        inherited: dict[str, tuple[str, str]] = {}  # name → (description, source_space_id)
        for ancestor_id in chain[1:]:
            ancestor_manifest = await self._load_manifest(instance_id, ancestor_id)
            for fname, desc in ancestor_manifest.items():
                if fname not in local_manifest and fname not in inherited:
                    inherited[fname] = (desc, ancestor_id)

        total = len(local_manifest) + len(inherited)
        logger.info("FILE_LIST space=%s local=%d inherited=%d", space_id, len(local_manifest), len(inherited))
        if total == 0:
            return "No files in this space yet."

        lines = []
        for name, desc in sorted(local_manifest.items()):
            file_path = self._space_files_dir(instance_id, space_id) / name
            size = file_path.stat().st_size if file_path.exists() else 0
            # Check if this shadows a parent file
            shadow = ""
            for ancestor_id in chain[1:]:
                anc_path = self._space_files_dir(instance_id, ancestor_id) / name
                if anc_path.exists():
                    shadow = " (local override)"
                    break
            lines.append(f"  {name} ({size} bytes){shadow} — {desc}")

        for name, (desc, source_id) in sorted(inherited.items()):
            file_path = self._space_files_dir(instance_id, source_id) / name
            size = file_path.stat().st_size if file_path.exists() else 0
            lines.append(f"  {name} ({size} bytes) (inherited from {source_id}) — {desc}")

        return f"Files in this space ({total}):\n" + "\n".join(lines)

    async def delete_file(
        self,
        instance_id: str,
        space_id: str,
        name: str,
    ) -> str:
        """Soft delete — move to .deleted/, remove from manifest."""
        if not self._valid_filename(name):
            return f"Error: Invalid filename '{name}'."

        file_path = self._space_files_dir(instance_id, space_id) / name
        if not file_path.exists():
            logger.info("FILE_DELETE space=%s name=%s exists=False", space_id, name)
            return f"Error: File '{name}' not found."

        logger.info("FILE_DELETE space=%s name=%s exists=True", space_id, name)
        deleted_dir = self._deleted_dir(instance_id, space_id)
        deleted_dir.mkdir(parents=True, exist_ok=True)

        # Timestamp in filename — replace colons for filesystem compatibility
        ts = utc_now().replace(":", "-")
        dest = deleted_dir / f"{name}_{ts}"
        file_path.rename(dest)

        await self._remove_from_manifest(instance_id, space_id, name)

        return f"Deleted '{name}'. File preserved for recovery."

    def _valid_filename(self, name: str) -> bool:
        """Validate filename — no path traversal, reasonable characters."""
        if not name or "/" in name or "\\" in name or ".." in name:
            return False
        if name.startswith("."):
            return False
        return bool(re.match(r'^[\w\-. ]+$', name))

    # --- Manifest CRUD ---

    async def _load_manifest(self, instance_id: str, space_id: str) -> dict[str, str]:
        """Load the file manifest for a space.

        On JSON parse failure: rebuilds the manifest from actual files on disk
        (descriptions are lost but files are preserved). Logs a WARNING so the
        corruption event is visible in logs.
        """
        manifest_path = self._manifest_path(instance_id, space_id)
        if not manifest_path.exists():
            return {}
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "MANIFEST_CORRUPT: Failed to parse manifest for %s/%s: %s — "
                "rebuilding from disk. Descriptions will be empty.",
                instance_id, space_id, exc,
            )
            return await self._rebuild_manifest(instance_id, space_id)

    async def _rebuild_manifest(
        self, instance_id: str, space_id: str
    ) -> dict[str, str]:
        """Rebuild manifest by scanning the files directory.

        Called when the manifest JSON is corrupt. Descriptions are lost.
        Writes the recovered manifest back to disk atomically.
        """
        files_dir = self._space_files_dir(instance_id, space_id)
        if not files_dir.exists():
            return {}
        manifest: dict[str, str] = {}
        for p in files_dir.iterdir():
            if p.is_file() and not p.name.startswith("."):
                manifest[p.name] = "(description unavailable — rebuilt after corruption)"
        logger.warning(
            "MANIFEST_REBUILD: recovered %d file(s) for %s/%s",
            len(manifest), instance_id, space_id,
        )
        manifest_path = self._manifest_path(instance_id, space_id)
        await self._write_manifest_atomic(manifest_path, manifest)
        return manifest

    def _write_manifest_atomic(self, manifest_path: Path, manifest: dict) -> None:
        """Write manifest JSON atomically: temp file → os.replace.

        Prevents partial writes from corrupting the manifest during
        concurrent writes or process interruptions (e.g. 429 retry delays).
        """
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=manifest_path.parent, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(manifest, indent=2))
            os.replace(tmp_path, str(manifest_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    async def load_manifest(self, instance_id: str, space_id: str) -> dict[str, str]:
        """Public alias for _load_manifest (used by CompactionService)."""
        return await self._load_manifest(instance_id, space_id)

    async def _update_manifest(
        self, instance_id: str, space_id: str, name: str, description: str
    ) -> None:
        manifest = await self._load_manifest(instance_id, space_id)
        manifest[name] = description
        manifest_path = self._manifest_path(instance_id, space_id)
        self._write_manifest_atomic(manifest_path, manifest)

    async def _remove_from_manifest(
        self, instance_id: str, space_id: str, name: str
    ) -> None:
        manifest = await self._load_manifest(instance_id, space_id)
        manifest.pop(name, None)
        manifest_path = self._manifest_path(instance_id, space_id)
        self._write_manifest_atomic(manifest_path, manifest)
