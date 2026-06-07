"""v1 self-test finding (2026-06-06): file I/O broke in a live turn because the
dispatcher read only the canonical ``name`` arg and silently defaulted to "".
A model asked in plain English to "read docs/X" / "save to results.md" emits
``path`` / ``filename`` / ``file`` instead, so the call reached the handler with
an empty filename → "Invalid filename ''". The resolver tolerates the synonyms.
"""
from kernos.kernel.reasoning import _resolve_file_name


def test_canonical_name_wins():
    assert _resolve_file_name({"name": "results.md"}) == "results.md"


def test_path_synonym_resolves():
    assert _resolve_file_name({"path": "docs/V1-SELF-TEST.md"}) == "docs/V1-SELF-TEST.md"


def test_filename_synonym_resolves():
    assert _resolve_file_name({"filename": "notes.txt"}) == "notes.txt"


def test_file_and_filepath_synonyms_resolve():
    assert _resolve_file_name({"file": "a.txt"}) == "a.txt"
    assert _resolve_file_name({"filepath": "b/c.md"}) == "b/c.md"
    assert _resolve_file_name({"file_name": "d.md"}) == "d.md"


def test_path_is_canonical_over_name():
    # path is now the canonical schema field (SAE-V1 adopt-the-shape); it wins
    assert _resolve_file_name({"name": "old.md", "path": "canon.md"}) == "canon.md"


def test_empty_or_whitespace_is_skipped():
    # blank canonical falls through to a real synonym
    assert _resolve_file_name({"name": "  ", "path": "real.md"}) == "real.md"


def test_truly_empty_returns_empty_string():
    # preserves the handler's existing "Invalid filename ''" behavior
    assert _resolve_file_name({}) == ""
    assert _resolve_file_name({"content": "x"}) == ""
