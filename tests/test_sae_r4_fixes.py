"""SAE-V1 Codex review r4 folds: gate confirmation text reads the canonical
`path` arg; inspect_tools listings are collision-aware (no duplicate wire names).
"""
from kernos.kernel.tool_introspection import _display_name


def test_gate_describe_uses_path(monkeypatch):
    from kernos.kernel.gate import DispatchGate
    g = object.__new__(DispatchGate)  # _describe_action needs no state
    assert "notes.md" in g._describe_action("write_file", {"path": "notes.md"})
    assert "old.md" in g._describe_action("delete_file", {"path": "old.md"})
    # legacy `name` still works
    assert "leg.md" in g._describe_action("write_file", {"name": "leg.md"})
    # neither → graceful fallback
    assert "a file" in g._describe_action("write_file", {})


def test_display_name_namespaces_normally():
    taken = {"write_file", "manage_plan"}
    assert _display_name("write_file", taken) == "files__write_file"


def test_display_name_flat_on_collision():
    # a real tool already named files__write_file → keep kernel flat
    taken = {"write_file", "files__write_file"}
    assert _display_name("write_file", taken) == "write_file"
