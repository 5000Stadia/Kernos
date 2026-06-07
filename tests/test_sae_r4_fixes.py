"""SAE-V1 Codex review r4 fold: gate confirmation text reads the canonical
`path` arg (so blocked/proactive file ops name the real target).
"""


def test_gate_describe_uses_path(monkeypatch):
    from kernos.kernel.gate import DispatchGate
    g = object.__new__(DispatchGate)  # _describe_action needs no state
    assert "notes.md" in g._describe_action("write_file", {"path": "notes.md"})
    assert "old.md" in g._describe_action("delete_file", {"path": "old.md"})
    # legacy `name` still works
    assert "leg.md" in g._describe_action("write_file", {"name": "leg.md"})
    # neither → graceful fallback
    assert "a file" in g._describe_action("write_file", {})
