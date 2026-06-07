"""Integration tests for the WORKSHOP-EXTERNAL-SERVICE-PRIMITIVE
dispatcher wiring in WorkspaceManager.

Covers:
- register_tool runs the extended descriptor parser, the authoring-
  pattern validator, and records the registration hash on the catalog
  entry.
- register_tool refuses tools with authoring-pattern findings unless
  force=True.
- service-bound tools route through the new dispatch path; the four
  runtime checks fire; the tool receives a context with credentials.
- non-service-bound tools continue through the existing subprocess
  path unchanged.
- audit entries are written for service-bound invocations.
"""

import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from kernos.kernel.services import (
    ServiceRegistry,
    parse_service_descriptor,
)
from kernos.kernel.tool_catalog import ToolCatalog
from kernos.kernel.workspace import WorkspaceManager


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


class FakeAuditStore:
    """Captures audit entries so tests can assert on what was written."""

    def __init__(self):
        self.entries: list[tuple[str, dict]] = []

    async def log(self, instance_id: str, entry: dict) -> None:
        self.entries.append((instance_id, entry))


@pytest.fixture
def env_key(monkeypatch):
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY", Fernet.generate_key().decode())


@pytest.fixture
def workspace(tmp_path, env_key):
    catalog = ToolCatalog()
    services = ServiceRegistry()
    services.register(parse_service_descriptor({
        "service_id": "notion",
        "display_name": "Notion",
        "auth_type": "api_token",
        "operations": ["read_pages", "write_pages"],
    }))
    audit = FakeAuditStore()
    ws = WorkspaceManager(
        data_dir=str(tmp_path),
        catalog=catalog,
        service_registry=services,
        audit_store=audit,
    )
    # INSTALL-FOR-STOCK-CONNECTORS: dispatch is now disabled-by-default
    # without an explicit service-state record. These tests exercise
    # the happy-path of a known-good service, so we enable notion in
    # the install state. Tests of the disabled-refusal path live in
    # test_install_dispatch_enforcement.py.
    from kernos.setup.service_state import (
        ServiceStateSource,
        ServiceStateUpdatedBy,
    )
    ws.service_state_store().set(
        "notion",
        enabled=True,
        source=ServiceStateSource.SETUP,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
    )
    return ws, catalog, services, audit, tmp_path


def _write_descriptor_and_impl(
    workspace_dir: Path, *, name: str, descriptor_extras: dict, impl_src: str,
):
    workspace_dir.mkdir(parents=True, exist_ok=True)
    descriptor = {
        "name": name,
        "description": "test tool",
        "input_schema": {"type": "object"},
        "implementation": f"{name}.py",
    }
    descriptor.update(descriptor_extras)
    desc_path = workspace_dir / f"{name}.tool.json"
    impl_path = workspace_dir / f"{name}.py"
    desc_path.write_text(json.dumps(descriptor))
    impl_path.write_text(impl_src)
    return desc_path, impl_path


# ---------------------------------------------------------------------------
# register_tool extension
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_tool_records_extended_metadata(workspace):
    ws, catalog, _services, _audit, tmp_path = workspace
    space_dir = tmp_path / "discord_owner" / "spaces" / "space_a" / "files"
    _write_descriptor_and_impl(
        space_dir,
        name="reader",
        descriptor_extras={
            "service_id": "notion",
            "authority": ["read_pages"],
            "operations": [{"operation": "read_pages", "classification": "read"}],
            "audit_category": "notion",
        },
        impl_src="def execute(input_data, context): return {'ok': True}\n",
    )
    msg = await ws.register_tool("discord:owner", "space_a", "reader.tool.json")
    assert "Registered" in msg
    entry = catalog.get("reader")
    assert entry is not None
    assert entry.service_id == "notion"
    assert entry.descriptor_file == "reader.tool.json"
    assert entry.registration_hash != ""
    assert entry.force_registered is False


@pytest.mark.asyncio
async def test_register_tool_rejects_authoring_findings_without_force(workspace):
    ws, catalog, _services, _audit, tmp_path = workspace
    space_dir = tmp_path / "discord_owner" / "spaces" / "space_a" / "files"
    _write_descriptor_and_impl(
        space_dir,
        name="leaky",
        descriptor_extras={"gate_classification": "read"},
        impl_src=(
            "def execute(input_data, context):\n"
            "    with open('/etc/passwd') as f:\n"
            "        return {'leak': f.read()}\n"
        ),
    )
    msg = await ws.register_tool("discord:owner", "space_a", "leaky.tool.json")
    assert "Authoring-pattern validation rejected" in msg
    assert catalog.get("leaky") is None  # not registered


@pytest.mark.asyncio
async def test_register_tool_force_accepts_authoring_findings(workspace):
    ws, catalog, _services, _audit, tmp_path = workspace
    space_dir = tmp_path / "discord_owner" / "spaces" / "space_a" / "files"
    _write_descriptor_and_impl(
        space_dir,
        name="forced",
        descriptor_extras={"gate_classification": "read"},
        impl_src=(
            "def execute(input_data, context):\n"
            "    with open('/etc/hosts') as f:\n"
            "        return {'len': len(f.read())}\n"
        ),
    )
    msg = await ws.register_tool(
        "discord:owner", "space_a", "forced.tool.json", force=True,
    )
    assert "Registered" in msg
    entry = catalog.get("forced")
    assert entry is not None
    assert entry.force_registered is True


@pytest.mark.asyncio
async def test_register_tool_rejects_unknown_service(workspace):
    ws, catalog, _services, _audit, tmp_path = workspace
    space_dir = tmp_path / "discord_owner" / "spaces" / "space_a" / "files"
    _write_descriptor_and_impl(
        space_dir,
        name="phantom",
        descriptor_extras={
            "service_id": "ghost_service",
            "authority": ["nope"],
        },
        impl_src="def execute(input_data, context): return {}\n",
    )
    msg = await ws.register_tool("discord:owner", "space_a", "phantom.tool.json")
    assert "not registered" in msg or "not in the declared" in msg
    assert catalog.get("phantom") is None


# ---------------------------------------------------------------------------
# Service-bound dispatch path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_bound_dispatch_requires_member_id(workspace):
    ws, catalog, _services, _audit, tmp_path = workspace
    space_dir = tmp_path / "discord_owner" / "spaces" / "space_a" / "files"
    _write_descriptor_and_impl(
        space_dir,
        name="reader",
        descriptor_extras={
            "service_id": "notion",
            "authority": ["read_pages"],
            "operations": [{"operation": "read_pages", "classification": "read"}],
        },
        impl_src="def execute(input_data, context): return {'ok': True}\n",
    )
    await ws.register_tool("discord:owner", "space_a", "reader.tool.json")
    # Invoke without member_id — should refuse cleanly.
    result_str = await ws.execute_workspace_tool(
        "discord:owner", "reader", {}, str(tmp_path),
    )
    result = json.loads(result_str)
    assert "error" in result
    assert "member_id" in result["error"]


@pytest.mark.asyncio
async def test_service_bound_dispatch_succeeds_with_credential(workspace):
    ws, catalog, _services, audit, tmp_path = workspace
    space_dir = tmp_path / "discord_owner" / "spaces" / "space_a" / "files"
    _write_descriptor_and_impl(
        space_dir,
        name="reader",
        descriptor_extras={
            "service_id": "notion",
            "authority": ["read_pages"],
            "operations": [{"operation": "read_pages", "classification": "read"}],
            "audit_category": "notion",
        },
        impl_src=(
            "def execute(input_data, context):\n"
            "    cred = context.credentials.get()\n"
            "    return {'ok': True, 'token_prefix': cred.token[:5]}\n"
        ),
    )
    await ws.register_tool("discord:owner", "space_a", "reader.tool.json")
    # Credential the member.
    store = ws._credential_store_for("discord:owner")
    store.add(member_id="mem_alice", service_id="notion", token="secret_abcdef")
    # Invoke.
    result_str = await ws.execute_workspace_tool(
        "discord:owner", "reader", {}, str(tmp_path),
        member_id="mem_alice",
    )
    result = json.loads(result_str)
    assert result == {"ok": True, "token_prefix": "secre"}
    # Audit entry written with the workshop primitive's shape.
    assert len(audit.entries) == 1
    instance_id, entry = audit.entries[0]
    assert instance_id == "discord:owner"
    assert entry["tool_name"] == "reader"
    assert entry["service_id"] == "notion"
    assert entry["normalized_category"] == "tool.invocation.external_service"
    assert entry["operation"] == "read_pages"
    assert entry["payload_digest"] != ""
    assert entry["success"] is True


@pytest.mark.asyncio
async def test_service_bound_dispatch_fails_without_credential(workspace):
    ws, catalog, _services, audit, tmp_path = workspace
    space_dir = tmp_path / "discord_owner" / "spaces" / "space_a" / "files"
    _write_descriptor_and_impl(
        space_dir,
        name="reader",
        descriptor_extras={
            "service_id": "notion",
            "authority": ["read_pages"],
            "operations": [{"operation": "read_pages", "classification": "read"}],
        },
        impl_src="def execute(input_data, context): return {'ok': True}\n",
    )
    await ws.register_tool("discord:owner", "space_a", "reader.tool.json")
    # No credential added. Invocation should fail at runtime enforcement.
    result_str = await ws.execute_workspace_tool(
        "discord:owner", "reader", {}, str(tmp_path),
        member_id="mem_alice",
    )
    result = json.loads(result_str)
    assert "error" in result
    assert "No credential" in result["error"]
    # Audit entry written with success=False.
    assert len(audit.entries) == 1
    _instance, entry = audit.entries[0]
    assert entry["success"] is False
    assert "No credential" in entry["error"]


@pytest.mark.asyncio
async def test_service_bound_dispatch_respects_per_member_credentials(workspace):
    """Alice and bob both have notion credentials; the tool sees only
    the invoking member's token."""
    ws, _catalog, _services, _audit, tmp_path = workspace
    space_dir = tmp_path / "discord_owner" / "spaces" / "space_a" / "files"
    _write_descriptor_and_impl(
        space_dir,
        name="reader",
        descriptor_extras={
            "service_id": "notion",
            "authority": ["read_pages"],
            "operations": [{"operation": "read_pages", "classification": "read"}],
        },
        impl_src=(
            "def execute(input_data, context):\n"
            "    return {'token': context.credentials.get().token}\n"
        ),
    )
    await ws.register_tool("discord:owner", "space_a", "reader.tool.json")
    store = ws._credential_store_for("discord:owner")
    store.add(member_id="mem_alice", service_id="notion", token="alice-secret")
    store.add(member_id="mem_bob", service_id="notion", token="bob-secret")

    alice_result = json.loads(await ws.execute_workspace_tool(
        "discord:owner", "reader", {}, str(tmp_path),
        member_id="mem_alice",
    ))
    bob_result = json.loads(await ws.execute_workspace_tool(
        "discord:owner", "reader", {}, str(tmp_path),
        member_id="mem_bob",
    ))
    assert alice_result == {"token": "alice-secret"}
    assert bob_result == {"token": "bob-secret"}


@pytest.mark.asyncio
async def test_service_bound_dispatch_hash_check_fires_on_impl_edit(workspace):
    ws, _catalog, _services, audit, tmp_path = workspace
    space_dir = tmp_path / "discord_owner" / "spaces" / "space_a" / "files"
    desc_path, impl_path = _write_descriptor_and_impl(
        space_dir,
        name="reader",
        descriptor_extras={
            "service_id": "notion",
            "authority": ["read_pages"],
            "operations": [{"operation": "read_pages", "classification": "read"}],
        },
        impl_src="def execute(input_data, context): return {'v': 1}\n",
    )
    await ws.register_tool("discord:owner", "space_a", "reader.tool.json")
    store = ws._credential_store_for("discord:owner")
    store.add(member_id="mem_alice", service_id="notion", token="x")

    # Edit the implementation post-registration.
    impl_path.write_text("def execute(input_data, context): return {'v': 2, 'evil': True}\n")

    result = json.loads(await ws.execute_workspace_tool(
        "discord:owner", "reader", {}, str(tmp_path),
        member_id="mem_alice",
    ))
    assert "edited since" in result["error"]


@pytest.mark.asyncio
async def test_service_bound_dispatch_refuses_disabled_service_with_dedicated_audit(
    workspace,
):
    """INSTALL-FOR-STOCK-CONNECTORS Section 2 + 11: dispatch refuses
    invocation of a disabled service-bound tool, and the refusal
    lands under audit category install.dispatch_refused_disabled_service
    (distinct from the workshop's per-tool audit shape)."""
    ws, _catalog, _services, audit, tmp_path = workspace
    # Flip notion to disabled (the workspace fixture had enabled it).
    from kernos.setup.service_state import (
        ServiceStateSource,
        ServiceStateUpdatedBy,
    )
    ws.service_state_store().set(
        "notion",
        enabled=False,
        source=ServiceStateSource.OPERATOR,
        updated_by=ServiceStateUpdatedBy.OPERATOR,
        reason="disabled for this test",
    )

    space_dir = tmp_path / "discord_owner" / "spaces" / "space_a" / "files"
    _write_descriptor_and_impl(
        space_dir,
        name="reader",
        descriptor_extras={
            "service_id": "notion",
            "authority": ["read_pages"],
            "operations": [{"operation": "read_pages", "classification": "read"}],
        },
        impl_src="def execute(input_data, context): return {'ok': True}\n",
    )
    await ws.register_tool("discord:owner", "space_a", "reader.tool.json")
    # Add a credential so the only refusal path is the disabled flag.
    store = ws._credential_store_for("discord:owner")
    store.add(member_id="mem_alice", service_id="notion", token="x")

    result = json.loads(await ws.execute_workspace_tool(
        "discord:owner", "reader", {}, str(tmp_path),
        member_id="mem_alice",
    ))
    assert "disabled" in result["error"]
    assert "notion" in result["error"]
    assert "kernos services enable" in result["error"]

    # Audit entry uses the dedicated install.* category, NOT the
    # workshop's tool.invocation shape.
    assert len(audit.entries) == 1
    instance_id, entry = audit.entries[0]
    assert instance_id == "discord:owner"
    assert entry["audit_category"] == "install.dispatch_refused_disabled_service"
    assert entry["service_id"] == "notion"
    assert entry["tool_name"] == "reader"


@pytest.mark.asyncio
async def test_register_tool_finds_and_normalizes_nested_descriptor(workspace):
    """v1 self-test: the model sometimes builds the tool inside a subdir
    (flip_coin_tool/flip_coin.tool.json) instead of the files root.
    register_tool must find it by basename, register it, and copy the
    descriptor + impl up to the root so the runtime loaders (which read the
    root by basename) can load the tool on first call."""
    ws, catalog, _services, _audit, tmp_path = workspace
    files_root = tmp_path / "discord_owner" / "spaces" / "space_a" / "files"
    nested = files_root / "flip_coin_pkg"
    _write_descriptor_and_impl(
        nested,
        name="flip_coin",
        descriptor_extras={},
        impl_src="def execute(input_data): return {'result': 'heads'}\n",
    )
    # only the bare basename is passed (path separators are rejected)
    msg = await ws.register_tool("discord:owner", "space_a", "flip_coin.tool.json")
    assert "Registered" in msg, msg
    assert catalog.get("flip_coin") is not None
    # normalized to the files root so runtime loading works
    assert (files_root / "flip_coin.tool.json").is_file()
    assert (files_root / "flip_coin.py").is_file()
