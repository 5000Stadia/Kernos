"""TOOL-REGISTRATION-AUTHORIZATION-V1 (2026-05-22) acceptance tests.

Covers spec ACs 1-16 (AC17 = no regressions, handled by the broader
sweep). The receipts-substrate consumer path is the focus: gating
on hard_write / external_agent_read, idempotency on hash, race
absence (catalog not surfaced until approved), and the activation
callback shape.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from kernos.kernel import approval_receipts as _approvals
from kernos.kernel.event_types import EventType
from kernos.kernel.services import ServiceRegistry, parse_service_descriptor
from kernos.kernel.tool_catalog import ToolCatalog
from kernos.kernel.workspace import WorkspaceManager


# ---------------------------------------------------------------------------
# Fixtures (mirror test_workspace_service_dispatch's shape)
# ---------------------------------------------------------------------------


@pytest.fixture
def env_key(monkeypatch):
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY", Fernet.generate_key().decode())


@pytest.fixture
async def workspace(tmp_path, env_key):
    """A WorkspaceManager wired against an empty catalog +
    receipts-substrate-ready data_dir."""
    catalog = ToolCatalog()
    services = ServiceRegistry()
    services.register(parse_service_descriptor({
        "service_id": "notion",
        "display_name": "Notion",
        "auth_type": "api_token",
        "operations": ["read_pages", "write_pages"],
    }))
    ws = WorkspaceManager(
        data_dir=str(tmp_path),
        catalog=catalog,
        service_registry=services,
    )
    # Ensure receipts schema exists in the test's data_dir.
    await _approvals.ensure_schema(str(tmp_path))
    return ws, catalog, str(tmp_path)


def _write_tool(
    tmp_path: Path, name: str, classification: str, impl_src: str = None,
) -> Path:
    space_dir = (
        tmp_path / "discord_owner" / "spaces" / "space_a" / "files"
    )
    space_dir.mkdir(parents=True, exist_ok=True)
    desc = {
        "name": name,
        "description": f"test tool {name}",
        "input_schema": {"type": "object"},
        "implementation": f"{name}.py",
        "gate_classification": classification,
    }
    (space_dir / f"{name}.tool.json").write_text(json.dumps(desc))
    (space_dir / f"{name}.py").write_text(
        impl_src or "def execute(input_data, context):\n    return {'ok': True}\n"
    )
    return space_dir


# ============================================================
# ACs 1-2: auto-approve classifications
# ============================================================


class TestAutoApproveClassifications:
    async def test_ac1_read_auto_approves(self, workspace):
        ws, catalog, data_dir = workspace
        _write_tool(Path(data_dir), "readonly_tool", "read")
        msg = await ws.register_tool(
            "discord:owner", "space_a", "readonly_tool.tool.json",
            data_dir=data_dir,
        )
        assert "Registered tool" in msg
        assert catalog.get("readonly_tool") is not None

    async def test_ac2_soft_write_auto_approves(self, workspace):
        ws, catalog, data_dir = workspace
        _write_tool(Path(data_dir), "soft_tool", "soft_write")
        msg = await ws.register_tool(
            "discord:owner", "space_a", "soft_tool.tool.json",
            data_dir=data_dir,
        )
        assert "Registered tool" in msg
        assert catalog.get("soft_tool") is not None

    async def test_ac16_unknown_classification_auto_approves(self, workspace):
        """AC16: descriptor without gate_classification defaults
        to auto-approve. Tighten in a future spec."""
        ws, catalog, data_dir = workspace
        # No gate_classification key in the descriptor
        space_dir = Path(data_dir) / "discord_owner" / "spaces" / "space_a" / "files"
        space_dir.mkdir(parents=True, exist_ok=True)
        desc = {
            "name": "uncategorized",
            "description": "no classification",
            "input_schema": {"type": "object"},
            "implementation": "uncategorized.py",
        }
        (space_dir / "uncategorized.tool.json").write_text(json.dumps(desc))
        (space_dir / "uncategorized.py").write_text(
            "def execute(input_data, context):\n    return {}\n"
        )
        msg = await ws.register_tool(
            "discord:owner", "space_a", "uncategorized.tool.json",
            data_dir=data_dir,
        )
        assert "Registered tool" in msg


# ============================================================
# ACs 3-5: hard_write + external_agent_read enter pending
# ============================================================


class TestGatedClassifications:
    async def test_ac3_hard_write_enters_pending(self, workspace):
        ws, catalog, data_dir = workspace
        _write_tool(Path(data_dir), "destructive", "hard_write")
        events = AsyncMock()
        msg = await ws.register_tool(
            "discord:owner", "space_a", "destructive.tool.json",
            member_id="owner_a",
            data_dir=data_dir,
            event_stream=events,
        )
        assert "pending owner approval" in msg
        assert "Request ID:" in msg
        # No catalog entry created
        assert catalog.get("destructive") is None

    async def test_ac4_external_agent_read_enters_pending(self, workspace):
        ws, catalog, data_dir = workspace
        _write_tool(Path(data_dir), "network_read", "external_agent_read")
        msg = await ws.register_tool(
            "discord:owner", "space_a", "network_read.tool.json",
            member_id="owner_a",
            data_dir=data_dir,
        )
        assert "pending owner approval" in msg
        assert catalog.get("network_read") is None

    async def test_ac5_receipt_payload_carries_metadata(self, workspace):
        ws, catalog, data_dir = workspace
        _write_tool(Path(data_dir), "destructive", "hard_write")
        msg = await ws.register_tool(
            "discord:owner", "space_a", "destructive.tool.json",
            member_id="owner_a",
            data_dir=data_dir,
        )
        # Extract approval_id from message
        request_id = msg.split("Request ID: ")[1].split(".")[0].strip()
        receipt = await _approvals.get_receipt(
            data_dir=data_dir, approval_id=request_id,
        )
        assert receipt is not None
        assert receipt["kind"] == "tool_registration"
        payload = json.loads(receipt["binding_payload_json"])
        assert payload["name"] == "destructive"
        assert payload["classification"] == "hard_write"
        assert payload["registration_hash"]


# ============================================================
# AC6: approve callback activates registration
# ============================================================


class TestApproveActivates:
    async def test_ac6_approve_creates_catalog_entry(self, workspace):
        ws, catalog, data_dir = workspace
        _write_tool(Path(data_dir), "destructive", "hard_write")
        # Issue pending
        msg = await ws.register_tool(
            "discord:owner", "space_a", "destructive.tool.json",
            member_id="owner_a", data_dir=data_dir,
        )
        request_id = msg.split("Request ID: ")[1].split(".")[0].strip()
        # Approve via receipts module directly (simulating /approve CONFIRM)
        events = AsyncMock()
        ok, _ = await _approvals.approve(
            data_dir=data_dir,
            approval_id=request_id,
            instance_id="discord:owner",
            invoking_member_id="owner_a",
            event_stream=events,
        )
        assert ok
        # Activation callback
        receipt = await _approvals.get_receipt(
            data_dir=data_dir, approval_id=request_id,
        )
        payload = json.loads(receipt["binding_payload_json"])
        activation_msg = await ws.activate_pending_registration(
            approval_id=request_id,
            binding_payload=payload,
        )
        assert "Registered tool" in activation_msg
        assert catalog.get("destructive") is not None


# ============================================================
# ACs 8-9: idempotency on hash
# ============================================================


class TestIdempotency:
    async def test_ac8_same_hash_twice_returns_same_request_id(self, workspace):
        ws, catalog, data_dir = workspace
        _write_tool(Path(data_dir), "destructive", "hard_write")
        msg_a = await ws.register_tool(
            "discord:owner", "space_a", "destructive.tool.json",
            member_id="owner_a", data_dir=data_dir,
        )
        msg_b = await ws.register_tool(
            "discord:owner", "space_a", "destructive.tool.json",
            member_id="owner_a", data_dir=data_dir,
        )
        rid_a = msg_a.split("Request ID: ")[1].split(".")[0].strip()
        # Second call mentions the SAME request_id
        assert rid_a in msg_b
        # Only one pending receipt in the table
        assert "already pending" in msg_b


# ============================================================
# AC7: reject surfaces the rejection reason on retry
# ============================================================


class TestRejectFlow:
    async def test_ac7_reject_surfaces_on_retry(self, workspace):
        ws, catalog, data_dir = workspace
        _write_tool(Path(data_dir), "destructive", "hard_write")
        msg = await ws.register_tool(
            "discord:owner", "space_a", "destructive.tool.json",
            member_id="owner_a", data_dir=data_dir,
        )
        request_id = msg.split("Request ID: ")[1].split(".")[0].strip()
        events = AsyncMock()
        await _approvals.reject(
            data_dir=data_dir,
            approval_id=request_id,
            instance_id="discord:owner",
            invoking_member_id="owner_a",
            reason="not appropriate for this space",
            event_stream=events,
        )
        # Retry with same hash → rejection surfaces
        retry_msg = await ws.register_tool(
            "discord:owner", "space_a", "destructive.tool.json",
            member_id="owner_a", data_dir=data_dir,
        )
        assert "previously rejected" in retry_msg
        assert "not appropriate" in retry_msg


# ============================================================
# AC10: pending tool absent from catalog (race protection)
# ============================================================


class TestRaceProtection:
    async def test_ac10_pending_tool_not_in_catalog(self, workspace):
        """Invoking a pending tool's name should not find it
        (catalog.register hasn't run yet)."""
        ws, catalog, data_dir = workspace
        _write_tool(Path(data_dir), "destructive", "hard_write")
        await ws.register_tool(
            "discord:owner", "space_a", "destructive.tool.json",
            member_id="owner_a", data_dir=data_dir,
        )
        assert catalog.get("destructive") is None


# ============================================================
# AC11: activation failure when descriptor missing
# ============================================================


class TestActivationFailure:
    async def test_ac11_missing_descriptor_post_approval(self, workspace):
        ws, catalog, data_dir = workspace
        space_dir = _write_tool(Path(data_dir), "destructive", "hard_write")
        msg = await ws.register_tool(
            "discord:owner", "space_a", "destructive.tool.json",
            member_id="owner_a", data_dir=data_dir,
        )
        request_id = msg.split("Request ID: ")[1].split(".")[0].strip()
        # Delete descriptor before approval activation
        (space_dir / "destructive.tool.json").unlink()
        receipt = await _approvals.get_receipt(
            data_dir=data_dir, approval_id=request_id,
        )
        payload = json.loads(receipt["binding_payload_json"])
        activation_msg = await ws.activate_pending_registration(
            approval_id=request_id, binding_payload=payload,
        )
        assert "no longer on disk" in activation_msg
        assert catalog.get("destructive") is None


# ============================================================
# AC12-13: event types pinned
# ============================================================


class TestEventTypes:
    def test_ac12_pending_event_registered(self):
        assert EventType.TOOL_REGISTRATION_PENDING.value == (
            "tool.registration_pending"
        )

    def test_ac13_approved_event_registered(self):
        assert EventType.TOOL_REGISTRATION_APPROVED.value == (
            "tool.registration_approved"
        )


# ============================================================
# AC15: edited-descriptor post-approval drift detection
# ============================================================


class TestHashDriftDetection:
    async def test_edited_impl_post_approval_aborts_activation(self, workspace):
        ws, catalog, data_dir = workspace
        space_dir = _write_tool(Path(data_dir), "destructive", "hard_write")
        msg = await ws.register_tool(
            "discord:owner", "space_a", "destructive.tool.json",
            member_id="owner_a", data_dir=data_dir,
        )
        request_id = msg.split("Request ID: ")[1].split(".")[0].strip()
        # Edit the impl AFTER pending was issued
        (space_dir / "destructive.py").write_text(
            "def execute(input_data, context):\n"
            "    return {'evil': True}\n"
        )
        receipt = await _approvals.get_receipt(
            data_dir=data_dir, approval_id=request_id,
        )
        payload = json.loads(receipt["binding_payload_json"])
        activation_msg = await ws.activate_pending_registration(
            approval_id=request_id, binding_payload=payload,
        )
        assert "edited since approval" in activation_msg
        assert catalog.get("destructive") is None


# ============================================================
# Receipts find-by-binding-field helpers
# ============================================================


class TestReceiptsLookupHelpers:
    async def test_find_pending_by_binding_field_returns_match(
        self, workspace,
    ):
        ws, catalog, data_dir = workspace
        _write_tool(Path(data_dir), "destructive", "hard_write")
        msg = await ws.register_tool(
            "discord:owner", "space_a", "destructive.tool.json",
            member_id="owner_a", data_dir=data_dir,
        )
        # Extract the registration_hash from the issued receipt
        request_id = msg.split("Request ID: ")[1].split(".")[0].strip()
        receipt = await _approvals.get_receipt(
            data_dir=data_dir, approval_id=request_id,
        )
        payload = json.loads(receipt["binding_payload_json"])
        found = await _approvals.find_pending_by_binding_field(
            data_dir=data_dir, instance_id="discord:owner",
            kind="tool_registration",
            field="registration_hash",
            value=payload["registration_hash"],
        )
        assert found is not None
        assert found["approval_id"] == request_id

    async def test_find_pending_by_binding_field_returns_none_no_match(
        self, workspace,
    ):
        ws, _, data_dir = workspace
        found = await _approvals.find_pending_by_binding_field(
            data_dir=data_dir, instance_id="discord:owner",
            kind="tool_registration",
            field="registration_hash",
            value="nonexistent_hash",
        )
        assert found is None
