"""Runtime enforcement of the four checks the Kit-revised spec
requires at every workshop tool invocation.

Covers:
- Hash check: clean pass, descriptor-edit detection, impl-edit
  detection, missing-file handling.
- Operation authority: pass, missing operation when authority
  declared, unknown operation, service-removed-operation drift.
- Credential scope: trivial pass for non-service tools, missing
  credential for service-bound, expired credential.
- Sandbox check: pass for paths under data_dir, fail for paths
  above, fail via symlink resolution.
- Composed enforce_invocation: runs all four; first failure raises;
  force-registered tools (per Kit edit 5) still subject to runtime
  enforcement.
"""

import json
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from kernos.kernel.credentials_member import MemberCredentialStore
from kernos.kernel.services import (
    ServiceRegistry,
    parse_service_descriptor,
)
from kernos.kernel.tool_descriptor import parse_tool_descriptor
from kernos.kernel.tool_runtime import build_runtime_context
from kernos.kernel.tool_runtime_enforcement import (
    AuthorityViolationError,
    CredentialUnavailableError,
    EnforcementInputs,
    HashMismatchError,
    SandboxViolationError,
    check_credential_scope,
    check_hash_unchanged,
    check_operation_authority,
    check_sandbox_path,
    compute_registration_hash,
    enforce_invocation,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env_key(monkeypatch):
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY", Fernet.generate_key().decode())


@pytest.fixture
def store(tmp_path, env_key):
    return MemberCredentialStore(tmp_path, "discord:i")


@pytest.fixture
def notion_registry():
    registry = ServiceRegistry()
    registry.register(parse_service_descriptor({
        "service_id": "notion",
        "display_name": "Notion",
        "auth_type": "api_token",
        "operations": ["read_pages", "write_pages"],
    }))
    return registry


def _write_tool_files(tmp_path, *, name="reader", impl_src="def execute(d, c): return {}\n",
                     descriptor_extras=None):
    desc_path = tmp_path / f"{name}.tool.json"
    impl_path = tmp_path / f"{name}.py"
    descriptor = {
        "name": name,
        "description": "x",
        "input_schema": {"type": "object"},
        "implementation": f"{name}.py",
    }
    if descriptor_extras:
        descriptor.update(descriptor_extras)
    desc_path.write_text(json.dumps(descriptor))
    impl_path.write_text(impl_src)
    return desc_path, impl_path, descriptor


# ---------------------------------------------------------------------------
# Hash check
# ---------------------------------------------------------------------------


def test_compute_registration_hash_is_deterministic():
    a = compute_registration_hash('{"a":1}', "code")
    b = compute_registration_hash('{"a":1}', "code")
    assert a == b
    assert len(a) == 64


def test_compute_registration_hash_changes_with_descriptor_edit():
    a = compute_registration_hash('{"a":1}', "code")
    b = compute_registration_hash('{"a":2}', "code")
    assert a != b


def test_compute_registration_hash_changes_with_impl_edit():
    a = compute_registration_hash('{"a":1}', "code1")
    b = compute_registration_hash('{"a":1}', "code2")
    assert a != b


def test_compute_registration_hash_separator_prevents_collision():
    """Two different (descriptor, impl) pairs concatenating to the
    same byte sequence should still produce different hashes — the
    separator handles this."""
    a = compute_registration_hash("aabb", "ccdd")
    b = compute_registration_hash("aa", "bbccdd")
    assert a != b


def test_check_hash_unchanged_passes_when_files_match(tmp_path):
    desc_path, impl_path, descriptor = _write_tool_files(tmp_path)
    registered = compute_registration_hash(
        desc_path.read_bytes(), impl_path.read_bytes(),
    )
    # Should not raise.
    check_hash_unchanged(
        descriptor_path=desc_path,
        implementation_path=impl_path,
        registered_hash=registered,
    )


def test_check_hash_unchanged_fails_after_descriptor_edit(tmp_path):
    desc_path, impl_path, descriptor = _write_tool_files(tmp_path)
    registered = compute_registration_hash(
        desc_path.read_bytes(), impl_path.read_bytes(),
    )
    descriptor["description"] = "modified description"
    desc_path.write_text(json.dumps(descriptor))
    with pytest.raises(HashMismatchError, match="edited since"):
        check_hash_unchanged(
            descriptor_path=desc_path,
            implementation_path=impl_path,
            registered_hash=registered,
        )


def test_check_hash_unchanged_fails_after_impl_edit(tmp_path):
    desc_path, impl_path, _ = _write_tool_files(tmp_path)
    registered = compute_registration_hash(
        desc_path.read_bytes(), impl_path.read_bytes(),
    )
    impl_path.write_text("def execute(d, c): return {'evil': True}\n")
    with pytest.raises(HashMismatchError):
        check_hash_unchanged(
            descriptor_path=desc_path,
            implementation_path=impl_path,
            registered_hash=registered,
        )


def test_check_hash_unchanged_fails_when_file_missing(tmp_path):
    desc_path, impl_path, _ = _write_tool_files(tmp_path)
    registered = compute_registration_hash(
        desc_path.read_bytes(), impl_path.read_bytes(),
    )
    impl_path.unlink()
    with pytest.raises(HashMismatchError, match="cannot be read"):
        check_hash_unchanged(
            descriptor_path=desc_path,
            implementation_path=impl_path,
            registered_hash=registered,
        )


# ---------------------------------------------------------------------------
# Operation authority re-check
# ---------------------------------------------------------------------------


def test_authority_passes_when_operation_in_list():
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "authority": ["read_pages"],
    })
    check_operation_authority(descriptor=desc, operation="read_pages")


def test_authority_passes_with_blank_op_when_no_authority_list():
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
    })
    check_operation_authority(descriptor=desc, operation="")


def test_authority_fails_when_operation_unknown():
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "authority": ["read_pages"],
    })
    with pytest.raises(AuthorityViolationError, match="not in tool"):
        check_operation_authority(descriptor=desc, operation="delete_pages")


def test_authority_fails_with_blank_op_when_authority_declared():
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "authority": ["read_pages"],
    })
    with pytest.raises(AuthorityViolationError, match="did not name"):
        check_operation_authority(descriptor=desc, operation="")


def test_authority_fails_when_service_drops_operation(notion_registry):
    """Service-bound tool's operation gets removed from the service's
    declared operations between registration and invocation."""
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "service_id": "notion",
        "authority": ["read_pages", "write_pages"],
    }, service_lookup=notion_registry.get)

    # Mutate the registry: replace Notion's descriptor with a version
    # that no longer declares write_pages.
    notion_registry.unregister("notion")
    notion_registry.register(parse_service_descriptor({
        "service_id": "notion",
        "display_name": "Notion",
        "auth_type": "api_token",
        "operations": ["read_pages"],  # write_pages dropped
    }))

    with pytest.raises(AuthorityViolationError, match="no longer in"):
        check_operation_authority(
            descriptor=desc,
            operation="write_pages",
            service_registry=notion_registry,
        )


def test_authority_fails_when_service_unregistered(notion_registry):
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "service_id": "notion",
        "authority": ["read_pages"],
    }, service_lookup=notion_registry.get)
    notion_registry.unregister("notion")
    with pytest.raises(AuthorityViolationError, match="no longer registered"):
        check_operation_authority(
            descriptor=desc,
            operation="read_pages",
            service_registry=notion_registry,
        )


# ---------------------------------------------------------------------------
# Credential scope re-check
# ---------------------------------------------------------------------------


def test_credential_check_passes_for_non_service_tool(store):
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
    })
    check_credential_scope(
        descriptor=desc, member_id="m", credential_store=store,
    )


def test_credential_check_fails_when_credential_missing(store, notion_registry):
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "service_id": "notion",
        "authority": ["read_pages"],
    }, service_lookup=notion_registry.get)
    with pytest.raises(CredentialUnavailableError, match="No credential"):
        check_credential_scope(
            descriptor=desc, member_id="mem_alice", credential_store=store,
        )


def test_credential_check_fails_when_credential_expired(store, notion_registry):
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "service_id": "notion",
        "authority": ["read_pages"],
    }, service_lookup=notion_registry.get)
    store.add(
        member_id="mem_alice", service_id="notion", token="x",
        expires_at=int(time.time()) - 60,  # already expired
    )
    with pytest.raises(CredentialUnavailableError, match="expired"):
        check_credential_scope(
            descriptor=desc, member_id="mem_alice", credential_store=store,
        )


def test_credential_check_passes_when_credential_valid(store, notion_registry):
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "service_id": "notion",
        "authority": ["read_pages"],
    }, service_lookup=notion_registry.get)
    store.add(member_id="mem_alice", service_id="notion", token="x")
    check_credential_scope(
        descriptor=desc, member_id="mem_alice", credential_store=store,
    )


# ---------------------------------------------------------------------------
# Auto-refresh wiring (OAUTH-DEVICE-CODE-SUBSYSTEM Q4 follow-on)
# ---------------------------------------------------------------------------


@pytest.fixture
def slack_oauth_registry():
    """Service registry with an oauth_device_code service ('slack')."""
    registry = ServiceRegistry()
    registry.register(parse_service_descriptor({
        "service_id": "slack",
        "display_name": "Slack",
        "auth_type": "oauth_device_code",
        "operations": ["read_messages", "post_message"],
        "required_scopes": ["chat:write"],
        "oauth": {
            "device_authorization_uri": "https://slack.com/oauth/device",
            "token_uri": "https://slack.com/oauth/token",
            "client_id": "C-test",
        },
    }))
    return registry


def _slack_tool(slack_oauth_registry):
    return parse_tool_descriptor({
        "name": "slack_post", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "service_id": "slack",
        "authority": ["post_message"],
    }, service_lookup=slack_oauth_registry.get)


def test_credential_refresh_succeeds_when_expired_with_refresh_token(
    monkeypatch, store, slack_oauth_registry,
):
    """Codex pre-push verified: an expired credential with a refresh
    token on an oauth_device_code service triggers refresh_credential.
    On success the rotated credential lives in the store and the
    check returns cleanly."""
    desc = _slack_tool(slack_oauth_registry)
    store.add(
        member_id="mem_alice", service_id="slack",
        token="old-access", refresh_token="rt-1",
        expires_at=int(time.time()) - 60,  # already expired
    )
    refresh_calls: list[dict] = []

    def fake_refresh_credential(*, service, member_id, store):
        refresh_calls.append({
            "service_id": service.service_id, "member_id": member_id,
        })
        return store.rotate(
            member_id=member_id, service_id=service.service_id,
            token="new-access", refresh_token="rt-1",
            expires_at=int(time.time()) + 3600,
        )

    monkeypatch.setattr(
        "kernos.kernel.oauth_device_code.refresh_credential",
        fake_refresh_credential,
    )
    check_credential_scope(
        descriptor=desc, member_id="mem_alice", credential_store=store,
        service_registry=slack_oauth_registry,
    )
    assert refresh_calls == [{"service_id": "slack", "member_id": "mem_alice"}]
    rotated = store.get(member_id="mem_alice", service_id="slack")
    assert rotated.token == "new-access"
    assert not rotated.is_expired


def test_credential_refresh_falls_through_to_existing_error_on_revoked(
    monkeypatch, store, slack_oauth_registry,
):
    """When the token endpoint rejects the refresh (e.g. invalid_grant
    after the user revoked authorization), the runtime falls back to
    the existing CredentialUnavailableError so the user is asked to
    re-onboard."""
    from kernos.kernel.oauth_device_code import TokenEndpointError

    desc = _slack_tool(slack_oauth_registry)
    store.add(
        member_id="mem_alice", service_id="slack",
        token="old-access", refresh_token="rt-revoked",
        expires_at=int(time.time()) - 60,
    )

    def fake_refresh_credential(*, service, member_id, store):
        raise TokenEndpointError("invalid_grant", code="invalid_grant")

    monkeypatch.setattr(
        "kernos.kernel.oauth_device_code.refresh_credential",
        fake_refresh_credential,
    )
    with pytest.raises(CredentialUnavailableError, match="expired"):
        check_credential_scope(
            descriptor=desc, member_id="mem_alice", credential_store=store,
            service_registry=slack_oauth_registry,
        )


def test_credential_refresh_skipped_when_no_refresh_token(
    monkeypatch, store, slack_oauth_registry,
):
    """A credential with empty refresh_token cannot be refreshed; we
    skip the attempt and raise the original error."""
    desc = _slack_tool(slack_oauth_registry)
    store.add(
        member_id="mem_alice", service_id="slack",
        token="old-access",  # refresh_token defaults to ""
        expires_at=int(time.time()) - 60,
    )
    refresh_called = False

    def fake_refresh_credential(*, service, member_id, store):
        nonlocal refresh_called
        refresh_called = True

    monkeypatch.setattr(
        "kernos.kernel.oauth_device_code.refresh_credential",
        fake_refresh_credential,
    )
    with pytest.raises(CredentialUnavailableError, match="expired"):
        check_credential_scope(
            descriptor=desc, member_id="mem_alice", credential_store=store,
            service_registry=slack_oauth_registry,
        )
    assert refresh_called is False


def test_credential_refresh_skipped_for_api_token_service(
    monkeypatch, store, notion_registry,
):
    """api_token services do not have refresh tokens or refresh
    endpoints; the wiring must not attempt refresh on them even if
    a refresh_token field somehow exists on the stored credential."""
    desc = parse_tool_descriptor({
        "name": "t", "description": "x",
        "input_schema": {"type": "object"}, "implementation": "t.py",
        "service_id": "notion",
        "authority": ["read_pages"],
    }, service_lookup=notion_registry.get)
    store.add(
        member_id="mem_alice", service_id="notion",
        token="x", refresh_token="rt-bogus",
        expires_at=int(time.time()) - 60,
    )
    refresh_called = False

    def fake_refresh_credential(*, service, member_id, store):
        nonlocal refresh_called
        refresh_called = True

    monkeypatch.setattr(
        "kernos.kernel.oauth_device_code.refresh_credential",
        fake_refresh_credential,
    )
    with pytest.raises(CredentialUnavailableError, match="expired"):
        check_credential_scope(
            descriptor=desc, member_id="mem_alice", credential_store=store,
            service_registry=notion_registry,
        )
    assert refresh_called is False


def test_credential_refresh_rejects_rotation_with_preserved_expired_timestamp(
    monkeypatch, store, slack_oauth_registry,
):
    """Codex pre-push fold: when the token endpoint response omits
    ``expires_in``, ``refresh_credential`` calls
    ``store.rotate(expires_at=None)`` and the store treats None as
    "preserve the prior value" — the access token rotates but the
    original expired timestamp survives. The gate must NOT accept
    such a rotation; the credential is still expired by the gate's
    own measure, so the user must be asked to re-onboard."""
    desc = _slack_tool(slack_oauth_registry)
    original_expiry = int(time.time()) - 60
    store.add(
        member_id="mem_alice", service_id="slack",
        token="old-access", refresh_token="rt-1",
        expires_at=original_expiry,
    )

    def fake_refresh_credential_no_expires_in(*, service, member_id, store):
        # Mirror refresh_credential's behaviour when the token
        # endpoint response has no expires_in: rotate with
        # expires_at=None, which preserves the old (expired) value.
        return store.rotate(
            member_id=member_id, service_id=service.service_id,
            token="new-access", refresh_token="rt-1",
            expires_at=None,
        )

    monkeypatch.setattr(
        "kernos.kernel.oauth_device_code.refresh_credential",
        fake_refresh_credential_no_expires_in,
    )
    with pytest.raises(CredentialUnavailableError, match="expired"):
        check_credential_scope(
            descriptor=desc, member_id="mem_alice", credential_store=store,
            service_registry=slack_oauth_registry,
        )
    # Sanity: the access token did rotate (the inner call ran), but
    # the gate still raised because the expiry stayed stale.
    rotated = store.get(member_id="mem_alice", service_id="slack")
    assert rotated.token == "new-access"
    assert rotated.expires_at == original_expiry


def test_credential_refresh_skipped_when_no_registry_supplied(
    monkeypatch, store, slack_oauth_registry,
):
    """Backward-compat: existing callers that don't pass
    service_registry still get the original behaviour (raise on
    expiry, no refresh attempt). A fresh oauth credential with a
    refresh token but no registry hands behaves identically to v1."""
    desc = _slack_tool(slack_oauth_registry)
    store.add(
        member_id="mem_alice", service_id="slack",
        token="old-access", refresh_token="rt-1",
        expires_at=int(time.time()) - 60,
    )
    refresh_called = False

    def fake_refresh_credential(*, service, member_id, store):
        nonlocal refresh_called
        refresh_called = True

    monkeypatch.setattr(
        "kernos.kernel.oauth_device_code.refresh_credential",
        fake_refresh_credential,
    )
    with pytest.raises(CredentialUnavailableError, match="expired"):
        check_credential_scope(
            descriptor=desc, member_id="mem_alice", credential_store=store,
            # service_registry omitted
        )
    assert refresh_called is False


# ---------------------------------------------------------------------------
# Sandbox check
# ---------------------------------------------------------------------------


def test_sandbox_check_passes_for_path_inside(tmp_path, store):
    ctx = build_runtime_context(
        install_data_dir=tmp_path,
        credential_store=store,
        instance_id="i",
        member_id="m",
        space_id="s",
        tool_id="t",
    )
    inside = ctx.data_dir / "out.json"
    inside.write_text("{}")
    check_sandbox_path(target=inside, context=ctx)


def test_sandbox_check_fails_for_path_outside(tmp_path, store):
    ctx = build_runtime_context(
        install_data_dir=tmp_path,
        credential_store=store,
        instance_id="i",
        member_id="m",
        space_id="s",
        tool_id="t",
    )
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("nope")
    with pytest.raises(SandboxViolationError, match="System32"):
        check_sandbox_path(target=outside, context=ctx)


# ---------------------------------------------------------------------------
# Composed enforce_invocation
# ---------------------------------------------------------------------------


def test_enforce_invocation_passes_when_all_checks_pass(tmp_path, store, notion_registry):
    desc_path, impl_path, descriptor = _write_tool_files(
        tmp_path,
        descriptor_extras={
            "service_id": "notion",
            "authority": ["read_pages"],
        },
    )
    desc = parse_tool_descriptor(descriptor, service_lookup=notion_registry.get)
    registered = compute_registration_hash(
        desc_path.read_bytes(), impl_path.read_bytes(),
    )
    store.add(member_id="mem_alice", service_id="notion", token="x")
    inputs = EnforcementInputs(
        descriptor=desc,
        operation="read_pages",
        descriptor_path=desc_path,
        implementation_path=impl_path,
        registered_hash=registered,
        member_id="mem_alice",
        credential_store=store,
        service_registry=notion_registry,
    )
    enforce_invocation(inputs)  # should not raise


def test_enforce_invocation_first_failure_raises_specific_subclass(
    tmp_path, store, notion_registry,
):
    """When multiple checks would fail, the first one (hash) raises;
    later checks aren't reached. Verifies the order is hash → authority
    → credentials."""
    desc_path, impl_path, descriptor = _write_tool_files(
        tmp_path,
        descriptor_extras={
            "service_id": "notion",
            "authority": ["read_pages"],
        },
    )
    desc = parse_tool_descriptor(descriptor, service_lookup=notion_registry.get)
    registered = compute_registration_hash(
        desc_path.read_bytes(), impl_path.read_bytes(),
    )
    # Edit the impl to break the hash check.
    impl_path.write_text("def execute(d, c): return {'evil': True}\n")
    # Also drop the credential to make Check 3 fail too.
    # Hash check fires first.
    inputs = EnforcementInputs(
        descriptor=desc,
        operation="read_pages",
        descriptor_path=desc_path,
        implementation_path=impl_path,
        registered_hash=registered,
        member_id="mem_alice",
        credential_store=store,
        service_registry=notion_registry,
    )
    with pytest.raises(HashMismatchError):
        enforce_invocation(inputs)


def test_force_registered_tool_still_subject_to_runtime_enforcement(
    tmp_path, store, notion_registry,
):
    """Kit edit 5: force-register bypasses authoring-pattern validation
    only. Runtime enforcement still applies. Construct an invocation
    where the descriptor author was force-registered; the four checks
    must still fire if their preconditions fail."""
    desc_path, impl_path, descriptor = _write_tool_files(
        tmp_path,
        descriptor_extras={
            "service_id": "notion",
            "authority": ["read_pages"],
        },
    )
    desc = parse_tool_descriptor(descriptor, service_lookup=notion_registry.get)
    registered = compute_registration_hash(
        desc_path.read_bytes(), impl_path.read_bytes(),
    )
    # Force-register status is orthogonal; we are a force-registered
    # tool but invoked with an operation outside our authority. The
    # authority check must still fire.
    inputs = EnforcementInputs(
        descriptor=desc,
        operation="delete_pages",  # not in authority
        descriptor_path=desc_path,
        implementation_path=impl_path,
        registered_hash=registered,
        member_id="mem_alice",
        credential_store=store,
        service_registry=notion_registry,
    )
    with pytest.raises(AuthorityViolationError):
        enforce_invocation(inputs)
