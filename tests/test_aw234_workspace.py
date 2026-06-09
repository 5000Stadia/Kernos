"""Tests for AW-2/3/4: Workspace Manifest + Tool Registration + Builder Flow.

Covers: manifest CRUD, artifact versioning, descriptor validation,
registration flow, workspace tool dispatch, gate classification.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.workspace import (
    WorkspaceManager, Artifact, WorkspaceManifest,
    MANAGE_WORKSPACE_TOOL, REGISTER_TOOL_TOOL,
)
from kernos.kernel.tool_catalog import ToolCatalog, CatalogEntry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_workspace(tmp_path) -> tuple[WorkspaceManager, ToolCatalog]:
    catalog = ToolCatalog()
    ws = WorkspaceManager(data_dir=str(tmp_path), catalog=catalog)
    return ws, catalog


# ---------------------------------------------------------------------------
# Manifest CRUD
# ---------------------------------------------------------------------------


class TestManifestLifecycle:
    async def test_fresh_manifest_is_empty(self, tmp_path):
        ws, _ = _make_workspace(tmp_path)
        manifest = await ws.load_manifest("t1", "sp1")
        assert manifest.artifacts == []
        assert manifest.instance_id == "t1"
        assert manifest.space_id == "sp1"

    async def test_add_artifact(self, tmp_path):
        ws, _ = _make_workspace(tmp_path)
        msg, artifact = await ws.add_artifact("t1", "sp1", {
            "name": "invoice_tracker",
            "type": "data_tool",
            "description": "Track invoices",
            "files": {"implementation": "invoice_tracker.py", "store": "invoices.json"},
        })
        assert "invoice_tracker" in msg
        assert artifact.name == "invoice_tracker"
        assert artifact.version == 1
        assert artifact.status == "active"
        assert artifact.id.startswith("artifact_")

    async def test_manifest_persists(self, tmp_path):
        ws, _ = _make_workspace(tmp_path)
        await ws.add_artifact("t1", "sp1", {
            "name": "test_tool", "type": "script",
            "description": "A test", "files": {},
        })
        # Clear cache and reload
        ws._loaded_manifests.clear()
        manifest = await ws.load_manifest("t1", "sp1")
        assert len(manifest.artifacts) == 1
        assert manifest.artifacts[0].name == "test_tool"

    async def test_update_increments_version(self, tmp_path):
        ws, _ = _make_workspace(tmp_path)
        _, artifact = await ws.add_artifact("t1", "sp1", {
            "name": "tool", "type": "script", "description": "v1", "files": {},
        })
        result = await ws.update_artifact("t1", "sp1", artifact.id, {"description": "v2"})
        assert "version 2" in result

        manifest = await ws.load_manifest("t1", "sp1")
        assert manifest.artifacts[0].version == 2
        assert manifest.artifacts[0].description == "v2"

    async def test_archive_sets_status(self, tmp_path):
        ws, catalog = _make_workspace(tmp_path)
        _, artifact = await ws.add_artifact("t1", "sp1", {
            "name": "doomed", "type": "script", "description": "soon archived",
            "files": {}, "catalog_entry": "doomed_tool",
        })
        # Register in catalog
        catalog.register("doomed_tool", "Doomed", "workspace")

        result = await ws.archive_artifact("t1", "sp1", artifact.id)
        assert "Archived" in result

        manifest = await ws.load_manifest("t1", "sp1")
        assert manifest.artifacts[0].status == "archived"
        # Removed from catalog
        assert catalog.get("doomed_tool") is None

    async def test_list_artifacts_formatted(self, tmp_path):
        ws, _ = _make_workspace(tmp_path)
        await ws.add_artifact("t1", "sp1", {
            "name": "tracker", "type": "data_tool",
            "description": "Tracks things", "files": {"implementation": "tracker.py"},
        })
        result = await ws.list_artifacts("t1", "sp1")
        assert "tracker" in result
        assert "data_tool" in result

    async def test_list_empty_workspace(self, tmp_path):
        ws, _ = _make_workspace(tmp_path)
        result = await ws.list_artifacts("t1", "sp1")
        assert "No artifacts" in result


# ---------------------------------------------------------------------------
# Tool Registration
# ---------------------------------------------------------------------------


class TestToolRegistration:
    async def test_register_valid_tool(self, tmp_path):
        ws, catalog = _make_workspace(tmp_path)
        space_dir = tmp_path / "t1" / "spaces" / "sp1" / "files"
        space_dir.mkdir(parents=True)

        # Write implementation
        (space_dir / "my_tool.py").write_text(
            "def execute(data): return {'status': 'ok'}")

        # Write descriptor
        descriptor = {
            "name": "my_tool",
            "description": "A custom tool",
            "input_schema": {"type": "object", "properties": {}},
            "implementation": "my_tool.py",
        }
        (space_dir / "my_tool.tool.json").write_text(json.dumps(descriptor))

        result = await ws.register_tool("t1", "sp1", "my_tool.tool.json")
        assert "Registered" in result
        assert catalog.get("my_tool") is not None
        assert catalog.get("my_tool").source == "workspace"

    async def test_register_forgives_missing_schema_and_object_impl(self, tmp_path):
        # v1 self-test Test 7 live shape: no input_schema + object-shaped
        # implementation should register on the FIRST call, not bounce.
        ws, catalog = _make_workspace(tmp_path)
        space_dir = tmp_path / "t1" / "spaces" / "sp1" / "files"
        space_dir.mkdir(parents=True)
        (space_dir / "coin_flip_tool.py").write_text(
            "import random\ndef execute(data): return {'result': random.choice(['heads','tails'])}")
        descriptor = {
            "name": "coin_flip_tool",
            "description": "Flip a coin",
            # no input_schema at all
            "implementation": {"file": "coin_flip_tool.py", "entrypoint": "execute"},
        }
        (space_dir / "coin_flip_tool.tool.json").write_text(json.dumps(descriptor))

        result = await ws.register_tool("t1", "sp1", "coin_flip_tool.tool.json")
        assert "Registered" in result, result
        assert catalog.get("coin_flip_tool") is not None

    async def test_register_missing_descriptor(self, tmp_path):
        ws, _ = _make_workspace(tmp_path)
        result = await ws.register_tool("t1", "sp1", "nonexistent.tool.json")
        assert "not found" in result.lower()

    async def test_register_missing_implementation(self, tmp_path):
        ws, _ = _make_workspace(tmp_path)
        space_dir = tmp_path / "t1" / "spaces" / "sp1" / "files"
        space_dir.mkdir(parents=True)
        descriptor = {
            "name": "broken_tool",
            "description": "Missing impl",
            "input_schema": {"type": "object"},
            "implementation": "nonexistent.py",
        }
        (space_dir / "broken.tool.json").write_text(json.dumps(descriptor))

        result = await ws.register_tool("t1", "sp1", "broken.tool.json")
        assert "not found" in result.lower()

    async def test_register_invalid_name(self, tmp_path):
        ws, _ = _make_workspace(tmp_path)
        space_dir = tmp_path / "t1" / "spaces" / "sp1" / "files"
        space_dir.mkdir(parents=True)
        (space_dir / "impl.py").write_text("def execute(d): return {}")
        descriptor = {
            "name": "Bad-Name!",
            "description": "Invalid",
            "input_schema": {"type": "object"},
            "implementation": "impl.py",
        }
        (space_dir / "bad.tool.json").write_text(json.dumps(descriptor))

        result = await ws.register_tool("t1", "sp1", "bad.tool.json")
        assert "snake_case" in result.lower()

    async def test_register_auto_adds_to_manifest(self, tmp_path):
        ws, catalog = _make_workspace(tmp_path)
        space_dir = tmp_path / "t1" / "spaces" / "sp1" / "files"
        space_dir.mkdir(parents=True)
        (space_dir / "auto_tool.py").write_text("def execute(d): return {}")
        descriptor = {
            "name": "auto_tool",
            "description": "Auto-tracked",
            "input_schema": {"type": "object", "properties": {}},
            "implementation": "auto_tool.py",
        }
        (space_dir / "auto_tool.tool.json").write_text(json.dumps(descriptor))

        await ws.register_tool("t1", "sp1", "auto_tool.tool.json")

        manifest = await ws.load_manifest("t1", "sp1")
        assert len(manifest.artifacts) == 1
        assert manifest.artifacts[0].catalog_entry == "auto_tool"


# ---------------------------------------------------------------------------
# Workspace Tool Execution
# ---------------------------------------------------------------------------


class TestWorkspaceToolExecution:
    async def test_execute_workspace_tool(self, tmp_path):
        ws, catalog = _make_workspace(tmp_path)
        space_dir = tmp_path / "t1" / "spaces" / "sp1" / "files"
        space_dir.mkdir(parents=True)

        # Write a simple tool
        (space_dir / "adder.py").write_text(
            "def execute(data):\n"
            "    a = data.get('a', 0)\n"
            "    b = data.get('b', 0)\n"
            "    return {'sum': a + b}\n"
        )

        # Register it
        catalog.register("adder", "Add two numbers", "workspace")
        entry = catalog.get("adder")
        entry.home_space = "sp1"
        entry.implementation = "adder.py"

        result_str = await ws.execute_workspace_tool(
            "t1", "adder", {"a": 3, "b": 7}, str(tmp_path))
        result = json.loads(result_str)
        assert result.get("sum") == 10

    async def test_execute_missing_tool(self, tmp_path):
        ws, catalog = _make_workspace(tmp_path)
        result_str = await ws.execute_workspace_tool(
            "t1", "ghost_tool", {}, str(tmp_path))
        result = json.loads(result_str)
        assert "error" in result


# ---------------------------------------------------------------------------
# Lazy Registration
# ---------------------------------------------------------------------------


class TestLazyRegistration:
    async def test_ensure_registered_loads_from_manifest(self, tmp_path):
        ws, catalog = _make_workspace(tmp_path)
        space_dir = tmp_path / "t1" / "spaces" / "sp1" / "files"
        space_dir.mkdir(parents=True)

        # Pre-create a manifest with an artifact
        (space_dir / "lazy_tool.py").write_text("def execute(d): return {}")
        descriptor = {
            "name": "lazy_tool",
            "description": "Lazy loaded",
            "input_schema": {"type": "object"},
            "implementation": "lazy_tool.py",
        }
        (space_dir / "lazy_tool.tool.json").write_text(json.dumps(descriptor))
        manifest = {
            "version": 1, "instance_id": "t1", "space_id": "sp1",
            "artifacts": [{
                "id": "artifact_lazy01",
                "name": "lazy_tool",
                "type": "data_tool",
                "description": "Lazy loaded",
                "files": {"descriptor": "lazy_tool.tool.json", "implementation": "lazy_tool.py"},
                "catalog_entry": "lazy_tool",
                "created_at": _now(), "last_modified": _now(),
                "version": 1, "status": "active",
                "home_space": "sp1", "stateful": True,
            }],
        }
        (space_dir / "workspace_manifest.json").write_text(json.dumps(manifest))

        # Before ensure_registered, tool not in catalog
        assert catalog.get("lazy_tool") is None

        await ws.ensure_registered("t1", "sp1")

        # After, tool is registered
        assert catalog.get("lazy_tool") is not None
        assert catalog.get("lazy_tool").source == "workspace"


# ---------------------------------------------------------------------------
# Gate Classification
# ---------------------------------------------------------------------------


class TestGateClassification:
    def test_manage_workspace_list_is_read(self):
        from kernos.kernel.gate import DispatchGate
        gate = DispatchGate(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        assert gate.classify_tool_effect("manage_workspace", None, {"action": "list"}) == "read"

    def test_manage_workspace_add_is_soft_write(self):
        from kernos.kernel.gate import DispatchGate
        gate = DispatchGate(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        assert gate.classify_tool_effect("manage_workspace", None, {"action": "add"}) == "soft_write"

    def test_register_tool_is_soft_write(self):
        from kernos.kernel.gate import DispatchGate
        gate = DispatchGate(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        assert gate.classify_tool_effect("register_tool", None) == "soft_write"


# ---------------------------------------------------------------------------
# Tool Schemas
# ---------------------------------------------------------------------------


class TestToolSchemas:
    def test_manage_workspace_schema(self):
        assert MANAGE_WORKSPACE_TOOL["name"] == "manage_workspace"
        assert "action" in MANAGE_WORKSPACE_TOOL["input_schema"]["required"]

    def test_register_tool_schema(self):
        assert REGISTER_TOOL_TOOL["name"] == "register_tool"
        assert "descriptor_file" in REGISTER_TOOL_TOOL["input_schema"]["required"]
