"""Tests for authoring-pattern validation + runtime context.

Covers:
- Pattern detection (hardcoded paths, bare open, instance/member
  literals, secret env reads).
- Clean source passes.
- Force-register semantics — what bypasses authoring patterns vs.
  what doesn't (member isolation still enforced).
- Runtime context layout (AppData-style data_dir).
- Credential accessor scoping.
- Sandbox containment check.
"""

import textwrap
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from kernos.kernel.credentials_member import MemberCredentialStore
from kernos.kernel.tool_runtime import (
    ToolCredentialUnavailable,
    build_runtime_context,
    is_within_sandbox,
    resolve_tool_data_dir,
)
from kernos.kernel.tool_validation import (
    ValidationResult,
    validate_tool_file,
    validate_tool_source,
)


# ---------------------------------------------------------------------------
# Authoring-pattern validation
# ---------------------------------------------------------------------------


def test_clean_source_passes():
    source = textwrap.dedent("""
    def execute(input_data, context):
        path = context.data_dir / "report.json"
        path.write_text("{}")
        return {"ok": True}
    """)
    result = validate_tool_source(source)
    assert result.is_clean is True
    assert result.findings == ()


def test_hardcoded_absolute_path_caught():
    source = textwrap.dedent("""
    def execute(input_data, context):
        with open("/home/user/secrets/notes.txt") as f:
            return {"data": f.read()}
    """)
    result = validate_tool_source(source)
    assert not result.is_clean
    assert any(f.code == "bare_open_absolute" or f.code == "hardcoded_absolute_path"
               for f in result.findings)


def test_hardcoded_data_path_caught():
    source = textwrap.dedent("""
    def execute(input_data, context):
        path = "/data/discord_alice/secret.json"
        return {"path": path}
    """)
    result = validate_tool_source(source)
    assert not result.is_clean
    assert any(f.code == "hardcoded_absolute_path" for f in result.findings)


def test_instance_id_literal_caught():
    source = textwrap.dedent("""
    def execute(input_data, context):
        if context.instance_id == "discord:1234567":
            return {"hardcoded": True}
        return {}
    """)
    result = validate_tool_source(source)
    assert not result.is_clean
    assert any(f.code == "instance_id_literal" for f in result.findings)


def test_member_id_literal_caught():
    source = textwrap.dedent("""
    def execute(input_data, context):
        target_member = "mem_alice"
        return {"target": target_member}
    """)
    result = validate_tool_source(source)
    assert not result.is_clean
    assert any(f.code == "member_id_literal" for f in result.findings)


def test_secret_env_read_caught():
    source = textwrap.dedent("""
    import os
    def execute(input_data, context):
        token = os.environ.get("ANTHROPIC_API_KEY", "")
        return {"have_token": bool(token)}
    """)
    result = validate_tool_source(source)
    assert not result.is_clean
    assert any(f.code == "secret_env_read" for f in result.findings)


def test_credential_key_env_read_caught():
    source = textwrap.dedent("""
    import os
    def execute(input_data, context):
        return {"key": os.environ["KERNOS_CREDENTIAL_KEY"]}
    """)
    result = validate_tool_source(source)
    assert any(f.code == "secret_env_read" for f in result.findings)


def test_comment_lines_are_ignored():
    """Pattern matches in pure comment lines do not register findings."""
    source = textwrap.dedent("""
    # Note: do NOT do `open('/home/secrets/x')` — use context.data_dir.
    def execute(input_data, context):
        return {}
    """)
    result = validate_tool_source(source)
    assert result.is_clean


def test_render_message_includes_appdata_analogy():
    source = textwrap.dedent("""
    def execute(input_data, context):
        with open("/etc/passwd") as f:
            return {"d": f.read()}
    """)
    result = validate_tool_source(source)
    msg = result.render()
    assert "System32" in msg or "AppData" in msg


def test_validate_tool_file_round_trips(tmp_path):
    path = tmp_path / "tool.py"
    path.write_text("def execute(input_data, context): return {}\n")
    result = validate_tool_file(path)
    assert result.is_clean


def test_validate_tool_file_unreadable(tmp_path):
    """Missing file produces a single 'unreadable_source' finding rather
    than crashing."""
    result = validate_tool_file(tmp_path / "does-not-exist.py")
    assert not result.is_clean
    assert result.findings[0].code == "unreadable_source"


# ---------------------------------------------------------------------------
# Runtime context: data_dir layout
# ---------------------------------------------------------------------------


def test_resolve_tool_data_dir_layout(tmp_path):
    """Per Section 7: layout is <install>/<instance>/members/<member>/tools/<tool>/."""
    data_dir = resolve_tool_data_dir(
        install_data_dir=tmp_path,
        instance_id="discord:owner",
        member_id="mem_alice",
        tool_id="list_invoices",
    )
    expected = tmp_path / "discord_owner" / "members" / "mem_alice" / "tools" / "list_invoices"
    assert data_dir == expected


def test_build_runtime_context_creates_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY", Fernet.generate_key().decode())
    store = MemberCredentialStore(tmp_path, "discord:owner")
    ctx = build_runtime_context(
        install_data_dir=tmp_path,
        credential_store=store,
        instance_id="discord:owner",
        member_id="mem_alice",
        space_id="space_a",
        tool_id="list_invoices",
    )
    assert ctx.data_dir.exists()
    assert ctx.data_dir.is_dir()
    assert ctx.member_id == "mem_alice"
    assert ctx.tool_id == "list_invoices"


# ---------------------------------------------------------------------------
# Credential accessor scoping
# ---------------------------------------------------------------------------


def test_credential_accessor_returns_member_scoped_token(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY", Fernet.generate_key().decode())
    store = MemberCredentialStore(tmp_path, "i")
    store.add(member_id="mem_alice", service_id="notion", token="alice-token")
    store.add(member_id="mem_bob", service_id="notion", token="bob-token")

    ctx_alice = build_runtime_context(
        install_data_dir=tmp_path,
        credential_store=store,
        instance_id="i",
        member_id="mem_alice",
        space_id="s",
        tool_id="t",
        service_id="notion",
    )
    assert ctx_alice.credentials.get().token == "alice-token"
    assert ctx_alice.credentials.has_credential is True

    ctx_bob = build_runtime_context(
        install_data_dir=tmp_path,
        credential_store=store,
        instance_id="i",
        member_id="mem_bob",
        space_id="s",
        tool_id="t",
        service_id="notion",
    )
    assert ctx_bob.credentials.get().token == "bob-token"


def test_credential_accessor_raises_when_not_service_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY", Fernet.generate_key().decode())
    store = MemberCredentialStore(tmp_path, "i")
    ctx = build_runtime_context(
        install_data_dir=tmp_path,
        credential_store=store,
        instance_id="i",
        member_id="mem_alice",
        space_id="s",
        tool_id="t",
        # No service_id — not service-bound.
    )
    assert ctx.credentials.has_credential is False
    with pytest.raises(ToolCredentialUnavailable):
        ctx.credentials.get()


# ---------------------------------------------------------------------------
# Sandbox containment
# ---------------------------------------------------------------------------


def test_is_within_sandbox_accepts_paths_under(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    inside = sandbox / "child" / "file.txt"
    inside.parent.mkdir(parents=True)
    inside.write_text("x")
    assert is_within_sandbox(inside, sandbox) is True


def test_is_within_sandbox_rejects_paths_above(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")
    assert is_within_sandbox(outside, sandbox) is False


def test_is_within_sandbox_rejects_traversal_via_symlink(tmp_path):
    """A symlink inside the sandbox pointing outside should not pass
    once the path is resolved."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    link = sandbox / "linked.txt"
    link.symlink_to(outside)
    # The resolved target lives outside the sandbox.
    assert is_within_sandbox(link, sandbox) is False
