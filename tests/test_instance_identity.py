"""CLI-FIRST-CORE-V1 A4 — instance-identity resolver pins.

Substrate-fidelity shape: each test asserts the resolved value AND the
persisted marker state (the durable substrate), not just the return.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from kernos.setup.instance_identity import (
    AmbiguousInstanceIdentity,
    resolve_instance_id,
)


def _seed_members(data_dir: Path, instance_ids: list[str]) -> None:
    conn = sqlite3.connect(data_dir / "instance.db")
    conn.execute(
        "CREATE TABLE members (member_id TEXT, instance_id TEXT DEFAULT '')"
    )
    for i, iid in enumerate(instance_ids):
        conn.execute(
            "INSERT INTO members VALUES (?, ?)", (f"mem_{i}", iid)
        )
    conn.commit()
    conn.close()


def _marker(data_dir: Path) -> dict:
    return json.loads((data_dir / "instance_identity.json").read_text())


def test_env_wins_and_never_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNOS_INSTANCE_ID", "discord:12345")
    assert resolve_instance_id(tmp_path) == "discord:12345"
    # Explicit env does not write a marker — the env stays the source.
    assert not (tmp_path / "instance_identity.json").exists()


def test_legacy_single_tenant_adopted_and_persisted(tmp_path, monkeypatch):
    """A4 pin: existing no-env Discord data loads the same tenant."""
    monkeypatch.delenv("KERNOS_INSTANCE_ID", raising=False)
    _seed_members(tmp_path, ["discord:98765"])
    assert resolve_instance_id(tmp_path) == "discord:98765"
    marker = _marker(tmp_path)
    assert marker["instance_id"] == "discord:98765"
    assert marker["source"] == "legacy-adoption"
    # Second boot reads the marker (no re-scan dependency).
    assert resolve_instance_id(tmp_path) == "discord:98765"


def test_legacy_ambiguity_refuses_with_candidates(tmp_path, monkeypatch):
    monkeypatch.delenv("KERNOS_INSTANCE_ID", raising=False)
    _seed_members(tmp_path, ["discord:111", "phone:+15550000"])
    with pytest.raises(AmbiguousInstanceIdentity) as excinfo:
        resolve_instance_id(tmp_path)
    assert "discord:111" in str(excinfo.value)
    assert "phone:+15550000" in str(excinfo.value)
    assert "KERNOS_INSTANCE_ID" in str(excinfo.value)
    # Refusal persists nothing.
    assert not (tmp_path / "instance_identity.json").exists()


def test_fresh_dir_generates_and_persists_stably(tmp_path, monkeypatch):
    monkeypatch.delenv("KERNOS_INSTANCE_ID", raising=False)
    first = resolve_instance_id(tmp_path)
    assert first.startswith("kernos:")
    assert _marker(tmp_path)["source"] == "generated"
    # Stable across boots: the marker is the identity, never re-derived.
    assert resolve_instance_id(tmp_path) == first


def test_env_overrides_existing_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("KERNOS_INSTANCE_ID", raising=False)
    first = resolve_instance_id(tmp_path)
    monkeypatch.setenv("KERNOS_INSTANCE_ID", "explicit:override")
    assert resolve_instance_id(tmp_path) == "explicit:override"
    # Marker untouched — env is a per-process override, not a rewrite.
    assert _marker(tmp_path)["instance_id"] == first


def test_corrupt_marker_treated_as_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("KERNOS_INSTANCE_ID", raising=False)
    (tmp_path / "instance_identity.json").write_text("{not json")
    _seed_members(tmp_path, ["discord:222"])
    assert resolve_instance_id(tmp_path) == "discord:222"


def test_missing_members_table_is_fresh(tmp_path, monkeypatch):
    monkeypatch.delenv("KERNOS_INSTANCE_ID", raising=False)
    conn = sqlite3.connect(tmp_path / "instance.db")
    conn.execute("CREATE TABLE unrelated (x TEXT)")
    conn.commit()
    conn.close()
    assert resolve_instance_id(tmp_path).startswith("kernos:")
