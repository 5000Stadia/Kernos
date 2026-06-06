"""v1 self-test bug #4: a plain "remember this fact" had no surfaced home.
`remember` is search-only; the write-side `note_this` was not pinned, so the
store side of memory wasn't always available. Both sides must be pinned.
"""
from kernos.kernel.tool_catalog import ALWAYS_PINNED


def test_memory_read_and_write_both_pinned():
    assert "remember" in ALWAYS_PINNED       # search side
    assert "note_this" in ALWAYS_PINNED      # write side (the fix)
