"""Pin: execute_code works when data_dir is relative (production default).

The bot's production default is ``data_dir = os.getenv("KERNOS_DATA_DIR", "./data")``
— always relative. _run_native renders launcher paths as string literals
into the subprocess; if those paths are relative, the subprocess resolves
them against its own cwd (= space_dir), producing a doubled path that
yields ModuleNotFoundError on sandbox_preamble. This pin runs execute_code
with a relative data_dir to lock the abspath normalization.
"""
from __future__ import annotations
import os
import pytest
from kernos.kernel.code_exec import execute_code


@pytest.fixture
def isolated_scope(monkeypatch):
    monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "isolated")
    monkeypatch.delenv("KERNOS_BUILDER", raising=False)
    yield


async def test_execute_code_works_with_relative_data_dir(
    tmp_path, isolated_scope, monkeypatch,
):
    """Pin against the relative-path → doubled-path → ModuleNotFoundError
    regression. Run with cwd=tmp_path and a relative data_dir; the launcher
    must still resolve sandbox_preamble cleanly."""
    monkeypatch.chdir(tmp_path)
    result = await execute_code(
        "t1", "sp1", 'print("hi from relative-dir")',
        data_dir="./data",  # relative — the production default shape
    )
    assert result["success"] is True, (
        f"execute_code failed with relative data_dir; "
        f"stderr={result.get('stderr', '')!r}"
    )
    assert "hi from relative-dir" in result["stdout"]
