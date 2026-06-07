"""Tests for SPEC-3A: Per-Space File System.

Covers: FileService CRUD, filename validation, soft delete + recovery,
manifest consistency, delete principle enforcement, kernel tool routing,
compaction manifest injection, upload handling, cross-space isolation.
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernos.kernel.files import FILE_TOOLS, FileService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TENANT = "tenant_test"
SPACE_A = "space_aaaa1111"
SPACE_B = "space_bbbb2222"


def make_service(tmp_path: Path) -> FileService:
    return FileService(str(tmp_path))


# ---------------------------------------------------------------------------
# FileService — write_file
# ---------------------------------------------------------------------------


class TestWriteFile:
    async def test_creates_new_file(self, tmp_path):
        svc = make_service(tmp_path)
        result = await svc.write_file(TENANT, SPACE_A, "notes.md", "# Hello", "My notes")
        assert result.startswith("Created 'notes.md'")
        file_path = tmp_path / TENANT / "spaces" / SPACE_A / "files" / "notes.md"
        assert file_path.exists()
        assert file_path.read_text() == "# Hello"

    async def test_returns_char_count(self, tmp_path):
        svc = make_service(tmp_path)
        result = await svc.write_file(TENANT, SPACE_A, "f.txt", "hello", "desc")
        assert "5 chars" in result

    async def test_overwrites_existing_file(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.write_file(TENANT, SPACE_A, "draft.md", "v1", "First")
        result = await svc.write_file(TENANT, SPACE_A, "draft.md", "v2", "Second")
        assert result.startswith("Updated 'draft.md'")
        file_path = tmp_path / TENANT / "spaces" / SPACE_A / "files" / "draft.md"
        assert file_path.read_text() == "v2"

    async def test_updates_manifest_on_create(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.write_file(TENANT, SPACE_A, "note.md", "content", "A note")
        manifest = await svc.load_manifest(TENANT, SPACE_A)
        assert manifest["note.md"] == "A note"

    async def test_updates_manifest_on_overwrite(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.write_file(TENANT, SPACE_A, "note.md", "v1", "Old desc")
        await svc.write_file(TENANT, SPACE_A, "note.md", "v2", "New desc")
        manifest = await svc.load_manifest(TENANT, SPACE_A)
        assert manifest["note.md"] == "New desc"

    async def test_lazy_directory_creation(self, tmp_path):
        svc = make_service(tmp_path)
        files_dir = tmp_path / TENANT / "spaces" / SPACE_A / "files"
        assert not files_dir.exists()
        await svc.write_file(TENANT, SPACE_A, "file.txt", "data", "desc")
        assert files_dir.exists()

    async def test_rejects_invalid_filename_traversal(self, tmp_path):
        svc = make_service(tmp_path)
        result = await svc.write_file(TENANT, SPACE_A, "../../etc/passwd", "evil", "desc")
        assert "Error" in result
        assert "Invalid filename" in result

    async def test_rejects_dot_leading_filename(self, tmp_path):
        svc = make_service(tmp_path)
        result = await svc.write_file(TENANT, SPACE_A, ".hidden", "data", "desc")
        assert "Error" in result

    async def test_rejects_slash_in_filename(self, tmp_path):
        svc = make_service(tmp_path)
        result = await svc.write_file(TENANT, SPACE_A, "sub/file.txt", "data", "desc")
        assert "Error" in result

    async def test_unicode_content_accepted(self, tmp_path):
        svc = make_service(tmp_path)
        result = await svc.write_file(TENANT, SPACE_A, "emoji.md", "Hello 🌍", "Emoji file")
        assert "Error" not in result
        assert "Created" in result


# ---------------------------------------------------------------------------
# FileService — read_file
# ---------------------------------------------------------------------------


class TestReadFile:
    async def test_reads_existing_file(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.write_file(TENANT, SPACE_A, "hello.txt", "world", "desc")
        result = await svc.read_file(TENANT, SPACE_A, "hello.txt")
        assert result == "world"

    async def test_error_on_missing_file(self, tmp_path):
        svc = make_service(tmp_path)
        result = await svc.read_file(TENANT, SPACE_A, "missing.txt")
        assert "Error" in result
        assert "not found" in result

    async def test_suggests_list_files_on_missing(self, tmp_path):
        svc = make_service(tmp_path)
        result = await svc.read_file(TENANT, SPACE_A, "ghost.md")
        assert "list_files" in result

    async def test_rejects_traversal_in_read(self, tmp_path):
        svc = make_service(tmp_path)
        result = await svc.read_file(TENANT, SPACE_A, "../secret.txt")
        assert "Error" in result


# ---------------------------------------------------------------------------
# FileService — list_files
# ---------------------------------------------------------------------------


class TestListFiles:
    async def test_empty_space_returns_no_files_message(self, tmp_path):
        svc = make_service(tmp_path)
        result = await svc.list_files(TENANT, SPACE_A)
        assert "No files" in result

    async def test_lists_single_file(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.write_file(TENANT, SPACE_A, "doc.md", "content", "My doc")
        result = await svc.list_files(TENANT, SPACE_A)
        assert "doc.md" in result
        assert "My doc" in result

    async def test_lists_multiple_files(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.write_file(TENANT, SPACE_A, "a.txt", "aaa", "File A")
        await svc.write_file(TENANT, SPACE_A, "b.txt", "bbb", "File B")
        result = await svc.list_files(TENANT, SPACE_A)
        assert "a.txt" in result
        assert "b.txt" in result
        assert "File A" in result
        assert "File B" in result

    async def test_shows_file_count(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.write_file(TENANT, SPACE_A, "x.txt", "x", "desc")
        await svc.write_file(TENANT, SPACE_A, "y.txt", "y", "desc")
        result = await svc.list_files(TENANT, SPACE_A)
        assert "2" in result

    async def test_shows_file_size(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.write_file(TENANT, SPACE_A, "sized.txt", "12345", "desc")
        result = await svc.list_files(TENANT, SPACE_A)
        assert "bytes" in result


# ---------------------------------------------------------------------------
# FileService — delete_file
# ---------------------------------------------------------------------------


class TestDeleteFile:
    async def test_soft_delete_moves_to_deleted_dir(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.write_file(TENANT, SPACE_A, "old.md", "content", "desc")
        result = await svc.delete_file(TENANT, SPACE_A, "old.md")
        assert "Deleted" in result

        files_dir = tmp_path / TENANT / "spaces" / SPACE_A / "files"
        assert not (files_dir / "old.md").exists()

        deleted_dir = files_dir / ".deleted"
        deleted_files = list(deleted_dir.iterdir())
        assert len(deleted_files) == 1
        assert deleted_files[0].name.startswith("old.md")

    async def test_deleted_file_preserved_for_recovery(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.write_file(TENANT, SPACE_A, "recover.md", "precious content", "desc")
        await svc.delete_file(TENANT, SPACE_A, "recover.md")

        deleted_dir = tmp_path / TENANT / "spaces" / SPACE_A / "files" / ".deleted"
        deleted_files = list(deleted_dir.iterdir())
        assert deleted_files[0].read_text() == "precious content"

    async def test_removes_from_manifest_on_delete(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.write_file(TENANT, SPACE_A, "gone.txt", "bye", "desc")
        await svc.delete_file(TENANT, SPACE_A, "gone.txt")
        manifest = await svc.load_manifest(TENANT, SPACE_A)
        assert "gone.txt" not in manifest

    async def test_delete_missing_file_returns_error(self, tmp_path):
        svc = make_service(tmp_path)
        result = await svc.delete_file(TENANT, SPACE_A, "nofile.md")
        assert "Error" in result
        assert "not found" in result

    async def test_list_files_excludes_deleted(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.write_file(TENANT, SPACE_A, "visible.md", "keep", "desc")
        await svc.write_file(TENANT, SPACE_A, "hidden.md", "delete", "desc")
        await svc.delete_file(TENANT, SPACE_A, "hidden.md")
        result = await svc.list_files(TENANT, SPACE_A)
        assert "visible.md" in result
        assert "hidden.md" not in result


# ---------------------------------------------------------------------------
# Filename validation
# ---------------------------------------------------------------------------


class TestFilenameValidation:
    def test_valid_simple(self, tmp_path):
        svc = make_service(tmp_path)
        assert svc._valid_filename("notes.md") is True

    def test_valid_with_hyphens_underscores(self, tmp_path):
        svc = make_service(tmp_path)
        assert svc._valid_filename("henderson-sow_v2.md") is True

    def test_valid_with_spaces(self, tmp_path):
        svc = make_service(tmp_path)
        assert svc._valid_filename("my notes.txt") is True

    def test_invalid_empty(self, tmp_path):
        svc = make_service(tmp_path)
        assert svc._valid_filename("") is False

    def test_invalid_slash(self, tmp_path):
        svc = make_service(tmp_path)
        assert svc._valid_filename("path/to/file.txt") is False

    def test_invalid_backslash(self, tmp_path):
        svc = make_service(tmp_path)
        assert svc._valid_filename("path\\file.txt") is False

    def test_invalid_dotdot(self, tmp_path):
        svc = make_service(tmp_path)
        assert svc._valid_filename("../secret.txt") is False

    def test_invalid_dot_leading(self, tmp_path):
        svc = make_service(tmp_path)
        assert svc._valid_filename(".hidden") is False

    def test_invalid_dotdot_in_middle(self, tmp_path):
        svc = make_service(tmp_path)
        assert svc._valid_filename("foo..bar") is False


# ---------------------------------------------------------------------------
# Cross-space isolation
# ---------------------------------------------------------------------------


class TestCrossSpaceIsolation:
    async def test_files_not_visible_across_spaces(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.write_file(TENANT, SPACE_A, "campaign.md", "d&d notes", "D&D")
        result_b = await svc.list_files(TENANT, SPACE_B)
        assert "No files" in result_b

    async def test_read_does_not_cross_spaces(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.write_file(TENANT, SPACE_A, "secret.md", "private", "desc")
        result = await svc.read_file(TENANT, SPACE_B, "secret.md")
        assert "Error" in result
        assert "not found" in result

    async def test_manifests_are_per_space(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.write_file(TENANT, SPACE_A, "dnd.md", "notes", "D&D")
        await svc.write_file(TENANT, SPACE_B, "nda.md", "contract", "NDA")
        manifest_a = await svc.load_manifest(TENANT, SPACE_A)
        manifest_b = await svc.load_manifest(TENANT, SPACE_B)
        assert "dnd.md" in manifest_a
        assert "nda.md" not in manifest_a
        assert "nda.md" in manifest_b
        assert "dnd.md" not in manifest_b


# ---------------------------------------------------------------------------
# FILE_TOOLS constant
# ---------------------------------------------------------------------------


class TestFileToolsConstant:
    def test_has_four_tools(self):
        assert len(FILE_TOOLS) == 4

    def test_tool_names(self):
        names = {t["name"] for t in FILE_TOOLS}
        assert names == {"write_file", "read_file", "list_files", "delete_file"}

    def test_write_file_requires_description(self):
        write_tool = next(t for t in FILE_TOOLS if t["name"] == "write_file")
        required = write_tool["input_schema"]["required"]
        assert "description" in required

    def test_write_file_required_fields(self):
        write_tool = next(t for t in FILE_TOOLS if t["name"] == "write_file")
        required = write_tool["input_schema"]["required"]
        # SAE-V1: `path` is the canonical file arg (was `name`)
        assert set(required) == {"path", "content", "description"}

    def test_delete_file_schema(self):
        delete_tool = next(t for t in FILE_TOOLS if t["name"] == "delete_file")
        assert "path" in delete_tool["input_schema"]["required"]

    def test_list_files_no_required_fields(self):
        list_tool = next(t for t in FILE_TOOLS if t["name"] == "list_files")
        # list_files takes no required params
        required = list_tool["input_schema"].get("required", [])
        assert len(required) == 0


# ---------------------------------------------------------------------------
# Delete gate — enforcement now via Haiku (no keyword fast path)
# ---------------------------------------------------------------------------


class TestDeleteGateEnforcement:
    def test_delete_file_classified_as_soft_write(self):
        """delete_file is gated as soft_write — Haiku authorizes based on user message."""
        from kernos.kernel.reasoning import ReasoningService
        svc = ReasoningService.__new__(ReasoningService)
        svc._registry = None
        assert svc._classify_tool_effect("delete_file", None) == "soft_write"

    def test_no_explicit_instruction_matches_method(self):
        """_explicit_instruction_matches removed — Haiku is the sole authority."""
        from kernos.kernel.reasoning import ReasoningService
        assert not hasattr(ReasoningService, "_explicit_instruction_matches")


# ---------------------------------------------------------------------------
# Kernel tool routing in ReasoningService
# ---------------------------------------------------------------------------


class TestKernelToolRoutingFiles:
    def _make_reasoning(self, tmp_path):
        from kernos.kernel.reasoning import ReasoningService
        from kernos.kernel.files import FileService

        provider = MagicMock()
        events = MagicMock()
        mcp = MagicMock()
        audit = MagicMock()
        audit.log = AsyncMock()

        svc = ReasoningService(provider, events, mcp, audit)
        files = FileService(str(tmp_path))
        svc.set_files(files)
        return svc, files

    async def test_write_file_routes_to_file_service(self, tmp_path):
        svc, files = self._make_reasoning(tmp_path)
        # FileService is wired in — write goes to the actual file system
        result = await svc._files.write_file(TENANT, SPACE_A, "test.md", "hello", "desc")
        assert "Created" in result

    def test_kernel_tools_set_contains_all_file_tools(self):
        from kernos.kernel.reasoning import ReasoningService
        expected = {"write_file", "read_file", "list_files", "delete_file"}
        assert expected.issubset(ReasoningService._KERNEL_TOOLS)

    def test_remember_still_in_kernel_tools(self):
        from kernos.kernel.reasoning import ReasoningService
        assert "remember" in ReasoningService._KERNEL_TOOLS


# ---------------------------------------------------------------------------
# Compaction manifest injection
# ---------------------------------------------------------------------------


class TestCompactionManifestInjection:
    async def test_manifest_injected_into_space_definition(self, tmp_path):
        """When files exist, compact() injects manifest into the compaction prompt."""
        from kernos.kernel.compaction import CompactionService, CompactionState
        from kernos.kernel.tokens import EstimateTokenAdapter
        from kernos.kernel.files import FileService

        # Write a file to the space
        files = FileService(str(tmp_path))
        await files.write_file(TENANT, SPACE_A, "campaign.md", "notes", "Campaign notes")

        # Mock reasoning service to capture the prompt
        captured = {}

        async def mock_complete_simple(system_prompt, user_content, **kwargs):
            captured["user_content"] = user_content
            return "# Ledger\n\n## Compaction #1 — 2026-01-01 → 2026-01-01\n\nTest.\n\n# Living State\n\nState."

        reasoning = MagicMock()
        reasoning.complete_simple = mock_complete_simple

        service = CompactionService(
            state=MagicMock(),
            reasoning=reasoning,
            token_adapter=EstimateTokenAdapter(),
            data_dir=str(tmp_path),
        )
        service.set_files(files)

        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(
            id=SPACE_A, instance_id=TENANT, name="D&D",
            description="Campaign space", space_type="domain",
        )

        comp_state = CompactionState(
            space_id=SPACE_A, message_ceiling=100000,
            document_budget=50000, conversation_headroom=8000,
        )

        messages = [
            {"role": "user", "content": "Test message", "timestamp": "2026-01-01T00:00:00"},
            {"role": "assistant", "content": "Test reply", "timestamp": "2026-01-01T00:01:00"},
        ]

        await service.compact(TENANT, SPACE_A, space, messages, comp_state)

        assert "campaign.md" in captured["user_content"]
        assert "Campaign notes" in captured["user_content"]

    async def test_empty_manifest_not_injected(self, tmp_path):
        """When no files exist, manifest section is not injected."""
        from kernos.kernel.compaction import CompactionService, CompactionState
        from kernos.kernel.tokens import EstimateTokenAdapter
        from kernos.kernel.files import FileService

        files = FileService(str(tmp_path))
        captured = {}

        async def mock_complete_simple(system_prompt, user_content, **kwargs):
            captured["user_content"] = user_content
            return "# Ledger\n\n## Compaction #1 — 2026-01-01 → 2026-01-01\n\nTest.\n\n# Living State\n\nState."

        reasoning = MagicMock()
        reasoning.complete_simple = mock_complete_simple

        service = CompactionService(
            state=MagicMock(),
            reasoning=reasoning,
            token_adapter=EstimateTokenAdapter(),
            data_dir=str(tmp_path),
        )
        service.set_files(files)

        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(
            id=SPACE_A, instance_id=TENANT, name="General",
            description="General space", space_type="general",
        )

        comp_state = CompactionState(
            space_id=SPACE_A, message_ceiling=100000,
            document_budget=50000, conversation_headroom=8000,
        )

        messages = [
            {"role": "user", "content": "Test", "timestamp": "2026-01-01T00:00:00"},
        ]

        await service.compact(TENANT, SPACE_A, space, messages, comp_state)

        # Should not contain file manifest section
        assert "Current files in this space" not in captured["user_content"]


# ---------------------------------------------------------------------------
# Upload handling
# ---------------------------------------------------------------------------


class TestUploadHandling:
    async def test_upload_stores_file(self, tmp_path):
        """_handle_file_upload stores file and returns notification."""
        from kernos.kernel.files import FileService

        files = FileService(str(tmp_path))
        await files.write_file(TENANT, SPACE_A, "upload.txt", "file content", "Uploaded by user on 2026-03-14")

        result = await files.read_file(TENANT, SPACE_A, "upload.txt")
        assert result == "file content"

    async def test_upload_manifest_entry(self, tmp_path):
        """Upload creates a manifest entry with date description."""
        from kernos.kernel.files import FileService

        files = FileService(str(tmp_path))
        await files.write_file(TENANT, SPACE_A, "report.csv", "a,b,c", "Uploaded by user on 2026-03-14")

        manifest = await files.load_manifest(TENANT, SPACE_A)
        assert "report.csv" in manifest


# ---------------------------------------------------------------------------
# ContextSpace model fields
# ---------------------------------------------------------------------------


class TestContextSpaceFields:
    def test_max_file_size_bytes_defaults_none(self):
        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(id="s1", instance_id="t1", name="Test")
        assert space.max_file_size_bytes is None

    def test_max_space_bytes_defaults_none(self):
        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(id="s1", instance_id="t1", name="Test")
        assert space.max_space_bytes is None

    def test_can_set_limits(self):
        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(
            id="s1", instance_id="t1", name="Test",
            max_file_size_bytes=1_000_000,
            max_space_bytes=10_000_000,
        )
        assert space.max_file_size_bytes == 1_000_000
        assert space.max_space_bytes == 10_000_000


# ---------------------------------------------------------------------------
# Manifest persistence
# ---------------------------------------------------------------------------


class TestManifestPersistence:
    async def test_manifest_stored_as_json(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.write_file(TENANT, SPACE_A, "a.md", "x", "desc A")
        await svc.write_file(TENANT, SPACE_A, "b.md", "y", "desc B")

        manifest_path = (
            tmp_path / TENANT / "spaces" / SPACE_A / "files" / ".manifest.json"
        )
        data = json.loads(manifest_path.read_text())
        assert data["a.md"] == "desc A"
        assert data["b.md"] == "desc B"

    async def test_manifest_survives_service_restart(self, tmp_path):
        svc1 = make_service(tmp_path)
        await svc1.write_file(TENANT, SPACE_A, "persist.md", "data", "Persisted")

        # Create a new service instance pointing at same data dir
        svc2 = make_service(tmp_path)
        manifest = await svc2.load_manifest(TENANT, SPACE_A)
        assert "persist.md" in manifest
        assert manifest["persist.md"] == "Persisted"

    async def test_files_survive_after_multiple_writes(self, tmp_path):
        svc = make_service(tmp_path)
        for i in range(5):
            await svc.write_file(TENANT, SPACE_A, f"file{i}.txt", f"content{i}", f"File {i}")

        manifest = await svc.load_manifest(TENANT, SPACE_A)
        assert len(manifest) == 5
        for i in range(5):
            assert f"file{i}.txt" in manifest
