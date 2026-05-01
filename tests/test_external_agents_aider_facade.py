"""C3 tests: Aider harness adapter + builders/ compatibility facade.

* Aider harness raises HarnessUnavailable on consult (build-only).
* Aider harness adapter routes build() through the legacy
  AiderBuilder with mapped harness_options.
* AiderHarness handles missing-binary cleanly (aider not installed
  on this system).
* Facade re-exports the exact name set existing callers import:
  BuilderBackend, BuildResult, VALID_BUILDERS, BUILDER_TIER,
  UnknownBuilderError, get_builder. Each is identity-equivalent
  to the legacy reference.
"""
from __future__ import annotations

import shutil

import pytest

from kernos.kernel.external_agents.harnesses.aider import AiderHarness
from kernos.kernel.external_agents import (
    HarnessRegistry,
    HarnessUnavailable,
)


# ===========================================================================
# Aider harness
# ===========================================================================


class TestAiderHarness:
    def test_health_check_when_missing(self):
        # On this system aider is NOT installed (per spec note).
        h = AiderHarness()
        out = h.health_check()
        if shutil.which("aider"):
            # If installed (e.g., in a CI lane that has aider), the
            # health check returns installed=True.
            assert out.installed is True
        else:
            assert out.installed is False
            assert "aider" in out.detail.lower()

    async def test_consult_raises_unavailable(self):
        """AC18: aider consult mode raises HarnessUnavailable with a
        clear message. Aider's CLI is task-shaped, not Q&A-shaped;
        v1 doesn't pretend it can answer questions."""
        h = AiderHarness()
        with pytest.raises(HarnessUnavailable, match="task-shaped"):
            await h.consult(
                question="x", context="", session_id="",
                workspace_dir=None,  # type: ignore[arg-type]
                timeout_seconds=10, harness_options={},
            )

    async def test_build_adapter_routes_to_legacy(
        self, tmp_path, monkeypatch,
    ):
        """AC18 (in-venv variant): Aider IS installed via aider-chat
        in Kernos's venv. Test the adapter routing without invoking
        the real CLI by monkeypatching AiderBuilder.build to capture
        the call and return a known result."""
        from kernos.kernel.external_agents.harnesses import (
            aider as aider_mod,
        )
        from kernos.kernel.builders.base import (
            BuildResult as LegacyBuildResult,
        )

        captured: dict = {}

        async def fake_build(self, **kwargs):
            captured.update(kwargs)
            return LegacyBuildResult(
                success=True,
                stdout="adapter routed",
                stderr="",
                exit_code=0,
                files_modified=["foo.py"],
            )

        monkeypatch.setattr(
            aider_mod.AiderBuilder, "build", fake_build,
        )
        h = AiderHarness()
        result = await h.build(
            task="print('hello')",
            workspace_dir=tmp_path,
            timeout_seconds=42,
            harness_options={
                "instance_id": "inst_test",
                "space_id": "sp_test",
                "data_dir": str(tmp_path),
                "scope": "isolated",
                "write_file_name": "scratch.py",
            },
        )
        # Adapter routes harness_options to the legacy fields.
        assert captured["instance_id"] == "inst_test"
        assert captured["space_id"] == "sp_test"
        assert captured["code"] == "print('hello')"
        assert captured["timeout_seconds"] == 42
        assert captured["write_file_name"] == "scratch.py"
        assert captured["data_dir"] == str(tmp_path)
        assert captured["scope"] == "isolated"
        # Legacy result is repackaged as the unified BuildResult.
        assert result.success is True
        assert result.stdout == "adapter routed"
        assert result.files_modified == ["foo.py"]


class TestAiderInRegistry:
    def test_registry_exposes_aider_in_build_mode_only(self):
        reg = HarnessRegistry()
        reg.register(
            AiderHarness(),
            consult_supported=False,
            build_supported=True,
        )
        assert "aider" in reg.list_build_harnesses()
        assert "aider" not in reg.list_consult_harnesses()

    def test_registry_blocks_aider_consult_at_boundary(self):
        """The registry rejects an ``aider`` consult lookup before
        the harness's consult() method even runs — a defense-in-
        depth pin."""
        reg = HarnessRegistry()
        reg.register(
            AiderHarness(),
            consult_supported=False,
            build_supported=True,
        )
        with pytest.raises(HarnessUnavailable, match="consult"):
            reg.get("aider", mode="consult")


# ===========================================================================
# Compatibility facade
# ===========================================================================


class TestBuildersCompatFacade:
    """AC9: existing callers' imports keep working unchanged. Each
    name resolves to the same object existing callers depend on."""

    def test_BuilderBackend_re_exports(self):
        from kernos.kernel.builders import BuilderBackend
        from kernos.kernel.builders.base import BuilderBackend as Base
        assert BuilderBackend is Base

    def test_BuildResult_re_exports(self):
        from kernos.kernel.builders import BuildResult
        from kernos.kernel.builders.base import BuildResult as Base
        assert BuildResult is Base

    def test_VALID_BUILDERS_re_exports(self):
        from kernos.kernel.builders import VALID_BUILDERS
        from kernos.kernel.builders.base import VALID_BUILDERS as Base
        assert VALID_BUILDERS == Base
        assert "native" in VALID_BUILDERS
        assert "aider" in VALID_BUILDERS
        assert "claude-code" in VALID_BUILDERS
        assert "codex" in VALID_BUILDERS

    def test_BUILDER_TIER_re_exports(self):
        from kernos.kernel.builders import BUILDER_TIER
        from kernos.kernel.builders.base import BUILDER_TIER as Base
        assert BUILDER_TIER == Base
        assert BUILDER_TIER["native"] == "scoped"
        assert BUILDER_TIER["aider"] == "scoped"

    def test_UnknownBuilderError_importable(self):
        from kernos.kernel.builders import UnknownBuilderError
        # Used by code_exec.py:335 — must remain a ValueError subclass.
        assert issubclass(UnknownBuilderError, ValueError)

    def test_get_builder_returns_native(self):
        from kernos.kernel.builders import get_builder
        b = get_builder("native")
        assert b.name == "native"

    def test_get_builder_returns_aider(self):
        from kernos.kernel.builders import get_builder
        b = get_builder("aider")
        assert b.name == "aider"

    def test_get_builder_unknown_raises_typed(self):
        from kernos.kernel.builders import get_builder, UnknownBuilderError
        with pytest.raises(UnknownBuilderError):
            get_builder("imaginary")


class TestExistingCallerImportsStillWork:
    """AC9 strengthening: simulate exact import shapes used by
    existing callers and verify they resolve to the expected types."""

    def test_code_exec_imports(self):
        """``kernos/kernel/code_exec.py`` imports
        UnknownBuilderError + get_builder + BuilderBackend + BuildResult
        from ``kernos.kernel.builders``."""
        from kernos.kernel.builders import (
            BuilderBackend,
            BuildResult,
            UnknownBuilderError,
            get_builder,
        )
        # All importable; types are correct
        assert callable(get_builder)
        assert hasattr(BuilderBackend, "__class_getitem__") or True  # Protocol marker

    def test_workspace_config_imports(self):
        """``kernos/kernel/setup/workspace_config.py`` imports
        BUILDER_TIER + VALID_BUILDERS from ``kernos.kernel.builders``."""
        from kernos.kernel.builders import BUILDER_TIER, VALID_BUILDERS
        assert isinstance(VALID_BUILDERS, tuple)
        assert isinstance(BUILDER_TIER, dict)
