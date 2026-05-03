"""Tests for the service registry + auth-by-channel matrix."""

import json

import pytest

from kernos.kernel.services import (
    AUTH_CHANNEL_MATRIX,
    AuthType,
    ChannelType,
    DuplicateServiceError,
    IncompatibleAuthChannelError,
    ServiceDescriptor,
    ServiceDescriptorError,
    ServiceRegistry,
    assert_auth_channel_compatible,
    channel_alternatives_for,
    is_auth_channel_compatible,
    load_service_descriptor_file,
    parse_service_descriptor,
)


# ---------------------------------------------------------------------------
# Auth-by-channel matrix
# ---------------------------------------------------------------------------


def test_api_token_only_compatible_with_cli():
    assert is_auth_channel_compatible(AuthType.API_TOKEN, ChannelType.CLI) is True
    for non_cli in (ChannelType.DISCORD, ChannelType.SMS, ChannelType.TELEGRAM):
        assert is_auth_channel_compatible(AuthType.API_TOKEN, non_cli) is False


def test_oauth_device_code_works_on_every_channel():
    for channel in ChannelType:
        assert is_auth_channel_compatible(AuthType.OAUTH_DEVICE_CODE, channel) is True


def test_assert_auth_channel_compatible_raises_for_unsafe_pair():
    with pytest.raises(IncompatibleAuthChannelError) as excinfo:
        assert_auth_channel_compatible(AuthType.API_TOKEN, ChannelType.DISCORD)
    err = excinfo.value
    assert err.auth == AuthType.API_TOKEN
    assert err.channel == ChannelType.DISCORD
    # Error message names the alternative channel(s).
    assert "cli" in str(err).lower()


def test_assert_auth_channel_compatible_passes_for_safe_pair():
    # Should not raise.
    assert_auth_channel_compatible(AuthType.OAUTH_DEVICE_CODE, ChannelType.SMS)
    assert_auth_channel_compatible(AuthType.API_TOKEN, ChannelType.CLI)


def test_channel_alternatives_for_returns_sorted_list():
    alts = channel_alternatives_for(AuthType.OAUTH_DEVICE_CODE)
    assert [c.value for c in alts] == sorted(c.value for c in alts)


def test_cookie_upload_not_in_auth_type_enum():
    """cookie_upload is reserved for BROWSER-COOKIE-IMPORT; declaring it
    here without an implementation is the registration footgun the
    the design review-revised spec deliberately removed."""
    assert "cookie_upload" not in [a.value for a in AuthType]


def test_auth_channel_matrix_covers_every_auth_type():
    """Every AuthType must have a matrix entry, even if frozenset is empty.
    Forgetting an entry would silently allow all channels (KeyError handling
    in is_auth_channel_compatible returns frozenset for the lookup)."""
    for auth in AuthType:
        assert auth in AUTH_CHANNEL_MATRIX


# ---------------------------------------------------------------------------
# Descriptor parsing
# ---------------------------------------------------------------------------


def _valid_descriptor():
    return {
        "service_id": "notion",
        "display_name": "Notion",
        "auth_type": "api_token",
        "operations": ["read_pages", "write_pages"],
        "audit_category": "notion",
        "required_scopes": [],
        "notes": "Workspace knowledge base.",
    }


def test_parse_valid_descriptor():
    desc = parse_service_descriptor(_valid_descriptor())
    assert desc.service_id == "notion"
    assert desc.auth_type == AuthType.API_TOKEN
    assert desc.operations == ("read_pages", "write_pages")
    assert desc.audit_category == "notion"


def test_parse_descriptor_defaults_audit_category_to_service_id():
    raw = _valid_descriptor()
    raw.pop("audit_category")
    desc = parse_service_descriptor(raw)
    assert desc.audit_category == "notion"


def test_parse_descriptor_rejects_invalid_service_id():
    raw = _valid_descriptor()
    raw["service_id"] = "Notion"  # uppercase invalid
    with pytest.raises(ServiceDescriptorError, match="service_id"):
        parse_service_descriptor(raw)


def test_parse_descriptor_rejects_missing_display_name():
    raw = _valid_descriptor()
    raw["display_name"] = ""
    with pytest.raises(ServiceDescriptorError, match="display_name"):
        parse_service_descriptor(raw)


def test_parse_descriptor_rejects_unknown_auth_type():
    raw = _valid_descriptor()
    raw["auth_type"] = "cookie_upload"  # the deliberately-removed value
    with pytest.raises(ServiceDescriptorError, match="auth_type"):
        parse_service_descriptor(raw)


def test_parse_descriptor_rejects_invalid_operation_name():
    raw = _valid_descriptor()
    raw["operations"] = ["read_pages", "Write-Pages"]  # uppercase + hyphen
    with pytest.raises(ServiceDescriptorError, match="operation"):
        parse_service_descriptor(raw)


def test_parse_descriptor_accepts_empty_operations():
    raw = _valid_descriptor()
    raw["operations"] = []
    desc = parse_service_descriptor(raw)
    assert desc.operations == ()


def test_supports_operation_check():
    desc = parse_service_descriptor(_valid_descriptor())
    assert desc.supports_operation("read_pages") is True
    assert desc.supports_operation("delete_pages") is False


def test_load_service_descriptor_file(tmp_path):
    path = tmp_path / "notion.service.json"
    path.write_text(json.dumps(_valid_descriptor()))
    desc = load_service_descriptor_file(path)
    assert desc.service_id == "notion"


def test_load_service_descriptor_file_rejects_invalid_json(tmp_path):
    path = tmp_path / "broken.service.json"
    path.write_text("{not json")
    with pytest.raises(ServiceDescriptorError, match="Invalid JSON"):
        load_service_descriptor_file(path)


# ---------------------------------------------------------------------------
# ServiceRegistry
# ---------------------------------------------------------------------------


def test_register_and_get():
    registry = ServiceRegistry()
    desc = parse_service_descriptor(_valid_descriptor())
    registry.register(desc)
    assert registry.get("notion") is desc
    assert registry.has("notion") is True


def test_get_returns_none_for_unknown_service():
    assert ServiceRegistry().get("nope") is None


def test_register_rejects_duplicates():
    registry = ServiceRegistry()
    desc = parse_service_descriptor(_valid_descriptor())
    registry.register(desc)
    with pytest.raises(DuplicateServiceError, match="already registered"):
        registry.register(desc)


def test_unregister_returns_true_then_false():
    registry = ServiceRegistry()
    desc = parse_service_descriptor(_valid_descriptor())
    registry.register(desc)
    assert registry.unregister("notion") is True
    assert registry.unregister("notion") is False
    assert registry.has("notion") is False


def test_list_services_returns_sorted():
    registry = ServiceRegistry()
    for sid in ("github", "notion", "slack"):
        raw = _valid_descriptor()
        raw["service_id"] = sid
        raw["display_name"] = sid.title()
        registry.register(parse_service_descriptor(raw))
    assert [d.service_id for d in registry.list_services()] == ["github", "notion", "slack"]


def test_supports_operation_via_registry():
    registry = ServiceRegistry()
    registry.register(parse_service_descriptor(_valid_descriptor()))
    assert registry.supports_operation("notion", "write_pages") is True
    assert registry.supports_operation("notion", "delete_pages") is False
    assert registry.supports_operation("nonexistent", "anything") is False


def test_load_stock_dir_loads_files_with_correct_suffix(tmp_path):
    # Two valid descriptors plus a junk file that should be ignored.
    for sid in ("notion", "github"):
        raw = _valid_descriptor()
        raw["service_id"] = sid
        raw["display_name"] = sid.title()
        (tmp_path / f"{sid}.service.json").write_text(json.dumps(raw))
    (tmp_path / "readme.md").write_text("not a descriptor")
    (tmp_path / "broken.service.json").write_text("{not json")

    registry = ServiceRegistry()
    loaded = registry.load_stock_dir(tmp_path)
    assert loaded == 2
    assert registry.has("notion") and registry.has("github")
    # The broken file did not crash the loader.


def test_load_stock_dir_returns_zero_for_missing_dir(tmp_path):
    registry = ServiceRegistry()
    assert registry.load_stock_dir(tmp_path / "does-not-exist") == 0
