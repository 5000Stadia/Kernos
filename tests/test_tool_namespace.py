"""SEMANTIC-ACTION-ENVELOPE-V1: the kernel-tool namespace map must stay complete
(every dispatchable kernel tool has exactly one namespace) and must round-trip
(to_namespaced → canonicalize back → flat), so presentation never desyncs from
dispatch.
"""
from kernos.kernel.reasoning import ReasoningService
from kernos.kernel.tool_namespace import (
    NAMESPACE_FOR,
    to_namespaced,
    SEPARATOR,
)
from kernos.kernel.tool_aliases import canonicalize_tool_name

# Kernel tools intentionally NOT namespaced (none today; kept explicit so a
# deliberate omission is visible rather than silent drift).
_UNNAMESPACED: set[str] = set()


def test_every_kernel_tool_has_a_namespace():
    kernel = set(ReasoningService._KERNEL_TOOLS)
    missing = kernel - set(NAMESPACE_FOR) - _UNNAMESPACED
    assert not missing, f"kernel tools missing a namespace: {sorted(missing)}"


def test_namespace_map_has_no_unknown_tools():
    kernel = set(ReasoningService._KERNEL_TOOLS)
    extra = set(NAMESPACE_FOR) - kernel
    assert not extra, f"namespace map references non-kernel tools: {sorted(extra)}"


def test_namespaced_names_round_trip_to_flat():
    kernel = set(ReasoningService._KERNEL_TOOLS)
    known = frozenset(kernel)
    for flat in NAMESPACE_FOR:
        ns_name = to_namespaced(flat)
        assert SEPARATOR in ns_name
        canon, repaired = canonicalize_tool_name(ns_name, known)
        assert canon == flat, f"{ns_name} did not canonicalize to {flat} (got {canon})"
        assert repaired is True


def test_unmapped_tool_returned_unchanged():
    # MCP/workshop tools aren't in the map → presented as-is
    assert to_namespaced("brave_web_search") == "brave_web_search"
    assert to_namespaced("flip_coin") == "flip_coin"


def test_compose_focus_prefers_exact_non_kernel_name():
    # a real non-kernel tool named like area__x resolves exactly (not flattened)
    from kernos.kernel.tool_introspection import compose_focus

    class _Cat:
        def get_metadata(self, name):
            if name == "files__summarize_csv":
                return {"source": "workspace", "description": "summarize a csv"}
            return None  # flattened "summarize_csv" must NOT resolve

    out = compose_focus(_Cat(), "files__summarize_csv")
    assert "summarize a csv" in out
    assert "isn't in the catalog" not in out


def test_compose_focus_flattens_kernel_namespaced_name():
    from kernos.kernel.tool_introspection import compose_focus

    class _Cat:
        def get_metadata(self, name):
            if name == "write_file":
                return {"source": "kernel", "description": "write a file"}
            return None  # the namespaced form is not a catalog key

    out = compose_focus(_Cat(), "files__write_file")
    assert "write a file" in out
