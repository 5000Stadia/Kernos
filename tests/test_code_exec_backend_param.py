"""C5 tests: code_exec gains an optional `backend` parameter.

Per-call backend choice overrides KERNOS_BUILDER env var; default
behavior (no backend supplied) is unchanged.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from kernos.kernel.builders.base import BuildResult


# We call execute_code with a stubbed builder and verify backend
# selection logic without invoking the real native subprocess.


@pytest.fixture
def fake_get_builder(monkeypatch):
    """Patch get_builder to record the requested name + return a
    deterministic stub backend. Keeps tests fast and independent of
    real builders."""
    captured: dict = {"name": None}

    class _StubBackend:
        async def build(self, **_):
            return BuildResult(
                success=True, stdout="stub", exit_code=0,
                files_modified=[],
            )

    def _fake(name):
        captured["name"] = name
        return _StubBackend()

    monkeypatch.setattr(
        "kernos.kernel.builders.get_builder",
        _fake,
    )
    return captured


class TestBackendParam:
    async def test_per_call_backend_overrides_env(
        self, fake_get_builder, monkeypatch, tmp_path,
    ):
        from kernos.kernel.code_exec import execute_code

        monkeypatch.setenv("KERNOS_BUILDER", "native")
        await execute_code(
            instance_id="i", space_id="s", code="print('x')",
            data_dir=str(tmp_path),
            backend="aider",
        )
        assert fake_get_builder["name"] == "aider"

    async def test_no_backend_param_uses_env(
        self, fake_get_builder, monkeypatch, tmp_path,
    ):
        from kernos.kernel.code_exec import execute_code

        monkeypatch.setenv("KERNOS_BUILDER", "claude-code")
        await execute_code(
            instance_id="i", space_id="s", code="print('x')",
            data_dir=str(tmp_path),
        )
        assert fake_get_builder["name"] == "claude-code"

    async def test_no_backend_param_no_env_defaults_to_native(
        self, fake_get_builder, monkeypatch, tmp_path,
    ):
        from kernos.kernel.code_exec import execute_code

        monkeypatch.delenv("KERNOS_BUILDER", raising=False)
        await execute_code(
            instance_id="i", space_id="s", code="print('x')",
            data_dir=str(tmp_path),
        )
        assert fake_get_builder["name"] == "native"

    async def test_invalid_backend_returns_structured_error(
        self, monkeypatch, tmp_path,
    ):
        """Per-call invalid backend doesn't crash; returns the same
        structured error shape startup validation would yield."""
        from kernos.kernel.code_exec import execute_code

        result = await execute_code(
            instance_id="i", space_id="s", code="print('x')",
            data_dir=str(tmp_path),
            backend="imaginary",
        )
        assert result["success"] is False
        assert "imaginary" in result["error"]

    async def test_backend_normalized_to_lowercase(
        self, fake_get_builder, tmp_path,
    ):
        from kernos.kernel.code_exec import execute_code

        await execute_code(
            instance_id="i", space_id="s", code="print('x')",
            data_dir=str(tmp_path),
            backend="  AIDER  ",  # trim + lowercase
        )
        assert fake_get_builder["name"] == "aider"
